// 左栏会话列表：按天分组（可折叠），增量更新。
//
// 增量更新这条必须保留：修复前每次刷新都整块 innerHTML 重画，鼠标按下与松开
// 之间只要发生一次重画，那一行连同删除按钮就被换成新 DOM，click 合成不出来 ——
// 表现就是「删除按钮要点好几次才中」。现在同一条记录永远复用同一个节点。
//
// 分组之前是「今天 / 昨天 / 本周 / 更早」四档，问题出在最后一档：一周以前的
// **全部并进「更早」**。真实数据里 78 条记录全落在同一周以前，整列只有一个
// 标签「更早 78」—— 分组等于没做，用户原话「全部记录平铺了，有点太多了」。
// 现在换成 `dayGroupKey()`（见 util.js）：今天 / 昨天 / `9月12日`（星期进提示）。
// 每组前面一枚文件夹图标：展开=开口、折叠=合口；行右侧另有「…」菜单，
// 可以一次删掉整组（删前必确认，见 deleteGroup）。
//
// 行是**单行制**（状态记号 + 标题 + 时刻）：一屏能看的条数翻倍，
// 而行业 / 平台 / 时长 / 字数 / 完整状态这些行上放不下的，全部进 title 悬浮提示，
// 点开记录后右栏头部也照样给全（result.js）。
// 记号只标异常：正常完成的记录不画点 —— 一列里绝大多数都是「正常完成」，
// 每条都落的记号等于没有记号（对照 Claude / Linear 的侧栏）。
// 失败记录能点开看原因 —— 后端会把 job.json 摘要返回给 /api/history/{id}。

import { $, el, esc, fmtClock, fmtStamp, dayGroupKey, toast, bindOnce } from "./util.js";
import { api } from "./api.js";
import { state, setResult, detachJob } from "./store.js";
import { setLeftFolded, syncBusyAffordance } from "./ui.js";
import { attach, openRecord } from "./jobs.js";
import { STATE_LABEL, BUSY_STATES } from "./progress.js";
import { appConfirm } from "./overlays.js";

const index = new Map();      // id -> 该行最近一次数据（委托 handler 从这里取，闭包不会过期）
/** gid -> { label, count, items }：分组菜单要按组删，得知道这一组是哪几条。
 *  每次 paint() 重建 —— 与 index 同一个道理：委托事件里不能拿闭包旧数据。 */
const groups = new Map();
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

/* 分组图标：文件夹，**开 / 合两个字形**（参考 Qoder 侧栏的项目行）。
   展开 = 开口，折叠 = 合口 —— 文件树里用了几十年的那套读法。
   ⚠ 换图标不能把折叠信号换没：上一轮的结论是「折叠态只由图标表达，
   不给文字换色」（把折起来的「今天」提亮，读成"这行被选中"而不是"这组收着"）。
   所以这里是**两个真的不同路径**，不是同一个图标转个角度 —— 旋转一个文件夹
   读不出开合，只会变成歪的。
   ⚠ 尺寸：字形 14、盒子 16（CSS 的 .gl-folder 给），与左栏那一列的图标盒同宽；
   线宽 1.6 由 styles.css 顶部那条 svg 全局规则收口（规范 §4）。
   此前这里是折叠箭头（展开朝下、收起朝右）。更早用 CSS 边框画三角，
   静止态朝右、折叠转成朝下，方向与所有分组列表的惯例相反。 */
const FOLDER_BASE = `viewBox="0 0 24 24" width="14" height="14" fill="none"
  stroke="currentColor" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"`;
const FOLDER_CLOSED = `<svg class="gl-folder" ${FOLDER_BASE}>
  <path d="M3.2 18.2V6.8c0-.9.7-1.6 1.6-1.6h3.9c.5 0 1 .2 1.3.6l1.2 1.5h7.9c.9 0 1.7.7 1.7 1.6v9.3c0 .9-.8 1.6-1.7 1.6H4.8c-.9 0-1.6-.7-1.6-1.6Z"/></svg>`;
const FOLDER_OPEN = `<svg class="gl-folder" ${FOLDER_BASE}>
  <path d="M3.2 16.4V6.8c0-.9.7-1.6 1.6-1.6h3.9c.5 0 1 .2 1.3.6l1.2 1.5h7.2c.9 0 1.6.7 1.6 1.5v1.8"/>
  <path d="M2.4 20.4 5 13.2c.2-.6.8-1 1.5-1h14.9c1 0 1.7 1 1.3 1.9l-2.4 6.3c-.2.6-.8 1-1.5 1H3.9c-1 0-1.7-1-1.5-2Z"/></svg>`;
/* 分组行的「更多」入口：省略号，悬停才现身（与会话行的删除键同一画法）。 */
const MORE_SVG = `<svg viewBox="0 0 24 24" width="14" height="14" fill="currentColor" aria-hidden="true">
  <circle cx="5.5" cy="12" r="1.6"/><circle cx="12" cy="12" r="1.6"/><circle cx="18.5" cy="12" r="1.6"/></svg>`;

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

/** 当前仍在后台跑的记录（新 → 旧）。
 *  「几条在跑 / 谁在跑」的**唯一**来源：后端没有 GET /api/jobs 列表端点，
 *  在跑的作业是 /api/history 把内存快照并进摘要里给出的（app/server.py 的 history()）。
 *  排除掉当前正看着的那条 —— 那一条界面上已经有进度与「停止」键，不该再算「后台」。 */
export function busyRecords() {
  return allItems.filter(it => BUSY_STATES.has(it.state || ""));
}

function paint(items) {
  const list = $("session-list");
  Array.from(list.children).forEach(n => { if (!n.dataset.key) n.remove(); });

  const shown = (items || []).filter(matches);
  if (query && !shown.length) {
    list.innerHTML = "";
    // ⚠ query 是用户输入，el() 的第三参数走 innerHTML —— 不 esc 就是 XSS：
    //   往搜索框粘 <img src=x onerror=...> 即执行（五路审查 P0-2）。
    list.appendChild(el("p", "hint sess-empty", `没有匹配「${esc(query)}」的会话`));
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
    if (!grouping.has(g.id)) grouping.set(g.id, { id: g.id, label: g.label, weekday: g.weekday, arr: [] });
    grouping.get(g.id).arr.push(it);
  }

  const specs = [];
  groups.clear();
  for (const g of grouping.values()) {
    specs.push({ kind: "grp", key: "g:" + g.id, id: g.id, label: g.label,
                 count: g.arr.length, isFolded: !query && folded.has(g.id),
                 weekday: g.weekday, items: g.arr });
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
  applyRovingTabindex();
  // 「几条还在后台跑」是这张表算出来的，读数就跟着这张表更新（见 ui.js 的 syncBusyAffordance）。
  syncBusyAffordance();
}

/** 分组标签：可点击折叠 + 一个「更多」菜单（删整组，以后还有重命名等）。
 *  标签用 <button> 而不是 <div> 才有键盘可达性（Tab 能到、Enter/Space 能触发）
 *  与读屏语义；`aria-expanded` 是这类「点一下展开收起」控件的标准信号。
 *  ⚠ 结构必须是**外层 div 套两个按钮**：「更多」是按钮，而 HTML 不允许
 *  按钮套按钮（嵌套的 <button> 会被解析器直接拆出去，实测点不到）。
 *  外层 `.grp-row` 只做定位参照，胶囊底色仍然画在 `.group-lbl` 上 ——
 *  左栏那一列的几何判据量的是胶囊，不是这层壳。 */
function buildGroup() {
  const wrap = el("div", "grp-row");
  wrap.innerHTML = `<button type="button" class="group-lbl" aria-expanded="true">
      ${FOLDER_OPEN}${FOLDER_CLOSED}<span class="gl-t"></span><span class="count"></span>
    </button>
    <button type="button" class="grp-more" aria-haspopup="menu" aria-expanded="false"
      title="这一组的更多操作">${MORE_SVG}</button>
    <div class="grp-menu hidden" role="menu">
      <button type="button" class="grp-menu-item danger" role="menuitem">
        ${DEL_SVG}<span class="gm-t"></span></button>
    </div>`;
  return wrap;
}

function updateGroup(n, sp) {
  const lbl = n.querySelector(".group-lbl");
  // 星期从标签上挪到这里：一行三段（日期 + 星期 + 计数）太挤，
  // 但「上周四那次」这种查法还得走得通 —— 悬停与确认框给全名。
  const long = sp.weekday ? `${sp.label} ${sp.weekday}` : sp.label;
  // ⚠ 这张表必须在签名早退**之前**写：groups 每次重画都先 clear()，
  //  放在早退之后，凡是这一轮没变化的分组就会从表里消失 —— 而 deleteGroup
  //  的守卫是 `if (!g) return`，于是「删除这一组」会**静默什么都不做**。
  //  轮询每 3 秒重画一次，只要有一条记录动了，其余分组就全进了那个洞。
  groups.set(sp.id, { label: long, count: sp.count, items: sp.items });
  const sig = sp.label + "|" + sp.count + "|" + sp.isFolded + "|" + sp.weekday;
  if (n._sig === sig) return;
  n._sig = sig;
  lbl.classList.toggle("folded", sp.isFolded);
  // aria-expanded 与视觉同源，避免「看着折了、读屏说展开了」
  lbl.setAttribute("aria-expanded", sp.isFolded ? "false" : "true");
  lbl.dataset.gid = sp.id;
  n.dataset.gid = sp.id;
  lbl.title = sp.isFolded ? `展开「${long}」的 ${sp.count} 条` : `收起「${long}」`;
  lbl.querySelector(".gl-t").textContent = sp.label;
  lbl.querySelector(".count").textContent = sp.count;
  // 菜单项把条数写进字里： destructive 操作必须让人在点之前就知道会没掉几条。
  n.querySelector(".gm-t").textContent = `删除这一组（${sp.count} 条）`;
  n.querySelector(".grp-more").title = `「${long}」的操作`;
}

/** 一行 = 状态记号 + 标题 + 时刻，三项排在同一条基线上。
 *  状态点占一个 16px 图标盒（与分组箭头的盒同宽，也是左栏那一列的图标盒尺寸），
 *  于是标题文字的左边缘 = 分组标签文字的左边缘 = 搜索框/新建/设置的文字左边缘
 *  —— 旧的「标题 + 副标题」两行式里，副标题从 20px 起、标题从 34px 起，
 *  同一行内两个左边缘，读起来就是「没对齐」。
 *  行上没有状态字：只有异常才落记号，理由见 updateRow 里那张三档表。
 *  ⚠ 焦点：行与行内的删除键都**不进** Tab 序（tabIndex=-1），整列只留一个停靠点，
 *  方向键在行之间走 —— 见下面 applyRovingTabindex 的说明。 */
function buildRow() {
  const row = el("div", "sess-item");
  row.setAttribute("role", "button");
  row.tabIndex = -1;
  row.innerHTML = `<span class="dot"></span><span class="sess-topic"></span>`
    + `<span class="sess-time"></span>`;
  const del = el("button", "sess-del", DEL_SVG);
  del.tabIndex = -1;             // 鼠标仍然悬停可见；键盘走 Delete 键（见 bindSessionList）
  row.appendChild(del);
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
  // 当前那条就是键盘停靠点（Tab 从正文回到左栏时，落在用户认识的那一行上）
  if (isCur) rovingId = it.id;

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

/** ── 键盘：整列只有一个 Tab 停靠点（roving tabindex）────────────
 *
 *  为什么必须做：一列 100 条历史记录，原来**每行**是一个 tabIndex=0 的 role=button，
 *  行里还各带一个删除按钮 —— 也就是 200+ 个 Tab 停靠点。键盘用户按 Tab 走进会话栏
 *  就出不来了（要按两百多次才到得了正文），读屏用户的 Tab 序同样被淹没。
 *  标准做法（WAI-ARIA Authoring Practices 的 list / grid 都是这一套）：
 *  Tab 只停**一个**点，↑↓/Home/End 在行之间走，Enter 打开、Delete 删。
 *  ⚠ 删除键没有从键盘上消失：焦点在某一行时按 Delete/Backspace 就是删那一行
 *     （走的仍是 appConfirm 二次确认，与鼠标点删除完全同一条路）。
 *  ⚠ 停靠点选「当前正在看的那条」，没有就在第一条 —— 从正文 Tab 回来时，
 *     焦点落在用户认识的那一行上，而不是列表开头。 */
let rovingId = "";

function rowNodes() {
  return Array.from($("session-list").querySelectorAll(".sess-item"));
}

function applyRovingTabindex() {
  const rows = rowNodes();
  if (!rows.length) { rovingId = ""; return; }
  const cur = rows.find(r => r.dataset.id === rovingId)
    || rows.find(r => r.classList.contains("active"))
    || rows[0];
  rovingId = cur.dataset.id;
  rows.forEach(r => { r.tabIndex = r === cur ? 0 : -1; });
}

function moveRoving(delta) {
  const rows = rowNodes();
  if (!rows.length) return;
  const i = Math.max(0, rows.findIndex(r => r.dataset.id === rovingId));
  const next = rows[Math.min(rows.length - 1, Math.max(0, i + delta))];
  if (!next) return;
  rows.forEach(r => { r.tabIndex = r === next ? 0 : -1; });
  rovingId = next.dataset.id;
  next.focus();
  next.scrollIntoView({ block: "nearest" });
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
  // 提示写在容器上、不逐行重复：一百行都念一遍「按 Delete 删除」是噪声。
  list.setAttribute("aria-label", "会话记录：方向键选择，Enter 打开，Delete 删除所选");
  const activate = (row) => {
    const it = index.get(row.dataset.id);
    if (!it) return;
    // 分流判据是「它还在跑吗」，**不是**「它的 state 是不是 done」。
    // 修前写的是 `state === "done" ? openRecord : attach`：failed / cancelled
    // 以及索引里残留的旧状态全被送去 attach() —— 那里读的是**内存里的作业**，
    // 应用重启后必然 404，于是磁盘上明明有 job.json 摘要（含失败原因），
    // 界面上只留下一条 3.5 秒的「无法回到这次生成」，jobs.js 里那张
    // 「这次失败了 · 用同样参数重来」的卡片从此没人能画出来（死代码）。
    // 现在：在跑的（含排队）才值得接轮询，其余一律回读落盘的记录。
    BUSY_STATES.has(it.state || "") ? attach(it.id) : openRecord(it.id);
  };
  list.addEventListener("click", ev => {
    // 「更多」与它的菜单优先：两者都 stopPropagation，避免下面那句
    // 「点别处就关菜单」把它们刚打开的菜单当场关掉（同一趟冒泡里就关了）。
    const more = ev.target.closest(".grp-more");
    if (more) {
      ev.stopPropagation();
      const menu = more.closest(".grp-row").querySelector(".grp-menu");
      const willOpen = menu.classList.contains("hidden");
      closeGroupMenus();
      if (willOpen) {
        menu.classList.remove("hidden");
        more.setAttribute("aria-expanded", "true");
        menu.querySelector(".grp-menu-item").focus();
      }
      return;
    }
    const item = ev.target.closest(".grp-menu-item");
    if (item) {
      ev.stopPropagation();
      deleteGroup(item.closest(".grp-row").dataset.gid);
      return;
    }
    closeGroupMenus();
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
    if (ev.key === "Escape") { closeGroupMenus(); return; }
    // <button> 的 Enter/Space 本来就会派发 click，这里不要再处理一遍，
    // 否则一次按键折叠两次 = 看起来「点了没反应」。
    if (ev.target.closest(".group-lbl") || ev.target.closest(".grp-more")
        || ev.target.closest(".grp-menu-item")) return;
    const row = ev.target.closest(".sess-item");
    if (!row) return;
    // 方向键在行之间走（整列只有一个 Tab 停靠点，见 applyRovingTabindex 的说明）
    if (ev.key === "ArrowDown" || ev.key === "ArrowUp") {
      ev.preventDefault();
      moveRoving(ev.key === "ArrowDown" ? 1 : -1);
      return;
    }
    if (ev.key === "Home" || ev.key === "End") {
      ev.preventDefault();
      const rows = rowNodes();
      const n = ev.key === "Home" ? rows[0] : rows[rows.length - 1];
      if (n) { rows.forEach(r => { r.tabIndex = r === n ? 0 : -1; });
               rovingId = n.dataset.id; n.focus(); n.scrollIntoView({ block: "nearest" }); }
      return;
    }
    // 删除键从行上按：行内的删除按钮不在 Tab 序里（否则一百条就是两百个停靠点），
    // 键盘用户要能删，就得有一条等价路径。走的仍是同一个 deleteSession + 二次确认。
    if (ev.key === "Delete" || ev.key === "Backspace") {
      ev.preventDefault();
      const it = index.get(row.dataset.id);
      if (!it) return;
      const del = row.querySelector(".sess-del");
      if (del) { del.focus(); del.click(); }
      return;
    }
    if (ev.key !== "Enter" && ev.key !== " ") return;
    ev.preventDefault();
    activate(row);
  });
  // 菜单开着时点到列表外（正文、输入框、设置页）就该收起来 —— 与下拉菜单同一条规矩。
  if (!window.__grpMenuOutsideBound) {
    window.__grpMenuOutsideBound = true;
    document.addEventListener("click", closeGroupMenus);
  }
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

function closeGroupMenus() {
  document.querySelectorAll("#session-list .grp-menu:not(.hidden)").forEach(m => {
    m.classList.add("hidden");
    const more = m.closest(".grp-row").querySelector(".grp-more");
    if (more) more.setAttribute("aria-expanded", "false");
  });
}

/** 删掉整个分组：逐条走与单条删除**同一个** DELETE。
 *  后端没有批量入口，而 `pipeline.discard` 自己会顺带停掉在跑的后台作业，
 *  所以「这一组里有正在生成的」不需要特殊分支 —— 同一条路，只是走 N 次。
 *  ⚠ 必须先确认：一次抹掉 N 条记录，比删一条重得多，而它不可恢复。
 *  ⚠ 部分失败要如实报（后端对「文件正被占用」是明确报错而不是假装成功）：
 *    报"已删除 N 条"而列表里还留着几条，等于骗用户。 */
async function deleteGroup(gid) {
  closeGroupMenus();
  const g = groups.get(gid);
  if (!g || !g.items.length) return;
  const ok = await appConfirm("删除整个分组",
    `「${g.label}」的 ${g.items.length} 条记录会被删除，不可恢复。`);
  if (!ok) return;
  const ids = g.items.map(it => it.id);
  const failed = [];
  for (const id of ids) {
    try { await api.removeRecord(id); } catch (_) { failed.push(id); }
  }
  // 当前开着的那条 / 正在跑的那条如果被抹掉了，界面要跟着松手，
  // 否则会留着一块指向已删记录的正文（与 deleteSession 同一套）。
  if (state.job && ids.includes(state.job.id)) detachJob();
  if (state.result && ids.includes(state.result.id)) setResult(null);
  const n = ids.length - failed.length;
  toast(failed.length
    ? `已删除 ${n} 条，${failed.length} 条未删成（文件可能正被占用），可稍后重试`
    : `已删除「${g.label}」的 ${n} 条记录`, 4200);
  loadSessions();
}
