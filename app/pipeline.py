# -*- coding: utf-8 -*-
"""流水线编排：参数归一 → 选题策划 → 文案撰写 → 校验回炉 → 组装落盘

生成方法（提示词、注入文件、回炉上限）不在代码里，而是每个行业包自带的 skill.yaml
——知识库管"写什么"，技能管"怎么写"，两者都随包配置、随包分发。

作业状态机:
    queued → selecting → (paused_awaiting_confirmation，仅分步模式)
           → writing → checking(第N轮) → done / failed
单段重写: rewrite_segment 对单卡局部改写并重跑校验，不整篇回炉。
"""
from __future__ import annotations

import json
import re
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from string import Template

from pydantic import BaseModel

from .checker import Banwords, Quota, check_script, count_chars
from .config import AppConfig, load_config
from .knowledge import Pack
from .llm import LLMClient
from .schemas import (ConfirmRequest, GenerateRequest, RewriteSegmentRequest,
                      ScriptSection, StoryboardShot, TopicPlan)


class ScriptDraft(BaseModel):
    sections: list[ScriptSection]
    storyboard: list[StoryboardShot]


class SegmentRewrite(BaseModel):
    text: str
    subtitle: str = ""


class Job:
    def __init__(self, jid: str, kind: str, params: dict):
        self.id = jid
        self.kind = kind                  # generate / packgen
        self.params = params
        self.state = "queued"
        self.steps: list[dict] = []
        self.error: str | None = None
        self.result: dict | None = None
        self.created_at = datetime.now().isoformat(timespec="seconds")
        self._lock = threading.Lock()

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "id": self.id, "kind": self.kind, "state": self.state,
                "params": self.params, "steps": self.steps,
                "error": self.error, "result": self.result,
                "created_at": self.created_at,
            }

    def update(self, **kw):
        with self._lock:
            for k, v in kw.items():
                setattr(self, k, v)


class Pipeline:
    def __init__(self, root: Path, cfg: AppConfig):
        self.root = root
        self.cfg = cfg
        self.mock = getattr(cfg, "mock", False)
        self.llm = self.build_llm()
        self.jobs: dict[str, Job] = {}

    def build_llm(self) -> LLMClient:
        """每次生成前重建客户端：设置里改了 Key/模型立即生效，无需重启。"""
        cfg = load_config(self.root)
        return LLMClient(cfg.llm, mock=self.mock)

    # ── 对外接口 ────────────────────────────────────────────
    def start_generate(self, req: GenerateRequest) -> str:
        pack = Pack(self.root, req.pack)
        self.llm = self.build_llm()
        jid = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
        job = Job(jid, "generate", req.model_dump())
        self.jobs[jid] = job
        self._spawn(job, lambda: self._run_generate(job, pack))
        return jid

    def confirm(self, jid: str, req: ConfirmRequest) -> dict:
        job = self.jobs.get(jid)
        if not job:
            raise KeyError(jid)
        if job.state != "paused_awaiting_confirmation":
            raise ValueError(f"作业状态为 {job.state}，不可确认")
        plan = TopicPlan.model_validate(req.plan)
        pack = Pack(self.root, job.params["pack"])
        skill = pack.skill()
        if not skill:
            raise ValueError(f"行业包缺少 skill.yaml：{pack.name}")
        self._spawn(job, lambda: self._continue_write(job, pack, skill, plan))
        return job.snapshot()

    def rewrite_segment(self, jid: str, req: RewriteSegmentRequest) -> dict:
        job = self.jobs.get(jid)
        if not job:
            raise KeyError(jid)
        if job.state != "done":
            raise ValueError(f"作业状态为 {job.state}，仅完成后可单段重写")
        result = job.result
        if not result or not (0 <= req.index < len(result["sections"])):
            raise ValueError("段落序号无效")
        pack = Pack(self.root, result["pack"])
        self.llm = self.build_llm()
        self._spawn(job, lambda: self._run_rewrite_segment(job, pack, req.index, req.feedback or ""))
        return job.snapshot()

    def cancel(self, jid: str) -> dict:
        """取消排队/待确认中的作业（进行中的写入线程不可中断，仅这两种状态可取消）。"""
        job = self.jobs.get(jid)
        if not job:
            raise KeyError(jid)
        if job.state not in ("queued", "paused_awaiting_confirmation"):
            raise ValueError(f"作业状态为 {job.state}，不可取消")
        job.update(state="cancelled")
        self._persist(job)
        return job.snapshot()

    # ── 生成主流程 ──────────────────────────────────────────
    def _run_generate(self, job: Job, pack: Pack):
        try:
            skill = pack.skill()
            if not skill:
                raise ValueError(f"行业包缺少 skill.yaml（生成技能定义）：{pack.name}")
            p = self._normalize(pack, job.params)
            job.update(params=p, state="selecting")
            plan = self._select(job, pack, skill, p)
            self._step(job, "select", "选题策划", {"plan": plan.model_dump()})
            if p.get("mode") == "step":
                job.update(state="paused_awaiting_confirmation",
                           result={"plan": plan.model_dump()})
                self._persist(job)
                return
            self._continue_write(job, pack, skill, plan)
        except Exception as e:  # noqa: BLE001
            job.update(state="failed", error=str(e))
            self._persist(job)

    def _continue_write(self, job: Job, pack: Pack, skill: dict, plan: TopicPlan):
        try:
            p = job.params
            draft, revisions = self._write_with_recheck(job, pack, skill, p, plan)
            result = self._finalize(job, pack, p, plan, draft, revisions)
            job.update(state="done", result=result)
            self._persist(job)
        except Exception as e:  # noqa: BLE001
            job.update(state="failed", error=str(e))
            self._persist(job)

    # ── 技能渲染 ────────────────────────────────────────────
    @staticmethod
    def _render(skill: dict, stage: str, ctx: dict) -> tuple[str, str]:
        """按 skill.yaml 渲染某阶段的 (system, user) 提示词。"""
        cfg = skill["stages"][stage]
        system = Template(cfg["system"]).safe_substitute(ctx)
        user = Template(cfg["user_template"]).safe_substitute(ctx)
        return system, user

    @staticmethod
    def _stage_files(pack: Pack, skill: dict, stage: str, ctx: dict) -> None:
        """把 stage.files 声明的知识文件内容填进对应占位符（缺失文件→空串）。"""
        for key, rel in (skill.get("stages", {}).get(stage, {}).get("files", {}) or {}).items():
            ctx[key] = pack.file_text(rel)

    @staticmethod
    def _voice_parts(pack: Pack, skill: dict, level: str) -> tuple[str, str]:
        """按人味档位组装 (anti_ai_rule, voice_block)。"""
        cfg = (skill.get("stages", {}).get("write", {}).get("anti_ai")) or {}
        if level == "off" or not cfg:
            return "", ""
        rule = (cfg.get("rule") or "").strip()
        lv = (cfg.get("levels") or {}).get(level) or {}
        parts = [pack.file_text(rel) for rel in lv.get("files", [])]
        parts = [t for t in parts if t.strip()]
        heading = lv.get("heading") or ""
        block = (heading + "\n" + "\n\n".join(parts)).strip()
        return rule, block

    def _base_ctx(self, pack: Pack, p: dict) -> dict:
        q = p["quota"]
        return {
            "industry": pack.info.display_name,
            "topic": p["topic"], "segment": p["segment"], "audience": p["audience"],
            "platform": p["platform"], "style": p["style"], "persona": p["persona"],
            "duration": str(int(p["duration"])), "points": str(p["points"]),
            "rate": str(p["rate"]),
            "quota_total": str(q["total"]), "quota_hook": str(q["hook"]),
            "quota_body_per": str(q["body"] // max(p["points"], 1)),
            "quota_cta": str(q["cta"]),
        }

    # ── 节点实现 ────────────────────────────────────────────
    def _normalize(self, pack: Pack, params: dict) -> dict:
        def pick(key, fallback=None):
            v = params.get(key)
            return v if v not in (None, "", []) else pack.param_default(key, fallback)

        duration = float(pick("duration", 60))
        style = str(pick("style", ""))
        rate = params.get("rate") or pack.rate_for_style(style)
        quota = Quota.from_pack(pack.data)
        out = {
            "pack": pack.name, "topic": str(params.get("topic", "")).strip(),
            "segment": pick("segment"), "audience": pick("audience"),
            "duration": duration, "style": style,
            "platform": pick("platform"), "persona": pick("persona"),
            "cta": pick("cta"), "facts": params.get("facts") or "",
            "mode": params.get("mode", "auto"), "rate": float(rate),
            "voice": params.get("voice") if params.get("voice") in ("strong", "standard", "off") else "strong",
            "format": params.get("format") if params.get("format") in ("both", "voice") else "both",
            "points": pack.points_limit(duration),
            "quota": quota.target(duration, float(rate)),
        }
        if not out["topic"]:
            raise ValueError("主题不能为空")
        return out

    def _select(self, job: Job, pack: Pack, skill: dict, p: dict) -> TopicPlan:
        ctx = self._base_ctx(pack, p)
        self._stage_files(pack, skill, "select", ctx)
        ctx["topics_slice"] = pack.topics_slice(p["segment"])
        ctx["audience_slice"] = pack.audience_slice(p["audience"])
        system, user = self._render(skill, "select", ctx)
        return self.llm.chat_json("select", system, user, TopicPlan,
                                  on_retry=self._retry_logger(job))

    def _write_with_recheck(self, job: Job, pack: Pack, skill: dict,
                            p: dict, plan: TopicPlan) -> tuple[dict, list[dict]]:
        ban = Banwords(pack.banwords_data())
        quota = Quota.from_pack(pack.data)
        rounds = 1 + int((skill.get("limits") or {}).get("recheck_rounds", 2))
        revisions: list[dict] = []
        feedback = ""
        for rnd in range(1, rounds + 1):
            job.update(state="writing" if rnd == 1 else "rewriting")
            self._step(job, f"write_r{rnd}", "文案撰写" if rnd == 1 else f"回炉改写·第 {rnd - 1} 轮",
                       {"feedback": feedback} if feedback else {})

            ctx = self._base_ctx(pack, p)
            self._stage_files(pack, skill, "write", ctx)
            rule, voice_block = self._voice_parts(pack, skill, p.get("voice", "strong"))
            ctx["anti_ai_rule"] = rule
            ctx["voice_block"] = voice_block
            ctx["plan_json"] = json.dumps(plan.model_dump(), ensure_ascii=False, indent=1)
            fb = ""
            if feedback:
                fb = f"\n【回炉改写】上一版未通过代码校验，必须解决以下问题：\n{feedback}\n"
            ctx["feedback_block"] = fb
            facts_block = ""
            if p.get("facts"):
                facts_block += f"\n【用户提供的资料】\n{p['facts']}\n"
            private = pack.private_facts()
            if private:
                facts_block += f"\n【私有知识库（优先作为事实来源）】\n{private}\n"
            ctx["facts_block"] = facts_block

            system, user = self._render(skill, "write", ctx)
            draft = self.llm.chat_json("write", system, user, ScriptDraft,
                                       on_retry=self._retry_logger(job)).model_dump()

            job.update(state="checking")
            report = check_script(draft["sections"], p["duration"], p["rate"], ban, p["platform"], quota)
            self._step(job, f"check_r{rnd}", f"校验·第 {rnd} 轮", {"report": report})
            if report["passed"] or rnd == rounds:
                draft["check"] = report
                return draft, revisions
            feedback = self._violation_feedback(report)
            revisions.append({"round": rnd, "report": report, "action": "全文回炉"})

    @staticmethod
    def _violation_feedback(report: dict) -> str:
        lines = []
        if report["hard_hits"]:
            words = "、".join(f"「{h['word']}」×{h['count']}" for h in report["hard_hits"])
            lines.append(f"- 命中硬禁用词：{words}。必须删除或替换为合规表述"
                         f"（如『政府补贴』→『符合条件可申请财政补助，以当地政策为准』）")
        if abs(report["deviation_pct"]) > 10:
            lines.append(f"- 时长偏差 {report['deviation_pct']:+.1f}%：目标 {report['duration_target']} 秒，"
                         f"当前约 {report['estimated_seconds']} 秒（{report['chars_total']} 字，"
                         f"配额 {report['target_total']} 字），请按每段配额增删内容")
        for seg in report["segments"]:
            if seg.get("quota") and seg["chars"] > seg["quota"] * 1.3:
                lines.append(f"- 段落超配额：{seg['type']} 段 {seg['chars']} 字（配额≈{seg['quota']}），请压缩")
        return "\n".join(lines) or "未通过校验，请按口语化与合规要求改写"

    @staticmethod
    def _compute_timings(sections: list[dict], rate: float) -> list[dict]:
        """按段落字数推算时间轴；重写/回炉后必须重算。"""
        timings, t = [], 0.0
        for i, s in enumerate(sections):
            n = count_chars(s["text"])
            dur = n / rate + (0.5 if i else 0.0)
            timings.append({"start": round(t, 1), "end": round(t + dur, 1)})
            t += dur
        return timings

    def _finalize(self, job: Job, pack: Pack, p: dict, plan: TopicPlan,
                  draft: dict, revisions: list[dict]) -> dict:
        sections = draft["sections"]
        storyboard = draft["storyboard"]
        if p.get("format") == "voice":
            storyboard = []                  # 仅口播：分镜不进入产物
        timings = self._compute_timings(sections, p["rate"])
        full_text = "\n".join(s["text"] for s in sections)
        placeholders = sorted(set(re.findall(r"\{\{([^}]+)\}\}", full_text)))

        result = {
            "id": job.id, "created_at": datetime.now().isoformat(timespec="seconds"),
            "pack": pack.name, "pack_draft": pack.draft,
            "params": {k: p[k] for k in ("topic", "segment", "audience", "duration", "style",
                                         "platform", "persona", "cta", "mode", "rate", "voice",
                                         "format")},
            "quota": p["quota"],
            "plan": plan.model_dump(),
            "sections": sections,
            "storyboard": storyboard,
            "check": draft["check"],
            "placeholders": placeholders,
            "revisions": revisions,
            "timings": timings,
            "logs": job.steps,
        }
        self._write_output_files(result, full_text)
        return result

    def _write_output_files(self, result: dict, full_text: str) -> None:
        out_dir = self._out_dir(result["created_at"], result["id"])
        (out_dir / "result.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")

        p = result["params"]
        lines = [f"# 口播脚本：{p['topic']}",
                 f"- 行业包：{result['pack']}{'（草稿）' if result['pack_draft'] else ''}"
                 f"　细分：{p['segment']}　受众：{p['audience']}",
                 f"- {int(p['duration'])}s / {p['platform']} / {p['style']} / {p['persona']}",
                 "", "## 口播文案", ""]
        names = {"hook": "开场钩子", "cta": "结尾引导"}
        idx = 0
        for s, tm in zip(result["sections"], result["timings"]):
            if s["type"] == "point":
                idx += 1
                label = f"要点{idx}"
            else:
                label = names[s["type"]]
            lines.append(f"**【{label}】** {tm['start']}-{tm['end']} 秒 · {count_chars(s['text'])} 字")
            lines.append(s["text"])
            lines.append("")
        ch = result["check"]
        lines += ["---",
                  f"字数 {ch['chars_total']}/{ch.get('target_total') or '-'} 字 · "
                  f"预估 {ch['estimated_seconds']}s · 偏差 {ch['deviation_pct']:+.1f}% · "
                  f"{'✅ 合格' if ch['passed'] else '❌ ' + '；'.join(ch['blockers'])}", ""]
        if result["placeholders"]:
            lines += ["> 含占位事实：" + "、".join(result["placeholders"]) + "，请补充后再发布。", ""]
        if result["storyboard"]:
            lines += ["## 分镜表", "", "| 时间 | 画面/景别 | 口播 | 字幕 | 音效 | 提示 |", "|---|---|---|---|---|---|"]
            for shot, tm in zip(result["storyboard"], result["timings"]):
                t = shot.get("time") or f"{tm['start']}-{tm['end']}s"
                lines.append(f"| {t} | {shot.get('shot', '')} | {shot.get('voiceover', '')} "
                             f"| {shot.get('subtitle', '')} | {shot.get('sfx', '')} | {shot.get('note', '')} |")
            lines.append("")
        lines += ["", "## 合规检查", "",
                  f"- 硬禁用词：{ch['hard_hits'] or '无'}",
                  f"- 待确认：{ch['soft_hits'] or '无'}（语境正常即可放行）"]
        (out_dir / "脚本.md").write_text("\n".join(lines), encoding="utf-8")

    def _run_rewrite_segment(self, job: Job, pack: Pack, index: int, feedback: str) -> None:
        try:
            skill = pack.skill() or {}
            result = job.result
            p = result["params"]
            sections = result["sections"]
            seg = sections[index]
            q = result["quota"]
            n_points = max(sum(1 for s in sections if s["type"] == "point"), 1)
            seg_quota = {"hook": q["hook"], "point": q["body"] // n_points, "cta": q["cta"]}[seg["type"]]
            ctx = {
                "industry": pack.info.display_name,
                "context": "\n".join(f"[{s['type']}] {s['text'][:40]}…"
                                     for i, s in enumerate(sections) if i != index),
                "seg_type": seg["type"], "seg_text": seg["text"],
                "seg_quota": str(seg_quota), "seg_feedback": feedback or "按合规与口语化要求优化",
            }
            system, user = self._render(skill, "rewrite_segment", ctx)
            new = self.llm.chat_json("rewrite_segment", system, user, SegmentRewrite,
                                     on_retry=self._retry_logger(job))
            old = seg["text"]
            sections[index]["text"] = new.text
            if new.subtitle:
                sections[index]["subtitle"] = new.subtitle
            ban = Banwords(pack.banwords_data())
            quota = Quota.from_pack(pack.data)
            report = check_script(sections, p["duration"], p["rate"], ban, p["platform"], quota)
            result["check"] = report
            result["timings"] = self._compute_timings(sections, p["rate"])   # 重算时间轴
            result["revisions"].append({"segment": index, "from": old,
                                        "to": new.text, "feedback": feedback})
            result["logs"] = job.steps                                        # 日志同步
            self._step(job, "rewrite_segment", f"重写第 {index + 1} 段", {"report": report})
            job.update(state="done", result=result)
            self._write_output_files(result, "\n".join(s["text"] for s in sections))
        except Exception as e:  # noqa: BLE001
            job.update(state="failed", error=str(e))

    # ── 杂项 ────────────────────────────────────────────────
    def _retry_logger(self, job: Job):
        """接口自动重试时记录到作业日志（界面「日志」页签可见）"""
        def cb(note: str, attempt: int, total: int):
            self._step(job, "retry", f"接口自动重试·第 {attempt}/{total} 次", {"note": note})
        return cb

    def _step(self, job: Job, key: str, title: str, data: dict):
        job.steps.append({"key": key, "title": title, "data": data,
                          "ts": datetime.now().isoformat(timespec="seconds")})

    def _spawn(self, job: Job, fn):
        threading.Thread(target=fn, daemon=True).start()

    def _out_dir(self, created_at: str, jid: str) -> Path:
        d = self.root / "generated" / created_at[:10].replace("-", "") / jid
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _persist(self, job: Job):
        snap = job.snapshot()
        try:
            out_dir = self._out_dir(datetime.now().isoformat(timespec="seconds"), job.id)
            (out_dir / "job.json").write_text(
                json.dumps(snap, ensure_ascii=False, indent=1), encoding="utf-8")
        except Exception:
            pass

    def _persist_result(self, result: dict):
        try:
            out_dir = self._out_dir(result["created_at"], result["id"])
            (out_dir / "result.json").write_text(
                json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
        except Exception:
            pass


def wait_job(pipeline: Pipeline, jid: str, timeout: float = 120.0) -> dict:
    """测试辅助：等待作业结束"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        snap = pipeline.jobs[jid].snapshot()
        if snap["state"] in ("done", "failed"):
            return snap
        time.sleep(0.2)
    raise TimeoutError(jid)
