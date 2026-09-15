#!/usr/bin/env node
// 构建内嵌 Python 运行时 → desktop/vendor/py/（打包后是 resources/engine/py/）
//
// 为什么需要它
// ------------
// 安装包里必须**自带解释器**。修复前 `resolveEngine` 的第一优先级是
// `resources/engine/engine.exe`，但没有任何构建步骤产出它 —— 打包态只剩
// PATH 里的 `python`，而它未必装了 fastapi / uvicorn / httpx / pydantic / pyyaml。
// 于是 `npm run dist` 出来的安装包在没装过 Python 的机器上启动即失败，
// 还提示用户 `pip install -r requirements.txt` —— 对 NSIS 安装包来说不可接受。
// 详见 `代码审查报告-20260915.md` 的 P1-3。
//
// 做法：抄 WorkBuddy 的「zip 随包」形态，但用 **embeddable 免 pip 版**
// （压缩包十来 MB、解开几十 MB —— 实际值看 `du -sm desktop/vendor/py`，
// ⚠ 别抄数字进来，它会随 Python 版本与依赖变），且**不解压到用户目录**
// —— 运行时目录只读即可
// （`--data-dir` 已指向 userData，配置与产物都不写在这里）。
//
// 用法
// ----
//   node scripts/build-python-runtime.mjs            # 需要时下载/重建
//   node scripts/build-python-runtime.mjs --force    # 忽略缓存与 stamp 重建
//
// 零依赖：Node 没有内置 unzip，所以这里自己实现（只用到 node:zlib）；
// 代理走标准 `HTTPS_PROXY` / `https_proxy`（用 CONNECT 隧道，不依赖 curl）。

import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import zlib from 'node:zlib';
import http from 'node:http';
import https from 'node:https';
import tls from 'node:tls';
import { fileURLToPath } from 'node:url';
import { execFileSync } from 'node:child_process';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const DESKTOP = path.resolve(HERE, '..');
const REPO = path.resolve(DESKTOP, '..');

// ── 钉住的版本与校验值 ───────────────────────────────────────
// 改版本时**必须**同时更新 ZIP_SHA256 —— 校验失败时脚本会把实际值打出来。
// 哈希取自一次 TLS 验证过的 python.org 下载；官方在同一 URL 下另提供
// `.sigstore` 与 `.asc`（GPG），需要更强证明的人可以自行核验。
const PY_VERSION = '3.13.12';
const PY_TAG = '313';                     // → python313.zip / python313._pth
const ZIP_NAME = `python-${PY_VERSION}-embed-amd64.zip`;
const ZIP_URL = `https://www.python.org/ftp/python/${PY_VERSION}/${ZIP_NAME}`;
const ZIP_SHA256 = '76f238f606250c87c6beac75dccd35ee99070a13490555936abb6cb64ecce3d0';

const CACHE_DIR = path.join(DESKTOP, 'vendor', '.cache');
const ZIP_PATH = path.join(CACHE_DIR, ZIP_NAME);
const OUT_DIR = path.join(DESKTOP, 'vendor', 'py');
const SITE_PACKAGES = path.join(OUT_DIR, 'Lib', 'site-packages');
const STAMP = path.join(OUT_DIR, '.runtime-stamp.json');
const RUNTIME_REQ = path.join(REPO, 'requirements-runtime.txt');

const FORCE = process.argv.includes('--force');
const log = (...a) => console.log('[py-runtime]', ...a);

// ── 小工具 ──────────────────────────────────────────────────
function sha256File(p) {
  const h = crypto.createHash('sha256');
  const fd = fs.openSync(p, 'r');
  try {
    const buf = Buffer.allocUnsafe(1 << 20);
    for (;;) {
      const n = fs.readSync(fd, buf, 0, buf.length, null);
      if (!n) break;
      h.update(buf.subarray(0, n));
    }
  } finally {
    fs.closeSync(fd);
  }
  return h.digest('hex');
}

function human(bytes) {
  return bytes > 1 << 20 ? (bytes / (1 << 20)).toFixed(1) + ' MB'
                         : Math.ceil(bytes / 1024) + ' KB';
}

function dirSize(dir) {
  let total = 0;
  const walk = (d) => {
    for (const e of fs.readdirSync(d, { withFileTypes: true })) {
      const p = path.join(d, e.name);
      if (e.isDirectory()) walk(p);
      else if (e.isFile()) total += fs.statSync(p).size;
    }
  };
  if (fs.existsSync(dir)) walk(dir);
  return total;
}

// ── 下载（支持标准代理环境变量）─────────────────────────────
function proxyUrl() {
  return process.env.HTTPS_PROXY || process.env.https_proxy
      || process.env.HTTP_PROXY || process.env.http_proxy || '';
}

/** 发一个 GET，返回响应流。走代理时先 CONNECT 打隧道，再让 Node 的 HTTP
 *  机制在隧道上解析响应 —— 这样分块传输/头部解析都交给标准库，不用自己写。 */
function open(url, redirects = 0) {
  const u = new URL(url);
  const proxy = proxyUrl();
  const isHttps = u.protocol === 'https:';
  const headers = { Host: u.host, 'User-Agent': 'talkscript-build', Accept: '*/*',
                    Connection: 'close' };

  const request = (createConnection) => new Promise((resolve, reject) => {
    const mod = isHttps ? https : http;
    const req = mod.request({
      host: u.hostname, port: u.port || (isHttps ? 443 : 80),
      path: u.pathname + u.search, method: 'GET', headers, createConnection,
    });
    req.on('response', resolve);
    req.on('error', reject);
    req.end();
  });

  const viaProxy = (proxy && isHttps) ? new Promise((resolve, reject) => {
    const p = new URL(proxy);
    const c = http.request({ host: p.hostname, port: p.port, method: 'CONNECT',
                             path: `${u.hostname}:443`,
                             headers: { Host: `${u.hostname}:443` } });
    c.on('connect', (res, socket) => {
      if (res.statusCode !== 200) {
        reject(new Error(`代理拒绝 CONNECT（${res.statusCode}）：${proxy}`));
        return;
      }
      const secure = tls.connect({ socket, servername: u.hostname });
      resolve(() => secure);
    });
    c.on('error', reject);
    c.end();
  }) : Promise.resolve(null);

  return viaProxy.then((mk) => request(mk ? () => mk() : undefined))
    .then((res) => {
      if ([301, 302, 303, 307, 308].includes(res.statusCode)) {
        if (redirects >= 3) throw new Error('重定向次数过多：' + url);
        res.resume();
        return open(new URL(res.headers.location, url).toString(), redirects + 1);
      }
      if (res.statusCode !== 200) {
        res.resume();
        throw new Error(`下载失败 HTTP ${res.statusCode}：${url}`);
      }
      return res;
    });
}

async function download(url, dest) {
  fs.mkdirSync(path.dirname(dest), { recursive: true });
  const tmp = dest + '.part';
  const res = await open(url);
  const total = Number(res.headers['content-length'] || 0);
  await new Promise((resolve, reject) => {
    const out = fs.createWriteStream(tmp);
    let got = 0, lastPct = -1;
    res.on('data', (c) => {
      got += c.length;
      if (total) {
        const pct = Math.floor((got / total) * 100);
        if (pct >= lastPct + 20) { lastPct = pct; log(`  下载中 ${pct}%`); }
      }
    });
    res.pipe(out);
    out.on('finish', resolve);
    out.on('error', reject);
    res.on('error', reject);
  });
  fs.renameSync(tmp, dest);
  return dest;
}

// ── 解压（纯 Node，只用 zlib）───────────────────────────────
function unzip(zipPath, outDir) {
  const buf = fs.readFileSync(zipPath);
  // 从尾部往前找 EOCD（签名 0x06054b50，注释最长 64 KB）
  let eocd = -1;
  const floor = Math.max(0, buf.length - 22 - 0xffff);
  for (let i = buf.length - 22; i >= floor; i--) {
    if (buf.readUInt32LE(i) === 0x06054b50) { eocd = i; break; }
  }
  if (eocd < 0) throw new Error('不是有效的 zip：找不到中央目录结尾（EOCD）');
  const count = buf.readUInt16LE(eocd + 10);
  let off = buf.readUInt32LE(eocd + 16);

  let files = 0;
  for (let i = 0; i < count; i++) {
    if (buf.readUInt32LE(off) !== 0x02014b50) {
      throw new Error(`中央目录第 ${i} 条结构异常`);
    }
    const method = buf.readUInt16LE(off + 10);
    const compSize = buf.readUInt32LE(off + 20);
    const nameLen = buf.readUInt16LE(off + 28);
    const extraLen = buf.readUInt16LE(off + 30);
    const commentLen = buf.readUInt16LE(off + 32);
    const localOff = buf.readUInt32LE(off + 42);
    const name = buf.toString('utf8', off + 46, off + 46 + nameLen);
    off += 46 + nameLen + extraLen + commentLen;

    // 目录穿越防护：包里出现 ../ 或绝对路径一律拒绝（zip slip）
    const dest = path.resolve(outDir, name);
    if (!dest.startsWith(path.resolve(outDir) + path.sep)) {
      throw new Error(`压缩包条目越出目标目录：${name}`);
    }
    if (name.endsWith('/')) { fs.mkdirSync(dest, { recursive: true }); continue; }

    if (buf.readUInt32LE(localOff) !== 0x04034b50) {
      throw new Error(`本地头异常：${name}`);
    }
    const lNameLen = buf.readUInt16LE(localOff + 26);
    const lExtraLen = buf.readUInt16LE(localOff + 28);
    const start = localOff + 30 + lNameLen + lExtraLen;
    const raw = buf.subarray(start, start + compSize);
    const data = method === 0 ? raw
               : method === 8 ? zlib.inflateRawSync(raw)
               : (() => { throw new Error(`不支持的压缩方式 ${method}：${name}`); })();

    fs.mkdirSync(path.dirname(dest), { recursive: true });
    fs.writeFileSync(dest, data);
    files++;
  }
  return files;
}

// ── 写 `._pth` ──────────────────────────────────────────────
// ⚠ 这是内嵌发行版最容易踩的坑：它默认**禁用 `site`**，而且 `._pth` 存在时
// 进入 isolated 模式 —— **cwd 不进 `sys.path`**。于是 `python -m app.server`
// 直接报 `No module named 'app'`，而报错方向看起来像是「模块没写对」。
// 原版 `._pth` 只有 `python313.zip` / `.` 两行，`import site` 是注释掉的。
// 必须显式补上 `Lib\site-packages`（第三方包）与 `..`（相对 py/ 的 rootDir，
// 即 resources/engine —— `app/` 就在那儿），并取消 `import site` 的注释。
function writePth() {
  const p = path.join(OUT_DIR, `python${PY_TAG}._pth`);
  const lines = [
    `python${PY_TAG}.zip`,
    '.',
    'Lib\\site-packages',
    '..',
    'import site',
  ];
  fs.writeFileSync(p, lines.join('\n') + '\n', 'utf8');
  return p;
}

// ── 装依赖 ──────────────────────────────────────────────────
function findHostPython() {
  const cands = [
    process.env.TALKSCRIPT_BUILD_PYTHON,
    process.platform === 'win32'
      ? path.join(REPO, '.venv', 'Scripts', 'python.exe')
      : path.join(REPO, '.venv', 'bin', 'python'),
    'python3', 'python',
  ].filter(Boolean);
  for (const c of cands) {
    try {
      execFileSync(c, ['-c', 'import pip, sys'], { stdio: 'ignore' });
      return c;
    } catch (_) { /* 试下一个 */ }
  }
  throw new Error(
    '找不到带 pip 的宿主 Python。它只用于**构建**（往内嵌运行时里装依赖），' +
    '运行时不依赖它。可设 TALKSCRIPT_BUILD_PYTHON 指定，例如：\n' +
    '  set TALKSCRIPT_BUILD_PYTHON=C:\\Python313\\python.exe');
}

function installDeps() {
  const py = findHostPython();
  fs.mkdirSync(SITE_PACKAGES, { recursive: true });
  log(`  宿主 Python: ${py}`);
  // ⚠ `--python-version` **不能省**。宿主 Python 的版本与内嵌运行时未必相同
  //   （本项目开发机是 3.14，而运行时钉 3.13），不钉版本的话 pip 会按**宿主**
  //   的 ABI 挑 wheel —— 装进去 `pydantic_core-…-cp314-…whl`，
//   而 3.13 的解释器加载不了它。后果是：**构建显示成功、目录看着也正常
//   （几十 MB 都在），直到用户机器上才炸** `ModuleNotFoundError: No module named
  //   'pydantic_core._pydantic_core'`。这正是本项目一直在整治的静默降级。
  //   实测踩过：不钉版本时 5 个依赖里 2 个（pydantic_core / pyyaml）装错 ABI。
  //
  // `--only-binary=:all:` ：这 5 个依赖都有 wheel，禁止现场编译 ——
  // 否则一旦某个包只能从源码装，构建机会悄悄依赖上编译器，换台机器就挂。
  const pyVer = PY_VERSION.split('.').slice(0, 2).join('.');
  // `--no-compile`：**不让 pip 生成字节码**。原因见下面的 compileBytecode() ——
  // pip 是用**宿主**解释器编译 .pyc 的，`--python-version` 只管 wheel 的 ABI 标签、
  // 不管字节码版本。宿主比运行时新时，装出来的**整份**缓存运行时一个都用不上（纯死重）。
  // ⚠ **别在这儿抄体积/耗时数字**：抄进来就会过期（这个坑踩过两次）。
  //   要量就跑 `node _verify/pyc-cost.js`，数字以跑出来的为准。
  execFileSync(py, ['-m', 'pip', 'install', '--upgrade', '--target', SITE_PACKAGES,
                    '--python-version', pyVer, '--implementation', 'cp',
                    '--only-binary=:all:', '--no-cache-dir', '--no-compile',
                    '-r', RUNTIME_REQ],
               { stdio: 'inherit' });
}

/** 用**运行时自己的**解释器编译一遍字节码。
 *
 *  为什么必须自己编（实测踩到的第二个 ABI 类问题）：
 *  pip 编译 .pyc 用的是**正在跑 pip 的那个解释器**。开发机上是 3.14，
 *  而运行时是 3.13 —— 装出来的 `__pycache__` 全是 `cpython-314.pyc`，
 *  3.13 一个都认不了：这些缓存**一个都用不上**，纯死重。
 *
 *  ⚠ **不要声称它影响启动速度** —— 这条曾经写错过，别再改回去。
 *  单次测量能得到一个漂亮但假的数字（重建后磁盘冷缓存的假象）；换成
 *  「多次取最小值」的稳定测法后差异落在噪声内 —— 复测时甚至出现过
 *  **有缓存比无缓存还慢**的情况，方向都不稳定。
 *  **所以这条修的是「交付物里不该有错版本的内容」，不是性能。**
 *  ⚠ 同理别抄数字：要量跑 `node _verify/pyc-cost.js`（它会自己取最小值）。
 */
function compileBytecode() {
  const pyExe = path.join(OUT_DIR, 'python.exe');
  execFileSync(pyExe, ['-m', 'compileall', '-q', '-j', '0', 'Lib/site-packages'],
               { cwd: OUT_DIR, stdio: 'inherit' });
}

/** 断言 site-packages 里**没有**「不是本运行时版本」的 .pyc。
 *
 *  这条是上面那个坑的哨兵：只靠「构建成功」看不出来，得显式比对标签。
 *  万一将来有人把 `--no-compile` 去掉、或忘了调 compileBytecode，
 *  这里会直接报红，而不是等用户觉得「启动怎么有点慢」。
 */
function assertBytecodeMatchesRuntime() {
  const tag = `cpython-${PY_TAG}`;
  const bad = [];
  let good = 0;
  const walk = (d) => {
    for (const e of fs.readdirSync(d, { withFileTypes: true })) {
      const p = path.join(d, e.name);
      if (e.isDirectory()) { walk(p); continue; }
      if (!e.name.endsWith('.pyc')) continue;
      const m = e.name.match(/\.cpython-(\d+)/);
      if (!m) continue;
      if (`cpython-${m[1]}` === tag) good++;
      else bad.push(path.relative(OUT_DIR, p));
    }
  };
  walk(SITE_PACKAGES);
  if (bad.length) {
    throw new Error(
      `site-packages 里有 ${bad.length} 个版本对不上的 .pyc（运行时是 ${tag}），` +
      `前几个：${bad.slice(0, 3).join(' / ')}\n` +
      '  多半是 pip 用宿主解释器编译了字节码 —— 见 compileBytecode 的注释。');
  }
  if (good === 0) {
    throw new Error('site-packages 里一个可用的 .pyc 都没有 —— compileBytecode 没生效？');
  }
  log(`字节码校验通过：${good} 个 ${tag}.pyc，无版本错配`);
}

function copyDir(src, dest) {
  fs.mkdirSync(dest, { recursive: true });
  for (const e of fs.readdirSync(src, { withFileTypes: true })) {
    if (e.name === '__pycache__') continue;
    const s = path.join(src, e.name), d = path.join(dest, e.name);
    if (e.isDirectory()) copyDir(s, d);
    else fs.copyFileSync(s, d);
  }
}

/** 自检：用**运行时自己的**解释器把依赖与 `app.server` 都导一遍。
 *
 *  这是本脚本唯一能真正回答「装出来的东西能不能用」的一步。
 *  sha256 只保证 zip 没被换过、版本自检只保证解释器本身能跑 ——
 *  都管不了「依赖装错 ABI / 少装一个 / `._pth` 那几行没生效」。
 *  下面 `--python-version` 那个坑就是被这道自检逼出来的。
 *
 *  为什么要临时放一份 `app/`：`._pth` 里的 `..` 是**相对 `py/`** 的，
 *  打包后正好指向 `resources/engine/`（`app/` 就在那儿）。构建树里
 *  `py/` 的上一级是 `vendor/`，所以在那里临时铺一份，跑完就删 ——
 *  这样自检走的是**与打包后完全相同的相对路径**，而不是靠 PYTHONPATH 作弊。
 */
function verifyRuntime() {
  const pyExe = path.join(OUT_DIR, 'python.exe');
  const stage = path.resolve(OUT_DIR, '..');
  // 总是先删再铺：早先写成「已存在就跳过」，上一次跑崩留下的残留会让这次直接
  // 跳过、而且跳过之后也不会被清理，残留会一直攒着（踩过）。
  const staged = [];
  for (const name of ['app', 'packs']) {
    const dest = path.join(stage, name);
    fs.rmSync(dest, { recursive: true, force: true });
    copyDir(path.join(REPO, name), dest);
    staged.push(dest);
  }
  const probe = [
    'import fastapi, uvicorn, httpx, pydantic, yaml',
    'import app.server',
    'print("ok")',
  ].join('; ');
  let out;
  try {
    out = execFileSync(pyExe, ['-c', probe],
                       { cwd: stage, encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] });
  } catch (e) {
    throw new Error(
      '内嵌运行时的导入自检失败 —— 装出来的东西跑不起来，构建不算成功。\n' +
      `  ${(e.stderr || e.stdout || e.message).toString().trim()}\n` +
      '  常见原因：依赖 wheel 的 ABI 与钉住的 Python 版本不一致（见 installDeps 注释）。');
  } finally {
    for (const d of staged) fs.rmSync(d, { recursive: true, force: true });
  }
  if (!/ok/.test(out)) throw new Error('导入自检没有正常输出：' + out);
  log('导入自检通过：5 个依赖 + app.server 都能在内嵌运行时里导入');
}

// ── 主流程 ──────────────────────────────────────────────────
function stampFor() {
  return {
    python: PY_VERSION,
    zipSha256: ZIP_SHA256,
    requirements: sha256File(RUNTIME_REQ),
  };
}

function upToDate() {
  if (FORCE) return false;
  if (!fs.existsSync(path.join(OUT_DIR, 'python.exe'))) return false;
  if (!fs.existsSync(STAMP)) return false;
  try {
    const have = JSON.parse(fs.readFileSync(STAMP, 'utf8'));
    const want = stampFor();
    return Object.keys(want).every((k) => have[k] === want[k]);
  } catch (_) {
    return false;                     // stamp 读坏了就重建，不猜
  }
}

async function main() {
  if (upToDate()) {
    log(`已是最新（${PY_VERSION}，${human(dirSize(OUT_DIR))}），跳过。--force 可强制重建`);
    return;
  }

  // 1) 取 zip（有缓存就校验，不重新下载）
  let needDownload = true;
  if (fs.existsSync(ZIP_PATH)) {
    const got = sha256File(ZIP_PATH);
    if (got === ZIP_SHA256) { needDownload = false; log('缓存 zip 校验通过，跳过下载'); }
    else log(`缓存 zip 校验失败，重新下载（实际 ${got}）`);
  }
  if (needDownload) {
    log(`下载 ${ZIP_NAME} …`);
    await download(ZIP_URL, ZIP_PATH);
  }
  const got = sha256File(ZIP_PATH);
  if (got !== ZIP_SHA256) {
    throw new Error(
      `sha256 不匹配，拒绝使用：\n  期望 ${ZIP_SHA256}\n  实际 ${got}\n` +
      '如果确实是要升级 Python 版本，请把脚本里的 ZIP_SHA256 一起改掉。');
  }
  log(`sha256 校验通过（${ZIP_SHA256.slice(0, 16)}…）`);

  // 2) 解压
  fs.rmSync(OUT_DIR, { recursive: true, force: true });
  fs.mkdirSync(OUT_DIR, { recursive: true });
  const n = unzip(ZIP_PATH, OUT_DIR);
  log(`解压 ${n} 个条目 → ${path.relative(REPO, OUT_DIR)}`);

  // 3) 功能性校验：解出来的解释器**必须**就是钉住的那个版本。
  //    sha256 保证「文件没被换过」，这条保证「解出来的确实能跑」。
  const pyExe = path.join(OUT_DIR, 'python.exe');
  const ver = execFileSync(pyExe, ['-c', 'import sys; print(sys.version.split()[0])'],
                           { encoding: 'utf8' }).trim();
  if (ver !== PY_VERSION) {
    throw new Error(`解出来的解释器是 ${ver}，期望 ${PY_VERSION}`);
  }
  log(`解释器自检通过：python ${ver}`);

  // 4) `._pth` + 依赖
  writePth();
  log('写入 ._pth（stdlib zip / . / Lib\\site-packages / .. / import site）');
  log('安装运行依赖 …');
  installDeps();
  log('用运行时自己的解释器编译字节码 …');
  compileBytecode();
  assertBytecodeMatchesRuntime();
  verifyRuntime();

  // 5) 落 stamp
  fs.writeFileSync(STAMP, JSON.stringify({ ...stampFor(), builtAt: new Date().toISOString() },
                                         null, 1) + '\n', 'utf8');
  log(`完成：${human(dirSize(OUT_DIR))} → ${path.relative(REPO, OUT_DIR)}`);
}

main().catch((e) => {
  console.error('[py-runtime] 失败：' + (e.message || e));
  process.exit(1);
});
