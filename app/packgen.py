# -*- coding: utf-8 -*-
"""行业包生成器：输入行业名+业务描述，按 elevator 包同 schema 生成初版行业包。

草稿保护：
  - 生成包 pack.yaml 标 draft: true，界面显示"草稿·需人工校对"角标
  - 广告法/平台通用词表直接复用 elevator 包成熟版本，仅追加行业增补词
  - 标准/法规编号一律不生成，只写"待核实清单"（防编造监管依据）
"""
from __future__ import annotations

import re
import shutil
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from .knowledge import Pack
from .llm import LLMClient

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

PACKGEN_SYSTEM = """你是行业知识包编辑，为口播脚本智能体制作新行业的知识包初稿。
铁律：
1. 只输出一个 JSON 对象，不要任何多余文字
2. 绝不编造标准编号、法规文号、具体数据——需要核实的一律放进 verify_list
3. 行业红线宁严勿松，拿不准的不写
4. 内容必须具体可用（写"怎么选、怎么看、常见坑"），不写正确的废话"""


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


def create_pack(root: Path, llm: LLMClient, industry: str, description: str) -> dict:
    base = Pack(root, "elevator")
    user = (
        f"【任务】为口播脚本智能体生成「{industry}」行业的知识包初稿。\n"
        f"【业务描述】{description}\n"
        """【参照结构】电梯行业的包包含：细分领域（如 维保/加装/家用电梯）、受众（如 业主/物业/维保公司）、
人设、每个细分的知识点+常见误区表+需占位事实、每类受众的深层恐惧+高频疑问+推荐CTA、
可直接使用的选题库、行业红线、行业增补禁用词、待核实清单。

【输出 JSON 字段】
- display_name: 行业显示名
- segments / audiences / personas: 数组
- topics: [{"heading": "...", "core": "核心知识点2~4句", "myths": [{"myth": "...", "fact": "..."}], "placeholders": ["需用户提供的事实"]}]
  —— topics 覆盖全部 segments，heading 与 segments 一致
- audience_details: [{"name": "与 audiences 一致", "fears": ["深层恐惧"], "questions": ["高频疑问"], "cta": "..."}]
- ideas: ≥30 条可直接用的选题
- redlines: 5~8 条行业红线（广告法之外的行业特有雷区）
- banwords_extra_hard / banwords_extra_soft: 行业特有禁用词增补
- verify_list: 需人工核实的标准/法规/政策清单（只写名称，不写编号）"""
    )

    out: PackGenOut = llm.chat_json("packgen", PACKGEN_SYSTEM, user, PackGenOut)

    slug = slugify(out.display_name or industry)
    d = root / "packs" / slug
    if d.exists():
        raise FileExistsError(f"行业包已存在: {slug}")
    for sub in ("knowledge", "compliance", "patterns", "rules", "private/raw"):
        (d / sub).mkdir(parents=True, exist_ok=True)

    # 1. 通用文件直接复用电梯包（广告法/平台规则/钩子/增长/时长/输出模板）
    for rel in GENERIC_FILES:
        shutil.copyfile(base.dir / rel, d / rel)
    # 2. 私有资料模板
    for rel in PRIVATE_TEMPLATES:
        src = base.dir / rel
        if src.exists():
            (d / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, d / rel)

    # 3. 知识文件（draft 内容）
    topic_lines = ["# 分领域内容知识库（向导生成初稿，draft：需人工校对）", "",
                   "> ⚠️ 本文件由模型生成。core 与 myth/fact 需对照行业实际核实后删除本提示。", ""]
    for i, t in enumerate(out.topics, 1):
        topic_lines += [f"## {i}. {t.heading}", "", "### 核心知识点", t.core, ""]
        if t.myths:
            topic_lines += ["### 常见误区", "| 误区 | 事实 |", "|---|---|"]
            topic_lines += [f"| {m.get('myth', '')} | {m.get('fact', '')} |" for m in t.myths]
            topic_lines.append("")
        if t.placeholders:
            topic_lines += ["### 需占位的事实", "、".join(t.placeholders) + " → `{{待补}}`", ""]
        topic_lines.append("---")
        topic_lines.append("")
    (d / "knowledge/topics.md").write_text("\n".join(topic_lines), encoding="utf-8")

    aud_lines = ["# 受众痛点库与话术适配（向导生成初稿，draft：需人工校对）", ""]
    for a in out.audience_details:
        aud_lines += [f"## {a.name}", "", "### 深层恐惧（痛点）",
                      "、".join(a.fears) or "（待补充）", "",
                      "### 高频疑问（选题金矿）"]
        aud_lines += [f"- {q}" for q in a.questions]
        aud_lines += ["", f"### 推荐 CTA：{a.cta or '（待补充）'}", "", "---", ""]
    (d / "knowledge/audience.md").write_text("\n".join(aud_lines), encoding="utf-8")

    idea_lines = ["# 选题库（向导生成初稿，draft：需人工校对）", ""]
    idea_lines += [f"{i}. {t}" for i, t in enumerate(out.ideas, 1)]
    (d / "knowledge/ideas.md").write_text("\n".join(idea_lines), encoding="utf-8")

    std_lines = ["# 标准、法规与术语库（占位）", "",
                 "> ⚠️ 向导不生成任何标准/法规编号（防编造）。引用前逐条核实后手工补录。",
                 "", "## 待人工核实的清单", ""]
    std_lines += [f"- [ ] {v}" for v in out.verify_list]
    (d / "knowledge/standards.md").write_text("\n".join(std_lines), encoding="utf-8")

    red = ["# 行业红线（向导生成初稿，draft：需人工校对）", "",
           "> ⚠️ 以下红线由模型按行业常识生成，发布相关内容前务必人工核实补全。", ""]
    red += [f"- {r}" for r in out.redlines]
    (d / "compliance/industry.md").write_text("\n".join(red), encoding="utf-8")

    # 4. 词表：复用电梯包通用词表 + 行业增补
    bw = base.banwords_data()
    bw.setdefault("hard", [])
    bw.setdefault("soft", [])
    bw["hard"] = sorted(set(bw["hard"]) | set(out.banwords_extra_hard))
    bw["soft"] = sorted(set(bw["soft"]) | set(out.banwords_extra_soft))
    bw["updated"] = "draft-向导生成"
    (d / "banwords.yaml").write_text(
        yaml.safe_dump(bw, allow_unicode=True, sort_keys=False), encoding="utf-8")

    # 5. pack.yaml（identity 映射：segments/audiences 与知识标题一致）
    identity = lambda xs: {x: x for x in xs}  # noqa: E731
    pack_yaml = {
        "name": slug,
        "display_name": out.display_name,
        "draft": True,
        "version": 1,
        "description": f"{industry}：{description}（向导生成初稿，需人工校对）",
        "params": {
            "segment": {"label": "细分领域", "options": out.segments, "default": out.segments[0]},
            "audience": {"label": "受众", "options": out.audiences, "default": out.audiences[0]},
            "duration": {"label": "时长（秒）", "options": [15, 30, 60, 90, 180], "default": 60},
            "style": {"label": "风格",
                      "options": ["权威科普", "亲和接地气", "幽默玩梗", "严肃警示", "销售转化"],
                      "default": "亲和接地气"},
            "platform": {"label": "平台", "options": ["抖音", "视频号", "小红书", "B站"], "default": "抖音"},
            "persona": {"label": "人设", "options": out.personas, "default": out.personas[0]},
            "cta": {"label": "结尾引导", "options": ["关注", "私信", "留资", "到店", "评论关键词"],
                    "default": "关注"},
        },
        "rate_by_style": {"权威科普": 4.5, "亲和接地气": 4.5, "幽默玩梗": 5.0,
                          "严肃警示": 4.2, "销售转化": 5.0},
        "quota_table": base.data.get("quota_table"),
        "points_by_duration": base.data.get("points_by_duration"),
        "topics_map": identity(out.segments),
        "audience_map": identity(out.audiences),
        "files": {
            "select": ["knowledge/topics.md", "knowledge/audience.md", "knowledge/ideas.md",
                       "patterns/hooks.md"],
            "write": ["patterns/hooks.md", "patterns/growth.md", "knowledge/voice.md",
                      "patterns/anti-ai-smell.md", "rules/duration.md",
                      "rules/output-template.md"],
            "compliance": ["compliance/ad-law.md", "compliance/platform.md",
                           "compliance/industry.md"],
            "facts": ["knowledge/standards.md"],
            "private": ["private/products.yaml", "private/service.yaml",
                        "private/cases.yaml", "private/faq.yaml"],
        },
        "banwords": "banwords.yaml",
    }
    (d / "pack.yaml").write_text(
        yaml.safe_dump(pack_yaml, allow_unicode=True, sort_keys=False), encoding="utf-8")

    # 6. 校对清单
    checklist = ["# 新行业包校对清单", "",
                 f"行业：{out.display_name}（目录 packs/{slug}/，**草稿状态**）", "",
                 "使用前请逐项核实：", ""]
    checklist += [f"- [ ] {v}" for v in out.verify_list]
    checklist += ["", "## 红线核实", ""]
    checklist += [f"- [ ] {r}" for r in out.redlines]
    checklist += ["", "## 知识核实", "", "- [ ] topics.md 各细分的核心知识点与误区表",
                  "- [ ] audience.md 受众痛点与 CTA", "- [ ] ideas.md 选题是否符合本行业实际",
                  "- [ ] banwords.yaml 行业增补词是否恰当", "",
                  "核实完成后：把 pack.yaml 中 `draft: true` 改为 `false`，角标即消失。"]
    (d / "校对清单.md").write_text("\n".join(checklist), encoding="utf-8")

    return {"name": slug, "display_name": out.display_name,
            "dir": str(d), "draft": True,
            "checklist": "\n".join(checklist),
            "verify_list": out.verify_list}
