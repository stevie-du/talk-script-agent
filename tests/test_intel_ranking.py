# -*- coding: utf-8 -*-
"""热点排行与评估层的契约（`docs/需求方案-热点排行与选题建议.md` 阶段一 + 阶段二）。

每一条用例对应方案里的一行验收，标题里带 § 编号，改口径时先改文档再改这里。
判据方向沿本仓老规矩：**能证伪的断言** —— 拿真实形状喂进去，要求一个具体数字，
而不是"跑通了没抛"。

夹具来自 `tests/test_intel.py`（同一份假 HTTP，避免"两套桩各说各话"）：
新源（微博 / 百度）的形状也在那份桩里，所以这里加源不需要再造一份。
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import test_intel as T                                    # noqa: E402
import app.intel as I                                     # noqa: E402

_fake_http, _specs = T._fake_http, T._specs


def _now():
    return datetime.now(timezone.utc).astimezone()


def _item(title, *, label, guid=None, hot=None, source_id="", published="",
          segment="", platform="", desc="", seed=""):
    return {"title": title, "url": "", "guid": guid or title, "hot": hot,
            "published": published, "desc": desc, "source_id": source_id,
            "source_label": label, "platform": platform, "role": "破圈触发器",
            "segment": segment, "seed": seed}


def _snap(days_ago: int, rows: list[dict], *, source_rows=None) -> dict:
    """伪造一份 history 快照（旧→新的顺序由调用方管）。"""
    t = _now() - timedelta(days=days_ago)
    return {"fetched_at": t.isoformat(timespec="seconds"), "items": rows,
            "sources": source_rows or []}


# ── 阶段一 #1：S 的「没数据 ≠ 零竞争」──────────────────────────
def test_S_is_none_not_zero_when_this_segment_has_no_supply():
    """§1.2 #4①：该细分自己一条 B站供给都没有 → S=None，而不是 0.0。

    0.0 在界面上的意思是"零竞争、蓝海"，None 的意思是"没证据"——
    修复前只有"全局没有 B站数据"才给 None，于是别的领域有供给时，
    没供给的那个领域被印成了**最**值得做的那一个。
    """
    items = [
        _item("电梯维保合同模板", label="B站同类", source_id="bilibili_search",
              segment="维保"),
        _item("电梯维保周期规定", label="B站同类", source_id="bilibili_search",
              segment="维保"),
        _item("整机改造流程", label="下拉词", source_id="demand_terms",
              segment="整机改造"),
    ]
    out = I.score_topics(items, set(), events=[])
    assert out["维保"]["S"] == 1.0, "有供给的领域照旧算相对值"
    assert out["整机改造"]["S"] is None, "该细分没有 B站条目 → 没证据，不是零竞争"
    assert out["整机改造"]["opportunity"] is None
    assert items[-1]["score"]["S"] is None and items[-1]["score"]["opportunity"] is None
    # 反向对照：全局无 B站数据时，有供给的那一支也必须是 None（旧行为不能丢）
    only_demand = [_item("整机改造流程", label="下拉词", source_id="demand_terms",
                         segment="整机改造")]
    I.score_topics(only_demand, set(), events=[])
    assert only_demand[0]["score"]["S"] is None


# ── 阶段一 #2：归属要吃 seed ─────────────────────────────────
def test_seed_wins_over_title_literal_when_matching_segment():
    """§1.2 #4②：现成的 `seed` 字段过去根本不进文本，实盘有一批条目落进空桶。"""
    row = {"title": "电梯维保多少钱", "desc": "", "keyword": "", "seed": "整机改造"}
    got = I._item_segment(row, {}, ["整机改造", "维保"])
    assert got == "整机改造", "seed 是我们主动查的那个词，优先级高于标题字面"
    # 没有 seed 时才轮到 keyword / title（旧行为保留）
    assert I._item_segment({"title": "电梯维保记录", "seed": ""}, {},
                           ["整机改造", "维保"]) == "维保"
    # topics_map 的映射词仍然先于字面命中（B-2 的两档顺序没动）
    assert I._item_segment({"title": "旧楼加梯补贴", "seed": ""},
                           {"加装电梯": "加梯"}, ["加装电梯", "维保"]) == "加装电梯"


def test_fetch_pack_routes_seed_bearing_items_out_of_the_empty_bucket(tmp_path):
    """同一条口径在**整条链路**上生效：空桶条目必须真的减少（不是单测里好看）。"""
    out = I.fetch_pack("elevator", tmp_path, _specs(),
                       seeds=["电梯困人"], keywords=["电梯"],
                       topics_map={}, segment_options=["电梯困人"],
                       http=_fake_http())
    # 下拉词那一路的 seed 就是"电梯困人"这个细分本身
    demand = [it for it in out["items"] if it["source_id"] == "demand_terms"]
    assert demand and all(it["segment"] == "电梯困人" for it in demand), \
        "带 seed 的条目不再落进空桶"


# ── 阶段一 #3：E 用首见兜底，首次抓取仍是 null ────────────────
def test_E_uses_first_seen_when_source_has_no_published():
    """§1.2 #4③：热榜三类源都没有 `published`（实盘占多数），过去 E 恒 null。"""
    it = _item("电梯困人索赔", label="微博热搜", hot=1000)
    fresh = I.build_events([it], ranks={}, history=[], now=_now())
    I.score_topics([it], set(), events=fresh)
    assert fresh[0]["seen_before"] is False
    assert it["score"]["E"] is None, "连首见都没有 → 仍然 null，不猜成 1.0"

    old = _snap(3, [dict(it)])
    again = I.build_events([it], ranks={}, history=[old], now=_now())
    I.score_topics([it], set(), events=again)
    assert again[0]["seen_before"] is True
    assert 0 < again[0]["days_on_board"] <= 4
    e = it["score"]["E"]
    assert e is not None and 0 < e < 1, "三天前见过 → E 衰减到一个真实存在的小数"


# ── 阶段一 #4：事件聚合 ─────────────────────────────────────
def test_event_cluster_regression_set():
    """回归集（`tests/data/event_clusters.json`）：merge 的必须并成 1，分开的必须留 2。

    ⚠ 这条测试**不许**为了通过而改样本；改 `EVENT_JACCARD` / `_normalize_title` /
    `same_event` 任一处，先在这里看到后果，再决定是调阈值还是改样本标注。
    """
    data = json.loads((ROOT / "tests" / "data" / "event_clusters.json")
                      .read_text(encoding="utf-8"))
    assert len(data["merge"]) >= 10 and len(data["keep_apart"]) >= 8, \
        "回归集被删薄了 —— 阈值校准就失去了依据"
    for pair in data["merge"]:
        got = I.cluster_events([_item(t, label="微博热搜", guid=f"m{i}")
                                for i, t in enumerate(pair)])
        assert len(got) == 1, f"应并成同一事件却没有：{pair}"
    for pair in data["keep_apart"]:
        got = I.cluster_events([_item(t, label="微博热搜", guid=f"a{i}")
                                for i, t in enumerate(pair)])
        assert len(got) == 2, f"两件事被并成了一条：{pair}"


def test_containment_branch_needs_the_length_floor():
    """`same_event` 的包含那一支必须带 `EVENT_MIN_CONTAIN` 这道长度闸。

    回归集抓不到这一条（里面的样本都够长），所以这里直接判边界：
    没有闸的时候，「怎么办」这种三字尾巴是几十个标题的后缀，整包会被并成一堆；
    有闸才承认"长尾补充"这一类真同题。
    """
    assert I.same_event("电梯困人怎么办", "电梯困人怎么办是什么") is True
    assert I.same_event("怎么办", "电梯困人怎么办") is False
    assert I.same_event("谁来管", "三不电梯谁来管") is False
    assert I.EVENT_MIN_CONTAIN >= 3, "闸值降到 3 以下等于没有闸"


def test_today_marks_a_cooling_source_as_skipped(tmp_path):
    """§3.1 的「当日跳过」必须走到**读路径**上。

    `fetch_pack` 那一条只证明本轮没去发请求；界面上的三态来自 `today()` 重建的
    groups —— 那边过去只按声明给 ok / off / unwired 三值，"跳过"会被说成
    「已接入，今天没有命中」，而这两句话的下一步动作相反（等明天 vs 去看为什么挂）。
    """
    t = _now()
    fail_row = {"label": "抖音热榜", "count": 0, "error": "RuntimeError: 401",
                "state": "error"}
    # 三条失败快照打在**今天凌晨**，而不是"距今 3/4/5 小时"：后者在凌晨跑测试时
    # 会集体落到昨天（本机实测 02:07 减 3 小时 = 前一天 23:07），于是"最近那次
    # 发生在今天"这道闸不成立、skip 一直是 False —— 那是判据写法在骗自己。
    early = [t.replace(hour=0, minute=i, second=0, microsecond=0) for i in (1, 2, 3)]
    _write_history(tmp_path, [
        {"fetched_at": e.isoformat(timespec="seconds"),
         "items": [], "sources": [fail_row], "events": []} for e in early
    ])
    I.fetch_pack("elevator", tmp_path, _specs(), seeds=["电梯困人"],
                 keywords=["电梯"], http=_fake_http())
    view = I.today(tmp_path, "elevator", _specs())
    dou = next(g for g in view["groups"] if g["label"] == "抖音热榜")
    assert dou["state"] == "cooling", "读路径要把跳过透出来，不能压成 ok"
    assert "跳过" in dou["error"]
    assert dou["health"]["consecutive_fails"] == 3
    # 正对照：没到闸值的源不许被说成跳过（否则一次抖动就永久闭嘴）
    other = next(g for g in view["groups"] if g["label"] == "下拉词")
    assert other["state"] == "ok" and not other["error"]


def test_two_platforms_same_event_count_once_for_D_but_stay_two_items():
    """§3.1 末行：D 改按**事件数**计新收录，S 维持条目数（两层各管各的）。"""
    a = _item("电梯困人怎么办", label="微博热搜", source_id="weibo_hot", segment="困人")
    b = _item("电梯困人怎么办？", label="百度热搜", source_id="baidu_hot",
              segment="困人")
    events = I.build_events([a, b], ranks={}, history=[], now=_now())
    assert len(events) == 1, "同一件事跨两个平台 → 一个事件"
    assert events[0]["source_count"] == 2, "但两条来源证据都要留着"
    out = I.score_topics([a, b], set(), events=events, prev_event_keys=set())
    assert out["困人"]["D"] == 1.0
    # 两条都算"新条目"时 D 仍按 1 个事件计：拿 prev_event_keys 换掉那一个事件键 → 0
    again = I.score_topics([a, b], set(), events=events,
                           prev_event_keys={events[0]["key"]})
    assert again["困人"]["D"] is None, "今天没有新事件 → None，不是 0"


# ── 阶段一 #5 / 阶段二 #3：热点分与四根分项 ──────────────────
def test_hot_score_terms_and_their_weights():
    """§3.2 的 40/30/20/10 摊开显示：总分 = 各项加权和 − 衰减，缺项不计入。"""
    rows = [_item("事件甲", label="微博热搜", source_id="weibo_hot", hot=900),
            _item("事件甲", label="百度热搜", source_id="baidu_hot", hot=800),
            _item("事件甲", label="抖音热榜", source_id="hot_board", hot=700),
            _item("事件甲", label="知乎", source_id="hot_board", hot=600)]
    ranks = {I.item_key(r): (2, 50) for r in rows}
    ev = I.build_events(rows, ranks=ranks, history=[], now=_now())[0]
    s = ev["signals"]
    assert s["diversity"] == 1.0, "4 个来源 = 满分（HOT_SPREAD_TARGET）"
    assert s["velocity"] is None, "history 不满 7 天 → 增速 null（阶段二验收）"
    assert abs(s["rank"] - (50 - 2) / 50) < 1e-6
    expected = (I.HOT_WEIGHTS["diversity"] * 1.0
                + I.HOT_WEIGHTS["rank"] * s["rank"]
                + I.HOT_WEIGHTS["persistence"] * s["persistence"])
    assert abs(ev["hot_score"] - round(expected)) <= 1


def test_missing_velocity_is_not_penalised_but_missing_all_four_is_weak():
    """§3.2 的 null 规则：**分项缺 → 不计入、总分照常算**；四项全缺 → 信号不足。"""
    it = _item("孤立事件", label="下拉词", source_id="demand_terms")
    ev = I.build_events([it], ranks={}, history=[], now=_now())[0]
    assert ev["signals"]["velocity"] is None and ev["signals"]["rank"] is None
    assert ev["hot_score"] is not None, "缺两项不等于没有分"
    # 降级产物（旧 latest.json 合成的事件行）才是"全缺"
    weak = I._events_from_items([it])[0]
    assert all(v is None for v in weak["signals"].values())
    assert weak["weak_signal"] is True and weak["hot_score"] is None


def test_ranking_order_is_stable_across_runs_and_input_permutation():
    """§3.2「读时只排序不重算」的另一半：**同一份输入必须排出同一个顺序**。

    「换一批」是纯前端翻页，池子顺序变了用户就会看到同一条事件在两屏重复出现。
    """
    rows = [_item(f"事件{i}", label="微博热搜", source_id="weibo_hot",
                   guid=f"s{i}", hot=100 * i) for i in range(1, 8)]
    ranks = {I.item_key(r): (i + 1, len(rows)) for i, r in enumerate(rows)}
    a = [e["key"] for e in I.build_events(rows, ranks=ranks, history=[], now=_now())]
    b = [e["key"] for e in I.build_events(list(reversed(rows)), ranks=ranks,
                                          history=[], now=_now())]
    assert a == b, "输入顺序不该改变排行"
    assert len(set(a)) == len(a)


def test_velocity_and_breakout_pill_after_a_full_baseline_window():
    """阶段二 #3：窗口满 7 天才给增速；超过 5× 标「爆发中」。"""
    it = _item("电梯困人新国标", label="微博热搜", source_id="weibo_hot", hot=50_000_000)
    history = [_snap(9 - i, [dict(it, hot=1_000_000)]) for i in range(7)]
    ev = I.build_events([it], ranks={}, history=history, now=_now())[0]
    assert ev["growth"] == pytest.approx(50.0, rel=1e-3)
    assert ev["signals"]["velocity"] == 1.0, "封顶 1"
    assert ev["status"] == "breakout"
    # 窗口不满 → null，且**不因缺项扣分**
    short = [_snap(2, [dict(it, hot=1_000_000)])]
    ev2 = I.build_events([it], ranks={}, history=short, now=_now())[0]
    assert ev2["signals"]["velocity"] is None and ev2["growth"] is None


def test_same_day_snapshots_are_not_a_seven_day_baseline():
    """§3.2「近 7 天基线」量的是**天**，不是抓取次数。

    一天里点三次「重抓」就落三份快照。按快照数算基线的话，新装用户第一天连点
    七次就凑满了"窗口"，分母变成"今天早上" —— 一次正常的日内波动足够冲破 5×，
    于是榜首天天「爆发中」，而这一项唯一的作用就是区分 trending 与 top。
    """
    it = _item("一天内的正常波动", label="微博热搜", source_id="weibo_hot",
               hot=50_000_000)
    anchor = _now().replace(hour=0, minute=0, second=0, microsecond=0)
    same_day = [{"fetched_at": (anchor + timedelta(minutes=i)).isoformat(timespec="seconds"),
                 "items": [dict(it, hot=1_000_000)], "sources": [], "events": []}
                for i in range(7)]
    ev = I.build_events([it], ranks={}, history=same_day, now=_now())[0]
    assert ev["growth"] is None, "七份**同一天**的快照 = 一天基线都没有，不是「7 个样本」"
    assert ev["signals"]["velocity"] is None
    assert ev["status"] != "breakout", "没有基线就不许给出「爆发中」"
    assert ev["days_on_board"] == 1, "同一天抓七次不是在榜七天"


def test_baseline_window_is_the_recent_days_not_the_whole_history():
    """窗口取**最近** 7 天：分母若是三周前的水位，一个缓慢涨上来的题永远到不了档。"""
    it = _item("缓慢涨上来的题", label="微博热搜", source_id="weibo_hot", hot=2_000_000)
    old = [_snap(d, [dict(it, hot=1_000)]) for d in range(23, 7, -1)]
    recent = [_snap(d, [dict(it, hot=1_000_000)]) for d in range(7, 0, -1)]
    ev = I.build_events([it], ranks={}, history=old + recent, now=_now())[0]
    assert ev["growth"] == pytest.approx(2.0, rel=1e-3), "分母 = 最近 7 天的均值（100 万）"
    assert ev["status"] != "breakout", "2× 是稳步涨，不是爆发"


def test_decay_only_when_not_growing_and_never_below_zero():
    """§3.2 末行：首见 >48h 且没有在涨 → 每半天 −5，扣到 0 为止。"""
    it = _item("三天前的旧闻", label="微博热搜", source_id="weibo_hot", hot=10)
    history = [_snap(4, [dict(it)]), _snap(3, [dict(it)])]
    ev = I.build_events([it], ranks={}, history=history, now=_now())[0]
    assert ev["decay"] >= 5.0 and ev["status"] == "fading"
    # 同一个事件但在暴涨 → 不衰减（阶段二补的那道前置条件）
    # ⚠ 基线要**七个不同的、早于今天的天**：`_snap(0, …)` 是今天，今天不进分母
    #   （环比问的是"今天 vs 之前一周"，把自己算进去等于把自己稀释掉）。
    rising = [_snap(4, [dict(it, hot=1000)]), _snap(3, [dict(it, hot=1000)]),
              _snap(2, [dict(it, hot=1000)]), _snap(1, [dict(it, hot=1000)]),
              _snap(5, [dict(it, hot=1000)]), _snap(6, [dict(it, hot=1000)]),
              _snap(7, [dict(it, hot=1000)])]
    ev2 = I.build_events([dict(it, hot=9_000_000)], ranks={}, history=rising,
                         now=_now())[0]
    assert ev2["decay"] == 0.0, "增速为正就不该扣衰减"


# ── 阶段一 #6：平台真实热度有人接 ────────────────────────────
def test_platform_hot_is_numeric_and_reaches_the_event_row():
    """§1.2 #1：抖音 HotValue 是**字符串**、知乎给的是「9904 万热度」人话。

    不归一就会拿字符串比大小（"900" > "10000" 成立），排行榜安静地排错序，
    而界面上看不出任何异常 —— 这条测试钉住 `_hot_num` 与事件行的 `hot`。
    """
    assert I._hot_num("7713788") == 7713788
    assert I._hot_num("9904 万热度") == 99_040_000
    assert I._hot_num("1.2亿") == 120_000_000
    assert I._hot_num("900") == 900
    assert I._hot_num(None) is None and I._hot_num("") is None
    assert I._hot_num(False) is None, "bool 不是热度：False 变成 0 就是「这条零热度」"
    rows = [_item("甲", label="微博热搜", source_id="weibo_hot", hot="900"),
            _item("甲", label="百度热搜", source_id="baidu_hot", hot="10000")]
    ev = I.build_events(rows, ranks={}, history=[], now=_now())[0]
    assert ev["hot"] == 10000, "取各源最高的那个，且按数值比"


# ── 阶段一 #7：history 读路径的降级 ─────────────────────────
def test_load_history_skips_broken_snapshots_without_raising(tmp_path):
    I.history_dir(tmp_path, "elevator").mkdir(parents=True)
    good = {"fetched_at": _now().isoformat(timespec="seconds"), "items": [],
            "sources": [], "events": []}
    (I.history_dir(tmp_path, "elevator") / "20260101-000000.json").write_text(
        json.dumps(good), encoding="utf-8")
    (I.history_dir(tmp_path, "elevator") / "20260102-000000.json").write_text(
        "{坏的", encoding="utf-8")
    snaps = I.load_history(tmp_path, "elevator")
    assert len(snaps) == 1, "坏快照跳过，不牵连其它文件、不抛"
    assert I.load_history(tmp_path, "没有这个包") == []


def test_events_degrade_when_history_is_missing(tmp_path):
    """history 没有 / 只有一份时，排行必须**照常出**（只是增速位是「—」）。"""
    out = I.fetch_pack("elevator", tmp_path, _specs(), seeds=["电梯困人"],
                       keywords=["电梯"], http=_fake_http())
    assert out["events"], "首次抓取也要有排行行，否则第一天的选题页是空的"
    assert all(e["signals"]["velocity"] is None for e in out["events"])
    view = I.today(tmp_path, "elevator", _specs())
    assert view["events"] and view["items"]


# ── 阶段一 #8：旧 latest.json（无 events 键）的兼容降级 ──────
def test_old_latest_json_without_events_still_reads(tmp_path):
    d = I.intel_dir(tmp_path, "elevator")
    d.mkdir(parents=True)
    (d / "latest.json").write_text(json.dumps({
        "pack": "elevator", "fetched_at": _now().isoformat(timespec="seconds"),
        "sources": [], "items": [dict(_item("旧格式条目", label="下拉词"),
                                      score={"D": 0.4, "S": None,
                                             "opportunity": None, "E": None,
                                             "is_new": True})]},
        ensure_ascii=False), encoding="utf-8")
    view = I.today(tmp_path, "elevator", _specs())
    assert view["items"], "条目池照常下发"
    assert len(view["events"]) == 1
    ev = view["events"][0]
    assert ev["hot_score"] is None and ev["weak_signal"] is True, \
        "旧产物没有跨源/名次/在榜的证据 → 信号不足，而不是 0 分"
    assert view["errors"] == {}


def test_broken_events_shape_is_dropped_with_a_reason(tmp_path):
    d = I.intel_dir(tmp_path, "elevator")
    d.mkdir(parents=True)
    (d / "latest.json").write_text(json.dumps(
        {"pack": "elevator", "items": [], "events": [1, 2]}), encoding="utf-8")
    data = I.load_latest(tmp_path, "elevator")
    assert data["events"] == []
    assert "_read" not in data["errors"], "非对象元素静默剔除即可，不是文件坏了"
    (d / "latest.json").write_text(json.dumps(
        {"pack": "elevator", "items": [], "events": "不是列表"}), encoding="utf-8")
    data = I.load_latest(tmp_path, "elevator")
    assert data["events"] == [] and "_read" in data["errors"]


# ── 阶段二 #1：新适配器 ─────────────────────────────────────
def test_weibo_hot_needs_site_referer_and_publishes_onboard_time():
    """微博实测：Referer=请求 URL 本身回 403，必须是站点根 —— 所以这里要能覆盖。"""
    seen = {}

    def http(url, params=None, headers=None, **_kw):
        seen["url"], seen["headers"] = url, headers or {}
        return {"data": {"band_list": [
            {"word": "电梯困人新国标", "num": 2475642, "onboard_time": "1790609902",
             "word_scheme": "#电梯困人新国标#"}]}}

    ctx = I.FetchCtx(pack="elevator", keywords=["电梯"], http=http)
    spec = next(s for s in _specs() if s.id == "weibo_hot")
    rows = I.fetch_weibo_hot(spec, ctx)
    assert seen["headers"].get("Referer") == "https://weibo.com/"
    assert len(rows) == 1 and rows[0]["hot"] == 2475642
    assert rows[0]["published"] == "1790609902", "上榜时间是 E 与新鲜度的原料"
    assert "q=%23" in rows[0]["url"], "话题词要用 word_scheme 的 URL 编码形式"
    # 默认 Referer 仍然是请求 URL（抖音/头条那条路不能跟着换身份）
    ctx2 = I.FetchCtx(pack="elevator", http=lambda url, params=None, headers=None, **k:
                      {"word_list": []})
    I.fetch_hot_board(next(s for s in _specs() if s.label == "抖音热榜"), ctx2)


def test_baidu_hot_flattens_cards_and_coerces_string_hot():
    def http(url, params=None, headers=None, **_kw):
        assert (params or {}).get("platform") == "pc", "wise 那一份没有 hotScore"
        return {"data": {"cards": [
            {"content": [{"word": "电梯维保猫腻", "hotScore": "7713788",
                         "url": "https://e/b", "desc": "近日…"}]},
            {"content": [{"word": "老旧小区加梯", "hotScore": "120000"}],
             "updateTime": "1790609902"}]}}

    ctx = I.FetchCtx(pack="elevator", keywords=["电梯", "加梯"], http=http)
    rows = I.fetch_baidu_hot(next(s for s in _specs() if s.id == "baidu_hot"), ctx)
    assert [r["hot"] for r in rows] == [7713788, 120000], "两张卡片都要摊平"
    assert rows[0]["url"] == "https://e/b"


def test_zhihu_board_moved_off_the_dead_endpoint_and_parses_human_hot():
    """§1.2 #6 的根因之一：老端点稳定 401。换 `api.zhihu.com/topstory/hot-list`，
    它的热度只有人话一份（「9904 万热度」），必须解析成数。"""
    called = {}

    def http(url, params=None, headers=None, **_kw):
        called["url"] = url
        return {"data": [{"detail_text": "9904 万热度",
                          "target": {"id": 1, "title": "电梯困人该找谁",
                                     "url": "https://z/1", "created": 1790576966}}]}

    spec = I.parse_sources([{"id": "hot_board", "label": "知乎", "platform": "知乎",
                             "role": "破圈触发器", "cadence": "daily",
                             "params": {"board": "zhihu", "match": "keyword"}}])[0][0]
    ctx = I.FetchCtx(pack="elevator", keywords=["电梯"], http=http)
    rows = I.fetch_hot_board(spec, ctx)
    assert "api.zhihu.com/topstory/hot-list" in called["url"]
    assert "hot-lists/total" not in called["url"]
    assert rows[0]["hot"] == 99_040_000
    assert rows[0]["published"] == "", "created 是问题创建时间，不是上榜时间，不能当 published"


# ── 阶段二 #2：重试 / 退避 / 源健康 / 连续失败跳过 ───────────
def test_transient_failure_retries_once_with_backoff():
    """§3.1：单请求 1 次重试 + 指数退避（超时不变）。"""
    waits, calls = [], {"n": 0}

    def http(url, params=None, headers=None, **_kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("连接超时")
        return {"word_list": [{"word": "电梯困人索赔", "hot_value": 900}]}

    ctx = I.FetchCtx(pack="elevator", keywords=["电梯"], http=http,
                     sleep=waits.append)
    rows = I.fetch_hot_board(next(s for s in _specs() if s.label == "抖音热榜"), ctx)
    assert calls["n"] == 2 and len(rows) == 1, "抖一下不该整个源今天没了"
    assert waits and waits[0] == I.HTTP_BACKOFF_BASE, "退避真的等了，而且等的是那一份"


def test_permanent_4xx_is_not_retried():
    """401/403 是"再问一遍也不会给"：重试只会把一次等待变成两次。"""
    calls = {"n": 0}

    class Forbidden(RuntimeError):
        status_code = 401

    def http(url, params=None, headers=None, **_kw):
        calls["n"] += 1
        raise Forbidden("401 unauthorized")

    ctx = I.FetchCtx(pack="elevator", keywords=["电梯"], http=http, sleep=lambda s: None)
    with pytest.raises(Forbidden):
        I.fetch_hot_board(next(s for s in _specs() if s.label == "抖音热榜"), ctx)
    assert calls["n"] == 1, "永久失败只试一次"
    assert I._is_permanent_http_error(Forbidden("x")) is True
    assert I._is_permanent_http_error(RuntimeError("timeout")) is False
    for code in (408, 425, 429):
        err = type("E", (RuntimeError,), {"status_code": code})("忙")
        assert I._is_permanent_http_error(err) is False, f"{code} 是可重试的"


def _write_history(tmp_path, snaps):
    d = I.history_dir(tmp_path, "elevator")
    d.mkdir(parents=True, exist_ok=True)
    for i, s in enumerate(snaps):
        (d / f"2026010{i}-000000.json").write_text(json.dumps(s), encoding="utf-8")


def test_source_health_counts_and_skips_after_three_fails_today(tmp_path):
    """连续 3 次失败且最近那次在今天 → 跳过并标「暂不可用」，明天自动再试。"""
    t = _now()
    fail_row = {"label": "抖音热榜", "count": 0, "error": "RuntimeError: 连不上",
                "state": "error"}
    # 时间戳 anchored 到今天凌晨（理由同 `test_today_marks_a_cooling_source_as_skipped`：
    # "距今 N 小时"在凌晨跑会集体落到昨天，skip 那道闸就永远不成立）。
    early = [t.replace(hour=0, minute=i, second=0, microsecond=0) for i in (10, 20, 30)]
    _write_history(tmp_path, [
        {"fetched_at": e.isoformat(timespec="seconds"),
         "items": [], "sources": [fail_row], "events": []} for e in early
    ])
    calls = {"n": 0}

    def http(url, params=None, headers=None, **_kw):
        calls["n"] += 1
        if "iesdouyin" in url:
            raise AssertionError("被跳过的源不该再发请求")
        return {"g": [{"q": "电梯困人怎么办"}]}

    out = I.fetch_pack("elevator", tmp_path, _specs(), seeds=["电梯困人"],
                       keywords=["电梯"], http=http)
    dou = next(r for r in out["sources"] if r["label"] == "抖音热榜")
    assert dou["state"] == "cooling" and dou["count"] == 0
    assert "跳过" in dou["error"] and "连续 3 次" in dou["error"]
    assert out["health"]["抖音热榜"]["consecutive_fails"] == 3
    assert out["health"]["抖音热榜"]["fail"] == 3
    assert out["health"]["抖音热榜"]["skip"] is True, \
        "下发给界面的健康度要能说出「今天没试」，否则源表那行与跳过说明互相打架"


def test_health_counts_are_appended_not_overwritten(tmp_path):
    """健康统计**只追加不覆盖**：昨天的失败不会因为今天成功一次就被抹平。

    单独一个用例、单独一个 tmp_path：上一条里那次 `fetch_pack` 自己也写了
    一份快照，混在一起数就分不清"统计算对了"还是"夹具多了一份文件"。
    """
    t = _now()
    fail_row = {"label": "抖音热榜", "count": 0, "error": "x", "state": "error"}
    ok_row = {"label": "抖音热榜", "count": 5, "error": "", "state": "ok"}
    _write_history(tmp_path, [
        {"fetched_at": (t - timedelta(days=1)).isoformat(timespec="seconds"),
         "items": [], "sources": [fail_row], "events": []},
        {"fetched_at": (t - timedelta(hours=4)).isoformat(timespec="seconds"),
         "items": [], "sources": [fail_row], "events": []},
        {"fetched_at": (t - timedelta(hours=3)).isoformat(timespec="seconds"),
         "items": [], "sources": [fail_row], "events": []},
        {"fetched_at": (t - timedelta(hours=1)).isoformat(timespec="seconds"),
         "items": [], "sources": [ok_row], "events": []},
    ])
    h = I.source_health(I.load_history(tmp_path, "elevator"), now=t)
    assert h["抖音热榜"]["ok"] == 1 and h["抖音热榜"]["fail"] == 3
    assert h["抖音热榜"]["hits"] == 5
    assert h["抖音热榜"]["consecutive_fails"] == 0, "中间成功过一次，连击就断了"
    assert h["抖音热榜"]["skip"] is False


def test_a_failed_yet_is_not_reported_as_skipped(tmp_path):
    """第一次失败只记失败，不该被夸大成"连续失败已跳过" —— 两句话的下一步动作不同。"""
    t = _now()
    _write_history(tmp_path, [
        {"fetched_at": (t - timedelta(hours=1)).isoformat(timespec="seconds"),
         "items": [], "sources": [{"label": "抖音热榜", "count": 0,
                                   "error": "x", "state": "error"}], "events": []}])
    out = I.fetch_pack("elevator", tmp_path, _specs(), seeds=["电梯困人"],
                       keywords=["电梯"], http=_fake_http(boom={"iesdouyin"}))
    dou = next(r for r in out["sources"] if r["label"] == "抖音热榜")
    assert dou["state"] == "error", "没到闸值就照常去试"


# ── 阶段二 #4：双视图一致性 + 空赛道降级 ────────────────────
def test_events_partition_the_same_pool_the_source_view_shows(tmp_path):
    """排行视图与按来源视图必须是**同一份池子**切出来的两种看法。"""
    I.fetch_pack("elevator", tmp_path, _specs(), seeds=["电梯困人"],
                 keywords=["电梯"], http=_fake_http())
    view = I.today(tmp_path, "elevator", _specs())
    pool = {it["key"] for it in view["items"]}
    in_events = {k for e in view["events"] for k in e["item_keys"]}
    assert in_events == pool, "事件把条目分完，不重不漏"
    assert sum(len(e["rows"]) for e in view["events"]) == len(pool)
    assert {r["key"] for e in view["events"] for r in e["rows"]} == pool


def test_empty_segment_degrades_to_a_note_not_a_fake_track(tmp_path):
    """排行视图里"没归上赛道"是一条说明，不是一个可筛的 chip（§3.5 筛选轴不混）。"""
    I.fetch_pack("elevator", tmp_path, _specs(), seeds=["电梯困人"],
                 keywords=["电梯"], topics_map={}, segment_options=["完全不搭的词"],
                 http=_fake_http())
    view = I.today(tmp_path, "elevator", _specs())
    assert all(not e["segment"] for e in view["events"])
    assert not any(e["featured"] for e in view["events"]), \
        "空桶不等于高热高配"


# ── 阶段二 #5：低粉爆文 v1 + 争议黄标 ───────────────────────
def test_underdog_never_shows_when_the_sample_is_thin():
    """§3.3.4：样本不足就不给标记，**而不是恒显示**。"""
    rows = [_item(f"电梯维保教程{i}", label="B站同类", guid=f"b{i}",
                  hot=100 * i, source_id="bilibili_search", segment="维保")
            for i in range(3)]
    events = I.build_events(rows, ranks={I.item_key(r): (i + 1, 3)
                                         for i, r in enumerate(rows)}, history=[],
                            now=_now())
    I._underdog_signal(events, {I.item_key(r): r for r in rows},
                       {str(e["key"]): "维保" for e in events})
    assert all(e["underdog"] is False for e in events), "3 条样本比不出「头部」"


def test_underdog_marks_a_low_play_item_that_still_ranks():
    pool = [_item(f"标题{i}", label="B站同类", guid=f"b{i}",
                   hot=[4000, 4200, 5000, 300][i], source_id="bilibili_search",
                   segment="维保") for i in range(4)]
    ranks = {I.item_key(r): (i + 1, 4) for i, r in enumerate(pool)}
    ranks[I.item_key(pool[3])] = (2, 4)          # 低播放却排在第二名
    events = I.build_events([pool[3]], ranks=ranks, history=[], now=_now())
    I._underdog_signal(events, {I.item_key(r): r for r in pool},
                       {str(events[0]["key"]): "维保"})
    assert events[0]["underdog"] is True, "轮得到我：小体量也做进了前排"


def test_contested_words_mark_the_card_but_are_not_the_pack_default(tmp_path):
    """§3.3.5：warn 级争议 → 黄标。用的是包自己声明的 `intel_contested`，
    **不是** banwords 的 soft 档（那批词是「最/第一/最新」，张张卡都会黄）。"""
    it = _item("电梯维权业主与物业冲突", label="微博热搜", source_id="weibo_hot")
    assert I.hits_contested(it, ("维权", "纠纷")) is True
    assert I.hits_contested(_item("电梯最新国标发布", label="微博热搜"),
                            ("维权",)) is False
    out = I.fetch_pack("elevator", tmp_path, _specs(), seeds=["电梯困人"],
                       keywords=["电梯"], contested=("困人",), http=_fake_http())
    tagged = [x for x in out["items"] if ["warn", "争议话题"] in (x.get("flags") or [])]
    assert tagged, "命中争议词的条目要挂黄标"
    assert all("困人" in x["title"] for x in tagged)
    # 事件层同样带标记（排行视图的行不读 item.flags）
    assert any(e["contested"] for e in out["events"])


def test_elevator_pack_declares_contested_words():
    """包必须**真的声明**了争议词表 —— 没声明就等于这条能力在实盘上是空的。"""
    from app.knowledge import Pack
    pk = Pack(ROOT, "elevator")
    words = pk.intel_contested()
    assert words and "维权" in words
    # 形状宽容：字符串当一个词，别拆成单字（`list("维权")` 那个老坑的同族）
    pk.data["intel_contested"] = "维权"
    assert pk.intel_contested() == ("维权",)
    pk.data["intel_contested"] = 12
    assert pk.intel_contested() == ()


# ── 落盘形状：事件与历史快照 ────────────────────────────────
def test_latest_and_snapshot_carry_events_and_sources(tmp_path):
    I.fetch_pack("elevator", tmp_path, _specs(), seeds=["电梯困人"],
                 keywords=["电梯"], http=_fake_http())
    latest = json.loads((I.intel_dir(tmp_path, "elevator") / "latest.json")
                        .read_text(encoding="utf-8"))
    assert latest["events"] and latest["health"] is not None
    snaps = I.load_history(tmp_path, "elevator")
    assert len(snaps) == 1
    assert snaps[0]["events"], "快照必须带事件：那是明天算在榜天数的对账基准"
    assert snaps[0]["sources"], "快照必须带本轮源结果：那是 source_health 的原料"


def test_events_are_written_once_and_read_without_recomputation(tmp_path):
    """§3.2「分项与总分随抓取落盘，读时只排序不重算」。"""
    out = I.fetch_pack("elevator", tmp_path, _specs(), seeds=["电梯困人"],
                       keywords=["电梯"], http=_fake_http())
    written = {e["key"]: e["hot_score"] for e in out["events"]}
    view = I.today(tmp_path, "elevator", _specs())
    assert {e["key"]: e["hot_score"] for e in view["events"]} == written, \
        "读时重算会让同一份产物在不同时刻给出不同名次"


def test_quadrant_classifies_all_four_cells():
    """§3.3 第 6 条：热点分 × 账号相关度 四档都在，只有"高热高配"进推荐位。"""
    rows = [_item("甲", label="微博热搜", source_id="weibo_hot", guid="q1"),
            _item("乙", label="微博热搜", source_id="weibo_hot", guid="q2"),
            _item("丙", label="微博热搜", source_id="weibo_hot", guid="q3")]
    # 造三档：高热+有归属 / 高热+无归属 / 低热+有归属
    hist = [_snap(9 - i, [dict(rows[0], hot=10), dict(rows[1], hot=10),
                          dict(rows[2], hot=10)]) for i in range(7)]
    evs = I.build_events(rows, ranks={I.item_key(r): (1, 3) for r in rows},
                         history=hist, now=_now())
    for ev, seg in zip(evs, ("维保", "", "维保")):
        ev["segment"] = seg
        ev["hot_score"] = {"q1": 80, "q2": 80, "q3": 20}[ev["item_keys"][0]]
    I._quadrant(evs)
    got = {e["item_keys"][0]: (e["quadrant"], e["featured"]) for e in evs}
    assert got["q1"] == ("高热高配", True)
    assert got["q2"] == ("高热低配", False)
    assert got["q3"] == ("低热高配", False)
    # 分数算不出（降级产物）不许进"高热"那一档
    weak = I._events_from_items([_item("丁", label="下拉词", guid="q4")])[0]
    I._quadrant([weak])
    assert weak["quadrant"] == "低热低配" and weak["featured"] is False


def test_ignored_list_orders_today_first_then_newest():
    """忽略清单排序：今天忽略过的排前面，其余按最后一次日期**新的在上**。

    一个 sort key 里塞不进"一升一降"，写反了又很安静（清单看着像按日期，其实是倒的），
    所以给三个不同日期的样本把**每个位置**钉死。
    """
    rec = {"k-old": {"days": ["2026-09-10"]},
           "k-mid": {"days": ["2026-09-20"]},
           "k-new": {"days": ["2026-09-19", "2026-09-21"]}}
    rows = I.ignored_rows(rec, [], today="2026-09-21")
    assert [r["key"] for r in rows] == ["k-new", "k-mid", "k-old"], rows
    assert [r["ignored_today"] for r in rows] == [True, False, False]
    # 旧形状（值是裸日期串）照样进清单 —— 没有迁移脚本，读必须两种都认
    old_shape = {"k-legacy": "2026-09-21"}
    got = I.ignored_rows(old_shape, [], today="2026-09-21")
    assert [r["key"] for r in got] == ["k-legacy"] and got[0]["ignored_today"]


# ── 审查线（第 29 轮）：读路径的四处口径 ─────────────────────
def test_ignoring_every_member_drops_the_event_from_the_rank_face(tmp_path):
    """这件事的每一条证据今天都被忽略 → 排行面不该还给它留一行。

    条目池那条规则是"今天点过忽略 = 摘掉，明天同题自动回来"，事件层过去没接：
    `rows` 会空掉，而事件行照发、名次照按热点分排 —— 实测忽略掉全部 3 条来源之后
    `events[0]` 仍然是这件事。前端点下去的那一刻把行删了，重进视图它又回到原位，
    用户得到的结论只能是"忽略没用"。
    """
    I.fetch_pack("elevator", tmp_path, _specs(), seeds=["电梯困人"],
                 keywords=["电梯"], http=_fake_http())
    v = I.today(tmp_path, "elevator", _specs())
    top = max(v["events"], key=lambda e: e["hot_score"] or 0)
    others = [e["key"] for e in v["events"] if e["key"] != top["key"]]
    for k in top["item_keys"]:
        I.add_ignored(tmp_path, "elevator", k)
    v2 = I.today(tmp_path, "elevator", _specs())
    keys2 = [e["key"] for e in v2["events"]]
    assert top["key"] not in keys2, "全忽略的事件仍留在排行面"
    assert keys2 == others, "别的事件一行都不该被牵连（摘的只是这一件）"
    # 只忽略其中一条来源：事件仍然在，`rows` 少那一条 —— 证据变薄不是消失
    for k in top["item_keys"]:
        I.remove_ignored(tmp_path, "elevator", k)
    I.add_ignored(tmp_path, "elevator", top["item_keys"][0])
    v3 = I.today(tmp_path, "elevator", _specs())
    back = next(e for e in v3["events"] if e["key"] == top["key"])
    assert [r["key"] for r in back["rows"]] == top["item_keys"][1:], \
        "剩下的证据要还在，被忽略的那一条不许混在里面"


def test_a_skipped_round_is_not_counted_as_another_failure(tmp_path):
    """被跳过的那一轮不是一次尝试：连击与失败数都不该为它 +1。

    `cooling` 行带着非空 `error` 落进 history，修前下一次统计把它数成第 4 次失败，
    界面于是说「连续 4 次抓取失败，今天先跳过」—— 而我们只试过 3 次，第 4 次是
    我们自己没试的。故障次数被跳过动作越滚越大，用户按那个数字判断"要不要去查 Key"
    就永远查不完。
    """
    t = _now()
    fail = {"label": "抖音热榜", "count": 0, "error": "RuntimeError: 401",
            "state": "error"}
    cool = {"label": "抖音热榜", "count": 0, "error": "连续 3 次抓取失败，今天先跳过",
            "state": "cooling"}
    early = [t.replace(hour=0, minute=i, second=0, microsecond=0) for i in (1, 2, 3, 4)]
    _write_history(tmp_path, [
        {"fetched_at": e.isoformat(timespec="seconds"), "items": [],
         "sources": [fail if i < 3 else cool], "events": []}
        for i, e in enumerate(early)])
    h = I.source_health(I.load_history(tmp_path, "elevator"), now=t)
    assert h["抖音热榜"]["fail"] == 3, "跳过不该被算成第 4 次失败"
    assert h["抖音热榜"]["consecutive_fails"] == 3, "连击也不为跳过 +1"
    assert h["抖音热榜"]["skip"] is True, \
        "但连击不能被跳过打断：它仍然是「今天别再试」（跳三次≠试了三次都没成→解禁）"


def test_yesterdays_cooling_does_not_claim_today_is_skipped(tmp_path):
    """昨天跳过三次的记录，今天不许继续说「今天先跳过」。

    `health` 是抓取那一刻算好落盘的，`today()` 读的时候不重算 —— 于是那句话会
    一路挂到下一次抓取为止，而今天其实一次都还没试过、也完全可以试
    （`source_health` 自己的注释就写着"跨到明天日期一变自动恢复"）。
    """
    def write(at: datetime) -> dict:
        d = I.intel_dir(tmp_path, "elevator")
        d.mkdir(parents=True, exist_ok=True)
        (d / "latest.json").write_text(json.dumps({
            "pack": "elevator", "fetched_at": at.isoformat(timespec="seconds"),
            "items": [], "sources": [], "errors": {}, "events": [],
            "health": {"微博热搜": {"ok": 0, "fail": 3, "hits": 0,
                                    "consecutive_fails": 3,
                                    "last_fail": at.isoformat(timespec="seconds"),
                                    "skip": True}}}), encoding="utf-8")
        specs = [s for s in _specs() if s.label == "微博热搜"]
        return I.today(tmp_path, "elevator", specs)["groups"][0]

    t = _now()
    y = (t - timedelta(days=1)).replace(hour=10, minute=0, second=0, microsecond=0)
    g = write(y)
    assert g["state"] != "cooling", "昨天的跳过今天仍在说「今天先跳过」"
    assert "今天先跳过" not in g["error"]
    # 正对照：同一份记录、失败就发生在今天凌晨 → 那句话就该出现
    g2 = write(t.replace(hour=0, minute=5, second=0, microsecond=0))
    assert g2["state"] == "cooling" and "今天先跳过" in g2["error"], \
        "退成「没跳过」如果是无条件生效的，上一条就只是没渲染"


def test_load_latest_survives_bad_numeric_shapes(tmp_path):
    """`load_latest` 的「绝不抛」要盖到**数值形状**，不只是元素类型。

    它 docstring 承诺"空 / 坏 / 没有一律返回空结构，绝不抛"，而 `today()` 里到处是
    `-x` 的排序键：`score: {"opportunity": "50"}`、`hot_score: "88"`、`errors: "坏了"`
    这类 JSON 合法但类型不对的文件（手工改过、或上一版写歪过）会让 `/api/intel/today`
    抛 TypeError/ValueError → 选题页整屏 500。认不出来就当**算不出**（None →「—」）。
    """
    d = I.intel_dir(tmp_path, "elevator")
    d.mkdir(parents=True, exist_ok=True)
    (d / "latest.json").write_text(json.dumps({
        "pack": "elevator",
        "fetched_at": _now().isoformat(timespec="seconds"),
        "sources": [], "health": {},
        "errors": "整份坏了（不是映射）",
        "items": [{"title": "甲", "guid": "g1", "source_label": "下拉词",
                   "score": {"D": "1", "S": True, "E": [], "opportunity": "50"}},
                  {"title": "乙", "guid": "g2", "source_label": "下拉词",
                   "score": {"opportunity": 60}}],
        "events": [{"key": "ev:0a1b2c3d4e5f", "title": "甲", "item_keys": ["g1"],
                    "hot_score": "88", "signals": [1, 2]},
                   {"key": "ev:1a2b3c4d5e6f", "title": "乙", "item_keys": "g2",
                    "hot_score": 70, "signals": {"rank": 0.5}}]}), encoding="utf-8")
    data = I.load_latest(tmp_path, "elevator")
    sc = data["items"][0]["score"]
    assert all(sc[f] is None for f in ("D", "S", "E", "opportunity")), sc
    assert sc is not None, "score 仍然在场，只是四个数各归各的「算不出」"
    assert data["events"][0]["hot_score"] is None
    assert data["events"][1]["item_keys"] == [], "裸字符串不是键列表"
    assert data["events"][0]["signals"] == {}, "四根分项读不出 = 信号不足，不是崩"
    assert isinstance(data["errors"], dict) and data["errors"]["_read"], \
        "形状坏要说得出原因，不能静默当没有"
    # 真函数跑到底：这是修前 500 的那条路径
    view = I.today(tmp_path, "elevator", _specs())
    assert view["events"], "坏形状不该把整个排行面清空"
    by_guid = {it["guid"]: it for it in view["items"]}
    assert by_guid["g2"]["score"]["opportunity"] == 60, "好的那个数不许被连带抹掉"
    assert by_guid["g1"]["score"]["opportunity"] is None, "认不出的那个数才是「算不出」"


def test_an_event_without_a_pill_is_not_called_peak_in_the_brief_input(tmp_path):
    """药丸为空（还在涨、没到爆发档、入榜不足 3 天）→ 进模型的阶段是**上升期**。

    `STAGE_BY_STATUS` 过去给空药丸兜了 `peak`。而 `_hot_status` 唯一能走到空值
    的形状就是"增速在 1..5 之间且入榜不足 3 天"，也就是**正在往上走还没爆** ——
    说成爆发期，模型照这句写出的钩子会催"今天就拍"，证据只支持"可以先备着"。
    """
    d = I.intel_dir(tmp_path, "elevator")
    d.mkdir(parents=True, exist_ok=True)
    (d / "latest.json").write_text(json.dumps({
        "pack": "elevator", "fetched_at": _now().isoformat(timespec="seconds"),
        "sources": [], "errors": {}, "health": {},
        "items": [{"title": "还在涨还没爆的题", "guid": "g1", "source_label": "微博热搜"}],
        "events": [{"key": "ev:0a1b2c3d4e5f", "title": "还在涨还没爆的题",
                    "item_keys": ["g1"], "hot_score": 40, "status": "",
                    "signals": {}}]}), encoding="utf-8")
    ev = I.today(tmp_path, "elevator", _specs())["events"][0]
    assert ev["status_label"] == "", "空药丸就是不给药丸，不许兜一个中文名出来"
    assert ev["stage"] == "rising", "空药丸的阶段必须是上升期，不是爆发期"


def test_titles_that_normalise_empty_do_not_share_an_event_key():
    """纯 emoji / 纯标点的标题归一化后是空串：不能共用 `sha1("")` 那一个键。

    聚类那一步已经把空串排除在比较之外（各成一组），但**事件键**照锚点算 ——
    锚点为空时所有这类事件落到同一个 `ev:da39a3ee5e6b` 上。那个键同时是
    `briefs/<key>.json` 的文件名：在 A 题上点「生成选题卡」，回到 B 题点一下
    会直接命中 A 那张卡（还"没花钱"）。
    """
    a = _item("😱😱😱", label="微博热搜", source_id="weibo_hot", guid="wb-1")
    b = _item("！！！", label="微博热搜", source_id="weibo_hot", guid="wb-2")
    evs = I.build_events([a, b], ranks={}, history=[], now=_now())
    assert len(evs) == 2, "两条各自成事件是对的（没文本可比）"
    assert evs[0]["key"] != evs[1]["key"], "归一化为空的事件键撞在一起"
    assert all(I.is_event_key(e["key"]) for e in evs)
    # 同一条目跨快照仍要给出同一个键（否则"这题昨天看过"与选题卡缓存都会失效）
    assert I.event_key_of([a]) == I.event_key_of([a])
