# 阶段 1：持久化内核、身份与空间

> 状态：Completed（历史交付快照；不代表当前发布验收）  
> 前置阶段：[阶段 0](./phase-00-architecture-scaffold.md)  
> 阶段交付版本：0.2.0  
> 开始日期：2026-08-29  
> 完成日期：2026-08-29  
> 架构依据：[§5 租户与空间](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#5-租户agent-与空间模型)、[§6 身份](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#6-身份与实体模型)、[§20 SQLite](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#20-sqlite-canonical-store)、[§21 备份恢复](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#21-备份恢复与导出)、[§36 阶段 1](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#阶段-1持久化内核身份与空间)

本页归档交付范围；测试实测、复审过程与限制集中在[验证报告](../reports/phase-01-verification.md)。后续状态与未关闭工作以[阶段索引](README.md)及[Phase 14](phase-14-hardening-release.md)为准。

## 阶段目标

交付 SQLite WAL 与 Runtime Guard、UoW、Scope/Privacy、身份与绑定历史、Revision/Watermark、幂等与审计、Tombstone，以及在线备份和原子恢复。Agent 创建同时建立初始 Persona。

## 架构约束

- SQLite 规范化领域记录与不可变 Revision 是唯一事实源；当前表只保存指针和索引字段。
- Scope 的 `null` 是“该维向下可见”而不是请求通配符，权限由服务端 `AccessContext` 推导。
- 外部账号唯一键是 `(tenant_id, provider, realm, external_id)`，昵称不参与身份合并。
- Binding/Redirect 变化不回写历史 Observation，并必须同时保留发生时与当前解析视图的能力。
- Tombstone、Idempotency、Audit 和 Agent Watermark 从本阶段起就是事务基础设施，不得后补成旁路。

## 需求追踪

| 需求 ID | 历史需求范围 |
| --- | --- |
| P1-STORE-01 | SQLite Runtime、短事务、UoW 与兼容迁移 |
| P1-SCOPE-01 | Tenant/Agent/SpaceGroup/Space、Scope Null 与 Privacy |
| P1-IDENTITY-01 | Entity、ExternalIdentity、Binding、Redirect 与双身份视图 |
| P1-CONSISTENCY-01 | Revision、Watermark、Idempotency、Audit、Tombstone |
| P1-RECOVERY-01 | 一致性备份、隔离恢复与校验 |

需求对应的实现、测试与复审证据统一见[验证报告](../reports/phase-01-verification.md)。

## 工作包

1.1 SQLite Runtime 与事务层；1.2 Tenant、Agent 与空间；1.3 Identity Registry；1.4 通用一致性设施；1.5 备份与验证。

实现结果和复审记录统一见验证报告。

## 数据、契约与回退策略

本阶段 Migration、契约版本、兼容窗口和恢复证据见[验证报告](../reports/phase-01-verification.md)；决策依据：[ADR-0002](../adr/0002-scope-null-semantics.md)、[ADR-0003](../adr/0003-identity-and-binding.md)、[ADR-0004](../adr/0004-immutable-revisions.md)、[ADR-0005](../adr/0005-tombstone-priority.md)、[ADR-0008](../adr/0008-persona-bootstrap-seam.md)。阶段版本是历史快照，不能作为当前部署支持范围；当前版本读取 [version-manifest](../../schemas/version-manifest.json)，升级/回退按[Phase 14](phase-14-hardening-release.md)验证。

## 量化验收基线

Scope/身份性质、Revision/幂等并发、Runtime Guard、恢复中断与小规模 RPO/RTO的历史实测与规模条件见[验证报告](../reports/phase-01-verification.md)；原计划的逐项门槛保存在[原阶段验收目标](../reports/phase-01-verification.md#原阶段验收目标)，未测目标不会因归档消失。

## 退出门禁

本阶段按[验证报告](../reports/phase-01-verification.md)记录关闭；报告中的测试规模、版本和基准只适用于当时快照。限制项不因 Completed 状态自动消失，正式发布须重新通过 Phase 14 门禁。

## 交付证据

- [阶段验证报告](../reports/phase-01-verification.md)：实现/测试追踪、复审修复、历史门禁和限制。
- 决策：[ADR-0002](../adr/0002-scope-null-semantics.md)、[ADR-0003](../adr/0003-identity-and-binding.md)、[ADR-0004](../adr/0004-immutable-revisions.md)、[ADR-0005](../adr/0005-tombstone-priority.md)、[ADR-0008](../adr/0008-persona-bootstrap-seam.md)。
- [当前契约源](../../contracts/source/contracts.json)与[迁移文件](../../migrations/)；不在阶段摘要中复制版本、checksum 或端点清单。

## 已知限制

以[验证报告中的限制](../reports/phase-01-verification.md#已知限制)为唯一记录；报告同时标出已由后续阶段关闭的历史缺口。

## 明确不做

- 不实现 Observation、检索索引、完整 Persona 演进或宿主租约协调。
- 不按昵称、文本相似或模型判断自动建立 Verified Binding。
- 不支持多主 SQLite 或网络文件系统承载运行数据库。

## 交接条件

本阶段能力已进入后续集成基线。新增工作遵循[阶段索引](README.md)与[Phase 14](phase-14-hardening-release.md)的依赖和验收，不重复执行本页历史工作包。
