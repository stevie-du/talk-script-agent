// 生成进度：步骤时间线 + 流式思考过程。
//
// 参考成熟 agent 的做法（Claude / Cursor 的 tool-use 展示）：
//  - 每一步都可见，并给出**耗时**，用户能判断是「模型在想」还是「卡住了」
//  - 已完成步骤紧凑展示；出错的那一步展开并带原因
//  - 流式思考独立折叠块，默认展开、完成后自动折叠
//
// 修复前这里只有一个扁平的 pstep 列表：没有耗时、没有总时长，失败时也只把
// 错误塞进同一行，用户看不出「哪一步失败、花了多久」。

import { $, esc } from "./util.js";
import { state } from "./store.js";

export const STATE_LABEL = {
  queued: "排队中",
  selecting: "选题策划中",
  paused_awaiting_confirmation: "待确认选题",
  writing: "文案撰写中",
  rewriting: "回炉改写中",
  checking: "代码校验中",
  done: "完成",
  failed: "失败",
  cancelled: "已取消",
};

const STEP_ICON = {
  select: "选题",
  write: "撰写",
  check: "校验",
  rewrite: "回写",
  retry: "重试",
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

/** 生成中的占位骨架（助手气泡的初始内容）。 */
export function placeholderBody() {
  return `<div class="thinking">
      <span class="spinner"></span>
      <span class="t-text">正在生成…</span>
      <span class="t-elapsed" id="gen-elapsed"></span>
    </div>
    <ol class="steps" id="step-track"></ol>
    <details class="think-stream hidden" id="think-stream" open>
      <summary><span class="ts-title">思考过程</span><span class="ts-meta"></span></summary>
      <pre class="ts-body"></pre>
    </details>
    <div class="hint" id="gen-hint"></div>`;
}

/** 刷新进度区。body 为助手消息节点；snap 为 /api/jobs 返回。 */
export function renderProgress(body, snap) {
  if (!body) return;
  const track = body.querySelector("#step-track");
  if (!track) return;

  const steps = snap.steps || [];
  const terminal = snap.state === "done" || snap.state === "failed" || snap.state === "cancelled";

  // 每步耗时 = 本步 ts 与下一步 ts 之差；最后一步用「现在」兜底
  const rows = [];
  for (let i = 0; i < steps.length; i++) {
    const cur = steps[i];
    const next = steps[i + 1];
    const t0 = cur.ts ? new Date(cur.ts).getTime() : null;
    const t1 = next?.ts ? new Date(next.ts).getTime()
      : (terminal ? null : Date.now());
    const dur = (t0 !== null && t1 !== null && t1 >= t0) ? t1 - t0 : null;
    rows.push({ step: cur, dur });
  }

  const parts = rows.map(r => {
    const kind = stepKind(r.step.key);
    const note = r.step.data?.note || "";
    const badge = note ? `<span class="step-note">${esc(note)}</span>` : "";
    const dur = r.dur !== null ? `<span class="step-dur">${fmtDur(r.dur)}</span>` : "";
    return `<li class="step done k-${kind}">
        <span class="step-dot"></span>
        <span class="step-t">${esc(r.step.title)}</span>${badge}${dur}
      </li>`;
  });

  if (snap.state === "failed") {
    parts.push(`<li class="step err"><span class="step-dot"></span>
        <span class="step-t">${esc(snap.error || "生成失败")}</span></li>`);
  } else if (snap.state === "cancelled") {
    parts.push(`<li class="step stopped"><span class="step-dot"></span>
        <span class="step-t">已停止</span></li>`);
  } else if (!terminal) {
    const cur = STATE_LABEL[snap.state];
    if (cur && cur !== "完成") {
      parts.push(`<li class="step active"><span class="step-dot"></span>
          <span class="step-t">${esc(cur)}</span></li>`);
    }
  }
  track.innerHTML = parts.join("");

  const elapsed = body.querySelector("#gen-elapsed");
  if (elapsed) {
    elapsed.textContent = terminal ? "" : fmtElapsed(snap);
  }
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
  box.querySelector(".ts-title").textContent = `${st.phase || "模型"} · 思考过程`;
  box.querySelector(".ts-meta").textContent =
    `${st.reasoning_len ?? 0} 字` + (st.content_len ? ` · 正文 ${st.content_len} 字` : "");
  const pre = box.querySelector(".ts-body");
  pre.textContent = st.reasoning_tail || "（本阶段没有可展示的思考内容）";
  pre.scrollTop = pre.scrollHeight;
}

export { STEP_ICON };
