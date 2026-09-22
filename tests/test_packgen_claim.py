# -*- coding: utf-8 -*-
"""P2-46 后半：同名建包要在**花钱之前**被拦住。

修复前的形态：入口只有 `(packs/slug).exists()` 这一道同步检查，而目录要到模型
返回之后才建 —— 于是两条同名请求双双通过检查、双双起作业，双份 token，
第二条还要等一两分钟、以"作业失败"的样子告诉用户重名了。
`_creating` 的占用原来也发生在模型调用之后，等于没占。
"""
from __future__ import annotations

import re
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
