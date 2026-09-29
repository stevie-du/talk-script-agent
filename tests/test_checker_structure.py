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


def test_two_platform_notes_all_reach_the_report_exactly_once():
    """`checker.py:280->279` 从没走到 —— 也就是**同时有两条落空说明**时的形状没证据。

    promote 与 demote 各落空一次 = 两条 note。这不只是覆盖率数字：`errors` 是
    `param_audit` 与校验报告读的那本账，去重靠的是 `if note not in self.errors`，
    而它以前只在"一条 note"的情况下被验过 —— 第二条会不会被吞掉、会不会被重复追加，
    都没有断言说过话。正对照在下面的 `check_script` 输出里：账要说得出，词表也要照用。
    """
    ban = Banwords({"hard": ["绝对安全"], "soft": ["最安全"],
                    "platform": {"小红书": {"promote": ["从没出现在 soft 里的词"],
                                            "demote": ["也不是硬词的词"]}}},
                   source="banwords.yaml")
    ban.scan("这篇讲维保。", platform="小红书")
    notes = [e for e in ban.errors if "落空" in e]
    assert len(notes) == 2, ban.errors
    assert any("promote" in n for n in notes) and any("demote" in n for n in notes), notes
    # 再扫一次同平台：不许把同一句说明记成两笔（账本重复 = 报告里同一条出现两次）
    ban.scan("又一段。", platform="小红书")
    assert len([e for e in ban.errors if "落空" in e]) == 2, ban.errors
    # 落空的两条不影响真词表照常生效（否则"报出来"变成"把词表关了"）
    assert ban.scan("这个绝对安全。", platform="小红书")["hard"], ban.errors


def test_banwords_top_level_and_platform_shape_errors_are_spoken():
    """覆盖率实测：`checker.py:92/94-95/112-114` 三条 fatal 从没被执行过。

    这三条是**整份词表写坏**时唯一会说出口的地方（`hard:` 写成标量那条上面刚测过，
    所以只补没证据的三面）：
      · 顶层不是映射（整份 yaml 写成一列 `- 政府补贴`）→ fatal 并立刻返回，
        不许再往下按 dict 取键（那是 AttributeError，包作者只看到一句类型错）；
      · `platform:` 不是映射 → 同样 fatal 并返回（后面的 `platforms.items()` 会炸穿）；
      · 键为 None = "没配"，静默通过（与上面两种"写了但写坏"是两回事）。
    正对照必须同时给：写对的结构要 0 fatal，否则"有 fatal"这条断言是恒真的。
    """
    fatal, advisory = validate_banwords(["政府补贴"], source="banwords.yaml")
    assert fatal and "顶层必须是映射" in fatal[0] and "banwords.yaml" in fatal[0], fatal
    assert advisory == []
    fatal2, _ = validate_banwords({"hard": ["绝对安全"], "platform": ["抖音"]})
    assert any("platform 必须是映射" in f for f in fatal2), fatal2
    assert validate_banwords(None) == ([], []), "没配 ≠ 配坏"
    ok, ok_adv = validate_banwords(
        {"hard": ["绝对安全"], "platform": {"抖音": {"promote": ["最安全"]}}})
    assert ok == [] and ok_adv == [], (ok, ok_adv)


def test_quota_table_non_mapping_and_bad_duration_key_are_spoken():
    """`checker.py:138/140/365/369-371`：整张表写坏、时长键写坏，两支都要有账（实测 0 覆盖）。

    对应包作者两种真实笔误：`quota_table:` 下面写成列表，以及把 `60:` 写成 `六十:`。
    `Quota()` 在这之后必须**不炸**（早退），因为 pipeline 只把 errors 汇进 param_audit、
    不在加载期抛 —— 抛了就是"配置写错 = 生成失败"，而这里要的是"照样生成 + 说得出哪坏了"。
    """
    from app.checker import quota_table_errors
    assert quota_table_errors(None) == []
    assert any("必须是映射" in e for e in quota_table_errors([60])), quota_table_errors([60])
    q = Quota([60])
    assert q.table == {} and any("必须是映射" in e for e in q.errors), q.errors
    bad = Quota({"六十": {"total": 290, "hook": 45, "body": 190, "cta": 55}})
    assert any("时长键" in e and "不是数字" in e for e in bad.errors), bad.errors
    assert bad.table == {} and bad.target(60, 5.0) == {}, bad.table


def test_locate_hits_caps_the_list_but_still_walks_every_section():
    """`checker.py:348/349` 从没执行过 —— "命中够数就截断"这条支一直没有证据。

    `locate_hits` 供的是回炉反馈与界面下划线里"改哪一句"那份清单（P2-41）。
    截断写坏不会崩，只会让清单越拉越长 / 后面那几段永远进不来 —— 两种都看不见。
    所以上限内与上限外各测一次：`max_per_word=2` 必须在第 2 处收住，
    `max_per_word=4` 必须走完两段（只测前者的话，把 348/349 两行删掉也能过）。
    """
    from app.checker import locate_hits
    secs = [{"type": "point", "text": "政府补贴要讲。政府补贴不能承诺。政府补贴是红线。"},
            {"type": "point", "text": "政府补贴还有下文。"}]
    hits = [{"word": "政府补贴", "count": 4}]
    locate_hits(hits, secs, max_per_word=2)
    at = hits[0]["at"]
    assert len(at) == 2 and [p["segment"] for p in at] == [1, 1], at
    assert all(p["line"] for p in at), at
    hits4 = [{"word": "政府补贴", "count": 4}]
    locate_hits(hits4, secs, max_per_word=4)
    assert [p["segment"] for p in hits4[0]["at"]] == [1, 1, 1, 2], hits4[0]["at"]


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


def test_quota_interpolates_between_rows_and_extrapolates_outside():
    """★ 插值/外推整段从没被执行过（2026-09-29 分支覆盖实测 `checker.py:413-428`）。

    两个包的 5 档时长（15/30/60/90/180）都精确命中表行，所以这一路**不在当前 UI 路径上**
    —— 它只在包作者把 `quota_table` 与 `duration.options` 改得不配套时走到，
    而 `packgen` 生成的新包正是那种形状：那时每个非表内时长都靠这里算。
    下面这些数是**实测打印**的，不是手算的（round 的半值取偶会让手算差 1）。
    """
    q = Quota({30: {"total": 145, "hook": 25, "body": 90, "cta": 30},
               60: {"total": 290, "hook": 45, "body": 190, "cta": 55}})
    assert q.target(45, 5.0) == {"total": 218, "hook": 35, "body": 140, "cta": 42}
    assert q.target(15, 5.0) == {"total": 72, "hook": 15, "body": 40, "cta": 18}, "下外推"
    assert q.target(90, 5.0) == {"total": 435, "hook": 65, "body": 290, "cta": 80}, "上外推"
    # 表里的档照旧精确命中：插值支不许把 60 秒也算成一个"接近"的数
    assert q.target(60, 5.0) == {"total": 290, "hook": 45, "body": 190, "cta": 55}
    # 语速不是基准 5.0 时按比例折算（插值 × 折算两层叠起来才是线上口径）
    assert q.target(45, 4.5) == {"total": 196, "hook": 32, "body": 126, "cta": 38}


def test_quota_interpolation_drops_a_broken_field_but_the_error_is_already_public():
    """某一行把 `hook` 写成 `"约25"` → 这一项插不出数就**跳过该项**，其余照常。

    跳过本身不可怕，可怕的是无声：`quota_table_errors` 先在那一行报了名，
    而 `total` 坏掉时整份配额仍然返回 `{}` 走公开降级（上面两条用例量的就是那一步）。
    这里钉的是中间形态：单个字段坏 ≠ 整张表坏，也 ≠ 静默给一个 0。
    """
    q = Quota({30: {"total": 145, "hook": "约25", "body": 90, "cta": 30},
               60: {"total": 290, "hook": 45, "body": 190, "cta": 55}})
    assert any("hook" in e and "不是数字" in e for e in q.errors), q.errors
    out = q.target(45, 5.0)
    assert "hook" not in out and out["total"] == 218, out


def test_quota_scalar_rows_and_single_row_table():
    """`_row` 的两条降级支各测一次（覆盖率实测：`if not keys` 与 `len(keys)==1` 都没走过）。

    · 整张表都写成 `60: "290 字"` → 没有任何可用行 → `{}` → 调用方公开降级。
      （修复前这里 `keys[0]` IndexError，整个生成以「list index out of range」失败。）
    · 单行表 → 任何时长都取这一行（修复前 `keys[1]` 同样崩）。
    """
    q = Quota({30: "290 字", 60: "600 字"})
    assert q.errors and q.target(45, 5.0) == {}, (q.errors, q.target(45, 5.0))
    single = Quota({60: {"total": 290, "hook": 45, "body": 190, "cta": 55}})
    assert single.errors == []
    for d in (15, 60, 120):
        assert single.target(d, 5.0) == {"total": 290, "hook": 45, "body": 190, "cta": 55}, d


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