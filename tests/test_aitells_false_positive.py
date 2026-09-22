# -*- coding: utf-8 -*-
"""A-3：误改率回归集 —— 给 L1 尺子配一把"别乱咬"的卡尺。

为什么要这条测试
----------------
`需求方案-去AI味与热点情报.md` §2.1 给 L1 定了一条**硬指标**：
「20–30 条『人味本来就正常』的稿子做固定回归，任何词表改动必须 **误报 < 10%** 才能合入」。
在它之前，`ai_tells.yaml` 加词、改阈值、改 severity **没有任何回归网** ——
`tests/test_ai_tells.py` 全是"该报的报了没"，一条都没有"不该报的报了没"。
这是 banwords soft 当年 169/175 轮假命中的同一种缺口，只是换了一层。

本文件落地时实测到的**第一手结果**（2026-09-23，`packs/elevator/ai_tells.yaml`）：

    改之前：NORMAL 误报率 **39.1%**（9/23）—— 远超 10% 的线
            元凶一：`parallel_triple` 6 篇。md 的依据写的是「排比三连**占满一段**」，
                    实现却是"段里出现一处 ≥3 项并列就报"，于是
                    「说清楚小区名、几号楼、哪部梯」这种**自然列举**被算成排比。
                    → 已按 md 补上"占满该段一半字"的判据（`AITells.PARALLEL_COVERAGE`）
            元凶二：`no_specific` 4 篇，全是**通篇没有数字**的稿子。这不是代码错，
                    是两份依据不一致 —— 见下面 BORDERLINE 那段
    改之后：NORMAL 误报率 **4.3%**（1/23）、strong 命中 **0**

三族的判据不一样，别拉平（各族的定位见 `tests/aitells_corpus.py` 顶部说明）：
  · NORMAL     → 误报率 < 10% **且** strong 命中为 0（strong 一次扣 12 分，代价最大）
  · BORDERLINE → **钉住现状**：必须仍被 `no_specific` 报出来（口径改了要一起改这里）
  · NEGATIVE   → **一条都不许命中**（这族是 A-2 `in-place` 改写档的合格线）
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))       # 同目录的语料模块

import aitells_corpus as CORPUS                                # noqa: E402
from app.ai_tells import AITells, STRONG                       # noqa: E402

PACK = ROOT / "packs" / "elevator"

#: 合入门槛（需求方案 §2.1）。**改这个数等于改尺子的验收口径**，不是调参。
FP_GATE = 0.10


@pytest.fixture(scope="module")
def tells() -> AITells:
    data = yaml.safe_load((PACK / "ai_tells.yaml").read_text(encoding="utf-8")) or {}
    return AITells(data)


def _hits(tells: AITells, sections: list[dict]) -> list[dict]:
    return tells.report(sections)["hits"]


def test_normal_scripts_false_positive_rate_under_gate(tells: AITells):
    """「人味本来就正常」的稿子，命中率必须 < 10%。"""
    hit, detail = 0, []
    for label, secs in CORPUS.NORMAL:
        ids = [h["id"] for h in _hits(tells, secs)]
        if ids:
            hit += 1
            detail.append(f"{label}: {ids}")
    n = len(CORPUS.NORMAL)
    rate = hit / n
    assert rate < FP_GATE, (
        f"正常稿误报率 {rate:.1%}（{hit}/{n}）超过门槛 {FP_GATE:.0%} —— "
        f"这次改动让尺子开始乱咬了。命中的是：\n  " + "\n  ".join(detail) +
        "\n（要么收窄那条 tell 的判据，要么说明为什么这些确实该报，"
        "并把该稿从 NORMAL 移出去 —— 别直接抬门槛）")


def test_no_strong_tell_fires_on_normal_scripts(tells: AITells):
    """strong tell 一次扣 12 分，**在正常稿上必须一次都不出现**。

    与上一条分开断言，是因为两件事的代价差一个量级：weak 命中是"提示"，
    strong 命中是"这篇被判像 AI 写的"。混在一条里，weak 的 4% 会把 strong 的
    1 篇盖过去。
    """
    bad = []
    for label, secs in CORPUS.NORMAL:
        for h in _hits(tells, secs):
            if h["severity"] == STRONG:
                bad.append(f"{label}: {h['id']}（{h['where']}，{h['detail']}）")
    assert not bad, "strong tell 在正常稿上命中（代价最大的一类误报）：\n  " + "\n  ".join(bad)


def test_borderline_numberless_scripts_are_flagged(tells: AITells):
    """**钉住现状**：通篇没有数字的稿子仍会被 `no_specific` 报出来。

    这**不是**在断言"它们该报"，而是在钉住一条**尚未定案**的口径：
      · 需求方案 §2.1 的算法是「无阿拉伯数字、无『数词+量词』」→ 该报；
      · `anti-ai-smell.md` 的依据是「没有真实人名/地名/数字/**场景**」→ 有场景，不该报。
    A-4 校准若把"具体"扩展到场景/专名，**改 `_no_specific` 的同时把这条断言反过来**
    （改成"不许报"），别只是把它删掉 —— 删掉就没人记得这里有过一个口径分歧。
    """
    unflagged = [label for label, secs in CORPUS.BORDERLINE
                 if "no_specific" not in [h["id"] for h in _hits(tells, secs)]]
    assert not unflagged, (
        f"这几篇无数字稿不再被 no_specific 报了：{unflagged} —— "
        "若这是有意的口径扩展（把'具体'从数字扩到场景），请把本断言反过来写，"
        "并在 ai_tells_corpus.py 顶部记下新的口径来源")


#: md §二「教科书口吻」点名的那几个词。它们**故意没有**收进词表
#: （`按规范` 在行业里是正常说法，见 `anti-ai-smell.md` §六 末节），
#: 但它们是"最可能被顺手加进词表"的一批。负样本必须在**加了它们之后**仍然一条不中。
#:
#: 为什么要用"放宽后的词表"来测，而不是只测出厂词表：出厂词表下负样本
#: **结构上就是免疫的**（政策/法条里没有一个 tell 词），那样的断言永远绿、
#: 什么也守不住。加上这批词之后，`bookish_connective` 在政策原文/法条/数字密集句
#: 里各有 1 处命中 —— 只要有人把 `WEAK_MIN` 降到 1 或把该 tell 提成 strong，
#: 这条立刻报红。**可证伪**才是断言的最低要求。
_WIDENING = ("应当", "按规范", "根据相关标准", "应当指出", "按照规定", "依据", "按照")


def _widened(tells: AITells) -> AITells:
    """出厂配置 + md 点名的教科书口吻词（加进 `bookish_connective`）。"""
    data = yaml.safe_load((PACK / "ai_tells.yaml").read_text(encoding="utf-8")) or {}
    lex = {k: list(v or []) for k, v in (data.get("lexicon") or {}).items()}
    lex.setdefault("bookish_connective", [])
    lex["bookish_connective"] = list(lex["bookish_connective"]) + list(_WIDENING)
    return AITells({**data, "lexicon": lex})


def test_negative_samples_are_never_flagged(tells: AITells):
    """政策原文 / 法条 / 数字密集句 / 引文：一条 tell 都不许命中。

    这族与 NORMAL 的区别是**它们连"提示"都不该有** —— 这类文本是**照抄**来的，
    任何"这里像 AI 写的"的提示都会诱导人把文号、数字、条文改掉。
    A-2 的 `in-place` 改写档要拿它做合格线（"一条都没动"）。

    两种词表都要过：出厂的，以及把 md §二点名的教科书口吻词加进去之后的
    （后者才是这条断言的可证伪点，见 `_WIDENING` 的注释）。
    """
    bad = []
    for name, t in (("出厂词表", tells), ("放宽词表（+教科书口吻词）", _widened(tells))):
        for label, must_keep, secs in CORPUS.NEGATIVE:
            ids = [h["id"] for h in _hits(t, secs)]
            if ids:
                bad.append(f"[{name}] {label}: {ids}（必须原样保留的片段：{must_keep}）")
    assert not bad, "负样本被报了 AI 味（这类文本一个字都不该被建议改）：\n  " + "\n  ".join(bad)


def test_corpus_is_big_enough_for_the_gate_to_mean_something():
    """语料太少时 10% 这条线是空的：23 篇里 2 篇就是 8.7%、3 篇就破线。

    需求方案写的是 20–30 条，这里守住下界 —— 顺手删几篇"碍事"的样本
    就会让门槛失去意义，而这正是最容易被顺手做掉的事。
    """
    assert 20 <= len(CORPUS.NORMAL) <= 30, (
        f"NORMAL 语料 {len(CORPUS.NORMAL)} 条，应在 20–30 之间"
        "（少于 20 条时 10% 的门槛只剩 1–2 篇的余量，统计上说明不了任何事）")
    assert len(CORPUS.NEGATIVE) >= 4, "负样本至少要有政策原文/法条/数字密集/引文四类"
