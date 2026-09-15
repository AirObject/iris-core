# 日志与审计模块入口

> 本文件是本主题的现行正文，在此唯一维护。既有要求、已批准契约、建议和待批准事项保持各自状态；迁移不新增产品决定或实现授权。文档关系见[总入口](../INDEX.md)。

适用主题与局部定义：sink是输出端；诊断按等级路由，必须的审计同事务保存，Provider账本由Provider拥有。

设计／审核参考：[冻结原始文档](../reference/companion_memory_module_design_provider_logging_config.md)。仅供追溯，不作为现行约束。

按关联工作联合阅读：[全部实现表达规范](../CODING_STANDARDS.md)；[产品：故障、审计与Web管理行为](../product/operations-and-management.md)；[详细工程设计：日志等级、输出、审计隔离与背压](../architecture/logging.md)。

返回[文档总入口](../INDEX.md)；实际进度见[工作状态](../work/STATUS.md)。

## 按边界定位

下表仅作导航；模块职责与端口正文在本文件后半部，共享事务和详细契约链接到各自唯一正文。

| 关注点 | 阅读位置与边界 |
| --- | --- |
| 职责、非职责、数据所有权 | [模块所有权行](../architecture/ownership.md#m14)；[模块职责与端口](logging.md#contract) |
| 公开能力与依赖 | 运行诊断、审计与观察的公开端口见[详细契约](../architecture/logging.md)，其余“建议端口”保留完整模块方向；[统一依赖与启动边界](../architecture/ownership.md#source-line-267) |
| 状态、提交和恢复 | [接收承诺与允许损失](../architecture/persistence-and-transactions.md#section-04)；[T13](../architecture/persistence-and-transactions.md#t13)；[远程结果未知](../architecture/persistence-and-transactions.md#source-line-518)（仅涉及外部调用时） |
| 配置生效 | [统一快照及本模块相关参数](../architecture/configuration.md#source-line-895)；[命名空间与消费者](../architecture/configuration.md#source-line-858)。具体键按Schema固定，不自行填写默认值 |
| 产品验收定位 | [A64](../product/acceptance.md#a64)、[A66](../product/acceptance.md#a66)、[A93](../product/acceptance.md#a93)（任务范围扩大时按完整表补充） |
| 工程验收定位 | [V52](../architecture/acceptance.md#v52)、[V57](../architecture/acceptance.md#v57)、[V58](../architecture/acceptance.md#v58)、[V59](../architecture/acceptance.md#v59)、[V60](../architecture/acceptance.md#v60)、[V61](../architecture/acceptance.md#v61)、[V62](../architecture/acceptance.md#v62)、[V63](../architecture/acceptance.md#v63)、[V64](../architecture/acceptance.md#v64)、[V65](../architecture/acceptance.md#v65)、[V66](../architecture/acceptance.md#v66)、[V67](../architecture/acceptance.md#v67)、[V68](../architecture/acceptance.md#v68)、[V69](../architecture/acceptance.md#v69)、[V70](../architecture/acceptance.md#v70)、[V71](../architecture/acceptance.md#v71)、[V72](../architecture/acceptance.md#v72)（验收要求；实际覆盖见[STATUS](../work/STATUS.md)） |
| 尚未决定 | 运行诊断、队列／保留参数、事务审计和受限Web观察见[已批准日志契约](../architecture/logging.md)；生产G2、完整管理权限及历史保留政策仍待细化。 [产品剩余契约](../product/decisions-and-delivery.md#section-22)；[工程冻结点](../architecture/implementation-options.md#source-line-1216)；[当前任务与阻塞](../work/CURRENT_TASK.md) |

<a id="contract"></a>

## 模块职责与端口

<a id="source-line-451"></a>

### M14 日志与审计

**拥有**：统一结构化运行日志模型、logger注册、过滤与脱敏规则的执行、控制台/文件/Web输出、分段与索引、订阅游标、审计事件及历史正文隔离、输出健康和丢弃计数。日志参数和保留规则由[M15](configuration.md#contract)定义；[M14](logging.md#contract)不另有可绕过配置模块的热修改接口。

运行日志按标准等级处理；业务审计是独立记录类别，不是自创的更高日志等级。重要认知、生命周期、配置等审计加入同一Unit of Work，与业务状态一起提交。异步输出失败不撤销已提交业务，但要求记录故障；审计落库失败时不能假装受审计写命令已成功。

Provider的用量账本属于[M13](provider.md#contract)。[M14](logging.md#contract)可以记录同一调用的可读摘要与关联ID，但不能成为计费权威，也不能把ERROR过滤当作“无需记录成功调用费用”。所有日志渠道均不注入记忆、persona、embedding或agent工具。

**建议端口**：`get_logger`、`emit`、`append_audit(uow, event)`、`query_runtime_logs`、`subscribe_runtime_logs`、`read_audit`、`export_logs`、`get_sink_health`、`prepare_logging_revision`。输出、权限和等级契约见[第10节](../architecture/logging.md#section-10)。
