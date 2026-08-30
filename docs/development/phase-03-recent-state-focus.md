# 阶段 3：近期上下文、State 与 Focus

> 状态：Planned  
> 前置阶段：[阶段 2](./phase-02-observation-outbox-scheduler.md)  
> 目标版本：0.4.0  
> 架构依据：[§9 近期上下文、实时状态与关注项](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#9-近期上下文实时状态与关注项)、[§15.4 Recall](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#154-recall)、[§18 Recall 协议](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#18-recall-协议)、[§30 性能与容量](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#30-性能与容量目标)、[§36 阶段 3](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#阶段-3近期上下文state-与-focus)

## 阶段目标

建立三类语义明确且可恢复的短期能力：可重建的近期对话窗口、版本化的高频当前状态、Canonical 的认知关注项，并提供首个仅依赖结构化数据的 Recall Route。

## 架构约束

- RecentContextProjection 是 Observation 的可重建窗口；FocusItem 是 Canonical 认知对象，两者不得合并。
- 原始近期内容默认不跨 Space，共享只通过显式 Agent/SpaceGroup Scope 的 Focus 或后续长期对象。
- State Coalescing 只能合并投影 Job，不能跳过 Canonical State Revision。
- Focus 的衰减只改变 Activation/状态，不能提高事实 Confidence 或删除来源。
- 所有返回项仍执行 Scope、Privacy、Expiry、Revision 和 Tombstone 校验。

## 需求追踪

| 需求 ID | 基线要求 | 工作包 | 验证门禁 |
| --- | --- | --- | --- |
| P3-RECENT-01 | RecentContextProjection 只引用已提交 Observation，且按版本/Watermark 可重建 | 3.1 | 确定性重建、过期、Space 隔离测试 |
| P3-STATE-01 | State 当前指针、不可变 Revision、TTL 与 Namespace Policy | 3.2 | 并发写、Coalescing、历史与过期测试 |
| P3-FOCUS-01 | Focus 容量、状态机、衰减、激活与晋升 | 3.3 | 状态机、预算、时间与来源性质测试 |
| P3-RECALL-01 | 结构化 Route、Deadline、预算、稳定排序与降级 Envelope | 3.4 | 契约、排序、超时和权限矩阵测试 |
| P3-RECOVERY-01 | Current Pointer、投影与时间状态在崩溃/重启后恢复 | 3.1–3.4 | 故障注入、重启重建与 Watermark 对账 |

## 工作包

### 3.1 RecentContextProjection

- 从已提交 Observation 构建 Session/Space 热窗口、Token 上限、Source Watermark 和 Builder Version。
- 支持带 Source Refs 的确定性摘要段接口；无 Provider 时仍可使用窗口裁剪。
- 实现失效、过期、影子重建和 Space 隔离。

### 3.2 StateRecord

- 实现 `(scope, namespace, key)` 当前指针与不可变历史、Source Authority、TTL 和 Namespace Policy。
- 提供 Coalesce Key、过期过滤、历史保留策略以及 `PUT /v1/state/{namespace}/{key}`。
- 让高频写保持短事务，并记录写入延迟和合并率。

### 3.3 FocusItem

- 实现 goal/question/entity/clue/concern/affect/pending_input、状态机和 Expected Revision。
- 实现 Item 数、Kind 配额、Token Budget、Activation 衰减、休眠、Dismiss、Expiry 和 Promotion seam。
- `affect` 只作为未来 Persona State 证据，不允许直接改变 Trait/Core。

### 3.4 结构化 Recall

- 提供 Recent Context、State、Focus 的独立 Route 和统一 Candidate 转换。
- 实现子 Deadline、稳定排序、Layer Budget、Completed/Degraded Route 记录的最小骨架。
- 公开 `/v1/recent-context`、`/v1/state`、`/v1/focus-items` 及状态转换接口。

## 数据、契约与回退策略

- 通过增量 Migration 新增 Recent Projection 元数据、State Revision/Current Pointer、Namespace Policy 与 Focus Revision；不修改 Phase 2 Observation、Outbox、Tick 的既有语义。
- Recent Projection 和摘要段携带 `builder_version`、`source_watermark` 与 Source Refs。Builder 升级先影子重建、逐项校验，再原子切换；失败继续读取上一已验证版本或回退到 Canonical Observation 窗口。
- State/Focus 写接口先发布 Schema、稳定错误码和 Fixture；同一 `/v1` 只新增可选字段。旧客户端可忽略新增 Route，不得将缺失字段解释为跨 Scope 通配。
- 回退优先使用兼容旧二进制；新 State/Focus Revision 不做 Down Migration。旧二进制不能识别新 Builder 时停止读取该投影，并从 Canonical 数据重建，不删除 Revision 历史。

## 量化验收基线

- State Coalesced Write 在声明硬件、Payload、并发和数据库状态下 p95 ≤ 25 ms；报告同时给出未合并写比例、SQLite Busy 次数和队列 Lag。
- Recent Projection 对同一 Observation 集、Watermark 与 Builder Version 连续重建 3 次，Observation refs、摘要 Source Refs、Token 估算和结果哈希一致。
- Focus 容量、Kind 配额、Token Budget、TTL、衰减和状态转换每条性质至少运行 200 个固定种子生成案例。
- 在事务提交前后、投影切换前后和 Current Pointer 更新前后各执行至少 20 次故障/重启注入；恢复后不得出现跨 Space 内容、悬空 Pointer 或已过期对象参与 Recall。
- Structured Recall 在 100 次相同输入重放中 Candidate 顺序和裁剪结果一致；每个 Route 超时/取消场景至少重复 20 次，响应准确标记 Completed/Degraded/Partial。

## 退出门禁

- [ ] Recent Context 仅引用 committed Observation，Builder 版本变化可完整重建。
- [ ] State 高频写保留所有要求的 Canonical Revision，Coalescing 不改变最终值。
- [ ] Focus 容量、Kind 配额、衰减、激活和状态转换性质测试通过。
- [ ] 重启后 Current Pointer、TTL、Focus 和投影恢复正确。
- [ ] Space 私有 Recent 内容不会通过 Focus/Recall 默认跨端泄漏。
- [ ] Structured Recall 的稳定排序、Deadline 和降级 Envelope 测试通过。
- [ ] Migration/Builder 兼容/回退方案、需求追踪和交付证据已完成评审。

## 交付证据

- 代码/变更：待补充
- ADR：待补充
- Schema/Migration：待补充
- 测试/性能报告：待补充
- 已知限制：待补充

## 明确不做

- 不实现 Note/Task、长期 Claim、FTS 或向量召回。
- 不用自由文本摘要代替原始 Observation 或作为独立 Evidence。
- 不把 Working/Focus 当作无限增长的第二份聊天历史。

## 交接条件

Phase 4 可以依赖稳定的短期读取 Route、Focus Promotion seam、可注入时间和 Schedule Job Kind；Phase 6 可以复用 Recall Orchestrator 骨架。
