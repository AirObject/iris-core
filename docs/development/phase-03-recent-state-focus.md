# 阶段 3：近期上下文、State 与 Focus

> 状态：Planned  
> 前置阶段：[阶段 2](./phase-02-observation-outbox-scheduler.md)  
> 架构依据：[近期上下文、实时状态、关注项与阶段 3](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md)

## 阶段目标

建立三类语义明确且可恢复的短期能力：可重建的近期对话窗口、版本化的高频当前状态、Canonical 的认知关注项，并提供首个仅依赖结构化数据的 Recall Route。

## 架构约束

- RecentContextProjection 是 Observation 的可重建窗口；FocusItem 是 Canonical 认知对象，两者不得合并。
- 原始近期内容默认不跨 Space，共享只通过显式 Agent/SpaceGroup Scope 的 Focus 或后续长期对象。
- State Coalescing 只能合并投影 Job，不能跳过 Canonical State Revision。
- Focus 的衰减只改变 Activation/状态，不能提高事实 Confidence 或删除来源。
- 所有返回项仍执行 Scope、Privacy、Expiry、Revision 和 Tombstone 校验。

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

## 退出门禁

- [ ] Recent Context 仅引用 committed Observation，Builder 版本变化可完整重建。
- [ ] State 高频写保留所有要求的 Canonical Revision，Coalescing 不改变最终值。
- [ ] Focus 容量、Kind 配额、衰减、Pin/激活和状态转换性质测试通过。
- [ ] 重启后 Current Pointer、TTL、Focus 和投影恢复正确。
- [ ] Space 私有 Recent 内容不会通过 Focus/Recall 默认跨端泄漏。
- [ ] Structured Recall 的稳定排序、Deadline 和降级 Envelope 测试通过。

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
