# -*- coding: utf-8 -*-
"""变异检验：把本轮新增的实现逐条改回旧行为 → 对应断言必须报红。

判据不是"跑过了"，而是"改回去必须红"。任何一条全绿 = 那条断言是空的。
每条变异跑完立刻还原（finally），文件不会被留在变异态。
"""
import json
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
AI = ROOT / "app/ai_tells.py"
CHECK_F = ROOT / "app/checker.py"
KN_F = ROOT / "app/knowledge.py"
PIPE_F = ROOT / "app/pipeline.py"
INTEL_F = ROOT / "app/intel.py"
JOBS_F = ROOT / "app/jobs.py"
SRV_F = ROOT / "app/server.py"
MUTATE_F = ROOT / "_verify/mutate.py"
PACKGEN_F = ROOT / "app/packgen.py"
PKIMP_F = ROOT / "app/packimport.py"
PACKSEED_F = ROOT / "app/packseed.py"
CONFIG_F = ROOT / "app/config.py"
PACK = ROOT / "packs/elevator"
YAML_F = PACK / "ai_tells.yaml"
TOPICS_F = ROOT / "desktop/renderer/js/topics.js"
HEAD_F = ROOT / "desktop/renderer/js/head.js"
SESS_F = ROOT / "desktop/renderer/js/sessions.js"
RESULT_F = ROOT / "desktop/renderer/js/result.js"
UTIL_F = ROOT / "desktop/renderer/js/util.js"
SETTINGS_F = ROOT / "desktop/renderer/js/settings.js"
JOBSJS_F = ROOT / "desktop/renderer/js/jobs.js"
STORE_F = ROOT / "app/store.py"
ABOUT_F = ROOT / "desktop/renderer/js/about.js"
OVER_F = ROOT / "desktop/renderer/js/overlays.js"
WORDS_F = ROOT / "desktop/renderer/js/words.js"
HTML_F = ROOT / "desktop/renderer/index.html"
UI_F = ROOT / "desktop/renderer/js/ui.js"
PROMPTS_F = ROOT / "app/prompts.py"
CSS_F = ROOT / "desktop/renderer/styles.css"
VERIFY_F = ROOT / "_verify/verify.js"
SKILL_F = PACK / "skill.yaml"
TPL_SKILL_F = ROOT / "packs/_template/skill.yaml"
LLM_F = ROOT / "app/llm.py"
SEED_F = ROOT / "app/packseed.py"
IMPORT_F = ROOT / "app/packimport.py"
SCHEMAS_F = ROOT / "app/schemas.py"


def _find_node() -> str:
    """node 可执行文件：**不写死版本号**。

    五路审查 P2 报过"NODE 写死某用户 workbuddy 路径，换机器即崩"，
    2026-09-23 当场发作：环境把 node 从 22.22.2-**2** 升到 **-3**，
    写死的旧路径让 mutate 跑到第 39 条（第一条 verify: 变异）才
    FileNotFoundError —— 前 38 条 pytest 变异全绿，反而更危险
    （看起来像跑完了）。解析顺序：PATH → versions 目录里最新的。
    """
    import shutil
    found = shutil.which("node") or shutil.which("node.exe")
    if found:
        return found
    cands = sorted(Path.home().glob(
        ".workbuddy-ai/binaries/node/versions/*/node.exe"))
    if cands:
        return str(cands[-1])
    raise SystemExit("找不到 node 可执行文件：PATH 与 versions 目录都没有")


NODE = _find_node()

MUTATIONS = [
    # ── s9 组（2026-09-26 修复轮）：每条对应 审查报告-20260926 §3/§4 的一个修复 ──
    (
        "s9-1 P1-1 删模型的悬空判据去掉 `active and`（空 active 又被当成悬空）",
        SRV_F,
        'if active == body.id or (active and active not in {x["id"] for x in left}):',
        'if active == body.id or active not in {x["id"] for x in left}:',
        "tests/test_models_list.py -k all_deactivated",
    ),
    (
        "s9-2 P1-3 read_text_cached 把瞬时 OSError 重新钉进缓存",
        KN_F,
        'log.warning("知识文件读不出来（%s）：%s —— 本次按空内容处理；瞬时故障，"\n'
        '                    "下次读取会重试", path, e)\n'
        '        return ""',
        'log.warning("知识文件读不出来（%s）：%s —— 本次按空内容处理", path, e)\n'
        '        text = ""',
        "tests/test_transient_read_fault.py -k transient_oserror",
    ),
    (
        "s9-3 P1-4 情报落点键退回 pack.name（yaml 的 name 键，不是目录 slug）",
        PIPE_F,
        "                pack.dir.name, self.data_dir, sources,",
        "                pack.name, self.data_dir, sources,",
        "tests/test_intel.py -k slug_not_the_yaml_name",
    ),
    (
        "s9-4 P1-5 packseed 换入前不再把旧包挪进备份（回到先删后拷的暴露窗口）",
        SEED_F,
        "        dst.rename(backup)                    # 此刻起 dst 缺位；失败则 dst 原样",
        "        pass                                  # MUTATED: 旧包留在正式位",
        "tests/test_packseed.py -k \"normal_sync or double_failure\"",
    ),
    (
        "s9-5 P2-2 嵌套结构形状校验打掉（pack.yaml 半边）",
        KN_F,
        "            shape = pack_shape_error(data)\n"
        "            if shape:\n"
        "                raise ValueError(shape)",
        "            shape = pack_shape_error(data)\n"
        "            if shape:\n"
        "                pass",
        "tests/test_pack_yaml_integrity.py -k \"nested_params or banwords_shape\"",
    ),
    (
        "s9-6 P2-3 _swap_in 跨盘失败后不清半截 dst（回滚被跳过）",
        IMPORT_F,
        "        if dst.is_dir():\n"
        "            shutil.rmtree(dst, ignore_errors=True)",
        "        if False and dst.is_dir():\n"
        "            shutil.rmtree(dst, ignore_errors=True)",
        "tests/test_pack_import.py -k partial_dst",
    ),
    (
        "s9-7 P2-4 模板包丢 $alt_guide（换一版候选在新包里静默消失）",
        TPL_SKILL_F,
        "      $alt_guide\n\n      【要求】",
        "\n      【要求】",
        "tests/test_pack_yaml_integrity.py -k elevator_contract",
    ),
    (
        "s9-8 is_new / prev_keys 退回 `guid or title` 旧键式",
        INTEL_F,
        '        row["is_new"] = item_key(it) not in prev_keys',
        "        row['is_new'] = (it.get('guid') or it.get('title')) not in prev_keys",
        "tests/test_intel.py -k is_new_uses_item_key",
    ),
    (
        "s9-9 P2-20 _merge_usage 退化成覆盖（重试的账被最后一次抹掉）——上轮临时验证过但没登记，补上",
        LLM_F,
        "        target[k] = v if not isinstance(cur, (int, float)) else cur + v",
        "        target[k] = v",
        "tests/test_llm_retry.py -k accumulates",
    ),
    (
        "A-1 占位豁免去掉上限（退回「只要有一个 {{}} 就豁免」）",
        AI,
        """        denial = ""
        if ph:""",
        """        denial = ""
        if ph:
            return []             # MUTATED: 四条边界整段作废，有占位就豁免""",
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
        'LEX_SCOPE = {"slogan_closing": "cta_only", "opening_ban": "first_sent",\n'
        '             "helper_closing": "cta_strict"}',
        "LEX_SCOPE = {}",
        "tests/test_ai_tells.py -k 'slogan or opening'",
    ),
    (
        "A-6#7 量词退回 11 个白名单（汉字数词写法翻结果）",
        AI,
        '        if re.search(r"\\d", body) or _MEASURE_RE.search(body):',
        '        if re.search(r"\\d", body) or re.search(r"[一二三四五六七八九十百]+'
        '\\s*(?:天|次|台|元|块|米|层|分钟|小时|起|个|位)", body):',
        "tests/test_ai_tells.py -k measure",
    ),
    (
        "A-4 list_enumeration 阈值退回 ≥3（正当分步讲解被误判成清单体）",
        AI,
        "        if len(seq) >= 4:",
        "        if len(seq) >= 3:",
        "tests/test_ai_tells.py -k list_enumeration",
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
                        "通篇无一个具体数字/时间/数量" + (f"（{denial}）" if denial else ""))]""",
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
        "A-2 pack_info 不再校验 rewrite_scope（坏值静默退回默认）—— 缩进随 2026-09-26 pack_info 重构更新",
        KN_F,
        """                serr = rewrite_scope_error(data.get("rewrite_scope"))
                if serr:
                    err = serr""",
        "                pass",
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
        "B 去重不认 guid 优先级（只按 url）—— 键已收进 item_key()，2026-09-26 跟随更新",
        INTEL_F,
        'return str(it.get("guid") or it.get("url") or f"title:{it.get(\'title\')}")',
        'return str(it.get("url") or it.get("guid") or f"title:{it.get(\'title\')}")',
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
        '    rec[str(key)] = {"days": days}',
        '    rec[str(key)] = "1"',
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
        'INTEL_BUSY_STATES = frozenset({"queued", "fetching"})',
        "INTEL_BUSY_STATES = BUSY_STATES",
        "tests/test_intel.py -k does_not_consume_model_quota",
    ),
    (
        "B add_if_room 无视 states 参数（两族额度合成一族）",
        JOBS_F,
        "                   if same_quota_family(j.kind, job.kind)\n"
        "                   and j.state in states and not j.stranded) >= limit:\n"
        "                return False\n",
        "                   if same_quota_family(j.kind, job.kind)\n"
        "                   and j.state in BUSY_STATES and not j.stranded) >= limit:\n"
        "                return False\n",
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
    # ── 今日选题 UI（断言在 verify.js 里，所以选择器用 verify:）────────
    (
        "UI 分组桶按 x.label 找（而桶里存的是 x.g）→ 每张卡各成一组",
        TOPICS_F,
        "    let bucket = groups.find(x => x.g.label === g.label);",
        "    let bucket = groups.find(x => x.label === g.label);",
        "verify:topics",
    ),
    (
        "UI 切视图不写 #right[data-view]（头部情报动作永远不显示）",
        UI_F,
        '  const right = $("right");\n  if (right) right.dataset.view = name;',
        "  const right = null;",
        "verify:topics",
    ),
    (
        "UI「换一批」改成重新抓（把零成本的翻页换成真金白银）",
        TOPICS_F,
        '  $("btn-more").onclick = () => { page += 1; render(); };',
        '  $("btn-more").onclick = () => { page += 1; render(); api.intelRefresh(packName()); };',
        "verify:topics",
    ),
    (
        "UI「忽略」不本地摘掉（要等一整轮才消失）",
        TOPICS_F,
        "    for (const g of data.groups || []) g.items = (g.items || []).filter(it => !dropped.has(itemKey(it)));",
        "",
        "verify:topics",
    ),
    (
        "UI「未接入 N」不渲染（未接入的平台变成界面上不存在）",
        TOPICS_F,
        '    notes.push(`<span class="src-note" title="${esc(why.join("；"))}">未接入 ${off.length}</span>`);',
        "",
        "verify:topics",
    ),
    (
        "UI「去生成」自动发送（不给改参数的机会）",
        TOPICS_F,
        '  refreshGate();\n  gotoView("chat");',
        '  refreshGate();\n  gotoView("chat"); import("./jobs.js").then(m => m.send());',
        "verify:topics",
    ),
    (
        "UI 情报源表把「接入」与「上次抓取」合成一列",
        TOPICS_F,
        '      <td>${last}</td>\n      <td class="num">${g.count}</td>',
        '      <td class="num">${g.count}</td>',
        "verify:topics",
    ),
    # ── s10 组（2026-09-27 头部按视图渲染 + 选题页签栏 + 筛选胶囊）──────
    # 跑这一组：python _verify/mutate.py -k s10
    (
        "s10-1 head.js 不分视图，永远落会话头部（切到选题视图顶部还是「新对话」）",
        HEAD_F,
        '  const vh = view === "chat" ? null : viewHeads[view];',
        '  const vh = null;',
        "verify:topics",
    ),
    (
        "s10-2 gotoView 不重画头部（问题 1 原样复发）",
        UI_F,
        "  paintHead();",
        "",
        "verify:topics",
    ),
    (
        "s10-3 点左栏会话行不切回会话视图（选题视图里点会话 = 界面上毫无反馈）",
        SESS_F,
        '    gotoView("chat");',
        "",
        "verify:topics",
    ),
    (
        "s10-4 筛选胶囊不带 .on（「现在筛的是哪一颗」又读不出来了）",
        TOPICS_F,
        '''    + ` class="src-pill${curSrc === src ? " on" : ""}"`''',
        '''    + ` class="src-pill"`''',
        "verify:topics",
    ),
    (
        "s10-5 .src-pill.on 只改字色不改底色（选中态塌回「看不出区别」）",
        CSS_F,
        ".src-pill.on { background: var(--fill); color: var(--text-1); font-weight: 600; }",
        ".src-pill.on { color: var(--text-1); font-weight: 600; }",
        "verify:topics",
    ),
    (
        "s10-6 页签回调不切「情报源」面（点了页签没反应）",
        TOPICS_F,
        '    $("topics-src").classList.toggle("hidden", id !== "sources");',
        "",
        "verify:topics",
    ),
    (
        "s10-7 页签条给满栏内边距（与阅读面差出 --s6，左边界不再重合）",
        CSS_F,
        "  top: 0; padding: var(--s6) 0 0; gap: var(--s6);",
        "  top: 0; padding: var(--s6) var(--s6) 0; gap: var(--s6);",
        "verify:topics",
    ),
    # ── s11 组（2026-09-27 全方位审查后补的修复）──────────────────────
    # 跑这一组：python _verify/mutate.py -k s11
    (
        "s11-1 换包收口不发事件（面板点选换包时选题视图停在旧包，看着 A 做 B）",
        UI_F,
        '  emit("pack", $("pack")?.value || "");',
        "",
        "verify:topics",
    ),
    (
        "s11-2 setParam 不校验选项是否存在（把界面显示不出来的值发进引擎、胶囊显示空白）",
        UI_F,
        "sels.every(s => Array.from(s.options).some(o => o.value === v))",
        "true",
        "verify:topics",
    ),
    (
        # 原来这条指 `.sig-grid > :nth-child(3n)` —— 卡片那四格轨道已被**整段撤掉**
        # （真实数据是 D=0.136 / S=0 / E=null，四格里三格常年空刻度）。同一个缺陷
        # 在排行面换了个形状活着：四根条靠"组间缝比组内缝宽"才读成四根，
        # 缝一equal 就是「0.92 名次」与下一根粘成一个词组。判据见 selState 那组
        # 同一层的 `sigGaps`（比的是计算值，不写死 px）。
        # 2026-09-30 重指：信号条 4 列改 2×2（gap 变成「行 s2 列 s4」两值形态），
        # 旧锚点（repeat(4,1fr) + gap: var(--s2)）随实现一起失效 —— 变异改打列缝
        # （--s4 → --s1），列缝一塌，四根条照样粘成词组，判据语义不变。
        "s11-3 四根信号条的组间缝退回与组内同宽（「0.92 名次」与下一根粘成一个词组）",
        CSS_F,
        "  gap: var(--s2) var(--s4); }",
        "  gap: var(--s1); }",
        "verify:topics",
    ),
    (
        "s11-4 选题分组容器不挂 .kb-groups（两组之间贴死 0px）",
        HTML_F,
        '<div id="topics-root" class="kb-groups"></div>',
        '<div id="topics-root"></div>',
        "verify:topics",
    ),
    (
        "s11-5 被筛源清零后不回落「全部」（一颗选中的都没有 + 空池假象）",
        TOPICS_F,
        '    curSrc = "all"; page = 0; syncChips();',
        "",
        "verify:topics",
    ),
    (
        "s11-6 抓取失败的源被算进「今天没货」（等平台 vs 去配 Key，两句话相反）",
        TOPICS_F,
        'const idle = groups.filter(g => g.state === "ok" && g.count === 0 && !sourceError(g.label));',
        'const idle = groups.filter(g => g.state === "ok" && g.count === 0);',
        "verify:topics",
    ),
    (
        "s11-7 页签条不跟面走档（吸顶条盖不住 1160 的表格，内容从两侧穿出）",
        CSS_F,
        '#view-topics[data-face="sources"] .topics-tabs { max-width: var(--w-stg-3col); }',
        "",
        "verify:topics",
    ),
    # ── s12 组（2026-09-27 契约线 P0 修复：失败归属 / 今天口径 / 破坏性快捷键）──
    # 跑这一组：python _verify/mutate.py -k s12
    (
        "s12-1 抓取失败按 id 归属（一个适配器出三个源时，一次超时把三源全标红）",
        INTEL_F,
        '            errors[spec.label] = f"{type(e).__name__}: {e}"',
        '            errors[spec.id] = f"{type(e).__name__}: {e}"',
        "tests/test_intel.py -k same_id_sources_fail",
    ),
    (
        "s12-2 界面按 id 查失败原因（与后端按 label 写对不上 → 失败源被说成今天没货）",
        TOPICS_F,
        "    const err = sourceError(g.label);",
        "    const err = sourceError(g.id);",
        "verify:topics",
    ),
    (
        "s12-3 「已忽略 N」用历史累计（配的句子却是只影响今天）",
        TOPICS_F,
        "    const today = (data && data.ignored_today) || 0;",
        "    const today = (data && data.ignored) || 0;",
        "verify:topics",
    ),
    (
        "s12-4 选题视图里按 Esc 不把用户带回那条会话（停不停是第二步，先让他看见要停什么）",
        # ⚠ 变异只撤 `gotoView("chat")` 这一行，不撤整块守卫：
        #   撤整块会让第一下 Esc 真的停掉作业，后面所有依赖"这条作业跑到 done"
        #   的断言一起崩 —— 那时判据读到的是"网崩了"而不是"这条红了"，
        #   归因就废了（实测过）。"不许隔屏停作业"那一半由 `busy` 判据守着，
        #   整块被删时整网会大声崩，不是静默漏过。
        UI_F,
        '          gotoView("chat");\n          toast("已带你回到正在生成的会话 —— 再按一次 Esc 停止它", 4200);',
        '          toast("已带你回到正在生成的会话 —— 再按一次 Esc 停止它", 4200);',
        "verify:",
    ),
    # ── s13 组（2026-09-27 文案线 P0-3 / P1-4 / P1-6：终态话术收进一张表）──
    # 跑这一组：PYTHONUTF8=1 .venv/Scripts/python.exe _verify/mutate.py -k s13
    # 这一组的判据全住在整网（新断言写在 verify.js 主文件里），所以门禁一律 "verify:"。
    (
        "s13-1 页头结论退回二分（没测出来的产物亮红 ✗，与下方横幅一屏两话）",
        RESULT_F,
        "  const vk = verdictKey(passed);",
        '  const vk = passed === true && hardN === 0 ? "ok" : "no";',
        "verify:",
    ),
    (
        "s13-2 合规页签结论退回二分（同上，另一处调用点）",
        RESULT_F,
        "  const ck = verdictKey(passState(ch));\n  const comply = VERDICT[ck];",
        '  const ck = passState(ch) === true && hardN === 0 ? "ok" : "no";\n  const comply = VERDICT[ck];',
        "verify:",
    ),
    (
        "s13-3 有人在渲染处写回别名（✓ 合格 / 需人工确认）——静态禁令的正对照",
        RESULT_F,
        '  const verdict = `${VERDICT[vk].word}${vk !== "none" ? hardSuffix(hardN) : ""}`;',
        '  const verdict = vk === "ok" ? "✓ 合格" : "✗ 需人工确认";',
        "verify:static",
    ),
    (
        "s13-4 左栏那一行用第二套词（「未测」与表里的「没有校验结论」并存）",
        SESS_F,
        "    return k === \"none\" ? `${STATE_LABEL.done} · ${VERDICT.none.word}` : VERDICT[k].word;",
        "    return k === \"none\" ? \"已完成 · 未测\" : VERDICT[k].word;",
        "verify:",
    ),
    (
        "s13-5 表里的终态词改成「已取消」（停止键与提示不同名，别名禁令也该红）",
        WORDS_F,
        "  cancelled: \"已停止\",",
        "  cancelled: \"已取消\",",
        "verify:static",
    ),
    (
        "s13-6 节奏列不查表直接印机器码（daily / on_demand 上屏）",
        TOPICS_F,
        "      <td>${esc(CADENCE_LABEL[g.cadence] || g.cadence || \"—\")}</td>",
        "      <td>${esc(g.cadence || \"—\")}</td>",
        "verify:topics",
    ),
    # ── s14 组（2026-09-27 文案线 P1-3 + P3-7：图例⇄判据⇄卡片同一套刻度）──
    # 跑这一组：PYTHONUTF8=1 .venv/Scripts/python.exe _verify/mutate.py -k s14
    (
        "s14-1 卡片把机会分再除以 100 印成 0.72（与同屏图例的「≥ 60」两套刻度）",
        TOPICS_F,
        '  const opp = (sc.opportunity === null || sc.opportunity === undefined)\n'
        '    ? "—" : esc(String(Math.round(sc.opportunity)));',
        '  const opp = (sc.opportunity === null || sc.opportunity === undefined)\n'
        '    ? "—" : esc(String(Math.round(sc.opportunity / 100)));',
        "verify:topics",
    ),
    (
        "s14-2 图例的机会分阈值与引擎判据写散（改一边就没人对账）",
        HTML_F,
        "            <span><i class=\"hi\"></i>机会分 ≥ 60 · 值得做</span>",
        "            <span><i class=\"hi\"></i>机会分 ≥ 70 · 值得做</span>",
        "verify:static",
    ),
    (
        "s14-3 图例写回代码里不存在的规则（那条「50–59 要换角度」就是这么来的）",
        HTML_F,
        "            <span><i class=\"low\"></i>供给 S ≥ 0.70 · 红海，要换角度</span>",
        "            <span><i class=\"mid\"></i>50–59 · 要换角度</span>",
        "verify:static",
    ),
    (
        "s14-4 渲染层读回条目级 it.warn（引擎从不发这个键的死出口）",
        TOPICS_F,
        '    <div class="t-desc${/^https?:/.test(desc) ? " url" : ""}" title="${esc(prov)}">${esc(desc)}</div>',
        '    <div class="t-desc${/^https?:/.test(desc) ? " url" : ""}" title="${esc(prov)}">${esc(desc)}${it.warn || ""}</div>',
        "tests/test_intel_contract.py -k item_level_warn",
    ),
    (
        "s14-5 卡片不再渲染 flags（结论那排标记整条下线：机会分高 / 数据不足 都没了）",
        TOPICS_F,
        "    <div class=\"res-top\"><b class=\"card-title\">${esc(it.title)}</b>${flags}</div>",
        "    <div class=\"res-top\"><b class=\"card-title\">${esc(it.title)}</b></div>",
        "verify:topics",
    ),
    # ── s15 组（2026-09-27 文案线 P1-7 / P1-9 + 流程线 P2-4 的界面那半）──
    # 跑这一组：PYTHONUTF8=1 .venv/Scripts/python.exe _verify/mutate.py -k s15
    (
        "s15-1 卸载弹窗又把同一个东西叫「技能包」（面板叫行业包，用户以为两个东西）",
        SETTINGS_F,
        "  const ok = await appConfirm(\"卸载行业包\",",
        "  const ok = await appConfirm(\"卸载技能包\",",
        "verify:static",
    ),
    (
        "s15-2 选择器把内部模式名直接印上屏（mock 模式 / 夹具是开发用语）",
        UI_F,
        "  const cur = m.mock ? \"演示模式\"",
        "  const cur = m.mock ? \"mock 模式\"",
        "verify:static",
    ),
    (
        "s15-3 演示模式的悬停说明仍写「当前模型 + 切换后生效」（根本没在调模型）",
        UI_F,
        "  sel.title = m.mock\n    ? \"演示模式：不调用模型，返回内置样例文案\"\n    : `当前模型：${cur}。切换后对后续生成生效（正在跑的作业不受影响）`;",
        "  sel.title = `当前模型：${cur}。切换后对后续生成生效（正在跑的作业不受影响）`;",
        "verify:",
    ),
    (
        "s15-4 搜索空结果不把截断说出来（「这里只列最近 N 条」被去掉 = 搜不到被说成没有）",
        SESS_F,
        "    const atCap = cap > 0 && (items || []).length >= cap;",
        "    const atCap = false;",
        "verify:",
    ),
    # ── s16 组（2026-09-27 第 8 轮审查：类有了、规则没有 / 写死阈值 / 二分）──
    # 跑这一组：PYTHONUTF8=1 .venv/Scripts/python.exe _verify/mutate.py -k s16
    (
        "s16-1 删掉页签结论色的两条规则（回到「class 有、屏幕上看不出来」）",
        CSS_F,
        ".res-tab.ok .rt-b { color: var(--ok); }\n.res-tab.bad .rt-b { color: var(--bad); }",
        "",
        "verify:",
    ),
    (
        "s16-2 偏差胶囊退回写死 ±10%（不看引擎下发的 tolerance_pct）",
        RESULT_F,
        "  const devCls = (tol === null || tol === undefined) ? \"\"\n    : (Math.abs(dev) <= tol ? \"good\" : \"bad\");",
        "  const devCls = Math.abs(dev) <= 10 ? \"good\" : \"bad\";",
        "verify:",
    ),
    (
        "s16-3 人味分页签退回二分（没配词表的包被画成「差」）",
        RESULT_F,
        "      cls: smell === null ? \"\" : smell >= 90 ? \"ok\" : \"bad\",",
        "      cls: (smell ?? -1) >= 90 ? \"ok\" : \"bad\",",
        "verify:",
    ),
    (
        "s16-4 端点又写第二个历史上限字面量（「这个数只有一份」变假）",
        SRV_F,
        "        return (running + items)[:pipeline.store.HISTORY_LIMIT]",
        "        return (running + items)[:100]",
        "tests/test_intel_contract.py -k history_cap",
    ),
    (
        "s16-5 渲染层读一个引擎从不发的键（契约扫描的正对照）",
        TOPICS_F,
        '    <div class="t-desc${/^https?:/.test(desc) ? " url" : ""}" title="${esc(prov)}">${esc(desc)}</div>',
        '    <div class="t-desc${/^https?:/.test(desc) ? " url" : ""}" title="${esc(prov)}">${esc(it.phantom_note || desc)}</div>',
        "tests/test_intel_contract.py -k reads_only_keys",
    ),
    # ── s17 组（2026-09-27 第 8 轮审查的两条后端 P1：id→label 归并 / 不引用过期快照）──
    # 跑这一组：PYTHONUTF8=1 .venv/Scripts/python.exe _verify/mutate.py -k s17
    (
        "s17-1 那句限定回头引用开机快照里的总数（删过一条就说谎，且没人会红）",
        SESS_F,
        "    const atCap = cap > 0 && (items || []).length >= cap;",
        "    const atCap = cap > 0 && state.meta.history.total > cap;",
        "verify:static",
    ),
    (
        "s17-2 旧产物按 id 存的失败原因不再归并（「没抓到」在界面上变成「今天没货」）",
        INTEL_F,
        "    errors = _attribute_legacy_errors(errors, sources)",
        "    # 不归并（回归：老 latest.json 的 id 键按 label 查不出来）",
        "tests/test_intel.py -k legacy",
    ),
    (
        "s17-3 归并时只挂第一个候选源（其余几条继续被说成正常没货）",
        INTEL_F,
        "        note = msg if len(cands) == 1 else \\\n            f\"（旧记录按 id={key} 存，无法确定是哪个源）{msg}\"",
        "        note = msg",
        "tests/test_intel.py -k ambiguous",
    ),
    # ── s18 组（2026-09-27 第 8 轮审查 界面 P1：破坏性动作要问 / 内部词不上屏）──
    # 跑这一组：PYTHONUTF8=1 .venv/Scripts/python.exe _verify/mutate.py -k s18
    (
        "s18-1 「停止全部」回到一键生效（N 条作业说停就停，token 还不退）",
        # ⚠ 整条语句一起换（原来只换第一行，第二行那截模板留在原地 → ui.js 语法炸，
        #   判据读到的是"整网崩"而不是"这条红了"，归因作废 —— 变异只撤它要证伪的那件事，
        #   但必须撤得**语法闭合**）。
        UI_F,
        "    const ok = await appConfirm(\"停止全部后台生成\",\n      `确认停止这 ${bg.length} 条在跑的生成？已经花掉的模型调用不会退回。`);",
        "    const ok = true;   // 不问就直接停（回归：破坏性动作没有确认层）",
        "verify:",
    ),
    (
        "s18-2 文风强度的机器码直接上屏（读者要去猜 strong 是什么）",
        RESULT_F,
        "${esc(severityLabel(h.severity))}",
        "${esc(h.severity)}",
        "verify:static",
    ),
    (
        "s18-3 失败详情把包目录 slug 印给人看",
        JOBSJS_F,
        "行业包：${failParams.pack ? packLabel(failParams.pack) : \"-\"}",
        "行业包：${failParams.pack || \"-\"}",
        "verify:static",
    ),
    (
        "s18-4 悬停说明又替后端宣布「作业已释放」（从没核过的事实）",
        RESULT_F,
        # 锚点跟的是第 15 批改后的那句话（原文案尾巴上的「整体重生成」被收成「换一版」）
        '          : "单段重写只在刚生成的那条上可用；历史记录请用「换一版」"}',
        '          : "历史记录不可局部重写（后台作业已释放），可用「换一版」"}',
        "verify:static",
    ),
    (
        "s18-5 导出的 md 头部写目录 slug（交付物与界面两个名字）",
        RESULT_F,
        "    `- ${meta}（${packLabel(r.pack)}）`, \"\",",
        "    `- ${meta}（${r.pack}）`, \"\",",
        "verify:static",
    ),
    # ── s19 组（2026-09-27 第 8 轮审查 后端 P2/P3：认不出的节奏要留话 / 交付物 repr）──
    # 跑这一组：PYTHONUTF8=1 .venv/Scripts/python.exe _verify/mutate.py -k s19
    (
        "s19-1 引擎不认识的 cadence 不再留话（这条源从此永不自动补抓，且界面零提示）",
        INTEL_F,
        "        if (cadence and cadence not in CADENCE_DAYS\n                and cadence.lower() not in AUTO_FETCH_OFF):",
        "        if False:",
        "tests/test_intel.py -k unknown_cadence",
    ),
    (
        "s19-2 交付物把命中明细按 Python repr 印出去",
        STORE_F,
        "              f\"- 硬禁用词：{_hits('hard_hits')}\",",
        "              f\"- 硬禁用词：{ch.get('hard_hits') or '无'}\",",
        "tests/test_ai_tells.py -k repr",
    ),
    (
        "s19-3 中文节奏表少了 manual（两表一漂移，源表就印机器码）",
        WORDS_F,
        "  manual: \"手动\",",
        "",
        "tests/test_intel_contract.py -k cadence_tables",
    ),
    # ── s20 组（2026-09-27 第 8 轮审查 界面 P2：句子与判据同源 / 状态码不上屏）──
    # 跑这一组：PYTHONUTF8=1 .venv/Scripts/python.exe _verify/mutate.py -k s20
    (
        "s20-1 「超配额」的线又写回两处字面量（改一处另一处就不一致）",
        RESULT_F,
        "    const over = !!(info && info.quota && info.chars > info.quota * QUOTA_OVER_RATIO);",
        "    const over = !!(info && info.quota && info.chars > info.quota * 1.3);",
        "verify:static",
    ),
    (
        "s20-2 节奏条又宣布「各段都不超配额」（超了 20% 的段也被说成合规）",
        RESULT_F,
        "        ${over ? `${over} 段超配额三成以上` : \"各段超配额都不到三成\"}",
        "        ${over ? `${over} 段超配额` : \"各段都不超配额\"}",
        "verify:static",
    ),
    (
        "s20-3 更新被拒的提示又把主进程状态码印给人看（idle / downloaded）",
        ABOUT_F,
        "      toast(`现在不能安装更新（此刻：${statusText({ state: r.state })}）`, 3500);",
        "      toast(`现在不能安装更新（当前状态：${r.state}）`, 3500);",
        "verify:",
    ),
    # ── s21 组（2026-09-27 第 8 轮审查 流程线 P1-2 / P2-9 + 样式线 P2-5）──
    # 跑这一组：PYTHONUTF8=1 .venv/Scripts/python.exe _verify/mutate.py -k s21
    (
        "s21-1 重抓去掉 aria-busy 与呼吸 class（只剩禁用，读屏与视觉都不知道在跑）",
        TOPICS_F,
        "    btn.setAttribute(\"aria-busy\", \"true\");\n    btn.classList.add(\"busy\");",
        "    // 只留 disabled",
        "verify:topics",
    ),
    (
        "s21-2 用 textContent 写进行中文字（图标按钮的 SVG 被整颗抹掉，落地后剩空按钮）",
        TOPICS_F,
        "    btn.classList.add(\"busy\");",
        "    btn.textContent = \"抓取中…\";",
        "verify:topics",
    ),
    (
        "s21-3 冷启动的选题读数又写死一个 0（还没去过选题就宣布\"今天有 0 条\"）",
        HTML_F,
        "          <kbd id=\"topics-count\" hidden></kbd>",
        "          <kbd id=\"topics-count\">0</kbd>",
        "verify:",
    ),
    (
        "s21-4 拿到数据后也不揭开读数（hidden 恒真 = 那个 kbd 是装饰品）",
        TOPICS_F,
        "  if (cnt) { cnt.textContent = String(data.items.length); cnt.hidden = false; }",
        "  if (cnt) { cnt.textContent = String(data.items.length); }",
        "verify:topics",
    ),
    (
        "s21-5 首页那条滚动列不退让滚动条槽位（切进选题整列横移半个滚动条）",
        CSS_F,
        "  scrollbar-gutter: stable;\n  padding: var(--s6) var(--s6) var(--s4);",
        "  padding: var(--s6) var(--s6) var(--s4);",
        "verify:",
    ),
    # ── s22 组（2026-09-27 第 8 轮审查 流程线 P2-6 / P2-7：键盘可达）──
    # 跑这一组：PYTHONUTF8=1 .venv/Scripts/python.exe _verify/mutate.py -k s22
    (
        "s22-1 确认弹层没有焦点圈（Tab 两下焦点就溜回背景，Enter 打到后面的按钮）",
        OVER_F,
        "        const f = [$(\"cd-yes\"), $(\"cd-no\")].filter(Boolean);",
        "        const f = [];",
        "verify:",
    ),
    (
        "s22-2 占位横幅只认鼠标（Enter/Space 不接，键盘用户到不了那一处占位）",
        RESULT_F,
        "      if (e.key === \"Enter\" || e.key === \" \" || e.key === \"Spacebar\") {",
        "      if (false) {",
        "verify:",
    ),
    (
        "s22-3 可操作横幅没被声明成按钮（role/tabindex 缺失 = 键盘上根本到不了）",
        RESULT_F,
        "data-jump=\"placeholder\" role=\"button\" tabindex=\"0\"",
        "data-jump=\"placeholder\"",
        "verify:",
    ),
    # ── s23 组（2026-09-27 契约线 P2-4：窄窗门禁有没有牙）──
    # 跑这一组：PYTHONUTF8=1 .venv/Scripts/python.exe _verify/mutate.py -k s23
    (
        "s23-1 查看器退回 flex:1（basis 0）—— 窄窗下被挤到 0 宽、整块消失",
        CSS_F,
        ".kb-view { flex: 1 1 240px; min-width: 0; display: flex; flex-direction: column; align-self: stretch; }",
        ".kb-view { flex: 1; min-width: 0; display: flex; flex-direction: column; align-self: stretch; }",
        "verify:",
    ),
    (
        "s23-2 分组列退回不可缩 320（窄窗下把查看器挤出可视区）",
        CSS_F,
        "  flex: 0 1 320px; min-width: 200px;",
        "  flex: 0 0 320px; min-width: 320px;",
        "verify:",
    ),
    (
        "s23-3 首页那条列不跟窄窗降内边距（两列左边界错开 8px）",
        # 先前的伪证是删 `.topics-inner` 那行：查过 CSS 才知道它的基准 padding-inline
        # 本来就是 --s4(16)，且列是 max-width + margin-inline:auto 居中 ——
        # 那行媒体查询只改**底部**留白，删掉对左边界零影响，所以"漏检"是伪证无效，
        # 不是判据无效。真正会移动左边界的是 #chat-stream 这一行（24 → 16）。
        CSS_F,
        "  #chat-stream { padding: var(--s4) var(--s4) var(--s3); }",
        "  #chat-stream { padding: var(--s6) var(--s6) var(--s3); }",
        "verify:",
    ),
    (
        "A-3 对账解析全文扫（把文档后面别的表也当成 §2.1 的 tell 表）",
        ROOT / "tests/test_aitells_alignment.py",
        '        start = next(i for i, ln in enumerate(lines) if ln.startswith("### 2.1"))',
        "        start = 0",
        "tests/test_aitells_alignment.py -k spec_table",
    ),
    # ── B7：情报进提示词（2026-09-23）────────────────────────────
    (
        "B7 模板摘掉 $intel_block 引用（接线断：引擎注入了但模板不用）",
        SKILL_F,
        # 电梯包里有两处 `$intel_block`（select 与 write），判据按 stage 分参数逐条查。
        # 原来这条只写 `      $intel_block\n` —— 全文两处，replace-first 落在 select 那处，
        # 于是"锚点歧义"侥幸没出事；写全上下文之后，它明确只摘 select 那一处。
        "      $intel_block\n    # 钩子库不在这里声明",
        "      # 变异：摘掉引用\n    # 钩子库不在这里声明",
        "tests/test_intel_b7.py -k reference",
    ),
    (
        "B7 禁用词闸门失效（含 hard 词的条目被放行进提示词）",
        INTEL_F,
        "        if any(b and b in blob for b in banned):\n            dropped.append(title)\n            continue",
        "        if False:  # 变异：闸门失效\n            dropped.append(title)\n            continue",
        "tests/test_intel_b7.py -k picks_relevant",
    ),
    (
        "B7 指纹退回整块进指纹（情报一刷新所有历史版本失效）",
        PIPE_F,
        "        base = user.split(PROMPT_HEAD, 1)[0] if intel_key else user",
        "        base = user  # 变异：整块进指纹",
        "tests/test_intel_b7.py -k stable_across",
    ),
    (
        "状态色 --ok 亮色调回旧值（对比度 4.40 掉到 4.5 以下）",
        CSS_F,
        "--ok: light-dark(#1b7030, #57c173);",
        "--ok: light-dark(#248a3d, #57c173);",
        "verify:contrast",
    ),
    # ── P3-14 运行期键对账（2026-09-23 复审补）────────────────────
    # 原守卫只查了 PERSISTED/ELSEWHERE/DROPPED 三张表两两不重叠，唯独漏了
    # RUNTIME ⊆ PERSISTED —— 运行期键掉出后者时 result.json 的 params 按
    # PERSISTED 逐键取，静默不进产物，没有任何断言红（实测老方程全绿）。
    (
        "P3-14 运行期键掉出 PERSISTED_PARAMS（temperature）→ 子集 + 落盘双红",
        PIPE_F,
        '    "model", "max_tokens", "temperature",',
        '    "model", "max_tokens",',
        "tests/test_quota_degraded_signal.py",
    ),
    (
        "P3-14 运行期附加整段被删（不遍历 RUNTIME_PARAMS）→ 作业直接失败",
        PIPE_F,
        """            for k in RUNTIME_PARAMS:
                p[k] = getattr(client.cfg, k)""",
        "            pass  # 变异：附加被删",
        "tests/test_quota_degraded_signal.py",
    ),
    # ── 五路审查 P0 五条（2026-09-23 当轮修，逐条实测复现过）──────────
    (
        "P0-1 engine-dialogs.js 从 build.files 漏掉（装完启动即 MODULE_NOT_FOUND）—— 2026-09-26 跟随 files 列表追加 updater-core 后更新",
        ROOT / "desktop/package.json",
        '      "engine-dialogs.js",\n',
        '      "__engine-dialogs-removed__.js",\n',
        "node:desktop/packaging.test.js",
    ),
    (
        "P0-2 搜索提示退回不 esc（用户输入进 innerHTML = XSS）",
        TOPICS_F if False else ROOT / "desktop/renderer/js/sessions.js",
        '没有匹配「${esc(query)}」的会话',
        '没有匹配「${query}」的会话',
        "verify:xss",
    ),
    # （已撤）原「P0-2 参数胶囊 option 退回不 esc」：`<option>` 的内容按 HTML 规范
    #   只当**文本**解析，剥掉 esc() 之后 DOM 里既不会多出元素、`o.text` 也照样带着
    #   `<img` —— 判据两种写法都给同一个结果，实测「全绿 !! 漏检」。
    #   留一条永远不会红的变异比没有更糟（它会让人以为这一路有门禁），所以撤掉；
    #   `sections/xss.js` 那半边继续当**结果**检查（不注入元素、payload 以文本呈现），
    #   真正的注入面由另两条守着：搜索无结果提示（`<p>` 的 innerHTML）与
    #   行业包面板的文件名（`<span>` 的 innerHTML，见下面 P0-2 面板文件名）。
    (
        "P0-2 面板文件名退回不 esc（磁盘路径进 innerHTML = XSS）",
        ROOT / "desktop/renderer/js/settings.js",
        'meta.appendChild(el("span", "kb-name", esc(f.rel)));',
        'meta.appendChild(el("span", "kb-name", f.rel));',
        "verify:",
    ),
    (
        "P0-3 量词字符类加回 分/成/人/起（副词尾字误吞，no_specific 漏报）",
        AI,
        '_MEASURE_CHARS = ("天次台元块米层个位条家间辆口份部套户站年月日秒点倍手脚轮趟宗件种项类款档期批")',
        '_MEASURE_CHARS = ("天次台元块米层个位条家间辆口份部套户站年月日秒分点成倍人手脚轮趟宗件种项类款档期批起")',
        "tests/test_ai_tells.py -k adverb",
    ),
    (
        "P0-4 queued 剔出情报族（并发 refresh 可同时过检，额度上限形同虚设）",
        JOBS_F,
        'INTEL_BUSY_STATES = frozenset({"queued", "fetching"})',
        'INTEL_BUSY_STATES = frozenset({"fetching"})',
        "tests/test_intel.py -k queued",
    ),
    (
        "P0-4 add_if_room 去掉 kind 分族（intel 的 queued 会占生成名额）",
        JOBS_F,
        """            if sum(1 for j in self._jobs.values()
                   if same_quota_family(j.kind, job.kind)
                   and j.state in states and not j.stranded) >= limit:
                return False
""",
        """            if sum(1 for j in self._jobs.values()
                   if j.state in states and not j.stranded) >= limit:
                return False
""",
        "tests/test_intel.py -k queued",
    ),
    (
        "P0-5 删通用 Exception handler（未预期异常退回 500 text/plain 空 body）",
        SRV_F,
        """    @app.exception_handler(Exception)
    async def _unhandled(_req, exc: Exception):
        log.exception("未处理的内部错误：%s: %s", type(exc).__name__, exc)
        return _error_json(f"引擎内部错误（{type(exc).__name__}）：{exc}",
                           500, ERR_INTERNAL)""",
        "    # 变异：通用 handler 删掉",
        "tests/test_server_hardening.py -k unexpected_error",
    ),
    # ── 自动更新方案 s7：发布脚本与冒烟服务器（2026-09-24）──────────
    (
        "s7-1 serve-update parseRange 恒不认 Range（退回 python http.server 的坑：差分被静默跳全量）",
        ROOT / "desktop/scripts/serve-update.mjs",
        "  return { start, end };",
        "  return null;  // 变异：合法 Range 一律不认",
        "node:desktop/serve-update.test.mjs",
    ),
    (
        "s7-2 joinUrl 去掉 encodeURIComponent（文件名空格不编码，闭环验证漏掉「本地能下、线上 404」）",
        ROOT / "desktop/scripts/publish-update.mjs",
        "encodeURIComponent(name)",
        "name",
        "node:desktop/publish-update.test.mjs",
    ),
    (
        "s7-3 pickStale 排序退化成字典序（0.2.10 被判成旧版本删掉，线上版本倒退）",
        ROOT / "desktop/scripts/publish-update.mjs",
        "const sorted = [...versions].sort((x, y) => cmpVer(y.version, x.version));   // 新 → 旧",
        "const sorted = [...versions].sort((x, y) => (y.version < x.version ? -1 : 1));   // 变异：字典序",
        "node:desktop/publish-update.test.mjs",
    ),
    # ── 自动更新方案 §10：纯逻辑层 updater-core（2026-09-24 补登记）──────────
    # 这两条**文档 §10 与 updater-core.test.js 文件头都写明了**（"删掉
    # resolveFeedUrl 的空值分支 / shouldCheckNow 恒真，必须有断言变红"），
    # 但 MUTATIONS 里一直没有它们 —— 于是那句"绿"是自述而非实测。
    # 本项目验收口径就是变异检验，声称了不登记等于没做。
    (
        "s8-1 resolveFeedUrl 删掉空值分支（没配地址也当配了 → 「不配就整体关闭」失效）",
        ROOT / "desktop/updater-core.js",
        "  if (trimmed === '') return null;\n",
        "",
        "node:desktop/updater-core.test.js",
    ),
    (
        "s8-2 shouldCheckNow 恒真（6h 间隔与时钟回拨保护一起失效 → 每次问都放行）",
        ROOT / "desktop/updater-core.js",
        "  if (!(now - lastCheckAt >= interval)) return false; // 含时钟回拨（差为负）→ 不查\n",
        "",
        "node:desktop/updater-core.test.js",
    ),
    # ── s24 组（第 16 批：单位只有一种读法）─────────────────────
    # 跑这一组：PYTHONUTF8=1 .venv/Scripts/python.exe _verify/mutate.py -k s24
    # 六条里只有 s24-2 要走整网（它量的是页面里的纯函数），其余四条走 pytest
    # 的静态扫描与真值表 —— 秒级，不必为一条单位漂移等四分钟。
    (
        "s24-1 sec() 换回 ASCII 秒（静态扫描 + 界面档位同时失守的那个写法）",
        UTIL_F,
        "  return `${Math.round(v * 10) / 10} 秒`;",
        "  return `${Math.round(v * 10) / 10}s`;",
        "tests/test_unit_vocabulary.py -k no_site_renders",
    ),
    (
        "s24-2 fmtDur 整分补回「0 秒」（60 秒的脚本读成「1 分 0 秒」）",
        UTIL_F,
        "  return s % 60 ? `${m} 分 ${s % 60} 秒` : `${m} 分`;",
        "  return `${m} 分 ${s % 60} 秒`;",
        "verify:",
    ),
    (
        "s24-3 _secs 先判档再取整（两侧同一份时长能差 1 秒：59.6 走「60 秒」）",
        STORE_F,
        "    if total < 60:",
        "    if n < 60:",
        "tests/test_unit_vocabulary.py -k secs_matches",
    ),
    (
        "s24-4 分镜「时间」列原样吐模型的 0-4s（交付物里两种读法并存）",
        STORE_F,
        '        return f"{_num(m.group(1))}-{_num(m.group(2))} 秒"',
        "        return s",
        "tests/test_unit_vocabulary.py -k shot_time",
    ),
    (
        "s24-5 「N 秒后重试」在流式分支又抄一份（改字面必漏另一条）",
        LLM_F,
        "                    note = (self._rate_limit_note(wait) if e.code == 429",
        '                    note = (f"上游限流(429)，{self._fmt_wait(wait)} 秒后重试" if e.code == 429',
        "tests/test_unit_vocabulary.py -k rate_limit_note",
    ),
    (
        "s24-6 建议胶囊把档位写死成 ASCII（30s / 90s 与下拉里的「30 秒」不是一个读法）",
        RESULT_F,
        "  if (shorter) chips.push({ label: `压缩到 ${sec(shorter)}`, act: \"duration\", value: shorter });",
        "  if (shorter) chips.push({ label: `压缩到 30s`, act: \"duration\", value: shorter });",
        "tests/test_unit_vocabulary.py -k no_site_renders",
    ),
    (
        "s24-7 建包按钮报「生成中…」而不报已经走了多久（重新指回中文单位那条判据的正对照）",
        SETTINGS_F,
        "      : `生成中 · ${fmtDur(Date.now() - t0)}…`;",
        "      : \"生成中…\";",
        "verify:",
    ),
    (
        "s24-8 设置导航提示用 + \"s\" 拼秒（扫描的第三种形状，第 27 轮抓到的真漏网）",
        UI_F,
        '  if (getParam("duration")) parts.push(sec(getParam("duration")));',
        '  if (getParam("duration")) parts.push(getParam("duration") + "s");',
        "tests/test_unit_vocabulary.py -k no_site_renders",
    ),
    # ── s25 组（第 17 批：草稿接线、动作名、等待时长一份账）──────────
    # 跑这一组：PYTHONUTF8=1 .venv/Scripts/python.exe _verify/mutate.py -k s25
    (
        "s25-1 换表单前不收草稿（captureDraft 回到零调用点那条死代码）——填一半切走就没了",
        SETTINGS_F,
        "  captureDraft();\n  editingId = id || \"\";",
        "  editingId = id || \"\";",
        "verify:",
    ),
    (
        "s25-2 「添加模型」先翻标记再换表单（把正在编辑那条的值当成新增草稿收走）",
        SETTINGS_F,
        "    selectModel(\"\");\n    addingNew = true;",
        "    addingNew = true;\n    selectModel(\"\");",
        "verify:",
    ),
    (
        "s25-3 草稿的反面又改口叫「精修包」（同一件事第二个名字）",
        RESULT_F,
        '<span class="ok">✅ ${PROOF.cleared}</span>',
        '<span class="ok">✅ 精修包</span>',
        "verify:static",
    ),
    (
        "s25-4 建包轮询的兜底截止写回裸毫秒数（后端预算一改就在等一个不存在的时限）",
        SETTINGS_F,
        "    if (Date.now() - t0 > POLL_GRACE_MS) {",
        "    if (Date.now() - t0 > 1800000) {",
        "tests/test_unit_vocabulary.py -k job_timing_numbers",
    ),
    (
        "s25-5 作业轮询超时的话把分钟数写死（「30 分钟」与算出来的那份分叉；2026-10-11 P3-6 随 awaitJob 错误文案一并改口「作业」，三个调用方共用这条轮询）",
        TOPICS_F,
        "      toast(`作业状态未知（已等待 ${Math.round(POLL_GRACE_MS / 60000)} 分钟），请手动刷新或重试`, 5000);",
        "      toast(\"作业状态未知（已等待 30 分钟），请手动刷新或重试\", 5000);",
        "tests/test_unit_vocabulary.py -k job_timing_numbers",
    ),
    # ── s26 组（第 18 批：幽灵 token、缺档的 warn、CSS 注入的字形）────────
    # 跑这一组：PYTHONUTF8=1 .venv/Scripts/python.exe _verify/mutate.py -k s26
    (
        "s26-1 导入徽标退回引用未定义的 --accent（声明在、色从来不上屏）",
        CSS_F,
        ".pl-item .pl-s.imported { color: var(--ink); }",
        ".pl-item .pl-s.imported { color: var(--accent); }",
        "tests/test_css_tokens.py -k no_phantom",
    ),
    (
        "s26-2 删掉 .m-chip.warn 这一档（引擎真发 warn，标记落回中性灰）",
        CSS_F,
        ".m-chip.warn, .acc-badge.warn { background: var(--warn-soft); color: var(--warn); }",
        ".m-chip.warn, .acc-badge.warn { }",
        "verify:topics",
    ),
    (
        "s26-3 折叠箭头退回 CSS 注入字形 ▾（§4 禁、且换字体就换长相）",
        CSS_F,
        ".banner.why > summary::after {\n  content: \"\"; flex: 0 0 auto; width: 0; height: 0; align-self: center;",
        ".banner.why > summary::after { content: \" ▾\"; font-size: var(--f-min); opacity: .6;",
        "tests/test_css_tokens.py -k does_not_inject",
    ),
    # ── s27 组（第 19 批：坏文件要说坏了、摘要带上搜索读的键）──────
    # 跑这一组：PYTHONUTF8=1 .venv/Scripts/python.exe _verify/mutate.py -k s27
    (
        "s27-1 read_job 把坏掉的 job.json 吞回 None（端点就说「记录不存在」）",
        STORE_F,
        "            f = d / \"job.json\"\n            if f.exists():\n                return json.loads(f.read_text(encoding=\"utf-8\"))",
        "            f = d / \"job.json\"\n            if f.exists():\n                try:\n                    return json.loads(f.read_text(encoding=\"utf-8\"))\n                except Exception:\n                    return None",
        "tests/test_server_hardening.py -k corrupt_job_json",
    ),
    (
        "s27-2 已完成的摘要不给 segment（按细分领域搜永远是空态）",
        STORE_F,
        "            \"segment\": p.get(\"segment\"),\n            \"duration\": p.get(\"duration\"), \"chars\": c.get(\"chars_total\"),",
        "            \"duration\": p.get(\"duration\"), \"chars\": c.get(\"chars_total\"),",
        "tests/test_history_corruption.py -k segment",
    ),
    (
        "s27-3 左栏放弃在跑的记录时只 detach，不落实态卡（气泡继续假装在写）",
        SESS_F,
        "    const wasLive = !!state.job && state.job.id === it.id;\n    const viewed = wasLive && dropLiveJobView();",
        "    const wasLive = !!state.job && state.job.id === it.id;\n    if (wasLive) detachJob();\n    const viewed = false;",
        "tests/test_job_state_vocabulary_consistency.py -k abandoning",
    ),
    # ── s28 组（第 20 批：索引不再每读必重建、幽灵下拉）────────────
    # 跑这一组：PYTHONUTF8=1 .venv/Scripts/python.exe _verify/mutate.py -k s28
    (
        "s28-1 设置页退回按 Object.keys(pack.params) 全量画控件（幽灵下拉回来）",
        UI_F,
        "    if (!SENDABLE_PARAMS.includes(key)) { unsendable.push(key); continue; }",
        "    // 变异：不筛了，包声明什么就画什么",
        "tests/test_job_state_vocabulary_consistency.py -k only_renders",
    ),
    (
        "s28-2 跳过的参数键不写进审计行（少掉的下拉变成静默丢失）",
        UI_F,
        "    notes.push(...unsendable.map(k => `参数 ${k}：不显示（请求体带不走，引擎也不读）`));",
        "    // 变异：不报这一句",
        "verify:",
    ),
    (
        "s28-3 索引判据退回「磁盘条数 != 索引条数就重建」（一条坏文件就每读必重建）",
        STORE_F,
        '        if self._count_on_disk() != data.get("disk_count"):',
        "        if self._count_on_disk() != len(out):",
        "tests/test_history_corruption.py -k does_not_rebuild",
    ),
    # ── s29 组（第 21 批：抓取落盘失败要看得见）────────────────────
    # 跑这一组：PYTHONUTF8=1 .venv/Scripts/python.exe _verify/mutate.py -k s29
    (
        "s29-1 重抓之后把作业结果里的框架级错误丢掉（落盘失败又变成静默）",
        TOPICS_F,
        "      const snap = await awaitJob(job_id);\n      jobErrors = (snap && snap.result && snap.result.errors) || {};",
        "      await awaitJob(job_id);   // 变异：不接返回值",
        "verify:topics",
    ),
    # ── s30 组（第 22 批甲：幽灵记录的参数表示只留一份）──────────────
    (
        "s30-1 幽灵失败记录把参数平铺回去（界面读 params，头部空标题 + 重试丢主题）",
        SRV_F,
        '                        "params": {"pack": p.get("pack", ""),\n'
        '                                   "topic": p.get("topic", "")},',
        '                        "pack": p.get("pack", ""), "topic": p.get("topic", ""),',
        "tests/test_server_hardening.py -k ghost_failed_entry",
    ),
    # ── s31 组（第 23 批：胶囊 pressed 状态同步、交付物不带机器码）────
    (
        "s31-1 点胶囊只 render、不同步 pressed（卡区换了、读屏还念上一颗）",
        TOPICS_F,
        "    b.onclick = () => { curSrc = b.dataset.src; page = 0; syncChips(); render(); };",
        "    b.onclick = () => { curSrc = b.dataset.src; page = 0; render(); };",
        "verify:topics",
    ),
    (
        "s31-2 交付的 脚本.md 又印出 tell 的机器键与 strong/weak",
        STORE_F,
        "            lines.append(f\"> - {severity_word(h.get('severity', ''))}\"",
        "            lines.append(f\"> - [{h.get('severity', '')}] {h.get('id', '')}\"",
        "tests/test_ai_tells.py -k internal_identifiers",
    ),
    (
        "s31-3 引擎侧严重度词表改口（界面仍叫「明显/轻微」，两侧分叉）",
        ROOT / "app/ai_tells.py",
        'SEVERITY_WORD = {"strong": "明显", "weak": "轻微"}',
        'SEVERITY_WORD = {"strong": "严重", "weak": "轻微"}',
        "tests/test_job_state_vocabulary_consistency.py -k severity_words",
    ),
    # ── s32 组（第 24 批：交付物里的 None 与尾零）──────────────────
    (
        "s32-1 参数行退回 p.get(k, '')（键在而值为 None 时印出字符串 None）",
        STORE_F,
        "             f\"　细分：{_txt(p.get('segment'))}　受众：{_txt(p.get('audience'))}\",",
        "             f\"　细分：{p.get('segment', '')}　受众：{p.get('audience', '')}\",",
        "tests/test_ai_tells.py -k never_prints_the_word_none",
    ),
    (
        "s32-2 时间轴退回直插 float（交付物写「0.0-8.5 秒」，界面写「0–8.5 秒」）",
        STORE_F,
        "        lines.append(f\"**【{label}】** {_num(tm['start'])}-{_num(tm['end'])} 秒\"",
        "        lines.append(f\"**【{label}】** {tm['start']}-{tm['end']} 秒\"",
        "tests/test_ai_tells.py -k trailing_zeros",
    ),
    # ── s33 组（第 25 批：补齐**长期缺席的变异靶子**）────────────────
    # 为什么补：靶子清单是历次修复轮「顺手加一条」攒出来的，于是
    # 几个文件**一次都没被改坏过** —— 而「没被变异过」的断言，
    # 我们并不知道它到底能不能红（本仓反复栽在这上面）。
    # ⚠ 下面每条都**先跑一遍 selector 确认它选得到东西**，再钉进来：
    #   写变异时顺手编一个 `-k` 名字是很自然的事，而选不到东西的
    #   selector 会让这条变异"跑完了、全绿、什么都没验"——
    #   比不写还坏（它会让人以为这个文件被覆盖过了）。
    (
        "s33-1 checker：非法语速不再兜底（除零直接炸，且在烧完 token 之后）",
        ROOT / "app/checker.py",
        "    try:\n        r = float(rate)\n    except (TypeError, ValueError):\n        r = DEFAULT_RATE\n    if r <= 0:\n        r = DEFAULT_RATE",
        "    try:\n        r = float(rate)\n    except (TypeError, ValueError):\n        r = DEFAULT_RATE\n    if r <= 0:\n        r = 0.0",
        "tests/test_engine_api.py -k split_and_estimate",
    ),
    (
        "s33-2 schemas：主题最小长度从 2 放成 0（空主题不再被拒）",
        ROOT / "app/schemas.py",
        '    topic: str = Field(min_length=2, max_length=TOPIC_MAX, description="主题")',
        '    topic: str = Field(min_length=0, max_length=TOPIC_MAX, description="主题")',
        "tests/test_server_hardening.py -k validation_errors_are_said_in_chinese",
    ),
    (
        "s33-3 config：环境变量不再过区间夹逼（TALKSCRIPT_MAX_TOKENS=100 曾静默生效）",
        ROOT / "app/config.py",
        "    bounds = _ENV_NUM_BOUNDS.get(name)\n    if bounds and not (bounds[0] <= v <= bounds[1]):",
        "    bounds = None\n    if bounds and not (bounds[0] <= v <= bounds[1]):",
        "tests/test_env_bounds.py -k clamped",
    ),
    (
        "s33-4 fileio：os.replace 失败不再重试（Windows 上文件被占用就直接炸）",
        ROOT / "app/fileio.py",
        "        except OSError:\n            if attempt == _REPLACE_RETRIES - 1:\n                raise\n            time.sleep(_REPLACE_BACKOFF * (attempt + 1))",
        "        except OSError:\n            raise",
        "tests/test_transient_read_fault.py -k replace",
    ),
    (
        "s33-5 llm：Authorization 掩码不再吃 Bearer（Key 原样进日志）",
        LLM_F,
        'r"""(?ix)(authorization)\\s*["\']?\\s*[:=]\\s*["\']?\\s*(?:bearer\\s+)?\\S+""")',
        'r"""(?ix)(authorization)\\s*["\']?\\s*[:=]\\s*["\']?\\s*\\S+""")',
        "tests/test_llm_retry.py -k non_sk_shaped_key_in_authorization_header",
    ),
    (
        "s33-6 rules/*.md：15 秒档配额写回 40/15（与 pack.yaml 对不上，P1-30 那一格）",
        ROOT / "packs/_template/rules/duration.md",
        "| 15s | 70 | 15 | 47 | 8 |",
        "| 15s | 70 | 15 | 40 | 15 |",
        "tests/test_pack_yaml_integrity.py -k duration_md",
    ),

    # ── s34 组（2026-09-27 复审轮）：每条对应复审发现的那一处修复 ──────
    (
        "s34-1 jobs.js：失败卡退回读全局 meta.model（modelByJob 表被掏空）",
        JOBSJS_F,
        "modelByJob.get(jobId) || state.meta?.model || \"-\"",
        "state.meta?.model || \"-\"",
        "verify:review",
    ),
    (
        "s34-2 pipeline.py：阶段闸门退回「有 draft 就整段放过」"
        "（draft 路径仍渲染裸下标 stages.write，复审 P1-1）",
        PIPE_F,
        '    required = ["write"] if "draft" in stages else ["select", "write"]',
        '    required = [] if "draft" in stages else ["select", "write"]',
        "tests/test_pack_yaml_integrity.py -k with_draft_but_no_write",
    ),
    (
        "s34-3 pipeline.py：阶段闸门挪回作业线程（受理时不再同步验 → 回 200 + job_id，"
        "用户要等一整轮才看到失败，复审 P1-1 补充）",
        PIPE_F,
        "        _assert_stages_usable(pack)\n        jid = new_job_id()",
        "        jid = new_job_id()",
        "tests/test_pack_yaml_integrity.py -k missing_write_stage",
    ),
    (
        "s34-4 sessions.js：整组删除不再清屏（fnBody 判据能抓住吗？）",
        SESS_F,
        "  if (state.result && ids.includes(state.result.id) && !failed.includes(state.result.id)) {\n"
        "    setResult(null);\n    T.clearThread();",
        "  if (state.result && ids.includes(state.result.id) && !failed.includes(state.result.id)) {\n"
        "    setResult(null);",
        "verify:review",
    ),
    (
        "s34-5 ui.js：去掉 defaultPrevented 闸门（Ctrl+Enter 双触发）",
        UI_F,
        "    if (e.defaultPrevented) return;\n    if (mod && e.key === \"Enter\") {",
        "    if (mod && e.key === \"Enter\") {",
        "verify:review",
    ),
    (
        "s34-6 ui.js：停止的空窗反馈整段去掉（键在等后端回话时还写着「停止生成」）",
        UI_F,
        "  const stopping = canStop && state.stopping === true;",
        "  const stopping = false;",
        "verify:review",
    ),
    (
        "s34-7 settings.js：不可逆确认框里的「没法撤销」提醒去掉",
        SETTINGS_F,
        "没法撤销",
        "注意",
        "verify:review",
    ),
    (
        "s34-8 result.js：合规页签退回二分（passed 缺失时亮红，与横幅一屏两话）",
        RESULT_F,
        "  const ck = verdictKey(passState(ch));\n"
        "  const comply = VERDICT[ck];",
        '  const ck = passState(ch) === true ? "ok" : "no";\n'
        "  const comply = VERDICT[ck];",
        "verify:review",
    ),
    (
        "s34-9 settings.js：包详情那行又手写一遍「内容需人工校对」"
        "（PROOF.warn 之外的第二份 —— 这条是上面两条守卫真的报出来的）",
        SETTINGS_F,
        "`（草稿包：${PROOF.warn}后投产）`",
        '"（草稿包：内容需人工校对后投产）"',
        "verify:review",
    ),
    (
        "s34-10 words.js：STATE_LABEL.checking 又写回「代码校验中」（工程术语回界面）",
        WORDS_F,
        'checking: "校验中"',
        'checking: "代码校验中"',
        "verify:review",
    ),
    (
        # ⚠ 靶子打在 **index.html 的兜底**上，不是打在 main.js 上。
        #   把 main.js 的回填换成一个手写字面量，index.html 里那份兜底
        #   与真值本来就一字不差 —— 屏幕上什么都看不出来，键面与表在
        #   行为上仍然一致。那是**语义空转**，不是"守卫没守住"：
        #   变异检验认的是"改坏用户能看见的东西"，而那一改谁都看不见。
        "s34-11 index.html：兜底键面忘了跟表一起改（脚本没起来时读屏读到旧词）",
        HTML_F,
        'id="pi-undraft" title="人工校对完成后，去掉草稿标记">标记为已校对<',
        'id="pi-undraft" title="人工校对完成后，去掉草稿标记">标记为已校完<',
        "verify:review",
    ),
    (
        "s34-12 jobs.js：停止流程走完不把 stopping 交还（键永久卡在「正在停止…」）",
        JOBSJS_F,
        "  state.stopping = false;\n  if (err) {",
        "  if (err) {",
        "verify:review",
    ),
    (
        "s34-13 server.py：zip 上传不再分块判上限（大包会一路读进内存）",
        SRV_F,
        "        if len(buf) > MAX_ZIP_BYTES:\n"
        "                    raise HTTPException(400,\n"
        "                        f\"zip 太大（上限 {MAX_ZIP_BYTES // 1024 // 1024}MB）\")",
        "        if False:\n            pass",
        "tests/test_pack_import_api.py -k oversized_zip",
    ),
    (
        "s34-14 pipeline.py：抓取不再有收工闸门（点了停止，「正在停止…」挂到 8 个源全跑完）",
        PIPE_F,
        "                should_stop=lambda: self._stop_check(job))",
        "                )",
        "tests/test_intel.py -k cancels_a_fetch_midway",
    ),

    # ── s35 组（2026-09-27 第二轮复审的修复）─────────────────────────
    # 这一组全是**第二轮复审**发现的：前一轮的守卫自己也有 bug，
    # 以及守卫守着的实现自己也有洞。每条都对应那处发现。
    (
        "s35-1 sessions.js：删整组不再排除失败的那条（右屏与事实相反）",
        SESS_F,
        "  if (state.result && ids.includes(state.result.id) && !failed.includes(state.result.id)) {",
        "  if (state.result && ids.includes(state.result.id)) {",
        "tests/test_job_state_vocabulary_consistency.py -k abandoning",
    ),
    (
        "s35-2 sessions.js：删整组不再排除失败的那条（正在跑的那条）",
        SESS_F,
        "  if (state.job && ids.includes(state.job.id) && !failed.includes(state.job.id)) {",
        "  if (state.job && ids.includes(state.job.id)) {",
        "tests/test_job_state_vocabulary_consistency.py -k abandoning",
    ),
    (
        "s35-3 jobs.js：打开无产物那条时不再清 state.result"
        "（清屏判据的前提被破坏 → 删别的记录会误清右屏）",
        JOBSJS_F,
        "    setResult(null);\n    T.addUserMsg(state.sentTopic, { onEdit: onEditUserMsg });\n"
        "    const body = T.addAssistantMsg();\n    T.pushVersion(body, { id, state: r.state",
        "    T.addUserMsg(state.sentTopic, { onEdit: onEditUserMsg });\n"
        "    const body = T.addAssistantMsg();\n    T.pushVersion(body, { id, state: r.state",
        "verify:review",
    ),
    (
        "s35-4 pipeline.py：阶段闸门不再查 system/user_template"
        "（缺一格 → 烧完钱才 KeyError）",
        PIPE_F,
        '        gone = [k for k in ("system", "user_template")\n'
        '                if not str((cfg.get(k) or "")).strip()]',
        "        gone = []",
        "tests/test_pack_yaml_integrity.py -k stage_without_its_templates",
    ),
    (
        "s35-5 pipeline.py：_render_stage 不再查那一格能不能渲染"
        "（rewrite_segment 缺了就 KeyError）",
        PIPE_F,
        "        cfg = (pr.skill.get(\"stages\") or {}).get(stage)\n"
        "        if not isinstance(cfg, dict) or not str(cfg.get(\"system\") or \"\").strip() \\\n"
        "                or not str(cfg.get(\"user_template\") or \"\").strip():\n"
        "            raise PackBrokenError(\n"
        "                f\"行业包 {pr.pack.dir.name} 的「{stage}」阶段配不完整\"\n"
        "                \"（skill.yaml 的 stages 里这一格要写全 system 与 user_template）\")\n",
        "",
        "tests/test_pack_yaml_integrity.py -k stage_without_its_templates",
    ),
    (
        "s35-6 review.js：fnBody 退回定长窗口（两窗重叠 → 单条删除那半静默空转）",
        ROOT / "_verify/sections/review.js",
        "    const lines = src.slice(i).split(\"\\n\");\n"
        "    const out = [lines[0]];\n"
        "    for (let k = 1; k < lines.length; k++) {\n"
        "      // 下一个顶层声明 = 这个函数已经结束（两个函数挨着写的情形）\n"
        "      if (/^(export\\s+)?(async\\s+)?function\\s/.test(lines[k])) break;\n"
        "      out.push(lines[k]);\n"
        "    }\n"
        "    return out.join(\"\\n\");",
        "    return src.slice(i, i + 1800);",
        "verify:review",
    ),
    (
        "s35-7 review.js：切函数体那条前置自查被删掉（重叠就没人知道了）",
        ROOT / "_verify/sections/review.js",
        "  check(\"（前置）取函数体的切片没有越过下一个函数（否则会替别人交差）\",",
        "  if (false) check(\"（前置）取函数体的切片没有越过下一个函数（否则会替别人交差）\",",
        "verify:review",
    ),
    (
        "s35-8 result.js：硬伤后缀又手抄一份（页头与页签两个名字说同一个数）",
        RESULT_F,
        "  return n > 0 ? ` · ${n} 处硬伤` : \"\";",
        "  return n > 0 ? ` · ${n} 硬伤` : \"\";",
        "verify:review",
    ),
    (
        "s35-9 words.js：会话标题那一格被删（CHAT 的真值消失）",
        WORDS_F,
        "export const CHAT = {\n  title: \"新对话\",\n};",
        "export const CHAT = {\n  title: \"新会话\",\n};",
        "verify:review",
    ),
    # ── s36 组（第 29 批：热点排行 / 选题建议，需求方案三层落地的每一条验收）──
    # 跑这一组：PYTHONUTF8=1 .venv/Scripts/python.exe _verify/mutate.py -k s36
    # 阶段一 #1：S 的「没数据 ≠ 零竞争」
    (
        "s36-1 该细分没有 B站供给时 S 退回 0.0（把没证据印成零竞争、蓝海）",
        INTEL_F,
        "        S = round(sup_n[s] / max_sup, 3) if max_sup and sup_n[s] else None",
        "        S = round(sup_n[s] / max_sup, 3) if max_sup else None",
        "tests/test_intel_ranking.py -k S_is_none",
    ),
    # 阶段一 #2：归属要吃 seed
    (
        "s36-2 seed 不进归属字段表（§1.2 #4②：实盘那批空桶条目又落回空桶）",
        INTEL_F,
        'SEGMENT_FIELDS = ("seed", "keyword", "title", "desc")',
        'SEGMENT_FIELDS = ("keyword", "title", "desc")',
        "tests/test_intel_ranking.py -k seed",
    ),
    # 阶段一 #3：E 用首见兜底
    (
        "s36-3 热榜条目没有 published 时 E 不再用事件首见兜底（E 恒 null）",
        INTEL_F,
        "            if ev and ev.get(\"seen_before\"):",
        "            if False:",
        "tests/test_intel_ranking.py -k E_uses_first_seen",
    ),
    # 阶段一 #4：D 按事件数 + 聚类阈值
    (
        "s36-4 D 退回按条目数计新收录（跨源重复继续污染 D）",
        INTEL_F,
        "    new_n = {s: len([e for e in events",
        "    new_n = {s: len([it for it in items",
        "tests/test_intel_ranking.py -k two_platforms_same_event",
    ),
    (
        "s36-5 事件归并阈值调松（同一件事被拆成两条，来源多样性失真）",
        INTEL_F,
        "EVENT_JACCARD = 0.6",
        "EVENT_JACCARD = 0.95",
        "tests/test_intel_ranking.py -k cluster_regression_set",
    ),
    (
        "s36-6 包含关系那支去掉长度闸（两字词把整包吞成一个事件）",
        INTEL_F,
        "    if (len(a) >= EVENT_MIN_CONTAIN and b.endswith(a)) or \\\n"
        "       (len(b) >= EVENT_MIN_CONTAIN and a.endswith(b)):",
        "    if b.endswith(a) or a.endswith(b):",
        "tests/test_intel_ranking.py -k containment_branch_needs",
    ),
    # 阶段一 #5/#6：热点分与「hot 有人接」
    # ⚠ **后端变异的 selector 不许指 verify:topics**：界面那一组跑的是 verify.js 里
    #   手写的 INTEL_TODAY 桩数据，改 `app/intel.py` 它对不上 —— 实测三条"漏检"
    #   全是这个形状（s36-8 / s36-16 第一轮就被记成漏检）。后端口径指 pytest，
    #   只有**渲染层自己的写法**才指 verify:topics。
    (
        "s36-7 名次强度不参与打分（§3.2 的四根条只剩三根有数）",
        INTEL_F,
        '            "rank": (sum(strength) / len(strength)) if strength else None,',
        '            "rank": None,',
        "tests/test_intel_ranking.py -k hot_score_terms",
    ),
    (
        "s36-8 事件行不再带平台真实热度（§1.2 #1「hot 抓了没人接」复现）",
        INTEL_F,
        '            "hot": hot_now,',
        '            "hot": None,',
        "tests/test_intel_ranking.py -k platform_hot_is_numeric",
    ),
    (
        "s36-9 增速窗口不满也照样算（把 1 天历史当基线，爆发中天天见）",
        INTEL_F,
        "    if hot_now is None or len(hot_series) < baseline_days:",
        "    if hot_now is None:",
        "tests/test_intel_ranking.py -k velocity_and_breakout",
    ),
    (
        "s36-10 旧产物降级把「算不出」写成 0 分（三态塌回两态）",
        INTEL_F,
        '            "decay": 0.0, "growth": None, "hot_score": None, "status": "",',
        '            "decay": 0.0, "growth": None, "hot_score": 0, "status": "",',
        "tests/test_intel_ranking.py -k old_latest_json_without_events",
    ),
    # 阶段二 #1/#2：新源 + 健壮性
    (
        "s36-11 微博不再覆盖 Referer（实测 403，整源今天静默没了）",
        INTEL_F,
        "    data = ctx.get_json(WEIBO_URL, headers={\"Referer\": WEIBO_REFERER})",
        "    data = ctx.get_json(WEIBO_URL)",
        "tests/test_intel_ranking.py -k weibo_hot_needs_site_referer",
    ),
    (
        "s36-12 知乎退回 401 的死端点（§1.2 #6「401 之后永远失败」）",
        INTEL_F,
        '    "zhihu": ("https://api.zhihu.com/topstory/hot-list", "zhihu"),',
        '    "zhihu": ("https://www.zhihu.com/api/v3/feed/topstory/hot-lists/total", "zhihu"),',
        "tests/test_intel_ranking.py -k zhihu_board_moved",
    ),
    (
        "s36-13 永久 4xx 也走重试（一次 12 秒变两次，且掩盖「这源坏了」）",
        INTEL_F,
        "                if i + 1 >= self.attempts or _is_permanent_http_error(e):",
        "                if i + 1 >= self.attempts:",
        "tests/test_intel_ranking.py -k permanent_4xx",
    ),
    (
        "s36-14 重试不等待（退避只是注释里的承诺）",
        INTEL_F,
        "                self.sleep(wait)",
        "                pass",
        "tests/test_intel_ranking.py -k retries_once_with_backoff",
    ),
    (
        "s36-15 连续失败到闸值仍照常去试（界面说不出「今日跳过」）",
        INTEL_F,
        "        if h.get(\"skip\"):",
        "        if False:",
        "tests/test_intel_ranking.py -k skips_after_three_fails",
    ),
    (
        "s36-16 today() 不把 cooling 态透出（跳过被说成「已接入，今天没有命中」）",
        INTEL_F,
        '        return "cooling" if _cooling(spec.label) else "ok"',
        '        return "ok"',
        "tests/test_intel_ranking.py -k marks_a_cooling_source",
    ),
    # 阶段二 #5：低粉爆文 / 争议黄标
    (
        "s36-17 样本不足也发低粉爆文标记（§3.3 验收：不许恒显示）",
        INTEL_F,
        '            ev["underdog"] = False\n            continue',
        '            ev["underdog"] = True\n            continue',
        "tests/test_intel_ranking.py -k underdog_never_shows",
    ),
    (
        "s36-18 争议词写成读 banwords.soft 的口径不成立（这里直接不发标）",
        INTEL_F,
        "    if hits_contested(it, contested):",
        "    if False:",
        "tests/test_intel_ranking.py -k contested_words_mark",
    ),
    # 阶段三：额度 / 缓存 / 两道闸
    (
        "s36-19 intel_brief 不显式归类（靠默认分支「恰好对」，对账断言失去依据）",
        JOBS_F,
        'MODEL_JOB_KINDS = frozenset({"generate", "packgen", "intel_brief"})',
        'MODEL_JOB_KINDS = frozenset({"generate", "packgen"})',
        "tests/test_intel_brief.py -k explicitly_in_the_model_family",
    ),
    (
        "s36-20 briefing 不占模型额度（花钱的作业免费跑）",
        JOBS_F,
        '                         "storyboarding", "packing", "briefing"})',
        '                         "storyboarding", "packing"})',
        "tests/test_intel_brief.py -k briefing_state_is_wired",
    ),
    (
        "s36-21 当日缓存不再命中（重复点一次就再扣一次模型调用）",
        SRV_F,
        '        if hit["state"] == "fresh":',
        "        if False:",
        "tests/test_intel_brief.py -k returns_cache_without_starting_a_job",
    ),
    (
        "s36-22 事件键不再判形状（请求层的形状闸被摘掉，键直接拼成文件名）",
        ROOT / "app/schemas.py",
        "        if not is_event_key(v):",
        "        if False:",
        "tests/test_intel_brief.py -k rejects_a_malformed_key",
    ),
    (
        "s36-23 选题卡文件名留着冒号（Windows 非法字符，实测 WinError 87）",
        INTEL_F,
        '    return str(key).replace(":", "_") + ".json"',
        '    return f"{key}.json"',
        "tests/test_intel_brief.py -k windows_legal",
    ),
    (
        "s36-24 输出侧禁用词闸门退回「只标不摘」（命中 hard 的句子照样上屏）",
        INTEL_F,
        '            return "{{待补}}"',
        "            return t",
        "tests/test_intel_brief.py -k banword_gate",
    ),
    (
        "s36-25 生命周期阶段听模型的（同屏排行写爆发中、卡片写衰退期）",
        PIPE_F,
        '            payload["stage"] = str(event.get("stage") or payload.get("stage") or "")',
        '            payload["stage"] = str(payload.get("stage") or "")',
        "tests/test_intel_brief.py -k cannot_redecide",
    ),
    (
        "s36-26 默认面退回按来源（§3.5 定的是排行优先）",
        TOPICS_F,
        '  selectTopicsTab("rank", false);',
        '  selectTopicsTab("topics", false);',
        "verify:topics",
    ),
    (
        "s36-27 排行的 null 分项画成 0（「没数据」与「零分」在界面上同形）",
        TOPICS_F,
        '      + `<em class="sig-val">${has ? w : "—"}</em></span>`;',
        '      + `<em class="sig-val">${w}</em></span>`;',
        "verify:topics",
    ),
    (
        "s36-28 忽略事件只记一条成员键（明天另外两条来源又回来）",
        TOPICS_F,
        "  const keys = ev ? (ev.item_keys || []).filter(Boolean) : [key];",
        "  const keys = [key];",
        "verify:topics",
    ),
    (
        # 这条守的是一个**只有量像素才能发现**的失效：宽度走 CSSOM 写 --w，
        # 而"写了 sigBars 却忘了在渲染后挂宽度"在 DOM 上完全看不出来
        # （data-w 还在、title 还在、条子是零长）。内联 style 那条路更早就死了 ——
        # 本页 CSP（style-src 'self'）会整条丢掉它。
        "s36-30 渲染后不把宽度挂上 CSSOM（信号条全成零长空轨）",
        TOPICS_F,
        "  wireRank();\n  syncSigWidths(root);",
        "  wireRank();",
        "verify:topics",
    ),
    (
        "s36-31 推荐位不看象限（低热/没归属的题也被置顶）",
        INTEL_F,
        '        ev["featured"] = ev["quadrant"] == "高热高配"',
        '        ev["featured"] = True',
        "tests/test_intel_ranking.py -k quadrant_classifies",
    ),
    (
        "s36-29 选题卡输入块不声明「这是数据不是指令」（注入面敞着）",
        INTEL_F,
        '    return BRIEF_DATA_HEAD + "\\n" + "\\n".join(body) + "\\n"',
        '    return "\\n".join(body) + "\\n"',
        "tests/test_intel_brief.py -k stage_is_echoed",
    ),
    (
        "s36-32 选题卡 same_day 恒真（昨天的卡冒充今天生成过，stale 路径死掉）",
        INTEL_F,
        '    return {"state": "fresh" if same_day else "stale",',
        '    return {"state": "fresh",',
        "tests/test_intel_brief.py -k fresh_today",
    ),
    # ── s37 组（A-9 去AI味补遗落码，调研报告-20260928）──
    # 词表 schema 与回炉护栏：每条都对应 §七 P0/P1 的一个机制，摘掉任何一条
    # 都不会报错，只会让判据**静默变宽或变窄** —— 正是这组变异要拦的东西。
    (
        "s37-1 无来源归因的出处豁免被摘掉（「市场监管总局数据显示，2025 年…」也照报）",
        AI,
        "        window = ATTRIB_WINDOW if tid in ATTRIB_EXEMPT else 0",
        "        window = 0                        # MUTATED: 出处豁免不再生效",
        "tests/test_ai_tells.py -k unattributed",
    ),
    (
        "s37-2 cta_strict 被并入第二人称豁免（助手腔收尾恒被豁免、tell 永不响）",
        AI,
        '            if scope == "cta_only" and (re.search(r"[你您]", text) or "？" in text or "?" in text):',
        '            if scope in ("cta_only", "cta_strict") and (re.search(r"[你您]", text) or "？" in text or "?" in text):',
        "tests/test_ai_tells.py -k helper_closing",
    ),
    (
        "s37-3 `re:` 用在字面量类不再报出来（死词静默）",
        AI,
        "            if cat in first_sent_tells:",
        "            if cat not in first_sent_tells:",
        "tests/test_ai_tells.py -k regex_entries_outside",
    ),
    (
        "s37-4 弱 tell 门槛的 yaml 覆盖被忽略（包改了口径而分数照旧）",
        AI,
        "        min_n = self.thresholds.get(tid, WEAK_MIN)",
        "        min_n = WEAK_MIN                  # MUTATED: 覆盖值不生效",
        "tests/test_ai_tells.py -k per_tell_threshold",
    ),
    (
        "s37-5 词义限定的 unless 失效（「维保工单闭环」被当黑话报）",
        AI,
        '        for u in ((rules or {}).get(w) or {}).get("unless", ()):',
        "        for u in ():                    # MUTATED: 豁免短语一条都不看",
        "tests/test_ai_tells.py -k word_sense_qualifier",
    ),
    (
        "s37-6 对照句式的空档上限放宽成无限（长句里岔出去一句再补的「实则」也被算成对照反转）",
        AI,
        '_CONTRAST_GAP = r"[\\s\\S]{0,24}"',
        '_CONTRAST_GAP = r"[\\s\\S]*"              # MUTATED: 空档不设限',
        "tests/test_ai_tells.py -k contrast_frame",
    ),
    (
        "s37-7 结构类漏实现不再报（STRUCTURAL 加了名字而算法没写 = 静默不跑）",
        AI,
        "        unimplemented = [t for t in self.STRUCTURAL if self._structural_handler(t) is None]",
        "        unimplemented = []               # MUTATED: 漏实现无人知晓",
        "tests/test_ai_tells.py -k without_an_implementation",
    ),
    (
        "s37-8 全正则的词表类被误判成死 tell（lexicon_missing 没跟着 schema 改）",
        AI,
        "                                and not (self.lexicon.get(t) or self.lex_regex.get(t))]",
        "                                and not self.lexicon.get(t)]",
        "tests/test_ai_tells.py -k accepts_regex_frame_entries",
    ),
    (
        "s37-9 「没有词就不扫」又回到 first_sent 分派之前（全正则的开场禁区永不跑）",
        AI,
        "        words = self.lexicon.get(tid) or []\n        scope = LEX_SCOPE.get(tid, \"any\")\n        if scope == \"first_sent\":",
        "        words = self.lexicon.get(tid) or []\n        if not words:\n            return []\n        scope = LEX_SCOPE.get(tid, \"any\")\n        if scope == \"first_sent\":",
        "tests/test_ai_tells.py -k opening_ban_accepts_regex",
    ),
    (
        "s37-10 fact gate 整条摘掉（改丢了数字/文号没人说）",
        PIPE_F,
        "                    if rnd > 1 and prev_sections else [])",
        "                    if False and prev_sections else [])",
        "tests/test_fact_gate.py -k loses_a_fact_without_fixing",
    ),
    (
        "s37-11 fact gate 不再让位于合规（为了保一个数字留着硬违规）",
        PIPE_F,
        '            if lost and not report["passed"]:',
        "            if lost:",
        "tests/test_fact_gate.py -k compliance_beats_fact",
    ),
    (
        "s37-12 回炉块里的「六永不注入」被摘掉（过度矫正没有负面清单）",
        PIPE_F,
        "        lines.append(NEGATIVE_INJECTION_RULE)",
        "        pass                              # MUTATED: 负面清单不进回炉块",
        "tests/test_refine_feedback.py -k negative_list",
    ),
    (
        "s37-13 回炉点名退回「strong+weak 全点名列」（每轮都在动本来正常的段落）",
        PIPE_F,
        '        strong_ai = [h for h in ai if h.get("severity") == STRONG]',
        "        strong_ai = list(ai)              # MUTATED: weak 也进点名清单",
        "tests/test_refine_feedback.py -k only_names_strong",
    ),
    (
        "s37-14 成语表没配时那一格拿 0 冒充「一篇都没有」（没测≠很好）",
        AI,
        "        if idioms and spoken else None,",
        "        if spoken else 0,                 # MUTATED: 没配表也报 0",
        "tests/test_ai_tells.py -k shape_reports_six",
    ),
    (
        "s37-15 idioms 写成字符串不再被拦（会被逐字切成单字去数密度）",
        AI,
        "        if not isinstance(raw_idioms, list):",
        "        if False:                         # MUTATED: 形状检查摘掉",
        "tests/test_ai_tells.py -k shape_reports_six",
    ),
    (
        "s37-16 老产物缺 shape 时界面照样画表（空表加一排「—」，把没测说成测了）",
        RESULT_F,
        '  if (!s) return "";',
        "  s = s || {};                            // MUTATED: 没数也渲染一块表",
        "verify:",
    ),
    # ── s39 组（A-9 审查修复轮）：每条对应审查报告里的一个 P1/P2/P3 ──
    (
        "s39-1 fact gate 末轮拒收不再过一遍候选版（交付物形状崩，钱烧完才炸）",
        PIPE_F,
        "                    self._filter_alternatives(job, draft, p, ban, quota, tells, tolerance, rnd)\n"
        "                    return draft, revisions",
        "                    return draft, revisions",
        "tests/test_fact_gate.py -k rejected_last_round",
    ),
    (
        "s39-2 单字 unless 不再被拦（一条笔误就把整类 tell 静默打死）",
        AI,
        "                    if short:",
        "                    if False:             # MUTATED: 短豁免照收不误",
        "tests/test_ai_tells.py -k short_unless",
    ),
    (
        "s39-3 thresholds 白名单摘掉（no-op 的键被说成「换了口径」）",
        AI,
        "            if not eats:",
        "            if False:                     # MUTATED: 任何 tell 都收下这个覆盖",
        "tests/test_ai_tells.py -k thresholds_on_classes",
    ),
    (
        "s39-4 门槛的小数被静默截断（配 2.9 跑 2，与 recheck_rounds 的双标）",
        AI,
        "            if num != int(num):",
        "            if False:                     # MUTATED: 截断不留痕",
        "tests/test_ai_tells.py -k threshold_must_be",
    ),
    (
        "s39-5 出厂词表里「其实」的豁免被删（voice.md 鼓励、引擎扣分，两边又打架）",
        YAML_F,
        "    - 其实:\n        note: 堆在一起才算；「其实呢」是口语标记，不罚\n"
        "        unless: [其实呢]",
        "    - 其实",
        "tests/test_ai_tells.py -k do_not_punish_what_the_prompt",
    ),
    (
        "s39-6 交付物汇总行退回「处」（分数按条扣，读者反推不出来）",
        STORE_F,
        "{ai.get('strong', 0)} 条 · 轻微",
        "{ai.get('strong', 0)} 处 · 轻微",
        "tests/test_ai_tells.py -k script_md_has_no_internal_identifiers",
    ),
    (
        "s39-7 §2.1 表里同一条 tell 写两行不再报错（后一行静默改掉档位）",
        ROOT / "tests/test_aitells_alignment.py",
        "        assert tell not in out, (",
        "        assert True or tell not in out, (",
        "tests/test_aitells_alignment.py -k duplicate_row",
    ),
    (
        "s39-8 回炉反馈的兜底句又变成死代码（没问题可点时只剩两段改写规矩）",
        PIPE_F,
        "        if not lines:\n            lines.append(\"- 未通过校验，请按口语化与合规要求改写\")",
        "        if False:\n            lines.append(\"- 未通过校验，请按口语化与合规要求改写\")",
        "tests/test_refine_feedback.py -k says_why_when_it_can_name_nothing",
    ),
    # ── s40 组（第 29 轮修复：A-1 剩下的那头 + 两条"现役但没用例"的防线）──
    # 这一组的共同点：**改回去不会让任何既有断言变红**。前两条是满分通道与恒真命中，
    # 第三条是"配了正则条目却当字面量扫" —— 全都只在缺证据的那一侧坏。
    (
        "s40-1 占位密度的下限摘掉（每段恰好一个空的骨架稿又拿满分）",
        AI,
        "PLACEHOLDER_MIN_CHARS = 100",
        "PLACEHOLDER_MIN_CHARS = 0        # MUTATED: 空不需要正文来撑",
        'tests/test_ai_tells.py -k "one_placeholder_per_section or density_line"',
    ),
    (
        "s40-2 零宽正则匹配也算命中（re:[0-9]* 这种写宽的条目=每篇都报开场违规）",
        AI,
        "                if m and m.group(0):",
        "                if m:                      # MUTATED: 空匹配也算命中",
        "tests/test_ai_tells.py -k zero_width_regex",
    ),
    (
        "s40-3 字面量扫描不再跳过 re: 条目（正则被当成一串普通字符去扫正文）",
        AI,
        "    ws = sorted({w for w in words if w and len(w) >= 2 and not w.startswith(RE_PREFIX)},",
        "    ws = sorted({w for w in words if w and len(w) >= 2},  # MUTATED",
        "tests/test_ai_tells.py -k regex_entry_never_joins",
    ),
    # ── s41 组（第 30 轮覆盖率探针查出的"从没被执行过"的守卫，逐条补上证伪）──
    # 共同点：这些行在 958 条测试下 0 次覆盖 —— 改回去不会有任何断言变红，
    # 而它们守的都是"报得出来 / 不静默"这一类承诺。
    (
        "s41-1 句首重复的门槛从 ≥3 处降到 ≥2（两处同头就报 = 正常中文当套路）",
        AI,
        "        if n >= 3 and n / len(sents) > 1 / 3:",
        "        if n >= 2 and n / len(sents) > 1 / 3:   # MUTATED",
        "tests/test_ai_tells.py -k repeat_opening_reports",
    ),
    (
        "s41-2 句首重复的占比边界 >1/3 写成 ≥1/3（恰好三分之一也报）",
        AI,
        "        if n >= 3 and n / len(sents) > 1 / 3:",
        "        if n >= 3 and n / len(sents) >= 1 / 3:  # MUTATED",
        "tests/test_ai_tells.py -k repeat_opening_reports",
    ),
    (
        "s41-3 「声明了词表类却不给词」不再说出口（死 tell 伪装成人味 100 分）",
        AI,
        "        if self.lexicon_missing:",
        "        if False:                             # MUTATED: 空词表不再报",
        "tests/test_ai_tells.py -k without_words_is_announced",
    ),
    (
        "s41-4 unless 写成裸字符串就不再被接受（指南教的形态静默失效）",
        AI,
        "            if isinstance(unless, str):\n                unless = [unless]",
        "            if False:\n                unless = [unless]   # MUTATED",
        "tests/test_ai_tells.py -k forms_the_guide",
    ),
    (
        "s41-5 unless 里的空串不再被丢掉（空豁免短语会把所有命中赦了）",
        AI,
        "                keep = [str(x).strip() for x in unless if str(x).strip()]",
        "                keep = [str(x).strip() for x in unless]    # MUTATED",
        "tests/test_ai_tells.py -k forms_the_guide",
    ),
    (
        "s41-6 编译不了的正则条目被静默丢掉（包作者以为配了框架）",
        AI,
        "                        self.bad_entries.append(\n"
        "                            f\"lexicon.{cat} 的正则条目 {body!r} 编译不了（{e}）—— 已跳过\")",
        "                        pass                      # MUTATED: 静默丢掉",
        "tests/test_ai_tells.py -k broken_lexicon",
    ),
    (
        "s41-7 thresholds 写成列表时不再报，且在 .items() 上炸穿整条链",
        AI,
        "        if not isinstance(raw_th, dict):",
        "        if False:                             # MUTATED: 列表也当映射用",
        "tests/test_ai_tells.py -k broken_lexicon",
    ),
    (
        "s41-8 门槛 0 被收下（0 = 处处都报，而不是退回默认口径）",
        AI,
        "            if n < 1:",
        "            if False:                           # MUTATED: 0 也照收",
        "tests/test_ai_tells.py -k broken_lexicon",
    ),
    (
        "s41-9 配额插值算子被写死成下界（非表内时长一律拿上一档的字数）",
        ROOT / "app/checker.py",
        "        ratio = (duration - lo) / (hi - lo)",
        "        ratio = 0.0                           # MUTATED: 不外推也不插值",
        "tests/test_checker_structure.py -k interpolates_between_rows",
    ),
    (
        "s41-10 单行表当成多行表走（`keys[1]` 当场 IndexError，生成整条链失败）",
        ROOT / "app/checker.py",
        "        if len(keys) == 1:                      # 单行表：任何时长都取这一行",
        "        if False:                               # MUTATED",
        "tests/test_checker_structure.py -k scalar_rows_and_single_row",
    ),
    (
        "s41-11 时长低于最小档不再外推而是去取不存在的下界",
        ROOT / "app/checker.py",
        "        if duration <= keys[0]:",
        "        if False:                               # MUTATED",
        "tests/test_checker_structure.py -k interpolates_between_rows",
    ),
    (
        "s41-12 限定写成非字符串/非映射时整条词被丢掉（词表少一个词，无声）",
        AI,
        "        return word, None",
        '        return "", None                       # MUTATED: 词也一起丢了',
        "tests/test_ai_tells.py -k degenerate_inputs",
    ),
    (
        "s41-13 空词表不再早退（`re.compile(\"\")` 处处命中 = 一条 tell 突然每篇满命中）",
        AI,
        "    if not ws:",
        "    if False:                                   # MUTATED",
        "tests/test_ai_tells.py -k degenerate_inputs",
    ),
    (
        "s41-14 没有首句时不再收住（解包 None → TypeError，作业只留一句看不懂的失败）",
        AI,
        "        if not got:",
        "        if False:                               # MUTATED",
        "tests/test_ai_tells.py -k degenerate_inputs",
    ),
    (
        "s41-15 词表顶层不是映射时不再报，往下取键直接炸穿加载链",
        ROOT / "app/checker.py",
        "    if not isinstance(data, dict):",
        "    if False:                                   # MUTATED",
        "tests/test_checker_structure.py -k top_level_and_platform",
    ),
    (
        "s41-16 `platform:` 写成列表不再报（后面 .items() 在加载期抛 AttributeError）",
        ROOT / "app/checker.py",
        "    if platforms is not None and not isinstance(platforms, dict):",
        "    if False:                                   # MUTATED",
        "tests/test_checker_structure.py -k top_level_and_platform",
    ),
    (
        "s41-17 整张 quota_table 不是映射时不再报（校验函数自己在列表上 .items()）",
        ROOT / "app/checker.py",
        "    if not isinstance(table, dict):\n        return [f\"quota_table 必须是映射",
        "    if False:\n        return [f\"quota_table 必须是映射",
        "tests/test_checker_structure.py -k non_mapping_and_bad_duration",
    ),
    (
        "s41-18 Quota 对坏表不再早退（装配期崩，而不是把错误汇进 param_audit）",
        ROOT / "app/checker.py",
        "        if not isinstance(table, dict):\n            return",
        "        if False:                               # MUTATED\n            pass",
        "tests/test_checker_structure.py -k non_mapping_and_bad_duration",
    ),
    (
        "s41-19 时长键非数字不再记账（`六十:` 这种笔误整行静默消失）",
        ROOT / "app/checker.py",
        "            except (TypeError, ValueError):",
        "            except KeyError:                    # MUTATED: 认不下这一行",
        "tests/test_checker_structure.py -k non_mapping_and_bad_duration",
    ),
    (
        "s41-20 命中定位的每词上限失效（回炉清单与下划线越拉越长）",
        ROOT / "app/checker.py",
        "            while len(places) < max_per_word:",
        "            while True:                         # MUTATED: 上限不管了",
        "tests/test_checker_structure.py -k locate_hits_caps",
    ),
    (
        "s41-21 清单体不再要求序号连续（「第一…第三…第五…」这种分别举例也被当连排）",
        AI,
        "                if ch and (not seq or num[ch] == seq[-1] + 1):",
        "                if ch:                          # MUTATED: 跳号也累计",
        "tests/test_ai_tells.py -k consecutive_numbers",
    ),
    (
        "s41-22 平台落空说明不再去重（同一句账每扫一次多记一笔）",
        ROOT / "app/checker.py",
        "            if note not in self.errors:",
        "            if True:                            # MUTATED: 每次都追加",
        "tests/test_checker_structure.py -k two_platform_notes",
    ),
    (
        "s41-23 任何 `has no attribute` 都被说成行业包配置问题（§9.8 那条错误归因）",
        PIPE_F,
        "        if owner in _DATA_TYPES and attr in _DATA_METHODS:",
        "        if True:                                # MUTATED: 不看形状，一律怪包",
        "tests/test_engine_audit10.py -k attribute_error_blames",
    ),
    # ── s38 组（第 29 轮审查：落码后自查出的 15 条，四条 P1 全在后端读路径）──
    # 这一组的形状与 s36 不同：不是"少做了一步"，而是**同一句话在两处各说一遍**
    # （注释与实现、前端与后端、落盘时与读盘时）。这种缺陷不会报错，只会让
    # 屏幕上出现一个与证据相反的数 —— 所以每条都配一条能证伪它的断言。
    (
        "s38-1 增速基线退回「按快照数、含今天」（一天点三次重抓就凑满 7 天窗口）",
        INTEL_F,
        "        if hots and day and day != today:\n"
        "            cur = float(max(hots))\n"
        "            if day not in hot_by_day or cur > hot_by_day[day]:\n"
        "                hot_by_day[day] = cur",
        "        if hots:                          # MUTATED: 每份快照一个点，今天也算\n"
        "            hot_by_day[iso] = float(max(hots))",
        "tests/test_intel_ranking.py -k same_day_snapshots",
    ),
    (
        "s38-2 基线分母取全部历史（三周前的水位当上周均值，缓慢涨的题永远到不了档）",
        INTEL_F,
        "    window = hot_series[-baseline_days:]",
        "    window = hot_series                   # MUTATED: 不取最近 7 天",
        "tests/test_intel_ranking.py -k recent_days_not_the_whole_history",
    ),
    (
        "s38-3 盘上的数值字段不再校验（字符串进排序键 → /api/intel/today 500）",
        INTEL_F,
        "    if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float))):",
        "    if v is None:                         # MUTATED: 认不出来的形状原样留着",
        "tests/test_intel_ranking.py -k load_latest_survives_bad_numeric_shapes",
    ),
    (
        "s38-4 errors 不再是映射时不兜（today() 的 dict(errors) 抛 ValueError）",
        INTEL_F,
        '    if not isinstance(merged.get("errors"), dict):',
        "    if False:                             # MUTATED: 这一步不做",
        "tests/test_intel_ranking.py -k load_latest_survives_bad_numeric_shapes",
    ),
    (
        "s38-5 被跳过的那一轮算成一次失败（「连续 4 次抓取失败」而我们只试过 3 次）",
        INTEL_F,
        # 2026-10-05 重指：P2-8 修复把豁免从 cooling 单值扩成 cooling/off/unwired
        # 三态（三种行都没发过请求），本条变异随之改为"豁免收窄到只剩 off/unwired"
        # —— cooling 重新被当成一次失败，判据语义不变。
        '            if row.get("state") in ("cooling", "off", "unwired"):',
        '            if row.get("state") in ("off", "unwired"):'
        '   # MUTATED: 跳过也计一次失败',
        "tests/test_intel_ranking.py -k skipped_round_is_not_counted",
    ),
    (
        "s38-6 昨天落盘的 skip 今天继续说「今天先跳过」（今天一次都还没试过）",
        INTEL_F,
        "        return bool(last) and _snap_date(last) == today_str",
        "        return bool(h.get(\"skip\"))        # MUTATED: 读盘时不重新对日期",
        "tests/test_intel_ranking.py -k yesterdays_cooling",
    ),
    (
        "s38-7 归一化为空的标题仍用 sha1(\"\") 当锚（所有这类事件共用一个选题卡缓存）",
        INTEL_F,
        "    if not seed:",
        "    if False:                             # MUTATED: 空锚不退回 item_key",
        "tests/test_intel_ranking.py -k normalise_empty_do_not_share",
    ),
    (
        "s38-8 事件成员今天全被忽略仍留在榜首（点忽略、重进视图又回到原位）",
        INTEL_F,
        "        if keys and all(k in hidden for k in keys):",
        "        if False:                         # MUTATED: 排行面不按忽略摘事件",
        "tests/test_intel_ranking.py -k ignoring_every_member_drops",
    ),
    (
        "s38-9 空药丸的阶段兜成爆发期（还在涨还没爆的题，模型被喂一句「今天就拍」）",
        INTEL_F,
        '                   "fading": "fading", "": "rising"}',
        '                   "fading": "fading", "": "peak"}',
        "tests/test_intel_ranking.py -k without_a_pill_is_not_called_peak",
    ),
    (
        "s38-10 在飞的选题卡不再复用（同一件事点两次＝两次模型调用）",
        JOBS_F,
        "            if same is not None:",
        "            if False:                     # MUTATED: 不做同键去重",
        "tests/test_intel_brief.py -k in_flight_does_not_charge_twice",
    ),
    (
        "s38-11 排行面重排时不看沉底（按来源那面沉下去了，默认面仍稳在榜首）",
        TOPICS_F,
        "    if (Boolean(a[0].buried) !== Boolean(b[0].buried)) return a[0].buried ? 1 : -1;",
        "    // MUTATED: 沉底不参与排序",
        "verify:topics",
    ),
    (
        "s38-12 「已忽略 N」按今天的次数禁用（清单里还有历史条目时入口打不开）",
        TOPICS_F,
        "    ign.disabled = !((data && data.ignored_list || []).length);",
        "    ign.disabled = !today;",
        "verify:topics",
    ),
    (
        "s38-13 健康列把「一次都没试过」印成 0/1（没数据与零结论同形）",
        TOPICS_F,
        "    const tried = (h.ok || 0) + (h.fail || 0);",
        "    const tried = 1;                      // MUTATED: 永远当试过",
        "verify:topics",
    ),
    (
        "s38-14 没有出处的条目回退成 href=\"#\"（一颗永远没反应的假链接）",
        TOPICS_F,
        '  return /^https?:\\/\\//i.test(s) ? s : "";',
        '  return /^https?:\\/\\//i.test(s) ? s : "#";',
        "verify:topics",
    ),
    (
        "s38-15 不随这一屏水合缓存里的选题卡（切走再切回：卡片没了、文案说还没有）",
        TOPICS_F,
        "    if (ev.brief_card && !(briefState[ev.key] || {}).payload) {",
        "    if (false && ev.brief_card) {         // MUTATED: 不水合",
        "verify:topics",
    ),
    (
        "s38-16 出处的协议白名单放开（平台返回的 javascript: 变成一颗可点的链接）",
        TOPICS_F,
        '  return /^https?:\\/\\//i.test(s) ? s : "";',
        '  return s;                               // MUTATED: 什么形状都当能点',
        "verify:topics",
    ),
    (
        "s38-17 重渲之后不恢复「收起」那颗按钮的字（内容开着、键上说还没看过）",
        TOPICS_F,
        '      if (d) d.textContent = "收起";',
        "      // MUTATED: 只恢复展开，按钮字留在「看条目」",
        "verify:topics",
    ),
    # ── s39q 组（四象限散点，方案 §3.3 #6 散点那一半）──
    # 2026-10-05 P3：原来与上面的 s39 组（A-9 审查修复轮）整段撞号
    # （s39-1..5 各出现两次），「s39-3 红了」指不出是哪条 —— 重编号为 s39q-N；
    # `-k s39` 的子串匹配仍然两组全跑，行为不变。
    # 档线/点位/跨页/不进图四条判据各配一条变异；档位的"一份口径"在后端，那条指 pytest。
    (
        "s39q-1 today() 不再下发四象限档位（前端档线只能抄常数，重校准那天图与分档各画各的）",
        INTEL_F,
        '            "hot_quadrant_high": HOT_QUADRANT_HIGH,',
        '            "hot_quadrant_high": None,  # MUTATED: 档位不下发',
        "tests/test_intel_ranking.py -k quadrant_threshold",
    ),
    (
        "s39q-2 点位不走 CSSOM（--x 没人写，点全叠回兜底位置 —— 属性在、位置是假的）",
        TOPICS_F,
        '    d.style.setProperty("--x", String(s));',
        "    // MUTATED: 坐标没人写",
        "verify:topics",
    ),
    (
        "s39q-3 点图定位不翻页（目标行在另一页时永远找不到，图上的点点了没反应）",
        TOPICS_F,
        "  rankPage = Math.floor(idx / PAGE_SIZE);",
        "  rankPage = 0;  // MUTATED: 永远停在当前页",
        "verify:topics",
    ),
    (
        "s39q-4 算不出分的事件照画（「—」冒充 0 分落在最左，图在撒谎）",
        TOPICS_F,
        "  const scored = all.filter(e => e.hot_score !== null && e.hot_score !== undefined);",
        "  const scored = all.slice();  // MUTATED: 空分也画进去",
        "verify:topics",
    ),
    (
        "s39q-5 「高热」档线不画（四象限没了分界，高热低热看不出来）",
        TOPICS_F,
        "      ${(th === null || th === undefined) ? \"\"",
        "      ${true ? \"\"",
        "verify:topics",
    ),
    # ── s42 组（审查报告-20260930 的修复：5 条 P2 + thresholds 边角）──
    (
        "s42-1 M1 structural 档收尾句退回「一个字都不要动」（与「可以重排」自相矛盾复活）",
        PIPE_F,
        '        lines.append(NEGATIVE_TAIL.get(scope, NEGATIVE_TAIL["default"]))',
        '        lines.append(NEGATIVE_TAIL["default"])   # MUTATED: 不分档',
        "tests/test_refine_feedback.py -k negative_tail_follows",
    ),
    (
        "s42-2 M3' in-place 的人声注消失（「补回人声」重新诱导加句）",
        PIPE_F,
        "        if scope in VOICE_SCOPE_NOTE:\n"
        "            lines.append(VOICE_SCOPE_NOTE[scope])",
        "        if False:\n"
        "            lines.append(VOICE_SCOPE_NOTE[scope])   # MUTATED: 按档注消失",
        "tests/test_refine_feedback.py -k reined_in_for_in_place",
    ),
    (
        "s42-3 M3 no_specific 的正文判据退回只剥占位（画面里的数字重新买通 strong 豁免）",
        AI,
        "        body = _plain(full)",
        "        body = _PLACEHOLDER_RE.sub(\"\", full)   # MUTATED: 画面不剥",
        "tests/test_ai_tells.py -k ignores_picture_annotations",
    ),
    (
        "s42-4 M2 形态流退回手写窄标点表（`／` 重新被当成字灌进 mattr/3-gram）",
        AI,
        '_SHAPE_SPLIT = re.compile("[" + _PUNCT[1:-1] + "]*")',
        '_SHAPE_SPLIT = re.compile(r"[，。、？！：；…—·～\\n\\r\\t 　]*")   # MUTATED: 窄表',
        "tests/test_ai_tells.py -k matches_the_count_caliber",
    ),
    (
        "s42-5 M5 first_sent 字面条目带 note/unless 不再报「不生效」",
        AI,
        "                if spec and cat in first_sent_tells:",
        "                if False and cat in first_sent_tells:   # MUTATED: 不喊",
        "tests/test_ai_tells.py -k qualifiers_are_announced",
    ),
    (
        "s42-6 M5 re: 条目带限定不再报「不生效」（静默丢弃复活）",
        AI,
        "                if w.startswith(RE_PREFIX):\n"
        "                    if spec:",
        "                if w.startswith(RE_PREFIX):\n"
        "                    if False:   # MUTATED: 不喊",
        "tests/test_ai_tells.py -k qualifiers_are_announced",
    ),
    (
        "s42-7 E6 strong 的 contrast_frame 退回门槛口径（单次命中不报，strong 比弱 tell 更钝）",
        AI,
        "        min_n = (1 if self.sev(\"contrast_frame\") == STRONG",
        "        min_n = (WEAK_MIN if self.sev(\"contrast_frame\") == STRONG   # MUTATED",
        "tests/test_ai_tells.py -k strong_reports_a_single_hit",
    ),
    (
        "s42-8 E8 未声明 tell 的门槛重新「记门槛+印非默认口径」（no-op 被谎称换尺）",
        AI,
        # 2026-10-05 重指：E8 的判据从 contrast_frame 单类泛化到所有未声明 tell
        # （词表类同样该被告知"这条 tell 整个没跑"），锚点随实现更新。
        "            if sev_here is None:",
        "            if False and sev_here is None:   # MUTATED",
        "tests/test_ai_tells.py -k follow_the_severity",
    ),
    (
        "s42-9 E6 strong contrast_frame 的门槛照收（「不生效」不再说出口）",
        AI,
        "            if tid == \"contrast_frame\" and sev_here == STRONG:",
        "            if False and tid == \"contrast_frame\" and sev_here == STRONG:   # MUTATED",
        "tests/test_ai_tells.py -k follow_the_severity",
    ),
    (
        "s42-10 E7 thresholds 字符串形态退回被 float 静默收下（写错形态不说出口）",
        AI,
        "            if str_form:",
        "            if False:   # MUTATED: 不喊",
        "tests/test_ai_tells.py -k string_form_is_announced",
    ),
    # ── s43 组（审查报告-20260930 遗留 8：窄窗底边距死声明搬活）────────
    # 那条媒体规则曾在头部媒体块里死于源码顺序（基础规则在后，同特异性后者胜），
    # 声明的 12px 从未生效（computed 恒 24px）。2026-09-30 搬到基础规则之后才活，
    # verify 主流程窄窗段的「底边距降到 --s3」断言守它 —— 本变异把它改回 24，
    # 断言必须红。目标只能全量 verify：断言在主流程（无分组入口）。
    (
        "s43-1 选题两面窄窗底边距退回 24（死回源码顺序病：断言必须红）",
        CSS_F,
        "@media (max-width: 1120px) {\n"
        "  .topics-inner, .topics-src-inner { padding-bottom: var(--s3); }\n"
        "}",
        "@media (max-width: 1120px) {\n"
        "  .topics-inner, .topics-src-inner { padding-bottom: var(--s6); }\n"
        "}",
        "verify:",
    ),
    # s43-2/s43-3：改版新增的两条断言（四条左边界重合、排行右栏动作柱）。
    # 断言在 sections/topics.js，目标走 verify:topics。
    (
        "s43-2 页级标题区左沿离开页签文字（四条左边界重合必须红）",
        CSS_F,
        "  padding: var(--s6) var(--s4) 0;",
        "  padding: var(--s6) var(--s6) 0;",
        "verify:topics",
    ),
    (
        "s43-3 排行行右栏动作柱塌回全宽堆叠（分数与按钮回到内容行里）",
        CSS_F,
        ".rank-row { display: grid; grid-template-columns: minmax(0, 1fr) auto; column-gap: var(--s6); }",
        ".rank-row { display: grid; grid-template-columns: 1fr; column-gap: var(--s6); }",
        "verify:topics",
    ),
    # ── s44 组（审查报告-20261005 六路全方位审查的修复轮）──
    (
        "s44-1 P1-1 checker.PUNCT 的弯引号被吞复发（四字符静默消失，字数虚高回潮）",
        CHECK_F,
        'PUNCT = "[\\\\s，。、？！：；：\\u201c\\u201d\\u2018\\u2019\\u0022\\u0027（）《》〈〉【】…—·～*#|＿_／/,.!?;:-]"',
        'PUNCT = "[\\\\s，。、？！：；：\\u0022\\u0027（）《》〈〉【】…—·～*#|＿_／/,.!?;:-]"   # MUTATED: 弯引号消失',
        "tests/test_checker_structure.py -k quotes_are_not_speech",
    ),
    (
        "s44-2 P1-2 audience_slice 缺节退回整份复发（别的受众话术灌进 select/write）",
        KN_F,
        '        text = self.file_text("knowledge/audience.md")\n'
        '        return self._heading_body(text, _pick_heading_line(text, key))',
        '        return self.slice_heading("knowledge/audience.md", key)   # MUTATED: 退回整份',
        "tests/test_pack_layer_audit10.py -k mapped_but_section_missing",
    ),
    (
        "s44-3 P2-14 mutate 的 -k 0 匹配守卫拆掉（0 条跑过照样印全绿 + exit 0）",
        MUTATE_F,
        '    print(f"🔴 -k {_only!r} 一条都没匹配上（全表 {len(MUTATIONS)} 条）——"\n'
        '          "「0 条跑过、0 条不合格」的全绿是假的，直接退出")\n'
        '    sys.exit(2)',
        '    print(f"🔴 -k {_only!r} 一条都没匹配上（全表 {len(MUTATIONS)} 条）——"'
        '\n          "「0 条跑过、0 条不合格」的全绿是假的，直接退出")   # MUTATED: 守卫拆了',
        "tests/test_mutate_tool.py -k zero_match",
    ),
    (
        "s44-4 P2-15 子进程总闸超时拆掉（挂死占死整轮变异与锁）",
        MUTATE_F,
        '        return subprocess.run(cmd, capture_output=True, text=True, cwd=ROOT,\n'
        '                              timeout=timeout)',
        '        return subprocess.run(cmd, capture_output=True, text=True, cwd=ROOT)'
        '   # MUTATED: 没有总闸',
        "tests/test_mutate_tool.py -k kills_hung_commands",
    ),
    (
        "s44-5 P2-16 锁占坑退回非原子（O_EXCL 拆掉，双开都能过检）",
        MUTATE_F,
        "        try:\n"
        "            fd = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)\n"
        "        except FileExistsError:",
        "        try:\n"
        "            fd = os.open(LOCK, os.O_CREAT | os.O_WRONLY)   # MUTATED: 非原子\n"
        "        except FileExistsError:",
        "tests/test_mutate_tool.py -k acquisition_is_atomic",
    ),
    (
        "s44-6 P2-17 verify 桩的档线常量漂移（重校准只改引擎侧时门禁量旧世界）",
        VERIFY_F,
        "INTEL_TODAY.hot_quadrant_high = 60;",
        "INTEL_TODAY.hot_quadrant_high = 61;   // MUTATED: 桩与引擎漂移",
        "tests/test_intel_ranking.py -k stub_hot_quadrant",
    ),
    (
        "s44-7 P2 doBrief 生成中退回 hidden 盒（首次生成几十秒零反馈）",
        TOPICS_F,
        '  const st = briefState[key] || {};\n'
        '  if (st.payload || st.running) box.classList.remove("hidden");',
        '  const st = briefState[key] || {};\n'
        '  if (st.payload) box.classList.remove("hidden");   // MUTATED: 生成中不露面',
        "verify:topics",
    ),
    (
        "s44-8 P2 refreshBrief 的「看条目→收起」同步拆掉（同一行两个说法复发）",
        TOPICS_F,
        '  const d = row.querySelector(\'[data-act="detail"]\');\n'
        '  if (d) d.textContent = "收起";\n}',
        '  const d = row.querySelector(\'[data-act="detail"]\');\n'
        '  if (false && d) d.textContent = "收起";   // MUTATED: 不同步\n}',
        "verify:topics",
    ),
    (
        "s44-9 P2-5 bindActions 退回只绑第一颗（字幕面板的导出 SRT 回到死按钮）",
        RESULT_F,
        '    body.querySelectorAll(sel).forEach(n => { n.onclick = fn; });',
        '    var n = body.querySelector(sel); if (n) n.onclick = fn;   // MUTATED: 只绑第一颗',
        "verify:",
    ),
    (
        "s44-10 P2-4 closeOverlays 不再收口确认框（Promise 挂死 + 监听残留）",
        OVER_F,
        '    if (o.id === "confirm-dialog" && _activeConfirmDone) {\n'
        '      const d = _activeConfirmDone;\n'
        '      _activeConfirmDone = null;\n'
        '      d(false);          // 被外部收口 = 用户没确认 = 取消语义\n'
        '    }',
        '      if (false) {}   // MUTATED: 外部收口不说完',
        "verify:",
    ),
    (
        "s44-11 P2-7 check_base_url 恒假子句回潮（首尾空格放行并原样落盘）",
        SRV_F,
        '    if re.search(r"\\s", url) or url != (raw or ""):',
        '    if re.search(r"\\s", url) or False:   # MUTATED: 恒假子句回潮',
        "tests/test_models_list.py -k surrounding_spaces",
    ),
    (
        "s44-12 P2-8 source_health 记账错位回潮（off 记成功、unwired 记失败涨连击）",
        INTEL_F,
        '            if row.get("state") in ("cooling", "off", "unwired"):',
        '            if row.get("state") == "cooling":   # MUTATED: off/unwired 回到错位记账',
        "tests/test_intel_ranking.py -k never_counted_as_tried",
    ),
    (
        "s44-13 P2-9 B站业务码闸拆掉（HTTP 200 + code=-412 风控演成 ok，0 条）",
        INTEL_F,
        '        if code not in (None, 0):',
        '        if False:   # MUTATED: 业务码闸拆掉',
        "tests/test_intel_ranking.py -k business_error_code_fails",
    ),
    (
        "s44-14 P2-10 nan/inf 有限数闸拆掉（thresholds 配 .nan 在 int() 处炸作业）",
        AI,
        "            if not math.isfinite(num):",
        "            if False:   # MUTATED: 非有限浮点直通 int()",
        "tests/test_ai_tells.py -k nan_and_inf",
    ),
    (
        "s44-15 P2-11 summary 的 display:flex 拆掉（折叠箭头回到永不旋转）",
        CSS_F,
        ".banner.why > summary { cursor: pointer; list-style: none; display: flex; align-items: center; gap: var(--s2); }",
        ".banner.why > summary { cursor: pointer; list-style: none; }   /* MUTATED: flex 拆掉 */",
        "verify:",
    ),
    (
        "s44-16 P2-12 slugify 首部保留符回潮（下划线行业名建出隐形包）",
        PACKGEN_F,
        '    return s.lstrip("_.")',
        '    return s   # MUTATED: 首部 _/. 回潮',
        "tests/test_packgen_claim.py -k never_produces_the_hidden",
    ),
    (
        "s44-17 P2-13 pack_shape_error 的选项重复闸拆掉（列表全绿、点生成才 409）",
        KN_F,
        "            if isinstance(opts, list):\n"
        "                seen_opts: set[str] = set()",
        "            if False and isinstance(opts, list):   # MUTATED: 重复闸拆掉\n"
        "                seen_opts: set[str] = set()",
        "tests/test_pack_yaml_integrity.py -k sees_duplicate_options",
    ),
    (
        "s44-18 P2-14 首次导入的兜底拆掉（搬入失败留半截包进 list_packs）",
        PKIMP_F,
        "        try:\n"
        "            shutil.move(str(tmp_pack), str(dst))\n"
        "        except Exception:\n"
        "            if dst.is_dir():\n"
        "                shutil.rmtree(dst, ignore_errors=True)\n"
        "            elif dst.exists():\n"
        "                dst.unlink()\n"
        "            if dst.exists():\n"
        "                raise PackImportError(\n"
        "                    \"导入落盘失败，且未能清干净残骸 —— 请手工删除 \"\n"
        "                    f\"{dst.name} 后重试（残骸会被当成一个坏包）\")\n"
        "            raise PackImportError(\"导入落盘失败（残骸已清掉，未留下半截包）—— 请重试；\"\n"
        "                                  \"反复失败时检查目标磁盘空间与杀毒软件\")\n"
        "        return False",
        "        shutil.move(str(tmp_pack), str(dst))   # MUTATED: 无兜无清理\n"
        "        return False",
        "tests/test_pack_import.py -k first_import_failure",
    ),
    (
        "s44-19 P2-15 adopted 消费方拆掉（收编的用户包被出厂更新静默覆盖）",
        PACKSEED_F,
        '            if stored.get("adopted") and stored.get("local") == local_hash:',
        '            if False and stored.get("adopted") and stored.get("local") == local_hash:'
        '   # MUTATED: adopted 没人接',
        "tests/test_packseed.py -k adopted_user_pack",
    ),
    (
        "s44-20 P2-16 数据损坏闸拆掉（CRC 损坏/加密 zip 逃逸成 500 与裸 traceback）",
        PKIMP_F,
        # 2026-10-11 重指：P2-16 闸 10-07 加宽为五类异常（zlib.error/NotImplementedError/OSError 补口），
        # 旧锚点（两参数版 except）0 匹配导致锚点门必红。new 仍为 ValueError：
        # BadZipFile/RuntimeError/NotImplementedError/zlib.error/OSError 都不是 ValueError 子类。
        "            except (zipfile.BadZipFile, RuntimeError, NotImplementedError,\n"
        "                    zlib.error, OSError) as e:",
        "            except ValueError as e:   # MUTATED: 闸拆了（BadZipFile/zlib.error 逃逸）",
        "tests/test_pack_import.py -k corrupt_entry_data",
    ),
    (
        "s44-21 P3 ranks 名次表退回首写覆盖（跨源同键条目与名次两张表对不上）",
        INTEL_F,
        "            ranks.setdefault(item_key(r), (i, len(rows)))",
        "            ranks[item_key(r)] = (i, len(rows))   # MUTATED: 后到源顶掉名次",
        "tests/test_intel_ranking.py -k first_sources_rank",
    ),
    (
        "s44-22 P3 抓取去重拆掉（同包双飞，同秒快照互相覆盖）",
        PIPE_F,
        '        inserted, existing = self.registry.add_if_room_dedup(\n'
        '            job, MAX_CONCURRENT_INTEL, INTEL_BUSY_STATES,\n'
        '            same=lambda j: j.kind == "intel" and j.params.get("pack") == pack.dir.name)',
        '        inserted, existing = self.registry.add_if_room_dedup(\n'
        '            job, MAX_CONCURRENT_INTEL, INTEL_BUSY_STATES, same=None)'
        '   # MUTATED: 去重拆掉',
        "tests/test_intel.py -k rides_the_running_job",
    ),
    # ── s45 组（2026-10-11 修复轮）：对应 审查报告-20261011 §3/§4 的三个修复 ──
    (
        "s45-1 P2-1 阈值 isfinite 闸拆掉（nan/1e999 字符串形态逃到文案的 int(num) 炸作业）",
        AI,
        "            if not math.isfinite(num):",
        "            if False and math.isfinite(num):"
        "   # MUTATED: 闸拆了（nan/inf 逃到 int(num)）",
        "tests/test_ai_tells.py -k thresholds_nan_and_inf",
    ),
    (
        "s45-2 P3-2 _user_modified 大小写折叠装回单向守卫（台账键含大写的反向漏掉）",
        PKIMP_F,
        '        lk = name.lower()\n'
        '        for k, v in rec.items():\n'
        '            if isinstance(v, dict) and k.lower() == lk:\n'
        '                stored = v\n'
        '                break',
        '        lk = name.lower()\n'
        '        if lk != name:   # MUTATED: 单向守卫装回（lk == name 时跳过折叠搜索）\n'
        '            for k, v in rec.items():\n'
        '                if isinstance(v, dict) and k.lower() == lk:\n'
        '                    stored = v\n'
        '                    break',
        "tests/test_pack_import.py -k reverse",
    ),
    (
        "s45-3 P3-3 复判处退回只读舍入容差（|dev| 落 (round(limit,1), limit] 窗口时「报告合格、反馈催改」换侧复活）",
        PIPE_F,
        '        limit = report.get("tolerance_pct_exact")\n'
        '        if limit is None:\n'
        '            limit = report.get("tolerance_pct")',
        '        limit = report.get("tolerance_pct")   # MUTATED: 退回只读舍入键',
        "tests/test_refine_feedback.py -k unrounded_tolerance",
    ),
    # ── s46 组（2026-10-12 修复轮）：A-1 输出形状兼容 / A-2 同日沿用 / B-1 可见化 ──
    # 跑这一组：PYTHONUTF8=1 .venv/Scripts/python.exe _verify/mutate.py -k s46
    # ⚠ 预案文档里写的组号是 s43 —— s43 已被 2026-09-30 复审轮占用，以 s46 为准。
    (
        "s46-1 schemas：标量不再归一（titles/keywords 收到单个字符串时整卡校验报废，真模型 4/4 复现）",
        SCHEMAS_F,
        "    if isinstance(v, str) and v.strip():\n"
        "        return [v]",
        "    if False:                             # MUTATED: 标量不再归一\n"
        "        return [v]",
        "tests/test_intel_brief.py -k tolerant",
    ),
    (
        "s46-2 intel：形状骨架从提示词里摘除（校验归一成兜底，治本的那一半没了）",
        INTEL_F,
        """    '8. 输出形状骨架：{"angles": [{"type": "解读", "titles": {"trend": ["跟热点款标题"], "evergreen": ["长尾款标题"]}, "hook": {"type": "疑问", "line": "能开口的第一句"}, "keywords": ["关键词"], "risk": "无", "fit": 0.5}]}'""",
        '    ""',
        "tests/test_intel_brief.py -k shape_hint",
    ),
    (
        "s46-3 score_topics：同日沿用被摘（重抓一次把上午还算得出的机会分全体抹成「—」，实测 168/168）",
        INTEL_F,
        "        D = round(new_n[s] / max_new, 3) if max_new else (carry_d or {}).get(s)",
        "        D = round(new_n[s] / max_new, 3) if max_new else None"
        "  # MUTATED: 同日沿用被摘",
        "tests/test_intel_ranking.py -k carried",
    ),
    (
        "s46-4 fetch_pack：同日门控行摘掉（永远不沿用 —— 正对照证明沿用真的接上了）",
        INTEL_F,
        '    same_day = bool(prev_fetched) and \\\n'
        '        _snap_date(prev_fetched) == _snap_date(now.isoformat(timespec="seconds"))',
        "    same_day = False   # MUTATED: 同日门控行摘掉（永远不沿用）",
        "tests/test_intel_ranking.py -k cross_day",
    ),
    (
        "s46-5 audit_pack：anti_ai 判据块摘掉（静默空注入回到「界面一字不说」）",
        KN_F,
        "        if not rule and len(dead) == 2:",
        "        if False:                             # MUTATED: 判据摘了",
        "tests/test_pack_layer_audit10.py -k anti_ai",
    ),
    (
        "s46-6 ui.js：包体警告不渲染（pack.warnings 回到全前端零消费）—— 断言在整网，指 verify:",
        UI_F,
        "    notes.push(...(pack.warnings || []).map(w => `包体警告：${w}`));",
        "    // MUTATED: 包体警告不渲染（pack.warnings 回到零消费）",
        "verify:",
    ),
    # ── s47 组（2026-10-12 真模型全链路实测的修复：env 覆盖语义 + 分镜尾零）──
    # 跑这一组：PYTHONUTF8=1 .venv/Scripts/python.exe _verify/mutate.py -k s47
    (
        "s47-1 config：env-only 合成条目摘除（models 为空时不再用 env 三件套合成第一条 ——"
        " 回到「测试连接 pong、生成 400 还没配置模型」的自相矛盾）",
        CONFIG_F,
        "        if env_key or env_url or env_model:",
        "        if False:                             # MUTATED: 合成条目摘了",
        'tests/test_models_list.py -k "synthesizes"',
    ),
    (
        "s47-2 config：恢复 base_url/model 的 env 无条件覆盖（界面切模型静默失效的原始病灶）",
        CONFIG_F,
        '        if key in ("base_url", "model"):',
        "        if False:                             # MUTATED: 守卫摘了，env 重新压过生效条目",
        'tests/test_models_list.py -k "never_overrides or yields_to"',
    ),
    (
        "s47-3 store：分镜表回退分支清尾零摘除（「0.0-9.6 秒」回到导出的 脚本.md）",
        STORE_F,
        "        return f\"{_num(tm['start'])}-{_num(tm['end'])} 秒\"",
        "        return f\"{tm['start']}-{tm['end']} 秒\"   # MUTATED: 尾零回来了",
        'tests/test_unit_vocabulary.py -k "trailing_zeros"',
    ),
    (
        "s47-4 server：env_override 退回「变量在场即 true」（条目 key 非空时界面仍挂"
        "「当前由环境变量 TALKSCRIPT_API_KEY 覆盖」的说谎注释 —— 修复记录-20261012 遗留①）",
        SRV_F,
        '                "env_override": cfg.env_key_used}',
        '                "env_override": True                           # MUTATED: 恒 true',
        'tests/test_models_list.py -k env_override',
    ),
]

# 子集运行：`python _verify/mutate.py -k UI` 只跑名字里含 UI 的那几条。
# 全量一轮 40+ 条要二十多分钟（每条都要起一次 pytest 或整个界面回归网），
# 改一处 UI 时没必要把 A/B 线那几十条也重跑一遍。
_only = ""
if len(sys.argv) >= 3 and sys.argv[1] == "-k":
    _only = sys.argv[2]

# ── -k 匹配守卫（P2-14，2026-10-05）──────────────────────────
# `-k` 打错一个字（或中文经 shell 被 mangle）会 0 匹配：循环一条都不跑，
# 结尾照印「共 0 条变异，0 条不合格」并以 0 退出 —— 一份"全部跑过、全部没跑"
# 的全绿报告。README 记过这起事故，但当时只修了调用方（subprocess 传 list），
# 工具本身对 0 匹配至今没有守卫。这里补上：0 匹配 = 用法错误 = exit 2。
# ⚠ 必须放在**拿锁之前**：用法错误连锁都不该拿（否则排查一次手滑要等持锁
#   进程跑完；这里自己跑自己的子进程测试时也会跟外层的锁相撞）。
if _only and not any(_only in name for name, _p, _o, _n, _s in MUTATIONS):
    print(f"🔴 -k {_only!r} 一条都没匹配上（全表 {len(MUTATIONS)} 条）——"
          "「0 条跑过、0 条不合格」的全绿是假的，直接退出")
    sys.exit(2)

# ── 崩溃恢复日志 ──────────────────────────────────────────────
# 变异期间被杀（SIGTERM / 断电 / Ctrl-C 没走到 finally）会在工作区留下一份
# **变异态**的代码，而且它长得跟正常代码一模一样 —— 下一次运行会把这份残留
# 当成"原始内容"备份起来，于是残留被永久保留。
# 实测就发生过：topics.js 里被追加了三份 `import('./jobs.js').then(m => m.send())`，
# 而每条变异都报"报红 OK"（因为断言确实红了，红的是残留）。
#
# 所以每次改文件之前先把原内容写进日志，还原成功后删掉它；
# 启动时若发现日志还在，说明上次没跑完 —— 先按日志还原，再开始。
JOURNAL = ROOT / "_verify" / ".mutate-journal.json"
# 并发锁：本仓库长期有多个会话同时干活（2026-09-23 实测：两个 mutate 进程
# 撞在一起，一个的 orig 读到另一个改了一半的文件，finally 还原又把文件写成
# 混合态 —— 症状是一条变异假绿 + 工作区留下删了列的 topics.js）。
# 锁文件用**写 PID + 探活**：进程死了（断电 / kill -9）锁要能自动失效，
# 否则一次意外就把工具永久锁死，那比没有锁更糟。
LOCK = ROOT / "_verify" / ".mutate.lock"


def _lock_held_by_living_process() -> bool:
    """锁文件里的 PID 还活着吗。读不出来 / PID 不存在都当作没锁。

    ⚠ **Windows 上不能写 `os.kill(pid, 0)`**：Windows 不支持 POSIX 信号语义，
    Python 在那里把任意 sig 值当作 TerminateProcess 用 —— 探活会**真的把那个
    进程杀掉**（实测：锁里写着当前 shell 的 PID，跑一次变异 shell 就没了）。
    改用 OpenProcess 只问"能不能打开"（SYNCHRONIZE 权限最小），拿到句柄即活着。
    """
    import os
    try:
        pid = int(LOCK.read_text(encoding="utf-8").strip())
    except Exception:                       # noqa: BLE001
        return False
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        SYNCHRONIZE = 0x00100000
        k32 = ctypes.windll.kernel32
        k32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_bool, ctypes.c_uint32]
        k32.OpenProcess.restype = ctypes.c_void_p
        handle = k32.OpenProcess(SYNCHRONIZE, False, pid)
        if handle:
            k32.CloseHandle(ctypes.c_void_p(handle))
            return True
        return False                        # 打不开 = 进程没了（或拒绝访问）
    try:
        os.kill(pid, 0)                     # POSIX：信号 0 = 只探活不发送
    except ProcessLookupError:
        return False
    except PermissionError:                 # 进程在，但不属于当前用户
        return True
    except OSError:
        return False
    return True


def _acquire_lock() -> None:
    import os
    # P2-16（2026-10-05）：占坑必须**原子**。旧写法"检查→写"两步之间有窗口，
    # 两个 mutate 同时启动都读到无锁/死 PID、都写入自己的 PID、双双开跑 ——
    # 这把锁要防的就是这个事故（2026-09-23 实测假绿 + 工作区残留），而入口
    # 恰恰没原子化。O_CREAT|O_EXCL 把"看坑"与"占坑"合成一步：坑被占着才探活，
    # 活人拦下，死坑当场清掉重占。
    while True:
        try:
            fd = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if _lock_held_by_living_process():
                print(f"🔴 另一个变异进程正在跑（PID {LOCK.read_text().strip()}）—— "
                      "并发跑会互相踩文件（实测出过假绿 + 工作区残留），等它跑完再来")
                sys.exit(2)
            LOCK.unlink()   # 死坑：持有进程已经不在，清掉重占（下一圈 O_EXCL 必成）
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
        return


def _recover() -> None:
    if not JOURNAL.exists():
        return
    try:
        saved = json.loads(JOURNAL.read_text(encoding="utf-8"))
    except Exception as e:                          # noqa: BLE001
        print(f"🔴 崩溃恢复日志读不出来（{e}）：请手动核对工作区，再把它清空")
        sys.exit(2)
    if not saved:
        return                                      # 空对象 = 上次跑完了
    for rel, text in saved.items():
        p = ROOT / rel
        p.write_text(text, encoding="utf-8")
        print(f"⚠ 上次变异没跑完，已按日志还原：{rel}")
    # ⚠ 用**清空**而不是删文件：某些沙箱把删除工作区文件当成高危操作直接拦下，
    #   而"拦下"发生在这里的后果是整轮变异在第一条就停 —— 工具自己成了阻塞项。
    #   写空对象的效果一样（_recover 见到 {} 就当没有），而且不触发删除策略。
    JOURNAL.write_text("{}", encoding="utf-8")
    print("（恢复完成 —— 建议 git status 再确认一遍）")


# ── 输出编码检测（P3，2026-10-05）──────────────────────────
# verify: 组的失败判定要读子进程的 UTF-8 输出（中文 + ❌/✅）。缺省 cp936 环境
# 下 decode 必花 —— 输出被判成「工具错/基线不绿」，读者会去修"基线"，真相只是
# 环境变量没带。启动时显式检测，错就给可行动的提示（用法错误连锁都不该拿）。
def _check_output_encoding() -> None:
    enc = (getattr(sys.stdout, "encoding", "") or "").lower()
    if "utf" not in enc.replace("-", ""):
        print(f"🔴 本工具要读子进程的 UTF-8 输出（中文 + ❌/✅），当前 stdout 编码是 "
              f"{enc or '未知'} —— 请用 `PYTHONUTF8=1 python _verify/mutate.py …` 再跑")
        sys.exit(2)


_check_output_encoding()
_acquire_lock()          # 先拿锁再恢复：并发跑的工具互相踩文件比残留更毒
_recover()

# ── 基线检查 ────────────────────────────────────────────────
# 判据是「改回去必须红」，前提是「**没改的时候是绿的**」。
# 基线本身是红的时候（并发会话正在改同一个文件、或界面/用例基线真有欠账），
# 每条变异都会"报红" —— 看起来全过，其实一条都没守住。这种假绿最难发现：
# 输出里是清一色的「[报红 OK]」，跟真守住长得一模一样。
# 所以每个 selector 先跑一次**未变异**的基线，不绿就判「判定无意义」。
_baseline = {}


def _selector_cmd(selector):
    """selector → 子进程命令。⚠ 基线采样与变异执行**必须**跑同一个命令 ——
    原来是两份手抄（P3 2026-10-05），给一份加参数另一份不跟的话，
    "基线绿 → 变异必须红"的前提就被悄悄破坏。"""
    if selector.startswith("verify:"):
        kw = selector.split(":", 1)[1].strip()
        return [str(NODE), str(ROOT / "_verify/verify.js")] + ([kw] if kw else [])
    if selector.startswith("node:"):
        # node --test 的用例（desktop/*.test.js：打包接线/对话框脱敏）。
        # 退出码在这里是可靠的（没有 pytest 那种收尾清理撞沙箱的问题）。
        return [str(NODE), "--test", *shlex.split(selector[len("node:"):])]
    return [str(ROOT / ".venv/Scripts/python.exe"), "-m", "pytest", "-q",
            *shlex.split(selector)]


def _run_selector(selector):
    """跑一次 selector（未变异状态），返回 (输出, 退出码)。"""
    r = _run_cmd(_selector_cmd(selector))
    return (r.stdout or "") + (r.stderr or ""), r.returncode


# P2-15（2026-10-05）：两处 subprocess.run 原来都没有超时——pytest 或 verify.js
# 任意一次挂死（Chrome 僵死 / CDP 半开 / pytest 收尾卡住），整轮变异就无限期
# 停摆：锁文件里是活 PID（没人能并发接手），journal 里还留着**变异态**的工作区
# 文件；基线阶段挂死时甚至还没有 journal 兜底。"挂而不死"恰好落在 journal 与
# 死-PID 探活两个兜底之间。总闸按"工具错"处理，不让它无限等。
SUBPROC_TIMEOUT = 1800


def _run_cmd(cmd, timeout=SUBPROC_TIMEOUT):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, cwd=ROOT,
                              timeout=timeout)
    except subprocess.TimeoutExpired as e:
        class _TimeoutResult:
            pass
        r = _TimeoutResult()
        out = e.stdout
        r.stdout = out.decode("utf-8", "replace") if isinstance(out, bytes) else (out or "")
        r.stderr = f"\n[_run_cmd] 命令超过 {timeout}s 总闸被杀 —— 工具错，不是断言红"
        r.returncode = None
        return r


def _pytest_red(out):
    """pytest 这一轮**真的红了吗**？返回 (是否红, 一句依据)；跑都没跑成返回 None。

    ⚠ **绝不能看退出码**：pytest 收尾会清理 tmp 下的历史会话目录（一次四千多个
    文件），沙箱的「批量删除需确认」拦截正好在那一刻打断进程 —— 于是进度行是
    `..  [100%]`、一条 F 都没有，退出码却是 1。
    实测全量跑时 13 条基线就是这样被误判成"红"的，而单独重跑条条是绿
    （垃圾没攒到阈值，拦截不触发）—— 完全一样的命令，两种结论。
    """
    prog = re.search(r"^([.sxFEX]+)\s*\[\s*100%\]", out, re.M)
    fails = [ln for ln in out.splitlines() if ln.startswith("FAILED")]
    if prog is None:
        # 进度行没到 100% —— 但有 FAILED 行就说明确实红过（旧判据就是这个语义，
        # 保留：宁可判红也不要漏）。否则是"压根没跑成"，判不了。
        return (True, f"{len(fails)} 条 FAILED") if fails \
            else (None, "pytest 没跑成用例（进度行没到 100%）")
    if fails:
        return True, f"{len(fails)} 条 FAILED"
    if "F" in prog.group(1) or "E" in prog.group(1):
        return True, "进度行里有 F/E"
    return False, "进度行零 F/E"


def _baseline_with_retry(selector):
    """采一次基线。**第一次不绿就隔 2 秒再采一次**。

    并发会话正在提交时，采样那一瞬可能刚好落在"改到一半"的工作区上 ——
    实测连跑两轮都撞出 13 条「基线不绿」，而单独重跑那 13 条条条是绿的。
    重试能滤掉这类瞬时噪声，两轮都红才是真红。
    ⚠ 转绿时必须**说出来**：静默重试会把"刚才有人在改代码"这件事抹掉 ——
      那正是这次要让它可见的东西。
    """
    first = _run_selector(selector)
    ok, why = _is_green(selector, *first)
    if ok:
        return first
    print(f"  （基线第一次不绿：{why} —— 2 秒后重试一次，排除并发改动窗口）")
    time.sleep(2)
    return _run_selector(selector)


def _is_green(selector, out, rc):
    """这条 selector 现在是绿的吗？（返回 是否绿 + 一句依据）"""
    if selector.startswith("verify:"):
        kw = selector.split(":", 1)[1].strip()
        if kw and f"（分组：{kw}）" not in out:
            return False, f"分组没生效（输出里没有「分组：{kw}」）"
        m = re.search(r"(\d+)/(\d+) 通过", out)
        if not m:
            return False, "没读到「N/M 通过」结果行"
        return m.group(1) == m.group(2), m.group(0)
    if selector.startswith("node:"):
        m = re.search(r"^# fail (\d+)", out, re.M)
        if not m:
            return False, "没读到 node --test 的 # fail 行"
        return m.group(1) == "0" and rc == 0, f"node --test fail={m.group(1)}"
    red, why = _pytest_red(out)
    if red is None:
        return False, why
    return not red, why


# ── 启动即预检（第 29 轮加）────────────────────────────────────
# 表里任何一条锚点腐烂，**跑子集是看不见的**：漂移只有轮到它那一条才会现形，
# 而那可能要等几轮。22 条就是这么攒起来的（表现是跑到时记一句「旧串没找到 → SKIP」），
# 其中一条（s14-1）背后还压着一条已经降级成空转的判据。
# 所以动任何文件之前先把**整张表**扫一遍：有漂移就停在这里，一条都不跑、工作区不动。
# 判据与 `tests/test_mutation_anchors.py` 共用同一份函数（不另写一套口径）。
import importlib.util as _ilu
_spec = _ilu.spec_from_file_location(
    "_preflight_mutations", ROOT / "_verify" / "_preflight_mutations.py")
_pf = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_pf)
_drift = _pf.drifted(MUTATIONS)
if _drift:
    print(f"🔴 变异表有 {len(_drift)} 条锚点漂移 —— 先重指再跑（本次一条都没跑，工作区未动）：")
    for _line in _drift:
        print("   " + _line)
    print("   （只想跳过这道检查：直接看 tests/test_mutation_anchors.py 的判据怎么写的）")
    sys.exit(2)

bad = skipped = 0
for name, path, old, new, selector in MUTATIONS:
    if _only and _only not in name:
        skipped += 1
        continue
    # ⚠ 先确认基线是绿的：不绿的话下面那条「报红 OK」是假的 —— 它证明的是
    #   "工作区现在本来就是红的"，而不是"这条断言守住了"。
    if selector not in _baseline:
        _baseline[selector] = _baseline_with_retry(selector)
    b_ok, b_why = _is_green(selector, *_baseline[selector])
    if not b_ok:
        # ⚠ 只报"退出码 1"等于什么都没说 —— 全量跑时撞出过 13 条基线不绿，
        #   单独重跑那 13 条又条条是绿，没有失败内容就只能靠猜。
        #   把输出尾巴一起打出来，"为什么红"必须是看得见的。
        print(f"[基线不绿 !! 判定无意义] {name} —— 未变异时就已经是红的"
              f"（{b_why}，已重试一次），这条变异的「报红」什么都证明不了，先修基线\n"
              f"    ── 基线输出尾部 ──\n"
              + "\n".join("    " + ln for ln in _baseline[selector][0].splitlines()[-15:]))
        bad += 1
        continue
    orig = path.read_text(encoding="utf-8")
    if old not in orig:
        print(f"[SKIP] {name} —— 旧串没找到（变异没生效，不是断言报红）")
        bad += 1
        continue
    # 先把原内容落进崩溃恢复日志，再改文件 —— 顺序反了的话，改完到写日志之间
    # 被杀就是"残留 + 无日志"，正是这次踩到的那个坑。
    JOURNAL.write_text(json.dumps({str(path.relative_to(ROOT)): orig}, ensure_ascii=False),
                       encoding="utf-8")
    path.write_text(orig.replace(old, new, 1), encoding="utf-8")
    # 选择器两种写法：
    #   "verify:…" → 跑界面回归网（node _verify/verify.js），非零退出即报红。
    #                它的 check() 不抛异常，只在最后 exit(1)，所以判据只能是退出码。
    #                界面侧的断言同样要能证伪，否则"新加了一堆 check"只是看着热闹。
    #   其余       → 当成 pytest 的 -k 选择器。
    kw = selector.split(":", 1)[1].strip() if selector.startswith("verify:") else ""
    cmd = _selector_cmd(selector)
    try:
        r = _run_cmd(cmd)
        out = (r.stdout or "") + (r.stderr or "")
        if selector.startswith("verify:"):
            # ⚠ **分组真的生效了**才配信这个数：分组参数被无视而跑成整网时，
            #   输出是 353/357，会被读成"分组报红"，而真相是压根没按分组跑。
            #   verify.js 在分组模式下必打「（分组：x）」，没有这一行就是工具错，
            #   与"断言报红"是两回事 —— 混在一起就是静默假绿。
            if kw and f"（分组：{kw}）" not in out:
                print(f"[全绿 !! 工具错] {name} —— 分组没生效（输出里没有「分组：{kw}」），"
                      f"跑的很可能是整网：\n{out[-400:]}")
                bad += 1
                continue
            m = re.search(r"(\d+)/(\d+) 通过", out)
            reds = len(re.findall(r"^\s*❌", out, re.M))
            if m and m.group(1) == m.group(2):
                print(f"[全绿 !! 漏检] {name} —— 界面断言全过（{m.group(0)}）")
                bad += 1
            elif m and reds:
                # 「N/M 通过」这行只在**跑完**时打印（它在输出末尾），所以它 + 至少
                # 一条 ❌ 就是"网跑完了、确实有断言红"的正证据。
                # ⚠ 原来这一支排在下面那个 "Error" 启发式之后，于是**每一条
                #    verify:topics 变异都被误判成「工具错」**：topics 组的输出里
                #    天然带着 "TimeoutError: 连接超时" 这类**桩数据文本**
                #    （抓取失败源的活样本），拿整份输出里出现过 "Error" 当"网崩了"
                #    判据，把真红全吃掉了。判据换成"有没有跑完 + 有没有 ❌ 行"。
                print(f"[报红 OK] {name}（{m.group(0)}，{reds} 条报红）")
            elif "FAIL:" in out or "Error" in out:
                # 整网崩了（SyntaxError / 中途抛）—— 这**不算**报红：
                # 崩掉的网可能压根没跑到那条断言，拿它当"守住了"是自欺。
                print(f"[全绿 !! 工具错] {name} —— 回归网中途崩了，不是断言报红：\n{out[-400:]}")
                bad += 1
            else:
                print(f"[全绿 !! 工具错] {name} —— 没读到结果行，判不了：\n{out[-400:]}")
                bad += 1
            continue
        if selector.startswith("node:"):
            # node --test 的退出码是可靠的（没有 pytest 收尾清理撞沙箱的问题），
            # 但也要看一眼 # fail 行，把"用法错误/收集失败"与"用例红"分开。
            m = re.search(r"^# fail (\d+)", out, re.M)
            if m and m.group(1) != "0":
                print(f"[报红 OK] {name}（node --test {m.group(1)} 条失败）")
            elif r.returncode != 0:
                print(f"[全绿 !! 工具错] {name} —— node --test 非零退出但没有失败计数："
                      f"\n{out[-400:]}")
                bad += 1
            else:
                print(f"[全绿 !! 漏检] {name} —— node --test 全过")
                bad += 1
            continue
        # ⚠ 退出码 5 = "一条用例都没收集到"，用法错误也是非零 —— 直接当"报红"就是把
        #   「选择器写错了」误读成「断言守住了」。必须显式区分。
        #   （本工具第一版就是 `selector.split()`，把 `-k 'a or b'` 拆成了三个参数，
        #    pytest 报用法错误 → 退出码非零 → 假报红。改用 shlex。）
        # ⚠ **判据不能只看 `FAILED` 那几行**。实测（本工具第三版）：
        #   pytest 收尾时会清理 `tmp_path` 的历史目录（一次四万多个文件），
        #   那一下撞上沙箱的「批量删除」拦截 —— 进程在打印
        #   `==== short test summary info ====` 与 `FAILED …` **之前**就被打断，
        #   于是输出只剩进度行 `F.  [100%]`，而退出码仍是 1。
        #   只看摘要行 → 12 条**其实已经报红**的变异被读成"漏检"，
        #   而单独重跑同一批又是红的（不可复现），极容易误判成"断言是空的"。
        #   所以判据是两条一起看：进度行有没有跑完 + 退出码。
        red, why = _pytest_red(out)
        fails = [ln for ln in out.splitlines() if ln.startswith("FAILED")]
        if red:
            # 判据升级（2026-09-26）：不只看「红没红」，还核对**红的归属** ——
            # FAILED 的用例必须落在 selector 指定的文件里。曾经只判「有没有红」：
            # 改动波及了别的测试、或并发会话恰好提交了红的东西，都会被记成
            # 「报红 OK」—— 那证明的不是这条断言守住了。
            # 约定：pytest 型 selector 的第一个 token 是目标文件/目录。
            target = selector.split()[0]
            owned = [ln for ln in fails if target in ln]
            if fails and not owned:
                print(f"[报错 !! 归属不符] {name} —— 红的不是 selector（{target}）"
                      f"指定的用例，这条变异证明不了它的断言：\n"
                      + "\n".join("    " + ln for ln in fails[:5]))
                bad += 1
            else:
                print(f"[报红 OK] {name}（{len(owned or fails) or why} 条报红）")
        elif red is None:
            print(f"[全绿 !! 工具错] {name} —— {why}，看输出：\n{out[-400:]}")
            bad += 1
        else:
            print(f"[全绿 !! 漏检] {name} —— 选择器选中了用例但全过（{why}）")
            bad += 1
    finally:
        path.write_text(orig, encoding="utf-8")
        # ⚠ 还原必须**校验**：本工具第一版只写不查，实测丢过一次内容
        #   （topics.js 里「未接入 N」那一整段被变异删掉后没回来，而后续运行
        #    只报了一句 [SKIP] 旧串没找到 —— 代码静默少了一段，没人知道）。
        #   还原失败比变异失败严重得多：它是在改用户的工作区。
        if path.read_text(encoding="utf-8") != orig:
            print(f"🔴 还原失败：{path.name} 与变异前不一致！请立刻核对这个文件")
            bad += 1
        else:
            JOURNAL.write_text("{}", encoding="utf-8")
print("\n共 %d 条变异，%d 条不合格%s"
      % (len(MUTATIONS) - skipped, bad,
         ("（跳过 %d 条）" % skipped) if skipped else ""))
# 释放锁（atexit 兜底：中途抛异常 / 被 KeyboardInterrupt 打断也要放，
# 否则锁文件留着，下次 _lock_held_by_living_process 探活失败才自动失效 ——
# 那期间另一个人跑来会被误拒）。清空而不是删文件：沙箱会把删除工作区文件拦下。
import atexit
atexit.register(lambda: LOCK.write_text("", encoding="utf-8"))
sys.exit(1 if bad else 0)
