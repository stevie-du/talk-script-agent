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
from urllib.parse import urlparse

import yaml
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel

from .config import load_config, save_config
from .knowledge import Pack, list_packs
from .llm import LLMClient
from .packgen import create_pack
from .fileio import write_atomic
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


class ConfigIn(BaseModel):
    """设置页提交的模型配置。

    必须定义在模块级：本模块启用了 `from __future__ import annotations`，
    若把模型类放在 create_app 内部，注解只会留下一个无法解析的 ForwardRef
    （局部类不在模块 globals 里），FastAPI 便拿不到请求体 ——
    字段会永远停在默认空值，表现为「保存」写入空值、「测试连接」恒报未配置。
    """

    base_url: str = ""
    api_key: str = ""
    model: str = ""
    temperature: float | None = None


def _local_host(host: str) -> bool:
    """只认本机回环：引擎绑 127.0.0.1，合法来源不该有别的域名。"""
    host = host.strip("[]").lower()
    return host in ("127.0.0.1", "localhost", "::1", "0.0.0.0")


def _origin_allowed(origin: str | None) -> bool:
    """同源判定：本机/Electron 放行，其它站点一律拒绝。

    原来是 allow_origins=["*"]，等于任何一个你在浏览器里打开的网页都能悄悄
    GET /api/history 把你写过的脚本读走、POST /api/jobs 拿你的 Key 去烧额度、
    DELETE /api/history/{id} 删记录。引擎常年在本机后台跑着（Electron 拉起、
    用完也不一定退），这不是理论风险。

    放行集合刻意包含了 file:// 页面发起请求时的几种形态：没有 Origin（非浏览器
    客户端、同文档请求）、"null"（Chromium 对 file:// 文档统一上报的 Origin）、
    file:// / app://（Electron 打包后的自定义协议）。
    """
    if not origin:
        return True
    o = origin.strip().rstrip("/")
    if o.lower() in ("null", "file:", "file://"):
        return True
    if o.lower().startswith("app://") or o.lower().startswith("chrome-extension://"):
        return True
    return bool(urlparse(o).hostname) and _local_host(urlparse(o).hostname or "")


_CORS_METHODS = "DELETE, GET, OPTIONS, POST"
_CORS_HEADERS = "Content-Type"


def create_app(root: Path) -> FastAPI:
    def _cfg():
        """每次现读配置文件。

        以前只在 create_app 里读一次然后关进闭包，用户在设置里保存 Key 之后，
        /api/meta 仍返回旧模型名、has_api_key 仍是 false，建包接口也照旧拿旧的
        api_key 去拦 —— 表现为「明明刚填好 Key 还提示未配置」，必须重启才生效。
        """
        return load_config(root)

    pipeline = Pipeline(root, _cfg())

    app = FastAPI(title="TalkScript Engine", version=VERSION)

    def _cors_headers(origin: str) -> dict[str, str]:
        return {
            "Access-Control-Allow-Origin": origin,
            "Access-Control-Allow-Methods": _CORS_METHODS,
            "Access-Control-Allow-Headers": _CORS_HEADERS,
            "Access-Control-Max-Age": "600",
            "Vary": "Origin",
        }

    @app.middleware("http")
    async def local_origin_only(request, call_next):
        origin = request.headers.get("origin")
        if not _origin_allowed(origin):
            # 明确拒绝而非静默丢弃：出了问题时前端能看到 403，而不是一个空响应。
            return PlainTextResponse("跨站请求已被拒绝", status_code=403)
        if request.method == "OPTIONS" and origin:
            return PlainTextResponse("", status_code=204, headers=_cors_headers(origin))
        resp = await call_next(request)
        if origin:
            for k, v in _cors_headers(origin).items():
                resp.headers[k] = v
        return resp

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
        return {"ok": True, "version": VERSION, "mock": _cfg().mock}

    @app.get("/api/meta")
    def meta():
        cfg = _cfg()          # 现读：设置里改完模型，这里要立刻反映出来
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
        try:
            job = pipeline.get_job(jid)      # 走锁，避免与生成线程同时碰字典
        except KeyError:
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
        # 同样现读：有不少用户是「先建自己的行业包 → 再写脚本」，而填 Key 和建包
        # 之间往往不重启。用启动时那份旧配置拦，就会误报「未配置 API Key」。
        cfg = _cfg()
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
        write_atomic(p, yaml.safe_dump(data, allow_unicode=True, sort_keys=False))
        return {"ok": True, "name": name, "draft": False}

    # ── 历史 ────────────────────────────────────────────────
    @app.get("/api/history")
    def history():
        """全部会话：已落盘的结果 + 内存中正在跑的作业。

        会话应当是「一发起就有记录」。只扫 result.json 的话，生成中的会话在左栏
        完全不可见，用户切走就找不回正在生成的那一次；把在跑作业并进同一张列表，
        前端就能用同一个模子渲染，不必另造一个「生成中」分组。
        """
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
                        "state": "done",
                    })
                except Exception:
                    continue
        # 未落盘的作业插到最前：它们必然比已落盘的新，这样也无需对两种时间格式排序。
        # failed 必须算进来 —— 失败的作业没有 result.json，若当成终态一起排除，
        # 它会从列表里彻底消失，用户既看不到这条记录也不知道它失败了。
        # 只有 cancelled 排除：那是用户自己按的停止，不必再占一条。
        done_ids = {x["id"] for x in out}
        skip = {"done", "cancelled"}
        running = []
        # 必须走 snapshot_jobs() 取快照拷贝：直接迭代 pipeline.jobs.values() 时，
        # 后台线程可能正好在 start_generate 里插入新作业 —— 一边遍历一边增，
        # 偶发 RuntimeError: dictionary changed size during iteration，整个列表 500。
        for snap in pipeline.snapshot_jobs():
            if snap["state"] in skip or snap["id"] in done_ids:
                continue
            p = snap.get("params") or {}
            running.append({
                "id": snap["id"], "created_at": snap["created_at"],
                "pack": p.get("pack", ""), "topic": p.get("topic", ""),
                "duration": p.get("duration"),
                "chars": None, "passed": None,
                "state": snap["state"],
            })
        running.sort(key=lambda x: x["created_at"], reverse=True)
        return (running + out)[:100]

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
