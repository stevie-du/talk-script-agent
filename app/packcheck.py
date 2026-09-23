# -*- coding: utf-8 -*-
"""技能包校验 CLI（P3，方案 `docs/技能包系统方案.md` §7）。

给**包作者**用：`python -m app.packcheck <zip或目录>`。

    校验通过 → 退出码 0，打印包名/版本/作者/许可证/文件数
    校验失败 → 退出码 1，打印**人话原因**（与服务端导入一字不差）

为什么值得有它：作者最痛的流程是"打包 → 分享 → 用户导入失败 → 截图回来
问哪坏了"。有了 CLI，作者在打包前就能自检；而且校验代码与服务端导入
**同一份**（`validate_zip` / `validate_dir`）—— 不会出现"CLI 说 OK、
服务端拒"的分裂。

两种入参都收：
  · zip 文件 —— 与服务端导入路径完全一致（连 zip 层预检一起过）；
  · 目录     —— 作者手里还没打包的源目录，跳过 zip 层直接校验内容。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from .packimport import (PackImportError, validate_dir, validate_zip)


def _count_files(pack_root: Path) -> int:
    return sum(1 for f in pack_root.rglob("*") if f.is_file())


def check(target: str) -> int:
    """校验一个 zip 或目录。返回进程退出码。"""
    p = Path(target)
    if not p.exists():
        print(f"找不到：{target}")
        return 1
    try:
        if p.is_dir():
            r = validate_dir(p)
            kind = "目录"
        else:
            data = p.read_bytes()
            # zip 模式：校验要解压，临时目录的生命周期在这里。
            with tempfile.TemporaryDirectory(prefix="packcheck-") as td:
                r, pack_root = validate_zip(data, Path(td))
                kind = "zip"
                n = _count_files(pack_root)
        if p.is_dir():
            n = _count_files(p)
    except PackImportError as e:
        print(f"✗ 校验没通过：{e}")
        return 1
    except OSError as e:
        print(f"✗ 读不出来：{e}")
        return 1

    print(f"✓ 校验通过（{kind}）")
    print(f"  包名       {r.name}")
    print(f"  版本       v{r.version}")
    print(f"  作者       {r.author or '（未声明）'}")
    print(f"  许可证     {r.license or '（未声明）'}")
    print(f"  文件数     {n}")
    if r.has_private:
        print("  ⚠ 含 private/ —— 分享前记得去掉（那是你的商业信息，"
              "导入方看得到）")
    if not r.license:
        print("  ⚠ 没声明许可证 —— 建议在 pack.yaml 的 source.license 里写上，"
              "导入方看得到")
    print("\n可以打包分享了（zip 根目录直接是包目录，或裹一层同名目录）。")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(2)
    sys.exit(check(sys.argv[1]))
