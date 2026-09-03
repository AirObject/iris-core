# Phase 7 验证报告：Vector Recall

> 状态：Completed（实现 + 量化门禁实测 + 两轮对抗性复审修复后全量重跑）
> 日期：2026-09-03
> 基线 commit：`179b6a0`（feat: complete phase 6 fts recall）之上的工作区
> 硬件：Apple Silicon（darwin 25.6.0 arm64），本地 SQLite 3.50.4（测试显式 pin，等价部署 pin），faiss-cpu 1.15.0 + numpy 2.5.2
> 验收文档：[phase-07](../development/phase-07-vector-recall.md) · 决策：[ADR-0015](../adr/0015-phase7-vector-recall.md)

本报告只记录实际运行结果。全部数字来自本轮 `make ci` 与专项测试的实测输出。

## 1. 版本与契约

| 项 | 值 |
| --- | --- |
| Core / Python SDK / TypeScript SDK | 0.8.0 |
| Schema | 8（migration `0008_phase7_vector_recall.sql`；含 `vector_id_map.incorporated_generation` 成员戳列，见 §7 第二轮复审） |
| 契约版本 | 1.6.0（additive：capability `recall.vector.v1`、`embedding.v1`；路由枚举 +`vector`；降级原因码 +7；错误码 +`provider_unavailable`） |
| fixtures | 107 manifest cases（valid 56 / invalid 38 / forward 13，新增 3：vector 完成 envelope、未来路由 forward、非布尔 retryable invalid） |
| Runtime 兼容窗口 | [7, 8]（`test_window_is_7_to_8` 锁定；0001–0007 与 `179b6a0` 逐字节一致） |
| 路由 wire 枚举 | `tasks/recent_context/state/focus/claims/relations/fts/vector`（ADR-0015 §6） |
| 新依赖 | `faiss-cpu>=1.15,<2`、`numpy>=2.1,<3`（导入惰性探测，缺失 ⇒ Vector Capability 降级） |

Migration checksum（SHA-256，`MigrationRunner` 记录于 `schema_migrations`）：

```
bc4b9050be8b2a4991a4f1a0b1973780cde35132e7e98b83fa401d2ed6ed4512  migrations/0008_phase7_vector_recall.sql
```

`test_published_bytes_match_head_baseline` 锁定 0001–0007 与 `git show 179b6a0:migrations/…`
逐字节一致；`test_strict_shapes_and_constraints` 验证 0008 五张 STRICT 表的 CHECK/UNIQUE
约束（surrogate 上下界、status 枚举、delta op 枚举、invalid 必带 invalidated_us、
`incorporated_generation` 为可空 TEXT）。

## 2. 量化门禁实测

| 门禁 | 要求 | 实测 | 结果 |
| --- | --- | --- | --- |
| Surrogate 唯一性/边界 | ≥200 固定种子案例 | 分配唯一性 200 案例（每例 1–5 个 ID，单调、零碰撞、[1, 2^62]）；per-resource 映射 200 案例（双唯一约束下零碰撞）；失效/清理 200 资源（100 invalidate + 100 物理删除）；分配器上限拒绝（2^62 边界） | ✅ 碰撞/错误复用 = 0 |
| 重建前后 surrogate 稳定 | 一致 | 30 资源两代重建 + Restore 往返（id map 保留）前后 surrogate 集合恒等 | ✅ |
| 损坏场景 fail-closed | 每种 ≥20 次 | 7 类 × 20 轮：manifest 缺失、checksum 漂移、id-map 截断（checksum 重算后计数/ID 集仍拒绝）、维度漂移、未知 builder、snapshot 乱序、指针悬空/目录缺失——全部 `vector_index_corrupt`/`vector_builder_unknown`，无一替换可信 Handle | ✅ |
| 模型/维度切换 | ≥1,000 已知 Revision 抽样 | 1,000 claims 从 model `test-embedding`(dim 8) 切到 `test-embedding-v2`(dim 16)：新代 manifest 空间正确、旧代在新配置下 `vector_space_mismatch` 拒绝服务、surrogate 与 revision 全等、检索命中 revision 与 Canonical 一致 | ✅ |
| 并发 Search × Build/Swap | 50 并发 × ≥10 轮 | 50 线程持续搜索 × 10 轮完整 build/validate/swap 交错：零未处理异常、零 use-after-close、所有命中可 Rehydrate；另 4 线程 × 50 轮双重 release 拒绝；临时目录守护（全部服务目录都在 generations/ 下且 manifest 完整） | ✅ |
| 删除竞态 | 提交后旧内容返回 0 | 20 轮：搜索命中 → Forget 提交 → 新鲜搜索 0 复活（id map 门 + 墓碑最后防线）；canary 内容 0 泄漏 | ✅ |
| 降级 envelope 稳定 | 各 ≥20 次一致 | `vector_as_of_unsupported`(20)、`vector_rebuild_pending`(20)、`vector_unavailable`(20, provider 注入)、`vector_generation_stale`(20, delta lag) 逐次原因码/retryable/fallback 一致 | ✅ |
| 相同输入重放 | 100 次完全一致 | 100 次重放 signature（顺序/分数/冲突标记/completed/degraded/partial/dropped）集合大小 = 1 | ✅ |
| Usage 子集 | 链成立 + 伪造拒绝 | vector 参与的 returned 集回显 + `model_visible ⊆ host_selected ⊆ returned` 接受；破坏链拒绝 | ✅ |
| minimum_watermark | 不冒充 read-your-writes | 请求 W > 生成覆盖 ⇒ `vector_generation_stale`；W == 覆盖 ⇒ 服务；dead-letter 模拟（delta 被删）下 W+1 仍拒绝且旧 revision 不服务 | ✅ |
| Backup→Restore | vector 标记待重建 | Schema 8 快照 restore 后 `vector_projection_state='pending_rebuild'`、代/指针/delta 清空、id map 与 surrogate 计数保留；恢复目录内手工放入陈旧 vector/ 仍拒绝服务直到重建；备份目录夹带 vector/ 结构性拒绝 | ✅ |

## 3. 测试规模与覆盖率

- Phase 7 新增测试文件与用例数：`tests/unit/test_phase7_domain.py`（28，含 RankerV3 去重组）、`tests/integration/test_phase7_migration.py`（9）、`test_phase7_provider.py`（25，含 deadline 下传/半开单探针组）、`test_phase7_idmap.py`（8 个测试函数，内含 200 案例性质组）、`test_phase7_vector.py`（28）、`test_phase7_recall.py`（11）、`test_phase7_concurrency.py`（7）、`test_phase7_review.py`（24，两轮对抗性复审回归）、`tests/performance/test_hybrid_recall_latency.py`（2）——共 **142** 个用例。
- Phase 0–6 回归全部保留；按 Phase 7 语义更新的既有断言：schema 窗口 [6,7]→[7,8]（3 处）、head version 7→8（9 处文件）、`current_app_version` 0.7.0→0.8.0、enabled kinds +`vector.apply/rebuild/cleanup`（4 处）、legacy round2 快照 downgrade helper 补删 Phase 7 表、restore 后 Schema 6 目标先经启动迁移再服务（窗口不再含 6，删除账本回放 Store 显式豁免窗口校验——restore 本身仍不运行 migration runner）。第二轮复审后按修正语义更新的断言：混合去重（同一资源恰占一个预算槽）、cleanup 目录仅在提交后由钩子删除、性能测试 Deadline 由 250 s 更正为真实 250 ms。
- 全量 `make ci` 终测：**7868 passed / 0 failed**（294.70 s，含覆盖率收集），总覆盖率 **83.47%**（阈值 80%）。Phase 7 核心新模块覆盖率：`domain/vector.py` 94%、`storage/vector.py` 91%、`providers/embedding.py` 84%、`indexing/faiss.py` 86%、`indexing/vector.py` 81%。

## 4. 性能基线

测量方法：`tests/performance/test_hybrid_recall_latency.py`；`time.perf_counter()` 包住
`RecallService.recall()` 全程（控制事务、并行路由含 FTS+Vector、新鲜 Rehydrate、usage 持久化），40 样本 + 3 次 warm-up；数据集 60 claims（identity/fact 混合、约 40–120 字符）+ 20 热窗观察，candidate cap 20，token budget 100_000，FAISS dim 32，`deadline_at` **250 ms**（每请求真实 Deadline；初版测试误写为 250 s，第二轮复审指出后更正并重测——下列数字即在真实 250 ms 预算下测得）；索引状态 HOT（FTS 与 FAISS Handle 均已加载）；顺序调用（路由内部按生产语义有界并行 max 4）。

| 场景 | 样本 | 实测 p50 / p95 / max | 预算 |
| --- | --- | --- | --- |
| Hybrid Recall（Structured+FTS+Vector） | 40 | 20.5 ms / **23.6 ms** / 26.2 ms | 250 ms |

分阶段（同数据集实测中位数）：Query Embed 0.03 ms（确定性 Provider，本地无网络）、
FAISS Search（含信任门读事务）1.30 ms、其余（控制事务 + 并行路由 + 新鲜 Rehydrate + 排序/裁剪 + usage 写）≈ 18 ms。生产 HTTP Provider 下 Query Embed 由 Provider 延迟主导，受 Provider 超时与路由子 Deadline 双重约束（socket 级 min(配置, 剩余预算)，见 §7 第二轮复审）。

## 5. 生命周期与信任门专项（`test_phase7_vector.py` 全过）

六阶段构建（collect→allocate→embed+build→revalidate→verify+rename→switch）；manifest 冻结字段全量断言；第二次重建 CAS 换指针（epoch+1）且旧代 retired；发布前 Canonical 精确内容比对（变动 ⇒ ConflictError 重试）；epoch fencing 拒绝陈旧发布；重启只经指针加载已验证代；损坏代不替换上一可信代且加载失败保持 `vector_index_corrupt` 直到重建；孤儿目录与退休代清理受 24h 回退窗口约束（keep=2 + 窗口双重条件）；Handle 引用计数/use-after-close/double-close 拒绝；COW 换代期间在途 Handle 保持可搜；worker 驱动 rebuild/apply（provider 调用在 fenced 事务外）。

## 6. 泄漏扫描（canary）

- Provider 统计字段白名单断言（`test_logged_fields_never_contain_content_canary`）：`TOPSECRET-CANARY-CONTENT` 不出现在任何已记录字段；字段集合 ⊆ {id_hash, count, status, duration_ms, model, batch}。
- Trace：`test_trace_reports_vector_route_without_content`（canary 不出现在 trace 序列化中）。
- 搜索命中仅携带 ResourceRef/Revision/分数/安全元数据（无正文）——正文一律来自路由事务内的 Canonical 行并经最终 Rehydrate。

## 7. 对抗性自审与独立复审（2026-09-03）

实现期自审发现并修复 4 项（均已落回归）：vector 搜索缺墓碑最后防线（并发测试实际捕获）、
faiss SWIG 双引用 GC 陷阱（内部索引/read_index 拥有权）、id-map snapshot 字典序 vs 数值序、
Manager 对已 seal Handle 的分发。独立对抗性复审（15 类攻击面）确认 6 类 clean
（Forget 复活、Handle 引用计数、空间混用、SQL 注入、多进程指针、fencing CAS），报告
1 项 P1 + 4 项 P2 + 10 项 P3，处置如下：

1. **[P1] 跨租户清扫删除**：`sweep_orphans` 保留集原为单租户口径，会把其他租户正在服务的代目录当作孤儿删除。修复：保留集全局化（`all_generation_ids` + `all_pointer_generation_ids`），`tenant_id` 仅作任务归属。回归：`test_sweep_is_global_multitenant_no_cross_deletion`。
2. **[P2] 暂存文件未 fsync**：断电后指针可能指向未落盘字节。修复：`_build_files` 对四个文件逐一 fsync 并 fsync 暂存目录后再 rename。
3. **[P2] false-stale**：迟到的 `vector.apply` 以"apply 时"水位判断覆盖，会在代已并入该变更后写入冗余 delta，使带 `minimum_watermark` 的请求永久 stale。修复：改为**状态判定**（id map 行 revision == 观测状态且 active ⇒ 跳过）+ 发布事务内 `id_map_invalidate_tombstoned` 使墓碑行可信。回归：`test_late_apply_after_switch_records_no_spurious_delta`。
4. **[P2] 重建饥饿**：collect 与 revalidate 之间新建的资源无 surrogate ⇒ switch 精确比对必然 abort，持续写入下重建永不收敛。修复：stage-4 为新资源补分配（短写事务）并纳入本代，abort 窗口收窄到文件构建期间。回归：`test_new_resource_during_build_joins_generation`。
5. **[P2→已接受并记录] switch 事务全量枚举**：精确内容比对需要一次全租户枚举（N+1 revision 读取）在 `BEGIN IMMEDIATE` 内执行；大租户应安排安静窗口（与 FTS 重建持有 Writer Gate 的既有已知限制同类，且严格更短）。**未改**，记入 §8。
6. **[P3×6 已修复]**：verify/use TOCTOU（`verify_directory` 返回已验证 index 对象）；冷加载惊群（per-generation single-flight）；tmp/ 目录泄漏（sweep 扩展）；全局状态行文档失实（更正为全局口径 + restore 全库语义）；`delta_clear_covered` 死代码（移除，语义统一为"精确内容等价 ⇒ 全清 + 状态跳过"，文档同步）；HTTPError retryable 恒真（4xx 不可重试）与 localhost 前缀绕过（parsed host 精确匹配）。
7. **[P3 已修复] 无主 vector.apply 不可见**：freshness gate 计入 ownerless 未结算任务（fail-stale）。
8. **[P3 已修复] 限流器极端配置锁内长眠**：`max_qps` 下限 0.1。
9. **[P3 记录不修] envelope retryable 粒度**：`vector_index_corrupt` 在不同成因下 retryable 不同，但 trace 只携带原因码；envelope 按原因码推导（True），个别成因（租户错位代）与内部标记不一致。记入 §8。
10. **[P3 记录不修] Schema 7 库 health 误降级**：已修复（`sqlite3.OperationalError` ⇒ `never_built` 口径）。

复审后全量门禁重跑通过（§3、§9 数字为修复后终测）。

### 7.1 第二轮独立复审（2026-09-03，3×P1 + 5×P2 + 2×P3，全部处置）

复审方在独立临时目录复现了两个 P1（未发布构建的 false-fresh 最小复现、manifest+checksums
重写后验证仍接受），并指出混合排序未真正去重。逐项处置（每项均有回归测试）：

1. **[P1] 未发布构建伪造新鲜并静默漏召回**：`prepare_generation` 阶段 2 刷新 id map 至新
   revision，若发布失败（rename 后崩溃/精确内容冲突/fencing 拒绝），旧代仍服务 revision 1
   而 id map 已是 revision 2；迟到 `vector.apply` 凭"id map revision 相等"跳过 delta ⇒
   lag=0 假新鲜，且新旧代都召回不到该资源。修复：`vector_id_map.incorporated_generation`
   **成员戳**——只有发布事务能给 survivor 行盖章（与指针 CAS 原子提交；任何 upsert 清空
   戳）；apply 跳过条件升级为「revision 相等 ∧ 戳 == 当前指针代」。未发布构建的戳永不相等
   ⇒ 记 delta（lag>0，minimum_watermark 请求正确降级）⇒ 重建收敛。回归：
   `test_unpublished_build_cannot_fake_freshness`（含复审原始场景 + 发布后真跳过）。
2. **[P1] Generation 信任链未绑定 SQLite 权威记录**：`verify_directory` 只验证文件与
   checksums.txt 自洽（后者本身可重写），manifest 的 `index_hash` 未与真实索引摘要比对、
   未与 DB 行的三个 checksum/数量/水位对照，畸形字段抛裸 ValueError。修复：manifest 成为
   文件摘要权威（index/snapshot 摘要重算比对）；加载路径（`handle_for(pointer,
   generation=DB 行)`）绑定 SQLite 权威行（checksum/数量/空间身份精确相等，水位单调不
   回退）；全部畸形输入折叠为稳定 `vector_index_corrupt`。回归：
   `test_tampered_content_hash_rejected_against_db_row`（复审复现场景）、
   `test_rewritten_checksums_cannot_vouch_for_a_modified_index`、
   `test_malformed_manifest_fields_degrade_with_stable_reason`（5 类畸形 + 坏 JSON）。
3. **[P1] Hybrid Ranker 未真正去重**：v3 仅委托 v2（冗余降分不删除），同一资源经
   claims/fts/vector 三路由命中会三份并存、各占预算，与 ADR-0015"不重复占用预算"和源码
   注释"nothing is returned twice"矛盾。修复：RankerV3 在 v2 打分/标记后**按
   `(resource_type, resource_id)` 去重**——稳定序最优实例胜出（携带其 v2 冲突/冗余判定），
   重复项在预算分配前丢弃；不同资源的冲突组永不被合并；v2 类保持 Phase 6 语义不动。
   回归：单元组 `TestRankerV3Dedupe`（5 例：单实例胜出/单预算槽/冲突组不合并/置换确定性/
   v2 不变）+ 集成 `test_vector_and_fts_hits_deduplicate_to_one_budget_slot`。
4. **[P2] Provider deadline 未真下传 + 半开无单探针**：路由不传剩余 deadline，HTTP 调用
   固定用配置超时（被放弃的线程继续执行、连续超时积累）；冷却期满后所有并发调用同时涌向
   恢复中的 Provider。修复：`embed_batch(deadline_monotonic_us=…)`——每次 transport 调用
   超时 = min(配置, 剩余)（socket 级），剩余 ≤0 fail-fast；半开预留单探针位（`record()`
   释放，校验失败路径也释放且不计熔断），并发调用 `circuit_open` 快速失败。回归：
   `TestDeadlinePropagationAndHalfOpenProbe`（3 例，含传输层零调用断言与并发单探针）。
5. **[P2] 启动 Probe/required readiness 未接线**：`probe_provider` 无生产调用方，
   `capability_available` 只查 FAISS，health 只看库状态 ⇒ Provider 宕机时
   `vector_required=true` 仍报 ready。修复：`ValidatingEmbeddingProvider.probe()` +
   `VectorProjectionService.capability_available()`（faiss ∧ probe ∧ 熔断未开；probe 成功
   缓存、失败每冷却至多重试一次）；`HealthService(vector_capability=…)`——required 且
   能力不可用 ⇒ not_ready（reason `vector_capability_unavailable`），可选部署仅报告。回归：
   `test_required_vector_readiness_fails_when_provider_is_down`。
6. **[P2] 清理破坏 fencing/引用生命周期**：`cleanup_in_tx` 在 outbox fenced 写事务内直接
   `rmtree`——之后 completion CAS 失败时 SQLite 回滚行、目录无法恢复；删除前未检查旧
   Handle。修复：`JobCommit` 可选 `after_commit` 钩子（仅在业务写 + completion CAS 提交后
   运行；失败记指标不复活任务）；cleanup 事务内只删行，钩子内 `remove_generation_dirs`
   （cleanup 返回的代目录直删，跳过本进程已加载代）+ `sweep_filesystem`（全局保留集 +
   年龄窗口，本进程已加载代永不清；跨进程在途 Handle 由 POSIX unlink 语义 + faiss 全量
   内存加载保证安全）。回归：`test_cleanup_directories_removed_only_after_commit`
   （强制回滚 ⇒ 行与目录俱在；提交 + 钩子 ⇒ 目录删除且幂等）。
7. **[P2] 固定 4× overfetch 跨 Agent 假阴性**：租户级索引先取 4×limit 再按 Agent 过滤，
   其他 Agent 的高分向量可把合法候选全部挤出。修复：搜索迭代扩张（4×limit 起步、不足则
   4× 逐轮放大直至足够或耗尽索引；精确 FlatIP 下最终结果与全量 top-limit 一致、确定性
   不变）。回归：`test_cross_agent_crowding_does_not_starve_own_candidates`（40 条异 Agent
   洪泛 + 5 条自有，limit=5 全数召回）。
8. **[P2] 四个 Phase 7 指标无发射点**：`iris_index_generation`/`iris_index_lag_revisions`/
   `iris_provider_requests_total`/`iris_provider_duration_seconds` 只有声明。修复：projection
   与 provider 接受指标 Protocol 注入——generation 指标在发布提交后按指针 epoch 发射
   （admin 路径 + rebuild `after_commit` 钩子；fenced/回滚发布不动指标），lag 在每次可信
   搜索的信任门发射，provider 请求/时长每次调用（含失败）发射。回归：
   `test_phase7_metrics_emitted_from_production_paths`。
9. **[P3] 报告失实**：初版交付声明的文件计数有误（复审时点实为 67 = 46 modified +
   21 untracked；本轮修复后为 68，见 §9 后记）；性能测试 Deadline 250 s→250 ms 更正
   并重测（§4 数字为真实预算下实测）。

## 8. 已知限制

1. **Deterministic Provider 无语义质量**：测试 Provider 按文本哈希生成向量，验证的是生命周期/门禁/并发正确性，不是检索相关性；生产语义由 `HttpEmbeddingProvider`（OpenAI 兼容端点）承担，未在本 CI 内做真实网络调用（回环 mock server 除外）。
2. **Switch 事务持有一次全租户枚举**（复审 P2-5）：大租户（数万 indexable 资源）重建应安排安静窗口；与 FTS 重建持 Writer Gate 的既有限制同类且窗口更短。后续可用批量 revision 读取或聚合指纹优化。
3. **envelope retryable 按原因码推导**（复审 P3-13）：`vector_index_corrupt` 统一报告 retryable=true，个别成因内部标记为 false。
4. **FAISS 文件不进备份**：Restore 后必须 admin rebuild（`vector_rebuild_pending`）；备份携带 vector/ 目录会被结构性拒绝（验证过）。
5. **vector_projection_state 为全局单行**（复审 P3-9）：多租户共享一个部署时状态与 surrogate 计数全局；restore 是整库操作，全局重置语义正确，但单租户状态视图不存在。
6. **`/v1/search` 未加向量面**：向量语义只在 Recall 路由（独立 HTTP 端点无架构依据）；Search 面的 vector 支持留待后续评估。
7. **Rebuild 期间新建资源在文件构建窗口内落地仍会 abort 一次**（饥饿修复收窄但未消除窗口）；catch_up=latest + worker 重试收敛。
8. **合同/mock 面**：mock server capabilities 已同步至 Schema 8 全集（补齐 Phase 6 即有的缺口）。
9. **真孤儿目录按 mtime 年龄窗清除（第二轮复审引入）**：cleanup 删除行的目录由提交后钩子即时清除；但**无行孤儿**（崩溃构建残留）仍须老过回退窗口（默认 24h）才被清扫——这是对并发构建 rename 窗口的保守保护，可配 `retirement_window_us` 调低。
10. **能力探针的缓存粒度**：probe 成功后永久缓存（运行时由熔断器把门），失败每 breaker 冷却重试至多一次；`vector_capability` 未接线（未传 callable）的部署 readiness 不感知 Provider 状态——这是部署装配项，ADR-0015 §11 已写明接线要求。

## 9. `make ci` 摘要

（任何失败都会使本节重写。）

- `format-check` / `lint`（ruff + import boundaries + docs）：通过
- `typecheck`（mypy 165 files + tsc）：通过
- `contracts-check`（generator --check + compatibility）：通过（纯 additive，baseline 未动）
- `pytest`（全量，`--cov-fail-under=80`）：**7868 passed / 0 failed**，总覆盖率 **83.47%**
- `sdk-test`（tsc + node --test）：**12 pass / 0 fail**

最终一轮（2026-09-03，两轮对抗性复审修复后，基线 179b6a0 之上的工作区）实测输出摘要：

```
make ci
  ruff format --check .            -> 166 files already formatted
  ruff check .                     -> All checks passed!
  python -m tools.check_import_boundaries -> domain import boundary: ok
  python -m tools.check_docs       -> documentation structure and local links: ok
  mypy                             -> Success: no issues found in 165 source files
  npm run typecheck (typescript)   -> exit 0
  generate_contracts --check       -> generated contracts: ok
  check_compatibility              -> contract compatibility: ok
  pytest                           -> 7868 passed in 294.70s; Total coverage: 83.47%
  npm test (sdk/typescript)        -> pass 12 / fail 0
  EXIT=0
```

后记（复审 P3 更正）：工作区为 179b6a0 之上 **68 个变更文件**（终测后按
`git status --porcelain` 实点），未提交、未推送，供人工复审。
