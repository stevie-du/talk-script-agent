// 基础工具：DOM、转义、格式化、提示。
// 拆成模块的第一层：这些函数与业务无关，被其它模块普遍依赖。

export const $ = id => document.getElementById(id);
export const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

/** 建元素。html 一律由调用方保证已转义（默认走 esc）。 */
export function el(tag, cls, html) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (html !== undefined) e.innerHTML = html;
  return e;
}

/** 把「只应绑定一次」的函数包一层幂等守卫：第二次调用直接返回。
 *
 *  为什么需要
 *  ----------
 *  绑定函数里混着两类写法：
 *    · `addEventListener` —— **会重复挂**，一次点击触发两次；
 *    · `onclick =`        —— 天然幂等（后写的覆盖前写的）。
 *  重复调用时后者没事、前者出事，而症状（点一下保存发两次请求、
 *  `appConfirm` 的 Promise resolve 两次）出现的位置离原因很远，很难查。
 *
 *  为什么收成一个函数
 *  ----------------
 *  修复前这里有**三种写法**：`bindShell` 用 `fn._bound`、`bindSessionList`
 *  和它内部的 `bindSearch` 用 `el._bound`、`bindSettings` / `bindOverlays` /
 *  `bindScrollPin` 干脆没有。只补一处正是「同一个口径抄了几份，下次只修一半」
 *  的经典形态。统一到这里之后，`main.js` 的 `__ts.rebind()` 会**同时**再跑一遍
 *  全部绑定，`_verify/verify.js` 断言「没有新增任何监听器」——
 *  漏掉任何一个包装，那条断言就会红。
 */
export function bindOnce(fn) {
  const wrapped = function (...args) {
    if (wrapped._bound) return undefined;
    wrapped._bound = true;
    return fn.apply(this, args);
  };
  return wrapped;
}

/** HTML 转义。
 *  修复前不转义单引号 —— 当前所有动态值都进文本节点或双引号属性，没有可利用路径，
 *  但行业包的 label 等是用户可编辑内容，一旦将来用于单引号属性就会破。补上更省心。 */
export function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

let toastTimer = null;
export function toast(msg, ms = 2200) {
  const t = $("toast");
  if (!t) return;
  t.textContent = msg;
  t.classList.remove("hidden");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.add("hidden"), ms);
}

export async function copyText(text, tip) {
  try {
    await navigator.clipboard.writeText(text);
    toast(tip || "已复制");
    return true;
  } catch (_) {
    toast("复制失败", 1500);
    return false;
  }
}

/** 触发下载。
 *  修复前 createObjectURL 之后从不 revokeObjectURL，每导出一次泄漏一个 Blob。 */
export function download(filename, text, mime = "text/plain;charset=utf-8") {
  const url = URL.createObjectURL(new Blob([text], { type: mime }));
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 0);
}

/** 把 **加粗** 与 {{占位}} 渲染成 HTML（先转义再替换，顺序不能反）。 */
export function fmtText(s) {
  let h = esc(s);
  h = h.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
  h = h.replace(/\{\{([^}]+)\}\}/g, (m, k) => `<span class="over jumpable">{{待补：${k}}}</span>`);
  return h;
}

export function sec(v) {
  return (v === undefined || v === null) ? "" : `${Math.round(v * 10) / 10}s`;
}

export function pad2(n) { return String(n).padStart(2, "0"); }

/** 时刻 HH:MM。
 *
 *  会话行只到「时分」为止，不带日期前缀 —— 行已经被分组标签按天归好类了，
 *  在「昨天」这一组里再写一遍「昨天 15:36」是把分组键重复进每一条记录。
 *  完整时间戳仍在行的 title 里（见 sessions.js 的 updateRow）。
 *
 *  取代原来的 `fmtTime()` + `dayKey()`：那两个函数只有会话行一个调用点，
 *  改成不带日期的时刻后一起删掉，不留「导出了但没人用」。 */
export function fmtClock(d) {
  const x = new Date(d);
  if (isNaN(x)) return "";
  return `${pad2(x.getHours())}:${pad2(x.getMinutes())}`;
}

/** 完整时间戳 `YYYY-MM-DD HH:MM` —— 列表行只放时刻，完整值收进悬浮提示。 */
export function fmtStamp(d) {
  const x = new Date(d);
  if (isNaN(x)) return "";
  return `${x.getFullYear()}-${pad2(x.getMonth() + 1)}-${pad2(x.getDate())} ${fmtClock(x)}`;
}

const WEEKDAY = ["周日", "周一", "周二", "周三", "周四", "周五", "周六"];

/** 会话列表的分组键：`{ id, label }`。
 *
 * 为什么要有它 —— 早先的 `dayKey()` 只分四档（今天/昨天/本周/更早），一周以前的
 * **全部并进「更早」**。真实索引里 78 条记录全落在 9-06~09-12，于是整列只有
 * 一个标签「更早 78」，78 行之间再无分隔 —— 用户原话「全部记录平铺了，
 * 有点太多了」。**分组不是没做，是粒度太粗。**
 *
 * 规则：
 *   今天 / 昨天      → 沿用相对词（这两天用户是按"刚刚/昨天"记的，不是按日期）
 *   2 天及以上       → `M月D日`
 *   ⚠ 这里**不再有「本周」和「更早」**：那两个桶正是问题所在。
 *   ⚠ 日期**必须带中文单位**，不能写成 `09-12`：分组标签后面紧跟的是该组的条数
 *     （`09-12 26`），两个裸数字并排就是读不出哪个是日期、哪个是计数（用户原话）。
 *     `9月12日 26` 里日期由「日」字收尾，计数怎么轻都不会粘上去。
 *   ⚠ 星期（周四）**不进标签**：一行里「日期 + 星期 + 计数」是三段，正是"拥挤"的
 *     来源；它挪进悬浮提示（见 sessions.js 的 updateGroup），要查还在。
 *
 * `id` 与 `label` 分开，是因为两者用途不同、且**不能让 id 参与展示**：
 *   - id 要能**跨天稳定排序**（`2026-09-12` 这种可字典序排），也要当
 *     localStorage 里折叠状态的键 —— 用 label 当键的话，明天「今天」变成
 *     「昨天」、折叠状态就跟着错位了；
 *   - label 只给人看，允许随「今天」推移而变化。
 * 因此 id 用完整 ISO 日期（补零、带年份），label 用中文读法（不补零）。
 */
export function dayGroupKey(d) {
  const x = new Date(d);
  if (isNaN(x)) return { id: "unknown", label: "时间未知", weekday: "" };
  const now = new Date();
  const day = new Date(x.getFullYear(), x.getMonth(), x.getDate());
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const diff = Math.round((today - day) / 86400000);
  const weekday = WEEKDAY[x.getDay()];
  if (diff <= 0) return { id: "today", label: "今天", weekday };
  if (diff === 1) return { id: "yesterday", label: "昨天", weekday };
  const md = `${pad2(x.getMonth() + 1)}-${pad2(x.getDate())}`;
  // 排序用的 id 一定带年份：跨年时「12-31」和「01-02」按 MM-DD 排会反过来。
  const id = `${x.getFullYear()}-${md}`;
  return { id, label: `${x.getMonth() + 1}月${x.getDate()}日`, weekday };
}



export const sleep = ms => new Promise(r => setTimeout(r, ms));
