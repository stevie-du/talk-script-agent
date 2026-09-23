// XSS 探针组（五路审查 P0-2，2026-09-23）。
//
// util.js 的 `el(tag, cls, html)` 第三参数走 **innerHTML**（参数名就叫 html），
// 所以一切拼进 el() 的外部数据都必须 esc。这一组守三处漏放行中的两处
// 「页面加载后即可判」的：
//   · 搜索无结果提示拼 query（用户输入，搜索框可直接粘 payload）；
//   · 工具条参数胶囊的 option 拼 pack.yaml 的 options（可导入第三方包）。
// 第三处（行业包面板文件名 kb-name）依赖 packinfo 面板已打开，留在
// verify.js 整网里（分组模式下要自己切面板，且整网跑时它已在正确位置）。
//
// 判据统一盯 **DOM 里出了什么**，不盯文本里有没有那几个字符 ——
// esc 之后文本里照样有 `<img`，但 DOM 里不会有真元素。
// CSP 只拦网络请求、不拦事件，onerror 照样能跑，必须靠 esc 兜。
module.exports = async function xss({ evalIn, sleep, check }) {
    // ① 搜索无结果提示：往真实搜索框粘 payload
    const xssSearch = await evalIn(`return (function(){
    var box = document.getElementById('sess-search');
    if (!box) return { noBox: true };
    window.__xssFired = 0;
    window.addEventListener('error', function(){ window.__xssFired++; }, true);
    var payload = '<img src=x onerror="window.__xssFired++">';
    box.value = payload;
    box.dispatchEvent(new Event('input', { bubbles: true }));
    var hint = document.querySelector('#session-list .sess-empty');
    return {
      hintShown: !!hint,
      hintHTML: hint ? hint.innerHTML.slice(0, 120) : '',
      hintText: hint ? hint.textContent.trim() : '',
      injectedTags: hint ? hint.querySelectorAll('img,script,svg,iframe').length : -1,
      fired: window.__xssFired,
    }; })()`);
    check("搜索无结果提示不含注入元素（用户输入走 esc，el() 是 innerHTML）",
      xssSearch.hintShown && xssSearch.injectedTags === 0 && xssSearch.fired === 0
        && xssSearch.hintText.indexOf("<img") >= 0,
      JSON.stringify(xssSearch));
    // 复原：把搜索清掉，否则后续断言看到的是空列表
    await evalIn(`var b = document.getElementById('sess-search');
    b.value = ''; b.dispatchEvent(new Event('input', { bubbles: true })); return true;`);

    // ② 工具条参数胶囊：平台下拉第 4 项是桩里带的 payload option
    //    （pack.yaml 的 options 是可导入的第三方数据）
    const xssPill = await evalIn(`return (function(){
    window.__xssPill = 0;
    var sel = document.querySelector('#quick-params #p-platform');
    if (!sel) return { noSel: true };
    return {
      optCount: sel.options.length,
      imgInSel: sel.querySelectorAll('img,script,svg,iframe').length,
      fired: window.__xssPill,
      hasPayload: Array.prototype.some.call(sel.options,
        function(o){ return o.text.indexOf('<img') >= 0; }),
    }; })()`);
    check("工具条参数胶囊不含注入元素（pack.yaml 的 options 走 esc）",
      xssPill.optCount === 4 && xssPill.imgInSel === 0 && xssPill.fired === 0
        && xssPill.hasPayload,
      JSON.stringify(xssPill));
};
