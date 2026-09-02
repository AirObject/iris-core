# ADR-0013: Phase 5 — 显式长期记忆、双时态与不可复活删除

- 状态：Accepted（2026-09-02 复审补充 §3/§4/§5/§7/§10 六项语义；同日二阶复审补充 §3/§4/§5/§7/§10 七项语义；同日三阶复审补充 §3/§4/§5/§7/§10 五项语义）
- 日期：2026-09-01（复审 2026-09-02）
- 影响阶段：Phase 5（Phase 6/7/8/10 依赖本决策的语义）
- 基线：§13、§19、§21、§25.3、§29；ADR-0002/0004/0005/0012

## 背景

Phase 5 交付 Episode、Claim/Evidence、Relation、Artifact、Remember/Correct/Forget/Search、双时态历史读取、Retention/Legal Hold 与 Tombstone 全链路。实现中存在多项跨阶段、不可逆或影响后续契约的语义选择，冻结如下。

## 决策

### 1. 双时态与 Revision 的边界

- Valid time（`valid_from_us`/`valid_until_us`）是调用方声明的业务有效期，存在 Claim/Relation 行与每个 Revision 上；`NULL` 表示该侧无界。
- System time 只存在于不可变 Revision 行：`recorded_at_us` 在 Revision 成为 Current 时写入；`superseded_at_us` 由安装后继 Revision 的同一事务**恰好一次**写入（write-once）。Revision 内容永不修改；唯一的系统时间终止戳是 append-only 纪律的一部分（ADR-0004），不是原地编辑。
- 系统时间区间是 `[recorded, superseded)`，**不存在零长度区间**：同一微秒内的修正把戳记钳制为 `recorded_at + 1`（DB CHECK `superseded_at_us > recorded_at_us` 与 SQL 内 `MAX(?, recorded_at_us + 1)` 钳制共同保证）。
- `as_of` 历史读取按 Revision 链重建当时 Current 的 Revision。历史不足（Retention 修剪掉了所需 Revision，`history_available_from_us > as_of`）返回稳定 `history_unavailable`——**绝不把当前状态伪造成历史答案**；`as_of` Search 在 SQL 内排除历史不完整的 Claim。
- 发生时身份 vs 当前身份：Revision 保存写入时的 `subject_entity_id`；读取视图额外提供经 entity redirect 解析的 `current_subject_entity_id`。解析是纯投影，不回写 Claim，也不回写 Observation。

### 2. Claim 去重与修正语义

- 去重键**精确**为 (tenant, agent, subject, predicate, 规范值哈希, scope)。完全相同的事实再次 Remember 时把 Evidence 附加到既有 Claim（不建第二行、不改内容）；任何分量不同（含不同 Space/Session、不同谓词）就是不同 Claim。**文本或向量相似永远不产生合并**——Phase 10 的候选生成也走同一入口。有效性窗口不是去重分量：同一事实的新有效期是对同一 Claim 的修正（Revision），不是新行。
- Remember 省略主体仅在契约明确的 `subject_is_self` 场景补全：解析到 agent 的唯一 `self_entity` resource_link；不存在则幂等创建；多链接或两者同时给出 → `subject_ambiguous`。
- Correct 三种模式共用一个事务：`supersede`（新值 Revision，旧 Revision 戳记 superseded，状态保持可编辑态）、`dispute`（状态 Revision → disputed + contradicts Evidence）、`retract`（终态）。三模式均要求 Expected Revision CAS（同并发请求由 CAS 仲裁，败者稳定 `revision_mismatch`），写入 corrects/contradicts Evidence。**权威阶梯**（`SOURCE_AUTHORITY_RANK`）：修正的 source_authority 必须不低于现有值，否则拒绝并建议 dispute；模型推断永远不能覆盖高权威事实。
- 权限矩阵：Correct/Remember 全部走授权终检（资源自身 scope/privacy）+ Required Surface 双阶段门禁（缓存前 + 事务内，ADR-0012 §13）。

### 3. Evidence 有效性与 DB 级不变量

- Evidence 行 append-only（`invalidated_us` write-once）；SourceRef 准入按**来源自身**的行验证：存在性、同 tenant/agent、`scope_allows(source, claim_scope)`、来源 revision 匹配（提供时）、状态与 Tombstone。observation 证据额外要求 committed 效果（§15.2）且**其隐私标签对引用方可见**（二阶复审补充：`restricted` Observation 不得为无权调用方充当证据——claim/relation 共用的校验器与 task 证据校验器同法）；artifact 证据走 canonical Artifact validator。**claim 作证据同样走全量准入**（复审补充）：同 tenant/agent、scope 信封、privacy（按来源 Revision 标签评估）、且来源必须处于活跃状态（active/disputed）——superseded/retracted/expired 是历史陈述，tombstoned 更不是证据。
- **Active Claim 至少一条当前有效 Evidence 由数据库表达**：`claims.evidence_count` 去规范化列 + CHECK `status != 'active' OR evidence_count >= 1`。任何把 Active Claim 的计数降到 0 的非原子路径都被 CHECK 拒绝；合法的清零（retract/erasure）与状态变更在同一 UPDATE 内原子完成。Relation 有完全相同的不变量（`relations.evidence_count` + 同形 CHECK）。
- **Relation 证据是规范化行**（复审补充）：`relation_evidence` 与 `claim_evidence` 同形（唯一身份索引 + 来源失效戳记）。Revision JSON 里的 evidence_refs 是不可变的人类可读记录，**绝不是唯一指针**——Forget 抹除来源时 `relation_evidence` 行同步失效，同事务重述计数：归零的 Active Relation 以新 Revision 转入 `retracted`，部分存活的去规范化计数跟随实际有效行数（Claim 同法）。创建去重命中时新证据行幂等附加（身份索引去重）。
- **证据死亡级联是传递闭包**（二阶复审补充）：某 Claim 死亡（被抹除、被级联 retract、或被显式 `correct(retract)`）时，引用它的 claim_evidence 与 relation_evidence 行同步失效，归零的 Active Claim/Relation 以新 Revision 转 `retracted`，**级联迭代至不动点**（B 引用 A、C 引用 B：A 死则 B、C 皆落）。闭包实现为单一共享函数（`cascade_claim_evidence_loss`/`cascade_relation_evidence_loss`），Forget、ledger 重放与 `correct(retract)` 三处同法执行——显式 retract 与抹除对证据不变量是同一种死亡。**级联状态集是 `{active, disputed}`**（三阶复审补充）：dispute 是质疑，不是证据不变量的赦免——disputed 成员证据归零同样退场、部分存活同样 recount，其退场作为死亡源继续传递。**级联退场的新 Revision 发 `claim.changed`/`relation.changed` 变更事件**（与 Remember/Correct 同一投影面，dedupe key 携带新 revision）；它们不进 `memory.invalidated`——该 payload 契约是"tombstone 水位下非当前"，retracted 无 tombstone。Restore 不变量（backup.py）复检：`evidence_count` 与实际未失效行一致、未失效 Evidence 不得指向缺失或已 Tombstone 的来源。

### 4. Artifact 安全面

- 三种存储统一准入：`inline`（≤256 KiB，行内 BLOB）、`local_blob`（受控根目录 `<data_dir>/artifacts/<shard>/<uuid>`）、`external_ref`（URL 作为**数据**保存）。locator 由服务端从 id 派生，客户端提供的路径永不接受；locator 必须精确匹配 `shard/<uuid>`，拒绝绝对路径、`..`、反斜杠、盘符与符号链接（realpath 必须等于字面路径且不逃逸根）。写入经临时文件 + 原子替换；读取前重验 sha-256 与大小，不符即 `artifact_invalid`（fail closed）。
- **external_ref 永不被读取路径抓取**：`ArtifactService.read` 对 external_ref 结构性地只返回元数据（content=None）；Recall/Rehydrate（Phase 6+）复用同一 guarantee。`content_hash` 对 external_ref 是 locator 摘要（sha-256 of URL），**不是内容声明**。内嵌 userinfo 凭据的 URL 拒绝入库。
- 媒体类型默认白名单（text/plain、text/markdown、application/json、image/png|jpeg|webp、audio/ogg、application/octet-stream）；放宽是策略决定，需过本 ADR 变更。
- **内容去重身份是完整 scope 信封**（复审补充）：partial unique index 为 (tenant, scope_key, content_hash, storage_kind, status='active')。同租户全局去重会把 Space A 的 Artifact 发给 Space B 的写入者（越权读取面）并在无 Space A 权限时反噬 Space B 的合法写入——相同字节在不同 scope 是不同 Artifact；同 scope 重复写入才命中去重（引用计数）。**隐私身份参与去重**（二阶复审补充）：索引含 `privacy_key`（repo 侧对 privacy_labels 排序去重的规范化形式）——相同字节相同 scope 但不同隐私标签集也是两个 Artifact，restricted 写入永不别名 public 行（反之亦然）。`privacy_key` 的编码是标签集合的 **canonical JSON**（三阶复审补充）：分隔符 JOIN 不是单射——标签本身可含分隔符，`["a\x1fb"]` 与 `["a","b"]` 不得碰撞（JSON 转义控制字符）。恢复/清扫路径不依赖跨 scope/跨标签共享。
- **Forget 保留 Artifact 行**（复审补充）：事务内先把行内容置空并置 `status='tombstoned'`（CHECK 允许 tombstoned inline 行 content 为 NULL）；`external_ref` 的 URL locator 与 `source_ref` 同时抹除。`local_blob` 文件不能在 SQLite 事务内先删（后续回滚会留下 active 行指向缺失文件），因此只在提交后按本次 tombstone seq 区间删除；同事务落盘的 `memory.invalidated` 事件携带 `erase_content`，worker 会幂等补删，封闭“提交成功、同步 unlink 前进程死亡”的窗口。目录项删除后 fsync 父目录。行保留维持 scope 身份与审计链路、支撑同 key 幂等重放；canonical 读取（read/get）对 tombstoned 行返回不存在，活跃内容去重索引忽略之。
- **Procedure Claim**（§13.4）：value 只允许声明式结构（顶层键 name/description/steps/preferences/tool/notes；step 键 action/description/params/ordinal），结构性拒绝可执行键（exec/shell/sql/code/...）与 shell/SQL 前缀语句（`unsafe_procedure_claim`）。

### 5. Phase 4 交接：Task artifact 证据重启用与 promotion 闭合

- `EVIDENCE_RESOURCE_TYPES` 扩为 `{observation, artifact}`：canonical Artifact validator 已具备存在性、tenant/agent、scope 信封、状态、Tombstone 与 **privacy** 校验（复审补充：restricted/subject-private Artifact 不得为无权调用方完成 Step），满足 ADR-0012 §11.1 预设的启用条件；observation 证据同法评估自身隐私标签（二阶复审补充）。CognitiveEvent 仍结构性排除。
- Note promotion seam 闭合：`claim`/`episode` 目标由 Phase 5 canonical 服务真实物化（Claim 以 note Revision 为 Evidence；Episode 继承 note 已验证的 observation refs），同事务回填 `promotion_target_id` + `promoted_to` resource_link。幂等性来自 promoted 终态 + 写事务 CAS；task promotion 语义不变。

### 6. Note promotion 与 Surface 门禁

- 所有 Phase 5 应用面写（Remember/Correct/Episode/Relation/Artifact/应用面 Forget）沿用 ADR-0012 §12-13 的双阶段门禁：授权先行 → 幂等缓存前校验 → 缓存未命中时业务写事务内复核。lease proof 是逐调用凭证，不进逻辑请求指纹。
- 管理纠正/删除（session/space/data_request 选择器、Retention 策略、Legal Hold、deletion ledger 导出）要求 `access.admin` 与 reason，独立审计，不伪装在线宿主，也不经 Surface 门禁（维护平面）。

### 7. Tombstone selector 优先级与失效事件

- 五种 selector：`resource`、`subject_predicate`（应用面）；`session`、`space`、`data_request`（管理面）。selector 维度必须显式命名，`None` 只表示"不属于本选择器"，永不是通配。
- **应用面逐目标授权终检**（复审补充）：`subject_predicate` 按 agent 解析目标，调用方的 **space 信封**在事务内逐目标复检（`require_same_tenant_agent`）——信封外的目标使整个请求 fail closed（`access_denied`），绝不静默缩小删除集；管理面（admin）只受租户边界约束（§25.3 平面划分）。
- Forget 是一个事务：解析目标（SQL、Tombstone 感知）→ 保护过滤（Pinned Note、未兑现承诺 Note、安全 Claim（restricted）、Persona 资源、Tombstone/Audit/Ledger 元数据结构性不在目标集）→ Legal Hold 仲裁（单资源选择器 fail closed `legal_hold_active`；批量选择器跳过计数；data_request 被 Hold 覆盖时 fail closed）→ 逐资源 Tombstone + 内容抹除 + Evidence 失效（claim 与 relation 两面）+ 证据死亡级联 → deletion ledger 行 → 每 (tenant, agent) 恰一次 watermark 推进 → 分块（50 refs/job）`memory.invalidated` Outbox 事件（payload 只含版本化 ResourceRef、tombstone watermark 与非内容型 `erase_content` 模式位，**绝无正文快照**）。每个事件的 dedupe identity 以已落账的 `forget_request.id` 为根；相同 selector/微秒内的两个独立请求不会互相吞掉失效事件。
- **幂等重放次序**（复审补充）：tombstone 预检位于幂等缓存与 ledger 身份查重**之后**（事务内）——同一 key 重放成功的 Forget 返回原结果（`replayed=true`），不因资源已 tombstone 而 `not_found`；不同 key、不同时刻对已删资源的新请求仍 fail closed。
- **Ledger 请求身份是完整逻辑元组**（二阶复审补充，三阶复审补全）：`(tenant, app_instance_id, selector_key, created_us, idempotency_key, reason_code, erase_content)` 唯一——同 selector 同微秒但不同 app 实例、key、原因或抹除模式的两个请求是**两个请求**（各自入账、各自执行语义）；元组完全一致的重复提交才按同一逻辑请求合并。`app_instance_id` 与幂等缓存命名空间 `(tenant, app, operation, key)` 对齐（三阶复审补充）：**账本区分缓存所区分的**——两个 app 复用同一 key 不在账本上合并。ledger 行记录原始 `erase_content`，restore 重放按原始模式忠实执行。
- **空结果也写 ledger**（复审补充）：目标为空或全部被保护/Hold 跳过时，请求本身仍入账（`erased_count=0`，tombstone seq 区间坍缩为当前 watermark 的空区间 [w, w]，满足 `hi >= lo` CHECK）——零目标结局是合法且可审计的。
- **系统时间区间钳制覆盖所有状态移出路径**（二阶复审补充）：tombstone-only Claim Forget 与级联/显式 retract 一致使用 `max(now_us, recorded_at_us + 1)`——创建同微秒的删除/收回不产生零长度区间（`superseded_at_us > recorded_at_us` CHECK 恒成立）。
- 合规抹除清除正文（Claim 文本/值、Episode 标题/摘要、Note 标题/正文、Observation content/payload/effect_proof、Artifact blob+行内容），保留审计元数据（id、时间、哈希、scope）。`erase_content=False` 仅写 Tombstone 不抹正文（法规禁用场景由部署策略禁止该模式）。
- **抹除不复活**：Tombstoned Claim 上的 dedup 唯一索引失效（partial index 排除 tombstoned），合法重建创建**新行**；幂等缓存回放受 Forget 状态压制（同 Focus 语义，重放校验目标仍存在）；所有当前读取/搜索/重水化比较 Tombstone。

### 8. 三种遗忘的分离与保护资源

- `decay`（accessibility 乘性衰减，floor 0.1，写 Revision，内容与可见性不变）、`archive`（状态 Revision → archived，退出当前读取，历史保留）、`delete`（走与显式 Forget 完全相同的机器——同一事务体、同一 ledger、同一失效事件；**没有更便宜的后门**）。认知衰减永不被伪装成删除。
- Retention Policy 每 (tenant, resource_type, action, privacy_label) 唯一，版本号随更新递增（audit 记录 PolicyVersion 与计数）。**delete 策略必须命名 privacy label**（DB CHECK）——无范围的全量自动删除不可表达。保护资源结构性或策略性跳过且逐项计数：Pinned Note、未兑现承诺、安全 Claim、Persona、Tombstone、审计/Ledger 元数据。Legal Hold 阻断 delete/archive（decay 允许——内容未变）；Hold 释放需要 admin + reason。
- Maintenance 清扫使用无请求 scope 的列举模式（`scope_mode="maintenance"`）——Retention 是维护平面，逐资源应用自身保护/Hold 规则；这不是应用面越权（ADR-0010 §3 平面划分）。

### 9. Restore 不变量扩展

Phase 5 聚合加入 `verify_database_invariants`：episodes/claims/relations 的 Current→Revision 指针解析与 revision 号一致；`evidence_count` 与实际有效 Evidence 一致；未失效 Evidence 不指向缺失/Tombstone 来源；`superseded_at_us > recorded_at_us` 单调；forget ledger 行不超出 Tombstone watermark；Legal Hold 释放不早于创建。伪造 pointer/引用/计数在隔离恢复阶段即被拒绝（fail closed）。

### 10. 旧备份删除重放（deletion ledger）

- 每次成功的 Forget 写一行 `forget_requests`（**完整逻辑身份元组唯一**，二阶复审补充）：selector、原因、请求者、app 实例、幂等 key、抹除模式、tombstone seq 区间、目标/抹除/保护跳过/Hold 跳过计数。**这是可导出的合规删除日志**（admin 导出，details 不含正文）。
- Backup manifest 新增 `artifacts`（local_blob 清单：locator/hash/size）与 `forget_ledger_by_tenant`（**每租户完整 ledger 身份清单** `{watermark_us, requests:[{app_instance_id, selector_key, created_us, idempotency_key, reason_code, erase_content}]}`——复审补充，不再只存 MAX 水位；身份元组二阶复审补全、三阶复审补全 app 分量）。local blob 复制进备份 `artifacts/`，locator 走 canonical validator，磁盘文件集合与 manifest 做精确双向对账并逐项验证 hash/size（缺目录、缺文件、额外文件均拒绝）；restore 将 SQLite 与 blob 都放进私有 staging 树，完整验证后由同一次目录 rename 一起切换到 `<target>/canonical.sqlite3` 与 `<target>/artifacts/`。受控根必须位于目标目录，外置根 fail closed，避免数据库可见后 blob 复制失败造成悬空 active 行。
- **恢复策略（fail closed，按身份差分）**：操作者用恢复前导出的 deletion ledger 重放（`ForgetService.replay_deletion_ledger`）：**凡身份元组不在备份 manifest 清单中的行**按原 selector 幂等重执行（原 created_us 与原抹除模式保持身份与语义稳定；已在库中的行跳过）。manifest 差分只读取已认证的 staging 副本；ledger 在 staging DB 上完成重放、WAL checkpoint 到 main file、恢复不变量复核后才允许切换。任何重放/回调失败都丢弃 staging 并保留原目标，禁止“先切库、后重放”。纯水位 `>` 边界会被同微秒双删除击穿（备份前后两个 Forget 落同一微秒时后者漏放、备份复活）——身份差分对时钟回拨同样安全；重放整份 ledger 也幂等安全（已知行跳过），差分只是免于多余执行。重放语义按 selector 忠实执行——已记录的删除决策重放时不再二次评估内容保护（当时已裁定），但 Legal Hold 若在恢复后仍然活跃会按现行 Hold 规则跳过并计数。生产部署必须同步保存删除日志或使用不早于强制删除边界的备份（§21.2）。**禁止用 Down Migration 丢弃修正/删除历史**；回退使用能理解 Schema 6 的兼容二进制。
- **旧版 int manifest 兼容**（二阶复审补充）：恢复验证识别 legacy `{tenant: watermark_us}` 载荷并只校验其声明的水位；旧格式不带身份清单，restore 对整份 ledger 幂等重放（已知行跳过）——格式降级永不变成验证失败或漏放。
- **旧列集快照与粗粒度 manifest 兼容**（三/五阶复审补充）：身份列（`app_instance_id`/`idempotency_key`/`erase_content`）与 `privacy_key` 是 0006 未发布期间逐轮加上的——真实旧轮备份的快照就没有这些列。备份读方按 `PRAGMA table_info` 列自省构造 SELECT；旧 dict 形 manifest（条目只带 `selector_key`+`created_us`）按“只校验其声明的字段”对账，租户数与水位仍严格。通过认证与原始不变量检查后，restore 在私有 staging 中把旧 Schema 6 规范化为当前列集：backfill canonical `privacy_key`、重建 `forget_requests` 的完整身份 UNIQUE、保留历史行并把 `schema_migrations` 的 0006 checksum 更新到当前发布候选字节。这样同微秒同 selector 的异 reason/key 重放不会受旧 UNIQUE 阻断，恢复出的库也不会因缺 `privacy_key` 无法服务；规范化后再重放 ledger。粗粒度 manifest 无法供精确差分 → 该租户整表重放。

### 11. Search 的 SQL 优先过滤与饥饿预防

- 结构化 Search（无 FTS/Vector）：scope 维度、状态、有效期、Tombstone、as_of 系统时间（含重建 Revision 的状态，经 EXISTS 子查询）全部在 SQL 的 ORDER/LIMIT **之前**过滤。
- Privacy 评估保留在应用层的 canonical evaluator（单一真源）；饥饿由 keyset 续页消灭（页面循环直到取满 limit 或空页），而非把 privacy 下推成脆弱的 SQL。键序 (updated_us, id)。

### 12. Job Kind 推进

启用 5 个新 kind（均具备真实幂等 handler）：`claim.changed`/`episode.changed`/`relation.changed`（指针不变量检查，priority 6）、`memory.invalidated`（逐资源复核 Tombstone 状态与 watermark，fail closed；`erase_content=true` 时幂等补删 tombstoned local blob，priority 1）、`retention.compaction`（§19.5 清扫，priority 8, catch_up=latest）。`forget.execute`/`correct.apply`/`episode.consolidation`/`memory.reconciliation` 保持注册但禁用（无 placeholder 启用）。

## 否决的替代方案

- 用触发器维护"Active Claim 必有 Evidence"：状态机合法性属于领域层（ADR 纪律），DB 只表达可表达不变量——去规范化计数 + CHECK 即可无损表达。
- 把 privacy 下推 SQL LIKE/JSON 匹配：与 canonical evaluator 双轨必然漂移；keyset 续页同样消灭饥饿且零语义分叉。
- Forget 后按 dedup 键拒绝重建（永久封锁该事实）：被删除事实的重新声明是新的授权写入，复活禁止针对的是**旧资源身份**（id/Revision/pointer/blob/索引条目），不是事实本身。
- Retention 走独立轻量删除路径：两台删除机器必然漂移出不等价的保护/Hold/审计——delete 复用 Forget 事务体是唯一单点。
- external_ref 记录时抓取一次以获得真内容哈希：违反"读取路径永不抓取"的结构性保证；locator 摘要 + 未来受控 Ingest 流程才是正解。
- 零长度系统时间区间（允许 superseded == recorded）：区间语义 `[recorded, superseded)` 下会产生同一 as_of 同时命中两个 Revision 的歧义。

## 后果

- Schema 6 新增 12 张 STRICT 表（episodes/episode_revisions、claims/claim_revisions/claim_evidence、relations/relation_revisions/relation_evidence、artifacts、retention_policies、legal_holds、forget_requests）+ partial/expression 索引（含 scope 限定的 artifact 内容去重索引）；runtime 窗口 [5,6]。
- 契约 1.3.0 → 1.4.0（additive：6 个 capability、18 条路径、23 个 schema、7 个错误码：artifact_invalid/evidence_invalid/evidence_required/legal_hold_active/protected_resource/subject_ambiguous/unsafe_procedure_claim）；fixtures 54→85。
- Backup/Restore 不变量与 manifest 扩展（artifact 清单、ledger watermark、blob 回填）；RPO/RTO 演练连续 3 轮。
- Phase 6 交接：`memory.invalidated` 事件 + Tombstone watermark 是 FTS/Vector/Cache 失效的唯一权威输入；Search 的 keyset/过滤模式与 ClaimView 是 Recall 结构化路由的输入。

## 迁移影响

- `migrations/0006_phase5_long_term_memory.sql`（online_safe=true, lock_ms=200, min_app=0.6.0, recovery=none）；0001-0005 与 HEAD `51316fa` 逐字节一致（测试锁定）。
- 兼容窗口 [5,6]：0.6.0 在线升级 Schema 5 库；Schema ≤4 需先经 0.5.0 二进制（Runner 可多步走完，窗口只约束 Ready）。
- 无 Down Migration。回退顺序：停用 retention 清扫与事件领取 → 兼容二进制运行 Schema 6 → 必要时按 §10 恢复流程（隔离目录 + ledger 重放 + 不变量验证）回退备份。
