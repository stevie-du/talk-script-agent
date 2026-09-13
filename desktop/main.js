// TalkScript — Electron 主进程
//
// 职责：拉起 Python 引擎子进程（127.0.0.1）→ 等健康检查 → 开窗口 → 退出时回收子进程
//
// 本轮改动的关键点：渲染层不再用 loadFile(file://) 加载，而是从引擎**同源**加载
// （loadURL + 一次性令牌）。这一条同时解决三件事：
//   1. 引擎不必再为了迁就 file:// 而放行 `Origin: null` —— 那是本机任意网页
//      都能读走脚本 / 删记录 / 改 base_url 的根源；
//   2. 页面与 API 同源，前端可以用 ES 模块（file:// 下 <script type=module>
//      会被 CORS 拦掉），app.js 因此得以拆成 11 个模块；
//   3. 令牌可以走 URL 传给页面，请求带上 X-TalkScript-Token。
const { app, BrowserWindow, dialog, Menu, shell } = require('electron');
const { spawn } = require('child_process');
const net = require('net');
const path = require('path');
const fs = require('fs');
const crypto = require('crypto');

let win = null;
let engineProc = null;
let enginePort = 0;
let engineToken = '';
let quitting = false;
let rootDirCached = '';
let resourcesDirCached = '';

function findFreePort() {
  return new Promise((resolve, reject) => {
    const srv = net.createServer();
    srv.on('error', reject);
    srv.listen(0, '127.0.0.1', () => {
      const port = srv.address().port;
      srv.close(() => resolve(port));
    });
  });
}

// 引擎启动命令解析：TALKSCRIPT_PYTHON > 打包资源里的 engine.exe > 项目 .venv > PATH 中的 python
function resolveEngine(rootDir, resourcesDir) {
  // --data-dir：可写数据目录。打包后 rootDir 在安装目录（Program Files），
  // 配置与产物都不能往那儿写；开发态直接用项目根。
  const dataDir = app.isPackaged ? app.getPath('userData') : rootDir;
  // --version：版本号的唯一来源是 desktop/package.json（electron-builder 也认它），
  // 引擎在打包版里读不到这个文件（--root 指向 resources/engine），所以显式传过去。
  const args = ['-m', 'app.server', '--port', String(enginePort),
                '--root', rootDir, '--data-dir', dataDir, '--token', engineToken,
                '--version', app.getVersion()];
  const custom = process.env.TALKSCRIPT_PYTHON;
  if (custom) return { cmd: custom, args };

  const bundled = path.join(resourcesDir, 'engine', 'engine.exe');
  if (fs.existsSync(bundled)) {
    return { cmd: bundled, args: ['--port', String(enginePort), '--root', rootDir,
                                  '--data-dir', dataDir, '--token', engineToken,
                                  '--version', app.getVersion()] };
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
    if (quitting) return;
    // 引擎中途退出：窗口还开着的话，用户只会看到「无法连接本地引擎」，
    // 必须明确告知并提供重试，否则只能自己猜。
    if (win && !win.isDestroyed()) {
      const choice = dialog.showMessageBoxSync(win, {
        type: 'error',
        title: '引擎已退出',
        message: `Python 引擎异常退出（代码 ${code}）。`,
        detail: '常见原因：缺少依赖（pip install -r requirements.txt）、'
              + '端口被占用，或 Python 环境不可用。',
        buttons: ['重启引擎', '退出应用'],
        defaultId: 0,
        cancelId: 1,
      });
      if (choice === 0) restartEngine();
      else app.quit();
    }
  });
}

async function waitHealth(timeoutMs = 40000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (!engineProc) throw new Error('引擎进程未运行');
    try {
      const r = await fetch(`http://127.0.0.1:${enginePort}/api/health`);
      if (r.ok) return;
    } catch (_) { /* 还没起来，继续等 */ }
    await new Promise(r => setTimeout(r, 400));
  }
  throw new Error('引擎健康检查超时');
}

function killEngine() {
  if (engineProc) {
    try { engineProc.kill(); } catch (_) { /* 已退出 */ }
    engineProc = null;
  }
}

function engineUrl() {
  return `http://127.0.0.1:${enginePort}/?token=${encodeURIComponent(engineToken)}`;
}

async function restartEngine() {
  killEngine();
  try {
    enginePort = await findFreePort();
    startEngine(rootDirCached, resourcesDirCached);
    await waitHealth();
    if (win && !win.isDestroyed()) {
      await win.loadURL(engineUrl());
      dialog.showMessageBox(win, {
        type: 'info', title: '引擎已重启',
        message: '本地引擎已重新启动，界面已重新加载。',
        detail: '之前正在进行的生成作业已经丢失（引擎重启会释放内存中的作业），'
              + '已落盘的记录不受影响。',
        buttons: ['好'],
      });
    }
  } catch (e) {
    dialog.showErrorBox('引擎重启失败', String(e));
    app.quit();
  }
}

function createWindow() {
  // 去掉 Electron 自带菜单栏（Windows/Linux 顶部菜单），产品 UI 内无菜单需求
  Menu.setApplicationMenu(null);
  win = new BrowserWindow({
    width: 1320,
    height: 860,
    title: 'TalkScript · 口播脚本智能体',
    backgroundColor: '#faf9f7',
    autoHideMenuBar: true,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      spellcheck: false,
    },
  });

  // 菜单移除后 DevTools 快捷键失效，用 F12 保留开发调试入口
  win.webContents.on('before-input-event', (_e, input) => {
    if (input.type === 'keyDown' && input.key === 'F12') {
      win.webContents.toggleDevTools();
      _e.preventDefault();
    }
  });

  // 外部链接一律交给系统浏览器，不在应用窗口里打开
  win.webContents.setWindowOpenHandler(({ url }) => {
    if (/^https?:/.test(url)) shell.openExternal(url);
    return { action: 'deny' };
  });

  win.webContents.on('did-fail-load', (_e, code, desc, url) => {
    if (quitting) return;
    dialog.showErrorBox('界面加载失败',
      `无法从本地引擎加载界面。\n${desc}（${code}）\n${url}\n\n`
      + '请确认引擎正在运行；若反复出现，请重启应用。');
  });

  win.loadURL(engineUrl());
}

app.whenReady().then(async () => {
  const resourcesDir = process.resourcesPath || __dirname;            // 打包后为 resources/
  const rootDir = app.isPackaged
    ? path.join(resourcesDir, 'engine')
    : path.join(__dirname, '..');                                     // 开发态 = 项目根
  rootDirCached = rootDir;
  resourcesDirCached = resourcesDir;
  // 一次性访问令牌：每次启动随机生成，经 URL 交给渲染层，API 请求带上它。
  engineToken = crypto.randomBytes(24).toString('base64url');

  // 端口在「探测到空闲」与「引擎真正绑定」之间有一个窗口，理论上可能被抢占。
  // 抢到了就换一个端口重试一次，而不是让用户看到一句「健康检查超时」。
  let lastErr = null;
  for (let attempt = 0; attempt < 2; attempt++) {
    try {
      enginePort = await findFreePort();
      startEngine(rootDir, resourcesDir);
      await waitHealth();
      lastErr = null;
      break;
    } catch (e) {
      lastErr = e;
      killEngine();
    }
  }
  if (lastErr) {
    dialog.showErrorBox('引擎启动失败',
      `${lastErr}\n\n请确认已安装依赖：pip install -r requirements.txt`);
    app.quit();
    return;
  }
  createWindow();
  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

app.on('window-all-closed', () => { quitting = true; killEngine(); app.quit(); });
app.on('before-quit', () => { quitting = true; killEngine(); });
process.on('exit', killEngine);
