// 对话框文案与凭证脱敏（Node 内置 test runner）。跑法：node --test desktop/
//
// 为什么值得单独钉（P2-5）：main.js 里有三条把文本往外送的路 ——
//   · 引擎 stdout / stderr → 日志文件   ——过 maskSecrets
//   · spawn 的 argv        → console + 日志 ——过 maskSecrets
//   · **对话框**        ——原来独独这一条没过
// 而 `did-fail-load` 的 `url` 正是 `http://127.0.0.1:PORT/?token=<一次性令牌>`：
// 一次加载失败就把 /api/* 的唯一凭证打进一个能截图、能全选复制、
// 能贴进 issue 的系统对话框。这个令牌值钱在它能让本机任何进程
// 改模型 base_url，下一次生成就把用户的 API Key 连同 private/ 资料发过去。
'use strict';
const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const { maskSecrets, failLoadDetail } = require('./engine-dialogs');

const TOKEN = 'Zm9vYmFyMTIzNDU2Nzg5MDEyMzQ1Njc4OQ';      // 32 字符 base64url 形状
const PORT = 8931;
const ENGINE_URL = `http://127.0.0.1:${PORT}/?token=${encodeURIComponent(TOKEN)}`;

/** 一切「会被用户看到 / 复制走」的字符串都不许含当前令牌 —— 判据按确切值。 */
function assertNoToken(s, label) {
  assert.strictEqual(typeof s, 'string', `${label} 不是字符串`);
  assert.ok(!s.includes(TOKEN), `${label} 里出现了明文令牌：${s}`);
  // `token=` 后面必须紧跟脱敏标记（正则里那个 [ 就是「[已脱敏]」的开头）。
  // 只查"还有没有 token="会把已经修好的输出也判成红 —— 那是断言在量自己。
  assert.ok(!/[?&]token=(?!\[)/.test(s), `${label} 里还有一个没被脱敏的 token= 参数：${s}`);
}

test('加载失败对话框：整条地址留着（端口是排查线索），令牌不留', () => {
  const detail = failLoadDetail({
    desc: 'ERR_CONNECTION_REFUSED', code: -102, url: ENGINE_URL,
    logPath: 'C:/Users/x/AppData/Roaming/TalkScript/logs/engine.log',
    token: TOKEN,
  });
  assertNoToken(detail, '对话框正文');
  assert.ok(detail.includes(`127.0.0.1:${PORT}`), '端口被一起抹掉了，排查时无从下手：' + detail);
  assert.match(detail, /token=\[已脱敏\]/, detail);
  assert.match(detail, /ERR_CONNECTION_REFUSED/, detail);
  assert.match(detail, /-102/, detail);
  assert.match(detail, /engine\.log/, detail);
});

test('令牌为空 / 日志文件不可用时对话框仍然出得来（不冒 undefined）', () => {
  const d1 = failLoadDetail({ desc: 'ERR_FAILED', code: -1, url: 'http://127.0.0.1:9/', token: '' });
  assertNoToken(d1, '无令牌时的正文');
  assert.match(d1, /日志文件不可用/, d1);
  assert.ok(!/undefined|null/.test(d1), '对话框里吐出了 undefined：' + d1);
});

test('maskSecrets：每一种凭证写法都要被盖住（令牌 / --token / 头 / Bearer / sk-）', () => {
  const cases = [
    `加载自 ${ENGINE_URL} 失败`,
    `python -m app.server --port 8931 --token ${TOKEN}`,
    `--token=${TOKEN}`,
    `X-TalkScript-Token: ${TOKEN}`,
    `headers = {"x-talkscript-token": "${TOKEN}"}`,
    `Authorization: Bearer ${TOKEN}`,
    `上游返回 {"error":"invalid api key sk-abcdefghij0123456789"}`,
    // JSON 转义过的形态（异常堆栈里最常见）
    `{"url":"http://127.0.0.1:8931/?token\\u003d${TOKEN}"}`,
  ];
  for (const c of cases) {
    const out = maskSecrets(c, TOKEN);
    assert.ok(!out.includes(TOKEN), `按确切值没盖住：${c} → ${out}`);
    assert.match(out, /已脱敏/, `没有脱敏痕迹：${c} → ${out}`);
  }
  // sk- 这类第三方 Key 与令牌无关，按**格式**也要盖住（token 传空也一样）
  const keyOnly = maskSecrets('api_key=sk-zzzzzzzzzzzzzzzzzzzz', '');
  assert.ok(!/sk-zzzzzzzzzzzzzzzzzzzz/.test(keyOnly), keyOnly);
  // 不传 token 时那条 `token=` 的正则兜底必须还在（这才是"没有确切值可比"时的唯一防线）
  assert.match(maskSecrets(ENGINE_URL, ''), /token=\[已脱敏\]/);
});

test('脱敏不吃掉正文：没有凭证的行原样通过', () => {
  const plain = '模型接口返回 401：令牌已过期，请在设置里更换';
  assert.strictEqual(maskSecrets(plain, TOKEN), plain);
});

// ── main.js 接线：对话框必须走这一个出口 ─────────────────────
// 上面几条钉的是「函数会不会漏」，这一条钉的是「还有没有人绕过它」。
// P2-5 的病灶正是本地有一份 maskSecrets、而对话框那条通道没用它。
test('main.js 的对话框全部经由 engine-dialogs（不许再出现裸插值的 url）', () => {
  const src = fs.readFileSync(path.join(__dirname, 'main.js'), 'utf8');
  assert.ok(/require\('\.\/engine-dialogs'\)/.test(src),
    'main.js 不再从 engine-dialogs 取脱敏 —— 对话框可能又走回本地实现');
  assert.ok(!/function maskSecrets\s*\(s\)\s*\{[\s\S]{0,40}let out/.test(src),
    'main.js 里又本地抄了一份脱敏实现：两处迟早走散');
  const i = src.indexOf("did-fail-load");
  assert.ok(i > 0, '没找到 did-fail-load 这一段，本断言会空转');
  const block = src.slice(i, i + 900);
  assert.ok(/failLoadDetail\(/.test(block),
    'did-fail-load 没走 failLoadDetail：' + block.slice(0, 300));
  // 裸 `${url}` 就是把带令牌的整条地址塞进对话框的那一行写法
  assert.ok(!/\$\{\s*url\s*\}/.test(block), '对话框里又出现了裸 ${url}：' + block.slice(0, 300));
  assert.ok(!/showErrorBox\([^)]*\$\{/.test(block),
    'showErrorBox 又直接在模板串里拼参数了：' + block.slice(0, 300));
});
