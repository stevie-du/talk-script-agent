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


def test_oversized_zip_rejected(tmp_path, monkeypatch):
    """zip **总大小**这一重限制也要守住（P2-26）。

    `MAX_ENTRIES`（上面那条）与 `MAX_FILE_BYTES`（再上面那条）都有测试，
    唯独 `MAX_ZIP_BYTES` 在 tests/ 里**零引用** —— 三重限制少守一重。
    它挡的是"条目不多、单文件也不大、但总量很大"的包：前两重都拦不住，
    而它撑的是磁盘与内存（解压前先整份读进内存）。
    """
    import app.packimport as pi
    monkeypatch.setattr(pi, "MAX_ZIP_BYTES", 10)
    z = _min_pack()
    with pytest.raises(PackImportError, match="上限"):
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


def _corrupt_ledger(packs_dir: Path, text: str) -> None:
    (packs_dir / ".packseed.json").write_text(text, encoding="utf-8")


def test_replace_with_corrupt_ledger_rejected(tmp_path):
    """台账**存在但读不出来** → 拒绝覆盖，且说清是台账的问题。

    台账读失败曾被当成"没有台账记录"（= 用户自己导入的包）放行，于是导入会
    覆盖 + 删备份，用户改动不可逆丢失；而 packseed 对同一状态是「不动任何
    已有包」。同一个兜底口径两处给出相反动作 —— 前者还配了一句
    「packseed 同款兜底：不动用户的东西」的 docstring，与代码相反。
    """
    existing = tmp_path / "testpack"
    existing.mkdir()
    (existing / "pack.yaml").write_text(MIN_PACK_YAML + "# 我改的\n", encoding="utf-8")
    (existing / "skill.yaml").write_text(MIN_SKILL_YAML, encoding="utf-8")
    (existing / "banwords.yaml").write_text("hard: []\n", encoding="utf-8")
    _corrupt_ledger(tmp_path, "{ 这不是合法 json")

    with pytest.raises(PackImportError, match="台账"):
        import_pack(_zip(_min_pack()), tmp_path)
    # 被拒后原包必须原样还在（一个字节都没动）
    assert "# 我改的" in (existing / "pack.yaml").read_text(encoding="utf-8")


def test_replace_with_structurally_broken_ledger_rejected(tmp_path):
    """台账 JSON 合法但结构不对（顶层不是映射）→ 同样拒绝，不静默放行。

    判据与上一条同源：「台账坏了」意味着**我们不知道**用户改没改，
    不能拿"不确定"冒充"没记录"。
    """
    existing = tmp_path / "testpack"
    existing.mkdir()
    (existing / "pack.yaml").write_text(MIN_PACK_YAML, encoding="utf-8")
    (existing / "skill.yaml").write_text(MIN_SKILL_YAML, encoding="utf-8")
    (existing / "banwords.yaml").write_text("hard: []\n", encoding="utf-8")
    _corrupt_ledger(tmp_path, '["not", "a", "map"]')

    with pytest.raises(PackImportError, match="台账"):
        import_pack(_zip(_min_pack()), tmp_path)


def test_missing_ledger_still_allows_replace(tmp_path):
    """**台账文件不存在** ≠ 台账损坏：从来没播种过 → 目录里的包都是用户自己的，
    覆盖仍是允许的。这条守住上面两条修复不要误伤 FileNotFoundError 分支。"""
    import_pack(_zip(_min_pack()), tmp_path)
    assert not (tmp_path / ".packseed.json").exists()
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


def test_swap_in_recovers_when_cross_drive_move_leaves_partial_dst(tmp_path,
                                                                   monkeypatch):
    """跨盘 move（= copytree+删）中途失败留下半截 dst → 也必须回滚（P2-3）。

    曾经 `if not dst.exists()` 把这种情况放过去：旧包躺在 `.import-backup-*`
    里、正式位上是半截新包，报错却断言「已还原原样」—— 文案与事实相反，
    用户要手工改隐藏目录名才能找回旧包。
    """
    import shutil as _shutil
    from app.packimport import _swap_in

    dst = tmp_path / "p"
    dst.mkdir()
    (dst / "pack.yaml").write_text("name: 旧的\n", encoding="utf-8")
    tmp_pack = tmp_path / ".tmp-p"
    tmp_pack.mkdir()
    (tmp_pack / "pack.yaml").write_text("name: 新的\n", encoding="utf-8")

    def half_move(src, dst2, *a, **kw):
        # 模拟跨盘 move 的退化形态（copytree 建好目录、拷了一部分才炸）：
        # 半截内容已落到正式位。⚠ dst2 是 **str**（_swap_in 传的是
        # `str(tmp_pack) / str(dst)`），必须先转 Path —— 否则 `/` 运算直接
        # TypeError、半截文件根本没落地，回滚照常发生，对应的变异检验假绿
        # （实测踩过：s9-6 第一版就是这么漏检的）。
        d = Path(dst2)
        d.mkdir(parents=True, exist_ok=True)
        (d / "pack.yaml").write_text("name: 半截\n", encoding="utf-8")
        raise OSError("模拟跨盘拷贝中途失败")

    monkeypatch.setattr(_shutil, "move", half_move)
    with pytest.raises(PackImportError, match="已还原原样"):
        _swap_in(tmp_pack, dst)
    monkeypatch.undo()

    assert (dst / "pack.yaml").read_text(encoding="utf-8") == "name: 旧的\n", \
        "半截新包占着正式位 —— 回滚被跳过了"
    assert not (tmp_path / ".import-backup-p").exists(), "旧包还原后备份才该消失"


def test_delete_route_wiring(tmp_path):
    """`DELETE /api/packs/{name}` 路由层的接线（P2-10：曾经零覆盖）。

    卸载是用户可直达的**破坏性操作**：`_safe_name` 校验、PackImportError→400
    映射、内置包拒绝——这四条都挂在路由这一层，底层 `delete_pack` 的单测
    盖不住「路由漏挂 / 异常映射丢了」这种回归。
    """
    from fastapi.testclient import TestClient
    from app.server import create_app

    packs = tmp_path / "packs"
    packs.mkdir()
    # 造一份已导入的包（带来源标记）与一份内置包
    import_pack(_zip(_min_pack()), packs)
    (packs / "elevator").mkdir()
    (packs / "elevator" / "pack.yaml").write_text("name: elevator\n", encoding="utf-8")

    c = TestClient(create_app(tmp_path, token="pi-token"),
                   base_url="http://127.0.0.1:8765",
                   raise_server_exceptions=False)
    c.headers.update({"X-TalkScript-Token": "pi-token"})

    r = c.delete("/api/packs/testpack")
    assert r.status_code == 200, r.text
    assert not (packs / "testpack").exists(), "卸载没删掉包"

    r = c.delete("/api/packs/elevator")
    assert r.status_code == 400, r.text
    assert "内置" in r.json()["detail"], r.text

    r = c.delete("/api/packs/nope")
    assert r.status_code == 400, r.text

    r = c.delete("/api/packs/%2e%2e")
    assert r.status_code == 400, r.text
    assert not (tmp_path / "pack.yaml").exists(), "穿越名没被拦住"
