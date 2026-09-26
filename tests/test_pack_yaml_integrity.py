# -*- coding: utf-8 -*-
"""行业包 YAML 读坏时的行为回归（P1-5 / P1-6）。

跑法：pytest tests/test_pack_yaml_integrity.py

问题
----
`knowledge.read_yaml_cached()` 曾经是：

    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError, UnicodeDecodeError):
        return {}                       # ← 无日志、无区分、无返回值

于是「文件写坏了」与「本来就没这个文件」变成同一个结果 `{}`。三个后果：

| 文件 | 读坏后 | 用户看到 |
|---|---|---|
| `banwords.yaml` | 词表空 → `scan()` 一条也查不出 | **合规校验全过**，与「文案很干净」同形 |
| `pack.yaml` | `params` 空、包名退成目录 slug | 参数条空掉，像是个没配好的包 |
| `skill.yaml` | 提示词退回内置默认 | 产出的脚本不再符合本行业口径 |

最要命的是 **`param_audit` 自己也读 `pack.yaml`**：它本来是用来「把静默降级
摊到界面上」的防线，可包一坏它就变成 `{}` —— 防线与它要审计的数据同生共死，
失效时连自己都不报。

实测（2026-09-15，本机 `packs/elevator`）：

    文案「绝对安全，零事故，全网最低价，包过检。」
      真词表 → hard 4 条 + soft 1 条 + dropped_short 1 条
      空词表 → hard 0 + soft 0 + dropped_short 0

修法
----
1. `read_yaml_cached` 返回 `(data, err)` —— **形态变了，每个调用点都被迫
   显式决定「读不出来怎么办」**，不能靠自觉；
2. 读取口径收敛到 `fileio.read_yaml_file`（与 config.yaml 同一份）；
3. 生成路径（`Pack`）抛 `PackBrokenError` → HTTP **409**（包就在那儿，是内容要修）；
   列表路径（`pack_info`）**不抛**，把错误挂到 `PackInfo.pack_error` 摊到界面 ——
   包**留着并标出来**，既不静默降级也不静默消失。

变异检验（把下面任一条改回去，本文件必须报红）
--------------------------------------------
- M1 `read_yaml_cached` 丢掉 err（`err = ""`）→ **14 条红**
- M2 `Pack.__init__` 去掉 `if self.info.pack_error: raise` → **3 条红**
  （快速失败那条：坏包不再被拦在队列外）
- M3 `banwords_data()` 去掉出口守卫 → **1 条红**
  （`test_banwords_guard_survives_a_change_after_construction`）
- M4 `pack_info` 把 `pack_error=err` 改成 `""` → **6 条红**

**M5 单独不成立，别把它当成证伪依据**（实测）：把 `read_yaml_file` 的
`open(p)` 换成 `p.read_text()`，本文件 **0 红** —— 因为 `yaml_error_brief`
根本不用 `str(e)`，只取 `context` / `problem` / `problem_mark`。
两条防线**各自都够**，要同时去掉才会泄漏：M5 + M6（把 brief 改成 `return str(e)`）
→ 19 条红，其中包含本文件的 `test_pack_yaml_error_never_carries_the_source_line`。
写下来是为了别把「改一层没红」误判成「这条断言是假的」。
"""
from __future__ import annotations

import logging
import re
import shutil
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.checker import Banwords                                    # noqa: E402
from app.fileio import read_yaml_file                               # noqa: E402
from app.jobs import JobRegistry                                    # noqa: E402
from app.knowledge import (Pack, PackBrokenError, PackError,        # noqa: E402
                           list_packs, pack_info, read_yaml_cached)
from app.server import create_app                                   # noqa: E402

try:
    from fastapi.testclient import TestClient
except ImportError:                                                 # pragma: no cover
    TestClient = None

TOKEN = "pack-yaml-token"
LOOPBACK = "http://127.0.0.1:8765"

# 一段同时含硬伤与软词的文案。数字见文件头「实测」——不要在断言里重算，
# 这里钉的就是那几个数，词表一改就该有人来看一眼。
DIRTY = "绝对安全，零事故，全网最低价，包过检。"

# 坏法：都是真能触发 YAMLError 的写法（手写 YAML 文本，不经过 safe_dump ——
# 这里要的**就是**坏文件，故意不用 safe_dump）。
BROKEN_YAML = "hard:\n  - 绝对安全\n  bad: [unclosed\n"

# 「第 N 行第 M 列」。**不要硬编码具体数字**：同一份坏文件换个 PyYAML 版本、
# 甚至换个换行符，problem_mark 就会挪位置（实测：同一条内容在两次探针里
# 分别报「第 2 行第 6 列」和「第 3 行第 3 列」）。
POS_RE = re.compile(r"第 \d+ 行第 \d+ 列")


def _root(tmp_path: Path) -> Path:
    """把真实的 packs/ 拷到临时目录 —— 用真包而不是手搓的迷你包，
    这样「坏之前本来能查出什么」是有据可依的。"""
    root = tmp_path / "root"
    shutil.copytree(ROOT / "packs", root / "packs")
    return root


def _break(root: Path, pack: str, rel: str) -> Path:
    p = root / "packs" / pack / rel
    p.write_text(BROKEN_YAML, encoding="utf-8")
    return p


# ── 第 1 组：词表读坏必须抛，不能变成空表 ──────────────────────

def test_broken_banwords_raises_instead_of_returning_empty_table(tmp_path):
    """核心断言（P1-5）：词表读坏 → 抛，而不是交出空表。

    这里命中的是**构造期**那道检查（`Pack.__init__` 用 `pack_error` 快速失败）。
    出口守卫另有一条独立断言 —— 见 `test_banwords_guard_survives_a_change_after_construction`。
    """
    root = _root(tmp_path)
    assert len(Pack(root, "elevator").banwords_data()["hard"]) > 0, "前提：正常时词表非空"

    _break(root, "elevator", "banwords.yaml")
    with pytest.raises(PackBrokenError) as ei:
        Pack(root, "elevator").banwords_data()

    msg = str(ei.value)
    assert "banwords.yaml" in msg
    assert POS_RE.search(msg), f"要给出行列号，实际：{msg}"


def test_empty_table_is_exactly_the_silent_pass(tmp_path):
    """**前提守卫**：证明「空表」确实等于「静默全过」—— 否则上面那条断言
    只是在守一个无害的差异。

    这条不测我们的代码，测的是**修复的必要性**：空词表下同一段文案命中 0 条，
    与真词表下的 4+1 条天差地别，而两者的报告结构完全一致。
    """
    root = _root(tmp_path)
    real = Banwords(Pack(root, "elevator").banwords_data())
    empty = Banwords({})            # ← 修复前 banwords.yaml 读坏就是这个状态

    hits_real = real.scan(DIRTY, "抖音")
    hits_empty = empty.scan(DIRTY, "抖音")

    assert [h["word"] for h in hits_real["hard"]] == [
        "全网最低价", "包过检", "绝对安全", "零事故"]
    # P1-21 修复后：soft 与 hard 位置重叠的表述不再双计 ——
    # 「绝对」与 hard 的「绝对安全」重叠，只算 hard 一次，soft 应为 0。
    assert len(hits_real["soft"]) == 0
    assert len(real.dropped_short) == 1
    assert hits_empty == {"hard": [], "soft": [], "dropped_short": []}
    # 报告结构一致 —— 这正是「看不出来」的原因
    assert set(hits_real) == set(hits_empty)


def test_banwords_guard_survives_a_change_after_construction(tmp_path):
    """**出口守卫的独立证伪**：构造期检查只代表「那一刻是好的」。

    一个作业要跑几十秒（选题 → 写稿 → 回炉），期间包被改坏完全可能。
    所以 `banwords_data()` 自己也要守 —— 否则构造期那一次检查就是
    「只在门口查一次」的假安全。

    这条是唯一能在「构造期检查仍在」的前提下杀掉 M3 的断言，
    别因为「构造期已经查过了」把它删掉。
    """
    root = _root(tmp_path)
    pack = Pack(root, "elevator")                 # 此刻一切正常
    assert pack.banwords_data()["hard"], "前提：构造时词表是好的"

    _break(root, "elevator", "banwords.yaml")     # 作业跑着，包被改坏

    with pytest.raises(PackBrokenError) as ei:
        pack.banwords_data()
    msg = str(ei.value)
    assert "banwords.yaml" in msg
    # 出口这条消息要说清**后果**，不能只说「读不出来」——
    # 构造期那条（`pack_error`）是包级说明，不重复这段。
    assert "合规校验" in msg


@pytest.mark.skipif(TestClient is None, reason="需要 fastapi TestClient")
def test_generate_rejects_broken_banwords_before_burning_tokens(tmp_path):
    """构造期检查的价值：**在烧 token 之前**失败。

    词表是在写稿+回炉阶段才读的。如果只靠出口守卫，作业会先跑完 `select`
    阶段（一次真模型调用，已付费）才发现词表坏了。构造期拦下来，
    直接 409 且**不产生任何作业**。
    """
    root = _root(tmp_path)
    (root / "config.yaml").write_text(
        "llm:\n  api_key: MOCK\n  model: mock\n", encoding="utf-8")
    _break(root, "elevator", "banwords.yaml")

    c = TestClient(create_app(root, token=TOKEN), base_url=LOOPBACK,
                   headers={"X-TalkScript-Token": TOKEN})
    # 直接盯「有没有进队列」：把占额度那一步替换掉，
    # 断言它**压根没被调用**。这比「返回 409」更强 ——
    # 409 也可能是额度满了报的，只有这一步没执行才证明是验包拦下的。
    with patch.object(JobRegistry, "add_if_room", autospec=True) as m:
        m.return_value = True
        r = c.post("/api/generate", json={"pack": "elevator", "topic": "随便一个话题"})
    assert r.status_code == 409, r.text
    assert "banwords.yaml" in r.json()["detail"]
    m.assert_not_called()


def test_missing_banwords_is_not_an_error(tmp_path):
    """「没有这个文件」≠「文件坏了」。前者是正常的「本包不配词表」。"""
    root = _root(tmp_path)
    (root / "packs" / "elevator" / "banwords.yaml").unlink()
    assert Pack(root, "elevator").banwords_data() == {}


# ── 第 2 组：read_yaml_cached 的错误也要缓存 ───────────────────

def test_error_is_cached_so_the_warning_is_not_repeated(tmp_path, caplog):
    """错误必须一起进缓存：一次生成要读十几遍 YAML，
    不缓存就是刷十几条 WARNING，真正的第一条反而被淹掉。"""
    root = _root(tmp_path)
    _break(root, "elevator", "banwords.yaml")

    with caplog.at_level(logging.WARNING, logger="app.knowledge"):
        for _ in range(5):
            data, err = read_yaml_cached(root / "packs" / "elevator" / "banwords.yaml")
            assert data == {} and err

    warnings = [r for r in caplog.records if "YAML 读取失败" in r.getMessage()]
    assert len(warnings) == 1, f"同一份坏文件只该报一次，实际 {len(warnings)} 条"


def test_error_clears_after_the_file_is_fixed(tmp_path):
    """修好之后必须能自愈 —— 缓存是按 (mtime, size) 失效的。"""
    root = _root(tmp_path)
    p = _break(root, "elevator", "banwords.yaml")
    assert read_yaml_cached(p)[1], "前提：现在是坏的"

    good = yaml.safe_dump({"hard": ["绝对安全"]}, allow_unicode=True, sort_keys=False)
    p.write_text(good, encoding="utf-8")     # safe_dump 写盘，不手拼 YAML 文本
    data, err = read_yaml_cached(p)
    assert err == ""
    assert data["hard"] == ["绝对安全"]


def test_top_level_not_mapping_is_also_an_error(tmp_path):
    """能解析但顶层不是映射（例如整份被写成了一个字符串）—— 也算坏。"""
    root = _root(tmp_path)
    p = root / "packs" / "elevator" / "pack.yaml"
    p.write_text("just a string", encoding="utf-8")
    data, err = read_yaml_cached(p)
    assert data == {}
    assert "顶层应为键值映射" in err and "str" in err


# ── 第 3 组：生成路径（Pack）严格 ──────────────────────────────

def test_broken_pack_yaml_raises_for_generation(tmp_path):
    """P1-6 核心：pack.yaml 读坏 → 不能拿它生成。"""
    root = _root(tmp_path)
    _break(root, "elevator", "pack.yaml")
    with pytest.raises(PackBrokenError) as ei:
        Pack(root, "elevator")
    assert "pack.yaml" in str(ei.value)
    assert POS_RE.search(str(ei.value))


def test_missing_pack_yaml_is_still_the_404_case(tmp_path):
    """不存在 → 仍是 PackError（HTTP 404），不能被 409 抢走。"""
    root = _root(tmp_path)
    (root / "packs" / "elevator" / "pack.yaml").unlink()
    with pytest.raises(PackError) as ei:
        Pack(root, "elevator")
    assert not isinstance(ei.value, PackBrokenError)
    assert "不存在" in str(ei.value)


def test_broken_skill_yaml_raises_with_the_real_reason(tmp_path):
    """skill.yaml 坏掉曾经会被报成「行业包缺少 skill.yaml」—— 把「坏了」
    说成「缺了」，用户会去翻目录，而文件明明就在那儿。"""
    root = _root(tmp_path)
    _break(root, "elevator", "skill.yaml")
    with pytest.raises(PackBrokenError) as ei:
        Pack(root, "elevator").skill()
    msg = str(ei.value)
    assert "skill.yaml" in msg and "语法有误" in msg
    assert "缺少" not in msg


def test_missing_skill_yaml_still_returns_none(tmp_path):
    """真的缺 skill.yaml 时行为不变（调用方据此报「缺少 skill.yaml」）。"""
    root = _root(tmp_path)
    (root / "packs" / "elevator" / "skill.yaml").unlink()
    assert Pack(root, "elevator").skill() is None


def test_broken_private_facts_raises(tmp_path):
    """私有资料读坏 → 不能静默少注入：脚本里会写成模型编的东西，
    而用户从产物上看不出「我的资料压根没进去」。"""
    root = _root(tmp_path)
    pack = Pack(root, "elevator")
    rels = (pack.data.get("files") or {}).get("private") or []
    assert rels, "前提：这个包配了 private 文件，否则本断言空转"

    _break(root, "elevator", rels[0])
    with pytest.raises(PackBrokenError) as ei:
        Pack(root, "elevator").private_facts()
    # 消息里要给**相对路径**，只给文件名的话包作者还得猜是哪一个
    assert rels[0] in str(ei.value), f"应带上 {rels[0]}，实际：{ei.value}"


# ── 第 4 组：列表路径（pack_info）宽松但要说清楚 ────────────────

def test_broken_pack_does_not_disappear_from_the_list(tmp_path):
    """包坏了要**留着并标出来**。静默消失（P2-7）和静默降级一样差。"""
    root = _root(tmp_path)
    _break(root, "elevator", "pack.yaml")

    names = [i.name for i in list_packs(root)]
    assert names == ["elevator"], f"坏包不该从列表里消失，实际：{names}"


def test_pack_info_reports_instead_of_raising(tmp_path):
    """列表路径不抛，但必须把「这个包现在是降级值」说清楚。"""
    root = _root(tmp_path)
    _break(root, "elevator", "pack.yaml")

    info = pack_info(root / "packs" / "elevator")
    assert info.pack_error, "pack_error 必须非空"
    assert "pack.yaml" in info.pack_error
    # 顺带钉住「降级长什么样」—— 这正是没有 pack_error 时用户看到的东西
    assert info.display_name == "elevator"      # 退成目录 slug
    assert info.params == {}                    # 参数条全空
    assert info.param_audit == {}               # 防线自己也哑了


def test_pack_info_is_clean_when_everything_is_fine(tmp_path):
    """正常包不能被误标 —— 否则警示变成背景噪音。"""
    root = _root(tmp_path)
    info = pack_info(root / "packs" / "elevator")
    assert info.pack_error == ""
    # 显示名来自 pack.yaml，不是目录 slug（坏掉时才退成 slug，见上一条）
    declared = yaml.safe_load(
        (root / "packs" / "elevator" / "pack.yaml").read_text(encoding="utf-8"))
    assert info.display_name == declared["display_name"]
    assert info.display_name != "elevator"
    # 参数键集是**规格副本**：改 pack.yaml 的 params 就得一起改这里。
    # 原来写的是裸数字 `== 7`，报错只说「7 != 8」，看不出是哪个键多了/少了 ——
    # A-2 加 rewrite_scope 时就撞上过这一次，所以改成列出键集。
    assert set(info.params) == {
        "segment", "audience", "duration", "style", "platform", "persona",
        "cta", "rewrite_scope",
    }, f"电梯包的参数键集变了：{sorted(info.params)}"
    assert len(info.params) == len(declared["params"]), \
        "info.params 与 pack.yaml 声明的不是同一份"


def test_broken_banwords_is_visible_from_the_list_too(tmp_path):
    """词表坏掉是本组里最严重的（合规校验失效），不该只在点开参数条时才暴露。"""
    root = _root(tmp_path)
    _break(root, "elevator", "banwords.yaml")
    info = pack_info(root / "packs" / "elevator")
    assert "banwords.yaml" in info.pack_error


def test_scalar_hard_wordlist_is_a_structure_error_not_dropped_short(tmp_path):
    """P1-25：标量 `hard` 必须被当成「结构坏词表」亮出来（pack_error / 抛错），
    而不是把字符串拆成单字计入 dropped_short —— 否则界面横幅会把结构错误
    伪装成正常的「N 个单字被忽略」降级，命中归零还没人知道。"""
    root = _root(tmp_path)
    (root / "packs" / "elevator" / "banwords.yaml").write_text(
        "hard: 政府补贴\n", encoding="utf-8")

    info = pack_info(root / "packs" / "elevator")
    assert "hard" in info.pack_error, f"要指明键路径，实际：{info.pack_error}"
    assert "banwords.yaml" in info.pack_error

    # 生成路径：Banwords 直接构造也必须炸（带文件名与键路径），不许静默降级
    from app.checker import Banwords
    with pytest.raises(Exception) as ei:
        Banwords({"hard": "政府补贴"})
    msg = str(ei.value)
    assert "banwords.yaml" in msg and "hard" in msg, msg


def test_param_audit_says_the_whole_word_list_failed(tmp_path):
    """坏词表时，每个平台选项都要说「整个词表失效」，
    而不是「本平台没配」—— 后者会让人以为只有这一个平台有问题。"""
    root = _root(tmp_path)
    _break(root, "elevator", "banwords.yaml")

    audit = pack_info(root / "packs" / "elevator").param_audit
    platforms = audit.get("platform")
    assert platforms, f"平台项必须有说明，实际 audit={audit}"

    # 选项从 pack.yaml 直接读 —— 此时 Pack() 已经会被构造期检查拦下
    declared = yaml.safe_load(
        (root / "packs" / "elevator" / "pack.yaml").read_text(encoding="utf-8"))
    opts = declared["params"]["platform"]["options"]
    assert opts, "前提：包里有平台选项，否则本断言空转"
    for v in opts:
        text = platforms[str(v)]
        assert "整个禁用词表都失效" in text, f"{v}：{text}"


# ── 第 5 组：HTTP 语义 —— 409 而不是 404 ───────────────────────

@pytest.mark.skipif(TestClient is None, reason="需要 fastapi TestClient")
def test_generate_returns_409_for_broken_pack(tmp_path):
    """包**就在那儿**，给 404 会让用户在列表里反复找一个明明看得见的包。"""
    root = _root(tmp_path)
    (root / "config.yaml").write_text(
        "llm:\n  api_key: MOCK\n  model: mock\n", encoding="utf-8")
    _break(root, "elevator", "pack.yaml")

    c = TestClient(create_app(root, token=TOKEN), base_url=LOOPBACK,
                   headers={"X-TalkScript-Token": TOKEN})
    r = c.post("/api/generate", json={"pack": "elevator", "topic": "随便一个话题"})
    assert r.status_code == 409, r.text
    assert "pack.yaml" in r.json()["detail"]


@pytest.mark.skipif(TestClient is None, reason="需要 fastapi TestClient")
def test_generate_returns_404_for_missing_pack(tmp_path):
    """对照：真不存在仍是 404，别被 409 抢走。"""
    root = _root(tmp_path)
    (root / "config.yaml").write_text(
        "llm:\n  api_key: MOCK\n  model: mock\n", encoding="utf-8")

    c = TestClient(create_app(root, token=TOKEN), base_url=LOOPBACK,
                   headers={"X-TalkScript-Token": TOKEN})
    r = c.post("/api/generate", json={"pack": "no-such-pack", "topic": "随便一个话题"})
    assert r.status_code == 404, r.text
    assert "不存在" in r.json()["detail"]


@pytest.mark.skipif(TestClient is None, reason="需要 fastapi TestClient")
def test_meta_reports_pack_error_without_dropping_the_pack(tmp_path):
    """/api/meta 是首页与参数条的数据源 —— 坏包要带着 pack_error 出现。"""
    root = _root(tmp_path)
    _break(root, "elevator", "pack.yaml")

    c = TestClient(create_app(root, token=TOKEN), base_url=LOOPBACK,
                   headers={"X-TalkScript-Token": TOKEN})
    meta = c.get("/api/meta").json()
    packs = meta["packs"]
    assert len(packs) == 1, packs
    assert packs[0]["name"] == "elevator"
    assert "pack.yaml" in packs[0]["pack_error"]


# ── 第 6 组：安全 —— 消息里不能带出错行的正文 ──────────────────

def test_pack_yaml_error_never_carries_the_source_line(tmp_path):
    """pack.yaml 里可能有 `private` 指向的敏感内容，也可能就是含密钥的行。
    读法必须与 config.yaml 一致（文件对象），否则「安全性质取决于用了哪种读法」。

    这条与 `test_config_corruption.py` 同源，但**测的是行业包这条路径** ——
    修复前 `read_yaml_cached` 用的正是 `read_text()`。
    """
    secret = "sk-super-secret-do-not-leak-9f3a"
    root = _root(tmp_path)
    p = root / "packs" / "elevator" / "pack.yaml"
    p.write_text(f"name: elevator\napi_key: {secret}: extra\n", encoding="utf-8")

    _, err = read_yaml_file(p)
    assert err, "前提：这份文件确实读不出来"
    assert secret not in err, f"错误说明带出了密钥：{err}"
    assert "api_key" not in err


def test_read_yaml_file_returns_empty_for_missing_file(tmp_path):
    """口径：不存在 → ({}, "")。上面所有「区分两种没有」都建立在这一条上。"""
    assert read_yaml_file(tmp_path / "nope.yaml") == ({}, "")


# ── 第 7 组：P2-7 —— 「内容不符合约定」的包既不能消失、也不能静默 ──
#
# 与第 1~6 组是**不同的坏法**：YAML 语法完全正确，但结构不对。
# 这类不会触发 YAMLError，所以 `err` 是空的 —— 第 1~6 组的修复**盖不住它**。
# 修复前实测（2026-09-15）：
#
#   pack.yaml 里 version: v2
#     → pack_info 抛 ValueError → list_packs 的 `except Exception: continue` 咽掉
#     → list_packs 返回 []  → 包从界面上消失，用户以为包丢了
#     → 而 /api/generate 那边 Pack() 抛裸 ValueError → HTTP 500
#
# 注意 `list_packs` 返回 [] 与「这个 root 下真没有包」长得一模一样，
# 所以断言必须同时钉住「包还在」和「有一条日志」。

# 能解析成 YAML、但结构不符合约定的几种写法。都改成**同一个包的 pack.yaml**，
# 用 safe_dump 落盘（不手拼 YAML 文本）。
SHAPE_BROKEN = {
    "version 写成字符串": {"version": "v2"},
    "params.segment 写成字符串": {"params": {"segment": "家用电梯"}},
    "params 整个写成列表": {"params": ["segment"]},
    "audience_map 写成列表": {"audience_map": ["业主乘客"]},
    "quota_table 写成整数": {"quota_table": 3},
    # P2-2：嵌套结构的形状（曾经 pack_info 说健康、Pack 抛裸 AttributeError → 500）
    "banwords 写成映射": {"banwords": {"hard": ["自留词"]}},
    "params.segment.options 写成字符串": {
        "params": {"segment": {"label": "细分", "options": "AB", "default": "A"}}},
}


def _with_pack_yaml(tmp_path: Path, mutate) -> Path:
    """拷一份真包，按 `mutate(dict)` 改 pack.yaml 后用 safe_dump 写回。"""
    root = _root(tmp_path)
    p = root / "packs" / "elevator" / "pack.yaml"
    data = yaml.safe_load(p.read_text(encoding="utf-8"))
    data.update(mutate)
    p.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
                 encoding="utf-8")
    return root


@pytest.mark.parametrize("name", sorted(SHAPE_BROKEN))
def test_shape_error_does_not_remove_the_pack_from_the_list(tmp_path, name):
    """**P2-7 核心**：包从列表里消失是最糟的处理 —— 用户以为包丢了。"""
    root = _with_pack_yaml(tmp_path, SHAPE_BROKEN[name])

    names = [i.name for i in list_packs(root)]
    assert names == ["elevator"], f"{name}：包消失了，实际 {names}"

    info = pack_info(root / "packs" / "elevator")
    assert info.pack_error, f"{name}：pack_error 必须非空"
    assert "不符合约定" in info.pack_error
    # 退化的形态要说清楚：名字退成目录 slug、参数条空掉
    assert info.display_name == "elevator"


@pytest.mark.parametrize("name", sorted(SHAPE_BROKEN))
def test_shape_error_is_logged(tmp_path, name, caplog):
    """**P2-7 的原始诉求**：不能只有 `except: continue` 而没有日志。

    断言的是**日志本身**，不是返回值 —— 修复前返回值也是「没崩」，
    差别全在「有没有人知道」。
    """
    root = _with_pack_yaml(tmp_path, SHAPE_BROKEN[name])

    with caplog.at_level(logging.WARNING, logger="app.knowledge"):
        list_packs(root)

    hits = [r for r in caplog.records if "内容不符合约定" in r.getMessage()]
    assert len(hits) == 1, f"{name}：应有且仅有一条 WARNING，实际 {len(hits)} 条"
    # 日志要带 traceback 才好查（brief 只给一句人话）
    assert hits[0].exc_info, f"{name}：WARNING 应带 exc_info"


def test_shape_error_gives_409_not_500(tmp_path):
    """修复前这里是裸 ValueError → FastAPI 500，前端只能显示「服务器错误」。"""
    root = _with_pack_yaml(tmp_path, SHAPE_BROKEN["version 写成字符串"])
    with pytest.raises(PackBrokenError) as ei:
        Pack(root, "elevator")
    assert "不符合约定" in str(ei.value)
    # 必须是 PackBrokenError（→409），不能是别的
    assert isinstance(ei.value, PackError)


@pytest.mark.skipif(TestClient is None, reason="需要 fastapi TestClient")
def test_generate_returns_409_for_shape_error(tmp_path):
    root = _with_pack_yaml(tmp_path, SHAPE_BROKEN["version 写成字符串"])
    (root / "config.yaml").write_text(
        "llm:\n  api_key: MOCK\n  model: mock\n", encoding="utf-8")

    c = TestClient(create_app(root, token=TOKEN), base_url=LOOPBACK,
                   headers={"X-TalkScript-Token": TOKEN})
    r = c.post("/api/generate", json={"pack": "elevator", "topic": "随便一个话题"})
    assert r.status_code == 409, r.text
    assert "不符合约定" in r.json()["detail"]


def test_healthy_pack_logs_nothing(tmp_path, caplog):
    """对照：正常包不能刷日志 —— 否则真出问题时那条会淹在噪音里。"""
    root = _root(tmp_path)
    with caplog.at_level(logging.WARNING, logger="app.knowledge"):
        list_packs(root)
        pack_info(root / "packs" / "elevator")
    assert [r.getMessage() for r in caplog.records] == []


def test_packs_dir_unreadable_is_logged(tmp_path, caplog):
    """目录本身读不了（不是某个包坏了）—— 同样不能静默。

    用一个**文件**冒充 packs 目录：`exists()` 为真、`iterdir()` 抛
    NotADirectoryError。这比 monkeypatch `Path.iterdir` 更接近真实故障。
    """
    root = tmp_path / "root"
    root.mkdir()
    (root / "packs").write_text("not a directory", encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="app.knowledge"):
        out = list_packs(root)

    assert out == []
    assert any("行业包目录读取失败" in r.getMessage() for r in caplog.records), \
        f"应有一条目录读取失败的 WARNING，实际 {[r.getMessage() for r in caplog.records]}"


def test_template_pack_matches_elevator_contract():
    """新包的骨架（`packs/_template`）必须与 elevator 同一口径（P2-36 / P2-37）。

    新建行业包的骨架来自 `_template`，**新包会原样继承它**：
    - 15 秒档配额曾被判成错形状（cta 15 字 ≈ 3.2 秒：一条 15 秒的片子花
      五分之一时间念 CTA，正文只剩 40 字）→ elevator 已修成 `{body:47, cta:8}`；
    - 分镜 JSON 缺 `bgm` / `transition` / `shot_type`，而 `app/schemas.py`
      与 `docs/场景序列契约.md` 都要求这四个字段。
    不一致的表现是**新包一建出来就带着老毛病**，而且很难往回追到模板上。
    """
    tpl = yaml.safe_load(
        (ROOT / "packs" / "_template" / "pack.yaml").read_text(encoding="utf-8"))
    el = yaml.safe_load(
        (ROOT / "packs" / "elevator" / "pack.yaml").read_text(encoding="utf-8"))
    assert tpl["quota_table"] == el["quota_table"], \
        f"模板与 elevator 的配额表不一致：{tpl['quota_table']} vs {el['quota_table']}"

    tpl_skill = (ROOT / "packs" / "_template" / "skill.yaml").read_text(encoding="utf-8")
    for f in ("bgm", "transition", "shot_type"):
        assert f'"{f}"' in tpl_skill, f"模板的分镜 JSON 缺字段：{f}"

    # P2-4：`$alt_guide` 是「换一版」候选的注入口（prompts._alt_guide 只在
    # reroll 时给内容）。模板缺了它，engine 侧照常注入、safe_substitute 静默
    # 落空 —— 换一版在 elevator 里有候选、在**每个生成的新包**里永远没有，
    # 且无任何日志。draft 与 write 两个模板都要有（elevator 两处都有）。
    import re as _re
    for stage in ("draft", "write"):
        pat = "^  " + stage + r":\n(.*?)(?=^  \w+:$|^\w+:$|\Z)"
        m = _re.search(pat, tpl_skill, _re.S | _re.M)
        assert m and "$alt_guide" in m.group(1), f"模板的 {stage} 模板缺 $alt_guide"



def test_nested_params_shape_error_gives_409_not_500(tmp_path):
    """P2-2：`params.segment` 写成标量 → Pack 必须 409 且点名键，不能 AttributeError → 500。

    曾经 `pack_info` 看不见这层（或吞进宽 except 只给一句泛话），
    `Pack.__init__` 的选项去重在 `(params.get(key) or {}).get(...)` 上
    直接 AttributeError —— 生成端 500，用户只看到「服务器错误」。
    """
    root = _with_pack_yaml(tmp_path, SHAPE_BROKEN["params.segment 写成字符串"])
    with pytest.raises(PackBrokenError) as ei:
        Pack(root, "elevator")
    assert "params.segment" in str(ei.value), str(ei.value)
    assert isinstance(ei.value, PackError)


def test_banwords_shape_error_does_not_pretend_file_missing(tmp_path):
    """P2-2：`banwords:` 写成映射 → 要点名为形状错，不能被 str() 拼成「文件不存在」。"""
    root = _with_pack_yaml(tmp_path, SHAPE_BROKEN["banwords 写成映射"])
    info = pack_info(root / "packs" / "elevator")
    assert info.pack_error, "形状错没被看见"
    assert "banwords 不符合约定" in info.pack_error, info.pack_error
    assert "读取失败" not in info.pack_error, "被 str() 拼成路径后读不到，误报成文件问题"


def test_skill_stages_shape_error_is_seen_by_pack_info(tmp_path):
    """P2-2 的 skill 半边：`stages:` 写成列表 → 列表说坏、Pack 409，两处同一句话。"""
    root = _root(tmp_path)
    p = root / "packs" / "elevator" / "skill.yaml"
    data = yaml.safe_load(p.read_text(encoding="utf-8"))
    data["stages"] = ["draft", "write"]
    p.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
                 encoding="utf-8")
    info = pack_info(root / "packs" / "elevator")
    assert info.pack_error and "stages" in info.pack_error, info.pack_error
    with pytest.raises(PackBrokenError):
        Pack(root, "elevator")
