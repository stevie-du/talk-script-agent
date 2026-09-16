// 设置：整窗二级页（生成偏好 / 行业包 / 新建行业包 / 模型接口 / 知识库 / 技能）。
//
// 修复前的一个交互坑：每次打开设置都无条件 preloadSettings()，会把用户
// 「刚填好但还没保存」的 base_url / model 覆盖回服务端的旧值 ——
// 顺序是「打开设置 → 改 URL → Ctrl+, 关掉 → Ctrl+, 再开 → 输入没了」。
// 现在按字段记 dirty，只有没被改过的输入框才回填。

import { $, el, esc, toast, bindOnce } from "./util.js";
import { api } from "./api.js";
import { state, emit } from "./store.js";
import { closeOverlays, appConfirm } from "./overlays.js";
// ui.js 与 settings.js 互相引用，但引用的都是**函数声明**（会被提升），
// 所以循环依赖在调用时已解析完毕，安全。
import { fillPackSelect } from "./ui.js";

const PANES = ["gen", "packinfo", "packgen", "llm", "kb", "skills"];
// 导航项 → 资源；面板 → 资源下可能有的「子页面」。
// packgen 不在左导航里（动作不是资源），但它需要让「行业包」导航高亮，
// 读作「你在行业包这个资源下，进了它的子动作」。
// map: pane → 应高亮的 nav item data-pane（默认就是自身）
const NAV_OF_PANE = { packgen: "packinfo" };
// packgen 的来源面板：gen 的 [新建] 按钮 vs packinfo headbar 的 [新建]，
// 决定了「返回」按钮回到哪里。设成模块状态是因为 packgen 同一会话内
// 可能从两个入口先后进，记录最后一次的来源。
let packgenFrom = "gen";
const dirty = new Set();

// 数值项的合法区间。**必须与后端 `app/server.py` 的 `NUMERIC_BOUNDS` 逐项相等** ——
// 前后端没法共享代码，一致性由 `tests/test_numeric_bounds_consistency.py`
// 同时读这两个文件比对，别只改一处。
//
// 为什么要专门钉：这两处曾经不一致 —— 前端卡 0 ~ 1.5、后端卡 0.0 ~ 2.0，
// 用户填 1.8 会被**前端**拒掉而后端完全接受。前端比后端更严是更坏的一种不一致：
// 用户看到「界面说不行」，绕不过去，也想不到是界面在凭想象设限。
export const NUMERIC_BOUNDS = {
  temperature: [0, 2],
  retries: [0, 10],
  timeout: [5, 1800],
  max_tokens: [256, 200000],
};

// 区间 → 输入框的对应关系。校验与 min/max 都从这一张表来，
// 避免「能填的范围」和「能存的范围」变成两套数（temperature 曾经就是这样）。
const NUM_FIELDS = [
  ["temperature", "st-temperature", "采样温度"],
  ["retries", "st-retries", "重试次数"],
  ["timeout", "st-timeout", "单次超时"],
  ["max_tokens", "st-maxtokens", "输出预算"],
];

// 把区间写到输入框的 min/max 上。HTML 里那对属性只是**初始值**，
// 以这里为准 —— 否则改一处忘了另一处，浏览器照样能填出越界值。
function applyNumericBounds() {
  for (const [key, id] of NUM_FIELDS) {
    const n = $(id);
    if (!n) continue;
    const [lo, hi] = NUMERIC_BOUNDS[key];
    n.min = lo;
    n.max = hi;
  }
}

export function settingsOpen() {
  return !$("settings-screen").classList.contains("hidden");
}

export function openSettings(pane) {
  closeOverlays();          // 确认浮层 z 高于设置页，不关会一直糊在整窗上
  $("settings-screen").classList.remove("hidden");
  setPane(PANES.includes(pane) ? pane : state.settingsPane);
  preloadSettings().catch(() => {});
  return Promise.resolve();
}

export function closeSettings() {
  if (settingsOpen()) $("settings-screen").classList.add("hidden");
}

export function setPane(pane) {
  if (!PANES.includes(pane)) pane = state.settingsPane;
  PANES.forEach(p => $(("pane-" + p))?.classList.toggle("hidden", p !== pane));
  // 导航高亮：packgen 这种子动作让「资源」项高亮（NAV_OF_PANE 映射）。
  // 单一 `.on` 仍是断言守的「高亮唯一」，packgen 走到 packinfo，
  // 不会出现「两个 .on」或「没有 .on」。
  const navPane = NAV_OF_PANE[pane] || pane;
  document.querySelectorAll(".stg-nav-item").forEach(n => {
    n.classList.toggle("on", n.dataset.pane === navPane);
  });
  // 切面板要归零的滚动容器是 .stg-main，不是 .stg-pane。
  // 后者在 styles.css 里已不设 overflow（限宽居中的内容盒子），
  // 滚动容器上提是为了让滚动条贴页面右边 —— 归零必须跟着上提一级，
  // 否则从长面板切走再切回，会停在上次的位置。
  const stgMain = $("settings-screen")?.querySelector(".stg-main");
  if (stgMain) stgMain.scrollTop = 0;
  state.settingsPane = pane;
  emit("pane", pane);
  // 行业包详情每次进入都重拉：包可能被切换过，文件清单与草稿角标也可能变了
  if (pane === "packinfo") {
    openPackInfo().catch(e => toast("读取失败：" + e.message, 3500));
  } else if (pane === "kb" || pane === "skills") {
    openPackFiles(pane);
  }
  return pane;
}

async function preloadSettings() {
  const c = await api.config();
  fill("st-baseurl", c.base_url || "");
  fill("st-model", c.model || "");
  fill("st-temperature", c.temperature ?? "");
  fill("st-retries", c.retries ?? "");
  fill("st-timeout", c.timeout ?? "");
  fill("st-maxtokens", c.max_tokens ?? "");
  if (!dirty.has("st-apikey")) $("st-apikey").value = "";
  renderConfigError(c.config_error);
  const env = c.env_override ? "（当前由环境变量 TALKSCRIPT_API_KEY 覆盖）" : "";
  $("st-status").textContent = c.mock
    ? `当前为 mock 模式（返回夹具，不调模型）${env}`
    : c.api_key_set
      ? `已配置 Key · 模型 ${c.model} · 重试 ${c.retries} 次 / 超时 ${c.timeout}s${env}`
      : `未配置 API Key${env}`;
}

// config.yaml 读坏时的提示。
//
// 为什么必须显示：读坏之后上面那些输入框填的全是**内置默认值**，
// 而下面那行状态会照常说「已配置 Key · 模型 glm-4.7」——
// 用户以为配置还在，其实自己填的 base_url 一次都没生效过。
// 提示里要写清两件事：①上面显示的不是你的配置；②怎么恢复（重新保存一次）。
function renderConfigError(msg) {
  const n = $("st-cfg-err");
  if (!n) return;
  if (!msg) { n.classList.add("hidden"); n.textContent = ""; return; }

  n.textContent = `⚠ ${msg}。上面显示的是内置默认值，不是你保存过的配置 ——`
    + `重新填一次并点「保存」即可覆盖修复。`;
  n.classList.remove("hidden");
}

function fill(id, value) {
  if (dirty.has(id)) return;      // 用户改过就不覆盖
  const n = $(id);
  if (n) n.value = value;
}

export const bindSettings = bindOnce(function bindSettings() {
  applyNumericBounds();          // 区间由 JS 统一写入输入框的 min/max
  ["st-baseurl", "st-model", "st-apikey", "st-temperature",
   "st-retries", "st-timeout", "st-maxtokens"].forEach(id => {
    $(id).addEventListener("input", () => dirty.add(id));
  });
  document.querySelectorAll(".stg-nav-item").forEach(n => {
    n.onclick = () => setPane(n.dataset.pane);
  });
  $("btn-close-settings").onclick = closeSettings;
  $("btn-packinfo").onclick = () => setPane("packinfo");
  $("pi-close").onclick = () => setPane("gen");
  // packgen 的两个入口：gen 的 [新建]（btn-newpack）和 packinfo headbar 的 [新建]（pi-newpack）。
  // 区别在于「取消」回哪里 —— 这就是 packgenFrom 的存在意义。
  $("btn-newpack").onclick = () => {
    packgenFrom = "gen";
    $("pg-form").classList.remove("hidden");
    $("pg-result").classList.add("hidden");
    setPane("packgen");
  };
  $("pi-newpack").onclick = () => {
    packgenFrom = "packinfo";
    $("pg-form").classList.remove("hidden");
    $("pg-result").classList.add("hidden");
    setPane("packgen");
  };
  $("pi-refresh").onclick = () => openPackInfo().catch(e => toast("刷新失败：" + e.message, 3500));
  $("kb-refresh").onclick = () => openPackFiles(state.settingsPane);
  $("skills-refresh").onclick = () => openPackFiles(state.settingsPane);
  $("pg-close").onclick = () => setPane(packgenFrom);
  $("pg-run").onclick = runPackgen;
  $("pg-done").onclick = onPackDone;

  $("st-save").onclick = async () => {
    try {
      await saveSettings();
      dirty.clear();
    } catch (e) { toast("保存失败：" + e.message, 3500); }
  };
  // 「恢复默认」：base_url / model 填错之后，留空保存是无效的空操作
  // （后端会过滤空串防手滑），所以回到默认必须是一个显式动作。
  $("st-reset-baseurl").onclick = () => resetField("base_url", "st-baseurl");
  $("st-reset-model").onclick = () => resetField("model", "st-model");
  $("kb-pack").onchange = () => openPackFiles(state.settingsPane);
  $("skills-pack").onchange = () => openPackFiles(state.settingsPane);
  $("st-reset-adv").onclick = () => resetField(
    ["retries", "timeout", "max_tokens"], ["st-retries", "st-timeout", "st-maxtokens"]);
  $("st-test").onclick = testConnection;
  $("pi-export").onclick = exportSkill;
  $("pi-undraft").onclick = undraftPack;
});

async function saveSettings() {
  const body = {
    base_url: $("st-baseurl").value.trim(),
    model: $("st-model").value.trim(),
  };
  const key = $("st-apikey").value.trim();
  if (key) body.api_key = key;
  // 数值项（含采样温度）：留空表示不改，填了就在前端先卡一遍范围。
  // 后端也会卡，这里只是让错误当场可见，不必等一次往返；
  // 区间与输入框 min/max 共用 NUMERIC_BOUNDS 这一张表。
  //
  // 温度修复前是单独一个 if、区间写死 1.5（与后端的 2.0 不一致），
  // 现在并入同一张表 —— 少一处「凭想象设限」的机会。
  for (const [key, id, label] of NUM_FIELDS) {
    const raw = $(id).value.trim();
    if (raw === "") continue;
    const n = Number(raw);
    const [lo, hi] = NUMERIC_BOUNDS[key];
    if (!Number.isFinite(n) || n < lo || n > hi) {
      toast(`${label}需在 ${lo} ~ ${hi} 之间`);
      return;
    }
    body[key] = n;
  }
  await api.saveConfig(body);
  $("st-apikey").value = "";
  toast("已保存，下次生成即生效");
  // 顶栏的模型名/Key 状态要跟着变，否则用户以为没保存成功
  try {
    state.meta = await api.meta();
    emit("meta", state.meta);
  } catch (_) { /* 忽略：保存本身已成功 */ }
  await preloadSettings().catch(() => {});
}

async function resetField(field, inputId) {
  const fields = [].concat(field);
  const ids = [].concat(inputId);
  try {
    await api.resetConfig(fields);
    // 清掉 dirty 标记，否则 preloadSettings 会拒绝回填输入框
    ids.forEach(id => dirty.delete(id));
    await preloadSettings();
    toast("已恢复默认值");
    state.meta = await api.meta();
    emit("meta", state.meta);
  } catch (e) {
    toast("恢复失败：" + e.message, 3500);
  }
}

// ── 知识库 / 技能：两个面板共用一套只读查看器，但筛不同角色 ──────────
// 之前的 kb: () => true 是设计漏洞——知识库与技能会显示**同一组文件**
// （skill.yaml 在两边都出现），命名错位。改成按命名分组互不重叠：
//   kb     = 包清单 + 知识库 + 私有资料  （"资料类"）
//   skills = 技能主文件 + 规则/方法库/合规（"技能类"）
// 做成只读而不是增删改，是刻意的：这些文件是行业包的「源码」，写坏了
// 整个包都废，而浏览器里改文件既没有原子写也没有校验，风险与收益不成比例。
const PANE_FILE_FILTER = {
  kb:     (rel) => rel === "pack.yaml"
                 || rel.startsWith("knowledge/") || rel.startsWith("private/"),
  skills: (rel) => rel === "skill.yaml"
                 || /^(rules|patterns|compliance)\//.test(rel),
};

// ── 设置内搜索已移除 ──────────────────────────────────────
// 曾经的 filterSettingsNav() 按「导航项文字 + 分区面板全文」过滤左导航，
// 匹配不到就把整组连同标题一起收掉。删掉的理由：设置一共 6 个分区，
// 一屏就能看全，检索框的收益抵不上它在「返回工作区」下方多占的一行，
// 而且它是**唯一**会隐藏导航项的入口 —— 一旦过滤词留在框里（比如上次
// 输的「超时」），用户回到设置页会看到一份缺项却没有任何提示的导航，
// 像个 bug。现在导航恒定完整，所见即所得。
// 连带清理：.stg-search 的 HTML / CSS，以及只为它服务的
// `.stg-nav-item.hidden` / `.nav-sec.hidden` 两条规则。

// 分组卡片网格渲染：按 FILE_ROLE 分组，每组一个 .kb-group，
// 每张卡片 = 图标 + 文件名 + 角色描述 + 大小（点击切换右侧查看器）。
// 之前的实现是单层 flat 列表（行 295 是 `row = el("button", "kb-item", ...)`），
// 13 个文件一屏挤下来要找特定文件得在一堆路径里翻。
// 「分组 → 卡片」是 Zcode 的范式，对应"已安装 / 浏览器插件 / 文档技能"那种结构。
//
// 兼容性：每张卡仍是 `.kb-item`（断言 kb.rows === 3 还过着），
// 文件名放进 `.kb-name`（断言里 `n.firstChild.textContent` 改成 `.kb-name` 即可）。
const ROLE_GROUP_ORDER = [
  // 顺序就是分组从上到下出现的顺序；
  // 与"重要性"对齐：技能 → 规则 → 方法库 → 合规 → 知识库 → 禁用词 → 草稿 → 私有 → 包清单
  { key: "skill",      label: "技能（怎么写）" },
  { key: "rules",      label: "规则" },
  { key: "patterns",   label: "方法库" },
  { key: "compliance", label: "合规" },
  { key: "knowledge",  label: "知识库" },
  { key: "banwords",   label: "禁用词表" },
  { key: "checklist",  label: "草稿校对" },
  { key: "private",    label: "私有资料" },
  { key: "package",    label: "包清单" },
];

// FILE_ROLE 返回两类东西：CSS 用的 role key（稳定字符串）+ 用户看的中文标签。
// 一个文件 → 一个 key；key 与 ROLE_GROUP_ORDER 对应。
function fileRole(rel) {
  if (rel === "pack.yaml")     return "package";
  if (rel === "skill.yaml")    return "skill";
  if (rel === "banwords.yaml") return "banwords";
  if (rel === "校对清单.md")   return "checklist";
  if (rel.startsWith("knowledge/"))  return "knowledge";
  if (rel.startsWith("compliance/")) return "compliance";
  if (rel.startsWith("patterns/"))   return "patterns";
  if (rel.startsWith("rules/"))      return "rules";
  if (rel.startsWith("private/"))    return "private";
  return "package";  // 兜底：未识别的归"包清单"组，颜色中性
}
const ROLE_LABEL = Object.fromEntries(ROLE_GROUP_ORDER.map(g => [g.key, g.label]));

// 每个 role 的图标 SVG。统一 24×24 viewBox，stroke 用 currentColor
// （颜色由 CSS .kb-item[data-role=...] .kb-icon 的 color 接管，不写死）。
// 必须放在 openPackFiles 之前：函数体里用到 ROLE_ICON[key]，
// 但 async function 在模块顶层执行到时 ROLE_ICON 是 TDZ（const 不 hoist）。
const ROLE_ICON = {
  skill: `<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 3l2.2 4.5 5 .7-3.6 3.5.9 4.9L12 14.3 7.5 16.6l.9-4.9L4.8 8.2l5-.7z"/></svg>`,
  rules: `<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 6h16M4 12h16M4 18h10"/></svg>`,
  patterns: `<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="6" cy="6" r="2"/><circle cx="18" cy="6" r="2"/><circle cx="12" cy="18" r="2"/><path d="M6 8v3a3 3 0 0 0 3 3h6a3 3 0 0 0 3-3V8"/></svg>`,
  compliance: `<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 3l8 4v6a8 8 0 0 1-8 8 8 8 0 0 1-8-8V7z"/><path d="M9 12l2 2 4-4"/></svg>`,
  knowledge: `<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 5.5A1.5 1.5 0 0 1 5.5 4H10a2 2 0 0 1 2 2v13a1.5 1.5 0 0 0-1.5-1.5H4z"/><path d="M20 5.5A1.5 1.5 0 0 0 18.5 4H14a2 2 0 0 0-2 2v13a1.5 1.5 0 0 1 1.5-1.5H20z"/></svg>`,
  banwords: `<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" aria-hidden="round" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M5.6 5.6l12.8 12.8"/></svg>`,
  checklist: `<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M9 11l3 3 8-8"/><path d="M20 12v7a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h9"/></svg>`,
  private: `<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="5" y="11" width="14" height="9" rx="2"/><path d="M8 11V7a4 4 0 0 1 8 0v4"/></svg>`,
  package: `<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 8l-9-5-9 5 9 5 9-5z"/><path d="M3 8v8l9 5 9-5V8"/><path d="M12 13v8"/></svg>`,
};

async function openPackFiles(pane) {
  // 知识库与技能两个面板共用一套只读查看器，但 DOM 节点分开
  // （同一 ID 在一个文档里只能出现一次）
  const pfx = pane === "skills" ? "skills" : "kb";
  const list = $(pfx + "-list");
  const sel = $(pfx + "-pack");
  if (sel && !sel.options.length && state.meta) {
    sel.innerHTML = "";
    for (const p of state.meta.packs) {
      const o = el("option", "", esc(p.display_name) + (p.draft ? "（草稿）" : ""));
      o.value = p.name;
      sel.appendChild(o);
    }
    sel.value = state.meta.default_pack || (state.meta.packs[0] || {}).name || "";
  }
  const name = sel.value;
  if (!name) { list.innerHTML = "<p class='hint'>还没有可用的行业包。</p>"; return; }
  list.innerHTML = "<p class='hint'>载入中…</p>";
  try {
    const p = await api.pack(name);
    const keep = PANE_FILE_FILTER[pane] || (() => true);
    const files = (p.files || []).filter(f => keep(f.rel));
    list.innerHTML = "";
    if (!files.length) {
      list.innerHTML = "<p class='hint'>这个包没有符合条件的文件。</p>";
      return;
    }
    // 按 ROLE_GROUP_ORDER 顺序分组，未识别的归到"包清单"组（兜底）。
    const buckets = new Map(ROLE_GROUP_ORDER.map(g => [g.key, []]));
    for (const f of files) {
      const k = fileRole(f.rel);
      if (!buckets.has(k)) buckets.set(k, []);
      buckets.get(k).push(f);
    }
    const groups = el("div", "kb-groups");
    for (const { key, label } of ROLE_GROUP_ORDER) {
      const items = buckets.get(key);
      if (!items || !items.length) continue;        // 空组不显示（不浪费一行的分组标题）
      const grp = el("div", "kb-group");
      const h = el("h4", "kb-group-t");
      h.appendChild(document.createTextNode(label));
      h.appendChild(el("span", "kb-group-c", String(items.length)));
      grp.appendChild(h);
      const cards = el("div", "kb-cards");
      for (const f of items) {
        const row = el("button", "kb-item");
        row.type = "button";
        row.dataset.role = key;
        // 顺序：图标 → 元信息（名称+描述）→ 大小。firstChild 是图标，
        // 文件名放进 .kb-name —— 这是断言要查的元素（不是 firstChild 文本）。
        const icon = el("span", "kb-icon");
        icon.innerHTML = ROLE_ICON[key] || ROLE_ICON.package;
        row.appendChild(icon);
        const meta = el("div", "kb-meta");
        meta.appendChild(el("span", "kb-name", f.rel));
        meta.appendChild(el("span", "kb-desc", ROLE_LABEL[key] || ""));
        row.appendChild(meta);
        const sizeTxt = f.size > 1024
          ? (f.size / 1024).toFixed(1) + " KB"
          : f.size + " B";
        row.appendChild(el("span", "kb-size", sizeTxt));
        row.onclick = () => showPackFile(name, f.rel, row, pfx);
        cards.appendChild(row);
      }
      grp.appendChild(cards);
      groups.appendChild(grp);
    }
    list.appendChild(groups);
  } catch (e) {
    list.innerHTML = `<p class='hint'>载入失败：${esc(e.message)}</p>`;
  }
}

async function showPackFile(name, rel, row, pfx) {
  document.querySelectorAll(`#${pfx}-list .kb-item`).forEach(n => n.classList.remove("on"));
  if (row) row.classList.add("on");
  $(pfx + "-title").textContent = rel;
  $(pfx + "-size").textContent = "";
  $(pfx + "-body").textContent = "载入中…";
  try {
    const d = await api.packFile(name, rel);
    $(pfx + "-size").textContent =
      d.size > 1024 ? (d.size / 1024).toFixed(1) + " KB" : d.size + " B";
    $(pfx + "-body").textContent = d.text;
  } catch (e) {
    $(pfx + "-body").textContent = "读取失败：" + e.message;
  }
}

async function testConnection() {
  const btn = $("st-test");
  const label = btn.textContent;
  btn.disabled = true;
  btn.textContent = "测试中…";
  $("st-status").textContent = "正在连接…";
  try {
    // 带上界面当前值（Key 留空时后端沿用已保存的），未保存也能测
    const r = await api.testConfig({
      base_url: $("st-baseurl").value.trim(),
      api_key: $("st-apikey").value,
      model: $("st-model").value.trim(),
    });
    $("st-status").textContent = r.ok
      ? `连接正常 · ${r.model}${r.detail ? " · " + r.detail : ""}`
      : `连接失败：${r.detail}`;
    toast(r.ok ? "连接正常" : "连接失败，请看下方提示", r.ok ? 2000 : 4000);
  } catch (e) {
    $("st-status").textContent = e.message;
    toast("测试失败：" + e.message, 4000);
  }
  btn.disabled = false;
  btn.textContent = label;
}

async function runPackgen() {
  const industry = $("pg-industry").value.trim();
  const desc = $("pg-desc").value.trim();
  if (!industry || !desc) { toast("请填写行业名称和业务描述"); return; }
  const btn = $("pg-run");
  btn.disabled = true;
  btn.textContent = "生成中（约 1-2 分钟）…";
  try {
    const out = await api.createPack(industry, desc);
    state.lastCreatedPack = out.name;
    $("pg-form").classList.add("hidden");
    $("pg-result").classList.remove("hidden");
    $("pg-checklist").textContent = out.checklist;
    toast(`行业包「${out.display_name}」已生成（草稿）`);
  } catch (e) {
    toast("生成失败：" + e.message, 6000);
  }
  btn.disabled = false;
  btn.textContent = "生成";
}

async function onPackDone() {
  setPane("gen");
  try {
    state.meta = await api.meta();
    emit("meta", state.meta);
    // 显式选中刚建好的包（emit 只负责刷新列表，选择权在这里）
    fillPackSelect({ prefer: state.lastCreatedPack });
    toast("已切换到新建的行业包");
  } catch (e) { toast("刷新行业包列表失败：" + e.message, 3500); }
}

async function exportSkill() {
  const name = $("pack").value;
  const btn = $("pi-export");
  btn.disabled = true;
  try {
    const out = await api.exportSkill(name, false);
    $("pi-export-hint").innerHTML =
      `✅ 已导出 <b>${esc(out.files)}</b> 个文件到：<br><code>${esc(out.path)}</code>`
      + `<br>${out.hints.map(esc).join("<br>")}`;
    toast("已导出为 Agent 技能");
  } catch (e) { toast("导出失败：" + e.message, 4000); }
  btn.disabled = false;
}

async function undraftPack() {
  const name = $("pack").value;
  const ok = await appConfirm("标记为已校对",
    "确认该行业包已人工校对完毕？\n草稿标记会被移除，生成结果不再提示“需校对”。");
  if (!ok) return;
  try {
    await api.undraft(name);
    toast("已标记为校对完成");
    state.meta = await api.meta();
    emit("meta", state.meta);
    await openPackInfo();
  } catch (e) { toast("操作失败：" + e.message, 3500); }
}

// packinfo 表格的「说明」列要中文标签，调用方传入 fileRole() 拿到 key 再查 ROLE_LABEL。
// 不要在这里另写一份中文映射：两份的话改一份忘一份就漂了。
// 之前有一份返回中文标签的 FILE_ROLE()，现已统一到 fileRole + ROLE_LABEL。

async function openPackInfo() {
  const name = $("pack").value;
  const p = await api.pack(name);
  $("pi-title").textContent = `${p.display_name || name} · 包内容`;
  $("pi-desc").textContent = (p.description || "")
    + (p.draft ? "（草稿包：内容需人工校对后投产）" : "");
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
  tb.innerHTML = "<thead><tr><th>文件</th><th class='col-size'>大小</th>"
    + "<th class='col-role'>说明</th></tr></thead>";
  const body = el("tbody");
  for (const f of p.files || []) {
    const tr = el("tr");
    const kb = f.size > 1024 ? (f.size / 1024).toFixed(1) + " KB" : f.size + " B";
    tr.innerHTML = `<td class="cell-mono">${esc(f.rel)}</td>
      <td>${kb}</td><td>${esc(ROLE_LABEL[fileRole(f.rel)] || "")}</td>`;
    body.appendChild(tr);
  }
  tb.appendChild(body);
  box.appendChild(tb);
}
