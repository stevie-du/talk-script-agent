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


def test_streaming_empty_content_gives_actionable_error():
    """流式分支必须也做空内容诊断。

    修复前 `if on_delta: return self._stream_once(...)` 直接返回，绕过了
    非流式分支里的那段诊断，而 pipeline 全程都传 on_delta —— 于是
    「模型返回空内容，请调大 max_tokens」这条提示在真实使用中永远不出现。
    """
    cfg = _cfg(retries=0, max_tokens=1234)
    client = LLMClient(cfg.llm)

    # 只有思考、没有正文：正是推理模型耗尽输出预算时的真实形态
    sse = (b'data: {"choices":[{"delta":{"reasoning_content":"\\u60f3\\u5f88\\u4e45"}}]}\n\n'
           b'data: {"choices":[{"delta":{},"finish_reason":"length"}]}\n\n'
           b"data: [DONE]\n\n")

    def handler(req):
        return httpx.Response(200, content=iter([sse]),
                              headers={"content-type": "text/event-stream"})

    seen = []
    try:
        _run(handler, lambda: client.chat_json(
            "t", "s", "u", Out, on_delta=lambda k, t: seen.append((k, t))))
        raise AssertionError("空内容应抛 EmptyContentError")
    except EmptyContentError as e:
        assert "max_tokens" in str(e) and "1234" in str(e), str(e)
    assert seen and seen[0][0] == "reasoning", seen


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
            print(f"  ✅ {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  ❌ {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(cases) - failed}/{len(cases)} 通过")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
