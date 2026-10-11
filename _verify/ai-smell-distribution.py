# -*- coding: utf-8 -*-
"""A4 半步 + B-2：拿 generated/ 现成产物跑人味分与 L2 形态六信号的分布。

为什么需要这一步：A4 的原话是"拿 generated/ 现成产物跑分数分布，再决定 ai_smell
进不进回炉"。老产物的 `result.json` 里 `ai_tells` 全是 null —— 人味分是
2026-09-23 才落的功能（2d6b54d），产物都是在那之前生成的。所以分数分布不能直接
读落盘字段，要用**当前引擎对 sections 重跑**。

B-2（2026-10-12）补形态这一半：`check.ai_tells.shape`（L2 六信号）自 2026-10-12
起随产物落盘（真模型实测那批是第一批），而 no_emotion / idiom_density 要不要升
tell 的拍板前置就是"先看 L2 分布"—— 没有工具就永远没有数据。所以本工具现在：

  · 产物带 `check.ai_tells.shape` → **直读落盘**（分数与形态出自生成期同一次
    计算，不重算 —— 重算出的 shape 永远等于落盘值（shape_metrics 是纯统计），
    直读省掉的是"对每一篇都把引擎再跑一遍"的无谓功）；
  · 其余（ai_tells 为 null，或只有 score 没有 shape 的中间形态，如 20260924
    那篇）→ 照旧**当前引擎重跑**，分数与形态都是现算 —— 不混着读，避免
    "落盘的分数 + 现算的形态"这种两本账拼一行。

跑法：python _verify/ai-smell-distribution.py [-v]
  -v 打印每篇的命中明细（默认只出分布汇总）。

⚠ 这不是校准本身：校准要回答"多少分算 AI 味重"，那是人的判断。
   本工具只把分布摆出来，并且**按包的 tell 声明跑** —— 没声明 tell 的包
   分数恒 100（没测 ≠ 满分，那种包不进分布，单独计数；直读的产物自带
   落盘 ai_tells，说明生成期包声明过，不再按今天的 yaml 复查）。
   形态六信号是"只展示不设门"的 L2 指标（ai_tells.py 顶部有完整论证），
   这张表同样只供人看，不进任何门槛。
"""
from __future__ import annotations

import json
import sys
import unicodedata
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.ai_tells import AITells, score_of, shape_metrics      # noqa: E402
from app.knowledge import Pack, list_packs                     # noqa: E402

VERBOSE = "-v" in sys.argv

#: 产物发现根。仓库 `generated/` 之外，真模型实测的沙盒数据目录镜像
#: （`_verify/_realchain/data`，gitignored —— 别的机器上不存在，glob 空手而归
#: 不是错误）也数进来：20261012 那批真模型产物是第一批带落盘 shape 的，
#: 形态分布主要靠它们供数。
ARTIFACT_ROOTS = (ROOT / "generated",
                  ROOT / "_verify/_realchain/data/generated")

#: L2 六信号的展示行。clause_len 本身是个小分布（n/p50/p90/gt30），"每信号
#: min/p50/p90" 对它就是对三个分量各来一遍 —— 没法对一个小分布再套一遍百分位。
SHAPE_ROWS: tuple[tuple[tuple | str, str], ...] = (
    ("mattr", "词汇多样性MATTR"),
    ("trigram_repeat", "实词3gram重复"),
    (("clause_len", "p50"), "分句长中位(字)"),
    (("clause_len", "p90"), "分句长p90(字)"),
    (("clause_len", "gt30"), "分句>30字占比"),
    ("particles_per_100", "语气词每百字"),
    ("funcword_entropy", "虚词熵(bit)"),
    ("idioms_per_1000", "成语每千字"),
)

#: 成语每千字直方的桶宽。量级参照：250 字的稿子里出现 1 个成语 ≈ 4/千字，
#: 2 一档能看出「0 堆积」之外有没有第二座山；数据变大再调这个数。
IDIOM_BUCKET = 2


def discover(roots) -> list[Path]:
    """各根下的 `*/*/result.json`（generated/<日期>/<作业>/result.json）。"""
    out: list[Path] = []
    for root in roots:
        out += sorted(Path(root).glob("*/*/result.json"))
    return out


def _show(rf: Path) -> str:
    """报表里显示的路径：仓库相对优先，别的根（测试夹具）显示原样。"""
    try:
        return str(rf.relative_to(ROOT))
    except ValueError:
        return str(rf)


def stored_block(d: dict) -> dict | None:
    """落盘的 `check.ai_tells`：带 shape 的才算新产物，其余一律 None（走重跑）。"""
    chk = d.get("check")
    if not isinstance(chk, dict):
        return None
    at = chk.get("ai_tells")
    if isinstance(at, dict) and isinstance(at.get("shape"), dict):
        return at
    return None


def stored_row(d: dict, rel: str) -> dict | None:
    """新产物直读：分数、命中、形态全部取落盘值。"""
    at = stored_block(d)
    if at is None:
        return None
    hits = []
    for h in (at.get("hits") or []):
        if isinstance(h, dict) and h.get("id"):
            hits.append((h["id"], h.get("severity"), h.get("count")))
    return {"path": rel, "pack": d.get("pack") or "?",
            "score": at.get("score"), "hits": hits,
            "shape": at["shape"], "src": "直读"}


def rescan_row(d: dict, rel: str, tells: AITells) -> dict:
    """旧产物重跑：当前引擎对 sections 现算分数与形态（与旧版行为一致）。"""
    sections = d.get("sections") or []
    hits = tells.scan(sections)
    return {"path": rel, "pack": d.get("pack") or "?",
            "score": score_of(hits),
            "hits": [(h.id, h.severity, h.count) for h in hits],
            "shape": shape_metrics(sections, tells.idioms),
            "src": "重跑"}


def shape_value(shape, key) -> float | None:
    """按 SHAPE_ROWS 的键（"mattr" 或 ("clause_len", "p50")）取出数值；没有 → None。"""
    if isinstance(key, tuple):
        shape = (shape or {}).get(key[0]) if isinstance(shape, dict) else None
        key = key[1]
    v = shape.get(key) if isinstance(shape, dict) else None
    return v if isinstance(v, (int, float)) else None


def pctl(values: list, q: float):
    """最近秩百分位 —— 与分数分布同一个口径（int(n*q) 取秩），十几二十个数够准。"""
    s = sorted(values)
    return s[min(len(s) - 1, int(len(s) * q))]


def agg_signal(values: list) -> dict:
    """非 None 值的 min/p50/p90；None（没测）单独计数 —— 没测不能冒充 0
    （与 ai_tells.shape 把"没测"落 null、界面画「—」是同一条规矩）。"""
    got = [v for v in values if v is not None]
    if not got:
        return {"n": 0, "none": len(values), "min": None, "p50": None, "p90": None}
    return {"n": len(got), "none": len(values) - len(got),
            "min": min(got), "p50": pctl(got, .5), "p90": pctl(got, .9)}


def idioms_hist(values: list, width: int = IDIOM_BUCKET) -> list[tuple[int, int]]:
    """成语每千字的直方分桶（width 一档，从最高桶往 0 排）。None 不进桶。"""
    got = [v for v in values if v is not None]
    if not got:
        return []
    top = int(max(got) // width) * width
    buckets = Counter(int(v // width) * width for v in got)
    return [(lo, buckets.get(lo, 0)) for lo in range(top, -1, -width)]


def _disp(s: str) -> int:
    """终端显示宽（CJK 记 2），报表对齐用。"""
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in s)


def _fmt(v) -> str:
    return "—" if v is None else f"{v:g}"


def main(roots=None) -> None:
    roots = list(roots) if roots is not None else list(ARTIFACT_ROOTS)
    packs = {i.name: Pack(ROOT, i.name) for i in list_packs(ROOT)}
    tell_cache: dict[str, tuple[AiTells | None, bool]] = {}

    def tell_for(pack_name: str) -> tuple[AiTells | None, bool]:
        """(检测器, 该包是否声明了 tell)。没声明 → 分数恒 100，不进分布。"""
        if pack_name not in tell_cache:
            pk = packs.get(pack_name)
            data = pk.ai_tells_data() if pk else None
            declared = bool(data and (data.get("strong") or data.get("weak")))
            tell_cache[pack_name] = (AITells(data) if declared else None, declared)
        return tell_cache[pack_name]

    rows: list[dict] = []
    skipped_no_tell: Counter[str] = Counter()
    for rf in discover(roots):
        rel = _show(rf)
        try:
            d = json.loads(rf.read_text(encoding="utf-8"))
        except Exception as e:                          # noqa: BLE001
            print(f"读不了 {rel}：{e}")
            continue
        row = stored_row(d, rel)                        # 新产物：直读落盘
        if row is None:
            # 旧产物：照旧当前引擎重跑
            sections = d.get("sections") or []
            if not sections:
                continue
            pack_name = d.get("pack") or "?"
            tells, declared = tell_for(pack_name)
            if not declared:
                skipped_no_tell[pack_name] += 1
                continue
            row = rescan_row(d, rel, tells)
        rows.append(row)

    if not rows:
        print("没有可跑的产物（generated/ 下没有带 sections 且包声明了 tell 的 result.json）")
        return

    direct = sum(1 for r in rows if r["src"] == "直读")
    way = (f"直读落盘 {direct} 篇 · 引擎重跑 {len(rows) - direct} 篇"
           if direct else "全部按当前引擎重跑")
    scores = sorted(r["score"] for r in rows)
    n = len(scores)
    by_pack: dict[str, list[int]] = {}
    for r in rows:
        by_pack.setdefault(r["pack"], []).append(r["score"])

    print(f"产物 {n} 篇（{way}；包的 tell 声明决定跑不跑）")
    print(f"分数分布：min {scores[0]} · p10 {pctl(scores, .1)} · p25 {pctl(scores, .25)} · "
          f"中位 {pctl(scores, .5)} · p75 {pctl(scores, .75)} · p90 {pctl(scores, .9)} · max {scores[-1]}")
    print(f"平均 {sum(scores) / n:.1f}")
    print("\n按包（篇数 / 中位 / min~max）：")
    for pk, ss in sorted(by_pack.items(), key=lambda kv: -len(kv[1])):
        ss = sorted(ss)
        print(f"  {pk:12} {len(ss):3} 篇  中位 {ss[len(ss) // 2]:3}  "
              f"{ss[0]}~{ss[-1]}")
    if skipped_no_tell:
        print("\n未声明 tell 的包（没测 ≠ 满分，不进分布）："
              + "、".join(f"{k} {v} 篇" for k, v in skipped_no_tell.items()))
    # 直方图：10 分一档，看堆积形状比单看均值有用
    buckets = Counter(s // 10 * 10 for s in scores)
    print("\n直方图（10 分一档）：")
    for lo in range(100, -1, -10):
        c = buckets.get(lo, 0)
        print(f"  {lo:3}-{lo + 9:3} {'█' * c}{'' if c else '·'} {c}")

    # ── L2 形态六信号（B-2）：no_emotion/idiom_density 升不升 tell，先看分布 ──
    print("\nL2 形态六信号（min / p50 / p90；— = 没测，不冒充 0；只展示不设门）：")
    for key, label in SHAPE_ROWS:
        a = agg_signal([shape_value(r["shape"], key) for r in rows])
        pad = " " * max(1, 18 - _disp(label))
        if a["n"]:
            tail = f"（另没测 {a['none']} 篇）" if a["none"] else ""
            print(f"  {label}{pad}{_fmt(a['min'])} / {_fmt(a['p50'])} / {_fmt(a['p90'])}  {tail}")
        else:
            print(f"  {label}{pad}没测（{a['none']} 篇）")
    chars = agg_signal([shape_value(r["shape"], "chars") for r in rows])
    if chars["n"]:
        print(f"  （分母：念出字数 min {_fmt(chars['min'])} / 中位 "
              f"{_fmt(chars['p50'])} / p90 {_fmt(chars['p90'])}）")
    id_vals = [shape_value(r["shape"], "idioms_per_1000") for r in rows]
    hist = idioms_hist(id_vals)
    if hist:
        none_n = agg_signal(id_vals)["none"]
        print(f"\n成语每千字直方（{IDIOM_BUCKET} 一档"
              + (f"；没测 {none_n} 篇不在桶里" if none_n else "") + "）：")
        for lo, c in hist:
            print(f"  {lo:>3}-{lo + IDIOM_BUCKET - 1:>3} {'█' * c}{'' if c else '·'} {c}")

    if VERBOSE:
        print("\n逐篇（低分在前，看命中构成）：")
        for r in sorted(rows, key=lambda r: r["score"])[:20]:
            hits = "、".join(f"{t}/{s}×{c}" for t, s, c in r["hits"]) or "无命中"
            print(f"  {r['score']:3}  {r['pack']:10} {r['path']}  {hits}")


if __name__ == "__main__":
    main()
