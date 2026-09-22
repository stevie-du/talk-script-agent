# -*- coding: utf-8 -*-
"""LLM 客户端：OpenAI 兼容接口 + JSON 结构化输出 + pydantic 校验重试

两层重试：
  1. 请求层（本文件）：网络错误 / 超时 / 429 / 5xx 自动指数退避重试，次数与超时可配置；
     429 额外尊重上游 Retry-After 头、用更长的限流专用退避（见 _backoff）
  2. 解析层（chat_json）：模型返回的 JSON 不合法时，带错误信息重试一次；
     空内容 / 被 max_tokens 截断（finish_reason=length）时**升级预算**重试，
     并附「完整输出」提示 —— 不原样重发（P0-2 / 主报告 R3.3）。
     `_complete` 在 try 内调用、把 (正文, finish_reason) 一起交回来 ——
     `EmptyContentError` 与 length 截断都落在「预算升级重试」这一条分支上，
     不再是死代码；请求次数与第二次预算由 tests/test_llm_retry.py 钉住。

支持 mock 模式（TALKSCRIPT_MOCK=1 或 config llm.mock: true），
无 API Key 也能跑通全流程（返回固定夹具，用于开发与验收）。

两处修复：
  - **连接复用**：原来每次调用都走 `httpx.post` / `httpx.stream` 顶层函数，
    每次都新建连接（重新 TCP + TLS 握手）。一次生成正常 3~5 个调用、外加重试，
    这部分开销纯属浪费。现在共用一个线程安全的 `httpx.Client`（连接池）。
  - **流式分支的空内容诊断**：原来 `if on_delta: return self._stream_once(...)`
    直接返回，绕过了后面那段「模型返回空内容 → 请调大 max_tokens」的诊断，
    而 pipeline 全程都传 on_delta —— 于是这条最有用的排障提示在真实使用中
    永远不会出现，用户只会看到误导性的「模型连续 N 次输出的结构都不符合要求」
    （那是"它没写完"被说成"它写坏了"）。
"""
from __future__ import annotations

import email.utils
import json
import logging
import math
import random
import re
import threading
import time
from datetime import datetime, timezone

import httpx
from pydantic import BaseModel, ValidationError

from .config import LLMConfig
from . import mock_fixtures

log = logging.getLogger(__name__)

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
# P1-5：TCP/TLS 建连的等待上限。与读超时分开给（见 LLMClient._timeout）——
# 传裸 float 时 httpx 会把 connect 一并抬到 180，对端不可达就要空等三分钟。
CONNECT_TIMEOUT = 20.0

_JSON_FENCE = re.compile(r"```(?:json)?\s*(.+?)\s*```", re.S)

# 上游响应体回显长度：修复前是 300 字符原样进 error 字段，并落盘进 job.json、
# 展示在界面上。上游若回显请求上下文，可能把敏感信息一并带出去，收紧到 160。
ERROR_BODY_CHARS = 160

# 上游响应回显的脱敏（P1）：`_brief` 把 sk- 密钥 / Bearer / Authorization 头
# 全部掩掉 —— 实测上游 500 回显里放 46 字符假 Key 会原样落进 job.error / 历史 /
# 界面。**先掩码后截断**：密钥会被整体替换成短掩码，即使原本跨 160 字符边界
# 也不会把密钥尾巴露出来。
_KEY_MASK = re.compile(r"sk-[A-Za-z0-9_\-]{8,}")
_BEARER_MASK = re.compile(r"(?i)bearer\s+\S+")
_AUTH_MASK = re.compile(r"(?i)authorization\s*[:=]\s*\S+")

# 各生成阶段的中文名，错误文案里给用户指路用（见 _ensure_content）。
# pipeline 传入的 task 只有这几种。
_STAGE_NAME = {
    "select": "选题",
    "write": "文案撰写",
    "storyboard": "分镜",
    "rewrite_segment": "单段重写",
    # 建包用的是全代码库最大的单次输出（PackGenOut），也是最容易把预算想满的那次
    # ——文案要能说出"是哪件事失败了"，否则用户只看到一句没有主语的「模型返回空内容」。
    "packgen": "行业包生成",
}

# 解析错误回显长度：修复前 `str(e)` 整段（实测 981 字符、5 条 validation
# errors、还夹着模型输出片段）原样进 error 字段并落盘，截断到 200 以内，
# 前置中文引导（见 chat_json 的最终 raise）。
PARSE_ERROR_CHARS = 200


def _validation_detail(e: Exception) -> str:
    """解析错误的回显：压成单行 + 截断。pydantic 的英文原文只取前 200 字符。

    ⚠ 这一份是**给模型看的**（chat_json 的 re-prompt）与写日志用的；界面上那句
    用户话术走 `_structural_summary` —— 原文里有 `2 validation errors for ScriptDraft`
    这种内部类名，贴到中文错误框里等于没说话（第 10 轮复核 P3）。
    """
    return re.sub(r"\s+", " ", str(e or ""))[:PARSE_ERROR_CHARS]


def _structural_summary(e: Exception) -> str:
    """把"模型这次又没按结构输出"收成一句中文：哪个字段缺了、哪个字段类型不对。

    只保留**字段路径**（那是用户能对着行业包 schema 查的东西），丢掉英文句子。
    `json.loads` 直接失败那一支没有结构化 errors，这里**也不回退原文**：
    原文是 `Expecting value: line 1 column 1 (char 0)` 这类英文（第 11 轮复核 P3 抓到
    第一版正是从这里把英文又漏进了界面），而它含不含类名都不该给用户看 ——
    完整原文由 `chat_json` 收尾那句 `log.warning` 落到引擎日志。
    """
    errs = []
    if isinstance(e, ValidationError):
        try:
            errs = e.errors() or []
        except Exception:                      # noqa: BLE001 —— 归因不许挡住收口
            errs = []
    parts = []
    for er in errs[:6]:
        loc = ".".join(str(p) for p in (er.get("loc") or ()))
        kind = str(er.get("type") or "")
        what = "缺了这个字段" if kind.endswith("missing") else "这个字段结构不对"
        parts.append(f"{loc or '整体'}{what}")
    if not parts:
        return "输出不是可解析的 JSON"
    more = f"（另有 {len(errs) - 6} 处，详情看引擎日志）" if len(errs) > 6 else ""
    return "；".join(parts) + more


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
                timeout=httpx.Timeout(180.0, connect=CONNECT_TIMEOUT),
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
    """压缩上游响应体：去换行、对敏感信息掩码、截断。

    掩码顺序：Authorization 头 → Bearer → sk- 密钥。先掩后截（见 _KEY_MASK 的注释）。
    """
    t = re.sub(r"\s+", " ", (text or ""))
    t = _AUTH_MASK.sub("authorization: ***", t)
    t = _BEARER_MASK.sub("Bearer ***", t)
    t = _KEY_MASK.sub("sk-***", t)
    return t[:ERROR_BODY_CHARS]


class LLMClient:
    def __init__(self, cfg: LLMConfig, mock: bool = False, on_retry=None):
        self.cfg = cfg
        self.mock = mock or bool(cfg.api_key == "MOCK")
        self.on_retry = on_retry          # on_retry(note, attempt, total) —— 供流水线记录日志

    # ── 公开入口 ────────────────────────────────────────────
    def chat_json(self, task: str, system: str, user: str, model_cls: type[BaseModel],
                  max_retries: int = 1, on_retry=None, temperature: float | None = None,
                  on_delta=None, max_tokens: int | None = None, on_attempt=None,
                  should_abort=None, usage: dict | None = None,
                  deadline: float | None = None) -> BaseModel:
        """请求 JSON 输出并校验为 model_cls；校验失败带错误信息重试一次。

        temperature 传入时覆盖本次调用的默认温度（「换一版」用它换取不同表达）。
        on_delta(kind, text) 传入时改用流式请求，供界面实时显示思考过程；
        返回值仍是拼接好的完整正文，校验与重试逻辑完全不受影响。
        max_tokens 传入时覆盖本次调用的输出预算（分阶段预算用：
        select / rewrite_segment 的输出远小于 write，没必要共享 16000 的全额预算）。
        on_attempt() 在**每一次真正的 HTTP 尝试**开始前调用 —— 流式缓冲要按
        「尝试」而不是按「轮」清零（P2-13，见 _delta_handler 的注释）。
        should_abort() 在每次尝试开始前、以及流式收包每一行之前调用（P1-45）：
        调用方用它把「用户点了停止 / 整作业超预算」传进网络层，否则一条只发心跳的
        流可以无限期占住并发额度。它**必须抛异常**才生效（返回 True 不被解释）。
        usage 传入时收上游回的 token 用量（P3-15：只记账，不参与判定）。
        """
        if self.mock:
            data = mock_fixtures.response_for(task, user)
            return model_cls.model_validate(data)

        on_retry = on_retry or self.on_retry
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        last_err: Exception | None = None
        # 输出预算按次累计：**重试必须升级参数，不能原样重发**。
        # 主报告 R3：同样的 payload 打第二遍注定复现 —— 空内容是思考吃光预算，
        # 再发一遍照样吃光（P0-2）；被 max_tokens 截断（finish_reason=length）
        # 同理。两者都是「预算问题」，走同一条升级预算的重试；JSON 校验失败
        # 是「内容问题」，加了错误提示的重发才是它的解药。
        # `_complete` 必须放在 try 内调用：`EmptyContentError` 只在它里面抛，
        # 放外面就是 P0 的死代码 —— 那条升级分支从未触发过。
        # stage 让错误文案能指路：选题/分镜/单段重写用的是引擎写死的阶段预算
        # （4000/4000/3000），那几种情况下改 llm.max_tokens 不生效。
        stage = _STAGE_NAME.get(task)
        mt = max_tokens
        for attempt in range(max_retries + 1):
            truncated = False
            try:
                content, finish = self._complete(messages, on_retry=on_retry,
                                                 temperature=temperature,
                                                 on_delta=on_delta, max_tokens=mt,
                                                 stage=stage, on_attempt=on_attempt,
                                                 should_abort=should_abort, usage=usage, deadline=deadline)
                if (content or "").strip() and finish == "length":
                    # 被输出预算截断：JSON 一定残缺，解析必失败。与空内容同源的
                    # 预算问题 → 升级预算 + 带「完整输出」提示重试（R3.3：不是
                    # 同一份 payload 原样重发）。
                    truncated = True
                    raise EmptyContentError(
                        f"模型输出被输出预算截断（finish_reason=length，正文 {len(content)} 字符，"
                        f"{stage + '阶段' if stage else '本次调用'}的 JSON 不完整）。")
                return model_cls.model_validate(_extract_json(content))
            except EmptyContentError as e:
                last_err = e
                if attempt + 1 >= max_retries + 1:
                    continue
                cur = mt or self.cfg.max_tokens
                # 升级上限不能低于本次预算：`llm.max_tokens` 是可配到 200000 的，
                # 而 RETRY_UPGRADE_CAP 是写死的 20000 —— 直接取 min 会把"升级"
                # 变成**降档重发**（60000 → 20000），而截断这一支注定再次截断。
                cap = max(RETRY_UPGRADE_CAP, cur)
                nxt = min(int(cur * 3 / 2), cap)
                if nxt <= cur and not truncated:
                    # 预算已经到顶，而空内容这一支不会往 messages 里加任何东西 ——
                    # 再发一次就是**逐字节相同的 payload**，正是主报告 R3.3 批评的
                    # 「用完全相同的请求再打一遍，注定复现」。到顶就收手，
                    # 把这一次的钱与等待省下来，并把原因说清楚。
                    last_err = EmptyContentError(
                        f"{e} 本次预算 {cur} 已无可升空间（升级上限 {cap}），"
                        f"不再原样重发（同一份请求只会再空一次）。"
                        f"这条作业请换非推理档，或降低 pack 的 limits.recheck_rounds。")
                    break
                if truncated:
                    messages.append({"role": "assistant", "content": content})
                    messages.append({"role": "user", "content":
                                     "你的输出被输出长度（max_tokens）预算截断，JSON 不完整。"
                                     "请一次性完整输出目标 JSON 对象，不要省略或截断任何字段。"})
                mt = nxt
                if on_retry:
                    up = (f"已把输出预算提高到 {mt}" if mt > cur
                          # 到顶之后仍重发的那一支只有截断：payload 变了（带「完整输出」
                          # 提示），说"提高到"同一个数字是在编造一个没发生的动作。
                          else f"预算 {cur} 已到升级上限、只能原额再发")
                    on_retry(
                        (f"输出被截断（finish_reason=length），{up}并提示完整输出重试"
                         if truncated else f"模型返回空内容，{up}重试"),
                        attempt + 1, max_retries + 1)
            except (ValueError, ValidationError) as e:
                last_err = e
                if on_retry:
                    on_retry("JSON 结构不符合要求", attempt + 1, max_retries + 1)
                messages.append({"role": "assistant", "content": content})
                messages.append({"role": "user", "content":
                                 f"你的输出不是合法的目标 JSON：{_validation_detail(e)}。"
                                 f"只输出一个符合给定字段结构的 JSON 对象，不要多余文字。"})
        # 重试耗尽：保持异常类型 —— 空内容/截断是「预算问题」，JSON 校验是「内容问题」，
        # 调用方（pipeline 的错误展示 / 测试）要能区分。
        if isinstance(last_err, EmptyContentError):
            raise last_err
        # ⚠ 这句是**给用户看**的，不是给模型看的（给模型的那份在上面的 re-prompt 里，
        #   那里才该贴 pydantic 原文）。修复前它写成「模型输出无法解析为 ScriptDraft」：
        #   `ScriptDraft` 是代码内部的类名，界面上没有任何一个地方叫这个名字；
        #   后面缀的又是整段英文 validation error —— 中文错误框里三处没有一句能照着做。
        #   原文改成本地日志：排查要看的字段级细节一条不少，界面那句只留能照着做的。
        log.warning("模型输出结构不符（%s 次尝试后用尽）：%s",
                    max_retries + 1, _validation_detail(last_err))
        raise LLMError(f"模型连续 {max_retries + 1} 次输出的结构都不符合要求"
                       f"（{_structural_summary(last_err)}），本次任务已停止。"
                       f"可以再试一次；反复出现时换一档不那么爱加解释文字的模型，"
                       f"或在设置里调大输出预算。")

    def ping(self) -> tuple[bool, str]:
        """最小连通性测试：不约束输出格式，返回 (是否连通, 说明)。

        用于设置页「测试连接」——只验证 Key / 地址 / 模型名是否可用，
        不消耗有意义的 token，也不要求模型支持 JSON 输出模式。
        """
        if self.mock:
            return True, "mock 模式（未实际请求模型）"
        try:
            content, _ = self._complete([{"role": "user", "content": "ping"}],
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
                  temperature: float | None = None, on_delta=None,
                  stage: str | None = None, on_attempt=None,
                  should_abort=None, usage: dict | None = None,
                  deadline: float | None = None) -> tuple[str, str | None]:
        """单次网络请求（含重试），返回 (完整正文, finish_reason)。

        finish 必须交回去：`finish == "length"` 且正文非空 = JSON 被预算截断，
        chat_json 要靠它升级预算重试，而不是把同一份 payload 原样重发
        （P0-2 / R3.3）。stage 只用于错误文案指路。
        on_attempt 在每次 HTTP 尝试前调用（流式缓冲按尝试清零，P2-13）。
        should_abort 在流式收包的**每一行**之前调用（P1-45：只挂在 on_delta 上的话，
        一条只发心跳、不发正文的流永远查不到取消）。usage 传入时收上游回的使用量
        （P3-15：只记账，不参与任何判定）。
        """
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
        # 文案指路要分清预算从哪来：显式传了 max_tokens 的是**引擎写死的阶段预算**
        # （选题/分镜 4000、单段重写 3000），改 config 的 llm.max_tokens 不影响它；
        # 没传的（write）用的就是 cfg.max_tokens —— 修复前只看 `stage` 是否非空，
        # 于是「文案撰写」也报「引擎写死的阶段预算」，把用户指到一个改了也没用的键上。
        stage_budget = max_tokens is not None
        headers = {"Authorization": f"Bearer {self.cfg.api_key}"}
        attempts = max(1, int(self.cfg.retries) + 1)
        client = get_client()
        last_err: Exception | None = None

        for attempt in range(attempts):
            # 退避等待期间被取消/超预算：别再打下一次请求（钱与等待都省了）。
            # 流式分支另有逐行检查（P1-45），这里是两条分支共用的「尝试边界」闸。
            if should_abort:
                should_abort()
            if on_delta and on_attempt:
                # 被丢弃的那次尝试的思考字数不该累进界面显示的「思考 N 字」（P2-13）
                self._notify(on_attempt, "", attempt + 1, attempts)
            try:
                if on_delta:
                    # 流式：边收边把增量交出去；返回值仍是完整正文，调用方无感
                    content, finish = self._stream_once(
                        client, url, payload, headers, on_delta,
                        should_abort=should_abort, usage=usage, deadline=deadline)
                    return self._ensure_content(content, finish, streamed=True,
                                                 budget=payload["max_tokens"],
                                                 stage=stage,
                                                 stage_budget=stage_budget), finish
                resp = client.post(url, json=payload, headers=headers,
                                   timeout=self._timeout(self._capped_read(
                                       self.cfg.timeout, deadline)))
            except RetryableStatus as e:             # 流式分支里的 429/5xx
                last_err = e
                if attempt + 1 < attempts:
                    wait = self._backoff(attempt, rate_limited=(e.code == 429),
                                         retry_after=e.retry_after)
                    # 通知与真正要睡的秒数同源：夹逼与下限都算在内，两边共用同一个值
                    wf = 0.0 if should_abort else 0.2
                    wait = self._clamped_wait(wait, deadline, wf)
                    note = (f"上游限流(429)，{self._fmt_wait(wait)}s 后重试" if e.code == 429
                            else f"接口返回 {e.code}")
                    self._notify(on_retry, note, attempt + 1, attempts)
                    self._interruptible_sleep(wait, should_abort, deadline, floor=wf)
                    continue
                raise LLMError(f"模型接口返回 {e.code}: {_brief(e.text)}") from e
            except httpx.RequestError as e:          # 超时 / 连接失败 / 网络中断
                # ⚠ 先问一句"是不是预算用尽了/被取消"再决定怎么说：读超时被
                #   `_capped_read` 主动压到作业预算的到期点（P2-48），于是
                #   「到点」这件事在协议层长得跟「网络断了」一模一样 ——
                #   原样报「模型接口连接失败（已重试 N 次）」是**误归因**，
                #   用户会去查网线，而真实原因是上游卡死 + 预算用尽。
                if should_abort:
                    should_abort()
                last_err = e
                if attempt + 1 < attempts:
                    self._notify(on_retry, f"网络异常（{type(e).__name__}）", attempt + 1, attempts)
                    self._interruptible_sleep(self._backoff(attempt), should_abort, deadline,
                                              floor=0.0 if should_abort else 0.2)
                    continue
                raise LLMError(f"模型接口连接失败（已重试 {attempts} 次）："
                               f"{type(e).__name__}: {_brief(str(e))}") from e

            if resp.status_code in RETRYABLE_STATUS and attempt + 1 < attempts:
                rate_limited = resp.status_code == 429
                wait = self._backoff(attempt, rate_limited=rate_limited,
                                     retry_after=(_retry_after_seconds(resp.headers)
                                                  if rate_limited else None))
                wf = 0.0 if should_abort else 0.2      # 通知与实睡同一个口径
                wait = self._clamped_wait(wait, deadline, wf)
                note = (f"上游限流(429)，{self._fmt_wait(wait)}s 后重试" if rate_limited
                        else f"接口返回 {resp.status_code}")
                self._notify(on_retry, note, attempt + 1, attempts)
                self._interruptible_sleep(wait, should_abort, deadline, floor=wf)
                continue
            if resp.status_code >= 400:
                raise LLMError(f"模型接口返回 {resp.status_code}: {_brief(resp.text)}")

            try:
                data = resp.json()
                content = data["choices"][0]["message"]["content"]
            except (KeyError, IndexError, TypeError, ValueError) as e:
                # TypeError：上游把 choices[0] 回成了字符串/列表 —— 修复前它不在
                # 捕获之列，于是一句英文 `string indices must be integers` 直接
                # 冒到界面，用户看不出这是"接口结构不对"而不是"你的配置不对"。
                raise LLMError(f"模型接口返回结构异常: {_brief(resp.text)}") from e
            if usage is not None and isinstance(data, dict):
                u = data.get("usage")
                if isinstance(u, dict):
                    usage.update({k: v for k, v in u.items()
                                  if isinstance(v, (int, float))})
                    for det in ("completion_tokens_details", "prompt_tokens_details"):
                        if isinstance(u.get(det), dict):
                            usage[det] = u[det]
            finish = ((data.get("choices") or [{}])[0] or {}).get("finish_reason")
            return self._ensure_content(content, finish, budget=payload["max_tokens"],
                                        stage=stage), finish

        raise LLMError(f"模型接口连续失败（已重试 {attempts} 次）：{_brief(str(last_err))}")

    def _ensure_content(self, content: str, finish, *, streamed: bool = False,
                        budget: int | None = None, stage: str | None = None,
                        stage_budget: bool = False) -> str:
        """空内容统一在这里报错。

        修复前只有非流式分支做这个检查，而 pipeline 全程走流式 ——
        等于这条诊断永远不触发。

        budget 是**本次调用实际用的**输出预算：ping 传 512 时若按 cfg 的
        16000 报「请调大 max_tokens（当前 16000）」是在误导（P2-10）。
        stage 是生成阶段名，stage_budget 说明本次预算是不是**引擎写死的阶段预算**
        （选题/分镜 4000、单段重写 3000）—— 那几种情况下改 `llm.max_tokens`
        根本不影响本次调用，文案不能把方向指到 config 上去；write 用的是
        cfg.max_tokens，指错了就是另一回事。
        实测「预算越大思考越长」（deepseek-v4-flash：6000 → 思考 6.4~9.4k，
        16000 → 17.4~18.9k），所以文案的结论是「调大只会更慢」，
        建议先换非推理档或降 recheck_rounds。
        """
        if (content or "").strip():
            return content
        where = ("（引擎写死的阶段预算，改 llm.max_tokens 不影响本次调用）"
                 if stage_budget else "（config.yaml 的 llm.max_tokens）")
        raise EmptyContentError(
            f"模型返回了空内容（HTTP 200，但 content 为空{'' if finish is None else f'，finish_reason={finish}'}）。"
            f"{f'{stage}阶段，' if stage else ''}本次调用实际预算 {budget or self.cfg.max_tokens}"
            f"{where}"
            "，已被推理模型的思考 token 吃光。"
            "注意：调大预算只会让思考更长、输出更慢，并不保证正文出现；"
            "先换非推理档（无思考通道的模型），或降低回炉轮数"
            "（pack 里 limits.recheck_rounds、别开「换一版」），而不是先调大 max_tokens。"
            + ("（本次为流式请求：思考内容已收到，但正文为空。）" if streamed else ""))

    def _stream_once(self, client: httpx.Client, url: str, payload: dict,
                     headers: dict, on_delta, should_abort=None,
                     usage: dict | None = None,
                     deadline: float | None = None) -> tuple[str, str | None]:
        """流式请求一次：逐块解析 SSE，把增量交给 on_delta，返回 (完整正文, finish_reason)。

        推理型模型在 delta 里分两条通道推送：reasoning_content（思考过程）与
        content（最终正文）。两者必须分开累计 —— 思考只是给人看的过程，
        混进正文会让 json.loads 直接失败。

        返回 finish_reason（P0-1）：修复前流式分支的 finish 写死 None，
        而 pipeline 全程走流式 —— 于是「这次是被 max_tokens 截断的」这个信号
        没有任何代码知道，截断被当成普通空内容/解析失败，重试用同样的预算
        注定复现。最后一个 chunk 通常带 `finish_reason: stop|length`，读它。

        `should_abort()`（P1-45）：**每一行**都查一次，而不是只在收到正文增量时查。
        修复前取消/预算闸只挂在 `on_delta` 上，而一条「活着但一个字正文都不给」的流
        （上游排队、只发 `: keep-alive` 心跳、只推空 delta）永远不会触发那个回调 ——
        实测作业状态已是 cancelled、线程还在收包，而 httpx 的读超时也不会生效
        （字节一直在到达，只是没有内容）。这种流会把一个并发额度占到你重启引擎为止。
        检查成本是每行一次 `Event.is_set()` + 一次 `time.time()`，相对网络可忽略。

        `usage` 传入时把上游回的使用量就地填进去（P3-15）：OpenAI 兼容带的
        usage 在一个**没有 choices** 的末尾 chunk 里，原来那行 `if not choices:
        continue` 正好把它丢掉。只记账、不改任何判定。
        """
        body = {**payload, "stream": True}
        parts: list[str] = []
        finish: str | None = None
        with client.stream("POST", url, json=body, headers=headers,
                           timeout=self._timeout(self._capped_read(
                               self.cfg.timeout * STREAM_READ_TIMEOUT_MULT, deadline))) as resp:
            if resp.status_code in RETRYABLE_STATUS:
                resp.read()
                raise RetryableStatus(resp.status_code, resp.text[:300],
                                      retry_after=(_retry_after_seconds(resp.headers)
                                                   if resp.status_code == 429 else None))
            if resp.status_code >= 400:
                resp.read()
                raise LLMError(f"模型接口返回 {resp.status_code}: {_brief(resp.text)}")
            for line in resp.iter_lines():
                if should_abort:
                    should_abort()          # 抛异常即断开：不替上游继续收包
                if not line:
                    continue
                chunk = line[5:].strip() if line.startswith("data:") else line.strip()
                if chunk == "[DONE]":
                    break
                try:
                    obj = json.loads(chunk)
                except ValueError:
                    continue                      # 心跳等非 JSON 行，跳过
                if not isinstance(obj, dict):
                    continue                      # 数组/字面量等畸形行：不是本层的错，跳过
                if obj.get("error"):
                    # HTTP 200 但 SSE 里带 error 字段（网关把错误塞进流）：
                    # 修复前被当「空内容」报「请调大 max_tokens」，方向全错。
                    # 这是真正的接口错误，抛 LLMError 与「空内容」区分开。
                    err = obj["error"]
                    msg = err.get("message") if isinstance(err, dict) else str(err)
                    raise LLMError(f"模型接口返回错误: {_brief(msg)}")
                u = obj.get("usage")
                if isinstance(u, dict) and usage is not None:
                    usage.update({k: v for k, v in u.items() if isinstance(v, (int, float))})
                    for det in ("completion_tokens_details", "prompt_tokens_details"):
                        if isinstance(u.get(det), dict):
                            usage[det] = u[det]
                choices = obj.get("choices") or []
                if not choices:
                    continue
                first = choices[0]
                if not isinstance(first, dict):
                    # 结构畸形到没法读 delta：说清楚，别让它冒成一个英文 TypeError
                    raise LLMError(f"模型接口返回结构异常（choices[0] 不是对象）："
                                   f"{_brief(chunk)}")
                fr = first.get("finish_reason")
                if fr:
                    finish = fr
                delta = first.get("delta") or {}
                if not isinstance(delta, dict):
                    continue
                think = delta.get("reasoning_content")
                if think:
                    on_delta("reasoning", think)
                text = delta.get("content")
                if text:
                    parts.append(text)
                    on_delta("content", text)
        return "".join(parts), finish

    @staticmethod
    def _timeout(read: float) -> httpx.Timeout:
        """P1-5：连接超时与读超时分开给。

        修复前两个调用点都传**裸 float**（`timeout=self.cfg.timeout`），
        httpx 会把 read/write/pool/connect 全部设成同一个数 —— 于是
        `get_client()` 里写的 `connect=20.0` 形同虚设：对端不可达时
        每次尝试要空等 180 秒才报「连接失败」。
        """
        return httpx.Timeout(float(read), connect=CONNECT_TIMEOUT)

    @staticmethod
    def _clamped_wait(seconds: float, deadline: float | None = None,
                      floor: float = 0.0) -> float:
        """把一次等待夹到"剩余作业预算"里 —— 只此一处，别在调用点各算各的。

        界面上那句「60s 后重试」与线程真正睡的秒数必须是同一个数：批次 10 把
        退避夹进预算之后，通知仍按**未夹**的 `wait` 生成（第 6 轮复核抓到），
        于是用户看到"60s 后重试"而实际 2 秒就继续了。

        `floor` 管另一半：预算已到点时夹出来是 0 —— 有作业闸门可问时这正是对的
        （下一次尝试前的闸门会以「预算用尽」收工）；但**没有闸门可问**的调用点，
        0 退避等于把重试打成"立刻连打 N 次"的 hammer（第 7 轮实测：4 个请求 0 秒
        内全发完）。那种调用点给个下限，慢一点但不出环。
        """
        wait = max(0.0, float(seconds))
        if not deadline:
            # ⚠ 没有预算可夹的时候，下限同样要生效（第 9 轮实测：原来这里直接 return，
            #   floor 一次都没参与）—— 而 `ping()` 恰好是**唯一**不带 deadline 也不带
            #   闸门的重试入口：`Retry-After: 0` + retries=10 时 11 个请求 0.001s 发完、
            #   一次都没睡，这条下限在它唯一为之而写的调用点上贡献为 0。
            return max(wait, float(floor))
        left_budget = float(deadline) - time.time()
        if left_budget <= 0:
            return max(0.0, float(floor))   # 到点：有闸门交给闸门，没闸门也别空转
        return max(min(wait, left_budget), float(floor))

    @staticmethod
    def _fmt_wait(seconds: float) -> str:
        """退避秒数的展示：不到 1 秒也要说真数，不许印成「0s」。

        第 9 轮实测：`:.0f` 遇上 floor 的 0.2 就印「0s 后重试」，界面写着 0 秒、
        实际睡了 0.2 秒 —— 与这组函数存在的理由（通知与实睡同源）正好相反。
        """
        w = max(0.0, float(seconds))
        # 一位小数向下取整到"看不出来"也不行：2.5s 用 `:.0f` 会印成「2」（Python 用
        # 银行家舍入，3.5 反而印 4）—— 通知与实睡同源是这组函数存在的理由，
        # 差 0.5 秒也是差。所以整数才去掉小数点，带小数的一律原样说。
        # 只能**向上**取整（第 12 轮实测）：`round(90.06,1)` 给 90.0，而等待随后还会被
        # `_clamped_wait` 抬到下限 0.2 → 通知说 90s、实睡 90.2s。少说一次等待时长正是
        # 这组函数存在的理由的反面；429 的 Retry-After 语义是"至少等这么久"。
        s = f"{math.ceil(w * 10) / 10:.1f}"
        # ceil 之后任何 0<w<0.05 都会得到 "0.1"（宁可多说，也绝不说 0），
        # 所以这里不再需要补小数位；只有 w==0 才印 "0"。
        return s.removesuffix(".0")

    @staticmethod
    def _interruptible_sleep(seconds: float, should_abort=None,
                             deadline: float | None = None,
                             floor: float = 0.0) -> None:
        """可打断、且不睡过作业预算的退避等待。

        两个独立的洞，一次补：
        1. 原来是一把 `time.sleep(wait)`：用户在退避那几十秒里点「停止」，
           界面已经变了（`request_cancel` 立即落状态），线程却还要睡满
           Retry-After（封顶 60s）才走到下一个检查点 —— 期间它还占着一个并发额度，
           用户看到的是"点了停止没反应"。
        2. `wait` 本身不受 `deadline` 约束（P2-49，复核实测）：预算只剩 2 秒、
           上游回 `Retry-After: 60` 时，`_capped_read` 只管读超时，睡照样睡满 60s，
           `retries=3` 时最坏可达 ~3×60s —— 比整作业预算还长。
           剩余预算内睡不完就到此为止：下一次尝试前的闸门会以「预算用尽」收工。

        没有闸门（`should_abort=None`，例如 `ping`）时保持一次性的 `time.sleep`：
        可打断只在有作业可问的时候才有意义，也保住既有对退避时长的观测口径。

        ⚠ `floor` 必须由调用点**原样传进来**：第 8 轮复核实测，这里用默认值再夹一次，
        等于把调用点刚夹出来的 0.2 秒又压回 0 —— 那条防"0 秒连打 N 次"的下限
        成了安慰剂（4 个请求 0.000s 发完，与修前一模一样），而且通知说 0.2s、
        实际睡 0.0s，正好破了这个函数存在的理由（通知与实睡同源）。
        """
        wait = LLMClient._clamped_wait(seconds, deadline, floor)
        if not should_abort:
            if wait > 0:
                time.sleep(wait)
            return
        end = time.time() + wait
        while True:
            left = end - time.time()
            if left <= 0:
                return
            should_abort()                    # 该抛就抛：取消/预算不该再等
            time.sleep(min(0.5, left))

    @staticmethod
    def _capped_read(base: float, deadline: float | None) -> float:
        """把读超时压到"最多等到作业预算到点"（P2-48）。

        取消与预算的检查点都长在"收到一行"上；一条**一个字都不吐**的流永远走不到
        那些检查点，而 `timeout × STREAM_READ_TIMEOUT_MULT` 再乘重试次数实测最坏
        ≈48 分钟 —— 是整作业预算（1200s）的 2.4 倍，槽被一个卡死的上游占着。
        剩余时间不足时按下限 1 秒给，好让下一次 `should_abort()` / 超时以
        「预算用尽」收场，而不是无期等待。
        """
        if not deadline:
            return base
        left = float(deadline) - time.time()
        return max(1.0, min(base, left))

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
