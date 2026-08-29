# 阶段 10：巩固与 Reflection

> 状态：Planned  
> 前置阶段：[阶段 9](./phase-09-persona.md)  
> 架构依据：[认知处理、Outbox/Schedule、Provider 边界与阶段 10](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md)

## 阶段目标

交付 Evidence 驱动、固定 Watermark、可重放且可降级的后台认知流水线，将 Observation 窗口转换为经过服务端验证的 Episode、Claim/Relation、Note/Task 候选和 Persona Proposal，而不影响 Canonical 在线写入与召回。

## 架构约束

- Provider 输出是不可信候选，不是原始事实；未知 Entity、无 Evidence、越权 Scope 或直接 Core 修改必须拒绝。
- 每个 Job 固定 Source Watermark 和 Source Revision，提交前重新验证来源仍有效且未 Tombstone。
- ReflectionRecord 记录输入、模型/Prompt/Policy 版本和候选，不能把自己的输出当新证据循环强化。
- 网络 Provider 调用不在 SQLite 写事务内，也不阻塞 Observe/Forget/Correct/Task/Persona 读取。
- Provider 失败进入 Retry/Dead Letter/Circuit Breaker，Recall 通过现有契约降级。

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

## 退出门禁

- [ ] 相同 Watermark、版本和确定性参数可重放得到同一候选集合/稳定差异说明。
- [ ] 无来源、未知主体、越权 Scope、任意代码 Trigger、Active Task 和 Persona Core 候选被拒绝。
- [ ] Source Revision 过期、Correct/Forget 或 Lease Fencing 后旧 Worker 无法提交。
- [ ] Reflection 自身输出不能作为同一结论的新独立 Evidence。
- [ ] Provider 超时、限流、无效 JSON、熔断和预算耗尽不影响 Canonical 在线功能。
- [ ] Dead Letter 可安全检查和以新 Outbox ID 重放，保留原任务引用与审计。

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

## 交接条件

Core 的 Canonical、召回、Persona、后台巩固与契约能力至此形成完整服务闭环；Phase 11/12 可仅通过发布 SDK/Schema 接入，不复制 Domain Model。
