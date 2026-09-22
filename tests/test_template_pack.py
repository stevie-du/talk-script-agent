# -*- coding: utf-8 -*-
"""模板包 `packs/_template/` 与建包产物的一致性（P0：新包不得带别行业的事实）。

跑法：pytest tests/test_template_pack.py

缺陷原型（2026-09 复测）
----------------------
`packgen.TEMPLATE_PACK = "_template"` 后面跟着一条静默回退：目录不存在就用
`FALLBACK_TEMPLATE_PACK = "elevator"`。而 `GENERIC_FILES` 会**原样复制**
`patterns/{hooks,growth}.md` 与 `knowledge/voice.md`（growth 的标题就写着是某个
行业"特有的取舍"），`PRIVATE_TEMPLATES` 会复制填好该行业事实的 `private/faq.yaml`
—— 这四份经 `$hooks` / `$growth` / `$voice_block` / `$facts_block` **每轮注入**。
实测给「宠物医院」建的包：撰写提示词 6096 字里该行业的装置名出现 11 次、
注入的"私有知识库"1673 字全是它的话术。

本文件钉三件事：
1. 模板包**存在**且**一个行业都不属于**（逐文件 grep 行业专属词）；
2. 模板缺失时 `template_pack()` **抛错**，不再回退到任何行业包；
3. 用桩模型真建一个「宠物医院」包：产物文件 + 渲染出的 select/write 提示词里
   模板来源行业的专属词计数为 0，且 `facts_block` 为空（空白私有模板不被当事实注入）。
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import packgen                                              # noqa: E402
from app.knowledge import Pack, list_packs                           # noqa: E402
from app.packgen import GENERIC_FILES, PRIVATE_TEMPLATES             # noqa: E402
from app.packgen import TemplateMissingError, create_pack            # noqa: E402
from app.prompts import PromptRenderer                               # noqa: E402

TEMPLATE_DIR = ROOT / "packs" / "_template"

# 一个具体行业的专属词（取自模板来源行业）。模板包与建包产物里出现即为泄漏。
# 不含"宠物"这类词 —— 建包测试给的目标行业就是宠物医院，它自己的词当然会出现。
LEAK_TOKENS = [
    "电梯", "曳引", "困人", "维保", "抱闸", "轿厢", "井道", "扶梯", "加装",
    "救援", "限速器", "安全钳", "光幕", "制动器", "对重", "96333",
]
# 模板包会被复制进**所有**行业包，因此按更多行业各验一遍
EXTRA_TOKENS = ["宠物", "板材", "全屋定制", "甲醛", "疫苗", "驱虫"]


def _hits(text: str, tokens: list[str]) -> dict[str, int]:
    return {t: text.count(t) for t in tokens if t in text}


# ── 1. 模板包存在且行业中立 ───────────────────────────────────
def test_template_pack_exists_and_supplies_every_skeleton_file():
    assert (TEMPLATE_DIR / "pack.yaml").exists(), (
        "packs/_template 不存在：建包会直接抛 TemplateMissingError（不再回退行业包）")
    missing = [rel for rel in GENERIC_FILES + PRIVATE_TEMPLATES
               if not (TEMPLATE_DIR / rel).exists()]
    assert not missing, f"模板包缺少会被复制进新包的骨架文件：{missing}"


def test_template_pack_contains_no_industry_facts():
    """模板包**整目录**逐文件 grep：一条行业专属词都不许有（含注释与 README）。"""
    tokens = LEAK_TOKENS + EXTRA_TOKENS
    problems = []
    for f in sorted(TEMPLATE_DIR.rglob("*")):
        if not f.is_file():
            continue
        hits = _hits(f.read_text(encoding="utf-8"), tokens)
        if hits:
            problems.append(f"{f.relative_to(TEMPLATE_DIR)}: {hits}")
    assert not problems, ("模板包里混进了行业事实，会被复制进每一个新包：\n  "
                          + "\n  ".join(problems))


def test_template_loads_and_injects_nothing_as_private_facts():
    """模板包能被 `Pack` 加载，且空白 private 模板的注入量必须是 0 字。

    `private_facts()` 非空 = 新包第一次生成就把模板作者的示例当成
    「私有知识库（优先作为事实来源）」引用。
    """
    pk = Pack(ROOT, packgen.TEMPLATE_PACK)
    assert pk.private_facts() == "", "模板包的 private/*.yaml 还带着非空的示例内容"
    assert pk.banwords_data().get("hard"), "模板包词表为空：新包会失去通用广告法拦截"


def test_list_packs_skips_the_template_directory():
    """下划线目录是引擎内部骨架，不能出现在行业包列表里（参数全是占位符）。"""
    names = [i.name for i in list_packs(ROOT)]
    assert packgen.TEMPLATE_PACK not in names
    assert "elevator" in names


# ── 2. 模板缺失必须抛，不许回退 ────────────────────────────────
def test_missing_template_raises_instead_of_falling_back(tmp_path: Path):
    (tmp_path / "packs").mkdir(parents=True)
    with pytest.raises(TemplateMissingError) as e:
        packgen.template_pack(tmp_path)
    assert "_template" in str(e.value)
    # 旧实现的回退路径已经不存在：别再让它悄悄用某个行业包当模板
    assert not hasattr(packgen, "FALLBACK_TEMPLATE_PACK")


def test_incomplete_template_lists_the_missing_file(tmp_path: Path):
    shutil.copytree(TEMPLATE_DIR, tmp_path / "packs" / "_template")
    (tmp_path / "packs" / "_template" / "patterns" / "growth.md").unlink()
    with pytest.raises(TemplateMissingError) as e:
        packgen.template_pack(tmp_path)
    assert "patterns/growth.md" in str(e.value)


# ── 3. 真建一个「宠物医院」包：产物零别行业事实 ─────────────────
_PET = {
    "display_name": "宠物医院",
    "segments": ["疫苗驱虫", "内科门诊", "外科手术", "急诊重症", "老年照护"],
    "audiences": ["养宠新手", "慢性病随访家庭", "老年宠物家庭"],
    "personas": ["临床兽医", "护士长", "创院院长"],
    "topics": [
        {"heading": "疫苗驱虫", "core": "首免程序与加强针的间隔取决于母源抗体。",
         "myths": [{"myth": "不出门就不用驱虫", "fact": "寄生虫卵可经鞋底与衣物带入室内"}],
         "placeholders": ["本院常用疫苗品牌与批号"]},
        {"heading": "急诊重症", "core": "急诊分诊按生命体征稳定度排序，不按到达先后。",
         "myths": [{"myth": "呕吐就必须禁食禁水", "fact": "禁食时长须由检查结果决定"}],
         "placeholders": ["急诊响应时限"]},
    ],
    "audience_details": [
        {"name": "养宠新手", "fears": ["第一针打晚了", "被过度检查"],
         "questions": ["疫苗要打几针", "驱虫多久一次"], "cta": "评论关键词领时间表"},
    ],
    "ideas": ["幼犬第一针什么时候打 — 养宠新手 · 直接痛点",
              "不出门就不用驱虫？这个说法害人不浅 — 养宠新手 · 误解纠正",
              "急诊先到的先看？分诊不是这样 — 慢性病随访家庭 · 反常识"],
    "redlines": ["不承诺治愈率与存活率", "不展示手术血腥画面",
                 "处方药与驱虫药不得引导自行用药"],
    "banwords_extra_hard": ["包治好"],
    "banwords_extra_soft": ["最专业"],
    "verify_list": ["本院执业许可范围与有效期"],
}


class _StubLLM:
    def __init__(self, out):
        self._out = out

    def chat_json(self, task, system, user, model_cls, **kw):
        return model_cls(**self._out)


def _new_pet_pack(tmp_path: Path) -> str:
    (tmp_path / "packs").mkdir(parents=True, exist_ok=True)
    shutil.copytree(TEMPLATE_DIR, tmp_path / "packs" / "_template")
    res = create_pack(tmp_path, _StubLLM(_PET), "宠物医院", "连锁宠物医院，做养宠科普与到诊转化")
    return res["name"]


def test_generated_pack_files_carry_no_other_industry(tmp_path: Path):
    slug = _new_pet_pack(tmp_path)
    d = tmp_path / "packs" / slug
    problems = []
    for f in sorted(d.rglob("*")):
        if f.is_file():
            hits = _hits(f.read_text(encoding="utf-8"), LEAK_TOKENS)
            if hits:
                problems.append(f"{f.relative_to(d)}: {hits}")
    assert not problems, "生成的新行业包里混进了别行业的事实：\n  " + "\n  ".join(problems)


def test_generated_pack_prompts_are_clean_and_slices_resolve(tmp_path: Path):
    slug = _new_pet_pack(tmp_path)
    pk = Pack(tmp_path, slug)
    pr = PromptRenderer(pk)
    rate = pk.rate_for_style(pk.param_default("style"))
    p = {"pack": slug, "topic": "幼猫疫苗怎么打",
         "segment": pk.param_default("segment"), "audience": pk.param_default("audience"),
         "platform": "抖音", "style": pk.param_default("style"),
         "persona": pk.param_default("persona"), "cta": pk.param_default("cta"),
         "facts": "", "rate": rate, "voice": "strong", "format": "both",
         "points": 3, "duration": 60.0,
         "quota": {"total": 261, "hook": 40, "body": 170, "cta": 50}}
    plan = {"angle": "角度", "hook_type": "反常识", "hook_line": "钩子句",
            "points": ["要点一", "要点二"], "cta": "关注"}
    ctx_s, ctx_w = pr.select_ctx(p), pr.write_ctx(p, plan, "")
    _, us = pr.render("select", ctx_s)
    _, uw = pr.render("write", ctx_w)
    for name, text in (("select", us), ("write", uw)):
        hits = _hits(text, LEAK_TOKENS)
        assert not hits, f"{name} 提示词里出现别行业事实：{hits}"

    assert "不承诺治愈率与存活率" in us, "红线没注入选题阶段"
    assert "不出门就不用驱虫" in us, "选题库没注入选题阶段"
    assert ctx_w["redlines"], "撰写阶段的红线切片为空"
    assert ctx_w["terms"], "生成包的 standards.md 缺「核心术语」节：术语不注入"
    assert ctx_w["facts_block"] == "", "空白私有模板被当成事实注入了"
    assert "包治好" in pk.banwords_data().get("hard", []), "行业增补词未并入词表"
    # 前缀缓存：新包同样把回炉块放在末尾
    _, uw2 = pr.render("write", pr.write_ctx(p, plan, "- 时长偏差 +24%"))
    assert uw2.startswith(uw)
    # 引擎体检（P1-44）此时不应报"章节切片为空"
    notes = packgen._pack_audit(tmp_path, slug)
    assert not [n for n in notes if "切片为空" in n], f"生成包有注入不到的知识：{notes}"


def test_generated_pack_idea_lines_use_the_contract_vocabulary(tmp_path: Path):
    """模型产出的选题行仍要落在选题阶段的取值域里（钩子类型 / 受众名）。

    这条不保证模型一定照格式写 —— 它保证**不照格式时测试会红**，
    因为取值域是从模板包自己的两个文件（hooks.md 与 pack.yaml）读出来的。
    """
    from tests.test_pack_injection import _hook_types     # 同一套取值域解析，不写两份

    slug = _new_pet_pack(tmp_path)
    pk = Pack(tmp_path, slug)
    section = pk.file_slice("knowledge/ideas.md#选题库")
    assert section.strip(), "生成包的 ideas.md 没有「## 选题库」这一节 → 选题阶段注入为空"
    valid_aud = {str(a) for a in pk.param_options("audience")}
    hook_types = _hook_types(Pack(ROOT, packgen.TEMPLATE_PACK))
    assert len(hook_types) == 10
    for line in section.splitlines():
        line = line.strip()
        if not line or not line[0].isdigit() or "—" not in line:
            continue
        parts = [x.strip() for x in line.split("—", 1)[1].split("·") if x.strip()]
        assert parts, f"选题行缺钩子类型：{line}"
        assert parts[-1] in hook_types, f"钩子类型不在钩子库 10 类内：{line}"
        if len(parts) > 1:
            assert parts[0] in valid_aud, f"受众不是 pack.yaml 的 audience 选项：{line}"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
