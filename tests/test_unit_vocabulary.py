# -*- coding: utf-8 -*-
"""时长在两侧只有一种读法（第 16 批：单位与话术）。

现场：同一个钟在一张卡片里印过 `45.0s`、`已用 45 秒`、`12s` 三种；导出的
`脚本.md` 里段落标题写「0-5 秒」、分镜表写「0-3s」（模型照 `rules/output-template.md`
的模板抄的），而界面与导出是同一份产物的两面 —— 读的人要先做一次心算才敢确定
「60s」和「1 分」是同一个数。

判据分三层，缺一层就还会漂：
  1. **纯函数真值表**：`_secs` / `_shot_time` 的每一档边界（Python 侧），
     与前端 `util.js` 的 `fmtDur` / `sec` / `shotTime` 同一张表（那条在 verify.js 里）；
  2. **端到端**：真渲染一份 `脚本.md`，整份文档不许出现 ASCII 秒；
  3. **静态扫描**：渲染层与引擎里"把变量拼在 `s` 前面"的写法一律报红，
     守卫自己带正对照（坏例必须被抓、好例必须放过），否则它只是一个能变绿的形状。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from app.llm import LLMClient                    # noqa: E402
from app.store import _secs, _shot_time, render_script_md   # noqa: E402
import test_ai_tells as TA                       # noqa: E402  复用它的 md 最小产物

JS_DIR = ROOT / "desktop" / "renderer" / "js"

# 「N 秒」被写成 ASCII 的三种形状：变量后面紧跟 s、字面数字后面紧跟 s、
# **拼出来的** `x + "s"`（第 27 轮审查抓到：`ui.js setCfgHint()` 就是这么漏过前两种的）。
# 后面不能再跟字母/数字（`1.1s ease` 是 CSS，`2px`/`ms` 同理不在此列）。
_ASCII_SEC = re.compile(r"(?:\}s|[0-9]s)(?![0-9A-Za-z_])")
_ASCII_SEC_CONCAT = re.compile(r"""\+\s*["']s["']""")


def _ascii_sec_hit(line: str) -> bool:
    return bool(_ASCII_SEC.search(line) or _ASCII_SEC_CONCAT.search(line))


# ── 1. 纯函数真值表 ──────────────────────────────────────────
def test_secs_matches_the_ui_tiers():
    """档位与前端 `fmtDur` 完全一致：<10 一位小数，其余先取整再换档。

    59.6 秒那一格是两侧最容易分叉的地方：先判档会留「60 秒」，
    前端 round-then-tier 给「1 分」—— 同一份产物两侧不同数。
    """
    assert _secs(0.05) == "0.1 秒"
    assert _secs(2.5) == "2.5 秒"
    assert _secs(9.96) == "10 秒"
    assert _secs(59.4) == "59 秒"
    assert _secs(59.6) == "1 分"
    assert _secs(60) == "1 分"          # 整分不补「0 秒」
    assert _secs(90) == "1 分 30 秒"
    assert _secs(float("nan")) == ""    # 说不出来 ≠ 说 0
    assert _secs(-1) == ""


def test_secs_does_not_use_bankers_rounding():
    """Python 的 `round` 是银行家舍入（2.5→2），前端 `Math.round` 是四舍五入。

    不显式 `floor(x+0.5)` 对齐，同一段时长两侧能差 1 秒 —— 而这正是「两侧
    同一本账」这条规则唯一被写下来的地方。
    """
    assert _secs(3.5) == "3.5 秒"       # <10 那一档本来就带小数
    assert _secs(12.5) == "13 秒"
    assert _secs(13.5) == "14 秒"


def test_shot_time_only_swaps_the_unit():
    """模型给的 `time` 可以比段落轴更细，所以内容不许动，只把 `s` 换成「秒」。"""
    assert _shot_time("0-4s", {}) == "0-4 秒"
    assert _shot_time("3-8 s", {}) == "3-8 秒"
    assert _shot_time("0.5-2s", {}) == "0.5-2 秒"
    assert _shot_time("0.0-3.0s", {}) == "0-3 秒"      # 尾零不进交付物
    assert _shot_time("开场到第 3 秒", {}) == "开场到第 3 秒"  # 认不出就原样留
    assert _shot_time("", {"start": 0, "end": 5}) == "0-5 秒"  # 没有才兜底
    assert _shot_time(None, {}) == ""


# ── 2. 端到端：整份 脚本.md 不许出现 ASCII 秒 ────────────────
def test_exported_md_has_no_ascii_seconds():
    md = render_script_md(TA._md_result(None))
    assert "- 1 分 / 抖音 / 亲和接地气 / 维保老师傅" in md, md.splitlines()[1]
    assert "预估 3 秒 ·" in md, md
    hits = [ln for ln in md.splitlines() if _ASCII_SEC.search(ln)]
    assert not hits, f"交付物里还有 ASCII 秒：{hits}"


def test_exported_md_storyboard_time_column_is_not_two_accounts():
    """分镜表的「时间」列以前直接吐模型的 `time`（`0-4s`），段落标题却是「0-3 秒」。

    两侧同一条时长两种读法之外，更要紧的是：**同一份文档**里两种读法。
    """
    r = TA._md_result(None)
    r["storyboard"] = [{"shot": "轿厢·近景", "time": "0-4s"}]
    md = render_script_md(r)
    assert "| 0-4 秒 |" in md, md
    assert "0-4s" not in md


# ── 3. 静态扫描：句子只有一本账 ──────────────────────────────
def _strip_js_comments(src: str) -> str:
    src = re.sub(r"/\*[\s\S]*?\*/", "", src)
    src = "\n".join(ln for ln in src.split("\n") if not re.match(r"^\s*//", ln))
    return re.sub(r"//[^\n]*", "", src)


def _js_sources():
    for p in sorted(JS_DIR.glob("*.js")):
        yield p.name, _strip_js_comments(p.read_text(encoding="utf-8"))
    html = (ROOT / "desktop" / "renderer" / "index.html").read_text(encoding="utf-8")
    yield "index.html", re.sub(r"<!--[\s\S]*?-->", "", html)


def test_no_site_renders_ascii_seconds():
    bad = [(n, i, ln.strip()[:70]) for n, src in _js_sources()
           for i, ln in enumerate(src.split("\n"), 1) if _ascii_sec_hit(ln)]
    assert not bad, f"这些行把时长拼成 ASCII 秒（要走 util.js 的 sec/fmtDur）：{bad}"


def test_ascii_second_scan_is_not_a_placebo():
    """正对照：坏例必须被抓、好例必须放过 —— 否则上面那条只是个能变绿的形状。"""
    caught = [ln for ln in ("const t = `${d}s`;", "tip = `60s`;",
                            'parts.push(getParam("duration") + "s");',
                            "x = `${a} 秒`;") if _ascii_sec_hit(ln)]
    assert len(caught) == 3 and "+ \"s\"" in caught[2], caught
    # 毫秒、像素、CSS 变量都不是"时长的第二种读法"，不该被顺手扫进来
    #（动画时长一律写在 styles.css，扫描范围只有 js 与 index.html）
    assert not any(_ascii_sec_hit(ln) for ln in
                   ("poll(900ms);", "width: 24px;", ".rt-b { color: var(--ok) }",
                    "setTimeout(fn, 1500);", "gap: var(--s2) var(--s3);",
                    'const k = n + "sec-key";'))


def test_rate_limit_note_is_written_once():
    """「N 秒后重试」这句在 llm.py 的代码里只能有一份（流式/非流式两条分支曾各写一遍）。

    文档串里引用这句病因的人不少（`_clamped_wait` 就在讲"界面写着等 60 秒、
    实际 2 秒就继续"），所以扫描要先按 AST 把 docstring 摘出去 ——
    不摘就是把讲病因的人钉死（同 test_intel_contract 那条教训）。
    """
    import ast

    assert LLMClient._rate_limit_note(2.5) == "上游限流(429)，2.5 秒后重试"
    assert LLMClient._rate_limit_note(60.0) == "上游限流(429)，60 秒后重试"
    src = (ROOT / "app" / "llm.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    docs = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if isinstance(body, list) and body \
                and isinstance(body[0], ast.Expr) \
                and isinstance(body[0].value, ast.Constant) \
                and isinstance(body[0].value.value, str):
            docs.add(id(body[0].value))
    hits = [n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
            and "秒后重试" in n.value and id(n) not in docs]
    assert len(hits) == 1, f"这句在代码里被抄了 {len(hits)} 遍，改字面时必漏一处：{hits}"


def test_engine_and_renderer_agree_on_the_unit_word():
    """两侧都用「秒」而不是 `s`：话术表在 docs/UI视觉规范.md §八，这里盯住实现。"""
    util = (JS_DIR / "util.js").read_text(encoding="utf-8")
    assert " 秒" in util and " 分" in util and " 毫秒" in util
    body = (ROOT / "app" / "store.py").read_text(encoding="utf-8")
    assert "_secs" in body and "_shot_time" in body
