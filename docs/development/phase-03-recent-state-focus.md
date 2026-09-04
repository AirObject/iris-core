# 阶段 3：近期上下文、State 与 Focus

> 状态：Completed  
> 前置阶段：[阶段 2](./phase-02-observation-outbox-scheduler.md)  
> 目标版本：0.4.0（Schema 4）· 完成日期：2026-08-30  
> 架构依据：[§9 近期上下文、实时状态与关注项](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#9-近期上下文实时状态与关注项)、[§15.4 Recall](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#154-recall)、[§18 Recall 协议](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#18-recall-协议)、[§30 性能与容量](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#30-性能与容量目标)、[§36 阶段 3](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#阶段-3近期上下文state-与-focus)、[ADR-0011](../adr/0011-phase3-recent-state-focus.md)

## 阶段目标

建立三类语义明确且可恢复的短期能力：可重建的近期对话窗口、版本化的高频当前状态、Canonical 的认知关注项，并提供首个仅依赖结构化数据的 Recall Route。

## 架构约束

- RecentContextProjection 是 Observation 的可重建窗口；FocusItem 是 Canonical 认知对象，两者不得合并。
- 原始近期内容默认不跨 Space，共享只通过显式 Agent/SpaceGroup Scope 的 Focus 或后续长期对象。
- State Coalescing 只能合并投影 Job，不能跳过 Canonical State Revision。
- Focus 的衰减只改变 Activation/状态，不能提高事实 Confidence 或删除来源。
- 所有返回项仍执行 Scope、Privacy、Expiry、Revision 和 Tombstone 校验。

## 需求追踪

| 需求 ID | 基线要求 | 工作包 | 验证门禁 | 结果 |
| --- | --- | --- | --- | --- |
| P3-RECENT-01 | RecentContextProjection 只引用已提交 Observation，且按版本/Watermark 可重建 | 3.1 | 确定性重建、过期、Space 隔离测试 | 通过（见退出门禁 1） |
| P3-STATE-01 | State 当前指针、不可变 Revision、TTL 与 Namespace Policy | 3.2 | 并发写、Coalescing、历史与过期测试 | 通过（见退出门禁 2） |
| P3-FOCUS-01 | Focus 容量、状态机、衰减、激活与晋升 | 3.3 | 状态机、预算、时间与来源性质测试 | 通过（见退出门禁 3） |
| P3-RECALL-01 | 结构化 Route、Deadline、预算、稳定排序与降级 Envelope | 3.4 | 契约、排序、超时和权限矩阵测试 | 通过（见退出门禁 6） |
| P3-RECOVERY-01 | Current Pointer、投影与时间状态在崩溃/重启后恢复 | 3.1–3.4 | 故障注入、重启重建与 Watermark 对账 | 通过（见退出门禁 4） |

## 工作包

### 3.1 RecentContextProjection（已完成）

- `domain/recent.py`：确定性纯函数 builder（稳定键 `(occurred_us, id)` 排序、Token 预算热窗、仅含 Source Refs 的有界压缩段、结果哈希、`projection_invariants` 校验）+ 可注入 TokenEstimator + `RECENT_BUILDER_VERSION`。
- `storage/cognitive.py::RecentContextRepository`：generation/pointer 表、结构化跨 Space 窗口查询（tenant+agent+space 等值 WHERE；Space 窗口按 §5.2 严格空值语义只读 session-less 观察）、过期清扫。
- `application/recent.py`：读取（复算 result hash + 精确 target/ref 元数据终检 → 带同等过滤的 Canonical fallback）、shadow 重建（构建→校验→单事务 insert+原子 swap）、invalidate、admin rebuild（权限+reason+审计）；恢复验证同样拒绝损坏 hash、错绑 pointer 与 ref 元数据漂移。

### 3.2 StateRecord（已完成）

- `domain/state.py`：Namespace Policy（内置 runtime/environment/topic + 租户覆盖 + 保守默认）、写校验（TTL 默认/上限、value 字节、Source Authority 白名单、Scope 要求）、scope_key 规范化。
- `(scope_key, namespace, key)` 唯一身份；创建竞态由 UNIQUE 裁决、更新由 Expected Revision pointer CAS 裁决；PUT 幂等指纹只覆盖调用方字段（服务端时钟默认值不入指纹）。
- retention 以单条 DELETE 在同事务精确裁剪（含超过旧 500 行批次的策略收紧场景）；过期默认不可见、按策略可审计读取（`history_unavailable` 语义）；PUT completed replay 重新授权且 Tombstone 优先；`state.projection` coalescable 投影 Job（指针不变量检查 handler）。

### 3.3 FocusItem（已完成）

- 7 kinds × 5 statuses 全实现；非法迁移稳定 `invalid_state_transition`（合法性检查先于参数检查）。
- 容量（item 数/kind 配额/token budget）以确定性驱逐（lowest activation → dormant，永不删除）；衰减为 `(base, last_activated)` 纯函数（幂等 maintenance、不复合、不提 confidence、不动来源）；activate 有界增强、retrieved/returned 不增强。
- Promotion seam：仅记录 revision/目标类型（id 留空待 Phase 4/5 回填）+ 审计事件，不创建 Note/Task/Episode/Claim；`affect` 无任何 persona 写路径。
- `focus.maintenance` 经 Schedule/Tick 持久调度 + 幂等 handler；有界批次按 active → 过期 dormant → `updated_us` 轮转，超过 500 个永不删除的 dormant item 不会饿死较新的 active 工作集。

### 3.4 结构化 Recall（已完成，内部骨架）

- `application/recall.py`：仅 recent_context/state/focus 三条 Route（`RecallRoute` port 注入，Phase 6 可扩展）；请求级 Agent/Space/Session 授权先于 watermark 早退；每 Route 子 deadline（I/O 前后协作检查 + Orchestrator port 边界权威复核）+ 候选上限；completed/degraded/partial Envelope（稳定原因码 + retryable + fallback；partial 至少有一个关键 Route 成功，全降级稳定 `not_ready`）；统一 Candidate 转换（deterministic candidate_id）；版本化排序器；Layer/Token Budget；最终 Canonical Rehydrate；minimum watermark 不可达时稳定 `not_ready`；safe trace；授权失败请求级抛出不降级；Persona 非候选。
- **Phase 6 边界**：不实现 FTS/Vector/Graph/Provider/Recall Cache/Usage Report，不冻结 `/v1/recall` 协议。

## 数据、契约与回退策略

> 本节记录的是**已落地**结果，不是计划。

- `migrations/0004_phase3_recent_state_focus.sql`（online_safe=true，lock_ms=200，min_app=0.4.0，recovery=none；SHA-256 `c6174cbefd745d371d72f75a4e6adaeeb856dad780370e8c133ae4806a23ee9a`），8 张 STRICT 表；0001–0003 与 HEAD `8041552` 逐字节一致。
- Schema 3→4 在线升级（真实 Phase 2 数据升级测试通过）；runtime 兼容窗口 [3,4]；Schema 2 库经 0.3.0 二进制分阶段前移（MigrationRunner 可一次走完 2→4，窗口只约束 Ready）。
- Builder 升级路径：新 builder_version 影子重建（构建→校验→单事务原子切换）；失败读取上一已验证 generation 或 Canonical 窗口；旧二进制遇未知 builder_version 停用该投影并重建，不删 Revision 历史。
- 备份恢复不变量扩展（state/focus 指针解析、recent 指针→同 target verified generation、projection hash/结构、generation ref 的 observation 身份与 revision/time 一致）；Phase 1/2 备份路径保留（Phase 1 快照仍可恢复）。
- 契约 add-only：contract 1.1.0→**1.2.0**、schema 3→**4**、package **0.4.0**；新增 `recent-context.v1`、`state.v1`、`focus-items.v1` capability；新路径 `/v1/recent-context`、`/v1/state/{namespace}/{key}`（PUT/GET）+ `/v1/state` + history、`/v1/focus-items`（POST/GET/list + `:activate/:dormant/:dismiss/:expire/:promote`）、`/v1/admin/recent-context:rebuild`（admin+权限测试）；fixtures 21→35；双 SDK 校验器与客户端方法同步；无 FastAPI 传输层（沿用应用层契约 + mock server 模式）。

## 量化验收基线

> 下列数字为**实测值**，口径见验证报告。

- **State Coalesced Write p95 ≤ 25ms**：实测 **4.69ms**（顺序 coalesced 流、payload 121B、库内 2000 行 corpus 与 2000 pending 投影 Job；p50 3.69ms；`tests/performance/test_state_latency.py` 输出含环境行）。另如实报告 8 并发突发口径 p95 51.17ms——该数字度量的是进程内单 Writer Gate（§20.2）的队头阻塞而非写入路径本身；busy 事件 0。
- **Recent 连续重建 3 次一致**：同一 Observation 集/Watermark/Builder/Estimator 下 refs、segments、token estimate 与 result hash 三次全等（`test_three_rebuilds_are_identical`，且 builder 性质测试每条 200 固定种子案例覆盖）。
- **Focus 性质测试每条 ≥200 案例**：容量驱逐/状态机/衰减纯函数/幂等/score/summary/clamp 全部以 `CASES = 200` 固定种子驱动（`tests/unit/test_phase3_domain.py` + `test_focus_items.py`）。
- **故障注入**：新增 10 个边界（state_put 4、focus_transition 2、recent_rebuild 4）× 20 次 = **200 次**（累计 Phase 2 的 140 次之外）；恢复后无悬空 pointer、无未提交 observation ref、无跨 Space 内容、无过期对象参与 Recall，重放收敛。
- **Structured Recall 重放/超时**：相同输入+Canonical 快照 100 次重放顺序与裁剪完全一致；每条路由超时场景 20 次，Envelope 准确标记 completed/degraded/partial 与稳定原因码。

## 退出门禁

- [x] Recent Context 仅引用 committed Observation，Builder 版本变化可完整重建。  
  （`test_reads_only_committed_observations`、`test_three_rebuilds_are_identical`、`test_builder_version_mismatch_falls_back_to_canonical`、`test_ordering_independent_of_input_order`；`tests/unit/test_phase3_domain.py::TestRecentBuilderProperties` 4×200 案例）
- [x] State 高频写保留所有要求的 Canonical Revision，Coalescing 不改变最终值。  
  （`test_coalescing_merges_projection_jobs_not_revisions`：投影 Job 合并、5 个 revision 全在、watermark/audit 齐全；`test_fifty_threads_one_winner`：50 并发同 Expected Revision 恰一成功、49 稳定 `revision_mismatch`；`TestPhase3RestoreInvariants` 指针不变量）
- [x] Focus 容量、Kind 配额、衰减、激活和状态转换性质测试通过。  
  （`TestCapacityProperties`（含 200 案例 item cap）、`test_kind_quota_evicts_within_kind`、`test_token_budget_evicts_until_fit`、`TestDecayAndTtlProperties`（200 案例 idempotent-and-pure）、`test_ttl_expiry_via_maintenance`、`TestCreationAndTransitions`、`test_only_explicit_activation_boosts`）
- [x] 重启后 Current Pointer、TTL、Focus 和投影恢复正确。  
  （`tests/fault/test_kill9.py::TestStatePutKill9/TestFocusTransitionKill9/TestRecentRebuildKill9`：10 边界 ×20；`test_backup_restore_roundtrip_keeps_phase3_rows` ×3 轮；handler 重放幂等 `test_handler_is_idempotent_across_replays`）
- [x] Space 私有 Recent 内容不会通过 Focus/Recall 默认跨端泄漏。  
  （`test_space_b_never_sees_space_a_content`（20 次泄漏计数=0）、`test_space_group_membership_does_not_leak_raw_content`、`test_cross_space_recall_zero_leak`、`test_space_boundaries`、`test_space_group_boundaries`；窗口查询为结构性 WHERE 隔离）
- [x] Structured Recall 的稳定排序、Deadline 和降级 Envelope 测试通过。  
  （`test_hundred_replays_identical_order_and_trimming`（100 次）、`test_stable_sort_key_tie_breakers`、`test_each_route_timeout_degrades_accurately`（3 路由 ×20）、`test_route_exception_degrades_route_only`、`TestMinimumWatermark`、`TestRehydrate`、`TestSafeTrace`；审查加固后 `TestFailClosed`：负数上限拒绝、严格 partial、全降级稳定错误、I/O 跨 deadline 拒绝 completed）
- [x] Migration/Builder 兼容/回退方案、需求追踪和交付证据已完成评审。  
  （`tests/integration/test_migrations_phase3.py`：0001–0003 对 HEAD 基线逐字节校验、Phase 2 真实数据 3→4、空库=4、伪造指针拒绝、Phase 1 备份仍可恢复；ADR-0011；[验证报告](../reports/phase-03-verification.md)）

## 交付证据

- 代码/变更：Phase 3 实现提交。核心新增：`domain/{recent,state,focus}.py`、`storage/cognitive.py`（三仓储 + generation/pointer/revision CAS）、`application/{recent,state,focus,recall}.py`、`jobs/handlers.py` + `phase3_handlers`；扩展 `storage/uow.py`、`application/ports.py`、`storage/backup.py`（Phase 3 不变量）、`storage/runtime.py`（窗口 [3,4]）、`domain/jobs.py`（启用 4 个已测 handler + `state.projection`）、契约生成器/mock server/双 SDK。
- ADR：[ADR-0011](../adr/0011-phase3-recent-state-focus.md)（generation/pointer 结构与 NULL-free 键、State 幂等指纹与合并流、Focus 纯函数衰减模型与容量驱逐、Recall 骨架边界与授权语义、Scope 语义应用、Job 启用推进；审查加固：by-ID 信封授权与列表空值语义（§5）、Focus 变更幂等与写事务内状态机权威校验（§7）、Recall fail-closed 语义（§8）、契约自洽与 Scope 参数完备性（§9)）。
- Schema/Migration：`migrations/0004_phase3_recent_state_focus.sql`（SHA-256 `c6174cbefd745d371d72f75a4e6adaeeb856dad780370e8c133ae4806a23ee9a`），8 张 STRICT 表；Schema 3→4 在线升级；窗口 [3,4]；0001/0002/0003 与 HEAD `8041552` 逐字节一致。
- 测试/性能报告：[phase-03 验证报告](../reports/phase-03-verification.md)（审查与复审加固后最终单次 `make ci` 619 passed / 87.78%、Kill-9 新增 200 次、50 并发 CAS、p95 实测、契约与泄漏扫描证据、原 9 项审查缺陷及 4 组复审缺陷的修复与回归测试清单）。
- 已知限制：见验证报告"已知限制"节——Space 窗口的严格空值语义、State 历史按策略裁剪、Recall 骨架无 `/v1/recall` 契约（Phase 6）、单 Writer Gate 下的并发突发排队、应用层契约模式（无 FastAPI）。

## 明确不做

- 不实现 Note/Task、长期 Claim、FTS 或向量召回。
- 不用自由文本摘要代替原始 Observation 或作为独立 Evidence。
- 不把 Working/Focus 当作无限增长的第二份聊天历史。

## 交接条件

Phase 4 可以依赖稳定的短期读取 Route、Focus Promotion seam（target type + 待回填 id + 审计事件）、可注入时间和 Schedule Job Kind；Phase 6 可以复用 Recall Orchestrator 骨架（RecallRoute port、Envelope、rehydrate、预算与 deadline 语义）。
