# Web与管理入口模块入口

> 本文件是本主题的现行正文，在此唯一维护。既有要求、已批准契约、建议和待批准事项保持各自状态；迁移不新增产品决定或实现授权。文档关系见[总入口](../INDEX.md)。

适用主题与局部定义：Web负责会话、权限和用例展示；配置、Provider计量、日志各有唯一所有者。只读观察不授予业务调用权限。

设计／审核参考：[冻结原始文档](../reference/companion_memory_module_design_provider_logging_config.md)。仅供追溯，不作为现行约束。

按关联工作联合阅读：[全部实现表达规范](../CODING_STANDARDS.md)；[产品：故障、审计与Web管理行为](../product/operations-and-management.md)。

返回[文档总入口](../INDEX.md)；实际进度见[工作状态](../work/STATUS.md)。

[文本学习集中契约](../architecture/model-driven-text-learning.md)：六组推荐及明确例外已批准；本模块所涉细化在该正文唯一维护，实现与阻塞见CURRENT_TASK。

## 按边界定位

本次最小只读状态及日志观察见[持久接入与批次运行契约](../architecture/durable-ingress-and-batch-runtime.md#observation)（五组推荐已批准，实施已授权；验证状态见CURRENT_TASK）；本模块原职责和既有批准状态不变。

下表仅作导航；模块职责与端口正文在本文件后半部，共享事务和详细契约链接到各自唯一正文。

| 关注点 | 阅读位置与边界 |
| --- | --- |
| 职责、非职责、数据所有权 | [模块所有权行](../architecture/ownership.md#m12)；[模块职责与端口](management.md#contract) |
| 公开能力与依赖 | 本模块正文的“建议端口”仍为草名；[统一依赖与启动边界](../architecture/ownership.md#source-line-267) |
| 状态、提交和恢复 | [接收承诺与允许损失](../architecture/persistence-and-transactions.md#section-04)；[T08](../architecture/persistence-and-transactions.md#t08)、[T12](../architecture/persistence-and-transactions.md#t12)、[T13](../architecture/persistence-and-transactions.md#t13)；[远程结果未知](../architecture/persistence-and-transactions.md#source-line-518)（仅涉及外部调用时） |
| 配置生效 | [统一快照及本模块相关参数](../architecture/configuration.md#source-line-878)；[命名空间与消费者](../architecture/configuration.md#source-line-858)。具体键按Schema固定，不自行填写默认值 |
| 产品验收定位 | [A66](../product/acceptance.md#a66)、[A67](../product/acceptance.md#a67)、[A68](../product/acceptance.md#a68)、[A93](../product/acceptance.md#a93)（任务范围扩大时按完整表补充） |
| 工程验收定位 | [V24](../architecture/acceptance.md#v24)、[V35](../architecture/acceptance.md#v35)、[V36](../architecture/acceptance.md#v36)、[V38](../architecture/acceptance.md#v38)、[V54](../architecture/acceptance.md#v54)、[V61](../architecture/acceptance.md#v61)、[V68](../architecture/acceptance.md#v68)、[V69](../architecture/acceptance.md#v69)、[V76](../architecture/acceptance.md#v76)、[V81](../architecture/acceptance.md#v81)、[V89](../architecture/acceptance.md#v89)（预期行为，未执行） |
| 尚未决定 | 账号与权限细则、人工管理确认流程、备份恢复、完整来源与审计正文访问范围待固定。 [产品剩余契约](../product/decisions-and-delivery.md#section-22)；[工程冻结点](../architecture/implementation-options.md#source-line-1216)；[当前任务与阻塞](../work/CURRENT_TASK.md) |

<a id="contract"></a>

## 模块职责与端口

<a id="source-line-429"></a>

### M12 Web与管理入口

**拥有**：初始化向导的流程状态、管理会话、RBAC授权、页面视图组合及管理操作回执。配置实体属于[M15](configuration.md#contract)，模型请求和费用属于[M13](provider.md#contract)，日志与审计属于[M14](logging.md#contract)；Web不直接改这些表或扫描任意文件。

初始化用图形说明自我/稳定persona/当前状态/目标和S3/S2/S1。提供Provider配置及测试入口、按能力/角色的用量统计、运行日志实时与历史视图、审计视图、配置Schema表单与变更预览。

权限至少区分只读运行观察、Provider用量查询、敏感审计读取、配置编辑、密钥更新和备份恢复。账号体系复杂度可从单管理员开始，但权限端口和数据脱敏不能省略。专注梦境中只开放既定的只读运行观察：脱敏运行日志、已持久化Provider统计、配置生效状态均可读，不因此开放完整原始请求或记忆审计正文；配置写入、连接测试和模型试调用仍被门控拒绝。

**建议端口**：`initialize_role`、`read_runtime_dashboard`、`read_provider_dashboard`、`read_log_view`、`read_config_view`、`submit_management_command`、`create_backup`、`verify_restore`。
