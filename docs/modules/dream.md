# 梦境整理模块入口

> 本文件是本主题的现行正文，在此唯一维护。既有要求、已批准契约、建议和待批准事项保持各自状态；迁移不新增产品决定或实现授权。文档关系见[总入口](../INDEX.md)。

适用主题与局部定义：梦境执行认知整理步骤；实例模式由运行模块拥有，消息暂存与回流由缓存模块拥有。

设计／审核参考：[冻结原始文档](../reference/companion_memory_module_design_provider_logging_config.md)。仅供追溯，不作为现行约束。

按关联工作联合阅读：[全部实现表达规范](../CODING_STANDARDS.md)；[产品：梦境整理、专注隔离与入口回流](../product/dream.md)；[产品：自我认知与稳定persona](../product/self-and-persona.md)；[产品：双指标、召回反馈、遗忘与删除](../product/lifecycle.md)。

返回[文档总入口](../INDEX.md)；实际进度见[工作状态](../work/STATUS.md)。

## 按边界定位

下表仅作导航；模块职责与端口正文在本文件后半部，共享事务和详细契约链接到各自唯一正文。

| 关注点 | 阅读位置与边界 |
| --- | --- |
| 职责、非职责、数据所有权 | [模块所有权行](../architecture/ownership.md#m11)；[模块职责与端口](dream.md#contract) |
| 公开能力与依赖 | 本模块正文的“建议端口”仍为草名；[统一依赖与启动边界](../architecture/ownership.md#source-line-267) |
| 状态、提交和恢复 | [接收承诺与允许损失](../architecture/persistence-and-transactions.md#section-04)；[T09](../architecture/persistence-and-transactions.md#t09)；[远程结果未知](../architecture/persistence-and-transactions.md#source-line-518)（仅涉及外部调用时） |
| 配置生效 | [统一快照及本模块相关参数](../architecture/configuration.md#source-line-878)；[命名空间与消费者](../architecture/configuration.md#source-line-858)。具体键按Schema固定，不自行填写默认值 |
| 产品验收定位 | [A38](../product/acceptance.md#a38)、[A49](../product/acceptance.md#a49)、[A67](../product/acceptance.md#a67)、[A94](../product/acceptance.md#a94)、[A106](../product/acceptance.md#a106)、[A108](../product/acceptance.md#a108)（任务范围扩大时按完整表补充） |
| 工程验收定位 | [V24](../architecture/acceptance.md#v24)、[V26](../architecture/acceptance.md#v26)、[V80](../architecture/acceptance.md#v80)（验收要求；实际覆盖见[STATUS](../work/STATUS.md)） |
| 尚未决定 | 专注入退场与回流见[已批准模式契约](../architecture/durable-ingress-and-batch-runtime.md#modes)；完整整理、非专注发布协调、管理员中止与恢复、扫描预算仍待细化。 [产品剩余契约](../product/decisions-and-delivery.md#section-22)；[工程冻结点](../architecture/implementation-options.md#source-line-1216)；[当前任务与阻塞](../work/CURRENT_TASK.md) |

<a id="contract"></a>

## 模块职责与端口

<a id="source-line-417"></a>

### M11 梦境整理

**拥有**：梦境运行与阶段检查点、被处理的变更范围、候选变更集和persona发布结果。实例业务模式由[M02](runtime.md#contract)拥有，消息暂存与回流由[M03](buffers.md#contract)拥有。

梦境使用增量待整理标记、到期索引和明确扫描预算，不能每天把数百万条记忆全部放进prompt。受影响认知按来源和关系成组审查；相同失去支持事件去重处理，另外的时间衰减独立计算。

每一步都以短事务提交已经校验的变更，远程调用在事务外；不是整个梦境持有一个全局数据库写锁。persona最终发布单独原子完成。已完成梦境步骤有操作键，重启继续时不重复扣分。

专注模式按[M02](runtime.md#contract)门控；输入只入[M03](buffers.md#contract)梦境序列，不额外调用媒体或学习模型。内部整理可以调用模型。中断不丢梦境缓存，不发布半份persona；恢复先读持久化模式/检查点，再明确恢复或退出。

**建议端口**：`start_dream`、`plan_dirty_work`、`run_step`、`checkpoint_step`、`finalize_dream`、`abort_with_recovery_state`。
