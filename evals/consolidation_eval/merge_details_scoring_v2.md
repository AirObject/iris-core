# MG2-S 细节保留补充评分 v2（2026-10-12）

本次口径由规划者在任何真实运行之前确定，版本为 `merge_scoring_v2`。v1 文件保留；新运行的 manifest、案例 input 和本说明共同绑定此版本，旧材料继续按原版本解释。

本文件新增诊断，不替代或改写冻结的 consolidation_scoring_v1；原六字段判分仍按 scoring.md，报告与标注仍按 report-only-scoring.md。只使用本轮材料。不得查看另一轮判分。模型的 unique_details 是待核查的主张，不是答案；不能仅因引用片段真实出现就相信其完整性或独有性。

将 detail-judgment-template 复制到独立目录，保留 manifest.json 绑定材料指纹，逐案例填入 JSON。每轮的原评分目录通过 --judgments 传入，配套细节评分目录通过 --detail-judgments 传入；两者轮数一致。reason 必须说明具体原文事实和存续正文是否覆盖。布尔值只允许 true / false，不接受 null、数字、字符串。数组、ID、次序不可增删。原评分 schema 不增加字段。

## 合并后丢失细节（detail_loss）

对 actual_merges 的每个实际提交操作，逐事实对照 before_memories 的所有输入正文与 result.content。包括被并入记忆的方式、对象、时间、数值、否定、范围及其他限定，不能只比较核心事实。语义等价可视为覆盖；只有来源、历史或占位中仍存在某事实，而存续正文未覆盖，仍算丢失。合并链按每一步即时输入和结果检查，之前已丢失的事实仍计在对应步骤中，不能用后续操作抵消。

merge_detail_results 按 actual_merges 顺序给出 action_id、all_facts_preserved、reason。任意事实遗漏时 all_facts_preserved=false。两轮取 AND，分歧取不利结论。指标分子是丢失细节的操作数，分母是实际合并操作数；0 次合并 rate=null。细节丢失也计入原误合并指标，即使原评分误把 key_facts_preserved 判为 true。

## 误拒合并（false_refusal）

refusal_results 按 expected.merges 顺序列 index、pair、no_false_refusal、reason。结合 merge_checks、即时正文、result.unique_details、确定性校验结果、实际动作与最终占位判断。只要本应安全保留某一条原文的同一事实对，因为错误的独有细节判断或片段校验而未合并，no_false_refusal=false（例如把同义改写当成双方独有细节）。模型选 uncertain/separate 的理由若实际也是错误的细节判断，同样计入。

双方确有互不覆盖的细节时，MG2 决定要求不合并，属于正确拒绝，即使旧语料列为应合并，此项仍为 true；必须逐条解释依据，不改语料和原应合并识别率。保护、并发冲突、预算、候选未入选、不同事实等其他原因造成的未合并不计入此项；原识别率与有效运行检查照常记录。已经成功合并的 pair 不算误拒。

指标分母固定为语料 expected.merges 的对数，分子为其中因错误细节判断而未合并的对数，每对计一次；两轮 no_false_refusal 取 AND 后再取反，分歧取不利结论。输出逐对理由和分歧，避免把所有未合并都归因于细节保护。零分母 rate=null。

未提供细节判分时，detail_loss / false_refusal 为 null、detail_judge_rounds=0，不能宣称 MG2 质量通过。假模型只验证机制，不能作为真实模型质量结论。冻结语料、原评分及保护／相信程度上限口径保持不变。

## 运行有效性与阻塞归因

只因基础设施或执行不完整而作废：限流、网络错误、超时、用途暂停、token／调用预算耗尽、未完成调用、存储或其他未归类的执行异常、未解释的待处理项目等。完整返回的格式错误、单项内容拒绝、校验拒绝是质量结果，保留本例和所有应合并对，不通过重跑筛掉。

同一对的 conflict、mutual_details、invalid_details 会阻塞共用记忆的合并；`unresolved_conflict` 不使整例无效。该应合并对未识别，仍在 recognition 分母中。`merge_checks[].blocked_by` 从实际应用时的阻塞集合导出，包含阻塞检查 check_id、pair_ids、pair_keys 和 type；报告的 blocked_merges 列出被阻塞的应合并对及上述归因。由格式错误或其他失败引起的阻塞也记录其失败类型，不能遮蔽基础设施失败。

当 merge_allowed=false 的对仍被模型判 merge，应用实际抛出 merge_forbidden，计为 protected_merge_errors（“受保护对判合并”）。评测器只捕获这一明确错误码，不把任意 ValueError 当作它。该模型错误不使运行无效；实际正文／相信程度保护与实际误合并检查仍独立执行。类别名称沿规划者口径，merge_exclusion 同时列出具体禁合原因。

同次较早的合并可能令后续输入修订号失效；已记录 revision_conflict 且工作状态为 obsolete 的正常安全跳过也保留在质量分母中，不当作基础设施失败。未解释的跳过仍按执行不完整处理。

执行器与计分器共享上述有效性规则。work_results、调用记录、实际阻塞观察都写入指纹绑定的材料。只要有基础设施失败，即使同时出现模型错误或阻塞，本次仍无效。

## 管理员建议诊断（确定性统计，无需新增判分字段）

`expected.mutual_details` 是可选的记忆 key 对列表，例如 `[["a","b"]]`；每对恰好两条不同记忆，按无序集合匹配，不允许重复标注。公开 consolidation_v1 未提供此字段，按空集合统计，不改语料。

实际“合并待核对”建议的识别条件：`input.merge_checks[]` 中 `outcome=="conflicts"` 且 `result.merge_detail_check.outcome=="mutual_details"`。只看模型输出互有细节、但没有实际写建议（例如 skipped、failed、checked），不算建议。

- 建议误报 suggestion_false_positives：在未标为 mutual_details 的对上实际写建议的次数，每次 pair 检查计一次，不把给双方写的两条标注算两次。列出 check_id 和 pair；这是标签诊断，不计入误合并门槛。
- 互有细节建议命中 mutual_details：分母为 mutual_details 标注对数，分子为其中至少一次满足上述实际建议条件的对数；同一对最多命中一次。零分母 rate=null。未命中按 merge_detail_check.outcome 分类（invalid_details、not_merge、one_sided、equivalent、mutual_details 等）；没有详情为 missing_detail_check，没有候选检查为 not_evaluated。mutual_details 输出却未写建议也属于未命中，结合执行 outcome／reason 核对。若同一对有多次检查、多个不同 outcome，未命中分类分别计一次，因此分类小计可能重叠，逐对只扣一次。

“合并待核对”只是管理员复核建议，本身不算写入无依据的事实或错误结论；建议误报单列，不应再按该建议给 write_damage 判 true。实际合并丢失细节或其他损坏仍按原安全检查处理。写回检查的完整规则见本轮 write-audit.md（v2）。

误合并率（含合并后丢失细节）仍为门槛指标：≤5%，0 次合并仍按未达到。false_refusal 仍为人工语义诊断，不改变门槛或 recognition 分母；阻塞导致的未识别不能自动判成 false_refusal，须按本节之前的逐事实标准核对原因。保护、相信程度上限及双判分歧取不利结论保持不变。
