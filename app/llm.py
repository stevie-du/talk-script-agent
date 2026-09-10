# -*- coding: utf-8 -*-
"""LLM 客户端：OpenAI 兼容接口 + JSON 结构化输出 + pydantic 校验重试

两层重试：
  1. 请求层（本文件）：网络错误 / 超时 / 429 / 5xx 自动指数退避重试，次数与超时可配置
  2. 解析层（chat_json）：模型返回的 JSON 不合法时，带错误信息重试一次

支持 mock 模式（TALKSCRIPT_MOCK=1 或 config llm.mock: true），
无 API Key 也能跑通全流程（返回固定夹具，用于开发与验收）。
"""
from __future__ import annotations

import json
import random
import re
import time

import httpx
from pydantic import BaseModel, ValidationError

from .config import LLMConfig
from . import mock_fixtures

# 这些状态码视为瞬时故障，值得重试；401/403/400 等重试无意义
RETRYABLE_STATUS = {429, 500, 502, 503, 504}

_JSON_FENCE = re.compile(r"```(?:json)?\s*(.+?)\s*```", re.S)


class LLMError(RuntimeError):
    pass


class LLMClient:
    def __init__(self, cfg: LLMConfig, mock: bool = False, on_retry=None):
        self.cfg = cfg
        self.mock = mock or bool(cfg.api_key == "MOCK")
        self.on_retry = on_retry          # on_retry(note, attempt, total) —— 供流水线记录日志

    # ── 公开入口 ────────────────────────────────────────────
    def chat_json(self, task: str, system: str, user: str, model_cls: type[BaseModel],
                  max_retries: int = 1, on_retry=None, temperature: float | None = None) -> BaseModel:
        """请求 JSON 输出并校验为 model_cls；校验失败带错误信息重试一次。

        temperature 传入时覆盖本次调用的默认温度（「换一版」用它换取不同表达）。
        """
        if self.mock:
            data = mock_fixtures.response_for(task, user)
            return model_cls.model_validate(data)

        on_retry = on_retry or self.on_retry
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        last_err: Exception | None = None
        for attempt in range(max_retries + 1):
            content = self._complete(messages, on_retry=on_retry, temperature=temperature)
            try:
                return model_cls.model_validate(_extract_json(content))
            except (ValueError, ValidationError) as e:
                last_err = e
                if on_retry:
                    on_retry("JSON 结构不符合要求", attempt + 1, max_retries + 1)
                messages.append({"role": "assistant", "content": content})
                messages.append({"role": "user", "content":
                                 f"你的输出不是合法的目标 JSON：{str(e)[:500]}。"
                                 f"只输出一个符合给定字段结构的 JSON 对象，不要多余文字。"})
        raise LLMError(f"模型输出无法解析为 {model_cls.__name__}: {last_err}")

    def ping(self) -> tuple[bool, str]:
        """最小连通性测试：不约束输出格式，返回 (是否连通, 说明)。

        用于设置页「测试连接」——只验证 Key / 地址 / 模型名是否可用，
        不消耗有意义的 token，也不要求模型支持 JSON 输出模式。
        """
        if self.mock:
            return True, "mock 模式（未实际请求模型）"
        try:
            content = self._complete([{"role": "user", "content": "ping"}],
                                     json_mode=False, max_tokens=16)
        except Exception as e:  # noqa: BLE001
            return False, str(e)
        return True, (content or "").strip()[:80]

    # ── 底层调用（带网络重试）────────────────────────────────
    def _complete(self, messages: list[dict], on_retry=None,
                  json_mode: bool = True, max_tokens: int | None = None,
                  temperature: float | None = None) -> str:
        url = f"{self.cfg.base_url}/chat/completions"
        payload = {
            "model": self.cfg.model,
            "messages": messages,
            "temperature": self.cfg.temperature if temperature is None else temperature,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        if max_tokens:
            payload["max_tokens"] = max_tokens
        headers = {"Authorization": f"Bearer {self.cfg.api_key}"}
        attempts = max(1, int(self.cfg.retries) + 1)
        last_err: Exception | None = None

        for attempt in range(attempts):
            try:
                resp = httpx.post(url, json=payload, headers=headers, timeout=self.cfg.timeout)
            except httpx.RequestError as e:          # 超时 / 连接失败 / 网络中断
                last_err = e
                if attempt + 1 < attempts:
                    self._notify(on_retry, f"网络异常（{type(e).__name__}）", attempt + 1, attempts)
                    time.sleep(self._backoff(attempt))
                    continue
                raise LLMError(f"模型接口连接失败（已重试 {attempts} 次）：{e}") from e

            if resp.status_code in RETRYABLE_STATUS and attempt + 1 < attempts:
                self._notify(on_retry, f"接口返回 {resp.status_code}", attempt + 1, attempts)
                time.sleep(self._backoff(attempt))
                continue
            if resp.status_code >= 400:
                raise LLMError(f"模型接口返回 {resp.status_code}: {resp.text[:300]}")

            try:
                data = resp.json()
                return data["choices"][0]["message"]["content"]
            except (KeyError, IndexError, ValueError) as e:
                raise LLMError(f"模型接口返回结构异常: {str(resp.text)[:300]}") from e

        raise LLMError(f"模型接口连续失败（已重试 {attempts} 次）：{last_err}")

    @staticmethod
    def _backoff(attempt: int) -> float:
        """指数退避：1s、2s、4s…封顶 8s，加少量随机抖动防雪崩"""
        return min(2 ** attempt, 8) + random.uniform(0, 0.4)

    @staticmethod
    def _notify(on_retry, note: str, attempt: int, total: int):
        if on_retry:
            try:
                on_retry(note, attempt, total)
            except Exception:
                pass


def _extract_json(content: str) -> dict:
    """解析模型返回：容忍 ```json 围栏与前后杂文字"""
    content = content.strip()
    fence = _JSON_FENCE.search(content)
    if fence:
        content = fence.group(1)
    else:
        start, end = content.find("{"), content.rfind("}")
        if start >= 0 and end > start:
            content = content[start:end + 1]
    return json.loads(content)
