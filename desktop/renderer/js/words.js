// 话术表：校验结论与作业状态的显示名**只有这里一份**（审查文案线 P0-3 / P1-4）。
//
// ⚠ 本模块不 import 任何东西 —— 它是叶子，任何视图都能拿，不会造成循环。
//
// 修前同一个 `check.passed` 有四套说法：页头胶囊「✓ 合格」、合规页签「✓ 通过」、
// 合规表「✅ 合格」、左栏「已通过校验」；更要紧的是 `passed` 缺失（引擎没给出结论）时，
// 页头与合规页签走的是 `=== true ? 好 : 坏` 的二分 —— 于是**同一块屏幕**上同时出现
// 红色「✗ 未通过」与一句「本次没有校验结论」，用户会照着前者去反复重跑。
// 所以这张表的**行**是三态（通过 / 未通过 / 没有结论），渲染处只能选一行、不能自己二分。
//
// 别在这里加"短档表"：同一件事有两个词，迟早会有一处用错那个词。
// 要收紧就改这里，全界面跟着一起改。

/** 校验结论三态 —— `word` 是上屏的话，`cls` 是配套的配色档（空串 = 中性，不表态）。 */
export const VERDICT = {
  ok: { word: "已通过校验", cls: "ok" },
  no: { word: "未通过校验", cls: "bad" },
  none: { word: "没有校验结论", cls: "" },
};

/** `check.passed` → 表键。缺失**不是**任何一方的结论，是「没测出来」。 */
export function verdictKey(passed) {
  return passed === true ? "ok" : passed === false ? "no" : "none";
}

/** 作业状态显示名。
 *  「已停止」而不是「已取消」：用户按的那颗键写着「停止」，提示与键面不同名时
 *  他会以为这是两个动作（审查文案线 P1-4 六处不统一里的一处）。 */
export const STATE_LABEL = {
  queued: "排队中",
  selecting: "选题策划中",
  writing: "文案撰写中",
  rewriting: "回炉改写中",
  // 以前写「代码校验中」。「代码」是工程侧的叫法 —— 走查的明明是**这篇稿子**
  // （禁用词、字数、时长、占位符），而用户从头到尾没碰过任何代码。同一块屏幕上
  // 结论那三态说的是「已通过校验 / 未通过校验 / 没有校验结论」，进度这里却冒出
  // 一个「代码」，用户会以为界面串到了别的东西上。改成与结论同族的「校验」。
  checking: "校验中",
  storyboarding: "分镜生成中",
  packing: "行业包生成中",
  fetching: "情报抓取中",
  briefing: "选题卡生成中",
  done: "已完成",
  failed: "失败",
  cancelled: "已停止",
};

/** 文风提示的强度档 —— 机器码 strong / weak 是给 checker 与 pack.yaml 用的，
 *  界面上说「明显 / 轻微」：把 `strong` 印给写稿的人看，等于让他去猜工程术语
 *  （审查第 8 轮 界面 P1）。认不出来的值原样显示，别编一个词盖住它。 */
export const SEVERITY_LABEL = { strong: "明显", weak: "轻微" };
export const severityLabel = s => SEVERITY_LABEL[s] || s || "";

/** 「校对」一族的话。同一件事以前有五种说法：键面「标记为已校对」、
 *  提示「已标记为校对完成」、包状态「已校对」、列表副标「草稿 · 待校对」、
 *  结果页「内容需人工校对」。用户据此以为点一次和点两次不一样。
 * 现在：动作、结果、状态各有一个词，且**都由这里出**。 */
export const PROOF = {
  /** 键面 / 确认框标题：这个动作叫什么 */
  act: "标记为已校对",
  /** 做成了：toast 说什么 */
  done: "已标记为校对完成",
  /** 状态：包已经不是草稿了 */
  cleared: "已校对",
  /** 状态：包还是草稿，等着人看 */
  pending: "待校对",
  /** 提醒：这份内容还没人看过 */
  warn: "内容需人工校对",
};

/** 输入框快捷键的说明。同一件事以前有三份说法：placeholder 写
 * 「Enter 发送 · Esc 停止」、title 写「Enter 发送 · Shift+Enter 换行 ·
 *  Esc 停止生成」、发送键 title 写「生成（Ctrl + Enter）」—— 三份都只说了
 *  一半（真正的提交键有两个、Esc 在忙时才是停止），用户按了 Ctrl+Enter
 *  才发现第一份没提。三处现在**都从这里取**。
 * ⚠ index.html 是静态的，那两处由 main.js 起手时回填，别在 HTML 里另写一份。 */
export const SHORTCUT = {
  /** 输入框 placeholder / title 里的那一行 */
  box: "Enter 发送 · Shift+Enter 换行 · Ctrl+Enter 也可发送 · Esc 停止生成",
  /** 发送键的 title */
  send: "生成（Enter / Ctrl+Enter）",
};

/** 会话头部那个标题。修前它有**五份**手写：`index.html` 的 `#rh-title`、
 *  head.js 的初值、head.js 的 setHead 兜底、main.js 的 newChat、
 *  sessions.js 的两处删除后收屏。改一处漏一处，同一块屏幕上就两个名。 */
export const CHAT = {
  title: "新对话",
};

/** 情报源的更新节奏 —— 后端存的是机器码（各包 pack.yaml 里 source 的 cadence 字段，
 *  天数口径在 app/intel.py 的 CADENCE_DAYS），上屏必须过这张表：
 *  `daily` / `on_demand` 直接印在源表里就是内部标识泄露（审查文案线 P1-6）。
 *  认不出来的一律原样显示，别编一个中文词盖住它 —— verify.js 里有一条
 *  「包用过的每个 cadence 码都要在这张表里有词」的对账，漏了会红。
 *  ⚠ 本注释里不许写形如 packs/星号/xxx 的路径：星号紧跟斜杠会提前关掉块注释，
 *  后面的中文就成了语法错误（2026-09-27 实踩过，整页 SyntaxError）。 */
export const CADENCE_LABEL = {
  daily: "每天",
  weekly: "每周",
  monthly: "每月",
  quarterly: "每季度",
  on_demand: "按需",
  manual: "手动",
};
