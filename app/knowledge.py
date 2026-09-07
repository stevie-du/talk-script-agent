# -*- coding: utf-8 -*-
"""行业包加载：pack.yaml 解析、知识文件按章节切片、私有资料注入"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

from .schemas import PackInfo


class PackError(RuntimeError):
    pass


def list_packs(root: Path) -> list[PackInfo]:
    packs_dir = root / "packs"
    out: list[PackInfo] = []
    if packs_dir.exists():
        for d in sorted(packs_dir.iterdir()):
            if d.is_dir() and (d / "pack.yaml").exists():
                out.append(pack_info(d))
    return out


def pack_info(pack_dir: Path) -> PackInfo:
    data = _yaml(pack_dir / "pack.yaml")
    return PackInfo(
        name=data.get("name", pack_dir.name),
        display_name=data.get("display_name", pack_dir.name),
        draft=bool(data.get("draft", False)),
        description=data.get("description", ""),
        version=int(data.get("version", 1)),
        params=data.get("params", {}),
    )


class Pack:
    """一个行业包。引擎只认 pack.yaml 的结构，不认识任何具体行业。"""

    def __init__(self, root: Path, name: str):
        self.dir = root / "packs" / name
        if not (self.dir / "pack.yaml").exists():
            raise PackError(f"行业包不存在: {name}")
        self.data: dict = _yaml(self.dir / "pack.yaml")
        self.info = pack_info(self.dir)

    # ── 基础 ────────────────────────────────────────────────
    @property
    def name(self) -> str:
        return self.data.get("name", self.dir.name)

    @property
    def draft(self) -> bool:
        return bool(self.data.get("draft", False))

    def param_default(self, key: str, fallback=None):
        p = self.data.get("params", {}).get(key, {})
        return p.get("default", fallback)

    def param_options(self, key: str) -> list:
        return self.data.get("params", {}).get(key, {}).get("options", [])

    def file_text(self, rel: str) -> str:
        p = self.dir / rel
        if not p.exists():
            return ""
        return p.read_text(encoding="utf-8")

    def files_text(self, rels: list[str]) -> str:
        parts = []
        for rel in rels:
            t = self.file_text(rel)
            if t:
                parts.append(f"=== {rel} ===\n{t}")
        return "\n\n".join(parts)

    # ── 知识切片 ────────────────────────────────────────────
    def slice_heading(self, rel: str, keyword: str) -> str:
        """取文件中包含 keyword 的 `##` 章节全文；找不到则返回整个文件。"""
        text = self.file_text(rel)
        if not text:
            return ""
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
        mapping = self.data.get("topics_map", {})
        key = mapping.get(segment or "", "")
        target = key or mapping.get("通用", "")
        return self.slice_heading("knowledge/topics.md", target)

    def audience_slice(self, audience: str | None) -> str:
        mapping = self.data.get("audience_map", {})
        key = mapping.get(audience or "", "")
        return self.slice_heading("knowledge/audience.md", key) if key else self.file_text("knowledge/audience.md")

    # ── 配额与词表 ──────────────────────────────────────────
    def rate_for_style(self, style: str | None) -> float:
        rates = self.data.get("rate_by_style", {})
        return float(rates.get(style or "", 4.5))

    def points_limit(self, duration: float) -> int:
        table = self.data.get("points_by_duration", {})
        return int(table.get(str(int(duration)), table.get(int(duration), 3)))

    def banwords_data(self) -> dict:
        rel = self.data.get("banwords", "banwords.yaml")
        return _yaml(self.dir / rel) or {}

    def skill(self) -> dict | None:
        """包的生成技能定义（skill.yaml）：各阶段提示词、注入文件、回炉上限。"""
        return _yaml(self.dir / "skill.yaml") or None

    # ── 私有资料（只注入非空条目）────────────────────────────
    def private_facts(self) -> str:
        blocks = []
        for rel in self.data.get("files", {}).get("private", []):
            p = self.dir / rel
            if not p.exists():
                continue
            data = _yaml(p)
            if not data:
                continue
            slim = _strip_empty(data)
            if slim:
                blocks.append(f"=== {rel} ===\n" + yaml.safe_dump(slim, allow_unicode=True, sort_keys=False))
        return "\n".join(blocks)


def _yaml(p: Path) -> dict:
    if not p.exists():
        return {}
    with open(p, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _strip_empty(data):
    """递归剔除空值/示例占位（value 含"示例："的条目视为未填）"""
    if isinstance(data, dict):
        out = {}
        for k, v in data.items():
            v2 = _strip_empty(v)
            if v2 not in (None, "", [], {}):
                out[k] = v2
        return out
    if isinstance(data, list):
        out = []
        for item in data:
            v2 = _strip_empty(item)
            if v2 not in (None, "", [], {}):
                out.append(v2)
        return out
    if isinstance(data, str):
        if "示例：" in data:
            return None
        return data
    return data
