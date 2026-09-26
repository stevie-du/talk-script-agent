# -*- coding: utf-8 -*-
"""瞬时读故障不该被当成"包坏了"（第 22 轮复核 P1-1 的真实形态）。

子代理给的修法是"把 `_pack_audit` 里 `except Exception` 收窄成
`except (PackError, PackBrokenError)`"。**实测不成立**：用 `msvcrt.locking` 造真实
占用时，`read_yaml_file` 把 `PermissionError` 换成错误说明字符串（`app/fileio.py`），
`pack_info` 再把它变成 `pack_error`，`Pack()` 抛出来的是 **PackBrokenError** ——
和"包真的写坏了"同一类。所以收窄那条臂永远走不到，是一次安慰剂修复；
`_pack_audit` 里保留宽捕捉并注明原因，就是不让下一轮再"改进"回去。

两条真正成立的判据：
  1. **可重试的错误不进进程级缓存**（`fileio.YamlError.retryable` → `read_yaml_cached`）。
     缓存键是 (mtime, size)，而读失败时这两个都没变 —— 原来一次瞬时占用会把
     一个健康包钉成"坏"直到重启应用（实测：解除占用后再 `Pack()` 仍然抛同一句）。
  2. **判死之前先重读**（`packgen._RELOAD_WAITS`）。原来一次瞬时占用会让
     `create_pack` 把用户付了一份模型钱的整包删掉，还叫用户"换个更具体的业务描述"。

占用一律用 `msvcrt.locking` 造（不 mock 异常），解除时机由测试决定而不是等时钟，
所以在慢机器上也不会翻红。POSIX 上没有同一种锁，那两条标了 skipif；
跨平台那一半由 `test_only_content_errors_get_cached` 钉住机制本身。
"""
from __future__ import annotations

import logging
import msvcrt
import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import knowledge, packgen                                  # noqa: E402
from app.config import load_config                                  # noqa: E402
from app.fileio import YamlError, read_yaml_file                    # noqa: E402
from app.knowledge import Pack, read_yaml_cached                    # noqa: E402
from app.pipeline import Pipeline                                   # noqa: E402

win_only = pytest.mark.skipif(os.name != "nt",
                              reason="用 msvcrt 造真实文件占用，POSIX 没有同一形态的锁")


def _lock(p: Path) -> int:
    fd = os.open(str(p), os.O_RDONLY)
    msvcrt.locking(fd, msvcrt.LK_LOCK, max(p.stat().st_size, 1))
    return fd


def _unlock(fd: int) -> None:
    # 关闭句柄即释放锁。`LK_UNLCK` 要求"从当初加锁的那个偏移量"解，跨不了几次读之后
    # 的位置，实测会拿 PermissionError —— 而"关掉就好"正是杀毒软件放开文件的真实形态。
    os.close(fd)


def _assert_locked(p: Path) -> None:
    """探针自证：占用没成立的话后面所有结论都不作数（宁可红也不要假绿）。"""
    try:
        p.read_text(encoding="utf-8")
    except OSError:
        return
    raise AssertionError("探针无效：msvcrt 没能挡住同进程的读取，这条测试什么都没测到")


def _fresh_packs_root() -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="talkscript-lock-"))
    shutil.copytree(ROOT / "packs", tmp / "packs")
    return tmp


@win_only
def test_transient_lock_does_not_permanently_mark_a_good_pack_broken():
    """解除占用之后同一个进程必须能读回来（原形状：错误被 (mtime,size) 缓存钉死）。"""
    tmp = _fresh_packs_root()
    target = tmp / "packs" / "elevator" / "pack.yaml"
    key = str(target)
    fd = None
    try:
        fd = _lock(target)
        _assert_locked(target)
        knowledge._yaml_cache.pop(key, None)
        _, err1 = read_yaml_cached(target)
        assert err1 and "读取失败" in err1, f"占用中没有报读失败：{err1!r}"
        _unlock(fd)
        fd = None
        data2, err2 = read_yaml_cached(target)
        assert not err2, f"占用已经解除却仍说读不出来（缓存把瞬时故障钉成永久）：{err2}"
        assert data2, "重读成功了却是空数据"
    finally:
        if fd is not None:
            os.close(fd)
        shutil.rmtree(tmp, ignore_errors=True)


@win_only
def test_packgen_survives_a_transient_lock_during_the_audit():
    """审计期间文件被按住、随后放开：包必须留住、建包算成功、清单要说明发生过什么。"""
    tmp = _fresh_packs_root()
    cfg = load_config(tmp)
    cfg.mock = True
    llm = Pipeline(tmp, cfg).llm
    real_mat = packgen._materialize
    real_pack = knowledge.Pack
    box = {"fd": None, "n": 0}
    industry, desc = "口腔诊所", "连锁口腔诊所，面向家庭做儿牙与种植科普"
    slug = packgen.slugify(industry)
    d = tmp / "packs" / slug

    def mat(dest, *a, **k):
        notes = real_mat(dest, *a, **k)
        box["fd"] = _lock(dest / "pack.yaml")
        # 让审计成为占用之后的第一次读：前面的步骤已经把内容缓存住就测不到这条链路
        knowledge._yaml_cache.clear()
        knowledge._text_cache.clear()
        return notes

    class OnceOnly(Pack):
        """第 1 次构造时占用仍在，第 2 次之前解除 —— 时机由测试定，不等时钟。"""

        def __init__(self, root, name):
            if name == slug and box["fd"] is not None:
                box["n"] += 1
                if box["n"] == 1:
                    _assert_locked(Path(root) / "packs" / name / "pack.yaml")
                else:
                    _unlock(box["fd"])
                    box["fd"] = None
            super().__init__(root, name)

    try:
        packgen._materialize = mat
        knowledge.Pack = OnceOnly
        out = packgen.create_pack(tmp, llm, industry, desc)
        assert box["n"] >= 2, f"只读了一次就下结论，重读阶梯没跑到：{box['n']}"
        assert d.is_dir(), "瞬时占用把用户付过钱的整包删掉了"
        assert out["name"] == slug, out
        text = (d / "校对清单.md").read_text(encoding="utf-8")
        assert "重读" in text, f"清单没说明这次发生过重读，用户看不到发生了什么：{text[:160]}"
        knowledge._yaml_cache.clear()
        Pack(tmp, slug)                        # 留下来的包必须真能用
    finally:
        packgen._materialize = real_mat
        knowledge.Pack = real_pack
        if box["fd"] is not None:
            _unlock(box["fd"])
        shutil.rmtree(tmp, ignore_errors=True)


def test_only_content_errors_get_cached():
    """缓存策略的机制本身（跨平台）：读不出字节可以重试，内容坏不必重读。

    用一个叫 `pack.yaml` 的**目录**造真实的 OSError —— 不 mock，POSIX 与 Windows
    都会拿到 `IsADirectoryError` / `PermissionError`。
    """
    tmp = Path(tempfile.mkdtemp(prefix="talkscript-yamlerr-"))
    try:
        bad_dir = tmp / "pack.yaml"
        bad_dir.mkdir()
        data, err = read_yaml_file(bad_dir)
        assert err and not data, f"读一个目录却什么都没报：{err!r}"
        assert isinstance(err, YamlError) and err.retryable, \
            "读不出字节被标成了「不可重试」—— 它会进缓存，并把瞬时故障钉成永久"

        syntax = tmp / "syntax.yaml"
        syntax.write_text("a: [\n", encoding="utf-8")
        _, err2 = read_yaml_file(syntax)
        assert err2 and "语法有误" in err2, err2
        assert isinstance(err2, YamlError) and not err2.retryable, \
            "内容坏标成了可重试：每次调用都要重读一遍、再刷一条 WARNING"

        # 判据要落到缓存行为上，不能只停在标记位
        knowledge._yaml_cache.pop(str(bad_dir), None)
        knowledge._yaml_cache.pop(str(syntax), None)
        read_yaml_cached(bad_dir)
        assert str(bad_dir) not in knowledge._yaml_cache, \
            "可重试的错误仍然进了 (mtime,size) 缓存 —— 解除占用后也读不回来"
        read_yaml_cached(syntax)
        assert str(syntax) in knowledge._yaml_cache, \
            "内容坏的错误没进缓存：每次 /api/meta 都要重读并重刷一条 WARNING"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_a_permanent_load_failure_is_still_failed_and_says_so(tmp_path, monkeypatch):
    """重读阶梯救不了的坏包：仍然要失败，并且原因要写清"重读过几次、每次都过不去"。

    这条同时钉住阶梯本身是**跑满的**（不是 `()` 摆样子）：构造次数必须等于
    「1 次首读 + `len(_RELOAD_WAITS)` 次重读」。上一轮的教训是"分支永远走不到的
    收窄等于没修"，所以这里不只看最终消息，还数它到底读了几回。
    """
    shutil.copytree(ROOT / "packs", tmp_path / "packs")
    d = tmp_path / "packs" / "broken"
    shutil.copytree(tmp_path / "packs" / "elevator", d)
    (d / "pack.yaml").write_text("name: 坏包\nversion: 1\nparams: {}\nbad: [\n",
                                 encoding="utf-8")
    knowledge._yaml_cache.clear()
    calls = {"n": 0}
    real_pack = knowledge.Pack

    class Counting(real_pack):
        def __init__(self, root, name):
            calls["n"] += 1
            super().__init__(root, name)

    monkeypatch.setattr(knowledge, "Pack", Counting)
    monkeypatch.setattr(packgen.time, "sleep", lambda s: None)   # 不真等 1.25 秒
    try:
        packgen._pack_audit(tmp_path, "broken")
        raise AssertionError("内容坏到加载不过去的包被判成了成功")
    except ValueError as e:
        msg = str(e)
    assert calls["n"] == len(packgen._RELOAD_WAITS) + 1, \
        f"重读阶梯没跑满：首读 + {len(packgen._RELOAD_WAITS)} 次重读应当各构造一次，实际 {calls['n']}"
    assert packgen._RELOAD_WAITS, \
        "阶梯被清空了：那等于回到「读不出来就删包」，Windows 上一次杀毒占用就够毁一份付费产物"
    assert "连加载都过不去" in msg, msg
    assert "语法有误" in msg, f"原因里没点名是哪个文件、怎么坏的：{msg}"
    assert f"重读 {len(packgen._RELOAD_WAITS)} 次" in msg, \
        f"失败文案里的次数与常量不是一本账（改了常量忘了改文案）：{msg}"


# ── 编码错不能与「文件不存在」同形（P2-7）────────────────────
def test_non_utf8_knowledge_file_is_reported_not_silently_empty(tmp_path, caplog):
    """GBK 存的知识 `.md` → 返回空串，但**必须留下一条带路径的 warning**。

    中文 Windows 上很容易出现这种文件。修复前 `read_text_cached` 把
    `UnicodeDecodeError` 和 `OSError` 一起吞掉、返回 ""，于是下游报的是
    「占位符未填充 / 切片为空」—— 与**文件不存在**同形，真正的原因（编码）
    无处可查。
    """
    p = tmp_path / "topics.md"
    p.write_bytes("## 核心术语\n家用电梯\n".encode("gbk"))
    with caplog.at_level(logging.WARNING, logger="app.knowledge"):
        got = knowledge.read_text_cached(p)
    assert got == "", f"读不出来应当返回空串，实际：{got!r}"
    warns = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warns, "编码错被静默吞掉了 —— 下游只会说「占位符未填充」，根因查不到"
    assert any(str(p) in r.getMessage() for r in warns), \
        f"warning 里没带路径，定位不到是哪个文件：{[r.getMessage() for r in warns]}"

    # 反向对照：同一次生成里重复读**只该说一次**（失败也进缓存）——
    # 不缓存的话一次生成读十几遍同一份文件，同一条 warning 会把日志淹掉，
    # 真正的第一条反而看不见（与 read_yaml_cached 缓存 err 同款取向）。
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="app.knowledge"):
        knowledge.read_text_cached(p)
    assert not caplog.records, "失败没进缓存 —— 每读一次刷一条，日志会被淹掉"


@win_only
def test_transient_oserror_is_not_pinned_into_the_text_cache(tmp_path):
    """P1-3：瞬时占用返回空串，但**不进缓存** —— 解除后同进程必须能读回来。

    P2-7 修复时把 OSError 与 UnicodeDecodeError 一起落进了缓存：缓存键是
    (mtime, size)，读失败时两者都没变，空串一旦入缓存就把瞬时故障
    （杀软/索引器按住刚落盘的文件，Windows 常态）钉成永久 —— 解除占用也救
    不回来，只能重启。yaml 那条（`read_yaml_cached` 的 retryable 口径）早就
    修过同款问题，两条必须是同一个口径。
    """
    p = tmp_path / "hooks.md"
    p.write_text("## 钩子库\n家用电梯\n", encoding="utf-8")
    fd = _lock(p)
    try:
        _assert_locked(p)
        assert knowledge.read_text_cached(p) == "", "占用中应按空内容处理"
    finally:
        _unlock(fd)
    assert knowledge.read_text_cached(p).startswith("## 钩子库"), \
        "占用已解除却仍返回空串：OSError 被钉进了文本缓存，与 yaml 口径相反"


def test_utf8_knowledge_file_logs_nothing(tmp_path, caplog):
    """正对照：正常 UTF-8 文件不许刷 warning（否则日志里全是噪音）。"""
    p = tmp_path / "ok.md"
    p.write_text("## 核心术语\n家用电梯\n", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="app.knowledge"):
        assert knowledge.read_text_cached(p).startswith("## 核心术语")
    assert not caplog.records, f"正常文件不该有 warning：{caplog.records}"


if __name__ == "__main__":
    raise SystemExit("用 pytest 跑：pytest tests/test_transient_read_fault.py")
