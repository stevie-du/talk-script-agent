// updater-core 纯逻辑测试（Node 内置 test runner，零依赖）。
//
// 跑法：node --test desktop/updater-core.test.js
//
// 为什么要有它：这里钉的三件事都是「读码确认不了、错了又很安静」的规则——
//   1. 没配更新地址 → 一次请求都不发（resolveFeedUrl 返 null 是总闸）；
//   2. 6 小时边界是闭区间（定时器正好踩点时要放行，差 1ms 都不行）；
//   3. 404 要说人话、且原文必须还能进日志（classifyError 的 text/detail 分工）。
// 变异检验（§10）：删掉 resolveFeedUrl 的空值分支 / shouldCheckNow 恒真，
// 这个文件里必须有断言变红——守卫自己要能被红。
'use strict';
const test = require('node:test');
const assert = require('node:assert');
const {
  ENV_UPDATE_URL, STARTUP_DELAY_MS, CHECK_INTERVAL_MS,
  resolveFeedUrl, feedUrlWarning, shouldCheckNow, classifyError, disabledStatus,
  errorStatusText,
} = require('./updater-core');

const SIX_H = 6 * 60 * 60 * 1000;

// ── resolveFeedUrl：没有 / 为空 → null（整体关闭的总闸）────────────

test('环境变量没设（undefined）→ null，更新整体关闭', () => {
  assert.strictEqual(resolveFeedUrl(undefined), null);
});

test('非字符串（null / 数字）一律按没配处理 → null', () => {
  assert.strictEqual(resolveFeedUrl(null), null);
  assert.strictEqual(resolveFeedUrl(42), null);
  assert.strictEqual(resolveFeedUrl({}), null);
});

test('空串 / 纯空白 → null', () => {
  assert.strictEqual(resolveFeedUrl(''), null);
  assert.strictEqual(resolveFeedUrl('   '), null);
  assert.strictEqual(resolveFeedUrl(' \t\n '), null);
});

test('去首尾空白并补尾斜杠', () => {
  assert.strictEqual(resolveFeedUrl('  https://a.test/talkscript  '), 'https://a.test/talkscript/');
  assert.strictEqual(resolveFeedUrl('https://a.test/talkscript'), 'https://a.test/talkscript/');
});

test('已有尾斜杠不重复拼（幂等）', () => {
  assert.strictEqual(resolveFeedUrl('https://a.test/talkscript/'), 'https://a.test/talkscript/');
});

// ── scheme 与明文警示（P2-18）────────────────────────────────

test('非 http/https 的地址一律不启用（file: / ftp: 不是 electron-updater 能用的形态）', () => {
  for (const bad of ['file:///C:/updates/', 'ftp://a.test/talkscript/',
                     '//a.test/talkscript', 'a.test/talkscript']) {
    assert.strictEqual(resolveFeedUrl(bad), null, `${bad} 不该被启用`);
  }
});

test('明文 HTTP 警示：公网 http 要说，https 与本地回环不说', () => {
  const publicHttp = feedUrlWarning('http://updates.example.com/talkscript/');
  assert.ok(publicHttp, '公网 http 地址必须给警示（方案 §8 说 HTTPS 是底线）');
  assert.match(publicHttp, /https/, '警示里要给出该怎么做，不能只说"有风险"');

  // 本地冒烟（README 的示例就是这个）不算「把生产降级成明文」
  for (const local of ['http://127.0.0.1:9000/', 'http://localhost:9000/',
                       'http://[::1]:9000/']) {
    assert.strictEqual(feedUrlWarning(local), null, `${local} 是本地调试，不该警示`);
  }
  // https 与「没配」都不警示
  assert.strictEqual(feedUrlWarning('https://u.example/ts/'), null);
  assert.strictEqual(feedUrlWarning(null), null);
});

// ── shouldCheckNow：6h 边界，闭区间────────────────────────────

test('从未检查过 → 立刻查（每次启动后的第一次走这条）', () => {
  assert.strictEqual(shouldCheckNow(null, 1000), true);
  assert.strictEqual(shouldCheckNow(undefined, 1000), true);
});

test('正好满 6 小时 → 查（边界闭区间，定时器踩点必须放行）', () => {
  assert.strictEqual(shouldCheckNow(1000, 1000 + SIX_H), true);
});

test('差 1ms 满 6 小时 → 不查', () => {
  assert.strictEqual(shouldCheckNow(1000, 1000 + SIX_H - 1), false);
});

test('超过 6 小时 → 查', () => {
  assert.strictEqual(shouldCheckNow(1000, 1000 + SIX_H + 1), true);
});

test('时钟回拨（now < lastCheckAt）→ 不查，不趁机连击', () => {
  assert.strictEqual(shouldCheckNow(1000, 500), false);
});

test('自定义 interval 生效', () => {
  assert.strictEqual(shouldCheckNow(0, 999, 1000), false);
  assert.strictEqual(shouldCheckNow(0, 1000, 1000), true);
});

// ── classifyError：人话 + 原文保留────────────────────────────

test('404 → 明确说「更新地址不存在」，原文进 detail', () => {
  const r = classifyError(new Error('HttpError: 404'));
  assert.match(r.text, /404/);
  assert.match(r.text, /更新地址/);
  assert.strictEqual(r.detail, 'HttpError: 404');
});

test('sha512 不匹配 → 说「已放弃安装」，不裸奔原始英文', () => {
  const r = classifyError(new Error('sha512 checksum mismatch'));
  assert.match(r.text, /校验失败/);
  assert.strictEqual(r.detail, 'sha512 checksum mismatch');
});

test('签名错同样归到校验失败（ERR_UPDATER_INVALID_SIGNATURE）', () => {
  const r = classifyError(new Error('ERR_UPDATER_INVALID_SIGNATURE'));
  assert.match(r.text, /校验失败/);
});

test('404 的报文里带 latest.yml URL 时，仍必须判成 404 而不是解析失败', () => {
  // 顺序回归：这条红过，说明 classifyError 里 latest.yml 分支跑到了 404 前面
  const r = classifyError(new Error('HttpError: 404 for https://a.test/talkscript/latest.yml'));
  assert.match(r.text, /404/);
  assert.ok(!/解析失败/.test(r.text));
});

test('网络类错误（ECONNREFUSED / ENOTFOUND / socket hang up）→ 网络不通', () => {
  for (const msg of ['connect ECONNREFUSED 127.0.0.1:80', 'getaddrinfo ENOTFOUND a.test',
                     'socket hang up', 'net::ERR_CONNECTION_REFUSED']) {
    assert.match(classifyError(new Error(msg)).text, /网络不通/, msg);
  }
});

test('超时（ETIMEDOUT / timeout / timed out）→ 超时，不是网络不通', () => {
  for (const msg of ['connect ETIMEDOUT', 'request timeout', 'The operation timed out']) {
    assert.match(classifyError(new Error(msg)).text, /超时/, msg);
  }
});

test('latest.yml 缺失 / 解析失败 → 说人话', () => {
  for (const msg of ['Cannot find latest in the latest.yml',
                     'Unexpected token in YAML', 'Failed to parse JSON']) {
    assert.match(classifyError(new Error(msg)).text, /latest\.yml/, msg);
  }
});

test('未知错误 → 不装懂，但原文一定带回去', () => {
  const r = classifyError(new Error('boom'));
  assert.match(r.text, /未知/);
  assert.strictEqual(r.detail, 'boom');
});

test('非 Error 输入（字符串）也接得住', () => {
  const r = classifyError('plain string failure');
  assert.strictEqual(r.detail, 'plain string failure');
  assert.match(r.text, /未知/);
});

// ── disabledStatus：三种「没开」说不同的话（§4.3）──────────────

test('dev 态（未打包）→ 「开发模式不检查更新」，与没配地址区分开', () => {
  const r = disabledStatus(false, 'https://a.test/talkscript/', false);
  assert.strictEqual(r.state, 'disabled');
  assert.strictEqual(r.text, '开发模式不检查更新');
});

test('dev 态优先于「没配地址」：两个都没开时先说开发模式', () => {
  assert.strictEqual(disabledStatus(false, null, false).text, '开发模式不检查更新');
});

test('dev 态优先于 portable：dev 跑 portable 模板也先说开发模式', () => {
  assert.strictEqual(disabledStatus(false, 'https://a.test/talkscript/', true).text, '开发模式不检查更新');
});

test('打包态但没配地址 → 「未配置更新地址」', () => {
  const r = disabledStatus(true, null, false);
  assert.strictEqual(r.state, 'disabled');
  assert.strictEqual(r.text, '未配置更新地址');
});

test('打包态且配了地址 → null（链路可用）', () => {
  assert.strictEqual(disabledStatus(true, 'https://a.test/talkscript/', false), null);
});

// ── disabledStatus：portable 闸门（§3.2）─────────────────────

test('portable 态 → 「便携版不支持自动更新」，与 nsis 安装版分开', () => {
  const r = disabledStatus(true, 'https://a.test/talkscript/', true);
  assert.strictEqual(r.state, 'disabled');
  assert.strictEqual(r.text, '便携版不支持自动更新，请手动替换文件');
});

test('portable 优先于「没配地址」：便携版没配地址也说手动替换', () => {
  // 判据：portable 用户的出路是手动换文件，不是去配地址
  assert.strictEqual(disabledStatus(true, null, true).text, '便携版不支持自动更新，请手动替换文件');
});

// ── 常量本体（改这三个数字 = 改产品行为，必须显式红）──────────

test('常量：15s 启动延迟 / 6h 检查间隔 / 环境变量名', () => {
  assert.strictEqual(STARTUP_DELAY_MS, 15 * 1000);
  assert.strictEqual(CHECK_INTERVAL_MS, SIX_H);
  assert.strictEqual(ENV_UPDATE_URL, 'TALKSCRIPT_UPDATE_URL');
});

// ── 失败文案：日志没落成盘必须说出来（P2-43）────────────────
test('失败文案在日志没落盘时把原因带上', () => {
  // 日志正常：不带多余的话
  assert.strictEqual(errorStatusText('连接超时'), '检查更新失败：连接超时');
  // 日志没落成盘：必须说出来 —— 打包版**看不到** console.error，
  // 而 logPath 为空会让界面那行「完整日志：」整个不显示，
  // 用户既拿不到原文、也不知道为什么拿不到。
  const t = errorStatusText('连接超时', '更新日志没落成盘（EACCES）');
  assert.match(t, /连接超时/, t);
  assert.match(t, /更新日志没落成盘/, t);
  // 正对照：空串不算"有原因"（不要把一句空的"；"挂上去）
  assert.strictEqual(errorStatusText('连接超时', ''), '检查更新失败：连接超时');
});
