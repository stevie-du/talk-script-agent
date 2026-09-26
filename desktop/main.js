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
const { app, BrowserWindow, dialog, ipcMain, Menu, shell } = require('electron');
const { spawn } = require('child_process');
const net = require('net');
const path = require('path');
const fs = require('fs');
const crypto = require('crypto');

let win = null;
let engineProc = null;
let enginePort = 0;
let engineToken = '';
// 健康检查的身份凭证（见 waitHealth）：只用于确认应答者是我们的引擎，不授权任何操作。
let engineHealthNonce = '';
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
      return;
    }
    // 窗口未就绪（冷启动还没走到 createWindow / 全部窗口已关但进程未退）：
    // 没有可聚焦的东西，这次触发只能忽略 —— 但忽略也要留痕（P3），
    // 否则排查「为什么第二次启动没反应」时无迹可循。引擎日志此时可能还没初始化
    // （logLine 会静默跳过），所以再补一行 console 兜底。
    logLine('[second-instance] 第二实例触发，但窗口未就绪，忽略（无窗口可聚焦）');
    console.log('[talkscript] second-instance: 窗口未就绪，忽略');
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
// 自动更新的纯逻辑（地址解析 / 6h 守门 / 错误人话化）拆到 `updater-core.js`，
// 与 engine-path.js 同一理由：这些规则读码确认不了，得有断言钉着
// （docs/自动更新方案.md §6 / §10）。
const updaterCore = require('./updater-core');
// 自建 updater 实例（官方「instantiating updater directly」正规路径，§6）。
// ⚠ 不用 autoUpdater.setFeedURL——官方文档原话 "Do not call setFeedURL"。
const { NsisUpdater } = require('electron-updater');

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
    // 健康检查的身份凭证（见 waitHealth）：与令牌同一条 env 通道传给引擎。
    healthNonce: engineHealthNonce,
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
  // 每次新 spawn 前把上一颗引擎的 stderr 摘要清掉（P3）：两试循环的第二试 /
  // restartEngine 若不清，下一次崩溃的对话框会把**上一次**的报错安在这一次头上，
  // 把人往错误的排查方向引。exit 处理器里那处重置管的是「弹过框之后」，
  // 这里管的是「每一次新引擎起跑之前」，两处各守一段。
  engineStderrBuf = '';
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
  // exit 处理器要认「退出的这个是不是自己」：把 spawn 返回的引用先钉进闭包，
  // 与模块级 engineProc（会被下一次 spawn 改指）区分开 —— 这是 P2-5 守卫的前提。
  const proc = engineProc;
  engineProc.on('exit', (code, signal) => {
    // 身份校验（P2-5）：exit 事件的派发时机不受我们控制 —— 两试循环里 attempt 1
    // 被 killEngine() 杀掉后，它的 exit 事件可能**晚于** attempt 2 的 spawn 落地
    // 才到。不校验的话，旧进程的退场会把模块级 engineProc（此刻已指向在跑的
    // 新引擎）清成 null：waitHealth 接着误报「引擎进程未运行」，活着的引擎死了
    // 没人报，还会平白弹「引擎已退出」。所以：退出的若不是当前引擎，
    // 只留一行日志，其余一概不碰。
    if (engineProc !== proc) {
      logLine(`[exit-late] 非当前引擎的退场事件（code=${code} signal=${signal}），忽略`);
      return;
    }
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
      if (r.ok) {
        // ⚠ **不能只看 `r.ok`**：端口在「探测到空闲」与「引擎真正绑定」之间有窗口，
        //   本机别的进程抢到并回一个 200 时，后面的 `loadURL` 会把一次性访问令牌
        //   带进**那个进程**的请求行 —— 而 `engine-path.js` 顶部论证的正是
        //   「令牌不能落到别的本机进程手里」。所以还要确认应答者**知道这个 nonce**。
        const body = await r.json().catch(() => null);
        if (body && body.nonce === engineHealthNonce) return;
        // 身份不符**立刻**报，不等满 40 秒：那个进程不会突然变成我们的引擎。
        // 由 whenReady 的循环接住 → 换端口重试一次 → 仍失败则报「引擎启动失败」。
        const err = new Error(`端口 ${enginePort} 上的应答者不是我们的引擎`
          + `（健康检查身份不符：${body ? 'nonce 不匹配' : '响应不是 JSON'}）`
          + '—— 为免一次性令牌落到它手里，本次不采用');
        err.identityMismatch = true;
        throw err;
      }
    } catch (e) {
      if (e && e.identityMismatch) throw e;
      /* 还没起来，继续等 */
    }
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

// ── 自动更新（docs/自动更新方案.md）──────────────────────────
//
// 配置唯一来源是环境变量 TALKSCRIPT_UPDATE_URL（§6）：没配 → 整体关闭，
// 一次网络请求都不发。那条总闸在 updater-core.js（有单测），这里只消费它的结论。
//
// updater.log 与 engine.log 同一 <userData>/logs/ 目录、同一套机制（2 MB 轮转一档、
// 写前过 maskSecrets、流错误不冒未捕获异常）。刻意**并列实现**而不是把引擎日志重构
// 成公共模块：零功能收益，却要动 15 处调用点，回归风险不成比例（§3.3 末段）。
const UPDATER_LOG_MAX = 2 * 1024 * 1024;
let updaterLogPath = '';
// 更新日志没落成盘的原因（P2-43）：给失败状态行用，见 pushUpdaterStatus 的 error 分支。
let updaterLogError = '';

function updaterLogInit() {
  try {
    const dir = path.join(app.getPath('userData'), 'logs');
    fs.mkdirSync(dir, { recursive: true });
    updaterLogPath = path.join(dir, 'updater.log');
    try {
      const st = fs.statSync(updaterLogPath);
      if (st.size > UPDATER_LOG_MAX) {
        fs.copyFileSync(updaterLogPath, updaterLogPath + '.1');
        fs.writeFileSync(updaterLogPath, '');
      }
    } catch (_) { /* 第一次没有日志文件 */ }
    updaterLogError = '';
    updaterLogLine(`──── ${new Date().toISOString()} TalkScript v${app.getVersion()} `
                 + `更新日志启动（打包态=${app.isPackaged ? '是' : '否'}）────`);
  } catch (e) {
    console.error('[talkscript] 无法创建更新日志：', e.message);
    updaterLogPath = '';
    // ⚠ 记下来给失败状态行用（P2-43）：打包版**看不到** console.error，
    //   而 logPath 为空会让界面那行「完整日志：」整个不显示 ——
    //   用户既拿不到原文，也不知道为什么拿不到。
    updaterLogError = `更新日志没落成盘（${e.message}）`;
  }
}

// 同步追加写，不用 WriteStream：实测（2026-09-24 冒烟）electron-updater 的退出
// 安装钩子（app.once('quit')）晚于 process 'exit' 派发——安装器 spawn 的系统时刻
// 在「──── 更新日志关闭 ────」之后。异步 WriteStream 的 flush + closeUpdaterLog 的
// end()/置空会让「Auto install update on quit」三行静默丢失，而它是「退出时到底
// 装没装」的唯一日志证据（§3.3：失败必须可见）。更新事件稀疏，appendFileSync 的
// 成本无所谓；收尾也只写「关闭」一行、不再关流置空，quit handler 迟来的行照样落盘。
function updaterLogLine(s) {
  const line = maskSecrets(s);
  if (!updaterLogPath) return;
  try { fs.appendFileSync(updaterLogPath, line.endsWith('\n') ? line : line + '\n'); }
  catch (e) { console.error('[talkscript] 更新日志写入失败：', e.message); }
}

function closeUpdaterLog() {
  if (updaterLogPath) {
    try { updaterLogLine('──── 更新日志关闭 ────'); }
    catch (_) { /* 收尾失败无所谓 */ }
  }
}

// electron-updater 的 Logger 接口（6.8.9 types.d.ts）：info / warn / error 必需，
// debug 标称可选——但源码里有 10 处 logger.debug 调用（AppUpdater 4、MacUpdater 2、
// DifferentialDownloader 2、downloadPlanBuilder 2），差分下载路径必调。少实现一个
// debug 就是 TypeError，且只在「老用户 + 缓存健在」的差分路径上炸（§5）。
// 四个方法全实现，它的内部调试行与我们的行同在 updater.log（§3.1 末段）——
// 否则「它为什么没检查到更新」只有它自己知道。
const updaterLogger = {
  debug: (m) => updaterLogLine(`[upd-debug] ${m}`),
  info: (m) => updaterLogLine(`[upd] ${m}`),
  warn: (m) => updaterLogLine(`[upd-warn] ${m}`),
  error: (m) => updaterLogLine(`[upd-error] ${m}`),
};

let updater = null;             // NsisUpdater 实例；null = 链路未启用（不开就不发请求）
let updaterFeedUrl = null;      // resolveFeedUrl 的返回值（已规范化，null = 没配）
let updaterLastCheckAt = null;  // 最近一次检查的发起时间（Date.now() 制），6h 守门用
let updaterPendingVersion = ''; // 发现的新版本号（available → downloaded 全程带着显示）
let updaterStatus = { state: 'idle' };  // 推给渲染层的当前状态（七态，§4.2）
let updaterStartupTimer = null; // 启动后 15s 首查
let updaterCheckTimer = null;   // 之后每 6h 一次

// 把状态推给渲染层。主进程只给**事实**（state / version / percent / text），
// 界面文案与颜色归渲染层——但 error / disabled 的 text 例外：它们由
// classifyError / disabledStatus 在主进程人话化，原文同时进 updater.log。
function pushUpdaterStatus(patch) {
  updaterStatus = { ...updaterStatus, ...patch };
  if (win && !win.isDestroyed()) {
    win.webContents.send('updater:status', updaterStatus);
  }
}

function quitAndInstallUpdater() {
  if (!updater) return;
  // 「立即重启」路径 2（§4.2）：用户坐在设置页、明确点了按钮，直接装，
  // 不再二次确认。装完自动拉起 = quitAndInstall(isSilent=true, isForceRunAfter=true)。
  updaterLogLine('[install] 用户选择立即重启：quitAndInstall(isSilent=true, isForceRunAfter=true)');
  // quitAndInstall 内部会关窗并 app.quit()（BaseUpdater.quitAndInstall），
  // process.on('exit') 钩子随之回收引擎、关两份日志。引擎必须先显式回收：
  // 安装器不会替我们杀 python.exe，孤儿引擎会占着端口和内存，单实例锁也救不了它。
  quitting = true;
  killEngine();
  updater.quitAndInstall(true, true);
}

async function runCheck() {
  // 调用方（initUpdater 定时器 / IPC handler）已确认链路启用，updater 非 null。
  pushUpdaterStatus({ state: 'checking' });
  // 记在**发起时**：6h 守门问的是「距上次检查满没满」，检查在途期间定时器
  // 再触发时靠它防重入（updater 内部也复用同一 promise，双保险，§4.3）。
  updaterLastCheckAt = Date.now();
  try {
    const result = await updater.checkForUpdates();
    if (result && result.downloadPromise) {
      // autoDownload 时 checkForUpdates 提前 resolve，下载在 downloadPromise 上跑；
      // 下载失败它 reject（AppUpdater.downloadUpdate 的 catch 重抛），而原因已经
      // 由 'error' 事件报告。这里必须接住——否则未处理的 rejection 会打崩主进程，
      // 失败形态从「状态行留痕」变成「应用消失」（§3.3 铁律的反面教材）。
      result.downloadPromise.catch(() => { /* 已由 'error' 事件报告并留痕 */ });
    }
    if (result === null) {
      // 6.8.9 实况（node_modules/electron-updater/out/AppUpdater.js 的
      // checkForUpdates）：null **只在** `!isUpdaterActive()` 时返回 ——
      // 即「应用未打包且未 forceDevUpdateConfig」。「已有检查在途」返回的是
      // **同一个 promise**（checkForUpdatesPromise 复用），不是 null。
      // 我们的链路只在启用态（isPackaged 且配了地址）构造 updater 并调它，
      // 所以这按理是死分支 —— 保留仅作防御性留痕，不弹窗不改状态行。
      updaterLogLine('[check] checkForUpdates 返回 null（isUpdaterActive 为假，链路被关）');
    }
  } catch (e) {
    // checkForUpdates 失败时既 emit 'error'（上面已 push error 状态）又 reject——
    // 这里只留痕，不再二次报错。
    updaterLogLine(`[check] checkForUpdates reject：${e && e.message ? e.message : e}`);
  }
}

// 窗口出现后才调用（§7）：冷启动那几百毫秒不属于更新检查。
function initUpdater() {
  updaterLogInit();
  updaterFeedUrl = updaterCore.resolveFeedUrl(process.env[updaterCore.ENV_UPDATE_URL]);
  // portable 检测用 PORTABLE_EXECUTABLE_DIR——electron-builder 的 portable 安装
  // 模板（portable.nsi）在每个便携包启动时必设，模板级铁证，不靠口头约定（§3.2）。
  const isPortable = !!process.env.PORTABLE_EXECUTABLE_DIR;
  const disabled = updaterCore.disabledStatus(app.isPackaged, updaterFeedUrl, isPortable);
  if (disabled) {
    // dev / portable / 没配地址：整体关闭，一次请求都不发（§3.2 / §6）。
    // 「为什么没检查」必须写进日志——可查，不靠读代码相信。
    updaterLogLine(`[init] 更新链路未启用：${disabled.text}`
      + `（isPackaged=${app.isPackaged} isPortable=${isPortable}`
      + ` TALKSCRIPT_UPDATE_URL=${updaterFeedUrl ? '已配置' : '未配置'}）`);
    pushUpdaterStatus({ ...disabled });
    return;
  }

  updater = new NsisUpdater({ provider: 'generic', url: updaterFeedUrl });
  updater.logger = updaterLogger;
  // 发现新版本就后台下；询问发生在「装」的时候，不是「下」的时候（§4.1）。
  updater.autoDownload = true;
  // v6 安装语义（§3.1）：下载完即校验 sha512，挂 app.onQuit，exitCode 0 退出时
  // 静默安装、装完不自动拉起。默认就是 true，显式写出来是为了让 v6/v7 分叉
  // 只有这一行（§12.6：v7 出 stable 时改 autoInstallEvent = "onNextLaunch"）。
  // ⚠ 现在**不写** v7 的 autoInstallEvent：6.8.9 里没有这个属性，写了就是静默失效。
  updater.autoInstallOnAppQuit = true;

  // 六事件（AppUpdater.d.ts：checking-for-update / update-not-available /
  // update-available / download-progress / update-downloaded / error）。
  updater.on('checking-for-update', () => {
    pushUpdaterStatus({ state: 'checking' });
  });
  updater.on('update-not-available', () => {
    pushUpdaterStatus({ state: 'uptodate', version: app.getVersion() });
  });
  updater.on('update-available', (info) => {
    // 先报「发现新版本」，随后 download-progress 接管百分比（§4.2 七态表）
    updaterPendingVersion = info.version;
    pushUpdaterStatus({ state: 'available', version: info.version });
  });
  updater.on('download-progress', (p) => {
    pushUpdaterStatus({ state: 'downloading', version: updaterPendingVersion, percent: Math.floor(p.percent) });
  });
  updater.on('update-downloaded', (event) => {
    // 此刻 sha512 已校验过关（不过关走 error，不安装，§3.1）。
    updaterPendingVersion = event.version;
    pushUpdaterStatus({ state: 'downloaded', version: event.version });
    // 「立即重启」路径 1（§4.2）：用户没坐在设置页前 → 主进程弹确认框，
    // 默认焦点必须是「稍后」。重启会丢内存中进行中的生成作业（与 restartEngine
    // 对话框告知的是同一件事），不能让手滑把作业送了。
    if (win && !win.isDestroyed()) {
      dialog.showMessageBox(win, {
        type: 'info',
        title: '更新已就绪',
        message: `TalkScript ${event.version} 已下载完成。`,
        detail: '退出 TalkScript 时会自动安装；也可以现在立即重启，安装完成后会自动打开新版本。'
              + '\n重启会中断正在进行的生成作业（已落盘的记录不受影响）。',
        buttons: ['立即重启', '稍后'],
        defaultId: 1,
        cancelId: 1,
      }).then(({ response }) => {
        if (response === 0) quitAndInstallUpdater();
      }).catch(() => {
        // 窗口在弹框前被销毁：状态行已是 downloaded，用户仍可从面板自己点
        updaterLogLine('[install] 确认框弹出失败（窗口已关闭），保留 downloaded 状态待用户操作');
      });
    }
  });
  updater.on('error', (e) => {
    // 失败必须可见（§3.3）：人话进状态行，原文进 updater.log。
    const c = updaterCore.classifyError(e);
    updaterLogLine(`[error] ${c.detail}`);
    pushUpdaterStatus({ state: 'error',
      // 日志没落成盘时要跟着失败原因一起说（P2-43）—— 否则打包版只看到一句
      // 失败、下面「完整日志：」整行不显示，既拿不到原文也不知道为什么。
      text: updaterCore.errorStatusText(c.text, updaterLogError),
      detail: c.detail, logPath: updaterLogPath });
  });

  updaterLogLine(`[init] 更新链路启用：${updaterFeedUrl}（当前 v${app.getVersion()}，`
    + `启动 ${updaterCore.STARTUP_DELAY_MS / 1000}s 后首查，之后每 ${updaterCore.CHECK_INTERVAL_MS / 3600000}h）`);

  // 明文更新地址要摊到界面上：方案 §8 / §12.1 把 HTTPS 写成底线，
  // 而代码层面原来对降级到 http **完全沉默**（README 的本地冒烟示例恰是 http，
  // 用户很可能带进生产）。**不拒**（本地调试要用），但必须说出来。
  const feedWarn = updaterCore.feedUrlWarning(updaterFeedUrl);
  if (feedWarn) {
    updaterLogLine(`[init] ${feedWarn}`);
    pushUpdaterStatus({ warning: feedWarn });
  }

  // 启用态**先推一条 idle**：面板的常驻文案「自动更新已开启 · 每 6 小时检查一次」
  // 就是 idle 态（渲染层 `statusText` 的 idle 分支），它要让「有这个功能」本身可见
  // （§4.2 idle 行 / §14#12）。不推的话，用户打开面板看到的第一条状态一定是
  // checking —— 那句文案成了永不显示的死分支，而「本应用有自动更新」这件事
  // 在界面上没有任何常驻痕迹：只在出问题时才说话，等于只在坏消息里出现。
  pushUpdaterStatus({ state: 'idle' });

  // 15s 延迟首查，不跟引擎启动、页面加载抢冷启动（§4.1）；之后 6h 定时器——
  // 是定时不是轮询，两次检查之间零请求。
  updaterStartupTimer = setTimeout(() => {
    updaterStartupTimer = null;
    runCheck();
    updaterCheckTimer = setInterval(() => {
      // shouldCheckNow 守 6h 闭区间边界（含时钟回拨保护），纯函数有单测（§10）。
      // 睡眠 / 挂起让定时器漂移时，由它决定补不补查。
      if (updaterCore.shouldCheckNow(updaterLastCheckAt, Date.now())) runCheck();
    }, updaterCore.CHECK_INTERVAL_MS);
  }, updaterCore.STARTUP_DELAY_MS);
}

function clearUpdaterTimers() {
  if (updaterStartupTimer) { clearTimeout(updaterStartupTimer); updaterStartupTimer = null; }
  if (updaterCheckTimer) { clearInterval(updaterCheckTimer); updaterCheckTimer = null; }
}

function closeUpdater() {
  clearUpdaterTimers();
  closeUpdaterLog();
}

// 只读通道：取当前状态，**不触发检查**。
// 为什么必须单独开一条：面板打开时要的是「现在是什么状态」，不是「去查一次」。
// 复用 `updater:check` 会在启用态真的发一次请求，并把 6h 自动检查的时钟往后推
// —— `runCheck` 的第一件事就是 `updaterLastCheckAt = Date.now()`，于是
// 「每 6 小时一次」实际上取决于用户点开面板的频率（§1 / §4.1 写的是
// 「两次检查之间零请求、每 6h 一次」）。
// 状态本来就由主进程持有（`updaterStatus`），这里只是把当前值要回去。
ipcMain.handle('updater:getStatus', () => updaterStatus);

// 手动检查（设置页「检查更新」按钮）。结果永远必须可见，包括「没开」这个答案，
// 且三种「没开」文案不同（§4.3 三态表，updater-core.disabledStatus 有单测）。
ipcMain.handle('updater:check', async () => {
  const disabled = updaterCore.disabledStatus(
    app.isPackaged, updaterFeedUrl, !!process.env.PORTABLE_EXECUTABLE_DIR);
  if (disabled) {
    updaterLogLine(`[check] 手动检查：${disabled.text}`);
    pushUpdaterStatus({ ...disabled });
    return updaterStatus;
  }
  await runCheck();
  // runCheck resolve 时六事件已跑过（emit 在 doCheckForUpdates 的 resolve 之前），
  // 这里返回的就是最新状态。
  return updaterStatus;
});

// 「立即重启」按钮（设置页，只在 downloaded 态可见）。用户已经坐在设置页、
// 明确点了这个按钮，直接装、不再二次确认（§4.2 路径 2）—— 与路径 1
// （update-downloaded 时主进程弹确认框、默认焦点在「稍后」）触发条件不同。
// 守卫 updaterStatus.state：链路没启用（dev / portable / 没配地址）时
// updater 是 null，此时面板里根本没有这颗按钮，双保险。
ipcMain.handle('updater:restart', () => {
  if (updaterStatus.state !== 'downloaded') {
    updaterLogLine(`[install] 拒绝立即重启：当前状态是 ${updaterStatus.state}（不是 downloaded）`);
    return { ok: false, state: updaterStatus.state };
  }
  quitAndInstallUpdater();
  return { ok: true, state: updaterStatus.state };
});

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

/** 这个 URL 是不是**我们自己的引擎**（顶层导航只放行它）。
 *
 *  判据是「http + 127.0.0.1 + 当前 enginePort」。解析不出来也当**不放行**：
 *  宁可拦住一个合法导航（症状是界面卡住，一眼能发现），也不要放行一个非法导航
 *  （症状是 preload 被注入到远端页面，很难发现）。 */
function isEngineUrl(url) {
  try {
    const u = new URL(url);
    return u.protocol === 'http:' && u.hostname === '127.0.0.1'
      && String(u.port) === String(enginePort);
  } catch {
    return false;
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

  // ⚠ 上面那条只管「新窗口 / 新标签」，对**当前窗口导航**无效 ——
  //   `location.href = ...` / `location.assign(...)` / 普通 <a> 点击都不经过它。
  //   页面一旦被导航到远端源，preload 仍会在新页面注入 window.talkscript
  //   （updater.check / restart 直接可用），而用户丢失应用界面；
  //   CSP 的 `form-action 'none'` 拦不住 location.assign —— 只能在这里拦。
  win.webContents.on('will-navigate', (e, url) => {
    if (!isEngineUrl(url)) {
      e.preventDefault();
      logLine(`[nav] 拦截顶层导航（只放行引擎同源）：${maskSecrets(url)}`);
    }
  });

  // 「界面加载失败」的两个出口（did-fail-load 事件 + 初始 loadURL 的 rejection）
  // 共用一个一次性标记（P2-6）：事件先派发、promise 后 reject，同一个失败只许弹
  // 一次框 —— catch 看到事件已经报告过（含 ABORTED 这种被过滤的形态）就只留日志。
  let loadFailReported = false;
  const reportLoadFailure = (code, desc, url) => {
    if (loadFailReported) return;
    loadFailReported = true;
    // ⚠ `url` 就是 engineUrl()：`http://127.0.0.1:PORT/?token=<一次性令牌>`。
    // 修复前这里把它**原样**插进 showErrorBox —— 那是唯一一个不过脱敏的出口
    // （stdout / stderr / argv 三处都过了），于是一次加载失败就把访问令牌
    // 打进一个能截图、能全选复制、能贴进 issue 的系统对话框里（P2-5）。
    // 整条地址要留（端口是排查线索），令牌不留：文案构造收在 engine-dialogs.js，
    // 那里有断言守着「传进去的令牌值不可能出现在返回值里」。
    dialog.showErrorBox('界面加载失败',
      failLoadDetail({ desc, code, url, logPath: engineLogPath, token: engineToken }));
  };
  win.webContents.on('did-fail-load', (_e, code, desc, url, isMainFrame) => {
    if (quitting) return;
    // 主框架的任何失败（包括下面被过滤的 ABORTED）都算「这次加载的结局已被事件
    // 报告」：P2-6 的 rejection 出口据此决定要不要自己兜底弹框。
    if (isMainFrame) loadFailReported = true;
    // 子框架 / 子资源失败不弹框：界面照常能用，弹框只会吓人（P3）。
    if (!isMainFrame) return;
    // ERR_ABORTED(-3)：加载被**取代或打断**（又一次导航抢在前头 / 页面自己跳转），
    // 不是真的「加载不了」。不放行的话，正常操作也会弹失败框（P3）。
    if (code === -3) return;
    reportLoadFailure(code, desc, url);
  });

  // P2-6：初始 loadURL 的 promise 不许丢。引擎在「健康检查通过」与「加载完成」
  // 之间死掉的话这里会 reject —— 未接的 rejection 在 Node 20 下默认打崩主进程，
  // 失败形态从「对话框/日志留痕」恶化成「应用消失」（restartEngine 里的同名调用
  // 一直在 try/catch 里，唯独这第一次没有）。出口与 did-fail-load 一致但不重复
  // 弹窗：事件已报告过（loadFailReported）只补一行日志；事件没覆盖到的形态
  // （存疑：如渲染进程直接被杀）由这里用同一个出口兜底。
  loadFailReported = false;
  win.loadURL(engineUrl()).catch((e) => {
    // P2-6：事件没报告过的失败形态在这里兜底 —— 同一张框、同一份脱敏文案；
    // 已报告过的（did-fail-load 先到）只留一行日志，不重复弹窗。
    // 无论如何，「初始加载失败 + 原因」都要进引擎日志：对话框能被手滑关掉，日志不会。
    logLine(`[load] 初始界面加载失败：${(e && (e.message || e.code)) || e}`);
    if (!loadFailReported) {
      reportLoadFailure((e && e.code) || -1, (e && e.message) || String(e), engineUrl());
    }
  });
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
  // 健康检查的身份凭证（见 waitHealth）：**不只看 `r.ok`** —— 端口在「探测到空闲」
  // 与「引擎真正绑定」之间有窗口，被别的本机进程抢到并回 200 时，下面的 loadURL
  // 会把上面那个一次性令牌带进**那个进程**的请求行（`engine-path.js` 顶部论证的
  // 正是「令牌不能落到别的本机进程手里」）。nonce 只经子进程环境变量给引擎
  // （与令牌同一条通道），不知道它的进程伪造不了。
  // 允许环境变量覆盖：给集成测试一个注入点，也让手工起引擎调试时两边能对齐。
  engineHealthNonce = process.env.TALKSCRIPT_HEALTH_NONCE
    || crypto.randomBytes(16).toString('base64url');

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
    // 建议必须**跟着形态走**：打包态自带 Python 运行时（engine-path.js 降级链
    // 第 2 条 engine/py/python.exe），用户机器上不需要有 Python —— 让他去
    // `pip install -r requirements.txt` 既做不到、也不解决任何事（常见真因是
    // 杀毒软件把内嵌运行时隔离了、或安装包不完整）。那条路只对开发态成立。
    const advice = app.isPackaged
      ? '应用自带的运行时不完整，或被杀毒软件隔离。请重新安装 TalkScript；'
        + '若仍然失败，请把下面的日志文件发给我们。'
      : '请确认已安装依赖：pip install -r requirements.txt'
        + '（开发态也可用环境变量 TALKSCRIPT_PYTHON 指定解释器）';
    dialog.showErrorBox('引擎启动失败',
      `${lastErr}\n\n${advice}`
      + `\n\n完整日志：` + (engineLogPath || '（日志文件不可用）'));
    app.quit();
    return;
  }
  createWindow();
  // 更新链路在窗口出现后才初始化（§7）：15s 延迟首查在 initUpdater 里。
  // activate 重建窗口时不重复调用——单实例的 updater 与定时器已就位，
  // 重复 init 会双定时器双检查。
  initUpdater();
  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

// 退出钩子三处。updater 日志流（同步 append，见 updaterLogLine）**不在这两处关**：
// electron-updater 的退出安装钩子挂在 app.once('quit')，且实测它晚于 process 'exit'
// 派发——若在 before-quit / window-all-closed 收尾日志，会吞掉「Auto install update
// on quit」那行，而它是「退出时到底装没装」的唯一日志证据。定时器则必须在退出路径上
// 立刻清：退出过程中再触发一次 runCheck 会去打网络。
app.on('window-all-closed', () => { quitting = true; killEngine(); closeEngineLog(); clearUpdaterTimers(); app.quit(); });
app.on('before-quit', () => { quitting = true; killEngine(); closeEngineLog(); clearUpdaterTimers(); });
process.on('exit', () => { killEngine(); closeEngineLog(); closeUpdater(); });
