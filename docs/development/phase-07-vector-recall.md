# 阶段 7：Vector Recall

> 状态：Planned  
> 前置阶段：[阶段 6](./phase-06-fts-recall.md)  
> 架构依据：[Embedding、FAISS Generation、Provider 边界与阶段 7](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md)

## 阶段目标

在不削弱 Canonical/FTS 可靠性的前提下增加可回退的向量语义召回。阶段结束时，模型或维度切换、Generation 损坏、并发重建、进程重启均不会让查询读取半成品或错误向量空间。

## 架构约束

- 公共 UUID 与 FAISS `int64` ID 严格分离，映射由服务端持久化且不可哈希碰撞替代。
- 单个不可变 FAISS Handle 只读；Search 不与 Add/Remove/Write 并发。
- 不同 Model/Dimension/Normalization/Template 不得混入同一 Generation。
- 新 Generation 完整构建、Flush、校验后才原子切换；失败继续使用上一已验证版本并标记降级。
- Vector Candidate 仍受最终 Canonical Rehydrate 和 Tombstone 优先级约束。

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

## 退出门禁

- [ ] UUID↔int64 映射唯一、可恢复、可校验，重建前后 Resource Revision 一致。
- [ ] 并发 Search/Rebuild/Swap 压测无半构建 Handle、Use-after-close 或混合 Generation。
- [ ] 损坏文件、Checksum 错误、模型/维度变化和加载失败均安全回退。
- [ ] 重启只加载完整已验证 Generation，临时或孤儿目录不影响 Ready。
- [ ] Vector 中残留的已删除/越权/旧 Revision Candidate 被 Rehydrate 剔除。
- [ ] Hybrid Recall 在目标条件下 p95 ≤ 250 ms，并报告 Route 延迟与降级。

## 交付证据

- 代码/变更：待补充
- Provider/Generation Manifest：待补充
- Schema/Migration：待补充
- 并发/性能/损坏恢复报告：待补充
- 已知限制：待补充

## 明确不做

- 不在当前只读 Handle 上原地增删向量。
- 不因向量相似自动建立 Identity、Relation 或合并 Claim。
- 不让 Embedding Provider 故障阻塞 Observation、Forget、Correct 或结构化 Recall。

## 交接条件

Phase 8 可以复用 Builder Version/Watermark、Generation 验证、Route 降级和最终 Rehydrate 模式来实现 Profile 与 Graph。
