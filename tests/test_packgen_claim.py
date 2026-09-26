# -*- coding: utf-8 -*-
"""P2-46 后半：同名建包要在**花钱之前**被拦住。

修复前的形态：入口只有 `(packs/slug).exists()` 这一道同步检查，而目录要到模型
返回之后才建 —— 于是两条同名请求双双通过检查、双双起作业，双份 token，
第二条还要等一两分钟、以"作业失败"的样子告诉用户重名了。
`_creating` 的占用原来也发生在模型调用之后，等于没占。
"""
from __future__ import annotations

import random
import re
import shutil
import sys
import tempfile
import threading
import time
import unicodedata
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import packgen                                    # noqa: E402
from app.packgen import preview_slug                        # noqa: E402
from app.config import load_config                         # noqa: E402
from app.jobs import TERMINAL_STATES                       # noqa: E402
from app.llm import LLMClient                              # noqa: E402
from app.pipeline import Pipeline                          # noqa: E402

INDUSTRY = "宠物医院"
DESC = "连锁宠物医院，面向养宠家庭做科普与到院转化"


class SlowMock(LLMClient):
    """一次模型调用睡 0.4 秒 —— 给第二条请求制造真实的并发窗口。"""

    def __init__(self, cfg):
        super().__init__(cfg, mock=True)
        self.calls = 0
        self._gate = threading.Event()

    def chat_json(self, task, system, user, model_cls, **kw):
        self.calls += 1
        time.sleep(0.4)
        return model_cls.model_validate({
            "display_name": INDUSTRY, "tagline": "", "persona": "兽医",
            "segments": ["疫苗", "驱虫"], "audiences": ["养宠家庭"], "personas": ["兽医"],
            "topics": [{"heading": "疫苗", "core": "第一针在 8 周龄",
                        "myths": [], "hooks": []}],
            "audience_details": [], "ideas": ["幼犬第一针"], "redlines": ["不得承诺治愈"],
            "banwords_extra_hard": [], "banwords_extra_soft": [],
            "verify_list": ["现行免疫规范编号"],
        })


def _pipeline() -> tuple[Pipeline, Path, SlowMock]:
    tmp = Path(tempfile.mkdtemp(prefix="packgen-claim-"))
    shutil.copytree(ROOT / "packs", tmp / "packs")
    cfg = load_config(tmp)
    cfg.mock = True
    pl = Pipeline(tmp, cfg)
    client = SlowMock(cfg.llm)
    pl.llm = client
    return pl, tmp, client


def test_a_thread_that_cannot_start_releases_the_claim_exactly_once():
    """`Thread.start()` 起不来时：归还点必须只有一个、作业必须落终态、名字必须还能再用。

    第 15 轮我在报告里写下"这条路径会归还两次，可能偷走别人的同名占位"，自己复测下来是
    **恰好一次** —— 那就把它钉成用例，而不是留在一段话里（§15.25 的更正）。
    """
    import app.pipeline as plmod

    pl, tmp, client = _pipeline()
    releases = []
    real_start, real_release = threading.Thread.start, plmod.release_slug

    def counting(slug, owner):
        releases.append((slug, owner))
        return real_release(slug, owner)

    def boom(self, *a, **k):
        raise RuntimeError("起不了线程")

    try:
        threading.Thread.start = boom
        plmod.release_slug = counting
        with pytest.raises(RuntimeError):
            pl.start_packgen("探针丙", "探针用的行业说明文字")
    finally:
        threading.Thread.start = real_start
        plmod.release_slug = real_release
        for s in list(packgen._creating):
            packgen.release_slug(s, packgen.ANY_OWNER)
        shutil.rmtree(tmp, ignore_errors=True)

    slugs = [s for s, _ in releases]
    assert slugs == ["探针丙"], f"归还点不唯一或漏了：{slugs}"
    # 归还必须交回**凭证**（不是空串、不是 ANY_OWNER 的"硬摘"）：
    # 只数次数看不出"摘的是谁的锁"，而第 16 轮那条 P1 恰恰是次数对、对象错。
    owners = [o for _, o in releases]
    assert all(o and o != packgen.ANY_OWNER for o in owners), \
        f"归还时没带占位凭证，等于无条件 discard：{owners}"
    assert "探针丙" not in packgen._creating, "同名占位没还，之后永远建不出来"
    states = [s["state"] for s in pl.registry.snapshots()]
    assert states == ["failed"], f"作业没落到终态：{states}"
    assert pl.registry.running_count() == 0, "占着的并发额度没还"
    assert packgen.claim_slug("探针丙")
    packgen.release_slug("探针丙", packgen.ANY_OWNER)


def test_a_late_release_cannot_steal_a_claim_made_in_between():
    """`Thread.start()` 在**工作线程跑完之后**才抛：兜底那次归还不得摘掉别人的占位。

    第 16 轮复核的 P1。上面那条用例钉的是"start 完全起不来"（worker 从没跑过），
    这条钉的是另一半：worker 已经跑完并自己归还了，之后 `start()` 才抛
    —— CPython 3.14 的 `Thread.start()` = `_start_joinable_thread` **然后**
    `_started.wait()`，等待被打断是真实可能的形状。这时 `start_packgen` 的
    `except BaseException` 会再归还一次；这中间挤进来的那条同名作业正持有占位，
    按名字硬摘就等于让它和第一条并行建同一个目录（双份 token + 目录互踩）。
    """
    import app.pipeline as plmod
    from app import packgen as pg

    pl, tmp, client = _pipeline()
    slug = pg.preview_slug("探针庚")
    real_start = threading.Thread.start
    seen: dict[str, object] = {}

    def late_boom(self, *a, **k):
        real_start(self, *a, **k)
        if seen.get("done"):
            return
        seen["done"] = True
        self.join()                          # 等工作线程跑完（它自己已经归还）
        seen["b"] = pg.claim_slug(slug)      # 另一个人此刻抢到同一个名字
        raise RuntimeError("start() 在 _started.wait() 上被打断")

    try:
        threading.Thread.start = late_boom
        with pytest.raises(RuntimeError):
            pl.start_packgen("探针庚", "探针用的行业说明文字")
        assert seen["b"], "前置没成立：插进来的那次占位根本没拿到名字"
        assert pg._creating.get(slug) == seen["b"], \
            "活着的同名占位被第一条作业的第二次归还偷走了"
        assert pg.claim_slug(slug) is None, \
            "第三条请求能与 B 并行建同一个目录：占位互斥已经失效"
        pg.release_slug(slug, seen["b"])
    finally:
        threading.Thread.start = real_start
        for s in list(pg._creating):
            pg.release_slug(s, pg.ANY_OWNER)
        shutil.rmtree(tmp, ignore_errors=True)


def test_the_worker_side_release_is_also_token_scoped(monkeypatch):
    """三个归还点里**每条件建包都会走**的那一个（worker 的 finally）也必须认凭证。

    第 18 轮复核 P2-1：把这一处改成 `release_slug(slug, ANY_OWNER)`（按名字硬摘）时
    全量用例全绿 —— 交错用例钉的是入口兜底那一个点。这里把 worker 这一点也钉上：
    同一个交错反过来（A 先还、worker 后还），硬摘同样会摘掉 B 活着的占位。
    """
    import app.pipeline as plmod
    from app.jobs import Job

    pl, tmp, client = _pipeline()
    slug = plmod.preview_slug("探针壬")
    ta = packgen.claim_slug(slug)
    assert ta and packgen.release_slug(slug, ta) is True, "前置：A 占上又还掉"
    tb = packgen.claim_slug(slug)                    # B 现在持有这个名字
    assert tb

    job = Job("pg-late-2", "packgen",
              {"industry": "探针壬", "description": "探针用的行业说明"})
    job.claim_token = ta                             # worker 迟到归还的是 A 那份旧凭证
    pl.add_job(job)

    def fake_create_pack(root, _client, industry, _desc, **_kw):
        raise RuntimeError("模型那边炸了")

    monkeypatch.setattr(plmod, "create_pack", fake_create_pack)
    try:
        pl._run_packgen(job, client, slug)
        assert job.state == "failed", job.state
        assert packgen._creating.get(packgen._table_key(slug)) == tb, \
            "worker 的 finally 把别人活着的占位摘走了（等于回到硬摘）"
        assert packgen.claim_slug(slug) is None, \
            "第三条请求此刻能与 B 并行建同一个目录"
    finally:
        packgen.release_slug(slug, tb)
        shutil.rmtree(tmp, ignore_errors=True)


def test_reserved_or_oversized_names_are_refused_before_any_money_is_spent():
    """Windows 保留设备名与超长行业名：起作业之前就拒掉，一分钱都不花（第 18 轮 P3-4）。

    旧形态：这类名字要等模型返回、走到 `_materialize` 的 mkdir 才失败 ——
    钱花完了、占位拿掉了，用户只看到一句"生成失败"。
    """
    pl, tmp, client = _pipeline()
    try:
        for name in ("CON", "nul.", "com1", "LPT9", "x" * 300):
            with pytest.raises(ValueError) as ei:
                pl.start_packgen(name, "描述文字够长了吧确实够长了")
            assert client.calls == 0, f"{name}：先烧了模型才报错（{client.calls} 次）"
            assert str(ei.value) and "packs" not in str(ei.value), name
            assert packgen.slugify(name) not in packgen._creating, "占位没还或被占上"
        jid = pl.start_packgen("宠物医院", "社区医院，面向养宠家庭")
        while pl.get_job(jid).state not in TERMINAL_STATES:
            time.sleep(0.05)
        assert pl.get_job(jid).state == "done", pl.get_job(jid).error
        assert client.calls == 1, "合法名字被误拒或多烧了一次"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        for s in list(packgen._creating):
            packgen.release_slug(s, packgen.ANY_OWNER)


def test_cancel_after_the_pack_is_written_leaves_no_orphan_dir(monkeypatch):
    """取消落在写盘之后：刚建出来的包必须被回收，否则"已取消"与"下次同名 409"同时成立。

    第 15 轮复核把这条形态钉死：包在盘上、作业记 cancelled、历史又不收 packgen ——
    用户既看不到那个包，也再不能用同一个名字建。回收只准动本次这一个目录。
    """
    import app.pipeline as plmod
    from app.jobs import Job

    pl, tmp, client = _pipeline()
    slug = "探针戊"
    try:
        job = Job("pg-cancel-1", "packgen",
                  {"industry": "探针戊", "description": "探针用的行业说明"})
        pl.add_job(job)

        def fake_create_pack(root, _client, industry, _desc, **_kw):
            (root / "packs" / slug).mkdir(parents=True)
            (root / "packs" / slug / "skill.yaml").write_text("name: x\n", encoding="utf-8")
            job.request_cancel()          # 用户在建完之后、收尾之前点了停止
            return {"name": slug, "display_name": industry}

        monkeypatch.setattr(plmod, "create_pack", fake_create_pack)
        pl._run_packgen(job, client, slug)
        assert job.state == "cancelled", job.state
        assert not (tmp / "packs" / slug).exists(), \
            "取消掉的包留在盘上：下一次同名提交会报「行业包已存在」，而它不在任何列表里"
        assert (tmp / "packs" / "elevator").is_dir(), "回收越界，删到别人的包了"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_cancel_rollback_retries_when_a_file_is_locked(monkeypatch):
    """取消收尾的回收要和建包自己的清理同一次数：文件被瞬时占用时要重试。

    第 16 轮自查量到的不一致：`packgen.create_pack` 失败时走 `rmtree_resilient`
    （注释写明"单次 rmtree 在 Windows 上会被杀软挡掉"），而 `_run_packgen` 的取消
    回收用的是裸 `shutil.rmtree` —— 同一个平台、同一类目录、同一个"下次同名 409"
    后果，却有两本账。这里不新建判据，只把 packgen 那份既有判据接上。
    """
    import app.pipeline as plmod
    from app.jobs import Job

    pl, tmp, client = _pipeline()
    slug = "探针己"
    calls = {"n": 0}
    real_rmtree = shutil.rmtree

    def flaky(path, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("文件被占用（模拟杀毒软件扫描）")
        return real_rmtree(path, **kw)

    monkeypatch.setattr(shutil, "rmtree", flaky)
    try:
        job = Job("pg-cancel-2", "packgen",
                  {"industry": "探针己", "description": "探针用的行业说明"})
        pl.add_job(job)

        def fake_create_pack(root, _client, industry, _desc, **_kw):
            (root / "packs" / slug).mkdir(parents=True)
            (root / "packs" / slug / "skill.yaml").write_text("name: x\n", encoding="utf-8")
            job.request_cancel()
            return {"name": slug, "display_name": industry}

        monkeypatch.setattr(plmod, "create_pack", fake_create_pack)
        pl._run_packgen(job, client, slug)
        assert job.state == "cancelled", job.state
        assert calls["n"] >= 2, f"没走重试（rmtree 只被调用 {calls['n']} 次）"
        assert not (tmp / "packs" / slug).exists(), \
            "第一次被占用就放弃：包留在盘上，下次同名提交报「行业包已存在」"
    finally:
        monkeypatch.undo()
        shutil.rmtree(tmp, ignore_errors=True)


def test_a_failed_reclaim_is_told_to_the_user_not_just_logged(monkeypatch):
    """回收失败必须出现在作业上：只写日志等于用户看不见（第 21 轮复核 P2）。

    后果是确定的：目录留在 `packs/` 里、作业记 cancelled、下次同名提交永久 409，
    而应用里没有删包的入口（`/api/history/{jid}` 是唯一那条 DELETE，删不了包）。
    """
    import app.pipeline as plmod
    from app.jobs import Job

    pl, tmp, client = _pipeline()
    slug = "探针丑"

    def always_locked(path, **kw):
        # 让真的 rmtree_resilient 自己走完重试并返回 False（它的契约就是"失败返回 False"），
        # 而不是替换掉它 —— 替换会把 OSError 直接抛进 `_discard_created_pack`，测的是另一件事。
        raise OSError("文件被占用（模拟杀毒软件一直不放）")

    monkeypatch.setattr(shutil, "rmtree", always_locked)
    try:
        job = Job("pg-cancel-3", "packgen",
                  {"industry": "探针丑", "description": "探针用的行业说明"})
        pl.add_job(job)

        def fake_create_pack(root, _client, industry, _desc, **_kw):
            (root / "packs" / slug).mkdir(parents=True)
            (root / "packs" / slug / "skill.yaml").write_text("name: x\n", encoding="utf-8")
            job.request_cancel()
            return {"name": slug, "display_name": industry}

        monkeypatch.setattr(plmod, "create_pack", fake_create_pack)
        pl._run_packgen(job, client, slug)
        assert job.state == "cancelled", job.state
        assert (tmp / "packs" / slug).is_dir(), "前置：删不掉才要测文案"
        told = " ".join(s["title"] for s in job.steps)
        assert "未能回收" in told and slug in told, \
            f"回收失败只进了日志，界面上看不出为什么下次同名建不了：{told!r}"
    finally:
        monkeypatch.undo()
        shutil.rmtree(tmp, ignore_errors=True)


def test_second_same_name_is_refused_before_any_token_is_spent():
    pl, tmp, client = _pipeline()
    try:
        jid = pl.start_packgen(INDUSTRY, DESC)
        deadline = time.time() + 2
        while client.calls == 0 and time.time() < deadline:
            time.sleep(0.02)
        with pytest.raises(FileExistsError) as ei:
            pl.start_packgen(INDUSTRY, DESC)
        assert "正在创建中" in str(ei.value)
        # 第二条**没起作业**：注册表里只有一条 packgen
        pg = [j for j in pl.registry.snapshots() if j["kind"] == "packgen"]
        assert len(pg) == 1, f"第二条也被放行了：{len(pg)} 条建包作业"
        # 等第一条收尾，确认模型只被打了一次
        while pl.get_job(jid).state not in TERMINAL_STATES:
            time.sleep(0.05)
        assert client.calls == 1, "同名并发把模型打了两次 = 双份 token"
        assert pl.get_job(jid).state == "done", pl.get_job(jid).error
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _wait_released(slug: str, timeout: float = 5.0) -> bool:
    """取消是**协作式**的：线程还卡在模型里时占位本就该继续持有，
    要等它自己走到检查点走出来才归还。所以这里等"释放"而不是等"状态"。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if slug not in packgen._creating:
            return True
        time.sleep(0.05)
    return False


def test_claim_is_released_on_every_exit_path():
    """成功、失败、取消都得归还名字，否则这个 slug 从此永远建不了。"""
    pl, tmp, client = _pipeline()
    slug = preview_slug(INDUSTRY)
    try:
        jid = pl.start_packgen(INDUSTRY, DESC)
        pl.cancel(jid)                        # 模型还在睡的时候取消
        assert _wait_released(slug), "取消路径没归还占位"
        assert pl.get_job(jid).state == "cancelled"
        # 归还之后可以重新建（这次让它跑完）
        jid2 = pl.start_packgen(INDUSTRY, DESC)
        while pl.get_job(jid2).state not in TERMINAL_STATES:
            time.sleep(0.05)
        assert pl.get_job(jid2).state == "done", pl.get_job(jid2).error
        assert slug not in packgen._creating
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        packgen.release_slug(slug, packgen.ANY_OWNER)


def test_failed_build_also_gives_the_name_back(monkeypatch):
    """模型调用抛错（令牌过期那类）之后同一个名字必须能再建。"""
    pl, tmp, client = _pipeline()
    slug = preview_slug(INDUSTRY)

    def boom(self, task, system, user, model_cls, **kw):
        self.calls += 1
        raise RuntimeError("令牌已过期")
    monkeypatch.setattr(SlowMock, "chat_json", boom)
    try:
        jid = pl.start_packgen(INDUSTRY, DESC)
        while pl.get_job(jid).state not in TERMINAL_STATES:
            time.sleep(0.05)
        assert pl.get_job(jid).state == "failed", pl.get_job(jid).state
        assert _wait_released(slug), "失败路径没归还占位"
        assert "令牌已过期" in (pl.get_job(jid).error or "")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        packgen.release_slug(slug, packgen.ANY_OWNER)


def test_quota_rejection_also_gives_the_name_back():
    """额度满 → 作业根本没起，占位必须当场归还。

    这条最容易漏：占位现在发生在额度检查**之前**（不这样拦不住同名并发），
    于是 409 那条出口是一个新增的、以前不存在的返回路径。
    """
    pl, tmp, client = _pipeline()
    slug = preview_slug(INDUSTRY)
    try:
        # 把 4 个额度占满（用同名之外的行业，避免撞名检查）
        for i in range(4):
            s = f"占位行业{i}"
            jid = pl.start_packgen(s, "描述文字够长了吧")
            assert jid
        with pytest.raises(Exception) as ei:
            pl.start_packgen(INDUSTRY, DESC)
        assert "上限" in str(ei.value)
        assert slug not in packgen._creating, "额度满那条出口漏了归还占位"
    finally:
        for s in list(packgen._creating):
            packgen.release_slug(s, packgen.ANY_OWNER)
        shutil.rmtree(tmp, ignore_errors=True)


def slug_of(client) -> str:
    return preview_slug(INDUSTRY)


def _fs_same_dir(a: str, b: str) -> bool:
    """问**文件系统本身**：这两个名字在本机是不是同一个目录（不是问我们的表）。"""
    root = Path(tempfile.mkdtemp(prefix="fssame-"))
    try:
        (root / a).mkdir()
        try:
            (root / b).mkdir()
            return False
        except FileExistsError:
            return True
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_claim_and_release_are_pairwise():
    s = "某个临时占位"
    ta = packgen.claim_slug(s)
    assert ta, "第一次占位就该成功"
    assert packgen.claim_slug(s) is None          # 自己占着的，第二条占不到
    # 凭证不对就摘不掉 —— 这一格就是第 16 轮那条 P1 的最小形状
    assert packgen.release_slug(s, "") is False, "忘带凭证等于无条件 discard"
    assert packgen.release_slug(s, "不是那一份") is False
    assert s in packgen._creating, "失败的归还把占位弄丢了"
    assert packgen.release_slug(s, ta) is True
    tb = packgen.claim_slug(s)
    assert tb and tb != ta, "两次占位发的是同一个凭证，认不出谁是谁"
    # 同一份凭证叫第二次：空操作，且**不会**误伤 B 的新占位
    assert packgen.release_slug(s, ta) is False
    assert packgen._creating.get(s) == tb, "A 的第二次归还偷走了 B 的占位"
    assert packgen.release_slug(s, tb) is True
    assert packgen.claim_slug(""), "空 slug 交给正式判定，不占位"
    assert packgen.claim_slug("") == packgen.NO_CLAIM
    assert "" not in packgen._creating, "NO_CLAIM 进表了：那个键谁也删不掉（含兜底清理）"

    # 表的键必须与"本机文件系统是否把这两个名字当成同一个目录"同域。
    # 判据问的是文件系统（`_fs_same_dir` 真去 mkdir 一次），不是问我们的表 —— 否则自证。
    # Windows 不分大小写；POSIX 分；而 `casefold()` 比文件系统**宽**：
    # ß/ẞ、σ/ς、ﬁ/fi 在 NTFS 上是三个不同的目录，用 fold 会把这三个合法行业名
    # 误报成「行业包正在创建中」（实测 10 组难缠名字里 7 组与文件系统不一致）。
    for a, b in (("Probe-Case", "probe-case"), ("ß包", "ẞ包"), ("σα", "ςα"), ("ﬁn", "fin")):
        same = _fs_same_dir(a, b)
        ta2 = packgen.claim_slug(a)
        assert ta2, a
        tb2 = packgen.claim_slug(b)
        assert (tb2 is None) == same, (
            f"{a} / {b}：文件系统说两者是同一个目录吗 = {same}，"
            f"占位表却说互斥 = {tb2 is None} —— 表键要用 os.path.normcase，不是 casefold")
        packgen.release_slug(a, ta2)
        if tb2:
            packgen.release_slug(b, tb2)
    # 归还时换一种大小写写法也要认得（同一份凭证、同一个归一化键）
    tz = packgen.claim_slug("Probe-Case")
    assert tz
    same_case = packgen._table_key("Probe-Case") == packgen._table_key("PROBE-CASE")
    assert packgen.release_slug("PROBE-CASE", tz) is same_case, "归还的键归一化不一致"
    if same_case:
        assert packgen._creating == {}, "兜底归还之后表没清干净"
    # 凭证不能"匹配上表里根本没有的项"：`_creating.get(k)` 对缺项返回 None，
    # 于是 owner=None 会走进删除分支、紧接着 del 抛 KeyError
    #（第 18 轮复核复量 P1-2 时当场撞出来的一格）。
    assert packgen.release_slug("从来没有这个包", None) is False
    assert packgen.release_slug("从来没有这个包", "") is False
    te = packgen.claim_slug("凭证边界")
    assert packgen.release_slug("凭证边界", te) is True
    assert packgen.release_slug("凭证边界", None) is False, "重复归还应该只是 False，不是抛错"
    assert packgen._creating == {}


def test_the_claim_table_collapses_exactly_what_the_filesystem_collapses():
    """表键要与"本机文件系统是不是把这两个目录当成同一个"**实测**一致（第 20 轮 Q1）。

    判据的ground truth 是文件系统（真去 mkdir 一次），不是 `_table_key` 自己 —— 否则
    就是拿实现证明实现。方向上两种不一致都要报：
    - 文件系统合并而表不合并 = §15.32 那条 P1-2 的形状（同名两条作业并行建一个目录）；
    - 表合并而文件系统不合并 = 误报「行业包正在创建中」，第二个合法名字建不出来。
    今天这一格是绿的，靠的是 `slugify` 恰好把 NTFS 会忽略的那些字符（尾部点、空格、
    全角空格）都折掉；哪天有人放宽 `slugify`（允许 `.` 或 `~`），这条会立刻红。
    """
    lookalikes = [("a", "a."), ("a", "a "), ("a.", "a "), ("甲", "甲　"),
                  ("Probe Case", "probe case"), ("CON", "con"),
                  ("宠物 医院", "宠物-医院"), ("电梯包", "电梯包"),
                  ("x. ", "x"), ("全屋定制/装修", "全屋定制 装修")]
    loose, strict = [], []
    for a, b in lookalikes:
        sa, sb = preview_slug(a), preview_slug(b)
        assert sa and sb, f"前置：这对名字要都能起出目录名（{a!r}->{sa!r} {b!r}->{sb!r}）"
        fs_same = _fs_same_dir(sa, sb)
        key_same = packgen._table_key(sa) == packgen._table_key(sb)
        if fs_same and not key_same:
            loose.append((a, b, sa, sb))
        if key_same and not fs_same:
            strict.append((a, b, sa, sb))
    assert not loose, f"文件系统当成同一个目录、表却分作两个（并行双建）：{loose}"
    assert not strict, f"表合并而文件系统不合并（误报 409，合法名字建不出来）：{strict}"


def test_the_claim_table_agrees_with_the_filesystem_even_before_slugify():
    """绕开 `slugify` 的巧合，直接钉"表键 == 本机文件系统对目录名的看法"（第 20 轮 P3-2）。

    上面那条不变式今天能绿，一部分功劳在 `slugify` 恰好把尾部的点与空格折掉；
    那等于把安全性押在别人的一句话上。这里喂原始名字，两个方向都不许偏：
    Windows 上 `a.` / `a ` / `a` 是同一个目录（表也必须并作一个键），
    POSIX 上是三个不同目录（表也不许提前合并，那是误报 409）。
    """
    for a, b in (("a", "a."), ("a", "a "), ("a.", "a "), ("甲.", "甲")):
        fs_same = _fs_same_dir(a, b)
        key_same = packgen._table_key(a) == packgen._table_key(b)
        assert key_same == fs_same, f"{a!r}/{b!r}：表说同键={key_same}，文件系统说同一目录={fs_same}"
        ta = packgen.claim_slug(a)
        tb = packgen.claim_slug(b)
        assert ta
        assert (tb is None) == fs_same, f"{a!r}/{b!r}：占位互斥与本机的目录名不一致"
        packgen.release_slug(a, ta)
        if tb:
            packgen.release_slug(b, tb)
        packgen.release_slug(a, packgen.ANY_OWNER)


def test_the_slug_length_cap_has_one_authority_and_the_message_shows_what_the_user_typed():
    """目录名长度上限不许有第二本账；保留名的说明要回显用户输入（第 20 轮 P3-5 / P3-6）。"""
    from app.schemas import INDUSTRY_MAX

    assert packgen._max_slug_units() == INDUSTRY_MAX, \
        "packgen 又自己写了一个长度上限 —— 与请求层那处会各自漂移（原来是 120 vs 40）"
    # 走 HTTP 到不了这个分支（schema 先拒），但 CLI / 直调 pipeline 能到：判据必须真在
    assert packgen.slug_problem("x" * (INDUSTRY_MAX + 1)), "超过上限却没人挡"
    assert packgen.slug_problem("x" * INDUSTRY_MAX) == "", "边界内的名字被误伤"
    msg = packgen.slug_problem("CON", "CON.")
    assert "CON." in msg, f"报错只回显折叠后的目录名，用户会对不上自己输入的那串字：{msg}"
    assert "保留设备名" in msg, msg


def test_slugify_never_grows_the_name_the_request_layer_capped():
    """`INDUSTRY_MAX` 能同时当目录名上限，靠的是"折叠不会把名字折长"这条性质。

    它一点也不显然：`slugify` 现在是"连续非法字符折成一个 `-` + 去首尾 `-`"，
    只会等长或变短，所以上一条测试才敢说"走 HTTP 到不了长度分支"。
    谁往里面加转写（`ä`→`ae`、emoji→名字、繁简转换），请求层放行的 40 码元
    就能折出更长的目录名 —— 那时唯一还守着的判据是 `slug_problem`，而它只在
    直调路径上守（HTTP 已经被 schema 放行）。这一条把性质本身钉住。
    """
    from app.schemas import utf16_units

    corpus = [chr(c) for c in range(1, 0x10000) if not 0xD800 <= c <= 0xDFFF]
    corpus += [chr(c) for c in range(0x10000, 0x110000, 997)]
    hazards = [" ", "-", "_", ".", "？", "…", "\U0001F600", "ä", "　",
               "0", "中", "\n", "\t", "/", "\\", "*", ":"]
    rnd = random.Random(20260922)
    corpus += ["".join(rnd.choice(hazards) for _ in range(rnd.randint(1, 9)))
               for _ in range(4000)]
    grew = [(s, packgen.slugify(s)) for s in corpus
            if utf16_units(packgen.slugify(s)) > utf16_units(s)]
    assert not grew, (f"slugify 把名字折长了 {len(grew)} 例（共扫 {len(corpus)} 例）："
                      "请求层的上限不再约束目录名，长度判据得挪到建包路径上重做"
                      f" —— 前几例 {grew[:4]}")


def _stub_result_keys():
    """从 `_verify/verify.js` 里抠出 `PG_RESULT` 的顶层键（不抄第二份清单）。

    ⚠ 不能按"行首缩进 + 键名"匹配：`name/display_name/dir/draft` 四个键写在同一行，
    行首匹配只抠到 `name` —— 第一版就是这么把一条好守卫写成假红的（提取器 bug 会被
    误读成"桩与服务端不一致"，那种误判比漏判更费下一轮的时间）。
    """
    src = (ROOT / "_verify" / "verify.js").read_text(encoding="utf-8")
    i = src.index("var PG_RESULT = {")
    block = src[i:src.index("\n  };", i)]
    return set(re.findall(r"\b([a-z][a-z_]{1,})\s*:", block))


def _renderer_packgen_result_reads():
    """抠出渲染层**读建包产物**时用的字段名（`out.xxx`），不是全文件的 `out.`。

    ⚠ 必须按消费点裁剪：`settings.js` 里另有一个 `out = await api.saveModel(...)`，
    它读的 `out.id` / `out.warnings` 属于模型保存接口 —— 全文件扫会把那两个也拉进
    建包的对账里，报一条假红（第一版就这样）。
    ⚠ 每个锚点只准出现一次：改名/搬函数会让这里直接 ValueError，而不是安静地
    去比另一处同名代码。
    """
    js = (ROOT / "desktop" / "renderer" / "js" / "settings.js").read_text(encoding="utf-8")
    read = set()
    for anchor in ("const out = snap.result || {};", "function renderPackgenSummary(out) {"):
        assert js.count(anchor) == 1, f"锚点「{anchor}」出现 {js.count(anchor)} 次，判据不能再定位消费点"
        i = js.index(anchor)
        read |= set(re.findall(r"\bout\.([a-z_]+)", js[i:js.index("\n}", i)]))
    return read


def test_packgen_result_keys_agree_across_engine_stub_and_renderer():
    """建包产物的字段名：引擎、UI 桩、渲染层三方必须同一本账（第 22 轮自查）。

    快照的**键集**已有 §15.32/§15.33 两条守着，但 `result` 里面那层没有：
    引擎把 `banwords_extra_hard` 改成 `banwords_hard`，桩照旧发旧名、门照样绿，
    而真界面上摘要那几节直接空掉 —— 界面上"看起来成功、内容不见了"是最难查的一种。
    渲染层读到的键也一并量：它读了引擎不发的键，就等于在读 `undefined`。
    """
    pl, tmp, client = _pipeline()
    try:
        jid = pl.start_packgen(INDUSTRY, DESC)
        deadline = time.time() + 20
        while time.time() < deadline and pl.get_job(jid).state not in TERMINAL_STATES:
            time.sleep(0.05)
        job = pl.get_job(jid)
        assert job.state == "done", job.error
        engine_keys = set(job.result or {})
        assert engine_keys, "引擎没返回任何产物字段，这条对账会假绿"

        stub_keys = _stub_result_keys()
        assert stub_keys, "桩里抠不出 PG_RESULT 的键：判据本身要先改"
        assert stub_keys == engine_keys, (
            f"桩的建包产物字段与服务端不一致 只在桩={sorted(stub_keys - engine_keys)} "
            f"只在引擎={sorted(engine_keys - stub_keys)}")

        read = _renderer_packgen_result_reads()
        assert read, "渲染层一处 `out.` 都没抠到：锚点或提取器先失效了，这条会假绿"
        unknown = read - engine_keys
        assert not unknown, f"渲染层在读引擎不发的字段（拿到的是 undefined）：{sorted(unknown)}"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _stub_slug():
    """把 `_verify/verify.js` 里的 pgSlug 抠出来（不抄第二份实现）。"""
    src = (ROOT / "_verify" / "verify.js").read_text(encoding="utf-8")
    i = src.index("var BS = String.fromCharCode(92);")
    j = src.index("function pgSlug(t) {", i)
    k = src.index("\n  }", j) + len("\n  }")
    body = src[i:k]
    # 桩与引擎的判据必须同域；反斜杠在这里会被模板字符串吃掉一层，
    # 所以只准用 String.fromCharCode(92) 拼属性类，不准出现裸反斜杠。
    assert "p{L}" in body and "p{N}" in body, "桩没在用 Unicode 属性类，多半又退回手写区段表"
    assert "\\" not in body, "桩里出现了裸反斜杠（注进页面会被吃掉一层）"
    return body


def _stub_name_ok():
    """抠出桩里的包名校验（NAME_OK + packNameOk），不抄第二份实现。"""
    src = (ROOT / "_verify" / "verify.js").read_text(encoding="utf-8")
    i = src.index("var NAME_OK = new RegExp(")
    j = src.index("function packNameOk(n) {", i)
    k = src.index("\n  }", j) + len("\n  }")
    body = src[i:k]
    assert "String.fromCharCode(92)" in body, "桩的包名判据没走属性类（手写区段表的老路）"
    return body


def test_stub_pack_name_check_agrees_with_the_engine():
    """桩的"包名合法吗"必须与 `app/server.py` 的 `_safe_name` 同一判据。

    第 12 轮复核定性：原来它复用 `pgSlug(n) === n` 当校验，那**两个方向都错** ——
    `-a`、`a--b`、`ab-` 在引擎里合法（单词字符类加连字符），被 slugify 削首尾后
    不再相等 → 桩替引擎 400；反过来 Cn 类码点引擎 400、桩放行。
    这里逐码点 + 逐形状对账，唯一允许的差异是两个运行时各自的 Unicode 数据版本差
    （同一个测试里现算，不是抄来的清单）；除此之外任何一条不一致都红。
    """
    import json
    import re
    import subprocess

    node = shutil.which("node")
    if not node:
        pytest.skip("没有 node，跨语言对账跑不了")
    from app.server import _safe_name

    cps = [cp for cp in range(0x21, 0x2000) if not 0xD800 <= cp <= 0xDFFF] \
        + list(range(0x4E00, 0x4E20)) + [0x1D400, 0x1F600, 0x2C2F, 0x10000]
    names = [chr(cp) for cp in cps] \
        + ["-a", "a-", "a--b", "ab-", "-", "--", "a.b", "a/b", "..", "x", "电梯_a-1",
           "全屋定制-装修", "２３D打印",
           # 尾部空白这一族：Python 的 `$` 放过一个结尾换行、JS 的不放（第 16 轮实测）。
           # 引擎侧现在用 fullmatch，两边都拒 —— 改回 `match` + `$` 这条会红。
           "elevator\n", "宠物医院\n", "elevator ", "a\nb", "\nelevator"]
    tmp = Path(tempfile.mkdtemp())
    try:
        (tmp / "in.json").write_text(json.dumps(names, ensure_ascii=False), encoding="utf-8")
        (tmp / "name.js").write_text(_stub_name_ok() + "\nmodule.exports = packNameOk;\n",
                                     encoding="utf-8")
        (tmp / "run.js").write_text(
            "const fs = require('fs');\n"
            + "const ok = require(process.argv[2]);\n"
            + "const ins = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));\n"
            + "const BS = String.fromCharCode(92);\n"
            + "const W = new RegExp('[' + BS + 'p{L}' + BS + 'p{N}_]$', 'u');\n"
            + "process.stdout.write(JSON.stringify({ ok: ins.map(function (s) { return ok(s); }),\n"
            + "  word: ins.map(function (s) { return W.test(s); }),\n"
            + "  accept: (function () { const all = [];\n"
            + "    for (let cp = 0x21; cp <= 0x10FFFF; cp++) {\n"
            + "      if (cp >= 0xD800 && cp <= 0xDFFF) continue;\n"
            + "      if (ok(String.fromCodePoint(cp))) all.push(cp); }\n"
            + "    return all; })() }));\n",
            encoding="utf-8")
        r = subprocess.run([node, str(tmp / "run.js"), str(tmp / "name.js"), str(tmp / "in.json")],
                           capture_output=True, text=True, encoding="utf-8")
        assert r.returncode == 0, r.stderr[:300]
        out = json.loads(r.stdout)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    def engine_ok(n):
        """问生产函数本身，不在测试里重抄一遍判据。

        原来这里是 `bool(_NAME_RE.match(n)) and ".." not in n` —— 那是对服务端判据的
        **第二次抄写**：服务端把 `match` 换成 `fullmatch`、或把 `..` 规则挪走，
        这份副本不会跟着变，对账就成了"我的判据 vs 桩的判据"（两本账）。
        """
        try:
            _safe_name(n)
            return True
        except Exception:
            return False

    diff = [(n, engine_ok(n), bool(o), bool(w)) for n, o, w in zip(names, out["ok"], out["word"])
            if engine_ok(n) != bool(o)]
    # 唯一可解释的差异：这个**单字符**在 Python 的 \w 与 V8 的 p{L}/p{N}/_ 之间不一致
    # （即两个运行时的 Unicode 数据版本差），且方向必须是"桩比引擎宽"。
    # 反过来（引擎合法、桩拒）永远不可接受 —— 那是桩自己造的一次拒绝，界面上
    # "点了一个真引擎会接受的包"这条路就再也量不到了。
    unjustified = [(n, e, s) for n, e, s, w in diff
                   if not (len(n) == 1 and not re.fullmatch(r"\w", n) and w)
                   or not (e is False and s is True)]
    assert not unjustified, ("桩的包名判定与服务端不一致（前 8 条 名字/引擎/桩）：\n"
                             + "\n".join(f"  {n!r}: 引擎={e} 桩={s}" for n, e, s in unjustified[:8]))
    strict = [(n, e, s) for n, e, s, w in diff if e and not s]
    assert not strict, ("桩比引擎严（引擎合法的包名被桩拒了）—— 这类差异会让界面永远"
                        "量不到「点了一个引擎真会接受的包」：\n"
                        + "\n".join(f"  {n!r}: 引擎={e} 桩={s}" for n, e, s in strict[:8]))

    # 逐码点对账**扫到全非代理区**（第 18 轮复核 P2-6：原来只扫到 0x2000，
    # 于是 BMP 以上那 4600+ 个差异今天没有任何人量过，我还在报告里把窗口里的数当成了全库的数）。
    # 一次 node 扫描 ~60ms、一次 Python 全量 `_safe_name` ~0.8s，跑得动就别只跑一个窗口。
    stub_yes = set(out["accept"])
    eng_yes = set()
    compared = 0
    for cp in range(0x21, 0x110000):
        if 0xD800 <= cp <= 0xDFFF:
            continue                       # 代理区不能单独成为一个字符
        compared += 1
        try:
            _safe_name(chr(cp))
            eng_yes.add(cp)
        except Exception:
            pass
    # 对齐哨兵：桩那侧的枚举是"跳过代理区后压进数组"，Python 这侧独立重新枚举 ——
    # 任何一侧的索引/范围写错，这个数就会变（我自己写错过一次：用 enumerate 反推码点，
    # 跳过代理区之后整体错位，量出来的"差异"从 4657 变成 29369）。
    assert compared == 1112031, f"逐码点扫描的范围不对（比较了 {compared} 个码点）"
    assert len(stub_yes) > 100000, f"桩那侧一个字母表都没接受？扫描本身坏了：{len(stub_yes)}"
    only_eng = sorted(eng_yes - stub_yes)
    assert not only_eng, (
        "引擎接受而桩拒的单字符包名（不可接受的方向）："
        + ", ".join(f"U+{c:04X}" for c in only_eng[:8]))
    only_stub = stub_yes - eng_yes
    cats = {unicodedata.category(chr(c)) for c in only_stub}
    assert only_stub, (
        "两侧现在逐码点完全一致 —— 这条对账在本机上已经空转，"
        "要么把判据换成'不许出现引擎更宽'之外的事实，要么删掉它，别留着假装在测")
    assert cats <= {"Cn"}, (
        f"桩比引擎宽的 {len(only_stub)} 个码点里出现了已分配的类别 {sorted(cats)} —— "
        "那就不是两个运行时 Unicode 版本差可以解释的了，判据本身要重看")


def test_stub_slug_agrees_with_the_engine_over_a_unicode_sweep(tmp_path):
    """建包桩算出的目录名，必须与 `preview_slug` 逐字符一致。

    这条是第 8 轮复核逼出来的：手写区段表在 218 个码点里错了 62 个
    （`ª µ ² ə ᄒ 々 ﬀ` 与一切 astral 字符被漏，`× ÷ ＿` 与组合记号被错留），
    后果是桩会造出引擎根本不会给的 409/400（`×` 在引擎里目录名为空 → 400 且不占位）。
    与其再抄一份表，不如让两边**每次改动都被机器对一遍** —— 与
    test_job_state_vocabulary_consistency.py 读两份文件比常量的做法同一类。
    """
    import json
    import subprocess

    node = shutil.which("node")
    if not node:
        pytest.skip("没有 node，跨语言对账跑不了")
    # ⚠ 第 12 轮把"清单只作减法 + 每条自证"这套判据证伪了两次：
    #   (a) 豁免支实际是空跑 —— 全 BMP 扫下来没有任何多字符输入含分裂码点，所以
    #       "含分裂码点的整串也必须一致"这件事从没量过；实测 `猫咖+U+088F+甲`
    #       引擎给 猫咖-甲、桩给 猫咖᠏甲（桩占住一个引擎根本不会用的目录名）。
    #   (b) 判据是 `not any(ch in single)`，而 single 恰好等于整张清单，于是"每个
    #       单字符各红一次"被结构性地排除 —— 清单里的字符从不出现在 real-world 串里。
    #   修法：这张表**归桩所有**（下面的 PG_SPLIT_CP），测试从桩里读它，并且双向对账：
    #   "Python 认为非单词而 V8 认为单词"的码点集合必须恰好等于这张表。两边各自
    #   升级 Unicode 数据都会红，加条目、删条目、清单烂掉都不再是静默的。
    stub_body = _stub_slug()
    m = re.search(r"PG_SPLIT_CP\s*=\s*\[([^\]]*)\]", stub_body)
    assert m, "桩里没有 PG_SPLIT_CP 这张表了 —— 跨语言对账失去共同事实源"
    split_chars = {chr(int(tok.strip(), 16)) for tok in m.group(1).split(",") if tok.strip()}
    assert split_chars, "桩里的折叠表是空集：那 V8/CPython 的分歧必须由整串对账兜住"
    cps = [cp for cp in range(0x21, 0xFFFF) if not 0xD800 <= cp <= 0xDFFF] \
        + list(range(0x1D400, 0x1D560, 5)) + [0x10000, 0x20000, 0x1F600, 0x2C2F]
    inputs = [chr(cp) for cp in cps]
    # 几条真实的"同目录不同写法"，碰撞语义要靠多字符才量得到
    inputs += ["全屋定制/装修", "全屋定制 装修", "宠物医院", "？？？", "!!!",
               "café 机电", "３D打印", "ひらがな诊所", "한국어", "𝕬宠物"]
    # 整串对账：分裂码点拼进真词、以及首尾/连续分隔符这些折叠语义，全部喂进同一轮
    combos = [f"猫咖{ch}甲" for ch in sorted(split_chars)] \
        + [f"{ch}宠物" for ch in sorted(split_chars)] \
        + [f"宠物{ch}" for ch in sorted(split_chars)] \
        + ["宠物医院-", "-宠物医院", "宠物--医院", "宠物 医院", "宠物-医院",
           "ª宠物", "宠物ª", "ß维保", "２３D打印"]
    inputs += [s for s in combos if s not in inputs]
    (tmp_path / "in.json").write_text(json.dumps(inputs, ensure_ascii=False), encoding="utf-8")
    (tmp_path / "slug.js").write_text(_stub_slug() + "\nmodule.exports = pgSlug;\n",
                                      encoding="utf-8")
    (tmp_path / "run.js").write_text(
        "const fs = require('fs');\n"
        + "const pgSlug = require(process.argv[2]);\n"
        + "const ins = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));\n"
        + "const BS = String.fromCharCode(92);\n"
        + "const WRE = new RegExp('[' + BS + 'p{L}' + BS + 'p{N}_]$', 'u');\n"
        + "process.stdout.write(JSON.stringify({ slugs: ins.map(s => pgSlug(s)),\n"
        + "  word: ins.map(s => WRE.test(s)) }));\n", encoding="utf-8")
    r = subprocess.run([node, str(tmp_path / "run.js"), str(tmp_path / "slug.js"),
                        str(tmp_path / "in.json")],
                       capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, r.stderr[:300]
    out = json.loads(r.stdout)
    got, node_word = out["slugs"], out["word"]
    bad = [(s, preview_slug(s), g) for s, g in zip(inputs, got) if preview_slug(s) != g]
    # ⚠ 整串必须一致，**没有豁免支**（第 12 轮把原来那条"有证人就免"的判据证伪了：
    #   single 恰好等于整张清单，所以含清单码点的串必然被免掉，逐字符那层又全绿 ——
    #   于是 猫咖+U+088F+甲 这种真分歧穿着"有证人"的外衣过去了）。
    assert not bad, ("建包桩与引擎的目录名出现分歧（前 8 条）：\n"
                     + "\n".join(f"  {s!r}: 引擎={a!r} 桩={b!r}" for s, a, b in bad[:8]))

    def _cp(ch):
        return " ".join(f"U+{ord(c):04X}" for c in ch)

    # 双向对账这张折叠表：Python 的 \w 不认、V8 的 p{L}/p{N}/_ 认 —— 这些码点必须
    # **恰好**是桩里折掉的那些。表多一条（把两边本来一致的字符折成 '-'）与少一条
    # （放任一个真分歧）都红；Node/CPython 谁升级了 Unicode 数据也会红。
    divergent = {inputs[i] for i in range(len(inputs))
                 if len(inputs[i]) == 1
                 and re.fullmatch(r"\w", inputs[i]) is None
                 and node_word[i]}
    only_table = sorted(split_chars - divergent)
    only_real = sorted(divergent - split_chars)
    assert not only_real, ("这些码点两侧行为不同，桩却没折它们（前 8 个）："
                           + ", ".join(_cp(c) for c in only_real[:8]))
    assert not only_table, ("桩的折叠表里有码点两侧其实已经一致 —— 白拿一次差异，"
                            "删掉它（前 8 个）：" + ", ".join(_cp(c) for c in only_table[:8]))
    for ch in sorted(split_chars):
        assert preview_slug(ch) == "", \
            f"表里的 U+{ord(ch):04X} 在引擎侧并不是「折成空」，这条理由站不住：删掉它"
    # 真实用例的语义还得对得上：两种写法同一个目录名、纯符号起不出名字
    key = {s: g for s, g in zip(inputs, got)}
    assert key["全屋定制/装修"] == key["全屋定制 装修"] == "全屋定制-装修"
    assert key["？？？"] == "" and key["!!!"] == ""
    assert key["한국어"] == "한국어" and key["𝕬宠物"] == "𝕬宠物"


def test_cancel_landing_after_the_last_check_also_reclaims(monkeypatch):
    """取消落在最后一次 `_stop_check` 通过**之后**（transition("done") 被拒的
    微秒窗口）：与 `except JobCancelled` 分支**同一本账** —— 本次建出来的包
    同样回收（P2-1）。

    曾经这条路径裸 `return`：包留在盘上、作业记 cancelled，删/留取决于
    毫秒级竞态，而两个收尾分支的注释各执一词。
    """
    import app.pipeline as plmod
    from app.jobs import Job

    pl, tmp, client = _pipeline()
    slug = "探针寅"
    try:
        job = Job("pg-cancel-4", "packgen",
                  {"industry": "探针寅", "description": "探针用的行业说明"})
        pl.add_job(job)
        real_transition = job.transition

        def cancel_then_refuse(to_state, **kw):
            if to_state == "done":
                # 模拟取消恰好落进「_stop_check 通过之后、done 迁移之前」的窗口：
                # request_cancel 把状态落成 cancelled，real_transition 返回 False
                job.request_cancel()
            return real_transition(to_state, **kw)

        monkeypatch.setattr(job, "transition", cancel_then_refuse)

        def fake_create_pack(root, _client, industry, _desc, **_kw):
            (root / "packs" / slug).mkdir(parents=True)
            (root / "packs" / slug / "skill.yaml").write_text("name: x\n", encoding="utf-8")
            return {"name": slug, "display_name": industry}

        monkeypatch.setattr(plmod, "create_pack", fake_create_pack)
        pl._run_packgen(job, client, slug)
        assert job.state == "cancelled", job.state
        assert not (tmp / "packs" / slug).exists(), \
            "这条收尾路径把包留在了盘上：与 JobCancelled 分支两本账（P2-1）"
    finally:
        monkeypatch.undo()
        shutil.rmtree(tmp, ignore_errors=True)


def test_request_cancel_refuses_terminal_states():
    """P3-1 的 TOCTOU 收口：终态作业不许再被翻成 cancelled。

    pipeline.cancel() 的终态检查是无锁读，与 request_cancel 的置位之间有空窗
    —— 若 done 迁移恰好插在中间，一份已完整落盘的产物会在内存里被翻成
    cancelled。判终态与置位必须在同一把锁里（现在锁内拒绝）。
    """
    from app.jobs import Job
    j = Job("t-cancel-terminal", "generate", {})
    j.state = "done"                       # 直接落终态（绕过 TRANSITIONS）
    j.request_cancel()
    assert j.state == "done", "终态作业被 request_cancel 翻成了 cancelled"
    assert not j.is_cancelled(), "终态作业不该被置取消标志"
