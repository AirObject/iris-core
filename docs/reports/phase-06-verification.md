# Phase 6 验证报告：FTS Recall

> 状态：Completed（实现 + 量化门禁实测 + 对抗性复审三轮修复后全量重跑）
> 日期：2026-09-02
> 基线 commit：`b7bbad5`（feat: complete phase 5 long term memory）之上的工作区
> 硬件：Apple Silicon（darwin 25.6.0 arm64），本地 SQLite 3.50.4（测试显式 pin，等价部署 pin）
> 验收文档：[phase-06](../development/phase-06-fts-recall.md) · 决策：[ADR-0014](../adr/0014-phase6-fts-recall.md)

本报告只记录实际运行结果。全部数字来自本轮 `make ci` 与专项脚本的实测输出。

## 1. 版本与契约

| 项 | 值 |
| --- | --- |
| Core / Python SDK / TypeScript SDK | 0.7.0 |
| Schema | 7（migration `0007_phase6_fts_recall.sql`） |
| 契约版本 | 1.5.0（additive：3 capability、3 路径、9 schema、3 错误码） |
| fixtures | 104 manifest cases（valid 55 / invalid 37 / forward 12，新增 19） |
| Runtime 兼容窗口 | [6, 7]（`test_window_is_6_to_7` 锁定） |
| 路由 wire 枚举 | `tasks/recent_context/state/focus/claims/relations/fts`（ADR-0014 §3） |

Migration checksum（SHA-256，`MigrationRunner` 记录于 `schema_migrations`）：

```
92e37560eb215e3093a068cf68f6b40002d9e575fc22407689efe5a357236ce4  migrations/0007_phase6_fts_recall.sql
```

（第三轮复审调整了工作区内的 0007：移除第二轮引入的 `fts_generation_agents`，`recall_requests` 增加请求指纹/可重放响应/资源引用列——0007 未发布，0001–0006 字节锁定不受影响。）

0001–0006 与 `git show b7bbad5:migrations/…` **逐字节一致**（`test_published_bytes_match_head_baseline` 6 例参数化 + Phase 2/3/4/5 既有锁定测试）。FTS5 虚拟表**不在**迁移内：`test_migration_does_not_create_the_virtual_table` 断言 Schema 7 空库只有元数据表；`fts_index` 由投影服务在能力探测后幂等创建（ADR-0014 §1）。

## 2. 量化门禁实测

| 门禁 | 要求 | 实测 | 结果 |
| --- | --- | --- | --- |
| 硬过滤性质案例 | 每维度 ≥200 固定种子 | 11 维度（tenant/agent/space_group/space/session/privacy/status/time/tombstone/as_of/revision）× 200 案例 = **2200**，全部"violating 排除 + control 命中"双断言通过（48s） | ✅ |
| Usage 子集性质 | ≥200 生成案例 | 合法子集链 200 接受 + 破链 200 拒绝 + 伪造 id 200 拒绝 | ✅ |
| 重复 report 幂等 | 100 次仅一次累计 | 100 次重复：1 次 `created=True` + 99 次 replay，单逻辑行 | ✅ |
| 跨 tenant/请求伪造 | 成功数 = 0 | 跨请求 id 200 案例 0 成功；跨 tenant 请求锚定拒绝；伪造/破链/回显不完整全部 `invalid_request` | ✅ |
| FTS 落后/损坏/超时/最低水位/禁 partial | 各 ≥20 次稳定 envelope | 每场景 20 次：`fts_generation_stale`、`route_deadline_exceeded`、`minimum_watermark_unavailable`、`not_ready`（禁 partial）、`deadline_exceeded`（全超时）逐次一致 | ✅ |
| 相同输入重放 | 100 次完全一致 | 100 次重放 signature（顺序/分数/冲突标记/裁剪/missing components）集合大小 = 1 | ✅ |
| 删除竞态 | 提交后旧内容返回 0 | 协调屏障 20 轮：FTS 命中 → 收集期间并发 Forget 提交 → 新鲜 Rehydrate 全部剔除（`dropped_by_rehydrate ≥ 1`，正文 0 泄漏）；另含 route 事务内 Forget 变体 | ✅ |
| Backup→隔离 Restore | FTS 待重建标记 | restore 后 Schema 7 + `fts_projection_state='pending_rebuild'`；Schema 6 快照经启动迁移到 7（`never_built`），0001–0006 字节不变 | ✅ |
| Usage 不触碰 Confidence | — | 20 次报告前后 claims 行（confidence/importance/accessibility）逐字节相同 | ✅ |

## 3. 测试规模与覆盖率

- Phase 6 新增测试文件：`tests/integration/test_phase6_fts.py`（15）、`test_phase6_migration.py`（13）、`test_phase6_recall.py`（29 个测试函数，其中 11 个维度门各执行 200 个性质案例）、`test_phase6_usage.py`（11）、`test_phase6_review.py`（8，对抗性复审一轮回归）、`test_phase6_review_round2.py`（20，发布前外部复审回归；第三轮将其 2 例 staleness 用例改写为未结算 backlog 口径）、`test_phase6_review_round3.py`（15，第三轮复审回归）、`tests/performance/test_recall_latency.py`（2）；契约面新增 mock server 6 例 + TS wire 3 例 + Python SDK fixtures 清单全覆盖。
- Phase 0–5 回归：全部保留；按 Phase 6 语义更新的既有断言——`test_recall.py` 稳定排序 tie-breaker（focus 优先级提升，ADR-0014 §5）、最低水位/全超时错误细化为 `NotReadyError` 子类（保留内部契约）、schema 计数 6→7/窗口 [5,6]→[6,7]/`current_app_version` 0.6.0→0.7.0、enabled kinds 集合 +`fts.apply/rebuild/cleanup`、`memory_invalidated/note/claim/episode.changed` handler 时钟注入签名、legacy 快照 fixture 忠实回退到 Schema 6、Phase 4 边界测试改为断言已发布的 recall 契约面。
- 全量 `make ci`（第三轮复审修复后终跑）：**7725 passed / 0 failed**（263.72 s），总覆盖率 **83.26%**（阈值 80%）。复审轨迹：首轮 7690 / 83.07% → 第二轮 7710 / 83.19% → 第三轮 7725 / 83.26%（第三轮首跑暴露 2 个测试面问题——mypy 测试文件导入路径、legacy Schema-2 restore replay 缺表——均修复后终跑全绿）。

## 4. 性能基线

测量方法：`tests/performance/test_recall_latency.py`，`time.perf_counter()` 包住 `RecallService.recall()` 全程（含控制事务、并行路由、新鲜 Rehydrate 事务与 usage 持久化写）；40 样本/组；顺序调用（路由内部按生产语义有界并行，max 4）；数据集 60 claims（identity/fact 混合、40–120 字符）+ 20 热窗观察 + 8 state + 6 focus + FTS generation 60 文档；candidate cap = 各路由默认（20），token budget 100_000（避免裁剪干扰计时）；索引状态 HOT（首建后弃置前 3 次调用作 warm-up）；`deadline_at` 60 ms。

| 场景 | 样本 | 实测 p50 / p95 / max | 预算 |
| --- | --- | --- | --- |
| 结构化 Recall | 40 | 16.5 ms / **17.6 ms** / 20.7 ms | 50 ms |
| FTS Recall | 40 | 18.4 ms / **19.6 ms** / 21.6 ms | 100 ms |

（第三轮复审修复后复测：信任门改为未结算 backlog 计数（单条索引区间 COUNT）反而低于第二轮的 per-agent watermark 双读；恒走线程池的小额开销被抵消。历轮数字：首轮 16.2 / 17.3 ms → 第二轮 17.7·p50 24.5·p95 / 17.5·p50 20.2·p95 → 第三轮 17.6 / 19.6 ms p95。）

## 5. FTS 生命周期专项（`test_phase6_fts.py` 全过）

影子重建→原子切换（旧代 retired、指针 CAS）、失败切换回滚（上一已验证代继续服务）、重启持久（新 Store 读指针）、Correct 后旧 Revision 不可服务（文档行 revision 跟进）、Tombstone 逻辑失效→异步物理清理、`claim.changed`→`fts.apply` outbox 链路（worker 实跑 completed）、never_built/未知 builder（`fts_builder_unknown`, 不可重试）/落后（`fts_generation_stale`）/retired 指针（`fts_index_corrupt`）信任门、FTS 降级不破坏结构化路由、查询净化（FTS5 语法字符不可注入、停用词全消为 invalid）。

## 6. 泄漏扫描（canary）

- `test_trace_and_errors_never_leak_filtered_content`：restricted claim 的 `TOPSECRET-CANARY-CONTENT` 不出现在 trace/degraded/candidates/usage 行。
- `test_restricted_indexed_content_never_reaches_non_admin`（复审）：restricted claim **已入索引**（标签随行）但 recall 与 `/v1/search` 对非管理员 0 命中、0 正文。
- `test_usage_rows_store_no_candidate_text`：usage 表只含 candidate id 与计数。
- FTS Route 在 as_of 请求下降级 `fts_as_of_unsupported`（不返回当前代冒充历史）。

## 7. 对抗性自审（一轮，2026-09-02）

六个攻击面主动探查，发现并修复 1 个产品缺陷 + 1 个基础设施缺陷，全部落回归（ADR-0014 §12）：

1. **Stale snapshot**：新鲜 Rehydrate 屏障经"路由事务内提交 Forget"变体复验 —— 通过（本就覆盖；补 route-tx 内提交回归）。
2. **Forget resurrection**：`fts.apply` 完全未运行时 FTS SQL 的 tombstone 排除兜底 —— 通过（新增回归证明最后防线）；重建后同样 0 复活。
3. **FTS generation swap**：旧代残留被 `UNIQUE(generation, type, resource)` + current-generation SQL 过滤结构性挡住；搜索旧词 0 命中、当前修订正确服务。
4. **Usage forgery**：跨请求/跨 tenant/破链/不完整回显/persona 不匹配 5 类向量全部拒绝（§2）。
5. **Budget starvation**：`token_budget=0` 下 due task、focus 与说话人 identity claim 全部保留，非保护候选被裁剪。
6. **Privacy leakage**：§6 canary 扫描全过。
7. **[缺陷修复] 停用词 topic** 使 FTS 路由误报 `route_failed` → 改为完成且零候选（`test_stopword_topic_completes_fts_with_zero_not_degraded`）。
8. **[缺陷修复] MigrationRunner 连接泄漏**：`with connection` 不关闭句柄，在 WAL 库上钉住 sidecar 导致 restore 的 journal-mode 切换 `database is locked` → runner 显式 `closing()`（ADR-0014 §12-5）。

## 7b. 发布前外部复审（第二轮，2026-09-02）

CI 首轮全绿后的发布阻断级复审确认 9 项缺陷（7 项 P1、2 项 P2），全部修复并以
`tests/integration/test_phase6_review_round2.py`（20 例）锁定回归（ADR-0014 §13）：

1. **[P1] 路由 deadline 有界等待**：阻塞路由曾把响应拖过 deadline（future.result 无超时 + 线程池 join）——现逐 future 以剩余预算限时等待，超时弃置线程并标 `route_deadline_exceeded`（回归：500 ms 阻塞路由 + 50 ms deadline，响应 < 350 ms 返回）。
2. **[P1] Claims as_of**：历史 active、现已 retracted 的 claim 曾在候选阶段消失——`search_page(as_of_us=…)` 下推系统时间重建。
3. **[P1] FTS scope SQL**：精确 `=` 匹配违反下行可见性（space 请求看不到 agent 级文档）；请求维为空时又无子句（会话内容可被上级请求匹配到）。现按 `D IS NULL OR D = R` 逐维下沉，`space_group_id` 成为请求可选维度（契约 additive）——Group 内容可正向召回。
4. **[P1] Purpose/Actor 边界**：`data_purposes` 未参与授权、公开服务入口接受内部 speaker entity id——现 purpose 越界授予集即 `access_denied`；`RecallService.recall` 只接受外部 actor 引用并在服务内解析。
5. **[P1] 影子重建**：episodes/notes 每-Agent 10,000 截断曾静默产生"已验证但不完整"的代；checksum 校验声明了但未从持久化行重算——现全族键集分页 + 持久化行重算比对。
6. **[P1] FTS 落后口径**：租户级最大 watermark 既掩盖落后 Agent 又把追平 Agent 永久判 stale——新表 `fts_generation_agents` 记录逐 Agent applied watermark（重建播种、apply 推进、信任门按请求 Agent 比较）。
7. **[P1] Ranker 两处**：bm25 归一化方向反了（更相关得分更低）；冗余胜者曾被输入顺序覆盖评分顺序——现为 `|bm25|/(1+|bm25|)` 与"保留最高分者"。
8. **[P2] Usage 幂等重放**：同身份不同 payload 曾被静默吞掉且响应计数来自未持久化的第二次报告——现冲突重放 `invalid_request`、一致重放返回存量计数。
9. **[P2] Ghost postings**：退休代清理曾不发 FTS5 delete 命令，影子表 postings 永久膨胀——现逐行 delete 命令后删行（回归断言 docsize 影子行数 == 存活文档数）。

受影响的迁移语义：0007 增加 `fts_generation_agents`（工作区内版本，未发布——0001–0006 字节锁定不受影响），checksum 相应更新（§1）。

## 7c. 发布前外部复审（第三轮，2026-09-02）

第二轮修复后的复审确认 6 个未闭合边界与 1 个新增问题（6 项 P1、1 项 P2），全部修复并以
`tests/integration/test_phase6_review_round3.py`（15 例）+ 第二轮文件改写 2 例锁定回归（ADR-0014 §14）：

1. **[P1] FTS 信任门非连续消费前沿**：调度时读 Agent 当前最大 watermark、apply 以 `MAX()` 推进 applied——单个乱序 apply 跳过仍在排队的洞（false fresh）；非索引流量推进 live seq 却永不产生 apply（追平 Agent 永久 false stale）。信任门改为**请求 Agent 的未结算 `fts.apply` 任务数**（`outbox.unsettled_job_count`，pending/leased/retryable）：围栏完成 CAS 与投影写同事务提交，计数即连续消费前沿。`fts_generation_agents` 表发布前移除；`_schedule_fts_apply` 对无 Agent 触发事件（Forget 的 `memory.invalidated`）按资源归属解析 owner。（回归：乱序 apply 不恢复信任、队列排空才恢复、agentless 触发计入正确 Agent 积压。）
2. **[P1] Speaker 注入口未闭合**：`StructuredRecallRequest.speaker_entity_id` 仍是调用方可构造字段，`actors=None` 时原样使用——服务入口现**无条件清空**该字段，仅服务端 actor 解析回填（回归：注入内部 id 后编排器收到 None；actors 路径仍收到解析结果）。
3. **[P1] 组-空间组合未验证真实绑定**：只校验两者各自在授权集合，未查 `space_group_bindings`——recall `_authorize_request` 与 `authorize_scope`（覆盖 Search/写路径）在两者同时出现时要求命中活跃绑定行，否则 `access_denied`（回归：无关授权组合拒绝、绑定组合放行，recall 与 search 双面）。
4. **[P1] 单路由/单线程仍可无限阻塞**：`executors <= 1` 的同步快速路径删除——单路由与 `max_route_concurrency=1` 也经线程池限时 `future.result()`（回归：500 ms 阻塞路由 + 50 ms deadline，单路由抛 `deadline_exceeded` 且 < 350 ms；并发 1 下快路由完成、阻塞路由降级）。
5. **[P1] as_of 的 Valid Time 按 now 判定**：claim 路由下推 `valid_at_us=as_of_us`，最终 Rehydrate 与 relation 路由/Rehydrate 同以 as_of（未指定则 now）评估有效窗（回归："当时有效、现已过期"as_of 可召回、当前不可；"当时未生效"不进历史结果、当前可见）。
6. **[P1] 重建重索引已 Tombstone 的 Note**：`list_notes` 增加 SQL 级 `resource_tombstones` 排除（与 claims/episodes 枚举同法，`include_tombstoned` 显式开启）——已删除 Note 正文不再随重建回流新代（回归：墓碑 Note 不进 `documents_for_generation`，存活 Note 对照在）。
7. **[P2] Recall 请求级幂等无响应重放**：`recall_requests` 增加 `request_fingerprint`（排除 deadline 的逻辑摘要）+ `response_json`（完整服务响应）+ `resource_ids_json`——同 id 一致重放**逐字回放首次响应**（状态变化/新 deadline 不改变答复），冲突体重放 `invalid_request`；Forget 失效在同一写事务内 `scrub_request_responses` 擦除引用该资源的存储响应（异步 handler 幂等兜底；Schema <7 的 legacy replay 库无此表时为 no-op），被擦除的重放以 `conflict` fail-closed（回归：transport 重放逐字一致且不含后到内容、冲突拒绝、Forget 后重放 fail-closed、重放响应可完整 usage 回显）。

## 8. 已知限制

1. **FTS5 tokenizer 固定 unicode61**：构建器侧规范化 + 查询侧停用词已版本化，但更换 FTS5 tokenizer（porter 等）需要新的虚拟表面（新 ADR；ADR-0014 §1）。
2. **重建持有 Writer Gate**：全量重建在单写事务内收集+构建+切换，大租户应安排在安静窗口；增量 `fts.apply` 收敛积压。
3. **FTS 落后阈值**默认 10,000 个未结算 `fts.apply` 任务（可配，按请求 Agent 口径）；落后期间 FTS 路由 `fts_generation_stale` 降级而非阻塞。
4. **FTS 不支持 as_of 历史检索**（结构化 claims 路由承担 as_of；FTS 路由稳定降级 `fts_as_of_unsupported`）。
5. **Recall Cache 未实现**（ADR-0014 §6 显式非目标；`cache_until` 诚实返回 null，键语义已预冻结）。
6. **Usage 不回写 accessibility/activation**：记录-only；激励路径归 Phase 10（ADR-0014 §7）。
7. **`/v1/search` 请求面**暂无 `requested_privacy_labels`/`as_of` 参数（recall 面具备）；Phase 7+ 评估。
8. **可索引资源集 = {claim, episode, note}**；Observation 由 recent 路由承担、Artifact 结构性不索引（ADR-0014 §1）。

## 9. `make ci` 摘要

（任何失败都会使本节重写。）

- `format-check` / `lint`（ruff + import boundaries + docs）：通过
- `typecheck`（mypy 150 files + tsc）：通过
- `contracts-check`（generator --check + compatibility）：通过（纯 additive，baseline 未动）
- `pytest`（全量，`--cov-fail-under=80`）：**7725 passed / 0 failed**（263.72 s），总覆盖率 **83.26%**
- `sdk-test`（tsc + node --test）：**12 pass / 0 fail**

最终一轮（2026-09-02，发布前外部复审第三轮修复后，基线 b7bbad5 之上的工作区）实测输出摘要：

```
make ci
  ruff format --check .            -> files already formatted
  ruff check .                     -> All checks passed!
  python -m tools.check_import_boundaries -> domain import boundary: ok
  python -m tools.check_docs       -> documentation structure and local links: ok
  mypy                             -> Success: no issues found in 150 source files
  npm run typecheck (typescript)   -> exit 0
  generate_contracts --check       -> generated contracts: ok
  check_compatibility              -> contract compatibility: ok
  pytest                           -> 7725 passed in 263.72s; Total coverage: 83.26%
  npm test (sdk/typescript)        -> pass 12 / fail 0
  EXIT=0
```

mock-server 说明：契约验证使用测试进程管理的本机回环 HTTP server（`tests/contract/test_mock_server.py`，52 例）；最终 `make ci` 在允许 `127.0.0.1` 临时端口的本地环境完成，不访问外网。
