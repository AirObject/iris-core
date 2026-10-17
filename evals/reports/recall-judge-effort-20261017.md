# RJ-B1：召回判断推理档位与延迟（2026-10-17）

推荐只将 `recall_judge.reasoning_effort` 设为 `low`，保留 `[chat]` 的 `high`。插件上线观察先采用 `judge_budget_seconds=7`、`prepare_timeout=12`。两档各跑完七份公开语料：low 的判断 P95 为 2.214 秒（high 3.657 秒），各语料 Recall 与 relevant 精确率均不下降，无答案误返均为 0；合计 nDCG 下降 0.0013，主要来自更完整召回后的顺序变化。符合 AGENTS「召回与验证」守则，无须申请退化例外。此结论限本轮公开 dev，不代替隐藏验收或线上延迟验证。

**运行口径与复现材料**

基线为任务启动时 fetch 得到的 `origin/main`：`189cce89c04a1e219c82c2ca6b7f4abb4e39b1da`；当时本地 main 与之相同。独立分支 `eval/recall-judge-effort`，worktree 为 `/Users/cassia/Local/Code/iris-core-recall-judge`。真实实验时间 2026-10-17 18:39:15—19:08:11（Asia/Shanghai），先 low 连通探测，再 high 全轮、low 全轮。产品代码、提示词、语料、迁移、默认设置均未修改；仓库仅新增本报告。

提交前观察到 origin/main 已前进至 `d9fc539`（#122，询问过期设置）。本报告的运行材料、源码指纹及双版本测试均绑定上列 `189cce8` 基线。

方舟文档对 GLM-5.3-Flash 的实际档位映射为 low／high／max，其中 medium 与 high 等效，因此本次只比较有不同实际含义的 high 和 low；该模型始终启用思考。文档依据为[方舟深度思考说明](https://docs.volcengine.com/docs/ark/deep-thinking?lang=zh)（2026-10-17 查询）。当前配置端点的 low 探测成功，响应报告了 13 个推理 token；未发送关闭推理参数。探测不计入下面 191 次正式判断。

原 `IRIS_TEST_MODELS=/Users/cassia/Local/Code/iris-core/test-models.toml` 通过产品配置加载器用于连通检查和本次运行；`uv run iris models check` 的 chat、embedding 均成功，只查看成功结论。临时 `configs/high.toml`、`configs/low.toml` 只用 `api_key_env` 引用进程环境变量，密钥不写文件。程序逐项核对 `[chat]` 以及其他用途配置不变，仅为 recall_judge 单独设置档位。

旧 `m2-recall-judge/run-review-1/`、旧执行脚本和主仓库 `data/recall-embeddings.db` 在本机均不存在。本次从公开语料重新生成 471 个 embedding，全部成功；low 复用同端点、模型、2048 维及完整输入文本的缓存，新增 embedding 请求为 0。

采用产品 `run_recall_eval` 的 `default_judged`、全部七份冻结语料、243 条固定记忆和 228 条查询；各语料隔离入库。217 条 prepare 中 191 条实际判断、26 条无候选，另 11 条 search 不判断；历史 split 只保留为分组，全部公开样本按 dev 使用。K=8、支持分 50、recall_judge_v1、召回参数均沿用基线。没有重新标定或修改标签。

外部执行脚本只增加留档和首错停止：embedding 串行预取；每个 prepare 前间隔 1 秒（预算外）；正式判断使用产品 10 秒上限。任何一次非成功调用或 prepare 降级立即作废当前轮并停止整个实验，不重试或继续下一档。两轮复用建库后的同一快照，避免随机主体 ID 导致 about 顺序变化；191 对实际请求除 reasoning_effort 外逐项完全相同，输入 token 总量也相同。调用记录和响应正文留在仓库外，推理只保留出现标志、长度和用量，不保存全文。

质量由既有固定标注和 `recall_metrics` 计算，未另调模型判分。Recall／nDCG 对有答案查询计算，包含人物要点；relevant 精确率按标注正确的 relevant 返回数／全部 relevant 返回数合并计数；无答案误返只检查 reason=relevant。合计按全部查询重新计算，非七个均值的简单平均。

**质量：每格为 high → low**

| 语料／分组 | 查询数 | Recall@8 | nDCG@8 | relevant 精确率 | 无答案误返 |
| --- | ---: | --- | --- | --- | --- |
| recall_v1 | 38 | 0.9667 → 0.9667 | 0.9843 → 0.9843 | 0.9722 → 0.9722 | 0/8 → 0/8 |
| recall_v2 | 66 | 0.9733 → 0.9733 | 0.9490 → 0.9490 | 1.0000 → 1.0000 | 0/16 → 0/16 |
| recall_conversation_v1 | 24 | 0.9167 → 0.9444 | 0.9399 → 0.9283 | 1.0000 → 1.0000 | 0/6 → 0/6 |
| recall_short_terms_v1 | 16 | 1.0000 → 1.0000 | 1.0000 → 1.0000 | 1.0000 → 1.0000 | 0/4 → 0/4 |
| recall_conversation_v2 | 24 | 1.0000 → 1.0000 | 1.0000 → 1.0000 | 1.0000 → 1.0000 | 0/6 → 0/6 |
| recall_conversation_v3 | 20 | 1.0000 → 1.0000 | 1.0000 → 1.0000 | 1.0000 → 1.0000 | 0/4 → 0/4 |
| recall_no_answer_v1 | 40 | 1.0000 → 1.0000 | 1.0000 → 1.0000 | 0.9474 → 1.0000 | 0/22 → 0/22 |
| 合计 | 228 | 0.9763 → 0.9794 | 0.9747 → 0.9734 | 0.9880 → 0.9940 | 0/66 → 0/66 |
| 原三集合计（子集） | 128 | 0.9609 → 0.9660 | 0.9581 → 0.9560 | 0.9901 → 0.9902 | 0/30 → 0/30 |
| 对话中准备（子集） | 88 | 0.9750 → 0.9833 | 0.9820 → 0.9785 | 1.0000 → 1.0000 | 0/28 → 0/28 |

合计 Recall +0.003086、nDCG −0.001281、relevant 精确率 +0.006024，误返均为 0/66。high 为 164/166 个 relevant 返回标注正确，low 为 165/166。对话中准备 Recall +0.008333、nDCG −0.003458、精确率不变，误返均为 0/28。

七份语料、原三集合计及对话中准备均满足更严格的 Recall 不下降；精确率均未下降，误返没有增加，守住 AGENTS 的精确率降幅不超过 0.03、误返最多增加一条的要求，也未触及 evals/README.md 的单语料 Recall 下降超过 0.05 需规划者决定的线。nDCG 的下降单列，不能说所有指标都改善。

228 条查询中仅两条返回不同，其余 226 条的 ID／顺序／reason 完全一致：

| 查询 | high → low | 支持分变化与影响 |
| --- | --- | --- |
| recall_conversation_v1 / T11 | C10 → C09、C10 | C09 20→60，C10 95→90；补回等级 1 的 C09，等级 3 的 C10 排到第二。本集 Recall +0.027778，nDCG −0.011528。判断不重新排序。 |
| recall_no_answer_v1 / NA03 | N09、N41 → N09 | N41 65→30，N09 均为 95；去掉未标注相关的 N41，本集精确率 18/19→18/18。该题有答案，故不改变无答案误返数。 |

**判断耗时与预算**

单位为秒，来自 model_calls.duration_ms；包括实际 HTTP 等待与响应解析／校验，不含排队及随后记账。P50／P95 用 NumPy 线性百分位。只统计实际判断，search 和无候选项不作为零耗时样本。

| 语料 | 每档调用数 | P50 high → low | P95 high → low | 最长 high → low |
| --- | ---: | --- | --- | --- |
| recall_v1 | 35 | 1.6570 → 1.0950 | 2.6607 → 2.0299 | 2.7710 → 3.3510 |
| recall_v2 | 58 | 1.7555 → 1.1305 | 3.5705 → 3.0681 | 5.3560 → 4.0010 |
| recall_conversation_v1 | 19 | 1.7460 → 1.2040 | 3.0557 → 1.7905 | 3.6830 → 2.1640 |
| recall_short_terms_v1 | 4 | 2.3230 → 1.1030 | 3.4946 → 1.3225 | 3.5750 → 1.3530 |
| recall_conversation_v2 | 20 | 1.4940 → 1.0235 | 2.7945 → 1.6906 | 3.3160 → 1.7220 |
| recall_conversation_v3 | 19 | 1.8870 → 1.1550 | 4.8303 → 3.2359 | 6.0030 → 4.1800 |
| recall_no_answer_v1 | 36 | 1.8850 → 1.2025 | 3.5133 → 2.4192 | 4.8810 → 3.8330 |
| 合计 | 191 | 1.7330 → 1.1280 | 3.6565 → 2.2135 | 6.0030 → 4.1800 |

| 档位 | 5 秒预算：估算超阈值 | 7 秒预算：估算超阈值 | 10 秒预算：实际超时 | 429 | prepare 降级 |
| --- | ---: | ---: | ---: | ---: | ---: |
| high | 2/191（1.05%） | 0/191 | 0/191 | 0 | 0/217 |
| low | 0/191 | 0/191 | 0/191 | 0 | 0/217 |

5／7 秒列是同一轮 10 秒预算下的逐调用耗时阈值统计，没有重新发出 5／7 秒请求，也不模拟连续错误暂停后的级联降级。按整个 judgment.duration_ms 重算，计数仍为 high 的 2／0 和 low 的 0／0。它们是预算风险估计，不是线上短预算实测超时率。10 秒列则为调用记录中的实际 timed_out。

high 超过 5 秒的两次为 recall_v2/H06（5.356 秒）和 recall_conversation_v3/V05（6.003 秒）。包含材料组织、排队、记录和健康状态更新的整个 judgment，high 的 P50／P95／最长为 1.805／3.734／6.103 秒，low 为 1.202／2.281／4.270 秒；此整体计时也包含外部留档的少量开销。

| 档位 | 输入 token | 输出 token | 其中推理 token（已含在输出） |
| --- | ---: | ---: | ---: |
| high | 137348 | 11253 | 7710 |
| low | 137348 | 4392 | 904 |

low 的调用 P50 下降约 34.9%，P95 下降约 39.5%。本轮各档只运行一次，顺序固定为 high 后 low；供应商负载、缓存和随机性可能影响差异，不能把全部差异都归因于档位。公开输入与真实群聊不同，查询 embedding 在评测中预缓存；这不是插件端到端 prepare 的 P95。尤其不能由本表断言线上原 34% 超时会降为 0。

**现行暂停机制与建议改动（仅报告，未实施）**

结论是会计入，但须区分 HTTP 已发出与纯本地排队两种情况。`models.py:416—454` 将宿主预算转为共享 deadline，HTTP 超时参数使用剩余预算；`models.py:532—556` 将 FutureTimeout、HTTP 客户端超时及截止后才收到的成功结果记为 retryable、timed_out=1，并调用 health.observe。`model_health.py:215—229` 每次累加 consecutive_errors，第三次进入 temporarily_unavailable，首次 60 秒后探测。计数属于 recall_judge 用途，不暂停 chat；跳过判断不产生成功观测，也不重置计数。召回判断本身每次只有一次尝试；探测走独立用途预算，成功可恢复。

因此“服务商在 5 秒时还未回答”与“服务商／网络故障”会进入同一错误计数路径；错误摘要虽可能是 total timeout 或 ReadTimeout，仍没有宿主预算来源的语义区分。纯排队满／排队耗时用尽，以及发请求前预算已经耗尽，则在 observe 之前返回，不增加计数。迟到 HTTP 工作线程继续占用准入槽位直到结束，建议继续保持此约束以限制在途负载。

外部假网络复现走原 Gateway 和 ModelHealth，使用假时钟，不发真实请求：

| 场景 | 三次后的状态 | 错误计数 | 60 秒后成功探测 |
| --- | --- | --- | --- |
| 宿主 5 秒预算，HTTP 200 在假时钟 5.01 秒返回 | temporarily_unavailable | 1→2→3 | 恢复 normal |
| HTTP 客户端 ReadTimeout | temporarily_unavailable | 1→2→3 | 恢复 normal |
| 服务商 HTTP 504 | temporarily_unavailable | 1→2→3（timed_out 字段为 0） | 恢复 normal |
| 本地队列连续耗尽 5 秒预算 | normal | 始终 0，无模型调用记录 | 不需探测 |

建议规划者安排独立实现任务：在 `src/iris/models.py` 显式区分 caller_budget、用途内部超时和上游错误来源；在 `src/iris/model_health.py` 将宿主主动缩短预算的到期作为中性结果，不累加服务商错误，也不冒充成功清零。网络连接错误、服务商 408／504、429 和账户错误沿用相应健康规则。保留当前降级、短预算、有界排队、迟到结果丢弃与槽位约束，不能因区分原因而延长宿主等待。

同时在判断诊断／运行记录里单列预算、排队和网络耗时及超时来源，并在 `tests/test_recall_judge.py`、`tests/test_model_health.py` 覆盖三次宿主预算到期不暂停、真正网络错误仍暂停、混合事件计数以及迟到请求占槽位。诊断字段和持久化方案需规划者审查 v1 兼容性后安排；本报告不改接口或迁移。

**推荐配置与上线观察边界**

推荐 `recall_judge.reasoning_effort=low`、`judge_budget_seconds=7`、`prepare_timeout=12`；其他用途和 chat high 不变。落地时保留现用端点、模型和凭据引用，保存完整的独立 recall_judge 用途配置并设置 low。本轮 low 的整个 judgment 最长 4.270 秒，5 秒已能容纳全部样本；考虑真实负载未知且线上原 5 秒截断严重，建议上线观察期间保留额外 2 秒判断余量。

prepare_timeout 额外保留 5 秒：查询 embedding 上限为 2 秒，剩余用于接收消息等待、HTTP 往返及本地处理。已只读核对 iris-connectors `78633912a0466f9787724f90173c6c6bf3335d26` 的 bridge.py:155，插件总预算从 receive 前开始，包含等候消息提交，不能简单与 judge 预算设成相同。12 秒是上线观察用的工程余量，不是经本次端到端测量证明的上界；持续积压时仍可能用尽。

本次只提出组合，不写线上设置。上线先复核既有超时、排队、prepare 总耗时与降级记录；若落实上述原因区分，再单列 caller_budget 和上游失败。上线效果仍需真实日志确认。

**作废运行与验证**

作废运行：无。high、low 两轮均有效，正式判断成功 382/382，429=0、实际超时=0，prepare 降级 0/434；另一次 low 能力探测和 471 次 embedding 均成功。没有超时后重跑、按结果挑选或追加模型调用。

Python 3.13.13（`uv run pytest`）：3713 passed、8 warnings，1153.36 秒；随后 Python 3.12.12（`uv run --locked --isolated --python 3.12 pytest`）：3713 passed、8 warnings，1152.90 秒。两个版本顺序运行，`.venv` 保持 Python 3.13；自己的 `.pytest-tmp` 已删除。四项假网络健康机制复现全部通过。测试在真实评测结束后运行；产品源码及七份语料哈希再次核对一致。

全部完整材料在 `/Users/cassia/Local/Code/iris-eval-artifacts/RJ-20261017/`：manifest.json、两份临时环境变量引用配置、rj_evaluate.py、rj_analyze.py、rj_health_repro.py、calls.jsonl、high/ 与 low/ 的完整 JSON／正文、summary.json、health-reproduction.json、embedding 缓存与语料快照、执行及测试日志。没有复制虚拟环境或 node_modules，未另做 CPU 压测。旧材料缺失不影响本次两轮自包含对照。

源码 SHA-256：`fe29aa8d21919100628f755c0b60362e2ce7fe70f4d9c15cb55ff60487c1e965`（两轮前后相同）；提示词 SHA-256：`0260a07b58977c42435a3379dbdb23455a653a53060c82df2e773abdbf1f2ed3`；外部执行脚本 SHA-256：`fddd9189f865cca28343dd70c5fccf07174784e7c80f5764d543d88768b851fa`。七份语料的原文件哈希在 manifest.json 中。执行脚本拒绝覆盖既有 manifest；未来若规划者安排新实验，须用新的材料目录保留独立运行，不能覆盖本次材料。
