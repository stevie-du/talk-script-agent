# -*- coding: utf-8 -*-
"""skill.yaml 模板与渲染上下文的**一致性**测试。

跑法：python tests/test_prompt_templates.py   或   pytest tests/test_prompt_templates.py

为什么需要这一组：`string.Template.safe_substitute` 对未知变量**保持原样**，
所以 skill.yaml 里拼错的占位符既不会报错、也不会中断生成 —— 模型只会收到
一段字面量（比如 "$growth_block"），而本该注入的知识文件静默丢失。

这不是假设：elevator 包里真实存在过这个 bug ——
`stages.write.files` 的 key 是 `growth`，模板写的却是 `$growth_block`，
于是 `patterns/growth.md`（完播与转化方法）**从来没有进过提示词**。
本组测试对**每一个**含 skill.yaml 的行业包渲染全部阶段，断言零残留占位符。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.checker import Quota                                    # noqa: E402
from app.knowledge import Pack, list_packs                       # noqa: E402
from app.prompts import PromptRenderer                           # noqa: E402


def _fake_params(pack: Pack) -> dict:
    """一份「参数都填满」的归一结果，确保所有占位符都有对应上下文。"""
    quota = Quota.from_pack(pack.data)
    duration = float(pack.param_default("duration", 60) or 60)
    rate = pack.rate_for_style(pack.param_default("style", ""))
    target = quota.target(duration, rate) if quota.available else {}
    if not target:
        total = max(round(duration * rate * 0.95), 20)
        target = {"total": total, "hook": round(total * .15),
                  "body": round(total * .65), "cta": round(total * .20)}
    return {
        "pack": pack.name, "topic": "模板一致性测试主题",
        "segment": pack.param_default("segment", "默认细分"),
        "audience": pack.param_default("audience", "默认受众"),
        "platform": pack.param_default("platform", "抖音"),
        "style": pack.param_default("style", "亲和接地气"),
        "persona": pack.param_default("persona", "默认人设"),
        "cta": pack.param_default("cta", "关注"),
        "facts": "测试用补充资料", "rate": rate, "voice": "strong",
        "format": "both", "points": 3, "quota": target, "duration": duration,
    }


_PLAN = {"angle": "角度", "hook_type": "反常识", "hook_line": "钩子句",
         "points": ["要点一", "要点二"], "cta": "关注"}


def _packs_with_skill() -> list[Pack]:
    out = []
    for info in list_packs(ROOT):
        p = Pack(ROOT, info.name)
        if p.skill():
            out.append(p)
    return out


def test_every_pack_renders_without_unfilled_placeholders():
    packs = _packs_with_skill()
    assert packs, "没有找到任何带 skill.yaml 的行业包"
    problems = []
    for pack in packs:
        pr = PromptRenderer(pack)
        p = _fake_params(pack)
        stages = pr.skill.get("stages", {}) or {}

        sections = [{"type": "hook", "text": "钩子"},
                    {"type": "point", "text": "要点"}]
        timings = [{"start": 0.0, "end": 5.0}, {"start": 5.0, "end": 14.0}]
        renderings = [("select", pr.select_ctx(p))]
        renderings.append(("write", pr.write_ctx(p, _PLAN, "")))
        renderings.append(("write(回炉)", pr.write_ctx(p, _PLAN, "- 时长偏差 +24%")))
        renderings.append(("rewrite_segment",
                           pr.rewrite_ctx(sections, 1, 50, "更口语")))
        renderings.append(("storyboard", pr.storyboard_ctx(sections, timings)))
        for stage, ctx in renderings:
            base = stage.split("(")[0]
            if base not in stages:
                continue
            missing = pr.unfilled(base, ctx)
            if missing:
                problems.append(f"{pack.name}/{stage}: {'、'.join(missing)}")
    assert not problems, "模板里存在未填充的占位符：\n  " + "\n  ".join(problems)


def test_stage_files_keys_are_injected():
    """stages.<阶段>.files 声明的每个文件，内容都应真的出现在提示词里。

    这条比「零残留占位符」更进一步：它检查的是「文件确实被注入」，
    而不只是「占位符被替换成了空串」。

    遍历**包里的全部阶段**而不是写死 select/write：growth.md 曾经就是这么
    静默漏掉一整轮的（files 的 key 与占位符不同名），而 rewrite_segment /
    storyboard 也各自有一个 ctx 构造器 —— 漏掉哪个，那个阶段的 files 就白声明了。
    """
    problems = []
    for pack in _packs_with_skill():
        pr = PromptRenderer(pack)
        p = _fake_params(pack)
        sections = [{"type": "hook", "text": "钩子文案"},
                    {"type": "point", "text": "要点文案"}]
        timings = [{"start": 0.0, "end": 5.0}, {"start": 5.0, "end": 14.0}]
        ctxs = {
            "select": pr.select_ctx(p),
            "write": pr.write_ctx(p, _PLAN, ""),
            "rewrite_segment": pr.rewrite_ctx(sections, 1, 50, "更口语"),
            "storyboard": pr.storyboard_ctx(sections, timings),
        }
        stages = pr.skill.get("stages", {}) or {}
        unknown = sorted(set(stages) - set(ctxs))
        assert not unknown, (f"{pack.name} 有新阶段 {unknown} 没被本测试覆盖 —— "
                             "给它加 ctx 构造，别删这条断言")
        for stage, ctx in ctxs.items():
            files = (stages.get(stage, {}) or {}).get("files") or {}
            if not files:
                continue
            _, user = pr.render(stage, ctx)
            for key, rel in files.items():
                # 走 file_slice 而不是 file_text：`路径#章节` 的取值只注入那一节，
                # 探针必须按引擎真正注入的内容取，否则整份文件的首行永远"未注入"。
                content = pack.file_slice(str(rel))
                if not content.strip():
                    continue
                # 取文件里第一行有效正文，确认它出现在提示词中
                probe = next((ln.strip() for ln in content.splitlines()
                              if ln.strip() and not ln.strip().startswith(("#", ">", "|", "-"))),
                             "")
                if probe and probe not in user:
                    problems.append(f"{pack.name}/{stage}: {rel} 未注入（key={key}）")
    assert not problems, "有声明却未注入的知识文件：\n  " + "\n  ".join(problems)


def test_declared_stage_files_are_actually_found():
    """`stages.<阶段>.files` 声明的每个取值都必须真取得到内容。

    为什么单独一条：`路径#章节` 的语义是「只要这一节」，章节名写错或被人改名
    得到的是**空串**（`Pack.file_slice` 故意不退回整份），于是那份知识又不进
    提示词了 —— 正是 P0-18 那一半没被「零残留占位符」覆盖的形态（占位符填了，
    填的是空）。anti_ai.levels.*.files 不在此处断言：那里的约定是
    「不存在的文件静默跳过」，新包不必照搬电梯的 voice.md。
    """
    problems = []
    for pack in _packs_with_skill():
        pr = PromptRenderer(pack)
        for stage, cfg in (pr.skill.get("stages", {}) or {}).items():
            for key, rel in ((cfg or {}).get("files") or {}).items():
                spec = str(rel)
                path = spec.partition("#")[0].strip()
                if not pack.file_text(path).strip():
                    problems.append(f"{pack.name}/{stage}.{key}: 文件读不到 → {path}")
                elif not pack.file_slice(spec).strip():
                    problems.append(f"{pack.name}/{stage}.{key}: 章节切片为空 → {spec}")
    assert not problems, "声明了却注入不进去的知识文件：\n  " + "\n  ".join(problems)


def test_growth_file_is_actually_injected():
    """elevator 包的 growth.md 必须真的进提示词（回归上面提到的真实 bug）。"""
    pack = Pack(ROOT, "elevator")
    pr = PromptRenderer(pack)
    _, user = pr.render("write", pr.write_ctx(_fake_params(pack), _PLAN, ""))
    growth = pack.file_text("patterns/growth.md")
    assert growth.strip(), "patterns/growth.md 不应为空"
    probe = next(ln.strip() for ln in growth.splitlines()
                 if ln.strip() and not ln.strip().startswith(("#", ">", "|", "-")))
    assert probe in user, f"growth.md 未注入提示词（探针：{probe[:40]!r}）"
    assert "$growth_block" not in user, "占位符名与 files 的 key 不一致（应为 $growth）"


def test_unfilled_detector_itself():
    """检测器本身：只认模板里引用、上下文没给的标识符。

    刻意检查模板原文而非渲染结果 —— 注入的知识文件里可能出现 $ 字样，
    扫渲染结果会误报。
    """
    pack = Pack(ROOT, "elevator")
    pr = PromptRenderer(pack)
    pr.skill = {"stages": {"t": {
        "system": "你是$industry",
        "user_template": "$a 与 $b 与 ${c}；价格 $100 不算占位符；$$ 是转义",
    }}}
    assert pr.unfilled("t", {"industry": "x", "a": "1", "b": "2", "c": "3"}) == []
    assert pr.unfilled("t", {"industry": "x"}) == ["$a", "$b", "$c"]
    pr.skill["stages"]["t"]["system"] = "无占位符"
    pr.skill["stages"]["t"]["user_template"] = "无占位符"
    assert pr.unfilled("t", {}) == []
    # 注入内容里出现 $ 字样不应误报
    pr.skill["stages"]["t"]["user_template"] = "$industry\n${" + "industry" + "}知识正文里的 $foo"
    assert pr.unfilled("t", {"industry": "x"}) == ["$foo"]


def main() -> int:
    cases = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in cases:
        try:
            fn()
            print(f"  ✅ {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  ❌ {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(cases) - failed}/{len(cases)} 通过")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
