# 阶段 10：巩固、Reflection 与传输层

> 状态：Completed（历史交付快照；不代表当前发布验收）  
> 开始日期：2026-09-04  
> 完成日期：2026-09-05  
> 前置阶段：[阶段 9](./phase-09-persona.md)  
> 阶段交付版本：0.11.0（阶段记录：Core/双 SDK 0.11.0、Schema 11、Contract 1.9.0）  
> 架构依据：[§3.1 进程与模块边界](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#31-进程与模块边界)、[§15.3 后台提炼](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#153-后台提炼)、[§16 Outbox 与 Worker](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#16-transactional-outbox-与-worker)、[§17.4 周期任务](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#174-周期任务)、[§23 HTTP API 与能力协商](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#23-http-api-与能力协商)、[§24 Provider 边界](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#24-provider-边界)、[§35.4 启停与优雅关闭](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#354-启停与优雅关闭)、[§36 阶段 10](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#阶段-10巩固reflection-与传输层)  

本页归档交付范围；测试实测、复审过程与限制集中在[验证报告](../reports/phase-10-verification.md)。后续状态与未关闭工作以[阶段索引](README.md)及[Phase 14](phase-14-hardening-release.md)为准。

## 阶段目标

交付固定 Watermark 的 Episode/Extraction/Reconciliation/Reflection/Persona Evaluation 流水线、Provider 治理与重放，以及真实 ASGI 传输层、Bearer 认证、能力协商、管理端点和 serve/worker 入口。此前仅有应用层及 mock 的传输缺口在本阶段关闭。

## 架构约束

- Provider 输出是不可信候选，不是原始事实；未知 Entity、无 Evidence、越权 Scope 或直接 Core 修改必须拒绝。
- 每个 Job 固定 Source Watermark 和 Source Revision，提交前重新验证来源仍有效且未 Tombstone。
- ReflectionRecord 记录输入、模型/Prompt/Policy 版本和候选，不能把自己的输出当新证据循环强化。
- 网络 Provider 调用不在 SQLite 写事务内，也不阻塞 Observe/Forget/Correct/Task/Persona 读取。
- Provider 失败进入 Retry/Dead Letter/Circuit Breaker，Recall 通过现有契约降级。
- 传输层只做认证、校验、编解码与错误映射，不含领域规则：`AccessContext` 由服务端凭据推导，请求体只能收窄权限（§5.4、ADR-0002）。
- 传输层不得为了适配某条已发布路径而改变应用层语义；形状不匹配时按 ADR-0006 走新增可选字段或新 Capability。

## 需求追踪

| 需求 ID | 历史需求范围 |
| --- | --- |
| P10-EPISODE-01 | 固定 Watermark 的有界 Episode Consolidation |
| P10-EXTRACT-01 | 严格 Schema、Evidence Span 与确定性 Reconciliation |
| P10-REFLECT-01 | ReflectionRecord 可重放且防止自循环强化 |
| P10-PROVIDER-01 | Provider 最小输入、超时、预算、熔断与输出校验 |
| P10-FENCING-01 | 固定 Source Revision/Watermark 与提交前 Fencing |
| P10-OPERATIONS-01 | Dry Run、差异、审计重放与 Dead Letter 管理 |
| P10-TRANSPORT-01 | §23 的传输层：认证、AccessContext、错误 Envelope、幂等头与协商 |
| P10-PROCESS-01 | §3.1/§35.4 的 `serve`/`worker` 进程入口与优雅关闭 |
| P10-SURFACE-01 | 补齐仅缺传输面的 Entity/Identity/Binding/SpaceGroup 与管理端点 |

需求对应的实现、测试与复审证据统一见[验证报告](../reports/phase-10-verification.md)。

## 工作包

10.1 Episode Consolidation；10.2 提取与协调；10.3 Reflection 与 Persona Evaluation；10.4 Provider 治理；10.5 可重放与运维；10.6 HTTP 传输层与进程入口；10.7 补齐仅缺传输面的端点。

实现结果和复审记录统一见验证报告。

## 数据、契约与回退策略

本阶段 Migration、契约版本、兼容窗口和恢复证据见[验证报告](../reports/phase-10-verification.md)；决策依据：[ADR-0017](../adr/0017-contract-surface-alignment.md)、[ADR-0019](../adr/0019-phase10-consolidation-transport.md)。阶段版本是历史快照，不能作为当前部署支持范围；当前版本读取 [version-manifest](../../schemas/version-manifest.json)，升级/回退按[Phase 14](phase-14-hardening-release.md)验证。

## 量化验收基线

确定性重放、非法候选/Fencing、Provider 故障、ASGI operation 成败矩阵与生命周期的历史实测与规模条件见[验证报告](../reports/phase-10-verification.md)；原计划的逐项门槛保存在[原阶段验收目标](../reports/phase-10-verification.md#原阶段验收目标)，未测目标不会因归档消失。

## 退出门禁

本阶段按[验证报告](../reports/phase-10-verification.md)记录关闭；报告中的测试规模、版本和基准只适用于当时快照。限制项不因 Completed 状态自动消失，正式发布须重新通过 Phase 14 门禁。

## 交付证据

- [阶段验证报告](../reports/phase-10-verification.md)：实现/测试追踪、复审修复、历史门禁和限制。
- 决策：[ADR-0017](../adr/0017-contract-surface-alignment.md)、[ADR-0019](../adr/0019-phase10-consolidation-transport.md)。
- [当前契约源](../../contracts/source/contracts.json)与[迁移文件](../../migrations/)；不在阶段摘要中复制版本、checksum 或端点清单。

## 已知限制

以[验证报告中的限制](../reports/phase-10-verification.md#5-已知限制)为唯一记录；报告同时标出已由后续阶段关闭的历史缺口。

## 明确不做

- 不让自由文本模型输出直接修改 Binding、Forget、Task 完成或 Persona Current。
- 不保存或要求 Chain-of-Thought。
- 不因后台积压降低在线 Scope、Privacy、Tombstone 或一致性门禁。
- 不在传输层放置任何领域规则、Scope 判定或 Privacy 评估——它们只存在于 application/domain。
- 不交付容器镜像、Compose、只读根文件系统与 SBOM（Phase 14）。

## 交接条件

本阶段能力已进入后续集成基线。新增工作遵循[阶段索引](README.md)与[Phase 14](phase-14-hardening-release.md)的依赖和验收，不重复执行本页历史工作包。
