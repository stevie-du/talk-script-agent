# -*- coding: utf-8 -*-
"""配置加载：config.yaml + 环境变量覆盖。

环境变量覆盖（修复前只支持 api_key / base_url / model 三项，而打包版恰恰最需要
用环境变量而不是明文文件来配 retries / timeout / max_tokens）：

    TALKSCRIPT_API_KEY      模型 Key（避免落盘）
    TALKSCRIPT_BASE_URL     OpenAI 兼容接口地址
    TALKSCRIPT_MODEL        模型名
    TALKSCRIPT_TEMPERATURE  采样温度
    TALKSCRIPT_RETRIES      请求层重试次数
    TALKSCRIPT_TIMEOUT      单次请求超时（秒）
    TALKSCRIPT_MAX_TOKENS   单次输出预算
    TALKSCRIPT_MOCK=1       跑夹具，不调模型
    TALKSCRIPT_DEFAULT_PACK 默认行业包
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .fileio import write_atomic

DEFAULT_CONFIG = {
    "llm": {
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "api_key": "",
        "model": "glm-4.7",
        "temperature": 0.7,
        "retries": 2,
        "timeout": 180,
        "max_tokens": 16000,
    },
    "default_pack": "elevator",
}


@dataclass
class LLMConfig:
    base_url: str = DEFAULT_CONFIG["llm"]["base_url"]
    api_key: str = ""
    model: str = DEFAULT_CONFIG["llm"]["model"]
    temperature: float = 0.7
    retries: int = 2          # 请求失败（网络/超时/429/5xx）自动重试次数
    timeout: float = 180      # 单次请求超时（秒）
    # 单次请求的输出预算。必须显式给足：推理型模型（deepseek 系等）的「思考」
    # token 也计入这个预算，服务端默认值容易被思考吃光，导致 content 返回空串
    # （HTTP 仍是 200），表现为「模型输出无法解析为 ScriptDraft」。
    max_tokens: int = DEFAULT_CONFIG["llm"]["max_tokens"]


@dataclass
class AppConfig:
    llm: LLMConfig = field(default_factory=LLMConfig)
    default_pack: str = "elevator"
    root: Path = Path(".")
    mock: bool = False


def config_path(root: Path, config_dir: Path | None = None) -> Path:
    """config.yaml 的位置。

    打包后 root 是安装目录（Program Files），通常**不可写**；因此允许把配置
    落到用户数据目录（Electron 传 `--data-dir app.getPath('userData')`）。
    开发态不传就是项目根，与原来一致。
    """
    return (config_dir or root) / "config.yaml"


# 首次运行时写入的配置模板。
# 安装包**不携带任何 config**（连模板都不带）—— 用户自己配置，可以走设置界面，
# 也可以直接改这个文件。写一份带注释的模板在这里，是为了让「配置在哪、有哪些项」
# 有据可查，而不是靠猜。
CONFIG_TEMPLATE = """# TalkScript 配置
#
# 这个文件由程序在首次运行时生成，位置：<数据目录>/config.yaml
#   · Windows 打包版：%APPDATA%\\TalkScript\\
#   · 开发态：项目根目录
#
# 三种配置方式，优先级从高到低：
#   1. 环境变量（推荐，密钥不落盘）
#   2. 本文件
#   3. 内置默认值
#
# 环境变量一览：
#   TALKSCRIPT_API_KEY       模型 Key
#   TALKSCRIPT_BASE_URL      OpenAI 兼容接口地址
#   TALKSCRIPT_MODEL         模型名
#   TALKSCRIPT_TEMPERATURE   采样温度
#   TALKSCRIPT_RETRIES       请求失败自动重试次数
#   TALKSCRIPT_TIMEOUT       单次请求超时（秒）
#   TALKSCRIPT_MAX_TOKENS    单次输出预算
#   TALKSCRIPT_DEFAULT_PACK  默认行业包
#   TALKSCRIPT_MOCK=1        跑夹具、不调模型（无 Key 时可用）
#
# 也可以在应用内「设置 → 模型接口」里填写，效果相同。

llm:
  # 任意 OpenAI 兼容接口的根地址（不含 /chat/completions）
  base_url: https://open.bigmodel.cn/api/paas/v4
  # 在这里填你的 Key；留空则必须用环境变量 TALKSCRIPT_API_KEY
  api_key: ""
  model: glm-4.7
  temperature: 0.7
  # 请求失败（网络/超时/429/5xx）自动重试次数；0 = 不重试
  retries: 2
  # 单次请求超时（秒）；长输出模型可调大
  timeout: 180
  # 单次输出预算。推理型模型的「思考」token 也计入这里，给太小会导致
  # content 返回空串。遇到「模型返回了空内容」或 JSON 解析失败，优先调大这一项。
  max_tokens: 16000

default_pack: elevator
"""


def ensure_config_template(root: Path, config_dir: Path | None = None) -> Path | None:
    """首次运行时写一份带注释的配置模板；已存在则不动。

    刻意**不预填任何 Key**：安装包不带配置，用户自己配置。
    返回新建的文件路径；已存在或写失败时返回 None。
    """
    p = config_path(root, config_dir)
    if p.exists():
        return None
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(CONFIG_TEMPLATE, encoding="utf-8")
        return p
    except OSError:
        # 目录不可写不该让引擎起不来：配置本来就可以只走环境变量。
        return None


def _env(name: str, fallback: str = "") -> str:
    v = os.environ.get(name)
    return fallback if v is None or v == "" else v


def _env_num(name: str, fallback, cast):
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return fallback
    try:
        return cast(raw)
    except (TypeError, ValueError):
        return fallback


def _truthy(v) -> bool:
    return str(v or "").strip().lower() in ("1", "true", "yes", "on")


def load_config(root: Path, config_dir: Path | None = None) -> AppConfig:
    data: dict = {}
    p = config_path(root, config_dir)
    if p.exists():
        try:
            with open(p, encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            if not isinstance(data, dict):
                data = {}
        except (OSError, yaml.YAMLError):
            # 配置读坏不该让引擎起不来：退回默认值，界面照常能打开，
            # 用户在设置里重新保存一次即可。修复前这里会直接抛异常，
            # 表现为「无法连接本地引擎」，用户完全无从下手。
            data = {}

    llm = data.get("llm", {}) or {}
    if not isinstance(llm, dict):
        llm = {}

    cfg = LLMConfig(
        base_url=str(llm.get("base_url") or DEFAULT_CONFIG["llm"]["base_url"]).rstrip("/"),
        api_key=str(llm.get("api_key") or ""),
        model=str(llm.get("model") or DEFAULT_CONFIG["llm"]["model"]),
        temperature=float(llm.get("temperature", 0.7) or 0.7),
        retries=int(llm.get("retries", 2) or 0),
        timeout=float(llm.get("timeout", 180) or 180),
        max_tokens=int(llm.get("max_tokens") or DEFAULT_CONFIG["llm"]["max_tokens"]),
    )

    # 环境变量覆盖（避免密钥落盘）
    cfg.api_key = _env("TALKSCRIPT_API_KEY", cfg.api_key)
    cfg.base_url = _env("TALKSCRIPT_BASE_URL", cfg.base_url).rstrip("/")
    cfg.model = _env("TALKSCRIPT_MODEL", cfg.model)
    cfg.temperature = _env_num("TALKSCRIPT_TEMPERATURE", cfg.temperature, float)
    cfg.retries = _env_num("TALKSCRIPT_RETRIES", cfg.retries, int)
    cfg.timeout = _env_num("TALKSCRIPT_TIMEOUT", cfg.timeout, float)
    cfg.max_tokens = _env_num("TALKSCRIPT_MAX_TOKENS", cfg.max_tokens, int)

    app = AppConfig(
        llm=cfg,
        default_pack=_env("TALKSCRIPT_DEFAULT_PACK", str(data.get("default_pack", "elevator"))),
        root=root,
    )
    app.mock = (_truthy(os.environ.get("TALKSCRIPT_MOCK"))
                or _truthy(llm.get("mock"))
                or cfg.api_key == "MOCK")
    return app


def save_config(root: Path, llm: dict, default_pack: str | None = None,
                config_dir: Path | None = None) -> None:
    """合并保存：只覆盖传入的字段；api_key 传空串表示保持不变；
    未传入的字段（如 retries/timeout/mock）原样保留。"""
    data: dict = {}
    p = config_path(root, config_dir)
    if p.exists():
        try:
            with open(p, encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            if not isinstance(data, dict):
                data = {}
        except (OSError, yaml.YAMLError):
            data = {}
    existing = data.get("llm", {}) or {}
    if not isinstance(existing, dict):
        existing = {}
    for k, v in llm.items():
        if k == "api_key" and not v:
            continue
        if v is not None:
            existing[k] = v
    data["llm"] = existing
    if default_pack:
        data["default_pack"] = default_pack
    # 原子写：这里尤其要紧 —— config.yaml 被写坏就是半截 YAML，后果不是丢一条
    # 记录，而是每次 load_config 都炸、连界面都出不来。
    write_atomic(p, yaml.safe_dump(data, allow_unicode=True, sort_keys=False))
