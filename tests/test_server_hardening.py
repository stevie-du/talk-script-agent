# -*- coding: utf-8 -*-
"""批次 D 的回归测试：python tests/test_server_hardening.py

覆盖三件事：
  D1 配置不再是一次性闭包 —— 设置里保存的 Key/模型立刻生效（不用重启）。
      symptom：填完 Key 去新建行业包，还被拦「未配置 API Key」；/api/meta
     回到首页仍显示旧模型名。
  D2 CORS 只放行本机 —— 原来是 allow_origins=["*"]，任意网页都能读走你的
     历史脚本 / 起作业烧额度 / 删记录。
  D3 落盘原子化 —— 写到一半被杀只剩半截文件的几种后果（记录凭空消失、
     配置写坏启动不了、包列表崩掉）。
"""
import json
import shutil
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import load_config, save_config          # noqa: E402
from app.fileio import write_atomic                      # noqa: E402
from app.server import create_app                        # noqa: E402

try:
    from fastapi.testclient import TestClient
except ImportError:                                       # pragma: no cover
    TestClient = None


def _tmp_root() -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="talkscript-hardening-"))
    shutil.copytree(ROOT / "packs", tmp / "packs")
    # 干净起点：不带任何已保存的 Key / 模型
    (tmp / "config.yaml").write_text("llm:\n  api_key: ''\n  model: startup-model\n",
                                     encoding="utf-8")
    return tmp


# ── D1 配置现读 ────────────────────────────────────────────
def case_config_fresh(tmp: Path):
    import app.server as srv

    app = create_app(tmp)
    c = TestClient(app)

    before = c.get("/api/meta")
    assert before.status_code == 200, before.status_code
    assert before.json()["model"] == "startup-model", before.json()
    assert before.json()["has_api_key"] is False, before.json()

    # 没有任何 Key 时，建包必须先被拦下
    with patch.object(srv, "create_pack", lambda *a, **kw: {"name": "fake"}):
        denied = c.post("/api/packs/create",
                        json={"industry": "假体陀机", "description": "测试用行业描述"})
    assert denied.status_code == 400, (denied.status_code, denied.text)
    assert "未配置" in denied.json()["detail"], denied.text

    # 用户在设置页保存 Key 与模型（这一步之后不重启）
    save_config(tmp, {"api_key": "sk-after-startup", "model": "saved-model"})

    after = c.get("/api/meta")
    assert after.json()["model"] == "saved-model", after.json()
    assert after.json()["has_api_key"] is True, after.json()

    with patch.object(srv, "create_pack", lambda *a, **kw: {"name": "fake"}):
        allowed = c.post("/api/packs/create",
                         json={"industry": "假体陀机", "description": "测试用行业描述"})
    assert allowed.status_code == 200, (allowed.status_code, allowed.text)

    # health 同样反映最新的 mock 开关
    h = c.get("/api/health").json()
    assert h["mock"] is False, h


# ── D2 CORS 只放行本机 ──────────────────────────────────────
def case_cors_local_only(tmp: Path):
    app = create_app(tmp)
    c = TestClient(app)

    # 外部站点：一律拒绝，且不回 Allow-Origin（浏览器读不到内容）
    evil = {"Origin": "https://evil.example"}
    r = c.get("/api/history", headers=evil)
    assert r.status_code == 403, (r.status_code, r.text)
    assert "access-control-allow-origin" not in r.headers, dict(r.headers)

    pre = c.options("/api/jobs/x/confirm", headers={
        "Origin": "https://evil.example",
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "content-type"})
    assert pre.status_code == 403, pre.status_code

    # 写操作的跨站同样被拦在预检之前
    r = c.post("/api/generate", headers=evil, json={"pack": "elevator", "topic": "t"})
    assert r.status_code == 403, (r.status_code, r.text)

    for origin in ["http://evil.example", "https://talkscript.attacker.io:8765",
                   "http://192.168.0.9:8765"]:
        r = c.get("/api/meta", headers={"Origin": origin})
        assert r.status_code == 403, (origin, r.status_code)

    # 本机 / Electron：放行并正确回显 Origin
    for origin in [None, "null", "file://", "app://-",
                   "http://127.0.0.1:8765", "http://localhost:8765"]:
        h = {} if origin is None else {"Origin": origin}
        r = c.get("/api/meta", headers=h)
        assert r.status_code == 200, (origin, r.status_code, r.text)
        if origin is not None:
            assert r.headers["access-control-allow-origin"] == origin, dict(r.headers)

    # 预检（Electron 发 application/json 的 POST 一定会预检）
    pre = c.options("/api/generate", headers={
        "Origin": "null",
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "content-type"})
    assert pre.status_code == 204, (pre.status_code, pre.text)
    assert pre.headers["access-control-allow-methods"], dict(pre.headers)


# ── D3 落盘原子性 ──────────────────────────────────────────
def case_atomic_write(tmp: Path):
    target = tmp / "result.json"
    original = json.dumps({"id": "old-version", "pack": "elevator"}, ensure_ascii=False)
    target.write_text(original, encoding="utf-8")

    seen = {}

    def killed_replace(src, dst, **kw):
        # 模拟进程在「内容已写进临时文件、尚未换上去」时被杀：
        # 此刻目标文件必须还是完整可读的旧版本，而不是写了半截的新内容。
        seen["target_before_replace"] = target.read_text(encoding="utf-8")
        seen["tmp_path"] = str(src)
        seen["tmp_is_sibling"] = Path(src).parent == Path(dst).parent
        raise KeyboardInterrupt("simulated kill")

    with patch("app.fileio.os.replace", side_effect=killed_replace):
        try:
            write_atomic(target, json.dumps({"id": "new-version"}, ensure_ascii=False))
        except KeyboardInterrupt:
            pass

    assert seen, "没有走到 replace —— 断言没打在真正的写入路径上"
    assert seen["target_before_replace"] == original, seen["target_before_replace"][:80]
    assert seen["tmp_is_sibling"] is True, seen        # 临时文件必须同目录才 rename 得动
    assert seen["tmp_path"] != str(target), seen

    # 被中断后目标仍是完整的旧版本，且没留下临时文件垃圾
    assert target.read_text(encoding="utf-8") == original
    json.loads(target.read_text(encoding="utf-8"))
    assert list(tmp.glob("result.json.tmp-*")) == [], list(tmp.glob("*"))

    # 正常路径：目标被完整替换，且不残留临时文件
    new_text = json.dumps({"id": "new-version", "sections": [{"text": "x"}]},
                          ensure_ascii=False)
    write_atomic(target, new_text)
    assert json.loads(target.read_text(encoding="utf-8"))["id"] == "new-version"
    assert list(tmp.glob("*.tmp-*")) == [], list(tmp.glob("*"))


def case_config_roundtrip(tmp: Path):
    """save_config 现在走原子写：内容仍然完整，且不会在根目录丢垃圾。"""
    save_config(tmp, {"api_key": "sk-a", "model": "m-a"})
    save_config(tmp, {"model": "m-b"})
    cfg = load_config(tmp)
    assert cfg.llm.model == "m-b", cfg.llm.model
    assert cfg.llm.api_key == "sk-a", cfg.llm.api_key       # 空值不覆盖语义要保留
    assert list(tmp.glob("config.yaml.tmp-*")) == [], list(tmp.glob("*"))


def main():
    if TestClient is None:
        print("跳过：缺少 fastapi.testclient（pip install httpx）")
        return 1
    cases = [case_config_fresh, case_cors_local_only, case_atomic_write,
             case_config_roundtrip]
    roots, failed = [], 0
    for i, case in enumerate(cases, 1):
        tmp = _tmp_root()
        roots.append(tmp)
        try:
            case(tmp)
            print(f"[{i}] {case.__name__} 通过 ✅")
        except AssertionError as e:
            failed += 1
            print(f"[{i}] {case.__name__} 失败 ❌ {e}")
        except Exception as e:                              # noqa: BLE001
            failed += 1
            print(f"[{i}] {case.__name__} 异常 ❌ {type(e).__name__}: {e}")
    for r in roots:
        shutil.rmtree(r, ignore_errors=True)
    print(f"\n{len(cases) - failed}/{len(cases)} 通过")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
