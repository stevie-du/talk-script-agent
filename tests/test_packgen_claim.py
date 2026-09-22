# -*- coding: utf-8 -*-
"""P2-46 后半：同名建包要在**花钱之前**被拦住。

修复前的形态：入口只有 `(packs/slug).exists()` 这一道同步检查，而目录要到模型
返回之后才建 —— 于是两条同名请求双双通过检查、双双起作业，双份 token，
第二条还要等一两分钟、以"作业失败"的样子告诉用户重名了。
`_creating` 的占用原来也发生在模型调用之后，等于没占。
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import threading
import time
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
        packgen.release_slug(slug)


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
        packgen.release_slug(slug)


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
            packgen.release_slug(s)
        shutil.rmtree(tmp, ignore_errors=True)


def slug_of(client) -> str:
    return preview_slug(INDUSTRY)


def test_claim_and_release_are_pairwise():
    s = "某个临时占位"
    assert packgen.claim_slug(s) is True
    assert packgen.claim_slug(s) is False       # 自己占着的，第二条占不到
    packgen.release_slug(s)
    assert packgen.claim_slug(s) is True
    packgen.release_slug(s)
    assert packgen.claim_slug("") is True       # 空 slug 交给正式判定，不占位
