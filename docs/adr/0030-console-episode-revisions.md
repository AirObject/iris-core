# ADR-0030：Episode 管理修订与当前指针同步

状态：Accepted，2026-09-07。实现 [Console 资源矩阵](../design/console-backend.md#51-资源矩阵) 的 Episode 创建、摘要/边界/来源修订和状态管理，沿用 [ADR-0025](0025-console-command-authorization.md) 的管理身份与事务授权。

## 专属修订命令

EpisodeService 接收经当前 Grant 验证的 CommandActor，创建真实 Episode 或追加不可变 Revision。创建接受标题、摘要、参与实体、Observation/其他 SourceRef、评分、时间边界及显式隐私标签。更新只允许标题、摘要、参与实体、引用、评分和时间；不能覆盖 Scope、隐私信封、extractor_version、状态或历史修订。参与实体与所有来源必须当前可见，Observation 必须已确认、未删除并位于 Episode 的 Scope 信封内，不允许跨空间拼接。

管理输入最多 64 个参与实体、64 条 Observation 引用、100 条其他来源，去重后所有引用合计最多 100 个。固定修订经过读面授权，非法资源种类、错误修订、未知字段和反向时间边界在写入前拒绝。此入口写入操作者输入的摘要，不自动复制来源正文，也不伪造原观察的时间、身份或外部效果。

只有 open/sealed 片段可以编辑。更新以 expected_revision 做 CAS，追加修订后在同一事务推进 Current、审计、水位和 Outbox。旧标题、摘要、参与者、时间及来源仍在原修订中。召回收集结束后的 Final Rehydrate 使用当前修订号和内容哈希，期间发生更新时丢弃旧候选。

## 状态与当前表

状态动作复用既有 Episode 状态机：open 可封存、归档或被取代，sealed 可归档或被取代，archived 可重开，superseded/tombstoned 不可继续流转。封存结束时间取服务端时钟；管理动作拒绝在未来开始时间之前封存。

EpisodeRepository 在同一 CAS 中从新修订同步 Current 的标题、重要度、开始与结束时间。此前状态流转只更新指针和状态，封存结束时间可能只存在于修订；以后发生指针推进时同步这些冗余字段。没有改写历史修订或批量重写已有数据库，本切片不新增 Migration。

所有管理路径保持真实操作者审计、幂等和事务故障回滚，普通业务 create/transition 的 Required Surface 检查保留。重放重新授权当前目标、来源、参与实体和固定结果修订；被删除或不可见时不返回旧成功。Console 详情增加参与实体与分开的原始来源/Observation 引用，先验证完整依赖再返回，避免编辑表单遗漏既有来源。

## 验证与交付

Console 1.1.0 开发候选新增创建 POST、更新 PATCH、状态 POST 和六个 Schema，提供专属表单与动态动作。真实浏览器验证创建、编辑摘要/边界、封存、归档、刷新和旧摘要历史；领域测试覆盖权限、重放、回滚、召回修订竞争和备份恢复后的修订/删除标记。Core 0.13.0 / Schema 15 保持不变，业务 Episode HTTP/SDK 方法不新增更新入口。

本决定只关闭 Episode 管理切片，其余管理模块与生产发布门禁继续按 [Phase 14](../development/phase-14-hardening-release.md) 实施。
