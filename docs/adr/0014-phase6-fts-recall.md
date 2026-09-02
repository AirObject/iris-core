# ADR-0014: Phase 6 — FTS5 Projection Generations、完整 Recall 协议、新鲜 Rehydrate 与 Usage 四阶段

- 状态：Accepted（2026-09-02 实现期对抗性复审补充 §12）
- 日期：2026-09-02
- 影响阶段：Phase 6（Phase 7/8/10/11/12 依赖本决策的语义）
- 基线：§18、§22.1、§22.6、§23、§30、§31；ADR-0001/0002/0004/0005/0006/0011/0012/0013

## 背景

Phase 6 交付首个完整 Recall 协议：FTS5 可重建投影、结构化 + FTS 组合路由、Deadline/预算、最终 Canonical Rehydrate、稳定降级 Envelope、Usage 四阶段与 `/v1/recall`、`/v1/recall/{request_id}/usage`、`/v1/search` 契约。实现中存在多项跨阶段、不可逆或影响公共契约的语义选择，冻结如下。

## 决策

### 1. FTS Generation/Builder 与影子重建

- **可索引资源集**：`claim`、`episode`、`note`。文本模板版本化（`FTS_TEXT_TEMPLATE_VERSION=1`）：claim = `predicate + ": " + canonical_text`；episode = `title + "\n" + summary`；note = `title + "\n" + body`。Observation 不进 FTS（热窗口由 recent route 负责，原始正文最小化搜索面）；Artifact 永不索引——`external_ref` 结构性不抓取（ADR-0013 §4），inline/local blob 是二进制为主的内容，其 metadata 无可搜文本；`restricted` 与 subject-private 资源的**索引入口**不设例外（隐私评估只在读取/rehydrate 侧：索引行带 privacy_labels，读取时评估——索引本身只存已授权写入的 Canonical 文本，与 Canonical 读取面同权限）。
- **Generation 模型**：`fts_generations`（不可变，status ∈ {verified, retired}）+ 每租户一行 `fts_current` 指针 + `fts_documents` 文档行。文档绑定 `(tenant, resource_type, resource_id, resource_revision)`，并保存 content hash、builder/tokenizer/config 版本、source/tombstone watermark、scope 五维、privacy_labels、canonical status、valid time 与规范化 index_text——全部可重建元数据，无任何事实源地位（ADR-0001）。
- **单一 FTS5 虚拟表**：`fts_index` 为 external-content 表（`content='fts_documents', content_rowid='id'`, tokenizer `unicode61`），由投影服务在首次构建时幂等创建（**不在 migration 内创建**——migration 不得因 FTS5 编译差异失败；运行时探测 `fts5` 模块不可用时 FTS Capability fail-closed 降级，结构化 Recall 不受影响）。影子重建期间多 Generation 文档行共存于同一虚拟表（rowid 互异），搜索按 `generation_id = current AND doc_status='active'` 结构性过滤。
- **版本化三元组**：`FTS_BUILDER_VERSION`、`FTS_TOKENIZER_VERSION`（当前 1：unicode61 + 构建器侧 casefold/空白归一/受控停用词表仅用于查询侧而非索引侧）、`FTS_CONFIG_JSON`（语言、规范化、停用词声明）。任一版本变化 ⇒ 新 Generation 影子重建；未来更换 FTS5 tokenizer（如 porter）需要新的虚拟表面（新 ADR）。
- **影子构建与原子切换**：rebuild 在一个写事务内（受 Writer Gate 串行化）：从 Canonical 有效 Revision 全量拉取 → 构建新 Generation 文档 → 校验（数量、确定性 content checksum、抽样查询命中、source/tombstone watermark 记录）→ `fts_current` 指针 CAS 切换 + 旧 Generation 置 retired。任一步失败整个事务回滚——指针不动，读取继续使用上一已验证 Generation；**不存在部分可见的 building 状态**（与 ADR-0011 §1 recent generation 同纪律）。首次构建没有上一 Generation 时失败即无 FTS（route 降级），不阻塞结构化路由。
- **读取信任门**：搜索路径在每次查询时验证：指针存在、state=ready、generation status=verified、builder/tokenizer 版本 ∈ 已知版本集、（可选）落后不超过策略阈值。任何不满足 ⇒ 该 route 稳定降级码（`fts_rebuild_pending` / `fts_builder_unknown` / `fts_generation_stale` / `fts_index_corrupt`），绝不返回不可信结果。物理损坏（SQL 错误、checksum 复核失败）同样降级且不崩溃请求。

### 2. Revision/Tombstone 失效与物理清理

- 增量维护消费**既有 refs-only 变更事件**：`claim.changed`/`episode.changed`/`note.changed` handler 在指针不变量检查通过后追加 `fts.apply` job（payload 只含 resource ref + watermark，dedupe key 携带 watermark，coalesce key = resource 身份——同资源多次变更合并为一次应用）；`memory.invalidated` handler 为 chunk 内每个 FTS 可索引资源追加 `fts.apply`（apply 复读 Canonical，发现 Tombstone 即逻辑失效）。
- `fts.apply` 语义：同租户同资源在**当前 Generation 内至多一个文档行**（UNIQUE(generation_id, resource_type, resource_id)）。Revision 推进 ⇒ 同事务内旧行按新 Revision/text 原地替换（external-content FTS 先发 delete 命令再插入新文本）；Tombstone ⇒ `doc_status='invalid'` + `invalidated_us`（逻辑失效即刻生效），物理删除（行 + FTS delete + retired Generation 清理）由 `fts.cleanup` 异步执行。**过期 Revision 永不可能成为可信返回值**：即使物理清理滞后，FTS 命中的每个候选都必须带 resource_revision 通过最终 Rehydrate 的 Canonical Current Revision 精确匹配，任何不匹配（stale_revision）被剔除。
- FTS 落后（apply 积压）不阻塞读取：落后只影响召回完整性，不影响正确性；落后超过阈值时 route 以 `fts_generation_stale` 降级（策略阈值可配，默认 10_000 watermark 落后）。

### 3. Recall Envelope、路由并发与快照边界

- **路由名差异的显式裁定**：内部已冻结路由名 `tasks`（ADR-0011 §4、ADR-0012 §6：due-task 路由经 tasks 服务注入，名 `tasks`）与基线 §18.3 示例 `task` 存在拼写差异。`/v1/recall` 于本阶段**首次发布**，wire 枚举冻结为内部名：`recent_context`、`state`、`focus`、`tasks`、`claims`、`relations`、`fts`（`persona` 不是路由——Persona 经响应顶层字段协调，绝不做候选）。基线的 `task`/`profile`/`vector`/`graph` 是示例清单而非规范拼写；本 ADR 裁定以已发布的内部名为准，避免静默改变 Phase 3/4 已测试语义。Phase 7/8 新路由继续沿用资源复数名（`vector`、`profile`、`graph` 按基线示例保留原拼写，因其无既有内部名冲突）。
- **有界并行**：路由以线程池并行执行（并发上限默认 4，可注入）；**每个并行路由打开自己的 read Unit of Work（独立连接 + BEGIN DEFERRED 快照）**——SQLite 连接/事务绝不在线程间共享（sqlite3 默认 check_same_thread 语义与快照隔离共同要求这一点）。编排线程自己的控制事务只做请求授权、watermark 检查与 Persona 读取。路由结果按**固定路由顺序**合并（非完成顺序），保证同快照重放确定性。
- **新鲜 Rehydrate 边界（P6 关键不变量）**：候选收集完成后，最终 Canonical Rehydrate 在**新开**的 read Unit of Work 中执行——绝不复用候选收集前打开的陈旧 SQLite read snapshot。该屏障保证：收集期间并发提交的 Forget/Tombstone/Correct 在 Rehydrate 时必然可见；协调屏障测试（FTS 命中旧条目后、Rehydrate 前并发 Forget 提交）要求成功提交后旧内容返回数为 0。pending_event_ids 读取同样位于新鲜事务。
- **Deadline 单次换算**：API 的墙钟 `deadline_at` 只在 RecallService 入口换算一次为单调时钟 deadline（`deadline_monotonic = now_monotonic + max(0, deadline_at - now_wall)`）；之后所有子 deadline、路由检查、权威复查全部使用单调时钟，墙钟回拨不影响超时判定（§18.4）。
- **Partial 语义**沿用 ADR-0011 §8：`partial=true` 当且仅当存在降级路由且至少一个关键 Route 成功；全部关键路由失败或 `allow_partial=false` 且有降级 ⇒ 稳定错误。对外错误码细化：minimum watermark 不可达 ⇒ `minimum_watermark_unavailable`（本阶段进入契约错误码集）；全部路由因 deadline 失败 ⇒ `deadline_exceeded`；其余不可服务 ⇒ `not_ready`。授权失败（AccessDenied/ScopeViolation）永远是请求级错误，先于 watermark 早退与路由执行（不得以 `not_ready` 掩盖越权探测）。
- **ExternalActor 解析**：`actors` 至少含当前说话人的 `(provider, external_id)`；服务端经身份注册表解析：ExternalIdentity → verified binding → entity。不信任调用方声明的内部 Entity ID（请求不接受 entity_id 字段）。第一 actor（说话人）解析失败 ⇒ `identity_not_found`（新增契约错误码）；后续 actor 解析失败仅降权/忽略并记 trace 计数（直播批次容错）。说话人 entity 参与 Token 预算的"必要身份"保障与 FTS 查询扩展（说话人 subject 的 claim 加权）。

### 4. Persona 与候选边界

- Persona 只经响应顶层 `persona_revision` + `persona_content_hash` 返回（Phase 1 bootstrap persona 的 current revision；无 persona 时 revision=0、hash=""，SDK 不隐藏该状态）。Persona 修订进入 Recall Cache 键（见 §6）与 Usage 验证（report 携带 persona_revision 必须与请求时一致，`revision_mismatch` 否则）。Persona 永不作为候选（`test_persona_is_never_a_candidate` 延续）。

### 5. 排序、预算与确定性

- **Ranker v2**（`RECALL_RANKER_VERSION=2`，Phase 3 v1 的确定性纪律不变）：分量权重固定并版本化——relevance 0.28、authority 0.12、confidence 0.12、importance 0.14、accessibility 0.08、activation 0.06、recency 0.12、task_urgency 0.08，减 conflict_penalty 0.15 与 redundancy_penalty 0.10。**缺失分值 ≠ 0**：`scores` 是 `dict[str, float | None]`，缺失分量（键不存在或显式 None）既不贡献分数也不参与该候选的分量归一，且在候选上留下 `missing_components` 记录（trace 汇总数）；显式 0.0 是真实零分。`final_score = Σ(存在的分量 × 权重) / Σ(存在分量的权重) × 归一因子 - 惩罚`，保证缺失分量不会把候选拉到虚假零分。
- **稳定排序键**不变：`(-final_score, category_priority, occurred_at DESC, resource_id ASC)`；category priority：tasks 0、focus 1、claims 2、recent_context 3、relations 4、state 5、fts 6。
- **冲突/冗余标记**：同 `(subject_entity_id, predicate)` 的多个 claim 候选互标 `conflict_state="conflicts"`（含 disputed）；同 content_hash 的候选其后代者标 `conflict_state="redundant"` 并施加冗余惩罚（保留最高分者不惩罚）。标记是确定性的（同快照重放一致）。
- **预算保障顺序**：裁剪先保留 Due Task（`tasks`）、当前 Focus（`focus`）与说话人必要身份（`subject_entity_id == 说话人 entity` 的 `claims/identity` 类候选）——这些受保护候选**不参与** Token 上限竞争（仍受各自 candidate cap 与 layer budget 约束）；其余候选按稳定顺序在剩余 Token 预算内装入。Layer budget 语义不变（Phase 3）。Token Estimator 版本化（`TOKEN_ESTIMATOR_VERSION=1`，chars/4）。
- **群成员私有记忆隔离**：请求 scope 命名 space/space_group 时，其他成员的 subject-private / restricted 资源经 Privacy evaluator（唯一评估器，ADR-0013 §11）在 Rehydrate 复核中被剔除——同处一个 Space 绝不扩大隐私可见性；SQL 侧结构性 scope 过滤先于排名。

### 6. Recall Cache：本阶段不实现

Phase 6 不实现持久 Recall Cache（明确非目标；Phase 7+ 与 Vector 一并评估）。`cache_until` 诚实返回 `null`（服务不背书任何复用边界）；未来引入缓存时键必须完整包含 Scope/Actor/Purpose/Query、source/tombstone watermark、persona revision、schema/ranker/estimator 版本（§22.6），命中后仍必须 Canonical Rehydrate——本 ADR 预先冻结该键语义，防止后续阶段以弱键引入。

### 7. Usage 四阶段与防伪

- 持久化 `recall_requests`（Core 在 recall 提交时写入 `retrieved_count`/`returned_candidate_ids`/persona revision/ranker+estimator 版本）与 `recall_usage_reports`（宿主 report 写入 host_selected/model_visible 阶段）。四阶段：`retrieved`（路由收集数，含被 Rehydrate 剔除者）、`returned`（实际返回集）、`host_selected`、`model_visible`。
- 验证：`returned_candidate_ids` 必须**精确等于**该 request 存档的 returned 集（完整性回显，非子集）；`model_visible ⊆ host_selected ⊆ returned`；所有 id 属于该 request/tenant/agent；`persona_revision` 与请求存档一致；同一 `(tenant, request_id, host_cycle_id)` 的重复 report 幂等合并（唯一索引 + 回读首次结果），重复 100 次只产生一次逻辑累计。任何伪造（跨 tenant/request 候选、子集破坏、回显不完整）⇒ `invalid_request`，伪造成功数为结构性 0。
- Usage 记录**只写 usage 表**：Phase 6 不将 usage 回写 claim accessibility/focus activation（ADR-0011 §3 预留的"经验证真实 usage"激励路径归 Phase 10 consolidation，需独立评估 revision churn 与可审计性）；结构性保证 usage 代码路径永不触碰 confidence——回归测试断言 usage 前/后 claim 行逐字节不变。
- 隐私：usage 行只保存 candidate id 与阶段计数，不保存 prompt 或候选正文（canary 扫描覆盖）。

### 8. Search 端点

`POST /v1/search` 是 FTS 支撑的跨资源搜索（claim/episode/note），复用 FTS Route 的同一信任门、结构化 scope 过滤与 Canonical Rehydrate；查询串经 `build_fts_query` 规范化（引号包裹 token、AND 连接、上限 16 token，拒绝空查询）——用户输入永不直接拼进 FTS5 MATCH 语法。Phase 5 的 `GET /v1/claims`（结构化、无 FTS）语义不变。

### 9. Backup/Restore 与 FTS

- Backup（Online Backup API，整库快照）**可以**携带 FTS 表字节，但 Restore 在 staging 内（ledger 重放后、切换前）执行 `reset_fts_after_restore`：清空 fts_documents/fts_generations/fts_current、DROP fts_index 虚拟表（连带 shadow 表）、`fts_projection_state` 置 `pending_rebuild`。**FTS 绝不作为事实源恢复**——恢复后的第一次搜索在重建前以 `fts_rebuild_pending` 降级；admin rebuild 重建 Generation 后回到 ready。恢复不变量：state=ready ⇒ 每租户指针解析到同租户 verified generation；文档行引用的 generation 存在；invalid 文档必带 invalidated_us。
- `fts_projection_state`（全局单行：`never_built`/`ready`/`pending_rebuild`）同时是 FTS Capability 与 `/health/ready` 的投影状态输入（§31.4"当前索引可用或允许的降级状态"）。

### 10. 契约与版本

- 同一 Release Train：Core/双 SDK 0.7.0、Schema 7（migration `0007_phase6_fts_recall.sql`，min_app=0.7.0）、Contract 1.5.0（additive：3 路径 `/v1/recall`、`/v1/recall/{request_id}/usage`、`/v1/search`；capabilities 新增 `recall.v1`、`recall.usage.v1`、`search.fts.v1`；错误码新增 `deadline_exceeded`、`identity_not_found`、`minimum_watermark_unavailable`——均在 §23.4 冻结清单内）。Runtime 兼容窗口 [6, 7]。
- SDK 显式暴露：Scope、partial、degraded_routes（含原因码/retryable/fallback）、persona revision/hash、cache_until=null、usage 四阶段；未知可选枚举向前兼容（forward fixture：completed_routes 含未来路由名），SDK 对未知路由名不失败。
- Degraded 原因码冻结集：`route_deadline_exceeded`、`route_failed`、`fts_rebuild_pending`、`fts_builder_unknown`、`fts_generation_stale`、`fts_index_corrupt`、`fts_unavailable`。

### 11. Job Kind 推进

启用 `fts.apply`（priority 5，coalesce=fts:resource）、`fts.rebuild`（priority 3，catch_up=latest，admin/能力触发）、`fts.cleanup`（priority 8，catch_up=latest）。三者均具备真实幂等 handler；`CoalesceClass` 新增 `FTS`。变更 handler 追加 enqueue 属 additive——FTS 未构建时 `fts.apply` 快速 no-op（无当前 Generation 即返回）。

### 12. 实现期对抗性复审补充（Review Hardening）

六个攻击面的主动探查（回归见 `tests/integration/test_phase6_review.py`）冻结以下补充语义：

1. **Search 的快照语义**：`/v1/search` 在**单个**读事务内完成"FTS 命中 → Canonical 回读 → 硬过滤"，因此是快照一致的——同一事务内提交不可见属于一切单事务读取的通用语义（与 Phase 5 ClaimService.search 同法）。新鲜 Rehydrate 屏障（§3）只约束**跨事务**的编排流程（路由收集 → Rehydrate 两段）。两者不混淆。
2. **停用词 topic**：`build_fts_query` 对全停用词/空白查询抛 `invalid_request`；FTS Route 将其转化为**完成且零候选**（查询形状结果，非投影故障），绝不以 `route_failed` 误导降级。
3. **FTS 查询自身的 Tombstone 排除是最后防线**：即使 `fts.apply` 完全未运行（物理清理/逻辑失效滞后），FTS SQL 的 `NOT EXISTS (resource_tombstones)` 也使已删除资源不可命中；重建同样跳过 tombstoned 资源（`test_forget_then_search_without_async_apply_never_resurrects`、`test_rebuild_after_forget_never_resurrects`）。
4. **Generation 残留结构性不可表达**：`UNIQUE(generation_id, resource_type, resource_id)` 使同一资源在单代内至多一行；搜索按 current generation SQL 过滤，旧代文档（物理清理滞后期间）不可服务。
5. **MigrationRunner 连接生命周期**：runner 的连接改为显式 `closing()`——`with connection` 只界定事务不关闭句柄；泄漏的打开连接会在 WAL 库上钉住 sidecar，使 restore 后续的 journal-mode 切换失败（`database is locked`）。该修复对既有迁移语义零影响。
6. **Restore 不前向迁移**：staging 只重置已有 FTS 表（Schema ≥7 快照）并标记 `pending_rebuild`；Schema 6 快照由**启动迁移**自然到 7（`fts_projection_state` 缺省 `never_built`，同样必须重建后 FTS 才可用）。restore 永不运行 migration runner——带自定义迁移记录的探针数据库必须能被 restore 接受。
7. **Readiness 的投影状态语义**：`/health/ready` 新增 `checks.fts_projection_state`（additive）；`never_built` 是全新安装的正常态**不**降级 readiness，`pending_rebuild`/`unavailable` 标记 degraded（reason `fts_projection_rebuild_pending`），永不 not_ready——结构化 Recall 仍在服务（§31.4"允许的降级状态"）。

### 13. 发布前外部复审修复（Review Round 2, 2026-09-02）

CI 全绿后的一轮发布阻断级复审确认了 9 项缺陷，全部修复并落回归（`tests/integration/test_phase6_review_round2.py`，20 例）。冻结以下语义：

1. **Deadline 是有界等待**：并行路由的 future 逐个以"剩余总预算"为上限 `result(timeout=…)`；超时路由标 `route_deadline_exceeded` 并**弃置线程**（`shutdown(wait=False, cancel_futures=True)`，绝不 join）——被阻塞的路由不能把响应拖过请求 deadline。被弃线程自持读连接，结束后自行关闭，结果被丢弃。
2. **Claims 路由的 as_of 下推进 SQL**：`search_page(as_of_us=…)` 以系统时间重建"当时 current"的 revision 并把 status 过滤施加于**重建出的 revision**（Phase 5 仓储既有能力）——"历史 active、现已 retracted"的 claim 在候选阶段不再消失。
3. **FTS scope SQL = scope_allows 的逐维下沉**：每维 `D IS NULL OR (R NOT NULL AND D = R)`；请求维为空只匹配该维更宽作用域文档（请求侧 null 永不是通配符）。`space_group_id` 成为 recall/search 请求的可选维度（契约 scope additive，授权对照 `allowed_space_group_ids`）——Group 作用域内容可正向召回，会话隔离不因 SQL 前置过滤缺位而泄漏。
4. **Purpose 授权与 Speaker 注入口闭合**：`AccessContext.data_purposes` 非空时，请求 purpose 不在授予集 ⇒ `access_denied`（枚举校验不是授权）；空集保持"已知 purpose 均可"的历史行为。`RecallService.recall` 只接受外部 actor 引用并在服务内经身份注册表解析 speaker——公开边界不存在内部 entity id 注入参数。
5. **重建完整性与校验闭环**：episodes/notes 与 claims 一律键集分页（取消每 Agent 10,000 截断——"已验证但不完整"是最坏结果）；`_verify_generation` 除计数/样本查询外，**从持久化行重算 checksum** 并与 generation 记录比对。
6. **FTS 落后信任门是逐 Agent 口径**：新表 `fts_generation_agents(tenant, generation, agent, source_watermark)` 记录每代每 Agent 的"已应用 watermark"——重建时按所有持有 watermark 的 Agent 播种（含无可索引资源者），`fts.apply` 消费事件时推进（payload 携带事件 watermark），信任门比较**请求 Agent** 的 live seq 与其 applied 值。租户级最大值既会掩盖落后 Agent 也会把追平 Agent 永久判 stale，仅作报告元数据。（该机制次轮被 §14.1 的未结算 backlog 信任门取代；`fts_generation_agents` 表在 Schema 7 发布前即移除。）
7. **Ranker 语义**：bm25 归一化为 `|bm25|/(1+|bm25|)`（相关性单调递增；`1/(1+|bm25|)` 方向反了）；冗余标记的胜者是**稳定排序最高分者**（不再回退到路由输入顺序）。
8. **Usage 幂等重放即回放首次**：同一 `(tenant, request, host_cycle)` 的重放若 stage 集与存量不同 ⇒ `invalid_request`（冲突重放）；一致则返回**存量**计数——响应永不描述未持久化的第二次 payload。
9. **退休 Generation 清理必须发 FTS5 delete 命令**：external-content 影子表的 postings 不随内容行 DELETE 消失；`delete_retired_generations` 先逐行 `'delete'` 命令再删行（否则索引永久膨胀、MATCH 成本增长）。

### 14. 发布前外部复审修复（Review Round 3, 2026-09-02）

第二轮修复后的复审确认 6 个未闭合边界与 1 个新增问题，全部修复并落回归（`tests/integration/test_phase6_review_round3.py`，15 例）。冻结以下语义：

1. **信任门口径改为"未结算 backlog"（取代 §13.6 的 per-agent applied watermark）**：applied watermark 簿记在两个方向上都是错的——单次 apply 以 `MAX()` 把 applied 推到 live seq，**跳过仍在排队的更早任务**（false fresh）；非索引流量推进 live seq 却永不产生 `fts.apply`（追平 Agent 被永久判 stale）。信任门现比较**请求 Agent 的未结算 `fts.apply` 任务数**（`outbox.unsettled_job_count`，pending/leased/retryable）：任务的围栏完成 CAS 与投影写同事务提交，计数天然是连续消费前沿，无需维护且崩溃安全。`fts_generation_agents` 表自 Schema 7 移除（未发布），`fts.apply` payload 回到纯 refs。`_schedule_fts_apply` 在触发事件未携带 Agent 时按资源归属解析 owner（Forget 驱动的 `memory.invalidated` 以 `agent_id=None` 入队）。
2. **Speaker 注入点彻底闭合**：`StructuredRecallRequest.speaker_entity_id` 是调用方可构造字段——服务入口**无条件清空**该字段，仅服务端 actor 解析可回填；actorless 请求保持 actorless（§18.1"永不信任调用方内部 ID"在服务边界强制）。
3. **组-空间组合必须命中真实绑定**：`space_group_id` 与 `space_id` 各自获准不充分——两者同时出现时必须命中活跃 `space_group_bindings` 行（recall `_authorize_request` 与 `authorize_scope` 同法，覆盖 Search 与写路径），否则 `access_denied`；"分别授权的无关组合"不再可读两侧并集。
4. **单路由/单线程同样有界**：`executors <= 1` 的同步快速路径删除——单路由或 `max_route_concurrency=1` 也经线程池提交并以剩余预算限时 `future.result()` 等待，超时路由弃置线程（§13.1 同法）。
5. **as_of 的 Valid Time 判定点**：claim 路由 `search_page(valid_at_us=as_of_us)` 下推；最终 Rehydrate 与 relation 路由/Rehydrate 同以 `as_of`（未指定则 now）评估 `valid_from/valid_until`——"当时有效、现已过期"保持可历史召回，"当时尚未生效"不进历史结果。
6. **Tombstoned Note 不进新代**：`list_notes` 增加与 claims/episodes 枚举同法的 SQL 级 `resource_tombstones` 排除（`include_tombstoned=True` 显式要求才包含）——已删除 Note 的正文不再随影子重建回流索引、破坏异步物理清理语义。
7. **Recall 请求级幂等 = 响应重放**：`recall_requests` 增加 `request_fingerprint`（排除 deadline 的逻辑内容摘要——transport 重试天然携带新 deadline）、`response_json`（完整服务响应）与 `resource_ids_json`。同 id 重放先校验指纹（不一致 ⇒ `invalid_request` 冲突重放），一致则**逐字回放首次响应**——状态变化与新 deadline 都不改变答复，宿主对任一次执行的 usage 回显都落在同一 returned 集。Forget 失效在**同一写事务**内擦除引用了被失效资源的存储响应（`scrub_request_responses`，异步 `memory.invalidated` handler 幂等兜底）；被擦除的请求重放以 `conflict` fail-closed，删除内容不可能经重放路径复活。

## 否决的替代方案

- migration 内创建 FTS5 虚拟表：迁移在无 FTS5 编译的 runtime 上直接失败，违反"FTS 不可用时结构化 Recall 不受影响"的降级承诺。
- 每 Generation 一个 FTS5 虚拟表（`fts_index_g<N>`）：DDL 随版本无限增长、DROP/CREATE 竞态面大；单虚拟表 + generation 过滤给出同样的原子切换语义。
- FTS 文档行保存原文之外的派生字段（摘要、分词）：投影只保存可重建元数据与规范化文本；任何派生分析归 Canonical/后续阶段。
- 缓存命中跳过 Rehydrate：违反 §18.5 与 ADR-0001（本阶段无缓存亦不允许未来绕过）。
- 顺序执行路由以满足并行要求：不满足 §18.4 的并行要求；真实瓶颈（FTS match + 结构化 join）需要并行掩盖。
- 在共享控制事务内做 Rehydrate（Phase 3/4 现状）：复用收集前快照使并发 Forget 在 Rehydrate 不可见——旧内容可返回，违反删除竞态门禁。
- Usage report 直接更新 claim accessibility/focus activation：未经评估的 revision churn 与审计语义；Phase 10 以"经验证的 usage"专门设计。
- 把 `tasks` 路由改名为 `task` 对齐基线示例：静默改变 ADR-0011/0012 已冻结并在测试中锁定的内部语义，违反"不得静默改变已存在语义"。

## 后果

- Schema 7 新增 STRICT 表（fts_generations、fts_current、fts_documents、fts_projection_state、recall_requests、recall_usage_reports——共 6 张；§13.6 的 `fts_generation_agents` 发布前被 §14.1 取代移除）+ external-content FTS5 虚拟表（运行时创建）；0001–0006 字节不变。
- 契约 1.4.0 → 1.5.0（additive）；fixtures 新增 recall/usage/search 组。
- Recall Orchestrator 从单事务顺序模型升级为"控制事务 → 并行路由事务 → 新鲜 Rehydrate 事务"三段式；Phase 3/4 语义（授权顺序、fail-closed、稳定排序、安全 trace）保持。
- Restore 演练扩展 FTS 重置断言；性能基线新增结构化/FTS Recall p95 门禁。

## 迁移影响

- `migrations/0007_phase6_fts_recall.sql`（online_safe=true, lock_ms=200, min_app=0.7.0, recovery=none）；0001–0006 与 HEAD `b7bbad5` 逐字节一致（测试锁定）。
- 兼容窗口 [6, 7]：0.7.0 在线升级 Schema 6 库；Schema ≤5 需先经 0.6.0 二进制（Runner 可多步走完，窗口只约束 Ready）。
- 无 Down Migration；FTS Generation/文档与 usage 行不经降级脚本删除或回拨。回退顺序：停用 fts.* handler 与 `/v1/recall` 流量 → 兼容二进制运行 Schema 7 → 必要时按 ADR-0013 §10 恢复流程回退备份（FTS 重建由 0.7.0 重新触发）。
