# -*- coding: utf-8 -*-
"""LLM 客户端：OpenAI 兼容接口 + JSON 结构化输出 + pydantic 校验重试

两层重试：
  1. 请求层（本文件）：网络错误 / 超时 / 429 / 5xx 自动指数退避重试，次数与超时可配置；
     429 额外尊重上游 Retry-After 头、用更长的限流专用退避（见 _backoff）
  2. 解析层（chat_json）：模型返回的 JSON 不合法时，带错误信息重试一次

⚠ 已知缺陷（照这份文档写新代码前必读）：`chat_json` 里"空内容 → 提高预算重试"那条分支
   **当前不生效** —— `_complete` 在 `try` 之外调用，而 `EmptyContentError` 只在 `_complete`
   内部抛，所以那个 `except` 收不到它，空内容仍是一次请求、原预算、直接冒到作业层。
   另有一条测试（test_llm_retry 的 test_streaming_empty_content）用正则断言文案里的数字，
   看不见"只发了一次请求"，所以它是绿灯。两处都还没修，见《审查报告-20260920》P0-2。
   同理，`finish_reason` 虽然已经从流式分支取了回来，但除错误文案外无人消费 ——
   "非空但被 length 截断"仍会拿同样的 payload 重发一遍。

支持 mock 模式（TALKSCRIPT_MOCK=1 或 config llm.mock: true），
无 API Key 也能跑通全流程（返回固定夹具，用于开发与验收）。

两处修复：
  - **连接复用**：原来每次调用都走 `httpx.post` / `httpx.stream` 顶层函数，
    每次都新建连接（重新 TCP + TLS 握手）。一次生成正常 3~5 个调用、外加重试，
    这部分开销纯属浪费。现在共用一个线程安全的 `httpx.Client`（连接池）。
  - **流式分支的空内容诊断**：原来 `if on_delta: return self._stream_once(...)`
    直接返回，绕过了后面那段「模型返回空内容 → 请调大 max_tokens」的诊断，
    而 pipeline 全程都传 on_delta —— 于是这条最有用的排障提示在真实使用中
    永远不会出现，用户只会看到误导性的「模型输出无法解析为 ScriptDraft」。
"""
from __future__ import annotations

import email.utils
import json
import random
import re
import threading
import time
from datetime import datetime, timezone

import httpx
from pydantic import BaseModel, ValidationError

from .config import LLMConfig
from . import mock_fixtures

# 这些状态码视为瞬时故障，值得重试；401/403/400 等重试无意义
RETRYABLE_STATUS = {429, 500, 502, 503, 504}

# 429（限流）的等待口径。主流 SDK（openai / anthropic / liteLLM）对限流
# 的共识：优先按上游 Retry-After 等，没有就给**比普通故障更长的**指数退避
# —— 限流要等窗口过去，短退避试了也白试。
# Retry-After 尊重但封顶：交互式应用里一条作业被一次限流拖进无限等待，
# 比「少活一次重试」更糟。
RETRY_AFTER_WAIT_MAX = 60.0      # 上游 Retry-After 封顶（秒）
RATE_LIMIT_BACKOFF_BASE = 1.5    # 429 无 Retry-After 时的退避底数（1.5s 起）
RATE_LIMIT_BACKOFF_MAX = 30.0    # 429 指数退避封顶（秒）
# 空内容重试时的预算升级上限（P0-2：重试必须升级参数，但不能无限放大到
# 超出上游单次输出上限 —— 16000 的 1.5 倍左右即合理停靠点）。
RETRY_UPGRADE_CAP = 20000
# P1-5：流式 read timeout 是「两次 chunk 之间的空闲」，不是总时长 —— 推理模型
# 思考期不 flush，默认 180s 会在思考中途误杀（generated/20260912 的 job.json
# 两条 retry 间隔正是 180s/181s 的 ReadTimeout）。流式分支放大一倍。
STREAM_READ_TIMEOUT_MULT = 2.0

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

    def __init__(self, code: int, text: str, retry_after: float | None = None):
        super().__init__(str(code))
        self.code = code
        self.text = text
        self.retry_after = retry_after


def _retry_after_seconds(headers) -> float | None:
    """解析上游 429 的 Retry-After（秒数或 HTTP 日期格式）。

    主流网关两种写法都发：`Retry-After: 30` 与 `Retry-After: Fri, ... GMT`。
    拿不到 / 解析不了返回 None —— 调用方退回限流专用指数退避。
    """
    raw = str((headers or {}).get("retry-after", "")).strip()
    if not raw:
        return None
    if raw.isdigit():
        return float(raw)
    try:
        dt = email.utils.parsedate_to_datetime(raw)
        return max(0.0, (dt - datetime.now(timezone.utc)).total_seconds())
    except (TypeError, ValueError):
        return None


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
                  on_delta=None, max_tokens: int | None = None) -> BaseModel:
        """请求 JSON 输出并校验为 model_cls；校验失败带错误信息重试一次。

        temperature 传入时覆盖本次调用的默认温度（「换一版」用它换取不同表达）。
        on_delta(kind, text) 传入时改用流式请求，供界面实时显示思考过程；
        返回值仍是拼接好的完整正文，校验与重试逻辑完全不受影响。
        max_tokens 传入时覆盖本次调用的输出预算（分阶段预算用：
        select / rewrite_segment 的输出远小于 write，没必要共享 16000 的全额预算）。
        """
        if self.mock:
            data = mock_fixtures.response_for(task, user)
            return model_cls.model_validate(data)

        on_retry = on_retry or self.on_retry
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        last_err: Exception | None = None
        # 输出预算按次累计：**重试必须升级参数，不能原样重发**。
        # 主报告 R3：同样的 payload 打第二遍注定复现 —— 空内容是思考吃光预算，
        # 再发一遍照样吃光（P0-2）。这里只对 EmptyContentError 升级预算
        # （JSON 校验失败是内容问题，加了错误提示的重发才是它的解药）。
        mt = max_tokens
        for attempt in range(max_retries + 1):
            content = self._complete(messages, on_retry=on_retry,
                                     temperature=temperature, on_delta=on_delta,
                                     max_tokens=mt)
            try:
                return model_cls.model_validate(_extract_json(content))
            except EmptyContentError as e:
                last_err = e
                if attempt + 1 >= max_retries + 1:
                    continue
                mt = min(int((mt or self.cfg.max_tokens) * 3 / 2), RETRY_UPGRADE_CAP)
                if on_retry:
                    on_retry(f"模型返回空内容，已把输出预算提高到 {mt} 重试",
                             attempt + 1, max_retries + 1)
            except (ValueError, ValidationError) as e:
                last_err = e
                if on_retry:
                    on_retry("JSON 结构不符合要求", attempt + 1, max_retries + 1)
                messages.append({"role": "assistant", "content": content})
                messages.append({"role": "user", "content":
                                 f"你的输出不是合法的目标 JSON：{str(e)[:500]}。"
                                 f"只输出一个符合给定字段结构的 JSON 对象，不要多余文字。"})
        # 重试耗尽：保持异常类型 —— 空内容是「预算问题」，JSON 校验是「内容问题」，
        # 调用方（pipeline 的错误展示 / 测试）要能区分。
        if isinstance(last_err, EmptyContentError):
            raise last_err
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
                                     json_mode=False, max_tokens=512)
        except EmptyContentError:
            # 推理模型 512 预算也可能被思考吃光：HTTP 200 已证明连通，
            # 空内容只是「它在思考」，不算连接失败（P2-10）。
            return True, "已连通（模型返回空内容，可能是思考型模型输出预算偏小）"
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
                    content, finish = self._stream_once(
                        client, url, payload, headers, on_delta)
                    return self._ensure_content(content, finish, streamed=True,
                                                 budget=payload["max_tokens"])
                resp = client.post(url, json=payload, headers=headers, timeout=self.cfg.timeout)
            except RetryableStatus as e:             # 流式分支里的 429/5xx
                last_err = e
                if attempt + 1 < attempts:
                    wait = self._backoff(attempt, rate_limited=(e.code == 429),
                                         retry_after=e.retry_after)
                    note = (f"上游限流(429)，{wait:.0f}s 后重试" if e.code == 429
                            else f"接口返回 {e.code}")
                    self._notify(on_retry, note, attempt + 1, attempts)
                    time.sleep(wait)
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
                rate_limited = resp.status_code == 429
                wait = self._backoff(attempt, rate_limited=rate_limited,
                                     retry_after=(_retry_after_seconds(resp.headers)
                                                  if rate_limited else None))
                note = (f"上游限流(429)，{wait:.0f}s 后重试" if rate_limited
                        else f"接口返回 {resp.status_code}")
                self._notify(on_retry, note, attempt + 1, attempts)
                time.sleep(wait)
                continue
            if resp.status_code >= 400:
                raise LLMError(f"模型接口返回 {resp.status_code}: {_brief(resp.text)}")

            try:
                data = resp.json()
                content = data["choices"][0]["message"]["content"]
            except (KeyError, IndexError, ValueError) as e:
                raise LLMError(f"模型接口返回结构异常: {_brief(resp.text)}") from e
            finish = ((data.get("choices") or [{}])[0] or {}).get("finish_reason")
            return self._ensure_content(content, finish, budget=payload["max_tokens"])

        raise LLMError(f"模型接口连续失败（已重试 {attempts} 次）：{_brief(str(last_err))}")

    def _ensure_content(self, content: str, finish, *, streamed: bool = False,
                        budget: int | None = None) -> str:
        """空内容统一在这里报错。

        修复前只有非流式分支做这个检查，而 pipeline 全程走流式 ——
        等于这条诊断永远不触发。

        budget 是**本次调用实际用的**输出预算：ping 传 512 时若按 cfg 的
        16000 报「请调大 max_tokens（当前 16000）」是在误导（P2-10）。
        注意选题/分镜/单段重写用的是引擎里写死的阶段预算（4000/4000/3000），
        那几种情况下改 `llm.max_tokens` 不影响本次调用。
        """
        if (content or "").strip():
            return content
        raise EmptyContentError(
            f"模型返回了空内容（HTTP 200，但 content 为空{'' if finish is None else f'，finish_reason={finish}'}）。"
            "通常原因：输出预算被推理模型的思考 token 用尽。"
            f"请调大 config.yaml 里的 llm.max_tokens（本次调用预算 {budget or self.cfg.max_tokens}），"
            "或改用非推理模型。"
            + ("（本次为流式请求：思考内容已收到，但正文为空。）" if streamed else ""))

    def _stream_once(self, client: httpx.Client, url: str, payload: dict,
                     headers: dict, on_delta) -> tuple[str, str | None]:
        """流式请求一次：逐块解析 SSE，把增量交给 on_delta，返回 (完整正文, finish_reason)。

        推理型模型在 delta 里分两条通道推送：reasoning_content（思考过程）与
        content（最终正文）。两者必须分开累计 —— 思考只是给人看的过程，
        混进正文会让 json.loads 直接失败。

        返回 finish_reason（P0-1）：修复前流式分支的 finish 写死 None，
        而 pipeline 全程走流式 —— 于是「这次是被 max_tokens 截断的」这个信号
        没有任何代码知道，截断被当成普通空内容/解析失败，重试用同样的预算
        注定复现。最后一个 chunk 通常带 `finish_reason: stop|length`，读它。
        """
        body = {**payload, "stream": True}
        parts: list[str] = []
        finish: str | None = None
        with client.stream("POST", url, json=body, headers=headers,
                           timeout=self.cfg.timeout * STREAM_READ_TIMEOUT_MULT) as resp:
            if resp.status_code in RETRYABLE_STATUS:
                resp.read()
                raise RetryableStatus(resp.status_code, resp.text[:300],
                                      retry_after=(_retry_after_seconds(resp.headers)
                                                   if resp.status_code == 429 else None))
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
                fr = choices[0].get("finish_reason")
                if fr:
                    finish = fr
                delta = choices[0].get("delta") or {}
                think = delta.get("reasoning_content")
                if think:
                    on_delta("reasoning", think)
                text = delta.get("content")
                if text:
                    parts.append(text)
                    on_delta("content", text)
        return "".join(parts), finish

    @staticmethod
    def _backoff(attempt: int, *, rate_limited: bool = False,
                 retry_after: float | None = None) -> float:
        """等待时长（秒），加少量随机抖动防雪崩。

        429 与其它瞬时故障分开退避（主流 SDK 的共识）：
          - 上游给了 Retry-After → 按它（封顶 RETRY_AFTER_WAIT_MAX，防止一条
            作业被一次限流拖进无限期等待）；
          - 429 且无 Retry-After → 限流专用**更长**的指数退避
            （RATE_LIMIT_BACKOFF_BASE×2^n，封顶 RATE_LIMIT_BACKOFF_MAX）——
            限流要等窗口过去，短退避试了也白试；
          - 5xx / 网络错误 → 原指数退避（1s 起，封顶 8s）。
        """
        if retry_after is not None:
            return min(retry_after + random.uniform(0, 0.4), RETRY_AFTER_WAIT_MAX)
        if rate_limited:
            return min(RATE_LIMIT_BACKOFF_BASE * (2 ** attempt),
                       RATE_LIMIT_BACKOFF_MAX) + random.uniform(0, 0.4)
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
