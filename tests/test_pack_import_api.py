# -*- coding: utf-8 -*-
"""导入 API 的端到端回归（`/api/packs/import`、`DELETE /api/packs/{name}`）。

核心逻辑在 `app/packimport.py`（由 test_pack_import.py 覆盖）；这里钉的是
**接线**：zip 字节进得去、结果出得来、失败翻译成人话、`/api/meta` 的包列表
带来源徽标。接线断不全会静默 —— 前端点了导入没反应，比导入失败更糟。
"""
from __future__ import annotations

import io
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
