# 重试与配置保持测试：python tests/test_llm_retry.py
import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402
from app.config import load_config, save_config  # noqa: E402
from app.llm import LLMClient, LLMError  # noqa: E402
from pydantic import BaseModel  # noqa: E402


class Out(BaseModel):
    ok: bool


def resp(status=200, content='{"ok": true}'):
    """伪装 OpenAI 补全响应；非 200 时 body 为原样文本"""
    body = content if status != 200 else json.dumps(
        {"choices": [{"message": {"content": content}}]})
    return httpx.Response(status_code=status, text=body,
                          request=httpx.Request("POST", "http://test"))


def main():
    cfg = load_config(ROOT)
    cfg.retries = 2
    cfg.timeout = 5

    notes = []
    client = LLMClient(cfg.llm, on_retry=lambda note, a, t: notes.append((note, a, t)))

    # ── A. 网络错误两次 → 第三次成功 ──
    calls = {"n": 0}
    def flaky_post(url, **kw):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise httpx.ConnectError("refused", request=httpx.Request("POST", url))
        return resp(200, '{"ok": true}')
    with patch.object(httpx, "post", flaky_post):
        out = client.chat_json("t", "s", "u", Out)
    assert out.ok is True and calls["n"] == 3, (out, calls)
    assert len(notes) == 2, notes
    print(f"[A] 网络错误自动重试 OK（共调用 {calls['n']} 次，重试提示 {len(notes)} 条）")

    # ── B. 401 不重试，立即失败 ──
    calls["n"] = 0
    def unauthorized(url, **kw):
        calls["n"] += 1
        return resp(401, "bad key")
    with patch.object(httpx, "post", unauthorized):
        try:
            client.chat_json("t", "s", "u", Out)
            raise AssertionError("401 应抛错")
        except LLMError as e:
            assert "401" in str(e)
    assert calls["n"] == 1, f"401 不应重试，实际调用 {calls['n']} 次"
    print("[B] 401 不重试 OK")

    # ── C. 429 两次 → 成功 ──
    calls["n"] = 0
    notes.clear()
    def rate_limited(url, **kw):
        calls["n"] += 1
        if calls["n"] <= 2:
            return resp(429, "slow down")
        return resp(200, '{"ok": true}')
    with patch.object(httpx, "post", rate_limited):
        out = client.chat_json("t", "s", "u", Out)
    assert out.ok is True and calls["n"] == 3
    print(f"[C] 429 限流自动重试 OK（共调用 {calls['n']} 次）")

    # ── D. 重试耗尽仍失败 → 报错信息带重试次数 ──
    def always_down(url, **kw):
        raise httpx.ConnectError("refused", request=httpx.Request("POST", url))
    with patch.object(httpx, "post", always_down):
        try:
            client.chat_json("t", "s", "u", Out)
            raise AssertionError("应抛错")
        except LLMError as e:
            assert "已重试 3 次" in str(e), str(e)
    print("[D] 重试耗尽报错 OK")

    # ── E. 设置保存不丢字段：不传 api_key 保持原值，retries/timeout 保留 ──
    tmp = Path(tempfile.mkdtemp(prefix="ts-cfg-"))
    save_config(tmp, {"base_url": "https://x/v4", "api_key": "sk-123",
                      "model": "m1", "retries": 3, "timeout": 240})
    save_config(tmp, {"base_url": "https://y/v4", "api_key": "", "model": "m2"})  # 不带 key/retries
    c2 = load_config(tmp)
    assert c2.llm.api_key == "sk-123", f"key 被清空: {c2.llm.api_key}"
    assert c2.llm.retries == 3 and c2.llm.timeout == 240, vars(c2.llm)
    assert c2.llm.model == "m2" and c2.llm.base_url == "https://y/v4"
    print("[E] 配置合并保存 OK（空 Key 不清空、retries/timeout 保留）")

    print("\n重试与配置测试全部通过 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
