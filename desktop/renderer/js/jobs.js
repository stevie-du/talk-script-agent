// 生成作业的生命周期：发起 / 轮询 / 停止 / 重挂 / 单段重写 / 分步确认。
//
// 修复前这里的坑（都在本文件里解掉）：
//  · waitDone 没有空值守卫、不识别 cancelled —— 重写途中切走会抛 TypeError 被
//    外层 catch 成「重写失败」；若期间点了停止，会一直空转到 180 秒超时才报错。
//  · openSession 不重置参数快照 —— 打开历史记录后改参数会弹「结果已过期」，
//    但展示的其实是历史记录，提示与实际不符。
//  · 每次生成都清空输入框且不留版本，点「换一版」看起来像结果被覆盖。

import { $, toast, esc } from "./util.js";
import { api, ApiError } from "./api.js";
import { state, setJob, setResult, setBusy, detachJob, stopPolling } from "./store.js";
import * as T from "./thread.js";
import { placeholderBody, renderProgress, startTicker, stopTicker, STATE_LABEL, BUSY_STATES } from "./progress.js";
import { renderResult, renderFailure, renderStopped, setHead, jumpToFirstPlaceholder } from "./result.js";
import { loadSessions } from "./sessions.js";
import { openSettings } from "./settings.js";
import { closeOverlays } from "./overlays.js";

const POLL_MS = 900;
const POLL_MAX_MISSES = 3;
// P2-9：180s 对「82~130s 起步 + 校验」太紧 —— 重写是另一轮完整生成，
// 给足 4 分钟；真正的超时语义由后端作业状态给出，这里只是兜底。
const REWRITE_TIMEOUT_MS = 240000;

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

export function collectParams() {
  return {
    pack: $("pack").value,
    topic: $("topic").value.trim(),
    segment: getParam("segment"), audience: getParam("audience"),
    duration: getParam("duration") ? Number(getParam("duration")) : null,
    style: getParam("style"), platform: getParam("platform"),
    persona: getParam("persona"), cta: getParam("cta"),
    facts: $("facts").value.trim() || null,
    voice: $("voice") ? $("voice").value : "strong",
    format: $("format") ? $("format").value : "both",
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

  state.sentTopic = params.topic;
  setBusy(true, true);
  try {
    const { job_id } = await api.generate(params);
    snapshotParams();
    updateStale();
    // 必须走 setJob：refreshGate 订阅了 job 事件。直接赋值的话，发送键
    // 不会从「生成中…」变成「停止」—— 用户在生成期间点不动停止键。
    setJob({ id: job_id, state: "queued", params });
    state.pollMisses = 0;
    state.result = null;

    $("empty").classList.add("hidden");
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
  } catch (e) {
    setBusy(false);
    stopTicker();
    if (/API Key|令牌/.test(e.message)) {
      toast(e.message, 4000);
      openSettings("llm");
    } else {
      toast("生成失败：" + e.message, 4000);
    }
  }
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
      onCancelled(body);
    } else if (BUSY_STATES.has(snap.state)) {
      state.pollTimer = setTimeout(poll, POLL_MS);
    } else {
      // 认不出来的状态：按「这一趟已经不在跑了」处理，把界面解锁还给用户。
      // 修复前这里是无条件 `else { 继续轮询 }`，一个不在 BUSY_STATES 里的值
      // 就能让作业永远停在「生成中」—— 发送键不恢复、用户只能刷新窗口。
      onCancelled(body);
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
  return {
    rwOk: !!(state.job && state.result),
    platforms: state.meta?.packs?.find(p => p.name === $("pack").value)?.params?.platform?.options || [],
    audiences: state.meta?.packs?.find(p => p.name === $("pack").value)?.params?.audience?.options || [],
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
  const sel = $("p-" + act);
  if (sel) {
    sel.value = String(value);
    sel.dispatchEvent(new Event("change", { bubbles: true }));
  }
  updateStale();
  toast(`已设为 ${value} —— 回车即可生成新版本`, 3200);
  $("topic").focus();
  if (state.sentTopic) $("topic").value = state.sentTopic;
}

function onFailed(snap, body, jobId) {
  setBusy(false);
  stopTicker();
  const failParams = snap.params || {};
  setJob(null);
  const err = snap.error || "未知错误";
  setHead(state.sentTopic, "生成失败", "失败", "bad");
  renderFailure(body, {
    title: "生成失败",
    message: err,
    detail: `行业包：${failParams.pack || "-"} · 模型：${state.meta?.model || "-"}`,
    retryLabel: "用同样的参数重试",
  }, {
    onRetry: () => send({ ...failParams }, { asVersionOf: body }),
    onOpenSettings: () => openSettings("llm"),
  });
  toast("生成失败：" + err, 5000);
  T.follow(true);
  loadSessions();
}

function onCancelled(body) {
  setBusy(false);
  stopTicker();
  setJob(null);
  setHead("新对话", "本次生成已取消", "");
  if (body) {
    const vi = body._vi;
    if (vi >= 0) body._versions[vi] = { ...body._versions[vi], state: "cancelled" };
    renderStopped(body, {
      onRetry: () => send({ topic: state.sentTopic }, { asVersionOf: body, keepTopic: true }),
      onRevary: () => send({ topic: state.sentTopic, reroll: true },
        { asVersionOf: body, keepTopic: true }),
    });
  }
  state.activeBody = null;
  toast("已停止本次生成");
  loadSessions();
}

// ── 停止 ────────────────────────────────────────────────────
export async function abort() {
  const job = state.job;
  if (!job) return;
  stopPolling();
  const body = job.body || state.activeBody;
  let err = null;
  try {
    await api.cancel(job.id);
  } catch (e) {
    err = e.message || "未知原因";
  }
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
      onRetry: () => { state.pollMisses = 0; setBusy(true, true); poll(); },
      onOpenSettings: () => openSettings("llm"),
    });
    loadSessions();
    return;
  }
  onCancelled(body);
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
  $("empty").classList.add("hidden");
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

/** 打开一条历史记录（产物已落盘）。 */
export async function openRecord(id) {
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
  $("empty").classList.add("hidden");
  $("stale-banner").classList.add("hidden");
  state.paramsSnapshot = null;                 // 历史结果没有「当时的界面参数」可比
  state.sentTopic = r.params?.topic || "";

  // 失败/取消的历史记录没有 result，只有作业摘要
  if (!r.sections) {
    T.addUserMsg(state.sentTopic, { onEdit: onEditUserMsg });
    const body = T.addAssistantMsg();
    renderFailure(body, {
      title: r.state === "cancelled" ? "这次生成被取消了" : "这次生成失败了",
      message: r.error || "没有留下产物",
      detail: "产物未落盘，因此无法回看内容。可以按下面的按钮用相同参数重来一次。",
      retryLabel: "用同样的参数重来",
    }, {
      onRetry: () => send({ ...(r.params || {}) }, { asVersionOf: body }),
      onOpenSettings: () => openSettings("llm"),
    });
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

  setBusy(true, true);
  toast("正在重写这一段…");
  try {
    await api.rewrite(jid, index, feedback);
    await waitRewriteDone(jid, body, vi);
    toast("已重写并复检");
  } catch (e) {
    toast("重写失败：" + e.message, 4000);
  }
  setBusy(false);
  stopTicker();
}

/** 等待单段重写结束。
 *  修复前：没有空值守卫（切走就 TypeError）、不认 cancelled（点了停止会空转 180s）。
 *  ⚠ 槽位 `vi` 必须由调用方传进来、不能在这里现取 `body._vi`：等结果的这几秒里
 *     用户可能已经翻了版本，现取会把重写结果写进他**正在看**的那一版 ——
 *     正是这次要修的同一个 bug 的另一种发生方式。 */
async function waitRewriteDone(jid, body, vi) {
  const deadline = Date.now() + REWRITE_TIMEOUT_MS;
  while (Date.now() < deadline) {
    if (!document.contains(body)) return;              // 会话已切换：静默放弃
    // P2-9：轮询用轻量快照（不含 result 的几十 KB 产物），并每轮刷新进度。
    // 修复前 api.job(jid, true) 每 700ms 序列化整份产物，且 waitRewriteDone
    // 全程不调 renderProgress —— 唯一的反馈是 2.2s 就消失的 toast；后端还在跑、
    // 前端却已抛「重写超时」，两边相反。
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
      return;
    }
    if (snap.state === "cancelled") return;             // 用户停了，不再空转
    if (snap.state === "failed") throw new Error(snap.error || "重写失败");
    await new Promise(r => setTimeout(r, 700));
  }
  throw new Error("重写超时（超过 4 分钟），可稍后回看该记录");
}

export function autoGrowTopic() {
  const t = $("topic");
  if (!t) return;
  t.style.height = "auto";
  t.style.height = Math.min(t.scrollHeight, 140) + "px";
}
