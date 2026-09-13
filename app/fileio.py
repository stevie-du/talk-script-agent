# -*- coding: utf-8 -*-
"""统一的落盘写入。

单独抽一个模块只有一个原因：config.py 是最底层模块，pipeline / server 都依赖它，
写文件的工具函数放它们任何一边都会形成循环导入。
"""
from __future__ import annotations

import os
import time
import uuid
from pathlib import Path

# replace 失败后的重试次数与间隔。Windows 上刚落盘的文件会被杀毒软件 /
# 搜索索引短暂占用，此刻 rename 会返回 [WinError 5] 拒绝访问 —— 这是瞬时状态，
# 重试即可；不重试的后果是「写入静默失败」，索引文件就此消失。
_REPLACE_RETRIES = 5
_REPLACE_BACKOFF = 0.04


def _replace_with_retry(tmp: Path, path: Path) -> None:
    for attempt in range(_REPLACE_RETRIES):
        try:
            os.replace(tmp, path)
            return
        except OSError:
            if attempt == _REPLACE_RETRIES - 1:
                raise
            time.sleep(_REPLACE_BACKOFF * (attempt + 1))


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

    临时文件名必须**每次调用都不同**（pid + uuid4）。原来只用 pid，同一进程里
    两个线程同时写同一个目标时，它们会拿到同一个临时路径：A 写完 replace 掉，
    B 再 replace 时源文件已经不在了 —— 在 Windows 上表现为
    `[WinError 5] 拒绝访问 ... .tmp-1234 -> index.json`。
    （历史索引就踩过这个坑：HTTP 线程读索引触发重建、同时工作线程在 upsert。）

    同一个 WinError 5 还有第二个来源：目标文件被杀毒软件或搜索索引瞬时占用。
    这与临时文件命名无关，重试即可成功，所以 replace 失败会有限重试几次。
    不重试的代价极不对称 —— 历史索引只是「缓存」，写失败被吞掉后每次
    history() 都会全量重建，索引带来的性能优化等于白做。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}-{uuid.uuid4().hex[:8]}")
    try:
        with open(tmp, "w", encoding=encoding) as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())       # 先确保进盘，避免在 replace 之后才真正写失败
        _replace_with_retry(tmp, path)
    except BaseException:
        # 半途失败就把临时文件收走，别在产物目录里堆垃圾文件
        try:
            tmp.unlink(missing_ok=True)
        except Exception:              # noqa: BLE001
            pass
        raise
