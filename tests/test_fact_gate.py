# -*- coding: utf-8 -*-
"""A-9 **fact gate**：回炉改写不许把具体信息改丢了（本地校验，不是 prompt 约束）。

来源：`调研报告-20260928-去AI味机制对标补遗.md` §3.1 #2（ZeroSlop 的 Fact gate）。
本仓原来的状况：`SCOPE_INSTRUCTION` 三档都写着「文号、数字、引文与 {{待补}} 不许动」，
但**没有任何一处代码核对过这件事** —— 回炉轮把「96333」改成「那个号码」、
把 `{{待补：3天}}` 删掉，校验照样只看禁用词与时长，没人说得出话。

本文件钉四层：
  1. `specific_tokens` / `fact_survival` 纯函数（**双向**：少了要抓到、多了不许报）；
  2. 拒收本轮：仍然不合格 + 改丢了事实 → 退回上一版，下一轮点名要补回什么；
  3. 合规优先：这一版把硬违规修好了（哪怕少了一个数字）→ **照收**，但必须留痕；
  4. 首轮没有"上一版"，gate 不参与（否则每个作业都在拿空集比）。
"""
from __future__ import annotations

import copy
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.checker import fact_survival, specific_tokens   # noqa: E402
from app.config import load_config                       # noqa: E402
from app.mock_fixtures import _PLAN_TRAP, _SECTIONS_CLEAN, _SECTIONS_DIRTY  # noqa: E402
from app.pipeline import Pipeline, wait_job              # noqa: E402
from app.schemas import GenerateRequest                  # noqa: E402

#: 带**阿拉伯数字**的一句话。fact gate 只认得住这类可等值比对的东西。
#: ⚠ 刻意**不用 96333**：那是电梯包的 `banwords.yaml:131` 里的硬禁用词
#:   （"不是全国统一号码，不许写死"），拿它当"合格终版里的事实"会让夹具的名字说谎
#:   —— 那一份稿子其实永远不合格，测试就只是在测"不合格退回不合格"（A-9 审查 P2）。
FACT = "／救援到场中位用时 26 分钟，说清楚小区名、几号楼、哪部梯。"
FACT_LOST = "／救援到场中位用时半个钟头，说清楚小区名、几号楼、哪部梯。"


def _with(src: list[dict], tail: str) -> list[dict]:
    """把那句话装进第 4 段（要点段）—— 长度只差几个字，判定因素是**事实与违规**。"""
    secs = copy.deepcopy(src)
    secs[3]["text"] = secs[3]["text"].rstrip("。") + tail
    return secs


R1 = _with(_SECTIONS_DIRTY, FACT)             # 脏（含「政府补贴」）+ 带事实
R2_LOST = _with(_SECTIONS_DIRTY, FACT_LOST)   # 同样脏，还把那个数字改没了 → 该被拒收
CLEAN_KEEP = _with(_SECTIONS_CLEAN, FACT)     # 干净 + 事实原样
CLEAN_LOST = _with(_SECTIONS_CLEAN, FACT_LOST)  # 干净但改丢了事实 → 合规优先，照收留痕


# ── 1. 纯函数 ────────────────────────────────────────────────
def test_tokens_carry_numbers_placeholders_titles_and_long_quotes():
    secs = [{"type": "point", "text": "按 GB 7588 执行，2025 年 3 月复检，周期 15 日。"
                                      "{{待补：救援时长}}《特种设备安全法》"
                                      "「出现下列情况之一时应当报废」以及「限速器」"}]
    toks = specific_tokens(secs)
    assert "2025" in toks and "3" in toks and "15" in toks, toks
    assert "{{待补：救援时长}}" in toks, toks
    assert "《特种设备安全法》" in toks, toks
    assert any(t.startswith("「出现下列") for t in toks), toks
    # 短的「」是**术语**，换个说法不算丢事实 —— 不收，否则 gate 会拦掉正当的改写
    assert not any("限速器" in t for t in toks), toks


def test_survival_only_reports_losses_not_additions():
    prev = [{"type": "point", "text": "周期 15 日。"}]
    assert fact_survival(prev, [{"type": "point", "text": "周期 15 日、季度 30 日。"}]) == []
    assert fact_survival(prev, [{"type": "point", "text": "周期若干日。"}]) == ["15"]
    # 同一处丢两次只说一次（去重），但计数确实按多重集比
    two = [{"type": "point", "text": "15 日一次，15 日一次。"}]
    one = [{"type": "point", "text": "15 日一次。"}]
    assert fact_survival(two, one) == ["15"], fact_survival(two, one)
    assert fact_survival(one, one) == []


# ── 2~4. 接线（真跑管道）──────────────────────────────────────
class Scripted:
    """按提示词内容回稿的假客户端（`Pipeline.llm` 的可替换缝）。

    ⚠ 形参必须与 `LLMClient.chat_json` 对齐 —— 由
    `test_refine_feedback.test_fake_llm_signatures_cover_every_real_keyword` 全仓扫。
    """

    class cfg:                                     # noqa: N801
        temperature = 0.7
        max_tokens = 4000
        model = "fake-model"
        base_url = "http://fake/v1"

    #: 假上游不是 mock 通道（`result.mock` 要落 False，产物才算真稿）
    mock = False

    def __init__(self, rewrites: list[list[dict]], final: list[dict]):
        self.rewrites = list(rewrites)
        self.final = final
        self.writes: list[str] = []

    def chat_json(self, task, system, user, model_cls, max_retries=1,
                  on_retry=None, temperature=None, on_delta=None,
                  max_tokens=None, on_attempt=None, should_abort=None,
                  usage=None, deadline=None):
        if task == "storyboard":
            from app.mock_fixtures import _STORYBOARD
            return model_cls.model_validate({"storyboard": _STORYBOARD})
        if "先定选题" in user:                      # 合并阶段：选题 + 首版正文
            return model_cls.model_validate({"plan": _PLAN_TRAP, "sections": self.first})
        self.writes.append(user)
        payload = self.rewrites.pop(0) if self.rewrites else self.final
        return model_cls.model_validate({"sections": payload})


def _run(tmp_path, rewrites, final, first=None):
    shutil.copytree(ROOT / "packs", tmp_path / "packs")
    cfg = load_config(tmp_path)
    cfg.mock = False
    pl = Pipeline(tmp_path, cfg)
    client = Scripted(rewrites, final)
    client.first = R1 if first is None else first
    pl.llm = client
    jid = pl.start_generate(GenerateRequest(pack="elevator", topic="被困电梯怎么办",
                                           duration=60, platform="抖音"))
    snap = wait_job(pl, jid, timeout=90)
    assert snap["state"] == "done", snap.get("error")
    return snap, client


def _gate_steps(snap) -> list[dict]:
    return [s for s in snap["steps"] if s["key"].startswith("fact_gate")]


def test_a_round_that_loses_a_fact_without_fixing_anything_is_rejected(tmp_path):
    """R2 仍然含硬违规、又把那个数字改没了 → 本轮作废，下一轮点名要补回。"""
    snap, client = _run(tmp_path, [R2_LOST, CLEAN_KEEP], CLEAN_KEEP)
    gate = _gate_steps(snap)
    assert gate, [s["key"] for s in snap["steps"]]
    assert "26" in gate[0]["data"]["lost"], gate[0]["data"]
    # 拒收 = 这一版**不是**终稿：终稿里那个数字回来了，硬违规也没了
    text = "".join(s["text"] for s in snap["result"]["sections"])
    assert "26" in text and "政府补贴" not in text, text
    assert snap["result"]["check"]["passed"] is True, snap["result"]["check"]
    # 下一轮的反馈里点名"你上一轮改丢了什么"（不点名 = 模型没有机会补回去）
    assert any("26" in w and "改丢" in w for w in client.writes), client.writes


class AltScripted(Scripted):
    """同一批稿件，但每轮都带一个候选版（reroll 的形状）。

    存在的理由（A-9 审查 P1）：候选版在 `ScriptDraft` 里是 `list[sections]`，
    而交付契约 `ScriptResult.alternatives` 要的是过滤后的 `[{sections, check}]`。
    过滤原先只写在"收下"那条出口 —— fact gate 多开的"末轮拒收"出口漏了它，
    整个作业以 pydantic 校验失败收场（**钱烧完才炸**）。
    """

    def chat_json(self, task, system, user, model_cls, **kw):
        out = super().chat_json(task, system, user, model_cls, **kw)
        d = out.model_dump()
        if task != "storyboard":
            d["alternatives"] = [copy.deepcopy(CLEAN_KEEP)]
        return model_cls.model_validate(d)


def test_a_rejected_last_round_still_delivers_the_alternatives_contract(tmp_path):
    """末轮拒收时，退回的那一版的候选版也必须是契约形状（P1 的回归）。"""
    shutil.copytree(ROOT / "packs", tmp_path / "packs")
    cfg = load_config(tmp_path)
    cfg.mock = False
    pl = Pipeline(tmp_path, cfg)
    c = AltScripted([R2_LOST, R2_LOST], R2_LOST)     # 每一轮都丢事实且不合格 → 末轮必然拒收
    c.first = R1
    pl.llm = c
    jid = pl.start_generate(GenerateRequest(pack="elevator", topic="被困电梯怎么办",
                                           duration=60, platform="抖音", reroll=True))
    snap = wait_job(pl, jid, timeout=90)
    assert snap["state"] == "done", snap.get("error")
    gate_keys = [s["key"] for s in snap["steps"] if s["key"].startswith("fact_gate")]
    assert gate_keys == ["fact_gate_r2", "fact_gate_r3"], gate_keys   # 两轮都丢、都拒
    alts = snap["result"]["alternatives"]
    assert alts and all(isinstance(a, dict) and {"sections", "check"} <= set(a) for a in alts), alts
    assert alts[0]["check"]["passed"] is True, alts[0]["check"]


def test_compliance_beats_fact_retention_but_still_leaves_a_trace(tmp_path):
    """这版把硬违规修好了、但少了一个数字 → **照收**（合规优先），并且必须留痕。

    反过来"为了保数字留着「政府补贴」"是要出事的：数字丢了人还能核回，
    交付物里带着广告法硬违规出门口碑就没了。
    """
    snap, _ = _run(tmp_path, [CLEAN_LOST], CLEAN_LOST)
    gate = _gate_steps(snap)
    assert gate, [s["key"] for s in snap["steps"]]
    assert "26" in gate[0]["data"]["lost"], gate[0]["data"]
    assert gate[0]["data"].get("passed") is True, gate[0]["data"]
    text = "".join(s["text"] for s in snap["result"]["sections"])
    assert "政府补贴" not in text, text          # 收下的是修好合规的那一版


def test_first_round_never_meets_the_gate(tmp_path):
    """没有"上一版"就无所谓丢失：首轮直接合格时，不该出现任何 fact_gate 步骤。

    首稿用 `mock_fixtures` 里已被验证「能过 60s 校验」的那份（不加自造句子），
    否则这里红的是时长而不是 gate。
    """
    snap, _ = _run(tmp_path, [], CLEAN_KEEP, first=copy.deepcopy(_SECTIONS_CLEAN))
    assert not _gate_steps(snap), [s["key"] for s in snap["steps"]]
    assert snap["result"]["check"]["passed"] is True, snap["result"]["check"]
    assert [s["key"] for s in snap["steps"] if s["key"].startswith("write")] == [], \
        "首轮就合格，不该有回炉轮"


def test_a_clean_rewrite_loses_nothing_and_is_adopted(tmp_path):
    """正向对照（防止 gate 变成"每轮都拦"）：事实原样保留 + 修好违规 → 收下、无痕。"""
    snap, _ = _run(tmp_path, [CLEAN_KEEP], CLEAN_KEEP)
    assert not _gate_steps(snap), [s["key"] for s in snap["steps"]]
    text = "".join(s["text"] for s in snap["result"]["sections"])
    assert "26" in text and "政府补贴" not in text, text
