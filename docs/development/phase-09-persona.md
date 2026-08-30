# 阶段 9：完整 Persona

> 状态：Planned  
> 前置阶段：[阶段 8](./phase-08-profile-graph.md)  
> 目标版本：0.10.0  
> 架构依据：[§14 Persona 系统](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#14-persona-系统)、[§18 Recall 协议](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#18-recall-协议)、[§23.3 Persona API](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#233-主要端点)、[§29.3 Prompt 与模型安全](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#293-prompt-与模型安全)、[§36 阶段 9](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#阶段-9完整-persona)

## 阶段目标

将 Phase 1 的最小锁定 Persona Bootstrap 扩展为完整版本化人格系统，使多个宿主稳定读取相同 Core/Trait/Narrative Revision，并支持短时 State、受策略约束的 Proposal、发布、失效、通知和创建新 Revision 的回滚。

## 架构约束

- Persona Core 永远不能由模型自动修改；管理平面和应用平面凭据隔离。
- PersonaRevision 不可变，发布/回滚均创建新 Revision，Current Pointer 使用 Expected Revision 串行化。
- Persona State 有 TTL、白名单和值域，到期确定性回归 Baseline。
- 单一用户、单次情绪、Prompt Injection 或无效 Evidence 不能改变 Trait/Core。
- Persona 只进入宿主可信 Persona Slot，不能覆盖宿主安全、工具或平台策略。

## 需求追踪

| 需求 ID | 基线要求 | 工作包 | 验证门禁 |
| --- | --- | --- | --- |
| P9-REVISION-01 | 不可变 Core/Trait/Narrative Revision、Pointer、Hash 与历史 | 9.1 | Bootstrap 迁移、读取、Hash 和并发发布测试 |
| P9-STATE-01 | Persona State 白名单、TTL、衰减与 Baseline 回归 | 9.2 | 时间、重启 Catch-up、值域和来源测试 |
| P9-POLICY-01 | locked/manual/bounded_auto、证据、幅度与冷却期 | 9.3 | Policy 性质、恶意来源与边界测试 |
| P9-PROPOSAL-01 | 结构化 Proposal、Field Delta、Stale Base 与审批 | 9.3 | Schema、状态机、并发和权限测试 |
| P9-PUBLISH-01 | 原子发布/回滚、通知、失效与多端一致 | 9.4 | E2E、离线重连、Cache 和故障测试 |
| P9-SECURITY-01 | 管理平面隔离，Persona 不覆盖宿主安全策略 | 9.5 | Capability、Prompt Injection、审计与泄漏测试 |

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

## 数据、契约与回退策略

- 采用 Expand/Validate/Cutover 将 Phase 1 最小 Published Persona/Current Pointer 扩展到完整 Core/Trait/Narrative/Policy/State/Proposal Schema；先双读验证 Content Hash，再切换正式 Pointer，不原地改写 Bootstrap Revision。
- PersonaRevision、PersonaState、Policy 和 Proposal 分表版本化；发布/回滚单事务创建新 Revision、CAS 更新 Current Pointer、写 Audit/Watermark/Cache Invalidation Outbox，旧 Revision 永久只读。
- Current/History/State/Proposal/Publish/Rollback API、管理 Capability、通知事件、错误码与 SDK Fixture 同步冻结；普通应用 Credential 无法通过 Body 或未知字段获得发布权限。
- Persona State/Policy/Proposal Schema、Hash 规范化和通知 Payload 显式版本化；滚动部署期间新 Reader 支持上一版本，旧 Reader 遇到未知必需 Schema 时 Fail Closed，不使用未验证 Persona。
- 回退优先把 Current Pointer 指向由历史内容新建的 Rollback Revision；二进制回退前验证其能读取当前 Persona Schema。必要时恢复切换前备份，并回放切换后的发布/回滚审计，禁止重新激活旧行。

## 量化验收基线

- Persona Current Read 在声明硬件、并发和缓存状态下 p95 ≤ 20 ms；响应 Revision 与 Content Hash 必须和 RecallResponse 顶层值 100% 一致。
- locked/manual/bounded_auto、字段 Allowlist、单次/累计幅度、Evidence 数量/多样性/时间窗、冷却期和 Stale Base 性质每项至少运行 200 个固定种子案例。
- 50 个客户端以同一 Base/Expected Revision 并发发布时恰好一个成功，其余返回稳定 `revision_mismatch` 或 `persona_base_revision_stale`，只产生一个 Current Pointer 逻辑推进。
- State TTL 测试覆盖 UTC、时钟前跳/回拨、暂停和重启 Catch-up；连续 3 次同一时钟轨迹得到相同 Baseline 回归 Revision 和状态值。
- 发布、通知丢失、离线重连、Cache 失效、宿主采用和回滚链路各至少重复 20 次；所有宿主最终收敛到同一 Revision/Hash，未知或哈希不符 Persona 的采用数为 0。

## 退出门禁

- [ ] 多个并发客户端读取完全相同的 Persona Revision/Content Hash。
- [ ] 同 Base Revision 并发发布只有一个成功，其他返回稳定 Stale/Revision 错误。
- [ ] Prompt Injection、单一恶意来源、无 Evidence 和 Core 自动修改均被拒绝。
- [ ] locked/manual/bounded_auto、幅度、累计窗口、冷却期和敏感字段测试通过。
- [ ] State 到期、时钟跳变和重启后确定性回归 Baseline。
- [ ] 发布、失效、离线重连、Cache Invalidation 和回滚新 Revision E2E 通过。
- [ ] Bootstrap Migration、Schema/通知兼容和回退方案、需求追踪及交付证据已完成评审。

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
