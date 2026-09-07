# TalkScript · 口播脚本智能体

本地运行的口播短视频脚本生成器：**Python 流水线引擎 + Electron 桌面壳 + 行业知识包**。
代码守住"能不能用"的下限（字数/时长/禁用词由校验器兜底并自动回炉），模型去拼"好不好听"的上限。

> 行业包配置、技能（skill.yaml）配置、知识库内容来源、导出为 Agent 技能——
> 见 **[docs/知识库与技能配置指南.md](docs/知识库与技能配置指南.md)**。
> 知识库内容格式规范 + 可复制给任何 AI 的整理指令，见 **[docs/知识库内容规范.md](docs/知识库内容规范.md)**。

## 架构

```
┌─ Electron 窗口（desktop/）── 渲染界面，localhost fetch
│      │ spawn + 轮询 /api/health
┌─ Python 引擎（app/）── 127.0.0.1 只读本机
│   pipeline: 参数归一 → 选题策划 → 文案撰写 → 校验回炉(≤2轮) → 组装落盘
│   checker : 字数/时长/两级禁用词（行业包词表 × 平台分级）
│   packgen : 输入行业名+描述 → 生成新行业包初稿（草稿态）
└─ packs/<行业>/ ── 知识包（合规词库/选题库/受众库/钩子库/配额规则/私有资料）
```

单次生成的模型调用只有 2~4 次（选题 1 + 撰写 1 + 回炉 0~2），其余全是确定性代码。

## 启动（开发模式）

```bash
# 1) Python 引擎依赖
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt

# 2) Electron 依赖
cd desktop && npm install

# 3) 配置模型（也可启动后在界面 ⚙ 设置 里填）
#    编辑 config.yaml：llm.api_key 填你的 OpenAI 兼容接口 Key
#    或设环境变量 TALKSCRIPT_API_KEY

# 4) 启动桌面应用（自动拉起引擎）
cd desktop && npm start
```

也可以不开壳，直接浏览器用引擎页：

```bash
.venv\Scripts\python -m app.server --port 8765
# 打开 http://127.0.0.1:8765
```

无 Key 调试：设 `TALKSCRIPT_MOCK=1`，引擎返回固定夹具，可跑通全流程（含"政府补贴"拦截回炉演示）。

## 打包 Windows 安装包

```bash
cd desktop && npm run dist
# 产物在 desktop/dist/：TalkScript Setup.exe（NSIS 安装包）+ portable exe
```

打包包含引擎代码与行业包；运行打包版需要目标机器装有 Python + 依赖
（或先用 PyInstaller 把引擎打成 engine.exe 放进 `desktop/extraResources`，主进程会自动优先使用）。

## 日常使用

1. 左栏选行业包、填主题、调参数，点「生成脚本」
2. 默认**一键直通**；勾选**分步确认**则选题后暂停，确认/编辑角度再写文案
3. 结果五个页签：口播（分段卡片，每段可**重写本段**）/ 分镜 / 合规 / JSON / 日志
4. 「＋新建」输入行业名+一句话描述，生成新行业包（草稿态，按校对清单核实后把 pack.yaml 的 `draft` 改为 `false`）
5. 历史记录在底部，点击回看

## 行业包结构（packs/elevator 为参照）

```
packs/elevator/
├── pack.yaml        # 清单：参数定义、语速/配额表、知识文件映射（引擎只认结构，不认行业）
├── banwords.yaml    # 禁用词：hard（必改）/ soft（待确认）× 平台升降级
├── compliance/      # 广告法 / 平台差异 / 行业红线
├── knowledge/       # 细分领域知识点 / 受众痛点 / 选题库 / 标准索引
├── patterns/        # 钩子库与风格 / 完播与转化
├── rules/           # 时长配额 / 输出模板
└── private/         # 你的私有资料（型号/服务/案例/异议应答），分享包时不带此目录
```

新建行业包 = 复制结构改内容，或用界面「＋新建」让模型生成初稿。

## 已知边界（v0.1）

- 真实生成质量取决于所配模型；草稿行业包内容必须人工校对后再投产
- 打包版依赖目标机器 Python 环境（自包含 engine.exe 打包见上）
- 私有资料仍以编辑 yaml 文件维护，界面化管理未做
