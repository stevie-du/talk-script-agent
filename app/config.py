# -*- coding: utf-8 -*-
"""配置加载：config.yaml + 环境变量覆盖"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

DEFAULT_CONFIG = {
    "llm": {
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "api_key": "",
        "model": "glm-4.7",
        "temperature": 0.7,
        "retries": 2,
        "timeout": 180,
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


@dataclass
class AppConfig:
    llm: LLMConfig = field(default_factory=LLMConfig)
    default_pack: str = "elevator"
    root: Path = Path(".")
    mock: bool = False


def config_path(root: Path) -> Path:
    return root / "config.yaml"


def load_config(root: Path) -> AppConfig:
    data: dict = {}
    p = config_path(root)
    if p.exists():
        with open(p, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

    llm = data.get("llm", {}) or {}
    cfg = LLMConfig(
        base_url=str(llm.get("base_url") or DEFAULT_CONFIG["llm"]["base_url"]).rstrip("/"),
        api_key=str(llm.get("api_key") or ""),
        model=str(llm.get("model") or DEFAULT_CONFIG["llm"]["model"]),
        temperature=float(llm.get("temperature", 0.7)),
        retries=int(llm.get("retries", 2)),
        timeout=float(llm.get("timeout", 180)),
    )
    # 环境变量覆盖（避免密钥落盘）
    cfg.api_key = os.environ.get("TALKSCRIPT_API_KEY", cfg.api_key)
    cfg.base_url = os.environ.get("TALKSCRIPT_BASE_URL", cfg.base_url).rstrip("/")
    cfg.model = os.environ.get("TALKSCRIPT_MODEL", cfg.model)

    app = AppConfig(llm=cfg, default_pack=str(data.get("default_pack", "elevator")), root=root)
    app.mock = (os.environ.get("TALKSCRIPT_MOCK", "").lower() in ("1", "true", "yes")
                or str((llm.get("mock") or "")).lower() in ("1", "true", "yes")
                or cfg.api_key == "MOCK")
    return app


def save_config(root: Path, llm: dict, default_pack: str | None = None) -> None:
    """合并保存：只覆盖传入的字段；api_key 传空串表示保持不变；
    未传入的字段（如 retries/timeout/mock）原样保留。"""
    data: dict = {}
    p = config_path(root)
    if p.exists():
        with open(p, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    existing = data.get("llm", {}) or {}
    for k, v in llm.items():
        if k == "api_key" and not v:
            continue
        if v is not None:
            existing[k] = v
    data["llm"] = existing
    if default_pack:
        data["default_pack"] = default_pack
    with open(p, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)
