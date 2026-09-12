# 记忆、来源与关系模块入口

> 本文件是本主题的现行正文，在此唯一维护。既有要求、已批准契约、建议和待批准事项保持各自状态；迁移不新增产品决定或实现授权。文档关系见[总入口](../INDEX.md)。

适用主题与局部定义：相信程度评价命题，保留强度驱动生命周期；自我事实也属于本模块，来源与队列分别持有引用。

设计／审核参考：[冻结原始文档](../reference/companion_memory_module_design_provider_logging_config.md)。仅供追溯，不作为现行约束。

按关联工作联合阅读：[全部实现表达规范](../CODING_STANDARDS.md)；[产品：批次来源、正式记忆与关系](../product/provenance-and-memory.md)；[产品：双指标、召回反馈、遗忘与删除](../product/lifecycle.md)。

返回[文档总入口](../INDEX.md)；实际进度见[工作状态](../work/STATUS.md)。

已批准的使用反馈、覆盖序列及发布事实端口见[本地信息闭环主契约](../architecture/local-information-feedback.md)；发布与恢复规则只在该正文维护。

## 按边界定位

本轮已批准细化：[对象／关系／修订／状态](../architecture/formal-memory-source-media.md#objects)、[完整来源与候选终结](../architecture/formal-memory-source-media.md#sources-candidates)、[公开读取及权限](../architecture/formal-memory-source-media.md#ports-permissions)。集中决定与验收均见该契约，实施已获授权。

下表仅作导航；模块职责与端口正文在本文件后半部，共享事务和详细契约链接到各自唯一正文。

| 关注点 | 阅读位置与边界 |
| --- | --- |
| 职责、非职责、数据所有权 | [模块所有权行](../architecture/ownership.md#m06)；[模块职责与端口](memory.md#contract) |
| 公开能力与依赖 | 本模块正文的“建议端口”仍为草名；[统一依赖与启动边界](../architecture/ownership.md#source-line-267) |
| 状态、提交和恢复 | [接收承诺与允许损失](../architecture/persistence-and-transactions.md#section-04)；[T03](../architecture/persistence-and-transactions.md#t03)、[T07](../architecture/persistence-and-transactions.md#t07)、[T08](../architecture/persistence-and-transactions.md#t08)；[远程结果未知](../architecture/persistence-and-transactions.md#source-line-518)（仅涉及外部调用时） |
| 配置生效 | [统一快照及本模块相关参数](../architecture/configuration.md#source-line-878)；[命名空间与消费者](../architecture/configuration.md#source-line-858)。具体键按Schema固定，不自行填写默认值 |
| 产品验收定位 | [A23](../product/acceptance.md#a23)、[A24](../product/acceptance.md#a24)、[A26](../product/acceptance.md#a26)、[A27](../product/acceptance.md#a27)、[A29](../product/acceptance.md#a29)、[A30](../product/acceptance.md#a30)、[A31](../product/acceptance.md#a31)、[A32](../product/acceptance.md#a32)、[A34](../product/acceptance.md#a34)、[A39](../product/acceptance.md#a39)、[A42](../product/acceptance.md#a42)（任务范围扩大时按完整表补充） |
| 工程验收定位 | [V07](../architecture/acceptance.md#v07)、[V08](../architecture/acceptance.md#v08)、[V18](../architecture/acceptance.md#v18)、[V19](../architecture/acceptance.md#v19)、[V20](../architecture/acceptance.md#v20)（预期行为，未执行） |
| 尚未决定 | 类别Schema、修订匹配及受控读取已在[集中契约](../architecture/formal-memory-source-media.md#objects)批准；反馈有效期、临近删除反馈仍待固定，外部完整来源未开放。 [产品剩余契约](../product/decisions-and-delivery.md#section-22)；[工程冻结点](../architecture/implementation-options.md#source-line-1216)；[当前任务与阻塞](../work/CURRENT_TASK.md) |

<a id="contract"></a>

## 模块职责与端口

<a id="source-line-357"></a>

### M06 记忆、来源与关系

**拥有**：记忆当前内容、修订号、类别、主体与世界/扮演范围、来源快照、真实支持边、双指标、生命周期、遗忘起点、使用强化收据、下游待整理标记。

自我事实也保存在这里，以认知对象指向角色本身；[M07](self-model.md#contract)读取其视图，不复制成独立真相。来源快照通过不可变消息ID与内容保存完整三段，同批多个记忆共享快照，实际锚点分别保存。

继承统一接受量表与双阈值滞回。相信程度不是保留强度；普通查询过滤遗忘对象，深度读取不恢复，真实使用反馈增强强度，达到恢复阈值才恢复。重复使用反馈和批次终结须幂等；恢复关联影响通过工作标记交由agent检查。

删除撤销对象并保留无正文的必要墓碑/依赖状态，允许审计保留历史；共享来源不立即遮蔽，相关评价和persona等待梦境。索引尚未删除旧ID不使其可读。修改正文后旧版本只能经开发者审计查看。

对外写命令可以校验`expected_revision`，避免一个基于旧内容生成的变更集静默覆盖较新内容。冲突不会自动重复整次LLM学习；先用保存的候选检查是否可安全应用，不可时进入明确冲突处理/下轮整理。

**建议端口**：`apply_change_set`、`get_active`、`get_for_deep_recall`、`apply_usage`、`update_scores`、`delete_object`、`read_provenance`、`list_dirty_dependencies`。
