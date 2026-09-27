# -*- coding: utf-8 -*-
"""产物落盘与历史索引。

拆出来的理由：pipeline 原来既跑流程又拼文件路径、又写 JSON、又拼 Markdown，
「作业目录到底在哪」这件事在两个方法里各算了一次（`now()` vs `created_at`），
于是跨零点的作业会分裂成两个目录，而删除只删掉一个。

现在目录只由 `job_dir()` 一个函数决定，创建作业时算一次，之后全程复用。

历史索引
--------
修复前 `GET /api/history` 每次都要 glob 出所有 result.json 并**完整解析**每一份，
而前端在生成期间每 3 秒调一次 —— 100 条记录就是每 3 秒几 MB 的 JSON 解析。
更糟的是失败作业没有 result.json，只写了 job.json 却**从未被读取**，
重启后从历史里彻底消失。

现在维护一份 `generated/index.json`（只有摘要字段），history 只读它；
一旦发现磁盘上的产物数量与索引对不上（崩溃、手工拷入、旧版本数据），
自动重建一次，保证索引永远不是「真相的来源」而只是缓存。
"""
from __future__ import annotations

import json
import logging
import math
import re
import threading
from pathlib import Path

from .fileio import rmtree_resilient, write_atomic

log = logging.getLogger(__name__)

INDEX_NAME = "index.json"
# v2：摘要多了 platform（会话列表副标题要显示「平台」）。
# ⚠ 必须跟着升版本号 —— 老索引里的条目**没有** platform 字段，
# 不重建的话列表永远显示不出平台，而这与「用户当时真没选平台」在界面上
# 长得一模一样（参见审查基线里的「静默降级」）。_read_index_file 见到
# 版本不符会走 _rebuild()，从 result.json/job.json 重新摘一次。
INDEX_VERSION = 3   # v3：摘要加 `segment`（左栏搜索按细分领域筛要用它）

# 墓碑只用于拦住「删除后仍在跑的作业」，不需要长期留存。
# ⚠ 但它**有上限就等于有一个洞**：被挤掉的 id 若还有一趟在飞的作业，
# 它随后的 write_result 会把用户删掉的记录重新建出来（批次 10 复核实测：
# 再删 512 条之后就能复现）。桌面端一次会话删不满这个数，所以取一个
# "实际不可达"的量而不是换更复杂的机制：8192 个 id 常驻约 **1 MB**
# （tracemalloc 实测 1031 KB，一条 id 22 字符 —— 早先按 250 KB 估是把它
# 当成了纯字符串总量，忘了 set 的槽位开销）。这比给删除路径加持久化标记
# 的代价小得多。真要彻底闭合，得让作业侧在落盘前问一次注册表
# "这条还在不在"，那是另一件事。
_TOMBSTONE_CAP = 8192


def _day_dir(created_at: str) -> str:
    return (created_at or "")[:10].replace("-", "") or "00000000"


class ArtifactStore:
    def __init__(self, root: Path, data_dir: Path | None = None):
        self.root = root
        # 产物目录：开发态就是项目根的 generated/（已被 .gitignore 忽略）；
        # 打包后安装目录通常不可写，由 Electron 传 --data-dir 指到用户数据目录。
        self.gen = (data_dir or root) / "generated"
        # 已删除的作业 id。落盘前查一次：删除「正在生成」的记录时，后台线程
        # 迟早会走到 write_result，若不拦，目录会被重新建出来，记录当场复活。
        # 只记 id、且有上限 —— 它只需要覆盖「删除发生在落盘之前」这一个小窗口。
        self._tombstones: set[str] = set()
        self._tombstone_order: list[str] = []
        # RLock：_upsert 持锁时会走 _read_index_file → _rebuild → _write_index，
        # 而 _write_index 自己也要持锁（把并发写串行化，避免 Windows 上
        # 两个线程同时 replace 同一个索引文件）。普通 Lock 会在这里自锁死。
        self._lock = threading.RLock()

    # ── 目录 ────────────────────────────────────────────────
    def job_dir(self, jid: str, created_at: str) -> Path:
        """作业产物目录。唯一来源，全流程复用同一个结果。"""
        d = self.gen / _day_dir(created_at) / jid
        d.mkdir(parents=True, exist_ok=True)
        return d

    def find_dirs(self, jid: str) -> list[Path]:
        """所有含该作业的目录（正常情况下只有 1 个；历史遗留可能多个）。"""
        if not self.gen.exists():
            return []
        return [p for p in self.gen.glob(f"*/{jid}") if p.is_dir()]

    # ── 写入 ────────────────────────────────────────────────
    def write_result(self, result: dict, job_dir: Path) -> bool:
        """整份产物一次写完，返回是否真的写了（记录已被删除则返回 False）。

        与 `delete` 互斥（同一把锁）。

        加锁的理由不是「文件读写需要锁」（write_atomic 本身是原子的），
        而是**删除与落盘不能交错**：delete 先删掉目录，write_result 随后
        `mkdir(parents=True)` 又把目录建回来，只写了一半的产物留在磁盘上，
        记录当场复活。锁 + 墓碑共同保证：删除一旦发生，后续的落盘一定不会生效。
        """
        with self._lock:
            if job_dir.name in self._tombstones:
                return False
            # 先把**所有**要写的内容算完，再动磁盘：修复前 result.json 先落盘，
            # 随后 `_summary_from_result` / `render_script_md` 里任何一个 KeyError
            # 都会留下"状态是 failed、盘上却有一份完整 result.json"的记录
            # —— 而 `result.json 存在 ⟺ done` 是历史索引与左栏判断的不变量。
            summary = self._summary_from_result(result)
            md = render_script_md(result)
            payload = json.dumps(result, ensure_ascii=False, indent=1)
            # 覆盖之前先把**旧内容**留在手里。`write_result` 不只被"新建"调用：
            # 单段重写与"取消后回滚到重写前"两条路径都是在**一份已经存在的合格产物**
            # 上覆盖写（pipeline.py 的两处调用点）。那时第二个文件写失败如果直接
            # 把第一个删掉，用户手上就什么都没有了 —— 撤错东西比不撤更糟。
            prev: dict[Path, str | None] = {}
            try:
                for name, body in (("result.json", payload), ("脚本.md", md)):
                    f = job_dir / name
                    prev[f] = f.read_text(encoding="utf-8") if f.exists() else None
                    write_atomic(f, body)
            except Exception:
                # 两个文件是分开的原子写：中途失败就回到这次调用开始前的样子，
                # 索引还没动，所以不用回滚索引。
                for f, old in prev.items():
                    try:
                        if old is None:
                            f.unlink(missing_ok=True)
                        else:
                            write_atomic(f, old)
                    except OSError:
                        log.warning("落盘失败后的回滚也没成功，%s 可能处于半成品状态", f)
                raise
            self._upsert(summary)
            return True

    def write_job(self, snap: dict, job_dir: Path) -> bool:
        """落盘作业快照（失败/取消的作业没有 result.json，只有 job.json）。

        墓碑检查与 `write_result` 一致 —— 两者都必须拦，只拦一个是没用的：
        `write_atomic` 会 `mkdir(parents=True)`，任何一次落盘都能把删掉的
        目录重新建出来。今天所有 `_persist` 调用点碰巧都有取消检查兜着，
        但那是巧合，不该是这个模块的正确性前提。
        """
        with self._lock:
            if job_dir.name in self._tombstones:
                return False
            write_atomic(job_dir / "job.json",
                         json.dumps(snap, ensure_ascii=False, indent=1))
            # 终态才进索引：运行中的作业由内存里的注册表提供，不落索引，
            # 否则会在「已落盘的记录」和「内存里的作业」之间重复计数。
            if snap.get("state") in ("failed", "cancelled"):
                # 索引 state 的权威源：result.json 已在盘 = 这条有完整产物，
                # state 必须保持 done。修复前这里无条件按 snap.state 覆盖索引，
                # 而失败/取消路径可能在 write_result 之后才走到 write_job ——
                # 一条能正常打开的完成记录被翻成 failed，chars/passed 变 null，
                # 左栏忽而「失败」忽而「完成」（实测两种都出现过）。
                if not (job_dir / "result.json").exists():
                    self._upsert(self._summary_from_job(snap))
            return True

    # ── 读取 ────────────────────────────────────────────────
    def read_result(self, jid: str) -> dict | None:
        for d in self.find_dirs(jid):
            f = d / "result.json"
            if f.exists():
                return json.loads(f.read_text(encoding="utf-8"))
        return None

    def read_job(self, jid: str) -> dict | None:
        """读作业快照（失败/取消的作业没有 result.json，只有 job.json）。

        修复前 job.json 写了却**从未被读取** —— 失败作业在重启后从历史里
        彻底消失，用户既看不到这条记录也不知道它失败过。

        ⚠ 文件**坏了要抛**，不许吞成 None：读不出来与"这条从来没有"是两件事。
        以前这里 `except Exception: return None`，于是 job.json 损坏的记录走
        `/api/history/{id}` 一路落到内存注册表兜底、兜不到就 404「记录不存在」——
        而同一份产物的 result.json 坏了说的是「记录已损坏，无法读取」（`read_result`
        就是不吞的）。同一种坏两种出口，界面就在替文件系统撒一个更省事的谎。
        """
        for d in self.find_dirs(jid):
            f = d / "job.json"
            if f.exists():
                return json.loads(f.read_text(encoding="utf-8"))
        return None

    #: 左栏一次最多取多少条已结束作业。**这个数只有这里一份**：
    #: 以前 `history(limit=100)` 的默认值与 `server.py` 里那句 `history(limit=100)`
    #: 各写一遍，界面要说「只翻了最近 N 条」时就没有可信的 N 可引用（审查流程线 P2-4）。
    HISTORY_LIMIT = 100

    def history(self, limit: int | None = None) -> list[dict]:
        """已结束作业的摘要列表，新的在前。只读索引，不做全量 JSON 解析。"""
        idx = self._read_index_file()
        items = sorted(idx.values(), key=lambda x: x.get("created_at", ""), reverse=True)
        return items[:self.HISTORY_LIMIT if limit is None else limit]

    def history_ids(self) -> set[str]:
        """索引里**全部**记录 id（不受 `HISTORY_LIMIT` 截断）。

        谁需要它：`/api/history` 判"这条是不是已经在盘上了"。用截断后的 100 条
        当已知集，第 101~200 条已落盘的失败记录就会被当成"还在内存里跑的幽灵"
        重发一份 `ghost: true` 摘要，界面据此写「失败 · 未落盘（重启后消失）」——
        而它的 job.json 明明就在盘上（点开还读得出来）。
        """
        return set(self._read_index_file())

    def history_stats(self) -> dict:
        """界面上的「共 N 条 / 只翻了最近 M 条」要的是这一份，不是 `len(items)`。

        为什么必须带出去：搜索框在**已经截断**的 100 条里找不到的时候，
        说「没有这条记录」是假的 —— 它可能就在第 101 条。
        """
        total = len(self._read_index_file())
        return {"total": total, "limit": self.HISTORY_LIMIT,
                "truncated": total > self.HISTORY_LIMIT}

    # ── 删除 ────────────────────────────────────────────────
    def delete(self, jid: str) -> str:
        """删除该作业的**全部**目录与索引项。

        返回 `"ok"` / `"missing"` / `"partial"` —— 三种结果必须分开：
        `partial` 表示索引项没了但产物还在，refresh 之后记录会复活，
        接口层要给得出错提示，不能当成删除成功。

        修复前只删 glob 的第一个命中项就 return：跨零点分裂出的第二个目录
        会留下来，它的 result.json 下次被扫到时这条记录就「复活」了。

        修复后仍有两个坑，一并在这里堵上：
          1. `shutil.rmtree(..., ignore_errors=True)` 会把「没删掉」当成成功，
             接口照样回 200，界面删了行、刷新又回来 —— 现在删不干净就返回
             False，由接口层如实报错；
          2. 与 `write_result` 交错（见该方法的注释），现在全程持锁。
        """
        with self._lock:
            self._remember_tombstone(jid)
            dirs = self.find_dirs(jid)
            if not dirs:
                return "missing"
            removed = [rmtree_resilient(d) for d in dirs]
            cur = self._read_index_file()
            if cur.pop(jid, None) is not None:
                self._write_index(cur)
            # 目录没删干净就**不能**报成功：索引项已经没了，产物却还在，
            # 下次 history() 重建索引时这条记录会原样复活。接口层据此回 500，
            # 让用户知道没删掉，而不是看着它自己回来。
            return "ok" if all(removed) else "partial"

    def _remember_tombstone(self, jid: str) -> None:
        """记下已删除的 id（调用方须持锁）。"""
        if jid in self._tombstones:
            return
        self._tombstones.add(jid)
        self._tombstone_order.append(jid)
        if len(self._tombstone_order) > _TOMBSTONE_CAP:
            self._tombstones.discard(self._tombstone_order.pop(0))

    def reveal_target(self, jid: str) -> Path | None:
        dirs = self.find_dirs(jid)
        return dirs[0] if dirs else None

    # ── 索引维护 ────────────────────────────────────────────
    #
    # 设计取舍：**索引文件是唯一真相**，内存里不再放一份长期缓存。
    # 一开始我让内存缓存做权威，用「磁盘目录数 vs 索引条数」判断是否需要重建 ——
    # 结果被内存缓存自己骗过去了：索引文件被删掉后，条数依然对得上，于是既不重建
    # 也不回写，文件永远是缺的。现在每次都读那个小文件（几十条摘要，几百字节），
    # 缺失/损坏/条数不符就重建，自愈逻辑才真正生效。
    def _index_path(self) -> Path:
        return self.gen / INDEX_NAME

    def _read_index_file(self) -> dict[str, dict]:
        f = self._index_path()
        if not f.exists():
            return self._rebuild()
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            if data.get("version") != INDEX_VERSION:
                return self._rebuild()
            out = {it["id"]: it for it in data.get("items", []) if it.get("id")}
        except Exception:                       # noqa: BLE001
            return self._rebuild()
        # 条数与磁盘上的作业目录数不符（崩溃、手工拷入、旧版本数据）→ 重建
        if self._count_on_disk() != len(out):
            return self._rebuild()
        return out

    def _count_on_disk(self) -> int:
        """磁盘上**已落盘的记录**数 —— 判据必须与索引的口径完全一致。

        索引收录的是「有产物或快照的作业」，所以这里也数
        「含 result.json 或 job.json 的目录」，**不能数目录**。

        为什么不能数目录（修复前的写法是 `sum(1 for p in self.gen.glob("*/*") if p.is_dir())`）：
        `job_dir()` 在 `start_generate` 里就 `mkdir`，作业一开工就有一个**空目录**，
        而它还没进索引。于是只要有一个作业在跑，`len(index) != 目录数` 就**恒成立**，
        每次 `history()` 都会走 `_rebuild()` —— 全量 glob + 解析所有
        result.json/job.json + 重写 index.json。

        而前端在有未结束会话时会每 3 秒轮询一次 `/api/history`
        （`sessions.js` 的 `loadSessions`），于是整个生成期间就是
        **每 3 秒一次 O(N) 解析 + 一次磁盘写**，索引这个优化变成负收益。
        实测：40 条记录 16.4 ms/次，400 条记录 **115.2 ms/次**，
        且重建次数 == 调用次数（5 次调用重建 5 次）。

        改成按「已落盘」计数后，「在跑的作业」不再污染判据：
        每完成一个作业最多触发一次重建（`write_result` 写盘与 `_upsert` 之间
        有一个瞬时窗口），而不是每 3 秒一次。

        注意这里数的**不是文件数**：一个作业会同时留下 result.json 与 job.json，
        按文件数统计会得到 2 倍，与索引条目数永远对不上 —— 那样每次 history()
        都会白重建一次，索引等于没做。
        """
        if not self.gen.exists():
            return 0
        return sum(1 for p in self.gen.glob("*/*")
                   if (p / "result.json").exists() or (p / "job.json").exists())

    def _rebuild(self) -> dict[str, dict]:
        """从磁盘重建索引：job.json 先铺底（含失败/取消），result.json 覆盖它。"""
        out: dict[str, dict] = {}
        if self.gen.exists():
            for f in sorted(self.gen.glob("*/*/job.json")):
                try:
                    s = self._summary_from_job(json.loads(f.read_text(encoding="utf-8")))
                    if s.get("id"):
                        out[s["id"]] = s
                except Exception:               # noqa: BLE001
                    # 单条记录读不出来＝这条历史**凭空消失**，而 index.json 损坏
                    # 还有自愈重建兜着。不记下来就没人知道发生过：用户只看到
                    # 「少了几条」，引擎这边一片安静 —— 正是静默降级。
                    log.warning("历史记录读不出来，已从索引中跳过：%s", f, exc_info=True)
                    continue
            for f in sorted(self.gen.glob("*/*/result.json")):
                try:
                    s = self._summary_from_result(json.loads(f.read_text(encoding="utf-8")))
                    if s.get("id"):
                        out[s["id"]] = s
                except Exception:               # noqa: BLE001
                    log.warning("历史记录读不出来，已从索引中跳过：%s", f, exc_info=True)
                    continue
        self._write_index(out)
        return out

    def _upsert(self, summary: dict | None) -> None:
        if not summary or not summary.get("id"):
            return
        with self._lock:
            cur = self._read_index_file()
            cur[summary["id"]] = summary
            self._write_index(cur)

    def _write_index(self, items: dict[str, dict]) -> None:
        """写索引。失败不影响主流程（它只是缓存，下次读时会重建），但必须留痕。

        持锁写入：HTTP 线程读索引触发重建、同时工作线程在 upsert 时，
        两个写会撞在同一个目标文件上（Windows 上是 `[WinError 5] 拒绝访问`）。
        """
        try:
            payload = json.dumps(
                {"version": INDEX_VERSION,
                 "items": sorted(items.values(), key=lambda x: x.get("created_at", ""))},
                ensure_ascii=False)
            with self._lock:
                self.gen.mkdir(parents=True, exist_ok=True)
                write_atomic(self._index_path(), payload)
        except Exception as e:                  # noqa: BLE001
            log.warning("历史索引写入失败（下次读取时会重建）：%s", e)

    # ── 摘要 ────────────────────────────────────────────────
    @staticmethod
    def _summary_from_result(r: dict) -> dict:
        p = r.get("params") or {}
        c = r.get("check") or {}
        return {
            "id": r.get("id"), "created_at": r.get("created_at", ""),
            "pack": r.get("pack", ""), "topic": p.get("topic", ""),
            # 会话列表副标题现在显示「行业 · 时间 · 平台」，摘要必须带上 platform。
            # ⚠ 这是**两条**摘要路径里的第一条（另一条是 _summary_from_job）——
            # 只补一条的话，已完成与未完成记录的副标题会差一格，而缺的这一格
            # 与「用户当时真没选平台」在界面上长得一模一样（静默降级）。
            "platform": p.get("platform"),
            # 左栏的搜索读 `it.segment`（sessions.js），摘要不给这个键时"按细分领域搜"
            # 永远搜不出东西 —— 搜索框旁边的说明还写着能搜（第 27 轮契约线 P3）。
            "segment": p.get("segment"),
            "duration": p.get("duration"), "chars": c.get("chars_total"),
            "passed": c.get("passed"), "state": "done",
        }

    @staticmethod
    def _summary_from_job(snap: dict) -> dict:
        p = snap.get("params") or {}
        return {
            "id": snap.get("id"), "created_at": snap.get("created_at", ""),
            "pack": p.get("pack", ""), "topic": p.get("topic", ""),
            "platform": p.get("platform"),
            "segment": p.get("segment"),
            "duration": p.get("duration"), "chars": None, "passed": None,
            "state": snap.get("state", "failed"), "error": snap.get("error"),
        }


# ── 人类可读产物 ────────────────────────────────────────────

def _secs(v) -> str:
    """秒数的中文读法：<10 留一位小数，其余先取整再换档（≥60 走「分」）。

    与前端 `desktop/renderer/js/util.js` 的 `sec()` 是同一口径的两份实现
    （一边是界面、一边是导出物）。导出 md 是交付物：同一条时长界面写「60 秒」、
    导出写「60s」，读的人要先做一次心算才敢确定是同一个数。
    `Math.round` 是四舍五入，Python 的 `round` 是银行家舍入（2.5→2），
    所以这里显式 `floor(x+0.5)` 对齐两边 —— 否则同一个数两边能差 1。
    """
    n = float(v)
    if n != n or n < 0:                       # NaN / 缺省值：宁可空着也不说假数
        return ""
    if n < 10:
        return f"{math.floor(n * 10 + 0.5) / 10:g} 秒"
    # 先取整再判档：59.6 秒两边都该读「1 分」（先判档会留成「60 秒」，
    # 与前端 fmtDur 的 round-then-tier 差一格 —— 同一份产物两侧不同数）。
    total = math.floor(n + 0.5)
    if total < 60:
        return f"{total} 秒"
    # 整分不补「0 秒」：与前端同一口径，60 秒读作「1 分」。
    return f"{total // 60} 分 {total % 60} 秒" if total % 60 else f"{total // 60} 分"


def _shot_time(raw, tm: dict) -> str:
    """分镜表「时间」列：只换单位、不改数。

    `time` 由模型自己排（分镜可以比段落更细，引擎不校验它），而 `rules/
    output-template.md` 教的写法是 `0-3s` —— 原样上屏就是同一份交付物里两种
    读法：段落标题写「0-5 秒」、分镜表写「0-3s」，第三方包还照着模板一直交 ASCII。
    认不出这个形状就**原样保留**，不猜内容；完全没有 `time` 才退回引擎算好的段落轴。
    """
    s = str(raw or "").strip()
    if not s:
        if tm.get("start") is None or tm.get("end") is None:
            return ""
        return f"{tm['start']}-{tm['end']} 秒"
    m = re.match(r"^(\d+(?:\.\d+)?)[-–~](\d+(?:\.\d+)?)\s*s$", s)
    if m:
        return f"{_num(m.group(1))}-{_num(m.group(2))} 秒"
    return s


def _num(s: str) -> str:
    """`0.0` → `0`、`3.5` → `3.5`：模型给的小数尾零不该出现在交付物里。"""
    f = float(s)
    return str(int(f)) if f == int(f) else f"{f:g}"


def render_script_md(result: dict) -> str:
    """把结果渲染成可读的 Markdown（脚本.md）。

    修复前这里的分镜表「口播」列永远为空：schema 里写着 voiceover
    「缺省由组装器按段落填充」，但组装器从没填过。现在按段落补齐。
    """
    from .checker import count_chars          # 局部导入避免循环依赖

    p = result["params"]
    lines = [f"# 口播脚本：{p['topic']}",
             f"- 行业包：{result['pack']}{'（草稿）' if result.get('pack_draft') else ''}"
             f"　细分：{p.get('segment', '')}　受众：{p.get('audience', '')}",
             f"- {_secs(p['duration'])} / {p.get('platform', '')} / {p.get('style', '')}"
             f" / {p.get('persona', '')}",
             "", "## 口播文案", ""]
    names = {"hook": "开场钩子", "cta": "结尾引导"}
    idx = 0
    for s, tm in zip(result["sections"], result["timings"]):
        if s["type"] == "point":
            idx += 1
            label = f"要点{idx}"
        else:
            label = names[s["type"]]
        lines.append(f"**【{label}】** {tm['start']}-{tm['end']} 秒 · {count_chars(s['text'])} 字")
        lines.append(s["text"])
        lines.append("")
    ch = result["check"]
    # 校验结论三态与界面**同一份词**（desktop/renderer/js/words.js 的 VERDICT）。
    # Python 与 JS 共享不了代码，所以由 tests/test_job_state_vocabulary_consistency.py
    # 读两个文件对账 —— 只改一边就红，不靠"记得改两处"。
    # ⚠ 这里以前是二分 `'✅ 合格' if ch['passed'] else '❌ ' + blockers`：`passed` 为 None
    #   （旧产物 / 没跑校验）落到 else，导出的 md 里印着一行光秃秃的「❌ 」（blockers 是空的），
    #   而屏幕上说的是「没有校验结论」—— 看到的与拿走的又分成两份。
    passed = ch.get("passed")
    blockers = ch.get("blockers") or []
    if passed is True:
        verdict = "✅ 已通过校验"
    elif passed is False:
        verdict = "❌ 未通过校验" + ("：" + "；".join(blockers) if blockers else "")
    else:
        verdict = "没有校验结论"
    lines += ["---",
              f"字数 {ch['chars_total']}/{ch.get('target_total') or '-'} 字 · "
              f"预估 {_secs(ch['estimated_seconds'])} · 偏差 {ch['deviation_pct']:+.1f}% · {verdict}", ""]
    if result.get("placeholders"):
        lines += ["> 含占位事实：" + "、".join(result["placeholders"]) + "，请补充后再发布。", ""]
    if result.get("quota_degraded"):
        # 导出的 脚本.md 是最终交付物，降级说明必须跟着走 ——
        # 只在界面上提示、导出后却看不出配额是估的，等于降级又变回不可见。
        lines += [f"> ⚠ 本行业包没配 quota_table，字数配额（总计 "
                  f"{result.get('quota', {}).get('total', '-')} 字）是按「时长 × 语速」"
                  "估算的通用值，不是为本行业定制的。要拿到贴合本行业的配额，"
                  "请在 packs/<行业>/pack.yaml 里补 quota_table。", ""]
    # 人味分（`app/ai_tells.py`）：**只在界面上露等于导出后又变回不可见** ——
    # 与上面那条 quota_degraded 是同一条规矩（md 是最终交付物）。
    # ⚠ `ai_tells` 落 null = 这个包没配词表、**本次没测**，与"测了、很好"是两件事，
    #   所以这里只在非 null 时输出，且明说"不参与校验结论"（否则会被读成未通过）。
    ai = ch.get("ai_tells") or {}
    if ai:
        lines += [f"> 人味分 {ai.get('score', '-')}/100"
                  f"（strong {ai.get('strong', 0)} 条 · weak {ai.get('weak', 0)} 条）"
                  " —— 不参与校验结论，只是「念出来像不像人」的提示。", ""]
        for h in (ai.get("hits") or [])[:8]:
            lines.append(f"> - [{h.get('severity', '')}] {h.get('id', '')}"
                         f"（{h.get('where', '')}，{h.get('count', 0)} 处）：{h.get('detail', '')}")
        if ai.get("hits"):
            lines.append("")
    if result.get("storyboard"):
        lines += ["## 分镜表", "", "| 时间 | 画面/景别 | 口播 | 字幕 | 音效 | 提示 |",
                  "|---|---|---|---|---|---|"]
        for i, shot in enumerate(result["storyboard"]):
            tm = result["timings"][i] if i < len(result["timings"]) else {}
            t = _shot_time(shot.get("time"), tm)
            # 口播列：分镜没给 voiceover 时按对应段落补齐，不再留空
            vo = shot.get("voiceover") or (
                result["sections"][i]["text"] if i < len(result["sections"]) else "")
            lines.append(f"| {t} | {shot.get('shot', '')} | {vo} "
                         f"| {shot.get('subtitle', '')} | {shot.get('sfx', '')} "
                         f"| {shot.get('note', '')} |")
        lines.append("")
    # 命中明细要**排给人读**：以前直接把列表塞进 f-string，交付物里出现的是
    # `- 硬禁用词：[{'word': '补贴', 'count': 2}]` —— Python 的 repr 进了别人的笔记
    # （盘上 102 份产物恰好 hard_hits 全空，所以这条从没暴露过）。
    def _hits(key: str) -> str:
        rows = ch.get(key) or []
        return "、".join(
            f"{h.get('word', '?')}×{h.get('count', 1)}"
            for h in rows if isinstance(h, dict)) or "无"

    lines += ["", "## 合规检查", "",
              f"- 硬禁用词：{_hits('hard_hits')}",
              f"- 待确认：{_hits('soft_hits')}（语境正常即可放行）"]
    return "\n".join(lines)
