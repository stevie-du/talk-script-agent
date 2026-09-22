# -*- coding: utf-8 -*-
"""并发额度必须由**原子占位**保证，而不是「先查再插」。

背景
----
`/api/generate` 是**同步 `def`** —— FastAPI 把它丢进线程池执行，是真并发。
修复前 `Pipeline.start_generate` 是：

    if self.registry.running_count() >= MAX_CONCURRENT_JOBS:   # 加锁 #1
        raise StateConflict(...)
    ...
    self.registry.add(job)                                     # 加锁 #2

两次加锁之间就是窗口：N 个请求可以同时读到「还没满」，然后一起插入。
`MAX_CONCURRENT_JOBS` 于是只保证「**通常**有效」，而不是不变量 ——
4 条并发生成的线程、双倍 token、结果互相覆盖，都从这里进来。

`jobs.py` 在状态机上早把这类 TOCTOU 修对了（`Job.transition()` 检查与置位同锁，
`confirm()` / `rewrite_segment()` 都走它），**只有这个入口漏了**。

修法：`JobRegistry.add_if_room(job, limit)` —— 判断与插入在同一个临界区。

测试怎么做到「确定性」
--------------------
并发 bug 天然不好测：光靠 Barrier 起跑，老实现**可能**碰巧不超额。
所以这里用 `_slow_running_count` 夹具把「读计数」这一步人为拉长 50ms ——
窗口一放大，check-then-act 必现，而新实现压根不读计数、不受影响。
这是**测试夹具，不是被测代码**，注释里写清楚，免得后人以为它在改行为。
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import load_config                        # noqa: E402
from app.jobs import Job, JobRegistry, StateConflict      # noqa: E402
from app.knowledge import PackError                       # noqa: E402
from app.pipeline import MAX_CONCURRENT_JOBS, Pipeline    # noqa: E402
from app.schemas import GenerateRequest                   # noqa: E402

LIMIT = MAX_CONCURRENT_JOBS
THREADS = 20

# 先抓住原始实现，避免下面 monkeypatch 之后自我递归
_ORIG_RUNNING_COUNT = JobRegistry.running_count
WINDOW = 0.05


def _slow_running_count(self) -> int:
    """读计数之后故意停一下，把 check-then-act 的窗口放大到**必现**。

    ⚠ 夹具，不是被测代码。`add_if_room()` 内联算计数、**不调用**
    `running_count()`，所以它完全不受这个延时影响；
    而一旦有人把额度检查写回「读计数 + 另一次 add」，`THREADS` 个线程会
    全部读到同一个旧值 → 全部插入 → 断言立刻报红。
    """
    n = _ORIG_RUNNING_COUNT(self)
    time.sleep(WINDOW)
    return n


def _pipeline(tmp: Path) -> Pipeline:
    """建一个**不真正跑作业**的 Pipeline。

    `_spawn` 换成 no-op 是刻意的：作业停在 `queued` 不动，额度就一直被占着，
    这样才能观察「第 5 个进不来」。若真让它跑起来，mock 会瞬间完成、释放额度，
    成功数就变成时间函数（时快时慢），测不出东西。
    """
    cfg = load_config(tmp)
    cfg.mock = True
    pl = Pipeline(tmp, cfg)
    pl._spawn = lambda job, fn: None          # type: ignore[method-assign]
    return pl


def _tmp_root() -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="ts-quota-"))
    shutil.copytree(ROOT / "packs", tmp / "packs")
    return tmp


def _fire(n: int, fn) -> list:
    """n 个线程用 Barrier 同时起跑，收集返回值或异常。"""
    barrier = threading.Barrier(n)
    out: list = []
    lock = threading.Lock()

    def worker(i):
        barrier.wait()
        try:
            r = fn(i)
        except BaseException as e:            # noqa: BLE001
            r = e
        with lock:
            out.append(r)

    ts = [threading.Thread(target=worker, args=(i,), daemon=True) for i in range(n)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(30)
    return out


def _req() -> GenerateRequest:
    return GenerateRequest(pack="elevator", topic="被困电梯怎么办", mode="auto")


# ── 1. 原语本身的原子性 ──────────────────────────────────────

def test_add_if_room_is_atomic_under_threads(monkeypatch):
    """40 个线程同时抢 4 个位置 → 成功数必须**恰好** 4，不是「最多 4 附近」。"""
    monkeypatch.setattr(JobRegistry, "running_count", _slow_running_count)
    reg = JobRegistry()
    out = _fire(40, lambda i: reg.add_if_room(Job(f"j{i}", "generate", {}), LIMIT))

    ok = [r for r in out if r is True]
    assert len(ok) == LIMIT, f"超额放行：{len(ok)} 个成功"
    assert _ORIG_RUNNING_COUNT(reg) == LIMIT
    assert len(reg.snapshots()) == LIMIT


def test_add_if_room_full_leaves_registry_untouched():
    """满了就是「什么都没发生」—— 不能留下半插入的痕迹。"""
    reg = JobRegistry()
    for i in range(LIMIT):
        assert reg.add_if_room(Job(f"a{i}", "generate", {}), LIMIT) is True

    extra = Job("extra", "generate", {})
    assert reg.add_if_room(extra, LIMIT) is False
    assert reg.find("extra") is None, "失败时不该插入"
    assert _ORIG_RUNNING_COUNT(reg) == LIMIT


def test_every_live_state_occupies_quota():
    """额度口径：**除终态外，任何状态都必须占某一族额度**。

    原来有一条 `test_add_if_room_ignores_paused_jobs`（待确认不占额度）——
    那是为了给"开着确认卡慢慢想"腾地方而刻意开的口子，代价是必须再补一道
    `PAUSED_KEEP` 内存上界，并且攒够就会静默丢卡。分步确认移除后这个状态没了，
    口径回到最简单也最安全的形式：只要作业还活着就占额度。

    B4 起额度分**两族**（模型 / 情报抓取，各自独立计数），所以判据从
    "在不在 BUSY_STATES" 改成"在两族的并集里" —— 并集仍然要覆盖全部活跃态，
    这条断言守的还是同一件事：**别再开"不占额度的活跃态"这种口子**。
    """
    from app.jobs import (ALL_BUSY_STATES, BUSY_STATES, INTEL_BUSY_STATES,
                          TERMINAL_STATES, TRANSITIONS)

    live = {"queued", "selecting", "writing", "checking", "rewriting", "storyboarding",
            "packing", "fetching"}
    assert live == set(TRANSITIONS) - TERMINAL_STATES, (
        "状态机加了新状态，请同步判断它占不占额度 —— 默认应当占")
    assert set(ALL_BUSY_STATES) == set(BUSY_STATES) | set(INTEL_BUSY_STATES)
    assert not (set(BUSY_STATES) & set(INTEL_BUSY_STATES)), \
        "两族额度不许重叠 —— 重叠的作业会被两条闸各数一次，额度凭空翻倍"
    for st in live:
        reg = JobRegistry()
        job = Job("x", "generate", {})
        job.state = st
        reg.add(job)
        family = (INTEL_BUSY_STATES if st in INTEL_BUSY_STATES else BUSY_STATES)
        assert reg.running_count(family) == 1, f"{st} 不占额度 → 并发上限可被绕过"
    # 两族**互不占名额**：一条抓取不该让生成报"已达上限"（那正是 B4 拆两族的原因）。
    reg = JobRegistry()
    fetch_job = Job("f", "intel", {})
    fetch_job.state = "fetching"
    reg.add(fetch_job)
    assert reg.running_count(BUSY_STATES) == 0, "情报抓取占了模型额度 —— 生成会被误报已达上限"
    assert reg.running_count(INTEL_BUSY_STATES) == 1


# ── 2. start_generate 在真并发下不超额 ───────────────────────

def test_start_generate_never_exceeds_limit():
    """20 个并发请求 → 恰好 LIMIT 个拿到 job_id，其余全是 StateConflict。"""
    pl = _pipeline(_tmp_root())
    out = _fire(THREADS, lambda i: pl.start_generate(_req()))

    ok = [r for r in out if isinstance(r, str)]
    conflicts = [r for r in out if isinstance(r, StateConflict)]
    assert len(ok) == LIMIT, f"超额放行：{len(ok)} 个 job_id"
    assert len(conflicts) == THREADS - LIMIT, \
        f"其余应是 StateConflict，实际 {[type(r).__name__ for r in out if not isinstance(r, str)]}"
    assert _ORIG_RUNNING_COUNT(pl.registry) == LIMIT


def test_start_generate_does_not_check_then_act(monkeypatch):
    """**变异捕手**：把「读计数」拉长 50ms。

    - 新实现：`add_if_room` 不读计数 → 窗口拉长也没影响 → 恰好 LIMIT；
    - 退回 check-then-act：20 个线程全读到旧值 → 全部插入 → 报红。
    """
    monkeypatch.setattr(JobRegistry, "running_count", _slow_running_count)
    pl = _pipeline(_tmp_root())
    out = _fire(THREADS, lambda i: pl.start_generate(_req()))

    ok = [r for r in out if isinstance(r, str)]
    assert len(ok) == LIMIT, f"check-then-act 窗口被踩中：{len(ok)} 个成功"


def test_limit_is_reusable_after_slot_frees():
    """额度用完 → 腾一个 → 又能进一个（不是「一次占满就永久失效」）。"""
    pl = _pipeline(_tmp_root())
    for _ in range(LIMIT):
        pl.start_generate(_req())
    with pytest.raises(StateConflict):
        pl.start_generate(_req())

    # 一条作业落终态（模拟跑完）
    jid = next(iter(pl.registry.snapshots()))["id"]
    job = pl.registry.get(jid)
    job.transition("selecting", error=None)
    job.transition("writing", error=None)
    job.transition("done", error=None)

    assert isinstance(pl.start_generate(_req()), str)
    assert _ORIG_RUNNING_COUNT(pl.registry) == LIMIT


# ── 3. 顺序：包不存在优先于额度满，且不吃额度 ────────────────

def test_pack_error_does_not_consume_a_slot():
    """包不存在（PackError→404）不能顺手吃掉一个额度。

    若把顺序写成「先占额度、再验包」，`PackError` 抛出去时那条 `queued`
    作业已经进了注册表、没人回收 —— 额度被永久吃掉一个。
    这正是 P0-3 的形态（额度被没人管的作业占住），所以顺序是刻意的。
    """
    pl = _pipeline(_tmp_root())
    with pytest.raises(PackError):
        pl.start_generate(GenerateRequest(pack="no-such-pack", topic="随便一个话题", mode="auto"))
    assert _ORIG_RUNNING_COUNT(pl.registry) == 0, "报错却占了额度"
    assert pl.registry.snapshots() == []


def test_bad_pack_reported_before_quota():
    """额度已满时给坏包：仍报 PackError（永久性错误比可重试的 409 更该先说）。"""
    pl = _pipeline(_tmp_root())
    for _ in range(LIMIT):
        pl.start_generate(_req())
    with pytest.raises(PackError):
        pl.start_generate(GenerateRequest(pack="no-such-pack", topic="随便一个话题", mode="auto"))
