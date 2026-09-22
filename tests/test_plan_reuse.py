# -*- coding: utf-8 -*-
"""选题复用（方案 3 轻量版）：同一次应用运行内，完全相同的选题提示词不再调模型。

背景
----
`select` 是一次 4000 token 的调用（实测 10~30s）。用户的常见动作是「就这个主题再来一版
措辞」——参数一个没改，于是模型把同一个角度与结构重算一遍，白花一次。

三条约束决定了实现的形状（`app/pipeline.py::_select`）：

1. **指纹取「模型 + 渲染后的 system + user」，不枚举参数。**
   枚举式键表要列全"哪些字段影响选题"，漏一项就是静默复用了一个不匹配的选题；
   而渲染后的文本天然包含参数、skill.yaml 模板与所有被注入的知识切片 —— 改任何
   一样，指纹自己就变了。
2. **「换一版」（reroll）永不复用**：那个动作的语义就是要一个新角度。
3. **复用必须在作业日志里说出来**（`select_reuse` 步骤）：界面上如果照常出现
   「选题策划」这一步，用户以为它真的又想过一遍 —— 这是同一类静默。

缓存只在内存里（`Pipeline` 实例的生命周期 = 引擎进程），不落盘。

跑法：pytest tests/test_plan_reuse.py
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import load_config                  # noqa: E402
from app.jobs import Job                             # noqa: E402
from app.knowledge import Pack                       # noqa: E402
from app.pipeline import Pipeline, wait_job         # noqa: E402
from app.schemas import GenerateRequest, TopicPlan   # noqa: E402


def _pipeline(tmp: Path) -> Pipeline:
    cfg = load_config(tmp)
    cfg.mock = True
    return Pipeline(tmp, cfg)


def _steps(snap: dict) -> list[str]:
    return [s["key"] for s in snap["steps"]]


def _gen(pl: Pipeline, **kw) -> dict:
    topic = kw.pop("topic", "被困电梯怎么办")
    jid = pl.start_generate(GenerateRequest(pack="elevator", topic=topic, **kw))
    snap = wait_job(pl, jid)
    assert snap["state"] == "done", snap.get("error")
    return snap


def _tmp() -> Path:
    """老包夹具：剥掉 stages.draft 的 elevator。

    方案 10 之后仓库里的包默认走合并路径（选题与正文一次调用）—— 那条路径
    **没有单独的 select 调用**，本文件测的"复用"对它无从谈起（合并路径上
    没有可以省掉的那一通调用，见 `pipeline._draft_once` 的 docstring）。
    选题复用是老包（select+write 两段）的机制，所以这里把包退回老形态，
    这批测试顺带成为「老包路径在合并包落地后仍然工作」的回归守护。
    """
    import yaml
    tmp = Path(tempfile.mkdtemp(prefix="talkscript-reuse-"))
    shutil.copytree(ROOT / "packs", tmp / "packs")
    sk = tmp / "packs" / "elevator" / "skill.yaml"
    data = yaml.safe_load(sk.read_text(encoding="utf-8"))
    data["stages"].pop("draft", None)
    sk.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
                  encoding="utf-8")
    return tmp


def test_identical_request_skips_the_select_call():
    """同参数再跑一次：不调模型，选题与上一次逐字相同，并且界面上看得见是复用的。"""
    tmp = _tmp()
    try:
        pl = _pipeline(tmp)
        first = _gen(pl)
        assert "select" in _steps(first) and "select_reuse" not in _steps(first)
        second = _gen(pl)
        keys = _steps(second)
        assert "select_reuse" in keys and "select" not in keys, keys
        assert second["result"]["plan"] == first["result"]["plan"], "复用的选题必须原样"
        # 撰写照旧跑：省掉的只有选题这一次
        assert any(k.startswith("write_r") for k in keys), keys
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_changing_anything_that_feeds_select_re_runs_it():
    """改主题 / 改细分 / 改风格 / 改时长 → 指纹变 → 重新选题。

    这四条各代表一类输入：主题（自由文本）、segment 与 style（切知识文件与语速）、
    duration（改 points 与配额）。少一条就是"哪一项进了指纹"没人守着。
    """
    tmp = _tmp()
    try:
        pl = _pipeline(tmp)
        _gen(pl)
        for kw in ({"topic": "老旧电梯怎么换"},
                   {"segment": "旧楼加装"},
                   {"style": "权威科普"},
                   {"duration": 30}):
            snap = _gen(pl, **kw)
            assert "select" in _steps(snap), f"{kw} 之后仍然复用了旧选题"
            assert "select_reuse" not in _steps(snap), _steps(snap)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_reroll_never_reuses():
    """「换一版」的语义就是要一个新角度 —— 命中缓存等于把这个按钮作废。"""
    tmp = _tmp()
    try:
        pl = _pipeline(tmp)
        _gen(pl)
        snap = _gen(pl, reroll=True)
        assert "select" in _steps(snap) and "select_reuse" not in _steps(snap), \
            _steps(snap)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_edited_knowledge_invalidates_the_cache():
    """改**被注入的那几节**知识 → 提示词变 → 必须重新选题。

    这条是"为什么不枚举参数"的正面证据：文件内容根本不在 params 里，
    枚举式键表看不见它 —— 而它是选题提示词的一部分。
    反向也测：改一节**不进提示词**的内容不该白白重跑（指纹只认渲染结果）。
    """
    tmp = _tmp()
    try:
        pl = _pipeline(tmp)
        _gen(pl)
        assert "select_reuse" in _steps(_gen(pl))          # 先证明会复用

        hooks = tmp / "packs" / "elevator" / "patterns" / "hooks.md"
        out_of_slice = hooks.read_text(encoding="utf-8") + \
            "\n\n## 附录：不注入的章节\n- 只在导出技能里给人看\n"
        hooks.write_text(out_of_slice, encoding="utf-8")
        time.sleep(0.05)                           # mtime 缓存按 (mtime, size) 失效
        assert "select_reuse" in _steps(_gen(pl)), \
            "改了不进提示词的章节，却白白重跑了一次选题"

        hooks.write_text(out_of_slice.replace("### 钩子禁用清单",
                                              "### 新增类型：本地场景钩子\n- 举例一行\n\n"
                                              "### 钩子禁用清单", 1), encoding="utf-8")
        time.sleep(0.05)
        snap = _gen(pl)
        assert "select" in _steps(snap) and "select_reuse" not in _steps(snap), \
            _steps(snap)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_cache_is_bounded():
    """缓存有上界：长跑的引擎不该攒住整段会话用过的所有选题。"""
    from app.schemas import TopicPlan
    tmp = _tmp()
    try:
        pl = _pipeline(tmp)
        plan = TopicPlan(angle="a", hook_type="反常识", hook_line="h",
                         points=["p"], cta="关注")
        for i in range(Pipeline.PLAN_CACHE_MAX + 12):
            pl._remember_plan(f"k{i}", plan)
        assert len(pl._plan_cache) == Pipeline.PLAN_CACHE_MAX
        assert "k0" not in pl._plan_cache and f"k{Pipeline.PLAN_CACHE_MAX + 11}" \
            in pl._plan_cache, "淘汰顺序应是先进先出"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


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


# ── single-flight：同一指纹并发时只让一条去调模型 ──────────────
class _GateClient:
    """假客户端：第一条卡在 gate 上，第二条必须复用它的结果而不是再打一次。"""

    def __init__(self, gate):
        self.cfg = SimpleNamespace(model="fake-model", temperature=0.7,
                                   max_tokens=1000, base_url="http://fake/v1")
        self.gate = gate
        self.calls = 0
        self.entered = threading.Event()

    def chat_json(self, task, system, user, model_cls, **kw):
        self.calls += 1
        self.entered.set()
        self.gate.wait(5)
        return TopicPlan(angle=f"角度{self.calls}", hook_type="h", hook_line="l",
                         points=["p"], cta="c")


def _flight(tmp, client_cls=_GateClient):
    """起两条同参数并发生成，返回 (pipeline, 假客户端, 结果, 两条作业)。"""
    pl = _pipeline(tmp)
    pack = Pack(tmp, "elevator")
    skill = pack.skill()
    p = pl._normalize(pack, {"topic": "家用电梯怎么选", "duration": 60,
                             "platform": "抖音"})
    gate = threading.Event()
    client = client_cls(gate)
    out, jobs = {}, [Job("f-1", "generate", {}), Job("f-2", "generate", {})]

    def run(tag, job):
        try:
            out[tag] = pl._select(job, pack, skill, p, client)
        except Exception as e:   # noqa: BLE001
            out[tag] = e
    t1 = threading.Thread(target=run, args=("a", jobs[0]))
    t1.start()
    assert client.entered.wait(2), "第一条没进入选题调用"
    t2 = threading.Thread(target=run, args=("b", jobs[1]))
    t2.start()
    time.sleep(0.3)                       # 让第二条走到等待
    gate.set()
    t1.join(10); t2.join(10)
    return pl, client, out, jobs


def test_concurrent_same_fingerprint_only_calls_once():
    """白烧一次 4000 token 的调用是这条缓存存在的理由，并发路径不能漏。"""
    tmp = _tmp()
    try:
        pl, client, out, jobs = _flight(tmp)
        assert client.calls == 1, f"同一指纹打了 {client.calls} 次模型，single-flight 失效"
        assert out["a"].angle == out["b"].angle == "角度1"
        keys = _steps(jobs[1].snapshot())
        assert "select_reuse" in keys, "复用别人选题的那条必须在日志里说出来"
        assert "select" not in keys, "记一步「选题策划」等于告诉用户它又想了一遍"
        assert _steps(jobs[0].snapshot()).count("select") == 1
        assert pl._plan_flight == {}, "飞行槽没释放"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_flight_released_when_leader_fails():
    """leader 抛错时等待者退回自己打一次，且不留下卡死的飞行槽。"""
    class Boom(_GateClient):
        def chat_json(self, task, system, user, model_cls, **kw):
            self.calls += 1
            self.entered.set()
            self.gate.wait(5)
            raise ValueError("上游炸了")

    tmp = _tmp()
    try:
        pl, client, out, jobs = _flight(tmp, Boom)
        assert client.calls >= 2, "leader 失败后等待者该自己打一次"
        assert all(isinstance(v, Exception) for v in out.values()), out
        assert pl._plan_flight == {}, "leader 抛错也必须释放飞行槽"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_reroll_never_joins_a_flight():
    """「换一版」的语义就是要一个新角度：不参与缓存、也不许搭别人的飞行槽。"""
    tmp = _tmp()
    try:
        pl = _pipeline(tmp)
        pack = Pack(tmp, "elevator")
        skill = pack.skill()
        p = pl._normalize(pack, {"topic": "家用电梯怎么选", "duration": 60,
                                 "platform": "抖音", "reroll": True})
        gate = threading.Event()
        gate.set()
        client = _GateClient(gate)
        j = Job("f-3", "generate", {})
        pl._select(j, pack, skill, p, client)
        pl._select(j, pack, skill, p, client)
        assert client.calls == 2, "reroll 走缓存了"
        assert pl._plan_cache == {} and pl._plan_flight == {}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
