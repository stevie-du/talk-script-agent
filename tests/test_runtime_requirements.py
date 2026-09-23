# -*- coding: utf-8 -*-
"""内嵌运行时的依赖清单与打包接线（P1-3）。

跑法：pytest tests/test_runtime_requirements.py

为什么要有这个文件
------------------
`requirements-runtime.txt`（装进发行包的那 5 个）与 `requirements.txt`（开发/测试）
**必然有一份是抄来的**，而抄来的东西会漂移：改了开发依赖的版本区间、忘了改运行时那份，
结果是「本地跑得好好的，装出来的包 import 失败」。

同一个道理，`desktop/package.json` 里那几行接线（`extraResources` 带 `vendor/py`、
`beforePack` 钩子、`files` 含 `engine-path.js`）**漏掉任何一条的表现都是
「构建成功、安装包装完启动即失败」** —— 一句提示都没有。所以在这里钉住。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DEV_REQ = ROOT / "requirements.txt"
RUNTIME_REQ = ROOT / "requirements-runtime.txt"
PKG = ROOT / "desktop" / "package.json"

LINE_RE = re.compile(r"^([A-Za-z0-9_.\-]+)(\[[^\]]*\])?(.*)$")


def _parse(path: Path) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        m = LINE_RE.match(line)
        assert m, f"{path.name} 里有解析不了的行：{raw!r}"
        out[m.group(1).lower()] = {"extras": m.group(2) or "",
                                   "spec": m.group(3).strip()}
    return out


@pytest.fixture(scope="module")
def dev():
    return _parse(DEV_REQ)


@pytest.fixture(scope="module")
def runtime():
    return _parse(RUNTIME_REQ)


def test_runtime_file_is_not_empty(runtime):
    """前提守卫：解析出空字典的话，下面每条断言都会空转。"""
    assert len(runtime) == 6, f"运行时应恰好 6 个依赖，实际 {sorted(runtime)}"
    assert ({"fastapi", "uvicorn", "httpx", "pydantic", "pyyaml",
             "python-multipart"} == set(runtime))


def test_runtime_packages_all_come_from_dev(dev, runtime):
    """运行时里的每个包都必须在 requirements.txt 里出现过。"""
    missing = sorted(set(runtime) - set(dev))
    assert not missing, f"这些包只写在 requirements-runtime.txt 里：{missing}"


def test_runtime_version_ranges_match_dev(dev, runtime):
    """版本区间必须逐字相同 —— 这才是防漂移的那一条。

    只比「包名存在」是不够的：两边都写了 fastapi，一边 `>=0.115` 一边 `>=0.90`，
    装出来的东西照样和本地不一样。
    """
    diff = {name: (dev[name]["spec"], runtime[name]["spec"])
            for name in runtime if dev[name]["spec"] != runtime[name]["spec"]}
    assert not diff, f"版本区间不一致（开发态, 运行时）：{diff}"


def test_runtime_excludes_test_dependencies(runtime):
    """pytest 不该进发行包 —— 装进去只是白送体积。"""
    assert "pytest" not in runtime


def test_runtime_drops_uvicorn_extras(runtime):
    """uvicorn 不带 [standard] extras。

    引擎走 `uvicorn.run(app, ...)`，没有 `--reload`，所以 httptools /
    watchfiles / websockets 全都用不上，`uvicorn.run` 会自动退回纯 Python 的
    h11 解析器。去掉 extras 少两个编译扩展、少几 MB，
    也少两个「这台机器没有对应 wheel」的可能。
    """
    assert runtime["uvicorn"]["extras"] == "", \
        f"运行时不该带 extras：{runtime['uvicorn']['extras']}"
    # 反过来确认开发态确实带了 —— 否则这条断言可能只是在测一个恒空的值
    assert _parse(DEV_REQ)["uvicorn"]["extras"] == "[standard]"


# ── 打包接线 ────────────────────────────────────────────────
@pytest.fixture(scope="module")
def pkg():
    return json.loads(PKG.read_text(encoding="utf-8"))


def test_extra_resources_ship_the_runtime(pkg):
    """`vendor/py` 必须被搬进 `engine/py` —— 这是 P1-3 的落地点。

    少了它：`resolveEngine` 找不到出厂运行时，安装包里没有解释器，
    在没装过 Python 的机器上启动即失败。
    """
    pairs = {(e.get("from"), e.get("to")) for e in pkg["build"]["extraResources"]}
    assert ("vendor/py", "engine/py") in pairs, f"extraResources 缺 vendor/py：{pairs}"
    # 另外三份也要在（引擎源码 / 行业包 / 渲染层）
    for src, dst in (("../app", "engine/app"), ("../packs", "engine/packs"),
                     ("renderer", "engine/renderer")):
        assert (src, dst) in pairs, f"extraResources 缺 {src} → {dst}"


def test_before_pack_hook_is_wired(pkg):
    """构建运行时挂在 electron-builder 的钩子上，而不是串在 npm script 里。

    串命令只有走 `npm run dist` 才会跑；直接调 electron-builder 就漏了，
    而漏了的后果是安装包里没有解释器、构建过程一句提示都没有。
    """
    assert pkg["build"].get("beforePack") == "scripts/before-pack.js"
    hook = PKG.parent / "scripts" / "before-pack.js"
    assert hook.exists(), "beforePack 指向的钩子文件不存在"
    assert "build-python-runtime.mjs" in hook.read_text(encoding="utf-8"), \
        "钩子没有真的去调构建脚本"


def test_packs_private_dir_is_not_shipped(pkg):
    """`packs/*/private/` 不能进安装包。

    README 承诺私有资料（未公开型号参数、客户案例）不外带，而守住这句话的
    只是 extraResources 里一个 glob 元素 —— 删掉它构建照样成功、一句提示都没有，
    泄露要等安装包被别人解压才发现。所以结构在这里拦，
    匹配行为由 `desktop/packaging.test.js` 用真匹配器对着磁盘上的文件钉。
    """
    entry = next((e for e in pkg["build"]["extraResources"] if e.get("from") == "../packs"), None)
    assert entry, "extraResources 里没有 ../packs 条目"
    negatives = [g for g in entry.get("filter") or [] if str(g).startswith("!")]
    assert any("private" in g for g in negatives), \
        f"../packs 没有排除 private/ 的规则，安装包会带出私有资料：{entry.get('filter')}"


def test_packaged_files_include_the_engine_path_module(pkg):
    """`engine-path.js` 必须进 asar。

    它是 main.js `require('./engine-path')` 的目标 —— 漏了它，
    打包后的主进程直接 `MODULE_NOT_FOUND`，窗口都开不出来。
    """
    files = pkg["build"]["files"]
    for name in ("main.js", "preload.js", "engine-path.js"):
        assert name in files, f"files 缺 {name}：{files}"


def test_build_script_pins_python_and_hash():
    """构建脚本必须钉住版本与 sha256，且带导入自检。

    三条都容易被「顺手简化」掉，而少了任何一条都会变成静默失败：
      · 不钉版本 → 装的 wheel ABI 与运行时不一致（实测踩过：cp314 装进 3.13）；
      · 不校验 sha256 → 下载被截断/被替换也不知道；
      · 去掉导入自检 → 上面两类问题都只能等用户在安装版上遇到。
    """
    src = (PKG.parent / "scripts" / "build-python-runtime.mjs").read_text(encoding="utf-8")
    assert re.search(r"const PY_VERSION = '\d+\.\d+\.\d+'", src), "没有钉住 Python 版本"
    assert re.search(r"const ZIP_SHA256 = '[0-9a-f]{64}'", src), "没有钉住 sha256"
    assert "--python-version" in src, "装依赖时没有钉住目标 Python 版本（ABI 会装错）"
    assert "--only-binary=:all:" in src, "没禁止现场编译，构建机会悄悄依赖编译器"
    assert "verifyRuntime" in src and "import app.server" in src, "缺少导入自检"


def test_build_script_compiles_bytecode_with_the_target_interpreter():
    """字节码必须用**运行时自己**的解释器编译。

    pip 编译 .pyc 用的是正在跑 pip 的那个解释器，而 `--python-version` 只管
    wheel 的 ABI 标签、**不管字节码版本**。开发机是 3.14 时，装出来的是
    `cpython-314.pyc`，3.13 一个都认不了 —— 发行包里会躺着一整份
    **永远用不上**的字节码。

    ⚠ **别把体积/耗时数字抄进注释或 docstring**：它们每次跑都可能变，
    抄进来就会过期（这个坑踩过两次，见报告教训 36）。
    要引用就写「跑 `node _verify/pyc-cost.js` 自己量」。

    修法是 `--no-compile`（不让 pip 编）+ 用运行时自己的解释器 `compileall`。
    这两步**必须成对**：只去掉 `--no-compile` 会退回错版本，
    只加 compileall 会留下一份错版本再叠一份对的。
    """
    src = (PKG.parent / "scripts" / "build-python-runtime.mjs").read_text(encoding="utf-8")
    assert "--no-compile" in src, "pip 会用它自己的解释器编译 .pyc（版本会错）"
    assert "compileBytecode" in src and "compileall" in src, "没有用运行时自己的解释器重编"
    assert "assertBytecodeMatchesRuntime" in src, "缺少「.pyc 版本标签必须匹配」的校验"


def _comment_lines(path: Path):
    """取出注释 / docstring 行 —— 只查这些行，免得误伤代码里的合法数字。"""
    in_doc = False
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        s = line.strip()
        if path.suffix == ".py":
            if '"""' in s:
                if s.count('"""') == 1:
                    in_doc = not in_doc
                yield i, line          # 起止行本身也算注释
                continue
            if in_doc or s.startswith("#"):
                yield i, line
        else:
            if s.startswith(("//", "*", "/*")):
                yield i, line


def test_no_hardcoded_measurement_numbers_in_comments():
    """实测数字不许抄进注释 / docstring —— 抄了必然过期。

    **根因不是记性差，是数字写在自然语言里就一定会和多份副本脱节** ——
    同一个量复测两次都不一样，更别说抄在好几处。
    完整经过（连踩两次）见报告的「教训 36」：**注释里只写该怎么做，不写历史。**

    所以判据定为：数字的可信来源只能有一个 —— **跑出来**。
      · 体积 / 耗时：`node _verify/pyc-cost.js`（自己取最小值）
      · 运行时目录大小：`du -sm desktop/vendor/py`
    注释里要引用就**指路**，不许抄值。「几十 MB」这种量级词可以，数字不行。
    """
    # 按**类别**扫，不按文件名枚举 —— 枚举出来的清单，新文件加入时不会自动覆盖：
    # `pyc-cost.js` 自己头部的注释就曾因此漏网（它不在清单里，于是照抄旧数字）。
    # `_verify/[!_]*.js`：下划线开头的是临时件（.gitignore 的 `_verify/_*`），跳过。
    targets = [
        *sorted((PKG.parent / "scripts").glob("*.mjs")),
        *sorted((ROOT / "_verify").glob("[!_]*.js")),
        Path(__file__),
    ]
    # 「数字 + 体积/时间单位」—— 正是会随版本与机器漂移的那类值
    stale = re.compile(r"\d+(?:\.\d+)?\s*(?:MB|s\b|秒)")
    offenders = []
    for f in targets:
        if not f.exists():
            continue
        for lineno, line in _comment_lines(f):
            if stale.search(line):
                offenders.append(f"{f.name}:{lineno}: {line.strip()[:90]}")

    assert not offenders, (
        "注释 / docstring 里出现了实测数字，它会过期（教训 36，已踩两次）：\n  "
        + "\n  ".join(offenders)
        + "\n\n改法：删掉数字改成指路 —— 「跑 node _verify/pyc-cost.js 自己量」。"
    )


def test_readme_python_version_matches_build_script():
    """README 里写的 Python 版本，必须和构建脚本钉住的一致。

    版本号放在两个地方就会各自漂移：改了 `PY_VERSION` 忘了改 README，
    用户读到的是错的。**把 README 里的数字删掉**是一种解法，
    但这个信息对用户是有用的 —— 所以改成**让不一致可见**：
    构建脚本是唯一来源，README 必须跟上，跟不上就报红。
    """
    src = (PKG.parent / "scripts" / "build-python-runtime.mjs").read_text(encoding="utf-8")
    m = re.search(r"PY_VERSION\s*=\s*['\"]([0-9][0-9.]*)['\"]", src)
    assert m, "构建脚本里读不到 PY_VERSION（改名了？那这条断言也要跟着改）"

    pinned = m.group(1)
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert pinned in readme, (
        f"README 没提到构建脚本钉住的 Python {pinned} —— 改了版本忘了改文档？\n"
        "  来源：desktop/scripts/build-python-runtime.mjs 的 PY_VERSION"
    )


def test_python_tag_is_derived_from_version_not_hardcoded():
    """`PY_TAG`（313）必须**从版本号推导**，不能写成硬编码的字面量。

    同一个信息的两种表示必然漂移：改了 `PY_VERSION` 却忘了改 tag，
    就会生成 `python313._pth` 去找其实并不存在的 `python314.zip` ——
    **构建照样报「成功」，产物却是坏的**（和 ABI 装错是同一类静默失败）。
    所以这里守住的不是「值对不对」，而是「它有没有自己的来源」。
    """
    src = (PKG.parent / "scripts" / "build-python-runtime.mjs").read_text(encoding="utf-8")
    tag_lines = [ln for ln in src.splitlines() if ln.strip().startswith("const PY_TAG")]
    assert tag_lines, "找不到 PY_TAG 定义（改名了？那这条断言也要跟着改）"

    tag_line = tag_lines[0]
    assert "PY_VERSION" in tag_line, (
        "PY_TAG 是硬编码的字面量 —— 它会和 PY_VERSION 漂移。\n"
        "  改成从版本号推导：PY_VERSION.split('.').slice(0, 2).join('')"
    )


def test_engine_path_matches_extra_resources_target():
    """运行时代码里写的出厂路径，必须和 `extraResources` 的**落点**一致。

    同一个落点在两个地方各写了一遍：`package.json` 的 `{"to": "engine/py"}`
    与 `engine-path.js` 的 `path.join(o.resourcesDir, 'engine', 'py', 'python.exe')`。
    改了 `to` 没改代码（或反过来）的后果尤其坏：出厂运行时找不到 →
    **悄悄降级到系统 Python**，用户根本不知道自己用的不是内嵌的那个 ——
    这正是本项目一直在整治的静默降级，而且它**不会报错**。

    上面那条断言守的是 `from`（构建脚本输出），这条守的是 `to`（运行时查找），
    两侧都钉住，才不会「只守了一半」。
    """
    src = (PKG.parent / "engine-path.js").read_text(encoding="utf-8")
    m = re.search(r"path\.join\(o\.resourcesDir,\s*([^)]*)'python\.exe'\)", src)
    assert m, "engine-path.js 里读不到出厂运行时路径（改写法了？这条断言也要跟着改）"

    parts = re.findall(r"'([^']+)'", m.group(1))     # 'engine', 'py' → engine/py
    code_path = "/".join(parts)
    assert code_path, f"没解析出出厂路径：{m.group(1)}"

    pkg = json.loads(PKG.read_text(encoding="utf-8"))
    tos = [e.get("to") for e in pkg["build"].get("extraResources", []) if isinstance(e, dict)]
    assert code_path in tos, (
        f"engine-path.js 写的出厂路径是 `{code_path}`，而 extraResources 的落点是 {tos} —— "
        "两边不一致会让出厂运行时找不到，然后**静默降级到系统 Python**（不报错）。"
    )


def test_extra_resources_points_at_the_runtime_build_output():
    """`extraResources` 里那条必须指向构建脚本**实际输出**的目录。

    同一个路径在两个地方各写一遍就会漂移：改了 `OUT_DIR` 忘了改 `package.json`
    （或反过来），构建出来的运行时**根本不会进包** ——
    而构建全程一句警告都没有，要等用户在自己机器上装完、启动失败才发现。

    JSON 没法引用 JS 常量，所以这里**推导不了**，只能让断言盯着两处一致
    （按「推导 > 校验 > 删掉」的优先级，这是中间那一档）。
    """
    src = (PKG.parent / "scripts" / "build-python-runtime.mjs").read_text(encoding="utf-8")
    m = re.search(r"OUT_DIR\s*=\s*path\.join\(DESKTOP,\s*([^)]+)\)", src)
    assert m, "读不到 OUT_DIR 的定义（改写法了？这条断言也要跟着改）"

    parts = re.findall(r"'([^']+)'", m.group(1))     # 'vendor', 'py' → vendor/py
    rel = "/".join(parts)
    assert rel, f"从 OUT_DIR 里没解析出相对路径：{m.group(1)}"

    pkg = json.loads(PKG.read_text(encoding="utf-8"))
    # 注意在 `build` 节点下；上面那条断言守的是「package.json 侧写错了吗」，
    # 这条守的是「构建脚本的输出目录改了、package.json 没跟上」（那一侧更隐蔽）。
    extra = pkg["build"].get("extraResources", [])
    froms = [e.get("from") for e in extra if isinstance(e, dict)]
    assert rel in froms, (
        f"package.json 的 extraResources 里没有 `{rel}`（现在是 {froms}）—— "
        "改了构建脚本的输出目录却忘了改打包配置？运行时不会被打进包。"
    )


# ── probe 的 import 列表 ↔ requirements-runtime.txt 对账 ──────────
# 为什么需要：build-python-runtime.mjs 的导入自检 probe 是**显式写死的**
# import 列表（不能动态读：PyPI 包名 ≠ import 名，pyyaml→yaml、
# python-multipart→multipart，动态转换必错）。写死的列表会与
# requirements-runtime.txt 漂移 —— 漂的方向是"加了依赖没进 probe"：
# 装出来的运行时缺那个包，而自检照样全绿（它压根没导）。
# 实测踩过：2026-09-23 加 python-multipart 时 probe 还是旧的 5 个 import。
IMPORT_NAME = {"pyyaml": "yaml", "python-multipart": "multipart"}


def test_probe_imports_cover_runtime_requirements(runtime):
    """requirements-runtime.txt 的每个包都必须出现在 probe 的 import 里。"""
    src = (PKG.parent / "scripts" / "build-python-runtime.mjs").read_text(encoding="utf-8")
    m = re.search(r"const probe = \[(.*?)\]\.join", src, re.S)
    assert m, "读不到 probe 的定义（改写法了？这条断言也要跟着改）"
    imported = set(re.findall(r"^\s*'import ([^']+)'", m.group(1), re.M))
    modules: set[str] = set()
    for line in imported:
        modules.update(x.strip() for x in line.split(","))
    need = {IMPORT_NAME.get(p, p) for p in runtime}
    missing = sorted(need - modules)
    assert not missing, (
        f"这些运行依赖没进 probe 的 import 列表：{missing} —— 装了但自检不导，"
        "缺了它们自检照样全绿。请在 build-python-runtime.mjs 的 probe 里补上"
        "（import 名与 PyPI 包名不同的，加进本文件的 IMPORT_NAME 映射）")
