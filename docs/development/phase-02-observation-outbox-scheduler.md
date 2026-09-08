# 阶段 2：Observation、Outbox 与持久调度

2026-09-08 新实施项：[Observation 全量上下文与批量总结](observation-context.md)。统一使用 Observation 保存背景/交互原始事件，复用 Episode 保存分组摘要；自动总结显式开启，背景默认保留 30 天可配置。已接通实现，使用方式与本轮验证进度见链接说明；下文历史验收记录仅代表当时版本。

> 状态：Completed（历史交付快照；不代表当前发布验收）  
> 前置阶段：[阶段 1](./phase-01-persistence-identity-scope.md)  
> 阶段交付版本：0.3.0（Schema 3）· 完成日期：2026-08-30 · 复核修复：2026-08-30  
> 架构依据：[§8 Observation](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#8-observation-journal)、[§16 Outbox](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#16-transactional-outbox-与-worker)、[§17 Schedule/Tick](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#17-持久化-scheduletick-与认知时钟)、[§25 Active Surface](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#25-可选-active-surface-coordinator)、[§36 阶段 2](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#阶段-2observationoutbox-与持久调度)、[ADR-0009](../adr/0009-phase2-reliability-spine.md)、[ADR-0010](../adr/0010-active-surface-coordinator.md)

本页归档交付范围；测试实测、复审过程与限制集中在[验证报告](../reports/phase-02-verification.md)。后续状态与未关闭工作以[阶段索引](README.md)及[Phase 14](phase-14-hardening-release.md)为准。

## 阶段目标

交付原子 Observation Batch/Cursor、Transactional Outbox、Worker Lease/Fencing、重试与背压、Schedule/Tick Ledger、时钟异常恢复及可选 Active Surface。

## 架构约束

- Core 只接受已确认生效的用户、助手、工具或外部事件；失败尝试只能进入 Audit/Event。
- Observation、Source Cursor、Agent Watermark 和 Outbox 必须在同一 SQLite 事务提交。
- Worker 提交前验证 Owner、Lease Generation、Expiry 和 Source Revision。
- Tick Ledger 是周期执行事实源；进程内 Timer 只负责唤醒。
- Observe 在线路径不得调用 LLM、Embedding 或大型投影任务。
- Active Surface Coordinator 是可选控制平面，不是 Scope、Privacy、Revision 或并发正确性的前提。

## 需求追踪

| 需求 ID | 历史需求范围 |
| --- | --- |
| P2-OBSERVE-01 | 实际效果、Batch、Source Stream/Cursor 与原子 Watermark |
| P2-OUTBOX-01 | Transactional Outbox、Lease Generation、Fencing 与 Dead Letter |
| P2-PRESSURE-01 | 容量、公平调度、磁盘与安全优先通道 |
| P2-CLOCK-01 | Persistent Schedule/Tick、时区、Misfire 与时钟异常 |
| P2-OBSERVABILITY-01 | 低敏日志、指标与健康检查 |
| P2-SURFACE-01 | Off/Advisory/Required Lease、Epoch 与旧 Holder Fencing |

需求对应的实现、测试与复审证据统一见[验证报告](../reports/phase-02-verification.md)。

## 工作包

2.1 Observation Journal；2.2 Transactional Outbox 与 Worker；2.3 背压与故障语义；2.4 Schedule、Tick Ledger 与 Clock；2.5 可观测性；2.6 可选 Active Surface Coordinator。

实现结果和复审记录统一见验证报告。

## 数据、契约与回退策略

本阶段 Migration、契约版本、兼容窗口和恢复证据见[验证报告](../reports/phase-02-verification.md)；决策依据：[ADR-0009](../adr/0009-phase2-reliability-spine.md)、[ADR-0010](../adr/0010-active-surface-coordinator.md)。阶段版本是历史快照，不能作为当前部署支持范围；当前版本读取 [version-manifest](../../schemas/version-manifest.json)，升级/回退按[Phase 14](phase-14-hardening-release.md)验证。

## 量化验收基线

Observation/Outbox 原子性、Worker Fencing、背压、Tick/时区/时钟异常与强杀恢复的历史实测与规模条件见[验证报告](../reports/phase-02-verification.md)；原计划的逐项门槛保存在[原阶段验收目标](../reports/phase-02-verification.md#原阶段验收目标)，未测目标不会因归档消失。

## 退出门禁

本阶段按[验证报告](../reports/phase-02-verification.md)记录关闭；报告中的测试规模、版本和基准只适用于当时快照。限制项不因 Completed 状态自动消失，正式发布须重新通过 Phase 14 门禁。

## 交付证据

- [阶段验证报告](../reports/phase-02-verification.md)：实现/测试追踪、复审修复、历史门禁和限制。
- 决策：[ADR-0009](../adr/0009-phase2-reliability-spine.md)、[ADR-0010](../adr/0010-active-surface-coordinator.md)。
- [当前契约源](../../contracts/source/contracts.json)与[迁移文件](../../migrations/)；不在阶段摘要中复制版本、checksum 或端点清单。

## 已知限制

以[验证报告中的限制](../reports/phase-02-verification.md#9-已知限制)为唯一记录；报告同时标出已由后续阶段关闭的历史缺口。

## 明确不做

- 不做 Episode/Claim 提取、Embedding 或 Provider 调用。
- 不把生成完成、发送失败、取消输出保存为助手已说事实。
- 不让 Scheduler 执行外部动作或自动标记 Task 完成。

## 交接条件

本阶段能力已进入后续集成基线。新增工作遵循[阶段索引](README.md)与[Phase 14](phase-14-hardening-release.md)的依赖和验收，不重复执行本页历史工作包。
