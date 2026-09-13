# -*- coding: utf-8 -*-
"""本地访问控制：一次性令牌 + 同源判定。

背景（修复前的问题）
--------------------
引擎原来把 CORS 放行集写成 `{"*"}` 的等价物，后来收紧为「本机 host 一律放行」，
并且**刻意放行了 `null` / `file://` / `app://` / `chrome-extension://`**——
理由是渲染层当时用 `file://` 加载，Chromium 对 file:// 文档统一上报 `Origin: null`。

问题是这条放行规则对**任何**本机网页同样成立：用户下载并打开的任意 .html、
任何已安装的浏览器扩展，都能悄悄 `GET /api/history` 读走全部脚本、
`DELETE /api/history/{id}` 删记录、`POST /api/config` 把 base_url 改到攻击站点
（下一次生成就把 `Bearer <api_key>` 送出去）。引擎被 Electron 拉起后常驻，
这不是理论风险。

现在的做法
----------
1. 渲染层不再用 `file://` 加载，改由引擎同源提供（`main.js` 用 loadURL），
   于是「跨站」这个概念重新变得清晰：**只有引擎自己的 origin 是合法的**。
2. `/api/*` 一律要求 `X-TalkScript-Token`；令牌每次启动随机生成，经 URL 传给渲染层。
   跨站请求即使绕过同源判定，也拿不到令牌。
3. 令牌比较用 `hmac.compare_digest`，避免时序侧信道。
"""
from __future__ import annotations

import hmac
import secrets
from urllib.parse import urlparse

TOKEN_HEADER = "X-TalkScript-Token"

# 引擎自己的主机名。**必须限制在这个集合里**，否则会有 DNS rebinding 缺口：
# 攻击者把 evil.com 解析到 127.0.0.1，浏览器就会带着
# `Host: evil.com:8765` + `Origin: http://evil.com:8765` 打到引擎上 ——
# 而「Origin 等于 Host」这条规则对它是成立的，会被放行。
# 加上「Host 必须是本机回环名」这道判断，rebinding 直接失效。
# （令牌仍然是第二道；两层都不依赖对方。）
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def allowed_hostnames(bind_host: str | None = None) -> frozenset[str]:
    """允许出现在 Host 里的主机名：回环名 + 显式绑定的那个地址。

    绑定 0.0.0.0 / 局域网 IP 是使用者的显式选择（`--host`），此时把该地址
    也加进来，否则从局域网访问会连界面都打不开。
    """
    hosts = set(LOOPBACK_HOSTS)
    if bind_host:
        h = str(bind_host).strip().strip("[]").lower()
        if h and h not in ("0.0.0.0", "::"):
            hosts.add(h)
    return frozenset(hosts)


def new_token() -> str:
    return secrets.token_urlsafe(24)


def token_ok(expected: str, provided: str | None) -> bool:
    """常数时间比较；expected 为空视为「不启用鉴权」以外的一切情况都拒绝。"""
    if not expected or not provided:
        return False
    return hmac.compare_digest(expected, provided)


def self_origins(host_header: str | None) -> set[str]:
    """由请求自己的 Host 头推出「引擎自己的 origin」集合。

    用 Host 而不是启动时记下的端口，是因为引擎可能被 uvicorn 以任意
    host/port 挂起（开发态 8765、Electron 态随机端口、测试态 testserver），
    而浏览器发出的 Origin 一定与它请求的 Host 一致。
    """
    host = (host_header or "").strip().lower()
    if not host:
        return set()
    return {f"http://{host}", f"https://{host}"}


def origin_allowed(origin: str | None, host_header: str | None,
                   hosts: frozenset[str] | None = None) -> bool:
    """同源判定：Origin 必须是本机回环（或显式绑定的地址），且与请求的 Host 一致。

    - 无 Origin 头：放行。这不是浏览器发起的跨站请求（curl / 非浏览器客户端 /
      同源 GET 通常不带 Origin），且后面还有令牌这一道。
    - 有 Origin：主机名必须在允许集合里（挡 DNS rebinding），
      并且 Origin 与 Host 完全相同（挡别的站点 / 换端口的本机页面）。
      `null`、`file://`、`chrome-extension://` 一律拒绝 —— 这正是修复前被放行的集合。
    """
    if origin is None:
        return True
    o = origin.strip().rstrip("/")
    if not o:
        return True
    if o.lower() in ("null", "file:", "file://"):
        return False
    try:
        u = urlparse(o)
    except ValueError:
        return False
    if u.scheme not in ("http", "https"):
        return False
    if (u.hostname or "").lower() not in (hosts or LOOPBACK_HOSTS):
        return False
    return o.lower() in self_origins(host_header)
