# 阶段 2：Observation、Outbox 与持久调度

> 状态：Completed（含独立复核修复记录，见下）  
> 前置阶段：[阶段 1](./phase-01-persistence-identity-scope.md)  
> 目标版本：0.3.0（Schema 3）· 完成日期：2026-08-30 · 复核修复：2026-08-30  
> 架构依据：[§8 Observation](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#8-observation-journal)、[§16 Outbox](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#16-transactional-outbox-与-worker)、[§17 Schedule/Tick](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#17-持久化-scheduletick-与认知时钟)、[§25 Active Surface](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#25-可选-active-surface-coordinator)、[§36 阶段 2](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#阶段-2observationoutbox-与持久调度)、[ADR-0009](../adr/0009-phase2-reliability-spine.md)、[ADR-0010](../adr/0010-active-surface-coordinator.md)

## 阶段目标

打通“真实外部效果 → Canonical Observation → 可靠异步任务”的持久化脊柱，建立不依赖进程内 Timer 的认知时钟，并补齐宿主接入需要的可选 Active Surface Lease/Epoch。阶段结束时，重复、宕机、旧 Worker/Holder、时钟跳变和队列背压均不能破坏事实正确性。

## 架构约束

- Core 只接受已确认生效的用户、助手、工具或外部事件；失败尝试只能进入 Audit/Event。
- Observation、Source Cursor、Agent Watermark 和 Outbox 必须在同一 SQLite 事务提交。
- Worker 提交前验证 Owner、Lease Generation、Expiry 和 Source Revision。
- Tick Ledger 是周期执行事实源；进程内 Timer 只负责唤醒。
- Observe 在线路径不得调用 LLM、Embedding 或大型投影任务。
- Active Surface Coordinator 是可选控制平面，不是 Scope、Privacy、Revision 或并发正确性的前提。

## 需求追踪

| 需求 ID | 基线要求 | 工作包 | 验证门禁 |
| --- | --- | --- | --- |
| P2-OBSERVE-01 | 实际效果、Batch、Source Stream/Cursor 与原子 Watermark | 2.1 | 重复、Gap、乱序、Crash 测试 |
| P2-OUTBOX-01 | Transactional Outbox、Lease Generation、Fencing 与 Dead Letter | 2.2 | Kill-9、旧 Worker、重放测试 |
| P2-PRESSURE-01 | 容量、公平调度、磁盘与安全优先通道 | 2.3 | 软/硬阈值和 Ready 测试 |
| P2-CLOCK-01 | Persistent Schedule/Tick、时区、Misfire 与时钟异常 | 2.4 | DST、前跳、回拨、Catch-up 测试 |
| P2-OBSERVABILITY-01 | 低敏日志、指标与健康检查 | 2.5 | 泄漏扫描与健康契约测试 |
| P2-SURFACE-01 | Off/Advisory/Required Lease、Epoch 与旧 Holder Fencing | 2.6 | 并发抢占、重启和模式矩阵测试 |

## 工作包

### 2.1 Observation Journal

- 实现单条/批量 Observation、Artifact 引用占位、`effect_state=committed|partial` 和实际效果校验。
- 实现 Source Stream/Cursor 的单调推进、重复、Gap Policy 以及和 Idempotency 的独立语义。
- `POST /v1/observations:batch` 返回 accepted/duplicate IDs、Source/Agent Watermark 和 Outbox 状态。

### 2.2 Transactional Outbox 与 Worker

- 实现 Outbox 领取、优先级、Generation Fencing、Retry/Backoff、Dead Letter 和安全重放。
- 对 State/Profile/Graph 类任务支持保留最高 Source Revision 的 Coalescing；明确禁止 Observation、Forget、Task Occurrence 等丢弃式合并。
- Worker 只能通过 Application/Domain Service 提交结果，不能直接改 Current Pointer。

### 2.3 背压与故障语义

- 对队列数、Payload 字节、Tenant/Agent、公平调度、Worker 批量和磁盘余量设软/硬限制。
- 保留 Forget、Correct 和安全操作的优先通道；硬阈值返回 `storage_full`。
- 健康信息显式报告 Queue Lag、Oldest Pending、Dead Letter 和不可写状态。

### 2.4 Schedule、Tick Ledger 与 Clock

- 实现受限 Schedule、IANA 时区、Catch-up/Misfire Policy、Occurrence Key 和 Tick/Outbox 原子写入。
- 使用可注入 Wall/Monotonic Clock 处理 DST、前跳、回拨、休眠和重启补算。
- 建立后续 Phase 使用的周期 Job Kind 注册表，但只启用已有安全 Handler。

### 2.5 可观测性

- 增加 Observation、Outbox、Schedule、SQLite Busy、磁盘与 Worker 指标。
- 日志和 Trace 只记录 ID Hash、数量、Revision、错误码和耗时，不记录正文。
- 扩展 `/health/live` 与 `/health/ready` 的 Scheduler/Worker Lag 判断。

### 2.6 可选 Active Surface Coordinator

- 实现 Agent 级唯一 Lease、Acquire/Preempt/Heartbeat/Release/Expiry、单调 Epoch 和旧 Holder Fencing。
- 实现 `off|advisory|required` 策略与 `lease_held|lease_expired|lease_fenced` 错误；任何模式都不改变数据授权语义。
- 划分应用在线平面与管理/维护平面：Required 模式校验在线 Observe/Recall/认知操作，内部 Worker、备份和 Persona 管理不冒充活动宿主。
- 抢占先 Fence 旧 Holder，再发出撤销通知；Coordinator 故障不得损坏 Canonical 数据。

## 数据、契约与回退策略

- Observation、Cursor、Watermark、Outbox、Schedule、Tick、Lease 和 Epoch 表通过可向前兼容的增量 Migration 引入；新字段先允许旧 Worker 忽略，再切换必填约束。
- Worker Job Payload 和事件使用显式版本；滚动部署期间新 Worker 可读取上一版本，旧 Worker 不领取未知 Job Kind。
- Coordinator 从 `off` 开始部署，完成 Epoch/Fencing 验证后才能切换 `advisory` 或 `required`；回退到 `off` 不删除 Lease 历史。
- Schema 回退遵循 Phase 1 的备份/兼容二进制策略；Outbox、Tick 和 Cursor 记录不得通过 Down Migration 删除或回拨。

## 量化验收基线

- 单条 Observation p95 ≤ 30 ms，100 条 Batch p95 ≤ 150 ms；报告声明硬件、并发、Payload 和数据库状态。
- 每个事务边界和 Worker 提交边界的 Kill-9 场景至少重复 20 次，逻辑效果不得丢失或重复。
- 50 个并发 Worker/Holder 的抢占测试中，过期 Generation/Epoch 的提交成功数必须为 0。
- 时间测试至少覆盖 UTC、一个有 DST 的正时区、一个有 DST 的负时区和一个无 DST 的 IANA 时区，并覆盖前跳、回拨和重复时刻。
- 软/硬队列和磁盘阈值必须配置化；达到硬阈值时普通写稳定返回 `storage_full`，Forget/Correct 安全通道仍可用。

## 退出门禁

- [x] Batch 请求级校验保持原子性，重复 Event/Cursor/Idempotency 不产生重复事实。
  （`tests/integration/test_observations.py::test_invalid_record_zero_writes`、`TestDuplicateIdentities`：同键同指纹→duplicate、同键异指纹→`idempotency_key_reused`、重复 event/cursor/occurrence 返回既有事实且 outbox 零新增）
- [x] 在事务前、事务后、Worker 执行中和提交前 Kill -9，恢复后无丢失或重复逻辑效果。
  （`tests/fault/test_kill9.py`：7 个参数化边界 × 各 20 次 = 140 次真 SIGKILL 子进程注入；观察/游标/水位/outbox、Worker 业务结果+完成 CAS、Tick+Outbox 三组联合不变量在恢复后均成立，重放收敛到恰好一份事实）
- [x] Lease 过期后的旧 Worker 即使完成计算也无法提交。
  （`test_outbox.py::test_expired_lease_cannot_complete_even_after_success`、`test_stale_generation_cannot_complete`、`test_stale_owner_cannot_complete`、`test_stale_source_revision_cannot_complete`；50 Worker 竞争下过期 generation 提交成功数恒为 0：`test_50_concurrent_workers_stale_commits_are_zero`、`test_true_threaded_50_worker_race`）
- [x] Active Surface 并发 Acquire/Preempt/Heartbeat、旧 Epoch、重启重新获取及 Off/Advisory/Required 测试通过。
  （`tests/integration/test_surface.py`：模式矩阵 `TestModeMatrix`、50 Holder 并发 `test_50_concurrent_holders_exactly_one_epoch_wins`、50 轮旧 epoch 提交 0 次、回退 off 保留历史）
- [x] Cursor 对账、Gap、乱序和 Crash 恢复测试通过。
  （`test_observations.py::TestCursorSemantics` + Kill -9 观察场景恢复重放）
- [x] DST、时钟回拨/前跳、休眠、Catch-up 上限和重复 Occurrence 测试通过。
  （`tests/unit/test_phase2_domain.py::TestDstOccurrenceMath`：UTC/Europe-Berlin/America-New_York/Asia-Tokyo 矩阵、春季缺失 skip/postpone、秋季重复 first/second；`test_scheduler.py::TestCatchUpPolicies` 四策略+grace+上限、`TestTickLedger` 回拨/重启不重发）
- [x] 磁盘/队列背压时 Ready、错误码和安全优先通道符合契约。
  （`tests/integration/test_backpressure_health.py`：软/硬队列与字节阈值、租户配额、磁盘三态+滞回、safety lane 存活且自带受限配额、Ready 报告 lag/oldest/dead-letters/不可写）
- [x] Migration/兼容/回退方案、需求追踪和交付证据已完成评审。
  （`tests/integration/test_migrations_phase2.py`：0001/0002 字节校验和不变、真实 Phase 1 数据 Schema 2→3 升级、空库安装、备份→恢复→冒烟 ×3 轮、Phase 2 恢复不变量拒绝伪造态；`docs/reports/phase-02-verification.md`）

## 交付证据

- 代码/变更：Phase 2 实现提交。核心新增：`domain/{observation,jobs,schedule,surface}.py`、`storage/spine.py`（四仓储+fencing CAS）、`application/{observation,outbox,scheduler,surface,backpressure,health}.py`、`jobs/worker.py`、`coordinator/`、`observability/{metrics,logging}.py`；扩展 `storage/uow.py`（busy 指标钩子）、`storage/backup.py`（Phase 2 不变量）、`storage/runtime.py`（窗口 2..3）、契约生成器/mock server/双 SDK。
- ADR：[ADR-0009](../adr/0009-phase2-reliability-spine.md)（观察身份语义/整数游标/四重 fencing/Coalesce 白名单/补算边界/Job Kind 启用策略）、[ADR-0010](../adr/0010-active-surface-coordinator.md)（Lease/Epoch/模式矩阵/先 fence 后通知）。
- Schema/Migration：`migrations/0003_phase2_reliability_spine.sql`（online_safe=true，lock_ms=200，min_app=0.3.0，recovery=none；SHA-256 `a64c8f17637bff3375aba0fe05f3ac7227ef6ebc22f73c325dba8fd81a1ccf1f`——含复核修复引入的 tick `failed` 终态 CHECK），8 张 STRICT 表；Schema 2→3，二进制窗口 [2,3]；0001/0002 校验和与 HEAD 一致。
- 故障测试报告：[phase-02 验证报告](../reports/phase-02-verification.md)（Kill -9 ×140、性能实测、并发 fencing 零成功、契约与泄漏扫描证据、复核修复记录）。
- 已知限制：见验证报告"已知限制"节——整数游标、interval/daily 语法子集、背压滞回为进程内状态、仅启用 selfcheck 种子 Handler、API 仍为应用层契约模式（无 FastAPI 传输层）。

## 独立复核修复记录（2026-08-30）

独立代码复核结论为 Changes Requested，列出 3 项 P0、4 项 P1 与若干次要缺陷；以下修复全部落地，且每项均配套**对修复前实现必然失败**的回归测试（`tests/integration/test_review_regressions.py`、`tests/integration/test_surface.py`、`tests/contract/test_mock_server.py`）：

| 复核发现 | 级别 | 修复 | 回归测试 |
| --- | --- | --- | --- |
| Observation 可引用未授权 space/space_group/session、跨租户 space group、伪造 actor entity | P0 | `_check_container` 强化为租户归属 + allowed 集合双重校验；新增 `_check_actor` 身份解析（verified binding + redirect 终点比对 + 墓碑拒绝） | `TestObservationAuthorization` 5 例 |
| Active Surface acquire 无 Agent 授权校验、holder 可伪造、required 模式不出示租约即通过、过期/draining 租约可 release、`lease_held` 实际错误码为 conflict | P0 | acquire/heartbeat/release 携带 `AccessContext`，holder 绑定认证 app instance，holder space 授权；`check_online` 在 required 下强制请求出示 lease_id+epoch（缺一即 `lease_expired`+`missing_lease_proof`）；release CAS 仅接受存活 active 租约；改用域内 `LeaseHeldError`（稳定码 `lease_held`） | `TestAcquireAuthorization` 6 例、`test_fenced_holder_cannot_release`、`test_expired_lease_cannot_release`、`test_required_demands_presented_proof`、`test_second_acquire_without_preempt_is_lease_held`（断言 `.code == "lease_held"`） |
| `list_jobs` 可跨租户读取 | P0 | `_same_tenant` 强制同租户 | `TestAdminJobListing` |
| 背压不投影 payload bytes、Agent bytes 恒为 0、recovery_hysteresis/worker 并发与租约上限未使用、满队列时同 dedupe 重试误报 `storage_full`、Scheduler/Surface/Observe 绕过统一入口 | P1 | 新增统一入口 `enqueue_with_pressure`（全部生产者复用）；观测批以事务内真实落库后的压力判定（新任务数+实际字节，全重复重放不受阻）；Agent 维度 bytes+新配置 `max_pending_bytes_per_agent`；滞回改为 hard→hard+hysteresis；worker 并发上限入 `OutboxWorker`（claim 批量钳制）+ `max_worker_leases` 每 owner 租约余量钳制 | `TestBackpressureProjections` 6 例、`TestWorkerCeilings` 2 例 |
| Kill -9 测试接受退出码 0、watermark 断言含 `or True`、`post_work_pre_commit` 实际死于 claim 提交点 | P1 | 子进程退出码必须为 `-9`；watermark/audit 断言改按 `aggregate_type='observation'` 精确计数（前 0 后 1）；die 钩子在 claim 提交**之后**挂载，业务写入与完成 CAS 同事务被杀 | `tests/fault/test_kill9.py` 全部 7 边界（140 次注入重跑） |
| 幂等 fingerprint 缺 `committed_us`/`actor_entity_id_at_ingest`；应用层与 Python SDK 接受 `"01"` 游标 | P1 | fingerprint 覆盖全部调用方可变字段；应用层改用 `validate_cursor`（fullmatch），Python SDK 与 TS/JSON Schema 对齐 | `TestIdempotencyIdentity` 3 例 |
| retry/dead 无 source_revision fencing；重放包装 payload；调度任务死信不落 tick failed | P1 | `mark_retryable`/`mark_dead` 补四重谓词；重放逐字节沿用原 payload+原 payload_version；死信任务将 tick 置 `failed`（0003 CHECK 允许该终态） | `TestOutboxFencingSurface` 4 例 |
| OpenAPI admin 路径无 mock/契约覆盖；focus.maintenance catch-up 应为 coalesce；日志白名单不校验值；指标未接入生产路径；Ready 无真实可写探测 | 次要 | mock server 实现 4 条 admin 路径 + 双 SDK admin 客户端方法 + 4 例契约测试；注册表 catch-up 改 coalesce；日志清洗器对数值字段做类型强制（字符串→hash）；Observation/Outbox/Scheduler/Surface 挂接指标钩子；Readiness 执行真实写探测（失败→`storage_not_writable`/not_ready） | `test_admin_*` 4 例、`TestRegistryAndLogging` 2 例、`TestMetricsWiring` 4 例、`TestReadinessWriteProbe` |

修复后全量门禁：`make ci` 单次完整通过（exit 0），406 tests passed，覆盖率 88.47%（阈值 80；两次复跑 88.46%/88.47%）；0001/0002 校验和与 HEAD 逐字节一致。

## 第二轮独立复核修复记录（2026-08-30）

第二轮独立复核维持 Changes Requested，列出 3 项 P0、4 项 P1 遗留缺陷；以下修复全部落地，每项配套对修复前实现必然失败的回归测试：

| 复核发现 | 级别 | 修复 | 回归测试 |
| --- | --- | --- | --- |
| Observation 可跨 Agent 关联 Space（授权 A、B 两 Agent 的上下文把 A 的观察写入 B 的 space）；session 路径同样缺失 | P0 | `_check_container` 增加 agent 归属校验：agent-owned space 只接受归属 Agent 的记录（未指定 agent 的租户共享 space 不受限）；session 经所属 space 执行同一规则 | `TestObservationAgentBoundary` 2 例 |
| 无 external identity 时可任意声明同租户 Entity 为 actor | P0 | `actor_entity_id_at_ingest` 必须伴随 `actor_external_identity_id`（§6.4：服务端从外部身份解析，不信任调用方内部 ID）；请求级拒绝 `invalid_request`，零写入 | `test_actor_entity_without_identity_is_not_trusted`（含真实存在实体与不存在实体两种输入 + 零写入断言） |
| Surface holder space 不校验 Agent 归属；撤销 space grant 后仍可续租/释放；`current()` 仅租户校验即可读租约 | P0 | acquire 的 holder space 校验补 agent 归属；heartbeat/release 重演 holder space 全套授权（撤销 grant 立即拒绝且不改变租约状态）；`current()` 增加 Agent grant 校验 | `TestSurfaceAgentBoundary` 3 例 |
| 满队列时同 coalesce key 新 dedupe key 本应合并却 `storage_full`；leased coalescable 行的精确 dedupe 重试误报 `storage_full`；`replay_dead_letter` 绕过背压（队列 3→4） | P1 | 统一入口预查完整镜像仓储结果：dedupe 命中未决行、leased 行逐字节精确重试（吸收，不建冗余 follower）、可合并 kind 命中 pending 目标（合并）均不参与压力判定；真正新增行的入队照常全额判定；重放与普通入队共用同一压力投影 | `TestBackpressureCoalesceAndReplay` 3 例（含"不同 payload 仍被诚实拒绝"与"腾出余量后重放成功"的反向控制） |
| Kill -9 恢复用 no-op handler 且断言 `business_results == 0`，未证明恢复后无丢失 | P1 | 崩溃与恢复共用同一业务 closure（`business_result_work`）；恢复后断言 `completed_jobs == 1` **且** `business_results == 1`——恰好一次逻辑效果，崩溃尝试的写入被回滚后由恢复重放补齐 | `test_worker_recovery` 两边界 ×20 次注入 |
| Coordinator 故障无稳定错误码（`domain_error`；OSError 直接逃逸） | P1 | 域内新增 `NotReadyError`（稳定码 `not_ready`，契约码已注册）；`check_online` 捕获 `OperationalBusyError`+`OSError` 统一映射；删除未使用的 `LeaseCoordinatorUnavailable` | `test_coordinator_failure_leaves_canonical_intact`（直接断言 `.code == "not_ready"` + 观察路径零写入） |
| `expire_stale` 只改状态不记 `expired` 事件 | P1 | 清扫在同一事务内为每条 lapsed 租约追加 `expired` 事件行（actor `coordinator:expiry_sweep`），与 ADR-0010 append-only 声明一致 | `test_expired_lease_records_expired_event` |

第二轮修复后全量门禁：`make ci` 单次完整通过（exit 0），416 tests passed（+10），覆盖率 88.94%/88.78%（两次完整实测）；0001/0002 校验和与 HEAD 逐字节一致（0003 本轮未变更）；语义决策同步 ADR-0009（actor 归属、coalesce 精确重试吸收、重放背压）与 ADR-0010（holder space 归属、heartbeat/release/current 再授权、过期记账、not_ready 映射）。

## 第三轮独立复核修复记录（2026-08-30）

第三轮独立复核确认第二轮 7 项全部关闭，新复现 1 项 P0、2 项 P1 边界；以下修复全部落地，每项配套对修复前实现必然失败的回归测试：

| 复核发现 | 级别 | 修复 | 回归测试 |
| --- | --- | --- | --- |
| Coalesce 合并替换更大 payload 却不参与背压：41 字节硬上限被 2024 字节合并穿透（"不新增行"被误当"不新增压力"） | P1 | 压力投影改为入队真实足迹：合并路径按 `max(新 payload 字节 − 旧 payload 字节, 0)` 投影 bytes 增量（revision 更老不替换、增量为零）；超限整体 `storage_full` 回滚，目标行原样保留；腾出余量后同一合并成功且目标行携带新 payload/新 revision | `test_coalesce_merge_byte_growth_counts_against_byte_limit`（超限拒绝 + 行数/内容不变 + 余量下合并成功） |
| Leased 行同 dedupe key 非精确重试无法生成 follower：仓储走 `_coalesce_or_insert` 以原 dedupe key INSERT，撞 `UNIQUE(tenant, dedupe_key)` 抛偶然的 SQLite `conflict`（即使队列有余量） | P1 | 固化语义为"dedupe key 命名一份内容"（ADR-0009 §4）：同键 canonical payload 逐字节相同 → 任何状态吸收返回既有行（含 leased，revision 差异只是元数据）；同键不同 payload 命中未决行 → 稳定 `idempotency_key_reused`（应用投影层与仓储双层一致，与 Observation 同键异指纹语义对齐）；更新内容必须换新 dedupe key——这正是 leased 行背后 follower 的创建方式。拒绝"派生 follower key"方案：一个逻辑键映射多行破坏去重意义 | `test_leased_dedupe_changed_payload_with_headroom_is_key_reuse`（有余量 + 稳定错误码 + 队列不变 + 新键创建 follower）与 `test_plain_dedupe_changed_payload_is_key_reuse`；`test_exact_retry_of_leased_coalescable_job_is_absorbed` 更新断言 |
| Observation 的 Scope 层级关系未校验：`session_id` 单独出现被接受；`space_group_id + space_id` 互不相关的组合被接受——会持久化无法构造合法 Scope 的记录、产生错误组级归属 | P0 | 请求级结构规则：`session_id` 非空必须携带 `space_id`（`invalid_request`，整批零写入）；`_check_container` 增加绑定校验：group+space 必须命中 `space_group_bindings` 的当前活跃绑定或覆盖 `occurred_us` 的历史绑定（解绑保留发生时归属，§5.3）；`spaces.space_group_id` 列不是成员关系事实来源。契约 JSON Schema 补 `dependentRequired`（session_id→space_id）+ 新增 invalid fixture + Python/TS SDK 校验器与契约 mock 同步 | `TestObservationScopeHierarchy` 3 例（session 无 space 整批拒绝零写入；绑定错配拒绝 + 正确绑定/仅组/仅空间通过；历史绑定按 occurred_us 判定正反两向）+ 契约 mock `test_observe_batch_rejects_session_without_space` |

第三轮修复后全量门禁：`make ci` 单次完整通过（exit 0），423 tests passed（+7），覆盖率 88.98%/88.82%（两次完整实测）；0001/0002/0003 字节复核未变；语义决策同步 ADR-0009（§4 dedupe 内容语义与 follower 路径、§7 真实足迹投影、§8 Scope 层级）。

## 第四轮独立复核修复记录（2026-08-30）

第四轮独立复核确认原三个阻断场景闭门，新发现 1 项 P1：

| 复核发现 | 级别 | 修复 | 回归测试 |
| --- | --- | --- | --- |
| pending/retryable + coalescable 同键异内容仍走原地合并（应用投影与仓储同病）：同 dedupe key 不同 payload 的 pending `focus.maintenance` 被接受且原行 payload 被覆盖；且该分支只检查新任务是否带 coalesce_key、不查既有行 kind 元组——跨 kind 同键碰撞（`focus.maintenance` 共用 `maintenance.selfcheck` 的 dedupe key）改写他 kind 行 payload，kind/payload 不匹配、新工作丢失 | P1 | 删除"同键异内容 + coalescable + pending → 合并"例外，与 ADR-0009 §4 对齐：dedupe 命中未决行先比 canonical payload，异内容**无条件** `idempotency_key_reused`（投影层与仓储双层一致）；合并只发生在 dedupe key 未命中后、经限定 `(tenant, agent, kind, coalesce_key)` 全元组的查找进入——dedupe 命中永不改写行，跨 kind 变异被结构性排除 | `TestDedupeContentOwnership` 3 例：pending 同键异 payload 拒绝且行原样；retryable 同键异 payload 拒绝且行原样；跨 kind 同键碰撞在**仓储层直接调用**下拒绝，kind/payload 不变 |

第四轮修复后全量门禁：`make ci` 单次完整通过（exit 0），426 tests passed（+3），覆盖率 89.07%/88.92%（两次完整实测，其一为 `make ci` 全量）；0001/0002/0003 字节复核未变；ADR-0009 §4（无条件 key reuse + 合并排序 + 跨 kind 结构性排除）与 §7（合并仅 dedupe 未命中一条路径）同步修正。

## 明确不做

- 不做 Episode/Claim 提取、Embedding 或 Provider 调用。
- 不把生成完成、发送失败、取消输出保存为助手已说事实。
- 不让 Scheduler 执行外部动作或自动标记 Task 完成。

## 交接条件

Phase 3 可以依赖幂等 Observation 流、固定 Source Revision/Watermark、可靠 Outbox、可注入时钟、持久 Schedule/Tick 和可选 Active Surface Lease/Epoch 机制。
