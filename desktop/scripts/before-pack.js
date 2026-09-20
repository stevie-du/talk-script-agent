// electron-builder 的 `beforePack` 钩子：保证打包前内嵌 Python 运行时是新的。
//
// 为什么挂在钩子上，而不是在 `npm run dist` 里串一条命令
// ------------------------------------------------------
// 串命令只有走 npm script 才会跑；直接调 `npx electron-builder` 就漏了。
// 而漏了的后果是**安装包里没有解释器** —— 构建过程一句提示都没有，
// 直到用户装完启动才炸。挂钩子之后「怎么调用 electron-builder」都不影响结果。
//
// 脚本本身是幂等的（带 stamp：Python 版本 / zip sha256 / 依赖清单 三者任一变化
// 才重建），所以重复触发不会拖慢构建。
'use strict';
const fs = require('fs');
const path = require('path');
const { execFileSync } = require('child_process');

// 安装包不能带上 `packs/*/private/`：README 承诺私有目录（未公开的型号参数、
// 客户案例）不出机器，而执行它的只是 package.json 里 `"filter"` 的一个数组元素。
// 删掉它构建照样成功、一句提示都没有，泄露要等安装包被别人解压才发现 ——
// 所以这里在打包前把它拦成构建失败。纯判定拆成 privatePackLeak()，
// 好让 desktop/packaging.test.js 在没有 packs/、没有 electron-builder 的环境下也能钉住。
const PRIVATE_GLOB = '!**/private/**';

/** 排除规则没生效 + 磁盘上真有 private/ → 返回会被带上的目录；否则返回 []。 */
function privatePackLeak(extraResources, privateDirs) {
  const entry = (extraResources || []).find(r => r.from === '../packs');
  const excludes = (entry?.filter || []).some(
    g => typeof g === 'string' && g.startsWith('!') && g.includes('private'));
  return excludes ? [] : privateDirs;
}

function assertPrivateExcluded() {
  const root = path.join(__dirname, '..', '..');           // 仓库根（不是 desktop/）
  const cfg = JSON.parse(fs.readFileSync(path.join(root, 'desktop', 'package.json'), 'utf8'));
  const privateDirs = [];
  const packsDir = path.join(root, 'packs');
  if (fs.existsSync(packsDir)) {
    for (const dir of fs.readdirSync(packsDir)) {
      if (fs.existsSync(path.join(packsDir, dir, 'private'))) privateDirs.push(`packs/${dir}/private`);
    }
  }
  const leak = privatePackLeak(cfg.build?.extraResources, privateDirs);
  if (leak.length) {
    throw new Error(
      '打包会带上行业包私有目录，违反 README 的"private/ 不出机器"承诺。\n' +
      `  涉及：${leak.join('、')}\n` +
      '  修法：desktop/package.json → build.extraResources 里 ../packs 那条加\n' +
      `        "filter": ["**/*", "${PRIVATE_GLOB}"]`);
  }
  if (privateDirs.length) {
    console.log(`[before-pack] 私有目录已排除 ${privateDirs.length} 个：${privateDirs.join('、')}`);
  }
}

async function beforePack() {
  assertPrivateExcluded();
  execFileSync(process.execPath,
               [path.join(__dirname, 'build-python-runtime.mjs')],
               { stdio: 'inherit' });
}

module.exports = beforePack;
module.exports.default = beforePack;   // electron-builder 两种取法都兼容
module.exports.privatePackLeak = privatePackLeak;
module.exports.PRIVATE_GLOB = PRIVATE_GLOB;
