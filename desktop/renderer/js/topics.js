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
import { $, esc, sleep, toast } from "./util.js";
import { api } from "./api.js";
import { state, setGenParam } from "./store.js";
import { gotoView, refreshGate } from "./ui.js";
import { BUSY_STATES, STATE_LABEL } from "./progress.js";

/** 一屏放几张卡。「换一批」就是按这个粒度在池子里翻页（§2.5 定的是 4–6 条）。 */
const PAGE_SIZE = 5;

/** 最近一次 `/api/intel/today` 的结果。null = 还没加载过。 */
let data = null;
/** 当前筛选的来源（`all` 或某个源的 label）。筛选轴**只有来源这一根**，不混别的维度。 */
let curSrc = "all";
let page = 0;
/** 懒触发：本会话只补抓一次，避免每次切回来都发一轮真实网络请求。 */
let kickoffDone = false;

function packName() {
  return $("pack")?.value || state.meta?.default_pack || "elevator";
}

/** 一条条目的去重键 —— 必须与后端 `intel.dedup` 用的是同一个键，
 *  否则"忽略了但明天又出现"会变成找不到原因（忽略记录按这个键存）。 */
const itemKey = it => it.guid || it.url || `title:${it.title}`;

// ── 渲染 ────────────────────────────────────────────────────
function fmtPct(v) {
  // 「算不出」与「0」是两种形状：前者显示「—」，后者显示 0.00。
  // 后端把算不出的字段落成 null，这里必须原样区分（写成 `v || 0` 就抹平了）。
  return (v === null || v === undefined) ? "—" : Number(v).toFixed(2);
}

function sigRow(label, value, dim) {
  const w = (value === null || value === undefined) ? 0 : Math.round(value * 100);
  return `<span>${esc(label)}</span>`
    + `<div class="sig-track${dim ? " dim" : ""}"><i data-w="${w}"></i></div>`
    + `<span>${value === null || value === undefined ? "—" : `<b>${esc(fmtPct(value))}</b>`}</span>`;
}

function card(it) {
  const sc = it.score || {};
  const flags = (it.flags || [])
    .map(([cls, text]) => `<span class="m-chip ${esc(cls)}">${esc(text)}</span>`).join("");
  const tags = [it.segment, it.platform].filter(Boolean)
    .map(t => `<span class="m-chip">${esc(t)}</span>`).join("");
  const warn = it.warn
    ? `<div class="topic-from">
         <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-linecap="round"
              stroke-linejoin="round" aria-hidden="true"><path d="M8 2.5l5.8 10.7H2.2z"/>
           <path d="M8 6.6v2.8"/><circle cx="8" cy="11.3" r=".7" fill="currentColor" stroke="none"/></svg>
         ${esc(it.warn)}</div>` : "";
  const from = it.published || it.url
    ? `<div class="topic-from">${esc(it.published || "")}${it.url ? ` · ${esc(it.url)}` : ""}</div>` : "";
  const opp = sc.opportunity;
  return `<div class="page-card" data-key="${esc(itemKey(it))}">
    <div class="res-top"><b class="card-title">${esc(it.title)}</b>${flags}</div>
    <div class="sig-grid">
      ${sigRow("需求 D", sc.D)}${sigRow("供给 S", sc.S)}
      ${sigRow("机会分", opp === null || opp === undefined ? null : opp / 100)}
      ${sigRow("事件 E", sc.E, true)}
    </div>
    <div class="topic-quote">${esc(it.desc || it.ev || "")}${from}${warn}</div>
    <div class="res-metric-list">${tags}</div>
    <div class="topic-foot"><span class="topics-grow"></span>
      <button class="ghost tiny" data-act="ignore">忽略</button>
      <button class="primary tiny" data-act="gen">去生成</button>
    </div>
  </div>`;
}

/** 筛选行：只列**接了且今天有货**的源；`未接入 N` 走一颗不可点的 chip 说明为什么。 */
function renderChips() {
  const box = $("src-chips");
  if (!box || !data) return;
  const groups = data.groups || [];
  const live = groups.filter(g => g.state === "ok" && g.count > 0);
  const chips = [`<span class="m-chip${curSrc === "all" ? " acc-badge info" : ""}" data-src="all">全部 ${data.items.length}</span>`]
    .concat(live.map(g =>
      `<span class="m-chip${curSrc === g.label ? " acc-badge info" : ""}" data-src="${esc(g.label)}">`
      + `${esc(g.label)} ${g.count}</span>`));
  // 「已接入今天没货」留在行内、压淡 —— 与「未接入」长得不一样是刻意的：
  // 前者是"今天没有"，后者是"我们没接"。合成一个说法会让用户以为平台坏了。
  const idle = groups.filter(g => g.state === "ok" && g.count === 0);
  chips.push(...idle.map(g =>
    `<span class="m-chip" title="已接入，今天没有命中">${esc(g.label)} 0</span>`));
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
  root.innerHTML = groups.map(({ g, items }) => `
    <div class="kb-group">
      <div class="kb-group-t">${esc(g.label)} · ${esc(g.role || "")}<span class="m-chip kb-group-c">${g.count}</span>
        ${g.note ? `<span class="hint">${esc(g.note)}</span>` : ""}</div>
      <div class="topic-grid">${items.map(card).join("")}</div>
    </div>`).join("")
    || `<p class="topics-empty">今天这个池子是空的 —— 点顶栏那颗「重抓」，或到「情报源」看哪个源没接上。</p>`;

  // 数据驱动的宽度一律走 CSSOM：内联 style 属性会被 CSP 静默忽略
  // （style-src 'self' 且无 'unsafe-inline'），界面不会报错、条永远不显示。
  root.querySelectorAll(".sig-track > i").forEach(i => i.style.setProperty("--w", i.dataset.w || 0));
  root.querySelectorAll("[data-act]").forEach(btn => {
    const key = btn.closest(".page-card")?.dataset.key || "";
    btn.onclick = () => (btn.dataset.act === "ignore" ? doIgnore(key) : doGenerate(key));
  });

  const meta = $("topics-meta");
  if (meta) {
    const on = (data.groups || []).filter(g => g.state === "ok").length;
    const off = (data.groups || []).filter(g => g.state !== "ok").length;
    meta.textContent = `共 ${data.items.length} 条 · ${on} 源已接 · ${off} 源未接`
      + (data.fetched_at ? ` · 上次抓取 ${data.fetched_at.slice(5, 16).replace("T", " ")}` : " · 还没抓过");
  }
  const more = $("btn-more");
  if (more) {
    more.disabled = pages <= 1;
    more.textContent = pages <= 1 ? "就这些" : (page === 0 ? "换一批" : `第 ${page + 1}/${pages} 批`);
  }
  const ign = $("btn-ignored");
  if (ign) {
    ign.textContent = `已忽略 ${data.ignored || 0}`;
    ign.disabled = !data.ignored;
  }
  const cnt = $("topics-count");
  if (cnt) cnt.textContent = String(data.items.length);
}

/** 情报源那张表：`接入` 与 `上次抓取` 两根正交列 + 三带图例（§2.4）。 */
function renderSources() {
  const tbl = $("sources-table");
  if (!tbl || !data) return;
  const groups = data.groups || [];
  const rows = groups.map(g => {
    const state = g.state === "ok"
      ? `<td class="ok">已接入</td>`
      : (g.state === "unwired" ? `<td class="warn">未接入</td>`
        : (g.state === "off" ? `<td class="warn">未启用</td>`
          : `<td class="warn">${esc(g.state)}</td>`));
    const last = g.state === "ok" && g.count
      ? esc((data.fetched_at || "").slice(5, 16).replace("T", " ")) : "—";
    return `<tr>
      <td>${esc(g.id)}<br>${esc(g.platform || "")}</td>
      <td>${esc(g.role || "")}</td>
      <td>${esc(g.cadence || "—")}</td>
      ${state}
      <td>${last}</td>
      <td class="num">${g.count}</td>
      <td>${esc(g.note || "")}</td>
    </tr>`;
  }).join("");
  tbl.innerHTML = `<tr><th>源</th><th>角色</th><th>节奏</th><th>接入</th>
    <th>上次抓取</th><th class="num">今日命中</th><th>说明</th></tr>` + rows;
  const meta = $("sources-meta");
  if (meta) {
    meta.textContent = data.fetched_at ? `最近一次：${data.fetched_at}` : "还没有抓取记录";
  }
}

// ── 动作 ────────────────────────────────────────────────────
/** 等一条后台作业收口。**不复用 jobs.js 的 `poll()`**：那个是"当前生成作业"的
 *  轮询器（带进度条、思考流、取消按钮），情报抓取既没有流也没有步骤树 ——
 *  借它等于把抓取伪装成一次生成，用户会在选题页看到"文案撰写中"。
 *  这里只要三样：状态标签、终态、失败原因。 */
async function awaitJob(id) {
  const st = $("rh-state");
  for (;;) {
    let snap;
    try {
      snap = await api.job(id);
    } catch {
      if (st) { st.textContent = ""; st.className = "rh-state"; }
      return null;
    }
    const live = BUSY_STATES.has(snap.state);
    if (st) {
      st.textContent = live ? (STATE_LABEL[snap.state] || "处理中") : "";
      st.className = "rh-state" + (live ? " warn" : " ok");
    }
    if (!live) {
      if (st) { st.textContent = ""; st.className = "rh-state"; }
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
    for (const g of data.groups || []) g.count = (g.items || []).length;
    renderChips(); render();
    toast("已忽略 —— 只影响今天，明天同题还会回来", 2600);
  } catch (e) {
    toast(`忽略失败：${e.message}`, 3200);
  }
}

/** 「去生成」= 切回会话视图 + 写主题 + 填参数，**不自动发送**（§2.5 定稿）。
 *  参数的真值只在 `state.genParams`（参数胶囊靠 data-key 互相同步），
 *  所以这里必须走 `setGenParam` 而不是直接改某个 select 的值。 */
function doGenerate(key) {
  const hit = visibleItems().find(({ it }) => itemKey(it) === key);
  const it = hit?.it;
  if (!it) return;
  $("topic").value = it.title || "";
  if (it.segment) setGenParam("segment", it.segment);
  refreshGate();
  gotoView("chat");
  $("topic").focus();
  toast("已填好主题与细分领域 —— 确认参数后点发送（不会自动开始）", 3200);
}

// ── 绑定 ────────────────────────────────────────────────────
export function bindTopics() {
  $("btn-topics").onclick = () => openTopics();
  $("btn-more").onclick = () => { page += 1; render(); };
  $("btn-refetch").onclick = async () => {
    try {
      const { job_id } = await api.intelRefresh(packName());
      toast("已开始重抓…", 2200);
      await awaitJob(job_id);
      await load();
    } catch (e) {
      toast(`重抓失败：${e.message}`, 3200);
    }
  };
  $("btn-sources").onclick = () => {
    $("topics-scroll").classList.add("hidden");
    $("topics-src").classList.remove("hidden");
  };
  $("btn-src-close").onclick = () => {
    $("topics-src").classList.add("hidden");
    $("topics-scroll").classList.remove("hidden");
  };
  // 换行业包：分区、chip、计数、未接入全部要跟着换（同一份声明渲染四处）。
  $("pack").addEventListener("change", () => {
    curSrc = "all"; page = 0; kickoffDone = false;
    if ($("right").dataset.view === "topics") load().then(kickoffIfStale);
  });
}

/** 进选题视图。左栏与会话列表**常驻**（不跳页、不开二级窗），只换右栏内容区。 */
export async function openTopics() {
  $("topics-src").classList.add("hidden");
  $("topics-scroll").classList.remove("hidden");
  gotoView("topics");
  // 深链/切屏要滚到顶：`#right` 里几个视图共用同一个滚动上下文，
  // 从结果页切过来时保留着上次的滚动位置（§2.4 那条硬约束）。
  $("topics-scroll").scrollTo(0, 0);
  $("topics-src").scrollTo(0, 0);
  await load();
  kickoffIfStale();
}
