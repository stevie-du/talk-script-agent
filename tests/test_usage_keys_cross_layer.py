# -*- coding: utf-8 -*-
"""usage 的键名前后端必须是同一份（P3-15 的第二条腿）。

引擎把上游回的 token 量记进作业步骤（`app/pipeline.py` 四处 `_step`），界面在
步骤行上把它翻成「思考 N token · 正文 M token」（`desktop/renderer/js/progress.js`
的 `fmtUsage`）。两种语言没法共享代码，于是同一批键名被写了两遍 —— 而**抄键名
正是本项目反复翻车的地方**：`tests/test_job_state_vocabulary_consistency.py` 的
前言记录了状态词表走散一次的后果，第 9 轮又扫出「UI 桩手抄中文字面量、改文案静默
失效」同一族。

这里不靠"记得改两处"，而是读三个源文件对账：

  1. 界面读的**步骤字段名**必须是引擎真的写进 `_step` data 里的那一个（`usage`）；
  2. 界面读的 **details 字典名**必须在 `app/llm.py` 的白名单里 —— `usage` 的数值
     键是 `isinstance(v, (int, float))` 全量拷的，但嵌套的 details 是**按名字点名**
     才拷的（`llm.py:479` / `:584`），漏点名就等于界面永远读到空；
  3. `reasoning_tokens` 这个名字界面在用，而它由上游原样带回来（引擎不重命名），
     所以它**不许**出现在 `app/` 里被我们自己改写 —— 出现即说明有人加了第二本账。

第 2 条是真会坏的那种：改一行白名单，界面不会报错，只会永远显示「正文 = 全部
completion」，把思考开销完全藏起来 —— 而"思考吃掉多少预算"正是这条功能存在的全部
理由（主报告 R1）。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PIPELINE = ROOT / "app" / "pipeline.py"
LLM = ROOT / "app" / "llm.py"
PROGRESS_JS = ROOT / "desktop" / "renderer" / "js" / "progress.js"

# 调模型的阶段（纯代码校验那一步没有 usage，界面也就不该给它印徽章）。
# draft：方案 10 合并阶段（select+write 一次调用的包）；select/write_r{rnd}：
# 老包（无 stages.draft）仍走的两段路径。两条路径都必须记 usage。
MODEL_STAGES = {"select", "write_r{rnd}", "draft", "storyboard", "rewrite_segment"}

_STEP_HEAD = re.compile(r"self\._step\(\s*job,\s*f?[\"'](?P<key>[^\"']+)[\"']")


def _step_keys_recording_usage(src: str) -> set[str]:
    """每个 `"usage"` 出现处，回溯它属于哪一条 `_step` 调用的步骤 key。"""
    keys: set[str] = set()
    for m in re.finditer(r"\"usage\"", src):
        call = src.rfind("self._step(", 0, m.start())
        if call == -1:
            continue
        head = _STEP_HEAD.match(src[call:])
        if head:
            keys.add(head.group("key"))
    return keys


def test_ui_reads_a_step_field_the_engine_writes():
    """界面读的 `data.usage` 必须就是引擎写进 `_step` 的那个字段名。"""
    pipe = PIPELINE.read_text(encoding="utf-8")
    js = PROGRESS_JS.read_text(encoding="utf-8")
    written = _step_keys_recording_usage(pipe)
    assert MODEL_STAGES <= written, (
        f"这四个阶段才调模型，usage 要记全：缺 {sorted(MODEL_STAGES - written)}")

    # 界面从步骤 data 上读了哪些键（`cur.data?.usage` / `cur.data?.note` …）——
    # ⚠ 不能用 re.search 取第一个：note 那行在读 usage 那行**之前**，
    #   取第一个会把这条守卫变成在量 note（第 18 轮那种"判据问自己的副本"）。
    ui_fields = set(re.findall(r"data\?\.(\w+)", js))
    assert "usage" in ui_fields, (
        f"界面不再从步骤 data 读 usage 了（现在读的是 {sorted(ui_fields)}）："
        "要么这条功能被悄悄移走了，要么键名走散了")
    for field in sorted(ui_fields):
        assert re.search(r"\"%s\"" % field, pipe), (
            f"界面读 data.{field}，而引擎没有一个 _step 的 data 写这个键")


def test_details_dict_the_ui_reads_is_on_the_llm_copy_list():
    """界面点的嵌套字典名，必须在 llm.py 点名拷贝的白名单里。"""
    js = PROGRESS_JS.read_text(encoding="utf-8")
    llm = LLM.read_text(encoding="utf-8")
    # UI 里所有 `xxx_details` 形态的读取
    ui_details = set(re.findall(r"\b([a-z_]+_details)\b", js))
    assert ui_details, "progress.js 没读任何 details 字典 —— 思考 token 从哪来？"
    # ⚠ 这里必须是 **list** 而不是 set：两条 chat 路径的白名单写的是同一串字面量，
    #   收成 set 就永远只剩一个，"两条路径都要点名"这条断言会退化成恒真。
    listed = re.findall(r"for det in \(([^)]*)\)", llm)
    copied: set[str] = set()
    for group in listed:
        copied |= set(re.findall(r"[\"']([a-z_]+)[\"']", group))
    assert copied, "llm.py 里找不到 details 的点名拷贝 —— 白名单被改掉了？"
    missing = ui_details - copied
    assert not missing, (
        f"界面读 {sorted(missing)}，但 llm.py 只点名拷 {sorted(copied)}："
        "漏点名的键上游回了也不会进作业，界面会永远读到空（不报错，只是账是假的）")
    # 两条 chat 路径（非流式 / 流式）都要点名，漏一条就是"只有某条链路有账"
    assert len(listed) >= 2, (
        f"llm.py 里 details 白名单只出现 {len(listed)} 处，非流式与流式应当各有一处")


def test_reasoning_token_name_is_not_recounted_in_engine():
    """`reasoning_tokens` 由上游原样带回，引擎不许自己再算一份。"""
    app_src = "\n".join(p.read_text(encoding="utf-8")
                        for p in sorted((ROOT / "app").glob("*.py")))
    assert "reasoning_tokens" not in app_src, (
        "app/ 里出现了 reasoning_tokens 字面量：引擎开始自己造第二本账了。"
        "界面的那个数字应当直接来自上游回传的 completion_tokens_details")


if __name__ == "__main__":                       # pragma: no cover
    sys.exit(pytest.main([__file__, "-q"]))
