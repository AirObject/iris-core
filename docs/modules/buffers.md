# 入口缓存与批次模块入口

> 本文件是本主题的现行正文，在此唯一维护。既有要求、已批准契约、建议和待批准事项保持各自状态；迁移不新增产品决定或实现授权。文档关系见[总入口](../INDEX.md)。

适用主题与局部定义：每入口独立队列；S3历史辅助、S2唯一目标、S1最新辅助，冻结快照不吸收后到输入。

设计／审核参考：[冻结原始文档](../reference/companion_memory_module_design_provider_logging_config.md)。仅供追溯，不作为现行约束。

按关联工作联合阅读：[全部实现表达规范](../CODING_STANDARDS.md)；[产品：入口队列、冻结批次、终结与学习](../product/batches-and-learning.md)；[产品：批次来源、正式记忆与关系](../product/provenance-and-memory.md)；[产品：梦境整理、专注隔离与入口回流](../product/dream.md)。

返回[文档总入口](../INDEX.md)；实际进度见[工作状态](../work/STATUS.md)。

## 按边界定位

本次冻结、三终态和回流细节见[持久接入与批次运行契约](../architecture/durable-ingress-and-batch-runtime.md#batch)（五组推荐已批准，实施已授权；验证状态见CURRENT_TASK）；本模块原职责和既有批准状态不变。

下表仅作导航；模块职责与端口正文在本文件后半部，共享事务和详细契约链接到各自唯一正文。

| 关注点 | 阅读位置与边界 |
| --- | --- |
| 职责、非职责、数据所有权 | [模块所有权行](../architecture/ownership.md#m03)；[模块职责与端口](buffers.md#contract) |
| 公开能力与依赖 | 本模块正文的“建议端口”仍为草名；[统一依赖与启动边界](../architecture/ownership.md#source-line-267) |
| 状态、提交和恢复 | [接收承诺与允许损失](../architecture/persistence-and-transactions.md#section-04)；[T01](../architecture/persistence-and-transactions.md#t01)、[T02](../architecture/persistence-and-transactions.md#t02)、[T03](../architecture/persistence-and-transactions.md#t03)、[T04](../architecture/persistence-and-transactions.md#t04)、[T05](../architecture/persistence-and-transactions.md#t05)、[T06](../architecture/persistence-and-transactions.md#t06)；[远程结果未知](../architecture/persistence-and-transactions.md#source-line-518)（仅涉及外部调用时） |
| 配置生效 | [统一快照及本模块相关参数](../architecture/configuration.md#source-line-919)；[命名空间与消费者](../architecture/configuration.md#source-line-858)。具体键按Schema固定，不自行填写默认值 |
| 产品验收定位 | [A07](../product/acceptance.md#a07)、[A08](../product/acceptance.md#a08)、[A10](../product/acceptance.md#a10)、[A69](../product/acceptance.md#a69)、[A71](../product/acceptance.md#a71)、[A75](../product/acceptance.md#a75)、[A76](../product/acceptance.md#a76)、[A77](../product/acceptance.md#a77)、[A80](../product/acceptance.md#a80)、[A82](../product/acceptance.md#a82)、[A95](../product/acceptance.md#a95)、[A103](../product/acceptance.md#a103)（任务范围扩大时按完整表补充） |
| 工程验收定位 | [V01](../architecture/acceptance.md#v01)、[V02](../architecture/acceptance.md#v02)、[V04](../architecture/acceptance.md#v04)、[V05](../architecture/acceptance.md#v05)、[V08](../architecture/acceptance.md#v08)、[V25](../architecture/acceptance.md#v25)、[V79](../architecture/acceptance.md#v79)（预期行为，未执行） |
| 尚未决定 | 短批／空闲尾部关闭及固定窗口预算见[已批准配置](../architecture/configuration.md#runtime-compatible-budget-candidate)；本机SQLite验证采用已批准，生产存储及未采纳切分策略仍未批准。 [产品剩余契约](../product/decisions-and-delivery.md#section-22)；[工程冻结点](../architecture/implementation-options.md#source-line-1216)；[当前任务与阻塞](../work/CURRENT_TASK.md) |

<a id="contract"></a>

## 模块职责与端口

<a id="source-line-317"></a>

### M03 入口缓存与批次

**拥有**：原始消息的队列引用、每入口序列号、三段成员、冻结范围、批次终态、梦境位置与回流游标。原始内容可以共享一份不可变消息对象，队列和正式来源分别持有引用。

建议用持久化`message_id + entry_seq + placement`表达队列，而非只保存内存deque。`placement`可表示正常最新段、目标候选、梦境暂存等；历史辅助和批次快照通过显式成员引用表达。同一消息可既是已保存来源中的辅助，又仍是后续待总结输入，这不是重复学习。

学习材料按S3→S2→S1冻结，只总结S2。轮次结束按目标ID精确轮转。成功及零结果保留S2尾部；普通provider失败耗尽同样留尾但标失败；敏感拒学终结不留尾，清空该入口旧历史辅助引用，同时保护S1和后到输入。

梦境暂存无业务条数上限，保存于持久化表而非无限内存数组。回流通过同一事务迁移消息位置/引用；移交完成和学习完成分开计数。新实时输入不能越过该入口仍未移交的旧消息。一个入口的回流、失败或清空不改变其他入口的游标。

**建议端口**：`append_input`、`freeze_batch`、`get_batch_snapshot`、`finalize_batch`、`transfer_dream_page`、`get_entry_recent_view`、`get_pending_metadata`。
