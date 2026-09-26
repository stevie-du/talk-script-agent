# -*- coding: utf-8 -*-
"""批次 10 · 包层审计复验（主报告 §15）。

钉的是同一族缺陷的四条路径 —— **「静默换成别的内容」与「静默什么都不注入」**：

  P1-1 / P1-2 「选项 → 章节」的匹配按列表顺序取第一个包含关系，
        `segments=["维保合同","维保"]` 时两个选项拿到同一份知识，另一份永远不可达。
  P1-3 / P2-7 切片为空只有**建包期**看得见（运行期提示词里留一个空段照样 done）。
  P2-4  segment 越界时 `topics_slice` 退回**整份** topics.md（含 pack.yaml 字样）。
  P2-5  ai_tells.yaml 结构坏了会在**已经付过选题的钱之后**把整条作业打死。
  P2-6  平台 `promote/demote` 落空的条目无声消失（`extra_soft` 却有提醒）。
  P3-13 pack.yaml 里选项写重复 → 生成两份同名章节，第二份永远解析不到。

修法一致：宁缺勿错 —— 匹配不确定就当没匹配上（留空骨架 + 把竞争者写进校对清单），
所有降级都要在 `param_audit` / `Pack.warnings` 里说一句人话。
"""
from __future__ import annotations

import shutil
import sys
import types
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.checker import Banwords, validate_banwords               # noqa: E402
from app.knowledge import (Pack, PackError, name_matches,          # noqa: E402
                           param_audit)
from app.packgen import PackGenOut, _match_by_name, create_pack    # noqa: E402


# ── P1-1 / P1-2：匹配规则本体 ─────────────────────────────────
def test_exact_match_beats_a_heading_thatmerely_contains_it():
    """「维保」不能因为「维保合同」含这三个字就拿走合同的知识。"""
    hit, rivals = name_matches(["维保合同", "维保"], "维保")
    assert hit == "维保" and rivals == []
    hit, _ = name_matches(["维保", "维保合同"], "维保")      # 与列表顺序无关
    assert hit == "维保"


def test_containment_takes_the_closest_heading_not_the_first():
    # 「旧梯改造」：`改造`(差 2) 比 `旧梯更新改造`(差 2) 并列吗？不 ——
    # 对称差按字数算：改造=2 字、旧梯更新改造=6 字、选项 4 字 → |4-6|=2、|4-2|=2 并列，
    # 所以这一对**必须都不给**（宁缺不滥），下面单独立一条测这件事。
    hit, rivals = name_matches(["别墅电梯加装"], "别墅电梯")
    assert hit == "别墅电梯加装" and rivals == []


def test_two_equally_close_candidates_match_neither():
    """两个候选与选项的字数差相同 → 谁都不给，并把竞争者交出去。"""
    hit, rivals = name_matches(["维保合同", "维保责任"], "维保")
    assert hit is None and set(rivals) == {"维保合同", "维保责任"}
    # 同名标题出现两次是包自己写坏了：谁也不给
    hit2, rivals2 = name_matches(["业主", "业主"], "业主")
    assert hit2 is None and len(rivals2) == 2


def test_containment_only_reaches_a_heading_that_actually_contains_it():
    """`旧梯改造` 在 `旧梯更新改造` 里**不是连续的**，所以它不是候选 ——
    唯一的候选才给（这条测的是"别把对称差当成模糊匹配"）。"""
    hit, rivals = name_matches(["旧梯更新改造", "改造"], "旧梯改造")
    assert hit == "改造" and rivals == []


def test_normalisation_ignores_numbering_and_spaces():
    hit, _ = name_matches(["1. 别墅电梯", "2. 加装"], "别墅电梯")
    assert hit == "1. 别墅电梯"
    assert name_matches(["别 墅 电梯"], "别墅电梯")[0] == "别 墅 电梯"
    assert name_matches(["别墅电梯"], "  别墅电梯  ")[0] == "别墅电梯"


def test_unmatched_option_leaves_an_account_not_a_silence():
    """`_match_by_name` 的两类落空都要有账：被近名挤掉 / 模型压根没给。"""
    out = PackGenOut.model_validate({
        "display_name": "维保", "segments": ["维保", "维保合同"],
        "audiences": ["业主"], "personas": ["顾问"],
        "topics": [{"heading": "维保合同详解", "core": "合同那一节的坑"},
                   {"heading": "维保责任清单", "core": "责任怎么划"}],
        "audience_details": [], "ideas": [], "redlines": [], "verify_list": []})
    used: set = set()
    notes: list[str] = []
    got = {}
    for seg in out.segments:
        got[seg] = _match_by_name(out.topics, used, seg, "heading",
                                  notes=notes, kind="细分领域")
    # 合同那条唯一命中；裸「维保」与两个标题同样近 → 不给，且必须留下竞争者名单
    assert got["维保"] is None and got["维保合同"] is not None
    assert len(notes) == 1 and "维保合同详解" in notes[0] and "维保责任清单" in notes[0], notes

    used2, notes2 = set(), []
    assert _match_by_name(out.topics, used2, "根本不存在的细分", "heading",
                          notes=notes2, kind="细分领域") is None
    assert notes2 and "没有对应段落" in notes2[0], notes2


def test_ambiguous_headings_do_not_cross_contaminate_the_written_pack(tmp_path):
    """端到端：模型标题与选项对不上时，新包不许把 A 的内容写进 B 的章节。

    修复前按列表顺序取第一个包含关系 → 「维保」拿走「维保合同详解」，
    `## 2. 维保合同` 只剩空骨架，而包里一切"看起来正常"。
    """
    shutil.copytree(ROOT / "packs", tmp_path / "packs")
    out = {
        "display_name": "电梯维保", "segments": ["维保", "维保合同"],
        "audiences": ["业主", "业主代表"], "personas": ["顾问", "工程师"],
        "topics": [{"heading": "维保合同详解", "core": "合同里那些坑"},
                   {"heading": "维保责任清单", "core": "责任怎么划"}],
        "audience_details": [{"name": "业主代表", "fears": ["怕被业主质疑"],
                              "questions": ["怎么选"], "cta": "私信"}],
        "ideas": ["怎么选 — 业主 · 悬念提问"], "redlines": ["不许承诺工期"],
        "verify_list": ["当地维保指导价"]}

    class FakeLLM:
        mock = False
        cfg = types.SimpleNamespace(model="fake", max_tokens=1000, temperature=0.7,
                                    base_url="http://fake/v1", api_key="k",
                                    retries=0, timeout=30.0)

        def chat_json(self, task, system, user, model_cls, **kw):
            return model_cls.model_validate(out)

    info = create_pack(tmp_path, FakeLLM(), "电梯维保", "面向小区物业的电梯维保与合同科普")
    slug = info["name"]
    topics = (tmp_path / "packs" / slug / "knowledge" / "topics.md").read_text(encoding="utf-8")
    checklist = (tmp_path / "packs" / slug / "校对清单.md").read_text(encoding="utf-8")

    body_维保 = topics.split("## 1. 维保")[1].split("## 2.")[0]
    assert "## 2. 维保合同" in topics, topics[:400]
    # 歧义的那格必须是空的（待补），不许拿合同的内容冒充维保
    assert "待补" in body_维保, f"歧义选项拿到了内容：{body_维保[:120]}"
    assert "合同里那些坑" not in body_维保
    assert "合同里那些坑" in topics.split("## 2. 维保合同")[1]
    # 落空的账要出现在校对清单里，否则用户以为这个包是满的
    assert "无法判定该给谁" in checklist, checklist[-900:]
    assert "维保责任清单" in checklist and "维保合同详解" in checklist


# ── P1-3 / P2-4 / P2-7：运行期的空切片必须说话 ─────────────────
def _pack_with(tmp_path, pack_yaml: dict, files: dict[str, str]) -> Path:
    d = tmp_path / "packs" / "probe"
    d.mkdir(parents=True, exist_ok=True)
    for rel, text in files.items():
        (d / rel).parent.mkdir(parents=True, exist_ok=True)
        (d / rel).write_text(text, encoding="utf-8")
    (d / "pack.yaml").write_text(yaml.safe_dump(pack_yaml, allow_unicode=True,
                                                 sort_keys=False), encoding="utf-8")
    return d


def _base_pack_yaml(**over) -> dict:
    data = {"name": "probe", "display_name": "探针",
            "params": {"segment": {"label": "细分", "options": ["维保", "维保合同"],
                                   "default": "维保"},
                       "audience": {"label": "受众", "options": ["业主"], "default": "业主"},
                       "duration": {"label": "时长", "options": [60], "default": 60},
                       "style": {"label": "风格", "options": ["权威科普"], "default": "权威科普"}},
            "topics_map": {"维保": "维保", "维保合同": "维保合同"},
            "audience_map": {"业主": "业主"},
            "quota_table": {60: {"hook": 20, "body": 200, "cta": 20}},
            "points_by_duration": {60: 3}}
    data.update(over)
    return data


def test_segment_without_a_map_entry_is_reported_and_injects_nothing(tmp_path):
    d = _pack_with(tmp_path, _base_pack_yaml(topics_map={"维保": "维保"}), {
        "knowledge/topics.md": "# 库\n\n## 维保\n\n周期与报价。\n\n## 维保合同\n\n合同里的坑。\n"})
    audit = param_audit(d, yaml.safe_load((d / "pack.yaml").read_text(encoding="utf-8")))
    assert "维保合同" in audit.get("segment", {}), audit
    assert "不注入" in audit["segment"]["维保合同"]
    p = Pack(tmp_path, "probe")
    assert p.topics_slice("维保合同") == ""          # 不退回整份
    assert "合同里的坑" not in p.topics_slice("不存在的细分")
    assert "pack.yaml" not in p.topics_slice("不存在的细分")   # 整份文件绝不外泄


def test_heading_present_but_empty_counts_as_missing(tmp_path):
    """P2-7：只有标题、正文删空 —— 对注入来说与"没有这一节"是同一件事。"""
    d = _pack_with(tmp_path, _base_pack_yaml(), {
        "knowledge/topics.md": "# 库\n\n## 维保\n\n   \n\n## 维保合同\n\n合同里的坑。\n",
        "knowledge/audience.md": "# 受众\n\n## 业主\n\n怕被质疑。\n"})
    data = yaml.safe_load((d / "pack.yaml").read_text(encoding="utf-8"))
    audit = param_audit(d, data)
    assert "维保" in audit.get("segment", {}), f"空章节被判成健康：{audit}"
    assert "正文为空" in audit["segment"]["维保"]
    assert Pack(tmp_path, "probe").topics_slice("维保") == ""


# ── P3-13：重复选项 ────────────────────────────────────────────
def test_duplicate_options_are_refused_at_load(tmp_path):
    """选项名是参数的取值域，写重复就是包自己坏了 —— 判死，不静默生成第二份不可达章节。"""
    base_params = _base_pack_yaml()["params"]
    d = _pack_with(tmp_path, _base_pack_yaml(
        params=base_params | {"segment": {"label": "细分",
                                          "options": ["维保", "维保", "维保合同"],
                                          "default": "维保"}}), {
        "knowledge/topics.md": "# 库\n\n## 维保\n\n周期。\n\n## 维保合同\n\n坑。\n"})
    with pytest.raises(PackError) as ei:
        Pack(tmp_path, "probe")
    assert "重复" in str(ei.value), str(ei.value)
    assert d.exists()


# ── P2-5：坏掉的 ai_tells 只关那一格 ──────────────────────────
def test_broken_ai_tells_degrades_with_a_warning(tmp_path):
    d = _pack_with(tmp_path, _base_pack_yaml(), {
        "knowledge/topics.md": "# 库\n\n## 维保\n\n周期。\n\n## 维保合同\n\n坑。\n",
        "knowledge/audience.md": "# 受众\n\n## 业主\n\n怕。\n",
        "ai_tells.yaml": "strong: [[坏了, 的结构]]\nlexicon: [a\n"})
    p = Pack(tmp_path, "probe")
    assert p.ai_tells_data() in (None, {}), "结构坏掉的词表不该被当成可用"
    data = yaml.safe_load((d / "pack.yaml").read_text(encoding="utf-8"))
    notes = param_audit(d, data)
    assert any("人味" in n or "ai_tells" in n for grp in notes.values() for n in grp.values()), \
        f"关闭了人味提示词却没人说：{notes}"


def test_banwords_structure_errors_stay_fatal(tmp_path):
    """与 ai_tells 相反的取向要保住：词表不可信时必须抛，不能只警告。"""
    with pytest.raises(ValueError):
        Banwords({"hard": "政府补贴"})       # 标量会被拆成单字 → 命中归零
    _errs, advisory = validate_banwords({"hard": ["绝对安全"], "soft": ["最安全"],
                                        "platform": {"抖音": {"promote": ["最安全"]}}})
    assert advisory == [], f"不该有 advisory：{advisory}"


# ── P2-6：promote/demote 落空要有声音（与 extra_soft 同口径）──
def test_promote_entry_that_matches_nothing_is_advised():
    ban = Banwords({"hard": ["绝对安全"], "soft": ["最安全"],
                    "platform": {"小红书": {"promote": ["一个没在表里的词"],
                                            "demote": ["也没在表里的词"]}}})
    ban.scan("正文内容", "小红书")
    assert any("promote" in e or "一个没在表里的词" in e for e in ban.errors), ban.errors
    assert any("demote" in e or "也没在表里的词" in e for e in ban.errors), ban.errors
    # 反向：真正生效的 promote 不该被报成落空
    ok = Banwords({"hard": ["绝对安全"], "soft": ["最安全"],
                   "platform": {"小红书": {"promote": ["最安全"]}}})
    ok.scan("正文内容", "小红书")
    assert not [e for e in ok.errors if "promote" in e], ok.errors


def test_param_audit_stays_clean_for_the_elevator_pack():
    """反向守卫：真包不许有噪声 —— 上面那些新报警如果误伤健康包，就是假红。"""
    p = Pack(ROOT, "elevator")
    audit = param_audit(ROOT / "packs" / "elevator", p.data)
    assert audit == {}, f"电梯包被新增的降级检查点到了：{audit}"
    assert not p.warnings, p.warnings


def test_model_cannot_inject_a_heading_into_the_pack_display_name(tmp_path):
    """display_name 会被写进 pack.yaml / 校对清单 / 导出的 SKILL.md 正文。

    模型在那里换行就能凭空造出一节 `---` + `# 标题`（批次 10 复核实测），
    而行业显示名本来就是一行文字的事 —— 落地前先折行。
    """
    shutil.copytree(ROOT / "packs", tmp_path / "packs")
    out = {
        "display_name": "宠物医院\n---\nname: evil\n# 恶意标题",
        "segments": ["内科", "外科"], "audiences": ["养宠新人", "多宠家庭"],
        "personas": ["医生", "助理"],
        "topics": [{"heading": "内科", "core": "C1"}, {"heading": "外科", "core": "C2"}],
        "audience_details": [{"name": "养宠新人", "fears": ["怕贵"], "questions": ["多久"], "cta": "关注"},
                             {"name": "多宠家庭", "fears": ["怕打架"], "questions": ["怎么"], "cta": "私信"}],
        "ideas": ["怎么选 — 养宠新人 · 悬念提问"], "redlines": ["不承诺治愈"],
        "verify_list": ["当地诊疗收费备案"]}

    class FakeLLM:
        mock = False
        cfg = types.SimpleNamespace(model="fake", max_tokens=1000, temperature=0.7,
                                    base_url="http://fake/v1", api_key="k",
                                    retries=0, timeout=30.0)

        def chat_json(self, task, system, user, model_cls, **kw):
            return model_cls.model_validate(out)

    info = create_pack(tmp_path, FakeLLM(), "宠物医院", "社区宠物医院，做日常科普与引流")
    slug = info["name"]
    dn = str(yaml.safe_load(
        (tmp_path / "packs" / slug / "pack.yaml").read_text(encoding="utf-8"))["display_name"])
    assert "\n" not in dn and dn.strip(), dn      # 一行说完：换行才是注入的入口
    checklist = (tmp_path / "packs" / slug / "校对清单.md").read_text(encoding="utf-8")
    lines = [ln.strip() for ln in checklist.split("\n")]
    # 注入的形态是"另起一行写 `---` / `# 标题` / `name:`"，检查的是**行首**
    assert "---" not in lines, "清单里凭空多了一道 fence"
    assert not [ln for ln in lines if ln.startswith("# ") and "恶意" in ln], lines
    assert not [ln for ln in lines if ln.startswith("name:")], lines


if __name__ == "__main__":                       # pragma: no cover
    import pytest as _p
    sys.exit(_p.main([__file__, "-q"]))
