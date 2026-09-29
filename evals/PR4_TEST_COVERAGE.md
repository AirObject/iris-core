# M1-4 自动化覆盖

Windows Python 3.12.13／3.13.13：完整 pytest 各 175 项通过。R14 经用户确认按设计留到 M4，R02／R03 同属 M4。

| 设计场景 | 自动化检查 |
| --- | --- |
| R01 返回各分区 | `test_retrieval.py::test_r01_r04_r05_r06_partitions_shared_memory_and_no_raw_leak`；API 测试还验证 persona、近期消息、空 state、目标与提示 |
| R04 默认跨入口记忆共享 | 同上，保留 B 的入口出处，A 的响应不含 B 消息正文 |
| R05 未学习内容不当记忆 | 同上，消息表中存在的正文不会成为 search 结果 |
| R06 无相关结果为空 | 同上，独立的无答案查询 |
| R07 embedding 失败降级 | `test_r07_vector_index_after_commit_and_fallback`；边界测试验证两秒总超时、取消等待、不重试、调用记录与提示 |
| R08 重复反馈幂等 | `test_r08_r09_feedback_idempotent_atomic_and_revision_independent`；8 次并发重复反馈只强化一次；HTTP 重复请求同样验证 |
| R09 使用只增保留强度 | 同上，+8、相信程度不变、反馈不加修订号；反馈时正文已有新修订仍可计数 |
| R10 本地 P95 ≤500 ms | `test_r10_local_prepare_latency_and_empty_messages`；规模性能另按 5k／50k 运行脚本，见报告 |
| R11 来源已在返回消息中不占名额 | `test_r11_r12_r13_redundancy_budget_and_no_strength_for_read`；派生来源递归、混合初始来源另测 |
| R12 宿主已有记忆不重复返回 | 同上，已知 ID 被剔除，其余名额补入其他结果 |
| R13 近似重复只返一条 | 同上；边界测试同时保留否定不同的判断、限制人物要点和完整 JSON token 预算 |

其他测试覆盖学习 v5 临时编号消除与原始输出保存、自身／他人推断 about、提示词约束、迁移 004 回填与原设置保留、索引提交后增量／回滚、编辑删除后的检索、同名账号、结构过滤、非法 FTS 语法、模型异常向量、32KB UTF-8 上限、批量原子性、入口内去重、24 小时反馈有效期、删除不复活、状态 503、非回环拒绝、近期窗口／目标数／本入口缺口和最新模型结果、外部学习／召回明细、单／双判报告口径。

另有用量回归测试，保证共享检索的 `retrieval_query` 同时计入 embedding 分项与总 token。

判分和规模性能数据不是单元测试断言的替代；最终学习与召回验收仍需规划者的隐藏集。
