# -*- coding: utf-8 -*-
"""`_verify/ai-smell-distribution.py`（A4 分数分布 + B-2 L2 六信号分布）的行为守护。

no_emotion / idiom_density 升不升 tell 的拍板要照这张分布表做 —— 一个"跑起来
打印一张表"的工具，判据写反了照样产出一份**看起来很合理**的表。这里守四件最
容易假绿的事：

1. **新产物直读落盘**：带 `check.ai_tells.shape` 的产物，分数与形态取落盘值
   （夹具里放一个引擎算不出来的 mattr，直读路径若被重算悄悄替换，当场穿帮）；
2. **旧产物走引擎重跑**：`ai_tells` 落 null 的旧形态、以及"只有 score 没有
   shape"的中间形态，都必须触发 rescan —— 分数与形态出自同一次现算，不许
   "落盘的分数 + 现算的形态"两本账拼一行；
3. **p50/p90 的算术**：最近秩 int(n·q)，与分数分布同一把尺；没测(None) 单独
   计数，不能冒充 0（与 shape 落 null、界面画「—」是同一条规矩）；
4. **直方分桶不炸**：全没测 / 全 0 / 超大值都不炸、不画错桶。

所有用例都把发现根指到 tmp_path（不碰 `generated/` 与 realchain 镜像）：
那里的篇数与内容是环境状态，拿它当前提会让用例变成"取决于本机跑过几次生成"
的不定测试（与 test_slop_mine.py 同一条纪律）。
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_SPEC = importlib.util.spec_from_file_location(
    "ai_smell_distribution", ROOT / "_verify" / "ai-smell-distribution.py")
dist = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(dist)              # type: ignore[union-attr]

from app.ai_tells import AITells, score_of, shape_metrics   # noqa: E402
from app.knowledge import Pack                              # noqa: E402

#: 不含 no_specific 会咬的空话（带数字），也不刻意喂词表命中 —— 期望值一律在
#: 用例里用同一引擎现算，不抄死数（包的 yaml 变了用例不该跟着碎）。
SECTIONS = [
    {"type": "point", "text": "轿厢停在两层之间，门一开外面是井道，先按警铃再打 96333。"},
    {"type": "point", "text": "维保记录要留痕呢，这件事真人很少说得这么细。"},
]


def _write(root: Path, name: str, body: dict) -> Path:
    """按 generated/<日期>/<作业>/result.json 的真实布局落一份夹具。"""
    d = root / "20261012" / name
    d.mkdir(parents=True)
    f = d / "result.json"
    f.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
    return f


def _new_body() -> dict:
    """新形态：`check.ai_tells` 带 shape。mattr=0.123 是引擎对这个 sections
    算不出来的值 —— 直读若被重算替换，它当场变值（这比断言"有 shape"硬）。"""
    return {"mock": False, "pack": "elevator", "sections": SECTIONS,
            "check": {"ai_tells": {"score": 77,
                                   "hits": [{"id": "no_specific", "severity": "strong",
                                             "count": 1, "where": "全篇",
                                             "detail": "", "words": []}],
                                   "shape": {"mattr": 0.123, "trigram_repeat": 0.005,
                                             "clause_len": {"n": 2, "p50": 5.0,
                                                            "p90": 9.0, "gt30": 0.0},
                                             "particles_per_100": 0.37,
                                             "funcword_entropy": 3.39,
                                             "idioms_per_1000": 0.0, "chars": 40}}}}


def _elevator_tells() -> AITells:
    """真包的检测器（只做文本扫描，不发任何模型调用）。"""
    return AITells(Pack(ROOT, "elevator").ai_tells_data())


# ── 1. 直读路径 ──────────────────────────────────────────────

def test_new_artifact_reads_storage_verbatim():
    """带 shape 的产物：分数/命中/形态逐项取落盘值，一行都不重算。"""
    d = _new_body()
    row = dist.stored_row(d, "20261012/r1")
    assert row is not None and row["src"] == "直读"
    assert row["score"] == 77
    assert row["shape"]["mattr"] == 0.123, "落盘 shape 被重算覆盖了"
    assert row["hits"] == [("no_specific", "strong", 1)]


def test_stored_shape_survives_without_a_declared_pack():
    """直读不再按今天的 yaml 复查包声明：产物自带 ai_tells 就是生成期跑过的
    证据。包后来改了 yaml / 包没了，那份历史结论仍然进分布。"""
    d = _new_body()
    d["pack"] = "no-such-pack"
    row = dist.stored_row(d, "x")
    assert row is not None and row["shape"]["mattr"] == 0.123


# ── 2. 重跑路径的触发 ────────────────────────────────────────

def test_old_artifact_goes_through_engine_rescan(tmp_path, capsys):
    """`ai_tells` 落 null 的旧形态 → 不走直读，分数/形态与本地重算一致。"""
    _write(tmp_path, "r1", {"mock": False, "pack": "elevator",
                            "sections": SECTIONS})
    d = json.loads((tmp_path / "20261012" / "r1" / "result.json")
                   .read_text(encoding="utf-8"))
    assert dist.stored_row(d, "x") is None, "旧形态被当成了直读"

    dist.main(roots=[tmp_path])
    out = capsys.readouterr().out
    tells = _elevator_tells()
    exp_score = score_of(tells.scan(SECTIONS))
    exp_shape = shape_metrics(SECTIONS, tells.idioms)
    assert "全部按当前引擎重跑" in out, out
    assert f"min {exp_score}" in out, out
    assert f"{exp_shape['mattr']:g}" in out, "形态不是现算的"


def test_score_without_shape_also_goes_to_rescan(tmp_path, capsys):
    """中间形态（有 score 没 shape，如 20260924 那篇）→ 重跑，不混读两本账。"""
    _write(tmp_path, "r1", {"mock": False, "pack": "elevator",
                            "sections": SECTIONS,
                            "check": {"ai_tells": {"score": 55, "hits": []}}})
    d = json.loads((tmp_path / "20261012" / "r1" / "result.json")
                   .read_text(encoding="utf-8"))
    assert dist.stored_row(d, "x") is None, "没 shape 的落盘块被当成直读了"

    dist.main(roots=[tmp_path])
    out = capsys.readouterr().out
    exp = score_of(_elevator_tells().scan(SECTIONS))
    assert f"min {exp}" in out, out
    assert "min 55" not in out, "落盘 score 被直读进了分布（两本账混一行）"


def test_old_artifact_without_sections_is_skipped_quietly(tmp_path, capsys):
    """旧产物连 sections 都没有 → 跳过不炸；一篇不剩时话说清楚。"""
    _write(tmp_path, "r1", {"mock": False, "pack": "elevator", "sections": []})
    dist.main(roots=[tmp_path])
    assert "没有可跑的产物" in capsys.readouterr().out


def test_mixed_corpus_dispatches_both_paths(tmp_path, capsys):
    """新旧混仓：直读的篇数、重跑的篇数各报各的；直方与汇总照常出。"""
    _write(tmp_path, "new", _new_body())
    _write(tmp_path, "old", {"mock": False, "pack": "elevator",
                             "sections": SECTIONS})
    dist.main(roots=[tmp_path])
    out = capsys.readouterr().out
    assert "直读落盘 1 篇 · 引擎重跑 1 篇" in out, out
    assert "0.123" in out, "直读那篇的 mattr 标记值没进汇总"
    assert "成语每千字直方" in out, out


# ── 3. p50/p90 的算术与「没测」───────────────────────────────

def test_percentile_is_nearest_rank_same_ruler_as_scores():
    """最近秩 int(n·q) —— 分数分布沿用多年的那把尺，六信号不许另立口径。"""
    assert dist.pctl([1, 2, 3, 4], .5) == 3          # int(4*.5)=2 → 上中位
    assert dist.pctl([1, 2, 3, 4], .9) == 4          # int(3.6)=3
    assert dist.pctl([5, 1], .5) == 5                # 先排序再取秩
    assert dist.pctl([7], .5) == 7 and dist.pctl([7], .9) == 7


def test_none_is_counted_not_zero_filled():
    """没测（None）单独计数：n=2 的 min/p50/p90 不得被 None 拖成假数或假 0。"""
    a = dist.agg_signal([None, 1, 3])
    assert (a["n"], a["none"]) == (2, 1)
    assert (a["min"], a["p50"], a["p90"]) == (1, 3, 3)
    empty = dist.agg_signal([None, None])
    assert empty["n"] == 0 and empty["none"] == 2
    assert empty["min"] is None and empty["p50"] is None and empty["p90"] is None


def test_shape_value_walks_nested_keys_and_misses_safely():
    """clause_len 的分量键 (("clause_len","p50")) 取得到；缺键/坏形状落 None。"""
    shape = {"clause_len": {"p50": 6.0}, "mattr": 0.85}
    assert dist.shape_value(shape, ("clause_len", "p50")) == 6.0
    assert dist.shape_value(shape, "mattr") == 0.85
    assert dist.shape_value(shape, "idioms_per_1000") is None
    assert dist.shape_value(None, "mattr") is None
    assert dist.shape_value({"clause_len": None}, ("clause_len", "p50")) is None


# ── 4. 直方分桶不炸 ──────────────────────────────────────────

def test_idioms_hist_buckets_edge_shapes():
    """全没测 → 空表；全 0 → 单桶；常规与超大值 → 桶号对、总数不丢。"""
    assert dist.idioms_hist([]) == []
    assert dist.idioms_hist([None, None]) == []
    assert dist.idioms_hist([0]) == [(0, 1)]
    assert dist.idioms_hist([0, 0, 0.5, 7.69]) == [(6, 1), (4, 0), (2, 0), (0, 3)]
    big = dist.idioms_hist([1234.5, 0])
    assert big[0] == (1234, 1) and big[-1] == (0, 1)
    assert sum(c for _, c in big) == 2, "分桶把篇数弄丢了"
