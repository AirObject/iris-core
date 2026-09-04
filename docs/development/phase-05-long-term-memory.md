# 阶段 5：显式长期记忆与 Episode

> 状态：Completed  
> 前置阶段：[阶段 4](./phase-04-notes-tasks-events.md)  
> 目标版本：0.6.0（已发布：Core/Python SDK/TypeScript SDK 0.6.0，Schema 6，契约 1.4.0）  
> 架构依据：[§13 长期记忆模型](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#13-长期记忆模型)、[§19 Remember/Correct/Forget](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#19-remembercorrectforget-与保留)、[§21 备份恢复与导出](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#21-备份恢复与导出)、[§29 安全与隐私](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#29-安全与隐私)、[§36 阶段 5](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#阶段-5显式长期记忆与-episode)

## 阶段目标

建立可追溯、可修正、可遗忘的长期记忆事实层。阶段结束时，调用方可以显式创建和搜索 Claim，封装 Episode/Relation/Artifact，按双时态读取历史，并证明删除内容不会通过当前读取、后台任务或恢复流程复活。

## 架构约束

- 每个 Active Claim 至少有一条有效 Evidence；模型推断和文本相似只产生候选，不能直接覆盖高权威事实。
- Correct 创建新 Revision/Supersede/Dispute 和 Evidence，不原地覆盖 Canonical Text。
- Forget 先同步影响 Canonical 读取，投影清理由 Outbox 异步完成；所有未来 Builder 必须比较 Tombstone Watermark。
- Confidence、Importance、Accessibility、Activation 和情感分值分开演化，召回使用不能提高 Confidence。
- Episode 不等于 Session，不允许跨 Space 拼接原始内容；Procedure Claim 不保存可执行代码。

## 需求追踪

| 需求 ID | 基线要求 | 工作包 | 验证门禁 |
| --- | --- | --- | --- |
| P5-MEMORY-01 | Episode、Claim、Evidence 的来源、Revision 与有效状态 | 5.1 | 领域、引用完整性、并发与权限测试 |
| P5-RESOURCE-01 | Relation/Artifact 的 Evidence、Scope 与安全存储 | 5.2 | 关系约束、路径、哈希、配额和授权测试 |
| P5-COMMAND-01 | Remember/Correct/Forget/Search 的幂等与权限语义 | 5.3 | API/Schema、竞争写和错误契约测试 |
| P5-HISTORY-01 | 双时态、`as_of` 和发生时/当前身份读取 | 5.4 | 时间边界、历史窗口与不可用测试 |
| P5-ERASURE-01 | Tombstone 同步生效且在后台、缓存和恢复路径不可复活 | 5.5 | 删除竞态、重建、导入和 Restore 测试 |
| P5-RETENTION-01 | Retention、Legal Hold、Archive 与隐私删除分离 | 5.5 | Policy 矩阵、审计和保护资源测试 |

## 工作包

### 5.1 Episode、Claim 与 Evidence

- 实现 Episode 的 Open/Sealed/Superseded/Tombstoned、Observation refs、参与者、时间边界和提取器版本。
- 实现 Claim 的类别、规范值、状态、双时态、评分维度、Source Authority 和去重候选键。
- 实现 supports/contradicts/corrects Evidence，验证 SourceRef、Scope、Revision 和有效状态。

### 5.2 Relation 与 Artifact

- 实现带方向、类型、Privacy、Evidence 和有效期的 Canonical Relation。
- 实现 Artifact locator 规范化、媒体类型/大小/哈希校验、引用计数和受控本地存储。
- 外部引用只作为数据保存，Recall 或读取路径不得自动抓取 URL。

### 5.3 显式记忆 API

- 实现 Remember、Correct、Forget 和不依赖 FTS 的结构化 Search。
- 主体歧义时拒绝自动补全；所有写入校验 Idempotency、Expected Revision、Scope、Privacy 和权限。
- Correct/Forget 同事务更新 Agent/Tombstone Watermark、Audit 并写出未来 FTS/Vector/Profile/Graph/Cache 失效事件。

### 5.4 双时态与历史读取

- 实现 `valid_from/valid_until` 与 `recorded_at/superseded_at` 的读取语义。
- `as_of` 按历史 Revision/Watermark 重建当时可见状态；历史不足返回 `history_unavailable`。
- 支持发生时身份与当前身份的历史视图，不回写 Observation。

### 5.5 保留、Tombstone 与恢复

- 实现 Resource、Subject+Predicate、Session、Space 和授权数据范围的 Tombstone Selector。
- 区分 Accessibility 衰减、Archive 与合规删除；实现 Legal Hold 和不可普通遗忘资源规则。
- 扩展备份/恢复、导入接口和后台 Job，使 Tombstone Watermark 始终优先。

## 数据、契约与回退策略

- 使用 Expand/Backfill/Validate/Cutover 引入 Episode、Claim/Evidence、Relation、Artifact 和双时态字段；Active Claim 的有效 Evidence、Current Pointer 与 Tombstone 优先级在数据库约束和领域校验中同时成立。
- Remember/Correct/Forget/Search Schema 先以结构化 Subject、Scope、Privacy、Evidence 和 `as_of` Fixture 冻结；错误码只新增，省略主体只在契约明确的自我/当前 Actor 场景补全。
- Correct/Forget 在单事务内更新 Canonical Revision、Agent/Tombstone Watermark、Audit 与投影失效 Outbox；未来 FTS/Vector/Profile/Graph Job Payload 以版本化 ResourceRef 消费，不能依赖正文快照。
- Artifact 从 inline/local_blob/external_ref 分阶段启用；回退时先停止新 Blob Ingest，保留引用和内容哈希。外部 URL 永不由读取或 Recall 路径自动抓取。
- 回退优先使用能理解新 Revision 的兼容二进制；若必须恢复备份，先在隔离目录回放切换后 Tombstone，再验证 Current/History、Artifact 引用和权限，禁止用 Down Migration 丢弃修正/删除历史。

## 量化验收基线

- Forget 的 Canonical 生效在声明硬件、Selector 规模和并发下 p95 ≤ 100 ms；此指标不等待投影物理清理，但提交后所有当前读取必须立即拒绝目标。
- Claim/Evidence、双时态、Scope/Privacy、Retention/Legal Hold 和 Tombstone 不可复活性质每项至少运行 200 个固定种子案例。
- 并发 Recall/Correct/Forget 每类竞争场景至少重复 50 次；Forget 成功后的当前读取命中数必须为 0，历史读取仅按授权和保留策略返回。
- 对 Cache 占位、旧 Outbox、旧索引 Candidate、Artifact、导入和 Backup Restore 六条复活路径分别执行至少 20 次故障/重放测试，目标内容返回数必须为 0。
- Backup → 隔离 Restore → Tombstone/Pointer/Foreign Key/Artifact/Smoke Recall 校验连续通过 3 次；报告记录数据规模、RPO、RTO 与待重建投影。

## 退出门禁

- [ ] Active Claim 无 Evidence、未知 Entity、越权 Scope 或非法 ResourceRef 均被拒绝。
- [ ] Correct 保留旧 Revision，当前/历史/Disputed 视图与双时态测试通过。
- [ ] 并发 Recall/Correct/Forget 下，Forget 提交后任何当前读取都不返回目标内容。
- [ ] 删除后通过 Cache 占位、Outbox 重试、备份恢复和模拟索引旧结果均不可复活。
- [ ] Artifact 路径穿越、超限、哈希不符和未授权读取测试通过。
- [ ] 自动保留策略不会处理 Persona Core、Pinned Note、Active Task、未兑现承诺和 Tombstone。
- [ ] Migration/契约兼容/恢复回退方案、需求追踪和交付证据已完成评审。

## 交付证据

- 代码/变更：`domain/memory.py`、`domain/retention.py`、`storage/memory.py`（12 张新表的仓储，含 `relation_evidence`、artifact `privacy_key`（canonical JSON 编码）与 forget ledger 列自省）、`application/memory.py`（Remember/Correct/Search/history + claim/observation 证据全量准入 + `correct(retract)` 级联）、`application/episodes.py`（Episode/Relation + 规范化证据行）、`application/artifacts.py`（scope+隐私去重、tombstone 行保留）、`application/forget.py`（Forget/deletion ledger 完整身份元组（含 app 实例分量）/request-id 失效事件身份/提交后 blob 清理/身份差分重放/逐目标信封终检/传递闭包证据级联（覆盖 disputed，退场 Revision 发 claim.changed/relation.changed））、`application/retention.py`（Policy/Legal Hold/清扫）、`jobs/handlers.py` + `jobs/worker.py`（5 个新启用 kind，失效 worker 幂等补删 blob）、`storage/backup.py`（artifact+SQLite 同树原子切换、staging 内 ledger 重放、legacy int manifest 与旧列集 Schema 6 规范化、Phase 5 不变量）、Phase 4 交接（notes promotion seam、task artifact/observation 证据重启用含 privacy）。
- ADR：[ADR-0013](../adr/0013-phase5-long-term-memory.md)（双时态、去重/修正、Evidence 不变量、Artifact 安全面、Tombstone selector 优先级、旧备份删除重放、Retention/Legal Hold、Surface 门禁、Phase 6 投影边界；2026-09-02 第二至第五轮复审语义均已同步）。
- 回归：第二轮对抗性评审（2×P0 + 5×P1 + 1×P2）、第三轮二阶评审（6×P1 + 1×P2）、第四轮三阶评审（5×P1）与第五轮提交前复核（同微秒失效 identity、文件/事务边界、旧 Schema 6 升级、恢复 staging 原子性及连带发现）均已修复，`tests/integration/test_phase5_review_round2.py` 至 `round5.py` 与 `test_tasks.py` 增补锁定（见验证报告 §7.2–§7.5）。
- Schema/Migration：`migrations/0006_phase5_long_term_memory.sql`（online_safe=true, lock_ms=200, min_app=0.6.0, recovery=none；SHA-256 见验证报告）。0001-0005 与 HEAD `51316fa` 逐字节一致（测试锁定）。Runtime 窗口 [5,6]。
- 契约：1.4.0（additive：6 capability、18 路径、23 schema、7 新错误码）；fixtures 54→85；OpenAPI/JSON Schema/manifest/mock server/双 SDK 同步。
- 验证报告：[phase-05-verification.md](../reports/phase-05-verification.md)（实测测试数、覆盖率、性能、恢复演练）。
- 已知限制：见验证报告"已知限制"（Privacy 过滤在应用层经 keyset 续页实现、 Forget 事务规模上限 5000 目标、external_ref 内容不校验等）。

## 明确不做

- 不实现 LLM 自动提取、FTS、Embedding、Graph 多跳或 Profile 汇总。
- 不用相似度自动合并主体、Scope、有效时间不同的 Claim。
- 不把认知衰减伪装成隐私删除。

## 交接条件

> 下列内容同时构成 Phase 6 的验收基线。

Phase 6 可以依赖（均已交付并有测试锁定）：

- 稳定的 Claim/Episode/Relation/Artifact ResourceRef 与 Revision 契约；
- Tombstone Watermark 与 `memory.invalidated` 版本化失效事件（refs-only payload）作为 FTS/Vector/Cache 失效的唯一权威输入；
- 结构化 Search（SQL 优先过滤 + keyset 续页）与双时态 `as_of`/`history_unavailable` 语义；
- Artifact blob 受控根与 hash 验证读取；Procedure Claim 声明式约束。
