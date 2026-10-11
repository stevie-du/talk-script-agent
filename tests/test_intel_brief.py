# -*- coding: utf-8 -*-
"""选题卡（建议层，§3.4）的契约：额度、缓存、两道闸、指纹隔离、降级。

这一层的红线来自方案本身：**排行与条目永不依赖 LLM**（B-2 精神的延伸），
所以这里一半的断言在守"没配 Key / 调用失败 / 缓存过期"时界面还能拿到什么。
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import test_intel as T                                    # noqa: E402
import app.intel as I                                     # noqa: E402
from app.config import load_config                        # noqa: E402
from app.jobs import (BUSY_STATES, MODEL_JOB_KINDS, TERMINAL_STATES,  # noqa: E402
                      TRANSITIONS, Job, JobRegistry)
from app.pipeline import Pipeline                         # noqa: E402
from app.schemas import IntelBrief                        # noqa: E402

wait_job = T.wait_job


def _fetched(tmp_path):
    """抓一次（假 HTTP），返回 today() 视图 —— 选题卡的输入必须来自真实落盘产物。"""
    I.fetch_pack("elevator", tmp_path, T._specs(), seeds=["电梯困人"],
                 keywords=["电梯"], http=T._fake_http())
    return I.today(tmp_path, "elevator", T._specs())


# ── 额度与状态机（阶段三 #1 的四件套 + 额度隔离）─────────────
def test_intel_brief_is_explicitly_in_the_model_family():
    """§3.4：`intel_brief` **显式**进 `MODEL_JOB_KINDS`。

    `jobs.py` 的注释原话是"新 kind 必须在这里归类，忘了会静默落进模型族"——
    对 brief 恰好就要模型族，但**靠默认分支恰其对**的话，额度隔离那条断言
    就没有被守的对象：默认分支哪天翻转，选题卡就不占生成额度了。
    """
    assert "intel_brief" in MODEL_JOB_KINDS
    assert "intel" not in MODEL_JOB_KINDS
    # 同族判定：选题卡与生成同族，与抓取不同族
    from app.jobs import same_quota_family
    assert same_quota_family("intel_brief", "generate") is True
    assert same_quota_family("intel_brief", "intel") is False


def test_briefing_state_is_wired_in_all_four_places():
    """README B4 的四件套：TRANSITIONS + BUSY_STATES + 前端标签 + 状态词表测试。"""
    assert "briefing" in TRANSITIONS["queued"]
    assert TRANSITIONS["briefing"] >= {"done", "failed", "cancelled"}
    assert "briefing" in BUSY_STATES, "它花模型的钱，必须占模型额度"
    words = (ROOT / "desktop" / "renderer" / "js" / "words.js").read_text(encoding="utf-8")
    progress = (ROOT / "desktop" / "renderer" / "js" / "progress.js").read_text(encoding="utf-8")
    assert 'briefing: "选题卡生成中"' in words, "没有中文标签，界面会把机器码端上屏"
    assert '"briefing"' in progress, "不在前端在跑清单里，作业会被读成已结束"


def test_brief_does_not_consume_the_intel_quota(tmp_path):
    """抓取不花钱、选题卡花钱 —— 两族额度互不占用（反着来会互相饿死）。"""
    reg = JobRegistry()
    assert reg.add_if_room(Job("a", "intel_brief", {}), 4) is True
    # 模型族被 brief 占了一个名额时，情报族照样能进两条
    for i in range(2):
        assert reg.add_if_room(Job(f"b{i}", "intel", {}), 2, {"queued", "fetching"}) is True, \
            "选题卡不该挤掉抓取的名额"


# ── 缓存与过期（阶段三 #2）──────────────────────────────────
def _key(view) -> str:
    return view["events"][0]["key"]


def test_brief_cache_is_fresh_today_and_absent_after_three_days(tmp_path):
    _fetched(tmp_path)
    key = _key(I.today(tmp_path, "elevator", T._specs()))
    now = datetime.now().astimezone()

    def stamp(dt):
        return {"event_key": key, "stage": "rising", "angles": [],
                "created_at": dt.isoformat(timespec="seconds")}

    I.save_brief(tmp_path, "elevator", key, stamp(now))
    assert I.load_brief(tmp_path, "elevator", key)["state"] == "fresh"
    # 昨天生成的：还在有效期内，但**今天没生成过** → 要重花钱，同时上一版仍给界面。
    # ⚠ 写「昨天」的文件、用真现在读，别把读者拨回 20 小时前 —— 晚上 8 点后跑，
    #   now−20h 与 now 同处一个日期，same_day 判 fresh，这条测试跟着钟点红。
    I.save_brief(tmp_path, "elevator", key, stamp(now - timedelta(days=1)))
    assert I.load_brief(tmp_path, "elevator", key)["state"] == "stale"
    # 超过 TTL → 视为未生成，连 payload 都不给
    I.save_brief(tmp_path, "elevator", key, stamp(now - timedelta(days=4)))
    gone = I.load_brief(tmp_path, "elevator", key)
    assert gone["state"] == "absent" and gone["payload"] is None
    assert "过期" in gone["note"]


def test_fetch_pack_prunes_expired_briefs(tmp_path):
    """§3.4：清理挂在抓取后面，不设独立任务。"""
    view = _fetched(tmp_path)
    key = _key(view)
    now = datetime.now().astimezone()
    d = I.briefs_dir(tmp_path, "elevator")
    d.mkdir(parents=True, exist_ok=True)
    # 文件名走引擎自己的拼名函数：键里的冒号在 Windows 上非法，
    # 在这里手写 `{key}.json` 会让这条测试测的是一份**根本不存在**的文件。
    (d / I._brief_filename(key)).write_text(json.dumps(
        {"event_key": key, "created_at": (now - timedelta(days=9)).isoformat()}),
        encoding="utf-8")
    keep = view["events"][1]["key"] if len(view["events"]) > 1 else key
    (d / I._brief_filename(keep)).write_text(json.dumps(
        {"event_key": keep, "created_at": now.isoformat()}), encoding="utf-8")
    I.fetch_pack("elevator", tmp_path, T._specs(), seeds=["电梯困人"],
                 keywords=["电梯"], http=T._fake_http())
    left = sorted(p.name for p in d.glob("*.json"))
    assert I._brief_filename(key) not in left, "过期卡必须被抓取顺手清掉"
    assert I._brief_filename(keep) in left, "没过期的一行都不许动"


def test_brief_filename_is_windows_legal():
    """冒号是 Windows 的非法文件名字符 —— 原样拼名会让选题卡一次都存不下去。"""
    assert ":" not in I._brief_filename("ev:0123456789ab")
    tmp = Path(tempfile.mkdtemp(prefix="talkscript-brief-name-"))
    p = I.save_brief(tmp, "elevator", "ev:0123456789ab",
                     {"event_key": "ev:0123456789ab", "created_at":
                      datetime.now().astimezone().isoformat(timespec="seconds"),
                      "angles": []})
    assert p.exists() and ":" not in p.name
    assert I.load_brief(tmp, "elevator", "ev:0123456789ab")["state"] == "fresh"


def test_brief_read_never_raises_on_a_broken_file(tmp_path):
    _fetched(tmp_path)
    key = _key(I.today(tmp_path, "elevator", T._specs()))
    d = I.briefs_dir(tmp_path, "elevator")
    d.mkdir(parents=True, exist_ok=True)
    (d / I._brief_filename(key)).write_text("{不是合法 JSON", encoding="utf-8")
    got = I.load_brief(tmp_path, "elevator", key)
    assert got["state"] == "absent" and "读不出来" in got["note"]


# ── 路径安全（事件键会被拼成文件名）─────────────────────────
def test_event_key_is_validated_before_it_becomes_a_filename(tmp_path):
    for bad in ("../../evil", "ev:../../x", "ev:ZZZZ", "", "ev:abcd"):
        assert I.is_event_key(bad) is False, bad
    assert I.is_event_key("ev:0123456789ab") is True
    with pytest.raises(I.IntelError):
        I.save_brief(tmp_path, "elevator", "../escape", {"a": 1})
    with pytest.raises(I.IntelError):
        I.load_brief(tmp_path, "elevator", "../escape")


def test_endpoint_rejects_a_malformed_key():
    """请求层就把形状判掉（422），而不是等到拼文件名。"""
    from fastapi.testclient import TestClient
    root = T._root()
    c = TestClient(T.create_app(root, token=T.TOKEN), base_url=T.LOOPBACK,
                   raise_server_exceptions=False)
    c.headers.update({"X-TalkScript-Token": T.TOKEN})
    r = c.post("/api/intel/brief", json={"pack": "elevator", "key": "../../etc/passwd"})
    assert r.status_code == 422, r.text


# ── 端点行为：缓存命中不起作业（阶段三 #2 的"当日不重扣费"）───
def test_brief_endpoint_returns_cache_without_starting_a_job(tmp_path):
    from fastapi.testclient import TestClient
    root = T._root()
    view = _fetched(root)
    key = _key(view)
    I.save_brief(root, "elevator", key,
                 {"event_key": key, "stage": "rising", "angles": [],
                  "created_at": datetime.now().astimezone().isoformat(timespec="seconds")})
    c = TestClient(T.create_app(root, token=T.TOKEN), base_url=T.LOOPBACK,
                   raise_server_exceptions=False)
    c.headers.update({"X-TalkScript-Token": T.TOKEN})
    r = c.post("/api/intel/brief", json={"pack": "elevator", "key": key})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["state"] == "cached" and body["job_id"] == "", \
        "当天第二次点击不该起作业 —— 一条零成本的作业会白占一个并发额度"
    assert body["brief"]["event_key"] == key


def test_brief_endpoint_generates_once_then_caches(tmp_path):
    """真跑一遍 mock：一次模型调用、落盘、第二次直接回缓存。"""
    root = T._root()
    view = _fetched(root)
    key = _key(view)
    pl = Pipeline(root, load_config(root), data_dir=root)
    calls = {"n": 0}
    real = pl.llm.chat_json

    def counting(*a, **kw):
        calls["n"] += 1
        return real(*a, **kw)

    pl.llm.chat_json = counting                       # type: ignore[method-assign]
    jid, reused = pl.start_intel_brief("elevator", key)
    assert reused is False, "第一次点没有可搭的作业"
    snap = wait_job(pl, jid, timeout=60)
    assert snap["state"] in TERMINAL_STATES, snap
    assert snap["state"] == "done", snap.get("error")
    assert calls["n"] == 1
    payload = I.load_brief(root, "elevator", key)
    assert payload["state"] == "fresh"
    angles = payload["payload"]["angles"]
    assert angles and angles[0]["hook"]["line"], "卡里要有能直接念的钩子句"
    # mock 夹具从输入里取标题：**输入真的进了 prompt** 才能在这里被认出来
    assert view["events"][0]["title"][:6] in json.dumps(
        payload["payload"], ensure_ascii=False), "夹具没有复述输入，等于没测到注入"


def test_clicking_while_a_brief_is_in_flight_does_not_charge_twice(tmp_path):
    """§5 #2「当日重复点击不重扣费」里过去没被盖住的那一段：作业**还在飞**的几十秒。

    `load_brief` 只认盘上那份缓存 —— 在飞的时候缓存还是空的，于是第二次点击
    （换了窗口、或重启之后再点一次）会起**第二条**作业：两次模型调用、两张一样的卡，
    而界面上看不出任何异常（第二张卡照样出来，钱已经花了两遍）。
    这条必须真跑线程：只看 registry 的实现等于没测。
    """
    import threading
    root = T._root()
    key = _key(_fetched(root))
    pl = Pipeline(root, load_config(root), data_dir=root)
    real = pl.llm.chat_json
    calls = {"n": 0}
    entered = threading.Event()
    release = threading.Event()

    def blocking(*a, **kw):
        calls["n"] += 1
        entered.set()
        assert release.wait(timeout=30), "测试没有放行"
        return real(*a, **kw)

    pl.llm.chat_json = blocking                        # type: ignore[method-assign]
    jid1, reused1 = pl.start_intel_brief("elevator", key)
    assert jid1 and reused1 is False
    assert entered.wait(timeout=30), "作业没跑到模型调用那一步"
    jid2, reused2 = pl.start_intel_brief("elevator", key)
    assert jid2 == jid1 and reused2 is True, "在飞的这一条必须被搭上，不能再起一条"
    release.set()
    snap = wait_job(pl, jid1, timeout=60)
    assert snap["state"] == "done", snap.get("error")
    assert calls["n"] == 1, f"两次点击烧了 {calls['n']} 次模型调用"


def test_model_cannot_redecide_the_lifecycle_stage(tmp_path, monkeypatch):
    """`stage` 以榜单算出的那个为准：模型回什么都覆盖掉（§3.4「只复述不自己判」）。

    这条**必须跑整条作业路径**：只看 prompt 里写了"不要自己改判"证明不了任何事，
    真正会出问题的是落盘那一步有没有把模型返回的 stage 采纳进去。
    """
    root = T._root()
    view = _fetched(root)
    key = _key(view)
    board_stage = view["events"][0]["stage"]
    # 模型故意回一个**与榜单不同**的值（不写死：真实抓回来的事件处在哪个阶段
    # 取决于今天的历史，写死会让这条测试在某个时刻变成"模型恰好说对了"）。
    wrong_stage = "fading" if board_stage != "fading" else "rising"
    pl = Pipeline(root, load_config(root), data_dir=root)

    class Rogue(IntelBrief):
        pass

    def lying(*a, **kw):
        return Rogue.model_validate({"event_key": key, "stage": wrong_stage,
                                      "angles": [{"type": "解读",
                                                  "titles": {"trend": ["甲"], "evergreen": ["乙"]},
                                                  "hook": {"type": "疑问", "line": "丙"},
                                                  "risk": "无", "fit": 0.5}]})

    monkeypatch.setattr(pl.llm, "chat_json", lying)
    jid, reused = pl.start_intel_brief("elevator", key)
    assert reused is False
    snap = wait_job(pl, jid, timeout=60)
    assert snap["state"] == "done", snap.get("error")
    saved = I.load_brief(root, "elevator", key)["payload"]
    assert saved["stage"] == board_stage != wrong_stage, \
        "落盘的 stage 必须是榜单算出的那一个，模型说什么都不能采纳"


def test_brief_is_not_part_of_the_generation_fingerprint(tmp_path):
    """§3.4 指纹隔离的**反向**断言：briefs 目录不出现在任何生成路径里。

    只断言"文件名里搜不到 brief"是不够的 —— 真正要防的是有人把选题卡接进
    select 的上下文：那样情报刷新一次，同参数重跑就换了指纹，白烧一次 4000 token。
    所以这里查两处：`_plan_key` 的入参不含 brief，`select_for_prompt` 仍然只给
    标题+出处（PROMPT_LIMIT=3，B7 口径没动）。
    """
    import inspect
    import app.pipeline as P
    src = inspect.getsource(P.Pipeline._plan_key)
    assert "brief" not in src.lower(), "选题卡进了指纹 —— 它是一次性产物，不是生成输入"
    prompts = (ROOT / "app" / "prompts.py").read_text(encoding="utf-8")
    assert "brief" not in prompts.lower().replace("briefly", ""), \
        "提示词层出现了选题卡的痕迹"
    root = T._root()
    _fetched(root)
    I.fetch_pack("elevator", root, T._specs(), seeds=["电梯困人"],
                 keywords=["电梯"], http=T._fake_http())
    block, keys, _dropped = I.select_for_prompt(root, "elevator", None)
    # ⚠ 原来这条写的是 `len(keys) <= I.PROMPT_LIMIT` —— **拿常量比它自己**：
    #   把 `PROMPT_LIMIT` 从 3 改成 40，右边跟着变，测试照样绿，而那一屏提示词
    #   的体积与 token 成本正是 B7 定死 3 条要防的东西。口径写死，两处各自判。
    assert I.PROMPT_LIMIT == 3, "B7 定的条数就是 3；要改先改需求方案，再改这里"
    assert len(keys) <= 3, "实际选中的条数不许超过 3（自指断言守不住任何东西）"
    assert block and "不得照搬原文" in block


# ── 两道闸独立生效（阶段三 #3）─────────────────────────────
def test_brief_output_passes_the_banword_gate():
    payload = {"event_key": "ev:0123456789ab", "stage": "rising", "angles": [
        {"type": "解读", "titles": {"trend": ["终身免费维保怎么做"], "evergreen": ["维保周期"]},
         "hook": {"type": "疑问", "line": "真的能终身免费吗"}, "risk": "无"}]}
    cleaned, hits = I.brief_banwords(payload, ("终身免费",))
    # `hits` 数的是**处**（标题 1 处 + 钩子句 1 处），作业日志那句"有 N 处命中"用的就是它
    assert hits == ["终身免费", "终身免费"]
    a = cleaned["angles"][0]
    assert a["titles"]["trend"] == ["{{待补}}"], "hard 命中整段替换，不是删掉那条角度"
    assert a["hook"]["line"] == "{{待补}}"
    assert a["risk"] == "命中禁用词", "要让用户看出是被摘标，而不是没有这个角度"
    # 反向对照：没命中时**一个都不许动**（不许把干净卡也洗一遍）
    same, none = I.brief_banwords(payload, ("绝对安全",))
    assert none == [] and same == payload


def test_only_the_angle_that_hit_gets_the_risk_mark():
    """摘标按**角度**各记各的：一个角度命中不许把整张卡都标成有风险。

    共用一个 `hit` 列表时，第一个角度之后每个干净角度都会被写上「命中禁用词」——
    卡片说的话与数据对不上，而用户分辨不出来（只会以为这题处处是雷）。
    """
    payload = {"event_key": "ev:0123456789ab", "stage": "rising", "angles": [
        {"type": "解读", "titles": {"trend": ["终身免费维保"], "evergreen": ["周期"]},
         "hook": {"type": "疑问", "line": "真的吗"}, "risk": "无"},
        {"type": "盘点", "titles": {"trend": ["维保周期怎么定"], "evergreen": ["谁说了算"]},
         "hook": {"type": "共鸣", "line": "每家都被问过"}, "risk": "无"}]}
    cleaned, hits = I.brief_banwords(payload, ("终身免费",))
    assert len(hits) == 1, "汇总数的是**处**，一处就是一处"
    a0, a1 = cleaned["angles"]
    assert a0["risk"] == "命中禁用词" and a0["titles"]["trend"] == ["{{待补}}"]
    assert a1["risk"] == "无", "第二个角度一个字都没命中"
    assert a1["titles"]["trend"] == ["维保周期怎么定"]


def test_stage_is_echoed_from_the_board_not_decided_by_the_model():
    """§3.4：`stage` 与状态药丸同源，LLM 只复述不自己判。"""
    root = T._root()
    view = _fetched(root)
    key = _key(view)
    ev = view["events"][0]
    assert ev["stage"] in I.BRIEF_STAGE_LABEL
    user = I.build_brief_user(ev, pack_label="电梯", segments=["维保"], audience="业主")
    assert I.BRIEF_DATA_HEAD in user, "外部标题/摘要进 prompt 前要声明这是数据不是指令"
    assert "不要自己改判" in I.BRIEF_SYSTEM
    assert "生命周期阶段" in user and ev["status_label"] in user
    # 输入里带出处（§3.4：标题集合 + desc + 出处 + 阶段 + 领域/受众）
    assert "各平台原始条目" in user and "本账号覆盖的细分领域" in user


# ── 输出形状兼容（2026-10-12 A-1）────────────────────────────
# 真模型实测（GLM-5.3-Flash）：`titles.trend / evergreen` 被输出成**单个字符串**
# 而不是字符串数组，4/4 次调用在校验层全折 —— 而校验失败带错误重发治不了形状
# 漂移（模型会原样再犯）。修法是入口归一（schemas._brief_str_list）+ 提示词钉
# 形状骨架 + 低温。这里的归一**不是** union 放宽：str 漏进 brief_banwords 会被
# 逐字符迭代（预案已论证），归一之后走的仍是严格的 `list[str]` 校验。
def test_brief_titles_are_tolerant_of_a_bare_string():
    """str → [str]：模型把「标题数组」写成单个字符串时收下来，不整卡报废。"""
    brief = IntelBrief.model_validate({"angles": [{
        "type": "解读",
        "titles": {"trend": "跟热点款标题", "evergreen": "长尾款标题"},
        "keywords": "维保", "hook": {"type": "疑问", "line": "丙"}, "risk": "无"}]})
    a = brief.angles[0]
    assert a.titles.trend == ["跟热点款标题"]
    assert a.titles.evergreen == ["长尾款标题"]
    assert a.keywords == ["维保"], "keywords 是同一个形状漂移，同款归一"


def test_brief_titles_are_tolerant_of_none():
    """None → []：模型省略字段时落「没有」，不是校验失败。"""
    brief = IntelBrief.model_validate({"angles": [{
        "titles": {"trend": None, "evergreen": None}, "keywords": None}]})
    a = brief.angles[0]
    assert a.titles.trend == [] and a.titles.evergreen == [] and a.keywords == []


def test_brief_titles_keep_a_valid_list_untouched():
    """反向对照：形状本来就对的东西**一个字都不动**（不许把干净卡也洗一遍）。"""
    titles = ["甲", "乙", "丙"]
    brief = IntelBrief.model_validate({"angles": [{
        "titles": {"trend": titles, "evergreen": ["长尾"]}, "keywords": ["词一", "词二"]}]})
    a = brief.angles[0]
    assert a.titles.trend == titles and a.titles.evergreen == ["长尾"]
    assert a.keywords == ["词一", "词二"]


def test_brief_titles_still_cap_after_normalisation():
    """归一之后仍走严格校验：第 7 条标题 / 第 9 个关键词照样拦（不放宽上限）。"""
    seven = {"angles": [{"titles": {"trend": [f"标题{i}" for i in range(7)]}}]}
    with pytest.raises(ValidationError):
        IntelBrief.model_validate(seven)
    nine = {"angles": [{"keywords": [f"词{i}" for i in range(9)]}]}
    with pytest.raises(ValidationError):
        IntelBrief.model_validate(nine)


def test_brief_system_pins_the_shape_hint_for_string_arrays():
    """提示词里钉形状骨架：`trend / evergreen` 是字符串数组，不是单个字符串。

    照 `test_stage_is_echoed…` 的写法钉 `BRIEF_SYSTEM` 的原文 —— 校验层归一是
    兜底，提示词才是治本的那一半；骨架被人摘掉时这里必须红。
    """
    assert '"trend": ["' in I.BRIEF_SYSTEM
    assert '"evergreen": ["' in I.BRIEF_SYSTEM
    assert "字符串数组" in I.BRIEF_SYSTEM and "不是单个字符串" in I.BRIEF_SYSTEM


def test_no_model_configured_leaves_the_board_readable(tmp_path):
    """降级（§3.4 硬约束第一条）：没配 Key 时事件详情仍显示原始条目聚合。

    这条守的是整层设计的地基 —— **排行层零模型调用**（B-2）不能因为
    加了建议层而回退：`today()` 全程不碰 `llm`，抓一次之后即使把模型配置清空，
    选题页与事件详情也必须一个字都不少。
    """
    view = _fetched(tmp_path)
    assert view["events"], "没有模型也要有排行"
    ev = view["events"][0]
    assert ev["rows"], "事件详情用的是抓回来的条目，不是模型产物"
    assert ev["brief"] in ("absent", "stale", "fresh")
    # 不启动作业，只证明读取与抓取路径上从没构造过模型客户端：
    # 一旦有人在这两条路上接了 LLM，首装用户（没配 Key）的选题页就会空掉。
    import app.pipeline as P
    seen = {"n": 0}
    orig = P.Pipeline.llm

    def spy(self):
        seen["n"] += 1
        return orig.fget(self)

    P.Pipeline.llm = property(spy)
    try:
        I.today(tmp_path, "elevator", T._specs())
        I.fetch_pack("elevator", tmp_path, T._specs(), seeds=["电梯困人"],
                     keywords=["电梯"], http=T._fake_http())
    finally:
        P.Pipeline.llm = orig
    assert seen["n"] == 0, "读取与抓取路径上不该出现模型客户端"
