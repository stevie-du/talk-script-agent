# -*- coding: utf-8 -*-
"""第三方技能包导入：zip → 校验 → 原子落盘。

方案见 `docs/技能包系统方案.md` §4。设计前提：**第三方包 = 不可信输入**，
每一类威胁都有显式防线（不是"应该没问题"）：

| 威胁 | 防线 |
|---|---|
| Zip Slip（条目名 `../` 写出 packs 外） | 逐条目拒绝绝对路径/盘符/`..` 段 + 解压后再 resolve 复核 |
| 可执行文件走私 | 扩展名白名单（**不含 .py** —— `server._TEXT_SUFFIXES` 那个含 .py 的是"面板可读"，两回事） |
| 符号链接穿越 | external_attr 判 S_IFLNK，一律拒 |
| 解压炸弹 | 三重限：zip 总大小 / 条目数 / 单文件大小 |
| manifest 不兼容 | `pack_api` 版本闸（缺省当 1），超过引擎支持的直接拒 |
| 引用不存在的文件 | 结构校验：banwords 等 yaml 里声明的相对路径必须真实存在 |
| 静默覆盖用户改过的包 | 复用 packseed 的 `.packseed.json` + `tree_hash` 判"改过没"，改过就拒；**台账损坏**同样拒（"无法判断"≠"没记录"） |
| 落盘落一半 | 全校验在临时目录完成 → 同名先备份（同盘 rename）→ 失败整体回滚 |

与导出的对称性：同一个包格式（`pack.yaml` manifest）、同一条 private/ 约定
（导入的包含 private/ 就标出来，让用户看见这个包里带了别人的东西）。
"""
from __future__ import annotations

import io
import json
import re
import shutil
import tempfile
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .packseed import MANIFEST_NAME, PRIVATE_DIR_NAME, tree_hash

#: 导入侧的白名单。**刻意与 server._TEXT_SUFFIXES 不同**：那个是"知识库面板
#: 可读"（含 .py 是为了让作者贴的脚本也能看），这是"第三方包可装入"——
#: 包装进去的东西没有"看"的语义，只有"被引擎读"的语义，可执行文件一律禁。
ALLOWED_SUFFIXES = frozenset({".md", ".yaml", ".yml", ".txt", ".json"})

MAX_ZIP_BYTES = 20 * 1024 * 1024      # zip 总大小
MAX_FILE_BYTES = 1024 * 1024          # 解压后单文件
MAX_ENTRIES = 500                     # 条目数（含目录条目）

#: 引擎认识的技能包格式版本。老包不写 pack_api 字段 → 当 1。
PACK_API_SUPPORTED = 1

#: 行业包名的白名单。**单一来源**：server.py 的 `_safe_name`（web 层 400 语义）
#: 与本模块的 `import_pack` / `delete_pack`（导入层 PackImportError 语义）用的是
#: 同一条正则 —— 三处各写一份必然漂移，而"导入侧放行、API 侧拦截"（或反过来）
#: 都是同一份 zip 走两条路两个结果。错误文案可以分层，判定标准不行。
#: slug 允许中英文、数字、下划线与连字符，禁止 . / \ 等穿越字符。
#: ⚠ 用 `.fullmatch()`，**不**给正则加 `^`/`$`：Python 的 `$` 允许串尾多一个
#: 换行，于是 `"elevator\n"` 在引擎这边算合法，而 JS 的 `$` 不允许 —— 桩判不
#: 合法、引擎放行（第 16 轮复核实测，方向正是"桩比引擎严"那侧，也就是本仓库
#: 对账守卫写明不可接受的那一侧）。Windows 上尾部空白还会被文件系统吃掉，
#: `packs/elevator\n` 实际落到 `packs/elevator`：一条 URL 能指到真的包上。
_NAME_RE = re.compile(r"[\w\u4e00-\u9fff-]+")

#: 来源标记文件（导入时写进包里）。自包含：跟着包走，删包即消失，
#: 不依赖 packseed 台账的状态机 —— 台账会因升级/手动复制等原因失真，
#: 而"这个包是导入来的"这件事必须始终答得出来。
IMPORT_MARK = ".imported.json"


class PackImportError(Exception):
    """导入失败。message 是**给人看的一句话**（server 原样映射 400）。"""


@dataclass
class ImportResult:
    ok: bool
    name: str = ""
    version: int = 0
    author: str = ""
    license: str = ""
    has_private: bool = False
    replaced: bool = False
    note: str = ""


# Windows 保留设备名（不分区大小写、带不带扩展名都算）：以它为文件/目录名时
# `mkdir` / `open` 直接 OSError，而落盘在解压循环里发生 → 用户看到的是
# 500「导入过程中文件系统出错」，完全指不到是**哪个条目**的哪一段不行（P3）。
# 在白名单层就拒，报错点名条目。扩展名不算数：con.md 与 con 同罪。
_RESERVED_NAMES = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{i}" for i in range(1, 10)}
    | {f"lpt{i}" for i in range(1, 10)})


def _clean_rel(name: str) -> str:
    """zip 条目名 → 干净的 posix 相对路径。任何逃逸企图直接拒。"""
    s = name.replace("\\", "/")          # Windows 造的 zip 用反斜杠
    if not s or s.startswith("/"):
        raise PackImportError(f"zip 里有非相对路径的条目：{name!r}")
    if re.match(r"^[A-Za-z]:", s):       # 盘符
        raise PackImportError(f"zip 里有带盘符的条目：{name!r}")
    parts = []
    for seg in s.split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            raise PackImportError(f"zip 里有穿越路径的条目：{name!r}")
        stem = seg.split(".")[0].lower()
        if stem in _RESERVED_NAMES:
            raise PackImportError(
                f"zip 条目 {name!r} 用了 Windows 保留设备名「{stem}」——"
                "这种名字在落盘时必然失败，请改名后再打包")
        parts.append(seg)
    if not parts:
        raise PackImportError(f"zip 里有空路径条目：{name!r}")
    return "/".join(parts)


def _is_symlink(zi: zipfile.ZipInfo) -> bool:
    import stat
    return stat.S_ISLNK(zi.external_attr >> 16)


def _find_pack_root(tmp: Path) -> Path:
    """定位"哪一层是包根"（含 pack.yaml 的那层）。

    两种常见形态都接受：
      ① zip 根就是包目录（pack.yaml 在顶层）
      ② zip 里裹了一个顶层目录（打包时整个文件夹选进去的那种）
    别的形态（嵌套两层以上、没有 pack.yaml）一律拒 —— 猜会让"导了个
    什么进去"变得不可回答。
    """
    if (tmp / "pack.yaml").exists():
        return tmp
    dirs = [d for d in tmp.iterdir() if d.is_dir()]
    if len(dirs) == 1 and (dirs[0] / "pack.yaml").exists():
        return dirs[0]
    raise PackImportError("zip 里找不到 pack.yaml（包根必须是 zip 根或唯一顶层目录）")


def _read_manifest(pack_root: Path) -> dict:
    """读并校验 pack.yaml。返回归一后的 manifest 信息。"""
    try:
        text = (pack_root / "pack.yaml").read_text(encoding="utf-8")
    except OSError as e:
        raise PackImportError(f"pack.yaml 读不出来：{e}")
    try:
        import yaml
        data = yaml.safe_load(text)
    except Exception as e:  # noqa: BLE001
        raise PackImportError(f"pack.yaml 不是合法 YAML：{e}")
    if not isinstance(data, dict):
        raise PackImportError("pack.yaml 顶层必须是映射（键值对）")
    name = str(data.get("name") or "").strip()
    if not _NAME_RE.fullmatch(name) or ".." in name:
        raise PackImportError(f"pack.yaml 的 name 不合法：{name!r}"
                              "（只能字母/数字/中文/连字符，不能有路径字符）")
    version = data.get("version", 1)
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise PackImportError(f"pack.yaml 的 version 必须是 ≥1 的整数，现在是 {version!r}")
    pack_api = data.get("pack_api", 1)
    if not isinstance(pack_api, int) or isinstance(pack_api, bool):
        raise PackImportError(f"pack_api 必须是整数，现在是 {pack_api!r}")
    if pack_api > PACK_API_SUPPORTED:
        raise PackImportError(
            f"这个包要求技能包格式 v{pack_api}，当前引擎只支持 v{PACK_API_SUPPORTED} —— "
            "请升级 TalkScript 后再导入")
    src = data.get("source") if isinstance(data.get("source"), dict) else {}
    return {"name": name, "version": version,
            "author": str(src.get("author") or ""),
            "license": str(src.get("license") or ""),
            "banwords_rel": str(data.get("banwords", "banwords.yaml"))}


def _check_structure(pack_root: Path, banwords_rel: str) -> None:
    """结构校验：引擎**必然要读**的文件必须真实存在。

    引用不存在的文件 = 静默失效（合规词表缺失会让校验全过，是 knowledge 模块
    注释里点名的"本组问题里最严重的"那种），所以拦在导入前而不是导入后。
    """
    for rel in ("skill.yaml", banwords_rel):
        if not rel:
            raise PackImportError("pack.yaml 里的文件引用是空的")
        # 逃逸检查放**前面**：路径跑出包外是安全问题，报"缺少 xxx"会把
        # 用户引向"哦少个文件"，而真相是"这个包在试图读包外的东西"。
        try:
            (pack_root / rel).resolve().relative_to(pack_root.resolve())
        except ValueError:
            raise PackImportError(f"{rel} 的路径跑到了包外面")
        if not (pack_root / rel).exists():
            raise PackImportError(f"包缺少 {rel} —— 引擎加载时要读它")


def _ledger_broken(name: str, why: str) -> PackImportError:
    """台账坏了的**唯一出口**：说清"无法判断"，并给出可操作的下一步。

    三种坏法（读不出 / 不是 JSON / 结构不对）共用一句文案 —— 分成三份必然漂移，
    而用户要的信息是同一件：这次导入为什么被中止、接下来该动哪个文件。
    """
    return PackImportError(
        f"行业包台账（{MANIFEST_NAME}）{why} —— 无法判断 {name} 是不是你改过的"
        f"内置包。为免覆盖掉你的改动，本次导入已中止；"
        f"请修复或删除该文件（{MANIFEST_NAME}）后重试")


def _user_modified(packs_dir: Path, name: str) -> bool | None:
    """用户改过这个（播种来的）包吗？

    - `True`  = 改过（调用方必须拒）
    - `False` = 没改过（可覆盖）
    - `None`  = **台账里本来就没有这个包**（用户自己导入的，覆盖是他的选择）

    ⚠ 「台账**读不出来**」**不**返回 `None`，而是抛 `PackImportError`。
    它与「台账里没这条」是两件事：前者我们**不知道**用户改没改，后者我们
    **知道**这不是播种来的包。拿"不确定"冒充"没记录"，两者的正确动作恰好相反
    （拒 vs 放行），代价是覆盖 + 删备份、用户改动**不可逆丢失**。
    packseed 对台账损坏是「不动任何已有包」（`seed_bundled_packs` 的
    `except (OSError, ValueError)` 分支），这里必须同向。
    """
    dst = packs_dir / name
    if not dst.exists():
        return None
    ledger = packs_dir / MANIFEST_NAME
    try:
        raw = ledger.read_text(encoding="utf-8")
    except FileNotFoundError:
        # 从来没有过台账 = 这个目录没被播种过 → 里面的包都是用户自己的
        return None
    except OSError as e:
        raise _ledger_broken(name, f"读不出来：{e}")
    try:
        rec_all = json.loads(raw)
    except ValueError as e:
        raise _ledger_broken(name, f"不是合法 JSON：{e}")
    rec = rec_all.get("packs") if isinstance(rec_all, dict) else None
    if not isinstance(rec, dict):
        raise _ledger_broken(name, '结构不对（顶层应为 {"app_version": …, "packs": {…}}）')
    stored = rec.get(name)
    if not isinstance(stored, dict):
        return None
    return stored.get("local") != tree_hash(dst, skip_private=True)


def _swap_in(tmp_pack: Path, dst: Path) -> bool:
    """原子落盘：先备份旧的（同盘 rename，瞬间完成），再搬新的。

    返回是否替换了已存在的包。任何一步失败都回滚到原状 ——
    "导了个包结果旧的没了新的没进"比导入失败糟糕得多。
    """
    if not dst.exists():
        shutil.move(str(tmp_pack), str(dst))
        return False
    backup = dst.parent / f".import-backup-{dst.name}"
    if backup.exists():
        shutil.rmtree(backup, ignore_errors=True)
    dst.rename(backup)                     # 同盘 rename：原子
    try:
        shutil.move(str(tmp_pack), str(dst))
    except Exception:
        # ⚠ tmp 在 %TEMP%（通常 C:），packs_dir 在另一块盘时 `shutil.move`
        #   退化为 copytree+删除，中途失败会留下**半截新包**在 dst ——
        #   此时不能跳过回滚（P2-3：曾经 `if not dst.exists()` 把这种情况
        #   放过去，旧包躺在备份里、dst 是半成品，报错却断言「已还原原样」）。
        #   dst 上的东西必然是这次失败搬入的残骸（旧包已整体挪进 backup），
        #   清掉它才算把现场还原干净。
        if dst.is_dir():
            shutil.rmtree(dst, ignore_errors=True)
        elif dst.exists():
            dst.unlink()
        if not dst.exists() and backup.exists():
            backup.rename(dst)             # 回滚
        if not dst.exists():
            # 连回滚都失败：旧包完整躺在备份里 —— 留下它并说实话
            raise PackImportError(
                f"新包搬进用户目录失败，且自动还原也没成功："
                f"你的原包完整保留在 {backup}，请手工把它挪回 {dst}")
        raise PackImportError("新包搬进用户目录失败，已还原原样")
    shutil.rmtree(backup, ignore_errors=True)
    return True


def validate_zip(zip_bytes: bytes, tmp: Path) -> tuple[ImportResult, Path]:
    """zip → 预检 → 解压进 tmp → manifest/结构校验。**不落盘**。

    返回 (结果, 包根目录)。调用方掌握 tmp 的生命周期，自己决定接下来是
    落盘（import_pack）还是只出报告（CLI）。

    ⚠ 拆出这一层的意义：服务端导入与 `python -m app.packcheck` 校验 CLI
    必须是**同一份代码**。各写一份的后果是 CLI 放行的包服务端拒（或反过来），
    包作者拿着自检通过的文件兴冲冲导入失败，两边文案还对不上。
    """
    if len(zip_bytes) > MAX_ZIP_BYTES:
        raise PackImportError(f"zip 太大（{len(zip_bytes) // 1024 // 1024}MB，"
                              f"上限 {MAX_ZIP_BYTES // 1024 // 1024}MB）")
    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except zipfile.BadZipFile as e:
        raise PackImportError(f"这不是一个有效的 zip 文件：{e}")

    with zf:
        infos = zf.infolist()
        if len(infos) > MAX_ENTRIES:
            raise PackImportError(f"zip 里条目太多（{len(infos)}，上限 {MAX_ENTRIES}）")
        rels: list[tuple[str, zipfile.ZipInfo]] = []
        for zi in infos:
            rel = _clean_rel(zi.filename)
            if zi.is_dir():
                continue
            if _is_symlink(zi):
                raise PackImportError(f"zip 里有符号链接条目（{rel}）—— 一律拒收")
            if Path(rel).suffix.lower() not in ALLOWED_SUFFIXES:
                raise PackImportError(f"zip 里有不允许的文件类型：{rel}"
                                      f"（只接受 {' '.join(sorted(ALLOWED_SUFFIXES))}）")
            if zi.file_size > MAX_FILE_BYTES:
                raise PackImportError(f"zip 里 {rel} 太大（{zi.file_size // 1024}KB，"
                                      f"上限 {MAX_FILE_BYTES // 1024}KB）")
            rels.append((rel, zi))
        for rel, zi in rels:
            dest = tmp / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(zi) as src, open(dest, "wb") as out:
                shutil.copyfileobj(src, out, length=1024 * 64)
            if dest.stat().st_size > MAX_FILE_BYTES:   # 声明的size可能说谎
                # 与上面解压前的 file_size 预检是**同一上限的两道**
                # （预检防"声明超大"，这道防"元数据说谎"——恶意 zip 才有）。
                raise PackImportError(f"{rel} 解压后超过 {MAX_FILE_BYTES // 1024}KB")
            # 解压后再复核一次路径（防 dest 被 symlink 目录导出去——
            # 上面禁了 zip 内 symlink，这里兜底文件系统层面的）
            if not dest.resolve().is_relative_to(tmp.resolve()):
                raise PackImportError(f"{rel} 解压后跑到了临时目录外")

        pack_root = _find_pack_root(tmp)
        man = _read_manifest(pack_root)
        _check_structure(pack_root, man["banwords_rel"])
        return ImportResult(
            ok=True, name=man["name"], version=man["version"],
            author=man["author"], license=man["license"],
            has_private=(pack_root / PRIVATE_DIR_NAME).exists()), pack_root


def validate_dir(pack_dir: Path) -> ImportResult:
    """校验一个**已解压的**包目录（CLI 的目录模式）。zip 层不适用。"""
    pack_root = _find_pack_root(pack_dir)
    man = _read_manifest(pack_root)
    _check_structure(pack_root, man["banwords_rel"])
    return ImportResult(
        ok=True, name=man["name"], version=man["version"],
        author=man["author"], license=man["license"],
        has_private=(pack_root / PRIVATE_DIR_NAME).exists())


def import_pack(zip_bytes: bytes, packs_dir: Path) -> ImportResult:
    """把 zip 里的技能包导入 packs_dir。失败抛 PackImportError（人话原因）。"""
    with tempfile.TemporaryDirectory(prefix="packimport-") as td:
        r, pack_root = validate_zip(zip_bytes, Path(td))
        name = r.name

        dst = packs_dir / name
        replaced = dst.exists()
        if replaced:
            if _user_modified(packs_dir, name) is True:
                raise PackImportError(
                    f"你改过内置包 {name} —— 导入会丢掉你的修改。"
                    "请先备份你的版本（或把它改名），再导入")

        # 自包含来源标记：作者/许可证/导入时间。license 只是展示，
        # 引擎不裁决（方案 §9 红线 4）。
        mark = {"author": r.author, "license": r.license,
                "imported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "pack_version": r.version}
        (pack_root / IMPORT_MARK).write_text(
            json.dumps(mark, ensure_ascii=False, indent=1), encoding="utf-8")

        try:
            _swap_in(pack_root, dst)
        except PackImportError:
            raise
        except OSError as e:
            raise PackImportError(f"搬进用户目录失败：{e}")

    r.replaced = replaced
    r.note = ("已覆盖同名包 v%s" % r.version) if replaced else "新导入"
    return r


def delete_pack(name: str, packs_dir: Path) -> dict:
    """卸载导入的包。**播种来的内置包不给删** —— 删了下周播种又回来，
    用户看到的是"卸载没用"。给的路子是「恢复默认」（packseed 重播）。
    """
    if not _NAME_RE.fullmatch(name or "") or ".." in name:
        raise PackImportError("包名不合法")
    dst = packs_dir / name
    if not dst.exists():
        raise PackImportError(f"包不存在：{name}")
    if not (dst / IMPORT_MARK).exists():
        raise PackImportError(
            f"{name} 是内置行业包，不能卸载 —— 它由安装目录播种而来，"
            "删除后下次启动会被重新放回来。想要旧版请从安装目录恢复")
    try:
        shutil.rmtree(dst)
    except OSError as e:
        raise PackImportError(f"删除失败（文件被占用？）：{e}")
    return {"ok": True, "name": name}
