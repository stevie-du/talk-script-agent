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

from .ai_tells import AITells
from .checker import Banwords, Quota, check_script, count_chars
from .config import AppConfig, load_config
from . import jobs as _jobs                              # noqa: F401
from .jobs import (JOB_BUDGET_SECONDS, TERMINAL_STATES, Job,  # noqa: F401
                   JobBudget, JobCancelled, JobRegistry, StateConflict,
                   new_job_id)
from .knowledge import Pack, PackError, param_audit  # noqa: F401
from .llm import EmptyContentError, LLMClient
from .packgen import claim_slug, create_pack, preview_slug, release_slug
from .prompts import PromptRenderer
from .schemas import (GenerateRequest, RewriteSegmentRequest,
                      ScriptResult, ScriptSection, StoryboardShot, TopicPlan)
from .store import ArtifactStore


class ScriptDraft(BaseModel):
    """模型在「撰写」阶段要返回的结构。

    P1-30：只出 sections —— 分镜拆成了独立阶段（见 `_storyboard`），
    正文定稿并校验通过后才生成（实测拆分后 write 从 110~127s 降到 12~23s）。
    """
    sections: list[ScriptSection]


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
# 4) 运行期由 `_run_generate` 附加（P3-14），**不进 `_normalize`** ——
#    `_normalize` 是纯参数归一，不读 self.llm（测试直接调它时会没有客户端）。
#    它们最终仍落进 result.json 的 params（PERSISTED_PARAMS 已含）。
RUNTIME_PARAMS: tuple[str, ...] = ("model", "max_tokens", "temperature")


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
        if slug and (self.root / "packs" / slug).exists():
            raise FileExistsError(f"行业包已存在：{slug}")
        # P2-46 后半：名字要在**起作业之前**占住。占不到就是同步 409 ——
        # 让两条同名请求都跑到模型那一步再一败一成，等于白烧一份 token
        # 并且让用户等一两分钟才知道自己重名（修复前 `_creating` 的占用
        # 发生在模型调用之后，正是这个形态）。
        if not claim_slug(slug):
            raise FileExistsError(f"行业包正在创建中：{slug}")
        # 占位从这一行起就要有人还。原来 `try` 从 `self.llm` 才开始，于是
        # `new_job_id()` / `Job(...)` / `add_if_room()` 任何一处抛（第 6 轮复核
        # 实测：强行让 `new_job_id` 抛）都会留下一个**永不归还**的 slug ——
        # 那个行业名此后永远建不出来，而报错写着「行业包正在创建中」，
        # 只能重启引擎。归还做成幂等的：`release_slug` 用 discard。
        # ⚠ 归还点**只能有这一个**：内层再 release 一次，若这期间别人抢到了同名
        #   占位，第二次 discard 会偷走别人的锁。
        try:
            jid = new_job_id()
            job = Job(jid, "packgen", {"industry": industry,
                                       "description": (description or "").strip()})
            # 建包没有产物目录：job.json 不落 generated/，不进历史索引
            # （它不是一条脚本，出现在左栏会话列表里只会让人找不到）。
            if not self.registry.add_if_room(job, MAX_CONCURRENT_JOBS):
                raise StateConflict(
                    f"同时进行的任务已达上限（{MAX_CONCURRENT_JOBS} 个），请等其中一个完成后再试")
            # `self.llm` 是惰性构建的（会读 data_dir 下的配置文件），所以它**也可能抛**：
            # 放在 try 外面等于给「配置坏了」留一条漏占位的路径。
            client = self.llm               # P1-6：作业级抓一次，中途不换配置
            self._spawn(job, lambda: self._run_packgen(job, client, slug))
        except BaseException:
            release_slug(slug)
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
            p["model"] = client.cfg.model
            p["max_tokens"] = client.cfg.max_tokens
            p["temperature"] = client.cfg.temperature
            job.transition_or_raise("selecting", params=p)
            plan = self._select(job, pack, skill, p, client)
            self._stop_check(job)
            self._continue_write(job, pack, skill, plan, client)
        except JobCancelled:
            self._settle_cancel(job)
        except Exception as e:  # noqa: BLE001
            self._fail(job, e)

    def _continue_write(self, job: Job, pack: Pack, skill: dict, plan: TopicPlan,
                        client: LLMClient):
        try:
            self._stop_check(job)
            p = job.params
            draft, revisions = self._write_with_recheck(job, pack, skill, p, plan, client)
            # P1-30：正文定稿 + 校验通过后，分镜单独生成（voice 模式整个跳过）。
            # ⚠ "校验通过之后"只在**回炉还在跑**的意义上成立：最后一轮不过校验时
            #   `_write_with_recheck` 照样 return（见该方法的 `or rnd == rounds`），
            #   所以不合格也会走到这里画分镜 —— 那一条本来就要以 failed/不合格收场。
            # 老包（本次改造之前建的）没有 storyboard 阶段：跳过分镜而不是让
            # 整条已经写完的脚本以 KeyError 收场。
            # 分镜失败**不报废正文**：正文已通过校验，画面只是没画出来 ——
            # 记 storyboard_skip（带 error）、产物空分镜照常落盘；与单段重写
            # 路径（沿用旧分镜）同口径。修复前这里抛错会走下面的 except →
            # _fail(job)，一条合格的稿子因为画面这一步整条报废。
            storyboard = []
            if p.get("format") != "voice":
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
            self._finalize(job, pack, p, plan, draft, storyboard, revisions, client)
        except JobCancelled:
            self._settle_cancel(job)
        except Exception as e:  # noqa: BLE001
            self._fail(job, e)

    def _run_packgen(self, job: Job, client: LLMClient, slug: str = "") -> None:
        """建包作业的主体（进度 / 取消 / 错误都走作业这一套）。"""
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
            # 取消 —— 目录已经建好就不该假装失败，但状态必须是 cancelled，不能报 done。
            self._stop_check(job)
            if not job.transition("done", result=info):
                return
            self.registry.prune()
        except JobCancelled:
            self._settle_cancel(job)
        except Exception as e:  # noqa: BLE001
            self._fail(job, e)
        finally:
            # 名字是在 `start_packgen` 里、花钱之前就占下的，所以**任何**收尾路径
            # （成功 / 失败 / 取消 / 早 return）都要归还，否则这个 slug 永远建不了。
            release_slug(slug)

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
        def pick(key, fallback=None):
            v = params.get(key)
            return v if v not in (None, "", []) else pack.param_default(key, fallback)

        duration = float(pick("duration", 60))
        style = str(pick("style", ""))
        rate = params.get("rate") or pack.rate_for_style(style)
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
            "pack": pack.name, "topic": str(params.get("topic", "")).strip(),
            "segment": pick("segment"), "audience": pick("audience"),
            "duration": duration, "style": style,
            "platform": pick("platform"), "persona": pick("persona"),
            "cta": pick("cta"), "facts": params.get("facts") or "",
            "rate": float(rate),
            "voice": params.get("voice") if params.get("voice") in ("strong", "standard", "off") else "strong",
            "format": params.get("format") if params.get("format") in ("both", "voice") else "both",
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
        """
        missing = pr.unfilled(stage, ctx)
        system, user = pr.render(stage, ctx)
        if missing:
            self._step(job, f"tpl_{stage}", f"模板占位符未填充（{stage}）",
                       {"missing": missing,
                        "hint": "skill.yaml 的 stages.<阶段>.files 的 key 必须与占位符同名"})
        return system, user

    def _select(self, job: Job, pack: Pack, skill: dict, p: dict,
                client: LLMClient) -> TopicPlan:
        pr = PromptRenderer(pack)
        ctx = pr.select_ctx(p)
        system, user = self._render_stage(job, pr, "select", ctx)
        # P1-8：「换一版」的提温原来只作用于 write —— 于是「换一版」重掷出的
        # plan 与上一版几乎一样，白白花 74~173s。select 同样提温。
        temp = (min(1.0, float(client.cfg.temperature) + 0.25)
                if p.get("reroll") else None)
        key = None if p.get("reroll") else self._plan_key(client, system, user)
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
    def _plan_key(client: LLMClient, system: str, user: str) -> str:
        # base_url 必须在指纹里：只算 model 名的话，用户在设置里把同一个模型名
        # 换到另一家的服务（deepseek 官方 ↔ 第三方中转，实测同名不同质量），
        # reload_llm() 之后仍会命中上一家的选题缓存 —— 那是"静默用了另一个模型的产物"。
        raw = (f"{client.cfg.base_url}\n{client.cfg.model}\n{system}\n{user}").encode("utf-8")
        return hashlib.sha1(raw).hexdigest()

    def _remember_plan(self, key: str, plan: TopicPlan) -> None:
        with self._plan_lock:
            self._plan_cache[key] = plan
            while len(self._plan_cache) > self.PLAN_CACHE_MAX:
                # dict 保序 → 先进先出。上不封顶会让长跑的引擎攒住整段会话的选题。
                self._plan_cache.pop(next(iter(self._plan_cache)))

    def _write_with_recheck(self, job: Job, pack: Pack, skill: dict,
                            p: dict, plan: TopicPlan,
                            client: LLMClient) -> tuple[dict, list[dict]]:
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
        for rnd in range(1, rounds + 1):
            self._stop_check(job)     # 回炉每一轮前先看有没有让停
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
            if report["passed"] or rnd == rounds:
                draft["check"] = report
                return draft, revisions
            feedback = self._violation_feedback(report, draft.get("sections") or [])
            revisions.append({"round": rnd, "report": report, "action": "全文回炉"})
        return draft, revisions

    @staticmethod
    def _violation_feedback(report: dict, prev_sections: list[dict] | None = None) -> str:
        """回炉反馈（Self-Refine 的 critique）：**可执行的修改清单 + 上一版正文**。

        修复前这里只给统计数字（「命中硬禁用词「绝对安全」×1」）且**不带上一版**，
        于是每一轮回炉是「重新掷骰」而不是「改骰子」—— 花 12~130 秒换出另一篇稿子
        （主报告 §三、P2-41）。现在两处一起补：
          - 命中处带段号与原句（checker.locate_hits），模型知道改哪一句；
          - 附上上一版全文（口播正文实测只有 250~450 字，代价远小于一次白跑的重掷）。
        """
        lines = []
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
        # 容差取本次校验真正用的那个值（checker 的 tolerance_pct）。
        # 写死 10% 会让 15 秒档出现「报告说合格、反馈却催你改长度」的自相矛盾。
        limit = report.get("tolerance_pct")
        limit = 10.0 if limit is None else float(limit)
        if abs(report["deviation_pct"]) > limit:
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
        ai = (report.get("ai_tells") or {}).get("hits") or []
        if ai:
            lines.append("- 以下文风问题不阻塞合格，但这次改写请顺手改自然些：")
            for h in ai[:6]:
                lines.append(f"  · [{h['severity']}] {h['id']}（{h['where']}，{h['count']} 处）："
                             f"{h['detail']}")
        if not lines:
            lines.append("- 未通过校验，请按口语化与合规要求改写")
        if prev_sections:
            body = "\n".join(f"  第 {i} 段（{s.get('type', '')}）：{s.get('text', '')}"
                             for i, s in enumerate(prev_sections, 1))
            lines.append("- 上一版全文（**在它基础上按上面几条逐处修改**，"
                         "不要另起一炉重写、不要改动了未点名的段落）：\n" + body)
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
            "pack": pack.name, "pack_draft": pack.draft,
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
            job.begin_stream(phase)
        return cb

    def _step(self, job: Job, key: str, title: str, data: dict):
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
    if "has no attribute" in msg:
        return (f"行业包配置类型不对（{type(e).__name__}）：某处写成了数字/字符串，"
                f"期望是映射（如词表条目、audience_map 的值）。原文：{msg[:120]}")
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
