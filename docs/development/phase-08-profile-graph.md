# 阶段 8：Profile 与 Graph

> 状态：Completed（历史交付快照；不代表当前发布验收）  
> 开始日期：2026-09-03
> 完成日期：2026-09-03
> 前置阶段：[阶段 7](./phase-07-vector-recall.md)
> 阶段交付版本：0.9.0（阶段记录：Core/双 SDK 0.9.0、Schema 9、Contract 1.7.0）
> 架构依据：[§13.3 Relation](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#133-relation)、[§13.5 ProfileProjection](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#135-profileprojection)、[§18 Recall 协议](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#18-recall-协议)、[§22.5 Graph 与 Profile](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#225-graph-与-profile)、[§36 阶段 8](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#阶段-8profile-与-graph)

本页归档交付范围；测试实测、复审过程与限制集中在[验证报告](../reports/phase-08-verification.md)。后续状态与未关闭工作以[阶段索引](README.md)及[Phase 14](phase-14-hardening-release.md)为准。

2026-09-07 W01 当前接线候选已通过真实 ASGI 与安装后的 Core/Worker/公共 SDK 路由命中；2026-09-08 收尾 `ci-002` 已完整通过，见[W01 报告](../reports/w01-http-recall-assembly.md)。此更新不覆盖历史量化限制：Graph 原量化门禁仍归 W02，生产 Embedding 管理与真实出站仍归 W05。

2026-09-08 [W02](../reports/w02-graph-quantitative-gates.md)已通过真实 GraphRoute 深度/扇出/节点 10 倍压力与五类各 50 次 HTTP 查询竞态；收尾 CI、浏览器和隔离安装通过。原量化缺口已由本轮证据闭合，生产和稳定发布要求仍归后续工作包。

## 阶段目标

交付可重建 Profile/Graph 投影、逐边/端点隐私过滤、有界多跳 Recall、Builder/Watermark 信任门和 Canonical 降级。

## 架构约束

- Profile 是 Claim 的带来源汇总，不是独立事实源；Agent Persona 不能混入外部 Entity Profile。
- Graph 只投影 Canonical Relation、Verified Binding 和允许的 Claim，不从模型联想直接建边。
- 多跳遍历逐边执行 Scope/Privacy/Status/Valid Time/Tombstone 校验。
- Builder Version 未知、Watermark 超阈值或校验失败时不得读取投影。
- Binding/Redirect/SpaceGroup 变化只触发重建和失效，不重写历史事实。

## 需求追踪

| 需求 ID | 历史需求范围 |
| --- | --- |
| P8-PROFILE-01 | Profile 字段级来源、冲突、Freshness 与 Canonical 回退 |
| P8-GRAPH-01 | Graph 仅投影授权 Canonical 边并保存 Revision/Scope |
| P8-TRAVERSAL-01 | 多跳逐边授权、深度/扇出/节点/Token/Deadline 预算 |
| P8-INVALIDATION-01 | Binding/Redirect/SpaceGroup/Correct/Forget 触发失效 |
| P8-DEGRADE-01 | 未知/落后/损坏投影回退 Canonical Claim/Relation |

需求对应的实现、测试与复审证据统一见[验证报告](../reports/phase-08-verification.md)。

## 工作包

8.1 ProfileProjection；8.2 Relation Graph；8.3 受限 Graph Recall；8.4 管理与可观测性。

实现结果和复审记录统一见验证报告。

## 数据、契约与回退策略

本阶段 Migration、契约版本、兼容窗口和恢复证据见[验证报告](../reports/phase-08-verification.md)；决策依据：[ADR-0016](../adr/0016-phase8-profile-graph.md)、[ADR-0017](../adr/0017-contract-surface-alignment.md)。阶段版本是历史快照，不能作为当前部署支持范围；当前版本读取 [version-manifest](../../schemas/version-manifest.json)，升级/回退按[Phase 14](phase-14-hardening-release.md)验证。

## 量化验收基线

Graph/Profile 信任门、逐边隐私、预算/水位降级、重建/恢复和路由性能的历史实测与规模条件见[验证报告](../reports/phase-08-verification.md)；原计划的逐项门槛保存在[原阶段验收目标](../reports/phase-08-verification.md#原阶段验收目标)，未测目标不会因归档消失。

## 退出门禁

本阶段按[验证报告](../reports/phase-08-verification.md)记录关闭；报告中的测试规模、版本和基准只适用于当时快照。限制项不因 Completed 状态自动消失，正式发布须重新通过 Phase 14 门禁。

## 交付证据

- [阶段验证报告](../reports/phase-08-verification.md)：实现/测试追踪、复审修复、历史门禁和限制。
- 决策：[ADR-0016](../adr/0016-phase8-profile-graph.md)、[ADR-0017](../adr/0017-contract-surface-alignment.md)。
- [当前契约源](../../contracts/source/contracts.json)与[迁移文件](../../migrations/)；不在阶段摘要中复制版本、checksum 或端点清单。

## 已知限制

以[验证报告中的限制](../reports/phase-08-verification.md#8-已知限制)为唯一记录；报告同时标出已由后续阶段关闭的历史缺口。

## 明确不做

- 不把 Graph 当 Task Dependency 的状态源。
- 不用昵称、共同出现或向量相似直接创建 Relation。
- 不提供无界图遍历或跨 Privacy 边的"管理方便"旁路。

## 交接条件

本阶段能力已进入后续集成基线。新增工作遵循[阶段索引](README.md)与[Phase 14](phase-14-hardening-release.md)的依赖和验收，不重复执行本页历史工作包。
