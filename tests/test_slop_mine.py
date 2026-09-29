# -*- coding: utf-8 -*-
"""`_verify/slop-mine.py`（A-9 挖词管线）的行为守护。

一个"跑起来打印一堆词"的工具，如果判据写反了，它照样会产出一份**看起来很合理**的
候选表 —— 而人会照贴。所以这里守的是四件最容易假绿的事：

1. **它量的是比值，不是词频**：两侧同频的那句不能进候选；A==B 必须零候选；
2. **没有分母就退出**，而不是打印"零超额"（那是假的干净）；
3. **同一份稿子只算一份**（`dedupe_docs`）：本机 `generated/` 里 79/102 篇是同一份
   夹具稿，按"次"计数的话它每一句都会变成"绝对超额"，候选表会被一份稿子占满；
4. **极大串 + 互不为子串**：滑动窗口的一批串（"就打电梯里贴的那"/"打电梯里贴的那个"）
   互不为子串、其实同属一句话 —— 不收敛的话候选表读起来就是噪声，
   而且贴进 yaml 会撞 `_scan_words` 的"互不为子串"不变量。
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_SPEC = importlib.util.spec_from_file_location("slop_mine", ROOT / "_verify" / "slop-mine.py")
slop = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(slop)             # type: ignore[union-attr]

A_SIDE = ["电梯维保要留痕。我们要形成赋能闭环。"] * 8
B_SIDE = ["电梯维保要留痕这件事大家都懂。"] * 8


def test_surfaces_only_phrases_over_used_in_the_model_side():
    """A 独有的串进候选，两侧同频的那句不进（"超额"就是这个意思）。"""
    rows = slop.mine(A_SIDE, B_SIDE, ratio=3.0, min_count=1)
    assert any("赋能闭环" in r["phrase"] for r in rows), rows
    assert not any("电梯维保" in r["phrase"] for r in rows), \
        f"两侧同频的那句被当成超额了：{[r['phrase'] for r in rows]}"
    for r in rows:
        assert r["ratio"] is None or r["ratio"] >= 3.0, r
    ps = [r["phrase"] for r in rows]
    assert all(x not in y for x in ps for y in ps if x != y), ps


def test_self_comparison_yields_nothing_not_a_clean_bill_of_health():
    """⚠ 正对照：A 与 B 是同一批稿子时必须**零候选**。

    只断言"上面那条挖得到东西"守不住判据写反的情形 —— 自己跟自己比也能挖出一大堆
    "超额"，那份报告看着最像工作成果，其实什么都没测。
    """
    same = ["轿厢停在两层之间，门一开外面是井道，先按警铃再打救援电话。"] * 6
    assert slop.mine(same, same, ratio=1.5, min_count=1) == []


def test_sliding_window_candidates_are_collapsed_to_maximal_ones():
    """互不为子串但同属一句话的滑动窗口串，只留极大的那一条。

    "梯里贴的那个" 与 "拨打电梯里贴的那个" 互不为子串（dedupe 收不掉），
    但前者每一次出现都被后者覆盖 → 极大串判据把它摘掉。没有这条，
    一句 15 字的话能产出十几条"候选"，候选表读起来就是噪声。
    """
    a = ["救援时要拨打电梯里贴的那个救援电话。"] * 5
    rows = slop.mine(a, ["平时记住出口位置。"], ratio=3.0, min_count=1)
    ps = [r["phrase"] for r in rows]
    assert any("电梯里贴的那个" in p for p in ps), ps
    assert "梯里贴的那个" not in ps, f"窗口碎片进了候选表：{ps}"
    # 「救援」出现两次，但它是某条 8 字候选的子串 ⇒ 被"长词优先"收掉（yaml 的不变量）
    assert "救援" not in ps and any("救援" in p for p in ps), ps
    # 去重后 A 只有一份稿子：默认门槛（≥5 次）下它一条候选都不该出 ——
    # "只在一份稿子里出现过一次"不是模型口癖，是那份稿子的内容。
    assert slop.mine(a, ["平时记住出口位置。"], ratio=3.0, min_count=5) == []


def test_identical_drafts_count_as_one_document_not_seventy_nine():
    """同一份稿子重复 79 次只算一份（`generated/` 的真实形状就是这样）。

    不去重时那份夹具的**每一句**都会以 79 次的"证据量"变成绝对超额，
    默认 `--min-count 5` 下候选表会被一份稿子占满；去重后它的每句只值 1 次。
    """
    one = "困在电梯里，第一件事不是打电话。"
    dup = [one] * 79 + ["轿厢导轨要定期清洗，这件事真人很少说。"]
    b = ["日常巡检看记录，导轨清洗按手册。"]
    assert len(slop.dedupe_docs(dup)) == 2
    # 默认门槛（≥5 次）下：重复 79 次的那句不再算证据 → 不进候选
    strict = slop.mine(dup, b, ratio=3.0, min_count=5)
    assert not any("第一件事不是打电话" in r["phrase"] for r in strict), \
        [r["phrase"] for r in strict]
    # 放到 1 次时它回来了，但计数是 **1**（一份稿子），不是 79
    loose = {r["phrase"]: r["a_count"] for r in slop.mine(dup, b, ratio=3.0, min_count=1)}
    assert loose.get("困在电梯里") == 1, loose
    assert max(loose.values()) < 20, f"计数还在按次堆、没按篇去重：{loose}"


def test_missing_human_corpus_exits_instead_of_printing_zero_overuse(tmp_path, capsys):
    """没给对照稿 → 退出码 2 且话说清楚；不能走到"候选 0 条"那一步。

    A 侧用临时目录（不碰 `generated/`）：那里的 mock 分流与篇数是环境状态，
    拿它当前提会让这个用例变成"取决于本机跑过几次生成"的不定测试。
    """
    a_dir = tmp_path / "gen" / "run"
    a_dir.mkdir(parents=True)
    (a_dir / "result.json").write_text(json.dumps({"sections": [
        {"type": "point", "text": "轿厢停在两层之间，门一开外面是井道。" * 3}]},
        ensure_ascii=False), encoding="utf-8")
    assert slop.main(["--a", str(a_dir.parent)]) == 2
    out = capsys.readouterr().out
    assert "必须有分母" in out and "零超额" in out, out
    empty = tmp_path / "b"
    empty.mkdir()
    assert slop.main(["--a", str(a_dir.parent), "--b", str(empty)]) == 2
    assert "B 侧一份对照稿都没有" in capsys.readouterr().out


def test_nested_candidates_are_collapsed_to_the_longest():
    """候选表可以直接往 yaml 上贴 ⇒ 互不为子串是使用前提（`_scan_words` 的不变量）。"""
    rows = [{"phrase": "赋能", "n": 2, "a_count": 9, "a_per_1k": 9.0, "b_count": 0,
             "b_per_1k": 0.0, "ratio": None},
            {"phrase": "赋能闭环", "n": 4, "a_count": 6, "a_per_1k": 6.0, "b_count": 0,
             "b_per_1k": 0.0, "ratio": None},
            {"phrase": "能闭", "n": 2, "a_count": 6, "a_per_1k": 6.0, "b_count": 0,
             "b_per_1k": 0.0, "ratio": None}]
    assert [r["phrase"] for r in slop.dedupe(rows)] == ["赋能闭环"]


def test_mock_artifacts_are_excluded_by_default(tmp_path):
    """带 `mock` 标记的夹具产物默认不进 A 侧（那是测试数据，会把分布带偏）。"""
    d = tmp_path / "run"
    d.mkdir()
    f = d / "result.json"
    f.write_text(json.dumps({"mock": True, "sections": [
        {"type": "point", "text": "夹具正文，第一，看井道。"}]}, ensure_ascii=False),
        encoding="utf-8")
    assert slop.artifact_text(f) == ""
    assert slop.artifact_text(f, allow_mock=True), "显式要的时候必须给"
    # 老产物没有 `mock` 键（那字段是后加的）→ 只能当"非夹具"收下，去重那一层兜住
    old = d / "old.json"
    old.write_text(json.dumps({"sections": [{"type": "point", "text": "真稿正文。"}]},
                   ensure_ascii=False), encoding="utf-8")
    assert slop.artifact_text(old) == "真稿正文。"


def test_candidates_never_contain_punctuation_or_digits():
    """带标点/数字的 gram 不进候选：贴进 yaml 就是永远匹配不到的死条目。"""
    c, _total, _ext = slop.grams(["第一，看井道尺寸；第二，看维保记录 15 日。" * 10])
    assert c, "gram 一个都没数到 —— 这条断言就成了空转"
    assert all(_looks_clean(k) for k in c), sorted(c)[:10]


def _looks_clean(gram: str) -> bool:
    return all(slop._CLEAN.match(ch) for ch in gram)
