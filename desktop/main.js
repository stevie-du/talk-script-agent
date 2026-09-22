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
//
// 本轮（交付路径复审）改的四件事，都在这一个文件里：
//   · 令牌不再出现在子进程的**命令行**上（改走环境变量），console 也不打它；
//   · 单实例锁：开第二次不再叠第二个引擎，只把已有窗口唤到前台；
//   · spawn 失败有 `error` 处理器（TALKSCRIPT_PYTHON 指错时不再是白屏 40 秒）；
//   · 引擎的 stdout / stderr 落到用户数据目录的日志文件里（README 一直在
//     让用户"看日志里的 engine 行"，而此前那份日志只存在于 devtools console）。
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
let spawnError = '';      // 子进程根本没起来（ENOENT / EACCES），与"起来了又退出"分开处理

// ── 单实例锁（缺陷 6）─────────────────────────────────────
//
// 为什么必须在**申请到锁之前**就把话说明：拿到锁失败就直接退，
// 一次都不该去 spawn 引擎。
//
// 引擎侧的并发保护救不了这里 —— `pipeline` 里那个锁是**进程内**的线程锁，
// 两个引擎进程各有自己的一把，于是两份 `generated/index.json` 会互相覆盖：
// 后写的那份把先写的记录整条抹掉（不是报错，是少一条历史）。
// 孤儿看门狗（--parent-pid）也挡不住：那是第二个**健康的**主进程，不是孤儿。
const gotTheLock = app.requestSingleInstanceLock();
if (!gotTheLock) {
  app.quit();
} else {
  app.on('second-instance', () => {
    // 用户以为"没开起来"，所以第二次的动作必须是**看得见**的：
    // 还原 + 聚焦已有窗口，而不是静默吞掉。
    if (win && !win.isDestroyed()) {
      if (win.isMinimized()) win.restore();
      win.show();
      win.focus();
    }
  });
}

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

// 引擎启动命令解析已拆到 `engine-path.js`（纯逻辑，可脱离 Electron 单测）——
// 「降级链的顺序」是 P1-3 的核心，读码确认不了，得能跑断言。
const { resolveEngine } = require('./engine-path');
// 对话框文案与脱敏同理拆到 `engine-dialogs.js`：那里守的是「令牌不许进对话框」
// 这条安全不变量（P2-5），拆出来才有地方跑断言。
const { maskSecrets: maskWith, failLoadDetail } = require('./engine-dialogs');

function engineCommand() {
  return resolveEngine({
    rootDir: rootDirCached,
    resourcesDir: resourcesDirCached,
    // --data-dir：可写数据目录。打包后 rootDir 在安装目录（Program Files），
    // 配置与产物都不能往那儿写；开发态直接用项目根。
    dataDir: app.isPackaged ? app.getPath('userData') : rootDirCached,
    // --packs-dir：可写的行业包目录（缺陷 8）。打包版必须给 —— 否则用户
    // 新建的行业包、手改的 banwords.yaml、填的 private/ 资料都住在安装目录里，
    // 一次自动更新就没了。开发态刻意**不给**（继续用项目里的 packs/，
    // 改了立刻能在 git 里看到，不去污染 %APPDATA%）。
    packsDir: app.isPackaged ? path.join(app.getPath('userData'), 'packs') : undefined,
    // --version：版本号的唯一来源是 desktop/package.json（electron-builder 也认它），
    // 引擎在打包版里读不到这个文件（--root 指向 resources/engine），所以显式传过去。
    version: app.getVersion(),
    port: enginePort, token: engineToken,
    // 主进程 PID：引擎用它做看门狗。python.exe 的直接父进程就是这个主进程，
    // 所以传 process.pid 正好——传 GPU/renderer 的 pid 会误杀。
    parentPid: process.pid,
  });
}

// ── 脱敏 ──────────────────────────────────────────────────
// 引擎的 stdout / stderr 现在要落盘（缺陷 7），而它启动时会打印带 token 的界面
// 地址，异常堆栈里也可能带 Authorization 头 —— 日志文件是给"人来排查"看的，
// 也是最容易被顺手贴进 issue / 聊天窗口的一份东西，所以写入前一律过一遍。
// argv 那行日志同样过它（万一将来有人把令牌又加回命令行，这里仍不出货）。
//
// ⚠ 规则本体在 `engine-dialogs.js`（与 Electron 无关，可单测）。
//   以前这里有一副本地的实现，而**对话框**那条通道根本没走它（P2-5）——
//   收成一个模块之后，日志与对话框共用同一个出口，没有"哪条路忘了过一遍"这种漏法。
function maskSecrets(s) {
  return maskWith(s, engineToken);
}

// ── 引擎日志（缺陷 7）─────────────────────────────────────
//
// README 从很久以前就在让用户"看日志里有没有 `engine/py（出厂运行时）` 这一行"
// （判断有没有降级到系统 Python），但**从来没有一个日志文件**：
// 那些行只进 devtools console，而打包版连 DevTools 都要按 F12 才开。
// 现在落到 <userData>/logs/engine.log，超过上限轮转一档（engine.log.1）。
const ENGINE_LOG_MAX = 2 * 1024 * 1024;
let engineLogStream = null;
let engineLogPath = '';

function engineLogInit() {
  try {
    const dir = path.join(app.getPath('userData'), 'logs');
    fs.mkdirSync(dir, { recursive: true });
    engineLogPath = path.join(dir, 'engine.log');
    try {
      const st = fs.statSync(engineLogPath);
      if (st.size > ENGINE_LOG_MAX) {
        fs.copyFileSync(engineLogPath, engineLogPath + '.1');
        fs.writeFileSync(engineLogPath, '');
      }
    } catch (_) { /* 第一次没有日志文件 */ }
    if (engineLogStream) engineLogStream.end();
    engineLogStream = fs.createWriteStream(engineLogPath, { flags: 'a' });
    engineLogStream.on('error', (e) => {
      // 日志写不了不该影响引擎，也不该再冒一个未捕获异常
      console.error('[talkscript] 日志文件写入失败：', e.message);
      engineLogStream = null;
    });
    logLine(`──── ${new Date().toISOString()} TalkScript v${app.getVersion()} `
            + `启动（打包态=${app.isPackaged ? '是' : '否'}）────`);
  } catch (e) {
    console.error('[talkscript] 无法创建日志目录：', e.message);
    engineLogPath = '';
    engineLogStream = null;
  }
}

function logLine(s) {
  const line = maskSecrets(s);
  if (engineLogStream) {
    try { engineLogStream.write(line.endsWith('\n') ? line : line + '\n'); }
    catch (_) { /* 见 createWriteStream 的 error 处理器 */ }
  }
}

let engineStderrBuf = '';     // 引擎异常退出时一并显示，让用户看到真正的错
const STDERR_BUF_MAX = 4096;   // 截断防止超长（ImportError 堆栈可能上千字）

function startEngine() {
  const { cmd, args, env, via } = engineCommand();
  // 把「从哪条路起的」也打出来：引擎起不来时第一件要问的就是这个，
  // 而它以前只存在于代码的 if 顺序里，日志看不出来。
  // ⚠ args 里**不该再有令牌**（缺陷 1）；maskSecrets 是第二道，防的是有人把它加回去。
  const argvLine = maskSecrets(`${cmd} ${args.join(' ')}`);
  console.log(`[talkscript] engine (${via}):`, argvLine);
  logLine(`[spawn] via=${via} ${argvLine}`);
  spawnError = '';
  engineProc = spawn(cmd, args, {
    cwd: rootDirCached,
    // 令牌走环境变量（不进 argv，见 engine-path.js 顶部说明）。
    // TALKSCRIPT_TOKEN 这个名字的两端：这里写、app/server.py 的 TOKEN_ENV 读。
    env: { ...process.env, PYTHONIOENCODING: 'utf-8', ...env },
    stdio: ['ignore', 'pipe', 'pipe'],
    // windowsHide 不能省：python.exe 是 console 子系统程序，Windows 上 spawn
    // 默认会给它开一个控制台窗口 —— 于是每次启动 TalkScript 都会闪一下黑框。
    // 引擎的 stdout / stderr 已经被 pipe 到这里并以 [engine] 前缀转发，
    // 隐藏控制台不会丢任何日志。
    windowsHide: true,
  });
  // 子进程**根本没起来**时（TALKSCRIPT_PYTHON 指错 / 内嵌运行时被杀软删了 /
  // 目录不可访问）Node 只在 ChildProcess 上 emit 一个 'error'，不 throw。
  // 修复前这里没有监听者 → 'error' 变成**未捕获异常**，主进程直接挂掉，
  // 用户看到的是白屏或"应用打不开"，一句原因都没有。
  engineProc.on('error', (e) => {
    const why = e.code === 'ENOENT'
      ? `找不到引擎程序：${cmd}`
        + (process.env.TALKSCRIPT_PYTHON
            ? '（它来自环境变量 TALKSCRIPT_PYTHON，去掉这个变量就会改用应用自带的运行时）'
            : '（应用自带的 Python 运行时可能不完整或已被安全软件删掉）')
      : `无法启动引擎进程（${e.code || e.name}）：${e.message}`;
    spawnError = why;
    console.error('[talkscript] spawn 失败：', why);
    logLine(`[spawn-error] ${why}`);
    engineProc = null;
    if (win && !win.isDestroyed()) {
      dialog.showErrorBox('引擎无法启动', why
        + '\n\n完整日志：' + (engineLogPath || '（日志文件不可用）'));
    }
    // 窗口还没开时（首次启动在 waitHealth 里等着）不去弹框打断那条路径：
    // waitHealth 一看到这个 spawnError 就会立刻抛出同一句话，由它统一报错。
  });
  engineProc.stdout.on('data', (d) => {
    const s = maskSecrets(String(d));
    console.log('[engine]', s.trim());
    logLine(`[stdout] ${s}`);
  });
  engineProc.stderr.on('data', (d) => {
    const s = maskSecrets(String(d));
    console.error('[engine]', s.trim());
    logLine(`[stderr] ${s}`);
    engineStderrBuf = (engineStderrBuf + s).slice(-STDERR_BUF_MAX);
  });
  engineProc.on('exit', (code, signal) => {
    engineProc = null;
    if (quitting) return;
    if (spawnError) return;      // 'error' 已经报过原因，别再叠一个"异常退出（代码 null）"
    logLine(`[exit] code=${code} signal=${signal}`);
    // 引擎中途退出：窗口还开着的话，用户只会看到「无法连接本地引擎」，
    // 必须明确告知并提供重试，否则只能自己猜。
    if (win && !win.isDestroyed()) {
      // 把 stderr 摘要一并塞进 detail —— 用户**自己**就能看到 Python 报的错
      // （缺模块 / 端口冲突 / 路径不对），而不是只能看到「代码 1」。
      // ⚠ 完整的 stdout / stderr 现在在日志文件里（已脱敏，超长会轮转），
      //   对话框里的这段只是最后 4 KB。
      const errTail = engineStderrBuf.trim()
        ? `\n\n────── 引擎 stderr（最后 ${STDERR_BUF_MAX} 字符）──────\n`
          + engineStderrBuf.trim().slice(-STDERR_BUF_MAX)
        : '\n\n（未捕获到 stderr 输出 —— 看日志文件的 [engine] 行）';
      const choice = dialog.showMessageBoxSync(win, {
        type: 'error',
        title: '引擎已退出',
        message: `Python 引擎异常退出（代码 ${code}）。`,
        detail: '常见原因：缺少依赖（pip install -r requirements.txt）、'
              + '端口被占用，或 Python 环境不可用。'
              + errTail
              + '\n\n完整日志：' + (engineLogPath || '（日志文件不可用）'),
        buttons: ['重启引擎', '退出应用'],
        defaultId: 0,
        cancelId: 1,
      });
      engineStderrBuf = '';   // 重置，下次崩溃从空开始累计
      if (choice === 0) restartEngine();
      else app.quit();
    }
  });
}

async function waitHealth(timeoutMs = 40000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    // spawn 失败时不必等满 40 秒：原因已经拿到了，立刻报（缺陷 7）。
    if (spawnError) throw new Error(spawnError);
    if (!engineProc) throw new Error('引擎进程未运行');
    try {
      const r = await fetch(`http://127.0.0.1:${enginePort}/api/health`);
      if (r.ok) return;
    } catch (_) { /* 还没起来，继续等 */ }
    await new Promise(r => setTimeout(r, 400));
  }
  throw new Error(spawnError || '引擎健康检查超时');
}

function killEngine() {
  if (engineProc) {
    try { engineProc.kill(); } catch (_) { /* 已退出 */ }
    engineProc = null;
  }
}

function closeEngineLog() {
  if (engineLogStream) {
    try { logLine('──── 引擎进程已回收，本次记录到此 ────'); engineLogStream.end(); }
    catch (_) { /* 收尾失败无所谓 */ }
    engineLogStream = null;
  }
}

function engineUrl() {
  return `http://127.0.0.1:${enginePort}/?token=${encodeURIComponent(engineToken)}`;
}

async function restartEngine() {
  killEngine();
  // 重启前把 stderr 缓冲清掉 —— 上次的错不要混进这次
  engineStderrBuf = '';
  spawnError = '';
  try {
    enginePort = await findFreePort();
    startEngine();
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
    // ⚠ `url` 就是 engineUrl()：`http://127.0.0.1:PORT/?token=<一次性令牌>`。
    // 修复前这里把它**原样**插进 showErrorBox —— 那是唯一一个不过脱敏的出口
    // （stdout / stderr / argv 三处都过了），于是一次加载失败就把访问令牌
    // 打进一个能截图、能全选复制、能贴进 issue 的系统对话框里（P2-5）。
    // 整条地址要留（端口是排查线索），令牌不留：文案构造收在 engine-dialogs.js，
    // 那里有断言守着「传进去的令牌值不可能出现在返回值里」。
    dialog.showErrorBox('界面加载失败',
      failLoadDetail({ desc, code, url, logPath: engineLogPath, token: engineToken }));
  });

  win.loadURL(engineUrl());
}

app.whenReady().then(async () => {
  // 没抢到单实例锁的那个副本走的是 app.quit()，一个引擎都不该起。
  if (!gotTheLock) return;
  const resourcesDir = process.resourcesPath || __dirname;            // 打包后为 resources/
  const rootDir = app.isPackaged
    ? path.join(resourcesDir, 'engine')
    : path.join(__dirname, '..');                                     // 开发态 = 项目根
  rootDirCached = rootDir;
  resourcesDirCached = resourcesDir;
  engineLogInit();
  // 一次性访问令牌：每次启动随机生成，经 URL 交给渲染层，API 请求带上它。
  // 交给**引擎**的方式是子进程环境变量（不是命令行，见 engine-path.js 顶部）。
  engineToken = crypto.randomBytes(24).toString('base64url');

  // 端口在「探测到空闲」与「引擎真正绑定」之间有一个窗口，理论上可能被抢占。
  // 抢到了就换一个端口重试一次，而不是让用户看到一句「健康检查超时」。
  let lastErr = null;
  for (let attempt = 0; attempt < 2; attempt++) {
    try {
      enginePort = await findFreePort();
      startEngine();
      await waitHealth();
      lastErr = null;
      break;
    } catch (e) {
      lastErr = e;
      killEngine();
      // 第一次失败如果是"程序找不到"（ENOENT），换端口重试是白等 40 秒：
      // 换个端口不会让 python.exe 长出来。这类错误直接出局。
      if (spawnError) break;
    }
  }
  if (lastErr) {
    logLine(`[fatal] 引擎启动失败：${lastErr && lastErr.message ? lastErr.message : lastErr}`);
    closeEngineLog();
    dialog.showErrorBox('引擎启动失败',
      `${lastErr}\n\n请确认已安装依赖：pip install -r requirements.txt`
      + `\n\n完整日志：` + (engineLogPath || '（日志文件不可用）'));
    app.quit();
    return;
  }
  createWindow();
  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

app.on('window-all-closed', () => { quitting = true; killEngine(); closeEngineLog(); app.quit(); });
app.on('before-quit', () => { quitting = true; killEngine(); closeEngineLog(); });
process.on('exit', () => { killEngine(); closeEngineLog(); });
