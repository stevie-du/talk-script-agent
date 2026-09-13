// 真实端到端：真引擎（mock 模式）+ 真页面，走完「生成 → 出稿 → 导出」。
//
// 与 verify.js 的区别：verify.js 用桩 fetch 快速覆盖交互分支；本脚本连的是
// 真的 Python 引擎，覆盖「同源加载 + 令牌 + 真落盘 + 真历史索引」这条链路 ——
// 桩永远测不出「引擎没返回 job_id」「静态资源 404」「token 对不上」这类问题。
//
// 跑法：node _verify/e2e-live.js
"use strict";
const path = require("path");
const fs = require("fs");
const os = require("os");
const { spawn } = require("child_process");
const { freePort, killTree, launchChrome, waitTarget, connect } =
  require("./lib/cdp");

const ROOT = path.resolve(__dirname, "..");
const PY = path.join(ROOT, ".venv", "Scripts", "python.exe");
const sleep = ms => new Promise(r => setTimeout(r, ms));

let engine = null, chromeProc = null, cdp = null, _tmp = "";

function cleanup() {
  try { if (cdp) cdp.close(); } catch (_) { /* ignore */ }
  killTree(chromeProc && chromeProc.pid);
  if (engine) { try { engine.kill(); } catch (_) { /* ignore */ } }
  if (_tmp) { try { fs.rmSync(_tmp, { recursive: true, force: true }); } catch (_) { /* ignore */ } }
}
process.on("exit", cleanup);
process.on("SIGINT", () => { cleanup(); process.exit(130); });

const results = [];
const check = (n, ok, d) => results.push([n, !!ok, d || ""]);

(async function main() {
  const port = await freePort(8977);
  const cdpPort = await freePort(9377);
  const token = "e2e-token-" + Date.now();
  _tmp = fs.mkdtempSync(path.join(os.tmpdir(), "ts-e2e-"));
  const dataDir = path.join(_tmp, "data");
  fs.mkdirSync(dataDir, { recursive: true });

  engine = spawn(PY, ["-m", "app.server", "--port", String(port),
                      "--root", ROOT, "--data-dir", dataDir, "--token", token], {
    cwd: ROOT,
    env: { ...process.env, TALKSCRIPT_MOCK: "1", PYTHONIOENCODING: "utf-8" },
    stdio: ["ignore", "pipe", "pipe"],
  });
  engine.stderr.on("data", d => { if (process.env.VERBOSE) console.error("[engine]", String(d).trim()); });

  // 等健康检查
  const deadline = Date.now() + 30000;
  let up = false;
  while (Date.now() < deadline) {
    try {
      const r = await fetch(`http://127.0.0.1:${port}/api/health`);
      if (r.ok) { up = true; break; }
    } catch (_) { /* 还没起来 */ }
    await sleep(300);
  }
  check("引擎启动并通过健康检查", up, `port=${port}`);
  if (!up) throw new Error("引擎未启动");

  chromeProc = launchChrome(cdpPort, path.join(_tmp, "cprof"));
  cdp = await connect(await waitTarget(cdpPort));
  const errs = [];
  cdp.on(o => {
    if (o.method === "Runtime.exceptionThrown") {
      errs.push(o.params.exceptionDetails?.exception?.description || "exception");
    }
  });
  await cdp.send("Runtime.enable");
  await cdp.send("Page.enable");
  await cdp.send("Page.navigate", { url: `http://127.0.0.1:${port}/?token=${token}` });
  await sleep(2000);

  const evalIn = async (expr) => {
    const r = await cdp.send("Runtime.evaluate", {
      // 用 async 包裹：页面里可能 await（如 loadSessions），普通箭头函数会 SyntaxError
      expression: `(async () => { ${expr} })()`, awaitPromise: true, returnByValue: true,
    });
    if (r.exceptionDetails) throw new Error(r.exceptionDetails.exception?.description || "eval 失败");
    return r.result.value;
  };

  const boot = await evalIn(`return { ts: !!window.__ts, packs: window.__ts?.meta?.packs?.length,
    mock: window.__ts?.meta?.mock };`);
  check("真页面从引擎同源加载成功", boot.ts === true, JSON.stringify(boot));
  check("meta 来自真引擎且处于 mock 模式", boot.packs >= 1 && boot.mock === true, JSON.stringify(boot));

  // 真实生成（mock 夹具，不调模型）
  await evalIn(`const t = document.getElementById('topic');
    t.value = '真实端到端：电梯困人怎么办';
    t.dispatchEvent(new Event('input', {bubbles:true}));
    document.getElementById('btn-generate').click(); return true;`);
  await sleep(1500);
  const mid = await evalIn(`return { jobId: window.__ts.jobId };`);
  // mock 夹具几乎是瞬时返回，这里只断言「作业已被受理」（busy 可能已经翻过去了）
  check("真实作业已受理", !!mid.jobId, JSON.stringify(mid));

  // 等落盘（mock 很快，给足余量）
  let done = null;
  for (let i = 0; i < 40; i++) {
    await sleep(700);
    done = await evalIn(`return { busy: window.__ts.busy, hasResult: !!window.__ts.result,
      cards: document.querySelectorAll('.script-card').length,
      state: window.__ts.job?.state || null };`);
    if (!done.busy && done.hasResult) break;
  }
  check("真实生成完成并渲染结果", done.hasResult && done.cards >= 3, JSON.stringify(done));
  check("全流程无 JS 异常", errs.length === 0, errs.slice(0, 2).join(" | "));

  // 产物真的落到了 data-dir
  const genDir = path.join(dataDir, "generated");
  const found = fs.existsSync(genDir)
    && fs.readdirSync(genDir, { recursive: true })
      .filter(f => String(f).endsWith("result.json")).length > 0;
  check("产物落盘到 --data-dir 下的 generated/", found, genDir);
  const idx = path.join(genDir, "index.json");
  check("历史索引已生成", fs.existsSync(idx), idx);

  // 历史列表能看到这条
  const hist = await evalIn(`await window.__ts.loadSessions();
    return document.querySelectorAll('#session-list .sess-item').length;`);
  check("左栏会话列表出现该记录", hist >= 1, `rows=${hist}`);

  // 导出 SRT / MD（下载走 Blob，这里只验证函数路径不抛错）
  const exports_ok = await evalIn(`
    const r = window.__ts.result;
    if (!r) return false;
    document.querySelector('[data-act="save-srt"]')?.click();
    document.querySelector('[data-act="save-md"]')?.click();
    return true;`);
  check("导出 SRT / MD 不抛错", exports_ok === true, "");

  console.log("\n════════ 真实端到端结果 ════════");
  let pass = 0;
  for (const [n, ok, d] of results) {
    console.log(`${ok ? "✅" : "❌"}  ${n}${d ? "  — " + d : ""}`);
    if (ok) pass++;
  }
  console.log(`\n${pass}/${results.length} 通过`);
  cleanup();
  process.exit(pass === results.length ? 0 : 1);
})().catch(e => {
  console.error("FAIL:", e.message);
  for (const [n, ok, d] of results) console.error(`${ok ? "✅" : "❌"}  ${n}${d ? " — " + d : ""}`);
  console.error(e.stack);
  cleanup();
  process.exit(2);
});
