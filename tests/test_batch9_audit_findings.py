# -*- coding: utf-8 -*-
"""独立审计（批次 9 之后）反证到的五处残留，逐条钉住。

审计方式：MockTransport 逐次记录请求的 `max_tokens` 与整份 payload，
真跑 mock 生成读 steps，改 `jobs.JOB_BUDGET_SECONDS` 看文案。
下面每条都对应审计里一句「真修好，但……」的后半。
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import jobs as jobs_mod                          # noqa: E402
from app.config import LLMConfig, load_config             # noqa: E402
from app.llm import (RETRY_UPGRADE_CAP, EmptyContentError,  # noqa: E402
                     LLMClient)
from app.pipeline import Pipeline, ScriptDraft, wait_job   # noqa: E402
from app.schemas import GenerateRequest                    # noqa: E402


def _empty_client(handler, max_tokens):
    cfg = LLMConfig(base_url="http://x/v1", api_key="k", model="m",
                    max_tokens=max_tokens, retries=0)
    client = LLMClient(cfg)
    real = httpx.Client

    def factory():
        return real(transport=httpx.MockTransport(handler))
    return client, factory


# ── 1. 预算到顶之后不许再发同一份请求（审计：cap 之后 R3.3 复活）──
def test_no_identical_resend_once_budget_hits_the_cap(monkeypatch):
    calls = []

    def handler(req):
        body = json.loads(req.content)
        calls.append((body["max_tokens"], json.dumps(body, sort_keys=True)))
        return httpx.Response(200, json={"choices": [
            {"message": {"content": ""}, "finish_reason": "length"}]})

    # 19000 → 升到 20000 就到顶；第三次若再发就是**逐字节相同**的请求
    client, factory = _empty_client(handler, 19000)
    monkeypatch.setattr("app.llm.get_client", factory)
    with pytest.raises(EmptyContentError) as ei:
        client.chat_json("write", "s", "u", ScriptDraft, max_retries=3)
    budgets = [c[0] for c in calls]
    assert budgets == [19000, RETRY_UPGRADE_CAP], \
        f"到顶之后还在原样重发：{budgets}"
    payloads = [c[1] for c in calls]
    assert len(set(payloads)) == len(payloads), "出现了逐字节相同的重发"
    assert "上限" in str(ei.value) and "不再原样重发" in str(ei.value)


def test_truncated_resend_is_still_allowed_at_the_cap(monkeypatch):
    """到顶但**这一次带了「请完整输出」的追加消息** —— payload 变了，就该再试。

    不许把上一条修复做成"到顶一律不再重试"：那会连唯一还有救的那一支一起砍掉。
    """
    calls = []

    def handler(req):
        body = json.loads(req.content)
        calls.append(len(body["messages"]))
        return httpx.Response(200, json={"choices": [{
            "message": {"content": '{"sections": ['},   # 非空但残缺
            "finish_reason": "length"}]})

    client, factory = _empty_client(handler, RETRY_UPGRADE_CAP)
    monkeypatch.setattr("app.llm.get_client", factory)
    with pytest.raises(EmptyContentError):
        client.chat_json("write", "s", "u", ScriptDraft, max_retries=2)
    assert len(calls) == 3, f"截断这一支在到顶后被一并砍掉了：{calls}"
    assert calls == [2, 4, 6], "每轮必须多带一对『上一版 + 请完整输出』消息"


# ── 2. 预算文案读实时值（审计：from-import 副本会造成两本账）──
def test_budget_message_reads_the_live_value(monkeypatch):
    monkeypatch.setattr(jobs_mod, "JOB_BUDGET_SECONDS", 60.0)
    j = jobs_mod.Job("b-1", "generate", {})
    import time
    j.started_at = time.time() - 61
    assert j.overdue()
    with pytest.raises(jobs_mod.JobBudget) as ei:
        Pipeline._stop_check(j)
    assert "1 分钟" in str(ei.value), \
        f"触发按实时值判、文案却报 import 那一刻的副本：{ei.value}"


# ── 3. limits 的每一档改动都要看得见 ───────────────────────
def _pack_with_rounds(value) -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="limits-"))
    shutil.copytree(ROOT / "packs", tmp / "packs")
    f = tmp / "packs" / "elevator" / "skill.yaml"
    data = yaml.safe_load(f.read_text(encoding="utf-8"))
    data["limits"] = {"recheck_rounds": value}
    f.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
                 encoding="utf-8")
    return tmp


def _steps_of(tmp: Path) -> list:
    cfg = load_config(tmp)
    cfg.mock = True
    pl = Pipeline(tmp, cfg)
    jid = pl.start_generate(GenerateRequest(pack="elevator", topic="被困电梯怎么办",
                                           duration=60, platform="抖音"))
    snap = wait_job(pl, jid, timeout=90)
    assert snap["state"] == "done", snap.get("error")
    return [s["key"] for s in snap["steps"]]


@pytest.mark.parametrize("value,expect", [
    (0, "limits_zero"),            # 静默"不回炉"是最难查的那种配置生效
    (2.9, "limits_bad"),           # 截断成 2 也要说
    ("abc", "limits_bad"),
    (-1, "limits_bad"),
])
def test_each_limits_anomaly_leaves_a_trace(value, expect):
    tmp = _pack_with_rounds(value)
    try:
        keys = _steps_of(tmp)
        assert expect in keys, f"recheck_rounds={value!r} 没有留痕：{keys}"
        # 原来这行末尾挂着 `or True`，整条断言永远为真（等于没写）。真判据是
        # "只留这一条 limits_* 痕"：同一种异常被两处各记一次，界面就会出现两条
        # 说的同一件事的步骤，而这正是这里要挡的。
        assert [k for k in keys if k.startswith("limits_")] == [expect], keys
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_normal_rounds_leave_no_limits_noise():
    tmp = _pack_with_rounds(2)
    try:
        keys = _steps_of(tmp)
        assert not any(k.startswith("limits_") for k in keys), keys
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ── 4. 建包作业的中途检查点也要认预算闸 ─────────────────────
def test_packgen_abort_predicate_covers_the_budget(tmp_path, monkeypatch):
    """审计指出 `should_abort=job.is_cancelled` 只认取消不认预算。"""
    from app.packgen import create_pack
    monkeypatch.setattr(jobs_mod, "JOB_BUDGET_SECONDS", 0.0)
    j = jobs_mod.Job("pg-1", "packgen", {"industry": "宠物医院"})
    import time
    j.started_at = time.time() - 1
    should_abort = lambda: j.is_cancelled() or j.overdue()   # noqa: E731
    assert should_abort() is True, "预算闸没进建包的检查点"

    cfg = load_config(ROOT)
    cfg.mock = True
    client = LLMClient(cfg.llm, mock=True)
    called = {"n": 0}

    class Counting(LLMClient):
        def chat_json(self, *a, **kw):
            called["n"] += 1
            return super().chat_json(*a, **kw)

    c = Counting(cfg.llm, mock=True)
    with pytest.raises(Exception):
        create_pack(tmp_path, c, "宠物医院", "描述够长了吧", should_abort=should_abort)
    assert called["n"] <= 1, "取消点应在模型调用之后、写盘之前生效"
    assert not (tmp_path / "packs").exists() or \
        not any((tmp_path / "packs").glob("*")), "超预算还留下了半个目录"


# ── 5. mock 夹具要跟着契约走（否则投影只在单测里成立）────────
def test_mock_artifact_fills_the_scene_contract(tmp_path):
    tmp = Path(tempfile.mkdtemp(prefix="fixture-"))
    shutil.copytree(ROOT / "packs", tmp / "packs")
    cfg = load_config(tmp)
    cfg.mock = True
    pl = Pipeline(tmp, cfg)
    jid = pl.start_generate(GenerateRequest(pack="elevator", topic="被困电梯怎么办",
                                           duration=60, platform="抖音",
                                           style="亲和接地气"))
    snap = wait_job(pl, jid, timeout=90)
    assert snap["state"] == "done", snap.get("error")
    scenes = snap["result"]["scenes"]
    assert scenes[0]["audio"]["bgm"], "夹具没填 bgm → 这条投影只在单测里被验过"
    assert scenes[0]["shot_type"] == "closeup"
    assert scenes[1]["visual"]["transition"] == "dissolve"
    # 最后一镜的 style 留空 → 回落到本次生成的风格参数，而不是空串
    assert scenes[-1]["style"] == "亲和接地气", scenes[-1]["style"]
    shutil.rmtree(tmp, ignore_errors=True)


# ── 6. 测试不许在模块顶层改环境变量（我自己踩过，进程级污染）────────
def test_no_test_module_mutates_environ_at_import_time():
    """`os.environ[...] = ...` / `setdefault(...)` 写在模块顶层 = 污染整个 pytest 进程。

    真实后果（本轮我自己犯的）：一条新测试在 import 时
    `os.environ.setdefault("TALKSCRIPT_MOCK", "1")`，而 `load_config` 每次都读它
    —— 于是后面所有「没配模型必须被拒」的用例统统放行：
    `test_config_fresh` 拿到 200 + job_id（本该 400）、两条 models_list 拒绝用例、
    还有一条 mock 标记用例一起红。单跑全绿、全量跑才红 —— 顺序依赖最难查。

    规则不是"绝对不许改环境变量"：`test_env_bounds` / `test_llm_retry` 都在
    **函数内**改并在 `finally` 里还原，那是正当用法。这条守卫只禁"顶层"。
    """
    import ast

    offenders = []
    for f in sorted((ROOT / "tests").glob("test_*.py")):
        tree = ast.parse(f.read_text(encoding="utf-8"), filename=str(f))
        for node in tree.body:                     # 只看模块顶层的语句
            targets = []
            if isinstance(node, ast.Assign):
                targets = node.targets
            elif isinstance(node, (ast.Expr,)) and isinstance(node.value, ast.Call):
                call = node.value
                if _is_environ_mutation(call):
                    offenders.append(f"{f.name}:{node.lineno} 顶层 setdefault/pop")
                    continue
            for t in targets:
                if isinstance(t, ast.Subscript) and _is_environ(t.value):
                    offenders.append(f"{f.name}:{node.lineno} 顶层写 os.environ")
    assert not offenders, \
        "测试在模块顶层改了环境变量（会污染同进程后续用例）：\n" + "\n".join(offenders)


def _is_environ(node) -> bool:
    import ast
    return (isinstance(node, ast.Attribute) and node.attr == "environ"
            and isinstance(node.value, ast.Name) and node.value.id == "os") \
        or (isinstance(node, ast.Attribute) and node.attr == "environ"
            and isinstance(node.value, ast.Name) and node.value.id == "environ")


def _is_environ_mutation(call) -> bool:
    import ast
    fn = call.func
    return isinstance(fn, ast.Attribute) and fn.attr in ("setdefault", "pop") \
        and isinstance(fn.value, ast.Attribute) and fn.value.attr == "environ"


# ── 6. README 的最坏请求数口径要有机器账（报价数字与代码里的默认重试次数同源）──
def _chat_json_default_and_overrides(root: Path):
    """读 chat_json 的 max_retries 默认值，以及 app/ 里显式抬高它的调用点。"""
    import ast
    default = None
    tree = ast.parse((root / "app" / "llm.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "chat_json":
            for arg, d in zip(reversed(node.args.args), reversed(node.args.defaults)):
                if arg.arg == "max_retries" and isinstance(d, ast.Constant):
                    default = d.value
    overrides = []
    for path in sorted((root / "app").glob("*.py")):
        call_tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(call_tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
            if name != "chat_json":
                continue
            for kw in node.keywords:
                if (kw.arg == "max_retries" and isinstance(kw.value, ast.Constant)
                        and kw.value.value != default):
                    overrides.append(f"{path.name}:{node.lineno}")
    return default, overrides


def test_readme_worst_case_request_count_is_the_real_one():
    """README 用「逻辑调用数 × 2 × (llm.retries + 1)」报价最坏 HTTP 请求数。

    那个 2 是 chat_json 的默认尝试数，不是随手写的常数：默认值一改、或有调用点
    显式抬高 max_retries，README 的 30/40 就同时失真。这条路径有过"看着在、其实
    从没执行"的前科（批次 9 审计），报价不该只靠人记得同步。
    """
    import re
    default, overrides = _chat_json_default_and_overrides(ROOT)
    assert default is not None, "读不到 chat_json 的 max_retries 默认值"
    assert not overrides, \
        f"这些调用点抬高了 max_retries，README 的倍数要跟着改：{overrides}"
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    claimed = [int(n) for n in re.findall(r"逻辑调用数 × (\d+) ×", readme)]
    assert claimed == [default + 1], \
        f"README 报价的倍数 {claimed} 与真实默认尝试数 {default + 1} 不同源"
    for retries, expected in ((2, 30), (3, 40)):
        computed = 5 * (default + 1) * (retries + 1)
        assert computed == expected, \
            f"retries={retries} 时最坏应是 {computed} 次请求，README 还写着 {expected}"
        assert f"**{expected}**" in readme, \
            f"README 里 retries={retries} 的那句没标 {expected}，或数字还没同步"


def test_readme_lists_as_many_certification_checks_as_the_script_makes():
    """README 说 `npm run verify:package` 「查 N 样」，那个 N 由脚本自己报（第 22 轮）。

    加一条判据（本轮就加了 H 条：Setup 载荷 ↔ win-unpacked）而 README 不改，
    下一个人会以为脚本少查一步；反过来 README 加了而脚本没加，就是照着一份
    不存在的清单验收。所以数目不许靠人记得同步 —— 与上面那条 README 报价同源。
    """
    import re

    script = (ROOT / "desktop" / "scripts" / "verify-package.mjs").read_text(encoding="utf-8")
    letters = re.findall(r"^//   ([A-Z])\.", script, re.M)
    assert letters and letters == sorted(set(letters)), \
        f"脚本头部的判据清单读不出来（要的是 `//   A.` 这种行、且字母不重复）：{letters}"
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    claimed = [int(n) for n in re.findall(r"它查 (\d+) 样", readme)]
    assert claimed, "README 里那句「它查 N 样」被改述了：锚点与数目都要跟着改"
    assert claimed == [len(letters)], \
        f"README 报「{'、'.join(str(c) for c in claimed)} 样」，脚本实际列了 {len(letters)} 条" \
        f"（{''.join(letters)}）—— 改脚本就一起改 README，别把这一格放宽"
