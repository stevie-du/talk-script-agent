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

test('★ maskSecrets：Authorization 头里的 **非 sk- 形态** Key 也要盖住', () => {
  // 上一条传的是**确切 token**，走的是第一条 `split(token)` 整串替换 ——
  // 格式层那个洞被它掩盖了。而真实场景是上游回显里夹着用户自己的 API Key，
  // 引擎并不知道那个值，只能靠格式。原来的 `authorization\s*[:=]\s*\S+`
  // 里那个 `\S+` 只吃掉 `Bearer` 这个词本身，真正的 key 留在原地等下一条
  // `Bearer` 正则 —— 而那时已经没得可盖了。`sk-` 那条能救 sk- 开头的
  // （智谱/DeepSeek 恰好都是），于是只有非 sk- 形态会漏：
  // 企业网关自签 token、GitHub ghp_、内部 id.secret。
  // 修法：`(?:bearer\s+)?\S+` 连着 Bearer 一起吃。app/llm.py 的 _AUTH_MASK
  // 是同一个病、同一个修法 —— 两侧必须一起改。
  for (const key of ['0123456789abcdef.ABCDEFGHIJKLMNOPQRST',
                     'ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789',
                     'id.secret-value-not-sk']) {
    const out = maskSecrets(`{"Authorization": "Bearer ${key}"}`, '');   // token 传空 = 走格式层
    assert.ok(!out.includes(key), `非 sk- 形态的 Key 泄漏（${key.slice(0, 6)}…）：${out}`);
    assert.match(out, /已脱敏/, out);
    // 脱敏之后头名仍可读：排障要看得见「是哪个头出的问题」
    assert.match(out, /uthorization/, out);
  }
  // 独立 Bearer（没有 authorization: 前缀）同样要盖住
  const bare = 'ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789';
  assert.ok(!maskSecrets(`upstream said Bearer ${bare} here`, '').includes(bare));
  // 大小写与引号变体（上游 JSON 回显里都出现过）
  for (const c of [`authorization='Bearer ${bare}'`, `AUTHORIZATION: bearer ${bare}`]) {
    const out = maskSecrets(c, '');
    assert.ok(!out.includes(bare), `变体没盖住：${c} → ${out}`);
  }
});

test('脱敏不吃掉正文：没有凭证的行原样通过', () => {
  const plain = '模型接口返回 401：令牌已过期，请在设置里更换';
  assert.strictEqual(maskSecrets(plain, TOKEN), plain);
});

// ★ 更新源地址的**查询串**整段盖住（审查 P2 可维护性）。
// 为什么单列一条：README 自己警告过 `?sig=` / `?key=` 这类鉴权参数
// 「按形态写掩码盖不住」，而 electron-updater 的 debug 行会把完整的
// feed URL 打进 updater.log —— 也就是它**一定**会明文落盘，而日志是
// 用户要贴进 issue 的。盖的是查询串，主机与路径要留着（排查就靠它）。
test('★ maskSecrets：URL 的查询串整段脱敏（更新源鉴权参数，掩码按形态盖不住）', () => {
  const url = 'https://example.com/feed/latest.yml?sig=SECRETSIG&key=SECRETKEY';
  const out = maskSecrets(`[upd-debug] checkForUpdates url=${url}`, '');
  assert.ok(!out.includes('SECRETSIG'), `sig 没盖住：${out}`);
  assert.ok(!out.includes('SECRETKEY'), `key 没盖住：${out}`);
  // 反向：主机与路径必须还在 —— 全抹掉就等于把排查线索也扔了
  assert.ok(out.includes('example.com'), `主机被抹掉了：${out}`);
  assert.ok(out.includes('/feed/latest.yml'), `路径被抹掉了：${out}`);
  // ⚠ 反向之二：**参数名**也得留着。这一条不是"顺手加的"：
  //   第一次写这条断言时只钉了"密钥没了"，于是两种改坏都测不出来 ——
  //   ① 整段压成 `?[已脱敏]` ② 连参数名一起扔（输出成 `…ymlundefined=[已脱敏]`）。
  //   两者都"没泄密"，可用户拿到日志既看不出是哪个参数、也看不出地址形状，
  //   排查线索全没了 —— 与本仓"静默降级比报错更危险"同一条纪律。
  //   `?token=…` 那条既有规则正是靠 `token=[已脱敏]` 这个形状起作用的
  //   （加载失败对话框的断言钉着它），所以这里也必须钉住。
  assert.ok(/[?&]sig=/.test(out), `sig 这个参数名被抹掉了：${out}`);
  assert.ok(/[?&]key=/.test(out), `key 这个参数名被抹掉了：${out}`);
  // 没有查询串的 URL 不该被动过
  const bare = 'https://example.com/feed/latest.yml';
  assert.ok(maskSecrets(bare, '').includes(bare), '没查询串的地址被改了');
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
  const i = src.indexOf('did-fail-load');
  assert.ok(i > 0, '没找到 did-fail-load 这一段，本断言会空转');
  const block = src.slice(i, i + 900);
  assert.ok(/failLoadDetail\(/.test(block),
    'did-fail-load 没走 failLoadDetail：' + block.slice(0, 300));
  // 裸 `${url}` 就是把带令牌的整条地址塞进对话框的那一行写法
  assert.ok(!/\$\{\s*url\s*\}/.test(block), '对话框里又出现了裸 ${url}：' + block.slice(0, 300));
  assert.ok(!/showErrorBox\([^)]*\$\{/.test(block),
    'showErrorBox 又直接在模板串里拼参数了：' + block.slice(0, 300));

  // ⚠ 上面那段是**局部**扫描，漏过一次：restartEngine 里也有一处 loadURL，
  //   它的 rejection 走 `String(e)` 裸插值 —— 而那个错必然含整条
  //   `http://127.0.0.1:PORT/?token=…`。它离 did-fail-load 十万八千里，
  //   局部窗口盖不到，于是「所有对话框都经 engine-dialogs」这个不变量
  //   在测试全绿的情况下是假的（P0）。所以这里再钉一条**全文件**的：
  //   每一个 loadURL 调用都必须被 try 包住，且同作用域内出现 failLoadDetail。
  const loadUrls = [...src.matchAll(/loadURL\(/g)].map((x) => x.index);
  assert.ok(loadUrls.length >= 2,
    `只找到 ${loadUrls.length} 处 loadURL：引擎重启那条是不是被改没了？（本断言需要它来覆盖）`);
  for (const at of loadUrls) {
    // 窗口要够宽：有的出口是 `await loadURL()` + try/catch（failLoadDetail 在
    // catch 里），有的是 `loadURL().catch(...)`（经 reportLoadFailure 转发）。
    // 只看前后 600 字符会把后一种的转发函数切掉，于是误报。
    const around = src.slice(Math.max(0, at - 900), at + 900);
    const ok = /failLoadDetail\(/.test(around) || /reportLoadFailure\(/.test(around);
    assert.ok(ok,
      '有一处 loadURL 附近既没有 failLoadDetail 也没有 reportLoadFailure —— '
      + `它的 rejection 会把带令牌的整条 URL 弹进系统对话框：\n${around.slice(500, 1200)}`);
  }
  // restartEngine 这条路不许再出现 `showErrorBox('引擎重启失败', String(e))`
  const rIdx = src.indexOf('async function restartEngine');
  assert.ok(rIdx > 0, '没找到 restartEngine，本断言会空转');
  const rBlock = src.slice(rIdx, rIdx + 2200);
  assert.ok(!/showErrorBox\('引擎重启失败',\s*String\(e\)\)/.test(rBlock),
    'restartEngine 又把 loadURL 的错裸插进对话框了（P0：令牌泄漏）');
  assert.ok(/failLoadDetail\(/.test(rBlock),
    'restartEngine 的界面加载失败没走 failLoadDetail');
});
