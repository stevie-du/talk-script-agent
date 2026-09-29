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
from app.server import NUMERIC_BOUNDS  # noqa: E402

# ⚠ 期望值**从 server 现读**，不抄副本。
# 这里曾经是一份手写的 SERVER_BOUNDS 副本（注释还写着"若 server 侧调整了区间，
# 这里必须同步"），于是断言只比「config 的表 ↔ 副本」，**从不读 app/server.py** ——
# 改 server 的区间不报红，两端夹逼口径可以静默漂移；而漂移的后果是
# 「环境变量比界面宽 / 严」，用户在界面上看不出任何异常。
# 抄一份常量当期望值，守卫就只在抄的那一刻有效 —— 而 app/config.py:267 的注释
# 声称"test_env_bounds.py 把这张表与 server 的 NUMERIC_BOUNDS 比对钉住"，
# 在改这里之前那句话是不成立的。
_ENV_KEYS = sorted(_ENV_NUM_BOUNDS)


def _server_bounds_as_env() -> dict:
    """server 的 NUMERIC_BOUNDS 用**字段名**（retries / max_tokens …），
    这里映射成环境变量名（TALKSCRIPT_RETRIES …），好与 config 那张表逐项比。"""
    return {f"TALKSCRIPT_{k.upper()}": tuple(v) for k, v in NUMERIC_BOUNDS.items()}


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
    """夹逼区间必须与 server.py 的 NUMERIC_BOUNDS 逐项一致（以 server 为准）。

    ⚠ 判据是「读 server 那份常量来比」。比手抄副本的话，改 server 不报红，
    这条断言就成了摆设 —— 本项目对「守卫自己不能被红」的判定 = 没有守卫。
    """
    expected = _server_bounds_as_env()
    assert _ENV_NUM_BOUNDS == expected, (_ENV_NUM_BOUNDS, expected)


# ── 手改 config.yaml 那条入口（P0-6）──────────────────────────
#
# 上面那几条走的是**环境变量**。而 README 第 205-207 行恰恰教用户改**文件**
# （temperature 与 max_tokens 已从设置页移除，只能改那个文件），而文件这条路
# 以前**什么都不查**：`load_config` 照单全收，越界值直落请求层。
#   retries: 9999 → llm.py 算成 10000 次尝试（约 150 次真实 HTTP 请求打向上游，
#                    再乘 JSON 解析重试）；max_tokens: 0 → 上游回 400；
#   timeout: 0 → 每次请求读超时被压到 1 秒。
# 而 `config_error` 与 `llm_defaulted` 都是空串 —— 设置页把这些值当
# 「用户配过的」显示，用户无从自查。这三条就是那个洞的三个面。
def _with_file_llm(**llm):
    """写一份 llm 段越界的 config.yaml 再 load_config（环境变量全部清干净）。"""
    lines = ["models:",
             "  - {id: m1, name: t, base_url: 'https://api.example/v1',"
             " api_key: 'sk-test', model: 'test-model'}",
             "active_model: m1",
             "llm:"]
    lines += [f"  {k}: {v}" for k, v in llm.items()]
    tmp = Path(tempfile.mkdtemp(prefix="ts-fileb-"))
    (tmp / "config.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    old = {k: os.environ.get(k) for k in _ENV_KEYS}
    for k in _ENV_KEYS:
        os.environ.pop(f"TALKSCRIPT_{k}", None)
    try:
        return load_config(tmp)
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(f"TALKSCRIPT_{k}", None)
            else:
                os.environ[f"TALKSCRIPT_{k}"] = v


def test_file_out_of_range_is_clamped():
    """★ 手改 config.yaml 的越界值必须被夹回合法区间（P0-6）。"""
    cfg = _with_file_llm(retries=9999, max_tokens=0, timeout=0, temperature=99)
    assert cfg.llm.retries == 10, cfg.llm.retries            # 上界
    assert cfg.llm.max_tokens == 256, cfg.llm.max_tokens    # 下界（0 不是合法预算）
    assert cfg.llm.timeout == 5.0, cfg.llm.timeout          # 下界
    assert cfg.llm.temperature == 2.0, cfg.llm.temperature  # 上界


def test_file_out_of_range_is_reported_as_defaulted():
    """★ 越界值必须**记账**：进 llm_defaulted，设置页才标「内置默认」。

    不记的后果与「非法值」（`temperature: 快`）一样：兜底值冒充用户输入 ——
    用户以为那份配置生效了。P3-4 已经为「非法值」记过账，越界这条是同一个洞的
    另一半，当时漏了（因为文件这条路根本不查区间，也就没有「兜底」发生）。
    """
    cfg = _with_file_llm(retries=9999, max_tokens=0)
    defaulted = set(getattr(cfg, "llm_defaulted", []) or [])
    assert "retries" in defaulted, cfg.llm_defaulted
    assert "max_tokens" in defaulted, cfg.llm_defaulted
    # 没越界的那个不该被记（记账要精确，否则设置页一片「内置默认」就没人信了）
    assert "temperature" not in defaulted or cfg.llm.temperature == 0.7


def test_file_in_range_is_kept_as_user_config():
    """反向：合法值不许被夹、不许被记成默认（否则功能被修坏了）。"""
    cfg = _with_file_llm(retries=5, timeout=300, max_tokens=8000, temperature=1.2)
    assert (cfg.llm.retries, cfg.llm.timeout,
            cfg.llm.max_tokens, cfg.llm.temperature) == (5, 300.0, 8000, 1.2)
    defaulted = set(getattr(cfg, "llm_defaulted", []) or [])
    assert not ({"retries", "timeout", "max_tokens", "temperature"} & defaulted), \
        f"合法值被误记成默认：{cfg.llm_defaulted}"


def test_zero_temperature_is_still_legal():
    """0.0 是合法采样温度（求确定性输出）—— 夹逼不许把它当成「没填」。

    这是 P0-2 那条（「`or 默认值` 把 0 当没填」）在区间层的对应不变量：
    夹逼用的是 `lo <= n <= hi`（闭区间），0.0 在 temperature 的区间内。
    """
    cfg = _with_file_llm(temperature=0)
    assert cfg.llm.temperature == 0.0, cfg.llm.temperature
    defaulted = set(getattr(cfg, "llm_defaulted", []) or [])
    assert "temperature" not in defaulted, "0.0 是合法值，不该被记成默认"


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
