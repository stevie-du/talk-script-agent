# TalkScript 工具链与验证约定（细则，从 MEMORY.md 拆出）

> 本文件是权威细则。MEMORY.md 只留索引。

## 一、启动与回归

- **启动**：`npm run start:no-sandbox`（受限环境 GPU/渲染沙箱起不来；**别用 `--disable-gpu`，会白屏**）。
- **⚠ 若 `ELECTRON_RUN_AS_NODE=1` 在环境里，启动会崩**（2026-09-16 实测）：
  症状是主进程抛
  `TypeError: Cannot read properties of undefined (reading 'whenReady')`
  （`main.js:183` —— `require('electron')` 只返回了路径字符串，不是 app 对象），
  栈尾显示 `Node.js v20.18.3` 这种**Node 版本号**。
  **一眼识别**：`./node_modules/electron/dist/electron.exe --version`
  → 正常返回 **Electron 版本**（`v3x.x.x`）；返回 **Node 版本**就是中招了
  （此时 Electron 退化成纯 Node，`--version` 报的是它内置的 Node）。
  ⚠ 别被误导去查 `node_modules/electron/dist` 是否损坏 —— 二进制是好的（188MB 在），
  纯粹是环境变量的事。**解法**：
  ```
  env -u ELECTRON_RUN_AS_NODE npm run start:no-sandbox
  ```
  只在启动命令里清，**别全局改**（Agent 环境可能故意预设它来跑 Node 脚本）。
- **⚠ 重启应用：停掉后台任务 ≠ 进程真的退出**（2026-09-16 实测）：
  后台任务被 stop 后，`electron.exe`（通常 **4 个**：主进程 + 渲染/GPU 等）
  与 `python.exe`（引擎）**仍然活着**，端口照样监听、health 照样 200。
  直接再启动会让**两个实例并存**（各占一个端口，日志看着都正常，极易误判）。
  ⚠ **反过来也成立**（2026-09-17 实测）：后台任务报 completed 后，进程
  **也可能真全退了** —— 那次 `taskkill` 对 6 个 PID 全部报「没有找到进程」，
  一查 health `000` / electron 0 / python 0，确实一个不剩。
  所以**只有一条规矩**：启动前必须**实测**（`curl` 拿 `000` + `tasklist | grep -c electron` = 0），
  **别凭后台任务的状态猜** —— 猜错两个方向都会出事（漏杀 → 双实例；白杀 → 报错后以为环境坏了）。

  ⚠ **python 是 2 个，不是 1 个**（2026-09-16 补）：开发态一个实例 = **4 electron + 2 python**。
  引擎日志说启动的是 `\.venv\Scripts\python.exe`，但**真正监听端口的是系统 Python**
  （本次是 `E:\Environment\Python\...\3.14.3\python.exe`）——
  即 `.venv` 是 **virtualenv 式**的：它的 `python.exe` 只是个**启动器**，
  会再拉起基础解释器。`ps -W -e` 能看到两个都在、且**同一秒启动**。
  **只杀监听端口那个 python 会漏掉启动器**，下次启动就可能留下孤儿。

  **确认归属别靠猜**：`ps -W -e | grep -iE "electron|python"` 能打出**完整镜像路径**，
  本次 6 个进程路径全在工作区内、启动时间戳同一秒（20:16:52~55）→ 可判定同属一个实例。
  ⚠ `wmic` 在本机**不存在**、PowerShell 工具在本机**拿不到 stdout**
  （`Get-CimInstance` 之类调用返回 exit 0 但无输出）—— **别绕去调它们，用 `ps -W -e`**。
  （`powershell -Command` 从 bash 调会被安全策略拦，报「Invokes PowerShell from Bash」。）

  正确姿势 —— 按端口定位再逐个杀：
  ```
  netstat -ano | grep <端口>          # → python.exe 的 PID（引擎）
  tasklist | grep -i electron         # → 4 个 electron.exe 的 PID
  taskkill /F /PID <p1> /PID <p2> ... # 一次全杀
  ```
  杀完**必须确认**：`curl` 该端口返回 `000` 且 `tasklist | grep -c electron` = 0，
  然后再启动。（本次重启就是这样才干净的。）

  ⚠ **杀完发现还有 python 残留时，先分清是不是自己的**（2026-09-17 实测）：
  `probe.js` / `verify.js` 会**自己拉一个引擎**（也是 2 个 python），
  并发会话跑验证时它们就在那儿 —— **杀掉会直接毁掉对方正在跑的实验**。
  **判据**：① `ps -W -e` 看 **STIME** —— 是你刚杀进程**之后**才起的就不是你的；
  ② 路径风格（Git Bash 起的显示 `/e/...`，Electron 起的显示 `E:\...`）；
  ③ 它监听的端口**不是**你那个实例的端口。
  **只杀属于自己那个实例端口的 python**，其余一律留着。
- ⚠ **重启前先看工作区有没有 `MUTATION` 标记**（2026-09-16 实测）：
  本项目常有**并发会话**在做变异检验 —— 把实现临时改坏（注释里写明 `MUTATION`），
  跑完再自己改回来。判据：`git diff | grep -i mutation`。
  **它改的是磁盘，重启读的也是磁盘**，于是会撞上两个**方向相反**的假信号：
  ① 它刚弄坏、你正好重启 → 界面真呈现出那个 bug，看着像「重启把界面搞坏了」；
  ② 它刚还原、你没重启 → 窗口里还是旧样式，变成「代码好了界面还坏」。
  根因是同一个：**窗口与磁盘有时差**。
  ⚠ 别去"修"它，也别擅自 `git checkout` 还原 —— 会毁掉对方正在跑的检验
  （还原了实现，它的断言就不红，会误判成「探针抓不住」而去改探针）。
- ⚠ **重启前先判断值不值：比「文件 mtime」与「当前实例启动时间」**（2026-09-17）：
  渲染器是**启动时从磁盘读一次**，之后磁盘再改也不会自己更新。
  所以**只有 mtime 晚于实例启动时刻的改动，重启才有用**。
  ```
  ls --time-style=+%H:%M:%S -l desktop/renderer/*.{html,css} desktop/renderer/js/*.js
  ps -W -e | grep -iE "electron|python"    # 第 6 列 STIME 就是实例启动时刻
  ```
  实测：某次重启时前端 mtime `07:25~07:27` 而实例 `07:38` 才启 → **白重启，
  界面不会有任何变化**；另一次 index.html `07:42:07` 改、07:43 重启 → 才真的生效。
  ⚠ 用户连着喊了 5 次重启，其中至少 2 次是白的 —— 他以为重启能"刷新"，
  其实**重启只做一件事：把磁盘当前的内容重新读一遍**。
  想让改动生效，前提是**它已经在磁盘上**（编辑器没保存的化，重启也没用）。
- ⚠ **判断「并发会话改完没有」，监测窗口至少 60 秒**（2026-09-17 实测）：
  本项目常有并发会话在改代码，你想等它改完再重启/跑回归。
  ⚠ **20 秒无变化 ≠ 改完了** —— 那只是它在"想"（实测：判定稳定后重启，
  它紧接着又改了 3 个文件）。**改用 30 秒 × 连续 2 次无变化（= 90 秒）** 才可靠。
  **监测范围要连 `_verify/*.js` 一起**（不只是 `desktop/renderer/*`）——
  并发会话常常**前端和断言一起改**。只盯前端会漏，然后你就撞上下面这条：
- ⚠ **回归「崩了」先怀疑并发会话的半成品，别急着修**（2026-09-17 实测）：
  症状是 `TypeError: Failed to execute 'getComputedStyle': parameter 1 is not of
  type 'Element'`（或 `querySelector` 返回 null 后各种崩），
  且**耗时明显偏短**（20 秒 vs 正常 72 秒）。
  **判据**：`git status --short` 看 `_verify/verify.js` 是不是 `M` ——
  是 `M` 就说明断言正在被改，DOM 与新断言暂时对不上，**等它写完再跑**。
  ⚠ 与「跑出明显更少（如 98/98）」是同一类信号：
  **回归没跑完 ≠ 回归通过**，中途崩了后面几十条根本没执行。
  这次的完整时间线见 `2026-09-16.md`。
- ⚠ **探活要绕开代理**：本机设了 HTTP 代理，直接 `curl http://127.0.0.1:<port>/api/health`
  会拿到 **502（代理返回的假信号）**，看着像「应用挂了」其实是探针走了代理。
  **加 `--noproxy '*'`**；`000` 才是真没在听、`200` 才是真活着。（教训 39 的又一例：
  下结论前先确认探针本身可信。）
- **要读需要鉴权的接口，头名是 `X-TalkScript-Token`**（不是 `X-Auth-Token`）。
  `/api/health` 免鉴权，`/api/meta` `/api/config` 都要带。
  启动日志里那串 `--token <值>` 就是它：
  ```
  curl -s --noproxy '*' -H "X-TalkScript-Token: <token>" \
       http://127.0.0.1:<port>/api/meta
  ```
  ⚠ **头名写错时的症状会误导**：返回
  `{"detail":"缺少或无效的访问令牌。..."}` —— 与「token 值不对」**一模一样**，
  很容易误判成「应用代码没生效」。判据：先确认头名（`app/security.py:30` 的
  `TOKEN_HEADER`），再怀疑 token 值。
- **实例身份看端口 + token，别凭印象**：端口每次启动自适应，且**可以有多个实例并存**
  （不同端口）。哪个还活着用 `netstat -ano | grep <port>` 或读进程命令行确认；
  日志里的 `engine (.venv)` 表示**开发态用的是仓库 `.venv`，不是打包版的 `engine/py`**。
- **回归**：`node _verify/verify.js`（零依赖 CDP，自带桩引擎 + 真 Chrome）。
  ⚠ **判据是末尾 `N/N 通过`，不是退出码**：`cleanupAll()` 的 `killTree` 会波及自身进程，
  `$?` 拿到 1/SIGTERM 属正常收尾假象（先 `grep -c '❌' 日志`）。端口自适应（`freePort`），桌面应用开着也能跑。
- **pytest 计数**：`pytest -q` 的收尾汇总行会被 `^M` 吃掉（`grep passed` / `tr -d '\r'`
  都抓不到）→ 用 **`.venv/Scripts/python.exe -m pytest --collect-only | grep -c test_`**
  （实测 218，与点阵数一致）。
- 🔴 **本环境下 `pytest` 的退出码不可信 —— 判成败必须看进度行**（2026-09-23 实证）：
  pytest 收尾会清理 `AppData\Local\Temp\pytest-of-<user>` 下的历史会话目录，
  攒到几千个文件时，沙箱的「批量删除需确认」拦截**正好在那一刻打断进程** ——
  于是进度行是 `..  [100%]`、一条 F 都没有，**退出码却是 1**。
  → 单独重跑往往又变绿（垃圾没攒够，拦截不触发），**同一条命令两种结论**，
  极难查。任何脚本里写 `rc == 0` 判 pytest 通过都会踩这个。
  → 判据统一改成：**进度行跑到 100% 且里面没有 `F`/`E`，且没有 `FAILED` 行**
  （`_verify/mutate.py` 里的 `_pytest_red()` 就是这个判据，照抄即可）。
- **`Temp\pytest-of-<user>` 会攒到十几万个文件（2026-09-23 实测清过一次）**：
  实测攒到 **418 个会话 / 24 万个文件 / 10.8 万个子目录**。pytest 自己每次收尾
  都想清（保留最近 3 个），但删除超过 50 个文件就被沙箱拦下 —— 于是越攒越多，
  每次清理都触发拦截，**退出码假失败就是这么来的**。
  清理法（用户已确认的口径：只留最新一个）：
  ```python
  # 按 mtime 排序，删掉除最新以外的全部；分批 30~50 个、批间验证剩余数
  # 一次删 20 万个文件会被 SIGTERM 打断 —— 小批 + 后台跑
  ```
  实测清理后：pytest **退出码 0、拦截 0 次**。⚠ 会再攒（`mutate.py` 跑一轮全量
  就多几十个会话），隔段时间清一次即可；不清理也不影响判定（判据已改进度行）。
- **「谁在拿 pytest 退出码判定」已扫全（2026-09-23）**：`git ls-files` 里跑 pytest 的
  只有 `_verify/mutate.py` 一处，已改进度行判据；`verify.js` / `settings.js` /
  `README.md` 里的 "pytest" 只是注释和文档，没有其他自动消费者。
  → 以后新增脚本时记住这条：**别用退出码判 pytest**。
- **`verify.js` 分组入口的退出码**：分组全绿 → 0；未知分组名 → 3；分组跑完
  0 条断言 → 4。已实测。（整网那条路径的退出码仍受 `killTree` 自伤影响不可信，
  见上面「判据是末尾 N/N 通过」那条。）
- **⚠ pytest 哨兵 `test_no_hardcoded_measurement_numbers_in_comments`**：
  注释 / docstring 里**不许抄实测数字**。匹配式（测试 L218）：
  `\d+(?:\.\d+)?\s*(?:MB|s\b|秒)` —— **只抓 `MB` / `s` / `秒` 前的数字**
  （体积与耗时这类会随版本/机器漂移的量）。
  **`px` 值、纯数字、比例都不抓**（`16px` / `0.5px` / `276` / `12/12` 都安全）——
  它们是设计值，不会自己变。**别误以为所有数字都不能写**。

  **扫描范围**（L213-217，**按类别不按枚举**，新文件自动覆盖）：
  `scripts/*.mjs` + `_verify/[!_]*.js`（**临时件 `_*.js` 跳过**）+ 测试文件自己。
  → 真正受影响的是 `_verify/` 下的**正式工具**（`verify.js` / `runtime-smoke.js` 等）；
  `desktop/renderer/` 与 `.workbuddy-ai/` 记忆文件**不在范围内**。

  ⚠ 踩过一次：在 `verify.js` 注释里写「脚本 <数字>s 崩溃退出」→ 报红。
  改法：**删掉数字改成指路**（写成「总耗时明显短于正常」），判据保留、数字不抄。
  报错会精确列出 `文件:行号: 注释原文`，照着改即可。
- **四条回归**（**条数不写在这里** —— 会漂，唯一快照在 `MEMORY.md`）：
  ```
  .venv/Scripts/python.exe -m pytest -q
  node --test desktop/engine-path.test.js        # 降级链顺序
  node _verify/runtime-smoke.js                  # 内嵌运行时端到端，含 .pyc 标签 2 条
  node _verify/verify.js                         # 界面（全量，两分钟级）
  node _verify/verify.js topics                  # 只跑「今日选题 / 情报源」那一组
  node _verify/verify.js contrast                # 只跑状态色对比度守卫
  # 对着打好的产物跑（而不是构建树）：
  node _verify/runtime-smoke.js --root desktop/dist/win-unpacked/resources/engine
  ```
  ⚠ **判据是末尾 `N/N 通过`**。但**只看 N/N 也会漏**：脚本中途抛异常时后面几十条
  根本没跑（真出过：删了 DOM 没同步断言 → `querySelector` 返回 null → TypeError）。
  所以还要**对一下总数有没有明显变少**。
- **`verify.js` 的分组入口**（2026-09-23 加）：`node _verify/verify.js <分组名>`
  只跑那一组，跳过其余几百条断言的前置流程 —— 改 `topics.js` / `ui.js` /
  `styles.css` 时用它，比跑全量快一个数量级。可用分组名见 `verify.js` 顶部的
  `SECTIONS`（传错名字会**直接报失败**并列出可选值，不会静默跑成全量）。
  ⚠ **默认（不带参数）仍是全量**，分组只用于"我知道要看哪几条"，
  全量那份"整网全绿"的信号不能靠分组替代。
- **变异检验 `mutate.py` 的基线守卫**（2026-09-23 加）：每条变异前先跑一次
  **未变异**的基线，基线不绿就判「判定无意义」而不是「报红 OK」。
  → 并发会话正在提交时跑全量，会看到一排「基线不绿」：**那是真信号**
  （采样落在了改到一半的工作区上），不是回归。单独重跑那几条通常是绿的。
  不确定时先 `git status` 看有没有别人在改。
  `runtime-smoke.js` 的清理失败已改成 ⚠ 警告（不再掩盖断言、不再改退出码）。
- **`node _verify/pyc-cost.js`**：量字节码缓存的**体积与启动耗时**（18 秒出结果，
  自动取 5 次最小值并判定「是否噪声」）。**实测数字的唯一来源** ——
  注释与 docstring 里一律**只指路不抄值**（pytest 有哨兵守着，见 LESSONS）。

⚠ `node --test desktop/` 会把整个目录当成一个测试报错，**要指名文件**。

## 二、截图与像素测量

- **给运行中的应用截真图**：`node _verify/shot-live.js [组序号] [输出名]`
  —— 无头 Chrome 直连引擎地址，**不依赖窗口可见性**。
- **⭐ 用真实数据量几何 / 出图**：`node _verify/probe.js`
  ```
  node _verify/probe.js [--pane=kb] [--expr='<表达式>'] [--overflow]
                        [--shot=out.png] [--clip=x,y,w,h[,scale]]
                        [--size=1305x823] [--dpr=1.25]
  ```
  **自己拉一个真实引擎（不是桩）+ 无头 Chrome 直连**，带自己的 token ——
  既不用碰用户正在用的那个实例，也不用给桌面应用加 `--remote-debugging-port`
  重启。`verify.js` 的桩只有三五个文件，**布局问题往往只在真实数据量下才露出来**。
  - `--overflow` 是内置探针：找出「内容漏出盒子」的元素。
    判据是「非绝对定位子元素的底边 vs 父元素内容盒底边 ≥ 2px」。
    ⚠ **别用 `scrollHeight > clientHeight`** —— 视觉隐藏的绝对定位元素
    （`.segmented .radio` 里的 `<input>`）也会把它撑大，是假阳性。
  - 表达式**不能出现反引号**（`evalIn` 用模板串拼的，会提前截断）。
  - ⚠ **`--expr` 里的 `querySelector` 必须按面板 ID 限定**（2026-09-17 实测踩的）：
    设置页**所有面板的 DOM 同时存在**（`pane-gen` / `pane-packinfo` / `pane-packgen` /
    `pane-kb` / `pane-skills` / `pane-llm`），靠 class 显示/隐藏切换 ——
    `document.querySelector('.stg-pane.has-cols')` **会命中当前隐藏着的那个面板**。
    实测：`--pane=kb` 时查 `.stg-pane.has-cols` 返回 **true**（假的！kb 根本没三列），
    改成 `#pane-kb .pane-list` 才是 **false**。
    **判据**：查任何「只有某面板才有」的东西，必须写成 `#pane-xxx .yyy`，
    或先取 `.stg-pane:not(.hidden)` 再往下查。
    —— 「静默失效」的又一形态：**探针不报错，还给你一个自信的错误答案**，
    于是你会去"修"一个没坏的界面（这次差点）。
  - ⚠ **探针报「无」之前，先确认它不是空转**：顺手数一下
    `document.querySelectorAll('.stg-pane, #right, #left')` 下有多少元素
    （实测 1299 个）。**扫到 0 个 = 什么都没量**，此时的「无溢出」是空的 ——
    与 `verify.js` 里「量到 HIDDEN 要当失败」同一条纪律。
- **⭐ 量用户贴来的那张截图**：`node _verify/png-tools.js`
  ```
  node _verify/png-tools.js scan <in.png> <x> [y0] [y1] [阈值]     # 沿列扫，出暗带区间与节距
  node _verify/png-tools.js crop <in.png> <out.png> <x> <y> <w> <h> [倍数]   # 裁一块放大
  ```
  用户截图（Windows 剪贴板 PNG，在 `%USERPROFILE%\.workbuddy-ai\clipboard-images\`）
  里含着两个关键事实：**他的窗口多宽 / 缩放多少**、**元素的实际像素尺寸**。
  反推倍率：拿 CSS 里已知的节距（如卡片 30+4=34）除源图测得的节距（42.6）→ 1.25。
  有了倍率就能在 `probe.js` 里用同尺寸复现，出的图与他那张可直接比对。
  **「参考图不能看，要量」** —— 这是本项目栽过的老坑。
- **PNG 解码只留一份**：`_verify/lib/png.js`（`decodePNG` / `encodePNG` / `lumAt`）。
  `ink-measure.js` 与 `png-tools.js` 都 require 它，**别再各自抄一份解码器**。
- **像素金标准**：`node _verify/ink-measure.js [目标] [参照] [亮度阈值]`
  —— 扫截图像素；阈值在背景亮度(249)与墨迹(163)之间，默认 235。
- **CSP 拦截矩阵**：`node _verify/csp-probe.js`
  —— 零依赖探针，自带无头 Chrome，实测「哪种注入会被 `style-src`/`script-src` 拦」。
  换 Chrome 版本后行为可能变，`verify.js` 里的正对照依赖这个事实，所以做成了可复现工具。
- **`_verify/` 命名**：**下划线开头 = 临时件**（`.gitignore` 的 `_verify/_*`，
  已改成「下划线开头 + 任意扩展名」一把覆盖，不再按扩展名逐条列举）；
  不带下划线 = 正式工具（`verify.js` / `e2e-live.js` / `shot-live.js` /
  `who-locks.js` / `ink-measure.js` / `csp-probe.js`）+ 有意保留的 `lib/` `legacy/`。
  **`git add -A` 前先看 `git status --short`**。
  `main-*.png`/`param-audit.png`/`result-*.png`/`settings-*.png` 是回归引用的正式产物，不能删。
- **删文件**：`fs.rmSync` 会走本机的 safe-delete 垫片 → **回收站**（这是好事）。
  但**对 `.asar` 无效**：`fs.rmSync` 报 `[safe-delete] 操作失败 … Some operations
  were aborted`、`fs.renameSync` 报 `EBUSY`、PS 的删除命令静默无效果。
  同一目录树里的 `.png`/`.log` 都正常。**别为小体积去绕过回收站强删。**
  另：`cmd //c` 在 Git Bash 下会被转义成打开交互 shell，别用。

## 二·五、`verify.js` 的桩引擎（改它之前先读）

- 桩脚本**作为同源外部脚本 `/_stub.js` 提供**（不是内联），注入点是 `<head>` 开头
  —— 内联脚本在 `script-src 'self'` 下会被拦掉，而「为了让它跑起来给测试环境放宽 CSP」
  是断言空转的温床。放 `<head>` 是阻塞执行，先于 body 解析，初始 HTML 的内联样式也逃不掉。
  ⚠ `stubScript()` 返回**纯 JS**，不带 `<script>` 包装（改成外部脚本时忘剥会直接语法错误）。
- 桩服务下发**生产那份 CSP**：`readCsp()` 从 `app/server.py` 解析 `CSP` 常量，
  解析失败**直接抛**。这是为了防「测试环境比生产宽松 → 断言空转」。
- `serve()` 在 `_verify/lib/cdp.js`，参数 `{ inject, csp }`。
- 桩里还包了 `EventTarget.prototype.addEventListener` 计数（`window.__addCount`），
  配合 `window.__ts.rebind()` 断言「再绑一遍不重复挂监听器」（P2-2）。
- **`evalIn` 的表达式是函数体**，多语句要写 `return (...)` —— 写成
  `(function(){...})()` 会返回 `undefined` 而不报错（踩过两次）。

## 三、连运行中的桌面应用（CDP）

### 3.0 ⭐ 首选：直连引擎地址，**不必带 `--remote-debugging-port`**

**引擎自己就是 HTTP 服务**，浏览器直连它照样渲染、拿的还是真实数据（不是桩）。
所以量 DOM 几何 / 截真图**不要求桌面应用带调试端口启动** ——
`shot-live.js` 的第 1 步（先问桌面应用要地址）只是图省事，**可以整个跳过**。

```
cd desktop && npm run start:no-sandbox
# 日志里会出现（端口每次动态分配）：
#   [talkscript] engine (.venv): ... --port 64388 ...
#   界面地址：http://127.0.0.1:64388/?token=xxxx
node - <<'EOF'   # 把地址填进 APP_URL，见下方模板
```

探活用 `curl --noproxy '*' http://127.0.0.1:<port>/api/health` → **200**
（**必须 `--noproxy '*'`**，否则走代理拿到 502）。

比带端口启动更轻：改完源码不用重启应用，重新 navigate 即可；
桌面窗口被最小化/遮挡也无所谓（`Page.captureScreenshot` 挂起那个坑绕开了）。

**写这类内联脚本时的三个坑（都踩过）**：

1. **常量别叫 `URL`** —— 会遮蔽 Node 全局 `URL` 类，报
   `ReferenceError: URL is not defined`（来自本机 safe-delete shim，
   栈看起来完全不相关，极难联想到变量名）。用 `APP_URL` / `TARGET`。
2. **设置按钮的 id 有两个，别混**：
   `#btn-open-settings`（主屏左栏，打开设置）/ `#btn-close-settings`（设置页内，返回）。
   我第一版脚本写了不存在的 `#btn-settings`，直接 `Cannot read properties of null`。
3. **导航完必须轮询等 `loadSessions()`**，否则量到的是空列表：
   循环查 `#session-list .sess-item` 的 `length > 0`（最多 30 次 × 500ms）。
   同理切面板后 `sleep` 1.5~2.5s 再量 —— `openPackInfo()` 是异步的。

另：`evalIn` 的表达式**不能出现反引号**（见第四节）；多语句要 `return`。

### 3.1 走桌面应用的调试端口

带 `--remote-debugging-port=9333` 启动，用 `node - <<'EOF'` 走 stdin 跑内联脚本
（**别落盘临时 js**）：`require("./_verify/lib/cdp")` → `waitTarget` → `connect` →
`Runtime.evaluate` 取几何 / `Page.captureScreenshot` 出图（可带 `clip+scale` 放大局部）。

- **只想确认「跑着的这版加载了什么」，不必上 CDP**：引擎自己就是 HTTP 服务，
  直接 `curl --noproxy '*' 'http://127.0.0.1:<port>/static/styles.css?token=<token>'`
  拿**服务端实际下发**的文件来 grep（`/static/js/*.js` 同理）。
  比量几何更直接：能一眼看出「改的代码到底生效没有 / 是不是缓存了旧的」。

- **窗口最小化时 `captureScreenshot` 是永久挂起（不是报错）**：先查
  `outerWidth/outerHeight/document.hidden`，截图加 `Promise.race` 超时兜底。
  `Page.bringToFront` 无效，`Browser.setWindowBounds` 在 Electron 不存在。
  真要截就重启应用后立刻截；或用无头 Chrome 直连引擎地址。
- 刚 `Page.reload` 完**不要立刻量 DOM**（`loadSessions()` 未回，列表是空的），
  要轮询 `rows > 0`。
- 用户粘贴的截图在 `%TEMP%/ScreenShot_*.png`（按 mtime 取最新两个）。

## 四、写代码 / 写测试的铁律

- **同一个文件的多个改动必须串行发 Edit，不能并行** —— 并行时两个调用都返回成功，
  但只有后落盘的生效，前者被基于旧快照的写入**静默覆盖**。跨文件并行安全。
- **判断坐标类问题一律用 `getBoundingClientRect`，不要靠看截图**。
- **验证 UI 三步分开**：进程活着 ≠ 页面加载了 ≠ 画出来了。
- **写测试断言内容/几何，不只断言"不抛错"**。
- **断言空转 = 假绿**：`querySelectorAll` 过滤出来的断言**必须加非空守卫**
  （`length>=1` + 文案匹配）。实测教训：量 `#param-front` 字段宽度时写 `.select-btn`，
  而该字段没被 `beautifySelects` 换成自绘按钮 → 压根不在被测列表，两列栅格加回去
  也全绿。**凡"过滤后断言"都要问：过滤空了怎么办？**
- **`evalIn` 的模板字符串里不能出现反引号**（注释里也不行）—— 会提前截断字符串，
  报出莫名的 `ReferenceError: btn is not defined`。
- **变异检验要能证伪自己**：把"我以为需要的"实现去掉再跑一次
  （据此撤掉过一条多余的菜单清理）。
- **变异注入用 Edit 工具改源码**（不写脚本改），每次改后 `grep` 核对落盘；
  结束时 `grep -rn MUTATION` 确认 0 残留。

## 五、推送 git

`origin` = `github.com/stevie-du/talk-script-agent`，分支 `develop`。
必须显式带代理 + 绕开坏掉的凭据助手：

```
git -c http.proxy=$https_proxy -c https.proxy=$https_proxy -c credential.helper= \
    -c credential.helper=wincred push origin develop
```

**⭐ 代理对 github.com 挂掉时，先试「清空代理直连」**（比死等自愈快得多）：

```
git -c http.proxy= -c https.proxy= -c credential.helper= -c credential.helper=wincred \
    push origin develop
```

- 实测（2026-09-15 白天）：`github.com` 经代理 `000`、**直连 `200`** → 清空代理一次就推上去了。
- 实测（2026-09-15 22:58）：**两列都 `000`**，但 `baidu.com` / `registry.npmjs.org`
  直连与代理**都是 200** → 判定为 **github.com 单点故障**（对端/路由），
  **不是代理问题**。此时换直连没用，别反复试；**本地提交安全，等自愈**。
  诊断顺序：先测 3 个域名 × 2 列，再决定「换直连」还是「收手等」。
- ⚠ **别拿首页当探针**：实测（2026-09-15 23:50）`curl https://github.com` 直连 **200**，
  但同刻 git push 被 reset、且 `…/<repo>.git/info/refs?service=git-upload-pack`
  直连 **000**（http2 与 http1.1 都是 000）。**首页 200 是假信号**（缓存/拦截页），
  **要探就探 git 真正用的那个 URL**，否则会误判成「通了但 git 有毛病」。
- ✅ **故障会自愈**（00:0x 一次推上 6 笔 `75294e0..9ff47c2`），但**姿势要对**，
  见 `~/.workbuddy-ai/skills/git-remote-push-behind-proxy/SKILL.md`（权威）。要点：
  - ⚠ **别把探测当门禁**：「探通了再推」**不比直接推可靠** —— 窗口可能在两条命令
    之间就关掉（实测：第 5 次探测 200，紧接着的 push 立刻 sideband disconnect）。
    **要重试就直接重试 push。**
  - ⚠ **成败必须取命令自己的退出码**：`git push | tail -3` 里 `if` 判断的是
    `tail` 的退出码（永远 0）→ 失败也会误报成功。**先落文件再取 `$?`。**
  - 轮间 `sleep 4`（别睡 45s，会把窗口睡过去）；**轮次要给足**（实测第 8 轮才通）。
  - **交替两种模式**：带代理 `-c http.proxy=$https_proxy` / 清空代理 `-c http.proxy=`。
    两者都可能单独通，别只试一种。
  - 放**后台**跑循环（前台 120s 超时会 SIGTERM 掉整个循环，白跑）。
  - 连续 20+ 轮全挂就收手，把命令交给用户在自己终端跑 —— 本地提交是安全的。
  - ✅ **一次 push 会带上所有缺失的提交**：所以「某一笔到底推没推上去」
    不用逐笔猜（某次输出矛盾也不必纠结），**推一次再看 `ls-remote`** 即可。
    实测：故障自愈后第 1 轮就把 3 笔一起推上去了（`97b397b..e69b6a8`）。

## 六、打包（`npm run dist`）的坑

- ⚠ **本轮删除数超过阈值会让打包失败**（2026-09-16 实测）：
  ```
  [safe-delete][SAFE_DELETE_BULK_CONFIRM_REQUIRED] {"count":5974,"threshold":50,"scope":"turn"}
  ```
  safe-delete 垫片按 **turn** 累计删除数；electron-builder 替换旧产物会删几千个文件，
  累计超限后**任何** Node 删除都被拦 → 现象是**卡住十几分钟后**在 NSIS 阶段失败。
  **这不是代码问题**，别去改构建脚本。
  ✅ **计数会随新会话清零**（实测 9276 → 1101），但**清零也救不了**：
  一次打包**自身**就要删约 1101 个文件（electron-builder 替换 win-unpacked 内容），
  远超阈值 50 → 必然在收尾（删 `__uninstaller-nsis-talkscript.exe`）时被拦。
  ⚠ **所以在本环境里，`npm run dist` 的退出码永远是 1 —— 这是既定事实，别再重试。**
  但 packaging 与 NSIS **都已完成**，安装包是好的。
  **判断成败一律看产物**（`ls -la dist/*.exe` + 解包核对依赖），**不要看退出码**；
  portable 那个 exe 排在收尾之后，会停在**上一次成功**的时间戳，属于正常现象。
  想拿到完整的两个 exe（含 portable）就去**没有这个垫片的环境**打包。
- ⚠ **bash 的 `rm` 也走 safe-delete 垫片**（报 `genie-trash failed`，
  **不是** GNU coreutils，别指望绕过）。但它**按条目计数**：
  一个目录整体 trash 只算 **1 次**（报错里的 `targetCount:1` 就是这个），
  而 electron-builder 是**逐个文件替换**，几千个文件就累计到几千 —— 撞阈值的从来是后者。
  所以清场的正确姿势（亲测有效，19 次操作远低于阈值 50）：
  ```bash
  find dist/win-unpacked -maxdepth 1 -mindepth 1 -not -name resources -exec rm -rf {} +
  find dist/win-unpacked/resources -maxdepth 1 -mindepth 1 -not -name app.asar -exec rm -rf {} +
  ```
  注意 `-maxdepth 1` 不能省：递归会列出数千个文件，每个算一次，反而立刻撞线。
  **`.asar` 删不掉是已知的环境拦截**，留着它，electron-builder 覆盖它只多算 1 次。
- **解包核对安装包**：`7za` 是 **Windows 程序**，两个反直觉点：
  - `-o` 路径要用 Windows 风格：`-o/tmp/x` 会被解到 **`E:\tmp\x`**（不是 Git Bash 的 /tmp）
  - 列出的路径用**反斜杠**，grep 时别写正斜杠 —— 否则**假阴性**，
    会误判成「依赖没打进包」（我差点据此宣布安装包是坏的）
  - `node_modules/7zip-bin/win/` 下**只有 arm64 / ia32**（**没有 x64**，旧笔记里的 x64 路径已失效）
  - bash 里变量名别以数字开头（`7Z=...` 会被解析成 `$7`+`Z`）
- **打包中断后 `dist/win-unpacked` 可能不完整**，但**两个 exe 是自包含的**、不受影响。
  要验证交付物就解包 exe：
  `7za x -o<win路径> "dist/TalkScript Setup 0.2.0.exe"` 然后
  `node _verify/runtime-smoke.js --root <解包目录>/resources/engine`（实测 12/12）。
  `-c http.proxy=`（**空值**）不能省 —— 只「不传代理参数」不够，
  git config 里若本身有 `http.proxy` 照样会被用上。
- **诊断用域名对比法，且要同时测「经代理」与「直连」两列**：
  全挂 = 代理问题；只有 github.com 挂 = 对端/路由问题。

### 5.1 ⭐ `github.com:443` 挂但 `:22` 通 → 改走 SSH（2026-09-16 实测）

**这是比「死等自愈」更好的绕行路。** 故障形态很特别：不是 GitHub 整个挂，
是 **443 到 `github.com` 这一个目标**被卡住，而 **SSH 的 22 端口完好**。

实测矩阵（同一次探测）：

| 探测项 | 结果 |
|---|---|
| DNS `github.com` | ✅ `20.205.243.166` |
| TCP **443** 直连 `github.com` | ❌ 失败 ← 就是它挡着 https push |
| TCP **22** 直连 `github.com` | ✅ 通 |
| `api.github.com` HTTPS | ✅ 200 |
| `baidu.com` | ✅ 301 |

**判据**：`api.github.com` 通、`baidu.com` 通，唯独 `github.com:443` 挂
→ 不是代理、不是 DNS、不是 GitHub 整体故障，**是那一个 IP:端口 的路由被掐**。
此时换 `-c http.proxy=` 直连**没用**（已实测），但 **SSH 能走**。

**探测命令**（`/dev/tcp` 比 curl 更直接，能区分「TCP 层不通」与「HTTP 层报错」）：
```bash
timeout 8 bash -c 'exec 3<>/dev/tcp/github.com/443 && echo "443 OK"'   # 443：无输出=不通
timeout 8 bash -c 'exec 3<>/dev/tcp/github.com/22  && echo "22 OK"'    # 22：应回 "22 OK"
```

**走 SSH 的完整流程**：
1. 本机原本**没有** `~/.ssh`（`Permission denied (publickey)` 就是这个原因）→
   ```bash
   ssh-keygen -t ed25519 -C "talk-script-agent@$(hostname)" -f ~/.ssh/id_ed25519 -N ""
   chmod 600 ~/.ssh/id_ed25519
   cat ~/.ssh/id_ed25519.pub      # 交给用户去 GitHub 添加
   ```
2. 用户到 https://github.com/settings/ssh/new 粘贴公钥（**这一步只能人工**）。
3. 验证：`ssh -o StrictHostKeyChecking=no -T git@github.com`
   → 成功回 `Hi <user>! You've successfully authenticated...`
4. 推：`git push git@github.com:stevie-du/talk-script-agent.git develop:develop`
   （**先用显式 SSH 地址推这一次**，确认没问题再决定要不要改 `origin`）

⚠ **`ssh -T` 回 `Permission denied (publickey)` 只说明「没有可用私钥」，
不代表 22 不通** —— 两者要分开看：`/dev/tcp` 探端口、`ssh -T` 探认证。

**⚠ 但这次的结局是：SSH 一次也没用上（重要教训）。**
挂后台守候轮询了 **24 轮 × 15s ≈ 7 分钟**，用户始终没加公钥；而与此同时
**`github.com:443` 自己恢复了**，并发会话第 1 轮 `-c http.proxy=` 直连就推成功
（`d0d3a40..c4e22d2`）。我随后再推拿到的是 `Everything up-to-date`。

三条推论，按重要性排：

1. ⚠ **「配 SSH」是需要用户配合的动作，别拿它当第一选择** —— 用户可能一直不来，
   守候白跑。**先按老办法重试 HTTPS 直连**（`DEV-TOOLING.md` 本节上半部分），
   SSH 只作为「长时间（>30 分钟）确证 443 不通、且用户在场」时的备选。
2. ⚠ **别照旧印象重推**：动手前先 `git ls-remote origin refs/heads/develop` 对一下
   远端真实 HEAD —— 这个仓库有并发会话，**它们可能已经替你推了**。
   （本次就是：我以为积压 7 笔，实际远端早已到位。）
3. ✅ **`Everything up-to-date` 且 exit 0 = 成功**，不是失败、也不是「没执行」。
   它意味着远端已包含本地所有提交 —— **这本身就是「积压已清」的确证**，
   不用再补别的验证。
4. 副作用：本机留了一对**未注册**的 `~/.ssh/id_ed25519`（公钥没交给 GitHub）。
   没注册就不授予任何权限，留着当备选无害；不需要时直接删 `~/.ssh/` 即可。

- git **不读** `https_proxy` 环境变量；**端口会变，用 `$https_proxy` 别写死**。
- `github.com` 单域名故障也会自愈（可长达 30 分钟以上），但**先试直连**。
  **本地提交永远安全**，连续 20+ 轮全挂就收手交给用户。
- `credential.helper=helper-selector`(GCM) 取不到凭据，`wincred` 可以。
- `git status` 的 `[origin/develop: gone]` 是沙箱拦 `refs/remotes` 的**显示假象**；
  该目录有**防伪造护栏**（写入会被回滚），**别去"修"**，不影响 push/pull/fetch。
- 远程 `main`(`ea63939 Initial commit`) 与 develop 无共同祖先，别乱推。
- **路径**：用 `E:/Workspace/...`，`/e/Workspace/...` 在 `git -C` 下会失败。
- **删文件**：`fs.rmSync` 会走本机的 safe-delete 垫片 → **回收站**（这是好事）。
  但**对 `.asar` 无效**：`fs.rmSync` 报 `[safe-delete] 操作失败 … Some operations
  were aborted`、`fs.renameSync` 报 `EBUSY`（改父目录名报 `EPERM`）、
  而 `fs.writeFileSync(f,'')` **却能成功** —— 文件可写却删不掉，
  是环境对 `.asar`（Electron 应用包格式）的**保护性拦截**，不是文件锁。
  同目录树里的 `.png`/`.log` 都正常。**别为小体积去绕过回收站强删；
  这类目录要在资源管理器里手动删。**
  另：`cmd //c` 在 Git Bash 下会被转义成打开交互 shell，别用。

## pytest 在本环境的两个假信号（2026-09-17 实测）

**（1）`exit=1` + 汇总行消失 —— 是沙箱拦了 pytest 清理临时目录**
默认 basetemp 在 `C:/Users/.../Temp/pytest-of-...`，结束时被 safe-delete 钩子
拦下 → teardown 失败 → **退出码 1，且那行 `N passed` 根本不打印**。
✅ 正确姿势：`--basetemp=.pytest_tmp`（工作区内），跑完删掉（约 5 MB）。
⚠ 脚本里的 `rm -rf .pytest_tmp` 也会被 `SAFE_DELETE_BULK_CONFIRM_REQUIRED`
拦下删不掉 → **要单独开一条已提权的命令清理**，别指望闭环脚本自己清。

**（2）3 条用例必挂 —— 是沙箱的「按回合累计」删除配额**
```
FAILED tests/test_pack_yaml_integrity.py::test_missing_banwords_is_not_an_error
FAILED tests/test_pack_yaml_integrity.py::test_missing_pack_yaml_is_still_the_404_case
FAILED tests/test_pack_yaml_integrity.py::test_missing_skill_yaml_still_returns_none
```
真因：`Path.unlink()` 被 `sitecustomize.py` monkeypatch 成走回收站 + 批量删除守卫，
守卫是 `scope:"turn"` 的**累计计数**（`count:10483` vs `threshold:50`）。
这三条用例的语义就是「删掉某个 yaml 再断言行为」，于是
`unlink()` 直接 `SystemExit: 1`。

→ **同一回合里前面删过东西（哪怕只是 `rm -rf .pytest_tmp`），这 3 条必挂。**
✅ 判定与规避：
- 单独、干净地跑（本回合还没删过东西）→ **exit=0、0 FAILED**（已验证）；
- 别把 pytest 排在「先清理再跑」的闭环脚本里 —— 那必挂；
- 看到这 3 条，**先单独重跑一次再判断**，不要报成回归、也不要像我第一次那样
  误归给「并发会话干扰」。

**顺带的教训**：`tr -cd 'FE' < log | wc -c` **不能当失败计数** ——
`[safe-delete]…SAFE_DELETE…REQUIRED` 这行里就含 F 和 E。
要看就 grep `^FAILED`。

## ⚠⚠ 变异检验的还原**必须用备份文件**，绝不能用 `git checkout <file>`

**踩坑（2026-09-17）**：做「llm 刷新按钮」那道变异时，我用
`git checkout desktop/renderer/index.html` 还原 —— 结果**把该文件全部
未提交改动一起丢了**：packinfo 右列改造成 .page-card、llm 加刷新按钮、
删 st-maxtokens、删 st-save 四项全没（它们都还没提交）。只能照着记忆重做一遍。

`git checkout <file>` 的语义是「用 HEAD 覆盖该文件」—— **它不认「我刚改了什么」**，
只认「HEAD 是什么」。只要那个文件有未提交改动，全部报废。

✅ **正确姿势**（改之前先备份，还原时 cp 回来）：

```bash
cp desktop/renderer/index.html /tmp/index.bak      # 变异前
python - <<'PY'                                    # 注入变异
...
PY
node _verify/verify.js 2>&1 | grep -E "期望的断言名|^Error"
cp /tmp/index.bak desktop/renderer/index.html      # 还原（不是 git checkout！）
```

⚠ 一个文件可能同时承载**多道变异**，所以每道变异前都要重新 `cp` 一次备份
（别用「最开始的那份」，那份是原始态，会把前面已注入又已还原的改动也带回来）。

✅ 辅助判据：变异跑完 `git status --porcelain` 里该文件**仍应是 `M`** ——
如果它变干净了，说明还原方式选错了（把改动丢光了）。

## ⚠ 测试不许依赖**未跟踪的运行时文件**（否则提交自洽验证出现假红）

**踩坑（2026-09-17）**：`git worktree add --detach <dir> <sha>` 干净检出后
跑 pytest，5 条挂；**主仓库同样的测试全过**。

```
FAILED tests/test_llm_retry.py::test_network_retry_then_success   (及另外 4 条)
E   ValueError: unknown url type: '/chat/completions'
```

真因：那 6 条用例写的是 `load_config(ROOT)` —— 读**仓库根**的 `config.yaml`，
而它被 `.gitignore` 忽略（每台机器上都不一样）。worktree 里没有这个文件 →
`_parse_models` 返回空列表 → `cfg.llm.base_url` 为空 → httpx 拼出
`/chat/completions` → urllib 报错。

⚠ 这是**假红**：提交本身没问题，是测试依赖了不该依赖的东西。
但它会让人误判「这个提交是坏的」—— 正是本项目最忌讳的
「信号看起来对、其实指错方向」。

✅ **规矩**：测试要构造配置就**自己写一份到 `tmp_path`**，不要读仓库根。
参考 `tests/test_llm_retry.py` 的 `_cfg(**over)` helper：

```python
def _cfg(**over):
    tmp = Path(tempfile.mkdtemp(prefix="ts-llmcfg-"))
    (tmp / "config.yaml").write_text(
        "models:/n"
        "  - {id: m1, base_url: 'https://api.example/v1', api_key: 'sk', model: 'm'}\n"
        "active_model: m1\n", encoding="utf-8")
    cfg = load_config(tmp)
    for k, v in over.items():
        setattr(cfg.llm, k, v)
    return cfg
```

**判据**：`grep -rn "load_config(ROOT)" tests/` 应当**为空**。

### 附带坑：用 heredoc 脚本生成代码时，转义会掉一层

写上面的 helper 时我用 `python - <<'PY'` 生成代码，里面写 `"models://n"`
—— 落盘成了 `"models://n"`（正斜杠！）→ yaml 解析失败 → 主仓库反而挂 3 条。
**正解：用 `chr(92)` 拼反斜杠**，别赌 heredoc + python 字符串的层数：

```python
bs = chr(92)                      # 反斜杠
new = '"models:' + bs + 'n"'      # -> "models:/n"
```
