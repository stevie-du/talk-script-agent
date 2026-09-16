# -*- coding: utf-8 -*-
"""单条历史记录读坏时：这条记录会**凭空消失**，所以必须留下信号。

背景
----
`Store._rebuild()` 重建索引时逐个解析 `*/job.json` 与 `*/result.json`，
解析失败就 `continue`。修复前这两处**既不记日志也不报错**：用户只看到
「历史少了几条」，引擎这边一片安静 —— 正是本项目最忌讳的静默降级。

它和「index.json 坏了」不是一回事：后者有自愈（判据不符就重建），
前者没有任何兜底 —— 跳过就是没了，所以至少要**看得见**。

判据能否证伪：删掉 `_rebuild()` 里那两行 `log.warning` → 报红。
"""
import json
import logging
from pathlib import Path

from app.store import ArtifactStore as Store

ROOT = Path(__file__).resolve().parents[1]


def _put(root: Path, jid: str, name: str, text: str) -> None:
    """在 generated/<day>/<jid>/ 下放一个文件（索引就是按这个布局 glob 的）。"""
    d = root / "generated" / "2026-09-16" / jid
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(text, encoding="utf-8")


def test_corrupt_job_json_is_skipped_but_logged(tmp_path, caplog):
    _put(tmp_path, "good-1", "job.json", json.dumps({"id": "good-1"}))
    _put(tmp_path, "bad-1", "job.json", "{ 这不是 json")
    with caplog.at_level(logging.WARNING, logger="app.store"):
        items = Store(ROOT, data_dir=tmp_path).history()
    ids = [i["id"] for i in items]
    assert "good-1" in ids, "好记录不该被坏记录连累"
    assert "bad-1" not in ids
    assert any(r.levelno == logging.WARNING and "bad-1" in r.getMessage()
               for r in caplog.records), "坏记录被静默吞掉了（一条 WARNING 都没有）"


def test_corrupt_result_json_is_also_logged(tmp_path, caplog):
    """另一条循环同样要记 —— 只修一处的话，result.json 坏掉仍然没声音。"""
    _put(tmp_path, "good-2", "result.json", json.dumps({"id": "good-2"}))
    _put(tmp_path, "bad-2", "result.json", "{ 坏的")
    with caplog.at_level(logging.WARNING, logger="app.store"):
        Store(ROOT, data_dir=tmp_path).history()
    assert any(r.levelno == logging.WARNING and "bad-2" in r.getMessage()
               for r in caplog.records), "result.json 那条分支没记日志"


def test_good_records_log_nothing(tmp_path, caplog):
    """反面：好记录不该刷日志 —— 否则「有 WARNING」这个判据本身就失去意义。"""
    _put(tmp_path, "good-3", "job.json", json.dumps({"id": "good-3"}))
    with caplog.at_level(logging.WARNING, logger="app.store"):
        Store(ROOT, data_dir=tmp_path).history()
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []
