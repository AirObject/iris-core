# 测试分层与 pytest 耗时诊断报告

> 状态：分析完成（方案未实施，未迁移或重写测试）
> 日期：2026-09-07
> 基线 commit：`d5c0cb8e63ea519ae2804af29c390069019f19db` 之上的工作区
> 环境：macOS 26.6.2 arm64；Python 3.12.13；SQLite 3.50.4；pytest 8.4.2；pytest-cov 7.1.0
> 范围：测试分层、夹具开销、全量执行预算、覆盖率依赖；本次只新增本报告。

遵循[报告索引](README.md)的证据边界，并按 [Phase 8 验证报告](phase-08-verification.md)记录环境、命令、实测与限制。当前工作区已有文档及目录整理变更；本报告不将其归入本次工作，不修改 `tests/`、源码、pytest 配置或 CI。

## 1. 结论与统计口径

**存在错层，但将逻辑测试搬到 unit 不是主要提速手段。** 最大 8 文件中可解除过重依赖的样本只占约 3 秒；当前串行全量已超过 17 分钟，主要成本是反复建 schema、完整业务调用/造数、崩溃子进程与固定等待。前三项建议是：私有 schema 模板与快速关闭 mock、重组重复 recall 的性质测试、将剩余真实集成分片并合并覆盖率。完整两分钟目标需要额外有效执行资源，尚未实测证明。

`436.09 s` 是 Phase 8 在 2026-09-03、8,128 个测试、旧候选上的历史结果，不是当前工作区基准。[CI 注释](../../.github/workflows/ci.yml)引用的也是这次历史测量。本次收集到 **11,681 个 case**，不能将当前夹具成本直接倒算成当时 436 秒的组成。

文件数量体现维护成本；参数展开后的 case 数和实际执行时间才反映运行负担。按 Python 文件统计，用户给出的数字均得到复核：

| 目录 | Python 文件 | 其中 `test_*.py` | Python 行数 | pytest case |
| --- | ---: | ---: | ---: | ---: |
| `tests/integration` | 133 | 131 | 54,360 | 2,183 |
| `tests/unit` | 16 | 16 | 3,388 | 8,884 |
| `tests/contract` | 7 | 7 | 3,123 | 570 |
| `tests/performance` | 7 | 7 | 1,061 | 11 |
| `tests/fault` | 3 | 1 | 1,367 | 31 |
| `sdk/python/tests` | 不计入用户的 tests 文件口径 | — | 不计入下述 tests 行数 | 2 |

`tests/**/*.py` 共 **63,618 行**，`src/**/*.py` 共 **79,652 行**；前者包括 `conftest.py`、场景脚本和 helper，后者不含 Python SDK。unit 占展开 case 的 **76.1%**，已有大量 200-seed 领域性质测试，尤其是 `tests/unit/test_phase4_domain.py`。不能仅据 133:16 的文件比例断言缺少单元测试。

## 2. 最大 8 文件的分层抽样

按 `tests/integration/*.py` 的实际行数排序，检查每个文件的夹具、测试入口和调用目标，并抽读关键断言及对应源码。以下不是对全部 133 文件的推算。分类以**现有断言能否保真**为准，不将所有调用私有方法、抛领域异常或使用 deterministic provider 的测试都判成纯单元测试。

- **A：保留真实存储/集成边界**。至少需要真实 SQLite、持久化状态或跨服务协作；可缩小 wiring，并不表示每例都需要完整 HTTP app 或全部 recall 路由。
- **B：可由无 I/O 的领域或应用逻辑测试承接**。当前断言主体不需要数据库；移交时保留少量服务入口拒绝非法输入的桥接测试。
- **C：可收窄为无 SQLite 的组件测试**。仍需 ASGI 请求/取消、文件系统等，不冒充纯函数测试。

| 文件 | 行数 | 测试函数 | 展开 case | A | B | C |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `test_review_regressions.py` | 1,496 | 47 | 47 | 43 | 4 | 0 |
| `test_events.py` | 1,229 | 30 | 30 | 30 | 0 | 0 |
| `test_phase8_graph.py` | 1,209 | 34 | 129 | 129 | 0 | 0 |
| `test_triggers.py` | 1,203 | 27 | 35 | 24 | 11 | 0 |
| `test_phase6_recall.py` | 1,185 | 19 | 29 | 25 | 4 | 0 |
| `test_tasks.py` | 1,044 | 18 | 18 | 18 | 0 | 0 |
| `test_console_authentication.py` | 1,019 | 24 | 59 | 44 | 0 | 15 |
| `test_phase7_review.py` | 977 | 24 | 24 | 23 | 0 | 1 |
| **合计** | **9,362** | **223** | **471** | **436（92.6%）** | **19（4.0%）** | **16（3.4%）** |

这 8 个文件占 integration 行数的 17.2%、case 的 21.6%。B 的 19 个 case 中，**5 个原本就没有建库**，只有 14 个能直接解除数据库依赖。加上 C 的 16 个，共 30 个 case 是明确的过重夹具候选；保留入口桥接后，净减少量还会小于 30。搬走全部 35 个 B/C case 不能据此承诺节省数百秒。

### 2.1 `test_review_regressions.py`

真实集成部分：`TestObservationAuthorization`、`TestObservationScopeHierarchy` 验证绑定及 scope 的数据库现势状态；`TestBackpressureProjections` 验证 observations、outbox、cursor 同事务回滚；`TestOutboxFencingSurface`、`TestBackpressureCoalesceAndReplay`、`TestDedupeContentOwnership` 验证租约、CAS、合并、重试和持久化正文；`TestMetricsWiring` 验证生产写路径确实发出指标。这些不能用领域函数替代。

明确的 B 候选：

| 入口（当前行号） | 真正目标 | 最小依赖/承接位置 |
| --- | --- | --- |
| `test_disk_hysteresis_uses_recovery_gap`，430 | BackpressureGauge 的阈值与恢复滞回 | `FixedDiskProbe` + 普通 Path；当前 `database` 只是临时文件名，**未迁移数据库**；承接到 `tests/unit/test_phase2_domain.py` 或独立 backpressure 文件 |
| `test_leading_zero_cursor_rejected`，509 | `domain.observation.validate_cursor` 的 `01` 非法格式 | 纯输入矩阵；当前却要求 `clocked_store`、tenant、agent；保留一个 `ObservationService.observe_batch` 入口映射断言 |
| `test_focus_maintenance_catch_up_default_is_coalesce`，663 | `spec_for` registry 默认值 | 已无夹具，可归入 `tests/unit/test_phase2_domain.py` |
| `test_free_text_in_numeric_log_field_is_redacted`，668 | `sanitize_log_record` | 已无夹具，保留原数据断言即可 |

### 2.2 `test_events.py`

30 个 case 全部保留数据库语义。`event_ctx` 依赖 `clocked_store`、`phase4_events`、`phase4_tasks`，在测试函数中通过 `CognitiveEventService.create_internal` 建事件。ACK 100 次验证的是只产生一条历史链；lease mode matrix、expiry summary scope、分页饥饿和 post-CAS 返回值都需要真实 repository。

`TestNegativeCompletionGates` 看似状态机，实际上验证事件投递/ACK **没有跨服务改变 Task/Step**、没有伪造 completion evidence。将它换成 `validate_event_transition` 会丢失原来的安全断言。可把 `phase4_tasks` 从通用 `event_ctx` 中拆成按需夹具，但必须先解决其间接请求另一个 `store` 的问题（见 §3）。

### 2.3 `test_phase8_graph.py`

129 个 case 全部包含真实投影或权威存储断言。`world` 每例创建 `Phase8World`，同时接入 Identity、Observation、Claim、Relation、Graph、Profile 和 Recall。

`TestPerEdgeProperties` 的 scope/privacy/time 测试看起来接近纯规则，实际调用 `GraphRoute._edge_visible(tx, ...)`；源码还读取活墓碑及两端 Entity 的状态/隐私。仅测 `scope_allows` 或 `evaluate_privacy` 不能替代这些断言。`test_status_property_200_cases` 更包含 Claim correct/retract、重建及投影行检查。

可缩小 wiring 的重点是 `TestFaultInjection`：5 个参数化测试各 20 轮，共 100 个 case。除 checksum 案例还检查完整 recall 降级与恢复外，另外 4 类、80 个 case 的核心是 `GraphProjectionService.trusted_generation_in_tx` + repository。可使用真实 SQLite 的小型 graph fixture；不能只 mock reason code。`_healthy_world` 和恶意 hub 数据可由独立快照派生，每例仍在私有副本破坏并检验真实提交/回滚。保留 `test_corrupted_checksum_fails_closed` 的提交后状态断言，它专门防止“抛错导致 pending_rebuild 被回滚”。

### 2.4 `test_triggers.py`

24 个 case 保留：occurrence 唯一性、100 次扫描幂等、重启后 marker 恢复、DST 策略是否真的传入执行路径、租户/space/evidence 边界都需要 `TaskService` + SQLite。

B 共 11 个 case：`TestTriggerValidation.test_non_declarative_specs_rejected`（136，9 个参数）、`test_unknown_timezone_rejected`（146，1 个）、`TestDstCatchUpMatrix.test_timezone_matrix_covers_dst_edges`（448，1 个）。前两者仅检查声明式输入拒绝，却通过 `trig_ctx` 建 task；目标可落到 `validate_trigger_common`、`validate_condition_spec`、`parse_trigger_schedule_spec`，以及当前仍在应用层但无 I/O 的 `TaskService._canonical_schedule`。最后一例本来就仅用 `ZoneInfo`、`next_occurrence`，无需数据库。承接到已有 `tests/unit/test_phase4_domain.py`，不要重复现有条件/DAG 矩阵。

### 2.5 `test_phase6_recall.py`

25 个 case 保留真实集成：speaker registry、跨成员隐私、投影滞后、fresh rehydrate 对并发 Forget 的屏障、usage、trace、预算和多路 recall。尤其不能将 `test_concurrent_forget_during_collection_never_resurrects` 替换成静态过滤函数测试。

B 共 4 个 case：

- `test_missing_score_is_not_zero(world)`（669）：函数体未使用 `world`；只调用 `compute_final_score`，明确浪费一次 migrated store + `build_world`。
- `test_conflict_groups_marked_and_penalized`（700）：仅创建 `ScoredCandidate` 并调用 `mark_conflicts_and_redundancy`，原本无夹具。
- `test_deadline_is_converted_once_from_wall_clock(world)`（1120）与 `test_unknown_purpose_rejected(world)`（1141）：只调用 `RecallService.build_request`。该方法只用 wall/monotonic clock 及输入校验，不读 UoW。可用可控双时钟和“被访问即失败”的 UoW/orchestrator stub；精确验证转换公式和调用次数，比当前仅比较两个时间大小更有效。

这 4 例可进入新 `tests/unit/test_recall_request_and_scoring.py`。真正的大额成本候选另在 200 次重复 recall 循环，见 §4，不能与这 4 个小测试混为一谈。

### 2.6 `test_tasks.py`

18 个 case 保留数据库断言。DAG 的 `would_create_cycle` 确为纯函数，但 `test_cycle_rejected_stably` 通过 `add_dependency` 建图，验证服务读取已有边后拒绝成环；`test_cross_task_dependency_rejected` 验证归属；`test_steps_use_stable_keys_and_derived_readiness` 同时验证 UNIQUE stable_key、依赖添加后 readiness 回撤。`test_fifty_threads_same_expected_revision_one_winner` 必须保留真实竞争与 CAS。

纯 DAG、状态转移和 readiness 性质已有 `tests/unit/test_phase4_domain.py` 覆盖，继续搬空这 18 例会删除集成桥接。可以将更广的状态输入矩阵放在已有 unit 中，但不能把这算成当前 18 例可直接删除的成本。

### 2.7 `test_console_authentication.py`

44 个 case 保留真实 Console/SQLite 协作：session fixation、refresh alias 加密和重放、持久化 rate state、重启、撤销、owner handoff、授权不扩大、CLI 和幂等 TTL 等。`auth` 每例调用 `create_console_app` 并 `issue_offline`；`TestClient` 是进程内 ASGI 调用，不需要本地 TCP 监听。

C 共 15 个 case 可使用最小 ASGI app + config + 拒绝存储访问的安全服务 stub：`test_login_strict_boundary`（10 个输入，132）、`test_invalid_unicode_and_nul_are_rejected_before_storage`（3 个输入，1006），以及 `test_login_capacity_covers_body_and_is_released_on_cancellation` 的 `capacity`、`slow-body` 两个参数（929）。目标分别是入口/header/body 校验、UTF-8/NUL 拒绝、并发槽及 body timeout；仍需真实 Request、ASGI 异常映射与 semaphore。

该测试的 `cancel` 参数先完成正常登录，再在 `_pad_login` 抛取消，故保守地留在 A。`routes_auth._pad_login` 每次登录至少补齐 100 ms 是安全语义；普通边界测试未来可注入可控等待，但必须留独立的真实时间与取消释放验证，不能为提速修改生产 padding。

### 2.8 `test_phase7_review.py`

23 个 case 保留 SQLite + 文件系统/FAISS 的协作：rename 与 pointer 发布窗口、回滚、backup/restore、generation checksum、多个 Store 的句柄刷新、provider 故障不阻断 Canonical 写入，以及提交后才能删目录。deterministic embedding provider 只替代外部模型，未替代 SQLite 与 FAISS。

C 的 1 例是 `test_abandoned_tmp_directory_swept`（118）：仅创建临时目录、设置 mtime、调用 `VectorIndexManager.sweep_tmp`；该方法只访问文件系统，不查库、不加载 FAISS。可用 `tmp_path` + `VectorSpaceConfig` 直接构造 manager，解除整个 `VectorCtx`，承接到文件系统组件测试。

## 3. 夹具生命周期与实测

### 3.1 静态依赖

[tests/conftest.py](../../tests/conftest.py) 的 `@pytest.fixture` 均未指定 scope，默认 **function**，不是 session 复用；也没有自动应用全局 `store`。纯 unit 不会因为共享 conftest 就自动建库。

```text
tmp_path → database（只返回 canonical.sqlite3 路径）
                    ├─ store → MigrationRunner.migrate → Store(SystemClock)
                    └─ clocked_store → MigrationRunner.migrate → Store(MutableClock)

store → idempotency → phase4_notes / phase4_tasks / phase4_events
clocked_store ──────→ phase4_notes / phase4_tasks / phase4_events
```

因此，一个测试同时使用 phase4 service 和 clocked store 时，通常在**同一个测试的同一数据库文件**上调用两次 migrate，得到两套 Store/clock；第二次一般是检查已应用迁移，不是再建一个全新库。pytest 会在单个 case 内缓存同名 fixture 的值，但不会合并两个不同 fixture。Phase 5 的 `phase5_idempotency(clocked_store)` 已明确规避这个 clock 不一致的问题，Phase 4 可沿用这一依赖设计；直接统一前仍需检验事件时间、lease 和幂等行为。

`phase6_world → build_world`、Phase 8 本地 `world → Phase8World`、Phase 7 `ctx → VectorCtx` 在 function scope 上继续插 tenant/agent/space/entity 等并构造服务。它们不等同于纯建 schema。

[tests/migration_support.py](../../tests/migration_support.py) 的 `migrate_through` **不是 fixture**：每次创建 `TemporaryDirectory`，逐文件复制所需版本的历史 SQL，再运行一次真正的 `MigrationRunner.migrate()`，结束删除临时 SQL 目录。它只限制 migration 前缀，不缓存数据库。迁移测试中这些执行是被验证对象，不能作为通用 setup 一律跳过。

当前空库迁移执行 20 份 SQL。`storage/migrations.py` 的 discovery、checksum/metadata 校验、`migration_runs` 记账和多次 commit 都在每次新库迁移路径中；普通 runtime 连接仍有自己的版本、PRAGMA 和事务开销。

### 3.2 测量结果

全量按当前默认覆盖率设置运行，附加 `--durations=25`，临时 pytest 插件记录每个 case 的 setup/call/teardown 及主进程 `MigrationRunner.migrate` 的包围时间。插件和数据均在 `/tmp/iris-test-layering-20260907/`，没有写入 tests。

| 执行 | 实际结果 | pytest 报告总时长 |
| --- | --- | ---: |
| 当前工作区全量，启用 branch coverage | **11,627 passed，54 errors**；54 个错误全部是 `test_mock_server.py` 的 loopback bind 被沙箱拒绝 | **1,053.29 s** |
| 允许本地监听后，仅补测 `test_mock_server.py`，仍启用 coverage | **54 passed** | **32.40 s** |
| `test_console_authentication.py` 单文件环境排查，`--no-cov` | **59 passed** | **13.50 s** |

全量进程不是全绿，不把补测拼成“一次全量通过”。54 个错误在 setup 提前退出，也省掉了它们正常的 teardown；替换该文件的 case 阶段时间后，完整串行工作的**估算**为 `1053.29 - 0.0174 + 27.2575 = 1080.53 s`。这是约 18 分钟的工作量估算，不是第二次全量实测；更不能仍把当前 suite 称为 436 秒。

| 全量进程时间归因 | 次数/口径 | 累计秒 | 占 1,053.29 s |
| --- | --- | ---: | ---: |
| setup | 所有 case | 198.07 | 18.8% |
| 其中 migrate | 主进程 setup 内 2,149 次 | **117.60** | **11.2%** |
| setup 除 migrate 的其余工作 | world 造数、服务装配、临时目录、pytest 等 | 80.48 | 7.6% |
| call | 测试函数全程 | **838.39** | **79.6%** |
| 其中 migrate | 主进程 call 内 297 次 | 9.17 | 0.9% |
| teardown | 该进程内，未含失败 mock 的正常清理 | 1.83 | 0.2% |
| 其他 | 收集、coverage 报告、hook/框架等阶段差额 | 15.00 | 1.4% |

**migrate 是所在阶段的子集，不可再相加。call 也不是“纯断言时间”**：包括造数据、真实业务操作、DB 连接/事务、FAISS 重建、等待、线程和子进程。尤其 620 个 SIGKILL 子进程内的迁移没有进入主进程探针；它们算在 fault 的 call 中。这里不能给出当年 436 秒的精确夹具占比，只能回答当前工作区实测中，setup 占 18.8%，主进程 setup 迁移占 11.2%。

共享 `clocked_store` 实际构造 **1,938 次 / 113.28 s**，`store` **209 次 / 4.38 s**，其中 **140 个 case 同时要求两者**；依赖集合并集为 2,007 个 case。名为 `world` 的各模块夹具累计 **1,058 次 / 48.25 s**，不是某一个 World builder 单独的耗时；`auth` 累计 770 次 / 9.58 s。fixture 的内部计时用于定位，预算仍以不重叠的 case 阶段时间为准。

按目录累计 case 阶段时间如下，不能直接拿它们当各目录单独运行的完整 wall time：

| 目录 | setup s | call s | teardown s | 合计 s |
| --- | ---: | ---: | ---: | ---: |
| integration | 190.39 | 665.44 | 1.12 | **856.96** |
| fault | 0.02 | 113.81 | 0.09 | **113.91** |
| performance | 2.49 | 54.10 | 0.01 | **56.60** |
| contract（54 个环境错误） | 4.22 | 2.93 | 0.05 | 7.20 |
| unit | 0.97 | 1.93 | 0.55 | **3.45** |
| Python SDK tests | <0.01 | 0.17 | 0.01 | 0.18 |

### 3.3 最大 8 文件的成本对应关系

| 文件 | setup s | 其中迁移 s | call s | 含 teardown 的合计 s |
| --- | ---: | ---: | ---: | ---: |
| `test_review_regressions.py` | 3.01 | 2.81 | 0.45 | 3.48 |
| `test_events.py` | 2.21 | 1.95 | 3.16 | 5.38 |
| `test_phase8_graph.py` | 10.63 | 8.60 | 68.90 | 79.60 |
| `test_triggers.py` | 2.64 | 2.27 | 1.64 | 4.30 |
| `test_phase6_recall.py` | 1.80 | 1.53 | **150.42** | **152.24** |
| `test_tasks.py` | 1.32 | 1.14 | 2.18 | 3.51 |
| `test_console_authentication.py` | 3.69 | 2.95 | 10.31 | 14.03 |
| `test_phase7_review.py` | 1.68 | 1.56 | 2.27 | 3.99 |
| **合计** | **26.96** | **22.80** | **239.36** | **266.52** |

分类 B 全部 case 仅 **1.07 s**，分类 C 仅 **2.12 s**，其中不少 call 还应保留。即使把这些 case 的全部执行时间都抹掉，上限也只有 **3.19 s（全量的 0.3%）**。最大文件 `test_review_regressions.py` 反而只耗 3.48 秒；按行数拆大文件不是速度优化的可靠排序。

Phase 6 的 hard-filter 参数矩阵单独占 **147.04 s call**；独立收集的 revision 测试再用 **1.15 s**，二者 setup 合计只有 0.86 s。Phase 8 的五类故障参数化共耗 **8.20 s setup + 7.84 s call**；这里复用健康种子有价值，但它不是 graph 文件的全部 79.60 秒。

### 3.4 建库与固定等待微基准

不改测试，仅在临时目录构建/复制 schema，40 次循环，模板大小 1,814,528 bytes。每个副本经真实 Store 首读并验证 schema 20，另查 `quick_check=ok`、无 FK 违规；校验时间不混入复制时间。

| 操作 | 首轮平均 / 中位 ms | 复测平均 / 中位 ms |
| --- | ---: | ---: |
| 新路径完整迁移 20 个 SQL | 77.24 / 78.41 | 81.32 / 78.49 |
| 同库再调用 migrate（无待应用 SQL） | 3.32 / 3.22 | 3.37 / 3.29 |
| `shutil.copyfile` 复制已关闭模板 | 0.61 / 0.56 | 0.85 / 0.86 |
| 副本的第一次真实 Store 读取 | 2.65 / 2.64 | 2.73 / 2.65 |

微基准没有启用 coverage，且首轮与全量进程重叠、复测发生在 unit coverage 报告窗口附近；文件位置与热缓存也可能不同。它证明“复制已关闭模板远低于逐库执行 DDL”的可行性，**不是** 2,007 倍微基准差值的可靠收益预测。第一项方案的预算应以全量实际 117.60 秒 setup 迁移为上限，保留迁移门禁成本及每例复制开销。

mock-server 补测的 **setup 0.095 s，call 0.139 s，teardown 27.023 s**。本机标准库 `BaseServer.serve_forever(self, poll_interval=0.5)` 的默认轮询与每例 0.50 秒清理完全吻合；`mock_base_url`（51 次）和 `recorded_mock_server`（3 次）均为 function scope，每例启动/关闭一次 server，54 次合计约 27 秒。这是与 SQLite 无关的确定性夹具等待。0.01 s 轮询的预期等待约 0.54 秒，预计能省约 26 秒，但本次没有修改 fixture 验证该提速，调度延迟仍可能超过轮询间隔。

## 4. 两分钟目标与前三项方案

目标口径是**保留门禁语义、合并 branch coverage 后的完整 pytest wall time <120 s**；不是只执行 unit，也不是把慢测试移到别的目录后不收集。下列都是拟议变更，本次未实施。

### 4.1 第一优先：消除重复基础设施初始化和固定 teardown 等待

范围：`tests/conftest.py` 的 `store`、`clocked_store`、`idempotency` 与 phase4 service 依赖；`tests/contract/test_mock_server.py` 的 `mock_base_url`、`recorded_mock_server`。预计 0.5–1.5 工程日，收益以 §3 测量为上限，不把全部 setup 当作可删除成本。

1. 增加 session/worker 级**只读、已关闭的空 schema 模板**，从真实 MigrationRunner 构建一次；增加 function 级 `migrated_database(database, schema_template)`，为每个 case 复制私有文件。`store` 与 `clocked_store` 共同依赖它，实例仍为 function scope。保留原 `database` 作为空路径，避免无意给迁移测试提前建成最新 schema。
2. 模板在所有连接关闭、WAL 已结清后生成；只缓存 schema，不缓存 tenant、数据、活连接、Store、MutableClock 或 FAISS 句柄。SQLiteRuntime 的生产 PRAGMA/版本检查、每例真实提交和回滚继续执行。不要用统一外层事务 rollback 替代它们：服务自开连接、并发 Store、DDL、backup/restore 都会突破这种隔离。
3. 按 `phase5_idempotency` 的现有设计，为 phase4 service 绑定同一 `clocked_store`，消除同库两套 clock/UoW 的依赖歧义。收益主要是语义清楚及少量 no-op migration，不能把它当作第二次全量建库的节省。
4. `mock_base_url`、`recorded_mock_server` 保持 function scope 和每例私有状态，显式给 `serve_forever` 设置短 `poll_interval`（候选 0.01 s），保留 shutdown、close、join。直接 session 复用 server 会引入 mock 状态串扰，没必要为消除 0.5 s 等待而承担这种变化。

迁移和恢复路径例外：`test_migrations*.py`、各 `test_*migration*.py`、`test_backup.py`、historical `migrate_through` 的被测迁移仍真实执行，不将全局 `MigrationRunner.migrate` 换成复制。`tests/fault/kill9_scenarios.py::Spine` 的 schema 初始化在所有业务 kill point 之前，可在后续独立评估是否采用空模板；仍要每次启动真实子进程、SIGKILL、重开私有数据库。本项收益估算默认不包含子进程中的迁移。

验收：原 case 集合不变；隔离检查覆盖两个副本互不污染、同例服务时钟一致、源码 SQL 改变后模板重新生成；迁移/restore/并发/CAS/kill9 的原回归继续通过；记录 migration 次数、setup 和 teardown 的真实下降。模板只在本次 pytest session 内复用，暂不引入磁盘长期缓存。

### 4.2 第二优先：把性质样本量与完整链路重复量分开

范围：`tests/integration/test_phase6_recall.py`、`test_phase8_graph.py`、`phase8_helpers.py`，再处理 §2 的 B/C 清单。预计 1–3 工程日。此项改变测试组织，需要逐条证明原门禁语义仍被覆盖，不能机械把所有 200/100/50/20 改成 1。

`test_hard_filter_dimensions_exclude_violating_candidates` 的 10 个非 revision 维度，每维先一次性创建 200 个 violating + 200 个 control，再进行 200 次 topic/过滤条件相同的完整 recall；随机数只决定当轮检查哪一对。建议保留全部 200 对数据，先对一次结果验证 `violating ∩ returned = ∅` 与 `control ⊆ returned`，再保留少量显式改变时间、revision、授权或 usage 状态的请求序列。需要确认 candidate/token cap 足以容纳全集，以及多次 recall 的 usage 写入有没有被原断言隐式利用；不满足等价性时，保留对应真实序列或把 200 对拆为小批次逐批检验。

revision 分支直接调用 `test_hard_filter_revision_dimension(world)`，该函数又独立被收集，固定陈旧 revision 的 200 次请求因此执行两遍。将造候选的代码变成非 `test_` helper，只保留一个清晰的参数入口；把“revision 不等”性质样本放在独立过滤矩阵，保留真实 fresh-rehydrate 的代表场景。`test_concurrent_forget_during_collection_never_resurrects`、专门的 100 次 byte-identical replay 和 usage 幂等测试仍承担真实时序/重放门禁。

Phase 8 的 100 个故障 case 保留各次独立损坏、真实 commit 和拒绝服务断言；把 `_healthy_world` 生成的健康状态变为只读种子，每例复制后再破坏，并按断言按需创建 graph-only 或完整 recall service。恶意 hub 在三个预算/读取测试中反复建造，也可由相同只读种子派生。Graph 中 50 次 Correct/Forget/Binding 后验证旧边不复活、200 个端点隐私/墓碑案例不可替换成仅断言 reason code 的 mock。

§2 的 19 个 B case 和 16 个 C case 是这项工作的具体分层清单，但其目标主要是减少依赖和提高定位能力；以实测的小额耗时预算看待它们。保留服务入口桥接的同时将输入矩阵下沉，避免同时复制两套等价矩阵。

验收：按维度列出输入数、状态变化、持久化边界和对应断言；前后行/分支覆盖差异逐项解释；200 对性质数据必须逐对验证，不以减少样本数换时间。原文若明确要求某条真实路径执行 N 次，则继续执行 N 次；需求变更必须单独修订门禁，不能静默借“分层”豁免。

### 4.3 第三优先：让剩余真集成可调度，并合并完整覆盖率

范围：`pyproject.toml` 的 dev 依赖、`Makefile` 的 pytest 入口、`.github/workflows/ci.yml`、`tests/fault/test_kill9.py`。当前未安装、也未声明 pytest-xdist，本次未安装或尝试并行，不将理论 worker 倍数报告成实测提速。预计 1–2 工程日，先做 2/4 worker 对照，再决定是否需要独立 CI shard。

`test_kill9.py` 只有 31 个 pytest case，却在函数内循环 20 次，形成 **620 次真实子进程**；`Spine` 每次重建 schema。这些时间主要在 call，不会被 `store` fixture 的 setup 指标看见。未来可将 `(scenario, crash_at, iteration)` 暴露为独立可调度 case，仍使用各自 `tmp_path`，保留全部 620 次 SIGKILL 和重开后断言。不要仅按文件分配任务，否则这个文件及大型 recall 文件会成为长尾。

先隔离全局状态、临时目录、端口、vector root，再按 case 的实测耗时平衡分片。`test_tasks.py` 的 50 线程 CAS、Phase 8 并发测试与 `tests/performance` 不宜与多个高负载 worker 争用同一测量主机；性能门禁用独立 job/资源池，覆盖率数据与其他 shard 合并后再执行全局 `--fail-under=80`。不能要求每个 shard 单独达到 80%，也不能只取最快 shard 的覆盖率。

本项是 wall time 优化，不减少总工作量。若改成“普通 PR 快速反馈 + 完整 fault/performance 门禁另跑”，必须同时展示两者耗时，并把完整门禁设为合并要求；**不能把快速反馈 <120 s 标记成完整 pytest <120 s**。

### 4.4 时间预算与退出条件

从约 1,080.53 秒的串行工作量估算出发，以下是**工程预算，不是已实现收益**：

| 优先级 | 可归因的当前成本 | 初步节省预算 | 为什么排在这里 |
| --- | --- | --- | --- |
| 1. 空 schema 私有副本 + mock 快速关闭 | setup migrate 117.60 s；mock teardown 27.02 s | 约 100–115 s + 26 s | 不减少 case 或断言；实现集中，收益证据最清楚 |
| 2. Phase 6 请求重复 + Phase 8 健康种子 + 小型逻辑拆出 | Phase 6 hard-filter/revision call 148.20 s；graph call 68.90 s 中有重复建图；B/C 总计仅 3.19 s | 约 130–145 s + 10–20 s；B/C 不单列大额收益 | 主要针对真正的 call 热点；需花时间确认等价性 |
| 3. 真集成分片、fault case 化、单独资源跑性能门禁 | 前两项后仍约 **775–815 s** 的串行工作量 | 依赖资源与调度，尚未实测 | 不改变持久化/崩溃/时序语义，处理剩余 600+ 秒工作量 |

任何节省量都应避免重复计数：migration 已在 setup 内，健康 world 的复制收益可能与 schema 模板重叠，重复 recall 精简已包含其中的连接/usage 写入成本。

**只改分层和夹具、仍用单进程跑当前全部门禁，不足以支撑两分钟承诺。** 一个刻意乐观的反证：即使抹掉所有 setup 198.07 秒、整个 Phase 6 文件的 call 150.42 秒、mock 等待 27.02 秒，仍约有 **705 秒**。因此还需并行资源或更深的真实业务热点优化，不能把 436→120 当作本仓库当前只需节省 316 秒的小调整。

按前两项后的约 790 秒工作量举例：性能 suite 约 56.60 秒放到独立资源池，剩余约 733 秒。理想 4 个等速分片也需约 183 秒，尚未含收集/coverage 合并，**4 分片不能据此承诺达标**。理想 8 分片约 92 秒，若长尾、启动与合并合计控制在 20 秒以内，才有 <120 秒的预算空间；性能 job 与之并发，不再另加 56 秒。这里需要近似 8 个有效执行资源及独立性能资源，`-n 8` 挤在少核 runner 上不等于 8 倍吞吐，GitHub 当前 runner 的有效并行度须另测。

首次并行试验应关注已测出的长尾：`test_phase8_concurrency.py::test_reads_race_rebuilds_without_torn_results` **28.32 s**、`test_phase10_pipeline.py::test_dead_letter_replay_one_hundred_times_keeps_one_logical_episode` **20.49 s**、`test_phase7_vector.py::test_model_switch_never_mixes_spaces_1000_revisions` **19.13 s**。它们不是本次最大 8 文件抽样的纯逻辑迁移候选，不因长度排行而忽略。若不接受新增执行资源，建议明确降低目标为“先把当前约 18 分钟减至实测的新串行基线”，再决定真实算法/事务热点优化；本报告不虚构两分钟保证。

完成前三项后，在同一候选、同一依赖与硬件上记录至少三轮完整 wall time、最慢 case/shard、合并行与分支覆盖和所有 gate 状态；三轮均 <120 s 才能认定目标完成。若单 case 或性能门禁的不可并行部分本身超预算，明确报告目标未达到，不降低安全门禁或覆盖率分母。

## 5. 覆盖率依赖与迁移影响

### 5.1 实测：80% 门槛严重依赖集成路径

`pyproject.toml` 同时指定 `--cov=iris_memory_core`、`--cov=iris_memory_sdk`，`branch = true`，门槛是两包**总体行与分支联合覆盖**，不是单独的行覆盖或 unit 覆盖。统一分母为 **30,088 个语句 + 9,898 个分支 = 39,986 个可覆盖项**。

| 运行/数据集 | 结果与耗时 | 覆盖语句 | 覆盖分支 | 联合覆盖率 | 对 80% |
| --- | --- | ---: | ---: | ---: | --- |
| 原全量进程，54 个环境错误 | 11,627 passed / 54 errors，1,053.29 s | 26,369 | 7,251 | 84.08% | 覆盖数值达到；测试未全绿 |
| 全量 + 54 个成功端口补测的数据并集 | 两次 coverage 数据合并；不是一次全量运行 | 26,726 | 7,352 | **85.22%** | 高出 5.22 个百分点 |
| unit-only，固定完整分母 | **8,884 passed，14.10 s** | 8,381 | 906 | **23.23%** | 远低于门槛 |
| 排除 `tests/integration`，固定完整分母 | **9,498 passed，255.30 s** | 18,478 | 3,903 | **55.97%** | 低于门槛 24.03 个百分点 |

排除 integration 后比合并全量低 **29.25 个百分点**，可明确回答“严重依赖”。独立运行有线程/时钟等路径差异，这个差额是本次覆盖结果的净差，不冒充按 test context 精确归属的唯一分支集合。

排除 integration 仍保留 contract、fault、performance、unit 和 SDK；其中 `test_phase10_asgi.py` 用真实 app/SQLite，performance 还调用 `build_world`。所以 55.97% 也不是“纯单元测试覆盖率”。而且剩余套件仍需 255.30 秒，仅删除 integration 的执行范围既不保住覆盖率，也无法直接得到两分钟。

| 源码区域 | unit-only | 排除 integration | 合并全量 |
| --- | ---: | ---: | ---: |
| domain | 84.09% | 88.29% | 91.32% |
| application | 10.87% | 43.60% | 84.28% |
| storage | 25.78% | 61.39% | 87.66% |
| indexing | 12.22% | 50.98% | 82.98% |
| api | 6.81% | 50.77% | 89.76% |
| Python SDK | 0.00% | 72.81% | 72.81% |

unit 的领域覆盖已经较高；差距主要在真实应用、repository、投影和 API 路径。部分覆盖来自模块导入时执行的定义，并不意味着相同数量的业务分支已被 unit 断言。

### 5.2 分母陷阱与逻辑下沉后的变化

直接使用原配置只跑 unit，初测报告 **25.16% / 16.38 s**，同时警告 `iris_memory_sdk` 未导入；其 JSON 不含 SDK，分母缩成 28,095 语句 + 8,816 分支。为公平比较，本次用命令行 `--cov-reset --cov=src/iris_memory_core --cov=sdk/python/src/iris_memory_sdk` 重测 unit 和排除 integration 的组合，使 SDK 的未执行行/分支也纳入分母。没有修改项目配置。表中可比结果使用 23.23%，不使用偏大的 25.16%。

逻辑下沉的影响分三种：

1. **仅移动文件、执行路径保持相同**：只要全量仍收集两个目录，coverage 原理上不变；目录名不进入 coverage 的源码分母，也不决定计数权重。耗时通常也不变。
2. **解除 `world`、改为直接调用同一纯函数**：该函数已覆盖的行和分支可保留；原来顺带执行的 wiring、初始化、repository 和服务入口路径可能不再执行。若其他集成测试仍覆盖它们，总覆盖率通常变化很小；否则会下降。当前 B/C 只测得约 3.19 秒，不能假定其重写能带来显著提速，更不能预报具体覆盖率增减而不实施复测。
3. **从大量服务测试抽出输入矩阵，同时保留代表性集成断言**：可在 unit 增加边界分支、提升领域覆盖，且减少重复装配；但它不会自然补回 HTTP、CAS、回滚、rehydrate、索引发布的分支。这些仍由真集成负责。删除真集成来让 unit 比例更好看，会把整体门槛打穿。

当前并集仅高于门槛约 5.22 个百分点；固定分母下相当于约 2,089 个覆盖项的余量，不是允许删掉 5.22% 测试的配额。每一步实施后比较源码行/分支缺口，保留全局 80%，不通过排除 `storage`、SDK、降低阈值或只报告某个目录的覆盖率来“达标”。分片运行的数据必须先合并再做门槛检查。

## 6. 执行记录与限制

本次不移动、不改写测试，不调整 scope、重复次数、coverage 配置或 CI。性能采样不是发布验收，也不将本机结果承诺为 GitHub `ubuntu-latest` 的结果。

### 6.1 可复现命令与原始输出

工作目录：`/Users/cassia/Local/Code/iris_memory_core`。使用既有 `.venv`，未安装依赖；pytest wall time 不含 bootstrap、lint、mypy、TS/browser 或打包。以下 `R` 指本次临时证据目录，所有 coverage 文件均重定向到其中；对子集设置 `--cov-fail-under=0` 只是为了得到完整诊断输出，并未改变生产门槛或覆盖计数。

```sh
R=/tmp/iris-test-layering-20260907

# 结构与收集；只读。
git rev-parse HEAD
.venv/bin/python -m pytest --collect-only -q --no-cov -p no:cacheprovider

# 全量：保留 pyproject 的 Core + SDK、branch 与 fail-under=80。
PYTHONPATH="$R:src:sdk/python/src:." \
COVERAGE_FILE="$R/.coverage-full" LAYER_PROFILE="$R/full-profile.json" \
.venv/bin/python -m pytest -p layer_profile -p no:cacheprovider \
  --durations=25 --cov-report= --cov-report="json:$R/full-coverage.json" \
  -q > "$R/full.log" 2>&1

# 端口补测；运行环境须允许 127.0.0.1 临时监听。
PYTHONPATH="$R:src:sdk/python/src:." \
COVERAGE_FILE="$R/.coverage-mock" LAYER_PROFILE="$R/mock-profile.json" \
.venv/bin/python -m pytest tests/contract/test_mock_server.py \
  -p layer_profile -p no:cacheprovider --durations=25 --cov-fail-under=0 \
  --cov-report= --cov-report="json:$R/mock-coverage.json" \
  -q > "$R/mock.log" 2>&1

# 统一分母的 unit-only；将 tests/unit 换成 --ignore=tests/integration
# 即排除 integration 的对照，并分别使用 without-integration 的输出文件名。
PYTHONPATH="$R:src:sdk/python/src:." \
COVERAGE_FILE="$R/.coverage-unit-normalized" \
LAYER_PROFILE="$R/unit-normalized-profile.json" \
.venv/bin/python -m pytest tests/unit -p layer_profile -p no:cacheprovider \
  --cov-reset --cov=src/iris_memory_core --cov=sdk/python/src/iris_memory_sdk \
  --durations=25 --cov-fail-under=0 --cov-report= \
  --cov-report="json:$R/unit-normalized-coverage.json" \
  -q > "$R/unit-normalized.log" 2>&1
```

原始文件：`full.log`、`full-profile.json`、`full-coverage.json`；`mock.log`、`mock-profile.json`、`mock-coverage.json`；`unit-normalized.log`、`unit-normalized-coverage.json`；`without-integration.log`、`without-integration-profile.json`、`without-integration-coverage.json`；`combined-coverage.json`；`schema-bench.json`、`schema-bench-idle.json`。该临时目录不属于仓库持久证据，清理后会丢失；本报告保留了关键数字、口径和可重建探针，未把本机临时路径当作永久发布附件。

探针 `layer_profile.py` 的实现如下。`MigrationRunner.migrate` 只增加计时包装并调用原实现，未缓存、跳过或改写 SQL。主进程 fixture/node 归因通过 pytest hooks 完成；不对 SIGKILL 子进程声称做了细分计时。

```python
import json, os, time
from pathlib import Path
import pytest

reports, fixtures, migrations, items = [], [], [], []
current = {"nodeid": "", "phase": "collection"}

@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_setup(item):
    current.update(nodeid=item.nodeid, phase="setup")
    yield

@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    current.update(nodeid=item.nodeid, phase="call")
    yield

@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_teardown(item):
    current.update(nodeid=item.nodeid, phase="teardown")
    yield

@pytest.hookimpl(hookwrapper=True)
def pytest_fixture_setup(fixturedef, request):
    start = time.perf_counter()
    yield
    fixtures.append({"name": fixturedef.argname, "scope": fixturedef.scope,
                     "nodeid": current["nodeid"], "seconds": time.perf_counter()-start})

def pytest_runtest_logreport(report):
    reports.append({"nodeid": report.nodeid, "phase": report.when,
                    "seconds": report.duration, "outcome": report.outcome})

def pytest_collection_modifyitems(session, config, items):
    globals()["items"] = [{"nodeid": i.nodeid, "fixtures": i.fixturenames} for i in items]

def pytest_configure(config):
    from iris_memory_core.storage.migrations import MigrationRunner
    original = MigrationRunner.migrate
    def measured(self, *args, **kwargs):
        start, context = time.perf_counter(), dict(current)
        try:
            return original(self, *args, **kwargs)
        finally:
            migrations.append({**context, "seconds": time.perf_counter()-start})
    MigrationRunner.migrate = measured

@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(session, exitstatus):
    Path(os.environ["LAYER_PROFILE"]).write_text(json.dumps({
        "exitstatus": int(exitstatus), "items": items, "reports": reports,
        "fixtures": fixtures, "migrations": migrations}, indent=2))
```

coverage 并集使用 `coverage.CoverageData.read()` 读取 `.coverage-full`、`.coverage-mock`，通过 `Coverage.get_data().update()` 合并，保存到 `.coverage-combined` 后生成 JSON。没有把不同运行的百分比相加或取平均。微基准脚本 `bench_schema.py` 依次计时 fresh migrate、同库 no-op migrate、复制关闭模板、真实 Store 首读，各 40 次，单独校验副本；结果仅用于比较初始化机制。

### 6.2 `--durations=25` 全量输出摘录

以下 25 项均为 call；完整 phase 分解见 §3。这也说明只看前 25 名会漏掉累积 117.60 秒、但分散到两千余次的 setup 迁移。

```text
28.32s test_phase8_concurrency.py::test_reads_race_rebuilds_without_torn_results
24.95s test_state_latency.py::test_state_coalesced_write_p95
20.49s test_phase10_pipeline.py::test_dead_letter_replay_one_hundred_times_keeps_one_logical_episode
19.13s test_phase7_vector.py::test_model_switch_never_mixes_spaces_1000_revisions
17.00s test_phase6_recall.py::test_hard_filter_dimensions_exclude_violating_candidates[privacy]
15.95s test_phase6_recall.py::test_hard_filter_dimensions_exclude_violating_candidates[tenant]
15.15s test_phase6_recall.py::test_hard_filter_dimensions_exclude_violating_candidates[session]
15.14s test_phase6_recall.py::test_hard_filter_dimensions_exclude_violating_candidates[space]
14.96s test_phase6_recall.py::test_hard_filter_dimensions_exclude_violating_candidates[agent]
14.21s test_phase6_recall.py::test_hard_filter_dimensions_exclude_violating_candidates[space_group]
14.18s test_phase6_recall.py::test_hard_filter_dimensions_exclude_violating_candidates[status]
13.62s test_forget_latency.py::test_forget_canonical_effect_p95_under_100ms
13.55s test_phase6_recall.py::test_hard_filter_dimensions_exclude_violating_candidates[tombstone]
13.29s test_phase6_recall.py::test_hard_filter_dimensions_exclude_violating_candidates[time]
12.55s test_phase6_recall.py::test_hard_filter_dimensions_exclude_violating_candidates[as_of]
 9.73s test_phase8_graph.py::test_malicious_high_connectivity_never_exceeds_budgets
 8.62s test_focus_items.py::test_item_cap_evicts_lowest_activation_to_dormant
 8.53s test_phase8_graph.py::test_deadline_stops_expansion
 7.79s test_focus_items.py::test_decay_sweep_is_idempotent_and_pure
 7.79s test_phase10_pipeline.py::test_invalid_and_over_limit_provider_outputs_are_persisted_twenty_times
 7.71s test_console_operation_batches.py::test_fixed_batches_resume_with_a_new_worker_and_finish_once[500]
 7.46s test_phase8_profile.py::test_every_nonempty_field_has_a_valid_source_claim_200_subjects
 6.43s test_phase8_graph.py::test_status_property_200_cases
 6.24s test_phase8_graph.py::test_edge_reads_are_bounded_against_malicious_hubs
 5.54s test_console_forget_selection.py::test_selector_rejects_empty_or_oversized_set_without_storing_preview[501]
```

### 6.3 验证边界与未执行项

- 本机是共享诊断环境；全量期间还做过少量收集、单文件排查、mock 补测和微基准，不是硬件独占基准。无“同一全量无探针”A/B，未校准覆盖率与探针的净开销，不能据此推荐关闭 coverage 来保证提速。
- fault 阶段在全量进程为 113.81 秒 call，在排除 integration 的独立运行中为 152.14 秒，存在明显波动。时间预算必须留余量并复测，不能把不同运行的目录时间简单相减当作精确收益。mock teardown 两轮仍稳定约 27 秒。
- 所有正常完成的断言均通过；首轮 54 个环境错误的补测也通过，但未重跑一次允许端口的完整全绿命令。本报告记录的是诊断证据，不是新的发布通过证明。
- 两分钟优化、模板隔离、分层迁移、xdist/CI 分片、随机顺序与优化后覆盖差异均未实施、未验证；工程日和节省预算为估算。
- 分析期间既有文档/hosts 整理在其他工作中被提交；结束时 HEAD 为 `49d871cbe3f1d22245c77566fc6763f86090bb36`。相对开始基线检查 `tests`、`src`、`sdk/python`、`pyproject.toml`、CI 文件无差异，因此本报告的被测代码候选保持不变。本次没有执行提交，结束时唯一未跟踪文件是本报告。
- 报告交付前运行 `python -m tools.check_docs` 与文档 diff/空白检查；测试、源码与配置无本次修改。
