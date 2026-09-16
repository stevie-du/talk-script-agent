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


def _client(root: Path):
    try:
        from fastapi.testclient import TestClient
    except ImportError:                                   # pragma: no cover
        pytest.skip("未安装 fastapi/httpx")
    from app.server import create_app
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


def test_fresh_install_gets_one_placeholder_model(tmp_path):
    """全新安装也要有一条 —— 空列表会让「至少保留一个模型」这条规则自相矛盾。"""
    cfg = load_config(tmp_path)
    assert [m.id for m in cfg.models] == [DEFAULT_MODEL["id"]]
    assert cfg.active_model == DEFAULT_MODEL["id"]


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


def test_upsert_allows_blank_url_meaning_keep_or_default(tmp_path):
    """地址**留空**是另一回事：不是「填错」，是「没填」。

    编辑一条还没配过地址的模型时，弹窗里那个框本来就是空的 —— 用户只改了
    展示名，不该因为「地址不能为空」被拒（他压根没动过地址）。
    新增时留空则沿用内置默认地址，与「存了空值」的生效结果完全一致。
    """
    c = _client(tmp_path)
    # 新增：留空 → 落回内置默认地址，且仍报「内置默认」（因为它确实没配过）
    r = c.post("/api/models", json={"id": "", "name": "占位", "model": "a"})
    assert r.status_code == 200, r.text
    m = next(x for x in c.get("/api/config").json()["models"] if x["id"] == "m1")
    assert m["base_url"] == DEFAULT_MODEL["base_url"]
    assert "base_url" in m["defaulted"]

    # 编辑：只改展示名，地址留空 → 已存的那条地址不能被动过
    c.post("/api/models", json={"id": "m1", "base_url": "https://a/v1", "model": "a"})
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


def test_cannot_delete_the_last_model(tmp_path):
    """删到一条不剩 = 生成时没有任何连接信息可用，而界面还会显示「已配置 Key」。"""
    c = _client(tmp_path)
    r = c.post("/api/models/delete", json={"id": DEFAULT_MODEL["id"]})
    assert r.status_code == 400
    assert len(c.get("/api/config").json()["models"]) == 1


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
