# 模板包（`packs/_template/`）

**这不是一份行业包，是建包用的骨架。** 名称以下划线开头 → `list_packs` 会跳过它，
所以界面上选不到、参数条里也不会出现。

`app/packgen.py` 建一个新行业包时：

| 来源 | 内容 |
|---|---|
| 从本包**原样复制**（`GENERIC_FILES`） | `skill.yaml`、`patterns/{hooks,growth,anti-ai-smell}.md`、`knowledge/voice.md`、`rules/*`、`compliance/{ad-law,platform}.md` |
| 从本包复制**空白模板**（`PRIVATE_TEMPLATES`） | `private/README.md` 与四个 yaml + `private/raw/README.md` |
| 本包的表 | `banwords.yaml` 的通用词、`pack.yaml` 的 `rate_by_style`/`quota_table`/`points_by_duration` |
| 模型生成 | `knowledge/{topics,audience,ideas,standards}.md`、`compliance/industry.md`、`pack.yaml`、行业增补词 |

## 一条铁律：本包不许出现任何行业事实

复制过去的每一份文件都会**每轮注入**给新行业的模型（`$hooks`、`$growth`、
`$voice_block`、`$facts_block`）。历史上模板缺失时会静默回退到某个成熟行业包，
于是新建的行业包里带着**别的行业**的完播建议和一整份别人的异议话术。
现在由 `tests/test_template_pack.py` 钉住：本包任何文件里出现具体行业名/行业事实即失败。

## 章节名是契约

`skill.yaml` 的 `stages.<阶段>.files` 用了三个 `路径#章节`：

- `compliance/industry.md#红线速查`
- `knowledge/ideas.md#选题库`
- `knowledge/standards.md#核心术语`

这三个标题在**生成的包里**必须由 `packgen._materialize` 写出来（模型不写它们）。
改标题就要同时改 packgen，否则新包的对应知识注入到的是空串（`#章节` 找不到时**不**退回整份）。
