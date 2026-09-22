# -*- coding: utf-8 -*-
"""回炉反馈的可执行性（P2-41 / 主报告 §三）与容差口径（P2-27 的后半）。

只给「命中词×次数」的 critique 等于让模型重新掷一次骰子；checker 还必须把
本次真正用的容差外发，否则 15 秒档会出现「报告说合格、反馈催你改长度」。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.checker import Banwords, Quota, check_script, count_chars  # noqa: E402
from app.pipeline import Pipeline  # noqa: E402

BAN = Banwords({"hard": ["政府补贴"], "soft": ["绝对"]})


def _sections():
    return [{"type": "hook", "text": "你家电梯要是有政府补贴那件事，绝对靠谱。"},
            {"type": "point", "text": "第二点讲保养，第三点讲年检。"}]


def test_hits_carry_the_offending_line_and_segment():
    report = check_script(_sections(), 60, 4.5, BAN, "抖音", Quota({}))
    hard = report["hard_hits"][0]
    assert hard["word"] == "政府补贴"
    assert hard["at"], "命中必须带定位，否则反馈只能给统计数字"
    assert hard["at"][0]["segment"] == 1
    assert "政府补贴" in hard["at"][0]["line"]


def test_report_exports_the_tolerance_actually_used():
    # 15 秒档按 max(10, 3/duration*100) 放宽到 ±20%
    r15 = check_script(_sections(), 15, 4.5, BAN, "抖音", Quota({}))
    assert r15["tolerance_pct"] == 20.0
    r60 = check_script(_sections(), 60, 4.5, BAN, "抖音", Quota({}))
    assert r60["tolerance_pct"] == 10.0


def test_feedback_uses_report_tolerance_not_a_hardcoded_ten():
    """偏差在容差内时不许出现「时长偏差」那条 —— 修复前写死 10%，
    15 秒档 15% 偏差（合格）仍被反馈要求改长度。"""
    # 单段 78 字 / 4.5 字每秒 ≈ 17.3 秒，对 15 秒目标正好是 +15.6%：
    # 超 10% 但在 15 秒档的自适应容差（±20%）之内。
    sections = [{"type": "hook", "text": "电" * 78}]
    report = check_script(sections, 15, 4.5, Banwords({"hard": [], "soft": []}),
                          "抖音", Quota({}))
    assert 10 < abs(report["deviation_pct"]) <= report["tolerance_pct"], \
        "构造用例本身要落在「>10% 但在本次容差内」这个区间"
    fb = Pipeline._violation_feedback(report, sections)
    assert "时长偏差" not in fb


def test_feedback_quotes_the_previous_draft():
    """Self-Refine： critique 之外还要把上一版喂回去改（主报告 §三第 2 行）。"""
    sections = _sections()
    report = check_script(sections, 60, 4.5, BAN, "抖音", Quota({}))
    fb = Pipeline._violation_feedback(report, sections)
    assert "上一版全文" in fb
    assert "你家电梯要是有政府补贴那件事" in fb
    assert "第 1 段" in fb and "第 2 段" in fb
    # 反馈必须点名违规原句，而不是只报统计
    assert "政府补贴" in fb


def test_feedback_without_previous_draft_still_works():
    report = check_script(_sections(), 60, 4.5, BAN, "抖音", Quota({}))
    fb = Pipeline._violation_feedback(report)
    assert "上一版全文" not in fb and "命中硬禁用词" in fb


def test_count_chars_used_by_report_is_still_the_checker_one():
    assert count_chars("政府补贴，绝对靠谱。") == 8


def test_fake_llm_signatures_cover_every_real_keyword(tmp_path):
    """假 LLM 的 chat_json 必须接得住真客户端的每一个形参。

    这条用例的存在理由：`max_tokens` 与 `on_attempt` 两次加形参时，
    测试里的假客户端都是**跑到红**才发现（TypeError 被工作线程吞成
    job failed，报出来是一个看不出根因的 TimeoutError）。
    """
    import ast
    import inspect

    from app.llm import LLMClient
    real = set(inspect.signature(LLMClient.chat_json).parameters) - {"self"}
    root = Path(__file__).resolve().parent
    checked = 0
    for f in sorted(root.glob("test_*.py")):
        tree = ast.parse(f.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "chat_json":
                args = {a.arg for a in node.args.args} | {
                    a.arg for a in node.args.kwonlyargs}
                if node.args.kwarg is not None:     # **kw 兜住一切
                    continue
                missing = real - args
                assert not missing, f"{f.name} 的假 chat_json 缺形参：{sorted(missing)}"
                checked += 1
    assert checked >= 3, f"扫描到的假客户端数量异常：{checked}"


def test_fake_llm_cfgs_expose_every_attribute_the_pipeline_reads(tmp_path):
    """假客户端的 `cfg` 必须提供流水线真读的那些属性。

    存在理由（本轮踩了两次）：`_plan_key` 加了 `cfg.base_url` 之后，7 个假客户端
    同时在跑红 —— 但它们报的不是「签名不对」，而是作业 `failed` +
    `type object 'cfg' has no attribute 'base_url'`，被工作线程包成一句中文错误，
    看都看不出来。签名守卫（上一条）管不到属性，这条补上。
    """
    import ast
    import re

    src = (Path(__file__).resolve().parent.parent / "app" / "pipeline.py").read_text(
        encoding="utf-8")
    wanted = set(re.findall(r"\bclient\.cfg\.([a-z_]+)", src))
    assert wanted, "流水线不再读 client.cfg.* —— 这条守卫该改口径而不是空转"
    root = Path(__file__).resolve().parent
    checked = 0
    for f in sorted(root.glob("test_*.py")):
        tree = ast.parse(f.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call)
                    and getattr(node.func, "id", "") == "SimpleNamespace"):
                continue
            keys = {k.arg for k in node.keywords if k.arg}
            if "model" not in keys:
                continue                 # 不是假 LLM 的 cfg
            missing = wanted - keys
            assert not missing, (f"{f.name}:{node.lineno} 的假 cfg 缺属性："
                                 f"{sorted(missing)}（流水线在读它们）")
            checked += 1
    assert checked, "一个假 cfg 都没扫到 —— 守卫空转了"


def test_soft_hits_are_named_as_ad_law_words_not_colloquialism():
    """soft 表是广告法绝对化词，反馈里说成「口语化表述」会把模型指到错方向。"""
    sections = [{"type": "point", "text": "这是最安全的选择，绝对可靠。"}]
    report = check_script(sections, 60, 4.5,
                          Banwords({"hard": [], "soft": ["绝对", "最安全"]}), "抖音",
                          Quota({}))
    fb = Pipeline._violation_feedback(report, sections)
    assert "广告法软禁用词" in fb and "口语化" not in fb, fb
    assert "第 1 段" in fb, fb
