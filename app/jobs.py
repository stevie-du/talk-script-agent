# -*- coding: utf-8 -*-
"""作业（Job）与作业注册表（JobRegistry）。

从 pipeline.py 里拆出来的两个理由：
  1. 状态机是并发正确性的核心，值得独立成文、独立测试；
  2. pipeline 原来一个类同时管配置、提示词、落盘、状态，646 行里真正的
     「流程编排」不到一半，读的人得先把无关细节筛掉。

状态机
------
    queued → selecting → (paused_awaiting_confirmation)
                       → writing → checking(第N轮) → done / failed / cancelled
    单段重写：done → rewriting → done / failed / cancelled

所有迁移都必须走 `Job.transition()`——**检查与置位在同一把锁内完成**。
修复前的写法是「先无锁读 job.state 判断，再 job.update(state=…)」，
两个并发请求会双双通过检查（TOCTOU）：连点两次「继续」起两个写线程，
双倍 token、steps 重复、result 互相覆盖。
"""
from __future__ import annotations

import threading
import uuid
from datetime import datetime

# 终态：进入后不再流转。同时用于「停止」的幂等判断。
TERMINAL_STATES = frozenset({"done", "failed", "cancelled"})

# 合法迁移表：from_state -> 允许去的 to_state。
# 写成表而不是散落在各方法里的 if，是为了让「谁能到哪儿」一眼可查，
# 也让非法迁移统一变成 409 而不是静默写坏状态。
TRANSITIONS: dict[str, frozenset[str]] = {
    "queued": frozenset({"selecting", "writing", "failed", "cancelled"}),
    "selecting": frozenset({"paused_awaiting_confirmation", "writing", "failed", "cancelled"}),
    "paused_awaiting_confirmation": frozenset({"writing", "failed", "cancelled"}),
    "writing": frozenset({"checking", "rewriting", "done", "failed", "cancelled"}),
    "checking": frozenset({"writing", "rewriting", "done", "failed", "cancelled"}),
    "rewriting": frozenset({"checking", "done", "failed", "cancelled"}),
    "done": frozenset({"rewriting", "cancelled"}),
    "failed": frozenset({"writing", "cancelled"}),      # failed 允许「重试」原作业
    "cancelled": frozenset(),
}


class JobCancelled(Exception):
    """用户按了停止。

    这是控制流而非错误：走了它就不能落到 failed，否则界面显示「生成失败」
    让用户以为是自己配置错了，实则只是他主动停的。
    """


class StateConflict(RuntimeError):
    """并发发起的操作与当前状态冲突（HTTP 层映射为 409）。"""


class Job:
    def __init__(self, jid: str, kind: str, params: dict, work_dir=None):
        self.id = jid
        self.kind = kind                  # generate / packgen
        self.params = params
        self.state = "queued"
        self.steps: list[dict] = []
        self.error: str | None = None
        self.result: dict | None = None
        self.created_at = datetime.now().isoformat(timespec="seconds")
        # 产物目录在创建时一次算定：修复前 _persist 用 now()、result 用 created_at，
        # 跨零点的作业会把 job.json 与 result.json 落进两个日期目录，
        # 而 history_delete 只删第一个命中项 —— 被删的记录会「复活」。
        self.work_dir = work_dir
        self._lock = threading.Lock()
        self.cancel_event = threading.Event()   # 用户点了「停止」→ 后台线程据此尽早收工
        # 流式过程内容：只驻内存、不落盘（重启即弃，属过程态而非产物）
        self.stream_phase = ""            # 当前阶段名，如「文案撰写」
        # 思考文本只保留尾部（界面只展示最后 STREAM_TAIL 字），长度另用计数器累计 ——
        # 见 push_delta 的注释：全量驻留内存没有任何一处会用到。
        self.stream_reasoning = ""
        self.stream_reasoning_len = 0
        self.stream_content_len = 0       # 正文连尾部都不需要，界面只显示字数
        self.started_at = datetime.now().timestamp()

    # ── 状态迁移（唯一的写入口）──────────────────────────────
    def transition(self, to_state: str, *, force: bool = False, **fields) -> bool:
        """原子迁移。返回 True 表示迁移成功；状态不符返回 False。

        force=True 用于「取消」这种必须无条件生效的场景。
        已取消的作业永远拒绝再迁移（否则会出现「停了半秒又被改成 done」的假停止）。
        """
        with self._lock:
            if self.cancel_event.is_set() and not force:
                return False
            if not force:
                allowed = TRANSITIONS.get(self.state, frozenset())
                if to_state not in allowed:
                    return False
            self.state = to_state
            for k, v in fields.items():
                setattr(self, k, v)
            return True

    def transition_or_raise(self, to_state: str, **fields) -> None:
        if not self.transition(to_state, **fields):
            raise StateConflict(f"作业状态为 {self.state}，无法执行该操作")

    def update(self, **kw):
        """写非状态字段（保留旧语义，供步骤/结果等使用）。

        注意：这里**不再**承担状态写入——状态一律走 transition()。
        修复前它会在已取消时静默丢弃 state 键，看似安全，实则让
        「检查通过但置位被吞」变成静默失败，问题更难发现。
        """
        with self._lock:
            for k, v in kw.items():
                if k == "state":
                    raise ValueError("state 必须走 transition()")
                setattr(self, k, v)

    # ── 取消 ────────────────────────────────────────────────
    def is_cancelled(self) -> bool:
        return self.cancel_event.is_set()

    def request_cancel(self) -> None:
        """置取消标志并立即落到 cancelled 终态。

        先落状态是刻意的：后台线程可能正卡在一段 CPU 密集的校验里，等它自己
        走到检查点要一会儿；界面必须立刻响应按钮，不能让用户以为没点上。
        """
        with self._lock:
            self.cancel_event.set()
            self.state = "cancelled"

    # ── 流式缓冲 ────────────────────────────────────────────
    def begin_stream(self, phase: str):
        """进入某个阶段时重置缓冲，避免上一阶段的思考串到下一阶段。"""
        with self._lock:
            self.stream_phase = phase
            self.stream_reasoning = ""
            self.stream_reasoning_len = 0
            self.stream_content_len = 0

    def push_delta(self, kind: str, text: str):
        """累计流式内容。

        文本**只保留会被真正用到的部分**：界面要的只有「思考的最后 1500 字」
        与「累计字数」。修复前两个缓冲区都是无上限 `+=`：
          - 推理型模型的思考动辄上万字、正文草稿几十 KB；
          - 作业结束后缓冲**从不释放**，而注册表要保留 200 个终态作业，
            于是这些内容全部驻留到被 prune 掉为止；
          - 模型若异常持续输出，单个作业就能无上限吃内存。

        长度用计数器单独累计，这样界面显示的「思考 N 字」仍是真实值 ——
        直接截断文本会让字数停在 1500 字，那是**显示错误信息**，比占内存更糟。
        """
        with self._lock:
            if kind == "reasoning":
                self.stream_reasoning_len += len(text)
                self.stream_reasoning = (self.stream_reasoning + text)[-STREAM_TAIL:]
            else:
                self.stream_content_len += len(text)

    # ── 快照 ────────────────────────────────────────────────
    def snapshot(self, *, include_result: bool = True) -> dict:
        """取快照。

        include_result=False 用于轮询：运行期的结果体（sections + scenes +
        logs + check）动辄几十 KB，而前端在作业跑完之前只需要 state / steps /
        stream —— 每 0.9 秒序列化一次整份产物是纯浪费。
        """
        with self._lock:
            snap = {
                "id": self.id, "kind": self.kind, "state": self.state,
                "params": self.params, "steps": self.steps,
                "error": self.error,
                "created_at": self.created_at,
            }
            if include_result or self.state in TERMINAL_STATES:
                snap["result"] = self.result
            if self.stream_reasoning_len or self.stream_content_len:
                snap["stream"] = {
                    "phase": self.stream_phase,
                    "reasoning_tail": self.stream_reasoning[-STREAM_TAIL:],
                    "reasoning_len": self.stream_reasoning_len,
                    "content_len": self.stream_content_len,
                }
            return snap


# 流式思考内容随轮询外传时的截取长度：思考动辄上万字，每次全量传输没必要
STREAM_TAIL = 1500


def new_job_id() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]


class JobRegistry:
    """作业字典的并发安全容器。

    HTTP 层（FastAPI 线程池）会遍历作业，后台生成线程会往里增删改 ——
    两边都不加锁时，一边建作业一边拉历史就会
    「RuntimeError: dictionary changed size during iteration」。
    """

    def __init__(self):
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    def add(self, job: Job) -> None:
        with self._lock:
            self._jobs[job.id] = job

    def get(self, jid: str) -> Job:
        with self._lock:
            job = self._jobs.get(jid)
        if job is None:
            raise KeyError(jid)
        return job

    def find(self, jid: str) -> Job | None:
        with self._lock:
            return self._jobs.get(jid)

    def remove(self, jid: str) -> None:
        """移出注册表。已删除的记录不该再出现在 /api/history 的运行中列表里。"""
        with self._lock:
            self._jobs.pop(jid, None)

    def snapshots(self, *, include_result: bool = False) -> list[dict]:
        """先锁着拷出列表，再逐个取快照（不在持锁期间做序列化）。"""
        with self._lock:
            jobs = list(self._jobs.values())
        return [j.snapshot(include_result=include_result) for j in jobs]

    def running_count(self) -> int:
        with self._lock:
            return sum(1 for j in self._jobs.values() if j.state not in TERMINAL_STATES)

    def prune(self, keep: int = 200) -> None:
        """只保留最近 keep 个终态作业，防止长跑进程内存无界增长。"""
        with self._lock:
            terminal = [j for j in self._jobs.values() if j.state in TERMINAL_STATES]
            if len(terminal) <= keep:
                return
            terminal.sort(key=lambda j: j.created_at)
            for j in terminal[: len(terminal) - keep]:
                self._jobs.pop(j.id, None)
