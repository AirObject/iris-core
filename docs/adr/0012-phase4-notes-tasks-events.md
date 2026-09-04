# ADR-0012: Phase 4 — Note Lifecycle, Task Evidence Semantics, Trigger/Occurrence Identity and At-least-once Host Delivery

- 状态：Accepted（2026-09-01 审核修订并入；同日二轮 §10、三轮 §11、四轮 §12 审核修订并入）
- 日期：2026-08-31（初版）/ 2026-09-01（审核修订、二轮/三轮/四轮审核修订）
- 影响阶段：Phase 4（Phase 5/6/11/12 依赖本决策的语义）

## 背景

Phase 4 交付 Note 生命周期、Task/TaskStep/TaskDependency/TaskTrigger、Occurrence 与 CognitiveEvent 投递闭环（§10–12、§17、§36 阶段 4）。实现中存在多项跨阶段、不可逆或影响后续契约的语义选择，必须冻结为可审计决策。

## 决策

### 1. Note：review_after 是复查时间，复查是确定性整理而非删除

- 状态机严格按 §10.2：`snoozed` 只能回 `inbox`（由复查扫描唤醒），`promoted`/`tombstoned` 终态无出边。唤醒发生在复查扫描（`note.review` job）内，同一遍完成重复关联与后续整理——snooze 的到期就是被推迟的那次复查。
- 复查扫描只做四件事：唤醒 snooze、按 `content_hash` 建 `possible_duplicate` resource_link（只关联，绝不按文本相似度删除）、对 `follow_up`/`promise` 类一次性物化 proposed Task、对仍重要但未解决的 Note 前移 review_after。**任何路径都不删除 Note**：Pinned、未兑现承诺、活动 Task 来源 Note 与管理保留项结构性地不存在于删除路径中。
- Promotion seam：`task` 目标在 Phase 4 真实物化（创建 proposed Task + resource_link + 回填 promotion_target_id）；`claim`/`episode` 仅记录目标类型，id 留空待 Phase 5 属主服务回填。

### 2. Task：激活是特权，完成需要真实 Evidence

- **激活控制**（§11.5）：`origin ∈ {conversation, background}` 的创建被钉死为 `proposed`；激活转换在序列化写事务内校验 `may_activate`（explicit_tool / policy / admin 三者之一），失败为请求级 `access_denied`。注意 origin 由调用方声明，安全边界在于：conversation/background origin 无论谁调用都不能激活，而 admin origin 仍要求 access.admin。
- **Evidence 语义**：`EVIDENCE_RESOURCE_TYPES = {observation}`（三轮 §11.1 收紧；Phase 5 提供 canonical Artifact 仓储与其专用 validator 之前，artifact 引用被结构性拒绝）。声明 `expected_effect` 的 Step 完成必须引用同租户下 `effect_state=committed` 的 Observation——partial（已生成未发送/发送失败）不是证据（§15.2）。CognitiveEvent 与 artifact 均被明确排除在证据类型之外：投递、ACK、过期、发送失败都不是外部效果，不得推进完成。无 expected_effect 的 Step 完成不需要证据（纯内部步骤）。
- **并发仲裁规则**：50 个相同 Expected Revision 的并发转换中，落败方读到的聚合已被胜者推进（status 已是目标态）。此时状态机检查会先失败——我们规定：当 `status == target && current_revision > expected_revision` 时返回 `revision_mismatch`（CAS 是相同并发请求的权威仲裁者），其余非法转换仍稳定返回 `invalid_state_transition`。Note/Task/Step 三个状态机统一采用该规则。

### 3. Dependency：同 Task DAG，readiness 双向派生

- v1 依赖只在同一 Task 的 Step 之间（§11.3）；写入前 DFS 环检测，环稳定返回 `task_dependency_cycle`（本阶段进入契约错误码集）。
- `compute_step_status` 是纯函数：`pending` 与 `ready` 都是**派生态**——完成前驱授予 readiness，新增未满足的依赖同样**收回** readiness。手动 `→ready` 转换被状态机拒绝（`ready` 只能从 `pending` 经推导进入），派生值与存储值永不分歧。waiting/blocked/in_progress/终态是权威态，不受依赖推导影响。

### 4. Trigger：声明式 spec + Occurrence 身份 = (trigger, revision, 计划时刻)

- condition_spec 只允许按 kind 声明的字段白名单与 9 个比较操作符（eq/ne/lt/le/gt/ge/exists/absent/contains）；未知键、未知操作符、缺 comparand 一律 `invalid_request`。不存在 eval/脚本/隐式工具执行路径；`evaluate_state_condition` 是纯比较函数。recurrence 复用 ADR-0009 §5 的受限日程语法（interval/daily + IANA 时区 + DST skip/postpone/first/second），时区矩阵直接继承 Phase 2 的测试面。
- **Occurrence 身份**：`UNIQUE(trigger_id, trigger_revision, scheduled_at_us, occurrence_key)`。occurrence_key 编码计划时刻（时间型）或触发主体（observation id / state 观测时刻 / 目标 revision），因此重复扫描、重启、时钟回拨全部坍缩到同一行；trigger 的 SPEC revision 变更（含启停）使身份分叉——旧 occurrence 永不在新 revision 下重发。
- **调度位置与 spec revision 分离**：`next_fire_at_us`/`last_scan_us` 通过对旧值的 NULL-aware CAS 更新（不等则零写入），不产生 spec revision。调度进度必须不能改变 occurrence 身份。misfire/catch-up 复用 `plan_catch_up`（misfire_grace 界定回溯窗、单次上限、超出记 `skipped` 显式记账）；trigger 默认 misfire grace 为 24h（对齐日级日程）。

### 5. CognitiveEvent：at-least-once、ACK=接收、fence 重投同一 ID

- 投递拉取把 `pending → delivered` 写为一次 revision（记录 lease id/epoch、attempts+1、last_delivery_at），完整投递历史保存在 revision 链。无活动 Holder 时事件保持 pending。
- **ACK 只代表宿主接收并承担处理责任**：仅 `delivered → acknowledged` 合法；重复 ACK（任意幂等键）返回首次 ack_id，revision 链不再增长。ACK 审计 details 刻意不携带任何 task/step 状态或证据引用。
- **Fence 重投**：delivered 且未 ACK 的事件，其记录的 lease 不再授权持有者（epoch 落后或 lease 不再活跃）时回到 pending，下一次拉取向新 Holder 投递**同一 event id**。无 lease 的投递没有可 fence 的持有者，其安全网是过期 horizon 而非 fence。
- **过期与有界摘要合并**：超过 horizon 或尝试次数上限且未 ACK 的事件进入 expired；单次清扫超过阈值（默认 20）时折叠为一个 `summary.expired_events` 事件（计数 + 有界 id 列表 + resource_links），过期事件不静默消失。

### 6. Recall：tasks route 为最高优先级结构化信号，pending_event_ids 只广播 id

- Due Task 进入结构化 Recall 作为第一条路由（category priority 0），最终 rehydrate 复核 status/due/scope/privacy/tombstone。路由经 `tasks` 服务注入——不注入时 orchestrator 保持 Phase 3 三路由语义（Phase 3 测试不受影响）。
- `pending_event_ids` 只含 id（上限 50）：事件正文留在事件端点自身的授权与终检后面，recall 不复制内容。Phase 6 边界不变：不发布 `/v1/recall`、无 FTS/Vector/Graph/Cache/Usage。

### 7. Job Kind 推进与 Outbox 纪律

- 启用 `note.review`（priority 4, catch_up=all）与 `task.trigger_scan`（priority 4, catch_up=all），均具备真实幂等 handler。新增 `note.changed`/`task.changed`/`cognitive_event.changed`（priority 6）——与 `state.projection` 同型：Phase 4 每个修改在同一事务写 Revision + Pointer CAS + Audit + Watermark + Outbox，这些 job 就是 coalesced 流欠下的指针不变量检查（当前指针必须解析到与其 revision 号一致的 revision 行）。三个 kind 均不可 coalesce（occurrence/投递语义禁止丢弃式合并，ADR-0009 §4 白名单外）。
- 所有 Phase 4 载荷 payload_version=1；未知版本入队即拒绝（fail closed 不变）。

### 8. 泄漏纪律的实例化

Note/Task 的 audit details 只携带 kind、hash、计数与目标类型；调用方 reason 只进入 audit 的 reason_code 列，不复制进 details。Occurrence/Event 的 outbox 载荷只含 id/revision/kind。契约面不暴露 Note 正文与 Task goal 之外的任何派生内容；canary 泄漏扫描覆盖 audit details、outbox payload 与 last_error_code。

### 9. 审核修订（2026-09-01）：按 ID 取用的资源必须过自身作用域终检

初版实现后的人工审核发现十处"文档声明强于代码执行"的缺口，全部按以下语义修订（回归测试见验证报告 §8）：

1. **Evidence 终检**（修订 §2）：observation 证据按全局 ID 取回后，必须匹配**被完成 Task 自己的 tenant+agent**——committed/tombstone 之外新增作用域断言。证据的准入由资源自身的作用域维度决定，而非调用方声明。
2. **Pull 双终检**（修订 §5）：事件拉取逐条执行与 by-ID 访问相同的空间信封检查（`event.space_id ∈ access.allowed_space_ids`，否则留在 pending）；调用方具名的 lease 必须是本 tenant/agent/app 实例的**活跃** lease 且 epoch 匹配——记录的持有者正是后续 fence 仲裁的依据，不允许伪造。
3. **Fence 候选独立于过期候选**（修订 §5）：清扫先跑专用 fence pass（delivered、未 ACK、持有 lease），再跑过期候选；过期候选 SQL 的尝试上限改为服务配置 `max_attempts`（不再硬编码 50）。horizon 未到、尝试数未满的被 fence 事件由 fence pass 重投。
4. **DST 策略真实生效**（修订 §4）：`parse_trigger_schedule_spec` 直接委托 Phase 2 的 `parse_schedule_spec`——`kind` 与 `dst_missing`/`dst_ambiguous` 携带声明语义进入调度器，包装层不再重建 spec 而丢弃策略。
5. **有序操作符的合法输入**（修订 §4）：`value` 是唯一带类型的字段（字符串或数字）；lt/le/gt/ge 在**创建时**要求数字 comparand，contains 要求字符串。求值期遇到不可比较的活值（如字符串状态值对数字 comparand）判定为**不匹配**（False）而非抛错——单个异常值不得中止整个 agent 扫描。
6. **扫描游标只推进已读**（修订 §4）：observation 扫描批次截断时，`last_scan_us` 只推进到本批最大 `committed_us` 减一（同刻尾部保持可见），且已入 ledger 的观察在 SQL 内排除（`NOT EXISTS` 于 occurrence 键前缀）——LIMIT 只约束**新**工作，积压跨 tick 收敛，同刻大批不永久饥饿。
7. **触发器批次公平排序**（修订 §4）：到期时间触发器按 next_fire 排序先扫；条件触发器按 `last_scan_us` 陈旧度排序（NULL 优先、最老优先）——条件类数量超过批次上限时轮转收敛，不再固定读同一头部。
8. **task_transition 终检**（修订 §4）：condition 可命名任意 task/step ID，扫描时对被观察聚合复核**同 tenant+agent、未被 tombstone**；不满足则该触发器 fail closed 不发射。observation 扫描对 tombstone 复查并把跳过记入 ledger（`observation_tombstoned`）；过期 state 记录读作 absent。
9. **一次性 at_time 跳过即退休**（修订 §4）：misfire/catch-up 被记为 skipped 的一次性触发同样清空 `next_fire_at_us`——过期标记不再每个 tick 重读同一 occurrence。
10. **最小请求面一致**：task-create 的 JSON Schema 只要求 agent_id+title，服务端默认 origin/owner_kind；Python/TS SDK 校验器与 mock server 同步放宽为同一最小面（新增共享 fixture `task-create-request-minimum`）。

### 10. 二轮审核修订（2026-09-01）：作用域信封的完整语义与事件生命周期边界

二轮复核确认 §9 的 4/5/6/8/9/10 已关闭，但 1/2/3/7 仍有未覆盖边界，另发现三个事件生命周期缺陷。修订语义如下（回归测试见验证报告 §8 二轮表）：

1. **Evidence 终检升级为完整 scope_allows**（修订 §2 之 1）：tenant+agent 之外，observation 证据必须落在被完成 Task 自己的 space_group/space/session 之内（`scope_allows(observation, task)`）。按向下可见性模型，空间级事实对 agent 级视点不可见：A 空间的 Task 不得引用 B 空间的 committed Observation，agent 级 Task 亦不得引用任何空间级 Observation。
2. **条件触发器空间终检**（修订 §9 之 8 的强化）：observation 触发的扫描候选在 tenant+agent 之外逐条执行 `scope_allows(observation, trigger 所在 Task)`；越界观察记入 ledger（`observation_out_of_scope`）而非发射——ledger 键使该行退出候选集，不产生批次饥饿。task_transition 的被观察聚合复核升级为**同 tenant+agent + scope_allows + task 与（命名时）step 双 tombstone**。
3. **Lease 校验完备化**（修订 §9 之 2 的强化）：`lease_id` 必须携带 `lease_epoch`（否则 invalid_request——无 epoch 的投递记录无法被 fence 仲裁）；lease 的 `expires_us` 已过即视为死锁（access_denied），即使 surface expiry 尚未把行翻出 active。
4. **时间性过期即 fence**（修订 §5）：`_holder_is_fenced` 使用扫描时刻判定 lease 存活——`expires_us` 已过的 lease 即使行状态仍为 active 也判定 fenced，事件回 pending 由新持有者以同一 ID 重投；不再等待 surface expiry 任务先行。
5. **先清扫后投递**（修订 §5）：pull 的过期清扫移到投递循环**之前**。max_attempts=1 时首次 pull 返回的事件在库中是 delivered 而非 expired，ACK 可正常落账；本 call 投递的事件永不在同一 call 内被过期。副作用是 fence 重投事件可在同一 pull 内被新持有者领取（更快恢复，语义不变）。
6. **空间信封下沉 SQL**（修订 §9 之 6 的同类修复）：pull 与事件列表的空间过滤移入 SQL（`space_id IS NULL OR space_id IN (信封集)`，与 `require_same_tenant_agent` 同语义）——批次 LIMIT 只约束该 app 实际可持有的事件，50 条不可见事件不再饿死第 51 条可见事件。
7. **列表门禁 = 信封 + 状态参数化**（修订 §6）：事件列表与 recall `pending_event_ids` 改用访问信封（注册空间内的空间级事件可见）而非全空请求 scope 的 scope_allows（后者会结构性排除所有空间级事件）；statuses 透传 SQL，OpenAPI 声明的 acknowledged/expired/cancelled 过滤返回真实数据。
8. **ACK 持有者验证**（修订 §5）：带 lease 的投递只能由**记录在案的持有者 app 实例** ACK，且记录的 epoch 必须仍然匹配——同 agent/空间信封的其它 app 实例 ACK 会被拒绝，不能吞掉投递、阻断 fence 重投。
9. **触发事件的时钟与 TTL 注入**（修订 §5）：`create_internal` 接受 `now_us`/`ttl_us`；trigger scan 传入扫描服务的注入时钟与服务级 `event_ttl_us`（TaskService 新配置，与 CognitiveEventService 对齐）——触发器创建的事件不再落到 SystemClock 时间线与默认 TTL 上。

### 11. 三轮审核修订（2026-09-01）：证据面收敛、Required 在线门禁、摘要作用域与批次一致性

三轮复核确认的 4 个 P0 与 3 个 P1 全部按以下语义修订（回归测试见验证报告 §8.2）：

1. **证据类型收敛为 {observation}**（修订 §2）：Phase 5 之前没有 canonical Artifact 仓储，任何 artifact 引用都是不可验证输入——`EVIDENCE_RESOURCE_TYPES` 收紧为仅 `observation`，artifact completion evidence 被结构性拒绝（Task 与 Step 两条路径同一允许集）。Artifact 只有在后续阶段具备 canonical existence、tenant/agent/scope/privacy/tombstone 校验后才能重新加入证据类型集（连同其专用 validator）。
2. **Required 在线门禁接入 Phase 4 应用平面**（修订 §25.3 落地）：三个服务新增可选 `surface` 协调器注入；每个公开写（Note create/update/archive/promote、Task create/patch/transition、Step create/transition、Dependency、Trigger create、CognitiveEvent pull/ACK）都先完成访问检查，再按 §13 的双阶段规则调用 Phase 2 共享门禁——缓存前校验保护重放，缓存未命中后在序列化业务写事务内再次校验保护提交；off 放行、advisory 不阻断（warning 记入审计 details / pull bundle）、required 以稳定码 `lease_expired`/`lease_fenced` fail closed。维护平面（review sweep、trigger scan、事件清扫）不经过门禁。~~幂等指纹覆盖 lease proof：换新 proof 的重试必然重新过闸~~（四轮 §12.1 修订：指纹覆盖 proof 实测既挡不住缓存回放、又把合法轮换重试变成 `idempotency_key_reused`；proof 改为逐调用校验的凭证并移出指纹，门禁移到幂等缓存之前）。~~门禁仲裁 id+epoch 的知识（仅当前持有者可知）~~（四轮 §12.2 修订：`current()` 使 id+epoch 对所有获准实例可读，"仅持有者可知"前提不成立；门禁改为绑定持有者身份）。
3. **过期摘要按完整 scope 分组**（修订 §5）：清扫收集过期事件的完整行，按 `(tenant, agent, space_group, space, session)` 分组；每个超过阈值的组生成一个**继承该组精确 scope** 的摘要——`object_id` 与全部 `summary_of` links 只引用同组事件，阈值按组计算（互不可见的 scope 无法拼接触发）。空间级事件不再被提升为 agent 级摘要。
4. **Recall pending_event_ids 服从请求 scope**（修订 §6）：recall 是具体 `StructuredRecallRequest`，其 `agent_id/space_id/session_id` 构造正式向下可见规则（agent 级数据可进入空间请求；匹配空间可进入；他空间/他会话不可进入），并在 SQL 内、LIMIT 之前过滤。`/v1/cognitive-events` 列表保持访问信封语义不变。
5. **ACK 持有者存活验证**（修订 §5）：`_require_ack_holder` 使用同事务 `now_us`，验证 lease 的 tenant/agent/持有者 app/epoch、status=active、`expires_us > now_us`、且仍是该 tenant+agent 的当前 active lease——过期或被 fence 的持有者 ACK 被拒（事件保持 delivered，随后由 sweep 以同一 id 重投）。ACK 幂等重放不受影响：已 ACK 状态的同一逻辑请求仍返回首次 ack_id、不增加 revision。
6. **逻辑有效期与 tombstone 下沉 SQL**（修订 §9 之 6 的扩展）：pending 事件的 `expires_us <= now` 与 cognitive_event tombstone 排除进入 pull 与列表查询的 SQL——非 pending 状态不受 horizon 子句影响。信封、请求 scope、status、有效期、tombstone 全部先过滤再 ORDER/LIMIT。
7. **Pull 返回推进后的 Current**（修订 §5）：CAS 成功后重新读取 `tx.events.get(id)` 与当前 revision 行再加入 `DeliveryBundle`——返回的 current 与 revision 一致地处于 delivered 状态、推进后的 revision、戳记的 attempts/lease/delivery 时间；CAS 失败的事件仍不入 bundle。

### 12. 四轮审核修订（2026-09-01）：proof 绑定持有者、门禁先于幂等缓存、投递 proof 的模式矩阵

四轮复核确认三轮的 7 项修复已落地，但发现 proof 凭证模型与幂等缓存的交互存在两个 P0，以及投递 proof 校验、SDK 透传与审计留痕的四个次级缺陷。修订语义如下（回归测试见验证报告 §8.3）：

1. **门禁先于幂等缓存（gate-before-cache，修订 §11.2）**：所有 Phase 4 幂等写（含 ACK）的 §25.3 门禁移到 `IdempotencyManager` 查询**之前**——completed 记录的缓存回放同样必须先通过门禁，required 模式下"每次成功响应（含重放）都发生在请求者持有活 lease 之时"成为可验收不变量。**lease proof 移出幂等指纹**：proof 是逐调用校验的凭证，不是请求逻辑的一部分；把它烙进指纹会令 lease 轮换后的合法重试（同 key、同逻辑载荷、新 proof）撞上 `idempotency_key_reused` 而不是回放。三轮"指纹覆盖 proof"的声明基于一个错误前提（指纹不匹配 ⇒ 重新过闸）；实测不匹配走的是 key 复用错误路径，缓存命中则完全绕过门禁。第五轮 §13 进一步补上缓存未命中后的事务内二次门禁。
2. **proof 绑定持有者身份（修订 §11.2 前提）**：`check_online` 要求已认证 `app_instance_id`——proof 必须属于该实例**被记录的持有者**，不匹配按 `not_lease_holder` 归入 `lease_fenced` fail closed（advisory 只记 warning）。动机：`current()` 按 §25 的可观测性语义把完整 LeaseView（含 id+epoch）发给**所有**获准该 Agent 的实例，"仅持有者可知"不成立；绑定身份后，外借的 proof 在 Observe、Phase 4 写门禁、pull 戳记、ACK 持有者验证全部路径上失效。`current()` 的暴露面保留（运维观测需要），因为 proof 只在其持有者手里是凭证。pull 的 proof 解析（`_resolve_pull_lease`）与写门禁共用同一持有者绑定规则。
3. **投递 proof 的模式矩阵（修订 §9 之 2 / §10 之 3）**：pull 呈现的 proof 按 §25 同一矩阵处理——配对完整性（有 id 无 epoch 或反之）是请求形状错误，任何模式下都拒绝；**格式完好但无效**的 proof（未知/过期/他 agent/他持有者/epoch 落后）在 off 下被忽略（按无 lease 投递，不告警）、advisory 下按无 lease 投递并把原因作为 `lease_warning` 返回（只警告不拒绝）、required 下以稳定码 `lease_expired`/`lease_fenced` fail closed。off 不再因为一个陈旧 proof 拒绝整个拉取——模式定义（off 不校验、advisory 不阻断）对投递面同样成立。
4. **官方 SDK 的 proof 透传（修订 §11 之 2 的契约面）**：Python `ack_cognitive_event` 增加 `lease_id`/`lease_epoch` 参数并入请求体；TypeScript `noteAction`、`transitionTask`、`transitionTaskStep`、`ackCognitiveEvent` 的输入类型补上两个字段并真正写入 wire body（`transitionTask`/`transitionTaskStep` 顺带不再把传输层的 `idempotencyKey` 泄进 body）。两侧都以"实际发出的请求体"断言（Python 用 mock server 的请求记录通道，TS 用 fetch stub），而不是只测独立 schema validator。
5. **Advisory ACK 的 warning 进入审计（修订 §8 泄漏纪律）**：`cognitive_event.acknowledged` 的 audit details 携带 `lease_warning`（无值时为 null），与 Note/Task 写路径同一纪律——advisory 的承诺是"不阻断但留痕"，不留痕等于静默通过。

### 13. 五轮审核修订（2026-09-01）：Holder 全路径强绑定、双阶段门禁与授权顺序

第五轮复核确认四轮六项修复的主路径已落地，但独立竞态与跨 Surface 调用复现发现 2 个 P0、1 个 P1、1 个 P2，统一修订如下（回归测试见验证报告 §8.4）：

1. **在线调用者身份不可省略**：`check_online.app_instance_id` 从可选提示升级为必填的认证身份，所有在线调用均透传 `AccessContext.app_instance_id`。Observation 不再走匿名校验旁路；邻居即使从 `current()` 读取完整 proof，也不能写入 committed Observation。管理/维护平面不调用门禁，不需要以 `None` 冒充在线身份。
2. **双阶段门禁关闭 TOCTOU**：幂等写先在缓存前校验活 lease，缓存未命中后再在承载 Revision/Pointer/Audit/Watermark/Outbox 的同一序列化写事务内校验。前者阻止 fenced 实例读取缓存结果，后者阻止在预检与 `IdempotencyManager.run` 之间被抢占的旧 Holder 落账。事务内校验通过后，Surface 抢占写必须等待本业务事务提交，Canonical 与 lease 决策不再出现窗口。
3. **授权先于 Surface 状态查询**：Note/Task create 与 Observe/Pull 在缓存前先验证 Agent grant、Space/Session 容器和 Privacy envelope；未授权请求稳定返回 `access_denied`，不会以 `lease_expired`/`lease_fenced` 暴露目标 Agent 的 Surface 模式或活跃状态，也不会创建幂等记录。
4. **Observe 与同一在线策略对齐**：Observation 的请求级幂等重放同样执行缓存前门禁，缓存未命中同样执行事务内门禁；lease proof 移出 Observation 请求指纹，合法 lease 轮换后的同 key/同逻辑载荷返回原结果，异载荷仍稳定 `idempotency_key_reused`。Coordinator 自身不可达仍由缓存前校验映射为 `not_ready`。

## 否决的替代方案（初版）

- 用文本相似度自动删除重复 Note：违反 §10.3（关联不删除），且删除标准不可审计。
- 后台复查直接创建 active Task：违反 §11.5 激活特权。
- 把 ACK/投递成功当作完成证据：违反 §12.2（ACK≠完成）与 §15.2（未生效输出不是事实）。
- 跨 Task Dependency：v1 明确不做（§11.3），需要时以新 ADR 引入。
- cron 表达式 trigger：语义隐式，推迟（沿用 ADR-0009 §5 的受限语法）。
- occurrence 唯一键只含 (trigger, 时刻)：condition 类触发的同一时刻可能对应多个触发主体，键必须覆盖 occurrence_key 子键。
- 调度进度推进写新 spec revision：会使 occurrence 身份随调度 fork，同一逻辑时刻产生两个身份。
- 无 lease 投递也走 fence 重投：无持有者可 fence，会把正常在途投递误判为孤儿。
- 求值期对不可比较的活值抛错中止扫描：一个异常状态值不应拥有拒绝整个 agent 触发器扫描的能力——降级为"不匹配"。
- 信任 condition_spec 内嵌的任意 task/step ID：声明式 spec 是存储的程序，其引用必须在扫描时按资源自身作用域复核。

## 否决的替代方案（二轮）

- 求值期对越界观察抛错中止扫描：一个跨空间观察不应拥有拒绝整个 agent 扫描的能力——记 ledger 跳过（`observation_out_of_scope`）既可审计又让该行退出候选集。
- ACK 只验信封不验持有者：会允许邻居 app 实例吞掉投递并阻断 fence——持有者身份与 epoch 必须与投递记录一致。
- 清扫仍后置于投递、但对"本 call 刚投递"的事件豁免：无状态清扫无法区分投递批次，顺序（先清扫后投递）是唯一不引入额外状态的解。
- pull 空间过滤留在应用层并加大 LIMIT：LIMIT×K 仍可被构造性饿死；过滤必须在 SQL 内与 LIMIT 同层。
- agent 级 Task 接受任意空间级 Observation（"订阅面更宽"）：与 §5.2 向下可见性模型冲突——空间级数据对未命名该空间的视点不可见，接受即跨空间泄漏。

## 否决的替代方案（三轮）

- 给 artifact 增加形式校验（如 ID 格式/长度）：没有 canonical 存在性校验的格式检查只是仪式——任何符合格式的字符串都能完成步骤。宁可现在结构性拒绝。
- 为 Phase 4 复制一套精简租约规则（只查 active lease）：epoch 仲裁、时间性过期与稳定错误码已经在 Phase 2 协调器里——第二套规则必然漂移。
- ~~门禁在幂等查询之前执行（重放也必须持活租约）：同 proof 的重放是同一逻辑请求的确认，不是新的在线互动；指纹覆盖 proof 已保证换 proof 必然重新过闸~~（四轮 §12.1 反转本条：指纹不匹配走的是 `idempotency_key_reused` 而非重新过闸，缓存命中则完全绕过门禁——门禁必须先于幂等缓存，proof 移出指纹）。
- 摘要按 agent 汇总后对拉取者逐条过滤 links：摘要行的 scope 本身就是提权（计数与首 id 已泄漏）；必须按组继承精确 scope。
- recall 在取满 50 条后用 Python 丢弃越界 id：重建批次饥饿；scope 匹配必须在 SQL 内与 LIMIT 同层。
- pull 返回循环开始时的 pending current：调用方拿到 pending current + delivered revision 的矛盾对；CAS 后必须重读。

## 否决的替代方案（四轮）

- 邻居可读 proof 的另一个修法——把 `current()` 对非持有者脱敏：可观测性面（谁持有、到何时）本身是运维需求；且脱敏挡不住其它泄漏渠道。凭证失效必须在验证点（绑定持有者）完成，而不是靠保密。
- 接受"缓存回放绕过门禁"并只改 ADR 措辞：回放响应里带着资源 id 与逻辑结果，被 fence 的实例可以无限期继续获取它们——"required 下每次成功响应都持活 lease"这一验收语义不允许例外。
- 保留 proof 在指纹内、把 `idempotency_key_reused` 当作特性：lease 轮换（preempt/TTL 到期后重新 acquire）是正常运行路径，合法重试必须能回放；错误码语义（key 复用 = 逻辑请求不同）不能被凭证轮换劫持。
- off/advisory 下继续无条件拒绝无效 pull proof：违反 §25.1 模式定义本身——off 部署根本不应感知 lease 策略的存在。
- TS 侧只在类型上加字段但不改手工构造的 body：类型是编译期承诺，wire body 才是服务端看到的真实——透传必须在 body 构造处断言。

## 后果

- Schema 5 新增 13 张 STRICT 表（notes/note_revisions、tasks/task_revisions、task_steps/task_step_revisions、task_dependencies/task_dependency_revisions、task_triggers/task_trigger_revisions、task_trigger_occurrences、cognitive_events/cognitive_event_revisions）；scope 键全部 NULL-free 字符串。
- 错误码集新增 `task_dependency_cycle`（§23.4 冻结清单内首次启用）。
- 备份恢复不变量扩展：六个新聚合的指针解析、occurrence→trigger/event 引用、terminal 事件不残留 lease。
- 契约 1.2.0 → 1.3.0（additive：3 个 capability、13 条路径、14 个 schema、`task_dependency_cycle` 错误码）；fixtures 35→52。

## 迁移影响

- `migrations/0005_phase4_notes_tasks_events.sql`（online_safe=true, lock_ms=200, min_app=0.5.0, recovery=none）；0001–0004 与 HEAD `b4587b1` 逐字节一致。
- 兼容窗口 [4,5]：0.5.0 在线升级 Schema 4 库；Schema 3 需先经 0.4.0 二进制（MigrationRunner 可一次走完，窗口只约束 Ready）。
- 不提供 Down Migration；revision/occurrence/投递历史不经降级脚本删除或回拨。回退顺序：先停用 `note.review`/`task.trigger_scan` handler 与事件领取，保留 Pending Event、Tick 与 Occurrence Ledger，用兼容二进制或备份恢复——不把 Delivered/ACK 反推为任何完成态。
