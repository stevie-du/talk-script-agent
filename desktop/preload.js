const { contextBridge } = require('electron');

// 渲染层与引擎**同源**（http://127.0.0.1:<port>），API 通过 fetch 直连，
// 令牌由 URL 传入页面 —— 因此这里不再需要传递端口。
//
// 只暴露一个标记，供页面区分「在壳里」还是「浏览器直开」（浏览器直开时该对象不存在）。
// 修复前暴露的 `isElectron` 渲染层从未读过，属于死接口；现在补上 platform，
// 并明确它唯一的用途。
contextBridge.exposeInMainWorld('talkscript', {
  isElectron: true,
  platform: process.platform,
});
