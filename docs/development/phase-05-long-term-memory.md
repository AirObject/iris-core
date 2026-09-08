# 阶段 5：显式长期记忆与 Episode

2026-09-08 新实施项：[Observation 全量上下文与批量总结](observation-context.md)。统一使用 Observation 保存背景/交互原始事件，复用 Episode 保存分组摘要；自动总结显式开启，背景默认保留 30 天可配置。已接通实现，使用方式与本轮验证进度见链接说明；下文历史验收记录仅代表当时版本。

> 状态：Completed（历史交付快照；不代表当前发布验收）  
> 前置阶段：[阶段 4](./phase-04-notes-tasks-events.md)  
> 阶段交付版本：0.6.0（阶段记录：Core/Python SDK/TypeScript SDK 0.6.0，Schema 6，契约 1.4.0）  
> 架构依据：[§13 长期记忆模型](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#13-长期记忆模型)、[§19 Remember/Correct/Forget](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#19-remembercorrectforget-与保留)、[§21 备份恢复与导出](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#21-备份恢复与导出)、[§29 安全与隐私](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#29-安全与隐私)、[§36 阶段 5](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#阶段-5显式长期记忆与-episode)

本页归档交付范围；测试实测、复审过程与限制集中在[验证报告](../reports/phase-05-verification.md)。后续状态与未关闭工作以[阶段索引](README.md)及[Phase 14](phase-14-hardening-release.md)为准。

## 阶段目标

交付 Remember/Correct/Forget、Claim/Evidence/Relation/Episode/Artifact、双时态历史、Retention/Legal Hold、删除账本和恢复后的删除重放。

## 架构约束

- 每个 Active Claim 至少有一条有效 Evidence；模型推断和文本相似只产生候选，不能直接覆盖高权威事实。
- Correct 创建新 Revision/Supersede/Dispute 和 Evidence，不原地覆盖 Canonical Text。
- Forget 先同步影响 Canonical 读取，投影清理由 Outbox 异步完成；所有未来 Builder 必须比较 Tombstone Watermark。
- Confidence、Importance、Accessibility、Activation 和情感分值分开演化，召回使用不能提高 Confidence。
- Episode 不等于 Session，不允许跨 Space 拼接原始内容；Procedure Claim 不保存可执行代码。

## 需求追踪

| 需求 ID | 历史需求范围 |
| --- | --- |
| P5-MEMORY-01 | Episode、Claim、Evidence 的来源、Revision 与有效状态 |
| P5-RESOURCE-01 | Relation/Artifact 的 Evidence、Scope 与安全存储 |
| P5-COMMAND-01 | Remember/Correct/Forget/Search 的幂等与权限语义 |
| P5-HISTORY-01 | 双时态、`as_of` 和发生时/当前身份读取 |
| P5-ERASURE-01 | Tombstone 同步生效且在后台、缓存和恢复路径不可复活 |
| P5-RETENTION-01 | Retention、Legal Hold、Archive 与隐私删除分离 |

需求对应的实现、测试与复审证据统一见[验证报告](../reports/phase-05-verification.md)。

## 工作包

5.1 Episode、Claim 与 Evidence；5.2 Relation 与 Artifact；5.3 显式记忆 API；5.4 双时态与历史读取；5.5 保留、Tombstone 与恢复。

实现结果和复审记录统一见验证报告。

## 数据、契约与回退策略

本阶段 Migration、契约版本、兼容窗口和恢复证据见[验证报告](../reports/phase-05-verification.md)；决策依据：[ADR-0013](../adr/0013-phase5-long-term-memory.md)。阶段版本是历史快照，不能作为当前部署支持范围；当前版本读取 [version-manifest](../../schemas/version-manifest.json)，升级/回退按[Phase 14](phase-14-hardening-release.md)验证。

## 量化验收基线

Evidence/双时态、Forget 泄漏与强杀、Artifact 校验、删除账本恢复及 Canonical 延迟的历史实测与规模条件见[验证报告](../reports/phase-05-verification.md)；原计划的逐项门槛保存在[原阶段验收目标](../reports/phase-05-verification.md#原阶段验收目标)，未测目标不会因归档消失。

## 退出门禁

本阶段按[验证报告](../reports/phase-05-verification.md)记录关闭；报告中的测试规模、版本和基准只适用于当时快照。限制项不因 Completed 状态自动消失，正式发布须重新通过 Phase 14 门禁。

## 交付证据

- [阶段验证报告](../reports/phase-05-verification.md)：实现/测试追踪、复审修复、历史门禁和限制。
- 决策：[ADR-0013](../adr/0013-phase5-long-term-memory.md)。
- [当前契约源](../../contracts/source/contracts.json)与[迁移文件](../../migrations/)；不在阶段摘要中复制版本、checksum 或端点清单。

## 已知限制

以[验证报告中的限制](../reports/phase-05-verification.md#8-已知限制)为唯一记录；报告同时标出已由后续阶段关闭的历史缺口。

## 明确不做

- 不实现 LLM 自动提取、FTS、Embedding、Graph 多跳或 Profile 汇总。
- 不用相似度自动合并主体、Scope、有效时间不同的 Claim。
- 不把认知衰减伪装成隐私删除。

## 交接条件

本阶段能力已进入后续集成基线。新增工作遵循[阶段索引](README.md)与[Phase 14](phase-14-hardening-release.md)的依赖和验收，不重复执行本页历史工作包。
