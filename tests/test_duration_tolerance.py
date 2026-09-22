# -*- coding: utf-8 -*-
"""时长容差要能由行业包配置（P2-27 的后半）。

前半（`±max(10%, 3秒÷目标时长)` 的自适应口径）早就落了，但 `check_script` 的
`tolerance` 形参一直**没有任何调用点传**，函数自己的注释也写着"形参保留但当前
无人传" —— 也就是"60 秒档 ±10% 只有 ±27 字容错"这种物理装不下的包，
作者没有任何办法放宽，只能看着回炉白烧。

这里守三层：
  1. 取值合法（写错在**加载时**就摊成 pack_error，不带进生成）
  2. `check_script` 真的按传进来的值判
  3. 生成链路真的传了（mock 活体产物的 `check.tolerance_pct` 就是配置值）
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.checker import (Banwords, Quota, check_script,  # noqa: E402
                        tolerance_error)
from app.config import load_config                       # noqa: E402
from app.jobs import TERMINAL_STATES                     # noqa: E402
from app.knowledge import Pack, pack_info                # noqa: E402
from app.pipeline import Pipeline, wait_job              # noqa: E402
from app.schemas import GenerateRequest                  # noqa: E402


def _sections(n=70):
    return [{"type": "hook", "text": "开头一句钩子。"},
            {"type": "point", "text": "内" * n},
            {"type": "cta", "text": "关注我。"}]


# ── 1. 取值检查 ────────────────────────────────────────────
@pytest.mark.parametrize("bad", ["10", 0, -5, 0.1, 101, True, {"a": 1}, []])
def test_bad_tolerance_values_are_rejected(bad):
    assert tolerance_error(bad), f"{bad!r} 应当被拒绝"


@pytest.mark.parametrize("ok", [None, "", 0.5, 10, 20.0, 100])
def test_good_tolerance_values_are_accepted(ok):
    assert not tolerance_error(ok)


def test_broken_tolerance_shows_up_as_pack_error(tmp_path):
    """坏值必须让包在列表上就被标出来，而不是等第一次生成才发现。"""
    d = tmp_path / "packs" / "elevator"
    shutil.copytree(ROOT / "packs" / "elevator", d)
    data = yaml.safe_load((d / "pack.yaml").read_text(encoding="utf-8"))
    data["duration_tolerance_pct"] = "百分之十"
    (d / "pack.yaml").write_text(yaml.safe_dump(data, allow_unicode=True),
                                 encoding="utf-8")
    info = pack_info(d)
    assert "duration_tolerance_pct" in info.pack_error


def test_broken_value_falls_back_to_adaptive_when_read_directly():
    """`Pack.duration_tolerance()` 自己也不能把坏值交给校验器。

    直接构造实例（不走 `Pack(...)`）：加载路径上坏值早被 `pack_info` 判死、
    `Pack()` 会抛 `PackBrokenError` 让生成吃 409（这是对的行为），
    于是"坏值退回 None"这条防御在正常链路上永远走不到 —— 单独量一次才守得住。
    """
    pk = object.__new__(Pack)
    pk.data = {"duration_tolerance_pct": "百分之十"}
    assert Pack.duration_tolerance(pk) is None
    pk.data = {"duration_tolerance_pct": 0}
    assert Pack.duration_tolerance(pk) is None
    pk.data = {"duration_tolerance_pct": 18}
    assert Pack.duration_tolerance(pk) == 18.0


# ── 2. check_script 按传进来的值判 ──────────────────────────
def test_configured_tolerance_actually_gates():
    """容差要是唯一决定合格与否的那个数 —— 偏差量从报告里反推，不写死。"""
    sections = _sections(70)
    ban = Banwords({"hard": [], "soft": []})
    rep = check_script(sections, 60, 4.5, ban, "抖音", Quota({}))
    dev = abs(rep["deviation_pct"])
    assert rep["tolerance_pct"] == 10.0
    assert dev > rep["tolerance_pct"], "构造的稿子要落在默认 ±10% 之外"
    assert rep["passed"] is False

    loose = check_script(sections, 60, 4.5, ban, "抖音", Quota({}),
                         tolerance=dev + 2)
    assert loose["tolerance_pct"] == round(dev + 2, 1)
    assert loose["passed"] is True, "放宽到偏差之外就该合格"

    tight = check_script(sections, 60, 4.5, ban, "抖音", Quota({}),
                         tolerance=max(0.5, dev - 2))
    assert tight["passed"] is False, "收紧到偏差之内就该不合格"
    assert loose["chars_total"] == tight["chars_total"] == rep["chars_total"]


def test_tighter_tolerance_still_gates():
    sections = _sections(62)
    rep = check_script(sections, 60, 4.5, Banwords({"hard": [], "soft": []}),
                       "抖音", Quota({}), tolerance=1)
    assert rep["tolerance_pct"] == 1.0
    assert rep["passed"] is False


# ── 3. 生成链路真的传了 ─────────────────────────────────────
def test_generation_uses_the_pack_value(tmp_path):
    d = tmp_path / "packs" / "elevator"
    shutil.copytree(ROOT / "packs" / "elevator", d)
    data = yaml.safe_load((d / "pack.yaml").read_text(encoding="utf-8"))
    data["duration_tolerance_pct"] = 35
    (d / "pack.yaml").write_text(yaml.safe_dump(data, allow_unicode=True),
                                 encoding="utf-8")
    cfg = load_config(tmp_path)
    cfg.mock = True
    pl = Pipeline(tmp_path, cfg)
    jid = pl.start_generate(GenerateRequest(pack="elevator", topic="家用电梯怎么选",
                                           duration=60, platform="抖音"))
    snap = wait_job(pl, jid, timeout=90)
    assert snap["state"] in TERMINAL_STATES, "作业没结束"
    assert snap["state"] == "done", snap.get("error")
    assert snap["result"]["check"]["tolerance_pct"] == 35.0
    disk = next(tmp_path.glob("generated/*/*/result.json"))
    assert json.loads(disk.read_text(encoding="utf-8"))["check"]["tolerance_pct"] == 35.0


def test_default_pack_keeps_adaptive_tolerance():
    """电梯包不设这一项 —— 缺省口径不能被这次改动悄悄换掉。"""
    pk = Pack(ROOT, "elevator")
    assert pk.duration_tolerance() is None
    assert "duration_tolerance_pct" not in pk.data
    rep = check_script(_sections(40), 60, 4.5, Banwords({"hard": [], "soft": []}),
                       "抖音", Quota({}))
    assert rep["tolerance_pct"] == 10.0
