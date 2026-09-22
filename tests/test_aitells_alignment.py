# -*- coding: utf-8 -*-
"""A3：`需求方案 §2.1` ↔ `ai_tells.yaml` ↔ `AITells` 函数名 三方对账。

为什么需要
----------
三处各管一件事，靠手工同步必漂，而漂法是**静默**的：

  · §2.1 是**规格来源**（tell 名 + severity 口径）；
  · `ai_tells.yaml` 是**机器实现**（哪个 tell 跑、词表有哪些词）；
  · `app/ai_tells.py` 是**算法**（结构类在函数里、词表类走 `LEXICAL`）。

漂起来长这样：yaml 里把 `slogan_closing` 拼成 `slogan_close` → 这条 tell 永不触发，
报告里除了 `config_warnings` 一行之外毫无迹象，分数照样 100；或者把某个 tell
从 strong 挪到 weak → 单条扣分从 12 变 4，**尺子被换了但没人知道**。

⚠ 为什么依据用 §2.1 而**不是** `patterns/anti-ai-smell.md`
--------------------------------------------------------
`anti-ai-smell.md` 是**整份注入提示词**的（`skill.yaml` 的 `write.anti_ai.levels.*.files`）。
往里加一段可解析的表，会同时撞两条既有门禁：
  · `test_dangling_refs.py` —— 渲染出来的提示词里不许出现 `*.md` / `*.yaml` 路径；
  · `test_pack_injection.py` —— 撰写一轮的用户提示词预算 < 7600 字
    （实测加表前 7572，**只剩 28 字余量**）。
所以对账的第三方依据取**不进提示词**的规格文档。这条约束本身值得记着：
`anti-ai-smell.md` 是**给模型看的清单**，不是给 CI 看的账本 —— 要加机器可读的东西，
别加在那里。

另一类不能靠"两份文本对账"发现的漂移（词表内部的自相矛盾），由本文件的
`test_lexicon_*` 三条不变量守：那些是 `_scan_words` 的**使用前提**，
违反了不会报错、只会让某些词**永远匹配不到**。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.ai_tells import AITells                      # noqa: E402

SPEC = ROOT / "docs" / "需求方案-去AI味与热点情报.md"
PACK_DIRS = [ROOT / "packs" / "elevator", ROOT / "packs" / "_template"]

#: §2.1 的 tell 表：`| \`id\` | 中文名 | 算法 | severity |`
_SPEC_ROW = re.compile(r"^\|\s*`([a-z_]+)`\s*\|([^|]*)\|([^|]*)\|([^|]*)\|\s*$")


def _spec_table() -> dict[str, tuple[str, str]]:
    """规格文档 §2.1 的 tell 表 → {tell: (中文名, severity)}。"""
    text = SPEC.read_text(encoding="utf-8")
    out: dict[str, tuple[str, str]] = {}
    for line in text.splitlines():
        m = _SPEC_ROW.match(line.strip())
        if not m:
            continue
        tell, cn, _algo, sev_cell = (x.strip() for x in m.groups())
        sev = "strong" if "strong" in sev_cell else ("weak" if "weak" in sev_cell else "")
        assert sev, f"§2.1 的 {tell} 行没有 severity（单元格：{sev_cell!r}）"
        out[tell] = (cn, sev)
    assert out, "需求方案 §2.1 的 tell 表一行都没解析出来（表格格式变了？）"
    return out


def _yaml_of(pack: Path) -> dict:
    return yaml.safe_load((pack / "ai_tells.yaml").read_text(encoding="utf-8")) or {}


def _severity_map(data: dict) -> dict[str, str]:
    return {tid: tier for tier in ("strong", "weak") for tid in (data.get(tier) or [])}


def test_spec_table_and_code_agree_on_tell_set():
    """§2.1 列的 tell 与 `AITells` 认识的必须是同一批（含中文名，非空）。"""
    spec = _spec_table()
    code = set(AITells.ALL)
    assert set(spec) == code, (
        f"§2.1 与 AITells 的 tell 集合不一致 —— "
        f"规格有、代码没有：{sorted(set(spec) - code)}；"
        f"代码有、规格没写：{sorted(code - set(spec))}")
    nameless = [t for t, (cn, _) in spec.items() if not cn]
    assert not nameless, f"§2.1 里这些 tell 没写中文名（人读那一列是空的）：{nameless}"


@pytest.mark.parametrize("pack", PACK_DIRS, ids=lambda p: p.name)
def test_yaml_severity_matches_spec(pack: Path):
    """severity 决定扣分口径（strong 12 / weak 4），yaml 必须与 §2.1 一致。

    同时守住"yaml 声明的 tell 集合 == 代码认识的集合"：少声明一个 = 这个包不跑它，
    多声明一个 = 拼错名字或引用了已删的 tell。
    """
    spec = _spec_table()
    sev = _severity_map(_yaml_of(pack))
    assert set(sev) == set(spec), (
        f"{pack.name}：ai_tells.yaml 声明的 tell 与 §2.1 不是同一集合 —— "
        f"yaml 有、规格没有：{sorted(set(sev) - set(spec))}；"
        f"规格有、yaml 没声明（= 本包不跑）：{sorted(set(spec) - set(sev))}")
    wrong = {t: (sev[t], spec[t][1]) for t in sev if sev[t] != spec[t][1]}
    assert not wrong, (
        f"{pack.name}：severity 与 §2.1 不一致（格式 {{tell: (yaml, 规格)}}）：{wrong}")


@pytest.mark.parametrize("pack", PACK_DIRS, ids=lambda p: p.name)
def test_lexicon_keys_and_emptiness(pack: Path):
    """`lexicon` 的键必须是认识的词表类 tell；声明了的词表类必须有词。

    两头的静默失效各一个：
      · 键写错（`slogan_close`）→ `_lexical` 按 `LEXICAL` 遍历，永远不读它，
        词表看起来"配了"但一条不扫；
      · 键对、列表为空 → 这条 tell 每次返回 `[]`，是条**死 tell**，
        报告里它与"没命中"长得一模一样。
    """
    data = _yaml_of(pack)
    lex = data.get("lexicon") or {}
    unknown = sorted(set(lex) - set(AITells.LEXICAL))
    assert not unknown, (
        f"{pack.name}：lexicon 里有引擎不认识的类别 {unknown} —— "
        f"这些词一条都不会被扫描到（认识的类别：{list(AITells.LEXICAL)}）")
    empty = sorted(t for t in (data.get("strong") or []) + (data.get("weak") or [])
                   if t in AITells.LEXICAL and not (lex.get(t) or []))
    assert not empty, (
        f"{pack.name}：这些词表类 tell 声明了 severity 却没有词（死 tell）：{empty} —— "
        "要么补词，要么把它从 strong/weak 里删掉")


@pytest.mark.parametrize("pack", PACK_DIRS, ids=lambda p: p.name)
def test_structural_tells_have_no_lexicon(pack: Path):
    """结构类 tell 不该有词表 —— 有也没人读（算法在代码里）。"""
    lex = _yaml_of(pack).get("lexicon") or {}
    stray = sorted(set(lex) & set(AITells.STRUCTURAL))
    assert not stray, (
        f"{pack.name}：结构类 tell 被配了词表 {stray} —— 结构类走函数不走词表，"
        "这些词永远不会被读到（属于静默失效）")


@pytest.mark.parametrize("pack", PACK_DIRS, ids=lambda p: p.name)
def test_lexicon_words_satisfy_scan_words_preconditions(pack: Path):
    """`_scan_words` 的两条**使用前提**：词 ≥2 字、且互不为子串。

    这两条违反后**不报错**，只是某些词永远匹配不到 —— 正是本项目最怕的那种失效：

      · **单字词**：`_scan_words` 按 `len(w) >= 2` 过滤，写了等于没写
        （与 banwords 的 `MIN_WORD_LEN` 同口径）。`anti-ai-smell.md` 里那句
        「抽象名词要写成『高效性』而不是裸『性』」说的就是它。
      · **互为子串**：扫描是"长词优先、不重叠"，`绝对` 与 `绝对化` 同时存在时，
        短词在长词出现的位置永远取不到，而且**计数会虚高**（一个位置算两处）——
        banwords 已经为这个付过学费。
    """
    lex = _yaml_of(pack).get("lexicon") or {}
    short, nested = [], []
    for cat, words in lex.items():
        ws = [str(w) for w in (words or [])]
        short += [f"{cat}:{w}" for w in ws if len(w) < 2]
        for a in ws:
            for b in ws:
                if a != b and a in b:
                    nested.append(f"{cat}:「{a}」是「{b}」的子串")
    assert not short, (
        f"{pack.name}：这些词不到 2 字，`_scan_words` 会把它们整条丢掉"
        f"（写了等于没写）：{short}")
    assert not nested, (
        f"{pack.name}：词表里有互为子串的词，长词优先不重叠扫描会让短词永远取不到"
        f"（且计数虚高）：{sorted(set(nested))}")
