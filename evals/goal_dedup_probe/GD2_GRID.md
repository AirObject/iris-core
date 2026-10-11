# GD2：补判与弱重复标记门槛（真实标定尚未运行）

本次只做确定性／假模型验证。规划者先完成服务商对照并另行授权独占窗口，才运行真实模型；不读取隐藏集，不改 goal_dedup_v1、判断提示词或原评分规则。

旧 C2 两轮有效公开结果均为：误合并 0/13，应合并识别 28/28（100%），不应合并中可能重复 22/39（56.41%）；其中 separate 12/29、uncertain 10/10。见 C2_RESULTS.md。这是历史 GLM high 结果，不是当前服务商的新基线。

## 候选网格

新增运行设置 `goal_dedup_judge.possible_min_similarity`，范围 0—1（包含边界）。候选顺序固定为 **0、0.4、0.5、0.6、0.7、0.8、0.9**。0 为旧行为的回退值；0.6 是本补丁的待标定工程默认值，不能据假模型结果宣称质量改善，合并采用前仍需规划者审查真实结果。

分数为现有 `normalize_claim` 后正文的 `difflib.SequenceMatcher.ratio()`。它仅约束弱标记：C2 数字／否定冲突配对、模型 uncertain，以及 A 的近似正文提示；A 原有 0.88 下限也保留。共享入口与来源人只参与原候选召回，不能绕过新的标记门槛。

候选召回、材料、提示词和模型请求不变。原始全部 uncertain、序列冲突、多个 same 和候选溢出继续阻止自动合并；不能因为标记被过滤，就把另一个 same 自动合并。模型确认 same 的管理员目标仍标可能重复并由管理员处理；其他 strong same 歧义也保留。提高门槛不增加自动合并。现有已保存的可能重复关系不会被批量清除。

## 真实窗口中的运行协议（本回合未执行）

用规划者选定的同一服务商、模型、档位先跑 `--possible-min-similarity 0` 两轮当前基线，再按上述顺序各跑两轮。全部使用冻结的公开 `evals/goal_dedup_v1.json`，逐案隔离数据库与固定时钟。每轮新建仓库外输出目录；任何降级、待复核或不完整运行保留材料、作废重跑。

每次使用原运行器，仅增加门槛参数，例如：

```sh
IRIS_TEST_MODELS=/Users/cassia/Local/Code/iris-core/test-models.toml uv run python evals/goal_dedup_probe/run.py \
  --method C2 --possible-min-similarity 0.6 --budget-seconds 60 \
  --out /Users/cassia/Local/Code/iris-eval-artifacts/gd2-goal-pending-20261011/authorized-window/threshold-060-round-1
```

本表候选和提示词先冻结，运行中不修改源码。`report.json` 保留参数、语料／提示词／源码指纹、逐例结果、误合并率、应合并识别率及新增合计项 `nonmerge_possible_rate`（separate＋uncertain），并保留原分类统计、调用、用量、耗时、降级数；`0/0` 仍不可作为合并门槛达到。报告两次和较差值，列识别损失与人工复核量的取舍，由规划者据真实数据决定最终门槛。本补丁不新增未经批准的质量通过线，不用假模型选型。

产品继续使用 5 秒同步预算。选定门槛后，须另检查该预算下的超时与补判、注入延迟；60 秒探测延迟不能声称等于产品 5 秒效果。

假模型自检用 `--fake same|different|uncertain`，不会读取模型配置或访问网络；结果明确标记为 simulation，不参与质量选择。
