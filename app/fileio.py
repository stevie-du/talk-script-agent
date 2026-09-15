# -*- coding: utf-8 -*-
"""统一的文件读写。

单独抽一个模块只有一个原因：config.py 是最底层模块，pipeline / server 都依赖它，
写文件的工具函数放它们任何一边都会形成循环导入。

除了「原子写入」，这里也放**读 YAML 的唯一一份口径**（`read_yaml_file` /
`yaml_error_brief`）。config.yaml 与行业包（pack / banwords / skill / private）
都要读 YAML，而它们对「读坏了」的处理曾经各写各的 —— 一处记 WARNING 并透出，
另一处 `except: return {}` 静默吞掉。**口径散在多处本身就是一种静默降级**：
修的时候只修一处，另一处看起来「早就好了」。
"""
from __future__ import annotations

import logging
import os
import shutil
import time
import uuid
from pathlib import Path

import yaml

log = logging.getLogger(__name__)


def yaml_error_brief(e: Exception) -> str:
    """把 YAML 解析异常压成一行「原因 + 行列号」，**不取 `str(e)`**。

    两件事分开说，避免把结论记错（实测，2026-09-15）：

    1. **文件流读路径本来就不泄漏。** 把**文件对象**交给 `yaml.safe_load`，
       PyYAML 的流式 Reader 不保留完整 buffer，于是 `Mark.get_snippet()`
       返回 None —— `str(e)` 里只有 `"…config.yaml", line 2, column 13`，
       没有正文。实测三种「密钥所在行就是出错行」的写法，文件流路径全部
       `含密钥=False`。

    2. **但字符串路径会泄漏。** 同一个坏文件，若改成
       `yaml.safe_load(p.read_text())`，`str(e)` 立刻带上出错行原文：

           mapping values are not allowed here
             in "<unicode string>", line 1, column 50:
                ... sk-super-secret-do-not-leak-9f3a: extra
                                                    ^

       而 config.yaml 里那一行很可能就是 `api_key: sk-xxxx`；这段文字要随
       `/api/config` 经 HTTP 下发给渲染进程。

    所以这里不是「修一个已发生的泄漏」，而是**不让安全性质取决于
    「用了哪种读法」**：`p.read_text()` 是更短、更顺手的写法。
    `read_yaml_file` 一律走文件对象，`str(e)` 也就一律进不了对外消息。

    附带收益：消息形态与输入方式无关，且位置统一成中文「第 N 行第 M 列」，
    可以直接给用户看（`str(e)` 是英文 `line N, column M`）。
    """
    parts = []
    ctx = getattr(e, "context", None)
    prob = getattr(e, "problem", None)
    mark = getattr(e, "problem_mark", None)
    if ctx:
        parts.append(str(ctx).replace("\n", " ").strip())
    if prob:
        parts.append(str(prob).replace("\n", " ").strip())
    if mark is not None:
        parts.append(f"第 {mark.line + 1} 行第 {mark.column + 1} 列")
    if not parts:
        # 兜底也**不取 str(e)**，只取类名 —— 见上，str 里可能带正文。
        return type(e).__name__
    return "，".join(parts)


def read_yaml_file(p: Path) -> tuple[dict, str]:
    """读一个 YAML 文件，返回 `(数据, 错误说明)`；错误说明为空串表示读成功。

    **文件不存在 → `({}, "")`** —— 这是「没有这个文件」，不是「文件坏了」。
    行业包里的 `banwords.yaml` / `skill.yaml` / `private/*.yaml` 都是可选文件，
    缺了属于正常状态；**存在但解析不出来**才是错误。两者必须分开，
    否则「没配词表」和「词表写坏了」会退化成同一个结果，
    而后者正是「合规校验静默全过」的来源。

    只读不缓存 —— 缓存策略由调用方决定（`knowledge.read_yaml_cached`
    按 mtime/size 缓存，并且**连错误一起缓存**，避免每次调用都刷一条 WARNING）。
    """
    if not p.exists():
        return {}, ""
    try:
        # 必须是**文件对象**，不能是 p.read_text() —— 见 yaml_error_brief。
        with open(p, encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except yaml.YAMLError as e:
        return {}, f"{p.name} 语法有误（{yaml_error_brief(e)}）"
    except OSError as e:
        return {}, f"{p.name} 读取失败（{e}）"
    if data is None:
        return {}, ""
    if not isinstance(data, dict):
        # 文件能解析但顶层不是映射（例如整份被写成了一个字符串）
        return {}, f"{p.name} 顶层应为键值映射，实际是 {type(data).__name__}"
    return data, ""

# replace 失败后的重试次数与间隔。Windows 上刚落盘的文件会被杀毒软件 /
# 搜索索引短暂占用，此刻 rename 会返回 [WinError 5] 拒绝访问 —— 这是瞬时状态，
# 重试即可；不重试的后果是「写入静默失败」，索引文件就此消失。
_REPLACE_RETRIES = 5
_REPLACE_BACKOFF = 0.04


# 删目录的重试次数。Windows 上刚写完的文件常被杀毒软件 / 搜索索引短暂占用，
# 此时 rmtree 会拿到「拒绝访问」。配合 `ignore_errors=True` 就变成
# 「删不掉也当成功」，后果因场景而异且都不好查：
#   - 历史记录：目录还在，下次重建索引时记录复活；
#   - 行业包：半成品目录留着，重试建包被 FileExistsError 挡成 409。
_RMTREE_RETRIES = 5
_RMTREE_BACKOFF = 0.05


def rmtree_resilient(path: Path) -> bool:
    """删掉整棵目录树，返回**是否真的删掉了**。

    调用方必须拿这个返回值当回事 —— 静默吞掉失败等于埋雷。
    """
    if not path.exists():
        return True
    for attempt in range(_RMTREE_RETRIES):
        try:
            shutil.rmtree(path)
            return True
        except OSError as e:
            if attempt == _RMTREE_RETRIES - 1:
                log.warning("目录删除失败：%s —— %s", path, e)
                return False
            time.sleep(_RMTREE_BACKOFF * (attempt + 1))
    return False


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
