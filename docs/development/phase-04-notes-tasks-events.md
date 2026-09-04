# 阶段 4：Note、Task 与 CognitiveEvent

> 状态：Completed  
> 前置阶段：[阶段 3](./phase-03-recent-state-focus.md)  
> 目标版本：0.5.0（Schema 5）· 完成日期：2026-08-31  
> 架构依据：[§10 Note](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#10-note)、[§11 Task 与前瞻记忆](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#11-tasktaskstep-与前瞻记忆)、[§12 CognitiveEvent](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#12-cognitiveevent-与宿主投递)、[§17 Schedule/Tick](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#17-持久化-scheduletick-与认知时钟)、[§36 阶段 4](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#阶段-4notetask-与-cognitiveevent)、[ADR-0012](../adr/0012-phase4-notes-tasks-events.md)

## 阶段目标

交付低成本捕获、正式计划、前瞻触发和宿主投递闭环。阶段结束时，系统能区分“记录事项”“计划行动”“提醒已送达”和“外部效果已完成”，并在重复投递或宿主切换下保持正确。

## 架构约束

- Note 是独立 Canonical 对象，`review_after` 不是删除时间，Pin/承诺/活动 Task 来源不得自动删除。
- 对话提取的 Task 默认是 `proposed`；只有显式工具、确定性策略或管理员能激活。
- Task/Step 完成需要独立状态转换；外部效果要求实际成功 Observation/Evidence。
- CognitiveEvent 采用 At-least-once；ACK 只表示宿主承担处理责任，不表示 Task 完成。
- Trigger 只允许声明式语法和操作符，禁止任意代码、脚本或隐式工具执行。

## 需求追踪

| 需求 ID | 基线要求 | 工作包 | 验证门禁 | 结果 |
| --- | --- | --- | --- | --- |
| P4-NOTE-01 | Note 独立生命周期、复查、保留与晋升 | 4.1 | 状态机、保留、Schedule 与 Promotion 测试 | 通过（见退出门禁 1） |
| P4-TASK-01 | Task/Step Revision、Evidence、状态转换与依赖 | 4.2 | 性质、并发、环检测和 Evidence 测试 | 通过（见退出门禁 2） |
| P4-TRIGGER-01 | 受限 Trigger、时区、Occurrence 幂等与 Misfire | 4.3 | DST、Catch-up、重复 Tick 与重启测试 | 通过（见退出门禁 3、6） |
| P4-EVENT-01 | CognitiveEvent At-least-once、ACK、重投与过期 | 4.4 | Holder 切换、重复 ACK、过期和摘要测试 | 通过（见退出门禁 4） |
| P4-SAFETY-01 | ACK/投递/失败不得伪造 Task 完成或外部效果 | 4.2–4.4 | 端到端负向契约和审计测试 | 通过（见退出门禁 5） |

## 工作包

### 4.1 Note 生命周期（已完成）

- `domain/note.py`：§10.2 状态机（inbox/pinned/snoozed/archived/promoted/tombstoned；promoted/tombstoned 终态）、内容与分值校验、`note_content_hash`（重复关联哈希，非删除标准）、`extended_review_after`。
- `storage/plans.py::NoteRepository` + `application/notes.py::NoteService`：创建/更新/归档/晋升全部走 Expected Revision CAS + 幂等键，同事务写 Revision、Pointer、Audit、Watermark、`note.changed` Outbox。
- 复查扫描（`note.review` handler）：唤醒到期 snooze（唤醒即执行被推迟的复查：重复关联 + 后续整理同遍完成）、content_hash 重复关联（resource_link，不删除）、follow_up/promise 一次性物化 proposed Task（幂等：已晋升/已关联不再创建）、未解决 Note 前移 review_after。无任何删除路径。
- Promotion seam：`task` 真实物化（proposed Task + `promoted_to` link + id 回填）；`claim`/`episode` 只记录类型，id 留空（Phase 5 属主）。

### 4.2 Task 与 Step（已完成）

- `domain/task.py`：Task 7 态状态机（proposed 只能显式激活或取消；completed/cancelled 只能归档；archived 终态）；Step 8 态（pending/ready 为派生态，手动 →ready 被拒）；`may_activate` 激活特权；`would_create_cycle` DFS 环检测；`compute_step_status` 双向派生；`EVIDENCE_RESOURCE_TYPES = {observation}`（三轮收敛：artifact 结构性拒绝直至 Phase 5 canonical 仓储与其 validator 存在）+ 声明式 condition_spec 白名单（9 操作符，缺 comparand 拒绝）。
- `application/tasks.py::TaskService`：create/patch/transition/steps/dependencies/triggers；激活特权在写事务内校验（`access_denied`）；expected_effect Step 完成必须引用 committed Observation（partial 拒绝）；Task 完成要求全部 Step 终态；同 Task 依赖 + 环稳定 `task_dependency_cycle`；完成/跳过 Step 确定性重算后继 readiness；Note 晋升物化 proposed Task。
- 并发仲裁（ADR-0012 §2）：相同并发请求落败方（status 已是目标且 revision 落后）得到 `revision_mismatch`。

### 4.3 Trigger 与前瞻记忆（已完成）

- 五种 kind 全实现：at_time / recurrence（复用 ADR-0009 受限日程语法 + IANA 时区 + DST policy）/ observation_kind / state_condition / task_transition；spec 白名单外一律 `invalid_request`。
- Occurrence 身份：`UNIQUE(trigger_id, trigger_revision, scheduled_at_us, occurrence_key)`——重复扫描/重启/时钟回拨坍缩同一行；spec revision（含启停）变更使身份分叉。调度位置（next_fire_at/last_scan）NULL-aware CAS 推进，不写 spec revision。
- 扫描（`task.trigger_scan` handler）：misfire/catch-up 复用 `plan_catch_up`（超窗/超限记 `skipped` + reason，不静默丢弃）；每次 Occurrence 与其 CognitiveEvent 同事务原子创建；`cognitive_event.changed` 指针检查随投递写入。

### 4.4 CognitiveEvent 投递（已完成）

- 五态生命周期 + 完整投递 revision 历史（attempts/last_delivery/lease id/epoch/ack_id）。
- 拉取：pending→delivered（lease 戳记 + attempts++）；未到期/已过期/终态不投递；无活动 Holder 保持 pending。ACK 仅 delivered→acknowledged，任意幂等键重放返回首次 ack_id，revision 链不增长。
- Fence 重投：delivered 且未 ACK、记录的 lease 不再授权（epoch 落后/非活跃）→ 回 pending，向新 Holder 重投同一 event id；无 lease 投递不触发 fence（安全网是过期 horizon）。
- 过期：horizon 或尝试上限 → expired；单次清扫超阈值折叠为 `summary.expired_events`（计数 + 有界 id 列表 + links）。取消为终态。
- Recall 集成：`tasks` 路由为最高优先级结构化信号（category priority 0，due 任务 + rehydrate 终检）；`pending_event_ids` 只广播 id（≤50），正文留在事件端点授权之后。

## 数据、契约与回退策略

> 本节记录的是**已落地**结果，不是计划。

- `migrations/0005_phase4_notes_tasks_events.sql`（online_safe=true，lock_ms=200，min_app=0.5.0，recovery=none；SHA-256 `32ecc6f37f32b27443d03eb33c5365c4a0a9895b42c7df24fad6aa4a5a46f9e2`），13 张 STRICT 表；0001–0004 与 HEAD `b4587b1` 逐字节一致。
- Schema 4→5 在线升级（真实 Phase 3 数据升级测试通过）；runtime 兼容窗口 [4,5]；空库安装 =5。
- 备份恢复不变量扩展：六个新聚合指针解析、occurrence→trigger/event 引用、acknowledged 必有 ack_id、terminal 事件不残留 lease；Phase 1–3 备份路径保留。
- 契约 add-only：contract 1.2.0→**1.3.0**、schema 4→**5**、package **0.5.0**；新增 `notes.v1`、`tasks.v1`、`cognitive-events.v1` capability；13 条新路径（GET/POST /v1/notes、PATCH/:archive/:promote、GET/POST /v1/tasks、PATCH/:transition、steps、steps/:transition、dependencies、triggers、GET /v1/cognitive-events、:ack）；错误码新增 `task_dependency_cycle`；fixtures 35→52；OpenAPI 3.1 + 14 份独立 JSON Schema + mock server + 双 SDK（Python 12 方法/TS 12 方法，lockfile 更新）同步。
- 回退（ADR-0012 迁移影响）：先停用 `note.review`/`task.trigger_scan` 与事件领取，保留 Pending Event/Tick/Occurrence Ledger，用兼容二进制或备份恢复；不把 Delivered/ACK 反推为完成态。

## 量化验收基线

> 下列数字为**实测值**，口径见验证报告。

- 状态机与 DAG 性质：Note/Task/Step/CognitiveEvent 状态机与 Dependency 无环/ready 派生各 **200 个固定种子序列**（`tests/unit/test_phase4_domain.py`，3614 个参数化用例全绿）。
- **50 并发相同 Expected Revision**：`test_fifty_threads_same_expected_revision_one_winner` 实测 ok=1、mismatch=49（稳定码）、revision 行=2、task.active 审计=1、transition outbox=1、watermark 恰好 +1。
- **同一 Trigger Revision/计划时刻重复 100 次**：`test_recurrence_scans_are_idempotent_hundred_times` 实测 2 个到期时刻 → 恰好 2 个 Occurrence + 2 个逻辑 CognitiveEvent，100 轮扫描零新增。
- **同一 Event 重复投递/ACK 100 次**：`test_ack_is_idempotent_hundred_times` 实测同一 ack_id、恰 3 个 revision（created→delivered→acknowledged）、attempts=1。
- 时间矩阵：UTC、Europe/Berlin、America/New_York、Asia/Tokyo（+ 澳洲半时区）覆盖 DST 缺失（skip/postpone）、重复（first/second）、前跳、回拨（`test_clock_back_slew_does_not_duplicate`）、休眠（`test_sleep_then_catch_up_bounded`：10 秒沉睡 → 有界 3 个 enqueued + 显式 skipped）与重启 catch-up（`test_restart_recovers_from_persisted_marker`）。
- **20 次 Holder fence 场景**：`test_twenty_fence_scenarios_redeliver_same_id` 实测 20 轮抢占式 fence 全部重投同一 event id，最终恰 20 个事件、零重复。
- 负向端到端：delivered/acked/expired/投递失败四场景下 Task/Step 状态与证据引用零变化；CognitiveEvent 作为完成证据被结构性拒绝。
- 故障注入：新增 9 个边界（task_transition pre_revision/pre_pointer/pre_commit/post_commit、trigger_scan pre_occurrence/pre_commit/post_commit、event_ack pre_commit/post_commit）× 20 = **180 次**真 SIGKILL，恢复后 `verify_database_invariants` 零违例且重放收敛。

## 退出门禁

- [x] Note Pin/Snooze/Review/Promotion/Forget 状态机和保留规则测试通过。  
  （`tests/integration/test_notes.py` 15 例：全状态机走查、非法迁移稳定码、复查唤醒/重复关联/一次性晋升/复查延展、pinned 保留、tombstone 隐藏）
- [x] Task/Step 非法转换、Expected Revision 竞争和 Dependency 环被稳定拒绝。  
  （`tests/integration/test_tasks.py` 16 例（含跨租户/Agent/跨空间证据终检与 artifact 结构性拒绝）+ `test_phase4_domain.py` 状态机/DAG 200 种子；50 并发 CAS 门禁）
- [x] 重复 Tick/Occurrence 只生成一个逻辑 CognitiveEvent。  
  （`test_recurrence_scans_are_idempotent_hundred_times`、`test_at_time_fires_once_then_never_again`、`test_clock_back_slew_does_not_duplicate`、`test_restart_recovers_from_persisted_marker`）
- [x] Event 重投和重复 ACK 幂等，ACK、Delivered 均不会推进 Task/Step 完成。  
  （`tests/integration/test_events.py` 30 例：100×ACK、20×fence 同 id 重投、清扫路径 fence 重投、可配置尝试上限、pull 信封/lease 终检（SQL 内信封、epoch 必携、时间性过期 lease）、先清扫后投递、列表状态覆盖、ACK 持有者验证（含 lease 存活）、四场景负向完成门禁、事件证据拒绝、摘要 scope 分组、逻辑死亡行不饿死尾部、pull 返回推进后 current）
- [x] 发送或工具执行失败不会产生完成 Evidence；成功 Observation 后仍需显式转换。  
  （`test_completion_of_expected_effect_step_requires_committed_evidence`：无证据/partial Observation 拒绝，committed Observation 后仍需显式 complete）
- [x] 时区、DST、Catch-up 和过期 Policy 测试通过。  
  （`tests/integration/test_triggers.py` 34 例（含 `TestAuditRegressions` 12 例：DST 策略、数字 comparand、积压收敛、批次公平、跨作用域终检、at_time 退休、观察/被观察聚合空间终检、step tombstone、注入时钟与 TTL）+ `TestDstCatchUpMatrix` 四时区矩阵；过期/摘要合并/取消见 `test_events.py`）
- [x] Migration/Job 兼容/回退方案、需求追踪和交付证据已完成评审。  
  （`tests/integration/test_migrations_phase4.py` 19 例：0001–0004 字节不变、Phase 3 数据 4→5、空库=5、七类伪造指针/引用拒绝、备份三轮往返；ADR-0012；[验证报告](../reports/phase-04-verification.md)）

## 交付证据

- 代码/变更：Phase 4 实现提交（工作区待人工复核）。核心新增：`domain/{note,task,event}.py`、`storage/plans.py`（Note/Task/Trigger/Occurrence/Event 五仓储 + 指针 CAS + 调度位置 CAS）、`application/{notes,tasks,events,write_support}.py`、recall tasks 路由 + `pending_event_ids`、`jobs/handlers.py` Phase 4 handlers + `phase4_handlers`；扩展 `storage/uow.py`、`application/ports.py`、`storage/backup.py`（Phase 4 不变量）、`storage/runtime.py`（窗口 [4,5]）、`domain/jobs.py`（5 个启用 kind）、契约生成器/mock server/双 SDK。
- ADR：[ADR-0012](../adr/0012-phase4-notes-tasks-events.md)（Note 复查语义与 seam、激活特权与 Evidence 类型集、并发仲裁规则、Occurrence 身份与调度位置分离、fence/过期/摘要语义、tasks 路由与 pending_event_ids 边界、job kind 推进、泄漏纪律）。
- Schema/Migration：`migrations/0005_phase4_notes_tasks_events.sql`（SHA-256 `32ecc6f37f32b27443d03eb33c5365c4a0a9895b42c7df24fad6aa4a5a46f9e2`），13 张 STRICT 表；Schema 4→5 在线升级；窗口 [4,5]；0001–0004 与 HEAD `b4587b1` 逐字节一致。
- 测试/性能报告：[phase-04 验证报告](../reports/phase-04-verification.md)（`make ci` 全绿——初版 4351 passed / 85.80%，一轮 4366 / 85.87%，二轮 4377 / 85.92%，三轮审核修订后复跑 4414 passed / 85.99%，四轮审核修订后复跑 4418 passed / 86.05%，五轮审核修订后复跑 **4422 passed / 86.10%**；量化门禁实测、契约与泄漏扫描证据、审核修订 §8/§8.1/§8.2/§8.3/§8.4、已知限制）。
- 审核修订：2026-09-01 人工审核（2 P0 / 5 P1 / 3 P2）十项全部核实并修复——证据/拉取的按 ID 作用域终检、fence 候选独立于过期候选、DST 策略委托、有序操作符合法输入、扫描游标收敛与批次公平、一次性 at_time 退休、最小请求面一致；语义并入 [ADR-0012 §9](../adr/0012-phase4-notes-tasks-events.md)，回归测试见验证报告 §8。同日二轮审核（3 P0 / 5 P1 / 1 P2）九项全部核实并修复——证据与条件触发器的完整 scope_allows 终检（越界观察记 ledger）、lease epoch 必携与 expires_us 检查、时间性过期即 fence、pull 先清扫后投递、空间信封下沉 SQL、列表信封与状态参数化、ACK 持有者验证、触发事件时钟/TTL 注入；语义并入 [ADR-0012 §10](../adr/0012-phase4-notes-tasks-events.md)，回归测试见验证报告 §8.1。同日三轮审核（4 P0 / 3 P1）七项全部核实并修复——证据类型收敛为 {observation}（artifact 结构性拒绝直至 canonical 仓储存在）、Required 在线门禁接入全部 13 个 Phase 4 应用平面写（当轮实现曾把 lease proof 纳入幂等指纹，已由四轮 §12 反转；维护平面不设闸）、过期摘要按完整 scope 分组继承（禁止 agent 级提权与跨组拼接）、recall pending_event_ids 服从请求 scope（SQL 内 LIMIT 前过滤）、ACK 持有者存活验证、pending 有效期/tombstone 下沉 SQL、pull 返回推进后 current；契约/双 SDK/mock server/fixture 同步承载 lease proof。语义并入 [ADR-0012 §11](../adr/0012-phase4-notes-tasks-events.md)，回归测试见验证报告 §8.2（含 `test_phase4_surface_gate.py` 29 例模式矩阵）。同日四轮审核（2 P0 / 2 P1 / 2 P2）六项全部核实并修复——proof 绑定持有者身份（`check_online` 具名调用者必须是记录持有者，`current()` 外借的 proof 在写/pull/ACK 三路全部失效）、门禁移到幂等缓存之前且 proof 移出指纹（required 下含缓存回放的每次成功响应都持活 lease，lease 轮换后的合法重试回放而非 `idempotency_key_reused`）、pull proof 按 off/advisory/required 模式矩阵处理（off 忽略无效 proof、advisory 警告不拒）、Python `ack_cognitive_event` 与 TS 四个写方法的 lease proof 透传（两侧以实际请求体断言）、advisory ACK warning 入审计、三份文档残留的 `{observation, artifact}` 声明统一收敛；语义并入 [ADR-0012 §12](../adr/0012-phase4-notes-tasks-events.md)，回归测试见验证报告 §8.3。同日五轮审核（2 P0 / 1 P1 / 1 P2）修复 Holder 在 Observation 的匿名绕过、缓存预检后的抢占窗口、Create 授权顺序与文档不实声明，并把 Observe 对齐双阶段门禁与 proof 非指纹语义；语义并入 [ADR-0012 §13](../adr/0012-phase4-notes-tasks-events.md)，回归测试见验证报告 §8.4。
- 已知限制：见验证报告"已知限制"节——condition 类触发的扫描粒度为每 agent 有界批（跨 tick 收敛）、state_condition 按值对象 `value` 键求值（有序比较为数字语义）、无 FastAPI 传输层（沿用应用层契约 + mock server 模式）、tasks 路由需显式注入服务；§25.3 的全应用平面覆盖仍需后续跨阶段工作补齐 Phase 3 Focus 与 Phase 6 Recall 请求面。

## 明确不做

- 不执行 Task 中的外部动作，不保存可执行脚本或任意条件代码。
- 不支持跨 Task Dependency。
- 不让后台模型直接创建 Active Task 或高权威 Claim。

## 交接条件

Phase 5 可以使用 Note/Task Promotion seam（claim/episode 目标类型 + 待回填 id + resource_link/审计事件）和 CognitiveEvent Evidence 语义（事件不是证据，committed Observation 才是）；Phase 6 可以在 tasks 路由与 pending_event_ids 之上增加路由而不重定义既有语义；Phase 11/12 可以依赖稳定的事件拉取/ACK 语义。
