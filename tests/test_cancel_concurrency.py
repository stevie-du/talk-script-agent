# -*- coding: utf-8 -*-
"""三处 P0 的回归测试：python tests/test_cancel_concurrency.py

覆盖：
  P0-1 「停止」对运行中的作业不再是空操作 —— cancel() 接受 running 状态、
        流式收包过程中立即中断、线程的收尾写入不得把状态改回来。
  P0-2 作业字典并发访问 —— 一边起作业一边拉历史不再 RuntimeError。
  P0-3 已结束的作业再取消是幂等的（不报错）。

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

from app.config import load_config                     # noqa: E402
from app.pipeline import Pipeline, Job, JobCancelled   # noqa: E402
from app.schemas import GenerateRequest, TopicPlan     # noqa: E402


class SlowLLM:
    """假 LLM：select 瞬时返回，write 阶段慢慢吐增量（模拟几分钟的生成）。"""

    class _Cfg:
        temperature = 0.7
        max_tokens = 4096

    def __init__(self):
        self.cfg = self._Cfg()
        self.write_calls = 0
        self.finished_write = False       # 是否走完了整个 write（被取消时必须是 False）

    def chat_json(self, task, system, user, model_cls, max_retries=1,
                  on_retry=None, temperature=None, on_delta=None):
        if task == "select":
            return TopicPlan(angle="被困别慌", hook_type="反常识", hook_line="电梯里最危险的动作是扒门。",
                             points=["别扒门", "按警铃"], cta="关注我，乘梯更安心")
        self.write_calls += 1
        # 慢慢吐 ~4 秒。跑完会把 finished_write 置 True —— 那说明「停止」没拦住它，
        # 钱照烧、照写产物，正是修复前的行为。
        for _ in range(400):
            if on_delta:
                on_delta("reasoning", "想一点")
            time.sleep(0.01)
        self.finished_write = True
        raise AssertionError("write 阶段不应跑完：取消必须在流式过程中就中断它")


def make_pipeline(tmp: Path, llm: SlowLLM) -> Pipeline:
    cfg = load_config(tmp)
    cfg.mock = False
    pl = Pipeline(tmp, cfg)
    pl.llm = llm
    pl.build_llm = lambda: llm          # 别再被 _normalize 之前的重建覆盖掉
    return pl


def wait_state(pl, jid, states, timeout=15.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pl.get_job(jid).snapshot()["state"] in states:
            return pl.get_job(jid).snapshot()
        time.sleep(0.05)
    raise TimeoutError(f"{jid} 停在 {pl.get_job(jid).snapshot()['state']}")


def case_cancel_running():
    """P0-1：生成中点停止，作业立即落到 cancelled，后台线程及时收工且不再写回。"""
    tmp = Path(tempfile.mkdtemp(prefix="ts-cancel-"))
    shutil.copytree(ROOT / "packs", tmp / "packs")
    llm = SlowLLM()
    pl = make_pipeline(tmp, llm)

    jid = pl.start_generate(GenerateRequest(pack="elevator", topic="被困电梯怎么办", mode="auto"))
    wait_state(pl, jid, {"writing"})

    snap = pl.cancel(jid)                       # ← 以前这里会抛 ValueError「不可取消」
    assert snap["state"] == "cancelled", f"cancel 后应立刻为 cancelled: {snap['state']}"

    time.sleep(1.2)                             # 给后台线程收尾的时间
    after = pl.get_job(jid).snapshot()
    assert after["state"] == "cancelled", f"线程收尾不得把状态改回: {after['state']}"
    # 再等到超过被打断作业原本需要的时长：它若没真被掐断，这段时间足够跑完
    deadline = time.time() + 5.0
    while time.time() < deadline and not llm.finished_write:
        time.sleep(0.05)
    assert not llm.finished_write, "取消后 write 仍跑完了 —— 流式中断没生效"
    assert after["error"] in (None, ""), f"取消不是错误，不该留 error: {after['error']}"
    assert not list((tmp / "generated").glob(f"*/{jid}/result.json")), "已取消不应落盘产物"
    print("[1] 停止运行中的作业 OK（流式过程中中断、终态稳定、不落产物）")


def case_update_guard():
    """P0-1 附带：点了停止之后，任何线程的 update(state=…) 都不再生效。"""
    job = Job("j1", "generate", {})
    job.request_cancel()
    assert job.state == "cancelled"
    job.update(state="done", result={"a": 1})
    assert job.state == "cancelled", "已取消的作业被写成 done —— 假停止又回来了"
    assert job.result == {"a": 1}, "非 state 字段仍应正常写入"
    # 未取消的作业不受影响
    j2 = Job("j2", "generate", {})
    j2.update(state="writing")
    assert j2.state == "writing"
    print("[2] 取消后状态写保护 OK（state 冻结、其余字段照写）")


def case_cancel_idempotent():
    """P0-3：已结束的作业再点停止是幂等的，不当错误抛。"""
    tmp = Path(tempfile.mkdtemp(prefix="ts-idem-"))
    shutil.copytree(ROOT / "packs", tmp / "packs")
    llm = SlowLLM()
    pl = make_pipeline(tmp, llm)
    jid = pl.start_generate(GenerateRequest(pack="elevator", topic="加装电梯一楼不同意", mode="step"))
    wait_state(pl, jid, {"paused_awaiting_confirmation"})
    assert pl.cancel(jid)["state"] == "cancelled"
    assert pl.cancel(jid)["state"] == "cancelled", "重复取消应幂等"
    try:
        pl.cancel("不存在的作业")
    except KeyError:
        pass
    else:
        raise AssertionError("未知作业应抛 KeyError（HTTP 层转成 404）")
    print("[3] 重复取消幂等 / 未知作业抛 KeyError OK")


def case_jobs_concurrent():
    """P0-2：HTTP 层遍历作业字典时后台线程在并发建作业，不得 RuntimeError。"""
    tmp = Path(tempfile.mkdtemp(prefix="ts-lock-"))
    shutil.copytree(ROOT / "packs", tmp / "packs")
    llm = SlowLLM()
    pl = make_pipeline(tmp, llm)

    errors = []
    stop = threading.Event()

    def reader():
        # 模拟 GET /api/history：反复遍历 + 取快照
        try:
            while not stop.is_set():
                for snap in pl.snapshot_jobs():
                    _ = snap["id"], snap["state"]
        except Exception as e:  # noqa: BLE001
            errors.append(f"reader: {type(e).__name__}: {e}")

    def writer():
        try:
            for i in range(60):
                pl.add_job(Job(f"job-{i}", "generate", {}))
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

    assert not errors, "并发访问作业字典出错：" + "；".join(errors)
    assert JobCancelled.__name__ == "JobCancelled"
    print(f"[4] 作业字典并发安全 OK（{len(pl.jobs)} 个作业，3 读 2 写无异常）")


def main():
    case_cancel_running()
    case_update_guard()
    case_cancel_idempotent()
    case_jobs_concurrent()
    print("\n取消 / 并发回归全部通过 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
