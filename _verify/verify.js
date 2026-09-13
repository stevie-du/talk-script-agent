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
const SHOT_DIR = __dirname;

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
      } },
    { name: "fitment", display_name: "全屋定制包", draft: true,
      params: { segment: { label: "细分领域", default: "全屋定制", options: ["全屋定制"] } } },
  ],
};

function stubScript() {
  return `<script>
window.__INJECTED__ = 1;
window.__errs = [];
addEventListener('error', e => window.__errs.push(String(e.message)));
addEventListener('unhandledrejection', e => window.__errs.push('rej: ' + String(e.reason)));
(function(){
  var META = ${JSON.stringify(META)};
  var RESULT = ${JSON.stringify(RESULT)};
  // 首启引导要测「没配 Key」的情形：用 URL 上的 &nokey=1 切换
  var NOKEY = /(^|[?&])nokey=1/.test(location.search);
  var CONFIG = { base_url:"https://x/v4", model:"glm-4.7", api_key_set:!NOKEY,
                 mock:false, retries:2, timeout:180, max_tokens:16000,
                 temperature:0.7, env_override:false };
  var calls = { gen:0, job:0, cancel:0, rewrite:0 };
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
    if (s.indexOf('/api/packs/elevator/file') >= 0) return mk(
      { rel:'knowledge/topics.md', size:1024, text:'# 选题库 /  / - 家用电梯怎么挑？' });
    if (s.indexOf('/api/config') >= 0) return mk(CONFIG);
    if (s.indexOf('/api/packs/') >= 0) return mk({ display_name:'电梯行业包', description:'电梯行业口播脚本包',
      draft:false, checklist:'1. 核对参数 / 2. 核对禁用词',
      files:[{rel:'pack.yaml',size:2048},{rel:'skill.yaml',size:1024},{rel:'knowledge/topics.md',size:5120}] });
    if (s.indexOf('/api/history') >= 0) return mk([]);
    return mk({});
  };
})();
</script>`;
}

// ── 断言工具 ────────────────────────────────────────────────
const results = [];
function check(name, ok, detail) { results.push([name, !!ok, detail || ""]); }

const sleep = ms => new Promise(r => setTimeout(r, ms));

(async function main() {
  PORT = await freePort(8931);
  CDP_PORT = await freePort(9333);
  _tmpProfile = fs.mkdtempSync(path.join(os.tmpdir(), "ts-cprof-"));

  server = await serve(RENDERER, PORT, { inject: stubScript() });
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
    accs: document.querySelectorAll('.acc-list .acc').length,
    followups: document.querySelectorAll('.followups .fu-chip').length,
    whyBlock: document.querySelectorAll('.banner.why').length,
    softBanner: !!Array.from(document.querySelectorAll('.banner')).find(b => /待确认/.test(b.textContent)),
    dropBanner: !!Array.from(document.querySelectorAll('.banner')).find(b => /单字禁用词/.test(b.textContent)),
    verBar: document.querySelectorAll('.ver-bar').length,
  };`);
  check("完成后渲染出结果", done.hasResult && !done.busy, JSON.stringify(done));
  check("分段卡片 4 张", done.cards === 4, `cards=${done.cards}`);
  check("指标速览 4 项", done.metrics === 4, `metrics=${done.metrics}`);
  check("每段字数用后端统计值（12/85 而非前端重算）",
    done.quotaChips.some(t => t.includes("12/85")), JSON.stringify(done.quotaChips));
  check("折叠区：分镜 / 合规 / JSON / 日志", done.accs === 4, `accs=${done.accs}`);
  check("回炉原因可展开（决策解释）", done.whyBlock === 1, "");
  check("单字词被忽略有说明", done.dropBanner, "");
  check("后续建议 chips 出现", done.followups >= 2, `chips=${done.followups}`);
  check("单版本时不显示版本导航", done.verBar === 0, "");

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
    status: document.getElementById('st-status').textContent };`);
  check("模型接口分区回填 base_url 与温度",
    llm.base === "https://x/v4" && llm.temp !== undefined && llm.temp !== "",
    JSON.stringify(llm));
  check("接口状态显示重试/超时等实际生效值", /重试/.test(llm.status), llm.status);

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

  await evalIn(`const s = document.getElementById('stg-search');
    if (s) { s.value = ''; s.dispatchEvent(new Event('input', {bubbles:true})); }
    return true;`);

  // 设置内搜索：6 个分区以后还会更多，没检索就得一个个点过去
  await evalIn(`document.getElementById('btn-open-settings').click(); return true;`);
  await sleep(300);
  await evalIn(`const b = document.getElementById('stg-search');
    b.value = '超时'; b.dispatchEvent(new Event('input', {bubbles:true})); return true;`);
  await sleep(300);
  const stgHit = await evalIn(`return {
    visible: [...document.querySelectorAll('.stg-nav-item')]
      .filter(n => !n.classList.contains('hidden')).map(n => n.dataset.pane),
    pane: window.__ts.settingsPane };`);
  check("设置内搜索能定位到含该字段的分区",
    stgHit.visible.includes("llm") && stgHit.pane === "llm", JSON.stringify(stgHit));

  await evalIn(`const b = document.getElementById('stg-search');
    b.value = ''; b.dispatchEvent(new Event('input', {bubbles:true})); return true;`);
  await sleep(200);
  const stgAll = await evalIn(`return [...document.querySelectorAll('.stg-nav-item')]
    .filter(n => !n.classList.contains('hidden')).length;`);
  check("清空设置搜索后恢复全部分区", stgAll === 6, String(stgAll));
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

  await evalIn(`document.getElementById('sess-search-clear').click(); return true;`);
  await sleep(200);
  const cleared = await evalIn(`return {
    rows: document.querySelectorAll('#session-list .sess-item').length,
    val: document.getElementById('sess-search').value };`);
  check("清空搜索后恢复全部会话", cleared.rows === 2 && cleared.val === "",
    JSON.stringify(cleared));

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

  // 回到正常模式，继续后面的布局检查
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

  // ── 截图 ─────────────────────────────────────────────────
  const shot = async (name, expr) => {
    if (expr) { await evalIn(expr); await sleep(500); }
    const s = await cdp.send("Page.captureScreenshot", { format: "png" });
    fs.writeFileSync(path.join(SHOT_DIR, name), Buffer.from(s.data, "base64"));
  };
  await shot("main-sidebar.png",
    "window.__ts.newChat(); document.getElementById('btn-generate'); return true;");
  await shot("settings-gen.png",
    "document.getElementById('btn-open-settings').click(); window.__ts.setPane('gen'); return true;");
  await shot("settings-packinfo.png", "window.__ts.setPane('packinfo'); return true;");
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
