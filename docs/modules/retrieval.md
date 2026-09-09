# 检索与信息提供模块入口

> 本文件是权威原文的阅读视图，不是独立需求。原文仍是权威依据；后续修改规则时，先更新对应原文，再同步受影响的视图与相对链接；[覆盖映射](../work/ORGANIZATION_REPORT.md#coverage)仅作整理历史保留，不要求持续更新。保留原文“已确定、建议、示例、待确认”的性质；下列导读不新增决策。正文含原有编号，仅限文档追踪。

适用主题与局部定义：索引只提供候选；正式正文、修订与生命周期须回权威数据核验，召回不等于实际使用。

来源：[原文 L381–L394](../../companion_memory_module_design_provider_logging_config.md#section-06)。行号对应整理时的哈希基线。

按关联工作联合阅读：[全部实现表达规范](../../CODING_STANDARDS.md)；[产品：回复准备、记忆查询与入口隔离](../product/retrieval.md)；[产品：双指标、召回反馈、遗忘与删除](../product/lifecycle.md)；[详细工程设计：同步时限、数据所有权草图与外部接口](../architecture/request-paths.md)；[详细工程设计：内部上下文、批处理与成本控制](../architecture/context-and-cost.md)；[详细工程设计：Provider能力、调用控制、计量与安全](../architecture/provider.md)。

返回[文档总入口](../INDEX.md)；实际进度见[工作状态](../work/STATUS.md)。

## 按边界定位

下表是阅读路线；完整模块原文在本文件后半部，共享事务和详细设计仅通过链接引用。

| 关注点 | 阅读位置与边界 |
| --- | --- |
| 职责、非职责、数据所有权 | [模块所有权行](../architecture/ownership.md#m08)；[本模块完整契约](retrieval.md#contract) |
| 公开能力与依赖 | 本模块正文的“建议端口”仍为草名；[统一依赖与启动边界](../architecture/ownership.md#source-line-267) |
| 状态、提交和恢复 | [接收承诺与允许损失](../architecture/persistence-and-transactions.md#section-04)；[T07](../architecture/persistence-and-transactions.md#t07)；[远程结果未知](../architecture/persistence-and-transactions.md#source-line-518)（仅涉及外部调用时） |
| 配置生效 | [统一快照及本模块相关参数](../architecture/configuration.md#source-line-878)；[命名空间与消费者](../architecture/configuration.md#source-line-858)。具体键按Schema固定，不自行填写默认值 |
| 产品验收定位 | [A14](../product/acceptance.md#a14)、[A15](../product/acceptance.md#a15)、[A16](../product/acceptance.md#a16)、[A17](../product/acceptance.md#a17)、[A18](../product/acceptance.md#a18)、[A32](../product/acceptance.md#a32)、[A42](../product/acceptance.md#a42)、[A104](../product/acceptance.md#a104)（任务范围扩大时按完整表补充） |
| 工程验收定位 | [V16](../architecture/acceptance.md#v16)、[V17](../architecture/acceptance.md#v17)、[V18](../architecture/acceptance.md#v18)、[V19](../architecture/acceptance.md#v19)、[V20](../architecture/acceptance.md#v20)、[V21](../architecture/acceptance.md#v21)、[V22](../architecture/acceptance.md#v22)、[V23](../architecture/acceptance.md#v23)、[V33](../architecture/acceptance.md#v33)、[V78](../architecture/acceptance.md#v78)、[V84](../architecture/acceptance.md#v84)、[V90](../architecture/acceptance.md#v90)（预期行为，未执行） |
| 尚未决定 | 检索后端与中文方案、一秒语义降级策略、反馈凭据有效期和空间迁移覆盖条件待批准。 [产品剩余契约](../product/decisions-and-delivery.md#section-22)；[工程冻结点](../architecture/implementation-options.md#source-line-1216)；[整理待确认项](../work/ORGANIZATION_REPORT.md#open-items) |

<a id="contract"></a>

## 完整模块原文阅读视图

<a id="source-line-381"></a>

### M08 检索与信息提供

**拥有**：索引版本和覆盖水位、持久化embedding产物、向量/内容映射、查询向量缓存、召回记录、返回内容版本；向量生成与rerank请求及其账本统一由[M13](provider.md#contract)执行和拥有。正式记忆正文不以索引内容为权威。

回复准备由本地组合[M07](self-model.md#contract) persona、[M06](memory.md#contract)有效记忆、[M03](buffers.md#contract)本入口S2/S1、[M09](state.md#contract)状态及持续时间、[M10](goals.md#contract)目标，以及仅含数量/时间的其他入口待处理提示。普通查询和深度查询使用不同的生命周期过滤。都不提供其他入口正常或梦境缓存。

索引更新在记忆提交之后进行。新记忆先可按ID、结构化字段和本地词法路径使用；语义索引未完成时标记覆盖水位，不因embedding故障撤销正式记忆。对已遗忘/删除/改版ID进行查询末端核验，防止滞后索引越权返回旧正文。

在线排序采用可解释的确定性融合与有上限关联展开。可选rerank使用[M13](provider.md#contract)独立的能力端口，但不绕过统一Provider入口。返回前持久化召回凭据，以便重启后核对使用反馈；凭据只保存ID、版本、查询模式和必要元信息，不无期限复制完整响应。

只改变相信程度、保留强度或最近使用时间，不要求重新embedding；语义正文、embedding模型/维度或文本预处理变化才使相应向量失效。不同embedding空间不能混在同一索引中直接比较。密钥轮换或仅超时变更不改变向量空间；空间由实际provider/deployment、模型标识及已知版本、维度、预处理/规范化语义等组成，不把任意profile修订都当作重新向量化的原因。

**建议端口**：`prepare_reply`、`search_memory`、`deep_recall`、`record_recall`、`resolve_usage_ticket`、`update_index`、`rebuild_local_index`。
