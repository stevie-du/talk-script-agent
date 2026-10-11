# -*- coding: utf-8 -*-
"""变异工具本身的守卫测试（P2-14/15/16，2026-10-05）。

⚠ `_verify/mutate.py` 没有 `if __name__` 守卫，import 即跑全套（拿锁、预检、
进主循环）—— 所以这里的取用方式只有两种：
  · 子进程跑 mutate.py 本体（判退出码与输出）；
  · 用 ast 把目标函数单独摘出来 exec（命名空间自带依赖桩）。
跑法：pytest tests/test_mutate_tool.py
"""
from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _fn_from_mutate(name: str, ns: dict):
    """从 mutate.py 摘出一个顶层函数单独执行（带调用方给的依赖桩）。"""
    src = (ROOT / "_verify" / "mutate.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            exec(compile(ast.Module(body=[node], type_ignores=[]),
                         "<mutate>", "exec"), ns)
            return ns[name]
    raise AssertionError(f"mutate.py 里找不到顶层函数 {name}（工具改版了？重指本测试）")


# ── P2-14：-k 0 匹配必须显式报错，不许印假全绿 ─────────────────
def test_zero_match_exits_2_instead_of_faking_green():
    """README 记过这起事故：-k 打错一个字 → 0 条跑 → 「0 条变异、0 条不合格」
    + exit 0 = 一份"全部跑过、全部没跑"的全绿报告。工具必须自己拦。

    ⚠ 断言必须是**四条一起**：rc=2、有守卫文案、没有撞锁文案、没有主循环的
    结果行。只钉 rc 的话，守卫被拆掉的变异体会因为"撞上外层 mutate 的锁"
    （同样是 exit 2）而假绿；只钉文案的话，守卫拆掉后文案照印、只是不退出。"""
    # P3-7（2026-10-11）：子进程管道的输出编码取 locale —— 无 PYTHONUTF8 的机器上
    # 守卫文案里的 🔴 会先一步 UnicodeEncodeError → rc=1 ≠ 2，四条断言全红
    # （环境性误报）。工具契约是「PYTHONUTF8=1 跑 mutate」，在测试里钉死。
    r = subprocess.run(
        [sys.executable, str(ROOT / "_verify" / "mutate.py"), "-k", "zzzz_no_such_group"],
        capture_output=True, text=True, timeout=600, cwd=ROOT,
        env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"})
    out = (r.stdout or "") + (r.stderr or "")
    assert r.returncode == 2, (r.returncode, out[-500:])
    assert "一条都没匹配上" in out, out[-500:]
    assert "另一个变异进程" not in out, "退出发生在拿锁之后（守卫必须在拿锁前拦）：" + out[-500:]
    assert "共 0 条变异" not in out, "走到了主循环（守卫没拦住）：" + out[-500:]


# ── P2-15：子进程必须有总闸超时 ────────────────────────────────
def test_run_cmd_kills_hung_commands():
    """挂死的 pytest/verify 会占死整轮变异与锁（journal 里还留着变异态文件）。
    _run_cmd 的总闸到点杀进程，按"工具错"上报，绝不无限等。"""
    src = (ROOT / "_verify" / "mutate.py").read_text(encoding="utf-8")
    import re
    m = re.search(r"^SUBPROC_TIMEOUT = (\d+)", src, re.M)
    assert m, "mutate.py 里找不到 SUBPROC_TIMEOUT（工具改版了？重指本测试）"
    run_cmd = _fn_from_mutate("_run_cmd", {"subprocess": subprocess, "ROOT": ROOT,
                                           "SUBPROC_TIMEOUT": int(m.group(1))})
    hung = run_cmd([sys.executable, "-c", "import time; time.sleep(5)"], timeout=1)
    assert hung.returncode is None, (hung.returncode, hung.stderr)
    assert "总闸" in (hung.stderr or ""), hung.stderr
    healthy = run_cmd([sys.executable, "-c", "print('fine')"], timeout=60)
    assert healthy.returncode == 0 and "fine" in (healthy.stdout or "")


# ── P2-16：锁的占坑必须原子 ────────────────────────────────────
def test_lock_acquisition_is_atomic(tmp_path):
    """旧写法"检查→写"两步之间有窗口，双开 mutate 都能过检——锁要防的
    事故原样复发。O_CREAT|O_EXCL 之后：活人占着必须拒（exit 2），
    死坑必须当场清掉重占。"""
    lock = tmp_path / "probe.lock"
    alive = {"held": False}

    def probe() -> bool:
        return alive["held"]

    acquire = _fn_from_mutate(
        "_acquire_lock",
        {"os": os, "sys": sys, "LOCK": lock, "_lock_held_by_living_process": probe})

    acquire()                                   # 空场：占坑成功
    assert lock.read_text(encoding="utf-8") == str(os.getpid())

    lock.write_text("999999", encoding="utf-8")
    alive["held"] = True
    with pytest.raises(SystemExit):
        acquire()                               # 活人占着 → 拒绝
    assert lock.read_text(encoding="utf-8") == "999999", "拒绝时不得覆盖活人的坑"

    alive["held"] = False
    acquire()                                   # 死坑 → 清掉重占
    assert lock.read_text(encoding="utf-8") == str(os.getpid())
