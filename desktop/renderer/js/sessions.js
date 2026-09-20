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
// 行是**单行制**（状态记号 + 标题 + 时刻）：一屏能看的条数翻倍，
// 而行业 / 平台 / 时长 / 字数 / 完整状态这些行上放不下的，全部进 title 悬浮提示，
// 点开记录后右栏头部也照样给全（result.js）。
// 记号只标异常：正常完成的记录不画点 —— 一列里绝大多数都是「正常完成」，
// 每条都落的记号等于没有记号（对照 Claude / Linear 的侧栏）。
// 失败记录能点开看原因 —— 后端会把 job.json 摘要返回给 /api/history/{id}。

import { $, el, fmtClock, fmtStamp, dayGroupKey, toast, bindOnce } from "./util.js";
import { api } from "./api.js";
import { state, setResult, detachJob } from "./store.js";
import { setLeftFolded } from "./ui.js";
import { attach, openRecord } from "./jobs.js";
import { STATE_LABEL, BUSY_STATES } from "./progress.js";
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

/* 折叠箭头：内联 SVG，14px（规范 §4 的「行内符号 / 箭头」档）。
   ⚠ 方向必须是**展开朝下、收起朝右** —— 此前用 CSS 边框画三角，
   静止态画出来是朝右的 ▶、折叠时 rotate(90deg) 转成朝下 ▼，
   与所有文件树 / 分组列表的惯例正好相反：收起的那组看起来像可以展开。 */
const CHEVRON = `<svg class="gl-arrow" viewBox="0 0 24 24" width="14" height="14" fill="none"
  stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"
  aria-hidden="true"><path d="M6.5 9.5 12 15l5.5-5.5"/></svg>`;

const DEL_SVG = `<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor"
  stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
  <path d="M4 7h16M9.5 7V5.2A1.2 1.2 0 0 1 10.7 4h2.6a1.2 1.2 0 0 1 1.2 1.2V7M6.5 7l.8 12.1a1.2 1.2 0 0 0 1.2 1.1h7a1.2 1.2 0 0 0 1.2-1.1L17.5 7"/>
  <path d="M10.5 11v5.5M13.5 11v5.5"/></svg>`;

/** 「这一趟结束了吗」= 不在跑。
 *  取 `BUSY_STATES` 的**补集**，而不是自己抄一份终态清单 ——
 *  抄终态会把认不出来的状态（索引里残留的已删除状态）读成「还在跑」，
 *  于是每 3 秒空转刷新永不停止。判据的出处与理由见 progress.js。 */
const settled = st => !BUSY_STATES.has(st);

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
  // 只有**在跑**的作业才值得继续轮询（判据是 BUSY_STATES 白名单，
  // 终态与认不出来的状态都不算）。写成「非终态就轮询」的话，
  // 索引里一条残留的已删除状态就能让这里每 3 秒空转一次、永不停止。
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
  b.innerHTML = `${CHEVRON}<span class="gl-t"></span><span class="count"></span>`;
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

/** 一行 = 状态记号 + 标题 + 时刻，三项排在同一条基线上。
 *  状态点占一个 14px 图标盒（与分组箭头的 14px 同宽），于是标题文字的左边缘
 *  与分组标签文字的左边缘落在同一个 x —— 旧的「标题 + 副标题」两行式里，
 *  副标题从 20px 起、标题从 34px 起，同一行内两个左边缘，读起来就是「没对齐」。
 *  行上没有状态字：只有异常才落记号，理由见 updateRow 里那张三档表。 */
function buildRow() {
  const row = el("div", "sess-item");
  row.tabIndex = 0;
  row.setAttribute("role", "button");
  row.innerHTML = `<span class="dot"></span><span class="sess-topic"></span>`
    + `<span class="sess-time"></span>`;
  row.appendChild(el("button", "sess-del", DEL_SVG));
  return row;
}

/** 完整状态词 —— 只进 `aria-label` 与悬浮提示，行上不写字。
 *  （行上为什么一个字都不写，见 styles.css `.dot.ok` 那段说明：只有异常才落记号。）
 *  认不出来的状态（历史索引里残留的、已经删掉的枚举值）说「已中断」：
 *  它不在 BUSY_STATES 里，没有在跑，说「进行中」是假的；
 *  而直接把 `paused_awaiting_confirmation` 这种内部标识吐给用户看，
 *  与「列表曾经直接显示 slug」是同一类毛病。 */
function stateText(it, st) {
  if (st === "done") return it.passed === false ? "未通过校验" : "已通过校验";
  return STATE_LABEL[st] || "已中断";
}

function updateRow(row, it) {
  const st = it.state || "done";
  const done = settled(st);
  const isCur = done
    ? (!!state.result && it.id === state.result.id)
    : (!!state.job && it.id === state.job.id);
  row.dataset.id = it.id;
  row.classList.toggle("active", isCur);

  const clock = fmtClock(it.created_at);
  // sig 要覆盖行上**看得见**的每一项，漏一项就是「数据变了但那一格不重画」。
  const sig = [isCur, st, it.topic, it.passed, clock, it.duration, it.chars].join("\u0001");
  if (row._sig === sig) return;
  row._sig = sig;

  // 状态点 = **异常记号**：正常完成的记录不画点（.dot.ok 整格不可见，
  // 但仍占 14px 图标盒，标题左边缘不动）。三档：
  //   ok  = 无记号（默认状态不该每行喊一遍）
  //   no  = 红点（失败 / 完成但校验未通过 / 已中断）
  //   run = 灰点呼吸（真在跑）
  // ⚠ 呼吸只给 `BUSY_STATES` 里的状态。此前写的是
  //   `st === "done" ? … : st === "failed" ? "no" : "run"` —— 落到 else 的
  //   除了真正在跑的，还有 **cancelled** 与一切认不出来的旧枚举值，
  //   于是「已取消」的记录挂着一颗永远在呼吸的点，读成「还活着」。
  const dotCls = BUSY_STATES.has(st) ? "run"
    : (st === "done" && it.passed) ? "ok" : "no";
  row.querySelector(".dot").className = "dot " + dotCls;
  const aria = stateText(it, st);
  row.setAttribute("aria-label", `${it.topic || "未命名"}，${aria}`);
  row.querySelector(".sess-topic").textContent = it.topic || "";
  row.querySelector(".sess-time").textContent = clock;
  // 从行上撤掉的字段全部收进悬浮提示：列表这一层只负责「认哪条是哪条」。
  // 行业与平台在这份历史里 100 条恒等（单包 + 九成同平台），摆在行上是噪声，
  // 但换多包 / 多平台时仍然要看得到，所以是**挪走**不是删掉。
  const meta = [packLabel(it.pack), it.platform, fmtStamp(it.created_at), aria]
    .filter(Boolean).join(" · ");
  // 每项都先判 null —— 后端对在跑的作业不给 duration/chars/passed，
  // 硬拼会印出 undefined；filter(Boolean) 把空项整段丢掉，不留空行。
  const tip = [it.topic || "", meta];
  if (done) {
    if (it.duration != null) tip.push(`${it.duration}s`);
    if (it.chars != null) tip.push(`${it.chars} 字`);
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
