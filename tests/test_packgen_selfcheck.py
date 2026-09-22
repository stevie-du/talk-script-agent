# -*- coding: utf-8 -*-
"""建包自体检的第三条腿：模板占位符对账（P1-44 缺的那格）。

`safe_substitute` 对认不出的 `$foo` **原样保留** —— 模型收到一段字面量，
而本该从那格里注入的知识一个字都没有。生成期这条会在作业日志里留一步
`tpl_*`，建包这一步却什么都不说：拼错的占位符可以一路静默活到第一次付费生成。

`_pack_audit` 原来只有两条腿（`Pack()` 加载 + `param_audit` 全量）。
这里钉三件事：
  1. 新建的包体检**不该有**占位符问题（模板本身是对的）；
  2. 把模板里的占位符改错一个字母，体检必须点名它；
  3. 对账必须走**生产同一套函数**（`_normalize` + `*_ctx` + `unfilled`），
     不许在 packgen 里另抄一份"引擎提供哪些占位符"的表 —— 那是第二本账。
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
# ⚠ 不要在这里动 os.environ（原来写了 setenv TALKSCRIPT_MOCK=1）：
#   环境变量是**进程级**的，`load_config` 每次都读它 —— 一条测试就能把后面
#   所有「没配模型必须被拒」的用例变成放行（实测把 test_config_fresh、
#   test_models_list 两条与一条 mock 标记用例一起带红）。
#   本文件走 `cfg.mock = True` 就够：客户端的 mock 通道由它决定。

from app.config import load_config                        # noqa: E402
from app.knowledge import Pack                            # noqa: E402
from app.packgen import _pack_audit, create_pack          # noqa: E402
from app.pipeline import Pipeline                         # noqa: E402


@pytest.fixture()
def new_pack(tmp_path):
    shutil.copytree(ROOT / "packs", tmp_path / "packs")
    cfg = load_config(tmp_path)
    cfg.mock = True
    pl = Pipeline(tmp_path, cfg)
    info = create_pack(tmp_path, pl.llm, "口腔诊所", "连锁口腔诊所，面向家庭做儿牙与种植科普")
    return tmp_path, info["name"]


def _placeholder_notes(root: Path, slug: str) -> list[str]:
    return [n for n in _pack_audit(root, slug) if "占位符" in n]


def test_fresh_pack_has_no_placeholder_problem(new_pack):
    root, slug = new_pack
    assert _placeholder_notes(root, slug) == [], \
        "模板自带的问题会被复制进每一个新包 —— 这正是 P0-19 的形态"


def test_typo_in_template_is_named(new_pack):
    """改错一个字母 → 体检必须点名 `$hooksXX`，而不是静默通过。"""
    root, slug = new_pack
    f = root / "packs" / slug / "skill.yaml"
    text = f.read_text(encoding="utf-8")
    assert "$hooks" in text
    f.write_text(text.replace("$hooks", "$hooksXX"), encoding="utf-8")
    notes = _placeholder_notes(root, slug)
    assert len(notes) == 1, notes
    assert "$hooksXX" in notes[0] and "select" in notes[0]


def test_dangling_reference_is_still_reported(new_pack):
    """`路径#章节` 指到不存在的章节 → 注入为空，也必须点名（P0-18 同一形态）。"""
    root, slug = new_pack
    f = root / "packs" / slug / "skill.yaml"
    text = f.read_text(encoding="utf-8")
    assert "knowledge/standards.md#" in text, "模板里那条 `#章节` 引用不在了，这条用例要改写法"
    f.write_text(text.replace("knowledge/standards.md#", "knowledge/standards.md#不存在的节"),
                 encoding="utf-8")
    notes = [n for n in _pack_audit(root, slug) if "切片为空" in n or "读不到" in n]
    assert notes, "章节改名后静默不注入，正是报告反复点名的失效"


def test_audit_does_not_invent_its_own_placeholder_table():
    """体检不许自己维护一份占位符名单（第二本账）。"""
    import inspect

    import app.packgen as pg
    src = inspect.getsource(pg._placeholder_audit)
    for known in ("$hooks", "$growth", "$voice_block", "$plan_json"):
        assert known not in src, f"对账里写死了占位符名 {known}，引擎加新的就会误报"
    assert "unfilled" in src and "_normalize" in src, "没走生产路径，就是在另抄一份表"


def test_clean_pack_loads_with_no_fatal_note(new_pack):
    """加载类说明（结构坏）与占位符说明分开：干净的新包一条都不该有。"""
    root, slug = new_pack
    notes = _pack_audit(root, slug)
    assert not any("加载失败" in n for n in notes), notes
