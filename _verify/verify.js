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
  // 哪些 LLM 字段还是内置默认（config.yaml 里没写、环境变量也没有）。
  //
  // ⚠ 桩必须**有状态**，且状态要按「文件里存了什么」来算 —— 不能写成
  // 「保存过一次就清空」。模型从一条变成一份列表之后，「没配过」是**逐字段**
  // 的判断：用户只填了 Key、没动请求地址时，地址仍然是内置默认。
  // 按「保存过就全清」写的话，「保存后小标消失」这类断言会**假绿** ——
  // 它测的是一个与真实后端不同的口径。
  var CONFIGURED = !NOKEY && !CFGERR;
  var DEFAULT_URL = "https://x/v4", DEFAULT_NAME = "glm-4.7";
  // 原始条目：**空字段保持空**（与真实后端一致 —— 只有空 base_url 才报「内置默认」）
  var MODELS = [{ id: "m-default", name: "",
                  base_url: CONFIGURED ? DEFAULT_URL : "",
                  api_key: CONFIGURED ? "sk-stub" : "",
                  model: CONFIGURED ? DEFAULT_NAME : "" }];
  var ACTIVE = "m-default";
  var savedNum = CONFIGURED ? { temperature:1, retries:1, timeout:1, max_tokens:1 } : {};
  function activeRaw() {
    return MODELS.filter(function(x){ return x.id === ACTIVE; })[0] || MODELS[0];
  }
  function publicModels() {
    return MODELS.map(function(r){
      var d = [];
      if (!r.base_url) d.push("base_url");
      if (!r.model) d.push("model");
      return { id: r.id, name: r.name, label: (r.name || r.model || DEFAULT_NAME),
               base_url: (r.base_url || DEFAULT_URL), model: (r.model || DEFAULT_NAME),
               api_key_set: !!r.api_key, active: r.id === ACTIVE, defaulted: d };
    });
  }
  function llmDefaulted() {
    var d = ["temperature","retries","timeout","max_tokens"].filter(function(k){ return !savedNum[k]; });
    var r = activeRaw();
    if (!r.base_url) d.push("base_url");
    if (!r.model) d.push("model");
    return d;
  }
  function hasKey(){ return !!activeRaw().api_key; }
  function err(code, detail) {
    return Promise.resolve(new Response(JSON.stringify({ detail: detail }),
      { status: code, headers: { 'Content-Type': 'application/json' } }));
  }
  function configBody() {
    var r = activeRaw();
    return { base_url: (r.base_url || DEFAULT_URL), model: (r.model || DEFAULT_NAME),
             api_key_set: hasKey(), mock: false, retries: 2, timeout: 180,
             max_tokens: 16000, temperature: 0.7, env_override: false,
             defaults: { base_url: DEFAULT_URL, model: DEFAULT_NAME },
             llm_defaulted: llmDefaulted(),
             models: publicModels(), active_model: ACTIVE,
             config_error: CFGERR
               ? "config.yaml 语法有误（mapping values are not allowed here，第 2 行第 44 列）"
               : "" };
  }
  var calls = { gen:0, job:0, cancel:0, rewrite:0 };
  var ELEVATOR_DRAFT = true;   // 有状态：转正后变 false，才能验证按钮消失
  window.__calls = calls;
  // 空态 hero 的两套文案**都在 DOM 里**（由 [data-when] 切换），
  // 直接读 h3.textContent 会把两态拼在一起（「想聊点什么？先配置模型接口」）——
  // 那样不管哪种状态，两个正则都能匹配上，断言等于没写。
  // 所以要取**当前可见的那一份**。
  window.__shown = function(root){
    if (!root) return '';
    var n = Array.prototype.find.call(root.children,
      function(c){ return c.nodeType === 1 && !c.classList.contains('hidden'); });
    return n ? n.textContent.trim() : '';
  };
  function mk(o){ return Promise.resolve(new Response(JSON.stringify(o),
    { status:200, headers:{'Content-Type':'application/json'} })); }
  window.fetch = function(u, o){
    var s = String(u);
    var hdrs = (o && o.headers) || {};
    window.__lastHeaders = hdrs;
    // 记录令牌是否真的带上了（回归「渲染层没带 token」这类问题）
    window.__sawToken = !!(hdrs['X-TalkScript-Token'] || hdrs['x-talkscript-token']);
    if (s.indexOf('/api/meta') >= 0) {
      return mk(Object.assign({}, META,
        { has_api_key: hasKey(), mock: false, llm_defaulted: llmDefaulted(),
          models: publicModels(), active_model: ACTIVE }));
    }
    // ── 模型列表接口 ──
    // 三个具体路径必须排在「/api/models」的通用匹配**之前** ——
    // indexOf('/api/models') 会一并命中 activate / delete，
    // 顺序反了会让「切换当前模型」走进 upsert 分支（凭空多出一条模型）。
    if (s.indexOf('/api/models/activate') >= 0) {
      var ab = {};
      try { ab = JSON.parse(o && o.body || '{}'); } catch (_) {}
      if (!MODELS.some(function(x){ return x.id === ab.id; })) {
        return err(404, '没有这个模型：' + ab.id);
      }
      ACTIVE = ab.id;
      window.__lastActivate = ab.id;
      return mk({ ok:true, active_model:ACTIVE, models:publicModels() });
    }
    if (s.indexOf('/api/models/delete') >= 0) {
      var db = {};
      try { db = JSON.parse(o && o.body || '{}'); } catch (_) {}
      if (MODELS.length <= 1) return err(400, '至少要保留一个模型');
      var before = MODELS.length;
      MODELS = MODELS.filter(function(x){ return x.id !== db.id; });
      if (MODELS.length === before) return err(404, '没有这个模型：' + db.id);
      if (ACTIVE === db.id) ACTIVE = MODELS[0].id;
      return mk({ ok:true, active_model:ACTIVE, models:publicModels() });
    }
    if (s.indexOf('/api/models') >= 0) {
      var mb = {};
      try { mb = JSON.parse(o && o.body || '{}'); } catch (_) {}
      window.__lastModelBody = mb;
      var mid = String(mb.id || '');
      var target = null;
      if (mid) {
        target = MODELS.filter(function(x){ return x.id === mid; })[0];
        if (!target) return err(404, '没有这个模型：' + mid);
      } else {
        var n = 1;
        while (MODELS.some(function(x){ return x.id === 'm' + n; })) n++;
        mid = 'm' + n;
        target = { id:mid, name:'', base_url:'', api_key:'', model:'' };
        MODELS.push(target);
      }
      if (!String(mb.model || '').trim()) return err(400, '模型 ID 不能为空');
      var burl = String(mb.base_url || '').trim().replace(/[/]+$/, '');
      // ⚠ 这里刻意用 indexOf 而不是正则：桩整体是一个模板串，
      // 正则里的 \/ 会被模板串吃掉，剩下的 // 会把后面整行变成注释。
      if (burl && burl.indexOf('http://') !== 0 && burl.indexOf('https://') !== 0) {
        return err(400, '请求地址要以 http:// 或 https:// 开头');
      }
      target.name = String(mb.name || '').trim();
      target.model = String(mb.model || '').trim();
      // 留空 = 保持不变（编辑）/ 用内置默认（新增）—— 与真实后端同一语义
      if (burl) target.base_url = burl;
      if (String(mb.api_key || '').trim()) target.api_key = String(mb.api_key).trim();
      return mk({ ok:true, id:mid, models:publicModels() });
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
      if (raw) {
        try { window.__lastConfigBody = JSON.parse(raw); } catch (_) {}
        var cb = window.__lastConfigBody || {};
        // 数值项存过之后就不再是「内置默认」—— 逐项记，不整体清空。
        ["temperature","retries","timeout","max_tokens"].forEach(function(k){
          if (cb[k] !== undefined && cb[k] !== null && cb[k] !== "") savedNum[k] = 1;
        });
        // 老的连接信息入口（curl / 旧渲染层）：改的是**当前模型条目**，
        // 不是第二份数据 —— 与真实后端一致。
        if (cb.base_url) activeRaw().base_url = String(cb.base_url).replace(/[/]+$/, '');
        if (cb.model) activeRaw().model = cb.model;
        if (cb.api_key) activeRaw().api_key = cb.api_key;
      }
      return mk(configBody());
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

  // ── 截图辅助（**定义在最前面**，任何一步都能用）───────────
  // 原来它定义在脚本末尾的「截图区」，于是流程中段想拍一张图只能用裸 cdp 重写一遍
  // （生成中那一态就这么绕过两次）。它只依赖 evalIn / sleep / cdp / fs / SHOT_DIR，
  // 全部在脚本开头就绪，没有任何理由放在后面。
  // clip 可选：给局部特写用（整屏图上工具条只有几十像素高，看不清细节）。
  const shot = async (name, expr, clip) => {
    if (expr) { await evalIn(expr); await sleep(500); }
    const s = await cdp.send("Page.captureScreenshot",
      clip ? { format: "png", clip } : { format: "png" });
    fs.writeFileSync(path.join(SHOT_DIR, name), Buffer.from(s.data, "base64"));
  };
  // 自绘下拉的菜单挂在 body 下，截图时会盖住底下的控件（它不受任何容器裁剪）。
  // 整屏截图前统一收掉，否则「首屏长什么样」这张图永远带着一个展开的菜单。
  const closeMenus = `document.querySelectorAll('.select-wrap.open').forEach(w => w.classList.remove('open'));
    document.querySelectorAll('.select-menu:not(.hidden)').forEach(m => m.classList.add('hidden'));`;

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
  // ⚠ 桩脚本必须先证明自己**跑起来了**。
  //
  // 它一旦有语法错误，页面就是一个**没有桩**的环境：后面几十条断言会以各种
  // 莫名其妙的形式失败（`window.__csp` 是 undefined、fetch 打到不存在的接口…），
  // 而根因只有一个，报错位置离原因极远。
  //
  // 真出过一次：`stubScript()` 返回的整段是**模板串**，正则里的 `\/` 会被模板串
  // 吃掉，`/^https?:\/\//` 于是变成 `//` 开头的注释 —— 语法错误，桩静默失效。
  // 所以这里显式验一次，不通过就直接退出，不去跑那些注定失真的断言。
  if (!(await evalIn(`return window.__INJECTED__ === 1;`))) {
    console.error("\n❌ 桩脚本没跑起来（window.__INJECTED__ 不是 1）。\n"
      + "   先检查 stubScript() 返回的代码有没有语法错误 ——\n"
      + "   它整体是一个模板串，正则里的 \\/ 会被吃掉（写成 [/] 或 indexOf）。\n");
    cleanupAll();
    process.exit(1);
  }
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
  check("工具条渲染出参数胶囊", boot.quickPills >= 3, `pills=${boot.quickPills}`);
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
    hint: (() => { const h = document.getElementById('composer-gen-hint');
      return { text: h.textContent, display: getComputedStyle(h).display,
               h: Math.round(h.getBoundingClientRect().height) }; })(),
    btn: (() => { const b = document.getElementById('btn-generate');
      const bs = getComputedStyle(b);
      return { stopping: b.classList.contains('stopping'),
               img: bs.backgroundImage, bg: bs.backgroundColor }; })(),
  };`);
  check("发送后进入生成态（用户气泡 + 助手气泡）",
    running.busy && running.jobId === "job1" && running.userMsg, JSON.stringify(running));
  check("步骤时间线渲染出已完成步骤", running.steps >= 2, `steps=${running.steps}`);
  check("生成中展示流式思考过程", running.thinkShown, "");
  check("生成中发送键变为「停止」", /停止/.test(running.btnTitle), running.btnTitle);
  // 停止键必须是**实心按钮**，不能是淡底。判据看 backgroundImage 而不是
  // backgroundColor：--grad-btn 是 linear-gradient，它落在 background-image 上，
  // 而 backgroundColor 恒为透明 —— 只查底色的话，实心键会被判成「没有背景」。
  // 这条能抓住的回归是：把它改回 background: var(--fill)（浅底，卡面变浅灰后即隐形）。
  check("生成中「停止」键仍是实心按钮（没被淡化成看不见的灰底）",
    running.btn.stopping && running.btn.img !== "none",
    JSON.stringify(running.btn));
  // 生成中的输入区留一张特写：这一态改过配色（停止键从 --fill 淡底改回实心），
  // 只断言不够 —— 断言守的是「有没有渐变」，看不出「跟卡面拉不拉得开」。
  // 必须在这里拍、不能挪到末尾的截图区：那边生成早结束了，这一态就没了。
  // 也不能用 shot()：它定义在截图区（本行之后几百行）。
  const stopBox = await evalIn(`const r = document.getElementById('composer').getBoundingClientRect();
    return { x: Math.max(0, r.left - 8), y: Math.max(0, r.top - 12),
             width: r.width + 16, height: r.height + 24, scale: 2 };`);
  const stopShot = await cdp.send("Page.captureScreenshot", { format: "png", clip: stopBox });
  fs.writeFileSync(path.join(SHOT_DIR, "composer-stopping.png"),
    Buffer.from(stopShot.data, "base64"));
  check("发送后清空输入框", running.topicCleared, "");
  // 生成中参数胶囊会被 lockParams 锁住（变灰、点不动）。「为什么点不动」全靠
  // 这一句解释 —— 它和 lockParams 是一对：锁了却不说原因，用户只会看到参数
  // 莫名其妙失效。所以断言要同时覆盖「信号写进去了」和「它真的到得了眼睛」
  // （display + 实际高度），只查 textContent 的话，被 CSS 藏起来也算通过。
  check("生成中给出「参数已锁定」的解释，且真的可见",
    /参数已锁定/.test(running.hint.text) && running.hint.display !== "none"
    && running.hint.h > 0, JSON.stringify(running.hint));

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
  // 非生成态：这一行必须留空**且不占高度**。它空着却占一条缝的话，
  // 卡片下方会凭空多出一块、看着像没对齐 —— 键盘提示并进 placeholder 之后，
  // 「空着不占位」就是这条规则的唯一可见后果，得有人守着。
  const idleHint = await evalIn(`const h = document.getElementById('composer-gen-hint');
    return { text: h.textContent, display: getComputedStyle(h).display,
             footH: Math.round(h.parentNode.getBoundingClientRect().height) };`);
  check("非生成态：状态行留空且不占高度",
    idleHint.text === "" && idleHint.display === "none" && idleHint.footH === 0,
    JSON.stringify(idleHint));
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
    // 改用规则断言而不是硬数字 6：panes 仍是 6（packgen 面板 DOM 在），
    // 但 navs 从 6 改 5 ——「新建行业包」下沉为「行业包」面板 headbar 的动作。
    // 改硬数字会让「导航项数」被无意冻结：以后再加个 nav 就是 6 → 7，
    // 这条断言会挂而报一个跟"实际坏了"无关的错。
    // 现在断言：①panes=6 ②navs=5 ③数据一致（panes=navs+1）
    return { open: !s.classList.contains('hidden'),
      covers: Math.round(r.width) === innerWidth && Math.round(r.height) === innerHeight,
      panes: document.querySelectorAll('#settings-screen .stg-pane').length,
      navs: document.querySelectorAll('.stg-nav-item').length,
      packgenNav: !!document.querySelector('.stg-nav-item[data-pane="packgen"]'),
      packgenPane: !!document.getElementById('pane-packgen'),
      on: document.querySelectorAll('.stg-nav-item.on').length,
      genVisible: !document.getElementById('pane-gen').classList.contains('hidden') };`);
  check("设置页铺满窗口，panes=6 / navs=5（packgen 不占导航位）",
    stg.open && stg.covers && stg.panes === 6 && stg.navs === 5
      && !stg.packgenNav && stg.packgenPane,
    JSON.stringify(stg));
  check("默认分区为生成偏好且高亮唯一",
    stg.genVisible && stg.on === 1, JSON.stringify(stg));

  // 层级断言：packgen 是「行业包」面板 headbar 的 [新建] 入口。
  // 进 packgen 后，导航仍高亮「行业包」（NAV_OF_PANE 映射），
  // 不出现两个高亮、也不出现没有高亮。
  await evalIn(`window.__ts.setPane('packinfo'); return true;`);
  await sleep(300);
  await evalIn(`document.getElementById('pi-newpack').click(); return true;`);
  await sleep(200);
  const fromPackinfo = await evalIn(`return {
    pane: [...document.getElementById('settings-screen').querySelectorAll('.stg-pane')]
            .find(p => !p.classList.contains('hidden'))?.id,
    onPane: document.querySelector('.stg-nav-item.on')?.dataset.pane };`);
  check("从行业包 [新建] 进入 packgen，导航高亮仍停在「行业包」（面包屑感）",
    fromPackinfo.pane === 'pane-packgen' && fromPackinfo.onPane === 'packinfo',
    JSON.stringify(fromPackinfo));
  // 「取消」回行业包（不是回生成偏好）—— packgenFrom 来源记录生效
  await evalIn(`document.getElementById('pg-close').click(); return true;`);
  await sleep(200);
  const afterCancel = await evalIn(`return [...document.getElementById('settings-screen').querySelectorAll('.stg-pane')]
    .find(p => !p.classList.contains('hidden'))?.id;`);
  check("packgen 从行业包进入时，「取消」回到行业包（不绕回生成偏好）",
    afterCancel === 'pane-packinfo', afterCancel);

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
    rows: document.querySelectorAll('#st-model-rows tr').length,
    label: document.querySelector('#st-model-rows .mdl-label')?.textContent,
    sub: document.querySelector('#st-model-rows .mdl-sub')?.textContent,
    prov: document.querySelector('#st-model-rows .col-prov')?.textContent,
    activeRows: document.querySelectorAll('#st-model-rows tr.on').length,
    switchOn: document.querySelectorAll('#st-model-rows .mdl-switch.on').length,
    ops: document.querySelectorAll('#st-model-rows .mdl-op').length,
    status: document.getElementById('st-status').textContent,
    cfgErrHidden: document.getElementById('st-cfg-err')?.classList.contains('hidden'),
    cfgErrText: document.getElementById('st-cfg-err')?.textContent || '' };`);
  // 模型从「三个平铺输入框」变成了**一份列表**：一行一条，带服务商与操作。
  // 平铺那版加第二个模型没有位置可填，只能把第一个覆盖掉。
  check("模型接口列出模型（一行一条，含服务商与操作）",
    llm.rows === 1 && llm.ops === 3 && !!llm.prov,
    JSON.stringify(llm));
  check("模型行显示模型名与「模型 ID · 主机名」小字",
    llm.label === "glm-4.7" && /glm-4\.7/.test(llm.sub || ""),
    JSON.stringify({ label: llm.label, sub: llm.sub }));
  // 当前启用的那一行要有落点：否则三行长得一样，只能靠开关的明暗去猜
  check("当前启用的模型行有唯一落点（行高亮 + 开关亮着）",
    llm.activeRows === 1 && llm.switchOn === 1,
    JSON.stringify({ activeRows: llm.activeRows, switchOn: llm.switchOn }));
  // 表头与「当前启用」那一行**不能同色相邻**。
  // 修复前两者铺的都是 --fill，上下紧贴、颜色一模一样 —— 在界面上读成
  // **一整块灰**：表头「模型/服务商/启用/操作」和下面那条数据行连成一片，
  // 看不出哪一行才是数据；行自带 --r-sm 圆角，还在表头下沿留了两个白缺口。
  // 表头现在只用一条发丝线（与全站其他表格一致），灰底归那一行独占。
  //
  // 判据取「有效底色」的**亮度差**，不取颜色字符串：
  //   - 表头背景是透明的，往上找到的底是白页 —— 字符串比会把「透明 vs 灰」判成不同，
  //     而真正要守的是「两者读起来不是一块」；
  //   - 逐层把 rgba 叠到白底上再比亮度，改回 --fill 时差值会归 0，报红。
  const tblHead = await evalIn(`return (function(){
    function bgOf(n){
      var stack = [];
      for (var e = n; e; e = e.parentElement) {
        var m = /rgba?\\(([^)]+)\\)/.exec(getComputedStyle(e).backgroundColor);
        if (!m) continue;
        var p = m[1].split(',').map(function(x){ return parseFloat(x); });
        var a = p.length > 3 ? p[3] : 1;
        if (a > 0) stack.push({ r:p[0], g:p[1], b:p[2], a:a });
      }
      var out = { r:255, g:255, b:255 };
      for (var i = stack.length - 1; i >= 0; i--) {
        var s = stack[i];
        out = { r: s.r*s.a + out.r*(1-s.a), g: s.g*s.a + out.g*(1-s.a),
                b: s.b*s.a + out.b*(1-s.a) };
      }
      return (out.r + out.g + out.b) / 3;
    }
    var th = document.querySelector('.mdl-table thead th');
    var tr = document.querySelector('.mdl-table tbody tr.on');
    var cs = getComputedStyle(th);
    return { headLum: bgOf(th), rowLum: bgOf(tr),
             headBg: getComputedStyle(th).backgroundColor,
             headBottom: cs.borderBottomWidth };
  })()`);
  check("表头与「当前启用」行不是同一种底色（不再连成一整块灰）",
    Math.abs(tblHead.headLum - tblHead.rowLum) >= 6
      && parseFloat(tblHead.headBottom) > 0,
    JSON.stringify(tblHead));
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
  check("前端接受 1.8 采样温度（与后端区间一致，不再比后端更严）",
    !!t18 && t18.temperature === 1.8, JSON.stringify(t18));
  // 这一页的保存只管高级配置。连接信息（base_url / model）住在模型条目里，
  // 由弹窗保存 —— 这里再带一遍就等于同一件事有两处写入口。
  check("高级配置的保存不再连带写连接信息（那归模型条目管）",
    !!t18 && !('base_url' in t18) && !('model' in t18), JSON.stringify(t18));
  // 还原成默认值，免得影响后面的保存相关用例
  await evalIn(`var n = document.getElementById('st-temperature');
    n.value = '0.7'; n.dispatchEvent(new Event('input', {bubbles:true})); return true;`);
  // 配置正常时不能误报 —— 误报会让这条警示彻底失去可信度
  check("config.yaml 正常时「配置读坏」警示隐藏且无文案",
    llm.cfgErrHidden === true && llm.cfgErrText === '', JSON.stringify(llm));

  // 未保存的输入不被覆盖（修复点：每次打开设置都 preloadSettings 会冲掉编辑）
  await evalIn(`window.__ts.setPane('llm');
    const b = document.getElementById('st-timeout');
    b.value = '60';
    b.dispatchEvent(new Event('input', {bubbles:true})); return true;`);
  await sleep(200);
  await evalIn(`document.getElementById('btn-close-settings').click();
    document.getElementById('btn-open-settings').click(); return true;`);
  await sleep(600);
  const keep = await evalIn("return document.getElementById('st-timeout').value;");
  check("重开设置不覆盖未保存的输入", keep === "60", keep);
  // 收尾：把刚才改脏的输入还原回服务端值，否则后面「回填值」那条断言会读到 60
  await evalIn(`window.__ts.setPane('llm');
    document.getElementById('st-reset-adv').click(); return true;`);
  await sleep(500);

  // 连接信息（请求地址 / 模型 ID）现在住在**模型弹窗**里，所以「恢复默认」
  // 也跟着搬过去。它的做法是把内置默认值**显式填进框里**，由用户点保存落盘 ——
  // 直接改服务端会绕过「这条模型到底存了什么」，而且对非当前模型根本没法用。
  await evalIn(`window.__ts.setPane('llm');
    document.getElementById('st-add-model').click(); return true;`);
  await sleep(400);
  const dlg = await evalIn(`return {
    open: !document.getElementById('model-dialog').classList.contains('hidden'),
    title: document.getElementById('md-title').textContent,
    id: document.getElementById('md-id').value,
    url: document.getElementById('md-baseurl').value,
    model: document.getElementById('md-model').value };`);
  check("「添加模型」打开的是表单弹窗（不再是跳回设置页填一个名字）",
    dlg.open && dlg.title === "添加模型" && dlg.id === ""
    && dlg.url === "" && dlg.model === "", JSON.stringify(dlg));
  await evalIn(`document.getElementById('md-reset-baseurl').click(); return true;`);
  await sleep(200);
  const dreset = await evalIn(`return {
    url: document.getElementById('md-baseurl').value,
    tag: document.querySelector('#model-dialog .tag-default')?.dataset.field || '' };`);
  check("弹窗「恢复默认」把内置默认地址填进框里",
    dreset.url === "https://x/v4", JSON.stringify(dreset));
  await evalIn(`document.getElementById('md-cancel').click(); return true;`);
  await sleep(200);
  const dclosed = await evalIn(
    `return document.getElementById('model-dialog').classList.contains('hidden');`);
  check("弹窗可以取消，不留残余浮层", dclosed === true, String(dclosed));

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
  // 桩里 api.pack('elevator') 返回 3 个文件：pack.yaml / skill.yaml / knowledge/topics.md。
  // 改造后 kb 与 skills **按命名分组互不重叠**：
  //   kb     = pack.yaml + knowledge/   （2 个）
  //   skills = skill.yaml                  （1 个）
  // 修复前 kb: () => true 把 skill.yaml 也列入知识库，命名错位 —— 两个面板
  // 显示同一组文件，知识库的「资料」语义被偷换成了「全部」。
  // 规则断言：①互不重叠 ②各面板筛后文件数 ≥ 1（有内容） ③按角色分组。
  //
  // 「内容漏出卡片」探针（卡片高度必须容得下内容）。
  // 修复前 `.kb-item` 是 `<button>`，全局 `button { height: var(--h-btn) }`（30px）
  // 把它钉死，而卡里是「36px 图标 + 一行文件名」，需要 56px —— 内容从卡片底部
  // 漏出 18px。更坏的是**下一张卡的白底正好把漏出的那半行盖住**，所以界面上
  // 看到的是「文件名下面漂着一行小字、还压在下一张卡上」，只有每组的最后一张
  // 整行露在组外。用户 2026-09-16 贴的截图就是这个。
  //
  // 判据是**内容真的漏出盒子**：拿每个非绝对定位子元素的底边与卡的内容盒底边
  // 比。不用 `scrollHeight > clientHeight` 是因为那个口径有假阳性 ——
  // `.segmented .radio` 里视觉隐藏的 `<input>` 是绝对定位的，照样会把
  // scrollHeight 撑大 4px（实测），那是探针的问题、不是界面的问题。
  // ⚠ 量到 `HIDDEN` 要当**失败**：面板此刻是 display:none 时高度全是 0，
  // 差值算出来是无意义的数 —— 不显式报出来的话，这条断言就变成空转（假绿）。
  const overflowProbe = (sel) => `
    var bad = [];
    document.querySelectorAll(${JSON.stringify(sel)}).forEach(function (n) {
      var box = n.getBoundingClientRect();
      var name = n.querySelector('.kb-name') ? n.querySelector('.kb-name').textContent : '?';
      if (!box.height) { bad.push({ name: name, hidden: true }); return; }
      var cs = getComputedStyle(n);
      var bottom = box.bottom - parseFloat(cs.paddingBottom) - parseFloat(cs.borderBottomWidth);
      var over = 0;
      Array.prototype.forEach.call(n.children, function (c) {
        if (getComputedStyle(c).position === 'absolute') return;
        over = Math.max(over, c.getBoundingClientRect().bottom - bottom);
      });
      if (over >= 2) bad.push({ name: name, h: box.height, over: Math.round(over * 10) / 10 });
    });
    return bad;`;

  await evalIn(`window.__ts.setPane('kb'); return true;`);
  await sleep(400);
  const kb = await evalIn(`return {
    rows: document.querySelectorAll('#kb-list .kb-item').length,
    names: [...document.querySelectorAll('#kb-list .kb-item .kb-name')].map(n => n.textContent),
    groups: document.querySelectorAll('#kb-list .kb-group').length,
    hasPlaceholder: !!document.querySelector('#pane-kb .placeholder') };`);
  check("知识库面板列出资料类文件（kb + skills 互不重叠），且按角色分组",
    kb.rows >= 1 && !kb.hasPlaceholder && kb.groups >= 1
      && !kb.names.some(n => n === "skill.yaml"),
    JSON.stringify(kb));
  const kbOver = await evalIn(overflowProbe("#kb-list .kb-item"));

  await evalIn(`[...document.querySelectorAll('#kb-list .kb-item')]
    .find(n => n.querySelector('.kb-name').textContent.includes('knowledge')).click(); return true;`);
  await sleep(300);
  const kbBody = await evalIn(`return {
    title: document.getElementById('kb-title').textContent,
    body: document.getElementById('kb-body').textContent };`);
  check("点击知识库文件显示内容",
    /选题库/.test(kbBody.body) && kbBody.title === "knowledge/topics.md",
    JSON.stringify(kbBody));

  await evalIn(`window.__ts.setPane('skills'); return true;`);
  await sleep(400);
  const sk = await evalIn(`return {
    rows: [...document.querySelectorAll('#skills-list .kb-item')]
            .map(n => n.querySelector('.kb-name').textContent) };`);
  check("技能面板只列技能类文件（skill.yaml / rules|patterns|compliance/）",
    sk.rows.length === 1 && sk.rows[0] === "skill.yaml", JSON.stringify(sk));
  const skOver = await evalIn(overflowProbe("#skills-list .kb-item"));
  check("知识库/技能卡片容得下内容（不再被按钮的固定高度压扁）",
    kb.rows >= 1 && sk.rows.length >= 1
      && kbOver.length === 0 && skOver.length === 0,
    JSON.stringify({ kb: kbOver, skills: skOver }));

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
  check("设置导航：返回项 → 列表恒定 16px，五项全在且无隐藏项（搜索框已删；packgen 不占导航）",
    Math.abs(stgNavGeo.返回项到列表 - 16) <= 0.6
    && stgNavGeo.列表内上边距 === "0px"
    && stgNavGeo.导航项数 === 5 && stgNavGeo.被隐藏的项 === 0,
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
  // 工具条参数清单的**规格副本**：这里有意不复用 ui.js 的常量 ——
  // 复用就成了同义反复（实现改错、断言跟着一起错）。代价是调整分层时
  // 要同步改这一行，所以它必须显式写着「这是规格」。
  const TOOLBAR_KEYS = ["segment", "audience", "duration", "platform"];
  const expectFor = name => {
    const p = (META.packs.find(x => x.name === name) || {}).params || {};
    const keys = Object.keys(p).filter(k => p[k] && p[k].options && p[k].options.length);
    return { 胶囊: keys.filter(k => TOOLBAR_KEYS.includes(k)).length,
             参数组: keys.filter(k => !TOOLBAR_KEYS.includes(k)).length };
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

  // 模型选择器：此前只有进设置页才知道在用哪个模型，且要改模型必须跳页。
  // 现在它就在输入区工具条上、发送键左侧，可直接切换。
  const picker = await evalIn(`const s = document.getElementById('p-model');
    const btn = s?.parentNode?.querySelector('.select-btn');
    return s ? { value: s.value, text: btn.querySelector('.sel-text').textContent,
                 title: btn.title,
                 texts: Array.from(s.options).map(o => o.textContent) } : null;`);
  check("输入区显示当前模型（不再只有设置页能看到）",
    picker && picker.value === "m-default" && picker.text === "glm-4.7"
    && /模型/.test(picker.title), JSON.stringify(picker));
  // 候选来自**设置里那份模型列表**，不再是 localStorage 里攒的历史：
  // 历史与配置是同一件事的两份表示，而历史那份还是隐形的 —— 在设置里删掉
  // 一个模型，输入区的下拉里它还在；换台机器打开，历史全没了。
  check("模型下拉来自配置里的模型列表（不是本地攒的历史）",
    picker && picker.texts.length === 2 && picker.texts.some(t => /添加模型/.test(t)),
    JSON.stringify(picker && picker.texts));

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
  // 「动作区（新建对话）↔ 内容区（搜索+列表）」原本靠一条 0.5px 分隔线区分，
  // 线两侧各留 16 —— 那时搜索框距按钮 32、距列表 16，「更近列表」是有层次的。
  // 2026-09-16 删掉 .left-sep 后只靠一个 16px gap 区分，两段变成**等距**（都 16）。
  // 断言随之从「更近/更远」改为「等距且都是 16」：守住的是统一节奏，不是层次。
  check("按钮→搜索→列表 等距 16（靠距离区分，不再靠分隔线）",
    headOrder.gapToLbl >= 0 && headOrder.gapUp === headOrder.gapToLbl
    && headOrder.gapUp === 16,
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
    // ⚠ .left-sep 已于 2026-09-16 移除（动作区与内容区改为只用 16px 等距区分）。
    // 这里**不能**再 querySelector('.left-sep') —— 元素不存在会拿到 null，
    // 下面的 .getBoundingClientRect() 抛 TypeError，会把后续所有断言一起带走
    // （判据：**耗时明显短于正常**就说明中途崩了，后面的断言根本没跑 ——
    //   具体耗时每次不同，别抄数字，跑一遍自己对；见 LESSONS「断言空转」）。
    // 改为直接量「按钮 → 搜索」。
    const head = r('.left-head'), btn = r('#btn-new-chat'),
          sea = r('.sess-search'), sc = r('.left-scroll');
    const footEl = document.querySelector('.left-foot');
    const foot = footEl.getBoundingClientRect();
    const footBorder = parseFloat(getComputedStyle(footEl).borderTopWidth) || 0;
    return { 线到按钮: +(btn.t - head.b).toFixed(2),
             按钮到搜索: +(sea.t - btn.b).toFixed(2),
             搜索到列表: +(sc.t - sea.b).toFixed(2),
             底线到设置: +(r('#btn-open-settings').t - (foot.top + footBorder)).toFixed(2),
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

  // 「动作区与内容区之间有分隔线，且横贯侧栏全宽」这条断言**已随 .left-sep 一起移除**
  // （2026-09-16）：那条 0.5px 发丝线在 dpr=1 下会被 Chrome 向上取整成 1px 渲染，
  // 且要靠负 margin 才能横贯全宽，做法脆弱；改为只用一个 16px 的 gap 区分两个区域。
  //
  // ⚠ 别把这条断言加回来 —— 它守的元素已不存在，`querySelector` 返回 null 后
  // `.getBoundingClientRect()` 会抛 TypeError，把后续所有断言一起带走
  // （崩溃信号：**总耗时明显短于正常**，因为后面的断言压根没执行）。
  // 守卫职责已转交给上面两条：「左栏竖向节奏统一 16px（含底部设置区）」
  // 与「按钮→搜索→列表 等距 16」—— 它们守的是同一个「统一节奏」的意图。

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
  // 引导**长在空态主区上**（hero 整块换文案），不再另起一张卡片。
  // 曾经是「引导卡 + hero」两个居中块叠着、各带一个大图标，没有主次。
  await cdp.send("Page.navigate",
    { url: `http://127.0.0.1:${PORT}/?token=stubtoken&nokey=1` });
  await sleep(1800);
  const firstRun = await evalIn(`return (function(){
    var h3 = document.querySelector('#empty h3');
    var sub = document.querySelector('#empty .empty-sub');
    // ⚠ 状态挂在 .empty-cta 这个**外层**上（[data-when] 是它的属性），
    // 按钮自己永远不会带 .hidden —— 查按钮本身的话这条断言恒为真。
    var cta = document.querySelector('#empty .empty-cta');
    return {
      setupTitle: window.__shown(h3),
      setupSub: window.__shown(sub),
      setupBtn: !!cta && !cta.classList.contains('hidden')
        && !!document.getElementById('btn-empty-setup'),
      cards: document.querySelectorAll('#empty .setup-card').length,
      settingsOpen: !document.getElementById('settings-screen').classList.contains('hidden'),
      paneLlm: !document.getElementById('pane-llm').classList.contains('hidden'),
      samples: document.querySelectorAll('#empty-samples .sample-card').length,
    }; })()`);
  check("未配置 Key 时空态主区换成配置引导（含「去配置」）",
    /先配置模型接口/.test(firstRun.setupTitle) && firstRun.setupBtn
    && /OpenAI 兼容/.test(firstRun.setupSub), JSON.stringify(firstRun));
  check("引导与「想聊点什么？」是同一块 hero 的两态（不是两块叠着）",
    firstRun.cards === 0, JSON.stringify(firstRun));
  check("首启自动打开设置并落在「模型接口」分区",
    firstRun.settingsOpen && firstRun.paneLlm, JSON.stringify(firstRun));
  check("引导态仍保留示例卡（先挑主题再配 Key 这条路不能被挡掉）",
    firstRun.samples === 4, JSON.stringify(firstRun));
  // 「没配过」必须看得出来。后端会把没写的字段静默兜底成内置默认值
  // （config.py 的 _effective：空 base_url → DEFAULT_MODEL），界面若不标，
  // 未配置状态和已配置状态长得一模一样：列表里那行写着 glm-4.7、
  // 输入区右侧也写着 glm-4.7，用户第一眼就以为配好了。
  //
  // ⚠ 断言按 **data-field 的集合**比，不按标签个数比：个数对不上可能只是布局
  // 变了，而集合对不上才是「该标的没标 / 不该标的标了」。行内那个标把两个字段
  // 合成一个（一行里挂两个一模一样的「内置默认」是噪音），所以按逗号拆开。
  const notCfg = await evalIn(`return (function(){
    function fields(root){
      return [].concat.apply([], Array.from(root.querySelectorAll('.tag-default'))
        .map(function(t){ return (t.dataset.field || '').split(',').filter(Boolean); }));
    }
    var row = document.querySelector('#st-model-rows tr.on');
    var tags = Array.from(row.querySelectorAll('.tag-default'));
    var sub = row.querySelector('.mdl-sub');
    var s = document.getElementById('p-model');
    var b = s && s.parentNode.querySelector('.select-btn');
    return { rowFields: fields(row).sort(),
             advFields: fields(document.getElementById('st-adv')).sort(),
             modelTag: tags.length ? tags[0].textContent : '',
             modelTagColor: tags.length ? getComputedStyle(tags[0]).color : '',
             // 小标与上一行之间的**真实像素间距**：CSS 里那句 margin-top: 4px
             // 在 inline 元素上是**被忽略**的，只看 CSS 文本会以为它生效了。
             tagGap: (tags.length && sub)
               ? Math.round((tags[0].getBoundingClientRect().top
                   - sub.getBoundingClientRect().bottom) * 10) / 10
               : null,
             tagDisplay: tags.length ? getComputedStyle(tags[0]).display : '',
             pickerText: b ? b.querySelector('.sel-text').textContent : '',
             pickerValue: s ? s.value : '' };
  })()`);
  check("未配置时逐项标出「内置默认」（六项一个不漏，按字段集合核对）",
    notCfg.rowFields.concat(notCfg.advFields).sort().join()
      === "base_url,max_tokens,model,retries,temperature,timeout"
    && notCfg.modelTag === "内置默认", JSON.stringify(notCfg));
  // 中性灰而不是 warn 橙：没配过不是故障，染橙会让橙色贬值
  // （真正会失败的「未配置 API Key」那条就没人看了）。
  check("「内置默认」用中性灰而不是警示色",
    notCfg.modelTagColor !== 'rgb(178, 94, 0)' && /^rgb/.test(notCfg.modelTagColor),
    notCfg.modelTagColor);
  // 小标要跟上一行**分开**，不能贴在「glm-4.7 · 智谱」下面。
  // 修复前 `.tag-default` 是 inline：`.mdl-sub` 是 block，后面的 inline 元素
  // 虽然会自动换行，但 **inline 的 margin-top 不生效** —— 样式表里写着的 4px
  // 从来没算进布局，小标就紧贴着上一行（截图里它看起来像被行底边裁掉一截）。
  // 判据取**真实像素间距**，不查 CSS 文本：查文本的话，写着一句不生效的
  // margin-top 也会判绿，正是这条断言要防的事。
  check("「内置默认」小标与上一行之间留出间距（margin-top 真的生效）",
    notCfg.tagDisplay === 'inline-block' && notCfg.tagGap >= 3,
    JSON.stringify({ display: notCfg.tagDisplay, gap: notCfg.tagGap }));
  // 显示名带「（默认）」，但 option.value 必须还是模型 **id** ——
  // 混在一起的话激活时会把「glm-4.7（默认）」这个假 id 发出去。
  check("未配置时模型选择器标明「（默认）」且 value 仍是模型 id",
    notCfg.pickerText === "glm-4.7（默认）" && notCfg.pickerValue === "m-default",
    JSON.stringify(notCfg));

  // 编辑弹窗里，**没配过的字段必须留空**，不能把生效值（兜出来的默认地址）
  // 填进框里 —— 那等于程序写的值冒充用户输入，用户没动过手却看到一串地址，
  // 而且保存一次它就真的成了他的配置。
  await evalIn(`document.querySelectorAll('#st-model-rows tr.on .mdl-op')[1].click();
    return true;`);
  await sleep(300);
  const editDlg = await evalIn(`return {
    open: !document.getElementById('model-dialog').classList.contains('hidden'),
    title: document.getElementById('md-title').textContent,
    url: document.getElementById('md-baseurl').value,
    model: document.getElementById('md-model').value,
    ph: document.getElementById('md-baseurl').placeholder,
    tags: Array.from(document.querySelectorAll('#model-dialog .tag-default'))
            .map(t => t.dataset.field).sort().join(),
    akPh: document.getElementById('md-apikey').placeholder };`);
  check("编辑一条没配过的模型：弹窗标题是「编辑」，字段留空并逐项标出内置默认",
    editDlg.open && editDlg.title === "编辑模型"
    && editDlg.url === "" && editDlg.model === ""
    && editDlg.tags === "base_url,model", JSON.stringify(editDlg));
  // 留空但不能让人不知道填什么：placeholder 里给出默认地址
  check("留空的字段用 placeholder 说明默认值（不是一片空白）",
    /^https:\/\//.test(editDlg.ph || ""), editDlg.ph);
  check("未配置时 Key 输入框不再暗示「已经配过了」",
    /粘贴/.test(editDlg.akPh), editDlg.akPh);
  await evalIn(`document.getElementById('md-cancel').click(); return true;`);
  await sleep(200);
  // 「没有配置」这个状态留两张图：用户开机第一眼看到的是**自动弹开的设置页**，
  // 关掉之后才看到主界面上那块配置引导 —— 两张都要有人看过。
  await shot("setup-settings.png", `${closeMenus} return true;`);
  await shot("setup-main.png",
    `document.getElementById('btn-close-settings').click(); ${closeMenus} return true;`);

  // 从「没配过」走到「配好了」——**同一个页面、不刷新**。
  // 这一条专门防「只增不减 / 只切一次」：只在新页面里比对是测不出来的，
  // 一个「挂上就不摘」「只在 noKey 时改一次」的实现在新页面里
  // 与正确实现长得一模一样。
  await evalIn(`document.getElementById('btn-open-settings').click();
    window.__ts.setPane('llm'); return true;`);
  await sleep(600);
  const beforeSave = await evalIn(`return (function(){
    function fields(root){
      return [].concat.apply([], Array.from(root.querySelectorAll('.tag-default'))
        .map(function(t){ return (t.dataset.field || '').split(',').filter(Boolean); }));
    }
    var row = document.querySelector('#st-model-rows tr.on');
    var h3 = document.querySelector('#empty h3');
    return { fields: fields(row).concat(fields(document.getElementById('st-adv'))).sort().join(),
             heroTitle: window.__shown(h3),
             heroBtn: !document.querySelector('#empty .empty-cta').classList.contains('hidden') };
  })()`);
  // 走**用户真实的路径**：打开这条模型的编辑弹窗 → 填地址与模型名 → 保存。
  // 不能绕过界面直接调接口 —— 那样测的是后端，不是「界面会不会把标记摘掉」。
  // 填 Key 是必须的：不填的话「已配置」这个状态根本不会到来。
  await evalIn(`document.querySelectorAll('#st-model-rows tr.on .mdl-op')[1].click(); return true;`);
  await sleep(300);
  await evalIn(`document.getElementById('md-reset-baseurl').click();
    var m = document.getElementById('md-model'); m.value = 'glm-4.7';
    m.dispatchEvent(new Event('input', {bubbles:true}));
    var k = document.getElementById('md-apikey'); k.value = 'sk-test';
    k.dispatchEvent(new Event('input', {bubbles:true}));
    document.getElementById('md-save').click(); return true;`);
  await sleep(1000);
  const afterSave = await evalIn(`return (function(){
    function fields(root){
      return [].concat.apply([], Array.from(root.querySelectorAll('.tag-default'))
        .map(function(t){ return (t.dataset.field || '').split(',').filter(Boolean); }));
    }
    var row = document.querySelector('#st-model-rows tr.on');
    var s = document.getElementById('p-model');
    var b = s && s.parentNode.querySelector('.select-btn');
    var h3 = document.querySelector('#empty h3');
    return { fields: fields(row).concat(fields(document.getElementById('st-adv'))).sort().join(),
             rowTags: row.querySelectorAll('.tag-default').length,
             dlgHidden: document.getElementById('model-dialog').classList.contains('hidden'),
             pickerText: b ? b.querySelector('.sel-text').textContent : '',
             heroTitle: window.__shown(h3),
             heroBtn: !document.querySelector('#empty .empty-cta').classList.contains('hidden') };
  })()`);
  // 连接信息配好之后，那两项的小标必须立刻消失；高级配置那四项没动过，仍在。
  // 「只增不减」的实现在这里给的是 6 → 6，直接报红。
  check("配好模型之后同一页面里那两项「内置默认」小标立刻消失（不是只增不减）",
    beforeSave.fields === "base_url,max_tokens,model,retries,temperature,timeout"
    && afterSave.fields === "max_tokens,retries,temperature,timeout",
    `${beforeSave.fields} → ${afterSave.fields}`);
  check("保存后弹窗自动关闭", afterSave.dlgHidden === true, String(afterSave.dlgHidden));
  check("保存后模型选择器同步摘掉「（默认）」",
    afterSave.pickerText === "glm-4.7", afterSave.pickerText);

  // 再把「高级配置」也存一次。**这一步不能省**：数值项的小标走的是
  // markDefault（就地增删），与列表行（整行 innerHTML 重建）不是同一套机制，
  // 必须各自验一次「会消失」。
  //
  // 实测漏过一次：把 markDefault 改成「只加不摘」之后，整套断言**全绿** ——
  // 因为「已配置」的页面里它本来就没挂过标，看不出区别。只有在同一个页面里
  // 走一遍「没配 → 配好」，那个「摘」的动作才有东西可摘。
  await evalIn(`document.getElementById('st-save').click(); return true;`);
  await sleep(900);
  const afterAdv = await evalIn(`return (function(){
    function fields(root){
      return [].concat.apply([], Array.from(root.querySelectorAll('.tag-default'))
        .map(function(t){ return (t.dataset.field || '').split(',').filter(Boolean); }));
    }
    var row = document.querySelector('#st-model-rows tr.on');
    return { fields: fields(row).concat(fields(document.getElementById('st-adv'))).join(),
             advSub: document.getElementById('st-adv-sub').textContent };
  })()`);
  check("保存高级配置后那四项的小标也消失（markDefault 不是只增不减）",
    afterAdv.fields === "" && afterAdv.advSub === "",
    JSON.stringify(afterAdv));
  // hero 也必须跟着切回来。只在 noKey 时改一次的实现在这里会露馅：
  // 引导是「一次性的」，用户配好 Key 之后空态还写着「先配置模型接口」。
  check("保存后空态主区切回「想聊点什么？」（引导不是一次性的）",
    beforeSave.heroBtn === true && /先配置模型接口/.test(beforeSave.heroTitle)
    && afterSave.heroBtn === false && /想聊点什么/.test(afterSave.heroTitle),
    JSON.stringify({ before: beforeSave.heroTitle, after: afterSave.heroTitle }));
  await evalIn(`document.getElementById('btn-close-settings').click(); return true;`);
  await sleep(200);

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
    function fields(root){
      return [].concat.apply([], Array.from(root.querySelectorAll('.tag-default'))
        .map(function(t){ return (t.dataset.field || '').split(',').filter(Boolean); }));
    }
    var n = document.getElementById('st-cfg-err');
    if (!n) return { missing: true };
    var cs = getComputedStyle(n);
    var row = document.querySelector('#st-model-rows tr.on');
    return { hidden: n.classList.contains('hidden'), display: cs.display,
             text: n.textContent, color: cs.color,
             fields: fields(row).concat(fields(document.getElementById('st-adv')))
                       .sort().join(),
             status: document.getElementById('st-status').textContent };
  })()`);
  check("config.yaml 读坏时设置页给出警示（不是静默用默认值）",
    !cfgErr.missing && cfgErr.hidden !== true && cfgErr.display !== 'none'
      && /默认值/.test(cfgErr.text) && /保存/.test(cfgErr.text),
    JSON.stringify(cfgErr));
  // 两个条件各自吃劲：颜色证明用的是 --warn 而不是普通 hint 灰；
  // status 证明警示是**追加**的，没有把状态行整行换掉 ——
  // 换掉的话用户就看不到「当前用的是哪条模型」了。
  check("警示用 warn 色且不顶掉原状态行",
    cfgErr.color === 'rgb(178, 94, 0)' && /模型/.test(cfgErr.status),
    JSON.stringify(cfgErr));
  // 读坏与没配过是**同一件事的两个来源**（读坏 = 整个文件读不到 → 六项全取默认），
  // 所以两个信号必须同时出现：只有橙色警示、界面却不标默认，说明前端只消费了
  // 其中一个字段 —— 那正是「信号算对了但没人接」的老毛病。
  check("读坏时「内置默认」小标与橙色警示同时出现",
    cfgErr.fields === "base_url,max_tokens,model,retries,temperature,timeout",
    JSON.stringify(cfgErr));

  // 回到正常模式，继续后面的布局检查
  await cdp.send("Page.navigate", { url: `http://127.0.0.1:${PORT}/?token=stubtoken` });
  await sleep(1600);
  await evalIn(`document.getElementById('settings-screen').classList.add('hidden'); return true;`);

  // 反向对照：配好之后这些标记必须**全部消失**。
  // 没有这三条，一个「永远挂 6 个标 / 永远带（默认）后缀」的实现也能过上面那几条 ——
  // 断言只证明「未配置时看得见」，证明不了「已配置时看不见」。
  await evalIn(`document.getElementById('btn-open-settings').click();
    window.__ts.setPane('llm'); return true;`);
  await sleep(600);
  const cfgd = await evalIn(`return (function(){
    function fields(root){
      return [].concat.apply([], Array.from(root.querySelectorAll('.tag-default'))
        .map(function(t){ return (t.dataset.field || '').split(',').filter(Boolean); }));
    }
    var row = document.querySelector('#st-model-rows tr.on');
    var s = document.getElementById('p-model');
    var b = s && s.parentNode.querySelector('.select-btn');
    var h3 = document.querySelector('#empty h3');
    return { fields: fields(row).concat(fields(document.getElementById('st-adv'))).join(),
             rowTags: row.querySelectorAll('.tag-default').length,
             warnTags: row.querySelectorAll('.tag-warn').length,
             heroTitle: window.__shown(h3),
             heroBtn: !document.querySelector('#empty .empty-cta').classList.contains('hidden'),
             pickerText: b ? b.querySelector('.sel-text').textContent : '' };
  })()`);
  check("已配置时「内置默认」小标全部消失（不误报）",
    cfgd.fields === "" && cfgd.rowTags === 0, JSON.stringify(cfgd));
  // 「未配置 Key」同理不能误报：配好了还挂着它，会让这个警示彻底失去可信度。
  check("已配置时不再报「未配置 Key」（不误报）",
    cfgd.warnTags === 0, JSON.stringify(cfgd));
  check("已配置时模型名不带「（默认）」后缀（不误报）",
    cfgd.pickerText === "glm-4.7", cfgd.pickerText);
  // Key 输入框的 placeholder 也在弹窗里：已配置时回到「留空即保持不变」。
  // 从没配过时写这句等于在暗示「你已经配过了」—— 两个状态的文案各只有一份，
  // 已配置那句就是 HTML 里的 placeholder（首次打开时存进 data-ph-set）。
  await evalIn(`document.querySelectorAll('#st-model-rows tr.on .mdl-op')[1].click(); return true;`);
  await sleep(300);
  const cfgdAk = await evalIn(`return document.getElementById('md-apikey').placeholder;`);
  check("已配置时 Key 输入框回到「留空即保持不变」",
    /留空即保持不变/.test(cfgdAk), cfgdAk);
  await evalIn(`document.getElementById('md-cancel').click(); return true;`);
  await sleep(150);

  // ── 12f) 模型列表：添加 / 启用 / 编辑 / 删除 ──────────────
  // 全部走**界面路径**（点按钮 → 填弹窗 → 保存），不绕过界面直接调接口 ——
  // 直接调接口测的是后端，而这里要守的是「界面有没有把动作接上」。
  // 「信号算了但没人接」是本项目最容易犯的错（param_audit 那一类）。
  await evalIn(`document.getElementById('btn-open-settings').click();
    window.__ts.setPane('llm'); return true;`);
  await sleep(500);
  const mdlBefore = await evalIn(`return {
    rows: document.querySelectorAll('#st-model-rows tr').length,
    delDisabled: document.querySelectorAll('#st-model-rows .mdl-op')[2].disabled };`);
  check("只有一条模型时「删除」是禁用的（把「至少留一条」这条规则摆到界面上）",
    mdlBefore.rows === 1 && mdlBefore.delDisabled === true, JSON.stringify(mdlBefore));

  await evalIn(`document.getElementById('st-add-model').click(); return true;`);
  await sleep(250);
  await evalIn(`document.getElementById('md-model').value = 'deepseek-chat';
    document.getElementById('md-name').value = 'DeepSeek';
    document.getElementById('md-baseurl').value = 'https://api.deepseek.com/v1';
    document.getElementById('md-apikey').value = 'sk-dialog';
    document.getElementById('md-save').click(); return true;`);
  await sleep(800);
  const added = await evalIn(`return {
    hidden: document.getElementById('model-dialog').classList.contains('hidden'),
    rows: document.querySelectorAll('#st-model-rows tr').length,
    ids: Array.from(document.querySelectorAll('#st-model-rows tr')).map(t => t.dataset.id),
    label2: document.querySelectorAll('#st-model-rows .mdl-label')[1]?.textContent,
    prov2: document.querySelectorAll('#st-model-rows .col-prov')[1]?.textContent,
    body: window.__lastModelBody || null };`);
  check("弹窗保存后列表多出一条（不再是「填一个名字就跳回设置页」）",
    added.hidden && added.rows === 2 && added.ids[1] === "m1"
    && added.label2 === "DeepSeek", JSON.stringify(added));
  // 请求体必须只带这一条模型的信息，不能顺手把当前模型的地址也写进去 ——
  // 「同一件事两份表示」正是要避免的。
  check("保存的是**这条**模型（请求体里是它的地址与 Key）",
    !!added.body && added.body.base_url === "https://api.deepseek.com/v1"
    && added.body.model === "deepseek-chat" && added.body.api_key === "sk-dialog",
    JSON.stringify(added.body));
  // 服务商名是从请求地址**推**出来的（参考图里那一列），推不出来就原样显示主机名 ——
  // 硬套一个名字会把「这家」说成「那家」。
  check("服务商列按请求地址推断（未命中已知表就显示主机名）",
    added.prov2 === "DeepSeek", added.prov2);

  // 启用：必须真的调切换接口，而不是只把开关点亮
  await evalIn(`document.querySelectorAll('#st-model-rows tr')[1]
    .querySelector('.mdl-switch').click(); return true;`);
  await sleep(800);
  const act = await evalIn(`return {
    called: window.__lastActivate || '',
    onRows: document.querySelectorAll('#st-model-rows tr.on').length,
    onId: document.querySelector('#st-model-rows tr.on')?.dataset.id,
    onSwitches: document.querySelectorAll('#st-model-rows .mdl-switch.on').length,
    checked: Array.from(document.querySelectorAll('#st-model-rows .mdl-switch'))
               .map(b => b.getAttribute('aria-checked')).join() };`);
  // 开关是**单选**语义（同时只有一个生效），所以点一个必须关掉另一个 ——
  // 两个都亮着就是在骗人：用户以为能同时启用两个模型。
  check("点「启用」真的调了切换接口，且同时只有一个亮着",
    act.called === "m1" && act.onRows === 1 && act.onId === "m1"
    && act.onSwitches === 1 && act.checked === "false,true", JSON.stringify(act));

  // 输入区那个选择器要跟着变 —— 它读的是 /api/meta，不是设置页的局部状态
  await evalIn(`document.getElementById('btn-close-settings').click(); return true;`);
  await sleep(300);
  const pick2 = await evalIn(`const s = document.getElementById('p-model');
    const b = s && s.parentNode.querySelector('.select-btn');
    return { value: s.value, text: b.querySelector('.sel-text').textContent,
             n: s.options.length };`);
  check("设置里启用另一个模型后，输入区的选择器同步跟上",
    pick2.value === "m1" && pick2.text === "DeepSeek" && pick2.n === 3,
    JSON.stringify(pick2));
  // 在下拉里切换必须真的写出去（不只是改了显示）
  await evalIn(`const s = document.getElementById('p-model');
    s.value = 'm-default'; s.dispatchEvent(new Event('change', { bubbles: true }));
    return true;`);
  await sleep(600);
  const pickAct = await evalIn(`return window.__lastActivate || '';`);
  check("在输入区切换模型会调切换接口（不只是改了显示）",
    pickAct === "m-default", pickAct);

  // 删除：确认后真的从列表里消失
  await evalIn(`document.getElementById('btn-open-settings').click();
    window.__ts.setPane('llm'); return true;`);
  await sleep(500);
  await evalIn(`document.querySelectorAll('#st-model-rows .mdl-op')[2].click(); return true;`);
  await sleep(300);
  await evalIn(`document.getElementById('cd-yes').click(); return true;`);
  await sleep(800);
  const mdlAfter = await evalIn(`return {
    rows: document.querySelectorAll('#st-model-rows tr').length,
    ids: Array.from(document.querySelectorAll('#st-model-rows tr')).map(t => t.dataset.id),
    delDisabled: document.querySelectorAll('#st-model-rows .mdl-op')[2].disabled,
    onId: document.querySelector('#st-model-rows tr.on')?.dataset.id };`);
  // 删掉的正是当前启用的那条 → 必须自动换到剩下的一条，不留悬空引用。
  // 悬空的后果是「当前模型」指向一条不存在的记录，生成时取不到任何连接信息。
  check("删除当前启用的模型后自动落到剩下那条，且「删除」重新变灰",
    mdlAfter.rows === 1 && mdlAfter.ids[0] === "m1"
    && mdlAfter.onId === "m1" && mdlAfter.delDisabled === true,
    JSON.stringify(mdlAfter));
  await evalIn(`document.getElementById('btn-close-settings').click(); return true;`);
  await sleep(200);
  // 冷启动就配好的情形（老用户）：hero 不该停在配置引导上
  check("已配置时冷启动空态就是「想聊点什么？」（不误报）",
    /想聊点什么/.test(cfgd.heroTitle) && cfgd.heroBtn === false,
    JSON.stringify({ t: cfgd.heroTitle, btn: cfgd.heroBtn }));
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
  // shot() / closeMenus 定义在脚本开头（任何一步都能用）。
  // ── 输入区：一体化卡片 ─────────────────────────────────────
  // 修复前是「参数条 / 输入框 / 提示行」三块垂直堆叠的独立块：参数（次要控件）
  // 占了输入框**上方**最贵的位置，三块各有各的边界。现在合并为一个卡片：
  // textarea 在上、工具条在下，卡内不画分隔线（靠留白分区）。
  // 这一组断言取代了原来的「间距 12px / 左右对齐 / 描边一致」三条 —— 那三条
  // 都在描述**两个独立块之间**的关系，合并成一个容器后它们不再成立。
  const readComposer = () => evalIn(`return (() => {
    const qb = document.getElementById('quick-params');
    const cb = document.getElementById('composer-body');
    const topic = document.getElementById('topic');
    const tools = document.querySelector('#composer-body .composer-tools');
    const send = document.getElementById('btn-generate');
    const model = document.getElementById('model-pick');
    if (!qb || !cb || !topic || !tools || !send || !model) return { missing: true };
    const r = (n) => n.getBoundingClientRect();
    const cs = getComputedStyle(tools);
    const cbs = getComputedStyle(cb);
    const ss = getComputedStyle(send);
    return {
      inside: cb.contains(qb),
      topicInCard: cb.contains(topic),
      toolsBelowTopic: r(tools).top >= r(topic).bottom - 1,
      sendRight: Math.abs(r(send).right - r(tools).right),
      modelLeftOfSend: r(model).right <= r(send).left + 1,
      sameRow: Math.abs(r(model).top - r(send).top) < 8,
      toolCount: qb.querySelectorAll('.select-wrap.pill').length,
      gear: !!qb.querySelector('.qp-more'),
      sep: cs.borderTopWidth,
      composerTop: getComputedStyle(document.getElementById('composer')).borderTopWidth,
      face: cbs.backgroundColor,
      ring: cbs.boxShadow,
      sendW: r(send).width,
      sendRadius: parseFloat(ss.borderTopLeftRadius),
      sendBg: ss.backgroundColor,
      sendBgImg: ss.backgroundImage,
      sendDisabled: send.disabled,
    }; })()`);
  // 卡片面在**两种状态下都要立得住**，所以两态都读一次。
  // 为什么不能只读一态：应用启动时是自动聚焦的（main.js 的 $("topic").focus()），
  // 用户开机看到的就是「聚焦白面」那一态 —— 只测未聚焦态会漏掉他真正在看的东西。
  // ⚠ 切换状态后必须等**过渡跑完**再读（CSS 里给背景与投影都挂了 transition）：
  // getComputedStyle 在过渡进行中返回的是**插值中的当前值**，紧接着读会读到上一态的
  // 残留，断言就成了「在测一个不存在的瞬间」。
  // 这里踩过：blur 后立刻读是对的（首次无过渡），focus 后立刻读却拿到了未聚焦的值。
  await evalIn(`document.getElementById('topic').blur(); return true;`);
  await sleep(260);
  const composeIdle = await readComposer();
  await evalIn(`document.getElementById('topic').focus(); return true;`);
  await sleep(260);
  const compose = await readComposer();
  // 把「白底上的等效灰度」算出来。--fill 是 rgba(0,0,0,.04)，直接读 RGB 通道会读到
  // 0（黑），必须按 alpha 合成到白底上才是眼睛看到的颜色 —— 否则断言会把
  // 「几乎纯黑」判成「浅灰」，等于没测。
  const overWhiteLum = (s) => {
    const m = /rgba?\(([\d.]+),\s*([\d.]+),\s*([\d.]+)(?:,\s*([\d.]+))?\)/.exec(s || "");
    if (!m) return null;
    const a = m[4] === undefined ? 1 : +m[4];
    const sum = [+m[1], +m[2], +m[3]].reduce((acc, c) => acc + (c * a + 255 * (1 - a)), 0);
    return Math.round(sum / 3);
  };
  check("参数条已并入输入框卡片（不再是并列的兄弟块）",
    !compose.missing && compose.inside === true && compose.topicInCard === true,
    JSON.stringify(compose).slice(0, 160));
  // 卡内不画分隔线：输入区与工具条靠留白分区。参考图里的输入卡都是浑然一体的，
  // 通栏的线等于把一个卡片切成两个「小卡片」。
  check("工具条位于输入框下方，且卡内不再有分隔线",
    compose.toolsBelowTopic === true && parseFloat(compose.sep) === 0,
    `below=${compose.toolsBelowTopic} sep=${compose.sep}`);
  // 输入区整体上方那条通栏线也去掉：卡片自己已有边界，再压一条线是两套边界语言。
  check("输入区上方不再有通栏分割线",
    parseFloat(compose.composerTop) === 0, `borderTop=${compose.composerTop}`);
  // 卡片靠**一根看得见的描边**跟页面分家，不靠面色。
  // 三张参考图逐像素量过：填充都是白（255），边界都是「1px × 亮度 230」（≈10% 黑），
  // 且没有投影 —— 没有一张用灰底。所以这里守的是「有效墨量够深」。
  // 判据取 alpha × 宽度，而不是只看颜色或只看宽度：修复前是 0.5px × 7%
  // （有效墨量 0.035），参考图是 1px × 10%（0.10）—— 差 3 倍，只查一个维度会漏。
  // 阈值 0.09 = 参考图的九成：0.12（现状）过，0.07（1px 但颜色太浅）也红 ——
  // 「宽度够了颜色不够」这种半修法同样要被拦住。
  const ringInk = (s) => {
    const seg = String(s).split(/,(?![^(]*\))/)[0] || "";
    const m = /rgba?\(\s*[\d.]+\s*,\s*[\d.]+\s*,\s*[\d.]+\s*(?:,\s*([\d.]+))?\s*\)\s*[-\d.]+px\s+[-\d.]+px\s+[-\d.]+px\s+([-\d.]+)px/.exec(seg);
    return m ? (m[1] === undefined ? 1 : parseFloat(m[1])) * parseFloat(m[2]) : 0;
  };
  check("输入卡未聚焦时靠描边分家（白底 + 有效墨量够深）",
    composeIdle.face === "rgb(255, 255, 255)" && ringInk(composeIdle.ring) >= 0.09,
    `face=${composeIdle.face} ink=${ringInk(composeIdle.ring)} ring=${String(composeIdle.ring).slice(0, 56)}`);
  check("输入卡聚焦时仍是白底 + 同样的描边（只多一层抬升）",
    compose.face === "rgb(255, 255, 255)" && ringInk(compose.ring) >= 0.09,
    `face=${compose.face} ink=${ringInk(compose.ring)}`);
  // 聚焦多出来的那层是**真实的抬升**，不是 5% 的装饰（原来两层都是 5%，
  // 在纯白页面上等于没有）。判据用「最大模糊半径」而不是比对整串：
  // 写死整串的话换个配色就红，而这里要守的是「有一次真实的抬升」。
  const maxShadowBlur = (s) => Math.max(0, ...(String(s).match(/([\d.]+)px/g) || [])
    .map((v) => parseFloat(v)));
  check("聚焦比未聚焦多一层真实抬升（投影最大模糊 ≥ 16px）",
    maxShadowBlur(compose.ring) >= 16 && maxShadowBlur(composeIdle.ring) < 16,
    `focusBlur=${maxShadowBlur(compose.ring)} idleBlur=${maxShadowBlur(composeIdle.ring)}`);
  check("发送键仍在工具条右端（保持右下角）",
    compose.sendRight < 12, `rightOffset=${compose.sendRight}`);
  check("模型选择器紧邻发送键左侧、同一行",
    compose.modelLeftOfSend === true && compose.sameRow === true,
    `leftOfSend=${compose.modelLeftOfSend} sameRow=${compose.sameRow}`);
  // 只展示重要参数：条数写死会随分层调整而漂，所以断言的是「明显少于全量」
  // 且「至少还有 3 个」—— 前者防回归到「全塞进来」，后者防被清空。
  check("工具条只展示重要参数（已从全量精简）",
    compose.toolCount >= 3 && compose.toolCount <= 4, `toolCount=${compose.toolCount}`);
  check("「更多设置」不再占一个文字按钮位（降级为参数组末尾的图标）",
    compose.gear === true, `gear=${compose.gear}`);
  // 发送键是**圆角方块**（参考图里三个输入卡都是），不是正圆。
  // 判据用「圆角明显小于半宽」而不是写死 10px —— 正圆时 radius == width/2，
  // 写死数值的话换个尺寸就失去意义了。
  check("发送键是圆角方块而非正圆",
    compose.sendRadius > 0 && compose.sendRadius <= compose.sendW / 2 - 3,
    `radius=${compose.sendRadius} width=${compose.sendW}`);
  // 空输入时按钮不可点，但**不能隐形**。判据不是「有没有底色」—— --fill 也有底色，
  // 那种问法测不出这个 bug。真正的契约是「底色跟它所在的卡面拉不拉得开」：
  // 卡面改成浅灰之后，--fill 那种「跟卡面同色」的底就彻底消失了。
  // 两种卡面都要拉得开（现在两态都是白面，但仍照两态各查一次 ——
  // 哪天某一态的面色改了，这条会立刻说话）。
  const SEND_MIN_DELTA = 12;
  const sendLum = overWhiteLum(compose.sendBg);
  const idleFaceLum = overWhiteLum(composeIdle.face);
  const focusFaceLum = overWhiteLum(compose.face);
  check("空输入时发送键仍看得见（底色与两种卡面都拉得开）",
    compose.sendDisabled
      ? idleFaceLum - sendLum >= SEND_MIN_DELTA && focusFaceLum - sendLum >= SEND_MIN_DELTA
      : compose.sendBgImg !== "none",
    `disabled=${compose.sendDisabled} send=${sendLum} idleFace=${idleFaceLum} focusFace=${focusFaceLum}`);

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
  // 输入区特写：一体化卡片是这轮改动的核心，而整屏图上它只有几十像素高、
  // 细节全糊在一起。按 #composer 的实际矩形裁一张 2 倍图当证据。
  await evalIn(`${closeMenus} window.__ts.newChat(); return true;`);
  await sleep(300);
  const cbox = await evalIn(`const r = document.getElementById('composer').getBoundingClientRect();
    return { x: Math.max(0, r.left - 8), y: Math.max(0, r.top - 12),
             width: r.width + 16, height: r.height + 24, scale: 2 };`);
  await shot("main-composer.png", null, cbox);
  // 未聚焦态单独来一张：两态差别只在「有没有那层抬升」，而这个差别只有在
  // 并排看两张图时才看得出来 —— 只看一张的话，「另一态」永远没人见过。
  await shot("main-composer-idle.png", `document.getElementById('topic').blur(); return true;`, cbox);
  await evalIn(`document.getElementById('topic').focus(); return true;`);
  await shot("main-sidebar.png", `${closeMenus} return true;`);
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
  // 高级配置收起来是默认态（上面那张），展开态也要留一张 ——
  // 只截收起态的话，里面四项的「内置默认」小标在审查时根本看不到。
  await shot("settings-llm-adv.png",
    `window.__ts.setPane('llm');
     document.getElementById('st-adv').open = true; return true;`);
  // 模型弹窗：添加 / 编辑共用的那张表单，是这一版新增的主要界面
  await shot("settings-llm-dialog.png",
    `document.querySelectorAll('#st-model-rows tr.on .mdl-op')[1].click(); return true;`);
  await evalIn(`document.getElementById('md-cancel').click(); return true;`);
  await sleep(200);
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
