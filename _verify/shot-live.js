// 给「正在运行的桌面应用」截真图 —— 走**无头 Chrome 直连引擎地址**，
// 不依赖桌面窗口是否可见。
//
// 为什么需要它：桌面窗口被最小化 / 被别的窗口挡住时，
// `Page.captureScreenshot` 会**永久挂起**（不是报错），
// `Page.bringToFront` 无效、`Browser.setWindowBounds` 在 Electron 里不存在。
// 而引擎自己就是同源 HTTP 服务，用无头 Chrome 直接访问它的地址照常渲染 ——
// 拿到的还是真实数据（不是 verify.js 那套桩），只是换了个浏览器壳。
//
// 跑法：
//   node _verify/shot-live.js              # 侧栏顶部（未滚动）
//   node _verify/shot-live.js 1            # 滚到第 2 个分组标签处
//   node _verify/shot-live.js 1 my.png     # 自定义输出名
//
// 前置：桌面应用带调试端口启动
//   cd desktop && ./node_modules/.bin/electron . --no-sandbox --remote-debugging-port=9333
//
// 产物落在 _verify/ 下（*.png 已被 .gitignore 忽略，属于本地查看用）。
"use strict";
const fs = require("fs");
const os = require("os");
const path = require("path");
const { freePort, killTree, launchChrome, waitTarget, connect } = require("./lib/cdp");

const APP_CDP = 9333;
const groupIdx = Number(process.argv[2] || 0);
const outName = process.argv[3] || `_live_group${groupIdx}.png`;
const CLIP = { x: 0, y: 96, width: 280, height: 300, scale: 2 };

const sleep = ms => new Promise(r => setTimeout(r, ms));

(async function main() {
  // 1) 从运行中的应用问出引擎地址（带一次性令牌；复用是有效的）
  const targets = await (await fetch(`http://127.0.0.1:${APP_CDP}/json/list`)).json();
  const app = targets.find(t => t.type === "page" && t.webSocketDebuggerUrl);
  if (!app) throw new Error("应用没有可用的 page target（带 --remote-debugging-port 启动了吗？）");
  const appCdp = await connect(app.webSocketDebuggerUrl);
  await appCdp.send("Runtime.enable");
  const url = (await appCdp.send("Runtime.evaluate", {
    expression: "location.href", returnByValue: true,
  })).result.value;
  appCdp.close();
  console.log("引擎地址:", url.replace(/token=[^&]+/, "token=***"));

  // 2) 无头 Chrome 直连
  const cdpPort = await freePort(9444);
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "ts-shot-"));
  const chrome = launchChrome(cdpPort, path.join(tmp, "cprof"));
  const cdp = await connect(await waitTarget(cdpPort));
  await cdp.send("Runtime.enable");
  await cdp.send("Page.enable");
  await cdp.send("Page.navigate", { url });
  await sleep(4000);

  const evalIn = async (expr) => {
    const r = await cdp.send("Runtime.evaluate", {
      expression: "(async () => { " + expr + " })()",
      awaitPromise: true, returnByValue: true,
    });
    if (r.exceptionDetails) throw new Error(r.exceptionDetails.exception?.description || "eval 失败");
    return r.result.value;
  };

  // 刚导航完 loadSessions() 还没回来，量到的是空列表 —— 必须等
  let rows = 0;
  for (let i = 0; i < 30; i++) {
    rows = await evalIn("return document.querySelectorAll('#session-list .sess-item').length;");
    if (rows > 0) break;
    await sleep(500);
  }
  console.log("会话条数:", rows);

  // 3) 定位：把第 groupIdx 个分组标签放到容器顶部下方 120px 处
  const pos = await evalIn(
    "const lbls = document.querySelectorAll('.group-lbl');" +
    "const sc = document.querySelector('.left-scroll');" +
    "const i = " + groupIdx + ";" +
    "if (i > 0 && lbls.length <= i) return { err: '只有 ' + lbls.length + ' 个分组标签' };" +
    "if (i > 0) sc.scrollTop = sc.scrollTop + lbls[i].getBoundingClientRect().top" +
    "  - sc.getBoundingClientRect().top - 120;" +
    "return { 分组数: lbls.length, 定位到: lbls[i] ? lbls[i].textContent : null," +
    "  scrollTop: Math.round(sc.scrollTop) };");
  console.log("定位:", JSON.stringify(pos));
  await sleep(400);

  const shot = await cdp.send("Page.captureScreenshot", { format: "png", clip: CLIP });
  const out = path.join(__dirname, outName);
  fs.writeFileSync(out, Buffer.from(shot.data, "base64"));
  console.log("shot ->", out);

  cdp.close();
  killTree(chrome.pid);
  try { fs.rmSync(tmp, { recursive: true, force: true }); } catch (_) { /* ignore */ }
  process.exit(0);
})().catch(e => { console.error("FAIL:", e.message); process.exit(1); });
