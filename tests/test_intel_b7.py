# -*- coding: utf-8 -*-
"""B7：情报进 select / draft 提示词的接线测试。

跑法：pytest tests/test_intel_b7.py -v

为什么需要这一组：B7 的价值在"情报真的进提示词"。而接线有两处容易**静默断**：

1. **模板忘了引用** `$intel_block`（或拼错名）——渲染照常成功、零报错，
   `string.Template.safe_substitute` 对未知变量保持原样，引擎注入的情报静默丢失。
   `test_prompt_templates.py` 只查"ctx 该提供的都有"（正向），不查"模板确实用了"（反向）。
2. **select_for_prompt 挑错条目**——挑成别的领域的、或把原文带进提示词、
   或把含 hard 禁用词的条目放行。这些都是"情报块存在但内容不对"的静默降级。

另：`data_dir=None` 时 ctx 必须给空串而不是抛错（新装的正常状态，见 `_intel` 注释）。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.intel import PROMPT_HEAD, select_for_prompt               # noqa: E402
from app.knowledge import Pack, list_packs                     # noqa: E402
from app.llm import LLMClient                                    # noqa: E402
from app.prompts import PromptRenderer                         # noqa: E402

# 与 test_prompt_templates 同款假参数（合并路径参数齐，全阶段占位符都有上下文）
def _fake_params(pack: Pack) -> dict:
    from app.checker import Quota
    quota = Quota.from_pack(pack.data)
    duration = float(pack.param_default("duration", 60) or 60)
    rate = pack.rate_for_style(pack.param_default("style", ""))
    target = quota.target(duration, rate) if quota.available else {}
    if not target:
        total = max(round(duration * rate * 0.95), 20)
        target = {"total": total, "hook": round(total * .15),
                  "body": round(total * .65), "cta": round(total * .20)}
    return {
        "pack": pack.name, "topic": "B7 接线测试主题",
        "segment": pack.param_default("segment", "默认细分"),
        "audience": pack.param_default("audience", "默认受众"),
        "platform": pack.param_default("platform", "抖音"),
        "style": pack.param_default("style", "亲和接地气"),
        "persona": pack.param_default("persona", "默认人设"),
        "cta": pack.param_default("cta", "关注"),
        "facts": "测试用补充资料", "rate": rate, "voice": "strong",
        "format": "both", "points": 3, "quota": target, "duration": duration,
    }


def _packs_with_draft_or_select() -> list[tuple[Pack, str]]:
    """返回 (包, 阶段) 对：阶段在 skill.yaml 里声明且 user_template 存在。"""
    out = []
    for info in list_packs(ROOT):
        p = Pack(ROOT, info.name)
        skill = p.skill() or {}
        stages = skill.get("stages") or {}
        for stage in ("select", "draft"):
            st = (stages or {}).get(stage)
            if st and st.get("user_template"):
                out.append((p, stage))
    return out


@pytest.mark.parametrize("pack,stage", _packs_with_draft_or_select(),
                         ids=lambda v: getattr(v, "name", v))
def test_templates_reference_intel_block(pack, stage):
    """反向断言：每个带 select/draft 的包，模板**真的引用** `$intel_block`。

    正向（test_prompt_templates）保证"ctx 该有的都有"；本条保证"模板确实用了"。
    两条合起来才是 B7 的接线完整。
    """
    pr = PromptRenderer(pack)
    # 取 stage 对应 user_template 原文（不走渲染，避免 safe_substitute 静默吞掉拼错的占位符）
    skill = pr.skill
    st = ((skill.get("stages") or {}).get(stage) or {})
    tpl = st.get("user_template") or ""
    assert tpl, f"{pack.name}/{stage} 没有 user_template"
    assert "$intel_block" in tpl, (
        f"{pack.name}/{stage} 的 user_template 没引用 $intel_block —— "
        "引擎注入的情报会静默丢失（B7 接线断）")


def test_select_ctx_has_intel_block_key():
    """ctx 恒有 intel_block 键：data_dir=None（新装）给空串，不抛错。"""
    for info in list_packs(ROOT):
        pack = Pack(ROOT, info.name)
        pr = PromptRenderer(pack)  # data_dir=None
        ctx = pr.select_ctx(_fake_params(pack))
        assert "intel_block" in ctx
        assert ctx["intel_block"] == ""
        assert "intel_key" in ctx
        assert ctx["intel_key"] == ""


def test_plan_key_stable_across_intel_refresh():
    """B-3：情报刷新不应让选题缓存全失效。

    `_plan_key` 只把 `intel_key`（选中项 id + 出处）带进指纹，不带情报块全文。
    选中的还是那几条 → intel_key 不变 → 指纹不变 → 同一条选题仍可复现。
    选中的变了 → intel_key 变 → 指纹变（该重选就重选）。
    反过来，若实现退回"整块进指纹"，每次刷新（哪怕选中没变）都会撞出不同指纹。
    """
    import dataclasses

    from app.pipeline import Pipeline
    from tests.test_engine_audit10 import CFG  # noqa: F401

    client = LLMClient(dataclasses.replace(CFG.llm, base_url="https://api.A.com/v1",
                                           model="deepseek-chat"))
    system, user = "s", "u"  # 关键：user 里不带情报块（块是渲染后才拼进去的）
    k0 = Pipeline._plan_key(client, system, user, "")
    # 同一批选中项 → 同一短键 → 指纹稳定（情报刷新不该让历史版本失效）
    assert Pipeline._plan_key(client, system, user, "a1,a2,a3") == \
           Pipeline._plan_key(client, system, user, "a1,a2,a3")
    # 选中项变了 → 指纹变（该重选就重选）
    assert Pipeline._plan_key(client, system, user, "a1,a2,a3") != \
           Pipeline._plan_key(client, system, user, "b1,b2,b3")
    # 没情报（intel_key 空）和有条目要区分开，否则"没抓到情报"会命中"有条目"的缓存
    assert Pipeline._plan_key(client, system, user, "") != \
           Pipeline._plan_key(client, system, user, "a1,a2,a3")
    # 情报块全文不能进指纹：刷新后块内标题微变、但选中的还是那几条（intel_key 不变），
    # 也应命中同一缓存 —— 这正是 _plan_key 只带短键的意义。
    # 模拟：user 前缀相同、intel_key 相同，但块全文不同（刷新后标题措辞变了）。
    user_v1 = "静态体\n" + PROMPT_HEAD + "\n- 旧标题\n"
    user_v2 = "静态体\n" + PROMPT_HEAD + "\n- 新标题（微调措辞）\n"
    assert Pipeline._plan_key(client, system, user_v1, "a1") == \
           Pipeline._plan_key(client, system, user_v2, "a1")
    # 静态体变了 → 指纹必须变（模板升级不算情报刷新）
    user_v3 = "静态体改版\n" + PROMPT_HEAD + "\n- 新标题\n"
    assert Pipeline._plan_key(client, system, user_v3, "a1") != \
           Pipeline._plan_key(client, system, user_v2, "a1")


def _make_items(segment: str) -> list[dict]:
    """造一组覆盖各分支的情报条目。"""
    return [
        {"title": "本领域最新政策 A", "segment": segment, "published": "2026-09-20",
         "url": "https://gov.cn/a", "score": {"opportunity": 9, "E": 5}},
        {"title": "本领域政策 B", "segment": segment, "published": "2026-09-01",
         "url": "https://gov.cn/b", "score": {"opportunity": 7, "E": 6}},
        {"title": "通用动态 C", "segment": "", "published": "2026-09-10",
         "url": "https://x.com/c", "score": {"opportunity": 8, "E": 4}},
        {"title": "别的领域 D", "segment": "别的领域", "published": "2026-09-15",
         "url": "https://x.com/d", "score": {"opportunity": 10, "E": 5}},
        {"title": "无出处 E", "segment": segment, "published": None, "url": None,
         "score": {"opportunity": 6, "E": 3}},
    ]


def test_select_for_prompt_picks_relevant_and_drops_banned(tmp_path):
    """挑最相关 + 含 hard 禁用词的被摘且返回。"""
    import json
    from app.intel import intel_dir

    pack = "elevator"
    seg = "电梯维保"
    d = tmp_path / "data"
    (intel_dir(d, pack)).mkdir(parents=True)
    (intel_dir(d, pack) / "latest.json").write_text(
        json.dumps({"items": _make_items(seg)}, ensure_ascii=False), encoding="utf-8")

    block, keys, dropped = select_for_prompt(d, pack, seg, banned=("政策",))
    # 两条含"政策"的标题被摘 → 剩通用 C / 别的领域 D / 无出处 E 三条
    assert len(dropped) == 2, dropped
    assert "本领域最新政策 A" in dropped
    assert "本领域政策 B" in dropped
    # 挑 3 条：通用 C（本领域被摘光后，通用优先于别的领域、无出处垫底）
    assert len(keys) == 3, (block, keys)
    assert "通用动态 C" in block
    assert "别的领域 D" in block
    # 不带原文（只给标题+出处+链接）
    assert "最新政策 A" not in block
    assert "机会分 9" not in block.lower()
    # 指纹短键只含 id+出处（这里没有 id 字段，短键至少不含整条标题）
    for k in keys:
        assert "\n" not in k
        assert k not in block  # 短键不该是正文片段


def test_select_for_prompt_empty_is_empty_string(tmp_path):
    """没数据返回空串而不是'没有这类情报'话术（静默降级反例）。"""
    import json
    from app.intel import intel_dir

    pack = "elevator"
    d = tmp_path / "data"
    (intel_dir(d, pack)).mkdir(parents=True)
    (intel_dir(d, pack) / "latest.json").write_text(
        json.dumps({"items": []}), encoding="utf-8")
    block, keys, dropped = select_for_prompt(d, pack, "电梯维保")
    assert block == ""
    assert keys == []
    assert dropped == []


def test_draft_ctx_injects_intel_with_data_dir(tmp_path):
    """有 data_dir 时 draft_ctx 的 intel_block 真带上条目（端到端接线）。"""
    import json
    from app.intel import intel_dir
    from app.knowledge import list_packs

    pack = "elevator"
    d = tmp_path / "data"
    (intel_dir(d, pack)).mkdir(parents=True)
    (intel_dir(d, pack) / "latest.json").write_text(
        json.dumps({"items": _make_items("电梯维保")}, ensure_ascii=False),
        encoding="utf-8")

    info = next(i for i in list_packs(ROOT) if i.name == pack)
    pr = PromptRenderer(Pack(ROOT, pack), data_dir=d)
    ctx = pr.draft_ctx(_fake_params(Pack(ROOT, pack)))
    assert ctx["intel_block"] != ""
    assert "通用动态 C" in ctx["intel_block"]
