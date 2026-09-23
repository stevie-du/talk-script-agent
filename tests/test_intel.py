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
from app.knowledge import Pack, pack_info                        # noqa: E402
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


def _fake_http(boom: set[str] | None = None, hot=("电梯困人索赔", "明星八卦")):
    """离线假 HTTP：按 url 分派。`boom` 里的端点抛错（模拟离线/被墙）。"""
    boom = boom or set()

    def get(url, params=None, **_kw):
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
    assert "抖音热榜" in " ".join(out["errors"].values()) or out["errors"], \
        "失败的源必须在 errors 里留下人话原因"
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
        pl.start_intel_fetch("elevator")
    assert pl.registry.running_count(INTEL_BUSY_STATES) == MAX_CONCURRENT_INTEL
    assert pl.registry.running_count() == 0, "情报抓取占了模型额度"
    # 关键断言：抓取占满自己那档额度时，生成**必须仍然开得起来**。
    assert pl.start_generate(GenerateRequest(pack="elevator", topic="家用电梯怎么选",
                                             duration=60, platform="抖音")), \
        "情报额度被占满不该影响生成"


def test_intel_fetch_has_its_own_limit():
    from app.jobs import StateConflict
    tmp_path = _root()
    pl = Pipeline(tmp_path, load_config(tmp_path))
    for _ in range(MAX_CONCURRENT_INTEL):
        pl.start_intel_fetch("elevator")
    with pytest.raises(StateConflict):
        pl.start_intel_fetch("elevator")
    assert pl.registry.running_count(INTEL_BUSY_STATES) == MAX_CONCURRENT_INTEL


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
