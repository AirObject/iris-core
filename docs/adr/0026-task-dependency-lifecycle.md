# ADR-0026：Task 依赖修订与 Schema 15 升级边界

状态：Accepted，2026-09-06。承接 [ADR-0025](0025-console-command-authorization.md) 和 Phase 14 的 Core/Console 闭环要求。

## 持久化与行为

依赖有 `active`、`removed` 两个状态。解除不物理删除：向不可变修订表追加 removed 修订，CAS 推进当前指针；同一 Task 内重新添加相同步骤对时复用依赖 ID，追加 active 修订，可以重新选择 condition。依赖图和就绪推导只使用 active 边；恢复必须再次检查环，不能凭历史身份绕过图校验。

Console 发布依赖创建和解除。创建要求父 expected_revision；解除另需 child_expected_revision。父 Task、两端步骤及实际改变就绪状态的后继都需当前写授权。沿用共享事务中的 Revision、Pointer、Watermark、Audit、Outbox 和幂等执行器；父修订只推进一次。终结的 Task 不再接受依赖编辑。响应为 Task/Dependency ID 和原提交修订回执，不保存旧正文。

既有在线创建复用同一持久化实现，保留 Required Lease。在线创建的幂等结果记录当时依赖修订；同一 ID 解除或恢复后，旧成功请求仍返回原 condition/revision，不改写成最新版本。旧缓存没有 revision 时对应原先唯一的修订 1。结果发布继续验证当前父资源和两端步骤的 Scope、Privacy 与 Tombstone。

## Schema 与版本

新增 `0015_task_dependency_lifecycle.sql`，只向 task_dependencies 与 task_dependency_revisions 增加受 CHECK 约束的 status，既有行默认 active，并增加活动图索引。0001–0014 保持字节不变。Core 版本推进到 0.13.0；运行时只打开 Schema 15，旧库先迁移，超出窗口在 Ready 前失败。

业务 `/v1` Contract 仍为 1.10.0，Python SDK 0.11.1、TS SDK 0.11.2 的公开方法与 HTTP 形状不变。Console 1.1.0 仍是未发布开发候选，增加两个 operation、三个请求/字段 Schema，并扩展动作和 Lookup 描述。公开机器快照单独审阅。TaskDependencyEdge 是 Core 内部领域类型，不扩大顶层导出。

## 升级、首次建库与回退

该迁移声明 `online_safe=false recovery=backup min_app=0.13.0`。旧版本不理解 removed 语义；已有数据库升级前必须停止 API/Worker，CLI 使用 `migrate DATABASE --allow-offline --with-backup DIR` 创建并验证真实备份。内部 Runner 的两个标志是可信操作者前置条件，不向 HTTP 暴露。失败不写入 0015 的成功记录。

新增可选迁移头 `bootstrap_safe=true`，仅允许明确标记的迁移在一次首次建库中执行：进入 Runner 时没有已登记迁移，也没有应用表。应用版本检查仍生效；未标记的离线迁移继续拒绝。已有表、已有迁移或中断后重试都不获得该例外，不能把已有库误当空库继续自动升级。中断建库需按已有数据库流程检查和备份后继续。

回退停止新版本进程，恢复升级前的 Schema 14 备份并使用原 Core 0.12.0 安装物。不能把旧二进制直接放到 Schema 15 上运行，也不提供 DROP COLUMN 降级。升级后新增数据与解除操作需在回退前另行保全、核对和重放；不能声称恢复旧快照没有数据损失。

备份保留 removed/current/history。恢复校验除原指针/FK 外，还核对当前依赖的 status、condition、Tenant/Task 和两端步骤与所指修订一致，拒绝状态与修订不一致的快照。普通解除不是 Forget，既有删除账本的恢复要求仍适用。

## 验证边界

集成测试覆盖父/子 CAS、终结父对象、隐藏两端步骤、解除后的就绪推导、反向边和恢复环、原在线幂等回执、真实备份升级、解除状态恢复和损坏快照。真实浏览器覆盖创建→解除→复用 ID 恢复与刷新。此切片不替代 Phase 14 的生产 SQLite、容器隔离、三轮版本回退、24 小时 Soak 或不可变 RC 验收。
