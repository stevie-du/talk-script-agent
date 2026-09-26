# -*- coding: utf-8 -*-
"""B 线：行业情报（源注册表 / 抓取 / 落点 / 只读端点）。

对到 `需求方案-去AI味与热点情报.md` §2.2、§2.9 的 B-1~B-4 与 README 的 B1~B4。
这个文件守的是**六条容易悄悄坏掉的性质**，每一条都对应一个具体的失效形态：

  1. **抓取器在生成主链路之外**：任何一个源挂了都不影响别的源，也不影响生成；
     而且本模块**一次模型调用都没有**（B-2：LLM 只留在"角度建议"）——
     所以"没配 Key"对今日选题完全无影响，首装用户看到的是一页真实条目。
  2. **落点在 `data_dir/intel/<pack>/`**，不在仓库根、不在 `packs/`（B2）。
  3. **只读端点空/坏返回空结构，不抛**（B3）—— 而且这条与
     `Pack.private_facts()` 的失败语义**相反**，两处都不能被"顺手统一"。
  4. **"算不出"与"0"是两种形状**（B-1）：D/S 缺数据时是 `None`，
     机会分是 `None` 而不是 0 —— 0 的意思是"竞争激烈、没机会"，结论相反。
  5. **两族并发额度**（B4）：抓取不占模型额度，所以"点一次重抓"不会让生成
     报「已达上限」；但抓取之间仍按自己的上限互斥。
  6. **一处声明、四处一致**（§2.2）：分组 / 计数 / 未接入 / 情报源表全部由
     `pack.yaml` 的 `intel_sources` 渲染，渲染层不写死任何平台名。
"""
from __future__ import annotations

import csv
import json
import shutil
import sys
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient                        # noqa: E402

from app import intel as I                                       # noqa: E402
from app.config import load_config                               # noqa: E402
from app.jobs import (BUSY_STATES, INTEL_BUSY_STATES,            # noqa: E402
                      MAX_CONCURRENT_INTEL,
                      TERMINAL_STATES)
from app.knowledge import Pack, PackBrokenError, pack_info         # noqa: E402
from app.pipeline import (MAX_CONCURRENT_JOBS, Pipeline,         # noqa: E402
                          wait_job)
from app.schemas import IntelPackRequest                         # noqa: E402
from app.server import create_app                                # noqa: E402

TOKEN = "test-token-intel"
LOOPBACK = "http://127.0.0.1:8765"


def _root() -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="talkscript-intel-"))
    shutil.copytree(ROOT / "packs", tmp / "packs")
    (tmp / "config.yaml").write_text("llm:\n  api_key: MOCK\n  model: mock\n",
                                    encoding="utf-8")
    return tmp


def _client(root: Path) -> TestClient:
    c = TestClient(create_app(root, token=TOKEN), base_url=LOOPBACK,
                   raise_server_exceptions=False)
    c.headers.update({"X-TalkScript-Token": TOKEN})
    return c


#: 一份最小的源声明（两种角色各一个 + 一个未接入的）。
def _specs(**over):
    raw = [
        {"id": "demand_terms", "label": "下拉词", "platform": "百度", "role": "雷达",
         "cadence": "daily"},
        {"id": "bilibili_search", "label": "B站同类", "platform": "B站",
         "role": "供给度量", "cadence": "on_demand"},
        {"id": "hot_board", "label": "抖音热榜", "platform": "抖音",
         "role": "破圈触发器", "cadence": "daily",
         "params": {"board": "douyin", "match": "keyword"}},
        {"id": "xhs_board", "label": "小红书", "platform": "小红书",
         "role": "破圈触发器", "cadence": "—", "enabled": False,
         "note": "没有公开榜，要 x-s 签名 —— 不做"},
    ]
    raw[0].update(over)
    return I.parse_sources(raw)[0]


def _fake_http(boom: set[str] | None = None, hot=("电梯困人索赔", "明星八卦"),
               gate=None):
    """离线假 HTTP：按 url 分派。`boom` 里的端点抛错（模拟离线/被墙）。

    `gate`：给了就在每次请求前**等这个 Event**（`threading.Event`）——
    用来把抓取作业**钉在 fetching 态**，好验"额度被占满"这类与时间有关的事。
    原来那条 `test_intel_fetch_has_its_own_limit` 靠"真网络慢"来保证作业还没跑完，
    换成假 HTTP 之后瞬间就 done 了、额度当场释放，测试反而红
    （P2-28 顺手暴露的一个竞态）。
    """
    boom = boom or set()

    def get(url, params=None, **_kw):
        if gate is not None:
            gate.wait(timeout=5)
        for key in boom:
            if key in url:
                raise RuntimeError(f"连不上 {key}")
        if "sugrec" in url:
            seed = (params or {}).get("wd")
            return {"g": [{"q": f"{seed}怎么选"}, {"q": f"{seed}多少钱"}]}
        if "bilibili" in url:
            kw = (params or {}).get("keyword")
            return {"data": {"result": [{"data": [
                {"title": f"<em>{kw}</em>教程{i}", "arcurl": f"https://b/{kw}/{i}",
                 "bvid": f"BV{kw}{i}", "play": 100 * i, "pubdate": 1758000000 + i}
                for i in range(3)]}]}}
        if "iesdouyin" in url:
            return {"word_list": [{"word": w, "hot_value": 900} for w in hot]}
        raise RuntimeError("没打桩的端点：" + url)

    return get


_OFFLINE_HTTP_CALLS: list[str] = []


@pytest.fixture(autouse=True)
def _offline_http(monkeypatch, request):
    """把真实 HTTP 出口换成**离线空响应**（P2-28）。

    `fetch_pack` / `start_intel_fetch` 不传 `http=` 时走 `_http_json`（真网络），
    而 demand_terms 每给一个 seed 就发一次请求 —— 离线 / 沙箱下会慢或抖动，
    后台线程还可能跨用例存活（症状是下一条用例莫名看到上一条的作业）。

    两层：
    1. **隔离**：替身返回 `{}`，任何用例都不真发网络。端点测试（如
       `/api/intel/refresh`）内部必然走到这条路径 —— 它验的是「端点起了作业」，
       不是抓取本身，所以放它过。
    2. **判死**：收尾断言「单元测试一次都不许走到真实出口」。
       ⚠ 替身**不能直接抛**：`fetch_pack` 对每个源各自 try（一个源挂了不影响
       别的源），抛出去会被**吞进 `errors`**、测试照样绿 —— 实测踩到过
       （第一版就是这么写的，变异检验没红）。
    """
    import app.intel as _I
    _OFFLINE_HTTP_CALLS.clear()

    def _offline(url, *a, **kw):
        _OFFLINE_HTTP_CALLS.append(str(url))
        return {}

    monkeypatch.setattr(_I, "_http_json", _offline)
    yield
    # 端点测试内部必然调 start_intel_fetch（那里没有注入点），放过它 ——
    # 抓取本身由上面那些单元用例覆盖。
    if "endpoint" in request.node.name:
        return
    assert not _OFFLINE_HTTP_CALLS, (
        "这条用例没给 fetch_pack / start_intel_fetch 传 http=_fake_http()，"
        "走到了真实 HTTP 出口（已被离线化，但拿到的不是夹具里那份数据）："
        + "、".join(_OFFLINE_HTTP_CALLS[:3]))


# ── 1. 源声明解析：坏法各归各的，不抛 ────────────────────────
def test_parse_sources_reports_bad_shapes_without_raising():
    srcs, notes = I.parse_sources([
        "不是映射",
        {"label": "没有 id"},
        {"id": "demand_terms", "label": "A", "enabled": "yes", "params": [1]},
        {"id": "demand_terms", "label": "A"},          # label 重复
    ])
    joined = " ".join(notes)
    assert "需要映射" in joined and "没有 id" in joined
    assert "enabled 需要 true/false" in joined, "非布尔的 enabled 必须报出来（按 true 处理）"
    assert "params 需要映射" in joined
    assert "label='A' 重复" in joined
    assert len(srcs) == 2, "坏的两条丢掉，好的两条留下"
    assert srcs[0].enabled is True, "非布尔 enabled 按 true 处理（宁可抓一次也不要静默不抓）"


def test_unknown_adapter_is_a_note_only_when_enabled():
    """`enabled: true` + 引擎没有适配器 = **问题**（作者以为它在跑）。

    `enabled: false` + 没有适配器 = **决定**（§2.2 的 `xhs_board` 就是"不做逆向"）。
    后者也记 note 的话，审计会变成背景噪音（`test_pack_info_is_clean_when_
    everything_is_fine` 那条规矩）。
    """
    _, on = I.parse_sources([{"id": "nope", "label": "没接", "enabled": True}])
    assert any("没有对应适配器" in n for n in on)
    _, off = I.parse_sources([{"id": "nope", "label": "不做", "enabled": False}])
    assert not off, f"主动关掉的源不该被记成问题：{off}"


def test_parse_sources_empty_is_not_an_error():
    for raw in (None, "", [], {}):
        srcs, notes = I.parse_sources(raw)
        assert srcs == []
    # 非列表才是结构错（写成了映射）
    assert I.parse_sources({"id": "demand_terms"})[1]


# ── 2. 去重：guid > url 优先级 ───────────────────────────────
def test_dedup_prefers_guid_over_url():
    """同一个链接带不同跟踪参数时，guid 才是稳定标识（TrendRadar 的做法）。

    反过来（只按 url）会让同一条内容在两次抓取里各留一份，
    界面上的"今日 N 条"虚高，而 `is_new` 也永远为真。
    """
    rows = [
        {"guid": "g1", "url": "https://a?utm=1", "title": "甲"},
        {"guid": "g1", "url": "https://a?utm=2", "title": "甲"},
        {"url": "https://b", "title": "乙"},
        {"url": "https://b", "title": "乙（换个标题但同 url）"},
        {"title": "丙"},                      # 没有 guid 也没有 url → 按标题兜底
        {"title": "丙"},
    ]
    out = I.dedup(rows)
    assert [r["title"] for r in out] == ["甲", "乙", "丙"]


# ── 3. 抓取：一个源挂了不牵连别的源 ──────────────────────────
def test_one_broken_source_does_not_break_the_rest(tmp_path):
    srcs = _specs()
    out = I.fetch_pack("elevator", tmp_path, srcs, seeds=["电梯困人"],
                       keywords=["电梯"], http=_fake_http(boom={"iesdouyin"}))
    by = {s["label"]: s for s in out["sources"]}
    assert by["下拉词"]["count"] > 0, "别的源照跑"
    assert by["B站同类"]["count"] > 0
    assert by["抖音热榜"]["state"] == "error" and by["抖音热榜"]["count"] == 0
    # ⚠ 判据不能是 `or out["errors"]`：上一行刚断言过失败源存在，errors 必然非空 ——
    #   那等于**恒真**。要钉的是「这条失败**带上了原因**」，且原因落在**这个源**上
    #   （`errors` 的 key 是源 id，见 app/intel.py 的 `errors[spec.id]`）。
    assert "hot_board" in out["errors"], \
        f"失败的源没在 errors 里留下原因：{out['errors']}"
    assert out["errors"]["hot_board"], "原因是空串 —— 等于没有原因"
    assert by["小红书"]["state"] == "off", "主动关掉的源不是 error"


def test_no_llm_call_anywhere_in_fetch(monkeypatch):
    tmp_path = _root()
    """B-2：本模块**一次模型调用都没有** —— 所以"没配 Key"对选题零影响。

    这条用"把 LLMClient 的构造打成必炸"来证明：只要抓取路径上碰过模型，
    这里立刻红。首装用户（没配 Key）打开选题页必须能看到真实条目，
    而不是空白或"请先配置模型"。
    """
    import app.pipeline as P
    monkeypatch.setattr(P.Pipeline, "llm", property(
        lambda self: (_ for _ in ()).throw(AssertionError("抓取路径上不该碰模型"))))
    pl = Pipeline(tmp_path, load_config(tmp_path))
    # http 注入点（生产不传）：这条守的是「没配 Key 不碰模型」，本来不必真联网。
    # 实测（2026-09-23）：demand_terms 对每个 seed 各发一次真实请求（20 个），
    # 沙箱代理下稳定超过 60s 预算 —— 全量跑必红、单跑靠网络时序侥幸过。
    # 注入 _fake_http 后仍走完整 Job 管道，只是不发真实请求，语义不变。
    jid = pl.start_intel_fetch("elevator", http=_fake_http())
    snap = wait_job(pl, jid, timeout=60)
    assert snap["state"] in TERMINAL_STATES
    # 真网络可能连不上（沙箱/离线），所以**不**断言 done —— 断言的是"没碰模型"：
    # 只要上面那条 property 没被触发，这条测试就成立。
    assert snap["state"] in ("done", "failed", "cancelled")


def test_per_source_cap_limits_flooding(tmp_path):
    """每话题上限（TrendRadar）：一个源返回 200 条不该把界面刷满。"""
    rows = [{"guid": f"g{i}", "title": f"标题{i}"} for i in range(I.PER_SOURCE_CAP + 50)]
    specs = I.parse_sources([{"id": "manual_import", "label": "人工", "cadence": "on_demand"}])[0]
    man = I.intel_dir(tmp_path, "p") / "manual"
    man.mkdir(parents=True)
    (man / "a.json").write_text(json.dumps(rows), encoding="utf-8")
    out = I.fetch_pack("p", tmp_path, specs)
    assert len(out["items"]) == I.PER_SOURCE_CAP


# ── 4. 落点：data_dir/intel/<pack>/ ──────────────────────────
def test_intel_lands_in_data_dir_not_in_packs(tmp_path):
    """B2：落点在 `data_dir/intel/<pack>/`。

    放在仓库根（开发态能跑，因为 `data_dir` 默认等于 root）在打包后会**直接坏掉** ——
    安装目录不可写。所以判据盯的是"写到了 data_dir 下面"，而不是"写成功了"。
    """
    I.fetch_pack("elevator", tmp_path, _specs(), seeds=["x"], keywords=["电梯"],
                 http=_fake_http())
    assert (tmp_path / "intel" / "elevator" / "latest.json").exists()
    assert list((tmp_path / "intel" / "elevator" / "history").glob("*.json"))
    assert not (tmp_path / "packs" / "elevator" / "latest.json").exists(), \
        "情报不该落进 packs/ —— 那是可分发单元"


# ── 5. 只读读取：空 / 坏都不抛 ───────────────────────────────
def test_load_latest_never_raises(tmp_path):
    """B3：没有 / 坏 / 顶层不是映射 —— 三种都返回空结构。

    ⚠ 与 `Pack.private_facts()` 的失败语义**相反**（那边读不到要中止生成，
    否则模型会编事实）。这条差异是故意的，**别顺手统一**。
    """
    assert I.load_latest(tmp_path, "nope")["items"] == []
    d = I.intel_dir(tmp_path, "elevator")
    d.mkdir(parents=True)
    (d / "latest.json").write_text("{ 这不是 json", encoding="utf-8")
    got = I.load_latest(tmp_path, "elevator")
    assert got["items"] == [] and got["errors"].get("_read"), "坏文件要留下原因"
    (d / "latest.json").write_text("[1,2,3]", encoding="utf-8")
    assert I.load_latest(tmp_path, "elevator")["items"] == []


def test_today_reports_orphan_sources_instead_of_hiding_items(tmp_path):
    """盘上有条目、但当前声明里没有那个源（包改过配置）→ 也要露出来。

    否则界面上的"分组条数之和"与"总条数"对不上，而差额没有任何解释。
    """
    I.fetch_pack("elevator", tmp_path, _specs(), seeds=["x"], keywords=["电梯"],
                 http=_fake_http())
    only_demand = I.parse_sources([{"id": "demand_terms", "label": "下拉词"}])[0]
    got = I.today(tmp_path, "elevator", only_demand)
    labels = [g["label"] for g in got["groups"]]
    assert "B站同类" in labels and "抖音热榜" in labels, "孤儿源要露出来"
    assert sum(g["count"] for g in got["groups"]) == len(got["items"]), \
        "分组计数之和必须等于条目总数（否则用户对不上账）"


def test_today_marks_unwired_and_off_sources(tmp_path):
    got = I.today(tmp_path, "elevator", _specs())
    by = {g["label"]: g for g in got["groups"]}
    assert by["小红书"]["state"] == "off" and by["小红书"]["count"] == 0
    assert got["unwired"] >= 1, "「未接入 N」要能算出来（§2.2 四处一致之一）"


# ── 6. 打分：「算不出」与「0」是两种形状 ──────────────────────
def test_scores_are_none_not_zero_when_uncomputable(tmp_path):
    """B-1：D/S 缺数据时是 `None`，机会分也是 `None`。

    只配了需求词、没有 B站源 → S 算不出 → 机会分**必须是 None**。
    写成 0 的意思是"竞争激烈、没机会"，与"没数据"结论相反 ——
    界面上一列全是 0.00 的卡会被读成"今天没机会"，实际是"我们没测"。
    """
    specs = I.parse_sources([{"id": "demand_terms", "label": "下拉词",
                              "role": "雷达"}])[0]
    out = I.fetch_pack("elevator", tmp_path, specs, seeds=["电梯困人"],
                       keywords=["电梯"], topics_map={"维保": "困人"},
                       segment_options=["维保"], http=_fake_http())
    assert out["items"], "至少要有条目"
    for it in out["items"]:
        sc = it["score"]
        assert sc["S"] is None and sc["opportunity"] is None
        assert sc["D"] is not None, "需求词本次全是新收录 → D 算得出"
    assert any(f[1].startswith("数据不足") for f in out["items"][0]["flags"]), \
        "算不出机会分要在界面上有一句说法，不能只留一个空"


def test_second_fetch_marks_nothing_new_and_drops_d():
    """第二次抓取没有新词 → D 算不出（`None`），不是 0。

    这是"本次无新增"与"本次有新增但这条不是"的区别，两者结论不同：
    前者说明雷达没动，后者说明这条是老话题。
    """
    items = [{"guid": "g1", "title": "甲", "source_id": "demand_terms", "segment": "维保"},
             {"guid": "g2", "title": "乙", "source_id": "demand_terms", "segment": "维保"}]
    got = I.score_topics([dict(x) for x in items], prev_keys={"g1", "g2"})
    assert got["维保"]["D"] is None
    assert got["维保"]["opportunity"] is None


def test_event_decay_uses_role_tau():
    """E = exp(−Δ天/τ)，τ 按**来源角色**取：热榜 3 天、口径库 30 天。

    同一条 10 天前的条目，挂在不同角色下 E 必须不同 —— 否则"事件衰减"
    就只是个装饰，热榜上的旧闻会和政策文件一样"新鲜"。
    """
    now = datetime.now(timezone.utc).astimezone()
    pub = (now - timedelta(days=10)).isoformat(timespec="seconds")
    items = [
        {"guid": "hot", "title": "热点", "source_id": "hot_board",
         "segment": "维保", "role": "破圈触发器", "published": pub},
        {"guid": "pol", "title": "政策", "source_id": "policy_library",
         "segment": "维保", "role": "口径库", "published": pub},
    ]
    I.score_topics(items, set())
    assert items[0]["score"]["E"] < 0.05, "热榜 10 天前几乎归零（τ=3）"
    assert items[1]["score"]["E"] > 0.5, "政策 10 天前还很新鲜（τ=30）"


def test_unparseable_published_gives_none_not_zero():
    items = [{"guid": "g", "title": "甲", "source_id": "hot_board",
              "segment": "维保", "role": "破圈触发器", "published": "不知道什么时候"}]
    I.score_topics(items, set())
    assert items[0]["score"]["E"] is None, "解析不出的日期是「算不出」，不是「就是今天」"


# ── 7. 懒触发判据 ────────────────────────────────────────────
def test_is_stale_uses_the_tightest_cadence():
    """B4：懒触发按"上次抓取距今"，取所有可用源里**最紧**的那一档。

    ⚠ 只有 `on_demand` / 没写节奏的源 → **永不自动补抓**（它们只在用户点重抓时跑）。
    这一条很容易被写成"有源就每天抓"，那样 B站按需源会天天被拉一遍。
    """
    now = datetime.now(timezone.utc).astimezone()
    fresh = {"fetched_at": now.isoformat(timespec="seconds")}
    old = {"fetched_at": (now - timedelta(days=3)).isoformat(timespec="seconds")}
    daily = _specs()
    assert I.is_stale({}, daily) is True, "从没抓过 → 该抓"
    assert I.is_stale(fresh, daily) is False
    assert I.is_stale(old, daily) is True
    ondemand = I.parse_sources([{"id": "bilibili_search", "label": "B站",
                                "cadence": "on_demand"}])[0]
    assert I.is_stale(old, ondemand) is False, "按需源不参与懒触发"
    assert I.is_stale({}, ondemand) is False, "按需源连首次也不自动抓"


# ── 8. 忽略：只影响今天 ──────────────────────────────────────
def test_ignore_only_affects_today(tmp_path):
    """§2.5：「忽略」只影响今天，明天同题还会回来；连续 3 天被忽略才沉底。

    所以落盘的是 `{key: 最后一次忽略的日期}` —— 界面要按"连续几天"判沉底，
    而"连续"只能靠日期算，记一个布尔是算不出来的。
    """
    I.fetch_pack("elevator", tmp_path, _specs(), seeds=["电梯困人"], keywords=["电梯"],
                 http=_fake_http())
    got = I.today(tmp_path, "elevator", _specs())
    key = got["items"][0]["guid"] or got["items"][0]["title"]
    n0 = len(got["items"])
    I.add_ignored(tmp_path, "elevator", key)
    assert len(I.today(tmp_path, "elevator", _specs())["items"]) == n0 - 1
    # 把记录改成"昨天" → 今天又出现（只影响今天）
    rec = I.load_ignored(tmp_path, "elevator")
    rec[key] = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    (I.intel_dir(tmp_path, "elevator") / "ignored.json").write_text(
        json.dumps(rec, ensure_ascii=False), encoding="utf-8")
    assert len(I.today(tmp_path, "elevator", _specs())["items"]) == n0, \
        "昨天的忽略不该影响今天"
    # 连续三天的日期都留在盘上（界面据此判沉底）
    I.add_ignored(tmp_path, "elevator", key, today="2026-09-21")
    I.add_ignored(tmp_path, "elevator", key, today="2026-09-22")
    I.add_ignored(tmp_path, "elevator", key, today="2026-09-23")
    assert I.load_ignored(tmp_path, "elevator")[key] == "2026-09-23"


def test_today_ignore_uses_the_same_key_it_ships(tmp_path):
    """`today()` 的忽略过滤、忽略写入、下发的 `item.key` 必须是**同一个键**。

    修复前：过滤用 `guid or title`、而渲染层写记录用 `guid || url || title` ——
    于是**只有 url、没有 guid** 的条目（政策库那类，见 `item_key` 的注释）
    点「忽略」之后**下一轮原样回来**，用户看到的是"忽略没用"。
    这条用一个只有 url 的条目把它钉住。
    """
    d = I.intel_dir(tmp_path, "elevator")
    d.mkdir(parents=True, exist_ok=True)
    only_url = {"title": "政策库的一条", "url": "https://gov.example/p/1",
                "source_id": "policy_library", "source_label": "政策库"}
    (d / "latest.json").write_text(json.dumps(
        {"fetched_at": "2026-09-24T10:00:00+08:00", "items": [only_url], "errors": {}},
        ensure_ascii=False), encoding="utf-8")

    specs = I.parse_sources([{"id": "policy_library", "label": "政策库"}])[0]
    got = I.today(tmp_path, "elevator", specs)
    assert len(got["items"]) == 1, got
    k = got["items"][0]["key"]
    assert k == "https://gov.example/p/1", f"只有 url 时键该退到 url，实际：{k}"

    # 用**下发的那个键**写忽略（渲染层就是这么做的）→ 今天它必须消失
    I.add_ignored(tmp_path, "elevator", k)
    assert I.today(tmp_path, "elevator", specs)["items"] == [], \
        "忽略了却还在 —— 过滤用的键与写入用的键不是同一个"


def test_broken_ignored_file_does_not_get_overwritten(tmp_path):
    """`ignored.json` 读坏时 `add_ignored` 必须**中止**，不许拿 `{}` 当基底覆盖。

    旧实现 `load_ignored` 对坏文件返回 `{}`，而 `add_ignored` 是"读出来 → 加一条 →
    写回去" —— 一次读坏就把**整个忽略历史**覆盖成一条。用户看到的是
    "我忽略过的又都回来了"，而手上没有任何线索。

    ⚠ 只读路径（`today()` 的过滤）**不**受影响：情报不该因为一个坏文件整个看不了
    （B3，与本文件开头第 3 条同一条取向）—— 两处语义相反是**刻意的**，别"顺手统一"。
    """
    d = I.intel_dir(tmp_path, "elevator")
    d.mkdir(parents=True, exist_ok=True)
    broken = "{ 这不是 json"
    (d / "ignored.json").write_text(broken, encoding="utf-8")

    with pytest.raises(I.IntelError) as ei:
        I.add_ignored(tmp_path, "elevator", "k-new")
    assert "没有生效" in str(ei.value), str(ei.value)
    # 最要紧的一条：坏文件**原样还在**（没被覆盖成 {"k-new": …}）
    assert (d / "ignored.json").read_text(encoding="utf-8") == broken, \
        "坏文件被覆盖了 —— 那等于把用户已有的忽略记录全抹掉"

    # 只读路径照常：读坏退成**空过滤**（不抛，也不把条目全滤掉）
    (d / "latest.json").write_text(json.dumps(
        {"fetched_at": "2026-09-24T10:00:00+08:00",
         "items": [{"title": "一条", "url": "https://a/1",
                    "source_id": "policy_library", "source_label": "政策库"}],
         "errors": {}}, ensure_ascii=False), encoding="utf-8")
    specs = I.parse_sources([{"id": "policy_library", "label": "政策库"}])[0]
    assert len(I.today(tmp_path, "elevator", specs)["items"]) == 1, \
        "读坏不该把条目全滤掉（那等于把整个列表清空）"
    assert I.load_ignored(tmp_path, "elevator") == {}


def test_ignored_accumulates_and_structure_error_is_reported(tmp_path):
    """正常路径照常累加；结构不对（不是对象）也要报出来、不许静默当空。"""
    I.add_ignored(tmp_path, "elevator", "a")
    I.add_ignored(tmp_path, "elevator", "b")
    assert set(I.load_ignored(tmp_path, "elevator")) == {"a", "b"}

    d = I.intel_dir(tmp_path, "elevator")
    (d / "ignored.json").write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(I.IntelError) as ei:
        I.add_ignored(tmp_path, "elevator", "c")
    assert "结构不对" in str(ei.value), str(ei.value)


# ── 9. 人工导入（B-4）────────────────────────────────────────
def test_manual_import_reads_json_and_csv_and_skips_broken(tmp_path):
    """B-4：拿不到的平台用人工导入补上，与自动源**同一 schema**。

    坏文件只跳过它自己（不牵连别的文件、不牵连别的源）—— 用户在
    `manual/` 里放了一个手抖写坏的 json，不该让整个选题页变空。
    """
    man = I.intel_dir(tmp_path, "elevator") / "manual"
    man.mkdir(parents=True)
    (man / "a.json").write_text(json.dumps([
        {"title": "贴进来的小红书笔记", "url": "https://xhs/1", "desc": "3 万赞"}],
        ensure_ascii=False), encoding="utf-8")
    with (man / "b.csv").open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["title", "url", "desc", "published", "hot"])
        w.writeheader()
        w.writerow({"title": "协会资讯一条", "url": "https://assoc/2",
                    "desc": "", "published": "2026-09-01", "hot": "12"})
    (man / "broken.json").write_text("{坏", encoding="utf-8")
    specs = I.parse_sources([{"id": "manual_import", "label": "人工导入",
                              "role": "数据源"}])[0]
    out = I.fetch_pack("elevator", tmp_path, specs)
    titles = {it["title"] for it in out["items"]}
    assert {"贴进来的小红书笔记", "协会资讯一条"} <= titles
    assert out["sources"][0]["state"] == "ok", "一个坏文件不该让这个源报失败"


# ── 10. 配置链路：pack.yaml → Pack → pack_info ───────────────
def test_pack_declares_sources_and_derives_seeds():
    pk = Pack(ROOT, "elevator")
    srcs = pk.intel_sources()
    assert [s.label for s in srcs][:2] == ["下拉词", "B站同类"]
    assert len([s for s in srcs if s.id == "hot_board"]) == 3, \
        "hot_board 一个适配器按 params.board 出多个源（§2.2）"
    assert pk.intel_seeds() == [str(x) for x in pk.param_options("segment")]
    kws = pk.intel_keywords()
    assert "电梯" in kws and "维保" in kws, "命中词要含 display_name 与 topics_map 的映射词"


def test_pack_info_exposes_sources_without_pack_error(tmp_path):
    info = pack_info(ROOT / "packs" / "elevator")
    assert info.pack_error == ""
    assert len(info.intel_sources) == 8
    assert info.intel_sources[-1].label == "小红书"
    assert info.intel_sources[-1].enabled is False
    # 主动关掉的源**不该**产生审计 note（那是决定不是 bug）
    assert not info.param_audit.get("intel_sources"), \
        f"关掉的源不该被记成问题：{info.param_audit.get('intel_sources')}"


def test_pack_info_reports_a_broken_skill_yaml(tmp_path):
    """`skill.yaml` 读坏时**列表也必须标出来**（P2-19）。

    修复前 `pack_info` 从不读 `skill.yaml`，而 `Pack.skill()` 读坏会抛
    `PackBrokenError`（生成期 409）—— **列表说这个包是好的、点生成说它坏了**，
    两处结论相反；用户只会去查模型 / 网络，真正的原因（包里一个文件坏了）
    在界面上没有任何落点。

    判据：凡 `Pack()` 会因此抛的东西，`pack_info` 都要能看见 ——
    "列表宽松"指**不抛**，不是**看不见**。
    """
    d = tmp_path / "packs" / "elevator"
    shutil.copytree(ROOT / "packs" / "elevator", d)
    (d / "skill.yaml").write_text("{ 这不是 yaml", encoding="utf-8")

    info = pack_info(d)
    assert info.pack_error, "坏 skill.yaml 没被标出来 —— 列表把坏包显示成健康"
    assert "skill" in info.pack_error.lower(), info.pack_error

    # 生成期那边本来就是抛的（两处口径现在一致了）
    with pytest.raises(PackBrokenError) as ei:
        Pack(tmp_path, "elevator").skill()
    assert "skill" in str(ei.value).lower(), str(ei.value)


def test_meta_payload_carries_intel_sources(tmp_path):
    """`/api/meta` 已经 `model_dump()` 全量下发，前端不用新接口就能拿到。"""
    c = _client(_root())
    packs = c.get("/api/meta").json()["packs"]
    elev = next(p for p in packs if p["name"] == "elevator")
    assert len(elev["intel_sources"]) == 8
    assert {"id", "label", "platform", "role", "cadence", "enabled", "wired"} <= set(
        elev["intel_sources"][0])


def test_enabled_but_unwired_source_is_audited(tmp_path):
    """作者以为它在跑、实际引擎没有适配器 —— 必须可见（否则"永远 0 条"）。"""
    root = _root()
    p = root / "packs" / "elevator" / "pack.yaml"
    data = yaml.safe_load(p.read_text(encoding="utf-8"))
    data["intel_sources"].append({"id": "not_an_adapter", "label": "幽灵源",
                                  "cadence": "daily"})
    p.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
                 encoding="utf-8")
    info = pack_info(root / "packs" / "elevator")
    assert "not_an_adapter" in str(info.param_audit.get("intel_sources"))


# ── 11. 端点 ─────────────────────────────────────────────────
def test_today_endpoint_is_empty_not_error():
    c = _client(_root())
    r = c.get("/api/intel/today", params={"pack": "elevator"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["items"] == [] and body["fetched_at"] == ""
    assert [g["label"] for g in body["groups"]][:2] == ["下拉词", "B站同类"], \
        "没抓过也要按声明给出分区骨架（否则界面无从显示「今天还没抓」）"
    assert body["stale"] is True


def test_today_endpoint_survives_a_broken_pack(tmp_path):
    """坏包不该让选题页白屏 —— 情报与 pack.yaml 合法性是两件事。

    坏包走 `pack_info`（不抛）而不是 `Pack()`（抛 409），所以这里要 200。
    """
    root = _root()
    (root / "packs" / "elevator" / "pack.yaml").write_text("version: v2\n",
                                                           encoding="utf-8")
    c = _client(root)
    assert c.get("/api/intel/today", params={"pack": "elevator"}).status_code == 200


def test_refresh_endpoint_starts_a_job():
    c = _client(_root())
    r = c.post("/api/intel/refresh", json={"pack": "elevator"})
    assert r.status_code == 200, r.text
    jid = r.json()["job_id"]
    snap = c.get(f"/api/jobs/{jid}").json()
    assert snap["kind"] == "intel"


def test_refresh_unknown_pack_is_404():
    c = _client(_root())
    assert c.post("/api/intel/refresh", json={"pack": "nope"}).status_code == 404


def test_ignore_endpoint_records_today():
    tmp_path = _root()
    c = _client(tmp_path)
    r = c.post("/api/intel/ignore", json={"pack": "elevator", "key": "g1"})
    assert r.status_code == 200 and r.json()["ignored"] == 1
    assert I.load_ignored(tmp_path, "elevator")["g1"] == \
        datetime.now().strftime("%Y-%m-%d")


def test_ignore_endpoint_says_so_when_the_record_is_broken():
    """忽略记录读坏时端点必须**报错**，而不是静默把它覆盖掉（P2-8）。

    静默覆盖的后果：用户点了一次「忽略」，整个忽略历史变成只有这一条 ——
    「我忽略过的又都回来了」，而界面上没有任何线索（旧实现 `except: return {}`
    让这条路径一路成功、还弹「已忽略」）。
    """
    tmp_path = _root()
    d = I.intel_dir(tmp_path, "elevator")
    d.mkdir(parents=True, exist_ok=True)
    broken = "{ 这不是 json"
    (d / "ignored.json").write_text(broken, encoding="utf-8")
    c = _client(tmp_path)
    r = c.post("/api/intel/ignore", json={"pack": "elevator", "key": "g1"})
    assert r.status_code == 500, (r.status_code, r.text[:200])
    body = r.json()
    assert body.get("code") == "intel_ignore_failed", body
    assert "没有生效" in body.get("detail", ""), body
    # 最要紧的一条：坏文件**原样还在**（没被覆盖成 {"g1": …}）
    assert (d / "ignored.json").read_text(encoding="utf-8") == broken, \
        "端点把坏文件覆盖了 —— 用户的忽略历史被抹掉了"


# ── 12. 两族并发额度（B4）────────────────────────────────────
def test_intel_fetch_does_not_consume_model_quota():
    """抓取**不占模型额度**：否则「点一次重抓」会让生成报「已达上限」。

    这条正是 B4 拆两族的理由。判据不能只是"抓取作业的 running_count 是 0" ——
    那只说明 `fetching` 不在 `BUSY_STATES` 里，**说明不了两族互不占名额**。
    真正要钉的性质是：**情报额度被占满时，生成照样能开起来**。
    """
    from app.schemas import GenerateRequest
    tmp_path = _root()
    cfg = load_config(tmp_path)
    cfg.mock = True
    pl = Pipeline(tmp_path, cfg)
    for _ in range(MAX_CONCURRENT_INTEL):
        pl.start_intel_fetch("elevator", http=_fake_http())
    assert pl.registry.running_count(INTEL_BUSY_STATES) == MAX_CONCURRENT_INTEL
    assert pl.registry.running_count() == 0, "情报抓取占了模型额度"
    # 关键断言：抓取占满自己那档额度时，生成**必须仍然开得起来**。
    assert pl.start_generate(GenerateRequest(pack="elevator", topic="家用电梯怎么选",
                                             duration=60, platform="抖音")), \
        "情报额度被占满不该影响生成"


def test_intel_jobs_do_not_take_the_model_quota_of_a_rewrite():
    """情报抓取占满时，**重写**照样能开起来（P2-10）。

    `transition_if_room` 原来只数 `BUSY_STATES`，而 intel 作业也是以 `queued`
    插入的（见 `INTEL_BUSY_STATES` 的注释）—— 抓取于是占掉了模型族的名额。
    `add_if_room` 一直有 `same_quota_family`，这条闸漏了：同一件事两份口径，
    漏的那份只在"抓取与重写同时发生"时现形（用户点两次重抓 → 重写被误判
    「已达上限」，而占着名额的是一个不花模型钱的 HTTP 请求）。
    """
    from app.jobs import (BUSY_STATES, INTEL_BUSY_STATES, Job, JobRegistry,
                          MAX_CONCURRENT_INTEL)
    reg = JobRegistry()
    # 情报额度占满（queued 就占额度 —— 见 INTEL_BUSY_STATES 的注释）
    for i in range(MAX_CONCURRENT_INTEL):
        assert reg.add_if_room(Job(f"i{i}", "intel", {}), MAX_CONCURRENT_INTEL,
                               INTEL_BUSY_STATES), f"第 {i} 条抓取没开起来"
    # 重写载体：一个在跑的生成作业（queued → writing 在 TRANSITIONS 里是允许的）
    gen = Job("g1", "generate", {})
    assert gen.transition("writing"), "queued → writing 应当允许"
    assert reg.add_if_room(gen, 4, BUSY_STATES)
    assert reg.transition_if_room(gen, "rewriting", 2), \
        "情报抓取占掉了模型族的名额 —— 重写会被误判「已达上限」"

    # 反向对照：模型族的额度**确实**还在管 —— 否则上面那条可能只是"这道闸永远放行"
    reg.add(Job("g2", "generate", {}))
    reg.add(Job("g3", "generate", {}))
    assert not reg.transition_if_room(gen, "rewriting", 2), \
        "模型族占满了却还放行 —— 这道闸等于没有"


def test_intel_fetch_has_its_own_limit():
    """情报抓取之间按 `MAX_CONCURRENT_INTEL` 互斥。

    ⚠ 用 `gate` 把两条作业**钉在 fetching**：不钉住的话假 HTTP 瞬间跑完、
    作业变 done、额度当场释放，第三次调用就不会被拒 —— 这条测试会变成
    "靠真网络慢"才能过的竞态（P2-28 换成假 HTTP 时实测踩到）。
    """
    from app.jobs import StateConflict
    tmp_path = _root()
    pl = Pipeline(tmp_path, load_config(tmp_path))
    gate = threading.Event()
    try:
        for _ in range(MAX_CONCURRENT_INTEL):
            pl.start_intel_fetch("elevator", http=_fake_http(gate=gate))
        with pytest.raises(StateConflict):
            pl.start_intel_fetch("elevator", http=_fake_http(gate=gate))
        assert pl.registry.running_count(INTEL_BUSY_STATES) == MAX_CONCURRENT_INTEL
    finally:
        gate.set()          # 放行，别让 worker 线程挂在测试之后


# ── P0-4：情报额度从 queued 就算，不等 worker transition 到 fetching ──
# 病灶：INTEL_BUSY_STATES 原来只有 fetching，而作业**以 queued 插入**、
# transition 到 fetching 在 worker 线程里。N 个并发 refresh 可以在 transition
# 之前同时过检 —— 实测三个 queued intel 全部放行（上限 2）。
# 上面那条 start_intel_fetch 版本是**竞态**的（worker 线程可能还没 transition），
# 所以这里不起线程、直接按插入形态造 queued 作业，把窗口钉死。
def test_intel_quota_counts_from_queued_not_from_fetching():
    from app.jobs import Job, JobRegistry, new_job_id
    reg = JobRegistry()
    for i in range(MAX_CONCURRENT_INTEL):
        job = Job(new_job_id(), "intel", {"pack": "elevator"})
        assert job.state == "queued", "夹具前提：插入态必须是 queued"
        assert reg.add_if_room(job, MAX_CONCURRENT_INTEL, INTEL_BUSY_STATES), \
            f"第 {i + 1} 条就该放行"
    third = Job(new_job_id(), "intel", {"pack": "elevator"})
    assert not reg.add_if_room(third, MAX_CONCURRENT_INTEL, INTEL_BUSY_STATES), \
        "queued 的抓取不占情报额度 —— 并发 refresh 可同时过检，上限形同虚设"


# 同一病灶的另一面：queued 与 BUSY_STATES 重叠，而 add_if_room 原来**不按
# kind 分族**，于是 queued 的 intel 作业会把**生成**的名额占掉
# （INTEL_BUSY_STATES 注释里承诺"不会发生"的事）。修法：计数按族过滤。
def test_intel_queued_does_not_consume_model_quota():
    from app.jobs import Job, JobRegistry, new_job_id
    reg = JobRegistry()
    for _ in range(MAX_CONCURRENT_JOBS):
        reg.add(Job(new_job_id(), "intel", {"pack": "elevator"}))   # 直接落册，态即 queued
    gen = Job(new_job_id(), "generate", {})
    assert reg.add_if_room(gen, MAX_CONCURRENT_JOBS), \
        "queued 的情报抓取占了模型额度 —— 用户会看到「生成已达上限」，" \
        "而占着名额的是不花钱的 HTTP 请求（INTEL_BUSY_STATES 注释承诺过不会）"
    assert reg.running_count(BUSY_STATES, kind="generate") == 1
    assert reg.running_count(INTEL_BUSY_STATES, kind="intel") == MAX_CONCURRENT_JOBS


# ── 13. score 里不许有没人消费的字段（P2-21）────────────────
def test_score_has_no_dead_fields(tmp_path):
    """`item["score"]` 只写**有消费方**的字段。

    `demand_new` / `supply_n` / `total_n` / `fact_density` / `days_ago`
    是算 D/S/E 用的**中间量**，曾经随 `latest.json` 一起外发，而全仓库
    **零消费方**（前端只读 D/S/E/opportunity/is_new）。
    信号算了没人接 = 白写（铁律②）；更糟的是它会让人误以为这些数字
    参与过判定。

    ⚠ `CONSUMED` 是**允许清单**：将来要给 score 加字段，必须在这里表态。
    """
    CONSUMED = {"D", "S", "E", "opportunity", "is_new"}
    out = I.fetch_pack("elevator", tmp_path, _specs(), seeds=["电梯困人"],
                       keywords=["电梯"], http=_fake_http())
    assert out["items"], "夹具前提：至少要有条目"
    for it in out["items"]:
        extra = set(it["score"]) - CONSUMED
        assert not extra, f"score 里有没人消费的字段：{sorted(extra)}"


# ── 14. 落点键 = 目录 slug（P1-4）────────────────────────────
def test_pack_identity_is_the_directory_slug(tmp_path):
    """`PackInfo.name` = **目录名**，pack.yaml 的 `name` 键只是标签（P1-4）。

    曾经 name 取 yaml 的 `name` 键，而刷新/详情/删除端点走 `Pack()` 按
    **目录名**解析 —— 两套身份全靠「name == 目录名」的约定撑着，手写包
    改名只改一半时：重抓 404、情报读写分家、/api/packs/{name} 找不到包。
    目录名是文件系统唯一身份（两个目录不可能重名），链路统一按它寻址。
    """
    d = tmp_path / "slugpack"
    d.mkdir()
    (d / "pack.yaml").write_text("name: 别的名字\ndisplay_name: 展示名\n",
                                 encoding="utf-8")
    info = pack_info(d)
    assert info.name == "slugpack", "身份必须是目录名（yaml 的 name 键不再是身份）"
    assert info.display_name == "展示名", "display_name 的回退链不该被牵连"


def test_intel_read_and_write_share_the_slug_key(tmp_path):
    """抓取写、today 读必须是**同一个 slug**（P1-4 的对称性）。

    yaml `name` 与目录名不一致的包：数据落在 `intel/<slug>/`，
    today 按 slug 读得到；按 yaml-name 读不到 —— 落点键只有一份。
    改回 yaml-name 的话，这条会以「slug 读到 / name 读不到」两种方式红。
    """
    I.fetch_pack("slugpack", tmp_path, _specs(), seeds=["x"], keywords=["电梯"],
                 http=_fake_http())
    got = I.today(tmp_path, "slugpack", _specs())
    assert got["items"], "按 slug 读不到刚抓的数据 —— 读写键走散了"
    assert I.today(tmp_path, "别的名字", _specs())["items"] == [], \
        "yaml 的 name 键不该还能当落点键用"


def test_intel_fetch_job_writes_under_the_slug_not_the_yaml_name():
    """作业管道那一跳也要按 slug 落盘（pipeline._run_intel_fetch 的调用点）。

    手造一个「目录 slugpack、yaml name 别的名字」的包：重抓必须写进
    `intel/slugpack/`，而不是 `intel/别的名字/` —— 后者会让选题页
    （按 slug 读）永远空白，而界面还显示「重抓成功」。
    """
    tmp = _root()
    try:
        slug_dir = tmp / "packs" / "slugpack"
        shutil.copytree(tmp / "packs" / "elevator", slug_dir)
        py = slug_dir / "pack.yaml"
        py.write_text(py.read_text(encoding="utf-8").replace(
            "name: elevator", "name: 别的名字"), encoding="utf-8")
        pl = Pipeline(tmp, load_config(tmp))
        jid = pl.start_intel_fetch("slugpack", http=_fake_http())
        snap = wait_job(pl, jid, timeout=60)
        assert snap["state"] == "done", snap
        assert (tmp / "intel" / "slugpack" / "latest.json").exists(), \
            "抓取没落在 slug 名下 —— 选题页会永远空"
        assert not (tmp / "intel" / "别的名字").exists(), \
            "yaml 的 name 键还在当落点键"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_prune_history_keeps_the_newest_and_only_json(tmp_path):
    """`_prune_history` 的删除语义（P2-11：新增的删用户历史行为曾经零覆盖）。

    钉三件事：只留**名字排序最新**的 HISTORY_KEEP 份（抓取快照按时间戳命名，
    名字序即时间序）；不足保留数时一份不删；非 .json 文件（README 之类）
    永远不动。
    """
    hist = tmp_path / "hist"
    hist.mkdir()
    for i in range(I.HISTORY_KEEP + 5):
        (hist / f"2026092{i:02d}T000000.json").write_text("{}", encoding="utf-8")
    (hist / "说明.md").write_text("不是快照", encoding="utf-8")

    I._prune_history(hist)
    left = sorted(p.name for p in hist.glob("*.json"))
    assert len(left) == I.HISTORY_KEEP, f"该只留 {I.HISTORY_KEEP} 份：{len(left)}"
    assert left[0] == "202609205T000000.json", \
        f"删错了方向 —— 最旧的才该被删（剩最旧的是 {left[0]}）"
    assert (hist / "说明.md").exists(), "非快照文件不许动"

    # 不足保留数：一份都不删
    hist2 = tmp_path / "hist2"
    hist2.mkdir()
    for i in range(3):
        (hist2 / f"2026092{i:02d}T000000.json").write_text("{}", encoding="utf-8")
    I._prune_history(hist2)
    assert len(list(hist2.glob("*.json"))) == 3


def test_fetch_history_is_capped_at_the_keep_limit(tmp_path):
    """端到端：反复抓取后盘上的历史被压在 HISTORY_KEEP 内（真删用户数据的那条路径）。"""
    import app.intel as _imod
    for _ in range(4):
        I.fetch_pack("elevator", tmp_path, _specs(), seeds=["x"], keywords=["电梯"],
                     http=_fake_http())
    hist = I.intel_dir(tmp_path, "elevator") / "history"
    n = len(list(hist.glob("*.json")))
    assert n <= I.HISTORY_KEEP, f"历史没有封顶：{n}"
    # 收紧保留数再抓一次：立即生效，不需要重启
    monkey_old = _imod.HISTORY_KEEP
    _imod.HISTORY_KEEP = 2
    try:
        I.fetch_pack("elevator", tmp_path, _specs(), seeds=["x"], keywords=["电梯"],
                     http=_fake_http())
    finally:
        _imod.HISTORY_KEEP = monkey_old
    assert len(list(hist.glob("*.json"))) <= 2


def test_reserved_device_names_are_rejected_by_name(tmp_path):
    """zip 条目用 Windows 保留设备名（con.md / aux 目录）→ 白名单层点名拒绝（P3）。

    曾经放行到落盘，`mkdir`/`open` 抛 OSError → 500「导入过程中文件系统出错」，
    完全指不到是哪个条目的哪一段不行。
    """
    import zipfile as _zf
    from io import BytesIO
    from app.packimport import PackImportError, _clean_rel, import_pack
    for bad in ("con.md", "aux/knowledge/x.md", "com1.yaml", "Nul.md"):
        with pytest.raises(PackImportError) as ei:
            _clean_rel(bad)
        assert "保留设备名" in str(ei.value), f"{bad}: {ei.value}"
    # 端到端：带 con.md 的 zip 在 validate_zip 就被拒，报错里带条目名
    buf = BytesIO()
    with _zf.ZipFile(buf, "w") as z:
        z.writestr("pack.yaml", "name: rt\n")
        z.writestr("con.md", "x")
    with pytest.raises(PackImportError) as ei:
        import_pack(buf.getvalue(), tmp_path)
    assert "con.md" in str(ei.value), str(ei.value)


def test_score_topics_is_new_uses_item_key():
    """is_new / prev_keys 与去重/忽略必须是**同一个** item_key（P3：一库两键）。

    现有适配器都会给带 url 的条目合成 guid（手工导入 `guid=url`），端到端
    造不出新旧键式的差异 —— 所以这条直接对 `score_topics` 的**契约**断言：
    一个只有 url 的条目（政策库类），标题变了也必须是同一条（is_new=False）；
    旧键式（`guid or title`）会把它误报成「本周新出」。
    """
    prev_keys = {I.item_key({"guid": "", "url": "https://x/1", "title": "旧标题"})}
    items = [{"guid": "", "url": "https://x/1", "title": "新标题",
              "segment": "s", "published": "", "source_id": "manual_import"}]
    # score_topics 把评分**就地写回条目**（it["score"] = row）
    I.score_topics(items, prev_keys)
    assert items[0]["score"]["is_new"] is False, \
        "url 相同、标题变了就被当成新出 —— is_new 没用 item_key"


def test_load_latest_defends_against_bad_element_shapes(tmp_path):
    """P3：JSON 合法但元素形状坏（items 非对象 / score 非映射）→ 剔除+留因，不 500。

    B3 承诺「绝不抛」只防了顶层形状；`items: [1,2]` / `score: []` 这类手工改坏
    的元素形状，会让 today() 的排序 `.get` 冒 AttributeError。
    """
    d = I.intel_dir(tmp_path, "p")
    d.mkdir(parents=True)
    (d / "latest.json").write_text(
        json.dumps({"items": [1, 2], "fetched_at": "x"}), encoding="utf-8")
    got = I.load_latest(tmp_path, "p")
    assert got["items"] == [] and got["errors"].get("_read"), "非对象条目要剔除并留因"
    (d / "latest.json").write_text(
        json.dumps({"items": [{"title": "t", "score": []}]}), encoding="utf-8")
    got2 = I.today(tmp_path, "p", _specs())
    assert got2["items"][0]["score"] is None, "score 形状坏要按「算不出」处理"
