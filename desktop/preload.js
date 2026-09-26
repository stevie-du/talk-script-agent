const { contextBridge, ipcRenderer } = require('electron');

// 渲染层与引擎**同源**（http://127.0.0.1:<port>），API 通过 fetch 直连，
// 令牌由 URL 传入页面 —— 因此这里不再需要传递端口。
//
// 只暴露一个标记，供页面区分「在壳里」还是「浏览器直开」（浏览器直开时该对象不存在）。
// 修复前暴露的 `isElectron` 渲染层从未读过，属于死接口；现在补上 platform，
// 并明确它唯一的用途。
//
// updater 四个受管通道（docs/自动更新方案.md §4.3，最小面 + §4.2 的安装动作）：
//   check     —— 手动检查（设置页按钮）；返回当前状态对象，三种「没开」也照常返回
//   getStatus —— **只读**取当前状态，不触发检查。打开面板时用它：走 check 会真的
//                发一次请求，并把 6h 自动检查的时钟往后推（见 main.js 的注释）
//   restart   —— 「立即重启」（只在 downloaded 态可见的按钮）；直达 quitAndInstall
//   onStatus  —— 主进程推状态（含自动检查的进度）；返回取消订阅函数
// 状态对象的形状由主进程的七态状态机决定（state / version / percent / text…），
// 渲染层只消费事实，不在这里加工文案。
//
// restart 是 §4.3 两条通道之外补的**第三条**：方案 §4.2 要求设置页那颗
// 【立即重启】按钮「直接装，不再二次确认」，而渲染层碰不到 updater 实例
// （它在主进程），也没有第二条路可走。补一条 invoke 比把 restart 塞进
// check 的参数里诚实 —— 后者会让「取状态」和「执行安装」共用一个名字。
// getStatus 同理是**第四条**，理由一样：「取状态」与「发起检查」是两件事，
// 塞进 check 就再也分不开了（而它们的副作用差得很远）。
contextBridge.exposeInMainWorld('talkscript', {
  isElectron: true,
  platform: process.platform,
  updater: {
    check: () => ipcRenderer.invoke('updater:check'),
    getStatus: () => ipcRenderer.invoke('updater:getStatus'),
    restart: () => ipcRenderer.invoke('updater:restart'),
    onStatus: (cb) => {
      const h = (_e, s) => cb(s);
      ipcRenderer.on('updater:status', h);
      return () => ipcRenderer.removeListener('updater:status', h);
    },
  },
});
