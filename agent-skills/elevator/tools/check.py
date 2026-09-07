#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""口播脚本校验器

把模型不擅长的事交给代码：数字数、算时长、扫禁用词。
由 Skill 时代的 tools/check.py 升级而来：
  - 词表从行业包 banwords.yaml 读取（hard/soft 两级 × 平台升降级）
  - 计数前剥离格式符号：**加粗**、{{待补}}、[画面：…]
  - 数字串按 1 字计（对齐 rules/duration.md 口径）
  - 直接输出目标字数配额，模型不做算术

独立 CLI 用法:
    python -m app.checker <脚本文件> [--duration 60] [--rate 4.5] [--pack packs/elevator] [--platform 抖音]
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import yaml

# 标点/空白/标注符号（不计入口播字数）
PUNCT = r'[\s，。、？！：；：""''（）《》〈〉【】…—·～*#|＿_／/,.!?;:-]'

# 段落类型 → 配额字段
SEG_FIELD = {"hook": "hook", "point": "body", "cta": "cta"}
SEG_LABEL = {"hook": "开场钩子", "point": "要点", "cta": "结尾引导"}


def count_chars(text: str) -> int:
    """口播字数：剥离格式标注与标点；数字串按 1 字计"""
    text = re.sub(r"\{\{[^}]*\}\}", "", text)          # {{待补：xxx}} 占位符
    text = re.sub(r"\[画面：[^\]]*\]", "", text)        # [画面：xxx] 内联标注
    text = re.sub(r"\d+", "0", text)                   # 每个数字串按 1 字计
    return len(re.sub(PUNCT, "", text))


def split_sections(text: str) -> list[tuple[str, str]]:
    """按空行分段，供段落数与时长估算"""
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    if len(paras) <= 1:  # 单行多段（用换行分隔）时退回按行
        paras = [p.strip() for p in text.split("\n") if p.strip()]
    return paras


def estimate_seconds(text: str, rate: float) -> float:
    """预估时长 = 字数/语速 + 段落间停顿×0.5s（口径同 rules/duration.md）"""
    paras = split_sections(text)
    pauses = max(len(paras) - 1, 0) * 0.5
    return count_chars(text) / rate + pauses


class Banwords:
    """两级禁用词 + 平台升降级"""

    def __init__(self, data: dict):
        self.hard: set[str] = set(data.get("hard", []))
        self.soft: set[str] = set(data.get("soft", []))
        self.platform_rules: dict[str, dict] = data.get("platform", {}) or {}

    @classmethod
    def load(cls, path: Path) -> "Banwords":
        with open(path, encoding="utf-8") as f:
            return cls(yaml.safe_load(f) or {})

    def _adjusted(self, platform: str | None) -> tuple[set[str], set[str]]:
        hard, soft = set(self.hard), set(self.soft)
        if not platform:
            return hard, soft
        rules = self.platform_rules.get(platform, {}) or {}
        hard |= set(rules.get("extra_hard", []))
        hard |= set(rules.get("promote", [])) & soft
        hard -= set(rules.get("demote", []))
        soft |= set(rules.get("demote", [])) & self.hard
        soft -= hard
        return hard, soft

    def scan(self, text: str, platform: str | None = None) -> dict:
        hard, soft = self._adjusted(platform)
        return {
            "hard": [{"word": w, "count": text.count(w)} for w in sorted(hard) if w in text],
            "soft": [{"word": w, "count": text.count(w)} for w in sorted(soft) if w in text],
        }


class Quota:
    """字数配额：查表 + 按语速折算（表为 5.0 字/秒基准，见 rules/duration.md）"""

    def __init__(self, table: dict):
        self.table = {int(k): v for k, v in table.items()}

    @classmethod
    def from_pack(cls, data: dict) -> "Quota":
        return cls(data.get("quota_table", {}))

    def target(self, duration: float, rate: float) -> dict:
        d = int(duration)
        row = self._row(d)
        k = rate / 5.0
        return {key: round(v * k) for key, v in row.items()}

    def _row(self, duration: int) -> dict:
        if duration in self.table:
            return self.table[duration]
        keys = sorted(self.table)
        if duration <= keys[0]:
            lo, hi = keys[0], keys[1]
        elif duration >= keys[-1]:
            lo, hi = keys[-2], keys[-1]
        else:
            lo = max(k for k in keys if k < duration)
            hi = min(k for k in keys if k > duration)
        # 线性插值
        ratio = (duration - lo) / (hi - lo)
        out = {}
        for field in ("total", "hook", "body", "cta"):
            out[field] = round(self.table[lo][field] + (self.table[hi][field] - self.table[lo][field]) * ratio)
        return out


def check_script(
    sections: list[dict],
    duration: float,
    rate: float,
    banwords: Banwords,
    platform: str | None = None,
    quota: Quota | None = None,
) -> dict:
    """校验分段脚本。

    sections: [{"type": "hook"|"point"|"cta", "text": "...", ...}, ...]
    判定门槛：硬禁用词 0 处 且 时长偏差 ≤±10%。
    """
    full_text = "\n".join(s.get("text", "") for s in sections)
    total = count_chars(full_text)
    sec = estimate_seconds(full_text, rate)

    quota = quota or Quota({})
    target = quota.target(duration, rate) if quota.table else {}

    hits = banwords.scan(full_text, platform)

    # 逐段字数 vs 配额（提示级，不设门槛）
    seg_counts: list[dict] = []
    for s in sections:
        n = count_chars(s.get("text", ""))
        field = SEG_FIELD.get(s.get("type", ""), "body")
        seg_counts.append({"type": s.get("type"), "chars": n, "quota": target.get(field)})

    dev = (sec - duration) / duration * 100 if duration else 0.0
    ok_hard = not hits["hard"]
    ok_time = abs(dev) <= 10
    report = {
        "chars_total": total,
        "target_total": target.get("total"),
        "estimated_seconds": round(sec, 1),
        "duration_target": duration,
        "deviation_pct": round(dev, 1),
        "rate": rate,
        "platform": platform,
        "hard_hits": hits["hard"],
        "soft_hits": hits["soft"],
        "segments": seg_counts,
        "passed": ok_hard and ok_time,
        "blockers": ([] + (["硬禁用词 %d 处" % sum(h["count"] for h in hits["hard"])] if not ok_hard else [])
                     + (["时长偏差 %.1f%% 超 ±10%%" % dev] if not ok_time else [])),
    }
    return report


# ── CLI（兼容原 tools/check.py 用法）────────────────────────────

def _default_pack() -> Path:
    return Path(__file__).resolve().parent.parent



def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="口播脚本校验（字数/时长/禁用词）")
    ap.add_argument("file", help="脚本文件路径")
    ap.add_argument("--duration", type=float, default=None, help="目标时长（秒）")
    ap.add_argument("--rate", type=float, default=4.5, help="语速（字/秒）")
    ap.add_argument("--pack", default=str(_default_pack()), help="行业包目录")
    ap.add_argument("--platform", default=None, help="平台（抖音/视频号/小红书/B站）")
    args = ap.parse_args(argv)

    text = Path(args.file).read_text(encoding="utf-8")
    ban = Banwords.load(Path(args.pack) / "banwords.yaml")
    quota = Quota.from_pack(_load_pack_data(Path(args.pack)))

    if args.duration:
        secs = estimate_seconds(text, args.rate)
        sections = [{"type": "point", "text": p} for p in split_sections(text)]
        report = check_script(sections, args.duration, args.rate, ban, args.platform, quota)
    else:
        secs = estimate_seconds(text, args.rate)
        report = {"chars_total": count_chars(text), "estimated_seconds": round(secs, 1),
                  "hard_hits": ban.scan(text, args.platform)["hard"],
                  "soft_hits": ban.scan(text, args.platform)["soft"],
                  "passed": not ban.scan(text, args.platform)["hard"], "blockers": []}

    out = "=" * 46
    out += f"\n字数统计     : {report['chars_total']} 字"
    if report.get("target_total"):
        out += f"（配额 {report['target_total']} 字）"
    out += f"\n预估时长     : {report['estimated_seconds']} 秒（语速 {args.rate} 字/秒）"
    if args.duration:
        out += f"\n目标时长     : {args.duration} 秒"
        out += f"\n偏差         : {report['deviation_pct']:+.1f}%  → {'合格' if report['passed'] else '不合格，需重写'}"
    out += "\n" + "-" * 46
    hh, sh = report.get("hard_hits", []), report.get("soft_hits", [])
    out += f"\n禁用词[必改] : " + ("无" if not hh else f"{len(hh)} 词 → " + "、".join(f"{h['word']}×{h['count']}" for h in hh))
    out += f"\n禁用词[待确认]: " + ("无" if not sh else f"{len(sh)} 词 → " + "、".join(f"{s['word']}×{s['count']}" for s in sh))
    out += "\n" + "=" * 46
    print(out)
    return 0 if report.get("passed") else 1


def _load_pack_data(pack_dir: Path) -> dict:
    p = pack_dir / "pack.yaml"
    if p.exists():
        with open(p, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    return {}


if __name__ == "__main__":
    sys.exit(main())
