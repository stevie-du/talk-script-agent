# -*- coding: utf-8 -*-
"""导入 API 的端到端回归（`/api/packs/import`、`DELETE /api/packs/{name}`）。

核心逻辑在 `app/packimport.py`（由 test_pack_import.py 覆盖）；这里钉的是
**接线**：zip 字节进得去、结果出得来、失败翻译成人话、`/api/meta` 的包列表
带来源徽标。接线断不全会静默 —— 前端点了导入没反应，比导入失败更糟。
"""
from __future__ import annotations

import io
import os
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests.test_pack_import import (MIN_PACK_YAML, MIN_SKILL_YAML,  # noqa: E402
                                    _min_pack, _zip)

TOKEN = "import-token"


def _client(tmp_path: Path):
    try:
        from fastapi.testclient import TestClient
    except ImportError:                                   # pragma: no cover
        pytest.skip("未安装 fastapi/httpx")
    from app.server import create_app
    c = TestClient(create_app(tmp_path, token=TOKEN, packs_dir=tmp_path / "packs"),
                   base_url="http://127.0.0.1:8799",
                   raise_server_exceptions=False)
    c.headers.update({"X-TalkScript-Token": TOKEN})
    return c


def test_import_endpoint_roundtrip(tmp_path):
    c = _client(tmp_path)
    r = c.post("/api/packs/import",
               files={"file": ("pack.zip", _zip(_min_pack()), "application/zip")})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] and body["name"] == "testpack"
    # 落盘 + 列表可见 + 来源徽标
    assert (tmp_path / "packs" / "testpack" / "pack.yaml").exists()
    packs = {p["name"]: p for p in c.get("/api/meta").json()["packs"]}
    assert packs["testpack"]["imported"] is True


def test_import_endpoint_rejects_bad_zip(tmp_path):
    c = _client(tmp_path)
    r = c.post("/api/packs/import",
               files={"file": ("x.zip", b"garbage", "application/zip")})
    assert r.status_code == 400
    assert "zip" in r.json()["detail"]


def test_import_endpoint_rejects_zip_slip(tmp_path):
    c = _client(tmp_path)
    z = _min_pack()
    z["../evil.md"] = "x"
    r = c.post("/api/packs/import",
               files={"file": ("pack.zip", _zip(z), "application/zip")})
    assert r.status_code == 400
    assert "穿越" in r.json()["detail"]


def test_import_endpoint_rejects_oversized_zip(tmp_path, monkeypatch):
    """`/api/packs/import` 收不可信内容：总量上限必须在**读的过程中**判。

    整读进内存再交给 `import_pack` 判上限（改前的形状）意味着
    一个 2GB 的上传会先把 2GB 吃进引擎自己那份拷贝，然后才被拒。

    ⚠ 样本必须是「**每个条目都合法、只有总量越界**」的形态。
      第一版塞的是单个 2MB 条目 —— 它必然先撞 `packimport.MAX_FILE_BYTES`
      （1MB），那里被拒的文案是「zip 里 pack/big.md 太大（2048KB，上限
      1024KB）」，**同样含"太大"二字**。实测把端点侧 MAX_ZIP_BYTES 抬到
      20MB（等价于把分块读那道闸整个架空），端点回 400 + 那句文案，
      两条断言照样为真 —— 这条守卫对被测改动**完全空转**。
      现在每个成员 150KB（< 1MB），4 个加起来才越界，
      `MAX_FILE_BYTES` / `MAX_ENTRIES` 都拦不住它。
    ⚠ 判据要钉到**端点自己那句文案**上（含 `MAX_ZIP_BYTES` 那个数），
      才分得开是哪一道闸做的。
    """
    import app.server as S
    cap = 300 * 1024
    monkeypatch.setattr(S, "MAX_ZIP_BYTES", cap)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("pack/pack.yaml", MIN_PACK_YAML)
        zf.writestr("pack/skill.yaml", MIN_SKILL_YAML)
        for i in range(4):                      # 每个 150KB，都小于单条目上限
            zf.writestr(f"pack/notes{i}.md", os.urandom(150 * 1024))
    payload = buf.getvalue()
    assert cap < len(payload) < 4 * 150 * 1024 + 4096, \
        f"样本形状不对，这条测试是空的：{len(payload)} 字节"
    c = _client(tmp_path)
    r = c.post("/api/packs/import",
               files={"file": ("pack.zip", payload, "application/zip")})
    assert r.status_code == 400, r.text
    detail = r.json()["detail"]
    # ⚠ 判据钉的是端点**自己那句**的措辞。两条路的文案都含"太大"：
    #   端点侧「zip 太大（上限 NMB）」 vs packimport 侧「zip 里 X 太大（…KB）」。
    #   分不开就等于没钉 —— 实测把端点侧上限抬到 20MB（架空分块读那道闸）
    #   之后，这两条断言照样全绿。
    assert detail.startswith("zip 太大"), f"不是端点侧那道闸拒的：{detail}"


def test_import_endpoint_accepts_a_normal_sized_zip(tmp_path, monkeypatch):
    """上一条的对照：上限调得比这份小包还大时，同一个包要能进得来。

    没有这条，上一条可以靠「把上限调到 0」这类改坏来恒红 ——
    守卫红得毫无意义，还会把人引去改本来正确的代码。
    """
    import app.server as S
    monkeypatch.setattr(S, "MAX_ZIP_BYTES", 50 * 1024 * 1024)
    c = _client(tmp_path)
    r = c.post("/api/packs/import",
               files={"file": ("pack.zip", _zip(_min_pack()), "application/zip")})
    assert r.status_code == 200, r.text


def test_delete_endpoint(tmp_path):
    c = _client(tmp_path)
    c.post("/api/packs/import",
           files={"file": ("pack.zip", _zip(_min_pack()), "application/zip")})
    r = c.request("DELETE", "/api/packs/testpack")
    assert r.status_code == 200, r.text
    assert not (tmp_path / "packs" / "testpack").exists()


def test_delete_builtin_rejected(tmp_path):
    """内置包（无 .imported.json）不给删 —— 播种会把它放回来。"""
    c = _client(tmp_path)
    d = tmp_path / "packs" / "elevator"
    d.mkdir(parents=True)
    (d / "pack.yaml").write_text(MIN_PACK_YAML, encoding="utf-8")
    r = c.request("DELETE", "/api/packs/elevator")
    assert r.status_code == 400
    assert "内置" in r.json()["detail"]
    assert d.exists()
