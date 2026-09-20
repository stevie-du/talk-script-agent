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
