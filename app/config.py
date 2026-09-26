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

# 一个「模型条目」的默认值：连到某个服务商所需的**全部信息**。
#
# 每个条目**自带 base_url 与 api_key** —— 这正是「多模型」的价值所在：
# 可以同时配智谱和 DeepSeek 两把 Key，切换即生效，不用把 Key 重填一遍。
# 生成参数（temperature / retries / timeout / max_tokens）**不在这里** ——
# 那些是「怎么调模型」，跟连到哪家无关，仍然是全局一份（见 `llm` 段）。
DEFAULT_MODEL = {
    "id": "m-default",
    "name": "",              # 展示名；留空则显示 model 名
    "base_url": DEFAULT_CONFIG["llm"]["base_url"],
    "api_key": "",
    "model": DEFAULT_CONFIG["llm"]["model"],
}


@dataclass
class LLMModel:
    id: str
    name: str = ""
    base_url: str = DEFAULT_MODEL["base_url"]
    api_key: str = ""
    model: str = DEFAULT_MODEL["model"]

    @property
    def label(self) -> str:
        """界面上显示什么。展示名留空时退回模型名 —— 不能让一行显示成空白。"""
        return self.name or self.model


@dataclass
class LLMConfig:
    base_url: str = DEFAULT_CONFIG["llm"]["base_url"]
    api_key: str = ""
    model: str = DEFAULT_CONFIG["llm"]["model"]
    temperature: float = 0.7
    retries: int = 2          # 请求失败（网络/超时/429/5xx）自动重试次数
    timeout: float = 180      # 单次请求超时（秒）
    # 单次请求的输出预算。必须显式给足：推理型模型（deepseek 系等）的「思考」
    # token 也计入这个预算，服务端默认值容易被思考吃光 —— 表现为 llm._ensure_content
    # 那句「模型返回了空内容」（HTTP 仍是 200），而不是"结构不符"那一类。
    max_tokens: int = DEFAULT_CONFIG["llm"]["max_tokens"]


@dataclass
class AppConfig:
    # ⚠ 这是**当前生效的那一份完整配置**：连接信息取自 `active_model` 指的那条
    # 模型，生成参数取自全局 `llm` 段。之所以这么组装，是为了让 LLMClient、
    # server 里所有 `cfg.llm.xxx` 的消费方**一行都不用改** ——
    # 否则「模型从一条变多条」会波及全项目每一处发请求的地方。
    llm: LLMConfig = field(default_factory=LLMConfig)
    # 全部模型条目（至少一条）。`llm` 是其中 active 那条的展开。
    models: list["LLMModel"] = field(default_factory=list)
    active_model: str = ""
    # 每条模型**各自**的「哪些字段还是内置默认」，按 id 索引。
    # 只报当前那条是不够的：列表里其他行也要能标出来 —— 否则用户看到几条
    # 长得一模一样的条目（base_url 都显示着默认地址），分不清哪条是他配的、
    # 哪条是空壳。逐字段判，口径与下面的 `llm_defaulted` 完全一致。
    models_defaulted: dict[str, list[str]] = field(default_factory=dict)
    default_pack: str = "elevator"
    root: Path = Path(".")
    mock: bool = False
    # config.yaml 读坏时的一句话说明（读坏才非空）。随 /api/config 下发，
    # 让设置页能提示「你的配置没生效」—— 否则退回全默认这件事完全不可见。
    config_error: str = ""
    # 这些 LLM 字段在 config.yaml 里**没写**、也没有环境变量覆盖，值是内置默认
    # （model 会变成 DEFAULT_CONFIG 里那个）。与 config_error 是**同一类静默降级，
    # 只是没坏、只是没配**：不下发的话，界面会把内置默认值当用户配置显示出来
    # （设置页模型名写着 glm-4.7、输入区右侧也写着 glm-4.7），用户以为已经配好了。
    llm_defaulted: list[str] = field(default_factory=list)


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
#
# ⚠ 模板里**所有配置项一律注释掉**（原来写的是真实值，如 `model: glm-4.7`）。
# 为什么必须这样：写进去的真实值会让 `load_config` 认为「用户配过这个键」，
# 于是 `llm_defaulted` 是空的、界面把这些值当用户配置显示 ——
# 用户第一眼看到设置页写着 `model: glm-4.7`、输入区右侧也写着 `glm-4.7`，
# 以为已经配好了。**模板是程序写的，不是用户配的，不能冒充用户配置。**
# 注释掉之后取值完全不变（模板里的值与 DEFAULT_CONFIG 逐项相同，
# 缺键时 `load_config` 本来就取它）。
CONFIG_TEMPLATE = """# TalkScript 配置
#
# 这个文件由程序在首次运行时生成，位置：<数据目录>/config.yaml
#   · Windows 打包版：%APPDATA%\\TalkScript\\
#   · 开发态：项目根目录
#
# ⚠ 下面每一项**默认都是注释掉的**，注释状态等价于「没配」——
#   此时引擎用的是内置默认值，设置页会标明「还是内置默认值，不是你配的」。
#   想改哪一项，把该行的 `#` 去掉并填值，保存后重启（或点「保存」）即可。
#
# 三种配置方式，优先级从高到低：
#   1. 环境变量（密钥不落盘时用；数值项会**夹逼**回合法区间，与界面同一道闸，
#      见 app/config.py 的 _ENV_NUM_BOUNDS）
#   2. 本文件（保存走界面时会校验区间，见 app/server.py 的 NUMERIC_BOUNDS）
#   3. 内置默认值
#
# ⚠ 界面上改不到的两项：`temperature` 与 `max_tokens` 自 2026-09-17 起不在设置页里，
#   只能改这个文件或环境变量。
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
#
# 模型是**一份列表**，每条自带请求地址与 Key —— 可以同时配智谱和 DeepSeek，
# 切换当前模型即生效，不用把 Key 重填一遍。生成参数（温度/重试/超时/预算）
# 是全局一份，跟连到哪家无关，留在下面的 `llm` 段里。

# models:
#   - id: m1
#     # 展示名；留空则显示下面的 model 名
#     name: 智谱 GLM-4.7
#     # 任意 OpenAI 兼容接口的根地址（不含 /chat/completions）
#     base_url: https://open.bigmodel.cn/api/paas/v4
#     # 在这里填你的 Key；留空则必须用环境变量 TALKSCRIPT_API_KEY
#     api_key: ""
#     model: glm-4.7
# active_model: m1

# llm:
#   temperature: 0.7
#   # 请求失败（网络/超时/429/5xx）自动重试次数；0 = 不重试
#   retries: 2
#   # 单次请求超时（秒）；长输出模型可调大
#   timeout: 180
#   # 单次输出预算，**只对撰写阶段生效**（选题/分镜固定 4000、单段重写固定 3000）。
#   # 推理型模型的「思考」token 也计入这里，给太小会导致 content 返回空串。
#   # ⚠ 但它不是越大越稳：实测思考量随这一项单调上涨（deepseek-v4-flash：
#   #   6000 → 思考 6.4~9.4k，16000 → 17.4~18.9k），调大只会更慢、
#   #   也更容易想满预算而正文为空。
#   #   频繁遇到空内容时，先降低回炉次数或换非推理档，而不是先调大这里。
#   max_tokens: 16000

# default_pack: elevator
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


# LLM 字段 ←→ 环境变量名。**只此一份**：`load_config` 既用它做覆盖，
# 也用它判断「这个值到底是用户给的，还是内置默认」。
# 抄成两份的话，加一个字段时很容易只改一处 —— 于是新字段的覆盖生效了、
# 但「它没配过」这个判断漏掉了（或反过来）。
_LLM_ENV = {
    "base_url": "TALKSCRIPT_BASE_URL",
    "model": "TALKSCRIPT_MODEL",
    "temperature": "TALKSCRIPT_TEMPERATURE",
    "retries": "TALKSCRIPT_RETRIES",
    "timeout": "TALKSCRIPT_TIMEOUT",
    "max_tokens": "TALKSCRIPT_MAX_TOKENS",
}
_LLM_CAST = {"temperature": float, "retries": int, "timeout": float, "max_tokens": int}


def _env(name: str, fallback: str = "") -> str:
    v = os.environ.get(name)
    return fallback if v is None or v == "" else v


def _env_num(name: str, fallback, cast):
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return fallback
    try:
        v = cast(raw)
    except (TypeError, ValueError):
        return fallback
    # P2-11：环境变量路径绕过了 server.set_config 的 NUMERIC_BOUNDS 那道闸，
    # TALKSCRIPT_MAX_TOKENS=100 / TALKSCRIPT_RETRIES=999 曾会静默生效
    # （每次生成必然空内容 / attempts=1000），所以这里夹逼 + 留日志。
    # ⚠ 键必须是**环境变量名**（调用点传进来的是它）——修复前表里写的是字段名
    # （max_tokens），`.get(name)` 恒为 None，夹逼从未执行过（实测
    # TALKSCRIPT_MAX_TOKENS=100 / RETRIES=999 / TIMEOUT=0 / TEMPERATURE=-5
    # 全部原样生效）。区间与 server.py 的 NUMERIC_BOUNDS 逐项一致。
    bounds = _ENV_NUM_BOUNDS.get(name)
    if bounds and not (bounds[0] <= v <= bounds[1]):
        log.warning("环境变量 %s=%r 超出合法区间 [%s, %s]，已夹逼为 %s",
                    name, raw, bounds[0], bounds[1],
                    max(bounds[0], min(bounds[1], v)))
        return max(bounds[0], min(bounds[1], v))
    return v


# 环境变量数值项的合法区间。**键是环境变量名**（`_env_num` 收到的就是它）。
# 区间与 `server.set_config` 的 NUMERIC_BOUNDS（app/server.py）**逐项一致**
# （retries 0~10、timeout 5~1800、max_tokens 256~200000、temperature 0.0~2.0）
# —— 环境变量夹逼与设置页保存是同一道闸，两端不许漂移：
# tests/test_env_bounds.py 把这张表与 server 的 NUMERIC_BOUNDS 比对钉住。
_ENV_NUM_BOUNDS: dict[str, tuple[float, float]] = {
    "TALKSCRIPT_TEMPERATURE": (0.0, 2.0),
    "TALKSCRIPT_RETRIES": (0, 10),
    "TALKSCRIPT_TIMEOUT": (5.0, 1800.0),
    "TALKSCRIPT_MAX_TOKENS": (256, 200000),
}


def _truthy(v) -> bool:
    return str(v or "").strip().lower() in ("1", "true", "yes", "on")


def _num(data: dict, key: str, default, cast, fallbacks: list | None = None):
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
        if fallbacks is not None:
            # 「写了但非法」与「没写」在下发侧必须同账（P3-4）：曾经只有
            # 「没写」进 llm_defaulted，非法值回退成默认后被 `/api/config`
            # 当成「用户配的」—— 兜底值冒充用户输入，只是换了个入口。
            fallbacks.append(key)
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


def _parse_models(data: dict, llm: dict) -> tuple[list[dict], str, bool]:
    """把文件解析成 `(原始条目列表, 当前模型 id, 是否由旧格式迁移而来)`。

    ⚠ **原始条目 = 文件里写的样子，空字段保持空**，不在这里落回默认值。
    写路径（增删改模型）必须基于这一份 —— 拿生效值（已经落回默认的）写回去，
    等于把「用户没配过 base_url」这个事实抹掉，界面上的「内置默认」小标会凭空消失。

    **迁移**：config.yaml 里没有 `models` 段时（老版本写的文件），用 `llm` 段里的
    连接信息造一条 —— 否则老用户一升级就看到「一个模型都没有」，
    而他们的 base_url / api_key 明明还在文件里，这是最典型的静默降级。
    迁移**只在读的时候发生、不写回文件**：读一次和读十次结果必须一样。

    ⚠ **只在 `llm` 段里真有配置时才迁移**（2026-09-17）。
    全新安装时 `llm` 段是空的（模板里全是注释），此时**不再造那条「内置默认」模型** ——
    用户原话：「没有内置默认的模型的，需要用户自己添加，添加完还需要支持删除」。
    预置一条 `glm-4.7` 的后果是：未配置状态与已配置状态长得一样，用户以为已经配好了
    （`MEMORY.md` 第一节第 ⑤ 种静默降级）。判据用「三个连接字段里有没有任何非空值」，
    与 `models_defaulted` 的逐字段口径一致。
    """
    raw = data.get("models")
    items: list[dict] = []
    if isinstance(raw, list) and raw:
        for i, it in enumerate(raw):
            if not isinstance(it, dict):
                continue          # 坏条目直接跳过，不让它把整份配置带崩
            items.append({
                "id": str(it.get("id") or f"m{i + 1}"),
                "name": str(it.get("name") or ""),
                "base_url": str(it.get("base_url") or ""),
                "api_key": str(it.get("api_key") or ""),
                "model": str(it.get("model") or ""),
            })
    migrated = not items
    if migrated:
        legacy = {k: str(llm.get(k) or "").strip() for k in ("base_url", "api_key", "model")}
        if any(legacy.values()):
            items = [{
                "id": DEFAULT_MODEL["id"],
                "name": "",
                "base_url": legacy["base_url"],
                "api_key": legacy["api_key"],
                "model": legacy["model"],
            }]

    # id 去重：重复的 id 会让「当前模型」指向哪一条变得不确定
    seen: set[str] = set()
    for it in items:
        while it["id"] in seen:
            it["id"] += "-2"
        seen.add(it["id"])

    active = str(data.get("active_model") or "")
    if items and active and active not in seen:
        # 指向了一条不存在的模型（手改文件 / 删掉了当前模型）→ 退回第一条。
        # 不能留一个悬空的 active：那会让生成时取不到任何连接信息。
        active = items[0]["id"]
    elif items and "active_model" not in data:
        # 键**不存在**（老文件 / 迁移产物）→ 用第一条。
        # ⚠ 与「键存在但值是空串」必须分开：后者是用户**显式取消启用**
        #（2026-09-17，界面上的开关要能关掉），要原样保留空串。
        # 混在一起的话「关掉开关」会被静默退回第一条 —— 关了又跳回来。
        active = items[0]["id"]
    elif not items:
        # 一条模型都没有：active 必须是空串，不能留一个指向不存在条目的悬空值。
        active = ""
    return items, active, migrated


def _effective(raw: dict) -> LLMModel:
    """原始条目 → 生效条目（空字段落回内置默认）。

    ⚠ **这里的兜底必须保留**（2026-09-17 试删过一次，回退了）。
    理由：老用户的 config.yaml 里常常只填了 api_key，base_url / model 是空的
    —— 他们一直靠这层兜底在用。删掉它等于把这些人**正在工作的配置改坏**
    （实测仓库根的 config.yaml 就是 `base_url: '' / model: '' / api_key: sk_...`）。
    「移除内置默认模型」的诉求在 `_parse_models` 那一层解决（不再**预置条目**），
    不是在这一层 —— 这一层管的是「条目里某个字段没填时用什么」，
    属于技术兜底，界面会照旧标「内置默认」（`models_defaulted` 逐字段判）。
    """
    return LLMModel(
        id=raw["id"],
        name=raw.get("name") or "",
        base_url=str(raw.get("base_url") or DEFAULT_MODEL["base_url"]).rstrip("/"),
        api_key=raw.get("api_key") or "",
        model=str(raw.get("model") or DEFAULT_MODEL["model"]),
    )


def load_raw_models(root: Path, config_dir: Path | None = None) -> tuple[list[dict], str]:
    """文件里的模型条目（**原样，空字段保持空**）+ 当前 id。写路径专用。

    单独开一个入口而不是从 `AppConfig` 里取，就是因为后者是「生效值」——
    两者混用会把空字段填实（见 `_parse_models` 的说明）。
    """
    data, _ = read_config_file(config_path(root, config_dir))
    llm = data.get("llm", {}) or {}
    if not isinstance(llm, dict):
        llm = {}
    raw, active, _ = _parse_models(data, llm)
    return raw, active


def load_config(root: Path, config_dir: Path | None = None) -> AppConfig:
    p = config_path(root, config_dir)
    data, config_error = read_config_file(p)

    llm = data.get("llm", {}) or {}
    if not isinstance(llm, dict):
        llm = {}

    raw_models, active, _ = _parse_models(data, llm)
    models = [_effective(r) for r in raw_models]
    # 一条模型都没有（全新安装，或用户把模型删光了）：给一个**空模型**占位。
    # 三个连接字段都是空串 —— 生成会被明确拒绝（见 server 的 api_key 检查），
    # 而不是在这里 `next(...)` 抛 StopIteration 把整个 load_config 带崩。
    cur = next((m for m in models if m.id == active), None) or LLMModel(
        id="", name="", base_url="", api_key="", model="")

    # 「这条模型里哪些字段还是内置默认」——**逐字段**看原始条目里写没写。
    # 先算全量（每条模型各一份），当前那条直接取用，不另算一遍：
    # 两处各判一次的话，改一处忘一处就会出现「列表里标着、状态行里没标」。
    models_defaulted = {
        r["id"]: [k for k in ("base_url", "model") if not str(r.get(k) or "").strip()]
        for r in raw_models
    }

    invalid_llm: list[str] = []     # 「写了但非法 → 回退默认」的键（P3-4）
    cfg = LLMConfig(
        base_url=cur.base_url,
        api_key=cur.api_key,
        model=cur.model,
        temperature=_num(llm, "temperature", 0.7, float, fallbacks=invalid_llm),
        retries=_num(llm, "retries", 2, int, fallbacks=invalid_llm),
        timeout=_num(llm, "timeout", 180.0, float, fallbacks=invalid_llm),
        max_tokens=_num(llm, "max_tokens", DEFAULT_CONFIG["llm"]["max_tokens"], int,
                        fallbacks=invalid_llm),
    )

    # 环境变量覆盖（避免密钥落盘）。
    # 顺序很重要：**先记「文件里没写」的字段，再让环境变量把它们从名单里划掉**。
    # 反过来的话就分不清「用户配的」和「内置默认」了 —— 覆盖之后两者长得一样。
    #
    # 生成参数住在 `llm` 段：文件里没写就算没配。
    defaulted = [k for k in _LLM_ENV
                 if k not in ("base_url", "model") and llm.get(k) in (None, "")]
    defaulted += invalid_llm
    # base_url / model 住在**模型条目**里，逐字段看它是不是空的 ——
    # 不能按「有没有 models 段」一刀切：用户只改了模型名、没动请求地址时，
    # 请求地址仍然是内置默认，一刀切会把默认值说成「你配的」。
    # 用 .get：一条模型都没有时 active 是空串，不在这个表里。
    defaulted += models_defaulted.get(active, [])
    cfg.api_key = _env("TALKSCRIPT_API_KEY", cfg.api_key)
    for key, env_name in _LLM_ENV.items():
        cur_v = getattr(cfg, key)
        raw = os.environ.get(env_name)
        if key in _LLM_CAST:
            setattr(cfg, key, _env_num(env_name, cur_v, _LLM_CAST[key]))
        else:
            setattr(cfg, key, _env(env_name, cur_v).rstrip("/"))
        # 「真的被覆盖了吗」必须看**值有没有被接受**，不能只看变量存不存在：
        # `TALKSCRIPT_RETRIES=abc` 会让 _env_num 静默退回内置默认，那还是「没配」——
        # 按「设过就划掉」处理，等于把这次静默兜底藏起来（本项目最忌讳的那种）。
        # 已知的轻微不精确：环境变量给的值**恰好等于**内置默认时，也算「没配」。
        # 那种情况下值与默认完全一致，说「这不是你配的」并不误导。
        if raw not in (None, "") and (key not in _LLM_CAST or getattr(cfg, key) != cur_v):
            if key in defaulted:
                defaulted.remove(key)

    app = AppConfig(
        llm=cfg,
        models=models,
        active_model=active,
        models_defaulted=models_defaulted,
        default_pack=_env("TALKSCRIPT_DEFAULT_PACK", str(data.get("default_pack", "elevator"))),
        root=root,
        config_error=config_error,
        llm_defaulted=defaulted,
    )
    app.mock = (_truthy(os.environ.get("TALKSCRIPT_MOCK"))
                or _truthy(llm.get("mock"))
                or cfg.api_key == "MOCK")
    return app


def public_models(cfg: AppConfig) -> list[dict]:
    """给渲染层的模型列表：**剥掉 api_key 明文**，只留一个 `api_key_set` 布尔。

    ⚠ 剥离口径**只此一份**。两处各剥一次的话，漏掉一处就是把密钥送到渲染层
    （渲染层是网页，密钥一旦进去就等于进了 DOM 与任何一段注入脚本）。
    """
    return [{
        "id": m.id,
        "name": m.name,
        "label": m.label,
        "base_url": m.base_url,
        "model": m.model,
        "api_key_set": bool(m.api_key),
        "active": m.id == cfg.active_model,
        # 这条模型里哪些字段还是内置默认（文件里没写）。逐条下发，界面才能
        # 把「还没配过」标在**每一行**上，而不是只标当前那条。
        "defaulted": list(cfg.models_defaulted.get(m.id, [])),
    } for m in cfg.models]


def save_models(root: Path, models: list[dict], active_model: str | None = None,
                config_dir: Path | None = None) -> None:
    """把模型条目（**原始形态，空字段保持空**）写进 config.yaml，其它键原样保留。

    顺手**清掉 `llm` 段里的 base_url / api_key / model**：连接信息已经搬到
    `models` 段，留着会让文件里出现「同一个 Key 两份」的假象，而且那份是**被忽略的**
    —— 用户改了它却不生效，正是本项目一直在整治的那种看不见的失效。
    """
    p = config_path(root, config_dir)
    data, _ = read_config_file(p)
    data["models"] = models
    # ⚠ 判据是 `is not None` 而不是真值：`""` 是**有意义的取值**（用户把模型
    # 删光了 / active 悬空 → 写空串），不能当成「没传，别动」。
    # 用真值判断的话，删光模型后文件里会留着一个指向不存在条目的旧 active_model。
    if active_model is not None:
        data["active_model"] = active_model
    llm = data.get("llm")
    if isinstance(llm, dict):
        for k in ("base_url", "api_key", "model"):
            llm.pop(k, None)
    write_atomic(p, yaml.safe_dump(data, allow_unicode=True, sort_keys=False))


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
