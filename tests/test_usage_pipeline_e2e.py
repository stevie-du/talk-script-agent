# -*- coding: utf-8 -*-
"""P1-40 / P3-15 的行为级回归：usage 经真 HTTP 通道进作业步骤并落盘。

为什么单开这一个文件：mock 夹具通道（`llm.py` 的 `if self.mock`）**不产 usage**
—— `response_for` 只回 payload，`chat_json` 直接 `model_validate` 返回，
于是「usage 收进 step」这条链在现有测试里**没有任何行为级守护**，
只有 `test_usage_keys_cross_layer.py` 的静态对账（读源码断言键名一致）。
静态对账守不住「collect 了但没落盘」这类接线错误。

这里用 httpx.MockTransport 在 **get_client 层**注入假上游（和
`test_llm_retry.py` 同一手法），跑**完整管道**（draft → 校验 → 回炉 →
校验 → 分镜 → 落盘），断言：
  1. 每个调模型的步骤 data.usage 里有 `prompt_cache_hit_tokens` /
     `prompt_cache_miss_tokens` / `completion_tokens_details.reasoning_tokens`
     （P1-40 要显示的三个数，P3-15 收 usage 的那条链）；
  2. 落盘的 result.json 的 logs 里同样有（界面历史详情读的是落盘那份）；
  3. 回炉闭环照旧工作（首轮脏稿 → 校验不过 → 回炉 → 干净终版）；
  4. 末 chunk 带 usage 且 `choices` 为空数组的形态也被接住
     （P3-15 修的「`if not choices: continue` 丢掉带 usage 的末 chunk」）。

上游响应形态刻意照 DeepSeek 抄：流式 SSE，usage 在最后一个 choices 为空的
chunk 里。载荷文本复用 `mock_fixtures` 里已被验证「能过 60s 校验」的那几份。
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import load_config                        # noqa: E402
from app.mock_fixtures import (_PLAN_TRAP, _SECTIONS_CLEAN,  # noqa: E402
                               _SECTIONS_DIRTY, _STORYBOARD)
from app.pipeline import Pipeline, wait_job               # noqa: E402
from app.schemas import GenerateRequest                   # noqa: E402

USAGE = {
    "prompt_tokens": 8000, "completion_tokens": 500, "total_tokens": 8500,
    "prompt_cache_hit_tokens": 1234, "prompt_cache_miss_tokens": 88,
    "completion_tokens_details": {"reasoning_tokens": 300},
}


def _sse(obj) -> bytes:
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n".encode("utf-8")


def _payload_for(user: str, call_no: int) -> dict:
    """按提示词内容判断阶段（与 packgen 无关，纯粹复刻假上游的分流逻辑）。"""
    if "先定选题" in user:                      # stages.draft（方案 10 合并路径）
        dirty = call_no == 1
        return {"plan": _PLAN_TRAP,
                "sections": _SECTIONS_DIRTY if dirty else _SECTIONS_CLEAN}
    if "生成分镜" in user:                       # stages.storyboard
        return {"storyboard": _STORYBOARD}
    if "回炉" in user:                           # 回炉轮 → 干净稿
        return {"sections": _SECTIONS_CLEAN}
    return {"sections": _SECTIONS_DIRTY if call_no == 1 else _SECTIONS_CLEAN}


def _handler_factory():
    state = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        state["n"] += 1
        body = json.loads(req.content.decode("utf-8"))
        user = "".join(m.get("content", "") for m in body.get("messages", [])
                       if m.get("role") == "user")
        payload = _payload_for(user, state["n"])
        content = json.dumps(payload, ensure_ascii=False)
        chunks = [
            {"choices": [{"index": 0, "delta": {"content": content[:20]},
                          "finish_reason": None}]},
            {"choices": [{"index": 0, "delta": {"content": content[20:]},
                          "finish_reason": "stop"}]},
            # P3-15 的形态：末 chunk 带 usage 且 choices 为空数组
            {"choices": [], "usage": USAGE},
        ]
        return httpx.Response(200, headers={"Content-Type": "text/event-stream"},
                              content=b"".join(_sse(c) for c in chunks)
                              + b"data: [DONE]\n\n")
    return handler


def _run_pipeline(tmp: Path):
    (tmp / "config.yaml").write_text(
        "models:\n  - id: fake\n    name: 假上游\n"
        f"    base_url: http://127.0.0.1:1/v1\n    api_key: k\n    model: m\n"
        "active_model: fake\nllm:\n  retries: 1\n  timeout: 30\n"
        "  max_tokens: 8000\ndefault_pack: elevator\n", encoding="utf-8")
    cfg = load_config(ROOT, tmp)
    assert not cfg.mock
    pl = Pipeline(ROOT, cfg, data_dir=tmp)
    jid = pl.start_generate(GenerateRequest(pack="elevator", topic="被困电梯怎么办",
                                            duration=60))
    return pl, jid, wait_job(pl, jid, timeout=120)


def test_usage_with_cache_fields_flows_through_pipeline_to_disk():
    """P1-40/P3-15：真 HTTP 通道里收的 usage（含缓存字段）要进步骤并落盘。"""
    tmp = Path(tempfile.mkdtemp(prefix="usage-e2e-"))
    try:
        with patch("app.llm.get_client",
                   lambda *a, **k: httpx.Client(
                       transport=httpx.MockTransport(_handler_factory()))):
            pl, jid, snap = _run_pipeline(tmp)

        assert snap["state"] == "done", snap.get("error")
        r = snap["result"]
        assert r["check"]["passed"] is True, "终版应过校验（夹具是够长的干净稿）"
        assert not any("政府补贴" in s["text"] for s in r["sections"]), \
            "终版不应含硬禁词"

        # ① 回炉闭环：首轮脏 → check_r1 不过 → write_r2 → check_r2 过
        keys = [s["key"] for s in snap["steps"]]
        assert "check_r1" in keys and "write_r2" in keys and "check_r2" in keys, keys
        assert "storyboard" in keys, "正文合格后应画分镜"

        # ② 三个调模型的步骤都要带缓存字段（draft / write_r2 / storyboard）
        model_steps = {"draft", "write_r2", "storyboard"}
        for s in snap["steps"]:
            if s["key"] not in model_steps:
                continue
            u = (s.get("data") or {}).get("usage") or {}
            assert u.get("prompt_cache_hit_tokens") == 1234, \
                f"{s['key']} 缺 prompt_cache_hit_tokens：{u}"
            assert u.get("prompt_cache_miss_tokens") == 88, \
                f"{s['key']} 缺 prompt_cache_miss_tokens：{u}"
            think = (u.get("completion_tokens_details") or {}).get("reasoning_tokens")
            assert think == 300, f"{s['key']} 缺 reasoning_tokens：{u}"

        # ③ 纯代码校验步骤不产 usage（界面也不该给它们印徽章）
        for s in snap["steps"]:
            if s["key"].startswith("check_r"):
                assert not (s.get("data") or {}).get("usage"), \
                    f"{s['key']} 是纯代码校验，不该有 usage"

        # ④ 落盘那份也要有（历史详情读的是 result.json）
        rj = list((tmp / "generated").glob(f"*/{jid}/result.json"))
        assert rj, "result.json 未落盘"
        disk = json.loads(rj[0].read_text(encoding="utf-8"))
        logged = {s["key"]: (s.get("data") or {}).get("usage") or {}
                  for s in disk.get("logs", [])}
        for k in model_steps:
            assert logged.get(k, {}).get("prompt_cache_hit_tokens") == 1234, \
                f"落盘 logs 里 {k} 缺缓存字段：{logged.get(k)}"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
