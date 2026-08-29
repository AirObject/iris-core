# 阶段 13：旧 Iris 数据迁移

> 状态：Planned  
> 前置阶段：[阶段 11](./phase-11-bellis-adapter.md)、[阶段 12](./phase-12-astrbot-bridge.md)  
> 架构依据：[旧 Iris 数据迁移与阶段 13](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md)

## 阶段目标

以只读、可审计、可重复的工具链把旧 Iris 数据映射到新 Canonical Domain，并通过 Dry Run、隔离候选、分批导入、索引重建、增量追平和回退演练完成安全切换。

## 架构约束

- 迁移工具只读源库；每个目标对象保留 Legacy Ref、原 ID、源哈希和转换版本。
- 导入使用稳定幂等键，Tombstone/删除记录优先于其他内容。
- L1/L2/L3 只作为迁移输入概念，不进入新公共协议。
- 无法证明主体、Scope、时间或来源的内容进入 Restricted Quarantine，不能自动成为 Active Claim。
- FTS/FAISS/Profile/Graph 不作为事实迁移，从新 Canonical 数据重建。

## 工作包

### 13.1 发现与只读扫描

- 盘点旧 Schema、版本、编码、时区、ID、Persona/用户 Profile 隔离、删除记录和源数据可变性。
- 输出数量、空值、重复、孤儿引用、未知 Scope、时间精度和敏感数据报告。
- 对扫描器设置只读连接/文件权限并证明不修改源哈希。

### 13.2 映射与隔离策略

- 显式配置旧 Agent/Persona、平台账号、Space/Community 到新 Tenant/Agent/ExternalIdentity/Space 的映射。
- 将原始消息→Observation、L2→Episode/Claim+Evidence、L3→Entity/Relation Candidate、Note/Task/Persona 分别转换。
- 缺失 Scope、未知主体、低来源权威、Persona 演进和冲突事实进入对应 Quarantine/Proposal/Disputed 流程。

### 13.3 Dry Run 与分批导入

- Dry Run 生成目标数量、哈希、引用图、冲突、Quarantine、预计存储和批次 Manifest，不写目标库。
- 按 Tombstone → 身份/空间 → Observation → 长期对象 → Persona Candidate 的顺序幂等导入。
- 每批记录 Source Range、转换版本、成功/失败/跳过数量、Watermark 和可逆目标引用。

### 13.4 重建与校验

- 从 Canonical 重建 Recent/FTS/FAISS/Profile/Graph，校验 Builder Watermark 和 Tombstone Watermark。
- 对数量、哈希、引用、双时态、主体、Scope、Recall、Persona 隔离和删除做自动/抽样校验。
- 失败批次不模糊续跑位置，支持修正映射后稳定重跑。

### 13.5 增量追平、切换与回退

- 编写双写或冻结策略、增量 Cursor 追平、Adapter 切换、只读旧服务保留和停止条件。
- 切换前创建一致性备份，记录 Migration Manifest、Schema/Adapter 版本和验证签名。
- 演练在切换失败时恢复旧只读/写路径，并防止双端重复写入或旧删除复活。

## 退出门禁

- [ ] 只读扫描前后源库/文件校验和一致。
- [ ] 同一 Manifest 重复 Dry Run/Import 不产生重复逻辑资源。
- [ ] 未知 Scope/主体/来源不自动进入 Active Claim 或 Persona Current。
- [ ] 代表性数据集的数量、哈希、引用、时间、身份和 Recall 抽样达到批准阈值。
- [ ] 旧 Tombstone 在导入、索引重建和备份恢复后仍优先。
- [ ] 双写/冻结、Cursor 追平、切换和回退全流程演练通过并留存报告。

## 交付证据

- 源数据盘点/风险报告：待补充
- 映射规范/转换版本：待补充
- Dry Run/Migration Manifest：待补充
- 全量演练/回退报告：待补充
- Quarantine 与人工决策清单：待补充

## 明确不做

- 不修改或“清洗”源数据库。
- 不迁移旧索引作为事实，不伪造缺失的精确时间、身份或 Evidence。
- 不因数据量大而跳过 Tombstone、Scope、Privacy 或 Persona 审核。

## 交接条件

Phase 14 可以在已迁移代表性数据规模上执行性能、Soak、备份恢复、升级和回滚验证，并把 Migration Manifest 纳入发布归档。
