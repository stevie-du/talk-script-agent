# -*- coding: utf-8 -*-
"""注释里那些「最坏多少次 / 最坏多少秒」必须能从代码算出来（第 17 / 20 轮）。

同一个毛病本项目栽过好几次：文案写了一个不带口径的聚合数（"≈48 分钟"、"2.4 倍"、
"(1+2)×(1+1)×(1+3)"），后来某一层默认值改了、数字没人跟着改，于是注释开始教错的东西，
而**没有任何一条测试会红** —— 读注释的人（和写注释的我）只能靠运气发现。
§15.29 与第 16 轮复核都点到过这两处（`app/llm.py` 的倍率、`app/jobs.py` 的叠乘式）。

第 20 轮复核又抓出这条守卫自己的两个毛病，一并修了：
  - 它拿"整个文件里有没有这个子串"当判据 —— 在文件末尾追加一行无关注释就能把断言喂饱
    （M_B），而它守的其实是某一句特定的话；现在改成先切出**那一处**
    （`_capped_read` 的函数体 / `JOB_BUDGET_SECONDS` 上方那段注释）再断言。
  - 同一个 per-attempt 秒数在三处重述，而它只护住一处（M_D）：现在三处都钉。
"""
from __future__ import annotations

import ast
import inspect
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import LLMConfig                          # noqa: E402
from app.llm import STREAM_READ_TIMEOUT_MULT, LLMClient   # noqa: E402

BUDGET = 1200.0          # app/jobs.py 的 JOB_BUDGET_SECONDS，下面两句文案都拿它作分母
ILLUSTRATED_RETRIES = 3  # 两处文案里"举例那一档"用的 retries 值


def _whole(x) -> int:
    v = int(round(float(x)))
    assert abs(float(x) - v) < 1e-9, f"{x} 不是整数，文案的写法要跟着改"
    return v


def recheck_rounds() -> int:
    """出厂电梯包 `skill.yaml` 的 `limits.recheck_rounds`（回炉轮数，不含首轮）。"""
    cfg = yaml.safe_load((ROOT / "packs" / "elevator" / "skill.yaml")
                         .read_text(encoding="utf-8"))
    return _whole(((cfg.get("limits") or {}).get("recheck_rounds")) or 0)


def structural_retries() -> int:
    """`chat_json` 的结构回炉次数（`max_retries`，不含首轮）。"""
    return _whole(inspect.signature(LLMClient.chat_json)
                  .parameters["max_retries"].default)


def retries_default() -> int:
    return _whole(LLMConfig.__dataclass_fields__["retries"].default)


def timeout_default() -> int:
    return _whole(LLMConfig.__dataclass_fields__["timeout"].default)


def _capped_read_src() -> str:
    """`app/llm.py` 里 `_capped_read` **自己那份** docstring，不是整个函数体。

    第 21 轮复核 Q4-a1：原来返回整个函数体源码，于是"在同一个函数体里加一个嵌套函数、
    把正确数字写进它的 docstring、真句全改错"就能骗过断言（实测 2 passed）。
    """
    src = (ROOT / "app" / "llm.py").read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.FunctionDef) and node.name == "_capped_read":
            doc = ast.get_docstring(node) or ""
            assert doc, "_capped_read 没有 docstring 了：那段说明搬哪去了？"
            return doc
    raise AssertionError("app/llm.py 里找不到 _capped_read：那段说明搬哪去了？")


def _jobs_budget_comment() -> str:
    """`app/jobs.py` 里 `JOB_BUDGET_SECONDS` 上方那一段注释，不是整个文件。"""
    lines = (ROOT / "app" / "jobs.py").read_text(encoding="utf-8").splitlines()
    for i, ln in enumerate(lines):
        if ln.startswith("JOB_BUDGET_SECONDS"):
            j = i - 1
            while j >= 0 and (lines[j].lstrip().startswith("#") or not lines[j].strip()):
                j -= 1
            return "\n".join(lines[j + 1:i])
    raise AssertionError("app/jobs.py 里找不到 JOB_BUDGET_SECONDS")


def _docstring_containing(rel_path: str, marker: str) -> str:
    """找 `rel_path` 里含 `marker` 那句的那份 docstring（改述会红，不会静默失守）。"""
    src = (ROOT / rel_path).read_text(encoding="utf-8")
    tree = ast.parse(src)
    hits = []
    for node in ast.walk(tree):
        # 模块 docstring 不算（第 21 轮复核 Q4-a2：把 marker 那句抄进模块 docstring
        # 就能让"真句改错"继续绿），只有函数/类自己那份才算。
        if node is tree or not isinstance(
                node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        doc = ast.get_docstring(node) or ""
        if marker in doc:
            hits.append((node.name, doc))
    if len(hits) != 1:
        raise AssertionError(
            f"{rel_path} 里提到「{marker}」的 docstring 命中 {len(hits)} 份"
            f"（{[n for n, _ in hits]}），不是恰好一份："
            "0 份＝那段说明被搬走或改述了（去更新**文案**，别改 marker）；"
            "多份＝锚点不唯一，往任意一份里塞正确数字就能把断言喂饱。")
    return hits[0][1]


def test_jobs_budget_comment_matches_the_retry_layers():
    src = _jobs_budget_comment()
    rc, mr = recheck_rounds(), structural_retries()
    d = retries_default()
    default_line = f"(1+{rc})×(1+{mr})×(1+{d})={(rc + 1) * (mr + 1) * (d + 1)} 次"
    assert default_line in src, (
        f"JOB_BUDGET_SECONDS 上方那段注释与代码不符，应为 {default_line}"
        f"（回炉 {rc + 1} × 结构 {mr + 1} × 请求 {d + 1}）—— 改了默认值就把注释一起改")
    # 同一个 per-attempt 秒数在 JobBudget 的说明里也重述了一遍（"180×2=360s"），
    # 那一处同样得跟着代码 —— 原来它不在任何判据里（第 20 轮复核 M_D）。
    budget_doc = _docstring_containing("app/jobs.py", "再乘上重试次数")
    per_attempt = _whole(timeout_default() * STREAM_READ_TIMEOUT_MULT)
    assert f"{timeout_default()}×{STREAM_READ_TIMEOUT_MULT:g}={per_attempt}s" in budget_doc, \
        f"JobBudget 里 per-attempt 那句与代码不符，应为 {timeout_default()}×" \
        f"{STREAM_READ_TIMEOUT_MULT:g}={per_attempt}s"
    jobs_src = (ROOT / "app" / "jobs.py").read_text(encoding="utf-8")
    assert "JOB_BUDGET_SECONDS = 1200.0" in jobs_src, \
        "预算常量不再是 1200s：这段注释（以及 llm.py 里那句'整作业预算的 N 倍'）都要跟着改"
    if d != ILLUSTRATED_RETRIES:
        local_line = (f"(1+{rc})×(1+{mr})×(1+{ILLUSTRATED_RETRIES})="
                      f"{(rc + 1) * (mr + 1) * (ILLUSTRATED_RETRIES + 1)} 次")
        assert local_line in src, f"举例那一档的算式也该跟着改：{local_line}"


def test_llm_capped_read_comment_matches_the_wall_clock_math():
    src = _capped_read_src()
    layers = structural_retries() + 1
    per_attempt = _whole(timeout_default() * STREAM_READ_TIMEOUT_MULT)
    # 那一处也单独写了 "180s × 2 = 360s"（另一种拼法），同样得跟着代码
    assert f"{timeout_default()}s × {STREAM_READ_TIMEOUT_MULT:g} = {per_attempt}s" in src, \
        f"_capped_read 里 per-attempt 那一句与代码不符，应为 {timeout_default()}s × " \
        f"{STREAM_READ_TIMEOUT_MULT:g} = {per_attempt}s"
    for retries_at in sorted({retries_default(), ILLUSTRATED_RETRIES}):
        calls = retries_at + 1
        secs = layers * calls * per_attempt
        frag = f"{layers}×{calls}×{per_attempt} = {secs}s"
        assert frag in src, (
            f"_capped_read 里那句最坏墙钟与代码不符：retries={retries_at} 时应是 {frag}"
            f"（结构层 {layers} × 请求层 {calls} × 单次 {per_attempt}s）")
        ratio = round(secs / BUDGET, 1)
        assert str(ratio) in src, \
            f"文案里该出现 {ratio}（{secs}s / 预算 {BUDGET}s）—— 那是这段说明存在的理由"
