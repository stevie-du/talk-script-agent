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


def test_packgen_late_failure_cleans_up(tmp_path=None):
    """失败发生在 `_materialize` **之后**（最后写 `校对清单.md`）也要收走目录。

    上面那条用例把故障注入在 `pack.yaml`，落点其实在 try/except 里；`校对清单.md`
    是那份 try 之外的最后一次写盘，炸掉后盘上是一个「23 个文件、Pack 能加载、
    会出现在包列表里」的包，而作业记的是失败，重试同名永远 409。
    """
    tmp = _tmp_root()
    from app import packgen

    real_write = packgen.write_atomic
    hits = {"n": 0}

    def boom(path, text, *a, **kw):
        if str(path).endswith("校对清单.md"):
            hits["n"] += 1
            raise OSError("磁盘满了（落盘的最后一步）")
        return real_write(path, text, *a, **kw)

    slug = packgen.slugify("半成品行业")
    with patch.object(packgen, "write_atomic", side_effect=boom):
        try:
            packgen.create_pack(tmp, _FakeLLM(Partial), "半成品行业", "测试描述文本")
            raise AssertionError("应当抛错")
        except OSError:
            pass

    # 注入点必须真的到了：否则这条会在"根本没走到最后一步"时假绿。
    assert hits["n"] == 1, f"校对清单那一步没被执行（hits={hits}），故障注入没落点"
    assert not (tmp / "packs" / slug).exists(), "失败后残留了半成品目录，重试会被 409 挡死"
    shutil.rmtree(tmp, ignore_errors=True)


def test_packgen_rollback_never_touches_a_preexisting_pack(tmp_path=None):
    """回收网只准收走**本次**建的目录：重名时必须把别人的包原样留在盘上。

    这条钉的是上面那段 try 的形状 —— `if d.exists(): raise` 必须落在保护圈**之外**。
    把它挪进去（看着只是少一层缩进）就会让"同名包已存在"这一次失败顺手
    `rmtree` 掉那个已存在、里面有用户手写内容的包。
    """
    tmp = _tmp_root()
    from app import packgen

    slug = packgen.slugify("半成品行业")
    d = tmp / "packs" / slug
    d.mkdir(parents=True)
    (d / "用户手写的东西.md").write_text("别删我\n", encoding="utf-8")

    try:
        packgen.create_pack(tmp, _FakeLLM(Partial), "半成品行业", "测试描述文本")
        raise AssertionError("同名应当抛 FileExistsError")
    except FileExistsError:
        pass

    assert (d / "用户手写的东西.md").read_text(encoding="utf-8") == "别删我\n", \
        "同名建包失败时回收网删掉了已存在的包 —— 这是数据丢失，不是清理"
    shutil.rmtree(tmp, ignore_errors=True)


def test_packgen_rollback_ignores_a_dir_created_after_the_check():
    """检查说"目录不在"之后别人才把它建出来：这次失败也不许收走那个目录。

    第 18 轮复核 P1-1 —— `4bb65cb` 把回收圈扩到最后一次写盘，而 `d.exists()` 只证明
    "检查那一刻"不在：两个引擎进程、或同一台机器上的大小写变体（NTFS 不分大小写，
    见 P1-2）都能在检查之后把同名目录建出来。于是我们这边失败时 `rmtree` 掉的是
    **别人建好的包**，而那条作业还在报 done。修法：先独占 `mkdir`，
    只有本次真的建出了目录才允许回收。
    """
    tmp = _tmp_root()
    from app import packgen

    slug = packgen.slugify("半成品行业")
    d = tmp / "packs" / slug
    real_exists = Path.exists
    lied = {"n": 0}

    def lie(self):
        if lied["n"] == 0 and self.parent == tmp / "packs" and self.name == slug:
            lied["n"] += 1                      # 就在这一刻，另一个创建者把目录建好了
            d.mkdir(parents=True)
            (d / "owner.yaml").write_text("owner: other\n", encoding="utf-8")
            return False
        return real_exists(self)

    real_write = packgen.write_atomic

    def boom(path, text, *a, **kw):
        if str(path).endswith("pack.yaml"):
            raise OSError("磁盘满了")
        return real_write(path, text, *a, **kw)

    with patch.object(Path, "exists", lie), \
         patch.object(packgen, "write_atomic", side_effect=boom):
        try:
            packgen.create_pack(tmp, _FakeLLM(Partial), "半成品行业", "测试描述文本")
            raise AssertionError("应当抛错")
        except (OSError, FileExistsError):
            pass

    assert lied["n"] == 1, "存在性检查没被走到，这条什么都没测到"
    assert (d / "owner.yaml").read_text(encoding="utf-8") == "owner: other\n", \
        "回收网删掉了不是本次建的目录：别人的包没了，我们这边只记了一次失败"
    shutil.rmtree(tmp, ignore_errors=True)


def test_packgen_audit_failure_does_not_delete_the_finished_pack():
    """体检自己跑不成时，包必须留在盘上、这次建包仍算成功（第 20 轮复核 P2）。

    原形状：`_pack_audit` 的 try 只裹住 `Pack(root, slug)` 那一行，其后
    `pk.skill()` / `file_text` / `file_slice` / `param_audit` / `_placeholder_audit`
    全在无保护区 —— 任一处抛出就逃进 `create_pack` 的 `except Exception`，
    而那时目录已经写完、`created_here` 为真 → 回收网把**用户付过钱的那一整包**删掉，
    作业还记成 failed。"少一份自动说明"与"钱花了什么都没有"差着一个数量级。
    """
    tmp = _tmp_root()
    from app import packgen

    real = packgen._placeholder_audit
    packgen._placeholder_audit = lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("体检这一步自己炸了（模拟）"))
    slug = packgen.slugify("体检探针")
    d = tmp / "packs" / slug
    try:
        out = packgen.create_pack(tmp, _FakeLLM(Partial), "体检探针", "测试描述文本")
        assert d.is_dir(), "体检跑不成却把已写完的包删了：用户付了一份模型的钱，结果什么都没有"
        assert out["name"] == slug, "体检失败把整次建包判成失败了"
        text = (d / "校对清单.md").read_text(encoding="utf-8")
        assert "引擎体检未能跑完" in text, "没把「体检没跑成」如实写进清单，用户会以为体检过"
    finally:
        packgen._placeholder_audit = real
        shutil.rmtree(tmp, ignore_errors=True)


def test_packgen_unusable_pack_is_not_reported_as_success():
    """`Pack()` 都过不去的生成包：作业必须失败、目录要被回收，不能报 done（第 21 轮 P1-1）。

    复量到的原形态：模型产出的 `skill.yaml` 语法坏 → `_pack_audit` 把 `PackBrokenError`
    吞成一句"说明"，于是 `create_pack` 正常返回、作业 **done**、盘上留着一个谁也打不开的包、
    清单里还写着「这不是包结构错误」。钱确实已经花了，这点改不了，但界面上说"成功"
    就是第二层伤害，而那个半成品目录会把同名重试永久挡在 409（应用里没有删包入口）。
    """
    tmp = _tmp_root()
    from app import packgen

    real = packgen._materialize

    def broken(d, *a, **k):
        notes = real(d, *a, **k)
        (d / "skill.yaml").write_text("stages:\n  write:\n    files: [\n", encoding="utf-8")
        return notes

    slug = packgen.slugify("体检结构坏")
    packgen._materialize = broken
    msg = ""
    try:
        packgen.create_pack(tmp, _FakeLLM(Partial), "体检结构坏", "测试描述文本")
        raise AssertionError("打不开的包不该被当成成功")
    except ValueError as e:
        msg = str(e)
    finally:
        packgen._materialize = real
    assert "连加载都过不去" in msg, msg
    assert not (tmp / "packs" / slug).exists(), \
        "包打不开却留在盘上：作业失败了，下次同名提交还会被 409 永久挡住"
    shutil.rmtree(tmp, ignore_errors=True)


def test_industry_name_cap_uses_one_unit_on_both_sides():
    """行业名长度：请求层与建包层必须是**同一个单位**（第 21 轮复核 P2）。

    `'𠀀' * 40` 是 40 个码点 / 80 个 UTF-16 码元。原来请求层用 `max_length`（按码点）放行，
    建包层按码元报「有 80 个码元，上限 40」—— 一个 40 字的名字收到两条互相矛盾的账，
    而且上一笔我刚把"上限只有一个出处"写进注释：数字统一了、单位没统一，那句话仍是谎。
    """
    from app import packgen
    from app.schemas import PackCreateRequest, utf16_units      # noqa: F401

    astral = "𠀀" * 40
    assert len(astral) == 40 and utf16_units(astral) == 80
    assert packgen._utf16_units(astral) == utf16_units(astral), "两层又各算各的单位"
    assert packgen.slug_problem(packgen.slugify(astral)), \
        "建包层没拒：那请求层放行后就会在写盘时才炸"
    assert not packgen.slug_problem("宠物医院"), "边界内的名字被误伤"

    tmp = _tmp_root_mock()
    try:
        c = _client(tmp)
        r = c.post("/api/packs/create",
                   json={"industry": astral, "description": "社区推拿，面向上班族"})
        assert r.status_code == 422, (r.status_code, r.text[:150])
        assert "太长" in r.text, r.text[:200]
        ok = c.post("/api/packs/create",
                    json={"industry": "宠物医院", "description": "社区医院，面向养宠家庭"})
        assert ok.status_code == 200, ok.text[:150]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_history_route_rejects_a_jid_with_trailing_whitespace():
    """`_JID_RE` 那半边的 fullmatch 迁移也得有用例（第 18 轮复核 P2-2）。

    Python 的 `$` 放过结尾换行：`20260101-000000-abcdef\\n` 在旧写法下算合法 jid。
    包名那半边有 `test_stub_pack_name_check_agrees_with_the_engine` 守着（改回旧写法会红），
    jid 这半边当时**没有任何判据** —— 把它改回 `^...$` + `.match` 全量仍然绿。
    """
    tmp = _tmp_root_mock()
    try:
        c = _client(tmp)
        for tail in ("%0a", "%0d", "%20", "%0a%20"):
            r = c.get(f"/api/history/20260101-000000-abcdef{tail}")
            assert r.status_code == 400, (tail, r.status_code, r.text[:80])
        ok = c.get("/api/history/20260101-000000-abcdef")
        assert ok.status_code != 400, f"合法 jid 也被拒了：{ok.status_code} {ok.text[:80]}"
    finally:
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
        # 第 10 轮桩新加的三条 404（未知包的 /file、未知作业的 GET 与 cancel）同样必须同源于服务端，
        # 连 `code` 一起比 —— 界面按 code 判的那条路径不能只在桩上存在。
        rp = c.get("/api/packs/没有这个包/file?rel=pack.yaml").json()
        rj = c.get("/api/jobs/没有这个作业").json()
        rc = c.post("/api/jobs/没有这个作业/cancel").json()
        assert rp.get("code") == "pack_missing", rp
        # 桩"按路由发各自的原文"这件事只有在这两句**本来就不同**时才有意义：
        # 第 16 轮复核量出，今天整条对账比的是"桩里有没有这句话"（成员关系），
        # 所以把 cancel 那支改成发 GET 的原文，两边各发一份正确的原文、对账照样绿。
        # 这里补两刀：(1) 引擎这两句必须不同；(2) 桩里那两个常量必须各有使用点
        # （定义之外 ≥1 次）—— 共用一句 / 把其中一支改到另一支上，会立刻只剩定义行。
        assert rj["detail"] != rc["detail"], \
            f"GET 与 cancel 发了同一句 404（{rj['detail']}）：分路由的原文已经名存实亡"
        # ⚠ 必须按**带引号的字面量**比，不能裸比子串：「作业不存在」是
        #   「作业不存在或已随重启释放」的前缀，裸比会让 cancel 那支改错了也照样绿。
        rbad = c.get("/api/packs/%2e%2e/file?rel=pack.yaml")
        assert rbad.status_code == 400, rbad.text
        for frag in (f"'{rp['detail'].split('：', 1)[0]}：'", f"'{rp['code']}'",
                     f"'{rj['detail']}'", f"'{rc['detail']}'",
                     f"'{rbad.json()['detail']}'"):
            assert frag in stub, f"UI 桩的 404/400 文案与服务端不一致：{frag}"
        # 桩侧：两个 404 常量除了定义行还必须各有**使用点**（出现 ≥2 次）。
        # 把 cancel 那支改成发 GET 的原文（第 16 轮复核列的变异形状）时，
        # 「桩按路由发各自的原文」这件事就只剩一个悬空的常量 —— 这里会红，
        # 而原来那圈成员关系比对不会（两句都还在桩里）。
        for const in ("ERR_JOB_GONE_GET", "ERR_JOB_GONE_CANCEL"):
            assert stub.count(const) >= 2, \
                f"桩里 {const} 只出现 {stub.count(const)} 次（只有定义？）—— 那条路由没在发它"
        # 建包的两类冲突是两句不同的话（app/packgen.py 的两条 raise）：桩原来只有
        # 「正在创建中」一句，于是"占位到底还不还"在桩上量不出来（两种情况都 409）。
        # 走源码对账而不是走 HTTP：这条路径要花钱/要有模型配置，文案对账不该依赖它。
        pgsrc = (ROOT / "app" / "packgen.py").read_text(encoding="utf-8")
        for why in ("行业包正在创建中", "行业包已存在"):
            assert f"{why}：" in pgsrc, f"服务端不再有「{why}：」这句，桩里的措辞该跟着改"
            assert f"'{why}：'" in stub, f"桩里没有「{why}：」这一句（与服务端不同源）"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_packgen_stub_snapshot_keys_are_the_engine_key_list():
    """桩里那两条"快照有哪些键"的清单，必须就是 `Job.snapshot()` 现算出来的那两条。

    第 16 轮复核 P2-4：门禁里比对键名用的是**手抄的字面量**，在途那一份甚至少带
    `created_at` 与 error 也照样绿 —— 因为没人拿引擎的真键集去比。
    这里把两份清单变成可算的：引擎加了/改/删一个快照字段，桩与这两条 JS 字面量都会红。
    """
    from app.jobs import Job

    j = Job("jobpg-key-probe", "packgen", {"industry": "探针辛"})
    j.stream_phase = "行业包生成"              # 引擎只在设过 phase 之后才带 stream 键
    inflight = ",".join(sorted(j.snapshot(include_result=False)))
    j.state = "done"                          # 只为取终态那一份的键集，不走状态机
    terminal = ",".join(sorted(j.snapshot(include_result=False)))

    stub = (ROOT / "_verify" / "verify.js").read_text(encoding="utf-8")
    for label, keys in (("在途", inflight), ("终态", terminal)):
        assert f"'{keys}'" in stub, (
            f"桩里{label}快照的键名清单与引擎不同源：引擎现在给的是 {keys}")
    # 两条清单的差别本身也是判据：轮询走 include_result=False，未终态不带 result
    assert "result" in terminal and "result" not in inflight, (inflight, terminal)


# ── P0-5：未预期异常也必须有 detail + code（五路审查 2026-09-23）────
# 病灶：start_generate 建 job_dir 失败（磁盘满/权限）时 `_fail(job, e)` 后
# **裸 raise 原异常**，而全局只注册了 PackError/PackBrokenError/
# StateConflict/RequestValidationError 四个 handler —— 其余异常冒到
# FastAPI 默认层，变成 500 + text/plain「Internal Server Error」：
# 空 body、无 code，渲染层按 api.js 约定读不到任何原因。
# 同族抛出点还有 start_packgen / rewrite_segment / start_intel_fetch。
def test_unexpected_error_returns_json_detail_and_code():
    from unittest.mock import patch
    from app.store import ArtifactStore
    tmp = _tmp_root_mock()          # mock Key：否则「没配 Key」的 400 会提前返回，patch 根本走不到
    try:
        c = _client(tmp)
        with patch.object(ArtifactStore, "job_dir",
                          side_effect=OSError("磁盘空间不足")):
            r = c.post("/api/generate", json={"pack": "elevator", "topic": "探针"})
        # 状态码仍是 500（这是真错误），但**形状**必须与其它错误一致：
        # JSON + detail（人话，带异常原文）+ code（稳定机器码）。
        assert r.status_code == 500, r.text
        assert r.headers["content-type"].startswith("application/json"), \
            f"500 的 body 不是 JSON：{r.headers.get('content-type')}"
        body = r.json()
        assert isinstance(body.get("detail"), str) and "磁盘空间不足" in body["detail"], body
        assert body.get("code") == "internal_error", body
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_error_codes_are_a_closed_set():
    """ERR_* 常量定义与 handler 里的使用对账：只加一边 = 有错误永远走默认 500 空 body。

    守的是"两本账"：常量在 app/server.py 定义、handler 在 create_app 里引用。
    维度是**常量名**（ERR_XXX），不是它的小写值 —— 两本账在名字这一层合流。
    """
    src = (ROOT / "app" / "server.py").read_text(encoding="utf-8")
    import re as _re
    defined = set(_re.findall(r'^(ERR_\w+) = "', src, _re.M))
    assert "ERR_INTERNAL" in defined, "ERR_INTERNAL 没了 —— 未预期异常会退回 500 空 body"
    used = set(_re.findall(r'\b(ERR_\w+)\b', src))
    unused = defined - used
    assert not unused, f"定义了没人用的 code：{sorted(unused)}"
