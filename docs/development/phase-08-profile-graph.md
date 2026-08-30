# 阶段 8：Profile 与 Graph

> 状态：Planned  
> 前置阶段：[阶段 7](./phase-07-vector-recall.md)  
> 目标版本：0.9.0  
> 架构依据：[§13.3 Relation](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#133-relation)、[§13.5 ProfileProjection](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#135-profileprojection)、[§18 Recall 协议](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#18-recall-协议)、[§22.5 Graph 与 Profile](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#225-graph-与-profile)、[§36 阶段 8](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#阶段-8profile-与-graph)

## 阶段目标

增加可重建的 Entity/Relationship/SpaceGroup 汇总视图和受预算约束的关系召回，同时保证任何投影落后、损坏或 Binding 变化都能回退到 Canonical Claim/Relation。

## 架构约束

- Profile 是 Claim 的带来源汇总，不是独立事实源；Agent Persona 不能混入外部 Entity Profile。
- Graph 只投影 Canonical Relation、Verified Binding 和允许的 Claim，不从模型联想直接建边。
- 多跳遍历逐边执行 Scope/Privacy/Status/Valid Time/Tombstone 校验。
- Builder Version 未知、Watermark 超阈值或校验失败时不得读取投影。
- Binding/Redirect/SpaceGroup 变化只触发重建和失效，不重写历史事实。

## 需求追踪

| 需求 ID | 基线要求 | 工作包 | 验证门禁 |
| --- | --- | --- | --- |
| P8-PROFILE-01 | Profile 字段级来源、冲突、Freshness 与 Canonical 回退 | 8.1 | 来源追踪、冲突、过期和重建测试 |
| P8-GRAPH-01 | Graph 仅投影授权 Canonical 边并保存 Revision/Scope | 8.2 | 边白名单、来源完整性与投影一致性测试 |
| P8-TRAVERSAL-01 | 多跳逐边授权、深度/扇出/节点/Token/Deadline 预算 | 8.3 | 恶意高连接图、越权路径和终检测试 |
| P8-INVALIDATION-01 | Binding/Redirect/SpaceGroup/Correct/Forget 触发失效 | 8.1–8.4 | Watermark、Cache、重建和删除竞态测试 |
| P8-DEGRADE-01 | 未知/落后/损坏投影回退 Canonical Claim/Relation | 8.3–8.4 | 故障注入、Degraded Envelope 与可观测性测试 |

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

## 数据、契约与回退策略

- Profile/Graph 只新增投影表、Builder State、Manifest 与 Current Pointer；字段与边保存 Canonical ResourceRef/Revision、Scope/Privacy、有效期、Source/Tombstone Watermark，不复制不可追踪事实。
- Builder 升级、Binding/Redirect/SpaceGroup 变化使用新 Generation 或影子投影；校验来源覆盖、节点/边数量、Checksum 和抽样 Recall 后原子切换，旧投影保留受限回退窗口。
- Profile/Relations 读取和 Graph Route 通过可选 Capability、OpenAPI/Schema 和 Fixture 发布；旧客户端可忽略 Graph Route，但服务端不得用“投影不可用”放宽逐边授权或最终 Rehydrate。
- Cache/Projection 失效 Outbox 携带 Source Revision 与原因；旧 Worker 不领取未知 Builder Job，新 Worker 能识别上一 Payload 版本并拒绝 Stale Source 提交。
- 回退时禁用不可信 Profile/Graph Pointer，并改走 Canonical Claim/Relation 直接读取；不通过 Down Migration 删除 Builder 历史，也不回写 Entity/Binding/Relation 的 Canonical 历史。

## 量化验收基线

- Profile 的每个非空字段必须有至少一个仍有效的 Source Claim；对 100% 字段执行自动引用校验，并抽样比较 Canonical 回退内容与冲突状态。
- Scope、Privacy、Status、Valid Time、Tombstone 的逐边过滤性质每项至少运行 200 个生成图案例；跨 Tenant/Agent/SpaceGroup/Entity 的未授权路径返回数必须为 0。
- 使用至少为配置深度、扇出和节点上限 10 倍的高连接图压测，实际访问与返回不得突破任一限制，Deadline 到达后不得继续扩展。
- 对同一 Canonical Snapshot 和 Builder Version 连续全量重建 3 次，Profile 来源集合、Graph 边集合、Watermark 和 Manifest Checksum 一致。
- Binding/Redirect/SpaceGroup/Correct/Forget 与查询竞态各至少重复 50 次；变更提交后旧字段/边/Cache 的当前返回数必须为 0，Route 故障准确报告 Canonical 回退。

## 退出门禁

- [ ] Profile 每个字段可追溯到有效 Claim，冲突不会被汇总静默覆盖。
- [ ] Graph 多跳逐边 Privacy 测试覆盖跨 Tenant/Agent/SpaceGroup/Entity 越权。
- [ ] 深度、扇出、节点和 Token 预算在恶意高连接图上仍生效。
- [ ] 旧/未知 Builder、落后 Watermark 和损坏投影被拒绝并安全回退。
- [ ] Binding/Redirect/SpaceGroup 变化后重建与 Canonical 结果一致。
- [ ] Forget 后旧 Profile 字段、Graph 边和 Cache 均不能复活目标内容。
- [ ] Migration/Builder/Capability 兼容和回退方案、需求追踪及交付证据已完成评审。

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
