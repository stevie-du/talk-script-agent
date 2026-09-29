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
import { api, POLL_MS, POLL_GRACE_MS } from "./api.js";
import { state, on } from "./store.js";
import { gotoView, refreshGate, setParam } from "./ui.js";
import { setViewHead } from "./head.js";
import { packLabel } from "./sessions.js";
import { BUSY_STATES } from "./progress.js";
import { STATE_LABEL, CADENCE_LABEL } from "./words.js";

/** 一屏放几张卡。「换一批」就是按这个粒度在池子里翻页（§2.5 定的是 4–6 条）。 */
const PAGE_SIZE = 5;

/** 情报源接入态的中文名。后端 `app/intel.py` 的 state 共六值：ok / unwired /
 *  off / error（抓取时抛了异常）/ cooling（连续失败到闸值，今天先跳过）/
 *  orphan（配置里已删、盘上还有旧条目）。
 *  少映射一个，表格里就会直接冒出 error / orphan 这样的英文词。 */
const SOURCE_STATE_LABEL = {
  ok: "已接入",
  unwired: "未接入",
  off: "未启用",
  error: "抓取失败",
  // 「今天先跳过」与「抓取失败」是两句话：前者是引擎**知道**它坏了、这一轮没去试，
  // 后者是试了没成功。合成一个词会让人以为多试几次就有了（§3.1 健壮性那条病灶）。
  cooling: "今日跳过",
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
/** 刚跑完的那次抓取的框架级错误（`_save` 这类）。
 *
 *  它们**只存在于作业结果里**：`_save` 的意思恰恰是"那份 `latest.json` 没写成"，
 *  而 `/api/intel/today` 读的就是那份文件 —— 只从 payload 找，说明行那句
 *  「抓取结果没能存下来」从来没有渲染的可能（第 27 轮契约线 P1：
 *  `awaitJob` 的返回值过去被整条丢弃）。
 *  下一次抓取的作业结果整体覆盖它：存成功的那一刻，这句话就该消失。
 */
let jobErrors = {};

function frameErrors() {
  const e = { ...((data && data.errors) || {}), ...jobErrors };
  return Object.keys(e).filter(k => k.indexOf("_") === 0).map(k => [k, e[k]]);
}
/** 当前筛选的来源（`all` 或某个源的 label）。筛选轴**只有来源这一根**，不混别的维度。 */
let curSrc = "all";
let page = 0;
/** 排行视图的状态。赛道是这一面唯一的筛选轴（§3.5「一根轴不混别的维度」）。 */
let curSeg = "";
let rankPage = 0;
/** 排序轴（§5 #1 拍板"两轴都保留为可切换"）：默认热点分。 */
let rankAxis = "score";
/** 四象限散点（§3.3 #6 散点那一半）开合状态。开合随每次 renderRank 重新落到
 *  按钮与面板上 —— 只在点击处改的话，重渲/重进视图后键上写"收起"、内容却藏着，
 *  与「看条目」那颗按钮踩过的是同一个坑。 */
let quadOpen = false;
/** 展开过详情的事件键。记在模块变量里，换一批/翻页不该把已经看开的行收回去。 */
const openKeys = new Set();
/** 每个事件的选题卡状态：`{running, payload, cached}`。
 *  缓存与过期判定都在后端（§3.4），`cached` 只记"这份是从盘上的缓存水合来的"
 *  （`load()` 里由 `event.brief_card` 填），界面用它区分"我刚点的"与"上午点的"。 */
const briefState = {};
/** 懒触发：本会话只补抓一次，避免每次切回来都发一轮真实网络请求。 */
let kickoffDone = false;

function packName() {
  // 兜底链到 default_pack 为止 —— 硬编码 "elevator" 会让换掉默认包的用户
  // 在下拉还没填充时静默读别的包的情报（P3）。
  return $("pack")?.value || state.meta?.default_pack || "";
}

/** 抓来的 url 只认 http(s)，其余一律当**纯文本**处理。
 *  这一列的每一个字符都来自外网响应，而"能不能点"是导航层的权限：
 *  `javascript:` / `data:` / `file:` 这类形状在这里一个都不该被执行 ——
 *  少给一个可点的标题不损失什么，多给一个执行入口却什么都可能损失。 */
function safeHref(url) {
  const s = String(url || "");
  return /^https?:\/\//i.test(s) ? s : "";
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

/** D / S / E 收成一句小字。算不出印「—」、0 印 0（写成 `v || 0` 就抹平了两种形状）。
 *  机会分不进这一句：它是唯一参与判断的量（判据 `opp >= 60` 在 app/intel.py 的
 *  `_item_flags`，图例写的是同一个数），所以升到底行单独用粗字印。
 *  改前这里是四格「标签 / 轨道 / 值」，两条 237px 的轨道上真实数据只画 33px
 *  （D=0.136 / S=0 / E=null）—— 四格里三格常年是空刻度。 */
function minorSig(sc) {
  const one = (label, v) => `${esc(label)} `
    + ((v === null || v === undefined) ? "—" : esc(fmtPct(v)));
  return [one("需求 D", sc.D), one("供给 S", sc.S), one("事件 E", sc.E)].join(" · ");
}

function card(it) {
  const sc = it.score || {};
  const flags = (it.flags || [])
    .map(([cls, text]) => `<span class="m-chip ${esc(cls)}">${esc(text)}</span>`).join("");
  const flagTxt = (it.flags || []).map(([, text]) => text);
  const tags = [it.segment, it.platform].filter(Boolean)
    .map(t => `<span class="m-chip">${esc(t)}</span>`).join("");
  // 平台真实热度也上卡片（§阶段一 #6「卡片/排行行展示平台真实热度」）。
  // 没有 hot 的源**不发这一颗**：`热度 —` 在卡片上会被读成"量过、是零"，
  // 而排行那一行的分项有轨道当语境，卡片没有。
  const hot = (it.hot === null || it.hot === undefined || it.hot === "")
    ? "" : `<span class="m-chip rank-src">热度 ${esc(fmtHot(it.hot))}</span>`;
  // 条目上不再有「warn 一行红字」这条分支：`it.warn` 引擎**从来不发**
  // （app/intel.py 只在 flags 里发 ["warn", "数据不足 · 算不出机会分"]），
  // 于是它是一条永远走不到的渲染支 —— 而 UI 桩偏偏给它写了值，
  // 门禁一直在那儿演一条不存在的能力（P3-7 / P2-5）。
  // 结论要说话就走 `flags`（卡片顶部那一排 .m-chip），那是引擎真发的通道。
  // 证据行只占一行：有原句印原句，没有印 ev；ev 与右上角那枚标记同名时
  // 不重复印（改前「本周新出」在卡顶标记和证据行各出现一次），退到 URL。
  const src = it.desc || it.ev || "";
  const desc = (src && !flagTxt.includes(src)) ? src : (it.url || "");
  const prov = [it.published, it.url].filter(Boolean).join(" · ");
  const opp = (sc.opportunity === null || sc.opportunity === undefined)
    ? "—" : esc(String(Math.round(sc.opportunity)));
  return `<div class="page-card topic-card" data-key="${esc(itemKey(it))}">
    <div class="res-top"><b class="card-title">${esc(it.title)}</b>${flags}</div>
    <div class="t-desc${/^https?:/.test(desc) ? " url" : ""}" title="${esc(prov)}">${esc(desc)}</div>
    <div class="topic-foot">
      <span class="t-opp">机会分 <b>${opp}</b></span>
      <span class="t-minor">${minorSig(sc)}</span>
      <span class="res-metric-list">${tags}${hot}</span>
      <span class="topics-grow"></span>
      <button class="ghost tiny" data-act="ignore">忽略</button>
      <button class="primary tiny" data-act="gen">去生成</button>
    </div>
  </div>`;
}

// ── 排行视图（§3.2 热点分 + §3.5 三视图）──────────────────────
/** 平台真实热度的短读法（§1.2 #1：hot 抓了没人接 = 用户看到的"热度"是合成分数）。
 *  万/亿两档是中文语境的读法，与百度/微博给的人话字符串同一套单位；
 *  null 一律「—」，**不是 0** —— "这个源不给热度数"与"热度是零"是两种事实。 */
function fmtHot(v) {
  const n = Number(v);
  if (v === null || v === undefined || v === "" || Number.isNaN(n)) return "—";
  if (n >= 100000000) return (n / 100000000).toFixed(1).replace(/\.0$/, "") + " 亿";
  if (n >= 10000) return (n / 10000).toFixed(1).replace(/\.0$/, "") + " 万";
  return String(Math.round(n));
}

/** 四根信号条（名次 / 增速 / 持续 / 来源）。
 *  分项是 0..1 的比值；null 画成「—」并且**轨道不画**（空轨道会被读成"零分"，
 *  而 §3.2 的口径是"该分项不计入"）。总分照常算 —— 那句话的意思是
 *  "缺一项不判死这题"，不是"缺一项就抹成零"。
 *  ⚠ 宽度**不写在 HTML 里**：本页 CSP 是 style-src 'self'（没有 unsafe-inline），
 *  内联 style 属性会被整条丢掉 —— "画了条"与"条真的有长度"于是是两件事，
 *  而不量计算宽度就分不出来。长度走 CSSOM 写 --w（与 `.tempo > i` 同一份手法）。 */
function sigBars(ev) {
  const s = ev.signals || {};
  const one = (label, v) => {
    const has = v !== null && v !== undefined;
    const w = has ? Math.round(Math.max(0, Math.min(1, Number(v))) * 100) : 0;
    return `<span class="sig${has ? "" : " none"}" data-w="${w}" title="${esc(label)}：`
      + (has ? "相对值 " + Number(v).toFixed(2) : "这项没有数据，不计入总分") + '">'
      + `<i class="sig-lbl">${esc(label)}</i>`
      + `<span class="sig-track"><i class="sig-fill"></i></span>`
      + `<em class="sig-val">${has ? w : "—"}</em></span>`;
  };
  return one("名次", s.rank) + one("增速", s.velocity)
    + one("持续", s.persistence) + one("来源", s.diversity);
}

/** 渲染之后走 CSSOM 把长度挂上（理由见 sigBars 的 CSP 那条）。
 *  整屏一次，不在模板串里拼 —— 拼了就等于把这条数据重新塞回会被拦的地方。 */
function syncSigWidths(root) {
  if (!root) return;
  root.querySelectorAll(".sig").forEach(s => {
    const fill = s.querySelector(".sig-fill");
    if (fill) fill.style.setProperty("--w", s.dataset.w || "0");
  });
}

function statusPill(ev) {
  const label = ev.status_label || "";
  if (!label) return "";
  const cls = { breakout: "good", new: "good", persistent: "", fading: "warn" }[ev.status];
  return `<span class="m-chip ${cls || ""} rank-pill">${esc(label)}</span>`;
}

/** 一行排行（§3.5 的格子顺序：排名 / 标题 / 来源数 / 药丸 / 信号条 / 机会标签）。 */
function rankRow(ev, idx) {
  const sc = ev.hot_score;
  const score = (sc === null || sc === undefined) ? "—" : String(sc);
  const tags = [];
  if (ev.featured && ev.quadrant === "高热高配") tags.push(`<span class="m-chip good" title="热点分与本包细分都够 —— 置顶推荐">高热高配</span>`);
  if (ev.underdog) tags.push(`<span class="m-chip good" title="同细分里有低播放的条目也排进了前列 —— 轮得到我">小体量也做成过</span>`);
  if (ev.contested) tags.push(`<span class="m-chip warn">争议话题</span>`);
  if (ev.segment) tags.push(`<span class="m-chip">${esc(ev.segment)}</span>`);
  // 「今天已有选题卡」是**行上**的标记，不只在展开里：一屏五行，用户要的是
  // "哪几行我上午点过了"（点第二次不花钱，但也不该白等几十秒进度）。
  // 判据取两个来源：后端这屏带的 `brief`（重进视图那条路），以及本会话刚生成完、
  // 屏上还没重新 load 的那条。
  if (ev.brief === "fresh" || (briefState[ev.key] || {}).payload) {
    tags.push(`<span class="m-chip good" title="今天已经生成过 —— 再点一次读缓存，不再花模型调用">今天已有选题卡</span>`);
  }
  const opp = (ev.opportunity === null || ev.opportunity === undefined)
    ? "—" : String(Math.round(ev.opportunity));
  const minor = [`热度 ${fmtHot(ev.hot)}`,
    ev.days_on_board > 1 ? `在榜 ${ev.days_on_board} 天` : "",
    ev.first_seen ? `首见 ${String(ev.first_seen).slice(5, 10)}` : "",
    ev.weak_signal ? "信号不足" : ""].filter(Boolean).join(" · ");
  return `<div class="page-card rank-row${ev.featured ? " feat" : ""}" data-key="${esc(ev.key)}">
    <div class="res-top rank-head">
      <span class="rank-no">${idx + 1}</span>
      <b class="card-title">${esc(ev.title)}</b>
      <span class="m-chip rank-src" title="这件事在 ${ev.source_count} 个来源上同时出现">`
      + `${ev.source_count} 源</span>${statusPill(ev)}
      <span class="rank-score">热点分 <b>${score}</b></span>
    </div>
    <div class="rank-signals">${sigBars(ev)}</div>
    <div class="topic-foot">
      <span class="t-opp">机会分 <b>${opp}</b></span>
      <span class="t-minor">${esc(minor)}</span>
      <span class="res-metric-list">${tags.join("")}</span>
      <span class="topics-grow"></span>
      <button class="ghost tiny" data-act="detail">看条目</button>
      <button class="ghost tiny" data-act="ignore">忽略</button>
      <button class="primary tiny" data-act="brief">生成选题卡</button>
    </div>
    <div class="rank-detail hidden">${rankDetail(ev)}</div>
    <div class="rank-brief hidden">${rankBrief(ev)}</div>
  </div>`;
}

/** 事件详情（点行展开）：各源原始条目 + 发布时间（§3.5）。
 *  这一段**永远不依赖选题卡**：没配 Key 的用户展开看到的仍然是完整条目集合 ——
 *  §3.4「排行与条目永不依赖 LLM」这条精神延伸下来的底线。 */
function rankDetail(ev) {
  const rows = (ev.rows || []).map(r => `<div class="rank-item" data-key="${esc(r.key)}">
      <span class="ri-src">${esc(r.source_label || r.platform || "")}</span>
      ${safeHref(r.url)
        ? `<a class="ri-title" href="${esc(safeHref(r.url))}" target="_blank" rel="noreferrer">${esc(r.title)}</a>`
        : `<span class="ri-title">${esc(r.title)}</span>`}
      <span class="ri-meta">${esc([r.published, r.hot ? "热度 " + fmtHot(r.hot) : ""]
        .filter(Boolean).join(" · ")) || "—"}</span>
    </div>`).join("");
  return `<div class="ri-head">这件事的原始条目（${(ev.rows || []).length} 条 · `
    + `${(ev.platforms || []).length} 个来源）</div>${rows
      || `<p class="hint">今天这些条目都被忽略了 —— 到「已忽略」那张清单里恢复。</p>`}`;
}

/** 选题卡区块（§3.4）。三种形态：没有卡（+ 一句能不能生成）、在跑、有卡。 */
function rankBrief(ev) {
  const st = briefState[ev.key];
  if (st && st.running) return `<p class="hint">选题卡生成中…（一次模型调用，几十秒）</p>`;
  const card = st && st.payload;
  if (!card) {
    return `<p class="hint">${ev.brief === "stale"
      ? "上一次的选题卡已过期（超过 3 天视为未生成）—— 点「生成选题卡」重做一份。"
      : "还没有这张卡的选题角度。点「生成选题卡」让模型按这几个角度起草——"
        + "这一步要花一次模型调用，没配模型时点不了。"}</p>`;
  }
  const angles = (card.angles || []).map((a, i) => `<div class="brief-angle">
      <div class="ba-head"><b>${esc(a.type || "角度")}</b>
        <span class="hint">${esc(a.stage_hint || "")}</span></div>
      <div class="ba-titles">
        <div><i class="ba-kind">跟热点</i>${(a.titles && a.titles.trend || []).map(t => `<p>${esc(t)}</p>`).join("")}</div>
        <div><i class="ba-kind">长尾</i>${(a.titles && a.titles.evergreen || []).map(t => `<p>${esc(t)}</p>`).join("")}</div>
      </div>
      <p class="ba-hook">${esc((a.hook && a.hook.type) || "")}｜${esc((a.hook && a.hook.line) || "")}</p>
      <p class="ba-meta">${esc([a.audience, (a.keywords || []).join("、"),
        a.risk && a.risk !== "无" ? "风险：" + a.risk : ""].filter(Boolean).join(" · "))}</p>
      <button class="ghost tiny" data-act="use" data-i="${i}">用这个角度</button>
    </div>`).join("");
  return `<div class="brief-card">
    <div class="brief-head"><b>选题卡</b><span class="hint">${esc(stageLabel(card.stage))}</span></div>
    ${angles || `<p class="hint">这张卡没有给出可用的角度 —— 可以重做一份。</p>`}
    <p class="hint brief-caveat">基于热榜标题与摘要生成，事实以你核实为准。</p>
  </div>`;
}

const STAGE_TEXT = { rising: "上升期：抢先深度解读", peak: "爆发期：快速差异化",
  fading: "衰退期：盘点收长尾" };
function stageLabel(s) { return STAGE_TEXT[s] || ""; }

/** 「用这个角度」（§3.4 最后一条）：只回填**文本**（标题 + 钩子句）到输入框。
 *  angle/hook 不升成正式参数（§5 #5 建议的就是"永远只回填输入框"，本轮按它落），
 *  所以参数面板一行都不动 —— 动了反而让同一次点击在界面上有两处不可预期的变化。 */
function useAngle(key, i) {
  const card = (briefState[key] || {}).payload;
  const a = card && (card.angles || [])[i];
  if (!a) return;
  const title = ((a.titles && (a.titles.trend || [])[0]) || a.hook?.line || "").trim();
  if (!title) { toast("这个角度没有可回填的标题", 2600); return; }
  const hook = ((a.hook && a.hook.line) || "").trim();
  $("topic").value = hook ? `${title}\n\n${hook}` : title;
  gotoView("chat");
  $("topic").focus();
  toast("已填好标题与钩子句 —— 确认参数后点发送（不会自动开始）", 3200);
}

/** 赛道 chip（排行视图唯一的筛选轴，§3.5「一根轴不混别的维度」）。
 *  来源数那颗徽章**不可点** —— 它是证据不是筛选项，点它筛出来的东西
 *  与「按来源」那一面重复，两根轴就在同一个视图里打架了。 */
function renderSegChips() {
  const box = $("seg-chips");
  if (!box || !data) return;
  const segs = [];
  for (const ev of data.events || []) {
    const s = ev.segment || "";
    if (!segs.includes(s)) segs.push(s);
  }
  const rows = data.events || [];
  // 选中的赛道在这次的事件集合里没有了（重抓之后那条今天一条条目都不剩）：归回「全部赛道」。
  // 留着它 = 屏上没有一颗 chip 是选中态、排行面一片空 + 一句"换一个 chip"，
  // 而唯一的出路那颗就叫「全部赛道」—— 用户得自己想到它是出口。
  if (curSeg && !rows.some(e => (e.segment || "") === curSeg)) curSeg = "";
  const pill = (seg, label, n) =>
    `<button type="button" aria-pressed="${curSeg === seg ? "true" : "false"}"`
    + ` class="src-pill${curSeg === seg ? " on" : ""}" data-seg="${esc(seg)}">`
    + `${esc(label)} ${n}</button>`;
  const chips = [pill("", "全部赛道", rows.length)]
    .concat(segs.filter(Boolean).map(s =>
      pill(s, s, rows.filter(e => e.segment === s).length)));
  const un = rows.filter(e => !e.segment).length;
  box.innerHTML = chips.join("")
    // 「没归上赛道」单独说明而不是伪装成一个赛道 chip：那是"归属失败"，
    // 与"某个领域"是两种事实，混在一排里会让人以为引擎认出了这么一条赛道。
    + (un ? `<span class="topics-grow"></span>`
      + `<span class="src-note" title="这些事件没归上本包的任何细分领域（词表没覆盖到，不是 bug）">`
      + `未归属 ${un}</span>` : "");
  wireSegChips();
}

/** 只改"哪一颗是选中的"，**不重建**那些按钮 —— 与 `syncChips()` 同一条理由：
 *  整行重建会把 pressed 状态留在一个已经被摘掉的节点上（读屏念出假状态），
 *  还会把键盘焦点从刚按下的那颗摘走。重建只在事件集合本身变了的时候做。 */
function syncSegChips() {
  const box = $("seg-chips");
  if (!box) return;
  box.querySelectorAll("[data-seg]").forEach(b => {
    const on = b.dataset.seg === curSeg;
    b.setAttribute("aria-pressed", on ? "true" : "false");
    b.classList.toggle("on", on);
  });
}

function wireSegChips() {
  const box = $("seg-chips");
  if (!box) return;
  box.querySelectorAll("[data-seg]").forEach(b => {
    b.onclick = () => { curSeg = b.dataset.seg; rankPage = 0; syncSegChips(); renderRank(); };
  });
}

function rankEvents() {
  const rows = (data && data.events) || [];
  const pool = curSeg ? rows.filter(e => (e.segment || "") === curSeg) : rows;
  // 排序轴可切（§5 #1：两轴都保留）。**默认热点分**：这一屏回答的是"今天什么在热"，
  // 而机会分回答的是"值不值得追"，后者在按来源那一面的卡片上已经有位置。
  // 算不出的排最后（与后端同一条 null 语义），同分按标题字典序 —— 不许随点击抖动。
  const keyed = pool.map(e => [e, rankAxis === "opp"
    ? (e.opportunity === null || e.opportunity === undefined ? null : e.opportunity)
    : (e.hot_score === null || e.hot_score === undefined ? null : e.hot_score)]);
  keyed.sort((a, b) => {
    // 沉底的排最后 —— **这一条必须在最前面**，它压过排序轴与字典序。
    // 后端 `today()` 已经把 buried 排到尾并写明"顺序只让后端排一次"，但排行面
    // 自己还要按热点分/机会分重排（轴可切），不接这一条就等于：连续忽略满 3 天
    // 的事件在「按来源」那一面沉下去了，在**默认的**排行面仍然稳在榜首。
    if (Boolean(a[0].buried) !== Boolean(b[0].buried)) return a[0].buried ? 1 : -1;
    if ((a[1] === null) !== (b[1] === null)) return a[1] === null ? 1 : -1;
    if (a[1] !== null && b[1] !== null && a[1] !== b[1]) return b[1] - a[1];
    if (String(a[0].status || "") !== String(b[0].status || "")) {
      return String(a[0].status || "") < String(b[0].status || "") ? -1 : 1;
    }
    return String(a[0].title || "") < String(b[0].title || "") ? -1
      : String(a[0].title || "") > String(b[0].title || "") ? 1 : 0;
  });
  return keyed.map(x => x[0]);
}

function renderRank() {
  const root = $("rank-root");
  if (!root || !data) return;
  const all = rankEvents();
  const pages = Math.max(1, Math.ceil(all.length / PAGE_SIZE));
  if (rankPage >= pages) rankPage = 0;
  const slice = all.slice(rankPage * PAGE_SIZE, (rankPage + 1) * PAGE_SIZE);
  const weak = all.filter(e => e.weak_signal).length;
  root.innerHTML = slice.map((ev, i) => rankRow(ev, rankPage * PAGE_SIZE + i)).join("")
    || `<p class="topics-empty">${data.events && data.events.length
      ? "这个赛道今天没有条目 —— 换一个 chip，或点顶栏那颗「重抓」。"
      : "还没有抓到任何热榜事件 —— 点顶栏那颗「重抓」，或到「情报源」看哪个源没接上。"}</p>`;
  const note = $("rank-note");
  if (note) {
    // 框架级失败在**两个视图都要说**：它说的是"这份抓取记录坏了"，
    // 与用户此刻看的是排行还是卡片无关。原来只写在 #topics-note，
    // 于是排行视图里 _read/_save 那句话永远不会出现。
    note.textContent = frameErrors().map(([k, v]) =>
      (k === "_save" ? "抓取结果没能存下来：" : "抓取记录读不出来：") + v).join("；");
    note.className = "hint" + (note.textContent ? " cfg-err" : "");
  }
  const meta = $("rank-meta");
  if (meta) {
    meta.textContent = `${all.length} 个事件`
      + (rankAxis === "opp" ? " · 按机会分排" : " · 按热点分排")
      + (weak ? ` · ${weak} 个信号不足` : "")
      + (data.fetched_at ? ` · 上次抓取 ${fmtFetched(data.fetched_at)}` : " · 还没抓过");
  }
  const more = $("btn-rank-more");
  if (more) {
    more.disabled = pages <= 1;
    more.textContent = pages <= 1 ? "就这些" : (rankPage === 0 ? "换一批" : `第 ${rankPage + 1}/${pages} 批`);
  }
  const order = $("btn-rank-order");
  if (order) {
    order.textContent = rankAxis === "opp" ? "按机会分" : "按热点分";
    order.setAttribute("aria-pressed", rankAxis === "score" ? "true" : "false");
  }
  // 四象限开合只有一处真相（quadOpen），每次重渲都把按钮与面板拨到它上面。
  const qbtn = $("btn-quad");
  const qpanel = $("quad-panel");
  if (qbtn) qbtn.setAttribute("aria-expanded", quadOpen ? "true" : "false");
  if (qpanel) {
    qpanel.classList.toggle("hidden", !quadOpen);
    if (quadOpen) renderQuad(all, qpanel);
  }
  wireRank();
  syncSigWidths(root);
}

/** 四象限散点（§3.3 #6 散点那一半）：横轴热点分，上带 = 归得上本包细分、
 *  下带 = 未归属 —— 纵轴就是分档用的那个**布尔**，画成两根横带，不假装有
 *  一根 0-100 的相关度轴（那是分档落码时明说不做的假精度）。
 *  「高热」档线的位置用**后端下发的** `hot_quadrant_high`，前端不抄常数；
 *  算不出热点分的不进图 —— 画进去就是把「—」冒充 0 分。
 *  ⚠ 点位与档线的坐标**不写在 HTML 里**（CSP `style-src 'self'` 会把内联
 *  style 整条丢掉，信号条踩过同一道坑）：渲染后走 CSSOM 写 `--x` / `--jy`。 */
function renderQuad(all, panel) {
  if (!panel) return;
  const th = data ? data.hot_quadrant_high : undefined;
  const scored = all.filter(e => e.hot_score !== null && e.hot_score !== undefined);
  const noScore = all.length - scored.length;
  let hi = 0, lo = 0;
  const dots = scored.map(e => {
    const band = e.segment ? "hi" : "lo";
    const j = (band === "hi" ? hi++ : lo++) % 3 - 1;
    const tip = `${e.title} · ${e.quadrant || ""} · 热点分 ${e.hot_score}`;
    return `<button type="button" class="quad-dot${e.featured ? " feat" : ""}"`
      + ` data-key="${esc(e.key)}" data-band="${band}" data-j="${j}"`
      + ` data-score="${e.hot_score}" title="${esc(tip)}" aria-label="${esc(tip)}"></button>`;
  }).join("");
  panel.innerHTML = `<div class="quad-plot" role="img"
      aria-label="四象限分布：横轴热点分 0 到 100，上带是归得上本包细分的事件，下带是未归属的">
      <i class="quad-band hi"></i><i class="quad-band lo"></i>
      <i class="quad-band-lbl t">归得上本包细分</i><i class="quad-band-lbl b">未归属 / 别的赛道</i>
      ${(th === null || th === undefined) ? ""
        : `<i class="quad-th" data-th="${th}" title="热点分 ≥ ${th} 记高热（与机会分同一档常数）"></i>`}
      ${dots}
      <span class="quad-x0">0</span><span class="quad-x100">100</span>
    </div>
    <p class="hint quad-cap">${noScore ? `另有 ${noScore} 个事件算不出热点分，不进图 —— 「—」不是 0。` : ""}</p>`;
  syncQuadDots(panel);
  panel.querySelectorAll(".quad-dot").forEach(d => {
    d.onclick = () => showEventFromQuad(d.dataset.key);
  });
}

/** 渲染之后走 CSSOM 把坐标挂上（理由见 renderQuad 的 CSP 那条）。
 *  横轴留 2% 内边距防边缘裁切：0 分与满分那颗点不被裁掉一半，
 *  判据量的是 getBoundingClientRect，坐标错了一眼就看得见。 */
function syncQuadDots(panel) {
  panel.querySelectorAll(".quad-dot").forEach(d => {
    const s = Math.max(0, Math.min(100, Number(d.dataset.score) || 0));
    d.style.setProperty("--x", String(s));
    d.style.setProperty("--jy", String(Number(d.dataset.j) || 0));
  });
  const th = panel.querySelector(".quad-th");
  if (th) th.style.setProperty("--x", String(Math.max(0, Math.min(100, Number(th.dataset.th) || 0))));
}

/** 点图上那颗点 → 定位到排行里那一行：目标行在另一页时**先翻页**
 * （PAGE_SIZE=5，事件一多行就不在当前页）；找不到（赛道切换后键没了）就安静返回。 */
function showEventFromQuad(key) {
  const all = rankEvents();
  const idx = all.findIndex(e => e.key === key);
  if (idx < 0) return;
  rankPage = Math.floor(idx / PAGE_SIZE);
  renderRank();
  requestAnimationFrame(() => {
    const row = document.querySelector(`#rank-root .rank-row[data-key="${key}"]`);
    if (!row) return;
    row.scrollIntoView({ behavior: "smooth", block: "center" });
    row.classList.remove("flash");
    void row.offsetWidth;
    row.classList.add("flash");
  });
}

/** 排行行的三个动作 + 「看条目」的展开。整块在重渲染后要重新挂，
 *  但展开状态记在模块变量里（`openKeys`）—— 翻页/换一批不该把已经看开的详情收掉。 */
function wireRank() {
  const root = $("rank-root");
  if (!root) return;
  root.querySelectorAll(".rank-row").forEach(row => {
    const key = row.dataset.key || "";
    if (openKeys.has(key)) {
      row.querySelector(".rank-detail")?.classList.remove("hidden");
      row.querySelector(".rank-brief")?.classList.remove("hidden");
      // 按钮的字要跟着屏上的开合走：重渲（换一批 / 切赛道 / 忽略之后的整屏重画 / 重新 load）
      // 只把 hidden 去掉，label 还停在「看条目」—— 同一行两个说法：内容是开着的，
      // 键上说"还没看过"。与「已忽略 N」那一行同族（界面读数停在旧状态）。
      const d = row.querySelector('[data-act="detail"]');
      if (d) d.textContent = "收起";
    }
    row.querySelectorAll("[data-act]").forEach(btn => {
      const act = btn.dataset.act;
      btn.onclick = () => {
        if (act === "detail") toggleDetail(row, key, btn);
        else if (act === "ignore") doIgnore(key);
        else if (act === "brief") doBrief(key);
        else if (act === "use") useAngle(key, Number(btn.dataset.i || 0));
      };
    });
  });
}

function toggleDetail(row, key, btn) {
  const open = !row.querySelector(".rank-detail")?.classList.contains("hidden");
  if (open) {
    row.querySelector(".rank-detail")?.classList.add("hidden");
    row.querySelector(".rank-brief")?.classList.add("hidden");
    openKeys.delete(key);
    if (btn) btn.textContent = "看条目";
    return;
  }
  row.querySelector(".rank-detail")?.classList.remove("hidden");
  const bc = row.querySelector(".rank-brief");
  if (bc && (briefState[key] || {}).payload) bc.classList.remove("hidden");
  openKeys.add(key);
  if (btn) btn.textContent = "收起";
}

/** 已忽略清单（§3.5）：点开是一张能恢复的表，不是一句 toast。 */
function renderIgnored() {
  const box = $("topics-ignored");
  if (!box || !data) return;
  const rows = (data.ignored_list || []);
  const todayN = (data.ignored_today) || 0;
  if (!box.classList.contains("hidden")) {
    box.innerHTML = rows.length
      // 「已忽略 N」这句的 N 在按钮上是**今天**的条数，在清单里是**在册**的条数 ——
      // 两个数不同要各说各的话，否则按钮写 0、清单写 3，用户只能得出"这数在撒谎"。
      ? `<div class="ig-head">${todayN ? "已忽略" : "今天没新忽略，之前点下的有"} ${rows.length} 条 —— `
        + `连续忽略满 3 天的会沉到池底，`
        + `但「换一批」仍然翻得出来；点「恢复」立刻回池。</div>`
        + rows.map(r => `<div class="ig-row">
            <span class="ig-title">${esc(r.title)}</span>
            <span class="hint">${esc([r.source_label, r.segment,
              `${r.days.length} 天` + (r.buried ? " · 已沉底" : "")]
              .filter(Boolean).join(" · "))}</span>
            <span class="topics-grow"></span>
            <button class="ghost tiny" data-unignore="${esc(r.key)}">恢复</button>
          </div>`).join("")
      : `<div class="ig-head">今天没有忽略过任何条目。</div>`;
    box.querySelectorAll("[data-unignore]").forEach(b => {
      b.onclick = () => doUnignore(b.dataset.unignore);
    });
  }
}

async function doUnignore(key) {
  try {
    const r = await api.intelUnignore(packName(), key);
    data.ignored = r.ignored;
    data.ignored_today = typeof r.ignored_today === "number"
      ? r.ignored_today : data.ignored_today;
    await load();
    renderIgnored();
    toast("已恢复 —— 这条重新回到今天的池子里", 2600);
  } catch (e) {
    toast(`恢复失败：${e.message}`, 3200);
  }
}

async function doBrief(key) {
  const st = briefState[key] || (briefState[key] = {});
  if (st.running) return;
  st.running = true;
  refreshBrief(key);
  try {
    const r = await api.intelBrief(packName(), key);
    if (r.state === "cached") {
      st.payload = r.brief;
      st.running = false;
      refreshBrief(key);
      toast("今天已经生成过这张卡 —— 直接用缓存，没有再花一次模型调用", 3600);
      return;
    }
    if (r.reused) {
      // 同一条事件已经在飞了（换了窗口 / 重启后再点一次）。后端这次**没有**再发
      // 一次模型调用，而是让我们搭上那一条 —— 这句话要在**点下去的那一刻**说：
      // 等几十秒之后才讲"没花钱"，用户中途看到的仍然是一个不知道扣没扣费的进度条。
      toast("这件事本来就在生成中 —— 搭上那一条，没有再花一次模型调用", 4200);
    }
    const snap = await awaitJob(r.job_id);
    st.running = false;
    if (snap && snap.state === "done") {
      st.payload = snap.result;
      toast("选题卡已生成", 2400);
    } else if (snap && snap.state === "failed") {
      toast(`选题卡没做成：${snap.error || "模型调用失败"} —— 事件条目照常可看`, 4200);
    } else {
      toast("选题卡状态未知，请稍后重试（事件条目不受影响）", 3600);
    }
    refreshBrief(key);
  } catch (e) {
    st.running = false;
    // 没配模型 / 额度已满 / 事件不在今天的榜上：三句话各自指向不同的下一步，
    // 都演成"生成失败"会让人去重点一次（然后真的扣一次费）。
    toast(`生成选题卡失败：${e.message} —— 没配模型时先去「设置 → 模型接口」，`
      + "事件条目不受影响", 4600);
    refreshBrief(key);
  }
}

/** 只重画那一张卡的区块，不重渲整屏 —— 重渲会把用户已经展开的其它行一起打回。 */
function refreshBrief(key) {
  const row = ($("rank-root") || {}).querySelector?.(`.rank-row[data-key="${CSS.escape(key)}"]`);
  if (!row) return;
  const ev = ((data && data.events) || []).find(e => e.key === key);
  if (!ev) return;
  const box = row.querySelector(".rank-brief");
  if (!box) return;
  box.innerHTML = rankBrief(ev);
  box.querySelectorAll("[data-act]").forEach(btn => {
    btn.onclick = () => useAngle(key, Number(btn.dataset.i || 0));
  });
  if ((briefState[key] || {}).payload) box.classList.remove("hidden");
  row.querySelector(".rank-detail")?.classList.remove("hidden");
  openKeys.add(key);
}

// ── 按来源视图：筛选行 ──────────────────────────────────────
/** 筛选行。**只有可筛的那几颗是按钮**（`.src-pill`，文字 tab：透明底、
 *  选中给一层 `--fill`）；「已接入今天没货」「抓取失败」「未接入 N」这三类
 *  只做解释，一律 `.src-note`（无底纯字）并推到行尾。
 *  改前两类混用 `.m-chip`（5% 灰底 + 深色粗字）—— 与"选中那颗"同手法，
 *  一行里"能不能点"读不出来；而 `.m-chip` 本身是「有底色小标记」一族的成员，
 *  不能在这里给它脱底。 */
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
  const notes = [];
  // 「已接入今天没货」与「未接入」长得不一样是刻意的：
  // 前者是"今天没有"，后者是"我们没接"。合成一个说法会让用户以为平台坏了。
  // ⚠ 但**抓取失败的源不许算进"今天没货"**：`today()` 给失败源的 state 就是 "ok"
  //   + count 0，与"平台今天没有"完全同形 —— 这个文件 41-49 行把"必须认 errors"
  //   写成铁律，而这一行原来漏了：胶囊行说「抖音热榜 0 · 已接入，今天没有命中」，
  //   同一屏的计数行与源表却说「3 源抓取失败」。两句话只能有一句是对的，
  //   而用户按胶囊行那句会去**等平台**，实际该去做 API Key。
  const idle = groups.filter(g => g.state === "ok" && g.count === 0 && !sourceError(g.label));
  notes.push(...idle.map(g =>
    `<span class="src-note" title="已接入，今天没有命中">${esc(g.label)} 0</span>`));
  // 失败源：不进可筛态（点它筛出一片空），但必须在这一行说话 —— 措辞与源表一致。
  const failed = groups.filter(g => g.state === "ok" && !!sourceError(g.label));
  notes.push(...failed.map(g =>
    `<span class="src-note bad" title="${esc(sourceError(g.label))}">${esc(g.label)} 抓取失败</span>`));
  // 「今日跳过」是第三种形态（§3.1）：既不是"平台没货"也不是"刚才试了一下没成"，
  // 而是**引擎知道它连着挂了三次、这一轮没去试**。三种说法的下一步动作各不相同，
  // 所以不许并进上面任何一档。
  const cooling = groups.filter(g => g.state === "cooling");
  notes.push(...cooling.map(g =>
    `<span class="src-note bad" title="${esc(g.error || "")}">${esc(g.label)} 今日跳过</span>`));
  const off = groups.filter(g => g.state === "off" || g.state === "unwired");
  if (off.length) {
    // 「未接入」是**决定**（小红书不做 x-s 逆向）而不是"今天没货" ——
    // 所以它带一句为什么，且不进可筛态（点它不会筛出一片空）。
    const why = off.map(g => `${g.label}：${g.note || (g.state === "off" ? "未启用" : "引擎没有对应适配器")}`);
    notes.push(`<span class="src-note" title="${esc(why.join("；"))}">未接入 ${off.length}</span>`);
  }
  // 说明性那几颗推到行尾（`.topics-grow` 是现成的占位，与卡脚同一族）。
  box.innerHTML = chips.join("")
    + (notes.length ? `<span class="topics-grow"></span>${notes.join("")}` : "");
  syncChips();
  box.querySelectorAll("[data-src]").forEach(b => {
    b.onclick = () => { curSrc = b.dataset.src; page = 0; syncChips(); render(); };
  });
}

/** 只改"哪一颗是选中的"，**不重建**那些按钮。
 *
 *  为什么单独拆出来（第 28 轮无障碍线 P1）：`aria-pressed` 与 `.on` 是烘焙在
 *  `renderChips()` 的字符串里的，而点一颗胶囊只调 `render()` —— 于是卡区换了、
 *  胶囊行还停在上一颗：视觉上高亮错位，读屏念出来的是假状态（pressed=true 的
 *  是「全部」，实际筛的是「抖音热榜」）。走整行重建则会把键盘焦点从刚按下的那颗
 *  摘掉，所以这里是**原地改属性**。
 */
function syncChips() {
  const box = $("src-chips");
  if (!box) return;
  box.querySelectorAll("[data-src]").forEach(b => {
    const on = b.dataset.src === curSrc;
    b.setAttribute("aria-pressed", on ? "true" : "false");
    b.classList.toggle("on", on);
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
    curSrc = "all"; page = 0; syncChips();
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

  // （原先这里还有一行给 `.sig-track > i` 走 CSSOM 写 `--w`：四格信号条已撤，
  //   全项目不再产出那根轨道，"数据驱动宽度必须走 CSSOM"那条约定由
  //   节奏条 `.tempo > i` 继续守着。）
  root.querySelectorAll("[data-act]").forEach(btn => {
    const key = btn.closest(".page-card")?.dataset.key || "";
    btn.onclick = () => (btn.dataset.act === "ignore" ? doIgnore(key) : doGenerate(key));
  });

  const meta = $("topics-meta");
  if (meta) {
    // 抓取失败的源**不计入「已接」**：照 `g.state` 数会把"我们没抓到"报成
    // "已接入"，而用户看这行字就是为了判断"今天的数据靠不靠得住"。
    // 「今日跳过」同样不算已接：那一行说的是"今天这份数据全不全"。
    const on = (data.groups || []).filter(g => g.state === "ok" && !sourceError(g.label)).length;
    const bad = (data.groups || []).filter(g => sourceError(g.label)).length;
    const skip = (data.groups || []).filter(g => g.state === "cooling").length;
    // 「未接」只数**没接与关了**那两态：把"今天被跳过"算进未接，
    // 等于告诉用户这个平台根本没接（而那正是"要不要去配 Key"的判断依据）。
    const off = (data.groups || []).filter(g => g.state !== "ok" && g.state !== "cooling").length;
    meta.textContent = `共 ${data.items.length} 条 · ${on} 源已接`
      + (bad ? ` · ${bad} 源抓取失败` : "")
      + (skip ? ` · ${skip} 源今日跳过` : "")
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
    // 禁用条件认的是**清单里有没有东西**，不是"今天点过几次"：昨天与前天点下的
    // 仍然在册、仍然要一个恢复入口（连续忽略满 3 天会沉底，那是会自己生效的账），
    // 而今天一次都没点 —— 原写法恰好在这张清单最该打开的时候把它锁上了。
    ign.disabled = !((data && data.ignored_list || []).length);
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
    // 三态要分得开：抓取失败（试过没成）/ 今日跳过（知道它坏了，没试）/ 未接入（没适配器）。
    // 后端把跳过的原因写在 `g.error`（不是 errors 表 —— errors 说的是"这一轮试了"）。
    const st = err ? "抓取失败" : (SOURCE_STATE_LABEL[g.state] || g.state);
    const state = `<td class="${g.state === "ok" && !err ? "ok" : "warn"}">${esc(st)}</td>`;
    // 健康列（§3.1）：近 7 天的 成功/失败 与命中数。它回答的是"今天少看了哪几个榜"
    // —— 光看今天那一行，一个挂了五天的源与"今天刚抖一下"是同一句话。
    const h = g.health || {};
    // 「近 7 天」在还没有任何抓取记录时是**不知道**，不是"0 成 0 败"：
    // 新装用户的第一屏每一行都会印 0/0，而这一列回答的是"这周试过几次、成了几次"。
    // 0/0 读起来像"试了一周全没命中"，与「—」（还没试过）是两种事实 ——
    // 本仓的规矩一直是 `null` 画「—」、`0` 才是结论。
    const tried = (h.ok || 0) + (h.fail || 0);
    const health = `<td class="num${h.fail ? " warn" : ""}">`
      + (tried ? `${h.ok || 0}/${tried}` : "—") + `</td>`;
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
      ${health}
      <td>${last}</td>
      <td class="num">${g.count}</td>
      <td>${esc(err || g.error || g.note || "")}</td>
    </tr>`;
  }).join("");
  tbl.innerHTML = `<tr><th>源</th><th>角色</th><th>节奏</th><th>接入</th>
    <th class="num">近 7 天</th><th>上次抓取</th><th class="num">今日命中</th><th>说明</th></tr>` + rows;
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
  // 客户端截止：宽于后端作业预算一档（两个数都在 api.js，见 POLL_GRACE_MS）。
  // 没有上限时，引擎"作业丢了但没报错"（进程被杀后重启、作业表被
  // 清）会让这个循环永远转下去，用户看到「抓取中」常驻到天荒地老。
  const t0 = Date.now();
  for (;;) {
    if (Date.now() - t0 > POLL_GRACE_MS) {
      say("");
      toast(`抓取状态未知（已等待 ${Math.round(POLL_GRACE_MS / 60000)} 分钟），请手动刷新或重试`, 5000);
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
    await sleep(POLL_MS);
  }
}

async function load() {
  try {
    data = await api.intelToday(packName());
  } catch (e) {
    // 只读端点**空或坏返回空结构、不抛**；真抛出来只可能是引擎没起来/令牌失效，
    // 那是另一个层面的问题（与"今天没抓到"不同），如实说。
    toast(`读取今日选题失败：${e.message}`, 3200);
    data = { pack: packName(), fetched_at: "", groups: [], items: [], events: [],
            errors: {}, stale: false, ignored: 0, ignored_today: 0,
            ignored_list: [], unwired: 0 };
  }
  // 后端没给 events（旧引擎 / 旧产物 / 还没抓过的新格式）时排行视图也要有东西可看：
  // 退成"每个条目一条"，并把分项显示成「—」而不是 0 —— 与后端 `today()`
  // 对旧 latest.json 的降级同一个口径。判据写的是「数组为空」而不只是「不是数组」：
  // 旧引擎会带一个**空数组**回来，只判 Array.isArray 就等于这条降级永远不触发。
  if (data.items && data.items.length && !(data.events && data.events.length)) {
    data.events = (data.items || []).map(it => ({
      key: "ev:" + String(it.key || it.title || ""), title: it.title || "",
      segment: it.segment || "", platforms: [it.source_label].filter(Boolean),
      source_count: it.source_label ? 1 : 0, item_keys: [it.key],
      rows: [{ key: it.key, title: it.title, source_label: it.source_label,
               hot: it.hot, published: it.published, url: it.url, desc: it.desc,
               opportunity: (it.score || {}).opportunity }],
      hot: it.hot, signals: { rank: null, velocity: null, persistence: null,
                              diversity: null },
      hot_score: null, weak_signal: true, status_label: "", brief: "absent",
      opportunity: (it.score || {}).opportunity, days_on_board: 0, first_seen: "",
    }));
  }
  // 盘上今天的选题卡跟着这一屏一起下来（`event.brief_card`）：不水合的话，
  // 切走再切回卡片就凭空消失，文案退成「还没有这张卡的选题角度」——
  // 而用户真去点那颗按钮，服务端回的是 `cached`（免费的、内容就在盘上）。
  // 同一件事在屏上两句话，而且后一句是错的。
  for (const ev of data.events || []) {
    if (ev.brief_card && !(briefState[ev.key] || {}).payload) {
      briefState[ev.key] = { payload: ev.brief_card, cached: true };
    }
  }
  renderChips();
  renderSegChips();
  render();
  renderRank();
  renderSources();
  renderIgnored();
  return data;
}

/** 懒触发：`stale` 时补一发重抓（**本会话只补一次**）。 */
async function kickoffIfStale() {
  if (kickoffDone || !data?.stale) return;
  kickoffDone = true;
  try {
    const { job_id } = await api.intelRefresh(packName());
    toast("正在后台抓取情报…", 2400);
    const snap = await awaitJob(job_id);
    jobErrors = (snap && snap.result && snap.result.errors) || {};
    await load();
  } catch (e) {
    // 重抓失败**不影响已经显示的内容** —— 抓取器在生成主链路之外，
    // 失败只意味着"这次没拿到新的"，旧数据照常可看。
    toast(`重抓失败：${e.message}（已显示上次抓到的内容）`, 3600);
  }
}

async function doIgnore(key) {
  // 排行那一面点的是**事件**（键形如 `ev:…`），而忽略记录按条目键存 ——
  // 一个事件在三个平台上同时在榜，只忽略其中一条等于"忽略了但明天还看见它"。
  // 所以这里把事件展开成它的成员条目键，逐条记。判定用后端下发的 item.key，
  // 前端不自己拼（那是第 27 轮契约线抓过的"一库两键"）。
  const ev = ((data && data.events) || []).find(e => e.key === key);
  const keys = ev ? (ev.item_keys || []).filter(Boolean) : [key];
  if (!keys.length) return;
  try {
    let r = null;
    for (const k of keys) r = await api.intelIgnore(packName(), k);
    if (!r) return;
    const dropped = new Set(keys);
    // 本地也摘掉：等服务端回一轮再重渲染会有一次肉眼可见的跳动。
    for (const g of data.groups || []) g.items = (g.items || []).filter(it => !dropped.has(itemKey(it)));
    data.items = (data.items || []).filter(it => !dropped.has(itemKey(it)));
    data.events = (data.events || []).filter(e => e.key !== key);
    data.ignored = r.ignored;
    // 服务端回的两个数都要落，否则本地那一行会停在旧值上（重渲染才纠正，
    // 而"点了一下、数字没动"正是这条路径上唯一的用户反馈）。
    data.ignored_today = typeof r.ignored_today === "number"
      ? r.ignored_today : (data.ignored_today || 0) + keys.length;
    for (const g of data.groups || []) g.count = (g.items || []).length;
    renderChips(); renderSegChips(); render(); renderRank(); renderIgnored();
    toast(ev && keys.length > 1
      ? `已忽略这件事（${keys.length} 条来源）—— 只影响今天，明天同题还会回来`
      : "已忽略 —— 只影响今天，明天同题还会回来", 3200);
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
  $("btn-rank-more").onclick = () => { rankPage += 1; renderRank(); };
  // 排序轴切换（§5 #1：热点分与机会分两轴都保留）。pressed 状态**原地改属性**，
  // 与筛选胶囊同一条规矩（第 28 轮无障碍线 P1：整块重绘会把键盘焦点摘掉）。
  $("btn-rank-order").onclick = () => {
    rankAxis = rankAxis === "score" ? "opp" : "score";
    rankPage = 0;
    renderRank();
  };
  // 四象限散点（§3.3 #6）：只翻 quadOpen 这一个开关，开合与重渲的落位
  // 全在 renderRank 里做（那里是唯一能同时看到按钮、面板与数据的地方）。
  $("btn-quad").onclick = () => { quadOpen = !quadOpen; renderRank(); };
  // 「已忽略 N」点开是**一张能恢复的清单**（§3.5）。以前它只 toast 一句话 ——
  // 那等于告诉用户"有些东西被你藏起来了，但没有任何地方能看到它们"。
  $("btn-ignored").onclick = () => {
    const box = $("topics-ignored");
    const open = box.classList.contains("hidden");
    box.classList.toggle("hidden", !open);
    $("btn-ignored").setAttribute("aria-expanded", open ? "true" : "false");
    renderIgnored();
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
      const snap = await awaitJob(job_id);
      jobErrors = (snap && snap.result && snap.result.errors) || {};
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
  // 「排行 / 按来源 / 情报源」三颗页签：aria、roving tabindex 与 ←/→ Home End
  // 全在 util.wireTablist 里（与结果卡的产物分区同一份实现）。
  selectTopicsTab = wireTablist($("topics-tabs"), ".res-tab", id => {
    // 三个面共用一套显隐：面与页签一一对应，写死两个 id 会让第三个面
    // 无论点哪颗都留在屏幕上。
    $("topics-rank").classList.toggle("hidden", id !== "rank");
    $("topics-scroll").classList.toggle("hidden", id !== "topics");
    $("topics-src").classList.toggle("hidden", id !== "sources");
    // 页签条要跟当前面同档宽（CSS 认这个属性）：吸顶条是不透明的，
    // 只盖住 780 的话 1160 的表格会从条的两侧穿出来。
    $("view-topics").dataset.face = id;
    // 三个面共用一个滚动盒子，切面时归零 —— 否则从长表切回卡列表会停在半路。
    $("view-topics").scrollTo(0, 0);
  });
  document.querySelectorAll("#topics-tabs .res-tab").forEach(b => {
    b.onclick = () => selectTopicsTab(b.dataset.tab);
  });
  // 页签条那条发丝线**只在吸顶后出现**：不滚的时候两颗页签只占 133px、
  // 条宽 780，画满就是一条 647px 的空线；滚起来才需要它把"条"与"从底下
  // 穿过去的正文"分开。类挂在**滚动盒**上（CSS 认 `#view-topics.stuck`），
  // 因为吸不吸顶是滚动盒的属性，不是条子的。
  const topicsScroll = $("view-topics");
  const syncStuck = () => topicsScroll.classList
    .toggle("stuck", topicsScroll.scrollTop > 0);
  topicsScroll.addEventListener("scroll", syncStuck, { passive: true });
  // 换行业包：分区、胶囊、计数、未接入、头部副标题全部要跟着换（同一份声明渲染五处）。
  // ⚠ 订阅 `applyPackSwitch` 发出的事件，而不是 `#pack` 的 change ——
  //   换包有三条路径，面板点选与程序化指包都不派发 change
  //   （挂在事件上 = 只在其中一条路径上刷新，另两条就出现"看着 A 包、发往 B 包"）。
  on("pack", () => {
    curSrc = "all"; page = 0; kickoffDone = false;
    // 换包要把**排行这一面的全部本地状态**一起归零：赛道 chip、翻页、展开的行、
    // 已经生成过的选题卡。留着任何一个都会出现"看着 A 包的面、点出 B 包的行"。
    curSeg = ""; rankPage = 0; rankAxis = "score";
    openKeys.clear();
    Object.keys(briefState).forEach(k => delete briefState[k]);
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
  // 默认落在**排行**那一面（§3.5）：这一屏回答的是"今天该做什么"。
  selectTopicsTab("rank", false);
  await load();
  kickoffIfStale();
}
