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
INDEX_VERSION = 2

# 墓碑只用于拦住「删除后仍在跑的作业」，不需要长期留存
_TOMBSTONE_CAP = 512


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
            write_atomic(job_dir / "result.json",
                         json.dumps(result, ensure_ascii=False, indent=1))
            self._upsert(self._summary_from_result(result))
            write_atomic(job_dir / "脚本.md", render_script_md(result))
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
        """
        for d in self.find_dirs(jid):
            f = d / "job.json"
            if f.exists():
                try:
                    return json.loads(f.read_text(encoding="utf-8"))
                except Exception:               # noqa: BLE001
                    return None
        return None

    def history(self, limit: int = 100) -> list[dict]:
        """已结束作业的摘要列表，新的在前。只读索引，不做全量 JSON 解析。"""
        idx = self._read_index_file()
        items = sorted(idx.values(), key=lambda x: x.get("created_at", ""), reverse=True)
        return items[:limit]

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
            "duration": p.get("duration"), "chars": None, "passed": None,
            "state": snap.get("state", "failed"), "error": snap.get("error"),
        }


# ── 人类可读产物 ────────────────────────────────────────────

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
             f"- {int(p['duration'])}s / {p.get('platform', '')} / {p.get('style', '')}"
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
    lines += ["---",
              f"字数 {ch['chars_total']}/{ch.get('target_total') or '-'} 字 · "
              f"预估 {ch['estimated_seconds']}s · 偏差 {ch['deviation_pct']:+.1f}% · "
              f"{'✅ 合格' if ch['passed'] else '❌ ' + '；'.join(ch['blockers'])}", ""]
    if result.get("placeholders"):
        lines += ["> 含占位事实：" + "、".join(result["placeholders"]) + "，请补充后再发布。", ""]
    if result.get("quota_degraded"):
        # 导出的 脚本.md 是最终交付物，降级说明必须跟着走 ——
        # 只在界面上提示、导出后却看不出配额是估的，等于降级又变回不可见。
        lines += [f"> ⚠ 本行业包没配 quota_table，字数配额（总计 "
                  f"{result.get('quota', {}).get('total', '-')} 字）是按「时长 × 语速」"
                  "估算的通用值，不是为本行业定制的。要拿到贴合本行业的配额，"
                  "请在 packs/<行业>/pack.yaml 里补 quota_table。", ""]
    if result.get("storyboard"):
        lines += ["## 分镜表", "", "| 时间 | 画面/景别 | 口播 | 字幕 | 音效 | 提示 |",
                  "|---|---|---|---|---|---|"]
        for i, shot in enumerate(result["storyboard"]):
            tm = result["timings"][i] if i < len(result["timings"]) else {}
            t = shot.get("time") or (f"{tm.get('start')}-{tm.get('end')}s" if tm else "")
            # 口播列：分镜没给 voiceover 时按对应段落补齐，不再留空
            vo = shot.get("voiceover") or (
                result["sections"][i]["text"] if i < len(result["sections"]) else "")
            lines.append(f"| {t} | {shot.get('shot', '')} | {vo} "
                         f"| {shot.get('subtitle', '')} | {shot.get('sfx', '')} "
                         f"| {shot.get('note', '')} |")
        lines.append("")
    lines += ["", "## 合规检查", "",
              f"- 硬禁用词：{ch['hard_hits'] or '无'}",
              f"- 待确认：{ch['soft_hits'] or '无'}（语境正常即可放行）"]
    return "\n".join(lines)
