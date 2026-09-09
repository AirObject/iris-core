# 多目标与意图模块入口

> 本文件是权威原文的阅读视图，不是独立需求。原文仍是权威依据；后续修改规则时，先更新对应原文，再同步受影响的视图与相对链接；[覆盖映射](../work/ORGANIZATION_REPORT.md#coverage)仅作整理历史保留，不要求持续更新。保留原文“已确定、建议、示例、待确认”的性质；下列导读不新增决策。正文含原有编号，仅限文档追踪。

适用主题与局部定义：目标是未来意图；过期不等于完成或放弃。目标提醒计划持久化，投递仍然有限尽力。

来源：[原文 L405–L416](../../companion_memory_module_design_provider_logging_config.md#section-06)。行号对应整理时的哈希基线。

按关联工作联合阅读：[全部实现表达规范](../../CODING_STANDARDS.md)；[产品：多目标、去重与提醒](../product/goals.md)。

返回[文档总入口](../INDEX.md)；实际进度见[工作状态](../work/STATUS.md)。

## 按边界定位

下表是阅读路线；完整模块原文在本文件后半部，共享事务和详细设计仅通过链接引用。

| 关注点 | 阅读位置与边界 |
| --- | --- |
| 职责、非职责、数据所有权 | [模块所有权行](../architecture/ownership.md#m10)；[本模块完整契约](goals.md#contract) |
| 公开能力与依赖 | 本模块正文的“建议端口”仍为草名；[统一依赖与启动边界](../architecture/ownership.md#source-line-267) |
| 状态、提交和恢复 | [接收承诺与允许损失](../architecture/persistence-and-transactions.md#section-04)；[T08](../architecture/persistence-and-transactions.md#t08)；[远程结果未知](../architecture/persistence-and-transactions.md#source-line-518)（仅涉及外部调用时） |
| 配置生效 | [统一快照及本模块相关参数](../architecture/configuration.md#source-line-858)；[命名空间与消费者](../architecture/configuration.md#source-line-858)。具体键按Schema固定，不自行填写默认值 |
| 产品验收定位 | [A52](../product/acceptance.md#a52)、[A53](../product/acceptance.md#a53)、[A54](../product/acceptance.md#a54)、[A55](../product/acceptance.md#a55)、[A56](../product/acceptance.md#a56)、[A57](../product/acceptance.md#a57)、[A58](../product/acceptance.md#a58)、[A59](../product/acceptance.md#a59)、[A88](../product/acceptance.md#a88)（任务范围扩大时按完整表补充） |
| 工程验收定位 | [V28](../architecture/acceptance.md#v28)、[V29](../architecture/acceptance.md#v29)（预期行为，未执行） |
| 尚未决定 | 去重字段合并、旧ID映射、时区、提醒路由和梦境跨期合并策略待批准。 [产品剩余契约](../product/decisions-and-delivery.md#section-22)；[工程冻结点](../architecture/implementation-options.md#source-line-1216)；[整理待确认项](../work/ORGANIZATION_REPORT.md#open-items) |

<a id="contract"></a>

## 完整模块原文阅读视图

<a id="source-line-405"></a>

### M10 多目标与意图

**拥有**：多个并行目标、来源、期限/提前量、状态、去重任务、合并别名、提醒计划和投递结果。

外部注入以短事务先建立目标，随后进行相似去重。简单的完全相同管理命令先用幂等键去重；语义重复再用本地候选和必要agent判断，避免每个目标和全库逐对比较。字段冲突尤其涉及不同对象、期限和场景时，不无条件合并。

目标至少区分未结束、完成、放弃；过期是未结束目标的时间条件。外部完成/放弃后取消未来提醒。到期向指定宿主提出放弃或修改截止时间的高层意图，不自动执行或延期。

提醒的计划必须持久化；投递仍是有限尽力，不构建业务无限重发。已发出但结果未知时记录未知，不将其当作目标完成。专注期暂停投递，醒来后建议合并为当前所需的一次过期提示而不是补发所有过时提醒；这是待批准的时序策略。

**建议端口**：`inject_goal`、`create_internal_goal`、`deduplicate_goal`、`update_goal_status`、`change_deadline`、`list_open_goals`、`dispatch_due_intent`。
