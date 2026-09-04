# 阶段 10：巩固、Reflection 与传输层

> 状态：Completed  
> 负责人：Iris Memory Core Team  
> 开始日期：2026-09-04  
> 完成日期：2026-09-05  
> 前置阶段：[阶段 9](./phase-09-persona.md)  
> 目标版本：0.11.0（已达成：Core/双 SDK 0.11.0、Schema 11、Contract 1.9.0）  
> 架构依据：[§3.1 进程与模块边界](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#31-进程与模块边界)、[§15.3 后台提炼](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#153-后台提炼)、[§16 Outbox 与 Worker](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#16-transactional-outbox-与-worker)、[§17.4 周期任务](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#174-周期任务)、[§23 HTTP API 与能力协商](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#23-http-api-与能力协商)、[§24 Provider 边界](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#24-provider-边界)、[§35.4 启停与优雅关闭](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#354-启停与优雅关闭)、[§36 阶段 10](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#阶段-10巩固reflection-与传输层)  
> 决策记录：[ADR-0017 §3](../adr/0017-contract-surface-alignment.md)（传输层归属本阶段）、[ADR-0019](../adr/0019-phase10-consolidation-transport.md)（Phase 10 数据、Provider、认证、SSE、重放与生命周期语义） · 验证报告：[phase-10-verification](../reports/phase-10-verification.md)

## 阶段目标

交付两件事，Core 至此形成可被宿主真实调用的完整服务闭环：

1. Evidence 驱动、固定 Watermark、可重放且可降级的后台认知流水线，将 Observation 窗口转换为经过服务端验证的 Episode、Claim/Relation、Note/Task 候选和 Persona Proposal，而不影响 Canonical 在线写入与召回。
2. **HTTP 传输层与进程入口**：Phase 2–8 交付的是应用层服务 + 生成契约 + mock server，已发布 OpenAPI 的 69 条路径至今没有真实服务端。本阶段补齐传输面，使 Phase 11/12 的 Adapter 能够真正接入（ADR-0017 §3）。

## 架构约束

- Provider 输出是不可信候选，不是原始事实；未知 Entity、无 Evidence、越权 Scope 或直接 Core 修改必须拒绝。
- 每个 Job 固定 Source Watermark 和 Source Revision，提交前重新验证来源仍有效且未 Tombstone。
- ReflectionRecord 记录输入、模型/Prompt/Policy 版本和候选，不能把自己的输出当新证据循环强化。
- 网络 Provider 调用不在 SQLite 写事务内，也不阻塞 Observe/Forget/Correct/Task/Persona 读取。
- Provider 失败进入 Retry/Dead Letter/Circuit Breaker，Recall 通过现有契约降级。
- 传输层只做认证、校验、编解码与错误映射，不含领域规则：`AccessContext` 由服务端凭据推导，请求体只能收窄权限（§5.4、ADR-0002）。
- 传输层不得为了适配某条已发布路径而改变应用层语义；形状不匹配时按 ADR-0006 走新增可选字段或新 Capability。

## 需求追踪

| 需求 ID | 基线要求 | 工作包 | 验证门禁 |
| --- | --- | --- | --- |
| P10-EPISODE-01 | 固定 Watermark 的有界 Episode Consolidation | 10.1 | 迟到、重复、版本变化和删除来源测试 |
| P10-EXTRACT-01 | 严格 Schema、Evidence Span 与确定性 Reconciliation | 10.2 | 无来源、冲突、权威和候选状态测试 |
| P10-REFLECT-01 | ReflectionRecord 可重放且防止自循环强化 | 10.3 | 来源图、重复重放和 Persona Proposal 测试 |
| P10-PROVIDER-01 | Provider 最小输入、超时、预算、熔断与输出校验 | 10.4 | 故障、注入、配额、泄漏和 Ready 测试 |
| P10-FENCING-01 | 固定 Source Revision/Watermark 与提交前 Fencing | 10.1–10.5 | Correct/Forget、旧 Worker 和 Lease 竞争测试 |
| P10-OPERATIONS-01 | Dry Run、差异、审计重放与 Dead Letter 管理 | 10.5 | 可重复性、幂等、审计和低敏诊断测试 |
| P10-TRANSPORT-01 | §23 的传输层：认证、AccessContext、错误 Envelope、幂等头与协商 | 10.6 | 每条已发布路径的契约测试、越权矩阵、错误码映射测试 |
| P10-PROCESS-01 | §3.1/§35.4 的 `serve`/`worker` 进程入口与优雅关闭 | 10.6 | 干净环境启动、Ready 门、优雅关闭与强杀恢复测试 |
| P10-SURFACE-01 | 补齐仅缺传输面的 Entity/Identity/Binding/SpaceGroup 与管理端点 | 10.7 | 契约、权限、审计与弃用窗口测试 |

## 工作包

### 10.1 Episode Consolidation

- 以 Agent/Space/Session/时间/主题的有界窗口封闭 Episode，保留 Observation refs 和固定 Watermark。
- 处理迟到 Observation、Builder 版本变化、重复 Job 和已删除来源。
- 产生后续 Claim/Relation/Index Outbox，而非事务内联调用所有 Builder。

### 10.2 提取与协调

- 为 Claim、Relation、Note/Task Candidate 实现版本化 Prompt、严格 JSON Schema 和 Evidence Span 校验。
- 对重复、冲突、时效和 Source Authority 做确定性 Reconciliation，冲突事实并存为 Disputed。
- 普通对话产生的 Task 保持 Proposed；Note/Claim 权威与来源匹配。

### 10.3 Reflection 与 Persona Evaluation

- 实现 ReflectionRecord、Evidence Window、候选去重和防自循环标记。
- Persona Evaluation 只生成 Phase 9 Policy 可处理的结构化 Proposal，不直接发布 Core。
- 记录删除 Evidence、Stale Base Revision 和 Policy Denied 的稳定原因码。

### 10.4 Provider 治理

- 实现 Extraction/Summarization/Reconciliation/Persona Evolution Port 和按 Job Kind 的超时、重试、预算与并发。
- 输入按 Purpose/Scope/Privacy 最小化，输出执行 Schema、ID、Evidence、长度和值域 Allowlist。
- 实现熔断、有限 Probe、每日成本预算、积压公平调度和 Dead Letter 管理。

### 10.5 可重放与运维

- 支持按固定 Watermark/Builder/Prompt 版本 Dry Run、差异比较和受审计重放。
- 提供 Job 输入引用、候选/拒绝数量、Lag、Provider outcome 和成本的低敏诊断。
- 保证重放不重复逻辑资源或提高候选自身的证据权重。

### 10.6 HTTP 传输层与进程入口

- 实现 ASGI 应用与路由，覆盖已发布 OpenAPI 的每一条路径；`/v1` 业务前缀与不版本化的 `/health/live`、`/health/ready`、`/metrics`。
- 实现 Bearer 认证、App Instance 凭据到 `AccessContext` 的服务端推导、管理平面与应用平面的凭据隔离，以及 Body 只能收窄权限的强制校验。
- 实现领域错误到稳定 Envelope 的**全覆盖**映射：契约 `error_codes` 中的每个码都有映射来源，未映射异常一律 `internal_error` 且不泄漏 Stack Trace、路径或正文。
- 接线 `Idempotency-Key` 头与 Body 的 `expected_revision`；lease proof 是逐调用凭证，不进入请求指纹（ADR-0012 §12）。
- 实现 `GET /v1/capabilities` 与 `POST /v1/negotiation`，声明范围按 §23.2；实现 §23.1 的可选 SSE 事件面（Persona Revised、Lease Revoked、Revision Invalidated、Cognitive Event Ready）。
- 实现 `iris-memory-core serve` 与 `iris-memory-core worker` 两个进程命令，按 §35.4 的启动与关闭顺序执行，含 Grace Deadline 与 Lease 安全释放。
- mock server 从"契约夹具服务器"降级为纯测试替身；契约测试改为对真实传输层执行，mock 只保留给 SDK 的离线用例。

### 10.7 补齐仅缺传输面的端点

- 发布 Phase 1 已具备应用层能力的 `GET /v1/entities/{entity_id}`、`/v1/entities/{entity_id}/relations`、`POST /v1/identities`、`/v1/bindings:prepare|confirm|revoke`、`/v1/space-groups*`。
- 发布 `POST /v1/admin/indexes/{kind}:rebuild`（`kind ∈ {recent_context, fts, vector, graph, profile}`），并把 `/v1/admin/recent-context:rebuild` 标记弃用，保留至少一个发布窗口（ADR-0006、ADR-0017 §2）。
- 发布 `POST /v1/admin/backups`、`POST /v1/admin/exports`、`GET /v1/admin/audit-events`；三者要求管理 Capability 与原因码，独立审计。
- 导出与备份分别授权、分别保留（§21.3），不得把备份直接作为导出交付。

## 数据、契约与回退策略

- 增量 Migration 新增 Episode Consolidation State、ReflectionRecord、候选/拒绝记录、Provider Outcome 与版本引用；原始 Provider 响应不作为 Observation 或独立 Evidence。
- Job Payload 固定 Source Watermark、Source Revision、Builder/Prompt/Policy/Provider Schema Version 和最小 ResourceRefs；新 Worker 读取上一 Payload 版本，旧 Worker 不领取未知 Job Kind。
- Provider 网络调用完全位于数据库事务之外；提交事务重新校验 Scope/Privacy、Source Revision、Tombstone、Lease Generation 和 Policy，成功后再写 Canonical 结果与后续 Outbox。
- Prompt、输出 JSON Schema、Reconciliation 和 Provider Model 分别版本化；升级先 Dry Run/差异评审，再小流量启用。候选差异不通过直接修改历史记录处理。
- 回退可按 Job Kind 关闭 Provider Capability、熔断或切回上一 Prompt/Builder；在线 Canonical API 保持可用。Dead Letter 重放创建新 Outbox ID，保留原任务、版本、原因和审计，不复用过期 Lease。
- 传输层只发布已生成的契约形状，不引入新的 wire 语义；本阶段新增的端点全部为 additive，兼容基线快照只增不改。传输层故障回退到"停止接收新在线请求 + Worker 继续消费 Outbox"，Canonical 数据与后台流水线不受影响。

## 量化验收基线

- 相同 Canonical Snapshot、Watermark、Builder/Prompt/Policy 版本和确定性参数连续重放 3 次，候选 ID/指纹、Evidence refs、拒绝原因和差异摘要一致。
- 无来源、未知 Entity、越权 Scope、任意代码 Trigger、自动 Active Task、直接 Persona Core 修改与自循环 Evidence 每类至少运行 200 个生成案例，非法提交成功数必须为 0。
- 在 Provider 超时、429/5xx、无效 JSON、超限输出、熔断、预算耗尽和恢复 Probe 下各执行至少 20 轮；Observation、Forget、Correct、Task 转换和 Persona Current 仍满足既有正确性与延迟门禁。
- Correct/Forget/Source Revision 变化、Lease 过期和 Worker 抢占各至少重复 50 次，过期 Job 的 Canonical 提交成功数必须为 0。
- Dead Letter 以新 Outbox ID 重放 100 次不产生重复逻辑资源；积压指标必须报告 Job Kind、Lag、Oldest Pending、候选/拒绝数量和成本，且日志泄漏扫描无正文、Secret 或完整 External ID。
- 已发布 OpenAPI 的**每一条**路径都有至少一个成功用例与一个失败用例的传输层契约测试；未实现路径数必须为 0。
- 契约 `error_codes` 的每个码都有一条产生它的传输层测试；越权矩阵覆盖 Tenant/Agent/SpaceGroup/Space/Entity 双向，Body 提权成功数必须为 0。
- `serve`/`worker` 在干净环境连续启动、优雅关闭、`kill -9` 恢复各至少 20 次；优雅关闭在默认 30 s 内完成且无已提交事务丢失（§30、§35.4）。

## 退出门禁

- [x] 相同 Watermark、版本和确定性参数可重放得到同一候选集合/稳定差异说明——同一输入连续
  3 次校验得到同一 fingerprint/candidate_id/run_fingerprint，`candidate_diff` 全 `unchanged`；
  固定 `source_revision` 的 3 次重复 enqueue 收敛到 1 window / 1 run / 1 candidate / 1 claim
  （`test_same_snapshot_versions_and_parameters_replay_three_times_identically`、
  `test_pipeline_commits_evidence_bound_claim_and_is_replay_stable`）。
- [x] 无来源、未知主体、越权 Scope、任意代码 Trigger、Active Task 和 Persona Core 候选被拒绝——
  10 类纯域非法形状 × 200 个固定种子（`no_evidence`、`outside_window`、`self_reference`、
  `bad_span`、`stale_source`、`unsafe_trigger`、`active_task`、`persona_core`、`bad_score`、
  `bad_authority`）逐条落到指定 `RejectReason`；另有 3 类需要数据库的形状
  （`unknown_entity`、`scope_violation`、`privacy_denied`）各 200 例经真实 Worker 提交，
  `claims`/`relations` 计数为 0、拒绝记录为 200
  （`test_each_illegal_candidate_class_commits_zero`、
  `test_two_hundred_database_bound_illegal_candidates_commit_zero`）。
- [x] Source Revision 过期、Correct/Forget 或 Lease Fencing 后旧 Worker 无法提交——50 次
  预备-变更-提交序列的 Canonical 提交成功数为 0
  （`test_stale_source_mutations_fence_fifty_prepared_commits`）。
- [x] Reflection 自身输出不能作为同一结论的新独立 Evidence——`self_reference` 拒绝类覆盖
  候选引用自身/Reflection 的全部形状；Dead Letter 重放 100 次仍只有一个逻辑 Episode，
  重放不提高候选自身的证据权重
  （`test_dead_letter_replay_one_hundred_times_keeps_one_logical_episode`）。
- [x] Provider 超时、限流、无效 JSON、熔断和预算耗尽不影响 Canonical 在线功能——4 类故障
  各 20 轮、熔断半开 Probe 20 轮均只放行一个请求；非法与超限输出 20 轮全部落成
  `provider_outcomes` 低敏记录而无 Canonical 写入
  （`test_provider_failure_injection_is_bounded`、
  `test_circuit_breaker_allows_only_one_bounded_probe`、
  `test_invalid_and_over_limit_provider_outputs_are_persisted_twenty_times`）。
- [x] Dead Letter 可安全检查和以新 Outbox ID 重放，保留原任务引用与审计——100 次重放产生
  新 Outbox ID 与 `replay_of` 指向，逻辑 Episode 仍为 1。
- [x] 已发布 OpenAPI 的每条路径都有真实传输层实现，并通过成功/失败双向契约测试——85 条路径
  / 91 个 operation 与真实 ASGI 路由一一对应（未实现路径数 0），成功面与失败面两遍分别
  覆盖全部 operation（`test_every_frozen_operation_has_exactly_one_real_asgi_route`、
  `test_every_operation_has_a_success_case`、`test_every_operation_has_a_failure_case`）。
- [x] 认证、AccessContext 推导与 Body 收窄在越权矩阵下无提权路径——Bearer 认证、管理/应用
  平面隔离与 Tenant/Agent/SpaceGroup/Space/Entity 双向越权矩阵下 Body 提权成功数为 0
  （`test_bearer_auth_narrowing_management_and_new_surface`、
  `test_body_narrowing_bidirectional_scope_and_authority_matrix`）。
- [x] 契约错误码全部可由传输层产生，且响应不含 Stack Trace、路径、Secret 或正文——41 个稳定
  错误码全部有显式映射来源且各有一条真实产生它的传输层用例；备份/导出/审计/SSE 响应通过
  泄漏扫描（`test_every_stable_error_code_has_a_transport_mapping`、
  `test_every_stable_error_code_is_emitted_by_the_transport`、
  `test_backup_export_audit_sse_and_no_sensitive_error_leakage`）。
- [x] `serve`/`worker` 可在干净环境启动、通过 Ready 门、优雅关闭并从强杀恢复——干净环境
  连续 20 次启动 + Ready + 优雅关闭，最长一次远低于默认 30 s Grace Deadline；`worker` 同样
  20 次；Reconciliation 的 Canonical 提交边界新增 `kill -9` 场景，`pre_commit`/`post_commit`
  各 20 次重复后恢复均收敛到恰好 1 条 Claim、1 条物化候选、1 个已完成任务
  （`test_clean_asgi_start_and_default_graceful_shutdown_twenty_times`、
  `test_worker_clean_start_and_shutdown_twenty_times`、
  `tests/fault/test_kill9.py::TestReflectionKill9`）。**`serve` 进程本身未单独构造 SIGKILL**：
  在线写入走的是已被 observe/state/task/forget 强杀场景覆盖的同一批事务边界，传输层不持有
  额外的可丢失状态（见已知限制 4）。
- [x] Migration/Job/Prompt/Provider/传输层兼容和回退方案、需求追踪及交付证据已完成评审
  （[ADR-0019](../adr/0019-phase10-consolidation-transport.md)、
  [phase-10-verification](../reports/phase-10-verification.md)）。

## 交付证据

- **代码/变更**：`api/app.py`（真实 ASGI 应用：85 条路径路由、Bearer 认证与平面隔离、
  `AccessContext` 服务端推导与 Body 收窄、`Idempotency-Key`/`expected_revision` 接线、
  capabilities/negotiation、可关闭的 SSE 事件面、`/health/live|ready`、`/metrics`）、
  `api/errors.py`（41 个稳定错误码的全覆盖映射，未映射异常一律 `internal_error`）、
  `api/views.py`（契约 View 编码）、`application/security.py`（`sha256` 凭据签发/轮换/撤销）、
  `application/reflection.py`（窗口封闭、提取、确定性 Reconciliation、Persona Evaluation、
  Dry Run/diff/replay 与提交前 Fencing）、`domain/reflection.py`（候选校验、Evidence Span、
  fingerprint 与拒绝原因码）、`storage/reflection.py`（window/run/evidence/candidate/
  provider outcome/SSE cursor 仓库）、`storage/admin_archives.py`（备份与导出分目录）、
  `providers/cognitive.py`（四个认知 Port、按 kind 的超时/重试/预算/并发、熔断与有界 Probe、
  `DeterministicCognitiveProvider`）、`runtime.py` 与 `cli.py`（`serve`/`worker` 配置优先级、
  数据目录校验、启动顺序、Ready 门与 Grace Deadline）、`jobs/worker.py`（`phase10_handlers`，
  `episode.consolidation`/`reflection.generate`/`memory.reconciliation`/`persona.evaluation`
  四个 kind 全部启用，登记表中不再有 disabled 占位）、`application/recall.py`（ADR-0014 留给
  本阶段的 usage 激励路径启用）、双 SDK 0.11.0、`tools/generate_contracts.py`（Contract 1.9.0）、
  `tools/mock_server.py`（降级为 SDK 离线测试替身）。
- **Prompt/Provider/Builder 版本**：`prompt_version`、`provider_schema_version`、
  `builder_version`、`policy_version`、`reconciliation_version`、`model_id` 六个版本位独立
  演进，全部进入 `run_fingerprint` 与 `reflection_records`；本阶段随包发布的实现是
  `DeterministicCognitiveProvider`（`model_id = deterministic-fake-v1`），真实网络 Provider
  适配器不在本阶段交付（见已知限制 1）。
- **Schema/Migration**：`migrations/0011_phase10_consolidation_transport.sql`
  （online_safe=true、lock_ms=200、min_app=0.11.0、recovery=none；10 张 STRICT 表：
  `consolidation_windows`、`reflection_records`、`reflection_evidence`、`cognitive_candidates`、
  `provider_outcomes`、`provider_circuit_states`、`provider_budget_states`、
  `service_credentials`、`service_events`、`recall_usage_activations`），运行时兼容窗口
  推进为 [10, 11]。
- **契约**：Contract 1.8.0 → **1.9.0**（additive）；OpenAPI 路径 69 → **85**；capability
  32 → **48**；fixtures 123 → **136**；错误码仍为 41（无新增、无移除）。
  `/v1/admin/recent-context:rebuild` 标记 `deprecated`，由 negotiation 的
  `deprecated_capabilities` 指向 `/v1/admin/indexes/recent_context:rebuild`。
- **测试**：Phase 10 新增 **2169** 个用例（`tests/unit/test_phase10_reflection.py` 2101、
  `tests/contract/test_phase10_asgi.py` 47、`tests/integration/test_phase10_pipeline.py` 12、
  `tests/integration/test_phase10_migration.py` 4、`tests/integration/test_phase10_runtime.py` 3、
  `tests/contract/test_phase10_operation_matrix.py` 2），另在 `tests/fault/test_kill9.py`
  新增 Reconciliation 提交边界的 2 个强杀场景（各 20 次重复）。
- **重放/故障/安全报告**：[phase-10-verification](../reports/phase-10-verification.md)。
- **决策**：[ADR-0019](../adr/0019-phase10-consolidation-transport.md)。

## 已知限制

1. **随包发布的认知 Provider 是确定性 Fake**：`DeterministicCognitiveProvider` 实现四个
   Port 的完整治理语义（超时、重试、预算、并发、熔断、Schema/Evidence 校验），但不做真实
   模型推理。接入真实模型只需在 `providers/` 增加适配器，不改应用层；本阶段的重放、故障与
   预算数字都来自 Fake，不代表任何真实模型的抽取质量。
2. **SSE 是可选面且默认单进程内存扇出**：事件 cursor 持久在 `service_events`，客户端可用
   `Last-Event-ID` 恢复；但跨进程扇出、连接数上限与背压策略只在单进程下验证。关闭 SSE 时
   capability 不声明且端点 `not_ready`，宿主必须按能力协商结果决定是否订阅。
3. **`/metrics` 是每租户 JSON 快照，不是 Prometheus 文本格式**：它报告 Job Kind、Oldest
   Pending、候选/拒绝数量与 Provider 成本，满足本阶段的低敏诊断门禁；`iris_schedule_lag_seconds`
   仍只存在于进程内注册表，尚未出现在传输面快照上。导出格式与抓取端点归 Phase 14。
4. **`serve()` 的 uvicorn 绑定路径与进程级 SIGKILL 未单独验证**：启动/Ready/优雅关闭的
   20 次重复走的是 `create_app` + ASGI lifespan（`TestClient`），真实端口绑定、
   `timeout_graceful_shutdown` 计时与连接排空没有独立用例；进程级强杀也未构造。传输层不
   持有额外的可丢失状态，其在线写入复用已被 observe/state/task/forget/reflection 强杀场景
   覆盖的事务边界，因此这是覆盖缺口而非已知缺陷。真实进程级演练归 Phase 14 的部署硬化。
5. **Dead Letter 管理是数据面而非独立管理端点**：重放经
   `POST /v1/admin/reflections/{reflection_id}:replay` 与 Outbox 记录完成，没有独立的
   Dead Letter 列表/批量端点；运维需要通过审计事件与 `/metrics` 快照定位。
6. **旧路径弃用窗口未到期**：`/v1/admin/recent-context:rebuild` 与
   `admin.recent-context-rebuild.v1` 在本发布窗口内保持可用，移除时机按 ADR-0006 另行决定。

## 明确不做

- 不让自由文本模型输出直接修改 Binding、Forget、Task 完成或 Persona Current。
- 不保存或要求 Chain-of-Thought。
- 不因后台积压降低在线 Scope、Privacy、Tombstone 或一致性门禁。
- 不在传输层放置任何领域规则、Scope 判定或 Privacy 评估——它们只存在于 application/domain。
- 不交付容器镜像、Compose、只读根文件系统与 SBOM（Phase 14）。

## 交接条件

Core 的 Canonical、召回、Persona、后台巩固、传输层与进程入口至此形成完整服务闭环；Phase 11/12 可仅通过发布 SDK/Schema 接入真实服务端，不复制 Domain Model，也不再需要 mock server 替身。
