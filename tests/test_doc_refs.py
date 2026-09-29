# -*- coding: utf-8 -*-
"""入库的文档不许指向**不在版本库里**的文件或与代码对不上的常量（2026-09-27）。

这一类漂移的共同点是「读起来一切正常」：Markdown 里的引用不会报解析错误，
代码里的字符串常量也不会。所以没有任何东西会提醒，只有照着引用去查的人
才会发现「那东西不存在」—— 而他往往在很久以后、在另一台机器上。

本文件守三族（都是审查里实证过的）：

1. **死引用**：`docs/自动更新方案.md` 逐条引用 `PACKAGING.md` 与
   `UI-LAYOUT-RULES` 的第〇·五/六/十一节，而整个 `.workbuddy-ai/` 被
   `.gitignore` 排除 —— 主仓库能点开是因为本地有，**任何一次干净克隆或
   `git worktree` 里全是死链**。（已修：`!` 反选那四份规则文档入库。）
   判据直接问 git：这条引用指向的路径在 `git ls-files` 里吗？

2. **常量漂移**：`docs/技能包系统方案.md` 把导入包的来源标记写成
   `.packimport.json`，而代码里是 `IMPORT_MARK = ".imported.json"`
   （`app/packimport.py:62`）。同名不同值 —— 照文档去包里找那个文件，
   找不到。

3. **格式契约漂移**：`docs/知识库内容规范.md` 教的 `ideas.md` 格式是
   `编号. 标题角度`，而注入契约要求 `编号. 标题 — 钩子类型`
   （`tests/test_pack_injection.py` 按后者钉着）。照文档产出的包会被门禁挡下，
   而写文档的人那边**什么提示都收不到**。

**为什么用 git 判"在不在版本库里"而不是"文件在不在盘上"**：
这正是本文件的靶心 —— 死引用的定义就是「本机有、仓库里没有」。
只看 `Path.exists()` 的话，主仓库上全绿，而问题原封不动地留着。
git 不在时整条跳过（而不是假装通过），并在 skip 原因里说清。
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# 入库的文档：只查这些（`_verify/legacy/` 明确标注"不要直接运行"，不在其列）
DOCS = sorted(
    [ROOT / "README.md"]
    + list((ROOT / "docs").glob("*.md"))
    + [ROOT / "config.example.yaml"]
)


def _repo_files() -> set[str] | None:
    """仓库里"会被带上走"的文件：已跟踪的 + 未跟踪但**没被 ignore** 的。

    为什么不止 `git ls-files`：那条只列**已跟踪**的，于是
    「刚 `.gitignore` 反选出来、还没 `git add` 的那几份规则文档」会被判成
    死引用 —— 守卫把"还没提交"说成"永远不在库里"，而那正是本条要防的误报
    （实测踩到：`.workbuddy-ai/` 反选之后立刻红，改完 .gitignore 反而红了）。
    问 git 两次合起来才是那个问题的真答案：
    `ls-files` = 已经进了版本库；`ls-files --others --exclude-standard` =
    没进但不会被排除（也就是 `git add .` 之后就会进去）。

    git 不可用时返回 None —— 调用方**跳过**而不是放过：
    「跑不到判据」和「判据说没问题」必须读得出来不一样。
    """
    exe = shutil.which("git")
    if not exe:
        return None

    def run(*args: str) -> str:
        # ⚠ 必须显式 encoding="utf-8"：默认走 locale 编码，而本仓的文档名
        #   大量是中文（docs/知识库内容规范.md …）。在中文 Windows 上那是 cp936，
        #   git 的 `-z` 输出（`ls-files` 的路径是 UTF-8 字节）会被解成乱码 ——
        #   于是**所有中文文件名都变成"不在版本库里"**，实测四条噪声全是 docs/ 互链。
        #   一条会被 locale 骗到的守卫，在别人机器上会红得莫名其妙。
        return subprocess.run(
            [exe, "-C", str(ROOT), *args],
            capture_output=True, encoding="utf-8", errors="replace",
            timeout=60, check=True,
        ).stdout

    try:
        out = run("ls-files", "-z")
        other = run("ls-files", "-z", "--others", "--exclude-standard")
    except (subprocess.SubprocessError, OSError):
        return None
    return {p for p in (out + "\0" + other).split("\0") if p}


TRACKED = _repo_files()
needs_git = pytest.mark.skipif(TRACKED is None, reason="git 不可用，判据跑不到")


# ── 1. 文档引用的每个文件都得在版本库里 ───────────────────────────
# 形如 `PACKAGING.md` / `docs/UI视觉规范.md` / `app/pipeline.py:519-528`。
#
# ⚠ 只对**看得出来的仓库相对路径**下判据。裸文件名（`skill.yaml` /
#   `patterns/anti-ai-smell.md` 那种）大量出现在**包内文档**里 —— 那些路径
#   相对的是包目录（`packs/elevator/`），仓库根下当然找不到。实测第一版
#   把这类全报出来，十几条噪声里没有一条是真的 —— 一条会喊狼来了的守卫
#   比没有守卫更坏：下一个人会去把它注掉，连真的那几条一起丢掉。
#
# 判定口径（两类都放过，不报）：
#   · 带目录但不以已知仓库目录开头     → 包内相对路径
#   · 裸文件名且在根与 docs/ 下都找不到 → 同上
REPO_DIRS = ("app/", "docs/", "tests/", "desktop/", "packs/", "_verify/", "intel/",
             "examples/", "data/", "generated/", ".workbuddy-ai/")
# ⚠ 扩展名的交替**必须长的在前**：`js` 写在 `json` 前面时正则会先匹上
#   `desktop/package.js` 就停 —— 而盘上是 `desktop/package.json`。实测踩到：
#   守卫报「自动更新方案引用了 desktop/package.js」，去查发现那行写得好好的
#   是 `.json`。**一条自带 bug 的守卫比没有守卫更坏**：它会让人去改正确的文档。
FILE_REF = re.compile(r"(?<![\w/.-])((?:[\w.-]+/)*[\w.-]+\.(?:json|ya?ml|mjs|md|py|js|exe))")

# 合法的例外，每条写清理由 —— 白名单不写理由就等于把守卫关掉。
#   · config.yaml / generated/**   运行态路径（前者含明文 Key 被 .gitignore，
#     后者是产物目录），文档提它们是告诉用户"文件会在哪儿"，不是引用源码。
#   · app/export_skill.py         **已删除**的文件，README 在交代它何时下线的。
#     历史交代点名旧路径是有用的，所以放过而不是逼人改掉这段。
#   · _verify/_*.js               一次性探针（.gitignore 的 `_verify/_*` 规则），
#     方案文档里点名它们当"当时怎么量的"的记录，性质同上。
ALLOWED_MISSING = {
    "config.yaml": "运行态配置（含明文 Key，设计上不入库）",
    "generated/index.json": "产物目录（.gitignore），运行态生成",
    "app/export_skill.py": "已删除的文件，README 在交代它何时下线",
    "intel/pull_policy.py": "本机一次性抓取脚本（整个 intel/ 被 ignore，是政策语料）",
}


def _is_known_runtime_ref(rel: str) -> bool:
    return any(rel == k or rel.endswith("/" + k) for k in ALLOWED_MISSING)


def test_documented_paths_are_actually_in_the_repo():
    """文档提到的每个**仓库相对路径**都必须在版本库里（在盘上不算数）。"""
    if TRACKED is None:
        pytest.skip("git 不可用，这条判据跑不到（而不是当作通过）")
    dangling = []
    for doc in DOCS:
        text = doc.read_text(encoding="utf-8")
        for m in FILE_REF.finditer(text):
            rel = m.group(1).replace("\\", "/")
            if rel in TRACKED or _is_known_runtime_ref(rel):
                continue
            if rel.startswith("_verify/_") or "/generated/" in rel:
                continue
            if "/" in rel:
                # 带目录：只在它确实是仓库相对路径时才下判据（包内路径放过）
                if not any(rel.startswith(d) for d in REPO_DIRS):
                    continue
            else:
                # 裸文件名：版本库里记的是**带目录**的形式（`docs/xxx.md`），
                # 而文档里互链写的是裸名（`xxx.md`）—— 两种形式都要认，
                # 否则 docs/ 之间的互链会全被判成死链（实测四条噪声全是这个）。
                if rel in TRACKED or f"docs/{rel}" in TRACKED:
                    continue
                # 根与 docs/ 下都找不到 → 是包内相对路径或示例名，放过
                if not (Path(ROOT, rel).exists() or (ROOT / "docs" / rel).exists()):
                    continue
            if "Setup" in rel or rel.startswith(("TalkScript ", "example", "xxx")):
                continue
            dangling.append(f"{doc.relative_to(ROOT)} 引用了 {rel}（不在版本库里）")
    assert not dangling, (
        "文档指向了不在版本库里的文件 —— 主仓库能点开是本机有，干净克隆里全是死链：\n"
        + "\n".join(dangling))


@needs_git
def test_the_rule_docs_the_update_plan_links_to_are_tracked():
    """`docs/自动更新方案.md` 逐条引用的那两份必须真的入库。

    这一条是上一条的**定点**复核：上一条要靠正则从散文里抠出路径，
    「UI-LAYOUT-RULES 第〇·五节」那种**不带扩展名**的引用它抓不到。
    """
    plan = (ROOT / "docs" / "自动更新方案.md").read_text(encoding="utf-8")
    assert "PACKAGING.md" in plan, "夹具前提失真：更新方案里已经不再引用 PACKAGING.md 了"
    for name in ("PACKAGING.md", "UI-LAYOUT-RULES"):
        # 比**文件名主体**：文档里写的是「UI-LAYOUT-RULES 第〇·五节」（不带扩展名），
        # 而盘上是 `UI-LAYOUT-RULES.md` —— 按全名 endswith 匹配会永远落空，
        # 那是一条"量不到东西"的红（实测踩到：报「不在版本库里」而其实在）。
        found = [p for p in TRACKED if Path(p).name in (name, name + ".md")]
        assert found, (
            f"更新方案引用了 {name}，但它不在版本库里 —— "
            f"任何一次干净克隆里那条引用都是死链")
    # 被引的那几节也得真的在（索引变了就报出来，别让引用悄悄指空）
    rules = (ROOT / ".workbuddy-ai" / "memory" / "UI-LAYOUT-RULES.md")
    if rules.exists():
        body = rules.read_text(encoding="utf-8")
        for sec in ("〇·五", "六、", "十一、"):
            assert sec in body, f"UI-LAYOUT-RULES 里已经没有「{sec}」这一节了（引用指空）"


# ── 2. 文档里抄的常量与代码一致 ───────────────────────────────────
def test_documented_import_mark_matches_the_code():
    """`docs/技能包系统方案.md` 写的来源标记文件名 = `IMPORT_MARK` 的真值。

    原先文档写 `.packimport.json`、代码是 `.imported.json` —— 同名不同值。
    照文档去包里找那个文件的人找不到，而文档不会告诉他自己写错了。
    """
    src = (ROOT / "app" / "packimport.py").read_text(encoding="utf-8")
    m = re.search(r'IMPORT_MARK\s*=\s*"([^"]+)"', src)
    assert m, "app/packimport.py 里已经找不到 IMPORT_MARK 的定义了（改名了？）"
    mark = m.group(1)
    doc = (ROOT / "docs" / "技能包系统方案.md").read_text(encoding="utf-8")
    assert mark in doc, (
        f"代码里的来源标记是 {mark}，但 docs/技能包系统方案.md 里没有这个名字 —— "
        f"照文档去包里找那个文件的人会找不到")
    # 反向：文档不许还留着别的 .xxx.json 当来源标记（那是漂移的另一半）
    for wrong in re.findall(r"`(\.[\w.-]+\.json)`", doc):
        assert wrong == mark, (
            f"文档里还有一个 {wrong}，而代码用的是 {mark} —— 两处各说各的")


# ── 3. 文档教的格式 = 注入契约要求的格式 ─────────────────────────
def test_knowledge_spec_teaches_the_real_ideas_format():
    """`知识库内容规范.md` 教的 `ideas.md` 行格式必须带钩子类型。

    注入契约（`tests/test_pack_injection.py` 钉着）是 `编号. 标题 — 钩子类型`：
    破折号后必须是 `patterns/hooks.md` 那 10 类之一。文档原来写的是
    `编号. 标题角度` —— **照着写产出的包会被门禁挡下**，而写规范的人收不到
    任何提示：格式不是产物，是文档里的一句话。
    """
    spec = (ROOT / "docs" / "知识库内容规范.md").read_text(encoding="utf-8")
    i = spec.find("### knowledge/ideas.md")
    assert i >= 0, "知识库规范里已经找不到 knowledge/ideas.md 那一节了"
    seg = spec[i: i + 1200]
    assert "钩子类型" in seg, "ideas.md 那一节没提钩子类型（照着写必然不合约）"
    assert "编号. 标题 — 钩子类型" in seg, \
        "ideas.md 那一节没给出带钩子类型的行长什么样（只说了要有这个词）"
    # 输出三那一段是**照着做的人真正会抄的**那段，格式也得对。
    # ⚠ 切片要从**收尾那道横线**之后开始：段首本身是「━━━ 输出三：【…】━━━」，
    #   按 `find("━━━")` 会落在开头那道横线上，量出来的只有标题那一行
    #   （实测踩到：量到 `'输出三：【knowledge/ideas.md】'`，正文一个字没读）。
    out3 = spec[spec.find("输出三"):]
    cut = out3.find("━━━", out3.find("】") + 1)
    out3 = out3[cut:] if cut > 0 else out3[:400]
    assert "钩子类型" in out3, "输出三那段没提钩子类型（那才是被照抄的那一段）"
