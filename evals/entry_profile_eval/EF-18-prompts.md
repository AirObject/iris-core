# EF-18：隔离提示词变量的预登记

按规划者 EF-18 指令，默认恢复未经改写的 entry_profile_observe_v4 / entry_profile_admit_v4。v5 文件保留，不作默认；本轮不创建新提示词版本。

| 默认提示词 | SHA-256 |
| --- | --- |
| entry_profile_observe_v4 | dc3a81efd4422659e777b3310fb19d3e781efef869ec6299b937ee6ebb44ad7b |
| entry_profile_admit_v4 | a03ee65ca2f6944542f7b52ea5845ff002f02531009a8d2430e252f065fa2bd2 |

v5 增加的内容仅在说法段落：相同短句在跨人／跨日／跨场次的独立使用，以及“先…一会儿”“一下／一下下”等有限变体的明确说明。这些提示要求与 v5 其他效果需要后续独立评估；EF-17 单轮的质量下降不能据此归因到某一句提示。EF-18 保留代码中相应计数规则及准入补强，不撤回产品修正。v4 与新计数的 JSON 输入／输出结构兼容，无需适配或改写 v4；代码次数仍不是语义通过的充分条件。

本轮另修确定性送检证据选择，所有正文要点仍由 v4 检查核对。公开 entry_profile_v1 预登记 replay_mode=daily_plus_final_flush，只运行一次真实 GLM high；超时或 429 作废、保留材料并停止，不自行重跑。外部双判由两名未参与修改的全新独立 gpt-6-astra max 子代理完成，互不可见，分歧取不利结论，使用冻结 scoring_v3。

门槛：模型条目有依据 ≥90%，禁止内容与全部违规 0。覆盖、常聊话题数和 evidence_budget_exceeded 为诊断，与 EF-16／EF-17 对比；预算次数一致按最终仍带该原因的候选数统计，不能当作历史尝试次数或覆盖标签数。完整材料放仓库外 /Users/cassia/Local/Code/iris-eval-artifacts/EF-18-20261015/。本轮不以重复运行挑选结果，不修改冻结语料或评分。
