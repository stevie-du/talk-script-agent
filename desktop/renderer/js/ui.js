// 界面骨架：参数区、自绘下拉、空状态示例、折叠、快捷键、视图切换。
//
// 这里保留了两处踩过坑之后的正解：
//  · 自绘下拉的菜单挂到 body 上（祖先若有 backdrop-filter/transform 会成为
//    fixed 定位的包含块，菜单会被裁在容器内）；并且按视口可用空间自动上翻。
//  · 全局 change / keydown 监听只注册一次（修复前「新建行业包完成」会二次
//    boot()，导致 Ctrl+\ 连翻两次等于没翻）。

import { $, $$, el, esc, toast, bindOnce } from "./util.js";
import { state, setBusy, on, emit, setGenParam, resetGenParams } from "./store.js";
import { api } from "./api.js";
import { collectParams, updateStale, autoGrowTopic, getParam } from "./jobs.js";
import { stopTicker } from "./progress.js";
import { focusSessionSearch } from "./sessions.js";
import { closeSettings, openSettings, setPane, settingsOpen } from "./settings.js";
import { anyOverlayOpen, closeOverlays } from "./overlays.js";

// 输入区工具条常显的参数：只留「生成前必须确认」的硬约束 ——
// 写什么（细分领域）、给谁（受众）、多长（时长）、发哪（平台）。
// 其余的是「表达调性」（风格 / 人设 / 结尾引导）：包的默认值通常就够用、
// 改动频率低，统一交给设置页的「生成偏好」承接。
// 想让工具条展示别的参数，改这一个数组即可 —— 两侧会自动重新分层。
const TOOLBAR_KEYS = ["segment", "audience", "duration", "platform"];
const KEY_FALLBACK_LABEL = {
  segment: "细分领域", audience: "受众", duration: "时长（秒）",
  style: "风格", platform: "平台", persona: "人设", cta: "结尾引导",
};

const SAMPLES = [
  { t: "家用电梯怎么挑？", d: "老旧小区加装，预算 20 万", tag: "安全科普" },
  { t: "电梯维保到底保什么", d: "业主最关心的 3 个问题", tag: "维保科普" },
  { t: "加装电梯 5 个坑", d: "邻居沟通到验收全流程", tag: "旧楼加装" },
  { t: "扶梯突然停了怎么办", d: "商场常见场景应急科普", tag: "安全科普" },
];

export function currentPack() {
  return state.meta?.packs.find(p => p.name === $("pack").value) || null;
}

/**
 * 把 META.packs 灌进下拉。
 *
 * 默认**保住用户当前的选择**：这个函数会被 meta 事件（保存设置、新建行业包）
 * 反复调用，若每次都跳回 default_pack，「已切换到新建的行业包」就是句空话。
 * @param selectLast 选中最后一个（新建的包总是追加在末尾）
 * @param prefer     指定要选中的包名（优先级高于保住当前值）
 */
export function fillPackSelect({ selectLast = false, prefer = null } = {}) {
  const sel = $("pack");
  if (!sel || !state.meta) return;
  const names = state.meta.packs.map(p => p.name);
  const keep = prefer || sel.value;
  sel.innerHTML = "";
  for (const p of state.meta.packs) {
    // 「（损坏）」不是装饰：坏包的 display_name 会退成目录 slug，
    // 而参数条会空掉 —— 不标出来，用户只会觉得「这个包没配好」。
    const o = el("option", "",
                 esc(p.display_name) + (p.draft ? "（草稿）" : "")
                 + (p.pack_error ? "（损坏）" : ""));
    o.value = p.name;
    sel.appendChild(o);
  }
  if (selectLast) sel.value = names[names.length - 1] || "";
  else if (keep && names.includes(keep)) sel.value = keep;
  else sel.value = state.meta.default_pack || "";
  if (!sel.value && sel.options.length) sel.selectedIndex = 0;
  renderPackParams();
}

// ── 模型选择器（工具条右侧、发送键左边）──────────────────────
// 原先模型只在头部有个只读胶囊，想换模型得进设置页 —— 而「换个模型重试」恰恰
// 是结果不满意时最常见的动作，它应该离输入框最近。
//
// 候选来自「当前配置 + 本地用过的」，而不是硬编码一份模型清单：本项目走的是
// 用户自己的 OpenAI 兼容端点，清单里有没有、能不能调通完全取决于那个端点。
// 硬编码等于给用户一个假承诺 —— 选中一个根本调不通的模型，报错还发生在生成时，
// 那时用户早已忘了自己是从哪选的。
// 候选来自**设置里那份模型列表**（`/api/meta` 的 `models`）。
//
// 修复前这里是「当前配置 + localStorage 里用过的名字」：清单是本地攒的，
// 与设置页那份配置**没有任何关系** —— 在设置里删掉一个模型，输入区的下拉里
// 它还在；换台机器打开，历史全没了。同一件事两份表示，其中一份还是隐形的。
// 现在只有一个来源，选中的那个直接调 `/api/models/activate`。
//
// 也不硬编码一份模型清单：本项目走的是用户自己的 OpenAI 兼容端点，
// 清单里有没有、能不能调通完全取决于那个端点。硬编码等于给用户一个假承诺 ——
// 选中一个根本调不通的模型，报错还发生在生成时，那时用户早已忘了自己是从哪选的。
const ADD_MODEL = "__add__";

/** 没有可用模型时胶囊与菜单顶部那一项的占位语。
 *  用常量而不是两处各写一遍：按钮上的文字取自这个 option，改一处就会和
 *  renderModelPicker 里算出的 cur 分叉。 */
const PICKER_PLACEHOLDER = "选择模型";

export function renderModelPicker() {
  const m = state.meta;
  const box = $("model-pick");
  if (!box) return;
  // 菜单挂在 body 下，重建前先清掉旧的，避免残留浮层
  box.querySelectorAll(".select-wrap").forEach(w => w._menu?.remove());
  box.innerHTML = "";
  if (!m) return;

  const models = m.models || [];
  // 2026-09-17：`|| models[0]` 的兜底**去掉了** —— 后端保证 active_model 要么是
  // 有效的 id、要么是空串（`_parse_models` 处理悬空引用），而空串是**合法状态**
  //（用户把开关全关了，见 settings.js 的 activateModel）。
  // 原来的兜底会让「都没启用」时选择器仍显示第一条的名字 —— 显示与状态不一致：
  // 用户以为还有模型在用，点生成才发现被拒。
  const active = models.find(x => x.id === m.active_model) || null;
  // 没有可用模型时显示**占位语**，不显示「未配置模型」/「未启用模型」：
  // 这一排里其他控件摆的都是**值**（维保 / 业主乘客 / 60s / 抖音），一个五位字的
  // 诊断句挤在值的位置上，读起来像报错而不像控件。三种「现在生成会被拒」的原因
  // 一条都没丢 —— 它们挪到了悬停说明（下面的 _warnNote）和菜单里。
  const cur = m.mock ? "mock 模式"
    : active ? (active.label || active.model)
      : PICKER_PLACEHOLDER;
  // 2026-09-17（任务 5）：「默认模型」整个下线 —— 工具条 picker 上不再附
  // 「（默认）」后缀。区分"未配置 vs 已配置"的职责完全交给 sel._warnNote
  // （未配 Key 时显式告知「生成会被拒绝」+ warn 色）+ 模型接口的"未配置 Key"
  // 小标（见 settings.js 的 modelItem）。
  const sel = el("select");
  sel.id = "p-model";
  sel.dataset.pill = "1";
  for (const x of models) {
    const o = el("option", "", esc(x.label || x.model));
    o.value = x.id;
    sel.appendChild(o);
  }
  // 占位项：没有可用模型时下拉**不能空着** —— 空 select 会被浏览器显示成第一个
  // option，那又变成「看起来有个模型在用」。原因（一条都没有 / 有但没启用）
  // 不写在这里，写在下面的 _warnNote 里。
  if (!m.mock && !active) {
    const o = el("option", "", PICKER_PLACEHOLDER);
    o.value = "";
    sel.insertBefore(o, sel.firstChild);
  }
  // 「添加模型」不是装饰项：没有它，换新模型就无处可去，这个下拉会变成封闭集合。
  // 它现在直接开弹窗，不再只是「跳到设置页让你自己找」。
  const add = el("option", "", "＋ 添加模型…");
  add.value = ADD_MODEL;
  sel.appendChild(add);
  sel.value = active ? active.id : (m.mock ? "" : "");
  sel._mock = !!m.mock;
  // 橙色只留给「真的会失败」的情形 —— 三种"不能生成"各自说清原因，
  // 同一句「未配置 API Key」会把前两种指向错的地方（2026-09-17）：
  //   ① 一条模型都没有；② 有模型但都没启用；③ 启用了但没填 Key。
  sel._warnNote = !models.length
    ? "还没有配置模型 —— 生成会被拒绝，点击去「模型接口」添加"
    : !m.active_model
      ? "没有启用任何模型 —— 生成会被拒绝，点击去「模型接口」打开一个开关"
      : m.has_api_key ? "" : "未配置 API Key —— 生成会被拒绝，点击去「模型接口」填写";
  sel.title = `当前模型：${cur}。切换后对后续生成生效（正在跑的作业不受影响）`;
  sel.onchange = () => pickModel(sel);
  box.appendChild(sel);
  beautifySelects(box);
}

async function pickModel(sel) {
  const val = sel.value;
  const m = state.meta;
  if (val === ADD_MODEL) {
    // 它不是一个真实模型，只是「去加一个」的入口 —— 立刻退回原值，
    // 否则按钮上会一直显示「＋ 添加模型…」，看着像真的选中了。
    sel.value = m?.active_model || "";
    sel._sync?.();
    await openSettings("llm");
    $("st-add-model")?.click();
    return;
  }
  if (!m || !val || val === m.active_model) return;
  try {
    await api.activateModel(val);
    // 工具条与头部状态都要跟着变，否则用户以为没切成功
    state.meta = await api.meta();
    emit("meta", state.meta);
    const picked = (state.meta.models || []).find(x => x.id === val);
    toast(`已切换模型：${picked ? picked.label : val}`);
  } catch (e) {
    toast("切换模型失败：" + e.message, 4000);
    sel.value = m.active_model || "";
    sel._sync?.();
  }
}

export function renderPackParams() {
  const pack = currentPack();
  if (!pack) return;
  $("pack-badge").classList.toggle("hidden", !pack.draft);

  // 包坏了要**说出来**（P1-6 / P2-7）。不说的话，界面呈现的是「这个包参数很少」——
  // 而真相是 pack.yaml 没解析出来（或内容不符合约定），下面的 params 全是降级值，
  // 真去生成还会被引擎拒绝。两件事差得远。
  // 措辞用「不可用」而不是「读不出来」：坏法有两类，一类是 YAML 语法错、
  // 另一类是语法对但结构不对（`version: v2`），后者说「读不出来」就不准确了。
  const perr = $("pack-err");
  if (perr) {
    perr.textContent = pack.pack_error
      ? "⚠ 这个行业包不可用：" + pack.pack_error
        + "。下面的参数与知识切片都是降级值，生成会被拒绝 —— 修好该文件后重试。"
      : "";
    perr.classList.toggle("hidden", !pack.pack_error);
  }
  const front = $("param-front");
  // 只清 innerHTML 就够：下拉菜单虽然挂在 body 下，但 beautifySelects() 末尾有一段
  // 全局的孤儿菜单清扫（「清除脱离 DOM 的孤儿菜单」），会按 .select-wrap 反查并删掉
  // 没人引用的菜单。这里**不要**再补一遍 —— 重复机制只会让人以为少了它就会漏。
  front.innerHTML = "";
  const params = pack.params || {};
  const placed = new Set();
  // 2026-09-17（任务 2）：生成参数卡片要展示**全量**参数 —— 工具条（TOOLBAR_KEYS）
  // 既然存在就是为了快速设置，硬性把同样的字段在设置页再列一份叫「冗余」。
  // 设置页才是「所有可定制项」的总账。删掉那条 `placed.has(key) || TOOLBAR_KEYS.includes(key)`
  // 排除之后，「生成参数」卡片与工具条胶囊共用同一份 pack.params 数据源，
  // 用户在两处任一处改都会同步（两处各自一个 select，靠 `data-key` 认字段、
  // **不共用 id** —— 同 id 会让 getElementById 只认得第一个，另一处改了不生效；
  // 真值统一收在 state.genParams，updateStale 也听得到）。
  for (const key of Object.keys(params)) {
    if (placed.has(key)) continue;
    if (!params[key]?.options?.length) continue;
    front.appendChild(paramSelect(key, params[key]));
    placed.add(key);
  }
  renderQuickParams();
  beautifySelects();
}

/** 同一个参数在界面上的全部控件实例：工具条胶囊（`#p-<key>`）+ 设置页参数卡
   （`[data-key]`）。两处是**一个东西的两个视图**，不是两份设置。 */
function paramSelects(key) {
  return $$(`#quick-params #p-${key}, #param-front select[data-key="${key}"]`);
}

/** 把一个值同时写进该参数的所有视图，并刷新自绘下拉的可见文字。
   程序化改 `select.value` 不会自己重画 `.select-btn`，必须显式 `_syncDropdown()`。 */
function paintParam(key, value) {
  for (const s of paramSelects(key)) {
    if (s.value !== value) s.value = value;
    s._syncDropdown?.();
  }
}

/** 控件 → 真值 → 其它视图。两个视图共用这一条写入路径，谁改都不会只改到自己。 */
function bindParamSync(sel, key) {
  sel.addEventListener("change", () => {
    setGenParam(key, sel.value);
    paintParam(key, sel.value);
  });
}

function paramSelect(key, def) {
  const wrap = el("div");
  wrap.appendChild(el("label", "lbl", esc(def.label || KEY_FALLBACK_LABEL[key] || key)));
  const s = el("select");
  // ⚠ 不用 id（设置页参数组是整组常驻 DOM 的视图，与工具条胶囊同 key 撞 id）。
  // 真值不在 DOM 上，在 state.genParams —— 这里只挂 data-key 供视图互相同步。
  s.dataset.key = key;
  for (const opt of def.options) {
    const o = el("option", "", esc(String(opt)));
    o.value = String(opt);
    s.appendChild(o);
  }
  s.value = String(state.genParams[key] ?? def.default);
  bindParamSync(s, key);
  wrap.appendChild(s);
  return wrap;
}

function renderQuickParams() {
  const pack = currentPack();
  const box = $("quick-params");
  if (!pack || !box) return;
  // 菜单挂在 body 下，重建前先清掉旧的，避免残留浮层
  box.querySelectorAll(".select-wrap").forEach(w => w._menu?.remove());
  box.innerHTML = "";
  const params = pack.params || {};
  // 后端给的「哪些值没有行业定制」清单（见 app/knowledge.py 的 param_audit）：
  // 这些值不报错，只会静默走通用默认 —— 挂到 select 上，由 beautifySelects
  // 变成胶囊变色 + 菜单里的「!」标记，避免用户以为在定制、实际没生效。
  const audit = pack.param_audit || {};
  for (const key of TOOLBAR_KEYS) {
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
    s.value = String(state.genParams[key] ?? def.default);
    s.title = def.label || key;
    s._audit = audit[key] || {};
    bindParamSync(s, key);
    box.appendChild(s);
  }
  // 「更多设置」由文字按钮降级为参数组末尾的图标：它原来靠 margin-left:auto
  // 孤悬在整行最右端，与左侧那组参数没有任何视觉关系，描边还比主输入框更深
  // （层级倒置）。图标与设置页导航的「生成偏好」同款，语义一致 ——
  // 这里是常用参数，更多参数在设置里。
  const more = el("button", "qp-more");
  more.type = "button";
  more.title = "更多参数与设置（风格 / 人设 / 结尾引导 / 输出内容 / 补充资料）";
  more.setAttribute("aria-label", "更多参数与设置");
  more.innerHTML = `<svg viewBox="0 0 24 24" width="14" height="14" fill="none"
    stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"
    aria-hidden="true"><path d="M4 6h16M4 12h16M4 18h10"/>
    <circle cx="18" cy="18" r="2.2"/><circle cx="12" cy="6" r="2.2"/>
    <circle cx="7" cy="12" r="2.2"/></svg>`;
  more.onclick = () => openSettings("gen");
  box.appendChild(more);
  beautifySelects(box);
}

// ── 自绘下拉 ────────────────────────────────────────────────
// 原生菜单的系统蓝高亮 + 黑描边与整体设计语言冲突太大，统一换成自绘。
export function beautifySelects(scope = document) {
  scope.querySelectorAll("select:not([data-beauty])").forEach(sel => {
    sel.dataset.beauty = "1";
    sel.classList.add("native-hidden");
    // 可见的 .select-btn 已经承担了 aria-haspopup 与展开，原生 select 只是值的载体。
    // 不摘掉它的话每个下拉在 Tab 序里有**两个**停靠点，而且焦点环画在
    // 1px 透明的 .native-hidden 上 —— 看不见却能改值（规范 §2·5 第 5 条）。
    sel.tabIndex = -1;
    const wrap = el("div", "select-wrap" + (sel.dataset.pill ? " pill" : ""));
    sel.parentNode.insertBefore(wrap, sel);
    wrap.appendChild(sel);
    const btn = el("button", "select-btn");
    btn.type = "button";
    btn.setAttribute("aria-haspopup", "listbox");
    btn.innerHTML = `<span class="sel-text"></span>
      <svg class="chev" viewBox="0 0 12 8" width="11" height="8" fill="none" stroke="currentColor"
           stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
        <path d="M1 1.5L6 6.5L11 1.5"/></svg>`;
    const menu = el("div", "select-menu hidden");
    menu.setAttribute("role", "listbox");
    document.body.appendChild(menu);
    wrap.appendChild(btn);

    const sync = () => {
      const o = sel.options[sel.selectedIndex];
      btn.querySelector(".sel-text").textContent = o ? o.textContent : "";
      // 缺行业定制的当前值：胶囊变警示色 + 悬停给出原因。
      // 依据是 sel._audit（由 renderQuickParams 从 pack.param_audit 挂上）。
      // sel._warnNote 是给「非参数类」选择器（模型）用的显式警示 —— 它不来自
      // param_audit，而是调用方直接给出的原因（如未配 API Key）。
      const note = (sel._audit && sel._audit[sel.value]) || sel._warnNote || "";
      btn.classList.toggle("is-warn", !!note);
      // mock 态：当前返回夹具数据、根本没调模型。不说出来的话，用户会以为
      // 结果来自真模型 —— 这与「未配 Key」是两件事，所以颜色也不同。
      btn.classList.toggle("is-mock", !!sel._mock);
      const base = sel.title || "";
      btn.title = note ? (base ? base + "\n" : "") + note : base;
      menu.querySelectorAll(".select-opt").forEach(d =>
        d.classList.toggle("on", d.dataset.value === sel.value));
    };
    // 暴露给调用方：select 的值被程序改动后（「自定义模型…」只是个入口，
    // 选完要退回原值），外部需要主动重画按钮文字。
    sel._sync = sync;
    const build = () => {
      menu.innerHTML = "";
      Array.from(sel.options).forEach(o => {
        const note = sel._audit && sel._audit[o.value];
        // 缺定制的选项在**选中之前**就要能看出来，所以标记打在菜单项上，
        // 而不是只在选中后变色。
        const d = el("div", "select-opt" + (o.value === sel.value ? " on" : "")
          + (note ? " uncovered" : ""),
          `<span>${esc(o.textContent)}</span>` +
          (note ? `<span class="opt-warn" aria-hidden="true">!</span>` : "") +
          `<span class="tick">✓</span>`);
        d.dataset.value = o.value;
        d.setAttribute("role", "option");
        if (note) {
          d.title = note;
          d.setAttribute("aria-label", `${o.textContent}：${note}`);
        }
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
    const open = () => {
      build();
      menu.classList.remove("hidden");
      wrap.classList.add("open");
      btn.setAttribute("aria-expanded", "true");
      const r = wrap.getBoundingClientRect();
      // 先置于视口外测量自身尺寸：胶囊形态需按内容取宽（可宽于按钮）
      Object.assign(menu.style, {
        position: "fixed", left: "-9999px", right: "auto",
        top: "0px", width: "auto", maxHeight: "",
      });
      const mw = Math.max(menu.offsetWidth || 0, r.width);
      const natural = menu.offsetHeight || 0;
      const roomBelow = innerHeight - r.bottom - 6 - 8;
      const roomAbove = r.top - 6 - 8;
      const flipUp = roomAbove > roomBelow;
      const room = Math.max(96, flipUp ? roomAbove : roomBelow);
      const mh = Math.min(natural, room);
      Object.assign(menu.style, { maxHeight: mh + "px" });
      const left = Math.max(8, Math.min(r.left, innerWidth - mw - 8));
      const top = flipUp ? Math.max(8, r.top - 6 - mh) : r.bottom + 6;
      Object.assign(menu.style, { width: mw + "px", left: left + "px", top: top + "px" });
    };
    const close = () => {
      menu.classList.add("hidden");
      wrap.classList.remove("open");
      btn.setAttribute("aria-expanded", "false");
      Object.assign(menu.style, {
        position: "", left: "", right: "", top: "", width: "", maxHeight: "",
      });
    };
    sel._syncDropdown = sync;
    wrap._menu = menu;

    btn.onclick = () => (isOpen() ? close() : open());
    btn.onkeydown = ev => {
      const opts = Array.from(menu.querySelectorAll(".select-opt"));
      if (ev.key === "Escape") { close(); return; }
      if (ev.key === "ArrowDown" || ev.key === "ArrowUp") {
        ev.preventDefault();
        if (!isOpen()) open();
        const idx = opts.findIndex(d => d.classList.contains("on"));
        const next = ev.key === "ArrowDown"
          ? Math.min(idx + 1, opts.length - 1) : Math.max(idx - 1, 0);
        opts.forEach(d => d.classList.remove("hover"));
        (opts[next] || opts[0])?.classList.add("hover");
      } else if (ev.key === "Enter") {
        const hit = menu.querySelector(".select-opt.hover") || menu.querySelector(".select-opt.on");
        if (hit) hit.onclick();
      }
    };
    sync();
  });
  // 清除脱离 DOM 的孤儿菜单：wrap 被重建后挂在 body 上的旧菜单会残留
  {
    const live = new Set();
    document.querySelectorAll(".select-wrap").forEach(w => { if (w._menu) live.add(w._menu); });
    document.body.querySelectorAll(":scope > .select-menu").forEach(m => {
      if (!live.has(m)) m.remove();
    });
  }
  if (!window.__selOutsideBound) {
    window.__selOutsideBound = true;
    document.addEventListener("click", e => {
      document.querySelectorAll(".select-wrap.open").forEach(w => {
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

// ── 首页（landing）──────────────────────────────────────────
/** 问候语按时段分五档。写死一句「想聊点什么？」的问题不是它不好，而是它和
 *  顶部头部的「新对话」是同一层级的两句话 —— 换成带时段的问候，一句同时
 *  回答「现在能干什么」和「这是新的一条」，hero 就只剩标题 + 输入卡两件事。 */
const GREET_BUCKETS = [
  [5, "早上好"], [11, "中午好"], [13, "下午好"], [18, "晚上好"], [23, "夜深了"],
];
function greeting(h = new Date().getHours()) {
  const word = GREET_BUCKETS.find(([from]) => h < from)?.[1] || "夜深了";
  return `${word}，今天想讲点什么？`;
}

/** landing（首页）与对话态的唯一开关。
 *  改前三处 `$("empty").classList.add("hidden")` + 一处 remove 各写各的 ——
 *  现在示例胶囊搬到了合成器下面、不再嵌在 #empty 里，只切 #empty 会留下
 *  一排点不动的胶囊。所以把「谁可见 + 布局走哪套」收敛成一个函数、一个类。 */
export function setLanding(on) {
  $("view-chat").classList.toggle("is-landing", !!on);
  $("empty").classList.toggle("hidden", !on);
  $("empty-samples").classList.toggle("hidden", !on);
  if (on) {
    const h3 = $("empty-greet");
    if (h3) h3.textContent = greeting();
  }
}

export function renderSamples() {
  const box = $("empty-samples");
  if (!box || box.dataset.done) return;
  box.dataset.done = "1";
  box.innerHTML = "";
  for (const s of SAMPLES) {
    const b = el("button", "sample-card");
    b.textContent = s.t;
    b.title = `${s.tag} · ${s.d}`;
    b.onclick = () => {
      $("topic").value = s.t;
      refreshGate();
      autoGrowTopic();
      $("topic").focus();
    };
    box.appendChild(b);
  }
}

// ── 门控 ────────────────────────────────────────────────────
export function refreshGate() {
  const btn = $("btn-generate");
  const empty = !$("topic").value.trim();
  const canStop = state.busy && state.job;
  btn.disabled = state.busy ? !canStop : empty;
  btn.classList.toggle("stopping", !!canStop);
  btn.title = canStop ? "停止生成（Esc）"
    : state.busy ? "生成中…"
      : empty ? "输入主题后发送" : "发送（Enter / Ctrl + Enter）";
}

export function lockParams(lock) {
  $("topic").disabled = lock;
  const bar = $("quick-params");
  bar.classList.toggle("locked", lock);
  bar.querySelectorAll("select").forEach(s => { s.disabled = lock; });
  bar.querySelectorAll(".select-btn").forEach(b => { b.disabled = lock; });
  // 模型选择器一并锁上：生成中切模型对**正在跑的作业**没有任何影响
  // （作业启动时就带上了当时的模型），留着可点只会让人以为能中途换。
  const mp = $("model-pick");
  if (mp) {
    mp.classList.toggle("locked", lock);
    mp.querySelectorAll("select").forEach(s => { s.disabled = lock; });
    mp.querySelectorAll(".select-btn").forEach(b => { b.disabled = lock; });
  }
}

export function setCfgHint() {
  const pack = currentPack();
  const parts = [];
  if (pack) parts.push(pack.display_name || pack.name);
  if (getParam("segment")) parts.push(getParam("segment"));
  if (getParam("duration")) parts.push(getParam("duration") + "s");
  if (getParam("style")) parts.push(getParam("style"));
  const nav = $("btn-open-settings");
  if (nav) nav.title = `打开设置：${parts.join(" · ") || "生成偏好"}`;
}

// ── 折叠 ────────────────────────────────────────────────────
export function setLeftFolded(folded) {
  $("left").classList.toggle("folded", folded);
  localStorage.setItem("ts.left.folded", folded ? "1" : "0");
  const btn = $("btn-toggle-left");
  btn.classList.toggle("on", folded);
  btn.title = folded ? "展开会话栏（Ctrl+\\）" : "收起会话栏（Ctrl+\\）";
}

export function gotoView(name) {
  $$("#right > .view").forEach(v => v.classList.add("hidden"));
  $("view-" + name)?.classList.remove("hidden");
}

// `bindOnce`：全局监听只注册一次（修复前「新建行业包完成」会二次 boot()，
// 导致 Ctrl+\ 连翻两次等于没翻）。守卫从「只有 bindShell 有」统一到了
// util.bindOnce —— 五个绑定函数一处都不能漏，漏了 _verify 会报红。
export const bindShell = bindOnce(function bindShell() {
  if (localStorage.getItem("ts.left.folded") === "1") setLeftFolded(true);
  $("btn-toggle-left").onclick = () => setLeftFolded(!$("left").classList.contains("folded"));
  $("btn-open-settings").onclick = () => openSettings();
  // 「去配置」那颗按钮随空态引导一起下线了（见 index.html 的 #empty）——
  // 入口收敛到两处：工具条那颗模型胶囊，和设置页 headbar 的一颗「添加模型」。
  $("btn-packinfo").onclick = () => setPane("packinfo");

  // busy / job 任一变化都刷新门控与参数锁。
  // job 也要听：send() 先置 busy 再拿到 job_id，只听 busy 的话按钮会停在
  // 「生成中…」这个不可点状态，用户按不了停止。
  const syncGate = () => {
    // 不在生成态就停掉计时器：detachJob / 新建对话 / 打开历史都不会经过
    // jobs.js 的收尾分支，只靠那边的 stopTicker 会漏掉一个常驻 interval。
    if (!state.busy) stopTicker();
    refreshGate();
    lockParams(state.busy && state.loading);
    $("composer-gen-hint").textContent =
      state.busy && state.loading ? "生成中，参数已锁定（Esc 可停止）" : "";
  };
  on("busy", syncGate);
  on("job", syncGate);
  // meta 变了（保存设置 / 换包）要重画模型胶囊：刚配好 Key 或加完模型，
  // 工具条那颗必须立刻从「未配置模型」变成模型名，否则用户以为还没生效。
  on("meta", () => { fillPackSelect(); setCfgHint(); renderModelPicker(); });

  $("topic").addEventListener("input", () => { refreshGate(); autoGrowTopic(); });
  // Enter 发送、Shift+Enter 换行 —— placeholder、按钮 title、README 三处都这么承诺，
  // 而之前只有 Ctrl/Cmd+Enter 能用（按 Enter 只换行，界面一声不响）。
  // ⚠ isComposing 这层守卫不能省：这是中文输入应用，拼音/五笔选词时按的就是 Enter，
  //   不挡的话每确认一个候选词都会把半截消息发出去。keyCode 229 是老 WebKit 的等价写法。
  $("topic").addEventListener("keydown", e => {
    if (e.key !== "Enter" || e.shiftKey) return;
    if (e.isComposing || e.keyCode === 229) return;
    e.preventDefault();
    $("btn-generate").click();
  });

  // 参数变更：检测结果过期 + 刷新侧栏摘要。
  // 不能只认 #settings-screen —— 快捷条上的时长/平台/人设才是最常被改的几个。
  document.addEventListener("change", () => { updateStale(); setCfgHint(); });

  // 换行业包必须重渲染它带来的那批参数。
  // 快捷条胶囊（#quick-params）和「生成偏好」里的 #param-front 都是**按包**生成的，
  // 而 fillPackSelect() 只在启动 / 新建包 / meta 事件时被调用 —— 用户在下拉里换包
  // 这条路径**没有人接**（自定义下拉的选中只做 `sel.value=… + dispatchEvent('change')`，
  // 而 document 级那个 change 监听只管 updateStale/setCfgHint，不重渲染）。
  // 后果不只是"显示旧字段"：`cta` 这类参数的值域来自**包**，换了包却还留着上一个包的
  // 取值，生成时那个值在新包里不存在 → 静默降级（见 app/knowledge.py 的 param_audit），
  // 用户以为在定制、实际没生效。所以这里必须重渲染。
  // 换包 = 参数集与每项默认值全变，旧选择必须清空（否则新包沿用上一个包的
  // 取值，那个值在新包里根本不存在 → 静默降级，见 app/knowledge.py param_audit）。
  $("pack").addEventListener("change", () => { resetGenParams(); renderPackParams(); });

  document.addEventListener("keydown", e => {
    const mod = e.ctrlKey || e.metaKey;
    if (mod && e.key === "Enter") { e.preventDefault(); $("btn-generate").click(); return; }
    if (mod && e.key === "\\") {
      e.preventDefault();
      setLeftFolded(!$("left").classList.contains("folded"));
      return;
    }
    if (mod && e.key === ",") {
      e.preventDefault();
      settingsOpen() ? closeSettings() : openSettings();
      return;
    }
    if (mod && (e.key === "k" || e.key === "K")) {
      e.preventDefault();
      focusSessionSearch();
      return;
    }
    if (e.key === "Escape") {
      // 优先级：先收浮层 → 再关设置 → 最后才是「停止生成」。
      // 「停止」放最后是因为它是破坏性动作（token 不退），不该被误触。
      if (anyOverlayOpen()) { closeOverlays(); return; }
      if (settingsOpen()) { closeSettings(); return; }
      if (state.busy && state.job) {
        e.preventDefault();
        $("btn-generate").click();
      }
    }
  });
});
