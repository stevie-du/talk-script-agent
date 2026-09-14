# -*- coding: utf-8 -*-
"""行业包加载：pack.yaml 解析、知识文件按章节切片、私有资料注入。

性能说明
--------
修复前 `file_text` / `private_facts` 每次调用都读盘 + `yaml.safe_load`，
而一次生成最多要经历 3 轮回炉、每轮注入 6~8 个知识文件 —— 同一个文件被反复
解析十几遍。这里按 (mtime, size) 做进程内缓存：内容一变 mtime 就变，无需手工
失效；写入统一走 `fileio.write_atomic`，所以不会读到写了一半的文件。
"""
from __future__ import annotations

import re
import threading
from pathlib import Path

import yaml

from .schemas import PackInfo


class PackError(RuntimeError):
    pass


# ── 只读文本缓存（按 mtime/size 失效）──────────────────────
_cache_lock = threading.Lock()
_text_cache: dict[str, tuple[float, int, str]] = {}
_yaml_cache: dict[str, tuple[float, int, dict]] = {}


def read_text_cached(path: Path) -> str:
    try:
        st = path.stat()
    except OSError:
        return ""
    key = str(path)
    with _cache_lock:
        hit = _text_cache.get(key)
        if hit and hit[0] == st.st_mtime and hit[1] == st.st_size:
            return hit[2]
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""
    with _cache_lock:
        _text_cache[key] = (st.st_mtime, st.st_size, text)
    return text


def read_yaml_cached(path: Path) -> dict:
    try:
        st = path.stat()
    except OSError:
        return {}
    key = str(path)
    with _cache_lock:
        hit = _yaml_cache.get(key)
        if hit and hit[0] == st.st_mtime and hit[1] == st.st_size:
            return hit[2]
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError, UnicodeDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    with _cache_lock:
        _yaml_cache[key] = (st.st_mtime, st.st_size, data)
    return data


def list_packs(root: Path) -> list[PackInfo]:
    packs_dir = root / "packs"
    out: list[PackInfo] = []
    if packs_dir.exists():
        for d in sorted(packs_dir.iterdir()):
            if d.is_dir() and (d / "pack.yaml").exists():
                try:
                    out.append(pack_info(d))
                except Exception:               # noqa: BLE001
                    # 单个包写坏不该让整个列表（连带首页）崩掉
                    continue
    return out


def pack_info(pack_dir: Path) -> PackInfo:
    data = read_yaml_cached(pack_dir / "pack.yaml")
    return PackInfo(
        name=str(data.get("name", pack_dir.name)),
        display_name=str(data.get("display_name", pack_dir.name)),
        draft=bool(data.get("draft", False)),
        description=str(data.get("description", "")),
        version=int(data.get("version", 1) or 1),
        params=data.get("params", {}) or {},
        param_audit=param_audit(pack_dir, data),
    )


def _heading_exists(pack_dir: Path, rel: str, keyword: str) -> bool:
    """知识文件里是否存在标题含 keyword 的 `##` 章节。"""
    if not keyword:
        return False
    text = read_text_cached(pack_dir / rel)
    return any(re.match(r"^##\s", ln) and keyword in ln for ln in text.split("\n"))


def param_audit(pack_dir: Path, data: dict) -> dict[str, dict[str, str]]:
    """列出「用户能选、但本包没给对应定制」的参数值 → 一句人话降级说明。

    为什么需要
    ----------
    这些值不会报错，只会静默走通用默认：用户加了个「快手」以为照常查平台红线，
    实际平台差异化校验整个不生效；加了新细分领域，注入的却是别的章节。
    静默降级比报错更危险 —— 与 `checker.Banwords.dropped_short` 同一取向
    （那里注释写的是「保留可见性，避免包作者以为写了就生效」）。

    只列**有问题**的值；`persona` 只进提示词、无表可查，故不参与审计。

    返回 `{参数键: {选项值: 说明}}`。
    """
    params = data.get("params", {}) or {}
    out: dict[str, dict[str, str]] = {}

    def note(key: str, value, text: str) -> None:
        out.setdefault(key, {})[str(value)] = text

    def options(key: str) -> list:
        return (params.get(key, {}) or {}).get("options", []) or []

    def as_keys(table: dict) -> set[str]:
        return {str(k) for k in (table or {})}

    # 受众：audience_map 无该值 → audience_slice 退化成注入整份 audience.md
    amap = data.get("audience_map", {}) or {}
    for v in options("audience"):
        kw = amap.get(str(v))
        if not kw:
            note("audience", v, "未配 audience_map：将注入整份受众知识，而非对应章节")
        elif not _heading_exists(pack_dir, "knowledge/audience.md", str(kw)):
            note("audience", v,
                 f"映射的章节「{kw}」在 audience.md 中不存在：将注入整份文件")

    # 细分领域：topics_map 无该值 → 退到「通用」，注入的是别的章节
    tmap = data.get("topics_map", {}) or {}
    for v in options("segment"):
        kw = tmap.get(str(v))
        if not kw:
            g = tmap.get("通用", "")
            note("segment", v,
                 f"未配 topics_map：将改用「通用」章节（{g or '整份文件'}），"
                 "与所选细分领域不匹配")
        elif not _heading_exists(pack_dir, "knowledge/topics.md", str(kw)):
            note("segment", v,
                 f"映射的章节「{kw}」在 topics.md 中不存在：将注入整份文件")

    # 时长：quota_table 缺该键 → 配额按相邻键插值；points_by_duration 缺 → 默认 3
    quota, points = as_keys(data.get("quota_table", {})), as_keys(
        data.get("points_by_duration", {}))
    for v in options("duration"):
        missing = []
        if str(v) not in quota:
            missing.append("字数配额按相邻时长插值")
        if str(v) not in points:
            missing.append("正文要点数用默认 3")
        if missing:
            note("duration", v, "；".join(missing))

    # 风格：rate_by_style 缺该键 → 语速默认 4.5 字/秒，字数配额随之变化
    rates = as_keys(data.get("rate_by_style", {}))
    for v in options("style"):
        if str(v) not in rates:
            note("style", v, "rate_by_style 无该风格：语速按默认 4.5 字/秒，"
                             "字数配额随之变化")

    # 平台：banwords 的平台分级词表缺该键 → 只走基础词表
    try:
        ban = read_yaml_cached(pack_dir / str(data.get("banwords", "banwords.yaml")))
        rules = as_keys((ban or {}).get("platform", {}) or {})
    except Exception:                            # noqa: BLE001
        rules = set()
    for v in options("platform"):
        if str(v) not in rules:
            note("platform", v, "平台分级词表未定义该平台：只按通用词表校验，"
                                "平台差异化红线不生效")

    return out


class Pack:
    """一个行业包。引擎只认 pack.yaml 的结构，不认识任何具体行业。"""

    def __init__(self, root: Path, name: str):
        self.root = root
        self.name_arg = name
        self.dir = root / "packs" / name
        if not (self.dir / "pack.yaml").exists():
            raise PackError(f"行业包不存在：{name}")
        self.data: dict = read_yaml_cached(self.dir / "pack.yaml")
        self.info = pack_info(self.dir)

    # ── 基础 ────────────────────────────────────────────────
    @property
    def name(self) -> str:
        return str(self.data.get("name", self.dir.name))

    @property
    def draft(self) -> bool:
        return bool(self.data.get("draft", False))

    def param_default(self, key: str, fallback=None):
        p = (self.data.get("params", {}) or {}).get(key, {}) or {}
        return p.get("default", fallback)

    def param_options(self, key: str) -> list:
        p = (self.data.get("params", {}) or {}).get(key, {}) or {}
        return p.get("options", []) or []

    def file_text(self, rel: str) -> str:
        return read_text_cached(self.dir / rel)

    # ── 知识切片 ────────────────────────────────────────────
    def slice_heading(self, rel: str, keyword: str) -> str:
        """取文件中包含 keyword 的 `##` 章节全文；找不到则返回整个文件。"""
        text = self.file_text(rel)
        if not text:
            return ""
        if not keyword:
            return text
        lines = text.split("\n")
        start = None
        for i, line in enumerate(lines):
            if re.match(r"^##\s", line) and keyword in line:
                start = i
                break
        if start is None:
            return text
        end = len(lines)
        for j in range(start + 1, len(lines)):
            if re.match(r"^##\s", lines[j]):
                end = j
                break
        return "\n".join(lines[start:end])

    def topics_slice(self, segment: str | None) -> str:
        mapping = self.data.get("topics_map", {}) or {}
        key = mapping.get(segment or "", "")
        target = key or mapping.get("通用", "")
        return self.slice_heading("knowledge/topics.md", target)

    def audience_slice(self, audience: str | None) -> str:
        mapping = self.data.get("audience_map", {}) or {}
        key = mapping.get(audience or "", "")
        return (self.slice_heading("knowledge/audience.md", key) if key
                else self.file_text("knowledge/audience.md"))

    # ── 配额与词表 ──────────────────────────────────────────
    def rate_for_style(self, style: str | None) -> float:
        rates = self.data.get("rate_by_style", {}) or {}
        try:
            return float(rates.get(style or "", 4.5))
        except (TypeError, ValueError):
            return 4.5

    def points_limit(self, duration: float) -> int:
        table = self.data.get("points_by_duration", {}) or {}
        try:
            return int(table.get(str(int(duration)), table.get(int(duration), 3)))
        except (TypeError, ValueError):
            return 3

    def banwords_data(self) -> dict:
        rel = self.data.get("banwords", "banwords.yaml")
        return read_yaml_cached(self.dir / rel)

    def skill(self) -> dict | None:
        """包的生成技能定义（skill.yaml）：各阶段提示词、注入文件、回炉上限。"""
        return read_yaml_cached(self.dir / "skill.yaml") or None

    # ── 私有资料（只注入非空条目）────────────────────────────
    def private_facts(self) -> str:
        rels = (self.data.get("files", {}) or {}).get("private", []) or []
        blocks = []
        for rel in rels:
            data = read_yaml_cached(self.dir / rel)
            if not data:
                continue
            slim = strip_empty(data)
            if slim:
                blocks.append(f"=== {rel} ===\n"
                              + yaml.safe_dump(slim, allow_unicode=True, sort_keys=False))
        return "\n".join(blocks)


def strip_empty(data):
    """递归剔除空值/示例占位（value 含"示例："的条目视为未填）"""
    if isinstance(data, dict):
        out = {}
        for k, v in data.items():
            v2 = strip_empty(v)
            if v2 not in (None, "", [], {}):
                out[k] = v2
        return out
    if isinstance(data, list):
        out = []
        for item in data:
            v2 = strip_empty(item)
            if v2 not in (None, "", [], {}):
                out.append(v2)
        return out
    if isinstance(data, str):
        if "示例：" in data:
            return None
        return data
    return data
