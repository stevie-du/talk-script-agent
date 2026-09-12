# -*- coding: utf-8 -*-
"""统一的落盘写入。

单独抽一个模块只有一个原因：config.py 是最底层模块，pipeline / server 都依赖它，
写文件的工具函数放它们任何一边都会形成循环导入。
"""
from __future__ import annotations

import os
from pathlib import Path


def write_atomic(path: Path, text: str, encoding: str = "utf-8") -> None:
    """先写同目录临时文件，再 os.replace 换上去。

    直接 write_text 写到一半被强杀 / 断电 / Ctrl-C，目标文件会留下半截内容，
    而且这种损坏不会报错、也不会自我修复：
      - config.yaml 写坏 → 引擎起不来，界面只剩「无法连接本地引擎」；
      - result.json 写坏 → history() 解析失败就整条跳过，用户看到的是
        「刚生成完的记录凭空消失了」，连救都没处救；
      - pack.yaml 写坏 → list_packs 拆不出来，整个行业包列表连带首页一起废掉。
    先写临时文件能保证任何时刻目标要么是旧的完整版本、要么是新的完整版本，
    不存在中间态。

    注意必须落在同一分区才能 rename（这是它 GET 得到原子性的前提），所以临时文件
    取目标文件名加后缀、放在同一个目录下，绝不能图省事丢到系统临时目录。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    try:
        with open(tmp, "w", encoding=encoding) as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())       # 先确保进盘，避免在 replace 之后才真正写失败
        os.replace(tmp, path)
    except BaseException:
        # 半途失败就把临时文件收走，别在产物目录里堆垃圾文件
        try:
            tmp.unlink(missing_ok=True)
        except Exception:              # noqa: BLE001
            pass
        raise
