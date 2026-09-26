# 重试、空内容诊断与配置保持测试
# 跑法：python tests/test_llm_retry.py   或   pytest tests/test_llm_retry.py
import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402
from pydantic import BaseModel  # noqa: E402

from app.config import load_config, save_config  # noqa: E402
from app.llm import EmptyContentError, LLMClient, LLMError  # noqa: E402


class Out(BaseModel):
    ok: bool


def _resp(status=200, content='{"ok": true}'):
    """伪装 OpenAI 补全响应；非 200 时 body 为原样文本"""
    body = content if status != 200 else json.dumps(
        {"choices": [{"message": {"content": content}}]})
    return httpx.Response(status_code=status, text=body)


def _run(handler, fn):
    """把 LLMClient 的 HTTP 层换成 MockTransport。

    修复后所有请求都走 `app.llm.get_client()`（连接池复用），
    所以这里替换的是那个工厂，而不是 `httpx.post`（它已经不被调用了）。
    """
    transport = httpx.MockTransport(handler)
    with patch("app.llm.get_client", lambda: httpx.Client(transport=transport)):
        return fn()


def _cfg(**over):
    """构造一份**不依赖仓库根 config.yaml** 的配置。

    ⚠ 这些用例原来写的是 `load_config(ROOT)` —— 读仓库根的 config.yaml，
    而那是 **.gitignore 的运行时文件**（每个开发者机器上都不一样）。
    后果：**干净检出（`git worktree` / 新克隆）里这些用例必挂** ——
    config.yaml 不存在 → 模型列表为空 → `cfg.llm.base_url` 为空 →
    httpx 拼出 "/chat/completions" → `ValueError: unknown url type`。
    实测：主仓库 9/9 过、worktree 里 5 条挂（提交自洽验证出现假红）。
    **测试不该依赖未跟踪的运行时文件。**
    """
    tmp = Path(tempfile.mkdtemp(prefix="ts-llmcfg-"))
    (tmp / "config.yaml").write_text(
        "models:\n"
        "  - {id: m1, name: t, base_url: 'https://api.example/v1',"
        " api_key: 'sk-test', model: 'test-model'}\n"
        "active_model: m1\n", encoding="utf-8")
    cfg = load_config(tmp)
    for k, v in over.items():
        setattr(cfg.llm, k, v)
    return cfg


def test_network_retry_then_success():
    cfg = _cfg(retries=2, timeout=5)
    notes = []
    client = LLMClient(cfg.llm, on_retry=lambda n, a, t: notes.append((n, a, t)))
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise httpx.ConnectError("refused", request=req)
        return _resp(200, '{"ok": true}')

    out = _run(handler, lambda: client.chat_json("t", "s", "u", Out))
    assert out.ok is True and calls["n"] == 3, (out, calls)
    assert len(notes) == 2, notes


def test_401_not_retried():
    cfg = _cfg(retries=2)
    client = LLMClient(cfg.llm)
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        return _resp(401, "bad key")

    try:
        _run(handler, lambda: client.chat_json("t", "s", "u", Out))
        raise AssertionError("401 应抛错")
    except LLMError as e:
        assert "401" in str(e)
    assert calls["n"] == 1, f"401 不应重试，实际调用 {calls['n']} 次"


def test_429_retried():
    cfg = _cfg(retries=2)
    client = LLMClient(cfg.llm)
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        if calls["n"] <= 2:
            return _resp(429, "slow down")
        return _resp(200, '{"ok": true}')

    out = _run(handler, lambda: client.chat_json("t", "s", "u", Out))
    assert out.ok is True and calls["n"] == 3


def test_429_honors_retry_after():
    """429 且带 Retry-After 头 → 按它等（而不是无视头做限流退避）。"""
    cfg = _cfg(retries=2)
    client = LLMClient(cfg.llm)
    calls = {"n": 0}
    sleeps = []

    def handler(req):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, text="slow down",
                                  headers={"Retry-After": "3"})
        return _resp(200, '{"ok": true}')

    with patch("app.llm.time.sleep", side_effect=lambda s: sleeps.append(s)):
        out = _run(handler, lambda: client.chat_json("t", "s", "u", Out))
    assert out.ok is True and calls["n"] == 2, (out, calls)
    # Retry-After=3 + 抖动 → 等 [3.0, 3.4)；不能退化成限流退避的 1.5s 档
    assert len(sleeps) == 1 and 3.0 <= sleeps[0] < 3.5, sleeps


def test_429_retry_after_capped():
    """Retry-After 给超大值 → 封顶 RETRY_AFTER_WAIT_MAX，不当陪等。"""
    cfg = _cfg(retries=2)
    client = LLMClient(cfg.llm)
    sleeps = []

    def handler(req):
        return httpx.Response(429, text="slow down",
                              headers={"Retry-After": "9999"})

    with patch("app.llm.time.sleep", side_effect=lambda s: sleeps.append(s)):
        try:
            _run(handler, lambda: client.chat_json("t", "s", "u", Out))
            raise AssertionError("多次 429 应抛错")
        except LLMError:
            pass
    assert sleeps, "没有发生任何等待"
    assert all(60.0 <= s < 60.5 for s in sleeps), sleeps
    assert len(sleeps) == 2, f"retries=2 → 只应等 2 次，实际 {len(sleeps)} 次"


def test_429_without_retry_after_uses_rate_limit_backoff():
    """429 且无 Retry-After → 限流专用更长的退避（1.5s 起），不是普通 1s 档。"""
    cfg = _cfg(retries=2)
    client = LLMClient(cfg.llm)
    calls = {"n": 0}
    sleeps = []

    def handler(req):
        calls["n"] += 1
        if calls["n"] <= 2:
            return _resp(429, "slow down")
        return _resp(200, '{"ok": true}')

    with patch("app.llm.time.sleep", side_effect=lambda s: sleeps.append(s)):
        out = _run(handler, lambda: client.chat_json("t", "s", "u", Out))
    assert out.ok is True and calls["n"] == 3
    # 1.5s×2^n + 抖动：第 1 次 ∈[1.5,1.9)，第 2 次 ∈[3.0,3.4)
    assert len(sleeps) == 2, sleeps
    assert 1.5 <= sleeps[0] < 2.0, sleeps
    assert 3.0 <= sleeps[1] < 3.5, sleeps


def test_429_note_mentions_wait():
    """429 的 on_retry 通知要带上等待时长 —— 界面日志里能看出「在等限流」。"""
    cfg = _cfg(retries=2)
    notes = []
    client = LLMClient(cfg.llm, on_retry=lambda n, a, t: notes.append(n))

    def handler(req):
        return _resp(429, "slow down")

    with patch("app.llm.time.sleep", lambda s: None):
        try:
            _run(handler, lambda: client.chat_json("t", "s", "u", Out))
        except LLMError:
            pass
    assert notes and notes[0].startswith("上游限流(429)") and "后重试" in notes[0], notes


def test_retries_exhausted_reports_count():
    cfg = _cfg(retries=2)
    client = LLMClient(cfg.llm)

    def handler(req):
        raise httpx.ConnectError("refused", request=req)

    try:
        _run(handler, lambda: client.chat_json("t", "s", "u", Out))
        raise AssertionError("应抛错")
    except LLMError as e:
        assert "已重试 3 次" in str(e), str(e)


def test_streaming_empty_content_retries_with_upgraded_budget():
    """流式空内容：必须升级预算重试（P0：这条升级分支原本是死代码）。

    断言口径 = **数请求次数 + 第二次请求 body.max_tokens > 第一次**。
    不许再看文案里的数字（那只能证明文案会写数字，不能证明真的重发了）。
    """
    cfg = _cfg(retries=0, max_tokens=1234)
    client = LLMClient(cfg.llm)

    # 只有思考、没有正文：正是推理模型耗尽输出预算时的真实形态
    sse = (b'data: {"choices":[{"delta":{"reasoning_content":"\\u60f3\\u5f88\\u4e45"}}]}\n\n'
           b'data: {"choices":[{"delta":{},"finish_reason":"length"}]}\n\n'
           b"data: [DONE]\n\n")
    bodies = []

    def handler(req):
        bodies.append(json.loads(req.content))
        return httpx.Response(200, content=iter([sse]),
                              headers={"content-type": "text/event-stream"})

    seen = []
    try:
        _run(handler, lambda: client.chat_json(
            "t", "s", "u", Out, on_delta=lambda k, t: seen.append((k, t))))
        raise AssertionError("空内容应抛 EmptyContentError")
    except EmptyContentError:
        pass
    assert len(bodies) == 2, f"空内容应重试一次 → 2 个请求，实际 {len(bodies)}"
    assert bodies[1]["max_tokens"] > bodies[0]["max_tokens"], \
        f"第二次预算必须升级: {bodies[0]['max_tokens']} → {bodies[1]['max_tokens']}"
    assert seen and seen[0][0] == "reasoning", seen


def test_non_streaming_empty_content_retries_with_upgraded_budget():
    """非流式空内容：与流式同一条升级预算的重试分支。"""
    cfg = _cfg(retries=0, max_tokens=1234)
    client = LLMClient(cfg.llm)
    bodies = []

    def handler(req):
        bodies.append(json.loads(req.content))
        return _resp(200, "")          # HTTP 200 但 content 为空

    try:
        _run(handler, lambda: client.chat_json("t", "s", "u", Out))
        raise AssertionError("空内容应抛 EmptyContentError")
    except EmptyContentError:
        pass
    assert len(bodies) == 2, f"空内容应重试一次 → 2 个请求，实际 {len(bodies)}"
    assert bodies[1]["max_tokens"] > bodies[0]["max_tokens"], \
        f"第二次预算必须升级: {bodies[0]['max_tokens']} → {bodies[1]['max_tokens']}"


def test_streaming_length_truncation_retries_with_upgraded_budget():
    """content 非空但 finish_reason=length（JSON 被截断）：必须升级预算重试，
    且第二次请求带「完整输出」提示 —— 不是把同一份 payload 原样重发
    （主报告 R3.3 的核心批评）。"""
    cfg = _cfg(retries=0, max_tokens=1234)
    client = LLMClient(cfg.llm)
    bodies = []
    sse = (b'data: {"choices":[{"delta":{"content":"{\\"ok\\":"}}]}\n\n'
           b'data: {"choices":[{"delta":{},"finish_reason":"length"}]}\n\n'
           b"data: [DONE]\n\n")

    def handler(req):
        bodies.append(json.loads(req.content))
        return httpx.Response(200, content=iter([sse]),
                              headers={"content-type": "text/event-stream"})

    try:
        _run(handler, lambda: client.chat_json(
            "t", "s", "u", Out, on_delta=lambda k, t: None))
        raise AssertionError("被截断的输出应抛错")
    except LLMError:
        pass
    assert len(bodies) == 2, f"截断应重试一次 → 2 个请求，实际 {len(bodies)}"
    assert bodies[1]["max_tokens"] > bodies[0]["max_tokens"], \
        f"第二次预算必须升级: {bodies[0]['max_tokens']} → {bodies[1]['max_tokens']}"
    # 第二次请求必须带着「完整输出」的用户提示（消息最后一条）
    assert "完整输出" in bodies[1]["messages"][-1]["content"], bodies[1]["messages"][-1]


def test_streaming_429_retried_with_backoff():
    """流式分支的 429（RetryableStatus）走同一套限流专用退避。"""
    cfg = _cfg(retries=2, timeout=5)
    client = LLMClient(cfg.llm)
    calls = {"n": 0}
    sleeps = []
    sse = (b'data: {"choices":[{"delta":{"content":"{\\"ok\\":true}"}}]}\n\n'
           b"data: [DONE]\n\n")

    def handler(req):
        calls["n"] += 1
        if calls["n"] <= 2:
            return httpx.Response(429, text="slow down")
        return httpx.Response(200, content=iter([sse]),
                              headers={"content-type": "text/event-stream"})

    with patch("app.llm.time.sleep", side_effect=lambda s: sleeps.append(s)):
        out = _run(handler, lambda: client.chat_json(
            "t", "s", "u", Out, on_delta=lambda k, t: None))
    assert out.ok is True and calls["n"] == 3, (out, calls)
    # 429 无 Retry-After → 限流专用退避：1.5s×2^n + 抖动
    assert len(sleeps) == 2, sleeps
    assert 1.5 <= sleeps[0] < 2.0 and 3.0 <= sleeps[1] < 3.5, sleeps


def test_brief_masks_secrets():
    """上游响应回显必须脱敏（P1）：sk- 密钥 / Bearer / Authorization 头掩码，
    原文 Key 绝不能落进 error 字段 / 历史 / 界面。"""
    from app.llm import _brief
    key = "sk-" + "A" * 46          # 模拟上游 500 回显里夹带 46 字符假 Key

    # 1) 单独一个 Bearer 令牌
    out = _brief(f"error: Bearer {key} 更多内容" + "x" * 300)
    assert key not in out and key[3:] not in out, f"Key 泄漏: {out}"
    assert "Bearer" in out and "***" in out, out

    # 2) Authorization 头（连同整个令牌值掩掉）
    out2 = _brief(f"Authorization: Bearer {key} 上下文" + "x" * 300)
    assert key not in out2 and key[3:] not in out2, f"Key 泄漏: {out2}"
    assert "authorization" in out2.lower() and "***" in out2, out2

    # 3) 裸 Key（不带 Bearer 前缀）
    out3 = _brief(f"context={key}" + "x" * 300)
    assert key not in out3 and key[3:] not in out3, f"Key 泄漏: {out3}"
    assert "sk-" in out3 and key[3:] not in out3, out3


def test_streaming_content_accumulates_and_excludes_reasoning():
    cfg = _cfg(retries=0)
    client = LLMClient(cfg.llm)
    sse = (b'data: {"choices":[{"delta":{"reasoning_content":"\\u60f3"}}]}\n\n'
           b'data: {"choices":[{"delta":{"content":"{\\"ok\\":"}}]}\n\n'
           b'data: {"choices":[{"delta":{"content":"true}"}}]}\n\n'
           b"data: [DONE]\n\n")

    def handler(req):
        return httpx.Response(200, content=iter([sse]),
                              headers={"content-type": "text/event-stream"})

    out = _run(handler, lambda: client.chat_json(
        "t", "s", "u", Out, on_delta=lambda k, t: None))
    assert out.ok is True


def test_config_merge_keeps_untouched_fields():
    tmp = Path(tempfile.mkdtemp(prefix="ts-cfg-"))
    save_config(tmp, {"base_url": "https://x/v4", "api_key": "sk-123",
                      "model": "m1", "retries": 3, "timeout": 240})
    save_config(tmp, {"base_url": "https://y/v4", "api_key": "", "model": "m2"})
    c2 = load_config(tmp)
    assert c2.llm.api_key == "sk-123", f"key 被清空: {c2.llm.api_key}"
    assert c2.llm.retries == 3 and c2.llm.timeout == 240, vars(c2.llm)
    assert c2.llm.model == "m2" and c2.llm.base_url == "https://y/v4"


def test_broken_config_falls_back_to_defaults():
    """配置写坏不该让引擎起不来（原来会直接抛异常，界面只剩「无法连接引擎」）。

    2026-09-17：读坏 → `models` 段也读不到 → **一条模型都没有**
    （不再是「落回内置默认的 glm-4.7」—— 文件都读不出来还说「你用的是 glm-4.7」
    才是撒谎）。这里守的核心是**不抛异常** + 两个信号都在：
    `config_error` 说「读坏了」、空列表让界面说「还没有配置模型」。
    """
    tmp = Path(tempfile.mkdtemp(prefix="ts-badcfg-"))
    (tmp / "config.yaml").write_text("llm: [this is: not a mapping\n", encoding="utf-8")
    cfg = load_config(tmp)
    assert cfg.config_error, "读坏了却报告「没问题」"
    assert cfg.models == [] and cfg.llm.model == ""
    assert cfg.default_pack == "elevator"


def test_env_override_covers_all_fields():
    """环境变量要能覆盖 retries/timeout/max_tokens（打包版尤其需要）。"""
    import os
    tmp = Path(tempfile.mkdtemp(prefix="ts-envcfg-"))
    keys = ["TALKSCRIPT_API_KEY", "TALKSCRIPT_MODEL", "TALKSCRIPT_RETRIES",
            "TALKSCRIPT_TIMEOUT", "TALKSCRIPT_MAX_TOKENS", "TALKSCRIPT_TEMPERATURE",
            "TALKSCRIPT_DEFAULT_PACK", "TALKSCRIPT_MOCK"]
    old = {k: os.environ.get(k) for k in keys}
    try:
        os.environ.update({
            "TALKSCRIPT_API_KEY": "sk-env", "TALKSCRIPT_MODEL": "env-model",
            "TALKSCRIPT_RETRIES": "5", "TALKSCRIPT_TIMEOUT": "30",
            "TALKSCRIPT_MAX_TOKENS": "777", "TALKSCRIPT_TEMPERATURE": "0.1",
            "TALKSCRIPT_DEFAULT_PACK": "mypack", "TALKSCRIPT_MOCK": "1",
        })
        cfg = load_config(tmp)
        assert cfg.llm.api_key == "sk-env"
        assert cfg.llm.model == "env-model"
        assert cfg.llm.retries == 5 and cfg.llm.timeout == 30
        assert cfg.llm.max_tokens == 777 and cfg.llm.temperature == 0.1
        assert cfg.default_pack == "mypack" and cfg.mock is True
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def main() -> int:
    cases = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in cases:
        try:
            fn()
            print(f"  OK {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  FAIL {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(cases) - failed}/{len(cases)} 通过")
    return 1 if failed else 0


def test_usage_accumulates_across_retries():
    """重试**累加**用量，不是覆盖（P2-20）。

    重试循环把**同一个** `usage` 字典传给每次尝试（见 `chat_json` 的
    `for attempt`）；以前 `usage.update()` 让第二次把第一次**覆盖** ——
    被丢弃那次真实消耗的 token **静默不计**，用户按界面显示的数字对账会少一笔，
    而且重试次数越多差得越多。记账只会增加、不会回退。
    """
    cfg = _cfg()
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        # 第一次给一个过不了校验的正文 → 走重试；两次都带 usage
        content = "坏的" if calls["n"] == 1 else '{"ok": true}'
        body = json.dumps({
            "choices": [{"message": {"content": content}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 10,
                      "completion_tokens_details": {"reasoning_tokens": 4}},
        })
        return httpx.Response(200, text=body)

    usage: dict = {}
    _run(handler, lambda: LLMClient(cfg.llm).chat_json(
        "t", "s", "u", Out, max_retries=1, usage=usage))

    assert calls["n"] == 2, f"没走到重试（第一次该因校验失败重来）：{calls['n']}"
    # 两次尝试各消耗 100/10 —— 账上必须是 200/20，不是 100/10
    assert usage.get("prompt_tokens") == 200, usage
    assert usage.get("completion_tokens") == 20, usage
    assert usage.get("completion_tokens_details", {}).get("reasoning_tokens") == 8, usage


if __name__ == "__main__":
    sys.exit(main())
