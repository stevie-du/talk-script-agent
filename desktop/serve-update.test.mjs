// serve-update 测试（Node 内置 test runner，零依赖）。
//
// 跑法：node --test desktop/serve-update.test.mjs
//
// 为什么要有它：这个服务器存在的**全部理由**是 Range/206——python -m
// http.server 不支持 Range，差分下载在它上面会被静默跳成全量（方案 §10）。
// 所以断言分两层：
//   ① parseRange 纯函数三态（合法 / 越界 / 不认）；
//   ② 起真服务器打真请求：206 的 Content-Range 对、HEAD 不发体、404、
//      路径穿越被拒、带空格的真实产物名能下。
// 变异检验：parseRange 恒返 null / 删掉 206 分支 → 这个文件里必须有断言变红。
import { test } from 'node:test';
import assert from 'node:assert';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import http from 'node:http';
import { parseRange, startServer } from './scripts/serve-update.mjs';

// 临时目录交给系统清；fixture 在模块级建一次，所有测试共享。
const TMP = fs.mkdtempSync(path.join(os.tmpdir(), 'ts-serve-'));
process.on('exit', () => fs.rmSync(TMP, { recursive: true, force: true }));

const BIG = Buffer.alloc(200);
for (let i = 0; i < BIG.length; i++) BIG[i] = i % 256;
fs.writeFileSync(path.join(TMP, 'hello.bin'), BIG);
// 复刻真实产物名：带空格的 blockmap（客户端请求时是 %20）
fs.writeFileSync(path.join(TMP, 'TalkScript Setup 0.2.1.exe.blockmap'), Buffer.alloc(50, 0xAB));
fs.writeFileSync(path.join(TMP, 'latest.yml'), 'version: 0.2.1\n');
fs.mkdirSync(path.join(TMP, 'sub'));
fs.writeFileSync(path.join(TMP, 'sub', 'nested.bin'), Buffer.alloc(30, 7));
// 目录外的哨兵：路径穿越测试要能证明它拿不到
const SECRET = path.join(os.tmpdir(), `ts-serve-secret-${path.basename(TMP)}.txt`);
fs.writeFileSync(SECRET, 'TOP-SECRET');
process.on('exit', () => fs.rmSync(SECRET, { force: true }));

function get(port, urlPath, opts = {}) {
  return new Promise((resolve, reject) => {
    const req = http.get({
      host: '127.0.0.1', port, path: urlPath,
      method: opts.method || 'GET', headers: opts.headers || {},
    }, (res) => {
      const chunks = [];
      res.on('data', (c) => chunks.push(c));
      res.on('end', () => resolve({
        status: res.statusCode, headers: res.headers, body: Buffer.concat(chunks),
      }));
    });
    req.on('error', reject);
  });
}

// ── parseRange 纯函数 ────────────────────────────────────────

test('合法闭区间 bytes=0-99 → {0,99}', () => {
  assert.deepStrictEqual(parseRange('bytes=0-99', 200), { start: 0, end: 99 });
});

test('bytes=100-（开到尾）→ end = size-1', () => {
  assert.deepStrictEqual(parseRange('bytes=100-', 200), { start: 100, end: 199 });
});

test('suffix bytes=-50 → 最后 50 字节', () => {
  assert.deepStrictEqual(parseRange('bytes=-50', 200), { start: 150, end: 199 });
});

test('end 越过文件末尾 → clamp 到 size-1（不 416，客户端常这么拉最后一块）', () => {
  assert.deepStrictEqual(parseRange('bytes=100-999', 200), { start: 100, end: 199 });
});

test('start 越过文件末尾 → unsatisfiable（该回 416，不能默默给全量）', () => {
  assert.strictEqual(parseRange('bytes=200-', 200), 'unsatisfiable');
  assert.strictEqual(parseRange('bytes=999-', 200), 'unsatisfiable');
});

test('多段 Range bytes=0-1,3-4 → null（不支持就明说，回 200 全量）', () => {
  assert.strictEqual(parseRange('bytes=0-1,3-4', 200), null);
});

test('坏格式（bananas / bytes=abc-def / 空）→ null', () => {
  assert.strictEqual(parseRange('bananas', 200), null);
  assert.strictEqual(parseRange('bytes=abc-def', 200), null);
  assert.strictEqual(parseRange('', 200), null);
  assert.strictEqual(parseRange(undefined, 200), null);
  assert.strictEqual(parseRange(null, 200), null);
});

test('suffix 比文件还长 → null（全量，HTTP 规范同）', () => {
  assert.strictEqual(parseRange('bytes=-500', 200), null);
});

test('逆序 bytes=100-50 → null（非法头，忽略）', () => {
  assert.strictEqual(parseRange('bytes=100-50', 200), null);
});

// ── 真服务器（端口 0 = 随机空闲端口，测试之间互不干扰） ────────

test('全量 GET：200 + Accept-Ranges + 完整 body', async () => {
  const srv = await startServer(TMP, 0);
  try {
    const r = await get(srv.port, '/hello.bin');
    assert.strictEqual(r.status, 200);
    assert.strictEqual(r.headers['accept-ranges'], 'bytes');
    assert.strictEqual(r.body.length, 200);
    assert.ok(r.body.equals(BIG));
  } finally { await srv.close(); }
});

test('Range GET：206 + Content-Range 头正确 + body 是对的那一段', async () => {
  const srv = await startServer(TMP, 0);
  try {
    const r = await get(srv.port, '/hello.bin', { headers: { Range: 'bytes=0-99' } });
    assert.strictEqual(r.status, 206);
    assert.strictEqual(r.headers['content-range'], 'bytes 0-99/200');
    assert.strictEqual(r.body.length, 100);
    assert.ok(r.body.equals(BIG.subarray(0, 100)));
  } finally { await srv.close(); }
});

test('Range 开区间与 suffix 在真请求上也对', async () => {
  const srv = await startServer(TMP, 0);
  try {
    const a = await get(srv.port, '/hello.bin', { headers: { Range: 'bytes=150-' } });
    assert.strictEqual(a.status, 206);
    assert.strictEqual(a.headers['content-range'], 'bytes 150-199/200');
    assert.ok(a.body.equals(BIG.subarray(150)));

    const b = await get(srv.port, '/hello.bin', { headers: { Range: 'bytes=-20' } });
    assert.strictEqual(b.status, 206);
    assert.strictEqual(b.headers['content-range'], 'bytes 180-199/200');
    assert.ok(b.body.equals(BIG.subarray(180)));
  } finally { await srv.close(); }
});

test('HEAD：200 + Content-Length，body 为空（闭环验证/探测靠它）', async () => {
  const srv = await startServer(TMP, 0);
  try {
    const r = await get(srv.port, '/hello.bin', { method: 'HEAD' });
    assert.strictEqual(r.status, 200);
    assert.strictEqual(r.headers['content-length'], '200');
    assert.strictEqual(r.body.length, 0);
  } finally { await srv.close(); }
});

test('带空格的真实产物名：%20 解码后能下（客户端 generic provider 就这么拼）', async () => {
  const srv = await startServer(TMP, 0);
  try {
    const r = await get(srv.port, '/TalkScript%20Setup%200.2.1.exe.blockmap');
    assert.strictEqual(r.status, 200);
    assert.strictEqual(r.body.length, 50);
  } finally { await srv.close(); }
});

test('子目录里的文件也能 serve', async () => {
  const srv = await startServer(TMP, 0);
  try {
    const r = await get(srv.port, '/sub/nested.bin');
    assert.strictEqual(r.status, 200);
    assert.strictEqual(r.body.length, 30);
  } finally { await srv.close(); }
});

test('latest.yml 以 text/yaml 下发（electron-updater 要 GET 它）', async () => {
  const srv = await startServer(TMP, 0);
  try {
    const r = await get(srv.port, '/latest.yml');
    assert.strictEqual(r.status, 200);
    assert.match(r.headers['content-type'], /text\/yaml/);
    assert.match(r.body.toString('utf8'), /version: 0\.2\.1/);
  } finally { await srv.close(); }
});

test('不存在的文件 → 404', async () => {
  const srv = await startServer(TMP, 0);
  try {
    const r = await get(srv.port, '/nope.exe');
    assert.strictEqual(r.status, 404);
  } finally { await srv.close(); }
});

test('路径穿越：%2e%2e 被 URL 层规范化（404），..%2f 触到服务端防线（403），目录外文件一个字都取不到', async () => {
  const srv = await startServer(TMP, 0);
  try {
    // 形态一：%2e%2e 是 URL 规范的 double-dot 段，解析时就被消掉 → 变成找 TMP 里的
    // 一个不存在文件 → 404。永远到不了目录外。
    const a = await get(srv.port, `/%2e%2e/${path.basename(SECRET)}`);
    assert.strictEqual(a.status, 404);
    assert.ok(!a.body.toString('utf8').includes('TOP-SECRET'));
    // 形态二：%2f 不被 URL 当路径分隔符，decode 后才现形 → normalize 后落在目录外 → 403
    const b = await get(srv.port, `/..%2f${path.basename(SECRET)}`);
    assert.strictEqual(b.status, 403);
    assert.ok(!b.body.toString('utf8').includes('TOP-SECRET'));
  } finally { await srv.close(); }
});

test('多段 Range 真请求 → 200 全量（不认就不打 206，别假装支持）', async () => {
  const srv = await startServer(TMP, 0);
  try {
    const r = await get(srv.port, '/hello.bin', { headers: { Range: 'bytes=0-1,3-4' } });
    assert.strictEqual(r.status, 200);
    assert.strictEqual(r.body.length, 200);
  } finally { await srv.close(); }
});

test('越界 Range → 416 + Content-Range: bytes */size', async () => {
  const srv = await startServer(TMP, 0);
  try {
    const r = await get(srv.port, '/hello.bin', { headers: { Range: 'bytes=999-' } });
    assert.strictEqual(r.status, 416);
    assert.strictEqual(r.headers['content-range'], 'bytes */200');
  } finally { await srv.close(); }
});

test('非 GET/HEAD（PUT）→ 405', async () => {
  const srv = await startServer(TMP, 0);
  try {
    const r = await new Promise((resolve, reject) => {
      const req = http.request({ host: '127.0.0.1', port: srv.port, path: '/hello.bin', method: 'PUT' }, (res) => {
        res.resume();
        res.on('end', () => resolve(res.statusCode));
      });
      req.on('error', reject);
      req.end('x');
    });
    assert.strictEqual(r, 405);
  } finally { await srv.close(); }
});
