# Phase 2 验证报告：Observation、Outbox 与持久调度

> 结果：**通过（make ci exit 0）**  
> 日期：2026-08-30 · 实现基线：Phase 1 提交 `90097e0` 之上的 Phase 2 实现提交  
> 版本列车：Core/Python SDK/TypeScript SDK = **0.3.0** · Schema **3** · API v1 · Contract 1.1.0

## 1. 环境

| 项 | 值 |
| --- | --- |
| 硬件 | Apple Silicon（arm64, Darwin 25.5.0） |
| Python | 3.12.13（uv 管理） |
| SQLite（宿主绑定） | 3.50.4（测试以本地允许清单显式接入；生产允许清单不变） |
| uv / Node / npm | 满足 required-version（uv ≥0.11.29,<0.12；Node ≥22） |
| 数据库状态 | 每用例独立临时目录，WAL、synchronous=FULL、外键开启 |
| 并发 | 功能测试单线程；并发语义由 50 线程/50 Worker 专项测试覆盖 |

复现命令：`make ci`（= format-check + lint + import-boundary + docs + mypy strict + contracts-check + pytest + SDK typecheck/test）。

## 2. 门禁实测

| 检查 | 结果 |
| --- | --- |
| ruff format --check | 82 files already formatted |
| ruff check（E/F/B/UP/SIM/RUF） | All checks passed |
| Domain import boundary | passed |
| docs 结构与本地链接 | passed |
| mypy --strict（src+sdk+tools+tests，81 文件） | no issues |
| 契约生成无漂移 + v1 兼容快照 | ok（additive only） |
| Python 测试 | **426 passed**（含 2 个 SDK fixture 用例；含四轮复核修复回归 61 例） |
| 覆盖率（branch，阈值 80） | **89.07%/88.92%**（四轮复核修复后完整实测两次，其一为 `make ci` 全量；三轮后 88.98%/88.82%；二轮后 88.94%/88.78%；一轮后 88.47%/88.46%；修复前基线 88.03%） |
| TS typecheck + node --test | pass 1 / fail 0 |

测试构成：Phase 1 回归 137 + SDK 2（全绿，其中 2 处断言随 Schema 前进合法更新 `[1,2]→[1,2,3]`），Phase 2 新增：单元 50、观察 34、Outbox 39、Scheduler 26、Surface 33、背压/健康/泄漏 24、迁移/备份 14、Kill-9 7（×20 次）、性能 2、契约 mock 14、复核回归 28 + 二轮复核回归 10 + 三轮复核回归 7 + 四轮复核回归 3。

## 3. 需求 → 实现 → 测试 → 实测结果

### 3.1 Observation Journal（P2-OBSERVE-01 / §8）

| 需求 | 实现 | 测试 | 实测 |
| --- | --- | --- | --- |
| 单条/批量、全有或全无 | `ObservationService.observe_batch`：事务前校验全部记录 | `test_invalid_record_zero_writes` | 任一记录非法 → observations/outbox/audit 全零 |
| 实际效果语义 | 无 pending/failed 枚举；partial 必须携带 `effect_proof.confirmed_range`；失败尝试只入 Audit | `test_partial_requires_effect_proof`、`test_failed_attempt_never_becomes_observation`、`test_audit_details_carry_no_content` | 通过；失败尝试零 Observation、审计无正文 |
| 五重身份独立 | UNIQUE 约束 + 服务层先查后写（同事务） | `TestDuplicateIdentities`（key/指纹、event、occurrence、cursor） | duplicate 返回既有 id，outbox 零新增；同键异指纹 `idempotency_key_reused` |
| Cursor 单调/回拨/gap | 整数游标（ADR-0009），`advance_cursor` 带 `WHERE excluded > current` | `TestCursorSemantics`（gap 三策略、回拨拒绝、首见任意、对账读取） | reject→`cursor_gap`；mark→审计 `observations.cursor_gap`；旧游标不覆盖 |
| 原子提交 | 单写事务：observations+cursors+audit+watermark+outbox | Kill -9 `observe` 三边界 ×20 | 未提交边界 → 全零；已提交 → 恰好一份；重放收敛 |
| 批量响应契约形状 | `BatchOutcome`（accepted/duplicate/source&agent watermark/outbox_enqueued/cursors/lease_warning） | 契约 fixtures + mock 往返 | fixtures 20 用例通过 |
| Scope/Privacy/Tombstone/跨租户 | agent∈access.agent_ids、space/session 归属、墓碑复检、标签语法 | `TestAuthorizationAndTombstones` | 通过（含横向拒绝） |

### 3.2 Transactional Outbox 与 Worker（P2-OUTBOX-01 / §16）

| 需求 | 实现 | 测试 | 实测 |
| --- | --- | --- | --- |
| 状态机 + 稳定去重 | `UNIQUE(tenant,dedupe_key)`、claim 递增 generation、heartbeat、retry（抖动指数退避）、dead、replay（新 id+replay_of，幂等） | `TestClaimAndComplete`、`TestRetryAndDeadLetter`、`TestDeadLetterReplay` | 退避逐次递增；重放返回同一未决行；原任务保持 dead |
| 四重 fencing | 完成 CAS：owner+generation+expiry+source_revision，rowcount≠1 → `lease_fenced` 并回滚业务 | 四个 stale 测试 | 旧 owner/generation/expiry/source_revision 提交成功数 = 0 |
| Worker 只经服务提交 | `OutboxService.execute`：work 在事务外（可重放外部效果），commit 闭包与完成 CAS 同事务 | Kill -9 `worker` 两边界 ×20 | 业务结果与完成状态原子：崩溃后两者皆无；恢复后恰一次 |
| Coalescing 白名单 | 仅 state/profile/graph 四 kind；leased 不合并建 follower；禁用 kind 传 key 即 invalid_request | `TestCoalescing` 5 例（正反向） | 白名单合并保最高 revision；禁用 kind 永不合并 |
| 公平调度/安全优先道 | claim 按 safety lane → priority → available_at，每租户配额 | `TestClaimFiltering` | safety 先领；租户 ≤ 配额且他租户不受饿 |
| 未知 kind/payload 版本 | claim 过滤启用 kind 与 `payload_version <= 当前` | `test_unknown_payload_version_stays_pending`、`test_worker_only_claims_handled_kinds` | 保持 pending，无人领取 |
| 50 Worker 并发 | — | `test_50_concurrent_workers_stale_commits_are_zero`、`test_true_threaded_50_worker_race`（50 线程） | 过期 generation 提交成功数 **0**；无任务丢失、无异常逃逸 |
| 外部可重放消息 | Handler 契约文档 + 消息键 `(job, generation)` | `test_external_effect_deduped_by_message_key` | 同键重复投递可被外部去重 |

### 3.3 背压与故障语义（P2-PRESSURE-01 / §16.5）

| 需求 | 实现 | 测试 | 实测 |
| --- | --- | --- | --- |
| 全局/租户/Agent 队列数与字节 | `BackpressureConfig` 全配置化；检查含投影值（本批+1/+bytes） | `TestQueueThresholds` | 第 limit+1 次拒绝，前 limit 次全部接受 |
| 磁盘软/硬/滞回 | 可注入 DiskProbe；trip 后须回软阈值之上 | `TestDiskThresholds` | 硬→`storage_full`；中间态 degraded；滞回两段验证 |
| safety 通道保留 | safety 配额只统计 safety 任务 | `test_safety_lane_survives_normal_lane_pressure`、`test_safety_lane_has_its_own_bounded_quota` | 普通道满时 Forget 可入队；safety 自身受限 |
| Ready 报告 | `HealthService.readiness`：writable、disk、queue lag、oldest pending、dead letters、scheduler lag、storage_error_code | `TestReadiness` + mock 契约 | 三态与字段形状符合契约 fixtures |
| 不吞任务/不回拨/不跳审计 | 拒绝即整批回滚；audit 与业务同事务 | 见 3.1/3.2 原子性 | 通过 |

### 3.4 Schedule、Tick 与时钟（P2-CLOCK-01 / §17）

| 需求 | 实现 | 测试 | 实测 |
| --- | --- | --- | --- |
| Tick+Outbox 原子 | 同事务 `record_tick`+`enqueue`+`attach_outbox` | `test_advance_records_tick_and_outbox_atomically`、Kill -9 `tick` ×20 | 两者同生同灭；恢复后恰一份 |
| Occurrence 唯一 | `UNIQUE(schedule,occurrence_key)`，key=`scheduled_at:policy_version` | 重启/回拨重跑三例 | 二次 advance 零新增 |
| 时区矩阵 | zoneinfo 纯函数 + IANA 校验 | UTC / Europe/Berlin（DST+）/ America/New_York（DST−）/ Asia/Tokyo（无 DST） | 本地时刻精确命中 |
| DST 缺失/重复 | `dst_missing=skip|postpone`、`dst_ambiguous=first|second` | 春季缺失两策略、秋季重复差 1h、柏林切换、东京全年无偏移 | 通过 |
| 前跳/休眠/Catch-up 上限 | 四策略+grace+cap+跳过记账 | `TestCatchUpPolicies`（all/latest/skip/grace/cap/续跑） | 上限外显式 skipped，续跑收敛 |
| 回拨防护 | next_tick CAS 且 SQL `MAX()` 不回退 | `test_next_tick_marker_never_moves_backwards` | 通过 |
| Job Kind 注册表 | 全 kind 登记；仅 `maintenance.selfcheck` 启用 | `test_only_safe_handlers_enabled`、`test_disabled_kind_rejected` | 未启用 kind 拒绝建 Schedule |
| 可注入时钟 | Wall=`Clock`、Monotonic 端口+`FixedMonotonicClock` | `TestMonotonicObservations` | tick 记录 wall 与 monotonic |

### 3.5 可观测性（P2-OBSERVABILITY-01 / §31）

| 需求 | 实现 | 测试 | 实测 |
| --- | --- | --- | --- |
| 冻结指标面 | `Metrics`：7 个 §31 名称；禁高基数标签；自由文本哈希 | `TestLeakScan` 指标子集 | 未知名/禁标签/错标签集均拒；kind 哈希后 exposition |
| 低敏日志 | 字段白名单 + 禁用字段丢弃 + 严格模式 | `test_log_sanitizer_drops_forbidden_fields` 等 | content/payload/request_id 不出现 |
| 自动泄漏扫描 | canary 全路径 + 静态扫描 | `test_no_canary_in_logs_metrics_errors_or_health`、静态扫描 | canary 不出现在 metrics/日志/错误/健康/outbox payload/audit |
| busy 指标挂钩 | `Store(busty_observer=…)` 于 BEGIN/COMMIT 重试路径计数 | 挂钩存在（覆盖由 Store 路径承担） | 通过 |

### 3.6 Active Surface（P2-SURFACE-01 / §25）

| 需求 | 实现 | 测试 | 实测 |
| --- | --- | --- | --- |
| 唯一 Lease/生命周期 | partial unique + acquire/heartbeat/release/惰性过期 | `TestAcquireHeartbeatRelease` | 过期释放槽位，epoch 单调 |
| 抢占先 fence 后通知 | 同事务 fence CAS→事件→`surface.lease_revoked` outbox→新租约 | `test_preempt_publishes_revocation_notice`（rowid 顺序断言）+ `TestPreemption` | 通知 payload 含被撤销租约；fence 先于新 acquired |
| 三错误码 | `lease_held`（含 holder 详情）/`lease_expired`/`lease_fenced` | 旧 holder 心跳、过期、同优先级 | 语义与 details 符合 |
| off/advisory/required | 模式矩阵 + 平面划分 | `TestModeMatrix`（9 例） | required 阻断 Observe、放行内部 Worker；advisory 仅告警；off 不校验 |
| Coordinator 故障隔离 | 服务只写自有表；required fail closed `not_ready` | `test_coordinator_failure_leaves_canonical_intact` | 观察被拒且 Canonical 零写入 |
| 回退 off 保历史 | 事件/租约 append-only | `test_rollback_to_off_keeps_lease_history` | 通过 |
| 50 Holder 并发 | — | `test_50_concurrent_holders_exactly_one_epoch_wins`、`test_stale_epoch_surface_commits_zero`（50 轮） | 恰一 active；旧 epoch 心跳成功数 **0** |

### 3.7 Migration/备份/契约（§20.7、§21、ADR-0006）

| 需求 | 测试 | 实测 |
| --- | --- | --- |
| 0001/0002 字节不变 | `test_published_bytes_unchanged`（对照 HEAD） | SHA-256 一致 |
| 真实 Phase 1 数据 2→3 | `test_phase1_database_upgrades_with_data_intact` | 数据/水位保留，8 张新表就位，runtime 可开 |
| 空库安装 | `test_empty_database_installs_all_three` | version=3 |
| 备份→恢复→冒烟 ×3 | `test_backup_restore_smoke_three_rounds` | 不量零违例、观察/tick 数据存活 |
| Phase 2 恢复不变量 | 伪造孤儿 tick/缺完成时间/epoch 回退均被拒 | 三类拒绝 |
| Phase 1 旧备份仍可恢复 | `test_phase1_backup_still_restores` | 通过 |
| 契约 add-only | `contracts-check` + `check_compatibility` | 通过；新错误码 5 个（storage_full、cursor_gap、lease_held、lease_expired、lease_fenced）+invalid_request 实体化 |
| 版本列车 | version-manifest + 三处 package 版本 | 全部 0.3.0 / schema 3 |

## 4. 量化验收基线证据

| 基线 | 实测 |
| --- | --- |
| 单条 Observation p95 ≤ 30ms | **1.57ms**（warmup 10、样本 60；median 1.08ms；512B content；`tests/performance/test_observe_latency.py`，输出含环境行） |
| 100 条 Batch p95 ≤ 150ms | **10.22ms**（median 9.39ms；库内 7000 观察与 7000 outbox 行时测得） |
| 每个事务/提交边界 Kill -9 ≥ 20 次 | 7 个边界 × 20 = **140 次**（子进程真 SIGKILL，退出码必须为 -9；`post_work_pre_commit` 边界的 die 钩子在 claim 提交之后挂载，被杀的是业务写入+完成 CAS 事务——复核修复后该证据才真实成立） |
| 50 并发 Worker/Holder 过期 Generation/Epoch 提交成功数 = 0 | Outbox **0**；Surface **0**（50 线程 + 50 轮两口径） |
| 时区矩阵 | UTC、Europe/Berlin、America/New_York、Asia/Tokyo + 缺失/重复/前跳/回拨/休眠/上限/重复 occurrence |
| 软/硬阈值配置化、硬阈值 storage_full、safety 存活 | 全部通过（3.3） |

## 5. Migration 证据

- `0003_phase2_reliability_spine.sql`，头部 `-- iris: online_safe=true lock_ms=200 min_app=0.3.0 max_app= recovery=none`。
- SHA-256：`a64c8f17637bff3375aba0fe05f3ac7227ef6ebc22f73c325dba8fd81a1ccf1f`（与库内 `schema_migrations` 记录一致；复核修复在 `schedule_ticks` 的 CHECK 中允许带 outbox 引用的 `failed` 终态——死信任务的 tick 显式落 failed，0003 尚未发布，字节修订合法）。
- 兼容窗口：SUPPORTED_SCHEMA_MIN=2，MAX=3（0.3.0 可读 Phase 1 库并前向迁移；越窗 Ready 拒绝 `schema_incompatible`）。
- 回退策略：遵循 Phase 1 备份/兼容二进制策略；Outbox/Tick/Cursor 记录不经 Down Migration 删除或回拨（phase 文档"数据、契约与回退策略"约束）。

## 6. 契约证据

- `contracts.json`：contract_version 1.0.0→**1.1.0**、schema_version 2→**3**、package_version **0.3.0**；capabilities 追加 observe.batch.v1、source-cursor.v1、outbox.jobs.v1、schedules.v1、active-surface.v1、health.readiness.v2、metrics.v1。
- OpenAPI 新路径：observations:batch、cursors、active-surfaces（acquire/heartbeat/release/current）、admin jobs（list/retry）、admin schedules（create/run）、/metrics（readiness 响应升级为 ReadinessReport）。
- 错误码仅追加（27 个，v1 基线 22 个全保留）；fixtures 6→21 用例（三轮修复追加 session-without-space invalid 用例；record schema 补 `dependentRequired`）；Python/TS 校验器与客户端方法同步；TS lockfile 版本随包更新。

## 7. 设计决策（本轮固化）

1. **应用层契约模式**（经确认）：Phase 2 以应用服务+contracts+mock server+双 SDK 交付 API 形状，保持零运行时依赖；真实 HTTP 传输层（FastAPI/Pydantic，§39 冻结栈）留给后续阶段引入时以新 ADR 记录。
2. ADR-0009/ADR-0010 全部决策（见 ADR 正文）。
3. 协议属性以只读 property 暴露仓储（协变），维持 Store↔UnitOfWork 结构化兼容与 ADR-0007 依赖方向。
4. `BatchOutcome` 在服务边界收敛 tuple（幂等快照 JSON 往返后类型稳定）。
5. 租约事件顺序以 rowid 断言（同一微秒内 created_us 相同，UUID 主键不可作序）。

## 8. 独立复核修复（2026-08-30）

独立复核（结论 Changes Requested）发现的 3 项 P0、4 项 P1 与次要缺陷已全部修复并配套可失败回归测试；明细见 [phase-02 文档的复核修复记录](../development/phase-02-observation-outbox-scheduler.md)。要点：

- **授权边界**：Observation 的 space/space_group/session 走租户归属+allowed 集合双重校验；actor entity 必须命中 verified binding（redirect 终点比对，接受合并链）；Surface holder 绑定认证 app instance、Agent 授权、holder space 授权；admin 任务列表强制同租户。
- **required 模式闭门**：请求必须出示 lease_id+epoch（缺一即 `lease_expired`，warning `missing_lease_proof`）；release 仅接受存活 active 租约（过期→`lease_expired`，draining→`lease_fenced`）；`lease_held` 为域内一等稳定码。
- **背压**：统一入口 `enqueue_with_pressure`（Observe/Tick/撤销通知/显式入队全部复用）；观测批以事务内落库后的真实压力判定；Agent 维度 bytes 与 `max_pending_bytes_per_agent`；滞回 hard→hard+hysteresis；dedupe 命中未决行不占队列压力（同 key 重试不再误报 `storage_full`）；worker 并发与 `max_worker_leases` 生效。
- **Kill -9 证据去假阳性**：退出码必须 -9；watermark/audit 断言按 `aggregate_type='observation'` 精确计数；`post_work_pre_commit` 的 die 钩子在 claim 提交之后挂载——被杀事务恰为"业务写入+完成 CAS"。
- **Outbox 状态机**：retry/dead 补 source_revision 四重谓词；死信重放逐字节沿用原 payload；死信任务将对应 tick 落 `failed`（0003 CHECK 修订）。
- **次要**：mock server 实现 4 条 admin 路径并新增双 SDK 客户端方法与契约测试；focus.maintenance catch-up=coalesce；日志数值字段类型强制（字符串→hash）；Observation/Outbox/Scheduler/Surface 挂接指标；Readiness 执行真实写探测。

修复后门禁：`make ci` 单次完整 exit 0，406 passed，88.46%；0001/0002 与 HEAD 逐字节一致；0003 SHA 更新为 `a64c8f17637bff3375aba0fe05f3ac7227ef6ebc22f73c325dba8fd81a1ccf1f`。

### 8.1 第三轮独立复核修复（2026-08-30）

第三轮复核确认第二轮 7 项全部关闭，新复现 1 项 P0、2 项 P1；明细与测试映射见 [phase-02 文档的第三轮复核修复记录](../development/phase-02-observation-outbox-scheduler.md)。要点：

- **Scope 层级（P0）**：`session_id` 非空必须携带 `space_id`（请求级 `invalid_request`、整批零写入、契约 `dependentRequired` + 新 invalid fixture + 双 SDK 校验器同步）；`space_group_id + space_id` 必须命中 `space_group_bindings` 的当前活跃绑定或覆盖 `occurred_us` 的历史绑定——`spaces.space_group_id` 列不是成员关系事实来源。
- **合并字节增量（P1）**：压力投影改为入队真实足迹——合并不新增行但 payload 增大时按 `max(新−旧, 0)` 投影 bytes 增量，字节硬阈值不可被合并路径穿透（复核复现的 41 字节上限被 2024 字节合并穿透已闭门：`storage_full`、目标行原样、余量下同一合并成功）。
- **Dedupe 内容语义（P1）**：同键 canonical payload 逐字节相同 → 任何状态吸收返回既有行；同键不同 payload 命中未决行 → 稳定 `idempotency_key_reused`（应用投影层与仓储双层一致）；leased 行的 follower 由**新 dedupe key + 同 coalesce key** 创建（复核复现的有余量 `conflict` 已闭门为 `idempotency_key_reused`）。

第三轮修复后门禁：`make ci` 单次完整 exit 0，423 passed（+7），覆盖率 88.98%/88.82%（两次完整实测）；0001/0002 与 HEAD 逐字节一致，0003 未变更（`a64c8f17637bff3375aba0fe05f3ac7227ef6ebc22f73c325dba8fd81a1ccf1f`）。

### 8.2 第四轮独立复核修复（2026-08-30）

第四轮复核确认前三个阻断场景闭门，新发现 1 项 P1：pending/retryable + coalescable 同键异内容仍走原地合并（应用投影与仓储同病），且该分支只检查新任务的 coalesce_key、不查既有行 kind 元组——跨 kind 同键碰撞可改写他 kind 行的 payload。修复：删除该例外，dedupe 命中未决行先比 canonical payload，异内容**无条件** `idempotency_key_reused`；合并只发生在 dedupe key 未命中后、经限定 `(tenant, agent, kind, coalesce_key)` 全元组的查找进入——跨 kind 变异被结构性排除（复核复现 `PENDING_COALESCE_CHANGED_SAME_DEDUPE`/`CROSS_KIND_SAME_DEDUPE_MUTATION` 均闭门为 `idempotency_key_reused` 且行原样）。回归 `TestDedupeContentOwnership` 3 例（含仓储层直接调用）。ADR-0009 §4/§7 同步修正。

第四轮修复后门禁：`make ci` 单次完整 exit 0，426 passed（+3），覆盖率 89.07%/88.92%（两次完整实测，其一为 `make ci` 全量）；0001/0002 与 HEAD 逐字节一致，0003 未变更（`a64c8f17637bff3375aba0fe05f3ac7227ef6ebc22f73c325dba8fd81a1ccf1f`）。

### 8.1 第二轮复核修复（同日）

第二轮独立复核（仍为 Changes Requested）发现的 3 项 P0、4 项 P1 遗留缺陷已全部修复并配套可失败回归测试（明细同样见 phase-02 文档第二轮记录表）：

- **跨 Agent 边界**（P0）：agent-owned space/session 只接受归属 Agent 的 Observation 与租约 holder space；heartbeat/release 重演 holder space 授权（撤销 grant 立即闭门且不改状态）；`current()` 要求 Agent grant。
- **Actor 归属**（P0）：`actor_entity_id_at_ingest` 必须伴随 `actor_external_identity_id`——调用方内部 Entity ID 单独出现即 `invalid_request`（§6.4），零写入。
- **背压完整性**（P1）：统一入口预查完整镜像仓储结果（pending/retryable/leased dedupe 命中、leased 行逐字节精确重试吸收、可合并 kind 命中 pending 目标合并不占压力）；`replay_dead_letter` 与普通入队共用压力投影——重放不能把队列推过硬上限。
- **Kill -9 恢复语义**（P1）：恢复重执行与崩溃同一业务 closure，断言 `completed_jobs == 1` 且 `business_results == 1`——"恢复后无丢失逻辑效果"成立而非仅"崩溃回滚正确"。
- **稳定错误码**（P1）：Coordinator 不可达（busy/OSError）映射域内 `NotReadyError`（稳定码 `not_ready`）；未使用的 `LeaseCoordinatorUnavailable` 移除。
- **过期记账**（P1）：`expire_stale` 在置 expired 的同一事务内追加 `expired` 租约事件，与 ADR-0010 append-only 声明一致。

二轮修复后门禁：`make ci` 单次完整 exit 0，**416 passed，88.94%/88.78%**（两次实测）；0001/0002 仍与 HEAD 逐字节一致（0003 未再变更）。

## 9. 已知限制

- 游标限定十进制整数（不透明游标待真实平台需求+新 ADR）；日程语法限定 interval/daily。
- 磁盘恢复滞回是进程内状态：跨进程一致，但不共享 trip 标志（秒级窗口内另一进程可能按即时读数放行——磁盘读数本身是权威值，语义安全）。
- 默认仅启用 `maintenance.selfcheck` 种子 Handler；其余 §17.4 kind 等待对应阶段启用（未启用的 outbox 任务保持 pending，不堆积 Schedule）。
- 背压的租户/Agent 配额在 enqueue 短事务内聚合查询（当前规模 O(pending)；超大规模需增量计数表，属后续优化）。
- `GET /v1/observations/cursors` 的服务端实现存在，SDK 客户端方法以 mock 契约验证；真实传输层未引入。
- Kill -9 测试以 `os.kill(self, SIGKILL)` 于精确边界自毁（等价于外部 kill -9 的进程死亡语义；不等价于掉电瞬间的存储层部分写，后者由 SQLite FULL 同步+WAL 承担）。

## 10. 遗留给后续阶段

- Phase 3 依赖（幂等观察流、固定 Source Revision/Watermark、可靠 Outbox、可注入时钟、持久 Schedule/Tick、可选 Lease/Epoch）全部就绪。
- 性能数据复现：`uv run pytest tests/performance -s`（输出环境与 p95 行）。
