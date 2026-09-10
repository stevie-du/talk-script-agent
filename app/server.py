# -*- coding: utf-8 -*-
"""引擎 HTTP 服务（本地回环）

独立运行:  python -m app.server --port 8765
Electron:  主进程 spawn 本模块并轮询 /api/health
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import yaml
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

from .config import load_config, save_config
from .knowledge import Pack, list_packs
from .llm import LLMClient
from .packgen import create_pack
from .pipeline import Pipeline
from .schemas import (ConfirmRequest, GenerateRequest, PackCreateRequest,
                      RewriteSegmentRequest)

VERSION = "0.1.0"

# 路径参数白名单：作业 id 形如 20260910-010929-ddb666；
# 行业包名为 slug（允许中英文、数字、下划线与连字符），两者都禁止 . / 等穿越字符。
_JID_RE = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{6}$")
_NAME_RE = re.compile(r"^[\w\u4e00-\u9fff-]+$")


def _safe_jid(jid: str) -> str:
    """校验作业 id：既防路径穿越，也防 glob 通配（如 jid=* 命中任意记录）。"""
    if not _JID_RE.match(jid or ""):
        raise HTTPException(400, "记录标识不合法")
    return jid


def _safe_name(name: str) -> str:
    """校验行业包名：禁止 .. / \\ 等穿越字符。"""
    if not _NAME_RE.match(name or "") or ".." in name:
        raise HTTPException(400, "行业包名称不合法")
    return name


def create_app(root: Path) -> FastAPI:
    cfg = load_config(root)
    pipeline = Pipeline(root, cfg)

    app = FastAPI(title="TalkScript Engine", version=VERSION)
    app.add_middleware(
        CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

    @app.get("/")
    def index():
        return FileResponse(_renderer() / "index.html")

    @app.get("/styles.css")
    def styles():
        return FileResponse(_renderer() / "styles.css", media_type="text/css")

    @app.get("/app.js")
    def appjs():
        return FileResponse(_renderer() / "app.js", media_type="text/javascript")

    def _renderer() -> Path:
        # Electron 用 desktop/renderer 同一套文件；浏览器调试直接走引擎同源
        return root / "desktop" / "renderer"

    @app.get("/api/health")
    def health():
        return {"ok": True, "version": VERSION, "mock": cfg.mock}

    @app.get("/api/meta")
    def meta():
        packs = list_packs(root)
        return {
            "packs": [p.model_dump() for p in packs],
            "default_pack": cfg.default_pack,
            "model": cfg.llm.model,
            "has_api_key": bool(cfg.llm.api_key),
            "mock": cfg.mock,
        }

    @app.get("/api/packs/{name}")
    def get_pack(name: str):
        _safe_name(name)
        try:
            pack = Pack(root, name)
        except Exception as e:
            raise HTTPException(404, str(e))
        base = root / "packs" / name
        files = []
        for f in sorted(base.rglob("*")):
            if f.is_file() and "__pycache__" not in str(f) and f.stat().st_size > 0:
                files.append({"rel": f.relative_to(base).as_posix(), "size": f.stat().st_size})
        checklist = None
        cl = base / "校对清单.md"
        if cl.exists():
            checklist = cl.read_text(encoding="utf-8")
        has_skill = (base / "skill.yaml").exists()
        return {**pack.data, "files": files, "checklist": checklist, "has_skill": has_skill}

    # ── 脚本生成 ────────────────────────────────────────────
    @app.post("/api/generate")
    def generate(req: GenerateRequest):
        fresh = load_config(root)
        if not (fresh.llm.api_key or fresh.mock):
            raise HTTPException(400, "未配置模型 API Key，请在设置中填写后重试")
        return {"job_id": pipeline.start_generate(req)}

    @app.get("/api/jobs/{jid}")
    def job_snapshot(jid: str):
        job = pipeline.jobs.get(jid)
        if not job:
            raise HTTPException(404, "作业不存在")
        return job.snapshot()

    @app.post("/api/jobs/{jid}/confirm")
    def job_confirm(jid: str, req: ConfirmRequest):
        try:
            return pipeline.confirm(jid, req)
        except KeyError:
            raise HTTPException(404, "作业不存在")
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.post("/api/jobs/{jid}/rewrite_segment")
    def job_rewrite(jid: str, req: RewriteSegmentRequest):
        try:
            return pipeline.rewrite_segment(jid, req)
        except KeyError:
            raise HTTPException(404, "作业不存在")
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.post("/api/jobs/{jid}/cancel")
    def job_cancel(jid: str):
        try:
            return pipeline.cancel(jid)
        except KeyError:
            raise HTTPException(404, "作业不存在")
        except ValueError as e:
            raise HTTPException(400, str(e))

    # ── 行业包 ──────────────────────────────────────────────
    @app.post("/api/packs/create")
    def packs_create(req: PackCreateRequest):
        if cfg.mock:
            # mock 模式同样走生成流程（返回夹具数据），验证流程用
            pass
        if not (cfg.llm.api_key or cfg.mock):
            raise HTTPException(400, "未配置模型 API Key，请先在设置中填写")
        try:
            return create_pack(root, pipeline.build_llm(), req.industry, req.description)
        except FileExistsError as e:
            raise HTTPException(409, str(e))

    @app.post("/api/packs/{name}/export-skill")
    def packs_export(name: str):
        from .export_skill import export_agent_skill
        try:
            return export_agent_skill(root, name)
        except Exception as e:
            raise HTTPException(500, f"导出失败：{e}")

    @app.post("/api/packs/{name}/undraft")
    def packs_undraft(name: str):
        """人工校对完成后，把 draft 改为 false"""
        _safe_name(name)
        p = root / "packs" / name / "pack.yaml"
        if not p.exists():
            raise HTTPException(404, "行业包不存在")
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        data["draft"] = False
        p.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
        return {"ok": True, "name": name, "draft": False}

    # ── 历史 ────────────────────────────────────────────────
    @app.get("/api/history")
    def history():
        out = []
        gen = root / "generated"
        if gen.exists():
            for f in sorted(gen.glob("*/*/result.json"), reverse=True):
                try:
                    r = json.loads(f.read_text(encoding="utf-8"))
                    out.append({
                        "id": r["id"], "created_at": r["created_at"], "pack": r["pack"],
                        "topic": r["params"].get("topic", ""),
                        "duration": r["params"].get("duration"),
                        "chars": r["check"].get("chars_total"),
                        "passed": r["check"].get("passed"),
                    })
                except Exception:
                    continue
        return out[:100]

    @app.get("/api/history/{jid}")
    def history_item(jid: str):
        jid = _safe_jid(jid)
        for f in root.glob(f"generated/*/{jid}/result.json"):
            return json.loads(f.read_text(encoding="utf-8"))
        raise HTTPException(404, "记录不存在")

    @app.delete("/api/history/{jid}")
    def history_delete(jid: str):
        jid = _safe_jid(jid)
        for f in root.glob(f"generated/*/{jid}"):
            shutil.rmtree(f, ignore_errors=True)
            return {"ok": True, "id": jid}
        raise HTTPException(404, "记录不存在")

    @app.post("/api/history/{jid}/reveal")
    def history_reveal(jid: str):
        """在系统文件管理器里打开该条记录的产物目录（脚本.md / result.json 所在处）。"""
        jid = _safe_jid(jid)
        for f in root.glob(f"generated/*/{jid}"):
            try:
                _reveal_in_explorer(f)
            except Exception as e:  # noqa: BLE001
                raise HTTPException(500, f"打开失败：{e}")
            return {"ok": True, "path": str(f.resolve())}
        raise HTTPException(404, "记录不存在")

    # ── 设置 ────────────────────────────────────────────────
    @app.get("/api/config")
    def get_config():
        cfg = load_config(root)
        return {"base_url": cfg.llm.base_url, "model": cfg.llm.model,
                "api_key_set": bool(cfg.llm.api_key), "temperature": cfg.llm.temperature,
                "mock": cfg.mock}

    class ConfigIn(BaseModel):
        base_url: str = ""
        api_key: str = ""
        model: str = ""
        temperature: float | None = None

    @app.post("/api/config")
    def set_config(body: ConfigIn):
        # 空 base_url / model 不覆盖（防清空）；api_key 空串=保持不变（save_config 合并语义）
        updates = {k: v for k, v in body.model_dump().items() if v not in ("", None)}
        save_config(root, updates)
        return {"ok": True}

    @app.post("/api/config/test")
    def test_config(body: ConfigIn | None = None):
        """用当前配置发一个最小请求，验证 Key / 地址 / 模型名是否可用。

        body 可选：传入界面上尚未保存的值做临时覆盖（Key 留空表示沿用已保存的），
        这样用户填完就能测，不必先点保存。
        """
        fresh = load_config(root)
        if body:
            if body.base_url:
                fresh.llm.base_url = str(body.base_url).rstrip("/")
            if body.api_key:
                fresh.llm.api_key = str(body.api_key)
            if body.model:
                fresh.llm.model = str(body.model)
        if not (fresh.llm.api_key or fresh.mock):
            raise HTTPException(400, "未配置模型 API Key，请先填写")
        ok, detail = LLMClient(fresh.llm, mock=fresh.mock).ping()
        return {"ok": ok, "detail": detail, "model": fresh.llm.model,
                "base_url": fresh.llm.base_url}

    return app


def _reveal_in_explorer(target: Path) -> None:
    """在系统文件管理器中打开目录（跨平台）。"""
    p = str(target.resolve())
    if sys.platform == "win32":
        os.startfile(p)          # noqa: S606
    elif sys.platform == "darwin":
        subprocess.Popen(["open", p])
    else:
        subprocess.Popen(["xdg-open", p])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--root", default=".", help="项目根目录（含 packs/ 与 config.yaml）")
    args = ap.parse_args()

    import uvicorn
    app = create_app(Path(args.root).resolve())
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
