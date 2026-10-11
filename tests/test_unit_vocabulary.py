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


# 写死的分钟数：`分钟` 前面紧跟数字。从常量算出来的写法尾巴是 `} 分钟` / `) 分钟`，
# 不会命中 —— 这条判据管的是"后端预算一改，界面还在报一个不存在的时限"。
_HARD_MINUTES = re.compile(r"[0-9]+\s*分钟")


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
    """模型给的 `time` 可以比段落更细，所以内容不许动，只把 `s` 换成「秒」。"""
    assert _shot_time("0-4s", {}) == "0-4 秒"
    assert _shot_time("3-8 s", {}) == "3-8 秒"
    assert _shot_time("0.5-2s", {}) == "0.5-2 秒"
    assert _shot_time("0.0-3.0s", {}) == "0-3 秒"      # 尾零不进交付物
    assert _shot_time("开场到第 3 秒", {}) == "开场到第 3 秒"  # 认不出就原样留
    assert _shot_time("", {"start": 0, "end": 5}) == "0-5 秒"  # 没有才兜底
    assert _shot_time(None, {}) == ""


def test_shot_time_strips_trailing_zeros_on_both_entries():
    """第 47 轮 P3-1：分镜表「时间」列的**两条入口**守同一套清尾零。

    段落标题那条线第 28 轮就接上了 `_num`，分镜表这里漏了两条（兄弟路径
    不同权）：① 模型给的无后缀字符串（`0.0-9.2`，模板教的 `s` 后缀经常丢）
    原样放行；② 回退分支裸插值 float，交付「0.0-9.6 秒」。同一份交付物里
    段落标题写「0-9.2 秒」、分镜表写「0.0-9.2」，尾零是机器的、不是给人读的。
    透传字符串先解析成数再清，解析不了原样放行 —— 非数字表达不弄坏。
    """
    # ① 透传：无 s 后缀的数字区间，两端都清
    assert _shot_time("0.0-9.2", {}) == "0-9.2 秒"
    assert _shot_time("0.0-9.20", {}) == "0-9.2 秒"    # 9.20 的小数尾零也清
    assert _shot_time("3-8", {}) == "3-8 秒"           # 整数区间照常换单位
    assert _shot_time("0.0–9.2", {}) == "0-9.2 秒"     # 界面同款的 en dash 也认
    assert _shot_time("0.0~9.2", {}) == "0-9.2 秒"     # 波浪号分隔照旧
    # ② 回退：引擎算的段落轴是 float（0.0 / 9.6），裸插值曾带尾零
    assert _shot_time("", {"start": 0.0, "end": 9.6}) == "0-9.6 秒"
    assert _shot_time("", {"start": 0.0, "end": 12.0}) == "0-12 秒"
    # 非数字表达原样保留 —— 不能把模型的自由发挥弄坏
    assert _shot_time("开场到第 3 秒", {}) == "开场到第 3 秒"
    # 带 s 后缀的旧路（第 16 批修的那条）不回退
    assert _shot_time("0.0-3.0s", {}) == "0-3 秒"


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


def test_exported_md_storyboard_time_column_has_no_trailing_zeros():
    """端到端：导出的 脚本.md 分镜表「时间」列不许带机器尾零（第 47 轮 P3-1）。

    两条入口各演一遍：透传的无后缀字符串、回退的段落轴 float ——
    与段落标题的「0-3 秒」必须同一写法。
    """
    r = TA._md_result(None)
    r["timings"] = [{"start": 0.0, "end": 9.6}, {"start": 9.6, "end": 12.0}]
    r["storyboard"] = [{"shot": "轿厢·近景", "time": "0.0-9.2"},   # 透传
                       {"shot": "机房·全景"}]                       # 回退段落轴
    md = render_script_md(r)
    assert "| 0-9.2 秒 |" in md, md
    assert "| 9.6-12 秒 |" in md, md
    hits = [ln for ln in md.splitlines() if re.search(r"\b0\.0-|\d+\.0-", ln)]
    assert not hits, f"交付物里还有机器尾零：{hits}"


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


# ── 等待时长这类"同一个数写两遍"的账（第 27 轮契约线抓到的那一族）────────
def _renderer_code() -> str:
    return "\n".join(_strip_js_comments(p.read_text(encoding="utf-8"))
                     for p in sorted(JS_DIR.glob("*.js")))


def _count(src: str, literal: str) -> int:
    return src.count(literal)


def test_job_timing_numbers_are_written_once():
    """后端作业预算、轮询间隔、客户端兜底截止在渲染层各只许出现一次（都在 api.js）。

    改前是 `1200000` ×1 + `1800000` ×2 + `900` ×3，而界面上「20 分钟 / 30 分钟」
    两种说法分散在三处 prose —— 后端把预算一改，界面就在报一个不存在的时限。
    现在 `POLL_GRACE_MS` 是 `JOB_BUDGET_MS × 1.5` 算出来的，分钟数也从它推。
    """
    code = _renderer_code()
    api_js = _strip_js_comments((JS_DIR / "api.js").read_text(encoding="utf-8"))
    assert _count(code, "1200000") == 1, "作业预算的字面毫秒数只能有一份（在 api.js）"
    assert _count(code, "1800000") == 0, "兜底截止必须由 JOB_BUDGET_MS × 倍率算出来，不许写死"
    assert _count(code, "POLL_MS = 900") == 1, "轮询间隔只能定义一次"
    assert _count(api_js, "* 1.5") == 1, "宽限倍率改了要在这里改，别回到调用点各写一个数"
    # 分钟数一律从常量算：`Math.round(POLL_GRACE_MS / 60000)} 分钟` 里 `分钟` 前面
    # 是插值收尾的 `}`，而写死的说法（「等待超时（30 分钟）」）前面是数字。
    hard = [ln.strip()[:70] for ln in code.split("\n") if _HARD_MINUTES.search(ln)]
    assert not hard, f"这些字符串把分钟数写死了，后端预算一改就在报不存在的时限：{hard}"


def test_timing_scan_has_a_positive_control():
    """上面那条的计数器必须真的会数（把两种写法塞进假源码，都要被抓出来）。"""
    fake = ("const P = 900;\nif (t > 1800000) throw new Error(\"等待超时（30 分钟）\");\n"
            + "const R = 1200000;\ntest(`已等待 ${Math.round(G / 60000)} 分钟`);\n")
    assert _count(fake, "1800000") == 1 and _count(fake, "1200000") == 1
    hits = [ln for ln in fake.split("\n") if _HARD_MINUTES.search(ln)]
    assert len(hits) == 1 and "30 分钟" in hits[0], hits   # 算出来的那一条不许被抓
