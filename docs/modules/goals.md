# 多目标与意图模块入口

本模块获准的保守目标语义合并、原ID／来源及提醒竞争，见[集中契约](../architecture/daily-cognition-and-image-learning.md#5-目标语义判断与合并)。

> 本文件是本主题的现行正文，在此唯一维护。既有要求、已批准契约、建议和待批准事项保持各自状态；迁移不新增产品决定或实现授权。文档关系见[总入口](../INDEX.md)。

适用主题与局部定义：目标是未来意图；过期不等于完成或放弃。目标提醒计划持久化，投递仍然有限尽力。

设计／审核参考：[冻结原始文档](../reference/companion_memory_module_design_provider_logging_config.md)。仅供追溯，不作为现行约束。

按关联工作联合阅读：[全部实现表达规范](../CODING_STANDARDS.md)；[产品：多目标、去重与提醒](../product/goals.md)。

返回[文档总入口](../INDEX.md)；实际进度见[工作状态](../work/STATUS.md)。

已批准的本地信息闭环及具体端口见[集中契约](../architecture/local-information-feedback.md)，参数见[配置补充](../architecture/configuration.md#local-information-configuration-draft)；其余远程增强与未采纳替代保持原批准状态。

## 按边界定位

下表仅作导航；模块职责与端口正文在本文件后半部，共享事务和详细契约链接到各自唯一正文。

| 关注点 | 阅读位置与边界 |
| --- | --- |
| 职责、非职责、数据所有权 | [模块所有权行](../architecture/ownership.md#m10)；[模块职责与端口](goals.md#contract) |
| 公开能力与依赖 | 本地接口见[已批准集中契约](../architecture/local-information-feedback.md#interfaces)，其余“建议端口”仍为草名；[统一依赖与启动边界](../architecture/ownership.md#source-line-267) |
| 状态、提交和恢复 | [接收承诺与允许损失](../architecture/persistence-and-transactions.md#section-04)；[T08](../architecture/persistence-and-transactions.md#t08)；[远程结果未知](../architecture/persistence-and-transactions.md#source-line-518)（仅涉及外部调用时） |
| 配置生效 | [统一快照及本模块相关参数](../architecture/configuration.md#source-line-858)；[命名空间与消费者](../architecture/configuration.md#source-line-858)。具体键按Schema固定，不自行填写默认值 |
| 产品验收定位 | [A52](../product/acceptance.md#a52)、[A53](../product/acceptance.md#a53)、[A54](../product/acceptance.md#a54)、[A55](../product/acceptance.md#a55)、[A56](../product/acceptance.md#a56)、[A57](../product/acceptance.md#a57)、[A58](../product/acceptance.md#a58)、[A59](../product/acceptance.md#a59)、[A88](../product/acceptance.md#a88)（任务范围扩大时按完整表补充） |
| 工程验收定位 | [V28](../architecture/acceptance.md#v28)、[V29](../architecture/acceptance.md#v29)（验收要求；实际覆盖见[STATUS](../work/STATUS.md)） |
| 批准状态与范围外 | 本地闭环的已批准选项以[集中契约](../architecture/local-information-feedback.md)为准；未采纳替代及包外能力见[产品剩余契约](../product/decisions-and-delivery.md#section-22)和[工程冻结点](../architecture/implementation-options.md#source-line-1216)。实际实现／验证缺口见[当前任务](../work/CURRENT_TASK.md) |

<a id="contract"></a>

## 模块职责与端口

<a id="source-line-405"></a>

### M10 多目标与意图

**拥有**：多个并行目标、来源、期限/提前量、状态、去重任务、合并别名、提醒计划、宽限计时和投递结果。

外部注入以短事务先建立目标，随后进行相似去重。简单的完全相同管理命令先用幂等键去重；语义重复再用本地候选和必要agent判断，避免每个目标和全库逐对比较。字段冲突尤其涉及不同对象、期限和场景时，不无条件合并。

目标至少区分未结束、完成、放弃；过期是未结束目标的时间条件。外部完成/放弃后取消未来提醒。到期向指定宿主提出放弃或修改截止时间的高层意图，不自动执行或延期。

提醒的计划必须持久化；投递仍是有限尽力，不构建业务无限重发。已发出但结果未知时记录未知，不将其当作目标完成。专注期暂停投递；本地包的跨期处理见[已批准提醒时序](../architecture/local-information-feedback.md#state-goals)。

**建议端口**：`inject_goal`、`create_internal_goal`、`deduplicate_goal`、`update_goal_status`、`change_deadline`、`list_open_goals`、`dispatch_due_intent`。

生产WS的路由贯通、有限发送、ACK与UNKNOWN、计时／暂停／恢复唯一见[已批准提醒增量](../architecture/local-information-feedback.md#ws-reminders)。goals只消费受控路由能力、门控转换及传输结果；连接协议、按路由接管和在线会话归[通信适配](../architecture/external-communication.md#ws)，不得另存第二份目标或发送真相。
