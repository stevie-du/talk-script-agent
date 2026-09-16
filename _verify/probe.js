// 起一个**真实引擎**（不是桩）+ 无头 Chrome 直连，用来量几何 / 出图 / 跑任意探针。
//
// 为什么需要它
// ------------
// `verify.js` 用的是**桩引擎**（数据是写死的三五个文件），适合守回归，
// 但不适合「照用户那张图复现」—— 真实包有 13 个文件、分组有 5 组，
// 布局问题往往只在真实数据量下才露出来。
// 而桌面应用那条路（`--remote-debugging-port` + CDP）要先重启用户的窗口，
// 还会撞上「窗口最小化时 captureScreenshot 永久挂起」那个坑。
//
// 引擎自己就是 HTTP 服务：**自己拉一个引擎、自己带 token**，用无头 Chrome 直连它，
// 拿到的就是真实数据 + 真实渲染，而且不用碰用户正在用的那个实例。
//
// 用法
// ----
//   node _verify/probe.js [--pane=kb] [--expr='<表达式>'] [--overflow]
//                         [--shot=out.png] [--clip=x,y,w,h[,scale]]
//                         [--size=1305x823] [--dpr=1.25]
//
//   --pane      打开设置并切到该面板（gen / packinfo / llm / kb / skills）
//   --expr      在页面里跑一段**函数体**（多语句要自己写 return），结果以 JSON 打印
//   --overflow  内置探针：找出「内容漏出盒子」的元素（见下）
//   --shot      截整窗图；--clip 可只截一块并放大（scale 默认 1）
//   --size/--dpr  视口尺寸与缩放倍率。**照用户截图复现时这两个必须对上**：
//               先 `png-tools.js scan` 反推出他的倍率与窗口宽度，再在这里填。
//
// `--overflow` 的口径**不在这里写**，在 `_verify/lib/overflow.js` ——
// `verify.js` 里那条「卡片容得下内容」的断言用的是同一个。
// 判据与三个口径上的坑（scrollHeight 假阳性 / 要比外边距盒 /
// 折叠的 `<details>` 要跳过）都在那个文件的注释里，改之前先读它。
//
// 表达式里**不能出现反引号**（`evalIn` 用模板串拼的，会提前截断字符串）。
"use strict";
const fs = require("fs");
const os = require("os");
const path = require("path");
const { spawn } = require("child_process");
const { freePort, killTree, launchChrome, waitTarget, connect } = require("./lib/cdp");
const { probeSource } = require("./lib/overflow");

const ROOT = path.resolve(__dirname, "..");
const PY = path.join(ROOT, ".venv", "Scripts", "python.exe");
const TOKEN = "probe-token";
const sleep = ms => new Promise(r => setTimeout(r, ms));

const argv = process.argv.slice(2);
const opt = (name, dflt) => {
  const hit = argv.find(a => a.startsWith("--" + name + "="));
  return hit ? hit.slice(name.length + 3) : dflt;
};
const flag = name => argv.includes("--" + name);
const PANE = opt("pane", "");
const EXPR = opt("expr", "");
const SHOT = opt("shot", "");
const CLIP = opt("clip", "");
const [VW, VH] = (opt("size", "1305x823")).split("x").map(Number);
const DPR = Number(opt("dpr", "1.25"));

const OVERFLOW = probeSource({ skipClasses: ["kb-body"] });

(async function main() {
  const port = await freePort(6187);
  const eng = spawn(PY, ["-m", "app.server", "--port", String(port), "--root", ROOT,
    "--data-dir", ROOT, "--token", TOKEN, "--version", "0.2.0"],
    { cwd: ROOT, stdio: ["ignore", "pipe", "pipe"], windowsHide: true });
  eng.stdout.on("data", () => {});
  eng.stderr.on("data", d => process.stderr.write("[engine] " + d));
  let up = false;
  for (let i = 0; i < 60; i++) {
    try { if ((await fetch(`http://127.0.0.1:${port}/api/health`)).ok) { up = true; break; } } catch (_) {}
    await sleep(300);
  }
  if (!up) throw new Error("引擎没起来（看 stderr 里的 [engine] 行）");
  console.log(`引擎就绪：http://127.0.0.1:${port}/?token=***`);

  const cdpPort = await freePort(9455);
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "ts-probe-"));
  const chrome = launchChrome(cdpPort, path.join(tmp, "cprof"));
  const cdp = await connect(await waitTarget(cdpPort));
  await cdp.send("Runtime.enable");
  await cdp.send("Page.enable");
  await cdp.send("Emulation.setDeviceMetricsOverride",
    { width: VW, height: VH, deviceScaleFactor: DPR, mobile: false });

  const evalIn = async (expr) => {
    const r = await cdp.send("Runtime.evaluate", {
      expression: "(async () => { " + expr + " })()", awaitPromise: true, returnByValue: true });
    if (r.exceptionDetails) throw new Error(r.exceptionDetails.exception?.description || "eval 失败");
    return r.result.value;
  };

  await cdp.send("Page.navigate", { url: `http://127.0.0.1:${port}/?token=${TOKEN}` });
  await sleep(3500);
  await evalIn("document.getElementById('btn-open-settings').click(); return 1;");
  await sleep(1000);
  if (PANE) {
    await evalIn(`document.querySelector('[data-pane=${PANE}]').click(); return 1;`);
    await sleep(2200);                                    // 面板要异步拉包内容
  }

  if (flag("overflow")) {
    const bad = await evalIn(OVERFLOW);
    console.log("内容溢出：" + (bad.length ? JSON.stringify(bad, null, 1) : "无"));
  }
  if (EXPR) console.log(JSON.stringify(await evalIn(EXPR), null, 1));
  if (SHOT) {
    const args = { format: "png" };
    if (CLIP) {
      const [x, y, w, h, s] = CLIP.split(",").map(Number);
      args.clip = { x, y, width: w, height: h, scale: s || 1 };
    }
    const r = await cdp.send("Page.captureScreenshot", args);
    fs.writeFileSync(path.resolve(ROOT, SHOT), Buffer.from(r.data, "base64"));
    console.log("截图：" + SHOT);
  }

  cdp.close();
  killTree(chrome.pid);
  killTree(eng.pid);
  await sleep(400);
  process.exit(0);
})().catch(e => { console.error("失败：" + e.message); process.exit(1); });
