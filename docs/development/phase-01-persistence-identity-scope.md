# 阶段 1：持久化内核、身份与空间

> 状态：Completed  
> 前置阶段：[阶段 0](./phase-00-architecture-scaffold.md)  
> 目标版本：0.2.0  
> 开始日期：2026-08-29  
> 完成日期：2026-08-29  
> 架构依据：[§5 租户与空间](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#5-租户agent-与空间模型)、[§6 身份](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#6-身份与实体模型)、[§20 SQLite](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#20-sqlite-canonical-store)、[§21 备份恢复](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#21-备份恢复与导出)、[§36 阶段 1](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#阶段-1持久化内核身份与空间)

## 阶段目标

建立 SQLite Canonical Store 和所有后续领域共享的身份、空间、授权与版本语义。阶段结束时，系统能够安全创建并读取 Tenant/Agent/Space/Entity，支持历史 Binding、幂等写入、乐观并发、审计、Tombstone 和最小可恢复备份。

## 架构约束

- SQLite 规范化领域记录与不可变 Revision 是唯一事实源；当前表只保存指针和索引字段。
- Scope 的 `null` 是“该维向下可见”而不是请求通配符，权限由服务端 `AccessContext` 推导。
- 外部账号唯一键是 `(tenant_id, provider, realm, external_id)`，昵称不参与身份合并。
- Binding/Redirect 变化不回写历史 Observation，并必须同时保留发生时与当前解析视图的能力。
- Tombstone、Idempotency、Audit 和 Agent Watermark 从本阶段起就是事务基础设施，不得后补成旁路。

## 需求追踪

| 需求 ID | 基线要求 | 工作包 | 验证门禁 |
| --- | --- | --- | --- |
| P1-STORE-01 | SQLite Runtime、短事务、UoW 与兼容迁移 | 1.1 | Runtime、Busy、Migration 集成测试 |
| P1-SCOPE-01 | Tenant/Agent/SpaceGroup/Space、Scope Null 与 Privacy | 1.2 | 权限矩阵与性质测试 |
| P1-IDENTITY-01 | Entity、ExternalIdentity、Binding、Redirect 与双身份视图 | 1.3 | 冲突、撤销、环和历史测试 |
| P1-CONSISTENCY-01 | Revision、Watermark、Idempotency、Audit、Tombstone | 1.4 | 并发、重放和横向越权测试 |
| P1-RECOVERY-01 | 一致性备份、隔离恢复与校验 | 1.5 | Backup → Restore → Smoke 报告 |

## 工作包

### 1.1 SQLite Runtime 与事务层

- 实现 Runtime Allowlist、WAL、`synchronous=FULL`、Foreign Key、Trusted Schema 和 Busy Timeout 检查。
- 建立短写事务、Writer Gate、Busy Retry、Repository、Unit of Work 和可注入 Clock/ID Generator。
- 实现 Schema 兼容范围、Migration Run 记录及非 Online-safe Migration 拒绝机制。

### 1.2 Tenant、Agent 与空间

- 实现 Tenant、Agent、SpaceGroup、Space、Session 及 SpaceGroup Binding History。
- 实现 Scope 构造/匹配和服务端 AccessContext；Body 只能收窄授权。
- Agent 创建事务写入最小锁定 Published Persona 与 Current Pointer，只满足 Ready/Recall 契约；策略、状态和演进留到 Phase 9。

### 1.3 Identity Registry

- 实现 Entity、ExternalIdentity、版本化属性、Binding 状态机、Redirect 链和环/深度防护。
- 支持管理员确认的 Binding；为双端挑战码预留契约但不提前实现完整产品流程。
- 实现字段级权威、冲突并存、`identity_view=at_ingest|current` 所需读取接口。

### 1.4 通用一致性设施

- 实现不可变 Revision、Current Pointer、Expected Revision 和 Agent Watermark。
- 实现 Idempotency Record 的 Fingerprint/回放/冲突/崩溃恢复语义。
- 实现 Audit Event、ResourceRef/Link 和 Tombstone 最小内核；高风险管理操作要求原因码。

### 1.5 备份与验证

- 使用 SQLite Online Backup API 创建最小一致性备份，生成 Manifest 和 Checksums。
- 在隔离目录完成 Integrity、Foreign Key、Schema、Current Pointer 和 Tombstone 校验后恢复。
- 记录 RPO/RTO 测量入口；索引和 Artifact 的完整备份留待对应阶段扩展。

## 数据、契约与回退策略

- 使用 Expand/Validate/Cutover 迁移：先新增表、列和索引，再回填与校验，最后切换 Current Pointer；禁止同一版本中执行不可恢复的字段删除。
- 每个 Migration 声明 `online_safe`、预计锁时长、兼容的最小/最大应用版本和恢复前置条件；非 Online-safe 迁移要求停机与备份。
- API/Schema 先以可选字段扩展，至少保留一个发布窗口的旧读取形态；错误码只新增，不改变既有含义。
- 回退优先使用兼容旧二进制；若旧二进制不兼容新 Schema，则从切换前备份隔离恢复并完成 Pointer、Tombstone 和权限 Smoke 验证。

## 量化验收基线

- Scope、Privacy、Binding、Redirect、Revision 和幂等性质测试每项至少运行 200 个生成案例。
- 同一 Expected Revision 的 50 个并发写入必须恰好一个成功，其余均返回 `revision_mismatch`。
- 横向越权矩阵覆盖 Tenant、Agent、SpaceGroup、Space、Entity 的读写两种方向，允许的 Body Scope 只能收窄。
- Backup → 隔离 Restore → Smoke Read/Write 连续执行 3 次；RPO 必须为最后一个已提交事务，RTO 在声明数据规模下记录实测值。

## 退出门禁

- [x] Scope Null、SpaceGroup/Space、Privacy 交集和 Body 提权性质测试通过（每条性质 250 个固定种子生成案例）。
- [x] 并发相同 Expected Revision 只有一个写入成功，失败返回稳定 `revision_mismatch`（50 线程：1 成功 / 49 mismatch）。
- [x] Binding 冲突、撤销、Redirect 环及发生时/当前身份历史测试通过。
- [x] 同幂等键同 Payload 回放原结果，不同 Payload 返回 `idempotency_key_reused`（含崩溃恢复两分支）。
- [x] 跨 Tenant/Agent/Space/Entity 横向越权测试通过，审计与错误无敏感正文。
- [x] Online Backup → 隔离恢复 → Smoke Read/Write 验证通过（连续 3 轮 + 篡改拒绝 + 不变量拒绝）。
- [x] Migration/兼容/回退方案、需求追踪和交付证据已完成评审。

## 交付证据

- 代码/变更：Phase 1 实现提交 `aecedd379669ae6a21319fd0fc80551dfd75c2d6`；[Runtime/UoW](../../src/iris_memory_core/storage/uow.py)、[Repositories](../../src/iris_memory_core/storage/repositories.py)、[Backup](../../src/iris_memory_core/storage/backup.py)、[Provisioning](../../src/iris_memory_core/application/provisioning.py)、[Identity](../../src/iris_memory_core/application/identity.py)
- ADR：无新增冻结边界决策；沿用 [ADR-0002](../adr/0002-scope-null-semantics.md)、[ADR-0003](../adr/0003-identity-and-binding.md)、[ADR-0004](../adr/0004-immutable-revisions.md)、[ADR-0005](../adr/0005-tombstone-priority.md)、[ADR-0007](../adr/0007-repository-boundaries.md)、[ADR-0008](../adr/0008-persona-bootstrap-seam.md)
- Schema/Migration：[`0002_phase1_kernel.sql`](../../migrations/0002_phase1_kernel.sql)（SHA-256 `7fcd0883…2267c`，online_safe=true）；Schema Version 2；包版本 0.2.0；稳定错误码仅新增；0001 保持已发布字节不变，Phase 0→1 升级由测试覆盖
- 测试报告：[Phase 1 Verification Report](../reports/phase-01-verification.md)（完整
  `make ci` exit 0；139 个 Python 测试和 TypeScript SDK 测试通过，覆盖率 89.66%；含
  四轮针对性评审修复与回归测试）
- 已知限制：本地开发 SQLite 3.50.4 不在官方 Allowlist（集成测试显式 Pin，生产 Ready 仍用官方清单）；挑战码仅契约位；Redirect 撤销未建模；备份 artifacts/faiss 清单留待对应阶段；详见验证报告。

## 本轮评审固化的语义

- **字段权威授权**：断言 `platform_verified`/`admin_confirmed`/`explicit_correction`
  需要对应 capability（`platform_ingest`/`manage`/`identity.correct`）**或**管理平面
  （`admin=True`）。`admin` 由服务端从认证凭据派生，请求体不可自授，代表平台运营方
  全权——这是设计上的第二授权通道，不是越权漏洞。
- **备份完整性 vs 真实性**：目录内 checksums 证明意外损坏；对抗性篡改（改库并重算
  checksums）由操作员保管的外部密钥签发的 `authenticity.tag`（HMAC）拒绝。CLI 以
  `--backup-key-file` 提供密钥；`--with-backup` 仅在备份**验证通过**后才满足
  `recovery=backup` 前置条件。
- **恢复切换**：journal 化两段 rename + 文件/目录 fsync；中断后由
  `iris-memory-core recover-switch`（或 `recover_pending_switch()`）确定性完成或回滚，
  服务路径不会停留在"目标目录缺失"状态。
- **版本列车**：核心、Python SDK、TypeScript SDK 同步发布 0.2.0；如未来需要 SDK 独立
  版本策略，以 ADR 记录后再拆分。
- **水印推进**：每个写事务每 (tenant, agent) 恰好推进一次 `current_seq`，条目记录该事务
  内聚合的最终 revision（如创建+绑定 Space 记 `space/rev2`）。
- **Ready 门**：应用 `Store` 打开的每个连接都校验 Schema 兼容窗口，越界/未迁移库在
  事务开始前抛稳定 `schema_incompatible`——包括被恢复切换换入的数据库。
- **恢复信任边界**：备份目录使用精确文件白名单，未知文件、SQLite sidecar、目录和
  symlink 一律拒绝；恢复只复制白名单文件到 `0700` staging，并在不变量检查后再次完整
  校验将要切换的字节（无 verify→copy 窗口）；journal 只信任本模块命名模式；整段操作
  以 flock 串行化；journal 原子发布且仅在 staging/aside 清理成功后删除。
- **备份发布**：完整备份集在临时目录构建（逐文件 fsync）后单次 rename 原子发布，
  catalog 不可能指向半写状态。
- **幂等快照**：带格式版本信封，未知版本显式拒绝；新记录字段必须带默认值
  （`record_restore` 的前向兼容契约）；业务异常立即释放租约，失败可重试，仅已提交
  结果被回放。
- **lock_ms**：`busy_timeout` 约束锁获取；观测事务窗口（等待+执行+提交）超预算时，
  已提交迁移保持成功并通过 run 记录、Runner 与 CLI 发出告警。

## 明确不做

- 不实现 Observation、检索索引、完整 Persona 演进或宿主租约协调。
- 不按昵称、文本相似或模型判断自动建立 Verified Binding。
- 不支持多主 SQLite 或网络文件系统承载运行数据库。

## 交接条件

Phase 2 可以依赖稳定的事务/UoW、Revision、Watermark、Idempotency、Audit、AccessContext、身份/空间读取和备份恢复骨架。
