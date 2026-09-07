# ADR-0045：Console PersonaState 管理

状态：Accepted，2026-09-07。决定与实现验收分别记账；继续 [ADR-0018](0018-phase9-persona.md)、[ADR-0025](0025-console-command-authorization.md) 和 [ADR-0044](0044-console-persona-publication.md)。

## 授权与独立版本

PersonaState 更新和到期清理使用真实 Console CommandActor、memory.read、memory.write、console.manage 与覆盖 Agent 父范围的 Grant。以稳定 Agent ID 绑定幂等目标；expected_revision 对应独立 PersonaState 版本，0 仅表示尚无状态。命令在同一事务重验实际权限、当前状态及其来源可见性、State CAS，再写不可变状态行、Current State 指针、审计、水位和到期 Job。成功回执仅含资源 ID、Agent、状态 Revision 与提交状态；缓存返回继续核对当前权限和来源可见性。

这些命令不修改 Persona Current，不发布 Core/Traits/Narrative，不借用宿主 Lease 或伪造 Capability。沿用普通管理写入的 Session、CSRF、reason_code 和幂等要求；人格发布所需的独立 persona.publish 和近期认证不扩大到短期 State 更新。

## TTL、基线与来源

更新仅接受现有状态字段白名单和值域，TTL 为整数微秒且不超过七天，证据使用真实 Console Grant 和既有 Persona Evidence 当前性规则。只增加按 Agent 的 Current State 读取，不开放 persona_states 全表集合。

清理仅接受已到期的 Current State，并要求旧状态及来源当前仍然可见。清理创建新的基线版本，旧行与旧引用保留；不删除历史。新行 state 与 baseline 均为旧基线，started_us 为旧 expires_us，expires_us 为旧 expires_us + 1，source_refs 为空。这与既有 Worker 和启动 catch-up 的 expire_to_baseline 行为一致：短期事件证据在到期回归时退出当前状态，基线是先前显式设置的字段白名单值。手工清理不能用于复制不可见的旧基线。新状态 ID/Revision 使旧到期 Job 失效。

清理没有改变既有过期读取或时钟模型；系统时钟回拨可能推迟回归，仍由 Phase14 运行时硬化和故障验证承接。状态快照恢复需保留指针、历史和待执行到期任务，并验证 catch-up 后旧任务不能重复推进版本。

## 契约与页面

新增 GET/PATCH `/personas/{agent_id}/state` 与 POST `/personas/{agent_id}/state:clear`。读取返回 current 资源或 null、独立 expected_revision 及实际授权的表单元数据。严格更新 DTO 接受 state、可选 baseline、ttl_us 和证据；严格清理 DTO 仅接受版本与原因。复用 Schema20，不新增迁移或业务 SDK 方法。

Persona 页面展示独立状态版本、当前状态、基线、到期时间与证据。打开表单时固定已审阅版本，写入冲突不能自动覆盖。只读角色不显示写动作，清理仅在服务端判断已到期时提供。刷新后读取真实持久化状态。

## 验证与边界

验证 Required 模式、父范围授权、只读角色、严格 HTTP、并发 CAS、缓存后撤权、来源失效、入队失败的事务回滚、旧 Job 围栏、备份恢复和 catch-up。真实浏览器覆盖写入、到期清理、刷新与 Persona Current 保留；整合后运行完整 CI 和独立安装门禁。

本决定不完成 Proposal、Draft、Policy、保留策略或稳定发布。阶段状态与实际结果见 [Phase14](../development/phase-14-hardening-release.md) 和 [验证报告](../reports/phase-14-verification.md)。
