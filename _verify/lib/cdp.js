// 零依赖 CDP 客户端 + 静态服务。
//
// 从 verify.js 里抽出来：原来 1084 行的单文件里，协议管道（手写 WebSocket 分帧、
// 进程树回收、空闲端口探测）与断言混在一起，改断言要在一堆 buffer 位运算里找位置。
//
// 用法：
//   const { freePort, launchChrome, connect, killTree, serve } = require("./lib/cdp");
"use strict";
const http = require("http");
const net = require("net");
const fs = require("fs");
const path = require("path");
const { spawn, execSync } = require("child_process");

const CHROME = process.env.CHROME ||
  "C:/Program Files/Google/Chrome/Application/chrome.exe";

/** 取空闲端口：首选端口被占则交给系统分配 */
function freePort(preferred) {
  return new Promise(resolve => {
    const probe = net.createServer();
    probe.once("error", () => {
      const s = net.createServer();
      s.once("error", () => resolve(0));
      s.listen(0, "127.0.0.1", () => { const p = s.address().port; s.close(() => resolve(p)); });
    });
    probe.listen(preferred, "127.0.0.1", () => {
      const p = probe.address().port;
      probe.close(() => resolve(p));
    });
  });
}

/** 按进程树强杀：child.kill() 只杀父进程，浏览器会留一堆子进程继续占着端口 */
function killTree(pid) {
  if (!pid) return;
  try { execSync(`taskkill /PID ${pid} /T /F`, { stdio: "ignore" }); } catch (_) { /* 已退出 */ }
}

function launchChrome(cdpPort, userDataDir) {
  return spawn(CHROME, [
    `--remote-debugging-port=${cdpPort}`,
    `--user-data-dir=${userDataDir}`,
    "--headless=new",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-extensions",
    "--disable-gpu",
    "--window-size=1320,900",
    "about:blank",
  ], { stdio: "ignore" });
}

/** 等 CDP 端点可用，返回第一个 page target 的 ws 地址 */
async function waitTarget(cdpPort, timeoutMs = 20000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const r = await fetch(`http://127.0.0.1:${cdpPort}/json/list`);
      const list = await r.json();
      const page = list.find(t => t.type === "page" && t.webSocketDebuggerUrl);
      if (page) return page.webSocketDebuggerUrl;
    } catch (_) { /* 还没起来 */ }
    await new Promise(r => setTimeout(r, 200));
  }
  throw new Error("CDP 端点超时未就绪");
}

/** 手写 WebSocket 的 CDP 通道（无第三方依赖） */
function connect(wsUrl) {
  return new Promise((res, rej) => {
    const u = new (require("url").URL)(wsUrl);
    const key = Buffer.from("talkscript" + Date.now()).toString("base64");
    const sock = net.connect(Number(u.port), u.hostname, () => {
      sock.write(
        `GET ${u.pathname} HTTP/1.1\r\nHost: ${u.host}\r\n` +
        `Upgrade: websocket\r\nConnection: Upgrade\r\n` +
        `Sec-WebSocket-Key: ${key}\r\nSec-WebSocket-Version: 13\r\n\r\n`);
    });
    sock.once("error", rej);
    let buf = Buffer.alloc(0), upgraded = false, id = 0;
    const pending = new Map();
    const events = [];
    const listeners = [];
    const api = {
      send(method, params) {
        return new Promise((ok, no) => {
          const mid = ++id; pending.set(mid, { ok, no });
          const payload = Buffer.from(JSON.stringify({ id: mid, method, params }));
          const len = payload.length;
          let head;
          if (len < 126) head = Buffer.from([0x81, 0x80 | len]);
          else if (len < 65536) {
            head = Buffer.alloc(4); head[0] = 0x81; head[1] = 0xfe;
            head.writeUInt16BE(len, 2);
          } else {
            head = Buffer.alloc(10); head[0] = 0x81; head[1] = 0xff;
            head.writeBigUInt64BE(BigInt(len), 2);
          }
          const mask = Buffer.from([1, 2, 3, 4]);
          const masked = Buffer.from(payload);
          for (let i = 0; i < masked.length; i++) masked[i] ^= mask[i % 4];
          sock.write(Buffer.concat([head, mask, masked]));
        });
      },
      on(fn) { listeners.push(fn); },
      events,
      close() { sock.destroy(); },
    };
    sock.on("data", d => {
      buf = Buffer.concat([buf, d]);
      if (!upgraded) {
        const i = buf.indexOf("\r\n\r\n");
        if (i < 0) return;
        upgraded = true; buf = buf.slice(i + 4); res(api);
      }
      while (true) {
        if (buf.length < 2) return;
        let len = buf[1] & 0x7f, off = 2;
        if (len === 126) { if (buf.length < 4) return; len = buf.readUInt16BE(2); off = 4; }
        else if (len === 127) {
          if (buf.length < 10) return;
          len = Number(buf.readBigUInt64BE(2)); off = 10;
        }
        if (buf.length < off + len) return;
        const msg = buf.slice(off, off + len).toString();
        buf = buf.slice(off + len);
        let o;
        try { o = JSON.parse(msg); } catch (_) { continue; }
        if (o.id && pending.has(o.id)) {
          const { ok, no } = pending.get(o.id); pending.delete(o.id);
          o.error ? no(new Error(o.error.message)) : ok(o.result);
        } else if (o.method) {
          events.push(o);
          listeners.forEach(fn => fn(o));
        }
      }
    });
  });
}

const MIME = {
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".svg": "image/svg+xml",
};

/** 静态服务：把 / 与 /static/* 都映射到 renderer 目录（与引擎同构）。 */
function serve(rendererDir, port, { inject = "" } = {}) {
  return new Promise(res => {
    const s = http.createServer((req, rq) => {
      let p = decodeURIComponent(req.url.split("?")[0]);
      if (p === "/") p = "/index.html";
      if (p.startsWith("/static/")) p = p.slice("/static".length);
      const f = path.join(rendererDir, p);
      if (!f.startsWith(rendererDir)) { rq.writeHead(403); rq.end(); return; }
      fs.readFile(f, "utf8", (e, txt) => {
        if (e) { rq.writeHead(404); rq.end("404"); return; }
        if (p === "/index.html" && inject) {
          // 经典脚本在解析期立即执行，module 脚本是 deferred —— 所以桩一定先于 main.js
          txt = txt.replace('<script type="module"',
            `${inject}<script type="module"`);
        }
        rq.writeHead(200, { "Content-Type": MIME[path.extname(f)] || "application/octet-stream" });
        rq.end(txt);
      });
    });
    s.listen(port, "127.0.0.1", () => res(s));
  });
}

module.exports = { freePort, killTree, launchChrome, waitTarget, connect, serve, CHROME };
