# -*- coding: utf-8 -*-
"""盘上有、`skill.yaml` 没接线知识件 —— 用户维护而模型永远看不见（第 13 轮复核）。

第 12 轮的注入覆盖测量量出：电梯包里 4 份 md 从不进任何提示词（`compliance/ad-law.md`、
`compliance/platform.md`、`rules/duration.md`、`rules/output-template.md`），而
`packgen` 把这些文件**复制进每一个新建的行业包**，新包的校对清单还让人去填
`ad-law.md` 里那一节 —— 填了也不会生效。这不是"少注一点省钱"，是**账实不符**：
包作者以为自己在配置模型行为。

所以这里钉一条对账：包目录里每个知识件，要么被 `skill.yaml` 引用（进提示词），
要么登记在下面这份"给人看"的清单里并写明给谁看。清单只能作减法 ——
新加一个没接线的文件会红，把某个文件接进提示词后忘记从清单里删也会红。
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 这些由**引擎代码按约定**加载（不是 skill.yaml 里的路径）：$topics_slice /
# $audience_slice / $hooks。登记在这里，并且必须能在 app/ 的代码里 grep 到文件名与
# 切片名 —— 哪天引擎不再加载它，这条登记立刻红，而不是留下一个"以为在注入"的孤儿。
ENGINE_WIRED = {
    "knowledge/topics.md": "topics_slice",
    "knowledge/audience.md": "audience_slice",
    "patterns/hooks.md": "hooks_slice",
}

# 不进提示词是**有意的**，但必须说清给谁看：
#   ad-law.md            给《广告法》词族对账测试当事实源（tests/test_banwords_alignment.py）
#   platform.md          平台侧人工核实清单（含"AI 生成内容须标注"，界面上给人读）
#   duration.md          语速/时长换算的说理，真值在 pack.yaml + checker 里
#   output-template.md   交付物版式说明，导出侧读，不进提示词
HUMAN_ONLY = {
    "compliance/ad-law.md": "tests/test_banwords_alignment.py 的词族事实源",
    "compliance/platform.md": "平台侧人工核实清单（含 AI 标识要求）",
    "rules/duration.md": "时长换算说理，真值在 pack.yaml/limits 与 checker",
    "rules/output-template.md": "交付物版式说明，导出侧消费",
}

# 这些前缀由别的守卫管：private/ 有单独的"不许出厂/不许经 HTTP 读"账，raw/ 是原始素材
SKIP_PREFIXES = ("private/", "raw/")


def _referenced(skill_cfg) -> set:
    """递归收集 skill.yaml 里出现过的**包内相对路径**。

    刻意不按 `stages.*.files` 的固定形状取值：这张表的结构改过三次
    （`files` → `#章节` 切片 → `anti_ai.levels.*.files`），照形状取就会漏掉新枝，
    而漏掉的那一支正是这条守卫存在的理由。
    """
    out = set()

    def walk(node):
        if isinstance(node, dict):
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
        elif isinstance(node, str):
            head = node.split("#", 1)[0].strip()
            if head.endswith((".md", ".yaml", ".yml")):
                out.add(head.replace("\\", "/"))

    walk(skill_cfg)
    return out


def _orphans(pack_dir: Path) -> list:
    cfg = yaml.safe_load((pack_dir / "skill.yaml").read_text(encoding="utf-8"))
    refs = _referenced(cfg) | set(ENGINE_WIRED)
    found = []
    for f in sorted(pack_dir.rglob("*.md")):
        rel = f.relative_to(pack_dir).as_posix()
        if rel.startswith(SKIP_PREFIXES) or rel == "README.md":
            continue
        if rel not in refs:
            found.append(rel)
    return found


def test_every_pack_knowledge_file_is_wired_or_declared_human_only():
    packs = [d for d in sorted((ROOT / "packs").glob("*")) if (d / "skill.yaml").exists()]
    assert packs, "一个包都没扫到 —— 这条守卫会在空集合上空转，先让它红"
    orphans = {d.name: set(_orphans(d)) for d in packs}
    all_orphans = set().union(*orphans.values())
    offenders = sorted(all_orphans - set(HUMAN_ONLY))
    assert not offenders, (
        f"这些包内知识件既没被 skill.yaml 接线（模型永远看不到），也没登记在"
        f"「给人看」清单里：{offenders}\n"
        + "\n".join(f"  {n}: {sorted(o)}" for n, o in orphans.items()))
    # 清单只能作减法：某个文件在**所有**包里都接线了，就必须把这条登记删掉，
    # 否则下一份同名新文件会被它悄悄放过。
    wired = sorted(rel for rel in HUMAN_ONLY
                   if all(rel not in o for o in orphans.values()))
    assert not wired, f"这些文件已经全部接线，把 HUMAN_ONLY 里的条目删掉：{wired}"
    # ENGINE_WIRED 不是免检通道：文件名与切片名必须真的出现在引擎代码里。
    # 引擎哪天不再加载它（改名、删分支），这份登记就红，而不是继续假装在注入。
    app_src = "\n".join(p.read_text(encoding="utf-8") for p in sorted((ROOT / "app").glob("*.py")))
    for rel, slice_name in ENGINE_WIRED.items():
        assert Path(rel).name in app_src, \
            f"{rel} 已经不出现在 app/ 里了：它到底还进不进提示词？把登记或文件处理掉"
        assert slice_name in app_src, \
            f"占位符 {slice_name} 在 app/ 里找不到了：{rel} 的接线已断，别留在登记表里"


def test_the_guard_really_catches_a_new_unwired_file(tmp_path):
    """自证：往一份真的包拷贝里加一个没接线的 md，守卫必须红（否则它只是在念清单）。"""
    src = ROOT / "packs" / "elevator"
    dst = tmp_path / "packs" / "elevator"
    shutil.copytree(src, dst)
    assert _orphans(dst) and set(_orphans(dst)) <= set(HUMAN_ONLY), "前置：清单外不该有孤儿"
    (dst / "knowledge" / "新加而没接线.md").write_text("# 标题\n\n正文\n", encoding="utf-8")
    assert "knowledge/新加而没接线.md" in _orphans(dst), "加了没接线的文件却抓不到，守卫空转"
