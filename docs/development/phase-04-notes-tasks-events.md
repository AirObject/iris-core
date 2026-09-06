# 阶段 4：Note、Task 与 CognitiveEvent

> 状态：Completed（历史交付快照；不代表当前发布验收）  
> 前置阶段：[阶段 3](./phase-03-recent-state-focus.md)  
> 阶段交付版本：0.5.0（Schema 5）· 完成日期：2026-08-31  
> 架构依据：[§10 Note](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#10-note)、[§11 Task 与前瞻记忆](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#11-tasktaskstep-与前瞻记忆)、[§12 CognitiveEvent](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#12-cognitiveevent-与宿主投递)、[§17 Schedule/Tick](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#17-持久化-scheduletick-与认知时钟)、[§36 阶段 4](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#阶段-4notetask-与-cognitiveevent)、[ADR-0012](../adr/0012-phase4-notes-tasks-events.md)

本页归档交付范围；测试实测、复审过程与限制集中在[验证报告](../reports/phase-04-verification.md)。后续状态与未关闭工作以[阶段索引](README.md)及[Phase 14](phase-14-hardening-release.md)为准。

## 阶段目标

交付 Note、Task/Step/Dependency/Trigger、Occurrence Ledger、CognitiveEvent 拉取/ACK/重投，以及确定性日程和状态触发。

## 架构约束

- Note 是独立 Canonical 对象，`review_after` 不是删除时间，Pin/承诺/活动 Task 来源不得自动删除。
- 对话提取的 Task 默认是 `proposed`；只有显式工具、确定性策略或管理员能激活。
- Task/Step 完成需要独立状态转换；外部效果要求实际成功 Observation/Evidence。
- CognitiveEvent 采用 At-least-once；ACK 只表示宿主承担处理责任，不表示 Task 完成。
- Trigger 只允许声明式语法和操作符，禁止任意代码、脚本或隐式工具执行。

## 需求追踪

| 需求 ID | 历史需求范围 |
| --- | --- |
| P4-NOTE-01 | Note 独立生命周期、复查、保留与晋升 |
| P4-TASK-01 | Task/Step Revision、Evidence、状态转换与依赖 |
| P4-TRIGGER-01 | 受限 Trigger、时区、Occurrence 幂等与 Misfire |
| P4-EVENT-01 | CognitiveEvent At-least-once、ACK、重投与过期 |
| P4-SAFETY-01 | ACK/投递/失败不得伪造 Task 完成或外部效果 |

需求对应的实现、测试与复审证据统一见[验证报告](../reports/phase-04-verification.md)。

## 工作包

4.1 Note 生命周期（已完成）；4.2 Task 与 Step（已完成）；4.3 Trigger 与前瞻记忆（已完成）；4.4 CognitiveEvent 投递（已完成）。

实现结果和复审记录统一见验证报告。

## 数据、契约与回退策略

本阶段 Migration、契约版本、兼容窗口和恢复证据见[验证报告](../reports/phase-04-verification.md)；决策依据：[ADR-0012](../adr/0012-phase4-notes-tasks-events.md)。阶段版本是历史快照，不能作为当前部署支持范围；当前版本读取 [version-manifest](../../schemas/version-manifest.json)，升级/回退按[Phase 14](phase-14-hardening-release.md)验证。

## 量化验收基线

Task/Step 状态机、依赖环、Occurrence 去重、ACK/重投与 Lease 竞态的历史实测与规模条件见[验证报告](../reports/phase-04-verification.md)；原计划的逐项门槛保存在[原阶段验收目标](../reports/phase-04-verification.md#原阶段验收目标)，未测目标不会因归档消失。

## 退出门禁

本阶段按[验证报告](../reports/phase-04-verification.md)记录关闭；报告中的测试规模、版本和基准只适用于当时快照。限制项不因 Completed 状态自动消失，正式发布须重新通过 Phase 14 门禁。

## 交付证据

- [阶段验证报告](../reports/phase-04-verification.md)：实现/测试追踪、复审修复、历史门禁和限制。
- 决策：[ADR-0012](../adr/0012-phase4-notes-tasks-events.md)。
- [当前契约源](../../contracts/source/contracts.json)与[迁移文件](../../migrations/)；不在阶段摘要中复制版本、checksum 或端点清单。

## 已知限制

以[验证报告中的限制](../reports/phase-04-verification.md#7-已知限制)为唯一记录；报告同时标出已由后续阶段关闭的历史缺口。

## 明确不做

- 不执行 Task 中的外部动作，不保存可执行脚本或任意条件代码。
- 不支持跨 Task Dependency。
- 不让后台模型直接创建 Active Task 或高权威 Claim。

## 交接条件

本阶段能力已进入后续集成基线。新增工作遵循[阶段索引](README.md)与[Phase 14](phase-14-hardening-release.md)的依赖和验收，不重复执行本页历史工作包。
