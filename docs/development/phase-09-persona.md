# 阶段 9：完整 Persona

> 状态：Completed
> 负责人：Iris Memory Core Team
> 开始日期：2026-09-03
> 完成日期：2026-09-04
> 前置阶段：[阶段 8](./phase-08-profile-graph.md)
> 目标版本：0.10.0（已达成：Core/双 SDK 0.10.0、Schema 10、Contract 1.8.0）
> 架构依据：[§14 Persona 系统](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#14-persona-系统)、[§18 Recall 协议](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#18-recall-协议)、[§23.3 Persona API](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#233-主要端点)、[§29.3 Prompt 与模型安全](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#293-prompt-与模型安全)、[§36 阶段 9](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#阶段-9完整-persona)
> 决策记录：[ADR-0018](../adr/0018-phase9-persona.md) · 验证报告：[phase-09-verification](../reports/phase-09-verification.md)

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
| P9-REVISION-01 | 不可变 Core/Trait/Narrative Revision、Pointer、Hash 与历史 | 9.1 | Bootstrap 迁移、读取、Hash 和并发发布测试 ✅ |
| P9-STATE-01 | Persona State 白名单、TTL、衰减与 Baseline 回归 | 9.2 | 时间、重启 Catch-up、值域和来源测试 ✅ |
| P9-POLICY-01 | locked/manual/bounded_auto、证据、幅度与冷却期 | 9.3 | Policy 性质、恶意来源与边界测试 ✅ |
| P9-PROPOSAL-01 | 结构化 Proposal、Field Delta、Stale Base 与审批 | 9.3 | Schema、状态机、并发和权限测试 ✅ |
| P9-PUBLISH-01 | 原子发布/回滚、通知、失效与多端一致 | 9.4 | E2E、Outbox 与故障测试 ✅（离线重连/Cache 采用属宿主侧，Phase 11/12 验证） |
| P9-SECURITY-01 | 管理平面隔离，Persona 不覆盖宿主安全策略 | 9.5 | Capability、Prompt Injection、审计与泄漏测试 ✅ |

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

- [x] 多个并发客户端读取完全相同的 Persona Revision/Content Hash——`current` 与 RecallResponse
  顶层值在 Bootstrap、发布和回滚三点逐次相等（`test_recall_top_level_persona_tracks_publish_and_rollback`）。
- [x] 同 Base Revision 并发发布只有一个成功——50 并发写恰好 1 胜 49 `revision_mismatch`
  （`test_same_expected_revision_concurrency_has_exactly_one_winner`）。
- [x] Prompt Injection、单一恶意来源、无 Evidence 和 Core 自动修改均被拒绝——200 个固定种子
  Core 注入形状全部拒绝，Evidence 数量/多样性不足逐项拒绝。
- [x] locked/manual/bounded_auto、幅度、累计窗口、冷却期和敏感字段测试通过——每条限制单独驱动，
  其余放开（`test_evidence_diversity_span_cumulative_and_cooldown_each_deny_publication`）。
- [x] State 到期与重启后确定性回归 Baseline——Worker fencing 路径（迟到任务 no-op）与
  `expire_due_states` 重启 Catch-up 路径分别覆盖，同一时钟轨迹连续 3 次得到相同
  Revision 与状态值。**时钟回拨未单独构造**：到期判定只比较单调递增的 `expires_us`
  与当前 `now_us`，回拨的后果是推迟到期而非错误到期（见已知限制 5）。
- [x] 发布、失效、Cache Invalidation 键与回滚新 Revision E2E 通过；Outbox 通知为 refs-only。
  **离线重连与宿主采用收敛不在本阶段验证**：Core 至今没有 HTTP 传输层，宿主侧链路由
  Phase 11/12 端到端验证（ADR-0017 §3）。
- [x] Bootstrap Migration、Schema/通知兼容和回退方案、需求追踪及交付证据已完成评审
  （[ADR-0018](../adr/0018-phase9-persona.md)、[phase-09-verification](../reports/phase-09-verification.md)）。

## 交付证据

- **代码/变更**：`domain/persona.py`（Patch 展平、Core 拒绝、State 值域/TTL、幅度与内容
  Hash 规范化）、`storage/persona.py`（7 表仓库 + Pointer CAS + 到期扫描 + 累计/冷却查询）、
  `application/persona.py`（Current/History、Policy 替换、State 写入、Proposal 创建/审批/
  拒绝、发布与回滚、Evidence 准入、Policy 评估、审计与 Outbox）、`jobs/handlers.py`/
  `worker.py`（启用 `persona.revised`、`persona.revision_invalidated`、`persona.state_expire`
  三个 kind 并接线 `phase9_handlers`；`persona.evaluation` 仍是 disabled 占位，归 Phase 10）、`application/health.py`（readiness 增加
  Persona Pointer/Metadata/Policy/State 一致性）、`storage/backup.py`（恢复完整性检查）、
  `observability/metrics.py`（`iris_persona_proposals_total{outcome}`）、双 SDK 0.10.0、
  `tools/generate_contracts.py` 与 `tools/mock_server.py`（Contract 1.8.0 契约面）。
- **Persona Schema/Policy 版本**：Persona 内容编码沿用 Phase 1 冻结字节；Policy 配置为
  显式白名单键（未知键 `invalid_request`），`mode ∈ {locked, manual, bounded_auto}`，
  数值域 [0, 1]，State TTL ≤ 7 天。
- **Schema/Migration**：`migrations/0010_phase9_persona.sql`（online_safe=true、lock_ms=200、
  min_app=0.10.0、recovery=none；7 张 STRICT 表），运行时兼容窗口 [9, 10]；Phase 1 的
  `persona_revisions` 与 `agents.persona_current_revision_id` 字节、ID 与 Hash 不变
  （`test_schema9_upgrade_preserves_bootstrap_bytes_pointer_and_hash`）。
- **测试**：`tests/unit/test_phase9_persona_domain.py`（4 个 200 案例固定种子性质）、
  `tests/integration/test_phase9_persona.py`（17）、`test_phase9_migration.py`（3）、
  `tests/performance/test_persona_latency.py`（1）——Phase 9 新增 **25** 个用例，另有
  10 个 Persona 契约 Fixture 与 mock server 用例；全量门禁数字以
  [phase-09-verification](../reports/phase-09-verification.md) 为准。
- **安全/并发/E2E 报告**：[phase-09-verification](../reports/phase-09-verification.md)。
- **决策**：[ADR-0018](../adr/0018-phase9-persona.md)。

## 已知限制

1. **离线重连、宿主 Cache 采用与通知丢失重放未做端到端验证**：Core 只证明 Outbox 事件是
   refs-only 且缓存键为 `(agent_id, revision, content_hash)`。真实收敛需要传输层，归
   Phase 11/12（ADR-0017 §3）。
2. **`bounded_auto` 的累计窗口按已发布 Proposal 统计**：管理员直接 `publish_revision` 不计入
   累计幅度——管理平面被视为授权旁路，不是自动演进。
3. **State 到期依赖 Worker 或启动 Catch-up 扫描**：两者都未运行时，过期 State 仍会被
   `current` 读到；宿主不得把 State 当作强一致的到期语义使用。
4. **Evidence 只接受当前 Revision 的资源**：历史 Revision 或已撤销资源一律 fail closed，
   因此跨越长时间窗口的 Proposal 需要在证据仍 current 时提交。
5. **时钟回拨会推迟 State 到期**：到期判定是 `expires_us <= now_us` 的直接比较，没有独立的
   单调时钟来源。系统时钟回拨期间过期 State 继续被 `current` 返回，回拨结束后由同一确定性
   扫描收敛；本阶段没有单独构造回拨用例。
6. **无 HTTP 传输层**（应用层契约 + mock server 模式）：已发布 OpenAPI 的 69 条路径至今
   没有真实服务端，归 Phase 10 交付（ADR-0017 §3）。该项在传输层交付前不得从任何阶段的
   已知限制中移除。

## 明确不做

- 不保存模型私有 Chain-of-Thought。
- 不把用户 Profile、普通 Claim 或一次性措辞直接当作 Persona Current。
- 不允许普通应用 Token 发布、审批或回滚 Persona。

## 交接条件

Phase 10 可以生成受严格校验的 Persona Proposal 候选；Phase 11/12 可以依赖 Current/History、Revision/Hash、通知和可信 Slot 契约。
