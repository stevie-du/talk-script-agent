# -*- coding: utf-8 -*-
"""校验 CLI 的回归（P3，`python -m app.packcheck`）。

守两件事：
  ① CLI 的判定与服务端导入**完全一致** —— 它们共用 validate_zip /
     validate_dir，这条测试钉的是"没走两条路"（各写一份就会漂）；
  ② CLI 的退出码契约：通过 0 / 拒 1 / 用法错 2 —— 作者会把它写进
     打包脚本，退出码错了整条自动化静默失效。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from app.packcheck import check
from app.packimport import import_pack
from tests.test_pack_import import MIN_PACK_YAML, MIN_SKILL_YAML, _min_pack, _zip


def _pack_dir(tmp_path: Path, name="testpack") -> Path:
    d = tmp_path / name
    d.mkdir()
    (d / "pack.yaml").write_text(MIN_PACK_YAML, encoding="utf-8")
    (d / "skill.yaml").write_text(MIN_SKILL_YAML, encoding="utf-8")
    (d / "banwords.yaml").write_text("hard: []\nsoft: []\n", encoding="utf-8")
    return d


def test_check_dir_ok(tmp_path):
    assert check(str(_pack_dir(tmp_path))) == 0


def test_check_zip_ok(tmp_path):
    z = tmp_path / "pack.zip"
    z.write_bytes(_zip(_min_pack()))
    assert check(str(z)) == 0


def test_check_dir_missing_skill(tmp_path):
    d = _pack_dir(tmp_path)
    (d / "skill.yaml").unlink()
    assert check(str(d)) == 1


def test_check_zip_slip_rejected(tmp_path):
    z = _min_pack()
    z["../evil.md"] = "x"
    f = tmp_path / "bad.zip"
    f.write_bytes(_zip(z))
    assert check(str(f)) == 1


def test_check_missing_target(tmp_path):
    assert check(str(tmp_path / "nope")) == 1


def test_cli_verdict_matches_server_import(tmp_path):
    """同一个包：CLI 判 OK 时 import_pack 也必须 OK（共用校验的钉子）。

    反向也测：CLI 拒的包，import_pack 必须拒。两头都钉住才证明没走两条路。
    """
    good = tmp_path / "good.zip"
    good.write_bytes(_zip(_min_pack()))
    assert check(str(good)) == 0
    packs = tmp_path / "packs"
    packs.mkdir()
    r = import_pack(good.read_bytes(), packs)
    assert r.ok and r.name == "testpack"

    bad = _min_pack()
    bad["bad.py"] = "x"
    badf = tmp_path / "bad.zip"
    badf.write_bytes(_zip(bad))
    assert check(str(badf)) == 1
    from app.packimport import PackImportError
    with pytest.raises(PackImportError):
        import_pack(badf.read_bytes(), packs)


def test_module_entry_exit_codes(tmp_path):
    """`python -m app.packcheck` 的退出码：0 通过 / 1 拒 / 2 用法错。"""
    good = tmp_path / "good.zip"
    good.write_bytes(_zip(_min_pack()))
    r = subprocess.run([sys.executable, "-m", "app.packcheck", str(good)],
                       capture_output=True, text=True, cwd=Path(__file__).resolve().parent.parent)
    assert r.returncode == 0, r.stderr
    r2 = subprocess.run([sys.executable, "-m", "app.packcheck"],
                        capture_output=True, text=True, cwd=Path(__file__).resolve().parent.parent)
    assert r2.returncode == 2
    r3 = subprocess.run([sys.executable, "-m", "app.packcheck", str(tmp_path / "nope")],
                        capture_output=True, text=True, cwd=Path(__file__).resolve().parent.parent)
    assert r3.returncode == 1
