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
  sel.onchange = () => renderPackParams();
  renderPackParams();
  updateHistBadge();
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

async function generate(overrides) {
  const params = { ...collectParams(), ...(overrides || {}) };
  if (!params.topic) { toast("请先填写主题"); return; }
  try {
    const { job_id } = await api("/api/generate", { method: "POST", body: params });
    genParamsSnapshot = JSON.stringify(collectParams());
    updateStale();
    currentJob = { id: job_id, state: "queued" };
    currentResult = null;
    showTab("voice");
    $("seg-cards").innerHTML = "";
    $("metrics").classList.add("hidden");
    $("banners").innerHTML = "";
    $("result-actions").classList.add("hidden");
    $("tabsbar").classList.remove("hidden");
    $("empty").classList.add("hidden");
    setBusy(true, true);
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
      $("progress-panel").classList.add("hidden");
      renderResult(snap.result);
      updateHistBadge();
    } else if (snap.state === "failed") {
      setBusy(false);
      renderProgress(snap);
      toast("生成失败：" + (snap.error || "未知错误"), 5000);
    } else if (snap.state === "cancelled") {
      setBusy(false);
      $("progress-panel").classList.add("hidden");
      toast("本次生成已取消");
      currentJob = null;
    } else {
      pollTimer = setTimeout(poll, 900);
    }
  }).catch(() => { pollTimer = setTimeout(poll, 1500); });
}

function setBusy(b, loading = false) {
  busyNow = b;
  refreshGate();
  const btn = $("btn-generate");
  btn.classList.toggle("loading", loading);
  btn.innerHTML = loading
    ? `<span class="spinner" style="border-top-color:#fff;border-color:#ffffff55;border-top-color:#fff"></span> 生成中…`
    : `<svg viewBox="0 0 24 24" width="16" height="16" fill="currentColor" aria-hidden="true"><path d="M12 2l1.8 6.2L20 10l-6.2 1.8L12 18l-1.8-6.2L4 10l6.2-1.8L12 2zM19 15l.9 3.1L23 19l-3.1.9L19 23l-.9-3.1L15 19l3.1-.9L19 15z"/></svg> 生成脚本`;
  if (!loading) refreshGate();
}

// ── 防错与快捷键（Nielsen：错误预防 / 灵活高效）─────────
function refreshGate() {
  const btn = $("btn-generate");
  const empty = !$("topic").value.trim();
  btn.disabled = busyNow || empty;
  btn.title = empty ? "先填写主题" : "Ctrl + Enter 快捷生成";
}

function updateStale() {
  const b = $("stale-banner");
  if (!currentResult || !genParamsSnapshot) { b.classList.add("hidden"); return; }
  const changed = JSON.stringify(collectParams()) !== genParamsSnapshot;
  b.classList.toggle("hidden", !changed);
  if (changed) b.textContent = "参数已修改，当前结果基于旧参数——点「生成脚本」重新生成";
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

function renderProgress(snap) {
  const panel = $("progress-panel");
  panel.classList.remove("hidden");
  const parts = [`<span class="ptitle"><span class="spinner"></span>生成中</span>`];
  for (const s of snap.steps || []) {
    parts.push(`<span class="pstep done">${esc(s.title)}</span>`);   // 勾由 CSS ::before 添加
  }
  const cur = STATE_LABEL[snap.state];
  if (snap.state === "failed") {
    parts.push(`<span class="pstep err">✗ ${esc(snap.error || "失败")}</span>`);
  } else if (cur && cur !== "完成") {
    parts.push(`<span class="pstep active">${esc(cur)}…</span>`);
  }
  panel.innerHTML = parts.join("");
}

// ── 结果渲染 ─────────────────────────────────────────────
// ── 视图切换（空状态 / 结果 / 历史）───────────────────────
function showTab(name) {
  document.querySelectorAll(".tabpane").forEach(p => { if (p.id !== "history-view") p.classList.add("hidden"); });
  document.querySelectorAll(".tab").forEach(t => t.classList.toggle("active", t.dataset.tab === name));
  $("tab-" + name).classList.remove("hidden");
  $("empty").classList.add("hidden");
  $("history-view").classList.add("hidden");
  $("tabsbar").classList.remove("hidden");
}

function showEmptyView() {
  $("history-view").classList.add("hidden");
  $("tabsbar").classList.add("hidden");
  document.querySelectorAll(".tabpane").forEach(p => { if (p.id !== "history-view") p.classList.add("hidden"); });
  $("empty").classList.remove("hidden");
}

function showResultView() {
  $("history-view").classList.add("hidden");
  $("empty").classList.add("hidden");
  $("tabsbar").classList.remove("hidden");
  const active = document.querySelector(".tab.active")?.dataset.tab || "voice";
  document.querySelectorAll(".tabpane").forEach(p => { if (p.id !== "history-view") p.classList.add("hidden"); });
  $("tab-" + active).classList.remove("hidden");
}

async function showHistoryView() {
  $("empty").classList.add("hidden");
  $("tabsbar").classList.add("hidden");
  document.querySelectorAll(".tabpane").forEach(p => { if (p.id !== "history-view") p.classList.add("hidden"); });
  $("history-view").classList.remove("hidden");
  await loadHistory();
}

async function loadHistory() {
  const items = await api("/api/history");
  $("hist-sub").textContent = items.length ? `共 ${items.length} 条 · 点击行回看` : "";
  const wrap = $("history-list");
  wrap.innerHTML = "";
  const tb = el("table");
  tb.innerHTML = "<thead><tr><th style='width:84px'>状态</th><th style='width:130px'>时间</th><th>主题</th>"
    + "<th style='width:90px'>行业包</th><th style='width:64px'>时长</th><th style='width:64px'>字数</th>"
    + "<th style='width:64px'>操作</th></tr></thead>";
  const body = el("tbody");
  if (!items.length) {
    const tr = el("tr");
    tr.innerHTML = `<td colspan="7" style="text-align:center;color:var(--faint);padding:30px">还没有生成记录</td>`;
    body.appendChild(tr);
  }
  for (const it of items) {
    const tr = el("tr");
    tr.innerHTML = `<td><span class="row-status"><span class="dot ${it.passed ? "" : "no"}"></span>${it.passed ? "合格" : "未过"}</span></td>
      <td>${esc(it.created_at.slice(0, 16).replace("T", " "))}</td>
      <td>${esc(it.topic)}</td><td>${esc(it.pack)}</td>
      <td>${it.duration ?? "-"}s</td><td>${it.chars ?? "-"}</td>
      <td><button class="ghost hist-del">删除</button></td>`;
    tr.onclick = async () => {
      currentResult = await api(`/api/history/${it.id}`);
      showResultView();
      renderResult(currentResult);
    };
    tr.querySelector(".hist-del").onclick = async ev => {
      ev.stopPropagation();
      if (!(await appConfirm("删除记录", `「${it.topic.slice(0, 20)}」删除后不可恢复。`))) return;
      try {
        await api(`/api/history/${it.id}`, { method: "DELETE" });
        toast("已删除");
        loadHistory();
        updateHistBadge();
      } catch (e) { toast("删除失败：" + e.message, 3500); }
    };
    body.appendChild(tr);
  }
  tb.appendChild(body);
  wrap.appendChild(tb);
}

async function updateHistBadge() {
  try {
    const items = await api("/api/history");
    const c = $("hist-count");
    c.textContent = items.length;
    c.classList.toggle("hidden", !items.length);
  } catch (_) {}
}

function renderResult(r) {
  showTab("voice");
  // 分镜页签按内容显隐（仅口播时不出现）
  const hasSb = (r.storyboard || []).length > 0;
  document.querySelector('[data-tab=storyboard]').classList.toggle("hidden", !hasSb);
  if (!hasSb && document.querySelector(".tab.active")?.dataset.tab === "storyboard") showTab("voice");
  renderVoice(r);
  renderStoryboard(r);
  renderCompliance(r);
  $("json-view").textContent = JSON.stringify(r, null, 2);
  renderLogs(r.logs || []);
}

function nPoints(r) { return Math.max(r.sections.filter(s => s.type === "point").length, 1); }
function countCN(text) {
  return String(text).replace(/\s|／/g, "").replace(/\{\{[^}]*\}\}|\*|\[画面：[^\]]*\]/g, "").length;
}

function renderVoice(r) {
  const ch = r.check || {};
  // 指标卡
  const hardN = (ch.hard_hits || []).reduce((a, h) => a + h.count, 0);
  const softN = (ch.soft_hits || []).length;
  const dev = ch.deviation_pct ?? 0;
  const devCls = Math.abs(dev) <= 10 ? "good" : "bad1";
  const hardCls = hardN === 0 ? "good" : "bad1";
  $("metrics").classList.remove("hidden");
  $("metrics").innerHTML = `
    <div class="metric"><div class="m-lbl">字数</div>
      <div class="m-val">${ch.chars_total ?? "-"} <small>/ ${ch.target_total ?? "-"}</small></div>
      <div class="m-sub">实际 / 配额</div></div>
    <div class="metric"><div class="m-lbl">预估时长</div>
      <div class="m-val">${ch.estimated_seconds ?? "-"}s <small>/ ${ch.duration_target ?? "-"}s</small></div>
      <div class="m-sub">语速 ${ch.rate ?? "-"} 字/秒</div></div>
    <div class="metric ${devCls}"><div class="m-lbl">时长偏差</div>
      <div class="m-val">${dev > 0 ? "+" : ""}${dev}%</div>
      <div class="m-sub">${Math.abs(dev) <= 10 ? "✓ ±10% 内" : "✗ 超容差"}</div></div>
    <div class="metric ${hardCls}"><div class="m-lbl">禁用词</div>
      <div class="m-val">${hardN} <small>硬</small> · ${softN} <small>待确认</small></div>
      <div class="m-sub">${hardN === 0 ? "✓ 无必改项" : "✗ 有必改项"}</div></div>`;

  // 横幅
  const banners = [];
  if (!(ch.passed ?? true)) banners.push(`<div class="banner warn">⛔ ${esc((ch.blockers || []).join("；"))}——已达回炉上限，请人工调整或点「重跑」</div>`);
  if ((r.placeholders || []).length)
    banners.push(`<div class="banner warn">⚠ 含 ${r.placeholders.length} 处占位事实：${r.placeholders.map(esc).join("、")}——补充后再发布</div>`);
  if ((ch.soft_hits || []).length)
    banners.push(`<div class="banner info">待确认 ${ch.soft_hits.length} 词：${ch.soft_hits.map(h => esc(h.word) + "×" + h.count).join("、")}（语境正常即可放行）</div>`);
  if (r.pack_draft) banners.push(`<div class="banner warn">⚠ 本结果来自草稿行业包，内容需人工校对</div>`);
  $("banners").innerHTML = banners.join("");

  // 分段卡片
  const box = $("seg-cards");
  box.innerHTML = "";
  let pi = 0;
  const bodyQuota = Math.floor((r.quota?.body || 0) / nPoints(r));
  r.sections.forEach((s, i) => {
    const label = s.type === "point" ? `要点${++pi}` : TYPE_LABEL[s.type];
    const tm = (r.timings || [])[i];
    const chars = countCN(s.text);
    const card = el("div", `card ${s.type}`);
    card.style.setProperty("--i", i);   // staggered 入场
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
    box.appendChild(card);
  });
  $("result-actions").classList.remove("hidden");
}

function renderStoryboard(r) {
  const box = $("storyboard");
  box.innerHTML = "";
  if (!(r.storyboard || []).length) {
    box.appendChild(el("p", "hint", "本次未生成分镜（输出内容选择了「仅口播」）。需要分镜请在「更多设置 → 输出内容」切换后重新生成。"));
    return;
  }
  const wrap = el("div", "tbl-wrap");
  const tb = el("table");
  tb.innerHTML = `<thead><tr><th style="width:86px">时间</th><th>画面/景别</th><th>字幕</th><th>音效/BGM</th><th>拍摄提示</th></tr></thead>`;
  const body = el("tbody");
  for (const [i, sh] of (r.storyboard || []).entries()) {
    const tm = (r.timings || [])[i];
    const tr = el("tr");
    tr.innerHTML = `<td>${esc(sh.time || (tm ? `${tm["start"]}-${tm["end"]}s` : ""))}</td>
      <td>${esc(sh.shot || "")}</td><td>${esc(sh.subtitle || "")}</td>
      <td>${esc(sh.sfx || "")}</td><td>${esc(sh.note || "")}</td>`;
    body.appendChild(tr);
  }
  tb.appendChild(body);
  wrap.appendChild(tb);
  box.appendChild(wrap);
}

function renderCompliance(r) {
  const ch = r.check || {};
  const box = $("compliance");
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
  const wrap = el("div", "tbl-wrap");
  const tb = el("table");
  tb.innerHTML = "<thead><tr><th style='width:160px'>检查项</th><th>结果</th></tr></thead>";
  const body = el("tbody");
  for (const [k, v] of rows) {
    const tr = el("tr");
    tr.innerHTML = `<td>${k}</td><td>${v}</td>`;
    body.appendChild(tr);
  }
  tb.appendChild(body);
  wrap.appendChild(tb);
  box.innerHTML = "";
  box.appendChild(wrap);
}

function renderLogs(logs) {
  const box = $("logs");
  box.innerHTML = "";
  for (const s of logs) {
    const d = el("details", "log-block");
    d.innerHTML = `<summary>${esc(s.ts || "")} · ${esc(s.title)}</summary>`;
    d.appendChild(el("pre", "", esc(JSON.stringify(s.data, null, 1))));
    box.appendChild(d);
  }
  if (!logs.length) box.appendChild(el("p", "hint", "暂无日志"));
}

async function rewriteSegment(index, feedback) {
  if (!currentResult || busyNow) return;   // 防并发：重写进行中忽略再次提交
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
    if (snap.state === "done") { currentResult = snap.result; renderResult(snap.result); return; }
    if (snap.state === "failed") throw new Error(snap.error || "失败");
    await new Promise(r => setTimeout(r, 900));
  }
  throw new Error("超时");
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
  // 只提交界面上的字段；temperature/retries 等高级项保留 config.yaml 中的值
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
// 左栏折叠：记忆偏好，Ctrl+\ 或顶栏按钮切换
function setLeftFolded(folded) {
  $("left").classList.toggle("folded", folded);
  localStorage.setItem("ts.left.folded", folded ? "1" : "0");
  const btn = $("btn-toggle-left");
  btn.classList.toggle("on", folded);
  btn.title = folded ? "展开参数面板（Ctrl+\\）" : "收起参数面板（Ctrl+\\）";
}
function bindStatic() {
  if (localStorage.getItem("ts.left.folded") === "1") setLeftFolded(true);
  $("btn-toggle-left").onclick = () => setLeftFolded(!$("left").classList.contains("folded"));
  $("btn-generate").onclick = () => generate();
  document.querySelectorAll(".tab").forEach(t => t.onclick = () => showTab(t.dataset.tab));
  // 防错：主题为空时生成按钮禁用；参数变更时检测结果过期
  $("topic").addEventListener("input", () => { refreshGate(); updateStale(); });
  document.addEventListener("change", e => {
    if (e.target.closest("#left")) updateStale();
  });
  // 快捷键：Ctrl/Cmd+Enter 生成；Esc 关闭弹层
  document.addEventListener("keydown", e => {
    if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
      e.preventDefault();
      if (!busyNow && $("topic").value.trim()) generate();
    }
    if (e.ctrlKey && e.key === "\\") {
      e.preventDefault();
      setLeftFolded(!$("left").classList.contains("folded"));
    }
    if (e.key === "Escape") {
      document.querySelectorAll(".overlay:not(.hidden)").forEach(o => o.classList.add("hidden"));
    }
  });
  $("btn-history").onclick = () => {
    if ($("history-view").classList.contains("hidden")) showHistoryView();
    else currentResult ? showResultView() : showEmptyView();
  };
  $("hist-back").onclick = () => { currentResult ? showResultView() : showEmptyView(); };
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
    $("pack").dispatchEvent(new Event("change"));   // 触发参数重渲染 + 自绘下拉同步
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
    generate({ mode: "step" });
    toast("正在换个角度重选…");
  };
  $("cf-cancel").onclick = async () => {
    $("confirm-overlay").classList.add("hidden");
    try { await api(`/api/jobs/${currentJob.id}/cancel`, { method: "POST", body: {} }); } catch (_) {}
    currentJob = null;
    $("progress-panel").classList.add("hidden");
  };
  $("btn-copy-voice").onclick = () => currentResult &&
    copyText(currentResult.sections.map(s => s.text).join("\n\n"), "口播已复制");
  $("btn-copy-json").onclick = () => currentResult &&
    copyText(JSON.stringify(currentResult, null, 2), "JSON 已复制");
  $("btn-save-md").onclick = () => {
    if (!currentResult) return;
    const blob = new Blob([resultMarkdown(currentResult)], { type: "text/markdown" });
    const a = el("a");
    a.href = URL.createObjectURL(blob);
    a.download = `口播脚本_${currentResult.params.topic.slice(0, 12)}.md`;
    a.click();
  };
  $("btn-rerun").onclick = () => currentResult && generate({
    mode: currentResult.params.mode, voice: currentResult.params.voice, format: currentResult.params.format,
  });
}

boot();
