# 阶段 7：Vector Recall

> 状态：Completed（含两轮对抗性复审修复）
> 负责人：Iris Memory Core Team
> 开始日期：2026-09-03
> 完成日期：2026-09-03
> 前置阶段：[阶段 6](./phase-06-fts-recall.md)
> 目标版本：0.8.0（已达成：Core/双 SDK 0.8.0、Schema 8、Contract 1.6.0）
> 架构依据：[§22.2 Embedding](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#222-embedding)、[§22.3 FAISS Generation](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#223-faiss-generation)、[§22.4 并发规则](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#224-并发规则)、[§24 Provider 边界](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#24-provider-边界)、[§30 性能与容量](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#30-性能与容量目标)、[§36 阶段 7](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#阶段-7vector-recall)

## 阶段目标

在不削弱 Canonical/FTS 可靠性的前提下增加可回退的向量语义召回。模型或维度切换、Generation 损坏、并发重建、进程重启均不会让查询读取半成品或错误向量空间。

## 架构约束

- 公共 UUID 与 FAISS `int64` ID 严格分离，映射由服务端持久化且不可哈希碰撞替代。
- 单个不可变 FAISS Handle 只读；Search 不与 Add/Remove/Write 并发。
- 不同 Model/Dimension/Normalization/Template 不得混入同一 Generation。
- 新 Generation 完整构建、Flush、校验后才原子切换；失败继续使用上一已验证版本并标记降级。
- Vector Candidate 仍受最终 Canonical Rehydrate 和 Tombstone 优先级约束。

## 需求追踪

| 需求 ID | 基线要求 | 工作包 | 验证门禁 |
| --- | --- | --- | --- |
| P7-EMBED-01 | Provider Port、启动 Probe、输入最小化与输出验证 | 7.1 | Mock Provider、维度/数值、超时和泄漏测试 |
| P7-IDMAP-01 | UUID 与 signed `int64` 映射唯一、持久、可恢复 | 7.2 | 唯一性、边界、快照和重建一致性测试 |
| P7-GEN-01 | 不可变 Generation、Manifest/Checksum 与原子切换 | 7.3 | 构建、损坏、孤儿目录、重启和回退测试 |
| P7-CONCURRENCY-01 | Search 与 Build/Swap 隔离，旧 Handle 安全释放 | 7.3 | 并发压测、Fencing 与引用生命周期测试 |
| P7-HYBRID-01 | Vector Route 可降级并沿用 Rehydrate/预算/Usage 契约 | 7.4 | 混合排序、删除竞态、Deadline 与性能测试 |

## 工作包

### 7.1 Embedding Port

- 实现 Provider Port、Model/Dimension/Normalization/Batch/Timeout/Rate Limit/Circuit Breaker 配置。
- 启动 Probe 验证模型维度、归一化、最大输入；拒绝空、NaN/Inf 或维度错误向量。
- 使用版本化模板生成最小必要输入，日志仅记录哈希、长度、模型和耗时。

### 7.2 Vector ID Map 与 Delta

- 建立服务端生成的 signed `int64` surrogate ID 与 Resource Revision 映射。
- 实现唯一性、删除/失效、Tombstone Watermark、Source Watermark 和重建快照。
- 增量更新进入 Delta Ledger 或触发新 Generation，不修改当前查询 Handle。

### 7.3 FAISS Generation 生命周期

- 在临时目录生成 Manifest、Index、ID Map Snapshot 和 Checksums。
- 校验数量、模型、维度、哈希与抽样搜索后原子重命名并更新 Current Pointer。
- 实现进程内 Copy-on-write Handle Swap、跨进程 Generation 协调、旧 Handle 引用释放和安全延迟清理。

### 7.4 Hybrid Recall

- 将 Vector Route 加入现有子 Deadline、Candidate 上限、Degraded Route 和最终 Rehydrate。
- 实现 FTS/Vector/Structured 的版本化融合、去重、稳定排序和 Route 权重。
- Vector 不可用时保留结构化与 FTS，响应明确报告所用回退。

## 数据、契约与回退策略

- Vector ID Map、Delta Ledger 和 Generation Pointer 通过增量 Migration 引入；公共 UUID 不暴露 FAISS ID，surrogate ID 由服务端分配并以唯一约束防止复用或碰撞。
- 每个 Generation 的 Manifest 固定 Model、Dimension、Metric、Normalization、Template/Builder Version、Source/Tombstone Watermark、数量和 Checksum；不同向量空间永不共用索引或 Delta。
- Embedding/Vector Capability 先通过 Negotiate 和 Fixture 作为可选能力发布；Vector 缺失、熔断或不兼容时继续返回 Phase 6 结构化/FTS 结果，并在 `degraded_routes` 指明原因和回退。
- 模型切换采用双 Generation：构建、Flush、完整性/抽样验证后原子更新 Pointer，进程内 Copy-on-write Swap；旧 Generation 在无在途引用且超过回退窗口后才按受控策略清理。
- 回退到上一已验证 Generation 时必须重新验证其模型配置、Watermark 与 Tombstone；没有可信 Generation 时禁用 Vector Route，不尝试修补当前只读 Handle，也不阻塞 Observe/Correct/Forget。

## 量化验收基线

- Hybrid Recall 在声明硬件、语料规模、维度、并发、Candidate/Token 上限及冷/热 Handle 条件下 p95 ≤ 250 ms；分别报告 Embed、FAISS Search、Rehydrate 和融合耗时。
- UUID↔`int64` 映射边界、唯一性、删除/失效和快照恢复性质每项至少运行 200 个固定种子案例，映射碰撞或错误复用数必须为 0。
- 50 个并发 Search 与至少 10 轮 Build/Validate/Swap 交错运行，不得读取临时 Generation、混合模型空间、出现 Use-after-close 或返回未 Rehydrate Candidate。
- 对 Manifest 缺失、Checksum 错误、截断文件、数量不符、错误维度、NaN/Inf、加载异常和孤儿临时目录分别至少执行 20 次；当前可信 Route 不受损或显式降级。
- 模型/维度/Normalization/Template 切换前后各抽样至少 1,000 个已知 Resource Revision，ID Map、Content Hash、检索空间与 Canonical Rehydrate 结果全部匹配。

## 退出门禁

- [x] UUID↔int64 映射唯一、可恢复、可校验，重建前后 Resource Revision 一致。
- [x] 并发 Search/Rebuild/Swap 压测无半构建 Handle、Use-after-close 或混合 Generation。
- [x] 损坏文件、Checksum 错误、模型/维度变化和加载失败均安全回退。
- [x] 重启只加载完整已验证 Generation，临时或孤儿目录不影响 Ready。
- [x] Vector 中残留的已删除/越权/旧 Revision Candidate 被 Rehydrate 剔除。
- [x] Hybrid Recall 在目标条件下 p95 ≤ 250 ms，并报告 Route 延迟与降级。
- [x] Schema/Capability/Generation 兼容和回退方案、需求追踪及交付证据已完成评审。

## 交付证据

- 代码/变更：基线 `179b6a0` 之上的工作区（未提交，供复审）；核心模块 `domain/vector.py`（模板/空间身份/surrogate 规则/校验/原因码）、`providers/embedding.py`（HttpEmbeddingProvider + Deterministic 测试 Provider + 校验包装/熔断/限流/Probe）、`indexing/faiss.py`（真实 FAISS `IndexIDMap2(IndexFlatIP)` adapter，惰性探测）、`indexing/vector.py`（六阶段 Generation 流水线、COW Handle/Manager、信任门搜索、cleanup/sweep）、`storage/vector.py`（投影仓储）、`application/recall.py`（VectorRoute + Hybrid Ranker v3 接入）、`jobs/handlers.py` + `jobs/worker.py`（vector.apply/rebuild/cleanup）、`storage/backup.py`（restore 重置 + 不变量）、`application/health.py`、`observability/metrics.py`。
- 决策：[ADR-0015](../adr/0015-phase7-vector-recall.md)。
- 契约/SDK 版本：Contract 1.6.0（additive：capability `recall.vector.v1`、`embedding.v1`；路由枚举 +`vector`；降级原因码 +7；错误码 +`provider_unavailable`）；Python/TypeScript SDK 0.8.0；fixtures 107 manifest cases。
- Schema/Migration：Schema 8（`0008_phase7_vector_recall.sql`，online_safe、min_app=0.8.0）；0001–0007 与 `179b6a0` 逐字节一致（`test_published_bytes_match_head_baseline` 锁定）；窗口 [7, 8]。
- 性能/竞态测试报告：[phase-07-verification](../reports/phase-07-verification.md)。
- 对抗性复审第一轮（2026-09-03）：vector 搜索路径缺 Tombstone 最后防线（并发测试实际捕获并修复）、faiss SWIG 双引用 GC 陷阱、snapshot 字典序 vs 数值序、Manager 对已 seal Handle 的分发、跨租户清扫、阶段 4 饥饿分配、ownerless backlog——全部修复并落回归。
- 对抗性复审第二轮（2026-09-03，3×P1 + 5×P2）：(P1) 未发布构建经 id map 刷新伪造新鲜 ⇒ 引入 `incorporated_generation` 成员戳（与指针 CAS 同事务盖章，apply 跳过需 revision 相等 ∧ 戳 == 当前指针代）；(P1) 验证只信可重写的 checksums.txt ⇒ manifest 为文件摘要权威 + 加载路径绑定 SQLite 权威行（checksum/数量/空间精确相等、水位单调）+ 畸形输入折叠为稳定 `vector_index_corrupt`；(P1) RankerV3 未真正去重 ⇒ 按 Canonical 资源在预算前去重（v2 标记保留，冲突组不合并）；(P2) Provider deadline 真下传（socket 级 min(配置，剩余)，半开单探针）、`capability_available`/readiness 接入 Provider Probe（`vector_required` 部署 Provider 宕机 ⇒ not_ready）、目录删除移出 fenced 事务（`after_commit` 钩子，回滚不丢目录）、overfetch 迭代扩张（跨 Agent 挤占不再产生假阴性）、四个 Phase 7 指标接上真实发射点——全部修复并以复审复现场景落回归。
- 回退策略：停用 `vector.*` handler 与 Vector 路由 → 兼容二进制运行 Schema 8 → 必要时按 ADR-0013 §10 恢复流程回退备份（Vector 由 0.8.0 重建，restore 后强制 `pending_rebuild`）。
- 已知限制：见验证报告（Deterministic Provider 无语义质量、FAISS 未含于备份、重建逐 Agent 水印保守 abort 语义、`/v1/search` 未加向量面等）。

## 明确不做

- 不在当前只读 Handle 上原地增删向量。
- 不因向量相似自动建立 Identity、Relation 或合并 Claim。
- 不让 Embedding Provider 故障阻塞 Observation、Forget、Correct 或结构化 Recall。

## 交接条件

Phase 8 可以复用 Builder Version/Watermark、Generation 验证、Route 降级和最终 Rehydrate 模式来实现 Profile 与 Graph。
