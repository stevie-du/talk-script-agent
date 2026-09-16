// 界面回归网（零依赖 CDP 驱动）。
//
// 跑法：node _verify/verify.js        （加 VERBOSE=1 打印服务端请求）
//
// 结构：本文件只放「断言」；协议管道在 _verify/lib/cdp.js。
//
// 与前一轮的差别：渲染层改成 ES 模块 + 同源加载后，原来那套「app.js 里的全局变量」
// 断言（busyNow / currentJob / setSettingsPane）失效了。现在统一走
// `window.__ts` —— 那是一个**显式的**自动化接口，不再依赖散落的全局变量。
"use strict";
const path = require("path");
const fs = require("fs");
const os = require("os");
const { freePort, killTree, launchChrome, waitTarget, connect, serve } =
  require("./lib/cdp");

const RENDERER = path.resolve(__dirname, "..", "desktop", "renderer");
const REPO_ROOT = path.resolve(__dirname, "..");
const SHOT_DIR = __dirname;

/**
 * 从 `app/server.py` 里读出**生产那份** CSP 常量。
 *
 * 为什么要费这个劲：CSP 断言最容易变成空转 —— 桩服务不发 CSP 头，
 * 页面就在「没有 CSP」的环境里跑完全部断言，全绿，但什么也没验证。
 * 所以这里把真实值取过来，由桩服务原样下发；解析失败就**直接抛**，
 * 绝不允许「解析不出来 → 静默跳过 CSP 断言」。
 */
function readCsp() {
  const src = fs.readFileSync(path.join(REPO_ROOT, "app", "server.py"), "utf8");
  const m = src.match(/^CSP = \(([\s\S]*?)\)$/m);
  if (!m) throw new Error("没能从 app/server.py 解析出 CSP 常量");
  const parts = [...m[1].matchAll(/"([^"]*)"/g)].map(x => x[1]);
  if (!parts.length) throw new Error("解析出的 CSP 为空");
  return parts.join("");
}

let PORT = 8931;
let CDP_PORT = 9333;
let chromeProc = null, cdp = null, server = null;
let _tmpProfile = "";

function cleanupAll() {
  try { if (cdp) cdp.close(); } catch (_) { /* ignore */ }
  killTree(chromeProc && chromeProc.pid);   // 只要起过就杀，与 cdp 是否连上无关
  try { if (server) server.close(); } catch (_) { /* ignore */ }
  if (_tmpProfile) {
    try { fs.rmSync(_tmpProfile, { recursive: true, force: true }); } catch (_) { /* ignore */ }
  }
}
process.on("exit", cleanupAll);
process.on("SIGINT", () => { cleanupAll(); process.exit(130); });

// ── 桩引擎 ─────────────────────────────────────────────────
// 把 fetch 桩直接塞进 index.html（在 module 脚本之前）：
// 比 CDP 的 addScriptToEvaluateOnNewDocument 更确定 —— 不受进程切换/时序影响。
const RESULT = {
  id: "s1", created_at: "2026-09-13T10:00:00", pack: "elevator", pack_draft: false,
  params: { topic: "家用电梯怎么挑？", pack: "elevator", duration: 60, platform: "抖音",
            style: "口播科普", persona: "维保老师傅", segment: "家用电梯", audience: "业主乘客" },
  quota: { total: 261, hook: 40, body: 171, cta: 50 },
  plan: { angle: "看维保", hook_type: "反常识", hook_line: "钩子", points: ["要点一"], cta: "关注" },
  sections: [
    { type: "hook", text: "买家用电梯，先看这三件事。", subtitle: "先看三件事" },
    { type: "point", text: "第一，看井道尺寸和载重。", subtitle: "井道尺寸" },
    { type: "point", text: "第二，看维保响应时间。", subtitle: "维保响应" },
    { type: "cta", text: "关注我，选梯不踩坑。", subtitle: "关注" },
  ],
  storyboard: [{ shot: "中景 展示电梯", subtitle: "先看三件事", sfx: "轻音乐", note: "自然光" }],
  scenes: [],
  check: {
    passed: true, chars_total: 42, target_total: 261, deviation_pct: -3.2,
    estimated_seconds: 58, duration_target: 60, hard_hits: [],
    soft_hits: [{ word: "最", count: 1 }], dropped_short: ["最"],
    segments: [
      { type: "hook", chars: 13, quota: 40 },
      { type: "point", chars: 12, quota: 85 },
      { type: "point", chars: 11, quota: 85 },
      { type: "cta", chars: 6, quota: 50 },
    ],
    points: 2,
  },
  placeholders: ["{{待补：主力机型载重}}"],
  revisions: [{ round: 1, action: "全文回炉",
                report: { deviation_pct: 24.0, hard_hits: [{ word: "政府补贴", count: 1 }], chars_total: 320 } }],
  timings: [{ start: 0, end: 3 }, { start: 3.5, end: 7 }, { start: 7.5, end: 11 }, { start: 11.5, end: 13 }],
  logs: [{ key: "select", title: "选题策划", ts: "2026-09-13T10:00:01", data: {} }],
};

const META = {
  default_pack: "elevator", model: "glm-4.7", base_url: "https://x/v4",
  has_api_key: true, mock: false, max_concurrent: 4, version: "0.2.0",
  packs: [
    { name: "elevator", display_name: "电梯行业包", draft: false,
      params: {
        segment: { label: "细分领域", default: "家用电梯", options: ["家用电梯", "维保", "加装"] },
        audience: { label: "受众", default: "业主乘客", options: ["业主乘客", "物业业委会"] },
        duration: { label: "时长（秒）", default: 60, options: [15, 30, 60, 90] },
        platform: { label: "平台", default: "抖音", options: ["抖音", "视频号", "小红书"] },
        style: { label: "风格", default: "口播科普", options: ["口播科普", "带货"] },
        persona: { label: "人设", default: "维保老师傅", options: ["维保老师傅", "产品经理"] },
        cta: { label: "结尾引导", default: "关注", options: ["关注", "私信", "留资"] },
      },
      // 后端 param_audit 的桩：桩包里「小红书」没配平台分级词表。
      // 真值由 app/knowledge.py 的 param_audit() 按包配置算出。
      param_audit: { platform: { "小红书": "平台分级词表未定义该平台：只按通用词表校验，平台差异化红线不生效" } } },
    { name: "fitment", display_name: "全屋定制包", draft: true,
      params: { segment: { label: "细分领域", default: "全屋定制", options: ["全屋定制"] } } },
  ],
};

function stubScript() {
  // ⚠ 返回的是**纯 JS**，不带 <script> 包装 —— 它现在由 serve() 作为同源外部
  // 脚本 /_stub.js 提供。以前是内联注入，所以包装标签写在这里；
  // 改成外部脚本时若忘了剥掉，整个文件会以字面量 `<script>` 开头 → 语法错误
  // → 桩完全没跑起来（`__INJECTED__` 是 undefined）。
  return `
window.__INJECTED__ = 1;
window.__errs = [];
addEventListener('error', e => window.__errs.push(String(e.message)));
addEventListener('unhandledrejection', e => window.__errs.push('rej: ' + String(e.reason)));

// CSP 违规监听（P2-5）。桩脚本是 <head> 里的阻塞外部脚本，**先于 body 解析**，
// 所以初始 HTML 里的内联样式/脚本也逃不掉。
// 用 sessionStorage 累计：每次 Page.navigate 都会重跑本脚本，不这么做就只剩
// 最后一次导航的记录，前面几次的违规会被悄悄丢掉。
window.__csp = JSON.parse(sessionStorage.getItem('__csp') || '[]');
addEventListener('securitypolicyviolation', e => {
  window.__csp.push({ directive: e.violatedDirective, blocked: e.blockedURI,
                      sample: (e.sample || '').slice(0, 80) });
  sessionStorage.setItem('__csp', JSON.stringify(window.__csp));
});

// 监听器计数（P2-2）。绑定函数重复调用时，\`onclick =\` 天然幂等、但
// \`addEventListener\` 会**重复挂**（一次点击触发两次）。症状离原因很远，
// 所以要能主动数：调 \`__ts.rebind()\` 前后，注册的监听器数量必须**一个都没多**。
// 桩是 <head> 里的阻塞脚本，早于 main.js 执行 —— 计数从页面第一刻就开始了。
window.__addCount = 0;
(function () {
  var orig = EventTarget.prototype.addEventListener;
  EventTarget.prototype.addEventListener = function (type, fn, opts) {
    window.__addCount++;
    return orig.call(this, type, fn, opts);
  };
})();
(function(){
  var META = ${JSON.stringify(META)};
  // 行业包读坏的情形：&packerr=1（P1-6）。坏包的四个特征会**同时**出现，
  // 桩必须一起改 —— 只改 pack_error 而留着 params，测不出「静默降级」：
  //   display_name 退成目录 slug、params 空、param_audit 也空
  //   （它要审计的数据就是 pack.yaml —— 防线与数据同生共死）。
  var PACKERR = /(^|[?&])packerr=1/.test(location.search);
  if (PACKERR) {
    var _p0 = META.packs[0];
    _p0.pack_error = "pack.yaml 语法有误（while parsing a block collection，第 3 行第 3 列）";
    _p0.display_name = _p0.name;
    _p0.params = {};
    _p0.param_audit = {};
  }
  var RESULT = ${JSON.stringify(RESULT)};
  // 字数配额降级的情形：&quotadeg=1（P2-8）。
  // 行业包没配 quota_table 时引擎按「时长×语速」估一个通用配额 ——
  // 算出来的 quota 数字与真配额**长得一模一样**，只有这个标记能区分。
  var QUOTADEG = /(^|[?&])quotadeg=1/.test(location.search);
  if (QUOTADEG) { RESULT.quota_degraded = true; }
  // 首启引导要测「没配 Key」的情形：用 URL 上的 &nokey=1 切换
  var NOKEY = /(^|[?&])nokey=1/.test(location.search);
  // config.yaml 读坏的情形：&cfgerr=1（文案由后端 _yaml_error_brief 生成，
  // 这里只取形态：原因 + 中文行列号，且**不含**配置正文）
  var CFGERR = /(^|[?&])cfgerr=1/.test(location.search);
  var CONFIG = { base_url:"https://x/v4", model:"glm-4.7", api_key_set:!NOKEY,
                 mock:false, retries:2, timeout:180, max_tokens:16000,
                 temperature:0.7, env_override:false,
                 config_error: CFGERR
                   ? "config.yaml 语法有误（mapping values are not allowed here，第 2 行第 44 列）"
                   : "" };
  var calls = { gen:0, job:0, cancel:0, rewrite:0 };
  var ELEVATOR_DRAFT = true;   // 有状态：转正后变 false，才能验证按钮消失
  window.__calls = calls;
  function mk(o){ return Promise.resolve(new Response(JSON.stringify(o),
    { status:200, headers:{'Content-Type':'application/json'} })); }
  window.fetch = function(u, o){
    var s = String(u);
    var hdrs = (o && o.headers) || {};
    window.__lastHeaders = hdrs;
    // 记录令牌是否真的带上了（回归「渲染层没带 token」这类问题）
    window.__sawToken = !!(hdrs['X-TalkScript-Token'] || hdrs['x-talkscript-token']);
    if (s.indexOf('/api/meta') >= 0) {
      return mk(Object.assign({}, META, { has_api_key: !NOKEY, mock: false }));
    }
    if (s.indexOf('/api/generate') >= 0) {
      calls.gen++; calls.job = 0;
      // 主题里带「失败」就返回一个必然失败的作业，用来覆盖失败态渲染
      var body = {};
      try { body = JSON.parse(o && o.body || '{}'); } catch (_) {}
      window.__lastJobId = /失败/.test(body.topic || '') ? 'jobfail' : 'job1';
      return mk({ job_id: window.__lastJobId });
    }
    if (s.indexOf('/api/jobs/job1/cancel') >= 0) {
      calls.cancel++;
      return mk({ id:'job1', state:'cancelled', params:{} });
    }
    if (s.indexOf('/api/jobs/job1/rewrite_segment') >= 0) {
      calls.rewrite++; calls.job = 0; return mk({ id:'job1', state:'rewriting' });
    }
    if (s.indexOf('/api/jobs/job1') >= 0) {
      calls.job++;
      // 前两次是「撰写中」（带思考流与步骤），之后落到完成
      if (calls.job <= 2) return mk({ id:'job1', state:'writing',
        params:{ topic:'家用电梯怎么挑？', pack:'elevator', duration:60 },
        steps:[{ key:'select', title:'选题策划', ts:'2026-09-13T10:00:01', data:{} },
               { key:'write_r1', title:'文案撰写', ts:'2026-09-13T10:00:05', data:{} }],
        stream:{ phase:'文案撰写', reasoning_tail:'正在斟酌开场钩子……',
                 reasoning_len:136, content_len:12 } });
      return mk({ id:'job1', state:'done',
        params:{ topic:'家用电梯怎么挑？', pack:'elevator', duration:60 },
        steps:[{ key:'select', title:'选题策划', ts:'2026-09-13T10:00:01', data:{} },
               { key:'write_r1', title:'文案撰写', ts:'2026-09-13T10:00:05', data:{} },
               { key:'check_r1', title:'校验·第 1 轮', ts:'2026-09-13T10:00:09', data:{} }],
        result: RESULT });
    }
    if (s.indexOf('/api/jobs/jobfail') >= 0) return mk({ id:'jobfail', state:'failed',
      error:'模型接口连接失败（已重试 3 次）：Server disconnected',
      params:{ topic:'会失败的作业', pack:'elevator', duration:60 }, steps:[] });
    if (s.indexOf('/api/history/') >= 0) return mk(RESULT);
    // 未配置 Key 的模式要模拟「真·首次运行」：没有 Key **也没有历史记录**，
    // 否则 boot() 会按设计跳过自动打开设置（有历史说明不是第一次用）。
    if (s.indexOf('/api/history') >= 0 && NOKEY) return mk([]);
    if (s.indexOf('/api/history') >= 0) return mk([
      { id:'s1', created_at:'2026-09-13 10:00', pack:'elevator',
        topic:'家用电梯怎么挑？', duration:60, chars:42, passed:true, state:'done' },
      { id:'s2', created_at:'2026-09-13 15:30', pack:'elevator',
        topic:'扶梯突然停了怎么办', duration:null, chars:null, passed:null, state:'writing' } ]);
    if (s.indexOf('/api/config/test') >= 0) return mk({ ok:true, model:'glm-4.7', detail:'延迟 320ms' });
    // 必须在 /api/config 的通用匹配之前：indexOf('/api/config') 也会命中 reset
    if (s.indexOf('/api/config/reset') >= 0) return mk({ ok:true, fields:['base_url'] });
    // 知识库只读查看器：包文件清单 + 文件内容
    if (s.indexOf('/api/packs/elevator/export-skill') >= 0) return mk(
      { path:'C:/tmp/agent-skills/elevator', name:'elevator', files:12,
        include_private:false, hints:['已按安全默认排除 private/ 目录（商业信息不外带）。'] });
    // 匹配任意包名的转正：实际请求可能是 fitment（测试里切过包）
    if (s.indexOf('/undraft') >= 0) {
      ELEVATOR_DRAFT = false;                 // 服务端状态真的变了
      return mk({ ok:true, name:'elevator', draft:false });
    }
    if (s.indexOf('/api/packs/elevator/file') >= 0) return mk(
      { rel:'knowledge/topics.md', size:1024, text:'# 选题库 /  / - 家用电梯怎么挑？' });
    // 只在**有 body** 时记录：GET /api/config 会把 __lastConfigBody 覆盖成空，
    // 而 saveSettings 在 POST 之后还会走一次 preloadSettings（内含 GET）。
    if (s.indexOf('/api/config') >= 0) {
      var raw = (o && o.body) || '';
      if (raw) { try { window.__lastConfigBody = JSON.parse(raw); } catch (_) {} }
      return mk(CONFIG);
    }
    if (s.indexOf('/api/packs/') >= 0) return mk({ display_name:'电梯行业包', description:'电梯行业口播脚本包',
      draft:ELEVATOR_DRAFT, checklist:'1. 核对参数 / 2. 核对禁用词',
      files:[{rel:'pack.yaml',size:2048},{rel:'skill.yaml',size:1024},{rel:'knowledge/topics.md',size:5120}] });
    if (s.indexOf('/api/history') >= 0) return mk([]);
    return mk({});
  };
})();
`;
}

// ── 断言工具 ────────────────────────────────────────────────
const results = [];
function check(name, ok, detail) { results.push([name, !!ok, detail || ""]); }

const sleep = ms => new Promise(r => setTimeout(r, ms));

(async function main() {
  PORT = await freePort(8931);
  CDP_PORT = await freePort(9333);
  _tmpProfile = fs.mkdtempSync(path.join(os.tmpdir(), "ts-cprof-"));

  const CSP = readCsp();
  server = await serve(RENDERER, PORT, { inject: stubScript(), csp: CSP });
  chromeProc = launchChrome(CDP_PORT, _tmpProfile);
  const wsUrl = await waitTarget(CDP_PORT);
  cdp = await connect(wsUrl);

  const errs = [];
  cdp.on(o => {
    if (o.method === "Runtime.exceptionThrown") {
      errs.push(o.params.exceptionDetails?.exception?.description ||
                o.params.exceptionDetails?.text || "unknown");
    }
    if (o.method === "Runtime.consoleAPICalled" && o.params.type === "error") {
      errs.push(o.params.args.map(a => a.value || a.description || "").join(" "));
    }
  });

  await cdp.send("Runtime.enable");
  await cdp.send("Page.enable");
  await cdp.send("Page.navigate", { url: `http://127.0.0.1:${PORT}/?token=stubtoken` });
  await sleep(1800);

  const evalIn = async (expr) => {
    const r = await cdp.send("Runtime.evaluate", {
      expression: `(() => { ${expr} })()`,
      awaitPromise: true, returnByValue: true,
    });
    if (r.exceptionDetails) {
      throw new Error(r.exceptionDetails.exception?.description || "eval 失败");
    }
    return r.result.value;
  };

  // ── CSP（P2-5）：先证明测试环境没有比生产宽松 ─────────────
  // 桩服务必须原样下发**生产那份** CSP。如果它不发头，页面就在「没有 CSP」
  // 的环境里跑完全部断言 —— 全绿，但什么也没验证（典型的断言空转）。
  const servedCsp = (await fetch(`http://127.0.0.1:${PORT}/`))
    .headers.get("content-security-policy") || "";
  check("桩服务下发的 CSP 与生产逐字相同（测试环境不宽松于生产）",
    servedCsp === CSP, `桩=${JSON.stringify(servedCsp)}`);
  check("CSP 收紧到 style-src 'self'（没有 'unsafe-inline' 后门）",
    /style-src 'self'/.test(servedCsp) && !/unsafe-inline/.test(servedCsp),
    servedCsp);

  // 正对照：故意造一次内联样式违规。
  // **没有这一步，「零违规」可能只是监听器压根没装上** —— 那才是最容易发生的事。
  //
  // ⚠ 两个实测出来的坑（用 _verify/_csp-probe.js 逐向量测过）：
  //   1. `securitypolicyviolation` 是**异步派发**的 —— 注入完立刻读计数会是 0。
  //      第一版就是这么写的，6 个向量全报「没拦」，而实际拦了 14 次。
  //   2. 一次违规会派发 **2 条**事件（Chromium 重复上报），所以只能断言
  //      「增加了」，不能断言「恰好 +1」。
  //   实测被拦的向量：`setAttribute('style')` / innerHTML 带 style /
  //   insertAdjacentHTML 带 style → `style-src-attr`；
  //   `createElement('style')` → `style-src-elem`；`createElement('script')` → `script-src-elem`。
  //   **CSSOM（`el.style.setProperty`）不被拦** —— 界面里那些动态样式全靠它，
  //   这也正是「收紧 style-src 之后界面照常工作」的原因。
  const cspBefore = await evalIn(`return window.__csp.length;`);
  await evalIn(`const d = document.createElement('div');
    d.setAttribute('style', 'color:red');
    document.body.appendChild(d); d.remove(); return 1;`);
  await sleep(250);                        // 等事件派发，别立刻读
  const cspAfterCtrl = await evalIn(`return window.__csp.length;`);
  const cspLast = await evalIn(`return window.__csp[window.__csp.length - 1] || null;`);
  check("CSP 真的在拦（正对照：故意写一次内联 style，必须被拦下并记到）",
    cspAfterCtrl > cspBefore && /style/.test(cspLast?.directive || ""),
    JSON.stringify({ before: cspBefore, after: cspAfterCtrl, last: cspLast }));

  // ── 绑定函数的幂等守卫（P2-2）────────────────────────────
  // 绑定里混着 `addEventListener`（**会重复挂**，一次点击触发两次）与
  // `onclick =`（天然幂等）。重复挂的症状离原因很远，很难查 —— 所以要能主动数。
  // 修复前守卫只有 `bindShell` 一处有，`bindSettings` / `bindOverlays` /
  // `bindScrollPin` 是裸的（`bindSessionList` 又是第三种写法）。
  // 现在统一走 util.bindOnce，`__ts.rebind()` 把五个绑定函数全再跑一遍。
  const ctrlAdd = await evalIn(`return (function(){
    var n = window.__addCount;
    document.getElementById('st-save').addEventListener('click', function(){});
    return { before: n, after: window.__addCount };
  })()`);
  check("监听器计数器是活的（正对照：手工挂一个必须被数到）",
    ctrlAdd.after === ctrlAdd.before + 1, JSON.stringify(ctrlAdd));

  const rebind = await evalIn(`return (function(){
    var before = window.__addCount;
    window.__ts.rebind();
    return { before: before, after: window.__addCount };
  })()`);
  // 这条断言**同时**覆盖五个绑定函数：任何一个漏了 bindOnce 包装都会多出监听器。
  // （「rebind 真的调到了绑定函数」由变异检验证：去掉任一处的包装，这条立刻红。）
  check("再绑一遍不会重复挂监听器（五个绑定函数都幂等）",
    rebind.after === rebind.before, JSON.stringify(rebind));

  // ── 1) 启动 ──────────────────────────────────────────────
  const boot = await evalIn(`return {
    injected: window.__INJECTED__ === 1,
    ts: !!window.__ts,
    meta: !!window.__ts?.meta,
    packs: document.getElementById('pack').options.length,
    quickPills: document.querySelectorAll('#quick-params .select-wrap').length,
    samples: document.querySelectorAll('#empty-samples .sample-card').length,
    sessions: document.querySelectorAll('#session-list .sess-item').length,
    groups: document.querySelectorAll('#session-list .group-lbl').length,
    errs: window.__errs.slice(),
  };`);
  check("桩已注入且页面脚本为模块化加载", boot.injected && boot.ts, JSON.stringify(boot));
  check("无 JS 运行错误", errs.length === 0, errs.join(" | "));
  check("meta 载入且行业包下拉已填充", boot.meta && boot.packs === 2, `packs=${boot.packs}`);
  check("快捷条渲染出参数胶囊", boot.quickPills >= 5, `pills=${boot.quickPills}`);
  check("空状态示例卡渲染", boot.samples === 4, `samples=${boot.samples}`);
  check("会话列表渲染 2 条并分组", boot.sessions === 2 && boot.groups >= 1,
    `rows=${boot.sessions} groups=${boot.groups}`);
  check("请求带上了访问令牌", await evalIn("return window.__sawToken === true;"), "");

  // ── 2) 状态点三态 ────────────────────────────────────────
  const dots = await evalIn(`return Array.from(document.querySelectorAll('#session-list .sess-item'))
    .map(r => ({ dot: r.querySelector('.dot').className, aria: r.getAttribute('aria-label') || '' }));`);
  check("状态点按状态着色（ok / run）",
    dots.some(d => /dot ok/.test(d.dot)) && dots.some(d => /dot run/.test(d.dot)),
    JSON.stringify(dots));
  check("状态点不只靠颜色（有 aria-label 文案）",
    dots.every(d => d.aria.length > 0), JSON.stringify(dots.map(d => d.aria)));

  // ── 3) 生成全流程 ────────────────────────────────────────
  await evalIn(`const t = document.getElementById('topic');
    t.value = '家用电梯怎么挑？'; t.dispatchEvent(new Event('input', {bubbles:true}));
    document.getElementById('btn-generate').click(); return true;`);
  await sleep(400);
  const running = await evalIn(`return {
    busy: window.__ts.busy, jobId: window.__ts.jobId,
    userMsg: !!document.querySelector('.msg.msg-user .bubble.user'),
    steps: document.querySelectorAll('.msg-assistant .steps .step').length,
    thinkShown: !document.querySelector('#think-stream')?.classList.contains('hidden'),
    btnTitle: document.getElementById('btn-generate').title,
    topicCleared: document.getElementById('topic').value === '',
  };`);
  check("发送后进入生成态（用户气泡 + 助手气泡）",
    running.busy && running.jobId === "job1" && running.userMsg, JSON.stringify(running));
  check("步骤时间线渲染出已完成步骤", running.steps >= 2, `steps=${running.steps}`);
  check("生成中展示流式思考过程", running.thinkShown, "");
  check("生成中发送键变为「停止」", /停止/.test(running.btnTitle), running.btnTitle);
  check("发送后清空输入框", running.topicCleared, "");

  await sleep(2600);   // 等轮询落到 done
  const done = await evalIn(`return {
    busy: window.__ts.busy,
    hasResult: !!window.__ts.result,
    cards: document.querySelectorAll('.script-card').length,
    metrics: document.querySelectorAll('.res-metric-list .m-chip').length,
    quotaChips: Array.from(document.querySelectorAll('.script-card .quota')).map(e => e.textContent),
    tabs: Array.from(document.querySelectorAll('.res-tab .rt-t')).map(n => n.textContent),
    followups: document.querySelectorAll('.followups .fu-chip').length,
    whyBlock: document.querySelectorAll('.banner.why').length,
    softBanner: !!Array.from(document.querySelectorAll('.banner')).find(b => /待确认/.test(b.textContent)),
    dropBanner: !!Array.from(document.querySelectorAll('.banner')).find(b => /单字禁用词/.test(b.textContent)),
    quotaDegBanner: !!Array.from(document.querySelectorAll('.banner')).find(b => /字数配额/.test(b.textContent)),
    verBar: document.querySelectorAll('.ver-bar').length,
  };`);
  check("完成后渲染出结果", done.hasResult && !done.busy, JSON.stringify(done));
  check("分段卡片 4 张", done.cards === 4, `cards=${done.cards}`);
  check("指标速览 4 项", done.metrics === 4, `metrics=${done.metrics}`);
  check("每段字数用后端统计值（12/85 而非前端重算）",
    done.quotaChips.some(t => t.includes("12/85")), JSON.stringify(done.quotaChips));
  // 产物分区由「一条长流里的折叠区」改为 tab：文案 / 分镜 / 字幕 / 合规 / 数据
  check("产物分区改为 5 个 tab（文案/分镜/字幕/合规/数据）",
    done.tabs.join(",") === "文案,分镜,字幕,合规,数据", JSON.stringify(done.tabs));
  check("默认停在「文案」tab（主产物不被分镜挤下去）",
    (await evalIn(`return document.querySelector('.res-tab.on .rt-t').textContent;`)) === "文案",
    await evalIn(`return document.querySelector('.res-tab.on .rt-t').textContent;`));
  check("切换 tab 后对应的产物可见",
    (await evalIn(`document.querySelector('[data-tab="subs"].res-tab').click();
      return !document.querySelector('.res-pane[data-tab="subs"]').classList.contains('hidden')
        && document.querySelector('.res-pane[data-tab="script"]').classList.contains('hidden');`)) === true,
    "字幕 tab");
  // 关键：字幕 tab 预览的**内容**必须与导出的一致且干净。
  // SRT 曾出过「关键词当字幕、句子被砍断」的 bug，而以前只能导出成文件
  // 打开才发现 —— 现在界面里就能看见。
  const subPrev = await evalIn(`return {
    rows: document.querySelectorAll('.sub-table tbody tr').length,
    texts: Array.from(document.querySelectorAll('.sub-table .tx')).map(n => n.textContent),
    srtLines: (window.__ts.result ? window.__ts.exportSrt(window.__ts.result) : '')
      .split(String.fromCharCode(13) + String.fromCharCode(10))
      .filter(l => /^[0-9]+$/.test(l)).length };`);
  check("字幕预览无加粗标记且不为空",
    subPrev.rows >= 1 && subPrev.texts.every(t => t.length > 0 && t.indexOf("**") < 0),
    JSON.stringify(subPrev.texts.slice(0, 3)));
  check("字幕预览行数与导出的 SRT 一致（所见即所得）",
    subPrev.rows === subPrev.srtLines && subPrev.rows >= 1,
    `预览=${subPrev.rows} 导出=${subPrev.srtLines}`);
  await evalIn(`document.querySelector('[data-tab="script"].res-tab').click(); return true;`);
  check("回炉原因可展开（决策解释）", done.whyBlock === 1, "");
  check("单字词被忽略有说明", done.dropBanner, "");
  // 对照组：正常包不该出现配额降级提示（否则提示变成背景噪音）。
  // 对应的正向断言在 12f。
  check("正常包不出现配额降级提示", !done.quotaDegBanner, JSON.stringify(done.quotaDegBanner));
  check("后续建议 chips 出现", done.followups >= 2, `chips=${done.followups}`);
  check("单版本时不显示版本导航", done.verBar === 0, "");

  // 真实导出走的是 `a.href = URL.createObjectURL(blob)` + `a.click()`。
  // 这条路径**没被任何纯函数断言覆盖**（exportSrt/exportMd 是直接调用的），
  // 而 `blob:` 恰好是 CSP 最可能拦下来的一类 URL —— 所以必须真的点一次，
  // 由结尾的「零违规」断言兜住。点了没报错不代表没被拦，所以两边都要看。
  await cdp.send("Page.setDownloadBehavior",
    { behavior: "allow", downloadPath: _tmpProfile }).catch(() => { /* 版本差异，忽略 */ });
  const dl = await evalIn(`return (function(){
    var n = document.querySelector('[data-act="save-srt"]');
    if (!n) return { clicked: false };
    n.click();
    return { clicked: true, toast: document.querySelector('.toast')?.textContent || '' };
  })()`);
  check("导出 SRT 按钮真的点了（覆盖 blob: 下载路径）",
    dl.clicked && /SRT/.test(dl.toast), JSON.stringify(dl));

  // ── 4) 后续建议只预填、不提交 ────────────────────────────
  const before = await evalIn("return window.__calls.gen;");
  await evalIn(`const c = Array.from(document.querySelectorAll('.fu-chip'))
      .find(b => /压缩到/.test(b.textContent)); if (c) c.click(); return true;`);
  await sleep(300);
  const after = await evalIn(`return { gen: window.__calls.gen,
    dur: document.getElementById('p-duration').value,
    stale: !document.getElementById('stale-banner').classList.contains('hidden') };`);
  check("点后续建议只改参数、不自动发起生成", after.gen === before, JSON.stringify(after));
  check("点后续建议改了时长参数", after.dur === "30", `dur=${after.dur}`);

  // ── 5) 换一版 → 版本导航 ─────────────────────────────────
  await evalIn(`document.querySelector('[data-act="revary"]').click(); return true;`);
  await sleep(3200);
  const ver = await evalIn(`return {
    bar: document.querySelectorAll('.ver-bar').length,
    label: document.querySelector('.ver-label')?.textContent || '',
    buttons: document.querySelectorAll('.ver-btn').length,
    cards: document.querySelectorAll('.script-card').length,
    userMsgs: document.querySelectorAll('.msg.msg-user').length,
  };`);
  check("换一版 → 出现版本导航（版本 2 / 2）",
    ver.bar === 1 && /2 \/ 2/.test(ver.label), JSON.stringify(ver));
  check("换一版不新增用户气泡（同一问题下的新版本）", ver.userMsgs === 1, `userMsgs=${ver.userMsgs}`);
  await evalIn(`document.querySelector('.ver-btn[data-d="-1"]').click(); return true;`);
  await sleep(200);
  const back = await evalIn(`return { label: document.querySelector('.ver-label').textContent,
    cards: document.querySelectorAll('.script-card').length };`);
  check("可翻回上一个版本", /1 \/ 2/.test(back.label) && back.cards === 4, JSON.stringify(back));

  // ── 6) 编辑用户消息 ──────────────────────────────────────
  const edit = await evalIn(`const m = document.querySelector('.msg.msg-user');
    m.querySelector('.msg-edit').click();
    const has = !!m.querySelector('.bub-edit');
    m.querySelector('[data-a="cancel"]').click();
    return { has, restored: !!m.querySelector('.bubble.user') && !m.querySelector('.bub-edit') };`);
  check("用户消息可编辑（铅笔 → 文本域 → 取消可恢复）",
    edit.has && edit.restored, JSON.stringify(edit));

  // ── 7) 单段重写入口可用 ──────────────────────────────────
  const rw = await evalIn(`const b = document.querySelector('.card-foot .rw');
    return { disabled: b.disabled, title: b.title };`);
  check("有后台作业时单段重写可用", rw.disabled === false, JSON.stringify(rw));

  // ── 8) 停止生成 ──────────────────────────────────────────
  await evalIn(`const t = document.getElementById('topic');
    t.value = '再生成一次'; t.dispatchEvent(new Event('input', {bubbles:true}));
    document.getElementById('btn-generate').click(); return true;`);
  await sleep(400);
  await evalIn(`document.getElementById('btn-generate').click(); return true;`);  // 现在是停止
  await sleep(900);
  const stopped = await evalIn(`return {
    cancel: window.__calls.cancel,
    busy: window.__ts.busy,
    retry: !!document.querySelector('[data-act="retry"]'),
    text: document.body.innerText.includes('已停止本次生成'),
  };`);
  check("停止会真正通知后端", stopped.cancel >= 1, JSON.stringify(stopped));
  check("停止后界面解锁并给出重试入口",
    !stopped.busy && stopped.retry && stopped.text, JSON.stringify(stopped));

  // ── 9) Esc 停止 ──────────────────────────────────────────
  await evalIn(`const t = document.getElementById('topic');
    t.value = 'Esc 停止测试'; t.dispatchEvent(new Event('input', {bubbles:true}));
    document.getElementById('btn-generate').click(); return true;`);
  await sleep(400);
  await evalIn(`document.dispatchEvent(new KeyboardEvent('keydown',
    { key:'Escape', bubbles:true })); return true;`);
  await sleep(900);
  const escStop = await evalIn("return window.__calls.cancel;");
  check("Esc 可停止生成", escStop >= 2, `cancel=${escStop}`);

  // ── 10) 失败态 ───────────────────────────────────────────
  await evalIn(`window.__ts.newChat();
    const t = document.getElementById('topic');
    t.value = '失败测试'; t.dispatchEvent(new Event('input', {bubbles:true}));
    document.getElementById('btn-generate').click(); return true;`);
  await sleep(1600);
  const failUI = await evalIn(`return {
    actions: document.querySelectorAll('.fail-actions button').length,
    hasRetry: !!document.querySelector('.fail-actions [data-act="retry"]'),
    hasSettings: !!document.querySelector('.fail-actions [data-act="settings"]'),
    busy: window.__ts.busy,
    text: document.body.innerText,
  };`);
  check("失败态渲染出可操作按钮（重试 / 检查设置 / 复制错误）",
    failUI.actions === 3 && failUI.hasRetry && failUI.hasSettings && !failUI.busy,
    JSON.stringify({ a: failUI.actions, busy: failUI.busy }));
  check("失败原因可见", /模型接口连接失败/.test(failUI.text), "");

  // ── 11) 设置页 ───────────────────────────────────────────
  await evalIn(`if (window.__ts.settingsOpen) document.getElementById('btn-close-settings').click();
    return true;`);
  await evalIn(`document.getElementById('btn-open-settings').click(); return true;`);
  await sleep(250);
  const stg = await evalIn(`const s = document.getElementById('settings-screen');
    const r = s.getBoundingClientRect();
    return { open: !s.classList.contains('hidden'),
      covers: Math.round(r.width) === innerWidth && Math.round(r.height) === innerHeight,
      panes: document.querySelectorAll('#settings-screen .stg-pane').length,
      navs: document.querySelectorAll('.stg-nav-item').length,
      on: document.querySelectorAll('.stg-nav-item.on').length,
      genVisible: !document.getElementById('pane-gen').classList.contains('hidden') };`);
  check("设置页铺满窗口且有 6 分区",
    stg.open && stg.covers && stg.panes === 6 && stg.navs === 6,
    JSON.stringify(stg));
  check("默认分区为生成偏好且高亮唯一",
    stg.genVisible && stg.on === 1, JSON.stringify(stg));

  // 滚动容器上提到 .stg-main 后的两条守护。修复前滚动容器是 .stg-pane 自身，
  // 它带 max-width + margin-inline:auto（限宽居中），于是滚动条出现在**居中盒子**
  // 右边而不是页面右边（右侧那道空隙）；归零的对象也必须跟着上提一级。
  const sc = await evalIn(`const s = document.getElementById('settings-screen');
    const main = s.querySelector('.stg-main');
    const pane = document.getElementById('pane-gen');
    const mr = main.getBoundingClientRect(), pr = pane.getBoundingClientRect();
    return { mainOv: getComputedStyle(main).overflowY,
      paneOv: getComputedStyle(pane).overflowY,
      mainW: Math.round(mr.width), paneW: Math.round(pr.width) };`);
  check("设置页的滚动容器是 .stg-main（不是限宽居中的 .stg-pane）",
    sc.mainOv === "auto" && sc.paneOv === "visible",
    JSON.stringify(sc));

  // 为什么**没有**断言守着 setPane() 里那句「滚动归零」：
  // 在**桩环境**下测不出来（两分区都撑高 2400px 也分辨不出）——
  // 手动改 class 切分区时 scrollTop 保留 600，但走 setPane() 立刻变 0：
  // 切分区触发的重渲染让容器溢出消失、被浏览器夹回 0，归不归零看不出差别。
  // 第一版只撑高当前分区时变异注入仍全绿（**假绿**），加上「中途必须仍可滚」
  // 的防空转条件才暴露出来。不可证伪的断言不如不写，故此处只守看得见的那半（CSS）。
  //
  // ⚠ 但**不要**据此删掉 settings.js 里那句 `stgMain.scrollTop = 0` ——
  // 「桩环境分辨不出」≠「那句没用」。**真引擎**下它是可观测的（2026-09-16 实测）：
  //   切「行业包·包内容」滚到底(可滚 289) → 切到**同样可滚**的「模型接口」→ 切回，
  //   有那句 = 0；写成 if(false) = **289**（切回长面板时浏览器会恢复旧滚动位置）。
  // 桩里分辨不出，是因为桩的面板内容太短、切过去就被夹回 0，不是那句代码没用。
  //   → 变异检验换场景的必要性：用不可滚的分区当目标会得到「两者都是 0」的假象。

  await evalIn(`window.__ts.setPane('packinfo'); return true;`);
  await sleep(500);
  const pi = await evalIn(`return { rows: document.querySelectorAll('#pi-files tbody tr').length,
    title: document.getElementById('pi-title').textContent,
    hidden: document.getElementById('pane-packinfo').classList.contains('hidden') };`);
  check("行业包详情进入即拉取文件清单", !pi.hidden && pi.rows === 3, JSON.stringify(pi));

  await evalIn(`window.__ts.setPane('llm'); return true;`);
  await sleep(300);
  const llm = await evalIn(`return {
    temp: document.getElementById('st-temperature')?.value,
    base: document.getElementById('st-baseurl')?.value,
    status: document.getElementById('st-status').textContent,
    cfgErrHidden: document.getElementById('st-cfg-err')?.classList.contains('hidden'),
    cfgErrText: document.getElementById('st-cfg-err')?.textContent || '' };`);
  check("模型接口分区回填 base_url 与温度",
    llm.base === "https://x/v4" && llm.temp !== undefined && llm.temp !== "",
    JSON.stringify(llm));
  check("接口状态显示重试/超时等实际生效值", /重试/.test(llm.status), llm.status);
  // 前端**不许**比后端更严：1.8 是后端接受的合法值（区间 0 ~ 2）。
  // 修复前前端单独一个 if 卡 1.5 → 点保存弹 toast 并 return，
  // 请求根本不发出去，后端那句更宽松的校验永远不会被触发。
  // 这条断言驱动的是**真实行为**（请求体），不是 DOM 属性 ——
  // 曾想断言「min/max 被 JS 写对了」，但 HTML 属性本来就对，
  // 去掉 applyNumericBounds() 也照样绿，属于空转，故弃用。
  await evalIn(`var n = document.getElementById('st-temperature');
    n.value = '1.8'; n.dispatchEvent(new Event('input', {bubbles:true})); return true;`);
  await evalIn(`document.getElementById('st-save').click(); return true;`);
  await sleep(800);
  const t18 = await evalIn(`return window.__lastConfigBody || null;`);
  check("保存请求真的发出去了（未被前端区间拦下）",
    !!(t18 && 'base_url' in t18), JSON.stringify(t18));
  check("前端接受 1.8 采样温度（与后端区间一致，不再比后端更严）",
    !!t18 && t18.temperature === 1.8, JSON.stringify(t18));
  // 还原成默认值，免得影响后面的保存相关用例
  await evalIn(`var n = document.getElementById('st-temperature');
    n.value = '0.7'; n.dispatchEvent(new Event('input', {bubbles:true})); return true;`);
  // 配置正常时不能误报 —— 误报会让这条警示彻底失去可信度
  check("config.yaml 正常时「配置读坏」警示隐藏且无文案",
    llm.cfgErrHidden === true && llm.cfgErrText === '', JSON.stringify(llm));

  // 未保存的输入不被覆盖（修复点：每次打开设置都 preloadSettings 会冲掉编辑）
  await evalIn(`window.__ts.setPane('llm');
    const b = document.getElementById('st-baseurl');
    b.value = 'https://typed-by-user/v1';
    b.dispatchEvent(new Event('input', {bubbles:true})); return true;`);
  await sleep(200);
  await evalIn(`document.getElementById('btn-close-settings').click();
    document.getElementById('btn-open-settings').click(); return true;`);
  await sleep(600);
  const keep = await evalIn("return document.getElementById('st-baseurl').value;");
  check("重开设置不覆盖未保存的输入", keep === "https://typed-by-user/v1", keep);

  // 「恢复默认」：留空保存是无效操作（后端会过滤空串防手滑），
  // 所以回到默认必须是显式动作 —— 承接上面那个被改脏的输入框。
  await evalIn(`window.__ts.setPane('llm');
    document.getElementById('st-reset-baseurl').click(); return true;`);
  await sleep(500);
  const restored = await evalIn("return document.getElementById('st-baseurl').value;");
  check("恢复默认把 base_url 还原为服务端默认值",
    restored === "https://x/v4", restored);

  // 重试 / 超时 / 输出预算：原来只在状态行里展示、无法修改
  const adv = await evalIn(`window.__ts.setPane('llm');
    const t = document.getElementById('st-timeout'); return {
      retries: document.getElementById('st-retries')?.value,
      timeout: t?.value,
      max: document.getElementById('st-maxtokens')?.value,
      editable: !!t && !t.readOnly && !t.disabled };`);
  check("重试 / 超时 / 输出预算可编辑且已回填",
    adv.retries === "2" && adv.timeout === "180" && adv.max === "16000" && adv.editable,
    JSON.stringify(adv));

  await evalIn(`window.__ts.setPane('llm');
    const t = document.getElementById('st-timeout');
    t.value = '60'; t.dispatchEvent(new Event('input', {bubbles:true}));
    document.getElementById('st-reset-adv').click(); return true;`);
  await sleep(500);
  const adv2 = await evalIn("return document.getElementById('st-timeout').value;");
  check("高级项「恢复默认」还原超时值", adv2 === "180", adv2);

  // 知识库 / 技能：不再是占位面板，而是包内文件的只读查看器。
  // 技能面板只列技能相关文件（skill.yaml / rules|patterns|compliance 下的）。
  await evalIn(`window.__ts.setPane('kb'); return true;`);
  await sleep(400);
  const kb = await evalIn(`return {
    rows: document.querySelectorAll('#kb-list .kb-item').length,
    hasPlaceholder: !!document.querySelector('#pane-kb .placeholder') };`);
  check("知识库面板列出包内文件（不再是占位）",
    kb.rows === 3 && !kb.hasPlaceholder, JSON.stringify(kb));

  await evalIn(`[...document.querySelectorAll('#kb-list .kb-item')]
    .find(n => n.firstChild.textContent.includes('knowledge')).click(); return true;`);
  await sleep(300);
  const kbBody = await evalIn(`return {
    title: document.getElementById('kb-title').textContent,
    body: document.getElementById('kb-body').textContent };`);
  check("点击文件显示内容",
    /选题库/.test(kbBody.body) && kbBody.title === "knowledge/topics.md",
    JSON.stringify(kbBody));

  await evalIn(`window.__ts.setPane('skills'); return true;`);
  await sleep(400);
  const sk = await evalIn(`return {
    rows: [...document.querySelectorAll('#skills-list .kb-item')]
            .map(n => n.firstChild.textContent) };`);
  check("技能面板只列技能相关文件",
    sk.rows.length === 1 && sk.rows[0] === "skill.yaml", JSON.stringify(sk));

  // 复制口播：产出的是给提词器用的纯文本，`**` 必须去掉而 `／` 必须保留
  const voice = await evalIn(`return window.__ts.voicePlainText({ sections:[
    {type:'hook', text:'先量房／**井道、底坑**／提前确认'},
    {type:'cta', text:'关注我{{待补：品牌名}}'}] });`);
  check("复制口播去掉加粗标记但保留停顿符",
    !voice.includes("**") && voice.includes("／")
    && voice.includes("井道、底坑") && voice.includes("{{待补：品牌名}}"), voice);

  // 复制错误：技术细节必须带上，否则复制出去根本定位不了问题
  const err = await evalIn(
    `return window.__ts.errorText('生成失败','模型无响应','httpx.ConnectTimeout: 30s');`);
  check("复制错误包含技术细节",
    /生成失败/.test(err) && /模型无响应/.test(err) && /ConnectTimeout/.test(err), err);

  // 结果页：此前「重跑 / 换一版 / 打开文件夹 / 复制 JSON」这 4 个动作零覆盖
  await evalIn(`[...document.querySelectorAll('#session-list .sess-item')]
    .find(n => n.textContent.includes('家用电梯')).click(); return true;`);
  await sleep(700);
  const acts = await evalIn(`return [...document.querySelectorAll('[data-act]')]
    .map(n => n.dataset.act);`);
  check("结果页操作齐全（复制/导出/重跑/换一版/文件夹/JSON）",
    ["copy-voice", "save-srt", "save-md", "rerun", "revary", "reveal", "copy-json"]
      .every(a => acts.includes(a)), JSON.stringify(acts));

  await evalIn(`document.querySelector('[data-act="rerun"]').click(); return true;`);
  await sleep(300);
  const reran = await evalIn("return window.__ts.busy;");
  check("点「重跑」真的发起新一次生成", reran === true, String(reran));
  // 等它跑完，别把状态留给后面的用例
  for (let i = 0; i < 25 && await evalIn("return window.__ts.busy;"); i++) await sleep(300);

  // 剩余三个零覆盖动作：copy-json / regen / reveal
  // copy-json 真会踩的坑是循环引用导致 JSON.stringify 直接抛错
  const j = await evalIn(`try {
    const s = JSON.stringify(window.__ts.result, null, 2);
    JSON.parse(s);
    return { ok: true, len: s.length };
  } catch (e) { return { ok: false, err: String(e) }; }`);
  check("复制 JSON 能序列化当前结果（不因循环引用炸掉）", j.ok && j.len > 50,
    JSON.stringify(j));

  // regen：改参数后应出现「用新参数生成」，点了要真的发起新一次生成
  await evalIn(`const b = document.getElementById('stale-banner');
    const sel = document.getElementById('p-duration');
    if (sel) { sel.value = '90'; sel.dispatchEvent(new Event('change', {bubbles:true})); }
    return true;`);
  await sleep(400);
  const stale = await evalIn(`const b = document.getElementById('stale-banner');
    return { shown: !b.classList.contains('hidden'),
             hasBtn: !!b.querySelector('[data-act="regen"]') };`);
  check("改参数后出现「用新参数生成」提示", stale.shown && stale.hasBtn, JSON.stringify(stale));

  await evalIn(`const b = document.querySelector('#stale-banner [data-act="regen"]');
    if (b) b.click(); return true;`);
  await sleep(300);
  const regen = await evalIn("return window.__ts.busy;");
  check("点「用新参数生成」真的发起新一次生成", regen === true, String(regen));
  for (let i = 0; i < 25 && await evalIn("return window.__ts.busy;"); i++) await sleep(300);

  // reveal：无头环境验证不了「真的打开文件夹」，但至少点下去不能报错
  // 注意 evalIn 会把表达式包进 (() => { ... })()，async IIFE 前必须写 return
  const revealErr = await evalIn(`return (async () => {
    const before = document.querySelectorAll('.toast.bad, .toast.err').length;
    document.querySelector('[data-act="reveal"]')?.click();
    await new Promise(r => setTimeout(r, 300));
    return document.querySelectorAll('.toast.bad, .toast.err').length - before; })()`);
  check("打开文件夹点下去不报错", revealErr === 0, String(revealErr));

  // ── 完整流程：导出技能包（后端有测试，前端流程此前零覆盖）──
  await evalIn(`document.getElementById('btn-open-settings').click();
    window.__ts.setPane('packinfo'); return true;`);
  await sleep(500);
  await evalIn(`document.getElementById('pi-export').click(); return true;`);
  await sleep(600);
  const dbg = await evalIn(`return { pack: document.getElementById('pack').value,
    calls: JSON.stringify(window.__calls || {}) };`);
  const exp = await evalIn(`return document.getElementById('pi-export-hint').textContent;`);
  check("导出技能包流程：提示里给出真实文件数与路径",
    /已导出\s*12\s*个文件/.test(exp) && /agent-skills/.test(exp) && !/undefined/.test(exp),
    exp.slice(0, 90));

  // ── 完整流程：草稿转正（fitment 包是草稿态）──
  await evalIn(`const sel = document.getElementById('pack');
    sel.value = 'fitment'; sel.dispatchEvent(new Event('change', {bubbles:true}));
    window.__ts.setPane('packinfo'); return true;`);
  await sleep(600);
  const beforeUndraft = await evalIn(`return {
    hidden: document.getElementById('pi-undraft').classList.contains('hidden'),
    text: document.getElementById('pi-title').textContent };`);
  check("草稿包才显示「标记为已校对」按钮（非草稿包不显示）",
    beforeUndraft.hidden === false, JSON.stringify(beforeUndraft));

  await evalIn(`document.getElementById('pi-undraft').click(); return true;`);
  await sleep(300);
  await evalIn(`document.getElementById('cd-yes').click(); return true;`);   // 确认弹窗
  await sleep(700);
  const afterUndraft = await evalIn(`return {
    hidden: document.getElementById('pi-undraft').classList.contains('hidden'),
    title: document.getElementById('pi-title').textContent };`);
  check("转正后「标记为已校对」按钮消失（刷新生效）",
    afterUndraft.hidden === true && !/草稿/.test(afterUndraft.title),
    JSON.stringify(afterUndraft));
  await evalIn(`const sel = document.getElementById('pack');
    sel.value = 'elevator'; sel.dispatchEvent(new Event('change', {bubbles:true}));
    return true;`);
  await sleep(300);

  // 检索条几何：图标必须在容器内、input 不能自带描边。
  // 回归的是这一类 bug —— 全局 `input` 规则（styles.css 输入控件节）会给
  // input 加 box-shadow(0.5px 描边) + min-height:32px，而检索条容器只有 30px；
  // 容器若只清了 border/background 而没清 box-shadow/min-height，
  // input 就会在容器里自己画一圈线并撑破容器，看起来像「放大镜跑到搜索框外」。
  // 只断言「输入能过滤」是不够的：功能一直是好的，坏的是外观。
  const GEO = sel => `const box = document.querySelector('${sel}');
    const svg = box.querySelector('svg'), inp = box.querySelector('input');
    const R = e => { const b = e.getBoundingClientRect();
      return { left:b.left, right:b.right, top:b.top, bottom:b.bottom, height:b.height }; };
    const cs = getComputedStyle(inp);
    return { box: R(box), svg: R(svg), inp: R(inp),
             shadow: cs.boxShadow, minH: cs.minHeight, pad: cs.paddingTop };`;
  const geoOK = g => g.shadow === "none" && g.minH === "0px" && g.pad === "0px"
    && g.svg.left > g.box.left && g.svg.right < g.box.right
    && g.inp.left > g.svg.right && g.inp.right <= g.box.right
    && g.inp.height <= g.box.height;

  // ── 设置导航：删掉搜索框之后，返回项 → 列表的间距必须补回来 ──
  // 搜索框曾占着「返回工作区」下面那一行。删掉它是个**布局改动**而不只是删 DOM：
  // 原先 .stg-back 的 margin-bottom 只有 8px（唯一目的是不跟搜索框贴死），
  // 列表容器的 padding-top 是 16px —— 搜索框一走，两者直接叠成 24px。
  // 更要紧的是容器内边距在滚动时留不住（内容能滚进 padding 区），
  // 所以"永久间距"只能由 margin 给。现在 = margin-bottom 16px + padding-top 0：
  // 间距恒定 16px，滚到中间首项也不会贴住返回按钮。
  // 顺带守住「导航不再缺项」：搜索是**唯一**会隐藏导航项的入口，
  // 过滤词残留在框里时用户会看到一份缺项却毫无提示的导航，像个 bug。
  await evalIn(`document.getElementById('btn-open-settings').click(); return true;`);
  await sleep(300);
  const stgNavGeo = await evalIn(`const b = document.querySelector('.stg-back').getBoundingClientRect();
    const s = document.querySelector('.stg-nav-scroll');
    const sr = s.getBoundingClientRect();
    const items = [...document.querySelectorAll('.stg-nav-item')];
    return { 返回项到列表: +(sr.top - b.bottom).toFixed(2),
             列表内上边距: getComputedStyle(s).paddingTop,
             导航项数: items.length,
             被隐藏的项: items.filter(n => n.classList.contains('hidden')).length,
             首项文字: (items[0] || {}).textContent };`);
  check("设置导航：返回项 → 列表恒定 16px，六项全在且无隐藏项（搜索框已删）",
    Math.abs(stgNavGeo.返回项到列表 - 16) <= 0.6
    && stgNavGeo.列表内上边距 === "0px"
    && stgNavGeo.导航项数 === 6 && stgNavGeo.被隐藏的项 === 0,
    JSON.stringify(stgNavGeo));

  // 搜索框要连 DOM 一起删干净 —— 留一个隐藏的空壳，下一个人会以为它还在。
  const stgSearchGone = await evalIn(`return {
    box: !!document.querySelector('.stg-search'),
    input: !!document.getElementById('stg-search') };`);
  check("设置页搜索框已从 DOM 移除（不是只藏起来）",
    stgSearchGone.box === false && stgSearchGone.input === false,
    JSON.stringify(stgSearchGone));

  // 「生成偏好」是**单列整行**的表单：每个字段控件都要占满内容宽。
  // 曾经 #param-front 是 `grid-template-columns: 1fr 1fr`（仓库第一个提交就留下的，
  // 那时它装着好几个「更多参数」，两列正好配对），而 MORE_KEYS 后来只剩 `cta` 一个，
  // 于是「结尾引导」被按在左半格（实测 328px）、右半格空着，夹在一堆整行字段之间
  // —— 读起来就是没对齐。删掉那条覆盖后回到 `.page-card .fg` 的单列整行。
  // 断言口径：除「行业包」行（它的下拉与 详情/新建 并排，共同占满整行）之外，
  // 所有字段控件 + 分段控件 + 字段组容器的宽度都必须等于面板内容宽。
  // 容差 0.6，与左栏那组几何断言一致。
  const genWidths = await evalIn(`window.__ts.setPane('gen');
    const pane = document.getElementById('pane-gen');
    const cs = getComputedStyle(pane);
    const inner = +(pane.getBoundingClientRect().width
      - parseFloat(cs.paddingLeft) - parseFloat(cs.paddingRight)).toFixed(2);
    const packRow = pane.querySelector('.pack-row');
    const W = e => +e.getBoundingClientRect().width.toFixed(2);
    // ⚠ 直接量 #param-front 的子元素，不要量 .select-btn。
    // 这条断言第一版量的是 .select-btn，结果漏掉了「结尾引导」——
    // 它在被测状态里没有被 beautifySelects 换成自绘按钮，于是整条断言空转，
    // 把两列栅格加回去也照样全绿（假绿）。栅格/弹性项就是这些子元素本身，
    // 量它们不依赖下拉被换成了什么形态。
    // （注：这段是 evalIn 的模板字符串，注释里不能出现反引号 —— 会把字符串截断。）
    const front = document.getElementById('param-front');
    const frontKids = [...front.children]
      .map(e => ({ t: (e.textContent || '').trim().slice(0, 6), w: W(e) }));
    const ctrls = [...pane.querySelectorAll('.select-btn, select, textarea')]
      .filter(e => !packRow || !packRow.contains(e))
      .filter(e => !e.classList.contains('native-hidden'))
      .map(e => ({ t: (e.textContent || e.id || e.tagName).trim().slice(0, 5), w: W(e) }));
    const seg = pane.querySelector('.segmented');
    const groups = [...pane.querySelectorAll('.fg, .block-inner')].map(W);
    return { 内容宽: inner, 参数组子项: frontKids, 控件: ctrls,
             分段: seg ? W(seg) : null, 字段组: groups,
             行业包行: packRow ? W(packRow) : null,
             paramFront布局: getComputedStyle(front).display };`);
  check("「生成偏好」字段一律占满内容宽（不留半宽孤儿，如曾经 328px 的「结尾引导」）",
    genWidths.参数组子项.length >= 1                                  // 非空：防假绿
    && genWidths.参数组子项.some(c => /结尾引导/.test(c.t))            // 确实测到了那个字段
    && genWidths.参数组子项.every(c => Math.abs(c.w - genWidths.内容宽) <= 0.6)
    && genWidths.控件.every(c => Math.abs(c.w - genWidths.内容宽) <= 0.6)
    && Math.abs(genWidths.分段 - genWidths.内容宽) <= 0.6
    && genWidths.字段组.every(w => Math.abs(w - genWidths.内容宽) <= 0.6)
    && Math.abs(genWidths.行业包行 - genWidths.内容宽) <= 0.6,
    JSON.stringify(genWidths));

  // 换行业包必须重渲染它带来的那批参数。
  // #param-front 与快捷条胶囊都是**按包**生成的，而 fillPackSelect() 只在
  // 启动 / 新建包 / meta 事件时被调用 —— 「用户在下拉里换包」这条路径曾经**没人接**：
  // 自定义下拉的选中只做 sel.value=… + dispatchEvent('change')，而 document 级那个
  // change 监听只管 updateStale / setCfgHint，不重渲染。
  // 后果不只是"显示旧字段"：cta 这类参数的值域来自**包**，换了包却留着上一个包的取值，
  // 生成时那个值在新包里不存在 → 静默降级（app/knowledge.py 的 param_audit），
  // 用户以为在定制、实际没生效。
  // 期望值由桩里的 META 现算（不硬编码数字）：
  //   胶囊 = 该包 params 里属于 FRONT_KEYS 且有 options 的键数
  //   参数组 = 其余有 options 的键数（MORE_KEYS 只有 cta，所以等价）
  const FRONT_KEYS = ["segment", "audience", "duration", "platform", "style", "persona"];
  const expectFor = name => {
    const p = (META.packs.find(x => x.name === name) || {}).params || {};
    const keys = Object.keys(p).filter(k => p[k] && p[k].options && p[k].options.length);
    return { 胶囊: keys.filter(k => FRONT_KEYS.includes(k)).length,
             参数组: keys.filter(k => !FRONT_KEYS.includes(k)).length };
  };
  // 下拉菜单挂在 body 下、与 .select-wrap 一一对应 —— 这条不变量顺手守住
  // 「重渲染时把旧菜单摘掉」：renderPackParams 曾经只清 innerHTML，不摘菜单。
  const packSwap = await evalIn(`const sel = document.getElementById('pack');
    const snap = () => ({ pack: sel.value,
      胶囊: document.querySelectorAll('#quick-params .select-btn').length,
      参数组: [...document.getElementById('param-front').children].length,
      菜单数: document.querySelectorAll('.select-menu').length,
      下拉数: document.querySelectorAll('.select-wrap').length });
    const first = sel.value;
    const before = snap();
    const other = [...sel.options].map(o => o.value).find(v => v !== first);
    sel.value = other; sel.dispatchEvent(new Event('change', { bubbles: true }));
    const after = snap();
    sel.value = first; sel.dispatchEvent(new Event('change', { bubbles: true }));
    return { before, after, other, 首个: first, 还原: snap() };`);
  check("切换行业包会重渲染参数（不留上一个包的字段/取值，也不留游离菜单）",
    packSwap.after.pack === packSwap.other
    && packSwap.after.胶囊 === expectFor(packSwap.other).胶囊
    && packSwap.after.参数组 === expectFor(packSwap.other).参数组
    && packSwap.after.菜单数 === packSwap.after.下拉数
    && packSwap.还原.pack === packSwap.首个
    && packSwap.还原.胶囊 === expectFor(packSwap.首个).胶囊
    && packSwap.还原.参数组 === expectFor(packSwap.首个).参数组
    && packSwap.还原.菜单数 === packSwap.还原.下拉数,
    JSON.stringify(packSwap));

  await evalIn(`document.getElementById('btn-close-settings').click(); return true;`);
  await sleep(200);

  // 头部模型指示：此前只有进设置页才知道在用哪个模型
  const chip = await evalIn(`const c = document.getElementById('rh-model');
    return { hidden: c.classList.contains('hidden'), text: c.textContent,
             title: c.title };`);
  check("头部显示当前模型（不再只有设置页能看到）",
    !chip.hidden && chip.text === "glm-4.7" && /模型/.test(chip.title), JSON.stringify(chip));

  // 会话搜索（成熟 agent 的标配；历史一多就找不到）
  await evalIn(`const b = document.getElementById('sess-search');
    b.value = '扶梯'; b.dispatchEvent(new Event('input', {bubbles:true})); return true;`);
  await sleep(200);
  const searched = await evalIn(`return {
    rows: document.querySelectorAll('#session-list .sess-item').length,
    text: document.getElementById('session-list').textContent };`);
  check("会话搜索能过滤出匹配项",
    searched.rows === 1 && /扶梯/.test(searched.text), JSON.stringify(searched));

  await evalIn(`const b = document.getElementById('sess-search');
    b.value = '不存在的词'; b.dispatchEvent(new Event('input', {bubbles:true})); return true;`);
  await sleep(200);
  const nomatch = await evalIn(`return document.getElementById('session-list').textContent;`);
  check("搜索无结果时给出提示而不是空白", /没有匹配/.test(nomatch), nomatch.slice(0, 40));
  // 提示必须是「搜索框下面那一行」，不能沉到侧栏底部。
  // 回归的 bug：.sess-empty 也是 #session-list 的子节点，而 #session-list 曾经
  // margin-top: auto 贴底 —— 于是「没有匹配…」飘在 500px 空白之下，像渲染残渣。
  const emptyGeo = await evalIn(`const e = document.querySelector('#session-list .sess-empty');
    const sc = document.querySelector('.left-scroll');
    if (!e) return null;
    return { gap: Math.round(e.getBoundingClientRect().top - sc.getBoundingClientRect().top),
             scH: Math.round(sc.getBoundingClientRect().height) };`);
  check("无结果提示紧贴搜索框下方，不沉底",
    emptyGeo && emptyGeo.gap >= 0 && emptyGeo.gap < 40,
    JSON.stringify(emptyGeo));

  await evalIn(`document.getElementById('sess-search-clear').click(); return true;`);
  await sleep(200);
  const cleared = await evalIn(`return {
    rows: document.querySelectorAll('#session-list .sess-item').length,
    val: document.getElementById('sess-search').value };`);
  check("清空搜索后恢复全部会话", cleared.rows === 2 && cleared.val === "",
    JSON.stringify(cleared));

  const sessGeo = await evalIn(GEO(".sess-search"));
  check("会话搜索：图标在框内、input 无自带描边", geoOK(sessGeo), JSON.stringify(sessGeo));

  // 左栏头部顺序：新建对话在上、搜索在下。
  // 搜索过滤的对象就是会话列表，两者应相邻；中间夹一个「新建对话」会把
  // 「动作」和「被检索的内容」切开（ChatGPT / Cursor 等也是新建在上、搜索紧贴历史）。
  const headOrder = await evalIn(`const top = document.querySelector('.left-top');
    const btn = document.getElementById('btn-new-chat');
    const sea = document.querySelector('.sess-search');
    const sc = document.querySelector('.left-scroll');
    const list = document.getElementById('session-list');
    const lbl = document.querySelector('.group-lbl');
    return {
      order: Array.from(top.children).map(n => n.id || n.className).join(','),
      btnTop: Math.round(btn.getBoundingClientRect().top),
      seaTop: Math.round(sea.getBoundingClientRect().top),
      gapUp: Math.round(sea.getBoundingClientRect().top - btn.getBoundingClientRect().bottom),
      gapToScroll: Math.round(sc.getBoundingClientRect().top - sea.getBoundingClientRect().bottom),
      scrollTop: Math.round(sc.getBoundingClientRect().top),
      listTop: Math.round(list.getBoundingClientRect().top),
      gapToLbl: lbl ? Math.round(lbl.getBoundingClientRect().top - sea.getBoundingClientRect().bottom) : -1 };`);
  check("左栏顺序：新建对话在上、搜索在下",
    headOrder.btnTop < headOrder.seaTop
    && headOrder.order.indexOf('btn-new-chat') < headOrder.order.indexOf('sess-search'),
    JSON.stringify(headOrder));
  // 会话少时列表必须从容器顶部开始 —— 不许被 margin-top: auto 之类推到底部。
  // 桩里只有 2 条会话，一旦贴底这里就是 400+px 的差；改成顶部对齐后两者恒等。
  check("会话少时列表贴搜索框、不沉到侧栏底部",
    headOrder.listTop === headOrder.scrollTop,
    `listTop=${headOrder.listTop} scrollTop=${headOrder.scrollTop}`);
  // 间距要落在搜索框**上方**，这样它读起来是「贴着列表」而不是「贴着按钮」。
  check("搜索框与列表更近、与按钮更远（间距落在上方）",
    headOrder.gapToLbl >= 0 && headOrder.gapToLbl < headOrder.gapUp,
    `距按钮=${headOrder.gapUp} 距首条记录=${headOrder.gapToLbl}`);
  // 搜索框到下方内容的间距必须**只有一个来源**，两态读同一个数：
  //   滚动态 = 滚动区容器上边界 - 搜索框底边（.left-top 的 padding-bottom）
  //   非滚动态 = 第一个分组标签顶边 - 搜索框底边
  // 回归的 bug：第一个分组标签自带 padding-top 12px，于是非滚动态读出
  // 8+12=20px 而滚动态只有 8px —— 上下滚一下间距就变了，读起来像布局在抖。
  check("搜索框下方间距统一 16px（滚动态与非滚动态一致）",
    headOrder.gapToScroll === headOrder.gapToLbl && headOrder.gapToScroll === 16,
    `滚动态=${headOrder.gapToScroll} 非滚动态=${headOrder.gapToLbl}`);
  // 但「量到盒子顶边」还不够 —— 肉眼看到的是**字墨**顶边。
  // 文字在行盒里天生比盒顶低（11px 字号下字体 ascent 12 / 墨迹 ascent 9，
  // 再叠上半行距），回归时这一项实测 20.8px：盒子边 16、看到的 20.8，
  // 而滚动态看到的仍是容器边 16 —— 同一个位置两个间距。
  // 所以这里必须用 canvas 的 TextMetrics 把 em 盒换算到墨迹，量那条真正看得见的边。
  // （药丸的顶边也一并量：它不能被滚动容器裁掉，否则「59」会缺一角。）
  // 容差 1 → 0.6：**这条曾经是 <= 1，于是字墨顶 17 也判绿**，是用户肉眼抓出来的。
  // 根因在 line-height:1 —— 行盒(11px)比内容区(15px)矮，内容区溢出、字墨顶下沉 1px。
  // 改成 line-height:9px 后字墨顶与盒顶/药丸顶/容器顶四者重合，才敢把容差收紧。
  const inkGeo = await evalIn(`const sea = document.querySelector('.sess-search');
    const sc = document.querySelector('.left-scroll');
    const lbl = document.querySelector('.group-lbl');
    const pill = lbl.querySelector('.count');
    const tn = [...lbl.childNodes].find(n => n.nodeType === 3 && n.textContent.trim());
    const rg = document.createRange(); rg.selectNodeContents(tn);
    const c = document.createElement('canvas').getContext('2d');
    const cs = getComputedStyle(lbl);
    c.font = cs.fontWeight + ' ' + cs.fontSize + ' ' + cs.fontFamily;
    const m = c.measureText(tn.textContent.trim());
    const ink = rg.getBoundingClientRect().top
      + (m.fontBoundingBoxAscent - m.actualBoundingBoxAscent);
    const seaB = sea.getBoundingClientRect().bottom;
    return { 字墨: +(ink - seaB).toFixed(2),
             药丸: +(pill.getBoundingClientRect().top - seaB).toFixed(2),
             容器: +(sc.getBoundingClientRect().top - seaB).toFixed(2),
             药丸被裁: pill.getBoundingClientRect().top < sc.getBoundingClientRect().top - 0.01 };`);
  check("搜索框到「本周」字墨顶边 = 16px（字墨/药丸/容器三者都落在 16，且药丸没被裁）",
    Math.abs(inkGeo.字墨 - 16) <= 0.6 && Math.abs(inkGeo.药丸 - 16) <= 0.6
    && Math.abs(inkGeo.容器 - 16) <= 0.6 && inkGeo.药丸被裁 === false,
    JSON.stringify(inkGeo));

  // 「本周」与计数药丸的**墨底**必须齐平。并排的两个字形，肉眼对齐看的是墨迹下边缘，
  // 不是抽象基线 —— 中文与数字的「基线→墨底」关系不同（「本周」墨底比基线低 1px，
  // 「54」墨底就在基线上），拿基线比会得出反的结论（实测基线差 +1 但墨底完全重合）。
  // 这条是 `.group-lbl` 行盒 11px→9px 的连带检查点：文字上移 1px 后墨底 187.5→186.5，
  // 正好与药丸重合（改前是药丸比文字高 1px）。只改行高、不动药丸就会再次错开。
  const inkPair = await evalIn(`const c = document.createElement('canvas').getContext('2d');
    const inkBottom = (el, node) => {
      const rg = document.createRange(); rg.selectNodeContents(node);
      const cs = getComputedStyle(el);
      c.font = cs.fontWeight + ' ' + cs.fontSize + ' ' + cs.fontFamily;
      const m = c.measureText(node.textContent.trim());
      const top = rg.getBoundingClientRect().top
        + (m.fontBoundingBoxAscent - m.actualBoundingBoxAscent);
      return top + m.actualBoundingBoxAscent + m.actualBoundingBoxDescent;
    };
    const lbl = document.querySelector('.group-lbl');
    const pill = lbl.querySelector('.count');
    const tn = [...lbl.childNodes].find(n => n.nodeType === 3 && n.textContent.trim());
    const pn = [...pill.childNodes].find(n => n.nodeType === 3 && n.textContent.trim());
    return { 文字: tn.textContent.trim(), 药丸: pn.textContent.trim(),
             文字墨底: +inkBottom(lbl, tn).toFixed(2),
             药丸墨底: +inkBottom(pill, pn).toFixed(2) };`);
  check("「本周」与计数药丸的墨底齐平（行高改动的连带检查点）",
    Math.abs(inkPair.文字墨底 - inkPair.药丸墨底) <= 0.6,
    JSON.stringify(inkPair));

  // 后续分组标签（更早…）的竖向节奏。桩里只有 1 个分组，硬编码第二个会让测试
  // 依赖运行日期（今天跑是「昨天」、过几天就并进「更早」），所以**克隆现有的标签**
  // 插到第一条记录后面来量 —— 克隆体不是 :first-child，拿到的是真实的
  // margin-top + padding-top，量完立刻移除。
  // 这里守的是上一版改动的一个连带影响：.group-lbl 的行盒从 22px 收到 16px 后，
  // 字墨相对盒顶的偏移 d 由 4.8 降到 0.6，分组之间的视觉间距也跟着少了 4.2px。
  // 判据是"上大下小"——分组头必须贴自己的组，不能读成上一组的尾巴。
  const rhythm = await evalIn(`const list = document.getElementById('session-list');
    const lbl = list.querySelector('.group-lbl');
    const row = list.querySelector('.sess-item');
    const clone = lbl.cloneNode(true);
    row.after(clone);
    const tn = [...clone.childNodes].find(n => n.nodeType === 3 && n.textContent.trim());
    const rg = document.createRange(); rg.selectNodeContents(tn);
    const c = document.createElement('canvas').getContext('2d');
    const cs = getComputedStyle(clone);
    c.font = cs.fontWeight + ' ' + cs.fontSize + ' ' + cs.fontFamily;
    const m = c.measureText(tn.textContent.trim());
    const box = clone.getBoundingClientRect();
    const ink = rg.getBoundingClientRect().top
      + (m.fontBoundingBoxAscent - m.actualBoundingBoxAscent);
    const next = clone.nextElementSibling;
    const out = { 上方: +(ink - row.getBoundingClientRect().bottom).toFixed(2),
                  下方: next ? +(next.getBoundingClientRect().top - box.bottom).toFixed(2) : null,
                  marginTop: cs.marginTop, paddingTop: cs.paddingTop };
    clone.remove();
    return out;`);
  check("分组标签上间距 > 下间距（头贴自己的组），且上方没被行高收紧吃掉",
    rhythm.上方 > rhythm.下方 + 6 && rhythm.上方 >= 18,
    JSON.stringify(rhythm));

  // 左栏竖向节奏统一 16px：品牌线→按钮→分隔线→搜索框→列表，四段都是 16。
  // 原来上三段是 --s3(12)、只有最后一段是 --s4(16)，同一列里两种节奏，
  // 读起来像"上面挤、下面松" —— 没对齐，不是设计。
  // 分隔线是 0.5px 发丝线不占节奏，所以量的是「按钮底→分隔线→搜索框顶」两段。
  // 底部设置区同一套：列表底内边距 16、分隔线→设置按钮 16。
  //   这条线是 .left-foot 的 border-top（不是 .left-sep 那样的独立元素），
  //   CSS 写 0.5px 但 Windows/dpr=1 下 Chrome 会向上取整成 1px 渲染 ——
  //   所以「线底」= border box 顶边 + borderTopWidth，直接拿 foot.top 当线会少算 1px。
  //   （这正是实测 18 的一半来源；另一半是 .nav-item 的 margin-top:1px 在容器边界
  //   叠到了 padding 上，已改由 .nav-sec 的 gap 承担。）
  // 容差收到 0.6：改成精确 16 之后，容差 1 会放过「多 1px」的回归（17 也判绿）。
  const leftRhythm = await evalIn(`const r = s => {
      const b = document.querySelector(s).getBoundingClientRect();
      return { t: +b.top.toFixed(2), b: +b.bottom.toFixed(2) }; };
    const head = r('.left-head'), btn = r('#btn-new-chat'), sep = r('.left-sep'),
          sea = r('.sess-search'), sc = r('.left-scroll');
    const footEl = document.querySelector('.left-foot');
    const foot = footEl.getBoundingClientRect();
    const footBorder = parseFloat(getComputedStyle(footEl).borderTopWidth) || 0;
    return { 线到按钮: +(btn.t - head.b).toFixed(2),
             按钮到分隔线: +(sep.t - btn.b).toFixed(2),
             分隔线到搜索: +(sea.t - sep.b).toFixed(2),
             搜索到列表: +(sc.t - sea.b).toFixed(2),
             分隔线到设置: +(r('#btn-open-settings').t - (foot.top + footBorder)).toFixed(2),
             列表底内边距: getComputedStyle(document.querySelector('.left-scroll')).paddingBottom };`);
  check("左栏竖向节奏统一 16px（含底部设置区）",
    Object.entries(leftRhythm).every(([k, v]) =>
      k === "列表底内边距" ? v === "16px" : Math.abs(v - 16) <= 0.6),
    JSON.stringify(leftRhythm));

  // .nav-item（左栏底部「设置」）与 .stg-nav-item（设置页左导航）是**同一个控件的
  // 两处实例**，规则内容是复制粘贴的，只是类名不同 —— 改一处漏一处，两边就长得不一样。
  // 本次「左栏统一 16px」正是踩了这个坑：只把 .nav-item 的 margin 从 1px 0 改成 0，
  // 设置页那半边残留的 margin: 1px 0 与新加的 .nav-sec gap: 2px 叠成 4px 项间距。
  // 只比**计算值**（margin/padding/圆角/字号…），不比 height：
  // 设置页没打开时 .stg-nav-item 在 display:none 的子树里，height 拿不到 used value。
  // ⚠ 设置页那半边必须挑**未选中**的那一个（`:not(.on)`）：`.stg-nav-item.on`
  // 会带上 font-weight 600 + 选中底色，而左栏的 .nav-item（「设置」入口）没有选中态。
  // 拿选中项去比，比的是「选中态 vs 常态」，必然不等。
  // 这条断言第一版写的是 `document.querySelector('.stg-nav-item')`（第一个），
  // 只在「当前分区恰好不是第一项」时才碰巧通过 —— 加了一条 `setPane('gen')` 的
  // 断言之后，第一项变成选中态，它立刻报红。选中态不是同一件东西，要显式排掉。
  const navTwin = await evalIn(`const a = document.querySelector('.nav-item');
    const b = document.querySelector('.stg-nav-item:not(.on)');
    if (!a || !b) return { err: '缺少导航项（或设置页只剩选中项）' };
    const pick = el => { const s = getComputedStyle(el);
      return { m: s.margin, p: s.padding, r: s.borderRadius, g: s.gap,
               fs: s.fontSize, fw: s.fontWeight, jc: s.justifyContent, bg: s.backgroundColor }; };
    return { 左栏: pick(a), 设置页: pick(b) };`);
  check("左栏导航项与设置页导航项样式同步（同一个控件的两处实例）",
    !navTwin.err && JSON.stringify(navTwin.左栏) === JSON.stringify(navTwin.设置页),
    JSON.stringify(navTwin));

  // 窗口头部线必须贯通：左栏品牌行和右栏头部都是 48px，两边都要有下边框。
  // 回归的 bug：只有 .right-head 有 border-bottom，线画到侧栏边界就断了。
  const headLine = await evalIn(`const l = document.querySelector('.left-head').getBoundingClientRect();
    const r = document.querySelector('.right-head').getBoundingClientRect();
    return {
      lBottom: Math.round(l.bottom), rBottom: Math.round(r.bottom),
      lb: parseFloat(getComputedStyle(document.querySelector('.left-head')).borderBottomWidth) || 0,
      rb: parseFloat(getComputedStyle(document.querySelector('.right-head')).borderBottomWidth) || 0 };`);
  check("窗口头部线贯通：左栏品牌行与右栏头部同高且都有下边框",
    headLine.lBottom === headLine.rBottom && headLine.lb > 0 && headLine.rb > 0,
    JSON.stringify(headLine));

  // 动作区（新建对话）与内容区（搜索 + 列表）之间的分隔线，必须横贯侧栏全宽。
  // 若把线画在 .left-top 的内容宽度里，它会缩成一根断掉的短线，接不上两侧竖线。
  // 容差 1px：#left 自己有一条 0.5px 的 border-right，border-box 宽 276，
  // 内容宽 275.5 —— 分隔线铺到 275 就已经是"铺满"了，不能用 276 去比。
  const sepGeo = await evalIn(`const s = document.querySelector('.left-sep');
    const b = s.getBoundingClientRect();
    const side = document.querySelector('#left');
    return { h: +b.height.toFixed(2), left: Math.round(b.left), right: Math.round(b.right),
             sideContentW: Math.round(side.clientWidth) };`);
  check("动作区与内容区之间有分隔线，且横贯侧栏全宽",
    sepGeo.h > 0 && sepGeo.h <= 1 && sepGeo.left === 0
    && sepGeo.right >= sepGeo.sideContentW - 1,
    JSON.stringify(sepGeo));

  // 两个通栏控件必须同高同圆角。
  // 回归的 bug：.sess-search 硬编码 30px + --r-sm，而新建对话是 --h-btn-lg(32px) + --r-ctl(9px)，
  // 叠放时差 2px 高、1px 圆角 —— 读成失误而不是设计。输入控件应走 --h-ctl / --r-ctl。
  const headSize = await evalIn(`const s = getComputedStyle(document.querySelector('.sess-search'));
    const n = getComputedStyle(document.getElementById('btn-new-chat'));
    return { sh: s.height, nh: n.height, sr: s.borderRadius, nr: n.borderRadius };`);
  check("搜索框与新建对话同高同圆角（输入控件走 --h-ctl / --r-ctl）",
    headSize.sh === headSize.nh && headSize.sr === headSize.nr, JSON.stringify(headSize));

  // 会话行副标题显示行业包的 display_name，不是 slug。
  // 回归的 bug：sessions.js 直接吐 it.pack，界面上出现「elevator」这种内部标识，
  // 而 pack.yaml 里早就有 display_name（ui.js / settings.js 也一直在用）。
  const rowSub = await evalIn(`const r = document.querySelector('#session-list .sess-item');
    return r ? r.querySelector('.sess-sub').textContent : '';`);
  check("会话行副标题用行业包显示名而非 slug",
    /电梯行业包/.test(rowSub) && !/elevator/.test(rowSub), rowSub);

  // 左栏会话列表溢出时必须能滚到首尾。
  // 回归的 bug：.left-scroll 曾用 justify-content: flex-end 贴底 —— flex-end 会让
  // 溢出发生在**顶部**且不计入 scrollHeight（实测 sh==ch、maxScrollTop==0），
  // 于是列表一超过容器高度就彻底滚不动，首项被推到负坐标（实测 -3204px）永远看不到。
  // 桩里只有 2 条会话不会溢出，所以临时插入足量占位项制造溢出，测完移除。
  const scrollGeo = await evalIn(`const sc = document.querySelector('.left-scroll');
    const list = document.getElementById('session-list');
    const dummies = [];
    for (let i = 0; i < 40; i++) {
      const d = document.createElement('div');
      d.className = 'sess-item'; d.style.height = '56px';
      list.appendChild(d); dummies.push(d);
    }
    const first = list.querySelector('.sess-item');
    const last = list.lastElementChild;
    sc.scrollTop = 0;
    const r = {
      canScroll: sc.scrollHeight > sc.clientHeight,
      containerTop: Math.round(sc.getBoundingClientRect().top),
      firstTop: Math.round(first.getBoundingClientRect().top),
    };
    sc.scrollTop = 99999;
    r.maxScrollTop = Math.round(sc.scrollTop);
    r.containerBottom = Math.round(sc.getBoundingClientRect().bottom);
    r.lastBottom = Math.round(last.getBoundingClientRect().bottom);
    r.firstReachable = r.firstTop >= r.containerTop - 1;
    r.lastReachable = r.lastBottom <= r.containerBottom + 1;
    dummies.forEach(d => d.remove());
    sc.scrollTop = 0;
    return r;`);
  check("左栏会话溢出时能滚到首尾（不是滚不动）",
    scrollGeo.canScroll && scrollGeo.maxScrollTop > 0
      && scrollGeo.firstReachable && scrollGeo.lastReachable,
    JSON.stringify(scrollGeo));

  // 导出内容必须断言，不能只断言「不抛错」——
  // 错误的字幕（关键词当字幕、句子被砍断）同样能顺利导出。
  const srt = await evalIn(`return window.__ts.exportSrt({ params:{topic:'测试'},
    sections:[{type:'hook', text:'家里装电梯／装修先封墙／电梯后上门／特别容易卡住／我干维保这行', subtitle:'先封墙'}],
    timings:[{start:0,end:12.5}] });`);
  const cues = srt.split("\r\n").filter(Boolean);
  const nCue = cues.filter(l => /^\d+$/.test(l)).length;
  check("SRT 按停顿符切成多行字幕（不再一段一行）",
    nCue >= 4 && !/subtitle|先封墙/.test(srt.split("\r\n")[2] || ""), `行数=${nCue}`);
  check("SRT 每行不超长且不截断", cues.filter(l => /-->/.test(l) === false && /^\d+$/.test(l) === false)
    .every(l => l.length <= 18 && !l.endsWith("装电")), JSON.stringify(cues.slice(0, 3)));
  check("SRT 时间轴落在段内且递增",
    /^00:00:00,000 --> /.test(cues[1] || "") && /00:00:12,500/.test(cues[cues.length - 2] || ""),
    JSON.stringify(cues.slice(0, 2)));

  const md = await evalIn(`return window.__ts.exportMd({ params:{topic:'测试', duration:60},
    pack:'elevator', sections:[{type:'hook', text:'家里装电梯／装修先封墙'}],
    timings:[{start:0,end:5}] });`);
  check("MD 不再把停顿符挤成一行（／ 变换行）",
    md.includes("家里装电梯\n装修先封墙") && !md.includes("／"), JSON.stringify(md.slice(0, 80)));
  check("MD 缺失字段不输出 undefined", !/undefined/.test(md), md.slice(0, 120));

  // ── 12) 快捷键与浮层 ─────────────────────────────────────
  await evalIn(`if (window.__ts.settingsOpen) document.getElementById('btn-close-settings').click();
    return true;`);
  await sleep(150);
  await evalIn(`document.dispatchEvent(new KeyboardEvent('keydown',
    { key:',', ctrlKey:true, bubbles:true })); return true;`);
  await sleep(250);
  const esc1 = await evalIn(`const open = !document.getElementById('settings-screen').classList.contains('hidden');
    document.dispatchEvent(new KeyboardEvent('keydown', { key:'Escape', bubbles:true }));
    return { open, after: !document.getElementById('settings-screen').classList.contains('hidden') };`);
  check("Ctrl+, 开设置 / Esc 关设置", esc1.open && !esc1.after, JSON.stringify(esc1));

  await evalIn(`document.dispatchEvent(new KeyboardEvent('keydown',
    { key:'\\\\', ctrlKey:true, bubbles:true }));
    const folded = document.getElementById('left').classList.contains('folded');
    document.dispatchEvent(new KeyboardEvent('keydown',
    { key:'\\\\', ctrlKey:true, bubbles:true }));
    return true;`);
  await sleep(200);
  const fold = await evalIn(`return document.getElementById('left').classList.contains('folded');`);
  check("Ctrl+\\ 折叠后能再展开（监听只注册一次）", fold === false, `folded=${fold}`);

  // 折叠态：除左上角那个展开按钮外，左栏不应残留任何可见内容。
  // 回归的 bug：折叠时只隐藏了 .left-head 的子项 / .left-scroll / .left-foot，
  // 漏了 .left-top（搜索 + 新建对话）。左栏宽度归 0 后它仍在布局里，
  // 「新建对话」被挤成 ~26px 宽的竖排「建/对」小黑块挂在展开按钮下面。
  // 判据用「有非零尺寸且不在展开按钮内」—— 子项若在 display:none 的祖先下，
  // 自身 computed display 仍是原值，但 rect 会是 0，所以这个判据是准的。
  await evalIn(`document.dispatchEvent(new KeyboardEvent('keydown',
    { key:'\\\\', ctrlKey:true, bubbles:true })); return true;`);
  await sleep(250);
  const foldLeak = await evalIn(`const left = document.getElementById('left');
    const leaked = [...left.querySelectorAll('*')].filter(n => {
      if (n.closest('#btn-toggle-left')) return false;
      return n.getBoundingClientRect().width > 0;
    }).map(n => n.id || n.className || n.tagName);
    return { folded: left.classList.contains('folded'), leaked: leaked.slice(0, 6) };`);
  check("折叠左栏后只剩展开按钮，无残留内容",
    foldLeak.folded && foldLeak.leaked.length === 0, JSON.stringify(foldLeak));
  await evalIn(`document.dispatchEvent(new KeyboardEvent('keydown',
    { key:'\\\\', ctrlKey:true, bubbles:true })); return true;`);
  await sleep(200);

  // ── 12b) 刷新行业包列表不丢当前选择 ──────────────────────
  // 修复前 fillPackSelect 每次都跳回 default_pack：保存设置或新建包之后，
  // 用户刚选中的行业包会被悄悄换掉（「已切换到新建的行业包」成了空话）。
  await evalIn(`document.getElementById('pack').value = 'fitment';
    document.getElementById('pack').dispatchEvent(new Event('change', {bubbles:true}));
    return true;`);
  await evalIn(`document.getElementById('btn-open-settings').click();
    window.__ts.setPane('llm'); return true;`);
  await sleep(300);
  await evalIn(`document.getElementById('st-save').click(); return true;`);
  await sleep(700);
  const kept = await evalIn(`return {
    pack: document.getElementById('pack').value,
    options: document.getElementById('pack').options.length };`);
  check("保存设置后仍保持选中的行业包（不再跳回默认包）",
    kept.pack === "fitment" && kept.options === 2, JSON.stringify(kept));

  // ── 12c) 未配置模型时的首启引导 ──────────────────────────
  await cdp.send("Page.navigate",
    { url: `http://127.0.0.1:${PORT}/?token=stubtoken&nokey=1` });
  await sleep(1800);
  const firstRun = await evalIn(`return {
    setup: !!document.querySelector('.setup-card'),
    setupBtn: !!document.querySelector('.setup-card [data-act="go"]'),
    settingsOpen: !document.getElementById('settings-screen').classList.contains('hidden'),
    paneLlm: !document.getElementById('pane-llm').classList.contains('hidden'),
    samples: document.querySelectorAll('#empty-samples .sample-card').length,
  };`);
  check("未配置 Key 时显示首启引导卡（含「去配置」）",
    firstRun.setup && firstRun.setupBtn, JSON.stringify(firstRun));
  check("首启自动打开设置并落在「模型接口」分区",
    firstRun.settingsOpen && firstRun.paneLlm, JSON.stringify(firstRun));
  check("引导卡与示例卡共存（不互相顶掉）",
    firstRun.setup && firstRun.samples === 4, JSON.stringify(firstRun));

  // ── 12d) config.yaml 读坏时的警示 ────────────────────────
  // 读坏之后设置页里填的全是**内置默认值**，而状态行会照常说
  // 「已配置 Key · 模型 glm-4.7」—— 不显式提示，用户看不出自己填的
  // base_url 一次都没生效过。这是「静默降级」的典型形态。
  await cdp.send("Page.navigate",
    { url: `http://127.0.0.1:${PORT}/?token=stubtoken&cfgerr=1` });
  await sleep(1800);
  // 显式打开设置并落在「模型接口」—— 这条路径才会调 preloadSettings()，
  // 也就是把 /api/config 的 config_error 变成那行警示的唯一入口。
  await evalIn(`document.getElementById('btn-open-settings').click();
    window.__ts.setPane('llm'); return true;`);
  await sleep(700);
  const cfgErr = await evalIn(`return (function(){
    var n = document.getElementById('st-cfg-err');
    if (!n) return { missing: true };
    var cs = getComputedStyle(n);
    return { hidden: n.classList.contains('hidden'), display: cs.display,
             text: n.textContent, color: cs.color,
             status: document.getElementById('st-status').textContent };
  })()`);
  check("config.yaml 读坏时设置页给出警示（不是静默用默认值）",
    !cfgErr.missing && cfgErr.hidden !== true && cfgErr.display !== 'none'
      && /默认值/.test(cfgErr.text) && /保存/.test(cfgErr.text),
    JSON.stringify(cfgErr));
  // 两个条件各自吃劲：颜色证明用的是 --warn 而不是普通 hint 灰；
  // status 证明警示是**追加**的，没有把原来的状态行顶掉。
  check("警示用 warn 色且不顶掉原状态行",
    cfgErr.color === 'rgb(178, 94, 0)' && /已配置 Key/.test(cfgErr.status),
    JSON.stringify(cfgErr));

  // 回到正常模式，继续后面的布局检查
  await cdp.send("Page.navigate", { url: `http://127.0.0.1:${PORT}/?token=stubtoken` });
  await sleep(1600);
  await evalIn(`document.getElementById('settings-screen').classList.add('hidden'); return true;`);

  // ── 12e) 行业包读坏时的警示 ──────────────────────────────
  // pack.yaml 解析失败时，界面会呈现成「这个包参数很少」：display_name 退成
  // 目录 slug、参数条空掉、param_audit 也空（防线依赖它要审计的数据）。
  // 不标出来的话，用户看到的就是一份「没配好」的包 —— 而真去生成会被引擎
  // 拒绝（409），两件事差得远。
  await cdp.send("Page.navigate",
    { url: `http://127.0.0.1:${PORT}/?token=stubtoken&packerr=1` });
  await sleep(1800);
  const packErr = await evalIn(`return (function(){
    var sel = document.getElementById('pack');
    var n = document.getElementById('pack-err');
    if (!n) return { missing: true };
    var cs = getComputedStyle(n);
    return { opt: sel.options[sel.selectedIndex].textContent,
             hidden: n.classList.contains('hidden'), display: cs.display,
             text: n.textContent, color: cs.color,
             badge: !document.getElementById('pack-badge').classList.contains('hidden'),
             fields: document.querySelectorAll('#param-front .fg, #param-front > *').length };
  })()`);
  check("坏包在包下拉里被标出来（不是静默退成 slug）",
    !packErr.missing && /（损坏）/.test(packErr.opt) && /elevator/.test(packErr.opt),
    JSON.stringify(packErr));
  check("坏包给出人话警示，且含原因与行列号",
    packErr.hidden !== true && packErr.display !== 'none'
      && /不可用/.test(packErr.text) && /pack.yaml/.test(packErr.text)
      && /第 3 行第 3 列/.test(packErr.text),
    JSON.stringify(packErr));
  check("警示用 warn 色（不是普通 hint 灰）",
    packErr.color === 'rgb(178, 94, 0)', JSON.stringify(packErr));
  // 这条是「为什么需要警示」的证据：坏包的参数条**真的是空的**，
  // 界面本身看不出异常 —— 除非有人明确告诉用户。
  check("坏包的参数条是空的（证明不提示就看不出异常）",
    packErr.fields === 0, JSON.stringify(packErr));

  // 对照：正常包不能出现任何警示（否则警示变成背景噪音）
  await cdp.send("Page.navigate", { url: `http://127.0.0.1:${PORT}/?token=stubtoken` });
  await sleep(1600);
  const packOk = await evalIn(`return (function(){
    var sel = document.getElementById('pack');
    var n = document.getElementById('pack-err');
    return { opt: sel.options[sel.selectedIndex].textContent,
             hidden: n.classList.contains('hidden'),
             text: n.textContent };
  })()`);
  check("正常包不出现损坏标记与警示",
    !/（损坏）/.test(packOk.opt) && packOk.hidden === true && packOk.text === "",
    JSON.stringify(packOk));

  await evalIn(`document.getElementById('settings-screen').classList.add('hidden'); return true;`);

  // ── 12f) 字数配额降级时的提示 ────────────────────────────
  // 行业包没配 quota_table 时，引擎按「时长×语速」估一个通用配额。
  // 估出来的 quota 与包作者真配过的**数字长得一模一样**：结果页照常显示
  // 「261 字」、每段卡片照常显示「12/85 字」，用户完全看不出这份配额
  // 没为本行业定制过。这就是本项目一直在整治的静默降级 —— 信号算对了、
  // 也落盘了，但界面不读它等于白算。
  await cdp.send("Page.navigate",
    { url: `http://127.0.0.1:${PORT}/?token=stubtoken&quotadeg=1` });
  await sleep(1600);
  await evalIn(`const t = document.getElementById('topic');
    t.value = '家用电梯怎么挑？'; t.dispatchEvent(new Event('input', {bubbles:true}));
    document.getElementById('btn-generate').click(); return true;`);
  await sleep(2600);
  const qdeg = await evalIn(`return (function(){
    var chips = Array.from(document.querySelectorAll('.script-card .quota'))
      .map(function(e){ return e.textContent; });
    var b = Array.from(document.querySelectorAll('.banner'))
      .find(function(x){ return /字数配额/.test(x.textContent); });
    // ⚠ 横幅缺失时也必须把 chips 带回去 —— 否则下面的断言会在
    // undefined 上取 .length 抛异常，**后面所有断言（含布局组）都不会执行**。
    // 变异检验时踩到过：提示删掉后脚本直接崩在第 1358 行，红是红了，
    // 但红得毫无信息量，还掩盖了后续回归。
    if (!b) return { missing: true, chips: chips };
    var body = b.querySelector('.why-body');
    return { tag: b.tagName, expandable: b.tagName === 'DETAILS' && !!body,
             summary: b.querySelector('summary').textContent,
             body: body ? body.textContent : '',
             chips: chips };
  })()`);
  check("配额降级时结果页给出提示（不是静默用估算值）",
    !qdeg.missing && qdeg.expandable, JSON.stringify(qdeg));
  // 折叠行是给用户看的：说「估的、不是本行业配的」+ 带上具体数字。
  // `quota_table` 是配置键（行话），放在展开后的正文里给包作者看。
  check("折叠行说清配额是估的、不是本行业配的，并带上具体数字",
    /估算/.test(qdeg.summary) && /不是本行业/.test(qdeg.summary)
      && /261/.test(qdeg.summary), JSON.stringify(qdeg.summary));
  check("展开后给出补救办法（怎么补 quota_table）",
    /pack\.yaml/.test(qdeg.body) && /quota_table/.test(qdeg.body), JSON.stringify(qdeg.body));
  // 这条是「为什么需要提示」的证据：降级后的配额数字看着完全正常。
  check("降级配额与正常配额在界面上无法区分（证明不提示就看不出来）",
    qdeg.chips.length === 4 && qdeg.chips.some(function(t){ return /12\/85/.test(t); }),
    JSON.stringify(qdeg.chips));

  await cdp.send("Page.navigate", { url: `http://127.0.0.1:${PORT}/?token=stubtoken` });
  await sleep(1600);
  await evalIn(`document.getElementById('settings-screen').classList.add('hidden'); return true;`);

  // ── 13) 布局 ─────────────────────────────────────────────
  const layout = await evalIn(`return {
    overflowX: document.documentElement.scrollWidth > window.innerWidth + 1,
    scrollBtn: !!document.getElementById('scroll-bottom'),
  };`);
  check("无横向溢出", layout.overflowX === false, "");
  check("存在「回到最新」按钮", layout.scrollBtn, "");

  check("全流程无 JS 错误", errs.length === 0, errs.slice(0, 3).join(" | "));

  // 收口：整轮跑下来（含 6 次 Page.navigate、生成全流程、blob: 导出下载）
  // 除正对照那一次之外，不能再有任何 CSP 违规。
  // 谁以后再往模板里写 style="..."，内联样式会被**静默忽略**（列宽失效但不报错）
  // —— 这条断言就是那种「静默失效」的哨兵。
  const cspAll = await evalIn(`return window.__csp;`);
  check("全流程零 CSP 违规（正对照那几条除外）",
    cspAll.length === cspAfterCtrl,
    JSON.stringify(cspAll.slice(cspAfterCtrl).slice(0, 3)));

  // ── 截图 ─────────────────────────────────────────────────
  const shot = async (name, expr) => {
    if (expr) { await evalIn(expr); await sleep(500); }
    const s = await cdp.send("Page.captureScreenshot", { format: "png" });
    fs.writeFileSync(path.join(SHOT_DIR, name), Buffer.from(s.data, "base64"));
  };
  // 验证对齐：参数条左/右边缘 == 输入框左/右边缘
  const align = await evalIn(`return (() => {
    const qb = document.getElementById('quick-params').getBoundingClientRect();
    const cb = document.getElementById('composer-body').getBoundingClientRect();
    return { qbL: qb.left, cbL: cb.left, qbR: qb.right, cbR: cb.right }; })()`);
  // 圆角语言统一：参数条胶囊不再是 pill（半圆头），与输入框同属圆角矩形
  const radius = await evalIn(`return (() => {
    const pill = document.querySelector('#quick-params .select-btn');
    const more = document.querySelector('#quick-params .qp-more');
    const cb = document.getElementById('composer-body');
    return { pill: getComputedStyle(pill).borderRadius,
             more: getComputedStyle(more).borderRadius,
             cb: getComputedStyle(cb).borderRadius }; })()`);
  // 描边语言统一：三者都是 .07 发丝线；主输入框靠**阴影**突出，
  // 而不是把描边加深（修复前「更多设置」用 .12，比主输入框还深，层级倒置）
  const stroke = await evalIn(`return (() => {
    const g = (sel) => { const n = document.querySelector(sel);
      return n ? getComputedStyle(n).boxShadow : ""; };
    return { pill: g('#quick-params .select-btn'),
             more: g('#quick-params .qp-more'),
             composer: g('#composer-body') }; })()`);
  const hair = (s) => s.indexOf("rgba(0, 0, 0, 0.07) 0px 0px 0px 0.5px") === 0;
  check("胶囊 / 更多设置 / 主输入框 描边一致（.07 发丝线）",
    hair(stroke.pill) && hair(stroke.more) && hair(stroke.composer),
    JSON.stringify(stroke).slice(0, 150));
  check("主输入框靠阴影突出，而非描边加深",
    stroke.composer.split("rgba").length > stroke.pill.split("rgba").length
    && stroke.pill.split("rgba").length === 2,
    `pill=${stroke.pill.split("rgba").length} composer=${stroke.composer.split("rgba").length}`);

  check("参数条与输入框是同一套圆角语言（都不是 pill）",
    !/999px/.test(radius.pill) && !/999px/.test(radius.more)
    && /px/.test(radius.cb), JSON.stringify(radius));

  // 间距：输入框 50px 比胶囊 28px 高不少，8px 太挤
  const gap = await evalIn(`return Math.round(
    document.getElementById('composer-body').getBoundingClientRect().top
    - document.getElementById('quick-params').getBoundingClientRect().bottom);`);
  check("参数条与输入框间距 12px（原 8px 偏挤）", gap === 12, `gap=${gap}`);

  check("参数条与输入框左右对齐（修复前差 6px）",
    Math.abs(align.qbL - align.cbL) < 1 && Math.abs(align.qbR - align.cbR) < 1,
    JSON.stringify(align));

  // ── 12c) 「无行业定制」的可见提示 ─────────────────────────
  // 这些值不报错，只会静默走通用默认（没配 topics_map 的细分领域、缺
  // rate_by_style 的风格、平台词表里没有的平台…）。不标出来的话，用户会
  // 以为在定制、实际没生效 —— 比报错更危险。数据来自后端 param_audit。
  const auditIdle = await evalIn(`const sel = document.getElementById('p-platform');
    const btn = sel.parentNode.querySelector('.select-btn');
    return { value: sel.value, warn: btn.classList.contains('is-warn'), title: btn.title };`);
  check("有行业定制的值不显示警示",
    auditIdle.value === "抖音" && auditIdle.warn === false && auditIdle.title === "平台",
    JSON.stringify(auditIdle));

  const auditMenu = await evalIn(`const sel = document.getElementById('p-platform');
    const wrap = sel.parentNode;
    wrap.querySelector('.select-btn').click();
    return [...wrap._menu.querySelectorAll('.select-opt')].map(d => ({
      v: d.dataset.value, unc: d.classList.contains('uncovered'),
      mark: !!d.querySelector('.opt-warn'), title: d.title }));`);
  const uncOpt = auditMenu.find(o => o.v === "小红书");
  const covOpt = auditMenu.find(o => o.v === "抖音");
  check("菜单里无定制的选项在选中前就带标记",
    uncOpt?.unc === true && uncOpt?.mark === true
      && /平台分级词表未定义/.test(uncOpt?.title || "")
      && covOpt?.unc === false && covOpt?.mark === false,
    JSON.stringify(auditMenu));

  const auditWarn = await evalIn(`const sel = document.getElementById('p-platform');
    [...sel.parentNode._menu.querySelectorAll('.select-opt')]
      .find(d => d.dataset.value === '小红书').click();
    const btn = sel.parentNode.querySelector('.select-btn');
    return { value: sel.value, warn: btn.classList.contains('is-warn'), title: btn.title };`);
  check("选中无定制的值后胶囊变警示色并说明原因",
    auditWarn.value === "小红书" && auditWarn.warn === true
      && /平台分级词表未定义/.test(auditWarn.title),
    JSON.stringify(auditWarn));

  await shot("param-audit.png", `const sel = document.getElementById('p-platform');
    sel.parentNode.querySelector('.select-btn').click(); return true;`);

  const auditBack = await evalIn(`const sel = document.getElementById('p-platform');
    sel.value = '抖音'; sel._syncDropdown();
    return { warn: sel.parentNode.querySelector('.select-btn').classList.contains('is-warn') };`);
  check("换回有定制的值后警示消失", auditBack.warn === false, JSON.stringify(auditBack));
  await shot("main-sidebar.png",
    "window.__ts.newChat(); document.getElementById('btn-generate'); return true;");
  // 折叠态截图：左栏收成 0 宽后只应剩左上角的展开按钮。
  // 回归的 bug 是漏隐藏 .left-top，「新建对话」被挤成竖排「建/对」小黑块。
  await shot("main-folded.png",
    "document.getElementById('btn-toggle-left').click(); return true;");
  await evalIn("document.getElementById('btn-toggle-left').click(); return true;");
  await sleep(300);
  await shot("settings-gen.png",
    "document.getElementById('btn-open-settings').click(); window.__ts.setPane('gen'); return true;");
  await shot("settings-packinfo.png", "window.__ts.setPane('packinfo'); return true;");
  // 草稿态的行业包面板（能看到「标记为已校对」按钮）—— 新建的包本来就是草稿
  await shot("settings-packinfo-draft.png", `const sel = document.getElementById('pack');
    sel.value = 'fitment'; sel.dispatchEvent(new Event('change', {bubbles:true}));
    window.__ts.setPane('packinfo'); return true;`);
  await shot("settings-llm.png", "window.__ts.setPane('llm'); return true;");
  await shot("settings-kb.png", "window.__ts.setPane('kb'); return true;");
  await shot("settings-skills.png", "window.__ts.setPane('skills'); return true;");
  // 结果页此前**从未截过图** —— 审查时看不到它，才把已实现的重跑/换一版/
  // 失败重试/版本导航误判成缺失。补上。
  await shot("result-done.png", `document.getElementById('btn-close-settings').click();
    [...document.querySelectorAll('#session-list .sess-item')]
      .find(n => n.textContent.includes('家用电梯')).click(); return true;`);
  // 失败卡要等作业轮询到 failed 才渲染 —— 上次直接 shot 只拍到空态
  // 必须 dispatch input 事件（只塞 value 不触发监听，send 读不到）
  await evalIn(`window.__ts.newChat();
    const t = document.getElementById('topic');
    t.value = '失败测试'; t.dispatchEvent(new Event('input', {bubbles:true}));
    document.getElementById('btn-generate').click(); return true;`);
  let failedShown = false;
  for (let i = 0; i < 30; i++) {
    failedShown = await evalIn(
      `return !!document.querySelector('.fail-actions [data-act="retry"]');`);
    if (failedShown) break;
    await sleep(300);
  }
  check("失败卡真的渲染出来了（截图不是空态）", failedShown === true, String(failedShown));
  await evalIn(`const d = document.querySelector('#pane-llm, .banner.warn ~ details summary');
    if (d && d.tagName === 'SUMMARY') d.parentElement.open = true; return true;`);
  await shot("result-failed.png");
  // 产物 tab 化后的界面：文案 tab（默认）与字幕 tab（导出前可预览）
  await shot("result-tabs-subs.png", `window.__ts.newChat();
    [...document.querySelectorAll('#session-list .sess-item')]
      .find(n => n.textContent.includes('家用电梯')).click(); return true;`);

  // ── 输出 ─────────────────────────────────────────────────
  console.log("\n════════ 界面回归结果 ════════");
  let pass = 0;
  for (const [name, ok, detail] of results) {
    console.log(`${ok ? "✅" : "❌"}  ${name}${detail ? "  — " + detail : ""}`);
    if (ok) pass++;
  }
  console.log(`\n${pass}/${results.length} 通过`);
  cleanupAll();
  process.exit(pass === results.length ? 0 : 1);
})().catch(e => {
  console.error("FAIL:", e.message);
  if (results.length) {
    console.error("\n──────── 失败前已执行的断言 ────────");
    for (const [name, ok, detail] of results) {
      console.error(`${ok ? "✅" : "❌"}  ${name}${detail ? "  — " + detail : ""}`);
    }
  }
  console.error(e.stack);
  cleanupAll();
  process.exit(2);
});
