# MG2 细节保留补充评分 v1

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
