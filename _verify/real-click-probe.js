// 取证：直接连真实 Electron 窗口（--remote-debugging-port）打真实鼠标点击，
// 看侧栏每次点击的事件序列（pointerdown/mousedown/mouseup/click）到底丢在哪一层。
// 依赖：app.js 里临时插的 [SESSDBG] console 日志。
const net = require("net");
const http = require("http");

const DBG = 9333;
const sleep = ms => new Promise(r => setTimeout(r, ms));

function httpJSON(u) {
  return new Promise((res, rej) => {
    http.get(u, r => { let d = ""; r.on("data", c => d += c); r.on("end", () => { try { res(JSON.parse(d)); } catch (e) { rej(e); } }); })
      .on("error", rej);
  });
}

// 手写的零依赖 WebSocket/CDP 客户端（同 verify.js 的写法）
function connect(wsUrl, onEvt) {
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
    const api = {
      send(method, params) {
        return new Promise((ok, no) => {
          const mid = ++id; pending.set(mid, { ok, no });
          const payload = Buffer.from(JSON.stringify({ id: mid, method, params }));
          const len = payload.length;
          let head;
          if (len < 126) head = Buffer.from([0x81, 0x80 | len]);
          else if (len < 65536) { head = Buffer.alloc(4); head[0] = 0x81; head[1] = 0xfe; head.writeUInt16BE(len, 2); }
          else { head = Buffer.alloc(10); head[0] = 0x81; head[1] = 0xff; head.writeBigUInt64BE(BigInt(len), 2); }
          const mask = Buffer.from([1, 2, 3, 4]);
          const masked = Buffer.from(payload);
          for (let i = 0; i < masked.length; i++) masked[i] ^= mask[i % 4];
          sock.write(Buffer.concat([head, mask, masked]));
        });
      },
      close() { try { sock.destroy(); } catch (_) {} },
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
        else if (len === 127) { if (buf.length < 10) return; len = Number(buf.readBigUInt64BE(2)); off = 10; }
        if (buf.length < off + len) return;
        const msg = buf.slice(off, off + len).toString();
        buf = buf.slice(off + len);
        let o; try { o = JSON.parse(msg); } catch (_) { continue; }
        if (o.id && pending.has(o.id)) {
          const { ok, no } = pending.get(o.id); pending.delete(o.id);
          o.error ? no(new Error(o.error.message)) : ok(o.result);
        } else if (o.method && onEvt) onEvt(o);
      }
    });
  });
}

(async () => {
  let target = null;
  for (let i = 0; i < 20 && !target; i++) {
    try {
      const list = await httpJSON(`http://127.0.0.1:${DBG}/json/list`);
      target = list.find(t => t.type === "page" && /renderer\/index\.html/.test(t.url || ""));
    } catch (_) {}
    if (!target) await sleep(1000);
  }
  if (!target) throw new Error("未找到渲染进程目标，应用可能没起来");
  console.log("已连上窗口:", target.title);

  let logs = [];
  const cdp = await connect(target.webSocketDebuggerUrl, o => {
    if (o.method === "Runtime.consoleAPICalled" && /log|info/.test(o.params.type)) {
      logs.push(o.params.args.map(a => String(a.value !== undefined ? a.value : (a.description || ""))).join(" "));
    }
  });
  await cdp.send("Runtime.enable");
  await cdp.send("Page.enable");
  await cdp.send("Page.reload");      // 每次取证都重载，确保用的是当前磁盘上的样式/脚本
  await sleep(2500);
  const evalIn = async expr => {
    const r = await cdp.send("Runtime.evaluate", { expression: expr, returnByValue: true, awaitPromise: true });
    if (r.exceptionDetails) throw new Error(r.exceptionDetails.text);
    return r.result.value;
  };

  const info = await evalIn(`(() => {
    const rows = [...document.querySelectorAll('#session-list .sess-item')];
    const r = rows.find(x => { const d = x.querySelector('.sess-del'); return d && /删除/.test(d.title); });
    if (!r) return { found: false, rows: rows.length };
    const d = r.querySelector('.sess-del');
    const b = d.getBoundingClientRect();
    const cs = getComputedStyle(d);
    const cx = Math.round(b.left + b.width / 2), cy = Math.round(b.top + b.height / 2);
    const top = document.elementFromPoint(cx, cy);
    return { found: true, rows: rows.length, cx: cx, cy: cy, w: Math.round(b.width), h: Math.round(b.height),
             opacity: cs.opacity, top: top ? (top.id || String(top.className)) : null,
             topic: (r.querySelector('.sess-topic') || {}).textContent };
  })()`);
  console.log("目标按钮:", JSON.stringify(info));
  if (!info.found) { console.log("没有可点的记录"); cdp.close(); process.exit(0); }

  await cdp.send("Input.dispatchMouseEvent", { type: "mouseMoved", x: info.cx, y: info.cy });
  await sleep(300);

  const snap = `(() => { const s = document.querySelector('.left-scroll');
    const e = document.elementFromPoint(${info.cx}, ${info.cy});
    const d = document.querySelector('#session-list .sess-del');
    const b = d ? d.getBoundingClientRect() : null;
    return { scrollTop: Math.round(s.scrollTop),
             active: String((document.activeElement||{}).className || '').slice(0,40),
             atPoint: e ? String(e.className).slice(0,40) : null,
             btnTop: b ? Math.round(b.top) : null, btnLeft: b ? Math.round(b.left) : null }; })()`;

  let opens = 0;
  for (let i = 1; i <= 4; i++) {
    logs = [];
    const before = await evalIn(snap);
    await cdp.send("Input.dispatchMouseEvent", { type: "mousePressed", x: info.cx, y: info.cy, button: "left", clickCount: 1 });
    await sleep(60);
    const mid = await evalIn(snap);
    await cdp.send("Input.dispatchMouseEvent", { type: "mouseReleased", x: info.cx, y: info.cy, button: "left", clickCount: 1 });
    await sleep(300);
    const opened = await evalIn(`!document.getElementById('confirm-dialog').classList.contains('hidden')`);
    const trace = logs.filter(l => l.indexOf("[SESSDBG]") >= 0)
      .map(l => l.replace("[SESSDBG] ", "").replace(" target=sess-del", "").replace(" trusted=", " T=")).join(" ｜ ");
    if (opened) { opens++; await evalIn(`document.getElementById('cd-no').click(); true`); await sleep(200); }
    console.log(`── 第 ${i} 次：弹窗=${opened ? "是" : "否"}`);
    console.log(`   按下前 scrollTop=${before.scrollTop} 按钮top=${before.btnTop} 指针下=${before.atPoint} 焦点=${before.active}`);
    console.log(`   按下后 scrollTop=${mid.scrollTop} 按钮top=${mid.btnTop} 指针下=${mid.atPoint} 焦点=${mid.active}`);
    console.log(`   事件: ${trace || "（无）"}`);
    await sleep(300);
  }
  console.log(`\n合计：4 次真实点击，弹出 ${opens} 次确认弹窗`);
  cdp.close();
  process.exit(0);
})().catch(e => { console.error("FAIL:", e.message); process.exit(1); });
