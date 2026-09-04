# ADR-0018：Phase 9 完整 Persona 的版本、策略与发布协议

- 状态：Accepted
- 日期：2026-09-03
- 影响阶段：Phase 9；Phase 10–12 可依赖本协议
- 基线：§14、§18、§23.3、§29.3、§32.6、§36 阶段 9；ADR-0004/0006/0008/0014/0017

## 背景

Phase 1 只冻结了最小 Published Persona 与 Agent Current Pointer。Phase 9 必须在不改写
Bootstrap 字节、ID、Revision 或 Hash 的前提下，补齐 Core/Trait/Narrative 历史、短时
State、演进 Policy/Proposal、管理发布、回滚与多宿主失效协议。模型生成内容始终是不可信
数据，不能因为被称为 Persona 而获得系统指令或权限优先级。

## 决策

### 1. 版本与迁移

- Core、Python SDK、TypeScript SDK 为 **0.10.0**；Schema **10**；Contract **1.8.0**。
- `0010_phase9_persona.sql` 是 additive expand migration，运行时兼容窗口为 **[9, 10]**。
- Schema 9 的 `persona_revisions` 与 `agents.persona_current_revision_id` 不改写；迁移只为其
  建立确定性的 locked Policy 与 Revision Metadata。旧 Bootstrap 的 `[]`/空字符串在读侧
  解释为空对象，但存储字节和 Hash 永不重算。

### 2. Canonical 模型

- PersonaRevision 内容仍落在既有不可变 `persona_revisions`；正式生命周期、Policy、来源、
  生效区间和前驱关系由 `persona_revision_metadata` 扩展。
- 每个 Agent 恰有一个 current Policy；Policy 版本不可变，替换通过 revision CAS 完成。
- PersonaState 是独立不可变 Revision 流，Current Pointer 使用 expected revision；每条状态
  保存 baseline、TTL 与 `expire_to_baseline` 策略。
- Proposal 与 Proposal Event 分表；Proposal 保存结构化 patch、逐字段 delta、Evidence refs、
  generator 身份、Policy 评估和审核结果。事件表只追加。

### 3. 权限与策略

- 读取、State 写、提案、管理/审核分别使用 `persona.read.v1`、
  `persona.state.write.v1`、`persona.review.v1`、`persona.manage.v1`；发布、审批和回滚还必须是
  Admin 并携带 reason。
- Proposal 永远不能包含 Core。`locked` 拒绝演进；`manual` 必须人工批准；
  `bounded_auto` 仅发布 allowlist 中、非敏感且满足单次/累计幅度、Evidence 数量/多样性/
  时间跨度、Confidence、观察期与冷却期的 Trait/Narrative 字段。
- 创建 Proposal 与真正发布之间必须再次读取当前 Policy、Current Revision 与 Evidence；
  Evidence 已删除、非 current、跨 Tenant/Agent/Scope、Privacy 不可见或状态失效即 fail closed。

### 4. 原子发布、回滚与通知

- 发布事务原子创建新 Revision、CAS Current Pointer、封存前一 Metadata、写 Audit、推进
  Watermark，并写 refs-only Outbox；50 个相同 Expected Revision 的并发写只有一个获胜。
- 回滚不是重新激活旧行，而是创建新 Revision；新行逐字节复制目标历史内容和 Hash，以兼容
  冻结的 Bootstrap 编码。
- 通知冻结为 `persona.revised.v1` 与 `persona.revision_invalidated.v1`，payload 只含 Agent、
  Revision、Hash、reason 与失效 Revision，不含 Persona 内容。离线宿主重新读取 Current，
  缓存键为 `(agent_id, revision, content_hash)`。

### 5. State 到期与可观测性

- State TTL 最大七天。到期任务以 State ID + Revision fencing；迟到任务若已被新状态替代则
  no-op。启动时可调用同一确定性 catch-up 扫描，把仍 current 的过期状态写成新的 baseline
  Revision。
- `/health/ready` 与备份恢复完整性检查把 Persona Pointer、Metadata、Policy 与 State Pointer
  不一致视为 fatal。
- `iris_persona_proposals_total{outcome}` 只使用低基数状态标签；日志、通知与指标均不携带内容。

### 6. 契约面

- 发布 Current、History、Revision Create、State Update、Proposal Create、Approve、Reject、
  Rollback 八条 `/v1/personas` 路径及 Python/TypeScript SDK 方法。
- 新稳定错误为 `persona_policy_denied` 与 `persona_base_revision_stale`；普通 CAS 竞争继续使用
  `revision_mismatch`。
- 与 ADR-0017 一致，本阶段冻结应用层、OpenAPI、SDK 与 mock server；真实 HTTP/ASGI 传输层
  仍由 Phase 10 交付。

## 否决的替代方案

- 原地扩列并重写 Bootstrap JSON/Hash：破坏 ADR-0008 的兼容缝。
- 回滚时把旧 Revision 重新标为 Published：破坏不可变历史和审计时间线。
- 只在 Proposal 创建时评估 Policy/Evidence：存在审批时序竞争，可用换 Policy、撤销 Evidence
  或推进 Current Revision 绕过。
- 把 Persona 放入普通 Recall Candidate：允许不可信正文与可信 Persona Slot 竞争。
- 让 State 到期直接修改旧行：无法重放，也不能用 Revision fencing 防迟到任务。

## 后果与迁移影响

- Schema 9 可在线升级到 10；Schema ≤8 需先用 0.9.0 完成 staged upgrade。
- 回退二进制前必须确认 Current Persona 的 Schema 可读；数据回退使用创建新 Rollback Revision，
  不删除 Phase 9 历史表。
- Phase 10 可生成 Proposal 候选，但只能调用本 ADR 的结构化接口；Phase 11/12 可依赖
  Current/History、Revision/Hash 和 refs-only 通知，不能复制 Policy 判断。
