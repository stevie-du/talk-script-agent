# TalkScript 打包 / 运行时（已落地，2026-09-15）

> 本文件是 P1-3 的权威结论。5 步实机清单见 README「安装与卸载的行为」。
> **改打包相关的东西之前先读本文件。**

## 一、形态：内嵌 Python 运行时（zip 随包，但构建时下载）

- 安装包必须**自带解释器**。`resolveEngine` 以前第一优先级是
  `resources/engine/engine.exe`，但没有任何构建步骤产出它 —— 打包态只剩 PATH
  里的 `python`，而它未必装了那 5 个依赖。**装完启动即失败**。
- 三条路（内嵌运行时 / PyInstaller / 声明需预装）里选了**内嵌**：
  体积 38.7 MB 与 PyInstaller 相当，但**报错与开发态一字不差**
  （bootloader 会把 traceback 变形，那类失败最难查）。
- 用 **embeddable 免 pip 版**（zip 10.4 MB），**不解压到用户目录** ——
  运行时目录只读即可（`--data-dir` 已指向 userData）。
- 钉死 **Python 3.13.12**，zip **构建时下载 + 校验 sha256**（不入库）。

## 二、`resolveEngine` 降级链（`desktop/engine-path.js`）

```
TALKSCRIPT_PYTHON → engine/py/python.exe（出厂自带）→ engine.exe → 项目 .venv → PATH
```

**顺序 = 我们期望用户跑哪个，不是「哪条路先被写出来」。**
出厂路径必须排在 `engine.exe` 之前。

- 纯逻辑已从 `main.js` 拆出来（注入 `exists`），`node --test desktop/engine-path.test.js`
  能直接验顺序 —— **读码验不了「先后」，得变成可执行的**。
- 返回值多一个 `via` 字段：日志会打 `engine (engine/py（出厂运行时）): …`，
  引擎起不来时第一件要问的就是这个。

## 三、构建：`desktop/scripts/build-python-runtime.mjs`

零依赖（自己实现 zip 解压，代理走 `HTTPS_PROXY` 的 CONNECT 隧道）。
流程：下载 → 校验 sha256 → 解压 → 写 `._pth` → `pip install --target` → **导入自检** → 落 stamp。

挂在 electron-builder 的 **`beforePack`** 钩子上（`scripts/before-pack.js`），
不是在 npm script 里串命令 —— 串命令只有走 `npm run dist` 才会跑。

**幂等**：stamp 记 {Python 版本, zip sha256, requirements 哈希}，三者不变就跳过。

### ⚠ 四个必须记住的点

1. **`pip install` 必须带 `--python-version`。** 宿主是 3.14 而运行时钉 3.13 时，
   不钉版本会按**宿主** ABI 挑 wheel，装进 `cp314` 的扩展 →
   `ModuleNotFoundError: No module named 'pydantic_core._pydantic_core'`。
   **脚本当时报「完成 38.7 MB」，看着完全正常** —— 第一次就踩到了。
2. **必须有导入自检**（用产物自己的解释器 `import` 那 5 个依赖 + `app.server`）。
   sha256 管不了 ABI、版本自检管不了 site-packages。自检要按**打包后的真实布局**跑
   （临时铺一份 `app/`，跑完删），别用 `PYTHONPATH` 作弊。
3. **`--python-version` 只管 wheel 的 ABI 标签，不管字节码版本。**
   pip 编译 `.pyc` 用的是**正在跑 pip 的那个解释器** —— 宿主 3.14 时装出来的
   `__pycache__` 全是 `cpython-314.pyc`，3.13 一个都认不了。
   **体积口径（复测过，别再用早期的 1.8 MB）**：同一份 `site-packages`
   交给 3.14 编 → 404 个 / **6.76 MB**；交给 3.13 编 → 404 个 / **5.77 MB**（净减 ~1 MB）。
   修法两步**必须成对**：`pip install --no-compile` + 用运行时自己的解释器
   `python.exe -m compileall -q -j 0 Lib/site-packages`。
   守卫两层，**都要有**：
   - 构建脚本 `assertBytecodeMatchesRuntime()`：错配就**构建失败**（早）
   - `_verify/runtime-smoke.js` 产物级 `.pyc` 标签扫描：验**交付物**（晚）
   > ⚠ **别声称它影响启动速度**：我最初量到 1.15s vs 0.67s，取最小值重测只有
   > **~0.03s**（噪声内）—— 那个 0.5s 是重建后磁盘冷缓存的假象。
   > 这条修的是「交付物里不该有永远用不上的内容」。
   > **改动注释时记得同步**：`compileBytecode()` 的注释曾长期留着被证伪的「0.5 秒」。
   **为什么两层都要**（变异实测）：M7 只去掉 `--no-compile` → 构建失败（校验还在）；
   M8 把编译+校验**一起删** → **构建「成功」**（退出码 0，42.1 MB），
   只有产物级断言报红（11/12，404 个错配）。单一防线会被「嫌麻烦的人」一并删掉。
4. **暂存目录一律「先删再铺」**，不要「已存在就跳过」—— 残留会让这次直接跳过，
   而且跳过之后也不会被清理（踩过）。

## 四、`._pth` 必须写这四行

```
python313.zip
.
Lib\site-packages
..
import site
```

内嵌发行版默认**禁用 `site`**，且 `._pth` 存在时进入 isolated 模式 ——
**cwd 不进 `sys.path`**。原版只有前两行且 `import site` 是注释掉的。
缺 `Lib\site-packages` 第三方包 import 不到；缺 `..`（= `resources/engine`，`app/` 在那儿）
会报 `No module named 'app'` —— 报错方向看起来像「模块没写对」。

## 五、依赖清单：`requirements-runtime.txt`

装进发行包的那 5 个：`fastapi / uvicorn / httpx / pydantic / pyyaml`。

与 `requirements.txt` 的差别（**会漂移，有断言守着**）：
- **不带 pytest**（白送体积）；
- **uvicorn 不带 `[standard]`** —— 引擎走 `uvicorn.run(app, ...)`、没有 `--reload`，
  `httptools` / `watchfiles` / `websockets` 全用不上，会自动退回纯 Python 的 `h11`。
  少两个编译扩展、少几 MB，也少两个「这台机器没有对应 wheel」的可能。

`tests/test_runtime_requirements.py` 断言：每个包都在 `requirements.txt` 里出现过、
**版本区间逐字相同**、不含 pytest、不带 extras；并钉住 `package.json` 的三处接线
（`extraResources` 带 `vendor/py`、`beforePack` 钩子、`files` 含 `engine-path.js`）——
**漏掉任何一条的表现都是「构建成功、装完启动即失败」。**

## 六、验证工具

| 命令 | 验什么 |
|---|---|
| `node desktop/scripts/build-python-runtime.mjs` | 构建运行时（幂等） |
| `node --test desktop/engine-path.test.js` | 降级链的每条路径与**先后** |
| `node _verify/runtime-smoke.js` | 端到端：按打包布局真起引擎 + 真跑一次生成（会清掉 `PYTHONPATH`/`PYTHONHOME`，证明不借宿主环境） |
| `node _verify/runtime-smoke.js --root desktop/dist/win-unpacked/resources/engine` | **对着打出来的产物**跑同一套断言（只读，不铺不删） |
| `pytest tests/test_runtime_requirements.py` | 依赖清单不漂移 + 打包接线没被拆 |

## 七、实测记录（2026-09-15，真跑过 `npm run dist`）

```
npm run dist  →  1m46s，退出码 0
[py-runtime] 已是最新（3.13.12，38.7 MB），跳过     ← beforePack 确实被调用（走了幂等分支）

desktop/dist/
  TalkScript Setup 0.2.0.exe   96 MB   ← NSIS 安装包
  TalkScript 0.2.0.exe         96 MB   ← portable
  win-unpacked/resources/engine/{app,packs,py,renderer}
```

用**产物本身**冒烟：`--root …/resources/engine` → **12/12 通过**。

真启动那个 app（抓主进程日志）：

```
[talkscript] engine (engine/py（出厂运行时）):
  …\resources\engine\py\python.exe -m app.server --port 57909
  --root …\resources\engine --data-dir C:\Users\78470\AppData\Roaming\TalkScript
  --token … --version 0.2.0
GET http://127.0.0.1:57909/api/health → 200 {"ok":true,"version":"0.2.0","mock":false}
```

**安装包在没装过 Python 的机器上能跑，这件事现在是实测过的。**

⚠ 本机直接双击 exe 会崩在 GPU（`GPU process isn't usable. Goodbye.`），
加 `--no-sandbox` 正常 —— 本环境已知问题，与 P1-3 无关。

### 解包核对（用 7za 读安装包，不装、不改系统）

`node_modules/7zip-bin/win/ia32/7za.exe l "dist/TalkScript Setup 0.2.0.exe"`
（⚠ **只有 arm64 / ia32 两个，没有 x64** —— 早先笔记里的 x64 路径已失效）
能直接列出 NSIS 载荷（1249 个条目）。核对结果：
`py/python.exe`、`python313._pth`、`python313.zip`、`app/server.py`、
`packs/elevator/pack.yaml`、`renderer/index.html`、`app.asar` 全在；
编译扩展是 `_pydantic_core.cp313-win_amd64.pyd` / `_yaml.cp313-win_amd64.pyd` ✓
（证明 ABI 修正确实进了安装包）。

**这个手法值得记**：想验「安装包里到底装了什么」不必真安装 ——
`7za l` / `7za x` 能直接读 NSIS 载荷，零系统改动。

### 怎么查「某笔改动进没进产物」

1. **UI 改动直接看文件**：renderer 走 `extraResources`，落在
   `desktop/dist/win-unpacked/resources/engine/renderer/` —— **是纯文件，不在 app.asar 里**。
   所以直接 `grep` / `diff` 那个目录即可，不用解包。
2. ⚠ **别去 `grep` `app.asar`**：asar 是打包格式，grep 什么都搜不到，
   连必定存在的 `settings-screen` 也是 **0** → 必然得到「产物不含这个改动」的
   **假阴性**，差点据此误判。**搜之前先拿一个必定命中的串做正对照**（教训 39）。

**产物过期是常态**：`dist/` 不会自动跟着源码更新。2026-09-16 实测：
07:31 打的 `app.asar` / `renderer/styles.css`，不含 10:07 提交的滚动条修复。
→ 想让打包版带上新改动，**必须重跑 `npm run dist`**；判断依据是上面的内容核对，
不是时间戳（时间戳也可能骗人，比如只改了注释）。

## 八、还没做的

- **没验 NSIS 安装流程本身**（真装一次、从开始菜单启动、卸载是否残留）——
  要在干净机器/虚拟机里做。但「安装包里的运行时能用」已由上面三步证明。
- `desktop/dist*/` 旧目录（`dist2`…`dist15`）删不掉：**环境对 `.asar` 的保护性拦截**
  （文件可写、能截断，但删不掉也改不了名；`Remove-Item` 被静默吞）。
  不是文件锁，是沙箱把它当「不许动」的目标。**要在资源管理器里手动删。**
- `desktop/vendor/` 已 gitignore（运行时 + 下载缓存，约 50 MB）。
