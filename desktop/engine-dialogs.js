// 主进程要**弹给用户看**的那些对话框文案。
//
// 为什么单独一个文件（与 engine-path.js 同一个理由）
// --------------------------------------------------
// 这段逻辑与 Electron 无关，唯一的外部输入是「令牌值」——把它注进来就能单测。
// 而它守的是一条**安全**不变量：一次性访问令牌不许出现在对话框里。
// 留在 main.js 里就只能靠读码确认，而读码正好没确认住（P2-5 那次漏的就是这里：
// main.js 里 stdout / stderr / argv 三处都过了 maskSecrets，
// 唯独 did-fail-load 把整条 `engineUrl()` 原样插进了 showErrorBox）。
//
// 为什么这件事要紧
// ----------------
// `http://127.0.0.1:PORT/?token=<令牌>` 里的那个令牌就是 `/api/*` 的**唯一凭证**：
// 拿到它的本机进程可以读走全部生成记录、改模型 base_url，
// 于是下一次生成把用户的 API Key 与行业包 private/ 资料一起发到那个地址。
// 系统对话框是能**截图、能全选复制、能贴进 issue 或聊天窗口**的那一类出口，
// 而「界面加载失败」恰恰是用户最会去截图求助的那一种。
// 地址本身要留着（端口是排查线索），令牌不留。
'use strict';

/** 把凭证从一段文本里抹掉。
 * @param s      任意文本（日志行、对话框正文、命令行列）
 * @param token  当前这次的引擎令牌；空值时只做**格式**层面的脱敏
 *                （没有确切值可比对，就靠下面那几条正则）
 *
 * 顺序有讲究：先按**确切令牌值**整串替换（最可靠，且不受分隔符写法影响），
 * 再按格式兜底 —— 万一有人把令牌又拼回命令行或 URL 的另一种写法里。
 */
function maskSecrets(s, token) {
  let out = String(s == null ? '' : s);
  if (token) {
    out = out.split(token).join('[已脱敏]');     // 先按确切值替换，最可靠
  }
  return out
    .replace(/(--token[ =])\S+/gi, '$1[已脱敏]')
    .replace(/(token=)[^\s&"'%]+/gi, '$1[已脱敏]')
    .replace(/(x-talkscript-token\s*["']?\s*[:=]\s*)\S+/gi, '$1[已脱敏]')
    .replace(/(authorization\s*["']?\s*[:=]\s*)\S+/gi, '$1[已脱敏]')
    .replace(/(Bearer\s+)[^\s"',]+/gi, '$1[已脱敏]')
    .replace(/(sk-[A-Za-z0-9_-]{4})[A-Za-z0-9_-]+/g, '$1…[已脱敏]');
}

/** 「界面加载失败」这张对话框的正文。
 * @param o.desc     Chromium 的失败原因，如 ERR_CONNECTION_REFUSED
 * @param o.code     失败码
 * @param o.url      **带令牌的**整条地址（webContents 给的就是 engineUrl()）
 * @param o.logPath  引擎日志文件路径（可为空）
 * @param o.token    当前令牌 —— 只用来把它从 url 里精确地抹掉
 */
function failLoadDetail(o) {
  return `无法从本地引擎加载界面。\n${maskSecrets(o.desc, o.token)}（${o.code}）\n`
    + `${maskSecrets(o.url, o.token)}\n\n`
    + '请确认引擎正在运行；若反复出现，请重启应用。'
    + '\n\n引擎日志：' + (o.logPath || '（日志文件不可用）');
}

module.exports = { maskSecrets, failLoadDetail };
