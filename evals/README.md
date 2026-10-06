# 学习、召回与端到端评测

## 当前状态

2026-10-05 起，开发和评测的对话模型改为 glm-5.3-flash，embedding 仍为 doubao-embedding-vision，开发设备改为 macOS（见 DECISIONS.md 同日记录）。MiniMax-M3 时期的评测报告和执行记录已从仓库删除，原文保留在 git 历史中（main `2ee81d8`）。

`evals/reports/` 目前有 PR #6 的 GLM 学习运行诊断、PR #5 的召回与性能报告和 PR #8 的 GLM 探测。PR #6 那次学习运行两边都有因超时放弃的批次，按规则作废，其中的 GLM 判分只作预览。仓库里还没有可作门槛依据的学习结果，所有门槛都要在 GLM 上重新达到。

第一阶段对 GLM 的观察（PR #6）：main 一半以上的学习调用达到 120 秒总超时，推理 token 约占输出的 93%；4/189 次学习输出不是严格 JSON（前后带反引号或说明文字，现有解析器能容忍）；9 个敏感样本没有出现内容拒绝，HTTP 400 在现有网关中仍归为配置错误。

GLM 探测（PR #8）：方舟上的 glm-5.3-flash 不能关闭推理，`reasoning_effort` 默认为 max。max 档在 learning_v3 上的学习调用 P50 约 90 秒、P95 约 324 秒，有一次推理用尽 16000 token 输出上限、正文为空；low 档 P50 约 6 秒，最长约 21 秒。learning_v3 的执行者单判：max 档记忆精确率 70.4%、事实召回率 75.0%、归属正确率 88.9%，low 档分别为 66.7%、71.4%、71.4%（各一次、10 段，只作 dev 参考）。内容安全拒绝按方舟的 `error.code` 和 `finish_reason=content_filter` 识别，实际的拒绝路径尚未验证。

评测判分改由执行者所用的模型完成，不调用对话模型（DECISIONS.md 2026-10-05）；学习判分通道由 PR #7 实现，端到端沿用其材料与独立轮次约定，命令见下文。评分说明不变；代码导出判分材料，并按执行者交回的判分计算指标。两次判分在互不可见的独立会话中完成，分歧取不利结论；用于门槛判定的判分，由没有参与该轮代码和提示词修改的执行会话完成。只在 macOS 上验证，暂不做 Windows 验证。

## GLM 重测计划

| 阶段 | 工作 | 代码 | 用途 | 状态 |
| --- | --- | --- | --- | --- |
| 一 | 学习，全部 86 段（GLM 双判） | main、PR #5 | PR #5 的学习对照 | 两边都有超时放弃的批次，作废（PR #6） |
| 一 | 召回，默认配置 | main、PR #5 | 确认新设备和重新获取的 embedding 能复现以前的结果 | 纯全文逐条一致，向量路径有小差异；PR #5 已在当前 embedding 上重新标定 |
| 一 | 性能，5 千／5 万条 | PR #5 | R10：prepare P95 ≤ 500ms | 第四轮通过 |
| 一 | 执行者判分通道 | PR #7 | 导出判分材料，按执行者判分计算指标 | 完成 |
| 一 | 规划者隐藏召回集 | PR #5 | PR #5 的召回验收 | 第四轮的短词修复成立，但精确率退化，需第五轮 |
| 二 | PR #5 第五轮：守住精确率；学习材料恢复为 PR #4，并做确定性比较 | PR #5 | PR #5 的合并条件（DECISIONS.md 2026-10-06） | 已合并；纯全文降级的召回低于 main，另行修复 |
| 二 | GLM 探测：推理开关、自然耗时、内容安全信号；在 learning_v3 dev 上用执行者单判对比 | PR #8，不改产品代码 | GLM 适配的依据 | 完成 |
| 三 | 纯全文降级单独标定；复测 5 万条点名性能 | 检索修复分支 | 不配 embedding 或 embedding 失败时的召回；R10 余量 | 进行中 |
| 三 | GLM 推理档位对照：low、high 在全部公开学习语料上各跑两次，执行者单判 | main，不改产品代码 | 选默认档位（DECISIONS.md 2026-10-06） | 进行中 |
| 三 | 调度分支合入 main，端到端改用执行者判分 | 调度分支 | M1-6 | 进行中 |
| 四 | GLM 适配：推理档位配置、方舟错误码与内容安全信号、推理字段只作诊断 | 调度分支合入之后 | 学习能稳定完成 | — |
| 四 | 学习：dev 迭代（执行者单判），定稿后执行者双判 | GLM 适配之后 | M1 三项学习门槛 | — |
| 四 | 端到端 E001—E011，执行者双判 | 同上 | M1：至少 9/11；U04 | — |
| 五 | 规划者隐藏集：学习、端到端，召回复核 | 第四阶段定稿的代码 | M1 最终验收 | — |
| 五 | 人工抽查至少 10%；第二家服务商对照 | 同上 | 设计 21.2、21.4 | — |

对照规则（在看到结果之前确定）：

- 两个版本的学习对照在同一时间段、用同一份配置运行。任一边出现因限流或超时而放弃的批次，这次结果作废，重跑。
- 以执行者判分的结果为准。新版本的记忆精确率、证据正确率、归属正确率中，任一项比旧版本低 3 个百分点以上时，两边各再跑一次；两次平均后仍低 3 个百分点以上，判定新版本使学习变差。
- 只改召回的 PR，学习材料必须与 main 逐字一致，用确定性假模型逐批比较学习请求来证明，不跑学习对照（DECISIONS.md 2026-10-06）。
- 召回改动同时报告 relevant 标注精确率和无答案误返率，全部查询与对话中准备分别列出，并与上一个默认方案逐项比较；无答案误返多出两条或更多查询，或精确率下降超过 0.03，先修复或由规划者决定（DECISIONS.md 2026-10-06）。
- 复现历史召回结果时不重新选参。质量指标与历史结果不一致时，先报告原因（例如 embedding 服务返回的向量有变化），由规划者决定是否重新标定。

阶段门槛要求全部公开样本和规划者隐藏集同时达到。公开语料在 GLM 阶段全部按 dev 使用；`learning_v2` 的历史 holdout 只作回归分组单列，不作 holdout 判定。

## PR #5 本次复现（2026-10-05）

以下为第一阶段的历史记录；本次完成 PR #5 的 macOS 测试、召回复现和性能测量。详见 [收尾记录](reports/pr5-macos-closeout-20261005.md)、[召回逐项对照](reports/pr5-macos-recall-comparison-20261005.md) 和 [性能报告](reports/retrieval-performance-macos-20261005.md)。

Python 3.13／3.12 各 215 passed，uv build 成功。默认召回 Recall@8、nDCG@8、误返率与历史一致；relevant 标注精确率及五条 v2 返回存在差异，历史源码指纹也待追溯，未重新选参，交规划者决定。默认四组性能 R10 通过；扩展对照及 macOS RSS 口径另见报告。该阶段的真实学习对照安排现已由 2026-10-06 的确定性检查决定取代。

## PR #5 第四轮短词修复（2026-10-06）

按规划者的新要求，在新设备收尾提交推送后，先于 `830efb6` 单独冻结 16 条手写短词 dev，再写失败测试、实现修复和重新标定。trigram 补查已有 jieba 索引，只有已知姓名而没有话题词时从涉及人／说话人及正文提及取候选；学习检索独立不变。

全部 122 条 dev 的原网格规则选择 jieba；其余默认参数不变。新集 12 条有答案全部命中、4 条无答案全空。原 128 条 Recall 不变，但误返率 0.7000 → 0.8667、relevant 标注精确率 0.5479 → 0.2966，明确列为退化；不以新增样本抬高总体 Recall 掩盖。Python 3.13／3.12 各 245 passed，构建成功，四组默认性能 R10 通过，最大 prepare P95 304.5ms。

完整标定、逐语料／类别与全部查询的前后对照、内存及已知问题见 [第四轮报告](reports/pr5-r4-short-terms-20261006.md)。本轮授权取代第一阶段“只复现”的限制，未改变上方 main 两节历史原文。该阶段的学习对照安排现已作废，第五轮改为确定性请求比较。

## PR #5 第五轮：学习材料一致与召回精确率（2026-10-06）

合入 PR #6、PR #7 与 2026-10-06 决定，保留外部判分、短检查点及完整指纹校验。学习材料恢复为仅本批发言人，不按正文提及加入人物要点；同一确定性假模型在 main 和本分支的 86 段公开语料上各产生 203 批请求，其中 117 批已有记忆材料非空，完整 system/user 请求零差异。没有运行真实学习评测。

按全部 122 条 dev 和原选择规则标定，默认改为 trigram，向量权重 1，全文覆盖率 0.75、长片段最大文档频率 2；绝对下限、相对比例、前缀和维度不变。原三集 Recall 0.9813 与第三轮相同、误返仍为 21/30、relevant 精确率 0.5479 → 0.5722；对话中准备恢复为 0.9444、2/6、0.7826，满足本轮公开约束。短词集继续 12 条有答案全中、4 条无答案为空。

Python 3.13／3.12 各 292 passed，构建成功；R10 四组通过，5 万条点名 P95 为 488.6ms，较第四轮变慢并接近上限。另有纯全文降级的取舍：整体 Recall 0.8106 → 0.3924，精确率提高，短词仍通过；不能把混合检索的验收结果当成全文无退化。完整三方表、逐查询结果、剖析、已知问题见 [第五轮报告](reports/pr5-r5-precision-learning-20261006.md)，交规划者审查。

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
uv run python evals/benchmark_retrieval.py --default-config --out evals/reports
```

默认 `iris eval learning` 仍调用对话模型双判，`--judge-runs 1` 可作单判预览；预览不作门槛依据。学习最终评测必须提供两轮独立的外部判分；单轮只用于 dev 迭代或流程检查，不计算双判分歧率。学习评分 v3、语料和 M1 门槛保持不变。`--out` 位于仓库外时，JSON 的 `details` 保存每案例完整输入、全部记忆及来源、每次原判分和最终判分；仓库内报告保持汇总与抽查规模。

学习成功案例保存在 `data/lc/<16位指纹>`，外部报告的检查点在 `<out>/.lc/<16位指纹>`；meta.json 核对完整 SHA-256，metadata.json 保留导出所需的运行来源。模型预览只有源码、语料、模型端点／ID／embedding 维度、判分次数都一致才复用已完成案例；外部模式的检查点只记录学习，不依赖之后的判分轮数，两种模式的检查点分开。失败案例重新执行，不按分数选择结果。报告列 `resumed_cases` 和源码指纹。请勿在评测运行期间编辑源码／迁移或重建当前虚拟环境。

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

材料封装的 `format_version`、`evaluation`、案例映射和每轮指纹绑定不依赖学习的字段结构。端到端评测沿用同一约定，但使用 `evaluation="e2e"`、自己的 `input` 和 `e2e_scoring_v1` 校验器，见下文；学习与端到端的计分命令拒绝混用材料。

## 召回评测

默认运行四份公开语料，共 139 条固定记忆、144 条查询；每份语料在独立数据库中入库。直接使用固定记忆，不经过学习，不调用生成模型，走正式 Retrieval.prepare／search、去冗余和召回记录路径。

| 语料 | 来源 | 固定记忆 | 查询 | 无答案 | 原 split |
| --- | --- | ---: | ---: | ---: | --- |
| recall_v1.json | `a8af9b1` 单独冻结、手写 | 40 | 38 | 8 | 38 dev |
| recall_v2.json | `95549fc` 在检索改动前单独冻结、手写 | 50 | 66 | 16 | 44 dev、22 历史 holdout |
| recall_conversation_v1.json | `15cdd48` 在第二轮检索改动前单独冻结、手写 | 33 | 24 | 6 | 24 dev |
| recall_short_terms_v1.json | `830efb6` 在第四轮检索改动前单独冻结、手写 | 16 | 16 | 4 | 16 dev |

质量语料没有模板或脚本生成。v2 覆盖口语、改述、别名、相近名字、未知属性、参与者和近期消息去冗余；对话集覆盖私聊、群聊、直播，全部显式 `text: null`、`participants: null`，由近期消息推断当前问题和参与者。详见冻结时的 [recall_v2_notes.md](recall_v2_notes.md) 与 [recall_conversation_v1_notes.md](recall_conversation_v1_notes.md)。短词集覆盖带／不带人物筛选的两字关键词、只在涉及人或正文出现的两字名字、不在场的人及同类型无答案查询，来源和逐条分组见 [recall_short_terms_v1_notes.md](recall_short_terms_v1_notes.md)。notes 保存当时的计划，当前选参方法以下文为准；不改语料或 notes 来适应新结果。

GLM 阶段全部公开样本按 dev 使用，命令和报告仍尊重冻结文件的 split，历史 holdout 仅作回归分组，不代表未见验收。PR #5 原选参只使用 v1 38 条、v2 44 条 dev 和对话集 24 条，共 106 条；原 v2 的 22 条历史 holdout 已分析，不参与选参。新设备第一阶段运行全部 128 条复现，未重新选参。第四轮按规划者要求加入短词集后，在全部 122 条 dev 上按原规则重新标定，并在全部 144 条上做最终对照；原 v2 的 22 条历史 holdout 仍不参与选参。规划者的隐藏集在仓库外，执行者不得寻找或读取。

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

默认命令对照 jieba／trigram 的纯全文和混合向量四种方案，第五轮混合默认为 trigram。没有配置 embedding 时只输出全文结果，不能视为完成向量复现。embedding 使用真实服务，按端点／模型／维度／完整输入 SHA-256 缓存在 `data/recall-embeddings.db`；外部 --out 的缓存也在外部，不含凭据。预取与正式路径共用 prepare_query／embedding_text，新增请求单路、间隔至少 1 秒。首次运行另记新请求用量与耗时；本地 prepare P95 扣除查询 embedding 等待，包含检索、去冗余和召回记录提交。外部 JSON 的 details 另保存完整输入、记忆、标签和返回，仓库内只保留 ID 与汇总。

`--calibrate` 只允许 `--split dev`，报告完整网格，不写在用数据库。网格为 jieba／trigram × 向量绝对下限 {0.35, 0.45, 0.55, 0.65} × 相对比例 {0.75, 0.85, 0.95} × 向量权重 {0.5, 1, 2} × 前缀 {空, “为这个问题检索能回答它的个人记忆：”}，全文权重 1；第五轮增加完整词项覆盖率 {0.35, 0.5, 0.75} 和三字以上词片段最大文档频率 {0（关闭）, 2}，共 864 个混合方案及十二个全文对照。完整词项使用 jieba cut 的去重非姓名实词，避免 cut_for_search 重叠子词重复计数，短词补查也使用完整分母。三字以上片段可按整库文档频率入选，频率不受人物筛选或候选上限影响；一／两字词不能走此通路。默认维度 2048；只有额外传入 `--compare-embeddings` 才比较 1024／2048 维。第五轮只运行 `--split dev --calibrate`，不重新比较维度。

选择规则先计算 `Q=(Recall@8+nDCG@8)/2`，距全网格最高 Q 不足 0.01 才算持平，恰差 0.01 不算。持平先取 relevant 标注精确率较高者，再取无关误返率较低者，再看 Q，完全相同按固定网格顺序；精确率无分母按 0 处理。不得逐项链式平分或只比较每个分词器／前缀的局部赢家，不从 Q 扣除误返。M2 参考值仍为 Recall@8≥0.85、nDCG@8≥0.75、无关误返率≤0.10；无答案拒绝留到 M2，本轮只报告。

当前冻结默认见 `src/iris/retrieval_defaults.json`：trigram、RRF k=60、2048 维 float32、向量权重 1／全文权重 1、绝对下限 0.35、相对比例 0.75，加上述问题前缀，词项覆盖率 0.75、长片段最大文档频率 2。无向量时降级 trigram 并补查 jieba 短词；在相同词项门槛的 dev 全文对照中，trigram 也优于 jieba。新数据库保存到 runtime_settings.retrieval，现有设置保留；更换 embedding 模型或维度须重新标定，未经标定只用全文。学习材料使用独立的 learning_retrieval 设置，不随回复选参变化。

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

评测按入口实际节奏分批：私聊与群聊每批最多 12 条和 4000 token，直播每批最多 4 条；正式路径默认有 4 条历史、4 条后续上下文。每段使用独立临时数据库，走正式接收、冻结和学习代码。评分固定为 `src/iris/prompts/scoring_v3.md`，每段同样输入独立判两次，不一致按不利结论计分并逐项列出。最终报告仍须人工抽查至少 10%。

评分 v3 使用设计 8.3 的最新定义：自身喜好按亲历，作品／人／事的评价按观点；兼容既有标注时，自身喜好的亲历／观点都算归属和覆盖正确，这不放宽其他判分项。明确归属于他人的说法不算 forbidden；当成事实或“我”的认识才算。判分输入包括实际主体 ID、账号和别名，代码额外拒绝把数组字符串判成人名、把错误类型的联系判作 alias 覆盖。事实覆盖须具备标注必需的说话人、about 和明确指定的账号；没有任何记忆满足这些必要条件时，不能判为覆盖，语义内容仍由模型核对。既有数值门槛不变。

不加参数时默认运行全部仓库样本，报告在 `evals/reports/`。`--corpus` 可与 `--split` 组合，不会拼入仓库样本，例如只检查手写 dev：`uv run iris eval learning --split dev --corpus evals/learning_v3.jsonl`。`--out` 将 Markdown、JSON 和前次报告查找都放到指定目录，不往仓库写报告。读取与写入均为 UTF-8。报告比较校验样本内容 SHA-256、评分版本、判分次数、学习模型与判分方式。

报告单列因长度截断的**学习批次数**（同一批的首轮／修正／重试只计一批），同时列学习与判分截断调用数、生成耗时，以及按学习／判分分列的超时调用数；JSON 保留生成调用的紧凑统计字段，包含 finish_reason 与输出 token。别名覆盖率＝正确覆盖的 alias 标注／alias 标注数；别名精确率＝判对的实际学习别名／实际学习别名数；分母为零记为不可计算，不显示成 100%。其他人物联系指标包含 alias，别名另行拆出。每段判两次，所有分歧按不利结论统计并列清单。

学习与判分的输出额度均为 16000 token，不限制额外推理长度。学习首次请求、调用内重试和 JSON 修正共享 180 秒预算，评测判分及其修正请求共享 240 秒预算，报告记录两者。确定性名称／联系类型及必需主体检查只把不可能正确的正向判分改为 false；即使检查使两次最终结果相同，原模型判分的分歧仍列出。

独立隐藏验收集由规划者在仓库外维护，执行者不得寻找或读取。M1 的最终学习门槛须隐藏集和全部公开样本同时达到；公开样本即使全部达标，也不能据此宣布通过。后续真正新 holdout 仅用于最后一次评估；分析其失败后须转作 dev 并另写新集。

## v1 标注审计（2026-09-29）

- L025—L036：原 holdout 已在 PR #1 审查中被分析，逐段改为 dev；事实和其他标注未改。
- L018 第一条 must：小林说的是母亲养狗及狗名，即转述另一人的状况，立场由“亲历”改为“转述”。其余 must 按 8.3 逐条核对，未发现需更正的立场。

## 端到端脚本（M1-6）

```bash
uv run iris eval e2e --judge-mode external --out <运行目录>
uv run iris eval e2e --judge-mode external --script E001 --script E011 --out <流程检查目录>
uv run iris eval e2e --judge-mode external --corpus <外部脚本.json> --out <运行目录>
uv run iris eval e2e-score --materials <材料目录> --judgments <第一轮目录> --judgments <第二轮目录> --judge-model <执行者模型名称> --out <报告目录>
# 默认的对话模型双判仅作预览，单判预览用 --judge-runs 1
uv run iris eval e2e --split dev --judge-runs 1
```

默认读取手写 `e2e_v1.json` 的 11 个 dev 脚本；JSON 使用 `{"version":1,"scripts":[...]}` 或脚本数组，JSONL 每行一个脚本。`--corpus` 完全替换输入，`--split dev|holdout|all` 按脚本筛选，默认 all；`--script ID` 可重复指定，按原语料顺序运行所选脚本，不修改冻结语料；`--out` 替换输出目录，默认 evals/reports。隐藏脚本由规划者按相同格式运行，执行者不得查找或读取。

每脚本是如下对象（缩短示例仅说明格式，不是评测语料）：

```json
{
  "id": "external-example",
  "split": "dev",
  "entries": [{"id":"a", "name":"聊天", "platform":"chat", "kind":"private", "pace":"realtime"}],
  "messages": [
    {"entry_id":"a", "dedupe_key":"m1", "sender":"小林", "account_id":"lin", "kind":"message", "content":"我喜欢天文摄影。", "occurred_at":"2026-10-04T09:00:00+08:00"},
    {"entry_id":"a", "dedupe_key":"m2", "sender":"Iris", "account_id":"iris", "kind":"self_output", "content":"听见了。", "occurred_at":"2026-10-04T09:00:10+08:00"}
  ],
  "checkpoints": [{
    "id":"after-restart", "entry_id":"a",
    "question":{"sender":"小林", "account_id":"lin", "kind":"message", "content":"我喜欢什么摄影？", "occurred_at":"2026-10-05T09:00:00+08:00", "dedupe_key":"q1"},
    "prepare":{"recent_limit":1},
    "expected":[{"fact":"小林喜欢天文摄影。", "source_keys":["m1"]}],
    "forbidden":["Iris 喜欢天文摄影。"]
  }]
}
```

入口字段 `kind` 包括 private、group、live，`pace` 使用 realtime／standard／economy（默认 standard），或与 HTTP API 相同的自定义对象 `{"count":6,"idle_seconds":30,"max_wait_seconds":180}`。每条消息都写明账号、发言人、类型、带时区时间和去重键；按脚本数组顺序发送，多入口可交错。`occurred_at` 是原事件时间，不会加速调度器的接收时钟。可选 `delay_seconds` 在发出该条前实际等待（0—3600 秒）；没有指定则连续经 HTTP 发出。角色默认为 Iris，时区 Asia/Shanghai，服务启动在全新临时数据目录，没有通过离线命令预设记忆。

检查点先写入 `question` 消息，再原样向 prepare 发送可选 `prepare` 对象。省略 text 和 participants 测试最近消息推断；预算字段与正式 API 一致。expected 为非空列表，每项包含 fact，以及 source_keys 或 sources 二者之一，用来计算该事实从证据发出到记忆出现的时间；禁止说法是字符串列表。提问的去重键应与既有消息、其他检查点不同。不同账号即使同名也不合并；评分输入包含接收回执中的 message_id，可核对记忆来源和原账号。

同入口来源仍写 `"source_keys":["m1"]`，字符串始终是提问入口中的完整去重键，不解析冒号。跨入口来源写 `"sources":[{"entry_id":"group","key":"g1"}]`，允许同一事实引用多个入口；所有来源必须存在于同一脚本的 messages 中，不能引用检查点问题。两种格式不能同时出现。例：在 group 学到、到 dm 提问时，期望项可写 `{"fact":"Iris 答应带热茶。","sources":[{"entry_id":"group","key":"g4"}]}`。评分仍只使用 prepare 实际返回的记忆。

每个检查点还执行确定性的 `recent_message_isolation` 检查：使用 HTTP 接收回执的消息 ID 核对入口、正文、类型及提供的引用正文，确保 recent_messages 只包含本提问入口已发送的原始消息。其他入口已形成的记忆允许共享；即使模型判分通过，原始消息隔离失败也使整个脚本失败，报告保留失败的消息 ID 和完整响应（后者仅外部 --out）。

每脚本执行：启动真实 `iris serve` → HTTP 写入全部消息 → 轮询 status 等待后台自然排空 → 强制结束整个服务进程树 → 原目录重新启动 → 按检查点写入提问并调用 prepare → 导出执行者判分材料（或保留的默认对话模型预览）。评测器不调用 `/learn`，不直接调用学习函数，不读写被测数据库。`--wait-timeout` 默认 900 秒，只限制写完消息后的排空等待，不改变学习节奏；标准尾部可能自然等 10 分钟、省流尾部等 30 分钟，需要时增大它。脚本按顺序执行以降低并发限流。

固定评分为 `src/iris/prompts/e2e_scoring_v1.md`，只判断 memories 是否覆盖事实，不用 recent_messages、persona 或常识补答案。默认模型预览每检查点判两次；外部计分由 --judgments 的次数决定轮数。双判事实覆盖取 AND，禁止说法出现取 OR；记录两份理由和分歧数，所有检查点通过脚本才通过。`--judge-runs 1` 仅供迭代，分歧率为 null，不能冒充最终双判。模型预览的评分错误、服务异常或排空超时均保留为失败，不筛掉脚本；外部判分文件不合法时先报错，全部修正后才生成报告。

报告 JSON 包含每脚本结论和原因、事实延迟、U04（实时入口 ≤60 秒）单独判定、双判分歧、源码／语料指纹、模型 ID、学习调用的 P50／P95／最大耗时和超时数。U04 按来源入口的学习节奏判定（全部来源为 realtime 时适用），从 source_keys／sources 中最早证据发出开始，以对应记忆新增／最新修订在 HTTP 状态中首次被观察到为终点；轮询间隔约 0.2 秒，长发送间隔会给出更保守的上界。双判引用多条记忆时取最晚出现的必要记忆。

完整输入、接收回执、prepare 返回、各轮原判分及重启前后状态仅在仓库外 --out 的 details 中保存；仓库内只保存汇总、判分结论和耗时。临时服务目录在运行后删除；临时 TOML 仅含 api_key_env 变量名，密钥通过子进程环境传递，不落到临时文件或报告。全部公开脚本至少 9/11 只是可见集门槛，规划者的隐藏脚本也须达到，不能据此宣布 M1 最终验收通过。小样本单判只是流程检查；报告的 repository_gate 仅按本次脚本计算 ≥80%，不能把子集结果当成全量验收。当前仅在 macOS 验证，Windows／深路径验证暂停。

`E001`、`E005`、`E007`、新增跨入口 `E011` 不传查询文本和参与者。`E005` 用默认近期 20 条；其他多数检查点按较小的宿主上下文预算（2／5 条）检查召回，防止已在近期消息里的事实被正确去重后误判为召回失败。预算在真实评测前随脚本冻结，不按运行分数更改。

第二轮查询文本修正已于 `ce34fa1` 单独提交：公开脚本的 prepare.text 只省略或传提问原文；具体修改保留在该提交中，执行记录不再提交到仓库。

E011 的群聊 → 私聊共享记忆脚本在真实评测前另行冻结，加入后公开集为 11 个手写 dev；通过比例仍为 ≥80%（至少 9/11）。原 10 个脚本和新增跨入口脚本均逐项报告，不用新增样本掩盖原集失败。

### 端到端外部判分流程与文件格式

1. `e2e --judge-mode external` 正常运行 HTTP 接收、自然学习、强制重启及 prepare，不调用对话模型判分。完成后打印 judging-materials-<时间>/manifest.json，不生成质量报告；其父目录作为 --materials。此模式不能带 --judge-runs。
2. 给判分者相同的 scoring.md 和 cases/*.json，只按 input 判分；不提供旧报告或另一轮结果。两轮在互不可见的独立会话中进行，用于门槛的判分者不得参与该轮实现。目录绑定只能验证材料，不能证明会话独立。
3. 每轮建独立目录，将 round-template.json 复制为 manifest.json，按清单 judgment_file 保存一份检查点判分。e2e-score 的一次／两次 --judgments 分别是单轮／双轮，--judge-model 必填。离线计分不读取测试模型配置、不启动服务、不调用任何模型。缺失／多余文件、数组长度、非布尔值、无效 ID、无支持记忆的正向结论、非字符串 reason 都会报错，不自动补 false。错误按轮次和检查点汇总。

所有文件使用 UTF-8，材料与结果留在仓库外。目录结构沿用学习判分：

```text
materials/
  manifest.json          # format_version=1、evaluation="e2e"、run、cases 和文件指纹
  scoring.md             # 冻结 e2e_scoring_v1 原文
  cases/0000.json         # 一个脚本的一个实际返回检查点
  run.json               # HTTP 观测、回执、重启状态、学习调用和 U04 原始时间
  round-template.json    # {"materials_sha256":"材料清单中的 SHA-256"}
round1/
  manifest.json          # 从 round-template.json 复制
  0000.json              # 按 manifest.cases[].judgment_file 保存
round2/
  manifest.json
  0000.json
```

每份材料是 `{format_version:1, evaluation:"e2e", script_id, checkpoint_id, corpus_sha256, source_sha256, scoring_version:"e2e_scoring_v1", input}`。`input` 与原 `_judge` 用户输入逐字段一致：`messages`（含 HTTP 回执的 message_id）、`question`、`timezone`、`expected`、`forbidden`、`memories`。script_id／checkpoint_id 只用于映射，不直接拼入文件路径。语料 SHA-256 延续端到端旧定义，为输入文件原始字节摘要；本次筛选后的 script_ids 单列于 run，区别于学习评测的案例列表摘要。源码指纹在运行前记录。

materials_sha256 覆盖清单元数据及各文件 SHA-256，计分复核清单、材料、run.json、评分说明与输入映射。不要修改导出材料。run.json 不含旧 judges／combined／latencies；保存的消息回执和时间用于重新计算确定性隔离检查及 U04。若服务故障导致某检查点没有 HTTP 返回，不伪造待判材料，脚本失败仍在 run.json 和最终分母中保留。排空超时也照常导出已经取得的返回，但语义判分不能覆盖该失败。

每份判分沿用冻结格式；facts 与 expected、forbidden 与同名输入数组等长且顺序一致。memory_ids 只能是本次 prepare 返回的整数 ID，covered=true 或 present=true 必须至少列一条支持记忆。reason 字符串为原格式必需字段；额外字段不会改变计分，原判分留在外部报告 details[].checkpoints[].judges 中。例如一条 expected、一条 forbidden：

```json
{
  "facts": [{"covered": true, "memory_ids": [1], "reason": "记忆 1 完整保留事实、主体和时间。"}],
  "forbidden": [{"present": false, "memory_ids": [], "reason": "返回记忆没有该禁止说法。"}]
}
```

输出仍为 e2e-<时间>.json，保留旧脚本、U04、学习延迟和超时字段，新增 judge_mode、judge_model、chat_model、materials_sha256、judgment_rounds 和 judge_inconsistencies 清单。models.chat 同样表示学习模型；外部判分耗时、用量及超时未测量，不能把模型判分零调用理解为执行者零用量。单轮分歧率为 null。双轮事实取 AND、禁止项取 OR，支持 ID 取并集，分歧按原始判定逐项列出；两条路径共用校验、combine_judges、确定性否决和 U04 计算。

GLM max 推理可能触发 180 秒学习总超时，流程试用照实保留超时与排空失败，不调整超时、重试或学习请求参数。正式 GLM 学习／端到端门槛等待推理档位与方舟错误码适配后重测。
