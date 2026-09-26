#!/usr/bin/env node
// 发布更新：`npm run dist` 之后，把产物推到更新服务器。
//
// 为什么需要它
// ------------
// generic provider 官方明确「不会自动上传，必须手动传」—— `npm run dist` 出完包，
// 「让客户端真的能检查到」还差一步手工 scp。手工做会漏：忘传 blockmap（差分失效）、
// 把旧包覆盖上去（无声回退，用户客户端永远停在旧版还查不到原因）。
// 这个脚本把那一串手工动作固化成六步，**顺序即防线**。
//
// 用法
// ----
//   cd desktop
//   npm run dist
//   npm run publish:update -- --to user@host:/var/www/talkscript \
//                           --url https://talkscript.example.com/talkscript/
//
//   # 只检查不上传（传错地址时先跑这个）：
//   node scripts/publish-update.mjs --to ... --url ... --dry-run
//
// 六步
// ----
//   1 校验产物   2 防降级   3 上传   4 闭环验证   5 --keep 清理旧包   6 --dry-run
// 每一步失败都打印「哪一步 / 为什么 / 下一步该怎么办」——与本项目报错对话框
// 同一条规矩（末尾给路径，不让人猜）。
//
// 零依赖：只用 node 内置模块。Windows 开发机用内置 OpenSSH 的 scp / ssh
// （Win10 1809 起自带），不依赖 rsync。

import fs from 'node:fs';
import path from 'node:path';
import http from 'node:http';
import https from 'node:https';
import crypto from 'node:crypto';
import { fileURLToPath } from 'node:url';
import { execFileSync } from 'node:child_process';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const DESKTOP = path.resolve(HERE, '..');
const TAG = '[publish]';

// ── 参数 ────────────────────────────────────────────────────
function usage() {
  console.log(`用法：
  node scripts/publish-update.mjs --to user@host:/var/www/talkscript --url https://host/talkscript/ [选项]

必填：
  --to     scp 目标（user@host:目录）。目录是服务器上放更新文件的**绝对路径**
  --url    客户端访问用的更新地址（对应 TALKSCRIPT_UPDATE_URL，尾斜杠可省）

选项：
  --keep N  服务器上保留最近 N 个版本的安装包（默认 2，回滚用，方案 §9 步骤 5）
  --dry-run 只跑「校验产物 / 防降级 / 核对远程现状」，不上传也不删任何文件
  --dir     本地产物目录（默认 desktop/dist）
  --help    打印本段
`);
}

const argv = process.argv.slice(2);
const opt = { keep: 2, dryRun: false, dir: path.join(DESKTOP, 'dist') };
for (let i = 0; i < argv.length; i++) {
  const a = argv[i];
  if (a === '--help' || a === '-h') { usage(); process.exit(0); }
  if (a === '--dry-run') { opt.dryRun = true; continue; }
  const v = argv[++i];
  if (v === undefined) die('参数', `${a} 缺一个值`, '跑 npm run publish:update -- --help 看用法');
  if (a === '--to') opt.to = v;
  else if (a === '--url') opt.url = v;
  else if (a === '--keep') opt.keep = Number(v);
  else if (a === '--dir') opt.dir = path.resolve(v);
  else die('参数', `不认识的参数 ${a}`, '跑 npm run publish:update -- --help 看用法');
}

// ── 报错：哪一步 / 为什么 / 下一步该怎么办 ──────────────────
function die(step, why, next) {
  console.error(`\n${TAG} 失败（${step}）：${why}`);
  if (next) console.error(`  下一步：${next}`);
  process.exit(1);
}
function step(n, title) { console.log(`\n${TAG} ── ${n}/6 ${title}`); }
function ok(msg) { console.log(`${TAG}   ✓ ${msg}`); }

// ── 纯逻辑（导出给单测；这里不碰网络不碰进程） ──────────────

/**
 * 解析 electron-builder 生成的 latest.yml（builder 25 = 旧版 files[] 格式，§12.2）。
 *
 * 为什么不用 YAML 库：零依赖是本项目脚本的规矩（build-python-runtime.mjs 同）。
 * latest.yml 是机器生成的、形状稳定，只需要两个字段，写一个针对性读取器比
 * 引一个解析器诚实；读不出来就**明确失败**，不猜。
 *
 * @param {string} text
 * @returns {{version: string|null, files: string[],
 *            sizes: Record<string, number>, shas: Record<string, string>}}
 *            shas：每个 file 的 sha512（base64）——发布端重算对账的依据（步骤 1）。
 */
export function parseLatestYml(text) {
  const out = { version: null, files: [], sizes: {}, shas: {} };
  let inFiles = false;
  let cur = null;   // 当前 - url: 那一项
  for (const raw of String(text).split(/\r?\n/)) {
    if (!raw.trim() || raw.trim().startsWith('#')) continue;
    const indent = raw.length - raw.trimStart().length;
    const line = raw.trim();
    if (indent === 0) {
      inFiles = false;
      cur = null;
      const m = /^([A-Za-z0-9_-]+):\s*(.*)$/.exec(line);
      if (!m) continue;
      const val = m[2].trim().replace(/^['"]|['"]$/g, '');
      if (m[1] === 'version') out.version = val;
      else if (m[1] === 'files') inFiles = true;
      continue;
    }
    if (!inFiles) continue;
    const u = /^(?:-\s*)?url:\s*(.*)$/.exec(line);
    if (u) {
      cur = u[1].trim().replace(/^['"]|['"]$/g, '');
      out.files.push(cur);
      continue;
    }
    const s = /^size:\s*(\d+)\s*$/.exec(line);
    if (s && cur) out.sizes[cur] = Number(s[1]);
    // sha512 归到它上面那一项的 url 上（与 size 同一条归属规则）。顶层的
    // sha512（整包元数据）在 indent===0 分支就被跳过，混不进对账表。
    const h = /^sha512:\s*(\S+)\s*$/.exec(line);
    if (h && cur) out.shas[cur] = h[1];
  }
  return out;
}

/**
 * 版本比较：a<b → -1，相等 → 0，a>b → 1。
 *
 * 只按点分数字逐段比，**不带 semver 预发布语义**。缺位按 0 补（0.2 === 0.2.0）；
 * 段内以 parseInt 的数字前缀比，前缀相同即相等（0.2.1-beta === 0.2.1，不是小于）；
 * 纯非数字段退字典序，保证「总能给出一个确定答案」而不是 NaN 比较。
 * 本项目的版本号是 desktop/package.json 里的纯三段号，这些边角都走不到；
 * 真到了要发预演版的那天，这里要改成完整 semver 并且补一条测试——
 * 现在写一半比不写更危险。
 */
export function cmpVer(a, b) {
  const pa = String(a).split('.'), pb = String(b).split('.');
  const n = Math.max(pa.length, pb.length);
  for (let i = 0; i < n; i++) {
    const sa = pa[i] ?? '0', sb = pb[i] ?? '0';
    const x = parseInt(sa, 10), y = parseInt(sb, 10);
    if (Number.isNaN(x) || Number.isNaN(y)) {
      if (sa !== sb) return sa < sb ? -1 : 1;
      continue;
    }
    if (x !== y) return x < y ? -1 : 1;
  }
  return 0;
}

/**
 * 拼更新地址 + 文件名。尾斜杠由调用方保证（与 updater-core.resolveFeedUrl 同口径），
 * 文件名里的空格必须 %20 —— generic provider 在客户端就是这么拼的，服务端这侧
 * 闭环验证的 HEAD 要走同一条路，否则「本地能下、线上 404」这种错位验不出来。
 */
export function joinUrl(base, name) {
  return base.replace(/\/+$/, '') + '/' + encodeURIComponent(name).replace(/%2F/gi, '/');
}

/**
 * --keep：挑出该删的文件名。
 *
 * @param {Array<{version: string, names: string[]}>} versions 服务器上现存的版本
 * @param {number} keep 保留最近几个
 * @param {Set<string>|string[]} referenced 线上 latest.yml 正在引用的文件名（永不删）
 * @returns {string[]}
 */
export function pickStale(versions, keep, referenced) {
  const ref = referenced instanceof Set ? referenced : new Set(referenced || []);
  const sorted = [...versions].sort((x, y) => cmpVer(y.version, x.version));   // 新 → 旧
  const doomed = [];
  sorted.slice(Math.max(0, keep)).forEach((v) => {
    v.names.forEach((n) => { if (!ref.has(n)) doomed.push(n); });
  });
  return doomed;
}

// ── 小工具 ──────────────────────────────────────────────────
function readJson(p) { return JSON.parse(fs.readFileSync(p, 'utf8')); }

/** 本地文件的 sha512（base64，与 latest.yml 记录同一种编码）。
 *  流式读取：安装包约百 MB，一次性 readFileSync 再 hash 白占一份内存。 */
function sha512File(p) {
  return new Promise((resolve, reject) => {
    const h = crypto.createHash('sha512');
    const s = fs.createReadStream(p);
    s.on('data', (c) => h.update(c));
    s.on('end', () => resolve(h.digest('base64')));
    s.on('error', reject);
  });
}

// 单请求超时（毫秒）。可用环境变量覆盖：集成测试要把超时缩到毫秒级才能验
// 「超时真的会退出」，不为测试加命令行参数 —— 发布脚本的参数面保持最小。
const HTTP_TIMEOUT_MS = Number(process.env.PUBLISH_HTTP_TIMEOUT_MS) || 30000;

/** 一次 HTTP 请求（跟随重定向）。method 支持 GET / HEAD。
 *  ⚠ 必须带**整体**超时（旧 P3）：只连不答的服务器（防火墙黑洞 / 服务器 hang）
 *  以前会让发布脚本无限挂起，发布卡死在「看起来还在跑」。按「整个请求完成」计时
 *  而不是 socket 空闲 —— 慢滴服务器（隔几十秒挤一个字节）同样逃不掉。 */
function httpRequest(urlStr, method, redirects = 3) {
  return new Promise((resolve, reject) => {
    let u;
    try { u = new URL(urlStr); } catch (e) { reject(new Error(`不是合法 URL：${urlStr}`)); return; }
    let timer = null;
    const mod = u.protocol === 'https:' ? https : http;
    const req = mod.request(u, { method }, (res) => {
      if (res.statusCode >= 300 && res.statusCode < 400 && res.headers.location && redirects > 0) {
        clearTimeout(timer);   // 下一跳自己挂新表，旧表不许再响
        res.resume();   // 丢掉重定向响应体，否则连接不释放
        resolve(httpRequest(new URL(res.headers.location, u).toString(), method, redirects - 1));
        return;
      }
      const chunks = [];
      res.on('data', (c) => chunks.push(c));
      res.on('end', () => {
        clearTimeout(timer);
        resolve({
          status: res.statusCode, headers: res.headers, body: Buffer.concat(chunks),
        });
      });
    });
    timer = setTimeout(() => {
      // destroy(err) 会走下面的 'error' → reject，调用方按普通失败处理并 die
      req.destroy(new Error(`${method} ${urlStr} 超过 ${HTTP_TIMEOUT_MS}ms 无响应（超时）`));
    }, HTTP_TIMEOUT_MS);
    req.on('error', reject);
    req.end();
  });
}

function ssh(host, cmd) {
  try {
    return execFileSync('ssh', [host, cmd], { encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] });
  } catch (e) {
    const err = (e.stderr || '').toString().trim();
    die('远程命令', `ssh ${host} 执行失败：${err || e.message}`,
      '确认 ssh 能免密登录（ssh-copy-id），以及服务器上该目录存在');
  }
}

function scp(files, host, dir) {
  try {
    execFileSync('scp', [...files, `${host}:${dir}/`], { stdio: 'inherit' });
  } catch (e) {
    die('上传', `scp 失败：${e.message}`,
      '确认 scp 可用（Windows 10 1809+ 自带 OpenSSH）、磁盘满 / 目录权限（属主是不是这个用户）');
  }
}

// 远端 shell 单引号包裹：路径里有空格 / 单引号都不会被 shell 拆开
function shq(s) { return "'" + String(s).replace(/'/g, "'\\''") + "'"; }

function parseTarget(to) {
  const i = to.indexOf(':');
  if (i <= 0) die('参数', `--to 的格式是 user@host:/绝对/路径，收到的是 ${to}`, '看 --help');
  const host = to.slice(0, i);
  const dir = to.slice(i + 1).replace(/\/+$/, '');
  if (!dir.startsWith('/')) {
    die('参数', `--to 的目录必须是绝对路径（服务器上的），收到 ${dir}`, '例如 user@host:/var/www/talkscript');
  }
  return { host, dir };
}

// ── 主流程 ──────────────────────────────────────────────────
async function main() {
  if (!opt.to || !opt.url) {
    console.error(`${TAG} --to 与 --url 都是必填\n`);
    usage();
    process.exit(1);
  }
  if (!Number.isInteger(opt.keep) || opt.keep < 1) die('参数', `--keep 要正整数，收到 ${opt.keep}`, '看 --help');

  const { host, dir } = parseTarget(opt.to);
  const url = opt.url.endsWith('/') ? opt.url : opt.url + '/';
  const pkg = readJson(path.join(DESKTOP, 'package.json'));
  const dist = opt.dir;

  console.log(`${TAG} 本地版本 ${pkg.version} → ${host}:${dir}（客户端地址 ${url}）`);
  if (opt.dryRun) console.log(`${TAG} dry-run：不上传、不删文件，只做检查`);

  // ── 1) 校验产物 ──────────────────────────────────────────
  step(1, '校验产物');
  const ymlPath = path.join(dist, 'latest.yml');
  if (!fs.existsSync(ymlPath)) {
    die('校验产物', `${path.relative(DESKTOP, ymlPath)} 不存在`,
      '先在 desktop/ 下跑 npm run dist；确认 package.json 的 build.publish 配了 provider（配了才会生成 latest.yml）');
  }
  const meta = parseLatestYml(fs.readFileSync(ymlPath, 'utf8'));
  if (!meta.version) {
    die('校验产物', 'latest.yml 里读不出 version 字段',
      '这个文件是 electron-builder 生成的，禁止手写；看看是不是被编辑器改坏了');
  }
  if (cmpVer(meta.version, pkg.version) !== 0) {
    die('校验产物', `latest.yml 的 version 是 ${meta.version}，package.json 是 ${pkg.version}`,
      'dist/ 里混着上一个版本的产物：删掉 desktop/dist 重新 npm run dist');
  }
  ok(`latest.yml version = ${meta.version}，与 package.json 一致`);
  if (!meta.files.length) {
    die('校验产物', 'latest.yml 里没有 files[] 条目', 'electron-builder 版本与方案 §12.2 锁的 25 不一致？先核对再传');
  }
  const local = [];
  for (const name of meta.files) {
    const p = path.join(dist, name);
    if (!fs.existsSync(p)) {
      die('校验产物', `latest.yml 列出的文件不在 dist/ 里：${name}`,
        'electron-builder 的 files[] 与产物对不上——别手工补文件，重新 npm run dist');
    }
    const size = fs.statSync(p).size;
    if (meta.sizes[name] !== undefined && meta.sizes[name] !== size) {
      // yml 与包不同源（典型：dist/ 被复用，yml 是上次构建的、exe 被覆盖了）。
      // 这个错不拦，客户端下载后 sha512 校验不过 → 更新永远失败且报错难懂（§12.2 同类）。
      die('校验产物', `${name} 的大小 ${size} 与 latest.yml 记的 ${meta.sizes[name]} 不一致`,
        'yml 与安装包不是同一次构建的产物：删掉 desktop/dist 重新 npm run dist');
    }
    // sha512 重算对账（旧 P3）：只比 size 拦不住「同长度不同内容」——恰好那就是
    // 最危险的形态（半截写入 / 篡改 / 缓存串包）。客户端下载后拿同一字段校验，
    // 不过关就永远更新失败且报错难懂；发布端先用同一把尺子量一遍，对不上就拒传。
    const recordedSha = meta.shas[name];
    if (recordedSha === undefined) {
      die('校验产物', `latest.yml 里 ${name} 没有 sha512 字段`,
        '这个文件是 electron-builder 生成的，禁止手写；字段缺了就删掉 desktop/dist 重新 npm run dist');
    }
    const actualSha = await sha512File(p);
    if (actualSha !== recordedSha) {
      die('校验产物',
        `${name} 的 sha512 与 latest.yml 记录不一致（本地重算 ${actualSha.slice(0, 12)}… ≠ 记录 ${recordedSha.slice(0, 12)}…）`,
        'yml 与安装包不是同一次构建的产物：删掉 desktop/dist 重新 npm run dist');
    }
    ok(`${name}（${(size / 1048576).toFixed(1)} MB，sha512 对上）`);
    local.push({ name, size });
  }

  // ── 2) 防降级 ────────────────────────────────────────────
  step(2, '防降级（先问线上现在是什么版本）');
  let remoteMeta = null;
  try {
    const r = await httpRequest(url + 'latest.yml', 'GET');
    if (r.status === 200) {
      remoteMeta = parseLatestYml(r.body.toString('utf8'));
      ok(`线上 latest.yml version = ${remoteMeta.version || '（读不出）'}`);
    } else if (r.status === 404) {
      ok('线上还没有 latest.yml（404）——这是第一次发布');
    } else {
      die('防降级', `GET ${url}latest.yml 返回 HTTP ${r.status}`,
        '地址不对或服务器没配好；用浏览器打开这个 URL 看看是什么');
    }
  } catch (e) {
    die('防降级', `连不上 ${url}：${e.message}`,
      '确认地址可达、证书有效（HTTPS 是底线，§8）；本地还没配服务器就先 --dry-run 之外什么都做不了');
  }

  if (remoteMeta && remoteMeta.version) {
    const c = cmpVer(remoteMeta.version, pkg.version);
    if (c > 0) {
      // 把旧包传上去 = 线上版本号倒退。客户端只在「线上版本更高」时才更新，
      // 这一下会让所有用户永远停在新版本之前的旧版，且没有任何报错。
      die('防降级', `线上是 ${remoteMeta.version}（比本次 ${pkg.version} 新），传上去就是无声回退`,
        '确认你要发的版本号；如果是想回滚，请显式提升 package.json 的 version 再出包');
    }
    if (c === 0) {
      // 同一版本：完整就是无需重传；缺文件就是上次传到一半，允许补传（修复）。
      const missing = [];
      for (const name of remoteMeta.files) {
        try {
          const h = await httpRequest(joinUrl(url, name), 'HEAD');
          if (h.status !== 200) missing.push(`${name}（HTTP ${h.status}）`);
        } catch (e) { missing.push(`${name}（${e.message}）`); }
      }
      if (missing.length) {
        console.log(`${TAG} ⚠ 线上已是 ${pkg.version} 但有文件缺失：${missing.join('、')}`);
        console.log(`${TAG}   判定为「上次上传中断」，允许补传（latest.yml 最后传，所以它到了包没到）`);
      } else {
        die('防降级', `线上已经是 ${pkg.version} 且文件完整，无需重传`,
          '确实要重发同一版本（例如改了安装包内容）：提升 version 重新出包——同一 version 两份不同的包，客户端缓存会打架');
      }
    } else {
      ok(`线上 ${remoteMeta.version} < 本次 ${pkg.version}，允许上传`);
    }
  }

  // ── 3) 上传 ──────────────────────────────────────────────
  step(3, '上传');
  if (opt.dryRun) {
    console.log(`${TAG} dry-run：跳过上传（本会上传 ${local.map((f) => f.name).join('、')} + latest.yml）`);
  } else {
    // 顺序：大文件先、latest.yml 最后。yml 是「开关」——客户端只有读到它才知道
    // 有新版本；先传 yml 会让客户端看到新版本却下不到包（404），失败形态很难看。
    // 先传包再传 yml，中途断线的中间态是「客户端看到旧版本」，无感。
    const ordered = local.map((f) => path.join(dist, f.name));
    for (const p of ordered) {
      console.log(`${TAG} scp ${path.basename(p)} …`);
      scp([p], host, dir);
    }
    console.log(`${TAG} scp latest.yml …（最后传：它是客户端认版本的开关）`);
    scp([ymlPath], host, dir);
    ok('三个文件上传完成');
  }

  // ── 4) 闭环验证 ──────────────────────────────────────────
  // dry-run 的语义是「核对远程现状」，不是「核对上传结果」——文件还没传，
  // 拿本次要传的文件名去 HEAD 只会得到 404。所以 dry-run 只回答一个问题：
  // 线上现在是什么版本。上传后这里才应该变成 200 且 version = 本次版本。
  step(4, opt.dryRun
    ? '核对远程现状（dry-run：上传后才谈得上闭环）'
    : '闭环验证（回答「线上现在到底是什么版本」）');
  let verify = null;
  // 线上当前版本，收尾要报。null = 线上还没有 latest.yml（dry-run 首次发布的正常态）。
  let nowMeta = null;
  try {
    verify = await httpRequest(url + 'latest.yml', 'GET');
  } catch (e) {
    die('闭环验证', `连不上 ${url}：${e.message}`, '确认服务器可达；此时线上状态未知，用浏览器打开 latest.yml 人工核对');
  }
  if (verify.status === 404 && opt.dryRun) {
    // 与步骤 2 的「第一次发布」一致。这是 dry-run 的正常终点，不是失败——
    // 曾在这里无条件要求 200，导致「首次发布前先 dry-run 检查」永远走不通。
    ok('线上还没有 latest.yml（404）——与步骤 2 一致，符合首次发布');
    console.log(`${TAG}   真正发布后这一步应是 200 且 version = ${pkg.version}，文件可下载`);
  } else {
    if (verify.status !== 200) {
      die('闭环验证', `GET ${url}latest.yml 返回 HTTP ${verify.status}`,
        '上传可能没成功（目录权限 / 路径不对）；ssh 上去 ls 看看文件在不在');
    }
    nowMeta = parseLatestYml(verify.body.toString('utf8'));
    if (!opt.dryRun && nowMeta.version !== pkg.version) {
      die('闭环验证', `线上 latest.yml 是 ${nowMeta.version}，期望 ${pkg.version}`,
        '上传与读取不对称（CDN 缓存 / 传错了目录）；等一两分钟再 GET 一次，仍不对就人工核对');
    }
    ok(`线上 latest.yml version = ${nowMeta.version}`
      + (opt.dryRun ? `（本次是 ${pkg.version}，上传后这里应变成它）` : ''));
    if (opt.dryRun) {
      // 本次要传的文件线上还不存在是正常的（还没传）；不拿它们 HEAD。
      console.log(`${TAG}   本次要传的文件线上暂不存在属正常：${local.map((f) => f.name).join('、')}`);
    } else {
      for (const f of local) {
        try {
          const h = await httpRequest(joinUrl(url, f.name), 'HEAD');
          if (h.status !== 200) die('闭环验证', `${f.name} HEAD 返回 ${h.status}`, '文件没传上去或不可读；ssh 上去 ls -l 看权限');
          const len = Number(h.headers['content-length'] || 0);
          if (len && len !== f.size) {
            die('闭环验证', `${f.name} 线上大小 ${len} ≠ 本地 ${f.size}`,
              '传坏了（中断 / 磁盘满）；重跑本脚本（线上已是同版本且缺文件，会按「补传」放行）');
          }
          ok(`${f.name} 可下载（${(f.size / 1048576).toFixed(1)} MB）`);
        } catch (e) {
          die('闭环验证', `${f.name} 下载探测失败：${e.message}`, '确认服务器可达；此时先别让用户更新');
        }
      }
    }
  }

  // ── 5) --keep：清理旧包 ──────────────────────────────────
  step(5, `清理旧安装包（--keep ${opt.keep}）`);
  if (opt.dryRun) {
    console.log(`${TAG} dry-run：跳过清理`);
  } else {
    // 列目录只能靠 ssh（scp 没有 list）。只认「<productName> Setup <ver>.exe」这一种
    // 名字，别的一个不碰 —— 删错服务器文件比不删严重得多。
    const prefix = `${pkg.productName} Setup `;
    const re = new RegExp('^' + prefix.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
      + '(\\d+(?:\\.\\d+)+)\\.exe$');
    const listing = ssh(host, `ls -1 ${shq(dir)}`).split(/\r?\n/).map((s) => s.trim()).filter(Boolean);
    const byVer = new Map();
    for (const name of listing) {
      const m = re.exec(name);
      if (!m) continue;
      const v = m[1];
      if (!byVer.has(v)) byVer.set(v, { version: v, names: [] });
      byVer.get(v).names.push(name);
      const bm = name + '.blockmap';
      if (listing.includes(bm)) byVer.get(v).names.push(bm);
    }
    if (!byVer.size) {
      console.log(`${TAG} 服务器上没找到「${prefix}*.exe」，跳过清理（可能目录里还有别的文件，不猜）`);
    } else {
      const referenced = new Set(nowMeta.files || []);
      const doomed = pickStale([...byVer.values()], opt.keep, referenced);
      if (!doomed.length) {
        ok(`现存 ${byVer.size} 个版本，≤ keep=${opt.keep}，无需清理`);
      } else {
        for (const name of doomed) {
          ssh(host, `rm -f -- ${shq(path.posix.join(dir, name))}`);
          console.log(`${TAG} 已删除 ${name}`);
        }
        console.log(`${TAG} 说明：留 ${Math.min(byVer.size, opt.keep)} 版是为了回滚——发错包 / 新包有恶性 bug 时`
          + `旧 exe 没了就只能重出一个全量包（§8）。观察一个版本周期无恙后可 --keep 1`);
      }
    }
  }

  // ── 6) 收尾 ──────────────────────────────────────────────
  step(6, opt.dryRun ? 'dry-run 完成' : '发布完成');
  console.log(`${TAG} 线上地址：${url}latest.yml（version ${nowMeta ? nowMeta.version : '暂无，发布后为 ' + pkg.version}）`);
  console.log(`${TAG} 客户端：设 TALKSCRIPT_UPDATE_URL=${url} 重启应用，设置页 → 关于与更新 应显示「已是最新版本」`);
  if (!opt.dryRun) {
    console.log(`${TAG} 别忘了：README 的发布清单里记一笔（版本 / 日期 / sha512 出处）`);
  }
}

// 直接执行才跑；被 import（单测）时不启动 —— 否则一 import 就开始传文件。
const invokedDirectly = process.argv[1] !== undefined
  && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url);
if (invokedDirectly) {
  main().catch((e) => {
    console.error(`\n${TAG} 未预期的失败：${(e && e.stack) || e}`);
    process.exit(1);
  });
}
