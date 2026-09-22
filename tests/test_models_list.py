# -*- coding: utf-8 -*-
"""模型从「一条」变成「一份列表」：迁移、增删改切换、以及密钥不出门。

背景
----
原来模型是**三个平铺字段**（`llm.base_url` / `llm.api_key` / `llm.model`），
换一家服务商就得把地址和 Key 整个重填一遍。改成列表之后：

    models:
      - {id: m1, name: 智谱, base_url: ..., api_key: ..., model: glm-4.7}
      - {id: m2, name: DeepSeek, base_url: ..., api_key: ..., model: deepseek-chat}
    active_model: m2

每条**自带** base_url 与 api_key（这就是多模型的意义），生成参数
（temperature / retries / timeout / max_tokens）仍是全局一份 —— 那些是
「怎么调模型」，跟连到哪家无关。

本文件守四条线，每条都对应一个**已经踩过或极易踩到**的坑：

1. **迁移不能丢配置**（第 1 组）—— 老用户升级后 base_url / api_key 明明还在
   文件里，界面却显示「一个模型都没有」，是这类改动最典型的静默降级。
2. **写路径必须基于原始条目**（第 2 组）—— 见 `_raw_entry_keeps_empty_fields`，
   这是本文件里最容易写错、也最难发现的一条。
3. **密钥不出门**（第 3 组）—— 渲染层是网页，明文进去就等于进了 DOM。
4. **增删改切换的边界**（第 4 组）—— 删到一条不剩 / 删掉当前那条 / 切到
   不存在的 id，都不能留下悬空引用。
"""

import json
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import (DEFAULT_CONFIG, DEFAULT_MODEL,  # noqa: E402
                        load_config, load_raw_models, public_models,
                        save_models)

SECRET = "sk-do-not-leak-8f3a1c"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """环境变量会盖掉当前模型的连接信息，让断言随本机环境飘。"""
    for k in ("TALKSCRIPT_API_KEY", "TALKSCRIPT_BASE_URL", "TALKSCRIPT_MODEL"):
        monkeypatch.delenv(k, raising=False)


def _client(root: Path, seed: bool = True):
    try:
        from fastapi.testclient import TestClient
    except ImportError:                                   # pragma: no cover
        pytest.skip("未安装 fastapi/httpx")
    from app.server import create_app
    # 2026-09-17：全新安装**不再预置模型**（用户原话「没有内置默认的模型的，
    # 需要用户自己添加，添加完还需要支持删除」）。而本文件绝大多数测试测的是
    # 「已有模型时的增删改 / 迁移 / 密钥剥离」，需要一个起点 —— 由测试自己显式造，
    # 不再依赖产品隐式预置（那种依赖会在产品语义变化时集体报红，
    # 而它们守的东西其实没变）。文件已存在时不覆盖：`_write_legacy()` 先写过就尊重它。
    if seed and not (root / "config.yaml").exists():
        _seed_placeholder_model(root)
    c = TestClient(create_app(root, token="ml-token"),
                   base_url="http://127.0.0.1:8765",
                   raise_server_exceptions=False)
    c.headers.update({"X-TalkScript-Token": "ml-token"})
    return c


def _write_legacy(root: Path, **llm) -> Path:
    """写一份**单模型时代**的 config.yaml（只有 llm 段）。"""
    p = root / "config.yaml"
    body = "".join(f"  {k}: {v}\n" for k, v in llm.items())
    p.write_text(f"llm:\n{body}", encoding="utf-8")
    return p


def _seed_models(root: Path, *items: dict, active: str | None = None) -> Path:
    """显式写一份 `models` 段。"""
    lines = ["models:"]
    for it in items:
        body = ", ".join(f"{k}: {v!r}" for k, v in it.items())
        lines.append(f"  - {{{body}}}")
    if active is not None:
        lines.append(f"active_model: {active!r}")
    p = root / "config.yaml"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def _seed_placeholder_model(root: Path) -> Path:
    """一条**空壳**模型（id = DEFAULT_MODEL["id"]，三个连接字段都空）。

    这正是 2026-09-17 之前「全新安装自动预置」的那条。产品上不再预置了，
    但**已有用户**的文件里可能存着这么一条（迁移产物），且本文件要测的
    迁移 / 写路径 / 密钥剥离 / 增删改边界都需要一个起点。
    """
    return _seed_models(root, {"id": DEFAULT_MODEL["id"], "name": "",
                               "base_url": "", "api_key": "", "model": ""},
                        active=DEFAULT_MODEL["id"])


# ── 1. 迁移：老文件不能变成「一个模型都没有」──────────────────

def test_legacy_config_migrates_into_one_model(tmp_path):
    """只有 llm 段的旧文件 → 读出一条模型，连接信息逐项带过来。

    这是本组最重要的一条：迁移的失败形态不是报错，而是**界面空空如也**，
    用户以为配置丢了（其实还在文件里，只是没人去读它）。
    """
    _write_legacy(tmp_path, base_url="https://old.example/v1",
                  api_key=SECRET, model="old-model")
    cfg = load_config(tmp_path)
    assert len(cfg.models) == 1
    m = cfg.models[0]
    assert (m.base_url, m.model, m.api_key) == ("https://old.example/v1",
                                                "old-model", SECRET)
    assert cfg.active_model == m.id, "迁移出来的那条必须是当前生效的"
    # 生效配置（发请求用的那份）也要跟着走，不能只改了列表。
    assert cfg.llm.base_url == "https://old.example/v1"
    assert cfg.llm.model == "old-model"
    assert cfg.llm.api_key == SECRET


def test_migration_is_read_only_and_idempotent(tmp_path):
    """迁移**只在读的时候发生**：读十次结果一样，且不写回文件。

    写回文件的话，用户只是打开了一次应用，config.yaml 就被改写了 ——
    下次他用 git diff 或者手工编辑时会看到一堆自己没动过的改动。
    """
    p = _write_legacy(tmp_path, base_url="https://old.example/v1", model="old-model")
    before = p.read_text(encoding="utf-8")
    first = load_config(tmp_path)
    for _ in range(9):
        again = load_config(tmp_path)
        assert [m.id for m in again.models] == [m.id for m in first.models]
        assert again.models[0].base_url == first.models[0].base_url
    assert p.read_text(encoding="utf-8") == before, "迁移不该写回文件"


def test_migration_keeps_defaulted_signal_per_field(tmp_path):
    """迁移之后，「哪些字段还是内置默认」必须**逐字段**判。

    老文件里只写了 model、没写 base_url —— 此时 base_url 仍是内置默认，
    界面必须照旧标「内置默认」。按「有没有 models 段」一刀切就会说成
    「这是你配的」，把没配过的地址显示成用户配置。
    """
    _write_legacy(tmp_path, model="only-model")
    cfg = load_config(tmp_path)
    assert cfg.llm.base_url == DEFAULT_MODEL["base_url"]
    assert "base_url" in cfg.llm_defaulted, "没写过的地址被说成「你配的」"
    assert "model" not in cfg.llm_defaulted, "写过的模型名不该还在名单里"


def test_fresh_install_has_no_model(tmp_path):
    """全新安装**一条模型都没有**（2026-09-17 产品决定）。

    用户原话：「没有内置默认的模型的，需要用户自己添加，添加完还需要支持删除」。
    预置一条 glm-4.7 的后果是未配置状态与已配置状态长得一样 ——
    用户以为已经配好了（`MEMORY.md` 第一节第 ⑤ 种静默降级）。

    ⚠ 与 `test_legacy_config_migrates_into_one_model` 是一对：
    老文件（llm 段里有真实值）**仍要迁移**（否则静默丢配置），
    新文件（llm 段全空）才是空列表。判据是「llm 段里有没有任何非空值」。
    """
    cfg = load_config(tmp_path)
    assert cfg.models == [], "全新安装不该预置模型"
    assert cfg.active_model == "", "没有模型时 active 必须是空串（不留悬空引用）"
    # 不能崩：load_config 里那句 next(...) 在空列表上会 StopIteration
    assert cfg.llm.api_key == "" and cfg.llm.model == ""


def test_fresh_install_can_add_then_delete_back_to_empty(tmp_path):
    """空 → 加一条 → 删掉 → 又空。这是用户原话的完整路径。

    原来 `delete_model` 拦着「至少要保留一个模型」—— 那是「总有一条内置默认」
    时代的规则。现在模型是用户自己加的，删光就是「还没配」，
    生成前会被 `_require_model` 明确拦住（说清「还没有配置模型」）。
    """
    c = _client(tmp_path, seed=False)
    assert c.get("/api/config").json()["models"] == []

    r = c.post("/api/models", json={"id": "", "name": "智谱",
                                    "base_url": "https://x/v4", "model": "glm-4.7",
                                    "api_key": SECRET})
    assert r.status_code == 200, r.text
    body = c.get("/api/config").json()
    assert [m["id"] for m in body["models"]] == ["m1"]
    assert body["active_model"] == "m1", "第一条加进来就该是当前生效的"

    r = c.post("/api/models/delete", json={"id": "m1"})
    assert r.status_code == 200, r.text
    body = c.get("/api/config").json()
    assert body["models"] == [] and body["active_model"] == ""
    # 文件里的 active_model 也要被清掉，不能留一个指向不存在条目的悬空值
    raw, active = load_raw_models(tmp_path)
    assert raw == [] and active == ""


def test_setup_refusals_still_match_the_renderer_regex():
    """服务端那三句"没法生成"必须仍然落进 `api.js` 的 `MODEL_SETUP_REPLY`。

    `jobs.js:148` 的兜底是 `modelSetupGap(meta) || MODEL_SETUP_REPLY.test(e.message)`：
    meta 看起来正常而服务端仍拒绝（配置文件被手改坏、加载后又被删干净）时，
    界面靠这条正则把用户带回「设置 → 模型接口」。改文案的人不会想到有个正则在
    读它 —— 这条守卫就是让那次改动当场报红。判据从两边各读一份事实，不抄文案。
    ⚠ 「缺少或无效的访问令牌」那句必须**不**匹配：它要的是换地址重开，不是去配模型。
    """
    import ast
    import re

    srv = (ROOT / "app" / "server.py").read_text(encoding="utf-8")
    fn = next(n for n in ast.walk(ast.parse(srv))
              if isinstance(n, ast.FunctionDef) and n.name == "_require_model")
    refusals = []
    for node in ast.walk(fn):
        if (isinstance(node, ast.Call) and getattr(node.func, "id", "") == "HTTPException"
                and len(node.args) >= 2 and isinstance(node.args[1], ast.Constant)):
            refusals.append(str(node.args[1].value))
    assert len(refusals) >= 3, f"三种缺法该各自说清，只读到 {len(refusals)} 句：{refusals}"

    js = (ROOT / "desktop" / "renderer" / "js" / "api.js").read_text(encoding="utf-8")
    m = re.search(r"MODEL_SETUP_REPLY\s*=\s*/(.+?)/;", js)
    assert m, "api.js 里找不到 MODEL_SETUP_REPLY，这条守卫该改写法了"
    pattern = re.compile(m.group(1))
    for msg in refusals:
        assert pattern.search(msg), f"这句界面认不出来、不会把用户带去设置：{msg}"
    token_msg = "缺少或无效的访问令牌。请通过应用入口打开界面，或使用启动时打印的带 token 的地址。"
    assert not pattern.search(token_msg), "令牌问题不该被当成没配模型"


def test_generate_without_any_model_is_refused_with_a_clear_reason(tmp_path):
    """一条模型都没有时生成 → 400 且说清「还没配置模型」。

    与「有模型但没填 Key」是**两种不同的缺**：前者要去「添加模型」，
    后者才是填 Key。同一句「未配置 Key」会把用户指向错的地方。
    """
    c = _client(tmp_path, seed=False)
    r = c.post("/api/generate", json={"topic": "家用电梯怎么挑？"})
    assert r.status_code == 400, r.text
    assert "还没有配置模型" in r.json()["detail"], r.json()


def test_dangling_active_model_falls_back_to_first(tmp_path):
    """文件里 active_model 指向一条不存在的记录 → 退回第一条。

    手工编辑 config.yaml 删掉一条模型是常见操作。留一个悬空的 active，
    `load_config` 里那句 `next(r for r in raw_models if r["id"] == active)`
    会直接 StopIteration —— 引擎起不来，而用户只是删了个模型。
    """
    (tmp_path / "config.yaml").write_text(
        "models:\n"
        "  - {id: a, base_url: 'https://a/v1', model: a}\n"
        "  - {id: b, base_url: 'https://b/v1', model: b}\n"
        "active_model: ghost\n", encoding="utf-8")
    cfg = load_config(tmp_path)
    assert cfg.active_model == "a", "悬空的 active 必须退回第一条"
    assert cfg.llm.model == "a"


def test_duplicate_ids_are_disambiguated(tmp_path):
    """手改文件写出重复 id 时，要拆开。

    重复 id 的后果不是「少一条」：增删改都按 id 找，两条同 id 会让
    「当前模型指向哪一条」变得不确定 —— 用户切了模型却没换。
    """
    (tmp_path / "config.yaml").write_text(
        "models:\n"
        "  - {id: dup, base_url: 'https://a/v1', model: a}\n"
        "  - {id: dup, base_url: 'https://b/v1', model: b}\n"
        "active_model: dup\n", encoding="utf-8")
    cfg = load_config(tmp_path)
    ids = [m.id for m in cfg.models]
    assert len(ids) == len(set(ids)) == 2, f"id 没有拆开：{ids}"
    assert cfg.active_model == ids[0], "当前模型必须指向确定的那一条"


# ── 2. 写路径：基于**原始条目**，空字段保持空 ─────────────────

def test_raw_entry_keeps_empty_fields(tmp_path):
    """**本文件的核心断言**：原始条目里没写的字段，读回来必须还是空的。

    `load_config()` 给的是**生效值**（空 base_url 已落回默认地址）。
    写路径若拿它去写，等于把「用户没配过这个地址」这个事实抹掉 ——
    下次读回来，那个地址就成了「用户显式配的」，界面上「内置默认」的小标
    凭空消失。用户没做任何操作，提示却没了，而且再也回不来。
    """
    _seed_placeholder_model(tmp_path)
    raw, active = load_raw_models(tmp_path)
    assert raw[0]["base_url"] == "", "原始条目被填实了"
    assert raw[0]["model"] == ""
    assert raw[0]["api_key"] == ""
    # 对照：生效值是落回默认的 —— 两者必须能区分开，否则这层拆分白做。
    assert load_config(tmp_path).llm.base_url == DEFAULT_MODEL["base_url"]
    assert active == DEFAULT_MODEL["id"]


def test_saving_a_model_does_not_promote_defaults_to_explicit(tmp_path):
    """加了一条模型之后，**没动过的那条**仍应报「内置默认」。

    这是上一条的端到端版本，也是真实用户路径：他点「添加模型」填了 DeepSeek，
    当前模型还是那条没配过的默认条目 —— 界面上它的小标不能因为「保存过一次」
    就消失。
    """
    c = _client(tmp_path)
    r = c.post("/api/models", json={"id": "", "name": "DeepSeek",
                                    "base_url": "https://api.deepseek.com/v1",
                                    "model": "deepseek-chat", "api_key": SECRET})
    assert r.status_code == 200, r.text
    body = c.get("/api/config").json()
    assert len(body["models"]) == 2
    assert body["active_model"] == DEFAULT_MODEL["id"], "添加不该顺手切过去"
    assert "base_url" in body["llm_defaulted"], "保存过 ≠ 用户配过"
    assert "model" in body["llm_defaulted"]


def test_public_models_reports_defaulted_per_entry(tmp_path):
    """每条模型各自报「哪些字段还是内置默认」—— 不能只报当前那条。

    列表里同时存在「配好的」和「迁移出来的空壳」时，界面必须能分别标注；
    只报当前那条的话，非当前的行看起来一模一样（base_url 都显示着默认地址），
    用户分不清哪条是他配的。
    """
    c = _client(tmp_path)
    c.post("/api/models", json={"id": "", "name": "DeepSeek",
                                "base_url": "https://api.deepseek.com/v1",
                                "model": "deepseek-chat"})
    items = {m["id"]: m for m in c.get("/api/config").json()["models"]}
    assert sorted(items[DEFAULT_MODEL["id"]]["defaulted"]) == ["base_url", "model"]
    assert items["m1"]["defaulted"] == [], "配全了的那条不该还标着默认"
    # 与当前模型那条口径一致：当前还是 m-default，两个信号必须说同一件事。
    assert sorted(c.get("/api/config").json()["llm_defaulted"]
                  ) == ["base_url", "max_tokens", "model", "retries",
                        "temperature", "timeout"]


def test_activating_a_configured_model_clears_its_defaulted_fields(tmp_path):
    """切到一条**配全了**的模型 → base_url / model 不再是内置默认。"""
    c = _client(tmp_path)
    c.post("/api/models", json={"id": "", "base_url": "https://api.deepseek.com/v1",
                                "model": "deepseek-chat", "api_key": SECRET})
    assert c.post("/api/models/activate", json={"id": "m1"}).status_code == 200
    body = c.get("/api/config").json()
    assert body["model"] == "deepseek-chat"
    assert "base_url" not in body["llm_defaulted"]
    assert "model" not in body["llm_defaulted"]
    # 数值项住在 llm 段，跟切模型无关 —— 不能因为换了模型就整体放行。
    assert "temperature" in body["llm_defaulted"]


def test_save_models_removes_stale_connection_keys_from_llm(tmp_path):
    """写完列表要把 llm 段里的 base_url / api_key / model 清掉。

    留着的话文件里会出现**同一份连接信息两处表示**，而其中一份是被忽略的 ——
    用户改它却不生效，属于看不见的失效。清掉之后「模型在哪」只有一个答案。
    """
    _write_legacy(tmp_path, base_url="https://old.example/v1",
                  api_key=SECRET, model="old-model")
    raw, active = load_raw_models(tmp_path)
    raw[0]["model"] = "new-model"
    save_models(tmp_path, raw, active)

    text = (tmp_path / "config.yaml").read_text(encoding="utf-8")
    import yaml
    data = yaml.safe_load(text)
    assert data["llm"].get("base_url") is None
    assert data["llm"].get("model") is None
    assert data["llm"].get("api_key") is None
    # 但值本身没丢：它现在住在模型条目里。
    assert load_config(tmp_path).llm.model == "new-model"


def test_config_post_writes_into_active_model_entry(tmp_path):
    """老的 `POST /api/config` 写连接信息时，改的是**当前模型条目**。

    这条守的是「同一个信息不存两份」：旧入口保留下来（curl / 老渲染层在用），
    但它必须落到同一份数据上。
    """
    c = _client(tmp_path)
    assert c.post("/api/config", json={"base_url": "https://one.example/v1",
                                       "model": "one"}).status_code == 200
    body = c.get("/api/config").json()
    assert body["models"][0]["base_url"] == "https://one.example/v1"
    assert body["models"][0]["model"] == "one"
    assert "base_url" not in body["llm_defaulted"], "写过了还说没配"

    raw, _ = load_raw_models(tmp_path)
    assert len(raw) == 1, "旧入口不该另起一条"
    assert raw[0]["base_url"] == "https://one.example/v1"


def test_reset_config_dispatches_to_the_right_place(tmp_path):
    """「恢复默认」要按字段分派：数值项在 llm 段，连接信息在模型条目。

    一个循环写完会漏掉一类，表现为「点了恢复默认没反应」—— 静默无效。
    """
    c = _client(tmp_path)
    c.post("/api/config", json={"base_url": "https://one.example/v1", "model": "one",
                                "retries": 7})
    assert c.post("/api/config/reset",
                  json={"fields": ["base_url", "model", "retries"]}).status_code == 200
    body = c.get("/api/config").json()
    assert body["base_url"] == DEFAULT_MODEL["base_url"]
    assert body["model"] == DEFAULT_MODEL["model"]
    assert body["retries"] == DEFAULT_CONFIG["llm"]["retries"]
    assert {"base_url", "model", "retries"} <= set(body["llm_defaulted"])


# ── 3. 密钥不出门 ────────────────────────────────────────────

def test_public_models_strips_api_key(tmp_path):
    """`public_models()` 是**唯一**的剥离点，必须只给布尔。"""
    _write_legacy(tmp_path, base_url="https://old.example/v1",
                  api_key=SECRET, model="old-model")
    items = public_models(load_config(tmp_path))
    assert items[0]["api_key_set"] is True
    assert "api_key" not in items[0], "字段名都不该出现，免得日后有人顺手填上"
    assert SECRET not in json.dumps(items)


@pytest.mark.parametrize("path", ["/api/config", "/api/meta"])
def test_api_never_sends_api_key_plaintext(tmp_path, path):
    """接口响应里不能有明文密钥 —— 渲染层是网页，进去就等于进了 DOM。

    ⚠ 断言写成「整个响应体里搜不到这个串」，而不是「某个字段为空」：
    后者只挡住当前这一处，日后谁多下发一个字段就绕过去了。
    """
    _write_legacy(tmp_path, base_url="https://old.example/v1",
                  api_key=SECRET, model="old-model")
    text = json.dumps(_client(tmp_path).get(path).json(), ensure_ascii=False)
    assert SECRET not in text, f"{path} 把密钥下发到渲染层了"
    assert '"api_key"' not in text


def test_api_models_carry_the_active_flag(tmp_path):
    """当前是哪条要由后端说，不能让界面自己拿 active_model 去比 ——
    两处各算一次，迟早有一处忘了更新。"""
    c = _client(tmp_path)
    c.post("/api/models", json={"id": "", "base_url": "https://b/v1", "model": "b"})
    c.post("/api/models/activate", json={"id": "m1"})
    items = {m["id"]: m for m in c.get("/api/config").json()["models"]}
    assert items["m1"]["active"] is True
    assert items[DEFAULT_MODEL["id"]]["active"] is False


# ── 4. 增删改切换的边界 ──────────────────────────────────────

def test_upsert_rejects_blank_model_and_malformed_url(tmp_path):
    """模型名必填；请求地址**填了就得合法**。

    「api.openai.com/v1」少了协议头是最常见的漏填；不拦的话它一路存到生成时
    才在 urllib 里炸，报错完全指不到根因。
    """
    c = _client(tmp_path)
    cases = [
        ({"id": "", "base_url": "https://a/v1"}, "模型 ID"),
        ({"id": "", "base_url": "api.openai.com/v1", "model": "a"}, "http"),
        ({"id": "", "base_url": "ftp://a/v1", "model": "a"}, "http"),
    ]
    for payload, hint in cases:
        r = c.post("/api/models", json=payload)
        assert r.status_code == 400, f"{payload} 应被拒，实际 {r.status_code}"
        assert hint in r.json()["detail"]
    assert len(c.get("/api/config").json()["models"]) == 1, "被拒的请求不该留下痕迹"


def test_upsert_requires_url_when_creating(tmp_path):
    """新增时请求地址**必填**（2026-09-17）；编辑时留空仍是「保持不变」。

    新增留空的旧行为是「落回内置默认地址」—— 不再有那层语义可以借：
    用户加一条 DeepSeek 模型却指向智谱，报错要到生成时才出现，
    而且完全指不到根因（「未配置与已配置长得一样」的老坑）。
    编辑时那个框本来就是空的（还没配过地址），用户只改展示名不该被拒 ——
    他压根没动过地址，留空表示「保持不变」。
    """
    c = _client(tmp_path)
    # 新增：地址留空 → 拒，并说清要填什么
    r = c.post("/api/models", json={"id": "", "name": "占位", "model": "a"})
    assert r.status_code == 400, r.text
    assert "请求地址" in r.json()["detail"]
    assert len(c.get("/api/config").json()["models"]) == 1, "被拒的请求不该留下痕迹"

    # 新增：填了地址 → 通过
    r = c.post("/api/models", json={"id": "", "name": "占位",
                                    "base_url": "https://a/v1", "model": "a"})
    assert r.status_code == 200, r.text

    # 编辑：只改展示名、地址留空 → 已存的那条地址不能被动过
    r = c.post("/api/models", json={"id": "m1", "name": "改个名", "model": "a"})
    assert r.status_code == 200, r.text
    m = next(x for x in c.get("/api/config").json()["models"] if x["id"] == "m1")
    assert m["base_url"] == "https://a/v1", "留空把已存的地址清掉了"
    assert m["name"] == "改个名"


def test_editing_with_blank_key_keeps_the_stored_one(tmp_path):
    """编辑时密钥框是空的 → 「保持不变」，不是「清空」。

    清空的后果是用户只改了个展示名，模型就再也连不上了，而界面上一切正常。
    """
    c = _client(tmp_path)
    c.post("/api/models", json={"id": "", "name": "A",
                                "base_url": "https://a/v1", "model": "a",
                                "api_key": SECRET})
    r = c.post("/api/models", json={"id": "m1", "name": "A 改名",
                                    "base_url": "https://a/v1", "model": "a"})
    assert r.status_code == 200, r.text
    raw, _ = load_raw_models(tmp_path)
    assert next(x for x in raw if x["id"] == "m1")["api_key"] == SECRET
    assert c.get("/api/config").json()["models"][1]["name"] == "A 改名"


def test_new_ids_do_not_collide_with_existing(tmp_path):
    """自动编号要跳过已被占用的 —— 撞 id 会让两条模型互相覆盖。"""
    c = _client(tmp_path)
    for _ in range(3):
        c.post("/api/models", json={"id": "", "base_url": "https://a/v1", "model": "a"})
    ids = [m["id"] for m in c.get("/api/config").json()["models"]]
    assert len(ids) == len(set(ids)) == 4, ids


def test_can_delete_the_last_model(tmp_path):
    """**删光也可以**（2026-09-17 产品决定）。

    原来拦着「至少要保留一个模型」—— 那是「总有一条内置默认」时代的规则
    （删空了生成时取不到连接信息，而界面还会显示「已配置 Key」）。
    现在模型是用户自己加的（不再有预置条目），删光就是「还没配」：
    界面显示空列表引导，生成前被 `_require_model` 明确拦住。
    用户原话：「没有内置默认的模型的，需要用户自己添加，添加完还需要支持删除」。
    """
    c = _client(tmp_path)
    r = c.post("/api/models/delete", json={"id": DEFAULT_MODEL["id"]})
    assert r.status_code == 200, r.text
    body = c.get("/api/config").json()
    assert body["models"] == [], "最后一条也该能删掉"
    assert body["active_model"] == "", "删光后不能留悬空的 active_model"


def test_can_deactivate_current_model(tmp_path):
    """**开关可以关掉**（2026-09-17）。

    原来 `activate_model` 对空 id 会 404，前端 `activateModel` 更是直接
    `return`（注释写「已经是当前，别白写一次文件」）—— 于是开关**关不掉**：
    点当前启用的那条没有任何反应（用户报「模型开启时无法关闭」）。
    「启用」是单选语义，但「都不启用」也是合法状态（想先停用、改完配置再启用）。
    """
    c = _client(tmp_path)                 # seed 了一条空壳 m-default（当前启用）
    assert c.get("/api/config").json()["active_model"] == DEFAULT_MODEL["id"]

    r = c.post("/api/models/activate", json={"id": ""})
    assert r.status_code == 200, r.text
    body = c.get("/api/config").json()
    assert body["active_model"] == "", "关不掉：active_model 被退回了第一条"
    assert all(not m["active"] for m in body["models"]), "还有条目亮着"
    # ⚠ 关键：**存回文件再读**也要是空 —— 不能只在内存里空
    raw, active = load_raw_models(tmp_path)
    assert active == "", f"空 active 没落盘（读回 {active!r}）"


def test_deactivate_survives_reread(tmp_path):
    """关掉之后重新 load 仍是「都不启用」—— 不能退回第一条。

    `_parse_models` 原来对「不在 seen 里的 active」一律退回第一条，
    空串也会被退 —— 于是开关「关了又跳回来」。
    修法是区分「键**不存在**」（老文件 / 迁移产物 → 用第一条）与
    「键存在但空串」（用户显式取消 → 原样保留）。
    """
    _seed_models(tmp_path, {"id": "a", "base_url": "https://a/v1", "model": "a"},
                 {"id": "b", "base_url": "https://b/v1", "model": "b"}, active="a")
    raw, active = load_raw_models(tmp_path)
    assert active == "a"
    save_models(tmp_path, raw, "")
    cfg = load_config(tmp_path)
    assert cfg.active_model == "", "空 active 被退回了第一条（关了又跳回来）"
    # 生效配置跟着变成「空模型」—— 生成会被 _require_model 拦住
    assert cfg.llm.api_key == "" and cfg.llm.base_url == ""


def test_generate_without_active_model_is_refused(tmp_path):
    """有模型但**都没启用** → 生成被拦，且理由与「没配 Key」区分开。

    三种"不能生成"必须各自说清：一条模型都没加 / 加了但没启用 / 启用了没填 Key。
    同一句话会把用户指向错的地方。
    """
    c = _client(tmp_path)
    c.post("/api/models/activate", json={"id": ""})
    r = c.post("/api/generate", json={"topic": "家用电梯怎么挑？"})
    assert r.status_code == 400, r.text
    assert "没有启用任何模型" in r.json()["detail"], r.json()


def test_deleting_active_model_falls_back_to_a_remaining_one(tmp_path):
    """删掉当前模型 → 自动换成剩下的一条，不留悬空引用。

    悬空的后果：`active_model` 指向一条不存在的记录，生成时取不到任何连接信息。
    """
    c = _client(tmp_path)
    c.post("/api/models", json={"id": "", "base_url": "https://b/v1", "model": "b"})
    c.post("/api/models/activate", json={"id": "m1"})
    r = c.post("/api/models/delete", json={"id": "m1"})
    assert r.status_code == 200, r.text
    assert r.json()["active_model"] == DEFAULT_MODEL["id"]
    body = c.get("/api/config").json()
    assert body["active_model"] == DEFAULT_MODEL["id"]
    assert [m["id"] for m in body["models"]] == [DEFAULT_MODEL["id"]]


def test_deleting_a_non_active_model_keeps_the_current_one(tmp_path):
    """删的不是当前那条 → 当前模型不能被动过。"""
    c = _client(tmp_path)
    c.post("/api/models", json={"id": "", "base_url": "https://b/v1", "model": "b"})
    c.post("/api/models/activate", json={"id": "m1"})
    c.post("/api/models/delete", json={"id": DEFAULT_MODEL["id"]})
    assert c.get("/api/config").json()["active_model"] == "m1"


def test_activate_unknown_id_is_404(tmp_path):
    """切到不存在的 id 要报错，不能静默写进文件。"""
    c = _client(tmp_path)
    r = c.post("/api/models/activate", json={"id": "nope"})
    assert r.status_code == 404
    assert c.get("/api/config").json()["active_model"] == DEFAULT_MODEL["id"]


def test_editing_unknown_id_is_404(tmp_path):
    """`id` 非空表示「改那一条」—— 那条不存在时不能顺手变成新增。"""
    c = _client(tmp_path)
    r = c.post("/api/models", json={"id": "nope", "base_url": "https://a/v1",
                                    "model": "a"})
    assert r.status_code == 404
    assert len(c.get("/api/config").json()["models"]) == 1


# ── 5. 环境变量仍作用于当前模型 ──────────────────────────────

def test_env_key_override_beats_the_stored_one(tmp_path, monkeypatch):
    """环境变量优先级最高（密钥不落盘的用法）—— 且要作用在**当前**那条上。

    改成列表之后最容易漏的就是这一层：覆盖逻辑原来读 `llm.api_key`，
    现在那个字段是「当前模型展开出来的」，顺序错一步就会盖到别的模型上。
    """
    c = _client(tmp_path)
    c.post("/api/models", json={"id": "", "base_url": "https://a/v1", "model": "a",
                                "api_key": "sk-from-file"})
    c.post("/api/models/activate", json={"id": "m1"})
    monkeypatch.setenv("TALKSCRIPT_API_KEY", "sk-from-env")
    monkeypatch.setenv("TALKSCRIPT_MODEL", "env-model")

    cfg = load_config(tmp_path)
    assert cfg.llm.api_key == "sk-from-env"
    assert cfg.llm.model == "env-model"
    assert cfg.llm.base_url == "https://a/v1", "没被覆盖的字段不该受影响"
    # 文件里的原值不能被环境变量改写 —— 覆盖是「本次生效」，不是「落盘」。
    raw, _ = load_raw_models(tmp_path)
    assert next(x for x in raw if x["id"] == "m1")["api_key"] == "sk-from-file"


def test_legacy_config_write_needs_an_active_model(tmp_path):
    """**关掉开关后旧写入口不能 500**（2026-09-19 修）。

    `POST /api/config` 写 base_url/model/api_key 时落点是「当前那条模型」。
    开关关掉后 `load_raw_models` 返回 (非空列表, "")，而代码只挡了
    「列表为空」那一种，`next(x for x in raw if x["id"] == active)`
    在这里抛 StopIteration —— TestClient 下冒成 500。
    修法不是硬猜一条写进去（写进哪条是用户的决定），而是给可行动的 400。
    """
    _seed_models(tmp_path, {"id": "a", "base_url": "https://a/v1", "model": "a"},
                 {"id": "b", "base_url": "https://b/v1", "model": "b"}, active="a")
    c = _client(tmp_path, seed=False)
    assert c.post("/api/models/activate", json={"id": ""}).status_code == 200

    r = c.post("/api/config", json={"base_url": "https://x/v1", "model": "m-x"})
    assert r.status_code == 400, f"应为可行动的 400，实际 {r.status_code}：{r.text}"
    assert "模型" in r.json()["detail"], r.text
    # 报错了也不能把任何一条模型的连接信息改掉
    raw, _ = load_raw_models(tmp_path)
    assert [x["base_url"] for x in raw] == ["https://a/v1", "https://b/v1"], r.text


def test_config_reset_link_fields_without_active_is_noop(tmp_path):
    """**重置连接字段同理**：没启用任何一条时静默 no-op，不是 500。

    数字字段（timeout 等）住在配置本身，照常重置；连接字段没有落点，
    跳过即可 —— 原来 `if raw:` 挡住的是"一条都没有"，挡不住"有但都没启用"。
    """
    _seed_models(tmp_path, {"id": "a", "base_url": "https://a/v1", "model": "a"},
                 active="a")
    c = _client(tmp_path, seed=False)
    assert c.post("/api/models/activate", json={"id": ""}).status_code == 200

    r = c.post("/api/config/reset", json={"fields": ["base_url", "timeout"]})
    assert r.status_code == 200, f"应为 200 no-op，实际 {r.status_code}：{r.text}"
    raw, _ = load_raw_models(tmp_path)
    assert raw[0]["base_url"] == "https://a/v1", "没启用时不该改动任何一条模型"
    # 数字字段住在配置本身，照常重置 —— 不能因为连接字段跳过就连它一起漏掉
    assert load_config(tmp_path).llm.timeout == 180, "timeout 没被重置回默认"


def main() -> int:                                        # pragma: no cover
    """独立跑一遍迁移那组（不依赖 pytest fixture）。"""
    tmp = Path(tempfile.mkdtemp(prefix="talkscript-ml-"))
    try:
        _write_legacy(tmp, base_url="https://old.example/v1",
                      api_key=SECRET, model="old-model")
        cfg = load_config(tmp)
        assert len(cfg.models) == 1 and cfg.llm.model == "old-model"
        assert SECRET not in json.dumps(public_models(cfg))
        print("  ✅ 旧配置迁移成一条模型，且密钥不进列表")
        return 0
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":                                # pragma: no cover
    sys.exit(main())
