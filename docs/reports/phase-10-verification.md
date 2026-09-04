# Phase 10 验证报告：巩固、Reflection 与传输层

> 状态：Completed（实现 + 门禁实测 + 收尾阶段补测后全量重跑）
> 日期：2026-09-05
> 基线 commit：`1241a28`（test: derive legacy snapshot rewind coverage）之上的工作区
> 硬件：Apple Silicon（darwin 25.6.0 arm64），本地 SQLite 3.50.4（测试显式 pin，等价部署 pin）
> 验收文档：[phase-10](../development/phase-10-consolidation-reflection.md) · 决策：[ADR-0019](../adr/0019-phase10-consolidation-transport.md)

本报告只记录实际运行结果。全部数字来自本轮 `make ci` 与专项测试的实测输出；未运行的项
一律记为"未验证"，不按设计意图推定通过。

## 1. 版本与契约

| 项 | 值 |
| --- | --- |
| Core / Python SDK / TypeScript SDK | 0.11.0 |
| Schema | 11（migration `0011_phase10_consolidation_transport.sql`，min_app=0.11.0，online_safe=true，lock_ms=200，recovery=none） |
| 契约版本 | 1.9.0（additive：capability 32 → 48；错误码不变；OpenAPI 新增 16 条路径） |
| OpenAPI 路径 / operation | 85 / 91（Phase 9 为 69） |
| fixtures | 136 manifest cases（Phase 9 为 123；新增 13 个 Entity/Identity/Binding/SpaceGroup/Admin 用例） |
| JSON Schema 文件 | 84（Phase 9 为 75） |
| 错误码总数 | 41（不变；真源：`contracts/source/contracts.json`，ADR-0017 §1） |
| Runtime 兼容窗口 | [10, 11]（`SUPPORTED_SCHEMA_MIN=10`、`SUPPORTED_SCHEMA_MAX=11`） |
| 新启用 job kinds | `episode.consolidation`、`reflection.generate`、`memory.reconciliation`、`persona.evaluation`——四个全部 `enabled=True`。**登记表中不再有 disabled 占位** |
| 新依赖 | 运行时新增 `fastapi>=0.116,<1`、`uvicorn>=0.35,<1`，`jsonschema` 由 dev 提升为运行时；dev 新增 `httpx`（TestClient） |

Migration checksum（SHA-256，`MigrationRunner` 记录于 `schema_migrations`）：

```
bb5bdbe6b4e40e2ad14a8d3664708dd125f31731496ab252cca298e9d8db79ad  migrations/0011_phase10_consolidation_transport.sql
```

0011 创建 10 张 STRICT 表：`consolidation_windows`、`reflection_records`、
`reflection_evidence`、`cognitive_candidates`、`provider_outcomes`、
`provider_circuit_states`、`provider_budget_states`、`service_credentials`、
`service_events`、`recall_usage_activations`。

新增的 16 条路径全部为 additive：

```
/v1/entities/{entity_id}                    /v1/entities/{entity_id}/relations
/v1/identities                              /v1/bindings:prepare
/v1/bindings/{binding_id}:confirm           /v1/bindings/{binding_id}:revoke
/v1/space-groups                            /v1/space-groups/{gid}/spaces/{sid}:bind
/v1/space-groups/{gid}/spaces/{sid}:unbind  /v1/admin/indexes/{kind}:rebuild
/v1/admin/backups                           /v1/admin/exports
/v1/admin/audit-events                      /v1/admin/reflections:dry-run
/v1/admin/reflections/{reflection_id}:replay
/v1/events（SSE，capability `events.sse.v1`，可配置关闭）
```

`/v1/admin/recent-context:rebuild` 在 OpenAPI 中标记 `deprecated: true`，negotiation 的
`deprecated_capabilities` 把 `admin.recent-context-rebuild.v1` 指向
`/v1/admin/indexes/recent_context:rebuild`（ADR-0006、ADR-0017 §2）。

**两个 Capability 命名空间不要混淆**（同 Phase 9）：`AccessContext` 的授权串是
`admin.backup.v1`/`admin.export.v1`/`admin.audit.v1`/`admin.index-rebuild.v1`/`bindings.v1`/
`identities.v1`/`space-groups.v1` 等进程内权限判定名；契约协商面同名声明的是宿主可用能力。
二者在本阶段刻意取了相同字符串以便审计对照，但仍是两张表，不自动同步。

## 2. 量化门禁实测

| 基线要求 | 实测 | 证据 |
| --- | --- | --- |
| 相同快照/版本/参数连续重放 3 次候选一致 | 3 次 fingerprint、candidate_id、run_fingerprint 各 1 个不同值；`candidate_diff` 全 `unchanged`；固定 `source_revision` 的 3 次重复 enqueue 收敛到 1 window / 1 run / 1 candidate / 1 claim | `test_same_snapshot_versions_and_parameters_replay_three_times_identically`、`test_pipeline_commits_evidence_bound_claim_and_is_replay_stable` |
| 每类非法候选 ≥200 例，非法提交成功数 0 | 纯域 10 类 × 200 = 2000 例，逐条落到指定 `RejectReason`；数据库绑定 3 类 × 200 = 600 例经真实 Worker 提交，`claims`/`relations` 计数 0、拒绝记录 200 | `test_each_illegal_candidate_class_commits_zero`、`test_two_hundred_database_bound_illegal_candidates_commit_zero` |
| Provider 故障/熔断/预算各 ≥20 轮 | 4 类故障（timeout / 5xx / 429 / 预算耗尽）× 20 轮；熔断半开 Probe 20 轮各只放行 1 个请求；非法与超限输出 20 轮全部落成 `provider_outcomes` 低敏记录且无 Canonical 写入 | `test_provider_failure_injection_is_bounded`、`test_circuit_breaker_allows_only_one_bounded_probe`、`test_invalid_and_over_limit_provider_outputs_are_persisted_twenty_times` |
| Fencing 重复 ≥50 次，过期 Job 提交成功数 0 | 50 次"预备 → 变更来源 → 提交"序列，Canonical 提交成功数 0 | `test_stale_source_mutations_fence_fifty_prepared_commits` |
| Dead Letter 重放 100 次不产生重复逻辑资源 | 100 次以新 Outbox ID 重放后逻辑 Episode 仍为 1，`replay_of` 指向原任务 | `test_dead_letter_replay_one_hundred_times_keeps_one_logical_episode` |
| 积压指标报告 Job Kind / Oldest Pending / 候选/拒绝数量 / 成本 | `/metrics` 返回 `iris_outbox_jobs{job_kind,status}`、`iris_outbox_oldest_pending_age_us`、`iris_reflection_candidates{decision}`、`iris_provider_outcomes` 与 `iris_provider_cost_microunits{provider_kind,outcome}`；泄漏扫描无正文/Secret/完整 External ID | `test_backup_export_audit_sse_and_no_sensitive_error_leakage` |
| 每条已发布路径都有真实实现 + 成功/失败双向用例 | 85 条路径 / 91 个 operation 与 ASGI 路由一一对应，未实现路径数 **0**；成功面与失败面两遍各覆盖全部 91 个 operation | `test_every_frozen_operation_has_exactly_one_real_asgi_route`、`test_every_operation_has_a_success_case`、`test_every_operation_has_a_failure_case` |
| 41 个错误码各有一条传输层用例；越权矩阵 Body 提权成功数 0 | 41/41 有显式映射来源且各有一条真实产生它的用例；Tenant/Agent/SpaceGroup/Space/Entity 双向矩阵下 Body 提权成功数 0 | `test_every_stable_error_code_has_a_transport_mapping`、`test_every_stable_error_code_is_emitted_by_the_transport`、`test_body_narrowing_bidirectional_scope_and_authority_matrix`、`test_bearer_auth_narrowing_management_and_new_surface` |
| `serve`/`worker` 启动 / 优雅关闭 / 强杀恢复各 ≥20 次 | `serve` 干净环境 20 次启动 + Ready + 优雅关闭；`worker` 20 次；Reconciliation 提交边界 `pre_commit`/`post_commit` 各 20 次 SIGKILL，恢复后恰好 1 Claim / 1 物化候选 / 1 完成任务。全部远低于默认 30 s Grace Deadline | `test_clean_asgi_start_and_default_graceful_shutdown_twenty_times`、`test_worker_clean_start_and_shutdown_twenty_times`、`tests/fault/test_kill9.py::TestReflectionKill9` |

**未验证项**（不按设计意图推定通过）：

- `serve()` 本身的 uvicorn 绑定、`timeout_graceful_shutdown` 计时与连接排空未单独验证：
  20 次启动/Ready/优雅关闭走的是 `create_app` + ASGI lifespan（`TestClient`）；
  进程级 SIGKILL 也未构造（见 §5 已知限制 4）。
- 在线路径的延迟门禁未在传输层之上重测：Phase 6–9 的 `tests/performance/` 仍直接打应用层，
  HTTP 开销未计入。
- SSE 的跨进程扇出、连接上限与慢消费者断开只在单进程 `TestClient` 下验证。

## 3. 收尾阶段修复的缺陷

### 3.1 Reconciliation 的 Canonical 提交边界没有强杀证据（真实缺口）

退出门禁要求 `serve`/`worker` 从 `kill -9` 恢复且重复 ≥20 次。既有
`tests/fault/test_kill9.py` 覆盖 Phase 2–5 的 observe / worker / tick / state / focus /
projection / task / trigger / event-ack / forget 边界，**但没有任何场景落在 Phase 10 新增的
"候选 → Canonical Claim"提交上**——而这正是本阶段唯一新增的、由后台 Provider 结果驱动的
写事务。缺口是证据缺口，不是实现缺陷。

修复：在 `tests/fault/kill9_scenarios.py` 新增 `reflection` 场景。它先把 consolidation 与
reflection 两级跑完，只在**领取到 `memory.reconciliation` 之后、执行提交之前**装载
`_die_before_commit`，因此 claim 事务已提交、业务写与 fenced 完成 CAS 同处于将被强杀的那个
事务里；`post_commit` 变体则在提交成功后立刻 SIGKILL。父进程用 `phase10_drain` 以同一确定性
Provider 重放。

实测（各 20 次）：

- `pre_commit`：崩溃后 `claims = 0`、物化候选 = 0、已完成 reconciliation = 0，而 window 与
  reflection run 因为在更早的事务中提交而存活；恢复后收敛到恰好 1 / 1 / 1。
- `post_commit`：崩溃后即已是 1 / 1；恢复不产生第二条 Claim。

### 3.2 `serve`/`worker` 的启动失败会抛出 Python traceback（真实缺陷）

`cli.main` 只捕获 `MigrationError`。`serve`/`worker` 的启动校验有两类失败——配置校验的
`ValueError`（数据目录不合法、端口越界、backup/export 目录相同）和运行时校验的
`DomainError`（`RuntimeNotAllowedError`、`SchemaIncompatibleError`）——两类都直接冒泡成
traceback。实测：

```
$ uv run iris-memory-core worker --database <dir>/core.sqlite3 --once
Traceback (most recent call last):
  ...
iris_memory_core.domain.errors.RuntimeNotAllowedError: sqlite_runtime_not_allowed: ...
```

§35.4 的启动顺序是"验证配置/runtime → …"，验证失败应当是可诊断的退出，不是崩溃。修复后
两类失败都打印 `"<command> startup failed: <diagnosis>"` 到 stderr 并返回 1；回归见
`test_serve_and_worker_report_startup_failures_without_a_traceback`。

顺带修正 README：本机 SQLite（3.50.4）不在部署 allowlist 内，README 里的 `serve`/`worker`
示例缺 `--allow-local-sqlite`，照抄会直接失败。

### 3.3 `ruff format` 在 `tests/integration/test_phase10_runtime.py` 上漂移

`make format-check` 失败（`1 file would be reformatted`），其余 204 个文件干净。这是唯一一个
未通过的 CI 门禁，已 `ruff format` 修正，无语义变化。

### 3.4 阶段文档"阶段目标"中的路径数字过时

phase-10 文档写着"已发布 OpenAPI 的 61 条路径至今没有真实服务端"——61 是 Phase 8 结束时的
数字，Phase 9 交付后是 69。已更正为 69。

### 3.5 传输层交付后仍留有"无 HTTP 传输层"的现状声明

`README.md`、`sdk/python/README.md`、`sdk/typescript/README.md` 与
`tools/mock_server.py` 的模块 docstring 都还断言 Core 没有传输层。ADR-0017 §3 与
phase-09 已知限制 6 的纪律是"在传输层交付前不得移除"，交付后必须更新——否则新读者会照着
过时前提去搭 Adapter。四处均已改为当前事实，并把 Phase 8/9 文档里的同一条已知限制标注为
"Phase 10 已解除"而不是删除（保留历史记录）。

## 4. 全量门禁

`make ci` 端到端通过（exit 0）：

```
ruff format --check       205 files already formatted
ruff check                All checks passed
import boundaries         ok
check_docs                ok（15 个阶段文档 + 锚点 + 仓库根 Markdown）
mypy                      Success: no issues found in 204 source files
tsc --noEmit              ok
generate_contracts --check ok
check_compatibility       ok
pytest                    10338 passed（8 分 02 秒）
coverage                  83.55%（门槛 80%）
sdk-test（node）          14 passed
```

用例总数 10338 = Phase 9 结束时的 10335 + 本轮收尾新增 3
（`TestReflectionKill9` 2 个 + `test_serve_and_worker_report_startup_failures_without_a_traceback` 1 个）。
Phase 10 自身新增 **2169** 个用例：`tests/unit/test_phase10_reflection.py` 2101、
`tests/contract/test_phase10_asgi.py` 47、`tests/integration/test_phase10_pipeline.py` 12、
`tests/integration/test_phase10_migration.py` 4、`tests/integration/test_phase10_runtime.py` 3、
`tests/contract/test_phase10_operation_matrix.py` 2。

**性能门禁在全量跑下不稳定（预先存在，与 Phase 10 无关）**：除 `make ci` 那次之外，另外两次
独立的全量 `pytest` 各命中一个 p95 预算断言失败，且两次不是同一个用例
（`test_hybrid_recall_p95_within_budget`、`test_structured_recall_p95_under_50ms`）。两个用例
单独运行、以及 `tests/performance/` 整体运行（11 passed）都通过。失败原因是 10k 用例 +
coverage 插桩下的挂钟抖动，不是召回路径回归；本报告如实记录，不当作已解决。相关的稳定化
（固定预算基准或把性能门禁移出全量跑）归后续阶段。

## 5. 已知限制

Phase 10 的已知限制记在
[phase-10](../development/phase-10-consolidation-reflection.md#已知限制)，共 6 条：
随包 Provider 是确定性 Fake、SSE 只在单进程验证、`/metrics` 是每租户 JSON 快照而非
Prometheus 文本、`serve` 未单独构造进程级 SIGKILL、Dead Letter 没有独立管理端点、
旧 rebuild 路径的弃用窗口未到期。

Phase 5–9 曾逐阶段登记的"无 HTTP 传输层"一条自本阶段起解除：85 条已发布路径全部有真实
ASGI 实现并通过成功/失败双向契约测试。历史阶段文档中的该条目保留原文并加注解除说明，
不做删除。
