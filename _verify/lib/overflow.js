// 「内容溢出」探针 —— **全项目只此一份口径**。
//
// 为什么单独抽出来：这个判据本来在 `verify.js`（卡片容得下内容）和 `probe.js`
// （全文档扫描）里各写了一遍。同一个口径抄两份的下场是必然漂移 ——
// 改了一处忘了另一处，于是「回归说没问题」和「探针说有问题」同时成立，
// 谁也不知道该信哪个。这里只留一份，两边都 require 它。
//
// 判据：**子元素的外边距盒底边** 不得超出 **父元素的内容盒底边** ≥ 2px。
//
// 三个口径上的坑，都踩过：
//
// 1. ⚠ **不能用 `scrollHeight > clientHeight`。**
//    视觉隐藏的绝对定位元素（`.segmented .radio` 里的 `<input>`）照样把
//    scrollHeight 撑大 4px —— 那是探针的问题，不是界面的问题。
//
// 2. ⚠ **要比「外边距盒」，不能比 border box。**
//    块流/行盒排的是**外边距盒**。`.linkbtn`（「恢复默认」）就是靠
//    `height:auto` + `margin:-2px 0` 把 20px 的按钮压回 16px 的行盒 ——
//    它的 border box 上下各露 2px，但外边距盒正好 16px，文字中心与标签齐平。
//    按 border box 比就会把它报成溢出，然后你去"修"一个没坏的东西。
//
// 3. ⚠ **折叠的 `<details>` 里的内容要跳过。**
//    Chrome 对 `open=false` 的 details 内容**不绘制、却保留布局盒** ——
//    实测 `.st-adv-body` 在 `detailsH=30` 时仍报 `h=367`、`display:flex`，
//    `display:none` 这一层过滤根本拦不住它。
"use strict";

/**
 * 生成注入到页面里执行的探针源码。
 *
 * @param {object} [opts]
 * @param {string} [opts.selector]  只查这些元素（如 `"#kb-list .kb-item"`）。
 *        不传则扫描 `opts.roots` 下的全部元素。
 * @param {string[]} [opts.roots]   扫描根，默认 `[".stg-pane", "#right", "#left"]`。
 * @param {string[]} [opts.skipClasses] 扫描模式下属名跳过的类名（如 `["kb-body"]`，
 *        那个容器本来就该滚）。
 * @param {string} [opts.nameFn]    生成 `name` 的函数源码，默认 `TAG.首个类名`。
 * @returns {string} 一段可直接 `eval` 的函数体（以 `return bad;` 结尾）。
 */
function probeSource(opts) {
  const o = opts || {};
  const SEL = o.selector ? JSON.stringify(o.selector) : "null";
  const ROOTS = JSON.stringify(o.roots || [".stg-pane", "#right", "#left"]);
  const SKIP = JSON.stringify(o.skipClasses || []);
  const NAME = o.nameFn ||
    "function(n){ return n.tagName + '.' + String(n.className || '').split(' ')[0]; }";

  return `
    var bad = [];
    var SEL = ${SEL}, ROOTS = ${ROOTS}, SKIP = ${SKIP};
    var NAME = ${NAME};
    var nodes = [];
    if (SEL) {
      document.querySelectorAll(SEL).forEach(function (n) { nodes.push(n); });
    } else {
      // ⚠ 必须是 querySelectorAll —— 页面上有 6 个 .stg-pane，
      // 用 querySelector 只会扫到第一个，其余五个面板静默不扫（踩过）。
      ROOTS.forEach(function (r) {
        document.querySelectorAll(r).forEach(function (root) {
          root.querySelectorAll('*').forEach(function (n) {
            if (nodes.indexOf(n) < 0) nodes.push(n);        // 根之间可能重叠，去重
          });
        });
      });
    }
    // 折叠的 <details> 只绘制它的 <summary>，其余内容**不绘制但布局盒还在**
    // （实测 detailsH=30 时 .st-adv-body 仍报 h=367）。所以这类元素既不参与
    // 「被测量」，也不参与「撑高父元素」—— **两处都要跳过**，只写一处漏一处。
    var notPainted = function (el) {
      var d = el.closest('details:not([open])');
      return !!(d && el !== d && el.tagName !== 'SUMMARY');
    };
    nodes.forEach(function (n) {
      var cs = getComputedStyle(n);
      if (cs.display === 'none') return;
      if (!SEL) {
        if (cs.overflow === 'auto' || cs.overflow === 'scroll') return;   // 本来就该滚
        for (var i = 0; i < SKIP.length; i++) if (n.classList.contains(SKIP[i])) return;
      }
      // 定点测量时把它**报出来**而不是默默跳过 —— 静默跳过 = 断言空转（假绿）。
      if (notPainted(n)) { if (SEL) bad.push({ name: NAME(n), collapsed: true }); return; }
      var box = n.getBoundingClientRect();
      // 高度 0 = 这个元素此刻不可见（比如所在面板被切走了）。
      // **显式报出来**，不要默默跳过 —— 跳过的话这条断言就变成空转（假绿）。
      if (!box.height) { if (SEL) bad.push({ name: NAME(n), hidden: true }); return; }
      var contentBottom = box.bottom
        - (parseFloat(cs.paddingBottom) || 0)
        - (parseFloat(cs.borderBottomWidth) || 0);
      var over = 0;
      Array.prototype.forEach.call(n.children, function (c) {
        var ccs = getComputedStyle(c);
        if (ccs.position === 'absolute' || ccs.display === 'none') return;
        if (notPainted(c)) return;
        var mb = c.getBoundingClientRect().bottom + (parseFloat(ccs.marginBottom) || 0);
        over = Math.max(over, mb - contentBottom);
      });
      if (over >= 2) {
        bad.push({ name: NAME(n), h: Math.round(box.height),
                   over: Math.round(over * 10) / 10 });
      }
    });
    return bad;`;
}

module.exports = { probeSource };
