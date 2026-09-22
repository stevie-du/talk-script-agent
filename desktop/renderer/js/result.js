// 结果渲染：指标速览 / 分段卡片 / 分镜 / 合规 / JSON / 日志 / 后续建议。
//
// 三处对齐成熟 agent 的做法：
//  · 结构化输出当卡片看（Perplexity / Linear 的做法）——分段卡片保留，但每段的
//    字数与配额**一律用后端算好的值**，不再在前端重算。
//  · 决策可解释（Claude 的「Why this recommendation?」）——回炉原因、命中词、
//    单字词被忽略等都以可展开块呈现，而不是一句「未通过校验」。
//  · 后续建议（Perplexity 的 Related）——给 3 个可点的下一步，点了只**预填参数**、
//    不自动提交，用户可以改完再发。
//
// 修复前最要命的两个问题：
//  1. 前端 countCN 与后端 count_chars 是两份实现，同一句话差 5~7 字，
//     于是卡片上的「N/配额 字」和顶部「字数」永远对不上，卡片会莫名标红。
//  2. 导出 SRT / MD 从不 revokeObjectURL，每导出一次泄漏一个 Blob。

import { $, el, esc, fmtText, sec, copyText, download, toast } from "./util.js";
import { api } from "./api.js";
import { state } from "./store.js";
import { packLabel } from "./sessions.js";

export const TYPE_LABEL = { hook: "开场钩子", point: "要点", cta: "结尾引导" };

const ICONS = {
  sb: `<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="3" y="4" width="18" height="14" rx="2.2"/><path d="M3 9h18M8 18v2.5M16 18v2.5"/></svg>`,
  shield: `<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 3l7 3v5.5c0 4.3-2.9 7.7-7 9.5-4.1-1.8-7-5.2-7-9.5V6z"/><path d="M9 12l2 2 4-4"/></svg>`,
  code: `<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M9 7l-5 5 5 5M15 7l5 5-5 5"/></svg>`,
  log: `<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M5 4h9l5 5v11a1 1 0 01-1 1H5a1 1 0 01-1-1V5a1 1 0 011-1z"/><path d="M14 4v5h5M8 13h8M8 17h5"/></svg>`,
};


/** 该段在后端的字数 / 配额。后端没给就退回空串——**绝不**在前端重算，
 *  否则又会和 checker 的口径分叉。 */
function segChars(r, i) {
  const seg = (r.check?.segments || [])[i];
  return seg && typeof seg.chars === "number" ? seg : null;
}

export function setHead(title, sub, stateText, cls) {
  $("rh-title").textContent = title || "新对话";
  $("rh-sub").textContent = sub || "";
  const st = $("rh-state");
  st.textContent = stateText || "";
  st.className = "rh-state" + (cls ? " " + cls : "");
}

// ── 主入口 ──────────────────────────────────────────────────
/** opts: { rwOk, onRewrite, onRerun, onRevary, onEdit, jumpToPlaceholder } */
export function renderResult(r, body, opts = {}) {
  body.innerHTML = "";
  const ch = r.check || {};
  const dev = ch.deviation_pct ?? 0;
  const hardN = (ch.hard_hits || []).reduce((a, h) => a + h.count, 0);
  const passed = ch.passed ?? true;

  setHead(
    r.params?.topic || "生成结果",
    // 用显示名而不是 slug：左栏早就显示「电梯」，这里却吐 `elevator` ——
    // 同一个包在同一个窗口的两处叫法不一致，用户会以为是两个东西。
    `${packLabel(r.pack)} · ${r.params?.duration ?? "-"}s · ${r.params?.platform || ""}`,
    passed && hardN === 0 ? "✓ 合格" : `✗ ${hardN ? hardN + " 处硬伤" : "需人工确认"}`,
    passed && hardN === 0 ? "ok" : "bad"
  );

  body.appendChild(renderHeader(r, ch, dev, hardN, opts));
  const banners = renderBanners(r, ch, opts);
  if (banners) body.appendChild(banners);
  body.appendChild(renderResultTabs(r, ch, hardN, opts));
  body.appendChild(renderFollowups(r, opts));
  bindActions(body, r, opts);
}

// ── 1) 头部：主题 + 指标 + 操作 ─────────────────────────────
function renderHeader(r, ch, dev, hardN, opts) {
  const head = el("div", "res-head");
  head.innerHTML = `
    <div class="res-top">
      <div class="res-title">${esc(r.params?.topic || "")}</div>
      <div class="res-tools">
        <button class="ghost" data-act="copy-voice" title="复制口播文案">复制口播</button>
        <button class="ghost" data-act="save-srt" title="导出 SRT 字幕（可直接导入剪映 / PR）">导出 SRT</button>
        <button class="ghost" data-act="save-md" title="另存为 Markdown 文件">另存 MD</button>
        <button class="ghost" data-act="reveal" title="在文件管理器中打开产物目录">打开文件夹</button>
        <button class="ghost" data-act="copy-json" title="复制结构化 JSON">JSON</button>
        <button class="ghost" data-act="rerun" title="用当前参数重新生成（结果基本一致）">重跑</button>
        <button class="ghost" data-act="revary" title="同主题同参数重掷一次，换一种表达，保留本次为上一个版本">换一版</button>
      </div>
    </div>
    <div class="res-metric-list">
      <span class="m-chip">${ch.chars_total ?? "-"}/${ch.target_total ?? "-"} 字</span>
      <span class="m-chip">预计 ${ch.estimated_seconds ?? "-"}s / 目标 ${ch.duration_target ?? "-"}s</span>
      <span class="m-chip ${Math.abs(dev) <= 10 ? "good" : "bad"}">偏差 ${dev > 0 ? "+" : ""}${dev}%</span>
      <span class="m-chip ${hardN === 0 ? "good" : "bad"}">禁用词 ${hardN} 硬 · ${(ch.soft_hits || []).length} 待确认</span>
    </div>`;
  return head;
}

// ── 2) 横幅（含「为什么回炉」的可展开解释）──────────────────
function renderBanners(r, ch, opts) {
  const items = [];
  // 分流读机器码 `action_code`；只有这条改动之前落盘的老产物才走中文标签兜底
  // （不然历史记录里这条「为什么回炉」横幅会凭空消失）。
  const isFullRecheck = v => v.action_code === "full_recheck"
    || (v.action_code === undefined && v.action === "全文回炉");
  const revs = (r.revisions || []).filter(isFullRecheck);
  if (revs.length) {
    const why = [...new Set(revs.map(v => (v.report?.blockers || []).join("、")).filter(Boolean))]
      .join("；") || "未通过校验";
    const detail = revs.map(v => {
      const rep = v.report || {};
      const hard = (rep.hard_hits || []).map(h => `${h.word}×${h.count}`).join("、");
      return `第 ${v.round} 轮：偏差 ${rep.deviation_pct}%`
        + (hard ? `，命中 ${hard}` : "")
        + (rep.chars_total ? `，${rep.chars_total} 字` : "");
    }).join("\n");
    items.push(`<details class="banner info why">
        <summary>首轮${esc(why)}，已自动回炉 ${revs.length} 轮${ch.passed ? "并修正为合格版本" : "后仍未达标"} —— 为什么？</summary>
        <pre class="why-body">${esc(detail)}</pre>
      </details>`);
  }
  if (!(ch.passed ?? true)) {
    items.push(`<div class="banner warn">⛔ ${esc((ch.blockers || []).join("；"))}——已达回炉上限，可点「重跑」或按下方建议调整参数</div>`);
  }
  if ((r.placeholders || []).length) {
    items.push(`<div class="banner warn jumpable" data-jump="placeholder">⚠ 含 ${r.placeholders.length} 处占位事实：${r.placeholders.map(esc).join("、")}——点击定位首处，补充后再发布</div>`);
  }
  if ((ch.soft_hits || []).length) {
    items.push(`<div class="banner info">待确认 ${ch.soft_hits.length} 词：${ch.soft_hits.map(h => esc(h.word) + "×" + h.count).join("、")}（语境正常即可放行）</div>`);
  }
  if ((ch.dropped_short || []).length) {
    items.push(`<details class="banner info"><summary>有 ${ch.dropped_short.length} 个单字禁用词被忽略（${ch.dropped_short.map(esc).join("、")}）—— 为什么？</summary>
        <pre class="why-body">单字词会命中「最${""}近」「第一${""}次」这类正常用词，噪声大于收益，因此不下发匹配。
若要拦绝对化表述，请在 banwords.yaml 里写具体短语（如「最低价」「最便宜」）。</pre></details>`);
  }
  if (r.quota_degraded) {
    const q = (r.quota || {}).total;
    items.push(`<details class="banner info"><summary>字数配额${q ? `（总计 ${esc(String(q))} 字）` : ""}是按「时长 × 语速」估算的通用值，不是本行业包配的 —— 为什么？</summary>
        <pre class="why-body">这个行业包的 pack.yaml 里没有 quota_table，引擎只能按 时长 × 语速 × 0.95 估一个总量，
再按 15% / 65% / 20% 分给开场、正文、结尾。各段卡片上的「x/y 字」用的就是这个估算值，
所以它跟本行业的真实表达习惯可能有偏差。

要让它贴合本行业：在 packs/<行业>/pack.yaml 里补 quota_table，
按 60/90/120 秒等档位写 total/hook/body/cta 四个数（见 docs/ 里的建包说明）。</pre></details>`);
  }
  if (r.pack_draft) {
    items.push(`<div class="banner warn">⚠ 本结果来自草稿行业包，内容需人工校对</div>`);
  }
  if (!items.length) return null;
  const wrap = el("div", "res-banners");
  wrap.innerHTML = items.join("");
  const jp = wrap.querySelector('[data-jump="placeholder"]');
  if (jp) jp.onclick = () => opts.jumpToPlaceholder?.();
  return wrap;
}

// ── 3) 分段卡片 ─────────────────────────────────────────────
/** 某一段的实际语速（字/秒）。`null` = 算不出（没时间轴 / 没字数）。

 *  口播稿的第一约束是**时间**不是字数，而"这段念太快"正是最该被看见的问题 ——
 *  `pack.yaml` 的 `rate_by_style` 一直配着每档目标语速、checker 也算得出实际值，
 *  但界面上从来没出现过（§2.7 ②）。
 *
 *  ⚠ 时间轴把**段间停顿（0.5 秒）算在段里**（`pipeline._compute_timings`：
 *    `dur = 字数/语速 + (0.5 if 不是最后一段)`），所以还原"念这段话的语速"
 *    必须把那 0.5 秒减掉 —— 不减的话每段都被读成偏慢，而这是**系统性偏差**，
 *    不是某一段的问题。
 */
function segRate(r, i) {
  const tm = (r.timings || [])[i];
  const info = segChars(r, i);
  if (!tm || !info || !info.chars) return null;
  const last = (r.sections || []).length - 1;
  const dur = (tm.end - tm.start) - (i < last ? 0.5 : 0);
  return dur > 0 ? info.chars / dur : null;
}

/** 节奏条（§2.7 ①）：整篇的时间分配一眼可见。
 *
 *  修复前"9s / 35s / 14s"只散在每段的 `.meta` 文字里，看不出整篇分配 ——
 *  而口播是时间的东西，超配额的段本该一眼被认出来。
 *  `--w` 走 CSSOM 写（内联 style 属性会被 CSP 静默忽略）。 */
function renderTempo(r) {
  const secs = r.sections || [];
  const tms = r.timings || [];
  if (secs.length < 2 || tms.length !== secs.length) return null;
  const span = tms[tms.length - 1].end - tms[0].start;
  if (!(span > 0)) return null;
  const rows = secs.map((s, i) => {
    const dur = Math.max(0, tms[i].end - tms[i].start);
    const info = segChars(r, i);
    return { type: s.type, dur: dur, pct: Math.round(dur / span * 100),
             over: !!(info && info.quota && info.chars > info.quota * 1.3) };
  });
  const byType = (t) => rows.filter(x => x.type === t);
  const sum = (xs) => xs.reduce((a, x) => a + x.dur, 0);
  const hook = byType("hook"), point = byType("point"), cta = byType("cta");
  const pct = (d) => Math.round(d / span * 100);
  const bar = rows.map(x =>
    `<i class="${x.type}${x.over ? " over" : ""}" data-w="${Math.round(x.dur / span * 100)}"></i>`).join("");
  const over = rows.filter(x => x.over).length;
  return `<div class="page-card">
    <div class="res-top"><b class="card-title">节奏</b>
      <span class="hint">目标 ${r.params?.duration ?? "-"}s · 预计 ${r.check?.estimated_seconds ?? "-"}s</span>
      <span class="topics-grow"></span>
      <span class="m-chip ${over ? "bad" : "good"}">${over ? `${over} 段超配额` : "各段都不超配额"}</span></div>
    <div class="tempo">${bar}</div>
    <div class="tempo-legend">
      <span>开场钩子 ${hook.length} 段 ${sum(hook).toFixed(1)}s（${pct(sum(hook))}%）</span>
      <span>要点 ${point.length} 段 ${sum(point).toFixed(1)}s（${pct(sum(point))}%）</span>
      <span>结尾引导 ${cta.length} 段 ${sum(cta).toFixed(1)}s（${pct(sum(cta))}%）</span>
    </div>
  </div>`;
}

/** 把命中的 tell 词在正文里标出来（§2.7 ③：**可定位**）。
 *
 *  只加下划线，**不改文字色** —— 拿 `--warn` 当正文色就撞上 §2.6 那笔
 *  「状态药丸族对比度不达标」的欠账（正文比药丸更该守住 4.5:1）。
 *
 *  ⚠ 在 `fmtText` **之后**做，所以要绕开 HTML 标签：按 `<...>` 切开，
 *    只在文本片段里替换 —— 否则会插进 `<strong>` 的属性或标签名里，把标签拆坏。
 */
function markTells(html, words) {
  const ws = [...new Set((words || []).filter(Boolean))].sort((a, b) => b.length - a.length);
  if (!ws.length) return html;
  const rx = new RegExp(ws.map(w => w.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|"), "g");
  const parts = String(html).split(/(<[^>]*>)/);
  return parts.map((p, idx) => idx % 2 === 1
    ? p                                        // 奇数下标是标签，原样放回
    : p.replace(rx, m => `<span class="tell-mark" data-tell="1">${m}</span>`)
  ).join("");
}

/** 该段命中的 tell 词（后端 `TellHit.words` 给的**原文片段**，不是展示串）。 */
function tellWords(r, i) {
  const hits = (r.check?.ai_tells || {}).hits || [];
  const want = `第${i + 1}段`;
  return hits.filter(h => h.where === want).flatMap(h => h.words || []);
}

/** 文案面板 = 节奏卡 + 分段卡片。
 *
 *  节奏条放在最上面：口播的第一约束是时间，而"整篇怎么分配"是读分段卡片
 *  读不出来的（每段只知道自己那 9s / 35s / 14s，看不出 24% 花在了结尾）。
 */
function renderScriptPane(r, opts) {
  const wrap = el("div", "script-pane");
  const tempo = renderTempo(r);
  if (tempo) wrap.insertAdjacentHTML("beforeend", tempo);
  wrap.appendChild(renderSections(r, opts));
  // 节奏条里那些 `--w` 必须在这里补写：它是 insertAdjacentHTML 进来的，
  // 不在 renderSections 的查询范围里（漏了它条就是全 0 宽，界面不报错）。
  wrap.querySelectorAll(".tempo > i").forEach(i =>
    i.style.setProperty("--w", i.dataset.w || 0));
  return wrap;
}

function renderSections(r, opts) {
  const segs = el("div", "script-list");
  let pi = 0;
  (r.sections || []).forEach((s, i) => {
    const label = s.type === "point" ? `要点${++pi}` : TYPE_LABEL[s.type];
    const tm = (r.timings || [])[i];
    const info = segChars(r, i);
    const over = !!(info && info.quota && info.chars > info.quota * 1.3);
    const card = el("div", `script-card ${s.type}${over ? " over-quota" : ""}`);
    card.style.setProperty("--i", i);
    const quota = info
      ? `<span class="quota ${over ? "over" : ""}"
             title="${over ? "超过配额 30% 以上，建议压缩" : "后端统计字数 / 该段配额"}">${info.chars}${info.quota ? "/" + info.quota : ""} 字</span>`
      : "";
    // 每段语速 vs 目标（§2.7 ②）：`rate_by_style` 的目标语速一直配着、
    // checker 也算得出实际值，但界面上从来没露过。超目标 10% 走现成的 `.over` 红。
    const rate = segRate(r, i);
    const target = Number(r.params?.rate) || 0;
    const fast = rate !== null && target > 0 && rate > target * 1.1;
    const rateChip = rate === null || !target ? ""
      : `<span class="rate${fast ? " over" : ""}"
             title="这段念出来的语速（字数 ÷ 净口播秒数，已扣掉段间 0.5s 停顿）vs 本风格目标">`
        + `${rate.toFixed(1)} 字/秒 · 目标 ${target}</span>`;
    const marks = tellWords(r, i);
    const markTip = marks.length
      ? `<span class="tell-jump" data-goto="smell" title="点一下看这条人味标记的明细">`
        + `${marks.length} 处人味标记</span>` : "";
    card.innerHTML = `
      <div class="card-head">
        <span class="seg-tag ${s.type}">${esc(label)}</span>
        ${quota}
        ${rateChip}
        <span class="meta">${tm ? sec(tm.start) + "–" + sec(tm.end) : ""}${tm ? " · " : ""}字幕：${esc(s.subtitle || "—")}</span>
      </div>
      <div class="card-text">${markTells(fmtText(s.text), marks)}</div>
      <div class="card-foot">
        ${markTip}
        <button class="ghost rw"${opts.rwOk ? "" : " disabled"} title="${opts.rwOk
          ? "单段重写：只改这一段并重跑校验，不整篇回炉"
          : "历史记录不可局部重写（后台作业已释放），可用「换一版」整体重生成"}">✎ 重写本段</button>
        <input class="rw-feedback" placeholder="给这段的修改意见（可选），回车提交">
      </div>`;
    const input = card.querySelector(".rw-feedback");
    const rwBtn = card.querySelector(".rw");
    rwBtn.onclick = () => {
      if (!opts.rwOk) return;
      const foot = card.querySelector(".card-foot");
      foot.classList.toggle("editing");
      if (foot.classList.contains("editing")) input.focus();
    };
    input.addEventListener("keydown", ev => {
      if (ev.key === "Enter" && !ev.shiftKey) {
        ev.preventDefault();
        if (!opts.rwOk) { toast("历史记录不支持单段重写，请用「换一版」"); return; }
        opts.onRewrite?.(i, input.value.trim());
      }
    });
    segs.appendChild(card);
  });
  return segs;
}

// ── 4) 折叠块 ───────────────────────────────────────────────
/** 产物分区：文案 / 分镜 / 字幕 / 合规 / 数据。

    修复前这些全平铺在一条长滚动流里（分镜还默认展开，把主产物文案往下挤），
    而它们的性质完全不同：文案是主产物、分镜是视觉、JSON 是原始数据、
    日志是调试信息。混在一起层级就乱了。

    成熟 agent（Claude/ChatGPT 的 Artifacts、WorkBuddy 的产物区）都用
    「主内容区 + tab 切换视图」：每个视图只放一类产物，互不干扰。

    所有 pane 一次渲染、用 hidden 切换（不销毁重建）—— 否则 bindActions
    绑的单段重写 / 导出按钮在切回时会失效。
*/
function renderResultTabs(r, ch, hardN, opts) {
  const complyOk = (ch.passed ?? true) && hardN === 0;
  const logs = r.logs || [];

  const defs = [
    { id: "script", label: "文案", badge: "",
      render: () => renderScriptPane(r, opts) },
    { id: "story", label: "分镜", badge: `${(r.storyboard || []).length} 镜`,
      render: () => renderStoryboard(r),
      hide: !(r.storyboard || []).length },
    { id: "subs", label: "字幕", badge: "",
      render: () => renderSubtitlePane(r) },
    { id: "comp", label: "合规", cls: complyOk ? "ok" : "bad",
      badge: complyOk ? "✓ 通过" : `✗ ${hardN ? hardN + " 处硬伤" : "未通过"}`,
      render: () => renderCompliance(r) },
    { id: "data", label: "数据", badge: `${logs.length} 条日志`,
      render: () => renderDataPane(r) },
  ].filter(d => !d.hide);

  const wrap = el("div", "res-tabs-wrap");
  const bar = el("div", "res-tabs");
  bar.setAttribute("role", "tablist");
  const panes = el("div", "res-panes");

  defs.forEach((d, i) => {
    const btn = el("button", "res-tab" + (i === 0 ? " on" : "") + (d.cls ? " " + d.cls : ""));
    btn.type = "button";
    btn.dataset.tab = d.id;
    btn.setAttribute("role", "tab");
    // role=tab 光有 aria-selected 不够：读屏要能说出"这个 tab 控制哪块内容"，
    // 键盘要能用 ←/→ 在一组 tab 间移动（WAI-ARIA Tabs 模式）。
    // 原来这些都没做，tab 对键盘和读屏基本等于不存在。
    btn.id = `rtab-${d.id}`;
    btn.setAttribute("aria-controls", `rpane-${d.id}`);
    btn.tabIndex = i === 0 ? 0 : -1;          // roving tabindex：Tab 键只停在选中项
    btn.setAttribute("aria-selected", i === 0 ? "true" : "false");
    btn.innerHTML = `<span class="rt-t">${esc(d.label)}</span>`
      + (d.badge ? `<span class="rt-b">${esc(d.badge)}</span>` : "");
    btn.onclick = () => selectTab(d.id);
    bar.appendChild(btn);

    const pane = el("div", "res-pane" + (i === 0 ? "" : " hidden"));
    pane.dataset.tab = d.id;
    pane.id = `rpane-${d.id}`;
    pane.setAttribute("role", "tabpanel");
    pane.setAttribute("aria-labelledby", `rtab-${d.id}`);
    pane.tabIndex = 0;                        // 面板可聚焦，读屏才能从 tab 跳进内容
    const content = d.render();
    if (typeof content === "string") pane.innerHTML = content;
    else if (content) pane.appendChild(content);
    panes.appendChild(pane);
  });

  const tabs = () => Array.from(bar.querySelectorAll(".res-tab"));
  function selectTab(id) {
    for (const n of tabs()) {
      const on = n.dataset.tab === id;
      n.classList.toggle("on", on);
      n.setAttribute("aria-selected", on ? "true" : "false");
      n.tabIndex = on ? 0 : -1;
      if (on) n.focus();
    }
    panes.querySelectorAll(".res-pane").forEach(p =>
      p.classList.toggle("hidden", p.dataset.tab !== id));
  }
  // ←/→ 循环，Home/End 跳首尾 —— 与系统原生 tablist 行为一致
  bar.addEventListener("keydown", e => {
    const list = tabs();
    const cur = list.findIndex(n => n === document.activeElement);
    if (cur < 0) return;
    const map = { ArrowRight: 1, ArrowLeft: -1 };
    let next = null;
    if (e.key in map) next = (cur + map[e.key] + list.length) % list.length;
    else if (e.key === "Home") next = 0;
    else if (e.key === "End") next = list.length - 1;
    if (next === null) return;
    e.preventDefault();
    selectTab(list[next].dataset.tab);
  });

  wrap.appendChild(bar);
  wrap.appendChild(panes);
  return wrap;
}

/** 字幕预览：导出前就能看到 SRT 长什么样。

    以前只能导出成文件后打开才知道对不对 —— SRT 导出曾经出过
    「把关键词当字幕、句子被砍断」的 bug，而用户在界面上完全无从发现。
*/
function renderSubtitlePane(r) {
  const rows = [];
  (r.sections || []).forEach((s, i) => {
    const tm = (r.timings || [])[i] || { start: 0, end: 0 };
    srtCues(s.text, tm.start, tm.end).forEach(c => rows.push(c));
  });
  if (!rows.length) return `<p class="hint">暂无字幕内容。</p>`;

  const fmt = t => {
    const s = Math.max(0, t);
    const m = Math.floor(s / 60);
    return `${m}:${String(Math.floor(s % 60)).padStart(2, "0")}`;
  };
  const body = rows.map((c, i) => `<tr>
    <td class="sn">${i + 1}</td>
    <td class="tm">${fmt(c.start)}–${fmt(c.end)}</td>
    <td class="tx">${esc(c.text)}</td></tr>`).join("");
  return `<div class="sub-pane">
    <p class="hint">下面是导出 SRT 的实际内容（共 ${rows.length} 行），
      与「导出 SRT」按钮产出的文件一致。</p>
    <table class="sub-table"><tbody>${body}</tbody></table>
    <div class="sub-actions">
      <button class="ghost" data-act="save-srt">导出 SRT</button>
    </div></div>`;
}

/** 数据：原始 JSON 与调试日志 —— 明确归到「非产物」的最后一档。 */
function renderDataPane(r) {
  const logs = r.logs || [];
  return `<details class="acc">
      <summary><span class="acc-t">结构化 JSON</span></summary>
      <div class="acc-body"><pre class="code">${esc(JSON.stringify(r, null, 2))}</pre></div>
    </details>
    <details class="acc">
      <summary><span class="acc-t">日志</span>
        <span class="acc-badge info">${logs.length} 条</span></summary>
      <div class="acc-body">${renderLogs(logs)}</div>
    </details>`;
}

// ── 5) 后续建议（点了只预填，不自动提交）────────────────────
function renderFollowups(r, opts) {
  const chips = [];
  const dur = Number(r.params?.duration) || 60;
  if (dur > 30) chips.push({ label: `压缩到 30s`, act: "duration", value: 30 });
  if (dur < 90) chips.push({ label: `扩到 90s`, act: "duration", value: 90 });
  const platforms = opts.platforms || [];
  const other = platforms.find(p => p !== r.params?.platform);
  if (other) chips.push({ label: `换平台：${other}`, act: "platform", value: other });
  const audiences = opts.audiences || [];
  const otherAud = audiences.find(a => a !== r.params?.audience);
  if (otherAud) chips.push({ label: `换个受众：${otherAud}`, act: "audience", value: otherAud });
  const points = (r.sections || []).map((s, i) => ({ s, i })).filter(x => x.s.type === "point");
  if (points.length && opts.rwOk) {
    chips.push({ label: `重写「要点1」`, act: "rewrite", value: points[0].i });
  }
  if (!chips.length) return el("div", "followups hidden");
  const box = el("div", "followups");
  box.innerHTML = `<span class="fu-label">下一步</span>` +
    chips.slice(0, 4).map((c, i) =>
      `<button class="fu-chip" data-i="${i}">${esc(c.label)}</button>`).join("");
  box.querySelectorAll(".fu-chip").forEach(b => {
    b.onclick = () => {
      const c = chips[Number(b.dataset.i)];
      if (c.act === "rewrite") {
        const card = box.parentElement.querySelectorAll(".script-card .rw-feedback")[0];
        card?.closest(".card-foot")?.classList.add("editing");
        card?.focus();
        return;
      }
      opts.onPrefill?.(c.act, c.value);
    };
  });
  return box;
}

// ── 子渲染 ──────────────────────────────────────────────────
export function renderStoryboard(r) {
  if (!(r.storyboard || []).length) {
    return `<p class="hint">本次未生成分镜（输出内容选择了「仅口播」）。</p>`;
  }
  const rows = r.storyboard.map((sh, i) => {
    const tm = (r.timings || [])[i];
    const vo = sh.voiceover || r.sections?.[i]?.text || "";
    return `<tr>
      <td>${esc(sh.time || (tm ? `${tm.start}-${tm.end}s` : ""))}</td>
      <td>${esc(sh.shot || "")}</td><td>${esc(vo)}</td>
      <td>${esc(sh.subtitle || "")}</td><td>${esc(sh.sfx || "")}</td>
      <td>${esc(sh.note || "")}</td></tr>`;
  }).join("");
  return `<div class="tbl-wrap"><table>
    <thead><tr><th class="col-time">时间</th><th>画面/景别</th><th>口播</th>
    <th>字幕</th><th>音效/BGM</th><th>拍摄提示</th></tr></thead>
    <tbody>${rows}</tbody></table></div>`;
}

export function renderCompliance(r) {
  const ch = r.check || {};
  const hardN = (ch.hard_hits || []).reduce((a, h) => a + h.count, 0);
  const dev = ch.deviation_pct ?? 0;
  const rows = [
    ["硬禁用词（必改）", hardN === 0 ? `<span class="ok">✅ 无</span>`
      : `<span class="bad">${(ch.hard_hits || []).map(h => `${esc(h.word)}×${h.count}`).join("、")}</span>`],
    ["待确认（语境相关）", (ch.soft_hits || []).length
      ? ch.soft_hits.map(h => `${esc(h.word)}×${h.count}`).join("、") : `<span class="ok">✅ 无</span>`],
    ["字数与时长", `${ch.chars_total ?? "-"}/${ch.target_total ?? "-"} 字 · 偏差 ${dev > 0 ? "+" : ""}${dev}% → `
      + (ch.passed ? `<span class="ok">✅ 合格</span>`
        : `<span class="bad">❌ ${esc((ch.blockers || []).join("；"))}</span>`)],
    ["占位事实", (r.placeholders || []).length
      ? `⚠ ${r.placeholders.map(esc).join("、")}` : `<span class="ok">✅ 无</span>`],
    ["回炉/重写记录", (r.revisions || []).length
      ? r.revisions.map((v, i) => `第${i + 1}次：${esc(v.action || v.feedback || "单段重写")}`).join("；")
      : "无"],
    ["行业包状态", r.pack_draft ? `⚠ 草稿包，内容需人工校对` : `<span class="ok">✅ 精修包</span>`],
  ];
  return `<div class="tbl-wrap"><table>
    <thead><tr><th class="col-item">检查项</th><th>结果</th></tr></thead>
    <tbody>${rows.map(([k, v]) => `<tr><td>${k}</td><td>${v}</td></tr>`).join("")}</tbody></table></div>`;
}

export function renderLogs(logs) {
  if (!logs?.length) return `<p class="hint">暂无日志</p>`;
  return logs.map(s =>
    `<details class="log-block"><summary>${esc(s.ts || "")} · ${esc(s.title)}</summary>
      <pre>${esc(JSON.stringify(s.data, null, 1))}</pre></details>`).join("");
}

// ── 失败 / 停止态：给可操作的动作，而不是一句话 ─────────────
export function renderFailure(body, { title, message, detail, retryLabel = "重试" }, opts = {}) {
  body.innerHTML = `
    <div class="banner warn"><b>${esc(title)}</b><br>${esc(message)}</div>
    ${detail ? `<details class="banner info"><summary>技术细节</summary>
      <pre class="why-body">${esc(detail)}</pre></details>` : ""}
    <div class="fail-actions">
      <button class="ghost" data-act="retry">${esc(retryLabel)}</button>
      <button class="ghost" data-act="settings">检查模型设置</button>
      <button class="ghost" data-act="copy-err">复制错误</button>
    </div>`;
  const q = s => body.querySelector(s);
  q('[data-act="retry"]').onclick = () => opts.onRetry?.();
  q('[data-act="settings"]').onclick = () => opts.onOpenSettings?.();
  // 技术细节必须带上：用户复制错误信息多半是为了去问人 / 提 issue，
  // 只有标题和一句人话根本定位不了问题。
  q('[data-act="copy-err"]').onclick = () =>
    copyText(errorText(title, message, detail), "错误信息已复制");
}

/** 停止 / 中断态。
 *  ⚠ 「产物未落盘」这一句是**有前提的**：只有后端确认作业落在 cancelled 时才成立
 *  （app/pipeline.py 的 `_stop_check` 让取消永远走不到写产物那一步）。
 *  同一秒里其实已经跑完的那种，后端回的是 done + 产物 —— 那一条由 jobs.js 的
 *  abort() 直接画结果并说「产物已保存」，不进这个函数。 */
export function renderStopped(body, opts = {}) {
  const title = opts.title || "已停止本次生成";
  body.innerHTML = `
    <div class="banner info">${esc(title)}。已产生的 token 不会退回，产物未落盘。</div>
    <div class="fail-actions">
      <button class="ghost" data-act="retry">用同样的参数再来一次</button>
      <button class="ghost" data-act="revary">换个表达重掷</button>
    </div>`;
  body.querySelector('[data-act="retry"]').onclick = () => opts.onRetry?.();
  body.querySelector('[data-act="revary"]').onclick = () => opts.onRevary?.();
}

// ── 操作绑定 ────────────────────────────────────────────────
function bindActions(body, r, opts) {
  const q = s => body.querySelector(s);
  const on = (sel, fn) => { const n = q(sel); if (n) n.onclick = fn; };

  on('[data-act="copy-voice"]', () =>
    copyText(voicePlainText(r), "口播已复制"));

  on('[data-act="save-srt"]', () => {
    download(`字幕_${(r.params?.topic || "").slice(0, 12)}.srt`, resultSrt(r));
    toast("SRT 字幕已导出");
  });

  on('[data-act="save-md"]', () => {
    download(`口播脚本_${(r.params?.topic || "").slice(0, 12)}.md`,
      resultMarkdown(r), "text/markdown");
    toast("Markdown 已导出");
  });

  on('[data-act="reveal"]', async () => {
    try { await api.reveal(r.id); toast("已打开产物目录"); }
    catch (e) { toast("打开失败：" + e.message, 3500); }
  });

  on('[data-act="copy-json"]', () => copyText(JSON.stringify(r, null, 2), "JSON 已复制"));
  on('[data-act="rerun"]', () => opts.onRerun?.());
  on('[data-act="revary"]', () => opts.onRevary?.());
}

// ── 导出格式 ────────────────────────────────────────────────
export function resultMarkdown(r) {
  const p = r.params || {};
  // 字段可能缺失（旧产物 / 不同包），不能让文档里出现 "undefined"
  const meta = [p.duration && `${p.duration}s`, p.platform, p.style, p.persona]
    .filter(Boolean).join(" / ");
  const lines = [`# 口播脚本：${p.topic || "未命名"}`, "",
    `- ${meta}（${r.pack}）`, "",
    "## 口播文案", ""];
  let pi = 0;
  (r.sections || []).forEach((s, i) => {
    const tm = r.timings?.[i];
    const label = s.type === "point" ? `要点${++pi}` : TYPE_LABEL[s.type];
    lines.push(`**【${label}】** ${tm ? `${tm.start}-${tm.end}秒` : ""}`);
    // ／ 是停顿符：导出成一行会让整段挤成一坨，读的人找不到断句
    lines.push(String(s.text || "").replace(/／/g, "\n"), "");
  });
  // 校验信息可能整块缺失（旧产物 / 未校验），不能让文档里出现 "undefined"
  const ch = r.check || {};
  const stats = [
    ch.chars_total != null
      ? `字数 ${ch.chars_total}${ch.target_total != null ? "/" + ch.target_total : ""} 字` : "",
    ch.estimated_seconds != null ? `预估 ${ch.estimated_seconds}s` : "",
    ch.deviation_pct != null ? `偏差 ${ch.deviation_pct}%` : "",
    ch.passed != null ? (ch.passed ? "合格" : (ch.blockers || []).join("；")) : "",
  ].filter(Boolean);
  if (stats.length) lines.push("---", stats.join(" · "));
  if (r.placeholders?.length) lines.push("", `> 占位事实：${r.placeholders.join("、")}`);
  return lines.join("\n");
}

/** 复制口播用的纯文本。

    去掉 `**` 这类只服务于界面排版的标记 —— 复制出去是给提词器 / 剪映用的，
    字面量的星号会直接被念出来或显示出来。

    `／` **不能去掉**：examples/demo-60s.txt 里它就是标准停顿符（每行都有），
    抹掉等于删掉断句。
    `{{待补}}` 也保留 —— 那是提醒用户还有事实没填。
*/
export function voicePlainText(r) {
  return (r.sections || [])
    .map(s => String(s.text || "").replace(/\*\*/g, ""))
    .join("\n\n");
}

/** 错误信息文本：技术细节必须带上。 */
export function errorText(title, message, detail) {
  return [title, message, detail ? `技术细节：${detail}` : ""]
    .filter(Boolean).join("\n");
}

// 中文字幕单行建议长度：超过这个数观众读不完
const SRT_MAX_CHARS = 18;

/** 把一个段落切成若干字幕行，并按字数比例分配 [start, end] 这段时间。 */
export function srtCues(text, start, end) {
  const src = String(text || "")
    .replace(/\*\*/g, "")                 // 加粗符号不进字幕
    .replace(/\{\{[^}]*\}\}/g, "")        // 占位事实不进字幕
    .split(/／|\/|\n+/)                    // ／ 是生成时约定的停顿符，天然就是断句点
    .map(s => s.trim()).filter(Boolean);

  const chunks = [];
  for (const p of src) {
    if (p.length <= SRT_MAX_CHARS) { chunks.push(p); continue; }
    // 超长句先按标点切，仍超长再硬切
    for (const seg of p.split(/(?<=[，。、！？；：,.!?;:])/)) {
      let rest = seg;
      while (rest.length > SRT_MAX_CHARS) {
        chunks.push(rest.slice(0, SRT_MAX_CHARS));
        rest = rest.slice(SRT_MAX_CHARS);
      }
      if (rest) chunks.push(rest);
    }
  }
  const total = chunks.reduce((a, c) => a + c.length, 0) || 1;
  let t = start;
  return chunks.map(c => {
    const d = (end - start) * (c.length / total);
    const cue = { start: t, end: t + d, text: c };
    t += d;
    return cue;
  });
}

/** SRT：每段按停顿符切成多行，时间轴在段内按字数比例展开。

    修复前这里有三个错，导出来的文件根本没法当字幕用：
      1. 优先取 `subtitle` —— 但那字段是「字幕关键词 ≤12 字」的**摘要**，
         不是字幕文本，于是 60 秒视频导出 5 行关键词；
      2. 取不到时回落 `s.text.slice(0, 16)` —— 句子被从中间砍断；
      3. 一个段落只出一行，13 秒的段落显示一行停 13 秒。
*/
export function resultSrt(r) {
  const ts = t => {
    const ms = Math.max(0, Math.round((t || 0) * 1000));
    const pad = (n, w) => String(n).padStart(w, "0");
    return `${pad(Math.floor(ms / 3600000), 2)}:${pad(Math.floor(ms / 60000) % 60, 2)}`
      + `:${pad(Math.floor(ms / 1000) % 60, 2)},${pad(ms % 1000, 3)}`;
  };
  const lines = [];
  let n = 0;
  (r.sections || []).forEach((s, i) => {
    const tm = (r.timings || [])[i] || { start: 0, end: 0 };
    for (const cue of srtCues(s.text, tm.start, tm.end)) {
      n += 1;
      lines.push(String(n), `${ts(cue.start)} --> ${ts(cue.end)}`, cue.text, "");
    }
  });
  return lines.join("\r\n");
}

/** 「点击定位首处」跳到**第一个占位符本身**。
 *  ⚠ 选择器必须带 `.jumpable`：`.script-card .over` 会先命中卡片头上的
 *  字数胶囊 `<span class="quota over">120/85 字</span>` —— 那只是"超配额"，
 *  不是"这里缺事实"，两个 `.over` 是巧合同名（P3-9 实测跳到了字数胶囊上）。
 *  真正可跳的那个由 util.fmtText 打上 `.over.jumpable`。 */
export function jumpToFirstPlaceholder(body) {
  const hit = body.querySelector(".script-card .over.jumpable")
    || body.querySelector(".over.jumpable");
  if (!hit) { toast("未找到占位事实"); return; }
  hit.scrollIntoView({ behavior: "smooth", block: "center" });
  hit.classList.remove("flash");
  void hit.offsetWidth;
  hit.classList.add("flash");
}
