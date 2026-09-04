# 阶段 10：巩固、Reflection 与传输层

> 状态：Planned  
> 前置阶段：[阶段 9](./phase-09-persona.md)  
> 目标版本：0.11.0  
> 架构依据：[§3.1 进程与模块边界](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#31-进程与模块边界)、[§15.3 后台提炼](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#153-后台提炼)、[§16 Outbox 与 Worker](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#16-transactional-outbox-与-worker)、[§17.4 周期任务](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#174-周期任务)、[§23 HTTP API 与能力协商](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#23-http-api-与能力协商)、[§24 Provider 边界](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#24-provider-边界)、[§35.4 启停与优雅关闭](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#354-启停与优雅关闭)、[§36 阶段 10](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#阶段-10巩固reflection-与传输层)  
> 决策记录：[ADR-0017 §3](../adr/0017-contract-surface-alignment.md)（传输层归属本阶段）

## 阶段目标

交付两件事，Core 至此形成可被宿主真实调用的完整服务闭环：

1. Evidence 驱动、固定 Watermark、可重放且可降级的后台认知流水线，将 Observation 窗口转换为经过服务端验证的 Episode、Claim/Relation、Note/Task 候选和 Persona Proposal，而不影响 Canonical 在线写入与召回。
2. **HTTP 传输层与进程入口**：Phase 2–8 交付的是应用层服务 + 生成契约 + mock server，已发布 OpenAPI 的 61 条路径至今没有真实服务端。本阶段补齐传输面，使 Phase 11/12 的 Adapter 能够真正接入（ADR-0017 §3）。

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

- [ ] 相同 Watermark、版本和确定性参数可重放得到同一候选集合/稳定差异说明。
- [ ] 无来源、未知主体、越权 Scope、任意代码 Trigger、Active Task 和 Persona Core 候选被拒绝。
- [ ] Source Revision 过期、Correct/Forget 或 Lease Fencing 后旧 Worker 无法提交。
- [ ] Reflection 自身输出不能作为同一结论的新独立 Evidence。
- [ ] Provider 超时、限流、无效 JSON、熔断和预算耗尽不影响 Canonical 在线功能。
- [ ] Dead Letter 可安全检查和以新 Outbox ID 重放，保留原任务引用与审计。
- [ ] 已发布 OpenAPI 的每条路径都有真实传输层实现，并通过成功/失败双向契约测试。
- [ ] 认证、AccessContext 推导与 Body 收窄在越权矩阵下无提权路径。
- [ ] 契约错误码全部可由传输层产生，且响应不含 Stack Trace、路径、Secret 或正文。
- [ ] `serve`/`worker` 可在干净环境启动、通过 Ready 门、优雅关闭并从强杀恢复。
- [ ] Migration/Job/Prompt/Provider/传输层兼容和回退方案、需求追踪及交付证据已完成评审。

## 交付证据

- 代码/变更：待补充
- Prompt/Provider/Builder 版本：待补充
- Schema/Migration：待补充
- 重放/故障/安全报告：待补充
- 已知限制：待补充

## 明确不做

- 不让自由文本模型输出直接修改 Binding、Forget、Task 完成或 Persona Current。
- 不保存或要求 Chain-of-Thought。
- 不因后台积压降低在线 Scope、Privacy、Tombstone 或一致性门禁。
- 不在传输层放置任何领域规则、Scope 判定或 Privacy 评估——它们只存在于 application/domain。
- 不交付容器镜像、Compose、只读根文件系统与 SBOM（Phase 14）。

## 交接条件

Core 的 Canonical、召回、Persona、后台巩固、传输层与进程入口至此形成完整服务闭环；Phase 11/12 可仅通过发布 SDK/Schema 接入真实服务端，不复制 Domain Model，也不再需要 mock server 替身。
