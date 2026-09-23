# -*- coding: utf-8 -*-
"""行业包加载：pack.yaml 解析、知识文件按章节切片、私有资料注入。

性能说明
--------
修复前 `file_text` / `private_facts` 每次调用都读盘 + `yaml.safe_load`，
而一次生成最多要写 3 轮（首轮 + 2 轮回炉）、每轮都要取回同一批知识文件 ——
同一个文件被反复解析十几遍。这里按 (mtime, size) 做进程内缓存：内容一变 mtime 就变，
无需手工失效；写入统一走 `fileio.write_atomic`，所以不会读到写了一半的文件。
"""
from __future__ import annotations

import logging
import re
import threading
from pathlib import Path

import yaml

from .checker import DEFAULT_RATE, quota_table_errors, tolerance_error, validate_banwords
from .intel import parse_sources
from .schemas import IntelSource as IntelSourceModel
from .fileio import read_yaml_file
from .schemas import PackInfo

log = logging.getLogger(__name__)


class PackError(RuntimeError):
    """行业包**不存在**（HTTP 404）。"""


class PackBrokenError(PackError):
    """行业包**存在但内容读不出来**（HTTP 409）。

    与 `PackError` 分开是必要的：调用方与用户要做的事完全不同 ——
    「不存在」是选错了包，「坏了」是包里的 YAML 需要人去修。
    把两者都映射成 404，用户会去列表里反复找一个明明就摆在那儿的包。

    为什么必须抛、不能退回默认值
    ----------------------------
    这几个文件读不出来时，**退回默认值恰好等于「最坏的结果」**：

    - `pack.yaml` 坏 → `params` 空、包名退成目录 slug、`param_audit` 变 `{}`
      （防线依赖它要审计的数据，与数据同生共死）；
    - `banwords.yaml` 坏 → 词表空 = 一条禁用词都查不出，
      **合规校验全过**看起来和「文案很干净」一模一样；
    - `skill.yaml` 坏 → 提示词退回内置默认，产出的脚本不再符合本行业口径；
    - `private/*.yaml` 坏 → 用户自己填的产品事实不注入，
      脚本里写的全是编的东西，而用户看不出来。

    四种都是「静默降级」——本项目最核心的风险取向就是让它**别静默**。
    """


# ── 只读文本缓存（按 mtime/size 失效）──────────────────────
_cache_lock = threading.Lock()
_text_cache: dict[str, tuple[float, int, str]] = {}
# 值 = (mtime, size, data, err)。**err 也要缓存** —— 否则每次调用都刷一条
# WARNING，一次生成要读十几遍 YAML，日志会被淹掉，真正的第一条反而看不见。
_yaml_cache: dict[str, tuple[float, int, dict, str]] = {}


def read_text_cached(path: Path) -> str:
    try:
        st = path.stat()
    except OSError:
        return ""
    key = str(path)
    with _cache_lock:
        hit = _text_cache.get(key)
        if hit and hit[0] == st.st_mtime and hit[1] == st.st_size:
            return hit[2]
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""
    with _cache_lock:
        _text_cache[key] = (st.st_mtime, st.st_size, text)
    return text


def read_yaml_cached(path: Path) -> tuple[dict, str]:
    """读 YAML（按 mtime/size 进程内缓存），返回 `(数据, 错误说明)`。

    ⚠ **返回值形态在 2026-09-15 从 `dict` 改成了 `(dict, str)`**（P1-5/P1-6）。
    原来失败一律 `return {}`，于是「文件写坏了」与「本来就没这个文件」
    不可区分 —— 前者会让 `banwords.yaml` 静默变成空词表、
    让 `pack.yaml` 静默变成空参数，**而 `param_audit` 这道防线自己也是读
    `pack.yaml` 算出来的**，于是防线与数据一起哑掉。

    改成元组而不是「顺手记个日志」是故意的：每个调用点都必须**显式**
    决定「读不出来时怎么办」，编译器/测试盯着它，不能靠自觉。
    实际读取在 `fileio.read_yaml_file`（全项目读 YAML 的唯一口径）。

    文件不存在 → `({}, "")`，这是正常状态，不是错误。
    """
    try:
        st = path.stat()
    except OSError:
        return {}, ""
    key = str(path)
    with _cache_lock:
        hit = _yaml_cache.get(key)
        if hit and hit[0] == st.st_mtime and hit[1] == st.st_size:
            return hit[2], hit[3]
    data, err = read_yaml_file(path)
    if err:
        log.warning("YAML 读取失败：%s —— %s", path, err)
    if not getattr(err, "retryable", False):
        # 只缓存"重读也不会变好"的那类（内容/语法坏，以及成功读取）。
        # 读不出字节（瞬时占用）如果也缓存，就把一个瞬时故障钉成永久：缓存键是
        # (mtime, size)，而读失败时这两个都没变 —— 下次命中缓存返回的还是那条错误，
        # 解除占用也救不回来，只能重启应用。实测见 `fileio.YamlError`。
        with _cache_lock:
            _yaml_cache[key] = (st.st_mtime, st.st_size, data, err)
    return data, err


def list_packs(root: Path) -> list[PackInfo]:
    """列出所有行业包。**一个坏包不该让整个列表（连带首页）崩掉**，
    但也**绝不能让坏包静默消失** —— 那是两条不同的要求，别只满足前一条。

    修复前这里是 `except Exception: continue`，无日志。后果实测（2026-09-15）：

        pack.yaml 里 `version: v2`（写成字符串）
          → pack_info 抛 ValueError → 被咽掉
          → list_packs 返回 []  → 包从界面上消失，用户以为包丢了
          → 而 /api/generate 那边 Pack() 抛的是裸 ValueError → HTTP 500

    所以「不崩」和「说清楚」要一起做：`pack_info` 保证不抛并带 `pack_error`，
    这里只剩「目录本身读不了」（权限 / 被占用）需要兜，而它**必须记日志**。
    """
    packs_dir = root / "packs"
    out: list[PackInfo] = []
    if not packs_dir.exists():
        return out
    try:
        entries = sorted(packs_dir.iterdir())
    except OSError as e:
        log.warning("行业包目录读取失败：%s —— %s", packs_dir, e)
        return out
    for d in entries:
        try:
            # 下划线开头的目录是**引擎内部骨架**（`packs/_template`），不是一份行业：
            # 它的 segment/audience 都是 {{占位}}，选它生成只会产出带花括号的稿子。
            # 过滤放在这里（而不是让 Pack 拒绝加载）—— packgen 与导出仍要能直接
            # 用 Pack(root, "_template") 取骨架文件。
            if d.name.startswith("_"):
                continue
            if d.is_dir() and (d / "pack.yaml").exists():
                out.append(pack_info(d))
        except OSError as e:
            # pack_info 已保证不抛，这里只可能是 is_dir()/exists() 自身的 IO 问题。
            # 跳过是对的，但**不能不说** —— 静默跳过就是「包凭空少了一个」。
            log.warning("行业包目录读取失败，已跳过：%s —— %s", d, e)
    return out


def pack_info(pack_dir: Path) -> PackInfo:
    """包的**展示信息**。**保证不抛** —— 一个包写坏了不该让整个列表消失。

    修复前这里读坏就是静默降级：包还在列表里、但参数条空掉、名字退成目录 slug，
    用户看到的是一份「长得不太对」的包，而不是一条「这个包坏了」的说明。
    现在把错误挂到 `PackInfo.pack_error` 上摊到界面，包**留着并标出来**。

    要兜**两类**坏法，缺一类就漏：

    1. **YAML 解析不了**（`err` 非空）—— 多一个缩进、少个引号。见 `read_yaml_cached`。
    2. **YAML 能解析，但内容不符合约定** —— `version: v2`（字符串喂给 `int()`）、
       `params.segment: 家用电梯`（字符串当映射用）。**这类不会触发 `YAMLError`**，
       所以第 1 类修完之后它照样能让 `pack_info` 抛。

    第 2 类修复前的后果最隐蔽（实测 2026-09-15）：
    `list_packs` 的 `except Exception: continue` 把它咽掉 → 包**从界面上消失**，
    用户以为包丢了；而 `/api/generate` 那边 `Pack()` 抛的是裸 `ValueError`
    → **HTTP 500**，既不是 404 也不是 409，前端只能显示「服务器错误」。

    真正要用它生成时必须用 `Pack` —— 那里会把 `pack_error` 转成
    `PackBrokenError`（409）。列表宽松、生成严格，是这个模块的分工。
    """
    dir_name = pack_dir.name
    data, err = read_yaml_cached(pack_dir / "pack.yaml")
    # 词表坏掉同样要摊到列表上：它会让合规校验静默全过，是本组问题里最严重的，
    # 不该只在点进参数条时才被发现。
    if not err:
        bw_rel = str(data.get("banwords", "banwords.yaml"))
        bw, bw_err = read_yaml_cached(pack_dir / bw_rel)
        err = bw_err
        if not err:
            # 「能解析但结构不符合约定」与 YAML 语法错是两类坏法（同 P2-7）。
            # 标量 hard 会被 Banwords 拆成单字再被 MIN_WORD_LEN 全丢 → 命中归零，
            # 而横幅还在报「N 个单字被忽略」—— 结构错误必须在这里就以
            # pack_error 亮出来（含词表文件名与键路径），不进入生成。
            fatal, _advisory = validate_banwords(bw, bw_rel)
            if fatal:
                err = "；".join(fatal)
    if not err:
        # 容差写错 = 静默改变「合格」的定义（0 → 永远不合格；字符串 → 校验处 TypeError
        # 冒成"包配置不完整"）。同一类坏法同一处收口：加载时就摊成 pack_error。
        terr = tolerance_error(data.get("duration_tolerance_pct"))
        if terr:
            err = terr
    if not err:
        # A-2：改写范围写错 = 静默退回默认档（包作者以为配了 in-place，拿到的是 bounded）。
        # 与容差同一处收口 —— 都是「pack.yaml 里一个枚举值写错，行为变了但没人知道」。
        serr = rewrite_scope_error(data.get("rewrite_scope"))
        if serr:
            err = serr
    try:
        return PackInfo(
            name=str(data.get("name", dir_name)),
            display_name=str(data.get("display_name", dir_name)),
            draft=bool(data.get("draft", False)),
            description=str(data.get("description", "")),
            version=int(data.get("version", 1) or 1),
            params=data.get("params", {}) or {},
            param_audit=param_audit(pack_dir, data),
            # 情报源声明（`需求方案 §2.2`）。与 `topics_map`/`audience_map` 同一读法：
            # `data.get(键, [])`，缺了就是空列表（= 这个包没盯任何源），
            # **不是错误** —— 情报是锦上添花，不是生成的前提。
            intel_sources=[IntelSourceModel(**s.as_dict())
                           for s in parse_sources(data.get("intel_sources", []))[0]],
            pack_error=err,
        )
    except Exception as e:                       # noqa: BLE001
        brief = f"{type(e).__name__}: {e}".replace("\n", " ")[:160]
        # 详情（含 traceback）只进本地日志；给用户的一句要短、要能指向字段名
        # （`invalid literal for int() ... 'v2'` 已经把字段点出来了）。
        # **这里不缓存**：与 `read_yaml_cached` 不同，`pack_info` 只由 /api/meta
        # 和 Pack() 调用（不是每次生成读十几遍），一条 WARNING 不算刷屏，
        # 而缓存反而会让「改好了」之后仍显示旧错误。
        log.warning("行业包 %s 的内容不符合约定：%s", pack_dir, brief, exc_info=True)
        return PackInfo(
            name=dir_name, display_name=dir_name,
            pack_error=f"pack.yaml 的内容不符合约定（{brief}）",
        )


# ── 文风 tell 词表的结构校验（P2-5）────────────────────────────
# 一本账：`pack_info`（列表/设置页）与 `Pack.ai_tells_data`（生成期）都读这里，
# 免得两边各判一次、结论还不一样。
# 形状由 `app/ai_tells.py` 决定：`strong` / `weak` 是**tell 名列表**，
# `lexicon` 是 **{{tell名: [词…]}}**。别的地方改这两个名字，这里跟着一起改。
_TELL_TIERS = ("strong", "weak")


def _tell_names() -> tuple:
    """引擎认识的 tell 名（用来判断「字符串当列表写」那种笔误要不要抢救）。"""
    from .ai_tells import AITells
    return AITells.ALL


def resolve_ai_tells(pack_dir: Path, pack_data: dict) -> tuple[dict | None, list[str]]:
    """读包的文风 tell 词表 → (可用的词表 | None=关闭, 给包作者看的说明)。

    **坏在结构上就只关那一格，绝不抛**（与 `banwords_data` 相反，理由是危害不对称）：
    词表空 = 合规校验一条都查不出（伪装成「没问题」），必须挡住生成；
    tell 词表空 = 提示词少一段，而它被读到的时机在 select **之后** —— 那里已经
    付过一次钱了。实测两种笔误都会炸：

        strong: [[no_specific, 通篇零具体]]   → severity[列表] → TypeError: unhashable
        lexicon: [bookish_connective]        → .items()       → AttributeError

    作业停在 `steps=['select']` + failed，钱花了、产物没有。
    所以这里的形状是：**能用的留下、坏掉的丢掉并写进 warnings**；一条都不剩时
    返回 None（= 关闭），让校验报告的 `ai_tells` 落 null，而不是「人味 100 分」。
    """
    rel = str((pack_data or {}).get("ai_tells", "ai_tells.yaml"))
    notes: list[str] = []
    if not (pack_dir / rel).exists():
        return None, []
    data, err = read_yaml_cached(pack_dir / rel)
    if err:
        return None, [f"{rel} 读不出来：{err} —— 本次生成的人味提示词已关闭"
                      "（不影响合规校验与时长判定，但请修好这份词表）"]
    if data in (None, "", []):
        return None, []
    if not isinstance(data, dict):
        return None, [f"{rel} 顶层必须是映射（现在是 {type(data).__name__}）"
                      " —— 人味提示词已关闭"]
    # ⚠ `read_yaml_cached` 给的是**进程级缓存里的同一个对象**：就地改会把修好的形状
    # 留给下一个加载者，于是第二次加载时警告自己消失了。一律先浅拷贝。
    out: dict = dict(data)
    lex = out.get("lexicon", {})
    if isinstance(lex, list):
        notes.append(f"{rel} 的 lexicon 需要 {{类别: [词…]}} 映射，现在写成了列表"
                     " —— 词汇类提示已关闭")
        out["lexicon"] = {}
    elif not isinstance(lex, dict):
        if lex not in (None, "", []):
            notes.append(f"{rel} 的 lexicon 需要 {{类别: [词…]}} 映射，现在是 "
                         f"{type(lex).__name__} —— 词汇类提示已关闭")
        out["lexicon"] = {}
    else:
        fixed = {}
        for cat, words in lex.items():
            if isinstance(words, str):
                # `AITells` 会 `[str(w) for w in words]` → 拆成单字 → `_scan_words`
                # 按 len>=2 全丢 → 这一类**永远零命中**，看起来却像「没这个词」。
                notes.append(f"{rel} 的 lexicon.{cat} 是字符串（会被拆成单字而全部失效）"
                             " —— 本类已忽略，要写词请写成列表")
                continue
            if not isinstance(words, list):
                notes.append(f"{rel} 的 lexicon.{cat} 需要字符串列表，现在是 "
                             f"{type(words).__name__} —— 本类已忽略")
                continue
            keep = [w for w in words if isinstance(w, str) and len(w) >= 2]
            if len(keep) != len(words):
                notes.append(f"{rel} 的 lexicon.{cat} 里有非字符串或单字词（{[w for w in words if w not in keep][:3]}）"
                             " —— 这些词已丢掉，剩下的照常生效")
            if keep:
                fixed[cat] = keep
        out["lexicon"] = fixed
    for key in _TELL_TIERS:
        val = out.get(key, [])
        if val in (None, "", []):
            out[key] = []
            continue
        if isinstance(val, str):
            # 「strong: no_specific」这种笔误最阴：`for tid in "no_specific"` 不报错，
            # 只是把每个字母当成一个 tell 名 → 一个都不认识 → **全部 tell 静默关掉**，
            # 而 score 照样 100。字符串在这里唯一说得通的读法就是「漏了列表括号」。
            only = val.strip()
            if only in _tell_names():
                notes.append(f"{rel} 的 {key} 写成了字符串 {only!r}，已按 {key}: [{only}] 收编"
                             " —— 这一档需要的是列表")
                out[key] = [only]
            else:
                notes.append(f"{rel} 的 {key} 是字符串而不是 tell 名列表 —— 这一档已关闭")
                out[key] = []
            continue
        if not isinstance(val, list):
            notes.append(f"{rel} 的 {key} 需要 tell 名列表，现在是 {type(val).__name__}"
                         " —— 这一档已关闭")
            out[key] = []
            continue
        good = []
        for i, item in enumerate(val):
            if isinstance(item, str) and item.strip():
                good.append(item.strip())
            else:
                notes.append(f"{rel} 的 {key}[{i}] 需要 tell 名字符串，现在是 {item!r}"
                             " —— 这一条不生效（写 [名, 说明] 是错的，档位由 strong/weak 决定）")
        out[key] = good
    kept = sum(len(out[k]) for k in _TELL_TIERS if isinstance(out.get(k), list))
    if not kept and not any(out.get("lexicon") or {}):
        notes.append(f"{rel} 一条能用的规则都没有 —— 人味提示词已关闭"
                     "（校验报告里 ai_tells 会落 null 而不是「满分」）")
        return None, notes
    return out, notes


def _heading_slice(text: str, level: int, keyword: str) -> str:
    """取第 `level` 级标题里含 keyword 的那一节，**找不到就返回空串**。

    与 `Pack.slice_heading` 的区别就是这一条：那里是「切不到就退回整份文件」
    （宁可多注入，也不能让模型拿不到知识）；而**按参数值挑一节**时反过来才安全 ——
    风格选了「权威科普」却把另外 4 套语气模板一起塞进去，模型拿到的是互相冲突的指令。
    """
    if not keyword or not text:
        return ""
    head = re.compile(rf"^#{{{level}}}(?!#)\s")
    upper = re.compile(rf"^#{{1,{level}}}\s")
    lines = text.split("\n")
    start = next((i for i, ln in enumerate(lines) if head.match(ln) and keyword in ln), None)
    if start is None:
        return ""
    end = next((j for j in range(start + 1, len(lines)) if upper.match(lines[j])), len(lines))
    body = "\n".join(lines[start + 1:end])
    # 「## 红线速查」下面什么都没有 ≠ 有内容：那种标题给模型的是一个空承诺
    # （P2-7 —— 模板正文还写着「口径见本提示词的【行业红线】段」）。
    # 与「章节被改名」同一种失效形状，所以同样返回空串，让 unfilled / 体检去说。
    return "\n".join([lines[start], body]).strip() if body.strip() else ""


def _heading_lines(text: str, level: int = 2) -> list[str]:
    """文件里所有 `level` 级标题行（原样，含 `## ` 前缀）。"""
    head = re.compile(rf"^#{{{level}}}(?!#)\s")
    return [ln for ln in (text or "").split("\n") if head.match(ln)]


def _pick_heading_line(text: str, key: str, level: int = 2) -> str:
    """挑与 `key` 对应的标题行：先要标题正文逐字相等，再退到包含匹配，都没有 → 空串。

    为什么不能只有包含匹配（P1-1 的根因）：`维保` 是 `维保合同` 的子串，
    按文件顺序取第一个命中就把「维保」的口径换成了合同的口径，
    而两个细分在界面上都是正常选项 —— 包看起来完全健康。
    """
    key = str(key or "").strip()
    if not key:
        return ""
    lines = _heading_lines(text, level)
    norm = lambda s: str(s).lstrip("#").strip().replace(" ", "")  # noqa: E731
    for ln in lines:
        if norm(ln) == norm(key):
            return ln.rstrip()
    for ln in lines:                       # 「## 2. 维保」这类带序号的写法
        if key in ln:
            return ln.rstrip()
    return ""


def split_heading_sections(text: str, keywords, level: int = 2) -> dict:
    """按标题逐字切开：`{keyword: 该节正文}`，命不中或整节为空 → 空字符串。

    与 `_heading_slice` 的差别就是「相等 vs 包含」，见 `_pick_heading_line`。
    正文为空也算空 —— 「有标题但内容删空」与「章节没了」对注入结果是同一件事（P2-7）。
    """
    out: dict[str, str] = {}
    for kw in (keywords or ()):
        line = _pick_heading_line(text, str(kw), level)
        out[str(kw)] = _heading_body_of(text, line, level) if line else ""
    return out


def _norm_name(s) -> str:
    """名称归一：去 `1. ` 这类序号前缀与空白、统一小写，用于逐字比对。"""
    t = str(s or "").strip()
    return re.sub(r"^\d+[.、)\]]\s*", "", t).replace(" ", "").lower()


def name_matches(items, option: str, key=lambda it: it) -> tuple[object | None, list]:
    """在 `items`（章节标题 / 模型给的条目名）里为 `option` 找一个**没有歧义**的匹配。

    返回 `(命中项 | None, 判为歧义时的全部竞争者)`。

    规则（由严到宽，宁缺不滥）：
      1. 归一后**逐字相等** —— 唯一才认，出现两个同名标题就是包自己写坏了，谁也不给；
      2. 否则取**互为包含**的候选，「最优」= 标题与选项的**字数对称差最小**
         （`维保` vs `维保与责任` 差 3 字、vs `维保合同` 差 2 字 → 合同更近）；
         对称差并列 → **两边都不匹配**，把竞争者原样交给调用方去报；
      3. 都没有 → `(None, [])`。

    为什么不能按列表顺序取第一个包含关系（P1-1 / P1-2 的根因）：
    `segments=["维保合同","维保"]` 时「维保」会先撞上「维保合同」，真正的维保知识
    对每个选项都不可达，而包看起来完全健康；`["别墅","别墅电梯"]` 配
    `["别墅电梯加装","独栋别墅"]` 时，按顺序取第一个会把「别墅电梯」的口径
    挂到「独栋别墅」那一节。**错的口径比空的更危险** —— 空会被体检报出来，
    错的会一路进模型。歧义时返回竞争者，就是为了让它变成看得见的一条说明。
    """
    want = _norm_name(option)
    if not want:
        return None, []
    named = [(it, _norm_name(key(it))) for it in items]
    exact = [it for it, norm in named if norm and norm == want]
    if len(exact) == 1:
        return exact[0], []
    if len(exact) > 1:
        return None, list(exact)
    subs = [(it, norm) for it, norm in named
            if norm and (want in norm or norm in want)]
    if not subs:
        return None, []
    best = min(abs(len(norm) - len(want)) for _it, norm in subs)
    top = [it for it, norm in subs if abs(len(norm) - len(want)) == best]
    return (top[0], []) if len(top) == 1 else (None, top)


def _section_has_body(pack_dir: Path, rel: str, keyword: str, level: int = 2) -> bool:
    """`rel` 里是否有一节标题含 `keyword` **且正文非空**（P2-7 的判据）。

    以前 `param_audit` 问的是「标题在不在」，于是「有标题、内容删空」被当成健康 ——
    模型收到的是一段光秃秃的标题。对注入来说这两种情况是同一件事。
    """
    try:
        text = read_text_cached(pack_dir / rel)
    except OSError:
        return False
    return bool(_heading_body_of(text, _pick_heading_line(text, keyword, level), level))


def _heading_body_of(text: str, heading_line: str, level: int = 2) -> str:
    """取 `heading_line`（一整行标题）那一节的正文，含标题行本身。

    只有标题、正文为空 → 返回空串：与 `_heading_slice` 同一判据（P2-7）。
    「这一节存在」不等于「这一节有知识」，空标题发给模型只是一个空承诺。
    """
    if not heading_line:
        return ""
    lines = text.split("\n")
    start = next((i for i, ln in enumerate(lines) if ln.rstrip() == heading_line), None)
    if start is None:
        return ""
    upper = re.compile(rf"^#{{1,{level}}}(?!#)\s")
    end = next((j for j in range(start + 1, len(lines)) if upper.match(lines[j])), len(lines))
    body = "\n".join(lines[start + 1:end])
    return "\n".join([lines[start], body]).strip() if body.strip() else ""


def _heading_exists(pack_dir: Path, rel: str, keyword: str, level: int = 2) -> bool:
    """知识文件里是否存在 `level` 级标题含 keyword 的章节（默认 `##`）。

    hooks.md 的风格模板是 `### 风格名`（三级），audience/topics 的章节是 `##`（二级），
    所以 level 由调用方按文件层级传，不能写死。
    """
    if not keyword:
        return False
    text = read_text_cached(pack_dir / rel)
    return any(re.match(rf"^#{{{level}}}(?!#)\s", ln) and keyword in ln
               for ln in text.split("\n"))


# ── 改写范围三档（A-2）────────────────────────────────────────
#
# 来源：`需求方案-去AI味与热点情报.md` §2.9 A-2 —— 对标 MrGeDiao/shuorenhua 的
# structural / bounded / in-place 三档。**语义本来就已经在代码里**：
# `pipeline._violation_feedback` 的尾巴一直写着「不要另起一炉重写、不要改动了
# 未点名的段落」，那正是 `bounded` 档。A-2 做的事是把它**升格成显式参数**，
# 让三档各自有名字、能被配置、能被断言 —— 而不是把一句话藏在提示词尾巴里。
#
# 为什么默认 `bounded`：`in-place` 更保守但可能改不动（事实错的稿子必须删句），
# `structural` 放开了重排、改起来最有效但最容易"矫枉过正"（把好句子一起重写）。
# `bounded` 是文档 §2.9 A-2 与第三部分待确认 #6 定的默认档。
REWRITE_SCOPES: tuple[str, ...] = ("in-place", "bounded", "structural")
DEFAULT_REWRITE_SCOPE = "bounded"


def rewrite_scope_error(value) -> str:
    """`rewrite_scope` 的取值检查：返回人话错误，空串 = 合法或没配。

    与 `checker.tolerance_error` 同一套处理（都在 `pack_info` 里加载期判死）：
    `pack.yaml` 里把枚举值写错（`inplace` / `in_place` / `InPlace`）的后果是
    **静默走回默认档** —— 包作者以为配了"只换词不删句"，实际拿到的是 bounded，
    回炉照样敢删句。配置写错改变了行为却看不出来，正是本项目最忌讳的那一类。
    """
    if value is None or value == "":
        return ""
    if not isinstance(value, str):
        return (f"pack.yaml 的 rewrite_scope 必须是字符串（{'/'.join(REWRITE_SCOPES)}），"
                f"实际是 {type(value).__name__}：{value!r}")
    if value not in REWRITE_SCOPES:
        return (f"pack.yaml 的 rewrite_scope={value!r} 不是合法档位"
                f"（只能是 {'/'.join(REWRITE_SCOPES)}）—— 写错会静默退回默认档 "
                f"{DEFAULT_REWRITE_SCOPE}，回炉行为与你配的不是一回事")
    return ""


def param_audit(pack_dir: Path, data: dict) -> dict[str, dict[str, str]]:
    """列出「用户能选、但本包没给对应定制」的参数值 → 一句人话降级说明。

    为什么需要
    ----------
    这些值不会报错，只会静默走通用默认：用户加了个「快手」以为照常查平台红线，
    实际平台差异化校验整个不生效；加了新细分领域，注入的却是别的章节。
    静默降级比报错更危险 —— 与 `checker.Banwords.dropped_short` 同一取向
    （那里注释写的是「保留可见性，避免包作者以为写了就生效」）。

    只列**有问题**的值；`persona` 只进提示词、无表可查，故不参与审计。

    返回 `{参数键: {选项值: 说明}}`。
    """
    params = data.get("params", {}) or {}
    out: dict[str, dict[str, str]] = {}

    def note(key: str, value, text: str) -> None:
        out.setdefault(key, {})[str(value)] = text

    def options(key: str) -> list:
        return (params.get(key, {}) or {}).get("options", []) or []

    def as_keys(table: dict) -> set[str]:
        return {str(k) for k in (table or {})}

    # 受众：audience_map 缺该值、或映射到的章节不存在/正文为空 → 切片为空、**不注入**
    # （P1-22：曾退回整份 2167 字，里面 5 个别的受众章节会把话术带偏）。这里把它摊出来。
    amap = data.get("audience_map", {}) or {}
    for v in options("audience"):
        kw = amap.get(str(v))
        if not kw:
            note("audience", v, "未配 audience_map：本次不注入受众知识（切片为空）")
        elif not _section_has_body(pack_dir, "knowledge/audience.md", str(kw)):
            note("audience", v,
                 f"映射的章节「{kw}」在 audience.md 中不存在或正文为空：本次不注入受众知识"
                 "（切片为空，不会退回整份 —— 请改映射或补内容）")

    # 细分领域：topics_map 无该值 → **不注入**（P2-4）。旧文案说「将改用「通用」章节」，
    # 那是当时 `topics_slice` 真会退回整份；现在不退了，文案跟着改准 ——
    # 说谎的降级说明比没有说明更坏。
    tmap = data.get("topics_map", {}) or {}
    for v in options("segment"):
        kw = tmap.get(str(v))
        if not kw:
            note("segment", v,
                 "topics_map 没有这个取值：本次不注入该细分领域的选题知识"
                 "（切片为空，不退回整份 —— 整份里是别的细分领域，会把选题带偏）")
        elif not _section_has_body(pack_dir, "knowledge/topics.md", str(kw)):
            note("segment", v,
                 f"映射的章节「{kw}」在 topics.md 中不存在或正文为空：本次不注入选题知识"
                 "（切片为空，不退回整份 —— 请改映射或补内容）")

    # 文风 tell 词表（P2-5）：结构写坏只关闭提示词，不拦生成 —— 但它必须被看见，
    # 否则「这次没测人味」与「测了、很好」在界面上是同一个样子。
    _tells, tell_notes = resolve_ai_tells(pack_dir, data)
    for n in tell_notes:
        note("ai_tells", str(data.get("ai_tells", "ai_tells.yaml")), n)

    # 情报源（A/B 线交接处，`需求方案 §2.2`）：未知 id / label 重复 / 结构写坏
    # 记一条 note，**不报错、不拦生成** —— 情报是锦上添花，不是生成的前提。
    # 但必须可见：包作者写了 `id: xhs_board` 以为接上了，实际引擎没这个适配器，
    # 表现是"这个源永远 0 条"，与"今天没货"长得一模一样。
    _sources, src_notes = parse_sources(data.get("intel_sources", []))
    for n in src_notes:
        note("intel_sources", "intel_sources", n)

    # 时长：quota_table 缺该键 → 配额按相邻键插值；points_by_duration 缺 → 默认 3
    quota, points = as_keys(data.get("quota_table", {})), as_keys(
        data.get("points_by_duration", {}))
    # P1-26：表里写了非数字（如 `total: "约290"`）→ 该键在 Quota.target() 被静默
    # 丢掉 → 渲染成「总计≈ 字」。解析失败影响**所有**时长档位（target() 整体降级），
    # 所以每个 duration 选项都要带上具体是哪张表哪一项。
    qerrs = quota_table_errors(data.get("quota_table", {}))
    for v in options("duration"):
        missing = []
        if qerrs:
            missing.append("quota_table 解析失败：" + "；".join(qerrs))
        elif str(v) not in quota:
            # ⚠ 这三条是**三条不同的降级路径**，说成同一句会把人引到错的方向：
            #   整表缺失 → 按 时长×语速 估一个通用值，补一档没用，得把表建起来；
            #   只有一档 → 任何时长都取这一档（180 秒拿到的是 60 秒的配额）；
            #   有表缺该档 → 按相邻档位插值，补上这一档就对了。
            # 修复前这里一律写「按相邻时长插值」：包作者照这句话去补相邻档位，
            # 补完仍然是降级 —— 审计本身在误导。
            if not quota:
                missing.append("整个 quota_table 缺失：字数配额按 时长×语速 估算")
            elif len(quota) == 1:
                only = next(iter(quota))
                missing.append(f"quota_table 只有 {only} 秒一档："
                               "任何时长都取这一档，配额不随时长变化")
            else:
                missing.append("字数配额按相邻时长插值")
        if str(v) not in points:
            missing.append("正文要点数用默认 3")
        if missing:
            note("duration", v, "；".join(missing))

    # 风格：rate_by_style 缺该键 → 语速默认 4.5 字/秒；hooks.md 没写该风格 →
    # 钩子库切片退回整份（宁多勿缺，但该告诉包作者——实测退整份 2062 字）。
    rates = as_keys(data.get("rate_by_style", {}))
    hooks_rel = "patterns/hooks.md"
    hooks_text = read_text_cached(pack_dir / hooks_rel)
    for v in options("style"):
        missing = []
        if str(v) not in rates:
            missing.append("rate_by_style 无该风格：语速按默认 4.5 字/秒，"
                           "字数配额随之变化")
        if hooks_text and not _heading_exists(pack_dir, hooks_rel, str(v), level=3):
            missing.append(f"钩子库没写「{v}」风格模板：本次退回整份钩子库"
                           f"（{len(hooks_text)} 字）")
        if missing:
            note("style", v, "；".join(missing))

    # 平台：banwords 的平台分级词表缺该键 → 只走基础词表
    bw_rel = str(data.get("banwords", "banwords.yaml"))
    ban, bw_err = read_yaml_cached(pack_dir / bw_rel)
    # P1-25/P5-3：词表结构写错（标量 hard / platform.*.extra_soft 不读）也要摊到
    # 平台选项上 —— 结构错 = 整个词表不可信（对应生成路径的 ValueError / pack_error）。
    bw_fatal, bw_advisory = validate_banwords(ban, bw_rel)
    rules = as_keys((ban or {}).get("platform", {}) or {})
    for v in options("platform"):
        if bw_err:
            # 词表整个读不出来时，「缺某个平台」已经是最小的问题了 ——
            # 要说清楚是「整个词表都失效」，否则用户会以为只有这一个平台没配。
            note("platform", v, f"{bw_err}：整个禁用词表都失效，平台红线校验不生效")
        elif bw_fatal:
            note("platform", v, "；".join(bw_fatal)
                 + "：整个禁用词表都失效，平台红线校验不生效")
        elif str(v) not in rules:
            note("platform", v, "平台分级词表未定义该平台：只按通用词表校验，"
                                "平台差异化红线不生效")
        else:
            soft = [a for a in bw_advisory if f"platform.{v}." in a]
            if soft:
                note("platform", v, "；".join(soft))

    return out


class Pack:
    """一个行业包。引擎只认 pack.yaml 的结构，不认识任何具体行业。

    **与 `pack_info` 的分工**：`pack_info` 供列表展示，读坏也返回（带
    `pack_error`）；`Pack` 供生成使用，读坏直接抛 `PackBrokenError`。

    **两层检查，各守各的**（不是重复机制，别删其中一层）：

    1. 构造期（这里）—— **快速失败**。坏包不该被接受进队列：
       `banwords.yaml` 是在写稿+回炉阶段才读的，等到那时才发现，
       `select` 阶段那一次模型调用已经付过费了。
    2. 出口（`banwords_data` / `skill` / `private_facts`）—— **不变式**。
       构造期检查只代表「那一刻是好的」；一个作业要跑几十秒，
       期间包被改坏完全可能，出口必须保证「不交出降级结果」。
    """

    def __init__(self, root: Path, name: str):
        self.root = root
        self.name_arg = name
        # ⚠ 名字必须先校验再用 —— `root / "packs" / name` 里塞 `../../..`
        # 会解析到 packs **之外**，于是任何放得下 pack.yaml 的目录都能被当成
        # 行业包加载，它的 `private/*.yaml` 会被当私有资料注入提示词。
        # 而 `pack.yaml` 存在与否的差值会反映成 404/409，等于一个
        # 「这个路径上有没有 pack.yaml」的探测 oracle。
        #
        # 两层各守各的（与上面「构造期 / 出口」同一口径，不是重复机制）：
        #   · 端点上的 `_safe_name` 负责给出 **400 名称不合法**（web 层的语义）；
        #   · 这里负责**不变式** —— 任何调用方（generate / rewrite / packgen /
        #     导出，以及未来新增的）都不可能拿到 packs/ 之外的目录。
        # 只修端点的话，下一个忘了调用的入口就又漏了。
        packs_root = (root / "packs").resolve()
        if not name or "/" in name or "\\" in name or ".." in name:
            raise PackError(f"行业包名称不合法：{name!r}")
        self.dir = root / "packs" / name
        if packs_root not in self.dir.resolve().parents:
            # 名字里没有分隔符也可能翻出去（符号链接、Windows 短名、大小写）
            raise PackError(f"行业包名称不合法（解析到 packs 之外）：{name!r}")
        if not (self.dir / "pack.yaml").exists():
            raise PackError(f"行业包不存在：{name}")
        self.data, _ = read_yaml_cached(self.dir / "pack.yaml")
        self.info = pack_info(self.dir)      # 内部再读一次（缓存命中，不额外 IO）
        # pack_error 覆盖 pack.yaml 与它引用的 banwords.yaml。
        # 不抛的后果：params 空 → 参数条全空；name 退成目录 slug；
        # param_audit 变 {}（它要审计的数据就是这份 pack.yaml）；
        # 词表空 → 合规校验全过。生成照样跑得完，只是产出与这个行业无关。
        if self.info.pack_error:
            raise PackBrokenError(f"行业包「{name}」的 {self.info.pack_error}")
        # P1-3 加载期体检的产出（`_audit` 填），设置页与作业都读它。
        # `warnings` 与 `pack_error` 的分工照旧：error 阻断加载，warning 只说明。
        self.warnings: list[str] = []
        self._ai_tells_cache: tuple | None = None   # None=未解析（见 `_resolve_ai_tells`）
        # P3-13：pack.yaml 的选项列表不去重 → 同名两节、第二节的规则永远不可达，
        # 全程无警告。选项名是参数的取值域，重复就是包自己写坏了。
        params = self.data.get("params", {}) or {}
        for key in params:
            opts = (params.get(key) or {}).get("options", [])
            if not isinstance(opts, list):
                continue
            seen: set[str] = set()
            for o in opts:
                one = str(o.get("name", "") if isinstance(o, dict) else o).strip()
                if one and one in seen:
                    raise PackBrokenError(
                        f"行业包「{name}」的 params.{key}.options 里选项「{one}」重复出现："
                        "重复的取值在界面上同名，第二份的规则永远命中不到")
                seen.add(one)
        # P1-3：注入回来是空的，运行期也要看得见 —— `Pack` 是每条注入路径的必经之地，
        # 体检因此跟着加载走（模板包与设置页都在加载它）。
        self._audit()

    # ── 基础 ────────────────────────────────────────────────
    @property
    def name(self) -> str:
        return str(self.data.get("name", self.dir.name))

    @property
    def draft(self) -> bool:
        return bool(self.data.get("draft", False))

    def param_default(self, key: str, fallback=None):
        p = (self.data.get("params", {}) or {}).get(key, {}) or {}
        return p.get("default", fallback)

    def param_options(self, key: str) -> list:
        p = (self.data.get("params", {}) or {}).get(key, {}) or {}
        return p.get("options", []) or []

    def file_text(self, rel: str) -> str:
        return read_text_cached(self.dir / rel)

    # ── 加载期体检（P1-3）────────────────────────────────────
    def stage_files_map(self) -> dict[str, str]:
        """各阶段 `stages.*.files` 的并集：{占位符名: 路径[#章节]}（首个声明者为准）。"""
        out: dict[str, str] = {}
        skill = self.skill() or {}
        for cfg in (skill.get("stages", {}) or {}).values():
            for key, spec in ((cfg or {}).get("files") or {}).items():
                out.setdefault(str(key), str(spec))
        return out

    def _audit(self) -> None:
        """把「注入回来是空的」这类降级收进 `warnings`：加载期一次，只读缓存。

        为什么挂在 `Pack` 上：它是每条注入路径的必经之地，模板包与设置页都在加载它，
        所以警告不依赖作业有没有跑过。为什么只收 warning 而不抛：结构坏
        （`PackError` / `PackBrokenError`）已经在构造时处理掉了，这里再抛会让
        设置页的包列表整页变红。

        探针实测的空注入是**完全静默**的：把 `## 核心术语` 改名后 `$terms` 是空串，
        提示词留着一个光秃秃的小标题，作业里没有 `tpl_*`、`pack_error=''`、报 done。
        运行期那一半由 `PromptRenderer.unfilled` 补上（同一份名单，同一套判据）。
        """
        notes: list[str] = []
        for key, spec in sorted(self.stage_files_map().items()):
            rel, _, section = str(spec).partition("#")
            rel = rel.strip()
            if not self.file_text(rel).strip():
                notes.append(f"{rel} 读不到内容：${key} 注入为空")
                continue
            if section.strip() and not self.file_slice(spec).strip():
                notes.append(f"{rel}#{section.strip()} 切片为空：${key} 注入为空"
                             "（多半是那节被改名或删空了；`#章节` 找不到时不退回整份）")
        for rel, key, kind, getter, marker in (
                ("knowledge/topics.md", "segment", "细分", self.topics_slice, "$topics_slice"),
                ("knowledge/audience.md", "audience", "受众", self.audience_slice,
                 "$audience_slice")):
            text = self.file_text(rel)
            titles = [ln.lstrip("#").strip() for ln in _heading_lines(text)]
            for option in self.param_options(key):
                if getter(str(option)).strip():
                    continue
                # 把竞争的标题一起点名：只说「切片为空」，作者还得自己去猜是哪两个
                # 名字打架了（P1-2 要求的「naming the option and the competing headings」）。
                _hit, rivals = name_matches(titles, str(option))
                why = (f"与这些章节歧义：{'、'.join(str(r) for r in rivals)}" if rivals
                       else f"{rel} 里没有对应章节")
                notes.append(f"{kind}「{option}」{marker} 注入为空（{why}；"
                             "不退回整份，越界取值会把选题带偏）")
        _, tells_notes = self._resolve_ai_tells()
        notes.extend(tells_notes)
        self.warnings = list(dict.fromkeys(notes))

    # ── 知识切片 ────────────────────────────────────────────
    def slice_heading(self, rel: str, keyword: str) -> str:
        """取 `##` 标题里含 keyword 的那一节全文；找不到则返回整个文件。

        ⚠ 这里是**子串**匹配且取第一个命中：「维保」会命中「## 维保合同」。
        所以细分/受众切片**不走这条路**（见 `topics_slice` / `_heading_body`），
        `audience_map` 里也没人靠它区分两个相近的标题。
        留着的用处只有 `hooks_slice` 那类「一个关键词只可能落一处」的场合。
        """
        text = self.file_text(rel)
        if not text:
            return ""
        if not keyword:
            return text
        lines = text.split("\n")
        want = f"## {keyword}"
        start = None
        for i, line in enumerate(lines):
            if line.strip() == want:                      # 先认「标题就是这个词」
                start = i
                break
        if start is None:
            for i, line in enumerate(lines):
                if re.match(r"^##\s", line) and keyword in line:
                    start = i
                    break
        if start is None:
            return text
        end = len(lines)
        for j in range(start + 1, len(lines)):
            if re.match(r"^##\s", lines[j]):
                end = j
                break
        return "\n".join(lines[start:end])

    def _heading_body(self, text: str, heading_line: str) -> str:
        """按**整行标题**取那一节（含标题行本身）；`heading_line` 空 → 空串。"""
        return _heading_body_of(text, heading_line)

    def topics_slice(self, segment: str | None) -> str:
        """按细分领域取 `knowledge/topics.md` 的那一节。

        **命中不了就是空串，绝不退回整份文件。** 与 `audience_slice`（P1-22）同口径：
        整份 topics.md 装着**别的一些细分**（实测 805 字符、还含 `pack.yaml` 这个字样），
        注入进来不是「信息多一点」，而是把模型的注意力拉去讲另一个细分。
        旧写法那句 `target = key or mapping.get("通用", "")` 在生成包里没有落点
        —— 生成包根本没有 `## 通用` 节，于是每个越界取值都等价于「整份灌进去」。
        `segment` 在 `app/schemas.py` 里不校验取值域，越界是常态而不是意外。
        降级本身由 `param_audit` 与 `_audit` 标出，不靠这里兜底。
        """
        mapping = self.data.get("topics_map", {}) or {}
        key = mapping.get(segment or "", "")
        if not key:
            return ""
        text = self.file_text("knowledge/topics.md")
        # 逐字相等优先：子串匹配会把「维保」送到「维保合同」那一节去，
        # 于是真正的「维保」知识对每个选项都不可达，而包看起来完全健康（P1-1）。
        # 只有 `## 2. 维保` 这种带序号的写法才落到包含匹配（本项目两种都有）。
        return self._heading_body(text, _pick_heading_line(text, key))

    def audience_section_body(self, audience: str) -> str:
        """按**小节标题逐字相等**取 audience.md 的那一节（不走 audience_map）。

        与 `audience_slice` 的区别：后者查 pack.yaml 自己声明的映射（生成包与手写包
        都靠它），这里给建包期的对账用 —— 需要「选项 X 到底命中了哪一节」这个事实
        本身，而不是又一层映射。命中不了 → 空串（与 `audience_slice` 同口径）。
        """
        text = self.file_text("knowledge/audience.md")
        return self._heading_body(text, _pick_heading_line(text, str(audience or "")))

    def topics_section_body(self, segment: str) -> str:
        """按**章节标题**取 topics.md 的那一节；映射里没有这个细分 → 空串。"""
        key = (self.data.get("topics_map", {}) or {}).get(segment or "", "")
        if not key:
            return ""
        text = self.file_text("knowledge/topics.md")
        return self._heading_body(text, _pick_heading_line(text, key))

    def heading_bodies(self, rel: str, keywords) -> dict:
        """把文件按**标题逐字相等**切成 `{keyword: 正文}`（建包期对账用）。

        为什么需要它：`slice_heading` 是子串匹配 + 取第一个命中，`通用` 之类的
        短词会把相邻章节一起吞进来；建包期的体检要逐节判断「这一节到底有没有内容」，
        就必须按标题逐字切开，不能靠猜。
        """
        text = self.file_text(rel)
        return split_heading_sections(text, keywords)

    def audience_slice(self, audience: str | None) -> str:
        mapping = self.data.get("audience_map", {}) or {}
        key = mapping.get(audience or "", "")
        if not key:
            # P1-22：未知受众**不注入整份文件** —— 整份含 5 个别的受众章节，
            # 会把模型注意力拉向"物业/开发商"话术（实测 2167 字符整份注入 vs
            # 正常切片 181~392）。param_audit 已在设置页标出「未配 audience_map」
            # 的降级，这里把伤害降到零：宁缺毋滥，比误导强。
            return ""
        return self.slice_heading("knowledge/audience.md", key)

    def file_slice(self, spec: str) -> str:
        """按 `stages.<阶段>.files` 的取值取文件：`路径` 或 `路径#章节关键词`。

        带 `#` 时只取那一节：整份 `standards.md` 2195 字，撰写真正要用的只是
        「核心术语」那十几行表格；把法规编号清单一起塞进每一轮回炉，
        挤掉的是模型对正文的注意力（P2-25 信噪比）。

        **章节找不到时返回空串**，与 `slice_heading` 的「退回整份」相反，这是故意的：
        写了 `#章节` 就是作者明确说「只要这一节」，这时把整份塞回去恰好是他不想要的
        （新建包的 standards.md 只有一张待核实清单，整份注入等于往每轮里灌编号清单）。
        代价是「章节被改名 → 知识静默消失」可能看不出来，所以这条由
        `tests/test_prompt_templates.py` 的声明对账测试兜着，不靠运行期日志。
        """
        rel, _, kw = str(spec).partition("#")
        if not kw.strip():
            return self.file_text(rel.strip())
        text = self.file_text(rel.strip())
        # 二级（`## 五、核心术语`）与三级（`### 核心术语`）都算命中：
        # 包作者手写的知识文件层级不统一，不该因此让一节知识静默消失。
        for level in (2, 3):
            body = _heading_slice(text, level, kw.strip())
            if body:
                return body
        return ""

    def hooks_slice(self, style: str | None) -> str:
        """钩子库按风格切片：一份文件里 5 套语气模板，每轮只用得上当前这套。

        保留「钩子库」整节（类型表 + 禁用清单 + 平台匹配）+ 所选风格那一节。
        砍掉的两块都是重复：「风格×受众交叉建议」在风格已由用户选定时用不上，
        「口语化硬性检查」与 `skill.yaml` 的 write.system 逐条同义（P2-25）。
        """
        rel = "patterns/hooks.md"
        text = self.file_text(rel)
        if not text:
            return ""
        lib = _heading_slice(text, 2, "钩子库")
        block = _heading_slice(text, 3, style or "")
        if not lib or not block:
            # 该包不按「## 一、钩子库」+「### 风格名」的结构写，或风格没在
            # pack.yaml 里配（param_audit 已把后者标成降级）：退回整份，宁多勿缺。
            return text
        return (lib.rstrip()
                + f"\n\n## 风格语气模板（本次风格：{style}）\n" + block.strip() + "\n")

    # ── 配额与词表 ──────────────────────────────────────────
    def rate_for_style(self, style: str | None) -> float:
        """该风格的字/秒。包没配、配了非数、或配了 ≤0 时，一律退回兜底语速。

        **下限必须收在这个出口**，而不是散在各个调用点：

        - `checker.estimate_seconds` 自己处理了 `r <= 0`（退回 `DEFAULT_RATE`）；
        - `pipeline._compute_timings` 直接算 `n / rate`，**没有**任何保护。

        修复前 `float(rates.get(style, 4.5))` 会把包里的 0 原样返回，于是
        「校验说没问题、组装时间轴时 `ZeroDivisionError: float division by zero`」——
        同一份包配置，两处结论不同。而 `rate_by_style: {快节奏: 0}` 是很容易
        发生的写法（想表达「很快」却写成了 0），失败点还在**烧完 token 之后**的
        组装阶段，用户完全看不出根因。

        两处都 import 同一个 `DEFAULT_RATE`，口径因此不会再次分叉。
        """
        rates = self.data.get("rate_by_style", {}) or {}
        try:
            v = float(rates.get(style or "", DEFAULT_RATE))
        except (TypeError, ValueError):
            return DEFAULT_RATE
        return v if v > 0 else DEFAULT_RATE

    def points_limit(self, duration: float) -> int:
        table = self.data.get("points_by_duration", {}) or {}
        try:
            return int(table.get(str(int(duration)), table.get(int(duration), 3)))
        except (TypeError, ValueError):
            return 3

    def duration_tolerance(self) -> float | None:
        """时长偏差容差（百分比），`pack.yaml` 的 `duration_tolerance_pct`（P2-27 的后半）。

        None = 本包不覆盖，走 `checker.check_script` 的「时长自适应」口径
        （`±max(10%, 3秒÷目标时长)`，15 秒档因此放宽到 ±20%）。
        取值合法性由 `pack_info` 在**加载时**判死（`tolerance_error`），
        所以这里只兜"包绕过列表直接来取"的情况：坏值一律退回 None，
        而不是带着一个能把每次生成都判死的数字去校验。
        """
        v = self.data.get("duration_tolerance_pct")
        if tolerance_error(v):
            return None
        return float(v) if v not in (None, "") else None

    def rewrite_scope(self) -> str | None:
        """本包配的回炉改写范围（`pack.yaml` 的 `rewrite_scope`），没配返回 None。

        None 的含义是"本包不覆盖"，由 `pipeline._normalize` 落到
        `DEFAULT_REWRITE_SCOPE`。取值合法性由 `pack_info` 在**加载时**判死
        （`rewrite_scope_error`），这里再判一次只是为了兜"绕过列表直接来取"的路径：
        坏值一律退回 None（走默认档），而不是带着一个不认识的档位去拼提示词 ——
        拼进去的结果是 `_SCOPE_INSTRUCTION[...]` KeyError，作业失败在回炉那一步，
        而用户看到的是一句与包配置无关的异常。
        """
        v = self.data.get("rewrite_scope")
        if rewrite_scope_error(v):
            return None
        return str(v) if v not in (None, "") else None

    def intel_sources(self) -> list:
        """本包声明的情报源（`pack.yaml` 的 `intel_sources`）。缺了就是空列表。

        与 `pack_info` 走**同一个** `parse_sources`（一本账）：设置页/`/api/meta`
        看到的那份与抓取时真正跑的那份必须完全一致 —— 两处各解析一次迟早会漂，
        而漂法是"界面说有 5 个源、实际只跑了 3 个"。
        """
        return parse_sources(self.data.get("intel_sources", []))[0]

    def intel_seeds(self) -> list[str]:
        """需求词种子：**细分领域的选项**（下拉词按领域扩散，§1.2 实测的有效做法）。

        取 `params.segment.options` 而不是另开一个配置项：细分领域本来就是
        "这个行业的人会怎么问"，正是雷达要的种子。
        """
        return [str(x) for x in self.param_options("segment")]

    def intel_keywords(self) -> list[str]:
        """命中过滤词（热榜只做关键词命中，§1.2：泛热榜对垂直行业覆盖接近零）。

        `params.segment.options` + `display_name` + `topics_map` 的映射词 ——
        这三样合起来就是"这个行业的词"，不需要包作者再维护一份。
        """
        out = [str(x) for x in self.param_options("segment")]
        if self.info.display_name:
            out.append(str(self.info.display_name))
        out += [str(v) for v in (self.data.get("topics_map", {}) or {}).values() if v]
        seen, uniq = set(), []
        for w in out:
            if w and w not in seen:
                seen.add(w)
                uniq.append(w)
        return uniq

    def ai_tells_data(self) -> dict | None:
        """文风 tell 词表（`pack.yaml` 的 `ai_tells` 键指定文件名）；结构坏了返回 None。

        与 `banwords_data` 的两处关键差别，都是故意的：

        1. **没配就是不开**，返回 None 而不是空表。空表会让 `AITells.report()`
           打出「人味 100 分」—— 把"根本没测"读成"很好"，比缺一个字段更坏。
        2. **读坏 / 写坏也不抛**（词表那边必须抛，因为空表等于合规校验一条都查不出；
           这边空 = 提示词关闭，危害小得多）。为什么不能抛：调用方是
           `pipeline._load_tells()` → `checker.check_script(tells=...)`，它在 select
           **之后**，而 select 是真金白银 —— 实测这里抛 PackError 会让花钱跑完的作业
           停在 failed，用户什么产物都拿不到。关闭 + 一条 warning 是唯一允许的降级。

        结构与缓存都在 `resolve_ai_tells`（与 `pack_info` 同一本账）。
        """
        return self._resolve_ai_tells()[0]

    def _resolve_ai_tells(self) -> tuple[dict | None, list[str]]:
        """(词表或 None, 说明列表)。"""
        if self._ai_tells_cache is None:
            self._ai_tells_cache = resolve_ai_tells(self.dir, self.data)
        return self._ai_tells_cache

    def banwords_data(self) -> dict:
        """行业包禁用词表（`pack.yaml` 的 `banwords` 键指定文件名）。

        **读坏必须抛，绝不能退回空表。** 空表的含义是「这个包没有禁用词」，
        与「词表读不出来」在结果上完全同形 —— 都是 `scan()` 一条也查不出。
        实测：同一段文案在词表正常时命中 4 处硬伤，词表写坏后命中 0 处，
        而校验报告的形态一模一样。用户看到的是「校验通过」。

        文件**不存在**仍然返回空表（这是正常的「没配词表」），
        与「存在但读不出来」由 `read_yaml_cached` 的第二个返回值区分。
        """
        rel = self.data.get("banwords", "banwords.yaml")
        data, err = read_yaml_cached(self.dir / rel)
        if err:
            raise PackBrokenError(
                f"行业包「{self.name_arg}」的 {err} —— 禁用词表读不出来，"
                "合规校验会退化成「一条都查不出」，已中止本次生成")
        return data

    def skill(self) -> dict | None:
        """包的生成技能定义（skill.yaml）：各阶段提示词、注入文件、回炉上限。

        读坏同样抛 —— 退回 `None` 会让提示词**静默**换成内置默认，
        产出的脚本不再符合本行业口径；而且调用方拿到 `None` 会报
        「行业包缺少 skill.yaml」，与「文件写坏了」是两回事，会把人带偏。
        """
        data, err = read_yaml_cached(self.dir / "skill.yaml")
        if err:
            raise PackBrokenError(f"行业包「{self.name_arg}」的 {err}")
        return data or None

    # ── 私有资料（只注入非空条目）────────────────────────────
    def private_facts(self) -> str:
        rels = (self.data.get("files", {}) or {}).get("private", []) or []
        blocks = []
        for rel in rels:
            data, err = read_yaml_cached(self.dir / rel)
            if err:
                # 用户自己填的产品事实。读坏了不注入，脚本里就会写模型编的内容，
                # 而用户从产物上看不出「我的资料压根没进去」。
                # 消息里带**相对路径**（`private/products.yaml`），
                # 只给文件名的话，包作者还得自己去猜是哪一个。
                raise PackBrokenError(
                    f"行业包「{self.name_arg}」的私有资料读不出来：{rel} —— {err}。"
                    "该文件不会被注入，产物会缺这部分事实，已中止本次生成")
            if not data:
                continue
            slim = strip_empty(data)
            if slim:
                # 段名用**去扩展名的文件基名**而不是 `=== private/service.yaml ===`：
                # 模型没有文件系统，一个像路径的东西就是在让它去找不存在的文件
                # （`tests/test_dangling_refs.py` 现在把 `*.md`/`*.yaml` 路径一律判为悬空引用）。
                # 来源信息保留（它决定"这段是私有资料、优先当事实来源"），路径形态去掉。
                stem = rel.replace("\\", "/").rsplit("/", 1)[-1].split(".")[0]
                blocks.append(f"=== 私有资料 · {stem} ===\n"
                              + yaml.safe_dump(slim, allow_unicode=True, sort_keys=False))
        return "\n".join(blocks)


# 溯源字段：包作者写了这两个键，就是在声明"这条有出处、我核过"。
PROVENANCE_KEYS = ("source", "verified")
UNVERIFIED_KEY = "_未核实"
UNVERIFIED_NOTE = ("这条没有出处（source/verified 为空）：只能写成 "
                   "{{待补：…}} 占位，不许当既成事实播报，也不要替它编一个来源")

# P0-17 ② 条目级示例判定（2026-09-23 补）：包作者在条目里夹「示例：」字符串时，
# 整条照旧以【私有知识库（优先作为事实来源）】注入 —— 模型把模板占位台量当交付业绩
# 念出去，就是这条（真实产物里查到了"去年那12台"）。
# 原来的 `strip_empty` 只在**叶子字符串**上判"示例："，一条里只要还有别的非空值
# （哪怕只是 `units: 12` 这种示例数字），整条就漏进来。修法：
#   - 显式标记 `_example: true` 才是"这是示例条目"的权威说法（不靠猜字符串）……
#   - ……但旧包（含模板）都用"示例："开头，所以**两者都认**：任一命中即整条剔除。
# 保留"剥掉示例叶子"（示例值不在字面量里出现，本来就是假数据），但"条目级"才是
# 真正的账。2026-09-23 实测量：`{"name": "示例：X", "units": 12}` 修复前整条注入。
_EXAMPLE_KEYS = ("_example", "example")

def _is_example_entry(d: dict) -> bool:
    """dict 是否显式示例（`_example: true` / `example: 任意真值`）或任一值是
    以「示例：」开头的字符串（旧模板的口径）。整条命中即视为占位示例。"""
    if any(d.get(k) for k in _EXAMPLE_KEYS):
        return True
    return any(isinstance(v, str) and v.startswith("示例：")
               for v in d.values())


def strip_empty(data):
    """递归剔除空值/示例占位（value 含"示例："的条目视为未填）。

    另外补一件原来漏掉的事（P0-17 修 ③）：**条目声明了 `source`/`verified`
    却没填值**时，就地打一个 `_未核实` 标记。这批资料是以
    【私有知识库（优先作为事实来源）】的标题注入的，模型看到的就是"可信事实"；
    清空示例数字只解决了"仓库里不许有假数字"，解决不了
    "用户填了真数字但没写出处" —— 那条同样会被当成既成事实念出去。
    只有出处键、别的全空的条目不打标记（否则会凭空多出一条内容）。

    P0-17 ②：条目级示例判定。叶子判"示例："挡不住
    `{"name": "示例：X", "units": 12}` —— 只要有别的非空值整条就漏进来。
    所以 dict 先按整条判示例（显式 `_example: true` 或任一值以「示例：」开头），
    命中即整条剔除；没命中的照旧剥空值叶子。
    """
    if isinstance(data, dict):
        if _is_example_entry(data):
            return None
        out = {}
        for k, v in data.items():
            v2 = strip_empty(v)
            if v2 not in (None, "", [], {}):
                out[k] = v2
        declared = [k for k in PROVENANCE_KEYS if k in data]
        if declared and out and not any(k in out for k in declared):
            out[UNVERIFIED_KEY] = UNVERIFIED_NOTE
        return out
    if isinstance(data, list):
        out = []
        for item in data:
            v2 = strip_empty(item)
            if v2 not in (None, "", [], {}):
                out.append(v2)
        return out
    if isinstance(data, str):
        if "示例：" in data:
            return None
        return data
    return data
