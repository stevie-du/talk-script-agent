// 认证构建产物：把"打完包"变成一条可重复、可失败、不容易被糊弄的命令。
//
// 为什么要有这个脚本：每一批改动收尾都要"重打 + 认证"，而我此前用的是手敲的
// `diff -r app .../engine/app` —— 它只看了 app/，**没看 renderer/ 与 packs/**，
// 少看一本账就是宣称得比证据多（§15.37 就是这么被抓出来的）。
//
// 第 21 轮复核又抓到两件事，都记在这里免得再忘：
//   1) 它原来只比 `win-unpacked`，主进程的 `main.js` 等在 `app.asar` 里**根本不在任何一本账里**；
//      而且"产物不许比源码旧"那条判据读的是 mtime，`touch` 一下就满足了。
//   2) 它从不读 Setup.exe 的字节：把 Setup 换成别的文件它也照绿。
// 所以：**能读字节的地方一律读字节**，mtime 只用来报"是否曾被重打"，不作为内容证据。
//
// 判据（任何一条不满足退出码 1）：
//   A. engine/app 与源码 app/ 逐文件 md5 一致（忽略 __pycache__/.pyc）；
//   B. engine/renderer 与 desktop/renderer 逐文件 md5 一致；
//   C. engine/packs 与源码 packs/ 一致 —— 除了 `private/`（按设计不许出厂）；
//   D. 包内不许出现任何 private 文件；
//   E. package.json `files` 里那些进 asar 的文件，字节必须真的出现在 resources/app.asar 里；
//   F. 两个 exe 必须是有效 PE（MZ + PE 头）且不是空壳；Setup 还要带 NSIS 尾签；
//   G. 两个 exe 的时间戳不许早于**所有**会进包里的源码（含 desktop/*.js 与 requirements）。
// 它仍然证明不了的事（别再当成证明了）：安装流程在干净机器上能跑通、卸载干净、
// 应用更新后用户数据不被覆盖 —— 那几条要一台真机/虚拟机。
import { createHash } from 'node:crypto';
import { readdirSync, statSync, readFileSync, existsSync } from 'node:fs';
import path from 'node:path';

const ROOT = path.resolve(import.meta.dirname, '..', '..');
const UNPACKED = path.join(ROOT, 'desktop', 'dist', 'win-unpacked');
const ENGINE = path.join(UNPACKED, 'resources', 'engine');
const PYC = [/__pycache__/, /\.pyc$/];

function walk(dir, skip) {
  const out = new Map();
  if (!existsSync(dir)) return out;
  for (const entry of readdirSync(dir, { recursive: true })) {
    const abs = path.join(dir, entry);
    if (!statSync(abs).isFile()) continue;
    const rel = path.relative(dir, abs).split(path.sep).join('/');
    if ((skip || []).concat(PYC).some((re) => re.test(rel))) continue;
    out.set(rel, createHash('md5').update(readFileSync(abs)).digest('hex'));
  }
  return out;
}

const problems = [];
function compare(label, srcDir, pkgDir, ignoreOnlySrc) {
  const a = walk(srcDir, []);
  const b = walk(pkgDir, []);
  const onlySrc = [...a.keys()].filter((k) => !b.has(k)
    && !(ignoreOnlySrc || []).some((re) => re.test(k)));
  const onlyPkg = [...b.keys()].filter((k) => !a.has(k));
  const differ = [...a.keys()].filter((k) => b.has(k) && a.get(k) !== b.get(k));
  for (const [list, name] of [[onlySrc, '只在源码'], [onlyPkg, '只在包里'], [differ, '内容不同']]) {
    if (list.length) {
      problems.push(`${label}：${name} ${list.length} 个 —— ${list.slice(0, 6).join(', ')}`);
    }
  }
  console.log(`${label}: 源码 ${a.size} / 包内 ${b.size}`
    + `${onlySrc.length ? ` | 只在源码 ${onlySrc.length}` : ''}`
    + `${onlyPkg.length ? ` | 只在包内 ${onlyPkg.length}` : ''}`
    + `${differ.length ? ` | 内容不同 ${differ.length}` : ''}`);
}

compare('app', path.join(ROOT, 'app'), path.join(ENGINE, 'app'));
compare('renderer', path.join(ROOT, 'desktop', 'renderer'), path.join(ENGINE, 'renderer'));
compare('packs', path.join(ROOT, 'packs'), path.join(ENGINE, 'packs'),
        [/(^|\/)private\//]);

const inPkgPrivate = [...walk(path.join(ENGINE, 'packs'), []).keys()]
  .filter((k) => /(^|\/)private\//.test(k));
if (inPkgPrivate.length) problems.push(`包内出现 private 文件 ${inPkgPrivate.length} 个（不许出厂）`);

// ── E：asar 里的字节 ────────────────────────────────────────
const asarPath = path.join(UNPACKED, 'resources', 'app.asar');
const pkg = JSON.parse(readFileSync(path.join(ROOT, 'desktop', 'package.json'), 'utf8'));
// electron-builder 的 `files` 在 **build** 下面，不在顶层：第一版读 `pkg.files` 读到 undefined，
// 于是这个循环一个文件都没比、还照打"逐文件字节比对通过"—— 一条永远为真的判据
// （第 21 轮复核 Q1 的 (b2)/(d2) 就是靠这个洞过关的；我自己用变异抓回来的）。
const asarFiles = (pkg.build && pkg.build.files ? pkg.build.files : pkg.files || [])
  .map((f) => String(f)).filter((f) => f && !f.startsWith('!'));
if (!asarFiles.length) problems.push('package.json 里读不到 build.files：asar 比对没有输入');
else if (!existsSync(asarPath)) {
  problems.push('缺少 resources/app.asar（主进程代码不在任何一本 md5 账里）');
} else {
  const blob = readFileSync(asarPath);
  let checked = 0;
  for (const rel of asarFiles) {
    const abs = path.join(ROOT, 'desktop', rel);
    if (!existsSync(abs) || !statSync(abs).isFile()) continue;
    checked += 1;
    if (!blob.includes(readFileSync(abs))) {
      problems.push(`asar 里找不到 ${rel} 的字节（改了主进程代码却没重打，或它没被打进去）`);
    }
  }
  console.log(`asar: 逐文件字节比对 ${checked}/${asarFiles.length} 个（${asarFiles.join(', ')}）`);
}

// ── F/G：产物本身 ──────────────────────────────────────────
function sourceNewest() {
  let max = 0;
  // 只列**会进包里**的东西。`desktop/scripts/` 是构建与认证工具，不进产物 ——
  // 把它算进来会让每次改脚本本身都变成"安装包过期"的假红（第 21 轮自己踩到）。
  const dirs = ['app', path.join('desktop', 'renderer'), 'packs'];
  const files = [path.join('desktop', 'package.json'), 'requirements-runtime.txt',
                 'requirements.txt', path.join('desktop', 'main.js'),
                 path.join('desktop', 'preload.js')];
  for (const rel of dirs) {
    const abs = path.join(ROOT, rel);
    if (!existsSync(abs)) continue;
    for (const e of readdirSync(abs, { recursive: true })) {
      const p = path.join(abs, e);
      if (!statSync(p).isFile() || PYC.some((re) => re.test(e))) continue;
      max = Math.max(max, statSync(p).mtimeMs);
    }
  }
  for (const rel of files) {
    const p = path.join(ROOT, rel);
    if (existsSync(p)) max = Math.max(max, statSync(p).mtimeMs);
  }
  return max;
}

const newestSrc = sourceNewest();
for (const exe of ['TalkScript Setup 0.2.0.exe', 'TalkScript 0.2.0.exe']) {
  const p = path.join(UNPACKED, '..', exe);
  if (!existsSync(p)) { problems.push(`缺少产物 ${exe}`); continue; }
  const buf = readFileSync(p, null);
  const head = buf.subarray(0, 2).toString('latin1');
  let peOk = false;
  const scan = buf.subarray(0, Math.min(buf.length, 8192));
  const peIdx = scan.indexOf('PE\0\0', 'latin1');
  if (peIdx > 0) {
    const peOff = scan.readUInt32LE(0x3c);
    peOk = peOff > 0 && peOff + 4 <= buf.length
      && buf.subarray(peOff, peOff + 4).toString('latin1') === 'PE\0\0';
  }
  const size = statSync(p).size;
  const isSetup = /Setup/.test(exe);
  // NSIS 的 `NullsoftInst` 签名在文件**头部**的 stub 里（实测本产物在偏移 ~58.8 KB 处），
  // 不在尾部 —— 第一版按"最后 64KB"找，于是对一份合法 Setup 报了假红。
  const nsis = isSetup
    ? buf.subarray(0, Math.min(buf.length, 262144)).includes(Buffer.from('NullsoftInst'))
    : true;
  console.log(`${exe}: ${(size / 1048576).toFixed(0)} MB`
    + `${head === 'MZ' && peOk ? ' MZ+PE ok' : ' ⚠ 不是有效 PE'}`
    + `${isSetup ? (nsis ? ' NSIS 尾签 ok' : ' ⚠ 无 NSIS 尾签') : ''}`);
  if (head !== 'MZ' || !peOk) problems.push(`${exe} 不是有效的 PE 可执行文件（被替换或截断）`);
  if (size < 1048576) problems.push(`${exe} 小于 1 MB，不像一份真实产物`);
  if (isSetup && !nsis) problems.push('Setup 里没有 NSIS 签名，不像一份真安装包');
  if (statSync(p).mtimeMs < newestSrc) {
    problems.push(`${exe} 的时间戳早于最新源码改动 —— 疑似改了没重打（mtime 只能证"是否曾重打"，不证明内容）`);
  }
}

if (problems.length) {
  console.log('\n认证未通过：');
  for (const p of problems) console.log('  ✗ ' + p);
  process.exit(1);
}
console.log('\n认证通过：包内引擎与当前源码逐文件一致（app / renderer / packs / asar 字节），'
  + 'private 未出厂，两份产物都是有效 PE。');
console.log('仍需人工/真机验证：干净机器上的安装-升级-卸载流程（本脚本不证明）。');
