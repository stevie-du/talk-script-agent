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

# 登记按**包/路径**整体作键（第 16 轮自查：只按相对路径作键时，把某个文件在
# elevator 里接进提示词之后，它在 `_template` 里的同名副本仍被这条登记悄悄豁免 ——
# "清单只作减法"那条判据要等**所有**包都接线才会红，等于不会红）。
# 现在每个包要各自的登记；新增一个包就得显式决定，而不是靠名字撞上来。
HUMAN_ONLY = {
    "elevator/compliance/ad-law.md": "tests/test_banwords_alignment.py 的词族事实源",
    "elevator/compliance/platform.md": "平台侧人工核实清单（含 AI 标识要求）",
    "elevator/rules/duration.md": "时长换算说理，真值在 pack.yaml/limits 与 checker",
    "elevator/rules/output-template.md": "交付物版式说明，导出侧消费",
    "_template/compliance/ad-law.md": "随模板复制到新包，给人看；词族事实源那份在"
                                      " elevator（test_banwords_alignment 只读它）",
    "_template/compliance/platform.md": "随模板复制到新包；平台侧人工核实清单",
    "_template/rules/duration.md": "随模板复制到新包；时长换算说理",
    "_template/rules/output-template.md": "随模板复制到新包；交付物版式说明",
}

# 顶层 `private/` 免检，但**理由要写清**：那批文件由 `pack.yaml` 的 `files.private`
# 清单接线（`knowledge.Pack.private_facts` 读它），不走 skill.yaml —— 也就是这条守卫
# 现在只看一本账里的接线，另一本账（pack.yaml）里有没有列它，它并不查。
# ⚠ 这是一条**已知的粗免检**（第 16 轮复核 P2-9 的后半）：往 private/ 里放一份
# 既没进 `files.private` 也没人读的 md，今天不会红。收紧的做法是把 pack.yaml 也
# 交给 `_referenced()` 走一遍，然后取消这个前缀免检。
# 只免**顶层**：`knowledge/private/x.md` 不是资料池的位置，照红（实测，是想要的行为）。
SKIP_PREFIXES = ("private/",)

# 建包时引擎写给**人**看的交付物，每个生成包里都有一份，不该指望 skill.yaml 接线。
# （第 16 轮实测：往 packs/ 里放一个生成过的包 —— 例如开发者用仓库根跑一次建包 ——
#  守卫会因为这份 `校对清单.md` 而红，把一次正常操作变成一条假故障。）
# ⚠ 按**精确相对路径**免检，不是按文件基名：第 18 轮复核量到基名免检会连带放过
#   `knowledge/校对清单.md`、`rules/校对清单.md` 这类放错地方、引擎永不注入的文件 ——
#   同一笔提交刚把 HUMAN_ONLY 从"按相对路径跨包豁免"改成"按 包/路径"，理由一模一样。
HUMAN_ARTIFACTS = {"校对清单.md"}


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
        if rel in HUMAN_ARTIFACTS:
            continue
        if rel not in refs:
            found.append(rel)
    return found


def _bodies_with(src: str, name: str, slice_name: str) -> list:
    """`src` 里**同时**含 `name` 与 `slice_name` 的那些函数体（返回函数名列表）。

    只认函数体：类体或模块体里两个名字各自漂浮，说明不了"这一件由这一条路径注入"。
    """
    import ast
    out = []
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            seg = ast.get_source_segment(src, node) or ""
            if name in seg and slice_name in seg:
                out.append(node.name)
    return out


def test_every_pack_knowledge_file_is_wired_or_declared_human_only():
    packs = [d for d in sorted((ROOT / "packs").glob("*")) if (d / "skill.yaml").exists()]
    assert packs, "一个包都没扫到 —— 这条守卫会在空集合上空转，先让它红"
    orphans = {d.name: {f"{d.name}/{rel}" for rel in _orphans(d)} for d in packs}
    all_orphans = set().union(*orphans.values())
    offenders = sorted(all_orphans - set(HUMAN_ONLY))
    assert not offenders, (
        f"这些包内知识件既没被 skill.yaml 接线（模型永远看不到），也没在"
        f"「给人看」清单里按 包/路径 登记：{offenders}\n"
        + "\n".join(f"  {n}: {sorted(o)}" for n, o in orphans.items()))
    # 清单只能作减法：某一条登记（包/路径）已经接线了，就必须把这条删掉；
    # 只在**别的**包里接线不算 —— 键是 包/路径，所以每个包各自管自己。
    wired = sorted(rel for rel in HUMAN_ONLY
                   if all(rel not in o for o in orphans.values()))
    assert not wired, (f"这些登记对应的文件已经**不再是孤儿**了 —— 在该包里被接进提示词，"
                       f"或者那份文件/那个包已经不在了；两种情况都该删掉这条登记：{wired}")
    # ENGINE_WIRED 不是免检通道：文件名与切片/占位符名必须出现在**同一个函数体**里。
    # 旧判据是"两者各自在 app/ 的某个角落出现过"—— 把两对调（`topics.md` 配 `hooks_slice`）
    # 也不会红（第 16 轮复核 P3-11），而这条登记存在的理由恰恰是"这一件确实由这一条注入路径
    # 加载"。类体与模块体不算一处接线 —— 那正是两个名字能各自漂浮的地方。
    srcs = {p.name: p.read_text(encoding="utf-8")
            for p in sorted((ROOT / "app").glob("*.py"))}
    for rel, slice_name in ENGINE_WIRED.items():
        name = Path(rel).name
        hits = [f"{f}:{fn}" for f, s in srcs.items()
                for fn in _bodies_with(s, name, slice_name)]
        assert hits, (
            f"{rel} 与 {slice_name} 从没出现在同一个函数体里 —— 这条登记的接线大概已断。"
            f"现状：文件名在 {sorted(f for f, s in srcs.items() if name in s)}，"
            f"占位符在 {sorted(f for f, s in srcs.items() if slice_name in s)}。"
            f"要么把接线补回同一处，要么把 {rel} 从 ENGINE_WIRED 摘掉并说明它现在怎么进提示词。")


def test_the_guard_really_catches_a_new_unwired_file(tmp_path):
    """自证：往一份真的包拷贝里加一个没接线的 md，守卫必须红（否则它只是在念清单）。"""
    src = ROOT / "packs" / "elevator"
    dst = tmp_path / "packs" / "elevator"
    shutil.copytree(src, dst)
    found = set(_orphans(dst))
    assert found and all(f"elevator/{r}" in HUMAN_ONLY for r in found), \
        f"前置：elevator 的孤儿都应已按 包/路径 登记，未登记的是 {sorted(found)}"
    (dst / "knowledge" / "新加而没接线.md").write_text("# 标题\n\n正文\n", encoding="utf-8")
    assert "knowledge/新加而没接线.md" in _orphans(dst), "加了没接线的文件却抓不到，守卫空转"
    # 免检清单按**精确路径**：根目录那份是引擎生成的交付物，子目录里那份不是（第 18 轮 P3-2）
    (dst / "校对清单.md").write_text("# 校对清单\n", encoding="utf-8")
    assert "校对清单.md" not in _orphans(dst), "根目录那份生成物被误当成孤儿"
    (dst / "knowledge" / "校对清单.md").write_text("# 放错地方的那份\n", encoding="utf-8")
    assert "knowledge/校对清单.md" in _orphans(dst), \
        "按基名免检会连带放过子目录里那份没接线、引擎也永不注入的文件"


def test_the_engine_wiring_check_needs_both_names_in_one_function():
    """自证：对调两对 `文件 ↔ 切片` 必须红（第 16 轮复核 P3-11 的洞就在这）。"""
    apart = ("def load():\n    return file_text('knowledge/topics.md')\n\n"
             "def render():\n    return hooks_slice\n")
    assert _bodies_with(apart, "topics.md", "hooks_slice") == [], \
        "两个名字分处两函数也算接线 —— 那对调两对就不会红，正是被证伪的那条判据"
    together = "def load():\n    return slice_of(file_text('knowledge/topics.md')), hooks_slice\n"
    assert _bodies_with(together, "topics.md", "hooks_slice") == ["load"], "同函数体要算"
    in_class = ("class Pack:\n"
                "    def a(self):\n        return 'knowledge/topics.md'\n"
                "    def b(self):\n        return hooks_slice\n")
    assert _bodies_with(in_class, "topics.md", "hooks_slice") == [], "类体不算一处接线"
