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


def test_feedback_only_names_strong_tells_but_still_counts_weak():
    """A-9 §3.1 #3：回炉**只点名 strong 命中段**，weak 只报总数。

    两个方向都要断言：
      · strong 的点名句必须在（不然模型不知道改哪）；
      · weak 的具体段号**不许**出现（"只改标红段"，否则每轮都在动正常的段落，
        越改越平 —— 朱雀逆向经验与 shuorenhua bounded 档同一条）；
      · 但 weak 的**总数**要说（把数据藏起来不是收敛噪音，是丢信息）。
    """
    from app.ai_tells import AITells
    tells = AITells({"strong": ["slogan_closing"], "weak": ["filler_stack"],
                     "lexicon": {"slogan_closing": ["品质保证"],
                                 "filler_stack": ["其实", "真的"]}})
    secs = [{"type": "hook", "text": "其实真的很多。其实说真的不少。"},
            {"type": "cta", "text": "选我们，品质保证。"}]
    report = check_script(secs, 60, 4.5, BAN, "抖音", Quota({}), tells=tells)
    ai = report["ai_tells"]
    assert {h["severity"] for h in ai["hits"]} == {"strong", "weak"}, ai["hits"]
    fb = Pipeline._violation_feedback(report, secs)
    assert "品质保证" in fb, fb                      # strong：点名 + 带原文
    assert "明显" in fb and "轻微" in fb, fb          # 界面/交付物读的是中文档位，不是 strong/weak
    assert "另有 1 条" in fb and "合计 4 处" in fb, fb   # weak：条数与处数都要说清（扣分按条）
    assert "其实×" not in fb, fb                     # 不把 weak 的点名句喂给模型


def test_feedback_carries_the_negative_list_and_the_two_pass_rule():
    """A-9 §3.1 #1/#5：回炉块里要带「减法→注入人声」两段指令与「六永不注入」负面清单。

    为什么这两段必须在这里而不是 voice.md：voice.md 与 anti-ai-smell.md 都被
    `stages.write.anti_ai.levels.strong.files` 整份注入**撰写轮**提示词（预算实测只剩
    几十字），而 `$feedback_block` 由 pipeline 侧拼接、在 user_template 末尾 ——
    这是唯一不占撰写轮预算的通道。
    ⚠ 顺带钉住位置：静态规矩在「上一版全文」之前（动态内容在最后，前缀缓存才稳）。
    """
    secs = _sections()
    report = check_script(secs, 60, 4.5, BAN, "抖音", Quota({}))
    fb = Pipeline._violation_feedback(report, secs)
    assert "六条永不注入" in fb and "{{待补}}" in fb, fb
    assert "分两步做" in fb and "第一步只做减法" in fb and "第二步再把人声补回去" in fb, fb
    assert fb.index("六条永不注入") < fb.index("上一版全文"), fb[-200:]
    assert fb.index("分两步做") < fb.index("上一版全文"), fb[-200:]
    # 只出现一次：多轮回炉不是把规矩抄 N 遍的理由
    assert fb.count("六条永不注入") == 1, fb


def test_feedback_says_why_when_it_can_name_nothing():
    """什么问题都点不出来时，仍要说"未通过校验"—— 那句兜底不能变成死代码。

    存在理由（A-9 审查 P3）：两条静态规矩（两段指令）是无条件追加的，
    判空若写在它们后面就永远为假，于是回炉反馈会出现"没有任何问题清单、
    却教了模型两段改写规矩"的形状 —— 模型收到的信号是"照抄风格"，没人告诉它这版为什么被退。
    """
    clean = {"hard_hits": [], "soft_hits": [], "deviation_pct": 0.0, "segments": [],
             "ai_tells": None, "tolerance_pct": 10.0, "duration_target": 60,
             "estimated_seconds": 60, "chars_total": 261, "target_total": 261}
    fb = Pipeline._violation_feedback(clean)
    assert "未通过校验" in fb, fb
    assert fb.index("未通过校验") < fb.index("六条永不注入"), fb


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
