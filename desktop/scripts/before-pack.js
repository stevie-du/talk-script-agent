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
const path = require('path');
const { execFileSync } = require('child_process');

async function beforePack() {
  execFileSync(process.execPath,
               [path.join(__dirname, 'build-python-runtime.mjs')],
               { stdio: 'inherit' });
}

module.exports = beforePack;
module.exports.default = beforePack;   // electron-builder 两种取法都兼容
