# Phase 3 验证报告：近期上下文、State 与 Focus

> 归档证据：以下版本、测试数量、耗时与覆盖率是本阶段执行时的历史快照，未在本次文档整理中重跑；不能作为当前发布已通过的证明。当前状态见[阶段索引](../development/README.md)，发布重验见[Phase 14](../development/phase-14-hardening-release.md)。
> 后续闭环：HTTP/进程入口已由 [Phase 10](../development/phase-10-consolidation-reflection.md)交付；旧报告中的应用层/mock 范围只描述当时环境。

> 结果：**通过（make ci exit 0）**  
> 日期：2026-08-31 · 实现基线：Phase 2 提交 `8041552` 之上的 Phase 3 实现  
> 版本列车：Core/Python SDK/TypeScript SDK = **0.4.0** · Schema **4** · API v1 · Contract 1.2.0

## 1. 环境

| 项 | 值 |
| --- | --- |
| 硬件 | Apple Silicon（arm64, Darwin 25.5.0） |
| Python | 3.12.13（uv 管理） |
| SQLite（宿主绑定） | 3.50.4（测试以本地允许清单显式接入；生产允许清单不变） |
| uv / Node / npm | 满足 required-version（uv ≥0.11.29,<0.12；Node ≥22） |
| 数据库状态 | 每用例独立临时目录，WAL、synchronous=FULL、外键开启 |
| 并发 | 功能测试单线程；并发语义由 50 线程 CAS、8 线程性能突发与 50 Worker 专项测试覆盖 |

复现命令：`make ci`（= format-check + lint + import-boundary + docs + mypy strict + contracts-check + pytest + SDK typecheck/test）。

## 2. 门禁实测（最终单次完整运行）

| 检查 | 结果 |
| --- | --- |
| ruff format --check | 101 files already formatted |
| ruff check（E/F/B/UP/SIM/RUF） | All checks passed |
| Domain import boundary | passed |
| docs 结构与本地链接 | passed |
| mypy --strict（src+sdk+tools+tests，100 文件） | no issues |
| 契约生成无漂移 + v1 兼容快照 | ok（additive only） |
| Python 测试 | **619 passed**（含 2 个 SDK fixture 用例；原始 568 + 首轮审查 36 + 复审 15） |
| 覆盖率（branch，阈值 80） | **87.89%**（最终 `make ci` 全量实测） |
| TS typecheck + node --test | pass 1 / fail 0 |

测试构成：Phase 0–2 回归 426（全绿，其中 4 处版本列车断言随 Schema 前进合法更新：空库 [1,2,3]→[1,2,3,4]、Phase1 升级 [3]→[3,4]（staged 多版本路径）、备份 schema 3→4、窗口 (2,3)→(3,4)、启用 kind 集合 {selfcheck}→{selfcheck, observation.recorded, recent_context.maintenance, focus.maintenance, state.projection}——均为版本演进而非断言弱化，性质断言"启用 ⊆ 已实现 handler"保留并新增 `test_every_enabled_kind_has_a_handler`）。Phase 3 原始新增 142 个用例：domain 性质 18（每性质 200 固定种子案例）、recent 15、state 19、focus 17、recall 20、jobs 12、安全矩阵 7、迁移/备份 12、kill-9 新增 3 类、性能 1、契约 mock +9；首轮人工审查新增 36，复审完整性/授权/retention/maintenance 回归新增 15，最终共 619。

## 3. 需求 → 实现 → 测试 → 实测结果

### 3.1 RecentContextProjection（P3-RECENT-01 / §9.1）

| 需求 | 实现 | 测试 | 实测 |
| --- | --- | --- | --- |
| 只读 committed Observation | 窗口查询读 observations 表；失败批次零写入 | `test_reads_only_committed_observations` | 非法批次 → 窗口空 |
| Session/Space 热窗口 | session 窗口按 session 等值；space 窗口按 §5.2 严格空值语义读 session-less 观察 | `test_session_and_space_windows` | session 窗口 3 refs ⊂ space 窗口 1 ref（互不重叠） |
| 稳定键排序（不依赖 Outbox 顺序） | `(occurred_us, id)` 排序的纯函数 builder | `test_ordering_independent_of_input_order` + 性质 200 案例 | 乱序输入结果全等 |
| refs/head/tail/token/watermark/builder/expiry | BuiltProjection 全字段 + generation 表 | `test_projection_metadata_present` | 全字段齐备 |
| 无 Provider 确定性裁剪 + 仅 Source Refs 段 | Token 预算热窗 + 有界压缩段（无自由文本） | `TestRecentBuilderProperties`（4 性质 ×200 案例） | 三次重建哈希一致；段只含 refs |
| invalidate/expire/shadow/swap/Canonical fallback | pointer 删除→retired；读取终检失败→带过滤的 canonical 窗口 | `test_invalidate_returns_to_canonical_then_rebuild`、`test_generation_expiry_falls_back`、`test_builder_version_mismatch_falls_back_to_canonical` | 全通过 |
| generation 内容与 target 自洽 | 读取复算 result hash、精确核对 target 与 ref revision/time/token | `test_corrupt_generation_hash_falls_back`、`test_generation_target_metadata_is_revalidated`、`test_result_hash_is_an_integrity_invariant` | 损坏整代弃用并安全 fallback |
| tombstone/过期/越权不残留 | generation 逐 ref 复检 + fallback 同等过滤 | `test_tombstoned_observation_never_served`、`test_privacy_label_blocks_generation_serving` | 检出即弃用整代，fallback 过滤后返回 |
| 跨 Space 不泄漏 | 结构性 WHERE（tenant+agent+space 等值） | `test_space_b_never_sees_space_a_content`、`test_space_group_membership_does_not_leak_raw_content`、`test_cross_space_recall_zero_leak`（20 次循环） | 泄漏成功数 **0** |

### 3.2 StateRecord（P3-STATE-01 / §9.2）

| 需求 | 实现 | 测试 | 实测 |
| --- | --- | --- | --- |
| `(scope, ns, key)` + 不可变 revision + 原子指针 | scope_key NULL-free 唯一键；创建 UNIQUE 裁决、更新 pointer CAS | `TestPutIdempotency` 5 例、`TestPhase3RestoreInvariants` | 通过 |
| Namespace Policy（TTL 默认/上限、历史保留/上限、value 上限、Authority 白名单、Scope 要求） | 内置 3 namespace + 租户覆盖（admin+reason+审计）+ 保守默认 | `TestNamespacePolicy` 6 例 | 通过（含 TTL 封顶、默认 TTL、authority 拒绝、字节上限、scope 要求、租户覆盖生效） |
| PUT 幂等；同键同指纹回首次结果、异指纹 `idempotency_key_reused` | 指纹只覆盖调用方字段（时钟默认值不入指纹——实现期修复的真实缺陷） | `test_same_key_same_payload_returns_first_outcome`、`test_same_key_different_payload_is_key_reuse` | revision 1 重放 replayed=True、1 行；异指纹稳定拒绝 |
| completed replay 与历史遵从当前安全状态 | 当前 Scope 重授权 + Tombstone 优先 | `TestStateTombstoneAndReplayAuthorization` | 撤权/Forget 后稳定拒绝 |
| 并发同 Expected Revision 恰一成功 | pointer CAS | `test_fifty_threads_one_winner`（50 线程） | ok=**1**、mismatch=**49**（全为稳定 `revision_mismatch`）、最终 revision=2、总 revision 行=2 |
| 过期不参与默认读取/Recall，历史可审计 | 读路径时间过滤；`history_unavailable` 语义 | `test_expired_state_hidden_from_reads_but_audit_visible`、`test_expired_state_dropped_after_ttl` | 过期后 get=None、history 仍见 |
| 历史 retention 精确有界 | 同事务单条 DELETE 保留 newest N 且保护当前指针 | `TestPruneCurrentOnly` | current-only 正确；551 条一次收敛到 2 |
| Coalescing 只合并投影 Job | `state.projection`（STATE 类）coalesce per (scope,ns,key) | `test_coalescing_merges_projection_jobs_not_revisions` | pending 投影 Job ≤2、canonical revision 5/5、watermark/audit 齐 |
| payload 不泄漏 value | 指针校验 Job 只带 id/hash | `test_projection_job_payload_carries_no_values` | canary 不在 payload |

### 3.3 FocusItem（P3-FOCUS-01 / §9.3）

| 需求 | 实现 | 测试 | 实测 |
| --- | --- | --- | --- |
| 7 kinds / 5 statuses | 全枚举 + 终态无出边 | `test_all_kinds_accepted`、`test_invalid_transitions_are_stable` | 通过 |
| 非法迁移稳定 `invalid_state_transition`（先于参数检查） | 状态机预检 + InvalidTransitionError | 同上 | 终态→任何目标均稳定码 |
| 容量 = item 数 + kind 配额 + token budget（可配置） | 确定性驱逐（lowest activation→dormant） | `test_item_cap_evicts_lowest_activation_to_dormant`（**200 案例**：被驱逐者 activation == 准入前集合最小值）、`test_kind_quota_evicts_within_kind`、`test_token_budget_evicts_until_fit` | 通过 |
| 时间衰减可注入 Clock、确定性、不提 confidence/不动来源 | 纯函数 `base*0.5^(Δ/half)` + activation_base 不变量 | `test_decay_is_pure_and_monotone`、`test_decay_never_double_compounds`（各 200 案例）、`test_decay_sweep_is_idempotent_and_pure`（200 案例：同时刻两次 sweep revision 数不变、activation == 纯函数值） | 通过 |
| activate/dormant/dismiss/expire/promote | activate 有界增强（clamp 1.0） | `test_transitions_with_expected_revision`、`test_only_explicit_activation_boosts`（retrieved 5 次不增强） | 通过 |
| Promotion 只做 seam | revision 记 target type、id=NULL、审计事件 | `test_promote_records_target_and_audit_without_objects`（并断言 tasks 表不存在） | 通过 |
| Dismiss/Expire/Promote 保留历史不删除 | append-only revision | `test_dismiss_expire_keep_history_and_sources` | 2 revisions 全存 |
| `affect` 不写 Persona | 无 persona 写路径 | `test_affect_never_touches_persona` | persona 行数与 traits 内容不变 |
| TTL 过期经 maintenance | Sweep: expire 优先于 decay | `test_ttl_expiry_via_maintenance`、`test_expired_items_excluded_from_lists_and_recall_routes` | 过期入 history、列表/路由排除 |
| maintenance 有界批次无饥饿 | active 优先、过期 dormant 次优先、其余按 updated_us 轮转 | `test_active_item_is_not_starved_by_five_hundred_older_dormant_rows` | 500 个旧 dormant 之前的较新 active 仍在首批按 TTL 过期 |

### 3.4 结构化 Recall（P3-RECALL-01 / §18，Phase 3.4）

| 需求 | 实现 | 测试 | 实测 |
| --- | --- | --- | --- |
| 三 Route + 独立子 deadline + 候选上限 | Monotonic 子 deadline（1/3/1/3/剩余）+ per-route cap | `test_all_routes_completed`、`test_candidate_limits_per_route` | 三路由 completed、cap 生效 |
| completed/degraded/partial 结构 + 稳定原因码/retryable/fallback | DegradedRoute + 原因码集合；partial 至少一个关键 Route 成功 | `test_each_route_timeout_degrades_accurately`（**3 路由 ×20 次**）、`test_route_exception_degrades_route_only`、`TestFailClosed` | 超时路由 `route_deadline_exceeded`+retryable、他路由仍 completed、partial=True；任意全降级与 strict 降级 → `not_ready` |
| 统一 Candidate + 确定性排序/裁剪 | deterministic candidate_id + 版本化排序器 | `test_hundred_replays_identical_order_and_trimming`（**100 次**）、`test_stable_sort_key_tie_breakers` | 100 次签名唯一 |
| Layer/Token Budget | 稳定排序后逐层分配 | `test_layer_and_token_budgets` | 总量与分层均不越限 |
| 最终 Canonical Rehydrate | 每候选回读+六维终检 | `test_tombstoned_observation_dropped`、`test_promoted_focus_dropped_after_transition`、`test_expired_state_dropped_after_ttl` | 三类剔除全验证 |
| minimum watermark 读-写-见 | 不可达 → 无关键 Route 可成功，稳定 `not_ready` | `TestMinimumWatermark` 3 例 | 不可达均 fail closed / 可达时三路由 completed |
| safe trace | 只含 hash/数量/版本/耗时 | `TestSafeTrace` 2 例 | 正文 canary 不入 trace |
| Persona 非候选；授权失败请求级 | 请求 scope 在 watermark 早退前校验 + 路由异常分类 | `test_persona_is_never_a_candidate`、`test_cross_agent_denied`、`test_cross_tenant_denied`、`test_watermark_short_circuit_never_precedes_authorization` | 通过 |
| Phase 6 边界 | 无 FTS/Vector/Graph/Cache/Usage/`/v1/recall` 契约 | 契约面仅 recent/state/focus/admin-rebuild | 无越界路径 |

### 3.5 Job Handler 与安全（P3 各项）

| 需求 | 测试 | 实测 |
| --- | --- | --- |
| observation.recorded → 调度 recent 重建 | `test_observe_schedules_recent_rebuild` | 重建后 generation 生效、1 ref |
| 重建幂等（dedupe by target+watermark） | `test_handler_is_idempotent_across_replays`（4 轮 worker） | generation 恒为 1、任务全 completed |
| agent 级观察不产生 target | `test_agent_level_observation_schedules_nothing` | 零 maintenance 任务 |
| focus.maintenance 持久调度 + 幂等 | `test_scheduled_decay_sweep_runs_and_is_idempotent`（Schedule/Tick + coalesce catch-up + 24h 衰减跳变） | activation 衰减、状态 dormant、二次 tick revision 不减 |
| state.projection 指针校验 | `test_pointer_check_completes_for_healthy_records`、`test_broken_pointer_fails_the_job`（伪造指针 → retryable/dead） | 通过 |
| 未知 kind/version fail closed | `TestFailClosed` 4 例 | 入队拒绝 / pending 无人领取 / 调度拒绝 / 启用 ⊆ handler |
| 横向矩阵（Tenant/Agent/SpaceGroup/Space/Session） | `test_phase3_security.py::TestHorizontalMatrix` 5 例 | 全部 AccessDenied/InvalidRequest，零数据返回 |
| privacy/restricted/consent 终检 | `TestPrivacyFinalChecks` | restricted 仅 admin、entity-private 仅授权主体、公开内容正常 |
| 泄漏扫描 | `test_canary_absent_from_metrics_logs_audit_outbox_trace` | canary 不入 metrics/logs/audit details/outbox payload/trace |

### 3.6 Migration/备份（§20.7、§21）

| 需求 | 测试 | 实测 |
| --- | --- | --- |
| 0001–0003 字节不变（对 HEAD `8041552`） | `test_published_bytes_match_head_baseline`（git show 对比）、`test_recorded_checksums_still_match_after_0004` | 逐字节一致；库内 checksum 与磁盘一致 |
| 真实 Phase 2 数据 3→4 | `test_phase2_data_upgrades_intact` | 数据保留、8 张新表、不变量零违例 |
| 0004 元数据 | `test_0004_is_online_safe_with_version_window` | online_safe=true、min_app=0.4.0、recovery=none |
| 空库安装 | `test_empty_database_installs_all_four` | version=4 |
| Phase 3 恢复不变量 | 伪造 state/focus 指针、悬空/错绑 generation、损坏 projection hash、缺失/漂移 observation ref 均被拒 | 六类拒绝 |
| 备份→恢复→冒烟 ×3 | `test_backup_restore_roundtrip_keeps_phase3_rows` | 不变量零违例、state/focus/recent 行存活 |
| Phase 1 备份仍可恢复 | `test_phase1_backup_still_restores` | 通过 |

## 4. 量化验收基线证据

| 基线 | 实测 |
| --- | --- |
| State Coalesced Write p95 ≤ 25ms | **4.69ms**（warmup 10、样本 120、payload 121B、库内 2000 state 行 + 2000 pending 投影 Job；median 3.69ms；`tests/performance/test_state_latency.py`，输出含环境行） |
| 并发突发口径（如实报告，非门禁） | 8 并发流写手 p95 51.17ms（24 样本）——度量进程内单 Writer Gate（§20.2）队头排队而非写入路径；busy 事件 **0**；queue lag 8.6s（corpus 投影 Job 未被 worker 清扫所致，热键 revision 全数落地） |
| Recent 重建 3 次一致 | refs/segments/token/hash 三次全等（另有 4 条 builder 性质 ×200 案例） |
| Focus 性质每条 ≥200 案例 | 容量驱逐 200、状态机 200（终态×目标矩阵）、衰减纯函数 200、衰减不复合 200、幂等 sweep 200、score/summary/clamp 200 |
| 事务提交前后/指针前后/影子切换前后 ×20 | state_put（pre_tx、pre_pointer、pre_commit、post_commit）、focus_transition（pre_commit、post_commit）、recent_rebuild（pre_generation、pre_swap、pre_commit、post_commit）= **10 边界 ×20 = 200 次注入**（子进程真 SIGKILL，退出码必须 -9；累计 Phase 2 的 140 次之外共 340 次） |
| 恢复后无悬空 pointer/未提交 ref/跨 Space/过期参与 | 每 Kill-9 用例断言 `verify_database_invariants == ()` + 计数不变量 + 重放收敛（state 重放前按 §20.5 让 in_progress 租约失效） | 全部通过 |
| 50 并发 Expected Revision | ok=1、mismatch=49（稳定码）、最终 revision=2、revision 行=2 |
| Structured Recall 100 次重放 | Candidate 顺序与裁剪签名唯一 |
| 每 Route 超时 ≥20 次 | 3 路由 ×20，completed/degraded/partial 准确；审查加固后三路由均执行协作式子 deadline 检查（含空热窗口与 State/Focus 每 namespace/候选迭代），过期 deadline 下全部降级而非空完成（`TestFailClosed::test_every_route_respects_expired_deadline`） |

## 5. Migration 证据

- `0004_phase3_recent_state_focus.sql`，头部 `-- iris: online_safe=true lock_ms=200 min_app=0.4.0 max_app= recovery=none`。
- SHA-256：`c6174cbefd745d371d72f75a4e6adaeeb856dad780370e8c133ae4806a23ee9a`（与库内 `schema_migrations` 记录一致）。
- 兼容窗口：SUPPORTED_SCHEMA_MIN=3，MAX=4（0.4.0 可读 Phase 2 库并前向迁移；Schema 2 需先经 0.3.0 二进制——MigrationRunner 本身支持一次走完 2→4，窗口只约束 Ready）。
- 0001/0002/0003 对 HEAD `8041552` 逐字节一致（`ec236f7e…`、`7fcd0883…`、`a64c8f17…`）。
- 回退策略：遵循兼容二进制 + 备份路径；State/Focus revision 与 recent generation 不经 Down Migration 删除或回拨。

## 6. 契约证据

- `contracts.json`：contract_version 1.1.0→**1.2.0**、schema_version 3→**4**、package_version **0.4.0**；capabilities 追加 `recent-context.v1`、`state.v1`、`focus-items.v1`。
- OpenAPI 新路径：`/v1/recent-context`、`/v1/state/{namespace}/{key}`（PUT/GET）、`/v1/state`、`/v1/state/{namespace}/{key}/history`、`/v1/focus-items`（POST/GET）、`/v1/focus-items/{id}`（GET）、`/v1/focus-items/{id}:activate|:dormant|:dismiss|:expire|:promote`、`/v1/admin/recent-context:rebuild`（权限测试覆盖非 admin 拒绝）。
- 审查加固后（仍在 1.2.0 未发布版本列车内）：focus 五个变更端点均要求 `Idempotency-Key` 头；`listFocusItems`/`getStateHistory` 补 `space_id`/`session_id` 查询参数；`FocusView.promotion_target_type` 可空并纳入 enum。
- 错误码零变更（add-only 未新增）；fixtures 21→35 用例（valid 6 / forward 2 / invalid 6 新增）；JSON Schema 2020-12 新增 6 份；Python/TS 校验器与 12 个新客户端方法同步；TS lockfile 版本随包更新。`tools.generate_contracts --check` 与 compatibility check 无漂移。**新增门禁：`tests/contract/test_json_schemas.py` 以生成的 JSON Schema 严格校验全部 valid/invalid fixture（valid 必须过、invalid 必须挂；forward/ 目录专测 SDK 前向宽松性，不参与严格校验）**。
- 开发依赖新增 `jsonschema` + `types-jsonschema`（仅测试用途，运行时零依赖不变）。
- 未引入真实 FastAPI 传输层（沿用应用层契约 + mock server 模式，无新 ADR 需求）。

## 7. 设计决策（本轮固化）

ADR-0011 全文见 [adr/0011](../adr/0011-phase3-recent-state-focus.md)。要点：

1. Recent = 不可变 generation + 每 target 指针；shadow 重建为"构建→校验→单事务原子切换"；读取终检失败退回**带同等过滤**的 Canonical 窗口（fallback 不过滤会泄漏，实现期修复）。
2. 五维 scope 用 NULL-free 规范字符串作唯一键（SQLite 复合 UNIQUE 视 NULL 为互异）；State 创建/更新竞态分别由 UNIQUE 与 pointer CAS 裁决；PUT 幂等指纹只覆盖调用方字段。
3. Focus 衰减是 `(activation_base, last_activated)` 的纯函数——幂等、不复合、不提 confidence；容量为确定性驱逐到 dormant；promotion 仅 seam。
4. Recall 骨架：三路由、Monotonic 子 deadline、确定性 candidate_id 与版本化排序器、六维终检 Rehydrate；授权失败是请求级错误不降级为路由失败；`/v1/recall` 协议留给 Phase 6。
5. 请求 scope 只含调用方显式命名维度（不复制 space 的 group 成员关系）；Space 窗口按 §5.2 严格空值语义只读 session-less 观察。
6. `state.projection` 新 coalescable kind（STATE 类）承担指针不变量检查；四个 Phase 3 handler 随实现与测试启用。

## 8. 实现期修复的真实缺陷（配套回归测试）

| 缺陷 | 修复 | 回归测试 |
| --- | --- | --- |
| 段切片端点未钳制，热窗观察被重复计入 summary segment | builder 切片 `min(start+segment_sources, cursor+1)` | `test_invariants_hold_and_tail_is_newest`（200 案例，修复前即失败） |
| Canonical fallback 窗口不过滤 tombstone/privacy，已删除/受限内容经降级路径回流 | fallback `_build(access=…)` 逐行过滤 | `test_tombstoned_observation_never_served`、`test_privacy_label_blocks_generation_serving` |
| State PUT 幂等指纹计入服务端时钟默认值，同 payload 重试误报 `idempotency_key_reused` | 指纹仅覆盖调用方字段 | `test_same_key_same_payload_returns_first_outcome` |
| State 更新 CAS 使用当前值而非调用方 expected_revision，过期期望值可被接受 | 先比对后 CAS | `test_stale_expected_revision_rejected` |
| maintenance 的 activation 刷新以同状态调用状态机被静默拒绝，衰减漂移永不修正 | `_transition(allow_same_status=True)` 白名单 | `test_decay_sweep_is_idempotent_and_pure`（200 案例） |
| 非法迁移的参数检查先于状态机检查，终态→promote 缺参时报参数错误而非 `invalid_state_transition` | 合法性预检前置 | `test_invalid_transitions_are_stable` |
| 服务层把 space 的 group 成员关系复制进请求 scope，要求调用方持有未表达的 group 授权 | 请求 scope 只含显式命名维度 | `test_space_group_membership_does_not_leak_raw_content` |
| Recall 把授权失败吞为路由降级 | AccessDenied/ScopeViolation 请求级抛出 | `test_cross_agent_denied`、`test_cross_tenant_denied` |
| State 列表 session 过滤把存储为 NULL 的维度错误排除（违反 §5.2 向下可见） | `(r.session_id = ? OR r.session_id IS NULL)` | recall state 路由集成断言 |

## 9. 人工审查修复（2026-08-30，P0×2 + P1×5 + P2×2，全部带回归测试）

人工审查在 CI 全绿的前提下发现 9 项缺陷（结论：门禁缺少跨 Scope、负数边界、严格 partial 与 JSON Schema 自验证案例）。全部修复并补齐回归测试后复跑完整门禁；语义决策固化于 ADR-0011 §5（补强）、§7、§8、§9。

| 级别 | 缺陷 | 修复 | 回归测试 |
| --- | --- | --- | --- |
| P0 | Focus 按 ID 读取/修改只检查 tenant+agent，且历史/变更/幂等重放可绕过 Privacy/Tombstone；授权撤销或 Forget 后仍可能回读正文 | by-ID 内容路径复核 item space 信封、当前与返回 revision 的 Privacy、Tombstone；completed replay 在按 revision_id 回读前重跑当前安全终检 | `TestByIdSpaceAuthorization` + `TestByIdPrivacyTombstoneAndReplayAuthorization` |
| P0 | State 列表把请求侧空 scope 当通配符：agent 级列表返回所有 Space/Session 状态，space 级列表返回全部 Session 状态（横向泄漏） | SQL 结构性收窄（agent 级 → `space IS NULL AND session IS NULL`；space 级 → 排除 session 记录；session 级 → 向下可见）+ 应用层逐条 `scope_allows` 纵深复核 | `TestListScopeNullSemantics`（5 用例：agent/space/session 格、横向泄漏复现、越权 space 拒绝） |
| P1 | Recall 未 fail closed：deadline 只做调用前检查，I/O 跨过子 deadline 后仍可 completed；全降级空结果违反“至少一个关键 Route 成功才可 partial” | 三路由 I/O 前后 + 迭代检查，Orchestrator port 返回后权威复核；全降级与 watermark 不可达始终稳定 `not_ready`；strict 模式任一降级即错误 | `TestFailClosed`（含跨 I/O deadline 的三 Route 直击）+ `TestMinimumWatermark` |
| P1 | Focus 状态机 preflight TOCTOU：合法性在只读事务检查、写事务不复验；恒假比较条件（第 514 行）；预测下一 revision 的请求可能在并发方推进终态后仍写出非法状态 | 合法性权威校验移入幂等执行体内的同一序列化写事务（读→授权→合法性→参数→写→CAS），TOCTOU 窗口归零 | `test_write_tx_revalidates_terminal_state`（白盒直击执行体）、`test_public_replay_after_terminal_is_invalid_transition` |
| P1 | Focus activate/dormant/dismiss/expire/promote 缺 Idempotency Key；补键后的初版 completed replay 又错误缓存了旧授权 | 服务层必填键 + §20.5 指纹 + 第一次 revision 重放；重放前重新检查 Scope/Privacy/Tombstone；OpenAPI/双 SDK/mock 同步必填 | `TestMutationIdempotencyAndRevalidation`、`TestByIdPrivacyTombstoneAndReplayAuthorization`、裸 HTTP header 直击 |
| P1 | 发布的 Focus JSON Schema 拒绝自己的 valid fixture（promotion_target_type enum 不含 null） | schema 改为 `type: [string,null]` + enum 含 null；新增 CI 门禁：全部 valid fixture 必须过其 JSON Schema 2020-12、invalid 必须挂（双 SDK 校验器前向宽松，发现不了此类矛盾） | `tests/contract/test_json_schemas.py`（15 用例，含 null 回归直击） |
| P1 | Space/Session 资源无法经发布契约访问：listFocusItems 无 space_id/session_id；State history 只有 agent_id；OpenAPI 有 session_id 而 Python/TS SDK 的 listStates 遗漏 | OpenAPI 与双 SDK 补齐（focus 列表、state history 的 space/session 查询参数；listStates session_id 对齐）；mock server 校验 session 依赖 space | `test_focus_create_and_transitions_round_trip`（扩参）、`test_state_list_and_history_accept_scope_params`、`test_focus_list_rejects_session_without_space` |
| P2 | `max_history_revisions=0` + retain_history=true 时 keep=0 → repository 不裁剪，且固定 500 行删除批次在策略收紧时不能一次达到上限 | 应用层 `max(policy,1)` + repository floor 到 1；单条 DELETE 精确保留 newest N 并保护当前指针 | `TestPruneCurrentOnly`（current-only + 551 revisions 收紧到 2） |
| P2 | Recall 回归测试恒真断言（`… or True`，另有一处同型 `… and True`） | 改为真实断言（promoted 候选消失 + focus 路由仍 completed；state 候选存在） | `test_promoted_focus_dropped_after_transition`、`test_expired_state_dropped_after_ttl`（修复后仍全绿） |

复审继续检查同类边界，另发现并修复 4 组独立缺陷：

| 级别 | 缺陷 | 修复 | 回归测试 |
| --- | --- | --- | --- |
| P1 | State completed PUT replay 未重新授权，State Tombstone 后 history/new PUT/replay 仍可能触达旧记录 | completed replay 重新解析当前 scope 并核对 outcome target；所有写/历史路径 Tombstone 优先 | `TestStateTombstoneAndReplayAuthorization` |
| P1 | Recent `result_hash` 只生成不验证，pointer/generation target 与 ref revision/time/token 元数据未在读取及恢复时自洽校验 | 读取复算 hash、精确绑定 target、逐 ref 核对；Canonical fallback fail closed；恢复不变量同步拒绝损坏 hash、错绑 target 与漂移 ref | `TestFinalChecks::test_corrupt_generation_hash_falls_back`、`test_generation_target_metadata_is_revalidated`、`TestPhase3RestoreInvariants` 新增 2 例、domain hash invariant |
| P1 | Recall minimum-watermark 早退位于请求授权之前，越权请求可被错误归类为 `not_ready` | Orchestrator 在读取 watermark 前权威校验 Agent/Space/Session | `test_watermark_short_circuit_never_precedes_authorization` |
| P1 | Focus 永不删除 dormant item，但 maintenance 固定按 created_us 取前 500；累计 500 个旧 dormant 后，较新的 active 永久无法衰减/TTL 过期 | 专用 maintenance 队列：active 优先、过期 dormant 次优先、其余 dormant 按 updated_us 跨 tick 轮转 | `test_active_item_is_not_starved_by_five_hundred_older_dormant_rows` |

## 10. 已知限制

- Space 热窗口按 §5.2 严格空值语义只含 session-less 观察；session 内容需经 session 窗口恢复（安全默认，若宿主需要"空间聚合窗口"须以新 ADR 显式放宽）。
- State 历史按 Namespace Policy 同事务裁剪——高频无历史 namespace 只保当前 revision（有意的有界性）；审计事件仍完整。
- `recent_context.maintenance` 的 handler 在 worker 内执行完整重建（窗口有界，本阶段规模内事务短）；超大规模 target 需要分片策略，属后续优化。
- **历史缺口已关闭**：完整 Recall/Usage 契约与并行 Route 由 Phase 6 交付；Core Recall Cache 仍为显式非目标。
- 单 Writer Gate（§20.2）下 8 并发突发写 p95 51ms（队头排队，非写入路径）；对更高并发写吞吐需要批量写事务或写入合并，超出本阶段范围。
- **历史缺口已关闭**：真实 HTTP 传输由 [Phase 10](../development/phase-10-consolidation-reflection.md)交付；本报告的原始测试仍是当时应用层/mock 范围。
- Kill-9 以 `os.kill(self, SIGKILL)` 于精确边界自毁（等价进程死亡语义；掉电部分写由 SQLite FULL+WAL 承担）。
- 背压租户/Agent 配额在 enqueue 短事务内聚合查询（O(pending)），超大规模需增量计数表（沿袭 Phase 2 已知限制）。

## 11. 遗留给后续阶段

- Phase 4 依赖就绪：短期读取 Route、Focus Promotion seam（target type + 待回填 id + 审计）、可注入时间、Schedule Job Kind。
- Phase 6 可复用 RecallOrchestrator 骨架（RecallRoute port / Envelope / rehydrate / 预算 / deadline），新增路由无需改动三条现有路由语义。
- 性能数据复现：`uv run pytest tests/performance -s`（输出环境与 p95 行）。

## 原阶段验收目标

下列门槛从已归档阶段计划移入，保留未被实测证明的要求。它们是当时的验收目标，不能从本报告 Passed/Completed 标签推断逐项均已完成；是否达到须与前文的样本、测试与限制核对。尚未闭合项由 Phase 14 的发布矩阵承接。

> 下列数字为**实测值**，口径见验证报告。

- **State Coalesced Write p95 ≤ 25ms**：实测 **4.69ms**（顺序 coalesced 流、payload 121B、库内 2000 行 corpus 与 2000 pending 投影 Job；p50 3.69ms；`tests/performance/test_state_latency.py` 输出含环境行）。另如实报告 8 并发突发口径 p95 51.17ms——该数字度量的是进程内单 Writer Gate（§20.2）的队头阻塞而非写入路径本身；busy 事件 0。
- **Recent 连续重建 3 次一致**：同一 Observation 集/Watermark/Builder/Estimator 下 refs、segments、token estimate 与 result hash 三次全等（`test_three_rebuilds_are_identical`，且 builder 性质测试每条 200 固定种子案例覆盖）。
- **Focus 性质测试每条 ≥200 案例**：容量驱逐/状态机/衰减纯函数/幂等/score/summary/clamp 全部以 `CASES = 200` 固定种子驱动（`tests/unit/test_phase3_domain.py` + `test_focus_items.py`）。
- **故障注入**：新增 10 个边界（state_put 4、focus_transition 2、recent_rebuild 4）× 20 次 = **200 次**（累计 Phase 2 的 140 次之外）；恢复后无悬空 pointer、无未提交 observation ref、无跨 Space 内容、无过期对象参与 Recall，重放收敛。
- **Structured Recall 重放/超时**：相同输入+Canonical 快照 100 次重放顺序与裁剪完全一致；每条路由超时场景 20 次，Envelope 准确标记 completed/degraded/partial 与稳定原因码。
