# -*- coding: utf-8 -*-
"""行业包 → Agent 技能导出器

把引擎用的行业包导出为"文件型技能"目录（SKILL.md + 知识文件 + 独立校验工具），
可放入任何支持技能的 agent（WorkBuddy / ZCode / Claude 等的技能目录）。

SKILL.md 由包的 pack.yaml + skill.yaml 自动组装：
  - 流程指令来自 skill.yaml 各阶段提示词（剥离引擎专用的 JSON 输出指令）
  - 参数表来自 pack.yaml
  - 知识/合规/方法/规则文件原样复制
tools/check.py 为引擎校验器的独立副本（默认 pack 目录 = 技能根目录）。
"""
from __future__ import annotations

import re
import shutil
from pathlib import Path

import yaml

from .knowledge import Pack

COPY_DIRS = ["knowledge", "patterns", "rules", "compliance", "private"]
COPY_FILES = ["pack.yaml", "banwords.yaml", "skill.yaml"]
CHECKER_PATCH = (
    'def _default_pack() -> Path:\n'
    '    return Path(__file__).resolve().parent.parent\n'
)


def _frontmatter_name(pack_name: str) -> str:
    return re.sub(r"[^\w-]+", "-", pack_name).strip("-").lower() or "custom"


def _clean_system(system: str, display_name: str, anti_ai_rule: str) -> str:
    """把引擎用 system 提示词改写为 agent 用说明：
    去掉 JSON 输出指令（引擎专属），填入行业名与反 AI 味规则。"""
    out = system.replace("$industry", display_name)
    rule = (anti_ai_rule or "").strip()
    out = out.replace("$anti_ai_rule", rule)
    keep = [l for l in out.splitlines() if "只输出一个 JSON" not in l]
    return "\n".join(keep).strip()


def export_agent_skill(root: Path, pack_name: str, out_dir: Path | None = None) -> dict:
    pack = Pack(root, pack_name)
    skill = pack.skill() or {}
    stages = skill.get("stages", {})
    info = pack.info
    fm_name = _frontmatter_name(pack.name)

    out = out_dir or (root / "agent-skills" / fm_name)
    out.mkdir(parents=True, exist_ok=True)
    for d in COPY_DIRS:
        src = pack.dir / d
        if src.exists():
            shutil.copytree(src, out / d, dirs_exist_ok=True)
    for f in COPY_FILES:
        src = pack.dir / f
        if src.exists():
            shutil.copyfile(src, out / f)

    # 校验器独立副本：默认包目录 = 技能根目录
    checker_src = (Path(__file__).resolve().parent / "checker.py").read_text(encoding="utf-8")
    if CHECKER_PATCH.strip() not in checker_src:
        raise RuntimeError("checker.py 结构变化，导出补丁失效——请同步更新 export_skill")
    checker_src = checker_src.replace(
        'def _default_pack() -> Path:\n'
        '    return Path(__file__).resolve().parent.parent / "packs" / "elevator"',
        CHECKER_PATCH)
    (out / "tools").mkdir(exist_ok=True)
    (out / "tools" / "check.py").write_text(checker_src, encoding="utf-8")

    # ---- 组装 SKILL.md ----
    segments = pack.param_options("segment")
    audiences = pack.param_options("audience")
    platforms = pack.param_options("platform")
    styles = pack.param_options("style")
    personas = pack.param_options("persona")
    ctas = pack.param_options("cta")
    durations = pack.param_options("duration")

    write_sys = stages.get("write", {}).get("system", "")
    anti_ai = stages.get("write", {}).get("anti_ai", {})
    write_rules = _clean_system(write_sys, info.display_name, anti_ai.get("rule", ""))
    # 去 markdown 缩进噪音：引擎 system 是块标量，直接使用

    seg_lines = "\n".join(f"| {s} | … | … |" for s in segments)
    desc = (f"生成{info.display_name}行业短视频口播脚本。当用户要求写{info.display_name}相关的"
            f"口播文案、短视频脚本、分镜脚本时使用。覆盖 {'、'.join(segments)} 等主题，"
            f"输出含开场钩子、正文要点、结尾引导与预估时长。")

    skill_md = f"""---
name: {fm_name}
description: {desc}
---

# {info.display_name}行业口播脚本生成器

> 本目录是一个自包含的{info.display_name}行业知识包。按需读取引用文件，不要一次性全量加载。

## 你的角色

你是{info.display_name}行业资深的短视频内容策划。你懂行业，也懂短视频（前 3 秒定生死、完播率、转化引导）。你写的不是说明书，是能让人一口气听完并行动的口语。

## 输入参数（缺失用默认值，不要反问超过一个问题）

| 参数 | 取值 | 默认 |
|---|---|---|
| topic | 自由文本 | 必填 |
| segment | {' / '.join(map(str, segments))} | {pack.param_default('segment', '')} |
| audience | {' / '.join(map(str, audiences))} | {pack.param_default('audience', '')} |
| duration | {' / '.join(map(str, durations))} 秒 | {pack.param_default('duration', 60)} |
| style | {' / '.join(map(str, styles))} | {pack.param_default('style', '')} |
| platform | {' / '.join(map(str, platforms))} | {pack.param_default('platform', '抖音')} |
| persona | {' / '.join(map(str, personas))} | {pack.param_default('persona', '')} |
| cta | {' / '.join(map(str, ctas))} | {pack.param_default('cta', '关注')} |
| facts | 用户提供的产品手册、数据、案例 | 无 |

## 执行流程

### 前置：加载私有资料（持久化知识库）

`private/` 是持久化的底层知识库，写入一次之后每次生成都自动使用。按需读取：
products.yaml（型号参数，产品推介必读）/ service.yaml（服务承诺，转化类必读）/
cases.yaml（案例背书）/ faq.yaml（异议应答）。`private/raw/` 有未处理文件时先提示用户。
**已填部分优先作为事实来源；未填部分用 `{{{{待补：xxx}}}}` 占位，绝不估算。**

### 第 1 步 参数解析与字数配额

读 `rules/duration.md` 与 `pack.yaml` 的 `quota_table`（配额 = 表值 × 语速 ÷ 5.0）。
结论（目标字数与分段配额）必须写进输出头部。

### 第 2 步 选题策划

- 读 `knowledge/topics.md`：按 segment 对照 `pack.yaml` 的 `topics_map` 只取对应章节
- 读 `knowledge/audience.md`：按 audience 对照 `audience_map` 取对应章节
- 用户没给主题时，从 `knowledge/ideas.md` 推荐 3 个选题
- 读 `patterns/hooks.md` 选钩子类型（同一主题多版必须换类型）；`patterns/growth.md` 确认完播与转化策略

### 第 3 步 文案撰写

按以下规则与语气执行（来自本包技能定义）：

{write_rules}

### 第 4 步 合规扫描与时长复算（不可跳过）

**字数和禁用词必须用代码校验，不要目测——模型数不准。** 把口播文案存成文件后执行：

```
python tools/check.py <脚本文件> --duration <目标秒数> --rate <语速> --platform <平台>
```

- 命中「必改」级 → 必须改写后重跑，最多回炉 2 轮
- 命中「待确认」级 → 人工判断语境（如"最危险的动作""第一，按警铃"属正常用法）
- 偏差超 ±10% → 重写
- 无法执行脚本时，退回人工自查：`compliance/ad-law.md` → `compliance/platform.md` → `compliance/industry.md`

### 第 5 步 输出

按 `rules/output-template.md` 输出：①参数回执 ②口播文案 ③分镜表 ④合规检查报告 ⑤拍摄与发布提示。

## 硬约束（输出前再读一遍）

1. 禁止编造事实：`private/` 未填且用户未提供 facts 时，一律 `{{{{待补：xxx}}}}` 占位
2. 禁止绝对化安全承诺与权威背书（见 `compliance/industry.md` 红线）
3. 禁止危险行为演示（扒门、攀爬轿顶、短接门锁等，包括反面演示）
4. 救援口径唯一：按警铃或对讲 → 拨打当地电梯应急救援服务电话 → 原地等待；不写死号码
5. 不诋毁同行；平台差异按 `compliance/platform.md`
6. 引用标准编号前查 `knowledge/standards.md`，编号不确定就只说标准名称

## 术语与事实

引用标准编号前先查 `knowledge/standards.md`。**编号不确定就只说标准名称**——写错编号比不写更损害专业度。

## 文件索引

| 目录/文件 | 作用 |
|---|---|
| `pack.yaml` | 参数/配额/映射清单 |
| `skill.yaml` | 生成技能定义（本说明的来源，可继续编辑） |
| `banwords.yaml` | 禁用词：必改 / 待确认 × 平台分级 |
| `knowledge/` | 细分知识点 / 受众 / 选题库 / 标准索引 / 口语化声音 |
| `patterns/` | 钩子库与风格 / 完播转化 / 反 AI 味清单 |
| `rules/` | 时长配额 / 输出模板 |
| `compliance/` | 广告法 / 平台规则 / 行业红线 |
| `private/` | 私有资料（分享技能时删除此目录） |
| `tools/check.py` | 校验器：字数 / 时长 / 禁用词 |
"""
    (out / "SKILL.md").write_text(skill_md, encoding="utf-8")

    fm = yaml.safe_load((out / "SKILL.md").read_text(encoding="utf-8").split("---")[1])
    assert fm.get("name") and fm.get("description"), "SKILL.md frontmatter 校验失败"

    return {"path": str(out), "name": fm_name,
            "files": sum(1 for f in out.rglob("*") if f.is_file()),
            "hints": [
                "把整个目录复制到目标 agent 的技能目录即可使用：",
                "WorkBuddy: C:\\Users\\<你>\\.workbuddy\\skills\\",
                "ZCode: C:\\Users\\<你>\\.zcode\\skills\\",
                "Claude Code: C:\\Users\\<你>\\.claude\\skills\\",
                "分享给同事时删除 private/ 目录（商业信息不外带）。",
            ]}
