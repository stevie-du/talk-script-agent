# -*- coding: utf-8 -*-
"""注释里那些「最坏多少次 / 最坏多少秒」必须能从代码算出来（第 17 轮）。

同一个毛病本项目栽过好几次：文案写了一个不带口径的聚合数（"≈48 分钟"、"2.4 倍"、
"(1+2)×(1+1)×(1+3)"），后来某一层默认值改了、数字没人跟着改，于是注释开始教错的东西，
而**没有任何一条测试会红** —— 读注释的人（和写注释的我）只能靠运气发现。
§15.29 与第 16 轮复核都点到过这两处（`app/llm.py` 的倍率、`app/jobs.py` 的叠乘式）。

这里把那两句变成**可算的**：从代码里读出各层参数、重算一遍，要求文案里出现算出来的
那几个字符串。改动任何一层 —— `LLMConfig.retries`/`timeout` 默认、
`STREAM_READ_TIMEOUT_MULT`、`chat_json` 的 `max_retries`、行业包的
`limits.recheck_rounds` —— 都会立刻红在那句注释上，而不是等到有人去掐表。
"""
from __future__ import annotations

import inspect
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import LLMConfig                        # noqa: E402
from app.llm import STREAM_READ_TIMEOUT_MULT, LLMClient  # noqa: E402

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


def test_jobs_budget_comment_matches_the_retry_layers():
    src = (ROOT / "app" / "jobs.py").read_text(encoding="utf-8")
    rc, mr = recheck_rounds(), structural_retries()
    d = retries_default()
    default_line = f"(1+{rc})×(1+{mr})×(1+{d})={(rc + 1) * (mr + 1) * (d + 1)} 次"
    assert default_line in src, (
        f"app/jobs.py 的叠乘式与代码不符，应为 {default_line}"
        f"（回炉 {rc + 1} × 结构 {mr + 1} × 请求 {d + 1}）—— 改了默认值就把注释一起改")
    assert "JOB_BUDGET_SECONDS = 1200.0" in src, \
        "预算常量不再是 1200s：这段注释（以及 llm.py 里那句'整作业预算的 N 倍'）都要跟着改"
    if d != ILLUSTRATED_RETRIES:
        local_line = (f"(1+{rc})×(1+{mr})×(1+{ILLUSTRATED_RETRIES})="
                      f"{(rc + 1) * (mr + 1) * (ILLUSTRATED_RETRIES + 1)} 次")
        assert local_line in src, f"举例那一档的算式也该跟着改：{local_line}"


def test_llm_capped_read_comment_matches_the_wall_clock_math():
    src = (ROOT / "app" / "llm.py").read_text(encoding="utf-8")
    layers = structural_retries() + 1
    per_attempt = _whole(timeout_default() * STREAM_READ_TIMEOUT_MULT)
    for retries_at in sorted({retries_default(), ILLUSTRATED_RETRIES}):
        calls = retries_at + 1
        secs = layers * calls * per_attempt
        frag = f"{layers}×{calls}×{per_attempt} = {secs}s"
        assert frag in src, (
            f"app/llm.py `_capped_read` 里那句最坏墙钟与代码不符：retries={retries_at} 时"
            f"应是 {frag}（结构层 {layers} × 请求层 {calls} × 单次 {per_attempt}s）")
        ratio = round(secs / BUDGET, 1)
        assert str(ratio) in src, \
            f"文案里该出现 {ratio}（{secs}s / 预算 {BUDGET}s）—— 那是这段说明存在的理由"
