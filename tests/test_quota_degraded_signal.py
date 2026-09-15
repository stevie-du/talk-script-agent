# -*- coding: utf-8 -*-
"""P2-8：`quota_degraded`（行业包没配 quota_table 时的降级信号）必须走到终点。

报告里写的是「只写不读」。实测比这严重得多：

    `_normalize` 算出 `quota_degraded=True` 存进内存 `job.params`，
    `_finalize` 用一份**手写白名单**从 `p` 里挑参数落盘 —— 它不在名单里，
    于是在落盘那一刻无声地掉了：`result.json` 里一个字都没有。
    而 `quota: {total: 256, ...}` 与包作者真配过的配额长得一模一样。

契约层也拦不住：`ScriptResult.model_validate(raw)` 对**缺失**字段只取默认值，
不报错。（`extra="forbid"` 也帮不上 —— 它拦的是多余键，不是缺失键。）

本文件守三层：
  1. 信号能落盘、能被前端读（`quota_degraded` 字段 + 脚本.md 提示）
  2. `check_contract_complete` 两个方向都卡得住（raw 少给键 / 多给键）
  3. `_normalize` 的每个键都**显式表态**去哪（防下一个 `quota_degraded`）
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import load_config                                    # noqa: E402
from app.knowledge import Pack, param_audit                           # noqa: E402
from app.pipeline import (PARAMS_DROPPED, PARAMS_ELSEWHERE,           # noqa: E402
                          PERSISTED_PARAMS, Pipeline, check_contract_complete,
                          wait_job)
from app.schemas import (GenerateRequest, RewriteSegmentRequest,      # noqa: E402
                         ScriptResult)

TOPIC = "被困电梯怎么办"


# ── 夹具：一个「没配 quota_table」的包 + 一个正常包，各跑一次真实（mock）生成 ──

def _copy_packs(tmp: Path) -> Path:
    shutil.copytree(ROOT / "packs", tmp / "packs")
    return tmp / "packs" / "elevator" / "pack.yaml"


def _strip_quota_table(tmp: Path) -> None:
    """去掉 quota_table —— 模拟手写行业包时漏了这一节。

    用 `yaml.safe_dump` 写回，不手拼 YAML 文本（手拼的 `\\n` 不转义，
    解析会失败并被当成另一种错，把 1 个 bug 误判成 2 个）。
    """
    p = _copy_packs(tmp)
    data = yaml.safe_load(p.read_text(encoding="utf-8"))
    assert data.pop("quota_table", None) is not None, "夹具前提：原包本来有 quota_table"
    p.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
                 encoding="utf-8")


def _generate(tmp: Path) -> dict:
    cfg = load_config(tmp)
    cfg.mock = True
    pl = Pipeline(tmp, cfg)
    jid = pl.start_generate(GenerateRequest(pack="elevator", topic=TOPIC, mode="auto"))
    snap = wait_job(pl, jid)
    assert snap["state"] == "done", snap.get("error")
    f = next((tmp / "generated").glob(f"*/{jid}/result.json"))
    md = f.parent / "脚本.md"
    return {"pl": pl, "jid": jid, "tmp": tmp, "snap": snap,
            "result": snap["result"], "raw": f.read_text(encoding="utf-8"),
            "md": md.read_text(encoding="utf-8"), "dir": f.parent}


@pytest.fixture(scope="module")
def degraded():
    tmp = Path(tempfile.mkdtemp(prefix="p28-degraded-"))
    _strip_quota_table(tmp)
    try:
        yield _generate(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@pytest.fixture(scope="module")
def healthy():
    tmp = Path(tempfile.mkdtemp(prefix="p28-healthy-"))
    _copy_packs(tmp)
    try:
        yield _generate(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ── 第 1 组：信号要能落盘 ────────────────────────────────────
# 变异：删掉 `_finalize` 里 raw 的 "quota_degraded" 那行 → 本组报红。

def test_degraded_signal_reaches_disk(degraded):
    """核心断言：内存里算出来的降级，必须能在 result.json 里读到。"""
    assert degraded["snap"]["params"]["quota_degraded"] is True, \
        "夹具前提：内存里的信号本来是对的（这条绿了才说明丢的是落盘那一步）"
    assert "quota_degraded" in degraded["raw"], \
        "降级信号没落盘 —— 界面看到的 quota 与真配额无法区分"
    assert degraded["result"]["quota_degraded"] is True


def test_degraded_signal_is_not_always_true(healthy):
    """防假绿：正常包必须为 False。否则「恒为 True」也能让上面那条通过。"""
    assert healthy["result"]["quota_degraded"] is False


def test_degraded_quota_is_indistinguishable_without_the_flag(degraded, healthy):
    """说清危害：两份产物的 quota 数字长得一模一样，只有 flag 能区分。"""
    dq, hq = degraded["result"]["quota"], healthy["result"]["quota"]
    assert set(dq) == set(hq) == {"total", "hook", "body", "cta"}
    assert all(isinstance(dq[k], int) and dq[k] > 0 for k in dq), dq
    # 两份都是「合法且看着正常」的配额 —— 这正是静默降级的定义
    assert degraded["result"]["quota_degraded"] != healthy["result"]["quota_degraded"]


# ── 第 2 组：契约与产物的键集合必须一致 ──────────────────────
# 变异：把 raw 里的 "quota_degraded" 去掉但保留 ScriptResult 的字段 → 报红（缺键）；
#       反过来保留 raw、去掉字段 → 报红（多键，Pydantic 会静默丢弃）。

def test_result_json_keys_match_the_contract(degraded, healthy):
    """落盘产物的键集合 == 契约字段集合。"""
    for name, fx in (("degraded", degraded), ("healthy", healthy)):
        assert set(fx["result"]) == set(ScriptResult.model_fields), \
            f"{name}：产物与契约字段不一致"


def test_contract_guard_rejects_a_stray_key(degraded):
    """raw 多给一个契约没声明的键 → 必须炸，不能静默丢弃。

    ⚠ 这条是变异检验逼出来的：最初只写了「产物键集合 == 契约」，
    而 Pydantic 会**先**把多余键丢掉，产物键集合照样等于契约 ——
    等于断言空转。所以守卫必须做在 `model_validate` **之前**。
    """
    with pytest.raises(ValueError) as ei:
        check_contract_complete({**degraded["result"], "没人声明过的键": 1})
    assert "没人声明过的键" in str(ei.value)


def test_contract_guard_rejects_a_missing_key(degraded):
    """raw 少给一个契约声明的键 → 同样必须炸（否则静默取默认值）。"""
    raw = {k: v for k, v in degraded["result"].items() if k != "quota_degraded"}
    with pytest.raises(ValueError) as ei:
        check_contract_complete(raw)
    assert "quota_degraded" in str(ei.value)


def test_contract_guard_accepts_the_real_thing(degraded):
    """前提守卫：真实产物必须过 —— 否则上面两条是在测一个永远会炸的函数。"""
    check_contract_complete(degraded["result"])


def test_contract_declares_the_signal():
    assert "quota_degraded" in ScriptResult.model_fields
    assert ScriptResult.model_fields["quota_degraded"].default is False, \
        "默认值必须是 False —— 老产物没有这个键，读回来要按「没降级」处理"


def test_unknown_key_is_silently_dropped_by_the_contract(degraded):
    """前提守卫：证明「忘了加契约字段」确实是静默的，这条守卫才有意义。

    如果哪天 Pydantic 改成对多余键报错，本测试会红 —— 那是好事，
    说明守卫可以从「键集合比对」降级为「契约自己拦」。
    """
    got = ScriptResult.model_validate(
        {**degraded["result"], "完全没声明过的键": 1}).model_dump()
    assert "完全没声明过的键" not in got


# ── 第 3 组：`_normalize` 的每个键都要表态去哪 ───────────────
# 变异：往 `_normalize` 的 out 里加一个键 → 报红（逼作者表态）。

def test_normalize_keys_are_all_accounted_for(degraded):
    pack = Pack(degraded["tmp"], "elevator")
    out = degraded["pl"]._normalize(pack, {"topic": TOPIC})
    accounted = set(PERSISTED_PARAMS) | set(PARAMS_ELSEWHERE) | set(PARAMS_DROPPED)
    assert set(out) == accounted, (
        "`_normalize` 产出的键与三张表对不上。新增键必须在 pipeline.py 里表态：\n"
        f"  落进 params → PERSISTED_PARAMS\n"
        f"  另有落点     → PARAMS_ELSEWHERE\n"
        f"  确实不要     → PARAMS_DROPPED（附理由）\n"
        f"  没表态的键：{sorted(set(out) - accounted)}\n"
        f"  表里有但产出没有：{sorted(accounted - set(out))}"
    )
    # 三张表互不重叠，否则「表态」是假的
    assert not (set(PERSISTED_PARAMS) & set(PARAMS_ELSEWHERE))
    assert not (set(PERSISTED_PARAMS) & set(PARAMS_DROPPED))
    assert not (set(PARAMS_ELSEWHERE) & set(PARAMS_DROPPED))
    assert all(v for v in PARAMS_DROPPED.values()), "被丢弃的键必须写清理由"


def test_everything_in_elsewhere_really_lands_in_the_result(degraded):
    """`PARAMS_ELSEWHERE` 声明了落点，就得真的在产物里找到 —— 别只在注释里。"""
    pack = Pack(degraded["tmp"], "elevator")
    out = degraded["pl"]._normalize(pack, {"topic": TOPIC})
    r = degraded["result"]
    for key in PARAMS_ELSEWHERE:
        assert key in r, f"{key} 声明落在 result['{key}']，实际没有"
        assert r[key] == out[key] or (key == "pack" and r[key] == out[key]), \
            f"{key} 的落盘值与内存值不一致：{r[key]!r} != {out[key]!r}"


# ── 第 4 组：其它出口也要带上信号 ────────────────────────────

def test_script_md_carries_the_warning(degraded):
    """导出的 脚本.md 是最终交付物，降级说明必须跟着走。"""
    assert "quota_table" in degraded["md"]
    assert "估算" in degraded["md"]


def test_healthy_script_md_stays_clean(healthy):
    assert "quota_table" not in healthy["md"], "正常包不该出现降级说明"


def test_segment_rewrite_keeps_the_signal(degraded):
    """单段重写会重建整份 result，别把信号洗掉。"""
    pl, jid = degraded["pl"], degraded["jid"]
    before = len(pl.get_job(jid).snapshot()["result"]["revisions"])
    pl.rewrite_segment(jid, RewriteSegmentRequest(index=1, feedback="更口语化"))
    deadline = time.time() + 30
    snap = pl.get_job(jid).snapshot()
    while time.time() < deadline:
        snap = pl.get_job(jid).snapshot()
        if snap["state"] == "failed":
            break
        if snap["state"] == "done" and len(snap["result"]["revisions"]) > before:
            break
        time.sleep(0.2)
    assert snap["state"] == "done", snap.get("error")
    assert snap["result"]["quota_degraded"] is True, "重写后信号丢了"
    f = next((degraded["tmp"] / "generated").glob(f"*/{jid}/result.json"))
    assert json.loads(f.read_text(encoding="utf-8"))["quota_degraded"] is True, \
        "重写后落盘的产物里信号丢了"


# ── 第 5 组：生成**之前**就要能看出这个包没配额表 ────────────
# 结果侧提示是「事后」的。包作者在设置页选参数时就该看到，否则他永远
# 不知道是自己漏了 quota_table，只会觉得「这工具算的字数不太对」。

def _audit(tmp: Path) -> dict:
    p = tmp / "packs" / "elevator" / "pack.yaml"
    return param_audit(tmp / "packs" / "elevator",
                       yaml.safe_load(p.read_text(encoding="utf-8")))


def test_param_audit_says_the_whole_table_is_missing():
    tmp = Path(tempfile.mkdtemp(prefix="p28-audit-missing-"))
    try:
        _strip_quota_table(tmp)
        note = _audit(tmp)["duration"]["60"]
        assert "整个 quota_table 缺失" in note, note
        assert "估算" in note, note
        # 修复前这里写的是「按相邻时长插值」—— 照它去补相邻档位，
        # 补完仍然是降级。审计本身在误导。
        assert "相邻时长插值" not in note, "整表缺失时说成插值是误导"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _audit_with_table(tmp: Path, keys: list[str]) -> dict:
    """把电梯包的 quota_table 换成只留 `keys` 这几档，返回 param_audit。"""
    p = _copy_packs(tmp)
    data = yaml.safe_load(p.read_text(encoding="utf-8"))
    table = data.pop("quota_table", None)
    assert table, "夹具前提：原包本来有 quota_table"
    rows = {str(k): v for k, v in table.items()}
    data["quota_table"] = {k: rows[k] for k in keys}
    p.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
                 encoding="utf-8")
    return param_audit(tmp / "packs" / "elevator", data)


def test_param_audit_still_says_interpolation_when_the_table_exists():
    """有表、只是缺这一档 —— 这时「插值」才是对的说明（补上这档就好）。"""
    tmp = Path(tempfile.mkdtemp(prefix="p28-audit-partial-"))
    try:
        note = _audit_with_table(tmp, ["60", "90"])["duration"]["180"]
        assert "相邻时长插值" in note, note
        assert "整个 quota_table 缺失" not in note, note
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_param_audit_flags_a_single_row_table():
    """只有一档时不是「插值」而是「任何时长都用这一档」—— 说错方向比不说更糟。"""
    tmp = Path(tempfile.mkdtemp(prefix="p28-audit-single-"))
    try:
        note = _audit_with_table(tmp, ["60"])["duration"]["180"]
        assert "只有 60 秒一档" in note, note
        assert "相邻时长插值" not in note, note
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_healthy_pack_audits_clean():
    """前提守卫：正常包不该有任何 duration 审计项，否则上面几条断言在测空气。"""
    tmp = Path(tempfile.mkdtemp(prefix="p28-audit-ok-"))
    try:
        _copy_packs(tmp)
        assert "duration" not in _audit(tmp), "正常包的时长参数不该有降级项"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
