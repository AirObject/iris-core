# PC：第二家服务商公开对照（20261011）

执行者 MG；候选 `f5f1ab8a7fb075a1a4c692a085161a8d518fe4c7`。工作目录 `/Users/cassia/Local/Code/iris-core-provider-compare`，仅提交此报告。任务命名日期为 2026-10-11；实际执行为北京时间 2026-10-12，原始时间记录为 UTC。

材料根目录（下文简称 R）：`/Users/cassia/Local/Code/iris-eval-artifacts/provider-compare-20261011/`。完整命令、起止时间、耗时和退出状态见 `common/results/run-index.json` 及各档 `results/<运行名>.json`；原始输出、数据库、匿名判分与计分全部在 R 内。没有修改产品代码、提示词、语料或评分说明，没有寻找或读取隐藏集。本报告不作模型切换结论。

**方法与限制**

- 两档使用同一候选、公开语料和冻结评分；GLM 为默认档，MiniMax-M3 使用 `IRIS_TEST_MODEL_PROFILE=chat2`。模型配置由产品通过用户指定的 `IRIS_TEST_MODELS` 路径加载，执行者未打开、复制或打印配置文件。GLM 实际 reasoning_effort=high；MiniMax 普通用途未显式设置。目标去重冻结 runner 对两档另设 high 和 60 秒测量预算；embedding 不变。
- 同一服务商的真实运行严格串行。首轮跨服务商学习同时使用共享 embedding 并出现 429；随后 GLM 独跑也出现 429，不能将全部限流归因于跨档同时运行。后续避免共享 embedding 的重叠请求。两档有效学习均保留原 runner 的 4 案例并发，未采用曾提出的串行案例适配。召回 MiniMax 使用 GLM 运行产生的相同固定向量缓存，SHA-256 见 `common/results/recall*-vector-cache.json`；不可直接比较两档召回整轮墙钟时间。
- 有效性审查先于质量计分。降级、超时、基础设施失败和冻结规则认定的未执行工作作废，并从头重跑；完整有效运行的质量失败保留。下列有效结果为首个完整有效运行，没有按质量挑选。全部作废尝试另表列出；反复失败仍未获得有效完整结果的项目不拼接、不填零、不给完整质量分。
- 学习每档两个全新子代理，共四名不同判分员；端到端每档两个全新子代理。每次均 `gpt-6-astra`、max、`fork_turns=none`，只提供匿名输入与冻结评分，不提供服务商身份。逐轮运行，先前判分目录以权限 000 封存；输入 SHA 校验、判分文件摘要及来源映射位于 `common/results/`。这是同一系统账号下的目录访问隔离与任务限制，并非不同操作系统账号。判分全部结束后已恢复 14 个目录的材料访问权限，667 份判分文件的摘要均与封存记录一致。双判由官方计分器取不利结果；参考项和图片均单判，不能视为双判一致。
- MiniMax 整理使用仓库外 `scripts/consolidation_profile.py`：仅绕过原 CLI 必须 high 的入口断言，保留 chat2 实际配置，逐例调用原 `case_run`，语料、提示词、预算和评分不变。适配说明及摘要见 `common/results/consolidation-entry-adaptation.json`。这一入口差异随结果披露。
- 质量判分只依据最终外部材料；外部判分调用和 token 不在产品 `model_calls` 中，记为未统计。缺失的服务商 usage 保持未知，合计仅表示已报告用量。

**逐项对照摘要**

| 项目 | GLM | MiniMax-M3 |
|---|---|---|
| 学习，双判 | 精确 86.20%；事实召回 84.70%；归属 88.34% | 精确 83.33%；事实召回 80.60%；归属 90.67% |
| 默认召回判断 | Recall@8 0.982510；nDCG@8 0.980843；0/217 降级 | 6 次尝试均作废，无有效完整质量分 |
| 端到端，双判 | 11/11；基础设施失败 0 | 8/11；基础设施失败 0 |
| 隐私 | 99 次查询，泄漏 0 | 99 次查询，泄漏 0 |
| 整理，单判参考 | 5 次尝试均作废，无有效完整质量分 | 误合并 0/5；识别 5/12；矛盾／依赖一致 10/18 |
| persona_v1，单判参考 | 有依据变化 19/20；拒绝 3/23 | 有依据变化 21/21；拒绝 2/23 |
| persona_v2 | 2 次超时作废，无有效完整分 | 3 次超时作废，无有效完整分 |
| 目标去重，60 秒测量预算 | 误合并 0/13；应合并识别 16/28 | 3 次候选顺序校验失败，无完整分 |
| 入口画像，单判参考 | 有依据句 18/25；forbidden 0；覆盖 11/31；拒绝 0/6 | 3 次生成／检查失败，无有效完整语义分 |
| 手绘图片，单判参考 | 必需点 13/16；另有 1 处额外错误描述 | 必需点 16/16 |

所有缺口均已原样运行并重跑；重复失败的同一配置下，本次未获得有效完整结果。未通过放宽预算、修改解析、挑选部分案例或拼接时间线补齐；这些限制是本次公开对照结果的一部分。

**学习（双判）**

每档 116 段、272 批、2011 条消息；五份公开语料全量运行。历史 holdout 只作历史回归分组，本次全部公开样本按 dev 使用。下表为官方 `learning-score` 按冻结规则取不利结果；百分数按两位小数展示。

| 分组 | 档位 | 段数 | 新记忆 | 精确率 | 事实召回 | 证据正确 | 归属正确 | 误记率 | 人物联系召回 | 目标召回 |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| all | glm | 116 | 326 | 86.20% | 84.70% | 90.49% | 88.34% | 0.31% | 81.82% | 100.00% |
| all | minimax | 116 | 300 | 83.33% | 80.60% | 88.33% | 90.67% | 0.00% | 59.09% | 71.43% |
| learning_v1 | glm | 36 | 57 | 87.72% | 89.74% | 94.74% | 89.47% | 0.00% | 100.00% | 100.00% |
| learning_v1 | minimax | 36 | 49 | 79.59% | 74.36% | 87.76% | 95.92% | 0.00% | 50.00% | 33.33% |
| learning_v2 | glm | 40 | 156 | 97.44% | 84.38% | 94.87% | 90.38% | 0.00% | 60.00% | 100.00% |
| learning_v2 | minimax | 40 | 152 | 89.47% | 81.25% | 91.45% | 89.47% | 0.00% | 20.00% | 75.00% |
| learning_v3 | glm | 10 | 37 | 89.19% | 85.71% | 89.19% | 91.89% | 0.00% | 80.00% | 100.00% |
| learning_v3 | minimax | 10 | 36 | 88.89% | 85.71% | 86.11% | 94.44% | 0.00% | 60.00% | 100.00% |
| learning_v4 | glm | 18 | 37 | 81.08% | 84.62% | 67.57% | 75.68% | 2.70% | 100.00% | 100.00% |
| learning_v4 | minimax | 18 | 37 | 75.68% | 84.62% | 81.08% | 89.19% | 0.00% | 80.00% | 66.67% |
| learning_v5 | glm | 12 | 39 | 41.03% | 73.33% | 89.74% | 87.18% | 0.00% | 66.67% | 100.00% |
| learning_v5 | minimax | 12 | 26 | 57.69% | 73.33% | 84.62% | 84.62% | 0.00% | 100.00% | 100.00% |
| learning_v1+v3+v4 | glm | 64 | 131 | 86.26% | 87.10% | 85.50% | 86.26% | 0.76% | 92.86% | 100.00% |
| learning_v1+v3+v4 | minimax | 64 | 122 | 81.15% | 80.65% | 85.25% | 93.44% | 0.00% | 64.29% | 66.67% |
| historical_holdout | glm | 16 | 63 | 96.83% | 84.38% | 96.83% | 90.48% | 0.00% | 50.00% | — |
| historical_holdout | minimax | 16 | 65 | 92.31% | 89.06% | 93.85% | 90.77% | 0.00% | 0.00% | — |

GLM / MiniMax：别名召回 100% / 66.67%，别名精确率 90.91% / 100%；近似重复均 0 对，再次确认 29 / 27，目标重复均 0。历史 holdout 不代表未见验收。

GLM 学习分歧 27/1633（1.65%），涉及 22 段：F001、F005、F006、F009、F010、L014、M202、M206、M208、M209、M210、M213、M215、M217、N303、N305、N308、N310、N311、V012、V033、V039。字段分布：{"correct_worth": 5, "evidence_correct": 13, "attribution_correct": 6, "fact_covered": 2, "link_covered": 1}。
MiniMax 学习分歧 18/1525（1.18%），涉及 15 段：F008、L029、M204、M209、M216、N305、N307、N308、N310、V002、V004、V011、V020、V021、V039。字段分布：{"correct_worth": 10, "evidence_correct": 4, "fact_covered": 2, "attribution_correct": 2}。

**学习运行诊断**

| 指标 | GLM learning03 | MiniMax learning02 |
|---|---:|---:|
| 完整案例／成功批次 | 116／272 | 116／272 |
| 批次耗时 P50／P95／最大（秒） | 6.438／25.575／67.274 | 12.526／48.330／94.654 |
| 整轮墙钟（秒） | 653.497 | 1218.694 |
| 180 秒预算超时／调用内重试 | 0／0 | 0／0 |
| 模型 JSON 修正调用／确定性引号修正批次 | 0／0 | 2／8 |
| invalid_output／内容拒绝 | 0／0 | 0／0 |
| `<think>` 开头剥离调用 | 0 | 274 |
| 长度截断调用／批次 | 0／0 | 1／1 |
| 已报告输出 token（含学习修正） | 139,645 | 574,489 |
| 已报告 reasoning token | 98,586 | 206,173（93 次未报告） |
| 学习及修正模型调用 | 272 | 274 |
| embedding 调用 | 619 | 590 |

MiniMax 的 F008 批次 1 首次输出达到 16,000 token 上限，62.098 秒，后续修正成功；保留这一完整运行及其质量结果。两个有效运行的输出用量均无缺失；推理用量缺失不计为真实零。外部判分的用量不在产品调用记录中。两档使用原 runner 的 4 案例并发，期间没有 CPU 压测或构建。

学习误记对应 GLM 的 M209 / memory 2（1/326），MiniMax 为 0/300。原始双判全部保留，报告未自行重新判定语义。学习材料与最终计分分别位于 `glm/runs/learning03/`、`glm/scored/learning03/` 和 `minimax/runs/learning02/`、`minimax/scored/learning02/`。

**召回判断（default_judged）**

| 指标 | GLM recall01（有效） | MiniMax |
|---|---:|---|
| 查询／有答案／无答案 | 228／162／66 | 第四轮完成全部查询，但发生一次降级，仍作废 |
| Recall@8 | 0.982510 | 无有效完整分 |
| nDCG@8 | 0.980843 | 无有效完整分 |
| relevant 精确率 | 165/167 = 98.8024% | 无有效完整分 |
| 无答案误返 | 1/66 = 1.5152% | 无有效完整分 |
| 降级率 | 0/217 prepare | 见逐次诊断，不能用部分运行作完整集对照 |
| 超时／限流／暂停降级 | 0／0／0 | 六轮共 3／0／0；另有 invalid_output 3 |
| 判断模型调用 P50／P95／最大（秒） | 1.573／2.745／3.557，191 次 | 下表为作废运行诊断 |

七份公开语料共 243 条固定记忆、228 条查询。GLM 有效轮 471 次 embedding、191 次判断均成功，墙钟 1145.267 秒。MiniMax 使用同一向量缓存，六轮没有新增 embedding 调用。MiniMax 第二／三／四轮分别在 recall_v2 D13、recall_v2 D10、recall_no_answer_v1 NA39 遇到 JSONDecodeError；未保留原始判断正文，不能认定为入口画像同样的 Markdown 围栏问题。

下表耗时为已完成 prepare 的 judgment.duration_ms（含队列等开销，排除 no_candidates），不是上表网络调用耗时，也不是有效完整集质量分。失败被观测后停止自己的进程；监测间隔中可能又完成若干查询。

| MiniMax 作废轮 | 已完成 prepare | 降级 | 原因 | 判断耗时 P50／P95 秒 |
|---|---:|---:|---|---:|
| recall01 | 86 | 1 | timeout ×1 | 2.602／4.469 |
| recall02 | 59 | 1 | invalid_output ×1 | 2.326／5.746 |
| recall03 | 55 | 1 | invalid_output ×1 | 2.431／5.554 |
| recall04 | 217 | 1 | invalid_output ×1 | 2.640／4.996 |
| recall05 | 174 | 1 | timeout ×1 | 2.432／4.170 |
| recall06 | 29 | 1 | timeout ×1 | 2.121／4.065 |

六轮已观察到 6/620 prepare 降级，只是重复且部分截断运行的诊断，不是 228 查询的完整集降级估计。逐查询材料、诊断与作废原因见两档 `runs/recall*/` 和 `common/results/recall-attempt-diagnostics.json`。

**端到端（双判）**

GLM **11/11**，MiniMax **8/11**。两档各 11 个脚本、12 个检查点，26 个双判决定；分歧均 0，基础设施失败均 0。MiniMax 的 E007、E009、E011 检查点返回记忆为空，要求事实未覆盖；E007/E009 判断诊断为 no_candidates，E011 判断删除 memory_id 1。这是有效运行的质量结果，未重跑。

| U04 脚本 | GLM 秒 | MiniMax 秒 |
|---|---:|---:|
| E001 | 4.909 | 9.414 |
| E002 | 7.186 | 32.639 |
| E007 | 11.892 | 未覆盖，无法计算 |
| E009 | 4.899 | 未覆盖，无法计算 |
| E010 | 13.911 | 37.622 |
| E011 | 16.194 | 未覆盖，无法计算 |

MiniMax 三个空值在计分器中 U04=false，均不是超时或基础设施失败；其余脚本不适用 U04。18 次学习调用的 P50/P95/最大分别为 GLM 4.763/13.104/14.459 秒、MiniMax 8.388/39.874/46.409 秒，超时均 0。整轮墙钟分别 205.794 / 365.257 秒。材料：两档 `runs/e2e01/`、`scored/e2e01/`。

**隐私**

两档 `visibility_v1` 均完成 99/99 查询，泄漏总数 **0**；其中 61 次带令牌 HTTP search，判断开启且无降级。冻结 runner 的 HTTP 使用 ASGI transport，不是独立 TCP 服务。6 对 no_merge 误合并均 0；预期记忆命中均 93/94，目标命中均 18/18，include_goals 违规均 0。no_merge 是冻结构造与配对计划检查，不是模型整理实验。两档均 179 次成功调用（161 embedding、18 recall_judge），墙钟 GLM 245.538 秒、MiniMax 257.968 秒。逐项结果在两档 `runs/visibility01/report.json`。

**整理（单判参考）**

GLM 五次尝试均无完整有效结果：一次超时，随后四次在 CS005 和／或 CS011 出现 `unresolved_conflict` 导致合并任务跳过或延期；冻结 `run_validity` 判为 work_skipped / unattempted_work。第五次在保存 28 例后确认 CS011 已无效并停止。没有给这些作废轮做质量判分。

MiniMax 第三次为首个冻结计分器认定的完整有效轮，40 例，单判及细节单判均由同一全新独立判分员完成。原 runner 标记 original_valid=false，但冻结计分器明确将 CS019、CS036、CS040 的 3 个已尝试、已完成的格式失败保留为质量结果，最终 valid=true；本次没有自行放宽规则。

| MiniMax consolidation03 指标 | 结果 |
|---|---:|
| 误合并 | 0/5 |
| 应合并识别 | 5/12 = 41.67% |
| 矛盾报告一致 | 3/10 = 30% |
| 依赖报告一致 | 7/8 = 87.5% |
| 矛盾／依赖合计一致 | 10/18 = 55.56% |
| 保护检查 | 4/4 |
| 相信程度上限／来源继承 | 5/5；5/5 |
| 合并细节丢失 | 0/5 |
| 细节误拒 | 3/12（CS004、CS006、CS008） |
| 完成的格式失败／安全拒绝 | 3／6 |
| 模型调用 | 51，全成功（不等于业务结果都正确） |
| 调用 P50／P95 | 8.448／18.076 秒 |
| 整轮墙钟 | 479.586 秒 |

完整有效轮中的识别、报告一致率及格式失败均保留。跳过任务会令整轮作废，可能影响可计分运行的分布，须同时阅读全部尝试的失败频次。材料 `minimax/runs/consolidation03/materials/`，分数 `minimax/scored/consolidation03/report.json`，单判匿名轮 `acb3a1df4cc22f80`。

**persona（单判参考）**

| persona_v1 指标 | GLM persona_v101 | MiniMax persona_v102 |
|---|---:|---:|
| 变化次数／有依据变化 | 20／19（95%） | 21／21（100%） |
| 候选／拒绝 | 23／3（13.04%） | 23／2（8.70%） |
| 小／中／大变化 | 13／6／1 | 13／5／3 |
| 变化中违规 | 虚构 1 | 0 |
| 被拒候选中的违规 | 0 | 单一日期泛化 1 |
| must_reflect | 23/23 | 22/22 |
| must_not 命中 | 1 | 0 |
| 变化程度判定合理 | 21/23 | 22/23 |
| 模型调用／有效轮超时 | 46／0 | 46／0 |
| 调用 P50／P95（秒） | 17.785／37.487 | 26.682／86.263 |
| 整轮墙钟（秒） | 895.507 | 1480.109 |

两档各 5 条时间线、30 个检查点；7 个未到生成时间的检查点不计拒绝。GLM 的有依据变化缺口涉及把暂停请求写成已经停止的状态；must_not 命中为管理员输入的“纸月亮”。各句理由在独立判分文件中。`glm/scored/persona_v101/`、`minimax/scored/persona_v102/` 为对应分数；不要把单判参考当作双判门槛。

persona_v2 每档目标为 5 条时间线、35 个检查点。GLM 两次 persona_check 超时，分别用时 2258.965 / 1965.125 秒；MiniMax 三次分别为 persona_check / persona_generate / persona_check 超时，用时 513.038 / 378.599 / 370.258 秒，均为原有 120 秒预算。GLM 第一轮 CLI 退出为 0，但 PT25（2027-09-07）不完整，仍作废；第二轮失败调用的时间线时钟为 2027-08-28 23:00 +08:00。两档均无有效完整轮，不提供有依据变化比例、完整拒绝率或拼接分数；详细调用和失败记录保留在两档 `runs/persona_v20*/`、`results/persona_v20*-calls.json`。

**目标去重（自动计分参考）**

GLM C2 运行 67/67 例：误合并 0/13，应合并识别 16/28（57.14%，按冻结规则含正确合并或正确标为可能重复）；不应合并中标可能重复 8/29（27.59%），不确定类 7/10（70%），两类合计 15/39（38.46%）。38 次调用，无降级或待复核，案例耗时 P50/P95 1.434/3.284 秒。两档均用冻结 runner 的 60 秒测量预算；该结果不是产品默认 5 秒同步预算的耗时验收。

MiniMax 三轮均因返回候选编号顺序与输入不一致而降级，无有效完整分数。GD003 输入 [3,1,2]、GD004 输入 [2,3,1]，模型均返回 [1,2,3]。正文是合法 JSON，失败为严格顺序校验；没有修改排序、提示词或验证器。

**入口画像（EP2，单判参考）**

GLM 有效轮 6/6 候选发布、拒绝 0/6；25 条可用句中 18 条有依据（72%），适用 must_cover 11/31（35.48%），冻结 forbidden 列表命中 0。原 27 条中 2 条确定性删除后不在可用句分母。逐条违规另外有无依据 5、单条消息泛化 4、个人信息或隐私 1，类别可重叠；EP003 第 4 条引用了个人办事安排。62/62 引文通过原文匹配只证明引文存在，不能证明概括有依据。冻结适用性清单仍为 `proposed_pending_planner_confirmation`，9 项不适用；本次未改标签。12 次模型调用、墙钟 317.790 秒。材料 `glm/runs/entry_profile01/`、分数 `glm/scored/entry_profile01/`。

MiniMax 首轮生成超时；第二、三轮各 5/6 生成失败，剩余候选的检查也格式失败。各轮 7 个模型正文中 6 个带 Markdown 围栏，严格 JSON 解析失败；开头 think 块已经被剥离。没有有效完整语义分数。`model_calls` 在第二、三轮都记录网关成功，业务层解析失败需另报，不能从成功调用数推断功能成功。最小格式复现记录见 `common/results/entry-profile02-format-investigation.json`、`entry-profile03-format-investigation.json`；未修改解析器或提示词。

**图片（手绘小样本，单判）**

执行者逐张手写 SVG 并渲染为 PNG，4 张，无下载、无真实个人信息；调用前冻结 16 个必需点、8 个禁止点和文件摘要（`common/images/frozen.json`、`criteria.json`）。MiniMax 覆盖 16/16 必需点，GLM 覆盖 13/16。按每张全部必需点覆盖计，分别 4/4、1/4；不能将遗漏概括为三张均事实错误。冻结禁止点均 0；GLM 另在 P03 描述不存在的“数字 5 水印”，独立理由中有记录。

| 图片 | 内容 | GLM 秒 | MiniMax 秒 | 独立判分记录 |
|---|---|---:|---:|---|
| P01 | 三种标字几何形状及位置 | 2.536 | 2.515 | 两档完整 |
| P02 | RECEIVE→STORE→REPLY 与 WAIT 支线 | 2.500 | 3.146 | GLM 漏主箭头向右；MiniMax 完整 |
| P03 | MON 2、TUE 5、WED 3 | 2.859 | 2.337 | GLM 漏周二最高、另有水印描述；MiniMax 完整 |
| P04 | 右柄杯、星形、蒸汽与便笺 | 3.334 | 3.214 | GLM 漏右侧杯柄；MiniMax 完整 |

两档均 4 次成功、无超时或降级，墙钟 12.262 / 12.154 秒；输出 token 341 / 816。实际模型分别 glm-5.3-flash / MiniMax-M3。材料与独立单判分数在两档 `runs/images01/`、`scored/images01/report.json`。

**调用错误分类（包括所有作废尝试）**

统计范围为所有 42 次真实运行中保留下来的 model_calls，共 4,660 条；有效和作废分开列。一个调用按其持久化类别计一次，不包含独立外部判分用量。

| 范围 | GLM | MiniMax |
|---|---:|---:|
| 全部已记录调用 | 2615 | 2045 |
| 成功 | 2550 | 2024 |
| 可重试 | 65 | 15 |
| invalid_output | 0 | 6 |
| item_error／配置错误／账户问题／内容拒绝 | 均 0 | 均 0 |
| HTTP 429 | 62 | 7 |
| 超时 | 3 | 8 |
| 开头 think 剥离 | 0 | 1173 |
| 有效运行调用（全成功） | 1910 | 1216 |
| 作废运行调用 | 705 | 829 |

配置错误和账户问题的调用清单均为空，因此没有此类原因可列。MiniMax 的 7 次 429 全部来自仍共用的 embedding 服务，不是 MiniMax 对话端点；GLM 的 62 次来自两次学习作废运行。下表省略的 item_error、配置错误、账户问题、内容拒绝四列，在每个用途、两档均为 0。

| 用途 | GLM 成功 | GLM 可重试 | GLM 无效输出 | MiniMax 成功 | MiniMax 可重试 | MiniMax 无效输出 |
|---|---:|---:|---:|---:|---:|---:|
| consolidation_conflict | 100 | 1 | 0 | 62 | 0 | 0 |
| consolidation_conflict_repair | 8 | 0 | 0 | 5 | 0 | 0 |
| consolidation_dependency | 30 | 0 | 0 | 24 | 0 | 0 |
| consolidation_dependency_repair | 0 | 0 | 0 | 11 | 0 | 0 |
| consolidation_entry_profile_check | 6 | 0 | 0 | 3 | 0 | 0 |
| consolidation_entry_profile_generate | 6 | 0 | 0 | 15 | 1 | 0 |
| consolidation_merge | 85 | 0 | 0 | 51 | 0 | 0 |
| consolidation_merge_repair | 21 | 0 | 0 | 6 | 0 | 0 |
| goal_dedup_judge | 38 | 0 | 0 | 5 | 0 | 3 |
| image_understanding | 4 | 0 | 0 | 4 | 0 | 0 |
| learning | 373 | 26 | 0 | 317 | 0 | 0 |
| learning_repair | 0 | 0 | 0 | 2 | 0 | 0 |
| memory_embedding | 484 | 26 | 0 | 367 | 4 | 0 |
| persona_check | 75 | 2 | 0 | 37 | 3 | 0 |
| persona_generate | 84 | 0 | 0 | 43 | 1 | 0 |
| recall_eval_embedding | 632 | 0 | 0 | 161 | 0 | 0 |
| recall_judge | 221 | 0 | 0 | 582 | 3 | 3 |
| retrieval_query | 383 | 10 | 0 | 329 | 3 | 0 |

原始安全调用明细按运行存于 `results/*-calls.json`，汇总见 `common/results/all-call-categories.json`。MiniMax 6 个网关 invalid_output 为目标去重候选顺序 3 次和召回 JSON 解析 3 次；入口画像围栏、整理校验失败发生于业务处理阶段，已在各节另列。不能把网关 success 视为业务成功。

**作废运行**

共 28 次：GLM 9、MiniMax 19。均不进入有效质量对照；完整记录未删除。没有为提高质量分重跑有效结果。检测到明确超时、降级或冻结有效性失败后，以 SIGTERM 停止自己的进程组所产生的 -15 退出不是额外基础设施故障；原始原因列在下表。

| 档 | 运行目录（相对材料根目录） | 秒 | 已记录调用 | 作废原因 |
|---|---|---:|---:|---|
| MiniMax | `minimax/runs/learning01` | 132.578 | 96 | HTTP 429：7 次（模型调用记录） |
| GLM | `glm/runs/learning01` | 132.578 | 184 | HTTP 429：32 次（模型调用记录） |
| GLM | `glm/runs/learning02` | 125.799 | 161 | HTTP 429：30 次（模型调用记录） |
| MiniMax | `minimax/runs/persona_v101` | 513.568 | 12 | 超时：persona_check ×1 |
| GLM | `glm/runs/consolidation01` | 477.290 | 51 | 超时：consolidation_conflict ×1 |
| MiniMax | `minimax/runs/recall01` | 317.516 | 78 | 超时：recall_judge ×1 |
| MiniMax | `minimax/runs/recall02` | 233.983 | 57 | invalid_output：1 次；严格 JSON 解析失败 |
| GLM | `glm/runs/consolidation02` | 419.587 | 52 | CS011：unresolved_conflict，合并任务跳过／延期 |
| MiniMax | `minimax/runs/recall03` | 210.051 | 53 | invalid_output：1 次；严格 JSON 解析失败 |
| MiniMax | `minimax/runs/goal_dedup01` | 13.916 | 3 | invalid_output：1 次；候选 ID 顺序违反约定 |
| MiniMax | `minimax/runs/goal_dedup02` | 22.664 | 2 | invalid_output：1 次；候选 ID 顺序违反约定 |
| MiniMax | `minimax/runs/goal_dedup03` | 10.786 | 3 | invalid_output：1 次；候选 ID 顺序违反约定 |
| MiniMax | `minimax/runs/entry_profile01` | 297.906 | 5 | 超时：consolidation_entry_profile_generate ×1 |
| MiniMax | `minimax/runs/entry_profile02` | 286.444 | 7 | 5/6 生成失败；6/7 正文带 Markdown 围栏，含剩余候选的检查 |
| GLM | `glm/runs/consolidation03` | 388.628 | 54 | CS005：unresolved_conflict，合并任务跳过／延期 |
| MiniMax | `minimax/runs/persona_v201` | 513.038 | 12 | 超时：persona_check ×1 |
| MiniMax | `minimax/runs/consolidation01` | 493.444 | 48 | CS005：unresolved_conflict，合并任务跳过／延期 |
| MiniMax | `minimax/runs/entry_profile03` | 279.281 | 7 | 5/6 生成失败；6/7 正文带 Markdown 围栏，含剩余候选的检查 |
| GLM | `glm/runs/consolidation04` | 421.785 | 54 | CS005、CS011：unresolved_conflict，合并任务跳过／延期 |
| GLM | `glm/runs/persona_v201` | 2258.965 | 60 | 超时：persona_check ×1；PT25 / 2027-09-07；CLI 正常结束但时间线不完整 |
| MiniMax | `minimax/runs/consolidation02` | 559.914 | 60 | CS011：unresolved_conflict，合并任务跳过／延期 |
| MiniMax | `minimax/runs/persona_v202` | 378.599 | 8 | 超时：persona_generate ×1 |
| MiniMax | `minimax/runs/persona_v203` | 370.258 | 6 | 超时：persona_check ×1 |
| GLM | `glm/runs/persona_v202` | 1965.125 | 55 | 超时：persona_check ×1 |
| MiniMax | `minimax/runs/recall04` | 775.475 | 191 | invalid_output：1 次；严格 JSON 解析失败 |
| MiniMax | `minimax/runs/recall05` | 580.033 | 152 | 超时：recall_judge ×1 |
| GLM | `glm/runs/consolidation05` | 218.419 | 34 | CS011：unresolved_conflict，合并任务跳过／延期 |
| MiniMax | `minimax/runs/recall06` | 114.536 | 29 | 超时：recall_judge ×1 |

参考项中的固定格式／顺序兼容问题，以及默认预算下重复超时，是本次观察到的运行限制。执行者未修改代码、提示词、评分、冻结预算或标签来获得可计分结果。

**观察到的差异与结果边界**

- 在有效公开学习双判中，MiniMax 精确率比 GLM 低 2.86 个百分点、事实召回低 4.10、证据正确低 2.16，归属正确高 2.32；误记分别 0/300 和 1/326。语料分组并非同向，v5 精确率 MiniMax 57.69%、GLM 41.03%。不据此作模型选择。
- MiniMax 学习批次 P95 为 GLM 的约 1.89 倍，已报告输出 token 约 4.11 倍；两档有效学习均未达到 180 秒预算，MiniMax 保留了两次 JSON 修正及一次长度截断后恢复。
- MiniMax 端到端有三个事实未覆盖脚本，基础设施本身完整；其 persona_v1 和手绘图片覆盖在本次单判中高于 GLM。隐私固定检查两档均无泄漏。以上只描述这次公开样本。
- 完整正常路径召回、部分参考项仍缺成对有效结果。重复失败、单判、手绘小样本、共享 embedding、实际 reasoning_effort 不同，以及整理入口适配，均限制可作出的推断。公开材料不替代规划者的隐藏集。

**测试与提交范围**

| 检查 | 结果 | 墙钟秒 |
|---|---|---:|
| `uv run pytest`，Python 3.13 | 2534 passed，4 warnings | 873.055 |
| `uv run --locked --isolated --python 3.12 pytest` | 2534 passed，4 warnings | 762.787 |

两个版本依次执行且在真实模型评测之前完成；warnings 为 jieba 转义和 Starlette/httpx 弃用。日志在 `common/logs/pytest313.log`、`pytest312.log`。此后只整理仓库外材料和本报告，未修改产品输入或重建评测虚拟环境；每次运行均记录输入指纹未变；时间记录核对同服务商无重叠。没有进行 CPU 压测。提交前核对 `git check-ignore test-models.toml`、完整暂存 diff 与密钥模式；只提交本报告，不提交模型配置、原始输出或判分。

**判分与材料索引**

学习双判匿名轮：GLM `43135232ae27cddf` / `30ad20701d816aeb`；MiniMax `00f9c4083bb3db7a` / `0f751458a795aede`。端到端：GLM `9b20ed52979b06a6` / `2dae162091385228`；MiniMax `f8c1341c9a8f1eec` / `5aa2b3b25783bd81`。目录均为 `R/blind/<编号>/judgments/`，映射、输入摘要、原始分歧及封存摘要在 `R/common/results/`；公开报告披露映射发生在判分完成之后。

原始外部材料位于各有效运行的 `output/judging-materials-*/`；整理为 `materials/`，入口画像为运行子目录的 `judging-materials/`。精确绝对路径见 `common/results/blind-*.json` 与各 `*-validity.json`。全部命令、有效性、逐用途调用和作废原因由 `run-index.json`、`completed-summary.json`、`all-call-categories.json`、`void-attempts-summary.json` 汇总，均位于 `common/results/`。
