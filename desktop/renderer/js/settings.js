// 设置：整窗二级页（生成偏好 / 行业包 / 新建行业包 / 模型接口 / 知识库 / 技能）。
//
// 修复前的一个交互坑：每次打开设置都无条件 preloadSettings()，会把用户
// 「刚填好但还没保存」的 base_url / model 覆盖回服务端的旧值 ——
// 顺序是「打开设置 → 改 URL → Ctrl+, 关掉 → Ctrl+, 再开 → 输入没了」。
// 现在按字段记 dirty，只有没被改过的输入框才回填。

import { $, el, esc, toast, bindOnce } from "./util.js";
import { api } from "./api.js";
import { state, emit } from "./store.js";
import { closeOverlays, openOverlay, appConfirm } from "./overlays.js";
// ui.js 与 settings.js 互相引用，但引用的都是**函数声明**（会被提升），
// 所以循环依赖在调用时已解析完毕，安全。
import { fillPackSelect } from "./ui.js";

const PANES = ["gen", "packinfo", "packgen", "llm"];
// 2026-09-17：「知识 / 技能」两个独立面板已并入「行业包」面板，
// PANES 去掉 "kb" / "skills"。openPackInfo 是行业包面板右列的渲染函数，
// 进 packinfo 时自动加载；panegen 的 [新建] 不影响。
// 导航项 → 资源；面板 → 资源下可能有的「子页面」。
// packgen 不在左导航里（动作不是资源），但它需要让「行业包」导航高亮，
// 读作「你在行业包这个资源下，进了它的子动作」。
// map: pane → 应高亮的 nav item data-pane（默认就是自身）
const NAV_OF_PANE = { packgen: "packinfo" };
// packgen 的来源面板：gen 的 [新建] 按钮 vs packinfo headbar 的 [新建]，
// 决定了「返回」按钮回到哪里。设成模块状态是因为 packgen 同一会话内
// 可能从两个入口先后进，记录最后一次的来源。
let packgenFrom = "gen";
// 正在跑的建包作业 id（P1-43）。作业在服务端跑，所以：切面板、关设置页再回来，
// 进度都还在；「取消」是一等动作而不是丢弃返回值；引擎重启后作业随内存释放，
// 轮询会拿到 404 —— 那时按「没建成」处理，让人重来一遍（不会留下半个包）。
let packgenJob = null;
// 取消已发出、作业还没落到 cancelled 的那 ≤0.9 秒。按钮文案靠它，
// 不能只靠一次性 textContent 赋值 —— 秒表每 1s 会重写一次按钮（实测覆盖）。
let packgenCanceling = false;
const dirty = new Set();
// 最近一次 /api/config 的结果。模型列表、弹窗回填、「恢复默认」都读它 ——
// 每次要一个字段就现发一次请求的话，弹窗里的默认值可能与列表不是同一时刻的。
let lastCfg = null;

// 数值项的合法区间。**必须与后端 `app/server.py` 的 `NUMERIC_BOUNDS` 逐项相等** ——
// 前后端没法共享代码，一致性由 `tests/test_numeric_bounds_consistency.py`
// 同时读这两个文件比对，别只改一处。
//
// 为什么要专门钉：这两处曾经不一致 —— 前端卡 0 ~ 1.5、后端卡 0.0 ~ 2.0，
// 用户填 1.8 会被**前端**拒掉而后端完全接受。前端比后端更严是更坏的一种不一致：
// 用户看到「界面说不行」，绕不过去，也想不到是界面在凭想象设限。
export const NUMERIC_BOUNDS = {
  temperature: [0, 2],
  retries: [0, 10],
  timeout: [5, 1800],
  max_tokens: [256, 200000],
};

// 区间 → 输入框的对应关系。校验与 min/max 都从这一张表来，
// 避免「能填的范围」和「能存的范围」变成两套数（temperature 曾经是这样）。
// 2026-09-17：采样温度 (temperature) 从高级配置的**UI**移除（任务 4）；
// 同一天「输出预算（max_tokens）」也移除（用户原话「高级设置去掉 token 限制吧」）。
// 两项都**只删 UI、保留 NUMERIC_BOUNDS 区间** —— 前后端 NUMERIC_BOUNDS 集合
// 一致是 pytest 守卫的不变量（不一致就报红），且后端仍需这两个值：
// temperature 用于重试时微调（pipeline.py 的 +0.25），max_tokens 是模型输出上限。
// 它们属于「模型行为」而非「连接稳健性」，所以不再让用户调。
const NUM_FIELDS = [
  ["retries", "st-retries", "重试次数"],
  ["timeout", "st-timeout", "单次超时"],
];

// 把区间写到输入框的 min/max 上。HTML 里那对属性只是**初始值**，
// 以这里为准 —— 否则改一处忘了另一处，浏览器照样能填出越界值。
function applyNumericBounds() {
  for (const [key, id] of NUM_FIELDS) {
    const n = $(id);
    if (!n) continue;
    const [lo, hi] = NUMERIC_BOUNDS[key];
    n.min = lo;
    n.max = hi;
  }
}

export function settingsOpen() {
  return !$("settings-screen").classList.contains("hidden");
}

export function openSettings(pane) {
  closeOverlays();          // 确认浮层 z 高于设置页，不关会一直糊在整窗上
  $("settings-screen").classList.remove("hidden");
  setPane(PANES.includes(pane) ? pane : state.settingsPane);
  preloadSettings().catch(() => {});
  return Promise.resolve();
}

export function closeSettings() {
  if (settingsOpen()) $("settings-screen").classList.add("hidden");
  // P3-49：结果页直接关设置（没点「完成」）时，主界面行业包下拉要能看到新包。
  // 只刷新、**不切包**：用户没点「完成」，没有「去用新包」的意图，当前包保持不变。
  // 切包是 onPackDone 的职责（fillPackSelect({prefer})）—— 这里若也 prefer，
  // 会把用户正在用的包悄悄换掉（verify.js 实测过这一干扰）。
  if (state.lastCreatedPack) {
    state.lastCreatedPack = null;
    api.meta().then(m => {
      state.meta = m;
      emit("meta", m);          // on('meta') 的 fillPackSelect() 无参调用保留当前选中
    }).catch(() => {});
  }
}

export function setPane(pane) {
  if (!PANES.includes(pane)) pane = state.settingsPane;
  PANES.forEach(p => $(("pane-" + p))?.classList.toggle("hidden", p !== pane));
  // 导航高亮：packgen 这种子动作让「资源」项高亮（NAV_OF_PANE 映射）。
  // 单一 `.on` 仍是断言守的「高亮唯一」，packgen 走到 packinfo，
  // 不会出现「两个 .on」或「没有 .on」。
  const navPane = NAV_OF_PANE[pane] || pane;
  document.querySelectorAll(".stg-nav-item").forEach(n => {
    n.classList.toggle("on", n.dataset.pane === navPane);
  });
  // 切面板要归零的滚动容器是 .stg-main，不是 .stg-pane。
  // 后者在 styles.css 里已不设 overflow（限宽居中的内容盒子），
  // 滚动容器上提是为了让滚动条贴页面右边 —— 归零必须跟着上提一级，
  // 否则从长面板切走再切回，会停在上次的位置。
  const stgMain = $("settings-screen")?.querySelector(".stg-main");
  if (stgMain) stgMain.scrollTop = 0;
  state.settingsPane = pane;
  emit("pane", pane);
  // 行业包详情每次进入都重拉：包可能被切换过，文件清单与草稿角标也可能变了
  if (pane === "packinfo") {
    renderPackList();
    openPackInfo().catch(e => toast("读取失败：" + e.message, 3500));
  } else if (pane === "llm") {
    // 进入模型面板：右列默认停在「当前启用」那条 —— 空着的话第一眼看到的是
    // 一张空表单，会以为还没配模型。
    // `addingNew` 归零：重新进面板要回到「空态 / 停在当前启用那条」，
    // 不能停在上次点到一半的「添加」表单上。
    addingNew = false;
    if (!editingId) {
      const act = ((lastCfg || {}).models || []).find(x => x.active);
      selectModel(act ? act.id : "");
    }
  }
  return pane;
}

async function preloadSettings() {
  const c = await api.config();
  lastCfg = c;
  const dflt = new Set(c.llm_defaulted || []);
  fill("st-retries", c.retries ?? "");
  fill("st-timeout", c.timeout ?? "");
  // 「这一项还是内置默认」必须**逐项**标在框上，不能只在底部写一句总提示：
  // 用户的视线落在「超时 = 180」这个框上，结论就是「已经配好了」，
  // 底部那行浅灰小字他根本不会看。
  // 2026-09-17：模型行不再渲染「内置默认」标（任务 5 —— 移除默认模型）。
  // 高级配置这两项仍标：它们是「兜底值」，告诉用户「这个值不是你自己存的」
  // 仍有用（点「保存高级配置」就能变成自己的）。
  markDefault("st-retries", dflt.has("retries"), "retries");
  markDefault("st-timeout", dflt.has("timeout"), "timeout");
  renderAdvSub(dflt);
  renderModelList(c);
  renderLlmEmpty(c);        // 空列表 → 右列只显示空态（隐藏表单 + 高级配置）
  renderConfigError(c.config_error);
  renderStatus(c);
}

/** 高级配置收起来时，摘要行上要能看出「里面还有几项是内置默认」。
 *  否则收起来就什么都看不见了 —— 而「没配过」正是最需要被看见的那个状态。 */
function renderAdvSub(dflt) {
  const n = $("st-adv-sub");
  if (!n) return;
  const hit = NUM_FIELDS.filter(([key]) => dflt.has(key)).length;
  n.textContent = hit ? `重试 / 超时 · ${hit} 项还是内置默认` : "";
}

function renderStatus(c) {
  const env = c.env_override ? "（当前由环境变量 TALKSCRIPT_API_KEY 覆盖）" : "";
  const n = (c.models || []).length;
  // 2026-09-17：一条模型都没有 → 状态行**留空**。空态块已经在说这件事了，
  // 再写一句「当前启用的模型还没配 API Key」是**错的**：根本没有启用的模型。
  // 同理 active_model 为空（用户把开关全关了）→ 说清是「都没有启用」，
  // 而不是「没配 Key」—— 那是两种不同的缺。
  $("st-status").textContent = c.mock
    ? `当前为 mock 模式（返回夹具，不调模型）${env}`
    : !n ? ""
      : !c.active_model ? `有 ${n} 个模型，但都没有启用${env}`
        : c.api_key_set
          ? `已配置 Key · 当前启用 ${c.model}（共 ${n} 个模型）· 重试 ${c.retries} 次`
            + ` / 超时 ${c.timeout}s${env}`
          : `当前启用的模型还没配 API Key${env}`;
}

/** 一条模型都没有时，右列只显示**空态引导** —— 隐藏「添加模型」表单与高级配置。
 *
 *  用户原话：「没有模型的时候不应该显示添加啊，还有高级配置」。
 *  空表单会让人以为"已经在配了"；高级配置（重试 / 超时）是给**已配好的模型**
 *  调优用的，没有模型时它没有对象。空态只给一件事：去哪加第一个模型。
 *
 *  ⚠ `addingNew` 是「空列表下用户主动点了『添加模型』」—— 此时要临时显示表单，
 *  否则点了按钮什么都不会发生。它由 headbar / 空态的两个入口置 true，
 *  由保存成功、取消、重新进入面板置 false。 */
function renderLlmEmpty(c) {
  const empty = !((c || {}).models || []).length;
  const showEmpty = empty && !addingNew;
  $("llm-empty").classList.toggle("hidden", !showEmpty);
  $("md-form-card").classList.toggle("hidden", showEmpty);
  // 高级配置：**空列表时始终隐藏**（进了添加表单也不显示）——
  // 还没有任何模型，重试 / 超时没有对象可调。
  $("st-adv").classList.toggle("hidden", empty);
  // 三列骨架在「一条模型都没有」时是空转的：中列只剩一句「还没有模型。」，
  // 右列那张卡被推到它右边，看着像浮在左上角。收掉中列，让空态（以及第一条
  // 的添加表单）在内容区居中。判据是**有没有模型**而不是 showEmpty —— 否则
  // 点「添加模型」会让中列又冒出来，表单在两种状态间横向跳一格。
  $("pane-llm").classList.toggle("no-models", empty);
}

// ── 模型列表 ────────────────────────────────────────────────
//
// 改造前这里是三个平铺字段（Base URL / API Key / 模型名）：想同时配智谱和
// DeepSeek 是做不到的 —— 加第二个必须把第一个覆盖掉，而且切换要重填一遍 Key。
// 现在每条模型自带连接信息，列表里增删改切换；生成参数（温度/重试/超时/预算）
// 与「连到哪家」无关，仍是全局一份，收进「高级配置」。

/** 服务商名**从请求地址推**。这是一次猜测 —— 猜错会把「这家」说成「那家」，
 *  所以未命中已知表时原样显示主机名，不硬套一个名字；完整地址写在 title 上，
 *  用户一眼能核对。表只做「常见几家的别名归一」，不做穷举。 */
const PROVIDERS = [
  [/bigmodel\.cn|zhipu/i, "智谱"],
  [/deepseek/i, "DeepSeek"],
  [/dashscope|aliyuncs/i, "阿里云百炼"],
  [/volces|volcengine/i, "火山方舟"],
  [/moonshot/i, "月之暗面"],
  [/siliconflow/i, "硅基流动"],
  [/minimax/i, "MiniMax"],
  [/hunyuan|tencent/i, "腾讯混元"],
  [/openai\.com/i, "OpenAI"],
];

export function providerOf(baseUrl) {
  const host = (String(baseUrl || "").match(/^https?:\/\/([^/]+)/i) || [])[1] || "";
  if (!host) return "—";
  for (const [re, name] of PROVIDERS) if (re.test(host)) return name;
  return host;
}

// 列表缩略图标统一为「28×28 圆角块 + 16×16 描边 SVG」，与生成偏好左侧
// 三个导航项同一规格（fill=none / stroke=currentColor / stroke-width=1.8）。
// 图标按语义给：行业包行 = 打包盒，模型行 = 芯片；不渲染文字首字 ——
// 取首字既要有信息量又不能撞车（IP 首字符是 "1"、外部模型名首字符是
// 服务商前缀），取错了就像坏字。
function svgIcon(paths) {
  return `<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${paths}</svg>`;
}

// 打包盒：与生成偏好左侧「行业包」导航项同一组路径
const ICON_PACK = svgIcon(
  '<path d="M21 8l-9-5-9 5 9 5 9-5z"/><path d="M3 8v8l9 5 9-5V8"/><path d="M12 13v8"/>');
// 芯片：四条引脚的 CPU，表「模型 / 算力」
const ICON_MODEL = svgIcon(
  '<rect x="6" y="6" width="12" height="12" rx="2"/>'
  + '<path d="M12 2v4M12 18v4M2 12h4M18 12h4"/>');

const FIELD_LABEL = { base_url: "请求地址", model: "模型 ID" };

/**
 * 渲染中列的模型条目列表（三列骨架的「第二层级」）。
 * 原来这里渲染的是 `<table class="mdl-table">` 的行；改成 `.pl-item` 条目后
 * 每条仍是「名称 + 模型ID·服务商 + 标记 + 启用开关」，只是从表格列变成条目行。
 * ⚠ 保留了 mdl-label / mdl-sub / mdl-switch / tag-default / tag-warn 这些类名，
 * 断言口径得以延续（只把 `#st-model-rows tr` 换成 `#llm-list .pl-item`）。
 */
function renderModelList(c) {
  const list = $("llm-list");
  if (!list) return;
  const models = c.models || [];
  list.innerHTML = "";
  // 空列表时中列**不留任何东西**：`renderLlmEmpty()` 会把整列收掉（`.no-models`），
  // 原来那句「还没有模型。」因此永远不可见 —— 而空态卡已经说了同一件事。
  // 写一段只有 display:none 才出现的文案，是在给断言造一个查得到却看不见的靶子。
  if (!models.length) return;
  for (const m of models) list.appendChild(modelItem(m, models.length, c));
  // 列表是整块重建的，重建后要把「右列正在编辑那条」的 .sel 补回去 ——
  // 否则一保存/一刷新，中列就再也看不出选中了谁。
  if (editingId) {
    const cur = list.querySelector(`.pl-item[data-id="${CSS.escape(editingId)}"]`);
    if (cur) cur.classList.add("sel");
  }
}

function modelItem(m, total, c) {
  const prov = providerOf(m.base_url);
  // ⚠ 这一行是 `<div role=button>` 而**不是** `<button>`：行里嵌着启用开关
  // （下面那个 `button.mdl-switch`），而 `<button>` 里不许再放可交互元素 ——
  // 嵌套按钮既是非法 HTML，也是键盘陷阱（Tab 停在里层，外层永远聚焦不到，
  // 而 Enter 会同时触发两层）。行业包分组那一行（renderPackGroups）没有内嵌
  // 控件，仍然是原生 `<button>`，这是有意的差别，不是漏改。
  const item = el("div", "pl-item");
  item.setAttribute("role", "button");
  item.tabIndex = 0;
  item.dataset.id = m.id;
  // 当前启用的那条要有视觉落点：不然开关看着都一样
  if (m.active) item.classList.add("on");

  // 图标：芯片标记（与生成偏好左侧导航同一套 SVG 规格）；服务商与地址
  // 在副标题上，不以文字首字做图标。
  item.appendChild(el("span", "pl-ic", ICON_MODEL));

  const txt = el("span", "pl-txt");
  txt.appendChild(el("span", "pl-t mdl-label", esc(m.label || m.model || m.id)));
  const sub = el("span", "pl-s mdl-sub", esc(`${m.model || "—"} · ${prov}`));
  sub.title = m.base_url || "";
  txt.appendChild(sub);
  // 2026-09-17（任务 5）：模型行不再渲染「内置默认」小标 ——「默认模型」概念
  // 整个下线（"移除默认模型，改为需用户自行配置"）。连 DOM 节点也不保留：
  // 之前 `display:none` 隐藏但断言查 DOM 的方案是"假象"，未配置与已配置
  // 在底层还是两种状态。彻底删除后断言口径改成「DOM 上不应有 tag-default 」。
  // 用户在编辑表单里看到的提示是「字段留空 + placeholder 给示例 + Key 两态」。
  // 没 Key 要说出来：启用它生成必被拒，而列表上一切正常。
  // 环境变量给了 Key 时不说 —— 那时它其实能用，报「未配置」就是误报。
  if (!m.api_key_set && !c.env_override) {
    const t = el("span", "tag-warn", "未配置 Key");
    t.title = "这条模型没有 Key，启用它生成会被拒绝";
    txt.appendChild(t);
  }
  item.appendChild(txt);

  // 右侧操作区：启用开关（点它不该顺带切换右列选中，所以要 stopPropagation）
  const act = el("span", "pl-act");
  const sw = el("button", "mdl-switch" + (m.active ? " on" : ""));
  sw.type = "button";
  sw.setAttribute("role", "switch");
  sw.setAttribute("aria-checked", m.active ? "true" : "false");
  sw.title = m.active ? "当前启用的模型（同一时间只有一个）"
    : "启用这个模型（同一时间只有一个）";
  sw.onclick = (e) => { e.stopPropagation(); activateModel(m.id); };
  act.appendChild(sw);
  item.appendChild(act);

// 点条目 = 选中，右列加载它的编辑表单（原来是「编辑」按钮开弹窗）
  item.onclick = () => selectModel(m.id);
  // 原生 <button> 免费给的键盘激活，div 要自己补：Enter 与 Space 等同点击。
  // Space 必须 preventDefault，否则页面先滚一行再触发（键盘用户能立刻感觉到）。
  item.onkeydown = e => {
    if (e.target !== item) return;               // 里层开关自己处理自己的按键
    if (e.key === "Enter" || e.key === " " || e.key === "Spacebar") {
      e.preventDefault();
      selectModel(m.id);
    }
  };
  return item;
}

async function activateModel(id) {
  // 点**当前已启用**的那条 = **关掉它**（2026-09-17）。
  // 原来这里直接 `return`（注释写「已经是当前，别白写一次文件」）——
  // 于是开关**关不掉**：点当前启用的模型没有任何反应，看起来像坏了
  //（用户报「模型开启时无法关闭」）。「启用」是单选语义，但「都不启用」
  // 也是合法状态（想先停用、改完配置再启用）。
  // 关掉之后生成会被后端的 `_require_model` 拦住并说清「当前没有启用任何模型」。
  const target = (lastCfg && lastCfg.active_model === id) ? "" : id;
  try {
    await api.activateModel(target);
    await refreshAll();
    toast(target ? "已切换模型" : "已停用当前模型（生成前需要重新启用）");
  } catch (e) { toast("切换失败：" + e.message, 3500); }
}

async function removeModel(m) {
  const ok = await appConfirm("删除模型",
    `确认删除「${m.label || m.model}」？\n它的请求地址与 Key 会一起删掉，不可恢复。`);
  if (!ok) return;
  try {
    await api.deleteModel(m.id);
    await refreshAll();
    toast("已删除");
  } catch (e) { toast("删除失败：" + e.message, 3500); }
}

async function testModel(m) {
  const name = m.label || m.model;
  $("st-status").textContent = `正在测试「${name}」…`;
  try {
    // 带 model_id：编辑一条**非当前**模型时，不带 id 会错拿当前那条的 Key 去测 ——
    // 测出来的结果与用户以为的不是一回事，而界面会照常显示「连接正常」。
    const r = await api.testConfig({ model_id: m.id });
    $("st-status").textContent = r.ok
      ? `「${name}」连接正常 · ${r.model}${r.detail ? " · " + r.detail : ""}`
      : `「${name}」连接失败：${r.detail}`;
    toast(r.ok ? "连接正常" : "连接失败，请看下方提示", r.ok ? 2000 : 4000);
  } catch (e) {
    $("st-status").textContent = "测试失败：" + e.message;
    toast("测试失败：" + e.message, 4000);
  }
}

// ── 添加 / 编辑模型弹窗 ─────────────────────────────────────

let editingId = "";
// 空列表下用户主动点了「添加模型」→ 临时显示表单（否则点了没反应）。
// 见 renderLlmEmpty 的说明。
let addingNew = false;

/**
 * 选中一个模型：右列加载它的编辑表单（**不再开弹窗**）。
 * 原来是 openModelDialog(id) + openOverlay("model-dialog")；三列化之后
 * 编辑就在右列进行，弹窗已从 DOM 移除（留着会有两套编辑 UI）。
 *
 * ⚠ 条目上 `.on` 表示「当前启用」（业务状态，沿用原表格 tr.on 的口径），
 *   而「当前在右列编辑」是另一回事，用 `.sel` —— 两个状态混在一个 class 上
 *   会出现「启用的那条永远高亮、切到别的条目看不出选中了谁」。
 */
function selectModel(id) {
  editingId = id || "";
  const models = (lastCfg || {}).models || [];
  const m = models.find(x => x.id === id) || null;
  const dfl = new Set(m ? (m.defaulted || []) : []);
  $("md-id").value = editingId;
  $("md-title").textContent = m ? "编辑模型" : "添加模型";
  $("md-sub").textContent = m
    ? "改完点保存即生效；API Key 留空表示不改动已存的那把。"
    // 不写「左列」：一条模型都没有时中列是收起来的（见 renderLlmEmpty），
    // 指着一个不在 screen 上的方位，用户只会回头找。
    : "填好保存后，随时可以在模型列表里启用它。";
  // ⚠ 回填的是**文件里存着的值**，不是生效值。
  // 一条没配过地址的模型，生效值里那个地址是内置默认兜出来的 —— 填进框里
  // 就变成了「你填的」，用户没动过手却看到一串地址，而且保存一次它就真的成了
  // 他的配置。所以 defaulted 里的字段一律留空，靠 placeholder + 小标说明。
  $("md-model").value = m && !dfl.has("model") ? m.model : "";
  $("md-baseurl").value = m && !dfl.has("base_url") ? m.base_url : "";
  $("md-name").value = m ? m.name || "" : "";
  const ak = $("md-apikey");
  ak.value = "";
  if (!ak.dataset.phSet) ak.dataset.phSet = ak.placeholder;
  ak.placeholder = (m && m.api_key_set) ? ak.dataset.phSet : ak.dataset.phEmpty;
  // 2026-09-17（任务 5）：模型行不再渲染「内置默认」小标 —— 「默认模型」概念
  // 整个下线，前端不再为 model / base_url 调 markDefault。
  // DOM 与 data-field 也不再存在（HTML 里 .tag-default 已删，verify.js 改口径）。
  // 未配置状态靠 placeholder + Key 输入框两态 + 列表行「未配置 Key」warn 一起说。
  $("md-status").textContent = "";
  // 「删除」在编辑既有模型时**始终可用**（2026-09-17）。
  // 原来还有 `models.length <= 1` 就隐藏的限制 —— 那是「总有一条内置默认」时代的
  // 规则（删空了生成时取不到连接信息，而界面还会显示「已配置 Key」）。
  // 现在模型是用户自己加的、不再有预置条目，删光就是「还没配」：
  // 列表显示空引导，生成前被后端的 `_require_model` 明确拦住并说清原因。
  // 用户原话：「没有内置默认的模型的，需要用户自己添加，添加完还需要支持删除」。
  const del = $("md-delete");
  if (del) del.classList.toggle("hidden", !m);
  // 中列选中态（.sel，与「启用」的 .on 分开）
  document.querySelectorAll("#llm-list .pl-item").forEach(n => {
    n.classList.toggle("sel", n.dataset.id === id);
  });
}

/** 取消编辑：回到「选中当前启用那条」的状态（不再有弹窗可关）。 */
function closeModelDialog() {
  addingNew = false;
  const act = ((lastCfg || {}).models || []).find(x => x.active);
  selectModel(act ? act.id : "");
  // 空列表下取消「添加」→ 回到空态（否则表单空着、也没有模型）
  renderLlmEmpty(lastCfg || {});
}

async function saveModelDialog() {
  const model = $("md-model").value.trim();
  if (!model) { toast("模型 ID 不能为空"); return; }
  // 新增时请求地址必填（2026-09-17）：不再有「内置默认地址」可以兜，
  // 留空会被后端拒。前端先拦一道 —— 让错误当场可见，不必等一次往返。
  // 编辑时留空仍是「保持不变」（那个框本来就是空的）。
  if (!editingId && !$("md-baseurl").value.trim()) {
    toast("请填写请求地址，例如 https://api.deepseek.com/v1", 4000);
    return;
  }
  const body = {
    id: editingId,
    name: $("md-name").value.trim(),
    model,
    base_url: $("md-baseurl").value.trim(),
  };
  const key = $("md-apikey").value.trim();
  if (key) body.api_key = key;
  try {
    const out = await api.saveModel(body);
    // 高级配置**并入同一次保存**（2026-09-17）：用户报「模型面板有两个保存」——
    // 原来高级配置折叠区底部有一个独立的「保存高级配置」，与这里的「保存」
    // 同屏并列，让人不知道该点哪个。现在一屏一个保存：点它同时存
    // 这条模型 + 全局的重试 / 超时。
    // ⚠ 顺序：先存模型（它有校验，失败要能拦住），再存高级配置。
    await saveAdvancedConfig();
    // 保存后要**停在刚保存的那条**上，而不是跳回「当前启用」那条 ——
    // 编辑一条非启用模型时跳走，用户会以为没保存上。
    const savedId = (out && out.id) || editingId;
    addingNew = false;       // 保存成功 → 离开「添加」态（列表已有模型了）
    await refreshAll();
    selectModel(savedId);
    toast(`已保存模型「${model}」`);
    return out;
  } catch (e) {
    toast("保存失败：" + e.message, 4000);
  }
}

/** 保存高级配置（重试次数 / 单次超时）。
 *
 *  2026-09-17：不再有独立的「保存高级配置」按钮 —— 由模型表单的「保存」
 *  一并调用。**一屏一个保存**是各家 Agent 设置页的通行做法，
 *  同屏两个确认按钮是更差的设计（用户原话「模型面板有两个保存」）。
 *
 *  ⚠ 越界值要**抛异常**让调用方统一 toast —— 前端不许比后端更严，也不许更松：
 *  越界当场拒掉、请求根本不发（后端那份校验永远不会被触发才叫更严）。 */
async function saveAdvancedConfig() {
  const body = {};
  for (const [key, id, label] of NUM_FIELDS) {
    const raw = $(id).value.trim();
    if (raw === "") continue;
    const n = Number(raw);
    const [lo, hi] = NUMERIC_BOUNDS[key];
    if (!Number.isFinite(n) || n < lo || n > hi) {
      throw new Error(`${label}需在 ${lo} ~ ${hi} 之间`);
    }
    body[key] = n;
  }
  if (Object.keys(body).length) await api.saveConfig(body);
  dirty.clear();
}

async function testModelDialog() {
  const btn = $("md-test");
  const label = btn.textContent;
  btn.disabled = true;
  btn.textContent = "测试中…";
  $("md-status").textContent = "正在连接…";
  try {
    // 带上界面当前值（Key 留空时后端沿用那条模型已存的），未保存也能测。
    // model_id 指明测的是**哪一条** —— 编辑非当前模型时不然会错拿当前那条的 Key。
    const r = await api.testConfig({
      model_id: editingId,
      base_url: $("md-baseurl").value.trim(),
      api_key: $("md-apikey").value,
      model: $("md-model").value.trim(),
    });
    $("md-status").textContent = r.ok
      ? `连接正常 · ${r.model}${r.detail ? " · " + r.detail : ""}`
      : `连接失败：${r.detail}`;
    toast(r.ok ? "连接正常" : "连接失败，请看提示", r.ok ? 2000 : 4000);
  } catch (e) {
    $("md-status").textContent = e.message;
    toast("测试失败：" + e.message, 4000);
  }
  btn.disabled = false;
  btn.textContent = label;
}

async function refreshAll() {
  await preloadSettings().catch(() => {});
  try {
    state.meta = await api.meta();
    emit("meta", state.meta);
  } catch (_) { /* 忽略：设置本身已经保存成功 */ }
}

/** 在字段标签上打一个「内置默认」小标。幂等 —— preloadSettings 每次打开都会跑，
 *  不能叠出第二个。`field` 写进 data-field，让「标了哪些字段」可被断言核对。
 *
 *  2026-09-17：模型行不再调它（任务 5 —— 移除默认模型，改为需用户自行配置）。
 *  高级配置里仍打这个标的只有「重试次数 / 单次超时」：这些是「兜底值」，
 *  不是「默认模型」，告诉用户「这个值不是你自己存的」仍是有用的
 *  （点模型表单的「保存」会连同这两项一起存 —— 「保存高级配置」那颗按钮 2026-09-17 已删，
 *  一屏两个保存按钮会让人不知道该点哪个）。
 *  采样温度与输出预算（max_tokens）同日按用户要求移出设置页，界面上改不到，
 *  只能改数据目录的 config.yaml 或环境变量；后端仍校验区间、也仍在使用。
 *  fillDefault() 同步删除：模型上的「恢复默认」按钮已下线（任务 6）。 */
function markDefault(inputId, on, field) {
  const input = $(inputId);
  const lbl = input && input.closest(".block-inner")?.querySelector(".lbl");
  if (!lbl) return;
  const old = lbl.querySelector(".tag-default");
  if (!on) { old?.remove(); return; }
  if (old) { if (field) old.dataset.field = field; return; }
  const tag = el("span", "tag-default", "内置默认");
  if (field) tag.dataset.field = field;
  tag.title = "这个值来自内置默认，不是你保存过的配置";
  // 插在「恢复默认」按钮前面：标签 → 状态 → 操作，读起来是一条线
  const btn = lbl.querySelector(".linkbtn");
  if (btn) lbl.insertBefore(tag, btn); else lbl.appendChild(tag);
}

// config.yaml 读坏时的提示。
//
// 为什么必须显示：读坏之后上面那些输入框填的全是**内置默认值**，
// 而下面那行状态会照常说「已配置 Key · 模型 glm-4.7」——
// 用户以为配置还在，其实自己填的 base_url 一次都没生效过。
// 提示里要写清两件事：①上面显示的不是你的配置；②怎么恢复（重新保存一次）。
function renderConfigError(msg) {
  const n = $("st-cfg-err");
  if (!n) return;
  if (!msg) { n.classList.add("hidden"); n.textContent = ""; return; }

  n.textContent = `⚠ ${msg}。上面显示的是内置默认值，不是你保存过的配置 ——`
    + `重新填一次并点「保存」即可覆盖修复。`;
  n.classList.remove("hidden");
}

function fill(id, value) {
  if (dirty.has(id)) return;      // 用户改过就不覆盖
  const n = $(id);
  if (n) n.value = value;
}

export const bindSettings = bindOnce(function bindSettings() {
  applyNumericBounds();          // 区间由 JS 统一写入输入框的 min/max
  ["st-retries", "st-timeout"].forEach(id => {
    $(id).addEventListener("input", () => dirty.add(id));
  });
  // 模型弹窗里的四个框也算「用户改过」—— 弹窗是每次打开重建内容的，
  // 不记 dirty 的话 preloadSettings 的 fill() 会把它冲掉。
  ["md-model", "md-name", "md-baseurl", "md-apikey"].forEach(id => {
    $(id).addEventListener("input", () => dirty.add(id));
  });
  document.querySelectorAll(".stg-nav-item").forEach(n => {
    n.onclick = () => setPane(n.dataset.pane);
  });

  // 生成偏好：中列「分组条目」→ 右列显示对应分组的表单。
  // 生成偏好是纯表单（没有天然列表），所以按 pack / param / adv 三块拆开，
  // 中列点哪块右列显示哪块，跟知识库/技能「点文件看内容」是同一套骨架。
  // 默认停在第一块（行业包）—— 它是「生成什么」的前提，也是新用户第一个要选的。
  const genSecBtns = document.querySelectorAll("#gen-sec-list .pl-item");
  const genSecCards = document.querySelectorAll("#gen-sec-detail .page-card[data-sec]");
  const showGenSec = (sec) => {
    genSecBtns.forEach(b => b.classList.toggle("on", b.dataset.sec === sec));
    genSecCards.forEach(c => c.classList.toggle("hidden", c.dataset.sec !== sec));
  };
  genSecBtns.forEach(b => { b.onclick = () => showGenSec(b.dataset.sec); });
  showGenSec("pack");

  $("btn-close-settings").onclick = closeSettings;
  $("btn-packinfo").onclick = () => setPane("packinfo");
  // packgen 的两个入口：gen 的 [新建]（btn-newpack）和 packinfo headbar 的 [新建]（pi-newpack）。
  // 区别在于「取消」回哪里 —— 这就是 packgenFrom 的存在意义。
  $("btn-newpack").onclick = () => {
    packgenFrom = "gen";
    $("pg-form").classList.remove("hidden");
    $("pg-result").classList.add("hidden");
    setPane("packgen");
  };
  $("pi-newpack").onclick = () => {
    packgenFrom = "packinfo";
    $("pg-form").classList.remove("hidden");
    $("pg-result").classList.add("hidden");
    setPane("packgen");
  };
  $("pi-refresh").onclick = () => {
    renderPackList();
    openPackInfo().catch(e => toast("刷新失败：" + e.message, 3500));
  };
  // 生成中这一枚就是「取消生成」（P1-43）：取消检查点在模型返回之后、写盘之前，
  // 所以通常不会留下目录。**边界**：取消来得太晚（写盘已完成）时包会完整落盘、
  // 作业状态记 cancelled（见 pipeline 的 _run_packgen 二次检查）—— 那时它就在 packs/ 里，
  // 下次建同名会 409，不是"什么都没发生"。
  // 修复前它在生成期间被禁用 —— 因为那是个同步长请求，中途退出后结果会悄悄
  // 落进隐藏面板（P3-48）；现在取消有真实语义，禁用它的理由也随之消失。
  $("pg-close").onclick = () => {
    if (!packgenJob) { setPane(packgenFrom); return; }
    // 乐观反馈：作业要到下一拍轮询（≤0.9s）才落到 cancelled，
    // 按钮一直写着「生成中 · Ns…」会让人以为没点上而再点一次。
    packgenCanceling = true;
    $("pg-run").textContent = "取消中…";
    api.cancel(packgenJob).catch(e => toast("取消失败：" + e.message));
  };
  $("pg-run").onclick = runPackgen;
  // 结果页双出口：返回回来源面板（与表单页「取消」一致），完成去工作台用新包。
  $("pg-back").onclick = () => setPane(packgenFrom);
  $("pg-done").onclick = onPackDone;

  // 「保存高级配置」按钮已删（2026-09-17）：高级配置改由模型表单的「保存」
  // 一并存掉（见 saveAdvancedConfig），不再有独立的 st-save。
  // 「恢复默认」（数值项）：空保存是无效操作，后端过滤空串防手滑，所以回到
  // 默认必须是一个显式动作。base_url / model 的「恢复默认」已删（2026-09-17）：
  // 任务 5/6 ——「默认模型」整个下线，模型行不再有「恢复默认」按钮。
  $("st-reset-adv").onclick = () => resetField(
    ["retries", "timeout"], ["st-retries", "st-timeout"]);
  // headbar 的「刷新」：与行业包面板同构（2026-09-17）。
  // 重跑一次 preloadSettings 就够 —— 它重拉 /api/config 并重建列表与表单。
  $("llm-refresh").onclick = () => {
    preloadSettings().catch(e => toast("刷新失败：" + e.message, 3500));
  };
  // 「添加模型」：右列切到空表单（editingId=""），不再是开弹窗。
  // ⚠ 全页**只有这一个**添加入口（2026-09-17）：空态里原来也有一颗，
  // 与 headbar 这颗重复（用户问「没有配置的时候有两个添加模型入口」）。
  // 现在空态只负责说明，入口统一在这里 —— 但 `addingNew` 仍需要：
  // 空列表时右列显示的是空态，点了这颗要能把表单换出来。
  $("st-add-model").onclick = () => {
    addingNew = true;
    selectModel("");
    renderLlmEmpty(lastCfg || {});
  };
  $("md-cancel").onclick = closeModelDialog;
  $("md-save").onclick = saveModelDialog;
  $("md-test").onclick = testModelDialog;
  // 「删除」移到右列（原来在表格的操作列）：删完回到当前启用那条。
  $("md-delete").onclick = async () => {
    const m = ((lastCfg || {}).models || []).find(x => x.id === editingId);
    if (m) { await removeModel(m); closeModelDialog(); }
  };
  // 「导出为 Agent 技能」与「返回生成偏好」已删（2026-09-17）：
  //   ① 导出把 pack 目录复制成 SKILL.md，是个「用一次就忘」的动作，
  //      占着右列底部最显眼的位置不值当；
  //   ② 「返回生成偏好」是错的方向 —— 用户从哪个面板进来就该回哪去，
  //      而左导航一直在，点一下就走了，不需要面板底部再放一个出口。
  $("pi-undraft").onclick = undraftPack;
});

async function resetField(field, inputId) {
  const fields = [].concat(field);
  const ids = [].concat(inputId);
  try {
    await api.resetConfig(fields);
    // 清掉 dirty 标记，否则 preloadSettings 会拒绝回填输入框
    ids.forEach(id => dirty.delete(id));
    await refreshAll();
    toast("已恢复默认值");
  } catch (e) {
    toast("恢复失败：" + e.message, 3500);
  }
}

// ── 行业包面板：把「知识库 / 技能」两个旧面板并回一个入口 ────────
//
// 2026-09-17 用户贴图报：「行业包 / 知识库 / 技能」三个独立面板把同一份
// 数据切三份；知识/技能面板里那个「行业包」下拉又是空的。改为统一入口：
//   - 进「行业包」面板 → 左栏选一个行业
//   - 右栏直接看到**该行业全部文件按角色分组**（技能 / 规则 / 方法库 /
//     合规 / 知识库 / 禁用词表 / 草稿校对 / 私有资料 / 包清单）
//   - 点任一文件 → 同一个 #pi-file-view 查看器
//
// 数据模型本来就只有「一个行业 = packs/<slug>/ 一个目录」，现在 UI 也对应
// 上来了 —— 行业包面板成为行业内容的**唯一入口**，#pane-kb / #pane-skills
// 已从 DOM 与左导航里删除（见 index.html）。
//
// 「新增资料」按钮也一起删了：浏览器里改盘上的文件既没有原子写也没校验，
// 风险与收益不成比例 —— Claude Code / Cursor / Skills 等成熟智能体同样
// 不在 UI 里直接编辑。改文件 = 打开编辑器 + Ctrl+S + 回来点「刷新」即可。

// ── 设置内搜索已移除 ──────────────────────────────────────
// 曾经的 filterSettingsNav() 按「导航项文字 + 分区面板全文」过滤左导航，
// 匹配不到就把整组连同标题一起收掉。删掉的理由：设置一共 6 个分区，
// 一屏就能看全，检索框的收益抵不上它在「返回工作区」下方多占的一行，
// 而且它是**唯一**会隐藏导航项的入口 —— 一旦过滤词留在框里（比如上次
// 输的「超时」），用户回到设置页会看到一份缺项却没有任何提示的导航，
// 像个 bug。现在导航恒定完整，所见即所得。
// 连带清理：.stg-search 的 HTML / CSS，以及只为它服务的
// `.stg-nav-item.hidden` / `.nav-sec.hidden` 两条规则。

// 分组卡片网格渲染：按 FILE_ROLE 分组，每组一个 .kb-group，
// 每张卡片 = 图标 + 文件名 + 角色描述 + 大小（点击切换右侧查看器）。
// 之前的实现是单层 flat 列表（行 295 是 `row = el("button", "kb-item", ...)`），
// 13 个文件一屏挤下来要找特定文件得在一堆路径里翻。
// 「分组 → 卡片」是 Zcode 的范式，对应"已安装 / 浏览器插件 / 文档技能"那种结构。
//
// 兼容性：每张卡仍是 `.kb-item`（断言 kb.rows === 3 还过着），
// 文件名放进 `.kb-name`（断言里 `n.firstChild.textContent` 改成 `.kb-name` 即可）。
const ROLE_GROUP_ORDER = [
  // 顺序就是分组从上到下出现的顺序；
  // 与"重要性"对齐：技能 → 规则 → 方法库 → 合规 → 知识库 → 禁用词 → 草稿 → 私有 → 包清单
  { key: "skill",      label: "技能（怎么写）" },
  { key: "rules",      label: "规则" },
  { key: "patterns",   label: "方法库" },
  { key: "compliance", label: "合规" },
  { key: "knowledge",  label: "知识库" },
  { key: "banwords",   label: "禁用词表" },
  { key: "checklist",  label: "草稿校对" },
  { key: "private",    label: "私有资料" },
  { key: "package",    label: "包清单" },
];

// FILE_ROLE 返回两类东西：CSS 用的 role key（稳定字符串）+ 用户看的中文标签。
// 一个文件 → 一个 key；key 与 ROLE_GROUP_ORDER 对应。
function fileRole(rel) {
  if (rel === "pack.yaml")     return "package";
  if (rel === "skill.yaml")    return "skill";
  if (rel === "banwords.yaml") return "banwords";
  if (rel === "校对清单.md")   return "checklist";
  if (rel.startsWith("knowledge/"))  return "knowledge";
  if (rel.startsWith("compliance/")) return "compliance";
  if (rel.startsWith("patterns/"))   return "patterns";
  if (rel.startsWith("rules/"))      return "rules";
  if (rel.startsWith("private/"))    return "private";
  return "package";  // 兜底：未识别的归"包清单"组，颜色中性
}
const ROLE_LABEL = Object.fromEntries(ROLE_GROUP_ORDER.map(g => [g.key, g.label]));

// 每个 role 的图标 SVG。统一 24×24 viewBox，stroke 用 currentColor
// （颜色由 CSS .kb-item[data-role=...] .kb-icon 的 color 接管，不写死）。
// 必须放在 renderPackGroups 之前：函数体里用到 ROLE_ICON[key]。
const ROLE_ICON = {
  skill: `<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 3l2.2 4.5 5 .7-3.6 3.5.9 4.9L12 14.3 7.5 16.6l.9-4.9L4.8 8.2l5-.7z"/></svg>`,
  rules: `<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 6h16M4 12h16M4 18h10"/></svg>`,
  patterns: `<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="6" cy="6" r="2"/><circle cx="18" cy="6" r="2"/><circle cx="12" cy="18" r="2"/><path d="M6 8v3a3 3 0 0 0 3 3h6a3 3 0 0 0 3-3V8"/></svg>`,
  compliance: `<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 3l8 4v6a8 8 0 0 1-8 8 8 8 0 0 1-8-8V7z"/><path d="M9 12l2 2 4-4"/></svg>`,
  knowledge: `<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 5.5A1.5 1.5 0 0 1 5.5 4H10a2 2 0 0 1 2 2v13a1.5 1.5 0 0 0-1.5-1.5H4z"/><path d="M20 5.5A1.5 1.5 0 0 0 18.5 4H14a2 2 0 0 0-2 2v13a1.5 1.5 0 0 1 1.5-1.5H20z"/></svg>`,
  banwords: `<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" aria-hidden="round" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M5.6 5.6l12.8 12.8"/></svg>`,
  checklist: `<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M9 11l3 3 8-8"/><path d="M20 12v7a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h9"/></svg>`,
  private: `<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="5" y="11" width="14" height="9" rx="2"/><path d="M8 11V7a4 4 0 0 1 8 0v4"/></svg>`,
  package: `<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 8l-9-5-9 5 9 5 9-5z"/><path d="M3 8v8l9 5 9-5V8"/><path d="M12 13v8"/></svg>`,
};

/** 把一个包的文件按角色分组渲染到容器里。无网络调用：纯 DOM。
 *
 * 之前在两个 #pane-kb / #pane-skills 面板里各持一份（PANEL_FILE_FILTER
 * 按 rel 分类过滤后再走同样的逻辑）。2026-09-17 合并到「行业包」面板
 * 一份 ——「知识库 / 技能 / 合规 / 私有 / …」是**一个包内部**的角色分类，
 * 不是平行资源，再单独拆面板就是把同一份数据切三份。
 */
function renderPackGroups(name, p, container) {
  container.innerHTML = "";
  const files = p.files || [];
  if (!files.length) {
    container.appendChild(el("p", "hint", "这个包还没有文件"));
    return;
  }
  // 按 ROLE_GROUP_ORDER 顺序分组，未识别的归到"包清单"组（兜底）。
  const buckets = new Map(ROLE_GROUP_ORDER.map(g => [g.key, []]));
  for (const f of files) {
    const k = fileRole(f.rel);
    if (!buckets.has(k)) buckets.set(k, []);
    buckets.get(k).push(f);
  }
  const groups = el("div", "kb-groups");
  for (const { key, label } of ROLE_GROUP_ORDER) {
    const items = buckets.get(key);
    if (!items || !items.length) continue;        // 空组不显示（不浪费一行的分组标题）
    const grp = el("div", "kb-group");
    const h = el("h4", "kb-group-t");
    h.appendChild(document.createTextNode(label));
    h.appendChild(el("span", "kb-group-c", String(items.length)));
    grp.appendChild(h);
    const cards = el("div", "kb-cards");
    for (const f of items) {
      const row = el("button", "kb-item");
      row.type = "button";
      row.dataset.role = key;
      // 顺序：图标 → 元信息（名称）→ 大小。firstChild 是图标，
      // 文件名放进 .kb-name —— 这是断言要查的元素（不是 firstChild 文本）。
      const icon = el("span", "kb-icon");
      icon.innerHTML = ROLE_ICON[key] || ROLE_ICON.package;
      row.appendChild(icon);
      const meta = el("div", "kb-meta");
      meta.appendChild(el("span", "kb-name", f.rel));
      row.appendChild(meta);
      const sizeTxt = f.size > 1024
        ? (f.size / 1024).toFixed(1) + " KB"
        : f.size + " B";
      row.appendChild(el("span", "kb-size", sizeTxt));
      row.onclick = () => showPackFile(name, f.rel, row);
      cards.appendChild(row);
    }
    grp.appendChild(cards);
    groups.appendChild(grp);
  }
  container.appendChild(groups);
}

/** 行业包面板 / 唯一查看器。点 #pi-groups 里的 .kb-item → 加载到 #pi-file-view。
 *  之前还接了 #kb-list / #skills-list 两套；现在只有一份。
 */
async function showPackFile(name, rel, row) {
  document.querySelectorAll("#pi-groups .kb-item").forEach(n => n.classList.remove("on"));
  if (row) row.classList.add("on");
  $("pi-file-title").textContent = rel;
  $("pi-file-size").textContent = "";
  $("pi-file-body").textContent = "载入中…";
  try {
    const d = await api.packFile(name, rel);
    $("pi-file-size").textContent =
      d.size > 1024 ? (d.size / 1024).toFixed(1) + " KB" : d.size + " B";
    $("pi-file-body").textContent = d.text;
  } catch (e) {
    $("pi-file-body").textContent = "读取失败：" + e.message;
  }
}

const PACKGEN_HINT = "模型正在生成行业结构：细分领域、受众、选题库、红线与核实清单。";

/** 轮询建包作业直到终态；每轮把作业上的过程信息搬到界面（思考字数 / 接口重试）。
 *  `api.job` 抛错就往外抛：404 = 引擎重启、作业随内存释放，由调用方落到错误横幅。 */
async function pollPackgen(jid, onTick) {
  for (;;) {
    await new Promise(r => setTimeout(r, 900));
    const s = await api.job(jid);
    if (s.state === "packing" || s.state === "queued") {
      const retry = (s.steps || []).filter(x => x.key === "retry").pop();
      const think = s.stream && s.stream.reasoning_len ? `已思考 ${s.stream.reasoning_len} 字` : "";
      onTick([PACKGEN_HINT, think, retry ? retry.title : ""].filter(Boolean).join(" · "));
    }
    if (s.state === "done" || s.state === "failed" || s.state === "cancelled") return s;
  }
}

async function runPackgen() {
  if (packgenJob) return;                       // 已经有一个在跑（按钮此时是禁用态）
  const industry = $("pg-industry").value.trim();
  const desc = $("pg-desc").value.trim();
  if (!industry || !desc) { toast("请填写行业名称和业务描述"); return; }
  const btn = $("pg-run");
  const errBox = $("pg-error");
  // textContent 会把按钮里的 spark 图标冲掉：先留个引用，结束时把它放回去，
  // 否则每生成（或失败）一次，按钮就永久少一颗图标。
  const spark = btn.querySelector(".ic-spark");
  const working = $("pg-working");
  const t0 = Date.now();
  let timer = null;
  let note = "";
  const tick = (n) => {
    // 计时器解决的是「还在跑 vs 卡死了」；作业上的思考字数与重试提示
    // 进一步回答「它在干什么」（原来这里只有一行写死的「约 1-2 分钟」）。
    // note 必须留着：秒表每秒重写一次这一行，不带 note 就会把上一拍的进度冲掉。
    if (n !== undefined) note = n;
    btn.textContent = packgenCanceling ? "取消中…"
      : `生成中 · ${Math.round((Date.now() - t0) / 1000)}s…`;
    if (working) working.textContent = note || PACKGEN_HINT;
  };
  const finish = () => {
    clearInterval(timer);
    packgenJob = null;
    packgenCanceling = false;
    btn.disabled = false;
    if (working) working.classList.add("hidden");
    btn.textContent = "";
    if (spark) btn.appendChild(spark);
    btn.append("生成");
  };
  // 失败不能只靠一条几秒的 toast：用户走开一下回来就是「生成完没有后续」。
  // 原因常驻在表单下方，表单与输入值都保留，改完字段一键重试。
  const fail = (msg) => { errBox.textContent = msg; errBox.classList.remove("hidden"); toast(msg, 6000); };
  errBox.classList.add("hidden");
  errBox.textContent = "";
  btn.disabled = true;
  if (working) { working.textContent = PACKGEN_HINT; working.classList.remove("hidden"); }
  tick();
  timer = setInterval(() => tick(), 1000);
  let snap;
  try {
    // P1-43：起作业 + 轮询，而不是把 1~2 分钟的模型调用挂在 HTTP 请求上。
    const started = await api.createPack(industry, desc);
    packgenJob = started.job_id;
    snap = await pollPackgen(packgenJob, tick);
  } catch (e) {
    finish();
    fail(`生成失败：${e.message}`);
    return;
  }
  finish();
  if (snap.state === "cancelled") { toast("已取消生成"); return; }
  if (snap.state === "failed") { fail(`生成失败：${snap.error || "未知错误"}`); return; }
  const out = snap.result || {};
  state.lastCreatedPack = out.name;
  $("pg-form").classList.add("hidden");
  $("pg-result").classList.remove("hidden");
  renderPackgenSummary(out);
  $("pg-checklist").textContent = out.checklist;
  toast(`行业包「${out.display_name}」已生成（草稿）`);
}

/** 结果页「生成了什么」：细分/受众/人设/选题摘要。
 *  ⚠ 内容全部来自模型输出，一律 textContent，绝不 innerHTML 拼接。 */
function renderPackgenSummary(out) {
  const box = $("pg-summary");
  box.innerHTML = "";
  const title = el("div", "pg-summary-t");
  title.textContent = `已生成「${out.display_name}」行业包（草稿）`;
  box.appendChild(title);
  const stats = el("div", "pg-summary-stats");
  [["细分领域", out.segments], ["受众", out.audiences], ["人设", out.personas]]
    .forEach(([label, list]) => {
      if (!Array.isArray(list)) return;
      const s = el("span", "pg-stat");
      const n = el("b"); n.textContent = String(list.length);
      const l = el("span"); l.textContent = label;
      s.appendChild(n); s.appendChild(l);
      stats.appendChild(s);
    });
  const ideaN = el("span", "pg-stat");
  const ni = el("b"); ni.textContent = String((out.ideas || []).length);
  const li = el("span"); li.textContent = "选题";
  ideaN.appendChild(ni); ideaN.appendChild(li);
  stats.appendChild(ideaN);
  box.appendChild(stats);
  [["细分领域", out.segments], ["受众", out.audiences], ["人设", out.personas]]
    .forEach(([label, list]) => {
      if (!Array.isArray(list) || !list.length) return;
      const row = el("div", "pg-summary-row");
      row.appendChild(el("span", "pg-summary-k", label));
      const v = el("span", "pg-summary-v");
      v.textContent = list.join("、");
      row.appendChild(v);
      box.appendChild(row);
    });
  const ideas = out.ideas || [];
  if (ideas.length) {
    const row = el("div", "pg-summary-row");
    row.appendChild(el("span", "pg-summary-k", "选题示例"));
    const v = el("div", "pg-summary-v");
    ideas.slice(0, 5).forEach((t, i) => {
      const line = el("div", "pg-idea");
      line.textContent = `${i + 1}. ${t}`;
      v.appendChild(line);
    });
    if (ideas.length > 5) v.appendChild(el("div", "pg-idea-more", `…共 ${ideas.length} 条`));
    row.appendChild(v);
    box.appendChild(row);
  }
}

async function onPackDone() {
  setPane("gen");
  try {
    state.meta = await api.meta();
    emit("meta", state.meta);
    // 显式选中刚建好的包（emit 只负责刷新列表，选择权在这里）
    fillPackSelect({ prefer: state.lastCreatedPack });
    toast("已切换到新建的行业包");
  } catch (e) { toast("刷新行业包列表失败：" + e.message, 3500); }
}

// 「导出为 Agent 技能」整个入口已删（2026-09-17）。原来它会把 pack 目录
// 复制成一份 SKILL.md 技能目录 —— 一个用一次就忘的动作，却占着包详情底部
// 最显眼的位置。后端 `/api/packs/<name>/export-skill` 保留（没被别处依赖，
// 删接口是另一件事），只是界面不再暴露。

async function undraftPack() {
  const name = $("pack").value;
  const ok = await appConfirm("标记为已校对",
    "确认该行业包已人工校对完毕？\n草稿标记会被移除，生成结果不再提示“需校对”。");
  if (!ok) return;
  try {
    await api.undraft(name);
    toast("已标记为校对完成");
    state.meta = await api.meta();
    emit("meta", state.meta);
    await openPackInfo();
  } catch (e) { toast("操作失败：" + e.message, 3500); }
}

// packinfo 表格的「说明」列要中文标签，调用方传入 fileRole() 拿到 key 再查 ROLE_LABEL。
// 不要在这里另写一份中文映射：两份的话改一份忘一份就漂了。
// 之前有一份返回中文标签的 FILE_ROLE()，现已统一到 fileRole + ROLE_LABEL。

/**
 * 渲染中列的「已安装行业包」条目列表（三列骨架的第二层级）。
 * 与模型接口一样，保留 `.pl-item` 通用条目形态。
 * ⚠ 与「当前启用」不同：行业包没有「唯一启用」语义（任何包都可以选），
 *   所以 `.on` 留给业务态（这里是「草稿」），`.sel` 表示「当前在右列查看」。
 */
function renderPackList() {
  const list = $("pi-list");
  if (!list) return;
  list.innerHTML = "";
  const packs = (state.meta && state.meta.packs) || [];
  if (!packs.length) {
    list.appendChild(el("p", "hint", "还没有行业包 —— 点右上角「新建」生成一个。"));
    return;
  }
  for (const p of packs) list.appendChild(packItem(p));
  // 恢复右列当前查看的那条的 .sel（列表重建会丢选中态）
  const cur = $("pack").value;
  if (cur) {
    const it = list.querySelector(`.pl-item[data-id="${CSS.escape(cur)}"]`);
    if (it) it.classList.add("sel");
  }
}

function packItem(p) {
  const item = el("button", "pl-item");
  item.type = "button";
  item.dataset.id = p.name;
  if (p.draft) item.classList.add("on");        // 草稿态有视觉落点（待校对）

  // 图标：打包盒标记（与生成偏好左侧「行业包」同一枚图标、同一套 SVG 规格）
  item.appendChild(el("span", "pl-ic", ICON_PACK));

  const txt = el("span", "pl-txt");
  txt.appendChild(el("span", "pl-t", esc(p.display_name || p.name)));
  txt.appendChild(el("span", "pl-s",
    esc(p.draft ? "草稿 · 待校对" : "已校对")));
  // ⚠ 2026-09-17：去掉 description 行。中列窄（258px），再加一行字就两行，
  // 看起来像「右侧空 / 左侧堆字」。description 已在右列 headbar 显示，
  // 这里不再重复。
  item.appendChild(txt);

  item.onclick = () => selectPack(p.name);
  return item;
}

/** 选中一个包：右列加载它的详情（同时同步 #pack.value，其他代码依赖它）。 */
function selectPack(name) {
  const sel = $("pack");
  if (sel) sel.value = name;
  document.querySelectorAll("#pi-list .pl-item").forEach(n => {
    n.classList.toggle("sel", n.dataset.id === name);
  });
  openPackInfo(name).catch(e => toast("读取失败：" + e.message, 3500));
}

async function openPackInfo(name) {
  // 三列化后 name 由调用方传；保留旧的「未传则从 #pack 读」作为兜底，
  // 保证 `btn-packinfo` / `pi-refresh` 这两条老路径仍然能用。
  if (name === undefined) name = $("pack").value;
  const p = await api.pack(name);
  $("pi-title").textContent = `${p.display_name || name} · 包内容`;
  $("pi-desc").textContent = (p.description || "")
    + (p.draft ? "（草稿包：内容需人工校对后投产）" : "");
  $("pi-undraft").classList.toggle("hidden", !p.draft);
  const cw = $("pi-checklist-wrap");
  if (p.checklist) {
    cw.classList.remove("hidden");
    $("pi-checklist").textContent = p.checklist;
  } else {
    cw.classList.add("hidden");
  }
  // 切包 / 刷新：把文件查看器复位到「未选择文件」占位态。
  // 不复位的话，上个包选中的行还亮着、右列还在显示上一个包的内容 —
  // 用户切到 fitment 包会以为 elevator 还在生效。
  $("pi-file-title").textContent = "未选择文件";
  $("pi-file-size").textContent = "";
  $("pi-file-body").textContent = "从左侧文件分组选择一个文件查看内容。";
  // 2026-09-17 合并：原 #pi-files 的扁平表格换成与原知识/技能面板同型的
  // 角色分组列表（.kb-groups）。一个行业一份知识+技能+合规+私有，
  // 整个组的视觉语言见 styles.css 的 .kb-group 系列。
  renderPackGroups(name, p, $("pi-groups"));
}
