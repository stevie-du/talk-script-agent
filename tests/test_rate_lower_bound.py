# -*- coding: utf-8 -*-
"""rate_by_style 配成 0：语速下限必须收在 `Pack.rate_for_style` 出口。

背景
----
`rate_by_style: {快节奏: 0}` 是很容易写出来的（想表达「很快」却写成了 0）。
修复前 `float(rates.get(style, 4.5))` 会把包里的 0 原样返回，于是**同一份包配置、
两处结论不同**：

- `checker.estimate_seconds` 自己有 `r <= 0 → DEFAULT_RATE` 兜底 → 校验说「没问题」；
- `pipeline._compute_timings` 直接算 `n / rate`，没有任何保护 → 在**烧完 token
  之后**的组装阶段炸 `ZeroDivisionError`，用户完全看不出根因。

所以这里要钉三件事，缺一条就还是「靠某一处碰巧没崩」：

1. 出口值恒 > 0 —— 下限真的收在这一层，而不是散在调用点；
2. `_compute_timings` 拿到出口值不再抛，且时间轴单调递增（不是返回了个空壳）；
3. 两处兜底值是**同一个常量**（`checker.DEFAULT_RATE`）—— 否则下次还会分叉。

第 4 条 `test_raw_zero_still_crashes` 是**反空转守卫**：证明上面的「不抛」不是
因为 `_compute_timings` 本身吞掉了异常，而是真的拿到了合法语速。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import checker                      # noqa: E402
from app.knowledge import Pack               # noqa: E402
from app.pipeline import Pipeline            # noqa: E402

PACK_YAML = {
    "name": "demo",
    "display_name": "演示包",
    "params": {
        "segment": {"label": "细分领域", "default": "维保", "options": ["维保"]},
        "audience": {"label": "受众", "default": "业主", "options": ["业主"]},
        "duration": {"label": "时长", "default": 60, "options": [60]},
        "style": {"label": "风格", "default": "亲和", "options": ["亲和"]},
        "platform": {"label": "平台", "default": "抖音", "options": ["抖音"]},
        "persona": {"label": "人设", "default": "老师傅", "options": ["老师傅"]},
    },
    "rate_by_style": {"亲和": 4.5},
}


def _pack(tmp_path: Path, rates: dict) -> Pack:
    """建一个只关心 rate_by_style 的最小行业包。"""
    data = yaml.safe_load(yaml.safe_dump(PACK_YAML, allow_unicode=True))
    data["rate_by_style"] = rates
    pack_dir = tmp_path / "packs" / "demo"
    pack_dir.mkdir(parents=True, exist_ok=True)
    (pack_dir / "pack.yaml").write_text(
        yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return Pack(tmp_path, "demo")


SECTIONS = [
    {"type": "hook", "text": "你好世界"},
    {"type": "body", "text": "第二段的内容在这里"},
]


# ── 1. 出口值恒 > 0 ────────────────────────────────────────────

def test_zero_rate_is_clamped_at_exit(tmp_path):
    """包里写 0 → 出口必须是正数，不能把 0 原样放行。"""
    pack = _pack(tmp_path, {"快节奏": 0})
    rate = pack.rate_for_style("快节奏")
    assert rate > 0, f"出口仍是 {rate}，_compute_timings 会除零"
    assert rate == checker.DEFAULT_RATE


@pytest.mark.parametrize("bad", [0, 0.0, -3.0, "abc", None, True])
def test_all_illegal_rates_fall_back(tmp_path, bad):
    """0 / 负数 / 非数 / 缺省值 —— 全部退回兜底，一个都不能漏。

    `True` 是 YAML 里 `on`/`yes` 的解析结果，`float(True) == 1.0` 虽然不除零，
    但把布尔当语速是明显的配置事故；这里只要求「结果为正且不崩」。
    """
    pack = _pack(tmp_path, {"怪风格": bad})
    rate = pack.rate_for_style("怪风格")
    assert isinstance(rate, float) and rate > 0


def test_valid_rate_is_preserved(tmp_path):
    """下限不能顺手把所有值都拍平 —— 正常配置必须原样生效。"""
    pack = _pack(tmp_path, {"慢": 6.0})
    assert pack.rate_for_style("慢") == 6.0


# ── 2. 组装阶段不再炸 ─────────────────────────────────────────

def test_compute_timings_survives_zero_rate(tmp_path):
    """修复前这条会抛 ZeroDivisionError，且发生在烧完 token 之后。"""
    pack = _pack(tmp_path, {"快节奏": 0})
    rate = pack.rate_for_style("快节奏")

    timings = Pipeline._compute_timings(SECTIONS, rate)

    assert len(timings) == len(SECTIONS)
    # 不是空壳：时间轴必须真的往前走
    assert timings[0]["start"] == 0.0
    assert timings[0]["end"] > 0
    assert timings[-1]["end"] > timings[0]["end"]
    for t in timings:
        assert t["end"] >= t["start"]


def test_raw_zero_still_crashes(tmp_path):
    """反空转守卫：直接喂 0 确实会抛 —— 证明上面的「不抛」是真拿到了合法语速。

    如果哪天有人给 `_compute_timings` 加了内部兜底，这条会变红，
    提醒把兜底统一收回 `rate_for_style` 出口（避免又出现两处口径）。
    """
    with pytest.raises(ZeroDivisionError):
        Pipeline._compute_timings(SECTIONS, 0.0)


# ── 3. 单一来源 ───────────────────────────────────────────────

def test_fallback_value_is_single_source(tmp_path):
    """「没配过的风格」与「配了 0 的风格」必须给出同一个值。

    修复前 knowledge 里写死 4.5、checker 里另有 DEFAULT_RATE；
    改一处忘另一处就会再次分叉。这条把两者绑在一起。
    """
    pack = _pack(tmp_path, {"快节奏": 0})
    assert pack.rate_for_style("没配过") == pack.rate_for_style("快节奏")
    assert pack.rate_for_style("没配过") == checker.DEFAULT_RATE


def test_estimate_seconds_agrees_with_exit(tmp_path):
    """校验口径与组装口径一致：同字数下 estimate_seconds 的推算不慢于出口语速。"""
    pack = _pack(tmp_path, {"快节奏": 0})
    rate = pack.rate_for_style("快节奏")
    text = "你好世界"

    # estimate_seconds 是「字数/语速 + 段间停顿」，单段无停顿 → 纯字数/语速
    assert checker.estimate_seconds(text, rate) == pytest.approx(
        checker.count_chars(text) / rate)


def test_missing_table_entirely(tmp_path):
    """整个 rate_by_style 都没写 —— 同样走兜底，不崩。"""
    pack = _pack(tmp_path, {})
    assert pack.rate_for_style("亲和") == checker.DEFAULT_RATE
