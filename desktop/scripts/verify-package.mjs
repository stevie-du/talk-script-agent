// 认证安装包：把打出来的 win-unpacked 里那份引擎与当前源码逐项比一遍。
//
// 为什么要有这个脚本：每一批改动收尾都要"重打 + 认证"，而我此前用的是手敲的
// `diff -r app .../engine/app` —— 它只看了 app/，**没看 renderer/ 与 packs/**。
// 少看一处就等于宣称"认证过了"而实际只认证了三本账里的一本。
// 判据（任何一条不满足就退出码 1）：
//   1. engine/app 与源码 app/ 逐文件内容一致（忽略 __pycache__）；
//   2. engine/renderer 与 desktop/renderer 逐文件内容一致；
//   3. engine/packs 与源码 packs/ 一致 —— **除了** `private/`（按设计不许出厂）；
//   4. 包里不许出现任何 private 文件；
//   5. Setup/portable 两个产物的时间戳不早于源码里最新的改动（防"改了没重打"）。
import { createHash } from 'node:crypto';
import { readdirSync, statSync, readFileSync, existsSync } from 'node:fs';
import path from 'node:path';

const ROOT = path.resolve(import.meta.dirname, '..', '..');
const UNPACKED = path.join(ROOT, 'desktop', 'dist', 'win-unpacked');
const ENGINE = path.join(UNPACKED, 'resources', 'engine');

function walk(dir, skip) {
  const out = new Map();
  if (!existsSync(dir)) return out;
  for (const entry of readdirSync(dir, { recursive: true })) {
    const rel = path.relative(dir, path.join(dir, entry)).split(path.sep).join('/');
    if (!entry || !statSync(path.join(dir, entry)).isFile()) continue;
    if (skip.some((re) => re.test(rel))) continue;
    out.set(rel, createHash('md5').update(readFileSync(path.join(dir, entry))).digest('hex'));
  }
  return out;
}

const PYC = [/__pycache__/, /\.pyc$/];
const problems = [];

function compare(label, srcDir, pkgDir, ignore) {
  const a = walk(srcDir, PYC.concat(ignore || []));
  const b = walk(pkgDir, PYC);
  const onlySrc = [...a.keys()].filter((k) => !b.has(k) && !(ignore || []).some((re) => re.test(k)));
  const onlyPkg = [...b.keys()].filter((k) => !a.has(k));
  const differ = [...a.keys()].filter((k) => b.has(k) && a.get(k) !== b.get(k));
  for (const [list, name] of [[onlySrc, '只在源码'], [onlyPkg, '只在包里'], [differ, '内容不同']]) {
    if (list.length) problems.push(`${label}：${name} ${list.length} 个 —— ${list.slice(0, 6).join(', ')}`);
  }
  console.log(`${label}: 源码 ${a.size} / 包内 ${b.size}` +
    `${onlySrc.length ? ` | 只在源码 ${onlySrc.length}` : ''}` +
    `${onlyPkg.length ? ` | 只在包内 ${onlyPkg.length}` : ''}` +
    `${differ.length ? ` | 内容不同 ${differ.length}` : ''}`);
}

compare('app', path.join(ROOT, 'app'), path.join(ENGINE, 'app'));
compare('renderer', path.join(ROOT, 'desktop', 'renderer'), path.join(ENGINE, 'renderer'));
// packs：`private/` 按设计不出厂，所以"只在源码"里属于它的部分不算问题
compare('packs', path.join(ROOT, 'packs'), path.join(ENGINE, 'packs'), [/^.*\/private\//, /^private\//]);

const inPkgPrivate = [...walk(path.join(ENGINE, 'packs'), []).keys()].filter((k) => /(^|\/)private\//.test(k));
if (inPkgPrivate.length) problems.push(`包内出现 private 文件 ${inPkgPrivate.length} 个（不许出厂）`);

// 产物时间戳：不早于源码里最新的 .py/.js/.css/.yaml 改动
const newestSrc = (() => {
  let max = 0;
  for (const dir of ['app', path.join('desktop', 'renderer'), 'packs']) {
    const abs = path.join(ROOT, dir);
    if (!existsSync(abs)) continue;
    for (const e of readdirSync(abs, { recursive: true })) {
      const p = path.join(abs, e);
      if (!statSync(p).isFile() || !/\.(py|js|mjs|css|yaml|yml|html)$/.test(p)) continue;
      max = Math.max(max, statSync(p).mtimeMs);
    }
  }
  return max;
})();
for (const exe of ['TalkScript Setup 0.2.0.exe', 'TalkScript 0.2.0.exe']) {
  const p = path.join(UNPACKED, '..', exe);
  if (!existsSync(p)) { problems.push(`缺少产物 ${exe}`); continue; }
  const t = statSync(p).mtimeMs;
  console.log(`${exe}: ${new Date(t).toISOString()} ` +
    `${t >= newestSrc ? '(不早于源码改动)' : '(早于最新源码改动 —— 疑似改了没重打)'}`);
  if (t < newestSrc) problems.push(`${exe} 比最新源码还旧，安装包是过期的`);
}

if (problems.length) {
  console.log('\n认证未通过：');
  for (const p of problems) console.log('  ✗ ' + p);
  process.exit(1);
}
console.log('\n认证通过：包内引擎与当前源码逐项一致，private 未出厂，产物不早于源码。');
