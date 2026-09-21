// 引擎启动命令解析。
//
// 为什么单独一个文件
// ------------------
// 这段逻辑**与 Electron 无关**，唯一的耦合是 `app.isPackaged` / `app.getPath`。
// 放在 main.js 里就只能靠读码来确认「降级链的顺序对不对」——
// 而顺序恰恰是 P1-3 的核心（出厂运行时必须排在 PyInstaller 产物之前）。
// 拆出来之后 `node --test` 就能直接验，不用起 Electron。
//
// 降级链（**顺序 = 我们期望用户跑哪个**，不是「哪条路先被写出来」）
// ------------------------------------------------------------------
//   1. TALKSCRIPT_PYTHON       显式覆盖。开发态指 .venv、排障时指别的解释器。
//   2. engine/py/python.exe    **出厂自带运行时**（P1-3 的产物，
//                              由 desktop/scripts/build-python-runtime.mjs 生成）。
//                              它是唯一「装完就能用」的那条 —— 不依赖用户机器上
//                              有没有 Python、有没有装这 5 个依赖。
//   3. engine/engine.exe       PyInstaller 形态。本项目**没走这条路**：
//                              bootloader 会把 traceback 变形，而内嵌运行时的
//                              报错与开发态一字不差。保留只是为了兼容旧产物。
//   4. 项目 .venv              开发态（npm start）。
//   5. PATH 里的 python        最后兜底；此时依赖装没装只能听天由命。
//
// 2 排在 3 之前是**有意的**：如果哪天有人往 resources/engine 里同时放了两种产物，
// 该跑的是我们认真做的那条，不是「先被写出来的」那条。

'use strict';
const path = require('path');
const fs = require('fs');

/**
 * @param {object} o
 * @param {string} o.rootDir       引擎根目录（打包后 = resources/engine）
 * @param {string} o.resourcesDir  Electron 的 resources 目录
 * @param {string} o.dataDir       可写数据目录（打包后 = userData）
 * @param {string} o.version       版本号（唯一来源是 desktop/package.json）
 * @param {number} o.port
 * @param {string} o.token
 * @param {number} [o.parentPid]   Electron 主进程 PID；传了引擎就会在它退出时自杀
 *                                 （不传 = 不启用看门狗，命令行手动起引擎时用）
 * @param {string} [o.platform]    默认 process.platform
 * @param {object} [o.env]         默认 process.env
 * @param {function} [o.exists]    默认 fs.existsSync —— 测试用，避免真建文件
 * @returns {{cmd: string, args: string[], via: string}} via 是命中了哪一条（日志/断言用）
 */
function resolveEngine(o) {
  const platform = o.platform || process.platform;
  const env = o.env || process.env;
  const exists = o.exists || fs.existsSync;
  const win = platform === 'win32';

  const engineArgs = ['--port', String(o.port), '--root', o.rootDir,
                      '--data-dir', o.dataDir, '--token', o.token,
                      '--version', o.version];
  // 看门狗：主进程被强杀/崩溃时 Windows 不会连带杀掉 python.exe，
  // 于是每次崩溃都留一份引擎常驻（占端口 + 占内存，而且 /api/health 还会回 200，
  // 下一次启动可能被**旧引擎**应答）。传了 parent-pid，引擎自己就会退。
  if (o.parentPid) engineArgs.push('--parent-pid', String(o.parentPid));
  // 用 `python -m app.server` 跑时参数要多一个 `-m app.server`；
  // engine.exe 是已经封好的可执行文件，不带。
  const moduleArgs = ['-m', 'app.server', ...engineArgs];

  const custom = env.TALKSCRIPT_PYTHON;
  if (custom) return { cmd: custom, args: moduleArgs, via: 'TALKSCRIPT_PYTHON' };

  const bundledPy = win
    ? path.join(o.resourcesDir, 'engine', 'py', 'python.exe')
    : path.join(o.resourcesDir, 'engine', 'py', 'bin', 'python3');
  if (exists(bundledPy)) {
    return { cmd: bundledPy, args: moduleArgs, via: 'engine/py（出厂运行时）' };
  }

  const bundled = path.join(o.resourcesDir, 'engine', 'engine.exe');
  if (exists(bundled)) return { cmd: bundled, args: engineArgs, via: 'engine.exe' };

  const venvPy = win
    ? path.join(o.rootDir, '.venv', 'Scripts', 'python.exe')
    : path.join(o.rootDir, '.venv', 'bin', 'python');
  if (exists(venvPy)) return { cmd: venvPy, args: moduleArgs, via: '.venv' };

  return { cmd: win ? 'python' : 'python3', args: moduleArgs, via: 'PATH' };
}

module.exports = { resolveEngine };
