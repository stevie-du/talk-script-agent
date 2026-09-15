/**
 * 量「字节码缓存」的真实代价 —— 体积 + 启动耗时。
 *
 * 为什么要有这个工具：**实测数字曾经被抄进注释和 docstring，然后过期了两次**
 * （完整经过见报告的「教训 36」）。根因不是记性差，是
 * **数字写在自然语言里就一定会和多份副本脱节** —— 同一个量复测两次都不一样。
 * 所以现在数字只有一个来源：**跑这个脚本**。注释里只许指路，不许抄值。
 *
 * 用法：
 *   node _verify/pyc-cost.js                 # 用默认路径，自动探测另一个版本的解释器
 *   node _verify/pyc-cost.js --host <py314>  # 指定「错的那一版」解释器
 *
 * 输出两件事：
 *   1) 体积：错版本缓存 vs 对版本缓存，各多少个 / 多大
 *   2) 耗时：无缓存 vs 有缓存，**各跑 5 次取最小值**（单次测量会被磁盘冷缓存骗）
 */
'use strict';

const fs = require('fs');
const os = require('os');
const path = require('path');
const { execFileSync, spawnSync } = require('child_process');

const ROOT = path.resolve(__dirname, '..');
const DEFAULT_SP = path.join(ROOT, 'desktop', 'vendor', 'py', 'Lib', 'site-packages');
const DEFAULT_TARGET = path.join(ROOT, 'desktop', 'vendor', 'py', 'python.exe');

function arg(name, fallback) {
  const i = process.argv.indexOf(`--${name}`);
  return i > 0 && process.argv[i + 1] ? process.argv[i + 1] : fallback;
}

function pyVersion(py) {
  const r = spawnSync(py, ['-c', 'import sys;print("%d.%d" % sys.version_info[:2])'], { encoding: 'utf8' });
  return r.status === 0 ? r.stdout.trim() : null;
}

/** 找一个「版本和运行时不一样」的解释器当对照组（通常是开发机的宿主 Python）。 */
function findHost(targetVersion) {
  const candidates = [
    process.env.HOST_PY,
    'e:/Environment/Python/python/cpython-3.14.3-windows-x86_64-none/python.exe',
    'python',
  ].filter(Boolean);
  for (const c of candidates) {
    const v = pyVersion(c);
    if (v && v !== targetVersion) return { py: c, version: v };
  }
  return null;
}

function copyDir(src, dest) {
  fs.rmSync(dest, { recursive: true, force: true });
  fs.cpSync(src, dest, { recursive: true });
}

function purgePyc(dir) {
  for (const e of fs.readdirSync(dir, { withFileTypes: true })) {
    const p = path.join(dir, e.name);
    if (e.isDirectory()) {
      if (e.name === '__pycache__') fs.rmSync(p, { recursive: true, force: true });
      else purgePyc(p);
    }
  }
}

function measurePyc(dir) {
  let count = 0, bytes = 0;
  const walk = (d) => {
    for (const e of fs.readdirSync(d, { withFileTypes: true })) {
      const p = path.join(d, e.name);
      if (e.isDirectory()) { walk(p); continue; }
      if (e.name.endsWith('.pyc')) { count++; bytes += fs.statSync(p).size; }
    }
  };
  walk(dir);
  return { count, mb: bytes / 1048576 };
}

/** 多次取最小值：单次测量会被磁盘冷缓存骗出一个巨大的假差异。 */
function bench(py, sp, { noCache }) {
  const env = { ...process.env, PYTHONPATH: sp };
  if (noCache) env.PYTHONDONTWRITEBYTECODE = '1';
  const code = 'import importlib; [importlib.import_module(m) for m in ("pydantic","fastapi","httpx","uvicorn","yaml")]';
  let best = Infinity;
  for (let i = 0; i < 5; i++) {
    const t = Date.now();
    const r = spawnSync(py, ['-B', '-c', code], { env, encoding: 'utf8' });
    if (r.status !== 0) return null;
    best = Math.min(best, Date.now() - t);
  }
  return best;
}

function main() {
  const sp = arg('sp', DEFAULT_SP);
  const target = arg('target', DEFAULT_TARGET);
  if (!fs.existsSync(sp) || !fs.existsSync(target)) {
    console.error(`找不到运行时或 site-packages：\n  sp=${sp}\n  target=${target}\n先跑一次 npm run dist（或构建脚本）把运行时建出来。`);
    process.exit(2);
  }

  const targetVersion = pyVersion(target);
  const host = findHost(targetVersion);
  if (!host) {
    console.error(`没找到与运行时（${targetVersion}）不同版本的解释器作对照，用 --host <python> 指定。`);
    process.exit(2);
  }

  console.log(`site-packages : ${sp}`);
  console.log(`运行时（对）  : ${targetVersion}  ${target}`);
  console.log(`宿主（错）    : ${host.version}  ${host.py}\n`);

  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'pyc-cost-'));
  const wrongDir = path.join(tmp, 'wrong');
  const rightDir = path.join(tmp, 'right');
  try {
    copyDir(sp, wrongDir); purgePyc(wrongDir);
    copyDir(sp, rightDir); purgePyc(rightDir);

    execFileSync(host.py, ['-m', 'compileall', '-q', '-j', '0', wrongDir], { stdio: 'inherit' });
    execFileSync(target, ['-m', 'compileall', '-q', '-j', '0', rightDir], { stdio: 'inherit' });

    const wrong = measurePyc(wrongDir);
    const right = measurePyc(rightDir);
    console.log('\n── 体积 ──────────────────────────────');
    console.log(`错版本缓存（cpython-${host.version.replace('.', '')}）: ${wrong.count} 个 / ${wrong.mb.toFixed(2)} MB  ← 运行时一个都用不上`);
    console.log(`对版本缓存（cpython-${targetVersion.replace('.', '')}）: ${right.count} 个 / ${right.mb.toFixed(2)} MB`);
    console.log(`死重：约 ${(wrong.mb - right.mb).toFixed(2)} MB 的差额 + 整份错版本缓存本身`);

    console.log('\n── 启动耗时（各跑 5 次取最小值）──────');
    const cold = bench(target, rightDir, { noCache: true });
    const warm = bench(target, rightDir, { noCache: false });
    if (cold != null && warm != null) {
      const diff = (cold - warm) / 1000;
      console.log(`无缓存 ${(cold / 1000).toFixed(2)}s vs 有缓存 ${(warm / 1000).toFixed(2)}s → 差 ${diff.toFixed(2)}s`);
      console.log(diff < 0.1 ? '判定：在噪声内，**不要拿它当性能收益**' : '判定：差异显著，可以写进结论');
    } else {
      console.log('（依赖导入失败，跳过耗时测量）');
    }

    console.log('\n这些数字**每次跑都可能不同**（依赖版本、机器状态）。');
    console.log('别抄进注释 —— 要引用就写「跑 _verify/pyc-cost.js 自己量」。');
  } finally {
    fs.rmSync(tmp, { recursive: true, force: true });
  }
}

main();
