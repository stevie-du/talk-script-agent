// 状态色对比度守卫（§5：--ok/--warn/--bad 对宿主底 ≥4.5，防 token 调浅回退）。
//
// 从 verify.js 搬过来，理由与 topics 组相同：**它能被单独跑**。整网里它排在
// 1283 行，前面只有启动与导航，但为了跑这 1 条断言也要把后面 350 条的前置
// 全走完（127s）。现在 `node _verify/verify.js contrast` 只要几秒。
//
// ⚠ resolvedToken / contrastRatio **由 verify.js 传进来**，不在这里复制一份：
//   它们在整网里还被另外 8 处断言复用，抄一份就是"改了一处另一处静默过期"。
module.exports = async function contrast({ evalIn, sleep, check, resolvedToken, contrastRatio }) {
    {
      // 对**当前主题的实际宿主底**算：verify 页面可能跑在暗色（prefers-color-scheme: dark），
      // 亮色分支的 token 只该对白底、暗色分支只该对暗底。硬编码白底会把暗色分支
      // 误判成不达标（它们本来就不设计给白底）。
      const [okC, warnC, badC, surfaceC] = await Promise.all([
        resolvedToken("--ok"), resolvedToken("--warn"), resolvedToken("--bad"),
        resolvedToken("--surface"),
      ]);
      const ratios = {
        ok: await contrastRatio(okC, surfaceC),
        warn: await contrastRatio(warnC, surfaceC),
        bad: await contrastRatio(badC, surfaceC),
      };
      check("状态色对当前宿主底对比度 ≥4.5（§5，防 token 调浅回退）",
        ratios.ok >= 4.5 && ratios.warn >= 4.5 && ratios.bad >= 4.5,
        JSON.stringify(ratios));
    }
};
