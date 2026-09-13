// 交互边界压测：抓真实用户动作下的状态污染 / 竞态 / 未恢复 UI
// 场景：连点发送、生成中切会话、生成中开设置、取消生成、空主题、删除会话
"use strict";
const path = require("path");
const { spawn } = require("child_process");
const net = require("net");

const DESKTOP = path.resolve(__dirname, "..", "desktop");
const ELECTRON = path.join(DESKTOP, "node_modules", "electron", "dist", "electron.exe");
const CDP_PORT = 9351;
const sleep = ms => new Promise(r => setTimeout(r, ms));

function connect(wsUrl) {
  return new Promise((res, rej) => {
    const u = new (require("url").URL)(wsUrl);
    const sock = net.connect(Number(u.port), u.hostname, () => {
      sock.write(`GET ${u.pathname} HTTP/1.1\r\nHost: ${u.host}\r\nUpgrade: websocket\r\n` +
        `Connection: Upgrade\r\nSec-WebSocket-Key: ${Buffer.from("edge" + Date.now()).toString("base64")}\r\n` +
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
          const mask = Buffer.from([5, 5, 6, 6]); const m = Buffer.from(p);
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
  delete env.ELECTRON_RUN_AS_NODE;
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
    if (r.exceptionDetails) return { __err: (r.exceptionDetails.text || "") + " " + ((r.exceptionDetails.exception || {}).description || "").slice(0, 180) };
    return r.result.value;
  };
  await ev(`window.__ERRS__=[];addEventListener('error',e=>__ERRS__.push(String(e.message)));
            addEventListener('unhandledrejection',e=>__ERRS__.push('rej:'+String(e.reason)));true`);

  const pass = [], fail = [];
  const check = (name, cond, detail) => { (cond ? pass : fail).push(name + (detail ? " — " + detail : "")); console.log((cond ? "✅ " : "❌ ") + name + (detail ? " — " + detail : "")); };

  // ── 1) 空主题保护 ──
  console.log("\n【1】空主题提交");
  const before = await ev(`(window.__ERRS__||[]).length`);
  await ev(`(async()=>{ document.getElementById('topic').value=''; await send(); return true; })()`);
  await sleep(600);
  check("空主题不触发请求", await ev(`typeof currentJob==='undefined'||currentJob===null`), "currentJob 仍为空");

  // ── 2) 连点发送（竞态）──
  console.log("\n【2】快速连点发送 3 次");
  await ev(`const t=document.getElementById('topic'); t.value='电梯维保避坑指南'; t.dispatchEvent(new Event('input',{bubbles:true})); true`);
  await ev(`(async()=>{ const ps=[]; for(let i=0;i<3;i++){ ps.push(send()); } await Promise.allSettled(ps); return true; })()`);
  await sleep(1200);
  const jobs = await ev(`(() => { const b=document.querySelectorAll('#chat-stream .msg'); return { msgs:b.length, job: (typeof currentJob!=='undefined'&&currentJob)?currentJob.id:null }; })()`);
  console.log("   结果:", JSON.stringify(jobs));

  // 等多个 job 都完成
  for (let i = 0; i < 40; i++) { await sleep(1000); if (await ev(`!!((typeof currentResult!=='undefined')&&currentResult)`)) break; }
  const after = await ev(`(() => {
    const cards = document.querySelectorAll('.script-card').length;
    const thinking = document.querySelectorAll('.thinking, .spinner').length;
    const btnDisabled = document.getElementById('btn-generate').disabled;
    const topicDisabled = document.getElementById('topic').disabled;
    return { cards, thinking, btnDisabled, topicDisabled, errs:(window.__ERRS__||[]).slice(0,4) };
  })()`);
  check("连点后 UI 恢复（无残留 thinking）", after.thinking === 0, "thinking=" + after.thinking);
  check("连点后输入框解锁", after.topicDisabled === false, "topic.disabled=" + after.topicDisabled);
  check("连点后卡片数合理(≤5×多次)", after.cards <= 10, "cards=" + after.cards);

  // ── 3) 生成中途取消 ──
  console.log("\n【3】生成中途取消");
  await ev(`const t=document.getElementById('topic'); t.value='加装电梯怎么谈价'; t.dispatchEvent(new Event('input',{bubbles:true})); true`);
  await ev(`send(); true`);
  await sleep(900);
  const midCancel = await ev(`(async()=>{ await abortGeneration(); return { job:(typeof currentJob!=='undefined')?currentJob:null, busy:(typeof busyNow!=='undefined')?busyNow:null }; })()`);
  await sleep(800);
  const cancelState = await ev(`(() => ({
     busy:(typeof busyNow!=='undefined')?busyNow:null,
     topicDisabled: document.getElementById('topic').disabled,
     btnTitle: document.getElementById('btn-generate').title,
     hint: document.querySelector('#chat-stream')?.innerText?.includes('已放弃') || false,
     errs:(window.__ERRS__||[]).slice(0,4),
  }))()`);
  check("取消后 busy 复位", cancelState.busy === false, "busy=" + cancelState.busy);
  check("取消后输入框解锁", cancelState.topicDisabled === false, "disabled=" + cancelState.topicDisabled);

  // ── 4) 生成中打开设置页（状态是否串台）──
  console.log("\n【4】生成中打开设置 / 返回");
  await ev(`const t=document.getElementById('topic'); t.value='家用电梯安全误区'; t.dispatchEvent(new Event('input',{bubbles:true})); true`);
  await ev(`send(); true`);
  await sleep(700);
  await ev(`openSettings('gen'); true`);
  await sleep(500);
  const duringOpen = await ev(`document.getElementById('settings-screen').classList.contains('hidden')===false`);
  await ev(`closeSettings(); true`);
  for (let i = 0; i < 40; i++) { await sleep(1000); if (await ev(`!!((typeof currentResult!=='undefined')&&currentResult)`)) break; }
  const afterAll = await ev(`(() => ({
    result: !!(typeof currentResult!=='undefined'&&currentResult),
    cards: document.querySelectorAll('.script-card').length,
    settingsHidden: document.getElementById('settings-screen').classList.contains('hidden'),
    view: (document.querySelector('#right > .view:not(.hidden)')||{}).id,
    errs:(window.__ERRS__||[]).slice(0,6),
  }))()`);
  check("生成中开设置不阻断流程", afterAll.result === true, "有结果");
  check("返回后设置页已关闭", afterAll.settingsHidden === true, "hidden=" + afterAll.settingsHidden);
  check("返回后仍在对话视图", afterAll.view === "view-chat", "view=" + afterAll.view);

  // ── 5) 会话列表 ──
  console.log("\n【5】会话列表与重开");
  const sess = await ev(`(() => { const l=document.getElementById('session-list'); return { kids:l.children.length, items:l.querySelectorAll('.sess-item').length, empty:l.querySelectorAll('.sess-empty').length }; })()`);
  console.log("   " + JSON.stringify(sess));
  check("会话列表已渲染", sess.items > 0 || sess.empty > 0, `items=${sess.items} empty=${sess.empty}`);

  // ── 6) 错误汇总 ──
  console.log("\n【6】全程 JS 错误");
  const errs = await ev(`(window.__ERRS__||[])`);
  check("零 JS 报错", errs.length === 0, JSON.stringify(errs.slice(0, 5)));

  console.log("\n════════ 汇总 ════════");
  console.log(`通过 ${pass.length} / 失败 ${fail.length}`);
  if (fail.length) fail.forEach(f => console.log("   ❌ " + f));

  cdp.close(); app.kill();
  process.exit(fail.length ? 1 : 0);
})().catch(e => { console.error("FATAL", e); process.exit(2); });
