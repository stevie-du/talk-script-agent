// 启动与装配。
//
// 另外暴露一个显式的自动化接口 `window.__ts`：_verify/verify.js 是无依赖 CDP
// 驱动的界面回归网，它需要读一点内部状态、触发几个动作。修复前这些变量和函数
// 是散落的全局变量（busyNow / currentJob / setSettingsPane…），没有边界也没有
// 文档；现在收敛成一个带注释的接口，顺便让「测试依赖什么」变得可审阅。

import { $, esc, toast } from "./util.js";
import { api } from "./api.js";
import { state, detachJob } from "./store.js";
import * as T from "./thread.js";
import { abort, send, autoGrowTopic } from "./jobs.js";
import { loadSessions, bindSessionList } from "./sessions.js";
import { bindSettings, openSettings, setPane, settingsOpen, closeSettings } from "./settings.js";
import { bindOverlays } from "./overlays.js";
import { setHead, resultSrt, resultMarkdown, voicePlainText,
  errorText } from "./result.js";
import {
  bindShell, renderSamples, refreshGate, gotoView, fillPackSelect, setCfgHint,
  renderSetupNeeded,
} from "./ui.js";

async function boot() {
  bindShell();
  bindSettings();
  bindOverlays();
  bindSessionList();
  T.bindScrollPin();
  renderSamples();

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
  const sessions = await loadSessions();
  refreshGate();
  autoGrowTopic();

  // 首启引导：安装包不带任何配置，没 Key 就什么都生成不了。
  // 只在「确实没配过」（无 Key 且无历史记录）时自动弹设置，避免打扰老用户。
  const noKey = !state.meta.has_api_key && !state.meta.mock;
  if (noKey) {
    renderSetupNeeded();
    if (!(sessions || []).length) openSettings("llm");
  }
}

function newChat() {
  // 生成中点「新建对话」也要解锁，否则发送键永久变灰。
  // 但**不取消后端作业**：它照常跑，左栏「生成中」里留着入口，点回去即可接着看。
  if (state.busy) detachJob();
  T.clearThread();
  $("empty").classList.remove("hidden");
  $("stale-banner").classList.add("hidden");
  state.result = null;
  state.paramsSnapshot = null;
  state.sentTopic = "";
  setHead("新对话", "", "");
  $("topic").focus();
  loadSessions();
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
  // 导出是纯函数，挂出来才能在验证脚本里断言**内容** ——
  // 只断言「点了不报错」是没用的：错误的字幕照样能顺利导出。
  exportSrt: resultSrt,
  exportMd: resultMarkdown,
  voicePlainText,
  errorText,
  send,
  abort,
  loadSessions,
  openSettings,
  closeSettings,
  setPane,
  gotoView,
  newChat,
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
