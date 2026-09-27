# -*- coding: utf-8 -*-
"""CSS 里 `var(--x)` 引用的每一个 token，必须真的在某处被定义（第 27 轮布局线）。

现场：`.pl-item .pl-s.imported { color: var(--accent) }` —— 而**全站没有定义过
`--accent`**，注释还写着「--accent 是界面既有的强调色」。未定义又没有 fallback 的
自定义属性在计算期失效，于是"这个包不是出厂带的"那层颜色从来没上过：
声明在、规则读得到、屏幕上什么都没有 —— 门禁量 `color` 的计算值也只会量到"继承来的色"，
不报错，所以没有任何东西会响。

判据方向：**用到的 ⊆ 定义得到的**。
定义来源算三处（styles.css 与 index.html 的 `<style>`/内联 style，以及渲染层
`style.setProperty("--x", …)` 在运行时写进去的那些 —— 它们是合法来源，不算幽灵）。
JS 那两个名字是**扫出来的**不是写死的名单：改名或新增一律自动跟上。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

CSS_F = ROOT / "desktop" / "renderer" / "styles.css"
HTML_F = ROOT / "desktop" / "renderer" / "index.html"
JS_DIR = ROOT / "desktop" / "renderer" / "js"

_USE = re.compile(r"var\(\s*(--[a-zA-Z0-9-]+)")
_DEF = re.compile(r"(--[a-zA-Z0-9-]+)\s*:")
# `style.setProperty("--w", ...)`：运行时写进元素上的自定义属性
_SET = re.compile(r"""setProperty\(\s*["'](--[a-zA-Z0-9-]+)["']""")
# 浏览器/引擎自带的名字（不在我们的表里，也不该在）
_BUILTIN = {"--color-scheme", "--field-sizing"}


def _strip_css_comments(src: str) -> str:
    return re.sub(r"/\*[\s\S]*?\*/", "", src)


def _defined(css: str, html: str, js: str) -> set[str]:
    return set(_DEF.findall(css)) | set(_DEF.findall(html)) | set(_SET.findall(js))


def _offenders(sources):
    """sources: [(名字, 文本)] —— 返回"用了但没人定义"的 token 名。"""
    css = _strip_css_comments(CSS_F.read_text(encoding="utf-8"))
    html = _strip_css_comments(HTML_F.read_text(encoding="utf-8"))
    js = "\n".join(_strip_js(p.read_text(encoding="utf-8")) for p in sorted(JS_DIR.glob("*.js")))
    have = _defined(css, html, js) | _BUILTIN
    out = []
    for name, text in sources:
        seen = set()
        for lineno, line in enumerate(_strip_css_comments(text).split("\n"), 1):
            for tok in _USE.findall(line):
                if tok not in have and tok not in seen:
                    seen.add(tok)
                    out.append(f"{name}:{lineno} 用了未定义的 {tok}")
    return out


def _strip_js(src: str) -> str:
    src = re.sub(r"/\*[\s\S]*?\*/", "", src)
    return "\n".join(ln for ln in src.split("\n") if not re.match(r"^\s*//", ln))


def test_no_phantom_css_token():
    offenders = _offenders([("styles.css", CSS_F.read_text(encoding="utf-8")),
                            ("index.html", HTML_F.read_text(encoding="utf-8"))])
    assert not offenders, f"这些 var() 引用的 token 没有任何定义处：{offenders}"


def test_phantom_token_scan_is_not_a_placebo():
    """正对照：幽灵 token 必须被抓、有定义处的必须放过、注释里写的不算代码。

    `--accent` 这一族最容易假绿的地方是"从注释里扫" —— 病灶说明一律写着旧名字。
    """
    bad = "a { color: var(--never-defined-here); }\n/* b { color: var(--also-ghost); } */\n"
    hits = _offenders([("t.css", bad)])
    assert len(hits) == 1 and "never-defined-here" in hits[0], hits
    good = ".x { color: var(--ink); background: var(--w); }\n"
    assert _offenders([("t.css", good)]) == []
    # 用而不存在的名字，其定义来自 JS 运行时 setProperty —— 也不能报（今天 --w/--i 就是这个形状）
    assert "--w" in _defined("", "", 'n.style.setProperty("--w", v);')


# ── 规范 §4：CSS 不许注入字形图标 ─────────────────────────────
# 现场（第 27 轮布局线抓到）：`.banner.why > summary::after { content: " ▾" }`
# 与它的 `[open]` 兄弟 —— 字形图标换一副字体就换一个长相，读屏还会把它念出来，
# 而这一族的正确画法在同一个文件里已经存在三份（.st-adv / .pg-more 的边框三角）。
# 判据写成"content 只能是无形的（空串 / none / attr / 计数器）"，
# 比枚举被禁字形稳：没人记得全 Unicode 里有多少个箭头。
_CONTENT = re.compile(r"(?<![-\w])content\s*:\s*([^;{}]+)")
_OK_CONTENT = re.compile(r"""^\s*(none|normal|""|''|attr\(|counter\(|counters\(|var\(|open-quote|close-quote)""")
# 允许的非图标用法：中点/斜杠这类**分隔符**（副标题里「导入 · 作者 · license」那一格）。
# 这是名单不是通用规则 —— 加一项要写清为什么，别把字形图标塞进来。
_SEP_CONTENT = {'"·"', "'·'", '"/"', '"："', "'：'", '"|"'}


def _content_values(src: str):
    out = []
    for lineno, line in enumerate(_strip_css_comments(src).split("\n"), 1):
        for m in _CONTENT.finditer(line):
            out.append((lineno, m.group(1).strip()))
    return out


def test_css_does_not_inject_glyph_icons():
    bad = [(n, v) for n, v in _content_values(CSS_F.read_text(encoding="utf-8"))
           if not _OK_CONTENT.match(v) and v not in _SEP_CONTENT]
    assert not bad, f"这些 content 把字形当图标（规范 §4）：{bad}"


def test_glyph_scan_is_not_a_placebo():
    """正对照：带图标的 content 要被抓，空串 / 分隔符 / flex 的 *-content 都不许误报。"""
    src = ('a::after { content: " ▾"; }\n'
           + 'b::before { content: ""; }\n'
           + 'c { justify-content: flex-start; align-content: safe center; }\n'
           + "d::after { content: none; }\n"
           + 'e::after { content: "·"; }\n'
           + 'f::before { content: "✓"; }\n')
    vals = [v for _, v in _content_values(src)]
    assert "flex-start" not in vals and "safe center" not in vals, vals
    bad = [(ln, v) for ln, v in _content_values(src)
           if not _OK_CONTENT.match(v) and v not in _SEP_CONTENT]
    assert [v for _, v in bad] == ['" ▾"', '"✓"'], bad
