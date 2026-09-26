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
function installFakeElectron({ lock = true, withWindow = false, isPackaged = false,
                                loadURLRejects = false, dialogChoice = 1 } = {}) {
  const calls = { errorBoxes: [], quits: 0, messages: [], loadedUrls: [], webOn: {},
                  ipc: {}, appOn: {}, sent: [], messageBoxes: [] };
  const userData = fs.mkdtempSync(path.join(os.tmpdir(), 'ts-main-test-'));
  // ── 测试基建：engine 桩目录（审查 2026-09-26 §2.2 的次生脆弱）─────────────
  // 真跑 main.js（打包态假象）时 rootDirCached = desktop/engine，它是 spawn 的
  // **cwd** —— Windows 上 cwd 不存在时 spawn 直接 ENOENT（cmd 存不存在都一样）。
  // 曾以为「TALKSCRIPT_PYTHON 指向 node 自己 → spawn 会成功」，实际每次都失败，
  // 三条窗口测试全靠 mock fetch 的微任务时序碰巧过关。这里幂等地把桩目录建出来，
  // 让 spawn **真的**成功：node 随后因 `-m` 不是合法参数自己退出，正是下面
  // bootMainWithWindow 注释一直声称的前提。桩目录是测试基建，与产品无关
  // （electron-builder 的产物清单里没有它）。
  fs.mkdirSync(path.join(__dirname, 'engine'), { recursive: true });
  const app = {
    isPackaged,
    getPath: (k) => (k === 'userData' ? userData : userData),
    getVersion: () => '9.9.9-test',
    whenReady: () => Promise.resolve(),
    requestSingleInstanceLock: () => lock,
    quit: () => { calls.quits += 1; },
    // 记录 app 级事件（before-quit / window-all-closed）：测试收尾时手动触发，
    // 好让 main.js 的 clearUpdaterTimers() 真的跑掉 —— 否则那条 6h 的 setInterval
    // 会让 node --test **永远不退出**（实测挂到 180s 超时被 SIGTERM）。
    on: (ev, h) => { (calls.appOn[ev] = calls.appOn[ev] || []).push(h); },
  };
  const fake = {
    app,
    dialog: {
      showErrorBox: (title, detail) => calls.errorBoxes.push([title, detail]),
      // 记录并按 dialogChoice 应答：「重启引擎 / 退出应用」二选一（默认退出）。
      // main.js 调用的是 (win, opts) 两参形态，记 opts（有的调用只有 opts 一个参）。
      showMessageBoxSync: (winOrOpts, opts) => {
        calls.messageBoxes.push(opts || winOrOpts);
        return dialogChoice;
      },
      showMessageBox: () => Promise.resolve(),
    },
    Menu: { setApplicationMenu: () => {} },
    // 自动更新的 IPC handler（updater:check）。这两条失败路径都与更新无关，
    // 桩住即可——但要**在**：main.js 顶层就 ipcMain.handle，缺这个键 = TypeError，
    // 整个 main.js 都 require 不进来（症状是上面两条测试一起红，指错方向）。
    // 记录注册的 handler：测试据此直接调 `updater:getStatus` 等（不经过真 IPC）。
    ipcMain: { handle: (ch, h) => { calls.ipc[ch] = h; } },
    BrowserWindow: withWindow ? class {
      // 真跑 createWindow 时要一个**能过**的假窗口：只实现 main.js 实际用到的那几个
      // 成员。`webContents.on` 把 handler 记进 calls.webOn，测试据此直接调
      // will-navigate 的 handler —— 那是 Electron 事件，渲染层回归网（Chrome）里没有它。
      constructor() {
        this.webContents = {
          on: (ev, h) => { (calls.webOn[ev] = calls.webOn[ev] || []).push(h); },
          send: (ch, payload) => { calls.sent.push([ch, payload]); },
          toggleDevTools: () => {},
          setWindowOpenHandler: (h) => { calls.windowOpenHandler = h; },
        };
        // loadURLRejects：模拟 P2-6 的失败形态 —— 健康检查通过之后、加载完成之前
        // 引擎死了，loadURL 的 promise reject。修复前这个 rejection 无人接，
        // Node 20 下未处理 rejection 直接打崩进程（这条测试本身因此能红）。
        this.loadURL = (u) => {
          calls.loadedUrls.push(u);
          return loadURLRejects
            ? Promise.reject(new Error('ERR_CONNECTION_REFUSED（模拟：健康检查通过后引擎死了）'))
            : Promise.resolve();
        };
      }
      isDestroyed() { return false; }
      isMinimized() { return false; }
      restore() {}
      show() {}
      focus() {}
      on() {}
      static getAllWindows() { return []; }
    } : class {
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

/** 删掉临时目录，但**等 fd 真正释放**（审查 2026-09-26 §2.2 的夹具竞态）。
 *
 *  为什么不能裸 rmSync：before-quit 里的 closeEngineLog() 对 engine.log 的
 *  WriteStream 调 end() —— 关流是**异步**的（flush → close）。同 tick 就删，
 *  Windows 会把 unlink 记成 delete-pending，父目录 rmdir 时文件还在
 *  → ENOTEMPTY，三条测试随机挂（实测连续失败）。
 *  修法：反复尝试删除（每 50ms 一轮），fd 一释放立刻成功；上限 5s，
 *  仍失败就把最后一个错误抛出来 —— 真回归不该被吞成绿。 */
async function rmDirWhenReleasable(dir) {
  for (let i = 0; ; i++) {
    try {
      fs.rmSync(dir, { recursive: true, force: true });
      return;
    } catch (e) {
      if (i >= 100) throw e;   // 100 × 50ms = 5s 上限
      await new Promise((r) => setTimeout(r, 50));
    }
  }
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
    await rmDirWhenReleasable(userData);
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
    await rmDirWhenReleasable(userData);
  }
});

// ── 顶层导航守卫（P2-17）+ 更新链路启用态先推 idle（P2-1）──────────────
// 这两条**只能在真跑 main.js 时验**：
//   · `will-navigate` 是 Electron 的事件，渲染层回归网（Chrome）里根本没有它；
//   · 「启用分支必须推 idle」是主进程的编排 —— 渲染层那条断言用的是**桩**
//     （桩推什么由测试决定），验不到 main.js（见 _verify/verify.js 的注释）。
// 做法：假 electron 给一个**能过**的窗口；再把全局 fetch 换掉 —— waitHealth 就是
// 靠它判引擎起没起来的（`http://127.0.0.1:<port>/api/health`），mock 掉就不必真起引擎。
// spawn 也不 stub：把 TALKSCRIPT_PYTHON 指向 node 自己，installFakeElectron 里的
// desktop/engine 桩目录让 spawn **真的**成功（进程随后因 `-m` 非法自己退出），
// 于是 waitHealth 里 `if (!engineProc) throw` 不会把"进程退出了"误判成"没起来"。
async function bootMainWithWindow(env = {}, { healthBody, loadURLRejects } = {}) {
  const ctx = installFakeElectron({ lock: true, withWindow: true, isPackaged: true,
                                    loadURLRejects });
  const realFetch = globalThis.fetch;
  // 健康检查的应答体：默认模拟**我们自己的引擎**（回显壳给它的 nonce）。
  // 传 healthBody 可以模拟「端口被别的进程占了」—— 回 200，但不知道 nonce。
  const nonce = env.TALKSCRIPT_HEALTH_NONCE || 'test-health-nonce';
  const body = healthBody === undefined ? { ok: true, nonce } : healthBody;
  globalThis.fetch = async () => ({ ok: true, json: async () => body });
  const saved = {};
  for (const [k, v] of Object.entries({
    TALKSCRIPT_PYTHON: process.execPath,
    TALKSCRIPT_HEALTH_NONCE: nonce,
    ...env,
  })) {
    saved[k] = process.env[k];
    process.env[k] = v;
  }
  delete require.cache[require.resolve('./main.js')];
  require('./main.js');
  return {
    ...ctx,
    async cleanup() {
      // 先触发 main.js 的 before-quit：它会 killEngine() + clearUpdaterTimers()。
      // 不触发的话 15s 首查与 6h 轮询两个定时器还活着，node --test 收不了尾。
      for (const h of (ctx.calls.appOn['before-quit'] || [])) h();
      globalThis.fetch = realFetch;
      for (const [k, v] of Object.entries(saved)) {
        if (v === undefined) delete process.env[k]; else process.env[k] = v;
      }
      // rmSync 必须等 WriteStream 的 fd 真正释放（见 rmDirWhenReleasable 的注释）：
      // 上一版这里同 tick 裸删，三条测试随机 ENOTEMPTY（审查 2026-09-26 §2.2）。
      await rmDirWhenReleasable(ctx.userData);
    },
  };
}

test('顶层导航只放行引擎同源：远端 / file: / 换端口被拦，引擎 URL 放行（P2-17）', async () => {
  const ctx = await bootMainWithWindow({ TALKSCRIPT_UPDATE_URL: 'https://u.example/ts/' });
  try {
    const ok = await waitFor(() => (ctx.calls.webOn['will-navigate'] || []).length > 0, 20000,
      'will-navigate handler 没注册（createWindow 没跑到？）');
    assert.ok(ok, '没等到 will-navigate handler');
    const nav = ctx.calls.webOn['will-navigate'][0];
    const blocked = [];
    const probe = (url) => nav({ preventDefault: () => blocked.push(url) }, url);
    probe('https://evil.example/steal');
    probe('file:///C:/Windows/win.ini');
    probe('http://127.0.0.1:1/not-our-port');
    const engineUrl = ctx.calls.loadedUrls[0];
    assert.ok(engineUrl, '没记录到 loadURL —— 窗口没走到加载那一步');
    probe(engineUrl);                     // 正对照：引擎同源必须放行
    assert.deepStrictEqual(blocked,
      ['https://evil.example/steal', 'file:///C:/Windows/win.ini',
       'http://127.0.0.1:1/not-our-port'],
      '远端 / file: / 换端口必须被拦，引擎同源必须放行（否则界面根本加载不了）');
  } finally { await ctx.cleanup(); }
});

test('更新链路启用时先推一条 idle（面板常驻文案的锚点，P2-1）', async () => {
  const ctx = await bootMainWithWindow({ TALKSCRIPT_UPDATE_URL: 'https://u.example/ts/' });
  try {
    // ⚠ 等的是**推送**，不是 handler 注册：`ipcMain.handle` 在 main.js 顶层就注册了
    //   （立刻满足），而 idle 是 createWindow 之后由 initUpdater 推的 ——
    //   拿 handler 当信号会等到一个"还没开始推"的时刻（实测就是这么假红的）。
    const ok = await waitFor(
      () => ctx.calls.sent.some(([ch]) => ch === 'updater:status'), 20000,
      '没有任何 updater:status 推送');
    assert.ok(ok, '没等到任何状态推送');
    // ⚠ 判据必须是「**推过**一条 idle」，**不能**看 `getStatus()` 的返回值 ——
    //   `updaterStatus` 的**初始值**本来就是 `{state:'idle'}`（main.js 顶部），
    //   于是"推了"与"没推"从 getStatus 上看完全一样。这正是这个缺陷能藏住的原因：
    //   变异检验实测过 —— 删掉那句推送后，只读 getStatus 的断言照样绿。
    //   要守的是 `webContents.send('updater:status', …)` 这个**动作**。
    const pushed = ctx.calls.sent
      .filter(([ch]) => ch === 'updater:status')
      .map(([, s]) => s.state);
    assert.ok(pushed.includes('idle'),
      '启用态没有**推**过 idle —— 渲染层 statusText 的 idle 分支就成了永不显示的死分支，'
      + '「本应用有自动更新」在界面上没有常驻痕迹。实际推送过：' + JSON.stringify(pushed));
  } finally { await ctx.cleanup(); }
});

// ── 健康检查的身份校验（P2-16）────────────────────────────────
// 端口在「探测到空闲」与「引擎真正绑定」之间有窗口。本机别的进程抢到它并回一个 200 时，
// 壳若只看 `r.ok` 就会继续走 loadURL，把**一次性访问令牌**带进那个进程的请求行 ——
// 而 engine-path.js 顶部论证的正是「令牌不能落到别的本机进程手里」。
// 所以健康检查要看应答者知不知道 nonce（nonce 只经子进程 env 给引擎）。
test('健康检查认身份：回 200 但不知道 nonce 的冒充者不算引擎起来了（P2-16）', async () => {
  const ctx = await bootMainWithWindow(
    { TALKSCRIPT_UPDATE_URL: 'https://u.example/ts/' },
    { healthBody: { ok: true } });          // 冒充者：200，但没有 nonce
  try {
    const ok = await waitFor(() => ctx.calls.errorBoxes.length > 0, 20000,
      '冒充者的 200 被当成「引擎起来了」—— 没有报「引擎启动失败」');
    assert.ok(ok, '没报错：令牌会发到冒充者手上');
    assert.match(ctx.calls.errorBoxes[0][1], /不是我们的引擎/,
      '报错文案没指出根因（用户看不出是端口被占）');
    assert.deepStrictEqual(ctx.calls.loadedUrls, [],
      '用了被占的端口 loadURL —— 一次性令牌已经带进对方的请求行了');
  } finally { await ctx.cleanup(); }
});

test('健康检查认身份：带正确 nonce 的应答被接受（正对照，否则上面那条可能只是"永远失败"）', async () => {
  const ctx = await bootMainWithWindow({ TALKSCRIPT_UPDATE_URL: 'https://u.example/ts/' });
  try {
    const ok = await waitFor(() => ctx.calls.loadedUrls.length > 0, 20000,
      '正确 nonce 的应答没被接受（loadURL 没发生）');
    assert.ok(ok, '引擎没起来');
    assert.match(ctx.calls.loadedUrls[0], /token=/, 'loadURL 里没有令牌');
  } finally { await ctx.cleanup(); }
});

// ── P2-5：旧引擎的 exit 事件晚到，不许清掉在跑的新引擎 ───────────────────────
// 竞态本体：两试循环里 attempt 0 被杀（killEngine）后，它的 exit 事件可能**晚于**
// attempt 1 的 spawn 落地才派发。修复前处理器无条件 engineProc = null —— 把在跑的
// 新引擎引用清掉 → waitHealth 误报「引擎进程未运行」+ 给活引擎弹「引擎已退出」。
//
// 为什么这条要 stub spawn（文件头「不去 stub child_process」的例外）：
// 那句话守的是缺陷 6/7 —— 被验的**就是**真 spawn 的行为。这条被验的是 exit 处理器
// 的**身份校验**，而「旧 exit 晚于新 spawn」在真实进程上是 OS 时序（kill 之后
// exit 事件何时派发不可控），写不出能稳定红的断言。桩掉 spawn 桩掉的不是被验的
// 那件事，而是把事件**派发时机**拿到测试手里：旧进程被 kill 后不发 exit，
// 等新引擎落地后由测试手动派发 —— 对那条竞态做确定性重放。
test('P2-5 旧引擎 exit 晚到：不清新引擎引用、不弹「引擎已退出」、不退出应用', async () => {
  const ctx = installFakeElectron({ lock: true, withWindow: true, isPackaged: true,
                                    dialogChoice: 1 });
  const cp = require('child_process');
  const realSpawn = cp.spawn;
  const procs = [];
  let spawnCalls = 0;
  cp.spawn = () => {
    const p = new (require('events').EventEmitter)();
    p.stdout = new (require('events').EventEmitter)();
    p.stderr = new (require('events').EventEmitter)();
    // kill 只标记，**不发 exit** —— 「旧退场晚到」由测试在新引擎落地后手动派发。
    p.kill = () => { p.killed = true; };
    procs.push(p);
    spawnCalls += 1;
    return p;
  };
  const realFetch = globalThis.fetch;
  const nonce = 'p25-identity-nonce';
  let healthCalls = 0;
  // 第 1 次（attempt 0）回 200 但 nonce 不对 → 身份不符立刻抛 → 主循环
  // killEngine 杀旧引擎、起 attempt 1；此后全对 → attempt 1 成功。
  globalThis.fetch = async () => {
    healthCalls += 1;
    return { ok: true, json: async () => (healthCalls === 1 ? { ok: true } : { ok: true, nonce }) };
  };
  const saved = {};
  for (const [k, v] of Object.entries({
    TALKSCRIPT_PYTHON: process.execPath,
    TALKSCRIPT_HEALTH_NONCE: nonce,
  })) { saved[k] = process.env[k]; process.env[k] = v; }
  try {
    delete require.cache[require.resolve('./main.js')];
    require('./main.js');
    // attempt 1 成功 = createWindow 跑到 loadURL。此时 engineProc 指向 procs[1]，
    // procs[0] 被 kill 过但它的 exit 事件「还没到」。
    await waitFor(() => ctx.calls.loadedUrls.length > 0, 20000,
      'attempt 1 没起来（loadURL 没发生）——身份不符后的换端口重试坏了');
    assert.strictEqual(spawnCalls, 2, '两试循环只 spawn 了一次？前提不成立');
    assert.ok(procs[0].killed, '旧引擎没被 killEngine 杀掉：竞态前提不成立');
    assert.strictEqual(ctx.calls.quits, 0, '前置：成功路径不该有 quit');

    // ── 竞态重放：旧引擎的 exit 事件**现在**才到（新引擎已在跑）──────────
    procs[0].emit('exit', 1, null);
    // 修复前：engineProc 被无条件清成 null → 接着弹「引擎已退出」→ 选「退出应用」
    // → app.quit()。修复后：身份不符（当前是 procs[1]）→ 只留日志，什么都不碰。
    assert.strictEqual(ctx.calls.quits, 0,
      '旧引擎的迟到 exit 清掉了在跑的新引擎并触发退出（P2-5 回归）');
    assert.strictEqual(ctx.calls.messageBoxes.length, 0,
      '给活着的引擎弹了「引擎已退出」：新引擎被旧进程的退场误判成死了');

    // ── 正对照：守卫不许矫枉过正 ──────────────────────────────────────
    // 新引擎（当前引擎）自己退场时，「引擎已退出 → 退出应用」路径必须照常工作。
    procs[1].emit('exit', 1, null);
    assert.strictEqual(ctx.calls.messageBoxes.length, 1,
      '当前引擎退场该弹的「引擎已退出」没了：身份校验把正主也拦了');
    assert.strictEqual(ctx.calls.messageBoxes[0].title, '引擎已退出');
    assert.strictEqual(ctx.calls.quits, 1,
      '「引擎已退出」弹框后选了「退出应用」，app.quit() 没被调');
  } finally {
    for (const h of (ctx.calls.appOn['before-quit'] || [])) h();
    cp.spawn = realSpawn;
    globalThis.fetch = realFetch;
    for (const [k, v] of Object.entries(saved)) {
      if (v === undefined) delete process.env[k]; else process.env[k] = v;
    }
    await rmDirWhenReleasable(ctx.userData);
  }
});

// ── P2-6：初始 loadURL 的 rejection 有人接 ────────────────────────────────────
// 引擎在「健康检查通过」与「加载完成」之间死掉 → win.loadURL() reject。修复前这个
// promise 被丢弃：Node 20 下未处理 rejection 默认打崩进程，主进程死在
// 「界面加载失败」对话框之外，用户只看到应用消失。修复后 catch 接住并走与
// did-fail-load 同一张「界面加载失败」框。假窗口的 loadURL 直接返回 rejected
// promise（installFakeElectron 的 loadURLRejects），修复前这条测试会以
// unhandledRejection 的形式把测试进程打崩 —— 天然能红。
test('P2-6 初始 loadURL 的 rejection 有人接：弹「界面加载失败」并把原因留进日志', async () => {
  const ctx = await bootMainWithWindow({}, { loadURLRejects: true });
  try {
    await waitFor(() => ctx.calls.errorBoxes.length > 0, 20000,
      'loadURL 的 rejection 没被接住（修复前这里是 unhandled rejection，直接打崩测试进程）');
    assert.strictEqual(ctx.calls.errorBoxes[0][0], '界面加载失败',
      '走的不是 did-fail-load 同一张框：失败形态又分叉了');
    assert.match(ctx.calls.errorBoxes[0][1], /ERR_CONNECTION_REFUSED/,
      '框里没有失败原因，用户无从下手');
    // 出口的另一半：原因必须留进引擎日志（对话框可以被手滑关掉，日志不会）。
    const logPath = path.join(ctx.userData, 'logs', 'engine.log');
    await waitFor(() => /\[load\] 初始界面加载失败/.test(
      fs.existsSync(logPath) ? fs.readFileSync(logPath, 'utf8') : ''), 5000,
      'rejection 的原因没进引擎日志');
  } finally { await ctx.cleanup(); }
});
