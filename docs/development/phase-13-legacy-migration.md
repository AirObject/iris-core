# 阶段 13：旧 Iris 数据迁移

> 状态：Planned  
> 前置阶段：[阶段 11](./phase-11-bellis-adapter.md)、[阶段 12](./phase-12-astrbot-bridge.md)  
> 目标版本：Migration Toolkit 0.1.0（目标 Core Schema/SDK/Adapter 版本在演练 Manifest 中锁定）  
> 架构依据：[§21 备份恢复与导出](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#21-备份恢复与导出)、[§22 派生投影](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#22-fts5faiss-与派生投影)、[§36 阶段 13](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#阶段-13旧-iris-数据迁移)、[§37 旧 Iris 数据迁移](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#37-旧-iris-数据迁移)

## 阶段目标

以只读、可审计、可重复的工具链把旧 Iris 数据映射到新 Canonical Domain，并通过 Dry Run、隔离候选、分批导入、索引重建、增量追平和回退演练完成安全切换。

## 架构约束

- 迁移工具只读源库；每个目标对象保留 Legacy Ref、原 ID、源哈希和转换版本。
- 导入使用稳定幂等键，Tombstone/删除记录优先于其他内容。
- L1/L2/L3 只作为迁移输入概念，不进入新公共协议。
- 无法证明主体、Scope、时间或来源的内容进入 Restricted Quarantine，不能自动成为 Active Claim。
- FTS/FAISS/Profile/Graph 不作为事实迁移，从新 Canonical 数据重建。

## 需求追踪

| 需求 ID | 基线要求 | 工作包 | 验证门禁 |
| --- | --- | --- | --- |
| P13-DISCOVERY-01 | 源库只读、Schema/编码/时区/删除与异常盘点 | 13.1 | 前后 Hash、权限、统计与风险报告 |
| P13-MAPPING-01 | 身份/空间/领域对象显式映射与 Quarantine | 13.2 | 映射 Fixture、冲突、未知值和人工审核测试 |
| P13-IDEMPOTENCY-01 | Dry Run/Import 稳定 Manifest、Legacy Ref 与幂等键 | 13.3 | 重跑、断点续传、批次和引用完整性测试 |
| P13-REBUILD-01 | 仅迁移 Canonical，投影按 Builder/Watermark 重建 | 13.4 | FTS/FAISS/Profile/Graph 一致性与 Tombstone 测试 |
| P13-CUTOVER-01 | 增量追平、切换、停止条件和可回退旧路径 | 13.5 | 双写/冻结、Cursor、备份和回退演练 |
| P13-AUDIT-01 | 每个结果可追溯源 ID、Hash、转换版本和决策 | 13.1–13.5 | Manifest 签名、审计查询和抽样复核 |

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

## 数据、契约与回退策略

- 扫描器以操作系统权限和数据库只读模式双重约束源库；运行前后记录源文件、Schema 与抽样页 Hash，任何变化立即中止并保留诊断。
- Mapping Spec、Transformation Version、Legacy Ref、Source Hash、Batch Range、幂等键和 Quarantine Reason 属于迁移契约；Manifest 锁定源快照、目标 Schema、Core/SDK/Adapter、Builder 和工具版本。
- Dry Run 不写目标库；正式导入按 Tombstone → 身份/空间 → Observation → 长期对象 → Persona Candidate 顺序执行短批次事务。失败批次保持明确断点，不把部分成功伪装为整批完成。
- 旧索引和 Profile/Graph 仅用于盘点/比对，不写入目标事实层；目标投影从通过 Tombstone/Scope/Privacy 校验的 Canonical Revision 重建并记录 Source/Tombstone Watermark。
- 切换前创建并隔离验证目标备份；回退保留旧服务和源库，但任一时刻只允许一个写路径。回退后停止新 Adapter、Fence 旧 Epoch、保存增量 Cursor，并阻止已导入 Tombstone 被旧数据覆盖。

## 量化验收基线

- 每次只读扫描前后的源文件、Schema 和抽样页 Hash 必须 100% 一致；同一源快照和 Mapping Spec 连续 Dry Run 3 次生成字节一致的 Manifest 与统计摘要。
- 同一 Migration Manifest 完整导入 2 次及在每个批次边界中断/续跑后，目标逻辑资源、Revision、Legacy Ref 和 Source Hash 集合一致，重复逻辑资源数必须为 0。
- 对 Tombstone、身份/空间映射、Observation 引用、Active Claim Evidence、Persona 隔离和 Quarantine 执行 100% 自动校验；非关键正文/时间映射至少分层抽样 `max(总量的 1%, 200)` 条并记录批准阈值。
- 从 Canonical 连续重建 Recent/FTS/FAISS/Profile/Graph 3 次，Builder/Tombstone Watermark、Resource Revision 和校验摘要一致；旧 Tombstone 目标的 Recall 命中数必须为 0。
- 在代表性全量数据上至少完成 3 次“增量追平 → 备份 → Adapter 切换 → Smoke → 回退/再切换”演练，记录 RPO、RTO、Cursor 差异和人工决策清单。

## 退出门禁

- [ ] 只读扫描前后源库/文件校验和一致。
- [ ] 同一 Manifest 重复 Dry Run/Import 不产生重复逻辑资源。
- [ ] 未知 Scope/主体/来源不自动进入 Active Claim 或 Persona Current。
- [ ] 代表性数据集的数量、哈希、引用、时间、身份和 Recall 抽样达到批准阈值。
- [ ] 旧 Tombstone 在导入、索引重建和备份恢复后仍优先。
- [ ] 双写/冻结、Cursor 追平、切换和回退全流程演练通过并留存报告。
- [ ] Mapping/Manifest/版本兼容和回退方案、需求追踪及交付证据已完成评审。

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
