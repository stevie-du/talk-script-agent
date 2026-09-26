# -*- coding: utf-8 -*-
"""渲染层读到的每一个 intel 字段，引擎必须真的发（契约线 P2-5 / P3-7 的根因守卫）。

现场（2026-09-27 第 6 批改掉的）：`desktop/renderer/js/topics.js` 的选题卡片有一条
`it.warn` 渲染支，而 `app/intel.py` **从不发**条目级 `warn` —— 结论只走 `flags`
（形如 `["warn", "数据不足 · 算不出机会分"]`）。那条分支永远走不到；更糟的是 UI 桩还
给这个字段喂了值，于是门禁一直在演一条不存在的能力。
删掉渲染支之后，"下次再有人读一个引擎不发的键"就没有自动信号了 —— 这条测试补那个信号。

判据方向：**选题视图读的键 ⊆ 引擎真发出的键**。
拿真包（packs/elevator）+ 假 HTTP 抓一次落到临时 data_dir，再走 `intel.today()`，
三个层级（视图 / 源分组 / 条目）的键取并集当作"引擎发过的东西"。
两个必须记住的细节：
  1. **先剥注释再扫**（本仓的病灶说明一律写着旧字段名，不剥就把讲病因的人钉死 ——
     写这条测试时就被自己的注释绊过一次）；
  2. 切片锚点用 `index()` 找，找不到**直接抛**：改了函数名要跟着改这里，别删判据。
"""
from __future__ import annotations

import re
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from app import intel as I  # noqa: E402
import test_intel as T      # noqa: E402  复用它的假 HTTP 与源声明，不再造第二份夹具

TOPICS_JS = ROOT / "desktop" / "renderer" / "js" / "topics.js"

# 视图层 / 源分组 / 条目 —— 选题视图读 payload 的三处入口。
_READ = re.compile(r"\b(?:it|g|data)\.([A-Za-z_][A-Za-z0-9_]*)\b")
_SLICES = (
    ("卡片", "function card(it) {", "\nfunction renderChips"),
    ("源表", "function renderSources() {", "\nfunction doGenerate"),
    ("视图", "function render() {", "\nfunction renderSources"),
)


def _strip_js_comments(src: str) -> str:
    src = re.sub(r"/\*[\s\S]*?\*/", "", src)
    lines = [ln for ln in src.split("\n") if not re.match(r"^\s*//", ln)]
    src = "\n".join(lines)
    return re.sub(r" //[^\n]*", "", src)     # 行尾注释（不误伤 "://"：视图里没有 URL 字面量）


def _slice(src: str, start: str, end: str) -> str:
    i = src.index(start)          # 找不到就抛 ValueError，判据不会静默变空
    return src[i:src.index(end, i)]


@pytest.fixture(scope="module")
def engine_keys() -> set[str]:
    """引擎在一份真包上实际发出来的全部键（视图 / 分组 / 条目三层并集）。"""
    tmp = Path(tempfile.mkdtemp())
    specs = T._specs()
    I.fetch_pack("elevator", tmp, specs, seeds=["电梯困人"], keywords=["电梯"],
                 http=T._fake_http())
    view = I.today(tmp, "elevator", specs)
    out: set[str] = set(view)
    assert view["groups"], "夹具没产出任何分组 —— 判据会空转"
    for grp in view["groups"]:
        out |= set(grp)
    assert view["items"], "夹具没产出任何条目 —— 判据会空转"
    for it in view["items"]:
        out |= set(it)
    # 打分结果是**嵌在 score 里**的一层（不是条目的平铺键），单独核对它。
    scored = [it for it in view["items"] if isinstance(it.get("score"), dict)]
    assert scored, "没有任何一条带 score 的条目 —— 打分链路的判据会空转"
    assert "opportunity" in set(scored[0]["score"]), sorted(scored[0]["score"])
    return out


def test_topics_view_reads_only_keys_the_engine_sends(engine_keys):
    src = _strip_js_comments(TOPICS_JS.read_text(encoding="utf-8"))
    read_all: set[str] = set()
    for name, start, end in _SLICES:
        read = set(_READ.findall(_slice(src, start, end)))
        assert len(read) >= 8, f"{name}那一层只解析出 {sorted(read)} —— 锚点或写法变了"
        missing = sorted(read - engine_keys)
        assert not missing, (
            f"{name}在读引擎从不发的字段：{missing} —— "
            "结论类文字请走 flags（形如 [「warn」, 文案]），别在渲染层新造一个键")
        read_all |= read
    assert len(read_all) >= 18, f"三层合计只读到 {len(read_all)} 个字段，判据太弱"


def test_item_level_warn_stays_a_single_channel(engine_keys):
    """条目级 warn 这个"第二个结论通道"不许以任何形式回来（P3-7 的正面钉法）。

    引擎发的是 flags 里的 ["warn", …]；卡片要喊话就画那颗标记。
    如果哪天确实需要条目级警告，**先让 intel.py 发出来**（那时它会出现在
    `engine_keys` 里，这条判据自然放行），而不是在渲染层单方面读一个不存在的键。
    """
    src = _strip_js_comments(TOPICS_JS.read_text(encoding="utf-8"))
    assert not re.search(r"\bit\.warn\b", src), "卡片又读回 it.warn 了"
    assert "warn" not in engine_keys, "引擎开始发条目级 warn —— 那就要同步改图例与判据"


def test_history_cap_is_one_number_not_two():
    """`HISTORY_LIMIT` 必须真是唯一那一份（第 8 轮审查 P2：端点里还有第二个字面量）。

    界面那句「这里只有最近 N 条」引用的就是这个数。两处各写一遍，迟早一处改了
    另一处没改 —— 那句话于是变成假话，而且没有任何东西会红。
    """
    srv = (ROOT / "app" / "server.py").read_text(encoding="utf-8")
    code = "\n".join(ln for ln in srv.splitlines() if not ln.strip().startswith("#"))
    assert "[:100]" not in code, "server.py 又写了一个自己的历史上限字面量"
    assert "HISTORY_LIMIT" in code, "端点不再引用 store 那份上限？那句限定会失准"
    assert re.search(r"\.history\(limit\s*=", code) is None, \
        "调用点又显式传了一个 limit —— 上限只许 store 那一份说了算"


def test_cadence_tables_agree_across_languages():
    """`CADENCE_DAYS ∪ AUTO_FETCH_OFF` 必须等于 words.js 的 `CADENCE_LABEL` 键集。

    两边各一张表（Python 判"几天算过期"，JS 判"这一列印什么中文"），
    共享不了代码就逐键对账（第 8 轮审查 P2：`manual` 在中文表里有、天数表里没有）。
    少一个词 = 源表那一列把 `biweekly` 原样印给人看；多一个词 = 中文表在骗人。
    """
    from app.intel import AUTO_FETCH_OFF, CADENCE_DAYS

    words = (ROOT / "desktop" / "renderer" / "js" / "words.js").read_text(encoding="utf-8")
    block = words.split("CADENCE_LABEL = {")[1].split("};")[0]
    js_keys = set(re.findall(r"^\s*([a-z_]+)\s*:", block, re.M))
    py_keys = set(CADENCE_DAYS) | {k for k in AUTO_FETCH_OFF if k[0].isascii() and k[0].isalpha()}
    assert js_keys == py_keys, (
        f"节奏两表漂移：只在中文表 {sorted(js_keys - py_keys)}，"
        f"只在引擎侧 {sorted(py_keys - js_keys)}")

