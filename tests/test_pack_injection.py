# -*- coding: utf-8 -*-
"""选题/撰写阶段的注入内容与**前缀缓存**顺序（P0/P1：红线、选题库、回炉块位置）。

跑法：pytest tests/test_pack_injection.py

四条各自钉住一个已复测的缺陷：

1. **选题阶段拿到的是可执行的红线口径**（以前是 0 条：`select.files` 里根本没有
   `redlines`，而 hooks 实例自己还在示范"还在用就是在违法"这种法律定性）；
2. **红线只注「红线速查」那一节**（整份 `compliance/industry.md` 2957 字里，
   模型每轮要的是 ~800 字可执行口径，其余是给 checker 与人工看的细则）；
3. **`knowledge/ideas.md` 真的进选题阶段**（60 条选题此前从未被任何阶段注入），
   且其中的受众/钩子类型取值落在契约的取值域内；
4. **`$feedback_block` 在用户提示词末尾**：回炉轮与首轮共享整段静态前缀，
   DeepSeek 式前缀缓存才不会把 ~7k 字重算一遍。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.checker import Quota                                      # noqa: E402
from app.knowledge import Pack                                     # noqa: E402
from app.prompts import PromptRenderer                             # noqa: E402

PLAN = {"angle": "角度", "hook_type": "反常识", "hook_line": "钩子句",
        "points": ["要点一", "要点二"], "cta": "关注"}


@pytest.fixture(scope="module")
def pack() -> Pack:
    return Pack(ROOT, "elevator")


@pytest.fixture(scope="module")
def renderer(pack):
    return PromptRenderer(pack)


def _params(pack: Pack) -> dict:
    quota = Quota.from_pack(pack.data)
    rate = pack.rate_for_style("亲和接地气")
    return {"pack": pack.name, "topic": "困人了怎么办",
            "segment": pack.param_default("segment"),
            "audience": pack.param_default("audience"),
            "platform": pack.param_default("platform"), "style": "亲和接地气",
            "persona": pack.param_default("persona"),
            "cta": pack.param_default("cta"), "facts": "", "rate": rate,
            "voice": "strong", "format": "both", "points": 3, "duration": 60.0,
            "quota": quota.target(60.0, rate)}


# ── 1. 选题阶段带红线 ─────────────────────────────────────────
def test_select_prompt_carries_compliance_lines(renderer, pack):
    _, user = renderer.render("select", renderer.select_ctx(_params(pack)))
    for kw in ("96333", "统一救援口径", "反谣言", "抱闸"):
        assert kw in user, f"选题提示词缺红线口径「{kw}」—— 角度/钩子会先写错再靠回炉救"
    red = renderer.select_ctx(_params(pack))["redlines"]
    full = pack.file_text("compliance/industry.md")
    assert 0 < len(red) < 900, f"红线切片 {len(red)} 字，未瘦成速查节"
    assert len(red) < len(full), "注入的还是整份 industry.md"


# ── 2. 撰写阶段的红线是同一条切片，且提示词变小 ───────────────
def test_write_prompt_redlines_is_the_same_compact_slice(renderer, pack):
    ctx = renderer.write_ctx(_params(pack), PLAN, "")
    assert ctx["redlines"] == renderer.select_ctx(_params(pack))["redlines"], \
        "选题与撰写的红线不同源，回炉时会互相矛盾"
    _, user = renderer.render("write", ctx)
    assert len(user) < 7600, f"撰写一轮 {len(user)} 字（P1-34 前是 8411）"
    assert "96333" in user and "抱闸" in user


# ── 3. 选题库进提示词，且取值合规 ─────────────────────────────
def test_ideas_section_is_injected_into_select(renderer, pack):
    ctx = renderer.select_ctx(_params(pack))
    assert ctx["ideas"], "ideas.md 的「## 选题库」切片为空（章节名与 skill.yaml 不一致？）"
    _, user = renderer.render("select", ctx)
    probe = "进电梯先看这个标志，过期一天都别坐"      # ideas.md 独有的一句
    assert probe in user, "选题库没有被注入（P1-34 未修）"
    assert "受众默认：物业业委会" in user
    # 公式/禁忌那两节是给人读的，不该混进注入
    assert "挖选题的正确姿势" not in user


def _hook_types(pack: Pack) -> set[str]:
    """钩子库类型表第一列（`## 一、钩子库` 之后、第一个 `###` 之前的那张表）。"""
    types: set[str] = set()
    started = False
    for ln in pack.file_text("patterns/hooks.md").splitlines():
        if ln.startswith("## "):
            started = "钩子库" in ln
            continue
        if started and ln.startswith("### "):        # 类型表之后是"禁用清单"等小节
            break
        if started and ln.strip().startswith("|"):
            cell = ln.strip().strip("|").split("|")[0].strip()
            if cell and set(cell) != {"-"} and cell != "类型":
                types.add(cell)
    return types


def test_ideas_field_values_match_the_contract(pack):
    """`#选题库` 一节里显式写的受众 ∈ audience 选项，钩子类型 ∈ hooks.md 的类型表。"""
    section = pack.file_slice("knowledge/ideas.md#选题库")
    assert section
    valid_aud = {str(a) for a in pack.param_options("audience")}
    types = _hook_types(pack)
    assert len(types) == 10, f"钩子库类型数 {len(types)}，与 skill.yaml 的「10 类」不符"
    tagged = 0
    for ln in section.splitlines():
        ln = ln.strip()
        if not ln or not ln[0].isdigit() or "—" not in ln:
            continue
        parts = [x.strip() for x in ln.split("—", 1)[1].split("·") if x.strip()]
        assert parts, f"选题行缺钩子类型：{ln}"
        assert parts[-1] in types, f"钩子类型不在钩子库 10 类内：{ln}"
        tagged += 1
        if len(parts) > 1:
            assert parts[0] in valid_aud, f"受众不是 audience 选项名：{ln}"
    assert tagged == 60, f"选题库条目数 {tagged}，应为 60 条"
    # 「权威科普 / 清单钩子」这类把风格名当钩子类型的写法已经不再出现在选题库里
    assert "· 权威科普" not in section and "清单钩子" not in section.split("## 选题公式")[0]


# ── 4. 回炉块在末尾 → 前缀缓存 ────────────────────────────────
def test_feedback_block_sits_at_the_end_so_the_prefix_is_stable(renderer, pack):
    p = _params(pack)
    _, u1 = renderer.render("write", renderer.write_ctx(p, PLAN, ""))
    _, u2 = renderer.render("write", renderer.write_ctx(p, PLAN, "- 时长偏差 +24%\n- 命中：绝对安全"))
    assert u2.startswith(u1), "回炉轮与首轮不再共享完整前缀 → 整段静态提示词按未命中计费"
    tail = u2[len(u1):]
    assert "回炉改写" in tail, f"变化点不在末尾（尾部 40 字：{tail[-40:]!r}）"
    assert "回炉改写" not in u1, "首轮提示词里怎么会有回炉块"


# ── 5. 钩子实例不再示范违规表述 ───────────────────────────────
# 这些短语的**写法**（法律定性 / 无法核实的资历与概率自证）。红线速查里会以
# "禁则"的形式提到它们，那是要的；被禁的形态是**钩子实例把它们当范文示范**。
BAD_PATTERNS = [r"就\s*是\s*违\s*法", r"七\s*万\s*三\s*千\s*次",
                r"干\s*了\s*几\s*十\s*年", r"修\s*了\s*十\s*五\s*年",
                r"绝\s*大\s*多\s*数?\s*人", r"[九数]\s*成\s*的\s*人"]


def _bad_in(text: str) -> list[str]:
    import re
    return [p for p in BAD_PATTERNS if re.search(p, text)]


def test_hook_examples_no_longer_model_violations(pack, renderer):
    p = _params(pack)
    bad = _bad_in(pack.file_text("patterns/hooks.md"))
    assert not bad, f"hooks.md 仍在示范违规表述：{bad}"
    # 注入选题阶段的那一段钩子内容（按风格切片后的结果）
    slice_text = pack.hooks_slice(p["style"])
    assert slice_text and slice_text != pack.file_text("patterns/hooks.md"), \
        "钩子库按风格切片失效，退回整份"
    bad = _bad_in(slice_text)
    assert not bad, f"选题提示词的钩子范文里仍有违规表述：{bad}"
    # 法律定性词本身仍由词表拦截（示例清了，检测能力不能一起清掉）：
    # 原钩子范文那句"还在用，就是在违法"必须命中 hard。
    from app.checker import Banwords
    bw = Banwords(pack.banwords_data())
    hits = bw.scan("你家电梯的这个标志，超过这个日期还在用，就是在违法。")["hard"]
    assert hits, "法律定性表述的词表检测没命中（词表清示例时被一并清掉了？）"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
