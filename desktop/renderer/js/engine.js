// engine.js —— 「本地引擎还连得上吗」这一件事的唯一负责人。
//
// 为什么单独一个模块（这个判断原先散在两处，而两处都不是它该在的地方）：
//   · 开机拉 /api/meta 失败 → 把首页 hero 整块换成错误文案。hero 是「还没开始」
//     的引导位，刷新一次就没了，而故障还在。
//   · 之后每次拉会话列表失败 → 只在左栏列表头上插一行橙色小字。那一行是**滚动
//     内容的一部分**（.left-scroll 是 overflow-y:auto），列表一滚就走出视野；
//     侧栏折叠时更是整个消失（#left.folded .left-scroll{display:none}）。
// 引擎连不上是**全局阻断**：生成、选题抓取、保存、导出全在它身上。
// 所以提示钉在两栏都看得见的右栏顶部（#engine-banner），并且给出一条真的走得通的
// 出路 —— 原先那一行只解释、不给出口，而失败之后连一次自动重试都没有：
// 引擎哪怕两秒后活过来，界面也要等用户点到某个会刷新列表的动作才回得来。
//
// ⚠ 判据是 api.js 给的 `err.netFail`（请求根本没到引擎），**不是**「任何一次失败」：
//   引擎回了 5xx 说明它活着、是那一件事办砸了，由发起方就地说明。
//   把一次 503 升成「引擎没了」的全局红横幅，是拿局部错误冒充全局状态。
//
// ⚠ 自动重连的循环只有这一个：sessions.js 拉列表失败时不再自己排轮询，
//   否则两条循环各拉各的，退避节奏互相打乱（而"谁还在试"这件事就没有唯一答案了）。

import { $, toast } from "./util.js";

// 重连退避：起点与上限。同一个数只在这里写一次。
const GAP_START = 3000;
const GAP_MAX = 30000;

let down = false;      // 横幅摊着吗
let wanted = false;    // 还要继续自动重连吗（成功后由 clearEngineDown 关掉）
let gap = GAP_START;
let timer = null;
let retryFn = null;    // 「重新连一次」具体做什么，由 main.js 注册

/** 注册重连动作（main.js 的 handshake：拉 meta + 重画依赖它的那几块 + 拉列表）。
 *  为什么是注入而不是在这里 import：那两个模块都在本模块的下游，
 *  反向引用会绕成循环 —— 装配根负责接线，这里只管"什么时候该再试一次"。 */
export function setEngineRetry(fn) { retryFn = fn; }

export function engineDown() { return down; }

/** 摊出全局横幅并开始自动重连。
 *  @param detail 引擎自己回的那句话，可空（网络级失败时没有这句话，
 *                而「无法连接本地引擎」与主句重复，印出来等于同一句话说两遍）。
 *  幂等：反复报告不会叠出两条横幅，也不会把已经攒起来的退避重置回起点。 */
export function showEngineDown(detail = "") {
  const b = $("engine-banner");
  if (!b) return;
  const why = b.querySelector(".eb-why");
  // 空串要**收掉**上一轮留下的那句：先 5xx（有引擎原话）再断网（没有），
  // 留着旧的那句就是拿一次旧错误去解释一个新状态。
  if (why) why.textContent = detail || "";
  if (!down) {
    down = true;
    b.classList.remove("hidden");
    gap = GAP_START;
  }
  wanted = true;
  schedule();
}

/** 一次成功的握手：收横幅、停重连、退避归零。 */
export function clearEngineDown() {
  if (!down && !wanted && !timer) return;   // 常态：什么都不必做，别每 3 秒摸一遍 DOM
  down = false;
  wanted = false;
  gap = GAP_START;
  if (timer) { clearTimeout(timer); timer = null; }
  const b = $("engine-banner");
  if (b) b.classList.add("hidden");
}

/** 引擎活着但这一次没成（5xx / 超时）：不摊全局横幅，但列表还得有人再试。 */
export function keepTrying() {
  wanted = true;
  schedule();
}

function schedule() {
  if (timer || !retryFn) return;      // 已经排上了 / 没人知道怎么重连
  timer = setTimeout(tick, gap);
}

async function tick(manual = false) {
  timer = null;
  if (!retryFn) { wanted = false; return; }   // 还没接线（boot 之前的点击）：别把 TypeError 念成错误原因
  try {
    await retryFn();
  } catch (e) {
    // 网络级失败那句就是「无法连接本地引擎」—— 别再把它拼到同一句话后面。
    if (manual) toast(e && e.netFail ? "还是连不上本地引擎，稍后会自己再试"
      : `还是连不上本地引擎：${e.message || e}`, 4200);
  }
  // 成功路径里 loadSessions 会调 clearEngineDown()，wanted 已经关了 —— 直接收工。
  // 没关就是这次也没成：按退避再排一次（手动点重试失败同样回到自动循环里，
  // 用户不必守着那颗键连点）。
  if (!wanted) return;
  gap = manual ? GAP_START * 2 : Math.min(gap * 2, GAP_MAX);
  schedule();
}

/** 绑「重试」那颗键。boot() 里调一次（与 bindShell / bindSettings 同一批）。 */
export function bindEngine() {
  const b = $("engine-banner");
  if (!b) return;
  const btn = b.querySelector("#eb-retry");
  if (btn) btn.onclick = () => {
    if (timer) { clearTimeout(timer); timer = null; }
    tick(true);
  };
}
