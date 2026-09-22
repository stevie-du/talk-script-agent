# -*- coding: utf-8 -*-
"""P1-21 ③：ad-law.md 词族表与 banwords.yaml 的对账测试。

广告法词族是「依据」，词表是「机器可读实现」。两边靠手工同步必漂
（2026-09-20 实测缺 30+ 词）。这条测试把漂移变成红灯：
ad-law.md 列出的每个禁用词族，banwords.yaml（hard ∪ soft）里必须有词
能拦住它。判定用「子串互含」—— 扫描是「长词优先、不重叠」匹配，
「全网最低」会被 hard 的「全网最低价」覆盖（反之亦然），所以
b ∈ w 或 w ∈ b 都算覆盖。
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
AD_LAW = ROOT / "packs" / "elevator" / "compliance" / "ad-law.md"
BANWORDS = ROOT / "packs" / "elevator" / "banwords.yaml"

# ad-law.md 词族行各自以这些前缀开头；行内用 顿号/空格/逗号 分隔。
#
# ⚠ 前缀必须逐字等于**正文行**的开头，不是小节的标题。第 22 轮复核实测：
#   这里原来写的是 "级、"，而 ad-law.md 那一族的正文行是
#   `国家级、世界级、极品、…` —— 前缀永远匹配不上，于是**这一整族 11 个词
#   从来没有被对账过**（测试常年绿着，却什么都没查）。判据是「这行到底查没查到」：
#   把前缀改对之后，从 banwords 里删掉 `国家级` 必须立刻变红。
_WORD_LINE_PREFIXES = (
    "最、", "第一、", "国家级、", "行业领先、", "100%、", "清仓倒闭、", "央视上榜、",
)
_SPLIT = re.compile(r"[\s、，,;；]+")


def _adlaw_words() -> set[str]:
    words: set[str] = set()
    for line in AD_LAW.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line.startswith(_WORD_LINE_PREFIXES):
            continue
        for w in _SPLIT.split(line):
            w = w.strip("`'\"，、。")
            if len(w) >= 2:                    # 单字（如裸"最"）会被 MIN_WORD_LEN 丢弃，不对账
                words.add(w)
    return words


def test_ad_law_word_families_are_covered_by_banwords():
    data = yaml.safe_load(BANWORDS.read_text(encoding="utf-8")) or {}
    bank = set(data.get("hard", [])) | set(data.get("soft", []))
    missing = sorted(
        w for w in _adlaw_words()
        if not any(b in w or w in b for b in bank))
    assert not missing, (
        "ad-law.md 词族在 banwords.yaml 里没有覆盖（子串互含判定）："
        + "、".join(missing)
        + " —— 补进 banwords.yaml 后本条测试自然变绿")


def test_every_declared_prefix_actually_matches_a_line():
    """盯「这条对账自己有没有在工作」。

    前缀写错时，上面那条测试会**静默空转**：第 22 轮实测 `"级、"` 对不上
    ad-law.md 的 `国家级、…`，于是那一整族 11 个词从来没被查过，而测试常年绿。
    「永远为真的断言等于没有断言」—— 所以这里单独钉一条：每个声明的前缀
    都必须在 ad-law.md 里真的命中过至少一行。

    它不查词表，只查对账的**靶子还在不在**：前缀写错、或那一族正文行被改写
    （标题换了、行首插了字）都会红。
    """
    lines = [ln.strip() for ln in AD_LAW.read_text(encoding="utf-8").splitlines()]
    dead = [p for p in _WORD_LINE_PREFIXES
            if not any(ln.startswith(p) for ln in lines)]
    assert not dead, (
        "这些前缀在 ad-law.md 里一行都没匹配上 —— 对账测试对它们等于没跑："
        + "、".join(dead)
        + "。要么前缀写错了（注意要比的是**正文行**开头，不是小节标题），"
          "要么那一族正文行被改写了。")
