# -*- coding: utf-8 -*-
"""提示词里不许出现"去看某个文件"式指路（P3-28 / P3-38 那一类）。

模型没有文件系统。`knowledge/topics.md:184`、`private/faq.yaml:39` 都写过
"统一口径见 compliance/industry.md 反谣言库" —— 批次 5 声称"industry.md 已随
P0-18 注入、所以不悬空"，但**实际注入的是 `compliance/industry.md#红线速查` 那一节**
（808 字，内容确实覆盖了救援口径/反谣言/96333/119），所以：

  *  substance 到位了（这半句是真的）；
  *  指向一个模型手里没有的文件名，仍然会让它知道"还有权威依据我没拿到"，
    于是要么含糊带过、要么自行编一个口径 —— 与 P0-17 同一条因果链。

修法是把指路改成指**提示词里已有的那段**（「见本提示词的【行业红线】段」）。
本文件守的是"渲染出来的文本里不再出现任何 `*.md` 路径"，
对电梯包与新建包都查 —— 新建包的骨架来自 `packs/_template`，模板里写一句
就复制给每一个行业。
"""
from __future__ import annotations

import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import load_config                        # noqa: E402
from app.knowledge import Pack                            # noqa: E402
from app.pipeline import Pipeline                         # noqa: E402
from app.prompts import PromptRenderer                    # noqa: E402

# 形如 `compliance/industry.md`、`standards.md`、`rules/duration.md`，
# 以及 `*.yaml`（`patterns/growth.md` 里写过「见 skill.yaml」、
# `private/faq.yaml` 写过 `private/service.yaml` —— 模型手里没有一个叫这名字的文件）
MD_REF = re.compile(r"\S{0,30}\.(?:md|ya?ml)\b")


def _plan(pk: Pack):
    return {"angle": "a", "hook_type": pk.param_options("hook_type")[0]
            if pk.param_options("hook_type") else "反常识",
            "hook_line": "钩子", "points": ["一"], "cta": "关注"}


def _params(pk: Pack, **over) -> dict:
    p = {"topic": "家用电梯怎么选", "segment": pk.param_options("segment")[0],
         "audience": pk.param_options("audience")[0], "duration": 60,
         "style": pk.param_options("style")[0], "platform": pk.param_options("platform")[0],
         "persona": "", "cta": "关注", "facts": "", "rate": 4.5, "voice": "strong",
         "format": "both", "points": 3,
         "quota": {"total": 290, "hook": 45, "body": 190, "cta": 55},
         "quota_degraded": False}
    p.update(over)
    return p


def _rendered(root: Path, pack_name: str) -> dict:
    pk = Pack(root, pack_name)
    pr = PromptRenderer(pk)
    p = _params(pk)
    plan = _plan(pk)
    out = {}
    for stage, ctx in (("select", pr.select_ctx(p)),
                       ("write", pr.write_ctx(p, plan, "反馈")),
                       ("storyboard", pr.storyboard_ctx(
                           [{"type": "point", "text": "正文"}],
                           [{"start": 0.0, "end": 3.0}])),
                       ("rewrite_segment", pr.rewrite_ctx(
                           [{"type": "point", "text": "正文"}], 0, 90, "改口语化"))):
        s, u = pr.render(stage, ctx)
        out[stage] = s + u
    return out


def test_elevator_prompts_carry_no_file_pointers():
    """每个细分/受众/风格/平台选项都渲染一遍。

    切片内容随选项变，指路句可能只藏在某一节里 —— 只测默认参数会漏
    （`private/faq.yaml` 那条就只在被选中的那段里出现）。
    """
    pk = Pack(ROOT, "elevator")
    pr = PromptRenderer(pk)
    base = _params(pk)
    bad = []
    for opt_key in ("segment", "audience", "style", "platform"):
        for opt in pk.param_options(opt_key):
            p = dict(base)
            p[opt_key] = opt
            for stage, ctx in (("select", pr.select_ctx(p)),
                               ("write", pr.write_ctx(p, _plan(pk), "反馈"))):
                s, u = pr.render(stage, ctx)
                for hit in MD_REF.findall(s + u):
                    bad.append(f"{stage}[{opt_key}={opt}] → {hit}")
    assert not bad, "提示词里出现了模型打不开的文件引用：\n" + "\n".join(sorted(set(bad)))


def test_newly_generated_pack_prompts_carry_no_file_pointers():
    """新包的提示词同样要干净 —— 模板写一句就复制到所有行业。"""
    os.environ.pop("TALKSCRIPT_MOCK", None)
    tmp = Path(tempfile.mkdtemp(prefix="dangling-"))
    try:
        shutil.copytree(ROOT / "packs", tmp / "packs")
        cfg = load_config(tmp)
        cfg.mock = True
        pl = Pipeline(tmp, cfg)
        from app.packgen import create_pack
        info = create_pack(tmp, pl.llm, "宠物医院", "连锁宠物医院，面向养宠家庭")
        for stage, text in _rendered(tmp, info["name"]).items():
            hits = MD_REF.findall(text)
            assert not hits, f"新包 {stage} 阶段提示词里有文件指路：{hits[:4]}"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_human_only_docs_may_still_reference_files():
    """反向确认这条守卫不是"删掉所有引用"：人读文件里的指路是必要的。

    `rules/duration.md`、`knowledge/standards.md` 里互相引用没问题 —— 它们
    不进提示词。守卫只管**渲染出来的文本**。
    """
    pk = Pack(ROOT, "elevator")
    human = pk.file_text("knowledge/standards.md")
    assert MD_REF.search(human) or "TSG" in human, \
        "人读文件也该带引用/依据，否则这条反向守卫失去意义"
