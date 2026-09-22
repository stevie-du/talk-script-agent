# 环境变量数值夹逼测试（P2-11）
# 背景：环境变量路径曾绕过 server.set_config 的 NUMERIC_BOUNDS 那道闸，
# TALKSCRIPT_MAX_TOKENS=100 / RETRIES=999 / TIMEOUT=0 / TEMPERATURE=-5 全部原样生效
# （且 `_env_num` 的查键缺陷让夹逼代码从未执行过）。修复后必须夹回界内，
# 且区间与 app/server.py 的 NUMERIC_BOUNDS 逐项一致。
# 跑法：python tests/test_env_bounds.py   或   pytest tests/test_env_bounds.py
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402
from pydantic import BaseModel  # noqa: E402

from app.config import _ENV_NUM_BOUNDS, load_config  # noqa: E402

# 与 server.py NUMERIC_BOUNDS（app/server.py，唯一的一份）逐项相等的期望值。
# 若 server 侧调整了区间，这里必须同步 —— 两端夹逼口径不允许漂移。
SERVER_BOUNDS = {
    "TALKSCRIPT_MAX_TOKENS": (256, 200000),
    "TALKSCRIPT_RETRIES": (0, 10),
    "TALKSCRIPT_TIMEOUT": (5.0, 1800.0),
    "TALKSCRIPT_TEMPERATURE": (0.0, 2.0),
}

_ENV_KEYS = list(SERVER_BOUNDS)


def _tmp_cfg():
    tmp = Path(tempfile.mkdtemp(prefix="ts-envb-"))
    (tmp / "config.yaml").write_text(
        "models:\n"
        "  - {id: m1, name: t, base_url: 'https://api.example/v1',"
        " api_key: 'sk-test', model: 'test-model'}\n"
        "active_model: m1\n", encoding="utf-8")
    return tmp


def _with_env(**vals):
    """设环境变量后 load_config；结束后还原，不污染其它用例。"""
    old = {k: os.environ.get(k) for k in _ENV_KEYS}
    os.environ.update({f"TALKSCRIPT_{k}": str(v) for k, v in vals.items()})
    try:
        return load_config(_tmp_cfg())
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_bounds_table_matches_server():
    """夹逼区间必须与 server.py 的 NUMERIC_BOUNDS 逐项一致（以 server 为准）。"""
    assert _ENV_NUM_BOUNDS == SERVER_BOUNDS, _ENV_NUM_BOUNDS


def test_max_tokens_low_clamped():
    cfg = _with_env(MAX_TOKENS=100)
    assert cfg.llm.max_tokens == 256, cfg.llm.max_tokens


def test_max_tokens_high_clamped():
    cfg = _with_env(MAX_TOKENS=999999)
    assert cfg.llm.max_tokens == 200000, cfg.llm.max_tokens


def test_max_tokens_zero_clamped_and_never_sent():
    """MAX_TOKENS=0 被夹回 256 —— 0 不是合法预算，绝不能发给上游。"""
    cfg = _with_env(MAX_TOKENS=0)
    assert cfg.llm.max_tokens == 256, cfg.llm.max_tokens

    # 用夹逼后的配置真发一个请求，确认 payload 里 max_tokens 不是 0
    from app.llm import LLMClient

    class Out(BaseModel):
        ok: bool

    bodies = []

    def handler(req):
        bodies.append(json.loads(req.content))
        return httpx.Response(200, text=json.dumps(
            {"choices": [{"message": {"content": '{"ok": true}'}}]}))

    transport = httpx.MockTransport(handler)
    with patch("app.llm.get_client", lambda: httpx.Client(transport=transport)):
        out = LLMClient(cfg.llm).chat_json("t", "s", "u", Out)
    assert out.ok is True
    assert len(bodies) == 1 and bodies[0]["max_tokens"] == 256, bodies


def test_retries_high_clamped():
    cfg = _with_env(RETRIES=999)
    assert cfg.llm.retries == 10, cfg.llm.retries


def test_retries_low_clamped():
    cfg = _with_env(RETRIES=-3)
    assert cfg.llm.retries == 0, cfg.llm.retries


def test_timeout_zero_clamped():
    cfg = _with_env(TIMEOUT=0)
    assert cfg.llm.timeout == 5.0, cfg.llm.timeout


def test_timeout_high_clamped():
    cfg = _with_env(TIMEOUT=99999)
    assert cfg.llm.timeout == 1800.0, cfg.llm.timeout


def test_temperature_clamped_both_sides():
    cfg = _with_env(TEMPERATURE=-5)
    assert cfg.llm.temperature == 0.0, cfg.llm.temperature
    cfg = _with_env(TEMPERATURE=5)
    assert cfg.llm.temperature == 2.0, cfg.llm.temperature


def test_in_bounds_values_pass_through():
    """界内值原样生效（夹逼不能误伤合法配置）。"""
    cfg = _with_env(MAX_TOKENS=777, RETRIES=5, TIMEOUT=30, TEMPERATURE=0.1)
    assert cfg.llm.max_tokens == 777 and cfg.llm.retries == 5
    assert cfg.llm.timeout == 30.0 and cfg.llm.temperature == 0.1


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


if __name__ == "__main__":
    sys.exit(main())
