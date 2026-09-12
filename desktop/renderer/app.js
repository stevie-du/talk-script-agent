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
// 快捷条常显的参数（豆包 / 千问式，不进设置页）
const FRONT_KEYS = ["segment", "audience", "duration", "platform", "style", "persona"];
// 仍留在设置页「更多设置」里的项：结尾引导 + 补充资料（补充资料不是 select，单独渲染）
const MORE_KEYS = ["cta"];
const KEY_FALLBACK_LABEL = { segment: "细分领域", audience: "受众", duration: "时长（秒）",
  style: "风格", platform: "平台", persona: "人设", cta: "结尾引导" };

let META = null;
let currentJob = null;
let currentResult = null;
let pollTimer = null;
let pollMisses = 0;        // 连续拉取失败次数：超过上限就停，决不无限重试
let busyNow = false;
let genParamsSnapshot = null;   // 上次生成时的参数快照（检测"参数已改、结果未更新"）
let activeMsg = null;           // 当前正在生成/回写的消息节点（用于原地刷新状态与结果）
let sentTopic = "";             // 本次发送的主题（重跑 / 换角度重选时复用）

// 空状态示例卡：点一下直接起手，比空输入框更省心
const SAMPLES = [
  { t: "家用电梯怎么挑？", d: "老旧小区加装，预算 20 万", tag: "安全科普" },
  { t: "电梯维保到底保什么", d: "业主最关心的 3 个问题", tag: "维保科普" },
  { t: "加装电梯 5 个坑", d: "邻居沟通到验收全流程", tag: "旧楼加装" },
  { t: "扶梯突然停了怎么办", d: "商场常见场景应急科普", tag: "安全科普" },
];

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
  // 占位事实标红并标记行号，便于点击定位（见 bindPlaceholderJump）
  h = h.replace(/\{\{([^}]+)\}\}/g, (m, k) => `<span class="over jumpable">{{待补：${k}}}</span>`);
  return h;
}

// 占位事实「点击定位」：滚动到首个占位并高亮，方便逐处补全
function jumpToFirstPlaceholder(body) {
  const hit = body.querySelector(".script-card .over");
  if (!hit) { toast("未找到占位事实"); return; }
  hit.scrollIntoView({ behavior: "smooth", block: "center" });
  hit.classList.remove("flash"); void hit.offsetWidth; hit.classList.add("flash");
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
    <details class="think-stream hidden" id="think-stream" open>
      <summary><span class="ts-title">思考过程</span><span class="ts-meta"></span></summary>
      <pre class="ts-body"></pre>
    </details>
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
  renderThinkStream(body, snap);
}

// 流式思考过程：只显示尾部（后端已截取），并自动滚到底。
// 推理型模型「想」的时间远长于「写」的时间，把这部分露出来，
// 用户就不会盯着「正在生成…」干等几十秒。
function renderThinkStream(body, snap) {
  const box = body.querySelector("#think-stream");
  if (!box) return;
  const st = snap.stream;
  if (!st || (!st.reasoning_tail && !st.content_len)) {
    if (box.classList.contains("hidden")) return;
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

// 清掉消息流里的气泡。切换会话 / 新建对话都必须先走这一步：
// 先前 openSession 只 append 不清空，结果上一个会话的气泡（含「正在生成…」）
// 留在上方，切到别的记录时顶部看起来还在显示上一次生成的内容。
function clearStreamMsgs() {
  stream().querySelectorAll(".msg").forEach(m => m.remove());
  activeMsg = null;
}

// 清空消息流（新建对话）
function clearChat() {
  abandonRunningJob();        // 生成中点「新建对话」也要解锁，否则发送键永久变灰
  clearStreamMsgs();
  $("empty").classList.remove("hidden");
  $("stale-banner").classList.add("hidden");
  currentResult = null;
  genParamsSnapshot = null;
  setHead("新对话", "", "");
}

// 内容区头部：标题 + 副标题 + 状态徽标
function setHead(title, sub, state, stateCls) {
  $("rh-title").textContent = title || "新对话";
  $("rh-sub").textContent = sub || "";
  const st = $("rh-state");
  st.textContent = state || "";
  st.className = "rh-state" + (stateCls ? " " + stateCls : "");
}

// 空状态示例卡
function renderSamples() {
  const box = $("empty-samples");
  if (!box || box.dataset.done) return;
  box.dataset.done = "1";
  box.innerHTML = "";
  for (const s of SAMPLES) {
    const b = el("button", "sample-card");
    b.innerHTML = `<span class="sc-tag">${esc(s.tag)}</span>
      <span class="sc-t">${esc(s.t)}</span>
      <span class="sc-d">${esc(s.d)}</span>`;
    b.onclick = () => {
      $("topic").value = s.t;
      refreshGate();
      autoGrowTopic();
      $("topic").focus();
    };
    box.appendChild(b);
  }
}

function autoGrowTopic() {
  const t = $("topic");
  if (!t) return;
  t.style.height = "auto";
  t.style.height = Math.min(t.scrollHeight, 140) + "px";
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
  sel.onchange = () => { renderPackParams(); updateStale(); updateCfgHint(); };
  renderPackParams();
  updateCfgHint();
  renderSamples();
  bindSessionList();
  loadSessions();
  bindStatic();
  refreshGate();
  // 预填设置抽屉的模型接口字段（生成参数区在 renderPackParams 已渲染）
  preloadSettings().catch(() => {});
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

// 高频参数：直接放在输入框上方（豆包 / 千问式），不进设置页
// 沿用 p-<key> 作为 id，collectParams / getParam 无需改动
function renderQuickParams() {
  const pack = currentPack();
  const box = $("quick-params");
  if (!pack || !box) return;
  // 菜单挂在 body 下，重建前先清掉旧的，避免残留浮层
  box.querySelectorAll(".select-wrap").forEach(w => w._menu?.remove());
  box.innerHTML = "";
  const params = pack.params || {};
  for (const key of FRONT_KEYS) {
    const def = params[key];
    if (!def?.options?.length) continue;
    const s = el("select");
    s.id = `p-${key}`;
    s.dataset.pill = "1";        // 标记为胶囊形态，beautifySelects 据此套 .pill 变体
    for (const opt of def.options) {
      const o = el("option", "", key === "duration" ? `${opt}s` : String(opt));
      o.value = String(opt);
      s.appendChild(o);
    }
    s.value = String(def.default);
    s.title = def.label || key;
    s.onchange = updateCfgHint;
    box.appendChild(s);
  }
  const more = el("button", "ghost qp-more", "更多设置");
  more.title = "打开设置（结尾引导 / 输出内容 / 补充资料）";
  // 显式传入分区：直接把事件对象当 pane 传会走兜底逻辑，语义不清
  more.onclick = () => openSettings("gen");
  box.appendChild(more);
  beautifySelects(box);          // 快捷条同样走自绘下拉，避免原生菜单的系统蓝高亮
}

function renderPackParams() {
  const pack = currentPack();
  if (!pack) return;
  $("pack-badge").classList.toggle("hidden", !pack.draft);
  const front = $("param-front");
  front.innerHTML = "";
  const params = pack.params || {};
  const placed = new Set();
  for (const key of MORE_KEYS) {
    if (!params[key]?.options?.length) continue;
    front.appendChild(paramSelect(key, params[key]));
    placed.add(key);
  }
  // 其余参数（自定义包可能新增）也放这里，FRONT_KEYS 已由快捷条渲染，不重复
  for (const key of Object.keys(params)) {
    if (placed.has(key) || FRONT_KEYS.includes(key)) continue;
    if (!params[key]?.options?.length) continue;
    front.appendChild(paramSelect(key, params[key]));
  }
  renderQuickParams();
  beautifySelects();
}

function getParam(key) {
  const s = $("p-" + key);
  return s ? (s.value || null) : null;
}

// 生成设置条常显所选值（行业包 · 细分 · 时长 · 风格 · 模式）
function updateCfgHint() {
  const pack = currentPack();
  const parts = [];
  if (pack) parts.push(pack.display_name || pack.name);
  if (getParam("segment")) parts.push(getParam("segment"));
  if (getParam("duration")) parts.push(getParam("duration") + "s");
  if (getParam("style")) parts.push(getParam("style"));
  const mode = document.querySelector("input[name=mode]:checked");
  if (mode) parts.push(mode.value === "step" ? "分步确认" : "一键直通");
  // 摘要挂在主侧栏「设置」入口的 hover 提示上（左栏保持干净，不铺开设置项）
  const nav = $("btn-open-settings");
  if (nav) nav.title = `打开设置：${parts.join(" · ") || "生成偏好"}`;
}

// ── 自绘下拉：隐藏原生 select（仅作值容器），按钮 + 菜单替代其外观 ──
// 快捷条与设置页统一走这套：原生菜单的系统蓝高亮 + 黑描边跟整体设计语言冲突太大
function beautifySelects(scope = document) {
  scope.querySelectorAll("select:not([data-beauty])").forEach(sel => {
    sel.dataset.beauty = "1";
    sel.classList.add("native-hidden");
    const wrap = el("div", "select-wrap" + (sel.dataset.pill ? " pill" : ""));
    sel.parentNode.insertBefore(wrap, sel);
    wrap.appendChild(sel);
    const btn = el("button", "select-btn");
    btn.type = "button";
    btn.innerHTML = `<span class="sel-text"></span>
      <svg class="chev" viewBox="0 0 12 8" width="11" height="8" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M1 1.5L6 6.5L11 1.5"/></svg>`;
    const menu = el("div", "select-menu hidden" + (sel.dataset.pill ? " compact" : ""));
    // 菜单挂到 body 上：祖先若有 backdrop-filter/filter/transform，
    // 会成为 fixed 定位的包含块，导致菜单被裁在容器内（#composer 正是如此）
    document.body.appendChild(menu);
    wrap.appendChild(btn);

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
    // 左栏滚动容器会裁剪绝对定位菜单：改用 fixed 定位于视口
    const open = () => {
      build();
      menu.classList.remove("hidden");
      wrap.classList.add("open");
      const r = wrap.getBoundingClientRect();
      // 先置于视口外测量自身尺寸：胶囊形态需按内容取宽（可宽于按钮）
      // 注意必须清空 left/right，否则 .select-menu 的 right:0 会把宽度拉满
      Object.assign(menu.style, { position: "fixed", left: "-9999px", right: "auto", top: "0px", width: "auto", maxHeight: "" });
      const mw = Math.max(menu.offsetWidth || 0, r.width);
      const natural = menu.offsetHeight || 0;
      // 上下可用空间（各留 8px 安全边）
      const roomBelow = innerHeight - r.bottom - 6 - 8;
      const roomAbove = r.top - 6 - 8;
      // 选择空间更充裕的一侧，并把菜单高度压进该空间内（超出则内部滚动）
      const flipUp = roomAbove > roomBelow;
      const room = Math.max(96, flipUp ? roomAbove : roomBelow);
      const mh = Math.min(natural, room);
      Object.assign(menu.style, { maxHeight: mh + "px" });
      // 左沿贴齐按钮，超出视口右侧时再向内收
      const left = Math.max(8, Math.min(r.left, innerWidth - mw - 8));
      const top = flipUp ? Math.max(8, r.top - 6 - mh) : r.bottom + 6;
      Object.assign(menu.style, { width: mw + "px", left: left + "px", top: top + "px" });
    };
    const close = () => {
      menu.classList.add("hidden");
      wrap.classList.remove("open");
      Object.assign(menu.style, { position: "", left: "", right: "", top: "", width: "", maxHeight: "" });
    };
    sel._syncDropdown = sync;
    wrap._menu = menu;              // 供外部点击判定与重渲染清理使用

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
  // 清除脱离 DOM 的孤儿菜单：wrap 被重建（如切换分类 / 重渲染设置抽屉）后，
  // 挂在 body 上的旧菜单会残留，这里统一回收，避免浮层越积越多
  {
    const live = new Set();
    document.querySelectorAll(".select-wrap").forEach(w => { if (w._menu) live.add(w._menu); });
    document.body.querySelectorAll(":scope > .select-menu").forEach(m => { if (!live.has(m)) m.remove(); });
  }
  if (!window.__selOutsideBound) {
    window.__selOutsideBound = true;
    document.addEventListener("click", e => {
      document.querySelectorAll(".select-wrap.open").forEach(w => {
        // 菜单挂在 body 下，判定时要把菜单自身也算作"内部"
        const menu = w._menu;
        const inside = w.contains(e.target) || (menu && menu.contains(e.target));
        if (!inside) {
          w.classList.remove("open");
          if (menu) menu.classList.add("hidden");
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
  // 生成中不再受理新提交，且必须在 await 之前上锁。
  // 若放到 await 之后，一次 HTTP 往返的窗口期内连点会并发出多个 job：
  // activeMsg 被后建的助手气泡覆盖，先建的那些就永远停在 loading，无法回归。
  if (busyNow) return;
  const base = collectParams();
  const params = { ...base, ...overrides };
  if (!params.topic) { toast("请输入主题"); return; }
  sentTopic = params.topic;
  setBusy(true, true);
  try {
    const { job_id } = await api("/api/generate", { method: "POST", body: params });
    genParamsSnapshot = JSON.stringify(collectParams());
    updateStale();
    // params 随 job 带走：连接中断/失败后的「重试」要复用当时真正发出的参数，
    // 不能拿被用户改过的当前界面值充数。
    currentJob = { id: job_id, state: "queued", params };
    pollMisses = 0;
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
    setHead(params.topic, "生成中", "生成中…", "warn");
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
  // 作业令牌：请求发出到回来这段时间里，用户可能点了「新对话」、切了历史记录、
  // 或者干脆发起了新一次生成 —— currentJob 已经换成别人（甚至被清成 null）。
  // 没有令牌的话，旧作业的响应照样往下走：把别的作业的进度画到当前会话上，
  // 更糟的是它会分支到 else 重新 setTimeout(poll)，凭空复活一条本该停掉的轮询链。
  const jobId = currentJob.id;
  api(`/api/jobs/${jobId}`).then(snap => {
    if (!currentJob || currentJob.id !== jobId) return;   // 过期响应，直接丢弃
    pollMisses = 0;
    currentJob.state = snap.state;
    renderProgress(snap);
    if (snap.state === "paused_awaiting_confirmation") {
      setBusy(false);
      setHead(sentTopic, "待确认选题", "待确认", "warn");
      openConfirm(snap.result.plan);
    } else if (snap.state === "done") {
      setBusy(false);
      currentResult = snap.result;
      $("stale-banner").classList.add("hidden");
      const body = activeMsg || addAssistantMsg();
      const think = body.querySelector("#think-stream");   // 先把思考过程摘出来再清空
      body.innerHTML = "";
      activeMsg = body;
      renderResult(snap.result, body);
      if (think && !think.classList.contains("hidden")) {
        think.open = false;                 // 完成后默认折叠，想看再点开
        think.querySelector(".ts-title").textContent = "生成过程";
        body.appendChild(think);
      }
      loadSessions();
      scrollBottom();
    } else if (snap.state === "failed") {
      setBusy(false);
      const failParams = snap.params || {};   // 失败作业的参数，「重试」直接复用
      currentJob = null;
      const err = snap.error || "未知错误";
      setHead(sentTopic, "生成失败", "失败", "bad");
      const body = activeMsg || addAssistantMsg();
      body.innerHTML = `
        <div class="banner warn">⛔ 生成失败：${esc(err)}</div>
        <div class="fail-actions">
          <button class="ghost" id="retry-gen" title="用本次相同的参数再生成一次">重试</button>
        </div>`;
      const rb = body.querySelector("#retry-gen");
      // 失败多半是连接失败/限流这类瞬时问题，重试应当原样再来一次，
      // 所以用失败作业里的参数，而不是可能被改动的当前界面参数
      if (rb) rb.onclick = () => send({ ...failParams });
      activeMsg = null;
      toast("生成失败：" + err, 5000);
      scrollBottom();
    } else if (snap.state === "cancelled") {
      setBusy(false);
      currentJob = null;
      const body = activeMsg;
      if (body) body.innerHTML = `<div class="hint">本次生成已取消</div>`;
      activeMsg = null;
      setHead("新对话", "本次生成已取消", "");
      toast("本次生成已取消");
    } else {
      pollTimer = setTimeout(poll, 900);
    }
  }).catch(e => {
    if (!currentJob || currentJob.id !== jobId) return;   // 同上：旧作业的错误也别管
    pollMisses += 1;
    if (pollMisses <= 3) {
      pollTimer = setTimeout(poll, Math.min(1200 * pollMisses, 3000));   // 退避重试，封顶 3s
      return;
    }
    // 服务连续不可达就别再骗用户「正在生成」了：停在 busy 态既看不到进度
    // 也发不出下一句，只能重启。这里明确落到失败态，界面立刻可用。
    // 连接中断（不是作业失败）：没有 snap 可拿 params，用发起这次生成时的参数重试，
    // 否则用户改了界面参数后一按「重试」生成的就不是刚才那一版了。
    const failParams = currentJob.params || collectParams();
    currentJob = null;
    pollMisses = 0;
    setBusy(false);
    setHead(sentTopic, "连接中断", "失败", "bad");
    const body = activeMsg || addAssistantMsg();
    body.innerHTML = `
      <div class="banner warn">⚠️ 与生成服务的连接中断：${esc(e.message || "未知原因")}</div>
      <div class="fail-actions">
        <button class="ghost" id="retry-gen" title="用本次相同的参数重新生成">重试</button>
      </div>`;
    const rb = body.querySelector("#retry-gen");
    if (rb) rb.onclick = () => send(failParams);
    activeMsg = null;
    toast("连接中断：" + (e.message || "未知原因"), 5000);
    scrollBottom();
  });
}

// 「停止」：后端已支持协作式取消（Job.cancel_event），写了技能的手见证了 ——
// 流式收包过程中检测到取消标志就中断请求，不再是无言地烧完剩下的 token。
// 前端仍要立刻解锁界面，不能等网络往返。
async function abortGeneration() {
  const job = currentJob;
  if (!job) return;
  clearTimeout(pollTimer);
  // 后端不同意（作业其实已结束、或记录不存在）时要说出来。
  // 以前这里是 try/catch(_){} 全吞：用户看到「已放弃本次生成」以为停了，
  // 实际后台还在跑并把结果写回来，于是出现「停止后又多出一条记录」的怪事。
  let err = null;
  try {
    await api(`/api/jobs/${job.id}/cancel`, { method: "POST", body: {} });
  } catch (e) {
    err = e.message || "未知原因";
  }
  currentJob = null;
  pollMisses = 0;
  const body = activeMsg;
  if (body) body.innerHTML = `<div class="hint">${err ? "停止失败，后台可能仍在运行" : "已停止本次生成"}</div>`;
  activeMsg = null;
  setBusy(false);
  setHead("新对话", err ? "停止失败" : "已停止本次生成", "");
  toast(err ? `停止失败：${err}` : "已停止本次生成", err ? 4500 : 2200);
  loadSessions();
}

function renderProgress(snap) {
  const body = activeMsg;
  if (!body) return;
  setThinking(snap);
}

// 离开当前生成上下文（新建对话 / 回看历史 / 取消分步确认）时统一走这里：
// 停轮询 + 复位 busy，但**不取消后端的作业** —— 它照常往下跑，左栏「生成中」
// 分组里始终留着入口，点回去即可接着看（见 attachJob）。
// 要真正终止请点发送键上的「停止」（abortGeneration）。
// 关键：busyNow 必须和 currentJob 一起复位，否则会留下
// 「busy=true 且 currentJob=null」的死锁态 —— 发送键 disabled、输入框永久锁定，
// 且轮询已停无法自愈，用户只能重启应用。三个调用点都踩过这个坑。
function abandonRunningJob() {
  clearTimeout(pollTimer);
  currentJob = null;
  setBusy(false);
  loadSessions();        // 让这次生成立刻出现在左栏「生成中」
}

// 生成中：发送键变「停止」，同时锁住输入框与快捷参数
// 参数在发送瞬间已快照进 job，生成中改动不会生效——锁住比让用户白改更诚实
function setBusy(b, loading = false) {
  busyNow = b;
  const btn = $("btn-generate");
  btn.classList.toggle("loading", loading);
  btn.classList.toggle("stopping", b && loading);
  $("composer-gen-hint").textContent = (b && loading) ? "生成中，参数已锁定" : "";
  lockParams(b && loading);
  refreshGate();
}

function lockParams(lock) {
  $("topic").disabled = lock;
  const bar = $("quick-params");
  bar.classList.toggle("locked", lock);
  bar.querySelectorAll("select").forEach(s => { s.disabled = lock; });
  // 自绘下拉的可见按钮也要跟着禁用（原生 select 已被隐藏）
  bar.querySelectorAll(".select-btn").forEach(b => { b.disabled = lock; });
}

// ── 防错与快捷键 ─────────────────────────────────────────
function refreshGate() {
  const btn = $("btn-generate");
  const empty = !$("topic").value.trim();
  const canStop = busyNow && currentJob;
  btn.disabled = busyNow ? !canStop : empty;
  btn.title = canStop ? "放弃本次生成"
    : busyNow ? "生成中…"
    : empty ? "输入主题后发送" : "回车 / Ctrl + Enter 发送";
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

  // 内容区头部同步：标题 / 副标题 / 状态徽标（跟消息流解耦，抽屉打开也看得见）
  const dev0 = ch.deviation_pct ?? 0;
  const hard0 = (ch.hard_hits || []).reduce((a, h) => a + h.count, 0);
  const passed = ch.passed ?? true;
  setHead(
    r.params?.topic || "生成结果",
    `${r.pack || ""} · ${r.params?.duration ?? "-"}s · ${r.params?.platform || ""}`,
    passed && hard0 === 0 ? "✓ 合格" : `✗ ${hard0 ? hard0 + " 处硬伤" : "需人工确认"}`,
    passed && hard0 === 0 ? "ok" : "bad"
  );

  // 1) 消息头：主题 + 指标速览 + 操作
  const head = el("div", "res-head");
  const dev = ch.deviation_pct ?? 0;
  const hardN = (ch.hard_hits || []).reduce((a, h) => a + h.count, 0);
  head.innerHTML = `
    <div class="res-top">
      <div class="res-title">${esc(r.params?.topic || "")}</div>
      <div class="res-tools">
        <button class="ghost" data-act="copy-voice" title="复制口播文案">复制口播</button>
        <button class="ghost" data-act="save-srt" title="导出 SRT 字幕（可直接导入剪映 / PR）">导出 SRT</button>
        <button class="ghost" data-act="save-md" title="另存为 Markdown 文件">另存 MD</button>
        <button class="ghost" data-act="reveal" title="在文件管理器中打开产物目录">打开文件夹</button>
        <button class="ghost" data-act="copy-json" title="复制结构化 JSON">JSON</button>
        <button class="ghost" data-act="rerun" title="用当前参数重新生成（结果基本一致）">重跑</button>
        <button class="ghost" data-act="revary" title="同主题同参数重掷一次，换一种表达">换一版</button>
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
  // 回炉说明前置：把"为什么又写了一遍"从日志里提到结果头部
  const revs = (r.revisions || []).filter(v => v.action === "全文回炉");
  if (revs.length) {
    const why = [...new Set(revs.map(v => (v.report?.blockers || []).join("、")).filter(Boolean))].join("；") || "未通过校验";
    banners.push(ch.passed
      ? `<div class="banner info">首轮${esc(why)}，已自动回炉 ${revs.length} 轮并修正为合格版本</div>`
      : `<div class="banner warn">首轮${esc(why)}，自动回炉 ${revs.length} 轮后仍未达标</div>`);
  }
  if (!(ch.passed ?? true)) banners.push(`<div class="banner warn">⛔ ${esc((ch.blockers || []).join("；"))}——已达回炉上限，请人工调整或点「重跑」</div>`);
  if ((r.placeholders || []).length)
    banners.push(`<div class="banner warn jumpable" data-jump="placeholder">⚠ 含 ${r.placeholders.length} 处占位事实：${r.placeholders.map(esc).join("、")}——点击定位首处，补充后再发布</div>`);
  if ((ch.soft_hits || []).length)
    banners.push(`<div class="banner info">待确认 ${ch.soft_hits.length} 词：${ch.soft_hits.map(h => esc(h.word) + "×" + h.count).join("、")}（语境正常即可放行）</div>`);
  if (r.pack_draft) banners.push(`<div class="banner warn">⚠ 本结果来自草稿行业包，内容需人工校对</div>`);
  const bw = el("div", "res-banners");
  bw.innerHTML = banners.join("");
  const jp = bw.querySelector('[data-jump="placeholder"]');
  if (jp) { jp.onclick = () => jumpToFirstPlaceholder(body); }
  body.appendChild(bw);

  // 3) 口播分段卡片
  const segs = el("div", "script-list");
  let pi = 0;
  const bodyQuota = Math.floor((r.quota?.body || 0) / nPoints(r));
  r.sections.forEach((s, i) => {
    const label = s.type === "point" ? `要点${++pi}` : TYPE_LABEL[s.type];
    const tm = (r.timings || [])[i];
    const chars = countCN(s.text);
    const sec = (v) => (v === undefined || v === null ? "" : `${Math.round(v * 10) / 10}s`);
    const card = el("div", `script-card ${s.type}`);
    card.style.setProperty("--i", i);
    const quota = s.type === "point"
      ? `<span class="quota ${chars > bodyQuota ? "over" : ""}">${chars}/${bodyQuota} 字</span>`
      : `<span class="quota">${chars} 字</span>`;
    card.innerHTML = `
      <div class="card-head">
        <span class="pill ${s.type}">${esc(label)}</span>
        ${quota}
        <span class="meta">${tm ? sec(tm["start"]) + "–" + sec(tm["end"]) : ""}${tm ? " · " : ""}字幕：${esc(s.subtitle || "—")}</span>
      </div>
      <div class="card-text">${fmtText(s.text)}</div>
      <div class="card-foot">
        <button class="ghost rw">✎ 重写</button>
        <input placeholder="给重写的反馈（可选），回车提交">
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

  // 4) 折叠：分镜 / 合规 / JSON / 日志（图标 + 标题 + 状态徽标）
  const ICONS = {
    sb: `<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="14" rx="2.2"/><path d="M3 9h18M8 18v2.5M16 18v2.5"/></svg>`,
    shield: `<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3l7 3v5.5c0 4.3-2.9 7.7-7 9.5-4.1-1.8-7-5.2-7-9.5V6z"/><path d="M9 12l2 2 4-4"/></svg>`,
    code: `<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M9 7l-5 5 5 5M15 7l5 5-5 5"/></svg>`,
    log: `<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M5 4h9l5 5v11a1 1 0 01-1 1H5a1 1 0 01-1-1V5a1 1 0 011-1z"/><path d="M14 4v5h5M8 13h8M8 17h5"/></svg>`,
  };
  const acc = (title, contentHtml, open, badgeHtml = "", icon = "", cls = "") => {
    const d = el("details", "acc" + (open ? " open" : "") + (cls ? " " + cls : ""));
    d.innerHTML = `<summary>${icon ? `<span class="acc-ic">${icon}</span>` : ""}
      <span class="acc-t">${esc(title)}</span>${badgeHtml}</summary>`;
    const inner = el("div", "acc-body");
    inner.innerHTML = contentHtml;
    d.appendChild(inner);
    return d;
  };
  const sbN = (r.storyboard || []).length;
  const hardN2 = (ch.hard_hits || []).reduce((a, h) => a + h.count, 0);
  const logsN = (r.logs || []).length;
  const wrap = el("div", "acc-list");
  const badge = (text, cls) => `<span class="acc-badge ${cls}">${text}</span>`;
  const complyOk = (ch.passed ?? true) && hardN2 === 0;
  if (hasSb) wrap.appendChild(acc("分镜", renderStoryboard(r), true, badge(`${sbN} 镜`, "info"), ICONS.sb));
  wrap.appendChild(acc("合规检查", renderCompliance(r), !complyOk,
    badge(complyOk ? "✓ 通过" : `✗ ${hardN2 ? hardN2 + " 处硬伤" : "未通过"}`, complyOk ? "ok" : "bad"),
    ICONS.shield, complyOk ? "ok" : "bad"));
  wrap.appendChild(acc("JSON", `<pre class="code">${esc(JSON.stringify(r, null, 2))}</pre>`, false, "", ICONS.code));
  wrap.appendChild(acc("日志", renderLogs(r.logs || []), false, badge(`${logsN} 条`, "info"), ICONS.log));
  body.appendChild(wrap);

  // 5) 操作绑定（消息级）
  head.querySelector('[data-act="copy-voice"]').onclick = () =>
    copyText(r.sections.map(s => s.text).join("\n\n"), "口播已复制");
  head.querySelector('[data-act="save-srt"]').onclick = () => {
    const a = el("a");
    a.href = URL.createObjectURL(new Blob([resultSrt(r)], { type: "text/plain;charset=utf-8" }));
    a.download = `字幕_${(r.params?.topic || "").slice(0, 12)}.srt`;
    a.click();
    toast("SRT 字幕已导出");
  };
  head.querySelector('[data-act="reveal"]').onclick = async () => {
    try {
      const out = await api(`/api/history/${r.id}/reveal`, { method: "POST", body: {} });
      toast("已打开产物目录");
      console.log("[talkscript] output:", out.path);
    } catch (e) { toast("打开失败：" + e.message, 3500); }
  };
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
  // 换一版：同主题同参数重掷一次，用于「内容没毛病但想再看看别的表达」
  const rv = head.querySelector('[data-act="revary"]');
  if (rv) rv.onclick = () => {
    $("topic").value = r.params?.topic || "";
    send({ topic: r.params?.topic || "", mode: r.params?.mode, voice: r.params?.voice,
           format: r.params?.format, reroll: true });
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
// 按 今天 / 昨天 / 更早 分组，跟 WorkBuddy、Trae Work 的会话列表习惯一致
function dayKey(d) {
  const x = new Date(d);
  if (isNaN(x)) return "更早";
  const now = new Date();
  const day = new Date(x.getFullYear(), x.getMonth(), x.getDate());
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const diff = Math.round((today - day) / 86400000);
  if (diff <= 0) return "今天";
  if (diff === 1) return "昨天";
  if (diff < 7) return "本周";
  return "更早";
}

function sessTime(d) {
  const x = new Date(d);
  if (isNaN(x)) return "";
  const p = n => String(n).padStart(2, "0");
  const hm = `${p(x.getHours())}:${p(x.getMinutes())}`;
  const k = dayKey(d);
  if (k === "今天") return hm;
  if (k === "昨天") return "昨天 " + hm;
  return `${p(x.getMonth() + 1)}-${p(x.getDate())} ${hm}`;
}

let activeRefresh = null;      // 有会话在跑时的列表自动刷新
const SESS_ORDER = ["今天", "昨天", "本周", "更早"];
const sessIndex = new Map();   // id -> 这条记录最近一次的数据（委托 handler 从这里取数，闭包不会过期）
const DEL_SVG = `<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M4 7h16M9.5 7V5.2A1.2 1.2 0 0 1 10.7 4h2.6a1.2 1.2 0 0 1 1.2 1.2V7M6.5 7l.8 12.1a1.2 1.2 0 0 0 1.2 1.1h7a1.2 1.2 0 0 0 1.2-1.1L17.5 7"/><path d="M10.5 11v5.5M13.5 11v5.5"/></svg>`;

async function loadSessions() {
  let items = [];
  try { items = await api("/api/history"); } catch (_) {}
  paintSessions(items);
  // 还有会话没结束就定期刷新；结束后会自动变成正常记录。
  // failed / cancelled 不算「没结束」，否则失败后这条会一直触发空转刷新。
  clearTimeout(activeRefresh);
  if (items.some(it => {
    const s = it.state || "done";
    return s !== "done" && s !== "failed" && s !== "cancelled";
  })) {
    activeRefresh = setTimeout(() => loadSessions(), 3000);
  }
}

// ── 列表绘制：按 key 增量更新，绝不重建已经存在的行 ──────────
// 以前每次刷新都把 #session-list 整个 innerHTML 清空重画：鼠标按下（mousedown）
// 与松开（mouseup）之间只要发生一次重画，那一行连同里面的删除按钮就被换成了
// 新的 DOM 对象，click 事件合成不出来 —— 表现就是「删除按钮要点好几次」。
// 现在同一条记录永远复用同一个节点，内容没变时连一个字节都不写。
function paintSessions(items) {
  const list = $("session-list");
  // 清掉没有 key 的残留节点（比如空态提示）
  Array.from(list.children).forEach(n => { if (!n.dataset.key) n.remove(); });
  if (!items.length) {
    if (list.children.length) list.innerHTML = "";
    list.appendChild(el("p", "hint sess-empty", "还没有会话记录，先发一条试试"));
    return;
  }
  const specs = [];
  const groups = new Map();
  for (const it of items) {
    const k = dayKey(it.created_at);
    if (!groups.has(k)) groups.set(k, []);
    groups.get(k).push(it);
  }
  for (const k of SESS_ORDER) {
    const arr = groups.get(k);
    if (!arr || !arr.length) continue;
    specs.push({ kind: "grp", key: "g:" + k, label: k, count: arr.length });
    for (const it of arr) specs.push({ kind: "row", key: "s:" + it.id, it });
  }
  sessIndex.clear();
  for (const sp of specs) if (sp.kind === "row") sessIndex.set(sp.it.id, sp.it);

  const old = new Map();
  Array.from(list.children).forEach(n => { if (n.dataset.key) old.set(n.dataset.key, n); });
  let cursor = list.firstChild;
  for (const sp of specs) {
    let node = old.get(sp.key);
    if (!node) {
      node = sp.kind === "grp" ? buildGroupLabel() : buildSessionRow();
      node.dataset.key = sp.key;
    } else old.delete(sp.key);
    if (sp.kind === "grp") updateGroupLabel(node, sp);
    else updateSessionRow(node, sp.it);
    if (node === cursor) cursor = cursor.nextSibling;
    else list.insertBefore(node, cursor);   // 只在顺序不对时挪位置，正常刷新走不到这里
  }
  old.forEach(n => n.remove());
}

function buildGroupLabel() { return el("div", "group-lbl"); }

function updateGroupLabel(n, sp) {
  const sig = sp.label + "|" + sp.count;
  if (n._sig === sig) return;
  n._sig = sig;
  n.innerHTML = `${esc(sp.label)}<span class="count">${sp.count}</span>`;
}

// 行的结构只造一次；删除按钮始终在场（可见度交给 CSS），不存在「这一行没有按钮」
function buildSessionRow() {
  const row = el("div", "sess-item");
  row.innerHTML = `<div class="sess-top"><span class="dot"></span><span class="sess-topic"></span></div>
    <div class="sess-sub"></div>`;
  row.appendChild(el("button", "sess-del", DEL_SVG));
  return row;
}

function updateSessionRow(row, it) {
  const st = it.state || "done";
  const settled = st === "done" || st === "failed" || st === "cancelled";
  const isCur = settled
    ? (!!currentResult && it.id === currentResult.id)
    : (!!currentJob && it.id === currentJob.id);
  row.dataset.id = it.id;
  row.classList.toggle("active", isCur);
  const sub = st === "done"
    ? `${it.pack || ""} · ${sessTime(it.created_at)} · ${it.duration ?? "-"}s`
    : `${it.pack || ""} · ${STATE_LABEL[st] || st} · ${sessTime(it.created_at)}`;
  const sig = [isCur, st, it.topic, sub].join("\u0001");
  if (row._sig === sig) return;          // 内容没变就彻底不动 DOM
  row._sig = sig;
  row.querySelector(".dot").className = "dot " + (st === "done" && !it.passed ? "no" : "");
  row.querySelector(".sess-topic").textContent = it.topic || "";
  row.querySelector(".sess-sub").textContent = sub;
  row.title = `${it.topic || ""}\n${it.pack || ""} · ${sessTime(it.created_at)}${settled ? "" : " · 生成中"}`;
  const del = row.querySelector(".sess-del");
  del.title = settled ? "删除这条记录" : "放弃这次生成并移除记录";
  del.dataset.act = settled ? "del" : "cancel";
}

// 整个列表只在容器上挂一个监听器：以前逐行绑 onclick，行被刷新换掉后绑定跟着错位，
// 委托则与重绘无关，任何一行任何时候点击都走同一条路径。
function bindSessionList() {
  const list = $("session-list");
  if (list._bound) return;
  list._bound = true;
  list.addEventListener("click", ev => {
    const row = ev.target.closest(".sess-item");
    if (!row) return;
    const it = sessIndex.get(row.dataset.id);
    if (!it) return;
    if (ev.target.closest(".sess-del")) {
      ev.stopPropagation();
      ev.preventDefault();
      deleteSession(it);
      return;
    }
    // 有结果的按结果渲染；还在跑（或已失败）的点回去接着看它的状态
    const st = it.state || "done";
    st === "done" ? openSession(it.id) : attachJob(it.id);
  });
}

// 删除：两种语义
// - 已结束（done / failed）：产物已落盘，DELETE /api/history/{id}
// - 进行中：产物还没落盘，DELETE 会 404，改为取消作业；取消后列表不再列出它
async function deleteSession(it) {
  const st = it.state || "done";
  const settled = st === "done" || st === "failed" || st === "cancelled";
  const name = String(it.topic || "").slice(0, 20);
  const ok = settled
    ? await appConfirm("删除记录", `「${name}」删除后不可恢复。`)
    : await appConfirm("放弃这次生成", `「${name}」正在生成，将停止并移除该记录。`);
  if (!ok) return;
  try {
    if (settled) await api(`/api/history/${it.id}`, { method: "DELETE" });
    else await api(`/api/jobs/${it.id}/cancel`, { method: "POST", body: {} });
    // 删掉的正是当前挂着的作业时必须顺手解锁，否则会留下锁死态
    if (currentJob && currentJob.id === it.id) abandonRunningJob();
    if (currentResult && currentResult.id === it.id) currentResult = null;
    toast(settled ? "已删除" : "已放弃");
    loadSessions();
  } catch (e) { toast("删除失败：" + e.message, 3500); }
}

// 回到一个正在跑的会话：重建消息流 + 把轮询接回去。
// 后端作业一直在跑（切走只脱离、不取消），所以这里只是重新把界面挂上去。
async function attachJob(id) {
  let snap;
  try { snap = await api(`/api/jobs/${id}`); }
  catch (e) { toast("无法回到这次生成：" + e.message, 3500); loadSessions(); return; }
  closeSettings();
  clearTimeout(pollTimer);
  currentResult = null;
  pollMisses = 0;
  currentJob = { id, state: snap.state, params: snap.params || null };
  genParamsSnapshot = null;
  clearStreamMsgs();
  $("empty").classList.add("hidden");
  $("stale-banner").classList.add("hidden");
  const topic = snap.params?.topic || "";
  sentTopic = topic;
  addUserMsg(topic);
  const body = addAssistantMsg();
  body.innerHTML = placeholderBody();
  activeMsg = body;
  renderProgress(snap);
  scrollBottom();
  if (snap.state === "paused_awaiting_confirmation") {
    setBusy(false);
    setHead(topic, "待确认选题", "待确认", "warn");
    openConfirm((snap.result || {}).plan || {});
  } else {
    setBusy(true, true);
    setHead(topic, "生成中", STATE_LABEL[snap.state] || "生成中", "warn");
    poll();
  }
  loadSessions();
}

async function openSession(id) {
  try {
    const r = await api(`/api/history/${id}`);
    closeSettings();
    abandonRunningJob();        // 离开当前生成：停轮询 + 解锁；后端作业继续跑，左栏随时能点回去
    clearStreamMsgs();          // 必须先清空：否则上一个会话（含「正在生成…」）会留在上方
    currentResult = r;          // 历史回看不可再重写/轮询
    $("empty").classList.add("hidden");
    addUserMsg(r.params?.topic || "");
    const body = addAssistantMsg();
    renderResult(r, body);
    loadSessions();          // 刷新左栏高亮
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

// ── 设置：整窗二级页面（#settings-screen，自带左侧分类导航）────────
// 参照 Zcode：设置不是主侧栏里铺开的几项，而是覆盖整个窗口的独立页面，
// 有自己的左导航分组 + 右内容区。主界面保持挂载，关闭即原样回来。
const SETTINGS_PANES = ["gen", "packinfo", "packgen", "llm", "kb", "skills"];
let lastPane = "gen";           // 记住上次所在分区，重开设置回到原位

function setSettingsPane(pane) {
  if (!SETTINGS_PANES.includes(pane)) pane = lastPane;
  SETTINGS_PANES.forEach(p => {
    const el = $("pane-" + p);
    if (el) el.classList.toggle("hidden", p !== pane);
  });
  // 设置页自己的左导航高亮
  document.querySelectorAll(".stg-nav-item").forEach(n => {
    n.classList.toggle("on", n.dataset.pane === pane);
  });
  // 切换分区后内容从头看起，避免停在上一分区的滚动位置
  const cur = $("pane-" + pane);
  if (cur) cur.scrollTop = 0;
  lastPane = pane;
  // 行业包详情每次进入都重拉：包可能被切换过，文件清单与草稿角标也可能已被改动
  if (pane === "packinfo") {
    openPackInfo().catch(e => toast("读取失败：" + e.message, 3500));
  }
  return pane;
}

async function preloadSettings() {
  const c = await api("/api/config");
  $("st-baseurl").value = c.base_url || "";
  $("st-model").value = c.model || "";
  $("st-apikey").value = "";
  $("st-status").textContent = c.mock ? "当前为 mock 模式（未配置 Key）"
    : c.api_key_set ? `已配置 Key（模型 ${c.model}）` : "未配置 API Key";
}

// 打开设置：不传 pane 就回到上次所在分区（主侧栏入口 / Ctrl+,）
function openSettings(pane) {
  const target = SETTINGS_PANES.includes(pane) ? pane : lastPane;
  $("settings-screen").classList.remove("hidden");
  setSettingsPane(target);
  preloadSettings().catch(() => {});
  beautifySelects($("settings-screen"));
  return Promise.resolve();
}

function closeSettings() {
  if (!settingsOpen()) return;
  $("settings-screen").classList.add("hidden");
}

function settingsOpen() {
  return !$("settings-screen").classList.contains("hidden");
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

// SRT 字幕：时间轴取 timings，文本取每段 subtitle（缺则截取口播首句）
function resultSrt(r) {
  const ts = t => {
    const ms = Math.max(0, Math.round((t || 0) * 1000));
    const pad = (n, w) => String(n).padStart(w, "0");
    return `${pad(Math.floor(ms / 3600000), 2)}:${pad(Math.floor(ms / 60000) % 60, 2)}`
      + `:${pad(Math.floor(ms / 1000) % 60, 2)},${pad(ms % 1000, 3)}`;
  };
  const clean = s => s.replace(/\*\*/g, "").replace(/[／]/g, " ")
    .replace(/\{\{[^}]*\}\}/g, "").replace(/\s+/g, " ").trim();
  const lines = [];
  let n = 0;
  (r.sections || []).forEach((s, i) => {
    const tm = (r.timings || [])[i] || { start: 0, end: 0 };
    const text = clean(s.subtitle || "") || clean(s.text || "").slice(0, 16);
    if (!text) return;
    n += 1;
    lines.push(String(n), `${ts(tm.start)} --> ${ts(tm.end)}`, text, "");
  });
  return lines.join("\r\n");
}

async function copyText(text, tip) {
  try { await navigator.clipboard.writeText(text); toast(tip); }
  catch (_) { toast("复制失败", 1500); }
}

// 重新拉取行业包列表（新建包 / 草稿标记变更后调用），保留当前选中项
async function refreshPacks() {
  META = await api("/api/meta");
  const sel = $("pack");
  const keep = sel.value;
  sel.innerHTML = "";
  for (const p of META.packs) {
    const o = el("option", "", esc(p.display_name) + (p.draft ? "（草稿）" : ""));
    o.value = p.name;
    sel.appendChild(o);
  }
  sel.value = keep;
  if (!sel.value) sel.selectedIndex = 0;
  // 同步顶栏草稿徽标（不重建参数区，避免重置用户已选的细分/受众）
  const cur = META.packs.find(p => p.name === sel.value);
  $("pack-badge").classList.toggle("hidden", !(cur && cur.draft));
  updateCfgHint();
}

async function openPackInfo() {
  const name = $("pack").value;
  const p = await api(`/api/packs/${name}`);
  $("pi-title").textContent = `${p.display_name || name} · 包内容`;
  $("pi-desc").textContent = (p.description || "") + (p.draft ? "（草稿包：内容需人工校对后投产）" : "");
  $("pi-undraft").classList.toggle("hidden", !p.draft);
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
}

// ── 视图切换（Zcode 式：左栏分组导航 → 右栏整页内容）──

// 内容区只剩对话视图：行业包 / 模型接口 / 知识库 / 技能都在设置二级页里
const VIEW_META = {
  chat: { title: "新对话" },
};

// 左栏导航项不声明目标视图，也没有需要跨视图维持的高亮，故这里不再管 active
function gotoView(name) {
  document.querySelectorAll("#right > .view").forEach(v => v.classList.add("hidden"));
  const v = $("view-" + name);
  if (v) v.classList.remove("hidden");
  const m = VIEW_META[name];
  // 对话视图的标题由 renderResult / clearChat 动态维护，此处不能覆盖
  if (name !== "chat" && m) setHead(m.title, "", "");
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
  $("btn-new-chat").onclick = () => { closeSettings(); gotoView("chat"); clearChat(); loadSessions(); $("topic").focus(); };

  // 主侧栏底部「设置」入口 + 设置页自己的左侧分类导航
  $("btn-open-settings").onclick = () => openSettings();
  $("btn-close-settings").onclick = closeSettings;
  document.querySelectorAll(".stg-nav-item").forEach(n => {
    n.onclick = () => setSettingsPane(n.dataset.pane);
  });
  // 行业包的「详情 / 新建」不再是主侧栏导航项，改为设置页内的相邻分类。
  // 详情数据的拉取统一由 setSettingsPane 负责，这里只切面板，避免请求打两次。
  $("btn-packinfo").onclick = () => setSettingsPane("packinfo");
  $("pi-close").onclick = () => setSettingsPane("gen");

  // 发送：按钮 / Ctrl+Enter / Enter（非 Shift）
  $("btn-generate").onclick = () => { if (busyNow) abortGeneration(); else send(); };
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

  // 参数变更：检测结果过期 + 刷新侧栏设置摘要
  document.addEventListener("change", e => {
    if (e.target.closest("#settings-screen")) { updateStale(); updateCfgHint(); }
  });

  // Enter 换行自动增高 / 恢复单行
  function autoGrow() { autoGrowTopic(); }

  document.addEventListener("keydown", e => {
    if ((e.ctrlKey || e.metaKey) && e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      if (!busyNow && $("topic").value.trim()) send();
    }
    if (e.ctrlKey && e.key === "\\") {
      e.preventDefault();
      setLeftFolded(!$("left").classList.contains("folded"));
    }
    if ((e.ctrlKey || e.metaKey) && e.key === ",") {
      e.preventDefault();
      settingsOpen() ? closeSettings() : openSettings();
    }
    if (e.key === "Escape") {
      const open = document.querySelector(".overlay:not(.hidden)");
      if (open) { open.classList.add("hidden"); return; }
      if (settingsOpen()) closeSettings();
    }
  });

  $("st-save").onclick = async () => {
    try { await saveSettings(); } catch (e) { toast("保存失败：" + e.message, 3500); }
  };
  $("st-test").onclick = async () => {
    const btn = $("st-test"), label = "测试连接";
    btn.disabled = true; btn.textContent = "测试中…";
    $("st-status").textContent = "正在连接…";
    try {
      // 带上界面当前值（Key 留空时后端沿用已保存的），未保存也能测
      const body = {
        base_url: $("st-baseurl").value.trim(),
        api_key: $("st-apikey").value,
        model: $("st-model").value.trim(),
      };
      const r = await api("/api/config/test", { method: "POST", body });
      $("st-status").textContent = r.ok
        ? `连接正常 · ${r.model}${r.detail ? " · " + r.detail : ""}`
        : `连接失败：${r.detail}`;
      toast(r.ok ? "连接正常" : "连接失败，请看下方提示", r.ok ? 2000 : 4000);
    } catch (e) {
      $("st-status").textContent = e.message;
      toast("测试失败：" + e.message, 4000);
    }
    btn.disabled = false; btn.textContent = label;
  };
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
  $("pi-undraft").onclick = async () => {
    const name = $("pack").value;
    if (!(await appConfirm("标记为已校对", "确认该行业包已人工校对完毕？\n草稿标记会被移除，生成结果不再提示“需校对”。"))) return;
    try {
      await api(`/api/packs/${name}/undraft`, { method: "POST", body: {} });
      toast("已标记为校对完成");
      await refreshPacks();      // 刷新行业包列表（去掉「草稿」后缀与角标）
      await openPackInfo();      // 重渲染详情页，隐藏该按钮
    } catch (e) { toast("操作失败：" + e.message, 3500); }
  };
  $("btn-newpack").onclick = () => {
    $("pg-form").classList.remove("hidden");
    $("pg-result").classList.add("hidden");
    setSettingsPane("packgen");
  };
  $("pg-close").onclick = () => setSettingsPane("gen");
  $("pg-run").onclick = runPackgen;
  $("pg-done").onclick = async () => {
    setSettingsPane("gen");
    await boot();
    $("pack").value = $("pack").options[$("pack").options.length - 1].value;
    $("pack").dispatchEvent(new Event("change"));
    toast("已切换到新建的行业包");
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
  $("cf-cancel").onclick = () => {
    $("confirm-overlay").classList.add("hidden");
    abandonRunningJob();        // 停轮询 + 解锁 + 作废作业（原先只置空 currentJob，会锁死界面）
    if (activeMsg) activeMsg.innerHTML = `<div class="hint">本次生成已取消</div>`;
    activeMsg = null;
  };
}

boot();