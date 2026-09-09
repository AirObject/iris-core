# 模型Provider模块入口

> 本文件是本主题的现行正文，在此唯一维护。既有要求、已批准契约、建议和待批准事项保持各自状态；迁移不新增产品决定或实现授权。文档关系见[总入口](../INDEX.md)。

适用主题与局部定义：逻辑请求与网络attempt分开。Provider拥有准入、尝试、usage、费用和预算执行状态；profile与价格配置真相属于配置模块。

设计／审核参考：[冻结原始文档](../reference/companion_memory_module_design_provider_logging_config.md)。仅供追溯，不作为现行约束。

按关联工作联合阅读：[全部实现表达规范](../CODING_STANDARDS.md)；[详细工程设计：Provider能力、调用控制、计量与安全](../architecture/provider.md)；[详细工程设计：内部上下文、批处理与成本控制](../architecture/context-and-cost.md)。

返回[文档总入口](../INDEX.md)；实际进度见[工作状态](../work/STATUS.md)。

## 按边界定位

下表仅作导航；模块职责与端口正文在本文件后半部，共享事务和详细契约链接到各自唯一正文。

| 关注点 | 阅读位置与边界 |
| --- | --- |
| 职责、非职责、数据所有权 | [模块所有权行](../architecture/ownership.md#m13)；[模块职责与端口](provider.md#contract) |
| 公开能力与依赖 | 本模块正文的“建议端口”仍为草名；[统一依赖与启动边界](../architecture/ownership.md#source-line-267) |
| 状态、提交和恢复 | [接收承诺与允许损失](../architecture/persistence-and-transactions.md#section-04)；[T10](../architecture/persistence-and-transactions.md#t10)、[T11](../architecture/persistence-and-transactions.md#t11)、[T13](../architecture/persistence-and-transactions.md#t13)；[远程结果未知](../architecture/persistence-and-transactions.md#source-line-518)（仅涉及外部调用时） |
| 配置生效 | [统一快照及本模块相关参数](../architecture/configuration.md#source-line-878)；[命名空间与消费者](../architecture/configuration.md#source-line-858)。具体键按Schema固定，不自行填写默认值 |
| 产品验收定位 | [A09](../product/acceptance.md#a09)、[A10](../product/acceptance.md#a10)、[A60](../product/acceptance.md#a60)、[A94](../product/acceptance.md#a94)（任务范围扩大时按完整表补充） |
| 工程验收定位 | [V09](../architecture/acceptance.md#v09)、[V31](../architecture/acceptance.md#v31)、[V32](../architecture/acceptance.md#v32)、[V37](../architecture/acceptance.md#v37)、[V38](../architecture/acceptance.md#v38)、[V39](../architecture/acceptance.md#v39)、[V40](../architecture/acceptance.md#v40)、[V41](../architecture/acceptance.md#v41)、[V42](../architecture/acceptance.md#v42)、[V43](../architecture/acceptance.md#v43)、[V44](../architecture/acceptance.md#v44)、[V45](../architecture/acceptance.md#v45)、[V46](../architecture/acceptance.md#v46)、[V47](../architecture/acceptance.md#v47)、[V48](../architecture/acceptance.md#v48)、[V49](../architecture/acceptance.md#v49)、[V50](../architecture/acceptance.md#v50)、[V51](../architecture/acceptance.md#v51)、[V52](../architecture/acceptance.md#v52)、[V53](../architecture/acceptance.md#v53)、[V55](../architecture/acceptance.md#v55)、[V56](../architecture/acceptance.md#v56)、[V85](../architecture/acceptance.md#v85)、[V88](../architecture/acceptance.md#v88)（预期行为，未执行） |
| 尚未决定 | 协议草名、运行语言/SDK、价格和配额默认值、未知结果管理恢复与保留期均待固定。 [产品剩余契约](../product/decisions-and-delivery.md#section-22)；[工程冻结点](../architecture/implementation-options.md#source-line-1216)；[当前任务与阻塞](../work/CURRENT_TASK.md) |

<a id="contract"></a>

## 模块职责与端口

<a id="source-line-439"></a>

### M13 模型Provider

**拥有**：协议/能力适配器、按配置版本构建的客户端池、路由与准入执行、有限远程尝试、账户与角色限流、预算预留、逻辑请求和尝试账本、规范化usage、费用明细、统计聚合及健康状态。模型profile、价格表和限额的配置真相属于[M15](configuration.md#contract)，[M13](provider.md#contract)只持有已批准快照和执行状态。

所有LLM、embedding、rerank、多媒体模型请求，以及模型列表/能力测试/远程token计数等诊断调用均从此模块发出；试调用也计量，不因“来自Web”而免费或不可见。外部宿主自身的模型调用不在本服务控制范围内；这里的“所有”指本系统及受支持内嵌扩展发起的调用。

[M13](provider.md#contract)只接收规范化能力请求，不接管哪些记忆应生成、怎样遗忘、怎样轮转。模型返回结构、错误及usage在此统一，再由对应业务模块决定学习终态或局部媒体占位。普通超时、敏感拒绝、预算暂停、专注拒绝、配置错误、取消和远程结果未知分别表达。

统计使用独立的持久化账本，而不是解析运行日志。日志等级变成ERROR、文件轮转或Web断开，都不影响已知调用与费用计量。已付费业务结果由[M04](media.md#contract)/[M05](cognition.md#contract)/[M08](retrieval.md#contract)等持有并引用`provider_request_id`；Provider计量不因此重复保存一份可检索的记忆正文。

**建议端口**：`generate`、`embed`、`rerank`、`understand_media`、`inspect_capabilities`、`probe_profile`、`report_local_reuse`、`get_request`、`query_usage`、`get_budget_state`、`prepare_profile_revision`。完整请求与统计契约见[第8节](../architecture/provider.md#section-08)。
