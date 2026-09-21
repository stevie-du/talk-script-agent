# -*- coding: utf-8 -*-
"""mock 模式端到端冒烟：一键直通 + 回炉闭环 + 分步确认 + 单段重写 + 建包 + 导出技能。

跑法：python tests/test_smoke_mock.py   或   pytest tests/test_smoke_mock.py

不需要 API Key（走 mock 夹具）。夹具在首轮故意写入"政府补贴"，用来验收
「代码校验拦下 → 自动回炉 → 出合格版本」这条闭环。

注：本文件原为 smoke_mock.py，函数叫 main()，**pytest 收集不到**；
而且重构后仍按旧 API 用 `pl.jobs[jid].snapshot()`（jobs 现在是只读快照字典），
会直接抛 AttributeError。已改为 test_* + `pl.get_job(jid)`。
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

from app.config import load_config                             # noqa: E402
from app.export_skill import export_agent_skill                # noqa: E402
from app.packgen import create_pack                            # noqa: E402
from app.pipeline import Pipeline, wait_job                    # noqa: E402
from app.schemas import (GenerateRequest,                # noqa: E402
                         RewriteSegmentRequest)


def _pipeline(tmp: Path) -> Pipeline:
    cfg = load_config(tmp)
    cfg.mock = True
    return Pipeline(tmp, cfg)


def _wait_state(pl: Pipeline, jid: str, states: set, timeout: float = 60.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        snap = pl.get_job(jid).snapshot()
        if snap["state"] in states:
            return snap
        time.sleep(0.2)
    raise TimeoutError(f"{jid} 停在 {pl.get_job(jid).snapshot()['state']}")


def test_end_to_end_mock():
    tmp = Path(tempfile.mkdtemp(prefix="talkscript-smoke-"))
    shutil.copytree(ROOT / "packs", tmp / "packs")
    pl = _pipeline(tmp)
    try:
        jid_direct = _case_direct_and_recheck(pl, tmp)
        _case_voice_and_format(pl)
        _case_rewrite_segment(pl, tmp, jid_direct)
        _case_pack_without_storyboard_stage(pl, tmp)
        _case_packgen(pl, tmp)
        _case_export_skill(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ── 1. 一键直通 + 回炉闭环 ──────────────────────────────────
def _case_direct_and_recheck(pl: Pipeline, tmp: Path):
    jid = pl.start_generate(GenerateRequest(pack="elevator", topic="被困电梯怎么办"))
    snap = wait_job(pl, jid)
    assert snap["state"] == "done", snap.get("error")
    r = snap["result"]

    rounds = [s["key"] for s in snap["steps"] if s["key"].startswith("check_r")]
    r1 = next(s for s in snap["steps"] if s["key"] == "check_r1")["data"]["report"]
    assert "政府补贴" in [h["word"] for h in r1["hard_hits"]], \
        f"第 1 轮应命中政府补贴：{r1['hard_hits']}"
    assert len(rounds) >= 2, f"应发生回炉，实际轮次：{rounds}"
    assert r["check"]["passed"], f"终版应合格：{r['check']}"
    assert all("政府补贴" not in s["text"] for s in r["sections"]), "终版不应再含政府补贴"

    out = tmp / "generated"
    assert list(out.glob(f"*/{jid}/result.json")), "result.json 未落盘"
    assert list(out.glob(f"*/{jid}/脚本.md")), "脚本.md 未落盘"
    # 落盘目录只有一个（回归「跨零点分裂成两个目录」）
    assert len(list(out.glob(f"*/{jid}"))) == 1, "产物落进了多个日期目录"

    # P1-30：分镜拆成独立阶段 —— 必须在**校验通过之后**才跑（回炉轮不该重画），
    # 且与段落一一对应（scenes 按下标配对，多一段少一段都会错位）。
    sb = r["storyboard"]
    assert len(sb) == len(r["sections"]), \
        f"分镜应与段落一一对应：{len(sb)} vs {len(r['sections'])}"
    assert all(s["shot"] for s in sb), f"分镜画面不应为空：{sb}"
    keys = [s["key"] for s in snap["steps"]]
    assert keys[-1] == "storyboard" and keys.index("storyboard") > keys.index(rounds[-1]), \
        f"分镜应排在校验之后：{keys}"
    assert all(sc["visual"]["prompt"] for sc in r["scenes"]), "scenes 应带上分镜画面"
    return jid


# ── 1b/1c. 人味档位与输出内容开关 ───────────────────────────
def _case_voice_and_format(pl: Pipeline):
    jid = pl.start_generate(GenerateRequest(pack="elevator", topic="被困电梯怎么办",
                                            voice="off"))
    snap = wait_job(pl, jid)
    assert snap["state"] == "done", snap.get("error")
    assert snap["result"]["params"]["voice"] == "off"

    jid = pl.start_generate(GenerateRequest(pack="elevator", topic="被困电梯怎么办",
                                            format="voice"))
    snap = wait_job(pl, jid)
    assert snap["state"] == "done", snap.get("error")
    assert snap["result"]["sections"], "仅口播仍应有分段文案"
    assert snap["result"]["storyboard"] == [], "仅口播不应有分镜"
    assert not any(s["key"] == "storyboard" for s in snap["steps"]), \
        "仅口播不应跑分镜阶段（这一步是真实 token 开销）"
    assert snap["result"]["params"]["format"] == "voice"


# ── 2. 单段重写 ────────────────────────────────────────────
# 原先这里是「分步确认 + 选题编辑」（2026-09-19 随功能一并移除）。
# 它曾顺带承担"编辑后的选题会生效"这条断言；那条改由 _case_rewrite_segment
# 与 pipeline 的回炉用例覆盖 —— 角度不对时的修正走「按此修改」/「换一版」。


# ── 3. 单段重写（含落盘同步与时间轴重算）────────────────────
def _case_rewrite_segment(pl: Pipeline, tmp: Path, jid: str):
    before_snap = pl.get_job(jid).snapshot()
    before = before_snap["result"]
    before_text = before["sections"][1]["text"]
    before_revs = len(before["revisions"])
    sb_before = sum(1 for s in before_snap["steps"] if s["key"] == "storyboard")

    pl.rewrite_segment(jid, RewriteSegmentRequest(index=1, feedback="更口语化"))
    # 重写从 done 出发，要等 revisions 增加而不是等状态变化
    deadline = time.time() + 30
    snap = pl.get_job(jid).snapshot()
    while time.time() < deadline:
        snap = pl.get_job(jid).snapshot()
        if snap["state"] == "failed":
            break
        if snap["state"] == "done" and len(snap["result"]["revisions"]) > before_revs:
            break
        time.sleep(0.2)
    assert snap["state"] == "done", snap.get("error")
    after = snap["result"]["sections"][1]["text"]
    assert after != before_text, "单段重写应生效"

    day = snap["result"]["created_at"][:10].replace("-", "")
    md = (tmp / "generated" / day / jid / "脚本.md").read_text(encoding="utf-8")
    assert after[:15] in md, "脚本.md 未同步重写结果"
    tm = snap["result"]["timings"][-1]
    assert tm["end"] > tm["start"], "时间轴应重算"

    # P1-30：改写过的段落要重画分镜 —— 旧分镜的时间轴是按改写前的字数算的。
    # 计数 +1 而不是比内容：mock 夹具的画面文案是固定的一套，比内容比不出什么。
    sb_after = sum(1 for s in snap["steps"] if s["key"] == "storyboard")
    assert sb_after == sb_before + 1, \
        f"单段重写应重画分镜：{sb_before} → {sb_after}"
    assert len(snap["result"]["storyboard"]) == len(snap["result"]["sections"]), \
        "重画后分镜仍须与段落一一对应"


# ── 3b. P1-30 之前建的包没有 storyboard 阶段 ──────────────────
# 跳过分镜，而不是让一条已经写完、已经通过校验的脚本以 KeyError 收场。
def _case_pack_without_storyboard_stage(pl: Pipeline, tmp: Path):
    dst = tmp / "packs" / "nostage"
    shutil.copytree(tmp / "packs" / "elevator", dst)
    skill = yaml.safe_load((dst / "skill.yaml").read_text(encoding="utf-8"))
    skill["stages"].pop("storyboard")
    (dst / "skill.yaml").write_text(
        yaml.safe_dump(skill, allow_unicode=True), encoding="utf-8")

    jid = pl.start_generate(GenerateRequest(pack="nostage", topic="被困电梯怎么办"))
    snap = wait_job(pl, jid)
    assert snap["state"] == "done", snap.get("error")
    assert snap["result"]["storyboard"] == [], "缺 storyboard 阶段时不应有分镜"
    assert snap["result"]["sections"], "正文仍应正常产出"
    assert any(s["key"] == "storyboard_skip" for s in snap["steps"]), \
        "跳过必须留痕，不能静默"


# ── 4. 建包（mock 夹具，走后台作业）─────────────────────────
def _case_packgen(pl: Pipeline, tmp: Path):
    # P1-43：建包与生成同一套作业通道 —— 有状态、能取消、错误落在作业上。
    jid = pl.start_packgen("全屋定制/装修", "全屋定制家居品牌，面向新房装修业主获客")
    snap = wait_job(pl, jid)
    assert snap["state"] == "done", snap.get("error")
    assert snap["kind"] == "packgen", f"作业类型不对：{snap['kind']}"
    info = snap["result"]
    # 同名再来一次：这种结论不花钱，必须同步 409，不该起一个必然失败的作业
    try:
        pl.start_packgen("全屋定制/装修", "重复一次")
        raise AssertionError("同名行业包应当被同步挡下")
    except FileExistsError as e:
        assert "已存在" in str(e), str(e)
    pack_dir = tmp / "packs" / info["name"]
    assert (pack_dir / "pack.yaml").exists() and (pack_dir / "banwords.yaml").exists()
    assert (pack_dir / "knowledge/topics.md").exists()
    assert (pack_dir / "校对清单.md").exists()
    # P2-25 口径：新包必须留出「核心术语」这一节，否则 write.files.terms 永远注入不到东西
    assert "核心术语" in (pack_dir / "knowledge/standards.md").read_text(encoding="utf-8")
    pdata = yaml.safe_load((pack_dir / "pack.yaml").read_text(encoding="utf-8"))
    assert pdata["draft"] is True, "生成包必须为草稿"
    assert pdata.get("quota_table"), "新包必须继承模板的配额表（否则生成会降级）"

    jid = pl.start_generate(GenerateRequest(pack=info["name"], topic="全屋定制报价怎么看"))
    snap = wait_job(pl, jid)
    # mock write 夹具是电梯文案，跨行业时可能校验不过 —— 结构能走通即可
    assert snap["state"] in ("done", "failed"), snap

    # 取消的检查点必须落在**写盘之前**：作业取消后不该留下半个行业包目录，
    # 否则下一次同名建包会被 FileExistsError 永久挡住（P2-46 的清理只管异常路径）。
    from app.jobs import JobCancelled
    from app.packgen import preview_slug
    try:
        create_pack(tmp, pl.llm, "假行业取消测试", "用于验证取消检查点的描述",
                    should_abort=lambda: True)
        raise AssertionError("should_abort 为真时应抛 JobCancelled，不该往下写")
    except JobCancelled:
        pass
    assert not (tmp / "packs" / preview_slug("假行业取消测试")).exists(), \
        "取消后留下了半成品目录"


# ── 5. 导出 Agent 技能 ──────────────────────────────────────
def _case_export_skill(tmp: Path):
    exp = export_agent_skill(tmp, "elevator")
    exp_dir = Path(exp["path"])
    skill_md = (exp_dir / "SKILL.md").read_text(encoding="utf-8")
    assert skill_md.startswith("---\nname:") and "description:" in skill_md.split("---")[1]
    assert (exp_dir / "tools" / "check.py").exists()
    assert (exp_dir / "knowledge" / "topics.md").exists()
    assert (exp_dir / "skill.yaml").exists()
    assert "只输出一个 JSON" not in skill_md, "导出的技能不应含引擎专属 JSON 指令"
    assert not (exp_dir / "private").exists(), "默认不应导出 private/"

    # 导出的校验器要能在技能目录内独立运行
    demo = exp_dir / "_demo.txt"
    demo.write_text((ROOT / "examples" / "demo-60s.txt").read_text(encoding="utf-8"),
                    encoding="utf-8")
    run = subprocess.run([sys.executable, str(exp_dir / "tools" / "check.py"),
                          str(demo), "--duration", "60", "--rate", "4.5"],
                         capture_output=True, text=True)
    assert run.returncode == 0, run.stdout + run.stderr
    assert "244" in run.stdout, run.stdout
    demo.unlink(missing_ok=True)


def main() -> int:
    try:
        test_end_to_end_mock()
        print("  ✅ test_end_to_end_mock")
        print("\n1/1 通过")
        return 0
    except Exception as e:  # noqa: BLE001
        print(f"  ❌ test_end_to_end_mock: {type(e).__name__}: {e}")
        print("\n0/1 通过")
        return 1


if __name__ == "__main__":
    sys.exit(main())
