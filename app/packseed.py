# -*- coding: utf-8 -*-
"""出厂行业包 → 可写行业包目录的「播种」（缺陷 8 的后半段）。

问题
----
打包版里 `--root` 指安装目录（默认
`%LOCALAPPDATA%\\Programs\\TalkScript\\resources\\engine`），行业包就住在
`<root>/packs`。那个位置有两个致命性质：

  1. **升级 / 卸载会整个抹掉它** —— NSIS 只删自己装进去的文件听起来很安全，
     但 electron-builder 的 appUpdate 与「装到同一目录」的覆盖安装都会重写
     `resources/`；用户自己点一次「检查更新」就丢包。
  2. 它**可能根本不可写**（换成 `Program Files` 那种机器级安装时）——
     新建行业包（`packgen.create_pack` 写 `root/packs/<slug>`）会直接失败。

而 README 承诺"配置与产物在数据目录、卸载不丢"，唯独没提行业包 ——
用户手改的 `banwords.yaml`、填的 `private/products.yaml`、向导生成的整包
都在承诺之外。修法是：可写包根 = `<userData>/packs`，安装目录那份退化成
**只读种子**。

这里要解决的就是退化带来的新问题：**出厂包的改进怎么到达用户**。
一刀切"永远不覆盖"的话，用户永远拿到的是首次安装那一刻的 elevator 包
（而这个仓库里 elevator 包一直在被修），一刀切"每次都覆盖"的话就是我们
正要修的那个 bug。所以按三个事实分派，判断依据记在 `<packs>/.packseed.json`：

  · 出厂那份**变了**吗（相对上次播种）；
  · 用户那份**被他改过**吗（相对上次播种，`private/` 不算 —— 那本来就是
    用户自己的东西，而播种永远不会带出 private/，见 desktop/package.json）。

改过就**绝不动**它，并记一条 WARNING 说清"出厂更新没并进来、要手动合"，
不静默；没改过就同步过去，同时保住目录里已有的 `private/`。

⚠ 这个模块**不许让引擎起不来**：所有 IO 失败都只记日志并汇总返回，
最坏结果是列表里少了几个出厂行业，而不是白屏。
"""
from __future__ import annotations

import hashlib
import json
import logging
import shutil
from pathlib import Path

from .fileio import write_atomic

log = logging.getLogger(__name__)

# 播种台账的文件名。放在包根目录里（不是某个包内），且**不是目录**，
# `knowledge.list_packs` 只认「含 pack.yaml 的子目录」，所以它不会变成一个包。
MANIFEST_NAME = ".packseed.json"

# 播种与算哈希时跳过的东西：字节码缓存不是内容。
SKIP_DIR_NAMES = frozenset({"__pycache__"})
PRIVATE_DIR_NAME = "private"


def tree_hash(pack_dir: Path, skip_private: bool = False) -> str:
    """一棵目录树的稳定指纹（改动 = 指纹变）。

    只用 `相对路径 + 字节数 + 内容 sha256`，不含 mtime —— 安装/解压会把 mtime
    全部刷新，拿它比对的后果是"每次启动都以为出厂包更新了"。
    排序后再喂给哈希，遍历顺序不影响结果。
    """
    h = hashlib.sha256()
    if not pack_dir.exists():
        return "∅"
    for f in sorted(pack_dir.rglob("*")):
        if not f.is_file():
            continue
        rel = f.relative_to(pack_dir)
        if SKIP_DIR_NAMES & set(rel.parts):
            continue
        if skip_private and rel.parts[:1] == (PRIVATE_DIR_NAME,):
            continue
        h.update(rel.as_posix().encode("utf-8"))
        try:
            h.update(f.read_bytes())
        except OSError as e:
            # 读不动（占用 / 权限）也要留痕：跳过等于"这份没进指纹"，
            # 下一次可能被判成"用户没改过"而被覆盖。宁可让指纹变化。
            h.update(f"<unreadable:{e}>".encode("utf-8"))
        h.update(b"\0")
    return h.hexdigest()[:16]


def _is_pack_dir(d: Path) -> bool:
    try:
        return d.is_dir() and (d / "pack.yaml").exists()
    except OSError:
        return False


def _rmtree_if_exists(p: Path) -> None:
    if p.exists():
        shutil.rmtree(p, ignore_errors=True)


def _copy_pack(src: Path, dst: Path) -> None:
    """出厂包 → 用户目录，**保住用户已有的 private/**，且失败不撕包（P1-5）。

    为什么先拷到临时目录、再整体 rename 换入，而不是"先删旧内容再拷"：
    `copytree(dirs_exist_ok=True)` 只增不删，为了让"出厂删掉了某个 md"这件事
    也传过去，删除不可避免 —— 但**先删后拷**的窗口里任何一步失败（磁盘满 /
    杀软按住源文件 / 权限，Windows 常态），用户那份就被撕成半残：台账里
    `stored.local` 还是旧指纹，半残内容对不上它，从此每次启动都判成
    「用户改过」（conflict 分支不修复、不重播），应用内又删不掉内置包 ——
    只能手工救。先拷后换：**换入之前用户那份一个字节都没动过**。

    换入用 rename（同盘原子）：旧包整个挪进 `.seed-old` 备份，新包就位后才
    删备份；中途失败先把旧包还原回来再抛。暂存/备份目录点开头，
    `list_packs` 不把它们当包（见 knowledge.list_packs 的点前缀过滤）。
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.parent / f".{dst.name}.seed-new"
    _rmtree_if_exists(tmp)                    # 上次失败可能留下残骸
    try:
        shutil.copytree(src, tmp)
        # bundled 的 private/ 是**空模板**：换入时以用户的 private 为准（老语义：
        # 播种永不覆盖用户私有资料）—— 先把出厂那份整体清掉，再把用户的拷进来。
        # 直接 copytree(user → tmp) 会与出厂模板撞目录（WinError 183）。
        shutil.rmtree(tmp / PRIVATE_DIR_NAME, ignore_errors=True)
        if dst.exists() and (dst / PRIVATE_DIR_NAME).exists():
            shutil.copytree(dst / PRIVATE_DIR_NAME, tmp / PRIVATE_DIR_NAME,
                            dirs_exist_ok=True)
    except OSError:
        _rmtree_if_exists(tmp)
        raise
    if not dst.exists():                      # 首次播种：没有旧包可备份
        try:
            tmp.rename(dst)
        except OSError:
            _rmtree_if_exists(tmp)
            raise
        return
    backup = dst.parent / f".{dst.name}.seed-old"
    _rmtree_if_exists(backup)
    try:
        dst.rename(backup)                    # 此刻起 dst 缺位；失败则 dst 原样
        try:
            tmp.rename(dst)
        except OSError:
            backup.rename(dst)                # 先还原旧包；还原再失败交给外层留话
            raise
    except OSError:
        _rmtree_if_exists(tmp)
        if not dst.exists() and backup.exists():
            # 还原也失败了：旧包完整躺在备份里 —— 宁可留一个隐藏目录也不丢数据
            log.warning("行业包 %s 换入失败且自动还原也没成功："
                        "你的原包完整保留在 %s，请手工把它挪回原位", dst, backup)
        raise
    _rmtree_if_exists(backup)


def seed_bundled_packs(bundled: Path, packs_dir: Path, app_version: str = "") -> dict:
    """把 `bundled`（安装目录里那份）播种进可写的 `packs_dir`。

    返回一份汇总（`{"copied": [...], "updated": [...], "kept": [...],
    "skipped": "..."}`），调用方只用来打日志；**不抛异常**。
    """
    summary: dict = {"copied": [], "updated": [], "kept": [], "conflicts": []}
    try:
        bundled_r = bundled.resolve()
        target_r = packs_dir.resolve()
    except OSError as e:
        log.warning("行业包目录解析失败（%s / %s）：%s", bundled, packs_dir, e)
        return {**summary, "skipped": f"路径解析失败：{e}"}
    if bundled_r == target_r:
        # 开发态（--packs-dir 不给，或给的就是项目里的 packs）：没有两份，不需要播种。
        return {**summary, "skipped": "同一目录"}
    if not bundled_r.exists():
        return {**summary, "skipped": f"安装目录里没有包：{bundled_r}"}

    manifest_path = target_r / MANIFEST_NAME
    try:
        rec_all = json.loads(manifest_path.read_text(encoding="utf-8"))
        rec_all = rec_all if isinstance(rec_all, dict) else {}
    except FileNotFoundError:
        rec_all = {}
    except (OSError, ValueError) as e:
        # 台账坏了不影响正确性（判断退回"没记录"= 不动用户的东西），
        # 但**要说**：否则下次看到"出厂包没同步"没人知道是台账丢了。
        log.warning("行业包播种台账读取失败（%s）：%s —— 本次不会覆盖任何已有包",
                    manifest_path, e)
        rec_all = {}
    rec = rec_all.get("packs") if isinstance(rec_all.get("packs"), dict) else {}

    try:
        target_r.mkdir(parents=True, exist_ok=True)
        entries = sorted(p for p in bundled_r.iterdir() if _is_pack_dir(p))
    except OSError as e:
        log.warning("行业包目录读写失败：%s —— 跳过播种", e)
        return {**summary, "skipped": str(e)}

    changed = False
    for src in entries:
        name = src.name
        dst = target_r / name
        try:
            src_hash = tree_hash(src)
            if not dst.exists():
                _copy_pack(src, dst)
                rec[name] = {"version": app_version, "bundled": src_hash,
                             "local": tree_hash(dst, skip_private=True)}
                summary["copied"].append(name)
                changed = True
                continue

            stored = rec.get(name)
            local_hash = tree_hash(dst, skip_private=True)
            if not isinstance(stored, dict):
                # 用户目录里已经有一个同名包，而台账里没有它：
                # 老数据 / 用户自己拷进来的 / 本功能上线前建的 —— 一律**不动**，
                # 只把现状记下来，让以后的出厂更新可比对。
                rec[name] = {"version": app_version, "bundled": src_hash,
                             "local": local_hash, "adopted": True}
                summary["kept"].append(name)
                changed = True
                log.info("行业包 %s 在用户目录里已存在（不是本功能拷进去的）——"
                         "保留你的那份，出厂版本没有并进来", name)
                continue

            if stored.get("bundled") == src_hash:
                # 出厂那份自上次播种以来没变：无事可做（**一个字节都不写**，
                # 否则每次启动都要重刷 12 个文件的 mtime，用户编辑器的"文件已改动"
                # 提示会被点回来）。
                continue

            if stored.get("local") == local_hash:
                _copy_pack(src, dst)
                rec[name] = {"version": app_version, "bundled": src_hash,
                             "local": tree_hash(dst, skip_private=True)}
                summary["updated"].append(name)
                changed = True
                log.info("行业包 %s 已同步出厂更新（你没改过它）：%s → %s",
                         name, stored.get("version") or "?", app_version or "?")
            else:
                # 用户改过 —— 这是唯一"两边都有道理"的冲突，必须留下话。
                # 不自动合并：合并 YAML 里的词表与提示词片段是猜意，
                # 猜错的代价是产出悄悄变差而没人知道（本项目最忌讳的静默降级）。
                summary["conflicts"].append(name)
                log.warning("行业包 %s 你改过（%s），所以**没有**同步新版本自带的改动；"
                            "想要那些改动请手工合并：出厂那份在 %s",
                            name, dst, src)
                # bundled 指纹**不更新**：这条冲突下次启动还会再说一遍。
                # 一次就消失的提醒等于没有提醒 —— 用户不一定在看着日志。
        except OSError as e:
            # 失败的用户包**原样还在**（_copy_pack 先拷后换），只影响本次同步
            log.warning("行业包 %s 播种失败：%s —— 用户目录里那份没有被动过，"
                        "继续处理其余的", name, e)

    if changed or not manifest_path.exists():
        try:
            write_atomic(manifest_path, json.dumps(
                {"app_version": app_version, "packs": rec},
                ensure_ascii=False, indent=2))
        except OSError as e:
            log.warning("行业包播种台账写入失败（%s）：%s", manifest_path, e)
    return summary
