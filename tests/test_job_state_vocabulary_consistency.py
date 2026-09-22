# -*- coding: utf-8 -*-
"""作业状态词表前后端必须是同一份 —— 前端不许把认不出来的状态读成「还在跑」。

背景
----
`app/jobs.py` 的状态机与 `desktop/renderer/js/progress.js` 的中文标签是两种语言，
没法共享代码，于是同一份词表被写了两遍。它已经走散过一次：

分步确认（`paused_awaiting_confirmation`）在 2026-09-19 被整体移除 ——
`/api/jobs/{id}/confirm` 端点、`TRANSITIONS` 里的边、前端确认卡全删了。
但**磁盘上的记录还在**：`generated/index.json` 里留着一条
`state: "paused_awaiting_confirmation"`，而 `app/store.py` 的
`_summary_from_job()` 把 `job.json` 的 state **原样**透传出去。

前端当时是这样判「这一趟结束了吗」的：

    const settled = st => st === "done" || st === "failed" || st === "cancelled";

一个抄来的**终态清单**。于是那个已经消失的状态被读成「还没跑完」：
`loadSessions` 每 3 秒再拉一次 `/api/history`，**永不停止**；行上挂一颗
永远在呼吸的状态点；删除按钮写着「放弃这次生成」（其实没有任何作业可放弃）。
同一份词表还以第三种写法散在 `jobs.js` 的轮询分支里（无条件 `else → 继续轮询`），
`store.py:112` 又以第四种写法抄了一遍 `("failed", "cancelled")`。

**抄终态清单的方向是错的**：漏一个值 = 永久轮询 + 界面卡死。
所以判据改成抄**在跑清单**（`BUSY_STATES`，与后端同名同值）取补集 ——
漏一个值的后果只是「少刷一次」，而少刷一次会由下一次事件驱动刷新补上。

这里不靠「记得改两处」，而是读两个文件比对。改名或挪走会立刻报红。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.jobs import (ALL_BUSY_STATES, BUSY_STATES, INTEL_BUSY_STATES,  # noqa: E402
                      TERMINAL_STATES, TRANSITIONS)

PROGRESS_JS = ROOT / "desktop" / "renderer" / "js" / "progress.js"

# `export const STATE_LABEL = { queued: "排队中", … };`
_LABEL_BLOCK = re.compile(r"export const STATE_LABEL\s*=\s*\{(.*?)\n\};", re.S)
_LABEL_ENTRY = re.compile(r'^\s*(\w+)\s*:\s*"([^"]+)"', re.M)
# `export const BUSY_STATES = new Set([ "queued", … ]);`
_SET_BLOCK = re.compile(r"export const BUSY_STATES\s*=\s*new Set\(\[(.*?)\]\);", re.S)
_SET_ENTRY = re.compile(r'"([a-z_]+)"')

# 2026-09-19 整条移除的状态。它不该再出现在状态机里，
# 但**会**出现在注释与历史文档里 —— 所以只查状态机那三处。
REMOVED_STATE = "paused_awaiting_confirmation"


def _js_src() -> str:
    src = PROGRESS_JS.read_text(encoding="utf-8")
    assert src, f"{PROGRESS_JS} 是空的 —— 断言会空转"
    return src


def _js_labels() -> dict[str, str]:
    m = _LABEL_BLOCK.search(_js_src())
    assert m, "没找到 `export const STATE_LABEL = {…};` —— 改名了？" \
              " 本测试要跟着改，别直接删"
    out = dict(_LABEL_ENTRY.findall(m.group(1)))
    assert out, "STATE_LABEL 解析结果为空 —— 断言会空转，先修正则"
    return out


def _js_busy() -> set[str]:
    m = _SET_BLOCK.search(_js_src())
    assert m, "没找到 `export const BUSY_STATES = new Set([…]);` —— " \
              "前端把「在跑」的判据挪走了？它必须与后端同名同值，见本文件顶部"
    out = set(_SET_ENTRY.findall(m.group(1)))
    assert out, "BUSY_STATES 解析结果为空 —— 断言会空转，先修正则"
    return out


def _all_backend_states() -> set[str]:
    """状态机里出现过的每一个状态：迁移表的起点 ∪ 所有终点。"""
    out = set(TRANSITIONS)
    for targets in TRANSITIONS.values():
        out |= set(targets)
    return out


# ── 1. 前后端「在跑」口径一致 ─────────────────────────────────

def test_js_busy_states_match_backend():
    """核心断言：前端的在跑集合逐项等于后端 **`ALL_BUSY_STATES`**（两族额度的并集）。

    为什么不是 `BUSY_STATES`：后端从 B4 起有**两族**额度 ——
    模型额度（`BUSY_STATES`）与情报抓取的独立额度（`INTEL_BUSY_STATES`）。
    前端只关心"这条作业还在动吗"，那与它占哪一族额度无关，
    所以它的判据是两族的并集。拿 `BUSY_STATES` 比对会让 `fetching`
    被前端读成"已结束"：抓取跑着、界面不刷新、用户看不到进度。
    """
    assert _js_busy() == set(ALL_BUSY_STATES), (
        f"前端 {sorted(_js_busy())} != 后端 {sorted(ALL_BUSY_STATES)}。"
        "不一致时前端会把认不出来的状态当成「已结束」（少刷新），"
        "或把已结束当成「在跑」（每 3 秒空转轮询、永不停止）")
    assert set(ALL_BUSY_STATES) == set(BUSY_STATES) | set(INTEL_BUSY_STATES), \
        "ALL_BUSY_STATES 必须是两族额度的并集（两族各占一档额度，见 app/jobs.py）"


def test_js_state_labels_are_human_readable():
    """每个标签都得是中文，不是把枚举名直接吐给用户。

    「列表曾经直接显示 slug（elevator）」是同一类毛病：
    内部标识出现在界面上。状态名同理 —— 兜底文案必须是「已中断」这种话。
    """
    for key, label in _js_labels().items():
        assert label != key, f"{key} 的标签就是键名本身，界面上会直接吐出 {key}"
        assert re.search(r"[\u4e00-\u9fff]", label), f"{key} 的标签 {label!r} 不是中文"


# ── 2. 标签表与状态机不走散 ───────────────────────────────────

def test_every_backend_state_has_a_label():
    """状态机里的状态，界面上都得有中文名 —— 漏一个就只能靠兜底文案蒙。"""
    missing = _all_backend_states() - set(_js_labels())
    assert not missing, f"这些状态没有中文标签：{sorted(missing)}"


def test_no_label_for_a_state_that_no_longer_exists():
    """反向：标签表里不许留着已经删掉的状态。

    留着的话界面会一直有个「没人能到达」的说法，而读代码的人会以为
    状态机还有那条边 —— 同一信息两份表示、其中一份过期，正是这次事故的形状。
    """
    extra = set(_js_labels()) - _all_backend_states()
    assert not extra, f"标签表里有状态机没有的状态：{sorted(extra)}"


def test_busy_states_all_have_labels():
    """在跑的每个中间态都必须有标签 —— 列表行上要写字。"""
    labels = _js_labels()
    for st in ALL_BUSY_STATES:
        assert st in labels, f"在跑状态 {st} 没有标签，行上只剩一颗呼吸点"


# ── 3. 后端状态机自洽 ─────────────────────────────────────────

def test_state_sets_are_disjoint():
    """「在跑」与「终态」不能重叠 —— 重叠时 running_count() 与幂等判断互相打架。"""
    assert not (BUSY_STATES & TERMINAL_STATES), \
        f"同时属于在跑与终态：{sorted(BUSY_STATES & TERMINAL_STATES)}"


def test_state_sets_cover_the_transition_table():
    """迁移表里出现的每个状态，都必须被归进「在跑」或「终态」之一。

    漏进 BUSY_STATES 的作业**不占并发额度**（`running_count()` 读的是它），
    于是配额形同虚设；漏进 TERMINAL_STATES 则 `_trim()` 永远不会回收它。
    """
    unclassified = _all_backend_states() - (ALL_BUSY_STATES | TERMINAL_STATES)
    assert not unclassified, (
        f"这些状态既不在 BUSY_STATES / INTEL_BUSY_STATES 也不在 TERMINAL_STATES："
        f"{sorted(unclassified)}")


@pytest.mark.parametrize("where", ["transitions", "busy", "terminal"])
def test_removed_step_confirmation_state_stays_gone(where):
    """分步确认整体移除（2026-09-19）—— 状态机三处都不许再出现它。

    它仍然会出现在**磁盘上的旧记录**里，所以前端的兜底分支（认不出来 =
    已结束 + 「已中断」）必须留着；但它绝不能重新变成一个合法状态。
    """
    pool = {"transitions": _all_backend_states(),
            "busy": set(BUSY_STATES),
            "terminal": set(TERMINAL_STATES)}[where]
    assert REMOVED_STATE not in pool


def _strip_comments(src: str) -> str:
    """去掉块注释与整行注释，只留真代码。

    ⚠ 两条正则**不能合并**成一个带 re.S 的选择支：`.*$` 在 re.S 下会一路吃到
    文件末尾，把后面所有代码一起吞掉 —— 那这条断言就永远「通过」，
    而它看起来是绿的。
    """
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return re.sub(r"^\s*//.*$", "", src, flags=re.M)


def test_frontend_no_longer_copies_a_terminal_list():
    """守这次的具体病灶：`sessions.js` 不许再自己抄一份终态清单。

    只查「有没有独立写出 done/failed/cancelled 三个字符串的集合判断」——
    判据必须来自 `BUSY_STATES` 的补集，方向不能反（理由见本文件顶部）。
    """
    src = _strip_comments(
        (ROOT / "desktop" / "renderer" / "js" / "sessions.js").read_text(encoding="utf-8"))
    assert 'st === "done" ||' not in src, \
        'sessions.js 又出现了 settled = st === "done" || … —— 抄终态清单'
    assert "!BUSY_STATES.has(" in src, "settled 必须取 BUSY_STATES 的补集"
    # 非空守卫：注释全被吃掉时上面两条会失去意义，先确认代码还在
    assert "function updateRow" in src, "源码被剥空了 —— 断言会空转，先修 _strip_comments"


def test_quota_messages_still_carry_the_bucketing_marker():
    """409 分桶靠子串匹配，抛出方的文案必须仍然带着那个标记。

    `app/server.py` 给 StateConflict 分两个码：
    `ERR_QUOTA if _QUOTA_MARK in msg else ERR_STATE_CONFLICT`；渲染层
    `isQuotaConflict` 优先读机器码、只在没码时回退文案匹配。于是流水线把
    「同时进行的任务已达上限」改成「任务太多，稍后再试」= 那个 409 悄悄不再带
    quota 码：界面既不提示"等一拍再发"，也不再说清是额度 —— 而这一步
    现有测试一条都不会红（它只测"给了码的那些"）。

    与 `test_setup_refusals_still_match_the_renderer_regex` 同一类：判据从两边各读
    一份事实（AST 取抛出文案 + 源码取常量），不抄整句、不靠"记得改两处"。
    """
    import ast
    import re

    root = Path(__file__).resolve().parent.parent
    srv = (root / "app" / "server.py").read_text(encoding="utf-8")
    m = re.search(r'^_QUOTA_MARK\s*=\s*["“](.+?)["”]', srv, re.M)
    assert m, "app/server.py 里找不到 _QUOTA_MARK，这条守卫该改写法了"
    mark = m.group(1)

    def literals(fname):
        tree = ast.parse((root / "app" / fname).read_text(encoding="utf-8"))
        out = []
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call)
                    and getattr(node.func, "id", "") == "StateConflict" and node.args):
                continue
            a = node.args[0]
            if isinstance(a, ast.Constant):
                out.append((node.lineno, str(a.value)))
            elif isinstance(a, ast.JoinedStr):              # f-string：拼上常量段
                parts = [v.value for v in a.values if isinstance(v, ast.Constant)]
                out.append((node.lineno, "".join(str(p) for p in parts)))
        return out

    sites = literals("pipeline.py") + literals("jobs.py")
    assert sites, "一个 StateConflict 抛出点都没读到，守卫空转"
    quota = [(ln, msg) for ln, msg in sites if re.search(r"上限|额度|并发", msg)]
    other = [(ln, msg) for ln, msg in sites if (ln, msg) not in quota]
    assert len(quota) >= 2, f"额度类抛出点少于两条，说明分类判据散了：{sites}"
    for ln, msg in quota:
        assert mark in msg, f"pipeline.py:{ln} 这句说的是额度，却不再带分桶标记 {mark!r}：{msg}"
    for ln, msg in other:
        assert mark not in msg, f"jobs.py/pipeline.py:{ln} 这句不是额度问题却带着标记，会被念成「已达上限」：{msg}"


def test_error_codes_the_renderer_compares_against_exist_on_the_server():
    """渲染层按 `e.code === "…"` 分流，那些字符串必须仍然是服务端给的码值。

    `jobs.js` 用码分辨「额度满了（等一拍再发）」与「行业包坏了（把引擎原话送到屏幕）」，
    两边各写一份字符串：Python 侧是 `ERR_*` 常量，JS 侧是字面量。
    现有测试只钉住"服务端确实发出 pack_broken"这一半 —— 把常量的**值**改掉
    （改名会有测试提醒，改值不会），渲染层就静默不再分流：坏包被念成普通失败，
    而用户唯一能拿去修的行列号信息被替换掉（P1-1 修过的同一个错）。

    与 BUSY_STATES 那条同源的做法：读两个文件、比对，不靠"记得改两处"。
    """
    root = Path(__file__).resolve().parent.parent
    srv = (root / "app" / "server.py").read_text(encoding="utf-8")
    server_codes = set(re.findall(r'^ERR_[A-Z_]+\s*=\s*["\']([^"\']+)["\']', srv, re.M))
    assert len(server_codes) >= 4, f"服务端错误码没读到，守卫该改写法了：{server_codes}"

    js = (root / "desktop" / "renderer" / "js" / "jobs.js").read_text(encoding="utf-8")
    used = set(re.findall(r"\.code\s*===\s*[\"']([^\"']+)[\"']", js))
    assert used, "jobs.js 里不再按 code 分流 —— 这条守卫要跟着改写法，别直接删"
    dangling = sorted(used - server_codes)
    assert not dangling, \
        f"渲染层在比对这些码，但服务端已经不发它们：{dangling}（服务端现有：{sorted(server_codes)}）"


def test_revision_filtering_uses_a_shared_code_not_prose():
    """「哪些版本是全文回炉」由机器码分流；中文标签只做展示，且两侧不许各写一份。

    第 9 轮扫字面量扫出来的：`result.js` 原来写 `v.action === "全文回炉"`，
    而那句中文是 `pipeline.py` 产的 —— 改一句中文文案（行为完全不变）就会让
    「为什么回炉」横幅静默消失；而 `_verify/verify.js` 的桩手抄同一句中文，
    所以界面门禁照绿（桩比实现宽容 = 假绿）。同一族已修过三处：
    界面正则↔服务端文案、`_QUOTA_MARK` 分桶、错误码表。
    """
    root = Path(__file__).resolve().parent.parent
    py = (root / "app" / "pipeline.py").read_text(encoding="utf-8")
    js = (root / "desktop" / "renderer" / "js" / "result.js").read_text(encoding="utf-8")
    stub = (root / "_verify" / "verify.js").read_text(encoding="utf-8")

    m = re.search(r'REVISION_CODE_FULL_RECHECK\s*=\s*["\']([^"\']+)["\']', py)
    assert m, "app/pipeline.py 里的机器码常量不见了 —— 这条守卫要跟着改写法，别直接删"
    code = m.group(1)
    assert f'"full_recheck"' in js or f"'{code}'" in js, "result.js 不再按机器码分流"
    assert code in stub, "UI 桩不回机器码，界面就只能一直走兜底分支"

    prose = [l.strip() for l in js.splitlines()
             if "全文回炉" in l and not l.strip().startswith(("//", "*", "/*"))]
    assert len(prose) == 1, f"中文标签被当成判据用了不止一处：{prose}"
    assert "action_code === undefined" in prose[0], \
        f"唯一允许的那处必须是「老产物兜底」，且要带 action_code 判空：{prose}"
