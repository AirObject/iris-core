# ADR-0042：固定筛选集合与可停止的批量删除 Operation

状态：Accepted，2026-09-07。继续 [ADR-0037](0037-console-durable-forget-previews.md) 与 [ADR-0041](0041-console-task-forget-cascade.md)。本决定扩大 Console 预览根集合，实施和完整组合验收分别记账，不代表 Phase 14 已完成。

## 固定集合与接受

预览严格二选一：显式目标及 Revision 数组，或 collection、filters、sort 选择器。集合为 1–500 个可见根资源；空集合与超过 500 项均拒绝，不截断。筛选使用既有授权读面，在幂等预览事务中只解析一次；保存固定 ID、原输入序号与当前版本。提交和 Worker 不重新运行筛选，新增匹配内容不加入。扫描受现有时间、SQLite 步数与扫描行数预算约束；扫描结束恢复连接原先的 query_only 设置，普通只读事务始终不能写入。原显式预览请求的幂等指纹保持兼容。

至多 50 个根仍同步提交。51–500 项通过验证后，在一个事务中消费预览、保存 Operation 并创建首个 Outbox 作业；返回 202 的 ConsoleOperationEnvelope。接受时没有根资源删除。重复请求重新授权后返回同一 Operation 的当前元数据；原 200 同步回执保持不变。OpenAPI 显式声明两种成功响应。

## 实际授权、分批与失败

Operation 保存实际创建者的密钥 ID、Revision、Grant 指纹、原 Session ID 与 epoch。每批检查这些实际持久凭据、当前权限、Session 过期/撤销与 erase 最近重新认证时限；不构造伪 Session、管理员身份或绕过验证的后台授权。Worker 仅持有真实 UoW、时钟、ID 与幂等依赖。元数据和取消仅向相同当前密钥 Revision 与 Grant 指纹开放，SQL 在 LIMIT 前应用归属条件。

先对整个集合按引用顺序排列，再切成每批至多 50 个根。原输入序号与预览请求键跨批保留，Task 级联成员继续受既有总量和预算约束。每批在任何 Canonical 写入前复核权限、全部根的版本/保护状态、Hold 与删除水位。预检查失败可记录 blocked 与稳定问题码；开始修改后任何异常必须传播给既有 Outbox 事务，不能捕获后提交部分删除。

根及子对象变更、删除账本、进度 CAS、下一代非合并 safety 作业和本次作业的 Lease fence 同事务提交。自己的已提交批次更新预期删除水位，外部删除变化阻止后续批次。清理仅在 fence 提交后执行，持久清理作业继续承担重试。当前作业进入死信时，同一失败事务仅更新匹配作业及 Revision 的 Operation 为 failed；GET 不修复状态或执行删除。错误响应不包含异常正文。

取消只停止后续批次。零进度为 cancelled，已提交部分为 cancelled_partial，已提交 Tombstone 和擦除不能恢复。终态重试幂等；本切片不发布 resume 动作。元数据响应不返回固定目标正文、内部快照、Session、Grant 或队列 ID。问题页仅返回原输入序号（整体问题为 null）、稳定代码和时间。列表支持类型、状态、创建时间与签名分页；Cursor 绑定调用者、路径、筛选及有效期。

## Schema 20 与隔离恢复

Schema 20 新增 console_operations、console_operation_problems、归属/状态索引及有界 payload、进度约束和 Revision CAS。旧 Migration 不改写。Core 0.13.0 开发候选的运行窗口为 Schema 20；空库、Schema 19 在线升级、旧库恢复后升级与独立安装须验证。新增管理 HTTP 和 DTO 不增加业务 SDK 方法。

在最新删除账本重放之后、离线恢复目录切换之前，核对非终态 Operation 的原应用归属、选择器、原预览幂等键、删除模式及根 Tombstone。Entity 使用既有独立选择器命名。确认进度必须为连续已提交前缀，不能低于备份记载进度。全部已提交则记 completed；否则标为 blocked / restore_requires_review 并清除待处理私有快照，旧作业成为无修改的结束操作。再次恢复保持阻塞。无效快照或无法匹配的进度中止切换，保留旧目标目录。

该保护不等于 Phase 14 的完整恢复凭据重置；现有恢复仍须在后续工作中补齐该边界。恢复不补删尚未提交的根、不撤销删除事实、不伪造 Task 完成状态。

## 验证与剩余范围

验证 51/500 根、跨批引用顺序、每批新 Worker 身份、Hold/权限变化、晚期擦除异常和失租回滚、取消与重试、最新账本进度核对、重复恢复、异常恢复保持旧目录、固定筛选和授权范围、签名分页及脱敏响应。真实浏览器须完成接受 51 项、独立 Worker 提交 50 项、页面取消和最后一项存活验证；还须经过当前完整 CI、公共接口清单与独立安装。

其他 Operation 类型、Persona、导入导出、Provider/Settings、生产隔离、安全、Soak、恢复凭据重置和稳定发布验收仍保留在 [Phase 14](../development/phase-14-hardening-release.md)。实测记录于 [验证报告](../reports/phase-14-verification.md)。
