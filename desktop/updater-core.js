// 自动更新的纯逻辑层。
//
// 为什么单独一个文件
// ------------------
// 与 engine-path.js 同一个理由：这段规则**与 Electron 无关**（唯一的运行时输入是
// 一个环境变量字符串和两个时间戳），放进 main.js 就只能靠读码确认——
// 「没配地址就一次请求都不发」「6 小时边界」「404 要说人话」都是本项目铁律
// （docs/自动更新方案.md §3.3 / §6 / §10），拆出来 `node --test` 直接跑断言，
// 不用起 Electron、不用连服务器。
//
// 本文件只做「决策与文案」，不碰网络、不碰 electron-updater——那些在 main.js 的
// initUpdater() 里，并且只消费这里返回的结论。

'use strict';

// 更新地址的**唯一**来源（方案 §6）：运行期只认这个环境变量。
// package.json 里 build.publish.url 只是构建期生成 latest.yml 的占位，客户端不读它——
// 否则「占位地址」也可能被真的请求出去。将来要正式分发再加解析顺序，那是「一次
// 改动 + 一条测试」，不是重构。
const ENV_UPDATE_URL = 'TALKSCRIPT_UPDATE_URL';

// 启动后延迟 15s 才第一次检查：不跟引擎启动、页面加载抢那几百毫秒，
// 用户的观感是「应用起来了」，更新检查在后台自己跑（方案 §4.1）。
const STARTUP_DELAY_MS = 15 * 1000;

// 之后每 6 小时一次。是定时器不是轮询——两次检查之间零请求（方案 §1 / §4.1）。
const CHECK_INTERVAL_MS = 6 * 60 * 60 * 1000;

/**
 * 把环境变量规范化成更新地址；「没有 / 为空」一律返回 null。
 *
 * null 的语义是**整体关闭**：调用方拿到 null 就不该建 updater、不该发任何请求
 * （方案 §6「没配就一次请求都不发」，§10 有对应的零请求判据）。
 *
 * ⚠ **只认 http/https**（P2-18）：`file://` / `ftp://` 之类不是 electron-updater
 * 能用的形态，早退（= 不启用）比让它在深处报一个看不懂的错好。
 * ⚠ `http://` **不拒** —— README 的本地冒烟示例就是 `http://127.0.0.1:9000/`，
 * 拒掉等于把那条路堵死。但它是明文，由 `feedUrlWarning()` 摊到界面上
 * （方案 §8 / §12.1 说「HTTPS 是底线」，而代码层面对降级到 HTTP 原来**完全沉默**）。
 *
 * @param {unknown} raw 环境变量原值（可能是 undefined / 非字符串，都按没配处理）
 * @returns {string|null} 规范化后的地址（必以 '/' 结尾），或 null
 */
function resolveFeedUrl(raw) {
  if (typeof raw !== 'string') return null;
  const trimmed = raw.trim();
  if (trimmed === '') return null;
  if (!/^https?:\/\//i.test(trimmed)) return null;
  // generic provider 会把地址当 baseURL 拼 latest.yml：尾斜杠由这里补，
  // 不让每个调用方各自记得拼。
  return trimmed.endsWith('/') ? trimmed : trimmed + '/';
}

/**
 * 更新地址的**明文警示**（返回 null = 没问题）。
 *
 * 方案 §8 / §12.1 把 HTTPS 写成底线，但代码层面原来对 `http://` **完全沉默**：
 * 用户（很可能照着 README 的本地冒烟示例）把 http 地址带进生产，自动更新就在
 * 明文上跑 —— 安装包与 sha512 都可能被途中替换 —— 而界面上一个字都不说。
 * 不拒它的理由见 `resolveFeedUrl`；回环地址是本地冒烟，不算"生产降级成明文"。
 *
 * @param {string|null} feedUrl resolveFeedUrl 的返回值
 * @returns {string|null} 给人看的一句话，或 null
 */
function feedUrlWarning(feedUrl) {
  if (!feedUrl) return null;
  if (/^https:\/\//i.test(feedUrl)) return null;
  if (/^http:\/\/(127\.0\.0\.1|localhost|\[::1\])(:\d+)?\//i.test(feedUrl)) return null;
  return '更新地址是明文 HTTP：安装包与校验值都可能在途中被替换，建议换成 https'
    + '（本地调试用的 127.0.0.1 / localhost 不受此提醒）';
}

/**
 * 现在是否该做一次检查。
 *
 * @param {number|null|undefined} lastCheckAt 上次检查时间（Date.now() 制）；null = 从来没检查过
 * @param {number} now 当前时间（Date.now() 制）
 * @param {number} [intervalMs] 默认 CHECK_INTERVAL_MS
 * @returns {boolean} 距上次检查满一个 interval（或从未检查）才 true；边界取闭（>=）
 *
 * 边界闭区间是刻意的：6h 定时器每次触发都问它，正好等于 6h 时应当放行，
 * 差 1ms 都不放——放到 main.js 里就是一个 if (Date.now() - last >= SIX_HOURS)，
 * 但那个 >= 写在哪儿都行、就不会有人为它写一条边界断言。
 */
function shouldCheckNow(lastCheckAt, now, intervalMs) {
  const interval = typeof intervalMs === 'number' ? intervalMs : CHECK_INTERVAL_MS;
  if (typeof lastCheckAt !== 'number') return true;   // 从未检查（含每次启动后的第一次）
  if (!(now - lastCheckAt >= interval)) return false; // 含时钟回拨（差为负）→ 不查
  return true;
}

/**
 * 把 electron-updater / Node HTTP 栈抛出来的错翻成人话。
 *
 * 为什么必须翻：设置页状态行是用户唯一能看到「为什么没更新」的地方（方案 §3.3）。
 * 一句 "HttpError: 404" 对用户不是信息；但**原文必须保留**——排障要看真实错误，
 * 所以返回值带 detail，由调用方原样写进 updater.log。
 *
 * @param {unknown} err Error 或任意值
 * @returns {{text: string, detail: string}} text 给人看，detail 给日志
 */
function classifyError(err) {
  const detail = err && err.message ? String(err.message) : String(err);
  const m = detail.toLowerCase();
  // 顺序即优先级：404 的报文里往往带着 ".../latest.yml" 的 URL，
  // 所以 404 必须判在 latest.yml 之前，否则会被误译成「解析失败」。
  if (/sha512|checksum|signature/.test(m)) {
    return { text: '安装包校验失败（sha512 不匹配），已放弃安装；下次检查将重新下载', detail };
  }
  if (/404/.test(m)) {
    return { text: '更新地址不存在（HTTP 404）：请检查 TALKSCRIPT_UPDATE_URL 指向的目录里是否有 latest.yml', detail };
  }
  if (/etimedout|esockettimedout|timeout|timed out/.test(m)) {
    return { text: '连接更新服务器超时', detail };
  }
  if (/enotfound|econnrefused|econnreset|econnaborted|eai_again|socket hang up|network|err_connection|err_internet|err_name/.test(m)) {
    return { text: '网络不通：暂时连不上更新服务器', detail };
  }
  if (/latest\.yml|\.yml|yaml|parse|json/.test(m)) {
    return { text: '更新信息文件（latest.yml）缺失或解析失败', detail };
  }
  return { text: '检查更新失败（未知原因）', detail };
}

/**
 * 「没开」的几种情形，各自说不同的话（方案 §4.3 的三态表 + §3.2 的 portable 闸门）。
 *
 * dev 态与「没配地址」必须说不同的话：dev 态是 electron-updater 内置行为
 * （isUpdaterActive() 早退，零请求），告诉用户「未配置更新地址」是指错了方向；
 * 反之打包态没配地址时说「开发模式」也是错的。portable 同理——官方 auto-updatable
 * targets 只列 NSIS，便携版用户该看到的是「去手动替换文件」。
 *
 * @param {boolean} isPackaged app.isPackaged
 * @param {string|null} feedUrl resolveFeedUrl 的返回值
 * @param {boolean} [isPortable] PORTABLE_EXECUTABLE_DIR 在不在
 *                                （electron-builder portable 安装模板必设，模板级铁证）
 * @returns {{state: 'disabled', text: string}|null} null = 更新链路可用
 */
function disabledStatus(isPackaged, feedUrl, isPortable) {
  if (!isPackaged) return { state: 'disabled', text: '开发模式不检查更新' };
  if (isPortable) return { state: 'disabled', text: '便携版不支持自动更新，请手动替换文件' };
  if (!feedUrl) return { state: 'disabled', text: '未配置更新地址' };
  return null;
}

/**
 * 更新失败时的状态行文案。
 *
 * ⚠ `logError` 非空时**必须带出来**（P2-43）：打包版看不到 `console.error`，
 *   而 `logPath` 为空会让界面那行「完整日志：」整个不显示 —— 用户既拿不到
 *   原文，也不知道为什么拿不到（现象是"一句失败、没有下文"）。
 *
 * @param {string} reason 失败原因（人话，来自 classifyError）
 * @param {string} [logError] 更新日志没落成盘的原因（没有就空串）
 * @returns {string}
 */
function errorStatusText(reason, logError) {
  return `检查更新失败：${reason}` + (logError ? `；${logError}` : '');
}

module.exports = {
  ENV_UPDATE_URL,
  STARTUP_DELAY_MS,
  CHECK_INTERVAL_MS,
  resolveFeedUrl,
  feedUrlWarning,
  shouldCheckNow,
  classifyError,
  disabledStatus,
  errorStatusText,
};
