// 左栏会话列表：按天分组（可折叠），增量更新。
//
// 增量更新这条必须保留：修复前每次刷新都整块 innerHTML 重画，鼠标按下与松开
// 之间只要发生一次重画，那一行连同删除按钮就被换成新 DOM，click 合成不出来 ——
// 表现就是「删除按钮要点好几次才中」。现在同一条记录永远复用同一个节点。
//
// 分组之前是「今天 / 昨天 / 本周 / 更早」四档，问题出在最后一档：一周以前的
// **全部并进「更早」**。真实数据里 78 条记录全落在同一周以前，整列只有一个
// 标签「更早 78」—— 分组等于没做，用户原话「全部记录平铺了，有点太多了」。
// 现在换成 `dayGroupKey()`（见 util.js）：今天 / 昨天 / MM-DD 周X / MM-DD。
//
// 新增：运行中的会话在副标题上带一个**进度点**，并且失败记录现在也能点开看原因
//（后端会把 job.json 摘要返回给 /api/history/{id}）。

import { $, el, esc, fmtTime, dayGroupKey, toast, bindOnce } from "./util.js";
import { api } from "./api.js";
import { state, setResult, detachJob } from "./store.js";
import { setLeftFolded } from "./ui.js";
import { attach, openRecord } from "./jobs.js";
import { STATE_LABEL } from "./progress.js";
import { appConfirm } from "./overlays.js";

const index = new Map();      // id -> 该行最近一次数据（委托 handler 从这里取，闭包不会过期）
let refreshTimer = null;

/** 折叠状态：分组 id → 是否收起。持久化到 localStorage。
 *
 * ⚠ 存的键是**分组 id**（`2026-09-12` 这种），不是 label。
 * label 会随时间推移变（今天的记录明天就成了「昨天」），拿 label 当键的话
 * 折叠状态第二天就错位到别的分组上。
 * ⚠ 默认**展开**：只记住用户主动折起来的那些，而不是记展开的那些 ——
 * 否则每来一个新日期都是一个需要用户重新点开的陌生分组。
 */
const FOLD_KEY = "ts.sess.folded";
let folded = new Set();
try {
  const raw = localStorage.getItem(FOLD_KEY);
  if (raw) folded = new Set(JSON.parse(raw));
} catch (_) { /* 存储不可用 / 内容损坏时退回「全展开」，不要因此让列表画不出来 */ }

function saveFolded() {
  try { localStorage.setItem(FOLD_KEY, JSON.stringify([...folded])); } catch (_) {}
}

function toggleFold(id) {
  if (folded.has(id)) folded.delete(id); else folded.add(id);
  saveFolded();
  paint(allItems);
}

const DEL_SVG = `<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor"
  stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
  <path d="M4 7h16M9.5 7V5.2A1.2 1.2 0 0 1 10.7 4h2.6a1.2 1.2 0 0 1 1.2 1.2V7M6.5 7l.8 12.1a1.2 1.2 0 0 0 1.2 1.1h7a1.2 1.2 0 0 0 1.2-1.1L17.5 7"/>
  <path d="M10.5 11v5.5M13.5 11v5.5"/></svg>`;

const settled = st => st === "done" || st === "failed" || st === "cancelled";

/** 行业包 slug → 界面显示名（取自 pack.yaml 的 display_name）。
 *  此前列表直接吐 slug，界面上出现的是「elevator」这种内部标识；而包里早就有
 *  display_name: 电梯，前端别处（ui.js 的下拉、settings.js 的包详情）也一直在用它 ——
 *  只有会话列表漏了。找不到对应包时退回 slug，不要显示空白。 */
export function packLabel(slug) {
  if (!slug) return "";
  const p = (state.meta?.packs || []).find(x => x.name === slug);
  return (p && (p.display_name || p.name)) || slug;
}

// 会话搜索：历史一多就找不到，这是左栏最缺的一块（对照成熟 agent 的
// 会话检索）。过滤只在前端做 —— 条目量级是几十到几百，没必要惊动后端。
let allItems = [];
let query = "";

function matches(it) {
  if (!query) return true;
  const q = query.toLowerCase();
  return String(it.topic || "").toLowerCase().includes(q)
    || String(it.pack || "").toLowerCase().includes(q)
    || packLabel(it.pack).toLowerCase().includes(q)   // 显示名也要能搜到，否则界面上写「电梯」却搜不出来
    || String(it.segment || "").toLowerCase().includes(q);
}

/** 拉取并绘制会话列表。返回本次拿到的条目（首启引导要据此判断是否「真的第一次」）。 */
export async function loadSessions() {
  let items = [];
  try { items = await api.history(); } catch (_) { /* 引擎不可达时保留上一次列表 */ }
  allItems = items;
  paint(items);
  clearTimeout(refreshTimer);
  // 还有会话没结束就定期刷新；结束后自动变成普通记录。
  // failed / cancelled 不算「没结束」，否则失败后这条会一直触发空转刷新。
  if (items.some(it => !settled(it.state || "done"))) {
    refreshTimer = setTimeout(loadSessions, 3000);
  }
  return items;
}

function paint(items) {
  const list = $("session-list");
  Array.from(list.children).forEach(n => { if (!n.dataset.key) n.remove(); });

  const shown = (items || []).filter(matches);
  if (query && !shown.length) {
    list.innerHTML = "";
    list.appendChild(el("p", "hint sess-empty", `没有匹配「${query}」的会话`));
    return;
  }
  items = shown;

  if (!items.length) {
    list.innerHTML = "";
    list.appendChild(el("p", "hint sess-empty", "还没有会话记录，先发一条试试"));
    return;
  }

  // 分组：按天。id 用于排序 / 折叠状态，label 用于展示（见 util.dayGroupKey）。
  // 遍历顺序就是列表顺序（后端已按 created_at 倒序），所以首次遇到某个 id
  // 的先后天然是**新 → 旧**；用 Map 的插入顺序即可，不必再排一次。
  // 搜索时**忽略折叠**：用户在找东西，把结果藏在折起来的分组里是纯粹的阻碍。
  const grouping = new Map();
  for (const it of items) {
    const g = dayGroupKey(it.created_at);
    if (!grouping.has(g.id)) grouping.set(g.id, { id: g.id, label: g.label, arr: [] });
    grouping.get(g.id).arr.push(it);
  }

  const specs = [];
  for (const g of grouping.values()) {
    specs.push({ kind: "grp", key: "g:" + g.id, id: g.id, label: g.label,
                 count: g.arr.length, isFolded: !query && folded.has(g.id) });
    if (query || !folded.has(g.id)) {
      for (const it of g.arr) specs.push({ kind: "row", key: "s:" + it.id, it });
    }
  }
  index.clear();
  for (const sp of specs) if (sp.kind === "row") index.set(sp.it.id, sp.it);

  const old = new Map();
  Array.from(list.children).forEach(n => { if (n.dataset.key) old.set(n.dataset.key, n); });
  let cursor = list.firstChild;
  for (const sp of specs) {
    let node = old.get(sp.key);
    if (!node) {
      node = sp.kind === "grp" ? buildGroup() : buildRow();
      node.dataset.key = sp.key;
    } else {
      old.delete(sp.key);
    }
    if (sp.kind === "grp") updateGroup(node, sp);
    else updateRow(node, sp.it);
    if (node === cursor) cursor = cursor.nextSibling;
    else list.insertBefore(node, cursor);
  }
  old.forEach(n => n.remove());
}

/** 分组标签：可点击折叠。
 *  用 <button> 而不是 <div> 才有键盘可达性（Tab 能到、Enter/Space 能触发）
 *  与读屏语义；`aria-expanded` 是这类「点一下展开收起」控件的标准信号。 */
function buildGroup() {
  const b = el("button", "group-lbl");
  b.type = "button";
  b.innerHTML = `<span class="gl-arrow" aria-hidden="true"></span><span class="gl-t"></span><span class="count"></span>`;
  return b;
}

function updateGroup(n, sp) {
  const sig = sp.label + "|" + sp.count + "|" + sp.isFolded;
  if (n._sig === sig) return;
  n._sig = sig;
  n.classList.toggle("folded", sp.isFolded);
  // aria-expanded 与视觉同源，避免「看着折了、读屏说展开了」
  n.setAttribute("aria-expanded", sp.isFolded ? "false" : "true");
  n.dataset.gid = sp.id;
  n.title = sp.isFolded ? `展开「${sp.label}」的 ${sp.count} 条` : `收起「${sp.label}」`;
  n.querySelector(".gl-t").textContent = sp.label;
  n.querySelector(".count").textContent = sp.count;
}

function buildRow() {
  const row = el("div", "sess-item");
  row.tabIndex = 0;
  row.setAttribute("role", "button");
  row.innerHTML = `<div class="sess-top"><span class="dot"></span><span class="sess-topic"></span></div>
    <div class="sess-sub"></div>`;
  row.appendChild(el("button", "sess-del", DEL_SVG));
  return row;
}

function updateRow(row, it) {
  const st = it.state || "done";
  const done = settled(st);
  const isCur = done
    ? (!!state.result && it.id === state.result.id)
    : (!!state.job && it.id === state.job.id);
  row.dataset.id = it.id;
  row.classList.toggle("active", isCur);

  const packName = packLabel(it.pack);
  // 副标题只留「行业 · 时间 · 平台」：时长 / 字数 / 合格三项从列表里撤掉，
  // 它们既占宽又互相压字，而列表这一层的用途只是「认哪条是哪条」。
  // 撤掉的信息不丢 —— 全部并进 row.title 的悬浮提示（见下方）。
  // 未完成的记录另把文字状态插在行业之后（只有色点会让读屏与色弱用户丢信息）。
  const parts = [packName, fmtTime(it.created_at)];
  if (it.platform) parts.push(it.platform);
  if (st !== "done") parts.splice(1, 0, STATE_LABEL[st] || st);
  const sub = parts.join(" · ");
  const sig = [isCur, st, it.topic, sub].join("\u0001");
  if (row._sig === sig) return;
  row._sig = sig;

  // 状态点三态：已完成看是否通过校验；失败也算未通过；在跑用呼吸点。
  // 不能只靠颜色：title 与 aria-label 都带上文字，读屏与色弱可用。
  const dotCls = st === "done" ? (it.passed ? "ok" : "no") : st === "failed" ? "no" : "run";
  const dot = row.querySelector(".dot");
  dot.className = "dot " + dotCls;
  const stateText = st === "done" ? (it.passed ? "已通过校验" : "未通过校验")
    : (STATE_LABEL[st] || st);
  row.setAttribute("aria-label", `${it.topic || "未命名"}，${stateText}`);
  row.querySelector(".sess-topic").textContent = it.topic || "";
  row.querySelector(".sess-sub").textContent = sub;
  // 列表里撤掉的三项在这里补回：副标题只是「窄」，不是「没有」。
  // 每项都先判 null —— 后端对在跑的作业不给 duration/chars/passed，
  // 硬拼会印出 undefined；filter(Boolean) 把空项整段丢掉，不留空行。
  const tip = [it.topic || "", sub];
  if (done) {
    if (it.duration != null) tip.push(`${it.duration}s`);
    if (it.chars != null) tip.push(`${it.chars} 字`);
    if (it.passed != null) tip.push(it.passed ? "已通过校验" : "未通过校验");
  }
  row.title = tip.filter(Boolean).join("\n");
  const del = row.querySelector(".sess-del");
  del.title = done ? "删除这条记录" : "放弃这次生成并移除记录";
  del.dataset.act = done ? "del" : "cancel";
}

/** 整个列表只在容器上挂一个监听器：行被刷新换掉后绑定不会错位。 */
export function focusSessionSearch() {
  const box = $("sess-search");
  if (!box) return;
  if ($("left").classList.contains("folded")) setLeftFolded(false);
  box.focus();
  box.select();
}

function bindSearch() {
  const box = $("sess-search");
  const clear = $("sess-search-clear");
  if (!box) return;
  const apply = () => {
    query = box.value.trim();
    clear.classList.toggle("hidden", !query);
    paint(allItems);
  };
  box.addEventListener("input", apply);
  box.addEventListener("keydown", ev => {
    if (ev.key === "Escape") {
      // 先清搜索，不要一按 Esc 就把焦点弄丢（用户可能还想接着输）
      if (box.value) { box.value = ""; apply(); ev.stopPropagation(); return; }
      box.blur();
    }
    if (ev.key === "Enter") {
      const first = $("session-list").querySelector(".sess-item");
      if (first) first.click();
    }
  });
  clear.onclick = () => { box.value = ""; apply(); box.focus(); };
}

export const bindSessionList = bindOnce(function bindSessionList() {
  bindSearch();
  const list = $("session-list");
  const activate = (row) => {
    const it = index.get(row.dataset.id);
    if (!it) return;
    (it.state || "done") === "done" ? openRecord(it.id) : attach(it.id);
  };
  list.addEventListener("click", ev => {
    // 分组标签的折叠优先于行点击：两者在 DOM 上是兄弟，不会互相误判。
    const grp = ev.target.closest(".group-lbl");
    if (grp) { toggleFold(grp.dataset.gid); return; }
    const row = ev.target.closest(".sess-item");
    if (!row) return;
    const it = index.get(row.dataset.id);
    if (!it) return;
    if (ev.target.closest(".sess-del")) {
      ev.stopPropagation();
      ev.preventDefault();
      deleteSession(it);
      return;
    }
    activate(row);
  });
  list.addEventListener("keydown", ev => {
    // <button> 的 Enter/Space 本来就会派发 click，这里不要再处理一遍，
    // 否则一次按键折叠两次 = 看起来「点了没反应」。
    if (ev.target.closest(".group-lbl")) return;
    if (ev.key !== "Enter" && ev.key !== " ") return;
    const row = ev.target.closest(".sess-item");
    if (!row) return;
    ev.preventDefault();
    activate(row);
  });
});

/** 删除：已结束的记录走 DELETE 删产物；进行中的走 DELETE 也会顺带停掉后台作业
 *  （后端 discard = 取消 + 移出注册表 + 删目录），所以两种情形是同一个请求。
 *
 * 修复前进行中只调 cancel：作业停了，但 job.json 仍然落盘、仍然进历史索引，
 * 于是弹窗承诺「移除该记录」、列表里却还留着一条「已取消」 —— 承诺与行为不符。 */
async function deleteSession(it) {
  const st = it.state || "done";
  const done = settled(st);
  const name = String(it.topic || "").slice(0, 20);
  const ok = done
    ? await appConfirm("删除记录", `「${name}」删除后不可恢复。`)
    : await appConfirm("放弃这次生成", `「${name}」正在生成，将停止并移除该记录。`);
  if (!ok) return;
  try {
    await api.removeRecord(it.id);
    if (state.job && state.job.id === it.id) detachJob();
    if (state.result && state.result.id === it.id) setResult(null);
    toast(done ? "已删除" : "已放弃");
    loadSessions();
  } catch (e) {
    toast("删除失败：" + e.message, 3500);
  }
}
