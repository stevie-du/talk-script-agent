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
  checking: "代码校验中",
  storyboarding: "分镜生成中",
  packing: "行业包生成中",
  fetching: "情报抓取中",
  done: "已完成",
  failed: "失败",
  cancelled: "已停止",
};

/** 文风提示的强度档 —— 机器码 strong / weak 是给 checker 与 pack.yaml 用的，
 *  界面上说「明显 / 轻微」：把 `strong` 印给写稿的人看，等于让他去猜工程术语
 *  （审查第 8 轮 界面 P1）。认不出来的值原样显示，别编一个词盖住它。 */
export const SEVERITY_LABEL = { strong: "明显", weak: "轻微" };
export const severityLabel = s => SEVERITY_LABEL[s] || s || "";

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
