# -*- coding: utf-8 -*-
"""A4 半步：拿 `generated/` 现成产物跑人味分分布（定门槛的数据由它出，门槛本身等人定）。

为什么需要这一步：A4 的原话是"拿 generated/ 现成产物跑分数分布，再决定 ai_smell
进不进回炉"。产物的 `result.json` 里 `ai_tells` 全是 null —— 人味分是 2026-09-23
才落的功能（2d6b54d），产物都是在那之前生成的。所以分布不能直接读落盘字段，
要用**当前引擎对 sections 重跑**。

跑法：python _verify/ai-smell-distribution.py [-v]
  -v 打印每篇的命中明细（默认只出分布汇总）。

⚠ 这不是校准本身：校准要回答"多少分算 AI 味重"，那是人的判断。
   本工具只把分布摆出来，并且**按包的 tell 声明跑** —— 没声明 tell 的包
   分数恒 100（没测 ≠ 满分，那种包不进分布，单独计数）。
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.ai_tells import AITells, score_of                       # noqa: E402
from app.knowledge import Pack, list_packs                       # noqa: E402

VERBOSE = "-v" in sys.argv


def main() -> None:
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
    for rf in sorted(ROOT.glob("generated/*/*/result.json")):
        try:
            d = json.loads(rf.read_text(encoding="utf-8"))
        except Exception as e:                          # noqa: BLE001
            print(f"读不了 {rf.relative_to(ROOT)}：{e}")
            continue
        sections = d.get("sections") or []
        if not sections:
            continue
        pack_name = d.get("pack") or "?"
        tells, declared = tell_for(pack_name)
        if not declared:
            skipped_no_tell[pack_name] += 1
            continue
        hits = tells.scan(sections)
        rows.append({"path": str(rf.relative_to(ROOT)), "pack": pack_name,
                     "score": score_of(hits),
                     "hits": [(h.id, h.severity, h.count) for h in hits]})

    if not rows:
        print("没有可跑的产物（generated/ 下没有带 sections 且包声明了 tell 的 result.json）")
        return

    scores = sorted(r["score"] for r in rows)
    n = len(scores)
    by_pack: dict[str, list[int]] = {}
    for r in rows:
        by_pack.setdefault(r["pack"], []).append(r["score"])

    def pct(p: float) -> int:
        return scores[min(n - 1, int(n * p))]

    print(f"产物 {n} 篇（按当前引擎重跑；包的 tell 声明决定跑不跑）")
    print(f"分数分布：min {scores[0]} · p10 {pct(.1)} · p25 {pct(.25)} · "
          f"中位 {pct(.5)} · p75 {pct(.75)} · p90 {pct(.9)} · max {scores[-1]}")
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

    if VERBOSE:
        print("\n逐篇（低分在前，看命中构成）：")
        for r in sorted(rows, key=lambda r: r["score"])[:20]:
            hits = "、".join(f"{t}/{s}×{c}" for t, s, c in r["hits"]) or "无命中"
            print(f"  {r['score']:3}  {r['pack']:10} {r['path']}  {hits}")


if __name__ == "__main__":
    main()
