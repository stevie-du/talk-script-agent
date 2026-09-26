// resolveEngine 的降级链测试（Node 内置 test runner，零依赖）。
//
// 跑法：node --test desktop/
//
// 为什么要有它：P1-3 的核心就是**顺序**——出厂自带的运行时必须排在
// PyInstaller 产物之前。顺序错了的表现是「装完启动还是找不到解释器」，
// 而读码确认不了（`if` 的先后很容易看漏，改起来也容易顺手挪）。
// 所以把纯逻辑拆进 engine-path.js，在这里把每条路径与顺序都钉住。
'use strict';
const test = require('node:test');
const assert = require('node:assert');
const path = require('path');
const { resolveEngine } = require('./engine-path');

const RES = 'C:\\app\\resources';
const ROOT = 'C:\\app\\resources\\engine';

/** 造一个「这些路径存在」的 exists，其余一律不存在。 */
const only = (...paths) => {
  const set = new Set(paths.map((p) => path.resolve(p)));
  return (p) => set.has(path.resolve(p));
};

const base = (over = {}) => ({
  rootDir: ROOT, resourcesDir: RES, dataDir: 'C:\\userData',
  version: '9.9.9', port: 12345, token: 'tk',
  platform: 'win32', env: {}, exists: () => false,
  ...over,
});

const PY = path.join(RES, 'engine', 'py', 'python.exe');
const EXE = path.join(RES, 'engine', 'engine.exe');
const VENV = path.join(ROOT, '.venv', 'Scripts', 'python.exe');

test('TALKSCRIPT_PYTHON 优先于一切', () => {
  const r = resolveEngine(base({
    env: { TALKSCRIPT_PYTHON: 'D:\\py\\python.exe' },
    exists: only(PY, EXE, VENV),
  }));
  assert.strictEqual(r.cmd, 'D:\\py\\python.exe');
  assert.strictEqual(r.via, 'TALKSCRIPT_PYTHON');
});

test('出厂运行时优先于 engine.exe —— 这是 P1-3 的核心顺序', () => {
  const r = resolveEngine(base({ exists: only(PY, EXE) }));
  assert.strictEqual(r.cmd, PY);
  assert.match(r.via, /出厂运行时/);
});

test('没有出厂运行时时才轮到 engine.exe', () => {
  const r = resolveEngine(base({ exists: only(EXE) }));
  assert.strictEqual(r.cmd, EXE);
});

test('engine.exe 不带 -m app.server（它已经封好了）', () => {
  const r = resolveEngine(base({ exists: only(EXE) }));
  assert.ok(!r.args.includes('-m'), JSON.stringify(r.args));
  assert.strictEqual(r.args[0], '--port');
});

test('python 系一律走 `-m app.server`', () => {
  for (const exists of [only(PY), only(VENV), () => false]) {
    const r = resolveEngine(base({ exists }));
    assert.deepStrictEqual(r.args.slice(0, 2), ['-m', 'app.server'],
                           `${r.via} 少了 -m app.server`);
  }
});

test('engine.exe 优先于项目 .venv', () => {
  const r = resolveEngine(base({ exists: only(EXE, VENV) }));
  assert.strictEqual(r.cmd, EXE);
});

test('.venv 只在开发态兜底（出厂运行时与 engine.exe 都没有时）', () => {
  const r = resolveEngine(base({ exists: only(VENV) }));
  assert.strictEqual(r.cmd, VENV);
  assert.strictEqual(r.via, '.venv');
});

test('全都没有 → PATH 里的 python（此时依赖装没装只能听天由命）', () => {
  const r = resolveEngine(base({ exists: () => false }));
  assert.strictEqual(r.cmd, 'python');
  assert.strictEqual(r.via, 'PATH');
});

test('参数齐全：port / root / data-dir / version（**不含 token**）', () => {
  const { args } = resolveEngine(base({ exists: only(PY) }));
  const at = (k) => args[args.indexOf(k) + 1];
  assert.strictEqual(at('--port'), '12345');
  assert.strictEqual(at('--root'), ROOT);
  assert.strictEqual(at('--data-dir'), 'C:\\userData');
  assert.strictEqual(at('--version'), '9.9.9');
  assert.ok(!args.includes('--token'), `命令行里出现了 --token：${args.join(' ')}`);
  // 整条 argv 里不许出现令牌值本身 —— 拼成一行才是 Windows 上被
  // `wmic process get commandline` 读走的那个东西。
  assert.ok(!args.join(' ').includes('tk'), `argv 泄漏令牌：${args.join(' ')}`);
});

test('令牌只经子进程环境变量交付（缺陷 1：argv 在同机任何进程面前是透明的）', () => {
  for (const over of [{ exists: only(PY) }, { exists: only(EXE) }, { exists: () => false }]) {
    const r = resolveEngine(base(over));
    assert.strictEqual(r.env.TALKSCRIPT_TOKEN, 'tk', `${r.via} 没把令牌放进 env`);
    assert.ok(!r.args.join(' ').includes('tk'), `${r.via} 的 argv 里还有令牌`);
  }
  // 没有令牌时不要凭空造一个空的环境变量（引擎会自己随机生成）
  assert.deepStrictEqual(resolveEngine(base({ exists: only(PY), token: '' })).env, {});
});

test('healthNonce 也走 env（与令牌同一条通道：只有我们启动的那个引擎知道它）', () => {
  const r = resolveEngine(base({ exists: only(PY), healthNonce: 'nz-1' }));
  assert.strictEqual(r.env.TALKSCRIPT_HEALTH_NONCE, 'nz-1');
  // 反向：没传就不该凭空出现 —— 否则壳以为自己在验身份，实际拿空串去比，
  // 任何一个回 200 的本机进程都能通过（正是这条修复要堵的那个口子）。
  assert.strictEqual(
    resolveEngine(base({ exists: only(PY) })).env.TALKSCRIPT_HEALTH_NONCE, undefined);
  // 也不许进 argv：同机任何进程都能读别人的命令行（与令牌同一个理由）。
  assert.ok(!r.args.join(' ').includes('nz-1'), 'argv 泄漏了 healthNonce');
});

test('packsDir 传了才带 --packs-dir（开发态行为必须与改动前完全一致）', () => {
  const withIt = resolveEngine(base({ exists: only(PY), packsDir: 'C:\\userData\\packs' }));
  const i = withIt.args.indexOf('--packs-dir');
  assert.notStrictEqual(i, -1, '打包版没拿到可写包目录，用户包会被升级抹掉');
  assert.strictEqual(withIt.args[i + 1], 'C:\\userData\\packs');
  const without = resolveEngine(base({ exists: only(PY) }));
  assert.ok(!without.args.includes('--packs-dir'), without.args.join(' '));
});

test('非 Windows 走 py/bin/python3，不认 .exe', () => {
  const nixPy = path.join(RES, 'engine', 'py', 'bin', 'python3');
  const r = resolveEngine(base({ platform: 'darwin', exists: only(nixPy) }));
  assert.strictEqual(r.cmd, nixPy);
  assert.strictEqual(r.via.includes('出厂运行时'), true);
});

test('via 字段总是有值（日志要靠它说清从哪条路起的）', () => {
  for (const exists of [only(PY), only(EXE), only(VENV), () => false]) {
    const r = resolveEngine(base({ exists }));
    assert.ok(r.via && r.via.length > 0);
  }
});

test('传了 parentPid → 引擎拿到 --parent-pid（看门狗开关）', () => {
  const r = resolveEngine(base({ exists: only(EXE), parentPid: 4321 }));
  const i = r.args.indexOf('--parent-pid');
  assert.notStrictEqual(i, -1, '没传 --parent-pid，引擎不会自我回收');
  assert.strictEqual(r.args[i + 1], '4321');
});

test('不传 parentPid → 不加该参数（手动起引擎调试时不该被看门狗杀掉）', () => {
  for (const over of [{ exists: only(EXE) }, { exists: only(PY) }]) {
    const r = resolveEngine(base(over));
    assert.ok(!r.args.includes('--parent-pid'),
      `${r.via} 凭空带了 --parent-pid：${r.args.join(' ')}`);
  }
});

test('engine.exe 与 python -m 两条路都带上 --parent-pid', () => {
  const exe = resolveEngine(base({ exists: only(EXE), parentPid: 7 }));
  const py = resolveEngine(base({ exists: only(PY), parentPid: 7 }));
  assert.ok(exe.args.includes('--parent-pid'), 'engine.exe 漏了');
  assert.ok(py.args.includes('--parent-pid'), 'python -m 漏了');
  assert.strictEqual(py.args.indexOf('-m') + 1 >= 0, true);
});
