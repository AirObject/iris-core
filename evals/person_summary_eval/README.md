# 人物摘要运行器（PS1）

只读取显式 `--corpus` 文件，不扫描目录，不默认读取 person_summary_v1。GD 的 dev 集须手写并单独冻结；本 PR 未读取或修改该集。仓库内测试仅为假模型机制夹具。完整材料、原始输出、隔离数据库和判分应写到仓库外 `iris-eval-artifacts`。

## 给 GD 的语料格式

UTF-8 JSON，顶层 `{"format_version":1,"cases":[...]}`。每个 case 是一个目标人物，在独立数据库载入全部声明的入口、主体和记忆；同名人物用不同 subject ID。建议按规格手写 8—10 个案例，每人 10—30 条记忆。

| case 字段 | 格式／要求 |
| --- | --- |
| `id` | 非空、全文件唯一的案例编号 |
| `subject_id` | 当前目标人物，在 `subjects` 中声明；排除 self／scene |
| `as_of` | 带时区 ISO 8601 时间；所有记忆元数据时间不晚于它 |
| `timezone`、`role_name` | 可选；默认 Asia/Shanghai、Iris |
| `subjects` | 非空数组：`{id,name,aliases?:[字符串],alias_source_entries?:{别名:入口ID或null}}`；名字允许重名，ID 不重名；self／scene 自动存在，勿重复声明 |
| `entries` | 非空数组：`{id,name?,kind?,visibility?,visible_in?:[入口ID]}`；kind 默认 group，可用 group/live/private；visibility 默认 shared，可用 shared/entry_only/entries |
| `memories` | 非空数组，格式见下表；顺序决定隔离库的数字 ID，模型本地 M 编号另由选材排序分配 |
| `must_cover` | 应覆盖要点字符串数组，全部适用，计分时不自动剔除 |
| `forbidden` | 禁止说法字符串数组，建议明确写出隐私、来源越界、泛化、转述、过期等边界 |
| `leak_probes` | 可选数组：`{memory_id:语料记忆ID,text:原文连续子串}`；只可指向入口限定记忆／其派生后代，text 必须出现于该记忆且不能出现在任何全局记忆中；请手写独特短语，避免通用词造成误报 |
| `initial_summary` | 可选短正文，用管理员编辑方式入库；无需为首版评测提供 |

| memory 字段 | 格式／默认 |
| --- | --- |
| `id`、`content`、`speaker`、`created_at` | 必填；id 为案例内唯一字符串；speaker 为已声明 subject ID 或 self/scene；时间带时区 |
| `about` | 主体 ID 数组，默认 [] |
| `source_entries` | 此记忆直接来源的入口 ID 数组，默认 []（无入口限制的初始设定）；运行器存为保留的来源范围根，不虚构原始消息 |
| `derived_from` | 语料记忆 ID 数组，默认 []；可引用数组中任意位置，来源限制递归继承，环会终止 |
| `kind`、`stance`、`belief` | 默认 事实、亲历、90；请按语义标记亲历／转述／推断等 |
| `importance`、`retention` | 默认 50、80，整数 0—100；用于确定性选材排序 |
| `lifecycle`、`revision` | 默认 active、1；生命周期可 active/forgotten/deleted，修订号为正整数 |
| `updated_at`、`first_confirmed_at`、`last_confirmed_at` | 默认 created_at；不把确认日期本身当多日互动依据 |
| `event_time`、`tags`、`world` | 默认 null、[]、real；时间语义和保密等标签供生成／检查读取 |

`aliases` 保持原字符串数组，新增的 `alias_source_entries` 完全可选。映射的键必须在 aliases 中，值必须是本例已声明的入口 ID 或 null；省略某别名的映射或填写 null，均按 shared／手工别名处理。例：`"aliases":["夜猫"],"alias_source_entries":{"夜猫":"local"}`，当 local 为 entry_only 时，“夜猫”的身份关联不能把全局记忆归给这个主体。运行器仅为显式来源建空正文的消息元数据行，不虚构原话；独立检查同时核对受限别名和仅通过受限别名关联的材料／依据 ID。

入口 `entries` 即使 visible_in 覆盖当前所有入口，也视为声明受限。请覆盖单入口 entry_only、混合来源、受限祖先、同名主体，以及全局敏感信息（后者由语义检查负责）。至少 8 条与目标有关的全局 active 记忆才能生成，受限或遗忘内容不计入这个门槛。must_cover／forbidden 和 leak_probes 不进入模型提示词；memories.tags 属于供生成和检查读取的记忆材料。

## 执行和外部判分

本回合只使用 `--fake-responses`；它为整个运行按调用顺序消费的 JSON 对象数组，不读取模型配置。真实运行须另等规划者通知，再省略该参数并使用规定的 IRIS_TEST_MODELS 路径。

```bash
uv run python -m evals.person_summary_eval run --corpus /外部/tiny.json --fake-responses /外部/fake-responses.json --out /Users/cassia/Local/Code/iris-eval-artifacts/PS1-20261011
uv run python -m evals.person_summary_eval score --materials /外部/运行/judging-materials --judgments /外部/round-a --judgments /外部/round-b --judge-model gpt-6-astra --out /外部/score
```

沿用 PR #7：format_version=1、evaluation=person_summary、manifest.json、run.json、scoring.md、cases/*.json、round-template.json。源码、语料、提示词、评分说明和假响应／模型身份指纹绑定材料。完整原始输出保存在 attempts.outputs_json，逐条检查在 profile.checks；不作基于分数的缓存挑选或重用。

每轮将 round-template.json 复制成该轮目录的 manifest.json，按材料清单的 judgment_file 存评分 JSON；详见 [scoring.md](scoring.md)。计分无需模型配置，不调用对话模型，严格校验编号／真布尔值／数组长度／文件路径／指纹。双判取不利结论并列分歧；候选和发布版计主指标，被拒版单列诊断。失败案例仍占覆盖率分母，不填造模型结果。unconfigured、paused、usage_limit、temporary_unavailable 等跳过，以及任何模型调用错误（包括生成后检查失败），都算未完成；实际输出经引用、长度、语义或 invalid_output 判定后才算完成。

确定性泄漏检查单独从语料来源图计算受限记忆，核对材料 ID、最终依据 ID，以及全部尝试中材料和原始输出的 leak_probes 子串。它不会被人工打分覆盖；同义改写仍须由外部判分的依据和 forbidden 检查捕获。报告列 probe 数和检查数，避免把没有标注探针误说成全面语义证明。
