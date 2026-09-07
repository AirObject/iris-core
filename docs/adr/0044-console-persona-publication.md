# ADR-0044：Console Persona 发布与回滚

状态：Accepted，2026-09-07。决定接受与组合验收分别记账；继续 [ADR-0018](0018-phase9-persona.md) 和 [ADR-0025](0025-console-command-authorization.md)，不代表 Phase14 或完整 Persona 管理已完成。

## 授权与版本

发布与回滚使用真实 Console CommandActor、独立 persona.publish、memory.read、console.manage、覆盖 Agent 父范围的 Grant 和五分钟内重新认证。浏览器提交继续要求 Session、CSRF、固定 reason_code 和幂等键。不能借用宿主 Lease、伪造 admin/capabilities 或由请求指定身份。以稳定 Agent ID 绑定命令目标，避免 Current 指针推进后相同请求改变幂等身份。

首次写入在同一事务核对 expected_revision 和 expected_policy_revision，并验证当前人格可见性。发布复用手工发布的内容校验、持久化 CAS、审计、水位和 persona.revised 通知；明确人工发布权限与已有自动 Proposal 的 Policy 门禁分开。locked 不禁止具有显式人工发布权限的手工修改，不用该命令绕过 Proposal 审批。原幂等回执仅包含资源 ID、Agent、Revision、内容 Hash 和提交状态，缓存返回后再次核对当前权限、会话、近期认证和来源可见性。

## 证据与回滚

CommandActor 分支通过真实 Console Grant 和 ResourceReader 校验 Evidence 的当前可见性，并复用租户、Agent、当前 Revision、状态和 Tombstone 规则；不扩充虚构 AccessContext。PersonaState 只增加按不可变 ID 的内部引用读取，校验其来源链；不增加公共分页集合或全表扫描。作为本次提交 Evidence 的 PersonaState 必须仍为 Current 且未到期。

回滚检查当前人格、目标历史人格及目标证据后创建新 Revision，完整保留目标 Core/Traits/Narrative 原始字节和 Hash，并保留有效来源引用。与既有宿主回滚清空 source_refs 的行为分开，避免复制内容后脱离后续来源权限与遗忘约束。旧记录不改写，旧 Current 记为 superseded；同时发出 persona.revision_invalidated。失败必须回滚新行、指针、水位、通知和成功回执。复用 Schema20，无新迁移。

## 契约与页面

新增严格发布/回滚请求、最小回执和按 Agent 授权的命令元数据。Current GET 保持无查询参数的 ResourceViewEnvelope，补充 Policy Revision、Mode、Hash 和当前动作；历史读取展示真实生命周期状态。独立 Persona 页面分别读取 Current、历史、提案与命令元数据，固定已审阅的 Persona/Policy Revision，发布和回滚均要求确认，服务端挑战时重新认证并重试原请求。保留 Agent URL 选择以支持页面刷新。冻结的 Bootstrap 空层只在编辑表单中按空对象解释，不修改历史字节。

不提供 Published/Core/Current 删除，不把发布历史当作草稿。PersonaState 管理、Proposal 创建/审批/拒绝、真实 Draft 生命周期及完整 Policy 管理留在 Phase14 后续工作；保留策略、导入导出、生产硬化和发布门禁亦未由本决定完成。

## 验证与恢复

验证真实 Required 模式、父范围授权、近期认证、CAS 并发、缓存后撤权、失败回滚、直接与间接证据、过期/替换/隐藏来源、严格 HTTP 和只读角色。发布与回滚改变 Recall 顶层 Revision；即使回滚恢复相同 Hash，旧 Recall 仍失效且原 Usage JSON 不重写。已提交快照恢复保留 Current 和完整历史，不能以此替代全部灾备/凭据恢复要求。

合入采用逐文件摘要保护，保留并行工作，并运行同一主目录候选的完整 CI、Console 实际浏览器和独立安装门禁。当前结果与未完成范围见 [Phase14](../development/phase-14-hardening-release.md) 和 [验证报告](../reports/phase-14-verification.md)。
