# -*- coding: utf-8 -*-
"""行业包生成器：输入行业名+业务描述，按模板包同 schema 生成初版行业包。

草稿保护：
  - 生成包 pack.yaml 标 draft: true，界面显示"草稿·需人工校对"角标
  - 广告法/平台通用词表直接复用**模板包 packs/_template**（不含任何行业事实），
    仅追加行业增补词；模板缺失时直接抛 `TemplateMissingError`，不再回退到某个行业包
  - 标准/法规编号一律不生成，只写"待核实清单"（防编造监管依据）

三处修复：
  1. **空数组兜底**：`out.segments[0]` / `out.audiences[0]` / `out.personas[0]`
     原来没有保护，模型返回空数组就是 IndexError → 500，用户只看到「服务器内部错误」。
  2. **失败清理**：原来「目录已存在」的守卫在最前面，但中途任何一步抛错都会留下
     半成品目录，重试时被守卫挡成 409「行业包已存在」，用户只能去手工删目录。
  3. **原子写**：生成的知识文件原来用裸 `write_text`，而项目其它地方
     （config / result / pack.yaml）都走了 `write_atomic`，口径不一致。
"""
from __future__ import annotations

import logging
import re
import shutil
import threading
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from .fileio import rmtree_resilient, write_atomic
from .jobs import JobCancelled
from .knowledge import Pack, name_matches
from .llm import LLMClient

# P2-46：同名 slug 并发建包的 TOCTOU —— `d.exists()` 检查与写入分两段，
# 两个请求可双双通过、后者原子覆盖前者（双份 token、前者产物被静默替换）。
# 现在用这张表 + 锁把"静默替换"关掉了：第二个会拿到 FileExistsError 而不是覆盖。
# ⚠ **但没有关掉"双份 token"**：锁是在模型调用**之后**才取的（见 create_pack），
# 所以两个同名请求还是会各烧一次调用，只是其中一个最后判失败。
# 同步 409 只覆盖"目录已存在"那一种（预检在 Pipeline.start_packgen 里）；
# 跨进程/多引擎实例同样挡不住（这是进程内的一张表）。
# 用「正在创建」集合做 slug 级互斥。
_creating_lock = threading.Lock()
# slug -> **这一次占位的凭证**（键是大小写折叠过的，见 `_table_key`）。
# 原来"归还"无从判断还得掉的是谁的：同一条建包有三个归还点（目录已存在的早退、
# `start_packgen` 的兜底、`_run_packgen` 的 finally），
# 而 CPython 3.14 的 `Thread.start()` 是"先起线程、再 `_started.wait()`"——
# 工作线程跑完之后 `start()` 自己被打断是完全可能的，两次归还之间挤进别人的
# 同名占位，第二次 discard 就把那个**活着的**占位偷走了（第 16 轮复核实测：
# 第三条请求因此能与第一条并行建同一个目录 = 双份 token + 目录互踩）。
# 现在归还必须交出凭证，凭证对不上就是空操作。
_creating: dict[str, str] = {}
_claim_seq = 0
NO_CLAIM = "no-claim"     # 空 slug：本次不占位（正式判定留给 create_pack 里那句）
ANY_OWNER = "any"         # 显式"不管是谁，摘掉" —— 只给测试的兜底清理用
_MISSING = object()       # "这条根本不在表里"与"在表里但凭证是 None"的区分哨兵


def _new_token() -> str:
    """发一个占位凭证。只在 `_creating_lock` 内调用，所以不需要额外的原子性。"""
    global _claim_seq
    _claim_seq += 1
    return f"c{_claim_seq}"

log = logging.getLogger(__name__)

# 建包时从模板包**原样复制**的文件（不走模型）。
# 这些文件在新包里会被**每轮注入**（$hooks / $growth / $voice_block），
# 所以模板包必须与任何具体行业无关 —— 出现别行业的事实就是 P0 回归
# （tests/test_template_pack_neutral.py 钉住这件事）。
GENERIC_FILES = [
    "skill.yaml",
    "patterns/hooks.md", "patterns/growth.md",
    "knowledge/voice.md", "patterns/anti-ai-smell.md",
    "rules/duration.md", "rules/output-template.md",
    "compliance/ad-law.md", "compliance/platform.md",
]
# 文风 tell 词表：不注入提示词，只被 `app/ai_tells.py` 读来做校验。
# 不带给新包 = 新包的人味检测**静默关闭**（`Pack.ai_tells_data()` 找不到文件返回 None
# → 报告里 `ai_tells: null`，界面上什么迹象都没有），所以它和 banwords 一样算骨架件。
STYLE_FILES = ["ai_tells.yaml"]

PRIVATE_TEMPLATES = [
    "private/README.md", "private/products.yaml", "private/service.yaml",
    "private/cases.yaml", "private/faq.yaml", "private/raw/README.md",
]

# 模板包必须自带的文件。缺一个就意味着新包缺一份骨架：
# 复制循环是 `if src.exists()`（静默跳过），所以这里必须先验一遍，
# 否则"包少了 voice.md"这种事要等到用户第一次生成才看得出来。
REQUIRED_IN_TEMPLATE = (GENERIC_FILES + PRIVATE_TEMPLATES + STYLE_FILES
                       + ["pack.yaml", "banwords.yaml"])

# 模板包：**只有** packs/_template 一个来源。
# 原来它找不到 `_template` 就静默回退到电梯包（`FALLBACK_TEMPLATE_PACK`），
# 于是"给宠物医院建的包"里带着《电梯口播特有的取舍》和填好的电梯异议话术 ——
# 这两份每轮都注入。回退这条路已经删掉：缺模板就抛，让用户去把模板补回来。
TEMPLATE_PACK = "_template"


class TemplateMissingError(RuntimeError):
    """模板包缺失或不完整 —— 建包功能不可用，且**没有安全的退路**。"""


def template_pack(root: Path) -> Pack:
    """取模板包（`packs/_template`）。缺失或骨架文件不齐就直接抛。

    这里不抛的代价是**静默产出带别行业事实的包**：建包流程照样"成功"、
    作业照样 done、用户照样能选到新包 —— 要等到某次生成的稿子里冒出
    别的行业的装置名，才会发现模板三年前就不在了。
    """
    d = root / "packs" / TEMPLATE_PACK
    if not (d / "pack.yaml").exists():
        raise TemplateMissingError(
            f"模板包缺失：{d.relative_to(root)}（没有它就不能建新行业包，"
            "否则会产出带着别的行业事实的包）。请恢复 packs/_template/ 后重试。")
    missing = [rel for rel in REQUIRED_IN_TEMPLATE if not (d / rel).exists()]
    if missing:
        raise TemplateMissingError(
            f"模板包 {TEMPLATE_PACK} 缺少骨架文件：{'、'.join(missing)}"
            " —— 补齐后再建包，缺的文件不会进新包（复制时静默跳过）。")
    return Pack(root, TEMPLATE_PACK)


PACKGEN_SYSTEM = """你是行业知识包编辑，为口播脚本智能体制作新行业的知识包初稿。
铁律：
1. 只输出一个 JSON 对象，不要任何多余文字
2. 绝不编造标准编号、法规文号、具体数据——需要核实的一律放进 verify_list
3. 行业红线宁严勿松，拿不准的不写
4. 内容必须具体可用（写"怎么选、怎么看、常见坑"），不写正确的废话
5. segments / audiences / personas 都必须非空（至少 2 项），这是硬性要求"""


class TopicBlock(BaseModel):
    heading: str
    core: str
    myths: list[dict] = []       # [{myth, fact}]
    placeholders: list[str] = []


class AudienceBlock(BaseModel):
    name: str
    fears: list[str] = []
    questions: list[str] = []
    cta: str = ""


class PackGenOut(BaseModel):
    display_name: str
    segments: list[str] = Field(description="细分领域 5~8 个")
    audiences: list[str] = Field(description="受众 3~5 类")
    personas: list[str] = Field(description="人设 3~4 个")
    topics: list[TopicBlock]
    audience_details: list[AudienceBlock]
    ideas: list[str] = Field(description="可直接用的选题 ≥30 条")
    redlines: list[str] = Field(description="行业红线 5~8 条")
    banwords_extra_hard: list[str] = []
    banwords_extra_soft: list[str] = []
    verify_list: list[str] = Field(description="需人工核实的标准/政策清单")


def slugify(industry: str) -> str:
    """目录名：保留中文与字母数字，其余（`/`、空格、全角符号…）折成 `-`。

    **一个可用字符都没有时返回空串**，不再回落到 `"custom"` —— 那会让
    「？？？」与「！！！」这类输入共用同一个目录名，第二个永远建不出来，
    而报错说的是「行业包已存在」（实测过的形态，主报告 §15）。
    判空由调用方负责：入口 `pipeline.start_packgen` 在花钱之前就拒掉。
    """
    return re.sub(r"[^\w一-鿿]+", "-", industry).strip("-")



def preview_slug(industry: str) -> str:
    """作业入口用的目录名预演：动手之前就能回答「这个行业包已经存在」。

    与 `create_pack` 同一套 slugify 口径（P2-51：目录名跟**用户输入**走）。
    返回空串 = 这个名字起不出目录（纯符号），调用方必须就地拒掉，
    **不许**退到模型给的 display_name 上（那正是 P2-51 的原始缺陷形态）。
    """
    return slugify(industry.strip())


# Windows 保留设备名（不分大小写、带扩展名也保留）：这类目录名 mkdir 会直接失败，
# 而失败点在 `_materialize` —— 那时候模型的钱已经花完了（第 18 轮复核 P3-4）。
_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)} | {f"LPT{i}" for i in range(1, 10)})
# NTFS 单个路径段上限 255 个 UTF-16 码元；留一点余量（中文一个字就是 1 个码元，
# 但拼上父目录与 generated/ 之类还要走整条路径）。
_MAX_SLUG_UNITS = 120


def _utf16_units(s: str) -> int:
    return sum(2 if ord(c) > 0xFFFF else 1 for c in s)


def slug_problem(slug: str) -> str:
    """目录名能不能真建出来。返回人话原因，空串 = 没问题。

    判据放在花钱**之前**跑：`start_packgen` 里 `preview_slug` 之后、`claim_slug` 之前。
    实测过的情形：`CON` / `nul.` / `com1` 这类保留名与 300 字的长名字，
    旧流程会先付一份 token、再在 `_materialize` 的 mkdir 上炸掉，
    用户看到的是"生成失败"，而那两个字段的钱已经花掉了。
    """
    if not slug:
        return ""
    if slug.upper().rstrip(" .") in _RESERVED_NAMES:
        return (f"「{slug}」是 Windows 的保留设备名，做不了目录名 —— "
                "请在行业名里加一点实际文字（如「CON 建材」）")
    if _utf16_units(slug) > _MAX_SLUG_UNITS:
        return (f"行业名太长了（目录名 {_utf16_units(slug)} 个字符，上限 {_MAX_SLUG_UNITS}）—— "
                "请用更短的行业名，创建后可以在包详情里改显示名")
    return ""


def claim_slug(slug: str) -> str | None:
    """P2-46 的后半：**在花钱之前**把目录名占住，返回这一次占位的**凭证**。

    修复前 `_creating` 的占用发生在 `create_pack` 里（唯一那次模型调用之后），
    而入口的同步检查只有 `(packs/slug).exists()` —— 目录还没建出来，于是两条
    同名请求双双通过检查、双双起作业：双份 token，且第二条是以"作业失败"的形态
    告诉用户的（等了一两分钟才知道重名）。现在作业入口先占位，占不到就直接同步 409。

    返回值刻意不是 bool：归还方必须能证明"还的是我自己那一次占位"。
    占不到返回 None；空 slug 返回 `NO_CLAIM`（照旧放行，正式判定在 `create_pack` 里）。
    """
    if not slug:
        return NO_CLAIM                   # 空 slug（符号名）走 create_pack 里的正式判定
    key = _table_key(slug)
    with _creating_lock:
        if key in _creating:
            return None
        token = _new_token()
        _creating[key] = token
        return token


def _table_key(slug: str) -> str:
    """占位表的键：**大小写折叠过的** slug。

    第 18 轮复核实测：原来表按原样作键，而 NTFS/APFS 的目录名不区分大小写 ——
    `Probe Case` 与 `probe case` 各拿到一份凭证（c1/c2）、两条作业双双起、双双进模型，
    盘上却只有一个目录（实测：写盘 2 次、目录 1 个、两条作业都报 done）。
    P2-46 那句"花钱之前就把同名挡住"对大小写变体完全失效。折叠后与文件系统同域。
    """
    return slug.casefold()


def release_slug(slug: str, owner: str) -> bool:
    """凭 `owner` 归还。凭证对不上（已被摘走、或已被别人重新占上）就是空操作。

    旧实现是 `_creating.discard(slug)`：一条作业的第二个归还点会把**别人**的占位
    摘掉（第 16 轮复核量到的 P1）。传 `ANY_OWNER` 才是"不管是谁都摘"，
    今天只有测试的兜底清理用它。
    """
    with _creating_lock:
        if not slug:
            # 空 slug 从来不进表，所以正常路径这里是空操作；但兜底清理必须真能清得掉
            # （第 18 轮复核 P3-1：旧写法把早退放在 pop 之前，`""` 一旦进去谁也删不掉）。
            if owner == ANY_OWNER:
                return _creating.pop("", None) is not None
            return True
        key = _table_key(slug)
        if owner == ANY_OWNER:
            return _creating.pop(key, None) is not None
        # ⚠ 必须先确认"这条在表里"，再比凭证：`_creating.get(key)` 对**不存在**的键
        #   返回 None，于是 `owner=None` 会"匹配"上一个根本不存在的占位，
        #   紧接着的 `del` 直接 KeyError（第 18 轮复核复量 P1-2 时当场撞出来的形态）。
        held = _creating.get(key, _MISSING)
        if held is _MISSING or held != owner:
            return False
        del _creating[key]
        return True


def create_pack(root: Path, llm: LLMClient, industry: str, description: str, *,
                on_retry=None, on_delta=None, on_attempt=None, should_abort=None,
                deadline: float | None = None,
                slug_preclaimed: bool = False) -> dict:
    """按用户的行业名 + 一句话描述生成一个行业包初稿（草稿态）。

    P1-43 起它跑在后台作业里，下面几个回调就是作业的三件套：
    - `on_retry(note, attempt, total)` / `on_delta(kind, text)` / `on_attempt()`
      原样透传给 `chat_json`，界面因此能看到重试与思考流（以前是一个哑的长请求）；
      `on_attempt` 是 llm 侧的「每次 HTTP 尝试前清零流式缓冲」回调（P2-13），
      `pipeline._run_packgen` 一直在传，这里漏收就是 `TypeError` → 建包作业必失败。
    - `should_abort()` 在**唯一那次模型调用返回之后、写盘之前**检查 ——
      用户点了取消，就不该再往 `packs/` 里落一个没人要的目录
      （token 已经花掉，收不回来；目录至少可以不落）。
    """
    base = template_pack(root)
    user = (
        f"【任务】为口播脚本智能体生成「{industry}」行业的知识包初稿。\n"
        f"【业务描述】{description}\n"
        """【参照结构】模板包包含：细分领域、受众、人设、每个细分的知识点+常见误区表+
需占位事实、每类受众的深层恐惧+高频疑问+推荐CTA、可直接使用的选题库、行业红线、
行业增补禁用词、待核实清单。

【输出 JSON 字段】
- display_name: 行业显示名
- segments / audiences / personas: 数组（都必须非空，至少 2 项）
- topics: [{"heading": "...", "core": "核心知识点2~4句", "myths": [{"myth": "...", "fact": "..."}], "placeholders": ["需用户提供的事实"]}]
  —— topics 覆盖全部 segments，heading 与 segments 一致
- audience_details: [{"name": "与 audiences 一致", "fears": ["深层恐惧"], "questions": ["高频疑问"], "cta": "..."}]
- ideas: ≥30 条可直接用的选题。**每条一行，格式固定**：
  `标题 — <audiences 里的一个受众> · <钩子类型>`，
  钩子类型只能从这 10 类里选：反常识/场景代入/悬念提问/损失厌恶/身份背书/数据冲击/对比反差/直接痛点/故事开场/误解纠正
  （选题阶段要求 hook_type 从中选，写"清单钩子""权威科普"这类名字模型会照着自造）
- redlines: 5~8 条行业红线（广告法之外的行业特有雷区）
- banwords_extra_hard / banwords_extra_soft: 行业特有禁用词增补
- verify_list: 需人工核实的标准/法规/政策清单（只写名称，不写编号）"""
    )

    out: PackGenOut = llm.chat_json("packgen", PACKGEN_SYSTEM, user, PackGenOut,
                                    on_retry=on_retry, on_delta=on_delta,
                                    on_attempt=on_attempt, should_abort=should_abort,
                                    deadline=deadline)

    # display_name 会被写进 pack.yaml、校对清单，以及导出的 SKILL.md ——
    # frontmatter 之后那是**正文**，模型在这里换行就能凭空造出一节 `---` / `# 标题`
    # （批次 10 复核实测：`\n---\nname: evil` 原样进了正文，而自检只看 fence）。
    # 行业显示名本来就是一行文字的事，先折行再往下走。
    out.display_name = " ".join(str(out.display_name or "").split()) or industry.strip()

    # 空数组兜底：模型偶尔会返回 []，直接下标访问会 IndexError → 500
    def _dedupe(vals, field):
        """选项列表去重（P3-13）：重复项不是"多一个选项"，而是**第二份永远命中不到**。

        `params.<field>.options` 是下拉框的取值域，`topics_map` / `audience_map`
        按名字查 —— 两个同名选项只能指向同一节。这里保留第一份并**在报告里说**，
        而不是静默生成两份 `## N. 维保` 让第二份不可达。
        """
        out, seen, dup = [], set(), []
        for v in vals:
            key = str(v).strip()
            if key in seen:
                dup.append(key)
                continue
            seen.add(key)
            out.append(v)
        return out, dup

    segments, seg_dupes = _dedupe([s for s in (out.segments or []) if str(s).strip()], "segment")
    audiences, aud_dupes = _dedupe([a for a in (out.audiences or []) if str(a).strip()], "audience")
    personas, per_dupes = _dedupe([p for p in (out.personas or []) if str(p).strip()], "persona")
    dupes = [f"选项「{k}」在 {f} 里重复出现，已合并成一个（重复项在界面上同名，"
             f"第二份的规则永远命中不到）" for f, vals in
             (("segments", seg_dupes), ("audiences", aud_dupes), ("personas", per_dupes)) for k in vals]
    if not segments or not audiences or not personas:
        raise ValueError(
            "模型返回的行业结构不完整（细分领域/受众/人设存在空项）。"
            "请补充描述后重试，或换一个模型。")

    # P2-51：目录名必须跟**用户输入**走（slugify 保留中文、把 / 等转成 -）。
    # 修复前取模型返回的 display_name —— 模型自由发挥时（实测 mock 返回
    # 「全屋定制/装修」），用户输「门窗定制」却得到目录「全屋定制-装修」，
    # 找不到自己刚建的包，重试还必撞「行业包已存在」。
    slug = slugify(industry.strip())
    if not slug:
        # 走到这里说明调用方没在花钱之前拦（`start_packgen` 会拦）。
        # 宁可现在报错，也不把目录名交给模型的 display_name（P2-51）。
        raise ValueError("行业名称里没有任何可用作目录名的字符，请换成含文字或数字的名称")
    bad = slug_problem(slug)
    if bad:
        # 同一形态在 `start_packgen` 就该拦住；这里兜住**直接**调用 create_pack 的人，
        # 免得白写出一个永远建不完整的目录（钱已经在那一次模型调用里花掉了）。
        raise ValueError(bad)
    if should_abort and should_abort():
        # 检查点放在这里：上面那次模型调用是全部开销所在，往下就该建目录了。
        raise JobCancelled("已取消")
    # 占位与归还只有一本账：`claim_slug` 发凭证、`release_slug` 认凭证。
    # 作业入口（`pipeline.start_packgen`）已经用 `claim_slug` 在**花钱之前**占好了名字，
    # 这里就不能再占一次（否则自己跟自己撞），也不能在结束时释放别人的占位。
    owns_claim = not slug_preclaimed
    claim_token = None
    if owns_claim:
        claim_token = claim_slug(slug)
        if claim_token is None:
            raise FileExistsError(f"行业包正在创建中：{slug}")
    try:
        d = root / "packs" / slug
        if d.exists():
            raise FileExistsError(f"行业包已存在：{slug}")

        created_here = False
        try:
            # 独占创建。`d.exists()` 那道检查只证明"检查那一刻"目录不在：
            # 两个引擎进程、或同一台机器上的大小写变体（NTFS 不分大小写，见 `_table_key`），
            # 都可能在我们检查之后把同名目录建出来。第 18 轮复核量到：`4bb65cb` 把回收圈
            # 扩到最后一次写盘之后，"失败时顺手 rmtree"就会把**别人建好的那个包**删掉 ——
            # 受害的那条作业还在报 done。所以现在先证明这目录是我建的，才允许回收。
            try:
                d.mkdir()
            except FileExistsError:
                raise FileExistsError(f"行业包已存在：{slug}") from None
            created_here = True

            match_notes = _materialize(d, base, out, slug, industry, description,
                                       segments, audiences, personas)
            checklist = _checklist(out, slug)
            # P1-44：生成完必须体检 —— 模型输出的包能不能用，不能等用户第一次
            # 生成才发现（生成时已付过费）。加载 Pack(slug)（结构坏 → 抛）+
            # param_audit 全量（切片/配额/词表降级逐项列出），体检结果并进校对清单。
            # 再加两份**只有建包期知道**的事实（P1-2 / P3-13）：
            #   match_notes  哪个选项没配上章节、被哪些同名/近名标题挤掉
            #   dupes        模型把同一个选项写了两遍（合并后的那一份才有内容）
            audit_notes = _pack_audit(root, slug) + match_notes + dupes
            if audit_notes:
                checklist += ("\n\n## 引擎体检发现（生成时自动检测，逐项核实后重跑或用前确认）\n"
                              + "\n".join(f"- [ ] {t}" for t in audit_notes))
            write_atomic(d / "校对清单.md", checklist)
        except Exception:
            # 从"我们独占建出了这个目录"到"最后一次写盘"整段都在这里的保护圈内，
            # 而回收网只在 `created_here` 为真时才动手（第 18 轮复核 P1-1）。
            # （第 16 轮复核：修复前 try 只裹住 `_materialize`，而 `校对清单.md`
            #  是它**外面**的最后一次写盘 —— 那一步炸掉（磁盘满 / Windows 杀软
            #  正扫着刚建的 23 个文件）会留下一个 `Pack` 能加载、会出现在包列表里
            #  的包，作业却记为失败；重试同名永远 409，而应用里没有删包的入口。）
            # 上面 `if d.exists(): raise` 特意留在这个 try 之外：那是"别人的目录"，
            # 回收网不许碰它。
            #
            # 用 rmtree_resilient 而不是 `ignore_errors=True`：后者会把「没删掉」
            # 当成成功，于是半成品目录留在那儿，下次建包照样被 409 挡住，
            # 而我们已经把成功当成既定事实，连日志都不会有 —— 用户看到的现象
            # 永远是「重试一直失败」，却查不出为什么。
            if created_here and not rmtree_resilient(d):
                log.warning("半成品目录未能清除，下次建包同名行业会被挡：%s", d)
            raise

        # 产物摘要随返回下发：结果页据此渲染「生成了什么」（细分/受众/人设/选题），
        # 而不是只给一份待核实的校对清单 —— 用户此前「不知道生成了啥」。
        # ⚠ 只回短字符串列表，不回 topics 全文（body 里没有消费方）。
        # ⚠ 别在这里加"顺手算的"字段：`topic_count` 就没人消费（P3-11 已删）。
        return {"name": slug, "display_name": out.display_name,
                "dir": str(d), "draft": True,
                "checklist": checklist,
                "verify_list": out.verify_list,
                "segments": [str(x) for x in segments],
                "audiences": [str(x) for x in audiences],
                "personas": [str(x) for x in personas],
                "ideas": [str(x) for x in (out.ideas or [])],
                "redlines": [str(x) for x in (out.redlines or [])],
                "banwords_extra_hard": [str(x) for x in (out.banwords_extra_hard or [])],
                "banwords_extra_soft": [str(x) for x in (out.banwords_extra_soft or [])]}
    finally:
        if owns_claim:            # 入口预占的那份由 `pipeline._run_packgen` 释放
            release_slug(slug, claim_token)


def _pack_audit(root: Path, slug: str) -> list[str]:
    """生成包的自体检（P1-44）：加载 + 切片对账 + param_audit 全量，返回人话说明列表。

    返回空列表 = 体检通过。模型输出的包常有「segment 与 topics 章节标题对不上」
    「风格没配语速」「平台没配词表」这类静默降级 —— param_audit 把它们逐项列出；
    结构坏（YAML 解析不了等）则 Pack 直接抛，捕捉后给一句指向性的说明。

    **切片对账**是这里补上的一格：`路径#章节` 找不到章节时返回空串（不退回整份），
    而仓库里的对账测试只跑 `list_packs()` 看到的包 —— 生成的草稿包根本不在其中。
    于是"红线/术语/选题库注入不到东西"这种失效在生成时完全无声（P0-18 的同一形态）。
    """
    from .knowledge import Pack, param_audit
    notes: list[str] = []
    try:
        pk = Pack(root, slug)
    except Exception as e:                       # noqa: BLE001
        return [f"行业包加载失败：{e} —— 请检查生成的文件结构"]
    skill = pk.skill() or {}
    for stage, cfg in (skill.get("stages", {}) or {}).items():
        for key, spec in ((cfg or {}).get("files") or {}).items():
            spec = str(spec)
            if "#" not in spec:
                continue
            path = spec.partition("#")[0].strip()
            if not pk.file_text(path).strip():
                notes.append(f"{stage}.{key}：文件读不到 → {path}")
            elif not pk.file_slice(spec).strip():
                notes.append(f"{stage}.{key}：章节切片为空 → {spec}"
                             "（该知识一个字都不会注入；`#章节` 找不到时不退回整份）")
    for key, mapping in (param_audit(pk.dir, pk.data) or {}).items():
        for value, text in mapping.items():
            notes.append(f"参数「{key}={value}」：{text}")
    notes.extend(_placeholder_audit(root, pk))
    return notes


def _placeholder_audit(root: Path, pk) -> list[str]:
    """模板占位符对账（P1-44 缺的第三条腿）。

    `safe_substitute` 对认不出的 `$foo` **原样保留**：模型收到一段字面量，
    而本该从那格里注入的知识一个字都没有 —— 生成期只在作业日志里留一条
    `tpl_*`，建包这一步什么都没说。拼错的占位符因此可以静默活到第一次付费生成。

    这里刻意**复用生产路径**（`Pipeline._normalize` + `PromptRenderer.*_ctx` +
    `unfilled`）而不是另抄一份"引擎提供哪些占位符"的表：那张表就是第二本账，
    引擎加了新占位符而这里没同步，就会把合法的占位符报成错的（本项目反复出过
    这类"守卫本身说谎"的问题）。对账自身跑不起来时（缺默认参数等），
    如实说明"没跑成"而不是静默通过。
    """
    notes: list[str] = []
    try:
        from .config import load_config
        from .pipeline import Pipeline
        from .prompts import PromptRenderer

        cfg = load_config(root)
        cfg.mock = True
        pl = Pipeline(root, cfg)
        p = pl._normalize(pk, {"topic": "（新建行业包自检用主题）"})
        pr = PromptRenderer(pk)
        plan = {"angle": "a", "hook_type": "h", "hook_line": "l",
                "points": ["1"], "cta": ""}
        contexts = {
            "select": pr.select_ctx(p),
            "write": pr.write_ctx(p, plan, ""),
            "storyboard": pr.storyboard_ctx(
                [{"type": "point", "text": "自检段落"}],
                [{"start": 0.0, "end": 1.0}]),
            "rewrite_segment": pr.rewrite_ctx(
                [{"type": "point", "text": "自检段落"}], 0,
                p["quota"].get("body", 100), ""),
        }
        for stage, ctx in contexts.items():
            if not (pk.skill() or {}).get("stages", {}).get(stage):
                continue
            missing = pr.unfilled(stage, ctx)
            if missing:
                notes.append(f"{stage} 模板有引擎不提供的占位符：{'、'.join(missing)}"
                             "（会被原样发给模型，对应知识不会注入）")
    except Exception as e:                       # noqa: BLE001
        notes.append(f"占位符对账没跑成：{type(e).__name__}: {e}"
                     "—— 不影响建包，但请人工核对 skill.yaml 的 $占位符")
    return notes



def _match_by_name(items, used: set, option: str, attr: str,
                   notes: list[str] | None = None, kind: str = "细分领域"):
    """在模型给的条目里为某个选项值找出**没有歧义**的那一条（就地标记已用）。

    匹配规则在 `knowledge.name_matches`（逐字相等 → 包含匹配取对称差最小者 →
    并列即放弃），这里只加两件建包期才有的事：

    - `used`：一条内容不能被两个选项抢走（剩下的那条又没人认领）；
    - `notes`：没配上时把**原因**写下来（被哪些近名标题挤掉了 / 模型根本没给），
      由 `create_pack` 并进校对清单 —— 空骨架 + 「待补」是结果，说明是账。

    旧实现按 `items` 的**列表顺序**取第一个包含关系：`segments=["别墅","别墅电梯"]`
    配模型标题 `["别墅电梯加装","独栋别墅"]` 时，「别墅」先撞上「别墅电梯加装」，
    于是别墅电梯的知识挂到了别墅名下、`## 2. 别墅电梯` 只剩空骨架 —— 错的口径
    静默进模型，比什么都没有更坏（P1-2）。
    """
    pool = [(i, it) for i, it in enumerate(items) if i not in used]
    hit, rivals = name_matches([it for _i, it in pool], option, key=lambda it: getattr(it, attr, "") or "")
    if hit is None:
        if notes is not None:
            if rivals:
                notes.append(f"{kind}「{option}」没配上模型给的段落：这几个标题与它同样接近（"
                             f"{'、'.join(str(getattr(r, attr, '')) for r in rivals)}）—— "
                             "无法判定该给谁，已留空骨架，请改选项名或模型给的标题")
            else:
                # 文档承诺过"模型根本没给"这一种也要说；只报歧义的话，
                # 缺内容这种最常见的形态反而没有账。
                notes.append(f"{kind}「{option}」在模型返回的内容里没有对应段落 —— "
                             "已留空骨架（章节在、内容待补），别让这个细分领域裸奔")
        return None
    used.add(next(i for i, it in pool if it is hit))
    return hit


def _materialize(d: Path, base: Pack, out: PackGenOut, slug: str, industry: str,
                 description: str, segments: list[str], audiences: list[str],
                 personas: list[str]) -> list[str]:
    """把模型输出落成包文件；返回**没配上章节的选项**说明（并进校对清单）。"""
    for sub in ("knowledge", "compliance", "patterns", "rules", "private/raw"):
        (d / sub).mkdir(parents=True, exist_ok=True)

    # 1. 通用文件直接复用模板包（广告法/平台规则/钩子/增长/时长/输出模板）
    for rel in GENERIC_FILES:
        src = base.dir / rel
        if src.exists():
            (d / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, d / rel)
    # 2. 词表类骨架（人味 tell 集）
    for rel in STYLE_FILES:
        src = base.dir / rel
        if src.exists():
            shutil.copyfile(src, d / rel)
    # 3. 私有资料模板
    for rel in PRIVATE_TEMPLATES:
        src = base.dir / rel
        if src.exists():
            (d / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, d / rel)

    # 3. 知识文件（draft 内容）
    #
    # ⚠ 章节标题以**选项表为准**写，不按模型给的 heading 原样落盘。
    # `topics_map` / `audience_map` 是 identity 映射（选项 → 同名章节），
    # 而模型返回的 `topics[].heading`、`audience_details[].name` 未必逐字等于
    # segments / audiences（提示词要求过对齐，但那是"要求"不是"保证"）。
    # 一旦不一致：切片为空 → 这个细分领域的选题知识**一个字都不注入**，
    # 而界面上一切正常（P1-44 点名的裸奔、P0-18 的同一形态在新包里重演）。
    # 现在按选项生成标题，模型那份内容用名称匹配挂上去；对不上就留待补骨架，
    # 章节仍然存在 —— 注入路径是通的，缺的是内容，而缺内容有校对清单指着它。
    topic_lines = ["# 分领域内容知识库（向导生成初稿，draft：需人工校对）", "",
                   "> ⚠️ 本文件由模型生成。core 与 myth/fact 需对照行业实际核实后删除本提示。",
                   "> 章节标题 = pack.yaml 的 segment 选项（注入按标题切片，不要改标题）。", ""]
    topics = list(out.topics or [])
    used = set()
    # 匹配账：哪个选项没配上章节、被哪些近名标题挤掉。由 `create_pack` 并进
    # 校对清单（P1-2：留空骨架是"结果"，这份账是"原因"，两者都要有）。
    match_notes: list[str] = []
    for i, seg in enumerate(segments or [], 1):
        t = _match_by_name(topics, used, seg, "heading",
                           notes=match_notes, kind="细分领域")
        if t is None:
            # 空章节也写出来：切片解析得到内容，param_audit 才分得清
            # "标题不存在"与"标题在、内容待补"这两种完全不同的缺。
            topic_lines += [f"## {i}. {seg}", "", "### 核心知识点",
                            "（模型没给这个细分领域的内容 → 待补）", "",
                            "### 需占位的事实", "{{待补}}", "", "---", ""]
            continue
        topic_lines += [f"## {i}. {seg}", "", "### 核心知识点", t.core, ""]
        if t.myths:
            topic_lines += ["### 常见误区", "| 误区 | 事实 |", "|---|---|"]
            topic_lines += [f"| {m.get('myth', '')} | {m.get('fact', '')} |" for m in t.myths]
            topic_lines.append("")
        if t.placeholders:
            topic_lines += ["### 需占位的事实", "、".join(t.placeholders) + " → `{{待补}}`", ""]
        topic_lines += ["---", ""]
    # 模型多给了没匹配上的 topics（标题写得与选项不同）：不丢，挂在末尾标出来
    extras = [t for j, t in enumerate(topics) if j not in used]
    if extras:
        topic_lines += ["## 附：模型另给的段落（标题与 segment 选项不一致，待人工并回）", ""]
        for t in extras:
            topic_lines += [f"### {t.heading}", t.core, ""]
    write_atomic(d / "knowledge/topics.md", "\n".join(topic_lines))

    aud_lines = ["# 受众痛点库与话术适配（向导生成初稿，draft：需人工校对）", "",
                 "> 章节标题 = pack.yaml 的 audience 选项（注入按标题切片，不要改标题）。", ""]
    details = list(out.audience_details or [])
    used_a = set()
    for a_opt in audiences or []:
        a = _match_by_name(details, used_a, a_opt, "name",
                           notes=match_notes, kind="受众")
        if a is None:
            aud_lines += [f"## {a_opt}", "", "### 深层恐惧（痛点）", "（待补充）", "",
                          "### 高频疑问（选题金矿）", "- （待补充）", "",
                          "### 推荐 CTA：（待补充）", "", "---", ""]
            continue
        aud_lines += [f"## {a_opt}", "", "### 深层恐惧（痛点）",
                      "、".join(a.fears) or "（待补充）", "",
                      "### 高频疑问（选题金矿）"]
        aud_lines += [f"- {q}" for q in a.questions]
        aud_lines += ["", f"### 推荐 CTA：{a.cta or '（待补充）'}", "", "---", ""]
    extras_a = [a for j, a in enumerate(details) if j not in used_a]
    if extras_a:
        aud_lines += ["## 附：模型另给的受众段（名称与 audience 选项不一致，待人工并回）", ""]
        for a in extras_a:
            aud_lines += [f"### {a.name}", "、".join(a.fears), ""]
    write_atomic(d / "knowledge/audience.md", "\n".join(aud_lines))

    idea_lines = ["# 选题库（向导生成初稿，draft：需人工校对）", "",
                  "> 注入契约：只有下面「## 选题库」这一节进选题阶段提示词"
                  "（`skill.yaml` 的 `select.files.ideas`）。",
                  "> 每行的可用取值：**钩子类型**只能取 `patterns/hooks.md` 钩子库的 10 类，"
                  "**受众**只能取 `pack.yaml` 的 audience 选项；不带这两项时按本节默认。",
                  "", "## 选题库", ""]
    idea_lines += [f"{i}. {t}" for i, t in enumerate(out.ideas or [], 1)]
    idea_lines += ["", "## 扩展公式与禁忌（人工参考，不注入）", "",
                   "- 同一主题按不同受众各拆一条；误区=反常识选题；评论区高频疑问=痛点选题",
                   "- 禁忌选题以 `compliance/industry.md` 的「红线速查」为准"]
    write_atomic(d / "knowledge/ideas.md", "\n".join(idea_lines))


    std_lines = ["# 标准、法规与术语库（占位）", "",
                 "> ⚠️ 向导不生成任何标准/法规编号（防编造）。引用前逐条核实后手工补录。",
                 "", "## 待人工核实的清单", ""]
    std_lines += [f"- [ ] {v}" for v in out.verify_list or []]
    # 「核心术语」这一节必须留出来：撰写阶段注入的是 `standards.md#核心术语`
    # （只取这一节，不整份灌编号清单）。作者往别处写术语表 = 引擎一个字都不注入。
    std_lines += ["", "## 核心术语（规范说法 → 口语解释）", "",
                  "> 补在这里：撰写阶段只注入这一节。左列说规范叫法，右列给一句听得懂的大白话。",
                  "", "| 规范术语 | 口语化解释（用于脚本） |", "|---|---|",
                  "| （待补录） | （待补录） |", ""]
    write_atomic(d / "knowledge/standards.md", "\n".join(std_lines))

    # 行业红线：**必须**带「## 红线速查」这一节 —— select 与 write 两个阶段注入的
    # 就是它（`compliance/industry.md#红线速查`）。写成整篇散列表的话，
    # `Pack.file_slice` 找不到章节会返回空串（故意不退回整份），红线就又没了。
    red = ["# 行业红线（向导生成初稿，draft：需人工校对）", "",
           "> ⚠️ 以下红线由模型按行业常识生成，发布相关内容前务必人工核实补全。",
           "> 本节「## 红线速查」会被注入选题与撰写的每一轮；下面的细则只给人和 checker 看。",
           "", "## 红线速查（引擎注入用）", ""]
    red += [f"- {r}" for r in out.redlines or []]
    red += ["", "## 细则（人工补充：本行业的应急处置口径、禁用表述清单、术语规范、敏感话题）", "",
            "- [ ] 安全与效果类承诺的禁用表述与合规替换",
            "- [ ] 紧急/危险场景的唯一标准口径（模型每轮都按它写）",
            "- [ ] 不得演示、不得描述的操作清单",
            "- [ ] 规范术语 → 口语解释（同步进 knowledge/standards.md 的「核心术语」）",
            "- [ ] 敏感话题（纠纷、事故、政策补助）的处理方式", ""]
    write_atomic(d / "compliance/industry.md", "\n".join(red))


    # 4. 词表：复用模板包通用词表 + 行业增补
    bw = dict(base.banwords_data())
    bw.setdefault("hard", [])
    bw.setdefault("soft", [])
    bw["hard"] = sorted(set(bw["hard"]) | set(out.banwords_extra_hard or []))
    bw["soft"] = sorted(set(bw["soft"]) | set(out.banwords_extra_soft or []))
    bw["updated"] = "draft-向导生成"
    write_atomic(d / "banwords.yaml", yaml.safe_dump(bw, allow_unicode=True, sort_keys=False))

    # 5. pack.yaml（identity 映射：segments/audiences 与知识标题一致）
    identity = {x: x for x in segments}
    pack_yaml = {
        "name": slug,
        "display_name": out.display_name,
        "draft": True,
        "version": 1,
        "description": f"{industry}：{description}（向导生成初稿，需人工校对）",
        "params": {
            "segment": {"label": "细分领域", "options": segments, "default": segments[0]},
            "audience": {"label": "受众", "options": audiences, "default": audiences[0]},
            "duration": {"label": "时长（秒）", "options": [15, 30, 60, 90, 180], "default": 60},
            "style": {"label": "风格",
                      "options": ["权威科普", "亲和接地气", "幽默玩梗", "严肃警示", "销售转化"],
                      "default": "亲和接地气"},
            "platform": {"label": "平台", "options": ["抖音", "视频号", "小红书", "B站"], "default": "抖音"},
            "persona": {"label": "人设", "options": personas, "default": personas[0]},
            "cta": {"label": "结尾引导", "options": ["关注", "私信", "留资", "到店", "评论关键词"],
                    "default": "关注"},
        },
        "rate_by_style": base.data.get("rate_by_style") or {
            "权威科普": 4.5, "亲和接地气": 4.5, "幽默玩梗": 5.0,
            "严肃警示": 4.2, "销售转化": 5.0},
        "quota_table": base.data.get("quota_table"),
        "points_by_duration": base.data.get("points_by_duration"),
        "topics_map": identity,
        "audience_map": {x: x for x in audiences},
        # P0-18/19：files.select/write/compliance/facts 是引擎永不消费的孤儿
        # （注入由 skill.yaml 的 stages.<阶段>.files 决定，模板已随 GENERIC_FILES
        # 复制并带 redlines 引用）。这里只留引擎真读的 private。
        "files": {
            "private": ["private/products.yaml", "private/service.yaml",
                        "private/cases.yaml", "private/faq.yaml"],
        },
        "banwords": "banwords.yaml",
    }
    # pack.yaml 是这个包在列表里的身份证：写坏了不是这一个包不可用，
    # list_packs 会连带把整个首页的行业包列表一起带崩，所以必须原子替换。
    write_atomic(d / "pack.yaml", yaml.safe_dump(pack_yaml, allow_unicode=True, sort_keys=False))
    return match_notes


def _checklist(out: PackGenOut, slug: str) -> str:
    lines = ["# 新行业包校对清单", "",
             f"行业：{out.display_name}（目录 packs/{slug}/，**草稿状态**）", "",
             "使用前请逐项核实：", ""]
    lines += [f"- [ ] {v}" for v in out.verify_list or []]
    lines += ["", "## 红线核实", ""]
    lines += [f"- [ ] {r}" for r in out.redlines or []]
    lines += ["", "## 知识核实", "", "- [ ] topics.md 各细分的核心知识点与误区表",
              "- [ ] audience.md 受众痛点与 CTA",
              "- [ ] ideas.md 选题是否符合本行业实际（受众名与钩子类型是否还在取值域内）",
              "- [ ] banwords.yaml 行业增补词是否恰当",
              "- [ ] compliance/industry.md 的「细则」几节补上本行业的应急处置/术语/敏感话题口径",
              "      （只注进提示词的是「## 红线速查」那一节，细则是给 checker 和人看的）",
              "", "## 从模板包复制来的骨架（**不含任何行业内容，需要你填实例**）", "",
              "- [ ] patterns/hooks.md 的钩子实例列（现在是通用问句）",
              "- [ ] patterns/growth.md 的「信任/节奏/CTA」三节（现在是通用取舍）",
              "- [ ] knowledge/voice.md 的口语化范例与人设开场（三处 {{待补}}）",
              "- [ ] private/ 四个 yaml：现在**全是空白**，不填就没有任何私有事实可注入",
              "- [ ] compliance/ad-law.md 的「本行业专用高危区」表",
              "",
              "核实完成后：把 pack.yaml 中 `draft: true` 改为 `false`，角标即消失。"]
    return "\n".join(lines)

