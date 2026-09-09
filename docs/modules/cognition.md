# 认知加工模块入口

> 本文件是权威原文的阅读视图，不是独立需求。原文仍是权威依据；后续修改规则时，先更新对应原文，再同步本视图与[覆盖映射](../work/ORGANIZATION_REPORT.md#coverage)。保留原文“已确定、建议、示例、待确认”的性质；下列导读不新增决策。正文含原有编号，仅限文档追踪。

适用主题与局部定义：认知加工提出候选变更；正式状态转换由记忆等数据所有者执行，Provider持有尝试账本。

来源：[原文 L343–L356](../../companion_memory_module_design_provider_logging_config.md#section-06)。行号对应整理时的哈希基线。

按关联工作联合阅读：[全部实现表达规范](../../CODING_STANDARDS.md)；[产品：入口队列、冻结批次、终结与学习](../product/batches-and-learning.md)；[产品：批次来源、正式记忆与关系](../product/provenance-and-memory.md)；[详细工程设计：内部上下文、批处理与成本控制](../architecture/context-and-cost.md)；[详细工程设计：Provider能力、调用控制、计量与安全](../architecture/provider.md)。

返回[文档总入口](../INDEX.md)；实际进度见[工作状态](../work/STATUS.md)。

## 按边界定位

下表是阅读路线；完整模块原文在本文件后半部，共享事务和详细设计仅通过链接引用。

| 关注点 | 阅读位置与边界 |
| --- | --- |
| 职责、非职责、数据所有权 | [模块所有权行](../architecture/ownership.md#m05)；[本模块完整契约](cognition.md#contract) |
| 公开能力与依赖 | 本模块正文的“建议端口”仍为草名；[统一依赖与启动边界](../architecture/ownership.md#source-line-267) |
| 状态、提交和恢复 | [接收承诺与允许损失](../architecture/persistence-and-transactions.md#section-04)；[T02](../architecture/persistence-and-transactions.md#t02)、[T03](../architecture/persistence-and-transactions.md#t03)、[T04](../architecture/persistence-and-transactions.md#t04)、[T05](../architecture/persistence-and-transactions.md#t05)、[T11](../architecture/persistence-and-transactions.md#t11)；[远程结果未知](../architecture/persistence-and-transactions.md#source-line-518)（仅涉及外部调用时） |
| 配置生效 | [统一快照及本模块相关参数](../architecture/configuration.md#source-line-878)；[命名空间与消费者](../architecture/configuration.md#source-line-858)。具体键按Schema固定，不自行填写默认值 |
| 产品验收定位 | [A08](../product/acceptance.md#a08)、[A21](../product/acceptance.md#a21)、[A24](../product/acceptance.md#a24)、[A25](../product/acceptance.md#a25)、[A71](../product/acceptance.md#a71)、[A72](../product/acceptance.md#a72)、[A78](../product/acceptance.md#a78)（任务范围扩大时按完整表补充） |
| 工程验收定位 | [V06](../architecture/acceptance.md#v06)、[V07](../architecture/acceptance.md#v07)、[V09](../architecture/acceptance.md#v09)、[V30](../architecture/acceptance.md#v30)、[V40](../architecture/acceptance.md#v40)（预期行为，未执行） |
| 尚未决定 | 分类Schema、工具预算、变更冲突和远程未知恢复决定待固定。 [产品剩余契约](../product/decisions-and-delivery.md#section-22)；[工程冻结点](../architecture/implementation-options.md#source-line-1216)；[整理待确认项](../work/ORGANIZATION_REPORT.md#open-items) |

<a id="contract"></a>

## 完整模块原文阅读视图

<a id="source-line-343"></a>

### M05 认知加工

**拥有**：学习运行、选择的上下文清单、工具调用步骤、候选变更集及对[M13](provider.md#contract)请求ID的引用；不拥有第二份model_attempts或计费账本。正式记忆仍由[M06](memory.md#contract)持有。

输入为冻结批次、当前已发布persona、有限相关记忆和策略配置。生成的变更集可以包含零条或多条事件、事实认知、推断、观点、关系建议或目标建议；每个直接学习结果必须带S2目标锚点，辅助引用分开记录。

采用“默认单次结构化加工 + 必要时受预算约束的工具循环”，不将高度自治简化成固定无思考流水线，也不默认每条消息跑多agent辩论。有限调用次数是工程预算，可在Web调整；不是禁止agent形成看法。

本地校验分类、分数范围、来源ID、目标锚点、引用对象状态、命令权限、输出长度和变更前修订。模型输出字段合法不等于结论正确，仍需来源检查和后续梦境修正。结构不合法时可以本地规范化；不能默认追加一次昂贵“请修复JSON”的LLM调用而不计预算。

候选通过[M06](memory.md#contract)的合法命令应用，不能直接执行模型生成SQL、文件路径或任意代码。所有模型请求经过[M13](provider.md#contract)和[M15](configuration.md#contract)配置的统一上下文预算，工具只暴露需要的记忆能力，不开放开发者审计。

**建议端口**：`process_batch`、`build_change_set`、`validate_change_set`、`propose_goal`、`request_focused_learning`。
