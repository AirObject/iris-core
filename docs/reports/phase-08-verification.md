# Phase 8 验证报告：Profile 与 Graph
2026-09-06 未闭合复审项的后续修复与验证见[联合修复报告](phase-05-06-07-08-review-fixes.md)。

> 归档证据：以下版本、测试数量、耗时与覆盖率是本阶段执行时的历史快照，未在本次文档整理中重跑；不能作为当前发布已通过的证明。当前状态见[阶段索引](../development/README.md)，发布重验见[Phase 14](../development/phase-14-hardening-release.md)。
> 后续闭环：HTTP/进程入口已由 [Phase 10](../development/phase-10-consolidation-reflection.md)交付；旧报告中的应用层/mock 范围只描述当时环境。

> 状态：Completed（实现 + 量化门禁实测 + 两轮对抗性复审修复后全量重跑）
> 日期：2026-09-03
> 基线 commit：`869f04d`（feat: complete phase 7 vector recall）之上的工作区
> 硬件：Apple Silicon（darwin 25.6.0 arm64），本地 SQLite 3.50.4（测试显式 pin，等价部署 pin）
> 验收文档：[phase-08](../development/phase-08-profile-graph.md) · 决策：[ADR-0016](../adr/0016-phase8-profile-graph.md)

本报告只记录实际运行结果。全部数字来自本轮 `make ci` 与专项测试的实测输出。

## 1. 版本与契约

| 项 | 值 |
| --- | --- |
| Core / Python SDK / TypeScript SDK | 0.9.0 |
| Schema | 9（migration `0009_phase8_profile_graph.sql`，min_app=0.9.0，online_safe=true，lock_ms=200，recovery=none） |
| 契约版本 | 1.7.0（additive：capability `recall.graph.v1`、`profile.v1`；路由枚举 +`graph`/`profile`；降级原因码 +10；OpenAPI 新增 `GET /v1/entities/{entity_id}/profile` + `EntityProfileResponse` schema） |
| fixtures | 113 manifest cases（新增 6：graph 完成 envelope、graph 未来原因码 forward、graph 非布尔 retryable invalid、profile 响应 valid/invalid/future） |
| Runtime 兼容窗口 | [8, 9]（`test_window_is_8_to_9` 锁定；0001–0008 与 `869f04d` 逐字节一致，`test_published_bytes_match_head_baseline`） |
| 路由 wire 枚举 | `tasks/recent_context/state/focus/claims/relations/fts/vector/graph/profile`（ADR-0016 §4-5） |
| 新 job kinds | `graph.apply/rebuild/cleanup`、`profile.apply/rebuild/cleanup`（全部 payload v1、真实幂等 handler） |
| 新依赖 | 无（纯 SQLite 行投影；FAISS/embedding 均不涉及） |

Migration checksum（SHA-256，`MigrationRunner` 记录于 `schema_migrations`）：

```
0bd35ddf9fc1966c1d5956d405bcd7ad59aa416022e6030efda961b8f3903ca6  migrations/0009_phase8_profile_graph.sql
```

`test_strict_shapes_and_constraints` 验证 0009 十张 STRICT 表的 CHECK/UNIQUE/FK
约束（状态/主体/分区/边类枚举、scope 维一致性、binding 边唯一允许无 agent、
generation status 枚举、switch_epoch ≥ 1、invalid 行必带 invalidated_us 语义由
状态枚举与 verify 路径共同表达）。

## 2. 量化门禁实测

| 门禁 | 要求 | 实测 | 结果 |
| --- | --- | --- | --- |
| Profile 100% 来源有效 | 每个非空字段 ≥1 仍有效 Source Claim | 200+ 主体（entity+relationship）逐字段自动引用校验：全部来源 claim 存在、status ∈ {active,disputed}、`current_revision == source.revision`、无 tombstone；Canonical 回退视图与投影视图逐字段相等（25 主体抽样 + 全量等价断言） | ✅ 无效字段 = 0 |
| Scope 逐边性质 | ≥200 案例 | 200 生成案例（same-space 可见 / other-space 阻断）：路由自身 `_edge_visible` 逐边断言，阻断 ≥99、放行 ≥100 | ✅ |
| Privacy 逐边性质 | ≥200 案例 | 200 案例半数 restricted（admin 面写入）：逐边断言 restricted 边全部不可见 | ✅ |
| Status 逐边性质 | ≥200 案例 | 200 relationship claim：半数 retract、1/5 dispute——构建只产 active/disputed 边（≤101），retracted 零边 | ✅ |
| Valid Time 逐边性质 | ≥200 案例 | 200 案例（未生效/已过期/有效三态）：未生效与已过期边全部不可见（≥132） | ✅ |
| Tombstone 逐边性质 | ≥200 案例 | 200 目标实体半数 tombstone 且**不重建**：活墓碑账本逐边阻断 ≥99 | ✅ |
| 越权路径返回 0 | 跨 Tenant/Agent/SpaceGroup/Entity | 跨租户：信任门 fail closed（`graph_index_corrupt`/`graph_rebuild_pending`）；跨 Agent：逐边 scope 阻断（owner=other_agent 的边 0 可见）；restricted privacy：路由级 0 候选 | ✅ 越权返回 = 0 |
| 恶意高连接图 10× | 访问/返回不破任一预算 | 深度 2、扇出 16、节点 64、候选 5：枢纽 160 边（10×扇出）+ 160 二跳——BFS 计数器实测 `visited ≤ 64`、`traversed ≤ 32`、`depth ≤ 2`、graph 候选 ≤5；单节点边读取有界（fanout×4 确定性窗口，`test_edge_reads_are_bounded_against_malicious_hubs`） | ✅ |
| Deadline 到达不再扩展 | 到期即停 | 已过期 deadline：collect 入口即抛 `RouteDeadlineExceeded`（零扩展）；5 次单调读后到期的时钟：扩展在预算内停止（per-edge 协作检查） | ✅ |
| 重建一致性 ×3 | 同快照/Builder 全等 | profile 与 graph 各 3 连重建：来源集合/字段集合/边集合/水位/checksum 全等（epoch 递增）；100 次同请求重放 signature 集合大小 = 1 | ✅ |
| Correct 竞态 | ≥50 次且旧值 0 复活 | 50 轮（graph 边 revision 跟随 + profile 字段值翻转）：每轮提交后旧 revision/旧值返回 = 0 | ✅ |
| Forget 竞态 | ≥50 次且旧值 0 复活 | 50 轮 relation forget（graph）+ 50 轮 claim forget（profile）：apply 后旧边/旧字段 = 0 | ✅ |
| Binding/Redirect/SpaceGroup 失效 | 原子失效 | binding revoke 50 轮（边即死）；confirm/revoke/redirect/tombstone/bind/unbind 全部在 Canonical 事务内 enqueue `graph.apply`/`profile.apply`（断言 outbox 行存在） | ✅ |
| 主体墓碑即时生效 | 复审 P2 回归 | 实体 tombstone 提交后（不 apply、不重建）下一次 recall 的 profile 候选 = 0 | ✅ |
| 损坏 fail closed | 每种 ≥20 次 | 5 类 × 20 轮：manifest checksum 篡改（verify 报 corrupt + 置 pending_rebuild + 路由降级）、edge_count 篡改（读门 corrupt）、未知 builder（builder_unknown，不可重试）、悬空指针（corrupt）、tombstone 水位倒退（corrupt）+ 来源覆盖漂移（关系被删后 verify 拒绝） | ✅ 无一服务不可信内容 |
| Restore | 投影标记待重建 | Schema 9 快照 restore 后两投影 `pending_rebuild`、generations/pointers/content 行全清、信任门 `*_rebuild_pending`；手工走私自洽 generation+边（引用不存在的 relation）→ verify fail closed + 读门降级；rebuild 后恢复服务 | ✅ |
| 并发 × 重建 | 无撕裂读 | 12 读线程 × 10 轮 graph+profile 重建交错：零未处理异常、graph 路由每轮要么 completed 要么稳定降级；静默后重建 checksum 确定不变 | ✅ |
| 投影不可用不扩大结果 | Canonical 保真 | never_built ×20：graph/profile 稳定 `*_rebuild_pending`（retryable=true、fallback=`route_skipped_canonical_intact`），claims/relations 路由原样完成 | ✅ |
| as_of | 不用当前投影冒充历史 | ×20：`graph_as_of_unsupported`/`profile_as_of_unsupported`（retryable=false）逐次一致 | ✅ |
| 重复资源单预算槽 | 与其他路由去重 | 同一 claim 经 claims/profile/graph 命中：返回集 `(resource_type, resource_id)` 零重复（RankerV3 去重） | ✅ |
| 降级原因码不复用 | 语义准确 | graph_*/profile_* 十码全部专用（envelope + 契约枚举 + fixtures） | ✅ |

## 3. 测试规模与覆盖率

- Phase 8 新增测试文件与用例数：`tests/unit/test_phase8_domain.py`（27，分区映射/冲突保留/结构化目标抽取/确定性摘要/原因码）、`tests/integration/test_phase8_migration.py`（7）、`test_phase8_graph.py`（129，含逐边性质 5×200、故障注入 5×20、竞态 3×50、预算、重放 100）、`test_phase8_profile.py`（21，来源 200、竞态 50×2、读面过滤）、`test_phase8_recall.py`（46，降级 ×20×2、trace/usage）、`test_phase8_jobs.py`（9）、`test_phase8_concurrency.py`（7，并发/恢复/健康/指标）、`test_phase8_review_round3.py`（11，第三轮复审逐项回归）、`tests/performance/test_graph_profile_recall_latency.py`（1）——共 **258** 个用例。
- Phase 0–7 回归全部保留；按 Phase 8 语义更新的既有断言：schema 窗口 [7,8]→[8,9]（4 处）、head version 8→9（15 处文件）、`current_app_version` 0.8.0→0.9.0、enabled kinds +6（3 处集合并入 phase3/4 handler 覆盖测试）、legacy 降级 helper 补删 Phase 8 十表（5 处）、round2/4/5 快照重放目标 9。
- 全量 `make ci` 终测（第三轮复审修复后，2026-09-03）：**8128 passed / 0 failed**，总覆盖率 **83.9%**（阈值 80%，`coverage report` 实测 83.91%）。Phase 8 核心新模块覆盖率：`domain/profile.py` 98%、`domain/graph.py` 94%、`indexing/graph.py` 81%、`indexing/profile.py` 82%、`storage/projection.py` 80%。

## 4. 性能基线

测量方法：`tests/performance/test_graph_profile_recall_latency.py`；`time.perf_counter()`
包住 `RecallService.recall()` 全程（控制事务、并行路由含 graph+profile、新鲜
Rehydrate、usage 持久化），40 样本 + 3 次 warm-up；数据集 61 实体关系环 + 122
claims（fact+relationship 带结构化目标），candidate cap 默认，token budget 100_000，
`deadline_at` **250 ms**（每请求真实 Deadline）；投影状态 HOT（graph 与 profile
generation 均已构建）；顺序调用（路由内部按生产语义有界并行 max 4）。

| 场景 | 样本 | 实测 p50 / p95 / max | 预算 |
| --- | --- | --- | --- |
| Graph+Profile Hybrid Recall（第三轮修复后，2026-09-03 复测 ×2） | 40 | 23.5–24.3 ms / **29.6–31.8 ms** / 34.0–38.9 ms | 250 ms |

第三轮加固（每边 Canonical 现势性检查 + 端点实体隐私评估，均为有界逐边查询）使
p95 自 23.5 ms 升至约 30 ms（+27%），仍远低于 250 ms 预算的 1/8；对照 Phase 7
同法基线（Structured+FTS+Vector p95 23.6 ms），图路由为换取"失效边不扩展/受限
端点不穿越"的硬保证付出了有界成本。遍历预算（深度 2/扇出 16/节点 64/每节点边读
fanout×4）仍把每请求工作限制在有界窗口内。

## 5. 生命周期与信任门专项（`test_phase8_graph.py` / `test_phase8_profile.py` 全过）

影子重建（derive → checksum → 插入 → 从持久化行重算并比对 → fenced epoch CAS →
旧代 retired）；第二次重建 epoch+1；陈旧 epoch 发布被 CAS 拒绝；retired 保留窗口
（keep=2 + 24h 窗，窗内不删、窗外删且行目录同删——纯 SQLite 行，事务回滚即恢复）；
增量 apply 幂等收敛且 manifest 始终绑定权威行（apply 后 verify 通过）；指针信息
暴露 state/generation/epoch/builder/水位/node/edge 计数；worker 驱动
apply/rebuild（handler 在 fenced 事务内、指标经 after_commit）；未来 payload
版本任务永不被领取（attempt_count=0）；source_revision 被合并推进后旧 worker
完成 CAS rowcount=0；重建发布事务 settle 未租用 backlog（drain-by-construction）。

## 6. 泄漏扫描（canary）

- 指标标签白名单不变（`iris_index_generation{index_kind}`/`iris_index_lag_revisions{index_kind}`，index_kind ∈ {graph, profile}，低基数；无 tenant/entity 标签）。
- Trace：graph/profile 路由 trace 只含路由名/结果/计数/时长——canary 断言候选正文不出现（`test_trace_carries_no_content`）。
- Job payload refs-only：断言 payload 只含 resource_type/resource_id/version（无正文）。
- Profile 响应字段携带 privacy_labels/scope 元数据（供调用方过滤），正文仍只来自 Canonical 行。

## 7. 对抗性复审（三轮，2026-09-03）

**第一轮（自查，3 项修复 + 回归）**：(1) 恶意枢纽的边读取无内存上界——
`edges_for_source` 增加 LIMIT，路由读 fanout×4 确定性窗口
（`test_edge_reads_are_bounded_against_malicious_hubs`）；(2) 读门 subject 计数
加载全表——新增 `subject_count` COUNT 查询；(3) verify 对已 tombstone 的
binding 边判定与 rebuild 口径不一致——`_binding_drafts` 补 binding/identity
墓碑检查（`test_verify_catches_tombstoned_binding_edges`）。

**第二轮（独立复审，10 攻击面：1 P2 + 4 修复 + 记录项）**：
- **[P2] ProfileRoute 未检查主体实体墓碑**：实体 tombstone 提交后、ownerless
  apply 结算前（或 job dead 后永久），其 profile 字段仍可服务。修复：collect
  入口检查 speaker 主体 `resource_tombstones`——下一次请求即不服务
  （`test_tombstoned_subject_serves_no_profile_candidates_immediately`）。
- [P3→修复] `derive_all_subjects` 漏检 space_group 墓碑——补齐。
- [P3→修复] Profile 重建缺翻转前从持久化行重算 manifest 的 belt-and-suspenders
  ——补齐（与 graph 同纪律）。
- [P3→修复] 实体 tombstone 的 apply 不覆盖配对该实体的 relationship 主体
  ——`affected_subjects` 扩展。
- [P3→修复] 读面契约字段缺 privacy/scope 元数据——`EntityProfileResponse`
  扩展并同步双 SDK 校验器与 fixtures。
- 记录不修（ADR-0016 §12）：binding 边在正向遍历不可达（方向性结果）、
  边读取窗口确定性饥饿、LIKE 预过滤仅覆盖 canonical JSON、节点预算耗尽时同边
  不产候选。
- 其余 8 个攻击面（跨主体穿越、Forget/Correct 复活、未发布代伪造新鲜、
  manifest 脱钩、预算旁路、stale worker 发布、清理/回滚、冲突静默覆盖）复审
  判定 CLEAN（代码路径级确认，见 ADR-0016 §11）。

**第三轮（独立复审，4 P1 + 2 P2，全部修复 + 定向复现回归）**：
- **[P1] Graph 未校验端点实体自身隐私标签**：`_edge_visible` 只评估边标签，
  公开边通往 restricted 实体时既可见又可当跳板（定向复现证实）。修复：逐边
  评估两端 `Entity.privacy_labels`（租户级数据域，collect 级缓存受预算约束）；
  实体缺失/TOMBSTONED 一并 fail-closed（`test_public_edge_to_restricted_endpoint_is_invisible`、
  `test_restricted_intermediate_is_not_a_springboard`——restricted 中介后的
  第三跳不可达，admin 正向可见）。
- **[P1] 失效旧边仍可扩展 BFS**：revision/status 终检原先只发生在候选构建，
  目标节点在终检前已入 frontier——correct/revoke/隐私变更后旧边仍通向下一跳
  （最终 Rehydrate 兜底候选，但扩展已发生）。修复：`resource_still_admissible`
  （公开为投影服务方法）在**任何使用边之前**执行；失效边不再扩展也不再占用
  fanout（`test_stale_edge_neither_serves_nor_expands`——dispute 后第二跳不可
  达，apply 落地后恢复）。
- **[P1] verify 的 fail-closed 状态在真实 Worker 路径回滚**：verify 先写
  pending_rebuild 再抛异常，worker 事务整体回滚，状态仍是 ready（定向复现：
  篡改 checksum 后走 worker 提交路径，状态未变）。既有测试把 pytest.raises
  放在事务内，恰好把状态提交了——测试语义与生产相反。修复：verify 失败**返回
  False**（不再抛），状态写入随 cleanup 事务提交、跳过删除；全部断言改为
  提交后断言（`test_graph_cleanup_job_persists_pending_rebuild`、
  `test_profile_cleanup_job_persists_pending_rebuild`——真实 worker 提交路径）。
- **[P1] v1 重建误结算未来版本 apply job**：`settle_unleased_kind` 不过滤
  payload_version——pending 的 graph.apply v2 在 v1 rebuild 后被直接 completed，
  绕过"旧 worker 不领未来 payload"保证。修复：settle 增加
  `payload_version ≤ GRAPH/PROFILE_APPLY_PAYLOAD_VERSION` 过滤
  （`test_rebuild_settles_only_understood_payload_versions`——v1 结算、
  v2 保持 pending，graph/profile 双验）。
- **[P2] Relationship Profile 全量重建与增量/回退派生不一致**：`_subject_claims`
  收集两端实体的**全部** relationship Claim（不检查 target 是否为另一端），
  Bob|Carol 主体在回退派生中混入 Bob→Dave，read_profile 因 checksum 不等转
  回退并在 Bob|Carol 下返回第三方关系（定向复现证实）。修复：增量/回退派生
  与重建枚举同规则——只收结构化 target 等于配对另一端的 Claim
  （`test_relationship_subject_contains_only_its_pair`——投影路径直接服务、
  字段只含配对 Claim、另一配对独立保有）。
- **[P2] verify 与文档所称来源校验不符**：只查 sources 非空，不验证 Claim
  存在/status/当前修订/墓碑，也不查缺失的新 Canonical 字段；且既有
  "Canonical fallback comparison" 测试实际把 canonical 赋 None 后删除（断言
  空转）。修复：结构校验恒跑 + **静默管线**（apply 及其生产者事件全部
  unsettled 为零）时执行与新鲜 Canonical 派生的精确等价比对（来源/主体不缺
  不少/checksum 相等；在途工作允许暂时落后，避免把时滞错报损坏）
  （`test_verify_tolerates_pipeline_lag`、`test_graph_verify_flags_dead_apply_drift`、
  `test_profile_verify_flags_dead_apply_drift`、
  `test_profile_verify_flags_invalid_source_after_drain`）；伪对比测试改为真实
  等价断言（投影视图 == 同快照 Canonical 派生，25 主体采样）。
- 复核补充：migration 0009 SHA-256 实测 `0bd35ddf…03ca6` 一致；`git diff
  --check` 干净；修复后定向全过（单文件运行受全局 80% 覆盖率门槛影响属预期）。

复审后全量门禁重跑通过（§3、§9 数字为修复后终测）。

## 8. 已知限制

1. **Binding 边正向不可达**（ADR-0016 §12）：identity→entity 方向边被投影/验证/
   计数，但 speaker 锚定的 BFS 从实体节点出发永远走不到；反向遍历待真实需求。
2. **边读取窗口饥饿**：枢纽节点前 fanout×4 条边全部对该请求不可见时，窗口外
   可见边被确定性跳过（availability，非泄漏）。
3. **min-watermark 保守降级**：纯非投影流量推进 Agent 水印后需重建才恢复
   min-watermark 服务（Vector 同族限制）。
4. **单事务重建持 Writer Gate**（FTS 同法）：大租户重建应安排安静窗口。
5. **`claims_targeting_entity` LIKE 预过滤**只匹配 canonical JSON 两种空白形态
   （本仓库写入路径恒为 canonical；fail-closed 方向）。
6. **节点预算耗尽时同边不产候选**（确定性 completeness quirk）。
7. **默认预算为嵌入式保守值**（深度 2/扇出 16/节点 64/候选 12）；放大需独立
   容量评估。
8. **无 Recall/投影结果缓存**（仓库现状）：失效边界 = Canonical 事务内 Outbox
   事件（ADR-0014 §6 冻结未来键语义；ADR-0016 §7 记录不适用）。
9. **历史缺口已关闭**：ADR-0017 §8 统一 canonical relations 与 Graph 的端点隐私，回归为 `test_relation_endpoint_privacy.py`。
10. **历史缺口已关闭**：真实 HTTP 传输由 [Phase 10](../development/phase-10-consolidation-reflection.md)交付；本报告的原始测试仍是当时应用层/mock 范围。
## 9. `make ci` 摘要

（任何失败都会使本节重写。）

- `format-check` / `lint`（ruff + import boundaries + docs）：通过
- `typecheck`（mypy 180 files + tsc）：通过
- `contracts-check`（generator --check + compatibility）：通过（纯 additive，baseline 未动）
- `pytest`（全量，`--cov-fail-under=80`）：**8128 passed / 0 failed**（436.09 s），总覆盖率 **83.9%**
- `sdk-test`（tsc + node --test）：**12 pass / 0 fail**

最终一轮（2026-09-03，三轮对抗性复审修复后，基线 869f04d 之上的工作区）实测输出摘要：

```
make ci
  ruff format --check .            -> 181 files already formatted
  ruff check .                     -> All checks passed!
  python -m tools.check_import_boundaries -> domain import boundary: ok
  python -m tools.check_docs       -> documentation structure and local links: ok
  mypy                             -> Success: no issues found in 180 source files
  npm run typecheck (typescript)   -> exit 0
  generate_contracts --check       -> generated contracts: ok
  check_compatibility              -> contract compatibility: ok
  pytest                           -> 8128 passed in 436.09s; Total coverage: 83.91%
  npm test (sdk/typescript)        -> pass 12 / fail 0
  EXIT=0
```

工作区为 869f04d 之上 **76 个变更文件**（51 modified + 25 untracked，终测后按
`git status --porcelain` 实点）：Phase 8 主体 75 文件 + 第三轮复审新增
`tests/integration/test_phase8_review_round3.py`，其余第三轮修改（src 8、tests 3、
docs 3）均落在已计入文件内。未提交、未推送，供人工复审。

## 原阶段验收目标

下列门槛从已归档阶段计划移入，保留未被实测证明的要求。它们是当时的验收目标，不能从本报告 Passed/Completed 标签推断逐项均已完成；是否达到须与前文的样本、测试与限制核对。尚未闭合项由 Phase 14 的发布矩阵承接。

- Profile 的每个非空字段必须有至少一个仍有效的 Source Claim；对 100% 字段执行自动引用校验，并抽样比较 Canonical 回退内容与冲突状态。✅
  （200+ 主体、100% 字段逐来源校验 + Canonical 回退等价断言）
- Scope、Privacy、Status、Valid Time、Tombstone 的逐边过滤性质每项至少运行 200 个生成图案例；跨 Tenant/Agent/SpaceGroup/Entity 的未授权路径返回数必须为 0。✅
  （每性质 200 案例；跨租户信任门 fail closed、跨 agent/privacy 零返回）
- 使用至少为配置深度、扇出和节点上限 10 倍的高连接图压测，实际访问与返回不得突破任一限制，Deadline 到达后不得继续扩展。✅
  （160 扇出 × 深度 2 恶意图；BFS 计数器 ≤ 全部预算；到期时钟证明扩展停止）
- 对同一 Canonical Snapshot 和 Builder Version 连续全量重建 3 次，Profile 来源集合、Graph 边集合、Watermark 和 Manifest Checksum 一致。✅
  （profile/graph 各 3 连重建 checksum/集合全等 + 100 次请求重放 signature 唯一）
- Binding/Redirect/SpaceGroup/Correct/Forget 与查询竞态各至少重复 50 次；变更提交后旧字段/边/Cache 的当前返回数必须为 0，Route 故障准确报告 Canonical 回退。✅
  （correct/forget/binding-revoke 各 50 轮 + redirect/tombstone/SpaceGroup 失效）
