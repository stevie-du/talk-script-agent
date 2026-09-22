# -*- coding: utf-8 -*-
"""服务端加固回归测试。

跑法：python tests/test_server_hardening.py   或   pytest tests/test_server_hardening.py

覆盖：
  1 配置不再是一次性闭包 —— 设置里保存的 Key/模型立刻生效（不用重启）。
  2 访问控制 —— 令牌必需、跨站 Origin 一律拒绝（修复前主动放行 null / file://）。
  3 落盘原子化 —— 写到一半被杀只剩半截文件的几种后果。
  4 历史索引自愈 —— 手工往 generated/ 拷一份产物，索引要能重建出来。
  5 建包失败不留半成品目录 —— 否则重试会被 409「已存在」挡死。
  6 导出技能默认不带 private/ —— 商业信息不外带。
  7 渲染层下发 CSP 且没有 'unsafe-inline' 后门（纵深防御，P2-5）。
  8 交付路径复审的五个缺陷：令牌经 env 而非 argv、private/ 不经 HTTP 读走、
    「标记已校对」不吃注释、base_url 的拒/警分工、行业包住在可写目录。
"""
import json
import re
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import load_config, save_config          # noqa: E402
from app.fileio import write_atomic                      # noqa: E402
from app.knowledge import Pack                           # noqa: E402
from app.server import create_app                        # noqa: E402

try:
    from fastapi.testclient import TestClient
except ImportError:                                       # pragma: no cover
    TestClient = None

TOKEN = "hardening-token"


def _tmp_root() -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="talkscript-hardening-"))
    shutil.copytree(ROOT / "packs", tmp / "packs")
    (tmp / "config.yaml").write_text(
        "llm:\n  api_key: ''\n  model: startup-model\n", encoding="utf-8")
    return tmp


def _tmp_root_mock() -> Path:
    """带 mock Key 的根目录：可以直接跑生成而不真的调模型。"""
    tmp = _tmp_root()
    (tmp / "config.yaml").write_text(
        "llm:\n  api_key: MOCK\n  model: mock-model\n", encoding="utf-8")
    return tmp


# TestClient 默认的 "testserver" 主机名会被同源判定拒绝（那是 DNS rebinding 的形态），
# 测试要像真实客户端一样走回环地址。
LOOPBACK = "http://127.0.0.1:8765"


def _client(tmp: Path):
    c = TestClient(create_app(tmp, token=TOKEN), base_url=LOOPBACK,
                   raise_server_exceptions=False)
    c.headers.update({"X-TalkScript-Token": TOKEN})
    return c


def _wait_done(c, jid: str, timeout=30.0):
    import time
    deadline = time.time() + timeout
    while time.time() < deadline:
        snap = c.get(f"/api/jobs/{jid}").json()
        if snap.get("state") in ("done", "failed", "cancelled"):
            return snap
        time.sleep(0.1)
    raise AssertionError(f"作业超时：{snap}")


def _wait_state(c, jid: str, want: str, timeout=30.0):
    import time
    deadline = time.time() + timeout
    while time.time() < deadline:
        snap = c.get(f"/api/jobs/{jid}").json()
        if snap.get("state") == want:
            return snap
        if snap.get("state") in ("done", "failed", "cancelled"):
            raise AssertionError(f"作业提前落到 {snap['state']}，没等到 {want}：{snap}")
        time.sleep(0.1)
    raise AssertionError(f"没等到 {want}：{snap}")


# ── 1 配置现读 ──────────────────────────────────────────────
def test_validation_errors_are_said_in_chinese():
    """422 的 detail 必须是人话：渲染层按约定把 detail 原样显示（api.js 的注释写着）。

    复现路径极普通 —— 新建行业包时业务描述只打三个字，或主题打一个字。修前界面会
    露出 pydantic 的英文原文「String should have at least 4 characters」，
    而中文界面上没有任何一个叫 `description` 的框给用户看。
    """
    tmp = _tmp_root_mock()
    try:
        c = _client(tmp)
        r = c.post("/api/packs/create", json={"industry": "猫咖", "description": "小店"})
        assert r.status_code == 422, r.text
        body = r.json()
        assert body["detail"] == "业务描述太短了，要至少 4 个字", body
        assert body["code"] == "field_invalid", body
        # 两个字段同时错：一条一句并列给出，不能只报第一个就吞掉第二个
        r2 = c.post("/api/generate",
                    json={"topic": "梯", "pack": "elevator", "duration": 999})
        assert r2.json()["detail"] == "主题太短了，要至少 2 个字；时长（秒）不能大于 600", r2.json()
        # schema 里 duration 是 float，界面上不该出现「600.0」这种小数尾巴
        assert "600.0" not in r2.json()["detail"]
        # 纯符号行业名：引擎在花钱之前 400，且这句话就是建包桩的口径（不许两本账）
        r3 = c.post("/api/packs/create",
                    json={"industry": "？？？", "description": "纯符号名字探针"})
        assert r3.status_code == 400, r3.text
        assert "可用作目录名" in r3.json()["detail"], r3.json()
        # 请求体整体不是合法 JSON：pydantic 把位置写在 loc 里（实测
        # ["body", 14] —— 第 14 个**字符**），它不是字段名。之前取末段当"哪个框"，
        # 于是界面显示「14 不是合法的 JSON（JSON decode error）」：
        # 一个不存在的框 + 一句英文，等于没说（第 10 轮复核 P2）。
        r4 = c.post("/api/generate", content=b'{"topic": "\xe6\xa2\xaf",,}',
                    headers={"content-type": "application/json"})
        assert r4.status_code == 422, r4.text
        d4 = r4.json()["detail"]
        assert "不是合法的 JSON" in d4, d4
        assert "JSON decode error" not in d4, f"又把英文原话贴给了用户：{d4}"
        assert not re.match(r"^\d+", d4), f"拿字符偏移量当了字段名：{d4}"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_config_fresh(tmp_path=None):
    tmp = _tmp_root()
    c = _client(tmp)

    before = c.get("/api/meta")
    assert before.status_code == 200, before.status_code
    assert before.json()["model"] == "startup-model", before.json()
    assert before.json()["has_api_key"] is False, before.json()

    # 没有任何 Key 时，建包必须先被拦下
    # （P1-43 后 create_pack 在 pipeline 里、且由后台作业调用 ——
    #   「拦在开始之前」这件事现在只能钉 start_packgen 有没有被调到）
    from app.pipeline import Pipeline
    started: list[str] = []
    with patch.object(Pipeline, "start_packgen",
                      lambda self, industry, description: started.append(industry) or "job-fake"):
        denied = c.post("/api/packs/create",
                        json={"industry": "假体陀机", "description": "测试用行业描述"})
    assert denied.status_code == 400, (denied.status_code, denied.text)
    assert started == [], "没配 Key 也应该已经把作业起起来了 —— _require_model 形同虚设"
    # 2026-09-17 文案调整：从「未配置模型 API Key」改成「当前模型还没配 API Key，
    # 请在「设置 → 模型接口」里填写」—— 多了「当前模型」这个主语（明确改哪一条）
    # 与下一步动作。断言只认「API Key」这个关键信息，不锁整句。
    assert "API Key" in denied.json()["detail"], denied.text

    # 用户在设置页保存 Key 与模型（这一步之后不重启）
    save_config(tmp, {"api_key": "sk-after-startup", "model": "saved-model"})

    after = c.get("/api/meta")
    assert after.json()["model"] == "saved-model", after.json()
    assert after.json()["has_api_key"] is True, after.json()

    with patch.object(Pipeline, "start_packgen",
                      lambda self, industry, description: started.append(industry) or "job-fake"):
        allowed = c.post("/api/packs/create",
                         json={"industry": "假体陀机", "description": "测试用行业描述"})
    assert allowed.status_code == 200, (allowed.status_code, allowed.text)
    assert allowed.json() == {"job_id": "job-fake"}, allowed.json()
    # 存完 Key 再建包：作业真的被起起来了（不是又被 _require_model 拦在门外）
    assert started == ["假体陀机"], started
    shutil.rmtree(tmp, ignore_errors=True)


# ── 2 访问控制 ──────────────────────────────────────────────
def test_access_control(tmp_path=None):
    tmp = _tmp_root()
    app = create_app(tmp, token=TOKEN)
    # 先建一个不带令牌头的客户端，专门验 401
    anon = TestClient(app, base_url=LOOPBACK, raise_server_exceptions=False)
    # health 免鉴权（主进程开窗前轮询）
    assert anon.get("/api/health").status_code == 200
    # 令牌缺失 / 错误
    assert anon.get("/api/history").status_code == 401
    assert anon.get("/api/history", headers={"X-TalkScript-Token": "x"}).status_code == 401

    # FastAPI 默认挂 /docs、/redoc、/openapi.json，而中间件的令牌那道只查 `/api/*`
    # —— 三个文档端点因此一直在鉴权之外。现在必须拿不到。
    # 断"401 或 404"而不是死盯 404：将来若重新开放文档但纳入鉴权（401）也算合格。
    for doc_path in ["/docs", "/redoc", "/openapi.json"]:
        r = anon.get(doc_path)
        assert r.status_code in (401, 404), (doc_path, r.status_code, r.text[:200])

    c = TestClient(app, base_url=LOOPBACK, raise_server_exceptions=False)
    c.headers.update({"X-TalkScript-Token": TOKEN})

    # 跨站一律 403 且不回 CORS 头。修复前这一组（尤其 null / file://）是**放行**的。
    for origin in ["https://evil.example", "http://evil.example", "null",
                   "file://", "chrome-extension://abc",
                   "http://talkscript.attacker.io:8765", "http://192.168.0.9:8765",
                   # DNS rebinding 形态：Host 与 Origin 完全一致，但主机名不是本机
                   "http://evil.com:8765"]:
        r = c.get("/api/history", headers={"Origin": origin})
        assert r.status_code == 403, (origin, r.status_code, r.text)
        assert "access-control-allow-origin" not in {k.lower() for k in r.headers}, origin

    # 写操作的跨站同样被拦
    r = c.post("/api/generate", headers={"Origin": "https://evil.example"},
               json={"pack": "elevator", "topic": "测试"})
    assert r.status_code == 403, (r.status_code, r.text)

    # 同源放行（Origin 与请求 Host 一致，且主机名是本机回环）
    r = c.get("/api/meta", headers={"Origin": LOOPBACK})
    assert r.status_code == 200, r.status_code
    # 非浏览器客户端（无 Origin）放行
    assert c.get("/api/meta").status_code == 200
    shutil.rmtree(tmp, ignore_errors=True)


# ── 3 落盘原子性 ────────────────────────────────────────────
def test_atomic_write(tmp_path=None):
    tmp = Path(tempfile.mkdtemp(prefix="ts-atomic-"))
    target = tmp / "result.json"
    original = json.dumps({"id": "old-version", "pack": "elevator"}, ensure_ascii=False)
    target.write_text(original, encoding="utf-8")

    seen = {}

    def killed_replace(src, dst, **kw):
        # 模拟进程在「内容已写进临时文件、尚未换上去」时被杀：
        # 此刻目标文件必须还是完整可读的旧版本。
        seen["target_before_replace"] = target.read_text(encoding="utf-8")
        seen["tmp_path"] = str(src)
        seen["tmp_is_sibling"] = Path(src).parent == Path(dst).parent
        raise KeyboardInterrupt("simulated kill")

    with patch("app.fileio.os.replace", side_effect=killed_replace):
        try:
            write_atomic(target, json.dumps({"id": "new-version"}, ensure_ascii=False))
        except KeyboardInterrupt:
            pass

    assert seen, "没有走到 replace —— 断言没打在真正的写入路径上"
    assert seen["target_before_replace"] == original, seen["target_before_replace"][:80]
    assert seen["tmp_is_sibling"] is True, seen        # 临时文件必须同目录才 rename 得动
    assert seen["tmp_path"] != str(target), seen
    assert target.read_text(encoding="utf-8") == original
    json.loads(target.read_text(encoding="utf-8"))
    assert list(tmp.glob("result.json.tmp-*")) == [], list(tmp.glob("*"))

    new_text = json.dumps({"id": "new-version", "sections": [{"text": "x"}]},
                          ensure_ascii=False)
    write_atomic(target, new_text)
    assert json.loads(target.read_text(encoding="utf-8"))["id"] == "new-version"
    assert list(tmp.glob("*.tmp-*")) == [], list(tmp.glob("*"))
    shutil.rmtree(tmp, ignore_errors=True)


def test_config_roundtrip(tmp_path=None):
    """save_config 走原子写：内容完整，且不会在根目录丢垃圾。"""
    tmp = Path(tempfile.mkdtemp(prefix="ts-rt-"))
    save_config(tmp, {"api_key": "sk-a", "model": "m-a"})
    save_config(tmp, {"model": "m-b"})
    cfg = load_config(tmp)
    assert cfg.llm.model == "m-b", cfg.llm.model
    assert cfg.llm.api_key == "sk-a", cfg.llm.api_key       # 空值不覆盖语义要保留
    assert list(tmp.glob("config.yaml.tmp-*")) == [], list(tmp.glob("*"))
    shutil.rmtree(tmp, ignore_errors=True)


# ── 4 历史索引自愈 ──────────────────────────────────────────
def test_index_rebuilds_when_disk_changes(tmp_path=None):
    tmp = _tmp_root_mock()
    c = _client(tmp)
    jid = c.post("/api/generate", json={"pack": "elevator", "topic": "索引自愈"}).json()["job_id"]
    assert _wait_done(c, jid)["state"] == "done"

    # 模拟：索引被删掉（或换了台机器拷过来），磁盘上还有产物
    (tmp / "generated" / "index.json").unlink()
    items = c.get("/api/history").json()
    assert any(x["id"] == jid for x in items), "索引丢了应从磁盘重建"
    assert (tmp / "generated" / "index.json").exists()

    # 模拟：手工往 generated/ 里塞一条产物，索引数量对不上 → 重建后应包含它
    src = next((tmp / "generated").glob(f"*/{jid}"))
    dst = src.parent / "20260913-000000-abcdef"
    shutil.copytree(src, dst)
    r = json.loads((dst / "result.json").read_text(encoding="utf-8"))
    r["id"] = "20260913-000000-abcdef"
    (dst / "result.json").write_text(json.dumps(r, ensure_ascii=False), encoding="utf-8")
    items = c.get("/api/history").json()
    assert any(x["id"] == "20260913-000000-abcdef" for x in items), items
    shutil.rmtree(tmp, ignore_errors=True)


def test_index_cache_survives_inflight_job(tmp_path=None):
    """有作业在跑（目录已建、还没落盘）时，`history()` **不得**重建索引。

    修复前 `_count_on_disk()` 数的是**目录**数，而 `job_dir()` 在 `start_generate`
    里就 `mkdir` —— 作业一开工就有一个空目录，而索引只收录已落盘的作业。
    于是只要有一个作业在跑，判据就**恒不相等**，每次 `history()` 都走全量重建：
    glob + 解析所有 result.json/job.json + 重写 index.json。

    而前端在有未结束会话时每 3 秒轮询一次 `/api/history`（`sessions.js`），
    所以整个生成期间就是每 3 秒一次 O(N) 解析 + 一次磁盘写。
    实测 400 条记录时单次 **115 ms**，重建次数 == 调用次数。

    这条断言盯的就是「判据口径」：空目录不该被算作一条记录。
    """
    from app.store import ArtifactStore

    tmp = _tmp_root_mock()
    c = _client(tmp)
    for i in range(3):
        jid = c.post("/api/generate",
                     json={"pack": "elevator", "topic": f"在跑作业{i}"}).json()["job_id"]
        assert _wait_done(c, jid)["state"] == "done"

    st = ArtifactStore(tmp, data_dir=tmp)
    assert len(st.history()) == 3, st.history()

    rebuilds = []
    orig = ArtifactStore._rebuild

    def counting(self):
        rebuilds.append(1)
        return orig(self)

    with patch.object(ArtifactStore, "_rebuild", counting):
        # 稳定态：一次都不该重建
        st.history()
        assert rebuilds == [], "稳定态就重建了，缓存等于没做"

        # 模拟 start_generate：目录建出来，但还没有任何落盘文件
        inflight = st.job_dir("20260915-120000-aaaaaa", "2026-09-15T12:00:00")
        for _ in range(5):
            st.history()
        assert rebuilds == [], (
            f"有作业在跑时空转重建了 {len(rebuilds)} 次（5 次调用）—— "
            "索引缓存失效，生成期间每 3 秒全量解析一次")

        # 落一个 job.json（失败/取消/待确认的作业会走 write_job）——
        # 此刻磁盘上确实多了一条「已落盘」记录，允许重建，但之后必须重新收敛
        st.write_job({"id": "20260915-120000-aaaaaa",
                      "created_at": "2026-09-15T12:00:00",
                      "state": "failed", "error": "模拟失败",
                      "params": {"pack": "elevator", "topic": "在跑作业"}},
                     inflight)
        st.history()
        assert len(st.history()) == 4, "落盘后应被收进索引"
        rebuilds.clear()
        for _ in range(3):
            st.history()
        assert rebuilds == [], f"落盘收敛后又开始空转重建 {len(rebuilds)} 次"

    # 功能面不能为了性能退步：删掉那条记录后仍要能自愈
    assert st.delete("20260915-120000-aaaaaa") == "ok"
    assert len(st.history()) == 3
    shutil.rmtree(tmp, ignore_errors=True)


def test_delete_removes_all_duplicate_dirs(tmp_path=None):
    """跨零点分裂出的两个目录必须一起删，否则记录会「复活」。"""
    tmp = _tmp_root_mock()
    c = _client(tmp)
    jid = c.post("/api/generate", json={"pack": "elevator", "topic": "重复目录"}).json()["job_id"]
    assert _wait_done(c, jid)["state"] == "done"

    src = next((tmp / "generated").glob(f"*/{jid}"))
    # 造一个「另一个日期目录下的同一作业」——正是修复前 now()/created_at 分裂的形态
    other = (tmp / "generated" / "20200101" / jid)
    other.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src / "result.json", other / "result.json")

    assert len(list((tmp / "generated").glob(f"*/{jid}"))) == 2
    assert c.delete(f"/api/history/{jid}").status_code == 200
    assert list((tmp / "generated").glob(f"*/{jid}")) == [], "只删了一个目录，记录会复活"
    assert not any(x["id"] == jid for x in c.get("/api/history").json())
    shutil.rmtree(tmp, ignore_errors=True)


def test_done_implies_artifacts_on_disk(tmp_path=None):
    """看到 done 的那一刻，产物与历史索引必须都已在盘上。

    这条防的是「先置 done、后落盘」：状态一变，前端就去回读记录，
    而历史索引其实还没写 —— 记录凭空消失，且没有任何报错。
    """
    tmp = _tmp_root_mock()
    c = _client(tmp)
    jid = c.post("/api/generate", json={"pack": "elevator", "topic": "落盘顺序"}).json()["job_id"]
    assert _wait_done(c, jid)["state"] == "done"

    d = next((tmp / "generated").glob(f"*/{jid}"))
    assert (d / "result.json").exists()
    assert (tmp / "generated" / "index.json").exists(), "索引落盘晚于状态，前端会查不到"
    assert any(x["id"] == jid for x in c.get("/api/history").json())
    shutil.rmtree(tmp, ignore_errors=True)


def test_delete_running_job_does_not_resurrect(tmp_path=None):
    """生成途中删掉记录，后台线程的落盘不能把目录再建回来。

    修复前 `write_atomic` 里的 mkdir(parents=True) 会把删掉的目录重新建出来，
    用户刚删掉的记录当场复活。现在靠「墓碑 + 取消作业」两道一起拦。
    """
    tmp = _tmp_root_mock()
    c = _client(tmp)
    import app.store as store

    real_render = store.render_script_md
    entered = threading.Event()

    def slow_render(result):
        entered.set()               # 已进入落盘阶段
        time.sleep(0.8)             # 放大窗口，让删除有机会插进来
        return real_render(result)

    store.render_script_md = slow_render
    try:
        jid = c.post("/api/generate",
                     json={"pack": "elevator", "topic": "边跑边删"}).json()["job_id"]
        assert entered.wait(15), "作业没走到落盘阶段，断言没打在真正的窗口上"
        assert c.delete(f"/api/history/{jid}").status_code == 200
        time.sleep(1.5)             # 等后台线程彻底收尾
        assert list((tmp / "generated").glob(f"*/{jid}")) == [], "删除后产物又被建回来了"
        assert not any(x["id"] == jid for x in c.get("/api/history").json())
    finally:
        store.render_script_md = real_render
        shutil.rmtree(tmp, ignore_errors=True)


def test_write_job_respects_tombstone(tmp_path=None):
    """删除之后连 job.json 也不该再落盘。

    `write_atomic` 会 `mkdir(parents=True)`，任何一次落盘都能把删掉的目录
    重建出来，所以 `write_result` 与 `write_job` 必须**都**查墓碑 ——
    只拦一个等于没拦。
    """
    tmp = _tmp_root_mock()
    c = _client(tmp)
    jid = c.post("/api/generate", json={"pack": "elevator", "topic": "墓碑"}).json()["job_id"]
    assert _wait_done(c, jid)["state"] == "done"

    from app.store import ArtifactStore
    st = ArtifactStore(tmp)
    d = next((tmp / "generated").glob(f"*/{jid}"))
    assert st.delete(jid) == "ok"
    # 模拟「记录已删、后台线程还在收尾」
    assert st.write_job({"id": jid, "state": "cancelled", "params": {}}, d) is False
    assert not d.exists(), "已删除的作业又把目录建回来了"
    shutil.rmtree(tmp, ignore_errors=True)


def test_delete_reports_failure_instead_of_lying(tmp_path=None):
    """删不掉要如实报 500 —— 假装成功的代价是用户刷新后看到记录自己回来。"""
    tmp = _tmp_root_mock()
    c = _client(tmp)
    jid = c.post("/api/generate", json={"pack": "elevator", "topic": "删不掉"}).json()["job_id"]
    assert _wait_done(c, jid)["state"] == "done"

    import app.store as store
    real = store.rmtree_resilient
    store.rmtree_resilient = lambda p: False
    try:
        r = c.delete(f"/api/history/{jid}")
        assert r.status_code == 500, (r.status_code, r.text)
    finally:
        store.rmtree_resilient = real
        shutil.rmtree(tmp, ignore_errors=True)


# ── 5 建包失败不留半成品 ────────────────────────────────────
class Partial:
    """模型返回了结构，但落盘阶段会炸。"""
    display_name = "半成品行业"
    segments = ["A", "B"]
    audiences = ["X"]
    personas = ["P"]
    topics = []
    audience_details = []
    ideas = []
    redlines = []
    banwords_extra_hard = []
    banwords_extra_soft = []
    verify_list = []


def test_packgen_failure_cleans_up(tmp_path=None):
    tmp = _tmp_root()
    from app import packgen

    real_write = packgen.write_atomic

    def boom(path, text, *a, **kw):
        if str(path).endswith("pack.yaml"):
            raise OSError("磁盘满了")
        return real_write(path, text, *a, **kw)

    with patch.object(packgen, "write_atomic", side_effect=boom):
        try:
            packgen.create_pack(tmp, _FakeLLM(Partial), "半成品行业", "测试描述文本")
            raise AssertionError("应当抛错")
        except OSError:
            pass

    slug = packgen.slugify("半成品行业")
    assert not (tmp / "packs" / slug).exists(), "失败后残留了半成品目录，重试会被 409 挡死"
    shutil.rmtree(tmp, ignore_errors=True)


def test_packgen_cleanup_retries_when_locked(tmp_path=None):
    """清理半成品时若文件被瞬时占用，要重试，而不是静默留下目录。

    留下来的后果：下次建同名行业包被 `FileExistsError` 挡成 409，
    用户只能自己去文件管理器里删 —— 而且我们连日志都没有。
    """
    tmp = _tmp_root()
    from app import packgen

    real_rmtree = shutil.rmtree
    calls = {"n": 0}

    def flaky(path, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("文件被占用（模拟杀毒软件扫描）")
        return real_rmtree(path, **kw)

    real_write = packgen.write_atomic

    def boom(path, text, *a, **kw):
        if str(path).endswith("pack.yaml"):
            raise OSError("磁盘满了")
        return real_write(path, text, *a, **kw)

    with patch.object(shutil, "rmtree", side_effect=flaky), \
         patch.object(packgen, "write_atomic", side_effect=boom):
        try:
            packgen.create_pack(tmp, _FakeLLM(Partial), "半成品行业", "测试描述文本")
            raise AssertionError("应当抛错")
        except OSError:
            pass

    slug = packgen.slugify("半成品行业")
    assert calls["n"] >= 2, f"没走重试（rmtree 只被调用 {calls['n']} 次）"
    assert not (tmp / "packs" / slug).exists(), "重试后仍留下半成品目录"
    shutil.rmtree(tmp, ignore_errors=True)


def test_packgen_empty_arrays_rejected(tmp_path=None):
    """模型返回空数组时要给出可读错误，而不是 IndexError → 500。"""
    tmp = _tmp_root()
    from app import packgen

    class Empty:
        display_name = "空行业"
        segments = []
        audiences = []
        personas = []
        topics = []
        audience_details = []
        ideas = []
        redlines = []
        banwords_extra_hard = []
        banwords_extra_soft = []
        verify_list = []

    try:
        packgen.create_pack(tmp, _FakeLLM(Empty), "空行业", "测试描述文本")
        raise AssertionError("应当抛 ValueError")
    except ValueError as e:
        assert "不完整" in str(e), str(e)
    assert not (tmp / "packs" / packgen.slugify("空行业")).exists()
    shutil.rmtree(tmp, ignore_errors=True)


class _FakeLLM:
    def __init__(self, out):
        self._out = out

    def chat_json(self, task, system, user, model_cls, **kw):
        return self._out


# ── 6 导出默认不带 private ──────────────────────────────────
def test_export_excludes_private_by_default(tmp_path=None):
    tmp = _tmp_root()
    from app.export_skill import export_agent_skill

    # 先确保模板包里有 private 目录且有内容
    priv = tmp / "packs" / "elevator" / "private"
    assert priv.exists() and any(priv.glob("*.yaml"))

    out = export_agent_skill(tmp, "elevator")
    assert not (Path(out["path"]) / "private").exists(), "默认导出不该带 private/"
    assert out["include_private"] is False
    assert (Path(out["path"]) / "SKILL.md").exists()
    assert (Path(out["path"]) / "tools" / "check.py").exists()

    out2 = export_agent_skill(tmp, "elevator",
                              out_dir=tmp / "with-priv", include_private=True)
    assert (Path(out2["path"]) / "private").exists(), "显式开启时才应带 private/"
    shutil.rmtree(tmp, ignore_errors=True)


def test_export_rejects_path_traversal(tmp_path=None):
    tmp = _tmp_root()
    from app.export_skill import export_agent_skill
    from app.knowledge import PackError
    try:
        export_agent_skill(tmp, "../..")
        raise AssertionError("穿越路径应当被拒绝")
    except PackError:
        pass
    shutil.rmtree(tmp, ignore_errors=True)


def test_reset_restores_defaults(tmp_path=None):
    """base_url / model 填错之后要能回到默认 —— 留空保存是无效操作。

    `set_config` 会过滤空串（防手滑清空），副作用是一旦填错就再也改不回去，
    用户只能手工改 config.yaml。「恢复默认」因此必须是显式动作。
    """
    from app.config import DEFAULT_CONFIG, load_config

    tmp = _tmp_root()
    c = _client(tmp)
    assert c.post("/api/config", json={"base_url": "https://wrong.example/v9",
                                       "model": "wrong-model"}).status_code == 200
    assert load_config(tmp).llm.base_url == "https://wrong.example/v9"

    # 留空保存不会清空（空串被过滤）—— 这正是需要 reset 接口的原因
    assert c.post("/api/config", json={"base_url": "", "model": ""}).status_code == 200
    assert load_config(tmp).llm.base_url == "https://wrong.example/v9", "空串不该覆盖"

    r = c.post("/api/config/reset", json={"fields": ["base_url", "model"]})
    assert r.status_code == 200, (r.status_code, r.text)
    cfg = load_config(tmp)
    assert cfg.llm.base_url == DEFAULT_CONFIG["llm"]["base_url"], cfg.llm.base_url
    assert cfg.llm.model == DEFAULT_CONFIG["llm"]["model"], cfg.llm.model
    # 重置不能顺手把 Key 弄丢
    assert cfg.llm.api_key == "", cfg.llm.api_key

    # api_key 不在可重置名单里：清空密钥不该是一个顺手的动作
    assert c.post("/api/config/reset", json={"fields": ["api_key"]}).status_code == 400
    shutil.rmtree(tmp, ignore_errors=True)


def test_numeric_settings_editable_and_bounded(tmp_path=None):
    """重试 / 超时 / 输出预算要能改，且越界要当场报错。

    边界必须在写入前卡：这些项的合法区间都不包含 0（timeout ≥ 5、max_tokens ≥ 256），
    而 `load_config` 的历史写法是 `llm.get(x) or 默认值`，写进去的 0 会被悄悄换成
    默认值 —— 界面显示「已保存」、实际值却不是用户填的那个，比直接拒绝更难查。
    （`load_config` 现已改成「只有键缺失才取默认」，见
    `test_temperature_zero_roundtrip`；这里的区间校验仍然要保留，
    因为它还负责挡住负数与超上限这类**非 0** 的非法值。）
    """
    from app.config import load_config

    tmp = _tmp_root()
    c = _client(tmp)
    r = c.post("/api/config", json={"retries": 5, "timeout": 30, "max_tokens": 8000})
    assert r.status_code == 200, (r.status_code, r.text)
    cfg = load_config(tmp)
    assert (cfg.llm.retries, cfg.llm.timeout, cfg.llm.max_tokens) == (5, 30.0, 8000), cfg.llm

    # 越界一律 400，不能写进配置
    for bad in ({"timeout": 0}, {"timeout": 99999}, {"retries": 99}, {"max_tokens": 1}):
        r = c.post("/api/config", json=bad)
        assert r.status_code == 400, (bad, r.status_code, r.text)
    assert load_config(tmp).llm.timeout == 30.0, "越界值不该落盘"

    # 留空（不传 / None）表示不改
    assert c.post("/api/config", json={"retries": None}).status_code == 200
    assert load_config(tmp).llm.retries == 5
    shutil.rmtree(tmp, ignore_errors=True)


def test_temperature_zero_roundtrip(tmp_path=None):
    """`temperature: 0.0` 必须原样读回 0.0。

    修复前 `load_config` 写的是 `float(llm.get("temperature", 0.7) or 0.7)` ——
    `0.0` 是 falsy，于是「把温度调到 0 求确定性输出」变成**静默无效**：
    界面提示「已保存，下次生成即生效」、config.yaml 里也确实写着 0.0，
    但每次生成实际仍用 0.7。不报错、不可见、与用户意图相反。

    这条断言要同时守住三件事：
      1. 接口能存进 0.0（`set_config` 的 `v not in ("", None)` 过滤不能把 0.0 吃掉）；
      2. `load_config` 能读回 0.0（不能用真值判断取默认）；
      3. 0.0 是合法边界值，不能像 timeout=0 那样被 400 挡掉。
    """
    import yaml
    from app.config import config_path, load_config

    # ── 1) 走接口：0.0 既不被过滤，也不被区间校验拒绝 ──
    tmp = _tmp_root()
    c = _client(tmp)
    r = c.post("/api/config", json={"temperature": 0.0})
    assert r.status_code == 200, (r.status_code, r.text)
    raw = yaml.safe_load(config_path(tmp, tmp).read_text(encoding="utf-8"))
    assert raw["llm"]["temperature"] == 0.0, f"0.0 没写进 config.yaml：{raw}"
    got = load_config(tmp).llm.temperature
    assert got == 0.0, f"temperature 被静默改写成了 {got}（期望 0.0）"

    # 上边界同样要能存能读；越界仍要 400
    assert c.post("/api/config", json={"temperature": 2.0}).status_code == 200
    assert load_config(tmp).llm.temperature == 2.0
    assert c.post("/api/config", json={"temperature": 2.01}).status_code == 400
    assert c.post("/api/config", json={"temperature": -0.01}).status_code == 400
    shutil.rmtree(tmp, ignore_errors=True)

    # ── 2) 直接写盘：绕开接口，单独盯 load_config 的取值口径 ──
    tmp2 = Path(tempfile.mkdtemp(prefix="talkscript-temp-"))
    config_path(tmp2, tmp2).write_text(
        yaml.safe_dump({"llm": {"temperature": 0.0, "retries": 0}}, allow_unicode=True),
        encoding="utf-8")
    cfg = load_config(tmp2, tmp2)
    assert cfg.llm.temperature == 0.0, cfg.llm.temperature
    assert cfg.llm.retries == 0, cfg.llm.retries
    shutil.rmtree(tmp2, ignore_errors=True)

    # ── 3) 键真的缺失时才取默认（别把修复改成「永不取默认」）──
    tmp3 = Path(tempfile.mkdtemp(prefix="talkscript-temp-"))
    config_path(tmp3, tmp3).write_text(
        yaml.safe_dump({"llm": {"model": "m"}}, allow_unicode=True), encoding="utf-8")
    cfg3 = load_config(tmp3, tmp3)
    assert cfg3.llm.temperature == 0.7, f"缺失时应取默认 0.7，实际 {cfg3.llm.temperature}"
    assert cfg3.llm.timeout == 180.0 and cfg3.llm.retries == 2
    shutil.rmtree(tmp3, ignore_errors=True)


def test_undraft_clears_flag_and_keeps_rest(tmp_path=None):
    """草稿转正：此前**零测试**，而它是个写操作。

    除了要把 draft 置 false，还得保证不能顺手把 pack.yaml 里别的内容弄丢
    （实现是先 load 再改一个键再 dump，理论上安全，但没有测试兜着）。
    """
    import yaml

    tmp = _tmp_root()
    c = _client(tmp)
    y = tmp / "packs" / "elevator" / "pack.yaml"
    raw = yaml.safe_load(y.read_text(encoding="utf-8")) or {}
    assert isinstance(raw, dict)
    raw["draft"] = True
    write_atomic(y, yaml.safe_dump(raw, allow_unicode=True, sort_keys=False))
    assert c.get("/api/packs/elevator").json()["draft"] is True

    r = c.post("/api/packs/elevator/undraft")
    assert r.status_code == 200, (r.status_code, r.text)
    assert r.json()["draft"] is False
    assert c.get("/api/packs/elevator").json()["draft"] is False, "转正没生效"

    after = yaml.safe_load(y.read_text(encoding="utf-8"))
    for k, v in raw.items():
        if k == "draft":
            continue
        assert after.get(k) == v, f"转正把 {k} 弄丢了"

    # 幂等：已经是正式包时再调一次不该报错
    assert c.post("/api/packs/elevator/undraft").status_code == 200
    # 不存在的包 → 404；非法名 → 400
    assert c.post("/api/packs/nope/undraft").status_code == 404
    assert c.post("/api/packs/..%2f..%2fetc/undraft").status_code in (400, 404)
    shutil.rmtree(tmp, ignore_errors=True)


def test_pack_file_read_is_guarded(tmp_path=None):
    """知识库只读查看器：能读包内文件，但读不到包外的东西。

    最要紧的一条是穿越 —— `rel=../../config.yaml` 会把含明文 API Key 的
    配置文件读出去。
    """
    tmp = _tmp_root()
    c = _client(tmp)

    ok = c.get("/api/packs/elevator/file", params={"rel": "knowledge/topics.md"})
    assert ok.status_code == 200, (ok.status_code, ok.text)
    body = ok.json()
    assert body["rel"] == "knowledge/topics.md" and body["text"], body
    assert body["size"] == len(body["text"].encode("utf-8")) or body["size"] > 0

    # 穿越一律拿不到（config.yaml 里有明文 Key）
    for bad in ("../../config.yaml", "../config.yaml", "..\\config.yaml",
                "/etc/passwd", "knowledge/../../config.yaml"):
        r = c.get("/api/packs/elevator/file", params={"rel": bad})
        assert r.status_code in (400, 404), (bad, r.status_code, r.text)
        assert "api_key" not in r.text.lower(), f"配置被读出去了：{bad}"

    # 不存在的普通文件 / 非白名单类型
    assert c.get("/api/packs/elevator/file",
                 params={"rel": "nope.md"}).status_code == 404
    assert c.get("/api/packs/elevator/file",
                 params={"rel": "knowledge/topics.png"}).status_code == 404
    shutil.rmtree(tmp, ignore_errors=True)


def test_pack_detail_rejects_traversal(tmp_path=None):
    """GET /api/packs/{name} 也必须挡穿越 —— 它是唯一漏了白名单的那个。

    修复前 `get_pack` 没有 `_safe_name`：`%2e%2e` 被解码成 `..`（单段，路由匹配得上，
    不像 `%2f` 会被路由挡掉），于是 `Pack(root, "..")` 命中 `root/pack.yaml`，
    接口回 200，并附带 `base.rglob("*")` 的**整棵目录树清单** ——
    含 `config.yaml` 与各行业包 `private/` 下的文件名与体积。
    实测：25 条清单 + 一个包外 pack.yaml 的内容。

    这里同时守住「包外没有 pack.yaml」时也不能变成目录列举：
    只看状态码不够，还要确认响应里没有泄漏清单。
    """
    tmp = _tmp_root()
    c = _client(tmp)
    # 在 packs/ 的上一级放一个 pack.yaml，作为穿越的着陆点
    (tmp / "pack.yaml").write_text(
        "name: LEAKED\ndisplay_name: 包外的包\ndraft: false\nparams: {}\n",
        encoding="utf-8")

    for bad in ("%2e%2e", ".%2e", "%2e%2e%2f", "..%2f.."):
        r = c.get(f"/api/packs/{bad}")
        assert r.status_code in (400, 404), (bad, r.status_code, r.text)
        assert "LEAKED" not in r.text, f"包外 pack.yaml 被读出去了：{bad}"
        assert "api_key" not in r.text.lower(), f"配置被列举了：{bad}"

    # 正常包不受影响
    ok = c.get("/api/packs/elevator")
    assert ok.status_code == 200, (ok.status_code, ok.text)
    assert ok.json()["name"] == "elevator"
    assert ok.json()["files"], "文件清单不该为空"
    shutil.rmtree(tmp, ignore_errors=True)


def test_generate_pack_name_cannot_escape_packs_dir(tmp_path=None):
    """`POST /api/generate` 的包名要过校验 —— 它是唯一走**请求体**取包的入口。

    其余四个按名字取包的端点（pack_detail / pack_file / undraft / export-skill）
    都调了 `_safe_name`，只有这个漏了：`Pack(root, "../../..")` 会解析到 packs
    **之外**，于是任何放得下 pack.yaml 的目录都能被当成行业包加载 ——
    它的 `private/*.yaml` 会被当私有资料注入提示词。而 404 与 409 的差值本身
    就是一个「这个路径上有没有 pack.yaml」的探测 oracle。

    ⚠ 这条同时验**不变式**：绕过 HTTP 直接构造 `Pack` 也必须进不去。
    只测端点的话，下一个忘了调 `_safe_name` 的入口就又漏了 ——
    端点那层给的是 400 的语义，「包目录不会越界」才是底线。
    """
    from app.knowledge import Pack, PackError

    tmp = _tmp_root()
    # 穿越着陆点：packs 之外一个看起来很像行业包的目录
    (tmp / "pack.yaml").write_text(
        "name: OUTSIDE\ndisplay_name: 包外的包\nparams: {}\n", encoding="utf-8")
    c = _client(tmp)

    for bad in ("../../..", "..", "..\\..", "elevator/..", "/etc"):
        r = c.post("/api/generate", json={"pack": bad, "topic": "穿越"})
        assert r.status_code == 400, (bad, r.status_code, r.text)
        assert "名称不合法" in r.json()["detail"], (bad, r.text)
        # 泄漏底线：不能把包外目录当成包回出去
        assert "OUTSIDE" not in r.text and "包外的包" not in r.text, (bad, r.text)

    # 不变式：不经过 HTTP 也一样拦得住
    for bad in ("../../..", "..", "..\\..", "elevator/.."):
        try:
            Pack(tmp, bad)
        except PackError:
            pass
        else:
            raise AssertionError(f"Pack 越界成功：{bad!r}")

    # 正常包不受影响（用带 mock Key 的根，生成不会真去调模型）
    mroot = _tmp_root_mock()
    mc = _client(mroot)
    ok = mc.post("/api/generate", json={"pack": "elevator", "topic": "正常"})
    assert ok.status_code == 200, (ok.status_code, ok.text)
    assert ok.json()["job_id"]
    shutil.rmtree(tmp, ignore_errors=True)
    shutil.rmtree(mroot, ignore_errors=True)


# ── 7 版本号单一来源 ────────────────────────────────────────
def test_version_single_source(tmp_path=None):
    """引擎报出的版本必须与 desktop/package.json 一致。

    版本曾经三处各写一遍（server.py / package.json / 渲染层 __ts），
    漏改一处的后果是「安装包 0.2.0、引擎自报 0.3.0」，而没有任何环节会报错。
    """
    from app.server import FALLBACK_VERSION, read_version

    pkg = json.loads((ROOT / "desktop" / "package.json").read_text(encoding="utf-8"))
    # 注意：这里**不能**拿 read_version(ROOT) 去比 package.json —— 两者读同一个文件，
    # 无论文件改成什么都相等，是同义反复。真正要防的是「读取链路断了、悄悄退回兜底值」。
    got = read_version(ROOT)
    assert got != FALLBACK_VERSION, (
        "没读到 desktop/package.json，悄悄退回了兜底值 —— 引擎会自报 0.0.0-dev")
    assert got == pkg["version"] and re.match(r"^\d+\.\d+\.\d+", got), got
    # 读不到时退回兜底值（打包版正是靠 Electron 传参兜住这个缺口）
    assert read_version(Path(tempfile.mkdtemp())) == FALLBACK_VERSION

    # 打包版路径：Electron 显式传入，优先级高于文件
    tmp = _tmp_root()
    c = TestClient(create_app(tmp, token=TOKEN, version="9.9.9"),
                   base_url=LOOPBACK, raise_server_exceptions=False)
    c.headers.update({"X-TalkScript-Token": TOKEN})
    assert c.get("/api/health").json()["version"] == "9.9.9", "显式传入的版本应优先"
    shutil.rmtree(tmp, ignore_errors=True)


# ── 7 渲染层 CSP（P2-5）─────────────────────────────────────
def test_renderer_sends_csp(tmp_path=None):
    """渲染层必须带 CSP，且**没有 'unsafe-inline' 后门**。

    为什么要有这条：转义目前是全量排查过的（12 个模块的 innerHTML 写入点都过
    `esc` / `fmtText` / `textContent`），所以这**不是**一个现成漏洞 —— 但渲染层要
    展示**模型生成的内容**，一旦将来某处漏了转义，没有 CSP 就没有第二道防线。

    而 CSP 最容易「加上去但没人验证」：头没下发、或下发了却被 'unsafe-inline'
    抵消，界面看起来**一模一样**。所以这里钉死两件事：头在、且没有那两个后门。

    UI 侧（`_verify/verify.js`）另有一组断言：桩服务原样下发这份 CSP
    （测试环境不宽松于生产）+ 全流程监听 `securitypolicyviolation` 零违规
    + 一条正对照证明监听器真的在工作。这里只守「头在不在、内容对不对」。
    """
    data = _tmp_root()
    # root 用仓库根目录，才有真实渲染层（_tmp_root 只拷了 packs）
    app = create_app(ROOT, token=TOKEN, data_dir=data)
    c = TestClient(app, base_url=LOOPBACK, raise_server_exceptions=False)

    r = c.get("/")
    assert r.status_code == 200, r.status_code
    assert '<script type="module"' in r.text, "前提守卫：渲染层真的被服务出来了"

    csp = r.headers.get("content-security-policy", "")
    assert csp, "渲染层没有下发 CSP"
    for directive in ("default-src 'self'", "script-src 'self'", "style-src 'self'",
                      "img-src 'self' data:", "font-src 'self'", "connect-src 'self'",
                      "object-src 'none'", "base-uri 'none'", "form-action 'none'",
                      "frame-ancestors 'none'"):
        assert directive in csp, f"缺 {directive}：{csp}"
    assert "unsafe-inline" not in csp, f"留了 unsafe-inline 后门：{csp}"
    assert "unsafe-eval" not in csp, f"留了 unsafe-eval 后门：{csp}"

    # 子资源也要带：CSP 挂在中间件上，不是只挂 index —— 免得将来有人
    # 新加一类响应（比如某个 /download 路由）时漏挂。
    js = c.get("/static/js/main.js")
    assert js.status_code == 200, js.status_code
    assert js.headers.get("content-security-policy") == csp, "子资源没带 CSP"
    shutil.rmtree(data, ignore_errors=True)


# ── 8 交付路径：令牌通道 / 私有资料 / 注释 / 地址 / 可写包根 ──
#
# 这一组来自 2026-09-22 的「出厂 / 交付路径」复审，逐条对应一个已复现的缺陷。
# 每条都**先看行为**（起真进程 / 读真字节），不看代码顺不顺眼。

def test_engine_token_comes_from_env_not_argv(tmp_path=None):
    """缺陷 1：令牌经**子进程环境变量**交给引擎，命令行上一个字都不留。

    为什么必须真起进程：这条缺陷的全部内容就是「Windows 上同机任何进程能读到
    别人的 argv」，用 TestClient 直接 `create_app(token=...)` 是**同义反复**——
    它证明不了 Electron 传参那条路不写令牌。所以这里两头都钉：
      · 引擎侧：只给 `TALKSCRIPT_TOKEN` 也能起来，并且认这个令牌；
      · 参数侧：argv 里出现 `--token` 或令牌值本身 = 失败。
    （Electron 侧的 argv 由 `desktop/engine-path.test.js` 钉，那里能断到 args 数组。）
    """
    import os
    import socket
    import subprocess
    import urllib.error
    import urllib.request

    def _free_port() -> int:
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        return port

    def _get(port: int, path: str, token: str = ""):
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}")
        if token:
            req.add_header("X-TalkScript-Token", token)
        try:
            with urllib.request.urlopen(req, timeout=3) as r:
                return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")

    tmp = _tmp_root()
    port = _free_port()
    env_token = "env-only-token-4242"
    args = [sys.executable, "-m", "app.server", "--port", str(port),
            "--root", str(tmp), "--data-dir", str(tmp),
            "--parent-pid", str(os.getpid())]      # 看门狗照旧走 argv（不是凭证）
    eng = subprocess.Popen(
        args, cwd=str(ROOT),
        env={**os.environ, "PYTHONIOENCODING": "utf-8", "TALKSCRIPT_MOCK": "1",
             "TALKSCRIPT_TOKEN": env_token},
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        assert "--token" not in args and not any(env_token in a for a in args), \
            "测试自己就把令牌写进 argv 了，那这条断言是空的"
        deadline = time.time() + 40
        while time.time() < deadline:
            try:
                if _get(port, "/api/health")[0] == 200:
                    break
            except OSError:
                time.sleep(0.3)
        else:
            raise AssertionError("引擎没起来 —— 环境变量 TALKSCRIPT_TOKEN 没被认？")

        assert _get(port, "/api/meta")[0] == 401, "无令牌本该 401"
        code, body = _get(port, "/api/meta", env_token)
        assert code == 200, (code, body[:200])
        assert _get(port, "/api/meta", "wrong-token")[0] == 401

        # /docs 那条（缺陷 2）在这里顺手也验一次：它一直就在令牌之外，
        # 关掉之后连"带对令牌"都不该给出接口结构。
        for p in ("/docs", "/redoc", "/openapi.json"):
            assert _get(port, p, env_token)[0] in (401, 404), p
    finally:
        eng.kill()
        try:
            eng.wait(timeout=5)
        except Exception:                           # noqa: BLE001
            pass
        shutil.rmtree(tmp, ignore_errors=True)


def test_private_pack_files_are_not_served_over_http(tmp_path=None):
    """缺陷 4：`/api/packs/{name}/file?rel=private/...` 不再回明文。

    与「安装包排除 private/」是同一份产品立场，两件事不能互相矛盾。
    这里同时钉住**别把整个查看器一起弄坏**：非 private 的文件照旧 200。
    """
    tmp = _tmp_root()
    priv = tmp / "packs" / "elevator" / "private"
    priv.mkdir(parents=True, exist_ok=True)
    (priv / "products.yaml").write_text(
        "型号: SECRET-MODEL-9000\n报价: 128000\n", encoding="utf-8")
    c = _client(tmp)

    for rel in ("private/products.yaml", "private/", "Private/products.yaml",
                "./private/products.yaml", "private\\products.yaml",
                "knowledge/../private/products.yaml"):
        r = c.get("/api/packs/elevator/file", params={"rel": rel})
        assert r.status_code == 403, (rel, r.status_code, r.text[:200])
        assert "SECRET-MODEL-9000" not in r.text, f"{rel} 把私有资料读出去了"
        assert "私有资料" in r.json()["detail"], r.text      # 要说清去哪儿看

    ok = c.get("/api/packs/elevator/file", params={"rel": "pack.yaml"})
    assert ok.status_code == 200, (ok.status_code, ok.text[:200])
    assert ok.json()["text"], "非 private 的预览被一起挡掉了"
    # 列表仍然**看得见**私有文件（只有名字与体积）—— 界面靠它分组，
    # 而"存在一个 private/"这件事本来就不是秘密（安装包排除规则也承认它在）。
    files = [f["rel"] for f in c.get("/api/packs/elevator").json()["files"]]
    assert any(f.startswith("private/") for f in files), files
    shutil.rmtree(tmp, ignore_errors=True)


def test_undraft_preserves_comments(tmp_path=None):
    """缺陷 3：「标记为已校对」不许吃掉 pack.yaml 的注释。

    这条断的是**字节**，不是 `yaml.safe_load` 后的等价 ——
    safe_load 看不出注释丢了（注释在数据模型里根本不存在），所以原来的
    `test_undraft_clears_flag_and_keeps_rest` 全绿而注释照样没了。
    用仓库里那份真 pack.yaml（含 12 行注释 + 行尾注释）。
    """
    tmp = Path(tempfile.mkdtemp(prefix="talkscript-undraft-"))
    shutil.copytree(ROOT / "packs", tmp / "packs")
    (tmp / "config.yaml").write_text("llm:\n  api_key: MOCK\n", encoding="utf-8")
    y = tmp / "packs" / "elevator" / "pack.yaml"
    before = y.read_text(encoding="utf-8")
    # 前置：真包里 draft 当前是 false，先手动翻成 true 才像在走这条路径
    assert "\ndraft: false" in before, "前提变了：仓库包不再是 draft: false"
    y.write_text(before.replace("draft: false", "draft: true", 1), encoding="utf-8")

    c = _client(tmp)
    r = c.post("/api/packs/elevator/undraft")
    assert r.status_code == 200, (r.status_code, r.text)
    after = y.read_text(encoding="utf-8")

    def comments(t):
        return [ln for ln in t.split("\n") if ln.strip().startswith("#")]
    whole_line, inline = comments(before), comments(after)
    assert len(whole_line) > 8, f"这个包没有注释可比对，断言是空的：{len(whole_line)}"
    assert whole_line == inline, "整行注释少了 —— safe_dump 往返又回来了"
    # 行尾注释（"true 时界面显示…" 那类）同样要在，且贴在同一行上
    draft_line = [ln for ln in after.split("\n") if ln.startswith("draft:")]
    assert len(draft_line) == 1 and "#" in draft_line[0], draft_line
    # 最强的一条：false → true → 再点「已校对」回到 false，
    # 除值本身以外**一个字节都不该变**（注释、空行、对齐全部原地）。
    assert after == before, "就地改写后与原始字节不一致 —— 逐行贴出差异再改：\n" \
        + repr([(x, y) for x, y in zip(before.split("\n"), after.split("\n"))
                if x != y][:4])
    assert c.get("/api/packs/elevator").json()["draft"] is False
    shutil.rmtree(tmp, ignore_errors=True)


def test_base_url_rejects_and_warns(tmp_path=None):
    """缺陷 5：地址校验只管「存了必然出错」的，其余只警告但必须常驻可见。

    ⚠ 不许拦明文 http 的本机 / 内网地址 —— 用户正在用 `http://<ip>:9200/v1`
    跑本地模型，拦了就是把他能跑的配置改坏（这条同时是回归守卫）。
    """
    tmp = _tmp_root()
    c = _client(tmp)
    bad = {
        "file:///C:/Users/me/secrets": "协议",
        "javascript:alert(1)": "协议",
        "ftp://host/v1": "协议",
        "http://user:pass@host/v1": "内嵌账号密码",
        "http://host /v1": "空格",
        "https://": "主机名",
        "api.openai.com/v1": "http://",
    }
    for url, hint in bad.items():
        r = c.post("/api/models", json={"id": "", "base_url": url, "model": "m"})
        assert r.status_code == 400, (url, r.status_code, r.text)
        assert hint in r.json()["detail"], (url, r.text)
    # 旧入口 /api/config 也不能绕（界面上改不到，但 curl 与老渲染层会用）
    r = c.post("/api/config", json={"base_url": "http://user:pass@host/v1"})
    assert r.status_code == 400, r.text

    # 本机明文 http：过，且**不警告**
    r = c.post("/api/models", json={"id": "", "base_url": "http://127.0.0.1:9200/v1",
                                    "model": "local"})
    assert r.status_code == 200, r.text
    assert r.json()["warnings"] == [], r.text

    # 远端明文 http：过，但随响应回警告
    r = c.post("/api/models", json={"id": "", "base_url": "http://192.168.1.20:9200/v1",
                                    "model": "lan"})
    assert r.status_code == 200, r.text
    assert any("明文" in w and "192.168.1.20" in w for w in r.json()["warnings"]), r.text
    # 而且**不是只在保存那一刻说一次**：GET /api/config 常驻下发
    got = c.get("/api/config").json()
    assert any("明文" in w for w in got["base_url_warnings"]), got["base_url_warnings"]
    shutil.rmtree(tmp, ignore_errors=True)


def test_user_packs_live_in_writable_dir_and_survive_update(tmp_path=None):
    """缺陷 8：可写行业包目录 = 用户数据目录，安装目录那份退化成只读种子。

    钉的四件事：
      1. 出厂包被播种过去，列表里**合起来**看得到（用户目录一份就是全部）；
      2. 引擎写包（undraft、向导建包）落在用户目录，安装目录一个字节都不改；
      3. 出厂包升级会同步到没被用户改过的包，并保住其中的 private/；
      4. 用户改过的包**绝不覆盖**（这正是原缺陷的反面：一次更新抹掉一切）。
    """
    install = Path(tempfile.mkdtemp(prefix="ts-install-"))
    user = Path(tempfile.mkdtemp(prefix="ts-user-"))
    shutil.copytree(ROOT / "packs", install / "packs")
    (install / "config.yaml").write_text("llm:\n  api_key: MOCK\n", encoding="utf-8")
    packs_dir = user / "packs"

    c = _client_packs(install, packs_dir)
    names = [p["name"] for p in c.get("/api/meta").json()["packs"]]
    assert "elevator" in names, names
    assert (packs_dir / "elevator" / "pack.yaml").exists(), "没播种过去"
    assert (packs_dir / ".packseed.json").exists(), "没留台账"

    # 出厂那份不许被写：undraft 必须落在用户目录
    (packs_dir / "elevator" / "private").mkdir(parents=True, exist_ok=True)
    (packs_dir / "elevator" / "private" / "products.yaml").write_text("a: 1\n",
                                                                      encoding="utf-8")
    inst_elevator = (install / "packs" / "elevator" / "pack.yaml").read_text(encoding="utf-8")
    y = packs_dir / "elevator" / "pack.yaml"
    y.write_text(y.read_text(encoding="utf-8").replace("draft: false", "draft: true", 1),
                 encoding="utf-8")
    assert c.post("/api/packs/elevator/undraft").status_code == 200
    assert "draft: false" in y.read_text(encoding="utf-8"), "用户目录那份没被改"
    assert (install / "packs" / "elevator" / "pack.yaml").read_text(encoding="utf-8") \
        == inst_elevator, "写回到安装目录里了 —— 那正是会被升级抹掉的位置"

    # 模拟"一次自动更新"：安装目录的包换了内容（加了个新文件），重开引擎
    (install / "packs" / "elevator" / "knowledge" / "new-after-update.md").write_text(
        "出厂新增\n", encoding="utf-8")
    # 用户**没改过**内容（只加了 private/，而 private 不参与"改过"判定）→ 该同步
    c2 = _client_packs(install, packs_dir)
    assert c2.get("/api/meta").status_code == 200
    assert (packs_dir / "elevator" / "knowledge" / "new-after-update.md").exists(), \
        "出厂更新没进来（用户没改过这个包，同步是安全的）"
    assert (packs_dir / "elevator" / "private" / "products.yaml").exists(), \
        "同步把用户的 private/ 一起删了 —— 那是比丢注释更贵的错误"

    # 用户改过的包：绝不覆盖，并且要留下话（不静默）
    (packs_dir / "elevator" / "banwords.yaml").write_text(
        "groups:\n- name: 我的\n  words: [自留词]\n", encoding="utf-8")
    before = (packs_dir / "elevator" / "banwords.yaml").read_text(encoding="utf-8")
    (install / "packs" / "elevator" / "knowledge" / "another.md").write_text(
        "又一次出厂改动\n", encoding="utf-8")
    c3 = _client_packs(install, packs_dir)
    assert c3.get("/api/meta").status_code == 200
    assert (packs_dir / "elevator" / "banwords.yaml").read_text(encoding="utf-8") == before, \
        "用户手改的词表被出厂版本覆盖了"
    assert not (packs_dir / "elevator" / "knowledge" / "another.md").exists(), \
        "既然决定不覆盖，就不该出现半覆盖（这个词表改动会跟着新文件一起被抹）"

    # 用户自己建的包在安装目录里根本不存在，也不该被"清理"掉
    mine = packs_dir / "my-industry"
    mine.mkdir(exist_ok=True)
    (mine / "pack.yaml").write_text("name: my-industry\ndisplay_name: 我的行业\n"
                                    "draft: false\nparams: {}\n", encoding="utf-8")
    c4 = _client_packs(install, packs_dir)
    names4 = [p["name"] for p in c4.get("/api/meta").json()["packs"]]
    assert "my-industry" in names4 and "elevator" in names4, names4

    for d in (install, user):
        shutil.rmtree(d, ignore_errors=True)


def _client_packs(install_root: Path, packs_dir: Path):
    """带可写包根的客户端（`--packs-dir` 的 TestClient 形态）。"""
    app = create_app(install_root, token=TOKEN, data_dir=install_root,
                     packs_dir=packs_dir)
    c = TestClient(app, base_url=LOOPBACK, raise_server_exceptions=False)
    c.headers.update({"X-TalkScript-Token": TOKEN})
    return c


# ── 9 两个 409 必须分得开（P1-1）───────────────────────────────
# 「并发额度满」与「行业包坏了」都归 409 Conflict（两者确实都是
# "请求与资源当前状态冲突"），但用户要做的两件完全不同的事：
#   额度满 = 等一拍再发；包坏了 = 去修那个文件，不修则永远发不出去。
# 修复前渲染层判的是 `status === 409`，于是坏包被念成「同时进行的生成已达上限」，
# 而引擎那句「pack.yaml 语法有误（第 4 行第 6 列）」整句丢掉 ——
# 两句一模一样的话，两种完全不同的病。
#
# 修法不是换状态码（409 对两者都成立，且 tests/test_pack_yaml_integrity.py 已把
# 坏包这条钉在 409 上），而是**每条应答都同时给人话与机器码**：
# detail 继续是字符串原话，code 是给程序看的稳定标识。
BROKEN_PACK_YAML = "hard:\n  - 绝对安全\n  bad: [unclosed\n"
_POS_RE = re.compile(r"第 \d+ 行第 \d+ 列")


def _mock_root():
    """一个「配好了模型」的临时根目录：生成请求要能走到验包那一步。"""
    return _tmp_root_mock()


def test_broken_pack_409_carries_its_own_code(tmp_path=None):
    """坏包：状态码仍是 409，但 code 说清是谁，detail 仍是引擎那句**原话**。"""
    if TestClient is None:
        return
    tmp = _mock_root()
    try:
        (tmp / "packs" / "elevator" / "pack.yaml").write_text(
            BROKEN_PACK_YAML, encoding="utf-8")
        body = _client(tmp).post(
            "/api/generate", json={"pack": "elevator", "topic": "随便一个话题"})
        assert body.status_code == 409, body.text
        j = body.json()
        assert j.get("code") == "pack_broken", j
        # detail **必须还是字符串**：别的测试与渲染层都按「一句人话」读它，
        # 把它换成对象会让 "pack.yaml" in detail 这类断言静默读到键名。
        assert isinstance(j["detail"], str), type(j["detail"])
        assert "pack.yaml" in j["detail"] and _POS_RE.search(j["detail"]), j
        # 这句里不许混进额度的说法 —— 那正是这次要分开的两件事
        assert "已达上限" not in j["detail"] and "额度" not in j["detail"], j
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_quota_409_carries_its_own_code(tmp_path=None):
    """额度满：同一道闸，另一个 code；detail 是 pipeline 那句原话。"""
    if TestClient is None:
        return
    from unittest.mock import patch
    from app.jobs import StateConflict
    from app.pipeline import Pipeline

    tmp = _mock_root()
    try:
        msg = "同时进行的生成已达上限（4 个），请等其中一条完成后再试"
        with patch.object(Pipeline, "start_generate", side_effect=StateConflict(msg)):
            r = _client(tmp).post(
                "/api/generate", json={"pack": "elevator", "topic": "随便一个话题"})
        assert r.status_code == 409, r.text
        j = r.json()
        assert j.get("code") == "quota_exceeded", j
        assert j["detail"] == msg, j                      # 原话，一个字都不许换
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_the_two_409_replies_are_distinguishable_by_machine(tmp_path=None):
    """把这次的核心缺陷正面钉住：同一状态码，两次应答**必须**分得开。

    只测「各自都有 code」是不够的 —— 病灶是两个 409 在界面上说同一句话。
    这里把两份应答摆在一起比：状态码可以相同（都对），code 与 detail 都必须不同。
    """
    if TestClient is None:
        return
    from unittest.mock import patch
    from app.jobs import StateConflict
    from app.pipeline import Pipeline

    tmp = _mock_root()
    try:
        (tmp / "packs" / "elevator" / "pack.yaml").write_text(
            BROKEN_PACK_YAML, encoding="utf-8")
        broken = _client(tmp).post(
            "/api/generate", json={"pack": "elevator", "topic": "随便一个话题"}).json()
        with patch.object(Pipeline, "start_generate",
                          side_effect=StateConflict("同时进行的生成已达上限（4 个），请等其中一条完成后再试")):
            quota = _client(tmp).post(
                "/api/generate", json={"pack": "elevator", "topic": "随便一个话题"}).json()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    assert broken.get("code") and quota.get("code"), (broken, quota)
    assert broken["code"] != quota["code"], (broken, quota)
    assert broken["detail"] != quota["detail"], \
        f"两种 409 说了同一句话：{broken['detail']!r}"


def test_state_conflict_that_is_not_quota_is_not_labelled_quota(tmp_path=None):
    """StateConflict 还挂在「作业状态不允许这个操作」上（app/jobs.py）。
    它同是 409，但既不是额度也不是包坏了 —— 各自一个 code，别互相冒充。"""
    if TestClient is None:
        return
    from unittest.mock import patch
    from app.jobs import StateConflict
    from app.pipeline import Pipeline

    tmp = _mock_root()
    try:
        with patch.object(Pipeline, "start_generate",
                          side_effect=StateConflict("作业状态为 done，无法执行该操作")):
            j = _client(tmp).post(
                "/api/generate", json={"pack": "elevator", "topic": "随便一个话题"}).json()
        assert j.get("code") == "state_conflict", j
        assert j["detail"] == "作业状态为 done，无法执行该操作", j
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_missing_pack_404_also_gets_a_code(tmp_path=None):
    """包不存在 → 404 照旧，只是也多给一个 code：整套错误都用同一份约定，
    不是「谁想到了才加」。"""
    if TestClient is None:
        return
    tmp = _mock_root()
    try:
        c = _client(tmp)
        r = c.get("/api/packs/no-such-pack-at-all")
        assert r.status_code == 404, r.text
        assert r.json().get("code") == "pack_missing", r.json()
        assert isinstance(r.json()["detail"], str)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ── runner ──────────────────────────────────────────────────
def main() -> int:
    if TestClient is None:
        print("跳过：缺少 fastapi.testclient（pip install httpx）")
        return 1
    cases = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in cases:
        try:
            fn(None)
            print(f"  ✅ {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  ❌ {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(cases) - failed}/{len(cases)} 通过")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())


def test_every_reachable_validation_error_reads_in_chinese():
    """本项目请求模型能触发的每一种 422，界面都不能露英文整句。

    上一轮只覆盖了 `string_too_short` 与数值区间两类（第 9 轮复核指出：11 类里
    只测了 2 类）。这里穷举**实际可达**的形状：两个 Literal 下拉、一个布尔、
    一个 query 参数、以及畸形 JSON。判据不抄文案：只要求每一段都以中文标签开头
    —— 英文原句直出时它是 [A-Za-z] 开头，一定会红。
    """
    import re
    tmp = _tmp_root_mock()
    try:
        c = _client(tmp)
        cases = [
            ("voice 写错", c.post("/api/generate", json={"topic": "家用电梯怎么选", "voice": "很浓"}), "人味档位"),
            ("format 写错", c.post("/api/generate", json={"topic": "家用电梯怎么选", "format": "视频"}), "输出内容"),
            ("reroll 非布尔", c.post("/api/generate", json={"topic": "家用电梯怎么选", "reroll": "abc"}), "换一版"),
            ("query 参数写错", c.get("/api/jobs/whatever", params={"full": "maybe"}), "完整快照"),
            ("畸形 JSON", c.post("/api/generate", content=b"{oops",
                                 headers={"Content-Type": "application/json"}), None),
        ]
        for name, r, label in cases:
            assert r.status_code == 422, f"{name}: 期望 422，实得 {r.status_code} {r.text[:90]}"
            detail = r.json().get("detail")
            assert isinstance(detail, str), f"{name}: detail 不是人话字符串，而是 {type(detail).__name__}：{detail}"
            assert r.json().get("code") == "field_invalid", f"{name}: 没给机器可读码 {r.json()}"
            if label:
                assert detail.startswith(label), f"{name}: 没点名是哪个框 → {detail}"
            for seg in detail.split("；"):
                assert not re.match(r"^\s*[A-Za-z]", seg), f"{name}: 这一段是英文原句 → {seg}"
            assert "Input should be" not in detail, f"{name}: 仍然直出 pydantic 英文 → {detail}"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_packgen_stub_sends_exactly_what_the_engine_says():
    """UI 桩里那两句 422/400 文案必须与服务端真输出逐字相同。

    桩里抄一份服务端文案，是"第三本账"：改文案的人只会改一处，
    于是界面上的断言开始验证一句**引擎根本不会说的话** —— 门禁绿，量到的却是假的。
    做法与其它跨文件守卫一致：两边各读一份事实再比，不在测试里第三遍抄写。
    """
    tmp = _tmp_root_mock()
    try:
        c = _client(tmp)
        # 真输出（同一入口，与服务端唯一权威同源）
        real_422 = c.post("/api/packs/create",
                          json={"industry": "猫咖", "description": "小店"}).json()["detail"]
        real_400 = c.post("/api/packs/create",
                          json={"industry": "？？？", "description": "纯符号名字探针"}).json()["detail"]
        assert "太短" in real_422 and "目录名" in real_400, (real_422, real_400)

        stub = (ROOT / "_verify" / "verify.js").read_text(encoding="utf-8")
        assert f"'{real_422}'" in stub, \
            f"UI 桩的 422 文案与服务端不一致，界面在验证一句引擎不会说的话：{real_422}"
        # 400 那句在桩里是两段字符串拼起来的（模板串里一行太长），逐段比对
        assert real_400.split("，")[0] in stub and real_400.split("，", 1)[1].rstrip("。") in stub, \
            f"UI 桩的 400 文案与服务端不一致：{real_400}"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
