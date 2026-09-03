# 阶段 8：Profile 与 Graph

> 状态：Completed（含三轮对抗性复审修复与全量门禁重跑）
> 负责人：Iris Memory Core Team
> 开始日期：2026-09-03
> 完成日期：2026-09-03
> 前置阶段：[阶段 7](./phase-07-vector-recall.md)
> 目标版本：0.9.0（已达成：Core/双 SDK 0.9.0、Schema 9、Contract 1.7.0）
> 架构依据：[§13.3 Relation](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#133-relation)、[§13.5 ProfileProjection](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#135-profileprojection)、[§18 Recall 协议](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#18-recall-协议)、[§22.5 Graph 与 Profile](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#225-graph-与-profile)、[§36 阶段 8](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#阶段-8profile-与-graph)
> 决策记录：[ADR-0016](../adr/0016-phase8-profile-graph.md) · 验证报告：[phase-08-verification](../reports/phase-08-verification.md)

## 阶段目标

增加可重建的 Entity/Relationship/SpaceGroup 汇总视图和受预算约束的关系召回，同时保证任何投影落后、损坏或 Binding 变化都能回退到 Canonical Claim/Relation。

## 架构约束

- Profile 是 Claim 的带来源汇总，不是独立事实源；Agent Persona 不能混入外部 Entity Profile。
- Graph 只投影 Canonical Relation、Verified Binding 和允许的 Claim，不从模型联想直接建边。
- 多跳遍历逐边执行 Scope/Privacy/Status/Valid Time/Tombstone 校验。
- Builder Version 未知、Watermark 超阈值或校验失败时不得读取投影。
- Binding/Redirect/SpaceGroup 变化只触发重建和失效，不重写历史事实。

## 需求追踪

| 需求 ID | 基线要求 | 工作包 | 验证门禁 |
| --- | --- | --- | --- |
| P8-PROFILE-01 | Profile 字段级来源、冲突、Freshness 与 Canonical 回退 | 8.1 | 来源追踪、冲突、过期和重建测试 ✅ |
| P8-GRAPH-01 | Graph 仅投影授权 Canonical 边并保存 Revision/Scope | 8.2 | 边白名单、来源完整性与投影一致性测试 ✅ |
| P8-TRAVERSAL-01 | 多跳逐边授权、深度/扇出/节点/Token/Deadline 预算 | 8.3 | 恶意高连接图、越权路径和终检测试 ✅ |
| P8-INVALIDATION-01 | Binding/Redirect/SpaceGroup/Correct/Forget 触发失效 | 8.1–8.4 | Watermark、Cache、重建和删除竞态测试 ✅ |
| P8-DEGRADE-01 | 未知/落后/损坏投影回退 Canonical Claim/Relation | 8.3–8.4 | 故障注入、Degraded Envelope 与可观测性测试 ✅ |

## 工作包

### 8.1 ProfileProjection

- 按身份、偏好、关系、重要经历、长期目标、近期变化和交互建议生成字段级汇总。✅
  （`domain/profile.py` 冻结的确定性分区映射；`indexing/profile.py` 单一 derive 路径）
- 每字段保存 Source Claim IDs、Builder Version、Source Watermark、Freshness 和 Conflict State。✅
  （`profile_fields` 行携带全部元数据；freshness 为来源 recorded_at，快照锚定）
- 实现全量/增量重建、绑定变化失效及 Canonical Claim 回退读取。✅
  （影子 generation + 指针 CAS；`profile.apply` 增量；`read_profile` Canonical 回退）

### 8.2 Relation Graph

- 构建 Entity 节点、Canonical Relation、Verified Binding 和安全 Claim 边。✅
  （冻结 Allowlist：relation / verified binding / 带 `target_entity_id` 结构化引用的 relationship claim）
- 每条边保留 Canonical Ref/Revision、Scope、Privacy、有效期和 Builder 元数据。✅
- 实现影子重建、校验、Watermark/Version 拒绝以及绑定/重定向后的重投影。✅
  （翻转前从持久化行重算 manifest；未知 builder/水位倒退 fail closed）

### 8.3 受限 Graph Recall

- 实现最大深度、逐层扇出、总节点数、边类型 Allowlist、Deadline 和 Token Budget。✅
  （深度 2 / 扇出 16 / 节点 64 / 候选 12 / 单调 Deadline 每边检查 / 有界边读取窗口）
- 遍历阶段与最终 Candidate Rehydrate 都做权限检查，禁止从可见起点穿越不可见边。✅
- Graph Route 失败时回退到直接 Canonical Claim/Relation 并写入 Degraded Route。✅
  （10 个稳定降级原因码；`route_skipped_canonical_intact` 回退标注）

### 8.4 管理与可观测性

- 提供 Profile/Graph Watermark、Lag、Builder Version、节点/边数量和重建状态。✅
  （`pointer_info` / readiness checks / `iris_index_generation`/`iris_index_lag_revisions`）
- 管理重建使用安全 Job/Fencing，不阻塞 Canonical 写入。✅
  （6 个新 job kinds，全部 fenced handler；重建 settle 未租用 backlog）
- Cache 在 Binding、Redirect、SpaceGroup、Correct 和 Forget 后按 Outbox 失效。✅
  （仓库无 Recall Cache——失效边界 = Canonical 事务内 Outbox 事件，ADR-0016 §7）

## 数据、契约与回退策略

- Profile/Graph 只新增投影表、Builder State、Manifest 与 Current Pointer；字段与边保存 Canonical ResourceRef/Revision、Scope/Privacy、有效期、Source/Tombstone Watermark，不复制不可追踪事实。✅
  （Schema 9 十张 STRICT 表：`profile_projection_state/generations/current/subjects/fields` + `graph_projection_state/generations/current/nodes/edges`）
- Builder 升级、Binding/Redirect/SpaceGroup 变化使用新 Generation 或影子投影；校验来源覆盖、节点/边数量、Checksum 和抽样 Recall 后原子切换，旧投影保留受限回退窗口。✅
  （fenced epoch CAS + retired 保留窗口默认 24h，cleanup 验证后清理）
- Profile/Relations 读取和 Graph Route 通过可选 Capability、OpenAPI/Schema 和 Fixture 发布；旧客户端可忽略 Graph Route，但服务端不得用"投影不可用"放宽逐边授权或最终 Rehydrate。✅
  （capability `recall.graph.v1`/`profile.v1`；路由枚举 +`graph`/`profile`；`GET /v1/entities/{entity_id}/profile`）
- Cache/Projection 失效 Outbox 携带 Source Revision 与原因；旧 Worker 不领取未知 Builder Job，新 Worker 能识别上一 Payload 版本并拒绝 Stale Source 提交。✅
- 回退时禁用不可信 Profile/Graph Pointer，并改走 Canonical Claim/Relation 直接读取；不通过 Down Migration 删除 Builder 历史，也不回写 Entity/Binding/Relation 的 Canonical 历史。✅
  （Restore 重置两投影为 pending_rebuild；无 Down Migration）

## 量化验收基线

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

## 退出门禁

- [x] Profile 每个字段可追溯到有效 Claim，冲突不会被汇总静默覆盖。
- [x] Graph 多跳逐边 Privacy 测试覆盖跨 Tenant/Agent/SpaceGroup/Entity 越权。
- [x] 深度、扇出、节点和 Token 预算在恶意高连接图上仍生效。
- [x] 旧/未知 Builder、落后 Watermark 和损坏投影被拒绝并安全回退。
- [x] Binding/Redirect/SpaceGroup 变化后重建与 Canonical 结果一致。
- [x] Forget 后旧 Profile 字段、Graph 边和 Cache 均不能复活目标内容。
- [x] Migration/Builder/Capability 兼容和回退方案、需求追踪及交付证据已完成评审。

## 交付证据

- **代码/变更**：`domain/profile.py`、`domain/graph.py`（领域规则与确定性摘要）、
  `storage/projection.py`（10 表仓库 + fenced 指针 CAS）、`indexing/profile.py`、
  `indexing/graph.py`（构建/增量/验证/信任门/Canonical 回退）、`application/recall.py`
  （GraphRoute/ProfileRoute + 预算 + 降级接线）、`application/identity.py`/`provisioning.py`
  （Binding/Redirect/Tombstone/SpaceGroup 原子失效）、`jobs/handlers.py`/`worker.py`
  （6 个新 kind + 事件接线）、`storage/backup.py`（Restore 重置）、`application/health.py`
  （readiness 检查）、双 SDK 校验器与 0.9.0 版本、`tools/generate_contracts.py` 与
  `tools/mock_server.py`（Schema 9 契约面）。
- **测试**：`tests/unit/test_phase8_domain.py`（29）、`tests/integration/test_phase8_migration.py`（7）、
  `test_phase8_graph.py`（129）、`test_phase8_profile.py`（21）、`test_phase8_recall.py`（46）、
  `test_phase8_jobs.py`（9）、`test_phase8_concurrency.py`（7）、
  `tests/integration/test_phase8_review_round3.py`（11，第三轮复审回归——端点实体隐私、
  失效边不扩展、verify 判定持久化、清账 payload 版本、关系主体配对、静默管线全量比对）、
  `tests/performance/test_graph_profile_recall_latency.py`（1）——Phase 8 新增 **258** 用例；
  全量 `make ci` 门禁数字以 [phase-08-verification](../reports/phase-08-verification.md) 为准。
- **Builder Manifest**：generation 行即权威 manifest（counts + 从持久化行重算的
  content checksum + agent watermarks + builder version + source/tombstone watermark）；
  翻转前重绑定、验证/清理任务复核——`test_three_rebuilds_are_identical`、
  `test_apply_maintains_the_manifest_binding`、故障注入组各 20 轮。
- **Schema/Migration**：`migrations/0009_phase8_profile_graph.sql`（online_safe=true、
  lock_ms=200、min_app=0.9.0、recovery=none）；0001–0008 与 `869f04d` 逐字节一致
  （`test_published_bytes_match_head_baseline` 锁定）；窗口 [8, 9]。
- **隔离/重建/预算测试报告**：[phase-08-verification](../reports/phase-08-verification.md)。
- **决策**：[ADR-0016](../adr/0016-phase8-profile-graph.md)（含 §11 三轮对抗性复审补充：
  第三轮修复端点实体隐私授权、失效边不再扩展前沿、verify 判定持久化、重建清账按
  payload 版本界定、relationship 主体配对准入统一、静默管线全量 Canonical 等价比对）。

## 已知限制

1. **Binding 边在 speaker 锚定的正向遍历中不可达**（identity→entity 方向；BFS 从实体
   节点出发）：边仍被投影/验证/计数；反向遍历留待真实需求另议（ADR-0016 §12）。
2. **边读取窗口的确定性饥饿**：枢纽节点前 fanout×4 条边全部不可见时，窗口外可见边
   被确定性跳过（availability 取向，非泄漏）。
3. **min-watermark 的保守降级**：纯非投影流量推进 Agent 水印后，需重建才恢复
   min-watermark 服务（与 Vector 同族；ADR-0016 §10）。
4. **`claims_targeting_entity` LIKE 预过滤**只覆盖 canonical JSON 两种空白形态——
   本仓库写入路径恒为 canonical（fail-closed 方向）。
5. **单事务重建持有 Writer Gate**（FTS 同法）：大租户（数万资源）重建应安排安静
   窗口；纯 SQLite 行投影无 Provider 调用窗口。
6. **节点预算耗尽时同边不产候选**（确定性 completeness quirk）。
7. **Profile/Graph rebuild 的候选/预算默认值**（深度 2/扇出 16/节点 64/候选 12）为
   嵌入式部署保守值；服务端可注入更小值，放大需独立容量评估。
8. **Canonical relations 路由不评估端点实体自身隐私标签**（Phase 5 既有行为，先于
   Phase 8 存在）：第三轮修复只约束 Graph 遍历路由；canonical 关系读取面是否引入
   端点隐私评估属跨阶段语义变更，留待独立裁定（第三轮复审观察项，未纳入本轮）。

## 明确不做

- 不把 Graph 当 Task Dependency 的状态源。
- 不用昵称、共同出现或向量相似直接创建 Relation。
- 不提供无界图遍历或跨 Privacy 边的"管理方便"旁路。

## 交接条件

Phase 9 可以安全使用外部 Entity/Profile/Relation 作为 Persona Narrative 的候选 Evidence，同时保持 Persona 与用户画像隔离。（Profile 已结构性排除 `self_narrative` 类 Claim 与 Persona 数据源，ADR-0016 §2。）
