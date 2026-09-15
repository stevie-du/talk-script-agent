// 内嵌运行时的端到端冒烟（零依赖）。
//
// 跑法：
//   node _verify/runtime-smoke.js
//   node _verify/runtime-smoke.js --root desktop/dist/win-unpacked/resources/engine
//     ↑ 直接验**打出来的安装产物**（`npm run dist` 之后），不是构建树
//
//   前置（不带 --root 时）：先构建运行时 ——
//     node desktop/scripts/build-python-runtime.mjs
//
// 验证什么
// --------
// `desktop/scripts/build-python-runtime.mjs` 自己的导入自检只证明「能 import」。
// 这个工具按**打包后的真实布局**把引擎真正跑起来：
//
//   <root>/                 ← 打包后对应 resources/engine/
//     app/  packs/  renderer/
//     py/                   ← 内嵌运行时（`._pth` 里的 `..` 指向这一层）
//
// 然后断言：
//   1. `sys.path` 里确实有 `._pth` 写的那四项（stdlib zip / py / site-packages / ..）
//      —— 这是内嵌发行版最容易踩的坑，不验就只能等用户报错
//   2. 引擎能起来，`/api/health` 通
//   3. `/api/meta` 能下发行业包与参数（证明 `..` 那条路径真的能读到 packs/）
//   4. 真跑一次生成（mock 夹具），走完 参数归一 → 选题 → 撰写 → 校验 → 落盘
//   5. **清掉 PYTHONPATH / PYTHONHOME / VIRTUAL_ENV 之后依然能起来** ——
//      否则「自带运行时」可能只是借了宿主环境的光，换台机器就废
'use strict';
const fs = require('fs');
const os = require('os');
const path = require('path');
const net = require('net');
const http = require('http');
const { spawn, execFileSync } = require('child_process');

const REPO = path.resolve(__dirname, '..');
const VENDOR = path.join(REPO, 'desktop', 'vendor');

// --root：直接验一个已经成型的 engine 目录（打出来的产物）。此时**不铺也不删**
// 任何东西 —— 那个目录是交付物，冒烟只读它。
const argIdx = process.argv.indexOf('--root');
const GIVEN_ROOT = argIdx > 0 ? path.resolve(process.argv[argIdx + 1]) : null;
const ROOT = GIVEN_ROOT || VENDOR;
const PY = path.join(ROOT, 'py', 'python.exe');

const results = [];
const check = (name, ok, detail) => {
  results.push([name, !!ok, detail || '']);
  console.log(`${ok ? '✅' : '❌'}  ${name}${detail ? '  — ' + detail : ''}`);
};

function freePort() {
  return new Promise((res, rej) => {
    const s = net.createServer();
    s.on('error', rej);
    s.listen(0, '127.0.0.1', () => { const p = s.address().port; s.close(() => res(p)); });
  });
}

function get(url, headers) {
  return new Promise((resolve, reject) => {
    const req = http.get(url, { headers: headers || {} }, (r) => {
      let b = '';
      r.on('data', (c) => (b += c));
      r.on('end', () => resolve({ status: r.statusCode, body: b }));
    });
    req.on('error', reject);
    req.setTimeout(3000, () => req.destroy(new Error('超时')));
  });
}

function post(url, body, headers) {
  return new Promise((resolve, reject) => {
    const data = JSON.stringify(body);
    const u = new URL(url);
    const req = http.request({ host: u.hostname, port: u.port, path: u.pathname,
                               method: 'POST',
                               headers: { 'Content-Type': 'application/json',
                                          'Content-Length': Buffer.byteLength(data),
                                          ...(headers || {}) } },
                             (r) => {
      let b = '';
      r.on('data', (c) => (b += c));
      r.on('end', () => resolve({ status: r.statusCode, body: b }));
    });
    req.on('error', reject);
    req.setTimeout(5000, () => req.destroy(new Error('超时')));
    req.write(data);
    req.end();
  });
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

function killTree(pid) {
  if (!pid) return;
  try { execFileSync('taskkill', ['/PID', String(pid), '/T', '/F'], { stdio: 'ignore' }); }
  catch (_) { /* 已退出 */ }
}

(async function main() {
  if (!fs.existsSync(PY)) {
    console.error(`❌ 找不到内嵌运行时：${path.relative(REPO, PY)}\n` +
                  (GIVEN_ROOT
                    ? '   --root 指向的目录里没有 py/python.exe'
                    : '   先跑：node desktop/scripts/build-python-runtime.mjs'));
    process.exit(2);
  }
  console.log(`目标：${path.relative(REPO, ROOT) || ROOT}` +
              (GIVEN_ROOT ? '（已成型产物，只读）' : '（构建树，会临时铺 app/packs/renderer）'));

  // ── 1) sys.path：`._pth` 到底生效没有 ────────────────────
  const pathOut = execFileSync(PY, ['-c', 'import sys; print("\\n".join(sys.path))'],
                               { encoding: 'utf8' });
  const entries = pathOut.split(/\r?\n/).filter(Boolean);
  const has = (pred) => entries.some(pred);
  check('sys.path 含 stdlib zip（python313.zip）', has((p) => /python3\d+\.zip$/i.test(p)),
        entries[0] || '');
  check('sys.path 含 Lib\\site-packages（第三方依赖）',
        has((p) => /Lib[\\/]site-packages$/i.test(p)));
  // `..` 那条：指向 py/ 的上一级，也就是打包后的 resources/engine
  check('sys.path 含 py/ 的上一级（app/ 在那儿）',
        has((p) => path.resolve(p) === path.resolve(ROOT)));

  // ── 1b) 字节码缓存的版本标签必须与解释器一致 ──────────────
  // pip 编译 .pyc 用的是**跑 pip 的那个解释器**（开发机 3.14），而运行时是 3.13
  // —— 装出来的 `__pycache__` 全是 `cpython-314.pyc`，3.13 一个都认不了。
  // 后果是发行包里躺着一整份**永远用不上**的字节码（错版本那份）。
  // ⚠ **不要声称它影响启动速度** —— 多次取最小值测下来差异在噪声内。
  // ⚠ **别把体积/耗时数字抄进注释**：每次跑都可能变（踩过两次）。
  //   要量就跑 `node _verify/pyc-cost.js`，以跑出来的为准。
  // 这条修的是「交付物里有错版本的内容」，以及防止将来有人把编译步骤去掉。
  const tag = execFileSync(PY, ['-c', 'import sys; print(sys.implementation.cache_tag)'],
                           { encoding: 'utf8' }).trim();   // 例如 cpython-313
  const pyc = { good: 0, bad: [] };
  const walkPyc = (d) => {
    for (const e of fs.readdirSync(d, { withFileTypes: true })) {
      const p = path.join(d, e.name);
      if (e.isDirectory()) { walkPyc(p); continue; }
      const m = e.name.endsWith('.pyc') && e.name.match(/\.(cpython-\d+)\.pyc$/);
      if (!m) continue;
      if (m[1] === tag) pyc.good++;
      else pyc.bad.push(path.relative(ROOT, p));
    }
  };
  const sitePkgs = path.join(ROOT, 'py', 'Lib', 'site-packages');
  if (fs.existsSync(sitePkgs)) walkPyc(sitePkgs);
  check(`site-packages 里的 .pyc 全部匹配 ${tag}（没有别的版本的死重）`,
        pyc.bad.length === 0,
        pyc.bad.length ? `${pyc.bad.length} 个错配，如 ${pyc.bad.slice(0, 2).join(' / ')}`
                       : `${pyc.good} 个`);
  check('确实存在可用的字节码缓存（不是零个）', pyc.good > 0, `${pyc.good} 个`);

  // ── 2) 按打包布局起引擎 ──────────────────────────────────
  // 不带 --root 时：`desktop/vendor/` 是构建用的暂存目录（已 gitignore），
  // `app` / `packs` / `renderer` 都是**铺过来的副本**，跑完就删。所以这里
  // **总是**先删再铺 —— 早先写成「已存在就跳过」，上一次跑崩留下的残留会让这次
  // 直接跳过，而跳过之后 `staged` 是空的、清理也不删它，残留一直攒着。
  //
  // 带 --root 时（验已成型产物）：目录里本来就什么都有，**一个字都不动**。
  const staged = [];
  if (!GIVEN_ROOT) {
    for (const name of ['app', 'packs', 'renderer']) {
      const dest = path.join(VENDOR, name);
      fs.rmSync(dest, { recursive: true, force: true });
      copyDir(path.join(REPO, name === 'renderer' ? 'desktop/renderer' : name), dest);
      staged.push(dest);
    }
  }
  const port = await freePort();
  const token = 'smoke-token';
  // 清掉宿主 Python 的环境变量：自带的运行时不该借宿主的光。
  const env = { ...process.env, PYTHONIOENCODING: 'utf-8' };
  delete env.PYTHONPATH;
  delete env.PYTHONHOME;
  delete env.VIRTUAL_ENV;
  // 走 mock 夹具跑一次真生成 —— 不碰模型、不需要 Key。
  env.TALKSCRIPT_MOCK = '1';
  // data-dir 一律放临时目录：打包后的安装目录通常**不可写**，
  // 而且往交付物里写数据会把产物弄脏。
  const dataDir = fs.mkdtempSync(path.join(os.tmpdir(), 'ts-smoke-data-'));

  const proc = spawn(PY, ['-m', 'app.server', '--port', String(port),
                          '--root', ROOT, '--data-dir', dataDir,
                          '--token', token, '--version', '0.0.0-smoke'],
                     { cwd: ROOT, env, stdio: ['ignore', 'pipe', 'pipe'] });
  let out = '';
  proc.stdout.on('data', (d) => (out += d));
  proc.stderr.on('data', (d) => (out += d));

  try {
    let health = null;
    const deadline = Date.now() + 40000;
    while (Date.now() < deadline) {
      try {
        const r = await get(`http://127.0.0.1:${port}/api/health`);
        if (r.status === 200) { health = JSON.parse(r.body); break; }
      } catch (_) { /* 还没起来 */ }
      await new Promise((r) => setTimeout(r, 300));
    }
    check('引擎能起来（/api/health 通）', health && health.ok === true,
          health ? JSON.stringify(health) : out.trim().split('\n').slice(-3).join(' | '));

    if (health) {
      // `/api/*` 一律要带一次性令牌（`/api/health` 是唯一免鉴权的）。
      // 不带的话拿到的是 401 —— 那会伪装成「没有行业包」，把断言带偏。
      const auth = { 'X-TalkScript-Token': token };
      const metaRes = await get(`http://127.0.0.1:${port}/api/meta`, auth);
      check('/api/meta 鉴权通过（带令牌）', metaRes.status === 200,
            `HTTP ${metaRes.status}`);
      const meta = metaRes.status === 200 ? JSON.parse(metaRes.body) : {};
      check('/api/meta 能读到行业包（证明 ._pth 的 `..` 生效）',
            Array.isArray(meta.packs) && meta.packs.length > 0,
            `packs=${(meta.packs || []).map((p) => p.name).join(',') || '(空)'}`);
      check('/api/meta 下发参数条与审计',
            !!(meta.packs || []).find((p) => p.name === 'elevator')?.params,
            '');

      // ── 真跑一次生成：这才是「装完能用」的最终证据 ─────────
      // 前面几条只证明「引擎能起来、能读包」；这条要走完
      // 参数归一 → 选题 → 撰写 → 校验 → 组装落盘 的全链路。
      const gen = await post(`http://127.0.0.1:${port}/api/generate`,
                             { pack: 'elevator', topic: '被困电梯怎么办', mode: 'auto' },
                             auth);
      let jobId = null;
      try { jobId = JSON.parse(gen.body).job_id; } catch (_) { /* 见下 */ }
      check('提交生成作业被接受', gen.status === 200 && !!jobId,
            `HTTP ${gen.status} ${gen.body.slice(0, 120)}`);

      if (jobId) {
        let snap = null;
        const until = Date.now() + 90000;
        while (Date.now() < until) {
          const r = await get(`http://127.0.0.1:${port}/api/jobs/${jobId}`, auth);
          snap = JSON.parse(r.body);
          if (snap.state === 'done' || snap.state === 'failed') break;
          await new Promise((x) => setTimeout(x, 500));
        }
        check('生成跑到 done（全链路可用）', snap?.state === 'done',
              snap ? `state=${snap.state} ${snap.error || ''}` : '拿不到快照');
        check('产物有分段文案与字数配额',
              (snap?.result?.sections || []).length > 0 && !!snap?.result?.quota?.total,
              snap?.result ? `sections=${snap.result.sections.length} ` +
                             `quota=${snap.result.quota.total}` : '');
      }
    }
  } finally {
    killTree(proc.pid);
    for (const d of staged) fs.rmSync(d, { recursive: true, force: true });
    fs.rmSync(dataDir, { recursive: true, force: true });
  }

  const bad = results.filter((r) => !r[1]).length;
  console.log(`\n${results.length - bad}/${results.length} 通过`);
  process.exit(bad ? 1 : 0);
})().catch((e) => {
  console.error('❌ 冒烟失败：' + (e.stack || e.message || e));
  process.exit(1);
});
