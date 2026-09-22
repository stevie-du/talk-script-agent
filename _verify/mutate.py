# -*- coding: utf-8 -*-
"""变异检验：把本轮新增的实现逐条改回旧行为 → 对应断言必须报红。

判据不是"跑过了"，而是"改回去必须红"。任何一条全绿 = 那条断言是空的。
每条变异跑完立刻还原（finally），文件不会被留在变异态。
"""
import shlex
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
AI = ROOT / "app/ai_tells.py"
PACK = ROOT / "packs/elevator"
YAML_F = PACK / "ai_tells.yaml"

MUTATIONS = [
    (
        "A-1 占位豁免去掉上限（退回「只要有一个 {{}} 就豁免」）",
        AI,
        """        ph = len(_PLACEHOLDER_RE.findall(full))
        if 0 < ph <= max(len(sections), 1) and sum(_count(s.get("text", "")) for s in sections) > 0:
            return []
        if re.search(r"\\d", full) or _MEASURE_RE.search(full):
            return []
        return [TellHit("no_specific", self.sev("no_specific"), max(ph, 1), "全篇",
                        "通篇无一个具体数字/时间/数量" + (f"（{ph} 处占位，超过段数）" if ph else ""))]""",
        """        if re.search(r"\\{\\{[^}]*\\}\\}", full):
            return []
        if re.search(r"\\d|[一二三四五六七八九十百]+\\s*(天|次|台|元|块|米|层|分钟|小时|起|个|位|%|％)", full):
            return []
        return [TellHit("no_specific", self.sev("no_specific"), 1, "全篇",
                        "通篇无一个具体数字/时间/数量")]""",
        "tests/test_ai_tells.py -k placeholder",
    ),
    (
        "A-1 report 去掉 placeholders 字段（退回「整篇没写在链路上隐形」）",
        AI,
        """                "placeholders": {"count": ph,
                                 "per_100": round(ph / spoken * 100, 1) if spoken else None,
                                 "cap": max(len(sections), 1)},
""",
        "",
        "tests/test_ai_tells.py -k placeholder",
    ),
    (
        "A-6#1/#2 取词范围退回全文扫描（LEX_SCOPE 清空）",
        AI,
        'LEX_SCOPE = {"slogan_closing": "cta_only", "opening_ban": "first_sent"}',
        "LEX_SCOPE = {}",
        "tests/test_ai_tells.py -k 'slogan or opening'",
    ),
    (
        "A-6#7 量词退回 11 个白名单（汉字数词写法翻结果）",
        AI,
        '        if re.search(r"\\d", full) or _MEASURE_RE.search(full):',
        '        if re.search(r"\\d|[一二三四五六七八九十百]+\\s*(天|次|台|元|块|米|层|分钟|小时|起|个|位|%|％)", full):',
        "tests/test_ai_tells.py -k measure",
    ),
    # ── A-3 三方对账（需求方案 §2.1 / yaml / 函数名）────────────
    (
        "A-3 yaml 里把 tell 名拼错（slogan_closing → slogan_close）",
        YAML_F,
        "  - slogan_closing       # 口号式收尾出现即问题",
        "  - slogan_close         # 口号式收尾出现即问题",
        "tests/test_aitells_alignment.py",
    ),
    (
        "A-3 yaml 少声明一个 tell（删掉 filler_stack）",
        YAML_F,
        "  - filler_stack\n",
        "",
        "tests/test_aitells_alignment.py",
    ),
    (
        "A-3 lexicon 键拼错（filler_stack → filler_stacks）",
        YAML_F,
        "  filler_stack:\n",
        "  filler_stacks:\n",
        "tests/test_aitells_alignment.py",
    ),
    (
        "A-3 词表类 tell 声明了却一条词都没有（slogan_closing 整段清空）",
        YAML_F,
        """  slogan_closing:
    - 让生活更美好
    - 为你保驾护航
    - 值得拥有
    - 选择没错
    - 品质保证
""",
        "  slogan_closing:\n",
        "tests/test_aitells_alignment.py",
    ),
    (
        "A-3 结构类 tell 被塞了词表（词没人读）",
        YAML_F,
        "lexicon:\n  bookish_connective:",
        "lexicon:\n  no_specific:\n    - 通篇\n  bookish_connective:",
        "tests/test_aitells_alignment.py",
    ),
    (
        "A-3 词表里混进单字（「性」会被 _scan_words 整条丢掉）",
        YAML_F,
        "  abstract_noun_ending:\n",
        "  abstract_noun_ending:\n    - 性\n",
        "tests/test_aitells_alignment.py",
    ),
    (
        "A-3 词表里出现互为子串的词（「其实现」吃掉「其实」）",
        YAML_F,
        "  filler_stack:\n",
        "  filler_stack:\n    - 其实现\n",
        "tests/test_aitells_alignment.py",
    ),
    # ── A-3 误改率门禁本身要可证伪 ──────────────────────────────
    (
        "A-3 排比判据退回「出现即报」（PARALLEL_COVERAGE 0.5 → 0）",
        AI,
        "    PARALLEL_COVERAGE = 0.5",
        "    PARALLEL_COVERAGE = 0.0",
        "tests/test_aitells_false_positive.py -k normal",
    ),
    (
        "A-3 弱 tell 阈值降到 1（WEAK_MIN 2 → 1）",
        AI,
        "WEAK_MIN = 2",
        "WEAK_MIN = 1",
        "tests/test_aitells_false_positive.py -k negative",
    ),
    (
        "A-3 no_specific 永不报（BORDERLINE 的「钉住现状」必须因此报红）",
        AI,
        """        return [TellHit("no_specific", self.sev("no_specific"), max(ph, 1), "全篇",
                        "通篇无一个具体数字/时间/数量" + (f"（{ph} 处占位，超过段数）" if ph else ""))]""",
        "        return []",
        "tests/test_aitells_false_positive.py -k borderline",
    ),
]

bad = 0
for name, path, old, new, selector in MUTATIONS:
    orig = path.read_text(encoding="utf-8")
    if old not in orig:
        print(f"[SKIP] {name} —— 旧串没找到，断言会空转")
        bad += 1
        continue
    path.write_text(orig.replace(old, new, 1), encoding="utf-8")
    try:
        r = subprocess.run([str(ROOT / ".venv/Scripts/python.exe"), "-m", "pytest", "-q", *shlex.split(selector)],
                           capture_output=True, text=True, cwd=ROOT)
        out = (r.stdout or "") + (r.stderr or "")
        # ⚠ 退出码 5 = "一条用例都没收集到"，用法错误也是非零 —— 直接当"报红"就是把
        #   「选择器写错了」误读成「断言守住了」。必须显式区分：只有真出现 FAILED 才算报红。
        #   （本工具第一版就是 `selector.split()`，把 `-k 'a or b'` 拆成了三个参数，
        #    pytest 报用法错误 → 退出码非零 → 假报红。改用 shlex。）
        fails = [ln for ln in out.splitlines() if ln.startswith("FAILED")]
        if fails:
            print(f"[报红 OK] {name}（{len(fails)} 条报红）")
        elif "error" in out.lower() and not fails:
            print(f"[全绿 !! 工具错] {name} —— pytest 没跑成用例，看输出：\n{out[-400:]}")
            bad += 1
        else:
            print(f"[全绿 !! 漏检] {name} —— 选择器选中了用例但全过")
            bad += 1
    finally:
        path.write_text(orig, encoding="utf-8")
sys.exit(1 if bad else 0)
