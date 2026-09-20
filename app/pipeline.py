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
from .schemas import (GenerateRequest, RewriteSegmentRequest,
                      ScriptResult, ScriptSection, StoryboardShot, TopicPlan)
from .store import ArtifactStore


class ScriptDraft(BaseModel):
    """模型在「撰写」阶段要返回的结构。"""
    sections: list[ScriptSection]
    storyboard: list[StoryboardShot]


def check_contract_complete(raw: dict) -> None:
    """`raw` 与 `ScriptResult` 契约的字段集合必须**完全一致**。

    为什么不能只靠 `model_validate`：它卡的是「类型对不对」，不是「字段有没有」。
      - **少**给一个字段 → 静默取默认值。`quota_degraded` 就是这么丢的：
        `_finalize` 的白名单里没有它，于是「配额是估的」这个信号在落盘那一步
        无声消失，而契约层一声不吭。
      - **多**给一个字段 → 静默丢弃（Pydantic 默认忽略未声明的键）。
    两头都是「信号掉了但没人知道」—— 正是本项目一直在整治的静默降级。

    用显式比对而不是 `model_config = ConfigDict(extra="forbid")`：
    后者只堵「多」这一头，而且会一并改变 `_run_rewrite_segment` 里那次
    「读旧产物再校验」的语义 —— 升级路径上的宽容度不该被顺手收掉。
    """
    declared = set(ScriptResult.model_fields)
    if set(raw) != declared:
        raise ValueError(
            "产物字段与 ScriptResult 契约不一致 —— "
            f"契约有但 raw 没给：{sorted(declared - set(raw))}；"
            f"raw 给了但契约没声明：{sorted(set(raw) - declared)}")


class SegmentRewrite(BaseModel):
    """模型在「单段重写」阶段要返回的结构。"""
    text: str
    subtitle: str = ""


log = logging.getLogger(__name__)

# 同时进行的作业上限。修复前没有任何限制：一个（本机）脚本可以无限调
# /api/generate，每个作业开一个线程、各自烧 token，直到内存和额度一起见底。
MAX_CONCURRENT_JOBS = 4

# ── `_normalize` 的产出，每个键都必须有着落 ──────────────────────
#
# `_finalize` 曾经用一份手写白名单从 `p` 里挑参数落盘，而 `quota_degraded`
# 不在其中 —— 于是「行业包没配 quota_table」这个信号被算出来、存进内存，
# 然后在落盘那一刻**无声地掉了**：`result.json` 里一个字都没有，
# 而 `quota: {total: 256, ...}` 与真配额长得一模一样。
# 更要命的是 `ScriptResult.model_validate(raw)` 对**缺失**字段只会取默认值，
# 不会报错，所以契约层也拦不住。
#
# 下面三张表把「每个键去哪」写成显式声明。**新增键时
# `tests/test_quota_degraded_signal.py::test_normalize_keys_are_all_accounted_for`
# 会报红**，逼作者表态 —— 而不是靠记得去改那份白名单。
#
# 1) 落进 result.json 的 `params`
PERSISTED_PARAMS: tuple[str, ...] = (
    "topic", "segment", "audience", "duration", "style",
    "platform", "persona", "cta", "rate", "voice", "format",
    # P3-14：当时用的模型与预算 —— 事后要解释「这条为什么慢 / 为什么这样」
    # 只能靠这些落盘值，不能靠「现在的 config.yaml」（它今天就被改过两次）。
    "model", "max_tokens", "temperature",
)
# 2) 不进 params，但在 result.json 顶层另有落点
PARAMS_ELSEWHERE: dict[str, str] = {
    "pack": "raw['pack']",
    "quota": "raw['quota']",
    "quota_degraded": "raw['quota_degraded']",
}
# 3) 显式不持久化，附理由
PARAMS_DROPPED: dict[str, str] = {
    "facts": "用户临时粘贴的资料，可能很长/含私密内容，不属于产物属性",
    "reroll": "本次「换一版」的开关，不属于产物属性",
    "points": "已由提示词占位符 $points 消费；产物里的要点数看 len(plan.points)",
}
# 4) 运行期由 `_run_generate` 附加（P3-14），**不进 `_normalize`** ——
#    `_normalize` 是纯参数归一，不读 self.llm（测试直接调它时会没有客户端）。
#    它们最终仍落进 result.json 的 params（PERSISTED_PARAMS 已含）。
RUNTIME_PARAMS: tuple[str, ...] = ("model", "max_tokens", "temperature")


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
        """只读视图（供旧测试/调试）。真正写入请用 registry.add_if_room()。"""
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
        # 顺序：**先验包、再占额度**。
        #
        # 包不存在是永久性错误（404），比「额度满了」（409，可重试）更该先报；
        # 而且反过来写会留一条漏额度的路径 —— 若先占额度再验包，PackError 抛出去
        # 时那条 `queued` 作业已经插进注册表，没人回收它，额度被永久吃掉一个。
        # （这正是 P0-3 的形态：额度被没人管的作业占住。）
        pack = Pack(self.root, req.pack)
        jid = new_job_id()
        job = Job(jid, "generate", req.model_dump())
        job.work_dir = self.store.job_dir(jid, job.created_at)
        # 检查与插入在同一把锁内（add_if_room）。修复前是
        # `running_count() >= MAX` 判断之后另一次加锁 `add()` ——
        # 两次加锁之间有窗口，而 /api/generate 是同步 def（线程池真并发），
        # 两个请求能同时通过检查、双双插入，额度只保证「通常有效」。
        if not self.registry.add_if_room(job, MAX_CONCURRENT_JOBS):
            raise StateConflict(
                f"同时进行的生成已达上限（{MAX_CONCURRENT_JOBS} 个），请等其中一条完成后再试")
        self._spawn(job, lambda: self._run_generate(job, pack))
        return jid

    def rewrite_segment(self, jid: str, req: RewriteSegmentRequest) -> dict:
        job = self.get_job(jid)
        result = job.result
        if not result or not (0 <= req.index < len(result.get("sections", []))):
            raise ValueError("段落序号无效")
        pack = Pack(self.root, result["pack"])
        # 同上：原子迁移到 rewriting，避免并发发起多次单段重写同时改同一份 sections
        job.transition_or_raise("rewriting", error=None)
        # P1-6：单段重写也把 client 抓成局部变量（与生成主流程同口径）。
        client = self.llm
        self._spawn(job, lambda: self._run_rewrite_segment(
            job, pack, req.index, req.feedback or "", client))
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
            # P1-6：client 在作业开始时一次抓取，后续全程用局部变量 ——
            # 不这样做的话，作业 A 跑到回炉时 B 作业 reload_llm() 会换掉 self.llm
            # （用户在设置里改了 Key/模型），A 中途换模型/换预算继续烧。
            client = self.llm
            # P3-14：产物参数记下「当时用的什么配置」，事后才能解释
            # 「这条为什么慢 / 为什么长这样」—— 之前排查只能靠当前 config 倒推。
            p["model"] = client.cfg.model
            p["max_tokens"] = client.cfg.max_tokens
            p["temperature"] = client.cfg.temperature
            job.transition_or_raise("selecting", params=p)
            plan = self._select(job, pack, skill, p, client)
            self._step(job, "select", "选题策划", {"plan": plan.model_dump()})
            self._abort_if_cancelled(job)
            self._continue_write(job, pack, skill, plan, client)
        except JobCancelled:
            pass                              # 已由 request_cancel 置 cancelled，别再写回
        except Exception as e:  # noqa: BLE001
            self._fail(job, e)

    def _continue_write(self, job: Job, pack: Pack, skill: dict, plan: TopicPlan,
                        client: LLMClient):
        try:
            self._abort_if_cancelled(job)
            p = job.params
            draft, revisions = self._write_with_recheck(job, pack, skill, p, plan, client)
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
            "rate": float(rate),
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

    def _select(self, job: Job, pack: Pack, skill: dict, p: dict,
                client: LLMClient) -> TopicPlan:
        pr = PromptRenderer(pack)
        ctx = pr.select_ctx(p)
        system, user = self._render_stage(job, pr, "select", ctx)
        # P0-3：选题只产出 400~650 字正文 + 少量思考，与 write 共用 16000 全额
        # 预算会让思考量被预算反向推高（主报告 R1）。分阶段预算：select 4000。
        # P1-8：「换一版」的提温原来只作用于 write —— 于是「换一版」重掷出的
        # plan 与上一版几乎一样，白白花 74~173s。select 同样提温。
        temp = (min(1.0, float(client.cfg.temperature) + 0.25)
                if p.get("reroll") else None)
        return client.chat_json("select", system, user, TopicPlan,
                                on_retry=self._retry_logger(job),
                                on_delta=self._delta_handler(job, "选题策划"),
                                max_tokens=4000, temperature=temp)

    def _write_with_recheck(self, job: Job, pack: Pack, skill: dict,
                            p: dict, plan: TopicPlan,
                            client: LLMClient) -> tuple[dict, list[dict]]:
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
                temp = min(1.0, float(client.cfg.temperature) + 0.25)
            draft = client.chat_json(
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
            "mock": self.mock,
            "params": {k: p[k] for k in PERSISTED_PARAMS},
            "quota": p["quota"],
            # 字数配额是「按 时长×语速 估算」而不是查表得来的 —— 必须随产物一起
            # 落盘，否则界面看到的 `quota: {total: 256, ...}` 与包作者真正配过的
            # 配额完全无法区分（P2-8：信号算对了却没人接）。
            # 用 `p[...]` 而不是 `p.get(...)`：键缺失时这里必须炸，
            # 「缺了就当成没降级」正是本项目一直在整治的那种静默。
            "quota_degraded": bool(p["quota_degraded"]),
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
        check_contract_complete(raw)
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

    def _run_rewrite_segment(self, job: Job, pack: Pack, index: int, feedback: str,
                             client: LLMClient) -> None:
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
            # P0-3：单段重写输出一段口播，3000 预算足够，不占 write 的全额预算。
            new = client.chat_json("rewrite_segment", system, user, SegmentRewrite,
                                   on_retry=self._retry_logger(job),
                                   on_delta=self._delta_handler(job, "单段重写"),
                                   max_tokens=3000)
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

        P1-7 修复前「收到第一个增量才 begin_stream」：select 首字节前静默几十秒，
        快照没有 stream 键，思考块被 progress.js 隐藏，界面只剩「已用 N 秒」，
        用户以为卡死。现在**构造时就 begin_stream** —— 阶段一开始思考块就显示
        「等待模型首个 token…」；同时每进入一个阶段（含每次重试）都会清空缓冲，
        上一阶段/上一轮的思考不会串过来（P2-13 的计数污染一并缓解）。
        """
        job.begin_stream(phase)

        def cb(kind: str, text: str):
            # 停止要在「收包过程中」就生效：等整段创建完再取消，钱已经花完了。
            # 抛出去会顺着 httpx 的流式迭代一路冒到工作线程的 except JobCancelled。
            if job.is_cancelled():
                raise JobCancelled()
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
