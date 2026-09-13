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


# ── 1 配置现读 ──────────────────────────────────────────────
def test_config_fresh(tmp_path=None):
    tmp = _tmp_root()
    import app.server as srv
    c = _client(tmp)

    before = c.get("/api/meta")
    assert before.status_code == 200, before.status_code
    assert before.json()["model"] == "startup-model", before.json()
    assert before.json()["has_api_key"] is False, before.json()

    # 没有任何 Key 时，建包必须先被拦下
    with patch.object(srv, "create_pack", lambda *a, **kw: {"name": "fake"}):
        denied = c.post("/api/packs/create",
                        json={"industry": "假体陀机", "description": "测试用行业描述"})
    assert denied.status_code == 400, (denied.status_code, denied.text)
    assert "未配置" in denied.json()["detail"], denied.text

    # 用户在设置页保存 Key 与模型（这一步之后不重启）
    save_config(tmp, {"api_key": "sk-after-startup", "model": "saved-model"})

    after = c.get("/api/meta")
    assert after.json()["model"] == "saved-model", after.json()
    assert after.json()["has_api_key"] is True, after.json()

    with patch.object(srv, "create_pack", lambda *a, **kw: {"name": "fake"}):
        allowed = c.post("/api/packs/create",
                         json={"industry": "假体陀机", "description": "测试用行业描述"})
    assert allowed.status_code == 200, (allowed.status_code, allowed.text)
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

    边界必须在写入前卡：load_config 里是 `llm.get(x) or 默认值`，
    写进去的 0 或负数会被悄悄换成默认值 —— 界面显示「已保存」、
    实际值却不是用户填的那个，比直接拒绝更难查。
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
