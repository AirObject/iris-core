# 阶段 9：完整 Persona

> 状态：Completed（历史交付快照；不代表当前发布验收）  
> 开始日期：2026-09-03
> 完成日期：2026-09-04
> 前置阶段：[阶段 8](./phase-08-profile-graph.md)
> 阶段交付版本：0.10.0（阶段记录：Core/双 SDK 0.10.0、Schema 10、Contract 1.8.0）
> 架构依据：[§14 Persona 系统](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#14-persona-系统)、[§18 Recall 协议](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#18-recall-协议)、[§23.3 Persona API](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#233-主要端点)、[§29.3 Prompt 与模型安全](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#293-prompt-与模型安全)、[§36 阶段 9](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#阶段-9完整-persona)

本页归档交付范围；测试实测、复审过程与限制集中在[验证报告](../reports/phase-09-verification.md)。后续状态与未关闭工作以[阶段索引](README.md)及[Phase 14](phase-14-hardening-release.md)为准。

## 阶段目标

交付 Persona Core/Trait/Narrative Revision、State TTL、Policy/Proposal、受控发布与回滚、Current/History、通知引用和管理权限边界。

## 架构约束

- Persona Core 永远不能由模型自动修改；管理平面和应用平面凭据隔离。
- PersonaRevision 不可变，发布/回滚均创建新 Revision，Current Pointer 使用 Expected Revision 串行化。
- Persona State 有 TTL、白名单和值域，到期确定性回归 Baseline。
- 单一用户、单次情绪、Prompt Injection 或无效 Evidence 不能改变 Trait/Core。
- Persona 只进入宿主可信 Persona Slot，不能覆盖宿主安全、工具或平台策略。

## 需求追踪

| 需求 ID | 历史需求范围 |
| --- | --- |
| P9-REVISION-01 | 不可变 Core/Trait/Narrative Revision、Pointer、Hash 与历史 |
| P9-STATE-01 | Persona State 白名单、TTL、衰减与 Baseline 回归 |
| P9-POLICY-01 | locked/manual/bounded_auto、证据、幅度与冷却期 |
| P9-PROPOSAL-01 | 结构化 Proposal、Field Delta、Stale Base 与审批 |
| P9-PUBLISH-01 | 原子发布/回滚、通知、失效与多端一致 |
| P9-SECURITY-01 | 管理平面隔离，Persona 不覆盖宿主安全策略 |

需求对应的实现、测试与复审证据统一见[验证报告](../reports/phase-09-verification.md)。

## 工作包

9.1 Persona Revision 与读取；9.2 Persona State；9.3 Policy 与 Proposal；9.4 发布、回滚与通知；9.5 安全与管理。

实现结果和复审记录统一见验证报告。

## 数据、契约与回退策略

本阶段 Migration、契约版本、兼容窗口和恢复证据见[验证报告](../reports/phase-09-verification.md)；决策依据：[ADR-0018](../adr/0018-phase9-persona.md)。阶段版本是历史快照，不能作为当前部署支持范围；当前版本读取 [version-manifest](../../schemas/version-manifest.json)，升级/回退按[Phase 14](phase-14-hardening-release.md)验证。

## 量化验收基线

Persona Policy/证据性质、并发发布、Revision/Hash、TTL/回滚与 Current 读取延迟的历史实测与规模条件见[验证报告](../reports/phase-09-verification.md)；原计划的逐项门槛保存在[原阶段验收目标](../reports/phase-09-verification.md#原阶段验收目标)，未测目标不会因归档消失。

## 退出门禁

本阶段按[验证报告](../reports/phase-09-verification.md)记录关闭；报告中的测试规模、版本和基准只适用于当时快照。限制项不因 Completed 状态自动消失，正式发布须重新通过 Phase 14 门禁。

## 交付证据

- [阶段验证报告](../reports/phase-09-verification.md)：实现/测试追踪、复审修复、历史门禁和限制。
- 决策：[ADR-0018](../adr/0018-phase9-persona.md)。
- [当前契约源](../../contracts/source/contracts.json)与[迁移文件](../../migrations/)；不在阶段摘要中复制版本、checksum 或端点清单。

## 已知限制

以[验证报告中的限制](../reports/phase-09-verification.md#5-已知限制)为唯一记录；报告同时标出已由后续阶段关闭的历史缺口。

## 明确不做

- 不保存模型私有 Chain-of-Thought。
- 不把用户 Profile、普通 Claim 或一次性措辞直接当作 Persona Current。
- 不允许普通应用 Token 发布、审批或回滚 Persona。

## 交接条件

本阶段能力已进入后续集成基线。新增工作遵循[阶段索引](README.md)与[Phase 14](phase-14-hardening-release.md)的依赖和验收，不重复执行本页历史工作包。
