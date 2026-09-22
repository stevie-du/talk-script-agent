# -*- coding: utf-8 -*-
"""批次 10 复核（三条 lane 独立审计）查出的引擎缺陷，每条一个跑出来的用例。

修的都是同一族：**作业停在忙态不终态 = 永久占一个并发额度**、
**上游"活着但不给字"的流取消不掉**、**预算升级反成降级**、
**空产物以 done 收场**。审计方式与结论见主报告 §15。

约定（本项目的老教训）：每条用例都要能在**修复前**红。写不出这种用例的
"修复"不算修完。
"""
from __future__ import annotations

import dataclasses
import json
import re
import shutil
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import LLMConfig, load_config                       # noqa: E402
from app.jobs import (JOB_BUDGET_SECONDS, Job, JobBudget, JobCancelled,  # noqa: E402
                       TERMINAL_STATES)
from app.llm import RETRY_UPGRADE_CAP, LLMClient, LLMError, _brief    # noqa: E402
from app.packgen import claim_slug, preview_slug, release_slug       # noqa: E402
from app.pipeline import (MAX_CONCURRENT_JOBS, Pipeline, ScriptDraft,  # noqa: E402
                          SegmentRewrite, _readable_error, _safe_str, wait_job)
from app.schemas import GenerateRequest, RewriteSegmentRequest    # noqa: E402


CFG = load_config(ROOT, ROOT)


def _pipeline(tmp_path: Path) -> Pipeline:
    """一份干净的临时根（带真 packs/），客户端走夹具通道。"""
    shutil.copytree(ROOT / "packs", tmp_path / "packs")
    pl = Pipeline(tmp_path, CFG, data_dir=tmp_path)
    pl.llm = LLMClient(dataclasses.replace(CFG.llm, api_key="MOCK"))
    return pl


def _req(**kw) -> GenerateRequest:
    base = dict(pack="elevator", topic="家用电梯怎么选", duration=60, platform="抖音")
    base.update(kw)
    return GenerateRequest(**base)


def _client(**over) -> LLMClient:
    return LLMClient(dataclasses.replace(CFG.llm, base_url="http://x/v1",
                                         api_key="k", model="m",
                                         timeout=30.0, retries=0,
                                         max_tokens=1000, **over))


# ── P1-45 / P1-46：预算与"线程起不来"都不许把作业留在忙态 ─────────
def test_packgen_budget_overdue_does_not_strand_the_job(tmp_path, monkeypatch):
    """建包超预算时报的是 JobCancelled（packgen 的旧形态），作业却不能停在 packing。

    停在忙态不是"显示有点旧"：`prune()` 只回收终态，这个槽就永久没了 ——
    实测四个这样的作业之后，引擎在**一个都没在跑**的情况下一直回 409。
    """
    pl = _pipeline(tmp_path)
    job = Job("pg-1", "packgen", {"industry": "猫咖", "description": "小店"})
    pl.add_job(job)

    def fake_create_pack(*a, **kw):
        # 模型跑完的那一刻预算已经用尽，而 packgen 只会报"已取消"
        job.started_at = time.time() - JOB_BUDGET_SECONDS - 1
        raise JobCancelled("已取消")

    monkeypatch.setattr("app.pipeline.create_pack", fake_create_pack)
    pl._run_packgen(job, pl.llm, "cat-cafe")
    assert job.state == "failed", f"停在 {job.state}：这个并发额度再也不会回来了"
    assert "分钟" in (job.error or ""), job.error
    assert pl.registry.running_count() == 0


def test_genuine_cancel_stays_cancelled(tmp_path, monkeypatch):
    """真取消不该被翻成失败：那是用户自己点的停止。"""
    pl = _pipeline(tmp_path)
    job = Job("pg-2", "packgen", {"industry": "猫咖", "description": "小店"})
    pl.add_job(job)
    monkeypatch.setattr("app.pipeline.create_pack",
                        lambda *a, **kw: (_ for _ in ()).throw(JobCancelled("已取消")))
    job.request_cancel()
    pl._run_packgen(job, pl.llm, "cat-cafe")
    assert job.state == "cancelled", job.state
    assert not job.error


def test_slug_and_slot_released_when_the_thread_wont_start(tmp_path, monkeypatch):
    """`_spawn` 起不来（线程资源耗尽）时，占住的名字与额度都必须归还。

    注意打的是 `Thread.start` 而不是 `Pipeline._spawn`：要测的正是 `_spawn`
    里面那道收尾，桩掉它等于把被测代码一起删了（这样写会得到一个假绿）。
    """
    pl = _pipeline(tmp_path)

    class BoomThread(threading.Thread):
        def start(self):                          # noqa: D102
            raise RuntimeError("线程起不来")

    monkeypatch.setattr("app.pipeline.threading.Thread", BoomThread)
    for _ in range(2):        # 第二次不该变成 FileExistsError（名字漏还）
        with pytest.raises(RuntimeError):
            pl.start_packgen("猫咖", "小店")
    assert pl.registry.running_count() == 0
    # 名字确实还回来了：同一个 slug 再占一次能成功（修复前永远占不到）
    slug = preview_slug("猫咖")
    assert claim_slug(slug)
    release_slug(slug)


def test_generate_dir_failure_does_not_eat_the_quota(tmp_path, monkeypatch):
    """`store.job_dir` 抛错（磁盘满/权限）连打 5 次，第 5 次也不该是「已达上限」。

    修复前每次失败都留一条 `queued` 僵尸：4 个槽被占满，之后所有生成被 409
    拒掉，而注册表里一个在跑的作业都没有。
    """
    pl = _pipeline(tmp_path)

    def boom(*a, **kw):
        raise OSError("磁盘满")

    monkeypatch.setattr(pl.store, "job_dir", boom)
    for i in range(MAX_CONCURRENT_JOBS + 1):
        with pytest.raises(OSError):
            pl.start_generate(_req())
        assert pl.registry.running_count() == 0, f"第 {i + 1} 次漏了额度"


# ── P1-45：一条"活着但一个字都不给"的流必须能取消 ────────────────
def _silent_sse(n: int = 400) -> bytes:
    """只有空 delta 与心跳注释的流：`on_delta` 一次都不会被调用。"""
    out = []
    for _ in range(n):
        out.append('data: {"choices":[{"delta":{}}]}\n\n')
        out.append(": keep-alive\n\n")
    out.append("data: [DONE]\n\n")
    return "".join(out).encode()


def _stream_client(monkeypatch, body: bytes):
    transport = httpx.MockTransport(lambda req: httpx.Response(
        200, content=body, headers={"content-type": "text/event-stream"}))
    monkeypatch.setattr("app.llm.get_client", lambda: httpx.Client(transport=transport))
    return _client()


def test_silent_stream_honours_cancel_at_once(monkeypatch):
    c = _stream_client(monkeypatch, _silent_sse())
    job = Job("st-1", "generate", {})
    seen = {"n": 0}

    def gate():
        seen["n"] += 1
        if seen["n"] >= 5:
            job.request_cancel()
        Pipeline._stop_check(job)

    with pytest.raises(JobCancelled):
        c.chat_json("write", "s", "u", ScriptDraft,
                    on_delta=lambda kind, text: None, should_abort=gate)
    # 修复前：取消查不到（on_delta 从未触发），400 行收满才报"空内容"
    assert seen["n"] == 5, f"收包循环没有在每一行检查：{seen['n']}"


def test_silent_stream_honours_the_job_budget(monkeypatch):
    c = _stream_client(monkeypatch, _silent_sse())
    job = Job("st-2", "generate", {})
    n = {"i": 0}

    def gate():
        n["i"] += 1
        if n["i"] >= 3:
            job.started_at = time.time() - JOB_BUDGET_SECONDS - 1
        Pipeline._stop_check(job)

    with pytest.raises(Exception) as ei:      # JobBudget（不是空内容、不是取消）
        c.chat_json("write", "s", "u", ScriptDraft,
                    on_delta=lambda kind, text: None, should_abort=gate)
    assert type(ei.value).__name__ == "JobBudget"
    assert n["i"] == 3


# ── P2-1 复现：预算"升级"不能变成降级 ───────────────────────────
def _json_body(content: str, finish: str) -> bytes:
    return json.dumps({"choices": [{"message": {"content": content},
                                    "finish_reason": finish}],
                       "usage": {"prompt_tokens": 10, "completion_tokens": 20}}).encode()


def _post_client(monkeypatch, handler):
    monkeypatch.setattr("app.llm.get_client",
                        lambda: httpx.Client(transport=httpx.MockTransport(handler)))
    return _client()


def test_truncation_retry_never_lowers_an_above_cap_budget(monkeypatch):
    """`max_tokens` 可配到 200000，而升级上限写死 20000。

    修复前 60000 → min(90000, 20000) = **20000**：一次注定再次截断的降级重发，
    而且日志里写着"已把输出预算提高到 20000"（把它当成上调）。
    """
    sent: list[dict] = []
    notes: list[str] = []

    def handler(req):
        sent.append(json.loads(req.content.decode()))
        return httpx.Response(200, content=_json_body('{"sections":[', "length"))

    c = _post_client(monkeypatch, handler)
    c.cfg = dataclasses.replace(c.cfg, max_tokens=60000)
    with pytest.raises(Exception):
        c.chat_json("write", "s", "u", ScriptDraft, max_retries=1,
                    on_retry=lambda note, a, t: notes.append(note))
    assert len(sent) == 2, sent
    assert sent[1]["max_tokens"] >= 60000, \
        f"截断重发把预算从 60000 降到 {sent[1]['max_tokens']}"
    assert any("提高到" not in n for n in notes), f"文案把它说成了一次上调：{notes}"


def test_truncation_retry_still_escalates_below_the_cap(monkeypatch):
    """反向守卫：正常档（低于上限）必须**真的**升上去，别把这条支一起砍死。"""
    sent: list[dict] = []

    def handler(req):
        sent.append(json.loads(req.content.decode()))
        return httpx.Response(200, content=_json_body('{"sections":[', "length"))

    c = _post_client(monkeypatch, handler)
    with pytest.raises(Exception):
        c.chat_json("write", "s", "u", ScriptDraft, max_retries=1)
    assert [s["max_tokens"] for s in sent] == [1000, 1500], sent


def test_empty_content_at_cap_says_so_instead_of_lying(monkeypatch):
    """到顶时那句"已升到升级上限"必须是真升过；否则就是编造一个没发生的动作。"""
    sent: list[dict] = []
    notes: list[str] = []

    def handler(req):
        sent.append(json.loads(req.content.decode()))
        return httpx.Response(200, content=_json_body("   ", "stop"))

    c = _post_client(monkeypatch, handler)
    c.cfg = dataclasses.replace(c.cfg, max_tokens=RETRY_UPGRADE_CAP)
    with pytest.raises(Exception) as ei:
        c.chat_json("write", "s", "u", ScriptDraft, max_retries=2,
                    on_retry=lambda note, a, t: notes.append(note))
    assert len(sent) == 1, f"同一份 payload 又发了一遍：{len(sent)} 次"
    assert "无可升空间" in str(ei.value), str(ei.value)


# ── P1-47：没有正文的产物不能以 done 收场 ───────────────────────
def test_finalize_refuses_an_empty_script(tmp_path, monkeypatch):
    """修复前 `{"sections": []}` 一路走到落盘：状态 done、产物空、历史字数 0。

    「正文为空」不等于「正文合格」—— 检查器在容差够宽时对空稿回 passed=True，
    于是这次"成功"在界面上就是一次成功。
    """
    pl = _pipeline(tmp_path)
    monkeypatch.setattr(Pipeline, "_write_with_recheck",
                        lambda self, *a, **kw: ({"sections": []}, []))
    jid = pl.start_generate(_req(format="voice"))
    snap = wait_job(pl, jid, timeout=90)
    assert snap["state"] == "failed", snap["state"]
    assert "没有产出任何可读正文" in (snap["error"] or ""), snap["error"]
    assert not list((tmp_path / "generated").glob("*/*/result.json")), "空产物照样落了盘"


def test_rewrite_with_empty_text_keeps_the_original(tmp_path, monkeypatch):
    """单段重写返回空正文时，原文一个字都不许被覆盖（P1-44 的第三条腿）。"""
    pl = _pipeline(tmp_path)
    jid = pl.start_generate(_req(format="voice"))
    assert wait_job(pl, jid, timeout=90)["state"] == "done"
    disk = next((tmp_path / "generated").glob("*/*/result.json"))
    before = json.loads(disk.read_text(encoding="utf-8"))["sections"][0]["text"]

    class EmptyRewrite(LLMClient):
        def chat_json(self, task, *a, **kw):
            return SegmentRewrite(text="   ", subtitle="")

    pl.llm = EmptyRewrite(CFG.llm)
    pl.rewrite_segment(jid, RewriteSegmentRequest(index=0, feedback="短一点"))
    snap = wait_job(pl, jid, timeout=90)
    assert snap["state"] == "failed", snap["state"]
    assert "空正文" in (snap["error"] or ""), snap["error"]
    after = json.loads(disk.read_text(encoding="utf-8"))["sections"][0]["text"]
    assert after == before, "空的重写稿把合格的那一段抹掉了"


# ── P3：选题缓存指纹必须认服务地址 ──────────────────────────────
def test_plan_key_includes_base_url():
    """同名模型在不同服务商那儿不是一份权重；只算 model 名会静默复用上一家的选题。"""
    a = LLMClient(dataclasses.replace(CFG.llm, base_url="https://api.A.com/v1",
                                      model="deepseek-chat"))
    b = LLMClient(dataclasses.replace(CFG.llm, base_url="https://api.B.com/v1",
                                      model="deepseek-chat"))
    assert Pipeline._plan_key(a, "s", "u") != Pipeline._plan_key(b, "s", "u")
    assert Pipeline._plan_key(a, "s", "u") == Pipeline._plan_key(a, "s", "u")


# ── P3：limits.recheck_rounds 的每种写法都要留同一条痕 ──────────
def _rounds_steps(tmp_path, monkeypatch, raw: str) -> list[str]:
    pl = _pipeline(tmp_path)
    skill = tmp_path / "packs" / "elevator" / "skill.yaml"
    text = skill.read_text(encoding="utf-8")
    new, n = re.subn(r"(?m)^(\s*recheck_rounds:\s*).*$", lambda m: m.group(1) + raw, text)
    assert n >= 1, "模板里找不到 recheck_rounds，这条用例没在量任何东西"
    skill.write_text(new, encoding="utf-8")
    jid = pl.start_generate(_req(format="voice"))
    snap = wait_job(pl, jid, timeout=90)
    return [s["key"] for s in snap["steps"]]


@pytest.mark.parametrize("raw,expect", [
    ("0", "limits_zero"),
    ('"0"', "limits_zero"),        # 引号里的 0 同样是 0：修复前一条痕都不留
    ("2.9", "limits_bad"),
    ('"2.9"', "limits_bad"),
    ("true", "limits_bad"),        # 布尔会被 int() 安静读成 1 轮
    ("-1", "limits_bad"),
])
def test_recheck_rounds_writes_leave_traces(tmp_path, monkeypatch, raw, expect):
    assert expect in _rounds_steps(tmp_path, monkeypatch, raw), \
        f"recheck_rounds: {raw} 跑完没有任何痕迹"


def test_valid_recheck_rounds_stays_quiet(tmp_path, monkeypatch):
    """反向：配得对（整数 2）不该有噪声步骤。"""
    steps = _rounds_steps(tmp_path, monkeypatch, "2")
    assert not [k for k in steps if k.startswith("limits_")], steps


# ── P3：快照是"某一时刻"，不是活对象的别名 ──────────────────────
# ── P3：参数选了包里没有的选项，要看得见地在降级 ─────────────────
def test_unknown_option_degrades_visibly(tmp_path):
    """不认识的 segment 意味着「这个细分的知识一个字都不注入」，修复前毫无痕迹。"""
    from app.knowledge import Pack
    pl = _pipeline(tmp_path)
    valid = Pack(tmp_path, "elevator").param_options("segment")[1]

    jid = pl.start_generate(_req(segment="根本不存在的细分", format="voice"))
    snap = wait_job(pl, jid, timeout=90)
    steps = {s["key"]: s for s in snap["steps"]}
    assert "params_unmatched" in steps, f"静默降级：{sorted(steps)}"
    assert steps["params_unmatched"]["data"]["unmatched"]["segment"] == "根本不存在的细分"

    jid2 = pl.start_generate(_req(segment=valid, format="voice"))
    snap2 = wait_job(pl, jid2, timeout=90)
    assert not [s for s in snap2["steps"] if s["key"] == "params_unmatched"], \
        "合法选项也报降级 = 噪声，用户很快就不看这些提示了"


def test_snapshot_is_detached_from_the_live_job():
    """HTTP 层在锁外把快照序列化成 JSON —— 交活动引用会被工作线程当场改掉。"""
    job = Job("sn-1", "generate", {"a": 1})
    snap = job.snapshot()
    job.steps.append({"key": "late"})
    job.params["a"] = 2
    assert snap["steps"] == [], "快照里的 steps 是活引用"
    assert snap["params"]["a"] == 1, "快照里的 params 是活引用"


# ── P3：上游结构畸形要报"结构异常"，不是一句英文 TypeError ────────
def test_non_dict_choice_raises_a_readable_llm_error(monkeypatch):
    def handler(req):
        return httpx.Response(200, json={"choices": ["不是对象"]})

    c = _post_client(monkeypatch, handler)
    with pytest.raises(LLMError) as ei:
        c._complete([{"role": "user", "content": "u"}])
    assert "结构异常" in str(ei.value), str(ei.value)


def test_symbol_only_industry_name_is_refused_before_any_spend(tmp_path):
    """纯符号的行业名起不出目录名 —— 修复前回落成 "custom"，两个不同的名字共用一个目录。"""
    pl = _pipeline(tmp_path)
    for name in ("？？？", "!!!", "———"):
        with pytest.raises(ValueError) as ei:
            pl.start_packgen(name, "描述")
        assert "目录名" in str(ei.value), str(ei.value)
    assert pl.registry.running_count() == 0, "被拒的名字不该占住额度"


def test_empty_section_for_a_valid_option_is_said_during_the_job(tmp_path):
    """选项对得上、但包里那一节是空的 → 作业里必须有一步说"这份知识没注入"。

    修复前这件事只有行业包卡片上说一句，作业里毫无痕迹：产物看着完全正常，
    只是整个细分行业的知识从头到尾没进过模型（P1-3 / P2-7）。
    """
    from app.knowledge import Pack
    pl = _pipeline(tmp_path)
    pk = Pack(tmp_path, "elevator")
    seg = pk.param_options("segment")[1]
    topics = tmp_path / "packs" / "elevator" / "knowledge" / "topics.md"
    text = topics.read_text(encoding="utf-8")
    # 把选中的那一节正文清空（保留标题）——"这一节存在"但"没有知识"是同一种缺
    lines = text.split("\n")
    out, hit, found = [], False, False
    for ln in lines:
        if ln.startswith("## ") and seg in ln:
            hit = found = True
            out.append(ln)
            out.append("")
            continue
        if hit and ln.startswith("## "):
            hit = False
        if hit:
            continue
        out.append(ln)
    # 前置条件本身要成立：没找到那节就什么都没清空，后面的"该留痕"会以
    # 一个不相干的原因失败（原来这行写着 `assert hit or True` —— 永远为真，
    # 等于没断言；⚠ 也不能直接 `assert hit`：hit 是"当前在不在目标节内"的状态位，
    # 扫到下一个标题就被清掉，循环结束时必然为 False，那是断言自己写错）。
    assert found, f"没在 topics.md 里找到「{seg}」那一节，前面的改写等于没做任何事"
    topics.write_text("\n".join(out), encoding="utf-8")

    jid = pl.start_generate(_req(segment=seg, format="voice"))
    snap = wait_job(pl, jid, timeout=90)
    notes = [s for s in snap["steps"] if s["key"] == "knowledge_empty"]
    assert notes, f"空章节静默通过：{[s['key'] for s in snap['steps']]}"
    assert any(seg in n for n in notes[0]["data"]["notes"]), notes[0]["data"]

    # 反向：健康的包不该有这一步
    pl2 = _pipeline(tmp_path / "ok")
    jid2 = pl2.start_generate(_req(segment=seg, format="voice"))
    snap2 = wait_job(pl2, jid2, timeout=90)
    assert not [s for s in snap2["steps"] if s["key"] == "knowledge_empty"], snap2["steps"]


def test_rewrite_client_lookup_failure_does_not_strand_the_job(tmp_path, monkeypatch):
    """第四条漏槽路径（批次 10 复核 lane 实测）：`self.llm` 在额度之后、try 之外取。

    `self.llm` 是惰性构建的（要读 data_dir 下的配置），所以它**会**抛：
    作业这时已经迁到 rewriting 并占住额度，原样抛出就永久停在「重写中」。
    """
    pl = _pipeline(tmp_path)
    jid = pl.start_generate(_req(format="voice"))
    assert wait_job(pl, jid, timeout=90)["state"] == "done"

    monkeypatch.setattr(Pipeline, "llm", property(lambda self: (_ for _ in ()).throw(
        RuntimeError("配置读不出来"))))
    with pytest.raises(RuntimeError):
        pl.rewrite_segment(jid, RewriteSegmentRequest(index=0, feedback="短一点"))
    job = pl.get_job(jid)
    assert job.state == "failed", f"停在 {job.state}：这个额度又永久没了"
    assert pl.registry.running_count() == 0


def test_snapshot_does_not_alias_the_live_result_and_steps():
    """已发出的快照不许再变（HTTP 层在锁外序列化它）。"""
    job = Job("sn-2", "generate", {"a": 1})
    job.transition("done", force=True, result={"sections": [{"text": "x"}],
                                               "check": {"passed": True}})
    job.steps.append({"key": "select", "title": "选题", "data": {"plan": {}}})
    snap = job.snapshot()
    job.steps.append({"key": "late", "title": "迟到的一步", "data": {}})
    job.steps[0]["data"]["plan"] = {"angle": "改过了"}
    job.result["sections"] = []
    assert len(snap["steps"]) == 1, "steps 列表是活动引用"
    assert snap["steps"][0]["data"] == {"plan": {}}, "步骤的 data 是活动引用"
    assert snap["result"]["sections"], "result 是活动引用"


def test_finalize_refuses_a_placeholder_only_script(tmp_path, monkeypatch):
    """四段全是 {{待补}} 的稿子与空稿是同一件事：不能以 done 收场。"""
    pl = _pipeline(tmp_path)
    only_ph = {"sections": [{"type": "hook", "text": "{{待补：钩子}}"},
                            {"type": "point", "text": "{{待补：报价}}　 \u3000"},
                            {"type": "cta", "text": "{{待补：CTA}}"}]}
    monkeypatch.setattr(Pipeline, "_write_with_recheck",
                        lambda self, *a, **kw: (dict(only_ph), []))
    jid = pl.start_generate(_req(format="voice"))
    snap = wait_job(pl, jid, timeout=90)
    assert snap["state"] == "failed", snap["state"]
    assert "占位" in (snap["error"] or ""), snap["error"]
    assert not list((tmp_path / "generated").glob("*/*/result.json")), "半成品落了盘"


def test_read_timeout_is_capped_by_the_job_deadline():
    """一条什么都不吐的流不许把预算撑到 20 分钟之外（P2-48）。"""
    import time as _t
    from app.llm import LLMClient as C, STREAM_READ_TIMEOUT_MULT
    base = 180.0 * STREAM_READ_TIMEOUT_MULT
    assert C._capped_read(base, None) == base
    assert abs(C._capped_read(base, _t.time() + 5) - 5) < 1
    assert C._capped_read(base, _t.time() - 99) == 1.0, "到点也要留一次网络往返的量"


def test_shipped_pack_produces_no_degradation_noise(tmp_path):
    """反向守卫：新加的 `params_unmatched` / `knowledge_empty` 不许在真包的正常用法里冒出来。

    报警一旦误伤健康包，用户很快就学会不看这些提示 —— 那比没有提示更糟。
    （本轮实测过 16 组 segment×audience 全净；这里留两组，够住回归。）
    """
    from app.knowledge import Pack
    pl = _pipeline(tmp_path)
    pk = Pack(tmp_path, "elevator")
    segs = pk.param_options("segment")[:2]
    auds = pk.param_options("audience")[:2]
    for seg in segs:
        for aud in auds:
            jid = pl.start_generate(_req(segment=seg, audience=aud, format="voice"))
            snap = wait_job(pl, jid, timeout=90)
            keys = [s["key"] for s in snap["steps"]]
            assert snap["state"] == "done", (seg, aud, snap["error"])
            assert not [k for k in keys
                        if k in ("params_unmatched", "knowledge_empty")], (seg, aud, keys)


def test_disk_failure_still_leaves_a_durable_failed_record(tmp_path, monkeypatch):
    """产物写坏时不能把记录一起带走：墓碑会拦住随后的 job.json 落盘。

    批次 10 复核实测：`_finalize` 的 except 里调 `store.delete()` → 立墓碑 →
    `_fail` 的 `_persist` 返回 False → 这条「产物落盘失败」只活在内存里，
    重启就查无此事。现在半个产物由 `write_result` 自己回滚，删除不再发生。
    """
    pl = _pipeline(tmp_path)
    monkeypatch.setattr("app.store.render_script_md",
                        lambda r: (_ for _ in ()).throw(OSError("磁盘满")))
    jid = pl.start_generate(_req(format="voice"))
    snap = wait_job(pl, jid, timeout=90)
    assert snap["state"] == "failed", snap["state"]
    assert "落盘失败" in (snap["error"] or ""), snap["error"]
    dirs = list((tmp_path / "generated").glob("*/*/"))
    assert dirs, "失败记录整个没了：连目录都没留下"
    assert not any((d / "result.json").exists() for d in dirs), "半个产物留在了盘上"
    assert any((d / "job.json").exists() for d in dirs), \
        "没有 job.json：这条失败在重启后就查无此事"


def test_read_timeout_after_the_budget_reports_the_budget(monkeypatch):
    """到点之后的网络错要报「预算用尽」，不是「连接失败」让人去查网线。"""
    def boom(req):
        raise httpx.ReadTimeout("timed out")

    monkeypatch.setattr("app.llm.get_client", lambda: httpx.Client(
        transport=httpx.MockTransport(boom)))
    c = _client()
    c.cfg = dataclasses.replace(c.cfg, retries=0)
    job = Job("rl-1", "generate", {})
    fired = {"now": False}

    def gate():
        if fired["now"]:
            job.started_at = time.time() - JOB_BUDGET_SECONDS - 1
        Pipeline._stop_check(job)

    fired["now"] = True          # 请求发出之后预算才到期（模拟"卡在读上"）
    with pytest.raises(JobBudget):
        c.chat_json("write", "s", "u", ScriptDraft, should_abort=gate)


def test_failed_overwrite_restores_the_previous_artifact(tmp_path, monkeypatch):
    """写一半失败要回到调用前的样子 —— 尤其**覆盖写**：撤错东西比不撤更糟。

    `write_result` 不只服务新建：单段重写与"取消后回滚"都是在一份已经存在的
    合格产物上覆盖（pipeline 两处调用点）。修复前第二笔写失败会把第一笔撤走，
    于是用户从"有一版好的"变成"什么都没有"。
    """
    from app.store import ArtifactStore
    st = ArtifactStore(tmp_path, data_dir=tmp_path)
    d = st.job_dir("J-overwrite", "2026-09-22T07:00:00")
    (d / "result.json").write_text("OLD-GOOD-VERSION", encoding="utf-8")
    monkeypatch.setattr(st, "_summary_from_result", lambda r: {"id": "J-overwrite"})

    def boom(_result):
        raise OSError("磁盘满")

    monkeypatch.setattr("app.store.render_script_md", boom)
    with pytest.raises(OSError):
        st.write_result({"sections": []}, d)
    assert (d / "result.json").read_text(encoding="utf-8") == "OLD-GOOD-VERSION", \
        "覆盖写失败把上一版合格产物弄丢了"

    # 新建那一侧：本来没有文件，失败后也不该留下半成品
    d2 = st.job_dir("J-fresh", "2026-09-22T07:00:00")
    with pytest.raises(OSError):
        st.write_result({"sections": []}, d2)
    assert not (d2 / "result.json").exists()


def test_cancel_during_backoff_is_noticed_immediately(monkeypatch):
    """退避那几十秒里点的「停止」不该等睡完才生效（P2-49）。

    修复前是一把 `time.sleep(wait)`：`request_cancel()` 立刻把状态落成 cancelled
    （界面按钮已经变了），线程却还要睡满 Retry-After（封顶 60s）才走到下一个
    检查点 —— 期间它还在占一个并发额度，而用户看到的是"点了停止没反应"。
    """
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(429, headers={"Retry-After": "30"}, json={"error": "busy"})

    monkeypatch.setattr("app.llm.get_client", lambda: httpx.Client(
        transport=httpx.MockTransport(handler)))
    c = _client()
    c.cfg = dataclasses.replace(c.cfg, retries=3)
    job = Job("bk-1", "generate", {})
    gate_calls = {"n": 0}

    def gate():
        gate_calls["n"] += 1
        if gate_calls["n"] >= 2:        # 第一段睡（0.5s）之后用户点了停止
            job.request_cancel()
        Pipeline._stop_check(job)

    t0 = time.time()
    with pytest.raises(JobCancelled):
        c.chat_json("write", "s", "u", ScriptDraft, should_abort=gate)
    spent = time.time() - t0
    assert spent < 3.0, f"等满退避才收工：{spent:.1f}s"
    assert calls["n"] == 1, calls


def test_backoff_never_overshoots_the_deadline(monkeypatch):
    """退避要认预算：`Retry-After: 60` 不能把一条只剩 2 秒的作业再拖 60 秒（P2-49）。

    修复前 `_capped_read` 只压读超时，`time.sleep(wait)` 照睡不误；
    `retries=3` 时最坏 ~3×60s —— 比整作业预算还长，而这段时间槽一直被占。
    """
    def handler(request):
        return httpx.Response(429, headers={"Retry-After": "60"}, json={"error": "busy"})

    monkeypatch.setattr("app.llm.get_client", lambda: httpx.Client(
        transport=httpx.MockTransport(handler)))
    c = _client()
    c.cfg = dataclasses.replace(c.cfg, retries=3)
    dl = time.time() + 2.0

    def gate():
        if time.time() >= dl:
            raise JobBudget(JOB_BUDGET_SECONDS)

    t0 = time.time()
    with pytest.raises(JobBudget):
        c.chat_json("write", "s", "u", ScriptDraft, should_abort=gate, deadline=dl)
    spent = time.time() - t0
    assert spent < 6.0, f"睡满了 Retry-After 才收工：{spent:.1f}s"


def test_readable_error_keeps_the_underlying_cause():
    """到点抛出的预算异常，不能把"其实一开始就连不上"这个真因弄丢。"""
    from app.pipeline import _readable_error
    try:
        try:
            raise OSError("Connection refused")
        except OSError as inner:
            raise JobBudget(JOB_BUDGET_SECONDS) from inner
    except JobBudget as outer:
        msg = _readable_error(outer)
    assert "分钟" in msg, msg                      # 结论还在
    assert "Connection refused" in msg, f"真因没人说了：{msg}"


def test_readable_error_does_not_print_the_cause_twice():
    """外层已经把原文塞进消息时，不再补"上一步"——同一件事说两遍是噪声。"""
    from app.pipeline import _readable_error
    inner = ConnectionError("unreachable host")
    outer = RuntimeError(f"模型接口连接失败（已重试 2 次）：{inner}")
    outer.__cause__ = inner
    msg = _readable_error(outer)
    assert "上一步" not in msg, msg
    # 类型名已经出现过的也一样（外层写的是 `ConnectionError: …`，
    # 再补一句"上一步：…"只是把同一个异常的名字念两遍）
    outer2 = RuntimeError("ConnectionError: 连不上")
    outer2.__context__ = ConnectionError("连不上")
    assert "上一步" not in _readable_error(outer2)


def test_terminal_states_cover_the_busy_states():
    """守卫：新增忙态却忘了给它通往终态的迁移，上面的漏槽修复就全部失效。"""
    from app.jobs import BUSY_STATES, TRANSITIONS
    for s in BUSY_STATES | {"queued"}:
        assert "failed" in TRANSITIONS[s] or s == "failed", f"{s} 到不了 failed"


# ── 生成链路上每一次模型调用都必须带齐三个闸门参数（漏一个是整族回归）──
def test_every_generate_path_llm_call_carries_deadline_abort_and_usage():
    """`app/pipeline.py` 里每个 `chat_json` 都要传 `deadline` / `should_abort` / `usage`。

    三者各守一个已经复测过的缺陷：少 `deadline` 就是"一条只发心跳的流最坏挂 48 分钟"
    （P2-48，整作业预算管不住读超时）；少 `should_abort` 就是取消与预算传不进网络层
    （P1-45）；少 `usage` 则这条调用的 token 不进作业 —— 分镜/重写原先正是这一格，
    于是"这次生成花了多少 token"永远少算两次调用（P3-15）。
    新增调用点漏参 → 这条先红，而不是等用户遇到挂死或界面少算。
    ⚠ 只覆盖 `pipeline.py`：`packgen.create_pack` 带齐了前两个，第三个（usage）刻意不查 ——
      它的返回里没有消费方，而为记 token 单开一个可见步骤属于界面语义发明，等产品决定。
    """
    import ast
    tree = ast.parse((ROOT / "app" / "pipeline.py").read_text(encoding="utf-8"))
    missing: list[str] = []
    found = 0
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "chat_json"):
            continue
        found += 1
        kws = {k.arg for k in node.keywords}
        for kw in ("deadline", "should_abort", "usage"):
            if kw not in kws:
                missing.append(f"pipeline.py:{node.lineno} 缺 {kw}")
    assert found >= 4, f"只找到 {found} 个 chat_json 调用点，守卫该改写法了"
    assert not missing, "生成链路的模型调用缺参数：" + "；".join(missing)


# ── P3-15 运行时那一半：作业步骤里真的带上 token 用量（AST 守卫管不到的部分）──
def test_every_llm_step_of_a_real_job_carries_usage(tmp_path):
    """真跑一次生成：凡是对外的模型调用，其步骤 `data.usage` 必须非空。

    上一条 AST 守卫只证明"参数写了"，这条证明"值真的从调用点流到了步骤里"。
    少一个调用点（分镜、单段重写原先就是）→ 这里少一步，"这次生成花了多少 token"
    就永远少算 —— 而界面上看起来是个完整的数字。
    """
    pl = _pipeline(tmp_path)
    real = pl.llm.chat_json
    seen: list[tuple[str, bool]] = []

    def spy(task, system, user, model_cls, **kw):
        box = kw.get("usage")
        # 没传字典就是没接线：这里直接炸，而不是静默少记（变异检验靠它）
        assert isinstance(box, dict), f"{task} 没带 usage 容器，token 无处可记"
        box.update({"prompt_tokens": 1000, "completion_tokens": 20})
        seen.append((task, True))
        return real(task, system, user, model_cls, **kw)

    pl.llm.chat_json = spy
    jid = pl.start_generate(_req())
    snap = wait_job(pl, jid, timeout=120)
    assert snap["state"] == "done", snap.get("error")
    steps = {s["key"]: (s.get("data") or {}) for s in snap["steps"]}
    called = {t for t, _ in seen}
    assert {"select", "write", "storyboard"} <= called, called
    # 步骤名带轮次（`write_r1` / `write_r2`），且校验步是纯代码、不该假装有 token
    llm_steps = [k for k in steps
                 if k == "select" or k == "storyboard" or k.startswith("write_")]
    assert len(llm_steps) >= 3, sorted(steps)
    for key in llm_steps:
        assert steps[key].get("usage"), f"步骤 {key} 没有 usage：这一步的钱没记账"
    for key in [k for k in steps if k.startswith("check_")]:
        assert not steps[key].get("usage"), f"确定性步骤 {key} 凭空多了 token 数"


# ── 第 6 轮复核（lane）查出的四处，各自钉一条 ────────────────────
def test_read_error_walks_past_an_empty_middle_cause():
    """三层链上中间那层消息是空串时，根因依然要说得出来。

    实测过的形态：`RuntimeError("产物落盘失败") from OSError("") from
    ValueError("quota_table 第 3 行缺少 count 键")` —— 原来 `__cause__ or
    __context__` 只取一层，而 `if cs` 又把空串那一层当"没有原因"，
    于是界面只剩「产物落盘失败」五个字。
    """
    # 与 `raise ... from` 生成的异常图等价，只是不把异常真抛出去
    mid = OSError("")
    mid.__cause__ = ValueError("quota_table 第 3 行缺少 count 键")
    outer = RuntimeError("产物落盘失败")
    outer.__cause__ = mid

    msg = _readable_error(outer)
    assert "产物落盘失败" in msg, msg
    assert "quota_table 第 3 行缺少 count 键" in msg, f"根因被空消息的中间层吃掉了：{msg}"


def test_read_error_does_not_print_the_same_cause_twice():
    """外层已经塞进 `_brief` 截断过的原文时，不许再补一遍「上一步：…」。

    外层文案里的原文被截到 160 字，而异常链那头的原文更长 —— 原来的判据是
    `cs not in msg`，前缀相同、整体不同，于是同一段话印两遍（实测 160+120 字重复）。
    """
    long_text = "上游返回畸形响应 " + ("详细诊断信息。" * 40)
    cause = ValueError(long_text)
    outer = RuntimeError(f"模型接口不接受当前请求：{_brief(long_text)}")
    outer.__cause__ = cause
    msg = _readable_error(outer)
    assert msg.count("上一步") <= 1, f"补了两遍原因：{msg}"
    head = long_text[:120]
    assert msg.count(head) <= 1, "同一段原因原文出现了两次"


def test_snapshot_does_not_hand_out_live_nested_step_data(tmp_path):
    """快照交出的步骤，其 `data` 里再往下一层也不能是活引用。

    `dict(data)` 只有一层：`data["report"]` 仍是作业里那个对象 —— 实测发出快照后
    改 `job.steps[0]["data"]["report"]["hard_hits"]`，已发出的快照跟着变。
    这是契约破口（今天还没有活的改写点，但 `_step` 之后继续往 data 里塞东西
    正是流水线的常态，上一条 usage 就是这么加的）。
    """
    job = Job("snap-nested", "generate", {})
    job.steps.append({"key": "check_r1", "title": "校验",
                      "data": {"report": {"hard_hits": ["原始"],
                                          "deep": [{"deeper": "v"}]}},
                      "at": 0})
    snap = job.snapshot(include_result=False)
    job.steps[0]["data"]["report"]["hard_hits"].append("TAMPERED")
    job.steps[0]["data"]["report"]["deep"][0]["deeper"] = "TAMPERED"
    got = snap["steps"][0]["data"]["report"]
    assert got["hard_hits"] == ["原始"], got
    assert got["deep"][0]["deeper"] == "v", got


def test_retry_note_quotes_the_wait_it_will_actually_sleep(monkeypatch):
    """被预算夹过的等待，通知里必须报夹过之后的秒数。

    批次 10 把退避夹进作业预算之后，通知仍按未夹的 `wait` 生成：
    上游 `Retry-After: 60` 而预算只剩几秒时，界面写「60s 后重试」、实际几秒就继续，
    等于界面在说谎（第 6 轮复核抓到）。
    """
    import re
    import time as _t

    import httpx
    notes = []
    hits = []

    def handler(req):
        hits.append(_t.time())
        if len(hits) < 2:
            return httpx.Response(429, headers={"Retry-After": "60"}, text="slow down")
        return httpx.Response(200, json={"choices": [{
            "message": {"content": '{"sections": []}'}, "finish_reason": "stop"}]})

    client = _client()
    client.cfg = dataclasses.replace(client.cfg, retries=1)
    real = httpx.Client
    monkeypatch.setattr("app.llm.get_client",
                       lambda *a, **k: real(transport=httpx.MockTransport(handler)))
    started = _t.time()
    # 第二次尝试就成功：这里要看的只有"通知里的秒数"，不是产物
    client.chat_json("write", "s", "u", ScriptDraft,
                     deadline=started + 3.0,
                     on_retry=lambda note, i, n: notes.append(note))
    waited = _t.time() - started
    quoted = [int(m) for n in notes for m in re.findall(r"(\d+)s 后重试", n)]
    assert quoted, notes
    assert max(quoted) <= 3, f"通知说等 {quoted} 秒，而预算只剩 3 秒：{notes}"
    assert waited < 8, f"实际睡了 {waited:.1f}s，通知与预算都对不上"


def test_base_exception_in_a_worker_releases_the_slot(tmp_path):
    """作业线程抛出 `Exception` 之外的东西时，额度必须还得回来。

    各 worker 兜的是 `Exception`；`SystemExit` / `KeyboardInterrupt` 一类从它们上面
    穿过去 —— 线程死了、作业停在忙态，而 `prune()` 只回收终态，那个并发额度
    就永久没了（lane 实测 `_run_packgen` 抛 `SystemExit` 后 state 一直是 `packing`）。
    这一条测的是 `_spawn` 那一层统一的兜，而不是逐个 worker 各补一遍。
    """
    pl = _pipeline(tmp_path)
    job = Job("be-1", "generate", {})
    pl.registry.add_if_room(job, MAX_CONCURRENT_JOBS)
    job.transition_or_raise("selecting")

    def boom():
        raise SystemExit(3)

    pl._spawn(job, boom)
    for _ in range(80):
        if job.state in TERMINAL_STATES:
            break
        time.sleep(0.05)
    assert job.state == "failed", job.state
    assert pl.registry.running_count() == 0, "额度还挂在忙态作业上"


def test_start_packgen_releases_the_claim_even_before_the_job_exists(tmp_path, monkeypatch):
    """占位从 `claim_slug` 成功那一刻起就有人归还 —— 连作业都还没建出来时也一样。

    原来 `try` 从 `self.llm` 才开始：`new_job_id()` / `Job(...)` / `add_if_room()`
    任何一处抛，那个行业名就永远建不出来，报错还是「行业包正在创建中」。
    """
    pl = _pipeline(tmp_path)
    slug = preview_slug("宠物医院")
    assert slug and claim_slug(slug) is True        # 与 start_packgen 争同一个名字之前先归还
    release_slug(slug)

    def boom():
        raise RuntimeError("作业 id 都发不出来")
    monkeypatch.setattr("app.pipeline.new_job_id", boom)
    with pytest.raises(RuntimeError):
        pl.start_packgen("宠物医院", "社区小店")
    assert claim_slug(slug) is True, "占位没还：这个名字从此永远建不出包"
    release_slug(slug)


# ── 第 7 轮复核（打 §15.13 那批新代码的 lane）查出的四条，各自钉一条 ──
class _Boom(Exception):
    """`str()` 自己会抛的异常：归因代码不许假设异常一定说得出话。"""

    def __str__(self):
        return 1 / 0        # noqa: B018  ← 故意的


def test_read_error_survives_a_cause_whose_str_raises():
    """归因里一次 `str()` 抛错，绝不能把"作业收口"整条路带走。

    实测过的形态（lane 探针 G6）：`RuntimeError("产物落盘失败") from _Boom()`，
    而 `_Boom.__str__` 自己抛 `ZeroDivisionError` → 原来 `_readable_error` 直接
    把 Secondary 异常抛给 `_guarded`，`job.transition` 那一行根本没跑到，
    作业停在 `writing`、`running_count()` 永远是 1 —— 又是一次"忙态=永久漏额度"。
    """
    outer = RuntimeError("产物落盘失败")
    outer.__cause__ = _Boom()
    msg = _readable_error(outer)          # 不许抛
    assert "产物落盘失败" in msg, msg


def test_guarded_settles_the_job_even_when_fail_itself_blows_up(tmp_path, monkeypatch):
    """连 `_fail` 都抛的时候，兜底那条路必须仍然把作业落到终态。

    归因、落盘、渲染文案任何一步出问题都不该换成"作业永远在跑"：
    症状是引擎在一个都没跑的情况下持续回「已达上限」，只能重启。
    """
    pl = _pipeline(tmp_path)
    job = Job("be-2", "generate", {})
    pl.registry.add_if_room(job, MAX_CONCURRENT_JOBS)
    job.transition_or_raise("selecting")

    def boom(*a, **k):
        raise RuntimeError("记失败这一步自己也炸了")

    monkeypatch.setattr(pl, "_fail", boom)
    t = threading.Thread(target=pl._guarded(job, lambda: (_ for _ in ()).throw(_Boom())))
    t.start()
    t.join(5)
    assert job.state == "failed", job.state
    assert pl.registry.running_count() == 0


def test_read_error_keeps_a_root_cause_the_prose_only_mentions_by_class_name():
    """外层顺嘴提了一句异常类型名，不等于根因已经说过 —— 旧判据会把它整条丢掉。

    原来去重条件是 `type(c).__name__ in msg`（对整句话做子串匹配）：
    外层写「这类 ValueError 需要检查包配置」时，真正的 ValueError("quota_table …")
    就被当成"已经说过了"。空消息的环节本来就 continue 掉了，这条宽判是多余的。
    """
    cause = ValueError("quota_table 第 3 行缺少 count 键")
    outer = RuntimeError("包配置里的 ValueError 需要检查 pack.yaml")
    outer.__cause__ = cause
    msg = _readable_error(outer)
    assert "quota_table 第 3 行缺少 count 键" in msg, f"根因被类型名子串误伤：{msg}"


def test_clamped_wait_never_turns_retries_into_a_hammer():
    """预算到点后的等待：有闸门可问才是 0，没闸门可问时要有下限。

    `_clamped_wait` 夹到剩余预算是 P2-49 的修法，但"到点"夹出来是 0 ——
    有作业闸门时下一次尝试前会收工，没有闸门的调用点就变成 0 秒连打 N 次
    （实测 4 个请求 0 秒内全发完）。两个调用点现在都按有无闸门给 floor。
    """
    past = time.time() - 1.0
    assert LLMClient._clamped_wait(60.0, past, floor=0.0) == 0.0
    floored = LLMClient._clamped_wait(60.0, past, floor=0.2)
    assert floored >= 0.2, floored
    # 未到点时仍然以"剩余预算"为上界，不能被下限顶回去
    soon = time.time() + 0.05
    assert LLMClient._clamped_wait(60.0, soon, floor=0.0) <= 0.05


def test_the_backoff_floor_reaches_the_actual_sleep(tmp_path, monkeypatch):
    """下限必须一路传到真正那次 `time.sleep`，不然它只是句注释。

    第 8 轮复核实测到的安慰剂：调用点夹出 0.2s，`_interruptible_sleep` 又用
    **默认 floor=0** 夹一次 → 0.2 被压回 0，一次都不睡；4 个请求 0.000s 发完，
    与修前一模一样，而通知里却写着 0.2s（于是"通知与实睡同源"这条也被破了）。
    既有那条测 `_clamped_wait` 的用例只看辅助函数，看不见这件事 —— 它绿着放的过。
    """
    client = _client()
    slept: list[float] = []
    monkeypatch.setattr("app.llm.time.sleep", lambda s: slept.append(s))
    past = time.time() - 1.0
    client._interruptible_sleep(60.0, None, past, floor=0.2)
    assert slept and slept[-1] >= 0.2, f"下限没传到底：{slept}"
    # 有闸门可问时仍然交给闸门：那条路上预算到点该立刻收工，不该多睡
    slept.clear()
    client._interruptible_sleep(60.0, lambda: None, past, floor=0.2)


def test_floor_applies_without_a_deadline_and_the_note_never_says_zero(tmp_path, monkeypatch):
    """没预算可夹时，下限同样要生效；而且通知里不许印「0s」。

    第 9 轮复核实测到的两处：
      - `_clamped_wait` 在 `deadline is None` 时直接 `return wait`，floor 一次都没参与 ——
        而 `ping()` 恰是**唯一**不带 deadline 也不带闸门的重试入口：
        `Retry-After: 0` + retries=10 → 11 个请求 0.001s 发完、一次都没睡。
        也就是说第 8 轮那条下限在它唯一为之而写的调用点上贡献为 0（同族第二次安慰剂）。
      - 修完之后 `:.0f` 又把 0.2 印成「0s 后重试」：界面写着 0 秒、实际睡了 0.2 秒。
    """
    import re
    import httpx
    assert LLMClient._clamped_wait(0.0, None, 0.2) >= 0.2, "没 deadline 时下限被丢弃"

    stamps: list[float] = []

    def handler(req):
        stamps.append(time.monotonic())
        return httpx.Response(429, headers={"Retry-After": "0"}, text="slow down")

    notes: list[str] = []
    client = _client()
    client.cfg = dataclasses.replace(client.cfg, retries=2)
    real = httpx.Client
    monkeypatch.setattr("app.llm.get_client",
                        lambda *a, **k: real(transport=httpx.MockTransport(handler)))
    with pytest.raises(Exception):
        # ping 的形状：没有 deadline、没有 should_abort
        client.chat_json("write", "s", "u", ScriptDraft,
                         on_retry=lambda note, i, n: notes.append(note))
    gaps = [b - a for a, b in zip(stamps, stamps[1:])]
    assert len(stamps) == 3, stamps
    assert min(gaps) >= 0.15, f"重试之间没有下限（安慰剂形态）：{gaps}"
    quoted = [float(m) for n in notes for m in re.findall(r"([\d.]+)s 后重试", n)]
    assert quoted and min(quoted) > 0, f"通知里印着 0s：{notes}"
    for q, g in zip(quoted, gaps):
        assert abs(q - g) <= 0.35, f"通知说 {q}s、实际隔了 {g:.3f}s：{notes}"


def test_expired_budget_without_a_gate_still_spaces_its_retries(tmp_path, monkeypatch):
    """429 一路 + 预算已过 + 没闸门：重试之间必须有间隔（不许 0 秒连打）。"""
    import httpx
    stamps: list[float] = []

    def handler(req):
        stamps.append(time.monotonic())
        return httpx.Response(429, headers={"Retry-After": "60"}, text="slow")

    client = _client()
    client.cfg = dataclasses.replace(client.cfg, retries=2)
    real = httpx.Client
    monkeypatch.setattr("app.llm.get_client",
                        lambda *a, **k: real(transport=httpx.MockTransport(handler)))
    with pytest.raises(Exception):
        client.chat_json("write", "s", "u", ScriptDraft, deadline=time.time() - 1.0)
    assert len(stamps) >= 2, stamps
    gaps = [b - a for a, b in zip(stamps, stamps[1:])]
    assert min(gaps) > 0.05, f"重试间隔被夹成了 0（安慰剂形态）：{gaps}"


def test_guarded_fallback_keeps_the_error_already_recorded(tmp_path, monkeypatch):
    """兜底那句"二次出错"不许覆盖掉已经记对的可执行原因。

    复现（第 8 轮复核给的最小触发）：worker 抛 `KeyError('quota_table')`，
    `_fail` 已经把「行业包配置缺少字段：'quota_table'」写进 job.error，
    随后 `registry.prune()` 抛一下 —— 原来兜底无条件 force 成
    「…状态收口时二次出错」，一次不相干的收尾失败把原因抹了。
    """
    pl = _pipeline(tmp_path)
    job = Job("gf-1", "generate", {})
    pl.registry.add_if_room(job, MAX_CONCURRENT_JOBS)
    job.transition_or_raise("selecting")
    monkeypatch.setattr(pl.registry, "prune", lambda *a, **k: (_ for _ in ()).throw(OSError(28, "full")))
    with pytest.raises(Exception):
        pl._guarded(job, lambda: (_ for _ in ()).throw(KeyError("quota_table")))()
    assert job.state == "failed", job.state
    assert "quota_table" in (job.error or ""), job.error
    assert "二次出错" not in (job.error or ""), f"可执行的原因被兜底话覆盖了：{job.error}"


def test_guarded_fallback_does_not_turn_a_cancel_into_a_failure(tmp_path, monkeypatch):
    """取消之后落进来的兜底不许把 cancelled 改成 failed（取消不是失败）。"""
    pl = _pipeline(tmp_path)
    job = Job("gf-2", "generate", {})
    pl.registry.add_if_room(job, MAX_CONCURRENT_JOBS)
    job.transition_or_raise("selecting")

    def fail_then_cancel(*a, **k):
        job.request_cancel()          # 模拟"_fail 查过 is_cancelled 之后"取消才落进来
        raise OSError("记失败这一步炸了")

    monkeypatch.setattr(pl, "_fail", fail_then_cancel)
    with pytest.raises(Exception):
        pl._guarded(job, lambda: (_ for _ in ()).throw(RuntimeError("worker")))()
    assert job.state == "cancelled", job.state
    assert not (job.error or ""), f"取消被写成了失败：{job.error}"


class _NoStr(Exception):
    """`__str__` 抛 BaseException 的异常：链上出现它，归因不许半路作废。"""

    def __str__(self):
        raise SystemExit(2)


def test_read_error_survives_a_cause_that_raises_base_exception():
    """链上某环节 `__str__` 抛 SystemExit 时，外层那句可执行的原因要留着。

    第 8 轮为了"别吞掉 Ctrl-C"把 `_safe_str` 改成放行 BaseException，第 9 轮
    复核立刻发现它的另一面：链里一句 `__str__` 抛 SystemExit 会让 `_readable_error`
    半路抛出，`error` 于是从「产物落盘失败」退化成「详情看引擎日志」—— 槽没漏
    （兜底管住了），但**归因被抹掉了**，而那正是第 8 轮兜底立誓要保护的东西。
    两个诉求是真的：直接对异常放行、链内一律吞。
    """
    outer = RuntimeError("产物落盘失败")
    outer.__cause__ = _NoStr()
    assert _readable_error(outer) == "产物落盘失败"
    # 直接对象那侧仍然放行：Ctrl-C / SystemExit 不该被当成"这句话取不出来"
    with pytest.raises(SystemExit):
        _safe_str(_NoStr())


# ── 第 9 轮 N7：忙态漏槽的第二道网 ──────────────────────────────
def test_stranded_busy_jobs_are_reaped_but_fresh_ones_are_not():
    """远超预算仍挂在忙态的作业要被收口；正常在跑的一条不许被碰。

    `prune()` 原来只回收终态，于是"停在忙态"等于永久占一个并发额度
    （本轮已经修过多条把作业留在忙态的路径，但兜底自己坏掉时仍然没有后手）。
    """
    from app import jobs as J
    reg = J.JobRegistry()
    fresh = J.Job("stale-fresh", "generate", {})
    stranded = J.Job("stale-old", "generate", {})
    assert reg.add_if_room(fresh, 4) and reg.add_if_room(stranded, 4)
    stranded.transition_or_raise("selecting")
    stranded.started_at = time.time() - J.JOB_BUDGET_SECONDS * 3
    reg.prune()
    assert stranded.state == "failed", stranded.state
    assert "回收额度" in (stranded.error or ""), stranded.error
    assert fresh.state == "queued", "在预算内的作业被误伤"
    assert reg.running_count() == 1


def test_the_net_frees_the_slot_even_when_transition_raises():
    """收口这条路自己也坏了（MemoryError 一类）时，额度照样要还回来。

    这正是第 9 轮复核点出的残留：`_guarded` 的兜底若在自己那次 `job.transition`
    上抛，作业就停在忙态、没有任何东西能救。第二道网因此不能依赖同一次调用。
    """
    from app import jobs as J
    reg = J.JobRegistry()
    jobs_ = [J.Job(f"leak-{i}", "generate", {}) for i in range(4)]
    for j in jobs_:
        assert reg.add_if_room(j, 4)
    stuck = jobs_[0]
    stuck.transition_or_raise("selecting")
    stuck.started_at = time.time() - J.JOB_BUDGET_SECONDS * 3

    def boom(*a, **k):
        raise MemoryError("连迁移都失败")
    stuck.transition = boom                       # 收口失败的最坏形态
    newcomer = J.Job("newcomer", "generate", {})
    assert reg.add_if_room(newcomer, 4) is True, "四槽全被漏掉的忙态占死，第二道网没生效"
    assert reg.running_count() <= 4
