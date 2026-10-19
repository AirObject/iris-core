# EF-27：入口画像观察提名并集实验，默认关闭

本轮只准备实现和离线／假模型验证，真实模型调用0，未测试发现覆盖的实际增益，也未做质量门槛判定。分支 `exp/entry-profile-union-nomination`，基于最新 origin/main `9ae5d0e9836b77b3d4330baacaa4264d1c7bfbea`；没有合入已关闭的 #116／#126／#127。

## 运行设置与提名并集

`runtime_settings.entry_profile.union_nomination` 是严格布尔值，默认 `false`。已有安装缺字段自动按 false；不用迁移。默认关闭的是第二次观察实验，原有自动画像生成保持原状。实验开关由既有 Store 运行设置持久保存，运行器以 `--union-nomination` 为新数据库打开；本轮不新增管理接口、页面或管理员确认环节。

开启时每个非空冻结观察块执行两次相互独立的观察：提示词、消息、已有主题目录和 as_of 完全相同；第二次请求不携带第一次的回答，也不先写入第一次的候选再更新目录。每次观察独立获得原有180秒截止时间，本次调用内重试和一次JSON修正共用这一个截止时间。观察材料、修正材料和输出的上限保持原值；每次仍最多8个观察，两次合并最多16项。

原始回答分别保存在 `outputs_json.observe_nomination_1`／`observe_nomination_2`（修正与尝试后缀沿用原规则），`calls_json` 增加 `nomination_round`，两次均走原 Gateway，model_calls 和步骤限额按实际调用计算。关闭时仍用原来的 observe 输出键、一次调用和原有请求材料。

第一份有效结构持久保存在已有 work.parsed_json 的内部缓存中；预算中断／重启恢复后不重复请求已完成的一份。两份齐全才合并写回，单份结果不单独产生画像。来源、开关或入口设置变化时拒绝旧结果；缓存绑定原材料指纹，清除仍通过既有工作索引取消。所有新增模型调用在数据库写事务之外；原目标修订号、来源、范围和人工保护校验继续生效。

## 去重与冲突

- 同类别、同正文／说法／释义、同操作与目标的条目合并，字段顺序不影响判断。来源按 `(message_id, field, excerpt)` 去重；释义来源只在同一说法与释义的合并项内取并集。两个不同片段不拼接出新引文。
- 同一确认目标的来源合并；同一原话被两次提名仍是同一来源，独立性继续由原代码计数。未攒够门槛的内容只可成为原有低强度候选，不进入宿主投影；重复提名本身不能通过准入。
- 同一目标有撤下与确认／保留旧正文的提名时，忽略冲突修改并保留旧正文。不同的修订提案、撤下与修订冲突也不任意选一份：丢弃冲突修改，记录 `nomination_union.conflicts`。目录中相同旧正文有多个目标时都按保留处理。
- 单独提出且无冲突的修改／撤下仍交原流程检查；两次一致的修改只合并来源。不会凭并集直接修改、撤下或准入。

只做字面与结构去重，不用模型推断主题等价，不加话题词表。不同措辞的候选仍可能并存并增加待检查成本。有效结构合并后若来源绑定或原确定性规则失败，仍按原规则删除；没有新增放宽路径。一次观察完全失败时不拿另一份替代完整并集，工作按原失败／延后规则留下诊断。

原准入检查、证据选择与分页、独立性计数、生命周期、memory_ops、调度和模型预算配置未改；未移植 EF-26 的词法修正。提示词和默认版本保持 observe_v3／admit_v3，14份入口提示词逐字未变。scoring_v3 与 validate_vote、_combine_votes、score_draft、_incremental_stages、_judge_memory、_judge_source 的 AST 不变。

## 运行器命令

以下是准备好的用法，**本轮未执行真实模型命令**；新公开dev集由规划者手写冻结后再按通知运行。以后运行真实命令时，使用既定 `IRIS_TEST_MODELS=/Users/cassia/Local/Code/iris-core/test-models.toml` 环境变量，不复制配置。两组使用同一代码、语料、模型与判分流程，在不同目录完整重复N次，不按案例挑选运行。

```bash
uv run python -m evals.entry_profile_eval.memory_runner repeat --corpus <冻结的公开dev.json> --runs 3 --out <仓库外材料>/single
uv run python -m evals.entry_profile_eval.memory_runner repeat --corpus <冻结的公开dev.json> --runs 3 --union-nomination --out <仓库外材料>/union
```

每组目录含 series.json，及 run-0001、run-0002、run-0003，各自拥有独立 SQLite 数据库、原始记录和 judging-materials。旧输出目录不能覆盖；一次传输无效即保留材料并停止余下重复，输入在任一次前后发生变化也停止。原 run_sqlite 作废判定与 gate 标志不改，单次 `run` 同样支持 `--union-nomination`。

每一次先按原 score 命令完成独立外部双判，示例只列第1次；第2、3次须分别判分，不能直接把覆盖分当内置模型评分：

```bash
uv run python -m evals.entry_profile_eval.memory_runner score --materials <材料>/single/run-0001/judging-materials --judgments <该次第一轮> --judgments <该次第二轮> --judge-model 'gpt-6.1-sol max' --out <材料>/single/run-0001/score.json
```

原计分器完成逐条验证与从严合并后，离线汇总所有N次，不能省略失败或低分运行：

```bash
uv run python -m evals.entry_profile_eval.memory_runner summarize --series <材料>/single --score <材料>/single/run-0001/score.json --score <材料>/single/run-0002/score.json --score <材料>/single/run-0003/score.json --out <材料>/single-summary.json
uv run python -m evals.entry_profile_eval.memory_runner summarize --series <材料>/union --score <材料>/union/run-0001/score.json --score <材料>/union/run-0002/score.json --score <材料>/union/run-0003/score.json --out <材料>/union-summary.json
uv run python -m evals.entry_profile_eval.memory_runner compare --left <材料>/single-summary.json --right <材料>/union-summary.json --out <材料>/paired.json
```

真实请求的调用量与用量优先取provider记录；假模型没有provider记录时以去重的工作尝试作诊断，并标注 `provider_recorded=false`，不将该回退值声称为真实HTTP调用数。

summarize 保留每次结果、均值、逐案例覆盖数量分布与范围。按真实保留票计算有依据、不同违规模型／人工记忆、违规标签发生次数、投影违规及冻结 forbidden 命中，明确区分记忆数量和重复投影计数；不重新判分或更改原双判合并。

compare 按运行序号＋案例ID配对，输出逐对覆盖差、逐案例差值分布、两组违规／禁止内容总计、调用量、请求耗时与墙钟差。两组必须拥有相同的语料、源码、评分、回放、模型元数据、运行次数和案例标签；输入与汇总均有指纹核对。缺少 score、材料／案例绑定不匹配、无效或不完整系列会拒绝汇总，不静默丢弃运行。汇总／配对明确 descriptive_only=true、gate_eligible=false，不通过均值自动宣称门槛；各次原判分和 gate 标志单列保留。summarize／compare 不加载模型配置、不调用任何模型。

## 测试：先红后绿

新增31项手写确定性／假模型测试，位于 test_entry_profile_union_nomination.py、test_entry_profile_repeat_eval.py。初始在未实现的main基础上24失败／1通过；随后两处冲突键序／重复目录边界先红2失败，最后一次输入变更先红1失败，修复后新测试31通过。与原入口机制回归合计85通过后，另补多案例按ID配对测试并通过。所有假票只验证汇总格式和算术，不是质量判分。

覆盖：默认值与布尔校验；关闭保持单请求和原输出形状；两份材料相同且不相互泄漏；重复提名不凭空准入；第二份可提出新候选；来源与释义去重；撤下／确认／修订冲突；已准入正文保留；同一确认只确认一次；每份独立180秒与本份修正共截止；第二份超时不应用第一份；预算断点恢复；调用中切换设置拒绝写回。运行器覆盖独立库／目录、N校验、拒绝覆盖旧目录、实际调用计数、无效传输停止、运行中输入变化、各次／均值／案例分布、按序号和ID配对、违规与禁止分开累计、缺分／异指纹拒绝、离线命令不读模型配置。

main 在首轮本地验证期间合入 persona #61，执行者用 `git merge origin/main` 快进到9ae5d0e；两份目标产品文件在这次 main 更新中未变，本轮四份改动逐字恢复。先前 cc60a0b 上的3.13全量3785通过、未完成的3.12及旧对照全部保存在 validation-before-main-sync，不作为最终验证。最终以独立只读 baseline worktree 固定9ae5d0e，防止并行会话推进 main 导致对照漂移。

最终按顺序执行（期间没有真实评测或虚拟环境重建）：

| 验证 | 结果 | 耗时 |
| --- | --- | --- |
| `uv run pytest`，Python 3.13.13 | 5438通过，8个warning | 1396.36秒 |
| `uv run --locked --isolated --python 3.12 pytest`，Python 3.12.12 | 5438通过，8个warning | 1334.71秒 |
| `uv build` | wheel／sdist构建通过 | 2.456秒 |

构建后核对wheel和sdist中的产品文件及14份入口提示词，与工作区逐字一致，包中无密钥文件。全量测试与对照前后218份源码、提示词、语料及测试文件指纹不变；测试后已清理本worktree的 `.pytest-tmp`，`.venv`仍为3.13.13。

## 关闭开关的请求对照

对全部公开 entry_profile_v1 六例，以同一手写假模型经过原 SQLite 每日＋尾块回放，比较 main、实验分支缺省关闭、显式关闭三组完整系统提示词、用户JSON字符串、顺序、来源字段与请求参数。每组37次请求，字节完全一致，未做任何归一化。SHA-256均为 `dc5dd8dd54dbb3e5a0fac65c33b3ab4f9943a7c86f86bc72d57ee4aa02b7d1c8`。假模型只证明请求路径一致，不代表原规格覆盖或质量。

## 学习与召回对照

基线均为固定的 main `9ae5d0e`，候选为本轮源码，全部使用确定性假模型，不调用真实端点。

- 学习：按 `evals/compare_learning_requests.py` 的同一比较逻辑、仓库外适配脚本纳入全部learning_v1—v5，116段、272个批次、1925条记忆；请求差异0。两侧请求SHA-256同为 `147c831b46c19fa6f83c4055ff8a61c5f4838165453976a73687c5f2f485c08c`。只固定生成的时钟和批次追踪身份，不归一化请求正文、顺序或空白。
- 召回：七份公开语料，243条固定记忆、228次查询，五种模式共1140份返回差异0；214次召回判断输入也完全一致。只排除 `recall_id` 和耗时等运行字段，其余返回字段均比较。

上述结果证明旁路与关闭开关的兼容性，不是画像质量或发现能力的评测。

本轮真实模型调用0，无真实评测、无双判质量结果、无质量作废运行；未找寻或读取隐藏集，未修改冻结语料／评分或其他会话占用文件。完整材料和日志只存 `/Users/cassia/Local/Code/iris-eval-artifacts/EF-27-20261020/`。仓库仅提交实现、测试和本汇总；不合并PR。
