# 接入与入口注册模块入口

> 本文件是权威原文的阅读视图，不是独立需求。原文仍是权威依据；后续修改规则时，先更新对应原文，再同步受影响的视图与相对链接；[覆盖映射](../work/ORGANIZATION_REPORT.md#coverage)仅作整理历史保留，不要求持续更新。保留原文“已确定、建议、示例、待确认”的性质；下列导读不新增决策。正文含原有编号，仅限文档追踪。

适用主题与局部定义：入口绑定、原始事件和幂等键；主体同一联系不由接入层判断。

来源：[原文 L290–L302](../../companion_memory_module_design_provider_logging_config.md#section-06)。行号对应整理时的哈希基线。

按关联工作联合阅读：[全部实现表达规范](../../CODING_STANDARDS.md)；[产品：原始输入、媒体理解与引用清理](../product/input-and-media.md)。

返回[文档总入口](../INDEX.md)；实际进度见[工作状态](../work/STATUS.md)。

## 按边界定位

下表是阅读路线；完整模块原文在本文件后半部，共享事务和详细设计仅通过链接引用。

| 关注点 | 阅读位置与边界 |
| --- | --- |
| 职责、非职责、数据所有权 | [模块所有权行](../architecture/ownership.md#m01)；[本模块完整契约](ingress.md#contract) |
| 公开能力与依赖 | 本模块正文的“建议端口”仍为草名；[统一依赖与启动边界](../architecture/ownership.md#source-line-267) |
| 状态、提交和恢复 | [接收承诺与允许损失](../architecture/persistence-and-transactions.md#section-04)；[T01](../architecture/persistence-and-transactions.md#t01)；[远程结果未知](../architecture/persistence-and-transactions.md#source-line-518)（仅涉及外部调用时） |
| 配置生效 | [统一快照及本模块相关参数](../architecture/configuration.md#source-line-878)；[命名空间与消费者](../architecture/configuration.md#source-line-858)。具体键按Schema固定，不自行填写默认值 |
| 产品验收定位 | [A01](../product/acceptance.md#a01)、[A02](../product/acceptance.md#a02)、[A03](../product/acceptance.md#a03)、[A17](../product/acceptance.md#a17)、[A61](../product/acceptance.md#a61)（任务范围扩大时按完整表补充） |
| 工程验收定位 | [V01](../architecture/acceptance.md#v01)、[V03](../architecture/acceptance.md#v03)（预期行为，未执行） |
| 尚未决定 | 输入契约、无外部事件ID时的幂等键、鉴权与错误格式尚待固定。 [产品剩余契约](../product/decisions-and-delivery.md#section-22)；[工程冻结点](../architecture/implementation-options.md#source-line-1216)；[整理待确认项](../work/ORGANIZATION_REPORT.md#open-items) |

<a id="contract"></a>

## 完整模块原文阅读视图

<a id="source-line-290"></a>

### M01 接入与入口注册

**输入**：原始事件、回复准备请求、查询、使用反馈、状态更新、目标操作。  
**输出**：已持久化接收回执、对应业务结果，或明确的拒绝/故障。

入口使用稳定内部ID及平台/原始入口标识；调用凭据绑定允许的入口集合。宿主不能仅修改请求中的`entry_id`就读另一个入口的缓存。跨入口正式记忆的可读性与缓存绑定分开。

原始消息使用`(host_id, platform, entry_id, external_event_id)`等稳定标识防重复投递。相同正文但不同事件ID仍是不同发生记录；hash不作为所有文本消息的唯一键。缺少外部ID时给出接入方幂等键契约，不能推断“内容相同就是同一事件”。

内部人物关联交给[M06](memory.md#contract)/[M05](cognition.md#contract)，入口注册不因昵称一样合并平台主体。文本中自称管理员、provider拒学或目标完成，不自动升级为控制命令。

**建议端口**：`accept_event`、`resolve_entry`、`authorize_operation`、`dispatch_request`。
