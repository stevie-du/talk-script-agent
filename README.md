# TalkScript · 口播脚本智能体

本地运行的口播短视频脚本生成器：**Python 流水线引擎 + Electron 桌面壳 + 行业知识包**。
代码守住"能不能用"的下限（字数/时长/禁用词由校验器兜底并自动回炉），模型去拼"好不好听"的上限。

> 行业包配置、技能（skill.yaml）配置、知识库内容来源、导出为 Agent 技能——
> 见 **[docs/知识库与技能配置指南.md](docs/知识库与技能配置指南.md)**。
> 知识库内容格式规范 + 可复制给任何 AI 的整理指令，见 **[docs/知识库内容规范.md](docs/知识库内容规范.md)**。
> 场景序列（`scenes`）的字段契约，见 **[docs/场景序列契约.md](docs/场景序列契约.md)**；
> 改界面前先读 **[docs/UI视觉规范.md](docs/UI视觉规范.md)**。
> 界面亮/暗两套配色**跟随系统外观自动切换**（不需要设置项）；配色取向见规范 §7。

## 架构

```
┌─ Electron 壳（desktop/）── 拉起引擎 + 开窗；界面由引擎同源提供
│      │ spawn（--port / --root / --data-dir / --packs-dir / --parent-pid）
│      │   令牌**不在命令行上**：Windows 上同机任意进程都能读到别人的 argv
│      │   （`wmic process get commandline`），改走子进程环境变量 TALKSCRIPT_TOKEN
│      │ 轮询 /api/health → loadURL http://127.0.0.1:<port>/?token=…
│      │ 引擎 stdout / stderr → %APPDATA%\TalkScript\logs\engine.log（写前脱敏）
┌─ Python 引擎（app/）── 127.0.0.1，令牌鉴权
│   security  访问控制：一次性令牌 + 同源判定（/docs、/openapi.json 已关闭）
│   jobs      作业与状态机（原子迁移，见 TRANSITIONS）
│   pipeline  编排：参数归一 → 选题 → 撰写 → 校验回炉(≤2轮) → 分镜 → 组装落盘
│   checker   字数/时长/两级禁用词（行业包词表 × 平台分级）
│   prompts   skill.yaml 模板渲染 + 知识文件注入（带 mtime 缓存）
│   store     产物落盘 + 历史索引（index.json）
│   watchdog  父进程看门狗：Electron 一没，引擎自行退出（不留孤儿占端口）
│   packgen   输入行业名+描述 → 生成新行业包初稿（草稿态）
│   packseed  出厂包 → 可写包目录的播种：只拷不覆盖，保住用户改过的那份
└─ packs/<行业>/ ── 知识包（合规词库/选题库/受众库/钩子库/配额规则/私有资料）
             打包版真正使用的是**可写的那份**：%APPDATA%\TalkScript\packs
```

单次生成的模型调用：**正常 3~5 次**（选题 1 + 撰写 1 + 回炉 0~2 轮 + 分镜 1，
回炉轮是"撰写+校验"整体重跑；只出口播时不跑分镜，选题命中缓存时再少一次）。
「重写本段」是定稿后的独立动作，每次 2 个调用（改写该段 + 重画分镜）；「新建行业包」1 个调用。
同一次应用运行内，**选题提示词逐字相同**的重复生成会跳过选题那一次（作业日志写
「复用上次选题（本次未调用模型）」）。判据是「模型名 + 渲染后的选题 system/user」这一段指纹，
所以改主题 / 细分 / 受众 / 时长 / 风格 / 平台 / 人设 / 结尾引导、换模型、改被注入的知识内容
都会重新选题；而只改「补充资料」「人味档位」「输出内容」仍会复用 —— 它们不进选题提示词。
「换一版」从不调缓存，那个按钮的语义就是要一个新角度。同参数**并发**生成也只打一次模型
（选题加了一把 single-flight：第二条等第一条的结果，不再各花一次 4000 token）。
⚠ 以上是**无故障**口径；叠加请求层重试与 JSON 解析重试，最坏是
`逻辑调用数 × 2 × (llm.retries + 1)` 个 HTTP 请求 —— 默认 `retries: 2` 时 5 次调用为 **30**，
`retries: 3` 则为 **40**。为免这条最坏路径把人吊在界面上干等，一条作业另有**整作业时间上界**
20 分钟（`app/jobs.py` 的 `JOB_BUDGET_SECONDS`）：到点落 `failed` 并说明"上游太慢或反复重试"，
不再无限等下去（《审查报告-20260920》P1-5 的两半现在都收口了）。
⚠ 这句在**正常路径**上是绝对的；如果连"收口"这一步自己都在抛（内存耗尽一类），还有一道
按 `STALE_JOB_MULTIPLIER`（2× 预算）扫忙的回收网 —— 它**只把这条作业从并发额度里摘出去**
（`Job.stranded`），不改状态、不删条目、不动产物：那条作业该怎么走完还怎么走完，
界面照常轮询得到结果，引擎日志里留一条"已 N 秒无任何进展，不再占用并发额度"。
第 10 轮复核就是因为前两个版本分别犯了"force failed"和"移出注册表"这两个错才改成这样的。
其余全是确定性代码。

各阶段的**输出预算不是一刀切**：选题与分镜固定 4000、单段重写固定 3000，
只有撰写用配置里的 `llm.max_tokens`（`app/pipeline.py`）。推理型模型的"思考"也计这份预算，
想满时 `content` 会返回空串 —— 失败横幅会给出**本次调用实际用的**那个数字。
⚠ 所以看到"空内容"时，改 `llm.max_tokens` 只对**撰写**那一次有用；
而且调大它只会把思考推得更长（实测预算越大想得越多），先考虑换非推理档或降低回炉次数。

**渲染层为什么由引擎提供**：页面与 API 同源，才能既不用为了迁就 `file://` 而放行
`Origin: null`（那等于把引擎交给本机任意网页），又让前端用上 ES 模块。

## 启动（开发模式）

```bash
# 1) Python 引擎依赖
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt

# 2) Electron 依赖
cd desktop && npm install

# 3) 配置模型（也可启动后在界面 ⚙ 设置 里填）
#    编辑 config.yaml 的 models 列表：每条模型自带 base_url / api_key / model
#    或用环境变量（推荐，密钥不落盘）：TALKSCRIPT_API_KEY

# 4) 启动桌面应用（自动拉起引擎）
cd desktop && npm start
```

也可以不开壳，直接浏览器用引擎页（会打印带 token 的地址，**必须用打印出来的那个**）：

```bash
.venv\Scripts\python -m app.server --port 8765
# 打开 http://127.0.0.1:8765/?token=<启动时打印的令牌>
```

手工起引擎时令牌有两条来路：`--token <值>`（写在这条命令行上，同机能读到）
或环境变量 `TALKSCRIPT_TOKEN`（**Electron 用的是这条**：主进程生成随机令牌，
只放进子进程的环境块，argv 上不留痕）。两者都不给就随机生成一个并打印出来。

无 Key 调试：设 `TALKSCRIPT_MOCK=1`，引擎返回固定夹具，可跑通全流程（含"政府补贴"拦截回炉演示）。

**窗口空白 / GPU 进程起不来时**：若日志出现

```
ERROR:gpu_process_host.cc(982)] GPU process exited unexpectedly: exit_code=1
FATAL:gpu_data_manager_impl_private.cc(423)] GPU process isn't usable. Goodbye.
```

说明本机**沙箱化的子进程无法启动**（受限环境 / 无桌面会话 / 部分虚拟机常见）。
改用：

```bash
cd desktop && npm run start:no-sandbox
```

它等价于 `electron . --no-sandbox`。

⚠️ **不要用 `--disable-gpu` 那套参数救急**：`--disable-gpu --disable-gpu-compositing
--in-process-gpu` 虽然能让进程不崩，但窗口会**一片空白**（合成被关掉，什么都不画）；
只加 `--disable-gpu-sandbox` 也不行，渲染进程仍然起不来、页面根本不会加载。
真正缺的是 Chromium 自身的沙箱能力，`--no-sandbox` 一个参数即可解决。

## 测试

```bash
.venv\Scripts\python -m pytest        # 引擎侧（访问控制/状态机/校验器/模板/索引/建包/导出）
node --test desktop/                  # 壳侧纯逻辑（引擎降级链 + 打包排除规则，零依赖）
node _verify/verify.js                # 界面回归（桩 fetch，百秒量级：三百多条断言跑真页面）
node _verify/e2e-live.js              # 真实端到端（真引擎 + 真页面 + 真落盘）
```

四条都是零第三方依赖（除 pytest），可分别单独运行。
`_verify/legacy/` 里是重构前写的脚本，依赖已不存在的 DOM 与全局变量，**不要运行**。

## 打包 Windows 安装包

```bash
cd desktop && npm run dist
# 产物在 desktop/dist/：TalkScript Setup 0.2.0.exe（NSIS 安装包）+ TalkScript 0.2.0.exe（portable）
# 打完必须认证一遍（逐项比包内引擎与当前源码，缺一步都算没过）：
cd desktop && npm run verify:package
# 它查五样：app/ 逐文件一致、renderer/ 逐文件一致、packs/ 逐文件一致（private/ 按设计不出厂）、
# 包内不许出现 private 文件、两个 exe 的时间戳不许早于最新源码改动（防"改了没重打"）。
```

打包包含引擎代码、行业包与渲染层。**不含任何配置**（连模板都不带）——用户自己配置：

| 配置方式 | 说明 |
|---|---|
| 应用内「设置 → 模型接口」 | 推荐，填完保存即生效 |
| 环境变量 `TALKSCRIPT_API_KEY` 等 | 密钥不落盘；9 个变量的清单以 `app/config.py` 顶部注释为准（`config.example.yaml` 只列了最常碰的两个） |
| 数据目录下的 `config.yaml` | 引擎首次运行会自动生成一份带注释的模板 |

**模型是一份列表，不是一条。** 「模型接口」里可以同时配智谱、DeepSeek 等多家
（每条**自带**请求地址与 Key），列表里随时启用其中一个，切换立刻生效，换一家不用重填 Key。
生成参数与「连到哪家」无关，是全局一份，收在同一个页面的「高级配置」里 ——
**界面上能改的只有「重试次数」和「单次超时」两项**：采样温度与输出预算（`max_tokens`）
2026-09-17 起按用户要求从设置页移除了，要改只能改数据目录的 `config.yaml`
或环境变量（后端仍按 `app/server.py` 的 `NUMERIC_BOUNDS` 校验区间）。
输入区右侧的模型选择器直接读这份列表。

数据目录在打包版是 `%APPDATA%\TalkScript\`，开发态是项目根 ——
因为安装目录（Program Files）通常不可写、而且**一次自动更新就会整个重写**，
不能往那儿放。这个位置由 `--data-dir` 指定，Electron 主进程负责传。里面住着：

| 内容 | 位置 | 说明 |
|---|---|---|
| 配置 | `<数据目录>/config.yaml` | 首次运行写一份**全部注释掉**的模板 |
| 产物与历史 | `<数据目录>/generated/` | 每条记录一个目录 + `index.json` |
| **行业包** | `%APPDATA%\TalkScript\packs\` | 见下；由 `--packs-dir` 指定 |
| 引擎日志 | `%APPDATA%\TalkScript\logs\engine.log` | 见「启动失败时」 |

**行业包也在数据目录**（2026-09-22 起）。此前它们住在安装目录里，于是
用户自己新建的行业包、手改的 `banwords.yaml`、填进 `private/` 的产品资料
都会在一次应用更新后消失 —— 而那份 packs/ 是安装包里带的**只读种子**：
首次运行把它拷进数据目录（`app/packseed.py`），之后

- 出厂包**没有变化** → 一个字节都不写（不刷 mtime，编辑器不会误报"文件被改过"）；
- 出厂包**更新了**而用户那份没动过 → 同步过去，并保住目录里的 `private/`；
- 用户**改过**那一份 → 绝不覆盖，只在日志里留一条 WARNING 说清"出厂的改动
  没并进来，要合请手工合"（不静默替你猜）。

台账是 `<数据目录>/packs/.packseed.json`（一个文件，不是目录，所以不会被
当成一个行业包）。开发态不传 `--packs-dir`，用的仍是仓库 `packs/`，行为不变。

> 首次启动若检测到「没有 Key 且没有历史记录」，界面会**自动打开「模型接口」设置页**。
> 除此之外不再有第二处配置引导 —— 「还没配模型」只在输入区右侧那颗模型选择器上
> 说一次（橙色 + 悬停给出原因）。
>
> 没配过的字段会标上「内置默认」小标 —— 引擎对缺失字段会取内置默认值，
> 不标的话「未配置」看起来和「已配置」一模一样（模型名、温度、超时全都像你自己填的）。
> 所以编辑一条还没配过的模型时，右栏表单里对应的框是**空的**（靠 placeholder 提示默认值），
> 不会把引擎兜出来的默认值填进去冒充你填的。输入区的模型选择器同理：
> 「（默认）」后缀已于 2026-09-17 下线（`ui.js`），未配置的判据完全交给
> 选择器上的警示标记 `_warnNote` —— 一个装饰性后缀和一个真警告同时存在时，
> 用户读的是后缀，而真正说明"这条没配好"的是后者。
>
> 老版本写的 `config.yaml`（只有 `llm:` 段那种）**读的时候**会自动当成一条模型，
> 不写回文件 —— 升级后打开不会看到「一个模型都没有」，也不会发现文件被偷偷改过。

**打包版自带 Python 运行时，目标机器不需要装 Python。**
`npm run dist` 的 beforePack 阶段会自动构建一份内嵌的 Python 3.13.12：
下载官方 embeddable 包 → 校验 sha256 → 装依赖 → 用**运行时自己**的解释器编译字节码
→ import 自检，落到 `resources/engine/py/`。主进程优先用它，
找不到才退回系统 Python / `engine.exe`；显式设 `TALKSCRIPT_PYTHON` 仍可覆盖。

代价是安装包约 96 MB —— **这是当前版本的实测值，会随 Python 版本与依赖变化**，
实际以 `desktop/dist/` 里打出来的为准（运行时目录大小看 `du -sm desktop/vendor/py`）。
构建缓存 `desktop/vendor/` 已 gitignore，首次打包会下载；
版本/依赖不变时后续打包会跳过重建（`--force` 可强制）。

### 安装与卸载的行为

- **不要求管理员权限**：默认装到当前用户的 `%LOCALAPPDATA%\Programs\TalkScript`，
  不弹 UAC（要装给所有用户才需要改 `perMachine`）。
- 有安装向导，**可以改安装目录**。
- **单实例**：第二次启动不会再起一份引擎，只把已经开着的窗口唤到前台
  （`app.requestSingleInstanceLock()`）。以前开两次就有两份引擎，各自持有
  一份内存锁，同一份 `generated/index.json` 会被后写的那份整个覆盖 ——
  表现不是报错，是"历史记录少了几条"。
- 配置、产物与**行业包**都在**数据目录**（打包版是 `%APPDATA%\TalkScript\`），与安装目录分开
  —— 所以**卸载 / 升级都不会删掉你的配置、生成过的脚本和自己改过的包**
  （想彻底清空就手动删这个目录）。

#### 启动失败 / 引擎报错时看哪里

引擎子进程的 stdout 与 stderr 会写到 `<数据目录>\logs\engine.log`
（超过 2 MB 时截断留一档 `engine.log.1`），写入前先过一遍脱敏：
一次性令牌、`Authorization` / `Bearer` 头、`sk-…` 形态的 Key 都会变成 `[已脱敏]`。
所有报错对话框的末尾都写着这个文件的完整路径。

| 现象 | 日志里找什么 |
|---|---|
| 界面正常，但要确认用的是内嵌运行时 | `[spawn] via=engine/py（出厂运行时）`；显示 `.venv` / `PATH` 说明降级了，是 bug |
| 窗口空白 / 「引擎无法启动」一句都没有 | `[spawn-error]` —— `TALKSCRIPT_PYTHON` 指错路径时以前是未捕获异常（白屏 40 秒），现在会立刻说明"找不到引擎程序" |
| 引擎起来又退出 | `[stderr]` 与 `[exit] code=…` |
- ⚠ **安装流程本身还没在干净机器上验过**。安装包里的运行时能用已由「解包 + 端到端冒烟」
  证明，但「装到一台没装过 Python 的机器上」这一环待实机验证 ——
  有干净机器 / 虚拟机时照下面五步做，约 10 分钟：

  1. 双击安装包走完向导 → 应落在 `%LOCALAPPDATA%\Programs\TalkScript`
  2. 从**开始菜单**启动（不是直接双击 exe）→ 应能起来并进入引导页
  3. 生成一个脚本 → 确认用的是**内嵌运行时**：`%APPDATA%\TalkScript\logs\engine.log`
     里应出现 `[spawn] via=engine/py（出厂运行时）`，
     显示系统 Python 说明降级了，是 bug
  4. 控制面板卸载 → 安装目录应被删干净；`%APPDATA%\TalkScript` **预期保留**（那是用户数据）
  5. （可选）卸载后重装 → 旧配置应当还在

  NSIS 的卸载器是安装时才生成的，`7za l` 列不出来 —— 所以第 4 步只能实机验。

## 日常使用

1. 行业包在 **设置 → 生成偏好 → 行业包卡**里选（改动即时生效）；主题与参数在首页输入区填，
   点「生成脚本」（Enter 发送，生成中同一键变「停止」，Esc 也可停）
2. 生成**一杆到底**：选题 → 撰写 → 校验回炉 → 分镜 → 出稿，中途不打断。角度不满意走下面第 4、5 步
   事后修，而不是中途暂停等确认 —— 「分步确认」曾在这里，2026-09-19 移除：
   它省下的只是"角度不对时白跑一次撰写调用"，代价是确认卡必须依赖作业驻留内存，
   于是带来重启后记录点不开、待确认卡攒够被静默丢弃这一类没有好解法的问题
3. 结果：口播分段卡片（每段可**重写本段**）/ 分镜 / 合规 / JSON / 日志
4. 「换一版」是同一问题下的**新版本**，用结果上方的「版本 1/2」翻页对照，不会覆盖上一版
5. 结果下方的「下一步」给几个可点的参数调整；用户消息悬停有铅笔，可改主题就地重新生成
6. **新建行业包**：设置 → 生成偏好 → 行业包卡「新建」，填行业名 + 一句话描述。
   它现在是一条**后台作业**（状态「行业包生成中」），切走再回来进度还在，生成中可随时取消
   （取消点在"模型返回之后、写盘之前"，因此通常压根不会留下目录；取消来得太晚也一样 ——
   本次刚写出的那个 `packs/<名字>` 会被收走，作业记「已取消」，下次同名提交不会被 409 挡住。
   建包中途失败同理：不会留下"包在盘上、作业却报失败"的半成品目录。
   回收没成功（文件被占用等）只在作业日志里留一条 WARNING，不会把已生效的取消翻成失败。）
   完成后结果页给产物摘要与两个出口：「返回」回来源面板，「完成」跳到生成偏好并选中新包。
   新包是**草稿态**：按校对清单核实后，在包详情点「标记为已校对」
   （等价于把 `pack.yaml` 的 `draft` 改成 `false`）
7. 历史记录在左栏，点击回看

## 行业包结构（packs/elevator 为参照）

```
packs/elevator/
├── pack.yaml        # 清单：参数定义、语速/配额表、禁用词文件名、私有资料路径
│                    #（**哪个阶段注入哪些知识不在这里** —— 见 skill.yaml 的 stages.<阶段>.files）
├── skill.yaml       # 技能：各阶段的提示词模板 + 该阶段要注入的文件（可 `路径#章节`）
├── banwords.yaml    # 禁用词：hard（必改）/ soft（提示）× 平台 extra_hard / demote
├── compliance/      # 广告法 / 平台差异 / 行业红线（红线经 select+write.files.redlines
│                    #   注入，且只取 `compliance/industry.md#红线速查` 那一节）
├── knowledge/       # 细分领域知识点 / 受众痛点 / 选题库（经 select.files.ideas 注入
│                    #   `#选题库` 一节）/ 标准索引（只注入 `#核心术语` 一节）
├── patterns/        # 钩子库与风格（按风格切片）/ 完播与转化
├── rules/           # 时长配额 / 输出模板（两份都是人读约定，不注入模型）
└── private/         # 你的私有资料（型号/服务/案例/异议应答），不带进任何外发产物
```

新建行业包 = 在界面上让模型生成初稿（见上面「日常使用」第 6 步），或照这个结构手工建一份。
`private/` 两处都不外带：导出为 Agent 技能时**默认不导出**（需要时显式开启），
打安装包时也**不进 extraResources**（`desktop/packaging.test.js` 用真匹配器钉住这条规则，
构建前的 `beforePack` 还会再拦一次）。
「导出为 Agent 技能」的**界面入口 2026-09-17 已撤**，端点 `/api/packs/<name>/export-skill` 仍在
（手工调用可用，见 `docs/知识库与技能配置指南.md` 的说明）。

### 禁用词表的三个约定

- **不要写单字条目**（如 `最`）：它会命中「最近」「最后」「最好」这类正常用词，
  噪声远大于收益。要拦绝对化表述请写具体短语（`最低价` / `最便宜`）。
  引擎会忽略单字条目并在结果里说明忽略了哪些。
- 命中判定**同表内不重叠、跨表也不双计**：`绝对安全`（hard）已经占住那几个字，
  soft 里的 `绝对` 就不会再报一遍 —— 一处违规只出现一次。
- 平台级只认 `extra_hard` / `promote` / `demote` 三种调整，**没有 `extra_soft`**：
  写了不报错，但引擎不读（`app/checker.py`）。

## 已知边界（v0.2）

- 真实生成质量取决于所配模型；草稿行业包内容必须人工校对后再投产
- **回炉是"整篇重写"而不是"改违规段"**：每轮重发同一份提示词（不含上一版正文），
  违规反馈只给词与次数、不给违规句原文；段落级的自动路由没做（人工入口是「重写本段」）
- **分镜阶段失败会让整条作业判 failed**，即使正文已经通过校验（单段重写路径相反：沿用旧分镜）
- ~~回炉反馈块排在 user 模板开头，前缀缓存打不中~~ **2026-09-22 已修**（P1-40）：
  `$feedback_block` 已挪到撰写模板**末尾**，回炉轮与首轮共享整段静态前缀
  （60 秒档实测首轮 7162 字 / 次轮 7213 字，共同前缀 7162 字）；
  红线注入也从整份 `industry.md`（2957 字）瘦成 `#红线速查` 一节（798 字），
  撰写一轮 8411 → 7162 字。由 `tests/test_pack_injection.py` 钉住
- 时长容差默认按 `±max(10%, 3秒÷目标时长)` 自适应（15 秒档 ±20%、60 秒及以上 ±10%），
  包想覆盖就写 `pack.yaml` 的 `duration_tolerance_pct`（0.5~100；写错在包列表上就标
  `pack_error` 并拦住生成）。本次校验真正用的那个数随报告外发（`check.tolerance_pct`），
  回炉反馈读它 —— 不再出现「报告说合格、反馈催你改长度」。由 `tests/test_duration_tolerance.py` 钉住
- 内嵌运行时已用**打出来的产物本身**验过（解包核对 + 端到端冒烟 + 真启动 app），
  但 **NSIS 安装流程本身未在干净机器上验过**（真装一次、开始菜单启动、卸载残留）
- 私有资料仍以编辑 yaml 文件维护，界面化管理未做；把文件丢进 `private/raw/` 让引擎自动解析
  **没有实现**（引擎不读那个目录）
- 历史列表最多返回 100 条；单段重写要求后台作业仍在内存中（重启应用后请用「换一版」）
- 「版本导航」是**会话内**的：刷新或重开应用后，每个版本会各自成为一条独立记录
- 建包作业与刷新页面：刷新后作业仍在跑并占着并发额度，但界面找不回它（重启引擎即恢复）

## 待办（2026-09-21 记录：去AI味可验证化 + 行业情报输入）

两条新需求当天只做到「调研 + 实测 + 原型」，**未写生产代码**。以下按可独立交付的顺序排。
2026-09-21 二轮：代码库审计（子代理）+ 同行/开源调研，修正过 B4/A6/B7 三处设计、补了四类同行能力差异。
2026-09-21 三轮：**逐家拆到算法层**（severity 分级 / 评分门槛分离 / 误改率回归 / D·S·E 合成 / 源注册表），
方案与差异化见 `docs/需求方案-去AI味与热点情报.md`，设计页 `_verify/_proto-spec.html`（5 屏：选题 / 人味分三层 / 多版对比 / 情报源 / 数据流）。
下面 A·B 两条清单仍有效，但**以该文档的第三部分 9 条待确认为准**。

### 线 A · 让去AI味可验证

现状（2026-09-22 更新）：提示词层之外，**机器指标已经有了** —— A1/A2 已落，
`check.ai_tells` 出分数与带定位的命中，回炉反馈把它当**非阻塞建议**念给模型；
但 `check_script` 的 `passed` 仍然只 gate 硬禁用词与时长偏差，
所以"AI 味重但合规、时长准"的稿子照旧一轮就过 —— 门槛要等 A4 校准完再定。

**同行空白点**：PopTo 爆款兔 / 小云雀 / 蝉镜 / 腾讯智影 / 度加 全查过，**没有一家做文案去AI味/质量分/多稿对比** ——
本线是差异化点。可借鉴同行的三样：一次多版脚本并排挑（PopTo）、对标视频拆解喂分镜（小云雀/蝉镜）、一键跟创（蝉镜，B 线后期）。

- [x] **A1** `app/ai_tells.py`（纯函数无 IO）+ `packs/elevator/ai_tells.yaml` +
  `packs/_template/ai_tells.yaml`（新包出生就带上，否则新包的人味检测静默关闭）：
  每条 tell 带 `severity: strong|weak`（yaml 的 `strong:` / `weak:` 两个列表）与 `lexicon` 词类，
  `md:` 指向 `patterns/anti-ai-smell.md`；strong 单次命中即报、weak 需累计 ≥2 处（`WEAK_MIN`）。
  结构类（排比三连、清单体、段长等长、句首重复、通篇零具体）在函数里。
  ⚠ 已做到：`_scan_words` 就是"长词优先不重叠扫描"，单字被 `len>=2` 过滤（与 `MIN_WORD_LEN` 同口径，
  所以抽象名词要写成「高效性」而不是裸「性」）；`_count` 与 `checker.count_chars` 的一致性由
  `tests/test_ai_tells.py` 钉住。未知 tell 名会报进 `config_warnings`（此处原有一个
  `set(a) | set(b) - set(ALL)` 的优先级 bug，会把每个合法名都报成未知 —— 已修并有反向用例）
- [x] **A2** `check_script` 的 report 加 **`ai_tells: {score, hits[], strong, weak, tells_enabled,
  config_warnings}`**（字段名与计划里的 `ai_smell` 不同，以此为准），**不进 `passed`**：
  同一份稿子接与不接 tells，`passed` 必须一致（`tests/test_ai_tells.py` 里就是这么断的，
  比断 `passed is True` 更守得住 —— 后者会被时长脸色左右）。
  ⚠ 计划提示的两条返回路径都填了：主路径给报告、`duration` 非正数那条给 `null`（「没测」与
  「测了很好」必须是两种形状）。落点：`result.json` 的 `check.ai_tells` + 回炉反馈里的非阻塞建议；
  report 键集一致性测试仍缺（A3 一起做）
- [ ] **A3** `tests/test_aitells_alignment.py`：yaml ↔ `patterns/anti-ai-smell.md` ↔ 函数名三方对账。
  ⚠ **不能照抄** `test_banwords_alignment.py`：它靠 ad-law.md 固定前缀词族行解析（`:23-39`），
  而 anti-ai-smell.md 是自由清单、词在句内括号里、无函数名可锚
  （`anti-ai-smell.md:14`）。需给 md 加可解析词表节 + 断言 severity 一致
- [ ] **A4** 校准后**才**定门槛：拿 `generated/` 现成产物跑分数分布，再决定 `ai_smell` 进不进回炉。
  ⚠ 86/87 历史产物是 mock 夹具且含清单体（`mock_fixtures.py:39,47`）→ **按 `result.mock` 分流**统计，
  否则 ai_smell 一进门槛冒烟必挂；词表与 `skill.yaml:78` / `anti-ai-smell.md:14` 已禁的书面连接词**显式对齐**，
  否则 169/175 假命中复现
- [ ] **A5** 结果页 chips 加人味分 + `.banner.why` 折叠明细；`store.render_script_md` 同步一行
- [ ] **A6** 真人语料：`yt-dlp + faster-whisper` 出稿 → **`data_dir/intel/samples/<style>/*.md`**
  （**不放 packs/**：pack 是可分发单元、包内文件会被 `/api/packs/{name}` 全量列出供下载
  `server.py:371-394`，语料是运营数据不是行业知识）→ `write_ctx` 经 data_dir 读取注入 `$samples`。
  ⚠ `$samples`/`$intel_block` **恒设 ctx 键、无内容给空串**（照 `prompts.py:116-122` 的 facts_block），
  否则 `unfilled()` 在 strong 以外的档位每天刷未填充日志；样本进提示词前先过 banwords

### 线 B · 行业情报输入

实测结论先记着，别重复调研：**泛热榜对电梯行业覆盖为零**（三平台 130 条命中 0）；
**政策库不是雷达是口径库**（存量 10 份，最新一份距今 466 天）；**下拉词才是雷达**
（10 个种子扩散出 102 条真实问句、噪声 0，且比政策文件滞后出现——那才是"正在办、正卡壳"）。
**参考货币**：MoneyPrinterTurbo（125k star）无选题/质检/去AI味，可借鉴的是接入形态（WebUI/API/CLI/批量/任务历史）。

- [ ] **B1** 抓取器：纯 HTTP、**零新增依赖**（`httpx` 已在 `requirements-runtime.txt`）。
  五档各按自己节奏，不搞统一"每日"：政策库季度 / 通报月 / 需求词日 / 热榜日 / B站按需。
  已验证端点与坑见 `intel/policy/pull_policy.py`（政策库 `t=zhengcelibrary_all` 返回空，必须 gw+bm 分开查；
  `searchfield=content` 跨行业污染，只有 title 级命中可用）。
  ⚠ 脚本目前硬编码绝对路径（`pull_policy.py:7`）→ 改 argv 传 data_dir
- [ ] **B2** **intel 落点改到 `data_dir`**：现在在仓库根，开发态能跑是因为 `data_dir` 默认等于 root，
  打包后安装目录不可写、会找不到（同 `store.py:53` 里 `generated/` 那条约定；
  先例还有 `config.py:121`）。引擎侧新建读取模块挂 `data_dir/"intel"`
- [ ] **B3** `GET /api/intel/today`：只读本地文件，**空或坏返回 `[]` 不抛**（返 plain dict 即可，
  schemas.py 无响应模型先例；token 鉴权自动覆盖，仅 `/api/health` 白名单 `server.py:271`）。
  ⚠ 与 `private_facts()` 的失败语义**相反**（`knowledge.py` 的 `Pack.private_facts()` 读不到要中止生成）；
  这条差异要写进注释，否则将来一定有人"顺手统一"
- [ ] **B4** 懒触发：打开选题页时按"上次抓取距今"决定是否后台补抓，**走 Job 管道**。
  ⚠ **先例是 `start_generate` / `start_packgen`**（P1-43 已修，packgen 早走 Job：路由
  `app/server.py` 的 `packs_create`，入口 `app/pipeline.py` 的 `start_packgen`），
  "别学 packgen 同步 POST"这句话已过时，别引错路。成本：新增状态要动
  `TRANSITIONS`+`BUSY_STATES`+progress.js 标签+`test_job_state_vocabulary_consistency.py`（都在 `app/jobs.py` 顶部）；
  且 `add_if_room` 与生成共享 `MAX_CONCURRENT_JOBS=4`（`app/pipeline.py`）→ **纯 HTTP 抓取用独立并发额度**，
  别跟 LLM 作业抢名额
- [ ] **B5** LLM 归并成选题卡：批量一次调用处理 20–30 条，走现成 `chat_json`。
  **必须有降级**：没配 Key 或离线时退化成"原始条目列表"（标题+出处+日期+来源标签），
  否则首装用户打开是一页空白
- [ ] **B6** 选题视图 UI：**左栏常驻入口 + 右栏切视图，不跳页、不整窗覆盖**（原型定稿见
  `_verify/_proto-topics-src.html`）。筛选轴 = 来源，一根轴不混别的维度；
  两种"没有"要长得不一样：`计数0`=已接入今天没货（留在行内、压淡）、`未接入`=不进筛选行只在来源面板说明。
  ⚠ 新增 nav-item 必须复用 `.nav-item` / `.stg-nav-item` 现成类 —— 这两个是同一控件的两处实例，
  `verify.js` 直接比它们的计算值
- [ ] **B7** `select_ctx` 注入 `$intel_block`（按 segment 取最相关 3 条、截断、带文号与 URL），
  `skill.yaml` 的 select 模板加一节【政策与行业动态：引用须带文号，不得照搬】。
  ⚠ 注入会改 `_plan_key` 指纹（`pipeline.py:459-461` 哈希渲染后 system+user）→ **需明确 intel 进不进指纹**，
  否则情报刷新会让同参数重选题、选题缓存打不中

### 已定不做

自媒体分发适配（一稿多发）· 抖音/小红书行业内容抓取（签名墙 + `MediaCrawler` 禁商用）·
拿 perplexity/句长方差或外部"降AI率"检测器当门槛 · 抓取器进生成主链路 · 把 `intel/` 打进安装包

### 待拍板

1. `ai_smell` 进不进回炉门槛（进 = 加轮次 = 加钱加时；分数公式与阈值来源）
2. 左栏角标是「今日总数」还是「未读数」（未读要多存一个状态）
3. 「来源与抓取记录」面板做不做（不做的话重抓只能靠懒触发）
4. 真人语料谁挑：手工 30 条，还是批量 ASR 出 200 条再人工筛；谁对样本跑 banwords 预检
5. 情报预算：只走"免费公开 + 用户自传创作后台 CSV"，还是允许采购第三方数据 API
6. **A6 样本最终归属**：改放 `data_dir/intel/samples/`（不随 packs 分发）是否确认
7. **B4 抓取并发额度**：独立额度还是复用 MAX=4（建议独立，避免和生成抢）
