# -*- coding: utf-8 -*-
"""技能包导入的回归网（方案 `docs/技能包系统方案.md` §4）。

为什么每条都要钉：导入是**唯一一个把不可信内容放进用户数据目录**的入口。
任何一条防线失效都不是"导入失败"，而是"用户机器上多了个预料之外的东西"。
所以这里连失败分支一起测 —— 拒收的原因也要是人话（PackImportError.message）。
"""
from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pytest

from app.packimport import (IMPORT_MARK, MAX_ENTRIES, PackImportError,
                            delete_pack, import_pack)

MIN_PACK_YAML = """\
name: testpack
display_name: 测试包
version: 2
description: 测试用行业包
params:
  segment:
    label: 细分
    options: [A, B]
    default: A
"""

MIN_SKILL_YAML = """\
stages:
  write:
    user_template: |
      写一段关于 $topic 的口播稿。
"""


def _zip(files: dict[str, str | bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in files.items():
            if isinstance(content, str):
                content = content.encode("utf-8")
            zf.writestr(name, content)
    return buf.getvalue()


def _min_pack(**overrides) -> dict[str, str | bytes]:
    yaml_text = MIN_PACK_YAML
    for k, v in overrides.items():
        if k in yaml_text:
            yaml_text = yaml_text.replace(k, str(v))
    return {"pack.yaml": yaml_text, "skill.yaml": MIN_SKILL_YAML,
            "banwords.yaml": "hard: []\nsoft: []\n"}


# ── 正常路径 ────────────────────────────────────────────────

def test_import_min_pack(tmp_path):
    r = import_pack(_zip(_min_pack()), tmp_path)
    assert r.ok and r.name == "testpack" and r.version == 2
    assert (tmp_path / "testpack" / "pack.yaml").exists()
    assert (tmp_path / "testpack" / "skill.yaml").exists()
    assert not r.replaced and not r.has_private


def test_import_pack_wrapped_in_top_dir(tmp_path):
    """zip 裹一层顶层目录（打包时整个文件夹选进去的那种）也要认。"""
    r = import_pack(_zip({"mypack/" + k: v for k, v in _min_pack().items()}), tmp_path)
    assert r.ok and r.name == "testpack"
    assert (tmp_path / "testpack" / "pack.yaml").exists()


def test_import_writes_mark_and_keeps_author(tmp_path):
    z = _min_pack()
    z["pack.yaml"] = z["pack.yaml"].replace(
        "version: 2", 'version: 3\nsource:\n  author: 张三\n  license: MIT')
    r = import_pack(_zip(z), tmp_path)
    assert r.author == "张三" and r.license == "MIT"
    mark = json.loads((tmp_path / "testpack" / IMPORT_MARK).read_text(encoding="utf-8"))
    assert mark["author"] == "张三" and mark["license"] == "MIT"
    assert mark["pack_version"] == 3 and mark["imported_at"]


def test_import_flags_private_dir(tmp_path):
    z = _min_pack()
    z["private/products.yaml"] = "产品: 某电梯\n"
    r = import_pack(_zip(z), tmp_path)
    assert r.ok and r.has_private
    assert (tmp_path / "testpack" / "private" / "products.yaml").exists()


# ── zip 层防线 ──────────────────────────────────────────────

def test_bad_zip_rejected(tmp_path):
    with pytest.raises(PackImportError, match="zip 文件"):
        import_pack(b"not a zip at all", tmp_path)


def test_zip_slip_rejected(tmp_path):
    """条目名 ../evil.txt —— Zip Slip 的经典形态。"""
    z = _min_pack()
    z["../evil.txt"] = "x"
    with pytest.raises(PackImportError, match="穿越路径"):
        import_pack(_zip(z), tmp_path)


def test_absolute_path_entry_rejected(tmp_path):
    z = _min_pack()
    z["/etc/passwd"] = "x"
    with pytest.raises(PackImportError, match="非相对路径"):
        import_pack(_zip(z), tmp_path)


def test_backslash_path_entry_is_normalized(tmp_path):
    """Windows 造的 zip 用反斜杠 —— 洗干净后正常导入，不是拒收。"""
    z = {k.replace("/", "\\"): v for k, v in _min_pack().items()}
    r = import_pack(_zip(z), tmp_path)
    assert r.ok and (tmp_path / "testpack" / "pack.yaml").exists()


def test_executable_suffix_rejected(tmp_path):
    """.py 一律拒 —— 与面板可读白名单（含 .py）是两回事。"""
    z = _min_pack()
    z["scripts/setup.py"] = "import os; os.system('...')"
    with pytest.raises(PackImportError, match="不允许的文件类型"):
        import_pack(_zip(z), tmp_path)


def test_symlink_entry_rejected(tmp_path):
    z = _min_pack()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for k, v in z.items():
            zf.writestr(k, v.encode("utf-8") if isinstance(v, str) else v)
        zi = zipfile.ZipInfo("evil-link")
        zi.external_attr = (0o120777 << 16)
        zf.writestr(zi, "../../etc/passwd")
    with pytest.raises(PackImportError, match="符号链接"):
        import_pack(buf.getvalue(), tmp_path)


def test_too_many_entries_rejected(tmp_path):
    z = _min_pack()
    z.update({f"docs/f{i}.md": "x" for i in range(MAX_ENTRIES)})
    with pytest.raises(PackImportError, match="条目太多"):
        import_pack(_zip(z), tmp_path)


def test_oversized_file_rejected(tmp_path, monkeypatch):
    """单文件超限（把上限压到 10 字节来测，不用真造 1MB）。

    这条打到的是**解压前预检**（zip 声明的 file_size）。另有一道解压后
    复检防"zip 元数据说谎"（恶意构造的 zip 才触达），与预检同一上限 ——
    冗余关系写在 packimport 的代码注释里。
    """
    import app.packimport as pi
    monkeypatch.setattr(pi, "MAX_FILE_BYTES", 10)
    z = _min_pack()
    with pytest.raises(PackImportError, match="太大"):
        import_pack(_zip(z), tmp_path)


# ── manifest 与结构防线 ─────────────────────────────────────

def test_missing_pack_yaml_rejected(tmp_path):
    with pytest.raises(PackImportError, match="找不到 pack.yaml"):
        import_pack(_zip({"skill.yaml": MIN_SKILL_YAML}), tmp_path)


def test_broken_yaml_rejected(tmp_path):
    z = _min_pack()
    z["pack.yaml"] = "name: [unclosed"
    with pytest.raises(PackImportError, match="不是合法 YAML"):
        import_pack(_zip(z), tmp_path)


def test_bad_name_rejected(tmp_path):
    z = _min_pack()
    z["pack.yaml"] = z["pack.yaml"].replace("name: testpack", "name: ../../evil")
    with pytest.raises(PackImportError, match="name 不合法"):
        import_pack(_zip(z), tmp_path)


def test_bad_version_rejected(tmp_path):
    z = _min_pack()
    z["pack.yaml"] = z["pack.yaml"].replace("version: 2", 'version: "v2"')
    with pytest.raises(PackImportError, match="version"):
        import_pack(_zip(z), tmp_path)


def test_future_pack_api_rejected(tmp_path):
    z = _min_pack()
    z["pack.yaml"] = z["pack.yaml"].replace("version: 2", "version: 1\npack_api: 99")
    with pytest.raises(PackImportError, match="格式 v99"):
        import_pack(_zip(z), tmp_path)


def test_missing_skill_yaml_rejected(tmp_path):
    z = _min_pack()
    del z["skill.yaml"]
    with pytest.raises(PackImportError, match="缺少 skill.yaml"):
        import_pack(_zip(z), tmp_path)


def test_missing_banwords_file_rejected(tmp_path):
    """banwords 指向的文件不存在 = 合规校验静默全过（最严重的一类失效）。"""
    with pytest.raises(PackImportError, match="缺少 banwords.yaml"):
        import_pack(_zip({"pack.yaml": MIN_PACK_YAML, "skill.yaml": MIN_SKILL_YAML}), tmp_path)


def test_banwords_path_escaping_rejected(tmp_path):
    z = _min_pack()
    z["pack.yaml"] = z["pack.yaml"].replace("description: 测试用行业包",
                                            "description: x\nbanwords: ../../evil.yaml")
    with pytest.raises(PackImportError, match="跑到了包外面"):
        import_pack(_zip(z), tmp_path)


# ── 同名冲突：用户改过的绝不静默覆盖 ─────────────────────────

def _seed_ledger(packs_dir: Path, name: str, local_hash: str) -> None:
    (packs_dir / ".packseed.json").write_text(json.dumps(
        {"packs": {name: {"version": "1.0", "bundled": "b" * 16, "local": local_hash}}}),
        encoding="utf-8")


def test_replace_unmodified_seeded_pack_ok(tmp_path):
    """台账在、用户没改过（local 指纹对得上）→ 允许替换。"""
    from app.packseed import tree_hash
    existing = tmp_path / "testpack"
    existing.mkdir()
    (existing / "pack.yaml").write_text(MIN_PACK_YAML, encoding="utf-8")
    (existing / "skill.yaml").write_text(MIN_SKILL_YAML, encoding="utf-8")
    (existing / "banwords.yaml").write_text("hard: []\n", encoding="utf-8")
    _seed_ledger(tmp_path, "testpack", tree_hash(existing, skip_private=True))
    r = import_pack(_zip(_min_pack()), tmp_path)
    assert r.ok and r.replaced


def test_replace_user_modified_pack_rejected(tmp_path):
    """台账在、用户改过（local 指纹对不上）→ 拒，且说清为什么。"""
    from app.packseed import tree_hash
    existing = tmp_path / "testpack"
    existing.mkdir()
    (existing / "pack.yaml").write_text(MIN_PACK_YAML, encoding="utf-8")
    (existing / "skill.yaml").write_text(MIN_SKILL_YAML, encoding="utf-8")
    (existing / "banwords.yaml").write_text("hard: []\n", encoding="utf-8")
    _seed_ledger(tmp_path, "testpack", "stale-hash")
    (existing / "pack.yaml").write_text(MIN_PACK_YAML + "# 我改的\n", encoding="utf-8")
    with pytest.raises(PackImportError, match="你改过内置包"):
        import_pack(_zip(_min_pack()), tmp_path)
    # 被拒后原包必须原样还在（一个字节都没动）
    assert "# 我改的" in (existing / "pack.yaml").read_text(encoding="utf-8")


def test_replace_imported_pack_ok(tmp_path):
    """没有台账记录（用户自己导入的）→ 覆盖是用户的选择，允许。"""
    import_pack(_zip(_min_pack()), tmp_path)
    r = import_pack(_zip(_min_pack()), tmp_path)
    assert r.ok and r.replaced


def test_failed_import_leaves_no_half_pack(tmp_path):
    """导入失败时 packs_dir 里不该留下任何痕迹。"""
    z = _min_pack()
    z["bad.py"] = "x"
    with pytest.raises(PackImportError):
        import_pack(_zip(z), tmp_path)
    assert list(tmp_path.iterdir()) == []


# ── 卸载 ────────────────────────────────────────────────────

def test_delete_imported_pack(tmp_path):
    import_pack(_zip(_min_pack()), tmp_path)
    r = delete_pack("testpack", tmp_path)
    assert r["ok"] and not (tmp_path / "testpack").exists()


def test_delete_seeded_pack_rejected(tmp_path):
    """内置包不给删 —— 删了下次播种又回来，用户看到"卸载没用"。"""
    d = tmp_path / "elevator"
    d.mkdir()
    (d / "pack.yaml").write_text(MIN_PACK_YAML, encoding="utf-8")
    with pytest.raises(PackImportError, match="内置行业包"):
        delete_pack("elevator", tmp_path)
    assert d.exists()          # 拒绝删之后包还在


def test_delete_missing_pack_rejected(tmp_path):
    with pytest.raises(PackImportError, match="不存在"):
        delete_pack("nope", tmp_path)
