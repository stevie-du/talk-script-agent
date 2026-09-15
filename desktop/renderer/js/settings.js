// 设置：整窗二级页（生成偏好 / 行业包 / 新建行业包 / 模型接口 / 知识库 / 技能）。
//
// 修复前的一个交互坑：每次打开设置都无条件 preloadSettings()，会把用户
// 「刚填好但还没保存」的 base_url / model 覆盖回服务端的旧值 ——
// 顺序是「打开设置 → 改 URL → Ctrl+, 关掉 → Ctrl+, 再开 → 输入没了」。
// 现在按字段记 dirty，只有没被改过的输入框才回填。

import { $, el, esc, toast, bindOnce } from "./util.js";
import { api } from "./api.js";
import { state, emit } from "./store.js";
import { closeOverlays, appConfirm } from "./overlays.js";
// ui.js 与 settings.js 互相引用，但引用的都是**函数声明**（会被提升），
// 所以循环依赖在调用时已解析完毕，安全。
import { fillPackSelect } from "./ui.js";

const PANES = ["gen", "packinfo", "packgen", "llm", "kb", "skills"];
const dirty = new Set();

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
// 避免「能填的范围」和「能存的范围」变成两套数（temperature 曾经就是这样）。
const NUM_FIELDS = [
  ["temperature", "st-temperature", "采样温度"],
  ["retries", "st-retries", "重试次数"],
  ["timeout", "st-timeout", "单次超时"],
  ["max_tokens", "st-maxtokens", "输出预算"],
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
}

export function setPane(pane) {
  if (!PANES.includes(pane)) pane = state.settingsPane;
  PANES.forEach(p => $(("pane-" + p))?.classList.toggle("hidden", p !== pane));
  document.querySelectorAll(".stg-nav-item").forEach(n => {
    n.classList.toggle("on", n.dataset.pane === pane);
  });
  const cur = $("pane-" + pane);
  if (cur) cur.scrollTop = 0;
  state.settingsPane = pane;
  emit("pane", pane);
  // 行业包详情每次进入都重拉：包可能被切换过，文件清单与草稿角标也可能变了
  if (pane === "packinfo") {
    openPackInfo().catch(e => toast("读取失败：" + e.message, 3500));
  } else if (pane === "kb" || pane === "skills") {
    openPackFiles(pane);
  }
  return pane;
}

async function preloadSettings() {
  const c = await api.config();
  fill("st-baseurl", c.base_url || "");
  fill("st-model", c.model || "");
  fill("st-temperature", c.temperature ?? "");
  fill("st-retries", c.retries ?? "");
  fill("st-timeout", c.timeout ?? "");
  fill("st-maxtokens", c.max_tokens ?? "");
  if (!dirty.has("st-apikey")) $("st-apikey").value = "";
  renderConfigError(c.config_error);
  const env = c.env_override ? "（当前由环境变量 TALKSCRIPT_API_KEY 覆盖）" : "";
  $("st-status").textContent = c.mock
    ? `当前为 mock 模式（返回夹具，不调模型）${env}`
    : c.api_key_set
      ? `已配置 Key · 模型 ${c.model} · 重试 ${c.retries} 次 / 超时 ${c.timeout}s${env}`
      : `未配置 API Key${env}`;
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
  ["st-baseurl", "st-model", "st-apikey", "st-temperature",
   "st-retries", "st-timeout", "st-maxtokens"].forEach(id => {
    $(id).addEventListener("input", () => dirty.add(id));
  });
  document.querySelectorAll(".stg-nav-item").forEach(n => {
    n.onclick = () => setPane(n.dataset.pane);
  });
  $("btn-close-settings").onclick = closeSettings;
  $("btn-packinfo").onclick = () => setPane("packinfo");
  $("pi-close").onclick = () => setPane("gen");
  $("btn-newpack").onclick = () => {
    $("pg-form").classList.remove("hidden");
    $("pg-result").classList.add("hidden");
    setPane("packgen");
  };
  $("pg-close").onclick = () => setPane("gen");
  $("pg-run").onclick = runPackgen;
  $("pg-done").onclick = onPackDone;

  $("st-save").onclick = async () => {
    try {
      await saveSettings();
      dirty.clear();
    } catch (e) { toast("保存失败：" + e.message, 3500); }
  };
  // 「恢复默认」：base_url / model 填错之后，留空保存是无效的空操作
  // （后端会过滤空串防手滑），所以回到默认必须是一个显式动作。
  $("st-reset-baseurl").onclick = () => resetField("base_url", "st-baseurl");
  $("st-reset-model").onclick = () => resetField("model", "st-model");
  $("kb-pack").onchange = () => openPackFiles(state.settingsPane);
  $("skills-pack").onchange = () => openPackFiles(state.settingsPane);
  $("st-reset-adv").onclick = () => resetField(
    ["retries", "timeout", "max_tokens"], ["st-retries", "st-timeout", "st-maxtokens"]);
  $("st-test").onclick = testConnection;
  $("pi-export").onclick = exportSkill;
  $("pi-undraft").onclick = undraftPack;
});

async function saveSettings() {
  const body = {
    base_url: $("st-baseurl").value.trim(),
    model: $("st-model").value.trim(),
  };
  const key = $("st-apikey").value.trim();
  if (key) body.api_key = key;
  // 数值项（含采样温度）：留空表示不改，填了就在前端先卡一遍范围。
  // 后端也会卡，这里只是让错误当场可见，不必等一次往返；
  // 区间与输入框 min/max 共用 NUMERIC_BOUNDS 这一张表。
  //
  // 温度修复前是单独一个 if、区间写死 1.5（与后端的 2.0 不一致），
  // 现在并入同一张表 —— 少一处「凭想象设限」的机会。
  for (const [key, id, label] of NUM_FIELDS) {
    const raw = $(id).value.trim();
    if (raw === "") continue;
    const n = Number(raw);
    const [lo, hi] = NUMERIC_BOUNDS[key];
    if (!Number.isFinite(n) || n < lo || n > hi) {
      toast(`${label}需在 ${lo} ~ ${hi} 之间`);
      return;
    }
    body[key] = n;
  }
  await api.saveConfig(body);
  $("st-apikey").value = "";
  toast("已保存，下次生成即生效");
  // 顶栏的模型名/Key 状态要跟着变，否则用户以为没保存成功
  try {
    state.meta = await api.meta();
    emit("meta", state.meta);
  } catch (_) { /* 忽略：保存本身已成功 */ }
  await preloadSettings().catch(() => {});
}

async function resetField(field, inputId) {
  const fields = [].concat(field);
  const ids = [].concat(inputId);
  try {
    await api.resetConfig(fields);
    // 清掉 dirty 标记，否则 preloadSettings 会拒绝回填输入框
    ids.forEach(id => dirty.delete(id));
    await preloadSettings();
    toast("已恢复默认值");
    state.meta = await api.meta();
    emit("meta", state.meta);
  } catch (e) {
    toast("恢复失败：" + e.message, 3500);
  }
}

// ── 知识库 / 技能：两个面板共用一套只读文件查看器 ──────────
// 做成只读而不是增删改，是刻意的：这些文件是行业包的「源码」，写坏了
// 整个包都废，而浏览器里改文件既没有原子写也没有校验，风险与收益不成比例。
const PANE_FILE_FILTER = {
  kb: () => true,
  skills: (rel) => rel === "skill.yaml" || /^(rules|patterns|compliance)\//.test(rel),
};

// ── 设置内搜索已移除 ──────────────────────────────────────
// 曾经的 filterSettingsNav() 按「导航项文字 + 分区面板全文」过滤左导航，
// 匹配不到就把整组连同标题一起收掉。删掉的理由：设置一共 6 个分区，
// 一屏就能看全，检索框的收益抵不上它在「返回工作区」下方多占的一行，
// 而且它是**唯一**会隐藏导航项的入口 —— 一旦过滤词留在框里（比如上次
// 输的「超时」），用户回到设置页会看到一份缺项却没有任何提示的导航，
// 像个 bug。现在导航恒定完整，所见即所得。
// 连带清理：.stg-search 的 HTML / CSS，以及只为它服务的
// `.stg-nav-item.hidden` / `.nav-sec.hidden` 两条规则。

async function openPackFiles(pane) {
  // 知识库与技能两个面板共用一套只读查看器，但 DOM 节点分开
  // （同一 ID 在一个文档里只能出现一次）
  const pfx = pane === "skills" ? "skills" : "kb";
  const list = $(pfx + "-list");
  const sel = $(pfx + "-pack");
  if (sel && !sel.options.length && state.meta) {
    sel.innerHTML = "";
    for (const p of state.meta.packs) {
      const o = el("option", "", esc(p.display_name) + (p.draft ? "（草稿）" : ""));
      o.value = p.name;
      sel.appendChild(o);
    }
    sel.value = state.meta.default_pack || (state.meta.packs[0] || {}).name || "";
  }
  const name = sel.value;
  if (!name) { list.innerHTML = "<p class='hint'>还没有可用的行业包。</p>"; return; }
  list.innerHTML = "<p class='hint'>载入中…</p>";
  try {
    const p = await api.pack(name);
    const keep = PANE_FILE_FILTER[pane] || (() => true);
    const files = (p.files || []).filter(f => keep(f.rel));
    list.innerHTML = "";
    if (!files.length) {
      list.innerHTML = "<p class='hint'>这个包没有符合条件的文件。</p>";
      return;
    }
    for (const f of files) {
      const row = el("button", "kb-item", esc(f.rel));
      row.type = "button";
      const role = FILE_ROLE(f.rel);
      if (role) row.appendChild(el("span", "kb-role", role));
      row.onclick = () => showPackFile(name, f.rel, row, pfx);
      list.appendChild(row);
    }
  } catch (e) {
    list.innerHTML = `<p class='hint'>载入失败：${esc(e.message)}</p>`;
  }
}

async function showPackFile(name, rel, row, pfx) {
  document.querySelectorAll(`#${pfx}-list .kb-item`).forEach(n => n.classList.remove("on"));
  if (row) row.classList.add("on");
  $(pfx + "-title").textContent = rel;
  $(pfx + "-size").textContent = "";
  $(pfx + "-body").textContent = "载入中…";
  try {
    const d = await api.packFile(name, rel);
    $(pfx + "-size").textContent =
      d.size > 1024 ? (d.size / 1024).toFixed(1) + " KB" : d.size + " B";
    $(pfx + "-body").textContent = d.text;
  } catch (e) {
    $(pfx + "-body").textContent = "读取失败：" + e.message;
  }
}

async function testConnection() {
  const btn = $("st-test");
  const label = btn.textContent;
  btn.disabled = true;
  btn.textContent = "测试中…";
  $("st-status").textContent = "正在连接…";
  try {
    // 带上界面当前值（Key 留空时后端沿用已保存的），未保存也能测
    const r = await api.testConfig({
      base_url: $("st-baseurl").value.trim(),
      api_key: $("st-apikey").value,
      model: $("st-model").value.trim(),
    });
    $("st-status").textContent = r.ok
      ? `连接正常 · ${r.model}${r.detail ? " · " + r.detail : ""}`
      : `连接失败：${r.detail}`;
    toast(r.ok ? "连接正常" : "连接失败，请看下方提示", r.ok ? 2000 : 4000);
  } catch (e) {
    $("st-status").textContent = e.message;
    toast("测试失败：" + e.message, 4000);
  }
  btn.disabled = false;
  btn.textContent = label;
}

async function runPackgen() {
  const industry = $("pg-industry").value.trim();
  const desc = $("pg-desc").value.trim();
  if (!industry || !desc) { toast("请填写行业名称和业务描述"); return; }
  const btn = $("pg-run");
  btn.disabled = true;
  btn.textContent = "生成中（约 1-2 分钟）…";
  try {
    const out = await api.createPack(industry, desc);
    state.lastCreatedPack = out.name;
    $("pg-form").classList.add("hidden");
    $("pg-result").classList.remove("hidden");
    $("pg-checklist").textContent = out.checklist;
    toast(`行业包「${out.display_name}」已生成（草稿）`);
  } catch (e) {
    toast("生成失败：" + e.message, 6000);
  }
  btn.disabled = false;
  btn.textContent = "生成";
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

async function exportSkill() {
  const name = $("pack").value;
  const btn = $("pi-export");
  btn.disabled = true;
  try {
    const out = await api.exportSkill(name, false);
    $("pi-export-hint").innerHTML =
      `✅ 已导出 <b>${esc(out.files)}</b> 个文件到：<br><code>${esc(out.path)}</code>`
      + `<br>${out.hints.map(esc).join("<br>")}`;
    toast("已导出为 Agent 技能");
  } catch (e) { toast("导出失败：" + e.message, 4000); }
  btn.disabled = false;
}

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

const FILE_ROLE = (rel) => {
  if (rel === "pack.yaml") return "包清单";
  if (rel === "skill.yaml") return "技能（怎么写）";
  if (rel === "banwords.yaml") return "禁用词表";
  if (rel === "校对清单.md") return "草稿校对";
  if (rel.startsWith("knowledge/")) return "知识库";
  if (rel.startsWith("compliance/")) return "合规";
  if (rel.startsWith("patterns/")) return "方法库";
  if (rel.startsWith("rules/")) return "规则";
  if (rel.startsWith("private/")) return "私有资料";
  return "";
};

export async function openPackInfo() {
  const name = $("pack").value;
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
  const box = $("pi-files");
  box.innerHTML = "";
  const tb = el("table");
  tb.innerHTML = "<thead><tr><th>文件</th><th class='col-size'>大小</th>"
    + "<th class='col-role'>说明</th></tr></thead>";
  const body = el("tbody");
  for (const f of p.files || []) {
    const tr = el("tr");
    const kb = f.size > 1024 ? (f.size / 1024).toFixed(1) + " KB" : f.size + " B";
    tr.innerHTML = `<td class="cell-mono">${esc(f.rel)}</td>
      <td>${kb}</td><td>${FILE_ROLE(f.rel)}</td>`;
    body.appendChild(tr);
  }
  tb.appendChild(body);
  box.appendChild(tb);
}
