# -*- coding: utf-8 -*-
"""param_audit：把「用户能选、但本包没给对应定制」的参数值摊到界面上。

背景
----
这些值不会报错，只会静默走通用默认 —— 用户加了个「快手」以为照常查平台红线，
实际平台差异化校验整个不生效；加了新细分领域，注进来的却是别的章节（修好之前的
`topics_slice`）或者干脆什么都没有（修好之后：不注入，见 P2-4）。
**静默降级比报错更危险**，所以这里要钉住两件事：

1. 每种缺口都能被抓到（不能漏报）；
2. 没有缺口时一个都不报（不能误报）—— 误报会让这个提示彻底失去可信度。

与 `checker.Banwords.dropped_short` 同一取向：别让包作者以为「写了就生效」。
"""
from pathlib import Path

import yaml

from app.knowledge import param_audit, pack_info

ROOT = Path(__file__).resolve().parents[1]
ELEVATOR = ROOT / "packs" / "elevator"

BASE = {
    "params": {
        "segment": {"label": "细分领域", "default": "维保", "options": ["维保"]},
        "audience": {"label": "受众", "default": "业主", "options": ["业主"]},
        "duration": {"label": "时长", "default": 60, "options": [60]},
        "style": {"label": "风格", "default": "亲和", "options": ["亲和"]},
        "platform": {"label": "平台", "default": "抖音", "options": ["抖音"]},
        "persona": {"label": "人设", "default": "老师傅", "options": ["老师傅"]},
    },
    "topics_map": {"维保": "维保", "通用": "安全科普"},
    "audience_map": {"业主": "业主"},
    "rate_by_style": {"亲和": 4.5},
    # 两档（30/60）—— 这样「45 秒」才是真的**插值**。
    # 只有一档时 `Quota._row` 是「任何时长都取该档」，说成插值是误导，
    # 已单独修正；整表缺失 / 单档 / 缺档三条路径的断言见
    # tests/test_quota_degraded_signal.py 第 5 组。
    "quota_table": {30: {"total": 145, "hook": 25, "body": 90, "cta": 30},
                    60: {"total": 290, "hook": 45, "body": 190, "cta": 55}},
    "points_by_duration": {60: 3},
}

TOPICS_MD = "## 维保\n维保内容\n\n## 安全科普\n安全内容\n"
AUDIENCE_MD = "## 业主\n业主内容\n"


def _pack(tmp_path: Path, mutate=None, files=None) -> Path:
    """写一个最小可用的行业包目录；mutate 用来改配置制造缺口。"""
    data = yaml.safe_load(yaml.safe_dump(BASE, allow_unicode=True))
    if mutate:
        mutate(data)
    (tmp_path / "knowledge").mkdir(parents=True, exist_ok=True)
    (tmp_path / "pack.yaml").write_text(
        yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    (tmp_path / "banwords.yaml").write_text(
        yaml.safe_dump({"hard": [], "platform": {"抖音": {}}}, allow_unicode=True),
        encoding="utf-8")
    written = {"knowledge/topics.md": TOPICS_MD, "knowledge/audience.md": AUDIENCE_MD}
    written.update(files or {})
    for rel, text in written.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return tmp_path


def test_real_pack_has_no_false_positive():
    """电梯包每个选项都配了对应表 —— 一个都不该报。

    这是本测试最重要的一条：提示一旦误报，用户就会忽略它。
    """
    data = yaml.safe_load((ELEVATOR / "pack.yaml").read_text(encoding="utf-8"))
    assert param_audit(ELEVATOR, data) == {}


def test_complete_pack_reports_nothing(tmp_path):
    assert param_audit(_pack(tmp_path), yaml.safe_load(
        (tmp_path / "pack.yaml").read_text(encoding="utf-8"))) == {}


# ── points_by_duration 的值域（P1）──────────────────────────
#
# 以前只判「键在不在表里」，0 / 负数全部走通（`int()` 接受它们），于是：
#   · `pack_info` 全绿、`param_audit` 一条 note 都不产生（0 这个键**在**表里）
#   · 提示词里逐字写「0 个要点」，却同时要求「每要点约 190 字」
#     （`prompts.py` 的 quota_body_per = body // max(points, 1)）
#   · `checker` 的 n_points 从**实际段落数**出来 → 两本账对不上，
#     模型多半不产出 point 段 → 段落超配额与字数判定双双失真
# 而包在设置页与生成入口都是全绿的。这正是本项目最忌讳的静默降级。
def test_points_by_duration_zero_is_rejected(tmp_path):
    from app.knowledge import pack_info
    pack_dir = _pack(tmp_path, lambda d: d.update(points_by_duration={60: 0}))
    err = pack_info(pack_dir).pack_error
    assert err, "points_by_duration 配 0 没有被判死（包在设置页与生成入口全绿）"
    assert "points_by_duration" in err, err
    assert "正整数" in err, err


def test_points_limit_clamps_non_positive_directly():
    """兜底那层：`points_limit` 自己也要挡住非正数。

    `pack_shape_error` 只在包**走 pack_info 加载路径**时判死值域，而
    `Pack` 也可以被 packgen / 导入流程单独构造，那时绕过了那道。
    用 `object.__new__` 造一个只带 `data` 的实例：这里要量的就是
    「取值那一刻的兜底」，与包怎么加载进来的无关。
    """
    from app.knowledge import Pack
    for table, duration, want in (({60: 0}, 60, 3), ({60: -2}, 60, 3),
                                   ({"60": 0}, 60, 3), ({60: "x"}, 60, 3),
                                   ({60: 4}, 60, 4), ({30: 2}, 60, 3),
                                   ({}, 60, 3)):
        p = object.__new__(Pack)
        p.data = {"points_by_duration": table}
        got = p.points_limit(duration)
        assert got == want, f"{table} @ {duration}s → {got}，期望 {want}"


def test_points_by_duration_negative_and_non_int_are_rejected(tmp_path):
    from app.knowledge import pack_info
    for bad in (-1, 2.5, "3", True, None):
        pack_dir = _pack(tmp_path, lambda d: d.update(points_by_duration={60: bad}))
        err = pack_info(pack_dir).pack_error
        assert err, f"{bad!r} 没被拦"
        assert "points_by_duration" in err, err


def test_points_by_duration_whole_table_must_be_mapping(tmp_path):
    from app.knowledge import pack_info
    pack_dir = _pack(tmp_path, lambda d: d.update(points_by_duration=[3, 4]))
    err = pack_info(pack_dir).pack_error
    assert err, "整个表写成列表没被拦（取值时会 str() 成不存在的键）"
    assert "映射" in err, err


def test_points_by_duration_valid_values_pass(tmp_path):
    """反向对照：合法值不许被拦（否则这条守卫会把好包也判死）。"""
    from app.knowledge import pack_info
    for good in ({60: 3}, {30: 2, 60: 3, 90: 4}, {}):
        pack_dir = _pack(tmp_path, lambda d: d.update(points_by_duration=good))
        err = pack_info(pack_dir).pack_error
        assert not err, f"{good} 被误判：{err}"


def test_flags_missing_tables(tmp_path):
    """五类缺口一次全开，各自都要被标出、且说明要指到具体原因。"""
    def mutate(d):
        d["params"]["platform"]["options"] = ["抖音", "快手"]
        d["params"]["segment"]["options"] = ["维保", "新领域X"]
        d["params"]["style"]["options"] = ["亲和", "rap风"]
        d["params"]["duration"]["options"] = [60, 45]
        d["params"]["audience"]["options"] = ["业主", "外星人"]

    pack_dir = _pack(tmp_path, mutate)
    audit = param_audit(pack_dir, yaml.safe_load(
        (pack_dir / "pack.yaml").read_text(encoding="utf-8")))

    assert audit["platform"]["快手"].startswith("平台分级词表未定义")
    # P2-4：越界取值**不再退回「通用」章节 / 整份文件**，所以说明也不能再说谎。
    assert "不注入" in audit["segment"]["新领域X"]
    assert "通用" not in audit["segment"]["新领域X"]
    assert "rate_by_style" in audit["style"]["rap风"]
    assert "插值" in audit["duration"]["45"] and "默认 3" in audit["duration"]["45"]
    assert "audience_map" in audit["audience"]["外星人"]
    # 有定制的值不能被牵连进来
    for key, value in [("platform", "抖音"), ("segment", "维保"),
                       ("style", "亲和"), ("duration", "60"), ("audience", "业主")]:
        assert value not in audit.get(key, {}), f"{key}={value} 被误报"


def test_persona_is_never_audited(tmp_path):
    """人设只进提示词，没有表可查 —— 不该出现在审计结果里。"""
    def mutate(d):
        d["params"]["persona"]["options"] = ["老师傅", "随便一个新人设"]

    pack_dir = _pack(tmp_path, mutate)
    audit = param_audit(pack_dir, yaml.safe_load(
        (pack_dir / "pack.yaml").read_text(encoding="utf-8")))
    assert "persona" not in audit


def test_flags_mapped_but_missing_heading(tmp_path):
    """映射写了、章节却不存在 —— 切片为空（**不退回整份**），但必须报出来。

    这条容易漏：配置看起来是「配了」的，实际知识切片根本没生效。
    """
    def mutate(d):
        d["params"]["segment"]["options"] = ["维保", "没写章节的领域"]
        d["topics_map"]["没写章节的领域"] = "这个标题不存在"

    pack_dir = _pack(tmp_path, mutate)
    audit = param_audit(pack_dir, yaml.safe_load(
        (pack_dir / "pack.yaml").read_text(encoding="utf-8")))
    msg = audit["segment"]["没写章节的领域"]
    assert "不存在" in msg
    # 说明必须与真实行为一致：曾经写"将注入整份文件"，而代码早已改成不注入
    assert "整份" in msg and "不注入" in msg, msg


def test_pack_info_exposes_audit(tmp_path):
    """审计结果必须真的随 /api/meta 下发 —— 否则前端拿不到，界面上标不出来。"""
    def mutate(d):
        d["params"]["platform"]["options"] = ["抖音", "快手"]

    pack_dir = _pack(tmp_path, mutate)
    info = pack_info(pack_dir)
    assert "快手" in info.param_audit["platform"]
    # Pydantic 模型也要带得出去
    assert "快手" in info.model_dump()["param_audit"]["platform"]
