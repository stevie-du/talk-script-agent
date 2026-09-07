const { contextBridge } = require('electron');

// 把引擎端口传给渲染进程（渲染层直接 fetch http://127.0.0.1:<port>）
contextBridge.exposeInMainWorld('talkscript', {
  isElectron: true,
});
