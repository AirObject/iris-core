# 统一配置模块入口

> 本文件是本主题的现行正文，在此唯一维护。既有要求、已批准契约、建议和待批准事项保持各自状态；迁移不新增产品决定或实现授权。文档关系见[总入口](../INDEX.md)。

适用主题与局部定义：注册表定义参数元信息；版本、不可变快照与生效计划不同。当前状态、目标、已使用预算属于业务对象。

设计／审核参考：[冻结原始文档](../reference/companion_memory_module_design_provider_logging_config.md)。仅供追溯，不作为现行约束。

按关联工作联合阅读：[全部实现表达规范](../CODING_STANDARDS.md)；[产品：待细化契约、参数性质与实施组织](../product/decisions-and-delivery.md)；[详细工程设计：统一配置注册表、快照与安全热修改](../architecture/configuration.md)。

返回[文档总入口](../INDEX.md)；实际进度见[工作状态](../work/STATUS.md)。

## 按边界定位

下表仅作导航；模块职责与端口正文在本文件后半部，共享事务和详细契约链接到各自唯一正文。

| 关注点 | 阅读位置与边界 |
| --- | --- |
| 职责、非职责、数据所有权 | [模块所有权行](../architecture/ownership.md#m15)；[模块职责与端口](configuration.md#contract) |
| 参数定义与只读注册表契约 | [当前有效契约](../architecture/configuration.md#configuration-registry-contract)：[依据分类](../architecture/configuration.md#configuration-registry-evidence)、[数据定义](../architecture/configuration.md#configuration-registry-data)、[注册与冻结](../architecture/configuration.md#configuration-registry-lifecycle)、[公开接口](../architecture/configuration.md#configuration-registry-ports)、[错误](../architecture/configuration.md#configuration-registry-errors)、[验收例子](../architecture/configuration.md#configuration-registry-examples)；[契约已批准](../architecture/configuration.md#configuration-registry-decisions)；实现及验收进度见[STATUS](../work/STATUS.md) |
| 公开能力与依赖 | 本模块正文的“建议端口”仍为草名；[统一依赖与启动边界](../architecture/ownership.md#source-line-267) |
| 状态、提交和恢复 | [接收承诺与允许损失](../architecture/persistence-and-transactions.md#section-04)；[T12](../architecture/persistence-and-transactions.md#t12)、[T13](../architecture/persistence-and-transactions.md#t13)；[远程结果未知](../architecture/persistence-and-transactions.md#source-line-518)（仅涉及外部调用时） |
| 配置生效 | [统一快照及本模块相关参数](../architecture/configuration.md#source-line-837)；[命名空间与消费者](../architecture/configuration.md#source-line-858)。具体键按Schema固定，不自行填写默认值 |
| 产品验收定位 | [A20](../product/acceptance.md#a20)、[A67](../product/acceptance.md#a67)（任务范围扩大时按完整表补充） |
| 工程验收定位 | [V73](../architecture/acceptance.md#v73)、[V74](../architecture/acceptance.md#v74)、[V75](../architecture/acceptance.md#v75)、[V76](../architecture/acceptance.md#v76)、[V77](../architecture/acceptance.md#v77)、[V78](../architecture/acceptance.md#v78)、[V79](../architecture/acceptance.md#v79)、[V80](../architecture/acceptance.md#v80)、[V81](../architecture/acceptance.md#v81)、[V82](../architecture/acceptance.md#v82)、[V83](../architecture/acceptance.md#v83)、[V84](../architecture/acceptance.md#v84)、[V85](../architecture/acceptance.md#v85)、[V86](../architecture/acceptance.md#v86)、[V87](../architecture/acceptance.md#v87)、[V88](../architecture/acceptance.md#v88)、[V89](../architecture/acceptance.md#v89)、[V90](../architecture/acceptance.md#v90)（预期行为，未执行） |
| 尚未决定 | 具体Schema/default/apply_mode、bootstrap、秘密提供器、持久化后端及迁移/激活参与者方案待批准。 [产品剩余契约](../product/decisions-and-delivery.md#section-22)；[工程冻结点](../architecture/implementation-options.md#source-line-1216)；[当前任务与阻塞](../work/CURRENT_TASK.md) |

<a id="contract"></a>

## 模块职责与端口

<a id="source-line-461"></a>

### M15 统一配置

**拥有**：配置Schema/注册表、唯一默认值来源、作用域与继承、类型/范围/跨字段校验、秘密引用、版本与有效快照、变更审核/生效计划、订阅通知、回退记录和管理可解释性。

所有模块只获取本模块需要的不可变类型化配置；禁止模块自行读取环境变量、另开YAML或散落`get(key, 魔法默认值)`。实例、平台、任务角色、模型profile及日志sink等作用域由Schema明确支持，不任意传播未知覆盖。当前状态、目标、记忆和预算已使用量是业务状态，不放进配置表。

正常运行支持在线修改；是否即时作用于下一条事件、下次请求、下个批次或下次梦境，由参数元数据固定。数据库路径、监听端口等需重启，embedding空间变化需迁移。已冻结任务继续使用固定快照；不存在“热修改=随时覆盖所有任务当前值”。专注期配置写入仍拒绝，不排队代执行。

**建议端口**：`get_schema`、`get_effective_snapshot`、`validate_patch`、`preview_change`、`submit_change`、`activate_revision`、`subscribe_changes`、`rollback_revision`、`get_activation_status`、`export_redacted`。完整热修改契约见[第11节](../architecture/configuration.md#section-11)。
