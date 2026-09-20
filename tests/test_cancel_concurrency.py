# -*- coding: utf-8 -*-
"""取消与并发回归测试。

跑法：python tests/test_cancel_concurrency.py   或   pytest tests/test_cancel_concurrency.py

覆盖：
  1 「停止」对运行中的作业不再是空操作 —— cancel() 接受 running 状态、
    流式收包过程中立即中断、线程的收尾写入不得把状态改回来。
  2 状态冻结：已取消的作业不接受任何迁移（transition 返回 False）。
  3 重复取消幂等；未知作业抛 KeyError（HTTP 层转 404）。
  4 作业注册表并发读写不炸（原来会 RuntimeError: dictionary changed size）。

刻意不用 mock 夹具：mock 立即返回，跑不到「生成中」这个时间窗口，
必须用会慢慢吐 delta 的假 LLM 才能逼真复现用户点停止的那一瞬间。
"""
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import load_config                        # noqa: E402
from app.jobs import Job, JobCancelled, TRANSITIONS       # noqa: E402
from app.pipeline import Pipeline                         # noqa: E402
from app.schemas import GenerateRequest, TopicPlan        # noqa: E402


class SlowLLM:
    """假 LLM：select 瞬时返回，write 阶段慢慢吐增量（模拟几分钟的生成）。"""

    class _Cfg:
        temperature = 0.7
        max_tokens = 4096

    def __init__(self):
        self.cfg = self._Cfg()
        self.write_calls = 0
        self.finished_write = False       # 被取消时必须仍是 False

    def chat_json(self, task, system, user, model_cls, max_retries=1,
                  on_retry=None, temperature=None, on_delta=None):
        if task == "select":
            return TopicPlan(angle="被困别慌", hook_type="反常识",
                             hook_line="电梯里最危险的动作是扒门。",
                             points=["别扒门", "按警铃"], cta="关注我，乘梯更安心")
        self.write_calls += 1
        for _ in range(400):
            if on_delta:
                on_delta("reasoning", "想一点")
            time.sleep(0.01)
        self.finished_write = True
        raise AssertionError("write 阶段不应跑完：取消必须在流式过程中就中断它")


def make_pipeline(tmp: Path, llm) -> Pipeline:
    cfg = load_config(tmp)
    cfg.mock = False
    pl = Pipeline(tmp, cfg)
    pl.llm = llm                      # 走 setter，内部有锁；不再需要替换 build_llm
    return pl


def wait_state(pl, jid, states, timeout=15.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        snap = pl.get_job(jid).snapshot()
        if snap["state"] in states:
            return snap
        time.sleep(0.05)
    raise TimeoutError(f"{jid} 停在 {pl.get_job(jid).snapshot()['state']}")


def test_cancel_running():
    """生成中点停止：作业立即落到 cancelled，后台线程及时收工且不再写回。"""
    tmp = Path(tempfile.mkdtemp(prefix="ts-cancel-"))
    shutil.copytree(ROOT / "packs", tmp / "packs")
    llm = SlowLLM()
    pl = make_pipeline(tmp, llm)

    jid = pl.start_generate(GenerateRequest(pack="elevator", topic="被困电梯怎么办", mode="auto"))
    wait_state(pl, jid, {"writing"})

    snap = pl.cancel(jid)
    assert snap["state"] == "cancelled", f"cancel 后应立刻为 cancelled: {snap['state']}"

    time.sleep(1.2)
    after = pl.get_job(jid).snapshot()
    assert after["state"] == "cancelled", f"线程收尾不得把状态改回: {after['state']}"
    deadline = time.time() + 5.0
    while time.time() < deadline and not llm.finished_write:
        time.sleep(0.05)
    assert not llm.finished_write, "取消后 write 仍跑完了 —— 流式中断没生效"
    assert after["error"] in (None, ""), f"取消不是错误，不该留 error: {after['error']}"
    assert not list((tmp / "generated").glob(f"*/{jid}/result.json")), "已取消不应落盘产物"
    shutil.rmtree(tmp, ignore_errors=True)


def test_cancel_freezes_state():
    """点了停止之后，任何线程的状态迁移都不再生效。"""
    job = Job("j1", "generate", {})
    job.request_cancel()
    assert job.state == "cancelled"
    assert job.transition("done", result={"a": 1}) is False
    assert job.state == "cancelled", "已取消的作业被写成 done —— 假停止又回来了"
    assert job.result is None
    # 非状态字段仍应能写（取消后还要记 error=None 之类的收尾）
    job.update(error=None)
    assert job.error is None
    # 未取消的作业不受影响
    j2 = Job("j2", "generate", {})
    assert j2.transition("writing") is True
    assert j2.state == "writing"
    # 非法迁移被拒绝
    j3 = Job("j3", "generate", {})
    assert j3.transition("done") is False, "queued 不能直接到 done"


def test_transition_table_is_consistent():
    """状态表自洽性：终态无出边（cancelled 之后不得再流转）。"""
    from app.jobs import TERMINAL_STATES
    assert TRANSITIONS["cancelled"] == frozenset()
    for s in TERMINAL_STATES:
        assert s in TRANSITIONS, s
    assert "done" in TERMINAL_STATES and "failed" in TERMINAL_STATES


def test_cancel_idempotent():
    """已结束的作业再点停止是幂等的，不当错误抛。"""
    tmp = Path(tempfile.mkdtemp(prefix="ts-idem-"))
    shutil.copytree(ROOT / "packs", tmp / "packs")
    llm = SlowLLM()
    pl = make_pipeline(tmp, llm)
    jid = pl.start_generate(GenerateRequest(pack="elevator", topic="加装电梯一楼不同意"))
    # 原来这里靠 mode="step" 把作业停在待确认态，好让「取消」落在一个稳定点上。
    # 分步确认已移除，改为等作业进入活跃态再取消 —— SlowLLM 保证它还在跑。
    wait_state(pl, jid, {"selecting", "writing", "checking"})
    assert pl.cancel(jid)["state"] == "cancelled"
    assert pl.cancel(jid)["state"] == "cancelled", "重复取消应幂等"
    try:
        pl.cancel("不存在的作业")
    except KeyError:
        pass
    else:
        raise AssertionError("未知作业应抛 KeyError（HTTP 层转成 404）")
    assert JobCancelled.__name__ == "JobCancelled"
    shutil.rmtree(tmp, ignore_errors=True)


def test_jobs_concurrent():
    """HTTP 层遍历作业时后台线程在并发建作业，不得 RuntimeError。"""
    tmp = Path(tempfile.mkdtemp(prefix="ts-lock-"))
    shutil.copytree(ROOT / "packs", tmp / "packs")
    pl = make_pipeline(tmp, SlowLLM())

    errors = []
    stop = threading.Event()

    def reader():
        try:
            while not stop.is_set():
                for snap in pl.snapshot_jobs():
                    _ = snap["id"], snap["state"]
        except Exception as e:  # noqa: BLE001
            errors.append(f"reader: {type(e).__name__}: {e}")

    def writer():
        try:
            for i in range(60):
                pl.registry.add(Job(f"job-{i}", "generate", {}))
                time.sleep(0.001)
        except Exception as e:  # noqa: BLE001
            errors.append(f"writer: {type(e).__name__}: {e}")

    rs = [threading.Thread(target=reader, daemon=True) for _ in range(3)]
    ws = [threading.Thread(target=writer, daemon=True) for _ in range(2)]
    for t in rs + ws:
        t.start()
    for t in ws:
        t.join()
    stop.set()
    for t in rs:
        t.join(timeout=5)

    assert not errors, "并发访问作业注册表出错：" + "；".join(errors)
    shutil.rmtree(tmp, ignore_errors=True)


def test_registry_prune():
    """注册表要能回收终态作业，防止长跑进程内存无界增长。"""
    from app.jobs import JobRegistry
    reg = JobRegistry()
    for i in range(30):
        j = Job(f"j{i}", "generate", {})
        j.transition("failed", force=True, error="x")
        reg.add(j)
    assert len(reg.snapshots()) == 30
    reg.prune(keep=10)
    assert len(reg.snapshots()) == 10


def test_stream_buffers_are_bounded():
    """流式文本不能无上限累积，但界面显示的字数必须是真实值。

    修复前两个缓冲区都是无上限 `+=`，且作业结束**从不释放** —— 注册表
    要保留 200 个终态作业，推理型模型几万字的思考 + 几十 KB 的正文草稿
    会一直驻留到被 prune 掉为止。模型若异常持续输出，单个作业就能吃满内存。
    """
    from app.jobs import Job, STREAM_TAIL

    j = Job("20260913-000000-aaaaaa", "generate", {})
    j.begin_stream("文案撰写")
    for _ in range(50):                       # 思考 5 万字、正文 10 万字
        j.push_delta("reasoning", "想" * 1000)
        j.push_delta("content", "x" * 2000)

    assert len(j.stream_reasoning) <= STREAM_TAIL, "思考文本无上限驻留内存"
    # 字数必须仍是真实累计值：直接截断会让界面停在 1500 字，那是显示错误信息
    assert j.stream_reasoning_len == 50000, j.stream_reasoning_len
    assert j.stream_content_len == 100000, j.stream_content_len

    snap = j.snapshot(include_result=False)
    assert snap["stream"]["reasoning_len"] == 50000
    assert len(snap["stream"]["reasoning_tail"]) <= STREAM_TAIL
    assert snap["stream"]["content_len"] == 100000

    # 换阶段要重置（否则上一阶段的思考会串到下一阶段）
    j.begin_stream("校验")
    assert j.stream_reasoning_len == 0 and j.stream_content_len == 0
    assert j.snapshot(include_result=False).get("stream") is None


def main() -> int:
    cases = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in cases:
        try:
            fn()
            print(f"  ✅ {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  ❌ {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(cases) - failed}/{len(cases)} 通过")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
