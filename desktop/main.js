// TalkScript — Electron 主进程
// 职责：拉起 Python 引擎子进程（127.0.0.1）→ 等健康检查 → 开窗口 → 退出时回收子进程
const { app, BrowserWindow, dialog } = require('electron');
const { spawn } = require('child_process');
const net = require('net');
const path = require('path');
const fs = require('fs');

let win = null;
let engineProc = null;
let enginePort = 0;

function findFreePort() {
  return new Promise((resolve, reject) => {
    const srv = net.createServer();
    srv.listen(0, '127.0.0.1', () => {
      const port = srv.address().port;
      srv.close(() => resolve(port));
    });
    srv.on('error', reject);
  });
}

// 引擎启动命令解析：TALKSCRIPT_PYTHON > 打包资源里的 engine.exe > 项目 .venv > PATH 中的 python
function resolveEngine(rootDir, resourcesDir) {
  const args = ['-m', 'app.server', '--port', String(enginePort), '--root', rootDir];
  const custom = process.env.TALKSCRIPT_PYTHON;
  if (custom) return { cmd: custom, args };

  const bundled = path.join(resourcesDir, 'engine', 'engine.exe');
  if (fs.existsSync(bundled)) {
    return { cmd: bundled, args: ['--port', String(enginePort), '--root', rootDir] };
  }
  const venvPy = process.platform === 'win32'
    ? path.join(rootDir, '.venv', 'Scripts', 'python.exe')
    : path.join(rootDir, '.venv', 'bin', 'python');
  if (fs.existsSync(venvPy)) return { cmd: venvPy, args };

  const py = process.platform === 'win32' ? 'python' : 'python3';
  return { cmd: py, args };
}

function startEngine(rootDir, resourcesDir) {
  const { cmd, args } = resolveEngine(rootDir, resourcesDir);
  console.log('[talkscript] engine:', cmd, args.join(' '));
  engineProc = spawn(cmd, args, {
    cwd: rootDir,
    env: { ...process.env, PYTHONIOENCODING: 'utf-8' },
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  engineProc.stdout.on('data', d => console.log('[engine]', String(d).trim()));
  engineProc.stderr.on('data', d => console.error('[engine]', String(d).trim()));
  engineProc.on('exit', code => {
    engineProc = null;
    if (code && code !== 0 && win) {
      dialog.showErrorBox('引擎已退出', `Python 引擎异常退出（代码 ${code}）。\n请检查 Python 环境与依赖：pip install -r requirements.txt`);
    }
  });
}

function waitHealth(timeoutMs = 40000) {
  const deadline = Date.now() + timeoutMs;
  return new Promise((resolve, reject) => {
    const tick = async () => {
      if (!engineProc) return reject(new Error('引擎进程未运行'));
      try {
        const r = await fetch(`http://127.0.0.1:${enginePort}/api/health`);
        if (r.ok) return resolve();
      } catch (_) { /* not ready yet */ }
      if (Date.now() > deadline) return reject(new Error('引擎健康检查超时'));
      setTimeout(tick, 400);
    };
    tick();
  });
}

function killEngine() {
  if (engineProc) {
    try { engineProc.kill(); } catch (_) {}
    engineProc = null;
  }
}

function createWindow() {
  win = new BrowserWindow({
    width: 1320,
    height: 860,
    title: 'TalkScript · 口播脚本智能体',
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
    },
  });
  win.loadFile(path.join(__dirname, 'renderer', 'index.html'), {
    query: { port: String(enginePort) },
  });
}

app.whenReady().then(async () => {
  const resourcesDir = process.resourcesPath || __dirname;            // 打包后为 resources/
  const rootDir = app.isPackaged
    ? path.join(resourcesDir, 'engine')
    : path.join(__dirname, '..');                                     // 开发态 = 项目根
  try {
    enginePort = await findFreePort();
    startEngine(rootDir, resourcesDir);
    await waitHealth();
  } catch (e) {
    dialog.showErrorBox('引擎启动失败', String(e));
    app.quit();
    return;
  }
  createWindow();
  app.on('activate', () => { if (BrowserWindow.getAllWindows().length === 0) createWindow(); });
});

app.on('window-all-closed', () => {
  killEngine();
  app.quit();
});
app.on('before-quit', killEngine);
process.on('exit', killEngine);
