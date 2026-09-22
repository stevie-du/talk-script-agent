# -*- coding: utf-8 -*-
"""作业（Job）与作业注册表（JobRegistry）。

从 pipeline.py 里拆出来的两个理由：
  1. 状态机是并发正确性的核心，值得独立成文、独立测试；
  2. pipeline 原来一个类同时管配置、提示词、落盘、状态，646 行里真正的
     「流程编排」不到一半，读的人得先把无关细节筛掉。

状态机（**唯一权威是下面的 `STATES` / `TRANSITIONS`，别照这份图写代码**）
------------------------------------------------------------------------
    queued → selecting → writing → checking ─┬→ storyboarding → done / failed
                                              └→ (回炉) writing …
    单段重写：done → rewriting → storyboarding → done
    建包作业：queued → packing → done / failed / cancelled
    任一进行中的状态 → cancelled

所有迁移都必须走 `Job.transition()`——**检查与置位在同一把锁内完成**。
修复前的写法是「先无锁读 job.state 判断，再 job.update(state=…)」，
两个并发请求会双双通过检查（TOCTOU）：连点两次「继续」起两个写线程，
双倍 token、steps 重复、result 互相覆盖。

这里曾经还有 `paused_awaiting_confirmation`（分步确认：选题后暂停等用户改角度），
2026-09-19 整条移除。移除理由不是"没人用"，而是成本收益不划算：它省下的只是
"角度不对时白跑一次撰写调用"，换来的却是全链路最脆的一段 —— 确认卡要跨
"作业驻内存"这个前提活着，于是重启后记录点不开（/confirm 404）、待确认卡
攒到上限被静默丢弃、以及一张因快照不含 result 而整个空掉的确认卡。
事后修正本来就有「换一版」「重写本段」「按此修改」三条路，代价并不更高。
"""
from __future__ import annotations

import copy
import logging
import threading
import time
import uuid
from datetime import datetime

# 终态：进入后不再流转。同时用于「停止」的幂等判断。
TERMINAL_STATES = frozenset({"done", "failed", "cancelled"})

log = logging.getLogger(__name__)

# P1-5「整作业预算」：修复前只有**逐层**的重试上限（回炉轮 × chat_json 结构重试 ×
# 请求层重试），没有任何一处管「一条作业最多花多长时间」。叠乘的形状是
# (1+recheck_rounds)×(1+max_retries)×(1+retries)：出厂默认 (1+2)×(1+1)×(1+2)=18 次
# 模型调用，把 `retries` 调到 3 就是 (1+2)×(1+1)×(1+3)=24 次 ——
# 一次生成可以一路重试到几十分钟而不被叫停，用户只能干等或在界面上看着「已用 N 秒」变长。
# （把口径写成公式而不是一个孤零零的数：`retries` 用户可配 0~10、`recheck_rounds`
#   每个行业包自己定，任何不带条件的"最坏 N 分钟"都会过期 —— 本项目已经栽过几次。）
# 这里给一个宽到不会误伤正常作业的上界：正常路径实测 3 次调用、几十秒到几分钟，
# 触到这条线的都是「上游卡住 / 重试层叠乘」这类真正需要人看一眼的情况。
JOB_BUDGET_SECONDS = 1200.0

# 第二道网的倍数：忙态作业超过 `JOB_BUDGET_SECONDS × 本值` 还没收口，就一定是
# 收口代码自己坏了（正常路径上每个检查点都在预算内触发：阶段前的 `_stop_check`、
# 流式逐行 abort、被夹到剩余预算的读超时与退避）。第 9 轮复核定到的形态是
# `_guarded` 的兜底在 `transition` 上再抛 —— 那时没有任何东西会把额度还回来。
STALE_JOB_MULTIPLIER = 2

# 真正占用模型资源的作业状态 —— **并发额度只看这些**。
BUSY_STATES = frozenset({"queued", "selecting", "writing", "checking", "rewriting",
                         "storyboarding", "packing"})

# 合法迁移表：from_state -> 允许去的 to_state。
# 写成表而不是散落在各方法里的 if，是为了让「谁能到哪儿」一眼可查，
# 也让非法迁移统一变成 409 而不是静默写坏状态。
TRANSITIONS: dict[str, frozenset[str]] = {
    "queued": frozenset({"selecting", "writing", "packing", "failed", "cancelled"}),
    "selecting": frozenset({"writing", "failed", "cancelled"}),
    "writing": frozenset({"checking", "rewriting", "done", "failed", "cancelled"}),
    # storyboarding：P1-30 拆出来的分镜阶段，只在校验通过后进 —— 正文没过校验时
    # 不该为一版要重写的文案画分镜。
    "checking": frozenset({"writing", "rewriting", "storyboarding",
                           "done", "failed", "cancelled"}),
    # 单段重写后重画分镜（rewriting -> storyboarding），见 pipeline._run_rewrite_segment
    "rewriting": frozenset({"checking", "storyboarding", "done", "failed", "cancelled"}),
    "storyboarding": frozenset({"done", "failed", "cancelled"}),
    # packing：新建行业包（P1-43）。与生成同一套额度、取消与错误通道，
    # 不再是「唯一一个走同步长 HTTP 请求的耗时操作」。
    "packing": frozenset({"done", "failed", "cancelled"}),
    "done": frozenset({"rewriting", "cancelled"}),
    # failed → rewriting：done 作业重写失败后，用户还能再点一次「重写本段」。
    # 修复前 failed 只允许 {writing, cancelled}，失败记录在重试原作业之外
    # 永久得不到单段重写 —— 界面只有一句 409「作业状态为 failed」。
    "failed": frozenset({"writing", "rewriting", "cancelled"}),
    "cancelled": frozenset(),
}


class JobCancelled(Exception):
    """用户按了停止。

    这是控制流而非错误：走了它就不能落到 failed，否则界面显示「生成失败」
    让用户以为是自己配置错了，实则只是他主动停的。
    """


class StateConflict(RuntimeError):
    """并发发起的操作与当前状态冲突（HTTP 层映射为 409）。"""


class JobBudget(RuntimeError):
    """整作业时间预算用尽（P1-5）。

    与 JobCancelled 分开：取消是用户主动停的（状态落 cancelled、不算失败），
    超预算是「我们等不下去了」，必须落成 failed 并把原因说清楚 ——
    合并成一种会让界面把上游卡死报成「用户点了停止」。
    """

    def __init__(self, seconds: float):
        s = max(0.0, float(seconds))
        # 界面计时器报的是秒（progress.js「已用 N 秒」），这里原来只报分钟：
        # 「已用 1205 秒」配「超过 20 分钟仍未完成」，用户对不上这是同一本账，
        # 也就看不出"我到底等了多久被停的"。分钟数不整就带一位小数（90 秒说
        # 1.5 分钟，不说"超过 2 分钟" —— 那是虚报）。
        if s >= 60:
            m = f"{s / 60:.1f}".removesuffix(".0")
            span = f"{m} 分钟（{s:g} 秒）"
        else:
            span = f"{s:g} 秒"
        super().__init__(
            f"本次任务的单次时间预算 {span} 已用完，已停止（模型响应过慢或接口反复重试）。"
            "可在设置里降低 llm.retries、或降低行业包 skill.yaml 的 limits.recheck_rounds，"
            "推理型模型频繁空内容时建议换非推理档。")
        self.seconds = seconds


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
        # 最近一次"确实有进展"的时刻（走到检查点 / 记一步 / 收到流式增量都会刷新）。
        # 回收网只看这个，不看 `started_at`：一条活着但在赶进度慢的作业，
        # `started_at` 早就超过 2× 预算，而它的产物马上就要出来了。
        self.last_progress = self.started_at
        # 被第二道网"挂起来"：不再占用并发额度，但**条目、状态、产物一律不动**。
        # 见 `_reap_stranded`：删条目会让还在跑它的前端 404，改状态会废掉它的状态机。
        self.stranded = False
        # 建包作业持有的那份「目录名占位凭证」（`packgen.claim_slug` 发的）。
        # 归还点有**三个**（目录已存在的早退、入口的兜底、worker 的 finally），
        # 凭证必须跟着作业走，否则晚到的那一次会把这期间别人抢到的同名占位摘走
        # （第 16 轮复核 P1、第 18 轮复核 P2-1：worker 的 finally 是每条件建包都走的那个点）。
        self.claim_token = ""

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

    def overdue(self) -> bool:
        """整作业预算是否用尽（P1-5）。终态作业永不超时（不该事后翻成 failed）。"""
        if self.state in TERMINAL_STATES:
            return False
        return time.time() - self.started_at > JOB_BUDGET_SECONDS

    def deadline(self) -> float | None:
        """整作业预算的**绝对到期时刻**（epoch 秒）；终态作业返回 None。

        给网络层收缩读超时用（P2-48）：取消/预算的检查点长在"收到一行"上，
        一条**一个字都不吐**的流永远不会走到那些检查点，而 httpx 的读超时
        是 `timeout × STREAM_READ_TIMEOUT_MULT`（180×2=360s）再乘上重试次数 ——
        实测最坏 ≈48 分钟，是预算的 2.4 倍。把剩余预算喂给读超时，卡死的上游
        就会在预算到点时以超时收工，而不是继续占着槽。
        """
        if self.state in TERMINAL_STATES:
            return None
        return self.started_at + JOB_BUDGET_SECONDS

    def reset_budget(self) -> None:
        """重新计时：在已完成的老记录上发起单段重写时用。

        不重置的话 `started_at` 还是当初建作业那一刻 —— 打开一条昨天的记录点
        「重写本段」会立刻被判超预算，把一次正常的重写报成失败。
        """
        self.started_at = time.time()
        # ⚠ 与 `transition_if_room` 配对：那道闸在放行时 `touch()`（回收网的另一只钟），
        #   所以这里**不**再 touch —— 第 15 轮复核量到那一行在这条路径上贡献恰好 0 秒，
        #   是一具被断言自己喂饱的安慰剂。将来若有别的地方调用 `reset_budget`，
        #   那条路径必须自己负责把两只钟一起重开。

    def touch(self) -> None:
        """记一次「这条作业还在往前走」。浮点写，不需要锁。"""
        self.last_progress = time.time()

    def mark_stranded(self) -> None:
        """回收网专用：只挂额度，不碰状态。"""
        self.stranded = True

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
            self.touch()        # 收到增量就是进展：慢但活着，不该被回收网当成卡死
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
                # 交**副本**而不是活动引用：HTTP 层是在锁外把这些字典序列化成 JSON 的，
                # 期间工作线程可能继续 `steps.append(...)` 或改 params。
                # 修复前实测同一个快照里能同时出现"第 3 轮回炉"和只有 2 条的步骤，
                # 偶发但真实（快照是给界面看的"某一时刻"，不是活对象的别名）。
                "params": dict(self.params),
                # 每条步骤连它的 `data` 一起深拷贝：`dict(data)` 只有一层，
                # `data["report"]` 交活动引用等于让已发出的"某一时刻"再变
                # （第 6 轮复核实测：改 `steps[0]["data"]["report"]["hard_hits"]`
                # 会串进上一份快照）。整步深拷贝 0.08ms/次，相对 0.9s 轮询可忽略。
                "steps": copy.deepcopy(self.steps),
                "error": self.error,
                "created_at": self.created_at,
            }
            if include_result or self.state in TERMINAL_STATES:
                # 深拷贝：`dict(self.result)` 只有一层，`result["sections"][0]` 与
                # `result["check"]` 仍是活引用（批次 10 复核实测：改它们会串进已发出的
                # 快照）。产物只在 done 这一次带上，量过成本：几十 KB 的 deepcopy
                # 远小于 0.9s 的轮询间隔。
                snap["result"] = copy.deepcopy(self.result) if self.result else self.result
            # P1-7 修正：阶段一开始（begin_stream 置了 phase）就要下发 stream 键，
            # 前端据此显示「等待模型首个 token…」。修复前门禁挂在两个计数字段上，
            # 而 begin_stream 恰好清零它们 —— 于是首字节前的整个静默期都没有
            # stream 键，思考块被 progress.js 隐藏，静默几十秒的问题从未真正消失。
            if self.stream_phase:
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

    def add_if_room(self, job: Job, limit: int) -> bool:
        """**同一把锁内**检查并发额度并插入；返回 False 表示已满（未插入）。

        为什么不写成「先 `running_count() >= limit` 判断、再 `add(job)`」：
        那是两次独立的加锁，中间有窗口。而 `/api/generate` 是**同步 `def`**
        （FastAPI 丢线程池执行，是真并发），两个请求可以同时通过检查、
        双双插入 —— 额度形同虚设，`MAX_CONCURRENT_JOBS` 只保证「通常有效」。

        `Job.transition()` 修的是同一类 TOCTOU（检查与置位同锁），
        这里照同一套做法收口：**把判断和写入放进同一个临界区**。

        插入时 `job.state` 已是 `queued`（在 `BUSY_STATES` 里），
        所以额度从这一刻起就被占住，不必等 `_spawn` 起线程。
        """
        self._reap_stranded()      # 第二道网：先还额度再判（见 prune 的说明）
        with self._lock:
            if sum(1 for j in self._jobs.values()
                   if j.state in BUSY_STATES and not j.stranded) >= limit:
                return False
            self._jobs[job.id] = job
            return True

    def transition_if_room(self, job: Job, new_state: str, limit: int) -> bool:
        """单段重写在**原作业**上进行（不新增条目），所以额度要在这里补一道闸。

        修复前 `rewrite_segment` 只做迁移、从不看额度 —— 实测 12 条并发重写
        同时在飞（上限 4），`running_count` 事后报 12 只是陈述现状，拦不住。
        与 `add_if_room` 同款收口：**计数检查与迁移在同一把 registry 锁内**，
        并发重写不同作业时不会双双通过检查。

        注意锁序：拿 registry 锁 → 调 `Job.transition_or_raise`（作业自身锁）。
        全库没有反向（先作业锁再 registry 锁）的路径，不会死锁。
        """
        self._reap_stranded()      # 与 add_if_room 同一道第二网（取锁之前）
        with self._lock:
            busy = sum(1 for j in self._jobs.values()
                       if j.state in BUSY_STATES and not j.stranded and j.id != job.id)
            if busy >= limit:
                return False
            job.transition_or_raise(new_state, error=None)
            # 放行与"重新计时"必须在同一个临界区：不然刚被放行的老作业会在
            # 下一次回收扫描里被摘掉额度（第 11 轮复核 P1，实测 stranded=True 跑完整条重写）。
            # 被摘过额度的作业在这里**回到账上**（第 12 轮复核 P1）：三个额度口径
            # 对 stranded 的两条规则是"排除它"，而准入这条又允许它，于是 N 条被误摘的
            # 老记录可以同时重写，实测 7 条在飞 > 上限 4 而计数报 3。
            # 单向门在这里打开是安全的：这一次准入真的占了一个名额，就必须被数到。
            job.stranded = False
            job.touch()
            return True

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
        """占用并发额度的作业数（口径见 BUSY_STATES）。

        **不是**「非终态作业数」：待确认的作业不该算并发 ——
        否则 4 张没人理的确认卡就能把后续所有生成永久挡在 409 之外。

        ⚠ **不是**「非终态作业数」：待确认的作业不该算并发 ——
        否则 4 张没人理的确认卡就能把后续所有生成永久挡在 409 之外。

        ⚠ 更正这份文档原来的一句话（第 11 轮复核）：它自称"供 `/api/meta` 展示"，
        而 grep 全库确认 **生产端没有任何调用者** —— `/api/meta` 只回常量 max_concurrent。
        界面上"几条在跑"是渲染层按历史记录自己数的（`main.js` / `sessions.js`），
        那是另一本账：被回收网摘掉额度的作业这里不算它，界面却照样数它一条。
        所以它的用途只有两个：诊断日志与测试断言。**不要**拿它去做准入判断 ——
        那是 TOCTOU，准入用 `add_if_room()` / `transition_if_room()`。
        """
        with self._lock:
            return sum(1 for j in self._jobs.values()
                       if j.state in BUSY_STATES and not j.stranded)

    def prune(self, keep: int = 200) -> None:
        """回收终态作业，只保留最近 `keep` 个，防止长跑进程内存无界增长。

        这里曾并列着第二条 `_trim(paused, PAUSED_KEEP)` —— 给"待确认作业"单独
        设一道内存上界（它们当时被排除在并发额度外，数量不受 MAX_CONCURRENT_JOBS
        约束）。分步确认移除后这条上界连同它的静默丢弃一起没了。

        ⚠ 第二道网（第 9 轮复核：`_guarded` 的兜底如果自己也在 `transition` 上抛，
        作业就永远停在忙态，而这里只回收终态 —— 症状照旧是"一个都没在跑，
        生成却一直报已达上限"，只能重启）。这里在回收终态之前先 `stranded` 掉
        那些远超预算还挂在忙态的作业，把额度让出来；见 `_reap_stranded`。

        ⚠ 于是 `keep` 只是**终态**作业的上界：被摘掉额度的作业还没进终态，
        `_trim` 不收它，注册表可能超出 keep（第 11 轮复核 P2）。这是刻意的取舍 ——
        每一条这样的记录都对应一次"引擎自己没把作业收口"的缺陷，且各带一条
        WARNING 日志；宁可留着几条记录占几 KB，也不要再犯"为了回收内存而改动
        活作业状态"那个 P1。真正该修的是产生它的那条路径（见 `_spawn` / `_guarded` /
        `start_packgen` 的三处兜底）。
        """
        self._reap_stranded()
        with self._lock:
            self._trim(lambda j: j.state in TERMINAL_STATES, keep)
            # ⚠ 这里**不**收"被摘额度但仍忙"的记录（第 15 轮复核把我上一轮加的这条收回来了）：
            #   那些作业的线程还活着，删条目就等于 `/api/jobs/{id}` 从此 404，
            #   而它下一秒就会写出产物 —— 正是 `_reap_stranded` 上面立誓不再犯的那个 P1。
            #   内存代价（每条一份快照、各带一行 WARNING）留着，因为它同时是一个可见的
            #   缺陷信号：非零就说明有路径没把作业收口，该修的是那条路径。

    def _reap_stranded(self) -> None:
        """把「远超预算却还挂在忙态」的作业从并发额度里摘出去 —— 只摘额度。

        两道网的取舍（第 10 轮复核把前一版判为 P1）：
        - 前一版 `force("failed")`：看着像收口，实际把这条作业的状态机废了。
          `failed -> done` 不在迁移表里（实测），线程后面每一次
          `transition_or_raise` 都抛 StateConflict，产物还会被「取消/失败 → 撤掉」
          那一支删走 —— 回收网自己杀掉了本来会成功的作业。
        - 再前一版本 `_jobs.pop`：额度确实还了，但 `/api/jobs/{id}` 从此 404
          （`get` 抛 KeyError），正在看进度的界面变成"作业不存在"；
          而这条作业明明还在跑、马上就要出产物。

        所以现在只置 `stranded`：条目、状态、产物、轮询全都不动，
        只有额度计数（`add_if_room` / `transition_if_room` / `running_count`）不再算它。
        作业自己那套预算闸门照旧生效：每个检查点都会以「预算用尽」收工，
        那条路走的是它自己的状态机，迁移合法、说明也写得清楚。

        单向门只对 `touch` 成立（第 15 轮复核把这句话改准）：光"还在往前走"不把额度收回来，
        否则等于同一份算力卖两次。唯一打开这道门的地方是 `transition_if_room` ——
        那是一次**新的准入**，占了名额就必须回到账上。
        """
        limit = JOB_BUDGET_SECONDS * STALE_JOB_MULTIPLIER
        now = time.time()
        with self._lock:
            stale = [j for j in self._jobs.values()
                     if j.state in BUSY_STATES
                     and not j.stranded
                     and now - j.last_progress > limit]
            for j in stale:
                j.mark_stranded()
        for j in stale:
            log.warning("作业 %s 已 %s 秒无任何进展，不再占用并发额度"
                        "（作业线程仍在跑，条目与产物保留；无进展的原因可能是上游慢，"
                        "也可能是磁盘/确定性代码这一段卡住，两处都查）",
                        j.id, int(now - j.last_progress))

    def _trim(self, pred, keep: int) -> None:
        """丢掉匹配 pred 的、最旧的超出部分（调用方须持锁）。"""
        hit = [j for j in self._jobs.values() if pred(j)]
        if len(hit) <= keep:
            return
        hit.sort(key=lambda j: j.created_at)
        for j in hit[: len(hit) - keep]:
            self._jobs.pop(j.id, None)
