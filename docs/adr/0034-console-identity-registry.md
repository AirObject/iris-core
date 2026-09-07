# ADR-0034：Console 身份注册与显式绑定生命周期

状态：Accepted，2026-09-07。落实 [资源矩阵](../design/console-backend.md#51-资源矩阵) 中实体创建、外部身份注册和 Binding 提议、确认与撤销。实体属性、更正 redirect 与 tombstone 后续单独实施。

## 管理授权与领域复用

Entity、ExternalIdentity 和 Binding 属于租户全局数据。请求不接受伪造的 Agent/Space Scope；向下可读的 Grant 不自动获得全局写权限。每次管理命令及幂等重放都检查当前密钥、会话、Grant、目标与实体/外部身份端点。隐私来自实体及关联端点；仅创建 Entity 可明确提交隐私标签。

静态 entity.create、identity.create、binding.create、binding.confirm 与 binding.revoke 使用 memory.write。IdentityService 提取共享事务方法，原公开业务入口保留原有管理员检查、幂等行为和身份视图。Console 通过 CommandActor 和当前授权访问共享方法，不伪造 AccessContext.admin，不嵌套写事务。领域审计记录实际 console 操作者和原因，Console 命令审计与 Canonical 修改同事务提交。

实体始终从 provisional 创建，同名也分别获得新 ID。外部身份按 provider/realm/external_id 自然键复用；复用前重新检查实际记录可见与可写。省略 entity_id 保留原始关联，明确指定不同实体返回冲突。初始关联不是已验证身份，不能代替 Binding 确认。

## 绑定与历史

提议仅接受外部身份和目标实体 ID。method、confidence、proof_digest、state 与 confirmed_by 都由服务控制，客户端不能宣称已经验证。提议记录为 proposed；proof_digest 由真实操作者、原因、端点和记录时间生成。确认、撤销均携带 expected_revision 及原因，使用原状态机 CAS。

确认可以把 provisional 实体提升为 canonical，因此还要检查当前目标实体可写。竞争的已验证绑定若不可见，在任何状态修改之前返回 404。可见竞争沿用原领域语义：将提议提交为 conflicted，再返回 409；幂等重放不再次追加修订，不自动撤销已有胜出绑定。操作者可明确撤销原绑定后再确认冲突提议。

撤销设置有效期并保留发生时身份、修订和审计，不能把撤销当物理擦除。确认/撤销继续发布真实 graph.apply 任务。缓存回执保存当时的有效期与修订，以免后续撤销改变首次成功响应；返回回执前仍重新检查当前记录和所有端点，不借历史回执重新披露已删除对象。

## Console 与契约

增加三个创建表单、绑定详情中的确认/撤销动作，以及当前授权的外部身份 lookup。选择项显示 provider / realm / external_id，不要求操作者复制内部 ID。绑定动作发送 expected_revision 与 reason_code，不加入多余 fields。界面动作可用性来自当前权限和状态，服务器再次验证。

新增六个静态 Console 操作、七个请求 Schema 和十九个夹具，并扩充动作/lookup 枚举；原业务 HTTP、SDK、CLI、Core 0.13.0 与 Schema 15 不变。全部资源注册表响应仍按严格契约验证。测试与实际失败、修正记录见 [Phase 14 验证报告](../reports/phase-14-verification.md)。本切片不声明 Entity 全部编辑能力、Forget、生产部署或完整 Phase 14 已完成。
