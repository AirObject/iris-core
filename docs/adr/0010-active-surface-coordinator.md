# ADR-0010: Active Surface Coordinator — Lease、Epoch 与模式语义

- 状态：Accepted
- 日期：2026-08-30
- 影响阶段：Phase 2（阶段 11/12 验证宿主行为）

## 背景

基线 §25 要求一个可选控制平面，保证同一 Agent 在某一时刻只有一个对外互动入口（避免 QQ 与直播同时以同一人格主动互动）。它是产品层约束，**不是**记忆正确性前提：任何模式下 Scope、Privacy、Revision、Idempotency 与事务规则完全相同。

## 决策

### 1. 租约域与 Epoch

- 活动租约以 `(tenant_id, agent_id)` 为唯一域（partial unique index `WHERE status='active'`）。
- `surface_lease_state.current_epoch` 每 Agent 单调递增；Acquire、抢占、过期后重新获取都消耗新 epoch（`next_epoch` 为原子 UPSERT RETURNING）。
- 旧 epoch 的 Heartbeat/Release/在线校验返回 `lease_fenced`；租约过期返回 `lease_expired`；被他人持有且不可抢占返回 `lease_held`（**域内一等错误，稳定码 `lease_held`**，details 携带 holder 过期时间与优先级）。
- **持有者绑定**（复核修复）：Holder 永远是 `AccessContext` 背后经过认证的 app instance——`holder_app_instance_id` 只能等于该实例（缺失时取该实例），Agent 必须在该上下文的授权集合内，holder space 必须在 allowed 集合、属于本租户**且属于该 Agent**（agent-owned space 只接受其归属 Agent 的租约；未指定 agent 的租户共享 space 对任何已授权 Agent 开放）；Heartbeat/Release 同样携带 `AccessContext` 并**重演同一组校验**——撤销 space grant 立即剥夺续租与释放权，租约不会在授权之外续命。读取当前租约（`current`）同样要求 Agent 在授权集合内：仅凭租户身份不得窥探租约状态。不存在"为未授权 Agent 取租约"、"冒名 holder"或"跨 Agent 借用 space"的路径。
- **Release 语义**（复核修复）：只有存活（active 且未过期）的租约可被持有者释放；已过期租约返回 `lease_expired`，被抢占进入 draining 的租约返回 `lease_fenced`——fenced holder 不得再改写租约状态。
- 全部租约事件（acquired/preempted/heartbeat/released/expired/fenced）append-only 记账；回退到 `off` **不删除**任何历史。**过期同样记账**（二轮复核修复）：`expire_stale` 清扫把 lapsed 租约置为 expired 的同一事务内追加 `expired` 事件行（actor 为 `coordinator:expiry_sweep`），事件历史不依赖"谁发现了过期"。

### 2. 抢占：先 Fence，后通知

高优先级（严格大于）且 `allow_preempt`（必须带 reason）时：同一事务内先将旧 Holder 置为 `draining`（fence CAS on epoch+revision+status），记录 preempted/fenced 事件，然后发布 `surface.lease_revoked` Outbox 通知（dedupe key 绑定被撤销租约），最后才创建新租约。旧 Holder 此后的任何操作都被 fencing 拒绝，即使它尚未收到通知。

### 3. 模式矩阵与平面划分

| 模式 | 在线平面（Observe/Recall/认知操作） | 管理维护平面 |
| --- | --- | --- |
| `off` | 不校验；多宿主并发安全由 Phase 1 事务语义保证 | 不校验 |
| `advisory` | 维护租约、在响应中附 `lease_warning`，**绝不拒绝**业务请求 | 不校验 |
| `required` | 请求必须**出示**与当前 active 租约匹配的 lease_id+epoch（缺一即拒）；无租约或未出示→`lease_expired`，过期 epoch/lease_id→`lease_fenced` | 不校验 |

所有在线调用同时携带认证的 `AccessContext.app_instance_id`；proof 与 live lease 匹配但调用者不是记录 Holder 时按 `not_lease_holder` fencing。幂等在线写采用双阶段校验：访问授权后、缓存查询前校验一次，缓存未命中则在 Canonical 业务写事务内再次校验；lease proof 是逐调用凭证，不进入逻辑请求指纹。Observe 与 Phase 4 在线写遵循同一规则，避免缓存回放和预检后抢占绕过 Required 模式。

内部 Worker、备份、迁移、索引、Scheduler 与 Persona 管理属于管理/维护平面，**永不**调用在线校验——管理员凭据不能冒充活动宿主。required 模式下 Coordinator 不可用时 fail closed；advisory 只报告警告，不把记忆服务变成单点可用性门槛。Coordinator 的任何故障只影响自己的表，绝不触碰 Canonical 数据。

**不可达映射稳定错误码**（二轮复核修复）：Coordinator 不可达（存储 busy 或底层 I/O 故障，含 `OSError`）时，在线校验抛出域内一等错误 `NotReadyError`（稳定码 `not_ready`，retryable），绝不放任裸 `OSError` 逃逸、也绝不降级为通用 `domain_error`——因为此刻连"模式是否为 off/advisory"都无法证明，唯一安全的行为是按未知状态 fail closed。

### 4. 部署与回退

从 `off` 开始部署；完成 Epoch/Fencing 验证后方可切换 `advisory`/`required`。模式切换是管理平面操作（admin + reason + revision CAS），逐 Agent 配置。持租约宿主可运行到本地已知 `expires_at` 加固定短 grace（2s）后停止对外互动；服务恢复后必须重新 Acquire，旧 epoch 不复用。

## 否决的替代方案

- **把活动租约当作记忆正确性的前提**：Coordinator 是产品层约束。任何模式下 Scope、Privacy、
  Revision、Idempotency 与事务规则完全相同；`off` 也必须保证并发写入与多端人格版本安全。
- **先通知旧 Holder、后 Fence**：通知与 fence 之间的窗口里旧 Holder 仍是权威持有者，可以继续
  对外互动。顺序必须是同事务内先 fence CAS、再发通知（§2）。
- **对非持有者脱敏 `current()`**：谁持有、到何时本身是运维可观测性需求，且脱敏挡不住其它
  泄漏渠道。凭证失效必须在**验证点**完成（proof 绑定持有者身份），而不是靠保密（§1、
  ADR-0012 §12.2）。
- **允许已过期或被抢占进入 draining 的租约自行 Release**：fenced holder 不得再改写租约状态，
  否则它能在被抢占后清除新持有者赖以仲裁的记录（§1）。
- **回退到 `off` 时删除租约历史**：租约事件是 append-only 审计记录；模式回退不是历史撤销（§1）。
- **Coordinator 不可达时放任裸 `OSError` 逃逸或降级为通用 `domain_error`**：此刻连"模式是否为
  off/advisory"都无法证明，唯一安全行为是按未知状态 fail closed，并以稳定码 `not_ready`
  表达（§3）。
- **`advisory` 模式拒绝业务请求**：会把记忆服务变成单点可用性门槛，与模式定义直接冲突（§3）。
- **管理平面也校验在线租约**：内部 Worker、备份、迁移、索引、Scheduler 与 Persona 管理属于
  维护平面；让管理员凭据去"持有租约"等于允许它冒充活动宿主（§3）。

## 迁移影响

- `migrations/0003_phase2_reliability_spine.sql` 内新增 `surface_lease_state` /
  `surface_leases` / `surface_lease_events` 三张表（与 ADR-0009 同一次迁移，窗口 [2, 3]）。
- **不提供 Down Migration**：租约事件历史不经降级脚本删除。
- **模式迁移是数据迁移之外的运行时迁移**：部署必须从 `off` 开始，完成 Epoch/Fencing 验证后
  才逐 Agent 切换到 `advisory`/`required`（admin + reason + revision CAS）。直接以 `required`
  上线会在 Coordinator 尚未验证时 fail closed 掉全部在线流量。
- 回退到 `off` 立即停止在线校验，不需要数据变更，也不删除任何历史行；持租约宿主按本地
  已知 `expires_at` 加固定 grace（2 s）自然停止。

## 后果

- Schema 3 新增 `surface_lease_state` / `surface_leases` / `surface_lease_events` 三张表。
- 与 ADR-0009 的 Outbox 协同：撤销通知本身是一个不可合并的 outbox 任务（kind `surface.lease_revoked`）。
- 阶段 11/12 的宿主行为验证（含抢占通知的响应时间）以本 ADR 的 fencing 顺序为准。
