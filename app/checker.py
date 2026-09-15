#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""口播脚本校验器

把模型不擅长的事交给代码：数字数、算时长、扫禁用词。
由 Skill 时代的 tools/check.py 升级而来：
  - 词表从行业包 banwords.yaml 读取（hard/soft 两级 × 平台升降级）
  - 计数前剥离格式符号：**加粗**、{{待补}}、[画面：…]
  - 数字串按 1 字计（对齐 rules/duration.md 口径）
  - 直接输出目标字数配额（含**每段**配额），模型不做算术

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

# 时长下限：低于这个值配额会算出 0 甚至负数（修复前 duration=1 得到 body=-6）
MIN_DURATION = 5.0
MAX_DURATION = 1800.0

# 语速兜底。**这是全局唯一的一份** —— knowledge.rate_for_style 也 import 它，
# 两处必须给出同一个答案，否则同一份包配置会在「校验」与「组装时间轴」上
# 得出不同结论（修复前就是这样：estimate_seconds 有兜底、_compute_timings 没有，
# 于是 rate=0 的包在组装阶段炸出 ZeroDivisionError）。
DEFAULT_RATE = 4.5

# 单字词不下发匹配：中文里「最」这类单字条目会命中「最近」「最后」「最好」「最终」，
# 把 soft_hits 变成噪声（实测四种全部命中）。真要拦绝对化表述，应当枚举具体短语
# （最低价 / 最便宜 / 最好用），而不是裸单字。
MIN_WORD_LEN = 2


def count_chars(text: str) -> int:
    """口播字数：剥离格式标注与标点；数字串按 1 字计"""
    text = re.sub(r"\{\{[^}]*\}\}", "", text)          # {{待补：xxx}} 占位符
    text = re.sub(r"\[画面：[^\]]*\]", "", text)        # [画面：xxx] 内联标注
    text = re.sub(r"\d+", "0", text)                   # 每个数字串按 1 字计
    return len(re.sub(PUNCT, "", text))


def split_sections(text: str) -> list[str]:
    """按空行分段，供段落数与时长估算。

    注解原本写成 `list[tuple[str, str]]`，但返回的其实是**字符串列表**
    （调用方也是这么用的）。错注解比没注解更糟：照着它写的人会去解包。
    """
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    if len(paras) <= 1:  # 单行多段（用换行分隔）时退回按行
        paras = [p.strip() for p in text.split("\n") if p.strip()]
    return paras


def estimate_seconds(text: str, rate: float) -> float:
    """预估时长 = 字数/语速 + 段落间停顿×0.5s（口径同 rules/duration.md）"""
    # 语速非法时不能崩：包配置里 `rate_by_style: {快节奏: 0}` 会一路走到这里，
    # 抛出来的 ZeroDivisionError 用户根本看不懂。校验器是自己要「守下限」的那层，
    # 它先崩了就没有下限可言。
    try:
        r = float(rate)
    except (TypeError, ValueError):
        r = DEFAULT_RATE
    if r <= 0:
        r = DEFAULT_RATE
    paras = split_sections(text)
    pauses = max(len(paras) - 1, 0) * 0.5
    return count_chars(text) / r + pauses


class Banwords:
    """两级禁用词 + 平台升降级。

    匹配语义（修复前是 `text.count(w)` 逐个词统计，有两个毛病）：
      1. 重叠词重复计数 —— 「包过检」同时命中 `包过` 与 `包过检`，报「2 处硬伤」，
         回炉反馈里也把重复词喂给模型，白白多花一轮 token；
      2. 单字词噪声 —— `最` 命中「最近」「最后」「最好」「最终」。
    现在用一个「长词优先」的交替正则做**不重叠**扫描：每个字符只归属一个词，
    且归属给能匹配上的最长那个。计数因此与「实际违规处数」一致。
    """

    def __init__(self, data: dict):
        self.hard: set[str] = set(data.get("hard", []))
        self.soft: set[str] = set(data.get("soft", []))
        self.platform_rules: dict[str, dict] = data.get("platform", {}) or {}
        # 被丢弃的单字词：保留可见性，避免包作者以为「写了就生效」
        self.dropped_short: list[str] = sorted(
            w for w in (self.hard | self.soft) if len(w) < MIN_WORD_LEN)
        self._cache: dict[tuple, tuple] = {}

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

    @staticmethod
    def _pattern(words) -> "re.Pattern | None":
        # 长词在前 → 正则交替在该位置优先取最长匹配（Python re 取第一个能匹配的分支）
        ws = sorted({w for w in words if len(w) >= MIN_WORD_LEN},
                    key=lambda w: (-len(w), w))
        if not ws:
            return None
        return re.compile("|".join(re.escape(w) for w in ws))

    def scan(self, text: str, platform: str | None = None) -> dict:
        """返回 {hard: [{word,count}], soft: [...], dropped_short: [...]}"""
        hard, soft = self._adjusted(platform)
        key = (frozenset(hard), frozenset(soft))
        if key not in self._cache:
            self._cache[key] = (self._pattern(hard), self._pattern(soft))
        p_hard, p_soft = self._cache[key]

        hard_hits: dict[str, int] = {}
        soft_hits: dict[str, int] = {}
        if p_hard:
            for m in p_hard.finditer(text):
                w = m.group(0)
                hard_hits[w] = hard_hits.get(w, 0) + 1
        if p_soft:
            for m in p_soft.finditer(text):
                w = m.group(0)
                # hard 命中的词不再重复算 soft（长词优先已保证同表内不重叠）
                if w not in hard_hits:
                    soft_hits[w] = soft_hits.get(w, 0) + 1

        def fmt(d: dict[str, int]) -> list[dict]:
            return [{"word": w, "count": c}
                    for w, c in sorted(d.items(), key=lambda kv: (-kv[1], kv[0]))]

        return {"hard": fmt(hard_hits), "soft": fmt(soft_hits),
                "dropped_short": self.dropped_short}


class Quota:
    """字数配额：查表 + 按语速折算（表为 5.0 字/秒基准，见 rules/duration.md）"""

    def __init__(self, table: dict):
        self.table: dict[int, dict] = {}
        for k, v in (table or {}).items():
            try:
                self.table[int(k)] = v
            except (TypeError, ValueError):
                continue

    @classmethod
    def from_pack(cls, data: dict) -> "Quota":
        return cls(data.get("quota_table", {}))

    @property
    def available(self) -> bool:
        return bool(self.table)

    def target(self, duration: float, rate: float) -> dict:
        """目标字数。表为空时返回 {}，由调用方决定如何降级。

        修复前空表会在这里 IndexError（`keys[0]`）—— 缺 quota_table 的行业包
        会让整个生成以「list index out of range」失败，用户完全看不懂；
        单行表也会在 `keys[1]` 上崩。
        """
        if not self.table:
            return {}
        d = int(min(max(float(duration), MIN_DURATION), MAX_DURATION))
        row = self._row(d)
        k = rate / 5.0
        return {key: round(v * k) for key, v in row.items()
                if isinstance(v, (int, float))}

    def _row(self, duration: int) -> dict:
        if duration in self.table:
            return self.table[duration]
        keys = sorted(self.table)
        if len(keys) == 1:                      # 单行表：任何时长都取这一行
            return self.table[keys[0]]
        if duration <= keys[0]:
            lo, hi = keys[0], keys[1]
        elif duration >= keys[-1]:
            lo, hi = keys[-2], keys[-1]
        else:
            lo = max(k for k in keys if k < duration)
            hi = min(k for k in keys if k > duration)
        # 线性插值 / 外推
        ratio = (duration - lo) / (hi - lo)
        out = {}
        for field in ("total", "hook", "body", "cta"):
            a, b = self.table[lo].get(field), self.table[hi].get(field)
            if not isinstance(a, (int, float)) or not isinstance(b, (int, float)):
                continue
            out[field] = round(a + (b - a) * ratio)
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

    每段配额按**要点数均分**正文配额。修复前这里直接给每段「整个正文配额」，
    于是一个 2 要点、134 字的段落在 60s/4.5 配置下（body=171）永远够不到
    `171×1.3=222` 的阈值 —— pipeline 的「段落超配额」回炉分支是死代码，
    而前端却按 body/要点数 显示，两边对同一段给出相反结论。
    """
    full_text = "\n".join(s.get("text", "") for s in sections)
    total = count_chars(full_text)
    sec = estimate_seconds(full_text, rate)

    quota = quota or Quota({})
    target = quota.target(duration, rate) if quota.available else {}

    hits = banwords.scan(full_text, platform)

    n_points = max(sum(1 for s in sections if s.get("type") == "point"), 1)
    body_total = target.get("body")
    per_point = (body_total // n_points) if isinstance(body_total, int) else None

    # 逐段字数 vs 配额（提示级，不设门槛）
    seg_counts: list[dict] = []
    for s in sections:
        n = count_chars(s.get("text", ""))
        field = SEG_FIELD.get(s.get("type", ""), "body")
        seg_counts.append({
            "type": s.get("type"), "chars": n,
            "quota": per_point if field == "body" else target.get(field),
        })

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
        "dropped_short": hits["dropped_short"],
        "segments": seg_counts,
        "points": n_points,
        "passed": ok_hard and ok_time,
        "blockers": ([] + (["硬禁用词 %d 处" % sum(h["count"] for h in hits["hard"])] if not ok_hard else [])
                     + (["时长偏差 %.1f%% 超 ±10%%" % dev] if not ok_time else [])),
    }
    return report


# ── CLI（兼容原 tools/check.py 用法）────────────────────────────

def _default_pack() -> Path:
    return Path(__file__).resolve().parent.parent / "packs" / "elevator"


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

    secs = estimate_seconds(text, args.rate)
    if args.duration:
        sections = [{"type": "point", "text": p} for p in split_sections(text)]
        report = check_script(sections, args.duration, args.rate, ban, args.platform, quota)
    else:
        scan = ban.scan(text, args.platform)
        report = {"chars_total": count_chars(text), "estimated_seconds": round(secs, 1),
                  "hard_hits": scan["hard"], "soft_hits": scan["soft"],
                  "passed": not scan["hard"], "blockers": []}

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
