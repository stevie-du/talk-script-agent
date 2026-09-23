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
const { probeSource } = require("./lib/overflow");

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
            style: "口播科普", persona: "维保老师傅", segment: "家用电梯", audience: "业主乘客",
            // 目标语速（`pack.yaml` 的 rate_by_style 经 _normalize 落到 params.rate）。
            // 桩里给真值，好让"每段语速 vs 目标"那条断言能算得出期望。
            rate: 4.5 },
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
    // 人味报告（§2.7 ③）：桩里给两条带 `words` 的命中 —— 正文下划线读的就是它。
    // ⚠ `words` 必须是段内**原文子串**（后端 TellHit.words 的契约），
    //   桩里写成别的字样那条下划线就找不到位置、静默不出现。
    ai_tells: {
      score: 88, strong: 0, weak: 2, tells_enabled: ["bookish_connective", "no_specific"],
      config_warnings: [], placeholders: { count: 1, per_100: 2.4, cap: 4 },
      hits: [
        { id: "bookish_connective", severity: "weak", count: 2, where: "第2段",
          detail: "第一×1、第二×1", words: ["第一，看井道尺寸和载重。"] },
        { id: "list_enumeration", severity: "weak", count: 2, where: "第3段",
          detail: "清单体连排：第一…第二", words: ["第二，看维保响应时间。"] },
      ],
    },
  },
  placeholders: ["{{待补：主力机型载重}}"],
  // 两条回炉记录：新形态带机器码 action_code，第二条**故意只带中文标签**，
  // 用来覆盖 result.js 里对老产物的兜底分支（少一条，那条分支就在门禁里跑不到）。
  revisions: [{ round: 1, action: "全文回炉", action_code: "full_recheck",
                report: { deviation_pct: 24.0, hard_hits: [{ word: "政府补贴", count: 1 }], chars_total: 320 } },
              { round: 2, action: "全文回炉",
                report: { deviation_pct: 12.0, hard_hits: [{ word: "绝对靠谱", count: 1 }], chars_total: 300 } }],
  timings: [{ start: 0, end: 3 }, { start: 3.5, end: 7 }, { start: 7.5, end: 11 }, { start: 11.5, end: 13 }],
  logs: [{ key: "select", title: "选题策划", ts: "2026-09-13T10:00:01", data: {} }],
};

const META = {
  default_pack: "elevator", model: "glm-4.7", base_url: "https://x/v4",
  has_api_key: true, mock: false, max_concurrent: 4, version: "0.2.0",
  packs_dir: "C:/Users/test/AppData/Roaming/TalkScript/packs",
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
      param_audit: { platform: { "小红书": "平台分级词表未定义该平台：只按通用词表校验，平台差异化红线不生效" } },
      // 情报源声明（B 线，§2.2）：选题页的分区 / 筛选行 chip / 计数 /
      // 情报源表**四处都由它渲染**，所以桩也必须带上 —— 少了它，那四条断言
      // 会退化成"渲染了空列表"而照样绿（空转）。
      intel_sources: [
        { id: "demand_terms", label: "下拉词", platform: "百度", role: "雷达",
          cadence: "daily", note: "搜索联想词 diff", params: {}, enabled: true, wired: true },
        { id: "bilibili_search", label: "B站同类", platform: "B站", role: "供给度量",
          cadence: "on_demand", note: "", params: {}, enabled: true, wired: true },
        { id: "hot_board", label: "抖音热榜", platform: "抖音", role: "破圈触发器",
          cadence: "daily", note: "命中 0 是正常结果", params: {}, enabled: true, wired: true },
        { id: "xhs_board", label: "小红书", platform: "小红书", role: "破圈触发器",
          cadence: "—", note: "没有公开榜，要 x-s 签名 —— 不做", params: {},
          enabled: false, wired: false },
      ] },
    { name: "fitment", display_name: "全屋定制包", draft: true,
      params: { segment: { label: "细分领域", default: "全屋定制", options: ["全屋定制"] } } },
  ],
};

// 桩：今日选题的一份结果。形状与 app/intel.py 的 `today()` 完全一致
// （groups 按声明走，items 带 score/flags/ev）。
const INTEL_TODAY = {
  pack: "elevator", fetched_at: "2026-09-23T10:12:00+08:00", stale: false,
  ignored: 2, unwired: 1, errors: {},
  items: [
    { guid: "g-new", title: "电梯应急更换程序指引", desc: "本周新出，几乎没人写",
      source_id: "demand_terms", source_label: "下拉词", platform: "百度",
      role: "雷达", segment: "维保", published: "", url: "", ev: "本周新出",
      score: { D: 1.0, S: 0.22, E: 0.7, opportunity: 78, is_new: true,
               demand_new: 2, supply_n: 1, total_n: 3, fact_density: 0.33 },
      flags: [["good", "机会分高"], ["good", "本周新出"]] },
    { guid: "g-old", title: "住宅老旧电梯申报国债补贴工作指引", desc: "存量话题",
      source_id: "demand_terms", source_label: "下拉词", platform: "百度",
      role: "雷达", segment: "旧楼加装", published: "", url: "", ev: "存量话题",
      score: { D: 0.5, S: 0.9, E: null, opportunity: 5, is_new: false,
               demand_new: 1, supply_n: 9, total_n: 10, fact_density: 0.6 },
      flags: [["bad", "红海 · 需换角度"]] },
    { guid: "g-bili", title: "困人的第一原因不是电梯坏了，是人", desc: "同题已有 41 条",
      source_id: "bilibili_search", source_label: "B站同类", platform: "B站",
      role: "供给度量", segment: "维保", published: "1758000000", url: "https://b/1",
      ev: "存量话题",
      score: { D: 1.0, S: 1.0, E: 0.12, opportunity: null, is_new: false,
               demand_new: 2, supply_n: 9, total_n: 3, fact_density: 0.33 },
      flags: [["warn", "数据不足 · 算不出机会分"]] },
    { guid: "g-hot", title: "被关 30 分钟，能索赔吗", desc: "榜上 6 小时",
      source_id: "hot_board", source_label: "抖音热榜", platform: "抖音",
      role: "破圈触发器", segment: "维保", published: "", url: "",
      ev: "本周新出", warn: "禁「包赔 / 一定赔」，命中 banwords hard",
      score: { D: 1.0, S: 0.3, E: 0.95, opportunity: 70, is_new: true,
               demand_new: 2, supply_n: 3, total_n: 3, fact_density: 0.33 },
      flags: [["good", "机会分高"]] },
  ],
  groups: [
    { id: "demand_terms", label: "下拉词", platform: "百度", role: "雷达",
      cadence: "daily", note: "搜索联想词 diff", enabled: true, wired: true,
      state: "ok", count: 4, items: [] },
    { id: "bilibili_search", label: "B站同类", platform: "B站", role: "供给度量",
      cadence: "on_demand", note: "", enabled: true, wired: true,
      state: "ok", count: 3, items: [] },
    { id: "hot_board", label: "抖音热榜", platform: "抖音", role: "破圈触发器",
      cadence: "daily", note: "命中 0 是正常结果", enabled: true, wired: true,
      state: "ok", count: 1, items: [] },
    { id: "xhs_board", label: "小红书", platform: "小红书", role: "破圈触发器",
      cadence: "—", note: "没有公开榜，要 x-s 签名 —— 不做", enabled: false,
      wired: false, state: "off", count: 0, items: [] },
  ],
};
// 再补 4 条：一屏 5 条，**只有 4 条时「换一批」是 disabled**，
// 那条断言就退化成"点了个不能点的按钮"，`calls===0` 与"没重抓"之间没有因果。
(function () {
  const mk2 = (guid, title, label, sid, seg, opp) => ({
    guid: guid, title: title, desc: "补样本", source_id: sid, source_label: label,
    platform: "", role: "", segment: seg, published: "", url: "", ev: "存量话题",
    score: { D: 0.5, S: 0.5, E: 0.4, opportunity: opp, is_new: false,
             demand_new: 1, supply_n: 1, total_n: 2, fact_density: 0.5 },
    flags: [] });
  INTEL_TODAY.items.push(
    mk2("g-n2", "电梯维保记录该谁存", "下拉词", "demand_terms", "维保", 40),
    mk2("g-n3", "加装电梯资金怎么摊", "下拉词", "demand_terms", "旧楼加装", 30),
    mk2("g-b2", "维保记录为什么查不到", "B站同类", "bilibili_search", "维保", 20),
    mk2("g-b3", "电梯年检到底查什么", "B站同类", "bilibili_search", "检验检测", 10));
})();
INTEL_TODAY.groups[0].items = INTEL_TODAY.items.filter(i => i.source_label === "下拉词");
INTEL_TODAY.groups[1].items = INTEL_TODAY.items.filter(i => i.source_label === "B站同类");
INTEL_TODAY.groups[2].items = INTEL_TODAY.items.filter(i => i.source_label === "抖音热榜");

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
  // 今日选题的桩数据：必须**内联进桩脚本**（照 META 的写法），
  // 直接在桩里写 INTEL_TODAY 是拿不到的 —— 那是 Node 侧的常量，
  // 浏览器里没有它，fetch 一抛就被 load() 的 catch 接成空态（症状：全 0 条）。
  var INTEL = ${JSON.stringify(INTEL_TODAY)};
  // 2026-09-17：把 META 挂到 window 上，让 verify.js 的 evalIn 也能读当前 pack 的
  // params 列表（任务 2 的"全量参数"断言需要比对 expectedKeys 与实际渲染的 keys）。
  // 仅暴露元信息（不暴露 mock 函数 / 接口状态），是只读快照，不污染调用方。
  window.__tsMeta = META;
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
  // ── 带占位符的那份产物（P3-8 / P3-9 的现场，由 /api/jobs/jobph 返回）
  // 两张卡各带一种 .over，而且**文档顺序相反于该跳的那个**：
  //   第 2 张卡头：字数超配额 → span class="quota over" 120/85 字
  //   第 3 张卡正文：一个真占位 → span class="over jumpable"
  // 「点击定位首处」原来找的是 .script-card .over，抓到的是字数胶囊（P3-9）。
  // 占位符本身按引擎的约定写成带「待补：」前缀的形式（app/knowledge.py 与
  // 包模板就是要模型这么写），于是屏幕上会不会多印一层
  // 「待补：」也正好是这条夹具能量的东西（P3-8）。
  var PH_RESULT = JSON.parse(JSON.stringify(RESULT));
  PH_RESULT.sections[2].text = '第三，载重按{{待补：主力机型载重}}来定，别听口头报数。';
  PH_RESULT.sections[2].subtitle = '载重怎么定';
  PH_RESULT.check.segments[1] = { type: 'point', chars: 120, quota: 85 };
  PH_RESULT.placeholders = ['{{待补：主力机型载重}}'];
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
  // 取消建包作业失败的情形：&pgcancelfail=1（P3-10）。
  // 「取消中…」是一句关于后端的承诺：请求本身失败时它必须收回去，
  // 不能永久停在「取消中…」并且 disabled —— 那时用户既停不掉它，也退不回表单。
  var PGCANCELFAIL = /(^|[?&])pgcancelfail=1/.test(location.search);
  // 哪些 LLM 字段还是内置默认（config.yaml 里没写、环境变量也没有）。
  //
  // ⚠ 桩必须**有状态**，且状态要按「文件里存了什么」来算 —— 不能写成
  // 「保存过一次就清空」。模型从一条变成一份列表之后，「没配过」是**逐字段**
  // 的判断：用户只填了 Key、没动请求地址时，地址仍然是内置默认。
  // 按「保存过就全清」写的话，「保存后小标消失」这类断言会**假绿** ——
  // 它测的是一个与真实后端不同的口径。
  var CONFIGURED = !NOKEY && !CFGERR;
  var DEFAULT_URL = "https://x/v4", DEFAULT_NAME = "glm-4.7";
  // 2026-09-17：全新安装**不再预置模型**（用户原话「没有内置默认的模型的，
  // 需要用户自己添加，添加完还需要支持删除」）—— 真实后端在「文件里没有任何
  // 连接信息」时给出**空列表**。查询串 nomodels=1 模拟那个形态（一条都没有），
  // 默认仍给一条「空壳」：它 = 迁移产物 / 用户加了一条没填全，
  // 且本文件大量断言要验「有模型时」的行为，需要这个起点。
  var NOMODELS = /(^|[?&])nomodels=1/.test(location.search);
  // 原始条目：**空字段保持空**（与真实后端一致 —— 只有空 base_url 才报「内置默认」）
  var MODELS = NOMODELS ? [] : [{ id: "m-default", name: "",
                  base_url: CONFIGURED ? DEFAULT_URL : "",
                  api_key: CONFIGURED ? "sk-stub" : "",
                  model: CONFIGURED ? DEFAULT_NAME : "" }];
  var ACTIVE = NOMODELS ? "" : "m-default";
  var savedNum = CONFIGURED ? { temperature:1, retries:1, timeout:1, max_tokens:1 } : {};
  // 一条模型都没有时给一个「空条目」——真实后端也是这么做的（config.py 的
  // cur = next(...) or LLMModel(...)），不能让 activeRaw() 返回 undefined。
  // ⚠ 注释里不许用反引号：桩整体是一个模板串，反引号会提前把它截断。
  var EMPTY_RAW = { id: "", name: "", base_url: "", api_key: "", model: "" };
  function activeRaw() {
    if (!MODELS.length) return EMPTY_RAW;
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
    // 连接两项只在**有模型**时才进名单（没有模型时该说的是「还没有配置模型」，
    // 而不是「你的地址是内置默认」）—— 与真实后端 load_config 的口径一致。
    if (MODELS.length) {
      var r = activeRaw();
      if (!r.base_url) d.push("base_url");
      if (!r.model) d.push("model");
    }
    return d;
  }
  function hasKey(){ return !!activeRaw().api_key; }
  // 后端 GET /api/config 常驻 base_url_warnings、保存/测试回 warnings。
  // 桩**固定给一条**：这里验的是「后端给了字段，界面有没有人读」，
  // 判定逻辑（哪个地址该警、哪个不该）由后端自己的
  // tests/test_server_hardening.py::test_base_url_rejects_and_warns 守，
  // 不在渲染层重算一遍 —— 那只会让两边一起错。
  var WARN_HTTP = ['当前模型地址走明文 http（http://192.168.2.10:9200/v1）：'
                   + '密钥与整段提示词会明文穿过网络，请只在可信内网这样用。'];
  function err(code, detail) {
    return Promise.resolve(new Response(JSON.stringify({ detail: detail }),
      { status: code, headers: { 'Content-Type': 'application/json' } }));
  }
  // 带**机器可读码**的错误（app/server.py 的 _error_json 就是 detail + code 两份）。
  // 桩必须给得出 code，否则渲染层「按 code 判、不按状态码判」这条路径
  // 在本文件里永远是空转 —— 桩只回 {detail} 时，任何 409 都只能凭文案认，
  // 于是坏包被说成队列满也测不出来（P1-1）。
  function errc(status, detail, code) {
    return Promise.resolve(new Response(JSON.stringify({ detail: detail, code: code }),
      { status: status, headers: { 'Content-Type': 'application/json' } }));
  }
  function configBody() {
    var r = activeRaw();
    // 一条模型都没有时 base_url / model 是**空串**（不是兜底值）——
    // 与真实后端一致：_effective() 只在**有条目**时才兜底。
    return { base_url: MODELS.length ? (r.base_url || DEFAULT_URL) : "",
             model: MODELS.length ? (r.model || DEFAULT_NAME) : "",
             api_key_set: hasKey(), mock: false, retries: 2, timeout: 180,
             max_tokens: 16000, temperature: 0.7, env_override: false,
             defaults: { base_url: DEFAULT_URL, model: DEFAULT_NAME },
             llm_defaulted: llmDefaulted(),
             models: publicModels(), active_model: ACTIVE,
             config_error: CFGERR
               ? "config.yaml 语法有误（mapping values are not allowed here，第 2 行第 44 列）"
               : "",
             base_url_warnings: WARN_HTTP };
  }
  var calls = { gen:0, job:0, cancel:0, rewrite:0, pg:0 };
  // created_at 在一个作业的生命周期里是**固定**的（真引擎取的是 Job.created_at）。
  // 原来两处作业分支各自现算一个"多久以前"，等于每次轮询都把作业重新出生一次
  // —— 于是「已用 N 秒」被钉死在桩自己给的那个常数上，计时器相关的断言红的是桩、
  // 不是实现。第一个答案进来时记账，之后一律复用同一个值。
  var born = {};
  var PG_CLAIMED = [];   // 建包占位**目录名**集合：与 app/packgen.claim_slug 同一口径
  // 归还时必须还得是本次占住的那个键。原来两处归还都写死 pgSlug('全屋定制/装修')：
  // 只要有用例提交别的行业名（口腔诊所、猫咖…），它的键就**永远留在集合里**，
  // 之后同一个名字再提交就被桩判成"正在创建中" —— 桩又比后端严了另一个方向。
  // ⚠ 一个键也不够（第 10 轮复核 P2）：两次提交不同行业名时，第二次会把归还用的
  //   键覆盖掉，第一次那个名字就永久卡在"正在创建中" —— 桩凭空造出一个真引擎不会
  //   给的、而且**再也退不掉的** 409。所以占位键跟着作业走：每个建包作业一条记录。
  var PG_JOBS = {};      // job_id -> { slug, industry, polls, state, born }
  var PG_SEQ = 0;
  // 已经建成的包目录（引擎侧是 packs/<slug>/ 真的在那儿）：桩必须分得清
  // 「还在创建中」与「已经存在」两句不同的 409（app/packgen.py 的两条 raise）。
  var PG_CREATED = {};
  // 占位的上限时长：引擎在建包 worker 的 finally 里归还，与"有没有人轮询"无关；
  // 桩的作业只在被轮询时才推进，于是一条再没人问的作业会把名字**永久**占着
  // （第 11 轮复核 P2：12p 之外又留下两条永久 409）。桩没有线程可杀，就给占位一个
  // 到点该归还的占位（数值见常量，注释里不抄数字——这条由 test_runtime_requirements 盯着）：
  // 超时的作业按"预算用尽"落 failed 并归还 —— 与引擎那条路径同形
  // （失败 + 额度还回来），至少不会永久 409。窗口远大于任何一条断言的轮询间隔。
  var PG_CLAIM_MS = 60000;
  // 页面侧要能读到这个出厂值（断言用它钉值域）：PG_CLAIM_MS 是桩这段 IIFE 的局部变量，
  // 注入脚本之外看不见，所以显式挂出去 —— 与 __pgclaimms（注入用的旋钮）是两回事。
  window.PG_CLAIM_MS_DEFAULT = PG_CLAIM_MS;
  function pgJobId(u) {
    var tail = String(u || '').split('/api/jobs/')[1] || '';
    return tail.split(/[/?#]/)[0];
  }
  function pgSweepStale() {
    var nowT = Date.now();
    // 时限可以注入（window.__pgclaimms）：否则这条回收线在门禁里从不自发触发，
    // 它造的那句 failed 文案与"占位被归还"这两件事就永远没有断言覆盖
    // （第 12 轮复核：把 PG_CLAIM_MS 改成 600000000，331/331 照绿）。
    var lim = (typeof window.__pgclaimms === 'number') ? window.__pgclaimms : PG_CLAIM_MS;
    Object.keys(PG_JOBS).forEach(function (k) {
      var j = PG_JOBS[k];
      if (j.state === 'packing' && j.slug && nowT - j.born >= lim) {
        PG_CLAIMED = PG_CLAIMED.filter(function (x) { return x !== j.slug; });
        PG_JOBS[k] = { slug: '', industry: j.industry, polls: j.polls, state: 'failed',
                       born: j.born, error: '作业超时未收工（桩的占位回收，对应引擎的整作业预算）' };
      }
    });
  }
  // 建包作业的流式块只有一份：真引擎只要设过 stream_phase 就带 stream 键
  // （app/jobs.py 的 snapshot）—— 原来 done/cancelled 的快照干脆没有它，
  // 界面里"读不到就当没有"的那一支于是永远不跑（第 12 轮复核 P1）。
  var PG_STREAM = { phase: '行业包生成', reasoning_tail: '先想这个行业的细分领域……',
                    reasoning_len: 512, content_len: 0 };
  // 建包作业也真的有步骤：引擎里 _retry_logger 会往 job.steps 记一条「接口自动重试」
  // （app/pipeline.py 的 _run_packgen），而原来 pgSnap 硬编码 steps 为空数组 ——
  // 于是界面"建包作业也有进度步骤"那一支在门禁里从未跑过（第 16 轮复核 P2-4）。
  var PG_STEPS = [{ key: 'retry', title: '接口自动重试·第 1/2 次',
                    ts: '2026-09-20T10:00:05', data: { note: '模型返回空内容' } }];
  // 两句 404 属于两条不同路由（app/server.py 的 job_status 与 job_cancel），
  // 各起一个名字：桩/服务端对账才能按路由比，而不是"文件里出现过这句就算过"。
  var ERR_JOB_GONE_GET = '作业不存在或已随重启释放';
  var ERR_JOB_GONE_CANCEL = '作业不存在';
  // 快照只有一本账：轮询、取消、幂等重放都走这里（三处各写一份就是第 10 轮那种漂移）
  function pgSnap(pid) {
    var j = PG_JOBS[pid];
    // 字段集对齐 Job.snapshot()：id/kind/state/params/steps/error/created_at，
    // 外加进过终态才有的 result 与设过 stream_phase 才有的 stream。
    // ⚠ 在途与终态之间只差 **result 一个键**（引擎轮询走 include_result=False）。
    //   原来在途那一份是轮询分支里另写的字面量，少了 created_at 与 error ——
    //   界面里「已用 N 秒」和"读不到 error 就不显示"那两支对建包作业永远跑不到
    //   （第 16 轮复核 P2-4）。现在两条路共用这一个函数，字段集不再有第二份。
    //   三个终态名与 app/jobs.py 的 TERMINAL_STATES 同源，由
    //   test_job_state_vocabulary_consistency 对账，不在这里另立一份真相。
    var o = { id: pid, kind: 'packgen', state: j.state, created_at: bornAt(pid, 2000),
              error: j.error || null, stream: PG_STREAM, steps: PG_STEPS,
              params: { industry: j.industry } };
    if (j.state === 'done' || j.state === 'failed' || j.state === 'cancelled') {
      // ⚠ result 在"终态但非 done"时是 **null 而不是缺键** —— 引擎就是这么给的。
      o.result = j.state === 'done' ? PG_RESULT : null;
    }
    return mk(o);
  }
  // 包名的合法性判定与 app/server.py 的 _safe_name 同方向（不合法 → 400，排在"包存在吗"
  // 之前，与引擎一致：_safe_name 在前、Pack(root,name) 在后）。
  // ⚠ 第 12 轮量出"复用 pgSlug 当名称校验"是**第三种编码**且两个方向都错：
  //   -a、a--b、ab- 在引擎里合法（单词字符类 + 连字符），被 pgSlug 削首尾后不再相等 → 桩拒；
  //   而 Cn 类码点引擎 400、桩放行。改成直接写属性类字符集（连字符放末尾为字面量），
  //   与引擎同域；反斜杠还是只能靠 String.fromCharCode(92)（模板字符串会吃掉一层）。
  var NAME_OK = new RegExp("^[" + String.fromCharCode(92) + "p{L}"
                           + String.fromCharCode(92) + "p{N}_-]+$", "u");
  function packNameOk(n) {
    return !!n && n.indexOf('..') < 0 && NAME_OK.test(n);
  }
  // ⚠ 引擎按 slugify 之后的目录名占位，不是按用户输入的那串字：
  //   「全屋定制/装修」与「全屋定制 装修」都会落成 packs/全屋定制-装修/，
  //   真引擎第二次直接 409。桩原来按原文比，等于桩比后端宽容 ——
  //   「界面对重名提交做了什么」又变成量不出来的东西。
  //   ⚠ 本函数整体在模板字符串里：不许出现反引号，也不许写带反斜杠的正则
  //     （反斜杠会被吃掉一层，注进页面就是非法正则 —— 本项目踩过五次）。
  // ⚠ 判据必须与 Python 的"单词字符"类（Unicode 字母数字 + 下划线）同域。
  //   第 7 轮这里是手写区段表，第 8 轮复核对 218 个码点量出 62 个不一致：
  //   ª µ ² ə ᄒ 々 ﬀ 与一切 astral 字符（代理对被拆成两个 - ）被漏掉，
  //   × ÷ ． ＂ ＿ 与组合记号被错留。后果不是"算得不像"，而是桩会造出引擎
  //   根本不会给的 409/400 —— 例如 × 在引擎里目录名为空（花钱前 400、不占位），
  //   在桩里却占住键、第二次提交回 409。手写表追不上 Unicode，所以改用属性类
  //   p{L} p{N} + 下划线（与 CPython 的定义同源），带 u 标志后按码点走。
  //   跨语言一致性由 tests/test_packgen_claim.py 逐码点对账钉住。
  var BS = String.fromCharCode(92);
  // ⚠ V8 与 CPython 的 Unicode 数据版本不是一份：下面这些码点 V8 的 p{L} 认成字母、
  //   Python 的单词字符类不认（引擎把它们折成分隔符）。原来只在测试里"豁免"它们，
  //   第 12 轮实测证明豁免是错的：猫咖+U+088F+甲 引擎给 猫咖-甲、桩给 猫咖᠏甲 ——
  //   桩会占住一个引擎根本不会用的目录名，于是造出引擎不会给的 409/400。
  //   所以这张表必须在**桩这一侧**生效：先按引擎的口径把这些码点折成 "-"，再走属性类。
  //   这份表由 tests/test_packgen_claim.py 与 Python 侧逐码点对账（两边各写一份就是两本账）。
  var PG_SPLIT_CP = [0x88F, 0xC5C, 0xCDC, 0xA7CE, 0xA7CF, 0xA7D2, 0xA7D4, 0xA7F1];
  var PG_SPLIT = PG_SPLIT_CP.map(function (c) { return String.fromCharCode(c); });
  var NON_WORD = new RegExp("[^" + BS + "p{L}" + BS + "p{N}_]+", "gu");
  var EDGE_DASH = new RegExp("^-+|-+$", "g");
  function pgSlug(t) {
    var s = String(t || "");
    for (var si = 0; si < PG_SPLIT.length; si++) {
      s = s.split(PG_SPLIT[si]).join("-");     // 先按引擎口径把分裂码点当分隔符
    }
    return s.replace(NON_WORD, "-").replace(EDGE_DASH, "");
  }
  // 详情里那份文件清单（/api/packs/<name> 的 files）提到外面来：读文件的分支要按
  // **同一份表**回 size —— 两处各写一份就是第二本账（第 8 轮量到：详情说
  // knowledge/topics.md 是 5120 字节，读文件那条分支回的却是 1024）。
  var DET_FILES = [
    {rel:'pack.yaml',size:2048},
    {rel:'skill.yaml',size:1024},
    {rel:'banwords.yaml',size:512},
    {rel:'校对清单.md',size:768},
    {rel:'knowledge/topics.md',size:5120},
    {rel:'knowledge/faq.md',size:3072},
    {rel:'compliance/platform.md',size:1536},
    {rel:'patterns/hook.md',size:2560},
    {rel:'rules/duration.md',size:1024},
    {rel:'private/pricing.md',size:896},
  ];
  // 桩认为"盘上真有的包"，**显示名与包名都从 META 现算**（第 10 轮复核 P3）：
  // 这段原来抄了一份手写表，抄错的那一列让同一个包在下拉里叫「全屋定制包」、
  // 在右栏详情里叫「全屋定制/装修」—— 而 /file 干脆完全忽略包名，任何包名都回
  // DET_FILES 的内容。真引擎先构造 Pack(root, name)，不存在的包直接 404
  // （app/server.py 的 pack_file，在 private/ 判定**之前**），所以两处都必须问同一本账。
  var KNOWN_PACKS = {};
  META.packs.forEach(function (p) { KNOWN_PACKS[p.name] = p.display_name; });
  function bornAt(id, backMs) {
    if (!born[id]) born[id] = new Date(Date.now() - backMs).toISOString();
    return born[id];
  }
  // 建包作业（P1-43）的桩：POST /api/packs/create 只回 job_id，
  // 结果挂在 GET /api/jobs/jobpgN 上。第一拍必须是 packing ——
  // 若桩一上来就 done，界面里那段轮询/进度代码永远不会被执行（空转）。
  // ⚠ 这是一份**共享**的结果体：name/dir 恒为 fitment，不随提交的行业名变
  //   （快照里的 params.industry 才是本次真提交的那个）。要量"界面把哪个包加进了
  //   下拉"这类按对象判定的行为，得先把它改成按作业生成，别在这份常量上加字段。
  var PG_RESULT = {
    name: 'fitment', display_name: '全屋定制/装修', dir: 'C:/packs/fitment', draft: true,
    // 与 app/packgen._checklist 同形状：H1 + 说明 + 一批 "- [ ]" 条目 + 一个 "## " 小节。
    // 原来这里是一行 "1. 核对… / 2. 核对…"，界面拿到什么都在"通过"，
    // 于是清单渲染（P3-50）改成行级结构后桩测不出任何事 —— 桩比后端宽容就是假绿。
    checklist: '# 新行业包校对清单\\n\\n行业：全屋定制/装修（目录 packs/fitment/，**草稿状态**）\\n\\n'
      + '使用前请逐项核实：\\n\\n- [ ] 人造板甲醛释放量分级标准现行编号\\n'
      + '- [ ] 当地加装/改造审批口径\\n- [ ] 主力板材品牌与供货周期\\n\\n'
      + '## 引擎体检发现（生成时自动检测，逐项核实后重跑或用前确认）\\n'
      + '- [ ] 细分领域「预算报价」在模型返回的内容里没有对应段落 —— 已留空骨架\\n',
    verify_list: ['人造板甲醛释放量分级标准现行编号'],
    segments: ['板材环保', '空间规划', '预算报价'],
    audiences: ['装修业主', '二手房翻新业主'],
    personas: ['从业老师傅', '定制设计师'],
    ideas: ['全屋定制报价单，先看这三行', '板材环保等级，一条视频说清', '定制柜安装当天盯住这四处'],
    redlines: ['不承诺绝对零甲醛'],
    banwords_extra_hard: ['绝对零甲醛'],
    banwords_extra_soft: ['最环保'],
  };
  var ELEVATOR_DRAFT = true;   // 有状态：转正后变 false，才能验证按钮消失
  var DRAFTS = {};             // 其它包（如建包用例的 fitment）默认草稿，转正后记 false
  function isDraft(n) { return n === 'elevator' ? ELEVATOR_DRAFT : DRAFTS[n] !== false; }
  window.__calls = calls;
  // window.__shown 已删：它存在的唯一理由是「hero 两套文案都在 DOM 里，读
  // textContent 会把两态拼在一起，两个正则都匹配得上」。[data-when] 那一态
  // 下线后 #empty h3 只剩一份文案，textContent 本身就是无歧义的。
  function mk(o){ return Promise.resolve(new Response(JSON.stringify(o),
    { status:200, headers:{'Content-Type':'application/json'} })); }

  // 会话历史桩：跨 5 天，日期相对「今天」实算（见调用处注释说明为何不能写死）。
  // 覆盖 dayGroupKey 的三个分支：
  //   今天      → 相对词「今天」
  //   昨天      → 相对词「昨天」
  //   3 天前    → 落在「近一周」，label = MM-DD 周X
  //   10/12 天前 → 更早，label = MM-DD
  // ⚠ **必须有两天的天数都 > 7**（这里 10 与 12）。只放一天的话，旧口径的
  //   「更早」桶里就只有一条，而「3 天前」落在「本周」—— 两天天然被分开了，
  //   「相隔一周以上不会被并进同一组」那条断言就会**在变异态下照样绿**
  //   （实测踩过：变异回四档粗桶时那条没红）。多这一天，新旧口径才真正分得开。
  // ⚠ 时间要带 'T' 与秒，与后端 ISO 格式一致（'2026-09-13 10:00' 这种空格分隔的
  //   写法在部分环境按本地时区解析、部分当 UTC，跨时区会让「今天」漂到「昨天」）。
  // ⚠ 这段注释里**不能出现反引号**：整段桩脚本是外层模板串，
  //   反引号会提前把它截断，症状是 Node 报「SyntaxError: Unexpected identifier」。
  function histStub(){
    function at(daysAgo, hh, mm){
      var d = new Date();
      d.setDate(d.getDate() - daysAgo);
      d.setHours(hh, mm, 0, 0);
      function p2(n){ return String(n).padStart(2, '0'); }
      return d.getFullYear() + '-' + p2(d.getMonth() + 1) + '-' + p2(d.getDate())
        + 'T' + p2(hh) + ':' + p2(mm) + ':00';
    }
    return [
      { id:'s2', created_at: at(0, 15, 30), pack:'elevator',
        topic:'扶梯突然停了怎么办', platform:'抖音',
        duration:null, chars:null, passed:null, state:'writing' },
      { id:'s1', created_at: at(0, 10, 0), pack:'elevator',
        topic:'家用电梯怎么挑？', platform:'小红书',
        duration:60, chars:42, passed:true, state:'done' },
      { id:'s3', created_at: at(1, 9, 15), pack:'elevator',
        topic:'电梯困人如何自救', platform:'抖音',
        duration:60, chars:240, passed:true, state:'done' },
      { id:'s4', created_at: at(3, 20, 5), pack:'elevator',
        topic:'加装电梯一楼不同意', platform:'小红书',
        duration:90, chars:330, passed:false, state:'done' },
      { id:'s5', created_at: at(10, 11, 40), pack:'elevator',
        topic:'电梯维保避坑指南', platform:'抖音',
        duration:60, chars:238, passed:true, state:'done' },
      { id:'s6', created_at: at(12, 8, 50), pack:'elevator',
        topic:'电梯日常巡检要点', platform:'抖音',
        duration:60, chars:236, passed:true, state:'done' } ];
  }
  window.fetch = function(u, o){
    var s = String(u);
    var hdrs = (o && o.headers) || {};
    window.__lastHeaders = hdrs;
    // 记录令牌是否真的带上了（回归「渲染层没带 token」这类问题）
    window.__sawToken = !!(hdrs['X-TalkScript-Token'] || hdrs['x-talkscript-token']);
    // ── 情报（今日选题 / 情报源）──
    // 这份桩是**照 pack.yaml 的声明渲染出来的**，不是手写死的一屏 HTML：
    // 分组、chip、计数、未接入 N 全由它算，所以断言测的是"渲染层真的在读声明"。
    if (s.indexOf('/api/intel/today') >= 0) { return mk(INTEL); }
    if (s.indexOf('/api/intel/refresh') >= 0) {
      window.__refreshCalls = (window.__refreshCalls || 0) + 1;
      return mk({ job_id: 'inteljob1' });
    }
    if (s.indexOf('/api/intel/ignore') >= 0) {
      var ib = {}; try { ib = JSON.parse(o && o.body || '{}'); } catch (_) {}
      window.__ignoredKeys = (window.__ignoredKeys || []).concat([ib.key]);
      return mk({ ok: true, ignored: window.__ignoredKeys.length });
    }
    if (s.indexOf('/api/jobs/inteljob1') >= 0) {
      return mk({ id:'inteljob1', kind:'intel', state:'done', params:{ pack:'elevator' },
                  steps:[], error:null, created_at:'2026-09-23T10:00:00',
                  result:{ pack:'elevator', items:4, sources:[], errors:{} } });
    }
    if (s.indexOf('/api/meta') >= 0) {
      return mk(Object.assign({}, META,
        { has_api_key: hasKey(), mock: false, llm_defaulted: llmDefaulted(),
          models: publicModels(), active_model: ACTIVE,
          // 一条模型都没有时头部/选择器不该显示一个兜底出来的模型名
          model: MODELS.length ? META.model : "",
          base_url: MODELS.length ? META.base_url : "" }));
    }
    // ── 模型列表接口 ──
    // 三个具体路径必须排在「/api/models」的通用匹配**之前** ——
    // indexOf('/api/models') 会一并命中 activate / delete，
    // 顺序反了会让「切换当前模型」走进 upsert 分支（凭空多出一条模型）。
    if (s.indexOf('/api/models/activate') >= 0) {
      var ab = {};
      try { ab = JSON.parse(o && o.body || '{}'); } catch (_) {}
      // 空 id = 「都不启用」（2026-09-17，开关要能关掉）—— 与真实后端一致。
      if (ab.id && !MODELS.some(function(x){ return x.id === ab.id; })) {
        return err(404, '没有这个模型：' + ab.id);
      }
      ACTIVE = ab.id || '';
      window.__lastActivate = ab.id;
      return mk({ ok:true, active_model:ACTIVE, models:publicModels() });
    }
    if (s.indexOf('/api/models/delete') >= 0) {
      var db = {};
      try { db = JSON.parse(o && o.body || '{}'); } catch (_) {}
      var before = MODELS.length;
      MODELS = MODELS.filter(function(x){ return x.id !== db.id; });
      if (MODELS.length === before) return err(404, '没有这个模型：' + db.id);
      // 2026-09-17：**允许删到空**（不再拦「至少要保留一个模型」）——
      // 那是「总有一条内置默认」时代的规则。删光 = 还没配，由空列表引导 +
      // 生成前的检查说清。ACTIVE 同步成空串，不留悬空引用。
      if (ACTIVE === db.id || !MODELS.some(function(x){ return x.id === ACTIVE; })) {
        ACTIVE = MODELS.length ? MODELS[0].id : '';
      }
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
        // 新增时请求地址**必填**（2026-09-17）：不再有「内置默认地址」可以兜，
        // 留空会被静默填成别家的 —— 报错要到生成时才出现（老坑）。编辑时留空
        // 仍是「保持不变」，见下面。
        if (!String(mb.base_url || '').trim()) {
          return err(400, '请填写请求地址，例如 https://api.deepseek.com/v1');
        }
        var n = 1;
        while (MODELS.some(function(x){ return x.id === 'm' + n; })) n++;
        mid = 'm' + n;
        target = { id:mid, name:'', base_url:'', api_key:'', model:'' };
        MODELS.push(target);
        if (!ACTIVE) ACTIVE = mid;   // 第一条加进来就该是当前生效的
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
      // 「去生成」必须**不自动发送**：这条计数器是那条断言的判据。
      window.__genCalls = (window.__genCalls || 0) + 1;
      calls.gen++; calls.job = 0;
      // 2026-09-17：两种「不能生成」的原因分开说（与后端 _require_model 一致）——
      // 一条模型都没有 vs 有模型但没填 Key。同一句「未配置 Key」会把前者说成后者。
      if (!MODELS.length) {
        return err(400, '还没有配置模型 —— 请在「设置 → 模型接口」里点右上角「添加模型」');
      }
      if (!hasKey()) {
        return err(400, '当前模型还没配 API Key，请在「设置 → 模型接口」里填写');
      }
      var gbody = {};
      try { gbody = JSON.parse(o && o.body || '{}'); } catch (_) {}
      var gtopic = String(gbody.topic || '');
      // ── 真后端在这条路上会给的三种**开跑前**拒绝（都是 409/404，都不起作业）。
      // 桩原来一个都不给：于是渲染层「409 到底是谁」的判据在本文件里从没被跑过，
      // 坏包被说成「队列满了」（P1-1）也就一直绿着。文案与 code 都按
      // app/pipeline.py + app/knowledge.py + app/server.py 的真形态给。
      if (gtopic.indexOf('额度') >= 0) {
        return errc(409, '同时进行的生成已达上限（4 个），请等其中一条完成后再试',
          'quota_exceeded');
      }
      if (gtopic.indexOf('坏包') >= 0) {
        // knowledge.PackBrokenError 的原话形态：文件名 + 行列号都在。
        return errc(409, '行业包「elevator」的 pack.yaml 语法有误'
          + '（while parsing a block collection，第 4 行第 6 列）', 'pack_broken');
      }
      if (gtopic.indexOf('结构坏了') >= 0) {
        // 同一个 409、另一种坏法：给一个**没有 code** 的应答（老服务形态），
        // 界面既不能说成额度满、也不能编出一个占位原因。
        return err(409, '行业包 elevator 的结构不符合约定（version 必须是数字）');
      }
      // 主题里带「失败」就返回一个必然失败的作业，用来覆盖失败态渲染
      window.__lastJobId = /失败/.test(gtopic) ? 'jobfail'
        : /停不掉/.test(gtopic) ? 'jobrun'
        : /占位/.test(gtopic) ? 'jobph' : 'job1';
      return mk({ job_id: window.__lastJobId });
    }
    // 一条**永远在跑**的作业：用来验「界面接回它之后，别的作业不许把它解锁」。
    // 取消按 id 分开，是因为两条测试要的失败不同：
    //   jobrun/cancel   → 500（P2-3：停止失败 + 重新连接），
    //   jobkeep/cancel  → 正常 cancelled（P1-2：正在看的这一条确实能被 Esc 停掉）。
    if (s.indexOf('/api/jobs/jobrun/cancel') >= 0) {
      // 单独的计数器：不许去动 calls.cancel —— 前面那几条「停止会真正通知后端」
      // 的断言读的就是它，混在一起会让一条新测试悄悄改变老断言的量到的东西。
      calls.failcancel = (calls.failcancel || 0) + 1;
      return err(500, '引擎拒绝取消：作业正在写盘，请稍后重试');
    }
    if (s.indexOf('/api/jobs/jobrun') >= 0) {
      calls.job++;
      return mk({ id: 'jobrun', state: 'writing',
        created_at: bornAt('jobrun', 9000),
        params: { topic: '停不掉的作业', pack: 'elevator', duration: 60 },
        steps: [{ key: 'select', title: '选题策划', ts: '2026-09-13T10:00:01', data:{} }],
        stream: { phase: '文案撰写', reasoning_tail: '先想清楚这个钩子怎么说……',
                  reasoning_len: 88, content_len: 0 } });
    }
    // 一条**永远在跑**的作业，专门给「切走之后 busy 归谁」那组断言当接手对象。
    // 桩原来没有这种分支：未知 id 落到最后的兜底 mk({}) → 界面读到
    // 「认不出来的状态」而直接收工，于是那条作业上根本挂不住 busy（桩与后端不符）。
    // ⚠ 这个 id **刻意不在 histStub 那六条里**：boot() 会把历史里第一条 BUSY 记录
    //   自动接回界面（jobs.js 的 reattachBusyJob），拿现成的 s2 来用的话，
    //   首页那一整组 landing 断言会从第 1 节开始就不是 landing 态了。
    //   真实引擎里"在跑的作业"必然同时出现在 /api/history 与 /api/jobs 两处，
    //   这是桩把两份数据各自编出来的既有妥协，不是本组断言要验的东西。
    if (s.indexOf('/api/jobs/jobkeep/cancel') >= 0) {
      calls.keepcancel = (calls.keepcancel || 0) + 1;
      return mk({ id: 'jobkeep', state: 'cancelled', params: {} });
    }
    if (s.indexOf('/api/jobs/jobkeep') >= 0) {
      calls.keepjob = (calls.keepjob || 0) + 1;
      return mk({ id: 'jobkeep', state: 'writing',
        created_at: bornAt('jobkeep', 6000),
        params: { topic: '另一条还在跑的作业', pack: 'elevator', duration: 60 },
        steps: [], stream: { phase: '文案撰写', reasoning_tail: '正在想救援步骤……',
                             reasoning_len: 40, content_len: 0 } });
    }
    // 带占位符 + 超配额胶囊的那份产物（P3-8 / P3-9 的现场，见 /api/jobs/jobph）。
    if (s.indexOf('/api/jobs/jobph') >= 0) {
      return mk({ id: 'jobph', state: 'done', created_at: bornAt('jobph', 30000),
        params: { topic: '占位与超配额', pack: 'elevator', duration: 60 },
        steps: [{ key: 'select', title: '选题策划', ts: '2026-09-13T10:00:01', data:{} },
                { key: 'write_r1', title: '文案撰写', ts: '2026-09-13T10:00:05', data:{} },
                { key: 'check_r1', title: '校验·第 1 轮', ts: '2026-09-13T10:00:09', data:{} }],
        result: PH_RESULT });
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
      // 前两次是「正在跑」（带思考流与步骤），之后落到完成
      // ⚠ 窗口不能再长：后面「配额降级提示」那一段按固定时长等 done，
      //   多给一拍它就抢在 done 之前读 DOM（实测整段假红）。
      // ⚠ created_at 必须给：真实后端的快照一直有它（app/jobs.py 的 snapshot），
      //   桩漏掉会让「已用 N 秒」这一行永远渲染成空 —— 于是关于它的一切断言
      //   都在量一个空字符串，全绿但什么都没验证（2026-09-20 实测踩过）。
      //   要往回推一段再给，不能取 now：取 now 的话已用时永远停在起点，
      //   量不到它换写法（分/秒）的那一步。
      // ⚠ 本函数整体是一个模板字符串，注释里**不许出现反引号**（会提前闭合）。
      // ⚠ 状态取 rewriting 而不是 writing：后端是**阶段完成后才记那一步**
      //   （app/pipeline.py 里 write 步骤在 writing 结束之后才 append），
      //   所以「已完成的文案撰写」与「文案撰写中」在真实快照里不会同时出现。
      //   桩原来两个都给，界面照它画就会多出一行重复的阶段名 —— 拿一个
      //   后端根本产不出的组合去截图和断言，量到的都是假东西。
      //   rewriting（第 2 轮回炉）才是「已有 select + write 两步、仍在跑」的合法形态。
      if (calls.job <= 2) return mk({ id:'job1', state:'rewriting',
        created_at: bornAt('job1', 8000),
        params:{ topic:'家用电梯怎么挑？', pack:'elevator', duration:60 },
        steps:[{ key:'select', title:'选题策划', ts:'2026-09-13T10:00:01',
                 data:{ usage:{ prompt_tokens:5200, completion_tokens:610,
                                completion_tokens_details:{ reasoning_tokens:380 } } } },
               // usage（P3-15）：真实引擎把上游回的 token 量记在**模型调用那几步**上，
               // 而 check_r1 是纯代码校验、不调模型 —— 留成空 data 就是界面对
               // 「没有 usage 的步骤什么都不许印」的负样本。
               { key:'write_r1', title:'文案撰写', ts:'2026-09-13T10:00:05',
                 data:{ usage:{ prompt_tokens:9800, completion_tokens:2400,
                                completion_tokens_details:{ reasoning_tokens:1500 } } } },
               { key:'check_r1', title:'校验·第 1 轮', ts:'2026-09-13T10:00:08', data:{} }],
        stream:{ phase:'回炉改写', reasoning_tail:'正在斟酌开场钩子……',
                 reasoning_len:136, content_len:12 } });
      return mk({ id:'job1', state:'done',
        created_at: bornAt('job1', 8000),
        params:{ topic:'家用电梯怎么挑？', pack:'elevator', duration:60 },
        // ⚠ usage 必须**与上面那条在跑的快照一致**：完成后界面仍会拿这份快照
        // 重画一次步骤条，这里漏掉 usage 就等于"跑的时候有 token 账、跑完没了"。
        steps:[{ key:'select', title:'选题策划', ts:'2026-09-13T10:00:01',
                 data:{ usage:{ prompt_tokens:5200, completion_tokens:610,
                                completion_tokens_details:{ reasoning_tokens:380 } } } },
               { key:'write_r1', title:'文案撰写', ts:'2026-09-13T10:00:05',
                 data:{ usage:{ prompt_tokens:9800, completion_tokens:2400,
                                completion_tokens_details:{ reasoning_tokens:1500 } } } },
               { key:'check_r1', title:'校验·第 1 轮', ts:'2026-09-13T10:00:09', data:{} }],
        result: RESULT });
    }
    if (s.indexOf('/api/jobs/jobfail') >= 0) return mk({ id:'jobfail', state:'failed',
      created_at: bornAt('jobfail', 12000),
      error:'模型接口连接失败（已重试 3 次）：Server disconnected',
      params:{ topic:'会失败的作业', pack:'elevator', duration:60 }, steps:[] });
    // 删记录：DELETE 必须排在下面那条「/api/history/{id} → 返回产物」之前 ——
    // 两个分支的 URL 形状一样，靠 method 分开。桩记下被删的 id，
    // 让「删整组到底发了哪几条 DELETE」能在断言里数得出来。
    if (s.indexOf('/api/history/') >= 0 && o && o.method === 'DELETE') {
      (window.__delIds || (window.__delIds = [])).push(decodeURIComponent(s.split('/').pop()));
      return mk({ ok: true, id: s.split('/').pop() });
    }
    if (s.indexOf('/api/history/') >= 0) return mk(RESULT);
    // 未配置 Key 的模式要模拟「真·首次运行」：没有 Key **也没有历史记录**，
    // 否则 boot() 会按设计跳过自动打开设置（有历史说明不是第一次用）。
    if (s.indexOf('/api/history') >= 0 && NOKEY) return mk([]);
    // 会话桩：**跨 4 天**，且日子是相对「今天」实算的。
    // ⚠ 不能写死日期（如 2026-09-13）：分组口径是相对时间（今天 / 昨天 / MM-DD），
    // 写死的那天过几天就从「今天」滑到「MM-DD」，断言随日期漂。
    // 这里按「今天 / 昨天 / 3 天前 / 10 天前」各造一条，覆盖 dayGroupKey 的
    // 三个分支（相对词、带星期的近一周、只留日期的更早）。
    if (s.indexOf('/api/history') >= 0) return mk(histStub());
    if (s.indexOf('/api/config/test') >= 0) return mk({ ok:true, model:'glm-4.7', detail:'延迟 320ms',
      warnings: WARN_HTTP });
    // 必须在 /api/config 的通用匹配之前：indexOf('/api/config') 也会命中 reset
    if (s.indexOf('/api/config/reset') >= 0) return mk({ ok:true, fields:['base_url'] });
    // 匹配任意包名的转正：实际请求可能是 fitment（测试里切过包）。
    // ⚠ 名字要**回显请求里那一个**，并且只在真的是 elevator 时才动 ELEVATOR_DRAFT：
    //   原来恒回 name:'elevator'，等于桩替界面把"改错了包"这件事掩盖掉（批次 10 复核指出）。
    if (s.indexOf('/undraft') >= 0) {
      // ⚠ 这段在模板字符串里：注释里不许出现反引号，也不许写带反斜杠的正则
      //   （反斜杠会被模板吃掉一层，注进页面就是非法正则 —— 这条陷阱本项目踩过三次）。
      //   所以这里用 split 取包名。
      var segUnd = (s.split('/api/packs/')[1] || '');
      var undName = decodeURIComponent(segUnd.split('/undraft')[0] || 'elevator');
      window.__und = { url: s, name: undName };        // 诊断：断言失败时看得见的入口
      if (undName === 'elevator') ELEVATOR_DRAFT = false;   // 服务端状态真的变了
      else DRAFTS[undName] = false;
      // 回给界面的 draft 位必须与桩自己刚改过的状态一致：原来非 elevator 一律回
      // true，等于"服务端说它还是草稿"而注册表里已经转正 —— 只是界面当前不看
      // 响应体才没暴露（settings.js 的 undraftPack 走的是重新拉详情）。
      return mk({ ok:true, name:undName, draft: isDraft(undName) });
    }
    if (s.indexOf('/api/packs/') >= 0 && s.indexOf('/file') >= 0) {
      // ⚠ 包名也要问一本账（第 10 轮复核 P3）：这段原来**完全忽略包名**，
      //   任何包名都回 DET_FILES 里的内容 —— 而真引擎先构造 Pack(root, name)，
      //   不存在的包直接 404（app/server.py 的 pack_file，在 private/ 判定**之前**）。
      //   桩替实现把这一格演成成功，「界面点了个不存在的包还能读到正文」就量不出来。
      var filePack = decodeURIComponent((s.split('/api/packs/')[1] || '').split(/[/?#]/)[0] || '');
      // 顺序与引擎一致：先 _safe_name（不合法 400），再 Pack() 存在性（未知包 404），
      // 最后才是 private/ 的 403 —— 反过来会把"读不存在的包"演成"私有资料被拒"。
      if (!packNameOk(filePack)) return err(400, '行业包名称不合法');
      if (!KNOWN_PACKS[filePack]) {
        // 文案与 code 都与 app/server.py 同源（真引擎这里回 detail + code=pack_missing），
        // 逐字一致性由 tests/test_server_hardening.py 的桩/服务端对账用例钉住。
        return errc(404, '行业包不存在：' + filePack, 'pack_missing');
      }
      // 真实后端对 private/ 一律 403（安装包与导出都排除它，界面也不该能读全文），
      // 且**与包名无关**。桩原来只认 elevator：前面有用例建出第二个包之后，
      // 请求落到通用的 /api/packs/ 清单分支、拿回一份没有 size/text 的东西，
      // 于是这条断言量到空 body —— 红得像是实现的错（实测踩过）。
      // 判 private 与规范化都在下面做。第 8 轮把"URL 里含 private 字样"的子串正则
      // 换成了分段判（那会把 knowledge/private-notes.md 这种合法文件误拦成 403），
      // 第 9 轮对打 18 例又量出这段本身 11 例分歧：漏掉**最后一段**、大小写敏感、
      // 缺 rel 时缺省成一个真存在的文件、以及清单里没有的路径也回 200。
      var req2 = (s.split('rel=')[1] || '').split('&')[0];
      var relRaw = req2 ? decodeURIComponent(req2) : '';
      // 没给 rel 就是没给：真路由拿空串当"包目录本身"→ 404。缺省成 topics.md
      // 等于把「界面少传了参数」这种错演成「读到了一个真文件」。
      if (!relRaw) return err(404, '文件不存在：（没有给出 rel）');
      // 规范化要跟真实路由同步：server.py 先 resolve 再 relative_to(base)，
      // 反斜杠 / 重复斜杠 / ./ / ../ 都会被折掉。
      var withSlash = relRaw.split(BS).join('/');
      var segs = [], parts = withSlash.split('/');
      for (var pi = 0; pi < parts.length; pi++) {
        var seg = parts[pi];
        if (seg === '' || seg === '.') continue;
        if (seg === '..') { segs.pop(); continue; }
        segs.push(seg);
      }
      // 判据抄 app/server.py 的 _is_private_rel：**每一段**（含最后一段）小写后等于 private 就算。
      // 漏末段 → rel=private 在桩上 200、真引擎 403；不分大小写 → PRIVATE/x.md 同理。
      // 另一种方向的差异留在原地：越界路径桩会先折成 private/xxx 判 403，而引擎是 404 ——
      //   方向是"桩更严"，不会替实现掩盖任何东西，因此不改。
      var isPriv = false;
      for (var qi = 0; qi < segs.length; qi++) {
        if (segs[qi].toLowerCase() === 'private') isPriv = true;
      }
      if (isPriv) {
        return err(403, '私有资料不经界面浏览（packs/elevator/private 下的内容）。'
                        + '要查看或修改，请直接用编辑器打开本地文件。');
      }
      // 文件名按请求回显，size 取自详情那份 DET_FILES（一处定义两处用）。
      // 清单里没有的路径真引擎是 404（文件不存在），桩以前对任意 rel 都编一份 200
      // 内容 —— 「读到一个不存在的文件」这类错于是永远量不出来。
      var relQ = segs.join('/');
      var szRow = null;
      for (var ri = 0; ri < DET_FILES.length; ri++) { if (DET_FILES[ri].rel === relQ) szRow = DET_FILES[ri]; }
      if (!szRow) return err(404, '文件不存在：' + relQ);
      return mk({ rel: relQ, size: szRow.size,
                  text: relQ + ' 的内容（桩）— 家用电梯怎么挑？' });
    }
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
    // 建包 = 后台作业（P1-43）：只回 job_id，摘要在 /api/jobs/jobpg 的 result 里。
    // 真引擎在**花钱之前**用 claim_slug 占名（app/packgen.claim_slug），同名并发
    // 第二次直接 FileExistsError → 409「行业包正在创建中」。桩原来恒成功，
    // 于是"界面对并发提交做了什么"这件事全绿也量不出来（批次 10 复核指出）。
    if (s.indexOf('/api/packs/create') >= 0) {
      var pgBody = {};
      try { pgBody = JSON.parse((o && o.body) || '{}'); } catch (_) {}
      var ind = String(pgBody.industry || '');
      var indKey = pgSlug(ind);
      // 真实引擎的顺序是 pydantic 在前（422）再业务判断（400）：描述只打三个字时，
      // 行业名再怪也先回 422。桩原来两层都没建模，于是界面那条"422 的 detail 是人话"
      // 的路径在整个门禁里永远跑不到（服务端配套：app/server.py 的 _humanize_validation）。
      var dsc = String(pgBody.description || '');
      if (ind.length < 2) return errc(422, '行业名称太短了，要至少 2 个字', 'field_invalid');
      if (dsc.length < 4) return errc(422, '业务描述太短了，要至少 4 个字', 'field_invalid');
      // 纯符号名字：引擎在**花钱之前**就拒（pipeline.start_packgen → 400），
      // 压根不占位。桩原来会算出一个空串键并把 409 的理由写成「行业包正在创建中：」——
      // 那是桩自己造的一种"假忙碌"，还会让第二个纯符号名字看起来在排队（第 7 轮复核抓到）。
      if (!indKey) return err(400, '行业名称里没有任何可用作目录名的字符（纯符号起不了名），'
                                   + '请换成含中文、字母或数字的名称');
      pgSweepStale();                 // 先看有没有到点该归还的占位
      // 两句 409 是两件事（app/packgen.py 两条 raise 的原文）：目录已经在 = 已存在，
      // 只有还在创建中的那条才算"排队"。桩原来只有后一句。
      if (PG_CREATED[indKey]) return err(409, '行业包已存在：' + indKey);
      if (PG_CLAIMED.indexOf(indKey) >= 0) {
        calls.pgdup = (calls.pgdup || 0) + 1;
        return err(409, '行业包正在创建中：' + indKey);
      }
      PG_CLAIMED.push(indKey);
      PG_SEQ++;
      var pgId = 'jobpg' + PG_SEQ;          // 每次提交一个独立作业：真引擎就是按 job.id 记占位的
      PG_JOBS[pgId] = { slug: indKey, industry: ind, polls: 0, state: 'packing',
                        born: Date.now() };
      return mk({ job_id: pgId });
    }
    if (s.indexOf('/api/jobs/jobpg') >= 0 && s.indexOf('/cancel') >= 0) {
      var cid = pgJobId(s);
      if (!PG_JOBS[cid]) return err(404, ERR_JOB_GONE_CANCEL);
      if (PGCANCELFAIL) {
        calls.failpgcancel = (calls.failpgcancel || 0) + 1;
        return err(500, '引擎没有接受这次取消：作业正在写文件');
      }
      pgSweepStale();
      calls.cancel++;
      if (PG_JOBS[cid].state !== 'packing') return pgSnap(cid);   // 已结束：幂等，不改状态
      // 只归还**这条作业**占住的键（第 10 轮 P2：写死一个键会把别人的占位删掉，
      // 或把自己的留在集合里永久 409）。作业条目本身留着 —— 真引擎的终态作业还在
      // 注册表里（prune 只保留最近 200 条），轮询它照样有答案，不是 404。
      PG_CLAIMED = PG_CLAIMED.filter(function(x){ return x !== PG_JOBS[cid].slug; });
      PG_JOBS[cid] = { slug: '', industry: PG_JOBS[cid].industry,
                       polls: PG_JOBS[cid].polls, state: 'cancelled',
                       born: PG_JOBS[cid].born };
      return pgSnap(cid);
    }
    if (s.indexOf('/api/jobs/jobpg') >= 0) {
      var pid = pgJobId(s);
      pgSweepStale();
      var pj = PG_JOBS[pid];
      if (!pj) {
        // 未知建包作业：两句原文**按路由**发（server.py 的 job_status / job_cancel）。
        // 原来这里的注释写着"按路由发对应的那句"，代码却不分路由都发 GET 那句 ——
        // 而门禁的探针用的 id 不含 jobpg，压根进不到这一支，所以改错也没人知道
        // （第 16 轮复核 P1-3；本轮自己复量到：cancel 一条已回收的建包作业，
        // 界面看到的是"作业不存在或已随重启释放"，与它该看到的那句不同）。
        return s.indexOf('/cancel') >= 0 ? err(404, ERR_JOB_GONE_CANCEL)
                                        : err(404, ERR_JOB_GONE_GET);
      }
      if (pj.state !== 'packing') return pgSnap(pid);   // 已收工：同一份终态快照，幂等可轮询
      calls.pg++;             // 全局"建包被轮询了几次"的观测值（断言用它判"少传参数时不许开轮询"）
      pj.polls++;
      if (pj.polls <= 2) {
        // 在途的那一份也走 pgSnap：字段集与终态只差 result 一个键（引擎轮询
        // include_result=False）。原来这里另写一份字面量，缺 created_at 与 error，
        // 于是界面「已用 N 秒」对建包作业在门禁里从未被量到（第 16 轮复核 P2-4）。
        return pgSnap(pid);
      }
      PG_CLAIMED = PG_CLAIMED.filter(function(x){ return x !== pj.slug; });
      PG_CREATED[pj.slug] = 1;                       // 目录从此在那儿了：下一句 409 换措辞
      PG_JOBS[pid] = { slug: '', industry: pj.industry, polls: pj.polls, state: 'done',
                       born: pj.born };             // finally 归还
      return pgSnap(pid);
    }
    // 认不出来的作业 id 就是认不出来：真引擎 GET /api/jobs/{id} 回 404
    // 「作业不存在或已随重启释放」，cancel 未知 id 回 404「作业不存在」。
    // 原来这一路落到最后的兜底分支拿到 200 + 空对象，界面轮询因为 state 永远
    // undefined 而**死转**（真引擎会立刻收工报错）—— 桩比后端宽容，还顺手把
    // "作业消失之后界面做什么"这条路从门禁里抹掉了（第 11 轮复核 P2）。
    // ⚠ 位置有讲究：必须在**所有**具名作业分支（jobrun/jobkeep/jobph/job1/jobfail/
    //   jobpgN）之后，否则它会把真存在的作业一并 404 掉 —— 放错一次，八条断言同时红。
    if (s.indexOf('/api/jobs/') >= 0) {
      return s.indexOf('/cancel') >= 0 ? err(404, ERR_JOB_GONE_CANCEL)
                                       : err(404, ERR_JOB_GONE_GET);
    }
    if (s.indexOf('/api/packs/') >= 0) {
      // 详情必须**按请求的包名**回：原来无论问哪个包都回「电梯行业包」，
      // 于是「看着 A 包点了按钮、实际改的是 B 包」这类错误在桩上量不出来
      // （批次 10 复核抓到 settings.js 的 undraft 正是读错了对象）。
      var segDet = (s.split('/api/packs/')[1] || '');
      var detName = decodeURIComponent(segDet.split(/[/?#]/)[0] || 'elevator');
      // 详情与 /file 问同一本账（第 11 轮复核 P2：只有 /file 一侧把未知包判 404，
      // 详情照样 200 回一份十个文件的清单 —— 那本账又分成两页）。判据顺序照引擎：
      // _safe_name → Pack() 存在性。
      if (!packNameOk(detName)) return err(400, '行业包名称不合法');
      if (!KNOWN_PACKS[detName]) return errc(404, '行业包不存在：' + detName, 'pack_missing');
      var detDn = KNOWN_PACKS[detName];
      // 校对清单：真实后端按**盘上有没有 校对清单.md** 回（app/server.py 读那个文件，
      // 只有 packgen 建包时写过；「转正」也不删它）。原来这里恒回一句
      // '1. 核对参数 / 2. 核对禁用词' —— 既不是 markdown 形状（P3-50 的渲染器在桩上
      // 量不出任何事），也从不为空（「这个包没有清单」那一支界面代码从没被执行过）。
      // 形状与 app/packgen._checklist 逐段对齐：H1 / 说明 / 一批待勾项 / "## " 小节 /
      // 一条缩进续行。⚠ 这段在模板字符串里：不写反引号、换行只能写 \\n。
      var HAS_CL = { fitment: true };
      var CL_MD = '# 新行业包校对清单\\n\\n行业：全屋定制/装修（目录 packs/fitment/，**草稿状态**）\\n\\n'
        + '使用前请逐项核实：\\n\\n- [ ] 人造板甲醛释放量分级标准现行编号\\n\\n'
        + '## 红线核实\\n\\n- [ ] 绝对化用语不得出现在口播正文\\n\\n'
        + '## 从模板包复制来的骨架（**不含任何行业内容，需要你填实例**）\\n\\n'
        + '- [ ] knowledge/voice.md 的口语化范例与人设开场（三处 {{待补}}）\\n'
        + '      （只注进提示词的是「红线速查」那一节，细则是给 checker 和人看的）\\n';
      return mk({ display_name: detDn,
      description: detDn + '口播脚本包',
      draft: isDraft(detName), checklist: HAS_CL[detName] ? CL_MD : null,
      // ⚠ 2026-09-17：桩里的文件清单必须**覆盖全部角色**。桩只有 3 个文件时，
      // 「行业包面板覆盖知识 / 技能 / 合规 / 私有等所有角色」这条断言验的是
      // 桩的贫瘠，不是实现的正确 —— 属于空转。真实包（elevator）有 22 个文件，
      // 这里取覆盖 9 个角色的最小真形态。
      files: DET_FILES });
    }
    if (s.indexOf('/api/history') >= 0) return mk([]);
    return mk({});
  };
})();
`;
}

// ── 断言工具 ────────────────────────────────────────────────
const results = [];
// 崩溃时把**页面侧**的异常一起打出来：以前 evalIn 只说
// `Cannot read properties of undefined (reading 'rebind')`，根因（渲染层启动时抛的那一句）
// 全程没露过面，每次都得靠猜。断言与页面异常本来就在两个进程里，这里只是把它们一起报出来。
const pageErrs = [];
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

  const errs = pageErrs;
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

  // ── 状态色 token 解析辅助（规范 §7.2：断言比对 token 解析值，不抄 rgb 字面量）
  // 亮色 token 曾逐字锁进断言；要加深 --warn 达标对比度时，字面量断言就成了
  // 「改值先改断言」的负担。这里把"这颗胶囊吃的是不是 --warn"的判据换成：
  // 现场读页面里 var(--warn) 的**计算色**，再与元素实际颜色比对 —— token 一改，
  // 两侧一起变，断言守的是"吃这一档"而不是"这个具体的 rgb"。
  // light-dark() 由浏览器解析，读 computed color 自动拿到当前分支，不用手动拆。
  const resolvedToken = async (name) => evalIn(`var el = document.createElement('span');
    el.style.color = 'var(${name})'; document.body.appendChild(el);
    var c = getComputedStyle(el).color; el.remove(); return c;`);

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
    // 用 md-save（模型表单的「保存」）当锚点 —— 原先是 st-save，
    // 2026-09-17 那个按钮已删（高级配置并入模型表单的保存）。
    document.getElementById('md-save').addEventListener('click', function(){});
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
  check("会话列表渲染 6 条并分组", boot.sessions === 6 && boot.groups >= 4,
    `rows=${boot.sessions} groups=${boot.groups}`);
  check("请求带上了访问令牌", await evalIn("return window.__sawToken === true;"), "");

  // ── 1b) 首页（landing）：合成器就是 hero ──────────────────
  // 2026-09-20 参考 ZCode 重排。改前 hero 是五个居中块（深色图标砖 / 标题 / 副标题 /
  // 2×2 白卡 / 底注）飘在中上部，输入卡钉在底部，两者之间实测一大片空白 ——
  // 最该被看见的输入框离视觉中心最远。
  // 这一组量的是**结构关系**不是像素值，而且**两态都要量**：首页与对话态的差别
  // 只有 #view-chat 上那一个类，只量首页会漏掉「发送后没落回底部」这一整类回归
  // （胶囊留在原地 / 玻璃底挂回去 / 居中没收掉，都是看得见却没人守的）。
  // ── 1b) 规范 §2 刻度表：静态扫 styles.css，刻度外的值只许白名单那几处 ──
  // 为什么是「快照 + 白名单」而不是「一律禁止」：表外的值今天有十处，每一处都
  // 重新论证一遍不现实；但**新增**一处会立刻红，而**修好一处不删条目**也会红 ——
  // 于是名单只会被写短，不会像注释那样越写越长。
  // 注释在这里必须剥掉：规范允许 ≤2px 的光学补偿、理由写在注释里，如果扫描看得见
  // 注释，那"补一句注释"就成了绕过这条断言的后门。
  const cssNoCmt = (() => {
    const lines = fs.readFileSync(path.join(REPO_ROOT, "desktop", "renderer", "styles.css"), "utf8")
      .split(/\r?\n/);
    let inC = false;
    return lines.map(line => {
      let out = "";
      for (let i = 0; i < line.length; i++) {
        const two = line.slice(i, i + 2);
        if (inC) { if (two === "*/") { inC = false; i++; } out += "  "; }
        else if (two === "/*") { inC = true; out += "  "; i++; }
        else out += line[i];
      }
      return out;
    }).join("\n");
  })();
  const SPACE_RE = /^(gap|row-gap|column-gap|margin(-\w+)?|padding(-\w+)?)$/;
  const RADIUS_RE = /^border(-top|-bottom)?(-left|-right)?-radius$/;
  const LADDER_OF = {
    space: [0, 4, 8, 12, 16, 24, 32, 999],      // --s1..--s8（§2 间距行）
    radius: [0, 8, 12, 16, 999],                // --r-ctl / --r-card / --r-pop / --r-pill
  };
  const offLadder = [];
  for (const block of cssNoCmt.matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
    const sel = block[1].trim().replace(/\s+/g, " ");
    if (!sel || sel.startsWith("@") || sel.startsWith("--")) continue;
    // 名单按**第一个选择器**记，不按整串：给一族加一个成员是收口，不是新增破例，
    // 用整串当键会让每次收口都先把这条断言弄红一遍（实测踩过）。
    const key0 = sel.split(",")[0].trim();
    for (const decl of block[2].split(";")) {
      const d = /^\s*([a-z-]+)\s*:\s*([^]*)$/.exec(decl);
      if (!d) continue;
      const kind = SPACE_RE.test(d[1]) ? "space" : RADIUS_RE.test(d[1]) ? "radius" : null;
      if (!kind) continue;
      for (const n of d[2].matchAll(/(-?\d*\.?\d+)px/g)) {
        const v = Math.abs(parseFloat(n[1]));
        if (LADDER_OF[kind].includes(v) || v === 0.5) continue;   // 0.5 = 发丝线
        offLadder.push(`${key0} { ${d[1]}: ${d[2].trim()} }`);
      }
    }
  }
  const found = [...new Set(offLadder)].sort();
  const LADDER_ALLOW = [...new Set([
    // —— 光学补偿（§2 例外一，注释里写明了补的是什么）——
    ".linkbtn { margin: -2px 0 -2px var(--s2) }",        // 把 inline-block 的行盒压回 16
    ".linkbtn { padding: 2px var(--s2) }",               // 同上：可点区域 20，行盒仍是 16
    ".m-chip { padding: 1px var(--s2) }",                       // 小标记一族的墨底补偿
    ".nav-item kbd { padding: 1px var(--s1) }",          // 键帽的墨底补偿，与上面同一写法
    ".sess-search > kbd { padding: 1px var(--s1) }",     // 搜索框 Ctrl+K 同属键帽一族（同上）
    ".msg-assistant .avatar { margin-top: 2px }",        // 24 的头像盒对齐 15/1.6 首行
    ".res-tab { margin-bottom: -1px }",                  // 选中态的黑线压在容器分隔线上
    "#session-list { gap: 1px }",                        // 相邻涂底行之间的缝（§2 例外三）
    // —— 容器尺度，不是节奏 ——
    "select { padding: var(--s2) 28px var(--s2) var(--s3) }",   // 给自绘箭头留位
    "#left.folded ~ #right .right-head { padding-left: 56px }", // 折叠栏宽度 + 图标列
    ".msg-user { padding-left: 60px }",                        // 气泡让开左侧内容列
  ])].sort();
  const ladderExtra = found.filter(k => !LADDER_ALLOW.includes(k));
  const ladderStale = LADDER_ALLOW.filter(k => !found.includes(k));
  check("styles.css 刻度外的间距/圆角只有名单里那几处（新增必须先写明补的是什么）",
    ladderExtra.length === 0 && ladderStale.length === 0,
    `新增=${JSON.stringify(ladderExtra)} 已修好却没从名单里删=${JSON.stringify(ladderStale)}`);

  // ── 1c) 首页 landing 的几何 ────────────────────────────────
  const GREET_RE = /^(早上好|中午好|下午好|晚上好|夜深了)，今天想讲点什么？$/;
  const landing = await evalIn(`return (() => {
    const view = document.getElementById('view-chat');
    const card = document.getElementById('composer-body');
    const comp = document.getElementById('composer');
    const chips = Array.from(document.querySelectorAll('#empty-samples .sample-card'));
    const r = (n) => n.getBoundingClientRect();
    const vr = r(view);
    const one = (a) => Array.from(new Set(a));
    return {
      isLanding: view.classList.contains('is-landing'),
      emptyShown: !document.getElementById('empty').classList.contains('hidden'),
      chipsShown: !document.getElementById('empty-samples').classList.contains('hidden'),
      greet: (document.getElementById('empty-greet').textContent || '').trim(),
      // hero 只该剩「标识 + 一句话」两件事，副标题与底注是原来那五个居中块里的三个
      heroKids: document.getElementById('empty').children.length,
      extraLines: document.querySelectorAll('#empty .empty-sub, #empty .hint').length,
      markShown: getComputedStyle(document.querySelector('.empty-mark')).display !== 'none',
      // 深色图标砖（52px 渐变 + 阴影）不该回来：它是「实心深色」用在非主 CTA 上
      inkTile: !!document.querySelector('#empty .mono'),
      cardCenterOff: Math.round((r(card).top + r(card).height / 2) - (vr.top + vr.height / 2)),
      // 居中的**主体**是整列（问候语 + 输入卡 + 起手示例），不是输入卡单独一个。
      // auto 边距把这三块的外接框顶到正中，所以判据量这个框 —— 输入卡自己的中心
      // 偏离多少由「上面有多少 / 下面有多少」决定，它不是一个契约值。
      groupCenterOff: Math.round(((r(document.getElementById('chat-stream')).top
        + r(document.querySelector('.samples')).bottom) / 2) - (vr.top + vr.height / 2)),
      cardBottomGap: Math.round(vr.bottom - r(card).bottom),
      chipsBelowCard: chips.length > 0 && chips.every(c => r(c).top >= r(card).bottom - 1),
      chipRows: one(chips.map(c => Math.round(r(c).top))).length,
      chipHeights: one(chips.map(c => Math.round(r(c).height))),
      chipRowCenterOff: Math.round(
        (Math.min.apply(null, chips.map(c => r(c).left))
         + Math.max.apply(null, chips.map(c => r(c).right))) / 2 - (vr.left + vr.width / 2)),
      chipTransition: getComputedStyle(chips[0]).transitionProperty.split(/\\s*,\\s*/),
      // ── 胶囊一族与输入卡的两条边界（2026-09-21 用户报「字号、粗细、颜色、胶囊
      //    大小高度、间距都不统一」）─────────────────────────────
      // 卡内参数胶囊 / 模型胶囊逐项同档：高 / 字号 / 字重
      pillGeo: one(Array.from(document.querySelectorAll('#composer .select-btn'))
        .map(b => { const c = getComputedStyle(b);
          return Math.round(r(b).height) + "/" + c.fontSize + "/" + c.fontWeight; })),
      moreBox: (() => { const m = document.querySelector('.qp-more');
        return { w: Math.round(r(m).width), h: Math.round(r(m).height) }; })(),
      // 输入文字左沿 = textarea 盒左 + 它的左内边距；胶囊盒左沿必须落在同一条竖线上
      textLeft: (() => { const t = document.getElementById('topic'), c = getComputedStyle(t);
        return +(r(t).left + parseFloat(c.paddingLeft)).toFixed(1); })(),
      textRight: (() => { const t = document.getElementById('topic'), c = getComputedStyle(t);
        return +(r(t).right - parseFloat(c.paddingRight)).toFixed(1); })(),
      pillLeft: +(r(document.querySelector('#quick-params .select-btn')).left).toFixed(1),
      actRight: +(r(document.querySelector('#btn-generate')).right).toFixed(1),
      // 卡 → 起手示例：#composer 的下内边距与 .samples 的上内边距只能有一份
      chipsGap: Math.round(r(chips[0]).top - r(card).bottom),
      greetGap: Math.round(r(card).top - (document.querySelector('.empty').getBoundingClientRect().bottom)),
      // 首页没有「浮在滚动内容之上」的固定层语义 —— 输入区不该再挂玻璃底
      glass: getComputedStyle(comp).backdropFilter,
      // 「后台还有几条在跑」那一行在**首页必须不占位**：桩的左栏里就有一条
      // writing 记录，所以这一条量的是"有后台作业时的首页"，正是它最容易漏出来
      // 把 hero 挤歪的场合（实测漏出来时 chipsGap 16 → 44）。
      footText: document.getElementById('composer-gen-hint').textContent,
      footH: Math.round(document.getElementById('composer-gen-hint').parentNode
        .getBoundingClientRect().height),
      stopAllHidden: document.getElementById('btn-stop-all').classList.contains('hidden'),
      bgCount: (window.__ts.busyRecords ? window.__ts.busyRecords().length : -1),
    }; })()`);
  check("首页为 landing 态：问候语与起手胶囊同时在场",
    landing.isLanding && landing.emptyShown && landing.chipsShown, JSON.stringify(landing));
  // 前提要先钉住：这一屏**确实**有后台作业（否则"没占位"是空转出来的假绿）。
  check("首页：确有后台作业在跑（下一条断言的前提）", landing.bgCount >= 1,
    JSON.stringify({ bgCount: landing.bgCount }));
  check("首页：后台作业那一行不写进输入卡下方（留空且零高度、按钮收起）",
    landing.footText === "" && landing.footH === 0 && landing.stopAllHidden,
    JSON.stringify(landing));
  check("hero 只剩标识 + 一句问候（副标题、底注、深色图标砖都下线了）",
    landing.heroKids === 2 && landing.extraLines === 0
      && landing.markShown && !landing.inkTile, JSON.stringify(landing));
  check("问候语按时段落五档之一（不是写死的一句）",
    GREET_RE.test(landing.greet), landing.greet);
  // 居中的主体是**整列**（问候语 + 输入卡 + 起手示例），auto 边距把这三块的外接框
  // 顶到正中 —— 判据量那个框（≤2px 就是几何居中），而不是量输入卡自己的中心：
  // 卡心偏多少由「上面挂了多少、下面挂了多少」决定，改任何一块都会让它动，那不是一个契约。
  // 「不再钉在底部」这一半仍单独守：卡底到视口底必须留出成段的空白。
  check("首页整列几何居中，且输入卡不钉底（离视口底留成段空白）",
    Math.abs(landing.groupCenterOff) <= 2 && landing.cardBottomGap > 200,
    JSON.stringify({ groupOff: landing.groupCenterOff, cardOff: landing.cardCenterOff,
      bottomGap: landing.cardBottomGap }));
  check("起手胶囊在输入卡下方、排成一行、整行居中、高度同一档",
    landing.chipsBelowCard && landing.chipRows === 1
      && Math.abs(landing.chipRowCenterOff) <= 2 && landing.chipHeights.join() === "28",
    JSON.stringify(landing));
  // 胶囊一族在这一列里只许一种尺寸。规范 §2 的控件高度只有 28/32/40 三档，
  // 卡内参数胶囊原来是刻度外的 24，与卡下 28 的起手示例同列并存 —— 用户量的
  // 「胶囊大小高度不统一」就是这一条。字重一并量：结果卡段标签的 `.pill` 与胶囊
  // 变体 `.select-wrap.pill` 撞名时，600 会顺着继承落进这一排（`.select-btn` 写的是
  // `font: inherit`，挡不住权重）—— 规范 §2 字重表里控件那一档是 500。
  check("卡内胶囊与起手示例同档：高/字号/字重逐项相等，且落在 --h-sm 28 + 控件字重 500",
    landing.pillGeo.length === 1 && landing.pillGeo[0] === "28/13px/500"
      && landing.chipHeights.join() === "28"
      && landing.moreBox.h === 28 && landing.moreBox.w === 28,
    JSON.stringify({ pillGeo: landing.pillGeo, chipHeights: landing.chipHeights,
      moreBox: landing.moreBox }));
  // 一张卡里不许有两条左边界（与左栏「一个行内两个左边界」同一类缺陷）：
  // 工具条的左内边距必须等于 textarea 的左内边距，右端发送键同理。
  check("输入卡内只有一条左边界、一条右边界（胶囊与输入文字左沿 / 发送键与文字右沿对齐）",
    landing.pillLeft === landing.textLeft && landing.actRight === landing.textRight,
    JSON.stringify({ pillLeft: landing.pillLeft, textLeft: landing.textLeft,
      actRight: landing.actRight, textRight: landing.textRight }));
  // 竖向节奏：标识→问候 12、问候→卡 24、卡→示例 16。最后这一档曾被 `#composer`
  // 的下内边距与 `.samples` 的上内边距各算一份而叠成 32（比"问候→卡"还宽，
  // 附属物离主人的距离反而大于段落间的距离）。
  check("首页节奏三档：问候语→卡 24、卡→起手示例 16（下边距只算一份）",
    landing.greetGap === 24 && landing.chipsGap === 16,
    JSON.stringify({ greetGap: landing.greetGap, chipsGap: landing.chipsGap }));
  check("胶囊 hover 只用一种手法（过渡项只有一列，且就是底色，规范 §2·5.4）",
    landing.chipTransition.length === 1 && /^background(-color)?$/.test(landing.chipTransition[0]),
    JSON.stringify(landing.chipTransition));
  check("首页输入区不挂玻璃底（规范 §3：玻璃只给浮在滚动内容上的固定层）",
    landing.glass === "none", landing.glass);

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
    // 每一步的「标题 | 徽章文本」——P3-15 的 token 账要能在步骤行上看到
    usageBadges: Array.prototype.slice.call(
      document.querySelectorAll('.msg-assistant .steps .step'))
      .map(function (li) {
        var t = li.querySelector('.step-t'), n = li.querySelector('.step-note');
        return (t ? t.textContent : '') + '|' + (n ? n.textContent : '');
      }),
    thinkShown: !document.querySelector('#think-stream')?.classList.contains('hidden'),
    sess: (function () {
      var ids = Array.prototype.slice.call(
        document.querySelectorAll('#session-list .sess-item'))
        .map(function (r) { return r.dataset.id || ''; });
      var seen = {}, dup = [];
      ids.forEach(function (v) { if (seen[v]) dup.push(v); seen[v] = 1; });
      return { n: ids.length, dup: dup };
    })(),
    btnTitle: document.getElementById('btn-generate').title,
    topicCleared: document.getElementById('topic').value === '',
    hint: (() => { const h = document.getElementById('composer-gen-hint');
      return { text: h.textContent, display: getComputedStyle(h).display,
               h: Math.round(h.getBoundingClientRect().height) }; })(),
    btn: (() => { const b = document.getElementById('btn-generate');
      const bs = getComputedStyle(b);
      return { stopping: b.classList.contains('stopping'),
               img: bs.backgroundImage, bg: bs.backgroundColor }; })(),
    // 发送后必须**整块**落回对话态：landing 类收掉、胶囊隐藏、输入卡回到底部。
    // 只查 is-landing 不够 —— 胶囊的可见性走的是 setLanding 里另一条 classList，
    // 两处各写一半才是这个改动真正的风险点（改前三处 hide 各写各的）。
    // gap 的上界不是 #composer 的下内边距：生成中那行「参数已锁定」提示占在卡片
    // 下方（composer-foot），所以这里是「内边距 + 一行提示」的量级而不是内边距本身。
    // 判据要的是「贴回底部」，与首页那个 >200 的偏离量对照才成立 —— 单看这一条
    // 把阈值写死成任何一个具体值都是在猜实现，留 64 是给提示行换行留余量。
    leftLanding: (() => { const v = document.getElementById('view-chat');
      const cb = document.getElementById('composer-body');
      return { off: !v.classList.contains('is-landing'),
               chips: getComputedStyle(document.getElementById('empty-samples')).display,
               empty: document.getElementById('empty').classList.contains('hidden'),
               gap: Math.round(v.getBoundingClientRect().bottom - cb.getBoundingClientRect().bottom) }; })(),
    // ── 生成中状态行的位置与活体指示器（第三轮：边框环 →「太难看、闪」；
    //    SVG 描边弧线把一圈放到很慢 →「还是太快」；整枚一起明暗 →
    //    「读成卡住 / 闪」。这轮是三点起伏，两条硬要求来自上面三次退回，
    //    逐条根因写在 styles.css 的 .gs-live 那段注释里）
    // ⚠ 必须与上面同一个 evalIn 一起量：桩的 running 快照只覆盖前几次轮询，
    //   另起一次 evalIn 时后一次轮询已经把气泡换成结果，
    //   #gen-elapsed 早就不在了 —— 实测那样量到的是 elapsed=""，假红。
    // ⚠ 动效时长/延迟读**样式表里写下的值**而不是 getComputedStyle：无头 Chrome
    //   默认按 prefers-reduced-motion: reduce 报告，styles.css 里那条全局
    //   animation-duration 覆盖会把计算值压成近乎零，量到的永远是
    //   「无障碍降级后的结果」，不是设计值。
    //   ⚠ 本函数在模板字符串里，注释**不许出现反引号**（会提前闭合整个文件）。
    genStatus: (function(){
      var st = document.getElementById('gen-status');
      var think = document.getElementById('think-stream');
      var el = document.getElementById('gen-elapsed');
      var live = st.querySelector('.gs-live');
      var dots = live ? live.querySelectorAll('i') : [];
      var body = st.closest('.msg-body').getBoundingClientRect();
      var sr = st.getBoundingClientRect(), tr = think.getBoundingClientRect();
      var er = el.getBoundingClientRect();
      var d0 = dots.length ? getComputedStyle(dots[0]) : null;
      var cRule = null, iRule = null, d2 = null, d3 = null, kf = null;
      for (var i = 0; i < document.styleSheets.length; i++) {
        var rules = null;
        try { rules = document.styleSheets[i].cssRules; } catch (_) { continue; }
        for (var j = 0; j < (rules || []).length; j++) {
          var ru = rules[j], sel = ru.selectorText;
          if (sel === '.gs-live') cRule = ru;
          if (sel === '.gs-live i') iRule = ru;
          if (sel === '.gs-live i:nth-child(2)') d2 = ru;
          if (sel === '.gs-live i:nth-child(3)') d3 = ru;
          if (ru.type === 7 && ru.name === 'gs-live') kf = ru;
        }
        if (cRule && iRule && d2 && d3 && kf) break;
      }
      var frames = {};
      if (kf) for (var m = 0; m < kf.cssRules.length; m++) {
        var fr = kf.cssRules[m], op = parseFloat(fr.style.opacity);
        var ks = fr.keyText.split(",");
        for (var q = 0; q < ks.length; q++) frames[ks[q].trim()] = op;
      }
      var sec = function (v) { return v ? parseFloat(v) : null; };
      return {
        belowThink: Math.round(sr.top - tr.bottom),
        gapToRightEdge: Math.round(body.right - er.right),
        phase: document.getElementById('gen-phase').textContent,
        elapsed: el.textContent,
        // 形状：三颗点，4px，间距 4px（规范 §1 的 4 倍数刻度），吃 --text-2。
        dotCount: dots.length,
        dotW: d0 ? sec(d0.width) : null,
        dotH: d0 ? sec(d0.height) : null,
        dotColor: d0 ? d0.backgroundColor : "",
        dotGap: live ? sec(getComputedStyle(live).columnGap) : null,
        // 这一行里不许再有「环」：三种环的画法都被按观感退回过。
        noRing: !st.querySelector('.spinner, svg'),
        // 容器自己不许带动画（转 = 有方向 = 眼睛追得到，那正是第二轮的坑）
        liveAnim: cRule && cRule.style.animationName ? cRule.style.animationName : "none",
        dotDur: sec(iRule && iRule.style.animationDuration),
        delay2: sec(d2 && d2.style.animationDelay),
        delay3: sec(d3 && d3.style.animationDelay),
        // 关键帧必须改 opacity，而且**起止要全亮**：无障碍降级时界面停在
        // 100% 那一帧，那一帧若是暗的那头，降级用户看到的是三颗看不见的灰点。
        opStart: frames["0%"], opMid: frames["25%"], opEnd: frames["100%"],
      };
    })(),
  };`);
  check("发送后进入生成态（用户气泡 + 助手气泡）",
    running.busy && running.jobId === "job1" && running.userMsg, JSON.stringify(running));
  // 20260917 那轮留下的 P1-2：生成期间「左栏列表 + 聊天区」同屏两处状态，
  // 从来没有一条断言守过「同一条 id 在左栏只出现一次」。桩里本来就有一条
  // writing 记录，真作业收尾后前端还会把它并进列表 —— 并进与轮询各加一次
  // 就是两条一模一样的行，用户看到的是「我生成了两遍」。
  check("生成中：左栏会话列表没有重复 id",
    running.sess.dup.length === 0, JSON.stringify(running.sess));
  check("步骤时间线渲染出已完成步骤", running.steps >= 2, `steps=${running.steps}`);
  // P3-15：usage 记进作业步骤已经几轮了，界面始终没有消费方 —— 于是"这次思考
  // 吃掉了多少预算"仍然只能靠抓包看（主报告 R1 就是这件事看不见换来的）。
  // 两条一起钉：① 数字是 completion 减 reasoning 得来的（把 2400 当正文就是错账）；
  // ② 纯代码校验那一步什么都不显示 —— 少了这条，"永远输出 思考 0 token"也能全绿。
  check("生成中：模型步骤显示「思考 N token · 正文 M token」",
    running.usageBadges.some(s => s === "文案撰写|思考 1500 token · 正文 900 token") &&
    running.usageBadges.some(s => s === "选题策划|思考 380 token · 正文 230 token"),
    JSON.stringify(running.usageBadges));
  check("生成中：不调模型的步骤不显示 token 徽章",
    running.usageBadges.some(s => s === "校验·第 1 轮|"),
    JSON.stringify(running.usageBadges));
  check("生成中展示流式思考过程", running.thinkShown, "");

  // ── 3·b) 生成中状态行：位置与转圈（取法见上面 running.genStatus）
  const gs = running.genStatus;
  check("生成中：状态行在思考流下方、已用时贴着阶段名（不再顶到气泡右边缘）",
    gs.belowThink >= 0 && gs.belowThink <= 16
      && gs.gapToRightEdge > 40
      && gs.phase === "回炉改写中"
      && /^已用 [0-9]+ 秒$/.test(gs.elapsed),
    JSON.stringify(gs));
  check("活体指示器：三颗 4px 点 + 4px 间距 + 吃次级灰 + 这一行里没有环",
    gs.dotCount === 3 && gs.dotW === 4 && gs.dotH === 4 && gs.dotGap === 4
      && gs.dotColor === "rgb(110, 110, 115)" && gs.noRing,
    JSON.stringify(gs));
  // 这两条钉的是**三轮退回换来的东西**：
  // ① 不转（第二轮：只要还在转，眼睛就追得到方向，「太快」只能靠速度治，
  //    而调速度已经被退回过一次）；
  // ② 动必须分先后（第三轮：整枚一起明暗，暗下去那半秒整行没有任何东西在变，
  //    读起来像卡住 —— 所以三颗点各自的延迟必须严格错开，看到的才是一趟波）；
  // ③ 无障碍降级停住的那一帧（100%）必须全亮。
  check("活体指示器：不旋转、三颗点依次错开成一趟波、降级停住那帧全亮",
    gs.liveAnim === "none" && gs.dotDur >= 1.2
      && gs.delay2 > 0 && gs.delay3 > gs.delay2 && gs.delay3 < gs.dotDur
      && gs.opStart === 1 && gs.opEnd === 1 && gs.opMid > 0 && gs.opMid <= 0.4,
    JSON.stringify(gs));
  // 状态行留一张特写：断言守的是「几颗点、多大、错开多少」，守不住
  // 「13px 那行字旁边读起来静不静」—— 这一态已经按观感被退回来三轮，图得留下。
  // 和 composer-stopping 同一个道理：必须在这里拍，末尾截图区那边生成早结束了。
  const gsBox = await evalIn(`const r = document.getElementById('gen-status').getBoundingClientRect();
    return { x: Math.max(0, r.left - 10), y: Math.max(0, r.top - 8),
             width: r.width + 20, height: r.height + 16, scale: 3 };`);
  const gsShot = await cdp.send("Page.captureScreenshot", { format: "png", clip: gsBox });
  fs.writeFileSync(path.join(SHOT_DIR, "gen-status.png"),
    Buffer.from(gsShot.data, "base64"));
  // 「已用时」跨 tick 不抹空：轮询写一次，之后由本地计时器每秒续写。
  // 计时器读的是 state.job 那份快照 —— 它一旦缺 created_at，fmtElapsed 就返回
  // 空串，界面变成「显示一下、下一秒又被抹掉」。上面那次取值只覆盖了轮询刚
  // 回来的瞬间；这里跨过计时器的那一拍再取一次，漏挂 created_at 必然落空。
  // ⚠ 采样点必须落在「计时器已经走过一拍」与「桩落到 done」之间：
  //   计时器在发送后开始、逐秒一拍，done 在第三次轮询 —— 上面 400ms + 这里
  //   900ms 取到的是中间那段。跑长会读到 done 之后的 DOM（气泡已整块换掉），
  //   跑短则根本没跨过那一拍，两种都是假绿。
  await sleep(900);
  const afterTick = await evalIn(`return {
    elapsed: document.getElementById('gen-elapsed')?.textContent || '',
    jobCreatedAt: window.__ts.job?.created_at || '',
    stillRunning: window.__ts.busy === true,
  };`);
  check("生成中跨过计时器的一拍：已用时不会被抹空（state.job 带着 created_at）",
    afterTick.stillRunning && /^已用 [0-9]+ 秒$/.test(afterTick.elapsed)
      && Number(afterTick.elapsed.replace(/[^0-9]/g, "")) >= 8
      && !!afterTick.jobCreatedAt,
    JSON.stringify(afterTick));
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
  check("发送后整块落回对话态（landing 收掉、胶囊隐藏、输入卡回到底部）",
    running.leftLanding.off && running.leftLanding.empty
      && running.leftLanding.chips === "none" && running.leftLanding.gap <= 64,
    JSON.stringify(running.leftLanding));
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
    sess: (function () {
      var ids = Array.prototype.slice.call(
        document.querySelectorAll('#session-list .sess-item'))
        .map(function (r) { return r.dataset.id || ''; });
      var seen = {}, dup = [];
      ids.forEach(function (v) { if (seen[v]) dup.push(v); seen[v] = 1; });
      return { n: ids.length, dup: dup };
    })(),
  };`);
  // 收尾再量一次：本次作业最多只能给列表添**一条**行（多出来的一条
  // 就是「并进 + 轮询」双计的那条），且不许出现任何重复 id。
  check("完成后：左栏仍无重复 id，且本次作业最多并入一条",
    done.sess.dup.length === 0 && done.sess.n - running.sess.n <= 1,
    JSON.stringify({ running: running.sess, done: done.sess }));
  check("完成后渲染出结果", done.hasResult && !done.busy, JSON.stringify(done));
  // 非生成态：这一行必须留空**且不占高度**。它空着却占一条缝的话，
  // 卡片下方会凭空多出一块、看着像没对齐 —— 键盘提示并进 placeholder 之后，
  // 「空着不占位」就是这条规则的唯一可见后果，得有人守着。
  // 原来的「非生成态：状态行留空且不占高度」在这里已不成立：**后台真有作业时
  // 这行就该说话**（左栏被切走时没人看得见那几颗呼吸点）。所以按两态拆开守：
  //   首页 —— 不占位（上面那两条）；对话态 —— 说话、且只说一行、并给出动作。
  // "空着不许占位"这条规则本身没有放松：它搬到了首页那一屏去量，
  // 那里才是它真正会破的地方。
  const idleHint = await evalIn(`const h = document.getElementById('composer-gen-hint');
    const b = document.getElementById('btn-stop-all');
    return { text: h.textContent, h: Math.round(h.getBoundingClientRect().height),
             footH: Math.round(h.parentNode.getBoundingClientRect().height),
             btnShown: !b.classList.contains('hidden'), btnText: b.textContent,
             btnTitle: b.title,
             bg: (window.__ts.busyRecords ? window.__ts.busyRecords().length : -1) };`);
  check("对话态：后台作业时状态行说清楚有几条，并且只占一行",
    /后台进行/.test(idleHint.text) && idleHint.bg >= 1
    && idleHint.footH > 0 && idleHint.footH - idleHint.h <= 12,
    JSON.stringify(idleHint));
  check("对话态：「停止全部后台生成」按钮在场、带条数、title 里点出是哪几条",
    idleHint.btnShown && /停止全部后台生成（\d+）/.test(idleHint.btnText)
    && idleHint.btnTitle.indexOf('仍在后台进行') >= 0,
    JSON.stringify(idleHint));
  check("分段卡片 4 张", done.cards === 4, `cards=${done.cards}`);
  check("指标速览 4 项", done.metrics === 4, `metrics=${done.metrics}`);
  check("每段字数用后端统计值（12/85 而非前端重算）",
    done.quotaChips.some(t => t.includes("12/85")), JSON.stringify(done.quotaChips));
  // 产物分区由「一条长流里的折叠区」改为 tab：文案 / 分镜 / 人味分 / 字幕 / 合规 / 数据。
  // 「人味分」是 2026-09-23 加的（§2.7 ③）：正文里那条下划线要有个可跳的落点，
  // 否则「可定位」是句空话（挂了可点样式却没处理器 = 把假能力冒充真能力）。
  // ⚠ 它排在「字幕」之前：文案 → 分镜 → 人味分 是"读一遍稿子"的顺序，
  //   字幕/合规/数据都是导出与核对用的。
  check("产物分区改为 6 个 tab（文案/分镜/人味分/字幕/合规/数据）",
    done.tabs.join(",") === "文案,分镜,人味分,字幕,合规,数据", JSON.stringify(done.tabs));
  check("默认停在「文案」tab（主产物不被分镜挤下去）",
    (await evalIn(`return document.querySelector('.res-tab.on .rt-t').textContent;`)) === "文案",
    await evalIn(`return document.querySelector('.res-tab.on .rt-t').textContent;`));
  check("切换 tab 后对应的产物可见",
    (await evalIn(`document.querySelector('[data-tab="subs"].res-tab').click();
      return !document.querySelector('.res-pane[data-tab="subs"]').classList.contains('hidden')
        && document.querySelector('.res-pane[data-tab="script"]').classList.contains('hidden');`)) === true,
    "字幕 tab");
  // 结果出来后线程自动滚到底，页签条曾被整条推出滚动容器（实测它的 top 为负），
  // 用户以为产物只有一屏口播文案、不知道还有分镜/字幕/合规/数据。
  // ⚠ 桩里产物短，两次尝试都假绿过：直接 scrollTop 被钳回 0；用 height 压容器又被
  //    `flex: 1` 忽略。这里用 max-height（flex 项会认）+ 给产物区垫高，
  //    造出"产物比视口长、且已滚到底"的真状态，量完必须还原（后面还有断言在跑）。
  const tabGeo = await evalIn(`return (() => {
    const s = document.getElementById('chat-stream');
    const wrap = document.querySelector('.res-tabs');
    const pane = document.querySelector('.res-pane.on') || wrap.parentElement;
    const prevMax = s.style.maxHeight, prevH = pane.style.height, prevScroll = s.scrollTop;
    try {
      s.style.maxHeight = '160px';
      pane.style.height = '1200px';               // 产物比容器长得多 → 滚到底时页签条在上方
      void s.offsetHeight;
      s.scrollTop = 999999;
      const sr = s.getBoundingClientRect();
      const tr = wrap.getBoundingClientRect();
      return { sticky: getComputedStyle(wrap).position,
               scrollable: s.scrollHeight > s.clientHeight + 40,
               atBottom: s.scrollHeight - s.scrollTop - s.clientHeight < 4,
               streamTop: Math.round(sr.top), streamBottom: Math.round(sr.bottom),
               top: Math.round(tr.top), bottom: Math.round(tr.bottom) };
    } finally {
      s.style.maxHeight = prevMax; pane.style.height = prevH; s.scrollTop = prevScroll;
    }
  })();`);
  check("页签条声明为 sticky（吸顶的前提）",
    tabGeo.sticky === "sticky", tabGeo.sticky);
  check("产物比容器长且滚到底时，页签条被吸在容器上沿（四档页签可点）",
    tabGeo.scrollable && tabGeo.atBottom
      // 吸附位 = 容器上沿 + 容器内边距（实测 48 + 24 = 72），所以只能判"贴在上沿一带"，
      // 判"贴齐边框"是错的（我第一版就这么写死，红得很冤枉）。
      && tabGeo.top >= tabGeo.streamTop - 1 && tabGeo.bottom <= tabGeo.streamBottom + 1
      && tabGeo.top - tabGeo.streamTop <= 40,
    JSON.stringify(tabGeo));
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
  // 「建议胶囊」一族只许一种画法：首页卡下的 `.sample-card` 与结果区的 `.fu-chip`
  // 是同一个东西（点了只往输入区填一句话），曾经一个是 28/13px/正文色/--e1 描边环、
  // 另一个是 24/11px/次级灰/0.5px 实描边。这里比**计算值**而不是盒子 ——
  // 断言跑在对话态，起手示例此刻被 `setLanding` 收掉了，没有盒子可量。
  const chipTwin = await evalIn(`return (function(){
    var a = document.querySelector('.fu-chip'), b = document.querySelector('.sample-card');
    if (!a || !b) return { missing: true };
    var props = ['fontSize','fontWeight','color','backgroundColor','borderTopLeftRadius',
                 'paddingTop','paddingRight','paddingBottom','paddingLeft','lineHeight',
                 'boxShadow','whiteSpace','transitionProperty'];
    var ca = getComputedStyle(a), cb = getComputedStyle(b), diff = [];
    props.forEach(function(p){ if (ca[p] !== cb[p]) diff.push(p + ': ' + ca[p] + ' vs ' + cb[p]); });
    return { diff: diff, h: Math.round(a.getBoundingClientRect().height) };
  })()`);
  check("结果区建议胶囊与首页起手胶囊逐项同型（一族只有一种画法）",
    !chipTwin.missing && chipTwin.diff.length === 0 && chipTwin.h === 28,
    JSON.stringify(chipTwin));
  // 「有底色小标记」一族（规范 §2 + styles.css 末尾的收口那条）：八个类此刻在页面上
  // 存在几个就比几个，字号 / 字重 / 内边距 / 圆角必须逐项相等。
  // 这一族原来分五种画法，其中两份 padding 与 radius 是被更靠后的同特异性规则
  // 静默盖掉的**死声明** —— 只看 CSS 源码看不出来，只能量计算值。
  const badgeFam = await evalIn(`return (function(){
    var sels = ['.m-chip', '.acc-badge', '.rt-b', '.kb-group-c', '.badge',
                '.tag-warn', '.tag-default', '.rh-state'];
    var seen = {}, who = {};
    sels.forEach(function(s){
      var el = document.querySelector(s); if (!el) return;
      var c = getComputedStyle(el);
      var k = [c.fontSize, c.fontWeight, c.paddingTop, c.paddingRight,
               c.paddingBottom, c.paddingLeft, c.borderTopLeftRadius].join('/');
      seen[k] = (seen[k] || 0) + 1;
      (who[k] = who[k] || []).push(s);
    });
    var ks = Object.keys(seen);
    return { n: ks.length, kinds: ks.map(function(k){ return { v: k, 成员: who[k] }; }) };
  })()`);
  check("有底色小标记一族八个类字号/字重/内边距/圆角完全同档",
    badgeFam.n === 1, JSON.stringify(badgeFam));

  // ── 弹层里的「一行」：参数下拉 vs 分组「…」菜单（用户报「删除的弹层不统一」）
  // 改前实测同屏并排：行高 31 vs 28、内边距 8 全边 vs 0 8、字号 11 vs 13、
  // 过渡 all vs 四项。容器那层本来就一样，差的全在行上 —— 所以两态都要打开量。
  const closeAllMenus = closeMenus + `
    document.querySelectorAll('.grp-menu:not(.hidden)').forEach(function(m){ m.classList.add('hidden'); });
    document.querySelectorAll('.grp-more[aria-expanded]').forEach(function(b){ b.setAttribute('aria-expanded','false'); });`;
  await evalIn(closeAllMenus + `document.body.click(); return true;`);
  await sleep(200);
  await evalIn(`document.querySelector('#quick-params .select-btn').click(); return true;`);
  await sleep(320);
  const ROW_METRICS = `var o = document.querySelector(SELECTOR); if (!o) return { missing: true };
      var c = getComputedStyle(o), r = o.getBoundingClientRect(), m = o.parentElement;
      var mc = getComputedStyle(m);
      return { h: Math.round(r.height), fs: c.fontSize, lh: c.lineHeight, pad: c.padding,
        radius: c.borderTopLeftRadius, trans: c.transitionProperty, bg: c.backgroundColor,
        menuPad: mc.padding, menuRadius: mc.borderTopLeftRadius, menuBg: mc.backgroundColor };`;
  // 取**未选中**的那一行比：`.select-opt.on` 会换底色与字重，拿它比会误报成"不同型"。
  const menuRowA = await evalIn(`return (function(){ ${ROW_METRICS.replace("SELECTOR", "' .select-menu:not(.hidden) .select-opt:not(.on)'")} })()`);
  await evalIn(closeAllMenus + `document.body.click(); return true;`);
  await sleep(200);
  await evalIn(`document.querySelector('.grp-more').click(); return true;`);
  await sleep(320);
  const menuRowB = await evalIn(`return (function(){ ${ROW_METRICS.replace("SELECTOR", "'.grp-menu:not(.hidden) .grp-menu-item'")} })()`);
  await evalIn(closeAllMenus + `document.body.click(); return true;`);
  await sleep(200);
  const rowDiff = Object.keys(menuRowA || {}).filter(k =>
    k !== "missing" && JSON.stringify(menuRowA[k]) !== JSON.stringify((menuRowB || {})[k]));
  // 同心判据（不写死数字）：容器圆角 = 行圆角 + 容器内边距。
  // 外圈比内圈更圆时四角"包不住"行；单一项的菜单还会被画成一颗胶囊。
  const concentric = (row) => {
    const num = (v) => parseFloat(String(v));
    return Math.abs(num(row.menuRadius) - (num(row.radius) + num(row.menuPad))) < 0.01;
  };
  check("两种弹层里的「一行」逐项同型（行高/字号/行高值/内边距/圆角/过渡 + 容器内边距与圆角）",
    !menuRowA.missing && !menuRowB.missing && rowDiff.length === 0
      && menuRowA.h === 28 && menuRowA.fs === "13px",
    `差异=${JSON.stringify(rowDiff)} 参数下拉=${JSON.stringify(menuRowA)} 分组菜单=${JSON.stringify(menuRowB)}`);
  check("弹层圆角与里面的行同心（容器圆角 = 行圆角 + 容器内边距），两类弹层都成立",
    concentric(menuRowA) && concentric(menuRowB),
    `下拉=${menuRowA.menuRadius}/${menuRowA.radius}+${menuRowA.menuPad} 分组=${menuRowB.menuRadius}/${menuRowB.radius}+${menuRowB.menuPad}`);

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
  check("设置页铺满窗口，panes=4 / navs=3（packgen 不占导航位；kb / skills 已并入行业包）",
    stg.open && stg.covers && stg.panes === 4 && stg.navs === 3
      && stg.panes === stg.navs + 1
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

  // packgen 结果页：产物摘要 + 双出口。从 packinfo 入口走完整生成流程，
  // 断言 ①结果页可见且摘要已渲染（含细分/受众/选题） ②双出口齐备
  // ③「返回」回来源面板（与「取消」同语义，不强制跳去工作台）。
  await evalIn(`window.__ts.setPane('packinfo'); return true;`);
  await sleep(200);
  await evalIn(`document.getElementById('pi-newpack').click(); return true;`);
  await sleep(200);

  // ── 建包走后台作业（P1-43）：生成中看得见进度、取消是真取消、跑完才出结果页 ──
  await evalIn(`document.getElementById('pg-industry').value = '全屋定制/装修';
    document.getElementById('pg-desc').value = '全屋定制家居品牌，面向新房装修业主获客';
    document.getElementById('pg-run').click(); return true;`);
  await sleep(1000);
  const pgRun = await evalIn(`return {
    btn: document.getElementById('pg-run').textContent.trim(),
    disabled: document.getElementById('pg-run').disabled,
    closeDisabled: document.getElementById('pg-close').disabled,
    working: !document.getElementById('pg-working').classList.contains('hidden'),
    resultHidden: document.getElementById('pg-result').classList.contains('hidden') };`);
  check("建包生成中：按钮报「已用 N 秒」、结果页在作业结束前不出现",
    /生成中 · [0-9]+s/.test(pgRun.btn) && pgRun.disabled && pgRun.resultHidden,
    JSON.stringify(pgRun));
  check("建包生成中「取消」可用（同步长请求时代它被禁用以躲 P3-48 的竞态，作业化后理由消失）",
    pgRun.closeDisabled === false && pgRun.working, JSON.stringify(pgRun));
  await sleep(700);
  const pgHint = await evalIn(`return document.getElementById('pg-working').textContent.trim();`);
  check("生成中的说明行搬的是作业上的真实进度（思考字数 + 接口重试），不是一行写死的文案",
    pgHint.indexOf('已思考 512 字') >= 0 && pgHint.indexOf('接口自动重试') >= 0, pgHint);
  const cancelBefore = await evalIn(`return window.__calls.cancel || 0;`);
  await evalIn(`document.getElementById('pg-close').click(); return true;`);
  await sleep(500);
  const pgCancel = await evalIn(`return {
    resultHidden: document.getElementById('pg-result').classList.contains('hidden'),
    btn: document.getElementById('pg-run').textContent.trim() };`);
  pgCancel.n = (await evalIn(`return window.__calls.cancel || 0;`)) - cancelBefore;
  check("点「取消」真的发出 cancel 请求，取消后不显示结果页、按钮恢复可点",
    pgCancel.n === 1 && pgCancel.resultHidden && !/生成中/.test(pgCancel.btn),
    JSON.stringify(pgCancel));

  await evalIn(`document.getElementById('pg-run').click(); return true;`);
  await sleep(3200);          // 桩里第 3 拍才 done（前 2 拍是 packing）
  const pgDone = await evalIn(`return {
    resultVisible: !document.getElementById('pg-result').classList.contains('hidden'),
    summaryText: document.getElementById('pg-summary').textContent.trim(),
    hasBack: !!document.getElementById('pg-back'),
    hasDone: !!document.getElementById('pg-done'),
    checklistInDetails: !!document.getElementById('pg-more')
      && document.getElementById('pg-more').querySelector('#pg-checklist') !== null };`);
  check("packgen 生成成功：结果页展示产物摘要（细分/受众/人设/选题）",
    pgDone.resultVisible && pgDone.summaryText.indexOf('全屋定制/装修') >= 0
      && pgDone.summaryText.indexOf('板材环保') >= 0 && pgDone.summaryText.indexOf('选题') >= 0,
    JSON.stringify(pgDone));
  check("packgen 结果页双出口齐备：返回（回来源）+ 完成（去工作台），清单在折叠块内",
    pgDone.hasBack && pgDone.hasDone && pgDone.checklistInDetails,
    JSON.stringify(pgDone));
  // P3-50：清单是这次生成唯一需要人看的东西，原来它是一份 markdown 原文塞进 <pre>。
  const pgCl = await evalIn(`return (function(){
    var box = document.getElementById('pg-checklist');
    var rows = [].slice.call(box.querySelectorAll('.pg-summary-row'));
    var texts = rows.map(function(r){ return r.textContent; });
    return { rows: rows.length,
      rawMarks: texts.filter(function(t){ return t.indexOf('- [') === 0
        || t.indexOf('##') === 0 || t.trim() === '---'; }).length,
      boxes: rows.filter(function(r){ return r.children[0].textContent === '□'; }).length,
      first: texts.length ? texts[0].slice(0, 40) : '',
      tag: box.tagName.toLowerCase() };
  })()`);
  check("校对清单按行画成可勾的条目，不是一面 markdown 文本墙（P3-50）",
    pgCl.rows > 3 && pgCl.rawMarks === 0 && pgCl.boxes > 0 && pgCl.tag === 'div',
    JSON.stringify(pgCl));
  await evalIn(`document.getElementById('pg-back').click(); return true;`);
  await sleep(200);
  const afterPgBack = await evalIn(`return [...document.getElementById('settings-screen').querySelectorAll('.stg-pane')]
    .find(p => !p.classList.contains('hidden'))?.id;`);
  check("packgen 结果页「返回」回到来源面板（packinfo，与「取消」一致）",
    afterPgBack === 'pane-packinfo', afterPgBack);

  // 「完成」原来**永远**跳工作台，与「返回 / 取消」的去向不一致（P2-47）：
  // 从行业包面板进来的人，紧接着要看的就是新包的校对清单与文件，
  // 却被系统替他决定了"建完就走"。现在三个出口共用同一个来源判断。
  await evalIn(`document.getElementById('pi-newpack').click(); return true;`);
  await sleep(200);
  await evalIn(`document.getElementById('pg-run').click(); return true;`);
  await sleep(3200);          // 桩里第 3 拍才 done
  await evalIn(`document.getElementById('pg-done').click(); return true;`);
  await sleep(500);
  const afterPgDone = await evalIn(`return [...document.getElementById('settings-screen').querySelectorAll('.stg-pane')]
    .find(p => !p.classList.contains('hidden'))?.id;`);
  check("packgen「完成」回到来源面板（从 packinfo 进来的，别再扔回工作台）",
    afterPgDone === 'pane-packinfo', afterPgDone);

  // 花钱之前就拒的那两类输入，界面要说什么、按钮要回到什么状态。
  // ① 纯符号行业名 → 引擎 400（不占位、不起作业）；② 描述太短 → pydantic 422，
  // detail 必须是中文人话（修前会露出 String should have at least 4 characters）。
  // 两条都额外钉"一次轮询都没发"：400/422 之后还去轮询就是凭空造了个不存在的作业。
  await evalIn(`window.__ts.setPane('packinfo'); return true;`);
  await sleep(200);
  await evalIn(`document.getElementById('pi-newpack').click(); return true;`);
  await sleep(200);
  const pollsBefore = await evalIn(`return (window.__calls || {}).pg || 0;`);
  await evalIn(`document.getElementById('pg-industry').value = '？？？';
    document.getElementById('pg-desc').value = '纯符号名字，起不出目录名';
    document.getElementById('pg-run').click(); return true;`);
  await sleep(900);
  const pollsAfter = await evalIn(`return (window.__calls || {}).pg || 0;`);
  const p400 = await evalIn(`return {
    errShown: !document.getElementById('pg-error').classList.contains('hidden'),
    err: document.getElementById('pg-error').textContent,
    btnDisabled: document.getElementById('pg-run').disabled,
    formStill: !document.getElementById('pg-form').classList.contains('hidden'),
    resultShown: !document.getElementById('pg-result').classList.contains('hidden'),
    industry: document.getElementById('pg-industry').value };`);
  check("纯符号行业名：400 的原因常驻说清、按钮恢复、表单保留，且不起作业不轮询",
        p400.errShown && /可用作目录名/.test(p400.err) && p400.btnDisabled === false
          && p400.formStill && p400.resultShown === false && p400.industry === '？？？'
          && pollsAfter === pollsBefore,
        JSON.stringify(Object.assign({ polls: pollsAfter - pollsBefore }, p400)));
  await evalIn(`document.getElementById('pg-industry').value = '猫咖探针';
    document.getElementById('pg-desc').value = '小店';
    document.getElementById('pg-run').click(); return true;`);
  await sleep(900);
  const p422 = await evalIn(`return {
    err: document.getElementById('pg-error').textContent,
    shown: !document.getElementById('pg-error').classList.contains('hidden'),
    btnDisabled: document.getElementById('pg-run').disabled,
    resultShown: !document.getElementById('pg-result').classList.contains('hidden'),
    polls: (window.__calls || {}).pg || 0 };`);
  check("业务描述太短：422 的 detail 是中文人话而不是 pydantic 英文，且不起作业",
        p422.shown && p422.err.indexOf('业务描述太短了，要至少 4 个字') >= 0
          && p422.err.indexOf('String should have') < 0 && p422.btnDisabled === false
          && p422.resultShown === false && p422.polls === pollsAfter,
        JSON.stringify(p422));
  await evalIn(`document.getElementById('pg-close').click(); return true;`);
  // 收尾：把面板切回工作台，后面的断言都在量 .stg-main / pane-gen 的几何
  await evalIn(`const n = document.querySelector('.stg-nav-item[data-pane="gen"]');
    if (n) n.click(); return true;`);
  await sleep(200);

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
  const pi = await evalIn(`return { groups: document.querySelectorAll('#pi-groups .kb-group').length,
    items: document.querySelectorAll('#pi-groups .kb-item').length,
    title: document.getElementById('pi-title').textContent,
    hidden: document.getElementById('pane-packinfo').classList.contains('hidden') };`);
  // 2026-09-17 重组后：行业包面板右列是 **按角色分组的文件列表**（不再叫
  // 「文件清单」，也不再是单一扁平表）。ROLE_GROUP_ORDER 共 9 个角色；
  // 桩里的包给的是覆盖全部 9 组 / 10 个文件的最小真形态，所以这里按
  // 「≥8 组 / ≥9 条」判 —— 留一格的余量，不至于以后加一个角色就挂。
  // ⚠ 桩只给 3 个文件时这条会空转（只能验出 3 组）：桩数据见 stubScript()
  // 里 /api/packs/ 那段，改桩之前先想清楚它是不是还代表真实包。
  check("行业包详情进入即按角色渲染文件分组（≥8 组 / ≥9 条）",
    !pi.hidden && pi.groups >= 8 && pi.items >= 9,
    JSON.stringify(pi));

  // ⭐ 2026-09-17 新增断言：原「知识 / 技能」两个面板的内容（skill.yaml /
  // knowledge/、rules/、patterns/、compliance/、private/）现在必须能在
  // 行业包面板里看到 —— 这是把三面板并到一面板的核心证据；若 `.kb-item`
  // 仍只显示某种角色文件（如只显示私有资料），那说明合并没成功。
  const piAllRoles = await evalIn(`return (function(){
    var roles = new Set();
    document.querySelectorAll('#pi-groups .kb-item').forEach(function(n){
      var r = n.dataset.role; if (r) roles.add(r);
    });
    return { roles: [...roles].sort(), n: roles.size };
  })()`);
  check("行业包面板覆盖知识 / 技能 / 合规 / 私有等所有角色（不再需要单独面板）",
    piAllRoles.n >= 6
      && piAllRoles.roles.indexOf("knowledge") >= 0
      && piAllRoles.roles.indexOf("skill") >= 0
      && piAllRoles.roles.indexOf("compliance") >= 0
      && piAllRoles.roles.indexOf("private") >= 0,
    JSON.stringify(piAllRoles));

  // 行业包面板是三列（中列包列表 + 右列包详情）。
  // 「至少有一条包」 → 中列有 `.pl-item` + 默认 `.sel` 那条就是当前正在查看的；
  // 右列有 `#pi-groups .kb-item`（按角色分组的文件）。两者宽度与间距符合骨架口径。
  const pi3 = await evalIn(`var list = document.querySelector('#pi-list');
    var items = list ? list.querySelectorAll('.pl-item') : [];
    var on = [], sel = [];
    items.forEach(function (n) {
      if (n.classList.contains('on')) on.push(n.dataset.id);
      if (n.classList.contains('sel')) sel.push(n.dataset.id);
    });
    var detail = document.querySelector('#pane-packinfo .pane-detail');
    var lr = list ? list.getBoundingClientRect() : null;
    var dr = detail ? detail.getBoundingClientRect() : null;
    return { items: items.length, on: on, sel: sel,
             listW: lr ? Math.round(lr.width) : 0,
             detailW: dr ? Math.round(dr.width) : 0,
             sideBySide: (lr && dr) ? Math.round(dr.left - lr.right) : -1 };`);
  check("行业包面板是中列包列表 + 右列详情（三列骨架生效）",
    pi3.items >= 1
      && pi3.listW === 258 && pi3.detailW > 600
      && pi3.sideBySide >= 0 && pi3.sideBySide < 50
      && pi3.sel.length === 1,
    JSON.stringify(pi3));

  // 行业包面板的文件可点击预览：点 `.kb-item` → #pi-file-view 加载内容。
  // 2026-09-17 重组后共用同一份 `showPackFile(name, rel, row)` —— 之前还能按面板
  // 走 pfx="kb" / "skills" / "pi-file" 的三套实例；现在只剩一份。
  // 为什么是 .kb-item 而不是 .kb-group 行：每张卡仍是 `<button class="kb-item">`，
  // `dataset.role` 是稳定标识，不靠 firstChild 文本（首子是图标节点），
  // 文件名放进 `.kb-name` 是断言要查的元素。
  //
  // 桩引擎 vs 真引擎：verify 跑的是 stubtoken 的桩引擎（fitment 有
  // 3 个文件但 api.packFile 对它返错 / 短路内容）；probe 跑的是真引擎
  // （elevator 22 个文件，knowledge/topics.md 能正常 3937 字符）。
  // 桩数据飘忽，内容长度不稳定 —— **不查 bodyLen/具体字符**，
  // 只查「click 之后 title 变成那行的 rel + 不再是初始占位 + row 有 .on」。
  // 等到行真的渲染出来（renderPackGroups 完成后再点）。1500ms 兜底超时。
  const piReady = await evalIn(`return new Promise(function(res){
    var deadline = Date.now() + 1500;
    function poll(){
        var rows = document.querySelectorAll('#pi-groups .kb-item');
        if (rows.length > 0) return res(true);
        if (Date.now() > deadline) return res(false);
        setTimeout(poll, 50);
      }
      poll();
    });`);
  // 一气呵成：找目标 → 记下 rel 和点击前的 title → 点 → 等 title 不再是占位 →
  // 返回 {期望的 rel, 实际 title/body} 让断言用「期望 vs 实际」对得上。
  const piLoaded = await evalIn(`return new Promise(function(res){
    var rows = document.querySelectorAll('#pi-groups .kb-item');
    var target = null;
    rows.forEach(function(r){
      var nameEl = r.querySelector('.kb-name');
      var rel = nameEl ? nameEl.textContent : '';
      if (!target && rel && rel !== 'pack.yaml' && rel !== '校对清单.md') target = r;
    });
    if (!target) return res({ err: 'no target row' });
    var expectedRel = target.querySelector('.kb-name').textContent;
    var titleBefore = document.getElementById('pi-file-title').textContent;
    var bodyBefore = document.getElementById('pi-file-body').textContent;
    target.click();
    var deadline = Date.now() + 1500;
    function poll(){
        var body = document.getElementById('pi-file-body').textContent;
        var title = document.getElementById('pi-file-title').textContent;
        if (title && title !== '未选择文件'
            && body && body !== '载入中…'
            && body !== '从上方文件分组选择一个文件查看内容。') {
          return res({
            expectedRel: expectedRel,
            titleBefore: titleBefore,
            bodyBefore: bodyBefore,
            title: title,
            size: document.getElementById('pi-file-size').textContent,
            bodyLen: body.length,
          });
        }
        if (Date.now() > deadline) return res({
          expectedRel: expectedRel,
          titleBefore: titleBefore,
          bodyBefore: bodyBefore,
          title: title,
          size: document.getElementById('pi-file-size').textContent,
          bodyLen: body.length,
        });
        setTimeout(poll, 50);
      }
      poll();
    });`);
  check("点行业包文件分组里的 .kb-item，#pi-file-view 加载 yaml / md 内容",
    !!piLoaded && !piLoaded.err
      && piLoaded.title === piLoaded.expectedRel
      && piLoaded.title !== piLoaded.titleBefore
      && piLoaded.bodyLen !== piLoaded.bodyBefore.length
      && piLoaded.size.length > 0,
    JSON.stringify(piLoaded));

  // private/ 文件卡：后端现在对它回 **403**（安装包排除 private/，
  // 界面这个口子也必须关）。这里验两件事：整条路走得通（不崩、有内容），
  // 并且说的是「不经界面浏览」而不是「读取失败」—— 后者会把人引去查没坏的东西。
  const privFile = await evalIn(`return (function(){
    var rows = Array.prototype.slice.call(document.querySelectorAll('#pi-groups .kb-item'));
    var target = null;
    rows.forEach(function(r){
      var n = r.querySelector('.kb-name');
      if (!target && n && /private/i.test(n.textContent)) target = r;
    });
    if (!target) return { err: 'no private row' };
    target.click();
    return { clicked: true, rel: target.querySelector('.kb-name').textContent };
  })()`);
  await sleep(900);
  const privBody = await evalIn(`return {
    body: document.getElementById('pi-file-body').textContent,
    size: document.getElementById('pi-file-size').textContent,
  };`);
  check("点 private/ 文件卡：给的是「不经界面浏览」的说明，不是「读取失败」",
    privFile.clicked === true && /不经界面浏览/.test(privBody.body)
    && !/读取失败/.test(privBody.body) && privBody.size === "不经界面浏览",
    JSON.stringify({ privFile, privBody }));

  // 可写包目录要摊出来：打包版里包住在 %APPDATA%，安装目录那份会被覆盖安装重写。
  // 用户自建包 / 手改的词表去哪儿改，界面原来一个字都不说。
  // 只许有**一个**节点：这行是每次刷新包列表时重画的，重复叠加就是漏了清理。
  const packsDir = await evalIn(`return (function(){
    var n = document.querySelectorAll('#pi-packs-dir');
    var first = n[0];
    return { n: n.length, text: first ? first.textContent : '',
             inDetail: first ? !!first.closest('#pane-packinfo .pane-detail') : false,
             dir: (window.__ts && window.__ts.meta && window.__ts.meta.packs_dir) || '' };
  })()`);
  check("行业包详情里给出可写包目录（读 /api/meta 的 packs_dir，且不重复叠加）",
    packsDir.n === 1 && packsDir.inDetail && packsDir.dir
      && packsDir.text.indexOf(packsDir.dir) >= 0,
    JSON.stringify(packsDir));

  // .kb-item 家族一致性（kb / skills / packinfo 三处共用同一族）——
  // 既然 kb / skills 面板已并到行业包面板里，这条断言只在新位置复核一遍。
  // ⚠ 取 .pl-item 时要避开 .on / .sel（业务高亮态）。
  const kbVisMatch = await evalIn(`return (function(){
    var kb = document.querySelector('#pi-groups .kb-item:not(.on)');
    if (!kb) return { ok: false, reason: 'no kb-item' };
    var pl = document.querySelector('.pl-item:not(.on):not(.sel)');
    var plUsed = pl || document.querySelector('.pl-item');
    if (!plUsed) return { ok: false, reason: 'no pl-item' };
    var k = getComputedStyle(kb), p = getComputedStyle(plUsed);
    var bg = function(s){
      var m = String(s || '').match(/^rgba?\(([^)]+)\)/);
      if (!m) return s === 'transparent' ? 0 : null;
      var parts = m[1].split(',').map(function(x){return parseFloat(x.trim());});
      return parts.length >= 4 ? parts[3] : 1;
    };
    var firstCol = function(g){
      var s = String(g || '').trim();
      var first = s.split(/[ ]+/)[0] || '';
      return /^[0-9]+(?:\.[0-9]+)?px$/.test(first) ? parseFloat(first) : null;
    };
    return {
      ok: k.padding === p.padding
        && k.borderRadius === p.borderRadius
        && k.fontSize === p.fontSize
        && firstCol(k.gridTemplateColumns) === firstCol(p.gridTemplateColumns)
        // 原本硬要求 firstCol === 30：钉的是某一档绝对值，档位一调就得回来改断言。
        // 换成规范 v1 的硬约束「图标盒必须是 4 的倍数」（docs/UI视觉规范.md 第 2 节）：
        // 既不许两族分叉（上面已比过相等），也不许回到 30 这种非 4 倍数值。
        && firstCol(k.gridTemplateColumns) % 4 === 0
        && bg(k.backgroundColor) === 0
        && bg(p.backgroundColor) === 0,
      kb: { padding: k.padding, radius: k.borderRadius,
            grid: k.gridTemplateColumns, fontSize: k.fontSize,
            iconCol: firstCol(k.gridTemplateColumns),
            bgAlpha: bg(k.backgroundColor) },
      pl: { padding: p.padding, radius: p.borderRadius,
            grid: p.gridTemplateColumns, fontSize: p.fontSize,
            iconCol: firstCol(p.gridTemplateColumns),
            bgAlpha: bg(p.backgroundColor),
            classes: String(plUsed.className) }
    };
  })()`);
  check("行业包面板的文件条目（.kb-item）与 .pl-item 视觉同族（透明默认 + 一致几何）",
    kbVisMatch.ok, JSON.stringify(kbVisMatch));

  // 「内容漏出卡片」探针：卡片高度必须容得下内容。
  // 历史上 `.kb-item` 是 `<button>`，被全局 `button { height: 30px }` 钉死，
  // 而卡里是「30px 图标 + 一行文件名」，内容从底部漏出、还被下一张卡的白底
  // 盖住半行 —— 用户 2026-09-16 贴图报的就是这个。
  // ⚠ 口径**不在这里写**，在 `_verify/lib/overflow.js`（probe.js 用同一份）。
  // ⚠ 量到 HIDDEN 要当失败：面板 display:none 时高度全是 0，差值无意义，
  //    不显式报出来这条断言就变成空转的假绿。
  const overflowProbe = (sel) => probeSource({
    selector: sel,
    nameFn: `function(n){ var e = n.querySelector('.kb-name');
      return e ? e.textContent : n.tagName + '.' + String(n.className || '').split(' ')[0]; }`,
  });
  const piOver = await evalIn(overflowProbe("#pi-groups .kb-item"));
  check("行业包面板的文件卡片容得下内容（不被按钮固定高度压扁）",
    piOver.length === 0 && !piOver.hidden, JSON.stringify(piOver));

  // 文件分组与查看器必须**左右并排**（2026-09-17）。
  // 之前上下堆叠：9 个角色分组全展开后查看器被推到很下面，点完一个文件
  // 要滚一大段才看到内容 —— 用户贴图报的就是这个。
  // ⚠ 只看 gap 不够：上下堆叠的两块左对齐时 gap 也是 0。必须**同时**看
  //   垂直重叠 —— 并排时两块的垂直区间重叠，堆叠时 view.top >= list.bottom。
  // ⚠ 还要看查看器的高：父链上没定死高度时 `.kb-body{flex:1}` 不生效，
  //   查看器会塌成一小块（这正是它以前只能靠内容撑高的原因）。
  const piSideBySide = await evalIn(`return (function(){
    var l = document.querySelector('#pane-packinfo .pi-groups-col');
    var v = document.getElementById('pi-file-view');
    if (!l || !v) return { missing: true };
    var lr = l.getBoundingClientRect(), vr = v.getBoundingClientRect();
    return { listW: Math.round(lr.width), viewW: Math.round(vr.width),
             gap: Math.round(vr.left - lr.right),
             vertOverlap: Math.round(Math.min(lr.bottom, vr.bottom) - Math.max(lr.top, vr.top)),
             viewH: Math.round(vr.height) };
  })()`);
  check("包内容：文件分组与查看器左右并排（点完文件不用滚下去找）",
    !piSideBySide.missing
      && piSideBySide.gap >= 0 && piSideBySide.gap < 60
      && piSideBySide.vertOverlap > 200
      && piSideBySide.viewW >= 300 && piSideBySide.viewH >= 250,
    JSON.stringify(piSideBySide));

  // 切包时查看器复位 —— 不复位就粘上个包的内容。
  // 校验方式：先点一个文件 → 确认 body 有内容；再点中列另一个包 →
  // 等待新 openPackInfo 完成 → body 应该回到「未选择文件」占位态。
  // 用一个真包 + 一个明显不同的占位字符判断（不能直接对比"未选择文件"
  // 字面，因为这是开放文本；改用 bodyLen 小 + 不含刚才那个文件特征）。
  // ⚠ 2026-09-17 重组后「on 行」是 `.kb-item.on`，不再是 `tr.on`。
  await evalIn(`document.querySelector('#pi-list .pl-item:not(.on)').click();
    return true;`);
  await sleep(1500);  // 切包 openPackInfo 是异步的，要等
  const piReset = await evalIn(`return {
    title: document.getElementById('pi-file-title').textContent,
    bodyLen: document.getElementById('pi-file-body').textContent.length,
    hasTopic: /分领域内容知识库/.test(document.getElementById('pi-file-body').textContent),
    onItems: document.querySelectorAll('#pi-groups .kb-item.on').length };`);
  check("切换行业包后，文件查看器复位到「未选择文件」占位态（不粘上个包内容）",
    piReset.title === '未选择文件' && piReset.bodyLen < 100
      && !piReset.hasTopic && piReset.onItems === 0,
    JSON.stringify(piReset));

  await evalIn(`window.__ts.setPane('llm'); return true;`);
  await sleep(300);
  const llm = await evalIn(`return {
    // 三列化后模型列表从 <table> 行改成中列的 .pl-item 条目
    // （保留 mdl-label / mdl-sub / mdl-switch 类名，断言口径得以延续）
    rows: document.querySelectorAll('#llm-list .pl-item').length,
    label: document.querySelector('#llm-list .pl-item .mdl-label')?.textContent,
    sub: document.querySelector('#llm-list .pl-item .mdl-sub')?.textContent,
    // 服务商不再有独立的 .col-prov 列，合并进 mdl-sub（「模型ID · 服务商」）
    activeRows: document.querySelectorAll('#llm-list .pl-item.on').length,
    switchOn: document.querySelectorAll('#llm-list .pl-item .mdl-switch.on').length,
    // 操作按钮从表格的 .mdl-op 移到右列的 #md-form-card
    ops: ['md-test', 'md-save', 'md-cancel'].filter(function (id) {
      return !!document.getElementById(id); }).length,
    status: document.getElementById('st-status').textContent,
    cfgErrHidden: document.getElementById('st-cfg-err')?.classList.contains('hidden'),
    cfgErrText: document.getElementById('st-cfg-err')?.textContent || '' };`);
  // 模型从「三个平铺输入框」变成了**一份列表**：一条一个，带服务商与操作。
  // 平铺那版加第二个模型没有位置可填，只能把第一个覆盖掉。
  check("模型接口列出模型（中列条目一条一条，右列有操作按钮）",
    llm.rows === 1 && llm.ops === 3 && /·/.test(llm.sub || ""),
    JSON.stringify(llm));
  check("模型条目显示模型名与「模型 ID · 服务商」小字",
    llm.label === "glm-4.7" && /glm-4\.7/.test(llm.sub || ""),
    JSON.stringify({ label: llm.label, sub: llm.sub }));
  // 当前启用的那一条要有落点：否则几条长得一样，只能靠开关的明暗去猜
  check("当前启用的模型条目有唯一落点（条高亮 + 开关亮着）",
    llm.activeRows === 1 && llm.switchOn === 1,
    JSON.stringify({ activeRows: llm.activeRows, switchOn: llm.switchOn }));
  // 表头与「当前启用」那一行**不能同色相邻**。
  // 修复前两者铺的都是 --fill，上下紧贴、颜色一模一样 —— 在界面上读成
  // **一整块灰**：表头「模型/服务商/启用/操作」和下面那条数据行连成一片，
  // 看不出哪一行才是数据；行自带 --r-sm 圆角，还在表头下沿留了两个白缺口。
  // 表头现在只用一条发丝线（与全站其他表格一致），灰底归那一行独占。
  //
  // 三列化后表格没了（没有 thead），但**同类风险换了个地方**：
  // 「当前启用」的那条 `.pl-item.on` 若底色与列表容器同色、又没有描边，
  // 就是**隐形**—— 与上一轮修的「内容查看器 4% 底隐形」是同一个坑。
  // 所以判据从「表头 vs 启用行」改成「启用条目 vs 它所在的列表」。
  //
  // 仍取「有效底色」的**亮度差**，不取颜色字符串：
  //   - 背景可能是透明的，往上找到的底才是白页 —— 字符串比会把「透明 vs 白」
  //     判成不同，而真正要守的是「两者读起来不是一块」；
  //   - 逐层把 rgba 叠到白底上再比亮度；差值不足时还允许靠**描边**兜住。
  const mdlOn = await evalIn(`return (function(){
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
    var on = document.querySelector('#llm-list .pl-item.on');
    var list = document.querySelector('#llm-list');
    if (!on || !list) return { none: true };
    var cs = getComputedStyle(on);
    return { onLum: bgOf(on), listLum: bgOf(list),
             onBg: cs.backgroundColor,
             hasBorder: cs.boxShadow !== 'none' || parseFloat(cs.borderTopWidth) > 0 };
  })()`);
  check("「当前启用」的条目在列表里看得出来（底色拉得开或有描边，不隐形）",
    !mdlOn.none
      && (Math.abs(mdlOn.onLum - mdlOn.listLum) >= 6 || mdlOn.hasBorder),
    JSON.stringify(mdlOn));

  // 模型接口的三列形态：中列条目 + 右列编辑表单，且**弹窗已从 DOM 移除**。
  // 留着弹窗就会有「两套编辑 UI」，改哪套的问题迟早出现（同一信息两份表示）。
  const llm3 = await evalIn(`var list = document.querySelector('#llm-list');
    var detail = document.querySelector('#pane-llm .pane-detail');
    var lr = list.getBoundingClientRect(), dr = detail.getBoundingClientRect();
    return { listW: Math.round(lr.width), detailW: Math.round(dr.width),
             sideBySide: Math.round(dr.left - lr.right),
             dialogGone: !document.getElementById('model-dialog'),
             formInDetail: !!document.querySelector('#pane-llm .pane-detail #md-form-card') };`);
  check("模型接口是中列条目 + 右列编辑（三列骨架，且模型弹窗已移除）",
    llm3.listW === 258 && llm3.detailW > 600
      && llm3.sideBySide >= 0 && llm3.sideBySide < 50
      && llm3.dialogGone && llm3.formInDetail,
    JSON.stringify(llm3));
  check("接口状态显示重试/超时等实际生效值", /重试/.test(llm.status), llm.status);
  // 前端**不许**比后端更严，也不许更松：越界值要当场拒掉、请求根本不发出去。
  // 修复前前端单独一个 if 卡 temperature 1.5（比后端的 2.0 严）→ 点保存弹 toast
  // 并 return，后端那句更宽松的校验永远不会被触发。
  // 2026-09-17：temperature / max_tokens 先后从高级配置 UI 移除，只剩
  // retries（0~10）与 timeout（5~1800）—— 用 retries 填 99 验「越界被拒」。
  // ⚠ 同日起「保存」是**模型表单那颗按钮**（高级配置并入它，不再有 st-save），
  //   所以要先保证 md-model / md-baseurl 有值，否则会被表单自己的校验拦住。
  await evalIn(`var n = document.getElementById('st-retries');
    n.value = '99'; n.dispatchEvent(new Event('input', {bubbles:true}));
    var m = document.getElementById('md-model'); m.value = 'glm-4.7';
    m.dispatchEvent(new Event('input', {bubbles:true}));
    var u = document.getElementById('md-baseurl');
    if (!u.value) { u.value = 'https://x/v4'; u.dispatchEvent(new Event('input', {bubbles:true})); }
    window.__lastConfigBody = null; return true;`);
  await evalIn(`document.getElementById('md-save').click(); return true;`);
  await sleep(800);
  const over = await evalIn(`return window.__lastConfigBody || null;`);
  check("越界的重试次数被前端当场拒掉（与后端区间一致，不发请求）",
    over === null, JSON.stringify(over));
  // 还原成默认，免得影响后面「重试 / 超时已回填」那条断言
  await evalIn(`var n = document.getElementById('st-retries');
    n.value = '2'; n.dispatchEvent(new Event('input', {bubbles:true})); return true;`);

  // 数值项的保存请求体里**只有数值项** —— 连接信息归模型条目管，
  // 这里再带一遍就等于同一件事有两处写入口。
  // 2026-09-17 后它由模型表单的「保存」间接触发（saveModel 先、saveConfig 后），
  // 但两者的**职责边界没变**：POST /api/config 只该收到数值项。
  await evalIn(`var n = document.getElementById('st-timeout');
    n.value = '90'; n.dispatchEvent(new Event('input', {bubbles:true}));
    window.__lastConfigBody = null; return true;`);
  await evalIn(`document.getElementById('md-save').click(); return true;`);
  await sleep(800);
  const t18 = await evalIn(`return window.__lastConfigBody || null;`);
  check("数值项的保存请求体里不带连接信息（那归模型条目管）",
    !!t18 && !('base_url' in t18) && !('model' in t18) && t18.timeout === 90,
    JSON.stringify(t18));
  // 还原成默认值，免得影响后面「重试 / 超时已回填」（它查 timeout === "180"）
  await evalIn(`var n = document.getElementById('st-timeout');
    n.value = '180'; n.dispatchEvent(new Event('input', {bubbles:true})); return true;`);
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
    open: !document.getElementById('md-form-card').classList.contains('hidden'),
    title: document.getElementById('md-title').textContent,
    id: document.getElementById('md-id').value,
    url: document.getElementById('md-baseurl').value,
    model: document.getElementById('md-model').value };`);
  check("「添加模型」打开的是表单弹窗（不再是跳回设置页填一个名字）",
    dlg.open && dlg.title === "添加模型" && dlg.id === ""
    && dlg.url === "" && dlg.model === "", JSON.stringify(dlg));
  // 2026-09-17（任务 6）：md-reset-baseurl 已删除 ——「恢复默认」整段下线。
  // 这里不再 click 那个按钮（DOM 上找不到），改成直接验证 url 仍是空 +
  // model 仍是空（没点"恢复默认"、也没自己填，就是个空表单）。
  await sleep(200);
  const dreset = await evalIn(`return {
    url: document.getElementById('md-baseurl').value,
    model: document.getElementById('md-model').value,
    // 恢复默认按钮已下线 —— DOM 上不应存在。
    resetBtnExists: !!document.getElementById('md-reset-baseurl'),
    // tag-default 已下线（任务 5）—— DOM 上不应存在。
    tagExists: !!document.querySelector('#md-form-card .tag-default') };`);
  check("添加模型表单里没有「恢复默认」按钮 / 「内置默认」小标（任务 5/6）",
    dreset.url === "" && dreset.model === ""
      && dreset.resetBtnExists === false && dreset.tagExists === false,
    JSON.stringify(dreset));
  await evalIn(`document.getElementById('md-cancel').click(); return true;`);
  await sleep(200);
  // 原来是「弹窗可以取消，不留残余浮层」；三列化后表单常驻右列、不再有浮层，
// 所以「取消」的语义变成**回到当前启用的那条**、把刚才填的东西丢掉。
// 守的是状态被重置（不留残余），不是 hidden。
const dclosed = await evalIn(`var on = document.querySelector('#llm-list .pl-item.on');
  return { id: document.getElementById('md-id').value,
           onId: on ? on.dataset.id : null,
           title: document.getElementById('md-title').textContent,
           url: document.getElementById('md-baseurl').value };`);
check("取消编辑后回到当前启用的那条，且不留残余输入",
  !!dclosed.onId && dclosed.id === dclosed.onId && dclosed.title === "编辑模型",
  JSON.stringify(dclosed));

  // 重试 / 超时：原来只在状态行里展示、无法修改。
  // 2026-09-17：输出预算（max_tokens）已从高级配置 UI 移除
  //（用户原话「高级设置去掉 token 限制吧」），这里只查剩下两项。
  const adv = await evalIn(`window.__ts.setPane('llm');
    const t = document.getElementById('st-timeout'); return {
      retries: document.getElementById('st-retries')?.value,
      timeout: t?.value,
      // 输出预算的输入框必须**彻底不在 DOM 上**（不是藏起来）
      maxInputGone: !document.getElementById('st-maxtokens'),
      editable: !!t && !t.readOnly && !t.disabled };`);
  check("重试 / 超时可编辑且已回填（输出预算已从 UI 移除）",
    adv.retries === "2" && adv.timeout === "180" && adv.maxInputGone && adv.editable,
    JSON.stringify(adv));

  // 同一组字段的标签必须一样高。
  // 「重试次数」标签里有个行内的 `.linkbtn`（「恢复默认」）—— 它同样是 `<button>`，
  // 被全局 `button { height: var(--h-btn) }`（30px）接管；`label.lbl` 是块盒，
  // 这个 inline-block 会把**行盒**顶到 30px，于是四个字段里只有它比别人高 14px，
  // 那一个输入框跟着下沉，整组字段的节奏就断了（实测 16 / 30 / 16 / 16）。
  // 判据是「彼此相等」而不是某个绝对值：把 30 改成 24 也一样是坏的。
  const advLbl = await evalIn(`return (function(){
    var rows = [].slice.call(document.querySelectorAll('#st-adv .block-inner'))
      .map(function (b) {
        var l = b.querySelector('.lbl');
        return { t: l ? l.textContent.trim().slice(0, 4) : '',
                 h: l ? Math.round(l.getBoundingClientRect().height * 10) / 10 : -1 };
      });
    var hs = rows.map(function (r) { return r.h; });
    return { rows: rows, same: hs.length > 1 && hs.every(function (h) { return h === hs[0]; }) };
  })()`);
  check("高级配置里各字段的标签行高一致（行内按钮不再顶高行盒）",
    advLbl.same, JSON.stringify(advLbl));

  await evalIn(`window.__ts.setPane('llm');
    const t = document.getElementById('st-timeout');
    t.value = '60'; t.dispatchEvent(new Event('input', {bubbles:true}));
    document.getElementById('st-reset-adv').click(); return true;`);
  await sleep(500);
  const adv2 = await evalIn("return document.getElementById('st-timeout').value;");
  check("高级项「恢复默认」还原超时值", adv2 === "180", adv2);

  // ── 知识库 / 技能面板合并到「行业包」面板（2026-09-17 重组） ────────────
  // 之前这里有二十多条断言守护 kb / skills 面板的几何与内容。重组后：
  //   ① #pane-kb / #pane-skills 这两个 section 不再存在（断言下面"导航里
  //     只有 4 个导航项"会守护）。
  //   ② 行业包面板的 #pi-groups 是它们共同的归宿（前面那条断言
  //     "行业包面板覆盖知识 / 技能 / 合规 / 私有等所有角色"已经覆盖）。
  // 那二十几条 kb/skills 几何断言都被新的 #pi-groups / #pi-file 断言替代，
  // 故此处不再重复。


  // ── 知识库 / 技能 已并入「行业包」面板（2026-09-17 重组） ──────────────
  // 重组前这里守着二十多条 kb / skills 面板的几何与内容断言。现在那两个
  // section 连同导航项一起删掉了，同一份数据只由「行业包」面板承载，
  // 断言也归并到前面的 #pi-groups / #pi-file-view 那几条。
  // 此处只留重组后**仍然成立**的两条结构断言。
  //
  // ① 导航与面板里再没有 kb / skills 的痕迹。
  //    这不是「顺手查一下」—— 留一个隐藏的空 section，下一个人会以为
  //    它还在，改的时候改了不生效的地方。
  const kbGone = await evalIn(`return {
    navPanes: [...document.querySelectorAll('.stg-nav-item')].map(function(n){ return n.dataset.pane; }),
    paneKb: !!document.getElementById('pane-kb'),
    paneSkills: !!document.getElementById('pane-skills'),
    kbPackSel: !!document.getElementById('kb-pack'),
    skillsPackSel: !!document.getElementById('skills-pack'),
    kbBody: !!document.getElementById('kb-body'),
    skillsBody: !!document.getElementById('skills-body') };`);
  check("知识库 / 技能 面板与其行业包下拉已彻底移除（不残留隐藏 DOM）",
    !kbGone.paneKb && !kbGone.paneSkills && !kbGone.kbPackSel
      && !kbGone.skillsPackSel && !kbGone.kbBody && !kbGone.skillsBody
      && !kbGone.navPanes.includes('kb') && !kbGone.navPanes.includes('skills'),
    JSON.stringify(kbGone));

  // ② 剩下的三个设置面板（生成偏好 / 行业包 / 模型接口）**同族**：
  //    都有 .has-cols、.stg-cols 直接挂在 .stg-pane 下、max-width 一致。
  //    重组前 kb / skills 缺 .has-cols（面板窄一截）且 .stg-cols 套在
  //    .page-card 里（带 border-top + padding），看起来是两套布局 ——
  //    这条断言守住「现在只有一套」。
  const layoutMatch = await evalIn(`return (function(){
    var ids = ["pane-gen","pane-packinfo","pane-llm"];
    var out = {};
    for (var i=0; i<ids.length; i++){
      var sec = document.getElementById(ids[i]);
      if (!sec) { out[ids[i]] = {missing:true}; continue; }
      var cols = sec.querySelector(".stg-cols");
      out[ids[i]] = {
        hasCols: sec.classList.contains("has-cols"),
        maxW: getComputedStyle(sec).maxWidth,
        colsDirectParent: cols ? cols.parentElement.className.replace(/.*has-cols.*/, "stg-pane") : null,
        secW: Math.round(sec.getBoundingClientRect().width)
      };
    }
    return out;
  })()`);
  const maxWSet = new Set(Object.values(layoutMatch).map(p => p.maxW));
  const colsParents = new Set(Object.values(layoutMatch).map(p => p.colsDirectParent));
  check("三个设置面板同族（has-cols / .stg-cols 直接挂节下 / max-width 一致）",
    maxWSet.size === 1 && colsParents.size === 1
      && [...colsParents].every(c => c === "stg-pane")
      && Object.values(layoutMatch).every(p => p.hasCols),
    JSON.stringify(layoutMatch));

  // ③ 内容查看器必须**自己成块**（不透明于白底），不能是 `--fill` (4%) 那种
  //    几乎透明的底 —— 否则三列布局的右栏「看起来什么都没有」。
  //    修法见 styles.css 的 `.kb-body`：背景换 `--fill-strong` (7%) + 发丝描边。
  //    判据：背景 alpha ≥ 5% **或** 有 box-shadow 描边，任一即可。
  const kbVis = await evalIn(`return (function(){
    var b = document.getElementById('pi-file-body');
    if (!b) return { missing: true };
    var cs = getComputedStyle(b);
    var m = cs.backgroundColor.match(/rgba?\\(([^)]+)\\)/);
    var alpha = m ? parseFloat(m[1].split(',')[3]) : 0;
    return { missing: false, bg: cs.backgroundColor, alpha: alpha,
             shadow: cs.boxShadow, hasShadow: cs.boxShadow !== 'none' };
  })()`);
  check("内容查看器自己成块（背景不透明于白底）",
    !kbVis.missing && (kbVis.alpha >= 0.05 || kbVis.hasShadow),
    JSON.stringify(kbVis));

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

  // ── 导出 / 返回 两个入口已删（2026-09-17）──
  // 原来这里测的是「点导出 → 提示里给出真实文件数与路径」的**前端流程**。
  // 入口删了，那段前端流程也就不存在了 —— 后端 `/api/packs/<n>/export-skill`
  // 的覆盖在 pytest 里，这里只守「界面上确实没有了」。
  // 跟 kb / skills 那次同一个道理：留一个隐藏的空壳，下一个人会以为它还在，
  // 改的时候改了不生效的地方。
  await evalIn(`document.getElementById('btn-open-settings').click();
    window.__ts.setPane('packinfo'); return true;`);
  await sleep(500);
  const piActions = await evalIn(`return {
    exportBtn: !!document.getElementById('pi-export'),
    exportHint: !!document.getElementById('pi-export-hint'),
    closeBtn: !!document.getElementById('pi-close'),
    undraftBtn: !!document.getElementById('pi-undraft'),
    bottomActions: [].slice.call(
      document.querySelectorAll('#pane-packinfo .pane-detail > .page-actions button')
    ).map(function(b){ return b.id || b.textContent.trim(); }) };`);
  check("包详情底部只剩「标记为已校对」（导出 / 返回 已移除，不残留隐藏 DOM）",
    !piActions.exportBtn && !piActions.exportHint && !piActions.closeBtn
      && piActions.undraftBtn
      && piActions.bottomActions.length === 1
      && piActions.bottomActions[0] === 'pi-undraft',
    JSON.stringify(piActions));

  // 三个设置面板的 page-head 结构必须一致（2026-09-17）：
  // 之前 `pane-gen` 只有 `<h2>` 加 `<p.hint>` 直挂在 `.page-head` 里（缺
  // `.page-head-titles` 包装），于是 h2 拿不到 20px 样式、hint 还跟标题
  // **横向并排**。现在所有面板统一成 `.page-head-titles > h2 + hint`，
  // 视觉才一致。
  // ⚠ 这里只看**面板顶部**那个 .page-head（用 `:scope > .page-head` 锁住
  // 直系子级），不看 .pane-detail 里嵌套的 .page-head（packinfo 右列的
  // 「电梯 · 包内容」是另一个层级断言的范围）。
  const pageHeadUnified = await evalIn(`return (function(){
    var ids = ['pane-gen', 'pane-packinfo', 'pane-llm'];
    var out = {};
    for (var i=0; i<ids.length; i++) {
      var sec = document.getElementById(ids[i]);
      if (!sec) { out[id] = {missing: true}; continue; }
      var ph = sec.querySelector(':scope > .page-head');
      var titles = ph ? ph.querySelector(':scope > .page-head-titles') : null;
      var h2 = titles ? titles.querySelector('h2') : null;
      var hint = titles ? titles.querySelector('.hint') : null;
      // computed font-size：所有面板的 h2 字号应一致（var(--f-xl) = 20px）
      out[ids[i]] = {
        hasTitlesWrap: !!titles,
        hasH2: !!h2,
        hasHint: !!hint,
        h2FontSize: h2 ? getComputedStyle(h2).fontSize : null,
      };
    }
    // 判据比对的是**页面级标题档**的解析值，不是手抄的 px：
    // 2026-09-21 这一档从 --f-xl(20) 提为 --f-display(24)，写死 '20px'
    // 会让换档变成"改断言"，而这条想守的是「三个 h2 吃同一档」。
    out['__display'] = getComputedStyle(document.documentElement)
      .getPropertyValue('--f-display').trim();
    return out;
  })()`);
  const pageHeadPanes = Object.values(pageHeadUnified)
    .filter(v => v && typeof v === 'object');
  const h2Sizes = new Set(pageHeadPanes
    .filter(p => !p.missing && p.h2FontSize)
    .map(p => p.h2FontSize));
  check("三个设置面板的 page-head 结构与字号统一（.page-head-titles > h2 + hint）",
    pageHeadPanes.every(p => !p.missing && p.hasTitlesWrap && p.hasH2 && p.hasHint)
      && h2Sizes.size === 1 && [...h2Sizes][0] === pageHeadUnified.__display,
    JSON.stringify(pageHeadUnified));

  // 右列（.pane-detail）顶部 h3 字号一致（2026-09-17）：
  //   packinfo: <h3 id="pi-title">（之前 .detail-title 16px）
  //   llm:      <h3 id="md-title">（之前 .card-title 14px）
  //   gen:      <h3 class="card-title">生成参数 / 进阶</h3>（14px）
  // 三个 h3 同一个角色（**右列顶部 h3**）出现 14px / 16px 两种字号，
  // 是「同角色不同字号」的典型不统一。统一在 16px：删 .detail-title、
  // .card-title 改 16px。
  // ⚠ 选中面板不同 → 有些 h3 是隐藏的。Computed style 仍然可信（display:none
  //   也算正常样式），所以全量 querySelector 即可。
  const rcTitle = await evalIn(`return (function(){
    var ids = ['pane-gen', 'pane-packinfo', 'pane-llm'];
    var out = {};
    for (var i=0; i<ids.length; i++) {
      var sec = document.getElementById(ids[i]);
      if (!sec) { out[ids[i]] = {missing: true}; continue; }
      var h3 = sec.querySelector('.pane-detail > .page-head h3')
             || sec.querySelector('.pane-detail > .page-card h3')
             || sec.querySelector('.pane-detail h3');
      if (!h3) { out[ids[i]] = {missing: 'no h3'}; continue; }
      var c = getComputedStyle(h3);
      out[ids[i]] = { id: h3.id, cls: h3.className,
                      fs: c.fontSize, fw: c.fontWeight, ls: c.letterSpacing };
    }
    return out;
  })()`);
  const fsSet = new Set(Object.values(rcTitle).filter(p => p.fs).map(p => p.fs));
  const fwSet = new Set(Object.values(rcTitle).filter(p => p.fw).map(p => p.fw));
  const lsSet = new Set(Object.values(rcTitle).filter(p => p.ls).map(p => p.ls));
  // 原本硬要求 `=== '16px'`：钉的是某一档绝对值，档位一调就得回来改断言。
  // 现在改成「三者相等 **且** 落在 :root 的字阶表里」—— 字阶表**从 CSS 变量现场读**，
  // 不在测试里重抄一遍数字（同一信息两份表示正是本项目要防的）。
  const typeScale = await evalIn(`var cs = getComputedStyle(document.documentElement);
    return ['--f-2xs','--f-sm','--f-md','--f-xl'].map(function(k){
      return cs.getPropertyValue(k).trim(); });`);
  check("三个右列顶部 h3 字号字重字距一致（同角色 h3 不分家）",
    fsSet.size === 1 && typeScale.indexOf([...fsSet][0]) >= 0
      && fwSet.size === 1 && [...fwSet][0] === '600'
      && lsSet.size === 1,
    JSON.stringify(rcTitle) + ' scale=' + JSON.stringify(typeScale));

  // 「一屏一个保存」（2026-09-17 二改）：独立的高级配置保存按钮**整段删掉**，
  // 高级配置改由模型表单的「保存」一并存掉（settings.js 的 saveAdvancedConfig）。
  // 用户原话「模型面板有两个保存」—— 第一次是用折叠区把两个按钮错开，
  // 但同屏两个确认按钮依然是更差的设计，所以这次直接合并。
  // 判据：DOM 上**不存在** st-save；且右列可见的保存类按钮只有一个。
  const saveBtns = await evalIn(`window.__ts.setPane('llm');
    return (function(){
    var pane = document.getElementById('pane-llm');
    var vis = [].slice.call(pane.querySelectorAll('button'))
      .filter(function(b){ return !b.classList.contains('hidden')
        && b.offsetParent !== null
        && /保存/.test(b.textContent); })
      .map(function(b){ return b.id + ':' + b.textContent.trim(); });
    return { stSaveGone: !document.getElementById('st-save'), visibleSaves: vis };
  })()`);
  check("模型面板一屏只有一个「保存」（独立的高级配置保存按钮已删）",
    saveBtns.stSaveGone && saveBtns.visibleSaves.length === 1
      && saveBtns.visibleSaves[0] === "md-save:保存",
    JSON.stringify(saveBtns));

  // 顶部「新增 / 添加」主操作按钮样式统一（2026-09-17）：
  // packinfo 的 [+ 新建] 跟 llm 的 [+ 添加模型] 都是 .page-head-actions
  // 里的**主动作**，用 primary slim（实心深色）；同区「刷新」用 ghost bordered
  // —— 主次分明。判据：列 .page-head-actions 里所有 primary 按钮，
  // 并至少有一个 + 至少有一个 ghost.bordered（说明主次都存在）。
  const topBtns = await evalIn(`return (function(){
    var out = [];
    ['pane-gen','pane-packinfo','pane-llm'].forEach(function(id){
      var sec = document.getElementById(id);
      if (!sec) return;
      var act = sec.querySelector('.page-head-actions');
      if (!act) { out.push({id:id, hasAct: false}); return; }
      var btns = [].slice.call(act.querySelectorAll('button')).map(function(b){
        return { id: b.id, cls: b.className, text: b.textContent.trim() };
      });
      out.push({id:id, hasAct: true, btns: btns});
    });
    return out;
  })()`);
  // 找出所有顶级操作（带 primary slim 的）+ 所有次级操作（ghost bordered 的）
  var prims = topBtns.flatMap(function(p){ return (p.btns||[]).filter(function(b){ return /\bprimary\b/.test(b.cls) && /\bslim\b/.test(b.cls); }).map(function(b){ return {pane:p.id, btn:b}; }); });
  var ghosts = topBtns.flatMap(function(p){ return (p.btns||[]).filter(function(b){ return /\bghost\b/.test(b.cls) && /\bbordered\b/.test(b.cls); }).map(function(b){ return {pane:p.id, btn:b}; }); });
  check("顶部新增按钮样式：主动作统一 primary slim，次动作 ghost bordered",
    prims.length >= 2 && ghosts.length >= 1
      // 所有 primary slim 都是「新建/添加」类语义（按钮文案以 + 新建/添加 开头）
      && prims.every(function(p){ return /^[+]?\s*(新建|添加)/.test(p.btn.text); }),
    JSON.stringify({ prims: prims.map(function(p){ return p.pane+":"+p.btn.id; }),
                     ghosts: ghosts.map(function(p){ return p.pane+":"+p.btn.id; }) }));

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

  // P3-50 的另一半：结果页的清单改了行级渲染，**包详情这一屏没改** —— 同一个
  // 校对清单.md 在两个界面一份是清单、一面是 markdown 文本墙。判据只看"行首"，
  // 因为正文里可以合法出现 "## " 这个词（清单的续行就在引用它）。
  const clRows = await evalIn(`var box = document.getElementById('pi-checklist');
    var rows = [].slice.call(box.children);
    var val = function(r){ return r.lastElementChild ? r.lastElementChild.textContent : ''; };
    return { wrap: document.getElementById('pi-checklist-wrap').classList.contains('hidden'),
      tag: box.tagName, title: document.getElementById('pi-title').textContent,
      n: rows.length,
      allRows: rows.every(function(r){ return r.className === 'pg-summary-row'; }),
      hasMark: rows.every(function(r){ var k = r.firstElementChild;
        return !!k && k.className === 'pg-summary-k'; }),
      unchecked: rows.filter(function(r){ return r.firstElementChild.textContent === '□'; }).length,
      h1: rows.filter(function(r){ return val(r).indexOf('#') === 0; }).length,
      rawTodo: rows.filter(function(r){ return val(r).indexOf('- [') === 0; }).length,
      rawHead: rows.filter(function(r){ return val(r).indexOf('## ') === 0; }).length,
      text: box.textContent.slice(0, 24) };`);
  check("草稿包详情的校对清单是行级清单（□ 一栏 + 正文），不是 markdown 原文",
    clRows.wrap === false && clRows.tag === 'DIV' && clRows.n >= 6 && clRows.allRows
      && clRows.hasMark && clRows.unchecked === 3
      && clRows.h1 === 0 && clRows.rawTodo === 0 && clRows.rawHead === 0,
    JSON.stringify(clRows));

  // 先在列表里**点开 elevator 的详情**（下拉的 change 只管生成参数，不刷新右列），
  // 然后把下拉漂到 fitment 但不刷新右列：这时点「标记为已校对」必须转正 elevator，
  // 而不是下拉里的 fitment（修复前 settings.js 读 `$("pack").value`；
  // 桩原来无论问哪个包都回电梯的草稿位，两件事一起被掩盖）。
  const openedElevator = await evalIn(`var it = [].slice.call(
      document.querySelectorAll('#pi-list .pl-item'))
      .find(function(n){ return n.dataset.id === 'elevator'; });
    if (it) it.click(); return !!it;`);
  check("行业包列表里能点到 elevator（上一条的前提，列表没渲染就别往下量）",
    openedElevator === true, String(openedElevator));
  await sleep(400);
  // 上一条的桩改忠实之后立刻多出来的分支：仓库里手写包（elevator）没有 校对清单.md，
  // 真实后端回 checklist:null → 整块区域必须收起。原来桩恒回一句非空文本，
  // 这一支界面代码在整个门禁里从没跑过。
  const clElev = await evalIn(`return {
    wrap: document.getElementById('pi-checklist-wrap').classList.contains('hidden'),
    title: document.getElementById('pi-title').textContent };`);
  check("没有校对清单文件的手写包：整块清单区收起，不留一个空壳标题",
    clElev.wrap === true && clElev.title.indexOf('电梯') >= 0, JSON.stringify(clElev));
  await evalIn(`var sDrift = document.getElementById('pack');
    sDrift.value = 'fitment'; return true;`);
  await evalIn(`document.getElementById('pi-undraft').click(); return true;`);
  await sleep(300);
  await evalIn(`document.getElementById('cd-yes').click(); return true;`);   // 确认弹窗
  await sleep(700);
  const afterUndraft = await evalIn(`return {
    hidden: document.getElementById('pi-undraft').classList.contains('hidden'),
    title: document.getElementById('pi-title').textContent,
    und: window.__und || 'NO-REQUEST' };`);
  check("转正后「标记为已校对」按钮消失（刷新生效）",
    afterUndraft.hidden === true && !/草稿/.test(afterUndraft.title),
    JSON.stringify(afterUndraft));
  check("「标记为已校对」作用于**右列正在看的那个包**，不是下拉里漂移的另一个（P3-51）",
    !!afterUndraft.und && afterUndraft.und.name === 'elevator',
    JSON.stringify(afterUndraft.und));
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
  // 2026-09-17 重组后导航只剩三项：生成偏好 / 模型接口（基础设置）、
  // 行业包（行业）。知识库与技能从导航里撤掉 —— 它们本来就是把同一个
  // 行业包的数据切两份看，现在统一由「行业包」面板承载。
  check("设置导航：返回项 → 列表恒定 16px，三项全在且无隐藏项（搜索框已删；packgen 不占导航）",
    Math.abs(stgNavGeo.返回项到列表 - 16) <= 0.6
    && stgNavGeo.列表内上边距 === "0px"
    && stgNavGeo.导航项数 === 3 && stgNavGeo.被隐藏的项 === 0,
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
    // 三列化后「内容宽」不再是 pane 整体内宽，而是 **pane-detail 的内宽**
    // （pane-list + gap + pane-detail 才是真正的列）。
    // ⚠ 规范 v1 试点给 .page-card 加了**左右**内边距（发丝边卡片必须有留白，
    //   否则文字贴边）。原来"padding 只有上下、左右不缩"的前提不再成立，
    //   所以这里从"字段真正的容器"量：卡片内容宽 = 卡片宽 − 左右 padding。
    //   判据强度没变（仍是 ±0.6 的精确填满），只是参照物从列宽改成了它的子容器宽。
    const detail = pane.querySelector('.pane-detail');
    const cardEl = pane.querySelector('.page-card:not(.hidden)');
    const inner = +(cardEl
      ? cardEl.getBoundingClientRect().width
        - parseFloat(getComputedStyle(cardEl).paddingLeft)
        - parseFloat(getComputedStyle(cardEl).paddingRight)
      : detail.getBoundingClientRect().width).toFixed(2);
    // 生成偏好三列化后，「生成参数」「进阶」「行业包」三个分组各自藏在独立
    // .page-card 里，默认只显示一个。原来的断言靠默认可见的字段测全，
    // 现在要**逐个切换分组、把每个分组的字段都量一遍**，否则 hidden 卡片里的字段
    // 都是 0，断言就会假绿。
    const secBtns = [...document.querySelectorAll('#gen-sec-list .pl-item')];
    const allFrontKids = [];
    const allCtrls = [];
    let segW = null;
    let packRowW = null;
    secBtns.forEach(b => {
      b.click();
      const sec = b.dataset.sec;
      // ⚠ 同一个 click 在同一个脚本里不会同步触发 reflow 之外的副作用，
      // 但 .hidden class 切换是同步的 —— 这里读 getBoundingClientRect 已经反映。
      if (sec === 'param') {
        const front = document.getElementById('param-front');
        [...front.children].forEach(e => allFrontKids.push(
          { t: (e.textContent || '').trim().slice(0, 6), w: +e.getBoundingClientRect().width.toFixed(2) }));
      }
      if (sec === 'pack') {
        const packRow = pane.querySelector('.pack-row');
        packRowW = packRow ? +packRow.getBoundingClientRect().width.toFixed(2) : null;
      }
      if (sec === 'adv') {
        // 生成方式改成卡片式单选（.choice-cards，2026-09-17）——
        // 原来的 .segmented 胶囊已删。这里量的是「整组卡片」的宽度。
        const seg = pane.querySelector('.choice-cards');
        segW = seg ? +seg.getBoundingClientRect().width.toFixed(2) : null;
      }
      // 控件：所有 select / textarea / 自绘 .select-btn
      // ⚠ 排除 .pack-row 里的控件：那个 select 与「详情/新建」按钮并排共享整行，
      // select 本身不占满整行是有意为之（与按钮平分），不是「半宽孤儿」bug。
      // 排除隐藏的（hidden 父级、.native-hidden）。
      const packRow = pane.querySelector('.pack-row');
      [...pane.querySelectorAll('.select-btn, select, textarea')]
        .filter(e => e.offsetParent !== null)               // 排除 display:none 父级里的
        .filter(e => !e.classList.contains('native-hidden'))
        .filter(e => !packRow || !packRow.contains(e))      // pack-row 内的不算
        .forEach(e => allCtrls.push(
          { t: (e.textContent || e.id || e.tagName).trim().slice(0, 5),
            w: +e.getBoundingClientRect().width.toFixed(2) }));
    });
    // 测完切回默认（行业包），与 UI 一致
    secBtns.find(b => b.dataset.sec === 'pack').click();
    return { 内容宽: inner, 参数组子项: allFrontKids, 控件: allCtrls,
             分段: segW, 行业包行: packRowW };`);
  check("「生成偏好」各分组的字段都占满内容宽（不留半宽孤儿，如曾经 328px 的「结尾引导」）",
    genWidths.参数组子项.length >= 1                                  // 非空：防假绿
    && genWidths.参数组子项.some(c => /结尾引导/.test(c.t))            // 确实测到了那个字段
    && genWidths.参数组子项.every(c => Math.abs(c.w - genWidths.内容宽) <= 0.6)
    && genWidths.控件.every(c => Math.abs(c.w - genWidths.内容宽) <= 0.6)
    && (genWidths.分段 === null || Math.abs(genWidths.分段 - genWidths.内容宽) <= 0.6)
    && (genWidths.行业包行 === null || Math.abs(genWidths.行业包行 - genWidths.内容宽) <= 0.6),
    JSON.stringify(genWidths));

  // 「生成偏好」是三列（菜单 / 分组 / 详情）：中列列出 pack / param / adv 三块，
  // 点哪个右列就显示哪个。判据：中列 ≥3 个条目 + 中列选中态 ↔ 右列可见卡片
  // 一一对应（**两态都读**——只读一种会漏掉「切了分组但右列没动」那种静默 bug）。
  const gen3 = await evalIn(`var btns = [...document.querySelectorAll('#gen-sec-list .pl-item')];
    var cards = [...document.querySelectorAll('#gen-sec-detail .page-card[data-sec]')];
    var on = btns.filter(function(b){ return b.classList.contains('on'); }).map(function(b){ return b.dataset.sec; });
    var vis = cards.filter(function(c){ return !c.classList.contains('hidden'); }).map(function(c){ return c.dataset.sec; });
    var list = document.querySelector('#gen-sec-list');
    var detail = document.querySelector('#gen-sec-detail');
    var lr = list.getBoundingClientRect();
    var dr = detail.getBoundingClientRect();
    return { btnCount: btns.length, cardCount: cards.length,
             on: on, visible: vis,
             listW: Math.round(lr.width), detailW: Math.round(dr.width),
             sideBySide: Math.round(dr.left - lr.right) };`);
  check("生成偏好是中列分组 + 右列详情（三列骨架生效，且选中态与可见卡片一致）",
    gen3.btnCount === 3 && gen3.cardCount === 3
      && gen3.listW === 258 && gen3.detailW > 600 && gen3.sideBySide >= 0 && gen3.sideBySide < 50
      && gen3.on.length === 1 && gen3.visible.length === 1 && gen3.on[0] === gen3.visible[0],
    JSON.stringify(gen3));

  // 2026-09-17（用户报「生成参数和进阶上面怎么有两个分割线？和其他设置页面不统一」）：
  // `.page-card` 的 border-top 是「堆叠卡片之间的分隔线」语义，而生成偏好右列的
  // 三张卡片是**互斥切换**的（同时只有一张可见）—— 这个语义不成立，可见的那张
  // 平白多一条线 + 20px 顶部内边距。`:first-of-type` 只放过 pack（它认不出 .hidden，
  // 隐藏的卡片仍占 DOM 顺序），所以 param / adv 两条线一直挂着。
  // 判据：三张卡片的 border-top 与 padding-top 必须与 llm 面板的卡片一致（都是 0）
  // —— 比"绝对值 0"更贴用户那句"和其他设置页面不统一"：拿同角色面板当基线。
  const cardTop = await evalIn(`window.__ts.setPane('gen');
    function topOf(sel){
      var c = document.querySelector(sel);
      if (!c) return null;
      var cs = getComputedStyle(c);
      return { borderTop: parseFloat(cs.borderTopWidth) || 0,
               padTop: parseFloat(cs.paddingTop) || 0 };
    }
    return { gen: [...document.querySelectorAll('#gen-sec-detail > .page-card')].map(function(c){
               var cs = getComputedStyle(c);
               return { sec: c.dataset.sec,
                        borderTop: parseFloat(cs.borderTopWidth) || 0,
                        padTop: parseFloat(cs.paddingTop) || 0 };
             }),
             llm: topOf('#pane-llm .pane-detail > .page-card') };`);
  check("生成偏好右列三张卡片顶部都没有分割线（与 llm 面板的卡片一致）",
    cardTop.gen.length === 3
      && cardTop.gen.every(c => c.borderTop === cardTop.llm.borderTop
                             && c.padTop === cardTop.llm.padTop),
    JSON.stringify(cardTop));

  // 2026-09-17（用户报「右列高度行为不一致」）：三个三列面板的右列都要
  // 撑满「除 .stg-pane 上下边距外的全部可见高度」—— 不能有的撑满有的不撑
  // （原来只有行业包撑满，gen / llm 的右列是内容多高就多高，下方一大片空白）。
  // 判据：**右列容器**（.pane-detail）的底边贴近 .stg-main 的可见底边
  // （差 ≤ 48px = .stg-pane 的 padding-bottom 32px + 余量）。
  // ⚠ 量容器而不是量首块：模型接口右列在首块下面还有折叠区 + 状态行，
  // 量首块会把那两行算成"没撑满"（那是正常布局，不是缺陷）。
  const detailFill = await evalIn(`return (function(){
    var out = {};
    ['gen','packinfo','llm'].forEach(function(p){
      window.__ts.setPane(p);
      var pane = document.getElementById('pane-' + p);
      var detail = pane.querySelector('.pane-detail');
      var main = document.querySelector('#settings-screen .stg-main');
      if (!detail || !main) { out[p] = null; return; }
      var db = detail.getBoundingClientRect(), mb = main.getBoundingClientRect();
      out[p] = { gapToBottom: Math.round(mb.bottom - db.bottom),
                 detailH: Math.round(db.height) };
    });
    window.__ts.setPane('gen');
    return out;
  })()`);
  // ⚠ 判据只查「**不许在下方留死白**」（gapToBottom <= 48），不查「右列底边必须
  //   落在视口内」。原来还带一条 gapToBottom >= 0，等于把「内容比视口高」判成
  //   缺陷 —— 而撑满可见高度本来就是**下限**不是上限：模型接口展开「高级配置」
  //   后右列就该长过视口、交给 .stg-main 滚（2026-09-20 用户报底部状态行被遮挡，
  //   根因正是旧写法把列钳死、内容从盒子底下漏出去）。
  //   「长出去的部分必须够得着」由下面那条断言直接量，不在这里含糊带过。
  check("三个三列面板的右列都撑满可见高度（内容短时下方不留死白）",
    ['gen','packinfo','llm'].every(p => detailFill[p]
      && detailFill[p].gapToBottom <= 48),
    JSON.stringify(detailFill));

  // 内容比视口高的面板（模型接口展开高级配置）：滚到底时最后一块要**完整可见**
  // 并且离底边还留有下边距。修复前右列被钳成视口高，内容从盒子底下漏出去，
  // 漏出去的部分不计进 scrollHeight —— 滚到底状态行正好压在底边上，
  // .stg-pane 那 32px 下边距永远够不着，用户看到的就是「被遮挡」。
  const bottomReach = await evalIn(`return (function(){
    var main = document.querySelector('#settings-screen .stg-main');
    window.__ts.setPane('llm');
    document.getElementById('st-adv').open = true;
    var pane = document.getElementById('pane-llm');
    var detail = pane.querySelector('.pane-detail');
    var kids = Array.prototype.filter.call(detail.children, function(c){
      return !c.classList.contains('hidden'); });
    var last = kids[kids.length - 1];
    main.scrollTop = main.scrollHeight;
    var mb = main.getBoundingClientRect(), lb = last.getBoundingClientRect();
    var out = { id: last.id || last.className.split(' ')[0],
      text: (last.textContent || '').trim().slice(0, 12),
      below: Math.round(lb.bottom - mb.bottom),
      clipped: lb.bottom > mb.bottom + 1 };
    window.__ts.setPane('gen');
    return out;
  })()`);
  check("展开高级配置后滚到底：最后一行完整可见且离底边留有不小于 16px 的下边距",
    bottomReach.clipped === false && bottomReach.below <= -16,
    JSON.stringify(bottomReach));

  // 2026-09-17（用户报「headbar 按钮不齐」）：两个「列表型」面板的 headbar
  // 同构 —— 次级动作 ghost bordered（刷新）+ 主操作 primary slim（新建 / 添加）。
  // 原来只有行业包有刷新、模型接口只有一个主按钮。
  const headbar = await evalIn(`return (function(){
    var out = {};
    ['packinfo','llm'].forEach(function(p){
      window.__ts.setPane(p);
      var box = document.getElementById('pane-' + p).querySelector('.page-head-actions');
      if (!box) { out[p] = null; return; }
      var bs = [].slice.call(box.querySelectorAll('button'));
      out[p] = {
        n: bs.length,
        kinds: bs.map(function(b){ return b.classList.contains('primary') ? 'primary' : 'ghost'; }),
        ids: bs.map(function(b){ return b.id; }),
      };
    });
    window.__ts.setPane('gen');
    return out;
  })()`);
  check("两个列表型面板的 headbar 同构（刷新 ghost + 主操作 primary）",
    ['packinfo','llm'].every(p => headbar[p]
      && headbar[p].n === 2 && headbar[p].kinds.join() === 'ghost,primary'),
    JSON.stringify(headbar));

  // 2026-09-17（用户报「按钮大小，文字大小等等」不统一）：
  // headbar / 底部操作区的按钮原来混用 —— `.ghost` 走全局 30px + 继承 14px 字号，
  // `.primary.slim` 是 28px / 13px，并排时底边差 2px、字差 1px。
  // 判据取「彼此相等」而不是某个绝对值（改成 30px 也一样是统一）。
  const btnUniform = await evalIn(`return (function(){
    var out = [];
    ['gen','packinfo','llm','packgen'].forEach(function(p){
      window.__ts.setPane(p);
      var pane = document.getElementById('pane-' + p);
      [].slice.call(pane.querySelectorAll(
        '.page-actions button, .page-head-actions button, .pg-empty-actions button'))
        .forEach(function(b){
          var r = b.getBoundingClientRect();
          if (r.height > 0) out.push({ p: p, id: b.id, h: Math.round(r.height),
                                       fs: getComputedStyle(b).fontSize });
        });
    });
    window.__ts.setPane('gen');
    return out;
  })()`);
  check("设置页的操作按钮统一高度与字号（不再 ghost 30px / primary slim 28px 混用）",
    btnUniform.length >= 4
      && new Set(btnUniform.map(b => b.h)).size === 1
      && new Set(btnUniform.map(b => b.fs)).size === 1,
    JSON.stringify(btnUniform));

  // 2026-09-17：行业包右列首块改用 .page-card（与 gen / llm 同结构）——
  // 原来套的是 `.page-head`（那是**面板顶部**的 headbar 结构，含
  // .page-head-actions），用在右列里是错位复用，且间距节奏
  // （margin-bottom 20px）与 .page-card 的 gap 16px 不同。
  const piCard = await evalIn(`window.__ts.setPane('packinfo');
    var d = document.querySelector('#pane-packinfo .pane-detail');
    return { firstTag: d.firstElementChild.tagName,
             firstClass: d.firstElementChild.className,
             strayPageHead: !!d.querySelector(':scope > .page-head') };`);
  check("行业包右列首块是 .page-card（不再错位复用面板顶部的 .page-head）",
    piCard.firstTag === "DIV" && /page-card/.test(piCard.firstClass)
      && piCard.strayPageHead === false,
    JSON.stringify(piCard));
  await evalIn(`window.__ts.setPane('gen'); return true;`);

  // 2026-09-17：行业包分组的「详情 / 新建」是**两个纯按钮**，不该套 `.row`
  // —— `.row > :first-child { flex: 1 }` 是给「一个主控件 + 若干按钮」设计的，
  // 套上去会把「详情」拉满整行（截图里横跨 700px）。用 .btn-row。
  const packBtns = await evalIn(`window.__ts.setPane('gen');
    [...document.querySelectorAll('#gen-sec-list .pl-item')]
      .find(function(b){ return b.dataset.sec === 'pack'; }).click();
    var card = document.querySelector('#pane-gen .page-card[data-sec="pack"]');
    var detail = card.parentElement;
    var inner = detail.getBoundingClientRect().width;
    var bs = ['btn-packinfo','btn-newpack'].map(function(id){
      var b = document.getElementById(id);
      var r = b.getBoundingClientRect();
      return { id: id, w: Math.round(r.width), inRow: !!b.closest('.row') };
    });
    return { inner: Math.round(inner), btns: bs };`);
  check("行业包分组的「详情 / 新建」按内容取宽、不被拉满整行（按钮行不套 .row）",
    packBtns.btns.length === 2
      && packBtns.btns.every(b => b.w < packBtns.inner / 2 && b.inRow === false),
    JSON.stringify(packBtns));

  // 2026-09-17：「进阶」→「生成方式与输出」（用户问「进阶有用吗？需要优化或者
  // 合并吗？」）—— 4 项都在用（后端 pipeline.py 的 voice/format/mode 白名单 +
  // knowledge.py 的 private_facts），**一项都不能删**；问题只在**名字太泛**：
  // 听起来像"高级 / 不常用"，而「输出内容」「补充资料」其实常用。
  // 判据：中列条目与右列卡片标题必须同名，且两个旧名都不残留在可见文本里。
  // 「生成方式与输出」在 2026-09-19 也变成旧名了 —— 分步确认移除后这一组里
  // 不再有"生成方式"，只剩 输出内容 / 文案人味 / 补充资料，故改名「输出与风格」。
  const advName = await evalIn(`window.__ts.setPane('gen');
    const nav = [...document.querySelectorAll('#gen-sec-list .pl-item')]
      .find(function(b){ return b.dataset.sec === 'adv'; });
    return { navTitle: nav.querySelector('.pl-t').textContent.trim(),
             navSub: nav.querySelector('.pl-s').textContent.trim(),
             cardTitle: document.querySelector(
               '#gen-sec-detail .page-card[data-sec="adv"] .card-title').textContent.trim(),
             staleName: document.body.innerText.indexOf('进阶') >= 0,
             staleModeName: document.body.innerText.indexOf('生成方式与输出') >= 0 };`);
  check("「输出与风格」命名同步（中列条目与右列卡片一致，旧名不残留）",
    advName.navTitle === "输出与风格" && advName.cardTitle === "输出与风格"
      && advName.navSub.length > 0 && advName.staleName === false
      && advName.staleModeName === false,
    JSON.stringify(advName));

  // 2026-09-17（任务 1）：行业包分组现在是 .block-inner 结构（与「生成参数」「生成方式与输出」同族），
  // 之前是 .fg.pack-row 单行布局（select + 详情 + 新建），高度 ~50px、右栏大片空。
  // 判据：行业包卡片内 ≥ 2 个 .block-inner，每个都包含 .lbl + 输入控件/按钮，
  // 整卡有 .card-title（与生成参数、生成方式与输出的 h3.card-title 一致）。
  const packSec = await evalIn(`window.__ts.setPane('gen');
    const card = document.querySelector('#pane-gen .page-card[data-sec="pack"]');
    const secBtns = [...document.querySelectorAll('#gen-sec-list .pl-item')];
    secBtns.find(b => b.dataset.sec === 'pack').click();
    const blocks = [...card.querySelectorAll('.block-inner')];
    const fields = blocks.map(b => ({
      lbl: b.querySelector('.lbl')?.textContent.trim().slice(0, 12) || '',
      hasControl: !!(b.querySelector('select, button, input, textarea')),
      hasRow: !!b.querySelector('.row'),
    }));
    return {
      cardTitle: card.querySelector('.card-title')?.textContent.trim().slice(0, 12) || '',
      // 旧 .fg.pack-row 单行布局：只有一个 .row 包 select + 两个 button，没有 .block-inner
      hasLegacyPackRow: !!card.querySelector('.pack-row'),
      blockCount: blocks.length,
      fields,
    };`);
  check("生成偏好「行业包」分组是 .block-inner 多行结构（与「生成参数」「生成方式与输出」同族）",
    packSec.cardTitle === "行业包" && packSec.blockCount >= 2
      // 原判据要求每个 block 都有非空 .lbl。现在「操作」这个空词标签被删了
      // （<label> 连 for 都没有，按钮自己已说明一切），按钮行没有标签是合理的。
      // 真正要守的是结构：≥2 个 block、每个都装着控件、且没退回旧的单行 .pack-row。
      && packSec.fields.every(f => f.hasControl)
      && packSec.fields.some(f => f.lbl)          // 至少有一个字段是带标签的（防整组都没标签）
      && !packSec.hasLegacyPackRow,
    JSON.stringify(packSec));

  // 2026-09-17（任务 7）：行业包分组改造后整组高度应与窗口适配，
  // 不能再像之前那样 ~50px 一行、右栏空一大片。
  // 判据：行业包卡片可见时高度 ≥ pane-detail 内容宽 × 0.55（细高比 ≤ 0.55），
  // 即至少有"半屏高度"的内容（行业包分组 = select 行 + 操作按钮行 + 描述 hint 行）。
  // 注意高度只在 pane-detail 实际渲染时才有意义 —— 默认它**可见**（pl-item.on）。
  const packH = await evalIn(`window.__ts.setPane('gen');
    const secBtns = [...document.querySelectorAll('#gen-sec-list .pl-item')];
    secBtns.find(b => b.dataset.sec === 'pack').click();
    const card = document.querySelector('#pane-gen .page-card[data-sec="pack"]');
    const detail = card.parentElement;
    const r = card.getBoundingClientRect();
    return { h: Math.round(r.height),
             w: Math.round(r.width),
             detailH: Math.round(detail.getBoundingClientRect().height) };`);
  // 内容宽 ~700 → 半屏比例下高度至少 ~200px（细高比 ≤ 3.5）。
  check("「行业包」分组高度适配窗口（不再是 ~50px 一行）",
    packH.h >= 200 && (packH.w / packH.h) <= 3.5,
    JSON.stringify(packH));

  // 切到「生成参数」分组，验证任务 2（生成参数卡片展示全量参数，含 TOOLBAR_KEYS）。
  // 期望：所有 pack.params 里 options.length>0 的 key 都在 #param-front 里。
  // TOOLBAR_KEYS 是 verify.js 的规格副本（也是 UI 的"工具条胶囊"清单）；
  // evalIn 是另一个上下文，没法直接读这个常量，所以走 window.__tsMeta 同时
  // 接收（与 expectFor 同源、不冗余抄一份）。
  const fullParams = await evalIn(`window.__ts.setPane('gen');
    const secBtns = [...document.querySelectorAll('#gen-sec-list .pl-item')];
    secBtns.find(b => b.dataset.sec === 'param').click();
    const meta = window.__tsMeta;
    const active = meta.packs.find(x => x.name === document.getElementById('pack').value) || meta.packs[0];
    const p = (active || {}).params || {};
    const keys = Object.keys(p).filter(k => p[k]?.options?.length);
    const frontKeys = [...document.getElementById('param-front').querySelectorAll('select')]
                       .map(s => s.dataset.key || s.id.replace(/^p-/, ''));
    // P1-1 唯一性契约：param-front 的 select **不得**带 p- 前缀 id ——
    // 工具条胶囊（快速参数）已用 id="p-<key>"；文档级 id 唯一，
    // 设置页参数组是同台常驻 DOM 的另一个视图（openSettings 只切 settings-screen 的
    // hidden，不卸载 #view-chat），再挂一个同 id 的 select，getElementById 会取到
    // 错误的那一个 → 「在设置里改了参数、生成的却是工具条值」的静默不一致。
    // 判据盯「param-front 内有没有带 p- id 的 select」：实现改回带 id 就红。
    const frontIds = [...document.getElementById('param-front').querySelectorAll('select')]
                      .map(s => s.id).filter(x => x.indexOf('p-') === 0);
    // 工具条胶囊（来自 meta.active_toolbar_keys，st-bubbles 把 ui.js 的 TOOLBAR_KEYS 列表
    // 写到 meta —— 留作页面的只读快照，不依赖实现常量）。若 meta 没这字段，回退到硬编码。
    const TB = (meta.active_toolbar_keys || ["segment","audience","duration","platform"]);
    return { expectedKeys: keys.sort(),
             frontKeys: frontKeys.sort(),
             frontIds: frontIds,
             toolbarInFront: TB.filter(k => frontKeys.includes(k)) };`);
  // 工具条参数清单的**规格副本**：这里有意不复用 ui.js 的常量 ——
  // 复用就成了同义反复（实现改错、断言跟着一起错）。代价是调整分层时
  // 要同步改这一行，所以它必须显式写着「这是规格」。
  // 提到 main() 顶部作用域（原本在下面的 expectFor 块里）—— 否则
  // 「生成参数卡片展示全量参数」断言会触发 const 的 TDZ。
  const TOOLBAR_KEYS = ["segment", "audience", "duration", "platform"];
  check("「生成参数」卡片展示全量参数（含工具条的 segment/audience/duration/platform）",
    JSON.stringify(fullParams.expectedKeys) === JSON.stringify(fullParams.frontKeys)
      && fullParams.toolbarInFront.length === TOOLBAR_KEYS.length,
    JSON.stringify(fullParams));
  // P1-1：param-front 的 select 不带 `p-` 前缀 id（与工具条胶囊 id 冲突会
  // 让 getElementById 取错；两视图同台常驻 DOM，不是「互斥显示」能兜住的）。
  check("param-front 参数组不带 p- 前缀 id（与工具条胶囊不撞 id）",
    fullParams.frontIds.length === 0,
    JSON.stringify(fullParams));
  // 设置页「生成参数」卡改了要**真的进请求**。
  // 这条是补的洞：上面两条只验 DOM 结构（全量参数、不撞 id），于是出现过
  // 「卡片渲染正常、点了有反应、228 条断言全绿，但 collectParams() 永远送 null」——
  // 因为 getParam 只认工具条胶囊的 `#p-<key>`，而 style/persona/cta 根本没有胶囊。
  // 判据必须落到真值上：改卡片里的 select → collectParams() 跟着变。
  const paramReal = await evalIn(`window.__ts.setPane('gen');
    const secBtns = [...document.querySelectorAll('#gen-sec-list .pl-item')];
    secBtns.find(b => b.dataset.sec === 'param').click();
    const front = document.getElementById('param-front');
    const pick = k => front.querySelector('select[data-key="' + k + '"]');
    const other = s => [...s.options].map(o => o.value).find(v => v !== s.value);
    // ① 只在设置页出现、工具条没有胶囊的参数
    const solo = ['style','persona','cta'].map(pick).find(s => !!s);
    const soloKey = solo.dataset.key, soloFrom = solo.value, soloTo = other(solo);
    solo.value = soloTo; solo.dispatchEvent(new Event('change', {bubbles:true}));
    const sentSolo = window.__ts.collectParams()[soloKey];
    // ② 两处都有的参数：设置页改 → 工具条胶囊与真值都要跟着走
    const dur = pick('duration'), durFrom = dur.value, durTo = other(dur);
    dur.value = durTo; dur.dispatchEvent(new Event('change', {bubbles:true}));
    // ⚠ 这里必须把**值**取下来，不能只存节点引用留给 return 去读：
    // 下面还要还原，return 求值时读到的会是还原后的旧值 —— 我自己就这么被骗过一次
    // （断言报"胶囊没同步"，实际是断言自己在还原之后才读）。
    const pillScoped = document.querySelector('#quick-params #p-duration');
    const pillTo = pillScoped ? pillScoped.value : null;
    const pillCount = document.querySelectorAll('#p-duration').length;
    const sentDur = window.__ts.collectParams().duration;
    // 还原，别污染后面的断言
    solo.value = soloFrom; solo.dispatchEvent(new Event('change', {bubbles:true}));
    dur.value = durFrom; dur.dispatchEvent(new Event('change', {bubbles:true}));
    return { soloKey, soloTo, sentSolo, durTo, durFrom, pillTo, pillCount, sentDur,
             backTo: window.__ts.collectParams().duration };`);
  check("设置页「生成参数」改动真的进请求（无胶囊的参数也能生效 + 双向同步工具条）",
    paramReal.sentSolo === paramReal.soloTo
      && paramReal.sentDur === Number(paramReal.durTo)
      && paramReal.pillTo === paramReal.durTo
      && paramReal.pillCount === 1
      // 还原也要验：不还原的话这条会污染后面所有断言的基线
      && paramReal.backTo === Number(paramReal.durFrom),
    JSON.stringify(paramReal));

  // 切回默认（行业包），与 UI 一致
  await evalIn(`window.__ts.setPane('gen');
    [...document.querySelectorAll('#gen-sec-list .pl-item')]
      .find(b => b.dataset.sec === 'pack').click(); return true;`);

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
  //   参数组 = 全 keys 数 —— 任务 2 后「生成参数」卡片要展示全量（含 TOOLBAR_KEYS）。
  // 工具条参数清单的**规格副本**：这里有意不复用 ui.js 的常量 ——
  // 复用就成了同义反复（实现改错、断言跟着一起错）。代价是调整分层时
  // 要同步改这一行，所以它必须显式写着「这是规格」。
  // 2026-09-17：上面那条全量断言先一步需要 TOOLBAR_KEYS，所以把 const 提到上面。
  const expectFor = name => {
    const p = (META.packs.find(x => x.name === name) || {}).params || {};
    const keys = Object.keys(p).filter(k => p[k] && p[k].options && p[k].options.length);
    return { 胶囊: keys.filter(k => TOOLBAR_KEYS.includes(k)).length,
             参数组: keys.length };   // 2026-09-17（任务 2）：不排除 TOOLBAR_KEYS
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
  check("清空搜索后恢复全部会话", cleared.rows === 6 && cleared.val === "",
    JSON.stringify(cleared));

  const sessGeo = await evalIn(GEO(".sess-search"));
  check("会话搜索：图标在框内、input 无自带描边", geoOK(sessGeo), JSON.stringify(sessGeo));

  // ── 会话列表的分天分组与折叠 ────────────────────────────────────
  // 用户 2026-09-19 原话：「历史记录现在无法折叠，全部记录平铺了，有点太多了」。
  // 根因不是「没分组」，是 **dayKey 只有四档（今天/昨天/本周/更早），
  // 一周以前的全并进「更早」** —— 真实索引 78 条全在同一周以前，整列只剩
  // 一个标签「更早 78」，78 行之间再无分隔。
  //
  // 桩数据专门跨 4 天（今天 / 昨天 / 3 天前 / 10 天前），让三个分支都被走到：
  //   今天、昨天      → 相对词
  //   3 天前          → label = M月D日（星期不进标签，挪到悬浮提示）
  //   10 天前         → label = M月D日（**这条是旧实现会翻车的地方**：
  //                     旧口径下它和「3 天前」会被并成同一个「更早」组）
  const grouping = await evalIn(`const labels = [...document.querySelectorAll('#session-list .group-lbl')]
      .map(n => ({ label: n.querySelector('.gl-t').textContent,
                   count: +n.querySelector('.count').textContent,
                   id: n.dataset.gid }));
    return { labels: labels, rows: document.querySelectorAll('#session-list .sess-item').length };`);

  check("列表按天分组：跨天的记录被拆成多组（不是全塞进一个「更早」）",
    grouping.labels.length === 5, JSON.stringify(grouping.labels));

  // 日期必须写成**中文读法**：`09-17` 这种两段裸数字后面紧跟计数
  // （`09-17 26`），用户读不出哪个是日期、哪个是条数（原话「区分不出来
  // 日期和后面的数字」）。`9月17日` 由「日」字收尾，怎么轻都粘不上去。
  // 旧判据要求的是 `^[0-9]{2}-[0-9]{2}$` —— 那正是这轮要改掉的形状，
  // 所以这里不是放宽：从「必须是 MM-DD」换成「必须是 M月D日 且不是粗桶」。
  check("分组标签用相对词 + 中文日期（不是「本周」「更早」粗桶，也不是两段裸数字）",
    grouping.labels[0].label === "今天" && grouping.labels[1].label === "昨天"
      && grouping.labels.slice(2).every(g => /^[0-9]{1,2}月[0-9]{1,2}日$/.test(g.label))
      && !grouping.labels.some(g => /本周|更早/.test(g.label)),
    JSON.stringify(grouping.labels.map(g => g.label)));

  // ⚠ 这条是**旧实现会红**的关键：3 天前与 10 天前必须落在**两个不同的组**。
  // 只断言「组数变多了」是不够的 —— 把 dayKey 的四档原样保留、只加折叠，
  // 组数照样是 1，但那不叫修好。这里直接盯「两个不同天是否被分开」。
  check("相隔一周以上的两天不会被并进同一个分组（旧「更早」的病灶）",
    grouping.labels.length === 5
      && grouping.labels[3].count === 1 && grouping.labels[4].count === 1
      && grouping.labels[3].id !== grouping.labels[4].id,
    JSON.stringify(grouping.labels));

  // 组内计数之和 == 总行数：防止「分组分对了但漏画了行」。
  check("各分组计数之和等于总行数（没有记录漏画）",
    grouping.labels.reduce((a, g) => a + g.count, 0) === grouping.rows && grouping.rows === 6,
    `sum=${grouping.labels.reduce((a, g) => a + g.count, 0)} rows=${grouping.rows}`);

  // ── 折叠 ──
  // 折叠状态先清干净再测（否则上一次跑留在 localStorage 里的状态会污染基线）。
  await evalIn(`localStorage.removeItem('ts.sess.folded'); return true;`);
  const foldBefore = await evalIn(`return document.querySelectorAll('#session-list .sess-item').length;`);

  // 点第一个分组标签（今天）→ 该组的行应当消失、标签还在、aria 转 false。
  // ⚠ evalIn 的包装是 `(() => { ... })()`，**不是 async** —— 想在里面 await
  //   就必须自己再套一层 `(async () => { ... })()` 并用 return 交出去，否则报
  //   「await is only valid in async functions」。
  const folded = await evalIn(`return (async () => {
    const g = document.querySelector('#session-list .group-lbl');
    const label = g.querySelector('.gl-t').textContent;
    const gid = g.dataset.gid;
    g.click();
    await new Promise(r => setTimeout(r, 60));
    return { label: label, gid: gid,
      rows: document.querySelectorAll('#session-list .sess-item').length,
      groups: document.querySelectorAll('#session-list .group-lbl').length,
      aria: document.querySelector('#session-list .group-lbl').getAttribute('aria-expanded'),
      cls: document.querySelector('#session-list .group-lbl').classList.contains('folded'),
      stored: localStorage.getItem('ts.sess.folded') };
  })();`);

  // 组数不写死（桩一变它就漂）：用「折叠前后组数不变」守「折的是行不是标签」，
  // 用「恰好少了今天那组的条数」守「折对了组」。
  const todayCount = grouping.labels[0].count;
  check("点分组标签真的折起来：该组的行消失，标签本身还在",
    folded.rows === foldBefore - todayCount && folded.groups === grouping.labels.length,
    `折前=${foldBefore} 折后=${folded.rows} 该组条数=${todayCount} groups=${folded.groups}`);

  check("折叠状态同步给读屏（aria-expanded 转 false）且有视觉类 .folded",
    folded.aria === "false" && folded.cls === true, JSON.stringify(folded));

  // ⚠ 存的键必须是**分组 id（日期）**而不是 label。
  // 用 label 当键的话，「今天」这组明天就变成「昨天」，折叠状态会错位到别的组上。
  check("折叠状态写进 localStorage，且键是日期 id 不是 label",
    !!folded.stored && folded.stored.indexOf(folded.gid) >= 0
      && !/今天|昨天/.test(folded.stored),
    `stored=${folded.stored} gid=${folded.gid}`);

  // 再点一次 → 展开还原（防止「只能折不能展」）。
  const unfolded = await evalIn(`return (async () => {
    const g = document.querySelector('#session-list .group-lbl');
    g.click();
    await new Promise(r => setTimeout(r, 60));
    return { rows: document.querySelectorAll('#session-list .sess-item').length,
             aria: document.querySelector('#session-list .group-lbl').getAttribute('aria-expanded') };
  })();`);
  check("再点一次能展开还原（不是单向折叠）",
    unfolded.rows === foldBefore && unfolded.aria === "true", JSON.stringify(unfolded));

  // ⚠ 搜索时**忽略折叠**：用户在找东西，把结果藏在折起来的分组里是纯阻碍。
  await evalIn(`return (async () => {
    const g = document.querySelector('#session-list .group-lbl'); g.click();
    const b = document.getElementById('sess-search');
    b.value = '电梯'; b.dispatchEvent(new Event('input', {bubbles:true}));
    await new Promise(r => setTimeout(r, 80));
    return true;
  })();`);
  const searchIgnoresFold = await evalIn(`return {
    rows: document.querySelectorAll('#session-list .sess-item').length,
    folded: document.querySelectorAll('#session-list .group-lbl.folded').length };`);
  check("搜索时忽略折叠：折起的组里匹配到的记录照样显示",
    searchIgnoresFold.rows === 6 && searchIgnoresFold.folded === 0,
    JSON.stringify(searchIgnoresFold));

  // 分组标签必须是**真按钮**（键鼠两用），不能是加了个 onclick 的 div。
  // ⚠ 这条单列出来，是因为上面那三条「点击折叠」**抓不住这个退化**：
  //   把 button 换成 div 后如果补一句 addEventListener('click')，点击照样管用、
  //   aria 也能自己 setAttribute 上去 —— 三条断言全绿，但标签从此
  //   **Tab 不可达、Enter/Space 不响应、读屏读不出这是个控件**。
  //   所以直接查标签本身，而不是查它的行为。
  const lblA11y = await evalIn(`const g = document.querySelector('#session-list .group-lbl');
    return { tag: g.tagName, type: g.getAttribute('type'),
             tabbable: g.tabIndex >= 0 || g.tagName === 'BUTTON',
             expanded: g.getAttribute('aria-expanded') };`);
  check("分组标签是 <button>（Tab 可达、Enter/Space 可触发）而不是 div",
    lblA11y.tag === "BUTTON" && lblA11y.type === "button"
      && lblA11y.tabbable && lblA11y.expanded !== null,
    JSON.stringify(lblA11y));

  // 标签必须**靠左**：三角贴左内边距、文字紧随其后、计数跟在文字后面。
  // ⚠ 这条是「断言只测行为、漏掉视觉」的典型补丁 —— 我把标签从 <div> 改成
  //   <button> 之后，全局 `button { justify-content: center; padding: 0 13px }`
  //   把它接管了：点击、折叠、aria、localStorage **四条断言全绿**，
  //   但肉眼上整行是居中的（实测三角距左 80px、计数右边距 80px），
  //   看起来像浮在行中间的一行小字，完全不像分组标题。
  //   行为对 ≠ 长得对，所以这里单量几何。
  const lblAlign = await evalIn(`const g = document.querySelector('#session-list .group-lbl');
    const b = g.getBoundingClientRect();
    const a = g.querySelector('.gl-folder').getBoundingClientRect();
    const t = g.querySelector('.gl-t').getBoundingClientRect();
    const c = g.querySelector('.count').getBoundingClientRect();
    const cs = getComputedStyle(g);
    return { 图标左: Math.round(a.left - b.left), 文字左: Math.round(t.left - b.left),
             计数左: Math.round(c.left - b.left), 高: Math.round(b.height),
             内边距左: Math.round(parseFloat(cs.paddingLeft)), jc: cs.justifyContent };`);
  // 「三角贴自身的 padding-left」写成**与 padding 相等**，不写成某个绝对值：
  // 原来这里是 `三角左 <= 10`，那个 10 其实是「padding 8 + 一点余量」的魔数，
  // 左栏把内边距从 8 统一成 12 之后它就假红了。判据要守的是「贴左、不居中」，
  // 具体贴到哪由 token 说。
  check("分组标签内容靠左排（图标→文字→计数，不被 button 的居中规则接管）",
    lblAlign.jc === "flex-start"
      && lblAlign.图标左 === lblAlign.内边距左          // 图标就贴在自身内边距上
      && lblAlign.文字左 > lblAlign.图标左          // 文字在图标右边
      && lblAlign.计数左 > lblAlign.文字左,         // 计数在文字右边
    JSON.stringify(lblAlign));

  // 收尾：清搜索 + **把折叠状态复位**。
  // ⚠ 只 `localStorage.removeItem` 是不够的：`folded` 这个 Set 是在模块加载时
  //   从 localStorage 读进内存的，删了存储它也不会自己变空 —— 后面那些
  //   「按 data-id 取某一行」的断言会因为在场的组还是折着的而抓到 null。
  //   必须**再点一次**那个标签，让它真的展开（走的是用户的真实路径）。
  await evalIn(`return (async () => {
    document.getElementById('sess-search-clear').click();
    const g = document.querySelector('#session-list .group-lbl.folded');
    if (g) g.click();
    await new Promise(r => setTimeout(r, 80));
    return true;
  })();`);
  const afterFoldCleanup = await evalIn(`return {
    rows: document.querySelectorAll('#session-list .sess-item').length,
    folded: document.querySelectorAll('#session-list .group-lbl.folded').length,
    q: document.getElementById('sess-search').value };`);
  // 这条同时是后面所有「按 data-id 取行」断言的前置：列表必须是完整 5 条。
  check("测试收尾：搜索清空 + 折叠复位，列表回到完整 5 条",
    afterFoldCleanup.rows === 6 && afterFoldCleanup.folded === 0
      && afterFoldCleanup.q === "",
    JSON.stringify(afterFoldCleanup));
  await evalIn(`localStorage.removeItem('ts.sess.folded'); return true;`);

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
  // 2026-09-21 改 A：新建对话与搜索框同属「开始一段工作」的动作区，两个 16
  // 读成"三块等距、没有分组" —— 组内收到 12（--s3，8 实测太挤），组间保持 16（--s4）。
  // 断言守的是「组内 < 组间 且各自精确」。组内 12 = --s3、组间 16 = --s4。
  check("按钮→搜索 12 / 搜索→列表 16（动作区内收、组间保持 16）",
    headOrder.gapUp === 12 && headOrder.gapToLbl === 16,
    `距按钮=${headOrder.gapUp} 距首条记录=${headOrder.gapToLbl}`);
  // 搜索框到下方内容的间距必须**只有一个来源**，两态读同一个数：
  //   滚动态 = 滚动区容器上边界 - 搜索框底边（.left-top 的 padding-bottom）
  //   非滚动态 = 第一个分组标签顶边 - 搜索框底边
  // 回归的 bug：第一个分组标签自带 padding-top 12px，于是非滚动态读出
  // 8+12=20px 而滚动态只有 8px —— 上下滚一下间距就变了，读起来像布局在抖。
  check("搜索框下方间距统一 16px（滚动态与非滚动态一致）",
    headOrder.gapToScroll === headOrder.gapToLbl && headOrder.gapToScroll === 16,
    `滚动态=${headOrder.gapToScroll} 非滚动态=${headOrder.gapToLbl}`);
  // 悬浮胶囊的尺寸必须**处处相等**（用户抓的正是这个：「最上面那个鼠标悬浮态的
  // 背景胶囊尺寸都不对」）。根因是同一个盒子兼着两件事 —— 既画 hover 底色、
  // 又用上下 padding 撑分组间距，而 :first-child 为了「搜索框→列表」只有一个
  // 间距来源把 padding-top 归零，于是第一个分组 16 高、其余 28、会话行 32。
  // 现在：底色 = 32 的胶囊（与行同档），间距 = margin（盒子外）。
  // ⚠ 桩里只有 1 个分组，「处处相等」光量第一个是空判 —— 必须像下面这样
  //   克隆一个**非 first-child** 的标签插进行间来量（它拿到的是真实的
  //   margin-top 路径）。这条同时钉住三件事：胶囊等高、盒子里不许再有上下
  //   padding、以及墨迹在胶囊里居中（旧那条「字墨顶 = 盒顶 = 16」是给
  //   flex-start + line-height 魔数时代的写法配的，盒子等高之后基准回到
  //   胶囊本身：胶囊顶 = 容器顶 = 搜索框下 16，两态同一个数）。
  const pillGeo = await evalIn(`const sea = document.querySelector('.sess-search');
    const sc = document.querySelector('.left-scroll');
    const list = document.getElementById('session-list');
    const lbl = list.querySelector('.group-lbl');
    const row = list.querySelector('.sess-item');
    const clone = lbl.closest('.grp-row').cloneNode(true);
    row.after(clone);
    const cs = getComputedStyle(lbl);
    const c = document.createElement('canvas').getContext('2d');
    c.font = cs.fontWeight + ' ' + cs.fontSize + ' ' + cs.fontFamily;
    // 文字在 .gl-t 这个 span 里（标签改成折叠按钮后，文字不能再是裸文本节点 ——
    // 三角、文字、计数三者要各占一个 flex 项）。
    const glt = lbl.querySelector('.gl-t');
    const tn = [...glt.childNodes].find(n => n.nodeType === 3 && n.textContent.trim());
    const rg = document.createRange(); rg.selectNodeContents(tn);
    const m = c.measureText(tn.textContent.trim());
    const lb = lbl.getBoundingClientRect();
    const inkTop = rg.getBoundingClientRect().top
      + (m.fontBoundingBoxAscent - m.actualBoundingBoxAscent);
    const inkBot = inkTop + m.actualBoundingBoxAscent + m.actualBoundingBoxDescent;
    const cnt = lbl.querySelector('.count').getBoundingClientRect();
    const seaB = sea.getBoundingClientRect().bottom;
    const out = {
      胶囊高: Math.round(lb.height),
      克隆高: Math.round(clone.querySelector('.group-lbl').getBoundingClientRect().height),
      行高: Math.round(row.getBoundingClientRect().height),
      上下内边距: cs.paddingTop + '/' + cs.paddingBottom,
      胶囊顶: +(lb.top - seaB).toFixed(2),
      容器顶: +(sc.getBoundingClientRect().top - seaB).toFixed(2),
      墨上隙: +(inkTop - lb.top).toFixed(2), 墨下隙: +(lb.bottom - inkBot).toFixed(2),
      计数被裁: cnt.top < sc.getBoundingClientRect().top - 0.01 };
    clone.remove();
    return out;`);
  check("分组标签的悬浮胶囊与行等高、含非首个分组处处相等，且盒子里不再藏上下间距",
    pillGeo.胶囊高 === pillGeo.行高 && pillGeo.胶囊高 === pillGeo.克隆高
      && pillGeo.行高 === 32 && pillGeo.上下内边距 === "0px/0px",
    JSON.stringify(pillGeo));
  check("第一个分组胶囊顶 = 容器顶 = 搜索框下 16（两态同一基准），墨迹在胶囊内居中",
    pillGeo.胶囊顶 === 16 && pillGeo.容器顶 === 16
      && pillGeo.墨上隙 >= 6 && Math.abs(pillGeo.墨上隙 - pillGeo.墨下隙) <= 1.5
      && pillGeo.计数被裁 === false,
    JSON.stringify(pillGeo));

  // 「今天」与计数必须是**同一条基线**上的同一族字：同字号、同字重、同行高、同盒顶。
  // 演进：计数原本是灰底药丸，肉眼看到的是那个矩形，所以要拿**墨底**去对齐它 ——
  // 当时靠 `padding-top: 1px` 补偿（中文「今天」的墨底比基线低 1px，数字就在基线上）。
  // 现在计数是裸数字，矩形没了，可见的对齐关系回到基线本身；
  // 两者盒顶相同 + 度量相同 ⇒ 基线恒等，而那 1px 的**墨底差是字形自带的**，
  // 不该再用 padding 去抹（抹了会把数字的墨顶压低，变成另一种不齐）。
  // 所以这里量的是「同源」而不是「墨底重合」：盒顶差必须为 0，字号/字重/行高逐项相等，
  // 墨底差只允许是字形自带的那点（≤1.2px）—— 谁再把计数挪位就会红。
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
    const cnt = lbl.querySelector('.count');
    const glt = lbl.querySelector('.gl-t');
    // 文字在 .gl-t 里（标签改了折叠按钮，文字不再是裸文本节点）
    const tn = [...glt.childNodes].find(n => n.nodeType === 3 && n.textContent.trim());
    const pn = [...cnt.childNodes].find(n => n.nodeType === 3 && n.textContent.trim());
    const a = getComputedStyle(glt), b = getComputedStyle(cnt);
    return { 文字: tn.textContent.trim(), 计数: pn.textContent.trim(),
             盒顶差: +(cnt.getBoundingClientRect().top - glt.getBoundingClientRect().top).toFixed(2),
             字号同: a.fontSize === b.fontSize, 行高同: a.lineHeight === b.lineHeight,
             标签字重: +a.fontWeight, 计数字重: +b.fontWeight,
             背景: b.backgroundColor,
             文字墨底: +inkBottom(glt, tn).toFixed(2),
             计数墨底: +inkBottom(cnt, pn).toFixed(2) };`);
  // 计数与标签：同字号、同行高、同盒顶（= 同一条基线），**只有字重低一档**。
  // 原来这里断言的是「字号/字重/行高逐项相等」—— 那一版计数与标签长得一样重，
  // 用户读不出哪个是条数（「区分不出来日期和后面的数字」）。现在刻意让计数 400、
  // 标签 600，所以判据从「三项全等」改成「字号与行高等 + 字重必须不等且更低」：
  // 维度少了一个、多了一个，不是放宽。墨底仍要贴在同一条基线上（≤1.2px 是
  // 中文与数字自带的差别），谁再拿 padding 去挪字形就会红。
  check("计数与标签同基线（字号/行高/盒顶相等），但字重低一档、背景透明",
    inkPair.字号同 && inkPair.行高同 && inkPair.盒顶差 === 0
      && inkPair.标签字重 === 600 && inkPair.计数字重 === 400
      && Math.abs(inkPair.文字墨底 - inkPair.计数墨底) <= 1.2
      && /rgba?\([^)]*,\s*0\)$/.test(inkPair.背景),
    JSON.stringify(inkPair));

  // 分组标签的日期读法。⚠ 必须直接喂日期调用这个纯函数，不能只看 DOM：
  // 桩里的记录全落在今天/昨天两支，数字日期那一支永远渲染不出来 ——
  // 而「09-12 26」读不出哪个是计数，恰恰只在数字日期那一支出问题。
  const dayFmt = await evalIn(`const k = window.__ts.dayGroupKey;
    const at = (d) => { const x = new Date(); x.setDate(x.getDate() - d);
      x.setHours(12, 0, 0, 0); return k(x.toISOString()); };
    const fixed = (s) => k(s);
    return { 今天: at(0).label, 昨天: at(1).label, 三天前: at(3).label,
      六天前: at(6).label, 去年今日: fixed('2025-09-20T04:00:00').label,
      跨年id: [fixed('2025-12-31T04:00:00').id, fixed('2026-01-02T04:00:00').id],
      星期: at(3).weekday, 坏值: k('不是日期').label };`);
  check("分组日期用中文读法（「9月17日」而非「09-17」），计数才不会粘成一段数字",
    dayFmt.今天 === "今天" && dayFmt.昨天 === "昨天"
      && /^[0-9]{1,2}月[0-9]{1,2}日$/.test(dayFmt.三天前)
      && /^[0-9]{1,2}月[0-9]{1,2}日$/.test(dayFmt.六天前)
      && dayFmt.坏值 === "时间未知",
    JSON.stringify(dayFmt));
  // 星期不进标签（一行三段太挤），但必须还在 —— 挪进了悬浮提示（updateGroup）。
  // id 仍带年份且不参与展示：跨年时「12-31 / 01-02」按 MM-DD 排会反过来。
  check("星期从标签挪到提示但不丢失，排序 id 仍带年份",
    /^周[一二三四五六日]$/.test(dayFmt.星期) && !/周/.test(dayFmt.三天前)
      && /^[0-9]{4}-[0-9]{2}-[0-9]{2}$/.test(dayFmt.跨年id[0])
      && dayFmt.跨年id[0] < dayFmt.跨年id[1],
    JSON.stringify(dayFmt));

  // 折叠态的档距：折起的分组不渲染任何行，于是标签直接相邻。
  // 判据是「与展开时行与行的档距同一个数」—— 全部折起时这一列该读作一份
  // 普通密集列表，而不是一堆浮在空中的标题（用户：「每个分组的间距过大了」）。
  // ⚠ 不能改胶囊高度去收紧：那正是上一轮修掉的「同一列几种尺寸的悬浮盒」。
  const foldPitch = await evalIn(`const list = document.getElementById('session-list');
    const lbl = list.querySelector('.group-lbl');
    const row = list.querySelector('.sess-item');
    const rowPitch = () => { const rs = list.querySelectorAll('.sess-item');
      return +(rs[1].getBoundingClientRect().top - rs[0].getBoundingClientRect().top).toFixed(1); };
    const wrap = lbl.closest('.grp-row');
    const a = wrap.cloneNode(true), b = wrap.cloneNode(true);
    wrap.after(a); a.after(b);
    const pill = n => +n.querySelector('.group-lbl').getBoundingClientRect().height.toFixed(1);
    const top = n => n.getBoundingClientRect().top;
    const out = { 折档距: +(top(b) - top(a)).toFixed(1),
      行档距: rowPitch(), 胶囊高: +lbl.getBoundingClientRect().height.toFixed(1),
      折胶囊高: pill(b), 行高: +row.getBoundingClientRect().height.toFixed(1) };
    a.remove(); b.remove();
    return out;`);
  check("折叠分组之间的档距 == 展开时行与行的档距，且胶囊高度不因此变化",
    foldPitch.折档距 === foldPitch.行档距 && foldPitch.胶囊高 === foldPitch.行高
      && foldPitch.折胶囊高 === foldPitch.胶囊高 && foldPitch.胶囊高 === 32,
    JSON.stringify(foldPitch));

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
    const clone = lbl.closest('.grp-row').cloneNode(true);
    row.after(clone);
    const box0 = clone.querySelector('.group-lbl');
    // 文字在 .gl-t 里（标签改成折叠按钮后不再是裸文本节点）
    const glt = clone.querySelector('.gl-t');
    const tn = [...glt.childNodes].find(n => n.nodeType === 3 && n.textContent.trim());
    const rg = document.createRange(); rg.selectNodeContents(tn);
    const c = document.createElement('canvas').getContext('2d');
    const cs = getComputedStyle(box0);
    c.font = cs.fontWeight + ' ' + cs.fontSize + ' ' + cs.fontFamily;
    const m = c.measureText(tn.textContent.trim());
    const box = box0.getBoundingClientRect();
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

  // 左栏竖向节奏的**分组约定**：按钮→搜索 8（动作区内部，--s2），
  // 其余各段仍是 16（--s4）：头部线→按钮、搜索→列表、分隔线→设置、设置→窗口底。
  // 2026-09-21 改 A：此前四段都是 16，按钮与搜索框之间也是 16 —— 动作区两件事
  // 和"组与列表"的边界用同一个数，读成三块等距、没有分组。
  // 分隔线是 0.5px 发丝线不占节奏，所以量的是「按钮底→分隔线→搜索框顶」两段。
  // 底部设置区同一套：列表底内边距 16、分隔线→设置按钮 16。
  //   这条线是 .left-foot 的 border-top（不是 .left-sep 那样的独立元素），
  //   CSS 写 0.5px 但 Windows/dpr=1 下 Chrome 会向上取整成 1px 渲染 ——
  //   所以「线底」= border box 顶边 + borderTopWidth，直接拿 foot.top 当线会少算 1px。
  //   （这正是实测 18 的一半来源；另一半是 .nav-item 的 margin-top:1px 在容器边界
  //   叠到了 padding 上，已改由 .nav-sec 的 gap 承担。）
  // 容差收到 0.6：改成精确值之后，容差 1 会放过「多 1px」的回归（17 也判绿）。
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
             // ⚠ 量的是**第一个** nav 项，不是写死「设置」：§2.5 把「今日选题」
             // 放在设置上面之后，写死 #btn-open-settings 测的就变成了
             // "两个 nav 项之间的距离"，与它要守的"容器上内边距 16"完全是两件事。
             底线到首项: +(r('.left-foot .nav-sec .nav-item').t - (foot.top + footBorder)).toFixed(2),
             列表底内边距: getComputedStyle(document.querySelector('.left-scroll')).paddingBottom };`);
  check("左栏竖向节奏分组：按钮→搜索 12、其余段 16（含底部设置区）",
    Object.entries(leftRhythm).every(([k, v]) =>
      k === "列表底内边距" ? v === "16px"
        : k === "按钮到搜索" ? Math.abs(v - 12) <= 0.6
        : Math.abs(v - 16) <= 0.6),
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

  // ── 会话行：单行制 + 只标异常 ─────────────────────────────────
  // 用户 2026-09-20 两轮反馈：先是「历史记录优化一下布局样式」，改完再说
  // 「更不好看了」。实测旧版一行 49px，103 条要滚 5303px（侧栏可视区 587px），
  // 而副标题那四项里行业 103/103 恒定、平台 93/103 恒定、日期与分组标签重复。
  // 第二轮的病灶是**行上的状态字**：它在标题与时刻之间插出一栏，
  // 每行标题的截断点随之左右跳，红/灰字散在中间读成一片噪声。
  // 现在：状态记号 | 标题 | 时刻，**只有异常才落记号**（对照 Claude / Linear）。
  // ⚠ 取行按 data-id 点名（s1 = 已完成且校验通过），
  //   不要 `querySelector('.sess-item')` 拿第一条 —— 桩的顺序一变就指着另一条报错。
  const rowStruct = await evalIn(`const r = document.querySelector('#session-list .sess-item[data-id="s1"]');
    const d = r.querySelector('.dot'), db = d.getBoundingClientRect();
    return { sub: r.querySelector('.sess-sub'), state: r.querySelector('.sess-state'),
             anyState: document.querySelectorAll('#session-list .sess-state').length,
             time: r.querySelector('.sess-time').textContent,
             topic: r.querySelector('.sess-topic').textContent,
             dotCls: d.className, dotVis: getComputedStyle(d).visibility,
             dotW: +db.width.toFixed(1), dotL: +db.left.toFixed(1),
             tip: r.title.split(String.fromCharCode(10)).join(' | ') };`);
  check("会话行单行制：副标题与状态字整块没了，时刻只剩 HH:MM（不重复分组给的日期）",
    rowStruct.sub === null && rowStruct.state === null
      && rowStruct.anyState === 0
      && /^[0-9]{2}:[0-9]{2}$/.test(rowStruct.time)
      && rowStruct.topic === "家用电梯怎么挑？", JSON.stringify(rowStruct));
  // 正常完成的记录**不画点**，但 16px 图标盒必须还在原位 ——
  // 用 visibility 不用 display：后者会让整格塌掉，标题左边缘随记录状态左右跳，
  // 而「标题与分组标签文字同列」正是这一版立起来的对齐。
  check("正常完成的记录不画状态记号，但 16px 图标盒仍占位（标题不左右跳）",
    /dot ok/.test(rowStruct.dotCls) && rowStruct.dotVis === "hidden"
      && rowStruct.dotW === 16, JSON.stringify(rowStruct));
  // 从行上撤走 ≠ 丢掉：行业包显示名 / 平台 / 完整时间戳 / 状态词都要能在提示里找到。
  // ⚠ 行业包要盯「不是 slug」—— 这是上一轮的真实回归（列表曾直接吐 elevator）。
  check("悬浮提示补回行业包显示名（非 slug）+ 平台 + 完整时间戳 + 状态词",
    /电梯行业包/.test(rowStruct.tip) && !/elevator/.test(rowStruct.tip)
      && /小红书/.test(rowStruct.tip)
      && /[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}/.test(rowStruct.tip)
      && /已通过校验/.test(rowStruct.tip), rowStruct.tip);
  check("悬浮提示补回了被挪走的时长与字数",
    /60s/.test(rowStruct.tip) && /42 字/.test(rowStruct.tip), rowStruct.tip);

  // 记号三档：ok 无记号 / no 红点 / run 灰点呼吸。
  // 色弱可用性靠「有没有记号」而不是「红还是绿」—— 这一栏只有异常才落东西。
  // s2 = 在跑（writing），s4 = 完成但校验未通过（passed:false）。
  const marks = await evalIn(`const g = id => {
      const r = document.querySelector('#session-list .sess-item[data-id="' + id + '"]');
      if (!r) return null;
      const d = r.querySelector('.dot');
      const before = getComputedStyle(d, '::before');
      return { dot: d.className, vis: getComputedStyle(d).visibility,
               bg: before.backgroundColor, w: before.width, h: before.height,
               anim: before.animationName,
               time: r.querySelector('.sess-time').textContent,
               aria: r.getAttribute('aria-label') }; };
    return { run: g('s2'), no: g('s4'), ok: g('s1') };`);
  check("在跑记录 = 灰点呼吸，时刻仍不带日期",
    marks.run && /dot run/.test(marks.run.dot) && marks.run.vis === "visible"
      && marks.run.anim === "dotPulse"
      && /^[0-9]{2}:[0-9]{2}$/.test(marks.run.time), JSON.stringify(marks.run));
  // 「未通过校验」与「失败」共用一个红点：两者都是「这一条没有可用产物」，
  // 区别（产物不合格 / 根本没跑完）由提示与 aria 的文字承担，不在点上再分一档。
  check("完成但未通过校验的记录落 6px 红点（--bad），且整格可见",
    marks.no && /dot no/.test(marks.no.dot) && marks.no.vis === "visible"
      && marks.no.bg === await resolvedToken("--bad")
      && marks.no.w === "6px" && marks.no.h === "6px",
    JSON.stringify(marks.no));
  check("读屏标签带完整状态词（行上不写字，这一层只能靠 aria 与提示）",
    /未通过校验$/.test(marks.no.aria) && /已通过校验$/.test(marks.ok.aria)
      && /文案撰写中$/.test(marks.run.aria),
    JSON.stringify([marks.ok.aria, marks.no.aria, marks.run.aria]));

  // 行与分组标签必须**同一列**。两行制时副标题从 20px 起、标题从 34px 起，
  // 同一行里两个左边缘 —— 用户报的「边距不统一」有一半是这个。
  // 现在状态点占 16px 图标盒（与分组文件夹盒同宽），标题左边缘 == 标签文字左边缘。
  const rowGeo = await evalIn(`const lbl = document.querySelector('#session-list .group-lbl');
    const row = document.querySelector('#session-list .sess-item[data-id="s1"]');
    const rb = row.getBoundingClientRect();
    const dot = row.querySelector('.dot').getBoundingClientRect();
    return { 标题左: +row.querySelector('.sess-topic').getBoundingClientRect().left.toFixed(1),
             标签文字左: +lbl.querySelector('.gl-t').getBoundingClientRect().left.toFixed(1),
             点盒宽: +dot.width.toFixed(1), 图标盒宽: +lbl.querySelector('.gl-folder').getBoundingClientRect().width.toFixed(1),
             行高: +rb.height.toFixed(1),
             时刻右距: +(rb.right - row.querySelector('.sess-time').getBoundingClientRect().right).toFixed(1),
             // 量**图标**的右边，不量按钮盒：删除按钮是 28px 的命中区，
             // 里面 14px 的图标居中，按钮盒右距 4 而图标右距 11 —— 眼睛看到的是图标。
             删除图标右距: +(rb.right - row.querySelector('.sess-del svg').getBoundingClientRect().right).toFixed(1) };`);
  check("会话行标题与分组标签文字左边缘对齐，状态点盒与分组图标盒同宽(16)、行高 32",
    rowGeo.标题左 === rowGeo.标签文字左 && rowGeo.点盒宽 === 16
      && rowGeo.图标盒宽 === 16 && rowGeo.行高 === 32, JSON.stringify(rowGeo));
  // 悬停时删除图标要**顶掉时刻原来的位置**（两者抢同一个右上角）：
  // 右边缘错开的话，鼠标一停上去那一列就横跳。容差 1px 是规范 §2 例外 1 的
  // 光学微调额度（28px 命中区里居中的 14px 图标，天生带半像素取整）。
  check("悬停露出的删除图标与时刻同一个右边缘（≤1px，不让右上角横跳）",
    Math.abs(rowGeo.时刻右距 - rowGeo.删除图标右距) <= 1, JSON.stringify(rowGeo));

  // ── 左栏一整列必须只有**一套**几何（用户：「新建、搜索、历史记录等等之间的
  //    间距保持统一，还有胶囊高度等等，都要统一」）
  // 改前实测：五块的内边距是 8 / 8 / 8 / 8 / **12** 三档（两个按钮吃全局
  // `button { padding: 0 var(--s3) }`），「新建对话」的图标还**居中**在 87.5 处，
  // 文字列跑出 30 / 31 / 32 / 36 四档；搜索框更隐蔽 —— 它用真 `border: .5px`
  // 画边界，左右各吃掉 0.5px 布局宽度，于是图标从 8.5 起、文字从 32.5 起，
  // 半像素在 dpr=1 的屏上就是错开一列。
  // ⚠ 判据取「五块逐项相等」而不是「等于某个魔数」：宽度随视口变，
  //   但内边距 / 圆角 / 高度 / 图标列 / 文字列必须**一个值**。
  const colGeo = await evalIn(`const txt = (e) => {
      const n = [...e.childNodes].find(v => v.nodeType === 3 && v.textContent.trim());
      if (!n) return null;
      const rg = document.createRange(); rg.selectNodeContents(n);
      return rg.getBoundingClientRect().left;
    };
    const B = (name, sel, iconSel, textSel) => {
      const e = document.querySelector(sel);
      if (!e) return { 名称: name, 缺: true };
      const b = e.getBoundingClientRect(), c = getComputedStyle(e), x = b.left;
      const i = iconSel ? e.querySelector(iconSel) : null;
      const t = textSel ? e.querySelector(textSel) : null;
      const tl = t ? t.getBoundingClientRect().left : txt(e);
      return { 名称: name, h: +b.height.toFixed(1), w: +b.width.toFixed(1),
        pad: c.paddingTop + '/' + c.paddingLeft, radius: c.borderRadius,
        图标左: i ? +(i.getBoundingClientRect().left - x).toFixed(1) : null,
        图标宽: i ? +i.getBoundingClientRect().width.toFixed(1) : null,
        文字左: tl === null ? null : +(tl - x).toFixed(1) };
    };
    const R = s => document.querySelector(s).getBoundingClientRect();
    const foot = R('.left-foot'), btn = R('.left-foot .nav-sec .nav-item');
    const lastBtn = R('#btn-open-settings');   // 最末项，用于「末项→窗口底」那一段
    const bt = parseFloat(getComputedStyle(document.querySelector('.left-foot')).borderTopWidth);
    return { 块: [
        B('新建', '#btn-new-chat', 'svg', null),
        B('搜索', '.sess-search', 'svg', 'input'),
        B('分组标签', '#session-list .group-lbl', '.gl-folder', '.gl-t'),
        B('会话行', '#session-list .sess-item', '.dot', '.sess-topic'),
        B('设置', '#btn-open-settings', 'svg', '.nav-t'),
        // 「今日选题」（B 线，§2.5）与其余 nav 块**必须同一套几何** ——
        // 它是 nav-item 的第四个实例，自己画一套就会在左栏里显得错位。
        // ⚠ 这段注释在**模板字符串内部**：一律不许出现反引号（连类名也不要
        // 加反引号），否则模板会被提前截断、后面的字符变成 Node 代码执行 ——
        // 实测症状是 ReferenceError: item is not defined，而报错行号指向注释本身。
        B('今日选题', '#btn-topics', 'svg', '.nav-t')],
      竖向: { 头部线到新建: +(R('#btn-new-chat').top - R('.left-head').bottom).toFixed(1),
        新建到搜索: +(R('.sess-search').top - R('#btn-new-chat').bottom).toFixed(1),
        搜索到列表: +(R('.left-scroll').top - R('.sess-search').bottom).toFixed(1),
        分隔线到首项: +(btn.top - (foot.top + bt)).toFixed(1),
        末项到窗口底: +(innerHeight - lastBtn.bottom).toFixed(1) } };`);
  const same = (k) => colGeo.块.every(b => b[k] === colGeo.块[0][k]);
  check("左栏六块（新建/搜索/分组标签/会话行/设置/今日选题）胶囊几何逐项相等",
    colGeo.块.length === 6 && colGeo.块.every(b => !b.缺)
      && same('h') && same('w') && same('pad') && same('radius')
      && same('图标左') && same('图标宽') && same('文字左'),
    JSON.stringify(colGeo.块));
  check("左栏那一套值就是：高 32 + 圆角 8 + 内边距 0/12 + 图标列 12(盒宽 16) + 文字列 36",
    colGeo.块[0].h === 32 && colGeo.块[0].radius === "8px"
      && colGeo.块[0].pad === "0px/12px" && colGeo.块[0].图标左 === 12
      && colGeo.块[0].图标宽 === 16 && colGeo.块[0].文字左 === 36,
    JSON.stringify(colGeo.块[0]));
  // 竖向节奏的分组约定：按钮→搜索 12（动作区内，--s3），其余段都是 16（= --s4），
  // 列表内部 1px 是密集行的既定节奏（另一条守）。
  // 2026-09-21 改 A：此前「头部线→新建→搜索→列表」三段全是 16，动作区两件事
  // 与「组→列表」边界等距 → 三块等距没有分组。组内收到 12，其余 16 不动。
  // 这里连底部「分隔线→设置」一起量 —— 它曾被 .nav-item 的 1px margin 顶成 17。
  // 「设置→窗口底边」也钉在同一条 16 上：.left-foot 与 .left-top 是同列上下两个
  // 固定区，必须用同一套 padding。下边曾停在 --s2，于是上 16 下 8 —— 用户报
  // 「底部设置区域四周间距不统一」量的正是这个。左右仍是 12（图标列，见五块几何那条）。
  check("左栏竖向节奏分组：新建→搜索 12，其余段 16（头→新、搜→列、分隔→首项、末项→底）",
    colGeo.竖向.头部线到新建 === 16 && colGeo.竖向.新建到搜索 === 12
      && colGeo.竖向.搜索到列表 === 16 && colGeo.竖向.分隔线到首项 === 16
      && colGeo.竖向.末项到窗口底 === 16,
    JSON.stringify(colGeo.竖向));
  // ── 左栏的两处「刻度外细节」（用户：左侧只优化细节，不要大改）────────
  // 1) 图标盒：这一列里 14px 字形配的盒子必须都是 16 —— 搜索行的清除按钮
  //    原来是 18，是全列唯一不合档的那颗（规范 §2「4 的倍数」+ §4 图标档位）。
  // 2) 焦点环：全站只许一种配方 --shadow-focus（规范 §2·5.5）。
  //    改前左栏有两处自绘 outline 环，其中 .group-lbl 用的是 7% 黑的
  //    --ring-focus —— 在侧栏浅灰上量不出来，键盘走到分组标签看不见焦点。
  // ⚠ 焦点环读**样式表里写下的声明**，不 Tab 到元素上读计算值：
  //    :focus-visible 要键盘态才进得去，程序 focus() 进不进得去取决于
  //    上一次输入是鼠标还是键盘 —— 那样量会假绿。
  const leftDetail = await evalIn(`return (() => {
    const box = (n) => Math.round(n.getBoundingClientRect().width);
    // 清除按钮平时 display:none（hover / 聚焦才现），没有盒子可量。
    // 临时摘掉 hidden 量完再装回去 —— 它不参与任何状态，比读样式表里的
    // 声明强：读到的值可能被后面某条更具体的规则盖掉（这一族踩过一次）。
    const clr = document.querySelector('.sess-search .icon-btn');
    let clrW = null;
    if (clr) { clr.classList.remove('hidden'); clrW = box(clr); clr.classList.add('hidden'); }
    const ringOf = (sel) => {
      for (const sheet of document.styleSheets) {
        let rules; try { rules = sheet.cssRules; } catch (_) { continue; }
        for (const x of rules) {
          if (!x.selectorText) continue;
          if (x.selectorText.split(',').map(s => s.trim()).includes(sel)
              && x.style.getPropertyValue('box-shadow')) {
            return x.style.getPropertyValue('box-shadow');
          }
        }
      }
      return null;
    };
    const RING_STYLES = ['solid', 'double', 'dotted', 'dashed', 'groove', 'ridge', 'outset', 'inset', 'auto'];
    const rings = [];
    for (const sheet of document.styleSheets) {
      let rules; try { rules = sheet.cssRules; } catch (_) { continue; }
      for (const x of rules) {
        if (!x.selectorText || !/:focus-visible/.test(x.selectorText)) continue;
        // 只认**真的画了环**的写法。outline: none 把 outline-style 重置成 none、
        // 把 outline-width 重置成 initial —— 那正是这一族关掉浏览器默认环的写法，
        // 不是「第二种配方」。（第一版按 width 判，把全部 9 条都误判成了违规。）
        if (RING_STYLES.includes(x.style.getPropertyValue('outline-style'))) {
          rings.push(x.selectorText);
        }
      }
    }
    return {
      清除按钮盒: clrW,
      分组图标盒: box(document.querySelector('.gl-folder')),
      行记号盒: box(document.querySelector('.sess-item .dot')),
      品牌字形: box(document.querySelector('.brand .logo-wrap svg')),
      自绘焦点环: rings,
      分组焦点: ringOf('.group-lbl:focus-visible'),
      删除焦点: ringOf('.sess-del:focus-visible'),
    }; })()`);
  check("左栏图标盒同一档：清除按钮 / 分组文件夹 / 行记号都是 16 的盒",
    leftDetail.清除按钮盒 === 16 && leftDetail.分组图标盒 === 16
      && leftDetail.行记号盒 === 16, JSON.stringify(leftDetail));
  check("品牌字形回到规范 §4 的图标档位（14，不是刻度外的 13）",
    leftDetail.品牌字形 === 14, JSON.stringify(leftDetail.品牌字形));
  check("全站 :focus-visible 没有第二套焦点环配方（规范 §2·5.5）",
    leftDetail.自绘焦点环.length === 0, JSON.stringify(leftDetail.自绘焦点环));
  check("左栏那两处焦点环已落到 --shadow-focus 一族",
    /shadow-focus/.test(leftDetail.分组焦点 || '')
      && /shadow-focus/.test(leftDetail.删除焦点 || ''), JSON.stringify(leftDetail));
  // 悬停时时刻让位给删除按钮（两者抢同一个右上角）：不挡行底、不叠字。
  const timeFade = await evalIn(`const row = document.querySelector('#session-list .sess-item[data-id="s1"]');
    row.scrollIntoView({ block: 'center' });
    const b = row.getBoundingClientRect();
    return { x: Math.round(b.left + b.width / 2), y: Math.round(b.top + b.height / 2) };`);
  await cdp.send("Input.dispatchMouseEvent", { type: "mouseMoved", x: timeFade.x, y: timeFade.y });
  await sleep(250);
  const hovered = await evalIn(`const row = document.querySelector('#session-list .sess-item[data-id="s1"]');
    return { timeOp: getComputedStyle(row.querySelector('.sess-time')).opacity,
             delOp: getComputedStyle(row.querySelector('.sess-del')).opacity,
             bg: getComputedStyle(row).backgroundColor };`);
  check("悬停会话行：时刻淡出、删除按钮淡入（同一个位置不叠字）",
    hovered.timeOp === "0" && hovered.delOp === "1", JSON.stringify(hovered));
  await cdp.send("Input.dispatchMouseEvent", { type: "mouseMoved", x: 5, y: 5 });
  await sleep(200);

  // 分组图标 = 文件夹，**开 / 合两个字形**（参考 Qoder 侧栏）。
  // ⚠ 折叠信号必须换得来：这一列里折叠态只由图标表达（不给文字换色 —— 另一条
  //   断言守着），所以「换成文件夹」不能换成「同一个图标转个角度」，
  //   必须展开时可见开口那一枚、折起时可见合口那一枚，且**任何时刻只有一枚可见**
  //   （两枚都在 = 图标叠影；都不在 = 折叠态整列没有状态信号）。
  // 此前这里是箭头：静止态不旋转、折起 rotate(-90deg) 朝右。更早用 CSS 边框画
  // 三角，静止朝右、折叠转朝下，方向与所有分组列表的惯例相反。
  const folder = await evalIn(`return (async () => {
    const vis = () => {
      const g = document.querySelector('#session-list .group-lbl');
      return [...g.querySelectorAll('.gl-folder')].map(a => ({
        tag: a.tagName, shown: getComputedStyle(a).display !== 'none',
        w: Math.round(a.getBoundingClientRect().width) }));
    };
    const open = vis();
    document.querySelector('#session-list .group-lbl').click();
    await new Promise(r => setTimeout(r, 120));
    const shut = vis();
    document.querySelector('#session-list .group-lbl').click();
    await new Promise(r => setTimeout(r, 120));
    return { open, shut, back: vis(), stored: localStorage.getItem('ts.sess.folded') };
  })();`);
  // 露出来的那枚必须是 16 的图标盒；藏起来的那枚 display:none，天生没有盒子
  // （w=0）—— 那正是「只有一枚可见」的表现，不是缺陷。
  const one = (v) => v.length === 2 && v.every(x => x.tag === "svg")
    && v.filter(x => x.shown).length === 1 && v.find(x => x.shown).w === 16;
  check("分组图标是两枚 16px 内联 SVG 文件夹，展开/折起各自只露一枚、再展开复位",
    one(folder.open) && one(folder.shut) && one(folder.back)
      && folder.open.findIndex(x => x.shown) !== folder.shut.findIndex(x => x.shown)
      && folder.stored === "[]", JSON.stringify(folder));

  // 「…」菜单：常态隐藏、点开给**一个**危险动作，且条数写在字里。
  const grpMenu = await evalIn(`return (async () => {
    const row = document.querySelector('#session-list .grp-row');
    const more = row.querySelector('.grp-more'), menu = row.querySelector('.grp-menu');
    const idle = { moreOp: +getComputedStyle(more).opacity, menuHidden: menu.classList.contains('hidden'),
                   items: menu.querySelectorAll('.grp-menu-item').length };
    more.click();
    await new Promise(r => setTimeout(r, 120));
    const open = { menuHidden: menu.classList.contains('hidden'),
      aria: more.getAttribute('aria-expanded'),
      text: menu.querySelector('.grp-menu-item').textContent.trim(),
      color: getComputedStyle(menu.querySelector('.grp-menu-item')).color,
      // 与 --bad 现算比对，不抄色值：抄一份 rgb 进断言，令牌一改就假红
      bad: (() => { const t = document.createElement('span');
        t.style.color = 'var(--bad)'; document.body.appendChild(t);
        const v = getComputedStyle(t).color; t.remove(); return v; })(),
      w: Math.round(menu.getBoundingClientRect().width),
      under: Math.round(menu.getBoundingClientRect().top - row.getBoundingClientRect().bottom) };
    document.body.click();
    await new Promise(r => setTimeout(r, 120));
    const closed = { menuHidden: menu.classList.contains('hidden'),
      aria: more.getAttribute('aria-expanded') };
    return { idle, open, closed };
  })();`);
  check("分组「…」常态隐藏，点开弹出一个红色危险项、写着条数，点别处自动收起",
    grpMenu.idle.moreOp === 0 && grpMenu.idle.menuHidden && grpMenu.idle.items === 1
      && grpMenu.open.menuHidden === false && grpMenu.open.aria === "true"
      && /^删除这一组（[0-9]+ 条）$/.test(grpMenu.open.text)
      && grpMenu.open.color === grpMenu.open.bad
      && grpMenu.open.w >= 168 && grpMenu.open.under > 0
      && grpMenu.closed.menuHidden && grpMenu.closed.aria === "false",
    JSON.stringify(grpMenu));

  // 删整组 = 该组每条记录各发一个 DELETE，一条不多一条不少。
  // ⚠ 桩是**无状态**的（每次 /api/history 都重新生成同一份），所以这里能断言的
  //   是「请求打对了」而不是「列表少了 N 行」—— 真删掉的验证走 e2e-live.js 的
  //   真实引擎那条路。确认弹窗必须出现且取消时**一个请求都不发**：
  //   一次抹掉整组不可恢复，误触的代价比单条删除大得多。
  const grpDel = await evalIn(`return (async () => {
    window.__delIds = [];
    const row = document.querySelector('#session-list .grp-row');
    // 这一组的行 = 从本组壳往后数、直到下一个壳或列表结尾（行不在壳里，是兄弟）。
    const groupIds = [];
    for (let n = row.nextElementSibling; n && !n.classList.contains('grp-row'); n = n.nextElementSibling)
      if (n.classList.contains('sess-item')) groupIds.push(n.dataset.id);
    const otherIds = [...document.querySelectorAll('#session-list .sess-item')]
      .map(r => r.dataset.id).filter(x => !groupIds.includes(x));
    row.querySelector('.grp-more').click();
    row.querySelector('.grp-menu-item').click();
    await new Promise(r => setTimeout(r, 150));
    const dlg = { open: !document.getElementById('confirm-dialog').classList.contains('hidden'),
      title: document.getElementById('cd-title').textContent,
      msg: document.getElementById('cd-msg').textContent,
      dels: window.__delIds.slice() };
    document.getElementById('cd-no').click();
    await new Promise(r => setTimeout(r, 80));
    const afterNo = { dels: window.__delIds.slice(),
      hidden: document.getElementById('confirm-dialog').classList.contains('hidden') };
    row.querySelector('.grp-more').click();
    row.querySelector('.grp-menu-item').click();
    await new Promise(r => setTimeout(r, 150));
    document.getElementById('cd-yes').click();
    await new Promise(r => setTimeout(r, 400));
    return { 该组: groupIds, 其他: otherIds, dlg, afterNo, 删完: window.__delIds.slice() };
  })();`);
  check("删整组先弹确认；取消一个请求都不发，确认则**只**对该组每条记录各发一次 DELETE",
    grpDel.dlg.open && grpDel.dlg.title === "删除整个分组"
      && /不可恢复/.test(grpDel.dlg.msg) && grpDel.dlg.dels.length === 0
      && grpDel.afterNo.dels.length === 0 && grpDel.afterNo.hidden
      && grpDel.该组.length === 2
      && grpDel.删完.slice().sort().join() === grpDel.该组.slice().sort().join()
      && !grpDel.删完.some(id => grpDel.其他.includes(id)),
    JSON.stringify(grpDel));

  // 分组标签悬停必须有落点。此前是 `.group-lbl:hover{color:var(--text-2)}` ——
  // 与常态**同一个值**的一条空规则：标签看着就是一行普通文字，
  // 用户不知道它能点（「历史记录无法折叠」有一半是这个原因）。
  // ⚠ 用真悬停（dispatchMouseEvent），加 class 的假悬停抓不到写错的伪类。
  const lblPt = await evalIn(`const g = document.querySelector('#session-list .group-lbl');
    const b = g.getBoundingClientRect();
    return { x: Math.round(b.left + b.width / 2), y: Math.round(b.top + b.height / 2) };`);
  await cdp.send("Input.dispatchMouseEvent", { type: "mouseMoved", x: lblPt.x, y: lblPt.y });
  await sleep(250);
  const lblHover = await evalIn(`const g = document.querySelector('#session-list .group-lbl');
    const s = getComputedStyle(g);
    const c = getComputedStyle(g.querySelector('.count'));
    return { bg: s.backgroundColor, color: s.color,
             pillBg: c.backgroundColor, pillColor: c.color,
             idle: getComputedStyle(document.querySelectorAll('#session-list .group-lbl')[1]).color };`);
  check("悬停分组标签给落点：底色 --fill + 标签与计数一起提亮",
    lblHover.bg === "rgba(0, 0, 0, 0.04)" && lblHover.color === "rgb(29, 29, 31)"
      && lblHover.idle === "rgb(110, 110, 115)"
      && lblHover.pillColor === "rgb(110, 110, 115)", JSON.stringify(lblHover));
  // 计数是**裸数字**，不许再描背景：药丸在 11px 小字里凭空多出一块面积，
  // 读起来比标签本身还重（「今天 17」里眼睛先看到 17）。
  // 字重也从 600 降到 400 —— 与标签同重时，「09-12 26」两个数字是同一族字，
  // 读不出哪个是计数（见 .count 那段注释）。
  // 设置页 `.kb-group-c` 抄的是同一画法，但它只在行业包面板渲染，
  // 此刻多半不在 DOM 里 —— 跨页比对交给 styles.css 那条注释与人工走查。
  const countStyle = await evalIn(`const c = getComputedStyle(
      document.querySelector('#session-list .group-lbl .count'));
    return { bg: c.backgroundColor, pad: c.padding, radius: c.borderRadius,
             fs: c.fontSize, fw: c.fontWeight, color: c.color };`);
  check("分组计数不描背景（裸数字：透明底 / 无内边距 / 无圆角）、字重比标签轻一档",
    /rgba?\([^)]*,\s*0\)$/.test(countStyle.bg) && countStyle.pad === "0px"
      && countStyle.radius === "0px" && countStyle.fs === "11px"
      && countStyle.fw === "400" && countStyle.color === "rgb(110, 110, 115)",
    JSON.stringify(countStyle));
  await cdp.send("Input.dispatchMouseEvent", { type: "mouseMoved", x: 5, y: 5 });
  await sleep(200);

  // 折叠态**只由箭头表达**，文字不换色：试过把折起的标签提亮到 --text-1，
  // 真机上「今天(折)」比「昨天(开)」更黑，读成「选中 / 鼠标停着」而不是「收着」。
  const foldColor = await evalIn(`return (async () => {
    const gs = document.querySelectorAll('#session-list .group-lbl');
    const before = getComputedStyle(gs[0]).color;
    gs[0].click(); await new Promise(r => setTimeout(r, 80));
    const after = getComputedStyle(gs[0]).color;
    gs[0].click(); await new Promise(r => setTimeout(r, 80));
    return { before: before, after: after };
  })();`);
  check("折叠不改文字色（文件夹开合才是状态信号）",
    foldColor.before === foldColor.after && foldColor.before === "rgb(110, 110, 115)",
    JSON.stringify(foldColor));

  // ── 认不出来的状态必须落回「已结束」，不许让左栏永远轮询 ──────────
  // 分步确认（paused_awaiting_confirmation）2026-09-19 整体移除，但磁盘上的
  // 旧记录还在：`generated/index.json` 里就有一条，而 `store.py` 的
  // `_summary_from_job()` 把 job.json 的 state 原样透传出来。
  // 修复前 `settled` 是一份**抄来的终态清单**，这个值不在里面 → 读成「还没跑完」：
  //   · loadSessions 按固定周期再拉一次 /api/history，永不停止；
  //   · 行上一颗永远呼吸的状态点；
  //   · 删除按钮写着「放弃这次生成」，其实没有任何作业可放弃。
  // 同一形状的第二处是 cancelled 记录：旧的点色三元表达式落到 else，
  // 于是「已取消」也挂着一颗呼吸点。
  // ⚠ 桩里换列表要**先存回原 fetch**，否则后面所有取行的断言都指着这两条。
  const legacy = await evalIn(`return (async () => {
    const pad = n => String(n).padStart(2, '0');
    const d = new Date(); d.setDate(d.getDate() - 4);
    const iso = d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate()) + 'T09:00:00';
    const base = { id: '', created_at: iso, pack: 'elevator', platform: '抖音',
                   duration: null, chars: null, passed: null, error: null };
    window.__legacyHist = [
      Object.assign({}, base, { id: 'legacy1', topic: '分步确认时代的旧记录',
                                state: 'paused_awaiting_confirmation' }),
      Object.assign({}, base, { id: 'legacy2', topic: '被取消的那次生成',
                                state: 'cancelled' }),
    ];
    const orig = window.fetch;
    window.__histCalls = 0;
    window.fetch = function (u, o) {
      const s = String(u).split('?')[0];
      if (s.slice(-12) === '/api/history') { window.__histCalls += 1;
        return Promise.resolve(new Response(JSON.stringify(window.__legacyHist),
          { status: 200, headers: { 'Content-Type': 'application/json' } })); }
      return orig.call(window, u, o);
    };
    await window.__ts.loadSessions();
    const read = id => {
      const r = document.querySelector('#session-list .sess-item[data-id="' + id + '"]');
      if (!r) return null;
      const dot = r.querySelector('.dot');
      return { dot: dot.className, vis: getComputedStyle(dot).visibility,
               anim: getComputedStyle(dot, '::before').animationName,
               act: r.querySelector('.sess-del').dataset.act,
               delTip: r.querySelector('.sess-del').title,
               aria: r.getAttribute('aria-label'),
               tip: r.title.split(String.fromCharCode(10)).join(' | ') };
    };
    const rows = { old: read('legacy1'), cancel: read('legacy2') };
    const before = window.__histCalls;
    await new Promise(r => setTimeout(r, 3400));
    const after = window.__histCalls;
    window.fetch = orig;
    await window.__ts.loadSessions();
    return { rows: rows, polls: after - before, restored:
      document.querySelectorAll('#session-list .sess-item').length };
  })();`);
  check("已删除的旧状态读成「已结束」：落红点不呼吸、删除键是删除不是放弃",
    legacy.rows.old && /dot no/.test(legacy.rows.old.dot)
      && legacy.rows.old.vis === "visible" && legacy.rows.old.anim === "none"
      && legacy.rows.old.act === "del" && /删除这条记录/.test(legacy.rows.old.delTip)
      && /已中断/.test(legacy.rows.old.aria)
      && !/paused_awaiting_confirmation/.test(legacy.rows.old.tip),
    JSON.stringify(legacy.rows.old));
  check("已取消的记录不再挂呼吸点（旧点色三元表达式落到 else 的病灶）",
    legacy.rows.cancel && /dot no/.test(legacy.rows.cancel.dot)
      && legacy.rows.cancel.anim === "none"
      && legacy.rows.cancel.act === "del"
      && /已取消/.test(legacy.rows.cancel.aria), JSON.stringify(legacy.rows.cancel));
  check("列表里只剩这类记录时不轮询（修复前每 3 秒空转一次、永不停止）",
    legacy.polls === 0, `3.4 秒内 /api/history 被打了 ${legacy.polls} 次`);
  check("换完列表要还原（后面的断言仍指着桩里那 6 条）",
    legacy.restored === 6, `还原后行数=${legacy.restored}`);

  // 删除按钮悬停态：**只把图标由灰转红，背景保持透明**。
  // 用户 2026-09-18 两句话，第二句是对第一句实现的否决：
  //   ①「删除按钮悬浮时的样式改一下，现在会显得距离边距不统一」
  //   ②「不是让你改成红色的删除按钮，不要背景色，就改删除图标的颜色就行了啊」
  // 先按 ① 做了「实色红底 + 反白图标」，被 ② 直接否掉 —— 凭空多出的一块面积
  // 反而加重了「边距不统一」的观感，用户要的是图标自己变醒目。
  //
  // ⚠ 断言必须把 bg 也钉住（=== 透明），只测 fg 是不够的：
  //   「红底 + 白图标」和「透明底 + 红图标」的 fg 完全不同，看似能区分，但
  //   只要有人给按钮补一个**任意**底色（连 --fill-strong 淡灰底都算），
  //   只测 fg 的断言照样绿 —— 而那正是用户明确不要的东西。
  //
  // ⚠ 用真悬停（dispatchMouseEvent）而不是加 class：样式写错时假 hover 照样绿。
  const delPt = await evalIn(`const d = document.querySelector('#session-list .sess-item .sess-del');
    d.scrollIntoView({ block: 'center' });
    const b = d.getBoundingClientRect();
    return { x: Math.round(b.left + b.width / 2), y: Math.round(b.top + b.height / 2) };`);
  await cdp.send("Input.dispatchMouseEvent", { type: "mouseMoved", x: delPt.x, y: delPt.y });
  await sleep(250);
  const delHover = await evalIn(`const d = document.querySelector('#session-list .sess-item .sess-del');
    const s = getComputedStyle(d);
    return { bg: s.backgroundColor, fg: s.color, op: s.opacity };`);
  // --bad / --bad-hover 按 token 解析比对，不抄 rgb 字面量（规范 §7.2）
  // 透明底在 computedStyle 里的写法是 rgba(0, 0, 0, 0)（不是 "transparent"）；
  // 用 /0\\)$/ 匹配 alpha=0，顺带挡住任何带底色的写法。
  check("删除按钮悬停只改图标颜色、不加背景色（用户明确否掉了实色红底）",
    /rgba?\([^)]*,\s*0\)$/.test(delHover.bg) && delHover.fg === await resolvedToken("--bad")
      && delHover.op === "1",
    JSON.stringify(delHover));

  // 按下态同样不许长背景色 —— 只换个更深的红 + 缩放。
  // 单测 :hover 挡不住「hover 透明但 active 给底色」这种半吊子改法。
  await cdp.send("Input.dispatchMouseEvent", { type: "mousePressed", x: delPt.x, y: delPt.y, button: "left", clickCount: 1 });
  await sleep(120);
  const delActive = await evalIn(`const d = document.querySelector('#session-list .sess-item .sess-del');
    const s = getComputedStyle(d);
    return { bg: s.backgroundColor, fg: s.color };`);
  await cdp.send("Input.dispatchMouseEvent", { type: "mouseReleased", x: delPt.x, y: delPt.y, button: "left", clickCount: 1 });
  check("删除按钮按下态也不给背景色（仍是透明底 + 更深一档的红）",
    /rgba?\([^)]*,\s*0\)$/.test(delActive.bg) && delActive.fg === await resolvedToken("--bad-hover"),
    JSON.stringify(delActive));

  // 悬停块的几何必须与行对齐：28px 方块垂直居中、右边距与行的 padding 一致。
  // 「显得距离边距不统一」的另一半是这个 —— 底色方案被否掉之后，
  // 几何是**唯一**还留着的东西，方块若偏上/贴边，问题原样还在。
  const delGeo = await evalIn(`const row = document.querySelector('#session-list .sess-item');
    const del = row.querySelector('.sess-del');
    const rb = row.getBoundingClientRect(), db = del.getBoundingClientRect();
    const rs = getComputedStyle(row);
    return { up: Math.round(db.top - rb.top), down: Math.round(rb.bottom - db.bottom),
             right: Math.round(rb.right - db.right),
             padR: Math.round(parseFloat(rs.paddingRight)),
             size: [Math.round(db.width), Math.round(db.height)] };`);
  check("删除按钮悬停块在行内垂直居中、右距合理（不再像歪着的色斑）",
    delGeo.size[0] === 28 && delGeo.size[1] === 28
      && Math.abs(delGeo.up - delGeo.down) <= 1
      && delGeo.right <= delGeo.padR,
    JSON.stringify(delGeo));

  // 测完把鼠标挪开：后面还有断言要量别处的悬停态，留着会污染。
  await cdp.send("Input.dispatchMouseEvent", { type: "mouseMoved", x: 5, y: 5 });
  await sleep(200);

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
  // 「保存」现在是模型表单那颗（高级配置并入它，2026-09-17）——
  // 先保证 md-model / md-baseurl 有值，否则会被表单自己的校验拦住。
  await evalIn(`var m = document.getElementById('md-model'); m.value = 'glm-4.7';
    m.dispatchEvent(new Event('input', {bubbles:true}));
    var u = document.getElementById('md-baseurl');
    if (!u.value) { u.value = 'https://x/v4'; u.dispatchEvent(new Event('input', {bubbles:true})); }
    document.getElementById('md-save').click(); return true;`);
  await sleep(900);
  const kept = await evalIn(`return {
    pack: document.getElementById('pack').value,
    options: document.getElementById('pack').options.length };`);
  check("保存设置后仍保持选中的行业包（不再跳回默认包）",
    kept.pack === "fitment" && kept.options === 2, JSON.stringify(kept));

  // ── 12c) 未配置模型时的入口 ──────────────────────────────
  // 2026-09-20：空态 hero 的「配置引导」那一态整块下线（用户原话「太难看，直接
  // 去掉吧」）。留下来的两件事必须仍然成立：① 首启自动落到设置里的「模型接口」；
  // ② 工具条那颗胶囊真的看得出是警示态（它现在是唯一的常驻提示）。
  await cdp.send("Page.navigate",
    { url: `http://127.0.0.1:${PORT}/?token=stubtoken&nokey=1` });
  await sleep(1800);
  const firstRun = await evalIn(`return (function(){
    var empty = document.getElementById('empty');
    var h3 = document.querySelector('#empty h3');
    var pill = document.querySelector('#model-pick .select-btn');
    var cs = pill ? getComputedStyle(pill) : null;
    return {
      // hero 只该有一态：标题是**按时段落档的问候语**（五档之一，见 GREET_RE），
      // 不再是写死的一句「想聊点什么？」；[data-when] 那套整体拿掉
      heroTitle: (h3.textContent || '').trim(),
      whenNodes: empty.querySelectorAll('[data-when]').length,
      setupBtn: !!document.getElementById('btn-empty-setup'),
      cards: document.querySelectorAll('#empty .setup-card').length,
      samples: document.querySelectorAll('#empty-samples .sample-card').length,
      settingsOpen: !document.getElementById('settings-screen').classList.contains('hidden'),
      paneLlm: !document.getElementById('pane-llm').classList.contains('hidden'),
      // ⚠ 量**计算色**，不查 class：#model-pick .select-btn 是 id 选择器，
      //   历史上它无条件写 color，把按 class 写的 .is-warn / .is-mock 整个顶掉
      //   —— class 挂上了、颜色一点没变，只看 class 的断言会假绿。
      pillWarn: pill ? pill.classList.contains('is-warn') : null,
      pillColor: cs ? cs.color : null,
      pillText: pill ? pill.querySelector('.sel-text').textContent : null,
    }; })()`);
  check("首启自动打开设置并落在「模型接口」分区",
    firstRun.settingsOpen && firstRun.paneLlm, JSON.stringify(firstRun));
  check("空态 hero 只有一态（问候语分五档；引导态与 [data-when] 切换已下线，示例胶囊保留）",
    GREET_RE.test(firstRun.heroTitle)
      && firstRun.whenNodes === 0 && firstRun.setupBtn === false
      && firstRun.cards === 0 && firstRun.samples === 4,
    JSON.stringify(firstRun));
  check("未配置时工具条那颗胶囊确实是警示橙（不是只挂个 class）",
    firstRun.pillWarn === true && firstRun.pillColor === await resolvedToken("--warn"),
    JSON.stringify(firstRun));
  // 「没配过」必须看得出来。后端会把没写的字段静默兜底成内置默认值
  // （config.py 的 _effective：空 base_url → DEFAULT_MODEL），界面若不标，
  // 未配置状态和已配置状态长得一模一样：列表里那行写着 glm-4.7、
  // 输入区右侧也写着 glm-4.7，用户第一眼就以为配好了。
  //
  // ⚠ 2026-09-17（任务 5/6）：模型行不再渲染「内置默认」小标，「（默认）」后缀
  // 也从 picker 选项名上摘掉 —— "默认模型"整个下线，前端不再用这个概念。
  // 区分"未配置 vs 已配置"靠 placeholder + Key 两态 + 列表行「未配置 Key」warn。
  // 所以这条断言改成：模型行**不应有任何** tag-default 节点；
  // 高级配置那三项仍是默认（高级配置仍是「兜底值」语义，不是"默认模型"）。
  //
  // ⚠ 断言按 **data-field 的集合**比，不按标签个数比：个数对不上可能只是布局
  // 变了，而集合对不上才是「该标的没标 / 不该标的标了」。
  const notCfg = await evalIn(`return (function(){
    function fields(root){
      return [].concat.apply([], Array.from(root.querySelectorAll('.tag-default'))
        .map(function(t){ return (t.dataset.field || '').split(',').filter(Boolean); }));
    }
    var row = document.querySelector('#llm-list .pl-item.on');
    var s = document.getElementById('p-model');
    var b = s && s.parentNode.querySelector('.select-btn');
    return { rowFields: fields(row).sort(),
             advFields: fields(document.getElementById('st-adv')).sort(),
             // 任务 5：模型行 .tag-default 节点应当**为零**。
             rowTagCount: row.querySelectorAll('.tag-default').length,
             pickerText: b ? b.querySelector('.sel-text').textContent : '',
             pickerValue: s ? s.value : '' };
  })()`);
  check("未配置时：高级配置两项仍是「内置默认」，模型行不再有任何 tag-default",
    notCfg.rowFields.length === 0
      && notCfg.advFields.join() === "retries,timeout"
      && notCfg.rowTagCount === 0,
    JSON.stringify(notCfg));
  // 任务 5：picker 上不再附「（默认）」后缀。区分未配置与已配置靠
  // sel._warnNote（未配 Key 时显示）+ 列表行「未配置 Key」warn。
  // 判据：`text` 不含「默认」字样（用 includes 而不是正则 —— 之前用 /-默认-/
  // 漏检了「（默认）」前后是括号不是 `-`，结果变异检验**假绿**）。
  check("未配置时 picker 不再附「（默认）」后缀（默认模型概念已下线）",
    !notCfg.pickerText.includes("默认") && notCfg.pickerValue === "m-default",
    JSON.stringify({ text: notCfg.pickerText, value: notCfg.pickerValue }));

  // 右列编辑表单里，**没配过的字段必须留空**，不能把生效值（兜出来的默认地址）
  // 填进框里 —— 那等于程序写的值冒充用户输入，用户没动过手却看到一串地址，
  // 而且保存一次它就真的成了他的配置。
  // （原来是点表格里的「编辑」按钮开弹窗；三列化后**点条目本身**即选中编辑。）
  // 2026-09-17：tag-default 已被任务 5 移除 → tags 字段必然为空字符串。
  await evalIn(`document.querySelector('#llm-list .pl-item.on').click();
    return true;`);
  await sleep(300);
  const editDlg = await evalIn(`return {
    open: !document.getElementById('md-form-card').classList.contains('hidden'),
    title: document.getElementById('md-title').textContent,
    url: document.getElementById('md-baseurl').value,
    model: document.getElementById('md-model').value,
    ph: document.getElementById('md-baseurl').placeholder,
    // 「内置默认」小标已下线（任务 5）—— 编辑表单里不再有 tag-default 节点。
    // 测一下确实为空，留作回归锚点（突变"重新挂上"会立刻报红）。
    tags: Array.from(document.querySelectorAll('#md-form-card .tag-default'))
            .map(t => t.dataset.field).sort().join(),
    // md-reset-model / md-reset-baseurl 这两个 linkbtn 已删除（任务 6）。
    resetBtns: ['md-reset-model', 'md-reset-baseurl'].filter(function (id) {
      return !!document.getElementById(id); }).length,
    akPh: document.getElementById('md-apikey').placeholder };`);
  check("编辑一条没配过的模型：右列标题是「编辑」，字段留空，不再有内置默认标",
    editDlg.open && editDlg.title === "编辑模型"
    && editDlg.url === "" && editDlg.model === ""
    && editDlg.tags === "", JSON.stringify(editDlg));
  // 留空但不能让人不知道填什么：placeholder 里给出默认地址
  check("留空的字段用 placeholder 说明默认值（不是一片空白）",
    /^https:\/\//.test(editDlg.ph || ""), editDlg.ph);
  check("未配置时 Key 输入框不再暗示「已经配过了」",
    /粘贴/.test(editDlg.akPh), editDlg.akPh);
  // 任务 6：md-reset-model / md-reset-baseurl 这两个 linkbtn 必须从 DOM 移除。
  // 「恢复默认」整段下线，「默认模型」概念也跟着下线（任务 5）。
  check("模型编辑表单里不再有「恢复默认」按钮",
    editDlg.resetBtns === 0, JSON.stringify(editDlg.resetBtns));
  // 任务 4：采样温度 (temperature) 已从高级配置移除 —— 输入框 #st-temperature
  // 应当从 DOM 上消失。修法：HTML 整段删，JS 不再 applyNumericBounds 写 min/max。
  // 「恢复」这个字段需要同时改回 HTML + JS + verify.js 三处 —— 三处都守住才完整。
  await evalIn(`document.getElementById('btn-close-settings').click(); return true;`);
  await sleep(200);
  const advClean = await evalIn(`return {
    // DOM 上没有 st-temperature / st-maxtokens 输入框（两项都已下线）
    temperatureInput: !!document.getElementById('st-temperature'),
    maxTokensInput: !!document.getElementById('st-maxtokens'),
    // 高级配置只剩 2 个 block-inner（重试 / 超时）
    advBlocks: document.querySelectorAll('#st-adv .block-inner').length,
    // 独立的「保存高级配置」按钮已删（并入模型表单的「保存」，2026-09-17）
    stSaveGone: !document.getElementById('st-save'),
    // st-retries / st-timeout 两个输入框仍在
    advInputs: ['st-retries','st-timeout']
                 .filter(function(id){ return !!document.getElementById(id); }).length,
    // st-reset-adv 仍在（数值项的"恢复默认"是另一码事，不在本任务范围）
    advResetBtn: !!document.getElementById('st-reset-adv'),
  };`);
  check("高级配置：采样温度与输出预算都已彻底从 DOM 移除（仅留重试 / 超时两项）",
    advClean.temperatureInput === false && advClean.maxTokensInput === false
      && advClean.advBlocks === 2
      && advClean.stSaveGone && advClean.advInputs === 2 && advClean.advResetBtn,
    JSON.stringify(advClean));
  await evalIn(`document.getElementById('btn-open-settings').click(); window.__ts.setPane('gen'); return true;`);
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
    var row = document.querySelector('#llm-list .pl-item.on');
    // hero 的引导态已下线，这里改量**工具条那颗胶囊**：它是「配没配好」这件事
    // 现在唯一的常驻显示位。查计算色而不是 class（原因见 12c 那段注释）。
    var pill = document.querySelector('#model-pick .select-btn');
    return { fields: fields(row).concat(fields(document.getElementById('st-adv'))).sort().join(),
             pillColor: pill ? getComputedStyle(pill).color : null,
             pillWarn: pill ? pill.classList.contains('is-warn') : null };
  })()`);
  // 走**用户真实的路径**：打开这条模型的编辑弹窗 → 填地址与模型名 → 保存。
  // 不能绕过界面直接调接口 —— 那样测的是后端，不是「界面会不会把标记摘掉」。
  // 填 Key 是必须的：不填的话「已配置」这个状态根本不会到来。
  // 2026-09-17（任务 5/6）：md-reset-baseurl 已从 DOM 移除 —— 不能 click 它。
  // 以前这一步是 click 那个按钮，把默认地址**显式**填进框里；现在前端不主动
  // 兜默认地址，用户必须**自己填**。这是"移除默认模型"的产品语义。
  await evalIn(`document.querySelector('#llm-list .pl-item.on').click(); return true;`);
  await sleep(300);
  await evalIn(`var u = document.getElementById('md-baseurl');
    u.value = 'https://x.example/v4';
    u.dispatchEvent(new Event('input', {bubbles:true}));
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
    var row = document.querySelector('#llm-list .pl-item.on');
    var s = document.getElementById('p-model');
    var b = s && s.parentNode.querySelector('.select-btn');
    return { fields: fields(row).concat(fields(document.getElementById('st-adv'))).sort().join(),
             rowTags: row.querySelectorAll('.tag-default').length,
             // 三列化后编辑表单**常驻右列、不再隐藏** —— 所以不再查 dlgHidden。
             // 要守的是「保存后停在刚保存的那条」，而不是「弹窗关了」。
             mdId: document.getElementById('md-id').value,
             rowId: row ? row.dataset.id : null,
             pickerText: b ? b.querySelector('.sel-text').textContent : '',
             pillColor: b ? getComputedStyle(b).color : null,
             pillWarn: b ? b.classList.contains('is-warn') : null };
  })()`);
  // 连接信息配好之后：
  //  · 模型行本来就没挂过「内置默认」标（任务 5）；
  //  · 高级配置那两项**随同一次保存被落盘**（2026-09-17 起模型表单的「保存」
  //    一并调用 saveAdvancedConfig）→ 小标也跟着消失。
  // 「只增不减」的实现在这里给不出 ""（它会留在名单里），直接报红。
  check("配好模型之后小标符合预期（模型行无 tag；数值项随同一次保存被落盘 → 也消失）",
    beforeSave.fields === "retries,timeout"
    && afterSave.fields === "",
    `${beforeSave.fields} → ${afterSave.fields}`);
  // 原来是「保存后弹窗自动关闭」；三列化后没有弹窗了，改成守更有意义的那件事：
  // **保存后要停在刚保存的那条** —— 若跳回「当前启用」那条，用户会以为没保存上。
  check("保存后右列停在刚保存的那条（不是跳回当前启用那条）",
    !!afterSave.rowId && afterSave.mdId === afterSave.rowId,
    JSON.stringify({ mdId: afterSave.mdId, rowId: afterSave.rowId }));
  // 任务 5：picker 不再附「（默认）」后缀，所以"配好前"和"配好后"文本都该是
  // "glm-4.7"。要守的是"配好后**仍然**显示 glm-4.7" —— 不再前后变化。
  check("保存后模型选择器文本不附「（默认）」",
    afterSave.pickerText === "glm-4.7", afterSave.pickerText);

  // 再把「高级配置」也存一次。**这一步不能省**：数值项的小标走的是
  // markDefault（就地增删），与列表行（整行 innerHTML 重建）不是同一套机制，
  // 必须各自验一次「会消失」。
  //
  // 实测漏过一次：把 markDefault 改成「只加不摘」之后，整套断言**全绿** ——
  // 因为「已配置」的页面里它本来就没挂过标，看不出区别。只有在同一个页面里
  // 走一遍「没配 → 配好」，那个「摘」的动作才有东西可摘。
  // 「保存」现在是模型表单那颗（高级配置并入它，2026-09-17）——
  // 先保证表单字段有值，否则会被表单自己的校验拦住。
  await evalIn(`var m = document.getElementById('md-model'); m.value = 'glm-4.7';
    m.dispatchEvent(new Event('input', {bubbles:true}));
    var u = document.getElementById('md-baseurl');
    if (!u.value) { u.value = 'https://x/v4'; u.dispatchEvent(new Event('input', {bubbles:true})); }
    document.getElementById('md-save').click(); return true;`);
  await sleep(900);
  const afterAdv = await evalIn(`return (function(){
    function fields(root){
      return [].concat.apply([], Array.from(root.querySelectorAll('.tag-default'))
        .map(function(t){ return (t.dataset.field || '').split(',').filter(Boolean); }));
    }
    var row = document.querySelector('#llm-list .pl-item.on');
    return { fields: fields(row).concat(fields(document.getElementById('st-adv'))).join(),
             advSub: document.getElementById('st-adv-sub').textContent };
  })()`);
  check("保存后高级配置两项的小标也消失（markDefault 不是只增不减）",
    afterAdv.fields === "" && afterAdv.advSub === "",
    JSON.stringify(afterAdv));
  // 配好 Key 之后胶囊必须从警示橙落回正文色 —— 原来这里守的是「hero 引导态
  // 会不会跟着切回『想聊点什么？』」（一次性引导的 bug）。引导态已下线，
  // 同一个 bug 换了个显示位：配好了还橙着，这个警示就彻底失去可信度。
  check("保存后工具条模型胶囊从警示橙回到正文色（配好了不再误报）",
    beforeSave.pillWarn === true && beforeSave.pillColor === await resolvedToken("--warn")
      && afterSave.pillWarn === false && afterSave.pillColor === "rgb(29, 29, 31)",
    JSON.stringify({ before: beforeSave, after: afterSave }));
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
    var row = document.querySelector('#llm-list .pl-item.on');
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
  // 后端每次保存 / 测试都回 warnings、GET /api/config 常驻 base_url_warnings，
  // 修复前渲染层**一个字都没读**：明文 http 这件事只活在一次性 toast 里。
  const urlWarn = await evalIn(`return (function(){
    var n = document.getElementById('md-warn');
    if (!n) return { missing: true };
    var cs = getComputedStyle(n);
    return { hidden: n.classList.contains('hidden'), display: cs.display,
             text: n.textContent, color: cs.color };
  })()`);
  check("明文地址警告常驻在模型页（不是被顶掉的一次性提示）",
    !urlWarn.missing && urlWarn.hidden !== true && urlWarn.display !== 'none'
      && /明文|http/.test(urlWarn.text),
    JSON.stringify(urlWarn));
  check("警告行用 warn 色（与配置读坏那行同一族）",
    urlWarn.color === await resolvedToken("--warn"), JSON.stringify(urlWarn));
  check("警示用 warn 色且不顶掉原状态行",
    cfgErr.color === await resolvedToken("--warn") && /模型/.test(cfgErr.status),
    JSON.stringify(cfgErr));
  // 读坏与没配过是**同一件事的两个来源**（读坏 = 整个文件读不到 →
  // 高级数值项 + 连接信息都取默认），所以两个信号必须同时出现：
  // 只有橙色警示、界面却不标默认，说明前端只消费了其中一个字段 ——
  // 那正是「信号算对了但没人接」的老毛病。
  // 2026-09-17：temperature / max_tokens 先后从高级配置 UI 移除、
  // 模型行也不再渲染「内置默认」标（任务 4/5）—— 所以读坏时标出来的
  // **是 2 项**（retries / timeout）。
  check("读坏时「内置默认」小标与橙色警示同时出现",
    cfgErr.fields === "retries,timeout",
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
    var row = document.querySelector('#llm-list .pl-item.on');
    var s = document.getElementById('p-model');
    var b = s && s.parentNode.querySelector('.select-btn');
    return { fields: fields(row).concat(fields(document.getElementById('st-adv'))).join(),
             rowTags: row.querySelectorAll('.tag-default').length,
             warnTags: row.querySelectorAll('.tag-warn').length,
             pillColor: b ? getComputedStyle(b).color : null,
             pillWarn: b ? b.classList.contains('is-warn') : null,
             pickerText: b ? b.querySelector('.sel-text').textContent : '' };
  })()`);
  check("已配置时「内置默认」小标全部消失（不误报）",
    cfgd.fields === "" && cfgd.rowTags === 0, JSON.stringify(cfgd));
  // 「未配置 Key」同理不能误报：配好了还挂着它，会让这个警示彻底失去可信度。
  check("已配置时不再报「未配置 Key」（不误报）",
    cfgd.warnTags === 0, JSON.stringify(cfgd));
  // 已配置时 picker 文本不带「（默认）」后缀 —— 任务 5 移除后，
  // 这是个**平直**断言：始终 == "glm-4.7"（不再前后变化）。
  check("已配置时模型名不带「（默认）」后缀（不误报）",
    cfgd.pickerText === "glm-4.7", cfgd.pickerText);
  // Key 输入框的 placeholder 也在弹窗里：已配置时回到「留空即保持不变」。
  // 从没配过时写这句等于在暗示「你已经配过了」—— 两个状态的文案各只有一份，
  // 已配置那句就是 HTML 里的 placeholder（首次打开时存进 data-ph-set）。
  await evalIn(`document.querySelector('#llm-list .pl-item.on').click(); return true;`);
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
    rows: document.querySelectorAll('#llm-list .pl-item').length,
    delHidden: document.getElementById('md-delete').classList.contains('hidden') };`);
  // 2026-09-17：删除键**始终可用**（编辑既有模型时）。
  // 原来「只有一条时禁用」是「总有一条内置默认」时代的规则；
  // 现在模型是用户自己加的，删光就是「还没配」，由列表空引导 +
  // 生成前的 `_require_model` 明确拦住（用户原话「添加完还需要支持删除」）。
  check("编辑既有模型时「删除」始终可用（不再因只剩一条而隐藏）",
    mdlBefore.rows === 1 && mdlBefore.delHidden === false, JSON.stringify(mdlBefore));

  await evalIn(`document.getElementById('st-add-model').click(); return true;`);
  await sleep(250);
  await evalIn(`document.getElementById('md-model').value = 'deepseek-chat';
    document.getElementById('md-name').value = 'DeepSeek';
    document.getElementById('md-baseurl').value = 'https://api.deepseek.com/v1';
    document.getElementById('md-apikey').value = 'sk-dialog';
    document.getElementById('md-save').click(); return true;`);
  await sleep(800);
  const added = await evalIn(`return {
    hidden: document.getElementById('md-form-card').classList.contains('hidden'),
    rows: document.querySelectorAll('#llm-list .pl-item').length,
    ids: Array.from(document.querySelectorAll('#llm-list .pl-item')).map(t => t.dataset.id),
    label2: document.querySelectorAll('#llm-list .pl-item .mdl-label')[1]?.textContent,
    // 服务商不再有独立的 .col-prov 列，合并进 mdl-sub（「模型ID · 服务商」）
    sub2: document.querySelectorAll('#llm-list .pl-item .mdl-sub')[1]?.textContent,
    // 表单常驻右列，所以不再查 hidden；要查的是「停在刚添加的那条上」
    mdId: document.getElementById('md-id').value,
    body: window.__lastModelBody || null };`);
  check("右列保存后列表多出一条，并停在新加的那条上",
    added.rows === 2 && added.ids[1] === "m1" && added.mdId === "m1"
    && added.label2 === "DeepSeek", JSON.stringify(added));
  // 请求体必须只带这一条模型的信息，不能顺手把当前模型的地址也写进去 ——
  // 「同一件事两份表示」正是要避免的。
  check("保存的是**这条**模型（请求体里是它的地址与 Key）",
    !!added.body && added.body.base_url === "https://api.deepseek.com/v1"
    && added.body.model === "deepseek-chat" && added.body.api_key === "sk-dialog",
    JSON.stringify(added.body));
  // 服务商名是从请求地址**推**出来的（参考图里那一列），推不出来就原样显示主机名 ——
  // 硬套一个名字会把「这家」说成「那家」。
  check("服务商按请求地址推断（未命中已知表就显示主机名，写在条目的小字里）",
    /DeepSeek/.test(added.sub2 || ""), added.sub2);

  // 启用：必须真的调切换接口，而不是只把开关点亮
  await evalIn(`document.querySelectorAll('#llm-list .pl-item')[1]
    .querySelector('.mdl-switch').click(); return true;`);
  await sleep(800);
  const act = await evalIn(`return {
    called: window.__lastActivate || '',
    onRows: document.querySelectorAll('#llm-list .pl-item.on').length,
    onId: document.querySelector('#llm-list .pl-item.on')?.dataset.id,
    onSwitches: document.querySelectorAll('#llm-list .pl-item .mdl-switch.on').length,
    checked: Array.from(document.querySelectorAll('#llm-list .pl-item .mdl-switch'))
               .map(b => b.getAttribute('aria-checked')).join() };`);
  // 开关是**单选**语义（同时只有一个生效），所以点一个必须关掉另一个 ——
  // 两个都亮着就是在骗人：用户以为能同时启用两个模型。
  check("点「启用」真的调了切换接口，且同时只有一个亮着",
    act.called === "m1" && act.onRows === 1 && act.onId === "m1"
    && act.onSwitches === 1 && act.checked === "false,true", JSON.stringify(act));

  // 2026-09-17：再点一次**当前已启用**的那条 = **关掉它**。
  // 原来 activateModel 里有一句「已经是当前，别白写一次文件」直接 return ——
  // 于是开关**关不掉**：点了没反应（用户报「模型开启时无法关闭」）。
  // 语义上「启用」是单选，但「都不启用」也是合法状态（想先停用改配置再启用）。
  await evalIn(`document.querySelector('#llm-list .pl-item.on')
    .querySelector('.mdl-switch').click(); return true;`);
  await sleep(800);
  const off = await evalIn(`return {
    called: window.__lastActivate,
    onRows: document.querySelectorAll('#llm-list .pl-item.on').length,
    onSwitches: document.querySelectorAll('#llm-list .pl-item .mdl-switch.on').length,
    status: document.getElementById('st-status').textContent,
    // 输入区那个选择器也要跟上：**不能** fallback 显示第一条的名字
    //（显示与状态不一致 —— 用户以为还有模型在用，点生成才发现被拒）。
    // 标签统一是占位语「选择模型」，「为什么不能用」挪到悬停说明里。
    pickerText: (function(){ var s = document.getElementById('p-model');
      var b = s && s.parentNode.querySelector('.select-btn');
      return b ? b.querySelector('.sel-text').textContent : ''; })(),
    pickerTitle: (function(){ var s = document.getElementById('p-model');
      var b = s && s.parentNode.querySelector('.select-btn');
      return b ? b.title : ''; })(),
    pickerValue: (document.getElementById('p-model') || {}).value };`);
  check("再点一次当前启用的开关 → 真的关掉（都不启用），状态行与输入区选择器都跟上",
    off.called === "" && off.onRows === 0 && off.onSwitches === 0
    && /都没有启用/.test(off.status)
    && off.pickerText === "选择模型" && off.pickerValue === ""
    && /没有启用任何模型/.test(off.pickerTitle),
    JSON.stringify(off));
  // 复原：重新启用 **m1**（它是第 2 条）—— 后面那条断言期望「输入区选择器
  // 跟着变成 m1」。⚠ 别点第一条（m-default），那会把状态复原错。
  await evalIn(`document.querySelectorAll('#llm-list .pl-item')[1]
    .querySelector('.mdl-switch').click(); return true;`);
  await sleep(700);

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
  await evalIn(`document.querySelector('#llm-list .pl-item.on').click(); return true;`);
  await sleep(200);
  await evalIn(`document.getElementById('md-delete').click(); return true;`);
  await sleep(300);
  await evalIn(`document.getElementById('cd-yes').click(); return true;`);
  await sleep(800);
  const mdlAfter = await evalIn(`return {
    rows: document.querySelectorAll('#llm-list .pl-item').length,
    ids: Array.from(document.querySelectorAll('#llm-list .pl-item')).map(t => t.dataset.id),
    delHidden: document.getElementById('md-delete').classList.contains('hidden'),
    onId: document.querySelector('#llm-list .pl-item.on')?.dataset.id };`);
  // 删掉的正是当前启用的那条 → 必须自动换到剩下的一条，不留悬空引用。
  // 悬空的后果是「当前模型」指向一条不存在的记录，生成时取不到任何连接信息。
  // 2026-09-17：只剩一条时「删除」**仍然可用**（删光也是允许的，见后端
  // `test_can_delete_the_last_model`）—— 断言从 `delHidden === true`
  // 改成 `=== false`，守住"能删到空"这条产品语义。
  check("删除当前启用的模型后自动落到剩下那条（且删除键仍可用）",
    mdlAfter.rows === 1 && mdlAfter.ids[0] === "m1"
      && mdlAfter.onId === "m1" && mdlAfter.delHidden === false,
    JSON.stringify(mdlAfter));

  // ── 12g) 空列表：一条模型都没有（2026-09-17 全新安装的真实形态）──────
  // 用户原话：「没有内置默认的模型的，需要用户自己添加，添加完还需要支持删除」。
  // 这一段验完整闭环：空 → 加（地址必填）→ 能删 → 又空。
  // 桩的 `?nomodels=1` 模拟真实后端「文件里没有任何连接信息」时的空列表。
  await cdp.send("Page.navigate",
    { url: `http://127.0.0.1:${PORT}/?token=stubtoken&nomodels=1` });
  await sleep(1800);
  await evalIn(`document.getElementById('btn-open-settings').click();
    window.__ts.setPane('llm'); return true;`);
  await sleep(700);
  const emptyList = await evalIn(`
    var pane = document.getElementById('pane-llm');
    var card = document.getElementById('llm-empty');
    var lr = document.getElementById('llm-list').getBoundingClientRect();
    var cr = card.getBoundingClientRect();
    // 参照盒用 .stg-cols（内容区），**不是** .pane-detail —— 后者自己就被
    // 限宽居中了，拿它当基准量卡片永远是 0，等于什么都没断言。
    var sr = pane.querySelector('.stg-cols').getBoundingClientRect();
    return {
      rows: document.querySelectorAll('#llm-list .pl-item').length,
      // 2026-09-20：空列表时中列**整列收起**，空引导改由居中的空态卡承担
      //（用户报「模型空态时的布局样式」：一句「还没有模型。」顶着一张贴在
      //  左上角的卡，两列在演一个还不存在的层级关系）。
      // 判据因此从「中列有那句文本」换成「中列不可见 + 卡片真的居中」。
      // ⚠ 必须量几何不能量 DOM 文本 —— 文本恰恰是那种查得到却看不见的东西，
      //   上一版断言就是这么假绿的。
      listHidden: lr.width === 0 && lr.height === 0,
      noModelsCls: pane.classList.contains('no-models'),
      cardTitle: (card.querySelector('.pg-empty-t') || {}).textContent || '',
      cardW: Math.round(cr.width), cardH: Math.round(cr.height),
      // 水平 + 垂直居中偏差（卡片中心 − 内容区中心）
      offX: Math.round(cr.left + cr.width / 2 - (sr.left + sr.width / 2)),
      offY: Math.round(cr.top + cr.height / 2 - (sr.top + sr.height / 2)),
      // 2026-09-17：空列表时右列只显示**空态**，不摆「添加模型」表单与高级配置
      //（用户原话「没有模型的时候不应该显示添加啊，还有高级配置」）。
      // ⚠ 判据必须查**可见性**，不能只查 DOM 文本 —— 表单隐藏了 textContent
      //   还在，旧断言因此全绿放过（实测）。
      emptyShown: !card.classList.contains('hidden'),
      formHidden: document.getElementById('md-form-card').classList.contains('hidden'),
      advHidden: document.getElementById('st-adv').classList.contains('hidden'),
      statusText: document.getElementById('st-status').textContent,
      pickerText: (function(){ var s = document.getElementById('p-model');
        var b = s && s.parentNode.querySelector('.select-btn');
        return b ? b.querySelector('.sel-text').textContent : ''; })(),
      pickerTitle: (function(){ var s = document.getElementById('p-model');
        var b = s && s.parentNode.querySelector('.select-btn');
        return b ? b.title : ''; })(),
    };`);
  check("一条模型都没有时：中列收起、空态卡在内容区居中（隐藏表单与高级配置、状态行留空）",
    emptyList.rows === 0 && emptyList.listHidden && emptyList.noModelsCls
      && /还没有配置模型/.test(emptyList.cardTitle)
      && emptyList.cardW > 0 && emptyList.cardH > 0
      && Math.abs(emptyList.offX) <= 2 && Math.abs(emptyList.offY) <= 2
      && emptyList.emptyShown && emptyList.formHidden && emptyList.advHidden
      && emptyList.statusText === ""
      && emptyList.pickerText === "选择模型"
      && /还没有配置模型/.test(emptyList.pickerTitle),
    JSON.stringify(emptyList));

  // ── 12g·b) 输入区那一排控件必须逐项等尺寸（规范 §2·5.6「同层级同呼吸」）
  // 2026-09-20 用户报「字号是不是不协调」：模型胶囊 30px 高 / 13px 字，
  // 邻居是 24px / 11px —— 并排两个控件差 6px 高。判据量**计算值**，
  // 因为这条规则历史上就是被一条 id 选择器悄悄顶掉的。
  const pillRow = await evalIn(`return (function(){
    var bs = document.querySelectorAll('#composer .select-btn');
    var seen = {};
    Array.prototype.forEach.call(bs, function(b){
      var cs = getComputedStyle(b);
      seen[Math.round(b.getBoundingClientRect().height) + "/" + cs.fontSize + "/" + cs.fontWeight] = 1;
    });
    var kinds = Object.keys(seen);
    return { count: bs.length, kinds: kinds, size: kinds.join(",") };
  })()`);
  check("输入区那一排下拉控件高度、字号与字重完全一致（模型胶囊不再比参数胶囊大一档）",
    pillRow.count >= 5 && pillRow.kinds.length === 1
      && pillRow.size === "28/13px/500", JSON.stringify(pillRow));

  // 2026-09-17 二改：空态里**不放按钮**了（用户问「没有配置的时候有两个
  // 添加模型入口，你觉得需要优化吗？」）—— 入口统一留给 headbar 那一颗，
  // 空态只负责说明。判据：空态里 button 数为 0，且 headbar 那颗仍存在。
  const emptyBtns = await evalIn(`return {
    emptyBtnCount: document.querySelectorAll('#llm-empty button').length,
    headbarAdd: !!document.getElementById('st-add-model') };`);
  check("空态里不再有第二个「添加模型」入口（按钮统一在 headbar）",
    emptyBtns.emptyBtnCount === 0 && emptyBtns.headbarAdd,
    JSON.stringify(emptyBtns));

  // 但 headbar 那颗在空列表下要能把表单换出来（addingNew），否则点了没反应
  await evalIn(`document.getElementById('st-add-model').click(); return true;`);
  await sleep(300);
  const afterEmptyAdd = await evalIn(`return {
    emptyHidden: document.getElementById('llm-empty').classList.contains('hidden'),
    formShown: !document.getElementById('md-form-card').classList.contains('hidden'),
    advHidden: document.getElementById('st-adv').classList.contains('hidden'),
    mdTitle: document.getElementById('md-title').textContent };`);
  check("空列表下点 headbar「添加模型」→ 换成表单（高级配置仍隐藏：还没有模型可调）",
    afterEmptyAdd.emptyHidden && afterEmptyAdd.formShown
      && afterEmptyAdd.advHidden && afterEmptyAdd.mdTitle === "添加模型",
    JSON.stringify(afterEmptyAdd));

  // 留一张空态图（用户 2026-09-17 报「没有模型的时候不应该显示添加啊」）——
  // 重新进面板会把 addingNew 归零，回到空态。
  await shot("llm-empty-state.png",
    `document.getElementById('btn-close-settings').click();
     document.getElementById('btn-open-settings').click();
     window.__ts.setPane('llm'); ${closeMenus} return true;`,
    { x: 230, y: 0, width: 1280, height: 830, scale: 1 });

  // 新增时**地址必填** —— 留空要被拒（不再有「内置默认地址」可以兜：
  // 用户加一条 DeepSeek 模型却指向智谱，报错要到生成时才出现）。
  await evalIn(`document.getElementById('st-add-model').click(); return true;`);
  await sleep(250);
  await evalIn(`document.getElementById('md-model').value = 'glm-4.7';
    document.getElementById('md-name').value = '智谱';
    document.getElementById('md-apikey').value = 'sk-new';
    document.getElementById('md-save').click(); return true;`);
  await sleep(700);
  const deniedAdd = await evalIn(`return {
    rows: document.querySelectorAll('#llm-list .pl-item').length,
    toast: document.getElementById('toast').textContent };`);
  check("新增模型留空地址会被拒（不再有内置默认地址可兜）",
    deniedAdd.rows === 0 && /请求地址/.test(deniedAdd.toast), JSON.stringify(deniedAdd));

  await evalIn(`document.getElementById('md-baseurl').value = 'https://open.bigmodel.cn/api/paas/v4';
    document.getElementById('md-save').click(); return true;`);
  await sleep(800);
  const addedFirst = await evalIn(`return {
    rows: document.querySelectorAll('#llm-list .pl-item').length,
    id: document.querySelector('#llm-list .pl-item')?.dataset.id,
    active: document.querySelectorAll('#llm-list .pl-item.on').length,
    mdId: document.getElementById('md-id').value,
    delHidden: document.getElementById('md-delete').classList.contains('hidden') };`);
  check("空列表加一条：出现在中列、成为当前启用、删除键可用",
    addedFirst.rows === 1 && addedFirst.id === "m1" && addedFirst.active === 1
      && addedFirst.mdId === "m1" && addedFirst.delHidden === false,
    JSON.stringify(addedFirst));

  await evalIn(`document.getElementById('md-delete').click(); return true;`);
  await sleep(300);
  await evalIn(`document.getElementById('cd-yes').click(); return true;`);
  await sleep(800);
  const backToEmpty = await evalIn(`
    var lr = document.getElementById('llm-list').getBoundingClientRect();
    return {
      rows: document.querySelectorAll('#llm-list .pl-item').length,
      // 删到空要**整个回到空态布局**（中列收起），不是只把列表清空留个孤列。
      // 这里不查空态卡：删除流程停在 addingNew，右列显示的是「添加模型」表单。
      listHidden: lr.width === 0 && lr.height === 0,
      noModelsCls: document.getElementById('pane-llm').classList.contains('no-models'),
      mdTitle: document.getElementById('md-title').textContent };`);
  check("删掉最后一条 → 回到空态布局（中列收起；允许删到空，不再强制「至少保留一条」）",
    backToEmpty.rows === 0 && backToEmpty.listHidden && backToEmpty.noModelsCls
      && backToEmpty.mdTitle === "添加模型",
    JSON.stringify(backToEmpty));
  await evalIn(`document.getElementById('btn-close-settings').click(); return true;`);
  await sleep(200);
  await evalIn(`document.getElementById('btn-close-settings').click(); return true;`);
  await sleep(200);
  // 冷启动就配好的情形（老用户）：不该看到任何"还没配好"的脸色。
  // 原来这条查的是 hero 有没有停在配置引导上；引导态下线后，同一个"不误报"
  // 要求落在工具条那颗胶囊上 —— 配好了还挂着 is-warn / 橙色，用户会以为没存上。
  check("已配置时冷启动模型胶囊不报警示（不误报）",
    cfgd.pillWarn === false && cfgd.pillColor === "rgb(29, 29, 31)",
    JSON.stringify({ color: cfgd.pillColor, warn: cfgd.pillWarn }));
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
    packErr.color === await resolvedToken("--warn"), JSON.stringify(packErr));
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

  // ── 12h) 409 有两种，屏幕上必须是两句话（P1-1）─────────────────
  // 真后端在**开跑前**就能给两种 409：并发额度满（app/pipeline.py 的 StateConflict）
  // 与行业包坏了（app/knowledge.py 的 PackBrokenError，状态码由 app/server.py 映射）。
  // 修复前渲染层判的是 `status === 409`，于是坏包被说成「同时进行的生成已达上限」，
  // 而服务端那句带行列号的原话**整句丢掉** —— 两句一模一样的话，两种完全不同的病。
  // 现在判据读 code，所以桩也必须给得出 code（上面 errc 那一段），否则这组断言
  // 量到的是"桩里没有 code"，全绿而什么都没验证。
  const typeAndSend = async (topic) => {
    await evalIn(`window.__ts.newChat();
      const el0 = document.getElementById('topic');
      el0.value = ${JSON.stringify(topic)};
      el0.dispatchEvent(new Event('input', { bubbles: true }));
      document.getElementById('btn-generate').click(); return true;`);
    await sleep(500);
    return evalIn(`return { toast: document.getElementById('toast').textContent,
      busy: window.__ts.busy, jobId: window.__ts.jobId,
      hint: document.getElementById('composer-gen-hint').textContent };`);
  };
  const quotaCase = await typeAndSend('额度测试：这条应该被队列挡住');
  check("额度满的 409 说的是队列，且界面立刻可用",
    /额度/.test(quotaCase.toast) && !/pack\.yaml/.test(quotaCase.toast)
      && !quotaCase.busy && !quotaCase.jobId, JSON.stringify(quotaCase));
  const brokenCase = await typeAndSend('坏包测试：这条应该说出是哪个包坏了');
  check("坏包的 409 把引擎那句原话说全（包名 + 文件名 + 行列号）",
    /pack\.yaml/.test(brokenCase.toast) && /第 4 行第 6 列/.test(brokenCase.toast)
      && /行业包/.test(brokenCase.toast), JSON.stringify(brokenCase));
  check("坏包没有被说成队列满（这两句话必须不同）",
    !/已达上限/.test(brokenCase.toast) && !/额度/.test(brokenCase.toast)
      && brokenCase.toast !== quotaCase.toast, JSON.stringify(brokenCase));
  // 对照：**认不出 code** 的 409（老服务 / 第三种原因）谁都不许冒充。
  // 这一条钉的是修法的另一半 —— 不是把「不是额度」一律改说成「包坏了」，
  // 而是没有证据时就把服务端那句话原样递出去。
  const noCodeCase = await typeAndSend('结构坏了：这条应答里没有 code');
  check("没有 code 的 409 两条现成话都不套，只回服务端原话",
    /结构不符合约定/.test(noCodeCase.toast) && !/已达上限/.test(noCodeCase.toast)
      && !/pack\.yaml 语法有误/.test(noCodeCase.toast), JSON.stringify(noCodeCase));

  // ── 12i) 被遗弃的单段重写不许解锁**别人**的作业（P1-2）──────────
  // 现场：一条重写在轮询中，用户切到另一条**还在跑**的作业。
  // 修复前 waitRewriteDone 遇到切走是「静默 return」，调用方于是照常
  // setBusy(false) + stopTicker() —— 把刚接上的那条作业的按下键、参数锁、
  // 计时器全松掉了，而后台还在跑；同时 toast 一句「已重写并复检」。
  // 现在所有权跟着**正在被轮询的那条**走：切走的那条一行都不许碰。
  await evalIn(`window.__ts.newChat();
    const el0 = document.getElementById('topic');
    el0.value = '家用电梯怎么挑？';
    el0.dispatchEvent(new Event('input', { bubbles: true }));
    document.getElementById('btn-generate').click(); return true;`);
  await sleep(3000);                       // 等这一条跑完（桩第 3 拍落 done）
  const rwReady = await evalIn(`return { busy: window.__ts.busy,
    jobId: window.__ts.jobId, cards: document.querySelectorAll('.script-card').length,
    rw: !!document.querySelector('.card-foot .rw') };`);
  check("前提：有一条已完成且可局部重写的作业", rwReady.busy === false
    && rwReady.jobId === 'job1' && rwReady.cards >= 3 && rwReady.rw, JSON.stringify(rwReady));
  const rwBefore = await evalIn(`return { rewrite: window.__calls.rewrite || 0,
    job: window.__calls.job || 0 };`);
  await evalIn(`document.querySelector('.card-foot .rw').click();
    const i = document.querySelector('.card-foot .rw-feedback');
    i.value = '短一点'; i.dispatchEvent(new KeyboardEvent('keydown',
      { key: 'Enter', bubbles: true })); return true;`);
  await sleep(150);
  // 在重写自己的下一拍轮询（700ms）之前切到另一条**在跑**的作业
  await evalIn(`return (async () => { return await window.__ts.reattachBusyJob('jobkeep'); })();`);
  const toastSamples = [];
  for (let i = 0; i < 6; i++) {
    await sleep(450);
    toastSamples.push(await evalIn(`return document.getElementById('toast').textContent;`));
  }
  const detached = await evalIn(`return (function(){
    var body = document.querySelector('.msg.msg-assistant:last-of-type .msg-body');
    var gs = body && body.querySelector('#gen-status');
    var ge = gs && gs.querySelector('#gen-elapsed');
    var btn = document.getElementById('btn-generate');
    return { busy: window.__ts.busy, jobId: window.__ts.jobId,
      uiState: (document.getElementById('rh-state')||{}).textContent,
      genStatus: !!gs, elapsed: ge ? ge.textContent : '',
      stopping: btn.classList.contains('stopping'), btnDisabled: btn.disabled,
      topicLocked: document.getElementById('topic').disabled,
      paramLocked: !!document.querySelector('#quick-params.locked'),
      hint: document.getElementById('composer-gen-hint').textContent,
      rewriteSent: (window.__calls.rewrite || 0) - ${rwBefore.rewrite} };
  })()`);
  check("切走后仍在轮询的那条作业保持「生成中」（被遗弃的重写没解锁它）",
    detached.busy === true && detached.jobId === 'jobkeep' && detached.genStatus
      && detached.stopping && !detached.btnDisabled && detached.topicLocked
      && detached.paramLocked && /Esc 可停止/.test(detached.hint),
    JSON.stringify(detached));
  check("被遗弃的重写不报「已重写并复检」",
    detached.rewriteSent === 1 && !toastSamples.some(s => /已重写/.test(s)),
    JSON.stringify({ sent: detached.rewriteSent, seen: toastSamples.filter(s => /已重写/.test(s)) }));
  await sleep(1200);
  const elapsedLater = await evalIn(`return (document.querySelector('.msg.msg-assistant:last-of-type #gen-elapsed')||{}).textContent;`);
  check("重挂着的作业计时器还在走（不是冻住的读数）",
    /^已用 [0-9]+ 秒$/.test(detached.elapsed) && /^已用 [0-9]+ 秒$/.test(elapsedLater)
      && elapsedLater !== detached.elapsed,
    JSON.stringify({ t1: detached.elapsed, t2: elapsedLater }));
  // Esc 停的必须是**正在被轮询的那一条**（修复前 busy 已被清成 false → 停掉 0 条）
  await evalIn(`document.dispatchEvent(new KeyboardEvent('keydown',
    { key: 'Escape', bubbles: true })); return true;`);
  await sleep(900);
  const escKeep = await evalIn(`return { keepcancel: window.__calls.keepcancel || 0,
    keepjob: window.__calls.keepjob || 0,
    busy: window.__ts.busy, jobId: window.__ts.jobId };`);
  // keepjob >= 1 是这条断言的**前提**被说清楚：界面必须先轮询过这条作业，才有资格
  // 说"在跑的是它"；只看 keepcancel===1 的话，"停了一条从没轮询过的作业"也算过。
  check("Esc 停掉的正是界面上在跑的那一条（jobkeep）",
    escKeep.keepcancel === 1 && escKeep.keepjob >= 1 && escKeep.busy === false && !escKeep.jobId,
    JSON.stringify(escKeep));

  // ── 12j) 就地编辑主题里按 Esc 不许停掉作业（P2-6）───────────────
  await evalIn(`window.__ts.newChat();
    const el0 = document.getElementById('topic');
    el0.value = '停不掉的作业';
    el0.dispatchEvent(new Event('input', { bubbles: true }));
    document.getElementById('btn-generate').click(); return true;`);
  await sleep(500);
  const inlineEsc = await evalIn(`return (function(){
    var before = window.__calls.failcancel || 0;
    var m = document.querySelector('.msg.msg-user');
    m.querySelector('.msg-edit').click();
    var ta = m.querySelector('.bub-edit');
    var had = !!ta;
    ta.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
    return { had: had, closed: !m.querySelector('.bub-edit'),
      busy: window.__ts.busy, jobId: window.__ts.jobId,
      newCancels: (window.__calls.failcancel || 0) - before };
  })()`);
  check("编辑框里的 Esc 只收编辑器，不停掉正在跑的作业",
    inlineEsc.had && inlineEsc.closed && inlineEsc.busy && inlineEsc.jobId === 'jobrun'
      && inlineEsc.newCancels === 0, JSON.stringify(inlineEsc));

  // ── 12k) 停止失败之后「重新连接并查看状态」要把 DOM 也接回去（P2-3）
  await evalIn(`document.getElementById('btn-generate').click(); return true;`);  // 停止（注定失败）
  await sleep(700);
  const stopFail = await evalIn(`return (function(){
    var b = Array.prototype.slice.call(document.querySelectorAll('.fail-actions button'))
      .filter(function(x){ return /重新连接/.test(x.textContent); })[0];
    return { card: /停止失败，后台可能仍在运行/.test(document.body.innerText),
      hasReconnect: !!b, busy: window.__ts.busy,
      hint: document.getElementById('composer-gen-hint').textContent };
  })()`);
  check("停止失败给出失败卡与「重新连接并查看状态」，并把界面解锁",
    stopFail.card && stopFail.hasReconnect && stopFail.busy === false,
    JSON.stringify(stopFail));
  await evalIn(`Array.prototype.slice.call(
      document.querySelectorAll('.fail-actions [data-act="retry"]'))
    .filter(function(x){ return /重新连接/.test(x.textContent); })[0].click();
    return true;`);
  await sleep(500);
  const reconnected = await evalIn(`return (function(){
    var body = document.querySelector('.msg.msg-assistant:last-of-type .msg-body');
    var gs = body && body.querySelector('#gen-status');
    return { busy: window.__ts.busy, jobId: window.__ts.jobId,
      genStatus: !!gs, genStatusHidden: gs ? gs.classList.contains('hidden') : null,
      steps: body ? body.querySelectorAll('#step-track .step').length : -1,
      stillFailCard: /停止失败，后台可能仍在运行/.test((body||{}).innerText || ''),
      topicLocked: document.getElementById('topic').disabled,
      hint: document.getElementById('composer-gen-hint').textContent,
      elapsed: (body && body.querySelector('#gen-elapsed'))
        ? body.querySelector('#gen-elapsed').textContent : '' };
  })()`);
  check("重连之后卡片与状态行同源：失败卡没了、进度骨架回来了",
    reconnected.busy === true && reconnected.jobId === 'jobrun' && reconnected.genStatus
      && reconnected.genStatusHidden === false && reconnected.steps >= 1
      && reconnected.stillFailCard === false && reconnected.topicLocked === true
      && /生成中，参数已锁定/.test(reconnected.hint), JSON.stringify(reconnected));
  await sleep(1400);
  const reconnected2 = await evalIn(`return (document.querySelector('.msg.msg-assistant:last-of-type #gen-elapsed')||{}).textContent;`);
  check("重连后「已用 N 秒」继续走（计时器一起接回来了）",
    /^已用 [0-9]+ 秒$/.test(reconnected2) && reconnected2 !== reconnected.elapsed,
    JSON.stringify({ a: reconnected.elapsed, b: reconnected2 }));
  await evalIn(`window.__ts.newChat(); return true;`);   // 放手这条永远在跑的作业

  // ── 12l) 占位符只标一层「待补：」，定位跳到占位符本身（P3-8 / P3-9）
  await evalIn(`window.__ts.newChat();
    const el0 = document.getElementById('topic');
    el0.value = '占位与超配额';
    el0.dispatchEvent(new Event('input', { bubbles: true }));
    document.getElementById('btn-generate').click(); return true;`);
  await sleep(900);
  const phCase = await evalIn(`return (function(){
    var jump = document.querySelector('.script-card .over.jumpable');
    var chip = document.querySelector('.script-card .quota.over');
    var banner = document.querySelector('[data-jump="placeholder"]');
    var r = window.__ts.result;
    var md = r ? window.__ts.exportMd(r) : '';
    if (!jump || !banner) return { missing: true, jump: !!jump, banner: !!banner };
    banner.click();
    var flashed = document.querySelectorAll('.over.flash');
    return { screen: jump.textContent,
      chipText: chip ? chip.textContent : '',
      chipBeforeJump: !!chip && !!(jump.compareDocumentPosition
        && (chip.compareDocumentPosition(jump) & Node.DOCUMENT_POSITION_FOLLOWING)),
      jumped: jump.classList.contains('flash'),
      flashedIsChip: !!document.querySelector('.quota.over.flash'),
      flashedN: flashed.length,
      mdHasDouble: /待补：待补：/.test(md), mdHasIt: md.indexOf('待补：主力机型载重') >= 0,
      engineLabel: (r.placeholders || [])[0] };
  })()`);
  check("卡片上的占位符只有一层「待补：」，与引擎给的那份一字不差",
    !phCase.missing && phCase.screen === '{{待补：主力机型载重}}'
      && phCase.engineLabel === '{{待补：主力机型载重}}', JSON.stringify(phCase));
  check("屏幕与导出（Markdown）说的是同一份文本（导出不曾错，错的是屏幕）",
    !phCase.missing && phCase.mdHasIt && !phCase.mdHasDouble, JSON.stringify(phCase));
  check("「点击定位首处」跳到占位符，不是文档里更靠前的字数胶囊",
    !phCase.missing && phCase.chipBeforeJump && phCase.jumped
      && phCase.flashedIsChip === false && phCase.flashedN === 1,
    JSON.stringify(phCase));

  // ── 12m) 设置页是一张**焦点圈内**的模态（P2-7）──────────────────
  // ⚠ 先 focus 再 click：程序化的 .click() 在 Chrome 里**不**移动焦点，
  //   而「关掉设置把焦点还给打开它的那颗按钮」这条断言要的就是那个落点。
  await evalIn(`var g = document.getElementById('btn-open-settings');
    g.focus(); g.click(); return true;`);
  await sleep(400);
  const modal = await evalIn(`return (function(){
    var s = document.getElementById('settings-screen');
    var onOpen = document.activeElement ? document.activeElement.id : '';
    var inPanelOnOpen = s.contains(document.activeElement);
    document.dispatchEvent(new KeyboardEvent('keydown',
      { key: 'k', ctrlKey: true, bubbles: true }));
    var afterK = document.activeElement ? document.activeElement.id : '';
    var inPanelAfterK = s.contains(document.activeElement);
    // 把焦点人工丢到模态**背后**，再按一次 Tab：焦点圈必须把它拉回来
    document.getElementById('btn-open-settings').focus();
    document.getElementById('btn-open-settings')
      .dispatchEvent(new KeyboardEvent('keydown', { key: 'Tab', bubbles: true }));
    var pulled = s.contains(document.activeElement);
    // 这份选择器必须与 settings.js 的 FOCUSABLE 逐项一致，否则"最后一个"
    // 量的就不是焦点圈里的最后一个，绕回第一条也就无从谈起。
    var tabbable = Array.prototype.slice.call(document.querySelectorAll(
      '#settings-screen a[href], #settings-screen button:not([disabled]), '
      + '#settings-screen input:not([disabled]), #settings-screen select:not([disabled]), '
      + '#settings-screen textarea:not([disabled]), '
      + '#settings-screen [tabindex]:not([tabindex="-1"])'))
      .filter(function(x){ return x.getClientRects().length > 0; });
    var last = tabbable[tabbable.length - 1];
    last.focus();
    last.dispatchEvent(new KeyboardEvent('keydown',
      { key: 'Tab', bubbles: true, cancelable: true }));
    var wrappedFirst = document.activeElement === tabbable[0];
    return { role: s.getAttribute('role'), modalAttr: s.getAttribute('aria-modal'),
      label: s.getAttribute('aria-label'), onOpen: onOpen, inPanelOnOpen: inPanelOnOpen,
      afterK: afterK, inPanelAfterK: inPanelAfterK, pulled: pulled,
      wrappedFirst: wrappedFirst, n: tabbable.length,
      toast: document.getElementById('toast').textContent };
  })()`);
  check("设置页声明成模态（role=dialog + aria-modal + 名字）",
    modal.role === 'dialog' && modal.modalAttr === 'true' && !!modal.label,
    JSON.stringify(modal));
  check("打开设置时焦点进面板（不再留在背后那颗齿轮上）",
    modal.inPanelOnOpen && modal.onOpen === 'btn-close-settings', JSON.stringify(modal));
  check("设置页开着按 Ctrl+K 不把焦点丢到背后的会话搜索框",
    modal.afterK !== 'sess-search' && modal.inPanelAfterK && /返回工作区/.test(modal.toast),
    JSON.stringify(modal));
  check("Tab 圈在面板里：背后的控件被拉回来、末尾那一个绕回第一个",
    modal.pulled && modal.wrappedFirst && modal.n > 3, JSON.stringify(modal));
  await evalIn(`document.getElementById('btn-close-settings').click(); return true;`);
  await sleep(300);
  const afterClose = await evalIn(`return { open: window.__ts.settingsOpen,
    active: document.activeElement ? document.activeElement.id : '',
    inSettings: document.getElementById('settings-screen').contains(document.activeElement) };`);
  check("关掉设置：焦点还给打开它的那颗齿轮",
    afterClose.open === false && afterClose.active === 'btn-open-settings'
      && afterClose.inSettings === false, JSON.stringify(afterClose));
  await evalIn(`document.dispatchEvent(new KeyboardEvent('keydown',
    { key: 'k', ctrlKey: true, bubbles: true })); return true;`);
  await sleep(200);
  const kWorks = await evalIn(`return document.activeElement ? document.activeElement.id : '';`);
  check("对照：工作区里 Ctrl+K 仍然聚焦会话搜索（没被一并修死）",
    kWorks === 'sess-search', kWorks);

  // ── 12n) 「停止全部」的计数句不能再漏字（P2-4）──────────────────
  // 原来写的是 failed.length===1 ? "" : "共 " → 两条失败时读成
  // 「2 条没停成（共 可稍后重试）」。这条断言把这句话钉住。
  const stopAllCase = await evalIn(`return (async () => {
    window.__ts.newChat();
    var pad = function(n){ return String(n).padStart(2,'0'); };
    var d = new Date();
    var iso = d.getFullYear() + '-' + pad(d.getMonth()+1) + '-' + pad(d.getDate()) + 'T09:00:00';
    var base = { created_at: iso, pack: 'elevator', platform: '抖音',
                 duration: null, chars: null, passed: null, error: null };
    var two = [ Object.assign({}, base, { id:'st1', topic:'停不掉的甲', state:'writing' }),
                Object.assign({}, base, { id:'st2', topic:'停不掉的乙', state:'writing' }) ];
    var orig = window.fetch;
    window.fetch = function (u, o) {
      var s = String(u).split('?')[0];
      if (s.indexOf('/api/history') >= 0) {
        return Promise.resolve(new Response(JSON.stringify(two),
          { status:200, headers:{ 'Content-Type':'application/json' } }));
      }
      if (s.indexOf('/cancel') >= 0) {
        return Promise.resolve(new Response(JSON.stringify({ detail:'引擎没接这个取消' }),
          { status:500, headers:{ 'Content-Type':'application/json' } }));
      }
      return orig.call(window, u, o);
    };
    await window.__ts.loadSessions();
    var n = window.__ts.busyRecords().length;
    await window.__ts.stopAll();
    var t = document.getElementById('toast').textContent;
    window.fetch = orig;
    await window.__ts.loadSessions();
    return { n: n, toast: t };
  })();`);
  check("「N 条没停成」这句里没有孤零零的「共」字（也不许有别的漏字）",
    stopAllCase.n === 2 && /2 条没停成/.test(stopAllCase.toast)
      && !/共/.test(stopAllCase.toast) && /可稍后重试/.test(stopAllCase.toast),
    JSON.stringify(stopAllCase));

  // ── 12o) 建包取消失败后按钮必须收回「取消中…」（P3-10）───────────
  await cdp.send("Page.navigate",
    { url: `http://127.0.0.1:${PORT}/?token=stubtoken&pgcancelfail=1` });
  await sleep(1800);
  await evalIn(`document.getElementById('btn-open-settings').click(); return true;`);
  await sleep(300);
  await evalIn(`window.__ts.setPane('packinfo'); return true;`);
  await sleep(300);
  await evalIn(`document.getElementById('pi-newpack').click(); return true;`);
  await sleep(200);
  await evalIn(`document.getElementById('pg-industry').value = '全屋定制/装修';
    document.getElementById('pg-desc').value = '全屋定制家居品牌，面向新房装修业主获客';
    document.getElementById('pg-run').click(); return true;`);
  await sleep(1200);
  const pgCancelOptimistic = await evalIn(`return (function(){
    document.getElementById('pg-close').click();
    var b = document.getElementById('pg-run');
    return { label: b.textContent, disabled: b.disabled };
  })()`);
  await sleep(600);
  const pgCancelFailed = await evalIn(`return (function(){
    var b = document.getElementById('pg-run');
    return { label: b.textContent, disabled: b.disabled,
      fails: window.__calls.failpgcancel || 0,
      toast: document.getElementById('toast').textContent };
  })()`);
  check("点「取消」那一拍先给乐观反馈（取消中…）",
    /取消中/.test(pgCancelOptimistic.label), JSON.stringify(pgCancelOptimistic));
  check("取消请求失败后按钮收回「取消中…」、重新可用，并说清还在生成",
    pgCancelFailed.fails === 1 && !/取消中/.test(pgCancelFailed.label)
      && /生成中/.test(pgCancelFailed.label) && pgCancelFailed.disabled === false
      && /取消失败/.test(pgCancelFailed.toast) && /仍在生成/.test(pgCancelFailed.toast),
    JSON.stringify(pgCancelFailed));
  await cdp.send("Page.navigate", { url: `http://127.0.0.1:${PORT}/?token=stubtoken` });
  await sleep(1600);
  await evalIn(`if (window.__ts.settingsOpen) document.getElementById('btn-close-settings').click();
    window.__ts.newChat(); return true;`);
  await sleep(300);

  // ── 12p) 桩自己的建包占位必须按作业归还（第 10 轮复核 P2）───────────
  // 桩原来用**一个全局变量**记"这次提交占住了哪个目录名"：两次不同行业的提交会互相
  // 覆盖归还用的键，第一次那个名字就永久卡在"正在创建中" —— 桩凭空造出一个真引擎
  // 不会给、而且再也退不掉的 409。这条断言直接量桩（不绕界面），要的是界面走不到的
  // "并发两次"形态：A 收工**不能**把 B 还在排的队放掉，B 也**不能**被 A 的收工误放。
  const pgTwoJobs = await evalIn(`return (async () => {
    var api = function (u, body) {
      return window.fetch(u, { method: body ? 'POST' : 'GET',
        headers: { 'Content-Type': 'application/json' },
        body: body ? JSON.stringify(body) : undefined })
        .then(function (r) { return r.json().then(function (b) { return { st: r.status, b: b }; }); });
    };
    var newPack = function (ind) {
      return api('/api/packs/create', { industry: ind, description: ind + '，面向周边居民获客' });
    };
    var pollTo = function (id) {
      var last = null;
      var one = function (i) {
        if (i > 8 || (last && last.b.state === 'done')) return Promise.resolve(last);
        return api('/api/jobs/' + id).then(function (r) { if (r.b.state) last = r; return one(i + 1); });
      };
      return one(0).then(function () { return last; });
    };
    var a = await newPack('猫咖甲');
    var dup0 = (window.__calls && window.__calls.pgdup) || 0;
    var b = await newPack('口腔诊所乙');
    var ida = a.b.job_id, idb = b.b.job_id;
    var ra = await pollTo(ida);                    // A 先收工
    var dupA = await newPack('猫咖甲');            // A 的占位必须已经还掉：还留着就报"正在创建中"
    var dupB = await newPack('口腔诊所乙');        // B 还在跑：这个名字必须仍然报"正在创建中"
    var midB = await api('/api/jobs/' + idb);      // B 的作业不能被 A 的收工改动
    var rb = await pollTo(idb);
    var dupB2 = await newPack('口腔诊所乙');       // B 收工后占位才归还
    var stillThere = await api('/api/jobs/' + ida);  // 收工过的作业还在注册表里（真引擎 prune 才删）
    var cancelDone = await api('/api/jobs/' + ida + '/cancel', {});  // 幂等：不许把 done 改成 cancelled
    var detailGone = await api('/api/packs/' + '猫咖甲');            // 桩没真建目录：未知包必须 404
    return { sa: a.st, sb: b.st, distinct: ida !== idb && !!ida, ra: ra && ra.b.state,
             dupA: dupA.st, dupAwhy: String(dupA.b.detail || '').slice(0, 7),
             dupB: dupB.st, dupBwhy: String(dupB.b.detail || '').slice(0, 7),
             midB: midB.b.state, rb: rb && rb.b.state,
             dupB2: dupB2.st, dupB2why: String(dupB2.b.detail || '').slice(0, 7),
             still: stillThere.st + ':' + (stillThere.b.state || ''),
             cancelState: cancelDone.st + ':' + (cancelDone.b.state || ''),
             detail: detailGone.st + ':' + (detailGone.b.code || ''),
             indA: (ra.b.params || {}).industry, indB: (rb.b.params || {}).industry,
             dupHits: ((window.__calls && window.__calls.pgdup) || 0) - dup0,
             idA2: dupA.b.job_id, idB2: dupB2.b.job_id };
  })();`);
  // 引擎在包目录已存在时报的是「行业包已存在」，只有仍在创建中才报「正在创建中」
  // （app/packgen.py 两条 raise）。桩原来只有一句，于是"占位到底还不还"这件事量不出来：
  // 还不还都是 409。现在两句都在，判据按**措辞**分，不按状态码分。
  check("桩：并发两次建包各自归还自己的占位（A 收工不放 B 的队，也不误伤 B 的作业）",
    pgTwoJobs.sa === 200 && pgTwoJobs.sb === 200 && pgTwoJobs.distinct === true
      && pgTwoJobs.ra === 'done'
      && pgTwoJobs.dupA === 409 && pgTwoJobs.dupAwhy === '行业包已存在：'
      && pgTwoJobs.dupB === 409 && pgTwoJobs.dupBwhy === '行业包正在创建'
      && pgTwoJobs.midB === 'packing' && pgTwoJobs.rb === 'done'
      && pgTwoJobs.dupB2 === 409 && pgTwoJobs.dupB2why === '行业包已存在：'
      // "排队中"这个理由必须**只**给还在跑的那一条：整个场景里 pgdup 只能 +1
      // （dupA / dupB2 走的是"目录已存在"那句，不算重复排队）。
      && pgTwoJobs.dupHits === 1
      && pgTwoJobs.still === '200:done'
      && pgTwoJobs.cancelState === '200:done'
      && pgTwoJobs.detail === '404:pack_missing'
      && pgTwoJobs.indA === '猫咖甲' && pgTwoJobs.indB === '口腔诊所乙',
    JSON.stringify(pgTwoJobs));

  // 包名这一格也要有断言，否则"桩回不回 404"随时可以静默退回宽容（第 10 轮 P3）：
  // 真引擎对不存在的包是 404，且**先判包、再判 private**。
  const pgFileGate = await evalIn(`return (async () => {
    var api = function (u, body) {
      return window.fetch(u, { method: body ? 'POST' : 'GET',
        headers: { 'Content-Type': 'application/json' },
        body: body ? JSON.stringify(body) : undefined })
        .then(function (r) { return r.json().then(function (b) { return { st: r.status, b: b }; }); });
    };
    var st = function (u) { return api(u).then(function (r) { return r.st; }); };
    var out = {
      realYaml: await st('/api/packs/elevator/file?rel=pack.yaml'),
      noPack: await st('/api/packs/从没建过的包/file?rel=pack.yaml'),
      noPackPriv: await st('/api/packs/从没建过的包/file?rel=private/x.md'),
      priv: await st('/api/packs/elevator/file?rel=private/pricing.md'),
      noRel: await st('/api/packs/elevator/file'),
      badName: await st('/api/packs/%2e%2e/file?rel=pack.yaml'),
      // 详情路由（不带 /file）问的是同一个 packNameOk，但它是**第二个调用点**：
      // 只钉 /file 那一侧时，把详情那一侧的判据删掉/改错，门禁照绿（第 16 轮复核 P2-5）。
      badNameDet: await st('/api/packs/%2e%2e'),
      unkDet: await st('/api/packs/mei-you-zhe-ge-bao'),
      listed: Object.keys(window.__tsMeta.packs.reduce(function (m, p) { m[p.name] = 1; return m; }, {})).length
    };
    // 未知 id 的两句 404 分属两条路由，文案不同（server.py 的 job_status / job_cancel）：
    // 按**路由**比，不是"文件里出现过这句就算过"。
    var g404 = await api('/api/jobs/20260101-000000-abcdef');
    var c404 = await api('/api/jobs/20260101-000000-abcdef/cancel', {});
    out.getCode = g404.st + ':' + g404.b.detail;
    out.cancelCode = c404.st + ':' + c404.b.detail;
    // 同一件事在**建包那两条路由**上也要量一次：桩里带 jobpg 前缀的 id 走的是另一支
    // 分支（PG_JOBS 查不到就当场 404），探针不含 jobpg 就永远打不到那一支
    // —— 那一支写错（两句互换、或都发同一句）今天没人知道（第 16 轮复核 P1-3）。
    var gpg = await api('/api/jobs/jobpg-nope');
    var cpg = await api('/api/jobs/jobpg-nope/cancel', {});
    out.pgGet = gpg.st + ':' + gpg.b.detail;
    out.pgCancel = cpg.st + ':' + cpg.b.detail;
    // 终态快照的字段集必须与引擎一致（缺一个键，界面那条"读不到就算了"的分支永不跑）
    var sub = await api('/api/packs/create', { industry: '推拿所戊', description: '社区推拿，面向上班族' });
    var sid = sub.b.job_id;
    var infl = await api('/api/jobs/' + sid);
    out.inKeys = Object.keys(infl.b || {}).sort().join(',');
    out.inSteps = ((infl.b || {}).steps || []).length;
    out.inHasCreated = !!((infl.b || {}).created_at);
    await api('/api/jobs/' + sid + '/cancel', {});
    var term = await api('/api/jobs/' + sid);
    out.termKeys = Object.keys(term.b || {}).sort().join(',');
    out.termResult = 'result' in (term.b || {}) ? String(term.b.result) : 'ABSENT';
    out.termStream = term.b && term.b.stream ? term.b.stream.phase : '';
    // 出厂值那一档也得走一遍：不注入旋钮时（走的是 PG_CLAIM_MS 那个时限）刚提交的作业
    // **不该**被回收、名字该还占着；把旋钮注入成 0 之后同一条才落 failed 并归还。
    // 原来只有 lim=0 那一支被量过，于是"把 PG_CLAIM_MS 改成极大值（当场判死 / 永不回收）"
    // 这类改动手感上是红的、实际上门禁照绿（第 16 轮复核 P2-7）。
    var sLive = await api('/api/packs/create', { industry: '推拿所己', description: '社区推拿，面向上班族' });
    var live = await api('/api/jobs/' + sLive.b.job_id);
    out.liveState = live.b.state;
    out.claimMs = window.PG_CLAIM_MS_DEFAULT;   // 出厂值本身也要被看住（见下面那条 check）
    out.liveResub = (await api('/api/packs/create',
      { industry: '推拿所己', description: '社区推拿，面向上班族' })).st;
    window.__pgclaimms = 0;
    var liveSwept = await api('/api/jobs/' + sLive.b.job_id);
    out.liveSweptState = liveSwept.b.state;
    out.liveAfter = (await api('/api/packs/create',
      { industry: '推拿所己', description: '社区推拿，面向上班族' })).st;
    // 占位回收线：把时限注入成 0，让它当场触发 —— 否则这句永远没有断言覆盖
    window.__pgclaimms = 0;
    var s2 = await api('/api/packs/create', { industry: '宠物医院庚', description: '社区医院，面向养宠家庭' });
    var swept = await api('/api/jobs/' + s2.b.job_id);
    var resub = await api('/api/packs/create', { industry: '宠物医院庚', description: '社区医院，面向养宠家庭' });
    out.sweptState = swept.b.state;
    out.sweptErr = (swept.b.error || '').slice(0, 7);
    out.resub = resub.st + ':' + String(resub.b.detail || '').slice(0, 7);
    window.__pgclaimms = undefined;
    // 还原这一行也得可证：旋钮清掉之后再提交一条建包，它必须还是 packing。
    // 原来这行是个"没人观察的赋值"—— 删掉它门禁照绿（第 16 轮复核 P3），
    // 于是"后来的场景会不会被残留的 0 当场判死"完全没被量过。
    var s4 = await api('/api/packs/create', { industry: '推拿所辛', description: '社区推拿，面向上班族' });
    var afterRestore = await api('/api/jobs/' + s4.b.job_id);
    out.knobCleared = typeof window.__pgclaimms !== 'number';
    out.afterRestoreState = afterRestore.b.state;
    return out;
  })();`);
  check("桩：读包内文件先认包（不存在的包 404，private 才是 403）",
    pgFileGate.realYaml === 200 && pgFileGate.noPack === 404
      && pgFileGate.noPackPriv === 404 && pgFileGate.priv === 403
      && pgFileGate.noRel === 404 && pgFileGate.badName === 400
      && pgFileGate.badNameDet === 400 && pgFileGate.unkDet === 404
      && pgFileGate.listed === 2,
    JSON.stringify(pgFileGate));
  check("桩：未知作业按路由发各自的 404 原文，终态快照带齐引擎那 9 个键",
    pgFileGate.getCode === '404:作业不存在或已随重启释放'
      && pgFileGate.cancelCode === '404:作业不存在'
      // 建包那两条路由走桩里的另一支分支（jobpg 前缀 + PG_JOBS 查不到）：
      // 探针不含 jobpg 就永远打不到那一支，两句互换或都发同一句都没人知道（第 16 轮 P1-3）。
      && pgFileGate.pgGet === '404:作业不存在或已随重启释放'
      && pgFileGate.pgCancel === '404:作业不存在'
      && pgFileGate.termKeys === 'created_at,error,id,kind,params,result,state,steps,stream'
      && pgFileGate.termResult === 'null' && pgFileGate.termStream === '行业包生成'
      // 在途那一份：引擎轮询走 include_result=False，所以是同一套键**少 result**；
      // 少了 created_at / error 就是在骗界面（第 16 轮复核 P2-4），而建包确实有进度步骤
      // （引擎里 _retry_logger 会往 job.steps 记一条）。
      && pgFileGate.inKeys === 'created_at,error,id,kind,params,state,steps,stream'
      && pgFileGate.inSteps === 1 && pgFileGate.inHasCreated === true,
    JSON.stringify(pgFileGate));
  check("桩：占位回收时限可注入，到点落 failed 并归还名字（不会永久 409）",
    pgFileGate.sweptState === 'failed' && pgFileGate.sweptErr === '作业超时未收工'
      && /^409:行业包正在创建/.test(pgFileGate.resub) === false && /^200:/.test(pgFileGate.resub),
    JSON.stringify(pgFileGate));
  // 出厂值那一档也得有人量：出厂时限之内不许回收（否则"回收时限"其实是"当场判死"），
  // 而注入成 0 之后同一条作业要能落 failed 并把名字还回来（第 16 轮复核 P2-7）。
  check("桩：不注入旋钮时按出厂时限回收（刚提交的作业不被当场判死）",
    pgFileGate.liveState === 'packing' && pgFileGate.liveResub === 409
      && pgFileGate.liveSweptState === 'failed' && pgFileGate.liveAfter === 200
      // 还原那一行也在这条里被量：清掉旋钮之后再提交一条建包，它不该被当场回收
      //（原来 `window.__pgclaimms = undefined` 是一行没人观察的赋值，删掉门禁照绿）。
      && pgFileGate.knobCleared === true && pgFileGate.afterRestoreState === 'packing'
      // 值域那一格是诚实的极限：门禁只有两三分钟，"设成 600000000 = 永不回收"这种改动
      // 在行为上量不到，只能把常量本身钉在一个说得过去的区间里（0 = 当场判死、
      // 超过十分钟 = 与界面「正在创建中」的可等范围脱节）。
      && pgFileGate.claimMs >= 1000 && pgFileGate.claimMs <= 600000,
    JSON.stringify(pgFileGate));

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
      // 内边距按**设它的那个值**来判，不写死像素：把工具条左右内边距从 8 改到 16
      // 是为了让它与输入文字的左/右边界合成一条线（一张卡里不许有两条左边界），
      // 写死 8 的下一次 token 调整会被读成回归。
      toolsPadLeft: parseFloat(cs.paddingLeft),
      toolsPadRight: parseFloat(cs.paddingRight),
      topicPadLeft: parseFloat(getComputedStyle(topic).paddingLeft),
      topicPadRight: parseFloat(getComputedStyle(topic).paddingRight),
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
    compose.sendRight === compose.toolsPadRight && compose.toolsPadRight > 0,
    `rightOffset=${compose.sendRight} padRight=${compose.toolsPadRight}`);
  // 一张卡只许有一条左边界、一条右边界：工具条的左右内边距必须与输入框的左右
  // 内边距同源，否则「输入文字 415 / 第一个胶囊 407」那样在同一张卡里出现两条竖线。
  check("工具条左右内边距与输入文字同源（卡内不再有第二条左边界与第二条右边界）",
    compose.toolsPadLeft === compose.topicPadLeft
      && compose.toolsPadRight === compose.topicPadRight,
    `tools=${compose.toolsPadLeft}/${compose.toolsPadRight} topic=${compose.topicPadLeft}/${compose.topicPadRight}`);
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
    `document.querySelector('#llm-list .pl-item.on').click(); return true;`);
  await evalIn(`document.getElementById('md-cancel').click(); return true;`);
  await sleep(200);
  // 知识 / 技能 已并入行业包面板 —— 补一张「文件分组 + 查看器」的截图。
  // 只截面板默认态看不到查看器有内容，所以先点开一个文件再截。
  await shot("settings-packinfo-file.png", `window.__ts.setPane('packinfo');
    var it = document.querySelector('#pi-groups .kb-item');
    if (it) it.click();
    return true;`);
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

  // ── 结果页三样（§2.7）：节奏条 / 每段语速 / 人味标记回到正文 ────────
  // 三样都是"数据早就有、界面上没露"的那类，所以断言盯的是**展示口径**：
  // 数值算得对不对、下划线落在正文里、超目标的档位用得对不对。
  const result3 = await evalIn(`return (function(){
    var pane = document.querySelector('.res-pane[data-tab="script"]');
    var bars = [...pane.querySelectorAll('.tempo > i')];
    var rates = [...pane.querySelectorAll('.script-card .rate')];
    var marks = [...pane.querySelectorAll('.card-text .tell-mark')];
    var cards = [...pane.querySelectorAll('.script-card')];
    var legend = [...pane.querySelectorAll('.tempo-legend span')].map(function (x) { return x.textContent.trim(); });
    var kids = [...pane.querySelector('.script-pane').children].map(function (x) { return x.className; });
    return {
      barCount: bars.length,
      barWidths: bars.map(function (b) { return b.style.getPropertyValue('--w'); }),
      barCls: bars.map(function (b) { return b.className; }),
      legend: legend,
      rateCount: rates.length,
      rateTexts: rates.map(function (x) { return x.textContent.trim(); }),
      rateOver: rates.map(function (x) { return x.classList.contains('over'); }),
      cardCount: cards.length,
      markCount: marks.length,
      marksInText: marks.every(function (m) { return !!m.closest('.card-text'); }),
      markTipCount: pane.querySelectorAll('.card-foot .tell-jump').length,
      paneKids: kids,
    }; })()`);
  check("节奏条按段渲染、宽度走 CSSOM（4 段 + 3 行图例）",
    result3.barCount === 4 && result3.legend.length === 3
      && result3.barWidths.join(",") === "23,27,27,12"
      && result3.barCls[0].indexOf("hook") >= 0 && result3.barCls[3].indexOf("cta") >= 0,
    JSON.stringify({ n: result3.barCount, w: result3.barWidths,
                     cls: result3.barCls, legend: result3.legend }));
  check("节奏卡排在分段卡片之前（整篇分配先于逐段细节）",
    result3.paneKids.length >= 2 && result3.paneKids[0].indexOf("page-card") >= 0
      && result3.paneKids[1].indexOf("script-list") >= 0,
    JSON.stringify(result3.paneKids));
  // 每段语速 = 字数 ÷ **净口播秒数**（时间轴把段间停顿算在段里，必须减掉）。
  // 期望值按桩现算：字数 ÷ (时间轴跨度 − 段间停顿)，目标取 params.rate。
  // ⚠ 忘了减停顿的话每段都会被读成偏慢，而那是**系统性偏差**、不是某一段的问题 ——
  //   这条断言钉的就是它（第一段的期望值与"没减停顿"的值差得足够明显）。
  check("每段语速按「字数 ÷ 净口播秒数」算，且超目标 10% 走 .over 红",
    result3.rateCount === result3.cardCount
      && result3.rateTexts[0] === "5.2 字/秒 · 目标 4.5"
      && result3.rateTexts[1] === "4.0 字/秒 · 目标 4.5"
      && result3.rateOver.join(",") === "true,false,false,false",
    JSON.stringify({ texts: result3.rateTexts, over: result3.rateOver }));
  check("人味标记回到正文：命中词在 .card-text 里加下划线，卡脚给出处数",
    result3.markCount === 2 && result3.marksInText && result3.markTipCount === 2,
    JSON.stringify({ marks: result3.markCount, inText: result3.marksInText,
                     tips: result3.markTipCount }));

  // 人味标记的"可定位"要真的能点（§2.7 ③）—— 挂了个可点样式却没处理器，
  // 就是"把假能力冒充真能力"。判据：点正文里的下划线 → 真的切到人味分面板。
  await evalIn(`document.querySelector('.card-text .tell-mark').click(); return true;`);
  await sleep(150);
  const smell = await evalIn(`return (function(){
    var pane = document.querySelector('.res-pane[data-tab="smell"]');
    var tab = document.querySelector('.res-tab[data-tab="smell"]');
    return {
      tabLabel: tab ? tab.textContent.trim() : '',
      tabOn: tab ? tab.classList.contains('on') : false,
      paneVisible: pane ? !pane.classList.contains('hidden') : false,
      score: pane ? pane.querySelector('.smell-score .n').textContent.trim() : '',
      hitRows: pane ? pane.querySelectorAll('table tr').length - 1 : 0,
      saysNotGated: pane ? /不进「合格」判定/.test(pane.textContent) : false,
      phShown: pane ? /占位事实/.test(pane.textContent) : false,
    }; })()`);
  check("人味标记可定位：点正文里的下划线真的切到「人味分」面板",
    smell.tabOn && smell.paneVisible && smell.tabLabel.indexOf("人味分") === 0,
    JSON.stringify(smell));
  check("人味分面板：分数 + 逐条命中明细 + 明说「不进合格判定」（只报不拦）",
    smell.score === "88" && smell.hitRows === 2 && smell.saysNotGated && smell.phShown,
    JSON.stringify({ score: smell.score, rows: smell.hitRows,
                     notGated: smell.saysNotGated, ph: smell.phShown }));

  // ── 今日选题 / 情报源（B 线，§2.2 与 §2.4/§2.5）────────────────
  // 这一组守的核心是「**一处声明、四处一致**」：分区、chip、计数、情报源表
  // 四处都由 META 里那份 intel_sources（= pack.yaml 的声明）渲染。
  // 桩里特意留了一个 enabled:false 的源，好让「未接入 N」这条有活样本 ——
  // 没有它，那条断言会在"全部接入"的桩上永远绿（空转）。
  // ⚠ 阅读面宽度必须在**切到选题视图之前**量：`#chat-stream` 在选题视图里是
  // hidden，量出来是 0，那条"同档宽"的断言会变成拿 780 比 0（恒不成立）。
  // ⚠ 比的是**阅读面那一档 token**（--w-stream），不是某个容器的实际宽度：
  //   `#chat-stream` 自己是通栏（内层 `.msg` 才限宽），量它会得到 1026 这种
  //   "看起来不一样"的假结论。§2.8 定的口径就是"同一档宽度 + 居中"，
  //   所以判据落在 token 上，两边都读同一个数。
  const streamW = await evalIn(`return parseFloat(getComputedStyle(document.documentElement)
    .getPropertyValue('--w-stream')) || 0;`);
  await evalIn(`window.__refreshCalls = 0; window.__ignoredKeys = [];
    document.getElementById('btn-topics').click(); return true;`);
  await sleep(600);
  // ⚠ evalIn 的包装是 `(() => { <expr> })()`（**块体**），所以这里必须显式 return ——
  //   写成裸 IIFE 的话返回值被丢掉，拿到 undefined，后面每条断言都读属性报 TypeError。
  const topicsGeo = await evalIn(`return (function(){
    var on = document.getElementById('view-topics');
    var chat = document.getElementById('view-chat');
    var cards = on.querySelectorAll('#topics-root .page-card');
    var groups = on.querySelectorAll('#topics-root .kb-group');
    // ⚠ 这一段在**模板字符串内部**，所以注释与代码里都不许出现反斜杠转义
    //   （Node 会先把它转成真换行/真制表符，浏览器拿到的就是未闭合字符串，
    //    整段 evaluate 直接 SyntaxError: Invalid or unexpected token）。
    //   换行符一律用 String.fromCharCode(10) 现算。
    var NL = String.fromCharCode(10);
    var heads = [...groups].map(g => g.querySelector('.kb-group-t').textContent.trim().split(NL)[0]);
    var decl = (window.__tsMeta.packs.filter(function(p){ return p.name === 'elevator'; })[0] || {}).intel_sources || [];
    var declaredHead = function (h) {
      return decl.some(function (sc) { return h.indexOf(sc.label + ' \u00b7 ' + sc.role) === 0; }); };
    var chips = [...on.querySelectorAll('#src-chips .m-chip')].map(c => c.textContent.trim());
    var srcRows = on.querySelectorAll('#sources-table tr');
    var srcHead = srcRows[0] ? [...srcRows[0].children].map(t => t.textContent.trim()) : [];
    var accCol = srcRows.length > 1 ? [...srcRows[1].children].map(t => t.textContent.trim()) : [];
    var inner = on.querySelector('.topics-inner').getBoundingClientRect();
    return {
      topicsVisible: !on.classList.contains('hidden'),
      chatHidden: chat.classList.contains('hidden'),
      navOn: document.getElementById('btn-topics').classList.contains('on'),
      rightView: document.getElementById('right').dataset.view,
      refreshBtnVisible: getComputedStyle(document.getElementById('btn-refetch')).display !== 'none',
      groupCount: groups.length, heads: heads, cardCount: cards.length,
      allHeadsDeclared: heads.every(declaredHead),
      declLabels: decl.map(function (sc) { return sc.label + ' \u00b7 ' + sc.role; }),
      chips: chips,
      navCount: document.getElementById('topics-count').textContent,
      metaText: document.getElementById('topics-meta').textContent,
      srcHead: srcHead, accCol: accCol, srcRowCount: srcRows.length - 1,
      // ⚠ 表体列数必须等于表头列数：少一列时**表头看着完全正常**，
      //   只有数据行整体左移（「今日命中」跑到「上次抓取」下面）。
      //   实测就是这样抓到的：表体里那个 last 变量写了却没进模板。
      srcBodyCols: srcRows.length > 1 ? [...srcRows[1].children].length : 0,
      topicsWidth: +inner.width.toFixed(1),
      dashForUncomputable: /—/.test(on.textContent),
      // 信号条拿到 --w 了没有：宽度是**数据驱动**的，必须走 CSSOM 写入。
      // ⚠ 这里量的是"写进去了"，不是"有没有 style 属性" —— CSSOM 的 setProperty
      //   本身就会生成 style 属性，那是规范允许的做法（§2.4 明写"数据驱动的宽度
      //   一律走 CSSOM"）。真正不许的是**源码里的 style= 字面量**，那条由本文件
      //   既有的 securitypolicyviolation 监听覆盖（CSP 无 'unsafe-inline' 会拦下它）。
      barsWithW: [...on.querySelectorAll('.sig-track > i')]
        .filter(i => i.style.getPropertyValue('--w') !== '').length,
      barCount: on.querySelectorAll('.sig-track > i').length,
    }; })()`);
  check("点左栏「今日选题」切到选题视图（左栏与会话列表常驻，不跳页、不开二级窗）",
    topicsGeo.topicsVisible && topicsGeo.chatHidden && topicsGeo.navOn
      && topicsGeo.rightView === "topics",
    JSON.stringify({ v: topicsGeo.topicsVisible, c: topicsGeo.chatHidden,
                     n: topicsGeo.navOn, r: topicsGeo.rightView }));
  check("头部那两颗情报动作只在选题视图出现（用 #right[data-view] 控显隐）",
    topicsGeo.refreshBtnVisible, String(topicsGeo.refreshBtnVisible));
  // ⚠ 不能断言"组数 == 今天有货的源数"：一屏只放 5 条，**只有本页涉及的源**
  //   才会出现分区。要守的是另外两件事：每个组标题都来自声明（渲染层真的在读
  //   配置，而不是自己写死平台名）；以及组数 **严格小于本页卡片数** ——
  //   后者正是"桶按 x.label 找、而桶里存的是 x.g"那个 bug 的判据
  //   （每张卡各成一组，组标题重复，实测就是这样被这条抓出来的）。
  check("分区按来源渲染：组标题逐条来自声明的 label · role，且不是每张卡各成一组",
    topicsGeo.groupCount >= 1 && topicsGeo.allHeadsDeclared
      && topicsGeo.groupCount < topicsGeo.cardCount,
    JSON.stringify({ n: topicsGeo.groupCount, cards: topicsGeo.cardCount,
                     heads: topicsGeo.heads, decl: topicsGeo.declLabels }));
  check("筛选行 chip 含「全部 N」+ 各源计数 + 「未接入 N」（只列有货的源）",
    topicsGeo.chips.some(c => c.indexOf("全部 8") === 0)
      && topicsGeo.chips.some(c => c.indexOf("下拉词 4") === 0)
      && topicsGeo.chips.some(c => c.indexOf("未接入 1") === 0),
    JSON.stringify(topicsGeo.chips));
  check("左栏 nav 键帽计数 = 今日条目总数（不是「未读数」——那要多存一个状态）",
    topicsGeo.navCount === "8" && /共 8 条/.test(topicsGeo.metaText),
    JSON.stringify({ nav: topicsGeo.navCount, meta: topicsGeo.metaText }));
  check("算不出的 D/S/E 显示「—」而不是 0（0 与「没数据」结论相反）",
    topicsGeo.dashForUncomputable, String(topicsGeo.dashForUncomputable));
  check("选题阅读面与正文同档宽（--w-stream，§2.8 的列宽统一口径）",
    Math.abs(topicsGeo.topicsWidth - streamW) <= 1,
    JSON.stringify({ topics: topicsGeo.topicsWidth, stream: streamW }));
  check("信号条宽度走 CSSOM 写入（源码内联 style 由既有的 CSP 违规监听守着）",
    topicsGeo.barCount > 0 && topicsGeo.barsWithW === topicsGeo.barCount,
    JSON.stringify({ bars: topicsGeo.barCount, withW: topicsGeo.barsWithW }));
  check("情报源表：`接入` 与 `上次抓取` 是两根正交列，且表体列数与表头一致",
    topicsGeo.srcHead.indexOf("接入") >= 0 && topicsGeo.srcHead.indexOf("上次抓取") >= 0
      && topicsGeo.srcRowCount === 4
      && topicsGeo.srcBodyCols === topicsGeo.srcHead.length,
    JSON.stringify({ head: topicsGeo.srcHead, rows: topicsGeo.srcRowCount,
                     bodyCols: topicsGeo.srcBodyCols }));

  // 「换一批」是在池子里翻页，**不是重新抓** —— 判据是刷新请求数没涨。
  await evalIn(`document.getElementById('btn-more').click(); return true;`);
  await sleep(120);
  const paged = await evalIn(`return { label: document.getElementById('btn-more').textContent,
    calls: window.__refreshCalls || 0,
    firstCard: document.querySelector('#topics-root .page-card .card-title').textContent };`);
  check("「换一批」纯前端翻页：不触发重抓请求（要新数据点顶栏那颗重抓）",
    paged.calls === 0 && /第 2\//.test(paged.label),
    JSON.stringify(paged));

  // 「忽略」只影响今天：本地摘掉 + 落一条忽略记录（服务端按日期判"连续几天沉底"）。
  await evalIn(`document.getElementById('btn-more').click();
    window.__firstTitle = document.querySelector('#topics-root .page-card .card-title').textContent;
    document.querySelector('#topics-root .page-card [data-act="ignore"]').click(); return true;`);
  await sleep(300);
  const ignored = await evalIn(`return { keys: window.__ignoredKeys || [],
    // ⚠ 判据是"那张卡真的从 DOM 里没了"，不是只看 data.items.length ——
    //   只过滤 data.items 而不过滤 g.items 时，计数变了、卡还在，
    //   后者才是用户看到的东西（实测这条弱断言漏过了一次）。
    gone: ![...document.querySelectorAll('#topics-root .page-card .card-title')]
            .some(t => t.textContent === window.__firstTitle),
    navCount: document.getElementById('topics-count').textContent };`);
  check("「忽略」落一条记录并本地摘掉该卡（只影响今天，明天同题还会回来）",
    ignored.keys.length === 1 && ignored.gone && ignored.navCount === "7",
    JSON.stringify(ignored));

  // 「去生成」= 切回会话 + 写主题 + 填参数，**不自动发送**（一次生成几十秒真金白银）。
  // ⚠ 在同一次 evaluate 里读一次主题：分开两次读的话，"谁把主题清掉的"
  //   会变成一个说不清的问题（实测就是这样 —— 分开读只能看到结果为空）。
  const clicked = await evalIn(`var b = document.querySelector('#topics-root .page-card [data-act="gen"]');
    var t = b ? b.closest('.page-card').querySelector('.card-title').textContent : '';
    window.__genCalls = 0;
    if (b) b.click();
    return { had: !!b, cardTitle: t,
             topicRightAfter: document.getElementById('topic').value,
             viewRightAfter: document.getElementById('right').dataset.view };`);
  await sleep(300);
  const went = await evalIn(`return { view: document.getElementById('right').dataset.view,
    chatVisible: !document.getElementById('view-chat').classList.contains('hidden'),
    topic: document.getElementById('topic').value,
    navOn: document.getElementById('btn-topics').classList.contains('on'),
    refetchVisible: getComputedStyle(document.getElementById('btn-refetch')).display !== 'none',
    genCalls: window.__genCalls || 0 };`);
  check("「去生成」切回会话视图并填好主题，但**不自动发送**（留改参数的机会）",
    went.view === "chat" && went.chatVisible && went.topic.length > 0
      && !went.navOn && !went.refetchVisible && went.genCalls === 0,
    JSON.stringify(Object.assign({}, went, clicked)));

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
  if (pageErrs.length) {
    console.error(`
──────── 页面侧异常（崩溃根因常在这里） ────────`);
    for (const m of pageErrs.slice(0, 8)) console.error(`· ${String(m).split(String.fromCharCode(10))[0]}`);
  }
  console.error(e.stack);
  cleanupAll();
  process.exit(2);
});
