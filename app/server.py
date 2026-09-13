# -*- coding: utf-8 -*-
"""引擎 HTTP 服务（本地回环）

独立运行:  python -m app.server --port 8765
Electron:  主进程 spawn 本模块并轮询 /api/health

访问控制（详见 security.py）
---------------------------
渲染层改由引擎**同源**提供（`main.js` 用 loadURL 而不是 loadFile），于是
「跨站」重新变得清晰：只有引擎自己的 origin 合法，`null` / `file://` /
`chrome-extension://` 一律拒绝。`/api/*` 再叠一层一次性令牌 ——
修复前这两道都没有，本机任意网页都能读走全部脚本、删记录、改 base_url。

错误映射
--------
修复前 PackError 直接冒到 FastAPI 变成 500「Internal Server Error」，
用户选了个不存在的行业包只得到一句无信息的 500。现在：
    PackError       → 404（行业包不存在）
    StateConflict   → 409（并发操作与当前状态冲突）
    ValueError      → 400（参数不合法）
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import yaml
from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .config import ensure_config_template, load_config, save_config
from .fileio import write_atomic
from .jobs import StateConflict
from .knowledge import Pack, PackError, list_packs
from .llm import LLMClient
from .packgen import create_pack
from .pipeline import MAX_CONCURRENT_JOBS, Pipeline
from .schemas import (ConfirmRequest, GenerateRequest, PackCreateRequest,
                      RewriteSegmentRequest)
from .security import (TOKEN_HEADER, allowed_hostnames, new_token,
                        origin_allowed, token_ok)

FALLBACK_VERSION = "0.0.0-dev"


def read_version(root: Path | None = None) -> str:
    """版本号：**唯一来源是 desktop/package.json**。

    曾经三处各写一遍（`server.py` / `desktop/package.json` / 渲染层 `__ts`），
    改版本号时漏掉一处的后果是「关于页说 0.3.0、安装包还是 0.2.0」，
    而这类漂移没有任何环节会报错。

    打包后引擎的 --root 指向 resources/engine，那里没有 desktop/ 目录，
    所以 Electron 会显式把版本传进来（--version）；只有开发态与手工启动
    才走这里读文件。
    """
    if root is not None:
        f = Path(root) / "desktop" / "package.json"
        try:
            return str(json.loads(f.read_text(encoding="utf-8"))["version"])
        except Exception:                       # noqa: BLE001
            pass
    return FALLBACK_VERSION

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
    """校验行业包名：禁止 .. / \\ 等穿越字符。

    FastAPI 的 `{name}` 匹配单个路径段，但 `%2e%2e` / `%2f` 会在路由匹配**之后**
    被解码成 `..` / `/`，所以「只匹配一段」并不等于安全 —— 必须显式校验。
    """
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
    # 三项为 None 表示「不改」：界面留空时不能把超时写成 0
    retries: int | None = None
    timeout: float | None = None
    max_tokens: int | None = None


class ConfigResetIn(BaseModel):
    fields: list[str]


# 允许「恢复默认」的字段。api_key 不在其中：清空密钥不该是一个顺手的动作。
RESETTABLE_FIELDS = frozenset(
    {"base_url", "model", "temperature", "retries", "timeout", "max_tokens"})


def _renderer_dir(root: Path) -> Path:
    """渲染层目录。

    打包后 `root` 是 `resources/engine`，renderer 作为 extraResources 放在
    `engine/renderer`；开发态则直接是项目里的 `desktop/renderer`。
    """
    for cand in (root / "renderer", root / "desktop" / "renderer"):
        if (cand / "index.html").exists():
            return cand
    return root / "desktop" / "renderer"


def create_app(root: Path, token: str | None = None,
               data_dir: Path | None = None,
               bind_host: str | None = None,
               version: str | None = None) -> FastAPI:
    token = token or new_token()
    # 打包版由 Electron 传入（--version）；开发态回落到读 desktop/package.json
    version = version or read_version(root)
    # 允许出现在 Host 里的主机名：回环 + 显式绑定的地址。
    # 不传 bind_host 时只有回环 —— 这挡住 DNS rebinding（evil.com → 127.0.0.1）。
    hosts = allowed_hostnames(bind_host)
    data_dir = data_dir or root
    # 首次运行写一份带注释的配置模板（不预填 Key）。安装包不带任何 config，
    # 用户可以走设置界面，也可以直接改这个文件。
    ensure_config_template(root, data_dir)
    renderer = _renderer_dir(root)
    pipeline = Pipeline(root, load_config(root, data_dir), data_dir=data_dir)

    def _cfg():
        """每次现读配置：设置里改完 Key，/api/meta 要立刻反映出来。"""
        return load_config(root, data_dir)

    app = FastAPI(title="TalkScript Engine", version=version)
    app.state.token = token
    app.state.pipeline = pipeline

    # ── 访问控制中间件 ──────────────────────────────────────
    PUBLIC_PATHS = {"/api/health"}

    @app.middleware("http")
    async def guard(request: Request, call_next):
        path = request.url.path
        origin = request.headers.get("origin")
        host_header = request.headers.get("host")

        # 1) 同源判定：跨站请求直接 403，且不返回任何 CORS 头，
        #    浏览器因此读不到响应体（这是修复前被放行的那一类请求）。
        if not origin_allowed(origin, host_header, hosts):
            return PlainTextResponse("跨站请求已被拒绝", status_code=403)

        # 2) 令牌：/api/* 一律要带；静态资源与 health 放行。
        #    OPTIONS 预检不带自定义头，必须放行否则浏览器发不出真实请求。
        if (path.startswith("/api/")
                and path not in PUBLIC_PATHS
                and request.method != "OPTIONS"):
            if not token_ok(token, request.headers.get(TOKEN_HEADER)):
                return JSONResponse(
                    {"detail": "缺少或无效的访问令牌。请通过应用入口打开界面，"
                               "或使用启动时打印的带 token 的地址。"},
                    status_code=401)

        return await call_next(request)

    # ── 异常 → HTTP 状态码 ──────────────────────────────────
    @app.exception_handler(PackError)
    async def _pack_error(_req, exc: PackError):
        return JSONResponse({"detail": str(exc)}, status_code=404)

    @app.exception_handler(StateConflict)
    async def _conflict(_req, exc: StateConflict):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    # ── 静态资源（渲染层同源提供）───────────────────────────
    if renderer.exists():
        app.mount("/static", StaticFiles(directory=str(renderer)), name="static")

    @app.get("/")
    def index():
        f = renderer / "index.html"
        if not f.exists():
            return PlainTextResponse("渲染层文件缺失", status_code=500)
        return FileResponse(f, media_type="text/html")

    @app.get("/favicon.ico")
    def favicon():
        return PlainTextResponse("", status_code=204)

    # ── 健康 / 元信息 ───────────────────────────────────────
    @app.get("/api/health")
    def health():
        # 刻意不鉴权：主进程要在窗口打开前轮询它；返回内容不含任何敏感信息。
        return {"ok": True, "version": version, "mock": _cfg().mock}

    @app.get("/api/meta")
    def meta():
        cfg = _cfg()          # 现读：设置里改完模型，这里要立刻反映
        packs = list_packs(root)
        return {
            "packs": [p.model_dump() for p in packs],
            "default_pack": cfg.default_pack,
            "model": cfg.llm.model,
            "base_url": cfg.llm.base_url,
            "has_api_key": bool(cfg.llm.api_key),
            "mock": cfg.mock,
            "max_concurrent": MAX_CONCURRENT_JOBS,
            "version": version,
        }

    # ── 行业包 ──────────────────────────────────────────────
    @app.get("/api/packs/{name}")
    def get_pack(name: str):
        pack = Pack(root, name)                  # 不存在 → PackError → 404
        base = root / "packs" / name
        files = []
        for f in sorted(base.rglob("*")):
            if f.is_file() and "__pycache__" not in str(f):
                size = f.stat().st_size
                if size > 0:
                    files.append({"rel": f.relative_to(base).as_posix(), "size": size})
        checklist = None
        cl = base / "校对清单.md"
        if cl.exists():
            checklist = cl.read_text(encoding="utf-8")
        return {**pack.data, "files": files, "checklist": checklist,
                "has_skill": (base / "skill.yaml").exists()}

    @app.post("/api/packs/create")
    def packs_create(req: PackCreateRequest):
        cfg = _cfg()
        if not (cfg.llm.api_key or cfg.mock):
            raise HTTPException(400, "未配置模型 API Key，请先在设置中填写")
        try:
            return create_pack(root, LLMClient(cfg.llm, mock=cfg.mock),
                               req.industry, req.description)
        except FileExistsError as e:
            raise HTTPException(409, str(e))
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.post("/api/packs/{name}/export-skill")
    def packs_export(name: str, include_private: bool = False):
        from .export_skill import export_agent_skill
        # 这是唯一需要 name 拼路径的写操作：必须走同一套白名单校验，
        # 否则 `%2e%2e` 这类编码会一路走到 Pack(root, "..")。
        _safe_name(name)
        try:
            return export_agent_skill(root, name, include_private=include_private)
        except PackError as e:
            raise HTTPException(404, str(e))
        except Exception as e:  # noqa: BLE001
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

    # ── 脚本生成 ────────────────────────────────────────────
    @app.post("/api/generate")
    def generate(req: GenerateRequest):
        cfg = _cfg()
        if not (cfg.llm.api_key or cfg.mock):
            raise HTTPException(400, "未配置模型 API Key，请在设置中填写后重试")
        pipeline.reload_llm()          # 用最新的 Key/模型，且不影响正在跑的作业
        return {"job_id": pipeline.start_generate(req)}

    @app.get("/api/jobs/{jid}")
    def job_snapshot(jid: str, full: bool = False):
        try:
            job = pipeline.get_job(jid)
        except KeyError:
            raise HTTPException(404, "作业不存在或已随重启释放")
        return job.snapshot(include_result=full)

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

    # ── 历史 ────────────────────────────────────────────────
    @app.get("/api/history")
    def history():
        """全部会话：已落盘的摘要（含失败）+ 内存中正在跑的作业。

        修复前这里每次都要 glob 出所有 result.json 并**完整解析**每一份
        （前端生成期间每 3 秒调一次），而且失败作业没有 result.json、
        只写了 job.json 却从未被读取 —— 重启后从历史里彻底消失。
        现在摘要来自 store 的索引（只有几个字段），失败记录也在里面。
        """
        items = pipeline.store.history(limit=100)
        known = {x["id"] for x in items}
        skip = {"done", "cancelled"}
        running = []
        for snap in pipeline.snapshot_jobs(include_result=False):
            if snap["state"] in skip or snap["id"] in known:
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
        return (running + items)[:100]

    @app.get("/api/history/{jid}")
    def history_item(jid: str):
        """单条记录：优先返回产物；没有产物但有过作业（失败/取消）时返回作业摘要，
        这样界面能显示「这条为什么没出稿」而不是一句 404。"""
        jid = _safe_jid(jid)
        try:
            result = pipeline.store.read_result(jid)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(500, f"记录已损坏，无法读取：{e}")
        if result is not None:
            return result
        snap = pipeline.store.read_job(jid)
        if snap is not None:
            return snap
        raise HTTPException(404, "记录不存在")

    @app.delete("/api/history/{jid}")
    def history_delete(jid: str):
        jid = _safe_jid(jid)
        # 删**全部**命中目录：跨零点分裂出的第二个目录若不删掉，
        # 它的 result.json 下次被扫到，这条记录就「复活」了。
        # 走 pipeline.discard 而不是直接删目录：正在生成的记录还要顺手停掉后台线程。
        outcome = pipeline.discard(jid)
        if outcome == "missing":
            raise HTTPException(404, "记录不存在")
        if outcome == "partial":
            # 文件正被占用（常见：刚落盘就被删，杀毒软件还没松手）。
            # 如实报错胜过假装成功 —— 假装成功的代价是用户刷新后看到记录自己回来了。
            raise HTTPException(500, "记录未能完全删除，文件可能正被占用，请稍后重试")
        return {"ok": True, "id": jid}

    @app.post("/api/history/{jid}/reveal")
    def history_reveal(jid: str):
        """在系统文件管理器里打开该条记录的产物目录。"""
        jid = _safe_jid(jid)
        target = pipeline.store.reveal_target(jid)
        if target is None:
            raise HTTPException(404, "记录不存在")
        try:
            _reveal_in_explorer(target)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(500, f"打开失败：{e}")
        return {"ok": True, "path": str(target.resolve())}

    # ── 设置 ────────────────────────────────────────────────
    @app.get("/api/config")
    def get_config():
        cfg = _cfg()
        return {"base_url": cfg.llm.base_url, "model": cfg.llm.model,
                "api_key_set": bool(cfg.llm.api_key), "temperature": cfg.llm.temperature,
                "retries": cfg.llm.retries, "timeout": cfg.llm.timeout,
                "max_tokens": cfg.llm.max_tokens, "mock": cfg.mock,
                "env_override": bool(os.environ.get("TALKSCRIPT_API_KEY"))}

    @app.post("/api/config")
    def set_config(body: ConfigIn):
        # 空 base_url / model 不覆盖（防清空）；api_key 空串=保持不变（合并语义）
        updates = {k: v for k, v in body.model_dump().items() if v not in ("", None)}
        # 数值项必须在这里卡边界：写进 config.yaml 的 0 / 负数不会被
        # load_config 拦住（那里是 `or 默认值`，0 会悄悄变回默认），
        # 于是界面显示保存成功、实际值却不是用户填的那个 —— 更难查。
        for k, lo, hi in (("retries", 0, 10), ("timeout", 5, 1800),
                          ("max_tokens", 256, 200000)):
            v = updates.get(k)
            if v is None:
                continue
            if not (lo <= float(v) <= hi):
                raise HTTPException(400, f"{k} 需在 {lo} ~ {hi} 之间，当前为 {v}")
        save_config(root, updates, config_dir=data_dir)
        return {"ok": True}

    @app.post("/api/config/reset")
    def reset_config(body: ConfigResetIn):
        """把指定字段清空回默认值。

        存在的原因：`set_config` 会过滤掉空串（防手滑清空），副作用是
        base_url / model 一旦填错就再也改不回去 —— 用户只能去手工改 config.yaml。
        所以「回到默认」必须是**显式**动作，而不是靠留空输入框。

        实现上只是把字段写成空串：`load_config` 里是 `llm.get(x) or DEFAULT`，
        空串自然落回默认值。api_key 不在可重置名单里 —— 清空密钥不该这么顺手。
        """
        bad = [f for f in body.fields if f not in RESETTABLE_FIELDS]
        if bad:
            raise HTTPException(400, f"不支持重置的字段：{'、'.join(bad)}")
        save_config(root, {f: "" for f in body.fields}, config_dir=data_dir)
        return {"ok": True, "fields": body.fields}

    @app.post("/api/config/test")
    def test_config(body: ConfigIn | None = Body(default=None)):
        """用当前配置发一个最小请求，验证 Key / 地址 / 模型名是否可用。

        body 可选：传入界面上尚未保存的值做临时覆盖（Key 留空表示沿用已保存的），
        这样用户填完就能测，不必先点保存。
        """
        fresh = _cfg()
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
    ap.add_argument("--root", default=".", help="项目根目录（含 packs/）")
    ap.add_argument("--data-dir", default=None,
                    help="可写数据目录（config.yaml 与 generated/）；默认同 --root。"
                         "打包后安装目录通常不可写，由 Electron 指到用户数据目录。")
    ap.add_argument("--token", default=None, help="访问令牌（默认随机生成）")
    ap.add_argument("--version", default=None,
                    help="版本号（打包版由 Electron 传入；不传则读 desktop/package.json）")
    ap.add_argument("--open", action="store_true", help="启动后自动打开浏览器")
    args = ap.parse_args()

    import uvicorn
    root = Path(args.root).resolve()
    data_dir = Path(args.data_dir).resolve() if args.data_dir else None
    token = args.token or new_token()
    app = create_app(root, token=token, data_dir=data_dir,
                     bind_host=args.host, version=args.version)
    VERSION = args.version or read_version(root)
    url = f"http://127.0.0.1:{args.port}/?token={token}"
    print("=" * 62)
    print(f"  TalkScript 引擎已启动（v{VERSION}）")
    print(f"  界面地址：{url}")
    print(f"  项目根目录：{root}")
    print(f"  数据目录：{data_dir or root}")
    print("=" * 62, flush=True)
    if args.open:
        try:
            import webbrowser
            webbrowser.open(url)
        except Exception:                       # noqa: BLE001
            pass
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
