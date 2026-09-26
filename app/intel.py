# -*- coding: utf-8 -*-
"""行业情报：**源注册表** + 抓取 + 落点 + 只读读取。

为什么是这个形状
----------------
需求方案 §2.2 定的是「源注册表」模式（对标 DailyHotApi / NewsNow）：每个平台一个
adapter，统一 `{title,url,hot,desc}` 结构，**加一个源不改主流程**。所以：

  · `pack.yaml` 的 `intel_sources` 声明**这个行业要盯哪些源**，每个源自己在界面上
    叫什么名字（`label` / `platform` / `role` / `cadence`）—— **渲染层一个平台名
    都不写死**，加行业包只改配置；
  · `ADAPTERS` 是引擎侧的适配器表（`id` → 函数），**引擎按声明跑**；
  · 声明了但引擎没有这个 `id`（或 `enabled: false`）→ 不报错，如实标出来
    （「未接入 N」），与「用户能选但本包没定制」的降级口径一致。

落点：`data_dir/intel/<pack>/`（B2）。**不进安装包、不入库**（`.gitignore` 里
`intel/` 已排除）—— 这是"这周发生了什么"的运营数据，不是"这个内行怎么想"的
行业知识；每台机器自己抓、天天在变，随包分发没有意义。

无网络 / 无 Key / 无数据三种情况都有明确退路（§2.3 差异化第 5 条）
----------------------------------------------------------------
  1. **无网络**：每个源各自 try，失败只记进 `errors[源 id]`，**不影响其它源**，
     也**不影响生成**（抓取器在生成主链路之外）。界面把失败的源标出来。
  2. **无 Key**：本模块**不调模型**（见下面的 B-2）。所以"没配 Key"这件事
     对抓取完全无影响 —— 首装用户打开选题页看到的是一页**真实条目**，
     不是空白，也不是"请先配置模型"。
  3. **无数据**：`load_latest()` 对"文件不存在""文件坏了""目录没有"一律返回
     空结构而**不抛**（B3）。⚠ 这与 `Pack.private_facts()` 的失败语义**相反**
     （那边读不到要中止生成）—— 差别是故意的：私有资料缺失会让模型编事实，
     情报缺失只是"今天没选题可看"。**别顺手统一这两处。**

B-2：LLM 不参与相关性判定
------------------------
`需求方案 §2.9 B-2` 把 LLM 从"相关性判定"里彻底拿掉，只留在"角度建议"。两条依据：
实测 10 个种子扩出 102 条真实问句、噪声 0，说明词族 + `topics_map` 两档就够；
而 LLM 判定不可复现，还会**改选题缓存指纹**（`_plan_key = sha1(base_url+model+system+user)`），
system/user 里任何抖动都会让同参数重选题白花一次 4000 token 调用。
所以本模块的归并是**确定性的**（词族 + `topics_map` + segment 选项），
`angle_hint` 字段留空由调用方按需填 —— 本文件里**没有一次模型调用**。
"""
from __future__ import annotations

import csv
import io
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .fileio import write_atomic

log = logging.getLogger(__name__)

#: 抓取节奏 → 多少天算"该补抓了"。`on_demand` / `—` / 没写 = 不参与懒触发。
CADENCE_DAYS: dict[str, int] = {"daily": 1, "weekly": 7, "monthly": 30, "quarterly": 90}

#: 每个话题（源）最多保留多少条 —— 防刷屏（TrendRadar 的做法）。
PER_SOURCE_CAP = 60

#: history 目录最多留多少份快照。抓一次落一份（文件名是秒级时间戳），没有
#: 上限时这个目录跟着抓取次数无界增长：每天 4 个源各抓一次 ≈ 每包每年上千个
#: 文件，而它唯一的读者是"和上周比有没有新题"（is_new 只看 latest 与 prev_keys，
#: 根本不翻 history）—— 所以留个能回看的小窗口就够，其余删掉。
#: 删除只动 `*.json` 且按文件名排序：时间戳格式定长，字典序即时间序。
HISTORY_KEEP = 30

#: 单条标题/描述的长度上限。抓来的文本直接进界面，不截断会撑爆布局，
#: 也给了上游一个"往标题里塞一篇文章"的机会。
TITLE_MAX, DESC_MAX = 120, 300

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
# `_FACT_RE`（判断"这条有没有事实可写"的粗糙代理）已随 P2-21 删除：
# 它算出来的 `fact_density` 全仓库无消费方，却随 latest.json 外发。
# 若将来真要落地「占位密度」这条信号，**必须先接上消费方**再把它加回来 ——
# 只算不显示的话，它只会让人误以为这些数字参与过判定。


# ── 源声明 ────────────────────────────────────────────────────
@dataclass(frozen=True)
class IntelSource:
    """`pack.yaml` 里的一条 `intel_sources`。

    字段名与语义逐条对到需求方案 §2.2 的 YAML 示例：`label` 是**界面上显示什么**、
    `platform` 是组标题第二半、`role` 是分区标题里的「· 角色」、`cadence` 是抓取节奏、
    `note` 是分区标题后那句 hint（可省）。
    """

    id: str
    label: str
    platform: str = ""
    role: str = ""
    cadence: str = ""
    note: str = ""
    params: dict = field(default_factory=dict)
    enabled: bool = True
    #: 引擎里到底有没有这个适配器。**是字段而不是 property**：
    #: `/api/meta` 下发的是 `model_dump()`，服务端要把下发形状**原样还原**成
    #: 这个 dataclass（`IntelSource(**model_dump())`）—— 做成 property 的话
    #: `wired` 不在构造参数里，那条还原路径会 `TypeError` 打成 HTTP 500。
    #: 由 `parse_sources` 按 `ADAPTERS` 填，不从 YAML 读。
    wired: bool = False

    @property
    def usable(self) -> bool:
        return self.enabled and self.wired

    @property
    def cadence_days(self) -> int | None:
        return CADENCE_DAYS.get(str(self.cadence).strip().lower())

    def as_dict(self) -> dict:
        """下发形状（`/api/meta` 的 `packs[].intel_sources[]`）。

        `wired` 一起下发是**刻意的**：界面要能区分「已接入」「声明了但引擎没这个
        适配器」「已声明但主动关掉」三种状态 —— 只给 `enabled` 的话后两种长得
        一模一样，而它们的处理完全不同（前者是 bug，后者是决定）。
        """
        return {"id": self.id, "label": self.label, "platform": self.platform,
                "role": self.role, "cadence": self.cadence, "note": self.note,
                "params": dict(self.params), "enabled": self.enabled,
                "wired": self.wired}


def parse_sources(raw) -> tuple[list[IntelSource], list[str]]:
    """把 `pack.yaml` 的 `intel_sources` 解析成 `IntelSource` 列表 + 给包作者的说明。

    **坏在结构上就只丢那一条，绝不抛**（与 `resolve_ai_tells` 同一取向）：
    一份源声明写坏不该让整个包不可用 —— 情报是锦上添花，不是生成的前提。
    但**必须说出来**：`notes` 会被 `pack_info` 摊到界面上（`param_audit` 的
    `intel_sources` 一条），否则"我以为配了、实际没跑"就是静默降级。
    """
    notes: list[str] = []
    out: list[IntelSource] = []
    if raw in (None, "", []):
        return out, notes
    if not isinstance(raw, list):
        return out, [f"intel_sources 需要列表，现在是 {type(raw).__name__} —— 本包没有情报源"]
    seen: set[str] = set()
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            notes.append(f"intel_sources[{i}] 需要映射，现在是 {type(item).__name__} —— 已忽略")
            continue
        sid = str(item.get("id") or "").strip()
        if not sid:
            notes.append(f"intel_sources[{i}] 没有 id —— 已忽略（引擎按 id 找适配器）")
            continue
        # 同一个 id 声明两次：`hot_board` 是**故意**的用法（一个适配器按 `params.board`
        # 出多个源，见 §2.2 的示例），所以重复 id **不是错**，但要能区分开 ——
        # 界面上的分组按 `label` 走，`_key` 才是唯一键。
        #
        # ⚠ 只有 `enabled: true` 且引擎没有适配器时才算问题：`enabled: false` +
        # 没有适配器是**明确决定**（§2.2 的 `xhs_board` 就是"不做逆向，所以没有"），
        # 把它也记成 note 会让审计变成背景噪音（`test_pack_info_is_clean_when_
        # everything_is_fine` 那条规矩）。真问题只有一个形态：**作者以为它在跑**。
        if sid not in ADAPTERS and item.get("enabled", True) is not False:
            notes.append(f"intel_sources 里的 id={sid!r} 引擎没有对应适配器 —— "
                         f"这个源不会抓（已知适配器：{'、'.join(ADAPTERS)}）。"
                         f"确实不做的源请写 `enabled: false` 并加一句 note 说明原因")
        label = str(item.get("label") or sid).strip()
        if label in seen:
            notes.append(f"intel_sources 里 label={label!r} 重复 —— 界面上两个源会同名")
        seen.add(label)
        enabled = item.get("enabled", True)
        if not isinstance(enabled, bool):
            notes.append(f"intel_sources[{label}] 的 enabled 需要 true/false，"
                         f"现在是 {type(enabled).__name__} —— 按 true 处理")
            enabled = True
        cadence = item.get("cadence", "")
        params = item.get("params") or {}
        if not isinstance(params, dict):
            notes.append(f"intel_sources[{label}] 的 params 需要映射，"
                         f"现在是 {type(params).__name__} —— 已按空处理")
            params = {}
        out.append(IntelSource(
            id=sid, label=label, platform=str(item.get("platform") or "").strip(),
            role=str(item.get("role") or "").strip(), cadence=str(cadence or "").strip(),
            note=str(item.get("note") or "").strip(), params=params, enabled=enabled,
            wired=sid in ADAPTERS))
    return out, notes


# ── 适配器（源注册表）──────────────────────────────────────────
@dataclass
class FetchCtx:
    """适配器的运行环境。**http 可注入** —— 这是全模块唯一碰网络的地方，
    测试注入一个假 getter 就能把整条链路（去重/上限/落盘/读取/打分）跑成离线可测。"""

    pack: str
    seeds: list[str] = field(default_factory=list)   # 需求词种子（细分领域选项等）
    keywords: list[str] = field(default_factory=list)  # 命中过滤词（行业词）
    manual_dir: Path | None = None
    http: Callable[..., object] | None = None

    def get_json(self, url: str, **kw):
        if self.http is None:
            return _http_json(url, **kw)
        return self.http(url, **kw)


def _http_json(url: str, params: dict | None = None, timeout: float = 12.0):
    """真实网络取 JSON。**只在这里 import httpx** —— 离线/测试路径不碰它。"""
    import httpx
    with httpx.Client(timeout=timeout, follow_redirects=True,
                      headers={"User-Agent": UA, "Referer": url}) as c:
        r = c.get(url, params=params)
        r.raise_for_status()
        return r.json()


def _row(title, url="", guid="", hot=None, published="", desc="", **extra) -> dict:
    """统一条目结构（DailyHotApi 的 `{title,url,hotValue,desc}` 同形）。"""
    return {"title": str(title or "").strip()[:TITLE_MAX],
            "url": str(url or "").strip(),
            "guid": str(guid or "").strip(),
            "hot": hot, "published": str(published or "").strip(),
            "desc": str(desc or "").strip()[:DESC_MAX], **extra}


def fetch_demand_terms(spec: IntelSource, ctx: FetchCtx) -> list[dict]:
    """下拉词（雷达）：百度 `sugrec` 的搜索联想词 diff。

    §1.2 实测结论：**下拉词才是雷达** —— 10 个种子扩散出 102 条真实问句、噪声 0，
    而且比政策文件**滞后出现**（那才是"正在办、正卡壳"的事）。
    """
    seeds = _str_list(spec.params.get("seeds")) or ctx.seeds
    rows: list[dict] = []
    for seed in seeds[:20]:
        data = ctx.get_json("https://www.baidu.com/sugrec",
                            params={"prod": "pc", "wd": str(seed)})
        for g in (data or {}).get("g") or []:
            q = str(g.get("q") or "").strip()
            if not q:
                continue
            rows.append(_row(q, url=f"https://www.baidu.com/s?wd={q}",
                             guid=f"baidu-sug:{q}", seed=str(seed)))
    return rows


def fetch_bilibili_search(spec: IntelSource, ctx: FetchCtx) -> list[dict]:
    """B站同题（供给度量）：条数越多说明这题越红海。

    §1.2 实测：B站 `search/all/v2` 公开返回播放/点赞/发布日期。
    ⚠ 该端点对部分出口 IP 要求 `buvid3` cookie，拿不到就抛 —— 由 `fetch_pack`
    记进 `errors` 并把该源的 `supply` 落成 null（**"算不出"而不是 0**，
    见 `score_topics`）。
    """
    kws = _str_list(spec.params.get("keywords")) or ctx.keywords
    rows: list[dict] = []
    for kw in kws[:10]:
        data = ctx.get_json("https://api.bilibili.com/x/web-interface/search/all/v2",
                            params={"keyword": str(kw), "page": 1})
        for blk in ((data or {}).get("data") or {}).get("result") or []:
            for it in (blk or {}).get("data") or []:
                title = re.sub(r"<[^>]+>", "", str(it.get("title") or ""))
                rows.append(_row(title, url=it.get("arcurl") or "",
                                 guid=f"bili:{it.get('bvid') or it.get('aid') or title}",
                                 hot=it.get("play"),
                                 published=str(it.get("pubdate") or ""),
                                 desc=it.get("description") or "",
                                 keyword=str(kw)))
    return rows


#: 各平台的公开热榜端点（§2.2：`hot_board` **一个适配器按 `params.board` 出多个源**）。
#: ⚠ 泛热榜对垂直行业覆盖接近零（实测三平台 130 条命中 0），所以这个源的价值不在
#: "给我一堆热点"，而在 `match: keyword` 时当**破圈触发器** —— 行业词偶尔真上了热榜，
#: 那是一条强信号。命中 0 是**正常结果**，不是故障。
_BOARDS: dict[str, tuple[str, str]] = {
    "douyin": ("https://www.iesdouyin.com/web/api/v2/hotsearch/billboard/word/", "douyin"),
    "toutiao": ("https://www.toutiao.com/hot-event/hot-board/?origin=toutiao_pc", "toutiao"),
    "zhihu": ("https://www.zhihu.com/api/v3/feed/topstory/hot-lists/total", "zhihu"),
}


def fetch_hot_board(spec: IntelSource, ctx: FetchCtx) -> list[dict]:
    board = str(spec.params.get("board") or "douyin").strip().lower()
    if board not in _BOARDS:
        raise ValueError(f"未知的 board={board!r}（可用：{'、'.join(_BOARDS)}）")
    url, plat = _BOARDS[board]
    data = ctx.get_json(url, params={"limit": 50} if board == "zhihu" else None)
    rows: list[dict] = []
    if board == "douyin":
        for it in (data or {}).get("word_list") or []:
            rows.append(_row(it.get("word"), guid=f"dy:{it.get('word')}",
                             hot=it.get("hot_value"), url=url, board=board))
    elif board == "toutiao":
        for it in (data or {}).get("data") or []:
            rows.append(_row(it.get("Title"), url=it.get("Url") or "",
                             guid=f"tt:{it.get('ClusterId') or it.get('Title')}",
                             hot=it.get("HotValue"), board=board))
    else:
        for it in (data or {}).get("data") or []:
            tgt = it.get("target") or {}
            rows.append(_row(tgt.get("title"), url=tgt.get("url") or "",
                             guid=f"zh:{tgt.get('id') or tgt.get('title')}",
                             hot=(tgt.get("metrics") or {}).get("hot"),
                             board=board))
    if str(spec.params.get("match") or "").lower() == "keyword":
        words = _str_list(spec.params.get("keywords")) or ctx.keywords
        rows = [r for r in rows if any(w and w in r["title"] for w in words)]
    return rows


def fetch_policy_library(spec: IntelSource, ctx: FetchCtx) -> list[dict]:
    """政策库（口径库，不是雷达）：国务院政策文件库按标题检索。

    §1.2 实测：**政策库不是雷达是口径库**（存量 10 份、最新一份距今 466 天）。
    它的价值是"引用有出处"（带文号进 prompt），所以节奏配 `quarterly` 就够。
    ⚠ 已查实的坑（`intel/pull_policy.py`）：`t=zhengcelibrary_all` 返回空，
    必须 `gw`（国务院）与 `bm`（部门）分开查；`searchfield=content` 会跨行业污染，
    只有 **title 级命中**可用。
    """
    kws = _str_list(spec.params.get("keywords")) or ctx.keywords
    rows: list[dict] = []
    for kw in kws[:8]:
        for tab in ("zhengcelibrary_gw", "zhengcelibrary_bm"):
            data = ctx.get_json("https://sousuo.www.gov.cn/search-gov/data",
                                params={"t": tab, "q": str(kw), "timetype": "timeqb",
                                        "sort": "pubtime", "sortType": 1,
                                        "searchfield": "title", "p": 1, "n": 30})
            sv = (data or {}).get("searchVO") or {}
            lists = [v.get("listVO") or [] for v in (sv.get("catMap") or {}).values()
                     if isinstance(v, dict)]
            for it in (sv.get("listVO") or []) + [x for ls in lists for x in ls]:
                title = re.sub(r"<[^>]+>", "", str(it.get("title") or "")).strip()
                url = str(it.get("url") or "").replace("http://", "https://")
                if not title or not url:
                    continue
                rows.append(_row(title, url=url, guid=url,
                                 published=str(it.get("pubtimeStr") or it.get("time") or ""),
                                 desc=it.get("puborg") or "", keyword=str(kw)))
    return rows


def fetch_manual_import(spec: IntelSource, ctx: FetchCtx) -> list[dict]:
    """人工导入（B-4）：把"拿不到的那个平台"从限制变成动作。

    这个行业最对口的平台恰恰是拿不到的那个（小红书要 `x-s` 签名，不做逆向），
    所以首版就给**人工导入路径**：用户贴链接或导 CSV，落
    `data_dir/intel/<pack>/manual/`，与自动源**同一 schema** —— 于是
    "未接入"不再是死路，而是"你贴进来我就用"。

    支持 `*.json`（条目数组）与 `*.csv`（表头 `title,url,desc,published,hot`）。
    文件坏了只记一条日志并跳过那一个文件，不牵连其它文件、不牵连其它源。
    """
    d = ctx.manual_dir
    rows: list[dict] = []
    if not d or not d.exists():
        return rows
    for f in sorted(d.iterdir()):
        if not f.is_file():
            continue
        try:
            if f.suffix.lower() == ".json":
                data = json.loads(f.read_text(encoding="utf-8"))
                for it in (data if isinstance(data, list) else data.get("items") or []):
                    if isinstance(it, dict) and it.get("title"):
                        rows.append(_row(it.get("title"), url=it.get("url") or "",
                                         guid=it.get("guid") or it.get("url") or "",
                                         hot=it.get("hot"), published=it.get("published") or "",
                                         desc=it.get("desc") or "", manual=f.name))
            elif f.suffix.lower() == ".csv":
                text = f.read_text(encoding="utf-8-sig")
                for it in csv.DictReader(io.StringIO(text)):
                    if (it.get("title") or "").strip():
                        rows.append(_row(it["title"], url=it.get("url") or "",
                                         guid=it.get("url") or "", hot=it.get("hot"),
                                         published=it.get("published") or "",
                                         desc=it.get("desc") or "", manual=f.name))
        except Exception as e:                     # noqa: BLE001
            log.warning("人工导入文件读不出来，已跳过：%s —— %s", f, e)
    return rows


#: **源注册表**：`id` → 适配器。加一个源只改这里 + `pack.yaml`，不改主流程。
ADAPTERS: dict[str, Callable[[IntelSource, FetchCtx], list[dict]]] = {
    "demand_terms": fetch_demand_terms,
    "bilibili_search": fetch_bilibili_search,
    "hot_board": fetch_hot_board,
    "policy_library": fetch_policy_library,
    "manual_import": fetch_manual_import,
}


# ── 落点 ──────────────────────────────────────────────────────
def intel_dir(data_dir: Path, pack: str) -> Path:
    return Path(data_dir) / "intel" / pack


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _str_list(value) -> list[str]:
    """把 params 里的 seeds/keywords 归一成**词列表**。

    `list("电梯维保")` 会拆成 4 个单字 —— 曾经拿单字去发联想词请求，
    静默产生一整页无意义条目。字符串当**一个**词，列表原样，其余空。
    """
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, (list, tuple)):
        return [v for v in value if isinstance(v, str) and v.strip()]
    return []


def item_key(it: dict) -> str:
    """一条情报的**身份键**：池子去重、忽略记录、`today()` 的忽略过滤都用它。

    **只此一份** —— 渲染层从 `/api/intel/today` 的每条 `item.key` 直接取这个值，
    不再自己拼一份。曾经前端拼 `guid || url || title:…`，而 `today()` 的忽略过滤
    用的是 `guid || title`（**漏了 url 那一档**）—— 于是「只有 url、没有 guid」的
    条目（政策库那类）点「忽略」之后**下一轮原样回来**：用户看到的是"忽略没用"，
    而"两处规则不一致"这件事在界面上找不到任何线索。

    为什么 guid 优先：同一个链接在两次抓取里可能带上不同的跟踪参数（`?utm=…`），
    而 guid 是平台自己给的稳定标识。反过来，只有 url 的源（政策库）就退到 url。
    """
    return str(it.get("guid") or it.get("url") or f"title:{it.get('title')}")


def dedup(items: list[dict]) -> list[dict]:
    """去重：键用 `item_key`（`guid > url > title`）—— 与忽略记录**同一个键**，
    否则"同一份数据在两处被认成两条"会以"忽略没用"的形式冒出来。
    """
    out: list[dict] = []
    seen: set[str] = set()
    for it in items:
        key = item_key(it)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out


def _match_segment(text: str, mapping: dict, options: list) -> str:
    """把一条条目归到某个细分领域（B-2：**确定性归并**，不调模型）。

    两档，顺序固定：先查 `topics_map` 的映射词（行业自己的口径），
    再退回 segment 选项字面命中。都不中 → 空串（"没归上"而不是硬塞，
    与 BERTopic「outlier 不硬塞进主题」同一取向）。
    """
    for seg in options:
        kw = str(mapping.get(str(seg)) or "")
        if kw and kw in text:
            return str(seg)
    for seg in options:
        if str(seg) and str(seg) in text:
            return str(seg)
    return ""


#: 事件衰减的时间常数（天）—— `E = exp(−Δ天 / τ)`，按**来源角色**取。
#: 需求方案 §2.2 给的是"按类型：事故 3 / 政策 30 / 季节话题 90"，落到这里就是
#: 角色 → τ：破圈触发器（热榜上的事，几小时就凉）最短、口径库/数据源最长。
TAU_BY_ROLE: dict[str, float] = {
    "破圈触发器": 3.0, "供给度量": 3.0, "雷达": 14.0,
    "数据源": 30.0, "口径库": 30.0,
}
TAU_DEFAULT = 30.0

#: 一级来源 `demand_terms` 的适配器名 —— 判断"这条是不是需求词"要认它。
DEMAND_SOURCE_ID = "demand_terms"
SUPPLY_SOURCE_ID = "bilibili_search"


def _days_ago(published, now: datetime) -> float | None:
    """把各种 `published` 写法解析成"多少天前"；解析不出返回 None（= 算不出）。

    三种写法都得认（各源的字段天生不同，统一是这一层的事）：
      · epoch 秒（B站的 `pubdate` 就是它）；
      · `YYYY-MM-DD` / `YYYY-MM-DDTHH:MM:SS`（政策库、人工导入）；
      · 空串 / 乱码 → None。**不是 0** —— "不知道多久前"与"就是今天"结论相反。
    """
    s = str(published or "").strip()
    if not s:
        return None
    if s.isdigit() and len(s) >= 9:                # epoch 秒
        try:
            t = datetime.fromtimestamp(int(s), tz=now.tzinfo)
        except (OverflowError, OSError, ValueError):
            return None
    else:
        try:
            t = datetime.fromisoformat(s.replace("Z", "+00:00")[:19])
        except ValueError:
            return None
        if t.tzinfo is None:
            t = t.replace(tzinfo=now.tzinfo)
    return max(0.0, (now - t).total_seconds() / 86400.0)


def score_topics(items: list[dict], prev_keys: set[str], *, now: datetime | None = None) -> dict:
    """算 D / S / E / 机会分 —— **只用"现在就算得出来"的项**（§2.9 B-1）。

    B-1 的原话：「首版公式只留『现在就算得出来』的项。D 的搜索增长要 28 天基线、
    S 的分母要累计条数 —— **新装用户两样都没有**，写进首版就是界面上一列永远
    算不出的数。**公式可算性优先于公式完整。**」

    首版口径（**逐条**，全部是相对值，界面必须标「相对值」）：

        D（需求） = 该细分领域本次「新收录」条目数 ÷ 本次各领域里最大的那个
        S（供给） = 该细分领域 B站同题条数     ÷ 本次各领域里最大的那个
        E（事件） = exp(−Δ天 / τ)，τ 按来源角色（TAU_BY_ROLE）
        机会分    = D × (1 − S)      ← 蝉妈妈「高互动低竞争」的可计算版

    ⚠ **D/S 是"细分领域"粒度，不是"单条"粒度**，这是刻意的：单条需求词的
    "新收录"只有 0/1 两态，硬做成 0..1 是**假精度**（界面上的 0.92 会让人以为
    有个它其实没有的刻度）。领域粒度至少真的在比较。单条粒度等 A4 校准后再说。

    两种"没有"必须长得不一样（本项目的老规矩）：
      · 本次一条新收录都没有（第二次抓取没有新词）→ `D = None`
      · 本包没配 B站源 / 该源失败 → `S = None`
      · D 或 S 为 None → `机会分 = None`，界面显示「—」而**不是 0**。
        0 是"竞争激烈、没机会"，None 是"没数据"，两者结论相反。
    """
    now = now or datetime.now(timezone.utc).astimezone()
    segs = {(it.get("segment") or "") for it in items}
    new_n = {s: len([it for it in items
                     if (it.get("segment") or "") == s
                     and item_key(it) not in prev_keys])
             for s in segs}
    sup_n = {s: len([it for it in items
                     if (it.get("segment") or "") == s
                     and it.get("source_id") == SUPPLY_SOURCE_ID])
             for s in segs}
    tot_n = {s: len([it for it in items if (it.get("segment") or "") == s]) for s in segs}
    max_new, max_sup = max(new_n.values(), default=0), max(sup_n.values(), default=0)

    out: dict[str, dict] = {}
    for s in segs:
        D = round(new_n[s] / max_new, 3) if max_new else None
        S = round(sup_n[s] / max_sup, 3) if max_sup else None
        # ⚠ **不写没人消费的字段**（P2-21）：`demand_new` / `supply_n` / `total_n`
        #   是算 D/S 用的**中间量**，`fact_density`（原注释称它是"两条线的共用
        #   信号"）与 `days_ago`（算 E 用的中间量）同样 —— 它们**全仓库无消费方**
        #   （前端只读 D/S/E/opportunity），却随 `latest.json` 一起外发。
        #   信号算了没人接 = 白写；更糟的是它会让人误以为这些数字参与过判定。
        #   要显示就接上消费方，不显示就别写。
        out[s] = {
            "D": D, "S": S,
            "opportunity": round(D * (1 - S) * 100) if (D is not None and S is not None) else None,
        }
    for it in items:
        seg = it.get("segment") or ""
        row = dict(out.get(seg) or {})
        tau = TAU_BY_ROLE.get(str(it.get("role") or ""), TAU_DEFAULT)
        days = _days_ago(it.get("published"), now)
        row["E"] = round(2.718281828 ** (-days / tau), 3) if days is not None else None
        row["is_new"] = item_key(it) not in prev_keys
        it["score"] = row
    return out


def _flags(it: dict) -> list[list[str]]:
    """给界面用的标记（`.m-chip` 的 good / bad / warn 三档）。

    这些是**界面上的结论**，所以必须能从分数本身推出来，不能另有一套口径 ——
    否则"机会分 82 但没有标记"这种自相矛盾的卡会出现。
    """
    sc = it.get("score") or {}
    out: list[list[str]] = []
    opp, S = sc.get("opportunity"), sc.get("S")
    if opp is None:
        out.append(["warn", "数据不足 · 算不出机会分"])
    elif opp >= 60:
        out.append(["good", "机会分高"])
    elif S is not None and S >= 0.7:
        out.append(["bad", "红海 · 需换角度"])
    if sc.get("is_new"):
        out.append(["good", "本周新出"])
    return out


def fetch_pack(pack: str, data_dir: Path, sources: list[IntelSource], *,
               seeds: list[str] | None = None, keywords: list[str] | None = None,
               topics_map: dict | None = None, segment_options: list | None = None,
               http=None) -> dict:
    """跑一遍所有**可用**的源，去重、限量、归并、打分、落盘。**不抛**。

    返回 `{"fetched_at", "sources": [...], "items": [...], "errors": {...}}`。
    每个源独立 try —— 一个源挂了不影响别的源，也不影响生成链路
    （抓取器在生成主链路之外，§2.2 工程约束第一条）。
    """
    d = intel_dir(data_dir, pack)
    ctx = FetchCtx(pack=pack, seeds=list(seeds or []), keywords=list(keywords or []),
                   manual_dir=d / "manual", http=http)
    prev = load_latest(data_dir, pack)
    # is_new 用**同一个** item_key（guid > url > title:…）：曾经这里是
    # `guid or title`（无 url 档），只有 url 的条目标题一变就误报「本周新出」，
    # 而去重/忽略认它还是同一条 —— 一库两键。prev_keys 每次从盘上重算，
    # 换键无迁移问题（旧数据按新键式现算，is_new 最多抖一轮）。
    prev_keys = {item_key(it) for it in prev.get("items", [])}

    items: list[dict] = []
    errors: dict[str, str] = {}
    per_source: list[dict] = []
    for spec in sources:
        if not spec.enabled:
            per_source.append({**_source_row(spec), "count": 0, "error": "",
                               "state": "off"})
            continue
        if not spec.wired:
            per_source.append({**_source_row(spec), "count": 0,
                               "error": f"引擎没有 id={spec.id!r} 的适配器", "state": "unwired"})
            continue
        try:
            rows = ADAPTERS[spec.id](spec, ctx) or []
        except Exception as e:                     # noqa: BLE001
            # 失败**不是**异常出口：记一条人话原因，其它源照跑，界面标出来。
            log.info("情报源 %s(%s) 抓取失败：%s", spec.label, spec.id, e)
            errors[spec.id] = f"{type(e).__name__}: {e}"
            per_source.append({**_source_row(spec), "count": 0,
                               "error": errors[spec.id], "state": "error"})
            continue
        for r in rows:
            r["source_id"] = spec.id
            r["source_label"] = spec.label
            r["platform"] = spec.platform
            r["role"] = spec.role
            r["segment"] = _match_segment(
                f"{r.get('title', '')} {r.get('desc', '')} {r.get('keyword', '')}",
                topics_map or {}, segment_options or [])
        # 每话题上限（防刷屏）：**在去重之后、合并之前**按源各切一刀。
        rows = dedup(rows)[:PER_SOURCE_CAP]
        items += rows
        per_source.append({**_source_row(spec), "count": len(rows), "error": "",
                           "state": "ok"})

    items = dedup(items)
    # 打分**逐条**写进 `item["score"]`（原型里每张选题卡就带 D/S/E 三根信号条）。
    score_topics(items, prev_keys)
    for it in items:
        it["flags"] = _flags(it)
        it["ev"] = ("本周新出" if (it.get("score") or {}).get("is_new")
                    else "存量话题")

    out = {"pack": pack, "fetched_at": _now(), "sources": per_source,
           "items": items, "errors": errors}
    try:
        d.mkdir(parents=True, exist_ok=True)
        # 本模块三处落盘（latest / history 快照 / ignored）都走 write_atomic：
        # `latest.json` 写一半被强杀，下次打开选题页 load_latest 解析失败 →
        # 界面显示"情报文件读不出来"，而用户刚从上面看到"抓到了 N 条"。
        # 先写临时文件再 replace，任何时刻目标要么旧完整版、要么新完整版。
        write_atomic(d / "latest.json", json.dumps(out, ensure_ascii=False, indent=1))
        hist = d / "history"
        hist.mkdir(exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        write_atomic(hist / f"{stamp}.json",
                     json.dumps({"fetched_at": out["fetched_at"], "items": items},
                                ensure_ascii=False))
        _prune_history(hist)
    except OSError as e:
        # 落盘失败要进 errors —— 否则界面显示"抓到了 N 条"而下次打开又是空的。
        errors["_save"] = f"落盘失败：{e}"
        out["errors"] = errors
    return out


def _prune_history(hist: Path) -> None:
    """history 只留最近 HISTORY_KEEP 份，其余删掉（见 HISTORY_KEEP 的注释）。

    清理失败**不抛**：这是收尾动作，为它废掉一次成功的抓取（连同界面上
    刚显示的 N 条）本末倒置。写个 warning 就够了。
    """
    try:
        # 切片 `[:-HISTORY_KEEP]` 在不足 HISTORY_KEEP 份时天然是空列表，不用判长度。
        for old in sorted(hist.glob("*.json"))[:-HISTORY_KEEP]:
            old.unlink(missing_ok=True)
    except OSError as e:
        log.warning("情报历史清理失败：%s —— %s", hist, e)


def _source_row(spec: IntelSource) -> dict:
    return {"id": spec.id, "label": spec.label, "platform": spec.platform,
            "role": spec.role, "cadence": spec.cadence, "note": spec.note,
            "enabled": spec.enabled, "wired": spec.wired}


def load_latest(data_dir: Path, pack: str) -> dict:
    """读最近一次抓取结果。**空 / 坏 / 没有一律返回空结构，绝不抛**（B3）。

    ⚠ 与 `Pack.private_facts()` 的失败语义**相反**，这是故意的：
    私有资料读不到必须中止生成（否则模型会编事实），而情报读不到只是
    "今天没有选题可看"。**别顺手把两处统一** —— 那是把一条安全性质
    换成另一条的典型做法。

    `errors` 里带上读失败的原因，界面才能把"没抓过"与"抓了但文件坏了"
    分开显示 —— 这两种"没有"在界面上必须是两种样子。
    """
    d = intel_dir(data_dir, pack)
    f = d / "latest.json"
    empty = {"pack": pack, "fetched_at": "", "sources": [], "items": [], "errors": {}}
    if not f.exists():
        return empty
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except Exception as e:                         # noqa: BLE001
        log.warning("情报文件读不出来：%s —— %s", f, e)
        return {**empty, "errors": {"_read": f"latest.json 读不出来：{e}"}}
    if not isinstance(data, dict):
        return {**empty, "errors": {"_read": "latest.json 顶层不是映射"}}
    merged = {**empty, **{k: data.get(k, empty[k]) for k in empty}}
    # 顶层是映射 ≠ 元素形状对。JSON 合法但 `items: [1,2]` / `score: []` 这类
    # 手工改坏的形状，会让 today() 的排序与评分读取 AttributeError → 500，
    # 正好违背本函数「绝不抛」的承诺（B3）。形状坏 = 剔除坏元素 + 留下原因。
    if not isinstance(merged.get("items"), list):
        merged["items"] = []
        merged["errors"]["_read"] = "latest.json 的 items 不是列表（文件可能被手工改坏）"
    else:
        bad = [it for it in merged["items"] if not isinstance(it, dict)]
        if bad:
            merged["items"] = [it for it in merged["items"] if isinstance(it, dict)]
            merged["errors"]["_read"] = (
                f"latest.json 里有 {len(bad)} 个非对象条目已剔除（文件可能被手工改坏）")
        for it in merged["items"]:
            if not isinstance(it.get("score"), dict):
                it["score"] = None     # 排序/评分读取都按「算不出」处理
    return merged


class IntelError(Exception):
    """情报子系统的可读错误。

    ⚠ 只在**写路径**上抛（`add_ignored`）：只读路径（`today()` 的忽略过滤）对坏数据
    退成空是合理的 —— 情报不该因为一个坏文件就整个看不了（B3）。
    但**写**不一样：拿坏数据当基底写回去会把已有的东西抹掉。
    """


def _load_ignored_checked(data_dir: Path, pack: str) -> tuple[dict, str]:
    """读忽略记录，返回 `(数据, 错误说明)`。

    ⚠ **「读坏」与「没有记录」必须分开**：`add_ignored` 是"读出来 → 加一条 → 写回去"，
    读坏时拿到 `{}` 会把整个忽略历史**覆盖成一条** —— 用户看到的是
    "我忽略过的又都回来了"，而手上没有任何线索（旧实现就是 `except: return {}`）。
    """
    f = intel_dir(data_dir, pack) / "ignored.json"
    if not f.exists():
        return {}, ""
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except OSError as e:
        return {}, f"忽略记录读不出来（{f}）：{e}"
    except ValueError as e:
        return {}, f"忽略记录不是合法 JSON（{f}）：{e}"
    if not isinstance(data, dict):
        return {}, f"忽略记录的结构不对（{f}）：期望对象，实际 {type(data).__name__}"
    return data, ""


def load_ignored(data_dir: Path, pack: str) -> dict:
    """本地忽略记录：`{条目 key: "YYYY-MM-DD"}`（只影响今天，见 §2.5）。

    **读不到返回空 dict**（与 `load_latest` 同款：不抛）—— 只读路径用它。
    ⚠ **写路径必须用 `_load_ignored_checked`**：那边读坏要中止，
    不能拿 `{}` 当基底去覆盖。
    """
    return _load_ignored_checked(data_dir, pack)[0]


def add_ignored(data_dir: Path, pack: str, key: str, *, today: str | None = None) -> dict:
    """记一条忽略。**只影响今天** —— 明天同题还会回来。

    记的是 `{key: 最后一次忽略的日期}`，为方案 §2.5 设想的「连续 3 天被忽略
    才沉底」备着日期 —— 但那个沉底机制**尚未实现**（日期当前没有消费方，
    payload 只下发忽略计数）。曾有多处注释声称"连续 3 天沉底"，与实现不符；
    实现落地之前，这里如实写"只影响今天"。别照着旧注释去解释界面行为。
    """
    d = intel_dir(data_dir, pack)
    rec, err = _load_ignored_checked(data_dir, pack)
    if err:
        # 中止，而不是"从空开始"：读坏时写回会把整个忽略历史覆盖成一条，
        # 用户看到的是"我忽略过的又都回来了"（`Pack.private_facts` 同一条取向：
        # 写操作不能建立在坏数据上）。
        raise IntelError(f"{err} —— 为免覆盖已有的忽略记录，这次忽略没有生效")
    rec[str(key)] = today or datetime.now().strftime("%Y-%m-%d")
    try:
        d.mkdir(parents=True, exist_ok=True)
        write_atomic(d / "ignored.json", json.dumps(rec, ensure_ascii=False, indent=1))
    except OSError as e:
        # 写不进去也要**说出来**：以前只 log.warning，而前端照样弹"已忽略" ——
        # 明天同题回来时用户没有任何线索（"我明明忽略过"）。
        raise IntelError(f"忽略记录写不进去（{d}）：{e}") from e
    return rec


def is_stale(data: dict, sources: list[IntelSource], now: datetime | None = None) -> bool:
    """懒触发判据（B4）：**按"上次抓取距今"补跑**，而不是定时任务。

    ⚠ 节奏是**每个源各自**的（§2.2：政策库季度 / 通报月 / 需求词日 / 热榜日 /
    B站按需），所以判据取所有 `usable` 源里**最紧的那一档** —— 只要有一个源
    过期了就该补抓。`on_demand` / 没写节奏的源不参与（它们只在用户点"重抓"时跑）。
    """
    days = [s.cadence_days for s in sources if s.usable and s.cadence_days]
    if not days:
        return False
    fetched = str(data.get("fetched_at") or "")
    if not fetched:
        return True                                # 从没抓过 → 该抓
    try:
        t = datetime.fromisoformat(fetched)
    except ValueError:
        return True
    now = now or datetime.now(timezone.utc).astimezone()
    if t.tzinfo is None:
        t = t.replace(tzinfo=now.tzinfo)
    return (now - t).total_seconds() >= min(days) * 86400


# ── 注入提示词（B7 / §2.9 B-3）─────────────────────────────────
#: 块的首行标记。`_plan_key` 靠它把整块从指纹里**摘掉**。
#: ⚠ 改这一行要同步 `pipeline._plan_key`（它按这个标记切字符串）。
PROMPT_HEAD = "【政策与行业动态（引用须带文号，不得照搬原文）】"
#: 进提示词的条数（§2.2 定的是"按 segment 取最相关 3 条"）。
PROMPT_LIMIT = 3
#: 单条标题的截断长度（进提示词的文本要短，别把 token 花在长标题上）。
PROMPT_TITLE_MAX = 64


def _prompt_rank(it: dict, segment: str) -> tuple:
    """排序键：本细分领域 > 通用 > 别的领域；有出处的优先；再看机会分与新鲜度。"""
    seg = str(it.get("segment") or "")
    by_seg = 0 if (segment and seg == segment) else (1 if not seg else 2)
    has_src = 0 if (it.get("published") or it.get("url")) else 1
    sc = it.get("score") or {}
    return (by_seg, has_src, -(sc.get("opportunity") or 0), -(sc.get("E") or 0),
            str(it.get("title") or ""))


def select_for_prompt(data_dir: Path, pack: str, segment: str | None, *,
                      limit: int = PROMPT_LIMIT,
                      banned: tuple[str, ...] = ()) -> tuple[str, list[str], list[str]]:
    """按细分领域挑最相关的 N 条情报 → `(要注入的文本块, 指纹短键列表, 被摘掉的标题)`。

    三件事各有一个理由，都是对着 `需求方案` 里写明的顾虑来的：

    1. **只给标题 + 出处（来源 / 日期 / URL），不给原文。**
       待确认 #7 的顾虑是「政策原文含『政府补贴』这类 banwords hard 词，进正文层
       会让模型照抄后被判违规、陷入回炉死循环」。只给标题与出处，模型拿到的是
       "有这么一份文件"这样一个**可引用的线索**，而不是一段可以直接抄的正文。
    2. **含 hard 禁用词的条目直接不进块**（`banned` 由调用方从 `pack.banwords_data()`
       取）。这是上面那条顾虑的**第二道闸**：光靠"不给原文"挡不住标题里就带
       禁用词的情况 —— 政策与通报的标题里确实有。被摘掉的标题返回给调用方，
       由它记进作业日志（**摘了要看得见**，否则又是一次静默）。
    3. **块放在提示词末尾**（与 `$feedback_block` 同一条规矩，P1-40 的前缀缓存），
       且**指纹只带"选中项的 id + 出处"**（§2.9 B-3）—— 情报一刷新不该让所有
       历史版本失效，同一条选题仍要可复现。

    ⚠ 没数据时返回空串而不是"没有这类情报"之类的话术：那是**把没数据说成
    "这类没有"**，与 `no_specific` 那条同类的静默降级。
    """
    data = load_latest(data_dir, pack)
    rows: list[dict] = []
    dropped: list[str] = []
    for it in data.get("items") or []:
        title = str(it.get("title") or "").strip()
        if not title:
            continue
        blob = title + " " + str(it.get("desc") or "")
        if any(b and b in blob for b in banned):
            dropped.append(title)
            continue
        rows.append(it)
    rows.sort(key=lambda it: _prompt_rank(it, str(segment or "")))
    picked = rows[:max(0, int(limit))]
    if not picked:
        return "", [], dropped
    lines = [PROMPT_HEAD]
    keys: list[str] = []
    for i, it in enumerate(picked, 1):
        title = str(it.get("title") or "").strip()[:PROMPT_TITLE_MAX]
        src = " · ".join(str(x) for x in (it.get("source_label"), it.get("published"),
                                          it.get("url")) if x)
        lines.append(f"{i}. {title}" + (f"　—— {src}" if src else ""))
        keys.append(f"{it.get('guid') or title}|{it.get('published') or it.get('url') or ''}")
    lines.append("（写角度时可以**引用上面的文号或结论**，但不得整段照搬原文；"
                 "正文里没有的事实照旧用 {{待补：xxx}} 占位。）")
    return "\n".join(lines) + "\n", keys, dropped


def today(data_dir: Path, pack: str, sources: list[IntelSource]) -> dict:
    """给 `/api/intel/today` 的只读视图。

    **渲染层不写死任何平台名**：分组、chip、计数全按 `pack.yaml` 的声明生成
    （§2.2「这条配置同时决定四处」）。`enabled: false` 的源照常下发，
    界面据此显示「未接入 N」与原因 —— 于是"这个平台我们不做"是**可见的决定**，
    而不是"界面上没有它"。
    """
    data = load_latest(data_dir, pack)
    # 忽略记录走**带错误说明**的读法：读坏时静默当空，用户被忽略的条目
    # 会无声回来，界面「已忽略 0」与实际不符且无任何解释（与 latest.json
    # 的 _read 同一透出口径 —— 「别静默」不因这条路径只读而豁免）。
    ignored, ig_err = _load_ignored_checked(data_dir, pack)
    today_str = datetime.now().strftime("%Y-%m-%d")
    # 忽略过滤与**写入**用同一个键（`item_key`），并且把键**下发**给渲染层
    # （`item.key`）—— 渲染层拿它去 POST /api/intel/ignore，不再自己拼一份。
    # 曾经这里漏了 url 那一档（只 `guid or title`），于是"只有 url 的条目
    # 忽略了还在"，而前端拼的是 `guid || url || title` —— 两处不一致。
    items: list[dict] = []
    for it in data.get("items", []):
        k = item_key(it)
        if ignored.get(k, "") == today_str:
            continue
        items.append({**it, "key": k})
    # 池子顺序由后端定：**机会分降序、算不出的排最后**。
    # 「换一批」是纯前端在池子里翻页（零成本），所以池子的顺序就是"翻页顺序" ——
    # 两边各排一次迟早会不一致，只让后端排。
    items.sort(key=lambda it: (
        (it.get("score") or {}).get("opportunity") is None,
        -((it.get("score") or {}).get("opportunity") or 0),
        str(it.get("title") or "")))
    by_source: dict[str, list[dict]] = {}
    for it in items:
        by_source.setdefault(str(it.get("source_label") or it.get("source_id")), []).append(it)
    groups = []
    for spec in sources:
        groups.append({**_source_row(spec), "count": len(by_source.get(spec.label, [])),
                       "items": by_source.get(spec.label, []),
                       "state": ("off" if not spec.enabled
                                 else ("unwired" if not spec.wired else "ok"))})
    # 声明里没有、但盘上有条目的源（例如包改过配置、旧数据还在）也要露出来，
    # 否则那些条目会静默消失 —— 界面显示的总数与条目总数对不上。
    known = {s.label for s in sources}
    for label, rows in by_source.items():
        if label not in known:
            groups.append({"id": rows[0].get("source_id", ""), "label": label,
                           "platform": rows[0].get("platform", ""),
                           "role": rows[0].get("role", ""), "cadence": "",
                           "note": "", "enabled": True, "wired": True,
                           "count": len(rows), "items": rows, "state": "orphan"})
    errors = dict(data.get("errors", {}))
    if ig_err:
        errors["_ignored"] = ig_err + " —— 被忽略的条目会照常显示"
    return {"pack": pack, "fetched_at": data.get("fetched_at", ""),
            "groups": groups, "items": items, "errors": errors,
            "stale": is_stale(data, sources), "ignored": len(ignored),
            "unwired": sum(1 for g in groups if g["state"] in ("unwired", "off"))}
