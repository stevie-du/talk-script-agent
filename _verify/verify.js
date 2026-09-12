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
      params:{ topic:'扶梯突然停了怎么办', pack:'elevator', duration:60 }, steps:[],
      stream:{ phase:'文案撰写', reasoning_tail:'正在斟酌开场钩子的表达方式，避免直接报价格……',
               reasoning_len:136, content_len:12 } });
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

  cdp.close(); chrome.kill();
  srv.close();
  process.exit(pass === results.length ? 0 : 1);
})().catch(e => { console.error("FAIL:", e.message); process.exit(2); });
