// 右栏头部（#rh-title / #rh-sub / #rh-state）的唯一写入口。
//
// 为什么单独一个模块：头部是**壳层**（跟 `.right-head` 同族），而写它的需求分两类 ——
// 对话链路（jobs.js / result.js / main.js 的 newChat）与非对话视图（topics.js）。
// 修复前只有前者：`ui.js` 的 `gotoView()` 只管显隐、`data-view` 与左栏高亮，
// 从不碰头部，于是点「今日选题」顶部仍写着上一条会话留下的「新对话」。
//
// 两份真值、一个出口：
//   chatHead   —— 会话头部（生成中 / 完成 / 失败 / 取消…），由 setHead 写
//   viewHeads  —— 各视图自己的头部，由 setViewHead 按视图名写
// `paintHead()` 按 `#right[data-view]` 决定落哪一份，而 `setHead` 在非对话视图
// **只暂存不落 DOM**。于是「生成中切去看选题」不再两边互相覆盖：作业照常更新
// chatHead，切回会话时由 gotoView → paintHead 一次到位 —— 这正是
// `topics.js` 里「状态不敢写全局头部」那条注释所担心的形态。

import { $ } from "./util.js";
import { CHAT } from "./words.js";

const chatHead = { title: CHAT.title, sub: "", state: "", cls: "" };
/** 视图名 → { title, sub }。按视图存，不留"上一个视图的文案"串到新视图上。 */
const viewHeads = {};

function currentView() {
  return $("right")?.dataset.view || "chat";
}

/** 会话头部。签名与搬过来时一致，调用点只换 import 来源。 */
export function setHead(title, sub, stateText, cls) {
  chatHead.title = title || CHAT.title;
  chatHead.sub = sub || "";
  chatHead.state = stateText || "";
  chatHead.cls = cls || "";
  paintHead();
}

/** 某个视图的头部文案。`#rh-state` 不给非对话视图 —— 那是对话链路的记号。 */
export function setViewHead(view, title, sub) {
  viewHeads[view] = { title, sub: sub || "" };
  paintHead();
}

export function paintHead() {
  const title = $("rh-title");
  if (!title) return;
  const view = currentView();
  const vh = view === "chat" ? null : viewHeads[view];
  const sub = $("rh-sub");
  const st = $("rh-state");
  if (vh) {
    title.textContent = vh.title;
    sub.textContent = vh.sub;
    st.textContent = "";
    st.className = "rh-state";
    return;
  }
  title.textContent = chatHead.title;
  sub.textContent = chatHead.sub;
  st.textContent = chatHead.state;
  st.className = "rh-state" + (chatHead.cls ? " " + chatHead.cls : "");
}
