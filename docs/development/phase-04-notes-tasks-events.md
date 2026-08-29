# 阶段 4：Note、Task 与 CognitiveEvent

> 状态：Planned  
> 前置阶段：[阶段 3](./phase-03-recent-state-focus.md)  
> 架构依据：[Note、Task、CognitiveEvent 与阶段 4](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md)

## 阶段目标

交付低成本捕获、正式计划、前瞻触发和宿主投递闭环。阶段结束时，系统能区分“记录事项”“计划行动”“提醒已送达”和“外部效果已完成”，并在重复投递或宿主切换下保持正确。

## 架构约束

- Note 是独立 Canonical 对象，`review_after` 不是删除时间，Pin/承诺/活动 Task 来源不得自动删除。
- 对话提取的 Task 默认是 `proposed`；只有显式工具、确定性策略或管理员能激活。
- Task/Step 完成需要独立状态转换；外部效果要求实际成功 Observation/Evidence。
- CognitiveEvent 采用 At-least-once；ACK 只表示宿主承担处理责任，不表示 Task 完成。
- Trigger 只允许声明式语法和操作符，禁止任意代码、脚本或隐式工具执行。

## 工作包

### 4.1 Note 生命周期

- 实现 Inbox/Pinned/Snoozed/Archived/Promoted/Tombstoned 状态、Expected Revision 和审计。
- 实现 Review Schedule、到期扫描、重复关联以及向 Task/Claim/Episode 的 Promotion seam。
- 开放 Note 列表、创建、更新、归档和晋升端点。

### 4.2 Task 与 Step

- 实现 Task、稳定 TaskStep ID、状态机、Next Action、Owner、Due Time 和完成 Evidence。
- 实现 TaskDependency 环检测和基于依赖的 Ready 计算；v1 仅支持同一 Task 内依赖。
- 所有状态转换使用幂等键、Expected Revision、Audit 与 Outbox。

### 4.3 Trigger 与前瞻记忆

- 实现 `at_time|recurrence|observation_kind|state_condition|task_transition` 的受限 Trigger。
- 用 Trigger Revision + 计划时刻生成稳定 Occurrence ID，接入 Phase 2 Tick Ledger。
- 明确 DST、Misfire、Catch-up、启停和重复日程的行为。

### 4.4 CognitiveEvent 投递

- 实现 Pending/Delivered/Acknowledged/Expired/Cancelled、Lease holder 元数据和投递次数。
- 实现拉取、ACK 幂等、过期、摘要合并以及未 ACK 时向新 Holder 重投同一 Event ID。
- 将 Due Task/Event 加入结构化 Recall 高优先级 Route 和 `pending_event_ids`。

## 退出门禁

- [ ] Note Pin/Snooze/Review/Promotion/Forget 状态机和保留规则测试通过。
- [ ] Task/Step 非法转换、Expected Revision 竞争和 Dependency 环被稳定拒绝。
- [ ] 重复 Tick/Occurrence 只生成一个逻辑 CognitiveEvent。
- [ ] Event 重投和重复 ACK 幂等，ACK、Delivered 均不会推进 Task/Step 完成。
- [ ] 发送或工具执行失败不会产生完成 Evidence；成功 Observation 后仍需显式转换。
- [ ] 时区、DST、Catch-up 和过期 Policy 测试通过。

## 交付证据

- 代码/变更：待补充
- ADR：待补充
- Schema/Migration：待补充
- 时间/状态机测试报告：待补充
- 已知限制：待补充

## 明确不做

- 不执行 Task 中的外部动作，不保存可执行脚本或任意条件代码。
- 不支持跨 Task Dependency。
- 不让后台模型直接创建 Active Task 或高权威 Claim。

## 交接条件

Phase 5 可以使用 Note/Task Promotion seam 和 CognitiveEvent Evidence；Phase 11/12 可以依赖稳定的事件拉取/ACK 语义，但宿主映射仍后置。
