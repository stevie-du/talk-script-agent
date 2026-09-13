// 弹层：通用确认 + 分步选题确认。
//
// 场景切换时统一收口（closeOverlays）——修复前新建对话 / 切历史 / 打开设置
// 只关设置不关浮层，确认卡会孤儿一样盖在新内容上，点哪儿都先命中它。

import { $, esc, toast } from "./util.js";
import { abort, confirmPlan, reselect } from "./jobs.js";

export function closeOverlays() {
  document.querySelectorAll(".overlay:not(.hidden)").forEach(o => o.classList.add("hidden"));
}

export function openOverlay(id) { $(id)?.classList.remove("hidden"); }
export function anyOverlayOpen() { return !!document.querySelector(".overlay:not(.hidden)"); }

export function appConfirm(title, msg) {
  return new Promise(resolve => {
    $("cd-title").textContent = title;
    $("cd-msg").textContent = msg;
    openOverlay("confirm-dialog");
    const done = v => {
      $("confirm-dialog").classList.add("hidden");
      $("cd-yes").onclick = null;
      $("cd-no").onclick = null;
      resolve(v);
    };
    $("cd-yes").onclick = () => done(true);
    $("cd-no").onclick = () => done(false);
  });
}

/** 分步确认卡。plan 来自后端；params 用于解释「为什么是这个角度」。 */
export function openConfirmPlan(plan, params = {}) {
  $("cf-angle").value = plan.angle || "";
  $("cf-hooktype").value = plan.hook_type || "";
  $("cf-hookline").value = plan.hook_line || "";
  $("cf-cta").value = plan.cta || "";
  $("cf-points").value = (plan.points || []).join("\n");

  // 决策可解释：把「模型按什么选的」摆出来，而不是只给一个待填空的表单
  const bits = [];
  if (plan.hook_type) bits.push(`钩子类型「${plan.hook_type}」来自本包钩子库`);
  if (params.segment) bits.push(`细分「${params.segment}」`);
  if (params.audience) bits.push(`受众「${params.audience}」`);
  if (params.duration) bits.push(`目标 ${params.duration}s`);
  const n = (plan.points || []).length;
  if (n) bits.push(`${n} 个要点（超配额时模型会压缩）`);
  $("cf-why").textContent = bits.length
    ? `选题依据：${bits.join(" · ")}。可直接编辑下方任意字段再确认。` : "";

  openOverlay("confirm-overlay");
}

export function editedPlan() {
  return {
    angle: $("cf-angle").value.trim(),
    hook_type: $("cf-hooktype").value.trim(),
    hook_line: $("cf-hookline").value.trim(),
    points: $("cf-points").value.split("\n").map(s => s.trim()).filter(Boolean),
    cta: $("cf-cta").value.trim(),
  };
}

export function bindOverlays() {
  $("cf-continue").onclick = async () => {
    const plan = editedPlan();
    if (!plan.points.length) { toast("至少保留一个要点"); return; }
    $("confirm-overlay").classList.add("hidden");
    await confirmPlan(plan);
  };
  $("cf-reselect").onclick = () => {
    $("confirm-overlay").classList.add("hidden");
    reselect();
  };
  $("cf-cancel").onclick = () => {
    $("confirm-overlay").classList.add("hidden");
    // 停轮询 + 解锁 + 通知后端放弃（原先只置空 currentJob，会锁死界面）
    abort();
  };
}
