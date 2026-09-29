#!/usr/bin/env node
// 冒烟用的静态更新服务器：把一个目录（默认 desktop/dist）按 generic provider
// 的形态 serve 出来，**支持 Range/206**。
//
// 为什么不能只用 python -m http.server
// ------------------------------------
// 它不支持 Range（实测对 Range 请求回 200 全量）。差分下载的每个块请求都是
// Range，落在它上面会被静默跳成全量——看着「下载成功」，其实差分链一行都没验
// （方案 §10 差分链）。这个服务器把 206 做对，并且每个请求打一行日志，
// 差分验证时能直接看出客户端到底发了什么、拉了多少字节。
//
// 用法
// ----
//   cd desktop
//   node scripts/serve-update.mjs                  # serve dist/，端口 8788
//   node scripts/serve-update.mjs ./fake-0.2.1 --port 9000
//
//   客户端另开终端：
//   setx TALKSCRIPT_UPDATE_URL "http://127.0.0.1:8788/"   # 重启应用后生效
//
// 零依赖：只用 node 内置模块。

import fs from 'node:fs';
import http from 'node:http';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const TAG = '[serve]';

const MIME = {
  '.yml': 'text/yaml; charset=utf-8',
  '.yaml': 'text/yaml; charset=utf-8',
  '.blockmap': 'application/octet-stream',
  '.exe': 'application/octet-stream',
  '.nupkg': 'application/octet-stream',
};

function usage() {
  console.log(`用法：
  node scripts/serve-update.mjs [目录] [--port N] [--bind 地址]

  [目录]       要 serve 的目录（默认 desktop/dist）
  --port N     监听端口（默认 8788）
  --bind 地址  监听地址（默认 127.0.0.1；只绑本机，别默认暴露全网卡）
  --help       打印本段
`);
}

// ── Range 解析（导出给单测：这是整个服务器存在的理由） ──────────

/**
 * 解析 Range 头。
 * @param {string|undefined} header
 * @param {number} size 文件字节数
 * @returns {{start: number, end: number}|'unsatisfiable'|null}
 *   对象         合法范围（end 已 clamp 到 size-1，含两端）
 *   'unsatisfiable'  start 越过文件末尾 → 该回 416
 *   null         不认这个 Range（多段 / 坏格式 / suffix 超长 / 逆序）
 *                → 忽略 Range 回 200 全量
 */
export function parseRange(header, size) {
  const m = /^bytes=(\d*)-(\d*)$/.exec(String(header == null ? '' : header).trim());
  if (!m) return null;                     // 多段（bytes=0-1,3-4）/ 别的单位 / 坏格式
  const a = m[1], b = m[2];
  let start, end;
  if (a === '') {
    if (b === '') return null;
    start = size - Number(b);              // suffix：最后 N 字节
    end = size - 1;
  } else {
    start = Number(a);
    end = b === '' ? size - 1 : Number(b); // bytes=100- → 到末尾
  }
  if (!Number.isInteger(start) || !Number.isInteger(end)) return null;
  if (start >= size) return 'unsatisfiable';
  if (start < 0) return null;              // suffix 比文件还长 → 全量，HTTP 规范同
  end = Math.min(end, size - 1);           // clamp：bytes=100-999 对 200 字节文件
  if (end < start) return null;            // 逆序（bytes=100-50）是非法头，忽略
  return { start, end };
}

// ── 起飞前的差分前提自检（2026-09-27 修 P1-15）────────────────────
// 冒烟的真正判据是「差分有没有生效」，而差分**失效时客户端不报错**：拿不到
// blockmap 就回退全量，版本号照样变、安装照样成功 —— 看着"通过"了，差分一行
// 都没验。而 blockmap 恰恰是唯一**不在** latest.yml 里的一项（builder ≥27 的
// files[] 只列 exe，客户端自己按 "<exe URL>.blockmap" 拼），所以照着真 yml
// 的形状造 fake-feed，最容易漏的就是它。
// 这里把它变成**起服务时就说**：缺了就喊出来，而不是让人验完才发现白验。
//
// 只做这一个文件形状的核对，不做完整性校验（那是 publish-update 的活）：
// 这里要回答的是"这条差分链**可能**根本走不通吗"。

/**
 * 从 latest.yml 里取出 `files:` 段列出的 url（只认行首的 `- url:`）。
 * 刻意不引 YAML 依赖：这个服务器是零依赖的，而这里要的只是那一个字段。
 * @param {string} yml
 * @returns {string[]}
 */
export function feedUrls(yml) {
  const out = [];
  let inFiles = false;
  for (const raw of String(yml).split(/\r?\n/)) {
    if (/^files:\s*$/.test(raw)) { inFiles = true; continue; }
    if (inFiles && /^\S/.test(raw) && raw.trim() !== '') break;   // files 段结束
    if (!inFiles) continue;
    const m = /^\s*-\s*url:\s*(.+?)\s*$/.exec(raw);
    if (m) out.push(m[1].replace(/^['"]|['"]$/g, ''));
  }
  return out;
}

/**
 * 起飞前自检差分链的前提。
 * @param {string} dir 更新源目录
 * @returns {{exe: string[], problems: string[], notes: string[]}}
 *   problems —— 缺东西，差分**一定**走不通（起服务时要喊出来）
 *   notes   —— 形态上说得通但值得提一句的
 */
export function auditFeed(dir) {
  const problems = [];
  const notes = [];
  const ymlPath = path.join(dir, 'latest.yml');
  let yml = '';
  try { yml = fs.readFileSync(ymlPath, 'utf8'); } catch { /* 读不到当没有 */ }
  if (!yml) {
    problems.push('目录里没有 latest.yml —— 客户端连「有没有新版本」都问不到');
    return { exe: [], problems, notes };
  }
  const urls = feedUrls(yml);
  const exes = urls.filter(u => /\.exe$/i.test(u));
  if (!exes.length && urls.length) {
    notes.push('latest.yml 的 files[] 里一个 .exe 都没有 —— 这不是自动更新该有的形状');
  }
  for (const u of exes) {
    if (!fs.existsSync(path.join(dir, u))) {
      problems.push(`latest.yml 列的 ${u} 不在目录里`);
      continue;
    }
    // 差分的基准文件：客户端自己按 "<exe URL>.blockmap" 拼，不看 files[]。
    if (!fs.existsSync(path.join(dir, u + '.blockmap'))) {
      problems.push(`${u} 没有配套的 .blockmap —— 差分会被静默跳成全量`
        + `（latest.yml 里也不会列它，只能靠目录里真有这么一个文件）`);
    }
  }
  if (!problems.length && exes.length) {
    notes.push(`差分前提齐了：${exes.length} 个安装包都带 .blockmap`);
  }
  return { exe: exes, problems, notes };
}

// ── 服务器本体（导出给单测：起真服务器打真请求） ────────────────

/**
 * 起一个支持 Range 的静态服务器。
 * @param {string} root 目录（绝对/相对都接，内部 resolve）
 * @param {number} port 传 0 = 随机空闲端口（测试用）
 * @param {string} bind
 * @returns {Promise<{port: number, url: string, close: () => Promise<void>,
 *   tally: () => {requests: number, rangeRequests: number, bytes: number}}>}
 */
export function startServer(root, port = 8788, bind = '127.0.0.1') {
  const dir = path.resolve(root);
  // 停机时能报一句"这次一共服务了多少 Range 请求、多少字节"：
  // README 把「差分拉取量远小于全量」写成冒烟的验收线，而那条线靠数请求才量得准 ——
  // 光看"装成功了"分不出差分与全量（两者都是成功）。
  const tally = { requests: 0, rangeRequests: 0, bytes: 0 };
  const server = http.createServer((req, res) => {
    if (req.method !== 'GET' && req.method !== 'HEAD') {
      res.writeHead(405, { Allow: 'GET, HEAD' }).end('method not allowed');
      return;
    }
    let name;
    try {
      name = decodeURIComponent(new URL(req.url, 'http://x').pathname).replace(/^\/+/, '');
    } catch {
      res.writeHead(400).end('bad percent-encoding');
      return;
    }
    // 防穿越：URL 层会规范化 "../"，但 %2e%2e 不会，decode 后才现形。
    const file = path.normalize(path.join(dir, name));
    if (file !== dir && !file.startsWith(dir + path.sep)) {
      log(403, req, '路径穿越被拒');
      res.writeHead(403).end('forbidden');
      return;
    }
    let st = null;
    try { st = fs.statSync(file); } catch { /* 不存在 */ }
    if (!st || !st.isFile()) {
      log(404, req, '文件不存在');
      res.writeHead(404).end('not found');
      return;
    }
    const type = MIME[path.extname(file).toLowerCase()] || 'application/octet-stream';
    const base = { 'Content-Type': type, 'Accept-Ranges': 'bytes' };
    if (req.method === 'HEAD') {
      res.writeHead(200, { ...base, 'Content-Length': st.size }).end();
      tally.requests++;
      log(200, req, 'HEAD');
      return;
    }
    const r = parseRange(req.headers.range, st.size);
    tally.requests++;
    if (r === null) {
      tally.bytes += st.size;
      res.writeHead(200, { ...base, 'Content-Length': st.size });
      fs.createReadStream(file).pipe(res);
      log(200, req, `${st.size} 字节全量`);
    } else if (r === 'unsatisfiable') {
      res.writeHead(416, { ...base, 'Content-Range': `bytes */${st.size}` }).end();
      log(416, req, 'Range 越界');
    } else {
      const len = r.end - r.start + 1;
      tally.rangeRequests++;
      tally.bytes += len;
      res.writeHead(206, {
        ...base,
        'Content-Range': `bytes ${r.start}-${r.end}/${st.size}`,
        'Content-Length': len,
      });
      fs.createReadStream(file, { start: r.start, end: r.end }).pipe(res);
      log(206, req, `Range bytes=${r.start}-${r.end}（${len}/${st.size} 字节）`);
    }
  });
  return new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(port, bind, () => {
      resolve({
        port: server.address().port,
        url: `http://${bind}:${server.address().port}/`,
        close: () => new Promise((r) => server.close(r)),
        tally: () => ({ ...tally }),
      });
    });
  });
}

function log(code, req, extra) {
  console.log(`${TAG} ${code} ${req.method} ${req.url}${extra ? ' — ' + extra : ''}`);
}

// ── 主流程 ────────────────────────────────────────────────────
async function main() {
  const argv = process.argv.slice(2);
  let root = null, port = 8788, bind = '127.0.0.1';
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === '--help' || a === '-h') { usage(); process.exit(0); }
    if (a === '--port') { port = Number(argv[++i]); continue; }
    if (a === '--bind') { bind = argv[++i]; continue; }
    if (a.startsWith('-')) {
      console.error(`${TAG} 不认识的参数 ${a}\n`);
      usage();
      process.exit(1);
    }
    root = a;
  }
  if (!Number.isInteger(port) || port < 1 || port > 65535) {
    console.error(`${TAG} --port 要 1..65535 的整数，收到 ${port}`);
    process.exit(1);
  }
  const dir = path.resolve(root || path.join(HERE, '..', 'dist'));
  if (!fs.existsSync(dir) || !fs.statSync(dir).isDirectory()) {
    console.error(`${TAG} 目录不存在：${dir}\n  先 npm run dist，或把要伪装的目录路径传进来`);
    process.exit(1);
  }
  // 差分前提自检：缺 blockmap 时差分会**静默**回退全量，冒烟照样"通过"。
  // 宁可起服务时红字喊出来，也不要让人验完才发现这一趟白验（P1-15）。
  const feed = auditFeed(dir);
  for (const n of feed.notes) console.log(`${TAG} ${n}`);
  for (const p of feed.problems) console.error(`${TAG} ⚠ ${p}`);
  if (feed.problems.length) {
    console.error(`${TAG}   —— 这一趟验不了差分：客户端会退成全量下载，`
      + `而"装成功了"分不出差分与全量。补齐上面的文件再验。`);
  }
  let srv;
  try {
    srv = await startServer(dir, port, bind);
  } catch (e) {
    if (e && e.code === 'EADDRINUSE') {
      console.error(`${TAG} 端口 ${port} 已被占用：换个 --port，或先关掉占它的程序`);
    } else {
      console.error(`${TAG} 启动失败：${(e && e.message) || e}`);
    }
    process.exit(1);
  }
  const exeBytes = feed.exe
    .map((u) => { try { return fs.statSync(path.join(dir, u)).size; } catch { return 0; } })
    .reduce((a, b) => a + b, 0);
  console.log(`${TAG} 服务目录 ${dir}`);
  console.log(`${TAG} 地址 ${srv.url}（端口 ${srv.port}）`);
  console.log(`${TAG} 差分链只能在这个服务器上验（python -m http.server 不支持 Range，会把差分静默跳全量）`);
  console.log(`${TAG} 客户端：TALKSCRIPT_UPDATE_URL=${srv.url} 重启应用；Ctrl+C 停止`);
  console.log(`${TAG} 验收线：装完之后看下面这行 —— Range 请求数 > 0 且传输字节远小于安装包 `
    + `（${exeBytes} 字节）；客户端日志里出现 "fallback to full download" 就是差分没走通`);
  // 停机时把这次一共服务了多少、传了多少报出来：验收线要看的正是这两个数。
  const report = () => {
    const t = srv.tally();
    console.log(`${TAG} 本次共 ${t.requests} 个请求（Range ${t.rangeRequests} 个），`
      + `传输 ${t.bytes} 字节${exeBytes ? `，占安装包的 ${(t.bytes * 100 / exeBytes).toFixed(1)}%` : ''}`);
  };
  process.on('SIGINT', () => { report(); process.exit(0); });
  process.on('SIGTERM', () => { report(); process.exit(0); });
}

// 直接执行才跑；被 import（单测）时不启动 —— 否则一 import 就开始监听端口。
const invokedDirectly = process.argv[1] !== undefined
  && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url);
if (invokedDirectly) {
  main().catch((e) => {
    console.error(`${TAG} 未预期的失败：${(e && e.stack) || e}`);
    process.exit(1);
  });
}
