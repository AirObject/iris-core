# 检索与信息提供模块入口

> 本文件是本主题的现行正文，在此唯一维护。既有要求、已批准契约、建议和待批准事项保持各自状态；迁移不新增产品决定或实现授权。文档关系见[总入口](../INDEX.md)。

适用主题与局部定义：索引只提供候选；正式正文、修订与生命周期须回权威数据核验，召回不等于实际使用。

设计／审核参考：[冻结原始文档](../reference/companion_memory_module_design_provider_logging_config.md)。仅供追溯，不作为现行约束。

按关联工作联合阅读：[全部实现表达规范](../CODING_STANDARDS.md)；[产品：回复准备、记忆查询与入口隔离](../product/retrieval.md)；[产品：双指标、召回反馈、遗忘与删除](../product/lifecycle.md)；[详细工程设计：同步时限、数据所有权草图与外部接口](../architecture/request-paths.md)；[详细工程设计：内部上下文、批处理与成本控制](../architecture/context-and-cost.md)；[详细工程设计：Provider能力、调用控制、计量与安全](../architecture/provider.md)。

返回[文档总入口](../INDEX.md)；实际进度见[工作状态](../work/STATUS.md)。

已批准的本地信息闭环及具体端口见[集中契约](../architecture/local-information-feedback.md)，参数见[配置补充](../architecture/configuration.md#local-information-configuration-draft)；其余远程增强与未采纳替代保持原批准状态。

## 按边界定位

下表仅作导航；模块职责与端口正文在本文件后半部，共享事务和详细契约链接到各自唯一正文。

| 关注点 | 阅读位置与边界 |
| --- | --- |
| 职责、非职责、数据所有权 | [模块所有权行](../architecture/ownership.md#m08)；[模块职责与端口](retrieval.md#contract) |
| 公开能力与依赖 | 本地接口见[已批准集中契约](../architecture/local-information-feedback.md#interfaces)，其余“建议端口”仍为草名；[统一依赖与启动边界](../architecture/ownership.md#source-line-267) |
| 状态、提交和恢复 | [接收承诺与允许损失](../architecture/persistence-and-transactions.md#section-04)；[T07](../architecture/persistence-and-transactions.md#t07)；[远程结果未知](../architecture/persistence-and-transactions.md#source-line-518)（仅涉及外部调用时） |
| 配置生效 | [统一快照及本模块相关参数](../architecture/configuration.md#source-line-878)；[命名空间与消费者](../architecture/configuration.md#source-line-858)。具体键按Schema固定，不自行填写默认值 |
| 产品验收定位 | [A14](../product/acceptance.md#a14)、[A15](../product/acceptance.md#a15)、[A16](../product/acceptance.md#a16)、[A17](../product/acceptance.md#a17)、[A18](../product/acceptance.md#a18)、[A32](../product/acceptance.md#a32)、[A42](../product/acceptance.md#a42)、[A104](../product/acceptance.md#a104)（任务范围扩大时按完整表补充） |
| 工程验收定位 | [V16](../architecture/acceptance.md#v16)、[V17](../architecture/acceptance.md#v17)、[V18](../architecture/acceptance.md#v18)、[V19](../architecture/acceptance.md#v19)、[V20](../architecture/acceptance.md#v20)、[V21](../architecture/acceptance.md#v21)、[V22](../architecture/acceptance.md#v22)、[V23](../architecture/acceptance.md#v23)、[V33](../architecture/acceptance.md#v33)、[V78](../architecture/acceptance.md#v78)、[V84](../architecture/acceptance.md#v84)、[V90](../architecture/acceptance.md#v90)（验收要求；实际覆盖见[STATUS](../work/STATUS.md)） |
| 批准状态与范围外 | 本地闭环的已批准选项以[集中契约](../architecture/local-information-feedback.md)为准；未采纳替代及包外能力见[产品剩余契约](../product/decisions-and-delivery.md#section-22)和[工程冻结点](../architecture/implementation-options.md#source-line-1216)。实际实现／验证缺口见[当前任务](../work/CURRENT_TASK.md) |

<a id="contract"></a>

## 模块职责与端口

<a id="source-line-381"></a>

### M08 检索与信息提供

**拥有**：索引版本和覆盖水位、持久化embedding产物、向量/内容映射、查询向量缓存、召回记录、返回内容版本；向量生成与rerank请求及其账本统一由[M13](provider.md#contract)执行和拥有。正式记忆正文不以索引内容为权威。

回复准备由本地组合[M07](self-model.md#contract) persona、[M06](memory.md#contract)有效记忆、[M03](buffers.md#contract)本入口S2/S1、[M09](state.md#contract)状态及持续时间、[M10](goals.md#contract)目标，以及仅含数量/时间的其他入口待处理提示。普通查询和深度查询使用不同的生命周期过滤。都不提供其他入口正常或梦境缓存。

索引更新在记忆提交之后进行。新记忆先可按ID、结构化字段和本地词法路径使用；语义索引未完成时标记覆盖水位，不因embedding故障撤销正式记忆。对已遗忘/删除/改版ID进行查询末端核验，防止滞后索引越权返回旧正文。

在线排序采用可解释的确定性融合与有上限关联展开。可选rerank使用[M13](provider.md#contract)独立的能力端口，但不绕过统一Provider入口。返回前持久化召回凭据，以便重启后核对使用反馈；凭据只保存ID、版本、查询模式和必要元信息，不无期限复制完整响应。

只改变相信程度、保留强度或最近使用时间，不要求重新embedding；语义正文、embedding模型/维度或文本预处理变化才使相应向量失效。不同embedding空间不能混在同一索引中直接比较。密钥轮换或仅超时变更不改变向量空间；空间由实际provider/deployment、模型标识及已知版本、维度、预处理/规范化语义等组成，不把任意profile修订都当作重新向量化的原因。

**建议端口**：`prepare_reply`、`search_memory`、`deep_recall`、`record_recall`、`resolve_usage_ticket`、`update_index`、`rebuild_local_index`。
