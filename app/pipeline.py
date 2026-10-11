# -*- coding: utf-8 -*-
"""流水线编排：参数归一 → 选题策划 → 文案撰写 → 校验回炉 → 分镜 → 组装落盘

生成方法（提示词、注入文件、回炉上限）不在代码里，而是每个行业包自带的 skill.yaml
——知识库管"写什么"，技能管"怎么写"，两者都随包配置、随包分发。

本模块现在**只做编排**。职责被拆到：
  - `jobs.py`     作业与状态机（并发正确性的核心）
  - `store.py`    产物落盘与历史索引（「作业目录在哪」的唯一来源）
  - `prompts.py`  提示词渲染（skill.yaml 模板 + 知识文件注入）
  - `security.py` 本地访问控制

状态机与合法迁移见 jobs.TRANSITIONS。

「停止」的正确性
----------------
Python 没法强杀线程，取消只能协作式：把检查点放在每个耗钱耗时的动作**之前**，
线程自己走出来。关键不变量是：
    **result.json 存在 ⟺ 作业状态为 done**
做法是先做状态迁移、迁移成功才落盘 —— 修复前是「先落盘再改状态」，
于是取消恰好落在「写盘完成、状态未改」的窗口时，磁盘上多出一条已完成记录，
而内存里这条作业是 cancelled，历史列表里它就凭空「复活」了。
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel

from .ai_tells import STRONG, WEAK, AITells, severity_word
from .checker import Banwords, Quota, check_script, count_chars, fact_survival
from .config import AppConfig, load_config
from .fileio import rmtree_resilient
from . import jobs as _jobs                              # noqa: F401
from .intel import (BRIEF_SYSTEM, PROMPT_HEAD, brief_banwords as intel_brief_banwords,
                    build_brief_user as intel_build_brief_user, fetch_pack,
                    find_event as intel_find_event, save_brief)
from .jobs import (INTEL_BUSY_STATES, JOB_BUDGET_SECONDS,  # noqa: F401
                   MAX_CONCURRENT_INTEL, TERMINAL_STATES, Job,
                   JobBudget, JobCancelled, JobRegistry, StateConflict,
                   new_job_id)
from .knowledge import (DEFAULT_REWRITE_SCOPE, Pack, PackBrokenError,  # noqa: F401
                        PackError, param_audit, rewrite_scope_error)
from .llm import EmptyContentError, LLMClient
from .packgen import claim_slug, create_pack, preview_slug, release_slug, slug_problem
from .prompts import PromptRenderer
from .schemas import (GenerateRequest, IntelBrief, RewriteSegmentRequest,
                      ScriptResult, ScriptSection, StoryboardShot, TopicPlan)
from .store import ArtifactStore


class ScriptDraft(BaseModel):
    """模型在「撰写」阶段要返回的结构。

    P1-30：只出 sections —— 分镜拆成了独立阶段（见 `_storyboard`），
    正文定稿并校验通过后才生成（实测拆分后 write 从 110~127s 降到 12~23s）。

    P3-42：`alternatives` 只在「换一版」（reroll）时用 —— 一遍思考给 2~3 个
    不同表达的正文版本，让用户挑（对照度加/元器类"一次出 3 版"的公开工作流）。
    默认空 = 单稿路径，契约与成本完全不变；reroll 时模型**可以**多给。
    候选各自过 `check_script`，过的进候选（附各自 check），不过的被拒并在
    步骤里说明为什么少了一版。主稿永远是 sections 那份，`result.json 存在 ⟺
    done` 不变式不破（候选只是附加字段，不影响主稿落盘与左栏记录）。
    """
    sections: list[ScriptSection]
    alternatives: list[list[ScriptSection]] = []


class DraftBundle(BaseModel):
    """模型在「合并」阶段（stages.draft）要返回的结构：选题与正文同回。

    方案 10（主报告 §六-10）：select+write 合并为一次调用，一个思考阶段代替
    原来两轮（实测 select 占总耗时 17%~33%）。plan 在前 —— 模型先把角度/钩子/
    要点定下来，再按它写正文。sections 的结构与旧 write 契约完全一致；
    回炉轮仍走 stages.write（plan 由 $plan_json 注入），所以 write 阶段保留。

    P3-42：`alternatives` 与 ScriptDraft 同义 —— 「换一版」时合并路径也
    一遍思考给多个候选，默认空 = 单稿路径。
    """
    plan: TopicPlan
    sections: list[ScriptSection]
    alternatives: list[list[ScriptSection]] = []


class StoryboardDraft(BaseModel):
    """模型在「分镜」阶段要返回的结构：与 sections 一一对应的分镜列表。"""
    storyboard: list[StoryboardShot]


def check_contract_complete(raw: dict) -> None:
    """`raw` 与 `ScriptResult` 契约的字段集合必须**完全一致**。

    为什么不能只靠 `model_validate`：它卡的是「类型对不对」，不是「字段有没有」。
      - **少**给一个字段 → 静默取默认值。`quota_degraded` 就是这么丢的：
        `_finalize` 的白名单里没有它，于是「配额是估的」这个信号在落盘那一步
        无声消失，而契约层一声不吭。
      - **多**给一个字段 → 静默丢弃（Pydantic 默认忽略未声明的键）。
    两头都是「信号掉了但没人知道」—— 正是本项目一直在整治的静默降级。

    用显式比对而不是 `model_config = ConfigDict(extra="forbid")`：
    后者只堵「多」这一头，而且会一并改变 `_run_rewrite_segment` 里那次
    「读旧产物再校验」的语义 —— 升级路径上的宽容度不该被顺手收掉。
    """
    declared = set(ScriptResult.model_fields)
    if set(raw) != declared:
        raise ValueError(
            "产物字段与 ScriptResult 契约不一致 —— "
            f"契约有但 raw 没给：{sorted(declared - set(raw))}；"
            f"raw 给了但契约没声明：{sorted(set(raw) - declared)}")


class SegmentRewrite(BaseModel):
    """模型在「单段重写」阶段要返回的结构。"""
    text: str
    subtitle: str = ""


log = logging.getLogger(__name__)

# 同时进行的作业上限。修复前没有任何限制：一个（本机）脚本可以无限调
# /api/generate，每个作业开一个线程、各自烧 token，直到内存和额度一起见底。
MAX_CONCURRENT_JOBS = 4

# 回炉记录里给界面分流用的机器码（`action` 那一份是给人看的中文标签）。
# 界面以前只能拿中文字面量去筛，等于把一条 UI 行为绑在文案上。
REVISION_CODE_FULL_RECHECK = "full_recheck"
REVISION_ACTION_FULL_RECHECK = "全文回炉"

# ── `_normalize` 的产出，每个键都必须有着落 ──────────────────────
#
# `_finalize` 曾经用一份手写白名单从 `p` 里挑参数落盘，而 `quota_degraded`
# 不在其中 —— 于是「行业包没配 quota_table」这个信号被算出来、存进内存，
# 然后在落盘那一刻**无声地掉了**：`result.json` 里一个字都没有，
# 而 `quota: {total: 256, ...}` 与真配额长得一模一样。
# 更要命的是 `ScriptResult.model_validate(raw)` 对**缺失**字段只会取默认值，
# 不会报错，所以契约层也拦不住。
#
# 下面三张表把「每个键去哪」写成显式声明。**新增键时
# `tests/test_quota_degraded_signal.py::test_normalize_keys_are_all_accounted_for`
# 会报红**，逼作者表态 —— 而不是靠记得去改那份白名单。
#
# 1) 落进 result.json 的 `params`
PERSISTED_PARAMS: tuple[str, ...] = (
    "topic", "segment", "audience", "duration", "style",
    "platform", "persona", "cta", "rate", "voice", "format",
    # A-2：本次回炉用的改写范围。**必须落盘** —— 事后解释"这条为什么被重写了"
    # 靠的就是它；不落盘的话，同一个主题两次生成给出长短不同的稿子，
    # 而 `result.json` 里看不出当时用的是 in-place 还是 structural。
    "rewrite_scope",
    # P3-14：当时用的模型与预算 —— 事后要解释「这条为什么慢 / 为什么这样」
    # 只能靠这些落盘值，不能靠「现在的 config.yaml」（它今天就被改过两次）。
    "model", "max_tokens", "temperature",
)
# 2) 不进 params，但在 result.json 顶层另有落点
PARAMS_ELSEWHERE: dict[str, str] = {
    "pack": "raw['pack']",
    "quota": "raw['quota']",
    "quota_degraded": "raw['quota_degraded']",
}
# 3) 显式不持久化，附理由
PARAMS_DROPPED: dict[str, str] = {
    "facts": "用户临时粘贴的资料，可能很长/含私密内容，不属于产物属性",
    "reroll": "本次「换一版」的开关，不属于产物属性",
    "points": "已由提示词占位符 $points 消费；产物里的要点数看 len(plan.points)",
}
# 4) 运行期由 `_run_generate` 从 client.cfg 附加（P3-14），**不进 `_normalize`** ——
#    `_normalize` 是纯参数归一，不读 self.llm（测试直接调它时会没有客户端）。
#    键名只在这里声明一次，`_run_generate` 遍历本元组附加，别在调用点再抄一遍。
#    ⚠ 这三个键**必须**同时出现在 PERSISTED_PARAMS 里：result.json 的 params 是
#    按 PERSISTED_PARAMS 逐键取的，少一个就静默不进产物（测试有子集断言守着）。
RUNTIME_PARAMS: tuple[str, ...] = ("model", "max_tokens", "temperature")


def _assert_stages_usable(pack: Pack) -> None:
    """这个包的 skill.yaml 声明能不能走通一条生成路径？不能就抛 PackBrokenError。

    **为什么需要**：阶段齐不齐对导入与列表**不查**（结构合法、YAML 能解析
    就放过），而生成链路渲染某阶段时是**裸下标**
    `self.skill["stages"][stage]["system"]`（prompts.render）—— 缺了就 KeyError。
    第三方导入的包完全能长成缺阶段的样子（`packimport` 只管 zip 层与结构层），
    于是症状是「跑完一整轮、烧掉一次调用的钱，然后生成失败」。

    - `write` **恒**必需：draft 只省掉 select 那一轮，write 那一段照走
      （回炉轮渲染的还是它）。⚠ 本闸**只查 system / user_template 两个键**，
      不查 `stages.write.anti_ai` —— anti_ai 整块缺失时 `voice_parts` 会
      **静默退成 ("", "")**（人味档位整条失效）。2026-10-12 落账（B-1）：
      这一形态改由 `knowledge.audit_pack` 在**加载期**报「stages.write.anti_ai
      缺失…」，随 `PackInfo.warnings` 上屏（设置页 #pack-audit 那行）；
      闸门本身不加 —— 软警告拍板：为一段人味提示词把整单生成拦死，
      不是这条缺陷该有的代价，让包作者在界面上看见就够了。
    - `select` 只在**没有** draft 时必需：draft 路径走 `stage_files("draft")`，
      不碰 select。

    ⚠ 查的是**能不能渲染**，不只是「名字在不在」（复审实测抓到的漏网）：
      `skill_shape_error` 对 `stages: {write: {files: {...}}}`（缺
      `system` / `user_template`）返回**空串** —— 它只查 stages/cfg 是不是
      dict。于是那种包过得了这道闸门，一路跑到 `prompts.render` 才抛
      `KeyError: 'system'`，正是这道闸门要消灭的那种「烧完钱才失败」，
      只是触发点从"缺整格"挪到了"格不完整"。所以每一格都要断言那两个键。
    """
    skill = pack.skill()
    stages = (skill or {}).get("stages") or {}
    required = ["write"] if "draft" in stages else ["select", "write"]
    bad: list[str] = []
    for s in required:
        cfg = stages.get(s)
        if not isinstance(cfg, dict):
            # 写成字符串/列表的也有（`stages: {write: "见文档"}`）——
            # 那连 `.get` 都用不上，别让这道闸门自己先炸成 TypeError。
            bad.append(s)
            continue
        # 每一格都要能 render：system 与 user_template 是**两个**裸下标，
        # 少任何一个都是同一个 KeyError。空串同样算坏 —— Template("") 渲染出
        # 一条空指令，模型那边就是"没说要干什么"，比报错更坏。
        gone = [k for k in ("system", "user_template")
                if not str((cfg.get(k) or "")).strip()]
        if gone:
            bad.append(f"{s}（缺 {'、'.join(gone)}）")
    if bad:
        # 用 PackBrokenError 而不是自己造一个：它的语义正是
        # 「包存在、YAML 也合法，但它坏在这儿」→ 409 + 界面指去改包，
        # 与「包选错了」(404) 区分得开（见 knowledge.PackBrokenError）。
        raise PackBrokenError(
            f"行业包 {pack.dir.name} 缺少阶段声明：{'、'.join(bad)}"
            "（skill.yaml 的 stages 里要能选出这一条能走通的路径："
            "write 必需；没有 draft 合并阶段时 select 也要有；"
            "每一格都要写全 system 与 user_template）")

#: 回炉改写范围三档各自给模型的那句话（A-2，对标 shuorenhua 的三档）。
#: 三档都建立在**同一件事**上：把上一版全文附给模型、让它逐处改而不是重掷骰子
#: （P2-41）。差别只在**允许动多大**：
#:
#:   in-place   —— 只句内清理。事实（文号/数字/引文）、`{{待补}}` 占位、cta 的动作
#:                 一个字符都不许动，也不许删句/加句/并句/换段落顺序。
#:                 用在"稿子已经很好，只有几处文风/合规词要换"的时候。
#:   bounded    —— 默认档。可以按点名的几条逐处改，不另起一炉、不动未点名的段落。
#:   structural —— 允许合并/拆分/调整段落顺序。改得动，但最容易矫枉过正。
#:
#: ⚠ 三档都**不许动 `{{待补}}`**：占位是"这里缺事实"的标记（见 ai_tells 的 A-1），
#: 被改写顺手填上一个编的数字，比留着空坏得多。
SCOPE_INSTRUCTION: dict[str, str] = {
    "in-place":
        "**只做句内清理**：可以换词、改标点、调整同一句内的语序；"
        "**不许删句、加句、并句，也不许调整段落顺序**；"
        "文号、数字、引文、{{待补}} 占位、cta 的动作一个字符都不许动",
    "bounded":
        "**在它基础上按上面几条逐处修改**，不要另起一炉重写、不要改动了未点名的段落；"
        "文号、数字、引文与 {{待补}} 占位不许动",
    "structural":
        "**可以重排**：允许合并、拆分、调整段落顺序，但上面点名的每条问题都必须解决；"
        "文号、数字、引文照旧不许动",
}

#: 回炉轮的**两段指令**（A-9 §3.1 #1，抄 slopbuster 的两遍法）。
#: 为什么塞进回炉反馈而不是拆成两次调用：拆轮 = 每篇多烧一次钱（回炉轮的
#: `recheck_rounds` 默认 2，多一次就是 3 次 write）；slopbuster 自己承认"只做减法
#: 的结果死气沉沉、仍被识破"，而多家来源（great-writer 的"对聪明朋友这么说"测试、
#: 搜狐实战帖"改完朗读找卡嘴处"）给的都是**同一句话里的两半**：先删掉机器味，
#: 再把人声补回去。分两段写在同一次调用里，就是把这条路径先跑起来再比。
HUMAN_VOICE_RULE = (
    "- 这次改写**分两步做，顺序不能反**：\n"
    "  第一步只做减法：把上面点名的那几处改掉（删掉套话、拆掉超过 30 字的复合句）；\n"
    "  第二步再把人声补回去：句子有长有短（别每句都一样长）、观点要有**上一版或素材里"
    "已经有的**具体名词（型号、部位、金额、日期）、该下判断的地方就下判断"
    "（「是不是」式的收尾反问比「大家可以了解一下」像人话）。"
    "只做第一步的稿子会「干净但死」。")

#: 过度矫正的**负面清单**（A-9 §3.1 #5，抄 avoid-ai-writing-cn 的"六永不注入"
#: + de-slop 的 guardrails 反模式目录）。
#: 为什么放在回炉反馈块而不是 voice.md：voice.md 与 anti-ai-smell.md 都在
#: `stages.write.anti_ai.levels.strong.files` 里**整份注入撰写轮提示词**，
#: 那一轮的预算实测只剩几十字；回炉反馈块由 pipeline 侧拼接、在 user_template
#: 末尾，**不占撰写轮模板预算**（调研报告-20260928 §3.7 的修正）。
#: ⚠ 收尾句**不在这里**：它按档分（见 `NEGATIVE_TAIL`）—— 原先把"点名清单里
#:   没有的段落一个字都不要动"无条件追加，与 `SCOPE_INSTRUCTION["structural"]`
#:   的"可以重排"自相矛盾，structural 档被自己的负面清单静默降级成 bounded
#:   （审查报告-20260930 M1，实测两句话同现于一份反馈）。
NEGATIVE_INJECTION_RULE = (
    "- 改写有**六条永不注入**（只删减、不添加 —— 把稿子洗成另一种 AI 味不算改好）：\n"
    "  ① 不加原文没有的口头禅与语气词；② 不制造新的排比、对仗或「更重要的是」式收尾；\n"
    "  ③ 不表演坦率（「说实话」「讲真的」不是人味，是另一种模板）；④ 不制造紧张感"
    "（「千万别」「后果很严重」）；⑤ **不虚构任何细节**（数字、地名、案例、亲身经历 ——"
    " 素材里没有就写 {{待补}}，宁可空着也不许编）；⑥ 不把短句拆成断句式戏剧节奏。")

#: 负面清单的**收尾句按档分**（审查报告-20260930 M1）：
#:   in-place / bounded —— 护栏是"未点名的段落别动"（与各自 SCOPE_INSTRUCTION 同向）；
#:   structural —— 许可是"可以重排"，同一句"都不要动"会让模型收到自相矛盾的指令。
#:   重排的边界改成"只为准点名的问题重排"，而不是"不许动"。
#: 未知档位按最严的 default 走（rewrite_scope_error 在参数层已拦，这里是兜底）。
NEGATIVE_TAIL = {
    "structural":
        "  重排只为准上面点名的每一条问题服务 —— 不是为了把稿子换个写法；"
        "段落挪动时内容照旧，不趁机改写没点名的句子。",
    "default":
        "  点名清单里没有的段落，一个字都不要动。",
}

#: 两段指令的**按档补充**（审查报告-20260930 M3'）：in-place 档"不许删句、加句"，
#: 而"第二步把人声补回去"有诱导加句的张力 —— 补一句"在本档允许的动作内做"。
#: bounded/structural 允许动句子结构，不需要这句。
VOICE_SCOPE_NOTE = {
    "in-place":
        "  （本档只做句内清理：人声的补回也只能换词、改语序、加标点，"
        "不加句、不删句、不并句。）",
}


class Pipeline:
    def __init__(self, root: Path, cfg: AppConfig, data_dir: Path | None = None):
        self.root = root
        self.cfg = cfg
        self.mock = getattr(cfg, "mock", False)
        # data_dir：可写目录。开发态 = 项目根；打包后 = 用户数据目录
        # （安装目录通常不可写，配置与产物都不能往那儿放）。
        self.data_dir = data_dir or root
        self.registry = JobRegistry()
        self.store = ArtifactStore(root, data_dir=self.data_dir)
        self._llm_lock = threading.Lock()
        self._llm: LLMClient | None = None
        # 选题复用缓存（见 `_select` 与 `PLAN_CACHE_MAX` 的注释）
        self._plan_lock = threading.Lock()
        self._plan_cache: dict[str, TopicPlan] = {}
        # 同一指纹正在被哪条作业选题（single-flight，见 `_select_once`）
        self._plan_flight: dict[str, threading.Event] = {}

    # ── 兼容旧调用点的薄封装 ────────────────────────────────
    @property
    def jobs(self) -> dict:
        """只读视图（供旧测试/调试）。真正写入请用 registry.add_if_room()。"""
        return {s["id"]: s for s in self.registry.snapshots()}

    def add_job(self, job: Job) -> None:
        self.registry.add(job)

    def get_job(self, jid: str) -> Job:
        return self.registry.get(jid)

    def snapshot_jobs(self, *, include_result: bool = False) -> list[dict]:
        return self.registry.snapshots(include_result=include_result)

    # ── LLM 客户端 ──────────────────────────────────────────
    @property
    def llm(self) -> LLMClient:
        """惰性构建 + 缓存。

        修复前是 `self.llm = self.build_llm()`：每次生成都整体替换实例，
        而多个作业是并发跑的 —— A 作业跑到一半，B 作业把 self.llm 换掉，
        A 后续的请求就换了配置（用户若在此时改了 Key，A 会中途换 Key 继续烧）。
        现在只在缺失时构建，并且走锁；要刷新配置显式调 `reload_llm()`。
        """
        with self._llm_lock:
            if self._llm is None:
                self._llm = self._build_llm()
            return self._llm

    @llm.setter
    def llm(self, client) -> None:
        with self._llm_lock:
            self._llm = client

    def _build_llm(self) -> LLMClient:
        # 现读：设置里改了 Key/模型立即生效（配置文件在 data_dir 下）
        cfg = load_config(self.root, self.data_dir)
        return LLMClient(cfg.llm, mock=self.mock)

    def reload_llm(self) -> LLMClient:
        with self._llm_lock:
            self._llm = self._build_llm()
            return self._llm

    # ── 对外接口 ────────────────────────────────────────────
    def start_generate(self, req: GenerateRequest) -> str:
        # 顺序：**先验包、再占额度**。
        #
        # 包不存在是永久性错误（404），比「额度满了」（409，可重试）更该先报；
        # 而且反过来写会留一条漏额度的路径 —— 若先占额度再验包，PackError 抛出去
        # 时那条 `queued` 作业已经插进注册表，没人回收它，额度被永久吃掉一个。
        # （这正是 P0-3 的形态：额度被没人管的作业占住。）
        pack = Pack(self.root, req.pack)
        # 阶段声明**在受理时就验**（审查 P2 可维护性）：缺 write 的包会
        # 在回炉轮渲染 `stages.write` 时 KeyError —— 而 prompts.render 是
        # **裸下标** `self.skill["stages"][stage]`。放到作业线程里验的话，
        # 受理接口照样回 200 + job_id，界面把作业画成"生成中"，
        # 用户要等它跑完一整轮才看到失败文案 —— 那一轮的钱已经花掉了。
        # 阶段声明齐不齐对导入/列表**不查**（结构合法、YAML 能解析就够了），
        # 所以这道闸门是**生成入口**上唯一一次机会。
        # ⚠ 不是"全站唯一一次"：单段重写那条入口（`rewrite_segment`）自己
        #   `Pack(self.root, result["pack"])`、不经这里，它在 `_run_rewrite_segment`
        #   里渲染 `rewrite_segment` 阶段，另有一道（见那里）。别照着"加了这道
        #   闸门就到处都安全了"来理解 —— 那个理解已经害过一条路了。
        #
        # ⚠ **有 draft 也仍然要求 write 在场**，不是「有 draft 就整段放过」。
        #   draft 只省掉 select 那一轮，write 那一段照走。另一处更隐蔽：
        #   voice_parts 读 `stages.write.anti_ai`，缺了会**静默退成 ("", "")**，
        #   人味档位整条失效且界面毫无提示。
        _assert_stages_usable(pack)
        jid = new_job_id()
        job = Job(jid, "generate", req.model_dump())
        # 先占额度、后建目录：修复前 `job_dir` 在 `add_if_room` 之前无条件 mkdir，
        # 被 409 拒绝的每次点击都会在 generated/ 里漏一个空壳目录（现网已见 3 个），
        # 还让每次 history() 的 _count_on_disk 多 stat 两次。
        if not self.registry.add_if_room(job, MAX_CONCURRENT_JOBS):
            raise StateConflict(
                f"同时进行的生成已达上限（{MAX_CONCURRENT_JOBS} 个），请等其中一条完成后再试")
        try:
            job.work_dir = self.store.job_dir(jid, job.created_at)
        except Exception as e:  # noqa: BLE001
            # 建不出目录（磁盘满 / 权限）也要还额度：作业已经占上一个槽，
            # 原样抛出就是停在 queued —— prune 只收终态，槽永久没了（P1-46）。
            self._fail(job, e)
            raise
        self._spawn(job, lambda: self._run_generate(job, pack))
        return jid

    def start_packgen(self, industry: str, description: str) -> str:
        """P1-43：新建行业包走后台作业 —— 它原来是唯一一个同步长 HTTP 请求。

        同步长请求留下的三个洞一次补掉：
          - **断连即失明**：网关超时 / 机器休眠 / 用户关窗，客户端拿不到响应，
            却无从判断「包到底建出来没有」；作业在服务端继续跑，界面回来轮询就接上。
          - **没有取消**：点「取消」只是不要返回值了，模型的钱与 packs/ 里的目录照旧发生。
          - **不占额度**：与生成并发时互相看不见，磁盘与令牌配额一起被超用。

        能在开跑前确定的错误（名称为空 / 同名包已存在）仍然同步抛出：
        这些结论不花一分钱，没道理让用户等 1~2 分钟才看到。
        """
        industry = (industry or "").strip()
        if not industry:
            raise ValueError("行业名称不能为空")
        slug = preview_slug(industry)
        # 纯符号的行业名起不出目录名。修复前 slugify 回落成 "custom"，于是
        # 「？？？」与「！！！」共用一个目录：第二个永远建不出来，而报错写着
        # 「行业包已存在」。在花模型的钱之前拒掉，并且说清该怎么改。
        if not slug:
            raise ValueError("行业名称里没有任何可用作目录名的字符（纯符号起不了名），"
                             "请换成含中文、字母或数字的名称")
        # 保留设备名（CON / NUL / COM1…）与过长的名字，目录压根建不出来：
        # 拦在占位与模型调用**之前**，否则用户付完一份 token 才看到"生成失败"
        #（第 18 轮复核 P3-4）。
        problem = slug_problem(slug, industry)
        if problem:
            raise ValueError(problem)
        if slug and (self.root / "packs" / slug).exists():
            raise FileExistsError(f"行业包已存在：{slug}")
        # P2-46 后半：名字要在**起作业之前**占住。占不到就是同步 409 ——
        # 让两条同名请求都跑到模型那一步再一败一成，等于白烧一份 token
        # 并且让用户等一两分钟才知道自己重名（修复前 `_creating` 的占用
        # 发生在模型调用之后，正是这个形态）。
        # ⚠ 顺序：先抢锁、再看目录在不在（第 12 轮复核 P2）。原来"存在检查"在
        #   抢锁之前，两条同名请求可以同时越过它 → 各起一个作业、各花一份 token，
        #   后写的那一份还覆盖前一份；`packgen` 里那句"占位在模型调用之前，
        #   所以并发同名不会重复花钱"的说明因此与实际不符。
        # ⚠ 归还点**必须交回自己那份凭证**（第 16 轮复核 P1）：这条路径上归还点有三个
        #   —— 目录已存在的早退、这里的兜底、`_run_packgen` 的 finally。原来它们是三处
        #   `_creating.discard(slug)`，而 `Thread.start()` 在"工作线程已经跑完"之后仍可能
        #   自己抛（CPython 3.14 的 start = 起线程 + `_started.wait()`），于是两次归还之间
        #   挤进来的那条同名作业会被第二次 discard 偷走占位 → 两条作业并行建同一个目录。
        token = claim_slug(slug)
        if token is None:
            if (self.root / "packs" / slug).exists():
                raise FileExistsError(f"行业包已存在：{slug}")
            raise FileExistsError(f"行业包正在创建中：{slug}")
        if (self.root / "packs" / slug).exists():
            release_slug(slug, token)         # 抢到锁了但目录已在：把锁还回去再报错
            raise FileExistsError(f"行业包已存在：{slug}")
        # 占位从这一行起就要有人还。原来 `try` 从 `self.llm` 才开始，于是
        # `new_job_id()` / `Job(...)` / `add_if_room()` 任何一处抛（第 6 轮复核
        # 实测：强行让 `new_job_id` 抛）都会留下一个**永不归还**的 slug ——
        # 那个行业名此后永远建不出来，而报错写着「行业包正在创建中」，
        # 只能重启引擎。归还现在认凭证：同一个凭证叫两次，第二次是空操作。
        # ⚠ 原来这里写的是"归还点只能有这一个"，与实际不符（有三个），
        #   而"discard 幂等"恰恰是那条 P1 的成因 —— 幂等地摘错了人的锁。
        job_added = False
        try:
            jid = new_job_id()
            job = Job(jid, "packgen", {"industry": industry,
                                       "description": (description or "").strip()})
            # 凭证跟着作业走：`_run_packgen` 的 finally 要用它归还，
            # 而这里的兜底用的是同一个 `token` —— 谁先还成，另一次就是空操作。
            job.claim_token = token
            # 建包没有产物目录：job.json 不落 generated/，不进历史索引
            # （它不是一条脚本，出现在左栏会话列表里只会让人找不到）。
            if not self.registry.add_if_room(job, MAX_CONCURRENT_JOBS):
                raise StateConflict(
                    f"同时进行的任务已达上限（{MAX_CONCURRENT_JOBS} 个），请等其中一个完成后再试")
            job_added = True
            # `self.llm` 是惰性构建的（会读 data_dir 下的配置文件），所以它**也可能抛**：
            # 放在 try 外面等于给「配置坏了」留一条漏占位的路径。
            client = self.llm               # P1-6：作业级抓一次，中途不换配置
            self._spawn(job, lambda: self._run_packgen(job, client, slug))
        except BaseException as e:
            release_slug(slug, token)
            # P1-46 的第四条路（第 11 轮复核 P2）：`self.llm` 在 `add_if_room` **之后**抛
            # （配置读不出来）时，原来只归还 slug，作业却永远停在 queued —— 那条并发额度
            # 要等 2× 预算才被回收网摘掉，实测连点十次就是"一个都没在跑但一直已达上限"。
            if job_added and job.state not in TERMINAL_STATES:
                self._fail(job, e)
            raise
        return jid

    def rewrite_segment(self, jid: str, req: RewriteSegmentRequest) -> dict:
        job = self.get_job(jid)
        result = job.result
        if not result or not (0 <= req.index < len(result.get("sections", []))):
            raise ValueError("段落序号无效")
        pack = Pack(self.root, result["pack"])
        # 单段重写复用原作业，准入逻辑与生成走同一道额度闸：
        # 修复前这里只做状态迁移、从不看 MAX_CONCURRENT_JOBS —— 实测 12 条
        # 并发重写在飞（上限 4）。检查与迁移在 registry 同一把锁内完成。
        if not self.registry.transition_if_room(job, "rewriting", MAX_CONCURRENT_JOBS):
            raise StateConflict(
                f"同时进行的生成已达上限（{MAX_CONCURRENT_JOBS} 个），请等其中一条完成后再试")
        # P1-6：单段重写也把 client 抓成局部变量（与生成主流程同口径）。
        # ⚠ 必须在 try 内取（P1-46 的第四条路，本轮复核实测）：`self.llm` 是惰性构建，
        #   会读 data_dir 下的配置 —— 它抛异常时作业已经迁到 `rewriting` 并占住一个
        #   并发额度，原样抛出就永久停在 rewriting（prune 只收终态），界面显示「重写中」。
        try:
            client = self.llm
            # 老记录上的重写要重新计时：started_at 还是当初建作业那一刻，
            # 不重置的话「打开昨天的记录点重写」会立刻被判超预算（P1-5）。
            job.reset_budget()
            self._spawn(job, lambda: self._run_rewrite_segment(
                job, pack, req.index, req.feedback or "", client))
        except Exception as e:  # noqa: BLE001
            self._fail(job, e)
            raise
        return job.snapshot()

    def cancel(self, jid: str) -> dict:
        """取消作业：排队/待确认立即落终态，运行中则置标志让线程尽早自行收工。"""
        job = self.get_job(jid)
        if job.state in TERMINAL_STATES:
            return job.snapshot()          # 已结束：幂等返回，不再报错
        job.request_cancel()
        self._persist(job)
        self.registry.prune()
        return job.snapshot()

    def discard(self, jid: str) -> str:
        """删除记录，并让还在跑的作业立刻收工。

        只删目录是不够的：后台线程迟早会走到 write_result，把目录重新建出来，
        用户刚删掉的记录当场复活（墓碑只负责拦住写入，让它继续烧 token 也是浪费）。
        """
        job = self.registry.find(jid)
        if job is not None:
            job.request_cancel()
            self.registry.remove(jid)
        return self.store.delete(jid)

    # ── 生成主流程 ──────────────────────────────────────────
    def _run_generate(self, job: Job, pack: Pack):
        try:
            self._stop_check(job)
            skill = pack.skill()
            if not skill:
                raise ValueError(f"行业包缺少 skill.yaml（生成技能定义）：{pack.name}")
            p = self._normalize(pack, job.params)
            # 参数选了包里**不存在**的选项 → 对应那份细分/受众知识一个字都不注入。
            # 界面只给包里的选项，所以正常点击走不到这里；走到的是：用户改了
            # pack.yaml 的 options（或换了包）之后用历史/接口带进来的旧值，
            # 以及直接打 API 的调用。修复前这是**静默降级**：产物看着完全正常，
            # 只是少了本应注入的那份行业细分知识 —— P0-18 同一族，缺在参数层。
            unmatched = self._unmatched_options(pack, p)
            if unmatched:
                self._step(job, "params_unmatched",
                           "参数里有本行业包没有的选项，对应的细分/受众知识本次不注入"
                           "（换一个选项或把它补进 pack.yaml）",
                           {"unmatched": unmatched})
            # 选项对得上、但包里那一节本身是空的/映射指错了 —— 这一层只有
            # `param_audit` 知道（它按真文件判断"有没有正文"）。修复前这种降级
            # 只在行业包卡片上说一句，作业里毫无痕迹：用户看到的是一个字都不缺的
            # 产物，只是这一行的行业知识从头到尾没进过模型（P1-3 / P2-7）。
            empty_notes = self._empty_slice_notes(pack, p)
            if empty_notes:
                self._step(job, "knowledge_empty",
                           "本包缺这份细分/受众知识，本次未注入（产物看着正常，"
                           "但这一行是按通用口径写的）",
                           {"notes": empty_notes})
            # P1-6：client 在作业开始时一次抓取，后续全程用局部变量 ——
            # 不这样做的话，作业 A 跑到回炉时 B 作业 reload_llm() 会换掉 self.llm
            # （用户在设置里改了 Key/模型），A 中途换模型/换预算继续烧。
            client = self.llm
            # P3-14：产物参数记下「当时用的什么配置」，事后才能解释
            # 「这条为什么慢 / 为什么长这样」—— 之前排查只能靠当前 config 倒推。
            # 键名走 RUNTIME_PARAMS 单一声明：这里只负责从 client.cfg 取值，
            # 不重复抄键名（抄第二份的教训见 params 四张表那条注释）。
            for k in RUNTIME_PARAMS:
                p[k] = getattr(client.cfg, k)
            stages = skill.get("stages") or {}
            if "draft" in stages:
                # 方案 10：合并包（skill.yaml 声明 stages.draft）选题与正文一次调用，
                # 不进 selecting —— 省掉 select 单独一轮的思考与往返。
                # 状态机本就允许 queued → writing，无需新边。
                job.transition_or_raise("writing", params=p)
                plan, draft0, usage0 = self._draft_once(job, pack, skill, p, client)
                self._stop_check(job)
                self._continue_write(job, pack, skill, plan, client,
                                     prebuilt=(draft0, usage0))
            else:
                # 老包（stages 里没有 draft，本改造之前建的）：select+write 两段照旧。
                job.transition_or_raise("selecting", params=p)
                plan = self._select(job, pack, skill, p, client)
                self._stop_check(job)
                self._continue_write(job, pack, skill, plan, client)
        except JobCancelled:
            self._settle_cancel(job)
        except Exception as e:  # noqa: BLE001
            self._fail(job, e)

    def _continue_write(self, job: Job, pack: Pack, skill: dict, plan: TopicPlan,
                        client: LLMClient, *,
                        prebuilt: tuple[dict, dict] | None = None):
        try:
            self._stop_check(job)
            p = job.params
            draft, revisions = self._write_with_recheck(job, pack, skill, p, plan,
                                                        client, prebuilt=prebuilt)
            # P1-30：正文定稿 + 校验通过后，分镜单独生成（voice 模式整个跳过）。
            # ⚠ "校验通过之后"只在**回炉还在跑**的意义上成立：最后一轮不过校验时
            #   `_write_with_recheck` 照样 return（见该方法的 `or rnd == rounds`）。
            #   修复前不合格也会走到这里画分镜 —— 那一条本来就要以 failed/不合格
            #   收场，画面这步是白烧的一次模型调用（P1-30 的残留半）。
            #   现在只有 `draft["check"]["passed"]` 为真才画分镜；不合格则
            #   记 storyboard_skip（原因=校验未通过），产物空分镜照常落盘，
            #   界面仍看得到「为什么没有分镜」。
            # 老包（本次改造之前建的）没有 storyboard 阶段：跳过分镜而不是让
            # 整条已经写完的脚本以 KeyError 收场。
            # 分镜失败**不报废正文**：正文已通过校验，画面只是没画出来 ——
            # 记 storyboard_skip（带 error）、产物空分镜照常落盘；与单段重写
            # 路径（沿用旧分镜）同口径。修复前这里抛错会走下面的 except →
            # _fail(job)，一条合格的稿子因为画面这一步整条报废。
            storyboard = []
            final_passed = bool((draft.get("check") or {}).get("passed"))
            if p.get("format") != "voice":
                if final_passed:
                    if (skill.get("stages") or {}).get("storyboard"):
                        try:
                            storyboard = self._storyboard(job, pack, draft, p, client)
                        except JobCancelled:
                            raise
                        except Exception as e:  # noqa: BLE001
                            self._step(job, "storyboard_skip", "分镜生成失败，本次产物不含分镜",
                                       {"error": str(e)})
                    else:
                        self._step(job, "storyboard_skip",
                                   "行业包缺少 storyboard 阶段，本次未生成分镜", {})
                else:
                    self._step(job, "storyboard_skip",
                               "终版未通过校验，不生成分镜（分镜只在正文合格后画）",
                               {"passed": False})
            self._finalize(job, pack, p, plan, draft, storyboard, revisions, client)
        except JobCancelled:
            self._settle_cancel(job)
        except Exception as e:  # noqa: BLE001
            self._fail(job, e)

    def _run_packgen(self, job: Job, client: LLMClient, slug: str = "") -> None:
        """建包作业的主体（进度 / 取消 / 错误都走作业这一套）。"""
        built = False
        try:
            self._stop_check(job)
            job.transition_or_raise("packing")
            info = create_pack(self.root, client, job.params["industry"],
                               job.params["description"],
                               on_retry=self._retry_logger(job),
                               on_delta=self._delta_handler(job, "行业包生成"),
                               on_attempt=self._stream_reset(job, "行业包生成"),
                               # 传的是**会抛异常**的闸门而不是布尔谓词：谓词形态下
                               # 「超预算」也被 packgen 报成 JobCancelled("已取消")，
                               # 于是这条分支 `pass` 之后作业永远停在 packing
                               # （P1-45 / P1-46：一个槽永久没了）。
                               should_abort=self._abort_gate(job),
                               deadline=job.deadline(),
                               slug_preclaimed=bool(slug))
            # 回调里已经查过一次（就在写盘之前）；这里再查一次是防它写完之后才被子线程
            # 取消 —— 两条取消收尾路径（下面的 except 与 transition 被拒）对「已落盘的包」
            # 必须是同一本账：回收。见 P2-1。
            built = True
            self._stop_check(job)
            if not job.transition("done", result=info):
                # 取消恰好落在 _stop_check 通过**之后**（request_cancel 已把状态
                # 落成 cancelled，done 迁移被拒）：与下面 JobCancelled 分支
                # **同一本账** —— 本次真建出来的包同样回收（P2-1）。曾经这里
                # 裸 return，包留在盘上，删/留取决于毫秒级竞态，而两个分支的
                # 注释各执一词。
                self._discard_created_pack(job, slug)
                return
            self.registry.prune()
        except JobCancelled:
            # 取消的语义是"什么都没发生"（与生成路径「取消 → 撤掉产物」同一本账）。
            # 第 15 轮复核定到的形态：取消落在写盘之后时包已在盘上、作业却记 cancelled，
            # 界面说"已取消"而下一次同名提交回 409「行业包已存在」—— 用户既看不到
            # 那个包也删不掉它。只有**本次真的建出来了**才回收。
            if built:
                self._discard_created_pack(job, slug)
            self._settle_cancel(job)
        except Exception as e:  # noqa: BLE001
            self._fail(job, e)
        finally:
            # 名字是在 `start_packgen` 里、花钱之前就占下的，所以**任何**收尾路径
            # （成功 / 失败 / 取消 / 早 return）都要归还，否则这个 slug 永远建不了。
            # 用作业上那份凭证归还：入口的兜底可能已经先还过一次（那种情况下
            # 这里也是空操作），而**别人**在这之后抢到的同名占位不会被我们还掉。
            release_slug(slug, job.claim_token)

    # ── B4：情报抓取（走 Job 管道 + **独立**并发额度）────────────
    def start_intel_fetch(self, pack_name: str, http=None) -> str:
        """跑一遍本包声明的全部情报源。返回 `{job_id}`，进度看 `/api/jobs/{id}`。

        为什么走 Job 管道（README B4 明确点名）：抓取要几十秒、要能取消、
        要能在关窗后回来接上进度 —— 这三条正是 `start_packgen`（P1-43）当初
        从同步长请求改成作业的理由，一字不差。

        为什么**独立**额度（`MAX_CONCURRENT_INTEL`）：抓取是纯 HTTP、不花模型的钱。
        塞进 `MAX_CONCURRENT_JOBS` 的后果实测过一次同类形态 ——
        "点一次重抓"把生成的名额占掉，用户看到「生成已达上限」，
        而占着名额的是一个不花钱的请求。

        `http` 是**测试注入点**（生产不传）：`fetch_pack` 本就支持，但 Job 管道
        把调用包在线程里，不从这里透传就注入不进去 —— 那条"没配 Key 不碰模型"
        的测试于是只能真联网（20 个 seed × 每源 12s 预算，沙箱代理下稳定超时）。
        语义不变：注入后仍走完整 Job 管道，只是不发真实请求。
        """
        pack = Pack(self.root, pack_name)     # 不存在 → PackError(404)；坏 → 409
        jid = new_job_id()
        job = Job(jid, "intel", {"pack": pack.dir.name})   # 目录 slug，与落点同把键
        # P3（2026-10-05）：同一包的重抓去重 —— 与 brief 的 add_if_room_dedup 同权。
        # 旧写法用 add_if_room，两条同包抓取可以在飞（MAX_CONCURRENT_INTEL=2），
        # history 的同秒快照文件名互相覆盖、source_health 少记一轮。改用
        # "同一件事只许一条在飞"的闸：在飞时第二条**搭车**（返回同一个 job_id，
        # 这次请求不白花，进度看同一条作业），不同包之间仍受额度约束。
        inserted, existing = self.registry.add_if_room_dedup(
            job, MAX_CONCURRENT_INTEL, INTEL_BUSY_STATES,
            same=lambda j: j.kind == "intel" and j.params.get("pack") == pack.dir.name)
        if not inserted:
            if existing is not None:
                return existing.id
            raise StateConflict(
                f"同时进行的情报抓取已达上限（{MAX_CONCURRENT_INTEL} 个），"
                "请等其中一次完成后再试")
        try:
            self._spawn(job, lambda: self._run_intel_fetch(job, pack, http=http))
        except BaseException as e:            # noqa: BLE001
            # P1-46 同款兜底：起不来线程也要把作业落成终态，否则那个额度
            # 要等 2× 预算才被回收网摘掉，症状是"一个都没在跑，重抓却报已达上限"。
            if job.state not in TERMINAL_STATES:
                self._fail(job, e)
            raise
        return jid

    def _run_intel_fetch(self, job: Job, pack: Pack, http=None) -> None:
        """情报抓取作业的主体。

        ⚠ **失败不是异常出口**：`fetch_pack` 内部每个源各自 try，失败记进
        `errors` 并继续 —— 一个源挂了不该让整次抓取报失败（那会让界面显示
        「重抓失败」，而其实另外 7 个源都成功了）。整条作业失败只留给
        "声明读不出来"这种真正致命的情况。
        """
        try:
            self._stop_check(job)
            job.transition_or_raise("fetching")
            sources = pack.intel_sources()
            self._step(job, "intel_plan", f"情报源声明（{len(sources)} 个）",
                       {"sources": [s.as_dict() for s in sources]})
            out = fetch_pack(
                # ⚠ 落点键 = **目录 slug**（pack.dir.name），不是 pack.name：
                #   选题页 / 忽略 / 源表全部按 slug 寻址（PackInfo.name 即 slug，
                #   见 knowledge.pack_info 的 P1-4 注），yaml 的 name 键只是标签。
                pack.dir.name, self.data_dir, sources,
                seeds=pack.intel_seeds(), keywords=pack.intel_keywords(),
                topics_map=pack.data.get("topics_map") or {},
                segment_options=pack.param_options("segment"),
                # 争议词（§3.3 第 5 条的黄标那一半）来自包自己的声明，**不是** banwords
                # 的 soft 档 —— 那批词是「最 / 第一 / 最新」，拿它标争议会张张卡都黄。
                contested=pack.intel_contested(), http=http,
                # 源与源之间的收工闸门（审查 P2 可维护性）：抓取是最慢的一段，
                # 而没有闸门时用户点了停止，那句「正在停止…」要一直挂到
                # 8 个源全跑完 —— 停止键在观感上是坏的。抛的是 _stop_check
                # 认的那两种（JobCancelled / JobBudget），所以走的是与其它
                # 阶段**同一条**取消通路，不另开一个「抓取专用」的异常。
                should_stop=lambda: self._stop_check(job))
            # 取消检查放在**写盘之后、落状态之前**：落盘是有用的副作用
            # （下次打开就有数据），但状态必须是 cancelled 而不是 done。
            self._stop_check(job)
            for i, row in enumerate(out["sources"]):
                self._step(job, f"intel_src_{i}",
                           f"{row['label']}：{row['count']} 条"
                           + (f"（{row['error']}）" if row["error"] else ""),
                           {"count": row["count"], "error": row["error"],
                            "state": row["state"], "id": row["id"]})
            if not job.transition("done", result={
                    "pack": out["pack"], "fetched_at": out["fetched_at"],
                    "items": len(out["items"]), "sources": out["sources"],
                    "errors": out["errors"]}):
                return
            self.registry.prune()
        except JobCancelled:
            self._settle_cancel(job)
        except Exception as e:                # noqa: BLE001
            self._fail(job, e)

    # ── 选题卡（§3.4 建议层：手动触发、一次一卡、花钱）──────────
    def start_intel_brief(self, pack_name: str, key: str) -> tuple[str, bool]:
        """为**一个事件**生成选题卡。返回 `(job_id, 是不是复用了一条在飞的作业)`。

        为什么走 Job 而不是同步请求：与 `start_packgen` 同一条理由（P1-43）——
        一次模型调用几十秒、要能取消、关窗之后要能接上。
        为什么**占模型额度**而不是情报额度（§3.4）：抓取不花钱、这张卡花。
        `intel_brief` 因此**显式**写进 `jobs.MODEL_JOB_KINDS`，不靠"默认落进模型族"
        那个巧合 —— 靠巧合的话，额度隔离那条断言就没有被守的对象。

        缓存命中**不进这里**：`/api/intel/brief` 先看今天有没有生成过，
        有就直接把那份发回去（一条没有模型调用的作业会占着一个并发额度、
        进度条转一圈、最后说"没花钱"——那是把零成本的事演成昂贵的样子）。

        但缓存只覆盖"已经生成完"的那段：作业在飞的几十秒里盘上还是空的，
        换窗口或重启后再点一次会起**第二条**作业 —— 两次模型调用、两张一样的卡，
        而 §5 #2 的验收写的是"当日重复点击不重扣费"。所以这里改成
        "同一 (pack, key) 有未终态的作业就搭上它"（判+插在 registry 同一把锁内，
        与 `add_if_room` 拒绝 TOCTOU 的做法一致）。
        """
        pack = Pack(self.root, pack_name)     # 不存在 → PackError(404)；坏 → 409
        event = intel_find_event(self.data_dir, pack.dir.name, key)
        if event is None:
            raise StateConflict(
                "这件事不在今天抓到的热榜里了 —— 请重新抓取后再试"
                "（选题卡的输入就是今天那份榜单，不能凭空造一个事件）")
        jid = new_job_id()
        job = Job(jid, "intel_brief", {"pack": pack.dir.name, "key": key})

        def same(other: Job) -> bool:
            return (other.kind == "intel_brief"
                    and other.params.get("pack") == pack.dir.name
                    and other.params.get("key") == key)

        inserted, reused = self.registry.add_if_room_dedup(job, MAX_CONCURRENT_JOBS,
                                                           same=same)
        if reused is not None:
            return str(reused.id), True
        if not inserted:
            raise StateConflict(
                f"同时进行的生成已达上限（{MAX_CONCURRENT_JOBS} 个），"
                "选题卡一次要跑一条模型调用，请等其中一条完成后再试")
        try:
            self._spawn(job, lambda: self._run_intel_brief(job, pack, event))
        except BaseException as e:            # noqa: BLE001
            # P1-46 同款兜底：线程起不来也要把作业落成终态，别泄漏并发额度。
            if job.state not in TERMINAL_STATES:
                self._fail(job, e)
            raise
        return jid, False

    def _run_intel_brief(self, job: Job, pack: Pack, event: dict) -> None:
        """选题卡作业主体：拼输入 → 一次 `chat_json` → 过禁用词 → 落 `briefs/`。

        ⚠ 产物**绝不进 `_plan_key`**（§3.4 指纹隔离）：这张卡是用户手动点出来的
        一次性建议，不是生成的输入。哪天有人把它接进 select 的上下文，
        同参数重跑会随机换指纹 —— 反向断言在 tests 里钉着。
        """
        try:
            self._stop_check(job)
            job.transition_or_raise("briefing")
            client = self.llm
            self._step(job, "brief_plan",
                       f"选题卡输入：{str(event.get('title') or '')[:24]}",
                       {"key": event.get("key"), "status": event.get("status")})
            user = intel_build_brief_user(
                event, pack_label=pack.info.display_name or pack.dir.name,
                segments=pack.param_options("segment"),
                # 受众给**选项名**而不是 audience_map 那个映射字典：把 dict 直接 str()
                # 进 prompt，模型看到的是一段 Python 字面量（`{'家用电梯': '业主'}`），
                # 而它能用的是"这个账号对谁说话"这句话。
                audience="、".join(str(x) for x in pack.param_options("audience")[:6]))
            usage: dict = {}
            brief: IntelBrief = client.chat_json(
                "intel_brief", BRIEF_SYSTEM, user, IntelBrief,
                on_retry=self._retry_logger(job),
                on_delta=self._delta_handler(job, "选题卡"),
                on_attempt=self._stream_reset(job, "选题卡"),
                should_abort=self._abort_gate(job),
                deadline=job.deadline(),
                usage=usage,
                # 结构性任务降形状漂移（2026-10-12 A-1）：默认温度下模型会把
                # titles 的数组写成单个字符串，错误提示重发也治不了 —— 低温
                # 让它更贴 schema。chat_json 本就支持该参数（llm.py 的签名）。
                temperature=0.2,
                # 输入是几行标题、输出是 3-4 个角度 —— 与 write 共用全额预算
                # 会让思考被预算反向推高（P0-3 同一条），这里给 select 那一档。
                max_tokens=4000)
            payload = brief.model_dump()
            payload["event_key"] = str(event.get("key") or "")
            # 生命周期阶段以**榜单算出的那个**为准，模型回什么都不采纳（§3.4：
            # 「LLM 只复述不自己判」）。让它改判，同屏的排行行与选题卡就会
            # 一个写「爆发中」一个写「衰退期」。
            payload["stage"] = str(event.get("stage") or payload.get("stage") or "")
            hard = tuple(str(w) for w in (pack.banwords_data().get("hard") or []) if w)
            cleaned, hits = intel_brief_banwords(payload, hard)
            if hits:
                self._step(job, "brief_banwords",
                           f"选题卡里有 {len(hits)} 处命中禁用词，已摘标为 {{待补}}",
                           {"words": list(dict.fromkeys(hits))})
            cleaned["created_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
            cleaned["event_title"] = str(event.get("title") or "")
            cleaned["pack"] = pack.dir.name
            save_brief(self.data_dir, pack.dir.name, str(event.get("key") or ""), cleaned)
            # usage 记进步骤而不是塞进缓存文件（P3-15 的口径：只记账、不参与判定；
            # 而 briefs/<key>.json 是"给用户看的那张卡"，多一个没人读的键就是
            # 那条「信号算了没人接」的老账）。
            self._step(job, "brief_done", "选题卡已生成",
                       {"key": cleaned.get("event_key"),
                        "angles": len(cleaned.get("angles") or []),
                        **({"usage": usage} if usage else {})})
            self._stop_check(job)
            if not job.transition("done", result=cleaned):
                return
            self.registry.prune()
        except JobCancelled:
            self._settle_cancel(job)
        except Exception as e:                # noqa: BLE001
            self._fail(job, e)

    # ── 节点实现 ────────────────────────────────────────────
    _OPTION_PARAMS = ("segment", "audience", "style", "platform", "persona", "cta")

    @classmethod
    def _unmatched_options(cls, pack: Pack, p: dict) -> dict:
        """返回「参数里选了、但本包 options 里没有」的那几项（空 = 全部对得上）。

        只报有 options 清单的键：包没给清单（`options: []`）时无从判起，
        把合法值报成非法比漏报更糟。
        """
        out: dict[str, str] = {}
        for k in cls._OPTION_PARAMS:
            v = p.get(k)
            if v in (None, ""):
                continue
            opts = [str(o) for o in pack.param_options(k)]
            if opts and str(v) not in opts:
                out[k] = str(v)
        return out

    @classmethod
    def _empty_slice_notes(cls, pack: Pack, p: dict) -> list[str]:
        """本次**真正选中**的那几个参数，包里对应的知识是不是空的（复用 `param_audit`）。

        不另抄一份判断：`param_audit` 才是按真文件问"这一节有没有正文"的那一个，
        在流水线里再实现一次就是第二本账（对不上时又是"看着正常其实没注入"）。
        """
        try:
            audit = param_audit(pack.dir, pack.data)
        except Exception:                        # noqa: BLE001 体检本身坏了不该拖垮生成
            return []
        notes: list[str] = []
        for key in ("segment", "audience"):
            v = str(p.get(key) or "")
            note = (audit.get(key) or {}).get(v)
            if note:
                notes.append(f"{key}={v}：{note}")
        return notes

    def _normalize(self, pack: Pack, params: dict) -> dict:
        # P3-4（2026-10-11）：voice/format 的合法档位与默认值**只在这里手抄一份**，
        # 下面守卫与 `out` 字典两行共用同一组常量 —— 将来放行新档位只改这里；
        # 不再出现「守卫放行了、`out` 行的清单没跟上 → 静默换成默认」的复活病灶。
        voice_legal, voice_default = ("strong", "standard", "off"), "strong"
        format_legal, format_default = ("both", "voice"), "both"

        def pick(key, fallback=None):
            v = params.get(key)
            return v if v not in (None, "", []) else pack.param_default(key, fallback)

        duration = float(pick("duration", 60))
        style = str(pick("style", ""))
        rate = params.get("rate") or pack.rate_for_style(style)
        # A-2：改写范围。优先级 = 请求 > 本包 pack.yaml > 引擎默认（bounded）。
        # 请求里给了非法值**抛错而不是回落** —— 静默回落等于把用户/调用方
        # 明确表达的意图换成另一个档位去改写稿子（`GenerateRequest` 的 Literal
        # 已在请求层挡住 422，这里兜的是绕过请求模型直接调 `_normalize` 的路径）。
        scope = params.get("rewrite_scope")
        bad_scope = rewrite_scope_error(scope)
        if bad_scope:
            raise ValueError(bad_scope)
        scope = str(scope) if scope not in (None, "") else (pack.rewrite_scope() or DEFAULT_REWRITE_SCOPE)
        # P3（2026-10-05）：voice / format 与 rewrite_scope 同一条立场 —— 显式给了
        # 非法值**抛错而不是回落**。旧写法把非法值静默换成默认并落盘成 params：
        # 用户输入被顶替、无任何作业日志。请求层 Literal 挡住了正常路径（422），
        # 这里兜的是绕过请求模型直调 `_normalize` 的路径；None/空串 = 没填，
        # 不是非法，照走下面的默认。
        for _key, _legal in (("voice", voice_legal),
                              ("format", format_legal)):
            _v = params.get(_key)
            if _v not in (None, "") and _v not in _legal:
                raise ValueError(f"{_key} 取值不合法：{_v!r}")
        quota = Quota.from_pack(pack.data)
        points = pack.points_limit(duration)
        target = quota.target(duration, float(rate)) if quota.available else {}
        degraded = False
        if not target:
            # 行业包没写 quota_table 时的降级：按 时长×语速 估一个总量再分段。
            # 修复前这种情况会以「list index out of range」直接失败，用户
            # 完全看不出是包配置缺了字段。
            total = max(round(duration * float(rate) * 0.95), 20)
            target = {"total": total, "hook": round(total * 0.15),
                      "body": round(total * 0.65), "cta": round(total * 0.20)}
            degraded = True
        out = {
            # 作业参数里的 pack 也是**目录 slug**（与情报作业、PackInfo.name 同一身份，
            # 见 knowledge.pack_info 的 P1-4 注）—— yaml 的 name 键从此只活在报错文案里。
            "pack": pack.dir.name, "topic": str(params.get("topic", "")).strip(),
            "segment": pick("segment"), "audience": pick("audience"),
            "duration": duration, "style": style,
            "platform": pick("platform"), "persona": pick("persona"),
            "cta": pick("cta"), "facts": params.get("facts") or "",
            "rate": float(rate),
            "voice": params.get("voice") if params.get("voice") in voice_legal else voice_default,
            "format": params.get("format") if params.get("format") in format_legal else format_default,
            "rewrite_scope": scope,
            "reroll": bool(params.get("reroll")),
            "points": points,
            "quota": target,
            "quota_degraded": degraded,
        }
        if not out["topic"]:
            raise ValueError("主题不能为空")
        return out

    def _render_stage(self, job: Job, pr: PromptRenderer, stage: str,
                      ctx: dict) -> tuple[str, str]:
        """渲染 + 校验占位符。

        `safe_substitute` 对未知变量保持原样，拼错的占位符不会报错，
        只会让模型收到一段字面量、并静默丢掉本该注入的知识文件。
        这里把残留占位符记进作业日志（界面「日志」页签），包作者一眼能看到。

        ⚠ 这是**所有**阶段渲染的唯一收口（`prompts.render` 那边是裸下标
          `self.skill["stages"][stage]["system"]`，缺一格就 KeyError）。
          所以"这一格能不能渲染"在这里查一次，而不是散在各调用点：
          改了 `storyboard` 那种"缺了就跳过并记一条日志"的路径不会被波及，
          而 `rewrite_segment` 这种**没有兜底**的路径（改前导入一个少写
          `rewrite_segment` 的第三方包，生成一切正常，点「重写本段」才炸）
          会在用户按下那颗键的**那一刻**说清是包的问题。
          ⚠ 报的是 `PackBrokenError` 而不是让它 KeyError：作业落 failed
          时会把异常原文印在界面上，"行业包配置缺少字段：'rewrite_segment'"
          对用户是黑话，而"这个包没配单段重写阶段"是下一步动作。
        """
        cfg = (pr.skill.get("stages") or {}).get(stage)
        if not isinstance(cfg, dict) or not str(cfg.get("system") or "").strip() \
                or not str(cfg.get("user_template") or "").strip():
            raise PackBrokenError(
                f"行业包 {pr.pack.dir.name} 的「{stage}」阶段配不完整"
                "（skill.yaml 的 stages 里这一格要写全 system 与 user_template）")
        missing = pr.unfilled(stage, ctx)
        system, user = pr.render(stage, ctx)
        if missing:
            self._step(job, f"tpl_{stage}", f"模板占位符未填充（{stage}）",
                       {"missing": missing,
                        "hint": "skill.yaml 的 stages.<阶段>.files 的 key 必须与占位符同名"})
        return system, user

    def _select(self, job: Job, pack: Pack, skill: dict, p: dict,
                client: LLMClient) -> TopicPlan:
        pr = PromptRenderer(pack, data_dir=self.data_dir)
        ctx = pr.select_ctx(p)
        system, user = self._render_stage(job, pr, "select", ctx)
        self._step_intel(job, pr)
        # P1-8：「换一版」的提温原来只作用于 write —— 于是「换一版」重掷出的
        # plan 与上一版几乎一样，白白花 74~173s。select 同样提温。
        temp = (min(1.0, float(client.cfg.temperature) + 0.25)
                if p.get("reroll") else None)
        key = None if p.get("reroll") else self._plan_key(
            client, system, user, ctx.get("intel_key", ""), ctx)
        if key:
            hit = self._cache_lookup(key)
            if hit is not None:
                # 省下的是一次 4000 token 的调用与十几到几十秒的等待 ——
                # 但必须在作业日志里说出来，并**占掉「选题策划」那一步的位置**：
                # 照常记一步「选题策划」等于告诉用户"它又想了一遍"，是同一类静默。
                self._step(job, "select_reuse", "复用上次选题（本次未调用模型）",
                           {"plan": hit.model_dump(), "model": client.cfg.model,
                            "hint": self._PLAN_REUSE_HINT})
                # 返回**深拷贝**：缓存里存的是同一个实例，下游若是有人改了 plan
                #（理论上不该，但没有防御）会把脏数据留在缓存里串给后来者。
                return hit.model_copy(deep=True)
        return self._select_once(job, client, system, user, key, temp)

    def _select_call(self, job: Job, client: LLMClient, system: str, user: str,
                     temp: float | None, key: str | None) -> TopicPlan:
        """真的打一次选题模型：记「选题策划」这一步，并在有指纹时写进缓存。

        步骤与缓存都收在这里，不在调用方各写一遍 —— 并发路径（`_select_once`
        的 leader / 退回自打的等待者）漏掉哪一个都会出现"花了钱却没留痕"
        或"留了痕却没花钱"，两种都是界面在说谎。
        P0-3：选题只产出 400~650 字正文 + 少量思考，与 write 共用 16000 全额
        预算会让思考量被预算反向推高（主报告 R1）。分阶段预算：select 4000。
        """
        usage: dict = {}
        plan = client.chat_json("select", system, user, TopicPlan,
                                on_retry=self._retry_logger(job),
                                on_delta=self._delta_handler(job, "选题策划"),
                                on_attempt=self._stream_reset(job, "选题策划"),
                                should_abort=self._abort_gate(job),
                                deadline=job.deadline(),
                                usage=usage,
                                max_tokens=4000, temperature=temp)
        if key:
            self._remember_plan(key, plan)
        self._step(job, "select", "选题策划",
                   {"plan": plan.model_dump(), **({"usage": usage} if usage else {})})
        return plan

    _PLAN_REUSE_HINT = ("主题/细分/受众/时长/风格/平台/人设/结尾引导、"
                        "模型或被注入的知识任一变化都会重新选题；「换一版」永远重新选题")

    def _cache_lookup(self, key: str) -> TopicPlan | None:
        with self._plan_lock:
            return self._plan_cache.get(key)

    PLAN_FLIGHT_WAIT = 300.0

    def _select_once(self, job: Job, client: LLMClient, system: str, user: str,
                     key: str | None, temp: float | None) -> TopicPlan:
        """真正打一次选题模型；同一指纹并发时只让第一条去（single-flight）。

        修复前两条同参数的并发生成各自 miss、各自打一次 select —— 白烧一次
        4000 token 的调用，而且第二条等的时间与第一条一样长。
        等到的那条复用领头者的结果（与"复用缓存"同一语义，界面上同样要说出来）；
        领头者失败或被取消时，等待方退回自己打一次 —— 预算闸只是不让它白等。
        """
        if not key:
            return self._select_call(job, client, system, user, temp, None)
        with self._plan_lock:
            flight = self._plan_flight.get(key)
            if flight is None:
                flight = self._plan_flight[key] = threading.Event()
                leader = True
            else:
                leader = False
        if not leader:
            flight.wait(self.PLAN_FLIGHT_WAIT)
            hit = self._cache_lookup(key)
            if hit is not None:
                self._step(job, "select_reuse", "与同参数的另一条生成共用选题（本次未调用模型）",
                           {"plan": hit.model_dump(), "model": client.cfg.model,
                            "hint": self._PLAN_REUSE_HINT})
                return hit.model_copy(deep=True)
            # 领头者没留下东西（失败/取消/超时）：自己打一次，不占用飞行槽
            return self._select_call(job, client, system, user, temp, None)
        evt = flight
        try:
            # 先入缓存（_select_call 里做）、再在 finally 放行等待者：
            # 顺序反过来写的话等待者醒来查不到东西，只能自己再打一次。
            return self._select_call(job, client, system, user, temp, key)
        finally:
            # 无论成功、失败还是被取消，都要放行等待者：
            # 忘了 set() 的话并发的那条会一直等到预算闸把它自己拖死。
            with self._plan_lock:
                self._plan_flight.pop(key, None)
            evt.set()

    # ── 选题复用缓存（方案 3「轻量版」）───────────────────────
    # 指纹取「服务地址 + 模型 + 渲染后的 system + user」，不枚举参数：改主题 / 细分 / 受众 /
    # 时长 / 风格 / 人设 / CTA、改 skill.yaml 模板、改被注入的知识文件（切片也在
    # 这段文本里）、换模型，都会自然改变指纹。枚举式的键表一旦漏一项，
    # 就是"静默复用了一个不匹配的选题" —— 那正是本项目一直在整治的形态。
    # 只在本次应用运行内有效（内存，不落盘）：跨重启的缓存要处理失效与隐私，
    # 而产物里本来就留着 plan，收益不值那个复杂度。
    PLAN_CACHE_MAX = 32

    @staticmethod
    def _plan_key(client: LLMClient, system: str, user: str, intel_key: str = "",
                  ctx: dict | None = None) -> str:
        """选题缓存指纹。

        ⚠ 情报块**整块都不进指纹**，只进它的短键（`intel_key` = 选中项 id + 出处）。
        这是 §2.9 B-3 定死的：整块进指纹的话，"情报刷新"会让**所有**历史版本
        全部失效（每一次刷新都等于重选题，白花一次 4000 token）；
        而只带短键时，选中的还是那几条 → 指纹不变 → 同一条选题仍可复现。

        ⚠ 去掉情报块**不能靠切字符串**（P1）。原来是
        `user.split(PROMPT_HEAD, 1)[0]`，那条注释把「块在提示词**末尾**」
        当成了既定前提，而代码里没有任何地方验证它。`split(…, 1)[0]` 砍掉的是
        「第一次出现 PROMPT_HEAD 之后的**全部**内容」—— 包作者（尤其是通过
        `POST /api/packs/import` 导入的第三方包，产品明确支持的入口）把
        `$intel_block` 放在开头或中间时，被砍掉的不只是情报块，而是
        **主题 / 时长 / 细分 / 受众**这些决定选题的全部上下文，剩下的 `base`
        变成一段静态前缀 → 同一包内**任意两次生成都算出同一个指纹** →
        换主题拿到上个主题的 plan，界面上一切正常。
        （实测：块在末尾时两主题同键=False；块在开头时=True。仓库自带两个包
        都放对了，所以测试全绿。）

        现在改成：**把 ctx 里除 `intel_block` 之外的所有键按名排序后序列化**。
        无论块放在哪，指纹都完整；块的内容仍然只由 `intel_key` 决定要不要进指纹，
        §2.9 B-3 的原意一点没变。

        base_url 必须在指纹里：只算 model 名的话，用户在设置里把同一个模型名
        换到另一家的服务（deepseek 官方 ↔ 第三方中转，实测同名不同质量），
        reload_llm() 之后仍会命中上一家的选题缓存 —— 那是"静默用了另一个模型的产物"。
        """
        # ctx 为空（老调用方 / 测试直接调本函数）时退回切字符串那条老路，
        # 但仍按「块在末尾」的假设处理 —— 那是内置两个包的真实形状。
        if ctx is None:
            base = user.split(PROMPT_HEAD, 1)[0] if intel_key else user
            body = base
        else:
            # 只序列化**非情报**的上下文。`intel_block` 整个排除（它每次刷新
            # 内容都变，进指纹等于让缓存永不命中），`intel_key` 单独拼在后面。
            plain = {k: v for k, v in ctx.items() if k != "intel_block"}
            body = json.dumps(plain, ensure_ascii=False, sort_keys=True, default=str)
        raw = (f"{client.cfg.base_url}\n{client.cfg.model}\n{system}\n{body}\n"
               f"{intel_key}").encode("utf-8")
        return hashlib.sha1(raw).hexdigest()

    def _step_intel(self, job: Job, pr) -> None:
        """把"情报注进去了 / 被摘掉了"记进作业日志 —— 两件都要看得见。

        注入本身是**静默**的（提示词里多一截没人知道），被禁用词摘掉的条目更静默。
        两者都不记的话，事后没法回答"这条选题到底有没有参考情报"。
        """
        if pr.intel_dropped:
            self._step(job, "intel_drop",
                       f"{len(pr.intel_dropped)} 条情报因含禁用词未注入提示词",
                       {"titles": pr.intel_dropped[:6],
                        "hint": "含 banwords hard 词的条目进正文层会让模型照抄后被判违规"})
        keys = [k for k in str(pr.intel_key or "").split(",") if k]
        if keys:
            self._step(job, "intel_inject", f"注入 {len(keys)} 条情报（只带文号与出处，不带原文）",
                       {"count": len(keys)})

    def _remember_plan(self, key: str, plan: TopicPlan) -> None:
        with self._plan_lock:
            self._plan_cache[key] = plan
            while len(self._plan_cache) > self.PLAN_CACHE_MAX:
                # dict 保序 → 先进先出。上不封顶会让长跑的引擎攒住整段会话的选题。
                self._plan_cache.pop(next(iter(self._plan_cache)))

    def _draft_once(self, job: Job, pack: Pack, skill: dict, p: dict,
                    client: LLMClient) -> tuple[TopicPlan, dict, dict]:
        """合并包（stages.draft）的首调用：选题与正文一次拿回（方案 10）。

        与两段路径的三处不同，都是**有意**的：
          - 不走 plan 缓存（P1-8 的"复用上次选题"省的是 select 那一通调用；
            合并路径本来就没有单独的 select 可省，缓存只会留下第二本账）；
          - 不设 4000 的 select 预算 —— 一个思考阶段要同时装下选题与正文，
            用 write 的全额预算（cfg.max_tokens）；
          - 「换一版」照旧提温 0.25，但作用在这唯一一次调用上。
        返回 `(plan, draft_dict, usage)`；draft_dict 与 write 路径的
        `ScriptDraft(...).model_dump()` 同形，回炉循环不用区分来源。
        """
        pr = PromptRenderer(pack, data_dir=self.data_dir)
        ctx = pr.draft_ctx(p)
        system, user = self._render_stage(job, pr, "draft", ctx)
        self._step_intel(job, pr)
        temp = (min(1.0, float(client.cfg.temperature) + 0.25)
                if p.get("reroll") else None)
        usage: dict = {}
        bundle = client.chat_json(
            "draft", system, user, DraftBundle,
            on_retry=self._retry_logger(job),
            on_delta=self._delta_handler(job, "选题与撰写"),
            on_attempt=self._stream_reset(job, "选题与撰写"),
            should_abort=self._abort_gate(job),
            deadline=job.deadline(),
            usage=usage, temperature=temp)
        plan = bundle.plan
        draft = ScriptDraft(sections=bundle.sections,
                            alternatives=bundle.alternatives).model_dump()
        # 与 write 路径同一条规矩：阶段完成后再记步骤（界面把 steps 一律画成已完成）。
        self._step(job, "draft", "选题与撰写（一次调用）",
                   {"plan": plan.model_dump(),
                    **({"usage": usage} if usage else {})})
        return plan, draft, usage

    def _write_with_recheck(self, job: Job, pack: Pack, skill: dict,
                            p: dict, plan: TopicPlan,
                            client: LLMClient, *,
                            prebuilt: tuple[dict, dict] | None = None
                            ) -> tuple[dict, list[dict]]:
        ban = Banwords(pack.banwords_data())
        quota = Quota.from_pack(pack.data)
        tells = _load_tells(pack)
        tolerance = pack.duration_tolerance()   # 包没配就是 None → 时长自适应
        # limits.recheck_rounds：写错（0 / 负数 / 非数字）时**不能让回炉循环
        # 一次都不跑** —— 修复前 rounds=0 会让 draft 保持空 dict，最后以
        # 「行业包配置缺少字段：'sections'」收场（真因在 limits，且已经白烧一次
        # select 的钱）。下限夹到 1（= 不回炉），并把这个降级摊到作业日志。
        raw_rounds = (skill.get("limits") or {}).get("recheck_rounds", 2)
        # 先一律按**数值**理解（float 同时吃 2 / 2.0 / "2" / "2.9"），再决定留哪种痕。
        # 修复前用 `raw_rounds == 0` 和 `isinstance(raw_rounds, (int, float))` 判，
        # 于是 YAML 里最常见的两种写法 "0" 与 "2.9" 一条痕都不留 ——
        # 包作者以为配了 2.9 轮，实际跑的是 2 轮，且没有任何地方说过这件事。
        as_num: float | None
        if isinstance(raw_rounds, bool):
            # true/false 是 int 的子类：int(True) == 1 会被安静地当成"配了 1 轮"
            as_num = None
            self._step(job, "limits_bad", "回炉次数配置的是布尔值（不是数字），本次不回炉",
                       {"recheck_rounds": raw_rounds})
        else:
            try:
                as_num = float(raw_rounds)
            except (TypeError, ValueError):
                as_num = None
                self._step(job, "limits_bad", "回炉次数配置不是数字，本次不回炉",
                           {"recheck_rounds": raw_rounds})
        if as_num is None:
            extra = 0
        else:
            extra = int(as_num)
            if extra != as_num:
                # 2.9（或 "2.9"）→ 2：静默截断要说一声
                self._step(job, "limits_bad",
                           f"回炉次数 {raw_rounds} 不是整数，已按 {extra} 处理",
                           {"recheck_rounds": raw_rounds})
            elif extra == 0:
                self._step(job, "limits_zero", "本包配置为不回炉（recheck_rounds: 0）",
                           {"hint": "校验不合格时直接出终版，不追加改写轮"})
            elif extra < 0:
                self._step(job, "limits_bad", f"回炉次数配置为 {extra}（负数），已按 0 处理", {})
                extra = 0
        rounds = 1 + extra
        pr = PromptRenderer(pack)
        revisions: list[dict] = []
        feedback = ""
        draft: dict = {}
        # fact gate 要比对的"上一版"（只在 rnd>1 才有意义；首轮 `prev_sections` 是空的，
        # 上面那条 `if rnd > 1 and prev_sections` 就是挡这个的 —— 没有上一版就无所谓丢失）
        prev_draft: dict = {}
        prev_report: dict = {}
        prev_sections: list[dict] = []
        for rnd in range(1, rounds + 1):
            self._stop_check(job)     # 回炉每一轮前先看有没有让停
            if prebuilt is not None and rnd == 1:
                # 合并路径（方案 10）：选题与正文已在 stages.draft 那一次调用里
                # 完成并记过步骤，这里直接拿它的产物进校验 —— 不再 transition、
                # 不再渲染、不再重复记一步"文案撰写"。回炉轮照旧走 stages.write。
                draft, _ = prebuilt
            else:
                job.transition("writing" if rnd == 1 else "rewriting")

                ctx = pr.write_ctx(p, plan.model_dump(), feedback)
                system, user = self._render_stage(job, pr, "write", ctx)
                # 「换一版」：同主题同参数重掷，小幅提温换取不同表达（仍受校验约束）
                temp = None
                if p.get("reroll"):
                    temp = min(1.0, float(client.cfg.temperature) + 0.25)
                draft_usage: dict = {}
                draft = client.chat_json(
                    "write", system, user, ScriptDraft,
                    on_retry=self._retry_logger(job), temperature=temp,
                    on_delta=self._delta_handler(
                        job, "回炉改写" if rnd > 1 else "文案撰写"),
                    on_attempt=self._stream_reset(
                        job, "回炉改写" if rnd > 1 else "文案撰写"),
                    should_abort=self._abort_gate(job),
                                    deadline=job.deadline(),
                    usage=draft_usage).model_dump()
                # 步骤必须在阶段「完成后」再记：界面把 steps 一律渲染为已完成，
                # 若在开始前就记录，会出现「已完成的文案撰写」与「文案撰写中」并存。
                # usage（P3-15）：上游回的 token 量原先被 `if not choices: continue`
                # 连同末尾 chunk 一起丢掉 —— 思考吃多少、前缀缓存命中多少全靠抓包才知道。
                self._step(job, f"write_r{rnd}", "文案撰写" if rnd == 1 else f"回炉改写·第 {rnd - 1} 轮",
                           ({"feedback": feedback} if feedback else {})
                           | ({"usage": draft_usage} if draft_usage else {}))

            job.transition("checking")
            report = check_script(draft["sections"], p["duration"], p["rate"], ban,
                                  p["platform"], quota, tells=tells,
                                  tolerance=tolerance)
            self._step(job, f"check_r{rnd}", f"校验·第 {rnd} 轮", {"report": report})
            # A-9 **fact gate**（调研报告-20260928 §3.1 #2）：回炉改写不许把具体信息改丢了。
            # `SCOPE_INSTRUCTION` 三档都写着「文号、数字、引文与 {{待补}} 不许动」，但那
            # 只是给模型的话，**没有本地校验** —— 这一步就是那条本地校验。
            # 拒收的判据刻意保守：只在"这一版仍然不合格、又把事实改丢了"时作废本轮
            # （回到上一版继续，下一轮的反馈里点名它改丢了什么）。
            # ⚠ 反过来"这版合格了但少了一个数字"**照收**并留痕 —— 合规结论优先：
            #   数字丢了人还能补，留着硬违规（如「政府补贴」）是要出事的。
            lost = (fact_survival(prev_sections, draft.get("sections") or [])
                    if rnd > 1 and prev_sections else [])
            if lost and not report["passed"]:
                self._step(job, f"fact_gate_r{rnd}",
                           f"回炉第 {rnd - 1} 轮改丢了 {len(lost)} 处具体信息，且没修好任何问题 —— "
                           "本轮作废、退回上一版，下一轮按要求把原样放回去",
                           {"lost": lost[:10]})
                draft, report = prev_draft, prev_report
                feedback = self._violation_feedback(
                    report, prev_sections, p.get("rewrite_scope", DEFAULT_REWRITE_SCOPE),
                    lost_specific=lost)
                if rnd == rounds:
                    draft["check"] = report
                    # ⚠ 退回上一版也要把候选版过一遍校验：`ScriptResult.alternatives`
                    #   要的是过滤后的 `[{sections, check}]`（schemas.py:256），而模型给的
                    #   是 `list[sections]`。这条出口原先漏了这一步，整个作业以 pydantic
                    #   校验失败收场（烧完全部轮次才炸）—— 形状只能在**一处**被定义。
                    self._filter_alternatives(job, draft, p, ban, quota, tells, tolerance, rnd)
                    return draft, revisions
                continue
            if lost:
                self._step(job, f"fact_gate_r{rnd}",
                           f"回炉第 {rnd - 1} 轮改丢了 {len(lost)} 处具体信息 —— "
                           "本轮已收下（合规优先），请人工核回",
                           {"lost": lost[:10], "passed": report["passed"]})
            if report["passed"] or rnd == rounds:
                draft["check"] = report
                self._filter_alternatives(job, draft, p, ban, quota, tells, tolerance, rnd)
                return draft, revisions
            feedback = self._violation_feedback(report, draft.get("sections") or [],
                                                p.get("rewrite_scope", DEFAULT_REWRITE_SCOPE))
            # 走到这里 = 本轮收下并还要再改一轮：把"上一版"三件套留给下一轮的 fact gate。
            prev_draft, prev_report, prev_sections = draft, report, draft.get("sections") or []
            # `action` 是给人看的，`action_code` 才是给界面分流用的。修复前只有
            # `action`，而 result.js 拿中文字面量去筛"哪些版本是全文回炉" ——
            # 改一句中文文案（不改任何行为）就会让那条横幅静默消失，而 UI 桩是
            # 手抄同一句中文的，于是门禁照绿（第 9 轮扫字面量时抓到）。
            revisions.append({"round": rnd, "report": report,
                              "action": REVISION_ACTION_FULL_RECHECK,
                              "action_code": REVISION_CODE_FULL_RECHECK})
        return draft, revisions

    def _filter_alternatives(self, job: Job, draft: dict, p: dict, ban: Banwords,
                             quota: Quota, tells, tolerance, rnd: int) -> None:
        """P3-42：候选版逐个过校验，过的进候选（附各自 check），不过的被拒并说明为什么少了一版。

        ⚠ 为什么抽成函数而不是就地写两段：`ScriptResult.alternatives` 的契约是
        **过滤后**的 `[{sections, check}]`（`app/schemas.py:256`），而模型给的是
        `list[sections]`。这段过滤原先只在"收下"那条出口里跑，A-9 的 fact gate 多开了一条
        返回出口（末轮拒收）就漏了它 —— 整个作业以 pydantic 校验失败收场，
        而且是在钱烧完之后。形状只能在一处被定义。
        默认空 = 单稿路径，这条循环不跑，零额外开销。
        """
        alts = draft.get("alternatives") or []
        if not alts:
            return
        kept = []
        for i, cand in enumerate(alts, 1):
            crep = check_script(cand, p["duration"], p["rate"], ban,
                                p["platform"], quota, tells=tells, tolerance=tolerance)
            if crep["passed"]:
                kept.append({"sections": cand, "check": crep})
            else:
                self._step(
                    job, f"alt_reject_r{rnd}",
                    f"候选版 {i} 未过校验，未进候选（{len({h['word'] for h in crep['hard_hits']})}"
                    f" 处硬违规 / 时长偏差 {crep.get('deviation_pct', 0):+.1f}%）",
                    {"i": i, "check": crep})
        draft["alternatives"] = kept

    @staticmethod
    def _violation_feedback(report: dict, prev_sections: list[dict] | None = None,
                            scope: str = DEFAULT_REWRITE_SCOPE,
                            lost_specific: list[str] | None = None) -> str:
        """回炉反馈（Self-Refine 的 critique）：**可执行的修改清单 + 上一版正文**。

        修复前这里只给统计数字（「命中硬禁用词「绝对安全」×1」）且**不带上一版**，
        于是每一轮回炉是「重新掷骰」而不是「改骰子」—— 花 12~130 秒换出另一篇稿子
        （主报告 §三、P2-41）。现在两处一起补：
          - 命中处带段号与原句（checker.locate_hits），模型知道改哪一句；
          - 附上上一版全文（口播正文实测只有 250~450 字，代价远小于一次白跑的重掷）。

        `lost_specific` = fact gate 抓到"上一轮改丢了什么"（A-9）。点名放在清单**第一行**：
        它比文风问题优先级高 —— 数字与文号是稿子唯一的事实支撑，改丢了就是编造的另一面。
        """
        lines = []
        if lost_specific:
            shown = "、".join(str(x) for x in lost_specific[:12])
            lines.append(f"- 上一版里有、**你上一轮改丢了**的具体信息：{shown}。"
                         "把原样放回去（不许换个说法、不许改成约数、不许删掉）—— "
                         "数字、文号、引文与 {{待补}} 占位是稿子里唯一不能「顺手改」的东西")
        if report["hard_hits"]:
            words = "、".join(f"「{h['word']}」×{h['count']}" for h in report["hard_hits"])
            lines.append(f"- 命中硬禁用词：{words}。必须删除或改成行业里站得住脚的具体说法"
                         f"（换同义词躲过检查不算改，宁可删掉这半句）")
            for h in report["hard_hits"]:
                for at in h.get("at") or []:
                    lines.append(f"  · 第 {at['segment']} 段：…{at['line']}…")
        if report.get("soft_hits"):
            soft = "、".join(f"「{h['word']}」×{h['count']}" for h in report["soft_hits"][:6])
            lines.append(f"- 广告法软禁用词（不阻塞合格，但绝对化表述本身有合规风险）：{soft}，"
                         f"改为有依据的具体说法")
            for h in report["soft_hits"][:4]:
                for at in h.get("at") or []:
                    lines.append(f"  · 第 {at['segment']} 段：…{at['line']}…")
        # 容差取本次校验真正用的那个值 —— P3-3（2026-10-11）：优先**未舍入**的
        # `tolerance_pct_exact`（checker 判合格用的同一个数，deviation_pct_exact
        # 的对称键）；旧产物/桩没有这个键时回落舍入的 `tolerance_pct`。
        # 写死 10% 会让 15 秒档出现「报告说合格、反馈却催你改长度」的自相矛盾；
        # 退回只读舍入键则 |dev| 落在 (round(limit,1), limit] 窗口（<0.05pp）时
        # checker 判合格、这里复判成超 —— 同一句自相矛盾换侧复活。
        limit = report.get("tolerance_pct_exact")
        if limit is None:
            limit = report.get("tolerance_pct")
        limit = 10.0 if limit is None else float(limit)
        # P3（2026-10-05）：复判用 checker 判合格时的**同一个未舍入数**
        # （`deviation_pct_exact`，checker 现在随报告外发）。旧写法拿舍入后的
        # deviation_pct 复判 —— 偏差贴着容差边界（±0.05pp）时 checker 判
        # 不合格、这里复判成"没超"，时长条目不进反馈块，模型只收到兜底句。
        dev_exact = report.get("deviation_pct_exact")
        if dev_exact is None:
            dev_exact = abs(report["deviation_pct"])   # 旧产物/桩没有 exact 键
        if abs(dev_exact) > limit:
            lines.append(f"- 时长偏差 {report['deviation_pct']:+.1f}%（容差 ±{limit:g}%）："
                         f"目标 {report['duration_target']} 秒，"
                         f"当前约 {report['estimated_seconds']} 秒（{report['chars_total']} 字，"
                         f"配额 {report['target_total']} 字），请按每段配额增删内容")
        # 段落配额现在按要点数均分（见 checker.check_script），这条反馈才真正会触发；
        # 修复前每段拿到的是「整个正文配额」，阈值高到几乎不可能命中，是死代码。
        for i, seg in enumerate(report["segments"], 1):
            q = seg.get("quota")
            if q and seg["chars"] > q * 1.3:
                lines.append(f"- 第 {i} 段（{seg['type']}）超配额：{seg['chars']} 字"
                             f"（配额≈{q}），请压缩")
        # 人味（文风）建议：**不影响本次是否合格**，只是这次改写顺手能修的地方。
        # 与 banwords 的分工：那边不合规必须改，这边像不像人说话由人决定。
        #
        # ⚠ 只点名 strong 命中（A-9 §3.1 #3）：weak 不进点名清单。理由不是"weak 不重要"，
        #   而是**回炉的目标函数**——朱雀逆向经验与 shuorenhua 的 bounded 档同一条：
        #   "只改标红段，自己写的部分一字不动"；把 weak 也塞进点名，等于每轮让模型
        #   去动那些本来就正常的段落，改出来的稿子越改越平（"强追写得干瘪"）。
        #   weak 的**总数**照样说，只是不逐条点名 —— 隐藏数据不是收敛噪音。
        ai = (report.get("ai_tells") or {}).get("hits") or []
        strong_ai = [h for h in ai if h.get("severity") == STRONG]
        weak_ai = [h for h in ai if h.get("severity") != STRONG]
        if strong_ai:
            lines.append("- 以下文风问题不阻塞合格，但这次改写请顺手改自然些"
                         f"（只点名「{severity_word(STRONG)}」的那几处，其余段落别动）：")
            for h in strong_ai[:6]:
                lines.append(f"  · [{severity_word(h['severity'])}] {h['id']}（{h['where']}，"
                             f"{h['count']} 处）：{h['detail']}")
        if weak_ai:
            # 「条」与「处」是两回事：一条 tell 在一篇里可以命中多处，扣分按**条**
            # （需求方案 §2.1 的公式）。混着印会让人按 −4×处 反推分数。
            weak_at = sum(int(h.get("count") or 0) for h in weak_ai)
            lines.append(f"- 另有 {len(weak_ai)} 条「{severity_word(WEAK)}」文风提示"
                         f"（合计 {weak_at} 处；扣分按条不按处）"
                         " —— 未点名的段落**不要动**，改文风改不出人味，改具体才改得出")
        # 空清单的兜底句必须判在静态规矩**之前**：两条规矩是无条件追加的，
        # 判空写在它们后面 = 那句"未通过校验，请按…"永远说不出口（A-9 审查 P3）。
        if not lines:
            lines.append("- 未通过校验，请按口语化与合规要求改写")
        # 两条静态规矩放在**动态内容之后、上一版全文之前**（每轮变的问题清单与正文在最后）：
        # 与 `$feedback_block` 位于 user_template 末尾的前缀缓存规矩同向（P1-40）。
        lines.append(HUMAN_VOICE_RULE)
        # in-place 档的"人声补回"要与"不许加句"对齐（审查报告-20260930 M3'）
        if scope in VOICE_SCOPE_NOTE:
            lines.append(VOICE_SCOPE_NOTE[scope])
        lines.append(NEGATIVE_INJECTION_RULE)
        # 收尾句按档分：structural 的"可以重排"不能与"一个字都不要动"同现
        # （审查报告-20260930 M1）
        lines.append(NEGATIVE_TAIL.get(scope, NEGATIVE_TAIL["default"]))
        if prev_sections:
            body = "\n".join(f"  第 {i} 段（{s.get('type', '')}）：{s.get('text', '')}"
                             for i, s in enumerate(prev_sections, 1))
            lines.append(f"- 上一版全文（{SCOPE_INSTRUCTION[scope]}）：\n" + body)
        return "\n".join(lines)

    def _storyboard(self, job: Job, pack: Pack, draft: dict, p: dict,
                    client: LLMClient) -> list[dict]:
        """P1-30：分镜独立阶段，只在正文定稿、校验通过后跑一次。

        并回 write 的代价实测是 5~10 倍输出量（画面描述与口播同等长度），
        而且每一轮回炉都要重做一遍与校验无关的分镜。拆出来后时间轴由
        `_compute_timings` 按字数算好喂给模型 —— 模型只出创意，不做算术。
        """
        self._stop_check(job)
        job.transition("storyboarding")
        sections = draft["sections"]
        timings = self._compute_timings(sections, p["rate"])
        pr = PromptRenderer(pack)
        ctx = pr.storyboard_ctx(sections, timings)
        system, user = self._render_stage(job, pr, "storyboard", ctx)
        sb_usage: dict = {}
        sb = client.chat_json("storyboard", system, user, StoryboardDraft,
                              on_retry=self._retry_logger(job),
                              on_delta=self._delta_handler(job, "分镜生成"),
                              on_attempt=self._stream_reset(job, "分镜生成"),
                              should_abort=self._abort_gate(job),
                              deadline=job.deadline(),
                              usage=sb_usage,
                              max_tokens=4000)
        self._step(job, "storyboard", "分镜生成",
                   {"shots": len(sb.storyboard), "sections": len(sections),
                    **({"usage": sb_usage} if sb_usage else {})})
        if len(sb.storyboard) != len(sections):
            # 分镜与段落一一对应是 scenes 的组装前提（_build_scenes 按下标配对）。
            # 数量对不上不报错，但必须留痕：否则界面会出现某段没有画面而没人知道为什么。
            self._step(job, "storyboard_mismatch", "分镜段数与口播段数不一致",
                       {"storyboard": len(sb.storyboard), "sections": len(sections)})
        return [s.model_dump() for s in sb.storyboard]

    @staticmethod
    def _compute_timings(sections: list[dict], rate: float) -> list[dict]:
        """按段落字数推算时间轴；重写/回炉后必须重算。

        停顿算在**段落之间**（每处 0.5 秒，最后一段之后不计），与
        `checker.estimate_seconds` 同一条公式 —— 修复前这里给首段 +0.5 秒而
        段落间不计，于是校验说「60 秒的片子 63 秒，合格」，界面上的时间轴却
        只排到 61.5 秒：两处口径差 1.5 秒，没人能解释为什么对不上。
        """
        timings, t = [], 0.0
        last = len(sections) - 1
        for i, s in enumerate(sections):
            n = count_chars(s["text"])
            dur = n / rate + (0.5 if i < last else 0.0)
            timings.append({"start": round(t, 1), "end": round(t + dur, 1)})
            t += dur
        return timings

    @staticmethod
    def _build_scenes(sections: list[dict], storyboard: list[dict],
                      timings: list[dict], style: str = "") -> list[dict]:
        """组装统一产物「场景序列 Scene[]」。

        投影关系：口播 = narration · 分镜 = visual.prompt / audio.sfx
        契约见 docs/场景序列契约.md；与 sections/storyboard/timings 并行输出。

        `style` 取本次生成的**风格参数**（用户界面选的那个），不劳模型再猜一遍：
        它在 sections 里已经确定，让模型每镜重述只会多花钱还容易漂。
        """
        scenes = []
        for i, (s, tm) in enumerate(zip(sections, timings)):
            shot = storyboard[i] if i < len(storyboard) else {}
            scenes.append({
                "scene_id": f"s{i + 1}",
                "type": s["type"],
                "start": tm["start"],
                "end": tm["end"],
                "narration": s["text"],
                "subtitle": s.get("subtitle", "") or "",
                "visual": {
                    "prompt": shot.get("shot", ""),
                    "source": "generated",
                    # `or "cut"` 而不是 `get(..., "cut")`：分镜模型现在总是带这个键
                    # （StoryboardShot 有默认值，model_dump 不会省），模型留空时
                    # 取到的是 ""，契约里 transition 的默认值是 cut。
                    "transition": shot.get("transition") or "cut",
                },
                "audio": {
                    "bgm": shot.get("bgm", ""),
                    "sfx": shot.get("sfx", ""),
                },
                "shot_type": shot.get("shot_type", ""),
                # 模型每镜给的风格标签优先；没给就用本次生成的风格参数（用户选的），
                # 而不是留一个空槽让下游自己猜。
                "style": shot.get("style") or style,
            })
        return scenes

    def _finalize(self, job: Job, pack: Pack, p: dict, plan: TopicPlan,
                  draft: dict, storyboard: list[dict],
                  revisions: list[dict], client: LLMClient) -> None:
        self._stop_check(job)          # 关键检查点：组装前再确认一次
        sections = draft["sections"]
        # P1-47：**没有正文的产物不算成功**。修复前 `{"sections": []}` 能一路走到这里，
        # `check_script([])` 在容差够宽时返回 passed=True，于是作业以 done 收场、
        # 落一份空 result.json、历史索引记下 chars:0 —— 界面显示"完成"，点开是空的。
        # （P1-42「假成功」那一族的结构层版本：正文为空不等于正文合格。）
        if not any(_readable(s.get("text") or "") for s in sections):
            raise EmptyContentError(
                "模型没有产出任何可读正文（sections 为空、全是空白，或只剩 {{待补}} 占位），"
                "本次不生成产物。这通常是推理模型的思考吃光了输出预算：换非推理档，"
                "或降低行业包里的 limits.recheck_rounds。")
        # P1-30：分镜来自独立阶段（voice 时 _continue_write 已传 []）。
        timings = self._compute_timings(sections, p["rate"])
        scenes = self._build_scenes(sections, storyboard, timings,
                                    str(p.get("style") or ""))
        full_text = "\n".join(s["text"] for s in sections)
        placeholders = sorted(set(re.findall(r"\{\{([^}]+)\}\}", full_text)))

        raw = {
            "id": job.id, "created_at": job.created_at,
            "pack": pack.dir.name, "pack_draft": pack.draft,
            # P1-23：口径取**真正用了哪条通道**，不是取 Pipeline.mock ——
            # 设置里把 api_key 填成 "MOCK"（或模型行 enabled 走 mock）时
            # LLMClient.mock 为真而 Pipeline.mock 仍为假，于是夹具产物
            # 落盘成 mock: false，统计又一次被夹具污染（正是这条修复要防的）。
            "mock": bool(self.mock or client.mock),
            "params": {k: p[k] for k in PERSISTED_PARAMS},
            "quota": p["quota"],
            # 字数配额是「按 时长×语速 估算」而不是查表得来的 —— 必须随产物一起
            # 落盘，否则界面看到的 `quota: {total: 256, ...}` 与包作者真正配过的
            # 配额完全无法区分（P2-8：信号算对了却没人接）。
            # 用 `p[...]` 而不是 `p.get(...)`：键缺失时这里必须炸，
            # 「缺了就当成没降级」正是本项目一直在整治的那种静默。
            "quota_degraded": bool(p["quota_degraded"]),
            "plan": plan.model_dump(),
            "sections": sections,
            "storyboard": storyboard,
            "scenes": scenes,
            "check": draft["check"],
            "placeholders": placeholders,
            "revisions": revisions,
            "timings": timings,
            "logs": list(job.steps),
            # P3-42：候选版（换一版时模型多给的、过了校验的那些）。
            "alternatives": draft.get("alternatives") or [],
        }
        # 契约校验：修复前 ScriptResult / SceneItem 定义了却从未实例化，
        # docs/场景序列契约.md 的约定没有任何代码强制，字段漂移无人发现。
        check_contract_complete(raw)
        result = ScriptResult.model_validate(raw).model_dump()

        # 先落盘、后置 done —— 顺序一旦反了，不变量就是破的。
        #
        # 修复前是「先 transition('done') 再 write_result」：状态一变，轮询的界面
        # 与删除接口就认为作业结束了，而此刻产物还有一半没写（历史索引甚至尚未落盘）。
        # 实测的两个后果：
        #   - 前端一看到 done 就回读记录 → 索引还没写，记录凭空消失；
        #   - 此刻调用删除 → 目录删掉后 write_atomic 的 mkdir(parents=True)
        #     又把它建回来，只写了一半的产物留在磁盘上，记录当场复活。
        # 现在倒过来：产物完整落盘之后状态才允许变成 done，
        # 「done ⟹ 磁盘上已有完整产物」这才真正成立。
        self._stop_check(job)
        try:
            if not self.store.write_result(result, job.work_dir):
                # 落盘前记录已被删除（用户在生成途中删了这条）—— 就此收手。
                # 继续走下去会变成「done 但磁盘上没有产物」的空壳记录。
                return
        except Exception as e:  # noqa: BLE001
            # 落盘失败必须让状态跟着变成失败，否则内存说「完成」而磁盘上没有产物，
            # 用户点历史回看只会得到一个 404。
            # ⚠ 这里**不能**调 `store.delete()`：那会立墓碑，随后 `_fail` 的
            #   `_persist` 被墓碑拦住 → 这条失败记录只活在内存里，重启就没了，
            #   「产物落盘失败」这个原因也就丢了（批次 10 复核实测）。
            #   半个产物的清理归 `store.write_result` 自己做（它才知道写了哪几个文件）。
            job.transition("failed", force=True, error=f"产物落盘失败：{e}")
            # 抛出去的不是原异常：外层 `_fail` 会用 `_readable_error(e)` 覆盖 error，
            # 而 OSError("磁盘满") 进去就只剩「磁盘满」—— 用户看不出是**产物**没写进去
            # （会以为是模型/网络的问题）。把语境放进异常本身，谁接手都不丢。
            raise RuntimeError(f"产物落盘失败：{e}") from e
        if not job.transition("done", result=result):
            # 落盘期间用户按了停止：产物刚写进去，但作业已取消 —— 撤掉，
            # 否则历史里会留下一条「已停止却带着完整产物」的幽灵记录。
            # ⚠ 回收网**不参与**这里（第 10 轮复核）：它只把额度让出来、从不把活作业
            #   改成终态。所以这条 transition 失败只可能是用户真点了停止。
            self.store.delete(job.id)
            return
        self.registry.prune()

    def _run_rewrite_segment(self, job: Job, pack: Pack, index: int, feedback: str,
                             client: LLMClient) -> None:
        try:
            self._stop_check(job)
            result = job.result
            p = result["params"]
            # 深拷贝一份再改：修复前直接原地改 job.result["sections"]，
            # 一旦中途取消，内存里的结果已经是半改状态（产物文件倒是旧的）。
            sections = json.loads(json.dumps(result["sections"], ensure_ascii=False))
            seg = sections[index]
            q = result["quota"]
            n_points = max(sum(1 for s in sections if s["type"] == "point"), 1)
            seg_quota = {"hook": q.get("hook", 0), "point": q.get("body", 0) // n_points,
                         "cta": q.get("cta", 0)}[seg["type"]]
            pr = PromptRenderer(pack)
            ctx = pr.rewrite_ctx(sections, index, seg_quota, feedback)
            system, user = self._render_stage(job, pr, "rewrite_segment", ctx)
            # P0-3：单段重写输出一段口播，3000 预算足够，不占 write 的全额预算。
            rw_usage: dict = {}
            new = client.chat_json("rewrite_segment", system, user, SegmentRewrite,
                                   on_retry=self._retry_logger(job),
                                   on_delta=self._delta_handler(job, "单段重写"),
                                   on_attempt=self._stream_reset(job, "单段重写"),
                                   should_abort=self._abort_gate(job),
                                   deadline=job.deadline(),
                                   usage=rw_usage,
                                   max_tokens=3000)
            old = seg["text"]
            # P1-44 第三条腿：**空/空白的新稿不许覆盖掉已经合格的那一段**。
            # 修复前这里直接 `text: new.text` —— 思考吃光预算但 finish_reason=stop
            # 的模型（实测存在）会回一个 `{"text": ""}`，于是这次"改进"把一段好稿子
            # 就地抹成空白，作业照样以 done 收场（P1-42 的形态搬到重写）。
            if not (new.text or "").strip():
                raise EmptyContentError(
                    "单段重写返回了空正文，已保留原文（没有覆盖任何东西）。"
                    "这通常是推理模型的思考吃光了输出预算：换非推理档，或降低回炉轮数。")
            sections[index] = {**seg, "text": new.text,
                               **({"subtitle": new.subtitle} if new.subtitle else {})}
            ban = Banwords(pack.banwords_data())
            quota = Quota.from_pack(pack.data)
            report = check_script(sections, p["duration"], p["rate"], ban, p["platform"],
                                  quota, tells=_load_tells(pack),
                                  tolerance=pack.duration_tolerance())
            timings = self._compute_timings(sections, p["rate"])
            self._step(job, "rewrite_segment", f"重写第 {index + 1} 段",
                       {"report": report, **({"usage": rw_usage} if rw_usage else {})})

            # P1-30：改写过的段落要重画分镜 —— 旧分镜的时间轴是按改写前的字数
            # 算的，画面也对不上新文案了。重画失败不报废这次改写：
            # 正文已经通过校验，沿用旧分镜只是画面略旧。
            storyboard = result.get("storyboard") or []
            if p.get("format") != "voice" and \
                    ((pack.skill() or {}).get("stages") or {}).get("storyboard"):
                try:
                    storyboard = self._storyboard(
                        job, pack, {"sections": sections}, p, client)
                except JobCancelled:
                    raise
                except Exception as e:  # noqa: BLE001
                    self._step(job, "storyboard_skip", "分镜重画失败，沿用原有分镜",
                               {"error": str(e)})

            updated = {
                **result,
                "sections": sections,
                "storyboard": storyboard,
                "check": report,
                "timings": timings,
                "scenes": self._build_scenes(sections, storyboard, timings,
                                             str(p.get("style") or "")),
                "revisions": list(result.get("revisions", [])) + [
                    {"segment": index, "from": old, "to": new.text, "feedback": feedback}],
                "logs": list(job.steps),
            }
            updated = ScriptResult.model_validate(updated).model_dump()

            self._stop_check(job)
            # 与 _finalize 同样的顺序：先落盘、后置 done。
            if not self.store.write_result(updated, job.work_dir):
                return          # 记录已被删除，不必再写
            if not job.transition("done", result=updated):
                # 落盘期间被停止：把重写前的内容写回去。这里不能像 _finalize 那样
                # 直接删产物 —— 那是本次新建的；而重写是在既有记录上改，
                # 删掉会把用户原来那条记录一起抹掉。
                self.store.write_result(result, job.work_dir)
                return
        except JobCancelled:
            # 重写被中止：内存里的结果已经被丢弃（上面做了深拷贝），产物文件没动过，
            # 服务端状态留在 cancelled，用户下次打开仍是重写前的内容。
            # `error=None` 是刻意的：这条记录**本来就有过一份合格产物**，
            # 留着上一次的 error 会让历史卡片显示"失败"，而失败的那次重写什么也没改。
            job.update(error=None)
            self._settle_cancel(job)
        except Exception as e:  # noqa: BLE001
            self._fail(job, e)

    # ── 杂项 ────────────────────────────────────────────────
    def _fail(self, job: Job, e: Exception) -> None:
        """把作业落到 failed。

        **已取消的作业不在这里改写状态。** 取消之后线程可能还会因为别的原因抛错
        （比如流式连接被我们主动掐断，httpx 抛出 RequestError），
        如果无条件 force 成 failed，「用户点了停止、界面却显示生成失败」这个
        假停止的老问题就会回来。取消不是错误，日志里留一条就够了。
        """
        if job.is_cancelled():
            log.info("作业 %s 在取消后抛错（已忽略，状态保持 cancelled）：%s", job.id, e)
            return
        job.transition("failed", force=True, error=_readable_error(e))
        self._persist(job)
        self.registry.prune()          # 回收早期终态作业，避免长跑进程内存无界增长

    def _retry_logger(self, job: Job):
        """接口自动重试时记录到作业日志（界面「日志」页签可见）"""
        def cb(note: str, attempt: int, total: int):
            self._step(job, "retry", f"接口自动重试·第 {attempt}/{total} 次", {"note": note})
        return cb

    def _settle_cancel(self, job: Job) -> None:
        """`except JobCancelled` 分支的统一收尾（P1-46）。

        真取消：`request_cancel()` 已把状态落成 cancelled，这里什么都不做
        （写回反而是覆盖）。
        但不是取消（只剩「整作业超预算」这一条路，或者将来有人再把两种信号
        混成一种异常）：必须落 failed —— 停在 selecting/writing/packing 这类
        忙态的作业不是终态，`prune()` 不回收，那个并发额度就永久没了。
        实测形态：四个这样的作业之后，引擎在**一个都没在跑**的情况下一直说已达上限。
        """
        if not job.is_cancelled():
            self._fail(job, JobBudget(_jobs.JOB_BUDGET_SECONDS))

    def _discard_created_pack(self, job: Job, slug: str) -> None:
        """取消掉的建包：把**这一次**刚建出来的 `packs/<slug>/` 回收掉。

        只删这一个路径，且必须还落在 `packs/` 里 —— 它不是通用删除工具。
        删失败（文件被占用等）只留日志：取消本身已经生效，不该因为回收没干净
        再把作业改成 failed（那会是另一种假失败）。
        """
        base = (self.root / "packs").resolve()
        target = (base / slug).resolve() if slug else None
        if not target or target == base or not target.is_relative_to(base):
            log.warning("取消收尾：本次目录路径不像建包产物，跳过删除：%s", target)
            return
        if not target.is_dir():
            return                      # 取消得早，压根没建出来
        # rmtree_resilient 而不是裸 shutil.rmtree：Windows 上杀软正扫着刚写好的
        # 那批文件时，单次删除会抛"文件被占用"，而这里的后果和建包失败时一模一样
        # —— 目录留在盘上、下次同名提交被 409 挡住。同一件事只准有一本账
        # （app/packgen.py 的失败清理早就用这个）。
        if rmtree_resilient(target):
            log.info("取消已生效：本次创建的 packs/%s 已回收", slug)
        else:
            log.warning("取消收尾：packs/%s 回收失败 —— 包目录会留在盘上，"
                        "下次同名提交会报「行业包已存在」，需要人工删除", slug)
            # 只写日志等于没写：引擎日志在界面上是折叠的过程步骤，而"下次同名建不了、
            # 应用里又没地方删包"这件事用户必须当场看到（第 21 轮复核 P2）。
            self._step(job, "packgen_reclaim_failed",
                       f"本次目录未能回收：请手动删除 packs/{slug}，"
                       "否则同名重建会一直报「行业包已存在」（应用里没有删包的入口）",
                       {"slug": slug})

    @staticmethod
    def _abort_gate(job: Job):
        """交给 llm 层的收工闸：每次 HTTP 尝试前 + 流式每一行前查一次（P1-45）。

        与 `_delta_handler` 里那次检查的区别是**触发条件**：那一次只有真正收到
        可见增量才跑，所以「连接活着但上游一个字都不给」的流（排队、只发
        `: keep-alive`、只推空 delta）永远查不到取消 —— 而 httpx 的读超时在这种
        流上也不会生效（字节一直在到）。实测后果：作业状态早已 cancelled，
        工作线程还在收包，一个并发额度被占死；四个这样的作业就把引擎锁到重启。
        抛的是 `_stop_check` 的两种异常之一：取消 → JobCancelled（不是失败），
        超预算 → JobBudget（要落成 failed 并说清原因）。
        """
        def cb() -> None:
            Pipeline._stop_check(job)
        return cb

    @staticmethod
    def _delta_handler(job: Job, phase: str):
        """返回流式回调：把模型增量写进 job，界面据此实时显示思考过程。

        P1-7 修复前「收到第一个增量才 begin_stream」：select 首字节前静默几十秒，
        快照没有 stream 键，思考块被 progress.js 隐藏，界面只剩「已用 N 秒」，
        用户以为卡死。现在**构造时就 begin_stream**，并且 `Job.snapshot` 的
        stream 键门禁看的是 `stream_phase`（app/jobs.py）—— 首字节前也在下发。

        这里的 begin_stream 只负责**进入阶段**那一次清零；每次 HTTP 尝试前的
        清零由 `_stream_reset` 交给 llm 层调用（P2-13）。
        """
        job.begin_stream(phase)

        def cb(kind: str, text: str):
            # 停止要在「收包过程中」就生效：等整段创建完再取消，钱已经花完了。
            # 抛出去会顺着 httpx 的流式迭代一路冒到工作线程的 except JobCancelled。
            Pipeline._stop_check(job)
            job.push_delta(kind, text)

        return cb

    @staticmethod
    def _stream_reset(job: Job, phase: str):
        """P2-13：每次真正的 HTTP 尝试开始前清空流式缓冲。

        修复前 `_delta_handler` 每**轮**只求值一次，而 `_complete` 的网络/429
        重试与 `chat_json` 的解析重试复用同一个闭包 —— 被丢弃的那次尝试的思考
        字数照样累进 `stream_reasoning_len`，界面上的「思考 N 字」在发生过重试时
        是两遍之和（原注释还声称已经每轮清空了）。现在按尝试清零：
        显示的永远是**当前这一版**的量。
        """
        def cb(_note: str = "", _attempt: int = 0, _total: int = 0) -> None:
            # 每一次真正的 HTTP 尝试都记一次"还在往前走"。读超时已被 `_capped_read` 夹到
            # 剩余预算，所以没有这一行时"两次检查点之间的最长间隔"也只是一个整作业预算
            # （1200s），仍在回收网门限（2× 预算 = 2400s）之内 —— 第 15 轮复核据此把我
            # 先前写的"最坏 ~5400s"判为错数。这一行因此是**余量**，不是那道门唯一站得住
            # 的理由：确定性代码（写盘、校验）静默时它同样管用。
            job.touch()
            job.begin_stream(phase)
        return cb

    def _step(self, job: Job, key: str, title: str, data: dict):
        job.touch()          # 记一步 = 有进展（回收网看的是这个，不是 started_at）
        job.steps.append({"key": key, "title": title, "data": data,
                          "ts": datetime.now().isoformat(timespec="seconds")})

    @staticmethod
    def _stop_check(job: Job) -> None:
        """阶段之间的收工检查点：用户点了停止 → JobCancelled；整作业预算用尽
        → JobBudget（P1-5）。

        两者必须分开抛：取消不是错误（状态落 cancelled、界面不该说「失败」），
        超预算是失败（要落成 failed 并说清原因）。合并成一种的话，上游卡死会被
        报成「用户点了停止」—— 那是同一类假信号。
        """
        # 走到检查点本身就是"还在往前走"的证据；放在两条抛错之前，
        # 这样"被预算判停"的作业最后一次进展时刻仍是真的最后一次。
        job.touch()
        if job.is_cancelled():
            raise JobCancelled()
        if job.overdue():
            # 读模块属性而不是本文件的 from-import 副本：`overdue()` 判的是
            # `jobs.JOB_BUDGET_SECONDS` 的**实时值**，文案若读 import 那一刻的副本，
            # 改了值就会出现「按 A 判超、按 B 报分钟数」的两本账。
            raise JobBudget(_jobs.JOB_BUDGET_SECONDS)

    def _spawn(self, job: Job, fn):
        """起后台线程跑作业主体；**起不来就先把作业落成终态再抛**（P1-46）。

        能走到这一行说明并发额度已经占上了（`add_if_room` / `transition_if_room`
        都过了），而 `JobRegistry.prune()` 只回收**终态**作业。原样抛出会留下一条
        永远停在 queued / rewriting 的记录：实测失败三次（上限 4）之后，
        界面变成「一个作业都没在跑，但生成一直报已达上限」，只能重启引擎。
        线程起不来的真实原因有：线程资源耗尽、`store.job_dir` 建不出来（磁盘满 /
        权限）、`self.llm` 惰性构建时配置文件读不出来。
        """
        try:
            threading.Thread(target=self._guarded(job, fn), daemon=True).start()
        except Exception as e:  # noqa: BLE001
            self._fail(job, e)
            raise

    def _guarded(self, job: Job, fn):
        """包一层：作业主体**穿出来的一切**都必须把作业落到终态。

        各 worker 自己兜的是 `Exception`；`KeyboardInterrupt` / `SystemExit` /
        `MemoryError` 之外的解释器级异常（`BaseException` 支系）会从那些 except
        上面穿过去，线程死了而作业还停在忙态 —— `prune()` 只回收终态，
        那个并发额度就永久没了（P1-46 同族；第 6 轮复实在 `_run_packgen` 上
        实测到抛 `SystemExit` 之后 `state` 一直是 `packing`）。
        """
        def run():
            try:
                fn()
            except BaseException as e:  # noqa: BLE001
                if job.state not in TERMINAL_STATES:
                    try:
                        self._fail(job, e if isinstance(e, Exception)
                                   else RuntimeError(f"作业线程被 {type(e).__name__} 打断"))
                    except BaseException:  # noqa: BLE001
                        # 连"记失败"都失败（第 7 轮复核：归因里一次 `str()` 抛错就够）时，
                        # 绝不能把作业留在忙态 —— `prune()` 只回收终态，那个并发额度
                        # 就永久没了，症状还是那句"一个都没在跑，但生成一直报已达上限"。
                        #
                        # ⚠ 但这条兜底**不许覆盖已经记对的东西**（第 8 轮复核实测）：
                        #   让 `registry.prune` 抛一下，作业里原本那句
                        #   「行业包配置缺少字段：'quota_table'」就被换成
                        #   「…状态收口时二次出错」—— 一次不相干的收尾失败把可执行的
                        #   原因抹掉了，正是这个函数自己声明要避免的事。
                        #   同理，`_fail` 之后取消才落进来时不能把 cancelled 改成 failed
                        #   （取消不是失败）。所以：重查一次状态，有 error 就留着。
                        if job.state in TERMINAL_STATES or job.is_cancelled():
                            log.warning("作业 %s 收口时二次出错，但状态已落定，保持原状",
                                        job.id)
                        else:
                            kept = _safe_str(getattr(job, "error", "") or "", swallow_all=True)
                            job.transition("failed", force=True,
                                           error=kept or
                                           f"作业失败（{type(e).__name__}）·详情看引擎日志")
                            log.exception("作业 %s 收口失败，已强制落 failed", job.id)
                raise
        return run

    def _persist(self, job: Job):
        """落盘作业快照。失败不阻断流程，但必须留痕。

        修复前这里是 `except Exception: pass` —— 磁盘满或权限不足时，
        job.json 静默不写，用户看不到任何迹象，事后也查不到原因。

        **建包作业（kind=packgen）没有产物目录，也不落盘**：它不是一条脚本，
        快照写进 `generated/<日期>/` 会在历史索引里留下一条点不开的"会话"，
        失败记录还会被 `_upsert` 当真记录统计。它的过程与结果只活在内存注册表里，
        界面靠 `/api/jobs/{id}` 取 —— 与服务重启后"重新填一遍再建"的语义一致。
        """
        if job.work_dir is None:
            return
        try:
            self.store.write_job(job.snapshot(), job.work_dir)
        except Exception as e:                  # noqa: BLE001
            log.warning("作业 %s 快照落盘失败：%s", job.id, e)


def _readable(text: str) -> str:
    """剥掉 `{{待补：…}}` 占位、空白与标点后还剩多少"能播的字"。

    判"这篇稿子是不是空的"必须把占位算成空：全篇只有占位的稿子，用户拿去录
    就是对着麦克风念"待补"，与没有产物是同一件事（P1-47 的第三条路，本轮实测）。
    """
    t = re.sub(r"\{\{[^}]*\}\}", "", text or "")
    return re.sub(r"[\s、，。：:；;！!？?\-—·.。,]+", "", t)


def _safe_str(obj, swallow_all: bool = False) -> str:
    """取异常的可读文本，取不到就当没有。

    第 7 轮复核实测：`str()` 本身可以抛（自定义异常的 `__str__` 里再出错），
    而 `_readable_error` 是在**收尾失败的收尾代码**里被调用的 —— 它一抛，
    `_guarded` 就没机会把作业落到终态，于是忙态永久占着一个并发额度
    （`prune()` 只回收终态）。归因是"锦上添花"，绝不能反过来把收口挡住。

    ⚠ 两处调用要的是相反的东西，第 9 轮复核把这一点证出来了：
      - 最外层那次（`msg = _safe_str(e)`）宁可放行：`KeyboardInterrupt` /
        `SystemExit` 是"用户或解释器在说话"，吞掉等于 Ctrl-C 失灵；
      - **异常链里**那些环节必须一律吞掉：链上一句 `__str__` 抛 `SystemExit`
        若往外传，整段归因就半路作废，作业虽然仍落到 failed（第 8 轮的兜底管住了槽），
        但 `error` 从「产物落盘失败」退化成「详情看引擎日志」—— 那正是第 8 轮
        立誓要保护的"可执行的原因"。所以链内传 `swallow_all=True`。
    """
    try:
        return str(obj).strip()
    except Exception:  # noqa: BLE001
        return ""
    except BaseException:
        if swallow_all:
            return ""
        raise


#: `'str' object has no attribute 'get'` 这一整族消息才谈得上"数据形状"：
#: 接收者是包 yaml 里写得出来的类型（标量/容器），而被调的那个方法是我们**对着配置数据**
#: 调的那几个。两个条件缺一就不得把责任推给行业包 —— 见 `_readable_error` 里那条账。
_NO_ATTR_RE = re.compile(r"'([A-Za-z_][\w]*)' object has no attribute '([^']+)'")
_DATA_TYPES = {"str", "int", "float", "bool", "list", "tuple", "dict", "set", "frozenset",
               "NoneType", "bytes"}
_DATA_METHODS = {"get", "items", "keys", "values", "strip", "lstrip", "rstrip", "startswith",
                 "endswith", "split", "replace", "format", "join", "append", "extend",
                 "isdigit", "lower", "upper", "splitlines", "pop", "update", "setdefault"}


def _readable_error(e: Exception) -> str:
    """把异常转成用户能看懂的一句话。

    修复前直接把 str(e) 丢给界面：缺 quota_table 的包会让用户看到
    「list index out of range」，完全无从下手。pydantic 与类型转换的
    英文原文一样不该见人 —— 实测过三种会直达 toasts 的形态：
      `'int' object has no attribute 'items'`（badly-typed 词表/映射）
      `could not convert string to float: '六十'`（参数写成中文数字）
      `cannot use 'dict' as a set element`（词表条目写成映射）
    """
    msg = _safe_str(e) or type(e).__name__
    # 归因不能只留最后一层：预算闸门在「请求已经出错」之后抛出时，
    # 界面上就只剩"超过 20 分钟…"，而真正的起因（一个坏 URL、一次连接失败）
    # 没有任何地方说（批次 10 复核实测）。异常链里带着它，就把它一起说出去。
    #
    # ⚠ 只走一层不够（第 6 轮复核实测）：`RuntimeError("产物落盘失败")
    #   from OSError("") from ValueError("quota_table 第 3 行缺少 count 键")`
    #   这条真链上中间那层的消息是**空串**，而 `__cause__ or __context__` 偏好
    #   真值对象 —— 于是界面只剩「产物落盘失败」五个字，根因一个字都没露。
    #   所以要往里走到第一个"有话可说"的环节。
    node = e
    chain = []
    for _ in range(5):
        node = node.__cause__ or node.__context__
        if node is None:
            break
        chain.append(node)
    for c in chain:
        cs = _safe_str(c, swallow_all=True)
        if not cs:
            continue
        head = cs[:120]
        # 只在真能补一点信息时才追加：外层文案常常已经把原文塞进去了
        # （`_brief(str(e))` 截到 160 字），再补一遍就是两本账。
        # ⚠ 比的是**截断后的那一段**而不是完整 `cs`：外层带进来的本来就是被
        #   `_brief` 砍过的原文，用完整 `cs not in msg` 判"没说过"，实测会把
        #   同一段话印两遍（160 + 120 字重复）。取 120 字与要打印的长度同源。
        # ⚠ 不再用"类型名在不在 msg 里"当去重条件（第 7 轮复核抓到）：那是对
        #   整句话做子串匹配，外层只要顺嘴提了一句 `ValueError`，真根因就被丢掉。
        #   空消息的环节上面已经 `continue` 掉了，这一条本来就是多余的宽判。
        if head in msg:
            break
        msg = f"{msg}（上一步：{head}）"
        break
    if isinstance(e, IndexError):
        return f"行业包配置不完整（{msg}）：请检查 pack.yaml 的 quota_table / params 字段"
    if isinstance(e, KeyError):
        return f"行业包配置缺少字段：{msg}"
    # `AttributeError` 的归属必须看**形状**，不能看"消息里有没有 has no attribute"（§9.8 的账）：
    # 原来那一条把两者一律说成「行业包配置类型不对」，于是测试桩少一个属性（
    # `'Scripted' object has no attribute 'mock'`）也被指到包作者头上，而根因只是代码内部缺陷。
    m = _NO_ATTR_RE.search(msg)
    if m:
        owner, attr = m.group(1), m.group(2)
        if owner in _DATA_TYPES and attr in _DATA_METHODS:
            return (f"行业包配置类型不对（{type(e).__name__}）：某处写成了数字/字符串，"
                    f"期望是映射（如词表条目、audience_map 的值）。原文：{msg[:120]}")
        return (f"程序内部错误（{type(e).__name__}）：`{owner}` 上没有 `{attr}` —— "
                f"这个形状不是行业包能改出来的，请带这句话报 bug。原文：{msg[:120]}")
    if "could not convert string to float" in msg:
        return f"数字参数写成了非数字（如「六十」），请改成纯数字。原文：{msg[:120]}"
    if "as a set element" in msg:
        return (f"行业包配置类型不对：词表条目写成了映射而不是字符串。"
                f"原文：{msg[:120]}")
    return msg


def _load_tells(pack: Pack):
    """包级文风 tell 集；包没配 `ai_tells.yaml` 时返回 None。

    None 与"配了但一条都没开"是两件事：前者让校验报告的 `ai_tells` 字段落 null
    （没测），后者才是真的量过、分数为满分。混起来 = 把"没测"报成"很好"。
    """
    data = pack.ai_tells_data()
    return None if data is None else AITells(data)


def wait_job(pipeline: Pipeline, jid: str, timeout: float = 120.0) -> dict:
    """测试辅助：等待作业结束（含取消）。"""
    import time
    deadline = time.time() + timeout
    while time.time() < deadline:
        snap = pipeline.get_job(jid).snapshot()
        if snap["state"] in TERMINAL_STATES:
            return snap
        time.sleep(0.2)
    raise TimeoutError(jid)
