# -*- coding: utf-8 -*-
"""人味 tell 检测必须**接在线上**（P-新：本模块曾写完就没人调用）。

`app/ai_tells.py` 有 264 行、九条 tell、一个包级词表，但全仓库没有任何一处
import 它 —— 这就是本项目一直在治的「写了但没接上」形态（`files.*` 孤儿键、
`scenes` 空槽、raw/ 的假承诺都是同一类）。本文件守四件事：

  1. 引擎 → 包词表 → 校验报告 的链路是通的（真的能从 check_script 拿到分数）
  2. 词表写坏/写空不许伪装成「人味 100 分」
  3. 文风命中**不进 passed**（不合规才拦发布）
  4. `_count` 与 `checker.count_chars` 同一口径（模块自己的注释承诺过这一点）
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.ai_tells import AITells, score_of            # noqa: E402
from app.checker import Banwords, Quota, check_script, count_chars  # noqa: E402
from app.config import load_config                    # noqa: E402
from app.knowledge import Pack                        # noqa: E402
from app.pipeline import Pipeline, _load_tells, wait_job  # noqa: E402
from app.schemas import GenerateRequest               # noqa: E402

AI_YAML = ROOT / "packs" / "elevator" / "ai_tells.yaml"


def _sections(**kw):
    return [{"type": "hook", "text": kw.get("hook", "电梯维保这块，众所周知， safety 性很重要。")},
            {"type": "point", "text": kw.get("p1", "首先看保养，其次看年检，最后看记录。")},
            {"type": "point", "text": kw.get("p2", "其实真的非常特别值得关注。")},
            {"type": "cta", "text": kw.get("cta", "让生活更美好，品质保证。")}]


def test_pack_vocab_loads_and_engine_recognises_every_name():
    tells = _load_tells(Pack(ROOT, "elevator"))
    assert tells is not None, "elevator 包配了 ai_tells.yaml，却没被引擎读起来"
    assert not tells.unknown, f"词表里有引擎不认识的 tell 名：{tells.unknown}"
    assert not tells.lexicon_missing, f"声明了却空着词表：{tells.lexicon_missing}"


def test_report_reaches_the_checker_without_blocking_pass():
    """一份满是 tell 的稿子：分数要掉下来，但 passed 不许因此变 False。"""
    pack = Pack(ROOT, "elevator")
    tells = _load_tells(pack)
    report = check_script(_sections(), 60, 4.5, Banwords({"hard": [], "soft": []}),
                          "抖音", Quota({}), tells=tells)
    ai = report["ai_tells"]
    assert ai is not None and ai["score"] < 100, ai
    assert ai["hits"], "构造的稿子应该被检出 tell"
    assert all(h["where"] for h in ai["hits"]), "命中必须带定位，否则改不动"
    # 「不进 passed」的正确判据：同一份稿子接上与不接 tell，合格结论必须一致。
    # 写成 `passed is True` 会变成看时长脸色（构造的稿子短，本来就不合格），
    # 那样这条断言既守不住东西又会假红。
    plain = check_script(_sections(), 60, 4.5, Banwords({"hard": [], "soft": []}),
                         "抖音", Quota({}), tells=None)
    assert plain["passed"] == report["passed"], "文风命中改动了合格判定"


def test_tells_disabled_when_pack_has_no_file(tmp_path):
    """没配词表 → ai_tells 落 null（"没测"），不能伪装成满分。"""
    import shutil
    src = ROOT / "packs" / "elevator"
    dst = tmp_path / "packs" / "elevator"
    shutil.copytree(src, dst)
    (dst / "ai_tells.yaml").unlink()
    data = yaml.safe_load((dst / "pack.yaml").read_text(encoding="utf-8"))
    pack = Pack(tmp_path, "elevator")
    assert pack.ai_tells_data() is None
    assert _load_tells(pack) is None
    report = check_script(_sections(), 60, 4.5, Banwords({"hard": [], "soft": []}),
                          "抖音", Quota({}), tells=None)
    assert report["ai_tells"] is None
    assert data  # 包本身仍可用


def test_unknown_name_is_still_caught():
    """修优先级 bug 的代价不能是把守卫一起丢掉：写错名字仍要报出来。"""
    t = AITells({"strong": ["parallel_triiple", "no_specific"]})
    assert t.unknown == ["parallel_triiple"], t.unknown
    assert "parallel_triiple" in t.warnings()[0]


def test_count_chars_agrees_with_the_module_private_one():
    """ai_tells._count 与 checker.count_chars 必须同一口径（模块注释的承诺）。"""
    from app.ai_tells import _count
    for t in ["困了 23 分钟", "**加粗**一句话，带标点。", "{{待补：小区}}那台用了 8 年",
              "电梯、扶梯、别墅梯都算"]:
        assert _count(t) == count_chars(t), t


def test_score_is_monotone_in_hits():
    a = AITells({"strong": ["no_specific"], "weak": ["repeat_opening"]})
    clean = [{"type": "point", "text": "去年 3 月这台的抱闸换过一次，花了 2400 元。"}]
    dirty = _sections()
    assert a.report(clean)["score"] > a.report(dirty)["score"]
    assert score_of([]) == 100


def test_live_mock_generation_carries_the_score():
    """活体链路：mock 生成的产物里要真有 `check.ai_tells`（不是只在单测里通）。"""
    import shutil
    import tempfile
    tmp = Path(tempfile.mkdtemp(prefix="tells-"))
    try:
        shutil.copytree(ROOT / "packs", tmp / "packs")
        cfg = load_config(tmp)
        cfg.mock = True
        pl = Pipeline(tmp, cfg)
        jid = pl.start_generate(GenerateRequest(pack="elevator", topic="被困电梯怎么办",
                                               duration=60, platform="抖音"))
        snap = wait_job(pl, jid)
        assert snap["state"] == "done", snap.get("error")
        ai = snap["result"]["check"]["ai_tells"]
        assert isinstance(ai, dict) and 0 <= ai["score"] <= 100, ai
        assert ai["tells_enabled"], "包配了词表就必须有 tell 在跑"
        disk = next(tmp.glob("generated/*/*/result.json"))
        assert "ai_tells" in disk.read_text(encoding="utf-8")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
