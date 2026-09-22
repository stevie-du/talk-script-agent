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

第 22 轮复核 P3-1 抓到"切出那一处"之后剩下的两条喂判据的路，也一起堵了：
  - **同名函数**：`ast.walk` 命中第一份就返回 —— 在 `app/llm.py` 里再写一个模块级的
    `_capped_read`、把正确数字抄它那份 docstring，真句全改错仍 2 passed（实测）。
    现在要求那个名字在文件里恰好一处、且挂在 `LLMClient` 上。
  - **段内搬家**：把原句改错、在同一段末尾另起一行写一份正确的 —— `frag in src` 成立，
    连"整段只出现一次"也成立（正确的确实只有一份）。现在每条算式都必须写在
    **它自己那句说明的行里**（`_line_has` 锚点行），折行写的那两句退而求其次钉计数。
  - 顺手拆掉测试自己抄的第二本账：`BUDGET = 1200.0` 改成从 `app/jobs.py` 现读。
"""
from __future__ import annotations

import ast
import inspect
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import LLMConfig                          # noqa: E402
from app.llm import STREAM_READ_TIMEOUT_MULT, LLMClient   # noqa: E402

ILLUSTRATED_RETRIES = 3  # 两处文案里"举例那一档"用的 retries 值


def budget_seconds() -> float:
    """`JOB_BUDGET_SECONDS` **现读**，不在测试里抄第二份 1200.0。

    原来这里写死 `BUDGET = 1200.0`，另一处再用"文件里有没有 `JOB_BUDGET_SECONDS = 1200.0`
    这串字"把它钉住 —— 那是两本账加一条字符串判据：常量改名、或有人多抄一份赋值，
    判据就以另一种方式哑掉（第 22 轮复核 P3-1 顺手收掉）。
    """
    src = (ROOT / "app" / "jobs.py").read_text(encoding="utf-8")
    hits = re.findall(r"^JOB_BUDGET_SECONDS = ([0-9.]+)\s*$", src, re.M)
    assert len(hits) == 1, f"app/jobs.py 里 `JOB_BUDGET_SECONDS = <数>` 命中 {len(hits)} 处，" \
                           "要求恰好 1 处（多一处就有第二本预算，或者有人抄了一份来喂判据）"
    return float(hits[0])


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
    """`app/llm.py` 里 **`LLMClient._capped_read`** 自己那份 docstring，不是整个函数体。

    第 21 轮复核 Q4-a1：原来返回整个函数体源码，于是"在同一个函数体里加一个嵌套函数、
    把正确数字写进它的 docstring、真句全改错"就能骗过断言（实测 2 passed）。
    第 22 轮复核 P3-1 补第二格：`ast.walk` 命中第一份就返回 —— 再加一个**同名**函数
    （模块级、或挂别的类）并把正确数字塞它那份 docstring，真句改错照样绿。
    所以现在要求：这个名字在文件里**恰好一处**，而且它挂在 `LLMClient` 上。
    """
    src = (ROOT / "app" / "llm.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    owner_of = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    owner_of[sub] = node.name
    hits = [(owner_of.get(n, "<module>"), ast.get_docstring(n) or "")
            for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            and n.name == "_capped_read"]
    if len(hits) != 1:
        raise AssertionError(
            f"app/llm.py 里叫 `_capped_read` 的函数有 {len(hits)} 份"
            f"（分别挂在 {[o for o, _ in hits]}）—— 锚点不唯一，往任意一份里塞正确数字"
            "就能把断言喂饱。要么把那份真的搬走了（更新这里的锚点），要么别处重名了。")
    owner, doc = hits[0]
    assert owner == "LLMClient", \
        f"`_capped_read` 现在挂在 {owner} 上：锚点要跟着改，但先确认这不是复制出来的一份"
    assert doc, "_capped_read 没有 docstring 了：那段说明搬哪去了？"
    return doc


def _once(src: str, frag: str, where: str) -> None:
    """那个算式在该处**只出现一次**。

    第 22 轮复核 P3-1 的另一半：判据是 `frag in src` 时，在同一段里再补一行"正确"的
    算式，原来那句写错的就不用改了（实测把真句改错 + 追加一行正确 → 仍然绿）。
    重复出现还有一种正当情形：那段说明本来就在两处重述同一个数 —— 那要显式改判据，
    不能靠"多塞一行"蒙过去。
    """
    n = src.count(frag)
    assert n == 1, (f"{where}里「{frag}」出现 {n} 次，要求恰好 1 次："
                    "0 次＝那句没跟着代码改；多于 1 次＝有人往同一段里补了一份"
                    "正确的重述，那正是「改错原文也不会红」的口子")


def _only_line(src: str, anchor: str, where: str) -> str:
    hit = [ln for ln in src.splitlines() if anchor in ln]
    assert len(hit) == 1, (f"{where}里带锚点「{anchor}」的行有 {len(hit)} 行，要求恰好 1 行："
                           "0 行＝那句说明被改述或搬走了（去更新**文案**或这里的锚点，"
                           "别把判据放宽）；多行＝锚点不唯一")
    return hit[0]


def _line_has(src: str, anchor: str, frag: str, where: str) -> str:
    """`frag` 必须在**带锚点的那一行**里，而不是这段文字的任意角落。

    只有 `_once` 也挡不住**搬家**：把原句改错、在段末另起一行写一份正确的，
    "恰好出现一次"依然成立（实测如此）。判据要钉在那句说明身上 —— 锚点行里
    必须就是算出来的那个数。
    """
    line = _only_line(src, anchor, where)
    assert frag in line, (f"{where}「{anchor}」那一行的数字与代码不符，应为 {frag}"
                          f" —— 现在写的是：{line.strip()}")
    return line


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
    _once(src, default_line, "JOB_BUDGET_SECONDS 上方那段注释")
    _line_has(src, "出厂默认", default_line, "JOB_BUDGET_SECONDS 上方那段注释")
    # 同一个 per-attempt 秒数在 JobBudget 的说明里也重述了一遍（"180×2=360s"），
    # 那一处同样得跟着代码 —— 原来它不在任何判据里（第 20 轮复核 M_D）。
    budget_doc = _docstring_containing("app/jobs.py", "再乘上重试次数")
    per_attempt = _whole(timeout_default() * STREAM_READ_TIMEOUT_MULT)
    pa = f"{timeout_default()}×{STREAM_READ_TIMEOUT_MULT:g}={per_attempt}s"
    _once(budget_doc, pa, "JobBudget 的说明")
    _line_has(budget_doc, "STREAM_READ_TIMEOUT_MULT", pa, "JobBudget 的说明")
    if d != ILLUSTRATED_RETRIES:
        local_line = (f"(1+{rc})×(1+{mr})×(1+{ILLUSTRATED_RETRIES})="
                      f"{(rc + 1) * (mr + 1) * (ILLUSTRATED_RETRIES + 1)} 次")
        _once(src, local_line, "JOB_BUDGET_SECONDS 上方那段注释")
        _line_has(src, f"调到 {ILLUSTRATED_RETRIES} 就是", local_line,
                  "JOB_BUDGET_SECONDS 上方那段注释")


def test_llm_capped_read_comment_matches_the_wall_clock_math():
    src = _capped_read_src()
    layers = structural_retries() + 1
    per_attempt = _whole(timeout_default() * STREAM_READ_TIMEOUT_MULT)
    budget = budget_seconds()
    # 那一处也单独写了 "180s × 2 = 360s"（另一种拼法），同样得跟着代码
    pa = f"{timeout_default()}s × {STREAM_READ_TIMEOUT_MULT:g} = {per_attempt}s"
    _once(src, pa, "_capped_read 的说明")
    _line_has(src, "单次读超时", pa, "_capped_read 的说明")
    ratios = []
    for retries_at in sorted({retries_default(), ILLUSTRATED_RETRIES}):
        calls = retries_at + 1
        secs = layers * calls * per_attempt
        frag = f"{layers}×{calls}×{per_attempt} = {secs}s"
        _once(src, frag, "_capped_read 的说明")
        line = _line_has(src, f"`retries={retries_at}` 时是", frag, "_capped_read 的说明")
        assert f"{secs // 60} 分钟" in line, \
            f"那一行把 {secs}s 说成了别的分钟数，应为 {secs // 60} 分钟：{line.strip()}"
        ratios.append(round(secs / budget, 1))
    # 倍数与预算本体那两句是**折行**写的，钉不了"同一行"，那就钉"整段只出现一次"：
    # 每个倍数、以及它引用的预算秒数，都必须是现算的那一个、且只写一处。
    for r in sorted(set(ratios)):
        assert src.count(str(r)) == 1, \
            f"倍数 {r}（最坏秒数 / 预算 {budget:g}s）在那段说明里出现 {src.count(str(r))} 次，" \
            "要求恰好 1 次：0 次＝没跟着代码改，多过 1 次＝有人补了一份正确的重述"
    assert src.count(f"（{budget:g}s）") == 1, \
        f"那段说明引用的预算不是 {budget:g}s（或写了多处）—— 改了 JOB_BUDGET_SECONDS 要一起改"
