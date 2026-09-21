# -*- coding: utf-8 -*-
"""行业包生成器：输入行业名+业务描述，按模板包同 schema 生成初版行业包。

草稿保护：
  - 生成包 pack.yaml 标 draft: true，界面显示"草稿·需人工校对"角标
  - 广告法/平台通用词表直接复用模板包成熟版本，仅追加行业增补词
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
from .knowledge import Pack
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
_creating: set[str] = set()

log = logging.getLogger(__name__)

# 建包时从模板包**原样复制**的文件（不走模型）。
# ⚠ 模板包目前回退到电梯包（`packs/_template` 还不存在，见 TEMPLATE_PACK），
# 而 `patterns/growth.md` 的标题就是「电梯口播特有的取舍」、`hooks.md`/`voice.md` 同理 ——
# 它们会经 `$growth` / `$hooks` **每轮注入**给新行业的模型。
# 也就是说新建包自带的这三份内容是**别的行业**的，必须在校对清单里改掉；
# 真要根治，得把这三份拆成「通用骨架 + 行业段」或建出 `_template` 包。
GENERIC_FILES = [
    "skill.yaml",
    "patterns/hooks.md", "patterns/growth.md",
    "knowledge/voice.md", "patterns/anti-ai-smell.md",
    "rules/duration.md", "rules/output-template.md",
    "compliance/ad-law.md", "compliance/platform.md",
]
PRIVATE_TEMPLATES = [
    "private/README.md", "private/products.yaml", "private/service.yaml",
    "private/cases.yaml", "private/faq.yaml", "private/raw/README.md",
]

# 模板包：优先用 packs/_template（若存在），否则退回这个包。
# 原来硬编码 "elevator"：该包一旦改名或删除，建包功能整体失效。
TEMPLATE_PACK = "_template"
FALLBACK_TEMPLATE_PACK = "elevator"

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
    s = re.sub(r"[^\w\u4e00-\u9fff]+", "-", industry).strip("-")
    return s or "custom"


def template_pack(root: Path) -> Pack:
    """取模板包。"""
    if (root / "packs" / TEMPLATE_PACK / "pack.yaml").exists():
        return Pack(root, TEMPLATE_PACK)
    return Pack(root, FALLBACK_TEMPLATE_PACK)


def preview_slug(industry: str) -> str:
    """作业入口用的目录名预演：动手之前就能回答「这个行业包已经存在」。

    与 `create_pack` 同一套 slugify 口径（P2-51：目录名跟**用户输入**走）。
    输入全是符号时 slugify 为空串 —— 那时取的是模型给的 display_name，
    预演不出来，交给 `create_pack` 里的正式判定（仍然 409）。
    """
    return slugify(industry.strip())


def create_pack(root: Path, llm: LLMClient, industry: str, description: str, *,
                on_retry=None, on_delta=None, should_abort=None) -> dict:
    """按用户的行业名 + 一句话描述生成一个行业包初稿（草稿态）。

    P1-43 起它跑在后台作业里，三个回调就是作业的三件套：
    - `on_retry(note, attempt, total)` / `on_delta(kind, text)` 原样透传给
      `chat_json`，界面因此能看到重试与思考流（以前是一个哑的长请求）；
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
- ideas: ≥30 条可直接用的选题
- redlines: 5~8 条行业红线（广告法之外的行业特有雷区）
- banwords_extra_hard / banwords_extra_soft: 行业特有禁用词增补
- verify_list: 需人工核实的标准/法规/政策清单（只写名称，不写编号）"""
    )

    out: PackGenOut = llm.chat_json("packgen", PACKGEN_SYSTEM, user, PackGenOut,
                                    on_retry=on_retry, on_delta=on_delta)

    # 空数组兜底：模型偶尔会返回 []，直接下标访问会 IndexError → 500
    segments = [s for s in (out.segments or []) if str(s).strip()]
    audiences = [a for a in (out.audiences or []) if str(a).strip()]
    personas = [p for p in (out.personas or []) if str(p).strip()]
    if not segments or not audiences or not personas:
        raise ValueError(
            "模型返回的行业结构不完整（细分领域/受众/人设存在空项）。"
            "请补充描述后重试，或换一个模型。")

    # P2-51：目录名必须跟**用户输入**走（slugify 保留中文、把 / 等转成 -）。
    # 修复前取模型返回的 display_name —— 模型自由发挥时（实测 mock 返回
    # 「全屋定制/装修」），用户输「门窗定制」却得到目录「全屋定制-装修」，
    # 找不到自己刚建的包，重试还必撞「行业包已存在」。
    slug = slugify(industry.strip()) or slugify(out.display_name or industry)
    if should_abort and should_abort():
        # 检查点放在这里：上面那次模型调用是全部开销所在，往下就该建目录了。
        raise JobCancelled("已取消")
    with _creating_lock:
        if slug in _creating:
            raise FileExistsError(f"行业包正在创建中：{slug}")
        _creating.add(slug)
    try:
        d = root / "packs" / slug
        if d.exists():
            raise FileExistsError(f"行业包已存在：{slug}")

        try:
            _materialize(d, base, out, slug, industry, description,
                         segments, audiences, personas)
        except Exception:
            # 中途失败就把半成品收走：否则重试会被上面的 FileExistsError 挡成 409，
            # 用户只能自己去文件管理器里删目录。
            #
            # 用 rmtree_resilient 而不是 `ignore_errors=True`：后者会把「没删掉」
            # 当成成功，于是半成品目录留在那儿，下次建包照样被 409 挡住，
            # 而我们已经把成功当成既定事实，连日志都不会有 —— 用户看到的现象
            # 永远是「重试一直失败」，却查不出为什么。
            if not rmtree_resilient(d):
                log.warning("半成品目录未能清除，下次建包同名行业会被挡：%s", d)
            raise

        checklist = _checklist(out, slug)
        # P1-44：生成完必须体检 —— 模型输出的包能不能用，不能等用户第一次
        # 生成才发现（生成时已付过费）。加载 Pack(slug)（结构坏 → 抛）+ 
        # param_audit 全量（切片/配额/词表降级逐项列出），体检结果并进校对清单。
        audit_notes = _pack_audit(root, slug)
        if audit_notes:
            checklist += ("\n\n## 引擎体检发现（生成时自动检测，逐项核实后重跑或用前确认）\n"
                          + "\n".join(f"- [ ] {t}" for t in audit_notes))
        write_atomic(d / "校对清单.md", checklist)

        # 产物摘要随返回下发：结果页据此渲染「生成了什么」（细分/受众/人设/选题），
        # 而不是只给一份待核实的校对清单 —— 用户此前「不知道生成了啥」。
        # ⚠ 只回短字符串列表，不回 topics 全文（body 里没有消费方）。
        return {"name": slug, "display_name": out.display_name,
                "dir": str(d), "draft": True,
                "checklist": checklist,
                "verify_list": out.verify_list,
                "segments": [str(x) for x in segments],
                "audiences": [str(x) for x in audiences],
                "personas": [str(x) for x in personas],
                "topic_count": len(out.topics or []),
                "ideas": [str(x) for x in (out.ideas or [])],
                "redlines": [str(x) for x in (out.redlines or [])],
                "banwords_extra_hard": [str(x) for x in (out.banwords_extra_hard or [])],
                "banwords_extra_soft": [str(x) for x in (out.banwords_extra_soft or [])]}
    finally:
        with _creating_lock:
            _creating.discard(slug)


def _pack_audit(root: Path, slug: str) -> list[str]:
    """生成包的自体检（P1-44）：加载 + param_audit 全量，返回人话说明列表。

    返回空列表 = 体检通过。模型输出的包常有「segment 与 topics 章节标题对不上」
    「风格没配语速」「平台没配词表」这类静默降级 —— param_audit 把它们逐项列出；
    结构坏（YAML 解析不了等）则 Pack 直接抛，捕捉后给一句指向性的说明。
    """
    from .knowledge import Pack, param_audit
    notes: list[str] = []
    try:
        pk = Pack(root, slug)
    except Exception as e:                       # noqa: BLE001
        return [f"行业包加载失败：{e} —— 请检查生成的文件结构"]
    for key, mapping in (param_audit(pk.dir, pk.data) or {}).items():
        for value, text in mapping.items():
            notes.append(f"参数「{key}={value}」：{text}")
    return notes


def _materialize(d: Path, base: Pack, out: PackGenOut, slug: str, industry: str,
                 description: str, segments: list[str], audiences: list[str],
                 personas: list[str]) -> None:
    for sub in ("knowledge", "compliance", "patterns", "rules", "private/raw"):
        (d / sub).mkdir(parents=True, exist_ok=True)

    # 1. 通用文件直接复用模板包（广告法/平台规则/钩子/增长/时长/输出模板）
    for rel in GENERIC_FILES:
        src = base.dir / rel
        if src.exists():
            (d / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, d / rel)
    # 2. 私有资料模板
    for rel in PRIVATE_TEMPLATES:
        src = base.dir / rel
        if src.exists():
            (d / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, d / rel)

    # 3. 知识文件（draft 内容）
    topic_lines = ["# 分领域内容知识库（向导生成初稿，draft：需人工校对）", "",
                   "> ⚠️ 本文件由模型生成。core 与 myth/fact 需对照行业实际核实后删除本提示。", ""]
    for i, t in enumerate(out.topics or [], 1):
        topic_lines += [f"## {i}. {t.heading}", "", "### 核心知识点", t.core, ""]
        if t.myths:
            topic_lines += ["### 常见误区", "| 误区 | 事实 |", "|---|---|"]
            topic_lines += [f"| {m.get('myth', '')} | {m.get('fact', '')} |" for m in t.myths]
            topic_lines.append("")
        if t.placeholders:
            topic_lines += ["### 需占位的事实", "、".join(t.placeholders) + " → `{{待补}}`", ""]
        topic_lines += ["---", ""]
    write_atomic(d / "knowledge/topics.md", "\n".join(topic_lines))

    aud_lines = ["# 受众痛点库与话术适配（向导生成初稿，draft：需人工校对）", ""]
    for a in out.audience_details or []:
        aud_lines += [f"## {a.name}", "", "### 深层恐惧（痛点）",
                      "、".join(a.fears) or "（待补充）", "",
                      "### 高频疑问（选题金矿）"]
        aud_lines += [f"- {q}" for q in a.questions]
        aud_lines += ["", f"### 推荐 CTA：{a.cta or '（待补充）'}", "", "---", ""]
    write_atomic(d / "knowledge/audience.md", "\n".join(aud_lines))

    idea_lines = ["# 选题库（向导生成初稿，draft：需人工校对）", ""]
    idea_lines += [f"{i}. {t}" for i, t in enumerate(out.ideas or [], 1)]
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

    red = ["# 行业红线（向导生成初稿，draft：需人工校对）", "",
           "> ⚠️ 以下红线由模型按行业常识生成，发布相关内容前务必人工核实补全。", ""]
    red += [f"- {r}" for r in out.redlines or []]
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


def _checklist(out: PackGenOut, slug: str) -> str:
    lines = ["# 新行业包校对清单", "",
             f"行业：{out.display_name}（目录 packs/{slug}/，**草稿状态**）", "",
             "使用前请逐项核实：", ""]
    lines += [f"- [ ] {v}" for v in out.verify_list or []]
    lines += ["", "## 红线核实", ""]
    lines += [f"- [ ] {r}" for r in out.redlines or []]
    lines += ["", "## 知识核实", "", "- [ ] topics.md 各细分的核心知识点与误区表",
              "- [ ] audience.md 受众痛点与 CTA", "- [ ] ideas.md 选题是否符合本行业实际",
              "- [ ] banwords.yaml 行业增补词是否恰当", "",
              "核实完成后：把 pack.yaml 中 `draft: true` 改为 `false`，角标即消失。"]
    return "\n".join(lines)
