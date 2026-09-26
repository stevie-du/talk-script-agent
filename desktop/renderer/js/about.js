// 关于与更新面板（自动更新方案 §4.2）。
//
// 职责边界：**主进程给事实，这里给界面**。主进程推来的 status 只有
// state / version / percent / text / detail / logPath（七态，见
// desktop/main.js 的 pushUpdaterStatus）；文案怎么组织、按钮显不显、
// 禁用不禁用，全在这一层。error / disabled 的 text 是例外 —— 那句人话
// 由主进程的 classifyError / disabledStatus 生成，与 updater.log 里的
// 原文一一对应，两边不能是两套说法。
//
// 三条铁律落在本文件：
// ① 绑定函数包 util.bindOnce（UI-LAYOUT-RULES 第〇·五节）—— 绑定混着
//    addEventListener 会重复挂，一次点击触发两次；rebind 断言会抓。
// ② 模板串里不许出现 style=" —— CSP 由引擎中间件**全量响应**下发
//    （app/server.py 的 CSP 常量，style-src 'self' 且无 'unsafe-inline'），
//    渲染层与引擎同源、同受管辖，verify.js 全程监听
//    securitypolicyviolation，违规即红。颜色一律走现成 class 与 token。
// ③ 新 button 的高度归 --h-btn-sm（28px）档：.page-actions button 的
//    现成规则已经管到了（styles.css 1305-1313），不要再单独设高度。

import { $, toast, bindOnce } from "./util.js";

// 七态 → 界面。state 是主进程 `pushUpdaterStatus` 的原值，不猜：
//   idle / checking / uptodate / available / downloading / downloaded / error
//   （disabled 是「没开」，文案由主进程给，走 text 直出，见下）
function statusText(s) {
  switch (s.state) {
    case "idle":
      // 常驻态：不说「尚未检查更新」这种一次性的废话。自动更新是**一直在跑**的
      // 事实（启动 15s 后首查、之后每 6h），这一行要让「有这个功能」本身可见。
      return "自动更新已开启 · 启动后每 6 小时检查一次";
    case "checking":
      return "正在检查更新…";
    case "uptodate":
      return `已是最新版本 ${s.version || ""}`.trim();
    case "available":
      return `发现新版本 ${s.version || ""}`.trim();
    case "downloading":
      // percent 是 download-progress 事件的字节级进度（主进程已 Math.floor）
      return `正在下载更新 ${s.percent || 0}%`;
    case "downloaded":
      return `${s.version || "新版本"} 已下载完成，退出 TalkScript 时自动安装`;
    case "error":
      return s.text || "检查更新失败";
    default:
      // dev / portable / 没配地址三种「没开」也走这里：text 由主进程
      // disabledStatus 给（三种文案不同，§4.3 三态表）。
      return s.text || s.state || "";
  }
}

// 状态行的颜色：复用现成 token，不引入新颜色。异常（含三种「没开」）用 --warn，
// 其余中性 —— 「没配更新地址」不是故障，但用户需要看见它（§3.3 失败必须可见）。
// 实现上靠 class：cfg-err 是 .hint 的现成警示变体（--warn-soft 底 + --warn 字，
// styles.css 266），与「配置错误行」「地址警告常驻行」同一族画法。
function renderUpd(s) {
  const line = $("upd-status");
  if (!line) return;
  const text = statusText(s);
  if (line.textContent !== text) line.textContent = text;
  line.classList.toggle("cfg-err", s.state === "error" || s.state === "disabled");

  // 检查中：按钮禁用 + 转圈文案。不用 style="" 改外观 —— 禁用态由
  // button:disabled 的现成样式承担（全局 button 规则里有 opacity/not-allowed）。
  const check = $("upd-check");
  if (check) {
    const busy = s.state === "checking";
    check.disabled = busy;
    if (busy) check.textContent = "检查中…";
    else if (check.textContent !== "检查更新") check.textContent = "检查更新";
  }

  // 【立即重启】只在 downloaded 态出现（§4.2 路径 2）。其余态一律藏起来：
  // 一个不能点的按钮比没有按钮更糟。
  const restart = $("upd-restart");
  if (restart) restart.classList.toggle("hidden", s.state !== "downloaded");

  // 明文更新地址的警示（`updater-core.feedUrlWarning` 给的一句话）：
  // 方案 §8 说「HTTPS 是底线」，但 http 地址**不拒**（本地冒烟要用），
  // 所以必须在这里说出来 —— 不说的话用户会以为这条链路是安全的。
  // 独立一行、与失败行同族画法（`.hint.cfg-err`）。
  const warn = $("upd-warn");
  if (warn) {
    const text = s.warning || "";
    if (warn.textContent !== text) warn.textContent = text;
    warn.classList.toggle("hidden", !text);
  }

  // 失败指路：一句人话（上面那行）+ 完整日志路径。日志路径由主进程给
  // （updaterLogPath），不在这里拼 —— 拼两份就一定会有不一致的一天。
  const log = $("upd-log");
  if (log) {
    const show = s.state === "error" && !!s.logPath;
    log.textContent = show ? `完整日志：${s.logPath}` : "";
    log.classList.toggle("hidden", !show);
  }
}

// 「立即重启」= quitAndInstall(isSilent=true, isForceRunAfter=true)。
// 走 IPC 而不是在渲染层直接调：装之前主进程要先回收引擎（孤儿 python.exe
// 会占端口与内存，单实例锁也救不了它，main.js 的 quitAndInstallUpdater）。
// 这条路用户已经明确点了按钮，**不再二次确认**（§4.2）。
function restartNow() {
  const u = window.talkscript && window.talkscript.updater;
  if (!u || !u.restart) return;          // 浏览器直开（非 Electron）：没有这条通道
  // 主进程会再核一次状态（不是 downloaded 就拒绝并留痕）。拒绝必须**说出来**：
  // 静默失败就是「点了没反应」，正是本方案要消掉的那类缺陷（§3.3）。
  Promise.resolve(u.restart()).then((r) => {
    if (r && r.ok === false) {
      toast(`现在不能安装更新（当前状态：${r.state || "未知"}）`, 3500);
    }
  }).catch((e) => {
    toast("立即重启失败：" + (e && e.message ? e.message : e), 4000);
  });
}

export const bindAbout = bindOnce(function bindAbout() {
  const check = $("upd-check");
  if (check) {
    check.addEventListener("click", () => {
      const u = window.talkscript && window.talkscript.updater;
      if (!u) {
        // 非 Electron（浏览器里直接打开 renderer）：说清楚，不留一颗死按钮
        toast("自动更新仅在桌面应用中可用", 3500);
        return;
      }
      // 手动检查的结果必须可见，包括「没开」这个答案 —— 主进程 handler 对三种
      // 「没开」返回不同文案（§4.3），返回的状态直接渲染。
      // 不 catch 是故意的：invoke 失败（主进程没这条 handler）会冒出来，
      // 静默吞掉等于「点了没反应」，正是本方案要消掉的那类缺陷。
      Promise.resolve(u.check()).then(renderUpd).catch((e) => {
        toast("检查更新失败：" + (e && e.message ? e.message : e), 4000);
      });
    });
  }
  const restart = $("upd-restart");
  if (restart) restart.addEventListener("click", restartNow);

  // 订阅主进程推送（含自动检查）：打开设置页之前发生的状态变化也要补上。
  // onStatus 返回取消订阅函数；本模块是单例、绑定只跑一次（bindOnce），
  // 所以不需要（也没有）退订路径 —— 订阅跟窗口一样长。
  const u = window.talkscript && window.talkscript.updater;
  if (u && u.onStatus) u.onStatus(renderUpd);
});

/** 当前版本号：读 `window.__ts.version`（源头 desktop/package.json，经
 *  /api/meta 到 state.meta），**不在这里猜、也不写死在 HTML 里** ——
 *  写死的表现是「升级后这里还显示旧版本」。
 *  放在 refreshAbout 里而不是 bindAbout 里：bindAbout 跑在 boot() 之前，
 *  那时 /api/meta 还没回来（boot 里 bindSettings 排在 await api.meta() 前），
 *  读到的会是空串。 */
function renderVersion() {
  const n = $("upd-version");
  if (!n) return;
  const v = (window.__ts && window.__ts.version) || "";
  n.textContent = v ? `TalkScript ${v}` : "读取中…";
}

/** 打开「关于与更新」面板时调一次：把当前状态补画一遍。
 *
 *  为什么需要：状态是主进程**推**的，而推送只发生在状态变化的那个瞬间。
 *  用户在更新已经下载完（主进程弹过确认框、用户点了「稍后」）之后才打开设置页，
 *  此时没有新事件 —— 不补画的话面板停在 HTML 里的「尚未检查更新」，
 *  而真相是「0.2.1 已下载完成」，「立即重启」按钮也不见。用户会以为没这功能。
 *
 *  ⚠ 走的是**只读**的 getStatus，不是 check。
 *  这里曾经调 check 并注明"多查一次无害" —— 那个判断漏了主进程的副作用：
 *  `runCheck` 的第一件事是 `updaterLastCheckAt = Date.now()`，于是每次打开面板
 *  都把 6h 自动检查的时钟往后推，「每 6 小时一次」实际变成"取决于用户点开面板的
 *  频率"（§1 / §4.1 写的是两次检查之间零请求）；顺带每次开面板都发一次真实请求。 */
export function refreshAbout() {
  renderVersion();
  const u = window.talkscript && window.talkscript.updater;
  if (!u || !u.getStatus) return;   // 浏览器直开 / 旧 preload：没有这条通道
  // 失败必须可见（§3.3）：以前是空 catch，面板会停在 HTML 默认的「尚未检查更新」
  // —— 一个看起来正常、实际没接上的状态。与同文件按钮路径的口径对齐。
  Promise.resolve(u.getStatus()).then(renderUpd).catch((e) => {
    toast("读取更新状态失败：" + (e && e.message ? e.message : e), 4000);
  });
}
