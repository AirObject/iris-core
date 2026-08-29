# 阶段 8：Profile 与 Graph

> 状态：Planned  
> 前置阶段：[阶段 7](./phase-07-vector-recall.md)  
> 架构依据：[Profile、Relation Graph、派生投影与阶段 8](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md)

## 阶段目标

增加可重建的 Entity/Relationship/SpaceGroup 汇总视图和受预算约束的关系召回，同时保证任何投影落后、损坏或 Binding 变化都能回退到 Canonical Claim/Relation。

## 架构约束

- Profile 是 Claim 的带来源汇总，不是独立事实源；Agent Persona 不能混入外部 Entity Profile。
- Graph 只投影 Canonical Relation、Verified Binding 和允许的 Claim，不从模型联想直接建边。
- 多跳遍历逐边执行 Scope/Privacy/Status/Valid Time/Tombstone 校验。
- Builder Version 未知、Watermark 超阈值或校验失败时不得读取投影。
- Binding/Redirect/SpaceGroup 变化只触发重建和失效，不重写历史事实。

## 工作包

### 8.1 ProfileProjection

- 按身份、偏好、关系、重要经历、长期目标、近期变化和交互建议生成字段级汇总。
- 每字段保存 Source Claim IDs、Builder Version、Source Watermark、Freshness 和 Conflict State。
- 实现全量/增量重建、绑定变化失效及 Canonical Claim 回退读取。

### 8.2 Relation Graph

- 构建 Entity 节点、Canonical Relation、Verified Binding 和安全 Claim 边。
- 每条边保留 Canonical Ref/Revision、Scope、Privacy、有效期和 Builder 元数据。
- 实现影子重建、校验、Watermark/Version 拒绝以及绑定/重定向后的重投影。

### 8.3 受限 Graph Recall

- 实现最大深度、逐层扇出、总节点数、边类型 Allowlist、Deadline 和 Token Budget。
- 遍历阶段与最终 Candidate Rehydrate 都做权限检查，禁止从可见起点穿越不可见边。
- Graph Route 失败时回退到直接 Canonical Claim/Relation 并写入 Degraded Route。

### 8.4 管理与可观测性

- 提供 Profile/Graph Watermark、Lag、Builder Version、节点/边数量和重建状态。
- 管理重建使用安全 Job/Fencing，不阻塞 Canonical 写入。
- Cache 在 Binding、Redirect、SpaceGroup、Correct 和 Forget 后按 Outbox 失效。

## 退出门禁

- [ ] Profile 每个字段可追溯到有效 Claim，冲突不会被汇总静默覆盖。
- [ ] Graph 多跳逐边 Privacy 测试覆盖跨 Tenant/Agent/SpaceGroup/Entity 越权。
- [ ] 深度、扇出、节点和 Token 预算在恶意高连接图上仍生效。
- [ ] 旧/未知 Builder、落后 Watermark 和损坏投影被拒绝并安全回退。
- [ ] Binding/Redirect/SpaceGroup 变化后重建与 Canonical 结果一致。
- [ ] Forget 后旧 Profile 字段、Graph 边和 Cache 均不能复活目标内容。

## 交付证据

- 代码/变更：待补充
- Builder Manifest：待补充
- Schema/Migration：待补充
- 隔离/重建/预算测试报告：待补充
- 已知限制：待补充

## 明确不做

- 不把 Graph 当 Task Dependency 的状态源。
- 不用昵称、共同出现或向量相似直接创建 Relation。
- 不提供无界图遍历或跨 Privacy 边的“管理方便”旁路。

## 交接条件

Phase 9 可以安全使用外部 Entity/Profile/Relation 作为 Persona Narrative 的候选 Evidence，同时保持 Persona 与用户画像隔离。
