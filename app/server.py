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

令牌怎么交到引擎手里（缺陷 1）
------------------------------
Electron **不再**把令牌写在子进程的命令行上（`--token`）：Windows 上同机任意
进程都能读到别人的 argv。现在走子进程环境变量 `TALKSCRIPT_TOKEN`。
`--token` 这个 flag 保留给手工起引擎调试的场景，看门狗用的 `--parent-pid`
不是凭证、继续走 argv。

错误映射
--------
修复前 PackError 直接冒到 FastAPI 变成 500「Internal Server Error」，
用户选了个不存在的行业包只得到一句无信息的 500。现在**只有三个**全局处理器
（见下面 `@app.exception_handler`）：
    PackError       → 404  code=pack_missing     （行业包不存在）
    PackBrokenError → 409  code=pack_broken      （包在，但内容要人修）
    StateConflict   → 409  code=quota_exceeded / state_conflict
每个应答都同时给 `detail`（给人看的那一句，渲染层原样显示）与 `code`
（给程序看的稳定码）。两个 409 靠 code 分开：额度满与坏包是两件不同的事，
只看状态码会把坏包说成「队列满了」（P1-1）。

其余两类不在全局层：
    ValueError      → 400，由各端点就地 `raise HTTPException(400, str(e))`
                      （作业取消 / 重写 / 配置校验各自的措辞不同）
    LLMError        → **不映射成 HTTP 状态码**：它发生在作业线程里，被
                      app/pipeline.py 捕获后落成 `state=failed` + `error`，
                      界面在作业失败横幅上看到具体原因；设置页「测试连接」
                      走 LLMClient.ping()，它自己把异常转成 (False, 说明)。
                      本项目里没有任何一条请求会把 LLMError 冒到 HTTP 层，
                      所以这里也就没有一个 502 处理器（此处以前写着有）。
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

import yaml
from fastapi import Body, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .config import (DEFAULT_MODEL, ensure_config_template, load_config,
                     load_raw_models, public_models, save_config, save_models)
from .fileio import write_atomic
from .intel import IntelSource as _IntelSourceDC, add_ignored, today as intel_today
from .jobs import StateConflict

log = logging.getLogger(__name__)
from .knowledge import Pack, PackBrokenError, PackError, list_packs
from .llm import LLMClient
from .packseed import PRIVATE_DIR_NAME, seed_bundled_packs
from .packimport import PackImportError, delete_pack, import_pack
from .pipeline import MAX_CONCURRENT_JOBS, Pipeline
from .schemas import (GenerateRequest, IntelIgnoreRequest, IntelPackRequest,
                      PackCreateRequest, RewriteSegmentRequest)
from .security import (LOOPBACK_HOSTS, TOKEN_HEADER, allowed_hostnames,
                       new_token, origin_allowed, token_ok)

FALLBACK_VERSION = "0.0.0-dev"

# 引擎令牌从环境变量读取（**不走命令行**，见 main() 的说明）。
# 名字与 .env 风格一致、只在本模块用，故不放进 config.py 的 _LLM_ENV。
TOKEN_ENV = "TALKSCRIPT_TOKEN"


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
# ⚠ 这两条**不带** `^`/`$`，一律用 `.fullmatch()`：Python 的 `$` 允许串尾多一个换行，
# 于是 `"elevator\n"`、`"20260101-000000-abcdef\n"` 在引擎这边算合法，而 JS 的 `$`
# 不允许 —— 桩判不合法、引擎放行（第 16 轮复核实测，方向正是"桩比引擎严"那侧，
# 也就是本仓库对账守卫写明不可接受的那一侧）。Windows 上尾部空白还会被文件系统
# 吃掉，`packs/elevator\n` 实际落到 `packs/elevator`：一条 URL 能指到真的包上。
_JID_RE = re.compile(r"\d{8}-\d{6}-[0-9a-f]{6}")
_NAME_RE = re.compile(r"[\w\u4e00-\u9fff-]+")


def _safe_jid(jid: str) -> str:
    """校验作业 id：既防路径穿越，也防 glob 通配（如 jid=* 命中任意记录）。"""
    if not _JID_RE.fullmatch(jid or ""):
        raise HTTPException(400, "记录标识不合法")
    return jid


def _safe_name(name: str) -> str:
    """校验行业包名：禁止 .. / \\ 等穿越字符。

    FastAPI 的 `{name}` 匹配单个路径段，但 `%2e%2e` / `%2f` 会在路由匹配**之后**
    被解码成 `..` / `/`，所以「只匹配一段」并不等于安全 —— 必须显式校验。
    用 `fullmatch` 而不是 `match` + `$`：后者会放行尾部换行（见上面 `_NAME_RE` 的注释）。
    """
    if not _NAME_RE.fullmatch(name or "") or ".." in name:
        raise HTTPException(400, "行业包名称不合法")
    return name


# ── 错误的机器可读码（异常 → HTTP 的那一层用）─────────────────
# 为什么状态码不够：「同时进行的生成已达上限」与「行业包的 pack.yaml 语法有误」
# 都是 409 Conflict（两者确实都是"请求与资源当前状态冲突"），但用户要做的
# 是两件完全不同的事 —— 等一拍再发 vs 去修那个文件。
# 修复前渲染层只判 `status === 409`，于是坏包被说成「队列满了」，
# 而服务端那句具体的「第 4 行第 6 列」被整句丢掉（P1-1）。
# 所以：detail 继续是给人看的那一句（原样进 toast，不许改写成别的事），
# code 是给程序看的那一个 —— 判据读 code，不读文案。
ERR_PACK_MISSING = "pack_missing"       # → 404 包不存在
ERR_PACK_BROKEN = "pack_broken"         # → 409 包在，但内容要人修
ERR_QUOTA = "quota_exceeded"            # → 409 并发额度满（可重试）
ERR_STATE_CONFLICT = "state_conflict"   # → 409 作业状态不允许这个操作
ERR_FIELD_INVALID = "field_invalid"     # → 422 请求字段不合格（pydantic 挡的，不是业务挡的）
# → 500 未预期的内部错误（磁盘满 / 线程起不来 / 任何没被上面接住的异常）。
# 没有这一条时，这类错误冒到 FastAPI 变成 500 text/plain「Internal Server
# Error」—— 空 body、无 code，渲染层只看到「HTTP 500」，排查无从下手
# （五路审查 P0-5，2026-09-23：start_generate 的 job_dir 建不出来等 5 处
# 抛出点都走这条路）。detail 带异常原文，code 稳定可判。
ERR_INTERNAL = "internal_error"

# StateConflict 的三个抛出点（app/pipeline.py 的生成 / 建包 / 单段重写额度闸）
# 都带「已达上限」这四个字，而 jobs.py 的那一条是「作业状态为 X，无法执行该操作」。
# 这里按**共有片段**分桶而不是抄整句：文案改了标点不会让分桶悄悄失效，
# 而真改了语义（不再说"上限"）时会落到 state_conflict —— 那是更安全的失败方向。
_QUOTA_MARK = "已达上限"


def _error_json(message: str, status: int, code: str) -> JSONResponse:
    """错误应答：人话（detail）+ 机器可读码（code）一起给。

    `detail` 保持**字符串**原样：多处测试与渲染层都直接读它，
    把它换成对象会让「detail 里有没有那句话」的断言读不到东西。
    """
    return JSONResponse({"detail": message, "code": code}, status_code=status)


# 请求体字段 → 界面上那个输入框的叫法。渲染层把 detail 原样进错误框（api.js 的约定：
# 「detail 里就是人话」），而 pydantic 给的是英文 + 内部字段名 —— 用户在表单里
# 找不到叫 `description` 的东西。抄字段名而不是抄整句英文，是为了让这条表跟着界面文案走。
_FIELD_LABELS = {
    "topic": "主题", "pack": "行业包", "duration": "时长（秒）", "rate": "语速（字/秒）",
    "audience": "受众", "style": "风格", "persona": "人设", "platform": "平台",
    "cta": "结尾引导", "facts": "补充资料", "segment": "段落", "index": "段落序号",
    "feedback": "修改意见", "industry": "行业名称", "description": "业务描述",
    # 第 9 轮复核补的：这三类控件在界面上有中文名，之前一律露出裸字段名
    "voice": "人味档位", "format": "输出内容", "reroll": "换一版",
    "full": "完整快照", "include_private": "包含私有资料",
}


def _num(v) -> str:
    """界面上「时长不能大于 600.0」这种小数尾巴去掉（schema 里 duration 是 float）。"""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    return str(int(f)) if f.is_integer() else repr(f)


def _humanize_validation(errors: list) -> str:
    """pydantic 的字段错 → 一句"哪个框、要怎么改"的中文。

    实测到的形态：建包时业务描述只打三个字 → 422 的 detail 是
    `[{'type': 'string_too_short', …, 'msg': 'String should have at least 4 characters'}]`，
    而 api.js 对数组的处理是 `map(x => x.msg).join('；')` —— 于是中文界面里
    冒出英文原文，用户不知道该往哪个框里补几个字。
    """
    parts: list[str] = []
    for e in errors or []:
        loc = [str(x) for x in (e.get("loc") or []) if x not in ("body", "query")]
        key = loc[-1] if loc else "请求内容"
        label = _FIELD_LABELS.get(key, key)
        ctx = e.get("ctx") or {}
        kind = str(e.get("type") or "")
        if kind.endswith("too_short"):
            bound = ctx.get("min_length", ctx.get("min_items", "?"))
            unit = "个字" if "string" in kind else "项"
            parts.append(f"{label}太短了，要至少 {bound} {unit}")
        elif kind.endswith("too_long"):
            bound = ctx.get("max_length", ctx.get("max_items", "?"))
            unit = "个字" if "string" in kind else "项"
            parts.append(f"{label}太长了，最多 {bound} {unit}")
        elif "greater_than_equal" in kind:
            parts.append(f"{label}不能小于 {_num(ctx.get('ge', '?'))}")
        elif "less_than_equal" in kind:
            parts.append(f"{label}不能大于 {_num(ctx.get('le', '?'))}")
        elif kind == "greater_than":
            parts.append(f"{label}要大于 {_num(ctx.get('gt', '?'))}")
        elif kind == "less_than":
            parts.append(f"{label}要小于 {_num(ctx.get('lt', '?'))}")
        elif kind == "missing":
            parts.append(f"{label}不能为空")
        elif kind == "bool_parsing" or kind == "bool_type":
            parts.append(f"{label}只能是要或不是（true / false）")
        elif kind == "literal_error":
            # 枚举值写错：pydantic 给的是 "Input should be 'strong', 'standard' or 'off'"，
            # 中文界面里露这句等于没说话（第 9 轮实测：voice / format 两个下拉都走这里）。
            allowed = str(ctx.get("expected") or "").replace("'", "").strip()
            parts.append(f"{label}只能是这几个值之一：{allowed}" if allowed
                         else f"{label}的值不在允许的范围内")
        elif kind in ("json_invalid", "json_load_failed"):
            # pydantic 给 JSON 解析错的 loc **不是字段名**，而是位置。实测 2.13.5 +
            # FastAPI 0.141 下七种畸形 body 全是 `["body", <字符偏移>]` 一个数
            # （第 14 个字符 / 第 213 个字符…），msg 恒为 "JSON decode error"。
            # 之前取末段当"哪个框"，于是界面上一句「14 不是合法的 JSON（JSON decode error）」
            # —— 一个不存在的框 + 一句英文，等于没说（第 10 轮复核 P2）。
            # ⚠ 不写"两个数 = 行/列"那一支：第 11 轮复核量过，它在这套版本上永不达，
            #   留着就是装饰码 + 一句站不住的注释。
            nums = [x for x in loc if str(x).isdigit()]
            parts.append(("请求内容不是合法的 JSON" if nums else
                          f"{label}不是合法的 JSON")
                         + (f"（第 {nums[0]} 个字符附近）" if nums else ""))
        elif kind.endswith("_parsing"):
            parts.append(f"{label}要填数字")
        elif kind == "string_type":
            parts.append(f"{label}要填文字")
        else:
            # 认不出的类型不猜，但也不把英文原句直接当界面用语：至少先说清是哪个框，
            # 原文跟在后面供排查（第 9 轮：末支原来整句都是英文）。
            parts.append(f"{label}填写有误：{e.get('msg') or kind or '字段不合格'}")
    return "；".join(p for p in parts if p) or "请求里有字段不合格"


# ── 私有资料判定 ────────────────────────────────────────────
# 目录名 `private` 的**唯一**定义在 app/packseed.py 的 PRIVATE_DIR_NAME
# （播种时要靠它决定"哪些内容属于用户自己的、不许被出厂更新删掉"），
# 这里 import 它而不是再抄一个字面量：两处各写一份，迟早有一处改漏 ——
# 漏的那一处就是"界面读得到私有资料"或"更新删掉了私有资料"。


def _is_private_rel(rel: str) -> bool:
    """这个包内相对路径是否落在 `private/` 下（安装包刻意不带、也不该经 HTTP 外读）。

    判据用**路径段**而不是 `rel.startswith("private/")`：
      · Windows 文件系统不区分大小写，`Private/products.yaml` 是同一个文件；
      · 反斜杠形态（`private\\products.yaml`）也要挡住，否则等于没拦。
    与 pack.yaml 的 `files.private` 是同一个约定（那些文件都在 `private/` 段下）。
    """
    parts = re.split(r"[/\\]+", rel or "")
    return any(p.lower() == PRIVATE_DIR_NAME for p in parts)


# ── 模型请求地址校验 ────────────────────────────────────────
class BaseUrlError(ValueError):
    """地址**非法**（必须拒掉，不能只是警告）。"""


def check_base_url(raw: str) -> list[str]:
    """校验一条模型请求地址，返回**警告**列表（可能为空）。

    这台机器是用户自己的，本地 LLM 走 `http://192.168.x.x:9200/v1` 是正常用法，
    所以这里刻意做两件事的分工：**危险/写进去必然出错的** reject，
    **只是不体面的** warn。硬拦明文 http 会把用户已经在跑的配置改坏。

    拒绝（`BaseUrlError`）：
      · 前缀不是 http:// / https:// —— 少协议头会一路存到请求层才炸（原有闸，保留）；
        顺带堵住 `file://`、`javascript:`、`data:`、`gopher:` 这类不该出现在这里的 scheme；
      · 主机名为空（`http://` 后面什么都没有）；
      · 内嵌凭据（`http://user:pass@host`）—— httpx 会把它自动变成 Basic 头，
        等于把另一份凭据塞进每个请求里，而设置页上看不见；
      · 含空白 / 控制字符（粘贴带空格、`\\n` 尾巴，会让所有请求静默失败）。

    警告：明文 http 且主机不是回环 —— 每一次生成都会把 `Bearer <api_key>`
    和整段私有资料以明文发出去，同网段可直接读。
    """
    url = (raw or "").strip()
    if not url:
        return []
    # 内部控制字符 / 换行：先单独判，否则后面的报错会指不到根因
    if re.search(r"[\x00-\x1f\x7f]", raw):
        raise BaseUrlError("请求地址含控制字符或换行，请检查是否粘贴错了内容")
    if re.search(r"\s", url) or url != raw.strip():
        raise BaseUrlError("请求地址不能含空格")
    low = url.lower()
    if low.startswith(("http://", "https://")):
        pass
    elif "://" in url or ":" in url.split("/", 1)[0]:
        scheme, _, rest = url.partition(":")
        if scheme.lower() in ("http", "https"):
            # `https://`（只填了协议头）与 `http:/a`（少一个斜杠）都会走到这里。
            # 前者要说的是"主机名没写"，说斜杠反而把人引开 —— 接口层的
            # `.rstrip("/")` 会把 `https://` 变成 `https:`，所以两种都得判。
            if not rest.lstrip("/"):
                raise BaseUrlError("请求地址里没有主机名（只有协议头）")
            # `http:/a` 这类手滑：说清是斜杠的问题，
            # 而不是回一句「协议只能是 http 或 https」——他写的就是 http。
            raise BaseUrlError("请求地址要以 http:// 或 https:// 开头（两个斜杠）")
        raise BaseUrlError(f"请求地址的协议只能是 http 或 https，当前为「{scheme}」")
    else:
        # 用户最常见的漏填是「api.openai.com/v1」少了协议头 ——
        # 不拦的话，这个值会一路存到生成时才在 urllib 里炸，报错完全指不到根因。
        raise BaseUrlError("请求地址要以 http:// 或 https:// 开头")
    try:
        u = urlparse(url)
        host = (u.hostname or "").strip()
    except ValueError:
        raise BaseUrlError("请求地址无法解析，请检查拼写")
    if not host:
        raise BaseUrlError("请求地址里没有主机名")
    if "@" in (u.netloc or ""):
        raise BaseUrlError("请求地址不能内嵌账号密码（http://user:pass@host），"
                           "请用 API Key 字段认证")
    try:
        u.port                      # 端口非数字要在这里就报错，而不是等到发请求
    except ValueError:
        raise BaseUrlError("请求地址的端口不合法")
    warnings: list[str] = []
    if u.scheme == "http" and host.lower() not in LOOPBACK_HOSTS:
        warnings.append(
            f"请求地址走的是明文 HTTP，而目标不是本机（{host}）："
            "每次生成都会把 API Key（Bearer 头）与整段私有资料以明文发出去，"
            "同一网络里任何一台机器都能读到。建议改用 https，或让本地网关做 TLS。")
    return warnings


# ── pack.yaml 的原地改写 ───────────────────────────────────
_DRAFT_KEY_RE = re.compile(r"draft:([ \t]*)(.*)$")


def undraft_yaml_text(text: str) -> str:
    """把 pack.yaml 里顶层 `draft:` 的值改成 false，**其余字节一律不动**。

    为什么不能用 `yaml.safe_load` + `yaml.safe_dump` 往返（这是原来的实现）：
    PyYAML 不保留注释，而**这个仓库里 pack.yaml 的注释就是包作者的文档契约**
    （elevator/pack.yaml 有 12 行注释：字段含义、"分享给同事 = 复制目录（不含
    private/ 即不带商业信息）"这类约定）。点一次界面上的「标记为已校对」就把
    它们永久抹掉，而且是不可逆的内容丢失 —— 运行依赖里没有 ruamel.yaml
    （见 requirements-runtime.txt），为一个键引入新依赖也不值。

    实现是**行级**改写，只动 `draft:` 那一行的值，行尾注释原地保留：
        draft: true   # true 时界面显示"草稿·需人工校对"角标
      → draft: false   # true 时界面显示"草稿·需人工校对"角标

    约束：
      · 只认**零缩进**的 `draft:`（YAML 里那才是顶层键），注释行不算；
      · 同名键出现多次时全部改掉 —— 否则留下一个 `draft: true` 在后面，
        PyYAML 取最后一个，"已校对"会静默失效；
      · 没有这个键时追加一行（老包 / 手工写漏的包）；
      · 换行风格（\\r\\n / \\n）与 BOM 都按原文件保持。
    """
    bom = "\ufeff" if text.startswith("\ufeff") else ""
    body = text[len(bom):]
    nl = "\r\n" if "\r\n" in body else "\n"
    lines = body.split(nl)
    hits = 0
    for i, ln in enumerate(lines):
        if ln[:1].isspace() or ln.startswith("#"):
            continue
        m = _DRAFT_KEY_RE.match(ln)
        if not m:
            continue
        indent, rest = m.group(1), m.group(2)
        # 行尾注释：值与 `#` 之间必须有空白，`http://x#y` 那种不是注释。
        cm = re.match(r"(.*?)([ \t]+#.*)?$", rest)
        comment = (cm.group(2) or "") if cm else ""
        lines[i] = f"draft:{indent}false{comment}"
        hits += 1
    if hits:
        return bom + nl.join(lines)
    tail = "" if (not body or body.endswith(nl)) else nl
    return bom + body + tail + "draft: false" + nl



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
    # 「测试连接」要测哪一条模型（不传 = 当前生效的那条）。
    # 编辑一条**非当前**模型时，密钥留空意味着「沿用那条模型存着的 Key」——
    # 不指明 id 就会错拿当前模型的 Key 去测，测出来的结果与用户以为的不是一回事。
    model_id: str = ""
    temperature: float | None = None
    # 三项为 None 表示「不改」：界面留空时不能把超时写成 0
    retries: int | None = None
    timeout: float | None = None
    max_tokens: int | None = None


class ConfigResetIn(BaseModel):
    fields: list[str]


class ModelIn(BaseModel):
    """添加 / 编辑一个模型。

    `id` 空 = 新增；非空 = 改那一条。
    ⚠ `api_key` 空串是「保持不变」（与 `/api/config` 同一个语义）——
    界面上那个框在编辑时是空的，用户没重填就说明密钥不该动。
    """

    id: str = ""
    name: str = ""
    base_url: str = ""
    model: str = ""
    api_key: str = ""


class ModelIdIn(BaseModel):
    id: str


# 知识库面板可读的文件类型与体积上限（详见 /api/packs/{name}/file 的说明）
_TEXT_SUFFIXES = frozenset({".md", ".txt", ".yaml", ".yml", ".py", ".json"})
_MAX_FILE_BYTES = 256 * 1024

# 允许「恢复默认」的字段。api_key 不在其中：清空密钥不该是一个顺手的动作。
RESETTABLE_FIELDS = frozenset(
    {"base_url", "model", "temperature", "retries", "timeout", "max_tokens"})

# 数值项的合法区间。**这是唯一的一份** —— 前端 `settings.js` 的
# `NUMERIC_BOUNDS` 必须与它逐项相等，`tests/test_numeric_bounds_consistency.py`
# 会同时读这两个文件比对（前后端没法共享代码，只能靠断言钉住）。
#
# 为什么要专门钉：这两处曾经不一致 —— 前端卡 0 ~ 1.5、后端卡 0.0 ~ 2.0，
# 于是用户填 1.8 会被**前端**拒掉，而后端完全接受。前端比后端更严，
# 用户看到的是「界面说不行」，没有任何办法绕过，也不会想到是界面在凭想象设限。
#
# temperature 的下界含 0.0：0 是合法采样温度（求确定性输出），
# 而 `load_config` 曾经用 `or 默认值` 把它当成「没填」—— 见 P0-2。
NUMERIC_BOUNDS: dict[str, tuple[float, float]] = {
    "retries": (0, 10),
    "timeout": (5, 1800),
    "max_tokens": (256, 200000),
    "temperature": (0.0, 2.0),
}

# ── 纵深防御：渲染层的 CSP（P2-5）────────────────────────────
#
# 渲染层要展示**模型生成的内容**，而且不再是 `file://` 加载 —— 它从引擎
# 同源加载（`loadURL` 到 127.0.0.1:<port>，见 desktop/main.js）。转义目前是
# 全量排查过的：12 个模块所有 `innerHTML` 写入点的动态值都过了
# `esc()` / `fmtText()` / `textContent`，**所以这不是一个现成漏洞**。
# 但一旦将来某处漏了转义，没有第二道防线 —— CSP 就是那第二道。
#
# 刻意**不带** `'unsafe-inline'`：
#   - `script-src`：index.html 里没有内联 `<script>`，也没有内联事件处理器
#     （`onclick=` 之类全项目 0 处）；
#   - `style-src` ：仅有的 5 处内联 `style=` 已改成 class（`.col-*` / `.cell-mono`）；
#     `el.style.setProperty(...)` 走 CSSOM，不受 `style-src` 管辖。
#
# 收紧不是「但愿没事」，是**可证伪**的：`_verify/verify.js` 全程监听
# `securitypolicyviolation`，跑完整流程 + 真实下载（`blob:`）都必须是 0 条。
# 谁以后再往模板里写 `style="..."`，那条断言就会红 ——
# 否则内联样式会被**静默忽略**（列宽失效但界面不报错），正是本项目最忌讳的失效。
CSP = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self'; "
    "img-src 'self' data:; "      # styles.css 的下拉箭头是 data:image/svg+xml
    "font-src 'self'; "
    "connect-src 'self'; "
    "object-src 'none'; "
    "base-uri 'none'; "
    "form-action 'none'; "
    "frame-ancestors 'none'"
)


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
               version: str | None = None,
               packs_dir: Path | None = None) -> FastAPI:
    """起一个引擎实例。

    `packs_dir`（缺陷 8，打包版由 Electron 指到 `%APPDATA%\\TalkScript\\packs`）：
    **可写**的行业包根目录。不传时它就是 `root/packs`，行为与以前完全一致。
    之所以需要这一层：打包后 `root` 在安装目录（默认
    `%LOCALAPPDATA%\\Programs\\TalkScript\\resources\\engine`），
    那里既可能被升级 / 卸载整个抹掉，也可能根本没有写权限 ——
    于是用户新建的行业包、手改的 banwords.yaml、填进去的 private/ 资料
    都会在一次应用更新后消失，而 README 只承诺了 config 与 generated/ 会保留。
    安装目录里那份现在退化为**只读的种子源**：首次运行拷进用户目录，
    之后不覆盖用户的改动（`app/packseed.py`）。
    """
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

    # ── 行业包的两个路径（务必分清，见上面的说明）
    #   packs_root      包目录本身：packs/<name>/pack.yaml 的 packs/
    #   root_for_packs  交给 knowledge.Pack / list_packs / Pipeline 的那个 root
    #                   —— 它们都自己拼 `root / "packs"`，所以必须正好对上
    packs_root = Path(packs_dir).resolve() if packs_dir else (root / "packs")
    root_for_packs = packs_root.parent if packs_dir else root
    if packs_dir:
        # 只拷不覆盖；失败不拦引擎启动（顶多用不到出厂包，界面上会少几个行业）
        seed_bundled_packs(root / "packs", packs_root, version)
    pipeline = Pipeline(root_for_packs, load_config(root, data_dir), data_dir=data_dir)

    def _cfg():
        """每次现读配置：设置里改完 Key，/api/meta 要立刻反映出来。"""
        return load_config(root, data_dir)

    def _require_model(cfg) -> None:
        """生成 / 建包前的模型可用性检查（三种出口各自说清缺什么）。

        2026-09-17：模型不再有「内置默认」**条目**，而且开关**可以关掉**
        （用户报「模型开启时无法关闭」）—— 所以要能把三种"不能生成"分开说：
        一条模型都没加、加了但都没启用、启用了但没填 Key。
        同一句「未配置 Key」会把前两种说成第三种，把用户指向错的地方。
        """
        if cfg.mock:
            return
        if not cfg.models:
            raise HTTPException(400, "还没有配置模型 —— 请在「设置 → 模型接口」里点右上角「添加模型」")
        if not cfg.active_model:
            raise HTTPException(400, "当前没有启用任何模型 —— 请在「设置 → 模型接口」里打开一个模型的开关")
        if not cfg.llm.api_key:
            raise HTTPException(400, "当前模型还没配 API Key，请在「设置 → 模型接口」里填写")

    def _link_warnings(cfg) -> list[str]:
        """把每条模型地址里「合法但不体面」的问题汇总成一句句能照着做的话（缺陷 5）。

        为什么常驻 GET /api/config 也要下发，而不是只在「保存」那次返回：
        明文 HTTP 这件事是**每一次生成**都在发生的（Key 与私有资料以明文出网），
        而保存后的提示条几秒就消失、刷新界面就什么都看不到了。
        只警告不拦截：本机跑 Ollama / LM Studio 的用户用的就是 `http://<ip>:9200/v1`，
        硬拦等于把人家在用的配置改坏 —— 这是本地优先工具，不是公网服务。
        """
        out: list[str] = []
        seen = set()
        for m in cfg.models:
            seen.add(m.base_url)
            try:
                ws = check_base_url(m.base_url)
            except BaseUrlError as e:
                # 已经写在文件里的坏地址也要说 —— 这道闸只管新写的，
                # 老配置不会因此跑不起来，但用户得知道它为什么连不上。
                out.append(f"模型「{m.label}」的请求地址不合法：{e}")
                continue
            out.extend(f"模型「{m.label}」：{w}" for w in ws)
        if cfg.llm.base_url and cfg.llm.base_url not in seen:
            # 环境变量 TALKSCRIPT_BASE_URL 覆盖出来的地址不在任何条目里。
            # 不单独判一次的话，「界面上看着一切正常、实际在往明文远端发 Key」
            # 这个组合就完全隐身了。
            try:
                out.extend(f"（来自环境变量 TALKSCRIPT_BASE_URL）{w}"
                           for w in check_base_url(cfg.llm.base_url))
            except BaseUrlError as e:
                out.append(f"环境变量 TALKSCRIPT_BASE_URL 的地址不合法：{e}")
        return out

    # 中间件的令牌那道只管 `/api/*`（见下面 guard 的 startswith），而 FastAPI 默认就把
    # /docs、/redoc、/openapi.json 挂在中间件之内、令牌范围之外 —— 实测默认参数下
    # `GET /openapi.json` 返回 200 全量接口结构，里面连读配置的 GET /api/config 都写清楚了。
    # 同机任意进程或浏览器里任意一个页面都能顺着它把接口摸清楚。
    # 直接关掉而不是补白名单 —— 少一类要记得拦的东西。
    # 需要看接口清单就读 README，那里也写了参数含义。
    app = FastAPI(title="TalkScript Engine", version=version,
                  docs_url=None, redoc_url=None, openapi_url=None)
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

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        """给每个响应挂上 CSP（P2-5）。

        挂在**所有**响应上而不是只挂 `index.html`：`/static/js/*.js` 这些子资源
        的 CSP 由**文档**响应决定，逐个挂没有意义；而挂全量只是多一个几十字节的
        响应头，换来的是「不会有人漏挂某一类响应」。
        """
        resp = await call_next(request)
        resp.headers["Content-Security-Policy"] = CSP
        return resp

    # ── 异常 → HTTP 状态码 ──────────────────────────────────
    # 顺序无关：Starlette 沿 `type(exc).__mro__` 找第一个注册的处理器，
    # 所以子类 PackBrokenError 会命中下面那条，不会被 PackError 抢走。
    # 每一条都给 detail（人话）+ code（机器可读，见上面的 ERR_* 常量）：
    # 状态码分不开的两个 409，靠 code 分开 —— 渲染层从此不再凭 409 猜原因。
    @app.exception_handler(PackError)
    async def _pack_error(_req, exc: PackError):
        return _error_json(str(exc), 404, ERR_PACK_MISSING)

    @app.exception_handler(PackBrokenError)
    async def _pack_broken(_req, exc: PackBrokenError):
        # 409 而不是 404：包**就在那儿**，是内容要人去修。给 404 会让用户
        # 在列表里反复找一个明明看得见的包（这是 P1-5/P1-6 的修复之一）。
        return _error_json(str(exc), 409, ERR_PACK_BROKEN)

    @app.exception_handler(StateConflict)
    async def _conflict(_req, exc: StateConflict):
        # 同一个异常类挂着两种完全不同的意思（额度满 / 状态不对），
        # 所以 code 按消息分桶 —— 两者都该 409，但界面上一个是「等一拍」
        # 一个是「这条作业已经不是那个状态了」，说错的那一句会让人去查错地方。
        msg = str(exc)
        return _error_json(msg, 409,
                           ERR_QUOTA if _QUOTA_MARK in msg else ERR_STATE_CONFLICT)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_req, exc: RequestValidationError):
        # 422 由 pydantic 代发，默认 detail 是**英文原文列表**，而渲染层按约定把
        # detail 当人话直接显示 —— 描述打三个字就会在中文界面里露出
        # 「String should have at least 4 characters」。状态码不动（有按码分流的调用方），
        # 只把 detail 换成人话；code 给 field_invalid，让界面将来要区分时不必猜文案。
        return _error_json(_humanize_validation(list(exc.errors() or [])),
                           422, ERR_FIELD_INVALID)

    # 兜底：没被上面任何一类接住的异常（OSError 建不出目录 / RuntimeError
    # 起不来线程 / 任何漏网的 bug）。没有这条时它们冒到 FastAPI 的默认
    # ServerErrorMiddleware，变成 **500 + text/plain「Internal Server Error」**
    # —— 空 body、无 code，前端只看到「HTTP 500」，与本文件顶部「每个应答都
    # 同时给 detail 与 code」的承诺正相反。detail 带异常原文（log.exception
    # 会把全量堆栈打进日志，这里只给人话那一句 + 类型名）。
    @app.exception_handler(Exception)
    async def _unhandled(_req, exc: Exception):
        log.exception("未处理的内部错误：%s: %s", type(exc).__name__, exc)
        return _error_json(f"引擎内部错误（{type(exc).__name__}）：{exc}",
                           500, ERR_INTERNAL)

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
        packs = list_packs(root_for_packs)
        return {
            "packs": [p.model_dump() for p in packs],
            "default_pack": cfg.default_pack,
            # 行业包目录（**可写的那份**）。设置页要把它摊出来 ——
            # 用户手改 pack.yaml / 放 private 资料时必须知道去哪儿改；
            # 打包版尤其要紧，因为那里已经不是安装目录了。
            "packs_dir": str(packs_root),
            "model": cfg.llm.model,
            # 模型名是不是内置默认（用户没配过）。输入区右侧的模型选择器靠它
            # 把「glm-4.7」标成「glm-4.7（默认）」—— 不标的话，未配置状态下
            # 界面看起来像是已经配好了模型。
            "llm_defaulted": cfg.llm_defaulted,
            # 模型列表（**已剥掉 api_key 明文**）与当前生效的那条。
            # 输入区右侧的选择器直接列它，不再依赖 localStorage 里的历史。
            "models": public_models(cfg),
            "active_model": cfg.active_model,
            "base_url": cfg.llm.base_url,
            "has_api_key": bool(cfg.llm.api_key),
            "mock": cfg.mock,
            "max_concurrent": MAX_CONCURRENT_JOBS,
            "version": version,
        }

    # ── 行业包 ──────────────────────────────────────────────
    @app.get("/api/packs/{name}")
    def get_pack(name: str):
        # 必须与 pack_file / undraft 走同一套白名单。
        # 这里曾经漏了 —— 后果不是「读不到包」，而是 `%2e%2e` 解码成 `..` 后
        # `Pack(root, "..")` 成功命中 `root/pack.yaml`，接口回 200 并附带
        # `base.rglob("*")` 的**整棵目录树清单**（含 config.yaml 与各包 private/ 的
        # 文件名与体积）。实测复现：GET /api/packs/%2e%2e → 200 + 25 条文件清单。
        # `_safe_name` 的 `..` 与 `/` 检查正好堵住它（`%2f` 因路由不匹配进不来，
        # 但 `%2e%2e` 是单段，能进来）。
        name = _safe_name(name)
        pack = Pack(root_for_packs, name)        # 不存在 → PackError → 404
        base = packs_root / name
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

    @app.get("/api/packs/{name}/file")
    def pack_file(name: str, rel: str = ""):
        """读行业包内单个文件的内容（知识库面板的只读查看器用）。

        四道闸都必须有：
          1. 包名走 `_safe_name`、`rel` 解析后必须仍在包目录内 ——
             `rel=../../config.yaml` 会把含明文 API Key 的配置读出去；
          2. `private/` 一律拒绝（见下面的说明）；
          3. 后缀白名单 —— 包里可能有图片/字体，读出来是一堆乱码不说，
             直接塞进 <pre> 还可能带出不可见字符；
          4. 体积上限 —— 包目录理论上可以放任意大文件，全量读进内存没必要。

        为什么单独挡 `private/`（缺陷 4）
        --------------------------------
        安装包刻意**不携带** `packs/*/private/**`（`desktop/package.json` 的
        extraResources 排除规则，`tests/test_runtime_requirements.py` 钉着）。
        这个端点原先却是
        200 明文返回 `private/products.yaml` —— 一条数据「不许出厂」却又「随时可
        经 HTTP 读走」，两者只能留一个。选择关这个口子而不是放宽打包规则，理由：
          · 界面**从来不需要**它的内容才能工作：包详情把文件列出来（只有名字与
            体积）已经足够定位，正文要读就打开本地文件读 —— 这个项目里改包内容
            本来就是「开编辑器 + Ctrl+S + 回来点刷新」（见 settings.js 顶部说明），
            只读预览对 private/ 只是少一个便利，泄露的代价却是产品型号 / 报价 /
            客户案例整份外流；
          · 拿到令牌的客户端**不止**我们自己的界面：令牌每次启动经 URL 交给页面，
            任何一处渲染层 XSS、任何一次日志里漏出令牌，都会把 private/ 全量带走。
            同源判定与令牌是同一套凭证，挡不住"凭证被用掉"这一类；
          · 关掉不需要新配置项也不需要开关 —— 留一个界面能传的 `?include_private=`
            等于没关（那个参数本身就是攻击者能猜到的字符串）。

        返回 403 而不是 404：文件**确实在那儿**，包详情也列出了它；
        给 404 会诱导用户去"修好"一个其实没坏的东西（本项目最忌讳的误导）。
        detail 里带上本地路径，让界面可以直接照抄成一句「请用编辑器打开 …」。
        """
        name = _safe_name(name)
        Pack(root_for_packs, name)               # 不存在 → PackError → 404
        base = (packs_root / name).resolve()
        try:
            target = (base / rel).resolve()
        except Exception:                      # noqa: BLE001
            raise HTTPException(400, "文件路径不合法")
        if "__pycache__" in target.parts or not target.is_relative_to(base):
            raise HTTPException(404, "文件不存在")
        if _is_private_rel(target.relative_to(base).as_posix()):
            raise HTTPException(403, f"私有资料不经界面浏览（{base / 'private'} 下的内容）。"
                                    "要查看或修改，请直接用编辑器打开本地文件。")
        if not target.is_file():
            raise HTTPException(404, "文件不存在")
        if target.suffix.lower() not in _TEXT_SUFFIXES:
            raise HTTPException(415, f"不支持预览该类型文件：{target.suffix}")
        size = target.stat().st_size
        if size > _MAX_FILE_BYTES:
            raise HTTPException(413, f"文件过大（{size} 字节），暂不支持预览")
        return {"rel": target.relative_to(base).as_posix(), "size": size,
                "text": target.read_text(encoding="utf-8")}

    @app.post("/api/packs/create")
    def packs_create(req: PackCreateRequest):
        """新建行业包 = 后台作业（P1-43）。返回 `{job_id}`，进度看 `/api/jobs/{id}`。

        修复前这里是**唯一一个挂在同步长请求上的耗时操作**：一次 1~2 分钟的模型
        调用，断连就失明（客户端不知道包建没建出来），「取消」只是不要返回值而
        已 —— 钱与目录照旧发生，而且它不占并发额度。
        现在与生成同一套通道：额度、取消、重试提示、思考流、失败原因都在作业上。
        """
        cfg = _cfg()
        _require_model(cfg)
        try:
            return {"job_id": pipeline.start_packgen(req.industry, req.description)}
        except FileExistsError as e:
            # 同名包 / 同名 slug 正在创建中（P2-46）—— 都是「换个名字或等一下再试」，
            # 一分钱没花就能告诉用户，不该开一个必然失败的作业。
            raise HTTPException(409, str(e))
        except ValueError as e:
            raise HTTPException(400, str(e))
        # StateConflict（额度满）由全局处理器映射成 409，与 /api/generate 同口径。
        # LLMError 不再出现在这里：它发生在作业线程里，界面在作业失败横幅上看到它。

    @app.post("/api/packs/import")
    async def packs_import(file: UploadFile = File(...)):
        """导入第三方技能包（zip）→ `packs/<name>/`。

        方案 `docs/技能包系统方案.md` §4。**这是唯一把不可信内容放进用户
        数据目录的入口**，所以校验全在 `app/packimport.py`（zip 层/结构层/
        冲突层/落盘层四道），这里只做「读文件 → 调导入 → 翻译错误」。

        ⚠ 上传的文件先整个读进内存再校验：导入前拦不住流式攻击，而
        MAX_ZIP_BYTES 上限（20MB）让内存代价有界。文件超大在 read 时就该
        拒（UploadFile 有 .size 属性但不总可靠，读完用 len 兜底判一次）。
        """
        try:
            data = await file.read()
        except Exception as e:                       # noqa: BLE001
            raise HTTPException(400, f"上传读不出来：{e}")
        try:
            r = import_pack(data, packs_root)
        except PackImportError as e:
            # 全部是人话原因（zip 坏/类型不符/逃逸/改过的包…），原样给用户。
            raise HTTPException(400, str(e))
        except OSError as e:
            raise HTTPException(500, f"导入过程中文件系统出错：{e}")
        return r.__dict__

    @app.delete("/api/packs/{name}")
    def packs_delete(name: str):
        """卸载**导入的**包。内置播种包拒绝删（删了下周播种又回来）。"""
        try:
            return delete_pack(name, packs_root)
        except PackImportError as e:
            raise HTTPException(400, str(e))

    @app.post("/api/packs/{name}/undraft")
    def packs_undraft(name: str):
        """人工校对完成后把 `draft` 改为 false —— **就地改那一行**。

        原实现是 `yaml.safe_load` → 改一个键 → `yaml.safe_dump` 整体写回，
        代价是把 pack.yaml 的**全部注释**永久抹掉（PyYAML 不保留注释）。
        这些注释是包作者的文档契约（elevator/pack.yaml 有 12 行：字段含义、
        "分享给同事 = 复制目录（不含 private/ 即不带商业信息）"这类约定），
        而触发点只是界面上一个「标记为已校对」的按钮 —— 一次点击、不可逆、
        还在响应里回 `{"ok": true}`。详见 `undraft_yaml_text`。
        """
        _safe_name(name)
        p = packs_root / name / "pack.yaml"
        if not p.exists():
            raise HTTPException(404, "行业包不存在")
        try:
            text = p.read_text(encoding="utf-8")
        except OSError as e:
            raise HTTPException(500, f"pack.yaml 读取失败：{e}")
        new = undraft_yaml_text(text)
        # 写之前先确认改完**仍然是合法 YAML 且 draft 真的是 false**。
        # 不这么做的后果是：一个键名写法出乎意料（例如整份文件是一个列表、
        # 或 draft 出现在 flow 风格里）时会写出一个坏包 —— 坏 pack.yaml 会让
        # 这个行业的参数条、词表、私有资料全部退回默认（knowledge.Pack 为此
        # 专门抛 PackBrokenError）。宁可报 500 也不写坏。
        try:
            check = yaml.safe_load(new)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(500, f"pack.yaml 本来就不是合法 YAML，未做任何修改：{e}")
        if not isinstance(check, dict) or check.get("draft") is not False:
            raise HTTPException(500, "pack.yaml 结构异常（改完 draft 仍不是 false），"
                                     "已放弃修改，请手工编辑该文件")
        if new != text:
            write_atomic(p, new)
        return {"ok": True, "name": name, "draft": False}

    # ── 脚本生成 ────────────────────────────────────────────
    @app.post("/api/generate")
    def generate(req: GenerateRequest):
        # 入参校验排在「有没有配模型」之前：非法包名跟环境状态无关，
        # 放在后面会让同一个错误请求因为用户配没配 Key 而返回不同内容。
        # 包名过 `_safe_name`：其余按名字取包的端点（pack_detail / pack_file /
        # undraft）都调了，只有这个走请求体的入口漏了 ——
        # 于是 `{"pack":"../../.."}` 能把 packs 之外的目录当包加载。
        # knowledge.Pack 里还有一层不变式，这层负责给出 400 而不是 404。
        _safe_name(req.pack)
        cfg = _cfg()
        _require_model(cfg)
        pipeline.reload_llm()          # 用最新的 Key/模型，且不影响正在跑的作业
        return {"job_id": pipeline.start_generate(req)}

    @app.get("/api/jobs/{jid}")
    def job_snapshot(jid: str, full: bool = False):
        try:
            job = pipeline.get_job(jid)
        except KeyError:
            raise HTTPException(404, "作业不存在或已随重启释放")
        return job.snapshot(include_result=full)

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
        （前端生成期间每 900ms 调一次 —— 轮询间隔见 jobs.js 的 POLL_MS），
        而且失败作业没有 result.json、只写了 job.json 却从未被读取 ——
        重启后从历史里彻底消失。现在摘要来自 store 的索引（只有几个字段），
        失败记录也在里面。
        """
        items = pipeline.store.history(limit=100)
        known = {x["id"] for x in items}
        skip = {"done", "cancelled"}
        running = []
        for snap in pipeline.snapshot_jobs(include_result=False):
            if snap["state"] in skip or snap["id"] in known:
                continue
            if snap.get("kind") != "generate":
                # 建包作业不是一条脚本：混进左栏会话列表只会多出一行点不开的记录
                # （它没有产物、没有主题、没有行业包），它的位置是设置页里的进度。
                continue
            p = snap.get("params") or {}
            running.append({
                "id": snap["id"], "created_at": snap["created_at"],
                "pack": p.get("pack", ""), "topic": p.get("topic", ""),
                # platform 必须一并带上：会话列表副标题现在显示「行业 · 时间 · 平台」，
                # 漏了它，**正在生成的那条**会缺一格 —— 而它与「用户没选平台」
                # 在界面上长得一模一样（静默降级）。store 的两条摘要路径已带上，
                # 这条是第三处构造点，容易漏。
                "platform": p.get("platform"),
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

    # ── 情报（今日选题）─────────────────────────────────────
    #
    # 三个端点，口径逐条对到 `需求方案 §2.2` 与 README 的 B3/B4：
    #   GET  /api/intel/today   只读本地文件（**空或坏返回空结构，不抛**）
    #   POST /api/intel/refresh 立即重抓（走 Job 管道 + 独立并发额度）
    #   POST /api/intel/ignore  忽略一条（**只影响今天**）
    def _intel_sources(name: str) -> list:
        """读包的源声明 —— 走 `pack_info` 而不是 `Pack()`。

        刻意的选择：`Pack()` 对坏包抛 `PackBrokenError`（409），而**情报不该
        因为 pack.yaml 写坏了就看不了** —— 那两件事互不相干，而且
        "包坏了 → 选题页也白屏"会让用户以为两个功能一起坏了。
        `pack_info` 保证不抛，坏包退成空源列表（= 这个包没盯任何源），
        与「包里没配 intel_sources」是同一种表现，界面上的说法也一样。
        """
        info = next((p for p in list_packs(root_for_packs) if p.name == name), None)
        return [_IntelSourceDC(**s.model_dump()) for s in (info.intel_sources if info else [])]

    @app.get("/api/intel/today")
    def intel_today_api(pack: str = "elevator"):
        """只读：返回最近一次抓取的分组 / 条目 / 计数 / 是否该补抓。

        **空或坏返回空结构，不抛**（B3）。⚠ 这与 `Pack.private_facts()` 的失败
        语义**相反** —— 那边读不到要中止生成（否则模型会编事实），这边读不到
        只是"今天没有选题可看"。**这条差异别顺手统一。**
        """
        name = _safe_name(pack)
        return intel_today(data_dir, name, _intel_sources(name))

    @app.post("/api/intel/refresh")
    def intel_refresh(req: IntelPackRequest):
        """立即重抓（顶栏那颗「重抓」/ 懒触发的补跑）。

        懒触发本身**不在这里**：`/api/intel/today` 只回 `stale: true/false`，
        由渲染层决定要不要发这一发 POST。GET 不带副作用 —— 一个会自己起作业的
        GET 在轮询/预取/刷新时会被重复触发，而每次都是一轮真实网络请求。
        """
        name = _safe_name(req.pack)
        # 包不存在 → 404；坏包 → 409（与生成同口径，由全局处理器映射）
        return {"job_id": pipeline.start_intel_fetch(name)}

    @app.post("/api/intel/ignore")
    def intel_ignore(req: IntelIgnoreRequest):
        """忽略一条选题。**只影响今天**：明天同题还会回来，连续 3 天被忽略才沉底。

        所以落盘的是 `{key: 最后一次忽略的日期}` —— 界面按"连续几天"决定沉底，
        而"连续"这件事只能靠日期算，记一个布尔是算不出来的。
        """
        name = _safe_name(req.pack)
        rec = add_ignored(data_dir, name, req.key)
        return {"ok": True, "ignored": len(rec)}

    # ── 设置 ────────────────────────────────────────────────
    @app.get("/api/config")
    def get_config():
        cfg = _cfg()
        return {"base_url": cfg.llm.base_url, "model": cfg.llm.model,
                "api_key_set": bool(cfg.llm.api_key), "temperature": cfg.llm.temperature,
                "retries": cfg.llm.retries, "timeout": cfg.llm.timeout,
                "max_tokens": cfg.llm.max_tokens, "mock": cfg.mock,
                # 模型列表（**已剥掉 api_key 明文**）与当前生效的那条。
                "models": public_models(cfg),
                "active_model": cfg.active_model,
                # 读坏 config.yaml 时非空：上面这些字段全是**内置默认值**，
                # 不是用户存过的那份。不下发这个，界面就会把默认值当用户配置
                # 显示出来（「已配置 Key · 模型 glm-4.7」），用户完全看不出
                # 自己填的 base_url 其实没生效 —— 又一处静默降级。
                "config_error": cfg.config_error,
                # 哪些字段还是内置默认（文件里没写、环境里也没有）。与 config_error
                # 是同一类信号的两个来源：一个是「你的配置读坏了」，一个是「你还没配」。
                # 少了它，界面会把内置默认当用户配置显示（模型名写着 glm-4.7）。
                "llm_defaulted": cfg.llm_defaulted,
                # 内置默认的连接信息。渲染层「恢复默认」按钮要用它把默认值**显式**
                # 填进输入框 —— 不在这里下发，前端就只能自己抄一份默认地址，
                # 于是改后端默认值时界面还按老值填（同一信息两份表示的经典后果）。
                "defaults": {"base_url": DEFAULT_MODEL["base_url"],
                             "model": DEFAULT_MODEL["model"]},
                # 地址的「合法但不体面」问题（明文 http 打到远端等）。
                # 常驻下发而不是只在保存那次回一句：见 `_link_warnings`。
                "base_url_warnings": _link_warnings(cfg),
                "env_override": bool(os.environ.get("TALKSCRIPT_API_KEY"))}

    @app.post("/api/config")
    def set_config(body: ConfigIn):
        # 地址校验排在**所有写入之前**：数值项先落盘、地址后报错的话，
        # 这一次保存就成了「一半生效」，而界面只会显示一句 400。
        if body.base_url not in ("", None):
            try:
                link_warnings = check_base_url(str(body.base_url))
            except BaseUrlError as e:
                raise HTTPException(400, str(e))
        else:
            link_warnings = []
        # 数值项必须在这里卡边界：越界的 0 / 负数一旦写进 config.yaml，
        # `load_config` 是**照单全收**的（P0-2 之后 `_num` 只在键缺失或值为空时
        # 才取默认，不再把 0 当「没填」），于是界面显示保存成功、实际值就是那个
        # 越界值 —— 更难查。
        #
        # temperature 曾经是唯一漏网的一个：999 与 -5 都能存进 config.yaml，
        # 之后**每一次生成**都带着这个越界值去请求模型，上游多半回 400，
        # 用户看到的是「模型接口返回 400」，而根因是几天前存下的一个错数字。
        # 界面上那个 input 的 min/max 是纯客户端约束，绕开它只要一条 curl。
        updates = {k: v for k, v in body.model_dump().items()
                   if k in NUMERIC_BOUNDS and v not in ("", None)}
        for k, (lo, hi) in NUMERIC_BOUNDS.items():
            v = updates.get(k)
            if v is None:
                continue
            if not (lo <= float(v) <= hi):
                raise HTTPException(400, f"{k} 需在 {lo} ~ {hi} 之间，当前为 {v}")
        save_config(root, updates, config_dir=data_dir)

        # 连接信息（base_url / api_key / model）现在住在**当前模型条目**里。
        # 这个单模型时代的写入口保留下来（curl、老渲染层还在用），但它改的是
        # **同一份数据**，不是第二份 —— 两处各存一份必然有一天只改到一处。
        # 空串 = 不覆盖（防手滑清空），api_key 同理（合并语义）。
        link = {k: v for k, v in (("base_url", body.base_url), ("api_key", body.api_key),
                                  ("model", body.model)) if v not in ("", None)}
        if link:
            raw, active = load_raw_models(root, data_dir)
            if not raw:
                # 一条模型都没有（2026-09-17 起全新安装不再预置条目）。
                # 旧入口仍要能用：自动建一条，把这次写的连接信息装进去 ——
                # 不能 `next(...)` 硬取，那会 StopIteration 冒泡成 500。
                active = "m1"
                raw = [{"id": active, "name": "", "base_url": "",
                        "api_key": "", "model": ""}]
            elif not any(x["id"] == active for x in raw):
                # 有模型但**一条都没启用**：界面上的开关能关掉（2026-09-17），
                # 关掉后 `load_raw_models` 原样返回 (非空列表, "")。
                # 上面那条 `if raw` 守卫只挡住了"列表为空"，挡不住这个空串，
                # 于是 `next(...)` 在这里抛 StopIteration → 500。
                # 这里也不替用户猜一个：连接信息该写进哪一条是用户的事，
                # 悄悄建第四条或写进第一条都是替他做决定。给可行动的提示。
                raise HTTPException(400, "没有启用中的模型：请先在「模型接口」里启用一条，或新建一条")
            it = next(x for x in raw if x["id"] == active)
            if "base_url" in link:
                it["base_url"] = str(link["base_url"]).rstrip("/")
            if "model" in link:
                it["model"] = str(link["model"])
            if "api_key" in link:
                it["api_key"] = str(link["api_key"])
            save_models(root, raw, active, config_dir=data_dir)
        # warnings：界面据此在地址那一行挂一条黄字。回 200 是诚实的 ——
        # 值存下了、能用，只是不安全；不安全到必须让用户每次看得见。
        return {"ok": True, "warnings": link_warnings}

    @app.post("/api/config/reset")
    def reset_config(body: ConfigResetIn):
        """把指定字段清空回默认值。

        存在的原因：`set_config` 会过滤掉空串（防手滑清空），副作用是
        base_url / model 一旦填错就再也改不回去 —— 用户只能去手工改 config.yaml。
        所以「回到默认」必须是**显式**动作，而不是靠留空输入框。

        实现上只是把字段写成空串：空的 base_url / model 走
        `DEFAULT_MODEL` 兜底，数值项走 `_num()`（它对空串同样取默认），
        空串自然落回默认值。api_key 不在可重置名单里 —— 清空密钥不该这么顺手。

        ⚠ 两类字段住在**两个地方**（连接信息在模型条目、数值项在 llm 段），
        所以必须按字段分派。一个循环写完会漏掉一类，而漏掉的那类会表现为
        「点了恢复默认没反应」—— 静默无效。
        """
        bad = [f for f in body.fields if f not in RESETTABLE_FIELDS]
        if bad:
            raise HTTPException(400, f"不支持重置的字段：{'、'.join(bad)}")
        num_fields = [f for f in body.fields if f in NUMERIC_BOUNDS]
        link_fields = [f for f in body.fields if f in ("base_url", "model")]
        if num_fields:
            save_config(root, {f: "" for f in num_fields}, config_dir=data_dir)
        if link_fields:
            raw, active = load_raw_models(root, data_dir)
            # 一条模型都没有、或有模型但**没启用任何一条**（开关能关掉）→
            # 都没有"当前模型的连接字段"可重置，静默 no-op。
            # 原来只判 `if raw:`，空串 active 会让下面的 `next(...)` 抛
            # StopIteration 冒成 500 —— 注释写着"不能 next 硬取"却只修了一半。
            target = next((x for x in raw if x["id"] == active), None) if active else None
            if target is not None:
                for f in link_fields:
                    target[f] = ""
                save_models(root, raw, active, config_dir=data_dir)
        return {"ok": True, "fields": body.fields}

    # ── 模型列表（增删改 + 切换当前）────────────────────────
    @app.post("/api/models")
    def upsert_model(body: ModelIn):
        """添加（`id` 为空）或编辑（`id` 非空）一条模型。"""
        raw, active = load_raw_models(root, data_dir)
        mid = body.id.strip()
        model = body.model.strip()
        base_url = body.base_url.strip().rstrip("/")
        if not model:
            raise HTTPException(400, "模型 ID 不能为空")
        # 地址校验（缺陷 5）。原来这里只看 `startswith(("http://","https://"))`，
        # 于是 `file:///C:/x`、`http://user:pass@host`（httpx 会替你把凭据变成
        # Basic 头，设置页上看不见）、带空格/换行的粘贴内容都能存进去。
        # 分工：**存了必然出错**的拒；**存了能用但不体面**的（明文 http 打到
        # 远端）只警告并随响应回显 —— 本机 Ollama / 内网网关的用户正在用 http，
        # 硬拦等于把已经在工作的配置改坏。
        url_warnings: list[str] = []
        if base_url:
            try:
                url_warnings = check_base_url(base_url)
            except BaseUrlError as e:
                raise HTTPException(400, str(e))

        if mid:
            it = next((x for x in raw if x["id"] == mid), None)
            if it is None:
                raise HTTPException(404, f"没有这个模型：{mid}")
        else:
            # 新增时请求地址**必填**（2026-09-17）。不再有「内置默认地址」兜底
            # 的语义可以借：留空会被静默填成智谱的 —— 用户加一条 DeepSeek 模型
            # 却指向智谱，报错要到生成时才出现（「未配置与已配置长得一样」的老坑）。
            # 编辑时留空仍是「保持不变」，那是另一个语义，见下面。
            if not base_url:
                raise HTTPException(400, "请填写请求地址，例如 https://api.deepseek.com/v1")
            used = {x["id"] for x in raw}
            n = 1
            while f"m{n}" in used:
                n += 1
            mid = f"m{n}"
            it = {"id": mid, "name": "", "base_url": "", "api_key": "", "model": ""}
            raw.append(it)
            # 原来一条都没有（或用户把开关全关了）→ 新建的这条**直接生效**，
            # 否则用户加完还得再去点一次开关 —— 多一步，且很容易漏
            #（漏了的后果是生成被拒，而列表上一切正常）。
            if not active:
                active = mid
        it["name"] = body.name.strip()
        it["model"] = model
        # 请求地址**留空 = 保持不变**（与 api_key 同一个语义）——
        # 只对**编辑**成立：新增时上面已经拦过必填。
        # 不能要求编辑时必填：编辑一条还没配过地址的模型时那个框本来就是空的，
        # 用户只改了展示名，却会因为「地址不能为空」被拒 —— 而他压根没动过地址。
        if base_url:
            it["base_url"] = base_url
        # 空 = 保持不变：编辑时那个框是空的，用户没重填就说明密钥不该动
        if body.api_key.strip():
            it["api_key"] = body.api_key.strip()
        save_models(root, raw, active, config_dir=data_dir)
        return {"ok": True, "id": mid, "models": public_models(_cfg()),
                "warnings": url_warnings}

    @app.post("/api/models/delete")
    def delete_model(body: ModelIdIn):
        """删掉一条模型。**允许删到一条不剩**（2026-09-17）。

        原来拦着「至少要保留一个模型」—— 那是「总有一条内置默认」时代的规则：
        删空了生成时取不到连接信息，而界面还会显示「已配置 Key」。
        现在模型是用户自己加的（不再有预置条目），删光就是「还没配」：
        界面显示空列表引导，生成前被 `_require_model` 明确拦住（说清"还没配置模型"）。
        留着这条拦反而让用户删不掉自己不想要的那条 —— 用户原话
        「没有内置默认的模型的，需要用户自己添加，添加完还需要支持删除」。
        """
        raw, active = load_raw_models(root, data_dir)
        left = [x for x in raw if x["id"] != body.id]
        if len(left) == len(raw):
            raise HTTPException(404, f"没有这个模型：{body.id}")
        if active == body.id or active not in {x["id"] for x in left}:
            # 删掉的正是当前模型（或 active 已悬空）→ 换成剩下的第一条；
            # 一条不剩时写空串，不留悬空引用。
            active = left[0]["id"] if left else ""
        save_models(root, left, active, config_dir=data_dir)
        return {"ok": True, "active_model": active, "models": public_models(_cfg())}

    @app.post("/api/models/activate")
    def activate_model(body: ModelIdIn):
        """切换当前生效的模型（输入区那个选择器调它）。

        **传空 id 表示「都不启用」**（2026-09-17）。原来空 id 会 404
        （"没有这个模型："），于是界面上的开关**关不掉** —— 点当前启用的那条
        没有任何反应，看起来像坏了（用户报「模型开启时无法关闭」）。
        但「关掉」是个合法诉求（想先停用、改完配置再启用）。
        关掉之后生成会被 `_require_model` 拦住并说清「当前没有启用任何模型」。

        ⚠ `save_models` 的 active 判据是 `is not None`，所以空串能存进文件；
        `_parse_models` 也把「键存在但空串」与「键不存在」分开处理 ——
        否则关掉会被静默退回第一条（关了又跳回来）。
        """
        raw, _ = load_raw_models(root, data_dir)
        if body.id and body.id not in {x["id"] for x in raw}:
            raise HTTPException(404, f"没有这个模型：{body.id}")
        save_models(root, raw, body.id, config_dir=data_dir)
        return {"ok": True, "active_model": body.id, "models": public_models(_cfg())}

    @app.post("/api/config/test")
    def test_config(body: ConfigIn | None = Body(default=None)):
        """用当前配置发一个最小请求，验证 Key / 地址 / 模型名是否可用。

        body 可选：传入界面上尚未保存的值做临时覆盖（Key 留空表示沿用已保存的），
        这样用户填完就能测，不必先点保存。
        """
        fresh = _cfg()
        test_warnings: list[str] = []
        if body:
            # 先落到「要测的那条模型」上：编辑一条**非当前**模型时，密钥框是空的，
            # 不指明 id 就会错拿当前模型的 Key 去测 —— 测出来的结果与用户以为的
            # 不是一回事，而界面会照常显示「连接正常」。
            if body.model_id:
                m = next((x for x in fresh.models if x.id == body.model_id), None)
                if m:
                    fresh.llm.base_url = m.base_url
                    fresh.llm.api_key = m.api_key
                    fresh.llm.model = m.model
            if body.base_url:
                try:
                    test_warnings = check_base_url(str(body.base_url))
                except BaseUrlError as e:
                    raise HTTPException(400, str(e))
                fresh.llm.base_url = str(body.base_url).rstrip("/")
            if body.api_key:
                fresh.llm.api_key = str(body.api_key)
            if body.model:
                fresh.llm.model = str(body.model)
        if not (fresh.llm.api_key or fresh.mock):
            raise HTTPException(400, "未配置模型 API Key，请先填写")
        if not test_warnings:
            # 「测试连接」没带地址覆盖时，测的就是存着的那条 —— 警告同样要说，
            # 否则用户点完只看到「连接正常」，永远不知道该把它换成 https。
            test_warnings = _link_warnings(fresh)
        ok, detail = LLMClient(fresh.llm, mock=fresh.mock).ping()
        return {"ok": ok, "detail": detail, "model": fresh.llm.model,
                "base_url": fresh.llm.base_url, "warnings": test_warnings}

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


def parent_alive(pid: int) -> bool:
    """父进程还活着吗。看门狗用它决定要不要自杀。

    不能依赖 psutil —— 运行时依赖只有 fastapi / httpx / pydantic / uvicorn / pyyaml。

    ⚠ Windows 上**不能**用 `os.kill(pid, 0)` 探活：signal 0 在 Windows 的模拟实现里
    走的不是"发 0 号信号"，而是 `TerminateProcess`，实测会把目标直接干掉 ——
    也就是"探测父进程是否活着"这个动作本身会杀掉 Electron。
    所以这里用 OpenProcess + GetExitCodeProcess。
    """
    if pid <= 0:
        return True                       # 没传 = 不启用看门狗（手动起引擎调试的场景）
    if os.name == "nt":
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return False                  # 打不开 = 进程已不存在（父进程必属同一用户，不涉及权限）
        try:
            code = ctypes.c_ulong()
            if not k32.GetExitCodeProcess(h, ctypes.byref(code)):
                return False
            return code.value == STILL_ACTIVE
        finally:
            k32.CloseHandle(h)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True                       # 活着但没权限看，宁可不误杀
    except OSError:
        return False
    return True


def watch_parent(pid: int, interval: float = 2.0) -> None:
    """父进程（Electron 主进程）一没就自杀，别留孤儿引擎。

    实测：主进程被强杀或崩溃时，Windows 上子进程不会跟着走。多崩几次就有几份
    引擎常驻，每份占一个端口 + 一份内存；更糟的是下一次启动的健康检查可能被
    **旧引擎**应答（它同样在 127.0.0.1 上回 200），于是界面连到的是一个
    拿着旧 token / 旧配置的僵尸进程。

    用 `os._exit` 而不是优雅停机：拿不到 uvicorn 的 server 实例（`uvicorn.run`
    内部自建），而窗口都没了，等不到"当前作业写完"。产物落盘本来就是原子的
    （`fileio.write_atomic`），半截文件不会留在盘上。
    """
    def loop() -> None:
        while True:
            time.sleep(interval)
            if not parent_alive(pid):
                print("[engine] 主进程已退出，引擎自行关闭", file=sys.stderr, flush=True)
                os._exit(0)

    threading.Thread(target=loop, name="parent-watchdog", daemon=True).start()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--root", default=".", help="项目根目录（含 packs/）")
    ap.add_argument("--data-dir", default=None,
                    help="可写数据目录（config.yaml 与 generated/）；默认同 --root。"
                         "打包后安装目录通常不可写，由 Electron 指到用户数据目录。")
    ap.add_argument("--packs-dir", default=None,
                    help="可写行业包目录（打包版 = %%APPDATA%%\\TalkScript\\packs）。"
                         "默认同 --root/packs（开发态）。给了它就把 --root/packs 当只读种子。")
    ap.add_argument("--token", default=None,
                    help="访问令牌。**只给手工起引擎时用**：Electron 改走环境变量 "
                         f"{TOKEN_ENV}（命令行会被同机任何进程读到，见 desktop/main.js）。"
                         "两者都不给则随机生成。")
    ap.add_argument("--version", default=None,
                    help="版本号（打包版由 Electron 传入；不传则读 desktop/package.json）")
    ap.add_argument("--open", action="store_true", help="启动后自动打开浏览器")
    ap.add_argument("--parent-pid", type=int, default=0,
                    help="Electron 主进程 PID；它一退出引擎就自行关闭（不留孤儿引擎）。"
                         "不传则不启用看门狗 —— 命令行手动起引擎调试时正是要这样。")
    args = ap.parse_args()

    import uvicorn
    root = Path(args.root).resolve()
    data_dir = Path(args.data_dir).resolve() if args.data_dir else None
    packs_dir = Path(args.packs_dir).resolve() if args.packs_dir else None
    # 令牌来源的优先级：显式 --token > 环境变量 > 随机。
    # 环境变量这条是**唯一**Electron 用的通道（缺陷 1）：Windows 上任何本机进程都能
    # 用 `wmic process get commandline` / Get-CimInstance 读到别人的命令行，
    # 于是原来的 `--token <值>` 等于把访问令牌贴在了进程表里。拿到令牌的进程
    # 可以把 base_url 改成自己的服务器（POST /api/config），下一次生成就会把
    # `Bearer <API Key>` 连同整段私有资料一起发过去。
    # 子进程的环境块只能被同一用户的进程（且通常需调试权限）读到，比 argv 高一个量级。
    token = args.token or os.environ.get(TOKEN_ENV) or new_token()
    app = create_app(root, token=token, data_dir=data_dir,
                     bind_host=args.host, version=args.version,
                     packs_dir=packs_dir)
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
    # 看门狗要在进入阻塞的 run() 之前起，且晚于端口绑定前的任何准备工作。
    if args.parent_pid:
        watch_parent(args.parent_pid)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
