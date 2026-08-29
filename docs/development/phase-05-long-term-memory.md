# 阶段 5：显式长期记忆与 Episode

> 状态：Planned  
> 前置阶段：[阶段 4](./phase-04-notes-tasks-events.md)  
> 架构依据：[长期记忆、Remember/Correct/Forget、保留与阶段 5](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md)

## 阶段目标

建立可追溯、可修正、可遗忘的长期记忆事实层。阶段结束时，调用方可以显式创建和搜索 Claim，封装 Episode/Relation/Artifact，按双时态读取历史，并证明删除内容不会通过当前读取、后台任务或恢复流程复活。

## 架构约束

- 每个 Active Claim 至少有一条有效 Evidence；模型推断和文本相似只产生候选，不能直接覆盖高权威事实。
- Correct 创建新 Revision/Supersede/Dispute 和 Evidence，不原地覆盖 Canonical Text。
- Forget 先同步影响 Canonical 读取，投影清理由 Outbox 异步完成；所有未来 Builder 必须比较 Tombstone Watermark。
- Confidence、Importance、Accessibility、Activation 和情感分值分开演化，召回使用不能提高 Confidence。
- Episode 不等于 Session，不允许跨 Space 拼接原始内容；Procedure Claim 不保存可执行代码。

## 工作包

### 5.1 Episode、Claim 与 Evidence

- 实现 Episode 的 Open/Sealed/Superseded/Tombstoned、Observation refs、参与者、时间边界和提取器版本。
- 实现 Claim 的类别、规范值、状态、双时态、评分维度、Source Authority 和去重候选键。
- 实现 supports/contradicts/corrects Evidence，验证 SourceRef、Scope、Revision 和有效状态。

### 5.2 Relation 与 Artifact

- 实现带方向、类型、Privacy、Evidence 和有效期的 Canonical Relation。
- 实现 Artifact locator 规范化、媒体类型/大小/哈希校验、引用计数和受控本地存储。
- 外部引用只作为数据保存，Recall 或读取路径不得自动抓取 URL。

### 5.3 显式记忆 API

- 实现 Remember、Correct、Forget 和不依赖 FTS 的结构化 Search。
- 主体歧义时拒绝自动补全；所有写入校验 Idempotency、Expected Revision、Scope、Privacy 和权限。
- Correct/Forget 同事务更新 Agent/Tombstone Watermark、Audit 并写出未来 FTS/Vector/Profile/Graph/Cache 失效事件。

### 5.4 双时态与历史读取

- 实现 `valid_from/valid_until` 与 `recorded_at/superseded_at` 的读取语义。
- `as_of` 按历史 Revision/Watermark 重建当时可见状态；历史不足返回 `history_unavailable`。
- 支持发生时身份与当前身份的历史视图，不回写 Observation。

### 5.5 保留、Tombstone 与恢复

- 实现 Resource、Subject+Predicate、Session、Space 和授权数据范围的 Tombstone Selector。
- 区分 Accessibility 衰减、Archive 与合规删除；实现 Legal Hold 和不可普通遗忘资源规则。
- 扩展备份/恢复、导入接口和后台 Job，使 Tombstone Watermark 始终优先。

## 退出门禁

- [ ] Active Claim 无 Evidence、未知 Entity、越权 Scope 或非法 ResourceRef 均被拒绝。
- [ ] Correct 保留旧 Revision，当前/历史/Disputed 视图与双时态测试通过。
- [ ] 并发 Recall/Correct/Forget 下，Forget 提交后任何当前读取都不返回目标内容。
- [ ] 删除后通过 Cache 占位、Outbox 重试、备份恢复和模拟索引旧结果均不可复活。
- [ ] Artifact 路径穿越、超限、哈希不符和未授权读取测试通过。
- [ ] 自动保留策略不会处理 Persona Core、Pinned Note、Active Task、未兑现承诺和 Tombstone。

## 交付证据

- 代码/变更：待补充
- ADR：待补充
- Schema/Migration：待补充
- 历史/删除故障测试报告：待补充
- 已知限制：待补充

## 明确不做

- 不实现 LLM 自动提取、FTS、Embedding、Graph 多跳或 Profile 汇总。
- 不用相似度自动合并主体、Scope、有效时间不同的 Claim。
- 不把认知衰减伪装成隐私删除。

## 交接条件

Phase 6 可以依赖稳定的 Claim/Episode/Relation/Artifact ResourceRef、Tombstone Watermark、结构化 Search、历史读取与投影失效事件。
