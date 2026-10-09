# CO-01 只报告与标注改版：可以开始验证

实现提交：`94db8fce28d21808660e2780905b915f601efcac`；本次对照基线 main：`6be0bb0b5a88701611513cfe1d471b14769cff44`。本回合真实模型调用为 **0**，未读取模型密钥配置或隐藏验收集。当前配置为 broad + report_only_v1，单独的冻结清单见 [report_only_v1.json](report_only_v1.json)。新配置的真实双轮、双判结果尚未产生。

## 实现

合并恢复第一轮选中的 broad：冻结 `3d31c95b3fa4ba9b64f43bcb9b396e63916a5e7a` 的完整 `consolidation_pair_v1.md` 逐字保留，候选阈值与排除规则不重选。保留语义较完整的既有正文，等同完整时优先较早；仍以 deleted + merged_into 表示永久占位。发现矛盾后，再用只报告提示词生成具体结论及标注；第一阶段判断单独持久化，中断或预算耗尽后不重复分类。两阶段及格式修正均计入每次最多 50 次的调用预算；模型调用在事务外，全部读取对象在落地前复核修订号。

正文确定性校验只在合并应用前执行。保留学习的主体编号、数字／否定序列辅助函数（只读导入），继续拒绝性别代词、残留编号、非法结构化 ID 和正文改写。不再把 report/annotation 中的 keep_id、superseded、derived_from 等协议词当作主体；合并正文的合法入口 ID 包括 poem-live、stage-16、M12-live、P1，不会误认成残留编号。普通英文词不当作主体 ID。“吉他／其他”不误判为性别代词。

合并禁止生成第三份正文，结果须逐字等于选中的输入正文，数字和否定也不能改变。取消把摘要的数字／否定序列与整段消息强行等同的检查：消息可包含其他句子，摘要可从时间元数据补日期。既有数字／否定冲突排除和模型依据来源判断保持。离线校验兼容检查中，公开集 12 个应合并对的较完整既有正文均未被误拦截；这只是校验兼容性，不是本轮模型识别率。

矛盾与依赖的正文、相信程度写入代码已删除，包括 conservative_belief 和根据历史词／笔误词降低相信程度的分支；不更改生命周期，也不追加保留强度扣减。旧 rewrite/conservative 提示词及历史清单保留供追溯，运行入口不再接受这些候选，旧设置也不能重新启用写回。原 broad 提示词产生的旧式改写草稿不落地，只取分类决定；后续只报告调用拒绝 content/belief 字段。

## 标注与接口

迁移 015 保持历史字节；016 扩充 consolidation_annotations，并将设置切到 broad + report_only_v1、废止未完成的旧写回决定。新标注保存 memory_id、kind、text、evidence、related_memory_ids、superseded_by、memory_revision、run_id/work_id、去重键、创建及清除时间／操作者。kind 为 disputed、insufficient_support 或 superseded；superseded_by 必须是材料中另一条合法记忆。

同一配对、结论类别、替代目标／失效源头去重，理由换措辞不会重复创建。管理员清除保留去重记录，因此同一结论不会在下次整理自动复现。标注不增加记忆修订号，不干扰学习和反馈；之后正文等修订发生，标注仍在，详情按保存的修订号返回 modified_since_annotation=true、status_label=“标注后已修改”。置顶与人工正文编辑记忆同样可以标注。

GET /admin/api/memories/{id} 增加 consolidation_annotations，含来源报告地址、结论、理由和双方来源摘录。DELETE /admin/api/memories/{id}/annotations/{annotation_id} 接受 expected_revision，沿用管理员会话和 CSRF，保存操作记录。prepare/search 在选取、预算及召回判断结束后，由 people.annotate_memories 附加同名字段；不改名额、排序或判断请求。这里是管理 API 展示和清除，未修改前端。

评测导出保留完整标注，并增加实际持久化的 decision、conclusions、evidence、source_excerpts。冻结 scoring_v1 不改；[report_only_scoring.md](report_only_scoring.md) 落实规划者的新范围，独立绑定材料指纹，明确矛盾／依赖依照报告与标注判定，合并规则及双判取不利结论不变。

## 验证

先添加假模型测试并确认旧实现失败，再实现新行为；同时更新已退役写正文路径的断言。覆盖保护、管理员清除与去重、编辑后标注状态、来源报告、两阶段中断／预算、并发修订跳过、旧数据库升级与旧选项禁用、非法替代目标、冻结 broad 请求、向量／FTS 不变、评测材料与指纹。原 D01—D06、占位、暂停、依赖三种结果和 R10 单元检查继续通过。

按顺序执行：Python 3.13.15 全套 **1598 passed**（200.22s）；隔离 Python 3.12.14 全套 **1598 passed**（204.22s），各有 4 项既有第三方警告。uv build 成功，wheel 中当前运行代码、015/016 迁移和三份在用整理提示词与源码逐字一致。两版测试未并行，项目 .venv 仍为 3.13。

学习：未改 compare_learning_requests.py 的 worker，只在仓库外包装中加入第五份公开语料。与 main 对照 **116 段、272 批，messages/purpose/max_tokens 逐字零差异**。召回：判断关闭、未运行整理，七份公开语料的混合检索和全文降级共 **456 次对照、2216 条返回记忆**，移除新增 consolidation_annotations 字段后所有字段、ID、顺序与 reason 逐项一致。另有真实标注存在时的假模型测试验证追加字段和清除行为。

40 例假模型完整运行、导出与离线材料加载流程通过；仅作流程验证，不计质量分。无语义决定的基础流程有 43 次假调用（35 个配对、8 个依赖），真实运行另有矛盾只报告调用及可能的格式修正。

R10 使用原 benchmark_retrieval.py --default-config，仓库外包装加入 persona、当前状态和目标，每组 5 次预热、60 次采样。本地模拟网关，不含外部模型网络；在全部测试与构建完成后独立计时。

| 记忆数 | 查询 | prepare P95 ms | HTTP P95 ms |
| --- | --- | --- | --- |
| 5000 | 不点名 | 27.21 | 32.10 |
| 5000 | 点名 | 31.26 | 41.51 |
| 50000 | 不点名 | 123.42 | 145.30 |
| 50000 | 点名 | 148.85 | 151.12 |

四组 prepare P95 均低于 500ms。

## 下一窗口

只运行最终配置，在 consolidation_v1 上完整两次，每次由两个全新、互不可见且未参与实现的 gpt-6-astra max 子代理判分，分歧取不利结论。合并固定 broad 不再选型；网络降级、暂停或预算未完成的运行按原规则作废。核对误合并为 0、保护全部通过，报告矛盾／依赖一致率、相信程度上限、校验丢弃、调用与耗时。只报告结论仍可能错误，具体剩余失败类型需本轮真实验证后才能确认。

预计两轮合计 **110—130 次对话模型调用**；生成约 **15—25 分钟**，连同独立双判与汇总约 **30—50 分钟**，不含降级重跑。双判不使用对话模型端点。

全部材料、原始输出和完整日志位于 `/Users/cassia/Local/Code/iris-eval-artifacts/co01-report-only-20261010`，仓库内仅此汇总及冻结清单。历史 original broad 及 v2 探测数字分别保留在 results.md、probe_v2.md；不把历史结果当作当前新配置的实测结果。
