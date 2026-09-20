// 生成进度：步骤时间线 + 流式思考过程 + 状态行。
//
// 参考成熟 agent 的做法（Claude / Cursor 的 tool-use 展示）：
//  - 每一步都可见，并给出**耗时**，用户能判断是「模型在想」还是「卡住了」
//  - 已完成步骤紧凑展示；出错的那一步展开并带原因
//  - 流式思考独立折叠块，默认展开、完成后自动折叠
//  - 活体指示器（转圈 + 当前阶段 + 已用时）在思考流**下方**，跟着输出走
//
// 修复前这里只有一个扁平的 pstep 列表：没有耗时、没有总时长，失败时也只把
// 错误塞进同一行，用户看不出「哪一步失败、花了多久」。

import { $, esc } from "./util.js";
import { state } from "./store.js";

export const STATE_LABEL = {
  queued: "排队中",
  selecting: "选题策划中",
  writing: "文案撰写中",
  rewriting: "回炉改写中",
  checking: "代码校验中",
  done: "完成",
  failed: "失败",
  cancelled: "已取消",
};


function stepKind(key = "") {
  if (key.startsWith("select")) return "select";
  if (key.startsWith("write")) return "write";
  if (key.startsWith("check")) return "check";
  if (key.startsWith("rewrite")) return "rewrite";
  if (key.startsWith("retry")) return "retry";
  return "other";
}

function fmtDur(ms) {
  if (ms === null || ms === undefined || ms < 0) return "";
  const s = ms / 1000;
  if (s < 1) return `${Math.round(ms)}ms`;
  if (s < 60) return `${s.toFixed(1)}s`;
  return `${Math.floor(s / 60)}m${Math.round(s % 60)}s`;
}

/** 生成中的占位骨架（助手气泡的初始内容）。
 *
 *  顺序就是时间顺序：做完的（步骤）→ 正在想的（思考流）→ 此刻在干什么（状态行）。
 *  状态行放在思考流**下方**（2026-09-20，用户原话「转圈太难看了…可以参考其他
 *  智能体都是在思考下方」）：活体指示器跟着输出走，而不是在气泡顶上占一个比正文
 *  还大的标题位。原来那里同时有三处在报同一件事——顶上「正在生成…」、
 *  步骤行「文案撰写中」、思考块标题「文案撰写 · 思考过程」。
 *  现在只有状态行说阶段，思考块只说自己是「思考过程」。 */
export function placeholderBody() {
  return `<ol class="steps" id="step-track"></ol>
    <details class="think-stream hidden" id="think-stream" open>
      <summary><span class="ts-title">思考过程</span><span class="ts-meta"></span></summary>
      <pre class="ts-body"></pre>
    </details>
    <div class="gen-status" id="gen-status">
      <span class="spinner" aria-hidden="true"></span>
      <span class="gs-phase" id="gen-phase">正在生成…</span>
      <span class="gs-elapsed" id="gen-elapsed"></span>
    </div>`;
}

/** 刷新进度区。body 为助手消息节点；snap 为 /api/jobs 返回。 */
export function renderProgress(body, snap) {
  if (!body) return;
  const track = body.querySelector("#step-track");
  if (!track) return;

  const steps = snap.steps || [];
  const terminal = snap.state === "done" || snap.state === "failed" || snap.state === "cancelled";

  // 每步耗时 = 本步 ts 与下一步 ts 之差。⚠ 只有**下一步已经出现**才算得出耗时：
  // 在此之前这一步还在跑，给它显示一个每次轮询都变大一点的数字，就是用户报的
  // 「闪烁」—— 而且它和状态行的「已用 N 秒」是同一件事，报了两遍。
  const parts = [];
  for (let i = 0; i < steps.length; i++) {
    const cur = steps[i];
    const next = steps[i + 1];
    const t0 = cur.ts ? new Date(cur.ts).getTime() : null;
    const t1 = next?.ts ? new Date(next.ts).getTime() : null;
    const dur = (t0 !== null && t1 !== null && t1 >= t0) ? t1 - t0 : null;
    const kind = stepKind(cur.key);
    const note = cur.data?.note || "";
    const badge = note ? `<span class="step-note">${esc(note)}</span>` : "";
    const durHtml = dur !== null ? `<span class="step-dur">${fmtDur(dur)}</span>` : "";
    parts.push(`<li class="step done k-${kind}">
        <span class="step-dot"></span>
        <span class="step-t">${esc(cur.title)}</span>${badge}${durHtml}
      </li>`);
  }

  if (snap.state === "failed") {
    parts.push(`<li class="step err"><span class="step-dot"></span>
        <span class="step-t">${esc(snap.error || "生成失败")}</span></li>`);
  } else if (snap.state === "cancelled") {
    parts.push(`<li class="step stopped"><span class="step-dot"></span>
        <span class="step-t">已停止</span></li>`);
  }
  // 内容没变就**不重建 DOM**：轮询会一遍遍重画这块，整块重写会打掉列表里的
  // 文字选区，并让任何 CSS 动画从头开始 —— 那半截「一闪一闪」是这么来的，不是设计。
  const html = parts.join("");
  if (track._html !== html) { track._html = html; track.innerHTML = html; }

  // 进行中的阶段由状态行报（思考流下方那一行），不再往步骤列表里插一条
  // `.step.active` —— 它和状态行、思考块标题三处会同时说「文案撰写中」。
  const status = body.querySelector("#gen-status");
  const phase = body.querySelector("#gen-phase");
  const elapsed = body.querySelector("#gen-elapsed");
  if (status) status.classList.toggle("hidden", terminal);
  if (phase) phase.textContent = STATE_LABEL[snap.state] || "正在生成…";
  if (elapsed) elapsed.textContent = terminal ? "" : fmtElapsed(snap);
  renderThinkStream(body, snap);
}

let tickTimer = null;

/** 运行期每秒刷新一次已用时长（不重新请求，纯本地计时）。 */
export function startTicker(getSnap) {
  stopTicker();
  tickTimer = setInterval(() => {
    const snap = getSnap();
    const body = $("chat-stream")?.querySelector(".msg.msg-assistant:last-of-type .msg-body");
    const el = body?.querySelector("#gen-elapsed");
    if (el && snap) el.textContent = fmtElapsed(snap);
  }, 1000);
}

export function stopTicker() {
  clearInterval(tickTimer);
  tickTimer = null;
}

function fmtElapsed(snap) {
  const started = snap?.created_at ? new Date(snap.created_at).getTime() : null;
  if (!started || isNaN(started)) return "";
  const s = Math.max(0, (Date.now() - started) / 1000);
  if (s < 60) return `已用 ${s.toFixed(0)} 秒`;
  return `已用 ${Math.floor(s / 60)} 分 ${Math.round(s % 60)} 秒`;
}

/** 流式思考：只显示尾部（后端已截取），并自动滚到底。
 *  推理型模型「想」的时间远长于「写」的时间，把这部分露出来，
 *  用户就不会盯着「正在生成…」干等几十秒。 */
export function renderThinkStream(body, snap) {
  const box = body.querySelector("#think-stream");
  if (!box) return;
  const st = snap.stream;
  if (!st || (!st.reasoning_tail && !st.content_len)) {
    box.classList.add("hidden");
    return;
  }
  box.classList.remove("hidden");
  // 标题**不带阶段名**：紧挨着它下方的状态行已经在说「文案撰写中」了
  //（原来两处各写一遍，加上步骤行一共三遍）。后端 stream.phase 与
  // snap.state 是同一个阶段的两种写法，取 state 那份即可。
  box.querySelector(".ts-meta").textContent =
    `${st.reasoning_len ?? 0} 字` + (st.content_len ? ` · 正文 ${st.content_len} 字` : "");
  const pre = box.querySelector(".ts-body");
  pre.textContent = st.reasoning_tail || "（本阶段没有可展示的思考内容）";
  pre.scrollTop = pre.scrollHeight;
}

