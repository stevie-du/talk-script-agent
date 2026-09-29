# -*- coding: utf-8 -*-
"""A-9 挖词管线：模型产物 vs 真人对照稿 → **超额短语**候选词表（调研报告-20260928 §3.4）。

为什么要有这一步
----------------
现有词表是**手工收词**（别人清单里抄 + 自己攒），它有两个改不掉的毛病：
① 只能覆盖"已经被人命名过"的 tell，模型换代后新长出来的口癖没人知道；
② 无法回答"这个词在我们的稿子里到底多常见"——阈值全靠拍。
antislop 论文（arXiv:2510.15061，部分 slop 模式超人类基线 1000×）、EQ-Bench Slop Score、
Kobak 2025（Science Advances 的"超额词汇"）三家是同一套方法论：
**拿目标模型产物与真人语料做频率比，超额的就是候选**。这是唯一能自我更新、抗模型漂移的收词路径。

跑法
----
    python _verify/slop-mine.py --b <真人稿目录>            # A 默认取 generated/ 的产物正文
    python _verify/slop-mine.py --a A目录 --b B目录 --ratio 8 --min-count 5
    python _verify/slop-mine.py --b ... --include-mock      # 默认**排除**打了 mock 标记的夹具

输出：候选短语 + 两侧每千字频率 + 频率比 + 建议的下一步（过 A-3 的哪道闸）。
**本工具只出候选，不自动改任何词表** —— 把统计量直接变成判据，正是 L2 那一层被否决的理由。

三处刻意的设计，改之前先读懂（都有测试钉住：`tests/test_slop_mine.py`）：
  · **同一份稿子只算一份**（`dedupe_docs`）：本机 `generated/` 的 88/102 篇没有 `mock` 键
    （那字段是后来才加的），其中 79 篇是同一份夹具稿。按"篇"计数时它每一句都会变成
    "绝对超额"，候选表全是那一份稿子的滑动窗口 —— 频率比的前提是分母按篇去重。
  · **极大串**（`_maximal`）：滑动窗口会产出互不为子串、其实同属一句话的一批候选。
  · **没有 --b 直接退出**：自己跟自己比必然 ratio≈1，那份"零超额"是假的干净。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

#: n-gram 的字数范围。2 起（单字不算"短语"），10 止（再长就是句子模板，
#: 对词表没用 —— 但比 8 长一点才够：真实产物里"同一句话反复出现"的情形，
#: 8 字上限会让一整串窗口都卡在边界上被判成"极大"，实测候选表全是 8 字碎片）。
MIN_N, MAX_N = 2, 10
#: 候选必须是纯汉字/字母串：带标点或数字的 gram 贴进 yaml 就是永远匹配不到的死条目。
_CLEAN = re.compile(r"[一-鿿A-Za-z]")
_SPLIT = re.compile(r"[^一-鿿A-Za-z0-9]+")
_PLACEHOLDER = re.compile(r"\{\{[^}]*\}\}|\[画面：[^\]]*\]|\*\*")


def artifact_text(path: Path, allow_mock: bool = False) -> str:
    """一份 `result.json` → 整篇口播正文（拿不到给空串，不猜）。

    ⚠ `mock` 只在**新产物**里有（老产物根本没这个键），所以"排掉夹具"这件事
    只做到了"排掉带标记的那些"；剩下的靠 `dedupe_docs` 按篇去重兜住。
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    if not allow_mock and data.get("mock"):
        return ""
    return "\n".join(str(s.get("text", "")) for s in (data.get("sections") or []))


def texts_from_dir(d: Path) -> list[str]:
    """真人对照稿目录：每个 `.md`/`.txt` 是一份稿子。"""
    out: list[str] = []
    for f in sorted(d.rglob("*.md")) + sorted(d.rglob("*.txt")):
        try:
            out.append(f.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            continue
    return out


def dedupe_docs(docs: list[str]) -> list[str]:
    """**同一份稿子重复 N 次只算一份**（保序）。频率比量的是"篇"，不是"次"。"""
    return list(dict.fromkeys(d for d in docs if d and d.strip()))


#: 数 gram 时**多算一层**（`MAX_N + 1`）：极大串判据要看得到候选的扩一字形式，
#: 否则长度封顶在 8 的那一批窗口串会人人"极大"，滑动窗口噪声又回来了。
#: 多出来的那一层只用于判据，不作为候选输出（见 `mine()` 里的长度过滤）。
_COUNT_MAX = MAX_N + 1


def grams(texts: list[str]) -> tuple[Counter, int, dict]:
    """→ ({n-gram: 次数}, 总字数, {gram: {(侧, 相邻字)}})。

    第三项是给极大串判据用的：只记**实际出现过**的左右邻字，
    所以不必拿两万个汉字逐个去试（那样每条候选要查四万次）。
    """
    c: Counter = Counter()
    ext: dict[str, set] = defaultdict(set)
    total = 0
    for t in texts:
        # 段内连续才算 gram：跨段拼出来的串不是任何一句里真实出现过的短语
        for run in _SPLIT.split(_PLACEHOLDER.sub("", t)):
            total += len(run)
            for n in range(MIN_N, _COUNT_MAX + 1):
                for i in range(len(run) - n + 1):
                    g = run[i:i + n]
                    if not _is_clean(g):
                        continue
                    c[g] += 1
                    left, right = run[i - 1:i], run[i + n:i + n + 1]
                    if left and _is_clean(left):
                        ext[g].add(("l", left))
                    if right and _is_clean(right):
                        ext[g].add(("r", right))
    return c, total, ext


def _is_clean(chunk: str) -> bool:
    return bool(chunk) and all(_CLEAN.match(ch) for ch in chunk)


def _maximal(c: Counter, ext: dict, g: str) -> bool:
    """G 是不是**极大**的：它每一次出现都被更长的同一个 gram 覆盖，就不是新信息。

    为什么"更长的那个也得能当候选"才算覆盖：扩出来的串若已超过候选长度上限
    （`MAX_N`），它也进不了候选表 —— 这时再把 G 摘掉，整条短语就凭空消失了
    （实测：一句 9 字的话会因此**一个候选都不出**）。所以只在不报会丢信息时才收。

    另外这个判据真正发力之处是"同一短语在多份稿子里重复"：那种情况下
    G 与它的扩串**计数不同**（扩串还带着别处的不同上下文），G 被判极大、正常出候选；
    计数相同才说明 G 只是那个更长串的窗口碎片。
    """
    n = c.get(g, 0)
    for side, ch in ext.get(g, ()):
        longer = (g + ch) if side == "r" else (ch + g)
        if len(longer) <= MAX_N and c.get(longer, 0) == n:
            return False
    return True


def dedupe(rows: list[dict]) -> list[dict]:
    """候选表内部再收一遍：互不为子串（长词优先）—— yaml 的硬不变量。

    `_scan_words` 是"长词优先、不重叠"，同时收「普遍认为」与「专家普遍认为」
    会让短的那条永远取不到且计数虚高；`tests/test_aitells_alignment.py` 把这种
    写法判成缺陷。所以候选表在**给人贴之前**就先满足它，而不是让人贴完再被门禁挡。
    """
    rows = sorted(rows, key=lambda x: (-x["n"], -(x["ratio"] or 1e9), -x["a_count"]))
    kept: list[dict] = []
    for r in rows:
        if any(r["phrase"] in k["phrase"] for k in kept):
            continue
        kept.append(r)
    return kept


def mine(a_docs: list[str], b_docs: list[str], ratio: float, min_count: int) -> list[dict]:
    """频率比挖词：A 侧每千字频率 ÷ B 侧 ≥ `ratio` 且 A 侧至少出现 `min_count` 次。

    B 侧 0 次记成**绝对超额**（比值 `None`）—— 那正是 antislop 说的那一类：
    真人根本不写、模型特别爱写的口癖。
    """
    a, b = dedupe_docs(a_docs), dedupe_docs(b_docs)
    ac, atotal, aext = grams(a)
    bc, btotal, _ = grams(b)
    if not atotal or not btotal:
        return []
    out = []
    for g, n in ac.items():
        if len(g) > MAX_N:
            continue          # 多算的那一层只服务极大串判据，不作为候选输出
        if n < min_count or not _maximal(ac, aext, g):
            continue
        pa = n / atotal * 1000
        pb = bc.get(g, 0) / btotal * 1000
        r = (pa / pb) if pb else None
        if r is None or r >= ratio:
            out.append({"phrase": g, "n": len(g), "a_count": n, "a_per_1k": round(pa, 2),
                        "b_count": bc.get(g, 0), "b_per_1k": round(pb, 2),
                        "ratio": round(r, 1) if r is not None else None})
    return dedupe(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="频率比挖词（模型产物 vs 真人对照稿）")
    ap.add_argument("--a", default=None, help="产物目录（默认 generated/）")
    ap.add_argument("--b", default=None, help="真人对照稿目录（必填：没有分母就没有比值）")
    ap.add_argument("--ratio", type=float, default=8.0, help="超额倍数门槛（默认 8）")
    ap.add_argument("--min-count", type=int, default=5,
                    help="A 侧最少出现次数（默认 5；给 1 只是调试用 —— "
                         "一份稿子说过一次不是模型口癖）")
    ap.add_argument("--top", type=int, default=40, help="打印前 N 条（默认 40）")
    ap.add_argument("--include-mock", action="store_true",
                    help="把带 mock 标记的夹具产物也算进 A 侧（默认排除）")
    args = ap.parse_args(argv)

    if not args.b:
        print("缺少 --b <真人对照稿目录> —— 频率比**必须有分母**。\n"
              "拿产物跟自己比只会得到 ratio≈1 的「零超额」，那是假的干净。\n"
              "对照稿的攒法见 README 线 A 的 A6/A-7（yt-dlp + faster-whisper 出 ASR 文本）。")
        return 2
    bdir = Path(args.b)
    if not bdir.is_dir():
        print(f"--b 指向的目录不存在：{bdir}")
        return 2

    a_dir = Path(args.a) if args.a else ROOT / "generated"
    a_docs = [t for f in sorted(a_dir.rglob("result.json"))
              if (t := artifact_text(f, args.include_mock))]
    b_docs = texts_from_dir(bdir)
    if not a_docs:
        print(f"A 侧一篇都没有（目录：{a_dir}；mock 是否计入={args.include_mock}）")
        return 2
    if not b_docs:
        print(f"B 侧一份对照稿都没有（目录：{bdir}）—— 同样不做挖词，而不是报「零超额」")
        return 2

    a_uniq, b_uniq = len(dedupe_docs(a_docs)), len(dedupe_docs(b_docs))
    rows = mine(a_docs, b_docs, args.ratio, args.min_count)
    print(f"A 侧 {len(a_docs)} 篇（去重后 {a_uniq} 份）/ B 侧 {len(b_docs)} 篇"
          f"（去重后 {b_uniq} 份）→ 超额候选 {len(rows)} 条"
          f"（门槛：ratio ≥ {args.ratio:g} 且 A 侧 ≥ {args.min_count} 次）")
    for r in rows[:args.top]:
        rel = "绝对超额（真人侧 0 次）" if r["ratio"] is None else f"{r['ratio']}×"
        print(f"  {r['phrase']:<12} {r['n']}字  产物 {r['a_count']:>4} 次"
              f"（{r['a_per_1k']:>6}/千字）  真人 {r['b_count']:>3} 次"
              f"（{r['b_per_1k']:>6}/千字）  {rel}")
    if a_uniq < 20:
        print(f"\n⚠ 去重后 A 侧只有 {a_uniq} 份稿子 —— 频率比在这个样本量上没有解释力，"
              "候选表只能当线索看（同一份稿子反复生成不会让结论变可靠）。")

    print("\n下一步（本工具到此为止，**不自动改词表**）：")
    print("  1. 人挑候选 → 贴进 `packs/<行业>/ai_tells.yaml` 的 lexicon（一条一类，别混）；")
    print("  2. 跑 A-3 误报回归：PYTHONUTF8=1 .venv/Scripts/python.exe -m pytest "
          "tests/test_aitells_false_positive.py")
    print("     ⚠ 误报超线的条目**整条撤下**，不是抬 `WEAK_MIN` 或改 `thresholds`"
          "（那等于换一把更钝的尺子而报告看不出来）；")
    print("  3. 再跑产物分布复跑：PYTHONUTF8=1 .venv/Scripts/python.exe "
          "_verify/ai-smell-distribution.py")
    print("  4. 新增一类 tell（而不是往旧类里加词）时，三处同步：`AITells` + "
          "需求方案 §2.1 表 + 全包 yaml。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
