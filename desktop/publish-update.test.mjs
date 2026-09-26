// publish-update 纯逻辑测试（Node 内置 test runner，零依赖）。
//
// 跑法：node --test desktop/publish-update.test.mjs
//
// 为什么要有它：发布脚本一跑就是「往线上推文件」，错了很难收。这里钉的七件事
// 都是「错了很安静」的规则——
//   1. parseLatestYml：yml 与安装包必须是同一次构建（size **与 sha512** 对账的前提，
//      旧 P3：只比 size 拦不住「同长度不同内容」），path: 行不能混进 files[]；
//   2. cmpVer：0.2.10 > 0.2.9 必须按数字比——字典序会判反，防降级形同虚设
//      （把旧包传上去 = 用户永远停在旧版且没有任何报错）；
//   3. joinUrl：文件名里的空格必须 %20——generic provider 客户端就这么拼，
//      服务端 HEAD 探测不同路就会漏掉「本地能下、线上 404」；
//   4. pickStale：被线上 latest.yml 引用的文件永不删（删了 = 客户端下到 404）；
//   5. dry-run 闭环（本节末）：首次发布线上 404 必须放行、防降级/拒重传/补传
//      判定必须各自开红或放行——这些语义活在 main() 里，只能真子进程 + 本地
//      HTTP 服务器整体验；
//   6. 场景 F：本地文件内容与 yml 的 sha512 对不上 → 步骤 1 拒传；
//   7. 场景 G：只连不答的服务器 → 超时报错退出，不无限挂起。
// 变异检验：joinUrl 去掉 encodeURIComponent / pickStale 排序退化成字典序 /
// cmpVer 字典序化 / 步骤 4 对 dry-run 也要求 200 → 这个文件里必须有断言变红
// ——守卫自己要能被红。
import { test } from 'node:test';
import assert from 'node:assert';
import crypto from 'node:crypto';
import { parseLatestYml, cmpVer, joinUrl, pickStale } from './scripts/publish-update.mjs';
import { spawn } from 'node:child_process';
import http from 'node:http';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

// 真实形状：electron-builder 25 生成的 latest.yml（旧版 files[] 格式，§12.2）
const YML = [
  'version: 0.2.1',
  'files:',
  '  - url: TalkScript Setup 0.2.1.exe',
  '    sha512: 9mIq0Xr0eJ7yFmYhZJ8Zk2v8s9v0u1w2x3y4z5a6b7c8d9e0f1g2h3i4j5k6l7m8n9o0p1q2r3s4t5u6v7w8x9y0zA1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q==',
  '    size: 100743424',
  '  - url: TalkScript Setup 0.2.1.exe.blockmap',
  '    sha512: aB3cD4eF5gH6iJ7kL8mN9oP0qR1sT2uV3wX4yZ5a6B7c8D9e0F1g2H3i4J5k6L7m8N9oP0qR1sT2uV3wX4yZ5a6B7c8D9e0F1g2H3i4J5k6L7m8N9oP0qR==',
  '    size: 115343',
  'path: TalkScript Setup 0.2.1.exe',
  'sha512: zZ9yY8xX7wW6vV5uU4tT3sS2rR1qQ0pP9oO8nN7mM6lL5kK4jJ3iI2hH1gG0fF9eE8dD7cC6bB5aA4==',
  'releaseDate: 2026-09-24T08:00:00.000Z',
].join('\n');

// ── parseLatestYml ───────────────────────────────────────────

test('真实 latest.yml：version / files[] / sizes 全对，path: 行不混进 files', () => {
  const m = parseLatestYml(YML);
  assert.strictEqual(m.version, '0.2.1');
  assert.deepStrictEqual(m.files, [
    'TalkScript Setup 0.2.1.exe',
    'TalkScript Setup 0.2.1.exe.blockmap',
  ]);
  // 每个 size 必须归到它上面那一项的 url 上（顺序回归：记错项 = 校验静默失效）
  assert.strictEqual(m.sizes['TalkScript Setup 0.2.1.exe'], 100743424);
  assert.strictEqual(m.sizes['TalkScript Setup 0.2.1.exe.blockmap'], 115343);
});

test('CRLF 换行同样解析（Windows 上被编辑器改过的 yml）', () => {
  const m = parseLatestYml(YML.replace(/\n/g, '\r\n'));
  assert.strictEqual(m.version, '0.2.1');
  assert.strictEqual(m.files.length, 2);
});

test('★ sha512 归到所属文件的 url 上（步骤 1 重算对账的依据，旧 P3）', () => {
  // 归属记错一格 = 拿 A 文件的哈希量 B 文件 → 好产物被拒传 / 坏产物放行
  const m = parseLatestYml(YML);
  assert.strictEqual(m.shas['TalkScript Setup 0.2.1.exe'],
    '9mIq0Xr0eJ7yFmYhZJ8Zk2v8s9v0u1w2x3y4z5a6b7c8d9e0f1g2h3i4j5k6l7m8n9o0p1q2r3s4t5u6v7w8x9y0zA1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q==');
  assert.strictEqual(m.shas['TalkScript Setup 0.2.1.exe.blockmap'],
    'aB3cD4eF5gH6iJ7kL8mN9oP0qR1sT2uV3wX4yZ5a6B7c8D9e0F1g2H3i4J5k6L7m8N9oP0qR1sT2uV3wX4yZ5a6B7c8D9e0F1g2H3i4J5k6L7m8N9oP0qR==');
  // 顶层 sha512（整包元数据）不进 files 的对账表：表里只有 files[] 两条
  assert.strictEqual(Object.keys(m.shas).length, 2);
});

test('注释行与空行跳过', () => {
  const m = parseLatestYml('# 手写注释\n\nversion: 0.2.2\n\n# files:\n');
  assert.strictEqual(m.version, '0.2.2');
  assert.deepStrictEqual(m.files, []);
});

test('读不出 version → null（不猜，调用方据此明确失败）', () => {
  assert.strictEqual(parseLatestYml('files:\n  - url: x.exe\n').version, null);
  assert.strictEqual(parseLatestYml('').version, null);
});

test('非字符串输入（undefined / 数字 / null）不炸，version 为 null', () => {
  assert.strictEqual(parseLatestYml(undefined).version, null);
  assert.strictEqual(parseLatestYml(123).version, null);
  assert.strictEqual(parseLatestYml(null).version, null);
});

test('url 带引号时去引号', () => {
  const m = parseLatestYml("version: '1.0.0'\nfiles:\n  - url: 'TalkScript Setup 1.0.0.exe'\n");
  assert.strictEqual(m.version, '1.0.0');
  assert.deepStrictEqual(m.files, ['TalkScript Setup 1.0.0.exe']);
});

// ── cmpVer（防降级的判据） ───────────────────────────────────

test('三段号常规比较', () => {
  assert.strictEqual(cmpVer('0.2.0', '0.2.1'), -1);
  assert.strictEqual(cmpVer('0.2.1', '0.2.0'), 1);
  assert.strictEqual(cmpVer('0.2.1', '0.2.1'), 0);
  assert.strictEqual(cmpVer('1.0.0', '0.9.9'), 1);
});

test('★ 0.2.10 > 0.2.9：必须按数字比，字典序会判反（防降级静默失效）', () => {
  // 字典序 '10' < '9' → 会把新版本判成旧版本 → 允许上传旧包覆盖线上新版
  assert.strictEqual(cmpVer('0.2.10', '0.2.9'), 1);
  assert.strictEqual(cmpVer('0.2.9', '0.2.10'), -1);
});

test('位数不齐时缺位按 0 补：0.2 === 0.2.0', () => {
  assert.strictEqual(cmpVer('0.2', '0.2.0'), 0);
  assert.strictEqual(cmpVer('0.2.0', '0.2'), 0);
});

test('预发布后缀不带 semver 语义：数字前缀相同即相等；纯非数字段退字典序', () => {
  // parseInt('1-beta') = 1，所以与 0.2.1 判等（不是更小）；语义「不管」但答案确定
  assert.strictEqual(cmpVer('0.2.1-beta', '0.2.1'), 0);
  assert.strictEqual(cmpVer('0.2.1-beta', '0.2.1-alpha'), 0);
  assert.strictEqual(cmpVer('0.2.beta', '0.2.alpha'), 1);   // 纯非数字段：字典序
});

// ── joinUrl（闭环验证的 URL 必须与客户端同一条拼法） ─────────

test('★ 文件名里的空格编码成 %20（generic provider 客户端就这么拼）', () => {
  assert.strictEqual(
    joinUrl('https://a.test/talkscript/', 'TalkScript Setup 0.2.1.exe'),
    'https://a.test/talkscript/TalkScript%20Setup%200.2.1.exe');
});

test('尾斜杠归一：多个/没有都不重复拼', () => {
  assert.strictEqual(joinUrl('https://a.test/talkscript', 'a.exe'),
    'https://a.test/talkscript/a.exe');
  assert.strictEqual(joinUrl('https://a.test/talkscript///', 'a.exe'),
    'https://a.test/talkscript/a.exe');
});

test('中文名也编码（decodeURIComponent 可逆，不丢字符）', () => {
  const u = joinUrl('https://a.test/t/', '安装包 0.2.1.exe');
  assert.ok(!u.includes(' '));
  assert.ok(u.includes('%'));
  assert.ok(decodeURIComponent(u.split('/t/')[1]).startsWith('安装包'));
});

test('文件名自带子路径段时 / 不被编掉（%2F 还原）', () => {
  assert.strictEqual(joinUrl('https://a.test/t/', 'sub/a.exe'),
    'https://a.test/t/sub/a.exe');
});

// ── pickStale（--keep 清理：删错 = 客户端 404） ───────────────

const V3 = [
  { version: '0.2.0', names: ['TalkScript Setup 0.2.0.exe', 'TalkScript Setup 0.2.0.exe.blockmap'] },
  { version: '0.2.1', names: ['TalkScript Setup 0.2.1.exe', 'TalkScript Setup 0.2.1.exe.blockmap'] },
  { version: '0.2.2', names: ['TalkScript Setup 0.2.2.exe', 'TalkScript Setup 0.2.2.exe.blockmap'] },
];

test('keep 2 三版 → 只删最旧那一组（exe + blockmap 成组删）', () => {
  assert.deepStrictEqual(pickStale(V3, 2, ['TalkScript Setup 0.2.2.exe']), [
    'TalkScript Setup 0.2.0.exe', 'TalkScript Setup 0.2.0.exe.blockmap',
  ]);
});

test('keep ≥ 版本数 → 一个都不删', () => {
  assert.deepStrictEqual(pickStale(V3, 3, []), []);
  assert.deepStrictEqual(pickStale(V3, 5, []), []);
});

test('★ 线上 latest.yml 正在引用的文件永不删（删了客户端下到 404）', () => {
  const doomed = pickStale(V3, 2, [
    'TalkScript Setup 0.2.2.exe',
    'TalkScript Setup 0.2.0.exe.blockmap',   // 旧版但仍被引用（回滚场景）
  ]);
  assert.ok(!doomed.includes('TalkScript Setup 0.2.0.exe.blockmap'));
  assert.deepStrictEqual(doomed, ['TalkScript Setup 0.2.0.exe']);
});

test('referenced 传数组也接得住', () => {
  const doomed = pickStale(V3, 2, ['TalkScript Setup 0.2.0.exe']);
  assert.deepStrictEqual(doomed, ['TalkScript Setup 0.2.0.exe.blockmap']);
});

test('keep 0 → 除 referenced 外全删（纯函数语义；脚本层 --keep 校验正整数）', () => {
  assert.deepStrictEqual(pickStale(V3, 0, ['TalkScript Setup 0.2.2.exe']).sort(), [
    'TalkScript Setup 0.2.0.exe',
    'TalkScript Setup 0.2.0.exe.blockmap',
    'TalkScript Setup 0.2.1.exe',
    'TalkScript Setup 0.2.1.exe.blockmap',
    'TalkScript Setup 0.2.2.exe.blockmap',
  ].sort());
});

test('★ 排序按版本号数字：0.2.10 是比 0.2.9 新的版本，keep 1 时留它', () => {
  // 字典序会把 0.2.10 判成最旧删掉、留下 0.2.9 —— 线上版本倒退
  const vs = [
    { version: '0.2.9', names: ['TalkScript Setup 0.2.9.exe'] },
    { version: '0.2.10', names: ['TalkScript Setup 0.2.10.exe'] },
  ];
  assert.deepStrictEqual(pickStale(vs, 1, []), ['TalkScript Setup 0.2.9.exe']);
});

// ── dry-run 闭环行为（真子进程 + 本地 HTTP 服务器） ─────────────
//
// 上面四个函数是纯逻辑；dry-run 的闭环语义活在 main() 的步骤 2/4 里，只能把
// 脚本当黑盒整体跑。五个场景各钉一条「错了很安静」的规则：
//   A 首次发布（线上 404）→ dry-run 必须正常收尾。这里曾无条件要求 200，
//     「发布前先 dry-run 检查」永远走不通（脚本步骤 4 的注释记着这个坑）；
//   B 线上版本更新 → 防降级开红：传旧包 = 所有用户无声停在旧版；
//   C 线上同版本且文件完整 → 拒绝重传：同 version 两份不同的包，客户端缓存打架；
//   D 线上同版本但缺文件 → 判定「上次上传中断」，允许补传；
//   E 线上更旧 → 放行，步骤 4 如实报线上版本、提示上传后应变成什么。
// 「不上传、不删任何文件」也有执行证据：--to 指向不存在的主机，A/D/E 仍全程
// 退出码 0——真去连 scp/ssh 只会 die。哪一步改了语义，对应的场景就红。

const HERE_DIR = path.dirname(fileURLToPath(import.meta.url));
const SCRIPT = path.join(HERE_DIR, 'scripts', 'publish-update.mjs');
const PKG_VERSION = JSON.parse(fs.readFileSync(path.join(HERE_DIR, 'package.json'), 'utf8')).version;
const EXE = `TalkScript Setup ${PKG_VERSION}.exe`;
const BM = `${EXE}.blockmap`;
const EXE_SIZE = 2048;
const BM_SIZE = 512;

// 造一个「刚 npm run dist 完」的 dist/：yml 与文件同一次构建（size 对得上，
// sha512 也必须对得上 —— 步骤 1 现在用 yml 里的 sha512 与本地重算比对，旧 P3）。
// yml 里写的 sha512 一律**真算**：假哈希会被新守卫当「不同源的产物」拒传。
function makeDistDir() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'publish-test-dist-'));
  fs.writeFileSync(path.join(dir, EXE), 'x'.repeat(EXE_SIZE));
  fs.writeFileSync(path.join(dir, BM), 'b'.repeat(BM_SIZE));
  const sha = (s) => crypto.createHash('sha512').update(s).digest('base64');
  fs.writeFileSync(path.join(dir, 'latest.yml'), [
    `version: ${PKG_VERSION}`,
    'files:',
    `  - url: ${EXE}`,
    `    sha512: ${sha('x'.repeat(EXE_SIZE))}`,
    `    size: ${EXE_SIZE}`,
    `  - url: ${BM}`,
    `    sha512: ${sha('b'.repeat(BM_SIZE))}`,
    `    size: ${BM_SIZE}`,
    `path: ${EXE}`,
    `sha512: ${'c'.repeat(86)}==`,
    'releaseDate: 2026-09-24T08:00:00.000Z',
  ].join('\n'));
  return dir;
}

// 本地假更新服务器。routes 键 = 'METHOD 解码后路径'（注意前导 /），值 = () => ({status, body?})
function makeServer(routes) {
  const server = http.createServer((req, res) => {
    const key = `${req.method} ${decodeURIComponent(req.url)}`;
    const h = routes[key];
    if (!h) { res.statusCode = 404; res.end(); return; }
    const r = h();
    res.statusCode = r.status;
    if (r.body !== undefined) res.setHeader('content-length', Buffer.byteLength(r.body));
    res.end(req.method === 'HEAD' ? undefined : r.body);
  });
  return new Promise((resolve) => server.listen(0, '127.0.0.1', () => resolve(server)));
}

// 子进程必须用异步 spawn：HTTP 服务器就搭在本进程的事件循环上，spawnSync 会把
// 父进程事件循环堵死 → 子进程连上后等响应、父进程等子进程退出，死锁（实测挂住）。
// timeout 是硬闸：哪天语义被改坏，这里是「断言红 / 超时失败」，不是整套测试挂死。
// extraEnv：个别场景要注环境变量（场景 G 的超时毫秒数），发布脚本的命令行参数面
// 保持最小，测试注入走 env。
function dryRun(port, dir, timeoutMs = 20000, extraEnv = {}) {
  return new Promise((resolve, reject) => {
    const t0 = Date.now();
    const c = spawn(process.execPath, [
      SCRIPT, '--to', 'nobody@127.0.0.1:/nonexistent', '--url', `http://127.0.0.1:${port}/`,
      '--dry-run', '--dir', dir,
    ], { env: { ...process.env, ...extraEnv } });
    let out = '', err = '';
    const timer = setTimeout(() => {
      c.kill();
      reject(new Error(`dry-run ${timeoutMs}ms 未退出（死锁？）\nstdout:\n${out}\nstderr:\n${err}`));
    }, timeoutMs);
    c.stdout.on('data', (d) => { out += d; });
    c.stderr.on('data', (d) => { err += d; });
    c.on('error', (e) => { clearTimeout(timer); reject(e); });
    c.on('close', (code) => {
      clearTimeout(timer);
      resolve({ status: code, out, err, ms: Date.now() - t0 });
    });
  });
}

test('A ★ 首次发布：线上 404 → dry-run 正常收尾（曾在此无条件要求 200）', async (t) => {
  const dir = makeDistDir();
  const server = await makeServer({ 'GET /latest.yml': () => ({ status: 404 }) });
  t.after(() => { server.closeAllConnections?.(); server.close(); fs.rmSync(dir, { recursive: true, force: true }); });
  const r = await dryRun(server.address().port, dir);
  assert.strictEqual(r.status, 0, `stdout:\n${r.out}\nstderr:\n${r.err}`);
  assert.ok(r.out.includes('这是第一次发布'), r.out);
  assert.ok(r.out.includes('符合首次发布'), r.out);
  assert.ok(r.out.includes('dry-run：跳过上传'), r.out);
  assert.strictEqual(r.err, '');
});

test('B ★ 线上版本更新 → 防降级开红（exit 1，明说无声回退）', async (t) => {
  const dir = makeDistDir();
  const remote = ['version: 9.9.9', 'files:', `  - url: ${EXE}`, `path: ${EXE}`].join('\n');
  const server = await makeServer({ 'GET /latest.yml': () => ({ status: 200, body: remote }) });
  t.after(() => { server.closeAllConnections?.(); server.close(); fs.rmSync(dir, { recursive: true, force: true }); });
  const r = await dryRun(server.address().port, dir);
  assert.strictEqual(r.status, 1, `stdout:\n${r.out}\nstderr:\n${r.err}`);
  assert.ok(r.err.includes('防降级'), r.err);
  assert.ok(r.err.includes('无声回退'), r.err);
});

test('C ★ 线上同版本且文件完整 → 拒绝重传（exit 1）', async (t) => {
  const dir = makeDistDir();
  const remote = [`version: ${PKG_VERSION}`, 'files:', `  - url: ${EXE}`, `  - url: ${BM}`].join('\n');
  const server = await makeServer({
    'GET /latest.yml': () => ({ status: 200, body: remote }),
    [`HEAD /${EXE}`]: () => ({ status: 200 }),
    [`HEAD /${BM}`]: () => ({ status: 200 }),
  });
  t.after(() => { server.closeAllConnections?.(); server.close(); fs.rmSync(dir, { recursive: true, force: true }); });
  const r = await dryRun(server.address().port, dir);
  assert.strictEqual(r.status, 1, `stdout:\n${r.out}\nstderr:\n${r.err}`);
  assert.ok(r.err.includes('无需重传'), r.err);
});

test('D 线上同版本但缺文件 → 判定上传中断，允许补传（exit 0）', async (t) => {
  const dir = makeDistDir();
  const remote = [`version: ${PKG_VERSION}`, 'files:', `  - url: ${EXE}`, `  - url: ${BM}`].join('\n');
  const server = await makeServer({
    'GET /latest.yml': () => ({ status: 200, body: remote }),
    [`HEAD /${EXE}`]: () => ({ status: 404 }),
    [`HEAD /${BM}`]: () => ({ status: 200 }),
  });
  t.after(() => { server.closeAllConnections?.(); server.close(); fs.rmSync(dir, { recursive: true, force: true }); });
  const r = await dryRun(server.address().port, dir);
  assert.strictEqual(r.status, 0, `stdout:\n${r.out}\nstderr:\n${r.err}`);
  assert.ok(r.out.includes('允许补传'), r.out);
  assert.ok(r.out.includes('本次要传的文件线上暂不存在属正常'), r.out);
});

test('E 线上更旧 → 放行，步骤 4 报线上版本并提示上传后应变成它', async (t) => {
  const dir = makeDistDir();
  const remote = ['version: 0.0.1', 'files:', `  - url: ${EXE}`].join('\n');
  const server = await makeServer({ 'GET /latest.yml': () => ({ status: 200, body: remote }) });
  t.after(() => { server.closeAllConnections?.(); server.close(); fs.rmSync(dir, { recursive: true, force: true }); });
  const r = await dryRun(server.address().port, dir);
  assert.strictEqual(r.status, 0, `stdout:\n${r.out}\nstderr:\n${r.err}`);
  assert.ok(r.out.includes('允许上传'), r.out);
  assert.ok(r.out.includes(`本次是 ${PKG_VERSION}，上传后这里应变成它`), r.out);
});

test('F ★ 本地文件与 latest.yml 的 sha512 对不上 → 步骤 1 拒传（旧 P3：只比 size 拦不住同长度篡改）', async (t) => {
  const dir = makeDistDir();
  // 事后把 exe 内容换掉但**长度不变**：size 对账完全放行，只有 sha512 重算能抓到。
  // 这种包传上去 = 客户端下载后 sha512 校验不过 → 更新永远失败且报错难懂。
  fs.writeFileSync(path.join(dir, EXE), 'y'.repeat(EXE_SIZE));
  const server = await makeServer({ 'GET /latest.yml': () => ({ status: 404 }) });
  t.after(() => { server.closeAllConnections?.(); server.close(); fs.rmSync(dir, { recursive: true, force: true }); });
  const r = await dryRun(server.address().port, dir);
  assert.strictEqual(r.status, 1, `stdout:\n${r.out}\nstderr:\n${r.err}`);
  assert.ok(r.err.includes('校验产物'), r.err);
  assert.ok(r.err.includes('sha512'), r.err);
  assert.ok(r.err.includes(EXE), `没点名是哪个文件：${r.err}`);
});

test('G ★ 只连不答的服务器 → 超时报错退出（旧 P3：httpRequest 无超时会无限挂起）', async (t) => {
  const dir = makeDistDir();
  // 收下连接但永不响应：模拟防火墙黑洞 / 服务器 hang。请求没有「应答」可等，
  // 只有整体超时能救 —— 这正是超时必须按「整个请求完成」计时的原因。
  const server = http.createServer(() => { /* 故意不 end */ });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  t.after(() => { server.closeAllConnections?.(); server.close(); fs.rmSync(dir, { recursive: true, force: true }); });
  const t0 = Date.now();
  // PUBLISH_HTTP_TIMEOUT_MS：脚本的单请求超时可用环境变量覆盖（默认 30s），
  // 测试缩到 600ms —— 要验的是「超时会报错退出」，不是等 30s。
  const r = await dryRun(server.address().port, dir, 20000, { PUBLISH_HTTP_TIMEOUT_MS: '600' });
  assert.strictEqual(r.status, 1, `stdout:\n${r.out}\nstderr:\n${r.err}`);
  assert.ok(r.err.includes('超时'), r.err);
  assert.ok(Date.now() - t0 < 10000,
    `超时没生效：脚本挂了 ${Date.now() - t0}ms 才退出（或测试自身超时）`);
});
