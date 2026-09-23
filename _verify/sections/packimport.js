// 技能包导入 UI 这一组断言（方案 docs/技能包系统方案.md §6，2026-09-23）。
//
// 守的是三件事：
//   ① 导入入口在（按钮 + 隐藏的 file input）—— 没有它整个功能在前端不存在；
//   ② 导入来的包**看得见来源**（副标题徽标 + 右列来源行 + 卸载按钮）——
//      平台中立的前提是不隐藏：用户装了什么、许可证是什么，必须一直在眼前；
//   ③ 内置包**不显示**卸载 —— 服务端也会拒，但前端先显示一个必然失败的
//      按钮是误导。
//
// ⚠ 断言逻辑只有这一份：verify.js 整网跑的是同一个函数（与 topics/contrast
//   同一规矩），分组入口只服务于 mutate.py 的快速变异。
module.exports = async function packimport({ evalIn, sleep, check }) {
    // 进设置 → 行业包面板。导入按钮只在这个面板的 headbar 上。
    await evalIn(`document.getElementById('btn-open-settings').click(); return true;`);
    await sleep(400);
    await evalIn(`document.querySelector('.stg-nav-item[data-pane="packinfo"]').click(); return true;`);
    await sleep(500);

    // ① 导入入口：按钮与隐藏的 file input 都必须在 DOM 里。
    const hasBtn = await evalIn(`return !!document.getElementById('pi-import');`);
    check("行业包面板有「导入」按钮（技能包导入入口）", hasBtn);
    const hasInput = await evalIn(
        `return !!document.getElementById('pi-import-file');`);
    check("「导入」配着隐藏的 file input（原生选择器只能由它打开）", hasInput);

    // ② 桩的 META 里有一个 imported 包（见 _verify 的桩数据），它的条目必须带
    //    来源徽标；右列来源行与卸载按钮在选中它时出现。
    //    ⚠ 徽标类名是 .imported —— 没有它，"这个包是别人做的"在界面上不可见，
    //      而方案 §9 红线 4 要求 license 必须展示。
    await evalIn(
        `document.querySelector('#pi-list .pl-item[data-id="imported-pack"]').click(); return true;`);
    await sleep(500);
    const badge = await evalIn(`return (function(){
      const it = document.querySelector('#pi-list .pl-item[data-id="imported-pack"]');
      return !!(it && it.querySelector('.pl-s.imported'));
    })()`);
    check("导入的包在中列带来源徽标（.imported，作者/许可证可见）", badge);
    const srcRow = await evalIn(`return (function(){
      const s = document.getElementById('pi-import-src');
      return !!s && !s.classList.contains('hidden') && /第三方导入/.test(s.textContent || '');
    })()`);
    check("导入的包在右列显示来源行（第三方导入 · 作者 · 许可证）", srcRow);
    const rmShown = await evalIn(
        `return !document.getElementById('pi-remove').classList.contains('hidden');`);
    check("导入的包显示「卸载」按钮", rmShown);

    // ③ 内置包（elevator）不显示卸载、不显示来源行。
    await evalIn(
        `document.querySelector('#pi-list .pl-item[data-id="elevator"]').click(); return true;`);
    await sleep(500);
    const rmHidden = await evalIn(
        `return document.getElementById('pi-remove').classList.contains('hidden');`);
    check("内置包不显示「卸载」（服务端也会拒，前端不摆必然失败的按钮）", rmHidden);
    const noBadge = await evalIn(`return (function(){
      const it = document.querySelector('#pi-list .pl-item[data-id="elevator"]');
      return !!(it && !it.querySelector('.pl-s.imported'));
    })()`);
    check("内置包不带来源徽标", noBadge);
};
