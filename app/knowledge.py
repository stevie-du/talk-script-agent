# -*- coding: utf-8 -*-
"""行业包加载：pack.yaml 解析、知识文件按章节切片、私有资料注入。

性能说明
--------
修复前 `file_text` / `private_facts` 每次调用都读盘 + `yaml.safe_load`，
而一次生成最多要经历 3 轮回炉、每轮注入 6~8 个知识文件 —— 同一个文件被反复
解析十几遍。这里按 (mtime, size) 做进程内缓存：内容一变 mtime 就变，无需手工
失效；写入统一走 `fileio.write_atomic`，所以不会读到写了一半的文件。
"""
from __future__ import annotations

import logging
import re
import threading
from pathlib import Path

import yaml

from .checker import DEFAULT_RATE
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
        _, err = read_yaml_cached(pack_dir / str(data.get("banwords", "banwords.yaml")))
    try:
        return PackInfo(
            name=str(data.get("name", dir_name)),
            display_name=str(data.get("display_name", dir_name)),
            draft=bool(data.get("draft", False)),
            description=str(data.get("description", "")),
            version=int(data.get("version", 1) or 1),
            params=data.get("params", {}) or {},
            param_audit=param_audit(pack_dir, data),
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


def _heading_exists(pack_dir: Path, rel: str, keyword: str) -> bool:
    """知识文件里是否存在标题含 keyword 的 `##` 章节。"""
    if not keyword:
        return False
    text = read_text_cached(pack_dir / rel)
    return any(re.match(r"^##\s", ln) and keyword in ln for ln in text.split("\n"))


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

    # 受众：audience_map 无该值 → audience_slice 退化成注入整份 audience.md
    amap = data.get("audience_map", {}) or {}
    for v in options("audience"):
        kw = amap.get(str(v))
        if not kw:
            note("audience", v, "未配 audience_map：将注入整份受众知识，而非对应章节")
        elif not _heading_exists(pack_dir, "knowledge/audience.md", str(kw)):
            note("audience", v,
                 f"映射的章节「{kw}」在 audience.md 中不存在：将注入整份文件")

    # 细分领域：topics_map 无该值 → 退到「通用」，注入的是别的章节
    tmap = data.get("topics_map", {}) or {}
    for v in options("segment"):
        kw = tmap.get(str(v))
        if not kw:
            g = tmap.get("通用", "")
            note("segment", v,
                 f"未配 topics_map：将改用「通用」章节（{g or '整份文件'}），"
                 "与所选细分领域不匹配")
        elif not _heading_exists(pack_dir, "knowledge/topics.md", str(kw)):
            note("segment", v,
                 f"映射的章节「{kw}」在 topics.md 中不存在：将注入整份文件")

    # 时长：quota_table 缺该键 → 配额按相邻键插值；points_by_duration 缺 → 默认 3
    quota, points = as_keys(data.get("quota_table", {})), as_keys(
        data.get("points_by_duration", {}))
    for v in options("duration"):
        missing = []
        if str(v) not in quota:
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

    # 风格：rate_by_style 缺该键 → 语速默认 4.5 字/秒，字数配额随之变化
    rates = as_keys(data.get("rate_by_style", {}))
    for v in options("style"):
        if str(v) not in rates:
            note("style", v, "rate_by_style 无该风格：语速按默认 4.5 字/秒，"
                             "字数配额随之变化")

    # 平台：banwords 的平台分级词表缺该键 → 只走基础词表
    ban, bw_err = read_yaml_cached(pack_dir / str(data.get("banwords", "banwords.yaml")))
    rules = as_keys((ban or {}).get("platform", {}) or {})
    for v in options("platform"):
        if bw_err:
            # 词表整个读不出来时，「缺某个平台」已经是最小的问题了 ——
            # 要说清楚是「整个词表都失效」，否则用户会以为只有这一个平台没配。
            note("platform", v, f"{bw_err}：整个禁用词表都失效，平台红线校验不生效")
        elif str(v) not in rules:
            note("platform", v, "平台分级词表未定义该平台：只按通用词表校验，"
                                "平台差异化红线不生效")

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

    # ── 知识切片 ────────────────────────────────────────────
    def slice_heading(self, rel: str, keyword: str) -> str:
        """取文件中包含 keyword 的 `##` 章节全文；找不到则返回整个文件。"""
        text = self.file_text(rel)
        if not text:
            return ""
        if not keyword:
            return text
        lines = text.split("\n")
        start = None
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

    def topics_slice(self, segment: str | None) -> str:
        mapping = self.data.get("topics_map", {}) or {}
        key = mapping.get(segment or "", "")
        target = key or mapping.get("通用", "")
        return self.slice_heading("knowledge/topics.md", target)

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
                blocks.append(f"=== {rel} ===\n"
                              + yaml.safe_dump(slim, allow_unicode=True, sort_keys=False))
        return "\n".join(blocks)


def strip_empty(data):
    """递归剔除空值/示例占位（value 含"示例："的条目视为未填）"""
    if isinstance(data, dict):
        out = {}
        for k, v in data.items():
            v2 = strip_empty(v)
            if v2 not in (None, "", [], {}):
                out[k] = v2
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
