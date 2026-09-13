# -*- coding: utf-8 -*-
"""流水线编排：参数归一 → 选题策划 → 文案撰写 → 校验回炉 → 组装落盘

生成方法（提示词、注入文件、回炉上限）不在代码里，而是每个行业包自带的 skill.yaml
——知识库管"写什么"，技能管"怎么写"，两者都随包配置、随包分发。

本模块现在**只做编排**。职责被拆到：
  - `jobs.py`     作业与状态机（并发正确性的核心）
  - `store.py`    产物落盘与历史索引（「作业目录在哪」的唯一来源）
  - `prompts.py`  提示词渲染（skill.yaml 模板 + 知识文件注入）
  - `security.py` 本地访问控制

状态机与合法迁移见 jobs.TRANSITIONS。

「停止」的正确性
----------------
Python 没法强杀线程，取消只能协作式：把检查点放在每个耗钱耗时的动作**之前**，
线程自己走出来。关键不变量是：
    **result.json 存在 ⟺ 作业状态为 done**
做法是先做状态迁移、迁移成功才落盘 —— 修复前是「先落盘再改状态」，
于是取消恰好落在「写盘完成、状态未改」的窗口时，磁盘上多出一条已完成记录，
而内存里这条作业是 cancelled，历史列表里它就凭空「复活」了。
"""
from __future__ import annotations

import json
import logging
import re
import threading
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel

from .checker import Banwords, Quota, check_script, count_chars
from .config import AppConfig, load_config
from .jobs import (TERMINAL_STATES, Job, JobCancelled, JobRegistry,  # noqa: F401
                   StateConflict, new_job_id)
from .knowledge import Pack, PackError  # noqa: F401
from .llm import LLMClient
from .prompts import PromptRenderer
from .schemas import (ConfirmRequest, GenerateRequest, RewriteSegmentRequest,
                      ScriptResult, ScriptSection, StoryboardShot, TopicPlan)
from .store import ArtifactStore


class ScriptDraft(BaseModel):
    """模型在「撰写」阶段要返回的结构。"""
    sections: list[ScriptSection]
    storyboard: list[StoryboardShot]


class SegmentRewrite(BaseModel):
    """模型在「单段重写」阶段要返回的结构。"""
    text: str
    subtitle: str = ""


log = logging.getLogger(__name__)

# 同时进行的作业上限。修复前没有任何限制：一个（本机）脚本可以无限调
# /api/generate，每个作业开一个线程、各自烧 token，直到内存和额度一起见底。
MAX_CONCURRENT_JOBS = 4


class Pipeline:
    def __init__(self, root: Path, cfg: AppConfig, data_dir: Path | None = None):
        self.root = root
        self.cfg = cfg
        self.mock = getattr(cfg, "mock", False)
        # data_dir：可写目录。开发态 = 项目根；打包后 = 用户数据目录
        # （安装目录通常不可写，配置与产物都不能往那儿放）。
        self.data_dir = data_dir or root
        self.registry = JobRegistry()
        self.store = ArtifactStore(root, data_dir=self.data_dir)
        self._llm_lock = threading.Lock()
        self._llm: LLMClient | None = None

    # ── 兼容旧调用点的薄封装 ────────────────────────────────
    @property
    def jobs(self) -> dict:
        """只读视图（供旧测试/调试）。真正写入请用 registry.add()。"""
        return {s["id"]: s for s in self.registry.snapshots()}

    def add_job(self, job: Job) -> None:
        self.registry.add(job)

    def get_job(self, jid: str) -> Job:
        return self.registry.get(jid)

    def snapshot_jobs(self, *, include_result: bool = False) -> list[dict]:
        return self.registry.snapshots(include_result=include_result)

    # ── LLM 客户端 ──────────────────────────────────────────
    @property
    def llm(self) -> LLMClient:
        """惰性构建 + 缓存。

        修复前是 `self.llm = self.build_llm()`：每次生成都整体替换实例，
        而多个作业是并发跑的 —— A 作业跑到一半，B 作业把 self.llm 换掉，
        A 后续的请求就换了配置（用户若在此时改了 Key，A 会中途换 Key 继续烧）。
        现在只在缺失时构建，并且走锁；要刷新配置显式调 `reload_llm()`。
        """
        with self._llm_lock:
            if self._llm is None:
                self._llm = self._build_llm()
            return self._llm

    @llm.setter
    def llm(self, client) -> None:
        with self._llm_lock:
            self._llm = client

    def _build_llm(self) -> LLMClient:
        # 现读：设置里改了 Key/模型立即生效（配置文件在 data_dir 下）
        cfg = load_config(self.root, self.data_dir)
        return LLMClient(cfg.llm, mock=self.mock)

    def reload_llm(self) -> LLMClient:
        with self._llm_lock:
            self._llm = self._build_llm()
            return self._llm

    # ── 对外接口 ────────────────────────────────────────────
    def start_generate(self, req: GenerateRequest) -> str:
        if self.registry.running_count() >= MAX_CONCURRENT_JOBS:
            raise StateConflict(
                f"同时进行的生成已达上限（{MAX_CONCURRENT_JOBS} 个），请等其中一条完成后再试")
        pack = Pack(self.root, req.pack)          # 包不存在 → PackError（HTTP 层转 404）
        jid = new_job_id()
        job = Job(jid, "generate", req.model_dump())
        job.work_dir = self.store.job_dir(jid, job.created_at)
        self.registry.add(job)
        self._spawn(job, lambda: self._run_generate(job, pack))
        return jid

    def confirm(self, jid: str, req: ConfirmRequest) -> dict:
        job = self.get_job(jid)
        plan = TopicPlan.model_validate(req.plan)
        pack = Pack(self.root, job.params["pack"])
        skill = pack.skill()
        if not skill:
            raise ValueError(f"行业包缺少 skill.yaml：{pack.name}")
        # 原子迁移：检查「当前是待确认」与置位「writing」在同一把锁内完成。
        # 修复前是先无锁读 job.state 判断、再 update 置位 —— 连点两次「继续」
        # 两个请求都会通过检查，起两个写线程，双倍 token 且结果互相覆盖。
        job.transition_or_raise("writing", error=None)
        self._spawn(job, lambda: self._continue_write(job, pack, skill, plan))
        return job.snapshot()

    def rewrite_segment(self, jid: str, req: RewriteSegmentRequest) -> dict:
        job = self.get_job(jid)
        result = job.result
        if not result or not (0 <= req.index < len(result.get("sections", []))):
            raise ValueError("段落序号无效")
        pack = Pack(self.root, result["pack"])
        # 同上：原子迁移到 rewriting，避免并发发起多次单段重写同时改同一份 sections
        job.transition_or_raise("rewriting", error=None)
        self._spawn(job, lambda: self._run_rewrite_segment(
            job, pack, req.index, req.feedback or ""))
        return job.snapshot()

    def cancel(self, jid: str) -> dict:
        """取消作业：排队/待确认立即落终态，运行中则置标志让线程尽早自行收工。"""
        job = self.get_job(jid)
        if job.state in TERMINAL_STATES:
            return job.snapshot()          # 已结束：幂等返回，不再报错
        job.request_cancel()
        self._persist(job)
        self.registry.prune()
        return job.snapshot()

    def discard(self, jid: str) -> str:
        """删除记录，并让还在跑的作业立刻收工。

        只删目录是不够的：后台线程迟早会走到 write_result，把目录重新建出来，
        用户刚删掉的记录当场复活（墓碑只负责拦住写入，让它继续烧 token 也是浪费）。
        """
        job = self.registry.find(jid)
        if job is not None:
            job.request_cancel()
            self.registry.remove(jid)
        return self.store.delete(jid)

    # ── 生成主流程 ──────────────────────────────────────────
    def _run_generate(self, job: Job, pack: Pack):
        try:
            self._abort_if_cancelled(job)
            skill = pack.skill()
            if not skill:
                raise ValueError(f"行业包缺少 skill.yaml（生成技能定义）：{pack.name}")
            p = self._normalize(pack, job.params)
            job.transition_or_raise("selecting", params=p)
            plan = self._select(job, pack, skill, p)
            self._step(job, "select", "选题策划", {"plan": plan.model_dump()})
            self._abort_if_cancelled(job)
            if p.get("mode") == "step":
                if job.transition("paused_awaiting_confirmation",
                                  result={"plan": plan.model_dump()}):
                    self._persist(job)
                return
            self._continue_write(job, pack, skill, plan)
        except JobCancelled:
            pass                              # 已由 request_cancel 置 cancelled，别再写回
        except Exception as e:  # noqa: BLE001
            self._fail(job, e)

    def _continue_write(self, job: Job, pack: Pack, skill: dict, plan: TopicPlan):
        try:
            self._abort_if_cancelled(job)
            p = job.params
            draft, revisions = self._write_with_recheck(job, pack, skill, p, plan)
            self._finalize(job, pack, p, plan, draft, revisions)
        except JobCancelled:
            pass                              # 已由 request_cancel 置 cancelled，别再写回
        except Exception as e:  # noqa: BLE001
            self._fail(job, e)

    # ── 节点实现 ────────────────────────────────────────────
    def _normalize(self, pack: Pack, params: dict) -> dict:
        def pick(key, fallback=None):
            v = params.get(key)
            return v if v not in (None, "", []) else pack.param_default(key, fallback)

        duration = float(pick("duration", 60))
        style = str(pick("style", ""))
        rate = params.get("rate") or pack.rate_for_style(style)
        quota = Quota.from_pack(pack.data)
        points = pack.points_limit(duration)
        target = quota.target(duration, float(rate)) if quota.available else {}
        degraded = False
        if not target:
            # 行业包没写 quota_table 时的降级：按 时长×语速 估一个总量再分段。
            # 修复前这种情况会以「list index out of range」直接失败，用户
            # 完全看不出是包配置缺了字段。
            total = max(round(duration * float(rate) * 0.95), 20)
            target = {"total": total, "hook": round(total * 0.15),
                      "body": round(total * 0.65), "cta": round(total * 0.20)}
            degraded = True
        out = {
            "pack": pack.name, "topic": str(params.get("topic", "")).strip(),
            "segment": pick("segment"), "audience": pick("audience"),
            "duration": duration, "style": style,
            "platform": pick("platform"), "persona": pick("persona"),
            "cta": pick("cta"), "facts": params.get("facts") or "",
            "mode": params.get("mode", "auto"), "rate": float(rate),
            "voice": params.get("voice") if params.get("voice") in ("strong", "standard", "off") else "strong",
            "format": params.get("format") if params.get("format") in ("both", "voice") else "both",
            "reroll": bool(params.get("reroll")),
            "points": points,
            "quota": target,
            "quota_degraded": degraded,
        }
        if not out["topic"]:
            raise ValueError("主题不能为空")
        return out

    def _render_stage(self, job: Job, pr: PromptRenderer, stage: str,
                      ctx: dict) -> tuple[str, str]:
        """渲染 + 校验占位符。

        `safe_substitute` 对未知变量保持原样，拼错的占位符不会报错，
        只会让模型收到一段字面量、并静默丢掉本该注入的知识文件。
        这里把残留占位符记进作业日志（界面「日志」页签），包作者一眼能看到。
        """
        missing = pr.unfilled(stage, ctx)
        system, user = pr.render(stage, ctx)
        if missing:
            self._step(job, f"tpl_{stage}", f"模板占位符未填充（{stage}）",
                       {"missing": missing,
                        "hint": "skill.yaml 的 stages.<阶段>.files 的 key 必须与占位符同名"})
        return system, user

    def _select(self, job: Job, pack: Pack, skill: dict, p: dict) -> TopicPlan:
        pr = PromptRenderer(pack)
        ctx = pr.select_ctx(p)
        system, user = self._render_stage(job, pr, "select", ctx)
        return self.llm.chat_json("select", system, user, TopicPlan,
                                  on_retry=self._retry_logger(job),
                                  on_delta=self._delta_handler(job, "选题策划"))

    def _write_with_recheck(self, job: Job, pack: Pack, skill: dict,
                            p: dict, plan: TopicPlan) -> tuple[dict, list[dict]]:
        ban = Banwords(pack.banwords_data())
        quota = Quota.from_pack(pack.data)
        rounds = 1 + int((skill.get("limits") or {}).get("recheck_rounds", 2))
        pr = PromptRenderer(pack)
        revisions: list[dict] = []
        feedback = ""
        draft: dict = {}
        for rnd in range(1, rounds + 1):
            self._abort_if_cancelled(job)     # 回炉每一轮前先看有没有让停
            job.transition("writing" if rnd == 1 else "rewriting")

            ctx = pr.write_ctx(p, plan.model_dump(), feedback)
            system, user = self._render_stage(job, pr, "write", ctx)
            # 「换一版」：同主题同参数重掷，小幅提温换取不同表达（仍受校验约束）
            temp = None
            if p.get("reroll"):
                temp = min(1.0, float(self.llm.cfg.temperature) + 0.25)
            draft = self.llm.chat_json(
                "write", system, user, ScriptDraft,
                on_retry=self._retry_logger(job), temperature=temp,
                on_delta=self._delta_handler(
                    job, "回炉改写" if rnd > 1 else "文案撰写")).model_dump()
            # 步骤必须在阶段「完成后」再记：界面把 steps 一律渲染为已完成，
            # 若在开始前就记录，会出现「已完成的文案撰写」与「文案撰写中」并存。
            self._step(job, f"write_r{rnd}", "文案撰写" if rnd == 1 else f"回炉改写·第 {rnd - 1} 轮",
                       {"feedback": feedback} if feedback else {})

            job.transition("checking")
            report = check_script(draft["sections"], p["duration"], p["rate"], ban,
                                  p["platform"], quota)
            self._step(job, f"check_r{rnd}", f"校验·第 {rnd} 轮", {"report": report})
            if report["passed"] or rnd == rounds:
                draft["check"] = report
                return draft, revisions
            feedback = self._violation_feedback(report)
            revisions.append({"round": rnd, "report": report, "action": "全文回炉"})
        return draft, revisions

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
        # 段落配额现在按要点数均分（见 checker.check_script），这条反馈才真正会触发；
        # 修复前每段拿到的是「整个正文配额」，阈值高到几乎不可能命中，是死代码。
        for seg in report["segments"]:
            q = seg.get("quota")
            if q and seg["chars"] > q * 1.3:
                lines.append(f"- 段落超配额：{seg['type']} 段 {seg['chars']} 字"
                             f"（配额≈{q}），请压缩")
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

    @staticmethod
    def _build_scenes(sections: list[dict], storyboard: list[dict],
                      timings: list[dict]) -> list[dict]:
        """组装统一产物「场景序列 Scene[]」。

        投影关系：口播 = narration · 分镜 = visual.prompt / audio.sfx
        契约见 docs/场景序列契约.md；与 sections/storyboard/timings 并行输出。
        """
        scenes = []
        for i, (s, tm) in enumerate(zip(sections, timings)):
            shot = storyboard[i] if i < len(storyboard) else {}
            scenes.append({
                "scene_id": f"s{i + 1}",
                "type": s["type"],
                "start": tm["start"],
                "end": tm["end"],
                "narration": s["text"],
                "subtitle": s.get("subtitle", "") or "",
                "visual": {
                    "prompt": shot.get("shot", ""),
                    "source": "generated",
                    "transition": shot.get("transition", "cut"),
                },
                "audio": {
                    "bgm": shot.get("bgm", ""),
                    "sfx": shot.get("sfx", ""),
                },
                "shot_type": shot.get("shot_type", ""),
                "style": shot.get("style", ""),
            })
        return scenes

    def _finalize(self, job: Job, pack: Pack, p: dict, plan: TopicPlan,
                  draft: dict, revisions: list[dict]) -> None:
        self._abort_if_cancelled(job)          # 关键检查点：组装前再确认一次
        sections = draft["sections"]
        storyboard = draft["storyboard"]
        if p.get("format") == "voice":
            storyboard = []                    # 仅口播：分镜不进入产物
        timings = self._compute_timings(sections, p["rate"])
        scenes = self._build_scenes(sections, storyboard, timings)
        full_text = "\n".join(s["text"] for s in sections)
        placeholders = sorted(set(re.findall(r"\{\{([^}]+)\}\}", full_text)))

        raw = {
            "id": job.id, "created_at": job.created_at,
            "pack": pack.name, "pack_draft": pack.draft,
            "params": {k: p[k] for k in ("topic", "segment", "audience", "duration", "style",
                                         "platform", "persona", "cta", "mode", "rate", "voice",
                                         "format")},
            "quota": p["quota"],
            "plan": plan.model_dump(),
            "sections": sections,
            "storyboard": storyboard,
            "scenes": scenes,
            "check": draft["check"],
            "placeholders": placeholders,
            "revisions": revisions,
            "timings": timings,
            "logs": job.steps,
        }
        # 契约校验：修复前 ScriptResult / SceneItem 定义了却从未实例化，
        # docs/场景序列契约.md 的约定没有任何代码强制，字段漂移无人发现。
        result = ScriptResult.model_validate(raw).model_dump()

        # 先落盘、后置 done —— 顺序一旦反了，不变量就是破的。
        #
        # 修复前是「先 transition('done') 再 write_result」：状态一变，轮询的界面
        # 与删除接口就认为作业结束了，而此刻产物还有一半没写（历史索引甚至尚未落盘）。
        # 实测的两个后果：
        #   - 前端一看到 done 就回读记录 → 索引还没写，记录凭空消失；
        #   - 此刻调用删除 → 目录删掉后 write_atomic 的 mkdir(parents=True)
        #     又把它建回来，只写了一半的产物留在磁盘上，记录当场复活。
        # 现在倒过来：产物完整落盘之后状态才允许变成 done，
        # 「done ⟹ 磁盘上已有完整产物」这才真正成立。
        self._abort_if_cancelled(job)
        try:
            if not self.store.write_result(result, job.work_dir):
                # 落盘前记录已被删除（用户在生成途中删了这条）—— 就此收手。
                # 继续走下去会变成「done 但磁盘上没有产物」的空壳记录。
                return
        except Exception as e:  # noqa: BLE001
            # 落盘失败必须让状态跟着变成失败，否则内存说「完成」而磁盘上没有产物，
            # 用户点历史回看只会得到一个 404。
            job.transition("failed", force=True, error=f"产物落盘失败：{e}")
            raise
        if not job.transition("done", result=result):
            # 落盘期间用户按了停止：产物刚写进去，但作业已取消 —— 撤掉，
            # 否则历史里会留下一条「已停止却带着完整产物」的幽灵记录。
            self.store.delete(job.id)
            return
        self.registry.prune()

    def _run_rewrite_segment(self, job: Job, pack: Pack, index: int, feedback: str) -> None:
        try:
            self._abort_if_cancelled(job)
            result = job.result
            p = result["params"]
            # 深拷贝一份再改：修复前直接原地改 job.result["sections"]，
            # 一旦中途取消，内存里的结果已经是半改状态（产物文件倒是旧的）。
            sections = json.loads(json.dumps(result["sections"], ensure_ascii=False))
            seg = sections[index]
            q = result["quota"]
            n_points = max(sum(1 for s in sections if s["type"] == "point"), 1)
            seg_quota = {"hook": q.get("hook", 0), "point": q.get("body", 0) // n_points,
                         "cta": q.get("cta", 0)}[seg["type"]]
            pr = PromptRenderer(pack)
            ctx = pr.rewrite_ctx(sections, index, seg_quota, feedback)
            system, user = self._render_stage(job, pr, "rewrite_segment", ctx)
            new = self.llm.chat_json("rewrite_segment", system, user, SegmentRewrite,
                                     on_retry=self._retry_logger(job),
                                     on_delta=self._delta_handler(job, "单段重写"))
            old = seg["text"]
            sections[index] = {**seg, "text": new.text,
                               **({"subtitle": new.subtitle} if new.subtitle else {})}
            ban = Banwords(pack.banwords_data())
            quota = Quota.from_pack(pack.data)
            report = check_script(sections, p["duration"], p["rate"], ban, p["platform"], quota)
            timings = self._compute_timings(sections, p["rate"])
            self._step(job, "rewrite_segment", f"重写第 {index + 1} 段", {"report": report})

            updated = {
                **result,
                "sections": sections,
                "check": report,
                "timings": timings,
                "scenes": self._build_scenes(sections, result["storyboard"], timings),
                "revisions": list(result.get("revisions", [])) + [
                    {"segment": index, "from": old, "to": new.text, "feedback": feedback}],
                "logs": job.steps,
            }
            updated = ScriptResult.model_validate(updated).model_dump()

            self._abort_if_cancelled(job)
            # 与 _finalize 同样的顺序：先落盘、后置 done。
            if not self.store.write_result(updated, job.work_dir):
                return          # 记录已被删除，不必再写
            if not job.transition("done", result=updated):
                # 落盘期间被停止：把重写前的内容写回去。这里不能像 _finalize 那样
                # 直接删产物 —— 那是本次新建的；而重写是在既有记录上改，
                # 删掉会把用户原来那条记录一起抹掉。
                self.store.write_result(result, job.work_dir)
                return
        except JobCancelled:
            # 重写被中止：内存里的结果已经被丢弃（上面做了深拷贝），产物文件没动过，
            # 服务端状态留在 cancelled，用户下次打开仍是重写前的内容。
            job.update(error=None)
        except Exception as e:  # noqa: BLE001
            self._fail(job, e)

    # ── 杂项 ────────────────────────────────────────────────
    def _fail(self, job: Job, e: Exception) -> None:
        """把作业落到 failed。

        **已取消的作业不在这里改写状态。** 取消之后线程可能还会因为别的原因抛错
        （比如流式连接被我们主动掐断，httpx 抛出 RequestError），
        如果无条件 force 成 failed，「用户点了停止、界面却显示生成失败」这个
        假停止的老问题就会回来。取消不是错误，日志里留一条就够了。
        """
        if job.is_cancelled():
            log.info("作业 %s 在取消后抛错（已忽略，状态保持 cancelled）：%s", job.id, e)
            return
        job.transition("failed", force=True, error=_readable_error(e))
        self._persist(job)
        self.registry.prune()          # 回收早期终态作业，避免长跑进程内存无界增长

    def _retry_logger(self, job: Job):
        """接口自动重试时记录到作业日志（界面「日志」页签可见）"""
        def cb(note: str, attempt: int, total: int):
            self._step(job, "retry", f"接口自动重试·第 {attempt}/{total} 次", {"note": note})
        return cb

    @staticmethod
    def _delta_handler(job: Job, phase: str):
        """返回流式回调：把模型增量写进 job，界面据此实时显示思考过程。

        每次调用都新建一个，首次收到增量时才 begin_stream —— 这样每进入一个
        阶段（含每次重试）都会清空缓冲，上一阶段/上一轮的思考不会串过来。
        """
        state = {"started": False}

        def cb(kind: str, text: str):
            # 停止要在「收包过程中」就生效：等整段创建完再取消，钱已经花完了。
            # 抛出去会顺着 httpx 的流式迭代一路冒到工作线程的 except JobCancelled。
            if job.is_cancelled():
                raise JobCancelled()
            if not state["started"]:
                job.begin_stream(phase)
                state["started"] = True
            job.push_delta(kind, text)

        return cb

    def _step(self, job: Job, key: str, title: str, data: dict):
        job.steps.append({"key": key, "title": title, "data": data,
                          "ts": datetime.now().isoformat(timespec="seconds")})

    @staticmethod
    def _abort_if_cancelled(job: Job) -> None:
        """阶段之间的取消检查点：已停止就抛 JobCancelled 让线程收工。"""
        if job.is_cancelled():
            raise JobCancelled()

    def _spawn(self, job: Job, fn):
        threading.Thread(target=fn, daemon=True).start()

    def _persist(self, job: Job):
        """落盘作业快照。失败不阻断流程，但必须留痕。

        修复前这里是 `except Exception: pass` —— 磁盘满或权限不足时，
        job.json 静默不写，用户看不到任何迹象，事后也查不到原因。
        """
        try:
            self.store.write_job(job.snapshot(), job.work_dir)
        except Exception as e:                  # noqa: BLE001
            log.warning("作业 %s 快照落盘失败：%s", job.id, e)


def _readable_error(e: Exception) -> str:
    """把异常转成用户能看懂的一句话。

    修复前直接把 str(e) 丢给界面：缺 quota_table 的包会让用户看到
    「list index out of range」，完全无从下手。
    """
    msg = str(e).strip() or type(e).__name__
    if isinstance(e, IndexError):
        return f"行业包配置不完整（{msg}）：请检查 pack.yaml 的 quota_table / params 字段"
    if isinstance(e, KeyError):
        return f"行业包配置缺少字段：{msg}"
    return msg


def wait_job(pipeline: Pipeline, jid: str, timeout: float = 120.0) -> dict:
    """测试辅助：等待作业结束（含取消）。"""
    import time
    deadline = time.time() + timeout
    while time.time() < deadline:
        snap = pipeline.get_job(jid).snapshot()
        if snap["state"] in TERMINAL_STATES:
            return snap
        time.sleep(0.2)
    raise TimeoutError(jid)
