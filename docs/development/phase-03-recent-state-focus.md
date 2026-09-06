# 阶段 3：近期上下文、State 与 Focus

> 状态：Completed（历史交付快照；不代表当前发布验收）  
> 前置阶段：[阶段 2](./phase-02-observation-outbox-scheduler.md)  
> 阶段交付版本：0.4.0（Schema 4）· 完成日期：2026-08-30  
> 架构依据：[§9 近期上下文、实时状态与关注项](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#9-近期上下文实时状态与关注项)、[§15.4 Recall](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#154-recall)、[§18 Recall 协议](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#18-recall-协议)、[§30 性能与容量](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#30-性能与容量目标)、[§36 阶段 3](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#阶段-3近期上下文state-与-focus)、[ADR-0011](../adr/0011-phase3-recent-state-focus.md)

本页归档交付范围；测试实测、复审过程与限制集中在[验证报告](../reports/phase-03-verification.md)。后续状态与未关闭工作以[阶段索引](README.md)及[Phase 14](phase-14-hardening-release.md)为准。

## 阶段目标

交付有界 Recent Context 投影、State Revision/Coalescing、Focus 生命周期与容量整理，以及结构化 Recall 骨架。完整 Recall 契约和并行路由由 Phase 6 接续。

## 架构约束

- RecentContextProjection 是 Observation 的可重建窗口；FocusItem 是 Canonical 认知对象，两者不得合并。
- 原始近期内容默认不跨 Space，共享只通过显式 Agent/SpaceGroup Scope 的 Focus 或后续长期对象。
- State Coalescing 只能合并投影 Job，不能跳过 Canonical State Revision。
- Focus 的衰减只改变 Activation/状态，不能提高事实 Confidence 或删除来源。
- 所有返回项仍执行 Scope、Privacy、Expiry、Revision 和 Tombstone 校验。

## 需求追踪

| 需求 ID | 历史需求范围 |
| --- | --- |
| P3-RECENT-01 | RecentContextProjection 只引用已提交 Observation，且按版本/Watermark 可重建 |
| P3-STATE-01 | State 当前指针、不可变 Revision、TTL 与 Namespace Policy |
| P3-FOCUS-01 | Focus 容量、状态机、衰减、激活与晋升 |
| P3-RECALL-01 | 结构化 Route、Deadline、预算、稳定排序与降级 Envelope |
| P3-RECOVERY-01 | Current Pointer、投影与时间状态在崩溃/重启后恢复 |

需求对应的实现、测试与复审证据统一见[验证报告](../reports/phase-03-verification.md)。

## 工作包

3.1 RecentContextProjection（已完成）；3.2 StateRecord（已完成）；3.3 FocusItem（已完成）；3.4 结构化 Recall（已完成，内部骨架）。

实现结果和复审记录统一见验证报告。

## 数据、契约与回退策略

本阶段 Migration、契约版本、兼容窗口和恢复证据见[验证报告](../reports/phase-03-verification.md)；决策依据：[ADR-0011](../adr/0011-phase3-recent-state-focus.md)。阶段版本是历史快照，不能作为当前部署支持范围；当前版本读取 [version-manifest](../../schemas/version-manifest.json)，升级/回退按[Phase 14](phase-14-hardening-release.md)验证。

## 量化验收基线

Recent 重建、State/Focus 生命周期、硬过滤性质与结构化读取/写入延迟的历史实测与规模条件见[验证报告](../reports/phase-03-verification.md)；原计划的逐项门槛保存在[原阶段验收目标](../reports/phase-03-verification.md#原阶段验收目标)，未测目标不会因归档消失。

## 退出门禁

本阶段按[验证报告](../reports/phase-03-verification.md)记录关闭；报告中的测试规模、版本和基准只适用于当时快照。限制项不因 Completed 状态自动消失，正式发布须重新通过 Phase 14 门禁。

## 交付证据

- [阶段验证报告](../reports/phase-03-verification.md)：实现/测试追踪、复审修复、历史门禁和限制。
- 决策：[ADR-0011](../adr/0011-phase3-recent-state-focus.md)。
- [当前契约源](../../contracts/source/contracts.json)与[迁移文件](../../migrations/)；不在阶段摘要中复制版本、checksum 或端点清单。

## 已知限制

以[验证报告中的限制](../reports/phase-03-verification.md#10-已知限制)为唯一记录；报告同时标出已由后续阶段关闭的历史缺口。

## 明确不做

- 不实现 Note/Task、长期 Claim、FTS 或向量召回。
- 不用自由文本摘要代替原始 Observation 或作为独立 Evidence。
- 不把 Working/Focus 当作无限增长的第二份聊天历史。

## 交接条件

本阶段能力已进入后续集成基线。新增工作遵循[阶段索引](README.md)与[Phase 14](phase-14-hardening-release.md)的依赖和验收，不重复执行本页历史工作包。
