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
KN_F = ROOT / "app/knowledge.py"
PIPE_F = ROOT / "app/pipeline.py"
INTEL_F = ROOT / "app/intel.py"
JOBS_F = ROOT / "app/jobs.py"
SRV_F = ROOT / "app/server.py"
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
    # ── A-2 改写范围三档 ───────────────────────────────────────
    (
        "A-2 in-place 档不再禁止删句（只剩「句内清理」这句好话）",
        PIPE_F,
        '"**不许删句、加句、并句，也不许调整段落顺序**；"\n',
        '""\n',
        "tests/test_rewrite_scope.py -k three_scopes",
    ),
    (
        "A-2 三档文案只剩一份（_violation_feedback 无视 scope）",
        PIPE_F,
        'lines.append(f"- 上一版全文（{SCOPE_INSTRUCTION[scope]}）：\\n" + body)',
        'lines.append(f"- 上一版全文（{SCOPE_INSTRUCTION[DEFAULT_REWRITE_SCOPE]}）：\\n" + body)',
        "tests/test_rewrite_scope.py -k three_scopes",
    ),
    (
        "A-2 默认档从 bounded 换成 structural",
        KN_F,
        'DEFAULT_REWRITE_SCOPE = "bounded"',
        'DEFAULT_REWRITE_SCOPE = "structural"',
        "tests/test_rewrite_scope.py -k default",
    ),
    (
        "A-2 _normalize 无视请求与包配置（永远落默认档）",
        PIPE_F,
        'scope = str(scope) if scope not in (None, "") else (pack.rewrite_scope() or DEFAULT_REWRITE_SCOPE)',
        "scope = DEFAULT_REWRITE_SCOPE",
        "tests/test_rewrite_scope.py -k precedence",
    ),
    (
        "A-2 请求里的非法档位静默回落（不抛错）",
        PIPE_F,
        """        bad_scope = rewrite_scope_error(scope)
        if bad_scope:
            raise ValueError(bad_scope)""",
        "        scope = None if rewrite_scope_error(scope) else scope",
        "tests/test_rewrite_scope.py -k precedence",
    ),
    (
        "A-2 取值检查放行任意值（rewrite_scope_error 恒为空）",
        KN_F,
        """    if value not in REWRITE_SCOPES:
        return (f"pack.yaml 的 rewrite_scope={value!r} 不是合法档位\"""",
        """    if False:
        return (f"pack.yaml 的 rewrite_scope={value!r} 不是合法档位\"""",
        "tests/test_rewrite_scope.py -k bad_scope",
    ),
    (
        "A-2 pack_info 不再校验 rewrite_scope（坏值静默退回默认）",
        KN_F,
        """        serr = rewrite_scope_error(data.get("rewrite_scope"))
        if serr:
            err = serr""",
        "        pass",
        "tests/test_rewrite_scope.py -k pack_error",
    ),
    (
        "A-2 rewrite_scope 不进落盘白名单（算了但无声丢掉）",
        PIPE_F,
        '    "rewrite_scope",\n',
        "",
        "tests/test_rewrite_scope.py -k persisted",
    ),
    (
        "A-2 SCOPE_INSTRUCTION 漏一档（回炉那一刻 KeyError）",
        PIPE_F,
        """    "structural":
        "**可以重排**""",
        """    "unused_placeholder":
        "**可以重排**""",
        "tests/test_rewrite_scope.py -k every_scope",
    ),
    # ── B 线：情报 ─────────────────────────────────────────────
    (
        "B 去重不认 guid 优先级（只按 url）",
        INTEL_F,
        'key = it.get("guid") or it.get("url") or f"title:{it.get(\'title\')}"',
        'key = it.get("url") or it.get("guid") or f"title:{it.get(\'title\')}"',
        "tests/test_intel.py -k dedup",
    ),
    (
        "B 一个源挂了就整次抓取失败（不隔离）",
        INTEL_F,
        """        try:
            rows = ADAPTERS[spec.id](spec, ctx) or []
        except Exception as e:                     # noqa: BLE001""",
        """        try:
            rows = ADAPTERS[spec.id](spec, ctx) or []
        except ImportError as e:                   # noqa: BLE001""",
        "tests/test_intel.py -k broken_source",
    ),
    (
        "B 每话题上限失效（防刷屏没了）",
        INTEL_F,
        "rows = dedup(rows)[:PER_SOURCE_CAP]",
        "rows = dedup(rows)",
        "tests/test_intel.py -k per_source_cap",
    ),
    (
        "B 落点写回 packs/（打包后安装目录不可写）",
        INTEL_F,
        'return Path(data_dir) / "intel" / pack',
        'return Path(data_dir) / "packs" / pack',
        "tests/test_intel.py -k lands_in_data_dir",
    ),
    (
        "B 读坏文件直接抛（只读端点变 500）",
        INTEL_F,
        """    except Exception as e:                         # noqa: BLE001
        log.warning("情报文件读不出来：%s —— %s", f, e)
        return {**empty, "errors": {"_read": f"latest.json 读不出来：{e}"}}""",
        """    except Exception as e:                         # noqa: BLE001
        raise""",
        "tests/test_intel.py -k load_latest_never_raises",
    ),
    (
        "B 算不出的机会分落成 0（「没数据」被读成「没机会」）",
        INTEL_F,
        '"opportunity": round(D * (1 - S) * 100) if (D is not None and S is not None) else None,',
        '"opportunity": round((D or 0) * (1 - (S or 0)) * 100),',
        "tests/test_intel.py -k scores_are_none_not_zero",
    ),
    (
        "B 事件衰减的 τ 不分角色（热榜旧闻与政策一样新鲜）",
        INTEL_F,
        '''TAU_BY_ROLE: dict[str, float] = {
    "破圈触发器": 3.0, "供给度量": 3.0, "雷达": 14.0,
    "数据源": 30.0, "口径库": 30.0,
}''',
        "TAU_BY_ROLE: dict[str, float] = {}",
        "tests/test_intel.py -k event_decay",
    ),
    (
        "B 解析不出的日期当成「就是今天」",
        INTEL_F,
        """    except ValueError:
            return None
        if t.tzinfo is None:""",
        """    except ValueError:
            return 0.0
        if t.tzinfo is None:""",
        "tests/test_intel.py -k unparseable_published",
    ),
    (
        "B 按需源被算进懒触发（B站天天被抓）",
        INTEL_F,
        'CADENCE_DAYS: dict[str, int] = {"daily": 1, "weekly": 7, "monthly": 30, "quarterly": 90}',
        'CADENCE_DAYS: dict[str, int] = {"daily": 1, "weekly": 7, "monthly": 30, '
        '"quarterly": 90, "on_demand": 1}',
        "tests/test_intel.py -k is_stale",
    ),
    (
        "B 忽略记录只存布尔（算不出「连续几天」）",
        INTEL_F,
        'rec[str(key)] = today or datetime.now().strftime("%Y-%m-%d")',
        'rec[str(key)] = "1"',
        "tests/test_intel.py -k ignore_only_affects_today",
    ),
    (
        "B 主动关掉的源也被记成问题（审计变噪音）",
        INTEL_F,
        'if sid not in ADAPTERS and item.get("enabled", True) is not False:',
        'if sid not in ADAPTERS:',
        "tests/test_intel.py -k unknown_adapter",
    ),
    (
        "B 两族额度配错（INTEL_BUSY_STATES 写成 BUSY_STATES）",
        JOBS_F,
        'INTEL_BUSY_STATES = frozenset({"fetching"})',
        "INTEL_BUSY_STATES = BUSY_STATES",
        "tests/test_intel.py -k does_not_consume_model_quota",
    ),
    (
        "B add_if_room 无视 states 参数（两族额度合成一族）",
        JOBS_F,
        "                   if j.state in states and not j.stranded) >= limit:",
        "                   if j.state in BUSY_STATES and not j.stranded) >= limit:",
        "tests/test_intel.py -k its_own_limit",
    ),
    (
        "B 坏包让选题页白屏（改用 Pack() 而不是 pack_info）",
        SRV_F,
        "        info = next((p for p in list_packs(root_for_packs) if p.name == name), None)",
        "        from .knowledge import Pack as _P\n"
        "        _P(root_for_packs, name)\n"
        "        info = next((p for p in list_packs(root_for_packs) if p.name == name), None)",
        "tests/test_intel.py -k survives_a_broken_pack",
    ),
    (
        "B 盘上有、声明里没有的源被藏掉（分组计数与总数对不上）",
        INTEL_F,
        """    for label, rows in by_source.items():
        if label not in known:""",
        """    for label, rows in by_source.items():
        if False:""",
        "tests/test_intel.py -k orphan_sources",
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
