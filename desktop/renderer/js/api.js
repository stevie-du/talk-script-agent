// 与本地引擎的 HTTP 通信。
//
// 两处关键变化：
//  1. 渲染层由引擎**同源**提供，所以不再需要拼绝对地址 —— 直接用相对路径，
//     浏览器会自动带上正确的 Host。修复前是 `http://127.0.0.1:${port}`，
//     而页面来自 file://，属于跨站，逼得服务端把 null / file:// 也放行。
//  2. 每个请求带 `X-TalkScript-Token`：令牌由主进程随机生成，经 URL 传给页面。

const qs = new URLSearchParams(location.search);
export const TOKEN = qs.get("token") || "";

export class ApiError extends Error {
  /** @param message 服务端 `detail` 里那句**人话**，原样保留（界面直接显示它）
   *  @param status  HTTP 状态码 —— 只够分「哪一类」，不够分「哪一件事」
   *  @param code    服务端 `code` 里的稳定机器可读码（pack_broken / quota_exceeded / …）。
   *                 ⚠ 判「是什么错」读这个，不读 status、也不读 message：
   *                 两个 409 说的是两件完全不同的事（P1-1），而文案改一个标点
   *                 就会让按文案匹配的判据失效。老服务不给这个键时它是 undefined。 */
  constructor(message, status, code) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code || "";
  }
}

// ── 「这次生成会被拒，因为模型没配」的判据 ────────────────────
/** 用 /api/meta 的**字段**判断（不是错误文案）。
 *
 *  它逐项镜像 app/server.py 的 `_require_model()`：
 *      mock → 放行；一条模型都没有 → 拒；没有启用项 → 拒；启用项没有 Key → 拒。
 *  之所以要在前端重做一遍：`send()` 原来靠 `/API Key|令牌/` 去**读服务端的人话**，
 *  而三条拒绝文案里只有最后一条含「API Key」，另两条（没模型 / 没启用）
 *  永远匹配不上 —— 「去配置」那一跳在最常见的新手场景下根本不触发。
 *  文案是给人看的，改一个标点就把前端判据弄失效；字段不是。 */
export function modelSetupGap(meta) {
  if (!meta || meta.mock) return "";
  if (!(meta.models || []).length) return "还没有配置模型";
  if (!meta.active_model) return "没有启用任何模型";
  if (!meta.has_api_key) return "当前模型还没配 API Key";
  return "";
}

/** 服务端拒绝文案的兜底判据（配置在别处被改过时，前端那份 meta 可能是旧的）。
 *  锚取 `_require_model()` 三条**共有**的那段「去哪里改」，不抄整句：
 *    「还没有配置模型 —— 请在「设置 → 模型接口」里点右上角「添加模型」」
 *    「当前没有启用任何模型 —— 请在「设置 → 模型接口」里打开一个模型的开关」
 *    「当前模型还没配 API Key，请在「设置 → 模型接口」里填写」
 *  ⚠ 「令牌」那一条**不在**这里 —— 它说的是「请用启动时打印的带 token 的地址打开」，
 *  设置页修不了它，跳过去只会把用户引到更没用的地方。 */
export const MODEL_SETUP_REPLY = /设置\s*→\s*模型接口|模型接口」里/;

async function request(path, { method = "GET", body, timeout = 0 } = {}) {
  const opts = {
    method,
    headers: { "Content-Type": "application/json" },
  };
  if (TOKEN) opts.headers["X-TalkScript-Token"] = TOKEN;
  if (body !== undefined) opts.body = JSON.stringify(body);
  if (timeout) {
    const ac = new AbortController();
    opts.signal = ac.signal;
    setTimeout(() => ac.abort(), timeout);
  }

  let r;
  try {
    r = await fetch(path, opts);
  } catch (e) {
    throw new ApiError(
      e.name === "AbortError" ? "请求超时" : `无法连接本地引擎（${e.message}）`, 0);
  }

  if (!r.ok) {
    let msg = `HTTP ${r.status}`;
    let code = "";
    try {
      const j = await r.json();
      // code 在**顶层**（app/server.py 的 `_error_json` 就是这么给的）；
      // 也认 detail 里带 code 的写法，将来若有人把两者合进一个对象不必再改这里。
      if (typeof j?.code === "string") code = j.code;
      const d = j && j.detail;
      if (d) {
        if (Array.isArray(d)) msg = d.map(x => x.msg || JSON.stringify(x)).join("；");
        else if (typeof d === "object") {
          msg = String(d.message ?? JSON.stringify(d));
          if (!code && typeof d.code === "string") code = d.code;
        } else msg = String(d);
      }
    } catch (_) { /* 非 JSON 响应，保留 HTTP 状态码文案 */ }
    throw new ApiError(msg, r.status, code);
  }
  if (r.status === 204) return null;
  return r.json();
}

export const api = {
  get: (p) => request(p),
  post: (p, body = {}) => request(p, { method: "POST", body }),
  del: (p) => request(p, { method: "DELETE" }),

  meta: () => request("/api/meta"),
  history: () => request("/api/history"),
  record: (id) => request(`/api/history/${encodeURIComponent(id)}`),
  removeRecord: (id) => request(`/api/history/${encodeURIComponent(id)}`, { method: "DELETE" }),
  reveal: (id) => request(`/api/history/${encodeURIComponent(id)}/reveal`, { method: "POST", body: {} }),

  generate: (params) => request("/api/generate", { method: "POST", body: params }),
  job: (id, full = false) => request(`/api/jobs/${encodeURIComponent(id)}${full ? "?full=true" : ""}`),
  cancel: (id) => request(`/api/jobs/${encodeURIComponent(id)}/cancel`, { method: "POST", body: {} }),
  rewrite: (id, index, feedback) =>
    request(`/api/jobs/${encodeURIComponent(id)}/rewrite_segment`,
      { method: "POST", body: { index, feedback: feedback || null } }),

  pack: (name) => request(`/api/packs/${encodeURIComponent(name)}`),
  // 建包 = 后台作业（P1-43）：返回 {job_id}，进度与结果用 job(id) 轮询取。
  createPack: (industry, description) =>
    request("/api/packs/create", { method: "POST", body: { industry, description } }),
  // 注：「导出为 Agent 技能」已整个移除（UI 入口 2026-09-17 撤，后端端点与
  // app/export_skill.py 2026-09-22 删）。git 历史里有完整实现，想恢复从那里捞。
  undraft: (name) => request(`/api/packs/${encodeURIComponent(name)}/undraft`, { method: "POST", body: {} }),

  config: () => request("/api/config"),
  saveConfig: (body) => request("/api/config", { method: "POST", body }),
  resetConfig: (fields) => request("/api/config/reset", { method: "POST", body: { fields } }),
  // 模型列表：增删改 + 切换当前。`id` 空 = 新增，非空 = 改那一条；
  // `api_key` 空串 = 保持不变（与 /api/config 同一个语义）。
  saveModel: (body) => request("/api/models", { method: "POST", body }),
  deleteModel: (id) => request("/api/models/delete", { method: "POST", body: { id } }),
  activateModel: (id) => request("/api/models/activate", { method: "POST", body: { id } }),
  packFile: (name, rel) =>
    request(`/api/packs/${encodeURIComponent(name)}/file?rel=${encodeURIComponent(rel)}`),
  testConfig: (body) => request("/api/config/test", { method: "POST", body }),

  // 情报（今日选题 / 情报源）。三个端点分工：
  //   today   只读本地文件，空或坏返回空结构（**不抛**）—— 抓取失败不该让页面报错
  //   refresh 立即重抓，走后台作业（独立并发额度，不占生成的名额）
  //   ignore  忽略一条，**只影响今天**（明天同题还会回来）
  intelToday: (pack) => request(`/api/intel/today?pack=${encodeURIComponent(pack)}`),
  intelRefresh: (pack) => request("/api/intel/refresh", { method: "POST", body: { pack } }),
  intelIgnore: (pack, key) =>
    request("/api/intel/ignore", { method: "POST", body: { pack, key } }),
};
