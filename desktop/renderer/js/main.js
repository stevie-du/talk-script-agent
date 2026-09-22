// 启动与装配。
//
// 另外暴露一个显式的自动化接口 `window.__ts`：_verify/verify.js 是无依赖 CDP
// 驱动的界面回归网，它需要读一点内部状态、触发几个动作。修复前这些变量和函数
// 是散落的全局变量（busyNow / currentJob / setSettingsPane…），没有边界也没有
// 文档；现在收敛成一个带注释的接口，顺便让「测试依赖什么」变得可审阅。

import { $, esc, toast, dayGroupKey } from "./util.js";
import { api } from "./api.js";
import { state, detachJob } from "./store.js";
import * as T from "./thread.js";
import { abort, send, autoGrowTopic, collectParams, reattachBusyJob,
  stopAllBackgroundJobs } from "./jobs.js";
import { loadSessions, bindSessionList, busyRecords } from "./sessions.js";
import { bindSettings, openSettings, setPane, settingsOpen, closeSettings } from "./settings.js";
import { setHead, resultSrt, resultMarkdown, voicePlainText,
  errorText } from "./result.js";
import {
  bindShell, renderSamples, refreshGate, gotoView, fillPackSelect, setCfgHint,
  renderModelPicker, setLanding,
} from "./ui.js";
import { BUSY_STATES } from "./progress.js";

async function boot() {
  bindShell();
  bindSettings();

  bindSessionList();
  T.bindScrollPin();
  renderSamples();
  // 问候语按当前时刻落档（HTML 里那句是兜底文案，不参与展示）。
  setLanding(true);

  $("btn-generate").onclick = () => (state.busy ? abort() : send());
  $("btn-new-chat").onclick = () => { closeSettings(); gotoView("chat"); newChat(); };

  try {
    state.meta = await api.meta();
  } catch (e) {
    showEngineDown(e);
    return;
  }
  fillPackSelect();
  setCfgHint();
  // 模型选择器要在这里显式渲染一次：它平时挂在 meta 事件上，而 boot 里的
  // `state.meta = await api.meta()` 是**直接赋值**、不发事件 —— 不补这一句，
  // 首屏工具条上就没有模型名（原来头部的只读胶囊正是这么漏掉的）。
  renderModelPicker();
  const sessions = await loadSessions();
  refreshGate();
  autoGrowTopic();

  // 生成中刷新页面 / 重开窗口：以前这里什么都没有 —— 左栏那一行挂着一颗呼吸点，
  // 但界面上没有任何一条路径把渲染接回那个**已经在跑**的作业（缺陷 3）：
  // 发送键被 409 挡住、进度也看不见，用户只能等或者关掉窗口。
  // /api/history 会把内存里在跑的作业并进摘要（app/server.py 的 history()），
  // 所以「有没有在跑」读的就是刚拿到的这一份；接不接得回去问 /api/jobs/{id}。
  const running = (sessions || []).filter(it => BUSY_STATES.has(it.state || ""));
  let attached = false;
  for (const it of running) {
    if (await reattachBusyJob(it.id)) { attached = true; break; }
  }

  // 空态 hero 的「配置引导」那一态已下线：五个居中块叠着没有主次，
  // 而且「还没配模型」这件事工具条那颗胶囊已经在说了。
  // 只在「确实没配过」（无 Key 且无历史记录）时自动弹设置，避免打扰老用户。
  // 接上了一个在跑的作业时不弹 —— 那说明这台机器配过，弹上去反而把进度页顶走。
  const noKey = !state.meta.has_api_key && !state.meta.mock;
  if (!attached && noKey && !(sessions || []).length) openSettings("llm");
}

function newChat() {
  // 生成中点「新建对话」也要解锁，否则发送键永久变灰。
  // 但**不取消后端作业**：它照常跑，左栏「生成中」里留着入口，点回去即可接着看。
  if (state.busy) detachJob();
  T.clearThread();
  setLanding(true);
  $("stale-banner").classList.add("hidden");
  state.result = null;
  state.paramsSnapshot = null;
  state.sentTopic = "";
  setHead("新对话", "", "");
  $("topic").focus();
  // 「不取消」是有代价的：并发额度只有 4 条，连点四次新建对话之后再发就是 409，
  // 而那几条作业在界面上只剩左栏几颗呼吸点。所以这里必须**说出来**，
  // 并把「停止全部」那颗入口指给用户 —— 静默吃一个 409 是最坏的形态（缺陷 4）。
  loadSessions().then(() => {
    const bg = busyRecords();
    if (bg.length) toast(`这条新对话不含刚才那 ${bg.length} 条：它们仍在后台进行，`
      + `额度满了会被拒。下方「停止全部后台生成」可以一次停掉`, 6000);
  });
}

function showEngineDown(e) {
  // e.message 可能带上服务端 detail / 上游响应片段，进 innerHTML 前必须转义
  $("empty").innerHTML = `<h3>无法连接本地引擎</h3>
    <p class="hint">${esc(e.message || "")}<br>
    若在浏览器里直接打开，请使用启动时打印的带 token 的地址。</p>`;
  toast("无法连接本地引擎", 4000);
}

// ── 自动化 / 调试接口 ───────────────────────────────────────
// 只暴露必要的东西；verify.js 与人工排障都通过它，不再依赖散落的全局变量。
window.__ts = {
  // 不再写死：来自 /api/meta，源头是 desktop/package.json
  get version() { return state.meta ? state.meta.version : ""; },
  get meta() { return state.meta; },
  get busy() { return state.busy; },
  get job() { return state.job; },
  get result() { return state.result; },
  get jobId() { return state.job ? state.job.id : null; },
  get settingsOpen() { return settingsOpen(); },
  get settingsPane() { return state.settingsPane; },
  get msgCount() { return T.msgCount(); },
  // 参数真值的读取口。断言要靠它证明「设置页改的选项真的进了请求」——
  // 只看 DOM 是假的：这个 bug 的全部要害就是「DOM 变了但没人读」。
  collectParams,
  // 导出是纯函数，挂出来才能在验证脚本里断言**内容** ——
  // 只断言「点了不报错」是没用的：错误的字幕照样能顺利导出。
  exportSrt: resultSrt,
  exportMd: resultMarkdown,
  voicePlainText,
  errorText,
  send,
  abort,
  // 后台作业可见性：有几条在跑、以及那颗「停止全部」到底停了几条。
  loadSessions,
  busyRecords,
  stopAll: () => stopAllBackgroundJobs(),
  reattachBusyJob,
  openSettings,
  closeSettings,
  setPane,
  // 分组标签的日期读法 —— 纯函数，挂出来才能直接喂日期去断言格式
  // （只看 DOM 是空的：桩里的记录全是今天/昨天，永远量不到数字日期那一支）。
  dayGroupKey,
  gotoView,
  newChat,
  // 把全部绑定函数**再跑一遍**。幂等守卫的验证入口：
  // 绑定里混着 `addEventListener`（会重复挂，一次点击触发两次）与
  // `onclick =`（天然幂等），重复挂的症状离原因很远、很难查。
  // `_verify/verify.js` 会先数一遍监听器，调这里，再数一遍 —— 必须一个都没多。
  // 这条断言**同时**覆盖五个绑定函数：漏掉任何一个的 bindOnce 包装都会报红。
  rebind: () => {
    bindShell(); bindSettings(); bindSessionList(); T.bindScrollPin();
  },
};

// 兼容旧调用点（verify.js 以全局函数名调用）
window.gotoView = gotoView;
window.setSettingsPane = setPane;

// 便于在 devtools 里手改状态排障
window.__state = state;

boot().catch(e => {
  console.error(e);
  toast("启动失败：" + (e.message || e), 5000);
});
