// 弹层：通用确认对话框。
//
// 场景切换时统一收口（closeOverlays）——修复前新建对话 / 切历史 / 打开设置
// 只关设置不关浮层，确认卡会孤儿一样盖在新内容上，点哪儿都先命中它。
//
// 这里原本还有第二张卡：分步确认的「选题确认」弹层（openConfirmPlan /
// editedPlan / bindOverlays 的三个 cf-* 绑定），2026-09-19 随功能整体移除
// （理由见 app/jobs.py 顶部）。它一走，`bindOverlays` 就没有任何东西要绑了，
// 于是连同 main.js 里的两处调用一起删掉 —— 留一个空函数只会让人以为
// "弹层还需要初始化"。

import { $ } from "./util.js";

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
    // 焦点管理：打开时把焦点送进对话框（否则键盘用户还在背景上按 Tab，
    // 看不见也按不到「确认/取消」），关闭时还给打开它的那个控件。
    const opener = document.activeElement;
    const done = v => {
      $("confirm-dialog").classList.add("hidden");
      $("cd-yes").onclick = null;
      $("cd-no").onclick = null;
      document.removeEventListener("keydown", onKey, true);
      if (opener && document.contains(opener)) opener.focus();
      resolve(v);
    };
    const onKey = e => {
      // 只在弹层真的可见时接管 Esc。若它被别的路径关掉了（切会话、其它 openOverlay
      // 复位），这里必须放行 —— 否则这个捕获阶段的监听会残留，把全局 Esc
      // 吞干净（实测连带打死「Esc 关设置」那条快捷键）。
      if ($("confirm-dialog").classList.contains("hidden")) return;
      if (e.key === "Escape") { e.stopPropagation(); done(false); }
    };
    document.addEventListener("keydown", onKey, true);
    $("cd-yes").onclick = () => done(true);
    $("cd-no").onclick = () => done(false);
    ($("cd-no") || $("cd-yes")).focus();
  });
}
