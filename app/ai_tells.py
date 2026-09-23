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

#: 词表类 tell 的**取词范围**（A-6 收敛：spec 与实现此前不是一条尺子）。
#:   cta_only   —— 只看结尾引导段；该段有第二人称或在提问，就是对着人说话而不是喊口号。
#:   first_sent —— 只看全篇第一句「以」词表开头；正文里引用一句"大家好"不是模板开场。
#:   缺省 any   —— 全文扫描（其余词表类）。
LEX_SCOPE = {"slogan_closing": "cta_only", "opening_ban": "first_sent"}

#: 汉字数词后面能跟的量词。原来只列了 11 个，于是「全城只有两家公司」被判"通篇零具体"，
#: 而同一句写成「2 家」就放过 —— 口播里汉字数词更自然（TTS 念出来没差别），
#: 判定不该被写法翻过去（A-6 #7）。双字量词另列，单字走字符类。
# ⚠ 分 / 成 / 人 / 起 **不在**字符类里（五路审查 P0-3，2026-09-23）：
# 它们是「十分」「八成」「个人」「一起」这类高频副词/名词的尾字，混进量词
# 会让 _MEASURE_RE 把它们当成"数词+量词"→ no_specific 被错误豁免 →
# 这条 strong tell 对满篇空话的稿件大面积漏报（实测「十分重要的一块内容」
# 「我个人觉得」「八成是因为」scan() 全返回 []）。
# 删掉它们不丢真量词：数字形式（3 分 / 8 成 / 5 人）由下面 no_specific 的
# `re.search(r"\d", full)` 分支兜底；汉字形式被误伤的方向是"多报"
# （「三个人」不再算具体），与 no_specific「没有具体数字就报」的意图一致 ——
# 宁可多报，不可把一个 strong tell 打成静默漏报。
_MEASURE_CHARS = ("天次台元块米层个位条家间辆口份部套户站年月日秒点倍手脚轮趟宗件种项类款档期批")
# 单字量词走**字符类**，双字的另列分支。写成 `(?:天次台…)` 会把整串当成一个
# 字面分支，于是「两家」照样不匹配（本文件第一版就是这个错，由测试抓出）。
_MEASURE_RE = re.compile(r"[一二三四五六七八九十百千万两]\s*(?:[" + _MEASURE_CHARS + r"]|分钟|小时|%|％)")

#: 占位符 `{{…}}`：作者标出"这里缺事实"。
_PLACEHOLDER_RE = re.compile(r"\{\{[^}]*\}\}")


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
    #: 命中处**原文里能被下划线标出来的那几段字**（§2.7 ③「人味标记回到正文」）。
    #: 为什么单独给一份、而不是让渲染层去解析 `detail`：`detail` 是**给人读的
    #: 展示串**（「品质保证×1」「3 连并列占该段 62%：…」），改一个标点就会让
    #: 前端的解析静默失效 —— 这正是本项目反复出现的"把判据绑在文案上"。
    #: 结构类里能定位的（排比串）给原文；定位不到具体字词的（通篇零具体、
    #: 段长等长）给空元组，前端据此不下划线。
    words: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {"id": self.id, "severity": self.severity, "count": self.count,
                "where": self.where, "detail": self.detail,
                "words": list(self.words)}


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
    #: 并列串要占这一段的多少字才算"排比**占满一段**"（依据见
    #: `patterns/anti-ai-smell.md` §一「排比三连占满一段」）。
    #: 为什么必须补这条：实现原来只要段里出现一处 ≥3 项并列就报，于是
    #: 「说清楚小区名、几号楼、哪部梯」这种**自然列举**也被算成排比 ——
    #: A-3 误报集实测 6/23 篇正常稿栽在这条上（正常稿误报率 39%）。
    #: md 写的是"占满一段"，代码就该按"占满"判，而不是按"出现过"判。
    PARALLEL_COVERAGE = 0.5

    def _parallel_triple(self, sections) -> list[TellHit]:
        """排比三连：同一段里 ≥3 个由「、」连接、字数相近的并列项，**且占满该段**。"""
        hits = []
        for i, s in enumerate(sections, 1):
            spoken = _count(s.get("text", ""))
            for clause_run in re.findall(r"[\u4e00-\u9fa5A-Za-z0-9]+(?:、[\u4e00-\u9fa5A-Za-z0-9]+){2,}",
                                         _plain(s.get("text", ""))):
                items = clause_run.split("、")
                lens = [len(x) for x in items]
                # 长度极差 ≤3 才算"工整"；差得远是自然列举，不是排比
                if max(lens) - min(lens) > 3:
                    continue
                # 并列串自己的字数（不含「、」）要吃掉这一段一半以上 ——
                # 段里顺带列举三样，与整段就是在排比，是两件事。
                body = len(clause_run) - (len(items) - 1)
                cover = (body / spoken) if spoken else 0.0
                if cover < self.PARALLEL_COVERAGE:
                    continue
                hits.append(TellHit("parallel_triple", self.sev("parallel_triple"),
                                    len(items), f"第{i}段",
                                    f"{len(items)} 连并列占该段 {cover:.0%}：{clause_run[:24]}…",
                                    words=(clause_run,)))
        return hits

    def _list_enumeration(self, sections) -> list[TellHit]:
        """清单体：连续 ≥4 句以「第一/第二/第三」或「一是/二是」递增开头。

        刻意保守 —— banwords 曾因为裸「第一」造成 169/175 轮假命中
        （「第一件事」「第一步」是正常用法），所以这里要求**递增序列**，
        孤立出现一次不算。

        ⚠ 阈值是 **≥4** 不是 ≥3（2026-09-23 A-4 决策预实验，`0864e73`）：
        电梯包 101 篇产物里「正确做法就三步」的**标准答法模板**刚好凑 3 句
        递增，≥3 就把这类正当内容结构 94 段全误判成清单体（97% 命中、
        无区分度）；提到 ≥4 后 101 篇命中归零、误伤清零。≥4 的真问题稿
        （连排 4+ 步的机械清单）照样抓。
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
            if len(seq) >= 4:
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
        """通篇零具体：没有数字、没有可数事实，也没有留占位。

        这是 voice.md 自己标为"最致命"的一条，所以默认 strong。
        有 `{{待补}}` 可以不算命中 —— 那是"知道这里缺事实"，比编一个数字诚实；
        但**豁免有上限**（A-1）：占位数超过段数就不是"留了个空"而是整篇没写。
        光有上限还堵不住"每段恰好一个空"（8 段 8 个 → 8≤8 照样豁免），所以再加一条下限：
        **念出来的字数为 0 时不豁免**。两条各堵一头：上限管"空得太多"，下限管"只有空"。
        """
        full = "\n".join(s.get("text", "") for s in sections)
        ph = len(_PLACEHOLDER_RE.findall(full))
        if 0 < ph <= max(len(sections), 1) and sum(_count(s.get("text", "")) for s in sections) > 0:
            return []
        if re.search(r"\d", full) or _MEASURE_RE.search(full):
            return []
        return [TellHit("no_specific", self.sev("no_specific"), max(ph, 1), "全篇",
                        "通篇无一个具体数字/时间/数量" + (f"（{ph} 处占位，超过段数）" if ph else ""))]

    # ── 词表类 ────────────────────────────────────────────
    @staticmethod
    def _first_sentence(sections) -> tuple[int, str] | None:
        """全篇第一句（连同它所在的段号）。"""
        for i, s in enumerate(sections, 1):
            for sent in _sentences(s.get("text", "")):
                return i, sent
        return None

    def _opening_ban(self, tid: str, sections, words) -> list[TellHit]:
        """开场禁区只看全篇**第一句是否以词表开头**。

        此前是全文扫描，于是正文里引用一句"大家好"也被判模板开场 ——
        strong 单次即报、一扣 12 分，误伤代价最大的就是这条（A-6 #2）。
        """
        got = self._first_sentence(sections)
        if not got:
            return []
        i, sent = got
        w = next((w for w in sorted(words, key=lambda w: -len(w)) if sent.startswith(w)), None)
        if not w:
            return []
        return [TellHit(tid, self.sev(tid), 1, f"第{i}段",
                        f"开场以「{w}」起：{sent[:16]}…", words=(w,))]

    def _lexical(self, tid: str, sections) -> list[TellHit]:
        words = self.lexicon.get(tid) or []
        if not words:
            return []
        scope = LEX_SCOPE.get(tid, "any")
        if scope == "first_sent":
            return self._opening_ban(tid, sections, words)
        hits = []
        for i, s in enumerate(sections, 1):
            if scope == "cta_only" and s.get("type") != "cta":
                continue          # 口号式收尾只可能出现在结尾引导段（A-6 #1）
            text = _plain(s.get("text", ""))
            if scope == "cta_only" and (re.search(r"[你您]", text) or "？" in text or "?" in text):
                continue          # 对着人说话/在提问，不是喊口号 —— spec 的豁免
            found = _scan_words(text, words)
            if not found:
                continue
            n = sum(found.values())
            # strong 单次即报；其余词表类按弱处理，需累计到 WEAK_MIN 才报。
            if self.sev(tid) == STRONG or n >= WEAK_MIN:
                hits.append(TellHit(tid, self.sev(tid), n, f"第{i}段",
                                    "、".join(f"{w}×{c}" for w, c in sorted(
                                        found.items(), key=lambda kv: -kv[1])[:4]),
                                    words=tuple(found)))
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
        # 占位密度（A-1）：`{{待补}}` 是"知道这里缺事实"，但整篇都是空就是没写。
        # 这个字段以前不存在，于是"全是待补"在整条链路上隐形 —— 满分通道。
        # per_100 按**念出来的字数**算（_count 已剥掉占位本身）；字数为 0 时记 null，
        # 因为"每百字几个"在没有正文时是个无意义数，不能拿 0 冒充"很干净"。
        ph = len(_PLACEHOLDER_RE.findall("\n".join(s.get("text", "") for s in sections)))
        spoken = sum(_count(s.get("text", "")) for s in sections)
        return {"score": score_of(hits),
                "hits": [h.as_dict() for h in hits],
                "strong": sum(1 for h in hits if h.severity == STRONG),
                "weak": sum(1 for h in hits if h.severity == WEAK),
                "placeholders": {"count": ph,
                                 "per_100": round(ph / spoken * 100, 1) if spoken else None,
                                 "cap": max(len(sections), 1)},
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
