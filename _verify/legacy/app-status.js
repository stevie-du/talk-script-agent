// 只读状态自检：应用是否在跑、引擎端口、关键接口是否正常。
// 与 start-and-check.js 的区别：本脚本不启动任何东西，纯观测（可随时跑）。
"use strict";
const { execSync } = require("child_process");
const opt = { encoding: "utf8" };

const PROJ = "talk-script-agent";

// 1) 本项目相关进程
const psOut = execSync(
  'tasklist /FI "IMAGENAME eq electron.exe" /FO CSV', opt
).split(/\r?\n/).slice(1).filter(l => l.trim());
console.log("electron 进程数:", psOut.length, psOut.length >= 1 ? "（应用在运行）" : "（未运行）");

// 2) python 进程里挑出本项目的引擎
const pyPids = execSync('tasklist /FI "IMAGENAME eq python.exe" /FO CSV', opt)
  .split(/\r?\n/).slice(1).filter(l => l.trim())
  .map(l => (l.split('","')[1] || "").replace(/"/g, "")).filter(Boolean);

const listens = execSync("netstat -ano -p tcp", opt).split(/\r?\n/)
  .filter(l => /LISTENING/.test(l) && /127\.0\.0\.1:/.test(l))
  .filter(l => pyPids.some(p => l.trim().endsWith(p)));

(async () => {
  let found = false;
  for (const h of listens) {
    const port = h.trim().split(/\s+/)[1].split(":").pop();
    try {
      const health = await (await fetch(`http://127.0.0.1:${port}/api/health`)).text();
      if (!health.includes('"ok":true')) continue;
      found = true;
      console.log("引擎端口:", port, health);

      const cfg = await (await fetch(`http://127.0.0.1:${port}/api/config`)).json();
      console.log("配置: model=" + cfg.model +
        "  key=" + (cfg.api_key_set ? "已配置" : "未配置") +
        "  mock=" + cfg.mock);

      const meta = await (await fetch(`http://127.0.0.1:${port}/api/meta`)).json();
      console.log("行业包:", (meta.packs || []).map(p => p.name).join(", ") || "(无)");

      const hist = await (await fetch(`http://127.0.0.1:${port}/api/history`)).json();
      console.log("历史记录:", (Array.isArray(hist) ? hist.length : (hist.items || []).length), "条");
    } catch (_) { /* 非本项目的服务 */ }
  }
  if (!found) console.log("未找到本项目的引擎（应用可能没在运行）");
})();
