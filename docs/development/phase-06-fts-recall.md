# 阶段 6：FTS Recall

> 状态：Completed（历史交付快照；不代表当前发布验收）  
> 开始日期：2026-09-02  
> 完成日期：2026-09-02  
> 前置阶段：[阶段 5](./phase-05-long-term-memory.md)  
> 阶段交付版本：0.7.0（阶段记录：Core/双 SDK 0.7.0、Schema 7、Contract 1.5.0）  
> 架构依据：[§18 Recall 协议](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#18-recall-协议)、[§22.1 FTS5](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#221-fts5)、[§23 HTTP API](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#23-http-api-与能力协商)、[§30 性能与容量](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#30-性能与容量目标)、[§36 阶段 6](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#阶段-6fts-recall)

本页归档交付范围；测试实测、复审过程与限制集中在[验证报告](../reports/phase-06-verification.md)。后续状态与未关闭工作以[阶段索引](README.md)及[Phase 14](phase-14-hardening-release.md)为准。

## 阶段目标

交付 FTS5 版本化影子投影、完整 Recall/Search/Usage 契约、并发 Deadline、Canonical Rehydrate、确定性排序与 Token/Layer 预算。

## 架构约束

- FTS 是绑定具体 Resource Revision 的可重建投影，不能成为事实源。
- 缓存、FTS 和其他派生候选返回前必须 Canonical Rehydrate，重新执行 Scope/Privacy/Status/Time/Tombstone 检查。
- Persona 通过响应顶层 Revision/Hash 协调，不作为普通 Memory Candidate。
- Deadline 使用单调时钟；任一 Route 超时不能阻塞已完成 Route。
- 排序器版本化、确定性且区分缺失分值；Token 裁剪保障 Due Task、Focus 和当前说话人必要身份。

## 需求追踪

| 需求 ID | 历史需求范围 |
| --- | --- |
| P6-FTS-01 | FTS 绑定 Resource Revision、版本化 Builder 与影子重建 |
| P6-RECALL-01 | Recall Request/Response、Route、Deadline、Watermark 与 Partial |
| P6-REHYDRATE-01 | 所有派生候选最终回读 Canonical 并重做硬过滤 |
| P6-RANK-01 | 版本化确定性排序、冲突/去重、Layer/Token Budget |
| P6-USAGE-01 | Usage 四阶段、子集验证和幂等合并 |
| P6-PERF-01 | 结构化/FTS 延迟、容量保护和可解释降级 |

需求对应的实现、测试与复审证据统一见[验证报告](../reports/phase-06-verification.md)。

## 工作包

6.1 FTS5 Projection；6.2 Recall Orchestrator；6.3 Rehydrate、融合与预算；6.4 Usage Report 与契约发布；6.5 性能与降级。

实现结果和复审记录统一见验证报告。

## 数据、契约与回退策略

本阶段 Migration、契约版本、兼容窗口和恢复证据见[验证报告](../reports/phase-06-verification.md)；决策依据：[ADR-0014](../adr/0014-phase6-fts-recall.md)、[ADR-0017](../adr/0017-contract-surface-alignment.md)。阶段版本是历史快照，不能作为当前部署支持范围；当前版本读取 [version-manifest](../../schemas/version-manifest.json)，升级/回退按[Phase 14](phase-14-hardening-release.md)验证。

## 量化验收基线

FTS 生命周期、确定性重放、硬过滤/删除竞态、Usage 子集和结构化/FTS p95的历史实测与规模条件见[验证报告](../reports/phase-06-verification.md)；原计划的逐项门槛保存在[原阶段验收目标](../reports/phase-06-verification.md#原阶段验收目标)，未测目标不会因归档消失。

## 退出门禁

本阶段按[验证报告](../reports/phase-06-verification.md)记录关闭；报告中的测试规模、版本和基准只适用于当时快照。限制项不因 Completed 状态自动消失，正式发布须重新通过 Phase 14 门禁。

## 交付证据

- [阶段验证报告](../reports/phase-06-verification.md)：实现/测试追踪、复审修复、历史门禁和限制。
- 决策：[ADR-0014](../adr/0014-phase6-fts-recall.md)、[ADR-0017](../adr/0017-contract-surface-alignment.md)。
- [当前契约源](../../contracts/source/contracts.json)与[迁移文件](../../migrations/)；不在阶段摘要中复制版本、checksum 或端点清单。

## 已知限制

以[验证报告中的限制](../reports/phase-06-verification.md#8-已知限制)为唯一记录；报告同时标出已由后续阶段关闭的历史缺口。

## 明确不做

- 不实现向量语义、Graph 多跳或 Provider 在线调用。
- 不让 Cache/FTS 命中绕过 Canonical Rehydrate。
- 不把普通记忆 Candidate 注入宿主 Persona/System Slot。

## 交接条件

本阶段能力已进入后续集成基线。新增工作遵循[阶段索引](README.md)与[Phase 14](phase-14-hardening-release.md)的依赖和验收，不重复执行本页历史工作包。
