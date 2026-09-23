# -*- coding: utf-8 -*-
"""A-7 朱雀对照的准备材料：抽样清单 + 记录模板。

朱雀（matrix.tencent.com/ai-detect）是国内创作者侧事实标准，A-7 要量的是
**L1 人味分与朱雀判定的相关性**：相关性差 = 我们的 tell 集有盲区。

人工只需要做三件事：
  1. 跑 `python _verify/zhique-sample.py` 拿到抽样清单（JSON，含每篇正文）；
  2. 把每条正文贴进朱雀，记下它的判定（AI 概率 / 是否判 AI）；
  3. 把判定结果按模板写进 `data_dir/zhique-audit.json`（路径见下），
     跑同一个命令加 `--score` 出相关性报告。

红线（需求方案 §2.10 A-7）：**不做门槛、不做自动化** —— 纯人工对照。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "_verify" / "zhique-sample.json"
AUDIT = ROOT / "data" / "zhique-audit.json"

# 分层抽样：低分 / 中位 / 满分各取几篇，再加“带 {{待补}}”的样本。
# 只抽 elevator（唯一声明了 tell 的包），每档按分数排。
LAYERS = [("low", 4), ("mid", 4), ("high", 4), ("placeholder", 2)]


def full_text(d: dict) -> str:
    return "\n\n".join(s.get("text", "") for s in (d.get("sections") or []))


def main() -> None:
    sys.path.insert(0, str(ROOT))
    from app.ai_tells import AITells, score_of, _PLACEHOLDER_RE
    from app.knowledge import Pack

    pk = Pack(ROOT, "elevator")
    tells = AITells(pk.ai_tells_data())
    rows: list[dict] = []
    for rf in sorted(ROOT.glob("generated/*/*/result.json")):
        try:
            d = json.loads(rf.read_text(encoding="utf-8"))
        except Exception:                                   # noqa: BLE001
            continue
        secs = d.get("sections") or []
        if not secs:
            continue
        text = full_text(d)
        hits = tells.scan(secs)
        rows.append({"path": str(rf.relative_to(ROOT)),
                     "score": score_of(hits),
                     "has_placeholder": bool(_PLACEHOLDER_RE.search(text)),
                     "chars": len(text),
                     "text": text})

    if not rows:
        print("generated/ 下没有可抽的产物")
        return

    rows.sort(key=lambda r: r["score"])
    picked: list[dict] = []
    for name, n in LAYERS:
        if name == "low":
            pool = [r for r in rows if r["score"] < 95]
        elif name == "mid":
            pool = [r for r in rows if r["score"] == 96]
        elif name == "high":
            pool = [r for r in rows if r["score"] >= 99]
        else:
            pool = [r for r in rows if r["has_placeholder"]]
        # 每档内再按 chars 打散，避免抽到一连串同题稿
        pool = sorted(pool, key=lambda r: -r["chars"])
        step = max(1, len(pool) // max(1, n))
        for r in pool[::step][:n]:
            picked.append({"layer": name, **r})

    OUT.write_text(json.dumps(picked, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"已抽出 {len(picked)} 篇 → {OUT.relative_to(ROOT)}")
    for p in picked:
        print(f"  [{p['layer']:11}] L1={p['score']:3} {p['chars']:4}字  {p['path']}")

    print(f"""
━━━ 人工三步 ━━━
1. 打开 {OUT.relative_to(ROOT)}，把每篇的 text 贴进 matrix.tencent.com/ai-detect
2. 把判定按这个模板写进 {AUDIT.relative_to(ROOT)}（没有就新建）：

   [
     {{"path": "generated/.../result.json", "zhique": "human", "note": ""}},
     ...
   ]

   zhique 取值：human（判人类）/ ai（判 AI）/ unsure（拿不准）
3. 再跑一次 `python _verify/zhique-sample.py --score` 出相关性报告
""")

    if "--score" in sys.argv:
        if not AUDIT.exists():
            print(f"还没看到 {AUDIT.relative_to(ROOT)} —— 先按上面模板把朱雀判定写进去")
            return
        verdict = {a["path"]: a["zhique"] for a in json.loads(AUDIT.read_text(encoding="utf-8"))}
        have = [p for p in picked if p["path"] in verdict]
        if not have:
            print("判定文件里没有一篇能对上抽样清单的 path")
            return
        ai = [p["score"] for p in have if verdict[p["path"]] == "ai"]
        hu = [p["score"] for p in have if verdict[p["path"]] == "human"]
        un = [p["score"] for p in have if verdict[p["path"]] == "unsure"]
        print(f"\n对上 {len(have)}/{len(picked)} 篇")
        for name, ss in (("朱雀判 AI", ai), ("朱雀判人类", hu), ("拿不准", un)):
            if ss:
                print(f"  {name:8} {len(ss):2} 篇  L1 均分 {sum(ss)/len(ss):5.1f}  "
                      f"区间 {min(ss)}~{max(ss)}")
        if ai and hu:
            gap = sum(hu) / len(hu) - sum(ai) / len(ai)
            print(f"\n人味分差距（人类组均分 - AI 组均分）：{gap:+.1f}")
            print("差距 ≥ 5 且方向为负 = L1 与朱雀同向（我们的尺子有外部支撑）；"
                  "差距接近 0 = L1 分不出朱雀能分的东西，tell 集有盲区。")


if __name__ == "__main__":
    main()
