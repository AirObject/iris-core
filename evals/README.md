# 学习与召回评测

## 当前状态

2026-10-05 起，开发和评测的对话模型改为 glm-5.3-flash，embedding 仍为 doubao-embedding-vision，开发设备改为 macOS（见 DECISIONS.md 同日记录）。MiniMax-M3 时期的评测报告和执行记录已从仓库删除，原文保留在 git 历史中（main `2ee81d8`）。`evals/reports/` 目前为空：仓库里没有有效的评测结果，所有门槛都要在 GLM 上重新达到。

判分仍与学习共用对话模型（设计 21.4）。换成 flash 模型后，判分偏宽的风险更大，人工抽查不能省；是否为判分单独配置模型，由用户另行决定。

## GLM 重测计划

| 阶段 | 评测 | 代码 | 用途 |
| --- | --- | --- | --- |
| 一（可并行） | 学习，全部 86 段双判 | main | GLM 基线，不判门槛 |
| 一（可并行） | 学习，全部 86 段双判 | PR #5 | 与 main 对照，判断召回改动是否影响学习 |
| 一（可并行） | 召回，默认配置 | main、PR #5 | 确认新设备和重新获取的 embedding 能复现以前的质量结果 |
| 一（可并行） | 性能，5 千／5 万条 | PR #5 | R10：prepare P95 ≤ 500ms |
| 二 | 端到端 E001—E011，双判 | 调度分支合入 PR #5 之后 | M1：至少 9/11 |
| 三 | 学习，dev 迭代后双判 | GLM 适配之后 | M1 三项学习门槛 |
| 四 | 规划者隐藏集：学习、召回、端到端 | 第三阶段定稿的代码 | M1 最终验收 |
| 四 | 人工抽查至少 10%；第二家服务商对照 | 同上 | 设计 21.2、21.4 |

第一阶段的对照规则（在看到结果之前确定）：

- 两次学习评测在同一时间段、用同一份配置运行。任一边出现因限流或超时而放弃的批次，这次结果作废，重跑。
- PR #5 的记忆精确率、证据正确率、归属正确率中，任一项比 main 低 3 个百分点以上时，两边各再跑一次；两次平均后仍低 3 个百分点以上，判定 PR #5 影响学习。
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
uv run iris eval recall --corpus <外部 JSON 或 JSONL> --out <外部目录>
uv run python evals/benchmark_retrieval.py --out evals/reports
uv run python evals/benchmark_retrieval.py --default-config --out evals/reports
```

学习最终评测必须使用默认的双判；单判只用于 dev 迭代，不计算双判分歧率。学习评分 v3、语料和 M1 门槛保持不变。`--out` 位于仓库外时，JSON 的 `details` 保存每案例完整输入、全部记忆及来源、每次原判分和最终判分；仓库内报告保持汇总与抽查规模。

学习成功案例保存在 `data/lc/<16位指纹>`，外部报告的检查点在 `<out>/.lc/<16位指纹>`。meta.json 保存并校验完整 SHA-256。只有源码、语料、模型端点／ID／维度、判分次数都一致才复用已完成案例；失败案例重新执行，不按分数选择结果。报告列 `resumed_cases` 和源码指纹。请勿在评测运行期间编辑源码／迁移或重建当前虚拟环境。

## 召回评测

默认运行三份公开语料，共 123 条固定记忆、128 条查询；每份语料在独立数据库中入库。直接使用固定记忆，不经过学习，不调用生成模型，走正式 Retrieval.prepare／search、去冗余和召回记录路径。

| 语料 | 来源 | 固定记忆 | 查询 | 无答案 | 原 split |
| --- | --- | ---: | ---: | ---: | --- |
| recall_v1.json | `a8af9b1` 单独冻结、手写 | 40 | 38 | 8 | 38 dev |
| recall_v2.json | `95549fc` 在检索改动前单独冻结、手写 | 50 | 66 | 16 | 44 dev、22 历史 holdout |
| recall_conversation_v1.json | `15cdd48` 在第二轮检索改动前单独冻结、手写 | 33 | 24 | 6 | 24 dev |

质量语料没有模板或脚本生成。v2 覆盖口语、改述、别名、相近名字、未知属性、参与者和近期消息去冗余；对话集覆盖私聊、群聊、直播，全部显式 `text: null`、`participants: null`，由近期消息推断当前问题和参与者。详见冻结时的 [recall_v2_notes.md](recall_v2_notes.md) 与 [recall_conversation_v1_notes.md](recall_conversation_v1_notes.md)。notes 保存当时的计划，当前选参方法以下文为准；不改语料或 notes 来适应新结果。

GLM 阶段全部公开样本按 dev 使用，命令和报告仍尊重冻结文件的 split，历史 holdout 仅作回归分组，不代表未见验收。PR #5 原选参只使用 v1 38 条、v2 44 条 dev 和对话集 24 条，共 106 条；原 v2 的 22 条历史 holdout 已分析，不参与选参。新设备阶段运行全部 128 条复现，不重新选参。规划者的隐藏集在仓库外，执行者不得寻找或读取。

标准输入是 UTF-8 JSON：

```json
{
  "as_of": "2026-09-29T12:00:00+00:00",
  "subjects": [{"name": "小林", "aliases": ["林子"]}],
  "memories": [
    {"id": "tea", "content": "小林喜欢桂花乌龙茶", "about": ["小林"], "kind": "偏好"},
    {"id": "meeting", "content": "活动晚上八点开始", "source_message_ids": ["recent-1"]}
  ],
  "queries": [
    {"id": "Q1", "split": "dev", "text": "给林子准备什么茶", "relevant": {"tea": 3}},
    {"id": "Q2", "split": "dev", "text": null, "participants": null,
     "entry_kind": "private", "categories": ["对话中准备"], "relevant": {},
     "recent_messages": [{"id": "recent-1", "speaker": "小林", "content": "活动晚上八点开始"}]}
  ]
}
```

也接受 JSONL：一行 `{"memories":[...]}` 后接每行一个查询，或用 `record_type: "memory"|"query"` 区分记录。`--corpus` 完全替换仓库输入；`--split` 只筛选查询，固定记忆集不变。外部集合同样尊重提供的 split。

记忆字段：`id`、`content`，以及可选的 `about`（名字）、`speaker`（默认“我”）、`kind`、`stance`、`belief`、`importance`、`retention`、`event_time`、`world`、`lifecycle`、`tags`、`source_message_ids`。顶层 `subjects` 支持主体名称与 aliases；本人别名写入 subject_aliases，不伪造关系记忆。来源消息必须出现在某个查询的 `recent_messages` 中且 ID 全局唯一；未列来源的记忆视为有未提供的历史来源，不因当前消息剔除。

查询字段：`id`、`split`（缺省 dev）、`text`（省略为空字符串，显式 null 使用近期五条消息）、`relevant`（记忆 ID 到 1—3 相关等级的映射，列表简写等价于全为 1）。`participants` 省略为空列表；显式 null 才从近期 20 条消息发送者推断，排除我与场景，按最近发言先后排列。此处省略字段的兼容行为与 HTTP prepare 的默认推断不同。`entry_kind` 缺省 group，可选 private／live；`categories` 是可重叠的分类标签。`known_memory_ids` 排除宿主已有记忆；`recent_messages` 模拟真正入库的本入口消息，`recent_limit` 缺省 20。已在近期消息或宿主上下文中的信息不应列入 relevant。可选 `mode:"search"` 和 `filters` 测试结构筛选，其延迟不计入 prepare P95。默认 `as_of` 固定为 2026-09-29 12:00 UTC，只影响排名时间权重。

Recall@8 是有答案查询的宏平均；nDCG@8 使用 `(2^grade-1)/log2(rank+1)`，以理想前八条归一化。两者计全部返回，包含人物要点。无关误返率只统计无答案查询是否有 `reason=relevant` 返回，不计人物要点。relevant 标注精确率为标注相关的 relevant 返回数／全部 relevant 返回数，跨查询合并计数；平均返回数仅作诊断。空分母显示不可计算，类别可重叠；v1 无分类标签，其无答案查询仍计入总体误返。报告按语料、split、类别列指标，逐条保存返回 ID 和 reason。

默认命令对照 jieba／trigram 的纯全文和混合向量四种方案，混合默认为 trigram。没有配置 embedding 时只输出全文结果，不能视为完成向量复现。embedding 使用真实服务，按端点／模型／维度／完整输入 SHA-256 缓存在 `data/recall-embeddings.db`；外部 --out 的缓存也在外部，不含凭据。预取与正式路径共用 prepare_query／embedding_text，新增请求单路、间隔至少 1 秒。首次运行另记新请求用量与耗时；本地 prepare P95 扣除查询 embedding 等待，包含检索、去冗余和召回记录提交。外部 JSON 的 details 另保存完整输入、记忆、标签和返回，仓库内只保留 ID 与汇总。

`--calibrate` 只允许 `--split dev`，报告完整网格，不写在用数据库。网格为 jieba／trigram × 向量绝对下限 {0.35, 0.45, 0.55, 0.65} × 相对比例 {0.75, 0.85, 0.95} × 向量权重 {0.5, 1, 2} × 前缀 {空, “为这个问题检索能回答它的个人记忆：”}，全文权重 1，共 144 个混合方案及两个纯全文对照。默认维度 2048；只有额外传入 `--compare-embeddings` 才比较 1024／2048 维。本次不运行这些选参选项。

选择规则先计算 `Q=(Recall@8+nDCG@8)/2`，距全网格最高 Q 不足 0.01 才算持平，恰差 0.01 不算。持平先取 relevant 标注精确率较高者，再取无关误返率较低者，再看 Q，完全相同按固定网格顺序；精确率无分母按 0 处理。不得逐项链式平分或只比较每个分词器／前缀的局部赢家，不从 Q 扣除误返。M2 参考值仍为 Recall@8≥0.85、nDCG@8≥0.75、无关误返率≤0.10；无答案拒绝留到 M2，本轮只报告。

当前冻结默认见 `src/iris/retrieval_defaults.json`：trigram、RRF k=60、2048 维 float32、向量权重 2／全文权重 1、绝对下限 0.35、相对比例 0.75，加上述问题前缀。无向量时降级 jieba。新数据库保存到 runtime_settings.retrieval，现有设置保留；更换 embedding 模型或维度须重新标定，未经标定只用全文。学习材料使用独立的 learning_retrieval 设置，不随回复选参变化。

## 性能方法

`benchmark_retrieval.py` 使用固定随机种子生成 5 千／5 万条合成记忆，不属于质量语料。每个规模／dtype 在新进程测量；点名与不点名各预热 5 次、采样 60 次。查询向量预生成，不计外部网络；包含 FTS、向量评分、候选锚点、去冗余、JSON 和召回记录提交。HTTP 路径为进程内 ASGI TestClient。

原命令 `uv run python evals/benchmark_retrieval.py --out evals/reports` 比较 1024／2048 维与 float32／float16，并在合成测试库中关闭向量绝对和相对截断；不点名显式传 text 和空 participants。float16 用 float32 累加，在相同 20 条合成查询上比较前八名交集。

复现 PR #5 原默认方法须加 `--default-config`：固定当前 2048 维 float32 和原截断，覆盖 5 千／5 万条、点名／不点名四组。不点名请求 `{}` 从近期消息推断查询及参与者；点名显式查询“参与者1的天文观測记录”，参与者仍推断。脚本中的 settings 只写合成测试库，不修改产品默认参数或在用数据库。

报告列 prepare P50／P95、HTTP P95、向量 P95、索引载入时长、索引块内存和 RSS。macOS 下脚本的 peak_working_set_mib 没有 Windows peak_wset 时退回采样末尾 RSS，不应当作真实峰值。R10 要求本机 prepare P95≤500ms，不含外部 embedding 网络；结果反映单进程串行合成负载，不承诺并发吞吐。

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

评测按入口实际节奏分批：私聊与群聊每批最多 12 条和 4000 token，直播每批最多 4 条；正式路径默认有 4 条历史、4 条后续上下文。每段使用独立临时数据库，走正式接收、冻结和学习代码。评分固定为 `src/iris/prompts/scoring_v3.md`，每段同样输入独立判两次，不一致按不利结论计分并逐项列出。模型自己判自己可能偏高，最终报告仍须人工抽查至少 10%。

评分 v3 使用设计 8.3 的最新定义：自身喜好按亲历，作品／人／事的评价按观点；兼容既有标注时，自身喜好的亲历／观点都算归属和覆盖正确，这不放宽其他判分项。明确归属于他人的说法不算 forbidden；当成事实或“我”的认识才算。判分输入包括实际主体 ID、账号和别名，代码额外拒绝把数组字符串判成人名、把错误类型的联系判作 alias 覆盖。事实覆盖须具备标注必需的说话人、about 和明确指定的账号；没有任何记忆满足这些必要条件时，不能判为覆盖，语义内容仍由模型核对。既有数值门槛不变。

不加参数时默认运行全部仓库样本，报告在 `evals/reports/`。`--corpus` 可与 `--split` 组合，不会拼入仓库样本，例如只检查手写 dev：`uv run iris eval learning --split dev --corpus evals/learning_v3.jsonl`。`--out` 将 Markdown、JSON 和前次报告查找都放到指定目录，不往仓库写报告。读取与写入均为 UTF-8。报告比较校验样本内容 SHA-256、评分版本与判分次数。

报告单列因长度截断的**学习批次数**（同一批的首轮／修正／重试只计一批），同时列学习与判分截断调用数、生成耗时，以及按学习／判分分列的超时调用数；JSON 保留生成调用的紧凑统计字段，包含 finish_reason 与输出 token。别名覆盖率＝正确覆盖的 alias 标注／alias 标注数；别名精确率＝判对的实际学习别名／实际学习别名数；分母为零记为不可计算，不显示成 100%。其他人物联系指标包含 alias，别名另行拆出。每段判两次，所有分歧按不利结论统计并列清单。

学习与判分的输出额度均为 16000 token，不限制额外推理长度。学习请求总超时 120 秒，评测判分及其修正请求 240 秒，报告记录两者。确定性名称／联系类型及必需主体检查只把不可能正确的正向判分改为 false；即使检查使两次最终结果相同，原模型判分的分歧仍列出。

独立隐藏验收集由规划者在仓库外维护，执行者不得寻找或读取。M1 的最终学习门槛须隐藏集和全部公开样本同时达到；公开样本即使全部达标，也不能据此宣布通过。后续真正新 holdout 仅用于最后一次评估；分析其失败后须转作 dev 并另写新集。

## v1 标注审计（2026-09-29）

- L025—L036：原 holdout 已在 PR #1 审查中被分析，逐段改为 dev；事实和其他标注未改。
- L018 第一条 must：小林说的是母亲养狗及狗名，即转述另一人的状况，立场由“亲历”改为“转述”。其余 must 按 8.3 逐条核对，未发现需更正的立场。
