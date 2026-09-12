// 零依赖 CDP 验证：主侧栏只留「设置」入口 + 整窗设置二级页（含行业内两个分区）
// 用法: node _verify/verify.js
"use strict";
const http = require("http");
const fs = require("fs");
const path = require("path");
const { spawn, execSync } = require("child_process");
const net = require("net");

const ROOT = path.resolve(__dirname, "..", "desktop", "renderer");
const CHROME = process.env.CHROME ||
  "C:/Program Files/Google/Chrome/Application/chrome.exe";
const PORT = 8931;
const CDP_PORT = 9333;

// ── 静态服务 ─────────────────────────────────────────────
const MIME = { ".html": "text/html", ".js": "text/javascript", ".css": "text/css" };

// 把 fetch 桩直接塞进 index.html（在 app.js 之前）：
// 比 CDP 的 addScriptToEvaluateOnNewDocument 更确定——不受进程切换/时序影响
const STUB = `<script>
window.__INJECTED__ = 1;
window.__errs = [];
addEventListener('error', e => window.__errs.push(String(e.message)));
addEventListener('unhandledrejection', e => window.__errs.push('rej: ' + String(e.reason)));
(function(){
  var META = ${JSON.stringify({
    default_pack: "elevator",
    packs: [
      { name: "elevator", display_name: "电梯行业包", draft: false,
        params: { segment: { label: "细分领域", default: "家用电梯", options: ["家用电梯","维保","加装"] },
                  style: { label: "风格", default: "口播科普", options: ["口播科普","带货"] } } },
      { name: "fitment", display_name: "全屋定制包", draft: true,
        params: { segment: { label: "细分领域", default: "全屋定制", options: ["全屋定制"] } } },
    ] })};
  var CONFIG = { base_url: "https://open.bigmodel.cn/api/paas/v4", model: "glm-4.7", api_key_set: true, mock: false };
  function mk(o){ return Promise.resolve(new Response(JSON.stringify(o), { status:200, headers:{'Content-Type':'application/json'} })); }
  window.fetch = function(u, o){
    var s = String(u);
    if (s.indexOf('/api/meta') >= 0) return mk(META);
    // 注意：这段代码位于模板字面量内，反斜杠会被提前消掉（斜杠转义会让整行
    // 变成注释、桩脚本全部失效），所以这里只用 indexOf 判断，不用正则。
    // 真实接口是 GET /api/history，直接返回数组（不是 {items:[]}）。
    if (s.indexOf('/api/history') >= 0 && s.indexOf('/api/history/') < 0) return mk([
      { id:'s1', created_at:'2026-09-10 10:00', pack:'elevator',
        topic:'家用电梯怎么挑？', duration:60, chars:261 },
      { id:'s2', created_at:'2026-09-09 09:00', pack:'elevator',
        topic:'电梯维保到底保什么', duration:60, chars:255 } ]);
    if (s.indexOf('/api/config/test') >= 0) return mk({ ok:true, model:'glm-4.7', detail:'延迟 320ms' });
    if (s.indexOf('/api/config') >= 0) return mk(CONFIG);
    if (s.indexOf('/api/packs/') >= 0) return mk({ display_name:'电梯行业包', description:'电梯行业口播脚本包',
      draft:false, checklist:'1. 核对参数 / 2. 核对禁用词',
      files:[{rel:'pack.yaml',size:2048},{rel:'skill.yaml',size:1024},{rel:'knowledge/chanpin.md',size:5120}] });
    return mk({});
  };
})();
</script>
`;

function serve() {
  return new Promise(res => {
    const s = http.createServer((req, rq) => {
      let p = decodeURIComponent(req.url.split("?")[0]);
      if (process.env.VERBOSE) console.log("[srv]", req.url);
      if (p === "/") p = "/index.html";
      const f = path.join(ROOT, p);
      fs.readFile(f, "utf8", (e, txt) => {
        if (e) { rq.writeHead(404); rq.end("404"); return; }
        if (p === "/index.html") {
          txt = txt.replace('<script src="app.js"></script>', STUB + '<script src="app.js"></script>');
        }
        rq.writeHead(200, { "Content-Type": MIME[path.extname(f)] || "application/octet-stream" });
        rq.end(txt);
      });
    });
    s.listen(PORT, "127.0.0.1", () => res(s));
  });
}

// ── CDP 客户端（手写 WebSocket，无第三方依赖）────────────
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
        else if (len === 127) { if (buf.length < 10) return; len = Number(buf.readBigUInt64BE(2)); off = 10; }
        if (buf.length < off + len) return;
        const msg = buf.slice(off, off + len).toString();
        buf = buf.slice(off + len);
        try {
          const o = JSON.parse(msg);
          if (o.id && pending.has(o.id)) {
            const { ok, no } = pending.get(o.id); pending.delete(o.id);
            o.error ? no(new Error(o.error.message)) : ok(o.result);
          }
        } catch (_) {}
      }
    });
  });
}

const sleep = ms => new Promise(r => setTimeout(r, ms));

(async () => {
  // 清掉可能残留的 Chrome（端口占用会导致连不上旧实例）
  try { execSync("taskkill /F /IM chrome.exe /T >NUL 2>NUL"); } catch (_) {}
  await sleep(600);

  const srv = await serve();
  // 每次用全新 profile：复用旧 profile 会命中 HTTP 缓存，
  // 拿到上一轮的 index.html（不含 stub），导致"改了没生效"的假象
  const userDir = path.join(__dirname, "_cprof");
  try { fs.rmSync(userDir, { recursive: true, force: true }); } catch (_) {}
  fs.mkdirSync(userDir, { recursive: true });
  const chrome = spawn(CHROME, [
    `--headless=new`, `--remote-debugging-port=${CDP_PORT}`,
    `--user-data-dir=${userDir}`, `--no-first-run`, `--no-default-browser-check`,
    `--disable-gpu`, `--window-size=1440,900`, "about:blank",
  ], { stdio: "ignore" });

  // 等 CDP 就绪
  let target = null;
  for (let i = 0; i < 40; i++) {
    try {
      const r = await fetch(`http://127.0.0.1:${CDP_PORT}/json/new?about:blank`, { method: "PUT" });
      target = await r.json(); break;
    } catch (_) { await sleep(250); }
  }
  if (!target) throw new Error("CDP 未就绪");

  const cdp = await connect(target.webSocketDebuggerUrl);
  await cdp.send("Page.enable");
  await cdp.send("Runtime.enable");
  await cdp.send("Network.enable");
  await cdp.send("Network.setCacheDisabled", { cacheDisabled: true });

  // 自检：确认服务端真的把 stub 注入进了 HTML
  const selfHtml = await new Promise(r =>
    http.get(`http://127.0.0.1:${PORT}/index.html?port=1`, res => {
      let d = ""; res.on("data", c => d += c); res.on("end", () => r(d));
    }));
  console.log("[selfcheck] html contains stub:", selfHtml.includes("__INJECTED__"),
              "| len:", selfHtml.length);

  await cdp.send("Page.navigate", { url: `http://127.0.0.1:${PORT}/index.html?port=1` });
  await sleep(1400);

  const evalIn = async (expr) => {
    const r = await cdp.send("Runtime.evaluate", { expression: expr, returnByValue: true, awaitPromise: true });
    if (r.exceptionDetails) throw new Error(r.exceptionDetails.text + " :: " + JSON.stringify(r.result));
    return r.result.value;
  };

  const results = [];

  // 0) 诊断：确认 fetch 桩生效 + boot 是否跑完
  const diag = await evalIn(`(() => ({
    stubbed: /api\\/meta/.test(String(window.fetch)),
    errs: (window.__errs||[]).slice(0,5),
    packOpts: (document.getElementById('pack')||{options:[]}).options.length,
    emptyHTML: (document.getElementById('empty')||{}).innerHTML ? String(document.getElementById('empty').innerHTML).slice(0,120) : '',
    hasMeta: typeof META !== 'undefined',
    injected: window.__INJECTED__ === 1,
    docHasStub: document.documentElement.innerHTML.indexOf("__INJECTED__") >= 0,
    scriptCount: document.querySelectorAll("script").length,
    fetchStr: String(window.fetch).slice(0, 60),
  }))()`);
  console.log("DIAG:", JSON.stringify(diag, null, 2));

  // 1) 无 JS 报错
  const errs = await evalIn(`(window.__errs||[]).slice(0,8)`);
  results.push(["无 JS 运行错误", errs.length === 0, errs.join(" | ")]);

  // 2) 主侧栏：配置类入口全部收进设置二级页，只剩「设置」一项
  const nav = await evalIn(`(() => {
    const foot = document.querySelector('.left-foot');
    const items = [...foot.querySelectorAll('.nav-item')];
    return {
      secs: foot.querySelectorAll('.nav-sec').length,
      items: items.length,
      texts: items.map(e=>e.querySelector('.nav-t').textContent),
      labels: [...foot.querySelectorAll('.nav-sec-t')].map(e=>e.textContent),
      straySettings: foot.querySelectorAll("[data-view='settings']").length,
      strayPack: !!(document.getElementById('btn-nav-packinfo') || document.getElementById('btn-nav-newpack')),
      hasEntry: !!document.getElementById('btn-open-settings'),
    };
  })()`);
  results.push(["主侧栏只剩 1 个分组", nav.secs === 1, JSON.stringify(nav.labels)]);
  results.push(["主侧栏只剩「设置」1 项", nav.items === 1 && /设置/.test(nav.texts[0] || ""), JSON.stringify(nav.texts)]);
  results.push(["行业包/新建行业包已移出主侧栏", nav.strayPack === false, ""]);
  results.push(["侧栏不再有设置类明细项", nav.straySettings === 0, "data-view=settings 残留 " + nav.straySettings]);
  results.push(["「设置」入口存在", nav.hasEntry === true, ""]);

  // 3) 设置是独立整窗页面，初始隐藏
  const init = await evalIn(`(() => {
    const sc = document.getElementById('settings-screen');
    return {
      exists: !!sc,
      hidden: sc ? sc.classList.contains('hidden') : null,
      fixed: sc ? getComputedStyle(sc).position : null,
      rect: sc ? sc.getBoundingClientRect().width + 'x' + sc.getBoundingClientRect().height : null,
      panes: ['gen','packinfo','packgen','llm','kb','skills'].filter(p=>!!document.getElementById('pane-'+p)).length,
      navItems: document.querySelectorAll('.stg-nav-item').length,
      inRight: !!document.querySelector('#right #settings-screen'),
      visibleView: (document.querySelector('#right > .view:not(.hidden)')||{}).id,
      sessions: document.querySelectorAll('#session-list .sess-item').length,
    };
  })()`);
  results.push(["设置页存在且初始隐藏", init.exists && init.hidden === true, "hidden=" + init.hidden]);
  results.push(["设置页挂在 #right 之外（真·二级页）", init.inRight === false, ""]);
  results.push(["设置页 6 分区 + 6 个分类项", init.panes === 6 && init.navItems === 6, "panes=" + init.panes + " nav=" + init.navItems]);
  results.push(["初始视图 = chat", init.visibleView === "view-chat", init.visibleView]);
  // 这条同时校验 stub 的 /api/history 路径与返回结构是否和真实接口一致
  results.push(["会话列表渲染出 2 条（stub）", init.sessions === 2, "sessions=" + init.sessions]);

  // 4) 点主侧栏「设置」→ 整窗页面出现，默认落在生成偏好
  await evalIn(`document.getElementById('btn-open-settings').click(); true`);
  await sleep(320);
  const opened = await evalIn(`(() => {
    const sc = document.getElementById('settings-screen');
    const r = sc.getBoundingClientRect();
    return {
      hidden: sc.classList.contains('hidden'),
      coversViewport: Math.round(r.width) === window.innerWidth && Math.round(r.height) === window.innerHeight,
      w: Math.round(r.width), h: Math.round(r.height),
      vw: window.innerWidth, vh: window.innerHeight,
      pos: getComputedStyle(sc).position,
      paneVisible: !document.getElementById('pane-gen').classList.contains('hidden'),
      on: [...document.querySelectorAll('.stg-nav-item.on')].map(e=>e.querySelector('.nav-t').textContent),
      hasBack: !!document.getElementById('btn-close-settings'),
    };
  })()`);
  results.push(["点设置 → 页面打开", opened.hidden === false, ""]);
  results.push(["设置页铺满整个窗口", opened.coversViewport === true, opened.w + 'x' + opened.h + ' vs ' + opened.vw + 'x' + opened.vh]);
  results.push(["定位为 fixed 覆盖层", opened.pos === "fixed", opened.pos]);
  results.push(["默认分区 = 生成偏好", opened.paneVisible === true, ""]);
  results.push(["唯一高亮 = 生成偏好", opened.on.length === 1 && /生成偏好/.test(opened.on[0]), JSON.stringify(opened.on)]);
  results.push(["存在「返回工作区」", opened.hasBack === true, ""]);

  // 5) 设置页内切到「模型接口」→ 分区切换 + 唯一高亮 + 配置回填
  await evalIn(`document.querySelector('.stg-nav-item[data-pane="llm"]').click(); true`);
  await sleep(260);
  const llm = await evalIn(`(() => ({
    paneVisible: !document.getElementById('pane-llm').classList.contains('hidden'),
    othersHidden: ['gen','kb','skills'].every(p=>document.getElementById('pane-'+p).classList.contains('hidden')),
    on: [...document.querySelectorAll('.stg-nav-item.on')].map(e=>e.querySelector('.nav-t').textContent),
    baseurl: document.getElementById('st-baseurl').value,
  }))()`);
  results.push(["切到 llm 分区，其余隐藏", llm.paneVisible && llm.othersHidden, ""]);
  results.push(["唯一高亮 = 模型接口", llm.on.length === 1 && /模型接口/.test(llm.on[0]), JSON.stringify(llm.on)]);
  results.push(["配置已回填 base_url", /bigmodel/.test(llm.baseurl), llm.baseurl]);

  // 6) 切到「知识库」→ 占位面板
  await evalIn(`document.querySelector('.stg-nav-item[data-pane="kb"]').click(); true`);
  await sleep(260);
  const kb = await evalIn(`(() => ({
    paneVisible: !document.getElementById('pane-kb').classList.contains('hidden'),
    ph: !!document.querySelector('#pane-kb .placeholder'),
    on: [...document.querySelectorAll('.stg-nav-item.on')].map(e=>e.querySelector('.nav-t').textContent),
  }))()`);
  results.push(["切到知识库 → 占位面板渲染", kb.paneVisible && kb.ph === true, ""]);
  results.push(["高亮切到知识库", kb.on.length === 1 && /知识库/.test(kb.on[0]), JSON.stringify(kb.on)]);

  // 7) 「返回工作区」关闭设置
  //    关键：用真实鼠标事件而不是 el.click()——后者绕过命中测试，
  //    按钮被别的层盖住时测试照样通过，但用户点下去没反应。
  const hit = await evalIn(`(() => {
    const b = document.getElementById('btn-close-settings');
    const r = b.getBoundingClientRect();
    const cx = Math.round(r.left + r.width / 2), cy = Math.round(r.top + r.height / 2);
    const top = document.elementFromPoint(cx, cy);
    return { cx, cy, w: Math.round(r.width), h: Math.round(r.height),
             topId: top ? (top.id || "") : null,
             topCls: top ? String(top.className || "") : null,
             topTag: top ? top.tagName : null,
             reachable: !!top && (top === b || b.contains(top)) };
  })()`);
  results.push(["返回按钮可命中（无遮挡）", hit.reachable === true,
    "命中到的元素: " + hit.topTag + "#" + hit.topId + "." + hit.topCls + " @(" + hit.cx + "," + hit.cy + ") 尺寸" + hit.w + "x" + hit.h]);

  // 真实鼠标按下/抬起（走完整命中测试 + 事件派发）
  await cdp.send("Input.dispatchMouseEvent", { type: "mousePressed", x: hit.cx, y: hit.cy, button: "left", clickCount: 1 });
  await cdp.send("Input.dispatchMouseEvent", { type: "mouseReleased", x: hit.cx, y: hit.cy, button: "left", clickCount: 1 });
  await sleep(300);
  const closedViaBtn = await evalIn(`document.getElementById('settings-screen').classList.contains('hidden')`);
  results.push(["真实鼠标点击返回 → 设置关闭", closedViaBtn === true, "hidden=" + closedViaBtn]);

  // 8) Ctrl+, 打开 / Esc 关闭
  await evalIn(`document.dispatchEvent(new KeyboardEvent('keydown',{key:',',ctrlKey:true,bubbles:true})); true`);
  await sleep(300);
  const viaKbd = await evalIn(`(() => ({
    open: !document.getElementById('settings-screen').classList.contains('hidden'),
  }))()`);
  results.push(["Ctrl+, 打开设置", viaKbd.open === true, ""]);
  await cdp.send("Input.dispatchKeyEvent", { type: "keyDown", key: "Escape", code: "Escape", windowsVirtualKeyCode: 27 });
  await cdp.send("Input.dispatchKeyEvent", { type: "keyUp", key: "Escape", code: "Escape", windowsVirtualKeyCode: 27 });
  await sleep(260);
  const viaEsc = await evalIn(`document.getElementById('settings-screen').classList.contains('hidden')`);
  results.push(["Esc 关闭设置", viaEsc === true, ""]);

  // 9) 关闭后主界面仍在；行业包已迁入设置二级页，不再占用 #right 的视图
  const back = await evalIn(`(() => ({
    view: (document.querySelector('#right > .view:not(.hidden)')||{}).id,
    leftVisible: document.getElementById('left').getBoundingClientRect().width > 0,
    leftItems: document.querySelectorAll('.left-foot .nav-item').length,
    rightViews: document.querySelectorAll('#right > .view').length,
    strayView: !!document.getElementById('view-packinfo') || !!document.getElementById('view-packgen'),
  }))()`);
  results.push(["关闭后主界面原样（对话视图 + 侧栏在）", back.view === "view-chat" && back.leftVisible === true, back.view]);
  results.push(["内容区只剩 chat 一个视图", back.rightViews === 1 && back.strayView === false, "views=" + back.rightViews]);
  results.push(["主侧栏只剩设置 1 个入口", back.leftItems === 1, "items=" + back.leftItems]);

  // 9b) 行业包 / 新建行业包 现在是设置页里「行业」分组下的相邻分类；
  //     详情数据由 setSettingsPane 进入时自动拉取（不再由入口按钮各自触发）
  await evalIn(`document.getElementById('btn-open-settings').click(); true`);
  await sleep(300);
  await evalIn(`document.querySelector('.stg-nav-item[data-pane="packinfo"]').click(); true`);
  await sleep(700);
  const pi = await evalIn(`(() => ({
    paneVisible: !document.getElementById('pane-packinfo').classList.contains('hidden'),
    genHidden: document.getElementById('pane-gen').classList.contains('hidden'),
    on: [...document.querySelectorAll('.stg-nav-item.on')].map(e=>e.querySelector('.nav-t').textContent),
    filesRendered: (document.getElementById('pi-files').textContent||'').trim().length > 0,
    rows: document.querySelectorAll('#pi-files tbody tr').length,
    title: (document.getElementById('pi-title').textContent||'').trim(),
    sec: (document.querySelector('.stg-nav-item[data-pane="packinfo"]').closest('.nav-sec').querySelector('.nav-sec-t').textContent||'').trim(),
  }))()`);
  results.push(["设置页内「行业包」分区可切换", pi.paneVisible === true && pi.genHidden === true, ""]);
  results.push(["行业包项高亮（唯一）", pi.on.length === 1 && /行业包/.test(pi.on[0]), JSON.stringify(pi.on)]);
  results.push(["归入「行业」分组", /行业/.test(pi.sec), pi.sec]);
  results.push(["进入即自动载入详情文件清单", pi.filesRendered === true && pi.rows === 3, pi.title + " rows=" + pi.rows]);

  await evalIn(`document.querySelector('.stg-nav-item[data-pane="packgen"]').click(); true`);
  await sleep(320);
  const pg = await evalIn(`(() => ({
    paneVisible: !document.getElementById('pane-packgen').classList.contains('hidden'),
    packinfoHidden: document.getElementById('pane-packinfo').classList.contains('hidden'),
    on: [...document.querySelectorAll('.stg-nav-item.on')].map(e=>e.querySelector('.nav-t').textContent),
    formVisible: !document.getElementById('pg-form').classList.contains('hidden'),
    hasIndustry: !!document.getElementById('pg-industry') && !!document.getElementById('pg-run'),
  }))()`);
  results.push(["设置页内「新建行业包」分区可切换", pg.paneVisible === true && pg.packinfoHidden === true, ""]);
  results.push(["新建项高亮（唯一）", pg.on.length === 1 && /新建行业包/.test(pg.on[0]), JSON.stringify(pg.on)]);
  results.push(["新建表单控件完整", pg.formVisible === true && pg.hasIndustry === true, ""]);

  // 9c) 上述切换均为异步（openPackInfo 会自动拉数据），确认没有炸出运行时错误
  const errs2 = await evalIn(`(window.__errs||[]).slice(0,8)`);
  results.push(["切换过程无 JS 错误", errs2.length === 0, errs2.join(" | ")]);

  // 10) 无遗留旧节点 / 无横向溢出
  const legacy = await evalIn(`(() => {
    const ids = ['btn-nav-setup','btn-close-setup','drawer-setup','view-settings'];
    return ids.filter(i => !!document.getElementById(i));
  })()`);
  results.push(["无遗留旧节点", legacy.length === 0, JSON.stringify(legacy)]);
  await evalIn(`document.getElementById('btn-open-settings').click(); true`);
  await sleep(300);
  const layout = await evalIn(`(() => {
    const pane = document.querySelector('#settings-screen .stg-pane:not(.hidden)');
    const r = pane.getBoundingClientRect();
    const nav = document.querySelector('.stg-nav').getBoundingClientRect();
    return { pane: pane.id, paneW: Math.round(r.width), paneH: Math.round(r.height), navW: Math.round(nav.width),
             overflowX: document.documentElement.scrollWidth > window.innerWidth,
             paneScroll: pane.scrollHeight - pane.clientHeight };
  })()`);
  results.push(["可见分区有合理尺寸", layout.paneW > 400 && layout.paneH > 300, JSON.stringify(layout)]);
  results.push(["无横向溢出", layout.overflowX === false, ""]);

  // 11) 回归：生成中点「新建对话」不得把界面锁死
  //     曾经的 bug：clearChat 把 currentJob 置空却没复位 busy，
  //     轮询也停了 → btn-generate 永久 disabled、输入框永久锁定，只能重启应用。
  //     注意断言前要先填主题：发送键的可用性 = 「有主题 && 不在生成中」，
  //     空主题时它本来就该是灰的，否则这条断言会误报。
  const FILL_TOPIC = `(() => { const t = document.getElementById('topic');
    t.value = '锁死回归测试'; t.dispatchEvent(new Event('input', { bubbles: true })); })()`;
  const lockup = await evalIn(`(() => {
    gotoView('chat');
    setBusy(true, true);
    currentJob = { id: 'fake-job', state: 'writing' };
    clearChat();
    ${FILL_TOPIC}
    const t = document.getElementById('topic');
    return {
      busy: busyNow, job: currentJob,
      btnDisabled: document.getElementById('btn-generate').disabled,
      topicDisabled: t.disabled,
      hint: document.getElementById('composer-gen-hint').textContent,
    };
  })()`);
  results.push(["生成中点「新建对话」→ busy 复位", lockup.busy === false && lockup.job === null,
    "busy=" + lockup.busy + " job=" + JSON.stringify(lockup.job)]);
  results.push(["→ 发送键恢复可用", lockup.btnDisabled === false, "disabled=" + lockup.btnDisabled]);
  results.push(["→ 输入框恢复可编辑", lockup.topicDisabled === false, "disabled=" + lockup.topicDisabled]);
  results.push(["→ 「生成中」提示已清空", lockup.hint === "", JSON.stringify(lockup.hint)]);

  // 12) 回归：分步确认卡点「取消」同样不得锁死
  await evalIn(`(() => {
    setBusy(true, true);
    currentJob = { id: 'fake-job2', state: 'paused_awaiting_confirmation' };
    document.getElementById('confirm-overlay').classList.remove('hidden');
  })()`);
  await evalIn(`document.getElementById('cf-cancel').click(); true`);
  await sleep(320);
  const cfState = await evalIn(`(() => {
    ${FILL_TOPIC}
    const t = document.getElementById('topic');
    return { busy: busyNow, job: currentJob,
             btnDisabled: document.getElementById('btn-generate').disabled,
             topicDisabled: t.disabled };
  })()`);
  results.push(["分步确认「取消」→ 界面解锁",
    cfState.busy === false && cfState.job === null && cfState.btnDisabled === false && cfState.topicDisabled === false,
    JSON.stringify(cfState)]);

  // 收尾：清掉测试用的主题，避免污染后面的截图
  await evalIn(`(() => { const t = document.getElementById('topic');
    t.value = ''; t.dispatchEvent(new Event('input', { bubbles: true })); })()`);

  // 截图（2x 便于目检细节）
  await cdp.send("Emulation.setDeviceMetricsOverride", { width: 1440, height: 900, deviceScaleFactor: 2, mobile: false });
  const shot = async (name, setup) => {
    if (setup) { await evalIn(setup); await sleep(320); }
    const s = await cdp.send("Page.captureScreenshot", { format: "png" });
    fs.writeFileSync(path.join(__dirname, name), Buffer.from(s.data, "base64"));
  };
  // 主界面（设置关闭态）：底部只剩「设置」一个入口
  await shot("main-sidebar.png", "document.getElementById('settings-screen').classList.add('hidden'); gotoView('chat'); true");
  // 设置整窗页面：生成偏好 / 行业包 / 模型接口 / 知识库
  await shot("settings-gen.png", "document.getElementById('btn-open-settings').click(); setSettingsPane('gen'); true");
  await shot("settings-packinfo.png", "setSettingsPane('packinfo'); true");
  await shot("settings-llm.png", "setSettingsPane('llm'); true");
  await shot("settings-kb.png", "setSettingsPane('kb'); true");

  // 输出
  console.log("\n════════ 验证结果 ════════");
  let pass = 0;
  for (const [name, ok, detail] of results) {
    console.log(`${ok ? "✅" : "❌"}  ${name}${detail ? "  — " + detail : ""}`);
    if (ok) pass++;
  }
  console.log(`\n${pass}/${results.length} 通过`);

  cdp.close(); chrome.kill();
  srv.close();
  process.exit(pass === results.length ? 0 : 1);
})().catch(e => { console.error("FAIL:", e.message); process.exit(2); });
