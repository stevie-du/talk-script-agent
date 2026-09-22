# -*- coding: utf-8 -*-
"""AI 味检测：把人味 tell 从"感觉"变成可计数、可定位的字段。

与 `checker.Banwords` 的分工
--------------------------
banwords 管**合规**（广告法/平台规则，命中即必须改，阻塞发布）；
本模块管**文风**（像不像人说话，命中是提示，不阻塞）。
两者都是"把模型不擅长自查的事交给代码"，但**判定结果不能混**——
文风命中一旦进 `passed`，就等于用一套没校准过的尺子去拦发布，
所以本模块只产出分数与命中，是否回炉由调用方决定。

行业无关
--------
算法在本文件，词表在 `packs/<行业>/ai_tells.yaml`。换行业只换 yaml，
不改代码 —— 与 `banwords.yaml` 同一套分工。本文件里出现任何具体行业词
都是 bug。
"""
from __future__ import annotations

import re
import statistics
from dataclasses import dataclass

# 与 checker.count_chars 同一口径：数字串按 1 字计、剥掉格式符号。
# 这里**不 import checker**：checker 依赖本模块的方向是"校验调用文风检测"，
# 反向 import 会绕成循环；两处口径靠 tests/test_ai_tells.py 的一致性断言钉住。
_PUNCT = r'[\s，。、？！：；：\u201c\u201d\u2018\u2019（）《》〈〉【】…—·～*#|＿_／/,.!?;:-]'

# 强 tell：单次命中即报（结构上一眼可读出来，误伤空间小）
# 弱 tell：需 ≥2 处才报（语境相关，单次命中容易误伤，与 banwords 的 soft 同理）
STRONG, WEAK = "strong", "weak"

# 弱 tell 的最低命中数。写死成常量而不是塞进 yaml：
# yaml 里改这个数字等于改评分口径，必须和 score_of() 一起看，不该分散在两处。
WEAK_MIN = 2


def _plain(text: str) -> str:
    """剥掉 {{待补：…}}、[画面：…]、**加粗** 标记，便于按"念出来的话"判断。"""
    text = re.sub(r"\{\{[^}]*\}\}", "", text)
    text = re.sub(r"\[画面：[^\]]*\]", "", text)
    return re.sub(r"\*\*", "", text)


def _count(text: str) -> int:
    text = re.sub(r"\d+", "0", _plain(text))
    return len(re.sub(_PUNCT, "", text))


def _sentences(text: str) -> list[str]:
    """按句末标点切句（停顿符 ／ 也算断句，口播稿里它就是句号）。"""
    parts = re.split(r"[。！？!?；;]|／", _plain(text))
    return [p.strip() for p in parts if p.strip()]


def _scan_words(text: str, words) -> dict[str, int]:
    """长词优先的**不重叠**扫描（同一位置只算最长的词）。

    为什么不逐个 `text.count(w)`：「绝对」与「绝对化」会双计，
    一个位置报两处命中，分数被虚高 —— banwords 已经为这个付过学费。
    """
    ws = sorted({w for w in words if w and len(w) >= 2}, key=lambda w: (-len(w), w))
    if not ws:
        return {}
    pat = re.compile("|".join(re.escape(w) for w in ws))
    out: dict[str, int] = {}
    for m in pat.finditer(text):
        w = m.group(0)
        out[w] = out.get(w, 0) + 1
    return out


@dataclass
class TellHit:
    id: str
    severity: str
    count: int
    where: str        # 定位：段落序号 / 句序，让人能去找而不是重写一篇
    detail: str

    def as_dict(self) -> dict:
        return {"id": self.id, "severity": self.severity, "count": self.count,
                "where": self.where, "detail": self.detail}


class AITells:
    """一个行业包的文风 tell 集。构造即校验，读坏不静默。"""

    #: 结构类 tell（算法在代码里，词表帮不上忙）
    STRUCTURAL = ("parallel_triple", "list_enumeration", "uniform_para_len",
                  "repeat_opening", "no_specific")
    #: 词表类 tell（yaml 的 lexicon 给词）
    LEXICAL = ("bookish_connective", "abstract_noun_ending", "slogan_closing",
               "opening_ban", "filler_stack")
    ALL = STRUCTURAL + LEXICAL

    def __init__(self, data: dict | None):
        data = data or {}
        self.severity: dict[str, str] = {}
        for k in ("strong", "weak"):
            for tid in (data.get(k) or []):
                self.severity[tid] = STRONG if k == "strong" else WEAK
        self.lexicon: dict[str, list[str]] = {
            k: [str(w) for w in (v or [])] for k, v in (data.get("lexicon") or {}).items()}
        self.source = data.get("md") or ""
        # 未知 tell 名必须报出来：写错一个字母（parallel_triple → parallel_triiple）
        # 的后果是"这条 tell 永远不跑"，而报告看起来完全正常。
        # ⚠ 必须先并集再减：`|` 的优先级低于 `-`，写成
        # `set(a) | set(b) - set(ALL)` 会被解析成 `a | (b - ALL)` ——
        # 于是**每一个合法名字**都被报成未知（本模块原来就是这个错，
        # 由 tests/test_ai_tells.py 抓出），守卫本身成了噪声源。
        self.unknown = sorted((set(self.severity) | set(self.lexicon)) - set(self.ALL))
        self.lexicon_missing = [t for t in self.LEXICAL if t in self.severity and not self.lexicon.get(t)]

    def sev(self, tid: str) -> str:
        return self.severity.get(tid, WEAK)

    # ── 结构类 ────────────────────────────────────────────
    def _parallel_triple(self, sections) -> list[TellHit]:
        """排比三连：同一段里 ≥3 个由「、」连接、字数相近的并列项。"""
        hits = []
        for i, s in enumerate(sections, 1):
            for clause_run in re.findall(r"[\u4e00-\u9fa5A-Za-z0-9]+(?:、[\u4e00-\u9fa5A-Za-z0-9]+){2,}",
                                         _plain(s.get("text", ""))):
                items = clause_run.split("、")
                lens = [len(x) for x in items]
                # 长度极差 ≤3 才算"工整"；差得远是自然列举，不是排比
                if max(lens) - min(lens) <= 3:
                    hits.append(TellHit("parallel_triple", self.sev("parallel_triple"),
                                        len(items), f"第{i}段",
                                        f"{len(items)} 连并列：{clause_run[:24]}…"))
        return hits

    def _list_enumeration(self, sections) -> list[TellHit]:
        """清单体：连续 ≥3 句以「第一/第二/第三」或「一是/二是」递增开头。

        刻意保守 —— banwords 曾因为裸「第一」造成 169/175 轮假命中
        （「第一件事」「第一步」是正常用法），所以这里要求**递增序列**，
        孤立出现一次不算。
        """
        hits = []
        num = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8}
        # 两种形态：第X（后接任何字，含"第一，"这种直接停顿）与 X是
        pat = re.compile(r"^第([一二三四五六七八])|[，、]?([一二三四五六七八])是")
        for i, s in enumerate(sections, 1):
            seq: list[int] = []
            for sent in _sentences(s.get("text", "")):
                m = pat.match(sent)
                if not m:
                    continue
                ch = m.group(1) or m.group(2)
                if ch and (not seq or num[ch] == seq[-1] + 1):
                    seq.append(num[ch])
            if len(seq) >= 3:
                hits.append(TellHit("list_enumeration", self.sev("list_enumeration"),
                                    len(seq), f"第{i}段", f"清单体连排：第一…第{len(seq)}"))
        return hits

    def _uniform_para_len(self, sections) -> list[TellHit]:
        """每段等长、无起伏：≥3 段且字数变异系数 < 0.15。"""
        pts = [c for s in sections if s.get("type") == "point"
               and (c := _count(s.get("text", ""))) > 0]
        if len(pts) < 3:
            return []
        mean = statistics.fmean(pts)
        cv = statistics.pstdev(pts) / mean if mean else 0.0
        if cv < 0.15:
            return [TellHit("uniform_para_len", self.sev("uniform_para_len"),
                            len(pts), "全篇",
                            f"{len(pts)} 段字数 {pts} 几乎等长（变异系数 {cv:.2f}）")]
        return []

    def _repeat_opening(self, sections) -> list[TellHit]:
        """句首重复：同一个二字开头占据 >1/3 的句子。"""
        sents = [x for s in sections for x in _sentences(s.get("text", ""))]
        if len(sents) < 4:
            return []
        heads: dict[str, int] = {}
        for x in sents:
            h = x[:2]
            heads[h] = heads.get(h, 0) + 1
        top, n = max(heads.items(), key=lambda kv: kv[1])
        if n >= 3 and n / len(sents) > 1 / 3:
            return [TellHit("repeat_opening", self.sev("repeat_opening"),
                            n, "全篇", f"{n} 句以「{top}」开头")]
        return []

    def _no_specific(self, sections) -> list[TellHit]:
        """通篇零具体：没有数字、没有可数事实、也没有 {{待补}} 占位。

        这是 voice.md 自己标为"最致命"的一条，所以默认 strong。
        有 {{待补}} 不算命中 —— 那是"知道这里缺事实"，比编一个数字诚实。
        """
        full = "\n".join(s.get("text", "") for s in sections)
        if re.search(r"\{\{[^}]*\}\}", full):
            return []
        if re.search(r"\d|[一二三四五六七八九十百]+\s*(天|次|台|元|块|米|层|分钟|小时|起|个|位|%|％)", full):
            return []
        return [TellHit("no_specific", self.sev("no_specific"), 1, "全篇",
                        "通篇无一个具体数字/时间/数量")]

    # ── 词表类 ────────────────────────────────────────────
    def _lexical(self, tid: str, sections) -> list[TellHit]:
        words = self.lexicon.get(tid) or []
        if not words:
            return []
        hits, total = [], 0
        for i, s in enumerate(sections, 1):
            text = _plain(s.get("text", ""))
            found = _scan_words(text, words)
            if not found:
                continue
            n = sum(found.values())
            # 口号/开场禁区是"出现即问题"，走 strong 的单次阈值；
            # 其余词表类按弱处理，需要累计到 WEAK_MIN 才报。
            if self.sev(tid) == STRONG or n >= WEAK_MIN:
                total += n
                hits.append(TellHit(tid, self.sev(tid), n, f"第{i}段",
                                    "、".join(f"{w}×{c}" for w, c in sorted(
                                        found.items(), key=lambda kv: -kv[1])[:4])))
        return hits

    # ── 汇总 ─────────────────────────────────────────────
    def scan(self, sections: list[dict]) -> list[TellHit]:
        """sections = [{"type","text"}, …]。返回命中列表（未过阈值的 tell 不出现）。"""
        out: list[TellHit] = []
        run = {"parallel_triple": self._parallel_triple,
               "list_enumeration": self._list_enumeration,
               "uniform_para_len": self._uniform_para_len,
               "repeat_opening": self._repeat_opening,
               "no_specific": self._no_specific}
        for tid, fn in run.items():
            if self.severity.get(tid) is None:
                continue          # yaml 没声明的 tell 不跑（默认关，而不是默认开）
            out += fn(sections)
        for tid in self.LEXICAL:
            if self.severity.get(tid) is None:
                continue
            out += self._lexical(tid, sections)
        return out

    def report(self, sections: list[dict]) -> dict:
        """给 checker 用的人味报告。**不参与 passed 判定**。"""
        hits = self.scan(sections)
        return {"score": score_of(hits),
                "hits": [h.as_dict() for h in hits],
                "strong": sum(1 for h in hits if h.severity == STRONG),
                "weak": sum(1 for h in hits if h.severity == WEAK),
                "tells_enabled": sorted(self.severity),
                "config_warnings": self.warnings()}

    def warnings(self) -> list[str]:
        w = []
        if self.unknown:
            w.append("ai_tells.yaml 有未知 tell 名（引擎不跑它）：" + "、".join(self.unknown))
        if self.lexicon_missing:
            w.append("词表类 tell 已声明 severity 但 lexicon 为空：" + "、".join(self.lexicon_missing))
        return w


#: 单条 strong 扣分 / 单条 weak 扣分。**这两个数是待校准的初值，不是结论**：
#: A4 要先拿现成产物跑出分数分布再定门槛（未校准的尺子当门槛 = banwords soft
#: 那种 169/175 轮假命中的复现路径）。
PENALTY = {STRONG: 12, WEAK: 4}


def score_of(hits: list[TellHit]) -> int:
    """0-100，越高越像人。公式刻意简单到一眼能算，先可解释再谈准确。"""
    loss = sum(PENALTY.get(h.severity, 0) for h in hits)
    return max(0, 100 - loss)
