# -*- coding: utf-8 -*-
"""整作业预算（P1-5 后半）、流式缓冲按尝试清零（P2-13）、
连接超时不被读超时吃掉（P1-5 前半）、mock 标记取真正通道（P1-23）。

每一条都是**跑出来的**，不是读出来的：本项目的教训是「看了代码以为修了」
的条目在绿门下能存活好几轮。
"""
from __future__ import annotations

import json
import shutil
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import dataclasses

from app.config import LLMConfig, load_config                      # noqa: E402
from app.jobs import JOB_BUDGET_SECONDS, Job, JobBudget, JobCancelled  # noqa: E402
from app.knowledge import Pack                                      # noqa: E402
from app.llm import CONNECT_TIMEOUT, LLMClient                      # noqa: E402
from app.pipeline import Pipeline, ScriptDraft, StoryboardDraft, wait_job                         # noqa: E402
from app.schemas import GenerateRequest, TopicPlan                  # noqa: E402


# ── P1-5：整作业时间预算 ─────────────────────────────────────
def test_overdue_and_budget_reset():
    j = Job("t-1", "generate", {})
    assert not j.overdue()
    j.started_at = time.time() - JOB_BUDGET_SECONDS - 1
    assert j.overdue()
    j.reset_budget()
    assert not j.overdue()


def test_terminal_job_is_never_overdue():
    """昨天建成、今天还挂在注册表里的 done 作业不该被预算闸翻成失败。"""
    j = Job("t-2", "generate", {})
    j.transition("selecting")
    j.transition("writing")
    j.transition("done")
    j.started_at = time.time() - JOB_BUDGET_SECONDS - 3600
    assert not j.overdue()


def test_stop_check_prefers_cancel_over_budget():
    """同时「被取消」与「超预算」时报取消：不是用户点的那一下不该被改成失败。"""
    j = Job("t-3", "generate", {})
    j.request_cancel()
    j.started_at = time.time() - JOB_BUDGET_SECONDS - 1
    with pytest.raises(JobCancelled):
        Pipeline._stop_check(j)


def test_stop_check_raises_budget_readable():
    j = Job("t-4", "generate", {})
    j.started_at = time.time() - JOB_BUDGET_SECONDS - 1
    with pytest.raises(JobBudget) as ei:
        Pipeline._stop_check(j)
    assert "分钟" in str(ei.value) and "recheck_rounds" in str(ei.value)


# ── P1-5：连接超时不被读超时吃掉 ─────────────────────────────
def test_connect_timeout_survives_a_bare_float_read_timeout():
    t = LLMClient._timeout(180.0)
    assert t.read == 180.0 and t.connect == CONNECT_TIMEOUT == 20.0


def test_stream_and_post_actually_use_it(monkeypatch):
    """只在 `_timeout()` 里写对没用 —— 要看两个调用点真的传了这个对象。"""
    seen = {}

    class Recorder(httpx.Client):
        def stream(self, method, url, **kw):
            seen["stream"] = kw.get("timeout")
            return super().stream(method, url, **kw)

        def post(self, url, **kw):
            seen["post"] = kw.get("timeout")
            return super().post(url, **kw)

    def handler(request):
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"points":[]}'},
                                                      "finish_reason": "stop"}]})

    rec = Recorder(transport=httpx.MockTransport(handler))
    monkeypatch.setattr("app.llm.get_client", lambda: rec)
    cfg = LLMConfig(base_url="http://x/v1", api_key="k", model="m",
                    timeout=180.0, retries=0, max_tokens=1000)
    c = LLMClient(cfg)
    c._complete([{"role": "user", "content": "u"}])
    assert seen["post"].connect == CONNECT_TIMEOUT
    with pytest.raises(Exception):        # 上面那个响应不是合法 SSE，流式必然抛
        c._complete([{"role": "user", "content": "u"}],
                    on_delta=lambda kind, text: None)
    assert seen["stream"].connect == CONNECT_TIMEOUT
    # 流式读超时按倍数放大，但 connect 不跟着放大
    assert seen["stream"].read > cfg.timeout


# ── P2-13：被丢弃的尝试不该累进「思考 N 字」 ──────────────────
# SSE 里 delta.content 的取值（这段在 JSON 字符串里，所以引号要转义一层）
BROKEN = '{\\"sections\\":['          # 残缺 JSON：解析必失败 → chat_json 重试
GOOD = '{\\"sections\\":[]}'
GOOD_SB = '{\\"storyboard\\":[]}'


def _sse(reasoning: str, content: str) -> bytes:
    out = ""
    for piece in (reasoning[i:i + 7] for i in range(0, len(reasoning), 7)):
        out += 'data: {"choices":[{"delta":{"reasoning_content":"%s"}}]}\n\n' % piece
    out += 'data: {"choices":[{"delta":{"content":"%s"}}]}\n\n' % content
    out += 'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'
    return out.encode()


def test_stream_counters_reset_per_http_attempt(monkeypatch):
    """两次尝试各 30 字思考 → 界面只能显示 30，不能显示 60。

    修复前 `_delta_handler` 每**轮**只求值一次，重试复用同一个闭包，
    被丢弃那次尝试的思考照样累进 `stream_reasoning_len`（P2-13）。
    """
    notes = []
    transport = httpx.MockTransport(lambda req: httpx.Response(
        200, content=_sse("思" * 30, BROKEN),
        headers={"content-type": "text/event-stream"}))
    monkeypatch.setattr("app.llm.get_client",
                        lambda: httpx.Client(transport=transport))
    cfg = LLMConfig(base_url="http://x/v1", api_key="k", model="m",
                    timeout=30.0, retries=0, max_tokens=1000)
    c = LLMClient(cfg)
    job = Job("t-5", "generate", {})
    phase = "文案撰写"
    attempts = []

    def on_attempt(note="", a=0, t=0):
        attempts.append(a)
        Pipeline._stream_reset(job, phase)(note, a, t)   # 走真产物里那个闭包
    try:
        c.chat_json("write", "s", "u", ScriptDraft,
                    max_retries=1, on_retry=lambda *a: notes.append(a),
                    on_delta=Pipeline._delta_handler(job, phase),
                    on_attempt=on_attempt)
    except Exception:   # noqa: BLE001  两次都残缺，必然抛
        pass
    assert len(attempts) >= 2, "on_attempt 没有每次尝试都触发"
    assert job.stream_reasoning_len == 30, \
        f"被丢弃的尝试仍被累加：{job.stream_reasoning_len}"
    assert notes, "解析重试要留下痕迹"


def test_stream_reset_keeps_phase_visible(monkeypatch):
    """清零不该把阶段名一起清掉：首字节前的静默期靠它显示思考块（P1-7）。"""
    transport = httpx.MockTransport(lambda req: httpx.Response(
        200, content=_sse("思" * 12, GOOD_SB),
        headers={"content-type": "text/event-stream"}))
    monkeypatch.setattr("app.llm.get_client", lambda: httpx.Client(transport=transport))
    cfg = LLMConfig(base_url="http://x/v1", api_key="k", model="m",
                    timeout=30.0, retries=0, max_tokens=1000)
    job = Job("t-6", "generate", {})
    Pipeline._stream_reset(job, "分镜生成")()
    snap = job.snapshot()
    assert snap.get("stream", {}).get("phase") == "分镜生成"
    assert snap["stream"]["reasoning_len"] == 0
    LLMClient(cfg).chat_json("storyboard", "s", "u", StoryboardDraft,
                             on_delta=Pipeline._delta_handler(job, "分镜生成"),
                             on_attempt=Pipeline._stream_reset(job, "分镜生成"))
    assert job.snapshot()["stream"]["reasoning_len"] == 12


# ── P1-23：mock 标记取「真正用了哪条通道」 ────────────────────
def test_mock_marker_follows_the_client(tmp_path):
    cfg = load_config(ROOT, ROOT)
    root = tmp_path
    shutil.copytree(ROOT / "packs", root / "packs")
    pl = Pipeline(root, cfg)
    assert pl.mock is False
    # 设置里把 Key 填成 MOCK：客户端走夹具，但 Pipeline.mock 仍是假
    pl.llm = LLMClient(dataclasses.replace(cfg.llm, api_key="MOCK"))
    assert pl.llm.mock is True
    jid = pl.start_generate(GenerateRequest(pack="elevator", topic="家用电梯怎么选",
                                           duration=60, platform="抖音"))
    snap = wait_job(pl, jid, timeout=60)
    assert snap["state"] == "done", snap["error"]
    disk = next(root.glob("generated/*/*/result.json"))
    assert disk.read_text(encoding="utf-8") and \
        json.loads(disk.read_text(encoding="utf-8"))["mock"] is True, \
        "api_key=MOCK 的夹具产物被落成 mock:false —— 统计又会被夹具污染"


# ── P2-31 后半：scenes 的四个契约空位要真的有人填 ──────────────
def test_build_scenes_fills_every_contract_slot():
    """修复前 `_build_scenes` 读 `bgm/transition/shot_type/style`，
    而分镜模型里根本没有这几个键 —— 于是 `audio.bgm` 恒空、转场恒 cut、
    style 恒空：契约写了却没人灌（docs/场景序列契约.md 里的"空位"）。"""
    sections = [{"type": "hook", "text": "开头"}, {"type": "point", "text": "正文"}]
    shots = [{"shot": "中景", "sfx": "提示音", "bgm": "轻钢琴",
              "transition": "dissolve", "shot_type": "medium"},
             {"shot": "特写", "sfx": "", "bgm": "", "transition": "", "shot_type": ""}]
    timings = [{"start": 0.0, "end": 3.0}, {"start": 3.0, "end": 8.0}]
    scenes = Pipeline._build_scenes(sections, shots, timings, "亲和接地气")
    assert scenes[0]["audio"]["bgm"] == "轻钢琴"
    assert scenes[0]["visual"]["transition"] == "dissolve"
    assert scenes[0]["shot_type"] == "medium"
    assert scenes[0]["style"] == "亲和接地气"
    # 模型留空时：transition 回到契约默认 cut，style 回到本次风格参数（不是空串）
    assert scenes[1]["visual"]["transition"] == "cut"
    assert scenes[1]["style"] == "亲和接地气"


def test_storyboard_schema_exposes_the_four_slots():
    """`StoryboardDraft` 必须有这四个字段，否则模型给了也会被 pydantic 丢掉。"""
    from app.schemas import StoryboardShot
    fields = set(StoryboardShot.model_fields)
    assert {"bgm", "transition", "shot_type", "style"} <= fields
    d = StoryboardShot.model_validate({"shot": "x", "bgm": "b",
                                       "transition": "fade",
                                       "shot_type": "wide", "style": "s"})
    assert d.bgm == "b" and d.transition == "fade" and d.shot_type == "wide"
