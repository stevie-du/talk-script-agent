// main.js 的失败路径测试（Node 内置 test runner）。跑法：node --test desktop/
//
// 为什么要真起 main.js：缺陷 6 与 7 的全部内容都是「进程与窗口的真实行为」——
//   · spawn 失败时 Node 只 emit 'error'，**没有监听者就是未捕获异常**：
//     以前 `TALKSCRIPT_PYTHON` 指错 = 主进程崩掉 = 用户看到白屏，40 秒后
//     才有一句「健康检查超时」（而且什么都没写进文件）；
//   · 单实例锁没抢到的那个副本必须**一个引擎都不起**。
// 这两件事读码确认不了，只能把 main.js 真跑一遍。
//
// 做法：用 require.cache 塞一个假的 `electron`（main.js 唯一的外部耦合面），
// 然后把 `TALKSCRIPT_PYTHON` 指向一个不存在的程序，让**真的 spawn** 去失败。
// 不去 stub child_process —— 那等于把要验的那件事本身给桩掉了。
'use strict';
const test = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const os = require('os');
const path = require('path');
const Module = require('module');

const ELECTRON_ID = require.resolve('electron');

/** 装一个假的 electron 模块，返回它 + 收集到的调用记录。 */
function installFakeElectron({ lock = true } = {}) {
  const calls = { errorBoxes: [], quits: 0, messages: [] };
  const userData = fs.mkdtempSync(path.join(os.tmpdir(), 'ts-main-test-'));
  const app = {
    isPackaged: false,
    getPath: (k) => (k === 'userData' ? userData : userData),
    getVersion: () => '9.9.9-test',
    whenReady: () => Promise.resolve(),
    requestSingleInstanceLock: () => lock,
    quit: () => { calls.quits += 1; },
    on: () => {},
  };
  const fake = {
    app,
    dialog: {
      showErrorBox: (title, detail) => calls.errorBoxes.push([title, detail]),
      showMessageBoxSync: () => 1,          // 「重启引擎 / 退出应用」→ 选退出
      showMessageBox: () => Promise.resolve(),
    },
    Menu: { setApplicationMenu: () => {} },
    BrowserWindow: class {
      constructor() { throw new Error('测试里不该开出窗口'); }
      static getAllWindows() { return []; }
    },
    shell: { openExternal: () => {} },
  };
  // 关键一步：让 main.js 里的 require('electron') 拿到这个对象
  const m = new Module(ELECTRON_ID, null);
  m.filename = ELECTRON_ID;
  m.loaded = true;
  m.exports = fake;
  require.cache[ELECTRON_ID] = m;
  return { fake, calls, userData };
}

async function waitFor(pred, timeout = 25000, label = '') {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    if (pred()) return true;
    await new Promise((r) => setTimeout(r, 100));
  }
  assert.fail(`超时：${label || '条件未成立'}`);
}

test('TALKSCRIPT_PYTHON 指错时报错有真内容，并且引擎日志落到用户数据目录', async () => {
  const { calls, userData } = installFakeElectron({ lock: true });
  const bogus = path.join(userData, 'no-such', 'python.exe');
  process.env.TALKSCRIPT_PYTHON = bogus;
  try {
    delete require.cache[require.resolve('./main.js')];
    require('./main.js');
    await waitFor(() => calls.errorBoxes.length > 0, 25000, '没有弹出「引擎启动失败」');

    const [title, detail] = calls.errorBoxes[0];
    assert.match(title, /引擎/);
    // 缺陷 7：说的必须是"找不到引擎程序"，而不是「健康检查超时」那种指不到根因的话
    assert.match(detail, /找不到引擎程序/, detail);
    assert.ok(detail.includes('no-such'), '没把用户填的那个路径回显出来：' + detail);
    assert.match(detail, /TALKSCRIPT_PYTHON/, '没提示是环境变量指错了');

    // 缺陷 7 的另一半：README 让用户看的日志文件必须**真的在盘上**。
    // 写的是 WriteStream（end() 之后才落盘），所以要**轮询到内容出现**再断言，
    // 存在性检查会飘。
    const logPath = path.join(userData, 'logs', 'engine.log');
    assert.ok(fs.existsSync(logPath), '没有写日志文件：' + logPath);
    await waitFor(() => /\[spawn-error\]/.test(fs.readFileSync(logPath, 'utf8')),
      5000, '日志里没有 [spawn-error] 这一行');
    const log = fs.readFileSync(logPath, 'utf8');
    assert.match(log, /\[spawn\] via=TALKSCRIPT_PYTHON/, log);
    assert.match(log, /\[spawn-error\]/, log);
    assert.ok(log.includes(bogus), '日志里没有那条命令，排查时无从下手');

    // 缺陷 1 的落盘面：日志与对话框都不许出现令牌（这里令牌照常生成，只是不该外泄）
    assert.ok(!/TALKSCRIPT_TOKEN/.test(log), '日志里出现了环境变量名与值');
    assert.ok(!/--token/.test(log), 'argv 里又出现 --token 了：' + log);
  } finally {
    delete process.env.TALKSCRIPT_PYTHON;
    fs.rmSync(userData, { recursive: true, force: true });
  }
});

test('没抢到单实例锁：直接退出，一个引擎都不起（缺陷 6）', async () => {
  const { calls, userData } = installFakeElectron({ lock: false });
  try {
    delete require.cache[require.resolve('./main.js')];
    require('./main.js');
    await waitFor(() => calls.quits > 0, 5000, '没抢到锁却没有 app.quit()');
    // 不起引擎 = 不开日志文件、不 spawn、不弹框（第二次启动是用户手滑，不该骂他）
    assert.strictEqual(calls.errorBoxes.length, 0);
    assert.ok(!fs.existsSync(path.join(userData, 'logs', 'engine.log')),
      '第二个副本仍然去起引擎了 → 两份 generated/index.json 会互相覆盖');
  } finally {
    fs.rmSync(userData, { recursive: true, force: true });
  }
});
