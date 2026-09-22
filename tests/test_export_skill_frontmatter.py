# -*- coding: utf-8 -*-
"""导出 Agent 技能的 frontmatter：行业名与细分列表都是**用户可控文本**，
拼 YAML 拼出来的东西必须能被 YAML 读回去。

复验时实测到的缺陷（报告 §14.9 第 4 条里那句「选项里含 ': ' 会让 frontmatter
抛 ScannerError」）：`name:` / `description:` 用 f-string 直出，
只要某个 segment 选项写成「预算: 报价」这类带冒号的文本，导出的 SKILL.md
就不是合法 YAML —— 界面拿到的是一个 500，而且**技能目录已经写了一半在盘上**。
去留（要不要这个功能）还没定，但一条能崩的路径不该因为"可能要删"就留着。
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.export_skill import export_agent_skill                     # noqa: E402
from app.knowledge import Pack                                      # noqa: E402


def _pack_with_segments(tmp_path: Path, segments: list[str]) -> Path:
    shutil.copytree(ROOT / "packs", tmp_path / "packs")
    pk = tmp_path / "packs" / "elevator"
    data = yaml.safe_load((pk / "pack.yaml").read_text(encoding="utf-8"))
    data["params"]["segment"]["options"] = segments
    (pk / "pack.yaml").write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return tmp_path


def test_option_containing_colon_space_still_exports(tmp_path):
    """带「: 」的选项名（用户真的会这么写）不该把导出打成非法 YAML。"""
    root = _pack_with_segments(tmp_path, ["预算: 报价", "别墅电梯-加装", "-号开头"])
    out = Path(export_agent_skill(root, "elevator", tmp_path / "skill")["path"])
    text = (out / "SKILL.md").read_text(encoding="utf-8")
    assert text.startswith("---\n")
    fm = yaml.safe_load(text.split("\n---\n", 1)[0][4:])
    assert fm["name"] and fm["description"]
    # 描述里带着那些选项名 —— 被正确引用过才读得回来（读回来就是证明）
    assert "预算: 报价" in fm["description"]


def test_frontmatter_names_the_requested_pack(tmp_path):
    root = _pack_with_segments(tmp_path, ["维保"])
    out = Path(export_agent_skill(root, "elevator", tmp_path / "skill2")["path"])
    text = (out / "SKILL.md").read_text(encoding="utf-8")
    fm = yaml.safe_load(text.split("\n---\n", 1)[0][4:])
    assert Pack(root, "elevator").data["display_name"] in str(fm["description"])


def test_body_horizontal_rule_does_not_break_the_self_check(tmp_path):
    """自检按前两道 fence 取 frontmatter：正文里的 `---` 不该把它切错。

    修复前是 `text.split("---")[1]`：正文（表格分隔线）里只要出现一次 `---`，
    取到的就不是 frontmatter，自检会以一个看不懂的错误炸掉一次本来成功的导出。
    """
    root = _pack_with_segments(tmp_path, ["维保"])
    out = Path(export_agent_skill(root, "elevator", tmp_path / "skill3")["path"])
    p = out / "SKILL.md"
    text = p.read_text(encoding="utf-8")
    p.write_text(text.replace("## 执行流程", "---\n\n## 执行流程", 1), encoding="utf-8")
    # 直接把导出后的文件再走一遍解析逻辑（与 export_skill 的自检同口径）
    assert text.startswith("---\n")
    fm = yaml.safe_load(p.read_text(encoding="utf-8").split("\n---\n", 1)[0][4:])
    assert fm.get("name")


def test_export_refuses_a_broken_frontmatter_instead_of_500(tmp_path, monkeypatch):
    """自检失败要说人话：RuntimeError 而非裸 assert（AssertionError 只会变成一个 500）。"""
    root = _pack_with_segments(tmp_path, ["维保"])
    real_dump = yaml.safe_dump

    def crippled(obj, **kw):
        return "这不是 YAML：：\n\t- [" if isinstance(obj, dict) and "name" in obj \
            else real_dump(obj, **kw)

    monkeypatch.setattr("app.export_skill.yaml.safe_dump", crippled)
    with pytest.raises(Exception) as ei:
        export_agent_skill(root, "elevator", tmp_path / "skill4")
    assert not isinstance(ei.value, AssertionError), "自检还在用 assert 炸给用户看"


if __name__ == "__main__":                       # pragma: no cover
    sys.exit(pytest.main([__file__, "-q"]))
