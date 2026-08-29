# 阶段 9：完整 Persona

> 状态：Planned  
> 前置阶段：[阶段 8](./phase-08-profile-graph.md)  
> 架构依据：[Persona 系统、安全、Recall 协议与阶段 9](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md)

## 阶段目标

将 Phase 1 的最小锁定 Persona Bootstrap 扩展为完整版本化人格系统，使多个宿主稳定读取相同 Core/Trait/Narrative Revision，并支持短时 State、受策略约束的 Proposal、发布、失效、通知和创建新 Revision 的回滚。

## 架构约束

- Persona Core 永远不能由模型自动修改；管理平面和应用平面凭据隔离。
- PersonaRevision 不可变，发布/回滚均创建新 Revision，Current Pointer 使用 Expected Revision 串行化。
- Persona State 有 TTL、白名单和值域，到期确定性回归 Baseline。
- 单一用户、单次情绪、Prompt Injection 或无效 Evidence 不能改变 Trait/Core。
- Persona 只进入宿主可信 Persona Slot，不能覆盖宿主安全、工具或平台策略。

## 工作包

### 9.1 Persona Revision 与读取

- 完整实现 Core/Trait/Narrative Schema、Content Hash、History、Current Pointer 和有效期。
- 将 Phase 1 Bootstrap 数据无损迁移到正式 Schema，验证每个 Agent 均有有效 Published Persona。
- 实现 Current/History API，并让 RecallResponse 始终携带一致 Revision/Hash。

### 9.2 Persona State

- 实现 State Revision、Baseline、TTL、Decay Policy、来源和白名单校验。
- 接入 Scheduler 完成到期回归、重启 Catch-up 和 Cache Invalidation。
- 区分 `affect`/近期状态与长期 Trait，禁止隐式晋升。

### 9.3 Policy 与 Proposal

- 实现 `locked|manual|bounded_auto`、字段 Allowlist、幅度、累计窗口、Evidence 多样性、Confidence 和冷却期。
- 实现结构化 Field Delta、Base Revision、过期、审批/拒绝和发布前重评估。
- Core 修改、敏感字段、未知 Entity/Evidence、Stale Base Revision 由确定性规则拒绝。

### 9.4 发布、回滚与通知

- 原子创建 Revision、更新 Current Pointer、Audit 和 Cache Invalidation Outbox。
- 实现 `persona.revised.v1`/Revision Invalidated 通知、离线重连版本协商和宿主缓存键。
- 回滚从历史内容创建新 Revision，不重新激活旧行；记录原因、操作者和来源。

### 9.5 安全与管理

- 实现 Persona 专用管理 Capability、审批原因和审计查询。
- 对 Proposal/Persona 文本做不可信数据隔离、Schema/长度/值域/来源验证。
- 建立异常反馈和自动回滚告警接口，但 Core 不决定宿主最终回复。

## 退出门禁

- [ ] 多个并发客户端读取完全相同的 Persona Revision/Content Hash。
- [ ] 同 Base Revision 并发发布只有一个成功，其他返回稳定 Stale/Revision 错误。
- [ ] Prompt Injection、单一恶意来源、无 Evidence 和 Core 自动修改均被拒绝。
- [ ] locked/manual/bounded_auto、幅度、累计窗口、冷却期和敏感字段测试通过。
- [ ] State 到期、时钟跳变和重启后确定性回归 Baseline。
- [ ] 发布、失效、离线重连、Cache Invalidation 和回滚新 Revision E2E 通过。

## 交付证据

- 代码/变更：待补充
- Persona Schema/Policy 版本：待补充
- Schema/Migration：待补充
- 安全/并发/E2E 报告：待补充
- 已知限制：待补充

## 明确不做

- 不保存模型私有 Chain-of-Thought。
- 不把用户 Profile、普通 Claim 或一次性措辞直接当作 Persona Current。
- 不允许普通应用 Token 发布、审批或回滚 Persona。

## 交接条件

Phase 10 可以生成受严格校验的 Persona Proposal 候选；Phase 11/12 可以依赖 Current/History、Revision/Hash、通知和可信 Slot 契约。
