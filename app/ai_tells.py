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

import math
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

# 弱 tell 的**默认**最低命中数。包可以在 yaml 的 `thresholds:` 里按 tell 覆盖（A-9 schema），
# 但覆盖**不会静默生效**：非默认值一律进 `config_warnings`，因为 yaml 里改这个数字
# 等于改评分口径，必须和 `score_of()` 一起看（这条立场自 2026-09-23 起随 schema 一起修订）。
WEAK_MIN = 2

#: 词表类 tell 的**取词范围**（A-6 收敛：spec 与实现此前不是一条尺子）。
#:   cta_only   —— 只看结尾引导段；该段有第二人称或在提问，就是对着人说话而不是喊口号。
#:   cta_strict —— 只看结尾引导段，**不做**第二人称豁免（A-9）。为什么必须另开一档：
#:                 「希望对你有帮助」这类助手腔收尾**本身就含"你"**，套用 `cta_only`
#:                 的豁免会被恒豁免、这条 tell 永不响 —— 而"对着屏幕外的读者自说自话"
#:                 恰恰就是它的判据（对话对象错置）。
#:   first_sent —— 只看全篇第一句「以」词表开头；正文里引用一句"大家好"不是模板开场。
#:   缺省 any   —— 全文扫描（其余词表类）。
LEX_SCOPE = {"slogan_closing": "cta_only", "opening_ban": "first_sent",
             "helper_closing": "cta_strict"}

#: 词表条目的正则形态前缀（A-9）。**只有 `first_sent` 那一条路认它**
#: —— `_scan_words` 是 `re.escape` 字面量匹配，正则混进别的类会被当成一串普通字符，
#: 得到一条"配了词却永不命中"的死词。用错地方由 `config_warnings` 说出口。
RE_PREFIX = "re:"

#: 无来源归因的出处豁免窗口（A-9）：命中词之后这么多字内出现具体数字/文号，
#: 说明这是一句**有出处的真归因**（「市场监管总局数据显示，2025 年…」），不该报。
ATTRIB_WINDOW = 20
#: 需要这条豁免的 tell（判据是"归因有没有出处"，别的词表类不适用）。
ATTRIB_EXEMPT = ("unattributed",)

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

#: 「留了个空」与「整篇都是空」的分界（A-1）：**全篇均摊每个占位要有这么多念出来的
#: 正文来撑**（判定式 `spoken ≥ MIN_CHARS × 占位数`）。来处是实测而不是拍脑袋
#: （2026-09-29，`generated/*/*/result.json` 102 篇）：本仓产物全篇念出字数
#: min 126 / 中位 240 / max 268，段数 3~5。按 100 字/空，正文零具体的一篇中位稿
#: 最多容 2 个空；第 3 个空起，稿子的骨架就是空洞本身。
#: ⚠ 口径是**均摊**不是逐空（审查报告-20260930 M4：注释原先写"每个占位至少"，
#:   实现给不了逐空承诺）。为什么均摊是对的：这条下限堵的是"整篇被空撑满"，
#:   与"占位数 > 段数"的上限同族，都是**整篇**形状的判据；逐空判定会把
#:   "3 个空里 1 个恰好贴着一句引子"误判成骨架稿，误伤面更大。
#: ⚠ 这条只在**正文一个具体事实都没有**时才会被走到（`_no_specific` 先查正文），
#:   所以它判的是"这篇稿子除了空之外还剩什么"，不是"这篇稿子有几个空"。
PLACEHOLDER_MIN_CHARS = 100

#: 「先立后破」对照句式的**同句**形态（A-9 `contrast_frame`）。
#: 为什么两个空档之间限 24 字：口播一句大约 15~25 字（见 §口播层），
#: 跨过这个长度就不是同一个修辞框架里的两半，而是两句无关的话被拼接了 ——
#: 那正是跨句开放形态误伤面大的来源（A-8 句式类的教训）。
_CONTRAST_GAP = r"[\s\S]{0,24}"
_CONTRAST_PATTERNS: tuple[tuple[re.Pattern, str], ...] = tuple(
    (re.compile(p), label) for p, label in (
        (r"不是" + _CONTRAST_GAP + r"(?:而是|更是在|而是在)", "不是…而是"),
        (r"并非" + _CONTRAST_GAP + r"而是", "并非…而是"),
        (r"不仅仅?是" + _CONTRAST_GAP + r"更是", "不仅仅是…更是"),
        (r"与其说" + _CONTRAST_GAP + r"不如说", "与其说…不如说"),
        (r"看似" + _CONTRAST_GAP + r"实则", "看似…实则"),
        (r"你以为" + _CONTRAST_GAP + r"其实", "你以为…其实"),
        (r"[\u4e00-\u9fa5A-Za-z0-9]{0,12}不重要[，,]?\s*重要的是", "X 不重要，重要的是 Y"),
    ))


def _plain(text: str) -> str:
    """剥掉 {{待补：…}}、[画面：…]、**加粗** 标记，便于按"念出来的话"判断。"""
    text = re.sub(r"\{\{[^}]*\}\}", "", text)
    text = re.sub(r"\[画面：[^\]]*\]", "", text)
    return re.sub(r"\*\*", "", text)


def _count(text: str) -> int:
    text = re.sub(r"\d+", "0", _plain(text))
    return len(re.sub(_PUNCT, "", text))


def _placeholder_stats(sections: list[dict]) -> tuple[list[str], int, int]:
    """(占位原文, 占位数, 念出来的字数)。

    `_no_specific` 的判定与 `report()` 的面板**必须同源**：以前两边各算各的，
    于是出现过"面板显示每百字 7.5 个空、闸门却说这篇很干净"。
    念出的字数走 `_count`（已剥掉占位本身、`[画面：…]` 与标点）。
    """
    full = "\n".join(s.get("text", "") for s in sections)
    found = _PLACEHOLDER_RE.findall(full)
    return found, len(found), sum(_count(s.get("text", "")) for s in sections)


def _sentences(text: str) -> list[str]:
    """按句末标点切句（停顿符 ／ 也算断句，口播稿里它就是句号）。"""
    parts = re.split(r"[。！？!?；;]|／", _plain(text))
    return [p.strip() for p in parts if p.strip()]


def _lex_entry(item) -> tuple[str, dict | None]:
    """一条词表条目 → (词, 限定说明 | None)。

    A-9 schema #1「解析层兼容旧形态」：三种写法都收，老 yaml 一个字都不用改。

        闭环                                      → ("闭环", None)
        {闭环: 仅黑话义}                           → ("闭环", {"note": "仅黑话义"})
        {闭环: {note: 仅黑话义, unless: [工单闭环]}}  → ("闭环", {"note":…, "unless":[…]})

    `unless` 是**机器可判**的那一半（命中处撞上这些短语就放过），
    `note` 是给人读的那一半（跟着命中进反馈文案，免得把行业真术语当黑话改）。
    """
    if isinstance(item, str):
        return item.strip(), None
    if isinstance(item, dict) and len(item) == 1:
        (word, spec), = item.items()
        if not isinstance(word, str) or not word.strip():
            return "", None
        word = word.strip()
        if isinstance(spec, str):
            return word, ({"note": spec.strip()} if spec.strip() else None)
        if isinstance(spec, dict):
            out: dict = {}
            note = spec.get("note")
            if isinstance(note, str) and note.strip():
                out["note"] = note.strip()
            unless = spec.get("unless")
            if isinstance(unless, str):
                unless = [unless]
            if isinstance(unless, list):
                keep = [str(x).strip() for x in unless if str(x).strip()]
                if keep:
                    out["unless"] = keep
            return word, (out or None)
        return word, None
    return "", None


def _has_specific_tail(text: str, at: int, window: int) -> bool:
    """命中词**之后** `window` 字里有没有具体数字 / 「数词+量词」（出处豁免用）。"""
    tail = text[at:at + window]
    return bool(re.search(r"\d", tail) or _MEASURE_RE.search(tail))


def _scan_words(text: str, words, rules: dict[str, dict] | None = None,
                tail_window: int = 0) -> dict[str, int]:
    """长词优先的**不重叠**扫描（同一位置只算最长的词）。

    为什么不逐个 `text.count(w)`：「绝对」与「绝对化」会双计，
    一个位置报两处命中，分数被虚高 —— banwords 已经为这个付过学费。

    两个可选的**豁免**（"报得准"是这条尺子的另一半）：
      · `rules` —— 词义限定：命中处与 `unless` 短语重叠就放过（「维保工单闭环」
        是行业真术语，不是黑话「闭环」）。只看**重叠**而不是"段里出现过"，
        否则同段别处提过一次「工单闭环」就能把整段的「闭环」全赦了。
      · `tail_window` —— 无来源归因的出处豁免：命中后这么多字内有数字/文号就放过
        （「市场监管总局数据显示，2025 年…」是有出处的真归因）。

    ⚠ `re:` 前缀的正则条目**不在这里处理**：本函数按字面量扫描，正则当字面量扫
      只会得到一条"配了词却永不命中"的死词。这类条目在 `AITells.__init__` 就被
      剔出 `words` 并写进 `config_warnings`；这里再挡一道（传进来也不参与匹配）。
    """
    ws = sorted({w for w in words if w and len(w) >= 2 and not w.startswith(RE_PREFIX)},
                key=lambda w: (-len(w), w))
    if not ws:
        return {}
    pat = re.compile("|".join(re.escape(w) for w in ws))
    out: dict[str, int] = {}
    for m in pat.finditer(text):
        w = m.group(0)
        if tail_window and _has_specific_tail(text, m.end(), tail_window):
            continue
        for u in ((rules or {}).get(w) or {}).get("unless", ()):
            lo = max(0, m.start() - len(u) + 1)
            if u in text[lo:m.end() + len(u) - 1]:
                break
        else:
            out[w] = out.get(w, 0) + 1
    return out


#: 严重度的中文读法。**这一处是源头**：界面 `words.js: SEVERITY_LABEL` 与
#: 导出的 `脚本.md` 都从这一族词走 —— 交付物里印 `strong` / `[strong] bookish_connective`
#: 是把内部标识放进给人拿走的那份文件（第 28 轮导出线 P1）。
#: tell 的 `id` 只在 `result.json` 里留（它是机器可读的键，不是给人读的话）。
SEVERITY_WORD = {"strong": "明显", "weak": "轻微"}


def severity_word(sev: str) -> str:
    """认不出的严重度原样返回，不猜、也不留空。"""
    return SEVERITY_WORD.get(sev or "", sev or "")


#: ── L2 形态指标（纯统计、**只展示不设门**）──────────────────────
#: 为什么不进门槛（需求方案 §2.1 的 L2 定义 + 调研报告-20260928 §3.5）：
#: humanizer（TempParaphrase）与解码参数（How Sampling Affects Detectability,
#: Findings of EMNLP 2025）都能把困惑度/突发度这一类统计量**优化过去** ——
#: 一旦拿它当门，门本身就成了被攻击的目标函数。所以这一组数的定位是
#: "把稿子的形态摆出来给人看"，界面上必须写明"参考，不作判定依据"。
#:
#: ⚠ 口语体的基线本来就和书面语不同（重复多、句长短、多样性低）：
#: slopbuster 自测**句长 CV 在其语料上没有判别力**，所以这里不报句级 CV，
#: 只报分句长度的 p50/p90/>30 字占比（>30 字必拆是口播铁律，有出处）。
#: 虚词/语气词两张表是**通用中文**，不是行业词（本文件允许，行业词必须进 yaml）。
_FUNCTION_WORDS = ("的", "了", "着", "过", "和", "与", "或", "也", "就", "都",
                   "而", "是", "在", "有", "这", "那", "把", "被", "给", "让", "到")
#: 语气词与口语标记（正向信号：越高越像人说话，与 §3.5 的"语气词占比"同源）。
#: ⚠ 不收"其实呢"这种**含已列单字**的多字标记：`呢` 已经数过一次，再数一次
#:   会把同一个语气信号计两遍，密度就虚高了（这一格只展示，也要展示得对）。
_PARTICLES = ("呢", "吧", "啊", "吗", "嗯", "呀", "哦", "嘛", "你看", "你想")
#: MATTR 的滑动窗口字数。取 50 是为了在口播段落（250~450 字全文）上
#: 还有若干个窗口可用；窗口比正文长时退化成一次 TTR。
_MATTR_WINDOW = 50
#: 形态统计的分隔符。⚠ **从 `_PUNCT` 派生**（剥掉外层字符类括号复用）：
#: 形态流的口径必须与 `_count` 完全一致 —— 以前这里手写了一份更窄的标点表，
#: 漏了停顿符 `／` 与引号括号，`／` 被当成"字"灌进 mattr/3-gram/虚词熵，
#: 12 篇真实产物字数 +4.6%（审查报告-20260930 M2）。派生之后两处永不漂。
_SHAPE_SPLIT = re.compile("[" + _PUNCT[1:-1] + "]*")


def _char_stream(text: str) -> str:
    """形态统计用的字符流：剥标记、去空白与标点，只留"念出来的字"。"""
    return "".join(_SHAPE_SPLIT.split(_plain(text)))


def _pct(values: list[float], q: float) -> float | None:
    """最近秩百分位（十几二十个数用，够准且无依赖 —— 不为此引 numpy）。"""
    if not values:
        return None
    s = sorted(values)
    idx = min(len(s) - 1, max(0, int(round(q * (len(s) - 1)))))
    return float(s[idx])


def _mattr(chars: str, window: int = _MATTR_WINDOW) -> float | None:
    """MATTR：滑动窗口 TTR 的均值（对长度鲁棒的词汇多样性）。"""
    n = len(chars)
    if n < 2:
        return None
    if n <= window:
        return round(len(set(chars)) / n, 3)
    wins = [len(set(chars[i:i + window])) / window for i in range(0, n - window + 1)]
    return round(statistics.fmean(wins), 3)


def _content_trigram_repeat(chars: str) -> float | None:
    """实词 3-gram 重复率：1 − unique/total（虚词先屏蔽掉）。

    ⚠ 屏蔽是把虚词**删掉**再切 gram，相邻字会粘起来 —— 这是无分词器前提下的近似，
    所以它只做"看得见的重复有多高"的诊断，不能当判据。
    """
    kept = "".join(c for c in chars if c not in _FUNCTION_WORDS)
    grams = [kept[i:i + 3] for i in range(len(kept) - 2)]
    if len(grams) < 3:
        return None
    return round(1 - len(set(grams)) / len(grams), 3)


def _funcword_entropy(chars: str) -> float | None:
    """虚词频率分布的熵（bit）。LLM 的虚词使用更"可预测" → 熵偏低。"""
    counts = [chars.count(w) for w in _FUNCTION_WORDS]
    total = sum(counts)
    if total <= 0:
        return None
    e = -sum((c / total) * math.log2(c / total) for c in counts if c)
    return round(e, 3)


def shape_metrics(sections: list[dict], idioms: list[str] | None = None) -> dict:
    """L2 形态面板的六个信号（调研报告-20260928 §3.5 定型的六个）。

    每个都可能为 `None`（= 这份稿子短到算不出，或这个包没配成语表）——
    界面据此画「—」而不是 0：**"没测"与"测了、很好"必须是两种形状**（与
    `ai_tells` 落 null 同一条规矩）。
    """
    text = "\n".join(s.get("text", "") for s in sections)
    # 分子与分母同口径：`chars`（分母）是**剥掉 `{{待补}}`/`[画面：…]`/加粗与标点**之后的
    # 字流，所以语气词与成语也必须在剥过的文本上数 —— 否则镜头提示里的字会算进密度，
    # 而那些字根本不会被念出来（实测：13 个字的正文配着 37 字的原文，密度被顶到 7.69）。
    spoken_text = _plain(text)
    chars = _char_stream(text)
    spoken = len(chars)
    clauses = [c for c in re.split(r"[，。！？；、,.;!?]", spoken_text) if c.strip()]
    lens = [float(_count(c)) for c in clauses]
    particle_hits = sum(spoken_text.count(p) for p in _PARTICLES)
    idiom_hits = sum(spoken_text.count(i) for i in (idioms or []) if i)
    return {
        "mattr": _mattr(chars),
        "trigram_repeat": _content_trigram_repeat(chars),
        "clause_len": {"n": len(lens),
                       "p50": _pct(lens, 0.5), "p90": _pct(lens, 0.9),
                       "gt30": round(sum(1 for x in lens if x > 30) / len(lens), 3) if lens else None},
        "particles_per_100": round(particle_hits / spoken * 100, 2) if spoken else None,
        "funcword_entropy": _funcword_entropy(chars),
        # 成语密度：表在**包里**（`ai_tells.yaml` 的 `idioms:`），代码里零具体词；
        # 没配表 → null（没测），而不是 0（"一篇都没有"是另一个结论）。
        "idioms_per_1000": round(idiom_hits / spoken * 1000, 2)
        if idioms and spoken else None,
        "chars": spoken,
    }


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
                  "repeat_opening", "no_specific", "contrast_frame")
    #: 词表类 tell（yaml 的 lexicon 给词）
    LEXICAL = ("bookish_connective", "abstract_noun_ending", "slogan_closing",
               "opening_ban", "filler_stack",
               # A-9 三条（调研报告-20260928 §3.2）：无来源归因 / 悬念互动套路 /
               # 助手腔收尾。前两条是四源交叉证实的信号，不是拍脑袋加的词。
               "unattributed", "cliche_hook", "helper_closing")
    ALL = STRUCTURAL + LEXICAL

    def __init__(self, data: dict | None):
        data = data or {}
        self.severity: dict[str, str] = {}
        for k in ("strong", "weak"):
            for tid in (data.get(k) or []):
                self.severity[tid] = STRONG if k == "strong" else WEAK
        # 词表条目 → 词 + 限定说明（A-9 schema：兼容旧的纯字符串写法，见 `_lex_entry`）
        self.lexicon: dict[str, list[str]] = {}
        self.word_rules: dict[str, dict[str, dict]] = {}
        #: 各类的 `re:` 正则条目：{tell: [(原文, 编译后的 pattern), …]}
        self.lex_regex: dict[str, list[tuple[str, re.Pattern]]] = {}
        #: 读不出、用错了、编译不了的条目：**说出口而不是静默丢掉**
        self.bad_entries: list[str] = []
        # `re:` 只有 first_sent 那条路认识（`_scan_words` 是字面量匹配）。
        first_sent_tells = sorted(k for k, v in LEX_SCOPE.items() if v == "first_sent")
        for cat, raw in (data.get("lexicon") or {}).items():
            words: list[str] = []
            rules: dict[str, dict] = {}
            regexes: list[tuple[str, re.Pattern]] = []
            for item in (raw or []):
                w, spec = _lex_entry(item)
                if not w:
                    self.bad_entries.append(
                        f"lexicon.{cat} 有一条读不出词形（{item!r}）—— 已跳过")
                    continue
                if w.startswith(RE_PREFIX):
                    if spec:
                        # 正则条目带限定 = 限定被静默丢弃（continue 在 spec 处理之前，
                        # 审查报告-20260930 M5）：说出口，别让包作者以为限定生效了。
                        self.bad_entries.append(
                            f"lexicon.{cat} 的正则条目 {w!r} 带 note/unless —— first_sent "
                            "（句首）路径只做句首匹配、不消费词义限定，这些限定**不生效**")
                    body = w[len(RE_PREFIX):]
                    try:
                        regexes.append((body, re.compile(body)))
                    except re.error as e:
                        self.bad_entries.append(
                            f"lexicon.{cat} 的正则条目 {body!r} 编译不了（{e}）—— 已跳过")
                    continue
                words.append(w)
                if spec:
                    # 豁免短语必须是 ≥2 字的**具体**短语：单字（如 `环`）会与任何命中重叠，
                    # 于是整条 tell 被静默打死 —— 与本文件对词表的一贯立场相同，
                    # 宁可报出来，也不要一条"配了却不跑"的死判据。
                    unless_all = [str(x) for x in (spec.get("unless") or [])]
                    unless_ok = [u for u in unless_all if len(u) >= 2]
                    short = [u for u in unless_all if len(u) < 2]
                    if short:
                        self.bad_entries.append(
                            f"lexicon.{cat} 的条目 {w!r} 的 unless 里有不到 2 字的短语（{short}）"
                            " —— 这种豁免会把整条 tell 静默打死，已丢掉")
                    spec = {k: v for k, v in spec.items() if k != "unless"}
                    if unless_ok:
                        spec["unless"] = unless_ok
                if spec:
                    rules[w] = spec
                if spec and cat in first_sent_tells:
                    # first_sent 路径（`_opening_ban`）只做句首匹配，不读 `word_rules`
                    # —— note 不进 detail、unless 不豁免（审查报告-20260930 M5：
                    # 实测 `大家好不容易聚齐…` 作首句照报）。限定写在这儿不会生效，
                    # 必须说出口，别让包作者以为「三种形态引擎都认」。
                    self.bad_entries.append(
                        f"lexicon.{cat} 的条目 {w!r} 带 note/unless —— first_sent（句首）"
                        "路径当前不消费词义限定（句首命中即报），这些限定**不生效**")
            self.lexicon[cat] = words
            if rules:
                self.word_rules[cat] = rules
            if regexes:
                self.lex_regex[cat] = regexes
        # `re:` 只有 first_sent 那条路认识（`_scan_words` 是字面量匹配）。
        # 用错类别 = 一条"配了词却永不命中"的死词，必须说出口（A-9 §8.3 #4）。
        for cat in list(self.lex_regex):
            if cat in first_sent_tells:
                continue
            self.bad_entries.append(
                f"lexicon.{cat} 用了 `re:` 正则条目，而这一类走字面量扫描"
                f"（认正则的只有 {first_sent_tells}）—— 这些条目已跳过，"
                "否则它长得像「配了词」却永远不命中")
            self.lex_regex.pop(cat)
        #: 门槛的按包覆盖（A-9 schema #2）。**只有"段内累计"那一族吃它**：
        #: weak 的词表类（`_lexical` 的 `WEAK_MIN`）与 **weak 声明的** `contrast_frame`
        #: （同口径）。strong 是"单次即报"，与门槛在定义上互斥；其余结构类各有自己的
        #: 判据（≥3 项并列 / 递增 ≥4 句 / cv<0.15 / 句首 >1/3），代码不读 `thresholds`。
        #: 把这些键写进来是 **no-op**，而包作者会以为换了尺子 —— 所以报出来并不收下
        #: （收下就会在 `config_warnings` 里印"分数与别的包不可比"，那是假话）。
        self.thresholds: dict[str, int] = {}
        raw_th = data.get("thresholds") or {}
        if not isinstance(raw_th, dict):
            self.bad_entries.append(
                f"thresholds 需要 {{tell 名: 次数}} 映射，现在是 {type(raw_th).__name__}"
                f" —— 已按默认口径 {WEAK_MIN}")
            raw_th = {}
        for tid, v in raw_th.items():
            sev_here = self.severity.get(tid)
            if tid == "contrast_frame" and sev_here is None:
                # 门槛配给了**未声明**的 tell：它根本不跑，配了是 no-op ——
                # 不能照走"记门槛、印非默认口径"那条路（那是把 no-op 说成换了尺子，
                # 审查报告-20260930 E8）
                self.bad_entries.append(
                    f"thresholds.{tid} 配给了**未声明**的 tell（yaml 没在 strong/weak 里"
                    "声明它，这条 tell 根本不跑）—— 已忽略")
                continue
            if tid == "contrast_frame" and sev_here == STRONG:
                # strong 与门槛在定义上互斥（审查报告-20260930 E6；`_contrast_frame`
                # 对 strong 单次即报，门槛不参与）
                self.bad_entries.append(
                    f"thresholds.{tid} 对 strong 不生效 —— strong 是单次即报，"
                    "只有 weak 才吃段内门槛 —— 已忽略，别以为尺子换了")
                continue
            eats = ((tid in self.LEXICAL and sev_here == WEAK)
                    or (tid == "contrast_frame" and sev_here == WEAK))
            if not eats:
                self.bad_entries.append(
                    f"thresholds.{tid} 对这一类不生效 —— 只有 weak 的词表类与 "
                    "contrast_frame 吃门槛（strong 是单次即报，其余结构类有自己的判据）"
                    f" —— 已忽略，别以为尺子换了")
                continue
            if isinstance(v, bool):
                self.bad_entries.append(
                    f"thresholds.{tid} 是布尔值（{v!r}），不是次数 —— 已按默认口径 {WEAK_MIN}")
                continue
            str_form = isinstance(v, str)
            try:
                num = float(v)
            except (TypeError, ValueError):
                self.bad_entries.append(
                    f"thresholds.{tid} 不是整数（{v!r}）—— 已按默认口径 {WEAK_MIN}")
                continue
            if str_form:
                # "2" 会被 float 偷偷收下 —— 值碰巧不错，但形态错了必须说出口
                # （与「很多」→警告、2.9→警告同一条立场；审查报告-20260930 E7）
                self.bad_entries.append(
                    f"thresholds.{tid} 写成了字符串（{v!r}）—— 门槛要的是数字；"
                    f"值能读（按 {int(num)} 照用），但请改写成数字")
            if num != int(num):
                # 2.9 静默截成 2 是这条仓里反复治过的"配置者以为配了 A、实际跑的是 B"
                # （同一条立场见 `pipeline` 对 `limits.recheck_rounds: 2.9` 的处理）
                self.bad_entries.append(
                    f"thresholds.{tid} 是 {v!r}（不是整数）—— 已按默认口径 {WEAK_MIN}，"
                    "门槛没有「半个」这种数")
                continue
            n = int(num)
            if n < 1:
                self.bad_entries.append(
                    f"thresholds.{tid} 是 {n}（须 ≥1）—— 已按默认口径 {WEAK_MIN}")
                continue
            self.thresholds[str(tid)] = n
        self.source = data.get("md") or ""
        #: L2 成语密度用的成语表（**在包里不在代码里**，与 banwords/lexicon 同一分工）。
        #: 没配 → `idioms_per_1000` 落 null（"没测"），不冒充 0（"一篇都没有"）。
        #: ⚠ 写成字符串会被逐字切开、于是每个单字都在数密度 —— 与 `lexicon` 同一类
        #:   静默失效，所以这里过一遍形状并把不到 2 字的丢掉说出口。
        raw_idioms = data.get("idioms") or []
        if not isinstance(raw_idioms, list):
            self.bad_entries.append("idioms 需要字符串列表（L2 成语表）—— "
                                    "本包的「成语密度」那一格会显示为没配表")
            raw_idioms = []
        self.idioms = [str(x).strip() for x in raw_idioms if len(str(x).strip()) >= 2]
        # 未知 tell 名必须报出来：写错一个字母（parallel_triple → parallel_triiple）
        # 的后果是"这条 tell 永远不跑"，而报告看起来完全正常。
        # ⚠ 必须先并集再减：`|` 的优先级低于 `-`，写成
        # `set(a) | set(b) - set(ALL)` 会被解析成 `a | (b - ALL)` ——
        # 于是**每一个合法名字**都被报成未知（本模块原来就是这个错，
        # 由 tests/test_ai_tells.py 抓出），守卫本身成了噪声源。
        self.unknown = sorted((set(self.severity) | set(self.lexicon)) - set(self.ALL))
        # "声明了却没有词"= 死 tell。⚠ `re:` 条目也算词（A-9）：一类的条目若**全部**
        # 是正则（如开场禁区只收框架），按纯字面量列表判会把它误报成空 ——
        # 判据要跟着 schema 一起改，否则守卫自己造噪声。
        self.lexicon_missing = [t for t in self.LEXICAL
                                if t in self.severity
                                and not (self.lexicon.get(t) or self.lex_regex.get(t))]

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
        光有上限还堵不住"每段恰好一个空"（8 段 8 个 → 8≤8 照样豁免），所以再加下限：
        **念出来的字数为 0 时不豁免**，以及**全篇均摊每个空要有 `PLACEHOLDER_MIN_CHARS`
        字正文撑着才豁免**（`spoken ≥ MIN_CHARS × 占位数`；实测：8 段各配一句 13 字的
        引子 + 一个空，正文零具体 → 旧判据只看 `spoken > 0`，一句废话就把 strong
        买通了，人味分照样 100）。

        ⚠ 还有第三头（P1）：豁免的前提是占位标的是「**还不知道**的事实」，
          而 `{{待补：3天}}` / `{{待补：2026-09-01}}` 这类**把具体数值写进占位**的
          形态不是「缺事实」而是「事实躺在占位里」——正文一个数字都没有，
          strong 零命中、人味分满分，而 voice.md 恰恰把它列为最致命的一类。
          判据：占位里含数字或量词就**不豁免**（这条不依赖正文，正文那条另判）。
        """
        full = "\n".join(s.get("text", "") for s in sections)
        ph_texts, ph, spoken = _placeholder_stats(sections)
        # 拒绝豁免的理由要说得出是哪一条（四条各堵一头，含糊成"占位太多"等于没说）
        denial = ""
        if ph:
            # 占位里含具体数字 / 量词 → 那不是「缺事实」，不豁免（先判，因为它与
            # 「占位数量是否超上限」无关）
            if any(re.search(r"\d", p) or _MEASURE_RE.search(p) for p in ph_texts):
                denial = "占位里写着待确认的数值，不是缺事实"
            elif ph > max(len(sections), 1):
                denial = f"{ph} 处占位超过 {len(sections)} 段"
            elif spoken == 0:
                denial = "整篇只有占位、没有念得出口的正文"
            elif spoken < PLACEHOLDER_MIN_CHARS * ph:
                denial = (f"{ph} 处占位只剩 {spoken} 字正文来撑，"
                          f"均摊每个空不足 {PLACEHOLDER_MIN_CHARS} 字")
            if not denial:
                return []
        # 「有没有可数的事实」按**剥掉占位与画面标注之后**的正文判（`_plain` 与
        # `_count`/`_char_stream` 同一条口径）。直接扫 `full` 的话
        # `{{待补：3天}}` 里那个 `3` 会让整篇豁免（P1）；`{{待补：三天}}` 同理；
        # `[画面：字幕 2025 年新规]` 同理 —— 画面提示不进嘴，不该买通豁免
        # （审查报告-20260930 M3：修复前只剥占位，画面里的数字把 strong 静默豁免）。
        body = _plain(full)
        if re.search(r"\d", body) or _MEASURE_RE.search(body):
            return []
        return [TellHit("no_specific", self.sev("no_specific"), max(ph, 1), "全篇",
                        "通篇无一个具体数字/时间/数量" + (f"（{denial}）" if denial else ""))]

    # ── 词表类 ────────────────────────────────────────────
    @staticmethod
    def _first_sentence(sections) -> tuple[int, str] | None:
        """全篇第一句（连同它所在的段号）。"""
        for i, s in enumerate(sections, 1):
            for sent in _sentences(s.get("text", "")):
                return i, sent
        return None

    def _opening_ban(self, tid: str, sections, words) -> list[TellHit]:
        """开场禁区只看全篇**第一句是否以词表开头**（`re:` 条目要求从句首起匹配）。

        此前是全文扫描，于是正文里引用一句"大家好"也被判模板开场 ——
        strong 单次即报、一扣 12 分，误伤代价最大的就是这条（A-6 #2）。

        为什么唯独这一类要收正则（A-9 §七 P0-2）：「在当今…时代」「随着…的发展」
        是**框架**，中间的字枚举不完；而它只有在句首才算模板开场 ——
        所以 `re:` 只在这条 first_sent 路径上有效，别的类走字面量扫描。
        """
        got = self._first_sentence(sections)
        if not got:
            return []
        i, sent = got
        hit: str | None = next(
            (w for w in sorted(words, key=lambda w: -len(w)) if sent.startswith(w)), None)
        if hit is None:
            for _src, rx in self.lex_regex.get(tid, ()):
                m = rx.match(sent)
                # 只认**非空**命中：一条写宽了的正则若零宽匹配，等于每篇都报开场违规。
                if m and m.group(0):
                    hit = m.group(0)
                    break
        if hit is None:
            return []
        return [TellHit(tid, self.sev(tid), 1, f"第{i}段",
                        f"开场以「{hit}」起：{sent[:16]}…", words=(hit,))]

    def _contrast_frame(self, sections) -> list[TellHit]:
        """负向排比 / 对照反转：「不是…而是…」「与其说…不如说」这类**先立后破**的句式。

        为什么它不进词表而走结构类（A-9 §8.2 #2）：两个空档之间的字是开放的，
        `_scan_words` 的字面量匹配跑不了；而它又是本轮四条候选里唯一有
        **四源交叉 + 量化权重**的一条（EQ-Bench 把它单列 25%、de-ai-taste 给了
        全文 ≥3 的门槛、avoid-ai-writing-cn 给每篇 1 次的配额、Wikipedia 列为
        negative parallelism）。

        ⚠ 两个空档**必须在同一句内**（A-8 句式类的教训：跨句的开放形态误伤面大）。
        ⚠ 计数按**段**（与词表类同一口径：一个句式在一句话里是修辞，一段里排两颗
        才是模板）。全篇 ≥3 是 de-ai-taste 的口径，留作门槛调整的备选（yaml 的
        `thresholds: {contrast_frame: N}` 可覆盖，但覆盖值会进 config_warnings）。
        """
        hits = []
        # strong 单次即报（与其余 strong tell 同一口径，审查报告-20260930 E6）；
        # weak 走段内门槛（默认 `WEAK_MIN`，yaml 的 `thresholds` 可覆盖）。
        min_n = (1 if self.sev("contrast_frame") == STRONG
                 else self.thresholds.get("contrast_frame", WEAK_MIN))
        for i, s in enumerate(sections, 1):
            n = 0
            frames: list[str] = []
            for sent in _sentences(s.get("text", "")):
                for pat, label in _CONTRAST_PATTERNS:
                    if pat.search(sent):
                        n += 1
                        frames.append(label)
                        break                      # 一句只算一次，别同一句报两处
            if n >= min_n:
                shown = "、".join(dict.fromkeys(frames))     # 去重且保序
                hits.append(TellHit("contrast_frame", self.sev("contrast_frame"),
                                    n, f"第{i}段",
                                    f"{n} 处「先立后破」对照句式：{shown}"))
        return hits

    def _lexical(self, tid: str, sections) -> list[TellHit]:
        words = self.lexicon.get(tid) or []
        scope = LEX_SCOPE.get(tid, "any")
        if scope == "first_sent":
            # ⚠ 这一条**必须在"没有词就不扫"之前**：开场禁区的 `re:` 框架条目
            #   不进 `self.lexicon`（那是字面量列表），全用正则时 `words` 就是空的，
            #   先判空会让整条 tell 安静地不跑（实测：正则收进来却一条都不报）。
            return self._opening_ban(tid, sections, words)
        if not words:
            return []
        rules = self.word_rules.get(tid) or {}
        # 出处豁免只给「无来源归因」一族：别的词表类没有"下文有没有出处"这回事。
        window = ATTRIB_WINDOW if tid in ATTRIB_EXEMPT else 0
        min_n = self.thresholds.get(tid, WEAK_MIN)
        hits = []
        for i, s in enumerate(sections, 1):
            if scope in ("cta_only", "cta_strict") and s.get("type") != "cta":
                continue          # 口号式收尾 / 助手腔收尾只可能在结尾引导段
            text = _plain(s.get("text", ""))
            if scope == "cta_only" and (re.search(r"[你您]", text) or "？" in text or "?" in text):
                continue          # 对着人说话/在提问，不是喊口号 —— spec 的豁免
            # ⚠ `cta_strict` **故意没有**上面那条豁免：助手腔收尾本身就在对"你"
            #   说话（「希望对你有帮助」），沿用豁免等于这条 tell 永不响（A-9）。
            found = _scan_words(text, words, rules, window)
            if not found:
                continue
            n = sum(found.values())
            # strong 单次即报；其余词表类按弱处理，需累计到该类的门槛才报。
            if self.sev(tid) == STRONG or n >= min_n:
                notes = "；".join(f"{w}：{(rules.get(w) or {}).get('note')}"
                                 for w in found if (rules.get(w) or {}).get("note"))
                detail = "、".join(f"{w}×{c}" for w, c in sorted(
                    found.items(), key=lambda kv: -kv[1])[:4])
                hits.append(TellHit(tid, self.sev(tid), n, f"第{i}段",
                                    detail + (f"（{notes}）" if notes else ""),
                                    words=tuple(found)))
        return hits

    # ── 汇总 ─────────────────────────────────────────────
    def _structural_handler(self, tid: str):
        """结构类的算法**按命名约定**取：`_<tell 名>`。

        为什么不再手写一份清单：清单漏登记一条，后果是"这条 tell 永不跑"，
        而对账契约比的是 `STRUCTURAL` 的名字与 yaml/规格，比不出"有没有实现"。
        约定接上之后，漏实现只会在 `warnings()` 里说出口（而不是安静地满分）。
        """
        return getattr(self, f"_{tid}", None)

    def scan(self, sections: list[dict]) -> list[TellHit]:
        """sections = [{"type","text"}, …]。返回命中列表（未过阈值的 tell 不出现）。"""
        out: list[TellHit] = []
        for tid in self.STRUCTURAL:
            if self.severity.get(tid) is None:
                continue          # yaml 没声明的 tell 不跑（默认关，而不是默认开）
            fn = self._structural_handler(tid)
            if fn is not None:
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
        _, ph, spoken = _placeholder_stats(sections)
        return {"score": score_of(hits),
                "hits": [h.as_dict() for h in hits],
                "strong": sum(1 for h in hits if h.severity == STRONG),
                "weak": sum(1 for h in hits if h.severity == WEAK),
                "placeholders": {"count": ph,
                                 "per_100": round(ph / spoken * 100, 1) if spoken else None,
                                 "cap": max(len(sections), 1)},
                "tells_enabled": sorted(self.severity),
                #: L2 形态面板（§3.5 定型的六个信号）。**只展示**：`score` 与 `hits`
                #: 一个字都不看它 —— 门会被 humanizer 与采样参数优化过去（见本文件顶部说明）。
                "shape": shape_metrics(sections, self.idioms),
                "config_warnings": self.warnings()}

    def warnings(self) -> list[str]:
        w = []
        if self.unknown:
            w.append("ai_tells.yaml 有未知 tell 名（引擎不跑它）：" + "、".join(self.unknown))
        if self.lexicon_missing:
            w.append("词表类 tell 已声明 severity 但 lexicon 为空：" + "、".join(self.lexicon_missing))
        # 结构类只按命名约定接算法：漏实现 = 这条 tell 永不跑，必须说出口
        unimplemented = [t for t in self.STRUCTURAL if self._structural_handler(t) is None]
        if unimplemented:
            w.append("结构类 tell 声明了却没有算法（引擎跑不了它）：" + "、".join(unimplemented))
        w += list(self.bad_entries)
        # 门槛被包改掉 = 评分口径变了（`WEAK_MIN` 与 `score_of()` 必须一起看）
        for tid, n in sorted(self.thresholds.items()):
            if n != WEAK_MIN:
                w.append(f"{tid} 的弱 tell 门槛被 yaml 改成 {n}（默认 {WEAK_MIN}）"
                         " —— 非默认口径，分数与别的包不可比")
        return w


#: 单条 strong 扣分 / 单条 weak 扣分。**这两个数是待校准的初值，不是结论**：
#: A4 要先拿现成产物跑出分数分布再定门槛（未校准的尺子当门槛 = banwords soft
#: 那种 169/175 轮假命中的复现路径）。
PENALTY = {STRONG: 12, WEAK: 4}


def score_of(hits: list[TellHit]) -> int:
    """0-100，越高越像人。公式刻意简单到一眼能算，先可解释再谈准确。"""
    loss = sum(PENALTY.get(h.severity, 0) for h in hits)
    return max(0, 100 - loss)
