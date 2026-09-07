# ADR-0043：Console CognitiveEvent dismiss

状态：Accepted，2026-09-07。实现与组合验收分别记账。本决定继续 [ADR-0025](0025-console-command-authorization.md) 的管理 CommandActor 边界，不代表 Phase 14 已完成。

## 命令和状态

Console 明确提供 POST `/memory/cognitive-events/{id}:dismiss`，只接受 expected_revision 和固定 reason_code，要求当前 memory.write、memory.read、console.manage、浏览器 Session/CSRF 与幂等键。实际 CommandActor 经现有事务执行器传递，不伪装宿主、借用 Lease 或注入管理员 AccessContext；Required 模式下仍执行完整 Scope、Privacy、父对象授权和版本检查。

事件只从 pending/delivered 转为 cancelled。与 pull/ACK/过期处理共享既有事件状态机和 Revision CAS；旧版本冲突拒绝，当前终态不能再次转换。插入历史后 CAS 失败必须抛出异常，让整个事务回滚。重复同一幂等请求返回原最小回执，但在读取缓存后再次核对当前凭据和可见性。

保留投递次数、最后投递时间、历史和已存在的业务事实；清除过时投递 Lease。dismiss 不写 ACK、不完成 Task/Step、不创建外部效果证据，不编辑事件正文，也不产生 Tombstone 或删除账本。关联 Task/Step 完全不变。审计使用真实 Console 密钥归属；修订、水位与 cognitive_event.changed Outbox 事件在同一事务提交。取消后不再进入待投递 Recall ID 集合，宿主不能再 ACK 该事件。 Recall 缓存重放、首次发布及批量重验也按原有界 ID 集合核对事件当前 Scope、状态、过期和 Tombstone；失效则整体拒绝，不改写原 Usage 记录，也不重新选择新增事件。

## 契约与页面

新增严格请求 DTO 和仅包含 resource_type/resource_id/revision/canonical_status 的回执及 Envelope；不发布业务 SDK 的管理动作。事件读取补齐投递次数，继续排除 ACK token 与 Lease 细节。注册表不提供 create/update，仅有取消投递动作；详情页结合当前权限、版本、父对象和非终态判断可用性。浏览器通用表单将 dismiss 作为无 fields 的状态命令，匹配严格后端契约；终态仍展示历史，操作按钮禁用。

## 持久化与恢复

复用既有 CognitiveEvent current/revisions，不新增 Migration。取消属于正常业务状态，随已提交备份恢复；已包含取消的备份应保留 cancelled 及历史、Task 原状和无 ACK。旧备份是否包含取消遵循其快照和 RPO，不能把取消当作永久删除事实向所有旧备份补写。本决定不补齐 Phase14 全部恢复凭据重置和灾备门禁。

## 验证

应用与 HTTP 验证 pending/delivered/acknowledged/expired/cancelled、竞争失败回滚、幂等和缓存后撤权、父对象隐藏、严格字段拒绝、只读账号、无后续 pull/ACK、Recall 待投递 ID 与水位/审计/失效作业、真实备份恢复。真实浏览器在 Required 模式分别取消 pending/delivered、刷新后核对投递历史与 Task 不变，执行完整 Console 回归。合入必须保留并行 Recall 协议工作，通过公共清单、生成契约、类型、当前完整 CI 和独立安装；副本结果不能代替主目录同一输入验收。

拒绝复用无 Console CAS 的宿主 cancel、伪造 ACK 或补写永久删除事实。当前验证与未完成范围见 [Phase14](../development/phase-14-hardening-release.md) 和 [验证报告](../reports/phase-14-verification.md)。
