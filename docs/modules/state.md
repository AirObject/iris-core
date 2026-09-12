# 当前状态模块入口

> 本文件是本主题的现行正文，在此唯一维护。既有要求、已批准契约、建议和待批准事项保持各自状态；迁移不新增产品决定或实现授权。文档关系见[总入口](../INDEX.md)。

适用主题与局部定义：当前状态保存外部报告的现在；活动开始时间与更新时间不同，系统不能从历史消息自行恢复当前值。

设计／审核参考：[冻结原始文档](../reference/companion_memory_module_design_provider_logging_config.md)。仅供追溯，不作为现行约束。

按关联工作联合阅读：[全部实现表达规范](../CODING_STANDARDS.md)；[产品：外部管理的当前状态与时间](../product/current-state.md)。

返回[文档总入口](../INDEX.md)；实际进度见[工作状态](../work/STATUS.md)。

已批准的本地信息闭环及具体端口见[集中契约](../architecture/local-information-feedback.md)，参数见[配置补充](../architecture/configuration.md#local-information-configuration-draft)；其余远程增强与未采纳替代保持原批准状态。

## 按边界定位

下表仅作导航；模块职责与端口正文在本文件后半部，共享事务和详细契约链接到各自唯一正文。

| 关注点 | 阅读位置与边界 |
| --- | --- |
| 职责、非职责、数据所有权 | [模块所有权行](../architecture/ownership.md#m09)；[模块职责与端口](state.md#contract) |
| 公开能力与依赖 | 本地接口见[已批准集中契约](../architecture/local-information-feedback.md#interfaces)，其余“建议端口”仍为草名；[统一依赖与启动边界](../architecture/ownership.md#source-line-267) |
| 状态、提交和恢复 | [接收承诺与允许损失](../architecture/persistence-and-transactions.md#section-04)；[T08](../architecture/persistence-and-transactions.md#t08)；[远程结果未知](../architecture/persistence-and-transactions.md#source-line-518)（仅涉及外部调用时） |
| 配置生效 | [统一快照及本模块相关参数](../architecture/configuration.md#source-line-858)；[命名空间与消费者](../architecture/configuration.md#source-line-858)。具体键按Schema固定，不自行填写默认值 |
| 产品验收定位 | [A44](../product/acceptance.md#a44)、[A45](../product/acceptance.md#a45)、[A46](../product/acceptance.md#a46)、[A47](../product/acceptance.md#a47)、[A105](../product/acceptance.md#a105)（任务范围扩大时按完整表补充） |
| 工程验收定位 | [V27](../architecture/acceptance.md#v27)（预期行为，未执行） |
| 批准状态与范围外 | 本地闭环的已批准选项以[集中契约](../architecture/local-information-feedback.md)为准；未采纳替代及包外能力见[产品剩余契约](../product/decisions-and-delivery.md#section-22)和[工程冻结点](../architecture/implementation-options.md#source-line-1216)。实际实现／验证缺口见[当前任务](../work/CURRENT_TASK.md) |

<a id="contract"></a>

## 模块职责与端口

<a id="source-line-395"></a>

### M09 当前状态

**拥有**：外部状态包、活动ID、活动开始时间、各子状态更新时间、最后报告者及入口、版本。

仅外部管理接口有权设置/结束。普通输入描述过往活动可以成为未来历史记忆，但不直接覆盖当前值。回复准备计算截至观察时刻的持续时间；缺少实际起点时明确以首次报告为准或未知，不从重启时间重新计时。

专注期拒绝状态命令，不排队自动执行。醒来后保留持久化的最后状态及陈旧信息；外部可发最新状态。状态的各维度共用一个包或细分结构由schema决定，不把直播、游戏细节和情绪拆成独立人格。

**建议端口**：`set_state`、`update_state`、`end_activity`、`get_state_view`。
