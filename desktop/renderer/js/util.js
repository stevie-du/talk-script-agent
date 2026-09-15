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

/** 时间显示：今天 HH:MM / 昨天 HH:MM / MM-DD HH:MM */
export function fmtTime(d) {
  const x = new Date(d);
  if (isNaN(x)) return "";
  const hm = `${pad2(x.getHours())}:${pad2(x.getMinutes())}`;
  const k = dayKey(d);
  if (k === "今天") return hm;
  if (k === "昨天") return "昨天 " + hm;
  return `${pad2(x.getMonth() + 1)}-${pad2(x.getDate())} ${hm}`;
}

export function dayKey(d) {
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

export function escapeReg(s) { return String(s).replace(/[.*+?^${}()|[\]\\]/g, "\\$&"); }

export const sleep = ms => new Promise(r => setTimeout(r, ms));
