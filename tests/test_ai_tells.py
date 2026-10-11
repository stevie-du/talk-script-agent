# -*- coding: utf-8 -*-
"""人味 tell 检测必须**接在线上**（P-新：本模块曾写完就没人调用）。

`app/ai_tells.py` 有 264 行、九条 tell、一个包级词表，但全仓库没有任何一处
import 它 —— 这就是本项目一直在治的「写了但没接上」形态（`files.*` 孤儿键、
`scenes` 空槽、raw/ 的假承诺都是同一类）。本文件守四件事：

  1. 引擎 → 包词表 → 校验报告 的链路是通的（真的能从 check_script 拿到分数）
  2. 词表写坏/写空不许伪装成「人味 100 分」
  3. 文风命中**不进 passed**（不合规才拦发布）
  4. `_count` 与 `checker.count_chars` 同一口径（模块自己的注释承诺过这一点）
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.ai_tells import AITells, WEAK, score_of     # noqa: E402
from app.checker import Banwords, Quota, check_script, count_chars  # noqa: E402
from app.config import load_config                    # noqa: E402
from app.knowledge import Pack                        # noqa: E402
from app.pipeline import Pipeline, _load_tells, wait_job  # noqa: E402
from app.schemas import GenerateRequest               # noqa: E402

AI_YAML = ROOT / "packs" / "elevator" / "ai_tells.yaml"


def _sections(**kw):
    return [{"type": "hook", "text": kw.get("hook", "电梯维保这块，众所周知， safety 性很重要。")},
            {"type": "point", "text": kw.get("p1", "首先看保养，其次看年检，最后看记录。")},
            {"type": "point", "text": kw.get("p2", "其实真的非常特别值得关注。")},
            {"type": "cta", "text": kw.get("cta", "让生活更美好，品质保证。")}]


def test_pack_vocab_loads_and_engine_recognises_every_name():
    tells = _load_tells(Pack(ROOT, "elevator"))
    assert tells is not None, "elevator 包配了 ai_tells.yaml，却没被引擎读起来"
    assert not tells.unknown, f"词表里有引擎不认识的 tell 名：{tells.unknown}"
    assert not tells.lexicon_missing, f"声明了却空着词表：{tells.lexicon_missing}"


def test_report_reaches_the_checker_without_blocking_pass():
    """一份满是 tell 的稿子：分数要掉下来，但 passed 不许因此变 False。"""
    pack = Pack(ROOT, "elevator")
    tells = _load_tells(pack)
    report = check_script(_sections(), 60, 4.5, Banwords({"hard": [], "soft": []}),
                          "抖音", Quota({}), tells=tells)
    ai = report["ai_tells"]
    assert ai is not None and ai["score"] < 100, ai
    assert ai["hits"], "构造的稿子应该被检出 tell"
    assert all(h["where"] for h in ai["hits"]), "命中必须带定位，否则改不动"
    # 「不进 passed」的正确判据：同一份稿子接上与不接 tell，合格结论必须一致。
    # 写成 `passed is True` 会变成看时长脸色（构造的稿子短，本来就不合格），
    # 那样这条断言既守不住东西又会假红。
    plain = check_script(_sections(), 60, 4.5, Banwords({"hard": [], "soft": []}),
                         "抖音", Quota({}), tells=None)
    assert plain["passed"] == report["passed"], "文风命中改动了合格判定"


def test_tells_disabled_when_pack_has_no_file(tmp_path):
    """没配词表 → ai_tells 落 null（"没测"），不能伪装成满分。"""
    import shutil
    src = ROOT / "packs" / "elevator"
    dst = tmp_path / "packs" / "elevator"
    shutil.copytree(src, dst)
    (dst / "ai_tells.yaml").unlink()
    data = yaml.safe_load((dst / "pack.yaml").read_text(encoding="utf-8"))
    pack = Pack(tmp_path, "elevator")
    assert pack.ai_tells_data() is None
    assert _load_tells(pack) is None
    report = check_script(_sections(), 60, 4.5, Banwords({"hard": [], "soft": []}),
                          "抖音", Quota({}), tells=None)
    assert report["ai_tells"] is None
    assert data  # 包本身仍可用


def test_unknown_name_is_still_caught():
    """修优先级 bug 的代价不能是把守卫一起丢掉：写错名字仍要报出来。"""
    t = AITells({"strong": ["parallel_triiple", "no_specific"]})
    assert t.unknown == ["parallel_triiple"], t.unknown
    assert "parallel_triiple" in t.warnings()[0]


def test_count_chars_agrees_with_the_module_private_one():
    """ai_tells._count 与 checker.count_chars 必须同一口径（模块注释的承诺）。"""
    from app.ai_tells import _count
    for t in ["困了 23 分钟", "**加粗**一句话，带标点。", "{{待补：小区}}那台用了 8 年",
              "电梯、扶梯、别墅梯都算",
              # P1-1：引号曾是两本口径的分裂点——checker 的字面弯引号被吞成
              # 字符串定界符（四个字符静默消失、引号按口播字计），本模块又漏了
              # ASCII 引号。这两个方向都要有样本钉住，缺一类漂移就回来。
              "他说“这个好”。", "数字\"直角引号\"测试。", "单引号‘这样’与'这样'混排。"]:
        assert _count(t) == count_chars(t), t


def test_score_is_monotone_in_hits():
    a = AITells({"strong": ["no_specific"], "weak": ["repeat_opening"]})
    clean = [{"type": "point", "text": "去年 3 月这台的抱闸换过一次，花了 2400 元。"}]
    dirty = _sections()
    assert a.report(clean)["score"] > a.report(dirty)["score"]
    assert score_of([]) == 100


def _ids(hits):
    """`scan()` 给 dataclass、`report()["hits"]` 给 dict —— 两种都要能读。"""
    return [h.id if hasattr(h, "id") else h["id"] for h in hits]


def _where(hit):
    return hit.where if hasattr(hit, "where") else hit["where"]


# ── A-1：满分通道 ─────────────────────────────────────────
def test_all_placeholder_script_no_longer_scores_perfect():
    """8 段全是 `{{待补}}` 的稿子以前拿 100 分零命中 —— 整篇没写在链路上是隐形的。"""
    t = AITells({"strong": ["no_specific"]})
    secs = [{"type": "point", "text": "{{待补：数字}}"} for _ in range(8)]
    r = t.report(secs)
    assert "no_specific" in _ids(r["hits"]), r["hits"]
    assert r["score"] < 100, r["score"]
    assert r["placeholders"] == {"count": 8, "per_100": None, "cap": 8}, r["placeholders"]


def test_honest_gaps_are_still_exempted():
    """正向对照（防止把豁免整条删掉来"修"满分通道）：正文真写满了、每段留一个空，仍不报。

    ⚠ 这份 fixture 在 2026-09-29 改过一次：原来用「每段 15 字引子 + 一个空」共 60 字，
      那不是"稿子写完了、只缺一个数"，而是**整篇由空洞组成** —— 它测不到自己想测的东西，
      还把 A-1 那条下限钉成了"任何一句废话都能买通 strong"。现在按实测产物尺度写
      （本仓 102 篇产物全篇念出字数 min 126 / 中位 240），并且**当场自检**这个前提。
    """
    t = AITells({"strong": ["no_specific"]})
    body = ("电梯停了很多时候不是坏了，是它在保护你。门没关严、平层有偏差，轿厢就会自动停住，"
            "这是设计好的保护动作。被困时最危险的动作是扒门，轿厢很可能停在楼层之间，"
            "外面不是楼道而是空的井道，扒开门真的会出事。先别慌，按下报警按钮，"
            "在门外等着救援，别自己撬门。")
    # ⚠ 占位不能写进 f-string：f-string 会把 `{{`/`}}` 折成单花括号，那就不是占位符了
    secs = [{"type": "point", "text": body + "具体数字{{待补：本地救援到场平均用时}}。"}
            for _ in range(3)]
    # 自检：这条豁免测的是"占位撑不撑得起"，正文里混进数字或量词就走的是另一条豁免
    from app.ai_tells import _MEASURE_RE, _PLACEHOLDER_RE
    stripped = _PLACEHOLDER_RE.sub("", "".join(s["text"] for s in secs))
    assert not re.search(r"\d", stripped) and not _MEASURE_RE.search(stripped), \
        "fixture 的正文已经自带具体事实，量不到占位豁免这条路径"
    r = t.report(secs)
    assert "no_specific" not in _ids(r["hits"]), r["hits"]
    assert r["placeholders"]["count"] == 3 and r["placeholders"]["per_100"] <= 1.0


def test_one_placeholder_per_section_over_a_skeleton_is_not_exempt():
    """★ A-1 剩下的那头（2026-09-29 实测）：每段恰好一个空、正文只有引子 → 必须报。

    8 段各配一句 8 字引子 + 一个 `{{待补}}`：占位数 8 ≤ 段数 8（上限放行）、
    念出字数 48 > 0（旧下限放行）、正文零具体 —— 于是旧判据整篇豁免，
    人味分 100、strong 零命中，而这正是"整篇没写"。
    现在按**每个空的正文撑不撑得起**判：48 字撑 8 个空 = 每个空 6 字 < 100 字。
    """
    t = AITells({"strong": ["no_specific"]})
    secs = [{"type": "point", "text": f"这里要展开讲，{{{{待补：案例{chr(65 + i)}}}}}。"}
            for i in range(8)]
    r = t.report(secs)
    assert "no_specific" in _ids(r["hits"]), \
        f"每段恰好一个空的骨架稿又拿到满分：{r['score']} {r['placeholders']}"
    d = [h["detail"] for h in r["hits"] if h["id"] == "no_specific"][0]
    assert "每个空不足" in d, f"报了，但说不出为什么不是「留了个空」：{d}"


def test_density_line_is_the_only_thing_denying_that_exemption():
    """边界自检：上面那条**不能**由段数上限、空正文或"占位里有数"达成，只能是密度。

    少了这条，把 `PLACEHOLDER_MIN_CHARS` 改成 0 之后骨架稿可能因为别的条件照样报红，
    变异就跑了一趟空（判据空转）。这里让两份稿子只差在正文长短：占位数都等于段数
    （上限放行）、都念得出口（空正文那条放行）、占位里都不含数值（P1 那条放行）、
    正文都零具体（正文那条放行）—— 够撑的豁免、不够撑的报。
    """
    from app.ai_tells import PLACEHOLDER_MIN_CHARS, _count
    assert PLACEHOLDER_MIN_CHARS == 100, \
        "阈值的来处写在 ai_tells 的常量注释里（102 篇产物实测），改它要重跑分布"
    t = AITells({"strong": ["no_specific"]})
    # 刻意不含「一/两/三…」与量词相邻的组合：正文一旦自带"具体事实"就走另一条豁免
    unit = ("电梯停了很多时候不是坏了而是它在保护你，门没关严平层有偏差就会自动停住，"
            "这是设计好的动作，最危险的事情是扒门，外面不是楼道而是空的井道，"
            "被困的人最容易在这里受伤，先按报警按钮等着救援，别自己撬门，")
    hole = "。{{待补：到场用时}}"

    def secs(n):
        return [{"type": "point", "text": (unit * 2)[:n] + hole} for _ in range(2)]

    enough, thin = secs(196), secs(60)
    spoken = [sum(_count(s["text"]) for s in x) for x in (enough, thin)]
    assert min(spoken) > 0 and spoken[0] >= 2 * PLACEHOLDER_MIN_CHARS > spoken[1], \
        f"fixture 本身不成立（念出字数 {spoken}），这条什么都没量到"
    assert "no_specific" not in _ids(t.scan(enough)), t.scan(enough)
    assert "no_specific" in _ids(t.scan(thin)), \
        "把每个空的正文砍到不足 100 字，密度那条下限没拦"


def test_digit_inside_placeholder_does_not_exempt():
    """★ 占位符里带数字 / 量词**不构成**豁免（P1）。

    原来「有没有具体事实」直接扫**含占位符的原文**，于是 `{{待补：3天}}` 里的
    那个 `3` 让整篇豁免 —— 一篇正文一个真数字都没有、只在占位里写了几个
    待确认数值的稿子拿到人味分满分、strong 零命中，而这正是 voice.md 标为
    「最致命」的那一类（占位不是事实）。
    上面的 `test_honest_gaps_are_still_exempted` 用的占位**不含**数字与量词，
    所以它测不到这条路径。

    ⚠ 正文刻意避开「一点」「一下」这类词：`_MEASURE_RE` 把「一」+「点」当量词，
      那是**既有**的宽松匹配（正常中文里确实这么用），与本条无关。正文里
      混进这种词会走「正文有具体事实」那条豁免，量到的就不是本条要量的东西。
    """
    t = AITells({"strong": ["no_specific"]})
    for placeholder in ("{{待补：3天}}", "{{待补：三天}}", "{{待补：5项}}",
                        "{{待补：2026-09-01}}", "{{待补：50%}}"):
        secs = [{"type": "point", "text": f"这个情况要讲明白，{placeholder}。"}
                for _ in range(4)]
        r = t.report(secs)
        assert "no_specific" in _ids(r["hits"]), \
            f"占位 {placeholder} 里的数字/量词让它豁免了（正文其实零具体）：{r['hits']}"


def test_digit_in_real_text_still_exempts():
    """反向对照：数字/量词写在**正文**里仍然豁免（否则这条 tell 会变成噪声源）。"""
    t = AITells({"strong": ["no_specific"]})
    for body in ("维保周期是 15 天。", "每台每年保养三次。", "工期约 30 天。",
                 "这套方案服务 1200 户。", "误差控制在 3% 以内。"):
        secs = [{"type": "point", "text": f"{body}具体批次{{待补：批次号}}。"}
                for _ in range(4)]
        r = t.report(secs)
        assert "no_specific" not in _ids(r["hits"]), \
            f"正文里的真数字没被认出来（豁免判据被改坏了）：{body} → {r['hits']}"


def test_placeholder_count_cap_still_applies():
    """反向对照 2：占位**不含**具体值、只是数量超上限时，仍按 A-1 那条报。

    3 段里每段 2 个占位 = 6 个 > 3 段上限 → 整篇没写，必须报。
    （2 段各 1 个占位正好等于段数，仍在豁免区间内 —— 那正是
    `test_honest_gaps_are_still_exempted` 量到的情况，两者边界在这。）
    """
    t = AITells({"strong": ["no_specific"]})
    secs = [{"type": "point", "text": "这里要展开讲，{{待补：案例}}与{{待补：数据}}。"}
            for _ in range(3)]
    r = t.report(secs)
    assert "no_specific" in _ids(r["hits"]), r["hits"]


# ── A-4：list_enumeration 阈值 ≥4（2026-09-23 A-4 决策预实验）─────
def test_list_enumeration_requires_four_increasing():
    """阈值 3→4：恰好 3 句递增（正当分步讲解）不报；真·4 句连排才报。

    ⚠ 阈值 3→4 由 A-4 决策预实验定（`0864e73`）：电梯包 101 篇产物里
    「第一…第二…第三」是标准答法模板（97% 命中、无区分度），≥3 把这类
    正当内容结构误判成清单体；≥4 后误伤清零、真 4 连排照抓。
    ⚠ 变异检验要求：**恰好 3 句递增**是唯一能把 ≥3 与 ≥4 分开的样本 ——
    若实现改回 ≥3，本用例必须报红（three_steps 会被误判成命中）。
    """
    t = AITells({"weak": ["list_enumeration"]})
    # 恰好 3 句递增（句号句）——阈值 ≥4 时不报、若实现退回 ≥3 必须报红
    three_steps = [{"type": "point",
                    "text": "第一，先别扒门。第二，按警铃。第三，等救援。"}]
    assert "list_enumeration" not in _ids(t.scan(three_steps)), t.scan(three_steps)
    four_steps = [{"type": "point",
                   "text": "第一要定期维保。第二要注意卫生。第三要查紧急装置。第四要做记录。"}]
    assert "list_enumeration" in _ids(t.scan(four_steps)), t.scan(four_steps)


def test_list_enumeration_needs_consecutive_numbers_not_just_four():
    """★ 「连排」必须是**递增连续**：这条支在 975 条测试下从没被走到（覆盖率实测）。

    `if ch and (not seq or num[ch] == seq[-1] + 1)` 的 False 方向从没执行过 ——
    也就是说"跳号不算连排"一直只有注释、没有证据。把那个条件删成 `if ch:`
    （= 凑够四个序号就报），现有全部用例照样绿。
    这条与 A-4 的 ≥4 阈值是两件事：≥4 管"要凑几句"，连续性管"这几句得是一套"。
    口播里「第一…第三…第五…」是**分别举例**，不是清单体连排。
    """
    t = AITells({"weak": ["list_enumeration"]})
    gaps = [{"type": "point",
             "text": "第一，先别扒门。第三，按警铃。第五，看平层。第七，等救援。"}]
    assert t.scan(gaps) == [], \
        f"跳号的四个序号被当成清单体连排：{t.scan(gaps)}"
    # 正对照：同样四句、改成连续 → 必须报（证明上面那条不是把 tell 关掉了）
    run = [{"type": "point",
            "text": "第一，先别扒门。第二，按警铃。第三，看平层。第四，等救援。"}]
    assert _ids(t.scan(run)) == ["list_enumeration"], t.scan(run)
    # 连续段被打断就不再累计：断在第三句，seq 停在 2，够不到 4
    broken = [{"type": "point", "text": "第一，别扒门。第二，按警铃。第八，看平层。第九，等。"}]
    assert t.scan(broken) == [], t.scan(broken)


# ── A-6 #7：汉字数词+量词也算具体 ──────────────────────────
def test_hanzi_measure_words_count_as_specific():
    """「两家公司」与「2 家公司」必须给同一个结论 —— 口播里汉字写法更自然。"""
    t = AITells({"strong": ["no_specific"]})
    hanzi = [{"type": "point", "text": "全城只有两家公司肯接这种单，三家业委会都问过。"}]
    assert "no_specific" not in _ids(t.scan(hanzi)), t.scan(hanzi)


def test_genuinely_vague_script_still_fires():
    """正向对照：量词扩了之后，真·空话稿仍要报，否则这条 tell 等于被删。"""
    t = AITells({"strong": ["no_specific"]})
    vague = [{"type": "point", "text": "这种单没人肯接，业委会也没问过，大家都觉得麻烦。"}]
    assert "no_specific" in _ids(t.scan(vague))


# ── P0-3：副词尾字不许当量词（五路审查 2026-09-23）────────────
# 病灶：_MEASURE_CHARS 里混着 分/成/人/起，于是「十分」「八成」「个人」
# 「一起」全被当成"数词+量词"→ no_specific 被错误豁免。这条 tell 是
# strong（"通篇无一个具体数字/时间/数量"），被打穿的后果是**满篇空话
# 照样满分**。数字形式（3 分 / 8 成）走 \d 分支，不受删字影响。
def test_adverb_tails_are_not_measure_words():
    t = AITells({"strong": ["no_specific"]})
    # ⚠ 样本句里**不能**再出现真量词（「一块」「一下」的 块/下 都在字符类里），
    #   否则量词匹配会豁免掉这条 tell，测出来的就不是想测的那件事。
    for phrase, why in [
        ("十分重要的细节，大家都得重视，赶紧落实到位。", "十分"),
        ("我个人认为，这件事情需要仔细琢磨，不能想当然。", "我个人"),
        ("八成是因为这样，所以才会变成这样的结果。", "八成"),
    ]:
        hits = _ids(t.scan([{"type": "point", "text": phrase}]))
        assert "no_specific" in hits, f"{why} 被误当成具体数字，空话稿漏报：{phrase}"


def test_real_measure_words_still_exempt():
    """反向对照：删了四个字，真·汉字量词与数字形式仍要豁免（守卫别把自己删没了）。"""
    t = AITells({"strong": ["no_specific"]})
    ok = [{"type": "point", "text": "全城只有两家公司肯接，三家业委会问过，一台梯子跑了五天。"}]
    assert "no_specific" not in _ids(t.scan(ok)), t.scan(ok)
    digits = [{"type": "point", "text": "三个人一起，三成分红，跑了 3 分 30 秒。"}]
    assert "no_specific" not in _ids(t.scan(digits)), t.scan(digits)


# ── A-6 #1：口号收尾只看结尾引导段 ─────────────────────────
def test_slogan_closing_scoped_to_cta_with_exemption():
    lex = {"strong": ["slogan_closing"], "lexicon": {"slogan_closing": ["品质保证"]}}
    t = AITells(lex)
    assert _ids(t.scan([{"type": "cta", "text": "选我们，品质保证。"}])) == ["slogan_closing"]
    # 同样的词出现在要点段 —— 不是"口号式收尾"
    assert t.scan([{"type": "point", "text": "厂家会给你品质保证的。"}]) == []
    # cta 段里对着"你"说话或在提问 —— spec 的豁免
    assert t.scan([{"type": "cta", "text": "品质保证，你放心。"}]) == []
    assert t.scan([{"type": "cta", "text": "品质保证，你说是不是？"}]) == []


# ── A-6 #2：开场禁区只看全篇第一句 ─────────────────────────
def test_opening_ban_only_first_sentence():
    lex = {"strong": ["opening_ban"], "lexicon": {"opening_ban": ["大家好"]}}
    t = AITells(lex)
    hit = t.scan([{"type": "hook", "text": "大家好，今天说说电梯。"}])
    assert _ids(hit) == ["opening_ban"] and _where(hit[0]) == "第1段", hit
    # 正文里引用一句"大家好"不是模板开场（此前全文扫描，误伤代价最大：strong 一扣 12）
    assert t.scan([{"type": "hook", "text": "我们聊过很多次。大家好不容易聚齐，就别绕弯子。"}]) == []


# ── A-9（调研报告-20260928）：三条新词表 tell + `cta_strict` + 出处豁免 ──────
def test_helper_closing_cta_strict_has_no_second_person_exemption():
    """助手腔收尾走 `cta_strict`：**不做** `cta_only` 的第二人称/疑问豁免。

    为什么必须有这条：那些短语本身个个含"你"（「希望对你有帮助」），沿用旧豁免
    会让这条 tell 恒被豁免、**永不响** —— 而"对着屏幕外的读者自说自话"就是它的判据。
    正对照是同一段里 `slogan_closing` 因为含"你"被豁免：两档的差别必须看得见。
    """
    t = AITells({"strong": ["helper_closing", "slogan_closing"],
                 "lexicon": {"helper_closing": ["希望对你有帮助"],
                             "slogan_closing": ["品质保证"]}})
    hit = t.scan([{"type": "cta", "text": "品质保证，希望对你有帮助。"}])
    assert _ids(hit) == ["helper_closing"], hit          # 含"你"：口号那条豁免、助手腔照报
    # 只在结尾引导段：要点段里说一句"希望对你有帮助"不是收尾
    assert t.scan([{"type": "point", "text": "这条说明希望对你有帮助。"}]) == []


def test_unattributed_fires_only_without_a_source_in_the_tail():
    """无来源归因：段内 ≥2 处才报（weak），且命中后 20 字内出现数字/文号就放过。

    两个方向都要钉住 —— 只测"该报的报了"，会把豁免写成永远不触发也照样绿；
    反过来只测豁免又会把窗口写成"整段有数字就全放过"。窗口那条边界用 strong
    单独立一对，免得被 ≥2 的门槛盖住。
    """
    t = AITells({"weak": ["unattributed"],
                 "lexicon": {"unattributed": ["数据显示", "研究表明"]}})
    both = [{"type": "point", "text": "数据显示这事很常见。研究表明一半以上都是误报。"}]
    assert _ids(t.scan(both)) == ["unattributed"], t.scan(both)
    # 出处豁免：「市场监管总局数据显示，2025 年…」是有出处的真归因，不能报 ——
    # 豁免掉一处之后只剩一处，未达 weak 门槛，整段不报。
    cited = [{"type": "point", "text": "市场监管总局数据显示，2025 年全省电梯困人 1200 起。"
                                      "研究表明一半是人为。"}]
    assert t.scan(cited) == [], t.scan(cited)
    # 窗口边界（strong 让单处就能报）：数字在 20 字**之外** → 不豁免；在窗口内 → 豁免
    win = AITells({"strong": ["unattributed"],
                   "lexicon": {"unattributed": ["数据显示"]}})
    far = [{"type": "point", "text": "数据显示，这类问题在过去几年里逐步积累而成，"
                                    "涉及多个环节与多家单位。2025 年才集中整治。"}]
    assert _ids(win.scan(far)) == ["unattributed"], win.scan(far)
    near = [{"type": "point", "text": "数据显示，2025 年这类问题集中爆发。"}]
    assert win.scan(near) == [], win.scan(near)
    # 量词支（审查报告-20260930 E9）：`_MEASURE_RE` 的「汉字数词+量词」同样买通豁免 ——
    # 修复前后行为一致，但这条支路原来没有用例，变异 s37-1 只会走到数字支
    hanzi = AITells({"strong": ["unattributed"],
                     "lexicon": {"unattributed": ["业内一致认可"]}})
    assert hanzi.scan([{"type": "point",
                        "text": "业内一致认可，全城只有两家公司接这种单。"}]) == [], \
        hanzi.scan([{"type": "point", "text": "业内一致认可，全城只有两家公司接这种单。"}])


def test_cliche_hook_needs_two_in_one_section():
    """「口号排队」本义就是多个连用：单次「建议收藏」是正常口播用语，不报。"""
    t = AITells({"weak": ["cliche_hook"],
                 "lexicon": {"cliche_hook": ["建议收藏", "看到最后", "别划走"]}})
    assert t.scan([{"type": "hook", "text": "这条建议收藏。"}]) == []
    hit = t.scan([{"type": "hook", "text": "看到最后你会明白。建议收藏，转发给家里人。"}])
    assert _ids(hit) == ["cliche_hook"] and hit[0].count == 2, hit


def test_contrast_frame_is_same_sentence_and_per_section():
    """对照反转：句式在代码里（词表跑不了开放空档），**同句**才算、按段计 ≥2 才报。

    跨句那一条是 A-8 句式类的教训：把「不是」与「而是」分到两段话里就成了误伤。
    """
    t = AITells({"weak": ["contrast_frame"]})
    two = [{"type": "point", "text": "这不是运气，而是保养的结果。"
                                     "与其说贵，不如说值。"}]
    hit = t.scan(two)
    assert _ids(hit) == ["contrast_frame"] and hit[0].count == 2, hit
    assert "不是…而是" in hit[0].detail, hit
    # 只有一处 —— 段里出现一个修辞框架是写作，不是模板
    assert t.scan([{"type": "point", "text": "这不是运气，而是保养的结果。"}]) == []
    # 跨句（前半句"不是"、后半句"而是"）—— 不算对照反转
    assert t.scan([{"type": "point", "text": "这根本不是运气。而是保养到位了才这样。"}]) == []
    # 「不是…是」没有"而是"这一半 —— 是正常口语，不报
    assert t.scan([{"type": "point", "text": "问题不是电梯，是使用习惯。"}]) == []
    # ⚠ 同句之内还要**贴近**（24 字）：一句话里"看似…"之后先岔出去讲了一大段，
    #   句尾才出现一个"实则"，那不是同一个修辞框架的两半。空档不设限就会把它误判成
    #   AI 味 —— 变异 s37-6 就是拿这一段证伪的：这里一句是"远"的（不该算）、
    #   一句是"近"的真对照（该算），合起来 1 处 → 未达 weak 的 ≥2 → 不报；
    #   空档一旦放宽成无限，两句都算 → 2 处 → 报，用例当场红。
    far_apart = [{"type": "point", "text":
                  "看似只是擦一下导轨的小事，实际上关系到整扇门的运行，很多人用了好几年都不"
                  "知道选错清洗剂反而伤导轨，实则保养手册那一页写得清清楚楚。"
                  "与其说选电梯是选品牌，不如说是在选安装队，这句话第一次听都觉得夸张。"}]
    assert t.scan(far_apart) == [], t.scan(far_apart)
    # 正向对照：两处都是"近"的真对照 —— 该报（不然上面那条断言只是判据空转）
    close = [{"type": "point", "text": "这不是危险，而是提醒。这不是成本，而是必要的开销。"}]
    assert _ids(t.scan(close)) == ["contrast_frame"], t.scan(close)


# ── A-9 schema：`re:` 正则条目 / 词义限定 / 门槛覆盖，三样都不许静默失效 ──
def test_opening_ban_accepts_regex_frame_entries():
    t = AITells({"strong": ["opening_ban"],
                 "lexicon": {"opening_ban": ["re:在当今[\\s\\S]{0,12}(?:时代|社会|背景下)",
                                             "re:作为一[个名位]"]}})
    assert t.warnings() == [], t.warnings()
    hit = t.scan([{"type": "hook", "text": "在当今社会，电梯已经是刚需。"}])
    assert _ids(hit) == ["opening_ban"], hit
    # 框架中间的枚举不完的字，正则才堵得住
    assert _ids(t.scan([{"type": "hook", "text": "作为一个用了十五年电梯的人。"}])) == ["opening_ban"]
    # 不在首句 —— `first_sent` 口径：正文里引用一句不算模板开场
    assert t.scan([{"type": "hook", "text": "先说结论。作为一个业主，我踩过坑。"}]) == []
    # 只认句首起匹配：句中才有的"…在当今社会"不算开场
    assert t.scan([{"type": "hook", "text": "很多人说，在当今社会电梯最省心。"}]) == []


def test_regex_entries_outside_first_sent_are_announced_and_skipped():
    """`re:` 用在字面量扫描的类里 = 一条"看着配了、永不命中"的死词 —— 必须说出口。

    正反两向都要有：只断言"有警告"的话，把整条 tell 关掉也能骗过它；
    所以同时断言**同一段字面量词照常命中**（扫描没被正则条目搞坏）。
    """
    t = AITells({"weak": ["filler_stack"],
                 "lexicon": {"filler_stack": ["re:此", "此外"]}})
    assert any("re:" in w for w in t.warnings()), t.warnings()
    hit = t.scan([{"type": "point", "text": "此外还有一点。此外再说一句。"}])
    assert _ids(hit) == ["filler_stack"] and hit[0].count == 2, hit


def test_regex_entry_never_joins_the_literal_scan():
    """★ 第二道防线（`_scan_words` 里的 `RE_PREFIX` skip）此前没有用例。

    `__init__` 已经把 `re:` 条目剔出 `self.lexicon`，`_scan_words` 再挡一道
    （直接调它也会跳过）。**为什么上面那条正反对照抓不到它**：把 skip 摘掉之后，
    「re:此」被当成普通词去扫，而测试文本里没有"re:此"这三个字 → 命中数、警告
    一字不变，变异全绿 —— 判据空转。所以这里用 strong 档位（绕开 weak 的 ≥2 门槛，
    否则一次命中根本走不到报告）并拿一段**真含 "re:此" 字样**的文本去量。
    """
    t = AITells({"strong": ["filler_stack"],
                 "lexicon": {"filler_stack": ["re:此", "此外"]}})
    assert "re:此" not in (t.lexicon.get("filler_stack") or []), t.lexicon
    hit = t.scan([{"type": "point", "text": "此外还有一点。"}])
    assert sorted(hit[0].words) == ["此外"], hit[0].words
    dirty = t.scan([{"type": "point", "text": "包里写着 re:此 的条目不算词。此外还要说一句。"}])
    assert dirty and dirty[0].count == 1 and sorted(dirty[0].words) == ["此外"], \
        f"「re:此」被当字面量扫进正文了（这条 tell 从此在罚包作者的写法）：{dirty}"
    # ⚠ 上面三条**量不到** `_scan_words` 里那道 skip：全仓只有 `_lexical` 一个调用方，
    #   而它传的是 `self.lexicon[tid]` —— `re:` 早在 `__init__` 就被剔出去了，
    #   走公开接口永远到不了那行代码（实测把 skip 摘掉，三条断言照样全绿）。
    #   所以这条纵深防线只能在单元层面钉住：直接把手上有 `re:` 条目的词表喂给它。
    from app.ai_tells import _scan_words
    assert _scan_words("包里写着 re:此 的条目不算词。此外还要说一句。",
                       ["re:此", "此外"]) == {"此外": 1}, \
        "正则条目被当字面量扫了：这条 tell 会去罚包作者 yaml 里的写法，而不是正文"


def test_zero_width_regex_entry_does_not_flag_every_opening():
    """写宽了的 `re:` 条目（能零宽匹配）**不许**等于"每篇都报开场违规"。

    `rx.match()` 对 `re:[0-9]*` 这类模式在任意句首都返回一个**空**匹配对象，
    而空匹配是 truthy —— 只判 `if m` 就等于把「这个人味 tell」变成恒真命中，
    strong 单次即扣 12 分、每篇都红，且报出来的"命中词"是个空字符串。
    防线是 `if m and m.group(0)`；这条用例是它唯一的证据。
    """
    t = AITells({"strong": ["opening_ban"], "lexicon": {"opening_ban": ["re:[0-9]*"]}})
    assert t.warnings() == [], t.warnings()          # 能编译，所以不该有警告
    for sent in ("今天说说电梯的事。", "被困了先别慌，按报警按钮。"):
        assert t.scan([{"type": "hook", "text": sent}]) == [], \
            f"零宽匹配把正常开场报成模板开场：{sent}"
    # 正向对照：同一个模式在**真有匹配**时照常报（防线不是把整条 tell 关掉）
    got = t.scan([{"type": "hook", "text": "2026 年的电梯新规来了。"}])
    assert _ids(got) == ["opening_ban"], got
    assert got[0].words == ("2026",), got[0].words      # 命中词是**匹配到的那几个字**


def test_word_sense_qualifier_excludes_the_industry_term():
    """词义限定：`unless` 是机器可判的那一半，`note` 跟着命中进反馈给人读。"""
    data = {"weak": ["abstract_noun_ending"],
            "lexicon": {"abstract_noun_ending": [
                {"闭环": {"note": "仅黑话义", "unless": ["工单闭环", "维保闭环"]}}]}}
    t = AITells(data)
    assert t.lexicon["abstract_noun_ending"] == ["闭环"], t.lexicon
    # 行业真术语：两处都是「维保工单闭环」→ 全豁免
    real = [{"type": "point", "text": "维保工单闭环要留痕。工单闭环靠签字确认。"}]
    assert t.scan(real) == [], t.scan(real)
    # 黑话义：豁免短语没撞上 → 照报，且 `note` 出现在 detail 里（告诉改的人为什么报）
    slog = [{"type": "point", "text": "服务要形成闭环，管理要闭环。"}]
    hit = t.scan(slog)
    assert _ids(hit) == ["abstract_noun_ending"], hit
    assert "仅黑话义" in hit[0].detail, hit
    # 同段混着写：只有真术语那处被豁免，剩一处不到门槛 —— 豁免是**按命中位置**算的，
    # 不是"这段里出现过工单闭环就整段赦免"。
    mixed = [{"type": "point", "text": "维保工单闭环要留痕。服务也要形成闭环。"}]
    assert t.scan(mixed) == [], t.scan(mixed)


def test_per_tell_threshold_override_is_announced():
    """门槛可被 yaml 按 tell 覆盖，但**非默认口径必须进 config_warnings**：
    这个数直接决定扣分面，与 `score_of()` 同源，不能让某个包悄悄换尺子。"""
    data = {"weak": ["cliche_hook"],
            "lexicon": {"cliche_hook": ["看到最后", "建议收藏", "别划走"]}}
    base = AITells(data)
    secs = [{"type": "hook", "text": "看到最后。建议收藏。别划走。"}]
    assert base.scan(secs), base.scan(secs)               # 默认门槛 2：三处照报
    assert base.warnings() == [], base.warnings()         # 没覆盖 = 不报"非默认口径"
    raised = AITells({**data, "thresholds": {"cliche_hook": 3}})
    assert any("cliche_hook" in w for w in raised.warnings()), raised.warnings()
    assert raised.scan(secs), raised.scan(secs)            # 三处：抬到 3 也照报
    four = AITells({**data, "thresholds": {"cliche_hook": 4}})
    assert not four.scan(secs), four.scan(secs)            # 抬到 4：这一句不再报
    # 覆盖率实测补的一支：**显式写成默认值**不等于"改了口径"。
    # 报了就是谎称"这个包的分数与别的包不可比"（`warnings()` 那句话是给包作者看的账），
    # 而这个键本身又确实被吃进了判定 —— 两支都要钉住。
    same_as_default = AITells({**data, "thresholds": {"cliche_hook": 2}})
    assert same_as_default.thresholds == {"cliche_hook": 2}, same_as_default.thresholds
    assert same_as_default.warnings() == [], same_as_default.warnings()
    assert same_as_default.scan(secs), same_as_default.scan(secs)
    # 写坏了不静默：非数字 / 0 / 负数都报出来并按默认口径跑
    broken = AITells({**data, "thresholds": {"cliche_hook": "很多"}})
    assert any("thresholds" in w for w in broken.warnings()), broken.warnings()
    assert broken.scan(secs)


def test_structural_tell_without_an_implementation_is_announced(monkeypatch):
    """结构类按命名约定接算法（`_<tell 名>`）：漏实现 = 永不跑，必须说出口。

    变异方向：以前 `scan()` 里是一份手写清单，`STRUCTURAL` 加了名字而清单没加，
    对账测试（比的是名字）照样绿，而这条 tell 一条都不跑。
    """
    monkeypatch.delattr(AITells, "_contrast_frame")
    t = AITells({"weak": ["contrast_frame"]})
    assert any("没有算法" in w for w in t.warnings()), t.warnings()
    assert t.scan([{"type": "point", "text": "不是运气，而是保养。与其说贵，不如说值。"}]) == []


def test_shipped_packs_load_without_any_warning():
    """两个包的词表在引擎里必须**一条警告都没有**（含新加的 schema 字段）。

    这是对 `config_warnings` 的正向对照：上面那些"要报出来"的断言若把判据写反了，
    正常配置也会一片警告，界面上就没人看得出问题。
    """
    for pack_dir in ("elevator", "_template"):
        t = _load_tells(Pack(ROOT, pack_dir))
        assert t is not None, pack_dir
        assert t.warnings() == [], (pack_dir, t.warnings())
        assert t.lex_regex.get("opening_ban"), f"{pack_dir}：开场禁区的 re: 条目没被收下"
        # 词表类每一条都得有词（死 tell 由对齐测试管，这里管"引擎读完后还剩什么"）
        assert set(t.lexicon) >= set(AITells.LEXICAL), (pack_dir, sorted(t.lexicon))


def test_qualifier_entries_survive_the_loading_layer(tmp_path):
    """限定说明条目必须**活着穿过 `resolve_ai_tells`**（加载层只认字符串的话，
    引擎那边写得再对也读不到 —— 这是"解析层兼容旧形态"的后半件事）。"""
    from app.knowledge import resolve_ai_tells
    (tmp_path / "ai_tells.yaml").write_text(
        "strong:\n  - abstract_noun_ending\n"
        "lexicon:\n  abstract_noun_ending:\n"
        "    - 闭环:\n        note: 仅黑话义\n        unless: [工单闭环]\n"
        "    - re:赋能\n", encoding="utf-8")
    data, notes = resolve_ai_tells(tmp_path, {"ai_tells": "ai_tells.yaml"})
    assert data is not None, notes
    assert not [n for n in notes if "丢掉" in n], notes
    lex = data["lexicon"]["abstract_noun_ending"]
    assert any(isinstance(x, dict) for x in lex), lex          # 带限定的条目没被当成"非字符串"丢掉
    t = AITells(data)
    assert t.word_rules["abstract_noun_ending"]["闭环"]["unless"] == ["工单闭环"], t.word_rules
    assert any("re:" in w for w in t.warnings()), t.warnings()  # 而 re: 用在词表类要报出来


def test_shape_reports_six_signals_and_never_touches_the_score():
    """L2「形态指标」六个信号都算得出，但**分数一条都不看它**（只展示不设门）。

    判据不是"shape 存在"，而是**改 shape 的输入不动 score**：
    给同一个稿子配一份成语表 → `idioms_per_1000` 从 null 变成数，
    而 `score` / `hits` / `strong` / `weak` 必须逐字不变 ——
    哪天有人拿这组统计量去当门，这条会立刻红。
    """
    from app.ai_tells import shape_metrics
    secs = [{"type": "hook", "text": "困在电梯里，第一件事不是打电话。／真的，别急着掏手机。"},
            {"type": "point", "text": "我修了 15 年电梯，见过太多人一慌就去扒门。／"
                                     "轿厢停在两层之间，门一开，外面是井道，不是楼道。／"
                                     "这事儿至关重要，却也最容易被忽略，你看是不是。"},
            {"type": "cta", "text": "把这条转给你家里那个爱逞强的。／评论区说说你遇过什么。"}]
    sh = shape_metrics(secs, ["至关重要", "一目了然"])
    assert set(sh) >= {"mattr", "trigram_repeat", "clause_len", "particles_per_100",
                       "funcword_entropy", "idioms_per_1000", "chars"}, sorted(sh)
    assert 0 < sh["mattr"] <= 1 and 0 <= sh["trigram_repeat"] <= 1, sh
    assert sh["particles_per_100"] > 0, sh          # 「你看」「是不是」就是正向口语信号
    assert sh["idioms_per_1000"] > 0, sh            # 「至关重要」被数进来了
    assert sh["clause_len"]["n"] >= 4 and sh["clause_len"]["p50"], sh["clause_len"]
    assert sh["funcword_entropy"] > 0, sh

    data = yaml.safe_load(AI_YAML.read_text(encoding="utf-8")) or {}
    no_idiom_data = {k: v for k, v in data.items() if k != "idioms"}
    base = AITells(no_idiom_data).report(secs)
    with_idioms = AITells({**no_idiom_data, "idioms": ["至关重要", "循序渐进"]}).report(secs)
    without_idioms = AITells(no_idiom_data).report(secs)
    assert with_idioms["shape"]["idioms_per_1000"] is not None
    assert without_idioms["shape"]["idioms_per_1000"] is None, "没配表要落 null，不是 0"
    for key in ("score", "hits", "strong", "weak"):
        assert base[key] == with_idioms[key], f"shape 的输入改动了 L1 判定：{key}"
    # 表写坏成字符串不会被逐字切成单字去数密度（与 lexicon 同一条静默失效）
    broken = AITells({**no_idiom_data, "idioms": "至关重要"})
    assert broken.idioms == [] and any("idioms" in w for w in broken.warnings()), broken.warnings()


def test_shipped_packs_carry_an_idiom_table_for_l2():
    """两个包都配了 `idioms:` —— 否则 L2 那一格永远是"—（本包没配成语表）"，
    六个信号里就有一个是死的（写了但没人喂数据，与"没接上"同一种缺陷）。"""
    for pack_dir in ("elevator", "_template"):
        t = _load_tells(Pack(ROOT, pack_dir))
        assert t.idioms and all(len(i) >= 4 for i in t.idioms), (pack_dir, t.idioms)


def test_shape_on_an_empty_draft_is_all_none_not_all_zero():
    """空稿：所有指标落 null（"算不出"），字数落 0 —— 界面据此画「—」而不是 0 分。"""
    from app.ai_tells import shape_metrics
    sh = shape_metrics([], ["至关重要"])
    assert sh["chars"] == 0
    assert sh["mattr"] is None and sh["trigram_repeat"] is None
    assert sh["particles_per_100"] is None and sh["idioms_per_1000"] is None
    assert sh["clause_len"]["n"] == 0 and sh["clause_len"]["p50"] is None


def test_short_unless_phrase_is_announced_instead_of_killing_the_tell():
    """单字 `unless`（如 `环`）会和任何命中重叠 —— 那是把整条 tell 静默打死。

    与词表本身同一条立场：宁可报出来，也不留一条"配了却永不命中"的死判据。
    """
    data = {"weak": ["abstract_noun_ending"],
            "lexicon": {"abstract_noun_ending": [{"闭环": {"unless": ["环", "工单闭环"]}}]}}
    t = AITells(data)
    assert any("unless" in w and "2 字" in w for w in t.warnings()), t.warnings()
    # 坏的那条被丢掉、好的那条仍然有效：黑话照报（单字豁免不再吞掉一切）
    hit = t.scan([{"type": "point", "text": "服务要形成闭环，管理要闭环。"}])
    assert _ids(hit) == ["abstract_noun_ending"], hit
    assert t.scan([{"type": "point", "text": "维保工单闭环要留痕。工单闭环靠签字。"}]) == []

    # 覆盖率实测补的一支：`unless` **整条都是单字**时（上面混了好条目，走的是"留下好的"那支），
    # 丢弃之后 tell 必须照跑 —— 否则"报出来"变成"顺手把 tell 关了"，那正是本条要防的事。
    only_bad = AITells({"strong": ["abstract_noun_ending"],
                        "lexicon": {"abstract_noun_ending": [{"闭环": {"unless": ["环", "点"]}}]}})
    assert any("unless" in w and "2 字" in w for w in only_bad.warnings()), only_bad.warnings()
    assert only_bad.word_rules.get("abstract_noun_ending", {}).get("闭环", {}) == {}, \
        only_bad.word_rules
    assert _ids(only_bad.scan([{"type": "point", "text": "服务要形成闭环。"}])) == \
        ["abstract_noun_ending"], only_bad.scan([{"type": "point", "text": "服务要形成闭环。"}])


def test_thresholds_on_classes_that_do_not_read_it_are_announced():
    """`thresholds` 只有 weak 的词表类与 `contrast_frame` 吃。

    写给 strong 或别的结构类是 **no-op**，而原先 `warnings()` 会照样印一句
    "非默认口径、分数不可比" —— 那是假话：包作者以为换了尺子，实际什么都没变（A-9 审查 P2）。
    """
    base = {"weak": ["cliche_hook", "parallel_triple"], "strong": ["no_specific", "helper_closing"],
            "lexicon": {"cliche_hook": ["看到最后", "建议收藏"], "helper_closing": ["希望本文"]}}
    t = AITells({**base, "thresholds": {"no_specific": 3, "helper_closing": 5,
                                        "parallel_triple": 1, "uniform_para_len": 9}})
    # 2026-10-05 P3：未声明判据从 contrast_frame 泛化到所有 tell ——
    # uniform_para_len（未声明）现在得到"配给了未声明的 tell"的专属文案，
    # 不再混在"对这一类不生效"的泛化警告里。
    bad = [w for w in t.warnings() if "不生效" in w]
    assert len(bad) == 3, t.warnings()
    assert any("uniform_para_len" in w and "未声明" in w for w in t.warnings()), t.warnings()
    assert not any("非默认口径" in w for w in t.warnings()), \
        "no-op 的键不该被说成「换了口径」（那是假话：实际什么都没变）"
    # ⚠ 这份稿子刻意不写"一次/两家"这类**数词+量词**：那会让 `no_specific` 走豁免
    #   （它只认"有没有可数的事实"），这条用例要断言的是"strong 单次即报"，
    #   不是豁免逻辑。
    secs = [{"type": "point", "text": "全城没有维保记录，也没人统计过这回事。"},
            {"type": "cta", "text": "希望本文对你有用。"}]   # helper_closing 只认 cta 段
    ids = _ids(t.scan(secs))
    assert "no_specific" in ids and "helper_closing" in ids, ids   # strong 依旧单次即报


def test_threshold_must_be_an_integer_number_of_hits():
    t = AITells({"weak": ["cliche_hook"], "lexicon": {"cliche_hook": ["看到最后", "建议收藏"]}})
    for bad in ("2.9", True, "三"):
        w = AITells({"weak": ["cliche_hook"], "lexicon": t.lexicon,
                     "thresholds": {"cliche_hook": bad}})
        assert any("cliche_hook" in x for x in w.warnings()), (bad, w.warnings())
        assert "cliche_hook" not in w.thresholds, bad     # 按默认口径跑，不静默截断


# ── 审查报告-20260930 的修复（M2/M3/M5/E6/E7/E8）──────────────────

def test_no_specific_ignores_picture_annotations():
    """M3：`[画面：…]` 不进嘴，画面里的数字不许买通 strong 的豁免。

    修复前豁免判据只剥占位符、不剥画面标注（`_count`/`_char_stream` 都剥画面），
    「正文零具体 + [画面：字幕 2025 年新规]」→ scan() 零命中（实测漏报）。
    """
    t = AITells({"strong": ["no_specific"]})
    pics = [{"type": "point",
             "text": "这事儿你得重视，别等出了事才后悔。[画面：字幕 2025 年新规]"}]
    assert "no_specific" in _ids(t.scan(pics)), t.scan(pics)
    # 对照：正文里真有数字 → 豁免照常（剥画面不影响正文那条判）
    ok = [{"type": "point",
           "text": "上个月 3 号楼那台梯停了半天。[画面：字幕 2025 年新规]"}]
    assert t.scan(ok) == [], t.scan(ok)


def test_shape_stream_matches_the_count_caliber():
    """M2：形态流的口径必须与 `_count` 一致 —— `／` 是停顿符，不是字。

    修复前 `_SHAPE_SPLIT` 手写了一份更窄的标点表，`／` 被当成"字"灌进
    mattr/实词 3-gram/虚词熵（12 篇真实产物字数 +4.6%）。现在 `_SHAPE_SPLIT`
    从 `_PUNCT` 派生，两处永不漂 —— 本条断言钉的就是这个不变量。
    """
    from app.ai_tells import _count, shape_metrics
    secs = [{"type": "hook", "text": "哎，我跟你说／真事儿（上礼拜的事）"},
            {"type": "point", "text": "那天／电梯停在七楼／人被困了半小时，你说急不急。"}]
    assert shape_metrics(secs)["chars"] == sum(_count(s["text"]) for s in secs), \
        (shape_metrics(secs)["chars"], sum(_count(s["text"]) for s in secs))


def test_first_sent_qualifiers_are_announced_as_unused():
    """M5：first_sent 路径不消费 note/unless —— 加载时必须说出口。

    `_opening_ban` 只做句首匹配、不读 `word_rules`：note 不进 detail、unless 不豁免
    （实测「大家好不容易聚齐…」作首句照报）。正则条目同理：`continue` 在 spec
    处理之前，带 note/unless 一样是被静默丢掉 —— 两处都要喊。
    """
    t = AITells({"strong": ["opening_ban"],
                 "lexicon": {"opening_ban": [
                     {"大家好": {"note": "问候", "unless": ["大家好不容易"]}},
                     {"re:在当今[\\s\\S]{0,6}时代": {"note": "框架开场"}},
                 ]}})
    warns = t.warnings()
    assert any("大家好" in w and "不生效" in w for w in warns), warns
    assert any("正则条目" in w and "不生效" in w for w in warns), warns
    # 行为不变：句首命中照报（这条修的是"说出口"，不是改判据）
    hit = t.scan([{"type": "hook",
                   "text": "大家好不容易聚齐了业委会，今天把这事说透。"}])
    assert "opening_ban" in _ids(hit), hit


def test_contrast_frame_strong_reports_a_single_hit():
    """E6：strong 的 contrast_frame 单次即报（与其余 strong 同一口径）。

    修复前门槛与 severity 无关：声明成 strong 时单次命中也不报 —— strong 成了
    比 weak 更迟钝的怪档。门槛只在 weak 声明下参与（见下一条）。
    """
    t = AITells({"strong": ["contrast_frame"]})
    one = [{"type": "point", "text": "这不是运气，而是保养的结果。"}]
    hit = t.scan(one)
    assert _ids(hit) == ["contrast_frame"] and hit[0].count == 1, hit


def test_thresholds_for_contrast_frame_follow_the_severity():
    """E6/E8：门槛 × contrast_frame 的三态。

    weak：照常生效（非默认值进"非默认口径"警告）；strong：门槛不生效（说出口，
    不许印"非默认口径"—— strong 是单次即报，门槛压根不参与）；未声明：tell 根本
    不跑，门槛是 no-op —— 更不能印"非默认口径"（那是把没换尺子说成换了）。
    """
    weak_t = AITells({"weak": ["contrast_frame"], "thresholds": {"contrast_frame": 3}})
    assert weak_t.thresholds == {"contrast_frame": 3}
    assert any("非默认口径" in w for w in weak_t.warnings()), weak_t.warnings()

    strong_t = AITells({"strong": ["contrast_frame"],
                        "thresholds": {"contrast_frame": 3}})
    assert "contrast_frame" not in strong_t.thresholds, strong_t.thresholds
    # ⚠ 必须点名 strong 分支的专属理由（"对 strong 不生效"）：泛化的 no-op 兜底
    #   也说"不生效"——不钉措辞的话，把整个 strong 分支拆了照样绿（s42-9 实测漏检）。
    assert any("对 strong 不生效" in w for w in strong_t.warnings()), strong_t.warnings()
    assert not any("非默认口径" in w for w in strong_t.warnings())

    off_t = AITells({"weak": ["cliche_hook"],
                     "lexicon": {"cliche_hook": ["看到最后", "建议收藏"]},
                     "thresholds": {"contrast_frame": 3}})
    assert "contrast_frame" not in off_t.thresholds, off_t.thresholds
    assert any("未声明" in w for w in off_t.warnings()), off_t.warnings()
    assert not any("非默认口径" in w for w in off_t.warnings())


def test_thresholds_string_form_is_announced_but_still_used():
    """E7：`thresholds: {cliche_hook: "2"}` 必须说出口（修复前被 float 静默收下）。

    与「很多」→警告、2.9→警告同一条立场：配置写错形态必须出声。值本身能读、
    没有漂移 —— 所以照用（warn-and-use），不是 warn-and-default。
    """
    t = AITells({"weak": ["cliche_hook"], "lexicon": {"cliche_hook": ["看到最后", "建议收藏"]},
                 "thresholds": {"cliche_hook": "2"}})
    assert t.thresholds == {"cliche_hook": 2}, t.thresholds
    assert any("字符串" in w and "cliche_hook" in w for w in t.warnings()), t.warnings()


def test_shipped_packs_do_not_punish_what_the_prompt_encourages():
    """提示词与判据必须同一本账（A-9 审查 P2 的三处对不齐，都在这里钉住）：

      · voice.md 鼓励「其实呢」，词表里的「其实」必须带 `unless`，否则正向词自己会被扣"堆词"；
      · 「如果你也…」「评论区告诉我」是口播的正常互动收尾，**不是**助手腔 —— 曾收进
        `helper_closing`，一句就判 strong×2（人味 76 分），已删；
      · `idioms`（L2 密度表）与 tell 词表不得重叠：同一次命中不该既报 tell 又算成语密度。
    """
    for pack_dir in ("elevator", "_template"):
        t = _load_tells(Pack(ROOT, pack_dir))
        rules = t.word_rules.get("filler_stack") or {}
        assert "其实呢" in (rules.get("其实") or {}).get("unless", []), (pack_dir, rules)
        closing = t.lexicon.get("helper_closing") or []
        assert "如果你也" not in closing and "评论区告诉我" not in closing, (pack_dir, closing)
        tell_words = {w for ws in t.lexicon.values() for w in ws}
        both = sorted(set(t.idioms) & tell_words)
        assert not both, f"{pack_dir}：这些成语同时在 tell 词表与 idioms 表里：{both}"
    # 两个包的 idioms 必须一致：`_template` 是新包的出生起点，不一样的表=两份口径
    assert (_load_tells(Pack(ROOT, "elevator")).idioms
            == _load_tells(Pack(ROOT, "_template")).idioms)


def test_particles_are_counted_on_the_spoken_text_only():
    """L2 的分子与分母同口径：镜头提示与占位里的字不会被念出来，不该算进语气词密度。"""
    from app.ai_tells import shape_metrics
    one = [{"type": "point", "text": "[画面：师傅指着轿厢顶]大概是这样。{{待补：型号}} 你去看看啊。"}]
    sh = shape_metrics(one, [])
    assert sh["chars"] == len("大概是这样你去看看啊") or sh["chars"] < 20, sh
    assert sh["particles_per_100"] <= 30, sh       # 剥之前会被顶到 7.69/13 字这种虚高形态
    # 「其实呢」只算一次语气：`呢` 已经数过，多字标记若再含它就双计（虚高 2 倍）
    p4 = shape_metrics([{"type": "point", "text": "其实呢，这事不难。" * 4}], [])
    assert p4["chars"] == 28 and p4["particles_per_100"] == 14.29, p4


def test_live_mock_generation_carries_the_score():
    """活体链路：mock 生成的产物里要真有 `check.ai_tells`（不是只在单测里通）。"""
    import shutil
    import tempfile
    tmp = Path(tempfile.mkdtemp(prefix="tells-"))
    try:
        shutil.copytree(ROOT / "packs", tmp / "packs")
        cfg = load_config(tmp)
        cfg.mock = True
        pl = Pipeline(tmp, cfg)
        jid = pl.start_generate(GenerateRequest(pack="elevator", topic="被困电梯怎么办",
                                               duration=60, platform="抖音"))
        snap = wait_job(pl, jid)
        assert snap["state"] == "done", snap.get("error")
        ai = snap["result"]["check"]["ai_tells"]
        assert isinstance(ai, dict) and 0 <= ai["score"] <= 100, ai
        assert ai["tells_enabled"], "包配了词表就必须有 tell 在跑"
        # 出厂 yaml 经过引擎之后不该有任何警告（`re:` 条目、`idioms` 形状、
        # 门槛覆盖、结构类漏实现都归这一条管）—— 界面只会把警告排在卡尾，
        # 没人会去读一条"平时不响、配错才响"的东西，所以端到端也要钉一次。
        assert ai["config_warnings"] == [], ai["config_warnings"]
        # L2「形态指标」必须真的随产物落盘：界面读的就是这一份。
        # 只在单测里算过 = 链路没接上（本仓反复出现的那一类缺陷）。
        sh = ai["shape"]
        assert set(sh) >= {"mattr", "trigram_repeat", "clause_len", "particles_per_100",
                           "funcword_entropy", "idioms_per_1000", "chars"}, sh
        assert sh["chars"] > 0 and sh["mattr"] and sh["clause_len"]["p50"], sh
        # 但这一层一个字都不许进分数：分数必须仍等于"只看命中"的那个算式
        from app.ai_tells import PENALTY
        expect = max(0, 100 - (ai["strong"] * PENALTY["strong"] + ai["weak"] * PENALTY["weak"]))
        assert ai["score"] == expect, (ai["score"], expect, sh)
        disk = next(tmp.glob("generated/*/*/result.json"))
        assert "ai_tells" in disk.read_text(encoding="utf-8")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ── 导出的 脚本.md 必须带人味分（2026-09-23）────────────────────
def _md_result(ai_tells):
    """一份刚好够 `store.render_script_md` 跑完的最小产物。"""
    return {
        "id": "x", "created_at": "", "pack": "elevator", "pack_draft": False,
        "params": {"topic": "家用电梯怎么挑", "duration": 60, "platform": "抖音",
                   "style": "亲和接地气", "persona": "维保老师傅",
                   "segment": "家用电梯", "audience": "业主乘客"},
        "quota": {"total": 290}, "quota_degraded": False,
        "plan": {"angle": "a", "hook_type": "反常识", "hook_line": "h",
                 "points": ["一"], "cta": "关注"},
        "sections": [{"type": "hook", "text": "开场。", "subtitle": "开场"}],
        "storyboard": [], "scenes": [], "placeholders": [], "revisions": [],
        "timings": [{"start": 0, "end": 3}], "logs": [],
        "check": {"chars_total": 3, "target_total": 290, "estimated_seconds": 3,
                  "deviation_pct": -95.0, "passed": False, "blockers": ["太短"],
                  "hard_hits": [], "soft_hits": [], "segments": [], "ai_tells": ai_tells},
    }


def test_script_md_carries_the_human_score():
    """md 是**最终交付物** —— 人味分只在界面上露，导出后又变回不可见。

    与 `quota_degraded` 是同一条规矩（`app/store.py` 里两处相邻的注释）。
    """
    from app.store import render_script_md

    md = render_script_md(_md_result({
        "score": 88, "strong": 0, "weak": 1, "hits": [
            {"id": "bookish_connective", "severity": "weak", "count": 2,
             "where": "第2段", "detail": "首先×1、其次×1", "words": []}],
        "tells_enabled": ["bookish_connective"], "config_warnings": [],
        "placeholders": {"count": 0, "per_100": None, "cap": 1}}))
    assert "人味分 88/100" in md, md
    # 命中明细要跟着导出，但靠的是**给人读的那串**（`detail` + `where`），
    # 不是机器键。旧断言在这里查 `bookish_connective` —— 它把"交付物里出现
    # 内部标识"当成了正确行为（第 28 轮导出线 P1）。
    assert "首先×1、其次×1" in md and "第2段" in md, md
    assert "不参与校验结论" in md, "必须明说它不拦发布，否则 88 分会被读成未通过校验"


def test_script_md_has_no_internal_identifiers():
    """交付物里不许出现机器码：tell 的 `id`、严重度的 `strong` / `weak` 字面。

    现场（第 28 轮导出线 P1）：`generated/*/脚本.md` 尾部实读
    `（strong 0 条 · weak 0 条）` 与 `- [strong] bookish_connective（…）` ——
    这份文件会被用户拷进笔记、发给同事，读到的人不知道那是什么。
    中文读法的源头在 `app/ai_tells.py: SEVERITY_WORD`（与 `words.js` 成对，
    由 test_job_state_vocabulary_consistency 钉住），`id` 只留在 `result.json`。
    """
    from app.store import render_script_md

    md = render_script_md(_md_result({
        "score": 40, "strong": 1, "weak": 2, "hits": [
            {"id": "bookish_connective", "severity": "strong", "count": 1,
             "where": "第1段", "detail": "品质保证×1", "words": ["品质保证"]}],
        "tells_enabled": ["bookish_connective"], "config_warnings": [],
        "placeholders": {"count": 0, "per_100": None, "cap": 1}}))
    # ⚠ 这一行原先断言的是「明显 1 **处** · 轻微 2 **处**」—— 把错的单位钉成了规范
    #   （那两个数取的是 `ai["strong"]/["weak"]`，即**命中条数**，而分数正是按条扣的）。
    #   交付物与结果页（`result.js` 的 chips）各执一词，改回"条"并在这里钉住：
    #   明细那一行印的才是"处"（下一条断言）。
    assert "明显 1 条 · 轻微 2 条" in md, md
    assert "明显（第1段，1 处）：品质保证×1" in md, md
    assert "bookish_connective" not in md, "内部标识又回到交付物了"
    assert "[strong]" not in md and "[weak]" not in md, md
    assert "strong " not in md and "weak " not in md, md


def test_script_md_never_prints_the_word_none():
    """参数"键存在但值是 None"时，交付物里不许出现 `None`。

    `p.get(k, '')` 挡不住这种形状 —— 默认值只在**键不存在**时生效，而
    `schemas.py` 允许 platform/style/persona 留空（值就是 None），
    f-string 于是把 None 印成字符串（第 28 轮导出线 P3）。
    """
    from app.store import render_script_md

    r = _md_result(None)
    for k in ("segment", "audience", "platform", "style", "persona"):
        r["params"][k] = None
    md = render_script_md(r)
    assert "None" not in md, md
    assert "细分：　受众：" in md, md      # 空就是空：两格还在同一行，不印假值


def test_script_md_timeline_has_no_trailing_zeros():
    """时间轴的尾零是机器的：引擎给 `0.0 / 8.5`，交付物与界面都该写 `0-8.5 秒`。"""
    from app.store import render_script_md

    r = _md_result(None)
    r["sections"] = r["sections"] + [{"type": "point", "text": "第二段。", "subtitle": "x"}]
    r["timings"] = [{"start": 0.0, "end": 8.5}, {"start": 8.5, "end": 12.0}]
    md = render_script_md(r)
    assert "**【开场钩子】** 0-8.5 秒" in md, md
    assert "**【要点1】** 8.5-12 秒" in md, md
    assert "0.0" not in md and "12.0" not in md, md


def test_script_md_writes_hit_lists_for_humans_not_python_repr():
    """交付物里的命中明细要排成「词×次数」，不许是 Python 的 repr。

    改前是 `f"- 硬禁用词：{ch['hard_hits'] or '无'}"` → 落到别人笔记里的是一行
    `[{'word': '补贴', 'count': 2}]`。盘上 102 份产物恰好 hard_hits 全空，
    所以这条一直没人看见 —— 空列表时**仍然**要写「无」，两种形状都钉住。
    """
    from app.store import render_script_md

    hit = _md_result(None)
    hit["check"]["hard_hits"] = [{"word": "补贴", "count": 2}]
    hit["check"]["soft_hits"] = [{"word": "最", "count": 1}]
    md = render_script_md(hit)
    assert "硬禁用词：补贴×2" in md, md
    assert "待确认：最×1" in md, md
    assert "[{'" not in md, "交付物里出现了 Python repr"

    empty = render_script_md(_md_result(None))
    assert "硬禁用词：无" in empty, empty


def test_script_md_says_no_verdict_when_check_is_silent():
    """`passed` 为 None（旧产物 / 没跑校验）时，导出的 md 不许印一行光秃秃的「❌ 」。

    改前这里是二分 `'✅ 合格' if ch['passed'] else '❌ ' + '；'.join(blockers)`：None 落到
    else，而 blockers 是空的 —— 交付物上写着「❌ 」，屏幕上写着「没有校验结论」，
    看到的与拿走的分成两份（文案线 P0-3 的另一半：`app/store.py` 也在这条线上，
    三态词由 tests/test_job_state_vocabulary_consistency.py 与 words.js 对账钉住）。
    """
    from app.store import render_script_md

    none_case = _md_result(None)
    none_case["check"]["passed"] = None
    none_case["check"]["blockers"] = []
    md = render_script_md(none_case)
    assert "没有校验结论" in md, md
    assert "❌" not in md, "结论缺失不许画成失败"
    assert "合格" not in md, "md 的用词必须跟 words.js 那张表一致 —— 表里没有「合格」"
    # 未通过那一档把原因跟在词后面，别只留一句「未通过校验」让人去猜是哪条。
    assert "未通过校验：太短" in render_script_md(_md_result(None))


def test_script_md_says_nothing_when_tells_were_not_measured():
    """`ai_tells` 落 null = **本次没测**，与「测了、很好」是两件事。

    所以这时**不许**输出任何分数行 —— 输出「人味分 100/100」就是把
    "没测" 冒充成 "满分"，正是本项目一直在治的那种静默降级。
    """
    from app.store import render_script_md

    md = render_script_md(_md_result(None))
    assert "人味分" not in md, md


def test_repeat_opening_reports_when_it_should():
    """★ `repeat_opening` 的**正命中**在整个测试网里一次都没执行过（覆盖率实测）。

    2026-09-29 分支覆盖跑出来 `app/ai_tells.py` 那句 `return [TellHit("repeat_opening"…)`
    从未被走到 —— 这条 tell 只被证明过"不该报时不报"，从没被证明"该报时报得出"。
    后果：`_sentences` 的切句口径（`／`、标点集）一改，它就会静默不响而全绿。
    三个样本各钉一个边界，缺一个就会让某条比较符变成空转：
      · 4 句里 3 句同头 → 报（`n>=3` 与 `>1/3` 同时成立）
      · 4 句里 2 句同头 → 不报（钉住 `n >= 3`：写成 `>= 2` 这条必须红）
      · 9 句里 3 句同头 → 不报（3/9 == 1/3，钉住 `>`：写成 `>=` 这条必须红）
    """
    t = AITells({"weak": ["repeat_opening"]})
    got = t.scan([{"type": "point", "text": "维保要看记录。维保要看现场。维保要看资质。这块水很深。"}])
    assert _ids(got) == ["repeat_opening"], got
    assert got[0].count == 3 and "维保" in got[0].detail, got[0].detail

    two_of_four = [{"type": "point", "text": "维保要看记录。维保要看现场。这块水很深。那台也查过。"}]
    assert t.scan(two_of_four) == [], "两处同头就报：正常中文里两连是常态，不是套路"
    nine = [{"type": "point", "text": "先看记录。先看现场。先看资质。门要关严。梯要定期查。"
                                         "井道要清理。救援要等。记录要留。业主要知道。"}]
    assert t.scan(nine) == [], "3/9 恰好等于 1/3 就报：阈值边界是 >，不是 >="


def test_declared_tell_without_words_is_announced():
    """「词表写空不许伪装成人味 100 分」这句承诺，此前只有"为空"的反证（覆盖率实测 801 行 0 次）。

    声明了词表类 tell 却不给任何词 = 一条永不响的死 tell：命中 0、分数 100，
    界面上看不出与"这篇稿子很干净"的区别。现在要求它进 `config_warnings`。
    正向对照同样要给：**全靠 `re:` 框架的 `opening_ban` 不算空**
    （A-9 之后正则条目不进 `self.lexicon`，判据得跟着 schema 一起改）。
    """
    t = AITells({"weak": ["filler_stack"]})
    assert t.lexicon_missing == ["filler_stack"], t.lexicon_missing
    assert any("filler_stack" in x for x in t.warnings()), t.warnings()
    r = t.report([{"type": "point", "text": "此外还有一点。此外再说一句。"}])
    assert r["hits"] == [] and r["score"] == 100, r
    assert any("filler_stack" in x for x in r["config_warnings"]), r["config_warnings"]

    regex_only = AITells({"strong": ["opening_ban"], "lexicon": {"opening_ban": ["re:作为一[个名位]"]}})
    assert regex_only.lexicon_missing == [], regex_only.lexicon_missing
    assert _ids(regex_only.scan([{"type": "hook", "text": "作为一个业主。"}])) == ["opening_ban"]


def test_lexicon_entry_forms_the_guide_documents_all_parse():
    """`docs/技能包制作指南.md` §10.1 教包作者写三种形态，但其中两支**从没被执行过**。

    覆盖率实测：`_lex_entry` 的 153/156/164-171 全未执行 —— 也就是"词 + 一行说明"、
    `unless` 写成裸字符串、`note` 为空串、多键 dict 这些写法一旦有笔误，
    静默按"无限定"处理，包作者看不出来。这里逐形态钉返回值，
    再把 `unless: "工单闭环"`（字符串那一支）**端到端**验一遍真能豁免：
    只看解析结果不等于只看解析结果 —— 豁免要真的不报。
    """
    from app.ai_tells import _lex_entry
    for item, want in (
        ("闭环", ("闭环", None)),
        ({"闭环": "仅黑话义"}, ("闭环", {"note": "仅黑话义"})),
        ({"闭环": ""}, ("闭环", None)),                       # 空说明 = 没限定
        ({"闭环": {"note": "  "}}, ("闭环", None)),
        ({"闭环": {"unless": "工单闭环"}}, ("闭环", {"unless": ["工单闭环"]})),   # 裸字符串
        ({"闭环": {"unless": ["", "  "]}}, ("闭环", None)),   # 全空豁免 = 没豁免
        ({"闭环": {"note": "仅黑话义", "unless": ["工单闭环"]}},
         ("闭环", {"note": "仅黑话义", "unless": ["工单闭环"]})),
        ({"a": 1, "b": 2}, ("", None)),        # 多键 dict 读不出是哪条
        (5, ("", None)),                        # 既不是 str 也不是 dict
        ({"": "说明"}, ("", None)),             # 词是空串
    ):
        assert _lex_entry(item) == want, f"{item!r} → {_lex_entry(item)} ≠ {want}"

    t = AITells({"strong": ["abstract_noun_ending"],
                 "lexicon": {"abstract_noun_ending": [{"闭环": {"note": "仅黑话义",
                                                                "unless": "工单闭环"}}]}})
    hit = t.scan([{"type": "point", "text": "这样才形成管理闭环。"}])
    assert _ids(hit) == ["abstract_noun_ending"], hit
    assert "仅黑话义" in hit[0].detail, hit[0].detail        # note 跟着命中进反馈
    assert t.scan([{"type": "point", "text": "维保工单闭环要留痕。"}]) == [], \
        "unless 写成字符串就不生效：行业真术语又被当黑话报"


def test_empty_and_degenerate_inputs_do_not_invent_hits():
    """覆盖率探针剩下的四行：`ai_tells.py:170/201/651/665` 在 958 条测试下 0 次执行。

    这四行守的都是同一件事 —— **没有内容**时的形状：
      · 词表条目写成 `{"闭环": 5}`（限定既不是字符串也不是映射）→ 词要留下、限定丢掉；
      · `_scan_words` 拿到空词表 → 必须返回空，不能让 `re.compile("")` 去匹配每个位置
        （空模式处处命中 = 一条 tell 突然"每篇都满命中"）；
      · 全篇一句都没有 → `_first_sentence` 给 None，`_opening_ban` 必须就此收住
        （不收住是 `TypeError`，而作业只会记成一句看不懂的失败）。
    """
    from app.ai_tells import _lex_entry, _scan_words
    assert _lex_entry({"闭环": 5}) == ("闭环", None), "词留着、限定静默丢弃才对"
    assert _scan_words("此外还有一点。此外再说一句。", []) == {}, "空词表不许处处命中"
    assert _scan_words("", ["此外"]) == {}, "空正文不许凭空记账"

    t = AITells({"strong": ["opening_ban", "no_specific"],
                 "lexicon": {"opening_ban": ["re:作为一[个名位]", "大家好"]}})
    for empty in ([], [{"type": "hook", "text": ""}], [{"type": "hook", "text": "　 \u3000 。"}]):
        ids = _ids(t.scan(empty))
        assert "opening_ban" not in ids, f"没有首句却报了开场违规：{empty} → {t.scan(empty)}"
    # 正对照：同样这份配置，有首句时两条支都照报（别让上面的收住变成把 tell 关掉）。
    # ⚠ 用 `in` 而不是 `==`：这两句都**没有具体数字**，`no_specific` 也该一起报 ——
    #   本条要钉的是"有首句就报开场违规"，不是给另一条 tell 记账。
    assert "opening_ban" in _ids(t.scan([{"type": "hook", "text": "作为一个业主，我踩过坑。"}]))
    assert "opening_ban" in _ids(t.scan([{"type": "hook", "text": "大家好，今天说说电梯。"}]))


def test_broken_lexicon_or_threshold_entries_are_announced():
    """四条"坏配置要说出口"的公告支从没被执行过（390/397/443/475，覆盖率实测）。

    每一支都配**正对照**：只断言"有警告"的话，把整条 tell 关掉也能骗过它。
    """
    # ① 读不出词形的条目：丢掉并说出口，剩下的好条目照跑
    t = AITells({"strong": ["filler_stack"],
                 "lexicon": {"filler_stack": ["此外", {"a": 1, "b": 2}, 5]}})
    assert any("读不出词形" in x for x in t.warnings()), t.warnings()
    assert t.lexicon["filler_stack"] == ["此外"], t.lexicon
    assert _ids(t.scan([{"type": "point", "text": "此外还有一点。"}])) == ["filler_stack"]

    # ② 编译不了的正则：报出具名条目，别条正则仍生效
    t2 = AITells({"strong": ["opening_ban"],
                  "lexicon": {"opening_ban": ["re:(", "re:作为一[个名位]"]}})
    assert any("编译不了" in x for x in t2.warnings()), t2.warnings()
    assert _ids(t2.scan([{"type": "hook", "text": "作为一个业主。"}])) == ["opening_ban"]

    # ③ thresholds 写成列表：说明"按默认口径"，并且默认口径真的在跑（1 处不报、2 处才报）
    t3 = AITells({"weak": ["filler_stack"], "lexicon": {"filler_stack": ["此外"]},
                  "thresholds": ["filler_stack"]})
    assert any("thresholds 需要" in x for x in t3.warnings()), t3.warnings()
    assert t3.scan([{"type": "point", "text": "此外还有一点。"}]) == []
    assert len(t3.scan([{"type": "point", "text": "此外还有一点。此外再说一句。"}])) == 1

    # ④ 门槛 <1：同样"报出来 + 保持默认"，不能让 0 变成"处处都报"
    t4 = AITells({"weak": ["filler_stack"], "lexicon": {"filler_stack": ["此外"]},
                  "thresholds": {"filler_stack": 0}})
    assert any("须 ≥1" in x for x in t4.warnings()), t4.warnings()
    assert t4.thresholds == {}, t4.thresholds
    assert t4.scan([{"type": "point", "text": "此外还有一点。"}]) == []


def test_ai_tells_wrong_type_is_announced_not_silent(tmp_path):
    """pack.yaml 的 `ai_tells:` 键写成映射/列表 → 要出说明，不能无声关闭（P3）。

    `str({...})` 拼出的路径永远不存在 → 返回 None 且 notes 为空 ——
    本函数唯一完全无声的坏法。键为空（None/缺省）视作"没配"，不算这一条。
    """
    from app.knowledge import resolve_ai_tells
    data, notes = resolve_ai_tells(tmp_path, {"ai_tells": {"strong": ["x"]}})
    assert data is None
    assert notes and "不符合约定" in notes[0], f"静默关闭了：{notes}"
    # 正对照：键为 None（写了但空）= 没配，照旧静默
    data2, notes2 = resolve_ai_tells(tmp_path, {"ai_tells": None})
    assert data2 is None and notes2 == []


def test_thresholds_nan_and_inf_fall_back_with_a_word_not_a_crash():
    """P2-10（2026-10-05）：`.nan`/`.inf` 能被 float() 收下，却在 int() 处炸
    ValueError/OverflowError——触发点在 `_load_tells`（select/draft 的模型钱
    花掉之后作业 failed），且 `_readable_error` 对这两种英文消息没有翻译规则。
    与 true/"2"/2.9/-1 同一条立场：警告 + 默认口径，「读坏不静默」不该变成
    「读坏就炸」。

    P2-2（2026-10-11）把样本按两条腿钉全：
    - float() 不收的字符串形态（.nan/.inf）—— 本来就走「不是整数」警告，从未炸过；
    - float() 收得下的字符串形态（"nan"/"inf"/"1e999" → nan/inf）—— P2-1 修复前
      会在 str_form 文案的 `int(num)` 处炸，现由 isfinite 闸兜住（闸已排在
      str_form 之前），走「不是有限的数——已按默认口径」路径，tell 照常按默认
      门槛跑。"""
    # 数值形态（YAML 的 `.nan`/`.inf` 落成 float）：修复前在 int() 处炸
    for v in (float("nan"), float("inf"), float("-inf")):
        t = AITells({"weak": ["cliche_hook"], "thresholds": {"cliche_hook": v}})
        assert "cliche_hook" not in t.thresholds, (v, t.thresholds)
        assert any("不是有限的数" in w and "cliche_hook" in w for w in t.warnings()), \
            (v, t.warnings())
    # 字符串形态 · float() 不收的（.nan/.inf）：走既有的「不是整数」警告（同样不炸）
    for v in (".nan", ".inf"):
        t = AITells({"weak": ["cliche_hook"], "thresholds": {"cliche_hook": v}})
        assert "cliche_hook" not in t.thresholds, (v, t.thresholds)
        assert any("thresholds.cliche_hook" in w and "默认口径" in w for w in t.warnings()), \
            (v, t.warnings())
    # 字符串形态 · float() 收得下的（"nan"/"1e999" → nan/inf）：P2-1 修复前在
    # str_form 文案的 int(num) 处炸；现在走 isfinite 闸的「不是有限的数」警告，
    # 门槛按默认口径回落，tell 本身照常声明、照常跑 —— 与完全没配 thresholds
    # 的实例逐项等价（thresholds 表、tell 声明、警告差一条「不是有限的数」）。
    for v in ("nan", "1e999"):
        t = AITells({"weak": ["cliche_hook"], "thresholds": {"cliche_hook": v}})
        t_default = AITells({"weak": ["cliche_hook"]})
        assert t.thresholds == t_default.thresholds, (v, t.thresholds)
        assert t.severity.get("cliche_hook") == WEAK, (v, t.severity)
        assert any("不是有限的数" in w and "cliche_hook" in w and "默认口径" in w
                   for w in t.warnings()), (v, t.warnings())
        extra = [w for w in t.warnings() if w not in t_default.warnings()]
        assert extra and all("不是有限的数" in w for w in extra), (v, extra)
