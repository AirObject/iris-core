# ADR-0016: Phase 8 — ProfileProjection、Relation Graph 与受限 Graph/Profile Recall

- 状态：Accepted
- 日期：2026-09-03
- 影响阶段：Phase 8（Phase 9/10 依赖本决策的投影/降级/回退模式）
- 基线：§13.3、§13.5、§18、§19、§20.6–20.7、§21、§22.5–22.6、§23、§30–33、§36 阶段 8；ADR-0001/0002/0004/0005/0013/0014/0015
- 前置：ADR-0014（Recall Envelope、三段式编排、新鲜 Rehydrate）、ADR-0015（Generation/fencing/新鲜度纪律——本阶段整体沿用）

## 背景

Phase 8 在 Phase 6/7 的 Recall 协议之上增加两个可重建投影：Entity/Relationship/SpaceGroup
的 ProfileProjection（字段级、带来源汇总）与 Relation Graph（只投影授权 Canonical 边），
并为 Recall 增加严格受限的 `graph` route 与安全的 `profile` route。任何投影落后、损坏或
Binding 变化都必须能回退 Canonical Claim/Relation，绝不放宽逐边授权。

## 决策

### 1. 版本与 Release Train

- Core / Python SDK / TypeScript SDK **0.9.0**；Schema **9**（migration
  `0009_phase8_profile_graph.sql`，min_app=0.9.0，online_safe=true，lock_ms=200，
  recovery=none）；Contract **1.7.0**（纯 additive：capability `recall.graph.v1`、
  `profile.v1`；路由枚举 +`graph`/`profile`；降级原因码 +`graph_*` 5 个、`profile_*` 5 个；
  错误码集不变）。
- Runtime 兼容窗口 **[8, 9]**：0.9.0 在线升级 Schema 8 库；Schema ≤7 需先经 0.8.0 二进制。
- 0001–0008 字节不变（回归锁定扩展到以 `869f04d` 为基线的 0008）。

### 2. ProfileProjection（§13.5）

- **主体（subject）三种**：`entity`（主体=Entity id）、`relationship`（主体=规范化有序
  实体对 `min|max`，仅聚合两端各自的 relationship 类 Claim，按端点分字段，永不跨主体
  合并）、`space_group`（主体=SpaceGroup id，聚合 scope 维 `space_group_id == G` 且
  category ∈ {community, fact} 的 Claim，按 (subject, predicate) 分字段）。单一字段
  （field row）的来源 Claim 必须共享同一 `scope_key` 与同一 privacy 标签集合——**跨
  Scope/privacy 拼接结构性不可表达**（字段分组键含 scope_key 与 privacy 键）。
- **字段模型（builder v1，确定性映射）**：字段 = (subject, section, predicate 分组)。
  分区规则冻结：`identity` ← category identity；`preference` ← category preference；
  `relationship` ← category relationship；`experience` ← category fact 且
  importance ≥ 0.5；`goal` ← category fact 且谓词（小写）前缀命中冻结词干表
  `goal|plan|aim|want|aspire|intend`；`recent_change` ← 上述任一分区中
  `recorded_at ≥ reference_us − 7d` 的 Claim，其中 `reference_us` = 该主体全部合格
  Claim 的最大 recorded_at（**快照自身锚定**——重建时间不进入内容，保证同快照同
  checksum）；`interaction` ← category procedure（§13.4 声明式交互习惯）。
  SpaceGroup 主体的分区为 `community`/`fact`（+`recent_change`）。
- **Agent Persona 隔离**：Profile 的唯一来源是 Canonical Claim 的当前 Revision；
  Persona Revision/State 永不作为 Profile 来源；`self_narrative` 类 Claim 永不进入
  Entity Profile 字段（该类内容归 Persona/Narrative 管线，Phase 9）。Agent-kind
  Entity 的 Profile 同样只由 Claim 汇总，不含任何 Persona 数据。
- **每字段保存**：source claim ids + revisions（JSON）、builder_version、
  source/tombstone watermark（generation 级）、freshness_us（=最新来源 Claim 的
  recorded_at，非构建时刻）、conflict_state ∈ {single, conflict, disputed}、scope 五维、
  privacy_labels、valid_from/until、value_json（冲突时**显式并列全部冲突值**，
  绝不静默择一或合并）与 summary 文本。
- **冲突语义**：同一 (subject, section, predicate, scope_key, privacy) 下存在多条
  不同 `value_hash` 的可见 Claim（status active/disputed）⇒ 字段 conflict_state =
  conflict（多条来源全列）；任何来源 status=disputed ⇒ disputed；单条 ⇒ single。
  去重键相同（同 value_hash）的多条 Claim 合并为一条来源集。
- **Generation/影子重建**：`profile_generations`（不可变，status ∈ {verified, retired}，
  携带 builder_version、source/tombstone watermark、field_count、subject_count、
  content_checksum、agent_watermarks_json）+ 每租户 `profile_current` 指针
  （switch_epoch fencing CAS）+ `profile_subjects`（每主体 field_count 与
  subject_checksum；generation checksum = 对排序后 (subject_key, subject_checksum)
  的确定性摘要）+ `profile_fields`（内容行）。重建 = 新 generation 影子构建 →
  验证（计数、来源覆盖、从持久化行重算 checksum）→ 指针 CAS 原子切换 → 旧代
  retired（保留受限回退窗口，默认 24h，可配）。**同一 Canonical Snapshot 与
  Builder Version 的重建产出相同来源集合、字段集合与 checksum**（generation id
  与时间戳不在 checksum 材料内）。
- **增量刷新**：`profile.apply` 在当前 generation 内重派生受影响主体的字段行
  （删除该主体行 → 从 Canonical 重算插入），同步更新主体行与 generation 的
  field/subject 计数、subject checksum 与 content_checksum；并推进 generation 的
  `agent_watermarks[owner] = max(旧值, 事件 watermark)`。
- **读取面**：结构化读取 `ProfileService.read`（HTTP `GET /v1/entities/{entity_id}/profile`，
  additive）：读前经信任门（state ready、指针存在、generation verified 且属本租户、
  builder 已知、新鲜度门、主体级来源回验——**每个非空字段的全部来源 Claim 仍
  visible（active/disputed、revision 匹配、无 tombstone）**，任一来源失效 ⇒ 该字段
  丢弃）；携带请求上下文（access + request scope）时每个字段还需通过与 Canonical
  读取完全相同的 scope/privacy 评估（**结构化读取面绝不放宽可见性**；无请求上下文
  的管理面读取返回全部字段及其 privacy 标签，由调用方执行自身策略——契约的字段
  形状因此携带 agent/scope 五维与 privacy_labels）。投影不可信 ⇒ **Canonical 回退**：
  同一确定性 builder 直接在请求读事务内从 Canonical Claim 现算视图，响应标注
  `source: "canonical_fallback"`（语义相同、来源相同，绝不因投影缺失而拒绝或放宽）。
  实体 Redirect 链在读取入口解析（有界深度 + 环检测），提供终态实体的 Profile。

### 3. Relation Graph（§13.3、§22.5）

- **边 Allowlist（冻结，仅三类）**：
  1. `relation` 边：Canonical Relation，status ∈ {active, disputed}；edge_type =
     relation_type；
  2. `binding` 边：Verified Binding；节点 = external_identity ↔ entity，
     edge_type = `verified_binding`，方向 identity → entity；
  3. `claim` 边（安全子集）：category = `relationship` 且 value_json 为对象并携带
     键 `target_entity_id`，其值为同租户、存在、非 tombstone、≠ subject 的 Entity id；
     edge_type = 谓词（长度 ≤ 200）。**昵称、共同出现、向量相似、模型联想永不建边**
     ——没有结构化 target 引用的 relationship Claim 不产生任何边。
- **节点**：`entity` 与 `external_identity` 两种；只有被至少一条边引用的主体才成为
  节点（孤立实体不进图）。节点行保存 kind 与构建时状态。
- **每条边保存**：resource_type/resource_id/resource_revision（Canonical ref）、
  edge_kind/edge_type、source/target 节点与 kind、agent_id 与 scope 五维、
  privacy_labels、status、valid_from/until、confidence/importance、content_hash、
  source/tombstone watermark 与 builder 元数据。UNIQUE(generation, source, target,
  edge_kind, edge_type, resource_type, resource_id)。
- **Generation 生命周期**（ADR-0015 §5 同纪律，SQLite 权威）：`graph_generations`
  （不可变；node_count、edge_count、content_checksum = 对排序后边摘要的确定性摘要、
  builder_version、source/tombstone watermark、agent_watermarks_json）+
  `graph_current`（switch_epoch fencing CAS）+ `graph_nodes`/`graph_edges` 行。
  影子重建：新 generation 构建行 → 验证（计数、来源覆盖、从行重算 checksum、
  悬空节点为零）→ 发布写事务内 Canonical 精确内容复查（可投影集与构建 survivor 集
  精确相等，否则 abort 重试）→ 插入 verified 行 → 指针 CAS（epoch fencing）→
  旧代 retired。**SQLite generation 行即权威 manifest**；checksum 字段与权威行/内容
  行交叉绑定：构建/验证/清理路径从持久化行重算并与 manifest 字段比对，不一致 ⇒
  fail closed（state 置 pending_rebuild、route 降级）。读取信任门做廉价结构校验
  （指针解析、generation verified 且属本租户、builder 已知、node/edge COUNT 与
  manifest 计数一致、水位不倒退、新鲜度门）；全量 checksum 复核由重建/验证/清理
  任务与 admin verify 执行（与 ADR-0014 FTS 同一分工：读门结构校验，验证路径内容
  校验）。
- **增量**：`graph.apply` 在当前 generation 内重派生一个 Canonical 资源（relation/
  binding/claim/entity/space_group 变更）的边：删除该资源旧边 → 从 Canonical 重算
  插入 → 重建受影响节点的在边性 → 更新计数与 generation checksum 与
  `agent_watermarks[owner]`。无当前 generation 时快速 no-op。
- **禁止项**：不从 Observation 正文、Note、Episode、向量/FTS 命中建边；不用 Graph
  作为 Task Dependency 状态源；不提供跨 Privacy 的管理旁路。

### 4. 受限 Graph Route（§18.4–18.5）

- 路由名 `graph`（ADR-0014 §3 预留拼写），加入固定路由顺序、completed/degraded
  envelope、子 Deadline（均分 1/3）、candidate cap（默认 12）、Trace 与 Usage。
- **预算（全部服务端强制）**：最大深度（默认 2）、每层扇出（默认 16）、总访问节点
  数（默认 64）、返回候选数（candidate cap）、边类型 Allowlist（edge_kind 级，
  默认全部三类）、Token Budget（Ranker 统一裁剪）与单调时钟 Deadline（每扩展一条
  边前协作检查）。预算以配置为准，测试以 ≥10× 上限的恶意高连接图证明不突破。
- **遍历语义**：从服务端解析的 speaker entity（及其实体节点）出发 BFS；**每扩展
  一条边前**对该边执行 Scope（`scope_allows` 逐维）、Privacy（`evaluate_privacy`，
  含主体授权）、Status（active/disputed）、Valid Time（as_of 或 now）、Tombstone
  （对边资源与**两端节点**查 `resource_tombstones`——墓碑最后防线，投影滞后也不可
  穿越已删除主体）与 Redirect 终态检查；两端任一不可见 ⇒ 边不可扩展——**不可见
  中间节点不能成为授权跳板**。扩展顺序确定性（边按 (edge_kind, edge_type,
  resource_id) 排序），同快照重放一致。单节点的边读取本身有界（每节点最多读
  fanout×4 条的确定性窗口，见§11 已知限制——恶意枢纽不能使一次读取载入无界行集）。
- **候选与终检**：命中边的 Canonical 资源（claim/relation）只携带 ResourceRef、
  Revision、分数与安全元数据成为候选；正文、scope、privacy 从路由读事务内的
  Canonical 行读取；返回前经**新鲜 Canonical Rehydrate**（ADR-0014 §3 屏障）重新
  检查完整权限、current revision、watermark 与 tombstone。与 claims/relations/
  profile/fts/vector 重复命中的同一 Canonical 资源由 RankerV3 按
  (resource_type, resource_id) 去重——**恰好占一个预算槽**。
- **新鲜度与 minimum_watermark**：graph/profile 新鲜度 = 请求 Agent（含无主）
  未结算 `graph.apply`/`profile.apply` backlog（连续消费前沿，ADR-0015 §4 同法；
  增量直接维护当前代，无 delta 账本）。落后超阈值（默认 10_000）⇒
  `graph_generation_stale`/`profile_generation_stale` 降级。`minimum_watermark` W：
  generation 的 `agent_watermarks[agent] < W` 或 lag > 0 ⇒ 降级（保守方向：
  纯非投影流量推进水印后需重建才恢复 min-watermark 服务——与 Vector 同族的已知
  保守性，见§10）。
- **降级**：Graph 失败/不可信 ⇒ 路由降级（原因码见§6），claims/relations 等
  Canonical 路由结果原样保留（`route_skipped_canonical_intact`）——**投影不可用绝不
  扩大结果**。`as_of` 请求 ⇒ `graph_as_of_unsupported`（投影只索引当前态，不用
  当前图冒充历史）。

### 5. Profile Route

- 路由名 `profile`（ADR-0014 §3 预留拼写）。经同一信任门读取 speaker（及请求
  actors）的 Entity Profile 投影；**候选即字段来源 Claim 的当前 Canonical 行**
  （resource_type=claim，文本/权限来自 Canonical，profile 只决定选择与排序信号），
  因此天然通过既有 Claim Rehydrate，且经 RankerV3 与 claims/fts/vector 命中去重。
  投影不可信 ⇒ 降级为 `profile_*` 原因码，claims 路由继续提供 Canonical 结果。
  字段级视图（含冲突、来源、freshness）由 §2 的结构化读取面提供，不经 Recall
  candidate 复制正文。

### 6. 降级原因码（冻结）

`graph_rebuild_pending`（retryable=true）、`graph_builder_unknown`（false）、
`graph_generation_stale`（true）、`graph_index_corrupt`（true）、
`graph_as_of_unsupported`（false）；`profile_rebuild_pending`（true）、
`profile_builder_unknown`（false）、`profile_generation_stale`（true）、
`profile_index_corrupt`（true）、`profile_as_of_unsupported`（false）。
不复用 fts_*/vector_* 语义。`route_failed`/`route_deadline_exceeded` 语义不变。

### 7. 失效、Outbox 与 Jobs（§16、§19）

- **新 job kinds**（payload version 1）：`graph.apply`（priority 5，coalesce=GRAPH，
  per-resource）、`graph.rebuild`（3，catch_up=latest）、`graph.cleanup`（8）、
  `profile.apply`（5，coalesce=PROFILE，per-resource）、`profile.rebuild`（3）、
  `profile.cleanup`（8）。全部真实幂等 handler；Phase 2 注册的占位
  `profile.refresh`/`graph.refresh` 维持 disabled（从未启用、无生产者，保留注册
  以免改变 Phase 2 语义）。旧 Worker 不领取未知 kind（既有 claim 过滤）；新
  Worker 接受 payload version ≤ 1。
- **事件接线**：`claim.changed`/`relation.changed`/`episode.changed`（仅 claim 与
  relation 影响 graph/profile；episode 不参与）与 `memory.invalidated` handler 在
 同一 fenced 事务内追加 `graph.apply`/`profile.apply`（refs-only payload、
  per-resource coalesce、dedupe 携带 watermark——与 fts.apply/vector.apply 同法）。
  **Binding confirm/revoke、Entity Redirect、Entity Tombstone、SpaceGroup
  bind/unbind 在各自 Canonical 事务内原子写入失效 Outbox**（直接 enqueue
  `graph.apply`/`profile.apply`，payload 携带受影响资源引用——这些操作此前没有任何
  outbox 事件，本阶段补齐失效边界）。
- **长工作纪律**：rebuild 的构建阶段在事务外（SQLite 行投影无 Provider 调用，但
  遵守同一纪律：影子构建 → 发布事务只做复查 + CAS）；物理清理（旧代行删除）在
  fenced 事务内、目录级副作用不适用于 SQLite 行投影（无文件），retired 旧行由
  `*.cleanup` 在回退窗口后删除。过期 Worker、旧 source revision、陈旧 pointer
  epoch 的发布均被 CAS/fencing 拒绝（既有四重 CAS）。
- **Cache**：本仓库当前**不存在** Recall/投影结果缓存（ADR-0014 §6 冻结了未来
  缓存键语义；本阶段不引入）。失效边界 = 上述 Canonical 事务内的 Outbox 事件；
  "Binding/Redirect/SpaceGroup/Correct/Forget 后旧投影和缓存结果不能复活"由
  （a）增量 apply 的墓碑/状态回验、（b）graph 路由逐边墓碑检查、（c）新鲜
  Rehydrate 屏障、（d）recall 请求级响应擦除（ADR-0014 §14-7，既有）共同保证。

### 8. Schema 9（migration 0009）

新增 STRICT 表：`profile_projection_state`（全局单行）、`profile_generations`、
`profile_current`（per-tenant 指针 + switch_epoch）、`profile_subjects`、
`profile_fields`、`graph_projection_state`、`graph_generations`、`graph_current`、
`graph_nodes`、`graph_edges`——共 10 张。CHECK/UNIQUE/FK 表达：状态枚举、
subject_kind/edge_kind/node_kind 枚举、scope 维一致性（session ⇒ space）、
per-generation 资源唯一、计数非负、switch_epoch ≥ 1、generation status 枚举。
无 Down Migration（旧行/历史不经降级脚本删除）。

### 9. Backup/Restore（§21）

- Backup 不需要携带额外文件（投影全在 SQLite；整库快照自然含投影行）。
- Restore staging：清空 `profile_generations/profile_current/profile_subjects/
  profile_fields` 与 `graph_generations/graph_current/graph_nodes/graph_edges`，
  两 `*_projection_state` 置 `pending_rebuild`——**不可信投影绝不作为已恢复状态**；
  恢复后首次 graph/profile 读取以 `*_rebuild_pending` 降级，admin/worker rebuild
  后恢复 ready。快照早于 Schema 9 时表不存在，由启动迁移自然创建（never_built）。
- Restore 不变量新增：profile/graph 指针 → 同租户 verified generation；ready ⇒
  指针存在；字段/边行租户锚定且 generation 归属一致。

### 10. 健康检查、指标与契约

- `/health/ready` 新增 `checks.profile_projection_state` 与 `checks.graph_projection_state`
  （additive）：`never_built` 不降级（全新安装正常态）；`pending_rebuild`/`unavailable`
  按可选能力降级（reason `profile_projection_rebuild_pending`/`graph_projection_
  rebuild_pending`）；配置 `profile_required`/`graph_required=true` 时升级 not_ready。
  Graph/Profile 无外部 Provider 依赖（纯 SQLite），无需能力探针。
- 指标：复用 `iris_index_generation{index_kind}` 与 `iris_index_lag_revisions{index_kind}`
  （index_kind ∈ {graph, profile}），发布提交后（admin 路径 + rebuild after_commit
  钩子）与每次可信读取的信任门处发射——全部真实发射点、低基数。
- 契约 1.7.0（additive）：capabilities +`recall.graph.v1`、`profile.v1`；路由枚举
  +`graph`、`profile`（`forward/recall-response-future-routes.json` 中预用的 `graph`
  路由名转正为合法枚举值）；降级原因 +§6 十码；OpenAPI 新增
  `GET /v1/entities/{entity_id}/profile`（结构化 Profile 读取面，含 generation/
  watermark/builder 元数据与 `source` 回退标注）；fixtures 新增 graph/profile
  completed/degraded/forward 组；mock server capabilities 对齐 Schema 9 全集。
- SDK：Python/TypeScript 0.9.0 版本同步；`completed_routes`/`degraded_routes.route`
  保持自由字符串 + 枚举（SDK 对未知路由不失败——既有前向兼容语义不变）。

### 11. 实现期对抗性复审补充（Review Hardening, 2026-09-03）

三轮对抗性复审（自查 + 独立复审 ×2）冻结以下补充语义，全部已落回归：

1. **主体墓碑在 Profile 路由的立即生效**：ProfileRoute 在 collect 入口检查
   speaker 主体实体的 `resource_tombstones`——实体被 tombstone 后，其 profile
   字段在**下一次请求**即不可服务，不等待 ownerless `profile.apply` 结算（dead
   job 也不能把窗口变成永久）。GraphRoute 的逐端点墓碑检查同法（复审 P2）。
2. **实体 tombstone 的增量失效覆盖 relationship 主体**：`profile.apply(entity)`
   除了实体主体外，同时重派生所有配对该实体的 relationship 主体（"die with it"
   不变量对全部主体种类成立）。
3. **Profile 重建的翻转前再绑定**：与 Graph 重建同纪律——指针翻转前从持久化行
   重算 manifest（counts + checksum）并要求与构建值相等，不等 ⇒ 整个事务回滚。
4. **Binding 边派生的墓碑检查**：`graph.apply(binding)` 的重派生与 verify 口径
   一致——binding 本身或其 external identity 带墓碑 ⇒ 不产边（verify 不会对
   rebuild 会重建的边反复报 corrupt）。
5. **读面字段元数据**：契约的 entity-profile-response 字段形状携带
   agent_id/space 五维/privacy_labels——调用方（HTTP 层）能执行与 Core 相同的
   过滤，投影不复制不可见内容。
6. **有界边读取**：GraphRoute 的每节点边读取带确定性 LIMIT（fanout×4）窗口。
7. **重建清账（drain-by-construction）**：全量重建的发布事务内 settle 该投影
   kind 的全部未租用（pending/retryable）apply 任务（发布快照按构造覆盖其全部
   已提交变更）；已租用任务留给其 worker，四重 CAS 完成语义不变。若 unsettle
   计数只在超过阈值或 minimum_watermark 场景阻塞读取，dead job（不属 unsettled
   集合）不会伪造新鲜——1 的墓碑检查与新鲜 Rehydrate 兜底其余窗口。
8. **端点实体自身隐私标签参与逐边授权**（第三轮 P1）：`_edge_visible` 除边自身
   标签外，同时评估两端实体（Entity.privacy_labels，按租户级数据域）——公开边
   既不能暴露 restricted 端点，也不能把它当遍历跳板；实体查询按 collect 级缓存
   （受节点/边预算约束）。
9. **Canonical 现势性先于前沿扩展**（第三轮 P1）：BFS 在把目标节点加入 frontier
   之前先做 `resource_still_admissible`（存在/租户/可见状态/当前修订相等/无
   墓碑）——correct/revoke/隐私变更后尚未 apply 的旧边既不产候选，也**不再扩展**
   （旧实现仅在候选构建时终检，旧边仍可通向下一跳）。失效边不再占用 fanout。
10. **verify 判定以返回值持久化，不以异常逃逸**（第三轮 P1）：verify_in_tx 失败
    时写入 `pending_rebuild` 并**返回 False**，cleanup 据此跳过删除；异常逃逸会
    连同状态写入一起被 worker 事务回滚，使损坏代保持 ready——生产语义以提交后
    状态为准（回归断言移出事务）。
11. **重建清账按 payload 版本界定**（第三轮 P1）：settle 仅覆盖
    `payload_version ≤ 本构建理解版本`（graph/profile 各自常量，镜像
    JOB_PAYLOAD_VERSION）的 apply 任务；未来版本语义不可被旧构建证明覆盖，留队
    等待理解它的构建。
12. **relationship 主体的配对准入统一**（第三轮 P2）：`_subject_claims` 增量/回退
    派生只收集结构化 target 等于**配对另一端**的 relationship Claim——与
    `derive_all_subjects`（重建枚举）同规则；第三方关系（Bob→Dave）不再渗入
    Bob|Carol 主体，投影与回退派生 checksum 一致。
13. **verify 的静默管线全量等价比对**（第三轮 P2）：结构校验（counts/manifest
    checksum/主体 checksum）恒跑；当整条投影管线静默（apply 及其生产者事件
    ——`claim.changed`/`relation.changed`/`memory.invalidated`——均无 unsettled）
    时，再执行与新鲜 Canonical 派生的**精确等价**比对（来源 Claim 存在/状态/
    当前修订/墓碑、主体不缺不多、checksum 相等）——管线有在途工作时投影被允许
    暂时落后（避免把正常时滞错报为损坏）。

### 12. 与复审裁定的已知边界（记录不修）

- **Binding 边在 speaker 锚定的正向遍历中不可达**（identity→entity 方向，BFS 从
  实体节点出发）：它们仍被投影/验证/计数，参与图完整性；正向遍历到达不了它们
  是方向性结果，不是遗漏。反向遍历留待有真实需求时另议。
- **边读取窗口的确定性饥饿**：枢纽节点前 fanout×4 条边全部不可见时，窗口外的
  可见边被确定性跳过（availability 取向，非泄漏）。
- **`claims_targeting_entity` 的 LIKE 预过滤**只匹配 canonical JSON 的两种空白
  形态；非规范 JSON（不存在于本仓库写入路径）会被跳过——fail-closed 方向。
- **节点预算耗尽时同边不产候选**（确定性 completeness quirk）。

## 否决的替代方案

- **Profile 字段从 Episode/Note/Persona 混合来源汇总**：违反"Profile 是 Claim 的带
  来源汇总"（§13.5）与 Agent Persona 隔离要求；Episode 摘要留在 episode 资源自身。
- **relationship 类 Claim 用文本/向量匹配解析目标实体建边**：昵称/相似度建边被
  §13.3 明确禁止；只接受 value_json 中的结构化 `target_entity_id` 引用。
- **图遍历不做逐边墓碑检查（信任 generation 水位）**：墓碑优先级最高（ADR-0005）；
  投影滞后窗口内已删除主体必须立即不可穿越，逐边查 `resource_tombstones` 是最后
  防线（与 FTS 查询的 NOT EXISTS 同法）。
- **Profile candidate 直接携带字段合成正文（新 resource_type）**：新资源类型无
  Canonical 行可回 Rehydrate，违反 §18.5；改为携带字段来源 Claim（claim 资源有
  既有回验路径），字段视图由结构化读取面提供。
- **graph/profile 各自的 delta 账本（vector 式）**：图/档投影是 SQLite 行，增量可
  原子维护当前代（FTS 式），freshness 由未结算 apply backlog 表达；额外 delta
  账本是重复簿记（ADR-0015 §4 的纪律以 backlog 形式保留）。
- **读取门全量重算 checksum**：O(边数) 每请求不可接受；内容校验归构建/验证/清理
  路径（与 FTS 分工一致），读门做结构校验（计数/指针/builder/水位/新鲜度）。
- **min-watermark 用 watermark entries 反查"区间内无投影相关变更"来放宽**：正确但
  引入新查询面与簿记；采用与 Vector 相同的保守降级并记入已知限制。
- **引入 Recall/投影结果缓存以字面满足"缓存失效"验收**：仓库无缓存；
  ADR-0014 §6 已冻结未来键语义——不制造弱键缓存。

## 后果

- Schema 9 新增 10 张 STRICT 表；0001–0008 字节锁定；窗口 [7,8] → [8,9]。
- Recall 编排器新增 `graph`/`profile` 路由（默认仅当注入对应 ProjectionService 时
  参与）；Phase 6/7 envelope/预算/Usage 语义零变化；Ranker 继续为 v3（无新语义，
  图/档候选经既有 (type,id) 去重）。
- IdentityService/ProvisioningService 的 Binding/Redirect/Tombstone/SpaceGroup 写
  路径新增失效 Outbox 事件（additive，不影响既有返回值与幂等语义）。
- Restore 演练扩展 profile/graph 重置断言。

## 迁移影响

- `migrations/0009_phase8_profile_graph.sql`（online_safe=true, lock_ms=200,
  min_app=0.9.0, recovery=none）；0001–0008 与 `869f04d` 逐字节一致（测试锁定）。
- 无 Down Migration。回退顺序：停用 `graph.*`/`profile.*` handler 与 graph/profile
  路由 → **保持 0.9.0 二进制运行 Schema 9 库**（0.9.0 的窗口是 [8,9]；0.8.0 的窗口是
  [7,8]，**不能**运行 Schema 9 库，Ready 会以 `schema_incompatible` 拒绝）→ 若必须
  退回 0.8.0 二进制，只能按 ADR-0013 §10 从 Schema 8 备份隔离恢复（投影由重新升级到
  0.9.0 后重建）。（初版本条把"0.8.0 运行 Schema 9"与"运行 Schema 9 需 0.9.0"并列，
  自相矛盾；以 `storage/runtime.py` 的 `SUPPORTED_SCHEMA_MIN/MAX` 为准更正。）
