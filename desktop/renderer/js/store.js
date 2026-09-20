// 应用状态与事件总线。
//
// 修复前这些变量散在 app.js 顶部（META / currentJob / currentResult / busyNow /
// genParamsSnapshot / activeMsg / sentTopic…），任何模块都能随手改，出问题时
// 很难判断「谁把它改成 null 的」。现在集中在这里，并且只通过 setXxx() 修改。

export const state = {
  meta: null,            // /api/meta 的返回
  job: null,             // { id, state, params } —— 当前挂着的作业
  result: null,          // 当前展示的结果
  busy: false,           // 是否有作业在跑（决定发送键是「发送」还是「停止」）
  loading: false,        // 本次是「生成中」而非「停止后待重挂」
  paramsSnapshot: null,  // 上次生成时的参数快照（检测「参数已改、结果未旧」）
  sentTopic: "",         // 本次发送的主题（重跑 / 换角度重选复用）
  activeBody: null,      // 正在生成/回写的助手消息节点
  pollTimer: null,
  pollMisses: 0,
  settingsPane: "gen",
  // 生成参数的**唯一真值**。工具条胶囊与设置页「生成参数」卡只是同一批参数的两个视图。
  // 修前两处各存各的：胶囊带 id、卡片只有 data-key，于是卡片改了没有任何代码去读；
  // 而 style / persona / cta 只在卡片里出现 —— 界面上能选、看起来也生效，
  // 生成时永远送 null 走包默认值。这是最坏的一种静默降级。
  genParams: {},
};

/** 写一个生成参数。空串/null 视为"不覆盖"，删掉键以退回包默认。 */
export function setGenParam(key, value) {
  if (value === null || value === undefined || value === "") delete state.genParams[key];
  else state.genParams[key] = String(value);
}

/** 换行业包 = 参数集与每项默认值全变，旧选择必须整体清空。 */
export function resetGenParams() { state.genParams = {}; }

const handlers = new Map();

export function on(evt, fn) {
  if (!handlers.has(evt)) handlers.set(evt, new Set());
  handlers.get(evt).add(fn);
  return () => handlers.get(evt)?.delete(fn);
}

export function emit(evt, payload) {
  handlers.get(evt)?.forEach(fn => {
    try { fn(payload); } catch (e) { console.error(`[${evt}]`, e); }
  });
}

export function setJob(job) {
  state.job = job;
  emit("job", job);
}

export function setResult(result) {
  state.result = result;
  emit("result", result);
}

export function setBusy(busy, loading = false) {
  state.busy = busy;
  state.loading = busy && loading;
  emit("busy", { busy: state.busy, loading: state.loading });
}

export function stopPolling() {
  clearTimeout(state.pollTimer);
  state.pollTimer = null;
}

/** 离开当前生成上下文：停轮询 + 解锁，但**不取消后端作业**（它照常跑，
 *  左栏「生成中」分组里始终留着入口，点回去即可接着看）。 */
export function detachJob() {
  stopPolling();
  setJob(null);
  setBusy(false);
  state.pollMisses = 0;
}
