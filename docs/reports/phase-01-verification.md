# Phase 1 Verification Report

> 归档证据：以下版本、测试数量、耗时与覆盖率是本阶段执行时的历史快照，未在本次文档整理中重跑；不能作为当前发布已通过的证明。当前状态见[阶段索引](../development/README.md)，发布重验见[Phase 14](../development/phase-14-hardening-release.md)。
> 后续闭环：HTTP/进程入口已由 [Phase 10](../development/phase-10-consolidation-reflection.md)交付；旧报告中的应用层/mock 范围只描述当时环境。

> Result: Passed（`make ci` exit 0）  
> Date: 2026-08-29（评审修复：2026-08-30、2026-08-31 ×2）  
> Implementation commit: `aecedd379669ae6a21319fd0fc80551dfd75c2d6`

## Environment

| Component | Version |
| --- | --- |
| macOS architecture | Apple Silicon (`arm64`) |
| Python | 3.12.13 |
| SQLite（本地开发运行时） | 3.50.4 |
| uv | 0.11.29 |
| Node.js / npm | 26.5.0 / 11.17.0 |

本地开发 SQLite 3.50.4 **不在**官方 Runtime Allowlist（≥ 3.51.3 或 3.50.7 / 3.44.6）内；
集成测试按部署 Pin 模式显式声明 `allowed_versions` 使用本地运行时，官方清单仍是
`SQLiteRuntime` 的默认值，未在 Allowlist 的运行时会在 Ready 检查与 `connect()` 处被拒绝。

## Reproducible gate

```bash
make ci
```

第四轮修复后的完整门禁复验结果：

- Ruff format check / lint：通过。
- Domain import boundary：通过（Domain 仅依赖标准库）。
- Documentation structure and local links：通过。
- Mypy strict（含 tests/tools）：55 个源文件，零问题。
- TypeScript strict check：通过。
- 契约生成无漂移、v1 兼容快照：通过（错误码仅新增）。
- Python 测试：**139 passed**，覆盖率 **89.66%**（阈值 80%）；其中两个 localhost
  mock-server 契约测试均完成真实监听与 SDK 协商。
- TypeScript SDK Fixture：1 passed。

## 针对性评审修复（第一轮，2026-08-30）

| 评审问题 | 修复 | 回归测试 |
| --- | --- | --- |
| Phase 0 库无法升级（0001 被追加头部） | 0001 恢复为已发布原始字节（SHA-256 `ec236f7e…861`）；元数据头仅对 ≥ 0002 强制，0001 作为已发布文件豁免 | `test_phase0_database_upgrades_to_phase1`（用原始 checksum 构造 Phase 0 库后升级） |
| 幂等租约过期可双执行 | 引入 `owner_token`：过期认领是对 `status='failed_replayable'` 的 CAS，业务事务的完成更新以 owner token 为条件，被抢占者的整个事务回滚 | `test_expired_lease_grants_execution_to_exactly_one_worker`（副作用恰好 1 次） |
| 同租户横向写越权 / SpaceGroup 历史绕过 | `create_space` 强制 agent/group 在调用方授权集合内；创建即绑定组需 admin+原因码，同一事务写入 `space_group_bindings` 历史；`insert_space` 不再接受 `space_group_id` | `test_create_space_blocks_lateral_writes_outside_grants` |
| Tombstone 实体经身份视图复活 | `identity_view` 对 at_ingest/current 解析结果做 Tombstone 同步排除（含 Redirect 终点） | `test_tombstoned_entity_does_not_resurrect_through_identity_views` |
| 备份 manifest 领先快照 / 校验不全 | manifest 元数据改从快照本身读取；checksums.txt 覆盖 canonical+manifest+fingerprint；verify 将 manifest 水位与快照对账；指针校验要求 persona 属于该 Agent | `test_checksum_tampering_and_coverage_gaps_are_detected`、`test_tampered_manifest_watermarks_are_reconciled_against_snapshot` |
| 字段权威可伪造 / 同值不提升 | 权威声明按 capability 门控；同值更高权威 SUPERSEDE（提升） | `test_field_authority_cannot_be_spoofed_and_same_value_promotes` |
| 并发 confirm 不保证 conflicted | confirm 收敛为单事务（rival 检查+迁移+挂起同快照），唯一索引竞争也走挂起路径，提交后抛稳定 `binding_conflict` | `test_concurrent_confirmation_parks_exactly_one_binding_in_conflicted` |
| Privacy 标签语法 | 改为规范 `entity:<id>:private` | 单元与集成测试更新 |
| read 无快照 | `Store.read()` 以 BEGIN DEFERRED…COMMIT 固定单一快照 | UoW 行为内建 |

## 针对性评审修复（第二轮，2026-08-31）

| 评审问题 | 修复 | 回归测试 |
| --- | --- | --- |
| **阻断 1**：旧快照可抢占新租约（B 用过期观察把 A 的新租约降级，业务回调执行两次） | 恢复 CAS 同时匹配观察到的 `owner_token` + `expires_us` + `status='in_progress'` 并检查 rowcount；失配即快照过期 → 重载后在有界次数内重新分派；B 观察到 A 的未过期新租约时直接 `idempotency_in_progress`，**回调不再第二次执行** | `test_stale_recovery_snapshot_cannot_steal_a_fresh_lease`（定向时序复现：B 的恢复 no-op、A 的租约存活、B 被阻塞） |
| **阻断 2**：回放返回当前聚合而非首次结果；`expected_revision` 不进指纹；`app_instance_id` 硬编码 `"core"` | 幂等 outcome 保存完整记录快照（`record_snapshot`/`record_restore`），回放返回首次快照；指纹覆盖全部有效参数（含 `expected_revision`、payload、reason）；记录键使用 `access.app_instance_id`，不同 App 实例同键互不冲突 | `test_idempotent_replay_returns_the_first_outcome_not_current_state`（回放返回 verified/rev2 而库中已 rev3）、`test_idempotency_fingerprint_covers_expected_revision`、`test_idempotency_records_partition_by_app_instance`、`test_provisioning_idempotency_partitions_by_app_instance` |
| **阻断 3**：Tombstone 经集合查询复活（`verified_binding_for`/`bindings_for`/Redirect 查询无过滤） | 三类查询加 `NOT EXISTS resource_tombstones` 过滤；Redirect 边以**源实体**删除为准（源被删→边退出解析；目标被删→保留边使解析到达已删终点后被置空，绝不回落到合并前实体） | `test_tombstoned_binding_is_excluded_from_collections_and_views`（评审复现路径：绑定删除后 `identity_view` 双视图为空）、`test_redirect_edges_from_tombstoned_sources_drop_out_of_resolution` |
| **阻断 4**：`verify_backup()` 对 Phase 0 快照抛 `no such table`；CLI 只建目录即置 `backup_performed` | 校验按表存在性跳过 Phase 1 专属检查（schema 窗口放宽至 1..MAX 以接受迁移前备份）；CLI `--with-backup` 建**并验证**备份，验证失败 exit 1 且不置 `backup_performed` | `test_phase0_pre_migration_backup_verifies_and_restores`、`test_cli_with_backup_blocks_when_verification_fails` |
| 高优：备份无法抵御重算哈希的篡改 | 新增外部可信根：`signing_key`（CLI `--backup-key-file`）→ 备份携带 `authenticity.tag`（对 payload 摘要的 HMAC-SHA256）；带密钥验证时从实际文件重算 tag，改库+重算 checksums 无法通过。无密钥时保持"意外损坏检测"语义并在文档明示 | `test_signed_backup_detects_recomputed_checksum_tampering`（攻击者改库+重算 manifest/checksums：无密钥通过、有密钥拒绝、错误密钥拒绝）、`test_keyed_verification_requires_an_authenticity_tag` |
| 高优：恢复切换非原子、无启动恢复、无目录 fsync | 切换改为 journal 化两段 rename：journal 在首个 rename 前持久化（文件+目录 fsync）、切换完成后删除；新增 `recover_pending_switch()`（CLI `recover-switch`）按 journal 确定性完成/回滚；`restore_backup` 入口先清理遗留状态 | `test_interrupted_restore_switch_is_recovered_deterministically`（四种 Kill-9 时序：completed/aborted/completed/rolled_back）、`test_restore_cleans_pending_state_before_switching` |
| 高优：创建并绑定 Space 一个事务推进两次水位 | `Transaction` 内累计水位推进（聚合取最终 revision），**提交时**每 (tenant, agent) 恰好一次 `current_seq+1`；读事务调用即报错 | `test_watermark_is_monotonic_with_entries`（重写：一事务两次调用收敛为一次推进+最终 revision 条目）、`test_create_and_bind_space_advances_watermark_once_with_final_revision`（水位 1→2、条目 `space/rev2`）、`test_read_units_of_work_reject_watermark_advances` |
| 高优：写接口幂等覆盖不全 / 未注入 manager 时静默非幂等 | Identity 与 Provisioning 全部写接口（create_entity、register_external_identity、propose/confirm/revoke、redirect、tombstone、record_attribute、create_agent/space_group/space/session、update/bind/unbind）接受 `idempotency_key`；传键但未注入 runner → 稳定错误 `idempotency_unavailable` | `test_provisioning_writes_accept_idempotency_keys`、`test_idempotency_key_without_runner_fails_loudly`、`test_provisioning_idempotency_key_without_runner_fails_loudly` |
| 版本面不一致（module/SDK 仍 0.1.0） | `iris_memory_core.__version__`、Python SDK、TypeScript SDK（含 lockfile）统一 **0.2.0**；决策：三包同一发布列车（见阶段文档） | `make ci` 契约与元数据一致性检查 |
| Migration 版本比较非语义版本（0.2.0rc1 被当作 ≥0.2.0） | `_version_tuple` 改为预发布感知排序（dev < a < b < rc < final < post，非法字符串报 `MigrationMetadataError`）；`recovery` 枚举校验（none/backup）；`lock_ms` 落实为迁移连接的 `PRAGMA busy_timeout` | `test_version_ordering_is_prerelease_aware`、`test_release_candidate_does_not_satisfy_min_app`、`test_unknown_recovery_mode_is_rejected`、`test_lock_ms_is_enforced_as_busy_timeout` |
| Persona 指针校验不足（draft/retired/跨租户可通过） | 不变量加 `p.status='published'` 与 `p.tenant_id = a.tenant_id` | `test_persona_pointer_invariants_require_published_same_agent_revision` |
| admin 绕过 capability 的语义含糊 | 明确并写入交付说明：字段权威要求 **capability 或管理平面（admin）**，`admin` 是平台运营方的全权代表（服务端派生、不可自授） | `test_field_authority_requires_capability_or_management_plane`（无能力拒绝 / 仅 capability 通过 / admin 通过，三者都断言） |

## 针对性评审修复（第三轮，2026-08-31）

| 评审问题 | 修复 | 回归测试 |
| --- | --- | --- |
| **P1**：恢复 HMAC 校验 TOCTOU（verify 后、copy 前篡改源目录可恢复未认证内容） | `restore_backup` 改为**先整目录复制到 staging，再对 staging 中将切换的实际字节执行全部校验**（checksums/HMAC/manifest 对账/不变量），不存在 verify→copy 窗口 | `test_restore_verifies_the_bytes_it_switches`（攻击落在复制后：恢复的仍是原始字节；staging 被篡改则拒绝） |
| **P1**：业务异常把幂等键遗留为活动租约（revision_mismatch 漂移为最长 30s 的 in_progress） | `run()` 捕获业务异常后按 owner token CAS 把租约降级为 `failed_replayable`（best-effort，失败由租约过期兜底），立即重试会重新执行并抛出同一稳定错误 | `test_business_failure_releases_the_idempotency_lease`（reject→降级→重试成功；无 in_progress 阻塞） |
| **P1**：journal 可操作目标目录之外的路径（`{"staging":"../victim"}` 触发外部 rmtree） | journal 条目只信任本模块命名模式（staging 必须是 `<target>.restoring`、aside 必须匹配 `<target>.previous-*`，均为纯 basename 且拒绝 symlink）；不可信/坏 JSON → `invalid_journal`，不触碰任何路径 | `test_recovery_refuses_untrusted_journal_paths`（四种恶意/畸形 journal，victim 目录完好）、`test_corrupt_journal_is_reported_not_raised` |
| **P1**：journal 写入非原子、恢复无跨进程串行化 | journal 经 tmp+fsync+rename 原子发布；`restore_backup`/`recover_pending_switch` 全程持有 `<target>.restore-lock` 的 flock（超时抛稳定 `conflict`），staging 清理与 journal 状态不再可能互相竞争 | `test_restore_switch_lock_serializes_concurrent_operations`（持锁期间二次加锁 ConflictError；释放后可重入） |
| **P1**：Schema Ready 检查未接入应用 Store（schema 1 可正常打开） | `Store.read()/write()` 的每个连接都经 `connect(verify_schema=True)`：越界/未迁移库在任何事务开始前抛稳定 `schema_incompatible`（含被恢复切换换入的库）；`current_schema_version` 对无表库返回 0 | `test_store_rejects_out_of_window_and_unmigrated_databases` |
| **P1**：备份文件无持久原子发布（write_text、仅目录 fsync） | 完整备份集在 `<dest>.staging-<id>` 临时目录构建，canonical/manifest/fingerprint/tag/checksums 逐文件 fsync + 原子写入，最后单次 rename 整体发布 + 父目录 fsync；失败自动清理临时目录 | `test_backup_publication_is_atomic_and_cleans_up_on_failure`（成功无残留、失败无半发布目录） |
| P2：verify_backup 对坏 JSON/坏库抛异常 | 解析与对账全程捕获（JSON/SQLite/OS/Unicode/ValueError）→ 返回 `RestoreCheck(False, ("backup unreadable: …",))` | `test_verify_backup_returns_check_for_corrupt_content`（坏 manifest 与非数据库 canonical 均为失败检查） |
| P2：崩溃遗留 migration_runs `started` 行永久悬挂 | `migrate()` 每次运行对账：`started` 且版本已在 schema_migrations → `completed`；版本缺失 → `failed` | `test_interrupted_migration_run_rows_are_reconciled`（已应用→completed、未应用→failed） |
| P2：幂等快照无格式版本，未来加字段回放 KeyError | 快照带 `snapshot_version` 信封；`record_restore` 忽略未知键、缺字段回退声明默认值，仅缺必填字段报错（错误信息写明"新字段必须带默认值"契约） | `test_idempotency_snapshots_are_versioned_and_forward_compatible` |
| P2：`0.2.0.0` 排序错位（phase rank 移位） | release 段限 1–3 位（X[.Y[.Z]]），4 段直接 `MigrationMetadataError` | `test_four_segment_versions_are_rejected` |
| P2：lock_ms 只管等待，且后置测量曾把已提交迁移报告为失败 | 获取等待由 busy_timeout 强制；提交窗口只作可观测告警，超预算时 run 保持 completed、调用返回成功。该窗口包含等待+执行+提交，不宣称是 SQLite 未暴露的纯持锁时间 | `test_lock_budget_overrun_reports_committed_success_with_warning` |
| P2：create_tenant 无幂等键 | 接受 `idempotency_key`，在合成 `bootstrap` App 实例命名空间下走统一 runner；"全部写接口支持幂等"现在无例外 | `test_create_tenant_accepts_idempotency_key`（回放同结果、单行、未注入 runner 报 `idempotency_unavailable`） |

## 针对性评审修复（第四轮）

| 评审问题 | 修复 | 回归测试 |
| --- | --- | --- |
| **P1**：恢复会复制未认证的额外文件，SQLite WAL/SHM 可改变已签 canonical 的读取结果 | 备份格式改为精确白名单：必须且只能包含 canonical/manifest/fingerprint/checksums，以及可选 authenticity tag；未知文件、目录、symlink 全部拒绝，恢复只按已验证名称逐文件 `O_NOFOLLOW` 复制；所有 SQLite 校验以 `immutable=1` 打开，不创建或读取 sidecar | `test_backup_file_set_is_exact_and_sidecars_are_never_restored`（WAL sidecar 与普通额外文件均在复制前拒绝） |
| **P1**：staging 在不变量检查后、rename 前仍可被修改 | staging 以 `0700` 私有目录建立；不变量检查使用 immutable 只读连接；检查后再次执行完整文件集、checksum、HMAC 与 manifest 对账，只有最后一次校验的同一 staging 目录可进入 journal/switch | `test_restore_verifies_the_bytes_it_switches`（新增“不变量检查后改库”时序，最终校验拒绝且目标未切换） |
| **P1**：原子写使用可预测 `<name>.tmp`，可借 symlink 覆盖其他文件 | `_write_file_durable` 改用同目录 `mkstemp`（随机名、`O_EXCL`）+ fsync + `os.replace`，失败清理独占临时文件，不再打开攻击者预建路径 | `test_durable_file_write_does_not_follow_predictable_temp_symlink`（旧式 temp symlink 的 victim 保持原字节） |
| **P1**：migration 在 schema 已提交后因 `lock_ms` 超限抛失败，调用方会把 durable success 当失败；测量也不是纯持锁时长 | `busy_timeout` 继续约束锁获取；提交窗口只作为可观测告警写入 completed run 并由 `MigrationRunner.warnings`/CLI stderr 暴露，迁移仍返回成功。文档明确该值包含等待+执行+提交，不再宣称是 SQLite 未暴露的纯持锁时长 | `test_lock_budget_overrun_reports_committed_success_with_warning`（1000ms > 100ms：版本/表/run 均 completed，调用成功并带告警） |
| P2：合法 JSON 但 manifest 形状错误会触发 `AttributeError`/`TypeError` | typed access 前验证顶层、files、schema/tombstone 版本与 watermark map 的完整形状；所有解析/SQLite/I/O 类型错误收敛为失败 `RestoreCheck` | `test_verify_backup_returns_check_for_corrupt_content`（新增 list、null files、null schema、错误 watermark shape） |
| P2：`snapshot_version` 仅装饰输出，恢复会把未来版本当 v1 | envelope 必须精确匹配当前版本；未知/布尔/缺失版本以及非 object record 均显式拒绝；无 envelope 的既有裸快照继续兼容 | `test_idempotency_snapshots_are_versioned_and_forward_compatible`（未来版本与 null record 拒绝） |
| P2：restore 在错误收敛前 `iterdir`/copy，缺失或不可读源会抛异常并遗留 staging | 源目录检查、精确复制和 fsync 全部纳入错误收敛；任何失败返回 `RestoreReport(check.ok=False)`，并把 staging 清理失败追加为可见问题 | `test_restore_missing_source_returns_failure_and_cleans_staging` |
| P2：恢复清理用 `rmtree(ignore_errors=True)` 后仍删除 journal，失败状态被永久遗忘 | staging/aside 清理不再吞错；失败或目录仍存在即返回 `cleanup_failed` 并保留 journal；成功切换后的 previous-target 清理也在删除 journal 前完成 | `test_recovery_retains_journal_when_cleanup_fails` |

## 量化验收基线证据

| 基线 | 证据 |
| --- | --- |
| Scope/Privacy/Binding/Redirect/Revision/幂等性质测试各 ≥ 200 生成案例 | `tests/unit/test_domain_rules.py`、`tests/unit/test_identity_rules.py`（每条性质 250 个固定种子生成案例） |
| 同一 Expected Revision 的 50 个并发写入恰好一个成功 | `test_concurrency.py::test_fifty_concurrent_writers_same_expected_revision_exactly_one_wins`（1 成功 / 49 `revision_mismatch`） |
| 横向越权矩阵（Tenant/Agent/SpaceGroup/Space/Entity 读写双向，Body 只能收窄） | `test_lateral_access_matrix_blocks_cross_dimension_reads`、`test_write_directions_are_blocked_by_tenant_and_admin`、`test_create_space_blocks_lateral_writes_outside_grants`、`test_body_narrowing_never_expands_access` |
| Backup → 隔离 Restore → Smoke Read/Write 连续 3 次 | `test_backup.py::test_backup_restore_smoke_repeats_three_times`（RPO=最后已提交事务，RTO 实测值由 `RecoveryMeasurement` 记录） |

## Migration 证据

- `0001_phase0_metadata.sql`：与 Phase 0 发布字节完全一致（`ec236f7e…861`），未修改
- `0002_phase1_kernel.sql`（online_safe=true，lock_ms=200，min_app=0.2.0）
- SHA-256：`7fcd08837bd3758aa0bc1b1511d09b264abbc2b05cb55d98e4a8213ae652267c`
- Schema Version：2；应用二进制兼容窗口 [2, 2]（迁移前备份的恢复校验窗口放宽至 1..2）
- 升级路径：Phase 0 库（版本 1）→ 版本 2 由集成测试覆盖

## 契约证据

- Package version：0.2.0（module `__version__`、Python/TypeScript SDK、pyproject 全部对齐）；
  Schema version：2；API `v1` 不变
- 稳定错误码新增（只增不改）：`revision_mismatch`、`idempotency_key_reused`、
  `idempotency_in_progress`、`idempotency_unavailable`、`binding_conflict`、
  `redirect_cycle`、`redirect_depth_exceeded`、`access_denied`、`scope_violation`、
  `reason_required`、`history_unavailable`、`invalid_state_transition`、`not_found`、
  `conflict`、`invalid_scope`、`database_busy`、`sqlite_runtime_not_allowed`、
  `schema_incompatible`

## 设计决策（本轮评审确认）

- **权威与权限**：断言高权威字段需要对应 capability **或** 管理平面（`admin=True`）。
  `admin` 由服务端从认证凭据派生、请求体不可自授，代表平台运营方全权；这不是绕过，
  而是设计上的第二授权通道，已在代码注释与测试中显式固定。
- **备份真实性与完整性分离**：目录内 checksums 只证明意外损坏；对抗性篡改的拒绝依赖
  操作员保管的外部密钥（`--backup-key-file`）。两者同时验证。
- **版本列车**：核心、Python SDK、TypeScript SDK 同步 0.2.0，避免"核心已升、SDK 落后"
  的兼容窗口；SDK 独立发版需要时再引入独立版本策略（届时以 ADR 记录）。
- **幂等副作用边界**：业务回调仅在持有有效租约时执行一次；租约过期被接管时，被接管
  者的全部数据库写入随 owner-fencing 回滚。回调内部的**非事务外部副作用**（如外部
  HTTP 调用）不在 fencing 保护范围内——契约要求此类副作用由调用方放入可重放队列。

## 已知限制

- **历史范围已接续**：Observation、Recall/索引、Persona 演进、Surface 与 HTTP 已由 Phase 2–10 交付，不能列作当前未实现能力。
- 本地开发 SQLite 3.50.4 不在官方 Allowlist；生产 Ready 使用官方清单，升级运行时或通过
  ADR 扩展清单前，部署必须显式 Pin。
- 双端挑战码仅保留契约位（`method='challenge_code'`），产品流程按计划在后续阶段实现。
- Redirect 撤销未建模（合并单向不可逆；如需回滚由管理平面新建反向 Redirect 并审计）。
- 幂等租约过期由时钟判定：仍在执行中的长事务租约过期后可被其他 worker 接管，但
  owner-fencing 保证被接管者的提交整体回滚，副作用不重复。业务异常会立即降级租约
  （best-effort），失败操作按契约可重试——只有已提交的结果被记录回放。
- 恢复串行化使用 POSIX `flock`（macOS/Linux）；Windows 不在目标平台内。
- SQLite 无法抢占执行中的迁移事务，也不暴露 `executescript` 内纯锁持有时长：
  `lock_ms` 以 `busy_timeout` 约束获取等待；观测到的事务窗口包含等待、执行和提交，
  超出时在成功结果上发出告警，绝不把已提交迁移报告成失败。
- 备份 HMAC 密钥目前由操作员文件承载；密钥托管/轮换（KMS、密钥链）按计划在硬化阶段
  引入。
- **历史扩展已交付**：Phase 5 纳入 local blob 与删除账本；Phase 7 明确 FAISS 不进入备份，恢复后重建。
- RTO 数值依赖数据规模；当前记录的是空库至小规模种子数据下的实测值，未做规模基准。

## 原阶段验收目标

下列门槛从已归档阶段计划移入，保留未被实测证明的要求。它们是当时的验收目标，不能从本报告 Passed/Completed 标签推断逐项均已完成；是否达到须与前文的样本、测试与限制核对。尚未闭合项由 Phase 14 的发布矩阵承接。

- Scope、Privacy、Binding、Redirect、Revision 和幂等性质测试每项至少运行 200 个生成案例。
- 同一 Expected Revision 的 50 个并发写入必须恰好一个成功，其余均返回 `revision_mismatch`。
- 横向越权矩阵覆盖 Tenant、Agent、SpaceGroup、Space、Entity 的读写两种方向，允许的 Body Scope 只能收窄。
- Backup → 隔离 Restore → Smoke Read/Write 连续执行 3 次；RPO 必须为最后一个已提交事务，RTO 在声明数据规模下记录实测值。
