# ADR-0011: Phase 3 — Recent Generations, State Coalesced Stream, Focus Decay Model and the Structured-Recall Skeleton

- 状态：Accepted
- 日期：2026-08-30
- 影响阶段：Phase 3（Phase 4/6 依赖本决策的语义）

## 背景

Phase 3 交付三类短期能力：可重建的 RecentContextProjection、版本化高频 StateRecord、Canonical FocusItem,以及只组合这三者的内部结构化 Recall Route 骨架（§9、§15.4、§18）。实现中存在多项跨阶段、不可逆或影响后续契约的语义选择，必须冻结为可审计决策，不得只埋在代码里。

## 决策

### 1. RecentContextProjection = 不可变 generation + 每 target 一个 Current Pointer

- 存储结构：`recent_context_generations`（不可变、status ∈ {verified, retired}）与 `recent_context_current`（target_key 主键的指针表）。target_key 是五维 scope 的规范 NULL-free 字符串——SQLite 复合 UNIQUE 把 NULL 视为互异，复合主键会造成"无 session 的 target"出现重复指针；这是选字符串键而非复合列的直接原因（State/Focus 的 scope_key 同理）。
- 重建纪律（shadow）：先以纯函数构建（`domain/recent.py::build_projection`，同一 Observation 集 + watermark + builder_version + estimator 三次重建逐字节一致），再以 `projection_invariants` 校验，最后在**同一写事务**内 insert generation + 原子 upsert 指针。任一步失败 → 指针不动 → 读取继续使用上一已验证 generation，或退回 Canonical Observation 窗口。不存在部分可见的"building"状态。
- 读取终检：generation 服务前复算 `result_hash`、精确核对 pointer/generation/request target，并逐条复核 observation ref 的 revision/occurred/token 元数据、committed、tombstone、scope 与 privacy（含 subject consent）；任何一项不过 → 整代弃用，退回**带同等过滤**的 canonical 窗口读取。恢复不变量执行同一套结构/hash/target/ref 检查，损坏的备份不能切换上线。
- 摘要段：仅携带 source refs / 覆盖数 / token 估算的确定性压缩段，不伪造自由文本摘要（无 Provider）。segment 切片端点必须钳制在未入窗前缀上（否则会把热窗观察重复计入段内——同为实现期修复）。
- 过期/失效：`expires_us` 到期或 admin invalidate → 指针删除、generation 置 retired；读取自动降级为 canonical 窗口,maintenance 顺带清扫。

### 2. State 的身份、合并与投影 Job

- 记录身份 = `(scope_key, namespace, key)`；scope_key 为五维规范字符串（NULL-free）。创建竞态由 `UNIQUE(scope_key, namespace, key)` 裁决（败者得到 `revision_mismatch`），更新竞态由 Current Pointer 上的 Expected Revision CAS 裁决——创建路径的指针接线使用 `set_initial_pointer`（空指针 CAS），与更新路径的 revision CAS 分离。
- PUT 幂等指纹**只覆盖调用方显式提供的字段**：`observed_us` 等服务端时钟默认值若计入指纹，会把同 payload 重试误判为 `idempotency_key_reused`（实现期修复）。更新已有值必须携带 Expected Revision 且 CAS 以调用方值为准。
- Namespace Policy（默认 TTL/TTL 上限、是否保留历史及上限、value 字节上限、允许的 Source Authority、Scope 要求）以内置默认（runtime/environment/topic）+ 租户级 DB 覆盖（`state_namespace_policies`，管理面、审计）解析；未知 namespace 使用保守 DEFAULT policy。retention 在**同一事务、单条 DELETE**内精确裁剪到上限（即使策略收紧前已有超过 500 条历史也一次收敛）；`retain_history=false` 的 namespace 拒绝历史读取（`history_unavailable`），过期值默认读取不可见但按策略可审计读取。
- 新 Job kind `state.projection`（CoalesceClass.STATE、priority 6、handler 启用）：State 尚无派生表，该 job 是 coalesced 流欠我们的指针不变量检查（current pointer 必须解析到与其 revision 号一致的 revision 行）。State 写路径的投影 job 以 `(scope, namespace, key)` 为 coalesce key 合并 pending 工作；Canonical Revision/Audit/Watermark 一个不少（§16.4 语义）。

### 3. Focus 的衰减模型与容量

- 衰减是**纯函数**：`activation = base * 0.5 ** (Δt / half_life)`，输入是 activation_base（最近一次真实激活时的基准）与 last_activated_us，**从不**读取已衰减存储值——同一时刻重复执行 maintenance 产生相同结果、不产生新 revision、不会指数复合。存储行同时保存 `activation`（当前投影值）与 `activation_base`（衰减不变量）。
- 只有显式 activate（或未来经验证的真实 usage）增强 activation：base 上调有界增量并刷新 last_activated_us；retrieved/returned 不触碰任何值。增强上限 clamp 到 1.0。
- 状态机：active/dormant → {对方, promoted, dismissed, expired}；promoted/dismissed/expired 终态无出边（ADR-0004：历史不复活）。非法迁移稳定返回 `invalid_state_transition`，且**状态机合法性检查先于参数检查**（终态 → promote 缺参数也必须报状态迁移错误而非参数错误）。同状态的 activation 刷新是 maintenance 的显式白名单路径，不是状态迁移。
- 容量（max_items、kind 配额、token budget）以确定性驱逐执行：按 `(activation, created_us, id)` 升序把**active** item 转为 dormant（永不物理删除、不动 sources）；配额超额优先驱逐同 kind。
- maintenance 每批有界 500，但不得按 `created_us` 让永不删除的旧 dormant 行永久占满批次：sweep 顺序固定为 active（工作集受 max_items 约束）→ 已过期 dormant → 其余 dormant 按 `updated_us` 轮转。这样 500+ 历史积累下较新的 active 仍会按时衰减/过期，dormant 也能跨 tick 最终收敛。
- Promotion seam：`promote(promotion_target_type ∈ {note, task, episode, claim})` 仅记录 revision（status=promoted、target type、target id=NULL——由 Phase 4/5 的属主服务物化后回填）、audit 事件与目标引用；本阶段不创建任何 Note/Task/Episode/Claim 对象。
- `affect` 是普通 kind：无任何代码路径写 persona 表（测试断言 persona_revisions 行数与内容不变）。

### 4. 结构化 Recall 骨架的边界（Phase 6 预留）

- 仅组合 `recent_context`、`state`、`focus` 三条路由；路由以 `RecallRoute` port 注入,Phase 6 增加路由不得改动这三条的语义。
- 子 deadline 用 Monotonic Clock 计算：前两条路由各得总预算的 1/3,最后一条跑满剩余；协作式超时检查；超时路由入 `degraded_routes`（`route_deadline_exceeded`，retryable，fallback 描述），其余路由结果不受影响。**授权失败（AccessDenied/ScopeViolation）是请求级错误，直接抛出——绝不降级为路由失败**。
- 排序器版本化（RECALL_RANKER_VERSION=1）：固定分量权重、`(-final_score, category_priority, occurred DESC, id ASC)` 稳定键；candidate_id 由 `(route, resource_id, revision)` 哈希派生——同快照重放 100 次顺序与裁剪一致的前提。
- Orchestrator 在读取 watermark、分配 deadline 或执行任一路由前先权威校验 Agent/Space/Session 请求 scope；授权失败不能被不可达 watermark 的早退遮蔽。最终 Rehydrate 对每个候选回读 Canonical 并复核 Scope/Privacy/Status/Expiry/Revision/Tombstone；`minimum_watermark` 不可达时无关键 Route 可成功，始终稳定 `not_ready`——绝不返回旧数据却标记完整（§20.6）。
- Trace 只含 ID hash、数量、版本与耗时。**Persona 永不作为候选**。
- `/v1/recall` 的完整协议、Search、Usage、Recall Cache、FTS/Vector/Graph 归 Phase 6 冻结；本阶段契约面只发布 `/v1/recent-context`、`/v1/state/*`、`/v1/focus-items*` 与 admin rebuild。

### 5. Scope 语义在三个新面上的应用

- 请求 scope 只含调用方**显式命名**的维度：service 层不再把 space 的 group 成员关系复制进请求 scope（那是数据出处，不是请求维度；复制会要求调用方持有从未表达的 group 授权——实现期修复）。
- Space 热窗口按 §5.2 严格空值语义读取该 space 的 **session-less** 观察；session 内容经 session 窗口恢复。State 列表请求命名 space 时可见"该 space + 无 space"记录，命名 session 时同理。
- 原始近期内容默认不跨 Space：窗口查询以 tenant+agent+space 等值为结构性 WHERE，隔离不依赖后过滤；SpaceGroup 成员关系在原始内容面上不授予任何共享。
- **列表的请求侧 null 永远不是通配符**（审查修复，P0）：State 列表在请求未命名 space 时只匹配 `space_id IS NULL AND session_id IS NULL`（agent 级列表只见 agent 级记录），请求命名 space 而未命名 session 时排除 session 级记录；stored-null 继续向下可见（session 请求可见 space 级与 agent 级记录）。SQL 结构性收窄之外，service 层返回前逐条复跑 `scope_allows` 作为纵深防御——"请求侧 null 读到带值数据"这一类缺陷必须两道都过才会发生。
- **按 ID 访问以当前安全状态为准**（审查与复审修复，P0）：focus 的 get/activate/transition/history 要求 item 自身携带的 space 维度位于 `allowed_space_ids` 信封内；所有内容承载路径还在调用时复核当前及返回 revision 的 Privacy，并令 Tombstone 优先。completed 的幂等重放只缓存业务结果，不缓存授权：按 revision_id 回读第一次结果前必须重新执行 Scope/Privacy/Tombstone 终检，授权撤销或 Forget 后不得返回历史正文。

### 6. Job Kind 启用策略的推进

`observation.recorded`（验证已提交事实并为 target 调度 coalescable 的 recent 重建，dedupe key 携带 watermark）、`recent_context.maintenance`（确定性重建 + 过期清扫）、`focus.maintenance`（幂等衰减清扫，经 Schedule/Tick 持久调度）随本阶段启用——全部具备真实、已测试且幂等的 handler；其余 §17.4 kind 保持禁用（fail closed 不变）。

### 7. 审查加固：Focus 变更的幂等与状态机权威校验（Review Hardening）

- Focus 的 activate 与全部 transition **必须携带 Idempotency-Key**（§20.5 总不变量：所有写操作可安全重试）。指纹只覆盖调用方字段；重放按 revision_id 回读第一次写入的 revision，不再因世界已前移而退化为 `revision_mismatch`。幂等结果不冻结权限：回读前复核当前 item 信封、当前与目标 revision 的 Privacy 以及 Tombstone；安全状态优先于重放可用性。
- 状态机合法性的**权威校验在幂等执行体内的同一序列化写事务中进行**：读当前行 → 授权 → 合法性（先于参数检查，保持错误排序）→ 参数检查 → 写 revision → CAS。旧的"只读事务 preflight + 写事务不复验"存在 TOCTOU（预测下一 revision 的请求可能在并发方先推进到终态后仍写出非法状态）；重放路径在幂等层短路，因此这些状态相关检查全部位于 execute 回调内部——重放不会因第一次执行已改变世界而失败。
- `max_history_revisions=0` 语义钉死为"仅当前版本"：应用层以 `max(policy, 1)` 为 keep，repository 侧同样 floor 到 1（当前指针指向的 revision 永不裁剪）；裁剪不使用固定 500 行批次，策略上限在一次写事务内精确成立。

### 8. 审查加固：结构化 Recall 的 fail-closed 语义（Review Hardening）

- `candidate_limits`/`layer_budgets`/`token_budget` 在请求构造时校验非负：SQLite 把 `LIMIT -1` 当作无限制，负数上限绝不能到达 SQL。
- **三条路由都执行子 deadline 协作检查**（含空热窗口、每次潜在 I/O 的前后、State/Focus 的 namespace/候选迭代）；Orchestrator 在 Route port 返回后、记录 completed 前再做权威 deadline 检查，第三方 Route 忘记 post-I/O 检查也不能把超时结果标成 completed。
- `partial=True` 要求“存在降级路由且至少一个关键 Route 成功”（§18.3）；全部 Route 降级或 minimum watermark 不可达时无可用部分结果，始终抛稳定 `not_ready`。`allow_partial=false` 时任一路由降级同样抛 `not_ready`。
- 请求级 Agent/Space/Session 授权先于 watermark 早退和 Route 执行；不可达 watermark 不能把越权请求伪装成 `not_ready`。

### 9. 审查加固：契约自洽与 Scope 参数完备性（Review Hardening）

- `FocusView.promotion_target_type` 可空并纳入 enum（`[note, task, episode, claim, null]`）：除 promote 外的一切 revision 都携带 null。新增 CI 门禁用 JSON Schema 2020-12 校验全部 published fixture（valid 必须过、invalid 必须挂）——双 SDK 校验器为前向兼容对 view enum 宽松，无法发现"schema 比自己的 fixture 严"这类矛盾；forward/ fixture 目录专测 SDK 宽松性，不参与严格 schema 门禁。
- `listFocusItems` 与 `getStateHistory` 补齐 `space_id`/`session_id` 查询参数（session 依赖 space），`listStates` 的 `session_id` 在 OpenAPI 与双 SDK 间对齐——Space/Session 级资源必须能经发布契约完整访问。Focus 变更端点在契约中要求 `Idempotency-Key` 头，mock server 同步执行。

## 否决的替代方案

- Recent 投影直接在读取路径懒构建并缓存（无 generation/pointer）：无法表达"上一已验证版本"与原子切换,违反 ADR-0001 的版本化投影纪律。
- State 用 upsert 原地覆盖 + `updated_at`：违反 ADR-0004 不可变 revision 语义,也无法做并发裁决。
- 衰减按"每次乘以衰减因子"作用于存储值：重复运行指数复合、不幂等,maintenance 无法安全重放。
- Focus 容量靠物理删除最旧 item：删除来源事实,违反"淘汰=状态转换且保留历史"。
- 授权失败降级为路由失败：把横向越权变成可重试的"部分结果",安全语义完全错误。
- 用复合列 UNIQUE 表达含 NULL 维度的 scope 唯一性：SQLite 将 NULL 视为互异,唯一性形同虚设。
- by-ID 授权只查 tenant+agent（space 维度留给"资源 scope 自检"）：自比恒真,等于没有授权。
- 列表请求未命名的维度不加 WHERE（当作通配符）：请求侧 null 一旦成为通配符,agent 级列表立即横向泄漏所有 Space/Session 记录。
- Focus 变更不带幂等键、靠 expected_revision 天然防重：响应丢失后的合法重试只会得到 revision_mismatch,违反 §20.5"所有写操作可安全重试"的总不变量。

## 后果

- Schema 4 新增 8 张 STRICT 表（recent generations/pointer、state policies/records/revisions、focus items/revisions）；`state_namespace_policies` 允许租户覆盖策略但内置默认保证零配置可用。
- 备份恢复不变量扩展：state/focus 指针必须解析到对应 revision 行；recent 指针必须精确绑定同 target 的 verified generation，projection 内容哈希/结构必须自洽，引用 observation 的身份、revision 与 occurred time 必须一致。
- 错误码未新增（invalid_state_transition/revision_mismatch/idempotency_key_reused/history_unavailable 等已在 v1 契约内）。
- 性能口径：State Coalesced Write p95 以 Phase 2 同口径（单流、coalescing 生效）测量；8 并发突发在单 Writer Gate（§20.2）后的排队延迟另行如实报告,不混入写入路径口径。

## 迁移影响

- `migrations/0004_phase3_recent_state_focus.sql`（online_safe=true, lock_ms=200, min_app=0.4.0, recovery=none）；0001–0003 字节不变。
- 兼容窗口 [3,4]：0.4.0 在线升级 Schema 3 库；Schema 2 库经 0.3.0 二进制分阶段前移（MigrationRunner 本身仍可一次走完 2→4,窗口只约束 Ready）。
- 不提供 Down Migration；State/Focus revision 与 recent generation 不经降级脚本删除或回拨。
