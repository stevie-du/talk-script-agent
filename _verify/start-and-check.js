// 一键：启动 TalkScript → 等就绪 → 验证进程树/引擎端口/健康接口/元信息
// 用途：在本环境里 GUI 进程无法跨工具调用存活，故必须"启动+验证"在同一次调用内完成。
const path = require("path");
const fs = require("fs");
const net = require("net");
const { execSync } = require("child_process");

const wait = ms => new Promise(r => setTimeout(r, ms));
const root = path.resolve(__dirname, "..");
const desktop = path.join(root, "desktop");
const electron = path.join(desktop, "node_modules", "electron", "dist", "electron.exe");
const logFile = path.join(root, "_verify", "_app.log");

function run(c) { try { return execSync(c, { encoding: "latin1", stdio: ["ignore", "pipe", "ignore"] }); } catch (_) { return ""; } }
function procs(name) {
  const o = run(`tasklist /FI "IMAGENAME eq ${name}" /FO CSV`);
  return o.split(/\r?\n/).filter(l => new RegExp(name, "i").test(l) && l.includes('"'))
    .map(l => l.split('","')).map(c => c[1]).filter(Boolean);
}
function listenLines() {
  const out = run("netstat -ano -p TCP");
  const m = [...out.matchAll(/127\.0\.0\.1:(\d+)\s+\S+\s+LISTENING\s+(\d+)/gi)];
  return m.map(x => ({ port: +x[1], pid: x[2] }));
}
async function get(port, p, timeout = 3000) {
  try {
    const c = new AbortController(); const t = setTimeout(() => c.abort(), timeout);
    const r = await fetch(`http://127.0.0.1:${port}${p}`, { signal: c.signal });
    clearTimeout(t);
    const txt = await r.text();
    let json = null; try { json = JSON.parse(txt); } catch (_) {}
    return { status: r.status, json, txt: txt.slice(0, 120) };
  } catch (e) { return { err: String(e.message).slice(0, 50) }; }
}

(async () => {
  console.log("── 1) 启动 ──");
  const env = { ...process.env };
  delete env.ELECTRON_RUN_AS_NODE;                    // 宿主注入会让 electron 退化成纯 Node
  const bat = path.join(__dirname, "_start.bat");
  fs.writeFileSync(bat, [
    "@echo off", "set ELECTRON_RUN_AS_NODE=", 'cd /d "' + desktop + '"',
    'start "" "' + electron + '" . --disable-gpu --disable-gpu-compositing --no-sandbox >> "' + logFile + '" 2>&1',
  ].join("\r\n"), "latin1");
  try { execSync('cmd /c "' + bat + '"', { env, stdio: "ignore", timeout: 15000 }); } catch (_) {}

  // 轮询等待引擎就绪
  console.log("── 2) 等待引擎就绪 ──");
  let enginePort = null, meta = null, health = null;
  for (let i = 0; i < 20; i++) {
    await wait(900);
    const eps = procs("electron.exe");
    const lines = listenLines().filter(x => x.port > 10000 && x.port < 65535);
    for (const { port } of lines) {
      const h = await get(port, "/api/health", 1200);
      if (h.json && (h.json.ok === true || h.json.status)) {
        enginePort = port; health = h.json;
        meta = (await get(port, "/api/meta", 2000)).json;
        break;
      }
    }
    if (enginePort) { console.log(`   第 ${i + 1} 次轮询命中，引擎端口 ${enginePort}`); break; }
  }

  console.log("\n── 3) 进程树 ──");
  const ep = procs("electron.exe");
  console.log("   electron.exe × " + ep.length + "  PID: " + ep.join(", "));
  const py = procs("python.exe");
  console.log("   python.exe  × " + py.length + "  PID: " + py.join(", "));

  console.log("\n── 4) 引擎端口 ──");
  if (!enginePort) {
    console.log("   ✘ 未找到引擎端口");
    console.log("   当前 LISTENING: " + listenLines().map(x => x.port).join(", "));
  } else {
    console.log("   ✔ " + enginePort);
  }

  console.log("\n── 5) 接口自检 ──");
  if (enginePort) {
    console.log("   /api/health → " + JSON.stringify(health));
    if (meta) {
      const packs = (meta.packs || []).map(p => `${p.name}${p.draft ? "(草稿)" : ""}`);
      console.log("   /api/meta   → default_pack=" + meta.default_pack + "  packs=[" + packs.join(", ") + "]");
    }
    const cfg = await get(enginePort, "/api/config", 2500);
    console.log("   /api/config → api_key_set=" + (cfg.json?.api_key_set) + "  model=" + (cfg.json?.model) + "  mock=" + (cfg.json?.mock));
    const hist = await get(enginePort, "/api/history", 2500);
    console.log("   /api/history→ " + (Array.isArray(hist.json) ? hist.json.length + " 条会话" : (hist.json?.items?.length ?? "-") + " 条会话"));
  }

  console.log("\n── 6) 启动日志 ──");
  try {
    const t = fs.readFileSync(logFile, "utf8");
    console.log(t.slice(-500).trim() || "(空)");
  } catch (_) { console.log("(无日志)"); }

  console.log("\n结论: " + (enginePort && ep.length ? "✔ 应用与引擎均已就绪，接口正常" : "✘ 未能就绪"));
  process.exit(0);
})();
