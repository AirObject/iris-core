# ADR-0015: Phase 7 — Embedding Provider、FAISS Generation 生命周期与 Vector Route

- 状态：Accepted
- 日期：2026-09-03
- 影响阶段：Phase 7（Phase 8/10 依赖本决策的 Generation/降级/Rehydrate 模式）
- 基线：§18、§20.6–20.7、§21、§22.2–22.4、§23、§24、§30–33、§36 阶段 7；ADR-0001/0005/0011/0013/0014
- 前置：ADR-0014（Recall Envelope、三段式编排、新鲜 Rehydrate、Usage——Vector 全部沿用，不改语义）

## 背景

Phase 7 在 Phase 6 的 Recall 协议上增加可降级的向量语义召回：Embedding Provider Port、
UUID↔signed int64 surrogate ID 映射、不可变 FAISS Generation（文件系统）+ Current Pointer
（SQLite）、Copy-on-write Handle Swap 与 Hybrid 排序。实现中不可逆或跨阶段的语义选择冻结如下。

## 决策

### 1. 版本与 Release Train

- Core / Python SDK / TypeScript SDK **0.8.0**；Schema **8**（migration
  `0008_phase7_vector_recall.sql`，min_app=0.8.0，online_safe=true）；Contract **1.6.0**
  （纯 additive：capability `recall.vector.v1`、`embedding.v1`；路由枚举 +`vector`；降级原因码
  +`vector_*` 6 个；错误码 +`provider_unavailable`——该码在基线 §23.4 冻结清单内，本阶段首次启用）。
- Runtime 兼容窗口 **[7, 8]**：0.8.0 在线升级 Schema 7 库；Schema ≤6 需先经 0.7.0 二进制。
- 0001–0007 字节不变（回归锁定扩展到以 `179b6a0` 为基线的 0007）。

### 2. Embedding Provider（§22.2、§24.2）

- **Port** 在 application（`EmbeddingProvider` Protocol：`embed_batch(texts)` + 能力声明）；
  Domain 不导入 Provider SDK/FAISS/NumPy。生产实现 `HttpEmbeddingProvider`（urllib、
  OpenAI 兼容 `/v1/embeddings`、可注入 transport）+ 共享校验包装；测试使用确定性
  `DeterministicEmbeddingProvider`（文本哈希种子），仅用于测试，不进生产路径。
- **配置**：model、dimension、metric（固定 `cosine`）、normalization（固定 `l2`）、
  batch_size、timeout_us、rate limit（令牌桶 qps）、circuit breaker（连续失败阈值 + 冷却 +
  半开探针）、max_input_chars、capability 声明。维度/模型/归一化/模板构成**向量空间身份**。
- **启动 Probe**：固定探针输入 → 验证维度、L2 范数 ≈ 1（容差 1e-3）、超长输入拒绝。
  Probe 以 `ValidatingEmbeddingProvider.probe()` 暴露，由 `VectorProjectionService`
  惰性调用并缓存（成功缓存；失败每 breaker 冷却至多重试一次，绝不随 readiness
  轰炸 Provider）。Probe 失败或熔断开启 ⇒ `capability_available()` 为 false，
  `vector_required` 部署的 readiness 因此 not_ready（§11），其余功能不受影响。
- **Deadline 下传**：`embed_batch(texts, deadline_monotonic_us=…)`——每个 transport
  调用超时 = min(配置 timeout, 剩余预算)，剩余 ≤ 0 时 fail-fast；HTTP 侧是 socket
  级超时（urlopen timeout），被放弃的调用不会以搁浅线程形式苟活。
- **半开单探针**：冷却期满后 half-open 只放行**一个** in-flight 探针（预留位由
  `record()` 释放，校验失败路径也经 `record` 释放且不计入熔断），并发调用一律
  `circuit_open` 快速失败——恢复中的 Provider 不被积压流量踩踏。
- **输出校验**（fail-closed）：空文本列表、返回数不匹配、维度错误、NaN/Inf、非数值、
  未归一化（范数偏差 > 1e-3）⇒ 抛 `EmbeddingProviderError`，绝不写入投影。
- **输入最小化**：资源类型版本化模板（`VECTOR_TEXT_TEMPLATE_VERSION=1`，与 FTS 模板同形但
  独立版本空间）：claim = `predicate: canonical_text`；episode = `title\nsummary`；
  note = `title\nbody`；查询输入 = topic 归一化截断。超长输入在 Provider 侧截断到
  max_input_chars。
- **日志/指标/Trace**：只记录文本 sha256 前 16 位、长度、模型、批次大小、耗时与结果码。
  结构化日志走既有 `log_secure` 白名单；canary 扫描覆盖 Provider 路径。
- **隔离性**：Provider 超时/熔断/限流只使 Vector Route 降级，不阻塞 Observe/Correct/
  Forget/Task/结构化/FTS。Provider 调用绝不发生在写事务内（rebuild 的 embed 阶段在事务外；
  查询嵌入发生在路由自身的读快照内但不持有 Writer Gate——WAL 读不阻塞写）。
- **持久化前重验**：rebuild 在阶段 4（读事务）重新枚举 Canonical 并与收集快照比对，
  Revision 已推进或已 Tombstone 的资源从本代剔除（其变更事件的 ``vector.apply`` 负责
  delta 跟进）；阶段 6 发布事务再次全量枚举并要求与 survivor 集**精确相等**（对非索引
  流量与 dead-letter 丢失免疫），不等则 abort 重试。发布清空全部 delta（精确等价即证明
  已并入），同步墓碑失效到 id map，并为每个 survivor 行盖上**成员戳**
  （`incorporated_generation`，与指针 CAS 同事务）；此后迟到的 apply 仅当
  「行 revision == 观测 revision **且** 成员戳 == 当前指针代」才跳过 delta 写入——
  未发布的构建（阶段 2 已刷新 id map 但发布失败/被 fencing 拒绝）不可能伪造新鲜。

### 3. Surrogate ID 与 ID Map（P7-IDMAP-01）

- **分配**：服务端单调分配的合法 signed int64（区间 [1, 2^62]，per-tenant 计数器存于
  `vector_projection_state.last_surrogate_id`，同事务 CAS 递增）。**禁止** UUID 截断/哈希成
  FAISS ID。UNIQUE(tenant, surrogate) + UNIQUE(tenant, resource_type, resource_id) 双唯一约束；
  服务层校验 int64 边界与跨租户隔离。
- **不可错误复用**：映射行 status ∈ {active, invalid}；Correct/Forget/Tombstone/Revision 变化
  由 `vector.apply` 在 fenced 事务内即时置 invalid（或推进 revision 重激活），旧 Revision 在
  逻辑上立刻不可服务；物理清理由 `vector.cleanup` 完成。surrogate 一经分配永不重分配给其他
  资源（计数器只增）。Restore 保留 ID Map 行（surrogate 稳定性），但清空 Generation 元数据。
- **成员戳（复审 2 P1）**：行携带 `incorporated_generation`（可空 TEXT，非外键——退休代
  可先于行被清理，悬空戳永不匹配指针、方向安全）。只有发布事务能盖章；任何 upsert
  （含构建阶段 2/4 的刷新与 apply 的重写）都清空戳。「已并入当前代」的持久化证明 =
  revision 相等 ∧ 戳 == 指针代，二者与指针 CAS 原子提交。
- **快照**：每代 `id-map.snapshot`（确定性文本，按 surrogate 数值排序）；加载时与索引
  ID 集、DB id_map 交叉校验，任何不一致 ⇒ 该代不可加载。

### 4. Delta Ledger 与新鲜度口径

- 增量变化**只**进入 `vector_delta_ledger`（由 `vector.apply` 在 fenced 事务内写入/替换
  per-resource 期望状态），或触发新 Generation（`vector.rebuild`）；**绝不**修改当前查询
  Handle（FAISS Handle 永远只读）。collect 与 revalidate 之间新建的资源在阶段 4 补分配
  surrogate 并纳入本代（防持续写入下的重建饥饿）；发布后的迟到 apply 以**成员戳**判定
  "已并入"（§3）并跳过 delta——未发布构建的 id map 刷新不可能被误判为已并入。
- **新鲜度 = 未结算 `vector.apply` backlog（请求 Agent）+ 未并入 delta 行数（请求 Agent）**。
  与 ADR-0014 §14.1 同一"连续消费前沿"纪律：可索引变更在变更事务内 exactly-once 排程
  coalesced apply；apply 完成即入 delta；rebuild 在发布事务内清空全部 delta（发布快照按
  构造覆盖一切已提交状态）。非索引流量不产生 delta ⇒ 无永久 false stale；单条乱序 apply
  不减少未结算计数 ⇒ 无 false fresh。
- `minimum_watermark` 语义（§20.6）：generation 记录 per-agent 构建水印
  （`agent_watermarks_json`）；请求带 `minimum_watermark` 且（Agent 水印 < W 或 lag > 0）⇒
  Vector Route 以 `vector_generation_stale` 降级——不允许用旧代冒充 read-your-writes。
- 落后阈值（无 minimum_watermark 时）默认 10_000 条未并入变化（可配），超过 ⇒ `vector_generation_stale`。

### 5. FAISS Generation 生命周期（P7-GEN-01、§22.3–22.4）

- **目录**（§35.2，由 db 路径派生 `<db>/vector/`）：`generations/<id>/{manifest.json,
  index.faiss, id-map.snapshot, checksums.txt}` + `tmp/` 暂存。SQLite `vector_current`
  是唯一权威指针；§35.2 的 `current.json` 诊断镜像本阶段不实现（不承诺、不读取），
  孤儿/退休代目录由全局保留集 + 回退窗口清扫（tmp/ 同窗清理）。
- **Manifest 冻结字段**：generation_id、schema_version、model、dimension、metric、
  normalization、template_version、builder_version、source_watermark、tombstone_watermark、
  vector_count、content_hash、id_map_hash、index_hash、agent_watermarks（JSON）、created_at。
  checksums.txt = 三个文件的 sha256；content_hash = 排序后 (surrogate, type, id, revision,
  content_hash) 的确定性摘要（与 Canonical content hash 绑定）。
- **构建六阶段**：(1) 一致 Canonical 快照收集（读事务）；(2) surrogate 分配/刷新（短写事务，
  无 Provider 调用；upsert 清空成员戳）；(3) 事务外批量 Embedding + FAISS 构建
  （`IndexIDMap2(IndexFlatIP)`，L2 归一化向量 + 内积 = cosine；SWIG 所有权要求持有内层
  索引与原始读取结果的 Python 引用）；(4) Flush 后重读验证：文件摘要、manifest 自洽、
  ID 集 == snapshot 集、vector_count、维度、抽样自检索（构建向量 top-1 命中自身）；
  (5) 原子重命名目录（tmp → generations/<id>，逐文件 + 目录 fsync）；(6) 发布写事务：
  重新全量枚举要求与 survivor 集**精确相等** → 插入 verified generation 行 → pointer CAS
  （switch_epoch fencing）→ 旧代 retired → 清空 delta → 墓碑失效同步 → survivor 成员戳。
  任一阶段失败：tmp 目录清理，指针不动，上一可信代继续服务。
- **验证信任链（复审 2 P1）**：manifest 是文件摘要的权威（`index.faiss`/`id-map.snapshot`
  的 sha256 与 manifest 自身的 `index_hash`/`id_map_hash` 重算比对——`checksums.txt`
  可被整体重写，只作纵深不作依据）；加载路径（`handle_for(pointer, generation=DB 行)`）
  再把 manifest 绑定到 **SQLite 权威行**：三个 checksum、vector_count、空间身份精确
  相等，source/tombstone/per-agent 水位单调不回退。畸形字段（错误类型、缺失、坏 JSON、
  faiss 解析失败）一律折叠为稳定 `vector_index_corrupt`，绝不泄漏原始
  ValueError/KeyError。
- **Handle 生命周期**：`VectorIndexHandle` 不可变（FAISS 对象只读）；`VectorIndexManager`
  持当前 Handle 引用。查询路径在路由读事务内读 pointer；pointer 代 ≠ 已加载代 ⇒ 先加载
  （完整验证：文件摘要 + manifest 自洽 + **SQLite 行绑定** + 配置/数量/抽样自检索，
  冷加载单飞防惊群）再 COW 换引用。旧 Handle 引用计数归零且超过回退窗口（默认 24h，
  可配）后目录才可被清理。加载失败 ⇒ 保留旧 Handle；无旧 Handle ⇒ Vector Route 禁用
  （`vector_index_corrupt`/`vector_space_mismatch`）。
- **重启**：只加载"pointer 指向 + status=verified + checksum 正确 + 配置匹配"的代；
  tmp/孤儿/截断/坏 checksum/未知 builder/维度不符/ID Map 不一致/加载异常一律不得替换当前
  可信 Handle。指针悬空（文件缺失）⇒ 启动自愈：projection_state 置 `pending_rebuild`，
  Vector Route 降级 `vector_rebuild_pending`，不阻塞其他功能。
- **跨进程**：SQLite pointer + generation_id + switch_epoch fencing 为唯一权威；进程内锁只做
  本地 Handle 一致性，不是正确性机制。
- **空间隔离**：model/dimension/metric/normalization/template/builder 任一不同 ⇒ 不同向量
  空间，永不共用 Generation 或 delta；运行时配置与 manifest 不匹配 ⇒ `vector_space_mismatch`
  降级并需重建（模型切换 = 双 Generation 原子切换）。

### 6. Vector Route 与 Hybrid 排序

- 路由名 `vector`（基线示例拼写，ADR-0014 §3 预留）；加入 DEFAULT_ROUTES、completed/
  degraded_routes、子 Deadline（均分 1/3 总预算，与其他路由同法）、candidate cap（默认 20）、
  Trace 与 Usage 四阶段。
- 查询嵌入受总 Deadline 与路由子 Deadline 双重约束（Provider 超时取 min(配置, 剩余预算)）；
  FAISS search 前后协作式 deadline 检查；阻塞 Provider 不得拖住已完成路由（Phase 6 有界
  future.result 语义天然覆盖）。
- Candidate 只携带 ResourceRef + Revision + 分数 + 安全元数据；正文、scope、privacy 从
  路由事务内的 Canonical 行读取；最终 Rehydrate（新鲜事务）重查 Tenant/Agent/SpaceGroup/
  Space/Session/Privacy/Status/ValidTime/Expiry/Tombstone/Supersede/content-hash/水位。
  `as_of` 请求 ⇒ `vector_as_of_unsupported` 稳定降级（无历史向量代，不用当前代冒充）。
- **Hybrid Ranker v3**（`RECALL_RANKER_V3=3`）：v2 融合规则不变（缺失分量 ≠ 0、确定性
  冲突/冗余标记、保护性预算），v3 增加 `vector` 类别优先级（7，最后）、冻结三路由混合
  tie-breaker，并**按 Canonical 资源去重**：同一 `(resource_type, resource_id)` 经
  claims/fts/vector 多路由命中时，只保留稳定序最优的一个实例（v2 标记先行，胜者携带
  冲突/冗余判定；**不同资源**的冲突组永不被合并），重复项在预算分配**之前**丢弃——
  多路由资源恰好占一个预算槽（"nothing is returned twice" 是 v3 的真语义，不是 v2 的
  冗余降分）。相同输入/快照/版本完全可重放。
- **Overfetch 扩张（复审 2 P2）**：索引是租户级的，无法预过滤 Agent/Revision；固定
  4× 预取会被其他 Agent 的高分向量挤占造成假阴性。search 以 4×limit 起步，结果不足
  时按 4× 逐轮扩张直至足够或耗尽索引（精确 FlatIP 搜索，扩张只追加低分命中，最终结果
  与全量扫描的 top-limit 一致、确定性不变）。
- Vector 不可用时 Structured/FTS 结果原样保留，`degraded_routes` 报告稳定原因码、retryable
  与 fallback（`route_skipped_canonical_intact`）。不新增独立 Vector Search HTTP 端点。

### 7. 降级原因码（冻结）

`vector_rebuild_pending`（retryable=true）、`vector_builder_unknown`（false）、
`vector_generation_stale`（true）、`vector_index_corrupt`（true）、
`vector_unavailable`（true——Provider 瞬时故障/熔断可重试；faiss 缺失虽属部署
错误，但 retryable=true 只影响宿主提示，无正确性影响）、
`vector_as_of_unsupported`（false）、`vector_space_mismatch`（true，需重建）。

### 8. Jobs 与 Outbox

- 新 kind：`vector.apply`（priority 5，coalesce=VECTOR，per-resource）、`vector.rebuild`
  （priority 3，catch_up=latest）、`vector.cleanup`（priority 8，catch_up=latest）。全部启用
  真实幂等 handler。`CoalesceClass` 新增 `VECTOR`。freshness gate 同时计入无主
  （ownerless）未结算 apply 任务（fail-stale）。
- `claim/episode/note.changed` 与 `memory.invalidated` handler 在同一 fenced 事务内追加
  排程 `vector.apply`（refs-only payload、per-resource coalesce、dedupe 携带 watermark——
  与 fts.apply 同法）。`vector.apply` 完成即写 delta 行；无 Generation 时快速 no-op 但仍
  维护 id_map（首次构建前即有映射）。
- `vector.rebuild` handler 的 work() 阶段执行收集/分配/嵌入/验证（事务外），commit() 阶段
  执行发布事务；崩溃重试幂等（从 Canonical 重建，orphan 由 cleanup 收敛）。
- **Post-commit 钩子（复审 2 P2）**：`JobCommit` 闭包可选携带 `after_commit` 属性（零参
  callable），worker 仅在「业务写 + completion CAS 均已提交」后调用——SQLite 会回滚行，
  但不会恢复已 unlink 的目录，故文件删除绝不在 fenced 事务内发生。`vector.cleanup`
  在事务内删行、钩子内删目录（cleanup 返回的代目录直删；孤儿/tmp 由全局保留集 + 年龄
  窗口清扫，本进程已加载代永不清）；钩子失败只记指标不复活任务（孤儿自愈）。失败回滚
  ⇒ 行与目录俱在。`vector.rebuild` 用同一钩子在发布提交后发射 generation 指标。

### 9. Schema 8（migration 0008）

新增 STRICT 表：`vector_projection_state`（全局单行 + surrogate 计数器）、
`vector_generations`（不可变，status ∈ {verified, retired}）、`vector_current`（per-tenant
指针 + switch_epoch fencing）、`vector_id_map`（双唯一约束 + `incorporated_generation`
成员戳，§3）、`vector_delta_ledger`。
FAISS 文件**不在** SQLite 内、**不在** migration 内——运行时探测 faiss 模块不可用时
Vector Capability fail-closed 降级（与 ADR-0014 §1 FTS5 探测同法）。

### 10. Backup/Restore（§21）

- Backup 不携带 `vector/` 目录（可重建投影）。Restore staging：清空 `vector_generations`/
  `vector_current`/`vector_delta_ledger`，保留 `vector_id_map`（surrogate 稳定），
  `vector_projection_state` 置 `pending_rebuild`（含 surrogate 计数器保留）。恢复后首次
  Vector 请求 `vector_rebuild_pending` 降级；admin rebuild 后恢复 ready。
- 若未来备份选择携带 vector 目录：必须校验 manifest/checksum/配置/水位后才能采用
  （本阶段不实现携带路径，代码路径结构性拒绝）。
- Restore 不变量新增：vector_current → 同租户 verified generation；ready ⇒ 指针存在；
  delta 行租户锚定；id_map surrogate 租户内唯一（DB 约束 + 校验）。

### 11. 健康检查与指标

- `/health/live` 不依赖 Vector。`/health/ready` 新增 `checks.vector_projection_state`
  （additive）：`never_built` 不降级（全新安装正常态）；`pending_rebuild`/`unavailable`
  按"可选能力降级"标记 degraded（reason `vector_projection_rebuild_pending`）；当配置声明
  `vector_required=true` 时升级为 not_ready（fatal reason `vector_projection_required`）。
  响应不泄漏路径或 ID。
- **能力探针接线（复审 2 P2）**：`HealthService` 接受可选 `vector_capability` callable
  （部署把 `VectorProjectionService.capability_available` 接入）——投影状态只说明
  数据库欠账，`vector_required` 的就绪还必须证明运行时真能服务（faiss 可导入 ∧
  Provider probe 通过 ∧ 熔断未开）。capability 不可用 + required ⇒ not_ready
  （reason `vector_capability_unavailable`）；可选部署仅报告 `checks.vector_capability`。
- 指标（§31.2 建议集，本阶段进入冻结面）：`iris_index_generation{index_kind}`、
  `iris_index_lag_revisions{index_kind}`、`iris_provider_requests_total{provider_kind,outcome}`、
  `iris_provider_duration_seconds{provider_kind}`——全部低基数，且**都有真实发射点**：
  generation 指标由发布提交后（admin 路径与 rebuild 钩子）按指针 epoch 发射；lag 在
  每次可信搜索的信任门处发射；provider 请求/时长由校验包装在每次调用（含失败）发射。
  注册表实例由部署注入（与 Phase 2 指标同法）。

### 12. 契约与 SDK

- contracts source：capability +`recall.vector.v1`、`embedding.v1`；路由枚举 +`vector`；
  降级原因 +§7 七码；错误码 +`provider_unavailable`；版本 1.6.0/0.8.0/Schema 8。
  `completed_routes` 本就是自由字符串数组（forward fixture 已含 vector），未知路由名不拒绝。
- fixture：valid（vector 完成/降级 envelope）、forward（未来路由）、invalid（类型错误）+
  manifest 登记；Python/TypeScript SDK 0.8.0 wire/fixture 测试同步；mock server capabilities
  对齐（含既有 Phase 6 缺口一并修正为 Schema 8 全集）。

## 否决的替代方案

- UUID 哈希/截断为 FAISS ID：碰撞不可证明为零，违反 P7-IDMAP-01。
- 在当前 Handle 上原地 add/remove（FAISS 增量写）：违反 §22.4 只读纪律与"半成品不可见"。
- Generation 文件写 SQLite BLOB：大索引撑爆 WAL/备份，且 §35.2 要求文件系统布局。
- 单写事务内完成整个 rebuild（FTS 模式）：Provider 调用被禁止出现在长写事务内（§20.2），
  故拆六阶段并在 publish 重验。
- per-agent applied watermark 簿记新鲜度：ADR-0014 §14.1 已证明两方向皆错；改为
  backlog + delta 计数。
- 备份携带 FAISS 文件直接采用：陈旧索引不可信任；先标记重建（本阶段实现不携带路径）。
- 新增独立 `/v1/vector-search` 端点：无架构依据；Vector 是 Recall 路由不是独立查询面。

## 后果

- Schema 8 五张新表；0001–0007 字节锁定；窗口 [6,7] → [7,8]。
- 依赖新增 `faiss-cpu`、`numpy`（runtime 依赖；faiss 导入仍惰性探测，缺失 ⇒ Vector
  Capability 降级而非崩溃）。
- Recall 编排器新增 `vector` 路由（默认仅在配置 Provider 且 Generation ready 时参与）；
  Phase 6 envelope/预算/Usage 语义零变化。
- Backup manifest 不含 vector；Restore 后必须 admin rebuild 才恢复 Vector。

## 迁移影响

- `migrations/0008_phase7_vector_recall.sql`（online_safe=true, lock_ms=200,
  min_app=0.8.0, recovery=none）；0001–0007 与 `179b6a0` 逐字节一致（测试锁定）。
- 无 Down Migration。回退顺序：停用 `vector.*` handler 与 Vector 路由 → 0.7.0 兼容二进制
  运行 Schema 8 库（窗口 [7,8] 允许读旧 Schema 7；运行 Schema 8 需 0.8.0）→ 必要时按
  ADR-0013 §10 回退备份（Vector 由 0.8.0 重建）。
