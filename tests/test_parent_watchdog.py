# -*- coding: utf-8 -*-
"""父进程看门狗：主进程没了，引擎必须自己退。

为什么单独一个文件、而且真的起子进程：这条逻辑的**全部价值**在于
「进程真的会消失」这个事实，桩不出来。用 TestClient 或 mock 掉 `parent_alive`
只会得到一个同义反复的绿灯 —— 它证明不了引擎真的会走。

对应缺陷：Electron 主进程被强杀 / 崩溃时，Windows 不会连带杀掉 python.exe。
多崩几次就有几份引擎常驻，每份占一个端口 + 一份内存；更糟的是下一次启动的
健康检查可能被**旧引擎**应答（它同样在 127.0.0.1 上回 200），
于是界面连到一个拿着旧 token / 旧配置的僵尸进程。
"""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.server import parent_alive                      # noqa: E402


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_parent_alive_for_real_processes():
    """探活必须对真进程成立：活着的说活，杀干净的说死。

    ⚠ 这里**不能**用 `os.kill(pid, 0)` 来"探测"—— Windows 上 signal 0 走的是
    TerminateProcess，探测动作本身会把目标杀掉。这条测试同时也是那件事的守卫：
    如果哪天有人把实现换回 os.kill，本文件第二例会直接失败或误杀。
    """
    assert parent_alive(os.getppid()), "当前父进程被判成已死 —— 探活实现不可信"
    assert parent_alive(os.getpid()), "自己判自己不活 —— 探活实现不可信"
    assert not parent_alive(0x7FFFFFF0), "一个几乎不可能存在的 PID 被判成活"

    victim = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    assert parent_alive(victim.pid), "刚起的子进程被判成已死"
    victim.kill()
    victim.wait(timeout=10)
    assert not parent_alive(victim.pid), "进程已退出（且已回收）却仍被判成活"


def test_engine_exits_when_parent_dies():
    """真起一个引擎，杀掉它认的父进程，引擎要自己退。"""
    tmp = Path(tempfile.mkdtemp(prefix="talkscript-watchdog-"))
    shutil.copytree(ROOT / "packs", tmp / "packs")
    port = _free_port()

    # 牺牲品：一个会活到被我们杀掉为止的进程，充当"Electron 主进程"
    victim = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "TALKSCRIPT_MOCK": "1"}
    eng = subprocess.Popen(
        [sys.executable, "-m", "app.server", "--port", str(port),
         "--root", str(tmp), "--data-dir", str(tmp), "--token", "wd-token",
         "--parent-pid", str(victim.pid)],
        cwd=str(ROOT), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace")
    try:
        _wait_health(port, timeout=40)

        victim.kill()
        victim.wait(timeout=10)
        # 看门狗间隔 2s，留足余量；超时没退就是没生效
        try:
            eng.wait(timeout=20)
        except subprocess.TimeoutExpired:
            raise AssertionError(
                "父进程已退出，引擎却还活着 —— 孤儿引擎会占住端口等下一次启动来踩")
        err = eng.stderr.read()
        assert eng.returncode is not None, "引擎未退出"
        # ⚠ 只断"退了"是有假绿通道的：引擎因为别的原因死了也算过。
        # 必须确认是**看门狗**动的手，而不是崩溃、端口冲突、导入失败之类。
        assert "主进程已退出" in err, (
            f"引擎退了，但不是看门狗让它退的 —— stderr:\n{err[-800:]}")
    finally:
        for p in (victim, eng):
            try:
                p.kill()
                p.wait(timeout=5)
            except Exception:                       # noqa: BLE001
                pass
        shutil.rmtree(tmp, ignore_errors=True)


def test_engine_without_parent_pid_keeps_running():
    """不传 --parent-pid 时**不能**自杀。

    命令行手动起引擎调试（README 里那条 `python -m app.server --port 8765`）
    的父进程是 shell，跑完就没了 —— 若默认启用看门狗，调试用引擎会立刻被自己杀掉，
    表现是"启动后一秒就退出，没有任何报错"。
    """
    tmp = Path(tempfile.mkdtemp(prefix="talkscript-nowd-"))
    shutil.copytree(ROOT / "packs", tmp / "packs")
    port = _free_port()
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "TALKSCRIPT_MOCK": "1"}
    eng = subprocess.Popen(
        [sys.executable, "-m", "app.server", "--port", str(port),
         "--root", str(tmp), "--data-dir", str(tmp), "--token", "wd2"],
        cwd=str(ROOT), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        _wait_health(port, timeout=40)
        time.sleep(6)                               # 跨过两个看门狗周期
        assert eng.poll() is None, "没传 parent-pid 却被看门狗杀了"
    finally:
        eng.kill()
        eng.wait(timeout=5)
        shutil.rmtree(tmp, ignore_errors=True)


def _wait_health(port: int, timeout: float) -> None:
    deadline = time.time() + timeout
    url = f"http://127.0.0.1:{port}/api/health"
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as r:
                if r.status == 200:
                    return
        except Exception:                           # noqa: BLE001
            time.sleep(0.3)
    raise AssertionError(f"引擎 {timeout}s 内没有通过健康检查：{url}")


if __name__ == "__main__":                           # pragma: no cover
    test_parent_alive_for_real_processes()
    test_engine_without_parent_pid_keeps_running()
    test_engine_exits_when_parent_dies()
    print("✅ 父进程看门狗 3/3")
