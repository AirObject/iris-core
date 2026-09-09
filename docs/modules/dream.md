# 梦境整理模块入口

> 本文件是权威原文的阅读视图，不是独立需求。原文仍是权威依据；后续修改规则时，先更新对应原文，再同步本视图与[覆盖映射](../work/ORGANIZATION_REPORT.md#coverage)。保留原文“已确定、建议、示例、待确认”的性质；下列导读不新增决策。正文含原有编号，仅限文档追踪。

适用主题与局部定义：梦境执行认知整理步骤；实例模式由运行模块拥有，消息暂存与回流由缓存模块拥有。

来源：[原文 L417–L428](../../companion_memory_module_design_provider_logging_config.md#section-06)。行号对应整理时的哈希基线。

按关联工作联合阅读：[全部实现表达规范](../../CODING_STANDARDS.md)；[产品：梦境整理、专注隔离与入口回流](../product/dream.md)；[产品：自我认知与稳定persona](../product/self-and-persona.md)；[产品：双指标、召回反馈、遗忘与删除](../product/lifecycle.md)。

返回[文档总入口](../INDEX.md)；实际进度见[工作状态](../work/STATUS.md)。

## 按边界定位

下表是阅读路线；完整模块原文在本文件后半部，共享事务和详细设计仅通过链接引用。

| 关注点 | 阅读位置与边界 |
| --- | --- |
| 职责、非职责、数据所有权 | [模块所有权行](../architecture/ownership.md#m11)；[本模块完整契约](dream.md#contract) |
| 公开能力与依赖 | 本模块正文的“建议端口”仍为草名；[统一依赖与启动边界](../architecture/ownership.md#source-line-267) |
| 状态、提交和恢复 | [接收承诺与允许损失](../architecture/persistence-and-transactions.md#section-04)；[T09](../architecture/persistence-and-transactions.md#t09)；[远程结果未知](../architecture/persistence-and-transactions.md#source-line-518)（仅涉及外部调用时） |
| 配置生效 | [统一快照及本模块相关参数](../architecture/configuration.md#source-line-878)；[命名空间与消费者](../architecture/configuration.md#source-line-858)。具体键按Schema固定，不自行填写默认值 |
| 产品验收定位 | [A38](../product/acceptance.md#a38)、[A49](../product/acceptance.md#a49)、[A67](../product/acceptance.md#a67)、[A94](../product/acceptance.md#a94)、[A106](../product/acceptance.md#a106)、[A108](../product/acceptance.md#a108)（任务范围扩大时按完整表补充） |
| 工程验收定位 | [V24](../architecture/acceptance.md#v24)、[V26](../architecture/acceptance.md#v26)、[V80](../architecture/acceptance.md#v80)（预期行为，未执行） |
| 尚未决定 | 专注入退场、非专注发布协调、管理员中止与恢复、扫描预算待固定。 [产品剩余契约](../product/decisions-and-delivery.md#section-22)；[工程冻结点](../architecture/implementation-options.md#source-line-1216)；[整理待确认项](../work/ORGANIZATION_REPORT.md#open-items) |

<a id="contract"></a>

## 完整模块原文阅读视图

<a id="source-line-417"></a>

### M11 梦境整理

**拥有**：梦境运行与阶段检查点、被处理的变更范围、候选变更集和persona发布结果。实例业务模式由[M02](runtime.md#contract)拥有，消息暂存与回流由[M03](buffers.md#contract)拥有。

梦境使用增量待整理标记、到期索引和明确扫描预算，不能每天把数百万条记忆全部放进prompt。受影响认知按来源和关系成组审查；相同失去支持事件去重处理，另外的时间衰减独立计算。

每一步都以短事务提交已经校验的变更，远程调用在事务外；不是整个梦境持有一个全局数据库写锁。persona最终发布单独原子完成。已完成梦境步骤有操作键，重启继续时不重复扣分。

专注模式按[M02](runtime.md#contract)门控；输入只入[M03](buffers.md#contract)梦境序列，不额外调用媒体或学习模型。内部整理可以调用模型。中断不丢梦境缓存，不发布半份persona；恢复先读持久化模式/检查点，再明确恢复或退出。

**建议端口**：`start_dream`、`plan_dirty_work`、`run_step`、`checkpoint_step`、`finalize_dream`、`abort_with_recovery_state`。
