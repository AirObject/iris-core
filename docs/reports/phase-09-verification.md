# Phase 9 验证报告：完整 Persona

> 归档证据：以下版本、测试数量、耗时与覆盖率是本阶段执行时的历史快照，未在本次文档整理中重跑；不能作为当前发布已通过的证明。当前状态见[阶段索引](../development/README.md)，发布重验见[Phase 14](../development/phase-14-hardening-release.md)。
> 后续闭环：HTTP/进程入口已由 [Phase 10](../development/phase-10-consolidation-reflection.md)交付；旧报告中的应用层/mock 范围只描述当时环境。

> 状态：Completed（实现 + 门禁实测 + 收尾阶段补测后全量重跑）
> 日期：2026-09-04
> 基线 commit：`0267211`（feat: complete phase 8 profile graph）之上的工作区
> 硬件：Apple Silicon（darwin 25.6.0 arm64），本地 SQLite 3.50.4（测试显式 pin，等价部署 pin）
> 验收文档：[phase-09](../development/phase-09-persona.md) · 决策：[ADR-0018](../adr/0018-phase9-persona.md)

本报告只记录实际运行结果。全部数字来自本轮 `make ci` 与专项测试的实测输出；未运行的项
一律记为"未验证"，不按设计意图推定通过。

## 1. 版本与契约

| 项 | 值 |
| --- | --- |
| Core / Python SDK / TypeScript SDK | 0.10.0 |
| Schema | 10（migration `0010_phase9_persona.sql`，min_app=0.10.0，online_safe=true，lock_ms=200，recovery=none） |
| 契约版本 | 1.8.0（additive：capability `persona.v1`/`persona.state.v1`/`persona.evolution.v1`；错误码 +`persona_policy_denied`/`persona_base_revision_stale`；OpenAPI 新增 8 条 `/v1/personas` 路径） |
| OpenAPI 路径 | 69（Phase 8 为 61；Phase 9 +8） |
| fixtures | 123 manifest cases（Phase 8 为 113；新增 10 个 Persona valid/invalid 用例） |
| 错误码总数 | 41（真源：`contracts/source/contracts.json`，ADR-0017 §1） |
| Runtime 兼容窗口 | [9, 10]（`SUPPORTED_SCHEMA_MIN=9`、`SUPPORTED_SCHEMA_MAX=10`） |
| 新启用 job kinds | `persona.revised`(p2)、`persona.revision_invalidated`(p1)、`persona.state_expire`(p4)——均 `enabled=True` 且有真实幂等 handler。登记表里的 `persona.evaluation` 仍是 **disabled 占位**（与 `reflection.generate` 同批，归 Phase 10） |
| 新依赖 | 无（纯 SQLite 行存储；不引入 Provider 调用） |

Migration checksum（SHA-256，`MigrationRunner` 记录于 `schema_migrations`）：

```
42be7963091cd8e369a8ca2bee5ceaa0bf499169bb80d19e4b1a159e8484bdf3  migrations/0010_phase9_persona.sql
```

0010 创建 7 张 STRICT 表：`persona_policies`、`persona_revision_metadata`、`persona_states`、
`persona_state_current`、`persona_proposals`、`persona_proposal_events`、
`persona_adoption_feedback`。

**两个 Capability 命名空间不要混淆**：`AccessContext` 的授权串是
`persona.read.v1`/`persona.state.write.v1`/`persona.review.v1`/`persona.manage.v1`（ADR-0018 §3），
契约协商面的能力名是 `persona.v1`/`persona.state.v1`/`persona.evolution.v1`。前者是进程内
权限判定，后者是宿主协商，二者不是同一张表，也不应互相同步。

## 2. 量化门禁实测

| 门禁 | 要求 | 实测 | 结果 |
| --- | --- | --- | --- |
| Persona Current Read p95 | ≤ 20 ms | 200 次读取：**p95 = 1.69 ms**，median = 1.08 ms | ✅ |
| Revision/Hash 与 RecallResponse 顶层一致 | 100% | Bootstrap、发布后、回滚后三点逐次相等；回滚为新 Revision 且 Hash 回到目标值 `(n+1, 旧 hash)`；Persona 从不作为候选出现 | ✅ |
| Core 注入拒绝 | ≥200 固定种子案例 | 200 案例（`identity`/`safety_boundaries`/`relationship_constraints`/`values` 随机命中）全部 `invalid_request` | ✅ |
| State 值域性质 | ≥200 案例 | 200 案例（mood/energy/engagement 越界与合法混合）判定与值域定义逐案一致 | ✅ |
| State TTL 边界性质 | ≥200 案例 | 200 案例覆盖负值、0、窗口内与 2× 上限；只有 `0 < ttl ≤ 7 天` 被接受 | ✅ |
| Hash / 幅度可重放性 | ≥200 案例 | 200 案例：同内容 Hash 恒等，`field_magnitude ∈ [0, 1]` | ✅ |
| 同 Base Revision 并发发布 | 恰好一个成功 | 50 并发（12 线程池）：**won = 1，stale = 49**，Current Pointer 只推进一格 | ✅ |
| Policy 各限制独立生效 | locked/manual/bounded_auto、幅度、累计窗口、冷却期、敏感字段 | 逐条单独驱动、其余放开：allowlist 外字段、Evidence 多样性不足、Evidence 时间跨度不足、单次幅度超限、累计窗口超限、冷却期未过——各自返回 `persona_policy_denied` 且错误消息互不相同；冷却期结束后同一提案恢复可发布 | ✅ |
| 发布前重评估 | 审批时重读 Policy/Current/Evidence | 提案创建后改 Policy 为 `locked` → 审批 `persona_policy_denied`；提案创建后管理员插入发布 → 审批 `persona_base_revision_stale`；两种情形 Current 均未推进 | ✅ |
| 管理平面隔离 | 应用凭据不得发布 | 缺 `persona.manage.v1` 的应用凭据发布被 `access_denied` 拒绝 | ✅ |
| State 到期回归 Baseline | 确定性 | Worker 路径：迟到任务被 Revision fencing 判为 no-op，当前状态到期后写出 baseline 新 Revision；重启 Catch-up 路径：`expire_due_states` 在 Worker 完全未运行时补齐，二次扫描返回 0（幂等） | ✅ |
| 同一时钟轨迹可重复 | 连续 3 次结果相同 | 3 轮相同轨迹：`(Δrevision, state) = (1, {"mood": -0.2})` ×3 | ✅ |
| 时钟回拨下的到期语义 | 不误判 | **未单独构造**——到期判定为 `expires_us <= now_us` 的直接比较，回拨只推迟到期，不产生错误到期；记为已知限制 5 | ⏸ |
| Bootstrap 不被改写 | 字节/ID/Revision/Hash 不变 | Schema 9 → 10 升级后 `persona_revisions` 内容与 `agents.persona_current_revision_id` 逐字段比对不变；迁移只补 locked Policy 与 Revision Metadata | ✅ |
| 发布幂等 | 同 Idempotency Key 重放 | 重放返回首个 Revision 记录本身，Current 仍为 2 | ✅ |
| Outbox 通知不含正文 | refs-only | 发布产生的 Outbox payload 只含 Agent/Revision/Hash/reason，无 Core/Trait/Narrative 正文 | ✅ |
| 指标低基数 | 只用状态标签 | 提案生产路径产出的计数器恰为 `iris_persona_proposals_total{outcome="proposed"} = 1`，无内容或 ID 标签 | ✅ |
| Readiness / 备份完整性 | Pointer/Metadata/Policy/State 不一致视为 fatal | `/health/ready` 增加 `persona_current_integrity` 检查；备份完整性校验在 Current Persona Metadata 缺失时拒绝 | ✅ |
| 离线重连、宿主 Cache 采用、通知丢失重放收敛 | 各 ≥20 次 | **未验证**——需要真实传输层与宿主生命周期，Core 侧只能证明事件是 refs-only、缓存键为 `(agent_id, revision, content_hash)` | ⏸ 归 Phase 11/12（ADR-0017 §3） |

## 3. 收尾阶段修复的缺陷

Phase 9 主线实现完成后，收尾阶段发现并修复了四项问题。

### 3.1 旧快照回退清单漏掉 Phase 9 的表（真实缺陷）

`tests/integration/test_phase5_review_round4.py::_downgrade_snapshot_to_round2` 用一份**手工
维护**的 DROP 清单把新备份改写成 round-2 旧快照，随后 `DELETE FROM schema_migrations
WHERE version >= 7`。0010 新增的 7 张 persona 表不在清单里，于是回退删掉了 0010 的登记行
却留下物理表，重放 0010 时以

```
sqlite3.OperationalError: table persona_policies already exists
```

失败，`test_round2_schema_backup_verifies_restores_and_replays` 与
`test_legacy_restore_upgrades_before_replaying_colliding_requests` 双双红灯。

修复不是把 7 张表补进清单，而是**取消清单本身**：`tables_created_from_version(7)` 用
`discover_migrations(default_migrations_path())` 读取迁移文件、解析 `CREATE TABLE`，按建表
逆序返回；解析结果为空时直接抛错，避免"正则失配 → 静默不回退"这一更隐蔽的失效模式。
运行时创建的 FTS5 虚表 `fts_index` 不由迁移建立，是唯一显式保留项，且从
`storage.fts.INDEX_TABLE` 导入而非硬编码。解析前先剥掉 `--` 行注释——迁移头部的散文里若
出现 "CREATE TABLE agents" 这样的字样，未剥注释的解析会把 Phase 1 的表也一起 DROP。

同一份漂移还有第二处：两个测试都把重放结果断言成字面量 `[7, 8, 9]`。已改为
`migration_versions_from(7)`，同样从迁移文件推导。

新增 `test_rewind_drop_list_covers_every_table_migrations_add` 把这条不变量固定下来：分别
构建 Schema 6 与最新 Schema 的库，比对 `sqlite_master`，断言新增表集合被推导出的清单完全
覆盖。把推导人为限制在 `< 10` 后，该测试与两个原始测试同时失败——修复确认为承重。

### 3.2 文档门禁扫描范围不含仓库根 Markdown

`tools/check_docs.py` 只遍历 `docs/**/*.md`。根 `README.md` 指向
`docs/reports/phase-09-verification.md`（本文件），而该文件当时并不存在，`make ci` 仍然全绿。
已把根 README、两个 SDK README 与 `contracts/`、`schemas/` 下的 Markdown 纳入扫描；加入后
门禁立即以 `README.md -> docs/reports/phase-09-verification.md` 失败，符合预期。

### 3.3 退出门禁中未被测试触及的分支

`bounded_auto` 的 Evidence 多样性、Evidence 时间跨度、累计窗口与冷却期四条限制，原本只在
宽松配置下被间接经过，任一条回归都不会让测试变红；`expire_due_states`（ADR-0018 §5 的重启
Catch-up 扫描）与"Recall 顶层 Persona 与 `current` 一致"这条跨阶段不变量则完全没有测试。
已补三个用例（见 §2 对应行），Phase 9 用例数由 22 增至 25。

### 3.4 基线 §33.1 的 `domain/` 子树与实现从未一致

架构基线的仓库树把 `src/iris_memory_core/domain/` 画成 `identity/`、`spaces/`、
`observations/`、`cognition/`、`notes/`、`tasks/`、`memory/`、`persona/` 八个子包，而实现
从 Phase 1 起就是平铺模块（`identity.py`、`persona.py`…）。同一章又写着"其余目录必须与本树
保持一致"，因此这是一处会误导实现的自相矛盾。已把该层改为平铺说明，并声明本树只约束到
目录一层。

## 4. 全量门禁

```
ruff format --check       189 files already formatted
ruff check                All checks passed
import boundaries         ok
check_docs                ok（15 个阶段文档 + 锚点 + 仓库根 Markdown）
mypy                      Success: no issues found in 188 source files
tsc --noEmit              ok
generate_contracts --check ok
check_compatibility       ok
pytest                    8162 passed, 0 failed（6 分 34 秒）
coverage                  83.62%（门槛 80%）
```

## 5. 已知限制

1. **离线重连、宿主 Cache 采用与通知丢失重放未做端到端验证**：Core 只证明 Outbox 事件是
   refs-only 且缓存键为 `(agent_id, revision, content_hash)`。传输层已在 Phase 10 交付；宿主采用、失效重连与通知重放仍需 Phase 11/12 的端到端证据。
2. **`bounded_auto` 的累计窗口按已发布 Proposal 统计**：管理员直接 `publish_revision` 不计入
   累计幅度——管理平面被视为授权旁路，不是自动演进。
3. **State 到期依赖 Worker 或启动 Catch-up 扫描**：两者都未运行时，过期 State 仍会被
   `current` 读到；宿主不得把 State 当作强一致的到期语义使用。
4. **Evidence 只接受当前 Revision 的资源**：历史 Revision 或已撤销资源一律 fail closed，
   因此跨越长时间窗口的 Proposal 需要在证据仍 current 时提交。
5. **时钟回拨会推迟 State 到期**：到期判定是 `expires_us <= now_us` 的直接比较，没有独立的
   单调时钟来源。系统时钟回拨期间过期 State 继续被 `current` 返回，回拨结束后由同一确定性
   扫描收敛；本阶段没有单独构造回拨用例。

## 原阶段验收目标

下列门槛从已归档阶段计划移入，保留未被实测证明的要求。它们是当时的验收目标，不能从本报告 Passed/Completed 标签推断逐项均已完成；是否达到须与前文的样本、测试与限制核对。尚未闭合项由 Phase 14 的发布矩阵承接。

- Persona Current Read 在声明硬件、并发和缓存状态下 p95 ≤ 20 ms；响应 Revision 与 Content Hash 必须和 RecallResponse 顶层值 100% 一致。
- locked/manual/bounded_auto、字段 Allowlist、单次/累计幅度、Evidence 数量/多样性/时间窗、冷却期和 Stale Base 性质每项至少运行 200 个固定种子案例。
- 50 个客户端以同一 Base/Expected Revision 并发发布时恰好一个成功，其余返回稳定 `revision_mismatch` 或 `persona_base_revision_stale`，只产生一个 Current Pointer 逻辑推进。
- State TTL 测试覆盖 UTC、时钟前跳/回拨、暂停和重启 Catch-up；连续 3 次同一时钟轨迹得到相同 Baseline 回归 Revision 和状态值。
- 发布、通知丢失、离线重连、Cache 失效、宿主采用和回滚链路各至少重复 20 次；所有宿主最终收敛到同一 Revision/Hash，未知或哈希不符 Persona 的采用数为 0。
