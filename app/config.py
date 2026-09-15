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

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .fileio import read_yaml_file, write_atomic

log = logging.getLogger(__name__)

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
    # config.yaml 读坏时的一句话说明（读坏才非空）。随 /api/config 下发，
    # 让设置页能提示「你的配置没生效」—— 否则退回全默认这件事完全不可见。
    config_error: str = ""


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


def _num(data: dict, key: str, default, cast):
    """取一个数值配置项：**只有键缺失或为空时才用默认值**。

    为什么不能写成 `data.get(key) or default` —— 那会把合法的 0 当成「没填」。
    `temperature` 的合法区间包含 0.0（见 `server.set_config` 的 0.0 ~ 2.0），
    于是「把温度调到 0 求确定性输出」会变成静默无效：设置页提示保存成功，
    写进 config.yaml 的也是 0.0，但每次生成实际仍用默认的 0.7 ——
    不报错、不可见、与用户意图相反，正是本项目一直在整治的「静默降级」。

    同类写法在 timeout / max_tokens 上也有，当前只是靠接口层的区间校验挡着
    才没出事（0 不在它们的合法区间里），但 `TALKSCRIPT_TIMEOUT=0` 这类
    环境变量路径绕得过去，所以一并收口到这里。

    值非法（如 `temperature: 快`）时退回默认并留一条日志 —— 不回退会让
    `float("快")` 的 ValueError 冒到 `create_app`，引擎直接起不来。
    """
    v = data.get(key)
    if v is None or v == "":
        return default
    try:
        return cast(v)
    except (TypeError, ValueError):
        log.warning("配置项 %s 的值 %r 不是合法的 %s，已改用默认值 %r",
                    key, v, cast.__name__, default)
        return default


def read_config_file(p: Path) -> tuple[dict, str]:
    """读 config.yaml，返回 `(数据, 错误说明)`；错误说明为空串表示读成功。

    抽出来是因为 `load_config` 与 `save_config` 各自抄了一份同样的
    try/except（**且两处都静默**），修的时候很容易只修一处。

    真正的读取在 `fileio.read_yaml_file` —— 那是**全项目读 YAML 的唯一口径**，
    行业包（pack / banwords / skill / private）走的是同一个函数。
    这里只补一条 WARNING：config 读坏会退回内置默认值，而默认值里的
    base_url / model 是能跑通的，用户很容易以为「我的配置生效了」。
    """
    data, err = read_yaml_file(p)
    if err:
        log.warning("config.yaml 读取失败（%s），本次改用内置默认值：%s", p, err)
    return data, err


def load_config(root: Path, config_dir: Path | None = None) -> AppConfig:
    p = config_path(root, config_dir)
    data, config_error = read_config_file(p)

    llm = data.get("llm", {}) or {}
    if not isinstance(llm, dict):
        llm = {}

    cfg = LLMConfig(
        base_url=str(llm.get("base_url") or DEFAULT_CONFIG["llm"]["base_url"]).rstrip("/"),
        api_key=str(llm.get("api_key") or ""),
        model=str(llm.get("model") or DEFAULT_CONFIG["llm"]["model"]),
        temperature=_num(llm, "temperature", 0.7, float),
        retries=_num(llm, "retries", 2, int),
        timeout=_num(llm, "timeout", 180.0, float),
        max_tokens=_num(llm, "max_tokens", DEFAULT_CONFIG["llm"]["max_tokens"], int),
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
        config_error=config_error,
    )
    app.mock = (_truthy(os.environ.get("TALKSCRIPT_MOCK"))
                or _truthy(llm.get("mock"))
                or cfg.api_key == "MOCK")
    return app


def save_config(root: Path, llm: dict, default_pack: str | None = None,
                config_dir: Path | None = None) -> None:
    """合并保存：只覆盖传入的字段；api_key 传空串表示保持不变；
    未传入的字段（如 retries/timeout/mock）原样保留。

    ⚠ 文件读坏时这里会把**整份配置重置为只剩本次写入的字段**（旧的
    `default_pack` / `mock` 会丢）。但这是有意为之、且优于另一条路：
    不重置就没法把文件写回合法状态，用户只能去手工修 YAML。
    `read_config_file` 已经为此留了 WARNING，别让它变成静默丢失。
    """
    p = config_path(root, config_dir)
    data, _ = read_config_file(p)
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
