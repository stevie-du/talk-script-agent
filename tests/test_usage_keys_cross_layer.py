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
  2. 界面读的 **details 字典**必须能活着走过 `app/llm.py` 的 usage 合并 ——
     P2-20 之前是「按名字点名才拷」的白名单（`llm.py:479` / `:584`），
     现在是 `_merge_usage` 对 dict 值**递归累加**（不点名、全量保）。
     点名时代漏一个名字、递归时代丢了 dict 分支，症状一模一样：
     界面永远显示「正文 = 全部 completion」，把思考开销完全藏起来
     —— 而"思考吃掉多少预算"正是这条功能存在的全部理由（主报告 R1）；
  3. `reasoning_tokens` 这个名字界面在用，而它由上游原样带回来（引擎不重命名），
     所以它**不许**出现在 `app/` 里被我们自己改写 —— 出现即说明有人加了第二本账。
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


def test_details_dict_the_ui_reads_survive_the_merge():
    """界面点的嵌套字典，必须能活着走过 llm.py 的 usage 合并。

    旧守卫盯的是「点名白名单」（`for det in (...)`）；P2-20 把两处内联白名单
    重构成共享的 `_merge_usage`（dict 值递归累加、不再需要点名）后，那份正则
    永远零命中 —— 守卫自己先假红了。这里改成钉新机制的三个不变式：
    dict 分支还在（details 靠它全量通过）、是累加不是覆盖（重试的账不被抹）、
    两条 chat 路径都接了合并（漏一条就是"只有某条链路有账"）。
    """
    js = PROGRESS_JS.read_text(encoding="utf-8")
    llm = LLM.read_text(encoding="utf-8")
    # UI 里所有 `xxx_details` 形态的读取
    ui_details = set(re.findall(r"\b([a-z_]+_details)\b", js))
    assert ui_details, "progress.js 没读任何 details 字典 —— 思考 token 从哪来？"
    m = re.search(r"def _merge_usage\(target: dict, u: dict\).*?(?=\ndef |\Z)", llm, re.S)
    assert m, "llm.py 找不到 _merge_usage —— usage 合并被改没了？"
    body = m.group(0)
    # dict 值递归拷贝：details 键走的就是这条路（不点名、全量保）。
    # 删掉这个分支，界面的 details 会无声变空（不报错，只是账是假的）。
    assert re.search(r"isinstance\(v, dict\)", body) and "setdefault" in body, (
        "_merge_usage 不再递归拷贝 dict 值：界面读的 "
        f"{sorted(ui_details)} 会无声变空（不报错，只是账是假的）")
    # 累加而不是覆盖（P2-20 的本意）：覆盖会让重试后的账只剩最后一次。
    assert "cur + v" in body, "_merge_usage 退化成覆盖：重试后显示的是最后一次的用量"
    # 两条 chat 路径（非流式 / 流式）都要接上合并，漏一条就是"只有某条链路有账"。
    sites = re.findall(r"_merge_usage\(usage, u\)", llm)
    assert len(sites) >= 2, (
        f"llm.py 里 _merge_usage(usage, u) 只出现 {len(sites)} 处，非流式与流式应当各有一处")


def test_reasoning_token_name_is_not_recounted_in_engine():
    """`reasoning_tokens` 由上游原样带回，引擎不许自己再算一份。"""
    app_src = "\n".join(p.read_text(encoding="utf-8")
                        for p in sorted((ROOT / "app").glob("*.py")))
    assert "reasoning_tokens" not in app_src, (
        "app/ 里出现了 reasoning_tokens 字面量：引擎开始自己造第二本账了。"
        "界面的那个数字应当直接来自上游回传的 completion_tokens_details")


def test_cache_fields_the_ui_reads_are_passthrough_upstream_names():
    """P1-40：前端读的前缀缓存字段，必须是上游原样带回的名字，不许引擎改名。

    `$feedback_block` 挪到 user_template 末尾后，回炉轮里唯一变的就是末尾那段，
    理论上前缀缓存会命中整段静态提示词 —— 但 `prompt_cache_hit_tokens` 若不显示，
    重排是否真省钱**观测不到**（P1-40 的"重排了没验证"）。

    断言三层：
      1. 前端真的读了这两个缓存字段（`fmtUsage` 里有）—— 没读 = 白收；
      2. 引擎不重命名：这两个字面量**不出现在 app/ 源码**里（由上游原样带回，
         和 `reasoning_tokens` 同一条命）；
      3. 引擎的 usage 数值拷贝是"全量拷 int/float"（`isinstance(v,(int,float))`）
         —— 缓存字段是顶层数值，靠这条路径天然进作业，不需要点名白名单。
         若哪天改成"点名才拷"，这条会红，逼作者把缓存键也加进白名单。
    """
    js = PROGRESS_JS.read_text(encoding="utf-8")
    for key in ("prompt_cache_hit_tokens", "prompt_cache_miss_tokens"):
        assert key in js, f"progress.js 不再读 {key} —— 缓存命中/未命中不可见了"
    app_src = "\n".join(p.read_text(encoding="utf-8")
                        for p in sorted((ROOT / "app").glob("*.py")))
    for key in ("prompt_cache_hit_tokens", "prompt_cache_miss_tokens"):
        assert key not in app_src, (
            f"app/ 里出现了 {key}：引擎开始自己造缓存账本了，"
            "界面的数字应当直接来自上游回传的 usage")
    llm = LLM.read_text(encoding="utf-8")
    assert "isinstance(v, (int, float))" in llm, (
        "llm.py 的 usage 拷贝不再是'全量拷 int/float'："
        "顶层数值键（含缓存字段）可能漏收，改回全量拷或把缓存键点名加进白名单")


if __name__ == "__main__":                       # pragma: no cover
    sys.exit(pytest.main([__file__, "-q"]))
