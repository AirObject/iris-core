# ADR-0039：Focus 删除、工作集容量与恢复

状态：Accepted，2026-09-07。继续 [ADR-0037](0037-console-durable-forget-previews.md) 和 [ADR-0038](0038-console-entity-tombstone-ledger.md)。

## 删除与生命周期

Focus 加入 Console 的固定集合删除预览，支持 soft 和 erase。dismissed、expired、promoted 仍是原业务状态，删除不伪造成一次状态转换。soft 写 Tombstone 并禁止普通当前/历史读取；erase 额外清除当前摘要及所有修订的摘要、结构化内容、来源、隐私标签和自由文本提升策略。保留原 ID、修订、状态及提升目标 ID，不删除其来源事实或提升出的对象。

管理删除复用 ForgetService 的同事务账本、真实操作者审计、Evidence 失效、来源水位和 memory.invalidated 持久任务。Recall 已保存响应在提交事务内清理。静态权限为 memory.forget/read 与 console.manage；每次预览、提交和重放回执都复核当前 Grant、Scope、Privacy、Revision、Hold 与删除水位。erase 需要最近重新认证。

此切片仅扩大受控管理命令的显式目标集合，普通业务 Forget 及其广域 selector 保持原有六类准入。Focus 没有公开 SQL 或私有存储入口；固定目标上限仍为 50，较大集合和筛选 Operation 留待后续切片。

## 工作集与事务预算

现有 Focus 容量、列表和维护查询原先可能把 Tombstone 项计入结果。查询在排序/LIMIT 前排除删除项，防止已删除 Focus 阻止新建、占用容量或挤占维护名额。删除不通过新增一条 dismissed 修订规避此问题。

全部历史内容擦除使用受限 SQLite 执行预算，超时或超过步骤上限整体失败并回滚，不返回部分成功；大型历史的维护策略不能通过放宽权限或假称完成替代。

## 恢复与兼容

删除账本重放保留原逻辑身份、时间和模式。旧备份中已有 Focus 按其恢复时当前修订执行擦除；备份早于创建时仍记录不可复活的原 ID Tombstone，不伪造 Agent 或水位归属。重复重放不重复写入。恢复不以旧备份的 Hold 撤销已经完成的删除事实。

Core 0.13.0 开发候选继续使用 Schema 17；本切片不新增表或修改旧 Migration，SDK 与业务 HTTP 契约不扩大。Console 原有两个 Forget 操作增加 Focus 目标枚举与能力发现。回退按 Schema 17 的安装/备份策略执行；旧 Console 不显示新目标不构成恢复已删除内容的授权。

## 验证

覆盖 soft/erase 所有修订、当前/历史拒绝、最近认证、Hold/并发预览、50 项原子提交、混合批次失败回滚、Recall 响应清理、持久任务处理、删除后容量/维护名额及早于创建/删除的备份恢复。真实浏览器、安装物和完整 CI 结果记录在 [Phase 14 报告](../reports/phase-14-verification.md)。本 ADR 不声明其余 Phase 14 发布门禁完成。
