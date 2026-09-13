# -*- coding: utf-8 -*-
"""LLM 客户端：OpenAI 兼容接口 + JSON 结构化输出 + pydantic 校验重试

两层重试：
  1. 请求层（本文件）：网络错误 / 超时 / 429 / 5xx 自动指数退避重试，次数与超时可配置
  2. 解析层（chat_json）：模型返回的 JSON 不合法时，带错误信息重试一次

支持 mock 模式（TALKSCRIPT_MOCK=1 或 config llm.mock: true），
无 API Key 也能跑通全流程（返回固定夹具，用于开发与验收）。

两处修复：
  - **连接复用**：原来每次调用都走 `httpx.post` / `httpx.stream` 顶层函数，
    每次都新建连接（重新 TCP + TLS 握手）。一次生成 2~4 次调用、外加重试，
    这部分开销纯属浪费。现在共用一个线程安全的 `httpx.Client`（连接池）。
  - **流式分支的空内容诊断**：原来 `if on_delta: return self._stream_once(...)`
    直接返回，绕过了后面那段「模型返回空内容 → 请调大 max_tokens」的诊断，
    而 pipeline 全程都传 on_delta —— 于是这条最有用的排障提示在真实使用中
    永远不会出现，用户只会看到误导性的「模型输出无法解析为 ScriptDraft」。
"""
from __future__ import annotations

import json
import random
import re
import threading
import time

import httpx
from pydantic import BaseModel, ValidationError

from .config import LLMConfig
from . import mock_fixtures

# 这些状态码视为瞬时故障，值得重试；401/403/400 等重试无意义
RETRYABLE_STATUS = {429, 500, 502, 503, 504}

_JSON_FENCE = re.compile(r"```(?:json)?\s*(.+?)\s*```", re.S)

# 上游响应体回显长度：修复前是 300 字符原样进 error 字段，并落盘进 job.json、
# 展示在界面上。上游若回显请求上下文，可能把敏感信息一并带出去，收紧到 160。
ERROR_BODY_CHARS = 160

_client_lock = threading.Lock()
_client: httpx.Client | None = None


def get_client() -> httpx.Client:
    """进程内共用的 HTTP 客户端（连接池）。

    `httpx.Client` 是线程安全的。刻意不开 `follow_redirects`：
    跟随跳转会把 `Authorization` 头带到第三方地址上去。
    """
    global _client
    with _client_lock:
        if _client is None or _client.is_closed:
            _client = httpx.Client(
                timeout=httpx.Timeout(180.0, connect=20.0),
                limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
                follow_redirects=False,
                headers={"User-Agent": "TalkScript/0.2"},
            )
        return _client


def close_client() -> None:
    global _client
    with _client_lock:
        if _client is not None:
            try:
                _client.close()
            except Exception:                   # noqa: BLE001
                pass
            _client = None


class LLMError(RuntimeError):
    pass


class EmptyContentError(LLMError):
    """HTTP 200 但 content 为空。单独成类，方便上层识别并给出可操作的提示。"""


class RetryableStatus(RuntimeError):
    """流式分支里遇到的 429/5xx。

    非流式分支能直接看到 status_code 就地重试，流式分支在 with 块里不方便
    连续 continue，于是抛出来交给 _complete 的重试循环统一退避。
    """

    def __init__(self, code: int, text: str):
        super().__init__(str(code))
        self.code = code
        self.text = text


def _brief(text: str) -> str:
    """压缩上游响应体：去换行、截断。"""
    return re.sub(r"\s+", " ", (text or ""))[:ERROR_BODY_CHARS]


class LLMClient:
    def __init__(self, cfg: LLMConfig, mock: bool = False, on_retry=None):
        self.cfg = cfg
        self.mock = mock or bool(cfg.api_key == "MOCK")
        self.on_retry = on_retry          # on_retry(note, attempt, total) —— 供流水线记录日志

    # ── 公开入口 ────────────────────────────────────────────
    def chat_json(self, task: str, system: str, user: str, model_cls: type[BaseModel],
                  max_retries: int = 1, on_retry=None, temperature: float | None = None,
                  on_delta=None) -> BaseModel:
        """请求 JSON 输出并校验为 model_cls；校验失败带错误信息重试一次。

        temperature 传入时覆盖本次调用的默认温度（「换一版」用它换取不同表达）。
        on_delta(kind, text) 传入时改用流式请求，供界面实时显示思考过程；
        返回值仍是拼接好的完整正文，校验与重试逻辑完全不受影响。
        """
        if self.mock:
            data = mock_fixtures.response_for(task, user)
            return model_cls.model_validate(data)

        on_retry = on_retry or self.on_retry
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        last_err: Exception | None = None
        for attempt in range(max_retries + 1):
            content = self._complete(messages, on_retry=on_retry,
                                     temperature=temperature, on_delta=on_delta)
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
                  temperature: float | None = None, on_delta=None) -> str:
        url = f"{self.cfg.base_url}/chat/completions"
        payload = {
            "model": self.cfg.model,
            "messages": messages,
            "temperature": self.cfg.temperature if temperature is None else temperature,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        # 输出预算必须显式给足：推理型模型（deepseek 系等）的「思考」token 也计入
        # 该预算，服务端默认值容易被思考吃光 → content 返回空串（HTTP 仍是 200）。
        payload["max_tokens"] = max_tokens or self.cfg.max_tokens
        headers = {"Authorization": f"Bearer {self.cfg.api_key}"}
        attempts = max(1, int(self.cfg.retries) + 1)
        client = get_client()
        last_err: Exception | None = None

        for attempt in range(attempts):
            try:
                if on_delta:
                    # 流式：边收边把增量交出去；返回值仍是完整正文，调用方无感
                    content = self._stream_once(client, url, payload, headers, on_delta)
                    return self._ensure_content(content, None, streamed=True)
                resp = client.post(url, json=payload, headers=headers, timeout=self.cfg.timeout)
            except RetryableStatus as e:             # 流式分支里的 429/5xx
                last_err = e
                if attempt + 1 < attempts:
                    self._notify(on_retry, f"接口返回 {e.code}", attempt + 1, attempts)
                    time.sleep(self._backoff(attempt))
                    continue
                raise LLMError(f"模型接口返回 {e.code}: {_brief(e.text)}") from e
            except httpx.RequestError as e:          # 超时 / 连接失败 / 网络中断
                last_err = e
                if attempt + 1 < attempts:
                    self._notify(on_retry, f"网络异常（{type(e).__name__}）", attempt + 1, attempts)
                    time.sleep(self._backoff(attempt))
                    continue
                raise LLMError(f"模型接口连接失败（已重试 {attempts} 次）："
                               f"{type(e).__name__}: {_brief(str(e))}") from e

            if resp.status_code in RETRYABLE_STATUS and attempt + 1 < attempts:
                self._notify(on_retry, f"接口返回 {resp.status_code}", attempt + 1, attempts)
                time.sleep(self._backoff(attempt))
                continue
            if resp.status_code >= 400:
                raise LLMError(f"模型接口返回 {resp.status_code}: {_brief(resp.text)}")

            try:
                data = resp.json()
                content = data["choices"][0]["message"]["content"]
            except (KeyError, IndexError, ValueError) as e:
                raise LLMError(f"模型接口返回结构异常: {_brief(resp.text)}") from e
            finish = ((data.get("choices") or [{}])[0] or {}).get("finish_reason")
            return self._ensure_content(content, finish)

        raise LLMError(f"模型接口连续失败（已重试 {attempts} 次）：{_brief(str(last_err))}")

    def _ensure_content(self, content: str, finish, *, streamed: bool = False) -> str:
        """空内容统一在这里报错。

        修复前只有非流式分支做这个检查，而 pipeline 全程走流式 ——
        等于这条诊断永远不触发。
        """
        if (content or "").strip():
            return content
        raise EmptyContentError(
            f"模型返回了空内容（HTTP 200，但 content 为空{'' if finish is None else f'，finish_reason={finish}'}）。"
            "通常原因：输出预算被推理模型的思考 token 用尽。"
            f"请调大 config.yaml 里的 llm.max_tokens（当前 {self.cfg.max_tokens}），"
            "或改用非推理模型。"
            + ("（本次为流式请求：思考内容已收到，但正文为空。）" if streamed else ""))

    def _stream_once(self, client: httpx.Client, url: str, payload: dict,
                     headers: dict, on_delta) -> str:
        """流式请求一次：逐块解析 SSE，把增量交给 on_delta，返回拼接好的正文。

        推理型模型在 delta 里分两条通道推送：reasoning_content（思考过程）与
        content（最终正文）。两者必须分开累计 —— 思考只是给人看的过程，
        混进正文会让 json.loads 直接失败。
        """
        body = {**payload, "stream": True}
        parts: list[str] = []
        with client.stream("POST", url, json=body, headers=headers,
                           timeout=self.cfg.timeout) as resp:
            if resp.status_code in RETRYABLE_STATUS:
                resp.read()
                raise RetryableStatus(resp.status_code, resp.text[:300])
            if resp.status_code >= 400:
                resp.read()
                raise LLMError(f"模型接口返回 {resp.status_code}: {_brief(resp.text)}")
            for line in resp.iter_lines():
                if not line:
                    continue
                chunk = line[5:].strip() if line.startswith("data:") else line.strip()
                if chunk == "[DONE]":
                    break
                try:
                    obj = json.loads(chunk)
                except ValueError:
                    continue                      # 心跳等非 JSON 行，跳过
                choices = obj.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}
                think = delta.get("reasoning_content")
                if think:
                    on_delta("reasoning", think)
                text = delta.get("content")
                if text:
                    parts.append(text)
                    on_delta("content", text)
        return "".join(parts)

    @staticmethod
    def _backoff(attempt: int) -> float:
        """指数退避：1s、2s、4s…封顶 8s，加少量随机抖动防雪崩"""
        return min(2 ** attempt, 8) + random.uniform(0, 0.4)

    @staticmethod
    def _notify(on_retry, note: str, attempt: int, total: int):
        if on_retry:
            try:
                on_retry(note, attempt, total)
            except Exception:                   # noqa: BLE001
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
