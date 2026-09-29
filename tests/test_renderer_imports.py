# -*- coding: utf-8 -*-
"""渲染层的 import 图必须**在干净克隆里也解析得到**（2026-09-29 第 29 轮加）。

为什么这条守卫值得存在：`desktop/renderer/js/engine.js` 被 `main.js:15` 与
`sessions.js:26` import，但它当时**不在版本库里** —— 打包照样成功
（electron-builder 把 `renderer/` 整个目录 copy 走，`verify-package.mjs` 比的是
"包 == 工作区"，工作区里它有），本机一切正常；而干净克隆里
`import … from "./engine.js"` 直接抛 → 渲染层白屏。
这一类正是本仓记过的「运行时文件没入库」—— 靠人记得 `git add` 是不够的，得有门。

判据分三层，各挡一种坏法：
① 每个 `import … from "./x.js"` 与 index.html 的 `<script src="…">` 在**盘上**解析得到
   （文件被改名/删掉而忘了改引用 = 现在就白屏）；
② 这些文件没有一个被 **gitignore**（被忽略 = 永远进不了仓库，干净克隆必缺）；
③ 被**已入库**的代码 import 的文件，自己也必须已入库
   （今天这根红的就是它：引用者在仓库里，被引用者不在）。
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RENDERER = ROOT / "desktop" / "renderer"
JS_DIR = RENDERER / "js"

IMPORT_RE = re.compile(r"""^\s*import\s+(?:[^'"]*?from\s*)?["'](\./[^"']+\.js)["']""", re.M)
SRC_RE = re.compile(r"""<script[^>]+src=["']([^"']+\.js)["']""")


def _git(*args: str) -> str:
    """git 拿不到就返回空串（调用方按"跑不到"处理，不当通过）。"""
    import shutil
    exe = shutil.which("git")
    if not exe:
        return ""
    return subprocess.run([exe, "-C", str(ROOT), *args], capture_output=True,
                          text=True, encoding="utf-8", errors="replace",
                          check=False).stdout


def _tracked() -> set[str]:
    out = _git("ls-files", "-z", "--", "desktop")
    if not out:
        import pytest
        pytest.skip("git 不可用，这条判据跑不到（而不是当作通过）")
    return {p for p in out.split("\0") if p}


def _ignored(paths: list[str]) -> set[str]:
    """哪些路径被 .gitignore 命中（`git check-ignore` 逐条问，不自己实现忽略规则）。"""
    import shutil
    exe = shutil.which("git")
    if not exe:
        return set()
    out = subprocess.run([exe, "-C", str(ROOT), "check-ignore", "--stdin"],
                         input="\n".join(paths), capture_output=True, text=True,
                         encoding="utf-8", errors="replace", check=False).stdout
    return {p for p in out.splitlines() if p.strip()}


def _files() -> list[Path]:
    return sorted([*JS_DIR.glob("*.js"), RENDERER / "index.html",
                   RENDERER / "styles.css"])


def _edges() -> list[tuple[str, str, str]]:
    """`(引用者, 被引用的仓库相对路径, 引用它的代码是否已入库)`。"""
    tracked = _tracked()
    edges: list[tuple[str, str, str]] = []
    for f in _files():
        src = f.read_text(encoding="utf-8")
        refs = [m.group(1) for m in IMPORT_RE.finditer(src)]
        if f.name == "index.html":
            refs += [m.group(1) for m in SRC_RE.finditer(src)]
        for r in refs:
            if f.suffix == ".js":
                target = (f.parent / r).resolve()
            else:
                # index.html 走的是引擎的挂载点：`app/server.py:699`
                # `app.mount("/static", StaticFiles(directory=renderer))`
                # —— `/static/js/main.js` 就是 `desktop/renderer/js/main.js`。
                rel_url = r.split("?")[0].lstrip("/")
                if rel_url.startswith("static/"):
                    rel_url = rel_url[len("static/"):]
                target = (RENDERER / rel_url).resolve()
            rel = target.relative_to(ROOT).as_posix()
            src_rel = f.relative_to(ROOT).as_posix()
            edges.append((src_rel, rel, "是" if src_rel in tracked else "否"))
    return edges


def test_every_renderer_import_resolves_on_disk():
    bad = [f"{a} → {b}" for a, b, _ in _edges() if not (ROOT / b).exists()]
    assert not bad, "渲染层 import 了指向不存在文件的相对路径（现在就这样白屏）：\n" + "\n".join(bad)
    # 正对照：引用关系必须真的被解析出来了，空表也算"全绿"。
    assert len({b for _, b, _ in _edges()}) >= 10, (
        f"只解析出 {len(_edges())} 条 import 边 —— 正则与本仓的写法对不上，这条守卫在空转")


def test_no_imported_renderer_file_is_gitignored():
    targets = sorted({b for _, b, _ in _edges()})
    hit = _ignored(targets)
    assert not hit, "被 import 的渲染层文件被 .gitignore 命中 —— 它永远进不了仓库，" \
                    f"干净克隆必缺：{sorted(hit)}"


def test_imported_by_tracked_code_must_itself_be_tracked():
    tracked = _tracked()
    missing = sorted({f"{a} → {b}" for a, b, src_in in _edges()
                      if src_in == "是" and b not in tracked})
    assert not missing, (
        "已入库的代码 import 了一个**不在版本库里**的文件 —— 打包看不出来"
        "（electron-builder copy 整个 renderer 目录），干净克隆里渲染层直接起不来。"
        "提交时把它一起 add：\n" + "\n".join(missing))
