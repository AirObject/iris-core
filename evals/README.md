# 学习与召回评测

## 当前状态

2026-10-05 起，开发和评测的对话模型改为 glm-5.3-flash，embedding 仍为 doubao-embedding-vision，开发设备改为 macOS（见 DECISIONS.md 同日记录）。MiniMax-M3 时期的评测报告和执行记录已从仓库删除，原文保留在 git 历史中（main `2ee81d8`）。`evals/reports/` 目前为空：仓库里没有有效的评测结果，所有门槛都要在 GLM 上重新达到。

评测判分改由执行者所用的模型完成，不再调用对话模型（DECISIONS.md 2026-10-05）。评分说明不变；代码导出判分材料，并按执行者交回的判分计算指标。两次判分在互不可见的独立会话中完成，分歧取不利结论；用于门槛判定的判分，由没有参与该轮代码和提示词修改的执行会话完成。只在 macOS 上验证，暂不做 Windows 验证。

## GLM 重测计划

| 阶段 | 评测 | 代码 | 用途 |
| --- | --- | --- | --- |
| 一 | 学习，全部 86 段（GLM 双判） | main、PR #5 | 学习结果保留在检查点；GLM 判分只作预览 |
| 一 | 召回，默认配置 | main、PR #5 | 确认新设备和重新获取的 embedding 能复现以前的质量结果 |
| 一 | 性能，5 千／5 万条 | PR #5 | R10：prepare P95 ≤ 500ms |
| 一 | 执行者判分通道 | 独立分支 | 导出判分材料，按执行者判分计算指标 |
| 一 | 规划者隐藏召回集 | PR #5 | PR #5 的召回验收（第三轮已运行，需第四轮修复后复核） |
| 二 | 用执行者判分重新判第一阶段的学习结果 | 判分通道 | GLM 基线；PR #5 的学习对照 |
| 三 | 端到端 E001—E011，执行者双判 | 调度分支合入 PR #5 和判分通道之后 | M1：至少 9/11 |
| 四 | 学习，dev 迭代后执行者双判 | GLM 适配之后 | M1 三项学习门槛 |
| 五 | 规划者隐藏集：学习、端到端 | 第四阶段定稿的代码 | M1 最终验收 |
| 五 | 人工抽查至少 10%；第二家服务商对照 | 同上 | 设计 21.2、21.4 |

学习对照规则（在看到结果之前确定）：

- main 和 PR #5 的学习在同一时间段、用同一份配置运行。任一边出现因限流或超时而放弃的批次，这次结果作废，重跑。
- 以执行者重新判分的结果为准。PR #5 的记忆精确率、证据正确率、归属正确率中，任一项比 main 低 3 个百分点以上时，两边各再跑一次；两次平均后仍低 3 个百分点以上，判定 PR #5 影响学习。
- 召回只复现、不重新选参。质量指标与历史结果不一致时，先报告原因（例如 embedding 服务返回的向量有变化），由规划者决定是否重新标定。

阶段门槛要求全部公开样本和规划者隐藏集同时达到。公开语料在 GLM 阶段全部按 dev 使用；`learning_v2` 的历史 holdout 只作回归分组单列，不作 holdout 判定。

## 命令

在仓库根目录运行；Windows PowerShell 中命令相同，只是路径写法不同：

```bash
uv run iris models check
uv run iris eval learning --split dev --judge-runs 1
uv run iris eval learning --judge-runs 2
uv run iris eval recall
uv run iris eval recall --split dev --calibrate
uv run iris eval learning --corpus <外部 JSONL> --out <外部目录>
uv run iris eval learning --judge-mode external --split dev --corpus evals/learning_v3.jsonl --out <运行目录>
uv run iris eval learning-export --checkpoints <检查点指纹目录> --checkpoint-report <同次报告.json> --out <材料目录>
uv run iris eval learning-score --materials <材料目录> --judgments <第一轮目录> --judgments <第二轮目录> --judge-model <执行者模型名称> --out <报告目录>
uv run iris eval recall --corpus <外部 JSON 或 JSONL> --out <外部目录>
uv run python evals/benchmark_retrieval.py --out evals/reports
```

默认 `iris eval learning` 仍调用对话模型双判，`--judge-runs 1` 可作单判预览；预览不作门槛依据。学习最终评测必须提供两轮独立的外部判分；单轮只用于 dev 迭代或流程检查，不计算双判分歧率。学习评分 v3、语料和 M1 门槛保持不变。`--out` 位于仓库外时，JSON 的 `details` 保存每案例完整输入、全部记忆及来源、每次原判分和最终判分；仓库内报告保持汇总与抽查规模。

学习成功案例保存在 `data/learning-checkpoints/<指纹>`，外部报告的检查点在 `<out>/.learning-checkpoints/<指纹>`。模型预览只有源码、语料、模型端点／ID、判分次数都一致才复用已完成案例；外部模式的检查点只记录学习，不依赖之后的判分轮数，两种模式的检查点分开。失败案例重新执行，不按分数选择结果。报告列 `resumed_cases` 和源码指纹。请勿在评测运行期间编辑源码／迁移或重建当前虚拟环境。

### 外部判分流程与文件格式

1. **只学习**：`learning --judge-mode external` 按原节奏学习，完全不调用对话模型判分。完成后打印 `judging-materials-<时间>/manifest.json`；其父目录就是 `--materials`。不生成质量报告。`--judge-runs` 只适用于模型预览；外部模式的轮数由计分时提供的 `--judgments` 数量决定。
2. **已有学习免重跑**：`learning-export` 读取一个完整运行的全部编号检查点，保留该次学习的模型、源码指纹和语料摘要。旧检查点须用 `--checkpoint-report` 提供同次 JSON 报告；新检查点自带 `metadata.json`，可省略该参数。缺案例、来源不符或未完成时直接报错。导出目标必须为空目录；导出不改检查点。离线导出和计分都不读取模型配置、不进行模型调用。
3. **独立判分**：给每位判分者相同的 `scoring.md` 和 `cases/*.json`，只读其中的 `input` 作判断。不要提供旧报告、原检查点或另一轮判分。可用两个没有继承对方历史的独立会话／子代理，各自只获评分说明、材料和各自输出目录的访问权限；若子代理共享文件系统，需要由调用方隔离可访问的结果，不能只靠目录名称。两轮完成前不互相讨论或传递结果。用于门槛的执行会话不得参与该轮代码和提示词修改；本任务自己的小样本单判仅检查流程。
4. **保存每轮**：每轮新建一个目录，将材料中的 `round-template.json` 复制为该轮的 `manifest.json`，再按材料清单的 `judgment_file` 为每个案例保存一份 JSON（例如 `0000.json`）。两轮目录各自完整，不能把一个目录重复传入来充当双判。程序校验材料绑定和文件完整性；它不能证明两个不同目录中的判分来自独立会话。
5. **计分**：`learning-score` 传一次 `--judgments` 是单判，传两次是双判；必须通过 `--judge-model` 声明执行者模型名称。先汇总所有缺失、长度、类型、ID／顺序或材料指纹错误，全部校验通过才生成报告。正常名字、alias／联系类型、必需主体的确定性检查与旧路径共用；双判仍正向项取 AND、forbidden 取 OR，确定性否决之前的原始分歧仍列出。

所有文件使用 UTF-8。推荐把运行、材料、判分和报告放到仓库外；`run.json` 包含完整学习记录，不应提交。材料目录结构如下：

```text
materials/
  manifest.json          # 格式版本、evaluation、run 元数据、案例映射及文件指纹
  scoring.md             # 冻结 scoring_v3 原文
  cases/0000.json         # 单案例材料；编号按原语料顺序，案例 ID 单独保存
  run.json               # 计分所需学习记录与调用统计，供程序生成原格式报告
  round-template.json    # 复制到各轮目录后命名为 manifest.json
round1/
  manifest.json          # {"materials_sha256": "材料清单中的 SHA-256"}
  0000.json              # 对应案例的一轮判分
round2/
  manifest.json
  0000.json
```

每份案例材料是 `{format_version: 1, evaluation: "learning", case_id, corpus_sha256, source_sha256, scoring_version, input}`。`input` 与旧 `_judge` 发给模型的用户输入逐字段相同：`messages`、`must`、`forbidden`、`links`、`goals`、`actual_memories`、`actual_links`、`actual_goals`、`actual_subjects`、`actual_aliases`、`target_segments`。导出只选取学习数据，移除原 `judge`、`judges`、原分歧／票数及对话模型判分调用，避免泄漏旧判分；保留完整学习输出和来源用于复核。

`corpus_sha256` 延续现有定义：对本次筛选后的案例列表做 UTF-8、`ensure_ascii=False`、`sort_keys=True` 的 JSON 序列化后计算 SHA-256，不是原 JSONL 文件的字节摘要。`source_sha256` 是学习时记录的源码指纹。清单的 `materials_sha256` 覆盖元数据以及各案例、学习记录、评分说明的文件摘要；计分会复核内容和输入一致性。不要修改导出的材料或指纹。

每个判分文件沿用 `scoring_v3` 输出结构，数组长度和顺序必须与输入一致，记忆 ID 必须逐项匹配，所有判定必须为 JSON 布尔值；不能写字符串 `"true"`、数字 `1`、null 或省略项。例如一个记忆、一个 must、没有联系和目标时：

```json
{
  "memory_results": [{"id": 1, "correct_worth": true, "forbidden": false,
                      "evidence_correct": true, "attribution_correct": true}],
  "fact_covered": [true],
  "link_covered": [],
  "goal_covered": [],
  "actual_link_correct": []
}
```

可以在顶层增加 `reasons` 或在记忆项增加 `reason` 记录逐项理由；计分忽略额外字段，原判分保留在仓库外报告的 `details[].external_judgments`，每轮判分摘要保存在 `judgment_rounds`。校验不为错误或缺失值补 false。冻结评分说明本身不作修改。

报告仍为 `learning-*.md` 与配套 JSON，沿用指标、门槛、分歧和固定种子的至少 10% 抽查清单；新增 `judge_mode`（`external`／`model`）、`judge_model`、`judge_runs`、`materials_sha256`。`chat_model` 继续表示学习模型。外部判分的 token、耗时和超时无法由本程序测量，调用统计只包含实际学习／embedding 调用；不能把其中的判分零调用读作外部模型零用量。报告比较必须语料摘要、评分版本、判分次数、学习模型和判分方式全部一致；旧报告未标方式时视为模型预览。

材料封装的 `format_version`、`evaluation`、案例映射和每轮指纹绑定不依赖学习的字段结构。后续端到端评测可沿用这套导出／独立判分／离线计分约定，用自己的 `input` 和 `e2e_scoring_v1` 校验器；当前命令只接受 learning，本次不修改端到端评测。

## 召回评测

`recall_v1.json` 在 `a8af9b1` 单独冻结：40 条固定记忆、38 条手写 dev 查询、8 条无答案。没有模板或脚本生成质量语料；性能脚本中的合成记忆只用于规模测试。召回评测直接入库，不经过学习，不调用生成模型，使用正式 Retrieval.prepare／search、去冗余和记录路径。

标准输入是 UTF-8 JSON：

```json
{
  "as_of": "2026-09-29T12:00:00+00:00",
  "memories": [
    {"id": "tea", "content": "小林喜欢桂花乌龙茶", "about": ["小林"], "kind": "偏好"},
    {"id": "meeting", "content": "活动晚上八点开始", "source_message_ids": ["recent-1"]}
  ],
  "queries": [
    {"id": "Q1", "split": "dev", "text": "给小林准备什么茶", "relevant": {"tea": 3}},
    {"id": "Q2", "split": "dev", "text": "活动晚上八点开始", "relevant": {},
     "recent_messages": [{"id": "recent-1", "speaker": "小林", "content": "活动晚上八点开始"}]}
  ]
}
```

也接受 JSONL：一行 `{"memories":[...]}` 后接每行一个查询，或使用 `record_type: "memory"|"query"` 区分记录。`--corpus` 完全替换仓库输入；`--split` 只筛选查询，固定记忆集不变。规划者应在外部目录运行隐藏验收，执行者不得寻找或读取其文件。

记忆字段：`id`、`content`，以及可选的 `about`（名字）、`speaker`（默认“我”）、`kind`、`stance`、`belief`、`importance`、`retention`、`event_time`、`world`、`lifecycle`、`tags`、`source_message_ids`。来源消息必须出现在某个查询的 `recent_messages` 中且 ID 全局唯一；未列来源的记忆视为有未提供的历史来源，不因当前消息剔除。

查询字段：`id`、`split`（缺省 dev）、`text`、`relevant`（ID 到 1—3 相关等级的映射；列表简写等价于全为 1）。`participants` 缺省空列表；`known_memory_ids` 排除宿主已有记忆；`recent_messages` 模拟真正入库的本入口消息，`recent_limit` 缺省 20。已在近期消息或宿主上下文中的信息不应出现在 relevant。可选 `mode:"search"` 及 `filters` 测试结构筛选；其延迟不计入 prepare P95。默认 `as_of` 固定为 2026-09-29 12:00 UTC，只影响排名时间权重。

Recall@8 是有答案查询的宏平均；nDCG@8 使用 `(2^grade-1)/log2(rank+1)`，以理想前八条归一化；无关误返率只统计无答案查询中是否返回非空。报告对照 jieba、trigram、两者各自的纯全文和融合向量方案。没有配置 embedding 时只输出两组全文结果，不冒充已经完成向量比较。

embedding 使用真实服务，按端点／模型／文本 SHA-256 缓存在 `data/recall-embeddings.db`；外部 --out 则将缓存放在外部。缓存中不含凭据。首次运行另记新 embedding 的调用用量与耗时；本地 prepare P95 扣除查询 embedding 等待，仍包含候选检索、去冗余和召回记录提交。外部 JSON 的 `details` 保存所有固定记忆、查询、相关性标签和完整返回，仓库内只保留结果 ID 与汇总。

`--calibrate` 只允许与 `--split dev` 一起使用。词项覆盖网格为 0.25／0.5／0.75／1.0；余弦网格为 0.35／0.45／0.55／0.65／0.75／0.85。按 `(Recall@8+nDCG@8)/2-无关误返率` 选择，平分依次看 Recall、nDCG 和误返率。命令只报告推荐设置，不写生产库。M2 参考值为 Recall@8≥0.85、nDCG@8≥0.75、无关误返率≤0.10，M1 不要求达到。

当前默认参数见 `src/iris/retrieval_defaults.json`，是在 doubao-embedding-vision 上按 dev 标定的。新数据库将默认配置保存到 `runtime_settings.retrieval`；现有数据库保留已有设置。更换 embedding 模型后须重新标定，未标定时只用全文检索并给出提示。两种 FTS 表都保留，便于可重复对照；运行检索只走设置中选定的一种。

## 性能方法

`benchmark_retrieval.py` 分别生成 5 千、5 万条合成记忆，固定随机种子、2048 维向量；每个规模／dtype 在新进程测量，预热 5 次、采样 60 次。报告包含直接 prepare、进程内 HTTP／JSON 路径、向量评分 P95、索引载入时长、RSS 增量和进程峰值工作集。查询 embedding 已预先生成；不计外部网络。float16 用 float32 累加，和 float32 在相同 20 条查询上比较前八名交集。

R10 要求 prepare 在本机的 P95 不超过 500ms（不含外部 embedding 网络）。测量反映本机单进程串行负载，不是并发吞吐承诺；换开发设备后须重测。

## 学习语料

`learning_v1.jsonl` 是 PR #1 的 36 段短对话，全部为 dev（原来的 12 段 holdout 已在 PR #1 审查中被分析）。

`learning_v2.jsonl` 是 PR #2 在学习提示词 v3 前冻结的 40 段完全虚构对话，共 1056 条消息、160 条 must。原分组为 24 段 dev、16 段 holdout（64 条 must）；20 段私聊各 18 条，12 段群聊各 34 条，8 段直播各 36 条。它由 `build_learning_v2.py` 的模板产生，历史 holdout 也已用于分析，按 2026-09-29 的决定只能作为回归材料，不能代表独立验收。文件和 split 保持原样，不要重新运行生成脚本来求分。

`learning_v3.jsonl` 是 PR #3 在学习提示词 v4 修改前单独提交冻结的 10 段手写 dev：136 条消息、28 条 must、5 条 links（3 alias、1 same_as、1 roleplay）、3 条 goals。各段分别写群聊、私聊或直播中的真实交流过程，未使用模板或脚本生成对话。覆盖同名不同账号、本人昵称／新账号名、角色承诺、多人共同反馈、保密请求、跨批更正，以及满 12 条的信息密集群聊；完整概况和冻结前分批边界复核记录见 [learning_v3_notes.md](learning_v3_notes.md)。三份仓库数据合计 86 段、1408 条消息、228 条 must。

覆盖：指令夹带、历史／后续段、跨批重复与修正、扮演、同名昵称、相对时间、角色承诺、敏感内容、低信息闲聊、多人转述、外部评价、刷屏、被引用作者同时在场、场景事件与行动结果交错、长消息和跨午夜相对时间。v2 每段至少四条 must；v3 按每段对话中实际应记的信息标注。真实标注错误可以修正，但须逐条记录原因；不能为提高成绩修改样本或放宽评分。

每行一个 JSON 对象：

- `id`、`split`、`tags`、`entry_type`；
- `messages`：时间顺序，每条含 `at`、`speaker`、`type`、`content`；可含 `account_id`、`quote_author`、`quote_author_account_id`、`quote_content`；
- `must`：事实、说话人、必需的 `about`、可选的 `about_optional`、立场；同名人可用 `speaker_account_id` 和 `about_account_ids` 明确账号；
- `forbidden`、`links`、`goals`：不能当作事实或“我”的认识的内容、应识别的人物联系和内部目标；
- `links` 每项是 `{ "a": "本人名字", "b": "另一名字", "kind": "alias" }`，kind 还支持 same_as 和 roleplay。alias 的 b 必须直接属于 a 的主体别名，不能生成另一个主体或误用其他联系类型。

评测按入口实际节奏分批：私聊与群聊每批最多 12 条和 4000 token，直播每批最多 4 条；正式路径默认有 4 条历史、4 条后续上下文。每段使用独立临时数据库，走正式接收、冻结和学习代码。评分固定为 `src/iris/prompts/scoring_v3.md`，每段同样输入独立判两次，不一致按不利结论计分并逐项列出。最终报告仍须人工抽查至少 10%。

评分 v3 使用设计 8.3 的最新定义：自身喜好按亲历，作品／人／事的评价按观点；兼容既有标注时，自身喜好的亲历／观点都算归属和覆盖正确，这不放宽其他判分项。明确归属于他人的说法不算 forbidden；当成事实或“我”的认识才算。判分输入包括实际主体 ID、账号和别名，代码额外拒绝把数组字符串判成人名、把错误类型的联系判作 alias 覆盖。事实覆盖须具备标注必需的说话人、about 和明确指定的账号；没有任何记忆满足这些必要条件时，不能判为覆盖，语义内容仍由模型核对。既有数值门槛不变。

不加参数时默认运行全部仓库样本，报告在 `evals/reports/`。`--corpus` 可与 `--split` 组合，不会拼入仓库样本，例如只检查手写 dev：`uv run iris eval learning --split dev --corpus evals/learning_v3.jsonl`。`--out` 将 Markdown、JSON 和前次报告查找都放到指定目录，不往仓库写报告。读取与写入均为 UTF-8。报告比较校验样本内容 SHA-256、评分版本、判分次数、学习模型与判分方式。

报告单列因长度截断的**学习批次数**（同一批的首轮／修正／重试只计一批），同时列学习与判分截断调用数、生成耗时，以及按学习／判分分列的超时调用数；JSON 保留生成调用的紧凑统计字段，包含 finish_reason 与输出 token。别名覆盖率＝正确覆盖的 alias 标注／alias 标注数；别名精确率＝判对的实际学习别名／实际学习别名数；分母为零记为不可计算，不显示成 100%。其他人物联系指标包含 alias，别名另行拆出。每段判两次，所有分歧按不利结论统计并列清单。

学习与判分的输出额度均为 16000 token，不限制额外推理长度。学习请求总超时 120 秒，评测判分及其修正请求 240 秒，报告记录两者。确定性名称／联系类型及必需主体检查只把不可能正确的正向判分改为 false；即使检查使两次最终结果相同，原模型判分的分歧仍列出。

独立隐藏验收集由规划者在仓库外维护，执行者不得寻找或读取。M1 的最终学习门槛须隐藏集和全部公开样本同时达到；公开样本即使全部达标，也不能据此宣布通过。后续真正新 holdout 仅用于最后一次评估；分析其失败后须转作 dev 并另写新集。

## v1 标注审计（2026-09-29）

- L025—L036：原 holdout 已在 PR #1 审查中被分析，逐段改为 dev；事实和其他标注未改。
- L018 第一条 must：小林说的是母亲养狗及狗名，即转述另一人的状况，立场由“亲历”改为“转述”。其余 must 按 8.3 逐条核对，未发现需更正的立场。
