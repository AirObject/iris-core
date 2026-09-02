# Phase 5 验证报告：显式长期记忆与 Episode

> 状态：Completed（第一轮实测 + 第二至第五轮对抗性复审修复后重测）  
> 日期：2026-09-01（实现与第一轮实测）；2026-09-02（第二至第五轮评审修复与全量重跑）  
> 基线 commit：`51316fa`（feat: complete phase 4 notes tasks and events）之上的工作区  
> 硬件：Apple Silicon（darwin 25.6.0 arm64），本地 SQLite 3.50.4（测试显式 pin，等价部署 pin）  
> 验收文档：[phase-05](../development/phase-05-long-term-memory.md) · 决策：[ADR-0013](../adr/0013-phase5-long-term-memory.md)

本报告只记录实际运行结果。全部数字来自本轮 `make ci` 与量化门禁脚本的实测输出。

## 1. 版本与契约

| 项 | 值 |
| --- | --- |
| Core / Python SDK / TypeScript SDK | 0.6.0 |
| Schema | 6（migration `0006_phase5_long_term_memory.sql`） |
| 契约版本 | 1.4.0（additive：6 capability、18 路径、23 schema、7 错误码） |
| fixtures | 85（valid 61 / invalid 21 / forward 3，新增 31） |
| Runtime 兼容窗口 | [5, 6]（`verify_schema_compatible` 测试锁定） |

Migration checksum（SHA-256，`MigrationRunner` 记录于 `schema_migrations`；第四轮评审修复后重算）：

```
3d790483994a32bea9f37c6c23a9bc556d322b34214d2b2ab8fea5321dcc689c  migrations/0006_phase5_long_term_memory.sql
```

0006 未发布，发布前的三轮评审各修订了一次该文件：第三轮为 `forget_requests` 增加 `idempotency_key`/`erase_content` 列并扩展唯一身份索引、artifacts 增加 `privacy_key` 列且内容去重索引纳入隐私身份；第四轮为 `forget_requests` 再增 `app_instance_id` 身份列（与幂等命名空间对齐，见 §7.4-2）。0001–0005 与 `git show HEAD:migrations/…` 逐字节一致（`test_published_bytes_unchanged` 与 Phase 2/4 既有测试锁定）。

## 2. 量化门禁实测

| 门禁 | 要求 | 实测 | 结果 |
| --- | --- | --- | --- |
| Claim/Evidence 性质 | ≥200 固定种子 | 200 种子 × 3 组（状态机/双时态可见性/有效时间）全过 | ✅ |
| 去重键分量区分性质 | ≥200 | 200 种子全过（任意分量扰动 → 键不同） | ✅ |
| Procedure guard / locator / URL 防御 | ≥200 | 200 种子 × 4 组全过 | ✅ |
| selector/Hold/Policy/decay 性质 | ≥200 | 200 种子 × 4 组全过 | ✅ |
| 并发 Recall/Search × Forget 竞争 | ≥50 轮，Forget 后当前命中=0 | 50 轮 × 3 场景（searcher×4 竞争 / CAS / forget-vs-correct），提交后当前命中数全部为 0 | ✅ |
| 并发 Correct CAS | ≥50 轮 | 50 轮，每轮 4 线程同 Expected Revision：恰好 1 胜、其余稳定 `revision_mismatch` | ✅ |
| 六条复活路径 | 每条 ≥20 轮，目标返回=0 | 每条 20 轮全过（缓存回放 / 旧 Outbox 重放+handler 复核 / 模拟旧索引 rehydrate 门 / Artifact 全读取面 / Import 门 / Backup→Restore+ledger 重放），目标返回数全部为 0 | ✅ |
| 证据死亡级联 | — | 20 轮（kill-9 场景内含）+ 集成矩阵：唯一证据被抹除的 Active Claim 同事务转 `retracted` | ✅ |
| Forget canonical 生效 p95 | ≤100 ms | 见 §4 | ✅ |
| Backup→隔离 Restore→校验 | 连续 3 轮 | 3 轮全过（Tombstone/Pointer/FK/Artifact hash/Smoke Search + ledger 重放） | ✅ |
| Migration 5→6 | Phase 4 数据保全 | Note 数据 1:1 保全、12 张新表就位、invariants 为空 | ✅ |
| 伪造 pointer/reference 拒绝 | — | 4 类伪造（悬空指针/幽灵 evidence/计数撒谎/ledger 超 watermark）全部被 restore invariants 拒绝 | ✅ |
| kill-9 Forget 原子性 | — | 3 边界 × 20 重复：全有或全无（tombstone+erasure+ledger+invalidation 同进退） | ✅ |

## 3. 测试规模与覆盖率

（以下为最终 `make ci` 的实测汇总，见 §9）

- 阶段 5 新增测试文件：`tests/unit/test_phase5_domain.py`、`tests/integration/test_phase5_claims.py`、`test_phase5_episodes_relations.py`、`test_phase5_artifacts.py`、`test_phase5_forget.py`、`test_phase5_resurrection.py`、`test_phase5_concurrency.py`、`test_phase5_migration.py`、`test_phase5_backup.py`、`test_phase5_review_round2.py`（第二轮评审回归）、`test_phase5_review_round3.py`（第三轮评审回归）、`test_phase5_review_round4.py`（第四轮评审回归）、`test_phase5_review_round5.py`（第五轮评审与连带发现回归）、`tests/performance/test_forget_latency.py`、`tests/fault/test_kill9.py::forget`；另在 `tests/integration/test_tasks.py` 增补 artifact 与 observation 证据 privacy 回归。
- Phase 0–4 回归：未删除、未弱化；仅按 Phase 5 语义更新了四处既有断言（schema 窗口/迁移列表/已启用 kind 集/task artifact 证据测试重写为 canonical validator 语义，ADR-0013 §5），以及 phase3/4/migration 回滚夹具补 `relation_evidence` 表（Schema 6 表集合变更的合理跟进）。
- 覆盖率：**83.95%**（第五轮修复后全量 `make ci`，阈值 80%；第一轮 83.57%、第二轮 83.85%、第三轮 83.93%、第四轮 84.01%）。
- 测试量：第五轮修复后全量 **7597 passed / 0 failed**（第一轮 7550；第二轮 +16 评审回归；第三轮 +12；第四轮 +7；第五轮 +12——`test_phase5_review_round5.py`）。Phase 0-4 回归全部保留并通过。

## 4. Forget canonical 生效性能

测量方法：`tests/performance/test_forget_latency.py` 同法独立复测（2026-09-02，第四轮修复后的代码），`time.perf_counter()` 包住 `ForgetService.forget()` 全事务（tombstone+抹除+ledger+watermark+invalidation outbox+证据级联），不含异步投影清理；顺序执行；40 样本/组。第三轮起每个样本为独立请求时刻（此前冻结时钟下旧身份把重复样本合并为重放，测量偏乐观；现每样本真实执行完整抹除语义）。

| selector 规模 | 样本 | 实测 p50 / p95 / max | 预算 |
| --- | --- | --- | --- |
| 单资源 | 40 | 5.2 ms / **5.7 ms** / 5.8 ms | 100 ms |
| 20 目标 subject_predicate | 40 | 6.8 ms / **7.4 ms** / 8.0 ms | 100 ms |

（第三轮代码增加传递闭包级联与完整身份查重，20 目标 p95 由第二轮 5.0 ms 升至 7.2 ms；第四轮级联覆盖 disputed 并为退场 Revision 增发变更事件，复测 7.4 ms，仍远低于预算。）

## 5. 恢复演练（RPO/RTO）

`tests/integration/test_phase5_backup.py` 连续 3 轮：25 Claim + 1 local_blob artifact 备份 → 备份后抹除 1 Claim + 新增 1 Claim → 隔离 restore（SQLite + artifact blob 同一 staging 树，先完成 deletion ledger 重放与 WAL checkpoint，再原子切换）→ Tombstone/Pointer/FK/Artifact hash/真实 `ArtifactService.read`/Smoke Search 断言。三轮 restore 墙钟耗时记录于测试内 `durations`（断言非负，报告数值见本轮 CI 日志）。

- RPO：等于 online backup 时刻的最后已提交事务（backup manifest 记录 schema/watermark/ledger 身份清单）。
- RTO（本机、25 Claim + 1 local blob + 1 条 ledger 重放，2026-09-02 第四轮修复后复测）：三轮 restore+deletion-ledger 重放实测 **8 / 8 / 7 ms**（第三轮）→ 第四轮同法独立复测 restore 9 ms / smoke 2 ms 量级不变；测试内另含 smoke search 断言（survivor 在、doomed 与备份后新增均不在）。
- 同微秒双删除复活回归：`test_same_microsecond_forget_around_backup_is_replayed`（备份前后两个 Forget 落同一 created_us，恢复后两者均不可复活）；旧版 int manifest 恢复回归：`test_legacy_int_manifest_verifies_and_restores`（第三轮）；真实旧 Schema 6 列集快照（缺身份列）的 verify/restore/重放回归：`test_round2_schema_backup_verifies_restores_and_replays`（第四轮）。
- 待重建投影：无（Phase 6 之前不存在 FTS/Vector/Profile/Graph 投影；`memory.invalidated` 事件已入 Outbox）。

## 6. 泄漏扫描（canary）

- 集成测试 `test_audit_and_ledger_never_copy_content`：对 Forget 路径的 audit details 与 deletion ledger selector_json 做 canary 断言（"SECRET CANARY" 不出现）。
- 契约面：Phase 5 请求/响应 schema 不含正文之外的派生内容；`memory.invalidated` payload 仅 ResourceRef+watermark+非内容型 `erase_content` 模式位（handler 强校验）。
- 日志/错误：错误消息只含资源类型/id/约束名（`ConflictError(f"...constraint: {error}")` 不含行数据）。

## 7. 对抗性自审与回归

### 7.1 第一轮（实现期自审，2026-09-01）

1. 证据失效级联的候选收集时序（先失效后查询导致空集）→ 级联候选在失效前收集（`test_evidence_loss_cascade_retracts_active_claims`）。
2. Retention 清扫以 agent 级请求 scope 搜索导致 space 级 Claim 永不被清扫 → `scope_mode="maintenance"`（`test_legal_hold_blocks_retention_archive` 覆盖）。
3. 同微秒修正产生零长度系统时间区间违反 CHECK → SQL 级 `MAX(?, recorded_at+1)` 钳制（`test_supersede_preserves_old_revision_and_stamps_system_time`）。
4. `relations_for_entity` 相邻字符串字面量漏拼接（SQL incomplete）→ 修复并有 Forget data_request 路径覆盖。
5. `RetentionSweepReport` frozen 与计数累加冲突 → 改为可变 dataclass。
6. 备份 artifacts/ 目录被文件集形状校验拒绝、blob 回填源路径在 rename 后失效 → 两处修复（三轮演练覆盖）。
7. 维护平面 forget actor 无 agent 信封被 `require_same_tenant_agent` 拒绝 → admin 分支只查租户（§25.3 平面划分）。
8. `as_of` Search 的状态过滤错误地作用于当前指针而非重建 Revision（今天 retract 的 Claim 在历史时刻仍是 active）→ 状态过滤下推为 Revision EXISTS 子查询，当前行状态过滤仅在非 as_of 模式应用（回归：`test_as_of_sees_claim_that_was_active_before_retraction`）。

### 7.2 第二轮（独立对抗性评审，2026-09-02；8 缺陷全部修复并入回归）

评审方以临时数据库最小复现 2×P0 + 5×P1 + 1×P2；修复后全部回归测试落盘于 `tests/integration/test_phase5_review_round2.py`（15 例）与 `tests/integration/test_tasks.py`（1 例）：

1. **[P0] 删除账本水位漏放**：备份前后两个 Forget 落在同一微秒时，`MAX(created_us)` + 严格 `>` 的重放边界漏掉后者（恢复后 Claim 复活为 active）。修复：manifest `forget_ledger_by_tenant` 携带每租户完整 ledger 身份清单 `{watermark_us, requests[]}`，restore 按 (selector_key, created_us) **身份差分**重放（`test_same_microsecond_forget_around_backup_is_replayed`：同微秒双删除 0 复活；legacy int manifest 回退为整表幂等重放）。
2. **[P0] subject_predicate 跨 Space 越权删除**：授权只查 Agent，目标查询无 Space 信封，仅持 Space A 权限的调用方可删 Space B Claim。修复：`_execute_forget` 内应用面逐目标 `require_same_tenant_agent` 终检（fail closed，不静默缩小；admin 平面只受租户边界约束）（`test_narrow_space_caller_cannot_forget_other_space_claims`）。
3. **[P1] Task Artifact 证据缺 privacy 校验**：`_validate_evidence` 未接 `AccessContext`，非管理员可用 `restricted` Artifact 完成 expected-effect Step。修复：证据校验签名加入 access，artifact 分支执行 `evaluate_privacy`（`test_artifact_evidence_respects_privacy_labels`：非管理员 denied、step 保持 IN_PROGRESS、admin 正常完成）。
4. **[P1] Claim 作证据缺同 Agent/Scope/Privacy/有效状态校验**：原仅查 tenant/revision/tombstone，Agent 1 可引用 Agent 2 的 Claim 建 Active Claim。修复：claim 分支走全量准入（同 tenant/agent + `scope_allows` + 来源当前状态 ∈ {active, disputed} + 来源 Revision 标签 privacy）（`test_cross_agent_claim_evidence_rejected`、`test_space_scoped_claim_not_visible_to_space_scoped_citing_claim`、`test_retracted_claim_is_not_live_evidence`、`test_subject_private_claim_evidence_requires_consent`）。
5. **[P1] Relation 证据不可失效**：evidence 仅存于 Revision JSON，Forget 只失效 `claim_evidence`；删除唯一 Observation 后 Relation 仍 active 且 `evidence_count=1`。修复：0006 新增 `relation_evidence` 规范化表（同形唯一身份索引），创建/去重附加写入规范化行，Forget 同事务失效 + 级联（归零 retract 新 Revision、部分存活 recount；Claim 同步获得部分存活 recount）（`test_forgetting_the_only_evidence_retracts_the_relation`、`test_relation_with_two_evidence_rows_survives_one_loss`、`test_dedup_replay_attaches_new_evidence_rows`）。
6. **[P1] Artifact 去重 tenant 全局破坏 Scope 身份**：同内容写入 Space B 返回 Space A 的 Artifact（`requested_space != stored_space`；无 Space A 权限时 Space B 写入被拒）。修复：去重身份加入完整 `scope_key`（索引 + 查询），同 scope 才命中去重（`test_same_content_in_two_scopes_is_two_artifacts`）。
7. **[P1] 成功的 Resource Forget 无法幂等重放**：tombstone 预读先于幂等缓存，同 key 重试得 `not_found`。修复：tombstone 检查移入事务内、位于幂等缓存与 ledger 身份查重之后；Artifact 抹除保留行（blob 删除 + 内容置 NULL + `tombstoned`，inline CHECK 允许 tombstoned 行 NULL），read/get 返回不存在（`test_resource_forget_replays_its_own_success`、`test_artifact_forget_keeps_row_replays_and_reads_none`、`test_inline_artifact_forget_scrubs_content_bytes`）。
8. **[P2] 空选择器/全 Hold 跳过写不出 Ledger**：`[seq_before+1, seq_before]` 违反 `hi >= lo` CHECK，返回 `ConflictError`。修复：空结局 seq 区间坍缩为 `[w, w]`，请求照常入账并报告 `erased_count=0`（`test_subject_predicate_with_no_targets_writes_a_ledger_row`、`test_all_held_session_forget_reports_skips`）。

修复过程另有连带修正：phase3/4/migration 回滚夹具补 `relation_evidence` 表；`_cascade_evidence_loss` 非 active 候选跳过逻辑等价重写 + 部分存活 recount；`ForgetService` 测试夹具改为共享同一 ForgetService 实例（RetentionService 依赖注入）。

### 7.3 第三轮（二阶对抗性评审，2026-09-02；6×P1 + 1×P2 全部修复并入回归）

评审方在第二轮修复代码上复现 6 个 P1 与 1 个 P2（后者为第 4 项的 `correct(retract)` 分支）；修复后回归落盘于 `tests/integration/test_phase5_review_round3.py`（11 例）与 `tests/integration/test_tasks.py`（observation 证据 privacy 1 例）：

1. **[P1] 旧版 int 格式账本无法恢复**：restore 前的 manifest 严格比对按新结构进行，旧 int 格式直接验证失败，"整表重放"回退永远到不了。修复：`_reconcile_manifest` 识别 legacy int 载荷并只校验其声明的水位（快照行推导完整身份；多出的租户视为失配），restore 回退为整表幂等重放（`test_legacy_int_manifest_verifies_and_restores`；错误水位的 legacy manifest 仍拒绝——`test_legacy_manifest_with_wrong_watermark_still_fails`）。
2. **[P1] Observation 证据绕过 Privacy**：claim/relation 共用的 `validate_evidence_sources` 与 task 的 `_validate_evidence` 均未评估 Observation 标签。修复：两处 observation 分支执行 `evaluate_privacy(obs.privacy_labels, obs_scope, 目标 scope, access)`（`test_non_admin_cannot_cite_restricted_observation`、`test_relation_evidence_observation_privacy`、`test_observation_evidence_respects_privacy_labels`）。
3. **[P1] Artifact 去重忽略 privacy_labels**：restricted 写入命中既有 public Artifact 并返回其 id。修复：0006 artifacts 增加 `privacy_key`（repo 侧排序去重规范化），内容去重唯一索引与查询纳入隐私身份——同内容不同标签是两个 Artifact（`test_restricted_ingest_never_aliases_a_public_artifact`：restricted ingest 独立成行、普通调用方不可读、同标签仍去重）。
4. **[P1] Claim 证据死亡级联非传递闭包**：来源 Claim retract 后未失效引用它的证据并继续级联（上游 retracted、下游仍 active/evidence_count=1）；显式 `correct(retract)` 完全没有该处理。修复：级联改为工作清单传递闭包（claim 与 relation 证据行随 retract 失效、归零者同事务 retract、循环至不动点），并提取为模块级 `cascade_claim_evidence_loss`/`cascade_relation_evidence_loss` 供 Forget、ledger 重放与 `correct(retract)` 三处共用（`test_forget_cascade_is_transitive`、`test_correct_retract_cascades_to_citing_claims`、`test_correct_retract_cascades_to_relations`）。
5. **[P1] 同微秒独立 Forget 被错误合并**：请求身份只有 `(selector_key, created_us)`，不同 key/reason/erase_content 的同微秒请求返回第一条的 request id 且不执行新语义。修复：ledger 身份扩展为完整逻辑元组 `(tenant, selector_key, created_us, idempotency_key, reason_code, erase_content)`（0006 唯一索引同步扩展），backup manifest 身份清单与 restore 差分使用同一元组；ledger 行记录原始 erase_content，重放按原始模式执行（`test_same_instant_different_key_is_its_own_request`、`test_management_plane_reason_and_mode_distinguish`）。
6. **[P1] 同微秒 tombstone-only Claim Forget 零长度区间**：`erase_content=False` 分支未钳制，创建后同微秒 Forget 触发 `superseded_at_us > recorded_at_us` CHECK 失败。修复：`max(now_us, recorded_at_us + 1)` 钳制（`test_tombstone_only_forget_clamps_the_supersede_stamp`）。

连带修正：`tests/performance/test_forget_latency.py` 逐样本推进时钟（旧身份曾把冻结时钟下的重复样本合并为重放，测量偏乐观；现每样本真实执行，测量口径修正并记录于 §4）。

### 7.4 第四轮（三阶对抗性评审，2026-09-02；5×P1 全部修复并入回归）

评审方在第三轮修复代码上复现 5 个 P1；修复后回归落盘于 `tests/integration/test_phase5_review_round4.py`（7 例）：

1. **[P1] "Legacy 兼容"只改了 manifest 形状，不兼容真实旧 Schema 6 快照**：backup 读方直接 SELECT 新列，真实旧轮备份（`forget_requests` 无 `idempotency_key`/`erase_content`/`app_instance_id`、artifacts 无 `privacy_key`）在 verify 即报 `no such column`，"整表重放"回退不可达。修复：读侧改为**列自省**——`_ledger_rows` 按 `PRAGMA table_info` 出现的列构造 SELECT，缺列读作 `''`/`0`（正是旧构建会写入的值），备份写方与 verify/restore 读方共用；旧 dict 形 manifest（条目只带 `selector_key`+`created_us`）按"**只校验其声明的字段**"对账（租户数与水位仍严格）；粗粒度 manifest 无法供精确差分 → 该租户无身份清单、restore 整表重放。retention 仓储同样列自省（insert 只写存在的列、find 在旧列集上降级为 `(tenant, selector, created_us)` 最细身份——旧 schema 恢复库可继续重放与入账；这是旧库可表达的极限，文档化于 ADR-0013 §10）（`test_round2_schema_backup_verifies_restores_and_replays`：快照表真实重建为旧列集 + manifest 改写为旧条目形状 + 校验和重锚 → verify/restore/replay 全通、重放行落在旧列集上、restore invariants 干净）。
2. **[P1] Forget 身份缺 app_instance_id**：幂等缓存命名空间是 `(tenant, app, operation, key)` 而 ledger 身份只有 key——两个 app 以同 key/selector/reason/mode 同微秒请求时，缓存建两条记录、账本只落一行，第二个请求返回第一条 request id 且 `replayed=false`。修复：`forget_requests` 增加 `app_instance_id` 列并纳入唯一索引，完整身份元组为 `(tenant, app, selector, created_us, key, reason, mode)`——**账本区分缓存所区分的**；manifest 身份清单、restore 差分、ledger 重放同元组（`test_same_instant_same_key_different_app_is_distinct`：两行各自执行、真重放仍归原 app 的原结果）。
3. **[P1] privacy_key 分隔符编码可碰撞**：`"\x1f".join(sorted(set(labels)))` 下 `["custom:a","custom:b"]` 与合法单标签 `["custom:a\x1fcustom:b"]` 生成同一 key——前者权限的调用方可借去重读到后者的行，反之合法调用方反而被拒。修复：`privacy_key` 改为标签集合（排序去重）的 **canonical JSON** 编码——JSON 转义控制字符，编码是单射（`test_separator_inside_a_label_never_collides`：两行独立、标签各保其真、同集合仍去重）。
4. **[P1] 传递级联跳过 disputed**：候选状态过滤只有 active——disputed Claim 证据全灭后仍 disputed、去规范化计数陈旧（restore invariants 报失配）、死亡不经它继续传递。修复：claim/relation 两面级联的状态集扩为 `{active, disputed}`（**dispute 是质疑，不是证据不变量的赦免**）：归零同事务以新 Revision 转 `retracted`、部分存活 recount，且 retracted 的 disputed 成员作为死亡源继续进闭包（`test_disputed_claim_retracts_and_cascade_continues`——含"部分存活时先 recount 再全灭才退场"两段断言；`test_disputed_relation_retracts_when_evidence_dies`；两例均以在线备份快照断言 restore invariants 干净）。
5. **[P1] 级联产生的新 Revision 无 Outbox 变更事件**：退场 Revision 只推进指针/watermark/audit——投影无从得知下游 Claim 已 `retracted`。修复：`_retract_claim_without_evidence`/`_retract_relation_without_evidence` 发 `claim.changed`/`relation.changed`（与 Remember/Correct 同一变更面，dedupe key 携带新 revision；Forget、ledger 重放与 `correct(retract)` 三处级联共用同一实现）；级联成员**刻意不进** `memory.invalidated`——其 payload 契约是"tombstone 水位下非当前"，retracted 无 tombstone，handler 的 fail-closed 复核会拒绝（`test_claim_cascade_retractions_emit_claim_changed`：两个退场成员各得 revision-2 事件、invalidated 只含被抹除的顶点；`test_relation_cascade_retraction_emits_relation_changed`）。

连带修正：`backup_ledger_identities` 对粗粒度（非完整身份字段）manifest 条目返回该租户空身份集（此前会把缺字段条目当作默认值身份，误供差分）；`_reconcile_manifest` 非 legacy 分支由整体 dict 相等改为逐字段声明式对账（自身即旧形状容忍的一部分）。

### 7.5 第五轮（提交前复核，2026-09-02；4 项既有缺陷 + 5 项连带缺陷全部修复并入回归）

回归集中于 `tests/integration/test_phase5_review_round5.py`，并同步加强 `test_phase5_backup.py`、`test_phase5_forget.py`：

1. **[P1] 同微秒独立 Forget 的 Outbox dedupe identity 仍碰撞**：ledger 已使用完整身份，但 `memory.invalidated` 仍以 `selector_key+now_us+chunk` 去重；第二个合法请求会在 outbox UNIQUE 上失败并回滚。修复：事件 identity 以已落账的 `forget_request.id` 为根，正常 Forget 与 ledger replay 共用（同微秒两请求均提交且各有事件）。
2. **[P1] local_blob 在 SQLite 事务内 unlink**：若后续 ledger/outbox 写入失败，数据库回滚为 active，文件却不可回滚，形成悬空行。修复：事务内只 tombstone 行；提交后按 tombstone seq 区间删除并 fsync 父目录，`memory.invalidated` worker 依据 `erase_content=true` 幂等补删，覆盖提交后进程死亡窗口；tombstone-only 模式始终保留字节。
3. **[P1] 真实旧 Schema 6 只做到“可读”，恢复后仍不可服务**：缺 `artifacts.privacy_key` 的库在新仓储写入时报错，旧 `forget_requests` UNIQUE 还会拒绝同微秒同 selector 的异 reason/key 重放。修复：已认证快照在私有 staging 内规范化到当前 Schema 6（privacy key backfill、forget 表重建、完整 UNIQUE、0006 checksum 更新）后才重放与切换；历史行保留。
4. **[P2] legacy fixture 未记录真实历史 migration checksum**：测试只改表/manifest，仍保留当前 0006 checksum，无法证明迁移记录兼容路径。修复：fixture 写入真实旧轮 checksum，恢复后断言更新为当前 checksum 且普通 `MigrationRunner.migrate()` 无待执行项。
5. **[P1，连带] external_ref 抹除仍保留 URL 与 source_ref**：仅清空 inline content 不足以满足内容抹除。修复：tombstone 同事务把 URL locator 置为 `<erased>`、`source_ref` 置空；local/inline locator 只保留服务端派生值供清理/审计。
6. **[P1，连带] restore 的 ledger replay 在目标切换后执行且重读源 manifest**：源目录可在 verify 后被改写以抑制差分，重放失败时新目标已可见。修复：身份差分读取已认证 staging manifest，重放也在 staging DB 完成；任何失败丢弃 staging、旧目标保持原样。
7. **[P1，连带] staging ledger replay 留在 WAL，immutable 不变量检查看不到**：可能检查旧 main file 后连带 sidecar 切换。修复：切换前执行 WAL TRUNCATE checkpoint、转 DELETE journal、确认 sidecar 消失并 fsync；`immutable=1` 直读断言能看到重放 tombstone。
8. **[P1，连带] Artifact blob 在数据库切换完成后复制到目标目录之外**：复制失败时新数据库已可见，且 `Store` 实际受控根 `<target>/artifacts` 与复制位置不一致。修复：SQLite 与 blob 保持在同一 staging/target 树、一次目录 rename 共同生效；外置 artifact root fail closed。故障注入证明 blob 复制失败时旧数据库目录与旧 blob 均原样保留。
9. **[P1，连带] blob manifest 对账不是双向的**：manifest 声明 local blob 但整个 `artifacts/` 缺失时验证循环被跳过；目录内额外的未声明文件也可随 restore 进入受控根。修复：manifest locator 统一走 canonical locator validator，磁盘文件集合与声明集合精确双向对账，逐文件 hash+size 校验；缺目录、缺文件与额外文件都拒绝。复制完成后 fsync 对应父目录，补齐嵌套 blob 目录的崩溃持久性。

## 8. 已知限制

1. **Privacy 过滤在应用层**：canonical `evaluate_privacy` 是唯一评估器，Search 以 keyset 续页消除饥饿（ADR-0013 §11）；未把 privacy 下推 SQL。
2. **单 Forget 事务 ≤5000 目标**：超限要求收窄 selector 或分批（管理面编排，Phase 14 容量策略细化）。
3. **external_ref 内容不校验**：结构上永不抓取；`content_hash` 为 locator 摘要，不是内容声明。
4. **媒体类型默认白名单**较窄；放宽需 ADR 变更。
5. **History 修剪边界**：retention decay 路径修剪 Revision 并推进 `history_available_from_us`（keep=10）；更早 `as_of` 返回 `history_unavailable`（契约语义，不伪造）。
6. **Recall/FTS/Vector/Profile/Graph 未实现**（Phase 6+）：本阶段只交付 `memory.invalidated` 失效事件与结构化 Search。
7. **旧备份恢复必须配合 deletion ledger 重放**：未提供 ledger 的恢复无法恢复备份后删除（§21.2 生产要求同步保存删除日志）。

## 9. `make ci` 摘要

（任何失败都会使本节重写。）

- `format-check` / `lint`（ruff + import boundaries + docs）：通过
- `typecheck`（mypy + tsc）：通过
- `contracts-check`（generator --check + compatibility）：通过（纯 additive，baseline 未动）
- `pytest`（全量，`--cov-fail-under=80`）：**7597 passed, 0 failed**（155.68 s），总覆盖率 **83.95%**（阈值 80%）
- `sdk-test`（tsc + node --test）：**9 pass / 0 fail**

最终一轮（2026-09-02，第五轮提交前复核修复后，基线 51316fa 之上的工作区）实测输出摘要：

```
make ci
  ruff format --check .            -> 139 files already formatted
  ruff check .                     -> All checks passed!
  python -m tools.check_import_boundaries -> domain import boundary: ok
  python -m tools.check_docs       -> documentation structure and local links: ok
  mypy                             -> Success: no issues found in 138 source files
  npm run typecheck (typescript)   -> exit 0
  generate_contracts --check       -> generated contracts: ok
  check_compatibility              -> contract compatibility: ok（纯 additive，baseline 未动）
  pytest                           -> 7597 passed in 155.68s；Total coverage: 83.95%
  npm test (sdk/typescript)        -> pass 9 / fail 0
  EXIT=0
```

mock-server 说明：契约验证使用测试进程管理的本机回环 HTTP server（`tests/contract/test_mock_server.py`，46 例）；受限沙箱禁止 `socket.bind`，最终 `make ci` 在允许 `127.0.0.1` 临时端口的本机执行环境完成。该 server 不访问外网。
