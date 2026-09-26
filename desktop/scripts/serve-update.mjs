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

// ── 服务器本体（导出给单测：起真服务器打真请求） ────────────────

/**
 * 起一个支持 Range 的静态服务器。
 * @param {string} root 目录（绝对/相对都接，内部 resolve）
 * @param {number} port 传 0 = 随机空闲端口（测试用）
 * @param {string} bind
 * @returns {Promise<{port: number, url: string, close: () => Promise<void>}>}
 */
export function startServer(root, port = 8788, bind = '127.0.0.1') {
  const dir = path.resolve(root);
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
      log(200, req, 'HEAD');
      return;
    }
    const r = parseRange(req.headers.range, st.size);
    if (r === null) {
      res.writeHead(200, { ...base, 'Content-Length': st.size });
      fs.createReadStream(file).pipe(res);
      log(200, req, `${st.size} 字节全量`);
    } else if (r === 'unsatisfiable') {
      res.writeHead(416, { ...base, 'Content-Range': `bytes */${st.size}` }).end();
      log(416, req, 'Range 越界');
    } else {
      const len = r.end - r.start + 1;
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
  console.log(`${TAG} 服务目录 ${dir}`);
  console.log(`${TAG} 地址 ${srv.url}（端口 ${srv.port}）`);
  console.log(`${TAG} 差分链只能在这个服务器上验（python -m http.server 不支持 Range，会把差分静默跳全量）`);
  console.log(`${TAG} 客户端：TALKSCRIPT_UPDATE_URL=${srv.url} 重启应用；Ctrl+C 停止`);
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
