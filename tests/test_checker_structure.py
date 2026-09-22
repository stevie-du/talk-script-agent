# -*- coding: utf-8 -*-
"""词表 / 配额 / 要点数 / 校验器的结构调整与守卫测试。

一组「写错结构就静默降级」的修复回归（P1-25/P1-26/P1-27/P3-30/P5-3）：

  1. 词表 `hard: "政府补贴"` 写成标量 → 被当可迭代拆成单字 → 被 MIN_WORD_LEN 全丢
     → 命中归零，而横幅还在报「N 个单字禁用词被忽略（府、政、补…）」。修：
     加载期用 `validate_banwords` 校验结构，fatal 直接 ValueError（带文件名与键路径），
     `dropped_short` 只含作者**真写了**的单字条目。
  2. `quota_table.total: "约290"` → 该键静默消失、quota_degraded=False、
     提示词渲染「总计≈ 字」。修：`Quota` 收集 errors、`target()` 在 total 解析
     失败时整体返回 {} 让调用方公开降级，param_audit 上报具体哪张表哪项。
  3. `points_by_duration` 配 8 点 + `TopicPlan.points max_length=6` → 生成必炸。
     修：上限提到 12。
  4. `duration=0` → dev 恒 0 → 190 字/目标 18 字也判合格。修：非正数直接判参数错误。
  5. `platform.*.extra_soft` 引擎不读且无提示。修：记为 advisory 进 errors/audit。

跑法：pytest tests/test_checker_structure.py
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.checker import Banwords, Quota, check_script, validate_banwords      # noqa: E402
from app.knowledge import (Pack, PackBrokenError, pack_info, param_audit)     # noqa: E402
from app.schemas import TopicPlan                                              # noqa: E402

ELEVATOR = ROOT / "packs" / "elevator"


def _pack_dir(tmp_path: Path, banwords: dict = None, mutate=None) -> Path:
    """最小可用包目录；banwords=/mutate 用来制造坏结构。"""
    data = {
        "name": "probe",
        "params": {
            "duration": {"label": "时长", "options": [60], "default": 60},
            "style": {"label": "风格", "options": ["亲和"], "default": "亲和"},
            "platform": {"label": "平台", "options": ["抖音"], "default": "抖音"},
        },
        "rate_by_style": {"亲和": 4.5},
        "quota_table": {60: {"total": 290, "hook": 45, "body": 190, "cta": 55}},
        "points_by_duration": {60: 3},
    }
    if mutate:
        mutate(data)
    d = tmp_path / "probe"
    (d / "patterns").mkdir(parents=True, exist_ok=True)
    (d / "pack.yaml").write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
                                 encoding="utf-8")
    (d / "patterns" / "hooks.md").write_text(
        "## 二、风格语气模板\n### 亲和\n亲和模板\n", encoding="utf-8")
    (d / "banwords.yaml").write_text(
        yaml.safe_dump(banwords if banwords is not None else {"hard": [], "platform": {}},
                       allow_unicode=True, sort_keys=False),
        encoding="utf-8")
    return d


# ── 第 1 组：词表结构（P1-25）────────────────────────────────

def test_scalar_hard_raises_with_filename_and_keypath():
    with pytest.raises(ValueError) as ei:
        Banwords({"hard": "政府补贴"})
    msg = str(ei.value)
    assert "banwords.yaml" in msg, f"报错要带词表文件名：{msg}"
    assert "hard" in msg, f"报错要带键路径：{msg}"
    assert "非空字符串列表" in msg, msg


def test_scalar_hard_raises_via_load_with_actual_filename(tmp_path):
    p = tmp_path / "my_words.yaml"
    p.write_text("hard: 政府补贴\n", encoding="utf-8")
    with pytest.raises(ValueError) as ei:
        Banwords.load(p)
    assert "my_words.yaml" in str(ei.value), str(ei.value)
    assert "hard" in str(ei.value)


def test_scalar_platform_rule_raises_with_keypath():
    with pytest.raises(ValueError) as ei:
        Banwords({"soft": ["绝对"], "platform": {"抖音": {"extra_hard": "微信"}}})
    msg = str(ei.value)
    assert "platform.抖音.extra_hard" in msg, msg


def test_platform_node_itself_scalar_raises():
    with pytest.raises(ValueError) as ei:
        Banwords({"platform": {"抖音": ["私信我"]}})
    assert "platform.抖音" in str(ei.value)


def test_empty_hard_list_is_valid():
    """`hard: []` 是合法写法（项目里真实存在），不算结构错。"""
    ban = Banwords({"hard": [], "soft": ["最"]})
    assert ban.errors == []
    assert ban.dropped_short == ["最"]


def test_genuine_single_char_entry_keeps_dropped_short_visibility():
    """作者**真写了**单字条目 → dropped_short 保留可见性、且不误报结构错。"""
    ban = Banwords({"hard": ["最", "全网最低价"]})
    assert ban.errors == []
    assert ban.dropped_short == ["最"]
    # MIN_WORD_LEN 对真单字的忽略提示保留：'最近' 不该命中 '最'
    assert ban.scan("最近", None)["soft"] == []
    assert ban.scan("最近", None)["hard"] == []


def test_extra_soft_is_advisory_not_fatal():
    """P5-3：extra_soft 引擎不读 → 记入 errors（advisory），但不炸（词表本体还能用）。"""
    ban = Banwords({"hard": ["绝对安全"], "platform": {"抖音": {"extra_soft": ["最环保"]}}})
    joined = "；".join(ban.errors)
    assert "extra_soft" in joined and "引擎不读" in joined, joined
    assert ban.scan("绝对安全", "抖音")["hard"], "词表本体仍然生效"


def test_valid_elevator_pack_structure_is_clean():
    ban = Banwords.load(ELEVATOR / "banwords.yaml")
    assert ban.errors == []
    assert ban.dropped_short == ["最"]


def test_validate_banwords_splits_fatal_vs_advisory():
    fatal, advisory = validate_banwords(
        {"hard": "政府补贴", "platform": {"抖音": {"extra_soft": "x"}}})
    assert any("hard" in f for f in fatal)
    assert advisory and any("extra_soft" in a for a in advisory)


# ── 第 2 组：quota_table 非数字（P1-26）─────────────────────

def test_quota_total_non_numeric_collects_error_and_degrades():
    q = Quota({60: {"total": "约290", "hook": 45, "body": 190, "cta": 55}})
    assert any("total" in e and "不是数字" in e for e in q.errors), q.errors
    # target() 整体返回 {} → 调用方（pipeline._normalize）必然走 quota_degraded 公开降级，
    # 不会出现「总计≈ 字」的空 total
    assert q.target(60, 5.0) == {}


def test_quota_valid_table_has_no_errors_and_no_degrade():
    q = Quota({60: {"total": 290, "hook": 45, "body": 190, "cta": 55}})
    assert q.errors == []
    out = q.target(60, 5.0)
    assert isinstance(out.get("total"), int) and out["total"] > 0


def test_quota_row_itself_scalar_is_flagged():
    q = Quota({60: "290 字"})
    assert q.errors, "表行不是映射也要报出来"
    assert q.target(60, 5.0) == {}


# ── 第 3 组：TopicPlan 要点数（P1-27）────────────────────────

def test_eight_point_plan_passes_model_validation():
    plan = TopicPlan(angle="a", hook_type="反常识", hook_line="h",
                     points=[f"point{i}" for i in range(8)], cta="关注")
    assert len(plan.points) == 8


def test_point_upper_bound_still_exists():
    """上限提到 12 不等于无上限 —— 模型跑飞（13+ 条）仍要拦。"""
    with pytest.raises(Exception):
        TopicPlan(angle="a", hook_type="反常识", hook_line="h",
                  points=[f"p{i}" for i in range(13)], cta="关注")


# ── 第 4 组：duration 非正数（P3-30）────────────────────────

def test_non_positive_duration_is_a_param_error_not_pass():
    ban = Banwords({"hard": []})
    secs = [{"type": "point", "text": "这是很长的一段正文，足有快两百字。" * 20}]
    for dur in (0, -5):
        rep = check_script(secs, dur, 4.5, ban)
        assert rep["passed"] is False, f"duration={dur} 必须判失败"
        assert any("时长参数非正数" in b for b in rep["blockers"]), rep["blockers"]


def test_check_report_carries_banword_notes():
    """extra_soft 的 advisory 提示要跟着校验报告走（不静默）。"""
    ban = Banwords({"hard": [], "platform": {"抖音": {"extra_soft": ["最环保"]}}})
    rep = check_script([{"type": "point", "text": "你好"}], 60, 4.5, ban)
    assert any("extra_soft" in n for n in rep.get("banword_notes", []))


# ── 第 5 组：param_audit / pack_info 上报─────────────────────

def test_param_audit_reports_scalar_hard_on_platform_options(tmp_path):
    d = _pack_dir(tmp_path, banwords={"hard": "政府补贴"})
    audit = param_audit(d, yaml.safe_load((d / "pack.yaml").read_text(encoding="utf-8")))
    text = audit["platform"]["抖音"]
    assert "整个禁用词表都失效" in text, text
    assert "hard" in text, f"要指明键路径：{text}"


def test_param_audit_reports_quota_total_not_a_number(tmp_path):
    def mutate(d):
        d["quota_table"] = {60: {"total": "约290", "hook": 45, "body": 190, "cta": 55}}

    d = _pack_dir(tmp_path, mutate=mutate)
    audit = param_audit(d, yaml.safe_load((d / "pack.yaml").read_text(encoding="utf-8")))
    text = audit["duration"]["60"]
    assert "total" in text and "不是数字" in text, text


def test_param_audit_reports_unknown_style_hooks_fallback(tmp_path):
    """P1-25 顺带：风格没在 hooks.md 里 → 切片退回整份，必需要有降级提示。"""
    def mutate(d):
        d["params"]["style"]["options"] = ["亲和", "未知风"]
        d["rate_by_style"]["未知风"] = 4.5     # 语速配了，但 hooks.md 没这风格

    d = _pack_dir(tmp_path, mutate=mutate)
    audit = param_audit(d, yaml.safe_load((d / "pack.yaml").read_text(encoding="utf-8")))
    text = audit["style"]["未知风"]
    assert "退回整份钩子库" in text, text
    # 有模板的风格不能被误报
    assert "亲和" not in audit.get("style", {})


def test_param_audit_reports_extra_soft_advisory(tmp_path):
    d = _pack_dir(tmp_path, banwords={
        "hard": [], "platform": {"抖音": {"extra_soft": ["最环保"]}}})
    audit = param_audit(d, yaml.safe_load((d / "pack.yaml").read_text(encoding="utf-8")))
    text = audit["platform"]["抖音"]
    assert "extra_soft" in text and "引擎不读" in text, text


def test_pack_info_marks_structure_broken_banwords(tmp_path):
    root = tmp_path / "root"
    shutil.copytree(ROOT / "packs", root / "packs")
    (root / "packs" / "elevator" / "banwords.yaml").write_text(
        "hard: 政府补贴\n", encoding="utf-8")

    info = pack_info(root / "packs" / "elevator")
    assert "banwords.yaml" in info.pack_error, info.pack_error
    assert "hard" in info.pack_error, info.pack_error


def test_pack_construction_rejects_structure_broken_banwords(tmp_path):
    """生成路径：结构坏词表在构造期就拦下（PackBrokenError），不烧一轮 token。"""
    root = tmp_path / "root"
    shutil.copytree(ROOT / "packs", root / "packs")
    (root / "packs" / "elevator" / "banwords.yaml").write_text(
        "hard: 政府补贴\n", encoding="utf-8")
    with pytest.raises(PackBrokenError) as ei:
        Pack(root, "elevator")
    msg = str(ei.value)
    assert "banwords.yaml" in msg and "hard" in msg, msg


def test_real_pack_audit_stays_clean():
    """前提守卫：正常包（含真正的单字条目「最」）不能被误报成结构错。"""
    data = yaml.safe_load((ELEVATOR / "pack.yaml").read_text(encoding="utf-8"))
    assert param_audit(ELEVATOR, data) == {}
    assert pack_info(ELEVATOR).pack_error == ""