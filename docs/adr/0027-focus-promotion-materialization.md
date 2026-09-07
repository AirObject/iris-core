# ADR-0027：Focus 提升的真实目标与原始证据

状态：Accepted，2026-09-06。补齐 Phase 3 的目标占位，承接 Phase 14 的 Core 遗留闭环；管理事务遵循 [ADR-0025](0025-console-command-authorization.md)。

## 决定

显式 promote/promoted 在同一写事务创建目标对象，然后追加 Focus promoted 修订。先检查 Focus 状态与 expected_revision；目标创建、链接、目标修订/Watermark/Audit/Outbox，以及 Focus 修订/CAS/Watermark/Audit 一起提交或回滚。终结 Focus 不重复提升。请求重放返回原修订与目标，不重新创建；当前来源和目标的授权及删除状态高于历史成功。

内部 PromotionSource 是明确携带资源类型、Scope、源修订和内容的不可变快照。目标由对应 Note/Task/Episode/Claim 服务写入；Note 既有提升入口复用同一实现并保留原语义。不会把 Focus 伪装成 Note，不构造租户 admin 或在线凭据，也不临时使用无 Surface 服务绕过 Lease。在线请求仍先通过既有 Required Surface 检查；管理分支只接受真实 CommandActor 与当前运营 Grant。

| 目标 | 新建对象与内容 | 证据约束 |
| --- | --- | --- |
| Note | inbox；question 映射 question，其余为 important；标题取摘要前 500 字，正文保留完整摘要；默认七日复查 | 保留 Focus 与原始来源引用，不自动承诺或完成事项 |
| Task | proposed 待确认计划；标题前 500 字，goal 保留摘要；不自动激活或添加触发器 | 保留 Focus 与原始来源引用 |
| Episode | open；摘要完整保留，起始时刻为本次显式提升时刻 | 必须有至少一条当前有效、已提交的原始 Observation，不以 Focus 摘要替代原始观察 |
| Claim | self subject、promoted_from_focus 谓词、完整摘要与 Focus ID/kind；authority=agent_inference | 必须有当前有效的 Observation/Artifact/Episode/Claim/Note 原始 Evidence；Focus 本身不是 Evidence，不自动声称 platform_verified/admin_confirmed |

来源检查包括当前与固定修订的 Scope、Privacy、存在性、Tombstone 和传递来源；循环或超出深度 16/对象 200 的来源闭包失败关闭。Evidence 另复用所属领域的 effect、生命周期、修订与 Scope 校验。新目标取 Focus 与来源隐私标签的并集，防止提升放宽披露范围。Restricted 管理来源须当前 Grant 明确允许；领域分支只在该检查之后使用管理隐私规则，不提升 AccessContext.admin。

管理命令的来源引用参与幂等预检查与写事务重授权，实际目标参与结果重授权。读取 Focus 时，目标不可见则隐藏目标类型/ID；其既有状态与摘要仍遵循 Focus 自身读取规则。成功命令重放若来源或目标已不可见，返回拒绝，而非伪造未提升结果。

## 契约与旧数据

既有业务 Focus transition 的签名与四种目标类型不变；返回的 promotion_target_id 现在对应真实对象。Console 1.1.0 开发候选扩展现有状态转换 Schema，promoted 必须给出目标，其他状态不得夹带目标。前端从正式描述获取动作，不新增业务 SDK 方法或管理凭据通道。

使用 Schema 15 既有表，无新增迁移。旧 Phase 3 占位记录可能已是 promoted 且目标 ID 为 null；继续兼容读取，不伪造已创建对象，也不在迁移或读取时自动物化旧请求。它们不属于本次新命令成功的验收证据。范围更广的历史数据修复须独立定义显式操作与审计语义。

本决定不代替完整 Forget/保留、生产 Provider、容量、故障或发布门禁；当前实施证据记录在 [Phase 14 验证报告](../reports/phase-14-verification.md)。
