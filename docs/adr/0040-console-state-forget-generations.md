# ADR-0040：State 删除与同键新代记录

状态：Accepted，2026-09-07。继续 [ADR-0037](0037-console-durable-forget-previews.md) 和 [ADR-0039](0039-console-focus-forget.md)。

## 删除语义

Console 固定集合 Forget 增加 State，支持 soft 和 erase。soft 写入 Tombstone，当前和历史读取均不可访问；erase 额外清除全部保留修订的 value、source_ref 和 coalesce_key。原 ID、修订编号、作用域、namespace、key、来源权威与时间仍是可审计的身份元数据。过期是业务有效期变化，不代替删除。

仅允许当前命名空间策略接受 user 写入的 State 被管理删除。命名空间策略撤回权限后，旧预览不能提交，资源详情隐藏删除动作，显式预览显示 protected。每次提交仍复核真实操作者、当前 Grant、Scope、Privacy、Revision、Hold 和删除水位；erase 要求最近重新认证。普通业务 Forget 的六类准入与广域 selector 不扩大。

全部删除事实、内容擦除、账本、Recall 保存响应清理、审计和持久失效任务同事务提交。历史擦除受 150ms / 2,000,000 SQLite 步骤预算限制，失败回滚整个固定批次，可用原幂等键重试。全局历史 State 的 Agent 可为空，实际存在的全局记录推进已有空 Agent 水位；备份早于创建的缺失记录只写原 ID Tombstone，不伪造水位归属。HTTP 创建仍要求 Agent。

## 同键重建与 Schema 18

State 原表内的 UNIQUE(scope_key, namespace, key) 永久占用旧键，无法同时保存删除历史与创建新代。Schema 18 离线重建 State 主表和修订子表，保留原行及外键；新增 deleted_us，并改为仅 deleted_us IS NULL 时唯一。旧 Tombstone 的 created_us 回填此标记。自然键查找先取唯一活记录，无活记录时返回最后一个历史记录；列表在 LIMIT 前排除删除记录。

deleted_us 仅用于自然键索引，Tombstone 仍是删除权威。固定 SQL 触发器在 State Tombstone 插入的同事务内同步标记；另一个固定触发器拒绝再次插入已有 Tombstone 的原 ID，覆盖备份早于创建的情况。触发器不调用用户代码或外部服务。备份一致性检查验证标记与 Tombstone 时间相符，旧 Schema 没有该字段时跳过这一新检查。

只有 Console 显式 state.create 且 expected_revision=0 可在旧记录已删除时创建不同 ID、revision=1 的记录。普通 State upsert 不隐式重建；已有活记录仍返回 Revision 冲突，旧 ID 与旧幂等回执不能读取新代或复活旧内容。新代与旧记录的历史、来源引用保持分离。

## 恢复与部署

现代删除账本可在 Schema 17 旧备份上重放，再升级到 18；重放后的 Tombstone 经迁移回填标记。备份早于资源创建也保留不可复活的原 ID。重放保留原选择器身份、应用归属、幂等键、模式和逻辑时间；恢复事务的审计操作者为 restore:deletion_ledger，忽略旧备份重新出现的 Hold 或策略对已完成删除事实的撤销，重复执行不重复写入。

Core 0.13.0 开发候选运行窗口升到 Schema 18。空库允许 bootstrap；已有库必须停服务并准备已验证备份后显式升级，旧 Migration 不修改。回退恢复旧备份且先应用最新删除账本，不能将已升级数据库直接交给只支持 Schema 17 的旧程序。SDK 与业务 HTTP 契约不扩大。

## 验证

覆盖两种删除模式、完整保留历史、同键不同 ID、旧幂等回执、全局历史记录、当前策略/Hold/Grant、批量失败回滚与预算中断；迁移验证原行保留、外键、索引唯一性、故障原子性，以及 Schema 17/18 的早于创建或删除备份重放。真实浏览器、完整 CI 与安装产物结果记入 [Phase 14 报告](../reports/phase-14-verification.md)。本决定不宣称 Phase 14 发布完成。
