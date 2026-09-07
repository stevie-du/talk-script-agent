// TalkScript 渲染层：与本地引擎 HTTP API 交互
// Electron 模式端口来自 query；浏览器直开引擎页时端口即 location.port
"use strict";

const qs = new URLSearchParams(location.search);
const API = `http://127.0.0.1:${qs.get("port") || location.port || "8765"}`;

const $ = id => document.getElementById(id);
const el = (tag, cls, html) => {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (html !== undefined) e.innerHTML = html;
  return e;
};
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

const STATE_LABEL = {
  queued: "排队中", selecting: "选题策划中", paused_awaiting_confirmation: "待确认选题",
  writing: "文案撰写中", rewriting: "回炉改写中", checking: "代码校验中",
  done: "完成", failed: "失败", cancelled: "已取消",
};
const TYPE_LABEL = { hook: "开场钩子", point: "要点", cta: "结尾引导" };
const FRONT_KEYS = ["segment", "audience", "duration", "platform"];
const MORE_KEYS = ["style", "persona", "cta"];
const KEY_FALLBACK_LABEL = { segment: "细分领域", audience: "受众", duration: "时长（秒）",
  style: "风格", platform: "平台", persona: "人设", cta: "结尾引导" };

let META = null;
let currentJob = null;
let currentResult = null;
let pollTimer = null;
let busyNow = false;
let genParamsSnapshot = null;   // 上次生成时的参数快照（检测"参数已改、结果未更新"）
let activeMsg = null;           // 当前正在生成/回写的消息节点（用于原地刷新状态与结果）
let sentTopic = "";             // 本次发送的主题（重跑 / 换角度重选时复用）

// ── 基础 ─────────────────────────────────────────────────
async function api(path, opts) {
  const r = await fetch(API + path, {
    headers: { "Content-Type": "application/json" }, ...opts,
    body: opts?.body ? JSON.stringify(opts.body) : undefined,
  });
  if (!r.ok) {
    let msg = `HTTP ${r.status}`;
    try { msg = (await r.json()).detail || msg; } catch (_) {}
    throw new Error(msg);
  }
  return r.json();
}

function toast(msg, ms = 2200) {
  const t = $("toast");
  t.textContent = msg;
  t.classList.remove("hidden");
  clearTimeout(t._h);
  t._h = setTimeout(() => t.classList.add("hidden"), ms);
}

function fmtText(s) {
  let h = esc(s);
  h = h.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
  h = h.replace(/\{\{([^}]+)\}\}/g, '<span class="over">{{待补：$1}}</span>');
  return h;
}

// ── 消息流 ───────────────────────────────────────────────
const stream = () => $("chat-stream");

function scrollBottom(smooth = true) {
  const s = stream();
  s.scrollTo({ top: s.scrollHeight, behavior: smooth ? "smooth" : "auto" });
}

// 用户消息：右侧蓝气泡
function addUserMsg(topic) {
  const m = el("div", "msg msg-user", "");
  m.innerHTML = `<div class="bub">${esc(topic)}</div>`;
  stream().appendChild(m);
  return m;
}

// 助手消息：左侧头像 + 内容区
function addAssistantMsg() {
  const m = el("div", "msg msg-assistant", "");
  m.innerHTML = `<div class="avatar">
      <svg viewBox="0 0 24 24" width="15" height="15" fill="currentColor" aria-hidden="true">
        <rect x="2" y="9" width="2.6" height="6" rx="1" opacity=".55"/>
        <rect x="6.5" y="5" width="2.6" height="14" rx="1" opacity=".78"/>
        <rect x="11" y="2" width="2.6" height="20" rx="1"/>
        <rect x="15.5" y="7" width="2.6" height="10" rx="1" opacity=".78"/>
        <rect x="19.5" y="10" width="2" height="4" rx=".9" opacity=".55"/>
      </svg>
    </div>`;
  const body = el("div", "msg-body");
  m.appendChild(body);
  stream().appendChild(m);
  return body;   // 返回内容区，后续往里填结果
}

// 生成中的占位：气泡 + 步骤状态
function placeholderBody() {
  return `<div class="thinking">
      <span class="spinner"></span>
      <span class="t-text">正在生成…</span>
      <span class="t-steps" id="step-track"></span>
    </div>
    <div class="hint" id="gen-hint"></div>`;
}

function setThinking(snap) {
  const body = activeMsg;
  if (!body) return;
  const track = body.querySelector("#step-track");
  if (!track) return;
  const parts = [];
  for (const s of snap.steps || []) parts.push(`<span class="pstep done">${esc(s.title)}</span>`);
  const cur = STATE_LABEL[snap.state];
  if (snap.state === "failed") {
    track.innerHTML = parts.join("") + `<span class="pstep err">✗ ${esc(snap.error || "失败")}</span>`;
  } else if (cur && cur !== "完成") {
    track.innerHTML = parts.join("") + `<span class="pstep active">${esc(cur)}…</span>`;
  } else {
    track.innerHTML = parts.join("");
  }
  body.querySelector("#gen-hint").textContent = snap.state === "failed" ? "" : "";
}

// 清空消息流（新建对话）
function clearChat() {
  stream().querySelectorAll(".msg").forEach(m => m.remove());
  $("empty").classList.remove("hidden");
  $("stale-banner").classList.add("hidden");
  currentResult = null; currentJob = null;
  genParamsSnapshot = null;
  activeMsg = null;
  clearTimeout(pollTimer);
}

// ── 启动 ─────────────────────────────────────────────────
async function boot() {
  try {
    META = await api("/api/meta");
  } catch (e) {
    $("empty").innerHTML = `<h3>无法连接本地引擎</h3><p class="hint">${esc(e.message)}<br>请确认引擎已启动（python -m app.server）</p>`;
    return;
  }
  const sel = $("pack");
  sel.innerHTML = "";
  for (const p of META.packs) {
    const o = el("option", "", esc(p.display_name) + (p.draft ? "（草稿）" : ""));
    o.value = p.name;
    sel.appendChild(o);
  }
  sel.value = META.default_pack;
  if (!sel.value) sel.selectedIndex = 0;
  sel.onchange = () => { renderPackParams(); updateStale(); };
  renderPackParams();
  loadSessions();
  bindStatic();
  refreshGate();
}

function currentPack() {
  return META?.packs.find(p => p.name === $("pack").value);
}

function paramSelect(key, def) {
  const wrap = el("div");
  wrap.appendChild(el("label", "lbl", esc(def.label || KEY_FALLBACK_LABEL[key] || key)));
  const s = el("select");
  s.id = `p-${key}`;
  for (const opt of def.options) {
    const o = el("option", "", esc(String(opt)));
    o.value = String(opt);
    s.appendChild(o);
  }
  s.value = String(def.default);
  wrap.appendChild(s);
  return wrap;
}

function renderPackParams() {
  const pack = currentPack();
  if (!pack) return;
  $("pack-badge").classList.toggle("hidden", !pack.draft);
  const front = $("param-front"), more = $("param-more");
  front.innerHTML = ""; more.innerHTML = "";
  const params = pack.params || {};
  const placed = new Set();
  for (const key of FRONT_KEYS) {
    if (!params[key]?.options?.length) continue;
    front.appendChild(paramSelect(key, params[key]));
    placed.add(key);
  }
  for (const key of MORE_KEYS) {
    if (!params[key]?.options?.length) continue;
    more.appendChild(paramSelect(key, params[key]));
    placed.add(key);
  }
  for (const key of Object.keys(params)) {
    if (placed.has(key) || !params[key]?.options?.length) continue;
    more.appendChild(paramSelect(key, params[key]));
  }
  beautifySelects();
}

function getParam(key) {
  if (key === "duration") {
    const h = $("p-duration");
    return h ? (h.value || null) : null;
  }
  const s = $("p-" + key);
  return s ? (s.value || null) : null;
}

// ── 自绘下拉：隐藏原生 select（仅作值容器），按钮 + 菜单替代其外观 ──
function beautifySelects(scope = document) {
  scope.querySelectorAll("select:not([data-beauty])").forEach(sel => {
    sel.dataset.beauty = "1";
    sel.classList.add("native-hidden");
    const wrap = el("div", "select-wrap");
    sel.parentNode.insertBefore(wrap, sel);
    wrap.appendChild(sel);
    const btn = el("button", "select-btn");
    btn.type = "button";
    btn.innerHTML = `<span class="sel-text"></span>
      <svg class="chev" viewBox="0 0 12 8" width="11" height="8" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M1 1.5L6 6.5L11 1.5"/></svg>`;
    const menu = el("div", "select-menu hidden");
    wrap.appendChild(btn);
    wrap.appendChild(menu);

    const sync = () => {
      const o = sel.options[sel.selectedIndex];
      btn.querySelector(".sel-text").textContent = o ? o.textContent : "";
      menu.querySelectorAll(".select-opt").forEach(d => d.classList.toggle("on", d.dataset.value === sel.value));
    };
    const build = () => {
      menu.innerHTML = "";
      Array.from(sel.options).forEach(o => {
        const d = el("div", "select-opt" + (o.value === sel.value ? " on" : ""),
          `<span>${esc(o.textContent)}</span><span class="tick">✓</span>`);
        d.dataset.value = o.value;
        d.onclick = () => {
          sel.value = o.value;
          sync();
          close();
          sel.dispatchEvent(new Event("change", { bubbles: true }));
        };
        menu.appendChild(d);
      });
    };
    const isOpen = () => !menu.classList.contains("hidden");
    const open = () => { build(); menu.classList.remove("hidden"); wrap.classList.add("open"); };
    const close = () => { menu.classList.add("hidden"); wrap.classList.remove("open"); };
    sel._syncDropdown = sync;

    btn.onclick = () => (isOpen() ? close() : open());
    btn.onkeydown = ev => {
      const opts = Array.from(menu.querySelectorAll(".select-opt"));
      if (ev.key === "Escape") { close(); return; }
      if (ev.key === "ArrowDown" || ev.key === "ArrowUp") {
        ev.preventDefault();
        if (!isOpen()) open();
        const idx = opts.findIndex(d => d.classList.contains("on"));
        const next = ev.key === "ArrowDown" ? Math.min(idx + 1, opts.length - 1) : Math.max(idx - 1, 0);
        opts.forEach(d => d.classList.remove("hover"));
        (opts[next] || opts[0]).classList.add("hover");
      } else if (ev.key === "Enter") {
        const hit = menu.querySelector(".select-opt.hover") || menu.querySelector(".select-opt.on");
        if (hit) hit.onclick();
      }
    };
    sync();
  });
  if (!window.__selOutsideBound) {
    window.__selOutsideBound = true;
    document.addEventListener("click", e => {
      document.querySelectorAll(".select-wrap.open").forEach(w => {
        if (!w.contains(e.target)) {
          w.classList.remove("open");
          w.querySelector(".select-menu").classList.add("hidden");
        }
      });
    });
  }
}

// ── 生成与轮询 ───────────────────────────────────────────
function collectParams() {
  return {
    pack: $("pack").value,
    topic: $("topic").value.trim(),
    segment: getParam("segment"), audience: getParam("audience"),
    duration: getParam("duration") ? Number(getParam("duration")) : null,
    style: getParam("style"), platform: getParam("platform"),
    persona: getParam("persona"), cta: getParam("cta"),
    facts: $("facts").value.trim() || null,
    mode: document.querySelector("input[name=mode]:checked").value,
    voice: $("voice") ? $("voice").value : "strong",
    format: $("format") ? $("format").value : "both",
  };
}

async function send(overrides = {}) {
  const base = collectParams();
  const params = { ...base, ...overrides };
  if (!params.topic) { toast("请输入主题"); return; }
  sentTopic = params.topic;
  try {
    const { job_id } = await api("/api/generate", { method: "POST", body: params });
    genParamsSnapshot = JSON.stringify(collectParams());
    updateStale();
    currentJob = { id: job_id, state: "queued" };
    currentResult = null;
    activeMsg = null;
    // 消息流：用户气泡 + 助手占位
    $("empty").classList.add("hidden");
    addUserMsg(params.topic);
    const body = addAssistantMsg();
    body.innerHTML = placeholderBody();
    activeMsg = body;
    $("topic").value = "";
    $("stale-banner").classList.add("hidden");
    setBusy(true, true);
    scrollBottom(false);
    poll();
  } catch (e) {
    setBusy(false);
    if (/API Key/.test(e.message)) {
      toast("先在设置里配置模型 API Key", 3500);
      openSettings();
    } else {
      toast("生成失败：" + e.message, 3500);
    }
  }
}

function poll() {
  clearTimeout(pollTimer);
  if (!currentJob) return;
  api(`/api/jobs/${currentJob.id}`).then(snap => {
    currentJob.state = snap.state;
    renderProgress(snap);
    if (snap.state === "paused_awaiting_confirmation") {
      setBusy(false);
      openConfirm(snap.result.plan);
    } else if (snap.state === "done") {
      setBusy(false);
      currentResult = snap.result;
      $("stale-banner").classList.add("hidden");
      const body = activeMsg || addAssistantMsg();
      body.innerHTML = "";
      activeMsg = body;
      renderResult(snap.result, body);
      loadSessions();
      scrollBottom();
    } else if (snap.state === "failed") {
      setBusy(false);
      currentJob = null;
      const body = activeMsg || addAssistantMsg();
      body.innerHTML = `<div class="banner warn">⛔ 生成失败：${esc(snap.error || "未知错误")}</div>`;
      activeMsg = null;
      toast("生成失败：" + (snap.error || "未知错误"), 5000);
      scrollBottom();
    } else if (snap.state === "cancelled") {
      setBusy(false);
      currentJob = null;
      const body = activeMsg;
      if (body) body.innerHTML = `<div class="hint">本次生成已取消</div>`;
      activeMsg = null;
      toast("本次生成已取消");
    } else {
      pollTimer = setTimeout(poll, 900);
    }
  }).catch(() => { pollTimer = setTimeout(poll, 1500); });
}

function renderProgress(snap) {
  const body = activeMsg;
  if (!body) return;
  setThinking(snap);
}

function setBusy(b, loading = false) {
  busyNow = b;
  const btn = $("btn-generate");
  btn.classList.toggle("loading", loading);
  btn.innerHTML = loading
    ? `<span class="spinner" style="border-top-color:#fff;border-color:#ffffff55;border-top-color:#fff"></span> 生成中…`
    : `<svg viewBox="0 0 24 24" width="16" height="16" fill="currentColor" aria-hidden="true"><path d="M12 2l1.8 6.2L20 10l-6.2 1.8L12 18l-1.8-6.2L4 10l6.2-1.8L12 2zM19 15l.9 3.1L23 19l-3.1.9L19 23l-.9-3.1L15 19l3.1-.9L19 15z"/></svg> 生成`;
  refreshGate();
}

// ── 防错与快捷键 ─────────────────────────────────────────
function refreshGate() {
  const btn = $("btn-generate");
  const empty = !$("topic").value.trim();
  btn.disabled = busyNow || empty;
  btn.title = empty ? "输入主题后发送" : "回车 / Ctrl + Enter 发送";
}

function updateStale() {
  const b = $("stale-banner");
  if (!currentResult || !genParamsSnapshot) { b.classList.add("hidden"); return; }
  const changed = JSON.stringify(collectParams()) !== genParamsSnapshot;
  b.classList.toggle("hidden", !changed);
  if (changed) b.textContent = "参数已修改，当前展示的是旧参数结果——重新发送即可生成新脚本";
}

function appConfirm(title, msg) {
  return new Promise(resolve => {
    $("cd-title").textContent = title;
    $("cd-msg").textContent = msg;
    $("confirm-dialog").classList.remove("hidden");
    const done = v => {
      $("confirm-dialog").classList.add("hidden");
      $("cd-yes").onclick = null; $("cd-no").onclick = null;
      resolve(v);
    };
    $("cd-yes").onclick = () => done(true);
    $("cd-no").onclick = () => done(false);
  });
}

// ── 结果渲染（消息局部渲染）───────────────────────────────
function nPoints(r) { return Math.max(r.sections.filter(s => s.type === "point").length, 1); }
function countCN(text) {
  return String(text).replace(/\s|／/g, "").replace(/\{\{[^}]*\}\}|\*|\[画面：[^\]]*\]/g, "").length;
}

function renderResult(r, body) {
  const ch = r.check || {};
  const hasSb = (r.storyboard || []).length > 0;

  // 1) 消息头：主题 + 指标速览 + 操作
  const head = el("div", "res-head");
  const dev = ch.deviation_pct ?? 0;
  const hardN = (ch.hard_hits || []).reduce((a, h) => a + h.count, 0);
  head.innerHTML = `
    <div class="res-top">
      <div class="res-title">${esc(r.params?.topic || "")}</div>
      <div class="res-tools">
        <button class="ghost" data-act="copy-voice" title="复制口播文案">复制口播</button>
        <button class="ghost" data-act="copy-json" title="复制结构化 JSON">复制 JSON</button>
        <button class="ghost" data-act="save-md" title="另存为 Markdown 文件">另存 MD</button>
        <button class="ghost" data-act="rerun" title="用当前参数重新生成">重跑</button>
      </div>
    </div>
    <div class="res-metric-list">
      <span class="m-chip">${ch.chars_total ?? "-"}/${ch.target_total ?? "-"} 字</span>
      <span class="m-chip">预计 ${ch.estimated_seconds ?? "-"}s / 目标 ${ch.duration_target ?? "-"}s</span>
      <span class="m-chip ${Math.abs(dev) <= 10 ? "good" : "bad"}">偏差 ${dev > 0 ? "+" : ""}${dev}%</span>
      <span class="m-chip ${hardN === 0 ? "good" : "bad"}">禁用词 ${hardN} 硬·${(ch.soft_hits || []).length} 待确认</span>
    </div>`;
  body.appendChild(head);

  // 2) 横幅
  const banners = [];
  if (!(ch.passed ?? true)) banners.push(`<div class="banner warn">⛔ ${esc((ch.blockers || []).join("；"))}——已达回炉上限，请人工调整或点「重跑」</div>`);
  if ((r.placeholders || []).length)
    banners.push(`<div class="banner warn">⚠ 含 ${r.placeholders.length} 处占位事实：${r.placeholders.map(esc).join("、")}——补充后再发布</div>`);
  if ((ch.soft_hits || []).length)
    banners.push(`<div class="banner info">待确认 ${ch.soft_hits.length} 词：${ch.soft_hits.map(h => esc(h.word) + "×" + h.count).join("、")}（语境正常即可放行）</div>`);
  if (r.pack_draft) banners.push(`<div class="banner warn">⚠ 本结果来自草稿行业包，内容需人工校对</div>`);
  const bw = el("div", "res-banners");
  bw.innerHTML = banners.join("");
  body.appendChild(bw);

  // 3) 口播分段卡片
  const segs = el("div", "seg-list");
  let pi = 0;
  const bodyQuota = Math.floor((r.quota?.body || 0) / nPoints(r));
  r.sections.forEach((s, i) => {
    const label = s.type === "point" ? `要点${++pi}` : TYPE_LABEL[s.type];
    const tm = (r.timings || [])[i];
    const chars = countCN(s.text);
    const card = el("div", `card ${s.type}`);
    card.style.setProperty("--i", i);
    card.innerHTML = `
      <div class="card-head">
        <span class="pill ${s.type}">${esc(label)}</span>
        <span class="meta">${tm ? `${tm["start"]}-${tm["end"]} 秒` : ""} · 约 ${chars} 字 · 字幕：${esc(s.subtitle || "—")}</span>
      </div>
      <div class="card-text">${fmtText(s.text)}</div>
      <div class="card-foot">
        <button class="ghost rw">✎ 重写本段</button>
        <input placeholder="给重写的反馈（可选），回车提交">
        ${s.type === "point" ? `<span class="quota-tag">配额 ≈${bodyQuota} 字</span>` : ""}
      </div>`;
    const input = card.querySelector("input");
    card.querySelector(".rw").onclick = () => {
      card.querySelector(".card-foot").classList.toggle("editing");
      input.focus();
    };
    input.onkeydown = ev => { if (ev.key === "Enter" && !busyNow) rewriteSegment(i, input.value.trim()); };
    segs.appendChild(card);
  });
  body.appendChild(segs);

  // 4) 折叠：分镜 / 合规 / JSON / 日志
  const acc = (title, contentHtml, open) => {
    const d = el("details", "acc" + (open ? " open" : ""));
    d.innerHTML = `<summary>${esc(title)}</summary>`;
    const inner = el("div", "acc-body");
    inner.innerHTML = contentHtml;
    d.appendChild(inner);
    return d;
  };
  const wrap = el("div", "acc-list");
  if (hasSb) wrap.appendChild(acc("分镜", renderStoryboard(r), true));
  wrap.appendChild(acc("合规检查", renderCompliance(r)));
  wrap.appendChild(acc("JSON", `<pre class="code">${esc(JSON.stringify(r, null, 2))}</pre>`));
  wrap.appendChild(acc("日志", renderLogs(r.logs || [])));
  body.appendChild(wrap);

  // 5) 操作绑定（消息级）
  head.querySelector('[data-act="copy-voice"]').onclick = () =>
    copyText(r.sections.map(s => s.text).join("\n\n"), "口播已复制");
  head.querySelector('[data-act="copy-json"]').onclick = () =>
    copyText(JSON.stringify(r, null, 2), "JSON 已复制");
  head.querySelector('[data-act="save-md"]').onclick = () => {
    const blob = new Blob([resultMarkdown(r)], { type: "text/markdown" });
    const a = el("a");
    a.href = URL.createObjectURL(blob);
    a.download = `口播脚本_${(r.params?.topic || "").slice(0, 12)}.md`;
    a.click();
  };
  head.querySelector('[data-act="rerun"]').onclick = () => {
    $("topic").value = r.params?.topic || "";
    send({ topic: r.params?.topic || "", mode: r.params?.mode, voice: r.params?.voice, format: r.params?.format });
  };
  scrollBottom();
}

function renderStoryboard(r) {
  if (!(r.storyboard || []).length)
    return `<p class="hint">本次未生成分镜（输出内容选择了「仅口播」）。</p>`;
  const rows = (r.storyboard || []).map((sh, i) => {
    const tm = (r.timings || [])[i];
    return `<tr>
      <td>${esc(sh.time || (tm ? `${tm["start"]}-${tm["end"]}s` : ""))}</td>
      <td>${esc(sh.shot || "")}</td><td>${esc(sh.subtitle || "")}</td>
      <td>${esc(sh.sfx || "")}</td><td>${esc(sh.note || "")}</td></tr>`;
  }).join("");
  return `<div class="tbl-wrap"><table>
    <thead><tr><th style="width:86px">时间</th><th>画面/景别</th><th>字幕</th><th>音效/BGM</th><th>拍摄提示</th></tr></thead>
    <tbody>${rows}</tbody></table></div>`;
}

function renderCompliance(r) {
  const ch = r.check || {};
  const hardN = (ch.hard_hits || []).reduce((a, h) => a + h.count, 0);
  const rows = [
    ["硬禁用词（必改）", hardN === 0 ? `<span class="ok">✅ 无</span>`
      : `<span class="bad">${ch.hard_hits.map(h => `${esc(h.word)}×${h.count}`).join("、")}</span>`],
    ["待确认（语境相关）", (ch.soft_hits || []).length
      ? ch.soft_hits.map(h => `${esc(h.word)}×${h.count}`).join("、") : `<span class="ok">✅ 无</span>`],
    ["字数与时长", `${ch.chars_total}/${ch.target_total} 字 · 偏差 ${ch.deviation_pct > 0 ? "+" : ""}${ch.deviation_pct}% → `
      + (ch.passed ? `<span class="ok">✅ 合格</span>` : `<span class="bad">❌ ${esc((ch.blockers || []).join("；"))}</span>`)],
    ["占位事实", (r.placeholders || []).length ? `⚠ ${r.placeholders.map(esc).join("、")}` : `<span class="ok">✅ 无</span>`],
    ["回炉/重写记录", (r.revisions || []).length
      ? r.revisions.map((v, i) => `第${i + 1}次：${esc(v.action || v.feedback || "单段重写")}`).join("；") : "无"],
    ["行业包状态", r.pack_draft ? `⚠ 草稿包，内容需人工校对` : `<span class="ok">✅ 精修包</span>`],
  ];
  return `<div class="tbl-wrap"><table>
    <thead><tr><th style='width:160px'>检查项</th><th>结果</th></tr></thead>
    <tbody>${rows.map(([k, v]) => `<tr><td>${k}</td><td>${v}</td></tr>`).join("")}</tbody></table></div>`;
}

function renderLogs(logs) {
  if (!logs?.length) return `<p class="hint">暂无日志</p>`;
  return (logs || []).map(s =>
    `<details class="log-block"><summary>${esc(s.ts || "")} · ${esc(s.title)}</summary><pre>${esc(JSON.stringify(s.data, null, 1))}</pre></details>`
  ).join("");
}

async function rewriteSegment(index, feedback) {
  if (!currentJob || !currentResult || busyNow) return;
  setBusy(true, true);
  toast("重写中…");
  try {
    await api(`/api/jobs/${currentJob.id}/rewrite_segment`, {
      method: "POST", body: { index, feedback: feedback || null },
    });
    await waitDone();
    toast("已重写并复检");
  } catch (e) { toast("重写失败：" + e.message, 3500); }
  setBusy(false);
}

async function waitDone(timeout = 180) {
  const deadline = Date.now() + timeout * 1000;
  while (Date.now() < deadline) {
    const snap = await api(`/api/jobs/${currentJob.id}`);
    if (snap.state === "done") {
      currentResult = snap.result;
      const body = activeMsg;
      body.innerHTML = "";
      renderResult(snap.result, body);
      loadSessions();
      return;
    }
    if (snap.state === "failed") throw new Error(snap.error || "失败");
    await new Promise(r => setTimeout(r, 900));
  }
  throw new Error("超时");
}

// ── 会话记录（左栏）──────────────────────────────────────
async function loadSessions() {
  let items = [];
  try { items = await api("/api/history"); } catch (_) {}
  $("sess-count").textContent = items.length ? `${items.length}` : "";
  const list = $("session-list");
  list.innerHTML = "";
  if (!items.length) {
    list.appendChild(el("p", "hint sess-empty", "还没有会话记录，先发一条试试"));
    return;
  }
  for (const it of items) {
    const row = el("div", "sess-item");
    const time = it.created_at.slice(5, 16).replace("T", " ");
    row.innerHTML = `
      <div class="sess-top"><span class="dot ${it.passed ? "" : "no"}"></span>
        <span class="sess-topic">${esc(it.topic)}</span></div>
      <div class="sess-sub">${esc(it.pack)} · ${time} · ${it.duration ?? "-"}s</div>`;
    row.onclick = () => openSession(it.id);
    const del = el("button", "sess-del", "删除");
    del.onclick = async ev => {
      ev.stopPropagation();
      if (!(await appConfirm("删除记录", `「${it.topic.slice(0, 20)}」删除后不可恢复。`))) return;
      try {
        await api(`/api/history/${it.id}`, { method: "DELETE" });
        toast("已删除");
        loadSessions();
      } catch (e) { toast("删除失败：" + e.message, 3500); }
    };
    row.appendChild(del);
    list.appendChild(row);
  }
}

async function openSession(id) {
  try {
    const r = await api(`/api/history/${id}`);
    currentResult = r;
    currentJob = null;          // 历史回看不可再重写/轮询
    $("empty").classList.add("hidden");
    addUserMsg(r.params?.topic || "");
    const body = addAssistantMsg();
    renderResult(r, body);
    scrollBottom();
  } catch (e) { toast("回看失败：" + e.message, 3500); }
}

// ── 分步确认卡 ───────────────────────────────────────────
function openConfirm(plan) {
  $("cf-angle").value = plan.angle || "";
  $("cf-hooktype").value = plan.hook_type || "";
  $("cf-hookline").value = plan.hook_line || "";
  $("cf-cta").value = plan.cta || "";
  $("cf-points").value = (plan.points || []).join("\n");
  $("confirm-overlay").classList.remove("hidden");
}

function editedPlan() {
  return {
    angle: $("cf-angle").value.trim(),
    hook_type: $("cf-hooktype").value.trim(),
    hook_line: $("cf-hookline").value.trim(),
    points: $("cf-points").value.split("\n").map(s => s.trim()).filter(Boolean),
    cta: $("cf-cta").value.trim(),
  };
}

// ── 设置 / 新建包 ────────────────────────────────────────
async function openSettings() {
  const c = await api("/api/config");
  $("st-baseurl").value = c.base_url || "";
  $("st-model").value = c.model || "";
  $("st-apikey").value = "";
  $("st-status").textContent = c.mock ? "当前为 mock 模式（未配置 Key）"
    : c.api_key_set ? `已配置 Key（模型 ${c.model}）` : "未配置 API Key";
  $("settings-overlay").classList.remove("hidden");
}

async function saveSettings() {
  const body = {
    base_url: $("st-baseurl").value.trim(),
    model: $("st-model").value.trim(),
  };
  const key = $("st-apikey").value.trim();
  if (key) body.api_key = key;
  await api("/api/config", { method: "POST", body });
  toast("已保存，下次生成即生效");
  $("settings-overlay").classList.add("hidden");
}

async function runPackgen() {
  const industry = $("pg-industry").value.trim();
  const desc = $("pg-desc").value.trim();
  if (!industry || !desc) { toast("请填写行业名称和业务描述"); return; }
  $("pg-run").disabled = true;
  $("pg-run").textContent = "生成中（约 1-2 分钟）…";
  try {
    const out = await api("/api/packs/create", {
      method: "POST", body: { industry, description: desc },
    });
    $("pg-form").classList.add("hidden");
    $("pg-result").classList.remove("hidden");
    $("pg-checklist").textContent = out.checklist;
    toast(`行业包「${out.display_name}」已生成（草稿）`);
  } catch (e) {
    toast("生成失败：" + e.message, 5000);
  }
  $("pg-run").disabled = false;
  $("pg-run").textContent = "生成";
}

// ── 复制 / 另存 ─────────────────────────────────────────

function resultMarkdown(r) {
  const p = r.params;
  const lines = [`# 口播脚本：${p.topic}`, "",
    `- ${p.duration}s / ${p.platform} / ${p.style} / ${p.persona}（${r.pack}）`, "", "## 口播文案", ""];
  r.sections.forEach((s, i) => {
    const tm = r.timings?.[i];
    lines.push(`**【${TYPE_LABEL[s.type]}】** ${tm ? `${tm["start"]}-${tm["end"]}秒` : ""}`);
    lines.push(s.text, "");
  });
  const ch = r.check;
  lines.push(`---`, `字数 ${ch.chars_total}/${ch.target_total} 字 · 预估 ${ch.estimated_seconds}s · 偏差 ${ch.deviation_pct}% · ${ch.passed ? "合格" : ch.blockers.join("；")}`);
  if (r.placeholders?.length) lines.push("", `> 占位事实：${r.placeholders.join("、")}`);
  return lines.join("\n");
}

async function copyText(text, tip) {
  try { await navigator.clipboard.writeText(text); toast(tip); }
  catch (_) { toast("复制失败", 1500); }
}

async function openPackInfo() {
  const name = $("pack").value;
  const p = await api(`/api/packs/${name}`);
  $("pi-title").textContent = `${p.display_name || name} · 包内容`;
  $("pi-desc").textContent = (p.description || "") + (p.draft ? "（草稿包：内容需人工校对后投产）" : "");
  const cw = $("pi-checklist-wrap");
  if (p.checklist) {
    cw.classList.remove("hidden");
    $("pi-checklist").textContent = p.checklist;
  } else {
    cw.classList.add("hidden");
  }
  const box = $("pi-files");
  box.innerHTML = "";
  const tb = el("table");
  tb.innerHTML = "<thead><tr><th>文件</th><th style='width:90px'>大小</th><th style='width:110px'>说明</th></tr></thead>";
  const body = el("tbody");
  const role = rel => {
    if (rel === "pack.yaml") return "包清单";
    if (rel === "skill.yaml") return "技能（怎么写）";
    if (rel === "banwords.yaml") return "禁用词表";
    if (rel === "校对清单.md") return "草稿校对";
    if (rel.startsWith("knowledge/")) return "知识库";
    if (rel.startsWith("compliance/")) return "合规";
    if (rel.startsWith("patterns/")) return "方法库";
    if (rel.startsWith("rules/")) return "规则";
    if (rel.startsWith("private/")) return "私有资料";
    return "";
  };
  for (const f of p.files || []) {
    const tr = el("tr");
    const kb = f.size > 1024 ? (f.size / 1024).toFixed(1) + " KB" : f.size + " B";
    tr.innerHTML = `<td style="font-family:Consolas,monospace;font-size:12px">${esc(f.rel)}</td>
      <td>${kb}</td><td>${role(f.rel)}</td>`;
    body.appendChild(tr);
  }
  tb.appendChild(body);
  box.appendChild(tb);
  $("packinfo-overlay").classList.remove("hidden");
}

// ── 事件绑定 ─────────────────────────────────────────────
function setLeftFolded(folded) {
  $("left").classList.toggle("folded", folded);
  localStorage.setItem("ts.left.folded", folded ? "1" : "0");
  const btn = $("btn-toggle-left");
  btn.classList.toggle("on", folded);
  btn.title = folded ? "展开会话栏（Ctrl+\\）" : "收起会话栏（Ctrl+\\）";
}

function bindStatic() {
  if (localStorage.getItem("ts.left.folded") === "1") setLeftFolded(true);
  $("btn-toggle-left").onclick = () => setLeftFolded(!$("left").classList.contains("folded"));
  $("btn-new-chat").onclick = () => { clearChat(); $("topic").focus(); };

  // 发送：按钮 / Ctrl+Enter / Enter（非 Shift）
  $("btn-generate").onclick = send;
  $("topic").addEventListener("keydown", e => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      if (!busyNow && $("topic").value.trim()) send();
    }
  });
  $("topic").addEventListener("input", () => {
    refreshGate();
    autoGrow();
  });

  // 参数变更：检测结果过期
  document.addEventListener("change", e => {
    if (e.target.closest("#cfg-wrap, #left")) updateStale();
  });

  // Enter 换行自动增高 / 恢复单行
  function autoGrow() {
    const t = $("topic");
    t.style.height = "auto";
    t.style.height = Math.min(t.scrollHeight, 140) + "px";
  }

  document.addEventListener("keydown", e => {
    if ((e.ctrlKey || e.metaKey) && e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      if (!busyNow && $("topic").value.trim()) send();
    }
    if (e.ctrlKey && e.key === "\\") {
      e.preventDefault();
      setLeftFolded(!$("left").classList.contains("folded"));
    }
    if (e.key === "Escape") {
      document.querySelectorAll(".overlay:not(.hidden)").forEach(o => o.classList.add("hidden"));
    }
  });

  $("btn-settings").onclick = openSettings;
  $("st-close").onclick = () => $("settings-overlay").classList.add("hidden");
  $("st-save").onclick = saveSettings;
  $("btn-packinfo").onclick = () => openPackInfo().catch(e => toast("读取失败：" + e.message, 3500));
  $("pi-close").onclick = () => $("packinfo-overlay").classList.add("hidden");
  $("pi-export").onclick = async () => {
    const name = $("pack").value;
    $("pi-export").disabled = true;
    try {
      const out = await api(`/api/packs/${name}/export-skill`, { method: "POST", body: {} });
      $("pi-export-hint").innerHTML =
        `✅ 已导出 <b>${esc(out.files)}</b> 个文件到：<br><code>${esc(out.path)}</code><br>${esc(out.hints.join(" "))}`;
    } catch (e) { toast("导出失败：" + e.message, 4000); }
    $("pi-export").disabled = false;
  };
  $("btn-newpack").onclick = () => {
    $("pg-form").classList.remove("hidden");
    $("pg-result").classList.add("hidden");
    $("packgen-overlay").classList.remove("hidden");
  };
  $("pg-close").onclick = () => $("packgen-overlay").classList.add("hidden");
  $("pg-run").onclick = runPackgen;
  $("pg-done").onclick = async () => {
    $("packgen-overlay").classList.add("hidden");
    await boot();
    $("pack").value = $("pack").options[$("pack").options.length - 1].value;
    $("pack").dispatchEvent(new Event("change"));
  };
  $("cf-continue").onclick = async () => {
    $("confirm-overlay").classList.add("hidden");
    setBusy(true, true);
    try {
      await api(`/api/jobs/${currentJob.id}/confirm`, { method: "POST", body: { plan: editedPlan() } });
      poll();
    } catch (e) { setBusy(false); toast("确认失败：" + e.message, 3500); }
  };
  $("cf-reselect").onclick = () => {
    $("confirm-overlay").classList.add("hidden");
    send({ topic: sentTopic, mode: "step" });
    toast("正在换个角度重选…");
  };
  $("cf-cancel").onclick = async () => {
    $("confirm-overlay").classList.add("hidden");
    try { await api(`/api/jobs/${currentJob.id}/cancel`, { method: "POST", body: {} }); } catch (_) {}
    currentJob = null;
    if (activeMsg) activeMsg.innerHTML = `<div class="hint">本次生成已取消</div>`;
    activeMsg = null;
  };
}

boot();