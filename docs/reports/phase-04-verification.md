# Phase 4 验证报告：Note、Task 与 CognitiveEvent

> 归档证据：以下版本、测试数量、耗时与覆盖率是本阶段执行时的历史快照，未在本次文档整理中重跑；不能作为当前发布已通过的证明。当前状态见[阶段索引](../development/README.md)，发布重验见[Phase 14](../development/phase-14-hardening-release.md)。
> 后续闭环：HTTP/进程入口已由 [Phase 10](../development/phase-10-consolidation-reflection.md)交付；旧报告中的应用层/mock 范围只描述当时环境。

> 结果：**通过（make ci exit 0，含 2026-09-01 一/二/三/四轮审核修订回归）**  
> 日期：2026-08-31（初版）· 2026-09-01（审核修订、二轮/三轮/四轮审核修订与回归证据）· 实现基线：Phase 3 提交 `b4587b1` 之上的 Phase 4 实现  
> 版本列车：Core/Python SDK/TypeScript SDK = **0.5.0** · Schema **5** · API v1 · Contract 1.3.0

## 1. 环境

| 项 | 值 |
| --- | --- |
| 硬件 | Apple Silicon（arm64, Darwin 25.5.0） |
| Python | 3.12.13（uv 管理） |
| SQLite（宿主绑定） | 3.50.4（测试以本地允许清单显式接入；生产允许清单不变） |
| uv / Node / npm | 满足 required-version（uv ≥0.11.29,<0.12；Node ≥22） |
| 数据库状态 | 每用例独立临时目录，WAL、synchronous=FULL、外键开启 |
| 并发 | 功能测试单线程；并发语义由 50 线程 CAS、20 轮 fence 抢占与 180 次 SIGKILL 注入覆盖 |

复现命令：`make ci`（= format-check + lint + import-boundary + docs + mypy strict + contracts-check + pytest（含 80% 覆盖率门禁）+ SDK typecheck/test）。

## 2. 门禁实测（最终单次完整运行）

| 检查 | 结果 |
| --- | --- |
| ruff format --check | 116 files already formatted |
| ruff check（E/F/B/UP/SIM/RUF） | All checks passed |
| Domain import boundary | passed |
| docs 结构与本地链接 | passed |
| mypy --strict（src+sdk+tools+tests，116 文件） | no issues |
| 契约生成无漂移 + v1 兼容快照 | ok（additive only） |
| Python 测试 | **4422 passed**（初版 4351 + 一轮 15 + 二轮 11 + 三轮审核回归 37 + 四轮审核回归 4 + 五轮审核回归 4） |
| 覆盖率（branch，阈值 80） | **86.10%** |
| TS typecheck + node --test | pass 4 / fail 0（fixtures 1 + 四轮 wire 断言 3） |

测试构成：Phase 0–3 回归全绿（五轮在 Observation/Surface 既有面新增 2 个回归；4 处版本列车断言随 Schema 前进合法更新：空库/升级版本列表 4→5、窗口 (3,4)→(4,5)、备份 schema 4→5、启用 kind 集合 +5——均为版本演进而非断言弱化；性质断言"启用 ⊆ 已实现 handler"保留，fixture 现注入 phase4_handlers；focus promotion seam 测试从"tasks 表不存在"演进为"表存在但 focus 不得创建行"）。Phase 4 共 **3784** 个用例：domain 性质 3614（状态机×200 固定种子/次，DAG/条件求值/occurrence 身份/过期策略参数化）、notes 15、tasks 16、triggers 34、events 30（含四轮 pull proof 模式矩阵 3 例改写）、jobs/recall/泄漏 11、surface gate 34（29 + 四轮 3 + 五轮 2）、迁移/备份 19、kill-9 新增 9、契约 mock server +11（37 总数中 Phase 3 为 26）。

## 3. 需求 → 实现 → 测试 → 实测结果

### 3.1 Note 生命周期（P4-NOTE-01 / §10）

| 需求 | 实现 | 测试 | 实测 |
| --- | --- | --- | --- |
| §10.2 状态机 + 终态无出边 | `domain/note.py::NOTE_TRANSITIONS` | `TestStateMachineProperties`（200 种子合法/非法走查） | 非法迁移稳定 `invalid_request`/`invalid_state_transition` |
| 复查扫描：唤醒/重复关联/晋升/延展 | `NoteService.review_sweep`（确定性、同刻幂等） | `test_snooze_wake_and_duplicate_association` 等 4 例 | 唤醒 1、`possible_duplicate` link 建立、promise 一次性晋升（重扫 0）、review 前移 |
| 保留：无删除路径 | 扫描无删除代码路径 | `test_pinned_notes_are_never_deleted_by_review` | 复查后存活数不变 |
| Promotion seam | task 物化 / claim+episode 记录类型 | `TestNotePromotion` 3 例 | proposed Task + link + id 回填；claim/episode id 为 NULL 且无对应表 |
| 幂等/CAS/审计/Outbox 同事务 | 幂等 runner + pointer CAS + `note.changed` | `test_expected_revision_cas_and_idempotent_replay` 等 | 重放 revision 不变；乱序 expected 稳定 `revision_mismatch` |
| 安全（租户/空间/tombstone） | by-ID 信封 + 列表空值语义 | `TestNoteSecurity` 3 例 | 跨租户/agent 拒绝；agent 级列表不漏 space 记录；tombstone 后不可见 |

### 3.2 Task/Step/Dependency（P4-TASK-01 / §11）

| 需求 | 实现 | 测试 | 实测 |
| --- | --- | --- | --- |
| §11.5 激活特权 | `may_activate` 写事务内校验 | `test_conversation_origin_creates_proposed_and_cannot_self_activate` | conversation 创建=proposed；非特权激活 `access_denied`；admin/explicit_tool 可激活 |
| 状态机全走查 + 终态 | `TASK_TRANSITIONS`/`TASK_STEP_TRANSITIONS` | 200 种子性质 + `test_full_lifecycle_walk` | blocked 不能直接完成（需先解除）；archived 终态 |
| Evidence：外部效果需 committed Observation | `EVIDENCE_RESOURCE_TYPES={observation}`（三轮收紧，artifact 结构性拒绝） + effect_state + **完整 scope_allows 终检**（tenant/agent + space_group/space/session） | `test_completion_of_expected_effect_step_requires_committed_evidence`、`test_foreign_observations_are_never_completion_evidence`、`test_cross_space_observation_is_not_completion_evidence` | 无证据/partial 拒绝；**他 agent（同租户）、他租户、他空间的 committed Observation 同样拒绝**（按 ID 取用须过资源自身作用域，空间级事实对未命名该空间的 Task 视点不可见）；committed 后仍需显式 complete |
| Task 完成需 Step 全终态 | 转换内校验 | `test_task_completion_requires_terminal_steps` | 有未完成 Step 时稳定拒绝 |
| 稳定 key 身份 | `UNIQUE(task_id, stable_key)` | `test_steps_use_stable_keys_and_derived_readiness` | 重复 stable_key `conflict` |
| 环检测 | `would_create_cycle` DFS | `test_cycle_rejected_stably` | 三角/自环均 `task_dependency_cycle` |
| 跨 Task 依赖拒绝 | v1 限制 | `test_cross_task_dependency_rejected` | `invalid_request` |
| readiness 双向派生 | `compute_step_status` 纯函数 | 性质 200 种子 + 集成 2 例 | 加依赖收回 ready；完成/跳过前驱释放后继 |
| 50 并发 CAS | 并发仲裁规则（ADR-0012 §2） | `test_fifty_threads_same_expected_revision_one_winner` | ok=**1**、mismatch=**49**、revision 行=2、`task.active` 审计=1、transition outbox=1、watermark 恰好 +1 |

### 3.3 Trigger 与 Occurrence（P4-TRIGGER-01 / §11.4、§17）

| 需求 | 实现 | 测试 | 实测 |
| --- | --- | --- | --- |
| 五 kind 受限 spec | 字段白名单 + 9 操作符 + comparand 必填 | `test_non_declarative_specs_rejected`（9 组负向）+ unknown tz | eval/shell/cron/缺字段/缺值全部 `invalid_request` |
| Occurrence 身份（revision+时刻+key） | UNIQUE 四列 | 100 次重复扫描门禁 | 2 个到期时刻 → **2 Occurrence + 2 逻辑 Event**，100 轮零新增 |
| at_time 一次性和 at_time 后不再触发 | next_fire 置空 | `test_at_time_fires_once_then_never_again` | 首扫 1/1，后 5 轮 0/0/0 |
| misfire 显式记账 | plan_catch_up 复用 | `test_misfire_grace_skips_old_occurrences_with_reason` | `skipped` + `misfire_grace_exceeded` |
| 启停与 spec revision 分叉 | set_trigger_enabled 新 revision | `test_disable_stops_scanning_and_revision_change_forks_identity` | 停用零发生；重启用 revision=3，历史 occurrence 全属 revision 1 |
| observation/state/transition 触发 | 扫描内确定性求值 + **空间终检**（越界观察记 `observation_out_of_scope` skipped ledger） | `TestConditionKinds` 3 例 + `TestAuditRegressions` 8 例 | 精确匹配各 1 次；重放为零操作（ledger 排除后不进批次）；他空间观察/被观察聚合他空间/被观察 step 已 tombstone 均 fail closed 零发射 |
| DST/时区矩阵 | 复用受限日程语法 | `test_timezone_matrix_covers_dst_edges` | UTC/Berlin/NewYork/Tokyo(+Lord_Howe) 缺失/重复/前跳严格唯一递增 |
| 休眠/重启 catch-up | 持久 next_fire + 有界批 | `test_sleep_then_catch_up_bounded`、`test_restart_recovers_from_persisted_marker` | 10s 沉睡 → 3 enqueued + 显式 skipped；新服务实例恢复不重发 |
| 时钟回拨 | ledger 唯一键 | `test_clock_back_slew_does_not_duplicate` | 回拨 3 轮重扫零新增，事件恒 1 |

### 3.4 CognitiveEvent 投递（P4-EVENT-01 / §12）

| 需求 | 实现 | 测试 | 实测 |
| --- | --- | --- | --- |
| pending 保持 + 拉取投递（sweep 先行；lease 戳 + attempts；过期 lease 即 fence） | pull_in_tx（lease 戳 + attempts；`_holder_is_fenced` 用扫描时刻判 lease 存活） | `test_pending_without_holder_stays_pending_then_pull_delivers`、`test_not_due_event_is_not_pulled` | 未到期不投递；投递后 delivered/attempts=1 |
| ACK 幂等（100 次） | delivered→acknowledged + 首次 ack_id | `test_ack_is_idempotent_hundred_times` | 101 次 ACK 同一 ack_id、恰 3 revision、attempts=1 |
| ACK 前置状态 | 仅 delivered 可 ACK | `test_ack_requires_delivered_status` | pending 直接 ACK `invalid_state_transition` |
| Fence 重投同一 id（20 轮） | redeliver_after_fence + lease epoch | `test_twenty_fence_scenarios_redeliver_same_id` + `test_fenced_delivered_event_requeued_by_sweep_then_redelivered_same_id` | 20 轮抢占全部同 id 重投；最终恰 20 事件零重复；**清扫路径**（非直调）同样把 horizon 未到、attempts 未满的被 fence 事件回 pending 并同 id 重投 |
| 过期/取消/摘要 | expire_sweep + 有界 summary | `test_expired_event_never_resurrects` 等 3 例 | 过期后不可 ACK/不投递；30 个过期折叠为 1 个 summary（count=30） |
| 尝试上限为服务配置 + 首次投递存活 | `expiry_candidates(max_attempts=...)`；**pull 先清扫后投递** | `test_configurable_max_attempts_expires_via_sweep`、`test_first_delivery_at_attempt_cap_is_returned_alive_and_ackable` | max_attempts=1 时单次未 ACK 投递在下次清扫过期（SQL 不再硬编码 50）；首次 pull 返回的事件库中为 delivered 而非 expired，ACK 正常落账 |
| 安全 | 租户/agent/tombstone 终检；**空间信封在 SQL 内**（LIMIT 只约束可持有事件，不可见事件不饿死可见尾部） | `TestEventSecurity` 2 例 + `TestPullAuthorization` 4 例 + `TestAuditRoundTwo` 5 例 | 跨租户 fail closed；tombstone 不可见不可拉；pull 空间信封（窄信封不投递留 pending、宽信封正常投递、50 不可见不饿死第 51 条可见）；伪造 lease id / lease 无 epoch / 无 lease 的 epoch / 他 agent 的活跃 lease / 时间性过期 lease 全部拒绝 |

### 3.5 安全负向门禁（P4-SAFETY-01）

| 需求 | 测试 | 实测 |
| --- | --- | --- |
| Delivered/ACK/Expired/失败投递不改变完成状态 | `test_delivered_ack_expired_do_not_complete_task_or_step`、`test_failed_delivery_leaves_event_requeued_not_completed` | 四场景后 Task≠completed、Step≠completed、证据引用为空；失败投递回 pending 且同 id |
| 事件不是完成证据 | `test_event_evidence_is_not_completion_evidence` | cognitive_event 作为证据被结构性拒绝（`evidence resource type not allowed`） |
| 授权失败是请求级错误 | notes/tasks/events 安全矩阵 | 全部 AccessDenied/InvalidRequest，零数据返回；recall 授权语义沿用 Phase 3（授权先于 watermark 早退） |
| 泄漏扫描 | `test_canary_absent_from_jobs_audit_and_outbox` + mock server `Idempotency-Key` 强制 | canary 不入 audit details/outbox payload/last_error_code；所有 Phase 4 写端点缺幂等键 400 |

### 3.6 Recall 集成与 Phase 6 边界

| 需求 | 测试 | 实测 |
| --- | --- | --- |
| due tasks 高优先级路由 + pending_event_ids | `test_due_tasks_route_and_pending_event_ids` | tasks 路由 completed、仅 due 任务入选且排序第一、pending_event_ids 精确匹配 |
| rehydrate 终检 | `test_completed_task_dropped_by_rehydrate` | 已完成任务被剔除 |
| 边界：无 /v1/recall、无 search | `test_phase6_boundary_no_recall_contract_paths` | OpenAPI 无 recall/search 路径 |
| Phase 3 三路由不受影响 | 既有 test_recall.py 全绿 | 未注入 tasks 服务时语义不变 |

### 3.7 Job Handler / Migration / 备份

| 需求 | 测试 | 实测 |
| --- | --- | --- |
| note.review 经真实 worker 执行且幂等 | `test_note_review_job_runs_through_worker_idempotently` | 首轮晋升、重放 revision 不变 |
| task.trigger_scan 经 worker + 指针检查全绿 | `test_trigger_scan_job_creates_events_via_worker` | 事件创建、全部 task.changed 完成、零 dead |
| 未知 payload version fail closed | `test_unknown_payload_version_rejected_at_enqueue` | 入队即拒（更高版本永不入队） |
| 启用 ⊆ handler + 禁止 coalesce | `test_enabled_kinds_have_handlers`、`test_coalescing_stays_forbidden_for_phase4_kinds` | 10 个启用 kind 全有 handler；5 个新 kind 拒绝 coalesce key |
| 0001–0004 字节不变 | `test_published_bytes_match_head_baseline` | 与 HEAD `b4587b1` 逐字节一致 |
| Phase 3 数据 4→5 | `test_phase3_data_upgrades_intact` | 数据保留、13 张新表、不变量零违例 |
| 空库=5 / 0005 元数据 | `test_empty_database_installs_all_five`、`test_0005_is_online_safe_with_version_window` | version=5；online_safe/min_app=0.5.0/recovery=none |
| 伪造指针/引用拒绝 | `test_forged_pointers_rejected`（7 组） | 六类指针 + FK + occurrence→event 全拒 |
| 备份三轮往返 | `test_backup_restore_roundtrip_keeps_phase4_rows` | schema=5、不变量零违例、行存活 |
| Phase 1 备份仍可恢复 | `test_phase1_backup_still_restores` | 通过 |

## 4. 量化验收基线证据

| 基线 | 实测 |
| --- | --- |
| 状态机/DAG 性质每项 ≥200 固定种子 | Note/Task/Step/Event 各 200 合法+非法走查、DAG 200（Kahn 复核）、ready 派生 200、条件求值 200、occurrence 身份 200、review 延展 200、过期策略 200（`test_phase4_domain.py` 3614 参数化用例） |
| 50 并发相同 Expected Revision | ok=1、mismatch=49（稳定码）、revision 行=2、`task.active` 审计=1、transition outbox job=1、watermark 恰 +1（创建 watermark 之外仅胜者推进） |
| 同一 Trigger Revision/时刻 ×100 | 2 到期时刻 → 2 Occurrence + 2 逻辑 CognitiveEvent，100 轮零新增 |
| 同一 Event 投递/ACK ×100 | 同一 ack_id、3 revision（created→delivered→acknowledged）、attempts=1 |
| 时区矩阵 | UTC、Europe/Berlin、America/New_York、Asia/Tokyo + Australia/Lord_Howe；DST 缺失（skip/postpone）、重复（fold first/second）、前跳唯一递增、回拨零重复、10s 休眠有界 catch-up（3 enqueued + 显式 skipped）、重启恢复不重发 |
| 20 次 Holder fence/崩溃恢复 | 20 轮 epoch 抢占全部同 id 重投新 Holder；最终恰 20 事件零重复 |
| 负向完成门禁 | delivered/acked/expired/失败投递四场景 Task/Step 状态与证据零变化；cognitive_event 证据结构性拒绝 |
| Kill-9 | 9 边界（task_transition×4、trigger_scan×3、event_ack×2）× 20 = **180 次**真 SIGKILL（累计 Phase 2/3 的 340 次之外）；恢复后 `verify_database_invariants` 零违例、重放收敛、退出码必为 -9 |

## 5. Migration 证据

- `0005_phase4_notes_tasks_events.sql`，头部 `-- iris: online_safe=true lock_ms=200 min_app=0.5.0 max_app= recovery=none`。
- SHA-256：`32ecc6f37f32b27443d03eb33c5365c4a0a9895b42c7df24fad6aa4a5a46f9e2`（与库内 `schema_migrations` 记录一致）。
- 兼容窗口：SUPPORTED_SCHEMA_MIN=4，MAX=5（0.5.0 可读 Phase 3 库并前向迁移；Schema 3 需先经 0.4.0 二进制——MigrationRunner 本身支持一次走完，窗口只约束 Ready）。
- 0001/0002/0003/0004 对 HEAD `b4587b1` 逐字节一致。
- 回退策略：ADR-0012 迁移影响节（停 handler → 保留 ledger → 兼容二进制/备份恢复；不反推完成态）。

## 6. 契约证据

- `contracts.json`：contract_version 1.2.0→**1.3.0**、schema_version 4→**5**、package_version **0.5.0**；capabilities 追加 `notes.v1`、`tasks.v1`、`cognitive-events.v1`；错误码追加 `task_dependency_cycle`（§23.4 冻结清单内首次启用）。
- OpenAPI 新路径（13）：`/v1/notes`（GET/POST）、`/v1/notes/{note_id}`（PATCH）、`/v1/notes/{note_id}:archive`、`/v1/notes/{note_id}:promote`、`/v1/tasks`（GET/POST）、`/v1/tasks/{task_id}`（PATCH）、`/v1/tasks/{task_id}:transition`、`/v1/tasks/{task_id}/steps`、`/v1/tasks/{task_id}/steps/{step_id}:transition`、`/v1/tasks/{task_id}/dependencies`、`/v1/tasks/{task_id}/triggers`、`/v1/cognitive-events`（GET，含 pull/lease 参数）、`/v1/cognitive-events/{event_id}:ack`。全部写端点要求 `Idempotency-Key` 头，mock server 同步执行（缺键 400）。
- fixtures 35→**53**（valid 7 / invalid 8 / forward 3 新增；目录 23 valid / 21 invalid / 9 forward；含审核修订新增的 `task-create-request-minimum` 最小请求面 fixture）；JSON Schema 2020-12 新增 14 份；`tests/contract/test_json_schemas.py` 以生成 schema 严格校验全部 valid/invalid fixture；Python/TS 校验器对 view 枚举保持前向宽松（forward/ 目录专测），请求枚举严格。
- mock server：13 端点 + 校验分支（conversation origin 保持 proposed 且激活 403 `access_denied`、自依赖 409 `task_dependency_cycle`、cron kind 400、pull 戳 lease/epoch、ack 幂等）；Python SDK 12 个新客户端方法（create/list/update/transition/step/dependency/trigger/pull/ack），TS SDK 同步 12 方法 + lockfile 0.5.0。
- `tools.generate_contracts --check` 与 compatibility check 无漂移。无 FastAPI 传输层（沿用应用层契约 + mock server 模式，无新 ADR 需求）。

## 7. 已知限制

1. **扫描粒度**：`task.trigger_scan` 每 agent 处理有界触发器批（≤500×2 类）与观察批（≤500/触发器）；超大积压跨多个 tick **收敛**（时间类按 next_fire、条件类按 last_scan 陈旧度排序轮转；observation 批次截断时游标只推进到已读行的 committed_us−1，ledger 排除使 LIMIT 只约束新行——§8 R6/R7 回归证实 600 条两批积压两轮全部收敛）。pull 与事件列表的空间信封在 SQL 内执行（§8.1 R6），信封外事件不占用批次；tombstone 复查仍在应用层（forget 稀疏，不构成饥饿面）。
2. **state_condition 求值键**：条件对状态值 JSON 对象的 `value` 键求值（无该键则整体比较）；嵌套路径比较不支持——需要时以新 ADR 扩展字段路径语法。数值比较语义：lt/le/gt/ge 要求数字 comparand（创建时校验），活值不可比较时判定不匹配。
3. **历史缺口已关闭**：真实 HTTP 传输由 [Phase 10](../development/phase-10-consolidation-reflection.md)交付；本报告的原始测试仍是当时应用层/mock 范围。
4. **tasks recall 路由需显式注入**：orchestrator 不注入 TaskService 时保持 Phase 3 三路由语义（兼容默认），宿主接入时显式构造。
5. **trigger origin 声明由调用方给出**：conversation/background origin 无论如何不能激活（安全不变量），但 explicit_tool origin 的声明信任调用方凭据（服务端认证的 app instance）。
6. **无跨 Task 依赖 / 无 cron 语法**：§11.3 与 ADR-0009 §5 的既定边界。
7. **§25.3 的跨阶段覆盖尚未闭合**：本阶段及五轮修订闭合的是在线 Observe 与 Phase 4 Note/Task/CognitiveEvent；Phase 3 Focus 写仍没有 lease proof 请求面，完整 `/v1/recall` 又由模块明确留给 Phase 6。若在这些接口补齐前把 `required` 宣称为全应用平面强制策略，Focus/Recall 会成为既有旁路；应以独立跨阶段 ADR 同步应用层、契约、mock server 与双 SDK，而不是在 Phase 4 局部暗改旧协议。

## 8. 审核修订与回归证据（2026-09-01）

初版全绿后的人工审核提出 2 P0 / 5 P1 / 3 P2，经逐项代码核实**全部属实**并按下表修复；语义决策并入 ADR-0012 §9。`make ci` 复跑全绿（数字见 §2 更新）。

| # | 级别 | 审核发现 | 修复 | 回归测试（实测） |
| --- | --- | --- | --- | --- |
| 1 | P0 | `_validate_evidence` 丢弃 task 上下文，按全局 ID 取 Observation 只查 committed/tombstone，跨租户/Agent 的 committed Observation 可充当证据 | 取回后断言 `observation.tenant_id == task.tenant_id && observation.agent_id == task.agent_id` | `test_foreign_observations_are_never_completion_evidence`：他 agent + 他租户两组均 `invalid_request`，Step 保持 in_progress |
| 2 | P0 | `pull_in_tx` 只授权 Agent，逐事件无空间信封；lease_id/epoch 未验证活跃性与属主 | pull 循环内执行 `event.space_id ∈ access.allowed_space_ids` 过滤（与 by-ID 同规则）；新增 `_require_pull_lease`：未知/跨租户/跨 agent/他 app/非活跃/epoch 不符全部 `access_denied` | `test_event_outside_space_envelope_is_not_pulled`（窄信封不投递留 pending、宽信封正常投递）；`test_pull_rejects_forged_or_foreign_lease`；`test_pull_rejects_other_agents_lease` |
| 3 | P1 | `expiry_candidates` 只返回已过期或 attempts≥50 的事件，fence 分支不可达；硬编码 50 废掉可配置 max_attempts | 新增 `fence_candidates`（delivered、未 ACK、持有 lease）先行 fence pass；过期候选尝试上限改为服务 `max_attempts` 参数 | `test_fenced_delivered_event_requeued_by_sweep_then_redelivered_same_id`（仅经 pull/sweep，同 id 重投，零重复）；`test_configurable_max_attempts_expires_via_sweep`（max_attempts=1 生效） |
| 4 | P1 | `parse_trigger_schedule_spec` 忽略 kind/dst_missing/dst_ambiguous，声明策略未生效 | 委托 Phase 2 `parse_schedule_spec`（裸 spec 形态保持兼容推断） | `test_dst_policies_declared_in_spec_are_actually_applied`：Berlin 2026-03-29 缺失 02:30 — skip→次日 02:30、postpone→当日 03:30；dst_ambiguous=second 解析进 spec |
| 5 | P1 | comparand 强制字符串 + 有序比较禁字符串 ⇒ lt/le/gt/ge 无合法输入 | `value` 允许 str/数字；有序操作符创建时要求数字；求值期不可比较活值为 False 不抛错 | `test_numeric_comparand_end_to_end`：数字 lt 创建+触发；字符串 comparand 创建拒绝；`evaluate_state_condition(lt 5, "6")` 为 False |
| 6 | P1 | 观察扫描处理 ≤500 后把 last_scan_us 推到 now ⇒ 第 501 条起永久跳过；trigger 查询固定排序无游标 ⇒ >500 条尾部饥饿 | 批次截断时游标推进到 max(committed_us)−1；ledger 已记观察在 SQL 内 `NOT EXISTS` 排除（LIMIT 只约束新行）；`triggers_for_scan` 拆双查询（时间类按 next_fire、条件类按 last_scan 陈旧度） | `test_observation_backlog_converges_past_batch_limit`：500 同刻 + 100 后刻 → 两轮 500+100 全部发射；`test_due_time_trigger_not_starved_by_condition_mass`：600 条件触发器下到期时间触发器仍被扫描 |
| 7 | P1 | task_transition 信任 condition 内任意 task/step ID；observation/state 扫描无 tombstone/expiry 终检 | 被观察聚合复核同 tenant+agent 且未 tombstone（失败 fail closed）；tombstoned observation 记 `observation_tombstoned` skipped ledger；过期 state 记录读作 absent | `test_task_transition_condition_watching_foreign_agent_never_fires`；`test_tombstoned_observation_is_skip_ledgered_not_fired`；`test_expired_state_record_reads_as_absent` |
| 8 | P2 | JSON Schema 仅要求 agent_id+title，服务端有默认，但双 SDK 拒绝省略 origin（Python 还要求 owner_kind），mock server 亦 400 | Python/TS 校验器 origin/owner_kind 改为可选；mock server 缺省 origin=explicit_tool；新增共享 fixture `valid/task-create-request-minimum` | `test_task_create_accepts_the_contract_minimum`（mock server 最小请求 200 且 status=active）+ fixtures manifest（Python/TS/JSON Schema 三方同测） |
| 9 | P2 | 被 skip 的一次性 at_time 不清 at_time 标记，每个 tick 永久重扫同一 occurrence | decisions 非空即置 `next_fire=None`（fired 或 skipped 均退休） | `test_skipped_one_shot_at_time_retires_its_marker`：首扫 skipped=1，后 3 轮 0/0，marker 为 NULL，occurrence 恰 1 |
| 10 | P2 | job 测试含 `or True` 无条件通过断言 | 改为累计 `dead == 0` 的真实断言 | `test_trigger_scan_job_creates_events_via_worker` 复跑通过（真断言生效） |

连带修订：`TestConditionKinds` 观察重放断言从"absorbed=1"演化为"零操作"（ledger 排除后重放不再进入批次，occurrence 计数仍为 1——幂等语义不变、表现更干净）；`fence_candidates`/`expiry_candidates(max_attempts)`/`for_trigger_scan(trigger_id, trigger_revision)` 进入仓储与端口协议。

### 8.1 二轮审核修订与回归证据（2026-09-01）

二轮复核确认一轮 10 项中的 4/5/6/8/9/10 已完整关闭；1/2/3/7 存在未覆盖边界，另发现 3 个事件生命周期缺陷。全部修复并回归（ADR-0012 §10）：

| # | 级别 | 审核发现 | 修复 | 回归测试（实测） |
| --- | --- | --- | --- | --- |
| 1 | P0 | Evidence 终检只比 tenant+agent：同 Agent 他空间的 committed Observation 可完成本空间 Task 的 Step | `_validate_evidence` 升级为完整 `scope_allows(observation, task)`（space_group/space/session 全维度；向下可见性模型下空间级事实对未命名该空间的视点不可见） | `test_cross_space_observation_is_not_completion_evidence`：他空间 committed Observation `invalid_request`（"outside the task's scope"），Step 保持 in_progress |
| 2 | P0 | 条件触发器跨空间传播：他空间 Observation 触发本空间 Task 的事件；task_transition 只查 tenant+agent 且不查被观察 Step 的 tombstone | observation 候选逐条 `scope_allows(observation, task)`，越界记 `observation_out_of_scope` skipped ledger（退出候选集，无批次饥饿）；task_transition 终检升级为 tenant+agent+scope_allows+task 与 step 双 tombstone | `test_cross_space_observation_never_fires_a_scoped_task_trigger`（skipped=1、零事件、ledger 可审计）；`test_task_transition_watching_foreign_space_task_never_fires`；`test_task_transition_watching_tombstoned_step_never_fires` |
| 3 | P0 | lease 校验不完备：有 lease_id 可省 epoch（事件被记为 `delivered_lease_epoch=None`，fence 无法仲裁）；不查 `expires_us`，时间性过期 lease 仍可领取 | `_require_pull_lease(now_us=...)`：lease_id 必须携带 epoch（否则 invalid_request）；`expires_us <= now` 即 `access_denied`（即使行仍 active） | `test_pull_rejects_forged_or_foreign_lease`（无 epoch 拒绝 + 未知 lease 拒绝）；`test_pull_rejects_time_expired_lease`（行仍 active 但过期即拒绝，事件留 pending） |
| 4 | P1 | `_holder_is_fenced` 丢弃 now_us：lease 时间性过期而 surface expiry 未跑时事件滞留 delivered | fence 判定使用扫描时刻：`expires_us <= now` 即 fenced，事件回 pending、同 id 重投 | `test_time_expired_lease_is_fenced_by_sweep_and_redelivered`：过期 61s（行仍 active）→ sweep 回 pending（expired=0）→ 新持有者同 id 重投 |
| 5 | P1 | pull 先投递后清扫：max_attempts=1 时首次 pull 返回的事件在库中已是 expired，ACK 必然 `invalid_state_transition` | pull 内清扫移到投递循环之前；本 call 投递的事件永不在同一 call 内过期（fence 重投事件可在同 pull 被新持有者领取） | `test_first_delivery_at_attempt_cap_is_returned_alive_and_ackable`：首次 pull 返回 delivered/attempts=1、expired=0，ACK 成功 |
| 6 | P1 | SQL 先 LIMIT 再应用层过滤空间：50 条不可见事件占满批次时第 51 条可见事件永久 pending | 空间信封下沉 SQL（`space_id IS NULL OR space_id IN (信封集)`，与 `require_same_tenant_agent` 同语义）——LIMIT 只约束该 app 可实际持有的事件 | `test_invisible_events_do_not_starve_the_visible_tail`：50 条不可见（更早 scheduled）+ 1 条可见 → 窄信封 pull 恰返回可见事件并 delivered |
| 7 | P1 | 事件列表两处遗漏：全空请求 scope 的 scope_allows 结构性排除所有空间级事件；底层查询硬编码 pending/delivered，OpenAPI 的 acknowledged/expired/cancelled 永远查不到（recall `pending_event_ids` 同源受影响） | 列表与 recall 改用访问信封；statuses 透传 SQL（`pending_events(statuses=..., allowed_space_ids=...)`） | `test_listing_covers_acknowledged_expired_cancelled_and_space_scoped`：四种状态各自返回真实数据、空间级事件对注册空间可见；`test_due_tasks_route_and_pending_event_ids` 扩展：空间级 pending 事件亦进入 `pending_event_ids` |
| 8 | P1 | ACK 不验证投递持有者：同 Agent/空间的任意 app 实例可 ACK 他人 lease 持有的事件，吞掉投递并阻断 fence 重投 | `_require_ack_holder`：带 lease 的投递只能由记录在案的持有者 app 实例 ACK，且记录 epoch 必须仍匹配（幂等重放路径不受影响） | `test_only_the_recorded_delivery_holder_may_ack`：邻居 app 实例（同 agent/空间）`access_denied` 且事件保持 delivered；持有者 ACK 正常 |
| 9 | P2 | 触发器创建事件绕过注入时钟与 TTL 配置（SystemClock + 默认 TTL，测试时钟与服务级 ttl_us 不生效） | `create_internal(now_us=..., ttl_us=...)`；TaskService 新增 `event_ttl_us` 配置，trigger scan 传入扫描时刻与服务 TTL | `test_trigger_events_use_injected_clock_and_service_ttl`：`expires_us == 扫描时刻 + 自定义 TTL`（≠ 默认 7 天），推进 2×TTL 后 sweep 将其过期（默认 TTL 下不会） |

连带修订：`_event` 测试助手显式传 `now_us`（暴露并修正了"事件 TTL 落在 SystemClock 时间线"的测试盲区）；三处观察触发测试的 Task 对齐到观察所在空间（作用域对齐是修复后的合法形态，原"agent 级 Task + 空间级观察"组合正是被关闭的跨空间通道）；`TestSweepFenceRedelivery` 中段断言演化为"fence 重投与再投递发生在同一 pull"（先清扫后投递的更强语义）；`pullable_events(allowed_space_ids)`/`pending_events(statuses, allowed_space_ids)` 进入仓储与端口协议；mock server pull 端点同步执行"lease_id 必须携带 lease_epoch"。

### 8.2 三轮审核修订与回归证据（2026-09-01）

三轮复核确认 4 个 P0、3 个 P1，全部核实并修复（ADR-0012 §11）：

| # | 级别 | 审核发现 | 修复 | 回归测试（实测） |
| --- | --- | --- | --- | --- |
| 1 | P0 | `EVIDENCE_RESOURCE_TYPES` 含 artifact 但 `_validate_evidence` 只验证 observation——伪造 `{"resource_type": "artifact", "resource_id": "does-not-exist"}` 即可完成带 expected_effect 的 Step | 证据类型收紧为 `{observation}`：artifact 被结构性拒绝（allowlist 外即 invalid_request），Task 与 Step 两条 completion 路径共用该允许集；ADR 记录 artifact 需后续阶段具备 canonical 校验后才能回归 | `test_artifact_refs_are_structurally_rejected_as_evidence`：不存在/任意格式的 artifact ID 均 `evidence resource type not allowed`，Step 保持 in_progress 且证据引用为空，Task 级 completion 同样拒绝、revision 不落账 |
| 2 | P0 | SurfaceMode=required 时 Phase 4 应用平面写（Note/Task/Step/Dependency/Trigger、pull/ACK）无 lease proof 仍成功 | **三轮当时实现，已由 §8.3/§8.4 加固**：三服务注入可选 `surface`；13 个公开写接入 Phase 2 `check_online`；契约/OpenAPI/JSON Schema/双 SDK/mock server 同步承载 lease proof（11 个请求 schema + 新 fixture）。当轮“只在写回调校验、proof 进入指纹”的做法不再代表最终实现；最终规则是授权先行、缓存前校验、业务写事务内复核，proof 不进入逻辑指纹 | `test_phase4_surface_gate.py` 29 例：13 操作 × off/required(-proof) 参数化全矩阵 + advisory 不阻断且审计含 `no_active_lease` + 缺 epoch/时间性过期/旧 epoch/邻居 app/合法 Holder 五类 proof 判定 + 维护平面（review sweep/trigger scan/事件清扫）在 required 下不受门禁 |
| 3 | P0 | 过期清扫按 tenant+agent 汇总却把摘要固定创建成 agent scope，首过期事件 ID 写入 object_id——Space A 拉取者可收到 Space B 隐藏事件的摘要 | 过期事件按 `(tenant, agent, space_group, space, session)` 分组；摘要继承组内精确 scope，object_id 与全部 `summary_of` links 仅同组；阈值按组计算；摘要 ID 列表上限（50）与 bounded 行为不变 | `test_expiry_summary_stays_inside_its_scope_group`：21 个 Space B 过期 → 摘要落在 Space B scope，Space A pull/列表零泄漏，Space B 恰见该摘要；`test_expiry_summaries_never_stitch_scopes_past_the_threshold`：agent/space/session 三组各 21 → 恰 3 个摘要零串组；10+15 两组均低于阈值 → 零摘要 |
| 4 | P0 | Recall 针对 Space A 时 `pending_event_ids` 按整个 AccessContext 信封读取，包含 Space B 的事件 ID | 新增 `pending_event_ids_for_request_scope`：以请求的 agent/space/session 构造正式向下可见规则（agent 级可进、匹配空间可进、他空间/他会话不进），SQL 内 LIMIT 前过滤；`/v1/cognitive-events` 列表保持信封语义 | `test_pending_event_ids_follow_the_request_scope_matrix`：agent 级 + 双空间 + 双会话五事件，Space A 请求（无会话）恰得 {agent, spaceA}，Space A+s1 恰得 {agent, spaceA, s1}，spaceB/s2 永不泄漏 |
| 5 | P1 | `_require_ack_holder` 只比 app 实例与 epoch——lease 已过 expires_us、draining/released/expired 或已非当前 active 时旧 Holder 仍可 ACK | 存活验证升级：同事务 now_us、tenant/agent/持有者/epoch、status=active、`expires_us > now_us`、仍是当前 active lease；失败 fail closed，事件保持 delivered 由 sweep 同 id 重投；ACK 幂等重放不受影响 | `test_ack_rejected_when_lease_expired_in_time_but_row_still_active`：行仍 active 但时间已过期 → ACK `access_denied`（"expired"）、事件保持 delivered → sweep 回 pending → 新 Holder 同 id 重投 → ACK 成功 → 重放同 ack_id 零 revision 增长 |
| 6 | P1 | `pending_events` 先排序 LIMIT、应用层再丢 `expires_us <= now` 的 pending 与 tombstone——前 N 条全死时有效事件永久不可见（列表与 Recall 同源） | pending horizon（仅 pending 状态）与 cognitive_event tombstone 排除进入 pull/列表/ recall 查询的 SQL；信封/请求 scope/status/有效期/tombstone 全部先过滤再 ORDER/LIMIT | `test_logically_dead_events_do_not_starve_the_visible_tail`：50 条过期 + 50 条 tombstone 均排在前面，列表（limit=50）与 pull 都返回唯一有效尾部；`test_pending_event_ids_not_starved_by_logically_dead_rows`：recall 的 50-id 上限同样不被饿死 |
| 7 | P1 | pointer 已推进到 delivered 后 `DeliveryBundle` 仍返回循环开始时的 pending current，搭配 delivered revision 形成不一致结果 | CAS 成功后重读 `tx.events.get(id)` 与当前 revision 行再加入 bundle | `test_pull_returns_post_cas_current_with_delivered_revision`：current 与 revision 一致为 delivered/revision+1，attempts=1、lease id/epoch、last_delivery_us 与本次投递戳记完全一致；CAS 失败分支维持 skip |

连带修订：`require_surface_online` 共享门禁进入 `write_support`（规则仍只在 Phase 2 `check_online` 内）；`pending_events(now_us, request_scope)`、`pullable_events` 的 tombstone 排除进入仓储与端口协议；`DeliveryBundle` 新增 advisory `lease_warning` 字段；契约重生成（additive，兼容快照通过）；Python/TS SDK 十个请求校验器新增可选 lease proof 形状校验；mock server 13 个 Phase 4 写端点解析并校验 lease proof 形状；新共享 fixture `valid/task-create-request-with-lease-proof`。

### 8.3 四轮审核修订与回归证据（2026-09-01）

四轮复核确认三轮 7 项已落地，新发现 2 个 P0、2 个 P1（SDK Python/TypeScript 各一）、2 个 P2，全部核实并修复（ADR-0012 §12）：

| # | 级别 | 审核发现 | 修复 | 回归测试（实测） |
| --- | --- | --- | --- | --- |
| 1 | P0 | `check_online` 只比对 lease_id/epoch 不校验请求方 app 实例，而 `current()` 向任意获准该 Agent 的邻居实例返回完整 LeaseView——邻居读取 proof 后在 required 下成功创建 Note（"仅持有者可知"前提不成立） | **四轮当时实现，已由 §8.4 收紧**：`check_online` 当轮新增可选 `app_instance_id` 并在具名调用时绑定 Holder；五轮将身份改为在线调用的必填参数并补齐 Observation，彻底删除匿名旁路。`current()` 暴露面保留（proof 只在其持有者手里是凭证） | `test_neighbor_cannot_replay_an_exfiltrated_proof`：邻居经 `current()` 读到 live proof（断言 lease_id/epoch 可见）后，required 下 note/task 创建与 pull 全部 `lease_fenced`；advisory 下创建放行但审计 details 记 `not_lease_holder`；`test_required_rejects_incomplete_expired_stale_and_neighbor_proofs` 第 4 步改为邻居 pull 断言 `LeaseFencedError` |
| 2 | P0 | 门禁在 `_execute_*` 回调内执行，幂等缓存命中（completed + 同指纹）不调用回调——lease 过期后同 proof/同 key 仍成功回放；换新 proof/原 key 得 `idempotency_key_reused` 而非"重新过闸"（三轮 ADR 声明与实测不符） | **四轮完成缓存前校验，五轮 §8.4 再补事务内复核**：11 个 Phase 4 幂等写（Note 3、Task 7、ACK）先授权、再 gate-before-cache；缓存未命中则在业务写事务内复核。Observation 同步采用相同规则。lease proof 是逐调用凭证，不进入逻辑指纹——required 下缓存回放也需活 lease，合法 lease 轮换可回放，异载荷仍为 key reuse | `test_gate_runs_before_the_idempotency_cache`：required 下首写成功 → lease 时间性过期后同 key+同 proof 回放被 `lease_expired` 拒绝 → 同实例重新 acquire（epoch 递增）后同 key+同逻辑载荷+新 proof 命中缓存（replayed=True、同 note_id）→ 同 key 异载荷仍 `idempotency_key_reused` → notes 表恰 3 行（无重复写入） |
| 3 | P1 | `_require_pull_lease` 在读取 SurfaceMode 前无条件拒绝过期/外借/陈旧 proof——off 携带过期 proof 返回 access_denied、advisory 同样被阻断，违反"off 不校验、advisory 只警告"的模式定义 | 重构为 `_resolve_pull_lease`：配对完整性（id/epoch 缺一）仍是全模式 invalid_request；格式完好但无效的 proof（未知/过期/他 agent/他持有者/epoch 落后）按模式处理——off 忽略（按无 lease 投递、不告警）、advisory 按无 lease 投递并返回稳定原因 `lease_warning`、required 以 `lease_expired`/`lease_fenced` fail closed；投递戳记使用解析后的 proof（无效 proof 永不成为记录持有者） | 三个改写的矩阵测试：`test_pull_proof_pairing_and_unknown_lease_follow_the_mode_matrix`（未知 proof：off 无戳投递→advisory 警告 `stale_lease_id`→required `lease_fenced`）、`test_expired_pull_proof_follows_the_mode_matrix`（时间性过期：off→advisory `no_active_lease`→required `lease_expired`，事件保持 pending）、`test_other_agents_lease_proof_follows_the_mode_matrix`（他 agent lease：off 无戳投递、required `lease_fenced`） |
| 4 | P1 | Python `ack_cognitive_event` 无 lease_id/lease_epoch 参数、也不接受自定义 body——官方 Python SDK 在 required 下无法 ACK | `ack_cognitive_event` 增加 `lease_id`/`lease_epoch` 关键字参数并入请求体；mock server 增加测试专用请求记录通道（`create_server().received_requests`，只观测不校验，安全检查仍在 handler 内） | `test_ack_cognitive_event_carries_the_lease_proof_on_the_wire`：SDK 带 proof ACK 成功，且**记录到的实际请求体**含 `ack_token`/`lease_id`/`lease_epoch`（断言 wire 副本，非独立 validator） |
| 5 | P1 | TypeScript `noteAction` 输入类型不暴露 lease 字段且手工构造 body 丢弃额外字段（archive/promote 在 required 下不可用）；`transitionTask`/`transitionTaskStep` 输入类型同样遗漏；`ackCognitiveEvent` 手工生成无 proof 的 body | 四个方法的输入类型补 `lease_id?`/`lease_epoch?` 并真正写入 wire body；`transitionTask`/`transitionTaskStep` 改为解构剔除 `idempotencyKey`（传输层字段不再泄进 body） | `sdk/typescript/test/wire.test.ts` 3 例（fetch stub 断言实际 body）：noteAction/task/step transition/ack 的 body 均含 lease proof，`Idempotency-Key` 只在 header；`npm run typecheck` + `npm test` 4 例通过 |
| 6 | P2 | ACK 的 advisory `lease_warning` 被丢弃，`cognitive_event.acknowledged` 审计 details 只有 `ack: true`——ADR §11 声明 warning 进审计但只有 Note/Task 做到 | `_execute_ack` 写入**事务内复核所得** warning（`lease_warning`，无值为 null），与 Note/Task 同一纪律；缓存前 warning 只负责即时拒绝/回放门禁，不作为未命中写事务的审计事实 | `test_ack_audit_records_the_advisory_lease_warning`：advisory 无 lease 时 ACK 成功且审计 details `lease_warning == "no_active_lease"` |
| 7 | P2 | ADR 主决策 §2、开发文档 §4.2、验证报告 Evidence 表仍声明 `EVIDENCE_RESOURCE_TYPES={observation, artifact}`，与 §11 和代码（仅 observation）冲突，Phase 5 可能依据错误契约实现 | 三处统一改为 `{observation}` 并指向 §11.1 收敛决策（artifact 需 canonical existence/tenant/agent/scope/privacy/tombstone 校验与其 validator 后回归）；ADR §11.2 两处被四轮推翻的表述加删除线与 §12 修订指针，三轮否决清单中被反转的"门禁在幂等查询之前执行"条目加注反转依据 | 文档一致性：`grep artifact` 在三份文档中不再出现未加限定的 `{observation, artifact}` 声明（三轮 §8.2 历史表保留原文并已由本表第 7 行澄清） |

最终连带状态：notes/tasks 两个服务的 10 个幂等写与 ACK 方法签名不变（lease 参数原位）；`_execute_*` 接收 proof 并在业务事务内复核，审计采用该次复核的 warning；pull 顺序为“配对形状检查 → 授权 → coordinator 缓存前检查 → 业务事务内复核/解析 → 清扫 → 投递”；mock server 请求记录通道仅用于契约测试观测。

### 8.4 五轮审核：Holder 全路径、门禁竞态与授权顺序

第五轮复核以独立临时库注入抢占窗口并检查所有 `check_online` 调用点，发现 2 个 P0、1 个 P1、1 个 P2；修复同时覆盖 Phase 2 Observation 的同源语义漂移（ADR-0012 §13）：

| # | 级别 | 审核发现 | 修复 | 回归测试（实测） |
| --- | --- | --- | --- | --- |
| 1 | P0 | `check_online.app_instance_id` 可省略，Observation 调用恰好省略；邻居读取 Holder proof 后成功写入 committed Observation | app identity 改为在线调用必填；Observation 透传认证实例并在同一写事务复核，匿名绕过路径删除 | `test_neighbor_cannot_observe_with_an_exfiltrated_holder_proof`：`current()` 可见完整 proof，但邻居 Observe 稳定 `lease_fenced`，按 observation idempotency key 查询为零行 |
| 2 | P0 | Phase 4 门禁虽位于缓存前，却与业务写事务分离；在预检返回后、`IdempotencyManager.run` 前抢占 lease，旧 Holder 仍成功创建 Note | 保留缓存前门禁；全部 11 个幂等写在 `_execute_*` 完成访问终检后、任何业务写之前调用 `check_online_in_tx`，以业务写锁封闭抢占/过期窗口；Pull 同样外部预检 coordinator 可用性、事务内复核 | `test_preemption_between_preflight_and_idempotency_cannot_commit`：自定义 runner 在两阶段间真实抢占，旧 proof 得 `lease_fenced`，新 Holder 已生效，目标 title 的 Note 行数为 0 |
| 3 | P1 | Note/Task create 在 `authorize_scope` 之前查询 Surface；无 Agent grant 的请求可用错误差异探测 Required 模式/活跃租约 | create 缓存前先只读验证 Agent、Space、Session 与 Privacy，写事务内重复；授权失败不进入门禁或 idempotency begin | `test_create_authorization_precedes_surface_preflight`：隐藏 Agent 设为 required、调用方无 grant，Note/Task 均 `access_denied`，对应 idempotency record 数为 0 |
| 4 | P2 | ADR §11 仍称四轮实现“在写事务内、访问检查后调用门禁”，与实际缓存前单次校验矛盾；Observation 仍把 lease epoch 放入指纹并让缓存命中绕过门禁 | ADR-0010/0012、开发文档与本报告统一记录双阶段门禁；Observation 同步 gate-before-cache + in-tx，并把 proof 移出指纹 | `test_observation_gate_runs_before_cache_and_proof_is_not_fingerprint_material`：旧 proof 过期后缓存回放拒绝；同实例新 lease、同 key/同载荷返回原 Observation ids，不报 key reuse；Coordinator 故障 Observe/Pull 仍 `not_ready` |

扩展调用链检查确认：全部在线 `check_online` 调用均携带认证 app identity；Note 3、Task 7、ACK 1 个幂等写均同时存在缓存前与事务内门禁；Event cancel 按 §25.3 既定边界继续属于不设闸路径；maintenance sweeps 未被误接入在线门禁。

## 原阶段验收目标

下列门槛从已归档阶段计划移入，保留未被实测证明的要求。它们是当时的验收目标，不能从本报告 Passed/Completed 标签推断逐项均已完成；是否达到须与前文的样本、测试与限制核对。尚未闭合项由 Phase 14 的发布矩阵承接。

> 下列数字为**实测值**，口径见验证报告。

- 状态机与 DAG 性质：Note/Task/Step/CognitiveEvent 状态机与 Dependency 无环/ready 派生各 **200 个固定种子序列**（`tests/unit/test_phase4_domain.py`，3614 个参数化用例全绿）。
- **50 并发相同 Expected Revision**：`test_fifty_threads_same_expected_revision_one_winner` 实测 ok=1、mismatch=49（稳定码）、revision 行=2、task.active 审计=1、transition outbox=1、watermark 恰好 +1。
- **同一 Trigger Revision/计划时刻重复 100 次**：`test_recurrence_scans_are_idempotent_hundred_times` 实测 2 个到期时刻 → 恰好 2 个 Occurrence + 2 个逻辑 CognitiveEvent，100 轮扫描零新增。
- **同一 Event 重复投递/ACK 100 次**：`test_ack_is_idempotent_hundred_times` 实测同一 ack_id、恰 3 个 revision（created→delivered→acknowledged）、attempts=1。
- 时间矩阵：UTC、Europe/Berlin、America/New_York、Asia/Tokyo（+ 澳洲半时区）覆盖 DST 缺失（skip/postpone）、重复（first/second）、前跳、回拨（`test_clock_back_slew_does_not_duplicate`）、休眠（`test_sleep_then_catch_up_bounded`：10 秒沉睡 → 有界 3 个 enqueued + 显式 skipped）与重启 catch-up（`test_restart_recovers_from_persisted_marker`）。
- **20 次 Holder fence 场景**：`test_twenty_fence_scenarios_redeliver_same_id` 实测 20 轮抢占式 fence 全部重投同一 event id，最终恰 20 个事件、零重复。
- 负向端到端：delivered/acked/expired/投递失败四场景下 Task/Step 状态与证据引用零变化；CognitiveEvent 作为完成证据被结构性拒绝。
- 故障注入：新增 9 个边界（task_transition pre_revision/pre_pointer/pre_commit/post_commit、trigger_scan pre_occurrence/pre_commit/post_commit、event_ack pre_commit/post_commit）× 20 = **180 次**真 SIGKILL，恢复后 `verify_database_invariants` 零违例且重放收敛。
