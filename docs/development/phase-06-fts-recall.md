# 阶段 6：FTS Recall

> 状态：Completed（含发布前外部复审两轮修复）  
> 负责人：Iris Memory Core Team  
> 开始日期：2026-09-02  
> 完成日期：2026-09-02  
> 前置阶段：[阶段 5](./phase-05-long-term-memory.md)  
> 目标版本：0.7.0（已达成：Core/双 SDK 0.7.0、Schema 7、Contract 1.5.0）  
> 架构依据：[§18 Recall 协议](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#18-recall-协议)、[§22.1 FTS5](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#221-fts5)、[§23 HTTP API](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#23-http-api-与能力协商)、[§30 性能与容量](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#30-性能与容量目标)、[§36 阶段 6](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#阶段-6fts-recall)

## 阶段目标

交付第一个完整、可解释、无向量依赖的 Recall 协议：组合结构化 Route 与 FTS 候选，在 Deadline 和 Token Budget 内执行最终 Canonical Rehydrate，并明确报告完整、部分与降级结果及宿主实际使用情况。

## 架构约束

- FTS 是绑定具体 Resource Revision 的可重建投影，不能成为事实源。
- 缓存、FTS 和其他派生候选返回前必须 Canonical Rehydrate，重新执行 Scope/Privacy/Status/Time/Tombstone 检查。
- Persona 通过响应顶层 Revision/Hash 协调，不作为普通 Memory Candidate。
- Deadline 使用单调时钟；任一 Route 超时不能阻塞已完成 Route。
- 排序器版本化、确定性且区分缺失分值；Token 裁剪保障 Due Task、Focus 和当前说话人必要身份。

## 需求追踪

| 需求 ID | 基线要求 | 工作包 | 验证门禁 |
| --- | --- | --- | --- |
| P6-FTS-01 | FTS 绑定 Resource Revision、版本化 Builder 与影子重建 | 6.1 | 重建、切换、旧 Revision 与损坏测试 |
| P6-RECALL-01 | Recall Request/Response、Route、Deadline、Watermark 与 Partial | 6.2 | Schema/Fixture、超时和一致性测试 |
| P6-REHYDRATE-01 | 所有派生候选最终回读 Canonical 并重做硬过滤 | 6.3 | 删除、越权、过期、历史与旧版本竞态 |
| P6-RANK-01 | 版本化确定性排序、冲突/去重、Layer/Token Budget | 6.3 | 重放、边界、缺失分值和 Tie-breaker 测试 |
| P6-USAGE-01 | Usage 四阶段、子集验证和幂等合并 | 6.4 | 伪造、重复、跨 Tenant 与隐私测试 |
| P6-PERF-01 | 结构化/FTS 延迟、容量保护和可解释降级 | 6.5 | 性能基线、故障注入与低敏 Trace |

## 工作包

### 6.1 FTS5 Projection

- 建立绑定 `(resource_type, resource_id, resource_revision)` 的 FTS 文档和 Projection State。
- 版本化 Tokenizer、规范化、停用词、语言和 Builder；支持影子表构建、校验和原子切换。
- Revision/Tombstone 提交后先逻辑失效旧项，再异步物理清理。

### 6.2 Recall Orchestrator

- 实现 RecallRequest、ExternalActor 解析、Purpose、Category/Resource Filter、`as_of`、Minimum Watermark 和 Deadline。
- 并行运行 Persona/Recent/Focus/State/Task/Claim/Relation/FTS Route，分配子 Deadline 和候选上限。
- 生成完整 RecallResponse：Watermark、Persona Revision/Hash、Completed/Degraded Routes、Partial、Cache Until 和 Next Wake。

### 6.3 Rehydrate、融合与预算

- 对每个派生 Candidate 回读 Canonical Revision 并验证所有硬过滤条件。
- 实现版本化分量、稳定 Tie-breaker、冲突/冗余处理、Layer Budget 和 Token Estimator。
- 缓存键包含 Scope/Actor/Purpose/Query、Source/Tombstone Watermark、Persona Revision 和 Schema Version。

### 6.4 Usage Report 与契约发布

- 实现 retrieved/returned/host_selected/model_visible 四阶段及子集校验。
- Usage Report 幂等合并，只影响 Accessibility/Activation，不影响 Confidence。
- 冻结 `/v1/recall`、Usage、Search、Capabilities/Negotiate、稳定错误码、Schema、Fixture 和双 SDK。

### 6.5 性能与降级

- 建立结构化 Recall p95 ≤ 50 ms、FTS Recall p95 ≤ 100 ms 的可复现实验基线。
- 覆盖 FTS 落后/损坏、Minimum Watermark 不可达、Route 超时和允许/禁止 Partial 的行为。
- Trace 只返回安全摘要，不暴露隐私过滤前候选。

## 数据、契约与回退策略

- FTS 文档、Projection State、Builder Manifest 与 Cache 仅保存 ResourceRef/Revision/Hash/Watermark 等可重建数据；Canonical 表不因搜索优化改变写入语义。
- Tokenizer、规范化、停用词、语言和 Builder 版本变化使用新影子表。只有数量、Checksum、抽样查询、Source/Tombstone Watermark 全部验证后才原子切换 Current Pointer。
- `/v1/recall`、Usage、Search、Capabilities/Negotiate、错误 Envelope、OpenAPI/JSON Schema、Fixture 和 Python/TypeScript SDK 同一 Release Train 发布；旧客户端不支持必需字段时通过协商失败，不静默更改语义。
- 排序器与 Token Estimator 显式版本化；兼容窗口内保留上一版本重放能力。缓存键完整包含 Scope/Actor/Purpose/Query、Source/Tombstone Watermark、Persona Revision 与 Schema Version。
- 回退时可关闭 FTS Capability 并保留结构化 Route；加载或切换失败继续使用上一已验证 FTS Generation，若其 Watermark/Builder 不可信则禁用该 Route，绝不跳过 Canonical Rehydrate。

## 量化验收基线

- 结构化 Recall p95 ≤ 50 ms、FTS Recall p95 ≤ 100 ms；报告必须声明硬件、数据规模、文本长度、并发、Candidate/Token 上限及冷/热索引状态。
- 相同 Request、Canonical Snapshot、排序器和 Token Estimator 版本重放 100 次，Candidate 顺序、分数、冲突标记和裁剪结果完全一致。
- 每个硬过滤维度（Tenant/Agent/SpaceGroup/Space/Session、Privacy、Status、Time、Tombstone、Revision、`as_of`）至少覆盖 200 个性质案例。
- FTS 落后、损坏、Route 超时、Minimum Watermark 不可达、允许/禁止 Partial 各场景至少重复 20 次；响应 Envelope 与稳定原因码一致。
- Usage 的 `model_visible ⊆ host_selected ⊆ returned` 性质至少运行 200 个生成案例；跨 Tenant/Request 伪造成功数必须为 0，重复 Report 100 次只产生一次逻辑累计。

## 退出门禁

- [x] FTS Builder 版本变化可影子重建并无中断切换，旧 Revision 不再被采用。
- [x] Recall 稳定排序、Token/Layer Budget 和相同输入可复现性测试通过。
- [x] 并发 Forget 与 FTS Search 的删除竞态不能越过最终 Rehydrate。
- [x] Route 超时、索引落后和 Minimum Watermark 场景返回准确 Partial/Degraded Envelope。
- [x] Usage 子集伪造、跨 Tenant Candidate 和重复 Report 被拒绝或幂等处理。
- [x] 在声明的硬件/数据集/并发条件下达到结构化与 FTS p95 目标。
- [x] Schema/SDK/Builder 兼容和 FTS 回退方案、需求追踪及交付证据已完成评审。

## 交付证据

- 代码/变更：基线 `b7bbad5` 之上的工作区（未提交，供复审）；核心模块 `domain/fts.py`、`domain/recall.py`（Ranker v2）、`storage/fts.py`、`indexing/fts.py`（影子重建/信任门）、`application/recall.py`（三段式编排器 + RecallService/UsageService/SearchService）、`jobs/handlers.py`（`fts.apply/rebuild/cleanup` + 变更事件排程）、`storage/backup.py`（FTS 重置与不变量）。
- 决策：[ADR-0014](../adr/0014-phase6-fts-recall.md)（FTS Generation/Builder、Recall Envelope 与路由并发/新鲜 Rehydrate 边界、排序/预算、缓存非目标、Usage 语义、复审补充）。
- 契约/SDK 版本：Contract 1.5.0（additive：`recall.v1`、`recall.usage.v1`、`search.fts.v1` capability；`/v1/recall`、`/v1/recall/{request_id}/usage`、`/v1/search` 路径；9 个 schema；错误码 `deadline_exceeded`、`identity_not_found`、`minimum_watermark_unavailable`）；Python/TypeScript SDK 0.7.0；fixtures 104 manifest cases。
- Schema/Migration：Schema 7（`0007_phase6_fts_recall.sql`，checksum `92e37560…57236ce4`，min_app 0.7.0；`recall_requests` 携带请求指纹与可重放响应， Forget 失效同事务擦除；0007 属工作区未发布版本，第三轮移除了第二轮引入的 `fts_generation_agents`——其口径被未结算 backlog 信任门取代）；0001–0006 与 `b7bbad5` 逐字节一致；窗口 [6, 7]。
- 性能/竞态测试报告：[phase-06-verification](../reports/phase-06-verification.md)（三轮复审后终测；2200 硬过滤性质案例；100 次重放一致；20×5 降级场景；删除竞态 0 复活；最终 `make ci` 数字以报告 §9 为准）。
- 发布前外部复审（第二轮，2026-09-02）：9 项发布阻断级缺陷（路由 deadline 无界等待、Claims as_of 候选丢失、FTS scope SQL 违反下行可见性、Purpose/ExternalActor 授权边界、影子重建截断与 checksum 未重算、FTS 落后租户口径、BM25/冗余排序方向、Usage 幂等重放未校验、退休代 ghost postings）全部修复并落 20 例回归（ADR-0014 §13、验证报告 §7b）。
- 发布前外部复审（第三轮，2026-09-02）：6 个未闭合边界 + 1 个新增问题（FTS 信任门非连续消费前沿——false fresh 与永久 false stale、speaker_entity_id 注入口未闭合、组-空间组合未验证真实绑定、单路由/单线程仍可无限阻塞、as_of Valid Time 按 now 判定、tombstoned Note 重入索引、Recall 请求级幂等无响应重放）全部修复并落 15 例回归（ADR-0014 §14、验证报告 §7c）。
- 回退策略：停用 `fts.*` handler 与 recall 流量 → 兼容二进制运行 Schema 7 → 必要时按 ADR-0013 §10 恢复流程回退备份（FTS 由 0.7.0 重建，restore 后强制 `pending_rebuild`）。
- 已知限制：见验证报告 §8（tokenizer 固定 unicode61、重建持有 Writer Gate、FTS 落后阈值、不支持 as_of FTS、无 Recall Cache、usage 记录-only、search 请求面较窄、可索引集 {claim, episode, note}）。

## 明确不做

- 不实现向量语义、Graph 多跳或 Provider 在线调用。
- 不让 Cache/FTS 命中绕过 Canonical Rehydrate。
- 不把普通记忆 Candidate 注入宿主 Persona/System Slot。

## 交接条件

Phase 7 可以扩展独立 Vector Route，而无需改变 Recall Envelope、硬过滤、预算、Usage 或降级语义。
