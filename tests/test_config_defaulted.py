# -*- coding: utf-8 -*-
"""没配过 ≠ 配错了：`llm_defaulted` 是 `config_error` 的姊妹信号。

背景
----
`config_error`（见 `test_config_corruption.py`）治的是「config.yaml 读坏了 →
退回内置默认 → 界面把它当用户配置显示」。但**从来没配过**会走到同一个结果：

    model=str(llm.get("model") or DEFAULT_CONFIG["llm"]["model"])

空值被兜成 `glm-4.7`，而这件事**一个信号都没有**（`config_error` 是空的）。于是

- 设置页「模型名」框里写着 `glm-4.7`；
- 输入区右侧的模型选择器也写着 `glm-4.7`；
- 用户以为模型已经配好了，点生成才发现「未配置 API Key」。

与 config_error 是**同一类静默降级，只是没坏、只是没配**。修法也是同一个：
把「这些值不是你给的」算出来、下发出去、让界面说出来（`glm-4.7（默认）`）。

判定口径 —— 三条都满足才算「内置默认」：
1. config.yaml 里没写这个键（缺失，或写了空值）；
2. 环境变量也没有覆盖它；
3. （由 1+2 推出）它的值就是内置默认。

⚠ **顺序不能反**：必须在环境变量覆盖**之前**记名单。覆盖之后两者长得一模一样，
再判断就分不清「用户配的」和「兜出来的」—— 本文件第 2 组就是守这一条的。
"""

import shutil
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import (DEFAULT_CONFIG, _LLM_ENV,  # noqa: E402
                        ensure_config_template, load_config)

ALL_KEYS = sorted(_LLM_ENV)
ENV_VARS = sorted(_LLM_ENV.values())


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """这些环境变量会改变判定结果 —— 本机真设了的话，测试会随环境飘。"""
    for k in ENV_VARS:
        monkeypatch.delenv(k, raising=False)


def _client(root: Path):
    try:
        from fastapi.testclient import TestClient
    except ImportError:                                   # pragma: no cover
        pytest.skip("未安装 fastapi/httpx")
    from app.server import create_app
    c = TestClient(create_app(root, token="df-token"),
                   base_url="http://127.0.0.1:8765",
                   raise_server_exceptions=False)
    c.headers.update({"X-TalkScript-Token": "df-token"})
    return c


def _seed_placeholder_model(root: Path) -> Path:
    """一条**空壳**模型（id = DEFAULT_MODEL["id"]，三个连接字段都空）。

    2026-09-17：全新安装**不再预置模型**（用户原话「没有内置默认的模型的，
    需要用户自己添加」），而本文件要验的「连接两项（base_url / model）算不算
    内置默认」只有在**存在一条模型**时才有对象 —— 显式造出来，
    不再依赖产品隐式预置（那种依赖会在产品语义变化时集体报红）。
    这也是**已有用户**文件里可能存着的形态（迁移产物）。
    """
    p = root / "config.yaml"
    p.write_text(
        "models:\n"
        "  - {id: m-default, name: '', base_url: '', api_key: '', model: ''}\n"
        "active_model: m-default\n", encoding="utf-8")
    return p


# ── 1. 全新安装：六个字段全是内置默认 ────────────────────────

def test_fresh_install_reports_every_llm_field_defaulted(tmp_path):
    """有模型时六个字段**一个都不该漏**；没有模型时只报数值四项。

    漏掉某个键的后果不是「少提示一条」，而是界面继续把那个值当用户配置显示 ——
    而它恰恰是最容易被忽略的那个（temperature / max_tokens 这类不显眼的）。

    ⚠ 2026-09-17：全新安装不再预置模型 → 连接两项（base_url / model）
    **无处依附**，此时报它们反而是错的（该说的是「还没有配置模型」）。
    所以这里验两态：空列表只报数值四项、有一条空壳模型才六项全报。
    """
    only_num = sorted(k for k in ALL_KEYS if k not in ("base_url", "model"))

    cfg0 = load_config(tmp_path)
    assert sorted(cfg0.llm_defaulted) == only_num, (
        f"没有模型时不该报连接字段：{cfg0.llm_defaulted}")

    _seed_placeholder_model(tmp_path)
    cfg = load_config(tmp_path)
    assert sorted(cfg.llm_defaulted) == ALL_KEYS, (
        f"这些字段应是内置默认：{ALL_KEYS}，实际 {cfg.llm_defaulted}")
    assert cfg.llm.model == DEFAULT_CONFIG["llm"]["model"]


def test_generated_template_counts_as_not_configured(tmp_path):
    """**本文件的核心**：首次运行写下的模板不能冒充用户配置。

    模板原来写的是真实值（`model: glm-4.7`、`retries: 2` …），于是
    「文件里有这个键」→ 判定成「用户配过」→ 界面把内置默认值当用户配置显示。
    模板是**程序写的**，不是用户配的 —— 用户第一次打开设置页看到
    `model: glm-4.7`，会以为自己已经配好了。

    修法是把模板里所有配置项注释掉（值与内置默认逐项相同，注释掉取值不变）。
    这条断言同时守两件事：判定口径没退化，且**取值没变**。
    """
    p = ensure_config_template(tmp_path)
    assert p is not None, "首次运行应当写下模板"
    # 模板里不含 models 段（连接信息是用户自己加的）→ 连接两项无处依附，
    # 此时只报数值四项。写一条空壳模型再验六项全报（见上一条的分工）。
    cfg0 = load_config(tmp_path)
    assert sorted(cfg0.llm_defaulted) == sorted(
        k for k in ALL_KEYS if k not in ("base_url", "model")), "程序写的模板被当成了用户配置"

    _seed_placeholder_model(tmp_path)
    cfg = load_config(tmp_path)
    assert sorted(cfg.llm_defaulted) == ALL_KEYS, "程序写的模板被当成了用户配置"
    assert cfg.llm.model == DEFAULT_CONFIG["llm"]["model"], "注释掉之后取值必须不变"
    assert cfg.llm.retries == DEFAULT_CONFIG["llm"]["retries"]
    assert cfg.llm.max_tokens == DEFAULT_CONFIG["llm"]["max_tokens"]


def test_never_configured_has_no_config_error(tmp_path):
    """⚠ 这条是「为什么需要新信号」的证据：没配过时 `config_error` 是空的。

    也就是说，光靠 config_error **完全区分不出**「没配」和「配好了」——
    而这正是用户看到 `glm-4.7` 却以为配好了的原因。
    """
    cfg = load_config(tmp_path)
    assert cfg.config_error == "", "没配过不该报「读坏了」"
    assert cfg.llm_defaulted, "但必须另有信号说明这些值不是你配的"


# ── 2. 环境变量：覆盖了的字段不算「没配」 ────────────────────

# 每个字段给一个**类型合法**的值：数值字段给字符串的话 `_env_num` 会拒收、
# 值退回内置默认，那按下面的口径就仍算「没配」（见 test_invalid_env_value_...）。
_VALID = {"base_url": "https://env/v4", "model": "env-model",
          "temperature": "0.2", "retries": "7", "timeout": "33", "max_tokens": "999"}


@pytest.mark.parametrize("field,env_name", sorted(_LLM_ENV.items()))
def test_env_override_removes_field_from_defaulted(tmp_path, monkeypatch, field, env_name):
    """环境变量补上的字段不算「没配」—— 用户配了，只是配在环境里。

    ⚠ 这条同时守住**判定顺序**：如果先做覆盖、再拿「值等不等于默认」去猜，
    就把「用户显式配成默认值」误判成「没配」。所以判定必须在覆盖**之前**记名单。
    """
    monkeypatch.setenv(env_name, _VALID[field])
    # 连接两项（base_url / model）只在**存在一条模型**时才进名单 —— 显式造一条。
    _seed_placeholder_model(tmp_path)
    cfg = load_config(tmp_path)
    assert field not in cfg.llm_defaulted, f"{env_name} 已覆盖，不该还在名单里"
    # 别的字段照旧 —— 不能因为配了一个就整体放行。
    assert len(cfg.llm_defaulted) == len(ALL_KEYS) - 1


def test_invalid_env_value_stays_defaulted(tmp_path, monkeypatch):
    """环境变量给了**不合法**的值时，值静默退回内置默认 —— 那还是「没配」。

    这是本文件里最容易写错的一条：按「变量设过就划掉」处理，会把这次静默兜底
    藏起来（界面会说「这是你配的」，而实际生效的是内置默认）。
    判据必须看**值有没有被接受**，不能只看变量存不存在。
    """
    monkeypatch.setenv("TALKSCRIPT_RETRIES", "abc")
    cfg = load_config(tmp_path)
    assert cfg.llm.retries == 2, "不合法值应退回内置默认（这是既有行为）"
    assert "retries" in cfg.llm_defaulted, "退回默认了，却报告「这是用户配的」"


# ── 3. 文件里写了：那个字段不算「没配」，没写的仍算 ──────────

def test_explicit_file_value_is_not_defaulted(tmp_path):
    (tmp_path / "config.yaml").write_text(
        "llm:\n  model: my-own-model\n  retries: 7\n", encoding="utf-8")
    cfg = load_config(tmp_path)
    assert cfg.llm.model == "my-own-model"
    assert "model" not in cfg.llm_defaulted
    assert "retries" not in cfg.llm_defaulted
    # 没写的照旧在名单里 —— 不能因为「配了别的字段」就整体放行。
    assert "base_url" in cfg.llm_defaulted
    assert "temperature" in cfg.llm_defaulted


@pytest.mark.parametrize("raw", ["", "  ", "null", "~"])
def test_empty_value_counts_as_not_configured(tmp_path, raw):
    """写了空值 = 没配。

    这正是 `llm.get("model") or DEFAULT` 那个 `or` 兜住的形态：
    `model: ""` 与「键缺失」走到完全相同的默认值，判定也必须一致 ——
    否则用户会看到「模型名框是空的、但没提示这是默认值」。
    """
    (tmp_path / "config.yaml").write_text(
        "models:\n"
        f"  - {{id: m1, base_url: 'https://a/v1', model: {raw}}}\n"
        "active_model: m1\n", encoding="utf-8")
    cfg = load_config(tmp_path)
    assert cfg.llm.model == DEFAULT_CONFIG["llm"]["model"]
    assert "model" in cfg.llm_defaulted


def test_corrupt_file_reports_both_signals(tmp_path):
    """读坏时两个信号都要有：`config_error` 说「读坏了」，`llm_defaulted` 说「不是你的」。

    只给前者的话，界面只会说「语法有误」；用户改好语法、键却仍然没写，
    下一屏照样把 `glm-4.7` 当他的配置显示。
    """
    (tmp_path / "config.yaml").write_text("llm:\n  model: [unclosed\n", encoding="utf-8")
    cfg = load_config(tmp_path)
    assert cfg.config_error
    # 2026-09-17：文件读坏 → models 段也读不到 → 一条模型都没有。
    # 所以这里报的是**数值四项**；连接两项此刻无处依附（正确的说法是
    # 「还没有配置模型」，由界面另说 —— 见 verify.js 的读坏断言）。
    assert sorted(cfg.llm_defaulted) == sorted(
        k for k in ALL_KEYS if k not in ("base_url", "model"))


# ── 4. 透出到接口：算出来但不下发 = 白算 ────────────────────

def test_api_config_exposes_defaulted(tmp_path):
    """前端要拿得到 —— 拿不到就还是不可见（本项目最忌讳的那种「白算」）。"""
    _seed_placeholder_model(tmp_path)
    body = _client(tmp_path).get("/api/config").json()
    assert sorted(body["llm_defaulted"]) == ALL_KEYS


def test_api_meta_exposes_defaulted(tmp_path):
    """输入区右侧的模型选择器读的是 /api/meta，不是 /api/config。"""
    _seed_placeholder_model(tmp_path)
    body = _client(tmp_path).get("/api/meta").json()
    assert "model" in body["llm_defaulted"]


def test_api_config_defaulted_clears_after_save(tmp_path):
    """存过一次之后就不再是「没配」—— 提示必须消失，否则用户会一直看到它。"""
    _seed_placeholder_model(tmp_path)
    c = _client(tmp_path)
    assert "model" in c.get("/api/config").json()["llm_defaulted"]

    assert c.post("/api/config", json={"model": "my-own-model"}).status_code == 200

    body = c.get("/api/config").json()
    assert body["model"] == "my-own-model"
    assert "model" not in body["llm_defaulted"], "配过了却还说「不是你配的」"
    # 别的字段仍然没配 —— 保存只应该划掉它自己。
    assert "base_url" in body["llm_defaulted"]


def main() -> int:                                        # pragma: no cover
    """独立跑一遍（不依赖 pytest 的 fixture，只跑不需要 tmp_path 的那几条）。"""
    tmp = Path(tempfile.mkdtemp(prefix="talkscript-df-"))
    try:
        cfg = load_config(tmp)
        # 2026-09-17：全新安装不再预置模型 → 连接两项无处依附，只报数值四项
        only_num = sorted(k for k in ALL_KEYS if k not in ("base_url", "model"))
        assert sorted(cfg.llm_defaulted) == only_num
        assert cfg.config_error == ""
        print("  ✅ 全新安装：数值四项报「内置默认」，且没有 config_error")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":                                # pragma: no cover
    sys.exit(main())
