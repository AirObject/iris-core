# 阶段 2：Observation、Outbox 与持久调度

> 状态：Planned  
> 前置阶段：[阶段 1](./phase-01-persistence-identity-scope.md)  
> 目标版本：0.3.0  
> 架构依据：[§8 Observation](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#8-observation-journal)、[§16 Outbox](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#16-transactional-outbox-与-worker)、[§17 Schedule/Tick](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#17-持久化-scheduletick-与认知时钟)、[§25 Active Surface](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#25-可选-active-surface-coordinator)、[§36 阶段 2](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#阶段-2observationoutbox-与持久调度)

## 阶段目标

打通“真实外部效果 → Canonical Observation → 可靠异步任务”的持久化脊柱，建立不依赖进程内 Timer 的认知时钟，并补齐宿主接入需要的可选 Active Surface Lease/Epoch。阶段结束时，重复、宕机、旧 Worker/Holder、时钟跳变和队列背压均不能破坏事实正确性。

## 架构约束

- Core 只接受已确认生效的用户、助手、工具或外部事件；失败尝试只能进入 Audit/Event。
- Observation、Source Cursor、Agent Watermark 和 Outbox 必须在同一 SQLite 事务提交。
- Worker 提交前验证 Owner、Lease Generation、Expiry 和 Source Revision。
- Tick Ledger 是周期执行事实源；进程内 Timer 只负责唤醒。
- Observe 在线路径不得调用 LLM、Embedding 或大型投影任务。
- Active Surface Coordinator 是可选控制平面，不是 Scope、Privacy、Revision 或并发正确性的前提。

## 需求追踪

| 需求 ID | 基线要求 | 工作包 | 验证门禁 |
| --- | --- | --- | --- |
| P2-OBSERVE-01 | 实际效果、Batch、Source Stream/Cursor 与原子 Watermark | 2.1 | 重复、Gap、乱序、Crash 测试 |
| P2-OUTBOX-01 | Transactional Outbox、Lease Generation、Fencing 与 Dead Letter | 2.2 | Kill-9、旧 Worker、重放测试 |
| P2-PRESSURE-01 | 容量、公平调度、磁盘与安全优先通道 | 2.3 | 软/硬阈值和 Ready 测试 |
| P2-CLOCK-01 | Persistent Schedule/Tick、时区、Misfire 与时钟异常 | 2.4 | DST、前跳、回拨、Catch-up 测试 |
| P2-OBSERVABILITY-01 | 低敏日志、指标与健康检查 | 2.5 | 泄漏扫描与健康契约测试 |
| P2-SURFACE-01 | Off/Advisory/Required Lease、Epoch 与旧 Holder Fencing | 2.6 | 并发抢占、重启和模式矩阵测试 |

## 工作包

### 2.1 Observation Journal

- 实现单条/批量 Observation、Artifact 引用占位、`effect_state=committed|partial` 和实际效果校验。
- 实现 Source Stream/Cursor 的单调推进、重复、Gap Policy 以及和 Idempotency 的独立语义。
- `POST /v1/observations:batch` 返回 accepted/duplicate IDs、Source/Agent Watermark 和 Outbox 状态。

### 2.2 Transactional Outbox 与 Worker

- 实现 Outbox 领取、优先级、Generation Fencing、Retry/Backoff、Dead Letter 和安全重放。
- 对 State/Profile/Graph 类任务支持保留最高 Source Revision 的 Coalescing；明确禁止 Observation、Forget、Task Occurrence 等丢弃式合并。
- Worker 只能通过 Application/Domain Service 提交结果，不能直接改 Current Pointer。

### 2.3 背压与故障语义

- 对队列数、Payload 字节、Tenant/Agent、公平调度、Worker 批量和磁盘余量设软/硬限制。
- 保留 Forget、Correct 和安全操作的优先通道；硬阈值返回 `storage_full`。
- 健康信息显式报告 Queue Lag、Oldest Pending、Dead Letter 和不可写状态。

### 2.4 Schedule、Tick Ledger 与 Clock

- 实现受限 Schedule、IANA 时区、Catch-up/Misfire Policy、Occurrence Key 和 Tick/Outbox 原子写入。
- 使用可注入 Wall/Monotonic Clock 处理 DST、前跳、回拨、休眠和重启补算。
- 建立后续 Phase 使用的周期 Job Kind 注册表，但只启用已有安全 Handler。

### 2.5 可观测性

- 增加 Observation、Outbox、Schedule、SQLite Busy、磁盘与 Worker 指标。
- 日志和 Trace 只记录 ID Hash、数量、Revision、错误码和耗时，不记录正文。
- 扩展 `/health/live` 与 `/health/ready` 的 Scheduler/Worker Lag 判断。

### 2.6 可选 Active Surface Coordinator

- 实现 Agent 级唯一 Lease、Acquire/Preempt/Heartbeat/Release/Expiry、单调 Epoch 和旧 Holder Fencing。
- 实现 `off|advisory|required` 策略与 `lease_held|lease_expired|lease_fenced` 错误；任何模式都不改变数据授权语义。
- 划分应用在线平面与管理/维护平面：Required 模式校验在线 Observe/Recall/认知操作，内部 Worker、备份和 Persona 管理不冒充活动宿主。
- 抢占先 Fence 旧 Holder，再发出撤销通知；Coordinator 故障不得损坏 Canonical 数据。

## 数据、契约与回退策略

- Observation、Cursor、Watermark、Outbox、Schedule、Tick、Lease 和 Epoch 表通过可向前兼容的增量 Migration 引入；新字段先允许旧 Worker 忽略，再切换必填约束。
- Worker Job Payload 和事件使用显式版本；滚动部署期间新 Worker 可读取上一版本，旧 Worker 不领取未知 Job Kind。
- Coordinator 从 `off` 开始部署，完成 Epoch/Fencing 验证后才能切换 `advisory` 或 `required`；回退到 `off` 不删除 Lease 历史。
- Schema 回退遵循 Phase 1 的备份/兼容二进制策略；Outbox、Tick 和 Cursor 记录不得通过 Down Migration 删除或回拨。

## 量化验收基线

- 单条 Observation p95 ≤ 30 ms，100 条 Batch p95 ≤ 150 ms；报告声明硬件、并发、Payload 和数据库状态。
- 每个事务边界和 Worker 提交边界的 Kill-9 场景至少重复 20 次，逻辑效果不得丢失或重复。
- 50 个并发 Worker/Holder 的抢占测试中，过期 Generation/Epoch 的提交成功数必须为 0。
- 时间测试至少覆盖 UTC、一个有 DST 的正时区、一个有 DST 的负时区和一个无 DST 的 IANA 时区，并覆盖前跳、回拨和重复时刻。
- 软/硬队列和磁盘阈值必须配置化；达到硬阈值时普通写稳定返回 `storage_full`，Forget/Correct 安全通道仍可用。

## 退出门禁

- [ ] Batch 请求级校验保持原子性，重复 Event/Cursor/Idempotency 不产生重复事实。
- [ ] 在事务前、事务后、Worker 执行中和提交前 Kill -9，恢复后无丢失或重复逻辑效果。
- [ ] Lease 过期后的旧 Worker 即使完成计算也无法提交。
- [ ] Active Surface 并发 Acquire/Preempt/Heartbeat、旧 Epoch、重启重新获取及 Off/Advisory/Required 测试通过。
- [ ] Cursor 对账、Gap、乱序和 Crash 恢复测试通过。
- [ ] DST、时钟回拨/前跳、休眠、Catch-up 上限和重复 Occurrence 测试通过。
- [ ] 磁盘/队列背压时 Ready、错误码和安全优先通道符合契约。
- [ ] Migration/兼容/回退方案、需求追踪和交付证据已完成评审。

## 交付证据

- 代码/变更：待补充
- ADR：待补充
- Schema/Migration：待补充
- 故障测试报告：待补充
- 已知限制：待补充

## 明确不做

- 不做 Episode/Claim 提取、Embedding 或 Provider 调用。
- 不把生成完成、发送失败、取消输出保存为助手已说事实。
- 不让 Scheduler 执行外部动作或自动标记 Task 完成。

## 交接条件

Phase 3 可以依赖幂等 Observation 流、固定 Source Revision/Watermark、可靠 Outbox、可注入时钟、持久 Schedule/Tick 和可选 Active Surface Lease/Epoch 机制。
