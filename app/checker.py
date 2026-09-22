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

# 配额行字段（total/hook/body/cta）。表里写了但**不是数字**的值必须被审计出来，
# 否则该键会被 `isinstance(v, (int, float))` 静默丢掉（实测 `total: "约290"` →
# target_total=None → 提示词渲染成「总计≈ 字」零告警）。
QUOTA_FIELDS = ("total", "hook", "body", "cta")

# `pack.yaml` 的 `duration_tolerance_pct` 合法区间。下界不给 0：0 容差意味着
# 任何稿子都必然不合格（字数是整数，目标秒数不是），回炉轮次会被白白烧完。
TOLERANCE_MIN, TOLERANCE_MAX = 0.5, 100.0


def tolerance_error(value) -> str:
    """`duration_tolerance_pct` 的取值检查：返回人话错误，空串 = 合法或没配。

    这一项写错的后果与 `quota_table: total: "约290"` 同类 —— **静默改变判定**：
    写成字符串会一路抛到 `_row` 之外，写成 `0` 会让每次生成都跑到最后一轮才失败。
    所以在包加载时就判死（`pack_info` 的 fatal），不进生成。
    """
    if value is None or value == "":
        return ""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return (f"pack.yaml 的 duration_tolerance_pct 必须是数字（百分比，10 表示 ±10%），"
                f"实际是 {type(value).__name__}：{value!r}")
    v = float(value)
    if not (TOLERANCE_MIN <= v <= TOLERANCE_MAX):
        return (f"pack.yaml 的 duration_tolerance_pct={value} 超出可用区间 "
                f"{TOLERANCE_MIN}~{TOLERANCE_MAX}：太窄会把任何稿子都判不合格，"
                f"太宽等于不校验时长")
    return ""


def validate_banwords(data, source: str = "banwords.yaml") -> tuple[list[str], list[str]]:
    """禁用词表结构校验，返回 `(fatal, advisory)`。

    fatal —— 结构写错，词表已**不可信**，绝不允许静默降级：
      `hard` / `soft` / `platform.<p>.{extra_hard,promote,demote}` 必须是字符串列表
      （元素必须是非空字符串）；`platform` 与 `platform.<p>` 必须是映射。
      实测：`hard: "政府补贴"` 写成标量 → 被当可迭代拆成「府」「政」「补」等单字 →
      又被 `MIN_WORD_LEN=2` 全部丢掉 → 命中归零，而界面横幅还在报「N 个单字被忽略」。
    advisory —— 不影响本次匹配，但包作者写了引擎不读的键（`extra_soft`）。

    每条错误都带 `source`（词表文件名）与键路径，抛出去时包作者一眼能定位。
    """
    fatal: list[str] = []
    advisory: list[str] = []
    if data is None:
        return fatal, advisory
    if not isinstance(data, dict):
        fatal.append(f"{source} 顶层必须是映射（词表结构），实际是 {type(data).__name__}：{data!r}")
        return fatal, advisory

    def bad_word_list(v, where: str) -> bool:
        if not isinstance(v, list) or not all(
                isinstance(w, str) and w.strip() for w in v):
            fatal.append(f"{source} 的 {where} 必须是「非空字符串列表」，"
                         f"实际是 {type(v).__name__}：{v!r}")
            return True
        return False

    for key in ("hard", "soft"):
        v = data.get(key)
        if v is not None:
            bad_word_list(v, key)

    platforms = data.get("platform")
    if platforms is not None and not isinstance(platforms, dict):
        fatal.append(f"{source} 的 platform 必须是映射（平台→规则），"
                     f"实际是 {type(platforms).__name__}：{platforms!r}")
        return fatal, advisory
    for p, rules in (platforms or {}).items():
        if not isinstance(rules, dict):
            fatal.append(f"{source} 的 platform.{p} 必须是映射（extra_hard/promote/demote），"
                         f"实际是 {type(rules).__name__}：{rules!r}")
            continue
        for field in ("extra_hard", "promote", "demote"):
            v = rules.get(field)
            if v is not None:
                bad_word_list(v, f"platform.{p}.{field}")
        extra_soft = rules.get("extra_soft")
        if extra_soft not in (None, [], ""):
            advisory.append(f"{source} 的 platform.{p}.extra_soft：引擎不读 extra_soft，"
                            f"请并入顶层 soft（写了 {extra_soft!r}）")
    return fatal, advisory


def quota_table_errors(table) -> list[str]:
    """quota_table 结构校验：每行必须是映射、写了的字段必须是数字。

    返回人话错误列表（含**哪张表哪项**）；空 = 结构合法。解析失败的字段会在
    `Quota.target()` 里被静默丢掉（`isinstance` 过滤）—— 这条把它摊出来。
    """
    if table is None:
        return []
    if not isinstance(table, dict):
        return [f"quota_table 必须是映射（时长→配额行），实际是 {type(table).__name__}：{table!r}"]
    errors: list[str] = []
    for k, row in table.items():
        if not isinstance(row, dict):
            errors.append(f"quota_table 的 {k} 秒行必须是映射（total/hook/body/cta），"
                          f"实际是 {type(row).__name__}：{row!r}")
            continue
        for field in QUOTA_FIELDS:
            if field in row and not isinstance(row[field], (int, float)):
                errors.append(f"quota_table 的 {k} 秒行 {field} 不是数字：{row[field]!r}")
    return errors


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

    def __init__(self, data: dict, source: str = "banwords.yaml"):
        # P1-25：结构写错必须在这里就炸，不能带着「拆碎的单字」往下走。
        # 实测 `hard: "政府补贴"` 写成分行标量 → set(data.get("hard", [])) 把字符串
        # 拆成单字 → 单字全被 MIN_WORD_LEN 丢掉 → 命中归零；更糟的是横幅还报
        # 「N 个单字禁用词被忽略（府、政、补…）」，把结构错误伪装成正常降级。
        fatal, advisory = validate_banwords(data or {}, source)
        if fatal:
            raise ValueError("；".join(fatal))
        # 不致命但包作者该知道的：extra_soft（`validate_banwords` 给）、
        # 平台 promote/demote 落空（`_adjusted` 在 scan 时补进这里）。
        # ai_tells 的结构问题不在这里报 —— 它属于包层，见 `knowledge.resolve_ai_tells`
        # 写进的 `PackInfo.warnings` / `Pack.warnings`（词表构造时拿不到 tell 文件名）。
        self.errors: list[str] = list(advisory)
        self.source: str = source
        self.hard: set[str] = {w for w in (data or {}).get("hard", [])
                               if isinstance(w, str) and w.strip()}
        self.soft: set[str] = {w for w in (data or {}).get("soft", [])
                               if isinstance(w, str) and w.strip()}
        self.platform_rules: dict[str, dict] = (data or {}).get("platform", {}) or {}
        # 被丢弃的单字词：保留可见性，避免包作者以为「写了就生效」。
        # 结构错误不会进到这里 —— 那条路径已经在上面 raise 了。
        self.dropped_short: list[str] = sorted(
            w for w in (self.hard | self.soft) if len(w) < MIN_WORD_LEN)
        self._cache: dict[tuple, tuple] = {}

    @classmethod
    def load(cls, path: Path) -> "Banwords":
        with open(path, encoding="utf-8") as f:
            return cls(yaml.safe_load(f) or {}, source=path.name)

    def _adjusted(self, platform: str | None) -> tuple[set[str], set[str], list[str]]:
        """按平台调整硬/软词表，并返回**这次调整里落空的条目**说明（P2-6）。

        promote 只在词已经在顶层 soft 里时生效、demote 只在它本来是 hard 时生效，
        两个条件都不满足时条目被无声丢掉 —— 包作者以为「小红书禁止'绝对'」生效了，
        实际那条词在小红书平台什么都不做。`extra_soft` 早就有这类提醒
        （`validate_banwords`），同一种失效形状不该一边有声一边无声。
        """
        hard, soft = set(self.hard), set(self.soft)
        notes: list[str] = []
        if not platform:
            return hard, soft, notes
        rules = self.platform_rules.get(platform, {}) or {}
        hard |= set(rules.get("extra_hard", []))
        promote = set(rules.get("promote", []))
        demote = set(rules.get("demote", []))
        dead_promote = sorted(promote - soft - hard)
        dead_demote = sorted(demote - self.hard)
        if dead_promote:
            notes.append(f"{self.source} 的 platform.{platform}.promote 落空："
                         f"{'、'.join(repr(w) for w in dead_promote)} 不在顶层 soft 里，"
                         "没有可升级的东西（要么把词加进顶层 soft，要么直接写进 extra_hard）")
        if dead_demote:
            notes.append(f"{self.source} 的 platform.{platform}.demote 落空："
                         f"{'、'.join(repr(w) for w in dead_demote)} 本来就不是硬禁用词，"
                         "降级需要一个可降的东西")
        hard |= promote & soft
        hard -= demote
        soft |= demote & self.hard
        soft -= hard
        return hard, soft, notes

    @staticmethod
    def _pattern(words) -> "re.Pattern | None":
        # 长词在前 → 正则交替在该位置优先取最长匹配（Python re 取第一个能匹配的分支）
        ws = sorted({w for w in words if len(w) >= MIN_WORD_LEN},
                    key=lambda w: (-len(w), w))
        if not ws:
            return None
        return re.compile("|".join(re.escape(w) for w in ws))

    def scan(self, text: str, platform: str | None = None) -> dict:
        """返回 {hard: [{word,count}], soft: [...], dropped_short: [...]}

        副作用：把这一轮平台调整里**落空**的 promote/demote 记进 `self.errors`
        （P2-6）。`check_script` 读 `banwords.errors` 出 `banword_notes`，
        所以提醒会出现在校验报告里 —— 与 `extra_soft` 那条同一条路。
        """
        hard, soft, notes = self._adjusted(platform)
        for note in notes:
            if note not in self.errors:
                self.errors.append(note)
        key = (frozenset(hard), frozenset(soft))
        if key not in self._cache:
            self._cache[key] = (self._pattern(hard), self._pattern(soft))
        p_hard, p_soft = self._cache[key]

        hard_hits: dict[str, int] = {}
        soft_hits: dict[str, int] = {}
        covered: list[tuple[int, int]] = []
        if p_hard:
            for m in p_hard.finditer(text):
                w = m.group(0)
                hard_hits[w] = hard_hits.get(w, 0) + 1
                covered.append((m.start(), m.end()))
        if p_soft:
            for m in p_soft.finditer(text):
                w = m.group(0)
                # 与任何 hard 命中**位置重叠** → 同一处违规的两个表述，只报 hard。
                # 修复前只判「同词」，跨表时「绝对安全」(hard) 与「绝对」(soft)
                # 在同一位置双计 —— 一处违规报两遍，回炉反馈也把重复词喂给模型
                # （P1-21）。长词优先只保证同表内不重叠，管不了跨表。
                if any(s < m.end() and m.start() < e for s, e in covered):
                    continue
                soft_hits[w] = soft_hits.get(w, 0) + 1

        def fmt(d: dict[str, int]) -> list[dict]:
            return [{"word": w, "count": c}
                    for w, c in sorted(d.items(), key=lambda kv: (-kv[1], kv[0]))]

        return {"hard": fmt(hard_hits), "soft": fmt(soft_hits),
                "dropped_short": self.dropped_short}


# 句子切分：回炉反馈要指出「改哪一句」，只给「词×次数」时模型无从下手
_SENT_SPLIT = re.compile(r"[。！？!?；;\n]")


def _sentence_at(text: str, pos: int, word: str) -> str:
    """命中处所在的句子（原样，不剥离格式标注）—— 反馈里作为可执行的定位。"""
    start = max((m.end() for m in _SENT_SPLIT.finditer(text, 0, pos)), default=0)
    end_m = _SENT_SPLIT.search(text, pos)
    end = end_m.end() if end_m else len(text)
    return text[start:end].strip()


def locate_hits(hits: list[dict], sections: list[dict],
                max_per_word: int = 2) -> None:
    """给每条命中补 `at`：命中落在第几段、所在原句。

    Self-Refine 的 critique 必须是可执行的修改清单，而不是统计数字。
    修复前 `_violation_feedback` 只能说「命中硬禁用词「政府补贴」×1」，
    模型既不知道在哪一句、也看不出改完之后原句是什么，于是每轮回炉都是
    「重新掷骰」而不是「改骰子」（P2-41 / 主报告 §三）。
    就地改 `hits`（调用方传进来的就是 report 里那份列表）。
    """
    for h in hits:
        places: list[dict] = []
        for i, s in enumerate(sections):
            text = s.get("text", "") or ""
            start = 0
            while len(places) < max_per_word:
                j = text.find(h["word"], start)
                if j < 0:
                    break
                places.append({"segment": i + 1,
                               "line": _sentence_at(text, j, h["word"])})
                start = j + len(h["word"])
            if len(places) >= max_per_word:
                break
        h["at"] = places


class Quota:
    """字数配额：查表 + 按语速折算（表为 5.0 字/秒基准，见 rules/duration.md）"""

    def __init__(self, table: dict):
        # P1-26：表里写了「非数字」的值（如 `total: "约290"`）会被 target() 的
        # isinstance 过滤静默丢掉 → target_total=None 而 quota_degraded=False，
        # 提示词渲染成「总计≈ 字」零告警。这里把每条解析失败记进 errors 供
        # param_audit 上报；target() 在 total 不可用时整体返回 {}，让调用方
        # 走「公开降级」而不是「静默丢键」。
        self.errors: list[str] = quota_table_errors(table)
        self.table: dict[int, dict] = {}
        if not isinstance(table, dict):
            return
        for k, v in table.items():
            try:
                self.table[int(k)] = v
            except (TypeError, ValueError):
                self.errors.append(f"quota_table 的时长键 {k!r} 不是数字")
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
        out = {key: round(v * k) for key, v in row.items()
               if isinstance(v, (int, float))}
        if "total" not in out:
            # `total` 解析失败（写成了 "约290" 之类）→ 整份配额不可信。
            # 修复前这里返回 {hook:…, body:…, cta:…}（非空）→ quota_degraded=False
            # → 提示词渲染成「总计≈ 字」。现在返回 {}，调用方必然走公开降级路径，
            # 包作者从 param_audit 能看到具体是哪张表哪一项写错了。
            return {}
        return out

    def _row(self, duration: int) -> dict:
        if isinstance(self.table.get(duration), dict):
            return self.table[duration]
        # 行不是映射（写成了 `60: "290 字"`）→ 不能参与插值，跳过；全跳空时
        # 返回 {}，target() 会整体降级而不是在这里 AttributeError。
        keys = sorted(k for k, v in self.table.items() if isinstance(v, dict))
        if not keys:
            return {}
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
    tolerance: float | None = None,
    tells=None,
) -> dict:
    """校验分段脚本。

    sections: [{"type": "hook"|"point"|"cta", "text": "...", ...}, ...]
    判定门槛：硬禁用词 0 处 且 时长偏差 ≤±容差。

    tolerance（百分比）：**由包配置 `pack.yaml` 的 `duration_tolerance_pct` 传入**
    （`Pack.duration_tolerance()` → pipeline 的两个校验调用点）。不传时走下面
    的「时长自适应」口径 —— 包没配就是默认，不再是一句"形参保留但没人传"的死参数。

    duration 非正数 → 直接判「参数错误」，不按「偏差 0% 合格」通过（实测
    190 字/目标 18 字仍被判合格，因为 dev = 0.0 恒成立）。
    短时长物理上装不下绝对容差（15s 档合格区间仅 56~70 字，±7 字），
    按 `max(10, 3.0/duration*100)` 放宽：15s→±20%、30s→±10%（3.0/30*100=10）、
    60s 及以上→±10%。

    每段配额按**要点数均分**正文配额。修复前这里直接给每段「整个正文配额」，
    于是一个 2 要点、134 字的段落在 60s/4.5 配置下（body=171）永远够不到
    `171×1.3=222` 的阈值 —— pipeline 的「段落超配额」回炉分支是死代码，
    而前端却按 body/要点数 显示，两边对同一段给出相反结论。
    """
    full_text = "\n".join(s.get("text", "") for s in sections)
    total = count_chars(full_text)
    # 人味报告（`app/ai_tells.py`）：**只出分数与命中，不进 passed 判定**。
    # 拿一把没校准过的尺子去拦发布，就是 banwords soft 当年 169/175 轮假命中的
    # 复现路径。`tells=None` = 这个包没配 ai_tells.yaml，字段落 null 而不是
    # 假装满分 ——「没测」与「测了很好」在报告里必须是两种形状。
    ai_report = tells.report(sections) if tells is not None else None

    # P3-30：duration 非正数时 dev 恒为 0（`(sec-0)/0 if duration else 0.0`），
    # 190 字/目标 18 字也会被判合格。直接判「参数错误」，不进入合格判定。
    if not duration or float(duration) <= 0:
        return {
            "chars_total": total,
            "target_total": None,
            "estimated_seconds": round(estimate_seconds(full_text, rate), 1),
            "duration_target": duration,
            "deviation_pct": 0.0,
            "tolerance_pct": None,
            "rate": rate,
            "platform": platform,
            "hard_hits": [], "soft_hits": [],
            "dropped_short": banwords.dropped_short,
            "banword_notes": list(banwords.errors),
            "segments": [], "points": 0,
            "ai_tells": ai_report,
            "passed": False,
            "blockers": [f"时长参数非正数（{duration} 秒）：无法校验，请检查包配置或请求参数"],
        }
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
    limit = (tolerance if tolerance is not None
             else max(10.0, 3.0 / duration * 100 if duration else 10.0))
    ok_time = abs(dev) <= limit
    # 命中定位（P2-41）：回炉反馈要能指出「第几段的哪一句」，光给统计数字
    # 等于让模型重新掷一次骰子。
    locate_hits(hits["hard"], sections)
    locate_hits(hits["soft"], sections)
    report = {
        "chars_total": total,
        "target_total": target.get("total"),
        "estimated_seconds": round(sec, 1),
        "duration_target": duration,
        "deviation_pct": round(dev, 1),
        # 本次真正用的容差：短时长档是自适应放宽的（15s→±20%）。
        # 必须随报告外发 —— 修复前 `_violation_feedback` 自己写死 10%，
        # 于是 15 秒档「偏差 15%（合格）」照样被反馈要求改长度，
        # 两处对同一个数说出相反的话（P2-27 的口径只改了一半）。
        "tolerance_pct": round(limit, 1),
        "rate": rate,
        "platform": platform,
        "hard_hits": hits["hard"],
        "soft_hits": hits["soft"],
        "dropped_short": hits["dropped_short"],
        "banword_notes": list(banwords.errors),      # extra_soft 等不致命但该让作者知道的
        "segments": seg_counts,
        "points": n_points,
        "ai_tells": ai_report,
        "passed": ok_hard and ok_time,
        "blockers": ([] + (["硬禁用词 %d 处" % sum(h["count"] for h in hits["hard"])] if not ok_hard else [])
                     + (["时长偏差 %.1f%% 超 ±%.0f%%" % (dev, limit)] if not ok_time else [])),
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
