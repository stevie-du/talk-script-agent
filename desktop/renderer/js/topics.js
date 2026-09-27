// 今日选题 / 情报源（B 线，需求方案 §2.2 与 §2.4/§2.5）。
//
// 这一屏的全部结构都来自**后端下发的那份声明**（`pack.yaml` 的 `intel_sources`
// → `/api/meta` 的 `packs[].intel_sources` + `/api/intel/today` 的 `groups`）。
// 本文件里**一个平台名都不写死** —— 换行业包时分区、chip、计数、「未接入 N」
// 自动跟着变，这正是「一处声明、四处一致」要的那件事。
//
// 三条容易做错的：
//  1. **「换一批」不是重新抓**：今天抓回来的池子有 N 条，一屏放 5 条，
//     换一批 = 在池子里按机会分往下翻（纯前端，零成本）。要新数据点顶栏那颗重抓。
//  2. **懒触发不在这里硬编码节奏**：`/api/intel/today` 回 `stale`，
//     这里只在 stale 且本会话没触发过时补一发 POST（GET 不带副作用 ——
//     会自己起作业的 GET 在轮询/预取时会被重复触发，每次都是一轮真实网络请求）。
//  3. **「去生成」不自动发送**：一次生成几十秒真金白银，要留改参数的机会。
//     它只做三件事：切回会话视图、写 `#topic`、把参数填进 `state.genParams`。
import { $, esc, sleep, toast, bindOnce, wireTablist } from "./util.js";
import { api } from "./api.js";
import { state, on } from "./store.js";
import { gotoView, refreshGate, setParam } from "./ui.js";
import { setViewHead } from "./head.js";
import { packLabel } from "./sessions.js";
import { BUSY_STATES } from "./progress.js";
import { STATE_LABEL, CADENCE_LABEL } from "./words.js";

/** 一屏放几张卡。「换一批」就是按这个粒度在池子里翻页（§2.5 定的是 4–6 条）。 */
const PAGE_SIZE = 5;

/** 情报源接入态的中文名。后端 `app/intel.py` 的 state 共五值：ok / unwired /
 *  off / error（抓取时抛了异常）/ orphan（配置里已删、盘上还有旧条目）。
 *  少映射一个，表格里就会直接冒出 error / orphan 这样的英文词。 */
const SOURCE_STATE_LABEL = {
  ok: "已接入",
  unwired: "未接入",
  off: "未启用",
  error: "抓取失败",
  orphan: "配置已移除",
};

/** 最近一次 `/api/intel/today` 的结果。null = 还没加载过。 */
let data = null;

/** 某个源这轮抓取失败的原因（空串 = 没失败）。
 *
 *  ⚠ 必须认 `errors`，**不能只看 `g.state`**：`app/intel.py` 的 `today()` 重建
 *  groups 时 state 只有 `off` / `unwired` / `ok` 三种 —— 抓取失败的源在那边是
 *  `state: "ok"` + `count: 0`，与"今天没货"**完全同形**。唯一携带"我们没抓到"
 *  这个信息的字段就是 `errors[源 label]`。不认它，用户会把"平台今天没有"当成
 *  "我们没抓"（或反过来），而这两件事的下一步动作正好相反。
 *  （`SOURCE_STATE_LABEL` 里的 `error` / `orphan` 是给落盘记录与孤儿源用的，
 *   其中 `error` **不会**出现在 `today()` 的 groups 里 —— 别指望它。）
 *  ⚠ 键是 **label 不是 id**：一个适配器出多个源（`hot_board` 三条）时 id 相同，
 *   按 id 查会让一次超时把三个源全标成失败 —— 后端 `fetch_pack` 也按 label 写。 */
function sourceError(label) {
  return (data && data.errors && data.errors[label]) || "";
}

/** 框架级失败（键带下划线前缀）：`_read` = 抓取记录读不出来，`_save` = 没存下来。
 *
 *  后端 `intel.load_latest()` 的 docstring 写明了 `errors` 存在的唯一目的：
 *  「界面才能把"没抓过"与"抓了但文件坏了"分开显示 —— 这两种"没有"在界面上
 *  必须是两种样子」。源级失败由源表承担，这两条由顶部/说明行承担。 */
function frameErrors() {
  const e = (data && data.errors) || {};
  return Object.keys(e).filter(k => k.indexOf("_") === 0).map(k => [k, e[k]]);
}
/** 当前筛选的来源（`all` 或某个源的 label）。筛选轴**只有来源这一根**，不混别的维度。 */
let curSrc = "all";
let page = 0;
/** 懒触发：本会话只补抓一次，避免每次切回来都发一轮真实网络请求。 */
let kickoffDone = false;

function packName() {
  // 兜底链到 default_pack 为止 —— 硬编码 "elevator" 会让换掉默认包的用户
  // 在下拉还没填充时静默读别的包的情报（P3）。
  return $("pack")?.value || state.meta?.default_pack || "";
}

/** 顶部那行「我在哪」：标题走壳层的 head.js，副标题只放**包名**。
 *  条数与抓取时间由页内 `#topics-meta` 独家承担 —— 同一句话写两处，
 *  迟早有一处不更新（而且头部那格还要给会话标题留位置）。 */
function paintTopicsHead() {
  setViewHead("topics", "今日选题", packLabel(packName()));
}

/** 一条条目的身份键（忽略记录按它存）。
 *
 *  ⚠ **由后端算**：`app/intel.py` 的 `item_key()` 随 `/api/intel/today` 的每条
 *  `item.key` 下发，这里直接取。曾经两边各拼一份（前端 `guid || url || title`、
 *  后端忽略过滤 `guid || title`），于是「只有 url、没有 guid」的条目（政策库那类）
 *  点「忽略」之后下一轮原样回来 —— 而"忽略没用"在界面上找不到任何原因。
 *  兜底只在后端漏给 key 时兜一下：真要漏了，verify 里那条「忽略用的是后端下发的
 *  item.key」会先红，不必靠这里的兜底去猜。 */
const itemKey = it => it.key || it.guid || it.url || `title:${it.title}`;

// ── 渲染 ────────────────────────────────────────────────────
function fmtPct(v) {
  // 「算不出」与「0」是两种形状：前者显示「—」，后者显示 0.00。
  // 后端把算不出的字段落成 null，这里必须原样区分（写成 `v || 0` 就抹平了）。
  return (v === null || v === undefined) ? "—" : Number(v).toFixed(2);
}

/** 抓取时间戳的短读法（`09-27 01:25`）。计数行、源表的「上次抓取」列、
 *  源表面那行「最近一次」三处共用 —— 各写一份 slice 迟早有一处忘了改，
 *  而原样吐 ISO（`2026-09-27T01:25:47+08:00`）在这一屏里既读不动也没必要。 */
function fmtFetched(iso) {
  return iso ? iso.slice(5, 16).replace("T", " ") : "";
}

function sigRow(label, value, dim, from100) {
  // 两档刻度：D / S / E 后端就是 0–1 的相对值；机会分是 **0–100 的合成分**
  // （判据 `opp >= 60` 在 app/intel.py 的 `_item_flags`，图例写的是同一个数）。
  // 以前这里不分刻度一律 `fmtPct`，机会分被印成 0.72，而同一屏的图例说「≥ 60」——
  // 一个数两套刻度，用户没法判断手里这条到底够不够线（审查文案线 P1-3）。
  // ⚠ 「算不出」与「0」是两种形状：null 显示「—」，0 显示 0（写成 `v || 0` 就抹平了）。
  const frac = (value === null || value === undefined) ? null
    : (from100 ? value / 100 : value);
  const w = frac === null ? 0 : Math.round(frac * 100);
  return `<span>${esc(label)}</span>`
    + `<div class="sig-track${dim ? " dim" : ""}"><i data-w="${w}"></i></div>`
    + `<span>${frac === null ? "—" : `<b>${from100 ? esc(String(Math.round(value))) : esc(fmtPct(frac))}</b>`}</span>`;
}

function card(it) {
  const sc = it.score || {};
  const flags = (it.flags || [])
    .map(([cls, text]) => `<span class="m-chip ${esc(cls)}">${esc(text)}</span>`).join("");
  const tags = [it.segment, it.platform].filter(Boolean)
    .map(t => `<span class="m-chip">${esc(t)}</span>`).join("");
  // 条目上不再有「warn 一行红字」这条分支：`it.warn` 引擎**从来不发**
  // （app/intel.py 只在 flags 里发 ["warn", "数据不足 · 算不出机会分"]），
  // 于是它是一条永远走不到的渲染支 —— 而 UI 桩偏偏给它写了值，
  // 门禁一直在那儿演一条不存在的能力（P3-7 / P2-5）。
  // 结论要说话就走 `flags`（卡片顶部那一排 .m-chip），那是引擎真发的通道。
  const from = it.published || it.url
    ? `<div class="topic-from">${esc(it.published || "")}${it.url ? ` · ${esc(it.url)}` : ""}</div>` : "";
  const opp = sc.opportunity;
  return `<div class="page-card" data-key="${esc(itemKey(it))}">
    <div class="res-top"><b class="card-title">${esc(it.title)}</b>${flags}</div>
    <div class="sig-grid">
      ${sigRow("需求 D", sc.D)}${sigRow("供给 S", sc.S)}
      ${sigRow("机会分", opp, false, true)}
      ${sigRow("事件 E", sc.E, true)}
    </div>
    <div class="topic-quote">${esc(it.desc || it.ev || "")}${from}</div>
    <div class="res-metric-list">${tags}</div>
    <div class="topic-foot"><span class="topics-grow"></span>
      <button class="ghost tiny" data-act="ignore">忽略</button>
      <button class="primary tiny" data-act="gen">去生成</button>
    </div>
  </div>`;
}

/** 筛选行。可筛的那几颗是**胶囊按钮**（`.src-pill`，与首页起手胶囊同型）；
 *  只做解释的两类 —— 「已接入今天没货」与「未接入 N」—— 留 `.m-chip` 静态标记。
 *  分界是**能不能点**：修复前整行都是 11px 的 `.m-chip`，选中态只有一层 5% 灰底，
 *  用户既看不出哪颗能点、也看不出点的是哪一颗。 */
function renderChips() {
  const box = $("src-chips");
  if (!box || !data) return;
  const groups = data.groups || [];
  const live = groups.filter(g => g.state === "ok" && g.count > 0);
  const pill = (src, label, n) =>
    `<button type="button" aria-pressed="${curSrc === src ? "true" : "false"}"`
    + ` class="src-pill${curSrc === src ? " on" : ""}"`
    + ` data-src="${esc(src)}">${esc(label)} ${n}</button>`;
  const chips = [pill("all", "全部", data.items.length)]
    .concat(live.map(g => pill(g.label, g.label, g.count)));
  // 「已接入今天没货」与「未接入」长得不一样是刻意的：
  // 前者是"今天没有"，后者是"我们没接"。合成一个说法会让用户以为平台坏了。
  // ⚠ 但**抓取失败的源不许算进"今天没货"**：`today()` 给失败源的 state 就是 "ok"
  //   + count 0，与"平台今天没有"完全同形 —— 这个文件 41-49 行把"必须认 errors"
  //   写成铁律，而这一行原来漏了：胶囊行说「抖音热榜 0 · 已接入，今天没有命中」，
  //   同一屏的计数行与源表却说「3 源抓取失败」。两句话只能有一句是对的，
  //   而用户按胶囊行那句会去**等平台**，实际该去做 API Key。
  const idle = groups.filter(g => g.state === "ok" && g.count === 0 && !sourceError(g.label));
  chips.push(...idle.map(g =>
    `<span class="m-chip" title="已接入，今天没有命中">${esc(g.label)} 0</span>`));
  // 失败源：不进可筛态（点它筛出一片空），但必须在这一行说话 —— 措辞与源表一致。
  const failed = groups.filter(g => g.state === "ok" && !!sourceError(g.label));
  chips.push(...failed.map(g =>
    `<span class="m-chip bad" title="${esc(sourceError(g.label))}">${esc(g.label)} 抓取失败</span>`));
  const off = groups.filter(g => g.state === "off" || g.state === "unwired");
  if (off.length) {
    // 「未接入」是**决定**（小红书不做 x-s 逆向）而不是"今天没货" ——
    // 所以它带一句为什么，且不进可筛态（点它不会筛出一片空）。
    const why = off.map(g => `${g.label}：${g.note || (g.state === "off" ? "未启用" : "引擎没有对应适配器")}`);
    chips.push(`<span class="m-chip" title="${esc(why.join("；"))}">未接入 ${off.length}</span>`);
  }
  box.innerHTML = chips.join("");
  box.querySelectorAll("[data-src]").forEach(b => {
    b.onclick = () => { curSrc = b.dataset.src; page = 0; render(); };
  });
}

function visibleItems() {
  if (!data) return [];
  return (data.groups || [])
    .filter(g => curSrc === "all" ? g.state === "ok" : g.label === curSrc)
    .flatMap(g => (g.items || []).map(it => ({ g, it })));
}

function render() {
  const root = $("topics-root");
  if (!root || !data) return;
  paintTopicsHead();           // 包可能换了（换包会重渲染），头部跟着走
  // 筛选不能悬空：被筛的那个源可能在本轮里消失（它的条目被逐条忽略到 0、
  // 或重抓后该源转为失败/未接）。那时 pill 行里没有任何一颗是选中的，
  // 卡区却写着"池子是空的"——而「全部」里其实还有货。
  // 用户唯一的自救是猜"要点全部"，所以这里直接回落，并让他看见回落。
  if (curSrc !== "all" && !(data.groups || [])
        .some(g => g.label === curSrc && g.state === "ok" && g.count > 0)) {
    curSrc = "all"; page = 0; renderChips();
  }
  const fe = frameErrors();          // 框架级失败：空态 / 计数行 / 说明行三处共用
  const shown = visibleItems();
  const pages = Math.max(1, Math.ceil(shown.length / PAGE_SIZE));
  if (page >= pages) page = 0;
  const slice = shown.slice(page * PAGE_SIZE, (page + 1) * PAGE_SIZE);

  // 分区：一个来源一组，组标题就是配置里的 `label · role`（.kb-group 是知识库页
  // 现成的范式，不是新发明）。卡片按机会分降序 —— 池子的顺序由后端定，这里不再排。
  // ⚠ 桶里存的是 `{g, items}`，所以按 `x.g.label` 找同组 —— 写成 `x.label` 会
  // 永远找不到（undefined !== label），于是**每张卡各成一组**：组标题重复、
  // 「一个来源一组」这条静默失效。由 verify.js 的「组数 = 有货的源数」那条抓出来。
  const groups = [];
  for (const { g, it } of slice) {
    let bucket = groups.find(x => x.g.label === g.label);
    if (!bucket) groups.push(bucket = { g, items: [] });
    bucket.items.push(it);
  }
  // 空态分两种：**"没抓过 / 今天没货"** 与 **"抓了但记录读不出来"**。
  // 后端专门为此把 `_read` 放进 errors（见 `intel.load_latest()` 的 docstring），
  // 这里合成一句"池子是空的"就等于把那个信号又丢掉了。
  const emptyText = fe.length
    ? "抓取记录读不出来（见下方说明）—— 点顶栏那颗「重抓」重跑一轮。"
    : "今天这个池子是空的 —— 点顶栏那颗「重抓」，或到「情报源」看哪个源没接上。";
  root.innerHTML = groups.map(({ g, items }) => `
    <div class="kb-group">
      <div class="kb-group-t">${esc(g.label)} · ${esc(g.role || "")}<span class="m-chip kb-group-c">${g.count}</span>
        ${g.note ? `<span class="hint">${esc(g.note)}</span>` : ""}</div>
      <div class="topic-grid">${items.map(card).join("")}</div>
    </div>`).join("")
    || `<p class="topics-empty">${emptyText}</p>`;

  // 数据驱动的宽度一律走 CSSOM：内联 style 属性会被 CSP 静默忽略
  // （style-src 'self' 且无 'unsafe-inline'），界面不会报错、条永远不显示。
  root.querySelectorAll(".sig-track > i").forEach(i => i.style.setProperty("--w", i.dataset.w || 0));
  root.querySelectorAll("[data-act]").forEach(btn => {
    const key = btn.closest(".page-card")?.dataset.key || "";
    btn.onclick = () => (btn.dataset.act === "ignore" ? doIgnore(key) : doGenerate(key));
  });

  const meta = $("topics-meta");
  if (meta) {
    // 抓取失败的源**不计入「已接」**：照 `g.state` 数会把"我们没抓到"报成
    // "已接入"，而用户看这行字就是为了判断"今天的数据靠不靠得住"。
    const on = (data.groups || []).filter(g => g.state === "ok" && !sourceError(g.label)).length;
    const bad = (data.groups || []).filter(g => sourceError(g.label)).length;
    const off = (data.groups || []).filter(g => g.state !== "ok").length;
    meta.textContent = `共 ${data.items.length} 条 · ${on} 源已接`
      + (bad ? ` · ${bad} 源抓取失败` : "")
      + ` · ${off} 源未接`
      + (fe.length ? " · 抓取记录有问题"
        : (data.fetched_at ? ` · 上次抓取 ${fmtFetched(data.fetched_at)}` : " · 还没抓过"));
  }
  const more = $("btn-more");
  if (more) {
    more.disabled = pages <= 1;
    more.textContent = pages <= 1 ? "就这些" : (page === 0 ? "换一批" : `第 ${page + 1}/${pages} 批`);
  }
  const ign = $("btn-ignored");
  if (ign) {
    // 「已忽略 N」配的是那句「只影响今天」—— 所以 N 必须是**今天**的条数，
    // 不是历史累计（`data.ignored` 是累计，拿它写这行会得到"今天忽略 2 条、
    // 行上写 17"，用户核对之后只能认为这功能在撒谎）。
    const today = (data && data.ignored_today) || 0;
    ign.textContent = `已忽略 ${today}`;
    ign.disabled = !today;
  }
  const cnt = $("topics-count");
  // 只有真的拿到数据才把这颗读数亮出来（默认 hidden）：导航上的 0 会被读成
  // "今天没货"，而冷启动时界面根本不知道有几条（流程线 P2-9）。
  if (cnt) { cnt.textContent = String(data.items.length); cnt.hidden = false; }

  // 框架级失败写进 `#topics-note`：它不是"某个源没抓到"（那是源表的事），
  // 而是"整份抓取记录坏了 / 存不下" —— 前者去情报源看，后者只能重抓。
  // 复用 `.hint.cfg-err`（--warn-soft 底 + --warn 字），与「配置错误行」同族。
  const note = $("topics-note");
  if (note) {
    note.textContent = fe.map(([k, v]) =>
      (k === "_save" ? "抓取结果没能存下来：" : "抓取记录读不出来：") + v).join("；");
    note.className = fe.length ? "hint cfg-err" : "hint";
  }
}

/** 情报源那张表：`接入` 与 `上次抓取` 两根正交列 + 三带图例（§2.4）。 */
function renderSources() {
  const tbl = $("sources-table");
  if (!tbl || !data) return;
  const groups = data.groups || [];
  const rows = groups.map(g => {
    // 抓取失败**优先于**"已接入"：`today()` 给失败源的 state 是 "ok"，
    // 照 state 渲染就等于把"我们没抓到"说成"平台今天没货"。
    const err = sourceError(g.label);
    const st = err ? "抓取失败" : (SOURCE_STATE_LABEL[g.state] || g.state);
    const state = `<td class="${g.state === "ok" && !err ? "ok" : "warn"}">${esc(st)}</td>`;
    const last = g.state === "ok" && g.count
      ? esc(fmtFetched(data.fetched_at)) : "—";
    return `<tr>
      <!-- 首列以中文 label 为主、id 收进次行：用户在筛选行看到的是「政策库」，
           而这张表原来第一行吐的是 policy_library、第二行是平台名 ——
           同一个源在一屏里两个名字对不上号；而且三个源共用一个 id（hot_board），
           只印 id 还会让三行看着像同一个源。
           ⚠ 这段注释在模板串内部：一律不许出现反引号（会把字符串截断，
             而截断后 node --check 可能照样通过、只在浏览器里报 SyntaxError）。 -->
      <td>${esc(g.label || g.id)}<br><span class="hint">${esc(g.id)}${g.platform ? " · " + esc(g.platform) : ""}</span></td>
      <td>${esc(g.role || "")}</td>
      <td>${esc(CADENCE_LABEL[g.cadence] || g.cadence || "—")}</td>
      ${state}
      <td>${last}</td>
      <td class="num">${g.count}</td>
      <td>${esc(err || g.note || "")}</td>
    </tr>`;
  }).join("");
  tbl.innerHTML = `<tr><th>源</th><th>角色</th><th>节奏</th><th>接入</th>
    <th>上次抓取</th><th class="num">今日命中</th><th>说明</th></tr>` + rows;
  const meta = $("sources-meta");
  if (meta) {
    const fe = frameErrors();
    meta.textContent = fe.length
      ? `抓取记录有问题：${fe.map(([, v]) => v).join("；")}`
      : (data.fetched_at ? `最近一次：${fmtFetched(data.fetched_at)}` : "还没有抓取记录");
  }
}

// ── 动作 ────────────────────────────────────────────────────
/** 等一条后台作业收口。**不复用 jobs.js 的 `poll()`**：那个是"当前生成作业"的
 *  轮询器（带进度条、思考流、取消按钮），情报抓取既没有流也没有步骤树 ——
 *  借它等于把抓取伪装成一次生成，用户会在选题页看到"文案撰写中"。
 *  这里只要三样：状态标签、终态、失败原因。 */
async function awaitJob(id) {
  // ⚠ 状态写在本视图自己的说明行 #topics-note，**不写全局头部 #rh-state**：
  //   那里是生成流程 setHead 的地盘 —— 重抓每 900ms 覆盖它、落地时清空，
  //   与并发进行的生成互相打架（「情报抓取中」盖掉「生成中」、或把进行中
  //   的生成状态整行清掉）。选题视图隐藏时这行自然看不见，不污染别的视图（P3）。
  const st = $("topics-note");
  const say = (text, err) => {
    if (!st) return;
    st.textContent = text;
    st.className = "hint" + (err ? " cfg-err" : "");
  };
  // 客户端截止：后端有 JOB_BUDGET_SECONDS=1200（20 分钟作业预算），这里取 30
  // 分钟宽于它。没有上限时，引擎"作业丢了但没报错"（进程被杀后重启、作业表被
  // 清）会让这个循环永远转下去，用户看到「抓取中」常驻到天荒地老。
  const t0 = Date.now();
  for (;;) {
    if (Date.now() - t0 > 1800000) {
      say("");
      toast("抓取状态未知（已等待 30 分钟），请手动刷新或重试", 5000);
      return null;
    }
    let snap;
    try {
      snap = await api.job(id);
    } catch {
      say("");
      return null;
    }
    const live = BUSY_STATES.has(snap.state);
    // STATE_LABEL[fetching] 本身就是「情报抓取中」—— 直接用标签，别再包一层
    // 同义的壳（曾经渲染成「情报抓取中（情报抓取中）…」）。
    if (live) say(`${STATE_LABEL[snap.state] || "处理中"}…`);
    if (!live) {
      say("");
      if (snap.state === "failed") toast(`抓取失败：${snap.error || "未知原因"}`, 3600);
      return snap;
    }
    await sleep(900);
  }
}

async function load() {
  try {
    data = await api.intelToday(packName());
  } catch (e) {
    // 只读端点**空或坏返回空结构、不抛**；真抛出来只可能是引擎没起来/令牌失效，
    // 那是另一个层面的问题（与"今天没抓到"不同），如实说。
    toast(`读取今日选题失败：${e.message}`, 3200);
    data = { pack: packName(), fetched_at: "", groups: [], items: [], errors: {},
             stale: false, ignored: 0, unwired: 0 };
  }
  renderChips();
  render();
  renderSources();
  return data;
}

/** 懒触发：`stale` 时补一发重抓（**本会话只补一次**）。 */
async function kickoffIfStale() {
  if (kickoffDone || !data?.stale) return;
  kickoffDone = true;
  try {
    const { job_id } = await api.intelRefresh(packName());
    toast("正在后台抓取情报…", 2400);
    await awaitJob(job_id);
    await load();
  } catch (e) {
    // 重抓失败**不影响已经显示的内容** —— 抓取器在生成主链路之外，
    // 失败只意味着"这次没拿到新的"，旧数据照常可看。
    toast(`重抓失败：${e.message}（已显示上次抓到的内容）`, 3600);
  }
}

async function doIgnore(key) {
  try {
    const r = await api.intelIgnore(packName(), key);
    // 本地也摘掉：等服务端回一轮再重渲染会有一次肉眼可见的跳动。
    for (const g of data.groups || []) g.items = (g.items || []).filter(it => itemKey(it) !== key);
    data.items = (data.items || []).filter(it => itemKey(it) !== key);
    data.ignored = r.ignored;
    // 服务端回的两个数都要落，否则本地那一行会停在旧值上（重渲染才纠正，
    // 而"点了一下、数字没动"正是这条路径上唯一的用户反馈）。
    data.ignored_today = typeof r.ignored_today === "number"
      ? r.ignored_today : (data.ignored_today || 0) + 1;
    for (const g of data.groups || []) g.count = (g.items || []).length;
    renderChips(); render();
    toast("已忽略 —— 只影响今天，明天同题还会回来", 2600);
  } catch (e) {
    toast(`忽略失败：${e.message}`, 3200);
  }
}

/** 「去生成」= 切回会话视图 + 写主题 + 填参数，**不自动发送**（§2.5 定稿）。
 *  参数走 `setParam`：真值在 `state.genParams`，而界面上那颗参数胶囊是**自绘**的
 *  （原生 select 收在 .native-hidden 里）—— 只写 state 不重画，用户在参数条上
 *  看到的还是上一个值，而他正是要在参数条上确认参数的（P1）。 */
function doGenerate(key) {
  const hit = visibleItems().find(({ it }) => itemKey(it) === key);
  const it = hit?.it;
  if (!it) return;
  $("topic").value = it.title || "";
  // 选题条目的细分领域是**抓取时带回来的**，不保证落在当前行业包声明的选项里。
  // 落不进时不许静默换成默认值、也不许只写 state —— 那就成了
  // "发出去一个界面上显示不出来的值"。如实说出来，让用户自己选。
  const segOk = !it.segment || setParam("segment", it.segment);
  refreshGate();
  gotoView("chat");
  $("topic").focus();
  toast(segOk ? "已填好主题与细分领域 —— 确认参数后点发送（不会自动开始）"
              : `主题已填好；但这条选题的细分领域「${it.segment}」不在当前行业包的选项里`
                + " —— 请在参数条里自己选一个，再点发送", segOk ? 3200 : 5200);
}

// ── 绑定 ────────────────────────────────────────────────────
/** 选题 ↔ 情报源。收在模块变量里而不是就地写：`openTopics()` 每次进视图要把
 *  面归回「选题」，而它和绑定函数不在同一层。 */
let selectTopicsTab = () => {};

// bindOnce：`$("pack").addEventListener("change", …)` 会**重复挂**（一次切换触发
// 两次 load），而 boot() 在「新建行业包完成」等路径上会被二次调用 —— 少这层包装
// 时，_verify 的「再绑一遍不多监听器」断言也漏掉了这第五个绑定函数（main.js 的
// __ts.rebind 同样要把它列进去，两边一起才算真幂等）。
export const bindTopics = bindOnce(function bindTopics() {
  $("btn-topics").onclick = () => openTopics();
  $("btn-more").onclick = () => { page += 1; render(); };
  // 「已忽略 N」点击给一句如实的解释（P3：曾经是一颗点了没反应的死按钮）。
  // 忽略记录当前只有计数这一个消费面（没有忽略清单 UI），所以诚实的行为
  // 是把语义说清，而不是装作会展开一个不存在的列表。
  $("btn-ignored").onclick = () => {
    const n = (data && data.ignored_today) || 0;
    const total = (data && data.ignored) || 0;
    toast(n ? `今天已忽略 ${n} 条 —— 忽略只影响今天，明天同题会自动回来`
            + (total > n ? `（历史上累计忽略过 ${total} 条）` : "")
            : "今天还没有忽略过任何条目", 3600);
  };
  $("btn-refetch").onclick = async () => {
    const btn = $("btn-refetch");
    const title = btn.title;
    // 点下去必须有 in-flight 形态（流程线 P1-2）：状态行 #topics-note 会被页签/视图
    // 藏起来，抓取又没有进度条与步骤树 —— 于是"点了没反应"，用户连点就堆出第二个作业。
    // ⚠ 这是颗**图标按钮**（里面只有 SVG），所以绝不能用 textContent 写"抓取中…"：
    //   那会把图标整颗抹掉，落地后还回去的是一个空按钮（门禁当场抓到过一次）。
    //   进行中写在三个不会破坏画形的地方：禁用 + aria-busy + 图标转起来，title 说人话。
    btn.disabled = true;
    btn.setAttribute("aria-busy", "true");
    btn.classList.add("busy");
    btn.title = "正在重抓…（后台作业，可切去别处）";
    try {
      const { job_id } = await api.intelRefresh(packName());
      toast("已开始重抓…", 2200);
      await awaitJob(job_id);
      await load();
    } catch (e) {
      toast(`重抓失败：${e.message}`, 3200);
    } finally {
      btn.disabled = false;
      btn.removeAttribute("aria-busy");
      btn.classList.remove("busy");
      btn.title = title;
    }
  };
  // 「选题 / 情报源」两颗页签：aria、roving tabindex 与 ←/→ Home End
  // 全在 util.wireTablist 里（与结果卡的产物分区同一份实现）。
  selectTopicsTab = wireTablist($("topics-tabs"), ".res-tab", id => {
    $("topics-scroll").classList.toggle("hidden", id !== "topics");
    $("topics-src").classList.toggle("hidden", id !== "sources");
    // 页签条要跟当前面同档宽（CSS 认这个属性）：吸顶条是不透明的，
    // 只盖住 780 的话 1160 的表格会从条的两侧穿出来。
    $("view-topics").dataset.face = id;
    // 两个面共用一个滚动盒子，切面时归零 —— 否则从长表切回卡列表会停在半路。
    $("view-topics").scrollTo(0, 0);
  });
  document.querySelectorAll("#topics-tabs .res-tab").forEach(b => {
    b.onclick = () => selectTopicsTab(b.dataset.tab);
  });
  // 换行业包：分区、胶囊、计数、未接入、头部副标题全部要跟着换（同一份声明渲染五处）。
  // ⚠ 订阅 `applyPackSwitch` 发出的事件，而不是 `#pack` 的 change ——
  //   换包有三条路径，面板点选与程序化指包都不派发 change
  //   （挂在事件上 = 只在其中一条路径上刷新，另两条就出现"看着 A 包、发往 B 包"）。
  on("pack", () => {
    curSrc = "all"; page = 0; kickoffDone = false;
    if ($("right").dataset.view === "topics") load().then(kickoffIfStale);
  });
});

/** 进选题视图。左栏与会话列表**常驻**（不跳页、不开二级窗），只换右栏内容区。 */
export async function openTopics() {
  paintTopicsHead();
  // ⚠ 次序：先切视图，再归位页签。反了的话页签回调里的 `scrollTo(0,0)`
  //   落在一个还带着 `.hidden`（display:none）的滚动盒子上 —— 设了等于没设，
  //   "回到顶部"这件事只是碰巧被浏览器重置偏移兜住了（换成 visibility 隐藏就静默回归）。
  gotoView("topics");
  selectTopicsTab("topics", false);
  await load();
  kickoffIfStale();
}
