// 打包边界的测试（Node 内置 test runner）。跑法：node --test desktop/
//
// 为什么值得单独钉：README 承诺 `packs/*/private/`（未公开型号参数、客户案例）
// 不出机器，而守住这句话的只是 desktop/package.json 里的一个 glob 字符串。
// 全局搜"有没有 filter"是没用的绿灯 —— 写成 `!**/private` 也照样"有"，
// 但目录内容一个都排除不掉。所以这里拿 electron-builder 真正用的匹配器
// （minimatch）去匹配磁盘上真实存在的私有文件路径，让断言落在行为上。
'use strict';
const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const { minimatch } = require('minimatch');
const beforePack = require('./scripts/before-pack');
const { privatePackLeak } = beforePack;

const ROOT = path.join(__dirname, '..');
const cfg = JSON.parse(fs.readFileSync(path.join(ROOT, 'desktop', 'package.json'), 'utf8'));
const packsEntry = (cfg.build.extraResources || []).find(r => r.from === '../packs');

/** 磁盘上真实存在的私有文件，路径按 extraResources 的口径相对 ../packs。 */
function realPrivateFiles() {
  const out = [];
  const packsDir = path.join(ROOT, 'packs');
  if (!fs.existsSync(packsDir)) return out;
  for (const dir of fs.readdirSync(packsDir)) {
    const priv = path.join(packsDir, dir, 'private');
    if (!fs.existsSync(priv)) continue;
    for (const name of fs.readdirSync(priv)) out.push(`${dir}/private/${name}`);
  }
  return out;
}

test('排除规则真的匹配得上磁盘上的私有文件（而不是只写了个样子）', () => {
  const files = realPrivateFiles();
  if (!files.length) return;                 // 本机没有私有目录时这条无意义，不算通过也不算失败
  const negatives = (packsEntry.filter || []).filter(g => g.startsWith('!'));
  for (const f of files) {
    assert.ok(negatives.some(g => minimatch(f, g.slice(1))),
      `${f} 不会被任何排除规则命中，会留在安装包里`);
  }
});

test('排除规则不能顺手误伤正常词表文件', () => {
  const negatives = (packsEntry.filter || []).filter(g => g.startsWith('!'));
  for (const keep of ['elevator/pack.yaml', 'elevator/lexicon.yaml', 'elevator/templates/reply.md']) {
    assert.ok(!negatives.some(g => minimatch(keep, g.slice(1))),
      `排除规则过宽，把 ${keep} 也删掉了 —— 装完就是"没有可用行业包"`);
  }
});

test('privatePackLeak：没有排除规则时报出目录', () => {
  const dirs = ['packs/elevator/private'];
  assert.deepStrictEqual(privatePackLeak([{ from: '../packs' }], dirs), dirs);
  assert.deepStrictEqual(privatePackLeak([{ from: '../packs', filter: ['**/*'] }], dirs), dirs);
  assert.deepStrictEqual(privatePackLeak([{ from: '../packs', filter: ['**/*', '!**/private/**'] }], dirs), []);
  assert.deepStrictEqual(privatePackLeak([], []), []);   // 没打包内容也没私有目录 → 不该拦
});

// ── main.js 的每个同级 require 都必须在 build.files 里 ──────────
// 病灶（2026-09-23 五路审查 P0-1）：engine-dialogs.js 从 main.js 拆出来时
// 忘了加进 build.files。electron-builder 的 files 是**白名单、替换默认值**，
// 漏一个 sibling 文件的表现是「构建成功、装完启动即 MODULE_NOT_FOUND」，
// 一句提示都没有。而 verify-package 判据 E 只正向核对 files 列出的文件在不在
// asar 里，从不反向枚举 main.js require 了什么 —— 所以 1112 文件对账全绿，
// 照样认证出一个坏包。
// 这条断言就是那个盲区的守卫：**枚举 require，而不是枚举 files**。
// 以后任何人再从 main.js 拆模块忘加 files，这里立刻红。
test('main.js require 的每个同级模块都在 build.files 白名单里', () => {
  const mainSrc = fs.readFileSync(path.join(__dirname, 'main.js'), 'utf8');
  const reqs = [...mainSrc.matchAll(/require\('\.\/([\w.-]+)'\)/g)].map(m => m[1]);
  assert.ok(reqs.length >= 2, 'main.js 里一个同级 require 都没解析到，本断言会空转');
  // require 不带后缀（require('./engine-path')），files 里带（"engine-path.js"）——
  // 两边都褪掉 .js 再比，否则这条断言会因为"形状不同"恒红，白挨。
  const norm = new Set(((cfg.build && cfg.build.files) || [])
    .map(f => String(f).replace(/\.js$/, '')));
  const missing = reqs.filter(r => !norm.has(r));
  assert.deepStrictEqual(missing, [],
    '这些模块被 main.js require，但不在 build.files 里 —— 打出来的包装完即崩：' + missing.join(', '));
});
