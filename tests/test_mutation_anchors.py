# -*- coding: utf-8 -*-
"""变异表自身的完整性（第 29 轮，2026-09-29）。

判据只有一条：`_verify/mutate.py` 的 MUTATIONS 里每条的 `old` 锚点，必须在它自己的
目标文件里**恰好出现一次**。

为什么这条守卫值得存在：这张表两百多条，而 `-k` 跑子集时**看不见别处的腐烂**；
一条锚点烂掉的变异在轮到它之前没有任何症状（轮到时的表现是一句
「旧串没找到 → SKIP → 记一条不合格」）。实测同一棵树上攒了 **22 条**，
其中一条（s14-1）背后还压着一条已经悄悄降级成空转的判据 ——
"少跑一条变异"不是最坏的，"少跑变异而没人知道"才是。
所以这里把它接成常规门：`pytest` 每次全量都扫，`_verify/mutate.py` 启动时也扫
（两处用的是 `_preflight_mutations.drifted` **同一个函数**，不另写一套口径）。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PREFLIGHT = ROOT / "_verify" / "_preflight_mutations.py"


def _pf():
    spec = importlib.util.spec_from_file_location("_preflight_mutations", PREFLIGHT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_anchor_table_is_not_rotten():
    pf = _pf()
    rows = pf.load_mutations()
    assert len(rows) >= 100, (
        f"只解析出 {len(rows)} 条变异 —— 解析方式与 `_verify/mutate.py` 的表结构对不上了，"
        "这条守卫正在空转（先修解析，再谈锚点）")
    bad = pf.drifted(rows)
    assert not bad, (
        "变异表有锚点漂移。重指到当前写法上（别删条目：删掉等于把这条守卫静默撤走；"
        "确实已经不存在的那个机制要撤，就在原地留一条注释写明为什么）：\n"
        + "\n".join(bad))


def test_the_drift_checker_can_actually_fail():
    """正对照：这条守卫不是恒真。

    本仓反复栽在同一类上 —— 一条"看起来在检查"的判据其实什么都抓不到。
    所以这里把坏形状的**四种**各喂一遍，要求每种都被点名，再喂一条真锚点要求它不被误报。
    """
    pf = _pf()
    rows = pf.load_mutations()
    real = rows[0]
    good = ("合成-正常", real[1], real[2], "x", "verify:topics")
    gone = ("合成-锚点没了", real[1], "这一串绝不可能出现在文件里 __ROT__", "x", "verify:topics")
    twice = ("合成-锚点两次", PREFLIGHT, "if not p.exists():", "x", "verify:topics")
    empty = ("合成-空锚点", real[1], "", "x", "verify:topics")
    nofile = ("合成-缺文件", ROOT / "app" / "没有这个文件.py", "x", "x", "verify:topics")
    out = pf.drifted([good, gone, twice, empty, nofile])
    joined = "\n".join(out)
    assert all(k not in joined for k in ("合成-正常",)), out
    for label in ("合成-锚点没了", "合成-锚点两次", "合成-空锚点", "合成-缺文件"):
        assert label in joined, f"{label} 没被 drift 检查点出来：{out}"
    assert "锚点 0 次" in joined and "锚点 2 次" in joined, out


def test_mutate_py_shares_the_same_check():
    """`mutate.py` 启动时用的必须是**同一个** `drifted()`，不是它自己抄的一份。

    两份检查迟早一份严一份松，而那正是"表里明明有漂移、跑起来却一声不响"的形状。
    """
    src = (ROOT / "_verify" / "mutate.py").read_text(encoding="utf-8")
    assert "_preflight_mutations" in src and ".drifted(MUTATIONS)" in src, (
        "mutate.py 没有接进共享的锚点检查 —— 那就只有 pytest 这一头在守")
