# -*- coding: utf-8 -*-
"""Pydantic 数据模型：请求 / 节点产物 / 校验报告 / 最终输出"""
from __future__ import annotations

from pydantic import BaseModel, Field, field_validator


class GenerateRequest(BaseModel):
    pack: str = "elevator"
    topic: str = Field(min_length=2, description="主题")
    segment: str | None = None
    audience: str | None = None
    duration: float | None = None
    style: str | None = None
    platform: str | None = None
    persona: str | None = None
    cta: str | None = None
    facts: str | None = None          # 用户提供的产品手册/数据/案例
    mode: str = "auto"                # auto=一键直通 / step=分步确认
    rate: float | None = None         # 覆盖语速
    voice: str = "strong"             # 人味档位：strong=加强 / standard=仅去AI腔 / off=关闭
    format: str = "both"              # 输出内容：both=口播+分镜 / voice=仅口播

    @field_validator("voice")
    @classmethod
    def _valid_voice(cls, v: str) -> str:
        if v not in ("strong", "standard", "off"):
            raise ValueError("voice 必须是 strong/standard/off")
        return v

    @field_validator("format")
    @classmethod
    def _valid_format(cls, v: str) -> str:
        if v not in ("both", "voice"):
            raise ValueError("format 必须是 both/voice")
        return v


class ConfirmRequest(BaseModel):
    plan: dict                        # 编辑后的选题卡


class RewriteSegmentRequest(BaseModel):
    index: int
    feedback: str | None = None


class PackCreateRequest(BaseModel):
    industry: str = Field(min_length=2, description="行业名，如：全屋定制/装修")
    description: str = Field(min_length=4, description="一句话业务描述，如：全屋定制家居品牌，面向新房装修业主获客")


# ── 节点产物 ────────────────────────────────────────────────

class TopicPlan(BaseModel):
    angle: str                        # 一句话角度
    hook_type: str                    # 钩子类型（来自钩子库 10 类）
    hook_line: str                    # 钩子句
    points: list[str]                 # 要点（1~4 条，每条一句）
    cta: str                          # 结尾引导


class ScriptSection(BaseModel):
    type: str                         # hook / point / cta
    text: str
    subtitle: str = ""                # 字幕关键词 ≤12 字

    @field_validator("type")
    @classmethod
    def _valid_type(cls, v: str) -> str:
        if v not in ("hook", "point", "cta"):
            raise ValueError("type 必须是 hook/point/cta")
        return v


class StoryboardShot(BaseModel):
    time: str = ""
    shot: str = ""                    # 画面/景别
    subtitle: str = ""
    sfx: str = ""
    note: str = ""
    voiceover: str = ""               # 缺省由组装器按段落填充


# ── 场景序列契约（Scene[]）─────────────────────────────────
# 统一产物模型：口播 = 旁白投影 · 分镜 = 表格投影 · 视频 = 连续投影
# 约定见 docs/场景序列契约.md；先与 sections/storyboard/timings 并行输出，逐步迁移

class SceneVisual(BaseModel):
    prompt: str = ""                  # 画面描述 → 素材生成提示词 / 素材库检索 key
    source: str = "generated"         # generated / stock / user
    transition: str = "cut"           # cut / dissolve / fade

    @field_validator("source")
    @classmethod
    def _valid_source(cls, v: str) -> str:
        if v not in ("generated", "stock", "user"):
            raise ValueError("source 必须是 generated/stock/user")
        return v


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
    id: str
    created_at: str
    pack: str
    pack_draft: bool = False
    params: dict
    quota: dict                       # {total, hook, body, cta}
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
