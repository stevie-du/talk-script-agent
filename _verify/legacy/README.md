# 历史验证脚本（针对重构前的界面）

这些脚本写于渲染层还是「单文件 app.js + file:// 加载 + 全局变量」的年代，
里面依赖的东西现在都不存在了：

- `document.querySelector('.pstep')` —— 步骤条已改成 `.step` 时间线
- 页面全局变量 `busyNow` / `currentJob` / `currentResult` —— 已收敛进 `window.__ts`
- `<script src="app.js">` 的单文件入口 —— 已拆成 `renderer/js/*.js` 的 ES 模块
- 内联 `[SESSDBG]` 调试日志（`real-click-probe.js` 依赖它）

保留在这里是为了让「当时是怎么定位那个 bug 的」有据可查，**不要直接运行**
（它们会静默失效或报一堆找不到节点的错）。

现在请用：

| 脚本 | 用途 |
|---|---|
| `../verify.js` | 界面回归（44 项断言，桩 fetch，秒级） |
| `../e2e-live.js` | 真实端到端（真引擎 + 真页面 + 真落盘） |
| `../who-locks.js` | 文件占用排查（仍可用） |
| `../../tests/` | 引擎侧回归（pytest，37 项） |
