# 阶段 6：FTS Recall

> 状态：Planned  
> 前置阶段：[阶段 5](./phase-05-long-term-memory.md)  
> 架构依据：[Recall 协议、FTS5、HTTP API、性能目标与阶段 6](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md)

## 阶段目标

交付第一个完整、可解释、无向量依赖的 Recall 协议：组合结构化 Route 与 FTS 候选，在 Deadline 和 Token Budget 内执行最终 Canonical Rehydrate，并明确报告完整、部分与降级结果及宿主实际使用情况。

## 架构约束

- FTS 是绑定具体 Resource Revision 的可重建投影，不能成为事实源。
- 缓存、FTS 和其他派生候选返回前必须 Canonical Rehydrate，重新执行 Scope/Privacy/Status/Time/Tombstone 检查。
- Persona 通过响应顶层 Revision/Hash 协调，不作为普通 Memory Candidate。
- Deadline 使用单调时钟；任一 Route 超时不能阻塞已完成 Route。
- 排序器版本化、确定性且区分缺失分值；Token 裁剪保障 Due Task、Focus 和当前说话人必要身份。

## 工作包

### 6.1 FTS5 Projection

- 建立绑定 `(resource_type, resource_id, resource_revision)` 的 FTS 文档和 Projection State。
- 版本化 Tokenizer、规范化、停用词、语言和 Builder；支持影子表构建、校验和原子切换。
- Revision/Tombstone 提交后先逻辑失效旧项，再异步物理清理。

### 6.2 Recall Orchestrator

- 实现 RecallRequest、ExternalActor 解析、Purpose、Category/Resource Filter、`as_of`、Minimum Watermark 和 Deadline。
- 并行运行 Persona/Recent/Focus/State/Task/Claim/Relation/FTS Route，分配子 Deadline 和候选上限。
- 生成完整 RecallResponse：Watermark、Persona Revision/Hash、Completed/Degraded Routes、Partial、Cache Until 和 Next Wake。

### 6.3 Rehydrate、融合与预算

- 对每个派生 Candidate 回读 Canonical Revision 并验证所有硬过滤条件。
- 实现版本化分量、稳定 Tie-breaker、冲突/冗余处理、Layer Budget 和 Token Estimator。
- 缓存键包含 Scope/Actor/Purpose/Query、Source/Tombstone Watermark、Persona Revision 和 Schema Version。

### 6.4 Usage Report 与契约发布

- 实现 retrieved/returned/host_selected/model_visible 四阶段及子集校验。
- Usage Report 幂等合并，只影响 Accessibility/Activation，不影响 Confidence。
- 冻结 `/v1/recall`、Usage、Search、Capabilities/Negotiate、稳定错误码、Schema、Fixture 和双 SDK。

### 6.5 性能与降级

- 建立结构化 Recall p95 ≤ 50 ms、FTS Recall p95 ≤ 100 ms 的可复现实验基线。
- 覆盖 FTS 落后/损坏、Minimum Watermark 不可达、Route 超时和允许/禁止 Partial 的行为。
- Trace 只返回安全摘要，不暴露隐私过滤前候选。

## 退出门禁

- [ ] FTS Builder 版本变化可影子重建并无中断切换，旧 Revision 不再被采用。
- [ ] Recall 稳定排序、Token/Layer Budget 和相同输入可复现性测试通过。
- [ ] 并发 Forget 与 FTS Search 的删除竞态不能越过最终 Rehydrate。
- [ ] Route 超时、索引落后和 Minimum Watermark 场景返回准确 Partial/Degraded Envelope。
- [ ] Usage 子集伪造、跨 Tenant Candidate 和重复 Report 被拒绝或幂等处理。
- [ ] 在声明的硬件/数据集/并发条件下达到结构化与 FTS p95 目标。

## 交付证据

- 代码/变更：待补充
- 契约/SDK 版本：待补充
- Schema/Migration：待补充
- 性能/竞态测试报告：待补充
- 已知限制：待补充

## 明确不做

- 不实现向量语义、Graph 多跳或 Provider 在线调用。
- 不让 Cache/FTS 命中绕过 Canonical Rehydrate。
- 不把普通记忆 Candidate 注入宿主 Persona/System Slot。

## 交接条件

Phase 7 可以扩展独立 Vector Route，而无需改变 Recall Envelope、硬过滤、预算、Usage 或降级语义。
