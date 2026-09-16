# 媒体模块入口

本模块日常图片的真实协议、原字节租约及模态边界，见[集中契约](../architecture/daily-cognition-and-image-learning.md#4-图片provider与材料所有权)。

> 本文件是本主题的现行正文，在此唯一维护。既有要求、已批准契约、建议和待批准事项保持各自状态；迁移不新增产品决定或实现授权。文档关系见[总入口](../INDEX.md)。

适用主题与局部定义：blob是不可变原始媒体字节对象；一个blob可对应多个独立发生事件和业务引用。

设计／审核参考：[冻结原始文档](../reference/companion_memory_module_design_provider_logging_config.md)。仅供追溯，不作为现行约束。

按关联工作联合阅读：[全部实现表达规范](../CODING_STANDARDS.md)；[产品：原始输入、媒体理解与引用清理](../product/input-and-media.md)。

返回[文档总入口](../INDEX.md)；实际进度见[工作状态](../work/STATUS.md)。

## 按边界定位

已批准细化：[集中契约的媒体状态与复用](../architecture/formal-memory-source-media.md#interpretation)、[真实字节发布／引用／GC](../architecture/formal-memory-source-media.md#files-gc)、[容量与整体验收](../architecture/formal-memory-source-media.md#capacity)。实施已获授权，既有职责不变。

下表仅作导航；模块职责与端口正文在本文件后半部，共享事务和详细契约链接到各自唯一正文。

| 关注点 | 阅读位置与边界 |
| --- | --- |
| 职责、非职责、数据所有权 | [模块所有权行](../architecture/ownership.md#m04)；[模块职责与端口](media.md#contract) |
| 公开能力与依赖 | 本模块正文的“建议端口”仍为草名；[统一依赖与启动边界](../architecture/ownership.md#source-line-267) |
| 状态、提交和恢复 | [接收承诺与允许损失](../architecture/persistence-and-transactions.md#section-04)；[T01](../architecture/persistence-and-transactions.md#t01)、[T03](../architecture/persistence-and-transactions.md#t03)、[T04](../architecture/persistence-and-transactions.md#t04)、[T05](../architecture/persistence-and-transactions.md#t05)、[T06](../architecture/persistence-and-transactions.md#t06)；[远程结果未知](../architecture/persistence-and-transactions.md#source-line-518)（仅涉及外部调用时） |
| 配置生效 | [统一快照及本模块相关参数](../architecture/configuration.md#source-line-878)；[命名空间与消费者](../architecture/configuration.md#source-line-858)。具体键按Schema固定，不自行填写默认值 |
| 产品验收定位 | [A04](../product/acceptance.md#a04)、[A05](../product/acceptance.md#a05)、[A06](../product/acceptance.md#a06)、[A13](../product/acceptance.md#a13)、[A63](../product/acceptance.md#a63)、[A64](../product/acceptance.md#a64)、[A65](../product/acceptance.md#a65)、[A91](../product/acceptance.md#a91)、[A103](../product/acceptance.md#a103)（任务范围扩大时按完整表补充） |
| 工程验收定位 | [V10](../architecture/acceptance.md#v10)、[V11](../architecture/acceptance.md#v11)、[V12](../architecture/acceptance.md#v12)、[V13](../architecture/acceptance.md#v13)、[V14](../architecture/acceptance.md#v14)、[V15](../architecture/acceptance.md#v15)（验收要求；实际覆盖见[STATUS](../work/STATUS.md)） |
| 尚未决定 | 理解状态、新事件处理、上传限制与GC保护参数已在[集中契约](../architecture/formal-memory-source-media.md#interpretation)批准；内部部分结果生产路径和保护解除仍未开放。 [产品剩余契约](../product/decisions-and-delivery.md#section-22)；[工程冻结点](../architecture/implementation-options.md#source-line-1216)；[当前任务与阻塞](../work/CURRENT_TASK.md) |

<a id="contract"></a>

## 模块职责与端口

<a id="source-line-329"></a>

### M04 媒体

**拥有**：`blob_id`、原始文件hash/字节长度/类型、物理存放状态、业务引用、理解结果与处理版本。

建议对原始字节流计算SHA-256，文件路径按hash分层。上传时流式计算，完成持久化后以唯一hash与长度检查避免重复保存。相同图片被不同人发送十次，只保留一份二进制，但保存十次出现的消息与来源联系。重新编码、缩放或元数据不同的文件可能hash不同；首期不将感知相似去重混入精确文件去重。

外部成功理解结果优先使用。内容本身的内部理解可按`blob_hash + interpretation_fingerprint + task + prompt_version + interpretation_scope`缓存；有关“这个人为什么此时发送它”的情境解释不能仅凭文件hash跨场景复用。

媒体理解明确敏感拒绝时保存固定占位，不将其视作待自动补做的缺失。一般失败用明确状态表示，不把错误文本当作理解成功；不同模态是否被API支持由[M13](provider.md#contract)能力表校验。

文件写入与数据库不天然处于同一事务。采用上传暂存→校验/同步→同文件系统发布不可变blob→数据库建立READY及业务引用的有序流程。崩溃可能留下孤立文件，由定时整理清理；不能留下已确认接收事件却找不到其承诺已保存的附件。GC通过状态/锁与引用复核协调，不能在检查“无引用”后与新的引用建立并发误删。

**建议端口**：`stage_blob`、`publish_blob`、`attach_reference`、`get_or_create_interpretation`、`release_reference`、`collect_unreferenced`。
