// 消息流：用户气泡 / 助手气泡 / 版本切换 / 滚动跟随。
//
// 两个来自成熟 agent 的交互模式在这里落地：
//  · 可编辑的用户消息（ChatGPT / Claude 的铅笔按钮）——打错字不用重开一条，
//    改完就地重新生成，线程不会被「我先说的是…」这类澄清消息污染。
//  · 助手回复的**版本导航**（Claude 的「1 / 3」翻页）——「换一版」不再是把
//    旧结果丢掉重画，而是同一个问题下的第 N 个版本，可以来回对照。
//
// 修复前的问题：每次生成都往流里追加一对气泡，但没有「同一问题的多版本」概念，
// 点「换一版」看起来像结果被覆盖；且 renderResult 每次都无条件 scrollBottom()，
// 用户上滚查看前文时会被强行拽回底部。

import { $, el, esc, toast, bindOnce } from "./util.js";
import { state } from "./store.js";

export const streamEl = () => $("chat-stream");

// ── 滚动跟随 ────────────────────────────────────────────────
// 只有当用户本来就在底部附近时才自动跟随；否则出现「回到最新」按钮。
const NEAR_BOTTOM_PX = 80;

export function nearBottom() {
  const s = streamEl();
  return s.scrollHeight - s.scrollTop - s.clientHeight < NEAR_BOTTOM_PX;
}

export function scrollBottom(smooth = true) {
  const s = streamEl();
  s.scrollTo({ top: s.scrollHeight, behavior: smooth ? "smooth" : "auto" });
}

export const bindScrollPin = bindOnce(function bindScrollPin() {
  const s = streamEl();
  const btn = $("scroll-bottom");
  const sync = () => btn?.classList.toggle("hidden", nearBottom());
  s.addEventListener("scroll", sync, { passive: true });
  btn.onclick = () => scrollBottom();
  sync();
});

/** 跟随到底部——仅当用户没在往回翻。 */
export function follow(force = false) {
  if (force || nearBottom()) scrollBottom();
}

// ── 消息节点 ────────────────────────────────────────────────
export function addUserMsg(topic, { onEdit } = {}) {
  const m = el("div", "msg msg-user");
  m.innerHTML = `<div class="bub bubble user">${esc(topic)}</div>
    <button class="msg-edit" title="编辑主题并重新生成" aria-label="编辑主题">
      <svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor"
           stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
        <path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z"/></svg>
    </button>`;
  m._topic = topic;
  const btn = m.querySelector(".msg-edit");
  btn.onclick = () => beginEdit(m, onEdit);
  streamEl().appendChild(m);
  return m;
}

function beginEdit(m, onEdit) {
  if (m._editing) return;
  m._editing = true;
  const bub = m.querySelector(".bub");
  const old = m._topic;
  bub.innerHTML = `<textarea class="bub-edit" rows="1"></textarea>
    <div class="bub-actions">
      <button class="ghost tiny" data-a="cancel">取消</button>
      <button class="primary tiny" data-a="go">重新生成</button>
    </div>`;
  const ta = bub.querySelector("textarea");
  ta.value = old;
  ta.style.height = Math.min(ta.scrollHeight, 120) + "px";
  ta.focus();
  ta.setSelectionRange(old.length, old.length);
  const done = (go) => {
    m._editing = false;
    const v = ta.value.trim();
    bub.innerHTML = esc(old);
    if (go && v && v !== old) {
      m._topic = v;
      bub.textContent = v;
      onEdit?.(m, v);
    } else if (go && !v) {
      toast("主题不能为空");
    }
  };
  bub.querySelector('[data-a="cancel"]').onclick = () => done(false);
  bub.querySelector('[data-a="go"]').onclick = () => done(true);
  ta.addEventListener("keydown", e => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); done(true); }
    if (e.key === "Escape") { e.preventDefault(); done(false); }
  });
}

const AVATAR = `<div class="avatar">
    <svg viewBox="0 0 24 24" width="15" height="15" fill="currentColor" aria-hidden="true">
      <rect x="2" y="9" width="2.6" height="6" rx="1" opacity=".55"/>
      <rect x="6.5" y="5" width="2.6" height="14" rx="1" opacity=".78"/>
      <rect x="11" y="2" width="2.6" height="20" rx="1"/>
      <rect x="15.5" y="7" width="2.6" height="10" rx="1" opacity=".78"/>
      <rect x="19.5" y="10" width="2" height="4" rx=".9" opacity=".55"/>
    </svg>
  </div>`;

/** 助手气泡。返回的 body 支持多版本（见 pushVersion）。 */
export function addAssistantMsg() {
  const m = el("div", "msg msg-assistant");
  m.innerHTML = AVATAR;
  const body = el("div", "msg-body");
  body._versions = [];
  body._vi = -1;
  m.appendChild(body);
  streamEl().appendChild(m);
  return body;
}

// ── 版本 ────────────────────────────────────────────────────
/** 追加一个版本并切过去。entry: {id, result, state, error, params} */
export function pushVersion(body, entry) {
  body._versions.push(entry);
  body._vi = body._versions.length - 1;
  return body._vi;
}

export function currentVersion(body) {
  if (!body || body._vi < 0) return null;
  return body._versions[body._vi] || null;
}

/** 版本导航条：只有多于 1 个版本时才显示（Claude 的「1 / 2」翻页）。 */
export function renderVersionBar(body, onSwitch) {
  const n = body._versions.length;
  let bar = body.querySelector(".ver-bar");
  if (n <= 1) { bar?.remove(); return; }
  if (!bar) {
    bar = el("div", "ver-bar");
    body.insertBefore(bar, body.firstChild);
  }
  const i = body._vi;
  bar.innerHTML = `<button class="ver-btn" data-d="-1" ${i <= 0 ? "disabled" : ""}
      title="上一个版本" aria-label="上一个版本">‹</button>
    <span class="ver-label">版本 ${i + 1} / ${n}</span>
    <button class="ver-btn" data-d="1" ${i >= n - 1 ? "disabled" : ""}
      title="下一个版本" aria-label="下一个版本">›</button>`;
  bar.querySelectorAll(".ver-btn").forEach(b => {
    b.onclick = () => {
      const next = body._vi + Number(b.dataset.d);
      if (next < 0 || next >= n) return;
      body._vi = next;
      onSwitch(body);
    };
  });
}

export function clearThread() {
  streamEl().querySelectorAll(".msg").forEach(m => m.remove());
  state.activeBody = null;
}

/** 从某个用户消息之后的内容全部丢弃（编辑后重新生成时用）。 */
export function truncateAfter(userNode) {
  let n = userNode.nextElementSibling;
  while (n) {
    const next = n.nextElementSibling;
    n.remove();
    n = next;
  }
  state.activeBody = null;
}

export function msgCount() {
  return streamEl().querySelectorAll(".msg").length;
}
