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
from app.schemas import (ConfirmRequest, GenerateRequest,      # noqa: E402
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
        _case_direct_and_recheck(pl, tmp)
        _case_voice_and_format(pl)
        jid_step = _case_step_confirm(pl)
        _case_rewrite_segment(pl, tmp, jid_step)
        _case_packgen(pl, tmp)
        _case_export_skill(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ── 1. 一键直通 + 回炉闭环 ──────────────────────────────────
def _case_direct_and_recheck(pl: Pipeline, tmp: Path):
    jid = pl.start_generate(GenerateRequest(pack="elevator", topic="被困电梯怎么办", mode="auto"))
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


# ── 1b/1c. 人味档位与输出内容开关 ───────────────────────────
def _case_voice_and_format(pl: Pipeline):
    jid = pl.start_generate(GenerateRequest(pack="elevator", topic="被困电梯怎么办",
                                            mode="auto", voice="off"))
    snap = wait_job(pl, jid)
    assert snap["state"] == "done", snap.get("error")
    assert snap["result"]["params"]["voice"] == "off"

    jid = pl.start_generate(GenerateRequest(pack="elevator", topic="被困电梯怎么办",
                                            mode="auto", format="voice"))
    snap = wait_job(pl, jid)
    assert snap["state"] == "done", snap.get("error")
    assert snap["result"]["sections"], "仅口播仍应有分段文案"
    assert snap["result"]["storyboard"] == [], "仅口播不应有分镜"
    assert snap["result"]["params"]["format"] == "voice"


# ── 2. 分步确认 + 选题编辑 ──────────────────────────────────
def _case_step_confirm(pl: Pipeline) -> str:
    jid = pl.start_generate(GenerateRequest(pack="elevator", topic="加装电梯一楼不同意", mode="step"))
    snap = _wait_state(pl, jid, {"paused_awaiting_confirmation"})
    assert snap["state"] == "paused_awaiting_confirmation", f"应暂停：{snap['state']}"
    plan = snap["result"]["plan"]
    plan["hook_line"] = "一楼反对的从来不是电梯本身。"
    pl.confirm(jid, ConfirmRequest(plan=plan))
    snap = wait_job(pl, jid)
    assert snap["state"] == "done" and snap["result"]["sections"], "确认后应完成"
    assert snap["result"]["plan"]["hook_line"].startswith("一楼反对"), "编辑后的选题应生效"
    return jid


# ── 3. 单段重写（含落盘同步与时间轴重算）────────────────────
def _case_rewrite_segment(pl: Pipeline, tmp: Path, jid: str):
    before = pl.get_job(jid).snapshot()["result"]
    before_text = before["sections"][1]["text"]
    before_revs = len(before["revisions"])

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


# ── 4. 建包（mock 夹具）─────────────────────────────────────
def _case_packgen(pl: Pipeline, tmp: Path):
    info = create_pack(tmp, pl.llm, "全屋定制/装修", "全屋定制家居品牌，面向新房装修业主获客")
    pack_dir = tmp / "packs" / info["name"]
    assert (pack_dir / "pack.yaml").exists() and (pack_dir / "banwords.yaml").exists()
    assert (pack_dir / "knowledge/topics.md").exists()
    assert (pack_dir / "校对清单.md").exists()
    pdata = yaml.safe_load((pack_dir / "pack.yaml").read_text(encoding="utf-8"))
    assert pdata["draft"] is True, "生成包必须为草稿"
    assert pdata.get("quota_table"), "新包必须继承模板的配额表（否则生成会降级）"

    jid = pl.start_generate(GenerateRequest(pack=info["name"], topic="全屋定制报价怎么看"))
    snap = wait_job(pl, jid)
    # mock write 夹具是电梯文案，跨行业时可能校验不过 —— 结构能走通即可
    assert snap["state"] in ("done", "failed"), snap


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
