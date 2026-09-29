# TalkScript 后端约定（细则，从 MEMORY.md 拆出）

> 本文件是权威细则。MEMORY.md 只留索引。**改 app/ 之前先读本文件。**

## 一、读 YAML：唯一口径

- **只用 `fileio.read_yaml_file(p) -> (dict, str)`**（配 `fileio.yaml_error_brief(e)`）。
  `config.read_config_file` 只是它的薄封装。**新加读 YAML 的地方不要自己写 try/except**
  —— 抄第二份就是下一次只修一半的原因（P1-2 与 P1-5/6 都是这么来的）。
- **一律走文件对象**（`with open(p) as f`），**不要** `p.read_text()`：
  后者会把 YAML 出错行正文带进 `str(e)`，而 config.yaml 那行很可能就是 `api_key: sk-…`。
  脱敏做在 `yaml_error_brief` 里（只取 `context`/`problem`/`problem_mark`），
  **不让安全性质取决于用了哪种读法**。
- **「文件不存在」`({}, "")` 与「存在但读不出来」（err 非空）必须分开**。
  前者是正常状态（`banwords.yaml` / `skill.yaml` / `private/*.yaml` 都是可选文件），
  后者才是错误。压成同一个 `{}` 就是「合规校验静默全过」的来源。
- `knowledge.read_yaml_cached` 返回 `(data, err)`，**err 一起进缓存**
  （否则一次生成读十几遍，日志被刷爆，真正的第一条反而看不见）。
- **返回值是元组而不是「顺手记条日志」是故意的**：让每个调用点**被迫**显式决定
  「读不出来怎么办」，不靠自觉。

## 二、坏包的两层检查（不是重复机制，别删其中一层）

- `PackBrokenError(PackError)` → HTTP **409**（包存在但内容坏了）；
  `PackError` → **404**（不存在）。server 里 `except PackError` 要**先捕子类**。
- **构造期** `Pack.__init__` 看 `info.pack_error` → **快速失败**。坏包不该进队列：
  `banwords.yaml` 是写稿+回炉阶段才读的，等那时才发现，`select` 的模型调用**已经付费**。
- **出口** `banwords_data` / `skill` / `private_facts` → **不变式**。构造期检查只代表
  「那一刻是好的」；作业要跑几十秒，期间包被改坏完全可能。
- 两条各有一条**独立的证伪测试**，缺一条那层就是不可证伪的死代码：
  - 构造期 → `test_generate_rejects_broken_banwords_before_burning_tokens`
  - 出口 → `test_banwords_guard_survives_a_change_after_construction`
- `pack_info` **不抛**（列表不能因为一个坏包整体消失），把错误挂
  `PackInfo.pack_error` 摊到界面 —— 包**留着并标出来**，既不静默降级也不静默消失。
  界面：下拉标「（损坏）」+ `#pack-err` 一行 warn 色说明（文案用「不可用」，
  因为坏法有两类，见下）。
- **列表宽松、生成严格**是这个模块的分工。改的时候别把两边拉平。

### 坏包有**两类**，修一类盖不住另一类

1. **YAML 语法错** → `read_yaml_cached` 的 `err` 非空。
2. **YAML 合法、结构不符合约定** → `version: v2`、`params.segment: 家用电梯`
   （字符串当映射用）、`quota_table: 3`（整数当映射用）。**`err` 是空的。**

第 2 类修复前（P2-7）的后果实测：`pack_info` 抛 `ValueError`/`AttributeError`
→ `list_packs` 的裸 `except` 咽掉 → **包从界面消失**（返回 `[]` 与「真没包」
长得一模一样）；而 `Pack()` 抛**裸异常 → HTTP 500**。
现在 `pack_info` 是**全函数**：记一条带 `exc_info` 的 WARNING + 返回带
`pack_error` 的降级 `PackInfo`；`Pack()` 转成 `PackBrokenError` → 409。

**通用问法**：凡 `try: ... except: 继续` 的地方，都要问
「这里的异常到底有几种来源」+「跳过之后用户看到什么」。
**「不崩」与「说清楚」是两条要求，别只满足前一条。**

## 三、产物落盘：字段必须「显式表态」

- `ScriptResult` 是**唯一**产物契约（`app/schemas.py`）。`_finalize` 组装 `raw`
  后必须先过 `pipeline.check_contract_complete(raw)`，再 `model_validate`。
- **为什么不能只靠 `model_validate`**：它卡「类型对不对」，不卡「字段有没有」。
  **少**给字段 → 静默取默认值（`quota_degraded` 就这么丢的）；
  **多**给字段 → 静默丢弃。两头都是静默降级。
- **`_normalize` 的产出每个键都要表态**（`pipeline.py` 顶部三张表）：
  `PERSISTED_PARAMS`（进 `result.params`）/ `PARAMS_ELSEWHERE`（另有落点）/
  `PARAMS_DROPPED`（明确不要 + 理由）。**新增键时
  `tests/test_quota_degraded_signal.py::test_normalize_keys_are_all_accounted_for`
  会报红** —— 别去改那份白名单了事，先想清楚这个键去哪。
- **不要改成 `ConfigDict(extra="forbid")`**：只堵「多」那一头，且会一并改掉
  `_run_rewrite_segment` 里「读旧产物再校验」的语义（升级路径的宽容度别顺手收掉）。
- 派生信号（降级/降级原因）**落盘不够，还得有人读**：界面横幅 + 导出的 `脚本.md`
  两处都要（md 是最终交付物，只在界面提示 = 导出后又变回不可见）。

## 四、`param_audit` 的三条时长降级路径（别说错）

`quota_table` 相关有三条**不同**路径，说成同一句会把包作者引到错的方向：

| 情形 | 真实行为（`Quota._row`） | 文案 |
|---|---|---|
| 整表缺失 | 时长×语速×0.95 估算 | 整个 quota_table 缺失：字数配额按 时长×语速 估算 |
| 只有一档 | **任何时长都取该档**（180 秒拿到 60 秒的配额） | quota_table 只有 N 秒一档 |
| 有表缺该档 | 按相邻档位插值 | 字数配额按相邻时长插值 |

`tests/test_param_audit.py` 的 `BASE` 是**两档**（30/60）—— 原来只有一档，
那条 `assert "插值" in ...` 其实一直在断言那句误导文案。

## 五、参数条下拉选项

来源 `packs/<行业>/pack.yaml` → `params.<key>.options` → 引擎 `/api/meta` →
`ui.js:renderQuickParams()`。`FRONT_KEYS = [segment, audience, duration, platform,
style, persona]` 进参数条，其余（`cta`）进设置「生成偏好」。**目前只能手改 YAML**。

各参数对未知值的容忍度不同 —— **改包新增选项后要同步补 `param_audit` 表**，
否则界面出现警示标记：

| 参数 | 未知值行为 |
|---|---|
| `audience` | 注入**整份** audience.md（上下文被稀释） |
| `persona` | 无影响，随便加 |
| `duration` | 配额按相邻键**插值**，要点数默认 3 |
| `style` | 语速默认 4.5 字/秒 |
| `segment` | 退到 `topics_map.通用` → **注入别的章节** |
| `platform` | 只走基础词表 → **平台红线校验等于关闭** |

`knowledge.param_audit()` 算出「哪些选项没定制」+ 人话原因，随 `PackInfo.param_audit`
下发，前端胶囊变警示色、菜单项带「!」。**注意它依赖 `pack.yaml` 解析成功**
（防线与数据同生共死）—— 所以坏包要靠 `pack_error` 单独报，**别让 `param_audit` 去兜**。

## 六、模型是一份列表（`config.py`，2026-09-16 已定）

```
models:                          # 至少一条，每条**自带**连接信息
  - {id, name, base_url, api_key, model}
active_model: <id>               # 必须指向存在的条目，否则退回第一条
llm:                             # 只剩生成参数
  {temperature, retries, timeout, max_tokens}
```

### 三条不能破的约定

1. **`AppConfig.llm` 是「当前生效的那一份完整配置」** —— 连接信息取自
   `active_model` 指的那条、生成参数取自 `llm` 段。这么组装是为了让
   `LLMClient` 与 server 里所有 `cfg.llm.xxx` 的消费方**一行都不用改**。
   别为了「结构更干净」把它拆成两半 —— 那会波及全项目每一处发请求的地方。
2. **原始条目 vs 生效条目是两份，不能混。**
   - `_parse_models()` → 原始（**空字段保持空**，不落回默认）
   - `_effective()` → 生效（空字符落回 `DEFAULT_MODEL`）
   - `load_raw_models()` → **写路径专用**，取原始条目
   写路径拿生效值写回去 = 把「用户没配过 base_url」这个事实抹掉，
   界面上「内置默认」的小标会凭空消失。**用户没做任何操作，提示却没了。**
3. **迁移只在读的时候发生、不写回文件。** 老 config.yaml（只有 `llm` 段）
   读时造一条；读一次和读十次结果必须一样，也不能出现「只是打开了一次应用，
   文件就被改了」。

### 几个具体口径

- **`llm_defaulted` 逐字段判**，不按「有没有 `models` 段」一刀切：
  用户只改模型名、没动请求地址时，请求地址仍是内置默认。
  同口径另出一份 `models_defaulted`（id → 该条还没配的字段），
  界面才能把「还没配过」标在**每一行**上，而不是只标当前那条。
- **`public_models()` 是唯一的密钥剥离点**：只下发 `api_key_set` 布尔。
  两处各剥一次的话，漏一处就是把明文送到渲染层（渲染层是网页，进去就等于进了 DOM）。
- **`upsert` 里「请求地址留空」= 保持不变**（与 `api_key` 同一语义）。
  要求必填的话，编辑一条还没配过地址的模型时（那个框本来就是空的）会因为
  「地址不能为空」被拒 —— 而用户压根没动过地址。
  **但「填了但不对」必须拦**（缺 `http(s)://` 前缀）。
- **删除至少保留一条**；删掉当前那条要自动落到剩下第一条 ——
  悬空的 `active` 会让 `load_config` 里那句 `next(...)` 直接 StopIteration，
  引擎起不来，而用户只是删了个模型。
- **`save_models()` 顺手清掉 `llm` 段里的 base_url / api_key / model**：
  留着会让文件里出现「同一份连接信息两处表示」，而其中一份是被忽略的 ——
  用户改它却不生效，属于看不见的失效。
- `/api/config` 下发 `defaults`（内置默认的地址与模型名），
  供渲染层「恢复默认」把默认值**显式**填进输入框 —— 前端不另抄一份。
