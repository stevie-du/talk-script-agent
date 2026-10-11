// 生成作业的生命周期：发起 / 轮询 / 停止 / 重挂 / 单段重写 / 分步确认。
//
// 修复前这里的坑（都在本文件里解掉）：
//  · waitDone 没有空值守卫、不识别 cancelled —— 重写途中切走会抛 TypeError 被
//    外层 catch 成「重写失败」；若期间点了停止，会一直空转到 180 秒超时才报错。
//  · openSession 不重置参数快照 —— 打开历史记录后改参数会弹「结果已过期」，
//    但展示的其实是历史记录，提示与实际不符。
//  · 每次生成都清空输入框且不留版本，点「换一版」看起来像结果被覆盖。

import { $, toast } from "./util.js";
import { api, ApiError, modelSetupGap, MODEL_SETUP_REPLY, JOB_BUDGET_MS, POLL_MS } from "./api.js";
import { state, setJob, setResult, setBusy, detachJob, stopPolling } from "./store.js";
import * as T from "./thread.js";
import { placeholderBody, renderProgress, startTicker, stopTicker, BUSY_STATES, setDocTitle, resetDocTitle } from "./progress.js";
import { STATE_LABEL, CHAT } from "./words.js";
import { renderResult, renderFailure, renderStopped, renderVersionNoResult, jumpToFirstPlaceholder } from "./result.js";
import { setHead } from "./head.js";
import { loadSessions, busyRecords, packLabel } from "./sessions.js";
import { openSettings } from "./settings.js";
import { closeOverlays } from "./overlays.js";
import { setLanding, syncBusyAffordance, refreshGate, setParam, setCfgHint } from "./ui.js";

const POLL_MAX_MISSES = 3;
// 重写的等待上限。**必须不短于后端对同一个操作给出的上限**，否则两边说的是相反的话：
//   · 单次模型请求 timeout = 180s（app/config.py DEFAULT），retries = 2
//     → 一个调用最坏 3 次尝试 = 540s（app/llm.py 的 `attempts = retries + 1`）；
//   · `chat_json` 在 HTTP 重试之外还有一层「结构不合要求」重试（max_retries=1）
//     → 重写那一次调用最坏 2 × 540s；
//   · 重写之后还要重画分镜（再一次调用）；
//   · 作业级总闸是 app/jobs.py 的 `JOB_BUDGET_SECONDS = 1200`，超了后端自己判 failed。
// 修复前这里是 240000（4 分钟）—— 比**一个阶段**的最坏值还短，于是前端先报
// 「重写失败」，而后端在那之后把改写结果写进了 result.json。
// 与后端作业预算同一本账（api.js 的 JOB_BUDGET_MS）：真正的超时语义由后端作业状态给出。
const REWRITE_TIMEOUT_MS = JOB_BUDGET_MS;

/** jobId → 提交那一刻用的模型名。失败卡要印它，而不是印**此刻**的全局值
 *  （用户在跑作业期间去设置里换了模型，卡上就写着那个新模型，
 *  于是他照着去查一个跟这次失败毫无关系的模型）。
 *  ⚠ 不能靠后端回传：作业快照的 params 是 `req.model_dump()`，
 *  `GenerateRequest` 里没有这个字段 —— 发过去、存下来都不会有它。
 *  作业**离手**即删（成功 / 取消 / 失败三条出口都调 `_forgetModel`）：
 *  桌面端是长驻进程，只在失败那条 delete 的话，一天生成几百条之后
 *  这张表就单调攒到窗口关闭，而键值早已没人读。 */
const modelByJob = new Map();

/** 作业离手（成功 / 取消 / 失败）时清掉它的模型记录。 */
function _forgetModel(jobId) {
  if (jobId) modelByJob.delete(jobId);
}


// ── 参数 ────────────────────────────────────────────────────
/** 读一个生成参数的真值。
 *  ⚠ 以前这里是 `$("p-" + key)` —— 只认工具条胶囊那个 select。而
 *  `style / persona / cta` 只在设置页「生成参数」卡里出现（工具条放不下），
 *  于是这三项**在界面上能选、生成时永远是 null**，静默走包默认值；
 *  在设置页改 segment/duration 同样不生效，`updateStale` 也不报「参数已改」。
 *  真值改放 state.genParams，两个视图都往那里写（见 ui.js bindParamSync）。 */
export function getParam(key) {
  const v = state.genParams[key];
  if (v !== undefined && v !== "") return v;
  // 没人动过 → 回退到控件当前值（= 包默认），与改动前的行为逐字等价。
  // 不这么做的话首屏的段落标题、"参数已改"检测会凭空变空 —— 那是另一个 bug。
  // style / persona / cta 没有胶囊，仍然返回 null（后端取包默认），
  // 但用户在设置页选过之后就会走上面那条分支 —— 这才是这次修的东西。
  const s = $("p-" + key);
  return s ? (s.value || null) : null;
}

/** 请求体**带得走**的生成参数（唯一一份；界面上"能不能选"以这里为准）。
 *
 * 为什么要有这张表：`collectParams()` 是显式白名单，而设置页那张卡过去按
 * `Object.keys(pack.params)` 全量画控件 —— 第三方包多声明一个 `params.tone`，
 * 界面上就多一个能选、能显示、**永不发出去**的下拉（引擎侧也只读固定几个键，
 * `app/pipeline.py` 没有通用透传）。第 27 轮流程线 P1。
 * 现在不认识的键不画控件，改在 `#pack-audit` 那行如实说"这个参数不会生效"。
 */
export const SENDABLE_PARAMS = ["segment", "audience", "duration", "style",
  "platform", "persona", "cta", "rewrite_scope"];

export function collectParams() {
  return {
    pack: $("pack").value,
    topic: $("topic").value.trim(),
    segment: getParam("segment"), audience: getParam("audience"),
    duration: getParam("duration") ? Number(getParam("duration")) : null,
    style: getParam("style"), platform: getParam("platform"),
    persona: getParam("persona"), cta: getParam("cta"),
    // A-2：改写范围。**这一行不能少** —— 请求体是显式白名单，漏掉一个键就是
    // 「设置页里选了 in-place、发出去的请求里没有它、后端静默落回 bounded」，
    // 与 quota_degraded 当年被白名单滤掉是同一类静默降级。
    rewrite_scope: getParam("rewrite_scope"),
    facts: $("facts").value.trim() || null,
    // 元素缺失（理论上不该发生）→ null 走包默认，与 facts 同一口径：
    // 冒充一个内置默认值，后端就把「用户没选过」当成「用户选了 strong/both」（P3）。
    voice: $("voice") ? $("voice").value : null,
    format: $("format") ? $("format").value : null,
  };
}

function snapshotParams() {
  state.paramsSnapshot = JSON.stringify(collectParams());
}

// ── 过期提示 ────────────────────────────────────────────────
export function updateStale() {
  const b = $("stale-banner");
  if (!state.result || !state.paramsSnapshot) { b.classList.add("hidden"); return; }
  const changed = JSON.stringify(collectParams()) !== state.paramsSnapshot;
  b.classList.toggle("hidden", !changed);
  if (changed) {
    b.innerHTML = `参数已修改，当前展示的是旧参数的结果 ——
      <button class="link-btn" data-act="regen">用新参数生成</button>`;
    b.querySelector('[data-act="regen"]').onclick = () =>
      send({ topic: state.sentTopic || state.result?.params?.topic || "" });
  }
}

// ── 发送 ────────────────────────────────────────────────────
/**
 * @param overrides 覆盖当前界面参数（重跑 / 换一版 / 编辑后重生成）
 * @param opts.asVersionOf 传入助手 body 时，本次结果作为它的新版本（不新建气泡）
 * @param opts.userNode    配合 asVersionOf：复用哪个用户气泡
 */
export async function send(overrides = {}, opts = {}) {
  if (state.busy) { toast("正在生成中，请先等它结束或点停止"); return; }
  const params = { ...collectParams(), ...overrides };
  if (!params.topic) { toast("请输入主题"); $("topic").focus(); return; }

  // 发出去之前就知道会被拒 —— 那就不要去发，直接把用户送到能改的地方。
  // 判据用 /api/meta 的**字段**（见 api.js 的 modelSetupGap），不读服务端的人话。
  const gap = modelSetupGap(state.meta);
  if (gap) {
    toast(`${gap} —— 已打开「模型接口」，配好再发`, 4200);
    openSettings("llm");
    return;
  }

  state.sentTopic = params.topic;
  setBusy(true, true);
  // 记下**这次**用的模型（审查 P2 交互）：失败卡原来印的是 `state.meta.model`
  // —— 那是**此刻**的全局值。用户在跑作业期间去设置里换了模型，卡上就写着那个
  // 新模型，于是他照着去查那个模型的配额/日志，而真正跑挂的是另一个。
  //
  // ⚠ 存在**本模块的这张表**里，不在 state.job.params、也不在后端：
  //   失败卡读的 `snap.params` 来自后端作业快照，而那是 `req.model_dump()`
  //   （app/pipeline.py 建 Job 时）—— GenerateRequest 里没有这个字段，
  //   所以**发过去 / 存下来都不会有它**，写进 state.job.params 只是本地一份，
  //   失败时那一行照样落回 `state.meta.model`（= 本轮要修的那个 bug 原样）。
  //   按 jobId 存而不是全局一个变量：一次只挂一条作业，但轮询响应可能过期
  //   （下面的过期响应守卫），按 id 取才不会把别那条的模型印出来。
  const modelAtSubmit = state.meta?.model || "";
  try {
    const { job_id } = await api.generate(params);
    modelByJob.set(job_id, modelAtSubmit);
    snapshotParams();
    updateStale();
    // 必须走 setJob：refreshGate 订阅了 job 事件。直接赋值的话，发送键
    // 不会从「生成中…」变成「停止」—— 用户在生成期间点不动停止键。
    setJob({ id: job_id, state: "queued", params });
    state.pollMisses = 0;
    state.result = null;

    setLanding(false);
    $("stale-banner").classList.add("hidden");

    let body;
    if (opts.asVersionOf) {
      body = opts.asVersionOf;
      T.pushVersion(body, { id: job_id, result: null, state: "queued", params });
    } else {
      T.addUserMsg(params.topic, { onEdit: onEditUserMsg });
      body = T.addAssistantMsg();
    }
    body.innerHTML = placeholderBody();
    state.activeBody = body;
    state.job.body = body;

    if (!opts.keepTopic) $("topic").value = "";
    autoGrowTopic();
    setHead(params.topic, "生成中", "生成中…", "warn");
    T.scrollBottom(false);
    startTicker(() => state.job);
    poll();
    // 额度少了一个在飞的作业：左栏那颗「停止全部」的读数要跟着变。
    syncBusyAffordance();
    loadSessions();
  } catch (e) {
    setBusy(false);
    stopTicker();
    // 顺序有讲究：额度与坏包**同为 409**，先判谁都不能只凭状态码（见 isQuotaConflict）。
    if (isQuotaConflict(e)) { quotaDenied(); return; }
    if (isPackBroken(e)) { packBroken(e); return; }
    if (modelSetupGap(state.meta) || MODEL_SETUP_REPLY.test(e.message)) {
      // 三种「没配模型」都跳去能改它的那一页。修复前判据是 `/API Key|令牌/`，
      // 而服务端三条文案里只有一条含「API Key」—— 新手最常撞的那两条
      // （一条模型都没加 / 加了但没启用）匹配不上，「去配置」这一跳从未发生。
      toast(e.message, 4000);
      openSettings("llm");
    } else {
      toast("生成失败：" + e.message, 4000);
    }
    syncBusyAffordance();
  }
}

/** 「同时进行的生成已达上限（4 个）」= 后端 StateConflict → 409。
 *  ⚠ 判据**不能**只看 `status === 409`（修复前就是这么写的）：坏掉的行业包
 *  也吃 409（app/server.py 的 PackBrokenError 映射），于是
 *  「行业包「x」的 pack.yaml 语法有误（第 4 行第 6 列）」会被原样替换成
 *  「同时进行的生成已达上限」—— 一句会让人原地等队列的话，
 *  而真实原因修不好就永远发不出去（P1-1）。
 *  现在读服务端给的机器可读码 `code`；没有 code 的老服务才回退到文案。 */
function isQuotaConflict(e) {
  if (!(e instanceof ApiError)) return /已达上限/.test(e && e.message || "");
  if (e.code === "quota_exceeded") return true;
  if (e.code) return false;                    // 有码且不是额度 → 别猜
  return /已达上限/.test(e.message || "");
}

/** 行业包本身坏了（`code === "pack_broken"`，与 app/server.py 的常量同名）。
 *  这里要做的不是"换个说法"，而是**把引擎那句原话送到屏幕上**：
 *  它带着文件名与行列号，是用户能拿去修的唯一线索，替换成任何一句概括都是损失。 */
function isPackBroken(e) {
  return e instanceof ApiError && e.code === "pack_broken";
}

function packBroken(e) {
  setBusy(false);
  stopTicker();
  // 参数条上那颗「（损坏）」与 #pack-err 说的是同一件事（它们由 /api/meta 的
  // pack_error 驱动），但那要用户自己去扫一眼才发现 —— 他刚点了发送，
  // 所以这里把引擎那句原话说出来，并给足读完的时间。
  // 刻意**不**自动跳设置页：行业包面板会再发一次同一个坏包的详情请求，
  // 结果是同一条错误弹两次 toast，反而分不清哪句是真的。
  toast(e.message, 7000);
  syncBusyAffordance();
}

/** 额度满了：说清是谁占着，并指一条**这一屏上真的走得通**的出路。
 *  修复前这里只是一句「生成失败：同时进行的生成已达上限…」——
 *  而那些作业是用户连点几次「新建对话」留下的**后台**作业，界面上除了一颗
 *  呼吸点什么都没有，既不知道是谁占的也没有任何入口停掉它们。
 *
 *  ⚠ 指路必须分两种屏幕（2026-09-27 修 P1-27）：landing（首页）**刻意**不显示
 *  「停止全部后台生成」那颗按钮 —— composer-foot 有一条 28px 高度不变量，
 *  首页多一行就把 hero 挤歪（实测 chipsGap 16 → 44，verify.js 钉着）。
 *  所以在首页说「点下方『停止全部后台生成』」是让用户去找一颗不存在的按钮；
 *  而首页真正能走的那条路是**左栏**：行首挂呼吸点的那几条，点开任意一条，
 *  下方就有那颗停止键。两条指路各说各屏上真有的东西。 */
function quotaDenied() {
  const bg = busyRecords();
  setBusy(false);
  stopTicker();
  if (!bg.length) { toast("生成失败：同时进行的生成已达上限，稍后再试", 4500); return; }
  const landing = $("view-chat")?.classList.contains("is-landing");
  if (landing) {
    // 与 newChat 那句保持同一份事实与同一个下名词（同一个坑，两处指路不许各说各的）。
    toast(`发送被拒：还有 ${bg.length} 条生成在后台进行，占住了额度。`
      + `它们在左栏里挂着呼吸点，点开任意一条可以接着看、或在那里停止它。`, 7000);
  } else {
    toast(`发送被拒：还有 ${bg.length} 条生成在后台进行，占住了额度`, 5200);
  }
  syncBusyAffordance();
}


/** 编辑用户消息 → 从这条起重新生成（丢弃它之后的内容）。 */
async function onEditUserMsg(userNode, topic) {
  if (state.busy) { toast("正在生成中，请先停止再编辑"); return; }
  T.truncateAfter(userNode);
  state.result = null;
  setHead(topic, "生成中", "生成中…", "warn");
  await send({ topic, reroll: true });
}

// ── 轮询 ────────────────────────────────────────────────────
export function poll() {
  stopPolling();
  if (!state.job) return;
  // 作业令牌：请求往返期间用户可能点了「新建对话」、切了历史、或发起了新生成。
  // 没有令牌的话旧响应照样往下走，会把别的作业的进度画到当前会话上。
  const jobId = state.job.id;
  const body = state.job.body;
  api.job(jobId).then(snap => {
    if (!state.job || state.job.id !== jobId) return;      // 过期响应，丢弃
    state.pollMisses = 0;
    state.job.state = snap.state;
    // created_at 必须一并挂到 state.job 上：每秒刷新的「已用 N 秒」读的是
    // state.job（startTicker(() => state.job)），而 setJob 建的那份对象里没有它。
    // 漏掉时 fmtElapsed 返回空串，于是轮询把它写出来、下一次 tick 又抹掉 ——
    // 用户报的「秒数显示一下、隐藏一下」就是这个（2026-09-20）。
    if (snap.created_at) state.job.created_at = snap.created_at;
    renderProgress(body, snap);

    // 分步确认（paused_awaiting_confirmation）已于 2026-09-19 整体移除，
    // 那个「停在中间等人点确认」的分支跟着没了。
    if (snap.state === "done") {
      onDone(snap, body, jobId);
    } else if (snap.state === "failed") {
      onFailed(snap, body, jobId);
    } else if (snap.state === "cancelled") {
      onCancelled(body, "cancelled");
    } else if (BUSY_STATES.has(snap.state)) {
      state.pollTimer = setTimeout(poll, POLL_MS);
    } else {
      // 认不出来的状态：按「这一趟已经不在跑了」处理，把界面解锁还给用户。
      // 修复前这里是无条件 `else { 继续轮询 }`，一个不在 BUSY_STATES 里的值
      // 就能让作业永远停在「生成中」—— 发送键不恢复、用户只能刷新窗口。
      onCancelled(body, "unknown");
    }
  }).catch(e => {
    if (!state.job || state.job.id !== jobId) return;
    state.pollMisses += 1;
    if (state.pollMisses <= POLL_MAX_MISSES) {
      // 退避重试，封顶 3s
      state.pollTimer = setTimeout(poll, Math.min(1200 * state.pollMisses, 3000));
      return;
    }
    // 服务连续不可达：明确落到失败态，界面立刻可用（不再骗用户「正在生成」）
    const failParams = state.job.params || collectParams();
    state.pollMisses = 0;
    setBusy(false);
    stopTicker();
    setJob(null);
    setHead(state.sentTopic, "连接中断", "失败", "bad");
    renderFailure(body, {
      title: "与生成服务的连接中断",
      message: e.message || "未知原因",
      detail: "本地引擎可能已退出。可在设置里检查，或直接重试。",
    }, {
      onRetry: () => send(failParams, { asVersionOf: body }),
      onOpenSettings: () => openSettings("llm"),
    });
    T.follow(true);
  });
}

function onDone(snap, body, jobId) {
  setBusy(false);
  stopTicker();
  _forgetModel(jobId);           // 成功了就不必再记「当时用的哪个模型」
  // 标题打上「✓ 完成」（2026-09-27）：用户切到别的应用剪素材去了，
  // 任务栏那一行是唯一能告诉他「好了」的地方 —— 不进窗口也看得见。
  setDocTitle(snap.params?.topic || state.sentTopic || "", 0, 0, true);
  setResult(snap.result);
  $("stale-banner").classList.add("hidden");

  // 版本归档：把结果挂到当前版本槽，再整块重画
  const vi = body._vi >= 0 ? body._vi : T.pushVersion(body, { id: jobId });
  body._versions[vi] = { id: jobId, result: snap.result, state: "done", params: snap.params };
  renderVersion(body);
  loadSessions();
  T.follow(true);
}

function renderVersion(body) {
  const v = T.currentVersion(body);
  if (!v || !v.result) {
    // ⚠ 这一版**没有产物**（失败 / 被停止）。以前这里只重画版本条就 return，
    //   于是「版本 1 / 2」的条底下仍然是第 2 版的完整稿子 —— 用户以为在对照
    //   两版，实际看的是同一份，并据此做发布决定（P0）。
    //   现在清空并画一张说明卡，并指明哪一版有产物可看。
    setResult(null);
    // 找**最近**的、真的有产物的那一版做跳转目标（通常就是重试成功的那版）
    let hasIndex = -1;
    for (let i = body._versions.length - 1; i >= 0; i--) {
      if (body._versions[i]?.result) { hasIndex = i; break; }
    }
    renderVersionNoResult(body, v, hasIndex, (idx) => {
      body._vi = idx;
      renderVersion(body);
    });
    T.renderVersionBar(body, renderVersion);
    return;
  }
  // 翻版本必须同步 state.result —— 它不只是"当前结果"的缓存，
  // `rwOk`（能不能局部重写）、「按此修改」等都读它。
  // 漏掉的话界面显示 v1、state 里还是 v2，两者悄悄分叉。
  setResult(v.result);
  // 顺序不能反：renderResult 会 body.innerHTML = "" 清空容器，
  // 若先画版本导航条，它会被这一步直接删掉（表现为「换一版」后看不到 2/2）。
  renderResult(v.result, body, resultOpts(body));
  T.renderVersionBar(body, renderVersion);
}

export function resultOpts(body) {
  const r = T.currentVersion(body)?.result;
  const packParams = state.meta?.packs?.find(p => p.name === $("pack").value)?.params || {};
  return {
    rwOk: !!(state.job && state.result),
    platforms: packParams.platform?.options || [],
    audiences: packParams.audience?.options || [],
    // 时长档同样取自**当前包**（2026-09-27 修 P1-26）：下游建议过去写死
    // 「压缩到 30 / 扩到 90」，而行业包可以只给 [45, 120]。写死的档位在
    // 这种包里根本不存在，点下去 `<select>` 的 selectedIndex 变 -1 ——
    // 键从 DOM 上被删掉、`sel.value = "30"` 落空，界面退回包的默认值，
    // 而 toast 还在说「已设为 30 秒」（屏幕与承诺相反）。
    durations: (packParams.duration?.options || []).map(Number).filter(n => n > 0),
    onRewrite: (i, fb) => rewriteSegment(i, fb),
    onRerun: () => send({ topic: r?.params?.topic || "" }, { asVersionOf: body, keepTopic: true }),
    onRevary: () => send({ topic: r?.params?.topic || "", reroll: true },
      { asVersionOf: body, keepTopic: true }),
    onPrefill: (act, value) => prefill(act, value),
    jumpToPlaceholder: () => jumpToFirstPlaceholder(body),
  };
}

/** 后续建议：只预填参数 + 聚焦，不自动提交。 */
function prefill(act, value) {
  // ⚠ 走 ui.setParam 而不是直接 `sel.value = …`（2026-09-27 修 P1-26）。
  //   `sel.value = 不存在的键` 在 <select> 上会让 selectedIndex 落 -1 ——
  //   浏览器随即把 selectedIndex 复位成 -1 且 value 取第一个 option，
  //   界面看起来「没点上」，而 toast 无条件说「已设为 N」。
  //   setParam 是"程序化改参数"的唯一入口：它先验这一档在当前包里存不存在，
  //   不存在就返回 false（一个字都不写）。
  const ok = setParam(act, value);
  if (state.sentTopic) $("topic").value = state.sentTopic;
  updateStale();
  // 改前那句 `dispatchEvent(new Event("change"))` 会顺带触发 document 上那个
  // 监听（ui.js:844 → updateStale + setCfgHint）。setParam 只写值与重画，
  // 工具条那颗「设置」按钮的 title 摘要得自己刷，否则预填之后它还念旧参数。
  setCfgHint();
  // ⚠ 主题这里是**程序化赋值**，不触发 input —— 发送键的 disabled 不会自己跟上，
  //   而 Enter 做的事就是 `$("btn-generate").click()`：点一颗 disabled 按钮是空操作。
  //   所以必须显式刷一次门，否则 toast 承诺"回车即可生成"而回车什么都不发生（P1）。
  refreshGate();
  autoGrowTopic();
  // 三句都要真做得到：没设上不许说设上了，主题没填上不许说"回车即可"。
  const has = !!$("topic").value.trim();
  if (!ok) {
    toast(`这个包里没有「${value}」这一档 —— 自己在「更多参数」里挑一个`, 3600);
  } else {
    toast(has ? `已设为 ${value} —— 回车即可生成新版本`
              : `已设为 ${value} —— 填好主题后点发送`, 3200);
  }
  $("topic").focus();
}

function onFailed(snap, body, jobId) {
  setBusy(false);
  stopTicker();
  // 失败/停止不进「✓ 完成」—— 那是骗人。还原成中性标题
  // （2026-09-27）：`renderProgress` 在最后一帧写的还是「(4/5) 主题」，
  // 作业已经不在跑了还挂着进度数，任务栏那一行会一直说「在生成」。
  resetDocTitle();
  const failParams = snap.params || {};
  setJob(null);
  const err = snap.error || "未知错误";
  setHead(state.sentTopic, "生成失败", "失败", "bad");
  // ⚠ 失败也必须**占一个版本槽**（P0）。修前这里只画一张失败卡、`_vi` 一直是 -1，
  //   于是「用同样的参数重试」成功之后 pushVersion 落到下标 0，整个气泡只有
  //   一个版本 —— 翻页条不出现，用户无从知道「刚才那次失败过」。
  //   更糟的是：只有 1 个版本时 renderVersionBar 直接把条删掉，于是
  //   「版本 1 = 失败、版本 2 = 成功」这条路径根本走不到 P0-1 那张卡上。
  //   登记之后这条路径才成立，翻回第 1 版也才有「它自己没有产物」可显示。
  const vi = body._vi >= 0 ? body._vi : T.pushVersion(body,
    { id: jobId, result: null, state: "failed", error: err, params: failParams });
  body._versions[vi] = { id: jobId, result: null, state: "failed", error: err, params: failParams };
  renderFailure(body, {
    title: "生成失败",
    message: err,
    detail: `行业包：${failParams.pack ? packLabel(failParams.pack) : "-"} · 模型：${modelByJob.get(jobId) || state.meta?.model || "-"}`,
    retryLabel: "用同样的参数重试",
  }, {
    onRetry: () => send({ ...failParams }, { asVersionOf: body }),
    onOpenSettings: () => openSettings("llm"),
  });
  _forgetModel(jobId);            // 印完就收：这条作业已经离手
  toast("生成失败：" + err, 5000);
  T.follow(true);
  loadSessions();
}

/** 落到「这次生成没有产物」的界面。
 *  @param why  cancelled = 后端确认已停；其它值 = 认不出来的状态（也没在跑）。
 *  措辞必须与 `why` 一致：产物到底落没落盘是**后端告诉我们的**，不是猜的。
 *  原先这里只有一条路 —— 不管三七二十一写「产物未落盘」，于是与同一秒里
 *  已经跑完、产物已经存好的那次停止正好说反。见 abort()。 */
function onCancelled(body, why = "cancelled", opts = {}) {
  setBusy(false);
  stopTicker();
  // 同 onFailed：作业不在跑了，标题就别再挂着进度数（2026-09-27）
  resetDocTitle();
  _forgetModel(state.job && state.job.id);   // 必须在 setJob(null) 之前取 id
  setJob(null);
  // 保留主题（与 onFailed 同口径）：线程里还挂着这个主题的用户气泡和
  // 「已停止」卡，头部却回到「新对话」，两条收尾分支对同一场景两种说法（P3）。
  setHead(state.sentTopic || CHAT.title, "本次生成" + STATE_LABEL.cancelled, "");
  if (body) {
    // 与 onFailed 同口径：被停掉的这一版也要占槽，于是「停掉 → 再来一次」
    // 之后翻回第 1 版能说清「它被停了，没有产物」，而不是顶着上一版的稿子（P0）。
    // `_vi < 0` 说明这个气泡还没有任何版本，id 也就无从取（本来也没有产物）。
    const vi = body._vi >= 0 ? body._vi : T.pushVersion(body,
      { id: null, result: null, state: "cancelled" });
    body._versions[vi] = { ...body._versions[vi], state: "cancelled" };
    renderStopped(body, {
      title: why === "cancelled" ? "已停止本次生成" : "这次生成已经中断（后台没有它在跑的作业了）",
      onRetry: () => send({ topic: state.sentTopic }, { asVersionOf: body, keepTopic: true }),
      onRevary: () => send({ topic: state.sentTopic, reroll: true },
        { asVersionOf: body, keepTopic: true }),
    });
  }
  state.activeBody = null;
  if (!opts.silent) toast(why === "cancelled" ? "已停止本次生成" : "这次生成已中断");
  loadSessions();
  syncBusyAffordance();
}

/** 从别处（左栏「放弃这次生成」）收掉正在跑的生成气泡。
 *
 * 修前 `deleteSession` 只 `detachJob()`：轮询停了、左栏那行没了，右栏这颗气泡
 * 还写着「文案撰写中 · 已用 N 秒」并且继续呼吸 —— 一个没有任何出口的假进行中态
 * （用户只能刷新）。走 onCancelled 与点「停止」那条路同一份收尾（「已停止」标题 +
 * 两个再试出口 + 头部状态），不另编第二套话。
 * @return {boolean} 屏幕上有没有真的落了一张终态卡（在别的视图里删的就没有）。
 */
export function dropLiveJobView(opts = {}) {
  const had = !!state.activeBody;
  if (had) onCancelled(state.activeBody, "cancelled", opts);
  detachJob();
  return had;
}

// ── 停止 ────────────────────────────────────────────────────
/** 正在飞的那次**单段重写**（{jid, body, vi, owner}），没有则 null。
 *  存在期间，Esc / 停止键取消的是**这次重写**，不是那条已完成的生成作业。 */
let rewriteInFlight = null;

/** 这次取消取消的是「一段重写」而不是「整条生成」吗？ */
function cancellingRewrite() {
  return !!rewriteInFlight && !!state.job && state.job.id === rewriteInFlight.jid;
}

export async function abort() {
  const job = state.job;
  // 兜底：正常入口都带 `state.job` 门控（按钮在 busy&&!job 时 disabled，
  // Esc 也只在该态放行），走不到这里。但 abort() 是导出函数，将来任何新入口
  // （快捷键 / 自动化）漏了门控时，静默 return 会让用户以为「停止键坏了」——
  // 说一句「还在建立连接」，比无声无息好。
  if (!job) {
    if (state.busy) toast("正在建立连接，请稍候再停", 2500);
    return;
  }
  stopPolling();
  const body = job.body || state.activeBody;
  // 立刻说「正在停止…」（建包那条路早这么做过，settings.js 的 cancelPackgen）：
  // cancel 是一次网络往返，这段空窗里按钮要是还写着「停止生成」，
  // 用户只会当成没点上，于是再点一次。后端 cancel 幂等所以不会出两条作业，
  // 但那颗键会一直在骗人。
  state.stopping = true;
  refreshGate();
  let snap = null, err = null;
  try {
    // 停止请求的**响应本身就是答案**：app/pipeline.py 的 cancel() 对已经结束的
    // 作业幂等返回它的快照（不报错），所以「点停止的那一秒刚好跑完」时
    // 它回的是 state=done + 已落盘的产物，而不是 cancelled。
    snap = await api.cancel(job.id);
  } catch (e) {
    err = e.message || "未知原因";
  }
  state.stopping = false;
  if (err) {
    // 后端不同意时要说出来。修复前这里是全吞：用户看到「已停止」以为停了，
    // 实际后台还在跑并把结果写回来，于是出现「停止后又多出一条记录」的怪事。
    setBusy(false);
    stopTicker();
    toast(`停止失败：${err}`, 4500);
    if (body) renderFailure(body, {
      title: "停止失败，后台可能仍在运行",
      message: err,
      retryLabel: "重新连接并查看状态",
    }, {
      onRetry: () => reconnectToJob(job, body),
      onOpenSettings: () => openSettings("llm"),
    });
    loadSessions();
    syncBusyAffordance();
    return;
  }
  let st = snap && snap.state;
  if (!st) {
    // 应答里没有状态（老服务 / 精简快照）：再问一次。
    let askErr = null;
    try { snap = await api.job(job.id); st = snap && snap.state; } catch (e) { askErr = e; }
    if (!st && askErr) {
      // ⚠ 以前这里写的是 `catch (_) { st = "cancelled"; }` —— 而**上一行的注释**
      //   明明写着"猜是不诚实的"。猜成 cancelled 的后果：后端其实还在跑，
      //   界面已经宣布"已停止"，随后又冒出一条记录（屏幕与事实相反）。
      //   现在不猜：把"没问到状态"说出来，并把用户接回那条作业自己看。
      toast("停止请求已发出，但没问到作业状态：" + (askErr.message || askErr), 5000);
      reconnectToJob(job, body);
      return;
    }
  }
  if (st === "done") {
    // 没停下 —— 它已经成了。产物在盘上，就把结果画出来并说清「已保存」，
    // 绝不能继续演成「什么都没留下」。
    let gotResult = !!snap.result;
    if (!gotResult) {
      try { snap.result = await api.record(job.id); gotResult = true; } catch (_) { gotResult = false; }
    }
    onDone(snap, body, job.id);
    // ⚠ 产物没读出来就**不能**说"已保存"：那时屏幕上是"只有版本条、没有正文"，
    //   与这句承诺相反（用户会以为产物在、只是没显示）。两种情形说清是哪一种。
    toast(gotResult
      ? "它已经跑完了 —— 产物已保存，停止没有生效"
      : "它已经跑完了，但产物这次没读出来（下面只有版本记录）—— 到会话列表里点开看",
      5200);
    return;
  }
  if (st === "failed") { onFailed(snap, body, job.id); return; }
  // ⚠ 取消的是**单段重写**，不是整条生成（P0）。这一支以前落进 onCancelled →
  //   renderStopped，把整份稿子擦成「已停止本次生成。产物未落盘」—— 而产物
  //   好好地在 result.json 里、好好地显示在界面上（取消发生在 `_stop_check`，
  //   `write_result` 没执行）。用户因此以为稿子没了、两个按钮又不通向原稿。
  //   现在只收掉重写的界面状态，**结果视图原样留着**，并说清「稿子还是原来那版」。
  if (st === "cancelled" && cancellingRewrite()) {
    const rw = rewriteInFlight;
    rewriteInFlight = null;
    stopTicker();
    setBusy(false);
    // 重写也停了，标题别再挂着进度（2026-09-27，与 onCancelled 同口径）
    resetDocTitle();
    // 结果视图**原样留着**（含仍在转圈的那颗点由 renderResult 自己收）。
    // 只有当结果已经被别的动作换掉时，才退成不带「稿子还在」那句的说法。
    const keep = !!rw.body && document.contains(rw.body) && !!state.result;
    toast(keep ? "这一段的重写已停止，稿子还是原来那版" : "这一段的重写已停止", 3600);
    state.activeBody = null;
    loadSessions();
    syncBusyAffordance();
    return;
  }
  onCancelled(body, st === "cancelled" ? "cancelled" : st || "cancelled");
}


// ── 重挂 / 回看 ─────────────────────────────────────────────
/** 回到一个还在跑的会话：重建消息流并把轮询接回去。 */
export async function attach(id) {
  let snap;
  try {
    snap = await api.job(id, true);
  } catch (e) {
    toast("无法回到这次生成：" + e.message, 3500);
    loadSessions();
    return;
  }
  stopPolling();
  setResult(null);
  state.pollMisses = 0;
  setJob({ id, state: snap.state, params: snap.params || null, created_at: snap.created_at });
  state.paramsSnapshot = null;                 // 切了场景，过期提示的基准要重置
  T.clearThread();
  setLanding(false);
  $("stale-banner").classList.add("hidden");
  const topic = snap.params?.topic || "";
  state.sentTopic = topic;
  T.addUserMsg(topic, { onEdit: onEditUserMsg });
  const body = T.addAssistantMsg();
  state.job.body = body;

  if (snap.state === "failed") {
    T.pushVersion(body, { id, state: "failed" });
    onFailed(snap, body, id);
    return;
  }
  if (snap.state === "done" && snap.result) {
    T.pushVersion(body, { id, result: snap.result, state: "done", params: snap.params });
    renderVersion(body);
    setResult(snap.result);
    loadSessions();
    return;
  }
  body.innerHTML = placeholderBody();
  state.activeBody = body;
  renderProgress(body, snap);
  T.scrollBottom();
  // attach() 只会被非终态记录调到（会话列表里 done 走 openRecord），
  // 所以这里不再分支 —— 原来那个 `if (paused)` 随分步确认一起删了。
  setBusy(true, true);
  setHead(topic, "生成中", STATE_LABEL[snap.state] || "生成中", "warn");
  startTicker(() => state.job);
  poll();
  loadSessions();
}

/** 把后台还在跑的作业逐条停掉（同一个 POST /api/jobs/{id}/cancel，走 N 次）。
 *
 *  ⚠ 不改后端语义：后端没有批量取消端点，也不该由前端假装有一个 ——
 *  这里逐条发**同一个**取消请求，每条的结果都按 abort() 那套读法如实报：
 *  取消对已经跑完的作业是幂等返回 done + 产物（app/pipeline.py 的 cancel()），
 *  那种不能说成"已停止"，要说"它已经跑完了"。
 *  当前正看着的那条不在这里处理（它有自己的「停止」键，走 abort()）。 */
export async function stopAllBackgroundJobs() {
  const bg = busyRecords().filter(it => !state.job || it.id !== state.job.id);
  if (!bg.length) { toast("没有后台在跑的生成"); return; }
  const ids = bg.map(it => it.id);
  const done = [];          // 停的时候发现其实已经跑完了
  const failed = [];        // 取消请求本身失败
  for (const id of ids) {
    try {
      const snap = await api.cancel(id);
      if (snap && snap.state && snap.state !== "cancelled") done.push(id);
    } catch (e) {
      failed.push(id);
    }
  }
  const stopped = ids.length - done.length - failed.length;
  const parts = [];
  if (stopped) parts.push(`已停止 ${stopped} 条`);
  if (done.length) parts.push(`${done.length} 条其实已经跑完（产物已保存，去左栏点开）`);
  if (failed.length) parts.push(`${failed.length} 条没停成（可稍后重试）`);
  toast(parts.join("，") || "已处理", 5200);
  await loadSessions();
  syncBusyAffordance();
}

/** 刷新 / 重开窗口后接回「还在跑的那一条」（缺陷 3：原来没有任何重挂路径）。
 *  判据只用**真快照**：先问一次 /api/jobs/{id}，确认它仍在 BUSY_STATES 里才动界面。
 *  为什么不凭左栏那一行的 state 直接重挂：那一份来自历史索引，两拍之间它可能已经
 *  跑完、也可能随引擎重启消失 —— 凭它重挂会把首屏演成「生成中」再自己 cancelled，
 *  凭空多出一张「已停止」的卡。
 *  返回 false = 没接上（它已经不在了 / 已经结束了），首屏保持原样。
 *  ⚠ 后端没有 `GET /api/jobs` 这个列表端点（只有 /api/jobs/{id}），
 *  在跑的作业是从 /api/history 的返回里带出来的（app/server.py 的 history()
 *  把 store 摘要与内存快照并在一起），所以"有哪些在跑"读的是那份。 */
export async function reattachBusyJob(id) {
  let snap = null;
  try { snap = await api.job(id); } catch (_) { return false; }
  if (!snap || !BUSY_STATES.has(snap.state)) return false;
  await attach(id);
  return true;
}

/** 打开一条历史记录（产物已落盘）。 */
export async function openRecord(id) {
  // 打开历史记录时，窗口标题不再说上一条生成的事（与 newChat / onFailed 同账）
  resetDocTitle();
  let r;
  try {
    r = await api.record(id);
  } catch (e) {
    toast("回看失败：" + e.message, 3500);
    loadSessions();
    return;
  }
  detachJob();
  T.clearThread();
  closeOverlays();
  setLanding(false);
  $("stale-banner").classList.add("hidden");
  state.paramsSnapshot = null;                 // 历史结果没有「当时的界面参数」可比
  state.sentTopic = r.params?.topic || "";

  // 失败/取消的历史记录没有 result，只有作业摘要。
  // ⚠ 这一支曾经**永远跑不到**：左栏的点击是按 `state === "done"` 分流的
  //  （sessions.js 的 activate），failed / cancelled / 以及索引里残留的旧状态
  //  全被送进 attach() —— 而 attach() 读的是内存里的作业，重启后必然 404，
  //  于是磁盘上明明有 job.json 摘要，界面上只剩一条 3.5 秒就消失的 toast。
  if (!r.sections) {
    // ⚠ 必须把 state.result 清掉（与下面 done 那一支的 setResult 对齐，复审 P0）：
    //   少了这一句，state.result 会**一直指着上一条打开过的记录**，
    //   而右屏画的是这一条。于是 sessions.js 里
    //   「`state.result.id === it.id` 才收屏」那个判据就成立了：
    //   打开 A（有产物）→ 打开 B（失败、无产物，state.result 仍是 A）
    //   → 回左栏删 A → 右屏里 B 的失败卡被无端抹掉、清成 landing。
    //   同一类症状两种成因，而这一处是**根**。
    setResult(null);
    T.addUserMsg(state.sentTopic, { onEdit: onEditUserMsg });
    const body = T.addAssistantMsg();
    T.pushVersion(body, { id, state: r.state || "failed" });
    // 终态词全部取自 STATE_LABEL（words.js）：这条分支以前写「被取消了」+「已取消」，
    // 而停止键、卡片、左栏都说「停止」—— 同一个动作两个名字，用户会找第三个按钮。
    const stopped = r.state === "cancelled";
    const title = stopped ? `这次生成${STATE_LABEL.cancelled}`
      : r.state === "failed" ? `这次生成${STATE_LABEL.failed}了` : "这次生成没有完成";
    setHead(state.sentTopic, title, stopped ? STATE_LABEL.cancelled : STATE_LABEL.failed, "bad");
    renderFailure(body, {
      title,
      message: r.error || "没有留下产物",
      detail: "产物未落盘，因此无法回看内容。可以按下面的按钮用相同参数重来一次。"
        + `（记录状态：${r.state || "未知"}）`,
      retryLabel: "用同样的参数重来",
    }, {
      onRetry: () => send({ ...(r.params || {}) }, { asVersionOf: body }),
      onOpenSettings: () => openSettings("llm"),
    });
    T.scrollBottom();
    return;
  }

  T.addUserMsg(state.sentTopic, { onEdit: onEditUserMsg });
  const body = T.addAssistantMsg();
  T.pushVersion(body, { id, result: r, state: "done", params: r.params });
  renderVersion(body);
  setResult(r);
  loadSessions();
  T.scrollBottom();
}

/** 「重新连接并查看状态」：把**界面**也一起接回这条作业，不只是把轮询打开。
 *
 *  修复前这里只有 `setBusy(true) + poll()` 两件事，点完之后：
 *    · busy=true、提示行写着「生成中，参数已锁定（Esc 可停止）」，
 *    · 而气泡正文还是那张「停止失败，后台可能仍在运行」的失败卡，
 *      #gen-status / 步骤时间线整个不存在 —— 一张自相矛盾的卡，
 *      要一直等到作业自己结束才恢复（P2-3）。
 *    · 计时器也没重开，「已用 N 秒」停在停止失败那一刻。
 *  做法与 attach() 同一套：骨架重画 → 认领作业 → busy → 标题 → 计时器 → 轮询。
 *  第一拍轮询回来就会把真实进度画上去；若它其实已经结束，
 *  poll() 的 done / failed / cancelled 分支会立刻把这张卡换成对应的样子。 */
export function reconnectToJob(job, body) {
  if (!job) { toast("这条作业已经不在界面上，无法重连", 4000); return; }
  if (state.job !== job) setJob(job);       // 失败路径没清 job，但别的世界线可能清了
  if (body) {
    body.innerHTML = placeholderBody();     // 覆盖掉那张失败卡，卡片与状态必须同源
    state.activeBody = body;
  }
  state.pollMisses = 0;
  setBusy(true, true);
  setHead(state.sentTopic, "生成中", "生成中…", "warn");
  startTicker(() => state.job);
  poll();
  syncBusyAffordance();
}

/** 「到点了但没结束」——这是**控制流**，不是失败。
 *  调用方必须去问一读本端的实况再决定说什么（见 reportRewritePending）。 */
class RewritePending extends Error {}
/** 「这一条重写已经没有界面可写了」——也是控制流，不是失败（P1-2）。
 *  切会话 / 打开历史 / 新建对话都会把那条气泡从 DOM 里摘掉，从这一刻起
 *  busy、计时器、发送键**归用户正在看的那一条所有**，这条被遗弃的重写
 *  既不许把它们收回（那会把别人正在跑的东西解锁），也不许报「已重写并复检」。 */
class RewriteDetached extends Error {}

/** 只有**这条重写仍然拥有界面**时才收回它开始时借走的 busy / 计时器。
 *  @param owner 借状态时认领的那条作业（`state.job` 当时的那个对象）
 *  @param body 重写写入的那条助手气泡
 *  两个判据各挡一种发生方式：
 *    · body 已不在 DOM  = 界面整个换掉了（切会话 / 新建对话 / 打开历史），
 *      现在的 busy 是**下一条**设的，收了就把它的「生成中」按下键松开；
 *    · state.job 已换对象 = 气泡还在（同一会话里重挂/换版），但作业已经换了人。
 *  实测到的旧症状（P1-2）：重写还在轮询时切走，回来看到
 *  busy=false + uiState=rewriting、发送键可点、参数锁中途松开、
 *  「已用 15 秒」冻在原地、按 Esc 停掉 0 条作业却仍写着「（Esc 可停止）」。 */
function releaseRewriteUI(owner, body) {
  if (body && !document.contains(body)) return;
  if (owner && state.job && state.job !== owner) return;
  setBusy(false);
  stopTicker();
}

// ── 单段重写 ────────────────────────────────────────────────
export async function rewriteSegment(index, feedback) {
  if (state.busy) { toast("正在生成中，请稍候再重写", 2500); return; }
  if (!state.job) {
    toast("这是历史记录：局部重写需要后台作业仍在内存中（重启应用后不可用）。可用「换一版」整篇重生成", 4500);
    return;
  }
  if (!state.result) { toast("还没有可重写的结果", 2500); return; }
  const body = state.job.body || state.activeBody;
  if (!body) { toast("当前会话已切换，无法原地重写", 3000); return; }

  // ⚠ 重写的是**正在看的那一版**，不是「最新那一版的作业」。
  // 修前这里恒用 state.job.id —— 而 state.job 永远是最近一次生成，
  // 于是在「版本 1/2」里翻回 v1 点重写：请求打到 v2 的作业上，
  // 结果却写回 v1 的槽位，v1 显示成 v2 被改过的内容。
  // 「换一版不覆盖上一版」这个承诺当场被破坏，而且没有任何报错。
  let vi = body._vi;
  if (vi < 0) vi = T.pushVersion(body, { id: state.job.id });
  const jid = body._versions[vi]?.id || state.job.id;

  // 界面状态的**所有权**从这里开始：setBusy 借走它，只有还归这条重写的时候才还。
  const owner = state.job;
  setBusy(true, true);
  toast("正在重写这一段…");
  // 登记「在飞的是一次重写」：Esc / 停止键打到的 job 是**那条已完成的生成作业**
  // （state.job 没换过），而产物就在界面上、好好地在 result.json 里。
  // abort() 必须靠这个标记才知道「取消的是重写、不是生成」—— 否则它会走
  // onCancelled → renderStopped，把整份稿子擦成「产物未落盘」（P0）。
  rewriteInFlight = { jid, body, vi, owner };
  let keepBusy = false;
  try {
    await api.rewrite(jid, index, feedback);
    const outcome = await waitRewriteDone(jid, body, vi);
    if (outcome === "detached") throw new RewriteDetached();
    // 「已重写并复检」是一句**关于结果**的承诺，只能在真的拿到 done 时说。
    // 修复前 waitRewriteDone 三条出口（detached / cancelled / done）都返回
    // undefined，调用方一律念这一句 —— 于是被停止、被遗弃的重写也会 toast
    // 成功，而界面上那段文字根本没变（P1-2）。
    if (outcome === "done") toast("已重写并复检");
    else if (outcome === "cancelled") toast("这一段的重写已停止，稿子还是原来那版", 3600);
  } catch (e) {
    if (e instanceof RewriteDetached) return;      // 不还状态、不报成功、不报失败
    if (e instanceof RewritePending) {
      if (!document.contains(body)) return;        // 等到超时这一刻也已经换了主人
      keepBusy = await reportRewritePending(jid, body, vi,
        () => rewriteSegment(index, feedback));
    } else {
      if (document.contains(body)) toast("重写失败：" + e.message, 4000);
      else return;
    }
  } finally {
    if (rewriteInFlight && rewriteInFlight.jid === jid) rewriteInFlight = null;
  }
  if (!keepBusy) releaseRewriteUI(owner, body);
}

/** 等待单段重写结束。**返回这一趟的结局**，由调用方决定说什么、收不收界面状态。
 *   "done" 拿到新结果 · "cancelled" 用户停了它 · "detached" 界面已经换主人。
 *  修复前：没有空值守卫（切走就 TypeError）、不认 cancelled（点了停止会空转 180s）、
 *  三条出口一律 `return`（undefined），于是调用方无从区分，见 rewriteSegment。
 *  ⚠ 槽位 `vi` 必须由调用方传进来、不能在这里现取 `body._vi`：等结果的这几秒里
 *     用户可能已经翻了版本，现取会把重写结果写进他**正在看**的那一版 ——
 *     正是这次要修的同一个 bug 的另一种发生方式。 */
async function waitRewriteDone(jid, body, vi) {
  const deadline = Date.now() + REWRITE_TIMEOUT_MS;
  while (Date.now() < deadline) {
    // 会话已切换：这一条重写在界面上已经没有归宿了。**必须**把这件事当成
    // 一个独立结局交回调用方，而不是静默 `return` —— 静默返回在调用方眼里
    // 与「成功」一模一样（P1-2 的两半症状都从这里长出来）。
    if (!document.contains(body)) return "detached";
    // P2-9：轮询用轻量快照（不含 result 的几十 KB 产物），避免每 700ms
    // 序列化整份产物。⚠「每轮刷新进度」这句旧注释说的是假话（2026-10-05 P3
    // 改口）：重写期间 body 是结果视图、没有 #step-track，renderProgress
    // 进门就 return —— 重写**没有**进度展示，真实反馈只有 toast。调用保留
    // 是为将来结果视图挂进度轨时零改动接上，别把它当成已有的功能。
    const snap = await api.job(jid);
    renderProgress(body, snap);
    if (snap.state === "done") {
      const full = await api.job(jid, true);           // done 后再拉一次含结果的
      // 只有还在看这一版时才换全局结果 —— 否则会把用户翻走的视图抢回来
      const viewing = body._vi === vi;
      if (viewing) setResult(full.result);
      body._versions[vi] = { id: jid, result: full.result, state: "done", params: full.params };
      if (viewing) renderVersion(body);
      loadSessions();
      return "done";
    }
    if (snap.state === "cancelled") return "cancelled";   // 用户停了，不再空转
    if (snap.state === "failed") throw new Error(snap.error || "重写失败");
    await new Promise(r => setTimeout(r, 700));
  }
  throw new RewritePending("等待重写结果超时");
}

/** 到了前端的等待上限，但**后端没说失败**。这里问一读本端再说话。
 *
 *  修复前这里直接 `throw new Error("重写超时（超过 4 分钟）…")` → 界面「重写失败：…」，
 *  而后端对同一个操作的最坏值远大于 4 分钟（一笔账见 REWRITE_TIMEOUT_MS 上面），
 *  它跑完照样把改写结果写进 result.json —— 两边说的是相反的两件事，
 *  而用户信的是界面上那一句。
 *  返回 true = 仍在跑且已经把这一条交回正常轮询，调用方**不要**解锁界面。 */
async function reportRewritePending(jid, body, vi, retry = () => {}) {
  let snap = null;
  try { snap = await api.job(jid); } catch (_) { /* 404 = 作业随引擎重启释放 */ }
  if (snap && snap.state === "done") {
    const full = await api.job(jid, true).catch(() => snap);
    const viewing = body._vi === vi;
    if (viewing) setResult(full.result);
    body._versions[vi] = { id: jid, result: full.result, state: "done", params: full.params };
    if (viewing) renderVersion(body);
    loadSessions();
    toast("重写其实已经完成（比预期慢了很多），结果已刷新", 5200);
    return false;
  }
  if (snap && snap.state === "failed") {
    renderFailure(body, {
      title: "重写失败",
      message: snap.error || "未知错误",
      retryLabel: "再重写一次这一段",
    }, {
      onRetry: retry,
      onOpenSettings: () => openSettings("llm"),
    });
    toast("重写失败：" + (snap.error || "未知错误"), 5000);
    return false;
  }
  if (snap && BUSY_STATES.has(snap.state)) {
    const mins = Math.round(REWRITE_TIMEOUT_MS / 60000);
    if (state.job && state.job.id === jid) {
      // 交回正常轮询：进度继续走，落 done / failed 时由 poll() 分支如实收场。
      state.pollMisses = 0;
      renderProgress(body, snap);
      poll();
      toast(`重写仍在进行（已等了 ${mins} 分钟）—— 继续为你盯着`, 5200);
      return true;
    }
    toast(`重写仍在进行（已等了 ${mins} 分钟），稍后点开这条记录即可看到结果`, 5200);
    return false;
  }
  toast(`等待超过 ${Math.round(REWRITE_TIMEOUT_MS / 60000)} 分钟，且后台已没有这条作业`
    + "（引擎可能重启过）—— 稍后回看这条记录", 5600);
  return false;
}


export function autoGrowTopic() {
  const t = $("topic");
  if (!t) return;
  t.style.height = "auto";
  t.style.height = Math.min(t.scrollHeight, 140) + "px";
}
