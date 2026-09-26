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
KN_F = ROOT / "app/knowledge.py"
PIPE_F = ROOT / "app/pipeline.py"
INTEL_F = ROOT / "app/intel.py"
JOBS_F = ROOT / "app/jobs.py"
SRV_F = ROOT / "app/server.py"
PACK = ROOT / "packs/elevator"
YAML_F = PACK / "ai_tells.yaml"
TOPICS_F = ROOT / "desktop/renderer/js/topics.js"
UI_F = ROOT / "desktop/renderer/js/ui.js"
PROMPTS_F = ROOT / "app/prompts.py"
CSS_F = ROOT / "desktop/renderer/styles.css"
VERIFY_F = ROOT / "_verify/verify.js"
SKILL_F = PACK / "skill.yaml"
TPL_SKILL_F = ROOT / "packs/_template/skill.yaml"
LLM_F = ROOT / "app/llm.py"
SEED_F = ROOT / "app/packseed.py"
IMPORT_F = ROOT / "app/packimport.py"


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
        "      $alt_guide\n",
        "\n",
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
        'INTEL_BUSY_STATES = frozenset({"queued", "fetching"})',
        "INTEL_BUSY_STATES = BUSY_STATES",
        "tests/test_intel.py -k does_not_consume_model_quota",
    ),
    (
        "B add_if_room 无视 states 参数（两族额度合成一族）",
        JOBS_F,
        "                   if same_quota_family(j.kind, job.kind)\n"
        "                   and j.state in states and not j.stranded) >= limit:",
        "                   if same_quota_family(j.kind, job.kind)\n"
        "                   and j.state in BUSY_STATES and not j.stranded) >= limit:",
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
        "    for (const g of data.groups || []) g.items = (g.items || []).filter(it => itemKey(it) !== key);",
        "",
        "verify:topics",
    ),
    (
        "UI「未接入 N」不渲染（未接入的平台变成界面上不存在）",
        TOPICS_F,
        '    chips.push(`<span class="m-chip" title="${esc(why.join("；"))}">未接入 ${off.length}</span>`);',
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
        "      $intel_block\n",
        "      # 变异：摘掉引用\n",
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
        '`没有匹配「${esc(query)}」的会话`',
        '`没有匹配「${query}」的会话`',
        "verify:xss",
    ),
    (
        "P0-2 参数胶囊 option 退回不 esc（pack.yaml 数据进 innerHTML = XSS）",
        UI_F,
        'const o = el("option", "", key === "duration" ? `${esc(String(opt))}s` : esc(String(opt)));',
        'const o = el("option", "", key === "duration" ? `${String(opt)}s` : String(opt));',
        "verify:xss",
    ),
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
                   and j.state in states and not j.stranded) >= limit:""",
        """            if sum(1 for j in self._jobs.values()
                   if j.state in states and not j.stranded) >= limit:""",
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
]

# 子集运行：`python _verify/mutate.py -k UI` 只跑名字里含 UI 的那几条。
# 全量一轮 40+ 条要二十多分钟（每条都要起一次 pytest 或整个界面回归网），
# 改一处 UI 时没必要把 A/B 线那几十条也重跑一遍。
_only = ""
if len(sys.argv) >= 3 and sys.argv[1] == "-k":
    _only = sys.argv[2]

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
    if LOCK.exists() and _lock_held_by_living_process():
        print(f"🔴 另一个变异进程正在跑（PID {LOCK.read_text().strip()}）—— "
              "并发跑会互相踩文件（实测出过假绿 + 工作区残留），等它跑完再来")
        sys.exit(2)
    LOCK.write_text(str(os.getpid()), encoding="utf-8")


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


_acquire_lock()          # 先拿锁再恢复：并发跑的工具互相踩文件比残留更毒
_recover()

# ── 基线检查 ────────────────────────────────────────────────
# 判据是「改回去必须红」，前提是「**没改的时候是绿的**」。
# 基线本身是红的时候（并发会话正在改同一个文件、或界面/用例基线真有欠账），
# 每条变异都会"报红" —— 看起来全过，其实一条都没守住。这种假绿最难发现：
# 输出里是清一色的「[报红 OK]」，跟真守住长得一模一样。
# 所以每个 selector 先跑一次**未变异**的基线，不绿就判「判定无意义」。
_baseline = {}


def _run_selector(selector):
    """跑一次 selector（未变异状态），返回 (输出, 退出码)。"""
    if selector.startswith("verify:"):
        kw = selector.split(":", 1)[1].strip()
        cmd = [str(NODE), str(ROOT / "_verify/verify.js")] + ([kw] if kw else [])
    elif selector.startswith("node:"):
        # node --test 的用例（desktop/*.test.js：打包接线/对话框脱敏）。
        # 退出码在这里是可靠的（没有 pytest 那种收尾清理撞沙箱的问题）。
        cmd = [str(NODE), "--test", *shlex.split(selector[len("node:"):])]
    else:
        cmd = [str(ROOT / ".venv/Scripts/python.exe"), "-m", "pytest", "-q",
               *shlex.split(selector)]
    r = subprocess.run(cmd, capture_output=True, text=True, cwd=ROOT)
    return (r.stdout or "") + (r.stderr or ""), r.returncode


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
    if selector.startswith("verify:"):
        # ⚠ `verify:` 后面那段是分组名，**必须真的传进去**。它以前只是个装饰：
        #   每条 UI 变异都完整跑一遍整网，7 条累积起来是整轮里最耗时的一段。
        kw = selector.split(":", 1)[1].strip()
        cmd = [str(NODE), str(ROOT / "_verify/verify.js")] + ([kw] if kw else [])
    elif selector.startswith("node:"):
        cmd = [str(NODE), "--test", *shlex.split(selector[len("node:"):])]
    else:
        cmd = [str(ROOT / ".venv/Scripts/python.exe"), "-m", "pytest", "-q",
               *shlex.split(selector)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, cwd=ROOT)
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
            if m and m.group(1) == m.group(2):
                print(f"[全绿 !! 漏检] {name} —— 界面断言全过（{m.group(0)}）")
                bad += 1
            elif "FAIL:" in out or "Error" in out:
                # 整网崩了（SyntaxError / 中途抛）—— 这**不算**报红：
                # 崩掉的网可能压根没跑到那条断言，拿它当"守住了"是自欺。
                print(f"[全绿 !! 工具错] {name} —— 回归网中途崩了，不是断言报红：\n{out[-400:]}")
                bad += 1
            else:
                print(f"[报红 OK] {name}（{m.group(0) if m else '退出码非零'}）")
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
