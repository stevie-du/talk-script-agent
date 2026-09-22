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
//   E. package.json `build.files` 里那些进 asar 的文件：按 **asar 头里的路径 + 偏移 + 大小**
//      取出字节，与磁盘上的源文件比 md5（不再是"在整份归档里搜子串"）；
//   F. 两个 exe 必须是有效 PE（MZ + PE 头）且不是空壳；Setup 还要带 NSIS 签名；
//   G. 两个 exe 的时间戳不许早于**所有**会进包里的源码（含 desktop/*.js 与 requirements）。
//      —— mtime 只证"是否曾被重打"，所以另有：
//   H. Setup.exe 的**载荷**（文件清单 + 大小，用 7z 读 NSIS）必须与 win-unpacked 同一本账。
//      本机没有 7z 时这条会明确记为"跳过"并打在结论里，不混进"认证通过"。
// 它仍然证明不了的事（别再当成证明了）：安装流程在干净机器上能跑通、卸载干净、
// 应用更新后用户数据不被覆盖 —— 那几条要一台真机/虚拟机。
import { createHash } from 'node:crypto';
import { execFileSync } from 'node:child_process';
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
// 判据需要外部工具时不假装跑过：跑不了就记在这里，最后**连同"认证通过"一起**打出来，
// 免得一句"认证通过"被当成"每一条都核对过"。
const skipped = [];
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

// ── E：asar 里**那个路径**的字节 ────────────────────────────
const asarPath = path.join(UNPACKED, 'resources', 'app.asar');
const pkg = JSON.parse(readFileSync(path.join(ROOT, 'desktop', 'package.json'), 'utf8'));
// electron-builder 的 `files` 在 **build** 下面，不在顶层：第一版读 `pkg.files` 读到 undefined，
// 于是这个循环一个文件都没比、还照打"逐文件字节比对通过"—— 一条永远为真的判据
// （第 21 轮复核 Q1 的 (b2)/(d2) 就是靠这个洞过关的；我自己用变异抓回来的）。
const asarFiles = (pkg.build && pkg.build.files ? pkg.build.files : pkg.files || [])
  .map((f) => String(f)).filter((f) => f && !f.startsWith('!'));

// 帧结构是**量出来的**（本产物 win-unpacked/resources/app.asar）：
//   u32@0 = 4（外层 pickle 的字段长）、u32@4 = 头 pickle 的字节数、
//   u32@8 = 内层串字段长、u32@12 = 头 JSON 的字节数；JSON 从偏移 16 开始，
//   数据区起点 = 8 + u32@4（JSON 之后补零到 4 字节对齐，实测差值 0）。
// 之前是 `blob.includes(文件字节)`：子串命中不绑定路径 —— 换一个路径、或整份内容
// 出现在别处（打包器把同一份源码塞了两遍）它都照样绿（第 22 轮 P1-2）。
function asarHeader(blob) {
  if (blob.length < 16) { problems.push('app.asar 太小，读不出一张头'); return null; }
  const outer = blob.readUInt32LE(0);
  if (outer !== 4) {
    problems.push(`app.asar 外层帧不是预期的 4（读到 ${outer}）—— 解析器与归档不同版，`
      + '这条不能当通过');
    return null;
  }
  const pickle = blob.readUInt32LE(4), jsonLen = blob.readUInt32LE(12);
  const dataOffset = 8 + pickle;
  if (dataOffset < 16 + jsonLen || dataOffset - (16 + jsonLen) > 3) {
    problems.push(`app.asar 的头与数据区不自洽（JSON 到 ${16 + jsonLen}，数据区从 ${dataOffset} 起）`
      + ' —— 帧结构变了，解析器要跟着改，别把这条当通过');
    return null;
  }
  try {
    return { header: JSON.parse(blob.subarray(16, 16 + jsonLen).toString('utf8')), dataOffset };
  } catch {
    problems.push('app.asar 的头不是合法 JSON');
    return null;
  }
}
const asarEntry = (header, rel) => rel.split('/').reduce(
  (n, part) => (n && n.files ? n.files[part] : undefined), header);

if (!asarFiles.length) problems.push('package.json 里读不到 build.files：asar 比对没有输入');
else if (!existsSync(asarPath)) {
  problems.push('缺少 resources/app.asar（主进程代码不在任何一本 md5 账里）');
} else {
  const blob = readFileSync(asarPath);
  const parsed = asarHeader(blob);
  let checked = 0;
  for (const rel of asarFiles) {
    const abs = path.join(ROOT, 'desktop', rel);
    if (!existsSync(abs) || !statSync(abs).isFile()) {
      // 清单上有、源码里没有 —— 只打日志等于把这一格从账本上划掉（第 22 轮 P2-1）
      problems.push(`build.files 列了 ${rel}，但 desktop/ 下没有这个文件：比对少了输入`);
      continue;
    }
    const src = readFileSync(abs);
    const entry = parsed && asarEntry(parsed.header, rel);
    if (!entry) {
      problems.push(`asar 的头里没有 ${rel}（它没被打进包，界面上永远看不到这次改动）`);
      continue;
    }
    checked += 1;
    if (Number(entry.size) !== src.length) {
      problems.push(`${rel}: asar 里记的是 ${entry.size} 字节，磁盘上是 ${src.length} 字节`
        + ' —— 改了源码没重打，或包里是另一份');
      continue;
    }
    const from = entry.unpacked
      ? (existsSync(path.join(UNPACKED, 'resources', 'app.asar.unpacked', rel))
        ? readFileSync(path.join(UNPACKED, 'resources', 'app.asar.unpacked', rel)) : null)
      : blob.subarray(parsed.dataOffset + Number(entry.offset),
        parsed.dataOffset + Number(entry.offset) + src.length);
    if (!from) {
      problems.push(`${rel}: asar 标了 unpacked，但 resources/app.asar.unpacked 里没有它`);
      continue;
    }
    const md5 = (b) => createHash('md5').update(b).digest('hex');
    if (md5(from) !== md5(src)) {
      problems.push(`${rel}: asar 里**这个路径**（偏移 ${entry.offset}，${src.length} 字节）的字节`
        + '与磁盘上的不是一份 —— 按路径取字节，不再是在归档里搜子串');
    }
  }
  if (checked !== asarFiles.length) {
    problems.push(`asar 只比对到 ${checked}/${asarFiles.length} 个文件，认证不完整`
      + '（上面逐条列了缺哪一个）');
  }
  console.log(`asar: 按路径取字节比对 ${checked}/${asarFiles.length} 个（${asarFiles.join(', ')}）`);
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

// ── H：Setup.exe 的载荷 vs win-unpacked（要本机有能读 NSIS 的 7z）──
// F/G 只证明"那是一份真产物"（PE + NSIS 签名 + 体积），不证明它是**这次**构建的产物：
// 换一份别处的 Setup 进来、或 `touch` 一下时间戳，两条臂都照样绿（第 22 轮 P2-2）。
// 这条把安装包的**文件清单与大小**与 win-unpacked 逐项对账，是本机所能做到的最强绑定。
function findSevenZip() {
  const cands = [process.env.SEVENZ, '7z', '7za', '7zr'].filter(Boolean);
  for (const c of cands) {
    try {
      execFileSync(c, ['i'], { stdio: 'ignore', timeout: 30000 });
      return c;
    } catch { /* 换一个 */ }
  }
  return null;
}

const setupExe = path.join(ROOT, 'desktop', 'dist', 'TalkScript Setup 0.2.0.exe');
const seven = findSevenZip();
if (!existsSync(setupExe)) {
  skipped.push('Setup.exe 不在（F 条已经报过），载荷未对账');
} else if (!seven) {
  skipped.push('本机没有能读 NSIS 的 7z（可用环境变量 SEVENZ 指定路径）：'
    + 'Setup.exe 的载荷**没有**与 win-unpacked 对账，只核对了它是有效 PE + 带 NSIS 签名');
} else {
  const listing = execFileSync(seven, ['l', '-slt', setupExe],
    { maxBuffer: 256 << 20, encoding: 'utf8' });
  const norm = (s) => s.replace(/\\/g, '/').toLowerCase();
  const inSetup = new Map();
  let block = null;
  const flush = () => {
    // `Attributes` 含 D 的是目录：NSIS 会带空目录，win-unpacked 那边 readdir 也看得见，
    // 但对账只需要文件那一层（实测目录会在两个方向上各产生一批噪音差异）。
    if (block && block.path && !/D/.test(block.attr || '')) inSetup.set(block.path, block.size);
    block = null;
  };
  for (const line of listing.split(/\r?\n/)) {
    let m;
    if ((m = /^Path = (.*)$/.exec(line))) {
      flush();
      const p = norm(m[1]);
      if (p === norm(setupExe) || p.includes(':')) continue;   // 归档自身那一块
      block = { path: p };
      continue;
    }
    if (!block) continue;
    if ((m = /^Size = (\d+)$/.exec(line))) block.size = Number(m[1]);
    else if ((m = /^Attributes = (.*)$/.exec(line))) block.attr = m[1];
  }
  flush();

  const onDisk = new Map();
  for (const e of readdirSync(UNPACKED, { recursive: true })) {
    const abs = path.join(UNPACKED, e);
    if (!statSync(abs).isFile()) continue;
    onDisk.set(norm(path.relative(UNPACKED, abs)), statSync(abs).size);
  }
  const onlySetup = [...inSetup.keys()].filter((k) => !onDisk.has(k));
  const onlyDisk = [...onDisk.keys()].filter((k) => !inSetup.has(k));
  const diffSize = [...inSetup].filter(([k, v]) => onDisk.has(k) && onDisk.get(k) !== v);
  for (const [list, name] of [[onlySetup, '只在安装包'], [onlyDisk, '只在 win-unpacked'],
                              [diffSize, '同名大小不同']]) {
    if (list.length) {
      problems.push(`Setup 载荷：${name} ${list.length} 个 —— `
        + list.slice(0, 6).map((x) => (Array.isArray(x) ? `${x[0]}(${x[1]}/${onDisk.get(x[0])})` : x))
          .join(', '));
    }
  }
  console.log(`Setup 载荷: 与 win-unpacked 对账 ${inSetup.size} 个文件（用 ${seven}）`);
}

if (problems.length) {
  console.log('\n认证未通过：');
  for (const p of problems) console.log('  ✗ ' + p);
  for (const s of skipped) console.log('  ⚠ 跳过：' + s);
  process.exit(1);
}
console.log('\n认证通过：包内引擎与当前源码逐文件一致（app / renderer / packs / asar 按路径取字节），'
  + 'private 未出厂，两份产物都是有效 PE，Setup 载荷与 win-unpacked 同一本账。');
for (const s of skipped) {
  console.log(`  ⚠ 但有一项没核对：${s}`);
  console.log('    （上面那句"认证通过"不包括这一项，别把它当成全账已核。）');
}
console.log('仍需人工/真机验证：干净机器上的安装-升级-卸载流程（本脚本不证明）。');
