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
# 2026-09-17：采样温度 (temperature) 从前端**高级配置 UI**移除（任务 4）——
# 但 NUMERIC_BOUNDS 里**仍保留** temperature 区间（与后端一致是 pytest 守卫的不变量），
# 仅 UI 不暴露。所以 JS_TO_INPUT 不再包含 temperature（HTML 里也没 st-temperature 元素了）。
# 后端 NUMERIC_BOUNDS 仍然有 temperature 区间 → test_temperature_is_no_longer_narrower
# / test_temperature_accepts_zero 仍在跑且过。
JS_TO_INPUT = {
    "retries": "st-retries",
    "timeout": "st-timeout",
    "max_tokens": "st-maxtokens",
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
