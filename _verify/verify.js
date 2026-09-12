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
let PORT = 8931;          // 运行时换成实际拿到的空闲端口
let CDP_PORT = 9333;
// 端口不能写死：上一轮若没退干净，新实例会连到旧实例上然后卡到超时
// （表现为「输出为空 + SIGTERM」）。改为每次先探一个空闲端口。

/** 取空闲端口：首选端口被占则交给系统分配 */
function freePort(preferred) {
  return new Promise(resolve => {
    const probe = net.createServer();
    probe.once("error", () => {                 // 首选端口被占用
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
  try { execSync(`taskkill /PID ${pid} /T /F`, { stdio: "ignore" }); } catch (_) {}
}

// 退出时必须收干净。此前只在成功分支收尾，抛错或超时时浏览器会一直留着占端口。
// execSync 是同步的，所以放在 exit 钩子里也安全。
let _chromeRef = null, _cdpRef = null;
function cleanupAll() {
  try { if (_cdpRef) _cdpRef.close(); } catch (_) {}
  killTree(_chromeRef && _chromeRef.pid);   // 只要起过就杀，与 cdp 是否连上无关
  _chromeRef = null; _cdpRef = null;
}
process.on("exit", cleanupAll);
for (const sig of ["SIGINT", "SIGTERM"]) {
  process.on(sig, () => { cleanupAll(); process.exit(130); });
}
process.on("uncaughtException", e => {
  console.error("UNCAUGHT:", e.message); cleanupAll(); process.exit(3);
});

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
        // 与真实 packs/elevator/pack.yaml 的参数集合保持一致，
        // 否则快捷条只渲染 2 个胶囊，「无横向溢出」就测不到真实布局
        params: { segment: { label: "细分领域", default: "家用电梯", options: ["家用电梯","维保","加装"] },
                  audience: { label: "受众", default: "业主乘客", options: ["业主乘客","物业业委会"] },
                  duration: { label: "时长（秒）", default: 60, options: [15, 30, 60, 90] },
                  platform: { label: "平台", default: "抖音", options: ["抖音","视频号","小红书"] },
                  style: { label: "风格", default: "口播科普", options: ["口播科普","带货"] },
                  persona: { label: "人设", default: "维保老师傅", options: ["维保老师傅","产品经理"] },
                  cta: { label: "结尾引导", default: "关注", options: ["关注","私信","留资"] } } },
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
        topic:'家用电梯怎么挑？', duration:60, chars:261, passed:true, state:'done' },
      { id:'s2', created_at:'2026-09-12 15:30', pack:'elevator',
        topic:'扶梯突然停了怎么办', duration:null, chars:null, passed:null, state:'writing' } ]);
    // 已完成记录的详情（openSession 用它渲染结果）
    if (s.indexOf('/api/history/s1') >= 0) return mk({
      id:'s1', created_at:'2026-09-10T10:00', pack:'elevator',
      params:{ topic:'家用电梯怎么挑？', pack:'elevator', duration:60,
               platform:'抖音', style:'口播科普', persona:'维保老师傅' },
      quota:{ total:290, hook:45, body:190, cta:55 },
      plan:{ angle:'看维保', hook_type:'反常识', hook_line:'钩子', points:['要点一'], cta:'关注' },
      sections:[{ type:'hook', text:'开场文案示例', subtitle:'字幕' }],
      storyboard:[], scenes:[],
      check:{ passed:true, chars_total:10, target_total:290, deviation_pct:0, hard_hits:[] },
      placeholders:[], revisions:[], timings:[], logs:[] });
    // 在跑会话的详情（attachJob 用它重建消息流）；带 stream 以覆盖思考流渲染
    if (s.indexOf('/api/jobs/s2') >= 0) return mk({ id:'s2', state:'writing',
      params:{ topic:'扶梯突然停了怎么办', pack:'elevator', duration:60 },
      steps:[{ key:'select', title:'选题策划' }],
      stream:{ phase:'文案撰写', reasoning_tail:'正在斟酌开场钩子的表达方式，避免直接报价格……',
               reasoning_len:136, content_len:12 } });
    // 失败作业（覆盖失败态的「重试」按钮）
    if (s.indexOf('/api/jobs/s3') >= 0) return mk({ id:'s3', state:'failed',
      error:'模型接口连接失败（已重试 3 次）：Server disconnected',
      params:{ topic:'扶梯突然停了怎么办', pack:'elevator', duration:60 }, steps:[] });
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
  // 不再无差别执行 taskkill /IM chrome.exe（那会连用户自己开的浏览器一起干掉）。
  // 改为：端口用随机空闲端口避开冲突，退出时按进程树只回收本脚本起的实例。
  PORT = await freePort(PORT);
  CDP_PORT = await freePort(CDP_PORT);
  console.log("[port] 静态服务=" + PORT + "  CDP=" + CDP_PORT);

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
  _chromeRef = chrome;                     // 退出时按进程树回收

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
  _cdpRef = cdp;
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

  // 13) 回归：设置页里的自绘下拉必须浮在设置页之上
  //     曾经的 bug：.select-menu 的 z-index(40) 低于 .screen(60)，
  //     菜单虽然弹出了却被整窗设置页盖住 → 「风格 / 人设 / 结尾引导」点了没反应。
  //     判定不能只看菜单宽高（被盖住时依然有尺寸），要看 elementFromPoint 命中的是谁。
  await evalIn(`document.getElementById('btn-open-settings').click(); setSettingsPane('gen'); true`);
  await sleep(320);
  await evalIn(`(() => { const w = document.querySelector('#param-front select').closest('.select-wrap');
    w.querySelector('.select-btn').click(); })()`);
  await sleep(260);
  const dd = await evalIn(`(() => {
    const sel = document.querySelector('#param-front select');
    const menu = [...document.querySelectorAll('body > .select-menu')].find(m => !m.classList.contains('hidden'));
    if (!menu) return { found: false };
    const r = menu.getBoundingClientRect();
    const top = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
    return {
      found: true,
      label: sel.options[sel.selectedIndex] ? sel.options[sel.selectedIndex].textContent : '',
      options: menu.querySelectorAll('.select-opt').length,
      z: getComputedStyle(menu).zIndex,
      onTop: menu.contains(top),
      hit: top ? (top.id || top.className || top.tagName) : null,
    };
  })()`);
  // 桩里 style 只有 2 个选项，故断言按 >=2；关键是「菜单能不能浮在上面」
  results.push(["设置页下拉能展开出选项", dd.found === true && dd.options >= 2,
    "选项数=" + dd.options + " 当前=" + dd.label]);
  results.push(["下拉浮在设置页之上（可点选）", dd.onTop === true,
    "z=" + dd.z + " 命中=" + dd.hit]);
  // 收尾：关掉菜单，避免影响后面的截图
  await evalIn(`document.querySelectorAll('body > .select-menu').forEach(m => m.classList.add('hidden')); true`);

  // 14) 会话列表：「在跑」的会话就是一条普通记录（发起即有记录），不另起分组。
  //     曾经的做法是单独搞一个「生成中」分组，与其他智能体的习惯不一致。
  await evalIn(`currentResult = null; currentJob = { id: 's2', state: 'writing' };
    loadSessions(); true`);
  await sleep(500);
  const sess = await evalIn(`(() => {
    const list = document.getElementById('session-list');
    const labels = [...list.querySelectorAll('.group-lbl')]
      .map(e => (e.firstChild ? e.firstChild.textContent : '').trim());
    const rows = [...list.querySelectorAll('.sess-item')];
    const run = rows.find(r => r.classList.contains('active'));
    return {
      labels, rows: rows.length,
      hasRunningGroup: labels.some(t => t.indexOf('生成中') >= 0),
      sub: run ? run.querySelector('.sess-sub').textContent.trim() : null,
      topic: run ? run.querySelector('.sess-topic').textContent.trim() : null,
    };
  })()`);
  results.push(["不发散出「生成中」分组", sess.hasRunningGroup === false, "分组=" + JSON.stringify(sess.labels)]);
  results.push(["在跑会话与历史同列（共 2 条）", sess.rows === 2, "行数=" + sess.rows]);
  results.push(["在跑会话副标题显示实时状态", /撰写/.test(sess.sub || ""),
    sess.topic + " · " + sess.sub]);
  results.push(["在跑会话高亮为当前", sess.topic === "扶梯突然停了怎么办", String(sess.topic)]);

  // 15) 点回去能重新挂上（attachJob 重建消息流并接回轮询）
  await evalIn(`document.querySelectorAll('#session-list .sess-item.active')[0].click(); true`);
  await sleep(600);
  const reattached = await evalIn(`(() => ({
    hasUserMsg: !!document.querySelector('.msg .bubble.user') ||
                document.querySelectorAll('.msg').length > 0,
    busy: busyNow,
    job: currentJob ? currentJob.id : null,
    topic: document.getElementById('topic').value,
  }))()`);
  results.push(["点回在跑会话 → 重新挂上并恢复生成态",
    reattached.job === 's2' && reattached.busy === true && reattached.hasUserMsg === true,
    JSON.stringify(reattached)]);

  // 15b) 生成中要能看到模型的思考过程（流式）
  const thinkShown = await evalIn(`(() => {
    const b = document.getElementById('think-stream');
    if (!b) return { found: false };
    return {
      found: true,
      hidden: b.classList.contains('hidden'),
      open: b.open,
      title: (b.querySelector('.ts-title') || {}).textContent,
      meta: (b.querySelector('.ts-meta') || {}).textContent,
      body: (b.querySelector('.ts-body') || {}).textContent,
    };
  })()`);
  results.push(["生成中显示思考流（展开）",
    thinkShown.found === true && thinkShown.hidden === false && thinkShown.open === true,
    JSON.stringify(thinkShown).slice(0, 120)]);
  results.push(["思考流标题带阶段名", /文案撰写/.test(thinkShown.title || ""), String(thinkShown.title)]);
  results.push(["思考流有正文内容", (thinkShown.body || "").length > 5, String(thinkShown.body).slice(0, 40)]);
  results.push(["思考流显示进度字数", /字/.test(thinkShown.meta || ""), String(thinkShown.meta)]);

  // 15c) 步骤条不得出现两个「文案撰写」。
  //     曾经把「文案撰写」这一步记在撰写开始之前，而步骤一律按已完成渲染，
  //     于是同时出现「已完成的文案撰写」和进行中的「文案撰写中」。
  const track = await evalIn(`(() => {
    const t = document.querySelector('#step-track');
    return t ? [...t.querySelectorAll('.pstep')].map(e => e.textContent.trim()) : null;
  })()`);
  results.push(["步骤条不重复出现「文案撰写」",
    Array.isArray(track) && track.filter(x => /文案撰写/.test(x)).length === 1,
    JSON.stringify(track)]);
  // 收拾干净：停掉轮询与定时刷新，复位全局态，避免影响后面的截图
  await evalIn(`clearTimeout(pollTimer); clearTimeout(activeRefresh);
    currentJob = null; currentResult = null; setBusy(false); true`);

  // 16) 参数落位：风格 / 人设要在快捷条上直接可选，不该只藏在设置里。
  //     同一个参数不能同时出现在两处 —— 两边的 select 都用 p-<key> 作 id，
  //     重复会让 getParam 只认先出现的那个，另一处改了不生效。
  const paramPlacement = await evalIn(`(() => {
    const bar = document.getElementById('quick-params');
    const barIds = [...bar.querySelectorAll('select')].map(s => s.id);
    const pillCount = bar.querySelectorAll('.select-wrap.pill').length;
    const frontIds = [...document.querySelectorAll('#param-front select')].map(s => s.id);
    return { barIds, pillCount, frontIds };
  })()`);
  results.push(["风格已放到快捷条上", paramPlacement.barIds.includes('p-style'), JSON.stringify(paramPlacement.barIds)]);
  results.push(["设置页不再重复放风格（避免 id 冲突）",
    !paramPlacement.frontIds.includes('p-style'), JSON.stringify(paramPlacement.frontIds)]);
  results.push(["快捷条参数以胶囊形态渲染", paramPlacement.pillCount === paramPlacement.barIds.length,
    "胶囊=" + paramPlacement.pillCount + " select=" + paramPlacement.barIds.length]);
  results.push(["全部参数合计不重不漏",
    new Set([...paramPlacement.barIds, ...paramPlacement.frontIds]).size ===
    paramPlacement.barIds.length + paramPlacement.frontIds.length,
    "条=" + paramPlacement.barIds.length + " 设置=" + paramPlacement.frontIds.length]);

  // 17) 回归：切到别的记录后，消息流与顶端都不能残留上一个会话。
  //     原先 openSession 只 append 不清空，切记录后顶部还挂着上一次生成的内容，
  //     连「正在生成…」气泡都还在。
  await evalIn(`currentJob = null; currentResult = null; attachJob('s2'); true`);
  await sleep(500);
  const nBefore = await evalIn(`document.querySelectorAll('.msg').length`);
  await evalIn(`openSession('s1'); true`);
  await sleep(600);
  const swapped = await evalIn(`(() => ({
    msgs: document.querySelectorAll('.msg').length,
    stale: /正在生成/.test(document.body.innerText || ''),
    head: document.getElementById('rh-title').textContent || '',
  }))()`);
  results.push(["切到别的记录 → 不残留上一会话的气泡",
    nBefore === 2 && swapped.msgs === 2, "切换前 " + nBefore + " 条 → 切换后 " + swapped.msgs + " 条"]);
  results.push(["切到别的记录 → 不再出现「正在生成」", swapped.stale === false, "含'正在生成'=" + swapped.stale]);
  results.push(["顶端标题跟随打开的记录",
    /家用电梯怎么挑/.test(swapped.head) && !/扶梯突然停了/.test(swapped.head),
    "顶端=" + JSON.stringify(swapped.head)]);
  // 收拾干净，避免影响后面的截图
  await evalIn(`clearTimeout(pollTimer); clearTimeout(activeRefresh);
    currentJob = null; currentResult = null; setBusy(false);
    clearStreamMsgs(); document.getElementById('empty').classList.remove('hidden'); true`);

  // 18) 失败态要能重试：给一个「重试」按钮，且带的是失败作业自己的参数
  await evalIn(`currentJob = null; currentResult = null; attachJob('s3'); true`);
  await sleep(700);
  const fail = await evalIn(`(() => {
    const b = document.getElementById('retry-gen');
    return { hasRetry: !!b, label: b ? b.textContent : null,
             err: /生成失败/.test(document.body.innerText || '') };
  })()`);
  results.push(["生成失败时提供「重试」按钮", fail.hasRetry === true, JSON.stringify(fail)]);
  results.push(["失败态显示错误信息", fail.err === true, "含'生成失败'=" + fail.err]);
  // 点重试 → 应带着失败作业的参数重新发起（用 topic 是否在输入框出现来判断不靠谱，
  // 改为拦截 /api/generate 的请求体）
  await evalIn(`window.__GEN__ = []; if (!window.__genW) { window.__genW = true;
    const of = window.fetch.bind(window);
    window.fetch = function (u, o) {
      if (String(u).indexOf('/api/generate') >= 0 && o && o.body) window.__GEN__.push(o.body);
      return of(u, o); }; } true`);
  await evalIn(`(() => { const b = document.getElementById('retry-gen'); if (b) b.click(); })()`);
  await sleep(600);
  const retried = await evalIn(`window.__GEN__`);
  results.push(["点重试 → 用原参数重新发起",
    (retried || []).some(b => b.indexOf('扶梯突然停了怎么办') >= 0), JSON.stringify(retried).slice(0, 120)]);

  // 19) 已完结记录的删除按钮：应弹出确认框，确认后发出 DELETE 并关闭
  await evalIn(`clearTimeout(pollTimer); clearTimeout(activeRefresh);
    currentJob = null; currentResult = null; setBusy(false); loadSessions(); true`);
  await sleep(500);
  await evalIn(`window.__DEL__ = []; if (!window.__delW) { window.__delW = true;
    const of = window.fetch.bind(window);
    window.fetch = function (u, o) { if (o && o.method === 'DELETE') window.__DEL__.push(String(u));
      return of(u, o); }; } true`);
  // 明确挑「已结束」那条（生成中的排在前面，走的是取消而不是 DELETE）
  const clickDel = await evalIn(`(() => {
    const rows = [...document.querySelectorAll('#session-list .sess-item')];
    const t = rows.find(r => {
      const d = r.querySelector('.sess-del');
      return d && d.title.indexOf('删除这条记录') >= 0;
    });
    if (!t) return { found: false, rows: rows.length };
    t.querySelector('.sess-del').click();
    return { found: true, rows: rows.length };
  })()`);
  await sleep(400);
  const dlg = await evalIn(`(() => {
    const d = document.getElementById('confirm-dialog');
    const r = d.getBoundingClientRect();
    const top = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
    return { hidden: d.classList.contains('hidden'), w: Math.round(r.width), h: Math.round(r.height),
             title: document.getElementById('cd-title').textContent,
             onTop: !!(top && d.contains(top)) };
  })()`);
  results.push(["点删除 → 弹出确认框", clickDel.found === true && dlg.hidden === false && dlg.w > 0,
    JSON.stringify(clickDel) + " " + JSON.stringify(dlg).slice(0, 130)]);
  results.push(["确认框浮在最上层可点击", dlg.onTop === true, "命中=" + dlg.onTop]);
  await evalIn(`document.getElementById('cd-yes').click(); true`);
  await sleep(500);
  const delRes = await evalIn(`({ dels: window.__DEL__,
    closed: document.getElementById('confirm-dialog').classList.contains('hidden') })`);
  results.push(["确认后发出删除请求并关闭弹层",
    (delRes.dels || []).some(u => u.indexOf('/api/history/') >= 0) && delRes.closed === true,
    JSON.stringify(delRes)]);

  // 19b) 生成中的记录同样要有删除入口（此前只在已结束时渲染，导致点了没反应）
  await evalIn(`currentJob = null; currentResult = null; loadSessions(); true`);
  await sleep(500);
  const delBtns = await evalIn(`(() => {
    const rows = [...document.querySelectorAll('#session-list .sess-item')];
    return rows.map(r => {
      const d = r.querySelector('.sess-del');
      return { has: !!d, title: d ? d.title : null };
    });
  })()`);
  results.push(["每条记录都有删除入口（含生成中）",
    delBtns.length === 2 && delBtns.every(b => b.has === true),
    JSON.stringify(delBtns)]);
  results.push(["生成中的删除入口语义不同",
    delBtns.some(b => b.title && b.title.indexOf('放弃') >= 0), JSON.stringify(delBtns.map(b => b.title))]);

  // 19c) 删除按钮要「一次点中」：常驻可见 + 鼠标可直达 + 真实单击即弹确认
  //      此前按钮 opacity:0（不悬停看不见，第一下常落在行上），命中区也只有 26px
  await evalIn(`currentJob = null; currentResult = null; loadSessions(); true`);
  await sleep(500);
  const delHit = await evalIn(`(() => {
    const rows = [...document.querySelectorAll('#session-list .sess-item')];
    const t = rows.find(r => { const d = r.querySelector('.sess-del'); return d && d.title.indexOf('删除这条记录') >= 0; });
    if (!t) return { found: false, rows: rows.length };
    const d = t.querySelector('.sess-del');
    const r = d.getBoundingClientRect();
    const cx = Math.round(r.left + r.width / 2), cy = Math.round(r.top + r.height / 2);
    const top = document.elementFromPoint(cx, cy);
    const cs = getComputedStyle(d);
    return { found: true, cx: cx, cy: cy, w: Math.round(r.width), h: Math.round(r.height),
             opacity: parseFloat(cs.opacity), pointer: cs.pointerEvents,
             reachable: !!top && (top === d || d.contains(top)) };
  })()`);
  results.push(["删除按钮不悬停也看得见（opacity>0）",
    delHit.found === true && delHit.opacity > 0, JSON.stringify(delHit).slice(0, 150)]);
  results.push(["删除按钮命中区够大且鼠标可直达",
    delHit.w >= 28 && delHit.h >= 28 && delHit.reachable === true,
    delHit.w + 'x' + delHit.h + ' 可达=' + delHit.reachable]);

  // 先「按下不松」，看按钮会不会跑位。曾经的坑：全局 button:active{transform:scale(.96)}
  // 覆盖了按钮用于垂直居中的 translateY(-50%)，按下瞬间按钮下移 14px 脱离指针，
  // mouseup 落到行上 → click 归属变成「打开记录」，删除确认怎么点都弹不出来。
  await cdp.send("Input.dispatchMouseEvent", { type: "mousePressed", x: delHit.cx, y: delHit.cy, button: "left", clickCount: 1 });
  await sleep(140);
  const held = await evalIn(`(() => {
    const rows = [...document.querySelectorAll('#session-list .sess-item')];
    const t = rows.find(r => { const d = r.querySelector('.sess-del'); return d && d.title.indexOf('删除这条记录') >= 0; });
    const d = t.querySelector('.sess-del');
    const b = d.getBoundingClientRect();
    const e = document.elementFromPoint(${delHit.cx}, ${delHit.cy});
    return { top: Math.round(b.top), stillUnder: !!e && (e === d || d.contains(e)) };
  })()`);
  const expectTop = delHit.cy - Math.round(delHit.h / 2);
  results.push(["按下时删除按钮不位移、仍在指针下",
    Math.abs(held.top - expectTop) <= 3 && held.stillUnder === true,
    "实测top=" + held.top + " 期望≈" + expectTop + " 仍在指针下=" + held.stillUnder]);

  // 真实鼠标单击一次 —— 松开必须仍落在按钮上（成败全看上面那条）
  await cdp.send("Input.dispatchMouseEvent", { type: "mouseReleased", x: delHit.cx, y: delHit.cy, button: "left", clickCount: 1 });
  await sleep(400);
  const firstClickOpened = await evalIn(`document.getElementById('confirm-dialog').classList.contains('hidden') === false`);
  results.push(["真实鼠标单击一次即弹出确认", firstClickOpened === true, "已弹出=" + firstClickOpened]);
  await evalIn(`document.getElementById('cd-no').click(); true`);
  await sleep(250);

  // 19d) 刷新不再整段重建列表：给行打标记，连续刷新三次后标记还在、且没有任何行被移除。
  //      这是上面那个问题的根因——行节点被换掉就等于把用户正要点的按钮抽走了。
  const keepNodes = await evalIn(`(async () => {
    const rows = () => [...document.querySelectorAll('#session-list .sess-item')];
    rows().forEach((r, i) => { r.__probe = 'p' + i; });
    let removed = 0;
    const ob = new MutationObserver(ms => ms.forEach(m => {
      m.removedNodes.forEach(n => { if (n.classList && n.classList.contains('sess-item')) removed++; });
    }));
    ob.observe(document.getElementById('session-list'), { childList: true });
    await loadSessions(); await loadSessions(); await loadSessions();
    await new Promise(r => setTimeout(r, 80));
    ob.disconnect();
    const now = rows();
    return { removed: removed, total: now.length, kept: now.filter(r => r.__probe).length };
  })()`);
  results.push(["刷新列表不重建行节点（按钮不会被抽走）",
    keepNodes.removed === 0 && keepNodes.total > 0 && keepNodes.kept === keepNodes.total,
    JSON.stringify(keepNodes)]);

  // 20) 轮询作业令牌：请求回来时 currentJob 可能已经换成别的作业（或已清空），
  //     旧响应必须丢弃。否则它会把别的作业的进度画到当前会话上，还会用
  //     setTimeout(poll) 凭空复活一条本该停掉的轮询链。
  //     这里刻意让 sGhost 延迟 700ms 再返回：请求正在飞行时用户切到另一个作业，
  //     旧响应回来时 currentJob 已经是别人了。它若照常往下走，会把旧作业的状态
  //     写到新作业头上，还会替新作业渲染出一个「幽灵」结果气泡。
  await evalIn(`clearTimeout(pollTimer); currentJob = null; currentResult = null; activeMsg = null;
    setBusy(false); document.getElementById('empty').classList.add('hidden');
    window.__OF__ = window.__OF__ || window.fetch.bind(window);
    window.__REQ__ = [];
    if (!window.__ghostW) { window.__ghostW = true;
      const og = window.fetch.bind(window);
      window.fetch = function (u, o) {
        const s = String(u);
        if (s.indexOf('/api/jobs/') >= 0) window.__REQ__.push({ u: s, t: Date.now() });
        if (s.indexOf('/api/jobs/sGhost') >= 0) {
          const body = JSON.stringify({
            id: 'sGhost', state: 'done', error: null, params: { topic: '幽灵会话' },
            result: { id: 'sGhost', created_at: '2026-09-12T10:00', pack: 'elevator', pack_draft: false,
              params: { topic: '幽灵会话', pack: 'elevator', duration: 60 },
              quota: { total: 290, hook: 45, body: 190, cta: 55 },
              plan: { angle: '幽灵', hook_type: '反常识', hook_line: '幽灵开场', points: ['幽灵要点'], cta: '关注' },
              sections: [{ type: 'hook', text: '幽灵结果不该出现在这里', subtitle: '' }],
              storyboard: [], scenes: [],
              check: { passed: true, chars_total: 10, target_total: 290, deviation_pct: 0, hard_hits: [] },
              placeholders: [], revisions: [], timings: [], logs: [] } });
          return new Promise(res => setTimeout(() => res(new Response(body,
            { status: 200, headers: { 'Content-Type': 'application/json' } })), 700));
        }
        return og(u, o);
      }; }
    true`);
  await evalIn(`currentJob = { id: 'sGhost', state: 'writing', params: { topic: '幽灵会话' } };
    poll();                                              // 请求此刻在飞行途中
    currentJob = { id: 's2', state: 'queued', params: { topic: '扶梯突然停了怎么办' } };
    true`);                                              // 用户切走了，且没替新作业起轮询
  await sleep(1800);
  const tokenChk = await evalIn(`(() => ({
    state: currentJob && currentJob.state,
    id: currentJob && currentJob.id,
    ghost: (document.body.innerText || '').indexOf('幽灵结果不该出现在这里') >= 0,
    askedGhost: window.__REQ__.filter(r => r.u.indexOf('sGhost') >= 0).length,
  }))()`);
  results.push(["轮询期间确实发出了请求", tokenChk.askedGhost === 1, JSON.stringify(tokenChk)]);
  results.push(["旧作业的状态不得盖到新作业头上",
    tokenChk.id === 's2' && tokenChk.state === 'queued', JSON.stringify(tokenChk)]);
  results.push(["过期响应不再渲染出「幽灵结果」气泡",
    tokenChk.ghost === false, "含幽灵结果=" + tokenChk.ghost]);

  // 20b) 「停止」被后端拒绝时必须说出来。以前这里是 try/catch(_){} 全吞，
  //      用户看到「已放弃本次生成」以为停了，后台其实还在跑并把结果写回来。
  await evalIn(`clearTimeout(pollTimer); activeMsg = addAssistantMsg(); activeMsg.innerHTML = '';
    currentJob = { id: 'sCancel', state: 'writing', params: { topic: '停止失败用例' } };
    const of1 = window.fetch.bind(window);
    window.fetch = function (u, o) {
      if (String(u).indexOf('/cancel') >= 0) {
        return Promise.resolve(new Response(
          JSON.stringify({ detail: '作业状态为 done，不可取消' }),
          { status: 400, headers: { 'Content-Type': 'application/json' } }));
      }
      return of1(u, o);
    };
    true`);
  await evalIn(`abortGeneration(); true`);
  await sleep(700);
  const abortChk = await evalIn(`(() => ({
    toast: document.getElementById('toast').textContent || '',
    shown: !document.getElementById('toast').classList.contains('hidden'),
    jobNull: currentJob === null, busy: busyNow,
  }))()`);
  results.push(["停止被后端拒绝时明确提示，不再静默吞错",
    /停止失败/.test(abortChk.toast) && abortChk.shown === true, JSON.stringify(abortChk).slice(0, 160)]);
  results.push(["停止失败也要解锁界面（不卡在 busy）",
    abortChk.jobNull === true && abortChk.busy === false, JSON.stringify(abortChk).slice(0, 160)]);

  // 20c) 轮询连续失败要收敛：以前是无限 setTimeout 重试，界面永远停在「生成中」，
  //      既看不到进度也发不出下一句，只能重启应用。
  await evalIn(`clearTimeout(pollTimer); currentJob = null; activeMsg = null;
    clearStreamMsgs();          // 清掉此前失败用例留下的气泡，否则 #retry-gen 会重 id
    const of2 = window.fetch.bind(window);
    window.__REQ2__ = 0;
    window.fetch = function (u, o) {
      if (String(u).indexOf('/api/jobs/') >= 0) {
        window.__REQ2__++;
        return Promise.resolve(new Response(JSON.stringify({ detail: '引擎已退出' }),
          { status: 502, headers: { 'Content-Type': 'application/json' } }));
      }
      return of2(u, o);
    };
    currentJob = { id: 'sDown', state: 'writing', params: { topic: '连接中断用例的主题' } };
    pollMisses = 3;                        // 已是第 3 次失败，下一次就该收敛
    poll();
    true`);
  await sleep(900);
  const downChk = await evalIn(`(() => ({
    reqs: window.__REQ2__, jobNull: currentJob === null, busy: busyNow,
    hasRetry: !!document.getElementById('retry-gen'),
    nRetry: document.querySelectorAll('#retry-gen').length,
    body: (document.body.innerText || '').indexOf('连接中断') >= 0,
  }))()`);
  results.push(["轮询持续失败会收敛，不再无限重试",
    downChk.reqs === 1 && downChk.jobNull === true, JSON.stringify(downChk).slice(0, 160)]);
  results.push(["连接中断给出重试入口并解锁界面",
    downChk.hasRetry === true && downChk.nRetry === 1 && downChk.body === true && downChk.busy === false,
    JSON.stringify(downChk).slice(0, 160)]);
  await evalIn(`window.__GEN__ = []; true`);
  await evalIn(`(() => { const b = document.getElementById('retry-gen'); if (b) b.click(); })()`);
  await sleep(500);
  const downRetry = await evalIn(`window.__GEN__.map(b => String(b))`);
  results.push(["连接中断的重试带的是本次真正发出的参数",
    downRetry.some(b => b.indexOf('连接中断用例的主题') >= 0), JSON.stringify(downRetry).slice(0, 140)]);

  // 收尾：把 fetch 与作业状态复位，别污染后面的截图
  await evalIn(`clearTimeout(pollTimer); clearTimeout(activeRefresh);
    currentJob = null; currentResult = null; activeMsg = null; pollMisses = 0;
    setBusy(false); if (window.__OF__) window.fetch = window.__OF__; true`);

  // 21) 样式层回归：状态点 / CSS 变量 / 层级顺序 / 常驻入口 / 副标题避让删除按钮
  await evalIn(`openSession('s1'); true`);
  await sleep(600);
  const cssChk = await evalIn(`(() => {
    const rows = [...document.querySelectorAll('#session-list .sess-item')];
    const dots = rows.map(r => (r.querySelector('.dot') || {}).className || '').map(c => c.replace('dot', '').trim());
    const d = rows.length ? rows[0].querySelector('.dot') : null;
    const dr = d ? d.getBoundingClientRect() : { width: 0, height: 0 };
    const rw = document.querySelector('.card-foot .rw');
    const sub = document.querySelector('#session-list .sess-sub');
    const del = document.querySelector('#session-list .sess-del');
    const sr = sub && sub.getBoundingClientRect();
    const dlr = del && del.getBoundingClientRect();
    return {
      kinds: dots,
      dotW: d ? Math.round(dr.width) : 0, dotH: d ? Math.round(dr.height) : 0,
      dotBg: d ? getComputedStyle(d).backgroundColor : '',
      okSoft: getComputedStyle(document.documentElement).getPropertyValue('--ok-soft').trim(),
      zScreen: parseInt(getComputedStyle(document.getElementById('settings-screen')).zIndex, 10),
      zOverlay: parseInt(getComputedStyle(document.getElementById('confirm-overlay')).zIndex, 10),
      rwOpacity: rw ? parseFloat(getComputedStyle(rw).opacity) : -1,
      // 要比的是「文字实际排到的位置」= 内容盒右边缘，rect 含 padding 会虚报
      subPadRight: sub ? parseFloat(getComputedStyle(sub).paddingRight) : -1,
      overlap: !!(sr && dlr) &&
        Math.round(sr.right - parseFloat(getComputedStyle(sub).paddingRight)) > Math.round(dlr.left) + 1,
    };
  })()`);
  results.push(["会话状态点有实际尺寸（不再隐形）",
    cssChk.dotW >= 4 && cssChk.dotH >= 4 && cssChk.dotBg !== 'rgba(0, 0, 0, 0)',
    cssChk.dotW + 'x' + cssChk.dotH + ' ' + cssChk.dotBg]);
  results.push(["状态点按状态区分（ok / no / run）",
    cssChk.kinds.length > 0 && cssChk.kinds.every(k => ['ok', 'no', 'run'].includes(k)),
    JSON.stringify(cssChk.kinds)]);
  results.push(["--ok-soft 已定义（合格标签底色不丢）",
    cssChk.okSoft.length > 0, JSON.stringify(cssChk.okSoft)]);
  results.push(["确认浮层层级高于设置整窗页",
    cssChk.zOverlay > cssChk.zScreen, "overlay=" + cssChk.zOverlay + " screen=" + cssChk.zScreen]);
  results.push(["单段重写入口平时也看得见（不是 opacity:0）",
    cssChk.rwOpacity > 0 && cssChk.rwOpacity < 1, "opacity=" + cssChk.rwOpacity]);
  results.push(["副标题留出删除按钮的位置且不被压住",
    cssChk.subPadRight >= 28 && cssChk.overlap === false,
    "padding-right=" + cssChk.subPadRight + " 重叠=" + cssChk.overlap]);

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
  // 展开「风格」下拉：确认菜单浮在设置页之上（曾因 z-index 过低而点不到）
  await shot("settings-style-menu.png",
    "(() => { setSettingsPane('gen'); const w = document.querySelector('#param-front select').closest('.select-wrap'); w.querySelector('.select-btn').click(); })()");
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

  cleanupAll();
  process.exit(pass === results.length ? 0 : 1);
})().catch(e => { console.error("FAIL:", e.message); cleanupAll(); process.exit(2); });
