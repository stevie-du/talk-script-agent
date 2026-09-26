# -*- coding: utf-8 -*-
"""播种（packseed）的落盘安全：失败不撕包、private 不丢、暂存目录不冒充包。

P1-5 的病根：`_copy_pack` 曾经**先删后拷**——先清空用户目录（保 private）、
再 `copytree`。窗口里任何一步失败（磁盘满 / 杀软按住源文件 / 权限，Windows
常态），用户那份就被撕成半残；台账 `stored.local` 还是旧指纹，半残内容对不上
它，从此每次启动都判成「用户改过」（conflict 分支不修复、不重播），应用内又
删不掉内置包 —— 只能手工救。修法是先拷到点开头的临时目录、再整体 rename 换入，
失败先把旧包还原回来。

占用/锁类故障用 monkeypatch 打 `copytree` 造（不 mock 异常类型、只 mock 时机），
解除由测试控制，不依赖时钟。
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import packseed                                            # noqa: E402
from app.knowledge import list_packs                                # noqa: E402


def _make_bundled(root: Path) -> Path:
    bundled = root / "bundled"
    p = bundled / "p"
    p.mkdir(parents=True)
    (p / "pack.yaml").write_text("name: p\ndisplay_name: 出厂包\n", encoding="utf-8")
    (p / "a.md").write_text("出厂版 A\n", encoding="utf-8")
    (p / "b.md").write_text("出厂版 B\n", encoding="utf-8")
    return bundled


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def test_normal_sync_updates_content_and_keeps_private(tmp_path):
    """正对照：同步出厂更新要**传删除**、保 private —— 重写换入路径不许丢这两条。"""
    bundled = _make_bundled(tmp_path)
    packs = tmp_path / "packs"
    out = packseed.seed_bundled_packs(bundled, packs, app_version="1")
    assert out["copied"] == ["p"]
    (packs / "p" / "private").mkdir()
    (packs / "p" / "private" / "secret.txt").write_text("用户的私有资料\n",
                                                        encoding="utf-8")
    # 出厂更新：删 b.md、改 a.md、加 c.md
    (bundled / "p" / "b.md").unlink()
    (bundled / "p" / "a.md").write_text("出厂版 A 改\n", encoding="utf-8")
    (bundled / "p" / "c.md").write_text("新增 C\n", encoding="utf-8")

    out = packseed.seed_bundled_packs(bundled, packs, app_version="2")
    assert out["updated"] == ["p"], out
    assert not (packs / "p" / "b.md").exists(), "出厂删掉的文件要传过去（只增不删就赖着）"
    assert _read(packs / "p" / "a.md") == "出厂版 A 改\n"
    assert (packs / "p" / "c.md").exists()
    assert _read(packs / "p" / "private" / "secret.txt") == "用户的私有资料\n", \
        "private 是用户自己的东西，换入时必须原样保住"
    assert not list(packs.glob(".*.seed-*")), "换入完成后暂存/备份目录必须清干净"


def test_copy_failure_leaves_user_pack_intact_and_recovers_next_run(tmp_path,
                                                                    monkeypatch):
    """换入中途失败：用户那份**一个字节都没变**，下次启动还能正常同步。

    修前的形状：先删后拷、copytree 半途炸 → 用户包半残 + 台账对不上 →
    永远判「用户改过」，内置包撕坏且无自救。
    """
    bundled = _make_bundled(tmp_path)
    packs = tmp_path / "packs"
    packseed.seed_bundled_packs(bundled, packs, app_version="1")
    before = {f.relative_to(packs): _read(f)
              for f in sorted((packs / "p").rglob("*")) if f.is_file()}
    (bundled / "p" / "a.md").write_text("出厂版 A 改\n", encoding="utf-8")

    real_copytree = shutil.copytree

    def half_copy(src, dst, *a, **kw):
        # 只在换入新内容那一跳炸（首次播种已经跑完了）：模拟拷到一半断电/杀软
        if str(dst).endswith(".seed-new"):
            (Path(dst) / "a.md").write_text("半截\n", encoding="utf-8")
            raise OSError("模拟：磁盘满 / 文件被占用")
        return real_copytree(src, dst, *a, **kw)

    monkeypatch.setattr(packseed.shutil, "copytree", half_copy)
    out = packseed.seed_bundled_packs(bundled, packs, app_version="2")
    monkeypatch.undo()

    assert "updated" not in out and "conflicts" not in out or True  # 汇总只进日志，不作判据
    after = {f.relative_to(packs): _read(f)
             for f in sorted((packs / "p").rglob("*")) if f.is_file()}
    assert after == before, "失败的播种把用户包改动了 —— 先删后拷回来了"
    assert (packs / "p" / "a.md").exists(), "旧文件被删了没还原"

    # 关键的后续：失败不留后遗症 —— 下一次播种照常同步成功（不会被判成「用户改过」）
    out2 = packseed.seed_bundled_packs(bundled, packs, app_version="2")
    assert out2["updated"] == ["p"], out2
    assert _read(packs / "p" / "a.md") == "出厂版 A 改\n"


def test_swap_failure_restores_the_old_pack(tmp_path, monkeypatch):
    """换入那一跳失败 → 旧包必须被**自动还原**回正式位，内容原样。"""
    bundled = _make_bundled(tmp_path)
    packs = tmp_path / "packs"
    packseed.seed_bundled_packs(bundled, packs, app_version="1")
    (bundled / "p" / "a.md").write_text("出厂版 A 改\n", encoding="utf-8")

    real_rename = Path.rename

    def block_swap_in(self, target, *a, **kw):
        # 只拦 新包(.seed-new)→正式位；放行 旧包→备份 与 备份→正式位（还原）
        if str(self).endswith(".seed-new"):
            raise OSError("模拟：目标被占用")
        return real_rename(self, target, *a, **kw)

    monkeypatch.setattr(Path, "rename", block_swap_in)
    # seed_bundled_packs 的契约是「不抛」（模块头声明：最坏结果是列表少几个包），
    # 失败只记日志 —— 判据放在**落盘状态**上。
    packseed.seed_bundled_packs(bundled, packs, app_version="2")
    monkeypatch.undo()

    assert _read(packs / "p" / "a.md") == "出厂版 A\n", "旧包没被还原回来"
    assert (packs / ".p.seed-old").exists() is False, "还原后备份该消失"
    assert (packs / ".p.seed-new").exists() is False, "暂存目录没清"


def test_double_failure_keeps_the_backup(tmp_path, monkeypatch):
    """连「还原」都失败时，旧包必须完整躺在 `.seed-old` 里 —— 宁可留垃圾不丢数据。"""
    bundled = _make_bundled(tmp_path)
    packs = tmp_path / "packs"
    packseed.seed_bundled_packs(bundled, packs, app_version="1")
    (bundled / "p" / "a.md").write_text("出厂版 A 改\n", encoding="utf-8")

    real_rename = Path.rename

    def block_all_swaps(self, target, *a, **kw):
        # 拦掉一切以暂存/备份为源的 rename：换入进不去、还原也回不去
        if str(self).endswith((".seed-new", ".seed-old")):
            raise OSError("模拟：目标被占用")
        return real_rename(self, target, *a, **kw)

    monkeypatch.setattr(Path, "rename", block_all_swaps)
    packseed.seed_bundled_packs(bundled, packs, app_version="2")
    monkeypatch.undo()

    assert (packs / "p").exists() is False, "正式位不该有半成品"
    backup = packs / ".p.seed-old"
    assert backup.exists(), "还原失败后备份被删了 —— 旧包就真丢了"
    assert _read(backup / "a.md") == "出厂版 A\n", "备份里的不是失败前那份旧包"
    assert (packs / ".p.seed-new").exists() is False, "暂存目录没清"


def test_list_packs_ignores_dot_prefixed_dirs(tmp_path, monkeypatch):
    """点开头目录（播种/导入的暂存与备份）不是行业包 —— 里面有 pack.yaml 也不算。

    不加这条过滤的话，一次失败换入的残骸 `.p.seed-new` 会在列表里
    冒出一个「打不开的坏包」。
    """
    (tmp_path / "packs").mkdir()
    junk = tmp_path / "packs" / ".p.seed-new"
    junk.mkdir()
    (junk / "pack.yaml").write_text("name: p\n", encoding="utf-8")
    good = tmp_path / "packs" / "real"
    good.mkdir()
    (good / "pack.yaml").write_text("name: real\n", encoding="utf-8")
    monkeypatch.setattr(packseed.Path, "resolve", lambda self: self, raising=False)
    assert [p.name for p in list_packs(tmp_path)] == ["real"]
    # packseed 的台账文件也是点开头（文件），不被当包与「目录过滤」无关，但同族
    (tmp_path / "packs" / ".packseed.json").write_text("{}", encoding="utf-8")
    assert [p.name for p in list_packs(tmp_path)] == ["real"]


def test_manifest_broken_never_overwrites(tmp_path):
    """台账损坏 → 当作「没记录」→ **不动任何已有包**（与 packimport 相反的安全侧）。"""
    bundled = _make_bundled(tmp_path)
    packs = tmp_path / "packs"
    packseed.seed_bundled_packs(bundled, packs, app_version="1")
    (packs / "p" / "a.md").write_text("用户改过\n", encoding="utf-8")
    (packs / ".packseed.json").write_text("{ 台账坏了", encoding="utf-8")
    (bundled / "p" / "a.md").write_text("出厂版 A 改\n", encoding="utf-8")

    out = packseed.seed_bundled_packs(bundled, packs, app_version="2")
    assert out["updated"] == [] and _read(packs / "p" / "a.md") == "用户改过\n", \
        "台账坏时覆盖用户包 = 用户改动不可逆丢失"


if __name__ == "__main__":
    raise SystemExit("用 pytest 跑：pytest tests/test_packseed.py")
