# -*- coding: utf-8 -*-
"""前后端数值区间必须一致 —— 前端**不许**比后端更严。

背景
----
`app/server.py` 与 `desktop/renderer/js/settings.js` 是两种语言，没法共享代码，
于是同一个区间被写了两遍。它们曾经不一致：

| | temperature 区间 |
|---|---|
| 前端 `settings.js` | 0 ~ **1.5** |
| 后端 `server.py` | 0.0 ~ **2.0** |

用户填 `1.8` → **前端**弹「采样温度需在 0 ~ 1.5 之间」并直接 `return`，
请求根本不发出去，后端那句更宽松的校验永远不会被触发。

**前端比后端更严是更坏的一种不一致**：后端更严时用户至少能从错误信息知道
「服务端不接受」；前端更严时用户看到的是「界面说不行」—— 绕不过去
（除非去改 config.yaml），也想不到是界面在凭想象设限。

所以这里不靠「记得改两处」，而是**读两个文件比对**。
断言在 `tests/` 里，`settings.js` 改名或挪走会立刻报红。

顺带守住 `index.html` 的 min/max：JS 启动时会用 `NUMERIC_BOUNDS` 覆盖它们，
但覆盖之前的初始值也不该是错的（用户可能抢在 JS 之前看到它，
更实际的是「两处不一致会让人以为改对了」）。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.server import NUMERIC_BOUNDS, RESETTABLE_FIELDS  # noqa: E402

JS = ROOT / "desktop" / "renderer" / "js" / "settings.js"
HTML = ROOT / "desktop" / "renderer" / "index.html"

# 只匹配 `export const NUMERIC_BOUNDS = { ... };` 这一块，
# 不匹配文件里其他地方出现的 [lo, hi]
_JS_BLOCK = re.compile(r"export const NUMERIC_BOUNDS\s*=\s*\{(.*?)\};", re.S)
_JS_ENTRY = re.compile(r"(\w+)\s*:\s*\[\s*(-?[\d.]+)\s*,\s*(-?[\d.]+)\s*\]")

# 输入框：id + 属性串（属性可能跨行，所以用 re.S）
_INPUT = re.compile(r'<input\s+id="(st-[a-z]+)"(.*?)>', re.S)
_ATTR = re.compile(r'\b(min|max)="(-?[\d.]+)"')

# JS 里的字段名 → HTML 输入框 id。
# 2026-09-17：采样温度 (temperature) 与输出预算 (max_tokens) 先后从**高级配置 UI**
# 移除（用户原话「移除高级配置中的采样温度」「高级设置去掉 token 限制吧」）——
# 但 NUMERIC_BOUNDS 里**仍保留**这两项区间（与后端一致是 pytest 守卫的不变量，
# 且后端仍在用：temperature 供重试时微调、max_tokens 是模型输出上限），仅 UI 不暴露。
# 所以 JS_TO_INPUT 不再包含它们（HTML 里也没有对应输入框了）。
# 后端 NUMERIC_BOUNDS 仍有这两项 → test_temperature_is_no_longer_narrower /
# test_temperature_accepts_zero 仍在跑且过。
JS_TO_INPUT = {
    "retries": "st-retries",
    "timeout": "st-timeout",
}


def _js_bounds() -> dict[str, tuple[float, float]]:
    src = JS.read_text(encoding="utf-8")
    m = _JS_BLOCK.search(src)
    assert m, f"没找到 {JS.name} 里的 `export const NUMERIC_BOUNDS = {{…}};`" \
              " —— 改名了？本测试要跟着改，别直接删"
    out = {k: (float(lo), float(hi)) for k, lo, hi in _JS_ENTRY.findall(m.group(1))}
    # 非空守卫：正则失配时 out 会是 {}，那样下面的比对会「通过」得毫无意义
    assert out, "NUMERIC_BOUNDS 解析结果为空 —— 断言会空转，先修正则"
    return out


def _html_bounds() -> dict[str, dict[str, float]]:
    src = HTML.read_text(encoding="utf-8")
    out: dict[str, dict[str, float]] = {}
    for id_, attrs in _INPUT.findall(src):
        got = {k: float(v) for k, v in _ATTR.findall(attrs)}
        if got:
            out[id_] = got
    assert out, "没解析到任何 st-* 数值输入框 —— 断言会空转，先修正则"
    return out


def _backend() -> dict[str, tuple[float, float]]:
    return {k: (float(a), float(b)) for k, (a, b) in NUMERIC_BOUNDS.items()}


# ── 1. 前后端区间一致 ────────────────────────────────────────

def test_js_bounds_match_backend():
    """核心断言：前端区间逐项等于后端。"""
    assert _js_bounds() == _backend()


def test_backend_covers_every_js_field():
    """反向也要查：JS 多写一个字段、后端没有，同样是「前端凭想象设限」。"""
    extra = set(_js_bounds()) - set(_backend())
    assert not extra, f"前端有、后端没有的字段：{sorted(extra)}"


def test_temperature_is_no_longer_narrower():
    """把这次的具体缺陷单独钉一遍（回归时最该先看这条）。

    修复前前端是 [0, 1.5]，比后端的 [0, 2.0] 窄。
    """
    js = _js_bounds()["temperature"]
    be = _backend()["temperature"]
    assert js == be, f"前端 {js} != 后端 {be}"
    assert js[1] >= 2.0, f"上限 {js[1]} 仍然窄于 2.0"
    assert js[0] <= 0.0, "下界必须含 0（0 是合法采样温度，见 P0-2）"


# ── 2. HTML 初始 min/max 也要对 ──────────────────────────────

@pytest.mark.parametrize("key,id_", sorted(JS_TO_INPUT.items()))
def test_html_min_max_matches_backend(key, id_):
    """HTML 里的 min/max 是 JS 覆盖前的初始值，也不能错。"""
    got = _html_bounds().get(id_)
    assert got, f"{id_} 没写 min/max（或正则没匹配上）"
    lo, hi = _backend()[key]
    assert got.get("min") == lo, f"{id_} min={got.get('min')} 应为 {lo}"
    assert got.get("max") == hi, f"{id_} max={got.get('max')} 应为 {hi}"


def test_no_orphan_numeric_input():
    """反向：每个 st-* 数值框都该在表里有归属，否则它不受区间约束。"""
    known = set(JS_TO_INPUT.values())
    # 2026-09-17：st-temperature 已从 HTML 移除（采样温度 UI 下线）。
    # 这条测试的语义变成"HTML 不再有表外残留" —— 直接断言 _html_bounds 的键是 known 的子集。
    assert set(_html_bounds()) <= known, \
        f"这些输入框有 min/max 却不在 JS_TO_INPUT 里：{sorted(set(_html_bounds()) - known)}"


# ── 3. 边界值本身自洽 ────────────────────────────────────────

def test_bounds_are_well_formed():
    for k, (lo, hi) in NUMERIC_BOUNDS.items():
        assert lo < hi, f"{k}: 区间为空"
        assert k in RESETTABLE_FIELDS, f"{k} 可保存却不能重置，用户改错了没法回默认"


def test_temperature_accepts_zero():
    """0.0 必须落在区间内 —— P0-2 修的就是「0 存不进去」。"""
    lo, hi = NUMERIC_BOUNDS["temperature"]
    assert lo <= 0.0 <= hi


# ── 4. 散文里抄的区间也要对（2026-09-27 修）──────────────────────
# 上面那几条守的是**能执行的**三处（JS 表 / HTML min-max / 后端表）。
# 但同一个区间还有两份**给人读的抄件**：`config.example.yaml` 与
# `app/config.py` 的 `CONFIG_TEMPLATE`（引擎首次运行写给用户的那份配置，
# 逐字列了四项的合法区间）。它们一改就漂，漂了没人知道 ——
# 用户照着过期区间写 config.yaml，越界值被夹回边界，而那份注释还在
# 告诉他"这个数是合法的"。
#
# 与 P0-6 是同一件事的两个面：那张表已经从 server.py 下沉到 config._num
# （可执行的闸只有一处），但**给人看的两份抄件**还各写一遍。
# 所以这里把它们逐项读出来与表比对 —— 改表的人会立刻在这里被逮住。
#
# 写法说明：两份文件的散文形状不同（一份用 `retries      0 ~ 10` 的对齐写法，
# 一份用 `retries 0~10 · timeout 5~1800 秒 …` 的串行写法），所以只取
# 「字段名 + 紧跟其后的那个区间」这一对，与表的键名对账。
_PROSE = {
    "config.example.yaml": ROOT / "config.example.yaml",
    "CONFIG_TEMPLATE": ROOT / "app" / "config.py",
}


def _fmt(v) -> str:
    """把区间端点渲染成散文里会写的那个写法（0.0 写 0、2.0 写 2）。

    留着是因为写这条时踩过一次：文档里 `0.0 ~ 2.0` 与 `0 ~ 2` 都合法，
    但两端一端带小数一端不带，读起来像两套数 —— 判据统一按 float 比。
    """
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


@pytest.mark.parametrize("label", sorted(_PROSE))
def test_prose_copies_of_the_ranges_match_the_table(label):
    path = _PROSE[label]
    text = path.read_text(encoding="utf-8")
    missing, wrong = [], []
    for field, (lo, hi) in NUMERIC_BOUNDS.items():
        if field not in text:
            missing.append(field)
            continue
        # 扫**所有**含该字段名的行，取第一个带区间的 —— 不能只看第一次出现。
        # ⚠ 实测踩过：`config.example.yaml` 里 `retries` 第一次出现是在
        #   「界面上能改的只有 retries / timeout 两项」那一句（那一行没有区间），
        #   真正抄着区间的是后面第 N 行的注释块。只看第一次出现 → m 为 None →
        #   `continue` → **整条比较空转**，而"改了表忘了改文档"照样全绿。
        #   这与 P1-28 那条 IME 守卫同形：判据量不到东西时读起来和"通过"一样。
        m = None
        for ln in text.splitlines():
            at = ln.find(field)
            if at < 0:
                continue
            # ⚠ 只在**字段名之后**找区间。CONFIG_TEMPLATE 那行是
            #   「合法区间：retries 0~10 · timeout 5~1800 秒 · max_tokens 256~200000 ·」
            #   —— 三个字段名同行，整行找第一个区间的话 timeout/max_tokens 都会
            #   量成 retries 的 0~10（实测第二版就这样：一行改动红了两条，理由是假的）。
            # ① 跨行找会撞上**日期**：`2026-09-17` 会被当成区间（实测第一版）。
            # ② `a-b` 是日期里的短横，不是区间 —— 所以只认波浪号。
            hit = re.search(r"(?<![\d-])(\d+(?:\.\d+)?)\s*[~～]\s*(\d+(?:\.\d+)?)",
                            ln[at + len(field):])
            if hit:
                m = hit
                break
        if not m:
            continue                       # 该字段在这份文件里没抄区间（合法）
        got = (float(m.group(1)), float(m.group(2)))
        want = (float(lo), float(hi))
        if got != want:
            wrong.append(f"{field}：文档写 {got}，表里是 {want}")
    assert not wrong, \
        f"{label} 里抄的区间与 NUMERIC_BOUNDS 对不上（改表的人会在这里被逮住）：\n" \
        + "\n".join(wrong)
    # 正向对照：这几份抄件都真的抄了 —— 上面那组在"一处都没抄"时会全绿。
    assert not missing, \
        f"{label} 里这些字段没提到，改区间时它不会被提醒：{missing}"
    assert "0 ~ 10" in text or "0~10" in text, \
        "夹具前提失真：这份文件里已经找不到 retries 的区间那一行了"
