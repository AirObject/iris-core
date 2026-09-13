# 自我与persona模块入口

> 本文件是本主题的现行正文，在此唯一维护。既有要求、已批准契约、建议和待批准事项保持各自状态；迁移不新增产品决定或实现授权。文档关系见[总入口](../INDEX.md)。

适用主题与局部定义：自我认知本体在记忆模块；本模块负责其视图、persona候选及发布指针，当前情绪不属于稳定摘要。

设计／审核参考：[冻结原始文档](../reference/companion_memory_module_design_provider_logging_config.md)。仅供追溯，不作为现行约束。

按关联工作联合阅读：[全部实现表达规范](../CODING_STANDARDS.md)；[产品：自我认知与稳定persona](../product/self-and-persona.md)。

返回[文档总入口](../INDEX.md)；实际进度见[工作状态](../work/STATUS.md)。

[真实模型驱动的文本学习集中契约](../architecture/model-driven-text-learning.md)：六组推荐及明确例外已批准，含本模块必要端口增量；规则在集中正文唯一维护，实现与验证状态见CURRENT_TASK。

## 按边界定位

下表仅作导航；模块职责与端口正文在本文件后半部，共享事务和详细契约链接到各自唯一正文。

| 关注点 | 阅读位置与边界 |
| --- | --- |
| 职责、非职责、数据所有权 | [模块所有权行](../architecture/ownership.md#m07)；[模块职责与端口](self-model.md#contract) |
| 公开能力与依赖 | 本模块正文的“建议端口”仍为草名；[统一依赖与启动边界](../architecture/ownership.md#source-line-267) |
| 状态、提交和恢复 | [接收承诺与允许损失](../architecture/persistence-and-transactions.md#section-04)；[T09](../architecture/persistence-and-transactions.md#t09)；[远程结果未知](../architecture/persistence-and-transactions.md#source-line-518)（仅涉及外部调用时） |
| 配置生效 | [统一快照及本模块相关参数](../architecture/configuration.md#source-line-919)；[命名空间与消费者](../architecture/configuration.md#source-line-858)。具体键按Schema固定，不自行填写默认值 |
| 产品验收定位 | [A48](../product/acceptance.md#a48)、[A49](../product/acceptance.md#a49)、[A50](../product/acceptance.md#a50)、[A51](../product/acceptance.md#a51)、[A108](../product/acceptance.md#a108)（任务范围扩大时按完整表补充） |
| 工程验收定位 | [V26](../architecture/acceptance.md#v26)、[V80](../architecture/acceptance.md#v80)（预期行为，未执行） |
| 尚未决定 | 有限首次摘要整理见[已批准文本学习契约](../architecture/model-driven-text-learning.md)；常规梦境更新、变化幅度等范围外事项仍待固定。 [产品剩余契约](../product/decisions-and-delivery.md#section-22)；[工程冻结点](../architecture/implementation-options.md#source-line-1216)；[当前任务与阻塞](../work/CURRENT_TASK.md) |

<a id="contract"></a>

## 模块职责与端口

<a id="source-line-371"></a>

### M07 自我与persona

**拥有**：初始化自我输入的引用、persona候选、当前发布指针、生成版本/时间、监管配置版本和引用认知清单。自我认知本体仍在[M06](memory.md#contract)。

普通学习可改变自我认知，但不立即改写稳定persona。梦境调用本模块读取自我视图，按外部目标和监管prompt提炼候选，检查变化幅度并原子切换当前版本。候选失败保留旧版本并标明故障，不拼出半份新摘要。

可以把变化较少的自我视图预计算为只读投影，必须保留构建版本与依赖；它不是额外独立事实。短期情绪由[M09](state.md#contract)提供，不覆盖稳定摘要。历史persona正文按审计规则隔离，普通agent端只取当前发布与允许的候选。

**建议端口**：`read_self_view`、`get_published_persona`、`build_persona_candidate`、`validate_persona_candidate`、`publish_persona`。
