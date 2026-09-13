// 端到端冒烟：真实 Electron + mock 引擎，走完「填主题 → 生成 → 出稿 → 渲染 → 导出」全流程
// 重点抓：页面 JS 报错、接口失败、渲染缺字段、timings/quota 异常。
// 注意：本环境 GUI 进程无法跨工具调用存活，启动+测试必须在同一次调用内完成。
"use strict";
const path = require("path");
const { spawn } = require("child_process");

const DESKTOP = path.resolve(__dirname, "..", "desktop");
const ELECTRON = path.join(DESKTOP, "node_modules", "electron", "dist", "electron.exe");
const CDP_PORT = 9348;
const sleep = ms => new Promise(r => setTimeout(r, ms));

/* ---- 极简 CDP 客户端 ---- */
function connect(wsUrl) {
  return new Promise((res, rej) => {
    const net = require("net");
    const u = new (require("url").URL)(wsUrl);
    const sock = net.connect(Number(u.port), u.hostname, () => {
      sock.write(`GET ${u.pathname} HTTP/1.1\r\nHost: ${u.host}\r\nUpgrade: websocket\r\n` +
        `Connection: Upgrade\r\nSec-WebSocket-Key: ${Buffer.from("e2e" + Date.now()).toString("base64")}\r\n` +
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
  // 宿主注入 ELECTRON_RUN_AS_NODE 会让 electron 退化成纯 Node
  const env = { ...process.env };
  delete env.ELECTRON_RUN_AS_NODE;
  env.TALKSCRIPT_MOCK = "1";                       // 无 API Key 也能跑通全流程
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
  await cdp.send("Log.enable").catch(() => {});
  await sleep(3500);

  const ev = async e => {
    const r = await cdp.send("Runtime.evaluate", { expression: e, returnByValue: true, awaitPromise: true });
    if (r.exceptionDetails) return { __err: r.exceptionDetails.text + " " + (r.exceptionDetails.exception?.description || "").slice(0, 200) };
    return r.result.value;
  };

  // 装全局错误收集器（越早越好，但 boot 已跑过；至少覆盖后续交互）
  await ev(`window.__E2E_ERRS__=[];addEventListener('error',e=>__E2E_ERRS__.push(String(e.message)));
            addEventListener('unhandledrejection',e=>__E2E_ERRS__.push('rej:'+String(e.reason)));true`);

  console.log("URL:", target.url);
  console.log("boot 完成? 行业包数 =", await ev(`(document.getElementById('pack')||{options:[]}).options.length`));
  console.log("tick 可见 =", await ev(`!!document.querySelector('.tick')||'n/a'`));

  // 1) 填主题并发送
  console.log("\n── 1) 触发一次生成 ──");
  await ev(`const t=document.getElementById('topic'); t.value='家用电梯到底该怎么选？'; t.dispatchEvent(new Event('input',{bubbles:true})); true`);
  const sent = await ev(`(async()=>{ try { await send(); return 'sent'; } catch(e){ return 'ERR '+e.message; } })()`);
  console.log("   send() →", sent);

  // 2) 轮询直到 done / failed
  console.log("\n── 2) 等待完成 ──");
  let st = null;
  for (let i = 0; i < 90; i++) {
    await sleep(1000);
    st = await ev(`(() => {
      const r = (typeof currentResult!=='undefined' && currentResult) ? currentResult : null;
      const head = document.querySelector('#right .page-head .mono, #right .view:not(.hidden) .page-state');
      return { step: (typeof PollState!=='undefined')?PollState:null, done: !!r,
               title: document.querySelector('.page-title')?.textContent?.trim() || '',
               state: head?.textContent?.trim() || '', errs: (window.__E2E_ERRS__||[]).slice(0,5) };
    })()`);
    if (st.done) { console.log(`   第 ${i + 1}s 完成`); break; }
    if (i % 5 === 0) console.log(`   ${i}s  标题=${st.title}  状态=${st.state}`);
  }

  // 3) 检查结果结构
  console.log("\n── 3) 结果体检 ──");
  const chk = await ev(`(() => {
    const r = (typeof currentResult!=='undefined')?currentResult:null;
    if (!r) return { fatal:'无结果' };
    const secs = r.sections||[];
    return {
      topic: r.params?.topic,
      pack: r.pack, draft: r.pack_draft,
      quota: r.quota, plan_keys: Object.keys(r.plan||{}),
      sections: secs.length, secTypes: secs.map(s=>s.type),
      emptyText: secs.filter(s=>!s.text||!String(s.text).trim()).length,
      storyboard: (r.storyboard||[]).length, scenes: (r.scenes||[]).length,
      check_passed: r.check?.passed, dev: r.check?.deviation_pct,
      hard: (r.check?.hard_hits||[]).length, soft: (r.check?.soft_hits||[]).length,
      placeholders: r.placeholders?.length, revisions: (r.revisions||[]).length,
      timings: (r.timings||[]).length, logs: (r.logs||[]).length,
    };
  })()`);
  console.log(JSON.stringify(chk, null, 1));

  // 4) DOM 渲染体检
  console.log("\n── 4) DOM 渲染体检 ──");
  const dom = await ev(`(() => {
    const q = s => document.querySelectorAll(s).length;
    const txt = document.querySelector('#chat-stream')?.innerText || '';
    return {
      脚本卡片: q('.script-card'), 要点标签: q('.pill'), 折叠面板: q('details'),
      故事板行: q('tr, .sb-row, .story-row'), 横幅: q('.banner'),
      可见卡数: [...document.querySelectorAll('.script-card')].filter(e=>e.offsetParent).length,
      文本长度: txt.length, 有无占位提示: /占位/.test(txt),
      结果区可见: !!document.querySelector('.script-list')?.offsetParent,
    };
  })()`);
  console.log(JSON.stringify(dom, null, 1));

  // 5) 导出可用性
  console.log("\n── 5) 导出能力 ──");
  console.log("   Markdown:", await ev(`(()=>{try{const s=resultMarkdown(currentResult);return s.length+' 字符';}catch(e){return 'ERR '+e.message}})()`));
  console.log("   SRT     :", await ev(`(()=>{try{const s=resultSrt(currentResult);return s.length+' 字符';}catch(e){return 'ERR '+e.message}})()`));

  // 6) 错误汇总
  console.log("\n── 6) 错误汇总 ──");
  console.log("   " + JSON.stringify(await ev(`(window.__E2E_ERRS__||[]).slice(0,10)`)));

  cdp.close(); app.kill();
  console.log("\n完成");
  process.exit(0);
})().catch(e => { console.error("FATAL", e); process.exit(1); });
