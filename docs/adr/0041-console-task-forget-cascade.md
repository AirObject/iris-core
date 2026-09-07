# ADR-0041：Task 删除、子对象与投递事实

状态：Accepted，2026-09-07。继续 [ADR-0037](0037-console-durable-forget-previews.md) 和 [ADR-0040](0040-console-state-forget-generations.md)。实现与验收证据分别记账；本决定不表示功能已完成集成。

## 准入与保护

Console 固定集合 Forget 增加 Task 主资源，支持 soft 和 erase。仅 completed、cancelled、archived 的 Task 可删除；删除不推进任务状态，不生成完成证据。未删除的独立子 Task 计划阻止父 Task 删除，须先分别处理子计划，不将其隐式纳入级联。

没有 completed_us 的 Task，如果当前 source_refs 引用尚未归档的 promise/follow_up Note，继续按未兑现承诺保护。取消 Task 或删除来源 Note 不代表兑现；明确归档来源承诺才解除这一保护。已经完成的 Task 以既有完成事实判断，不再要求归档来源。来源验证最多 100 条。普通业务 Forget 的六类资源及广域选择器不扩大。

每次预览与提交都复核当前操作者、Grant、Scope、Privacy、Revision、Legal Hold 和删除水位；erase 要求最近重新认证。Task 的步骤、依赖、触发器和待处理投递事件分别经过可见与可修改检查，不能借父资源权限覆盖更严格的子资源。任何不可见成员都令父目标无法提交，预览不得泄露其标识或正文。

## 有界级联与擦除

一次固定提交最多 50 个根目标，所有 Task 合计最多 500 个级联成员。成员包括属于 Task 的 Step、Dependency（包含已移除的边）、Trigger，以及指向该 Task 或其 Step 的 pending/delivered CognitiveEvent。成员集合和内容版本进入预览哈希，公开预览仅显示分类数量。超限拒绝整次预览或提交；不截断、不悄悄转成部分成功。独立子 Task 不属于这些成员。

soft 为 Task、Step、Dependency、Trigger 写入 Tombstone；erase 另外清除所有保留 Task 修订的 title、goal、next_action、progress_note、source_refs 和隐私标签，全部 Step 修订的 title、description、expected_effect、completion_evidence_refs 和隐私标签，以及全部 Trigger 修订的 schedule_spec/condition_spec。主表冗余正文同时擦除。身份、修订编号、作用域、所有权、步骤 stable_key、生命周期状态和时间、依赖结构、触发器配置开关及 occurrence 身份保留，不伪造业务状态变化。

待处理事件通过既有事件状态转换取消，保存实际操作者审计、投递次数和既有修订。已 ACK、过期或取消事件不改写，也不创建事件 Tombstone 或虚构 ACK。历史 ACK 不等于 Task/Step 已完成。触发器扫描和 Task 列表在 LIMIT 前排除删除对象，恢复旧库中重新出现的 active 状态也不能继续投递或占满可用批次。

父子 Tombstone、内容擦除、事件取消、审计、水位、删除账本和持久失效任务同事务提交。失败回滚全部根目标，原幂等键可以重试。成员查询、子计划检查和历史擦除各受 150ms / 2,000,000 SQLite 步骤预算限制；旧响应与投影通过现有失效链清理，最终读取继续受 Tombstone 约束。选定根资源之间按引用关系先删引用方；包含 Task 子对象的引用，循环关系拒绝提交。

## Schema 19 与恢复

Schema 19 仅增加五个固定索引，覆盖事件目标与状态、父子 Task、Step/Trigger 历史和 Task 的 Trigger 查找；不修改旧 Migration 或领域行。Core 0.13.0 开发候选运行窗口升至 Schema 19，空库与 Schema 18 的在线升级均须验证。该变化不增加业务 SDK 方法。

账本保存一个已承诺的 Task 根选择器，恢复在旧备份中重新枚举其子对象，重放对应删除模式及事件取消。已完成的删除事实不被旧备份的活跃状态、Hold 或来源承诺撤销，也不将旧任务伪改为 completed。备份早于创建时保留原 Task ID 的 Tombstone；重复重放不重复写入。Schema 18 可先重放账本再应用 Schema 19 索引。恢复保留原选择器、应用归属、幂等键和逻辑时间，审计操作者为 restore:deletion_ledger。

## 验证

验证终态保护、来源承诺、独立子计划、子资源隐私、Hold、预览变化、两种模式、事件 ACK/投递事实、50 根与 500 成员边界、混合批次失败回滚、历史擦除预算、查询饥饿，以及旧备份和创建前备份恢复。还须完成当前契约/迁移兼容、真实浏览器、完整 CI 和独立安装验证；进展记入 [Phase 14 报告](../reports/phase-14-verification.md)。
