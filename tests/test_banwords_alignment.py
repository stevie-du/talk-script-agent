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

# ad-law.md 六个词族行各自以这些前缀开头；行内用 顿号/空格/逗号 分隔。
_WORD_LINE_PREFIXES = (
    "最、", "第一、", "级、", "行业领先、", "100%、", "清仓倒闭、", "央视上榜、",
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
