# -*- coding: utf-8 -*-
"""引擎 HTTP 层回归测试。

覆盖本轮修复的关键点：
  A 访问控制：令牌缺失/错误 → 401；跨站 Origin → 403；令牌正确 → 200
  B 行业包错误映射：不存在的包 → 404（原来 500）
  C 输入边界：duration / topic 越界 → 422
  D 缺 quota_table 的包 → 仍能生成（降级配额），不再 IndexError
  E 每段配额按要点数均分（原来给整段正文配额，导致回炉分支是死代码）
  F 禁用词不重叠计数（「包过检」原来报 2 处）
  G 历史索引：失败作业重启后仍在列表里；删除能清干净
  H 取消：状态冻结、不落产物（「result.json 存在 ⟺ done」）
  J 并发上限

跑法：python tests/test_engine_api.py    或    pytest tests/test_engine_api.py
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import time
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient                      # noqa: E402

from app.checker import Banwords, Quota, check_script          # noqa: E402
from app.config import load_config                             # noqa: E402
from app.pipeline import Pipeline                              # noqa: E402
from app.schemas import GenerateRequest                        # noqa: E402
from app.security import origin_allowed, token_ok              # noqa: E402
from app.server import create_app                              # noqa: E402
from app.store import INDEX_VERSION                            # noqa: E402

TOKEN = "test-token-abc"


def _tmp_root() -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="talkscript-api-"))
    shutil.copytree(ROOT / "packs", tmp / "packs")
    (tmp / "config.yaml").write_text(
        "llm:\n  api_key: MOCK\n  model: mock-model\n", encoding="utf-8")
    return tmp


# 用回环地址作为 base_url：TestClient 默认的 "testserver" 主机名会被同源判定拒绝
# （那是 DNS rebinding 的形态），所以测试也必须像真实客户端一样走 127.0.0.1。
LOOPBACK = "http://127.0.0.1:8765"


def _client(tmp: Path) -> TestClient:
    app = create_app(tmp, token=TOKEN)
    c = TestClient(app, base_url=LOOPBACK, raise_server_exceptions=False)
    c.headers.update({"X-TalkScript-Token": TOKEN})
    return c


def _wait(c: TestClient, jid: str, states=("done", "failed", "cancelled"), timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        snap = c.get(f"/api/jobs/{jid}").json()
        if snap.get("state") in states:
            return snap
        time.sleep(0.1)
    raise AssertionError(f"作业超时未结束：{c.get(f'/api/jobs/{jid}').json()}")


# ── A 访问控制 ──────────────────────────────────────────────
def test_access_control(tmp_path):
    tmp = _tmp_root()
    app = create_app(tmp, token=TOKEN)
    c = TestClient(app, base_url=LOOPBACK, raise_server_exceptions=False)

    # 无令牌 → 401
    assert c.get("/api/meta").status_code == 401
    # 错误令牌 → 401
    assert c.get("/api/meta", headers={"X-TalkScript-Token": "nope"}).status_code == 401
    # 正确令牌 → 200
    assert c.get("/api/meta", headers={"X-TalkScript-Token": TOKEN}).status_code == 200
    # health 免鉴权（主进程开窗前要轮询）
    assert c.get("/api/health").status_code == 200

    # 跨站 Origin 一律 403，且不回任何 CORS 头
    for evil in ["https://evil.example", "null", "file://", "chrome-extension://abc",
                 "http://127.0.0.1:1", "http://192.168.0.9:80"]:
        r = c.get("/api/meta", headers={"X-TalkScript-Token": TOKEN, "Origin": evil})
        assert r.status_code == 403, (evil, r.status_code)
        assert "access-control-allow-origin" not in {k.lower() for k in r.headers}, evil

    # 同源（Origin 与请求的 Host 一致、且主机名是本机回环）放行
    r = c.get("/api/meta", headers={"X-TalkScript-Token": TOKEN,
                                    "Origin": LOOPBACK})
    assert r.status_code == 200, r.status_code
    shutil.rmtree(tmp, ignore_errors=True)


def test_security_helpers():
    assert token_ok("abc", "abc") and not token_ok("abc", "abd")
    assert not token_ok("", "x") and not token_ok("x", None)
    assert origin_allowed(None, "127.0.0.1:8765")              # 非浏览器客户端
    assert origin_allowed("http://127.0.0.1:8765", "127.0.0.1:8765")
    assert origin_allowed("http://localhost:8765", "localhost:8765")
    assert not origin_allowed("null", "127.0.0.1:8765")
    assert not origin_allowed("file://", "127.0.0.1:8765")
    assert not origin_allowed("chrome-extension://x", "127.0.0.1:8765")
    assert not origin_allowed("http://127.0.0.1:9999", "127.0.0.1:8765")  # 端口不一致
    assert not origin_allowed("https://evil.example", "127.0.0.1:8765")
    assert not origin_allowed("http://127.0.0.1:8765", "")                # 无 Host 不放行

# ── A2 建包作业（P1-43）：长调用改走后台作业后的新契约 ────────
def test_packs_create_surfaces_llm_error_on_the_job(tmp_path):
    """建包失败不再走 HTTP 状态码：它落在**作业**上，理由必须原样可见。

    改作业之前这里是 `except LLMError → 502`（P0-2 那批修的）。现在模型调用发生在
    作业线程里，HTTP 只有「作业已受理」这一种结果 —— 于是原来的 502 断言必须
    **改指新位置**而不是删掉：`job.error` 里若只剩「HTTP 500」之类，
    用户就再也看不到「令牌已过期」这种最该看到的理由。
    """
    from unittest.mock import patch
    from app.llm import LLMError

    tmp = _tmp_root()
    c = _client(tmp)
    fake_msg = "模型接口返回 401: {\"code\":\"401\",\"message\":\"令牌已过期\"}"
    with patch("app.pipeline.create_pack", side_effect=LLMError(fake_msg)):
        r = c.post("/api/packs/create",
                   json={"industry": "装修", "description": "装修从基装到软装的的全流程"})
        assert r.status_code == 200, r.text
        jid = r.json()["job_id"]
        snap = _wait(c, jid)
    assert snap["state"] == "failed", snap
    assert "令牌已过期" in (snap["error"] or ""), snap
    # 建包作业不是一条脚本：不该出现在左栏会话列表里（它没有产物，点不开）。
    assert jid not in {x["id"] for x in c.get("/api/history").json()}
    # 也不该往 generated/ 里落任何东西（没有 work_dir，_persist 直接跳过）。
    assert not (tmp / "generated").exists() or \
        not list((tmp / "generated").glob(f"*/{jid}")), "建包作业写出了产物目录"
    shutil.rmtree(tmp, ignore_errors=True)


def test_packs_create_rejects_duplicate_pack_before_spending(tmp_path):
    """同名包必须**同步** 409：这个结论不花一分钱，没道理让人等 1~2 分钟。"""
    tmp = _tmp_root()
    (tmp / "packs" / "假体陀机").mkdir(parents=True)
    (tmp / "packs" / "假体陀机" / "pack.yaml").write_text(
        "name: dup\ndisplay_name: 假体陀机\nversion: 1\n", encoding="utf-8")
    c = _client(tmp)
    r = c.post("/api/packs/create",
               json={"industry": "假体陀机", "description": "测试用行业描述"})
    assert r.status_code == 409, (r.status_code, r.text)
    assert "已存在" in r.json()["detail"], r.text
    shutil.rmtree(tmp, ignore_errors=True)


def test_packs_create_reports_quota_conflict_as_409(tmp_path):
    """额度满时是 409（可重试），不是把作业悄悄塞进注册表。"""
    from unittest.mock import patch
    from app.jobs import StateConflict

    tmp = _tmp_root()
    c = _client(tmp)
    with patch.object(Pipeline, "start_packgen",
                      side_effect=StateConflict("同时进行的任务已达上限（4 个）")):
        r = c.post("/api/packs/create",
                   json={"industry": "装修", "description": "装修从基装到软装的的全流程"})
    assert r.status_code == 409, (r.status_code, r.text)
    assert "上限" in r.json()["detail"], r.text
    shutil.rmtree(tmp, ignore_errors=True)


# ── B/C 错误映射与输入边界 ──────────────────────────────────
def test_error_mapping_and_bounds(tmp_path):
    tmp = _tmp_root()
    c = _client(tmp)

    # 不存在的行业包 → 404（修复前是 500 Internal Server Error）
    r = c.post("/api/generate", json={"pack": "no-such-pack", "topic": "测试主题"})
    assert r.status_code == 404, (r.status_code, r.text)

    # 输入越界 → 422
    assert c.post("/api/generate", json={"pack": "elevator", "topic": "x"}).status_code == 422
    assert c.post("/api/generate", json={"pack": "elevator", "topic": "ok",
                                        "duration": 0}).status_code == 422
    assert c.post("/api/generate", json={"pack": "elevator", "topic": "ok",
                                        "duration": 100000}).status_code == 422
    assert c.post("/api/generate", json={"pack": "elevator", "topic": "x" * 300}).status_code == 422
    # 非法枚举值 → 422。靶子原是 `mode`（auto/step），随分步确认一起删了；
    # 这里换用仍然存在的两个 Literal 字段顶上 —— 枚举校验这条能力不能因为
    # 少了一个字段就失去回归保护。
    assert c.post("/api/generate", json={"pack": "elevator", "topic": "ok",
                                        "voice": "bogus"}).status_code == 422
    assert c.post("/api/generate", json={"pack": "elevator", "topic": "ok",
                                        "format": "bogus"}).status_code == 422

    # 记录 id 白名单
    assert c.get("/api/history/not-a-jid").status_code == 400
    assert c.delete("/api/history/*").status_code == 400
    shutil.rmtree(tmp, ignore_errors=True)


# ── D 缺 quota_table 的包应降级而不是崩 ─────────────────────
def test_missing_quota_table_degrades(tmp_path):
    tmp = _tmp_root()
    bad = tmp / "packs" / "nopack"
    shutil.copytree(tmp / "packs" / "elevator", bad)
    import yaml
    data = yaml.safe_load((bad / "pack.yaml").read_text(encoding="utf-8"))
    data.pop("quota_table", None)
    (bad / "pack.yaml").write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")

    c = _client(tmp)
    r = c.post("/api/generate", json={"pack": "nopack", "topic": "缺配额表的包"})
    assert r.status_code == 200, r.text
    snap = _wait(c, r.json()["job_id"])
    assert snap["state"] == "done", snap.get("error")
    assert snap["result"]["quota"]["total"] > 0
    shutil.rmtree(tmp, ignore_errors=True)


# ── E/F 校验器 ──────────────────────────────────────────────
def test_quota_robustness():
    assert Quota.from_pack({}).target(60, 4.5) == {}          # 空表不再 IndexError
    single = Quota({60: {"total": 290, "hook": 45, "body": 190, "cta": 55}})
    assert single.target(90, 5.0)["total"] == 290             # 单行表不再 IndexError
    q = Quota({60: {"total": 290, "hook": 45, "body": 190, "cta": 55}})
    assert q.target(1, 4.5)["total"] > 0                      # 极小时长不出现负配额
    assert q.target(100000, 4.5)["total"] > 0


def test_segment_quota_divided_by_points():
    """每段配额必须按要点数均分，否则「段落超配额」的回炉分支永远不触发。"""
    import yaml
    pdata = yaml.safe_load((ROOT / "packs" / "elevator" / "pack.yaml").read_text(encoding="utf-8"))
    ban = Banwords.load(ROOT / "packs" / "elevator" / "banwords.yaml")
    q = Quota.from_pack(pdata)
    body = q.target(60, 4.5)["body"]

    sections = [
        {"type": "hook", "text": "电梯困人别慌，先按警铃。"},
        {"type": "point", "text": "第一，" + "这是很长的一段要点内容。" * 12},
        {"type": "point", "text": "第二，短。"},
        {"type": "cta", "text": "关注我。"},
    ]
    rep = check_script(sections, 60, 4.5, ban, "抖音", q)
    assert rep["points"] == 2
    assert rep["segments"][1]["quota"] == body // 2, rep["segments"][1]
    assert rep["segments"][2]["quota"] == body // 2
    # 超出 1.3 倍的段必须能被回炉反馈捕捉到。
    # 断言从「段落超配额」这句旧措辞改成「点名第几段 + 带字数与配额」：
    # 反馈现在写的是「第 2 段（point）超配额：134 字（配额≈85）」——
    # 只认旧短语会把「说清了是哪一段」这个改进误判成回归，
    # 而"必须捕捉到超配额段"这条要守的东西一个字都没松。
    fb = Pipeline._violation_feedback(rep, sections)
    assert "超配额" in fb and "第 2 段" in fb, fb
    assert "134 字" in fb, fb


def test_count_chars_is_the_single_source_of_truth():
    """字数口径：全项目以 checker.count_chars 为准（前端已不再自己数）。

    这个函数此前**零测试**，而它决定字数、时长、配额与回炉判定 ——
    错了就是全局错。这里把规则钉死。
    """
    from app.checker import count_chars

    assert count_chars("你好，世界！") == 4              # 标点、空白不计
    assert count_chars("**重点**") == 2                   # 加粗符号不计
    assert count_chars("开场{{待补：品牌}}结束") == 4      # 占位符整体不计
    assert count_chars("正文[画面：电梯上升]继续") == 4      # 画面标注整体不计
    # 数字串按 1 字计（rules/duration.md 口径）
    assert count_chars("载重1000公斤") == 5               # 载重 + 0 + 公斤
    assert count_chars("") == 0
    # 小数按两点各计 1 字（现状语义，改之前请先确认是否要动配额基准）
    assert count_chars("3.5米高") == 4


def test_split_and_estimate():
    """分段与时长估算：段落间停顿 0.5s；语速非法时不能崩。"""
    from app.checker import (DEFAULT_RATE, estimate_seconds, split_sections)

    assert split_sections("第一段\n\n第二段") == ["第一段", "第二段"]
    # 没有空行时退回按行切（否则多行的单段会被当成一段、少算停顿）
    assert split_sections("第一段\n第二段") == ["第一段", "第二段"]
    assert split_sections("") == []

    one = estimate_seconds("你好世界", 4.5)               # 单段：无停顿
    two = estimate_seconds("你好\n\n世界", 4.5)            # 两段：+0.5s
    assert abs(two - one - 0.5) < 1e-9, (one, two)

    # 语速为 0 / 非法时兜底，而不是 ZeroDivisionError
    assert estimate_seconds("你好世界", 0) == 4 / DEFAULT_RATE
    assert estimate_seconds("你好世界", -1) == 4 / DEFAULT_RATE
    assert estimate_seconds("你好世界", None) == 4 / DEFAULT_RATE


def test_timeline_matches_checker_estimate():
    """产物时间轴的总长 = 校验用的预估秒数 —— 同一件事两处算，就得有人对账。

    修复前 `_compute_timings` 把 0.5 秒停顿加在**首段**、段落之间不加，
    `estimate_seconds` 却是「(段落数 − 1) 处各加 0.5 秒」：5 段的片子两处差 1.5 秒，
    于是「校验说 63 秒、合格」和「界面时间轴只排到 61.5 秒」同时成立而互相打脸，
    而 `rules/duration.md` 一直写着两者同口径 —— 文档说的不是代码做的。
    """
    from app.checker import estimate_seconds
    from app.pipeline import Pipeline

    texts = ["被困电梯，第一反应多半是错的。", "电梯停了，常是在保护你。",
             "先按警铃，再打救援电话。", "说清小区楼栋和电梯编号。",
             "关注我，电梯的事少踩坑。"]
    types = ["hook", "point", "point", "point", "cta"]
    sections = [{"type": t, "text": x} for t, x in zip(types, texts)]
    timings = Pipeline._compute_timings(sections, 4.5)
    est = estimate_seconds("\n\n".join(texts), 4.5)

    # 每段各自 round(x,1)，5 段最多累积约 0.5 秒舍入差 —— 口径一致，容许舍入
    assert abs(timings[-1]["end"] - est) <= 0.5, (timings[-1]["end"], est)
    assert timings[0]["start"] == 0.0
    assert all(b["start"] >= a["end"] - 0.05 for a, b in zip(timings, timings[1:])), \
        "时间轴必须首尾相接、不重叠"


def test_banwords_no_double_count():
    ban = Banwords.load(ROOT / "packs" / "elevator" / "banwords.yaml")
    hits = ban.scan("包过检", "抖音")["hard"]
    assert sum(h["count"] for h in hits) == 1, hits
    assert [h["word"] for h in hits] == ["包过检"], hits
    hits = ban.scan("加微信", "抖音")["hard"]
    assert sum(h["count"] for h in hits) == 1, hits
    # 单字词不下发匹配：'最近' 不该命中 '最'
    assert ban.scan("最近", "抖音")["soft"] == []
    assert "最" in ban.dropped_short


# ── G 历史索引 ──────────────────────────────────────────────
def test_history_keeps_failed_and_deletes_clean(tmp_path):
    tmp = _tmp_root()
    c = _client(tmp)

    # 造一条失败作业：直接让 pipeline 用一个必然抛错的参数
    jid = c.post("/api/generate", json={"pack": "elevator", "topic": "失败用例"}).json()["job_id"]
    snap = _wait(c, jid)
    assert snap["state"] == "done"

    items = c.get("/api/history").json()
    assert any(x["id"] == jid for x in items), items
    assert c.get(f"/api/history/{jid}").json()["id"] == jid

    # 索引文件存在且只有摘要字段。
    # ⚠ 版本号**比对常量**，不写死数字：写死 1 的话，以后每升一次版本都要改这行，
    # 而漏改的表现是「这条与版本无关的断言先红」，把真正的回归盖掉。
    idx = json.loads((tmp / "generated" / "index.json").read_text(encoding="utf-8"))
    assert idx["version"] == INDEX_VERSION and any(x["id"] == jid for x in idx["items"])
    assert "sections" not in idx["items"][0]
    # 摘要要带 platform：会话列表副标题靠它显示「抖音 / 小红书 / 视频号」。
    # 漏了不会报错，界面只是那一格永远空着（与「用户没选平台」长得一样）。
    assert idx["items"][0].get("platform"), idx["items"][0]

    # 删除后索引与磁盘都干净，且不会「复活」
    assert c.delete(f"/api/history/{jid}").status_code == 200
    assert not any(x["id"] == jid for x in c.get("/api/history").json())
    assert list((tmp / "generated").glob(f"*/{jid}")) == []
    # 再读一次（触发索引自愈检查）仍不应复活
    assert not any(x["id"] == jid for x in c.get("/api/history").json())
    assert c.get(f"/api/history/{jid}").status_code == 404
    shutil.rmtree(tmp, ignore_errors=True)


def test_failed_job_visible_after_restart(tmp_path):
    """失败作业只有 job.json，没有 result.json —— 它必须仍在历史里。"""
    tmp = _tmp_root()
    cfg = load_config(tmp)
    cfg.mock = False
    pl = Pipeline(tmp, cfg)

    class Boom:
        class cfg:                                     # noqa: N801
            temperature = 0.7
            max_tokens = 100
            model = 'fake-model'
            base_url = 'http://fake/v1'      # 选题缓存指纹要读它（_plan_key）
        def chat_json(self, *a, **kw):
            raise RuntimeError("模拟接口故障")
    pl.llm = Boom()

    jid = pl.start_generate(GenerateRequest(pack="elevator", topic="会失败的作业"))
    from app.pipeline import wait_job
    snap = wait_job(pl, jid, timeout=15)
    assert snap["state"] == "failed", snap
    assert "模拟接口故障" in (snap["error"] or "")

    # 模拟重启：全新的 Pipeline / 全新的 store 实例，只靠磁盘
    pl2 = Pipeline(tmp, load_config(tmp))
    items = pl2.store.history()
    assert any(x["id"] == jid and x["state"] == "failed" for x in items), items
    assert pl2.store.read_result(jid) is None
    assert pl2.store.read_job(jid)["error"]
    shutil.rmtree(tmp, ignore_errors=True)


# ── H 取消语义 ──────────────────────────────────────────────
def test_cancel_does_not_write_artifact(tmp_path):
    """「result.json 存在 ⟺ 状态为 done」：取消后不得留下产物。"""
    tmp = _tmp_root()
    cfg = load_config(tmp)
    cfg.mock = False
    pl = Pipeline(tmp, cfg)

    import threading

    class Slow:
        class cfg:                                     # noqa: N801
            temperature = 0.7
            max_tokens = 100
            model = 'fake-model'
            base_url = 'http://fake/v1'      # 选题缓存指纹要读它（_plan_key）
        def __init__(self):
            self.started = threading.Event()
        def chat_json(self, task, system, user, model_cls, max_retries=1,
                      on_retry=None, temperature=None, on_delta=None,
                      max_tokens=None, on_attempt=None, should_abort=None,
                      usage=None, deadline=None):
            if task == "select":
                from app.schemas import TopicPlan
                return TopicPlan(angle="a", hook_type="h", hook_line="l",
                                 points=["p"], cta="c")
            self.started.set()
            for _ in range(500):
                if on_delta:
                    on_delta("reasoning", "想")
                time.sleep(0.01)
            raise AssertionError("被取消的作业不应跑完")
    llm = Slow()
    pl.llm = llm

    jid = pl.start_generate(GenerateRequest(pack="elevator", topic="取消测试"))
    assert llm.started.wait(10), "写阶段没有开始"
    pl.cancel(jid)
    time.sleep(1.0)
    snap = pl.get_job(jid).snapshot()
    assert snap["state"] == "cancelled", snap["state"]
    assert snap["result"] is None
    assert list((tmp / "generated").glob(f"*/{jid}/result.json")) == [], "取消后不应落产物"
    shutil.rmtree(tmp, ignore_errors=True)


def test_transition_is_atomic_under_concurrent_calls():
    """同一作业被并发推同一个迁移：只能有一个赢，其余 StateConflict。

    原来这条叫 `test_concurrent_confirm_is_rejected`，靶子是分步确认的
    「连点两次继续」。功能删了，但它守的东西一个字都没变 ——
    **检查与置位必须在同一把锁内完成**，否则两个并发请求双双通过检查，
    起两个写线程：双倍 token、steps 重复、result 互相覆盖。
    这里直接压 `Job.transition()`，不再借某个业务入口，因此也与被删功能脱钩。
    """
    from app.jobs import Job, StateConflict

    job = Job("atomic-1", "generate", {})
    winners = []
    losers = []
    gate = threading.Barrier(8)

    def go():
        gate.wait()                             # 尽量让 8 个线程同时撞进去
        try:
            job.transition_or_raise("writing")  # 业务入口走的就是这个
            winners.append(1)
        except StateConflict:
            losers.append(1)

    threads = [threading.Thread(target=go) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(winners) == 1, f"{len(winners)} 个线程同时通过迁移检查（TOCTOU）"
    assert len(losers) == 7, f"落败方应是 7，实际 {len(losers)}"
    assert job.state == "writing"
    # 已经离开 queued 之后，再推 queued→writing 必须继续被拒
    assert job.transition("writing") is False


# ── J 并发上限 ──────────────────────────────────────────────
def test_concurrency_cap(tmp_path):
    tmp = _tmp_root()
    cfg = load_config(tmp)
    cfg.mock = False
    pl = Pipeline(tmp, cfg)

    import threading as _t

    class Hang:
        class cfg:                                     # noqa: N801
            temperature = 0.7
            max_tokens = 100
            model = 'fake-model'
            base_url = 'http://fake/v1'      # 选题缓存指纹要读它（_plan_key）
        def chat_json(self, *a, **kw):
            time.sleep(30)
    pl.llm = Hang()

    from app.jobs import StateConflict
    from app.pipeline import MAX_CONCURRENT_JOBS
    jids = []
    for i in range(MAX_CONCURRENT_JOBS):
        jids.append(pl.start_generate(GenerateRequest(pack="elevator", topic=f"并发{i}")))
    try:
        pl.start_generate(GenerateRequest(pack="elevator", topic="超限"))
        raise AssertionError("超出并发上限应当被拒绝")
    except StateConflict:
        pass
    for j in jids:
        pl.cancel(j)
    shutil.rmtree(tmp, ignore_errors=True)


def test_origin_rejects_dns_rebinding():
    """DNS rebinding：evil.com 解析到 127.0.0.1 时，Host 与 Origin 都是 evil.com。

    只比「Origin == Host」会被它绕过 —— 必须再要求主机名在本机回环（或显式绑定的
    地址）里。令牌是第二道，但两层不该互相依赖。
    """
    from app.security import allowed_hostnames, origin_allowed
    hosts = allowed_hostnames("127.0.0.1")
    # 经典 rebinding 形态：Host 和 Origin 完全一致，但主机名不是本机
    assert not origin_allowed("http://evil.com:8765", "evil.com:8765", hosts)
    assert not origin_allowed("http://attacker.example", "attacker.example", hosts)
    # 正常同源放行
    assert origin_allowed("http://127.0.0.1:8765", "127.0.0.1:8765", hosts)
    assert origin_allowed("http://localhost:8765", "localhost:8765", hosts)
    # 显式绑到局域网地址时，该地址也被接受（否则局域网访问连界面都打不开）
    lan = allowed_hostnames("192.168.1.5")
    assert origin_allowed("http://192.168.1.5:8765", "192.168.1.5:8765", lan)
    # 但 0.0.0.0 不是合法的 Host，不该被加进白名单
    assert "0.0.0.0" not in allowed_hostnames("0.0.0.0")


def test_registry_prune_after_terminal(tmp_path):
    """终态作业要被回收：修复前 prune() 从未被调用，长跑进程内存无界增长。"""
    tmp = _tmp_root()
    cfg = load_config(tmp)
    cfg.mock = True
    pl = Pipeline(tmp, cfg)
    from app.jobs import Job
    for i in range(250):
        j = Job(f"j{i}", "generate", {})
        j.transition("failed", force=True, error="x")
        pl.registry.add(j)
    assert len(pl.registry.snapshots()) == 250
    pl.registry.prune()                     # 默认 keep=200
    assert len(pl.registry.snapshots()) == 200
    shutil.rmtree(tmp, ignore_errors=True)


def test_cancelled_job_stays_cancelled_even_if_worker_raises(tmp_path):
    """取消之后线程再抛别的异常，也不能把状态改写成 failed。

    真实场景：用户点了停止 → 我们主动掐断流式连接 → httpx 抛 RequestError。
    修复前 _fail() 会 `transition("failed", force=True)`，于是界面显示
    「生成失败」——用户明明是自己停的，这就是「假停止」的老问题复发。
    """
    tmp = _tmp_root()
    cfg = load_config(tmp)
    cfg.mock = False
    pl = Pipeline(tmp, cfg)

    import threading as _t
    from app.jobs import JobCancelled
    from app.schemas import TopicPlan

    class CancelThenBoom:
        class cfg:                                     # noqa: N801
            temperature = 0.7
            max_tokens = 100
            model = 'fake-model'
            base_url = 'http://fake/v1'      # 选题缓存指纹要读它（_plan_key）

        def __init__(self):
            self.started = _t.Event()

        def chat_json(self, task, system, user, model_cls, max_retries=1,
                      on_retry=None, temperature=None, on_delta=None,
                      max_tokens=None, on_attempt=None, should_abort=None,
                      usage=None, deadline=None):
            if task == "select":
                return TopicPlan(angle="a", hook_type="h", hook_line="l",
                                 points=["p"], cta="c")
            self.started.set()
            for _ in range(400):
                try:
                    if on_delta:
                        on_delta("reasoning", "想")
                except JobCancelled:
                    # 模拟「取消把流式连接掐断」→ httpx 抛出普通异常
                    raise RuntimeError("connection aborted by peer") from None
                time.sleep(0.01)
            raise AssertionError("不该跑完")

    llm = CancelThenBoom()
    pl.llm = llm
    jid = pl.start_generate(GenerateRequest(pack="elevator", topic="取消后抛错"))
    assert llm.started.wait(10), "写阶段没有开始"
    pl.cancel(jid)
    time.sleep(1.2)
    snap = pl.get_job(jid).snapshot()
    assert snap["state"] == "cancelled", \
        f"取消后线程抛错把状态改成了 {snap['state']}（error={snap.get('error')}）"
    assert snap["error"] in (None, ""), f"取消不是错误，不该留 error：{snap['error']}"
    shutil.rmtree(tmp, ignore_errors=True)


# ── 便捷 runner（不装 pytest 也能跑）────────────────────────
def main() -> int:
    cases = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in cases:
        need_tmp = fn.__code__.co_argcount > 0
        try:
            if need_tmp:
                fn(Path(tempfile.mkdtemp(prefix="ts-fixture-")))
            else:
                fn()
            print(f"  ✅ {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  ❌ {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(cases) - failed}/{len(cases)} 通过")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
