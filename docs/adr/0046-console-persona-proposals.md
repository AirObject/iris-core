# ADR-0046：Console Persona 提案管理

状态：Accepted，2026-09-07。继续 [ADR-0018](0018-phase9-persona.md)、[ADR-0025](0025-console-command-authorization.md) 和 [ADR-0044](0044-console-persona-publication.md)；决定接受和当前候选验收分别记账。

## 创建与发布权限分离

Console 创建提案要求实际 memory.write、memory.read、console.manage 和覆盖 Agent 父范围的 Grant。创建始终保存 proposed 待审项，包括 bounded_auto Policy 下；请求不能选择自动发布或指定伪造的模型/操作者身份。generator 固定为实际 Console 操作者，generator_version 记录 Console 管理入口版本。原宿主和 Reflection 的自动发布分支保持不变。

创建核对已审阅的 Persona base_revision 和 expected_policy_revision，复用 Trait/Narrative 字段、Evidence、Policy、变化幅度、累积窗口、冷却期及置信度规则。禁止通过 Proposal 修改 Core。TTL 为正整数微秒，上限为既有三十天默认周期；到期时间由服务端时钟计算。Policy locked 继续禁止提案创建和发布。

## 人工审阅与事务

批准和拒绝要求实际 persona.publish、memory.read、console.manage、父范围 Grant 与五分钟内重新认证。浏览器继续使用 Session、CSRF、固定原因和幂等键。没有宿主 Lease 或伪造 Capability 分支。

Proposal 使用不可变 ID/base_revision 和 proposed 状态 CAS，没有新增普通整数 Revision。批准核对当前 Persona 与基准一致、当前 Policy 与已审阅版本一致、提案未到期、原始证据仍满足当前权限和领域规则；在共享事务中先转换 approved，再发布新 Persona，最后记录 published 和对应 Revision ID。中途失败回滚全部状态事件、人格指针、内容、通知、审计和成功回执。

拒绝核对提案的不可变基准和 proposed 状态，但允许拒绝基准已变化或已到期的提案，不要求当前 Policy 版本匹配。拒绝保留内容和事件历史，不能删除已发布人格。提案及其来源必须对实际操作者可见；当前人格不可见时不暴露其版本或 Policy，也不提供批准动作。过期批准失败不声称已经持久化 expired 状态，既有后台生命周期不在本接口中改写。

幂等目标绑定稳定 Agent，提案 ID 和已审阅版本进入请求指纹；指针推进后同一请求仍可返回原命令回执。回执仅含提案 ID、Agent、该次提交状态和可空的 published_revision_id；返回缓存后继续检查当前权限、认证及来源可见性。提案写入推进 Agent 水位但不伪造 Proposal 数字版本；真正发布另记实际 Persona Revision 水位项。

## 契约与管理页面

新增创建、批准、拒绝严格请求和最小回执，另增创建上下文及单提案详情。上下文包含实际动作、基准版本、可见的当前 Persona/Policy 版本及状态，列表继续使用既有分页接口。路由保留设计中的 `/personas/{agent_id}/proposals` 与 `/{proposal_id}:approve|:reject`。复用 Schema20，不新增业务 SDK 方法或迁移。

页面提供分页列表、来源与证据、到期时间、修改内容及详情。打开表单时固定已审阅的版本，不从提交瞬间的新数据自动替换。创建总是待审，批准和拒绝使用敏感操作确认及重新认证流程；完成后重新读取人格指针、列表和历史。旧通用 ResourceView 的展示版本不用于 Proposal 写入 CAS。

## 验证与剩余边界

验证 Required 模式、父范围权限、普通写权限不能发布、严格请求、Policy/Evidence/到期失效、并发审阅、成功缓存后撤权和晚期通知失败回滚。实际浏览器验证创建两份待审提案、重新认证批准一份、拒绝基准已变化的另一份及刷新后的持久化结果。实际备份恢复验证 published/rejected/proposed 三种记录、审阅事件、完整人格历史和 Current 指针。

整合必须保留并行改动，审阅公共接口清单，运行同一主目录候选的完整 CI、真实浏览器和独立安装。Draft、Policy 编辑、保留管理及 Phase14 生产硬化/稳定发布仍未完成；本切片不构成完整 Persona 或灾备验收。阶段与证据见 [Phase14](../development/phase-14-hardening-release.md) 和 [验证报告](../reports/phase-14-verification.md)。
