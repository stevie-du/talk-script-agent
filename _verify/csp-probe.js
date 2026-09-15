// CSP 拦截矩阵探针（零依赖，自带无头 Chrome）。
//
// 跑法：node _verify/csp-probe.js
//
// 回答一个问题：在**当前 Chrome** 上，`style-src 'self'` / `script-src 'self'`
// （不带 'unsafe-inline'）到底拦得住哪几种注入？以及哪些是**不拦**的。
//
// 为什么需要它：`_verify/verify.js` 里的正对照（「故意写一次内联 style，
// 必须被拦下」）依赖「哪个向量真的会被拦」这个事实。这个事实是**浏览器行为**，
// 不是文档能保证的 —— 换 Chrome 版本、换指令组合都可能变。所以把它固化成
// 可复现的探针，而不是只写在注释里。
//
// ⚠ 两个已经踩过的坑（探针第一版就栽在第一个上）：
//   1. `securitypolicyviolation` 是**异步派发**的。注入完立刻读计数会得到 0，
//      于是 6 个向量全报「没拦」，而实际拦了 14 次。
//      → 每个向量之间必须 `await` 一拍。
//   2. 一次违规会派发 **2 条**事件（Chromium 重复上报）。
//      → 断言只能写「增加了」，不能写「恰好 +1」。
"use strict";
const path = require("path");
const os = require("os");
const fs = require("fs");
const { freePort, killTree, launchChrome, waitTarget, connect, serve } =
  require("./lib/cdp");

// 与 app/server.py 的 CSP 保持一致。这里**故意硬编码一份副本**：
// 本探针要测的是「浏览器怎么处理这组指令」，不是「生产配了哪组指令」
// —— 后者由 verify.js / test_server_hardening.py 守。
const CSP = "default-src 'self'; script-src 'self'; style-src 'self'; " +
            "img-src 'self' data:; font-src 'self'; connect-src 'self'; " +
            "object-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'";

const HTML = `<!DOCTYPE html><html><head><meta charset="utf-8">
<script src="/_stub.js"></script></head><body><p>hi</p></body></html>`;

const STUB = `
window.__csp = [];
addEventListener('securitypolicyviolation', e => window.__csp.push(e.violatedDirective));
`;

// 「预期被拦」是给人看的对照，不参与断言 —— 一旦浏览器行为变了，
// 看输出就能立刻发现，而不是靠某个断言突然变红去猜原因。
const VECTORS = [
  ["setAttribute('style')", true,
   `const d=document.createElement('div');d.setAttribute('style','color:red');document.body.appendChild(d);d.remove();`],
  ["innerHTML 带 style 属性", true,
   `const d=document.createElement('div');d.innerHTML='<span style="color:red">x</span>';document.body.appendChild(d);d.remove();`],
  ["insertAdjacentHTML 带 style", true,
   `document.body.insertAdjacentHTML('beforeend','<i style="color:red">y</i>');`],
  ["createElement('style')+textContent", true,
   `const s=document.createElement('style');s.textContent='body{color:red}';document.head.appendChild(s);s.remove();`],
  ["createElement('script')+textContent", true,
   `const s=document.createElement('script');s.textContent='window.__x=1';document.body.appendChild(s);s.remove();`],
  // 这一条**预期不被拦**，而且是承重的：界面里所有动态样式
  // （`el.style.setProperty("--i", i)` 之类）全靠它 ——
  // 这也正是「收紧 style-src 之后界面照常工作」的原因。
  ["CSSOM el.style.setProperty", false,
   `const d=document.createElement('div');d.style.setProperty('color','red');document.body.appendChild(d);d.remove();`],
];

(async () => {
  const PORT = await freePort(8941), CDP_PORT = await freePort(9341);
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "csp-probe-"));
  const dir = path.join(tmp, "r");
  fs.mkdirSync(dir);
  fs.writeFileSync(path.join(dir, "index.html"), HTML);
  const srv = await serve(dir, PORT, { inject: STUB, csp: CSP });
  const chrome = launchChrome(CDP_PORT, path.join(tmp, "prof"));
  let cdp = null;
  try {
    cdp = await connect(await waitTarget(CDP_PORT));
    await cdp.send("Runtime.enable");
    await cdp.send("Page.enable");
    await cdp.send("Page.navigate", { url: `http://127.0.0.1:${PORT}/` });
    await new Promise(r => setTimeout(r, 1500));

    const ev = async (expr) => (await cdp.send("Runtime.evaluate",
      { expression: `(()=>{${expr}})()`, returnByValue: true })).result.value;

    const rows = [];
    let bad = 0;
    for (const [name, expectBlocked, code] of VECTORS) {
      const before = await ev("return window.__csp.length");
      await ev(code + " return 1;");
      await new Promise(r => setTimeout(r, 250));          // ← 坑 1：等事件派发
      const after = await ev("return window.__csp.length");
      const dirs = await ev("return window.__csp.slice(" + before + ")");
      const blocked = after > before;
      const ok = blocked === expectBlocked;
      if (!ok) bad++;
      rows.push({ 向量: name, 预期: expectBlocked ? "拦" : "放行",
                  实测: blocked ? "拦" : "放行",
                  条数: after - before, 指令: [...new Set(dirs)].join(",") });
    }
    console.table(rows);
    console.log(bad === 0
      ? `✅ ${rows.length}/${rows.length} 与预期一致`
      : `❌ ${bad}/${rows.length} 与预期不符 —— 浏览器行为变了，verify.js 的正对照需要重新挑向量`);
    process.exitCode = bad ? 1 : 0;
  } finally {
    try { if (cdp) cdp.close(); } catch (_) { /* ignore */ }
    killTree(chrome.pid);
    srv.close();
    // 删不掉就算了（Windows 上 profile 常被占），别让清理失败盖住结果
    try { fs.rmSync(tmp, { recursive: true, force: true }); } catch (_) { /* ignore */ }
  }
  process.exit(process.exitCode || 0);
})();
