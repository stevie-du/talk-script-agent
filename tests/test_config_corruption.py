# -*- coding: utf-8 -*-
"""config.yaml 读坏时：引擎照常起来，但**这件事必须可见**。

背景
----
修复前 `load_config` 里是 `except (OSError, yaml.YAMLError): data = {}` ——
一个不记日志的裸兜底。多打一个空格缩进就能让用户自填的
`base_url` / `model` / `api_key` **全部失效**，而设置页会照常显示
「已配置 Key · 模型 glm-4.7」（那是内置默认值），用户完全看不出
自己填的东西一次都没生效过。

这是本项目最忌讳的那类缺陷：不报错、不可见、与用户认知相反。
所以修法不是「让它别崩」（它本来就没崩），而是**让不可见的失效变得可见**：

1. 后端 `read_config_file` 记 WARNING，并把一句话说明挂到 `AppConfig.config_error`；
2. `/api/config` 把它下发；
3. 设置页顶部显一行警示 —— 明确说「上面显示的是默认值，不是你保存过的配置」。

第 4 组断言是**安全约束**，但结论要记准（实测 2026-09-15）：

- **当前读路径本来就不泄漏。** `read_config_file` 把**文件对象**交给
  `yaml.safe_load`，PyYAML 的流式 Reader 不保留完整 buffer →
  `Mark.get_snippet()` 返回 None → `str(e)` 只有 `line N, column M`，没有正文。
- **换成字符串路径立刻泄漏。** 同一个坏文件改成
  `yaml.safe_load(p.read_text())`，`str(e)` 就带上出错行原文（`api_key: sk-…`）。
  所以 `fileio.read_yaml_file` 一律走**文件对象**。

所以 `yaml_error_brief()` 的定位是「**不让安全性质取决于用了哪种读法**」，
不是「修一个已发生的泄漏」。测试也据此分成两层：

| 层 | 测什么 | 能否证伪 |
|---|---|---|
| `test_yaml_error_never_carries_source_snippet` | 直接喂**字符串输入**的异常（真带正文的那种） | ✅ 把函数体改成 `str(e)` → 红 |
| `test_message_points_at_line_and_column` | 出口消息形态与输入方式无关 | ✅ 调用点改成 `str(e)` → 红（英文 line/column） |

`test_file_stream_path_does_not_leak_today` 是**前提守卫**：钉住「文件流不泄漏」
这个实测事实，它一旦变化（PyYAML 行为改了）会红，提醒重估结论。

> **2026-09-15 后续（P1-5/P1-6）**：`yaml_error_brief` 已从 `config` 上移到
> `fileio`（公开名，去掉了下划线），因为行业包读 YAML 也要用它 ——
> 口径只留一份，且 `read_yaml_file` 统一走文件对象，
> 那条「只差一行」的泄漏路径已不存在。
"""
from __future__ import annotations

import logging
import shutil
import sys
import tempfile
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import (DEFAULT_CONFIG,  # noqa: E402
                        load_config, read_config_file, save_config)
from app.fileio import yaml_error_brief     # noqa: E402

SECRET = "sk-super-secret-do-not-leak-9f3a"

# 「密钥所在行本身就是出错行」—— 最容易被 `str(e)` 带出去的那几种写法。
# 注意 `: extra` / 未闭合的 `[` / 未闭合的引号 三者都能让 problem_mark 落回该行。
LEAKY = {
    "冒号二次出现": f"llm:\n  api_key: {SECRET}: extra\n  model: glm\n",
    "未闭合流序列": f"llm:\n  api_key: [{SECRET}, x\n",
    "未闭合引号": f"llm:\n  api_key: '{SECRET}\n",
}

# 其余坏法：或是不构成 YAML 异常（顶层类型不对），或是异常落在别的行。
OTHER_BROKEN = {
    "密钥行后紧接坏行": f"llm:\n  api_key: {SECRET}\n  model: [unclosed\n",
    "块映射里混了列表项": "llm:\n  - a\n  - b\n  model: glm\n",
    "顶层是列表": "- a\n- b\n",
    "顶层是字符串": "just a bare string\n",
}

BROKEN = {**LEAKY, **OTHER_BROKEN}

# 语法错（异常）与顶层类型错 —— 两类给出的说明文案不同，断言要分开。
YAML_ERRORS = {**LEAKY, "密钥行后紧接坏行": OTHER_BROKEN["密钥行后紧接坏行"],
               "块映射里混了列表项": OTHER_BROKEN["块映射里混了列表项"]}
NOT_MAPPING = {"顶层是列表": OTHER_BROKEN["顶层是列表"],
               "顶层是字符串": OTHER_BROKEN["顶层是字符串"]}


def _root() -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="talkscript-cfg-"))
    shutil.copytree(ROOT / "packs", tmp / "packs")
    return tmp


# ── 1. 不崩 + 报错信息 ────────────────────────────────────────

@pytest.mark.parametrize("name", sorted(BROKEN))
def test_broken_config_does_not_raise_and_is_reported(name, tmp_path):
    """每一种坏法都要：① 不抛异常；② `config_error` 非空。"""
    (tmp_path / "config.yaml").write_text(BROKEN[name], encoding="utf-8")
    cfg = load_config(tmp_path)
    assert cfg.config_error, f"{name}：读坏了却报告「没问题」"
    assert cfg.llm.model == DEFAULT_CONFIG["llm"]["model"]
    assert cfg.llm.base_url == DEFAULT_CONFIG["llm"]["base_url"]


def test_good_config_reports_nothing(tmp_path):
    """正常配置不能误报 —— 误报会让这条提示彻底失去可信度。"""
    (tmp_path / "config.yaml").write_text(
        "llm:\n  model: my-model\n  temperature: 0.3\n", encoding="utf-8")
    cfg = load_config(tmp_path)
    assert cfg.config_error == ""
    assert cfg.llm.model == "my-model"
    assert cfg.llm.temperature == 0.3


def test_missing_file_is_not_an_error(tmp_path):
    """首次运行没有 config.yaml —— 这是正常状态，不该报「读坏了」。"""
    assert not (tmp_path / "config.yaml").exists()
    cfg = load_config(tmp_path)
    assert cfg.config_error == ""
    assert cfg.llm.model == DEFAULT_CONFIG["llm"]["model"]


def test_empty_file_is_not_an_error(tmp_path):
    (tmp_path / "config.yaml").write_text("", encoding="utf-8")
    assert load_config(tmp_path).config_error == ""


# ── 2. 日志（不可见 → 可见的第一层）──────────────────────────

@pytest.mark.parametrize("name", sorted(BROKEN))
def test_broken_config_logs_a_warning(name, tmp_path, caplog):
    (tmp_path / "config.yaml").write_text(BROKEN[name], encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="app.config"):
        load_config(tmp_path)
    assert any(r.levelno == logging.WARNING for r in caplog.records), \
        f"{name}：静默吞掉了"


def test_good_config_logs_nothing(tmp_path, caplog):
    (tmp_path / "config.yaml").write_text("llm:\n  model: ok\n", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="app.config"):
        load_config(tmp_path)
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


# ── 3. 提示要能指到行号（用户才有得改）────────────────────────

@pytest.mark.parametrize("name", sorted(YAML_ERRORS))
def test_message_points_at_line_and_column(name, tmp_path):
    """位置必须是中文「第 N 行第 M 列」。

    `str(e)` 给的是英文 `line N, column M` —— 所以这条同时也守住
    「调用点没有退回 `str(e)`」。
    """
    (tmp_path / "config.yaml").write_text(YAML_ERRORS[name], encoding="utf-8")
    err = load_config(tmp_path).config_error
    assert "config.yaml" in err
    assert "第 " in err and " 行第 " in err and " 列" in err, \
        f"{name}：没给出中文位置，用户无从下手：{err}"


@pytest.mark.parametrize("name", sorted(NOT_MAPPING))
def test_top_level_not_mapping_is_described(name, tmp_path):
    (tmp_path / "config.yaml").write_text(NOT_MAPPING[name], encoding="utf-8")
    err = load_config(tmp_path).config_error
    assert "映射" in err, f"{name}：说明没讲清是「顶层类型不对」：{err}"


# ── 4. 安全：错误说明不得带出配置正文（可能含 api_key）────────

def _string_input_error(text: str) -> yaml.YAMLError:
    """用**字符串**输入解析，拿到带正文片段的异常。

    只有这条路径会带正文（见文件头说明）—— 也正因如此，它才是
    检验脱敏是否真的生效的唯一入口。
    """
    with pytest.raises(yaml.YAMLError) as ei:
        yaml.safe_load(text)
    return ei.value


@pytest.mark.parametrize("name", sorted(LEAKY))
def test_yaml_error_never_carries_source_snippet(name):
    """核心断言：**带正文的那种异常**，脱敏后必须一个字符都不带出来。

    把 `yaml_error_brief` 函数体换成 `str(e)` → 三条全红。
    """
    e = _string_input_error(LEAKY[name])
    # 先确认前提：这个异常本身确实带正文，否则本断言是空转
    assert SECRET in str(e), f"{name}：前提变了，该形态的异常不再带正文"
    brief = yaml_error_brief(e)
    assert SECRET not in brief, f"{name}：错误说明带出了密钥！"
    assert "api_key" not in brief
    assert "\n" not in brief, "说明必须是单行（要嵌进一行提示里）"


def test_brief_still_says_what_is_wrong():
    """脱敏不能把信息脱没了 —— 原因和位置都要留。"""
    e = _string_input_error(LEAKY["冒号二次出现"])
    brief = yaml_error_brief(e)
    assert "mapping values are not allowed here" in brief
    # 位置由 mark 现算，不硬编码列号 —— 改 SECRET 的长度它就会变
    m = e.problem_mark
    assert f"第 {m.line + 1} 行第 {m.column + 1} 列" in brief


def test_file_stream_path_does_not_leak_today(tmp_path):
    """前提守卫：钉住「文件流输入下 `str(e)` 不带正文」这个实测事实。

    它一旦变红，说明 PyYAML 的 Reader 行为变了（或读法被改了），
    上面 `yaml_error_brief` 的存在理由需要重新评估 —— 结论要重记，
    别让注释里那段「实测」悄悄过期。
    """
    p = tmp_path / "config.yaml"
    for name, text in LEAKY.items():
        p.write_text(text, encoding="utf-8")
        with pytest.raises(yaml.YAMLError) as ei:
            with open(p, encoding="utf-8") as f:
                yaml.safe_load(f)
        assert SECRET not in str(ei.value), \
            f"{name}：文件流路径也开始带正文了 —— 注释与结论都要更新"


@pytest.mark.parametrize("name", sorted(BROKEN))
def test_error_message_never_leaks_secret(name, tmp_path):
    """出口（`load_config` → `/api/config`）不得带出正文。"""
    (tmp_path / "config.yaml").write_text(BROKEN[name], encoding="utf-8")
    err = load_config(tmp_path).config_error
    assert SECRET not in err, f"{name}：错误说明里带出了密钥！"
    assert "extra" not in err


@pytest.mark.parametrize("name", sorted(BROKEN))
def test_log_record_never_leaks_secret(name, tmp_path, caplog):
    (tmp_path / "config.yaml").write_text(BROKEN[name], encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="app.config"):
        load_config(tmp_path)
    for r in caplog.records:
        assert SECRET not in r.getMessage(), f"{name}：日志里带出了密钥！"


# ── 5. 透出到 /api/config ────────────────────────────────────

def _client(root: Path):
    try:
        from fastapi.testclient import TestClient
    except ImportError:                                   # pragma: no cover
        pytest.skip("未安装 fastapi/httpx")
    from app.server import create_app
    c = TestClient(create_app(root, token="cfg-token"),
                   base_url="http://127.0.0.1:8765",
                   raise_server_exceptions=False)
    c.headers.update({"X-TalkScript-Token": "cfg-token"})
    return c


def test_api_config_exposes_error(tmp_path):
    """前端要拿得到 —— 拿不到就还是不可见。"""
    (tmp_path / "config.yaml").write_text(LEAKY["冒号二次出现"], encoding="utf-8")
    body = _client(tmp_path).get("/api/config").json()
    assert body["config_error"], "读坏了却没下发"
    assert SECRET not in body["config_error"]


def test_api_config_error_clears_after_save(tmp_path):
    """重存一次就该恢复 —— 提示里承诺的「重新保存即可覆盖修复」必须成立。

    这条同时守住 `/api/config` 是**现读**配置（`_cfg()` 不缓存），
    否则用户修好了、界面还挂着旧提示。
    """
    (tmp_path / "config.yaml").write_text("llm:\n  model: [unclosed\n", encoding="utf-8")
    c = _client(tmp_path)
    assert c.get("/api/config").json()["config_error"]

    assert c.post("/api/config", json={"model": "fixed-model"}).status_code == 200

    body = c.get("/api/config").json()
    assert body["config_error"] == "", "修好了提示却没消失"
    assert body["model"] == "fixed-model"


def test_save_config_repairs_broken_file(tmp_path):
    """save_config 走的是同一个读函数 —— 读坏时也能把文件写回合法状态。"""
    p = tmp_path / "config.yaml"
    p.write_text("llm:\n  model: [unclosed\n", encoding="utf-8")
    save_config(tmp_path, {"model": "repaired"})
    assert isinstance(yaml.safe_load(p.read_text(encoding="utf-8")), dict)
    assert load_config(tmp_path).config_error == ""


def test_read_config_file_reports_and_recovers(tmp_path):
    """直接测底层函数：坏 → 报错；修好 → 报错清空。"""
    p = tmp_path / "config.yaml"
    p.write_text("llm:\n  model: [unclosed\n", encoding="utf-8")
    data, err = read_config_file(p)
    assert data == {} and err

    p.write_text("llm:\n  model: ok\n", encoding="utf-8")
    data, err = read_config_file(p)
    assert err == "" and data["llm"]["model"] == "ok"
