# -*- coding: utf-8 -*-
"""Pydantic 数据模型：请求 / 节点产物 / 校验报告 / 最终输出

边界校验的意义（修复前完全没有）：
  - `duration` 无上界 → 实测传 100000 也能受理，配额被线性外推到 14076 字；
    传 0 甚至算出**负配额**（total=-4）。
  - `topic` 无上界 → 20 万字符照样进提示词，token 成本由客户端决定。
  - `facts` 无上界 → 同上，且它是直接拼进 user prompt 的。
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

# 时长上下限：低于下限配额会退化成 0，高于上限没有实际业务意义（口播短视频）
DURATION_MIN = 5.0
DURATION_MAX = 600.0
TOPIC_MAX = 200
FACTS_MAX = 20000


class GenerateRequest(BaseModel):
    pack: str = Field(default="elevator", max_length=64)
    topic: str = Field(min_length=2, max_length=TOPIC_MAX, description="主题")
    segment: str | None = Field(default=None, max_length=64)
    audience: str | None = Field(default=None, max_length=64)
    duration: float | None = Field(default=None, ge=DURATION_MIN, le=DURATION_MAX)
    style: str | None = Field(default=None, max_length=64)
    platform: str | None = Field(default=None, max_length=32)
    persona: str | None = Field(default=None, max_length=64)
    cta: str | None = Field(default=None, max_length=32)
    facts: str | None = Field(default=None, max_length=FACTS_MAX)  # 产品手册/数据/案例
    mode: Literal["auto", "step"] = "auto"      # auto=一键直通 / step=分步确认
    rate: float | None = Field(default=None, gt=0, le=20)          # 覆盖语速
    voice: Literal["strong", "standard", "off"] = "strong"         # 人味档位
    format: Literal["both", "voice"] = "both"   # 输出内容：both=口播+分镜 / voice=仅口播
    reroll: bool = False                        # 「换一版」：同参数重掷，略提温度


class ConfirmRequest(BaseModel):
    plan: dict                        # 编辑后的选题卡


class RewriteSegmentRequest(BaseModel):
    index: int = Field(ge=0, le=64)
    feedback: str | None = Field(default=None, max_length=2000)


class PackCreateRequest(BaseModel):
    industry: str = Field(min_length=2, max_length=40, description="行业名，如：全屋定制/装修")
    description: str = Field(min_length=4, max_length=500,
                             description="一句话业务描述，如：全屋定制家居品牌，面向新房装修业主获客")


# ── 节点产物 ────────────────────────────────────────────────

class TopicPlan(BaseModel):
    angle: str                        # 一句话角度
    hook_type: str                    # 钩子类型（来自钩子库 10 类）
    hook_line: str                    # 钩子句
    points: list[str]                 # 要点（1~4 条，每条一句）
    cta: str                          # 结尾引导


class ScriptSection(BaseModel):
    type: Literal["hook", "point", "cta"]
    text: str
    subtitle: str = ""                # 字幕关键词 ≤12 字


class StoryboardShot(BaseModel):
    time: str = ""
    shot: str = ""                    # 画面/景别
    subtitle: str = ""
    sfx: str = ""
    note: str = ""
    voiceover: str = ""               # 缺省由组装器按段落填充


# ── 场景序列契约（Scene[]）─────────────────────────────────
# 统一产物模型：口播 = 旁白投影 · 分镜 = 表格投影 · 视频 = 连续投影
# 约定见 docs/场景序列契约.md

class SceneVisual(BaseModel):
    prompt: str = ""                  # 画面描述 → 素材生成提示词 / 素材库检索 key
    source: Literal["generated", "stock", "user"] = "generated"
    transition: str = "cut"           # cut / dissolve / fade


class SceneAudio(BaseModel):
    bgm: str = ""                     # 空 = 沿用上一镜
    sfx: str = ""


class SceneItem(BaseModel):
    scene_id: str
    type: str = "point"               # hook / point / cta
    start: float = 0.0                # 秒（语速+字数推算）
    end: float = 0.0
    narration: str = ""               # 口播旁白全文 → TTS / 配音
    subtitle: str = ""                # 字幕关键词 ≤12 字
    visual: SceneVisual = Field(default_factory=SceneVisual)
    audio: SceneAudio = Field(default_factory=SceneAudio)
    shot_type: str = ""               # closeup / medium / wide / detail
    style: str = ""                   # 视觉风格标签


class ScriptResult(BaseModel):
    """最终产物。修复前这个模型定义了却从未被实例化 —— `_finalize` 手搓 dict，
    文档里的「场景序列契约」没有任何代码强制，字段漂移无人发现。
    现在在落盘前用 `model_validate` 卡一道。"""
    id: str
    created_at: str
    pack: str
    pack_draft: bool = False
    params: dict
    quota: dict                       # {total, hook, body, cta}
    # True = 这个 quota **不是**查 quota_table 得来的，而是按「时长×语速×0.95」
    # 估的通用值（行业包没配 quota_table）。必须随产物落盘：
    # 否则 `quota: {total: 256, ...}` 与包作者真正配过的配额长得一模一样，
    # 用户与包作者都看不出这份配额没为本行业定制过。
    quota_degraded: bool = False
    plan: TopicPlan
    sections: list[ScriptSection]
    storyboard: list[StoryboardShot]
    scenes: list[SceneItem] = []      # 场景序列契约（v1：与旧字段并行输出）
    check: dict                       # checker 报告
    placeholders: list[str] = []
    revisions: list[dict] = []        # 回炉/重写记录
    timings: list[dict] = []          # 每段 [start, end] 秒
    logs: list[dict] = []


class PackInfo(BaseModel):
    name: str
    display_name: str
    draft: bool = False
    description: str = ""
    version: int = 1
    params: dict = {}
    # {参数键: {选项值: 降级说明}}，只列「用户能选、但本包没给对应定制」的值。
    # 这些值不会报错，只会静默走通用默认 —— 摊到界面上，避免用户以为在定制。
    param_audit: dict = {}
    # 非空 = 这个包的 pack.yaml 或它引用的 banwords.yaml 读不出来（人话说明，
    # 含「第 N 行第 M 列」）。此时 params / display_name 全是降级值，
    # **不能拿它生成**（`Pack` 会抛 PackBrokenError）。列表里仍要显示这个包，
    # 但要标出来 —— 让它静默消失或静默降级都是更差的处理。
    pack_error: str = ""
