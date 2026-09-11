// 针对性验证「行业包迁入设置」后的两件事：
//   A) 双入口（生成偏好的「详情」按钮 + 设置左导航「行业包」）不会重复拉详情
//   B) pi-close / pg-close 都能回到「生成偏好」分区
"use strict";
const path = require("path");
const { spawn } = require("child_process");

const DESKTOP = path.resolve(__dirname, "..", "desktop");
const ELECTRON = path.join(DESKTOP, "node_modules", "electron", "dist", "electron.exe");
const CDP_PORT = 9355;
const sleep = ms => new Promise(r => setTimeout(r, ms));

function connect(wsUrl) {
  return new Promise((res, rej) => {
    const net = require("net");
    const u = new (require("url").URL)(wsUrl);
    const sock = net.connect(Number(u.port), u.hostname, () => {
      sock.write(`GET ${u.pathname} HTTP/1.1\r\nHost: ${u.host}\r\nUpgrade: websocket\r\n` +
        `Connection: Upgrade\r\nSec-WebSocket-Key: ${Buffer.from("pack" + Date.now()).toString("base64")}\r\n` +
        `Sec-WebSocket-Version: 13\r\n\r\n`);
    });
    sock.once("error", rej);
    let buf = Buffer.alloc(0), up = false, id = 0;
    const pending = new Map();
    const api = {
      send(method, params) {
        return new Promise((ok, no) => {
          const mid = ++id; pending.set(mid, { ok, no });
          const p = Buffer.from(JSON.stringify({ id: mid, method, params }));
          const L = p.length; let hd;
          if (L < 126) hd = Buffer.from([0x81, 0x80 | L]);
          else if (L < 65536) { hd = Buffer.alloc(4); hd[0] = 0x81; hd[1] = 0xfe; hd.writeUInt16BE(L, 2); }
          else { hd = Buffer.alloc(10); hd[0] = 0x81; hd[1] = 0xff; hd.writeBigUInt64BE(BigInt(L), 2); }
          const mask = Buffer.from([3, 1, 4, 1]);
          const m = Buffer.from(p);
          for (let i = 0; i < m.length; i++) m[i] ^= mask[i % 4];
          sock.write(Buffer.concat([hd, mask, m]));
        });
      },
      close() { sock.destroy(); },
    };
    sock.on("data", d => {
      buf = Buffer.concat([buf, d]);
      if (!up) { const i = buf.indexOf("\r\n\r\n"); if (i < 0) return; up = true; buf = buf.slice(i + 4); res(api); }
      for (;;) {
        if (buf.length < 2) return;
        let L = buf[1] & 0x7f, off = 2;
        if (L === 126) { if (buf.length < 4) return; L = buf.readUInt16BE(2); off = 4; }
        else if (L === 127) { if (buf.length < 10) return; L = Number(buf.readBigUInt64BE(2)); off = 10; }
        if (buf.length < off + L) return;
        const msg = buf.slice(off, off + L).toString(); buf = buf.slice(off + L);
        try {
          const o = JSON.parse(msg);
          if (o.id && pending.has(o.id)) { const q = pending.get(o.id); pending.delete(o.id); o.error ? q.no(new Error(o.error.message)) : q.ok(o.result); }
        } catch (_) {}
      }
    });
  });
}

(async () => {
  const env = { ...process.env };
  delete env.ELECTRON_RUN_AS_NODE;          // 否则 electron 退化成纯 Node
  env.TALKSCRIPT_MOCK = "1";
  const app = spawn(ELECTRON, [".", `--remote-debugging-port=${CDP_PORT}`, "--disable-gpu", "--no-sandbox"],
    { cwd: DESKTOP, stdio: "ignore", env });

  let target = null;
  for (let i = 0; i < 60; i++) {
    try {
      const list = await (await fetch(`http://127.0.0.1:${CDP_PORT}/json/list`)).json();
      const t = list.find(x => x.type === "page" && x.webSocketDebuggerUrl);
      if (t) { target = t; break; }
    } catch (_) {}
    await sleep(500);
  }
  if (!target) { console.log("CDP 未就绪"); app.kill(); process.exit(2); }

  const cdp = await connect(target.webSocketDebuggerUrl);
  await cdp.send("Runtime.enable");
  await sleep(3500);

  const ev = async e => {
    const r = await cdp.send("Runtime.evaluate", { expression: e, returnByValue: true, awaitPromise: true });
    if (r.exceptionDetails) return { __err: r.exceptionDetails.text + " " + (r.exceptionDetails.exception?.description || "").slice(0, 200) };
    return r.result.value;
  };

  await ev(`window.__ERRS__=[];addEventListener('error',e=>__ERRS__.push(String(e.message)));
            addEventListener('unhandledrejection',e=>__ERRS__.push('rej:'+String(e.reason)));true`);

  console.log("boot:", await ev(`(document.getElementById('pack')||{options:[]}).options.length`), "个行业包");

  // 给 /api/packs/ 的详情请求装计数器
  await ev(`window.__PACKF__=[];
            if(!window.__wrapped__){
              window.__wrapped__=true;
              const of=window.fetch.bind(window);
              window.fetch=function(u,o){ const s=String(u);
                if(s.indexOf('/api/packs/')>=0 && !s.endsWith('/packs')) window.__PACKF__.push(s);
                return of(u,o); };
            } true`);

  const results = [];
  const push = (n, ok, d) => results.push([n, ok, d]);

  const pane = () => ev(`(() => {
    const p = document.querySelector('#settings-screen .stg-pane:not(.hidden)');
    return { pane: p ? p.id : null, calls: (window.__PACKF__||[]).length };
  })()`);
  const reset = () => ev(`window.__PACKF__=[]; true`);
  const openSettings = () => ev(`document.getElementById('btn-open-settings').click(); true`);

  // ── A1: 生成偏好面板里的「详情」按钮 ──
  await ev(`setSettingsPane('gen'); document.getElementById('btn-open-settings').click(); true`);
  await sleep(300);
  await reset();
  await ev(`document.getElementById('btn-packinfo').click(); true`);
  await sleep(900);
  const a1 = await pane();
  push("入口1：生成偏好「详情」→ packinfo", a1.pane === "pane-packinfo", a1.pane);
  push("入口1：详情请求恰好 1 次", a1.calls === 1, "calls=" + a1.calls);

  // ── A2: 设置左导航的「行业包」 ──
  await reset();
  await ev(`document.querySelector('.stg-nav-item[data-pane="packinfo"]').click(); true`);
  await sleep(900);
  const a2 = await pane();
  push("入口2：左导航「行业包」→ packinfo", a2.pane === "pane-packinfo", a2.pane);
  push("入口2：详情请求恰好 1 次", a2.calls === 1, "calls=" + a2.calls);

  // ── B: 各返回按钮回到 gen ──
  await ev(`document.getElementById('pi-close').click(); true`);
  await sleep(300);
  const b1 = await pane();
  push("pi-close → 回到生成偏好", b1.pane === "pane-gen", b1.pane);

  await ev(`document.querySelector('.stg-nav-item[data-pane="packgen"]').click(); true`);
  await sleep(300);
  const b2 = await pane();
  push("左导航「新建行业包」→ packgen", b2.pane === "pane-packgen", b2.pane);

  await ev(`document.getElementById('pg-close').click(); true`);
  await sleep(300);
  const b3 = await pane();
  push("pg-close → 回到生成偏好", b3.pane === "pane-gen", b3.pane);

  // ── C: 生成偏好的「+ 新建」按钮 ──
  await ev(`document.getElementById('btn-newpack').click(); true`);
  await sleep(300);
  const c1 = await pane();
  push("生成偏好「+ 新建」→ packgen", c1.pane === "pane-packgen", c1.pane);

  const errs = await ev(`(window.__ERRS__||[]).slice(0,8)`);
  push("全程无 JS 错误", Array.isArray(errs) && errs.length === 0, JSON.stringify(errs));

  console.log("\n════════ 双入口验证 ════════");
  let pass = 0;
  for (const [n, ok, d] of results) {
    console.log(`${ok ? "✅" : "❌"}  ${n}${d ? "  — " + d : ""}`);
    if (ok) pass++;
  }
  console.log(`\n${pass}/${results.length} 通过`);

  cdp.close(); app.kill();
  process.exit(pass === results.length ? 0 : 1);
})().catch(e => { console.error("FAIL:", e.message); process.exit(2); });
