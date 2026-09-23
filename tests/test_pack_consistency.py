# -*- coding: utf-8 -*-
"""同一件事在多个知识文件里各写一遍 → 改一处漏两处（P2-37）。

审查报告点名的形态：维保周期的「半月 / 季度 / 半年 / 年度」同时写在
`knowledge/topics.md`、`knowledge/standards.md`、`compliance/industry.md`（两处），
而**注入状态各不相同**（topics 进 select、industry 进 write 红线、standards 的
那一节不进提示词）。内容现在是一致的，但没有任何东西阻止下一次只改其中一份。

不删重复的原因很实在：模型手里只有被注入的那几份，把 industry.md 里那句改成
"见 standards.md"就会造出一条**悬空引用**（P3-28 那一类，模型被告知去查一份
它没有的文档）。所以这里收的是"不许漂"，不是"只留一份"。

同一口径顺带守住另外两组重复度高的事实：语速表与标准编号的写法。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PACK = ROOT / "packs" / "elevator"

# 维保周期的四类，TSG T5002 的口径（顺序也是规范的顺序）
CYCLE = ("半月", "季度", "半年", "年度")
# 规范里**没有**的自造说法：出现即错（industry.md 自己列的反例）
BAD_CYCLE = ("每月维保", "双周维保", "每周维保", "年度维保一次", "每季度一次维保")
# 但"反例是写在禁令里的"：industry.md 那条规则原文就是
# 「只有半月、季度、半年、年度四类，不自造"每月维保""双周维保"」——
# 它提到坏说法恰恰是为了禁止它。守卫若把禁令本身判成违规，
# 就是一条会逼人删规则的空转断言（第一版就踩了这个）。
NEGATION = ("不自造", "不要", "不得", "禁止", "没有", "只有", "唯一", "❌", "不能", "不许")


def _md_files():
    return sorted(p for p in PACK.rglob("*.md") if p.is_file())


def _texts():
    out = {str(p.relative_to(PACK)): p.read_text(encoding="utf-8") for p in _md_files()}
    # yaml 也会带着口径进提示词（`private/service.yaml` 经 pack.yaml files.private
    # 注入，其 `maintenance.cycle` 在三副本时代是**第三处没人守的抄件**）——
    # 只扫 md 会漏掉 yaml 出现"半月/季度"这种行。2026-09-23 把 service.yaml 的
    # cycle 改成空（口径收敛到 standards.md 单点），这条文本想再漂就得走这里。
    svc = PACK / "private" / "service.yaml"
    if svc.exists():
        out["private/service.yaml"] = svc.read_text(encoding="utf-8")
    return out


def test_maintenance_cycle_is_stated_the_same_way_everywhere():
    """凡同时出现这四类里 ≥3 个的行（或**相邻两行**拼起来算的段），
    必须是同一套四类、同一个顺序。

    只查单行会漏掉 yaml 的 `cycle: "半月 / 季度 / 半年 / 年度"` ——
    它的四个词被折成 `cycle:` 与值两行，单行永远凑不齐 3 个（2026-09-23
    变异实测：把 service.yaml 的 cycle 装回去，单行守卫绿着放过去了）。
    所以对每一行，都再算一次「本行 + 下一行」的拼接。
    """
    offenders = []
    for rel, text in _texts().items():
        lines = text.splitlines()
        for i, line in enumerate(lines):
            chunk = line + (lines[i + 1] if i + 1 < len(lines) else "")
            found = [c for c in CYCLE if c in chunk]
            if len(found) < 3:
                continue
            if tuple(found) != CYCLE:
                offenders.append(f"{rel}: {chunk.strip()[:60]} → 列出的顺序/集合是 {found}")
            for bad in BAD_CYCLE:
                if bad in chunk and not any(n in chunk for n in NEGATION):
                    offenders.append(f"{rel}: 自造周期「{bad}」：{chunk.strip()[:60]}")
    assert not offenders, "维保周期口径漂移：\n" + "\n".join(offenders)


def test_no_file_claims_a_different_inspection_interval():
    """`N 天一次 / N 个月一次` 这类量化的周期说法只能有一个来源。

    正则刻意只抓"数字 + 时间单位 + 一次/每"这种**像是在定周期**的句子，
    免地把「困了 23 分钟」这类案例数字也算进来。
    """
    pat = re.compile(r"(每|间隔)?\s*\d+\s*(天|周|个月|月)(一次|维保|保养|检查)")
    hits = []
    for rel, text in _texts().items():
        for line in text.splitlines():
            for m in pat.finditer(line):
                hits.append(f"{rel}: {m.group(0)} ← {line.strip()[:50]}")
    assert not hits, "知识文件里出现了量化的维保周期表述（规范只有四类，没有天数）：\n" \
        + "\n".join(hits)


def test_duration_md_quota_mirrors_pack_yaml():
    """`rules/duration.md` 抄了一份配额表 —— 抄件必须与正本逐格相等。

    P2-37 的同一形态：改 `pack.yaml` 忘了改文档，作者按文档写稿、引擎按配置校验，
    两边都对不上还查不出根因。`rules/duration.md` 是人读文件（不注入模型），
    所以不能靠"少写点"回避，只能钉住一致性。
    """
    import re
    import yaml

    data = yaml.safe_load((PACK / "pack.yaml").read_text(encoding="utf-8"))
    table = data.get("quota_table") or {}
    assert table, "pack.yaml 没有 quota_table"
    md = (PACK / "rules" / "duration.md").read_text(encoding="utf-8")
    rows = dict()
    for line in md.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) == 5 and re.fullmatch(r"\d+s?", cells[0]):
            try:
                rows[int(cells[0].rstrip("s"))] = [int(c) for c in cells[1:]]
            except ValueError:
                continue          # 「约290」这类写法留给 quota_table_errors 去报
    assert rows, "duration.md 里没找到配额表（改了版式就要同步改这条断言）"
    assert set(rows) == {int(k) for k in table}, \
        f"文档与包配置的档位不一致：文档 {sorted(rows)} / 配置 {sorted(int(k) for k in table)}"
    bad = []
    for d, (total, hook, body, cta) in rows.items():
        cfg = table.get(d) or table.get(str(d))
        if [total, hook, body, cta] != [cfg["total"], cfg["hook"], cfg["body"], cfg["cta"]]:
            bad.append(f"{d}s 文档 {[total, hook, body, cta]} ≠ 配置 "
                       f"{[cfg['total'], cfg['hook'], cfg['body'], cfg['cta']]}")
        if hook + body + cta != total:
            bad.append(f"{d}s 三节相加 {hook + body + cta} ≠ total {total}")
    assert not bad, "配额表漂移：\n" + "\n".join(bad)


def test_short_durations_do_not_spend_their_time_on_the_cta():
    """P2-26：短档的结尾引导占比不许超过长档 —— 15 秒里念 3 秒 CTA 是反的。

    15 秒档原本 hook15 + body40 + cta15：正文只占 57%，而"这条视频想说的
    那件事"只有 8 秒。正确形状是钩子重、引导轻。
    """
    import yaml

    table = yaml.safe_load((PACK / "pack.yaml").read_text(encoding="utf-8"))["quota_table"]
    shares = {int(k): (v["cta"] / v["total"]) for k, v in table.items()}
    for d, share in shares.items():
        assert share <= 0.25, f"{d}s 档结尾引导占 {share:.0%}，压过四分之一就不是口播了"
    assert shares[15] <= shares[60] and shares[15] <= shares[180], \
        f"短档引导占比反而更高：{shares}"


def test_rate_table_is_not_reinvented_in_prose():
    """语速：pack.yaml 是唯一取值处；文档里出现的数字必须与表里某档一致。

    P2-36 的原始形态就是 `platform.md` 写 5.0~5.5、`duration.md` 写 5.5、
    pack.yaml 封顶 5.0 —— 三处都说自己在定口径。
    """
    import yaml

    data = yaml.safe_load((PACK / "pack.yaml").read_text(encoding="utf-8"))
    rates = {round(float(v), 2) for v in (data.get("rate_by_style") or {}).values()}
    assert rates, "pack.yaml 没有 rate_by_style，这条守卫就没有参照物了"
    offenders = []
    for rel, text in _texts().items():
        for line in text.splitlines():
            if "字/秒" not in line and "每秒" not in line:
                continue
            for n in re.findall(r"(\d+(?:\.\d+)?)\s*(?:[-~至]\s*(\d+(?:\.\d+)?))?\s*字/秒", line):
                for part in n:
                    if not part:
                        continue
                    v = round(float(part), 2)
                    # 5.0 是折算基准（duration.md 的通用折算式用它），不算档位
                    if v not in rates and v != 5.0:
                        offenders.append(f"{rel}: 语速 {v} 不在 rate_by_style {sorted(rates)} 里")
    assert not offenders, "文档里的语速与包配置对不上：\n" + "\n".join(sorted(set(offenders)))
