# 阶段 7：Vector Recall

> 状态：Completed（历史交付快照；不代表当前发布验收）  
> 开始日期：2026-09-03
> 完成日期：2026-09-03
> 前置阶段：[阶段 6](./phase-06-fts-recall.md)
> 阶段交付版本：0.8.0（阶段记录：Core/双 SDK 0.8.0、Schema 8、Contract 1.6.0）
> 架构依据：[§22.2 Embedding](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#222-embedding)、[§22.3 FAISS Generation](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#223-faiss-generation)、[§22.4 并发规则](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#224-并发规则)、[§24 Provider 边界](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#24-provider-边界)、[§30 性能与容量](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#30-性能与容量目标)、[§36 阶段 7](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#阶段-7vector-recall)

本页归档交付范围；测试实测、复审过程与限制集中在[验证报告](../reports/phase-07-verification.md)。后续状态与未关闭工作以[阶段索引](README.md)及[Phase 14](phase-14-hardening-release.md)为准。

## 阶段目标

交付 Embedding Port、独立 int64 ID Map、不可变 FAISS Generation/信任门、COW Handle Swap、故障回退与 Hybrid Recall。

## 架构约束

- 公共 UUID 与 FAISS `int64` ID 严格分离，映射由服务端持久化且不可哈希碰撞替代。
- 单个不可变 FAISS Handle 只读；Search 不与 Add/Remove/Write 并发。
- 不同 Model/Dimension/Normalization/Template 不得混入同一 Generation。
- 新 Generation 完整构建、Flush、校验后才原子切换；失败继续使用上一已验证版本并标记降级。
- Vector Candidate 仍受最终 Canonical Rehydrate 和 Tombstone 优先级约束。

## 需求追踪

| 需求 ID | 历史需求范围 |
| --- | --- |
| P7-EMBED-01 | Provider Port、启动 Probe、输入最小化与输出验证 |
| P7-IDMAP-01 | UUID 与 signed `int64` 映射唯一、持久、可恢复 |
| P7-GEN-01 | 不可变 Generation、Manifest/Checksum 与原子切换 |
| P7-CONCURRENCY-01 | Search 与 Build/Swap 隔离，旧 Handle 安全释放 |
| P7-HYBRID-01 | Vector Route 可降级并沿用 Rehydrate/预算/Usage 契约 |

需求对应的实现、测试与复审证据统一见[验证报告](../reports/phase-07-verification.md)。

## 工作包

7.1 Embedding Port；7.2 Vector ID Map 与 Delta；7.3 FAISS Generation 生命周期；7.4 Hybrid Recall。

实现结果和复审记录统一见验证报告。

## 数据、契约与回退策略

本阶段 Migration、契约版本、兼容窗口和恢复证据见[验证报告](../reports/phase-07-verification.md)；决策依据：[ADR-0015](../adr/0015-phase7-vector-recall.md)。阶段版本是历史快照，不能作为当前部署支持范围；当前版本读取 [version-manifest](../../schemas/version-manifest.json)，升级/回退按[Phase 14](phase-14-hardening-release.md)验证。

## 量化验收基线

ID Map、Generation 构建/损坏/并发切换、Provider 故障及 Hybrid p95的历史实测与规模条件见[验证报告](../reports/phase-07-verification.md)；原计划的逐项门槛保存在[原阶段验收目标](../reports/phase-07-verification.md#原阶段验收目标)，未测目标不会因归档消失。

## 退出门禁

本阶段按[验证报告](../reports/phase-07-verification.md)记录关闭；报告中的测试规模、版本和基准只适用于当时快照。限制项不因 Completed 状态自动消失，正式发布须重新通过 Phase 14 门禁。

## 交付证据

- [阶段验证报告](../reports/phase-07-verification.md)：实现/测试追踪、复审修复、历史门禁和限制。
- 决策：[ADR-0015](../adr/0015-phase7-vector-recall.md)。
- [当前契约源](../../contracts/source/contracts.json)与[迁移文件](../../migrations/)；不在阶段摘要中复制版本、checksum 或端点清单。

## 已知限制

以[验证报告中的限制](../reports/phase-07-verification.md#8-已知限制)为唯一记录；报告同时标出已由后续阶段关闭的历史缺口。

## 明确不做

- 不在当前只读 Handle 上原地增删向量。
- 不因向量相似自动建立 Identity、Relation 或合并 Claim。
- 不让 Embedding Provider 故障阻塞 Observation、Forget、Correct 或结构化 Recall。

## 交接条件

本阶段能力已进入后续集成基线。新增工作遵循[阶段索引](README.md)与[Phase 14](phase-14-hardening-release.md)的依赖和验收，不重复执行本页历史工作包。
