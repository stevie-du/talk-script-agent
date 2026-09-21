# -*- coding: utf-8 -*-
"""提示词渲染：把 skill.yaml 的模板 + 行业包的知识文件组装成 (system, user)。

从 pipeline.py 拆出来的理由：这段逻辑跟「流程走到哪一步」无关，只跟
「一个阶段需要哪些上下文」有关，两者混在一起时，改提示词要先读懂状态机。

读文件一律走 `Pack.file_text`（带 mtime 缓存），因此每一轮回炉不再重复读盘。
"""
from __future__ import annotations

import json
import logging
import re
from string import Template

from .knowledge import Pack

log = logging.getLogger(__name__)

# 模板里引用的 $占位符（含 ${name} 写法）。用捕获组拿到标识符本身。
# 为什么需要这个检查：`Template.safe_substitute` 对未知变量**保持原样**，
# 于是拼错的占位符不会报错 —— 模型会收到一段字面量 "$growth_block"，
# 而本该注入的知识文件静默丢失。这个 bug 在 elevator 包里真实存在过
# （files 的 key 是 growth，模板写的却是 $growth_block），
# 结果 patterns/growth.md 从来没有进过提示词。
_PLACEHOLDER = re.compile(r"\$\{?([a-zA-Z_][a-zA-Z0-9_]*)\}?")


class PromptRenderer:
    """按 skill.yaml 渲染各阶段的提示词。引擎不认识行业，只认识这些字段。"""

    def __init__(self, pack: Pack):
        self.pack = pack
        self.skill = pack.skill() or {}

    # ── 基础 ────────────────────────────────────────────────
    def render(self, stage: str, ctx: dict) -> tuple[str, str]:
        cfg = self.skill["stages"][stage]
        system = Template(cfg["system"]).safe_substitute(ctx)
        user = Template(cfg["user_template"]).safe_substitute(ctx)
        return system, user

    def unfilled(self, stage: str, ctx: dict) -> list[str]:
        """模板里引用、但上下文没提供的占位符（去重、保序）。空列表 = 对得上。

        检查的是**模板原文**而不是渲染结果：知识文件会被注入进 user 提示词，
        里面完全可能出现 `$` 开头的字样（价格写法、模板变量示例），
        拿渲染结果去扫会误报。
        """
        cfg = (self.skill.get("stages") or {}).get(stage) or {}
        out: list[str] = []
        for text in (cfg.get("system", ""), cfg.get("user_template", "")):
            for name in _PLACEHOLDER.findall(text or ""):
                if name not in ctx and f"${name}" not in out:
                    out.append(f"${name}")
        return out

    def stage_files(self, stage: str, ctx: dict) -> None:
        """把 stage.files 声明的知识文件内容填进对应占位符（缺失文件→空串）。

        取值支持 `路径` 与 `路径#章节关键词`（后者只注入那一节，见 `Pack.file_slice`）。
        """
        files = (self.skill.get("stages", {}).get(stage, {}).get("files") or {})
        for key, rel in files.items():
            ctx[key] = self.pack.file_slice(rel)

    def voice_parts(self, level: str) -> tuple[str, str]:
        """按人味档位组装 (anti_ai_rule, voice_block)。"""
        cfg = (self.skill.get("stages", {}).get("write", {}).get("anti_ai")) or {}
        if level == "off" or not cfg:
            return "", ""
        rule = (cfg.get("rule") or "").strip()
        lv = (cfg.get("levels") or {}).get(level) or {}
        parts = [self.pack.file_slice(rel) for rel in lv.get("files", []) or []]
        parts = [t for t in parts if t.strip()]
        heading = lv.get("heading") or ""
        block = (heading + "\n" + "\n\n".join(parts)).strip()
        return rule, block

    # ── 上下文 ──────────────────────────────────────────────
    def base_ctx(self, p: dict) -> dict:
        q = p["quota"]
        return {
            "industry": self.pack.info.display_name,
            "topic": p["topic"], "segment": p["segment"], "audience": p["audience"],
            "platform": p["platform"], "style": p["style"], "persona": p["persona"],
            "cta": p["cta"],
            "duration": str(int(p["duration"])), "points": str(p["points"]),
            "rate": str(p["rate"]),
            "quota_total": str(q.get("total", "")), "quota_hook": str(q.get("hook", "")),
            "quota_body_per": str(q.get("body", 0) // max(p["points"], 1)),
            "quota_cta": str(q.get("cta", "")),
        }

    def select_ctx(self, p: dict) -> dict:
        ctx = self.base_ctx(p)
        self.stage_files("select", ctx)
        ctx["topics_slice"] = self.pack.topics_slice(p["segment"])
        ctx["audience_slice"] = self.pack.audience_slice(p["audience"])
        # 钩子库按风格切片（一份文件 5 套语气模板，每轮只用 1 套）。
        # 引擎负责切，所以 skill.yaml 的 select.files 里**不该**再声明 hooks ——
        # 声明了也会被这里覆盖成切片。
        ctx["hooks"] = self.pack.hooks_slice(p["style"])
        return ctx

    def write_ctx(self, p: dict, plan_dump: dict, feedback: str) -> dict:
        ctx = self.base_ctx(p)
        self.stage_files("write", ctx)
        rule, voice_block = self.voice_parts(p.get("voice", "strong"))
        ctx["anti_ai_rule"] = rule
        ctx["voice_block"] = voice_block
        ctx["plan_json"] = json.dumps(plan_dump, ensure_ascii=False, indent=1)
        ctx["feedback_block"] = (
            f"\n【回炉改写】上一版未通过代码校验，必须解决以下问题：\n{feedback}\n"
            if feedback else "")
        facts_block = ""
        if p.get("facts"):
            facts_block += f"\n【用户提供的资料】\n{p['facts']}\n"
        private = self.pack.private_facts()
        if private:
            facts_block += f"\n【私有知识库（优先作为事实来源）】\n{private}\n"
        ctx["facts_block"] = facts_block
        return ctx

    def storyboard_ctx(self, sections: list[dict], timings: list[dict]) -> dict:
        """分镜阶段上下文：行业名（系统提示用）+ 带时间轴的段落。

        时间轴由 `pipeline._compute_timings` 算（口播字数/语速），模型不碰算术
        —— 与字数配额同口径：模型只做创意，不做计算。

        这里**不**走 `base_ctx`：单段重写路径拿到的 params 是产物里回读的
        `result["params"]`，只有 PERSISTED_PARAMS 那几项（没有 quota/points），
        拼全量上下文会在这里 KeyError —— 而分镜模板本来也不需要配额。
        模板若引用了别的占位符，`unfilled()` 会把它记进日志。
        """
        rows = []
        for i, (s, tm) in enumerate(zip(sections, timings)):
            rows.append(
                f"[{i + 1}] {s.get('type', '')}段 · "
                f"{tm.get('start', 0)}-{tm.get('end', 0)}s\n{s.get('text', '')}")
        ctx = {
            "industry": self.pack.info.display_name,
            "segments_with_time": "\n\n".join(rows),
        }
        self.stage_files("storyboard", ctx)
        return ctx

    def rewrite_ctx(self, sections: list[dict], index: int, seg_quota: int,
                    feedback: str) -> dict:
        seg = sections[index]
        ctx = {
            "industry": self.pack.info.display_name,
            "context": "\n".join(f"[{s['type']}] {s['text'][:40]}…"
                                 for i, s in enumerate(sections) if i != index),
            "seg_type": seg["type"], "seg_text": seg["text"],
            "seg_quota": str(seg_quota),
            "seg_feedback": feedback or "按合规与口语化要求优化",
        }
        self.stage_files("rewrite_segment", ctx)
        return ctx
