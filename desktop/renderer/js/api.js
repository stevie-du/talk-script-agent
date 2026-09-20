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
  constructor(message, status) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

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
    try {
      const j = await r.json();
      if (j && j.detail) {
        msg = Array.isArray(j.detail)
          ? j.detail.map(d => d.msg || JSON.stringify(d)).join("；")
          : String(j.detail);
      }
    } catch (_) { /* 非 JSON 响应，保留 HTTP 状态码文案 */ }
    throw new ApiError(msg, r.status);
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
  createPack: (industry, description) =>
    request("/api/packs/create", { method: "POST", body: { industry, description } }),
  // 注：「导出为 Agent 技能」的 UI 入口已删（2026-09-17），后端
  // /api/packs/<n>/export-skill 端点仍由 pytest 覆盖 —— 想恢复 UI 时
  // 把 exportSkill 加回来 + 复刻按钮 + 在 README 写明用途。
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
};
