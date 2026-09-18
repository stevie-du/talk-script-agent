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

from app.store import ArtifactStore as Store, INDEX_VERSION

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


# ── 历史摘要的字段契约（2026-09-18）──────────────────────────────
#
# 会话列表的副标题改成「行业 · 时间 · 平台」后，**平台成了列表上的一等信息**。
# 而它是从 `params.platform` 摘进索引摘要的 —— 摘漏了，列表上那一格就永远是空的。
#
# 为什么值得单独立断言：漏掉它不会报任何错。前端 `if (it.platform)` 是个静默分支，
# 字段缺失与「用户当时真没选平台」在界面上**长得一模一样**（本项目「静默降级」
# 的典型形态）。而且旧 index.json 里本来就没有这个字段 —— 必须靠版本号
# 逼一次重建，否则升级上来的老用户永远看不到平台。


def _result(jid: str, platform: str) -> dict:
    """一份**够用**的 result.json —— `_rebuild()` 会顺手补渲染 脚本.md，
    而 `render_script_md` 要读 check 里的 estimated_seconds / deviation_pct 等字段。
    缺字段会让 md 渲染抛异常，测试就变成「在验 md 渲染」而不是「在验 platform」——
    失败原因一混，变异检验就说不清是哪条断言拦住的。
    """
    return {
        "id": jid, "pack": "elevator", "pack_draft": False,
        "params": {"topic": "带平台的记录", "segment": "维保", "audience": "业主",
                   "duration": 60, "platform": platform, "style": "平实",
                   "persona": "专业", "mode": "auto", "rate": 4.5,
                   "voice": "standard", "format": "both"},
        "quota": {"total": 270, "hook": 40, "body": 190, "cta": 40},
        "quota_degraded": False,
        "plan": {"points": ["点一"]},
        "sections": [{"type": "hook", "text": "开场", "seq": 1}],
        "storyboard": [], "scenes": [], "placeholders": [], "revisions": [],
        "timings": [{"start": 0.0, "end": 1.8}], "logs": [],
        "check": {"chars_total": 42, "target_total": 270, "estimated_seconds": 12.0,
                  "duration_target": 60, "deviation_pct": -80.0, "rate": 4.5,
                  "platform": platform, "hard_hits": [], "soft_hits": [],
                  "dropped_short": [], "segments": [], "points": 1,
                  "passed": True, "blockers": []},
    }


def test_summary_carries_platform(tmp_path):
    """done 记录的摘要必须带 platform（列表副标题要显示它）。"""
    _put(tmp_path, "p-1", "result.json",
         json.dumps(_result("p-1", "小红书"), ensure_ascii=False))
    items = {i["id"]: i for i in Store(ROOT, data_dir=tmp_path).history()}
    assert items["p-1"].get("platform") == "小红书", \
        f"摘要里没有 platform，列表就显示不出来：{items['p-1']}"


def test_summary_from_job_carries_platform(tmp_path):
    """失败/取消的记录也要带 —— 它们同样进列表，且平台在 job.json 里是现成的。

    只修 done 那条路的话，失败记录的副标题会缺一格，而这是**两条独立的
    构造路径**（_summary_from_result / _summary_from_job），必须分别守。
    """
    _put(tmp_path, "p-2", "job.json", json.dumps({
        "id": "p-2", "created_at": "2026-09-18T10:00:00", "state": "failed",
        "error": "接口超时",
        "params": {"pack": "elevator", "topic": "失败的", "platform": "抖音"}},
        ensure_ascii=False))
    items = {i["id"]: i for i in Store(ROOT, data_dir=tmp_path).history()}
    assert items["p-2"].get("platform") == "抖音", items["p-2"]


def test_old_index_without_platform_is_rebuilt(tmp_path):
    """v1 时代写下的索引（条目里没有 platform）必须被判为过期 → 重建。

    判据是**版本号**，不是「条目里有没有这个键」—— 后者要为每个新字段
    写一遍扫描逻辑，漏一个就静默降级。这里写一份货真价实的 v1 索引，
    断言 history() 返回的条目已经带上了 platform。

    ⚠ 这里必须钉死版本号 **1**，不能写 `INDEX_VERSION - 1`：
    这么写的话，把常量改回 1 时它就变成 0，照样 ≠ 1 而触发重建 ——
    「忘了升版本号」这个变异**全绿漏过**，而它恰恰是最该守住的一种
    （老用户升级后永远看不到 platform）。钉死 1 之后，常量一旦退回 1，
    这份索引就被当成当前版本不再重建，断言当场报红（实测过，见提交说明）。
    """
    assert INDEX_VERSION > 1, "本次改动的全部意义就是升到 v2；退回去等于老索引不再重建"
    _put(tmp_path, "p-3", "result.json",
         json.dumps(_result("p-3", "视频号"), ensure_ascii=False))
    idx = tmp_path / "generated" / "index.json"
    idx.write_text(json.dumps({
        "version": 1,
        "items": [{"id": "p-3", "created_at": "2026-09-18T10:00:00",
                   "pack": "elevator", "topic": "带平台的记录",
                   "duration": 60, "chars": 42, "passed": True, "state": "done"}],
    }, ensure_ascii=False), encoding="utf-8")

    items = {i["id"]: i for i in Store(ROOT, data_dir=tmp_path).history()}
    assert items["p-3"].get("platform") == "视频号", \
        f"旧索引没被重建，platform 永远补不上：{items['p-3']}"
    # 重建后落盘的索引也必须是当前版本号，否则每次 history() 都白重建一次
    assert json.loads(idx.read_text(encoding="utf-8"))["version"] == INDEX_VERSION
