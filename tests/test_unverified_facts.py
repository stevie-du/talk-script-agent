# -*- coding: utf-8 -*-
"""私有资料没有出处时，注入段必须自己说清楚（P0-17 修 ③）。

清空示例数字只解决了一半：**用户填了真数字但没写 `source`/`verified`** 的那条，
原来照样顶着【私有知识库（优先作为事实来源）】的标题进提示词 —— 模型看到的就是
"可信事实"，脚本里就会念成既成事实。所以没有出处的条目要就地打 `_未核实` 标记。

同一份文件里也要有反例：打了标记不代表整份资料都不能用，填了出处的条目不许被连坐。
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.knowledge import UNVERIFIED_KEY, Pack, strip_empty
from app.prompts import PromptRenderer  # noqa: E402

PRODUCTS = """
products:
  - name: 曳引式家用电梯
    rated_load: 1050
    source: ""
    verified: ""
  - name: 液压式家用电梯
    rated_load: 800
    source: 2026 版产品手册第 12 页
    verified: "2026-09-01"
"""


def _pack_with_private(tmp: Path, body: str) -> Pack:
    d = tmp / "packs" / "elevator"
    shutil.copytree(ROOT / "packs" / "elevator", d)
    (d / "private" / "products.yaml").write_text(body, encoding="utf-8")
    return Pack(tmp, "elevator")


# ── 单元：标记的加与不加 ────────────────────────────────────
def test_entry_without_provenance_is_marked():
    data = yaml.safe_load(PRODUCTS)
    slim = strip_empty(data)
    first, second = slim["products"]
    assert first[UNVERIFIED_KEY], "没有出处的条目必须带着标记进提示词"
    assert first["rated_load"] == 1050, "打了标记不该把内容一起丢掉"
    assert UNVERIFIED_KEY not in second, "写了出处的条目不许被连坐"
    assert second["source"].startswith("2026")


def test_entry_whose_only_content_is_provenance_disappears():
    """只有空的 source/verified、别的全空 → 整条消失，不凭空多出一条标记。"""
    slim = strip_empty([{"source": "", "verified": ""}])
    assert slim == [], slim


def test_no_marker_when_provenance_keys_absent_entirely():
    """没写这两个键（比如一份纯规则清单）不算"声称有出处却没填"。"""
    slim = strip_empty({"usage_rules": [{"rule": "public 为 false 时不点名"}]})
    assert UNVERIFIED_KEY not in str(slim)


# ── 集成：标记真的到了模型眼前 ───────────────────────────────
def test_marker_reaches_the_rendered_prompt():
    tmp = Path(tempfile.mkdtemp(prefix="unverified-"))
    try:
        pk = _pack_with_private(tmp, PRODUCTS)
        facts = pk.private_facts()
        assert "1050" in facts and UNVERIFIED_KEY in facts
        assert facts.index(UNVERIFIED_KEY) < facts.index("800"), \
            "标记必须贴在没出色的那条上，不是整份文件末尾一句统称"

        a, sg, st, pf = (pk.param_options('audience')[0], pk.param_options('segment')[0],
                         pk.param_options('style')[0], pk.param_options('platform')[0])
        p = {"topic": "x", "segment": sg, "audience": a, "duration": 60, "style": st,
             "platform": pf, "persona": "", "cta": "a", "facts": "", "rate": 4.5,
             "voice": "strong", "format": "both", "points": 3,
             "quota": {"total": 290, "hook": 45, "body": 190, "cta": 55},
             "quota_degraded": False}
        s, u = PromptRenderer(pk).render(
            "write", PromptRenderer(pk).write_ctx(
                p, {"angle": "a", "hook_type": "h", "hook_line": "l",
                    "points": ["1"], "cta": "c"}, ""))
        assert "优先作为事实来源" in u, "注入标题本身要在（这条断言守的是那段还在）"
        assert UNVERIFIED_KEY in u and "{{待补" in u
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_marker_in_the_shipped_pack_is_per_entry_not_a_blanket():
    """仓库里这份包的实况：`products.yaml` 已清空到不注入，
    `service.yaml` 有内容却 `source/verified` 空着 → 只有它被标记。

    这条同时是两头的守卫：
      - 标记**精确到条目**（不是整份文件末尾一句统称，那等于没标）；
      - 将来谁把 service.yaml 的出处补上，标记就该消失 —— 那时这条会红，
        改了断言即可（红得对，不是 flaky）。
    """
    pk = Pack(ROOT, "elevator")
    facts = pk.private_facts()
    files = [ln for ln in facts.splitlines() if ln.startswith("=== ")]
    assert "=== 私有资料 · products ===" not in files, \
        "products.yaml 清空后不该再被注入（空壳条目不该占提示词）"
    marked = []
    cur = None
    for line in facts.splitlines():
        if line.startswith("=== "):
            cur = line
        if UNVERIFIED_KEY in line:
            marked.append(cur)
    # 段名用去扩展名的基名（`=== 私有资料 · service ===`），不再写文件路径：
    # 模型没有文件系统，路径形态的段名是在让它去找不存在的文件。
    assert marked == ["=== 私有资料 · service ==="], marked
    # 没写这两个键的文件（faq / cases 的 usage_rules）不被连坐
    assert "=== 私有资料 · faq ===" not in marked
    assert "=== 私有资料 · cases ===" not in marked
