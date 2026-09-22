# -*- coding: utf-8 -*-
"""A-2：回炉改写范围三档（`in-place` / `bounded` / `structural`）。

对标 MrGeDiao/shuorenhua 的三档改写范围（`需求方案-去AI味与热点情报.md` §2.9 A-2）。
这条需求的关键一句是「**语义已经在代码里**」—— `pipeline._violation_feedback` 的尾巴
一直写着「不要另起一炉重写、不要改动了未点名的段落」，那正是 `bounded` 档。
所以本文件守的不是"新功能"，而是**三件事**：

  1. 那句藏在提示词尾巴里的话，真的升格成了**有名字、可配置、可断言**的参数；
  2. 三档给模型的指令**确实不同**（不是三张贴纸，`in-place` 必须真的说"不许删句"）；
  3. 档位**真的流到了模型手里**（mock 活体产物里能看到它），而不是只存在于
     `_normalize` 的返回值里 —— "参数算了但没人用"是本项目反复出现的失效形态。

为什么"写错档位名"要判成坏包而不是静默退回默认：包作者写了 `inplace`（少个连字符）
以为自己配的是"只换词不删句"，实际拿到的是 `bounded`，回炉照样敢删句。
配置写错改变了行为却看不出来 —— 与 `duration_tolerance_pct` 写错同一类，
所以走同一条路（加载期判死 → `pack_error` → 包在列表上就被标出来）。
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import load_config                                    # noqa: E402
from app.jobs import TERMINAL_STATES                                  # noqa: E402
from app.knowledge import (DEFAULT_REWRITE_SCOPE, REWRITE_SCOPES,     # noqa: E402
                           Pack, pack_info, rewrite_scope_error)
from app.pipeline import (PERSISTED_PARAMS, SCOPE_INSTRUCTION,        # noqa: E402
                          Pipeline, wait_job)
from app.schemas import GenerateRequest                               # noqa: E402


def _pack_copy(tmp_path: Path, **over) -> Path:
    """把电梯包复制一份出来改 —— 不动仓库里那份。"""
    d = tmp_path / "packs" / "elevator"
    shutil.copytree(ROOT / "packs" / "elevator", d)
    data = yaml.safe_load((d / "pack.yaml").read_text(encoding="utf-8"))
    data.update(over)
    (d / "pack.yaml").write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return d


# ── 1. 取值检查 ────────────────────────────────────────────
@pytest.mark.parametrize("bad", ["inplace", "in_place", "InPlace", "BOUNDED",
                                 "bounded ", "rewrite", 1, True, {"a": 1}, ["bounded"]])
def test_bad_scope_values_are_rejected(bad):
    """枚举值写错必须报错 —— 静默退回默认档等于把作者配的意图换掉。"""
    assert rewrite_scope_error(bad), f"{bad!r} 应当被拒绝"


@pytest.mark.parametrize("ok", [None, "", *REWRITE_SCOPES])
def test_good_scope_values_are_accepted(ok):
    assert not rewrite_scope_error(ok)


def test_broken_scope_shows_up_as_pack_error(tmp_path):
    """坏档位要在**包列表**上就被标出来，而不是等第一次回炉才发现。"""
    d = _pack_copy(tmp_path, rewrite_scope="inplace")
    assert "rewrite_scope" in pack_info(d).pack_error


def test_broken_scope_falls_back_when_read_directly():
    """`Pack.rewrite_scope()` 自己也不能把坏值交出去（同 duration_tolerance 的防御）。

    正常链路上坏值早被 `pack_info` 判死、`Pack()` 会抛 `PackBrokenError`，
    所以这条防御单独量一次才守得住。
    """
    pk = object.__new__(Pack)
    pk.data = {"rewrite_scope": "inplace"}
    assert Pack.rewrite_scope(pk) is None
    pk.data = {"rewrite_scope": 7}
    assert Pack.rewrite_scope(pk) is None
    pk.data = {"rewrite_scope": "in-place"}
    assert Pack.rewrite_scope(pk) == "in-place"
    pk.data = {}
    assert Pack.rewrite_scope(pk) is None


# ── 2. 三档的指令确实不同 ───────────────────────────────────
def test_every_scope_has_an_instruction():
    """`REWRITE_SCOPES` 每档都要有文案 —— 漏一档会在回炉那一刻 KeyError。

    这条不是形式主义：`SCOPE_INSTRUCTION[scope]` 是**下标**取值，
    新增一个档位却忘了写文案，作业会停在回炉那一步失败（而且是在模型已经
    付过费之后），用户看到的是一句与包配置无关的异常。
    """
    assert set(SCOPE_INSTRUCTION) == set(REWRITE_SCOPES)


def test_three_scopes_give_three_different_instructions():
    prev = [{"type": "point", "text": "原始正文一句。"}]
    report = {"hard_hits": [], "soft_hits": [], "deviation_pct": 0.0,
              "tolerance_pct": 10.0, "duration_target": 60,
              "estimated_seconds": 60.0, "chars_total": 100,
              "target_total": 100, "segments": [], "ai_tells": None}
    texts = {s: Pipeline._violation_feedback(report, prev, s) for s in REWRITE_SCOPES}
    assert len(set(texts.values())) == len(REWRITE_SCOPES), "三档给模型的指令必须不同"
    for scope, text in texts.items():
        assert "原始正文一句。" in text, f"{scope} 档必须附上上一版全文（否则就是重掷骰子）"
        assert "{{待补}}" in text, f"{scope} 档都要明说占位不许动"
    # 三档各自的**特征句**：改实现把某档换成通用文案，这三条会分别报红。
    assert "不许删句" in texts["in-place"], "in-place 的核心约束是「不删句、只换词」"
    assert "不要另起一炉重写" in texts["bounded"], "bounded 是原来的口径，不许丢"
    assert "可以重排" in texts["structural"], "structural 与 bounded 的区别就是允许重排"


def test_default_scope_is_bounded():
    assert DEFAULT_REWRITE_SCOPE == "bounded"
    assert Pipeline._violation_feedback(
        {"hard_hits": [], "soft_hits": [], "deviation_pct": 0.0, "tolerance_pct": 10.0,
         "duration_target": 60, "estimated_seconds": 60.0, "chars_total": 100,
         "target_total": 100, "segments": [], "ai_tells": None},
        [{"type": "point", "text": "x"}]) == Pipeline._violation_feedback(
        {"hard_hits": [], "soft_hits": [], "deviation_pct": 0.0, "tolerance_pct": 10.0,
         "duration_target": 60, "estimated_seconds": 60.0, "chars_total": 100,
         "target_total": 100, "segments": [], "ai_tells": None},
        [{"type": "point", "text": "x"}], DEFAULT_REWRITE_SCOPE), \
        "不传 scope 时必须走默认档"


# ── 3. 归一：请求 > 包 > 引擎默认 ───────────────────────────
def test_normalize_precedence(tmp_path):
    """优先级：请求 > pack.yaml > 引擎默认。三层都要能单独验证。"""
    cfg = load_config(tmp_path)
    cfg.mock = True

    # 包配了 in-place，请求没给 → 用包的
    d = _pack_copy(tmp_path, rewrite_scope="in-place")
    pl = Pipeline(tmp_path, load_config(tmp_path))
    got = pl._normalize(Pack(tmp_path, "elevator"), {"topic": "t", "duration": 60})
    assert got["rewrite_scope"] == "in-place"
    assert d.exists()

    # 请求给了 structural → 压过包的 in-place
    got = pl._normalize(Pack(tmp_path, "elevator"),
                        {"topic": "t", "duration": 60, "rewrite_scope": "structural"})
    assert got["rewrite_scope"] == "structural"

    # 请求给了非法值 → 抛错，**不静默回落**
    with pytest.raises(ValueError, match="rewrite_scope"):
        pl._normalize(Pack(tmp_path, "elevator"),
                      {"topic": "t", "duration": 60, "rewrite_scope": "inplace"})


def test_normalize_defaults_to_bounded_when_pack_says_nothing(tmp_path):
    """仓库里那份电梯包**显式配了** bounded —— 这一条量的是"包没配"时的兜底。"""
    d = tmp_path / "packs" / "elevator"
    shutil.copytree(ROOT / "packs" / "elevator", d)
    data = yaml.safe_load((d / "pack.yaml").read_text(encoding="utf-8"))
    data.pop("rewrite_scope", None)
    (d / "pack.yaml").write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    pl = Pipeline(tmp_path, load_config(tmp_path))
    got = pl._normalize(Pack(tmp_path, "elevator"), {"topic": "t", "duration": 60})
    assert got["rewrite_scope"] == DEFAULT_REWRITE_SCOPE


def test_scope_is_persisted():
    """`rewrite_scope` 必须落盘：事后解释"这条为什么被重写了"只能靠它。

    `_finalize` 按 `PERSISTED_PARAMS` 白名单挑参数写进 `result.json.params`，
    不在名单里 = 算出来了但落盘那一刻无声丢掉（`quota_degraded` 当年就是这样）。
    """
    assert "rewrite_scope" in PERSISTED_PARAMS


# ── 4. 端到端：档位真的流到了产物里 ─────────────────────────
def test_generation_persists_the_requested_scope(tmp_path):
    _pack_copy(tmp_path)
    cfg = load_config(tmp_path)
    cfg.mock = True
    pl = Pipeline(tmp_path, cfg)
    jid = pl.start_generate(GenerateRequest(pack="elevator", topic="家用电梯怎么选",
                                            duration=60, platform="抖音",
                                            rewrite_scope="in-place"))
    snap = wait_job(pl, jid, timeout=90)
    assert snap["state"] in TERMINAL_STATES, "作业没结束"
    assert snap["state"] == "done", snap.get("error")
    assert snap["result"]["params"]["rewrite_scope"] == "in-place"
    disk = next(tmp_path.glob("generated/*/*/result.json"))
    assert json.loads(disk.read_text(encoding="utf-8"))["params"]["rewrite_scope"] == "in-place"
