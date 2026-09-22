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
import logging
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
                      JobRegistry, TERMINAL_STATES)
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
    t0 = claim_slug(slug)
    assert t0, "同一个 slug 再占一次应该成功（修复前永远占不到）"
    release_slug(slug, t0)


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
    # elevator 是合并包（方案 10，stages.draft）：首调用 draft、回炉走 write、
    # storyboarding 照旧 —— select 不该再出现（合并包没有单独的选题调用）。
    # 老包（无 stages.draft）走的才是 select+write 两段。
    assert {"draft", "storyboard"} <= called, called
    assert "write" in called and "select" not in called, called
    # 步骤名带轮次（`write_r2`…；r1 被 draft 顶掉），且校验步是纯代码、不该假装有 token
    llm_steps = [k for k in steps
                 if k in ("select", "draft", "storyboard") or k.startswith("write_")]
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
    # ⚠ 这一族正则需要**整份一致**：`_fmt_wait` 现在会印小数（2.5s），这里若还写
    #   (\d+) 就会把 2.4 读成 4 —— 通知是对的、判据把它读错了（第 11 轮复核 P2；
    #   同文件 :1022 那处已经是 [\d.]+，两处各写一份就是两本账）。
    quoted = [float(m) for n in notes for m in re.findall(r"([\d.]+)s 后重试", n)]
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
    t1 = claim_slug(slug) if slug else None         # 与 start_packgen 争同一个名字之前先归还
    assert slug and t1, "前置：这个 slug 要能占上"
    release_slug(slug, t1)

    def boom():
        raise RuntimeError("作业 id 都发不出来")
    monkeypatch.setattr("app.pipeline.new_job_id", boom)
    with pytest.raises(RuntimeError):
        pl.start_packgen("宠物医院", "社区小店")
    t2 = claim_slug(slug)
    assert t2, "占位没还：这个名字从此永远建不出包"
    release_slug(slug, t2)


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


# ── 第 9/10 轮：忙态漏槽的第二道网（只归还额度，绝不动作业）──
def _stranded(reg, jid, **kw):
    j = Job(jid, "generate", kw)
    assert reg.add_if_room(j, MAX_CONCURRENT_JOBS)
    j.transition_or_raise("selecting")
    j.last_progress = time.time() - JOB_BUDGET_SECONDS * 3
    return j


def test_a_stuck_job_frees_its_slot_without_being_mutated():
    """回收网的唯一职责是把并发额度还回来 —— 作业自己的状态它不许碰。

    第 10 轮复核把上一版判成 P1：把还在往前走的作业 force 成 failed，看着像收口，
    实际是废掉它的状态机（`failed → done` 不在迁移表里），后面每一次
    `transition_or_raise` 都抛 StateConflict，作业半路死掉，已写进去的产物还会被
    "取消 → 撤掉"那一支删走。实测两条作业（收与不收）都以 failed + 零产物收场。
    """
    reg = JobRegistry()
    fresh = Job("fresh-1", "generate", {})
    assert reg.add_if_room(fresh, MAX_CONCURRENT_JOBS)
    stuck = _stranded(reg, "stuck-1")
    reg.prune()
    assert reg.running_count() == 1, "额度没还回来"
    assert stuck.state == "selecting", f"回收网改了作业状态：{stuck.state}"
    assert fresh.state == "queued" and reg.get("fresh-1") is fresh


def test_a_full_house_of_stuck_jobs_still_admits_a_new_one():
    """症状本身：四条卡死的作业曾把后来所有生成都挡在 409 之外，只能重启。

    ⚠ 这里**不许**先调 `prune()`（第 11 轮复核 P1 抓的就是上一版这么写的）：
    "四槽全漏死"的现场恰恰是没有任何作业会完成，于是 prune 永远不会被触发 ——
    测试先 prune 等于替实现补上那道网要做的事，把唯一要证明的前提证没了。
    真正要测的是两道额度闸**在取锁之前**各自扫一遍。

    `running_count`（额度账上算数的那几条）与 `add_if_room`（真的放不放行）
    必须同一个口径，否则又是一处两本账：显示"0 个在跑"却继续拒绝新作业。
    """
    reg = JobRegistry()
    stuck = [_stranded(reg, f"stuck-{i}") for i in range(MAX_CONCURRENT_JOBS)]
    # 最后加进去的那条此刻还不算"过期"（它是 add 之后才被伪造成 40 分钟没动的），
    # 所以这里不能用 prune 补一刀 —— 要看的就是"下一次准入自己把它扫掉"。
    nxt = Job("next-1", "generate", {})
    assert reg.add_if_room(nxt, MAX_CONCURRENT_JOBS), "还是被卡死的作业挡住了"
    assert reg.running_count() == 1, "准入时那一次扫描没把额度算回去（只剩 nxt 自己）"
    # 三个额度口径都要一致 —— 重写走的是另一条路（`transition_if_room`），
    # 只补 `add_if_room` 的话，症状会在「重写本段」上原样复现。
    done = Job("done-1", "generate", {})
    assert reg.add_if_room(done, MAX_CONCURRENT_JOBS)
    done.transition_or_raise("writing")
    done.transition_or_raise("done")
    assert reg.transition_if_room(done, "rewriting", MAX_CONCURRENT_JOBS), \
        "卡死的作业还在堵住单段重写"
    # 被摘额度的作业仍然可查：条目没被删，界面不会变成"作业不存在"
    snaps = {s["id"] for s in reg.snapshots()}
    assert {j.id for j in stuck} <= snaps
    assert all(j.stranded and not j.is_cancelled() for j in stuck)


def test_a_busy_but_progressing_job_is_never_reaped():
    """一直在往前走的作业，哪怕跑了远超预算，也不该被回收网碰。"""
    reg = JobRegistry()
    j = Job("alive-1", "generate", {})
    assert reg.add_if_room(j, MAX_CONCURRENT_JOBS)
    j.transition_or_raise("selecting")
    j.started_at = time.time() - JOB_BUDGET_SECONDS * 3      # 按开始时间早该"超时"
    j.last_progress = time.time() - 1.0                      # 一秒前还在推进
    reg.prune()
    assert j.state == "selecting" and reg.get("alive-1") is j
    assert reg.running_count() == 1


def test_checkpoints_are_what_count_as_progress():
    """「有进展」的**四个**来源都要真的刷新时间戳，否则第二道网要么误伤要么失效。

    上一版这份用例标题写"三个"、实际只测了两个（第 11 轮复核 P1：`_step` 那一行
    删掉也全绿）；`_delta_handler` 走的仍是下面这两个，不单独算一个来源。
    """
    j = Job("touch-1", "generate", {})
    j.last_progress = 0.0
    Pipeline._stop_check(j)
    t1 = j.last_progress
    assert t1 > 0, "检查点不算进展"
    j.last_progress = 0.0
    Pipeline._step(object.__new__(Pipeline), j, "write", "文案撰写", {})
    assert j.last_progress > 0, "记一步进度不算进展"
    assert len(j.steps) == 1, "顺手把记步本身改坏了"
    j.last_progress = 0.0
    Pipeline._delta_handler(j, "文案撰写")("content", "字")
    assert j.last_progress > 0, "流式增量不算进展"
    j.last_progress = 0.0
    j.push_delta("reasoning", "再想一点")
    assert j.last_progress > 0, "push_delta 不算进展"


def test_a_reaped_job_that_still_finishes_keeps_its_artifact(tmp_path, monkeypatch):
    """被第二道网摘掉额度的作业如果真跑完了，产物、历史与轮询都必须照旧。

    这是回收网唯一可能被误伤的路径（判据是"40 分钟毫无进展"，正常作业每个
    检查点都会刷新，所以基本进不来；但一旦进来，代价不能是用户的数据）。
    上一版把作业从注册表里 `pop` 掉，这条测试直接 `KeyError` —— 也就是界面上
    的"作业不存在"，而它其实还在跑。
    """
    pl = _pipeline(tmp_path)
    orig = Pipeline._step
    fired = {"done": False, "job": None, "stranded": None}

    def step(self, job, key, title, data):
        out = orig(self, job, key, title, data)
        if key.startswith("write") and not fired["done"]:
            fired["done"] = True
            fired["job"] = job
            job.last_progress = 0.0        # 伪装成"很久没进展"，但线程继续往前走
            self.registry.prune()
            fired["stranded"] = job.stranded
        return out

    monkeypatch.setattr(Pipeline, "_step", step)
    jid = pl.start_generate(_req())
    snap = wait_job(pl, jid, timeout=120)
    assert fired["done"], "前置没成立：改写步骤的那一步根本没跑到"
    assert fired["stranded"] is True, "第二道网没触发 —— 这条测试什么都没测到"
    assert snap["state"] == "done", f"被回收的作业没能正常收尾：{snap}"
    assert pl.store.read_result(jid), "产物没了 —— 回收网把用户等出来的东西弄丢了"
    assert any(h.get("id") == jid for h in pl.store.history()), "历史里没有这条"
    # 轮询口径不能被弄坏：作业还在的时候 `/api/jobs/{id}` 认得它，跑完也认得
    assert pl.registry.get(jid) is fired["job"]


# ── 第 10 轮 P3：三处"说给用户听的话"必须与真的那本账同源 ─────────
def test_the_wait_shown_is_the_wait_slept_even_above_one_second():
    """`:.0f` 把 2.5 秒印成「2」也是说谎 —— 通知与实睡同源不分数量级。

    Python 用银行家舍入，3.5 又印成 4：同一个函数两种错法，所以整数以外一律带小数。
    """
    assert LLMClient._fmt_wait(2.5) == "2.5"
    assert LLMClient._fmt_wait(3.5) == "3.5"
    assert LLMClient._fmt_wait(0.2) == "0.2"
    assert LLMClient._fmt_wait(30.0) == "30"
    assert LLMClient._fmt_wait(0.0) == "0"


def test_the_budget_message_names_the_same_unit_the_timer_uses():
    """界面计时是秒（progress.js「已用 N 秒」），文案只报分钟就对不上是同一件事。

    分钟数不整时报 1.5 而不是"超过 2 分钟" —— 虚报的等待时长比不说还糟。
    """
    big = str(JobBudget(JOB_BUDGET_SECONDS))
    assert "20 分钟" in big and "1200 秒" in big, big
    odd = str(JobBudget(90.0))
    assert "1.5 分钟" in odd and "超过 2 分钟" not in odd, odd
    small = str(JobBudget(45.0))
    assert "45 秒" in small and "分钟" not in small, small


def test_a_structurally_bad_model_output_never_quotes_internal_class_names(monkeypatch,
                                                                           caplog):
    """模型连续输出坏结构时，界面上那句不许是「无法解析为 ScriptDraft」+ 整段英文。

    `ScriptDraft` 是代码内部的类名（连 pydantic 自己印出来的原文里都有它），界面上
    没有任何一个地方叫这个。给用户的话要带"下一步做什么"与**字段路径**；原文转日志，
    给模型的 re-prompt 里照旧留原文 —— 那才是看得懂英文的那一方。
    """
    import httpx

    def handler(_req):
        return httpx.Response(200, json={"choices": [{
            "message": {"content": '{"sections": [{"oops": 1}]}'}, "finish_reason": "stop"}]})

    client = _client()
    seen = []

    def spy(req):
        seen.append(json.loads(req.content.decode("utf-8")))
        return handler(req)

    real = httpx.Client
    monkeypatch.setattr("app.llm.get_client",
                        lambda *a, **k: real(transport=httpx.MockTransport(spy)))
    caplog.set_level(logging.WARNING)
    with pytest.raises(LLMError) as ei:
        client.chat_json("write", "s", "u", ScriptDraft, max_retries=1)
    msg = str(ei.value)
    assert "ScriptDraft" not in msg, f"把类名贴给了用户：{msg}"
    assert "validation errors" not in msg, f"整段英文原话进了错误框：{msg}"
    assert msg.startswith("模型") and "结构" in msg, msg
    assert "sections" in msg, f"字段路径被一并抹掉了：{msg}"
    assert "调大输出预算" in msg, f"没给下一步：{msg}"
    # 原文没丢：一次在日志里（排查），一次在给模型的那条 re-prompt 里
    assert "validation errors for ScriptDraft" in caplog.text
    reprompt = [m for m in seen[-1]["messages"] if m["role"] == "user"][-1]["content"]
    assert "不是合法的目标 JSON" in reprompt and "validation errors" in reprompt


def test_the_wait_note_never_prints_zero_for_a_nonzero_wait():
    """0.03 秒印成「0s 后重试」是 `_fmt_wait` 存在的理由反过来的样子（第 11 轮复核 P2）。

    向上取整成 0.1 也是一种谎，所以这里只要求两件事：非零进 → 非零出，且能原样读回。
    """
    for w in (0.03, 0.001, 0.2, 1.5, 30.0, 60.0):
        s = LLMClient._fmt_wait(w)
        assert float(s) > 0, f"等了 {w}s 却通知「{s} 后重试」"
        assert s != "0", f"非零等待印成了 0：{w}"
    assert LLMClient._fmt_wait(0.0) == "0"


def test_a_model_that_returns_no_json_at_all_still_speaks_chinese(monkeypatch):
    """模型一个字都不按 JSON 给（`json.loads` 直接抛）时，界面那句不许漏英文。

    第 11 轮复核抓到：这条恰恰是**最常见**的那次失败，而第一版的
    `_structural_summary` 在没有结构化 errors 时回退了 pydantic/json 原文
    （`Expecting value: line 1 column 1 (char 0)`）。原文只进引擎日志。
    """
    import httpx

    def handler(_req):
        return httpx.Response(200, json={"choices": [{
            "message": {"content": "好的，下面是我写的口播稿……"}, "finish_reason": "stop"}]})

    client = _client()
    real = httpx.Client
    monkeypatch.setattr("app.llm.get_client",
                        lambda *a, **k: real(transport=httpx.MockTransport(handler)))
    with pytest.raises(LLMError) as ei:
        client.chat_json("write", "s", "u", ScriptDraft, max_retries=1)
    msg = str(ei.value)
    assert "Expecting" not in msg and "char" not in msg, f"英文原文又漏进界面：{msg}"
    assert "输出不是可解析的 JSON" in msg, msg


def test_rewrite_of_an_old_record_is_not_stranded_the_moment_it_starts():
    """昨天那条记录点「重写本段」，不许在刚被放行的瞬间就被回收网摘掉额度。

    `transition_if_room` 放行与 `reset_budget` 都要重开回收网的钟：少任何一处，
    整条重写就全程不进额度账（`stranded` 是单向门），实测同时在飞的忙态作业能超上限。
    """
    reg = JobRegistry()
    j = Job("old-1", "generate", {})
    assert reg.add_if_room(j, MAX_CONCURRENT_JOBS)
    j.transition_or_raise("writing")
    j.transition_or_raise("done")
    j.last_progress = time.time() - JOB_BUDGET_SECONDS * 2 - 5      # 40 分钟没动过的老记录
    assert reg.transition_if_room(j, "rewriting", MAX_CONCURRENT_JOBS)
    # ⚠ 光"放行后立刻断言没被摘"是什么也测不到的（第 11 轮复核：那样写时把
    #   `transition_if_room` 里的 touch 删掉照样绿）—— 摘额度发生在**下一次扫描**，
    #   真机上就是并发的另一条生成。这里补上那一次扫描，才是被测到的那个时序。
    other = Job("peer-1", "generate", {})
    assert reg.add_if_room(other, MAX_CONCURRENT_JOBS)
    assert not j.stranded, "刚放行的重写在下一次扫描里被摘掉额度 —— 这一条重写在额度账上不存在"
    assert reg.running_count() == 2, f"重写不在账上：{[s['state'] for s in reg.snapshots()]}"
    # `reset_budget` 只管**整作业预算**那只钟（回收网的钟由放行那一步负责，
    # 第 15 轮复核量到在那里再 touch 一次是 0 秒的死行）—— 所以要测的是它能观察到的效果。
    before = j.deadline()
    j.reset_budget()
    assert j.deadline() > before, "reset_budget 没重开整作业预算"
    reg.prune()
    assert not j.stranded, "重写进行中却被回收网摘了额度（预算重开之后不该再旧）"


def test_packgen_admission_failure_after_the_slot_is_taken_settles_the_job(tmp_path,
                                                                          monkeypatch):
    """`self.llm` 在建包作业**占上额度之后**抛（配置读不出来）：作业必须落终态。

    P1-46 的第四条路 —— 原来这里只归还 slug，作业停在 queued，那条并发额度
    要等 2× 预算才被回收网摘掉（实测连点十次 = 引擎说"一个都没在跑但已达上限"）。
    """
    pl = _pipeline(tmp_path)
    pl._llm = None                              # 让 `self.llm` 走惰性构建那一条路

    def boom(self):
        raise RuntimeError("配置文件读不出来")
    monkeypatch.setattr(Pipeline, "_build_llm", boom)
    with pytest.raises(RuntimeError):
        pl.start_packgen("猫咖丁", "社区猫咖，面向养猫人群获客")
    states = [s["state"] for s in pl.registry.snapshots()]
    assert "failed" in states, f"作业没落终态：{states}"
    assert pl.registry.running_count() == 0, "那条并发额度还挂在 queued 上"
    from app.packgen import claim_slug, release_slug
    t3 = claim_slug("猫咖丁")
    assert t3, "归还点漏了：这个名字从此永远建不出包"
    release_slug("猫咖丁", t3)


def _literal_leaves(node):
    """`a + b + c` 里的各字面量片段（**按书写顺序**），拼不出来的叶子记 None。"""
    import ast
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _literal_leaves(node.left) + _literal_leaves(node.right)
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    return [None]


def _fold_str_concat(node):
    """整个 `+` 表达式都是字面量时返回拼好的串，否则 None。

    顺序必须与书写一致 —— 折错顺序会拼出一条**作者并没写**的正则，
    那会让守卫去报一个不存在的模式（假红比假绿更难查）。
    """
    parts = _literal_leaves(node)
    if any(p is None for p in parts):
        return None
    return "".join(parts)


def _whole_second_regex_offenders(sources):
    r"""挑出"读「N 秒后重试」却只认整数"的正则 —— 扫**所有字符串常量**，不看调用形状。

    第 12 轮量出按行扫的两个毛病：注释/文档串里引用这句历史的话会被当成真代码（假红），
    而没有 r 前缀、跨行隐式拼接、`([0-9]+)` 这类写法反而放过（假绿）。
    第 16 轮量出"只看 `re.findall(常量)` 这种形状"同样是假绿：把模式先赋给变量、
    `re.compile` 存起来再调、`pattern=` 关键字、`import re as r2` 这几种写法一个都不抓
    —— 而它们都是本仓库真会写出来的形状。

    所以这一版换成**行为判据**，与调用形状彻底解耦：
    1. 候选 = 任意字符串常量（文档串除外），**以及**整条都是字面量的 `+` 拼接
       （`r"(\d+)" + "s 后重试"` 这种任何一片单独看都不完整）；f-string 的字面片段
       本身就是常量，天然在候选里；
    2. 拿它去搜真实通知原文「上游限流(429)，4.7s 后重试」，取第一个捕获组（没有组就用整段）；
    3. 读出来的值必须含 `4.7`。只认整数的写法在这里必然读到 `7` —— 不管它写成
       `(\d+)`、`([0-9]+)`、`(\d{1,2})`、`[0-9][0-9]*` 还是 `\d+`。
    编译不过的（不是正则）跳过；压根搜不中的（不是读这句通知的）跳过。
    已知仍然放过：把数字类藏进变量/函数返回值再拼起来的模式 —— 那要跑起来才知道，
    静态扫不做第二遍解释器；这种写法今天全库没有一处。
    """
    import ast
    import re as _re
    import warnings
    sample = "上游限流(429)，4.7s 后重试"
    out = []
    for name, src in sources:
        # 样例里有故意写坏的正则（`"\d"` 没加 r 前缀），那份 SyntaxWarning 是**样例**的
        # 而不是本测试的 —— 抑制掉，别污染门禁输出。
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", SyntaxWarning)
            tree = ast.parse(src, filename=name)
        docs = set()
        for node in ast.walk(tree):
            body = getattr(node, "body", None)
            if isinstance(body, list) and body \
                    and isinstance(body[0], ast.Expr) \
                    and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                docs.add(id(body[0].value))               # 文档串是散文，不是代码
        # 候选 = 每个字符串常量，**加上**用 `+` 把字面量拼起来的模式（第 19 轮自查：
        # `"(\d+)" + "s 后重试"` 这种写法任何一片单独看都不含完整判据，只看常量会漏）。
        # f-string 的字面片段本身就是 ast.Constant，走的是同一条路。
        cands = [(n.lineno, n.value) for n in ast.walk(tree)
                 if isinstance(n, ast.Constant) and isinstance(n.value, str)
                 and id(n) not in docs]
        for n in ast.walk(tree):
            if isinstance(n, ast.BinOp) and isinstance(n.op, ast.Add):
                folded = _fold_str_concat(n)
                if folded is None or "后重试" not in folded:
                    continue
                # 只有"跨片才拼得出完整判据"的那种才需要额外报一次；某一片自己就
                # 同时含「后重试」与数字类的情况，常量那一轮已经报过了（去重的判据
                # 必须与常量那一轮的过滤一致 —— 写成"这片含 后重试 就算"会漏：
                # 单独一片 `s 后重试` 不算候选，拼出来的 `(\d+)s 后重试` 才是）
                leaves = _literal_leaves(n)
                if all(not ("后重试" in (p or "")
                            and ("\\d" in p or "[0-9]" in p)) for p in leaves):
                    cands.append((n.lineno, folded))
        for lineno, pat in cands:
            if "后重试" not in pat or ("\\d" not in pat and "[0-9]" not in pat):
                continue
            try:
                rx = _re.compile(pat)
            except _re.error:
                continue                                   # 不是正则，管不着
            m = rx.search(sample)
            if not m:
                continue
            got = m.group(1) if m.groups() else m.group(0)
            if "4.7" not in (got or ""):
                out.append(f"{name}:{lineno}: {pat!r} 读到 {got!r}（应为 '4.7'）")
    return out


def test_no_test_reads_the_retry_note_with_a_whole_second_regex():
    r"""`_fmt_wait` 会印小数，任何"只认整数"的正则读这句通知都会把对的读成错的。

    第 11 轮量到：一次 2.4s 的等待被整数正则读成 4，于是断言在通知正确时报红。
    """
    files = [(p.name, p.read_text(encoding="utf-8"))
             for p in sorted((ROOT / "tests").glob("*.py"))]
    offenders = _whole_second_regex_offenders(files)
    assert not offenders, f"这些正则读「N 秒后重试」却不认小数：{offenders}"
    # 守卫自证（本项目对"只能变绿的守卫"过敏）：坏例必须被抓、好例必须放过，
    # 而且**换成 indirect 写法照样抓**（第 16 轮的靶子就是这里只认一种调用形状）。
    bs = chr(92)
    bad = "(" + bs + "d+)s 后重试"
    demo = ('"""文档串里写 (' + bs + 'd+)s 后重试 不算代码"""\n'
            + "import re\n"
            + 're.findall(r"' + bad + '", t)\n'
            + 'pat = r"' + bad + '"\n'
            + 're.search(pat, t)\n'
            + 'R = re.compile(r"' + bad + '")\n'
            + 'R.fullmatch(t)\n'
            + 're.match(pattern=r"' + bad + '", string=t)\n'
            + 're.finditer(r"([0-9][0-9]*)s 后重试", t)\n'
            + 're.search(r"(' + bs + 'd+)" + "s 后重试", t)\n'
            + 're.findall(r"([' + bs + 'd.]+)s 后重试", t)\n'
            + 'note = "60s 后重试"\n')
    caught = _whole_second_regex_offenders([("demo.py", demo)])
    assert len(caught) == 6, (
        f"应抓到 6 条（4 种 indirect 形状 + [0-9][0-9]* + 用 + 拼出来的模式），实抓 {caught}")
    assert _whole_second_regex_offenders([("c.py", "import re\nx = re\n")]) == []


# ── 第 12 轮：额度账的"单向门"不再漏计 / 同名建包不重复花钱 ──────────
def _stranded_busy(reg, n):
    """造 n 条"挂在忙态但已被回收网摘掉额度"的作业（第 12 轮实测的形状）。"""
    out = []
    for i in range(n):
        j = Job(f"s-{i}", "generate", {})
        assert reg.add_if_room(j, MAX_CONCURRENT_JOBS)
        j.transition_or_raise("writing")
        j.last_progress = 0.0                      # 下一次扫描就会把它摘掉
        reg.prune()
        assert j.stranded, f"回收网没把 {j.id} 摘掉：这条测试什么都没测到"
        out.append(j)
    return out


def test_transition_if_room_sweeps_before_counting():
    """`transition_if_room` 自己那次扫描是**承重**的：删掉它，重写会永久 409。

    ⚠ 造现场不许借道 `add_if_room` 的扫描（第 15 轮复核抓我上一版就是这个毛病：
    `_stranded()` / `_stranded_busy()` 让 add_if_room 顺手把记录摘好，于是
    `transition_if_room` 里那行 `_reap_stranded()` 删掉也 550 全绿）。
    这里先把四条忙态作业**新鲜地**放进注册表，之后统一改老，再让唯一能救场的
    那道闸自己扫。
    """
    reg = JobRegistry()
    stuck = []
    for i in range(MAX_CONCURRENT_JOBS):
        j = Job(f"f-{i}", "generate", {})
        assert reg.add_if_room(j, MAX_CONCURRENT_JOBS + 8)   # 额度放宽：这一步不该扫到老记录
        j.transition_or_raise("writing")
        stuck.append(j)
    old = Job("old-1", "generate", {})
    assert reg.add_if_room(old, MAX_CONCURRENT_JOBS + 8)
    old.transition_or_raise("writing")
    old.transition_or_raise("done")
    for j in stuck:
        j.last_progress = 0.0                                # 全部改成"40 分钟没进展"
    assert reg.running_count() == MAX_CONCURRENT_JOBS, "前置：还没人扫描，额度应仍被占满"
    assert reg.transition_if_room(old, "rewriting", MAX_CONCURRENT_JOBS), \
        "重写被四条卡死的旧作业挡住了 —— 这道闸没在取锁前自己扫一遍"
    assert all(j.stranded for j in stuck), "重写能进来，只能是这道闸自己回收了额度"


def test_stranded_records_cannot_pile_up_unaccounted_rewrites():
    """被误摘额度的老记录各自点「重写本段」，并发数不许突破上限。

    第 12 轮实测：准入这条与三个额度口径对 stranded 的规则相反
    —— 准入允许它、计数排除它，7 条同时在飞而 `running_count()` 报 3。
    """
    reg = JobRegistry()
    stuck = _stranded_busy(reg, MAX_CONCURRENT_JOBS)
    admitted = 0
    for j in stuck:
        j.last_progress = time.time()               # 用户此刻点了按钮
        if reg.transition_if_room(j, "rewriting", MAX_CONCURRENT_JOBS):
            admitted += 1
            assert not j.stranded, "放行后必须回到账上，否则这条重写不占名额"
    counted = sum(1 for s in reg.snapshots()
                  if s["state"] in ("rewriting", "writing", "queued"))
    assert admitted <= MAX_CONCURRENT_JOBS, f"放行了 {admitted} 条 > 上限 {MAX_CONCURRENT_JOBS}"
    assert counted == admitted, f"界面/额度两本账：在飞 {counted}，准入 {admitted}"


def test_the_wait_note_never_understates_the_sleep():
    """上报的等待时长必须 ≥ 实睡（429 的 Retry-After 语义是"至少这么久"）。"""
    for w in (90.06, 0.2, 2.5, 3.5, 30.0, 0.03, 0.001, 59.99):
        s = LLMClient._fmt_wait(w)
        assert float(s) + 1e-9 >= w, f"通知说 {s}s，实睡 {w}s —— 少说了"
    assert LLMClient._fmt_wait(0.0) == "0"


def test_an_http_attempt_counts_as_progress(tmp_path):
    """每次真正的 HTTP 尝试都要给回收网喂一次"还活着"的证据。

    否则"两次检查点之间最长能隔多久"的下界是 3×timeout×流式倍率（默认 ~5400s），
    比 2× 预算(2400s) 还长 —— 一条只是在慢上游前面等着的活作业会被误判卡死。
    """
    pl = _pipeline(tmp_path)
    j = Job("att-1", "generate", {})
    j.last_progress = 0.0
    pl._stream_reset(j, "文案撰写")()
    assert j.last_progress > 0, "尝试开始不算进展（第二道网会误伤慢上游）"
