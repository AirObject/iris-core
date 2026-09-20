# 接入与入口注册模块入口

> 本文件是本主题的现行正文，在此唯一维护。既有要求、已批准契约、建议和待批准事项保持各自状态；迁移不新增产品决定或实现授权。文档关系见[总入口](../INDEX.md)。

适用主题与局部定义：入口绑定、原始事件和幂等键；主体同一联系不由接入层判断。

设计／审核参考：[冻结原始文档](../reference/companion_memory_module_design_provider_logging_config.md)。仅供追溯，不作为现行约束。

按关联工作联合阅读：[全部实现表达规范](../CODING_STANDARDS.md)；[产品：原始输入、媒体理解与引用清理](../product/input-and-media.md)。

返回[文档总入口](../INDEX.md)；实际进度见[工作状态](../work/STATUS.md)。

## 按边界定位

已批准依赖：[媒体出现与输入状态](../architecture/formal-memory-source-media.md#interpretation)、[真实上传及接收承诺](../architecture/formal-memory-source-media.md#files-gc)、[权限投影](../architecture/formal-memory-source-media.md#ports-permissions)；旧事件／装配兼容见同一集中契约。

本次详细接口及联合验收见[持久接入与批次运行契约](../architecture/durable-ingress-and-batch-runtime.md#ingress)（契约已批准；实际验证见[STATUS](../work/STATUS.md)）；本模块原职责和既有批准状态不变。

外部HTTP／WS、媒体上传、可信通知路由和宿主接入页面的增量已批准，见[通信契约与实施路径](../architecture/external-communication.md)及[本模块端口](#external-communication)；当前执行段见CURRENT_TASK。

下表仅作导航；模块职责与端口正文在本文件后半部，共享事务和详细契约链接到各自唯一正文。

| 关注点 | 阅读位置与边界 |
| --- | --- |
| 职责、非职责、数据所有权 | [模块所有权行](../architecture/ownership.md#m01)；[模块职责与端口](ingress.md#contract) |
| 公开能力与依赖 | 本模块正文的“建议端口”仍为草名；[统一依赖与启动边界](../architecture/ownership.md#source-line-267) |
| 状态、提交和恢复 | [接收承诺与允许损失](../architecture/persistence-and-transactions.md#section-04)；[T01](../architecture/persistence-and-transactions.md#t01)；[远程结果未知](../architecture/persistence-and-transactions.md#source-line-518)（仅涉及外部调用时） |
| 配置生效 | [统一快照及本模块相关参数](../architecture/configuration.md#source-line-878)；[命名空间与消费者](../architecture/configuration.md#source-line-858)。具体键按Schema固定，不自行填写默认值 |
| 产品验收定位 | [A01](../product/acceptance.md#a01)、[A02](../product/acceptance.md#a02)、[A03](../product/acceptance.md#a03)、[A17](../product/acceptance.md#a17)、[A61](../product/acceptance.md#a61)（任务范围扩大时按完整表补充） |
| 工程验收定位 | [V01](../architecture/acceptance.md#v01)、[V03](../architecture/acceptance.md#v03)（验收要求；实际覆盖见[STATUS](../work/STATUS.md)） |
| 批准边界与后续增量 | 输入、原键和错误格式已在[整体契约](../architecture/durable-ingress-and-batch-runtime.md#ports)批准；本地管理员与宿主令牌见[已批准身份契约](../architecture/managed-runtime-and-deployment.md#identity)。WS、正式通知路由与外部媒体接入补全见[已批准决定](../architecture/external-communication.md#decisions)；实际授权见[当前任务](../work/CURRENT_TASK.md) |

<a id="contract"></a>

## 模块职责与端口

<a id="source-line-290"></a>

### M01 接入与入口注册

**输入**：原始事件、回复准备请求、查询、使用反馈、状态更新、目标操作。  
**输出**：已持久化接收回执、对应业务结果，或明确的拒绝/故障。

入口使用稳定内部ID及平台/原始入口标识；调用凭据绑定允许的入口集合。宿主不能仅修改请求中的`entry_id`就读另一个入口的缓存。跨入口正式记忆的可读性与缓存绑定分开。

原始消息使用`(host_id, platform, entry_id, external_event_id)`等稳定标识防重复投递。相同正文但不同事件ID仍是不同发生记录；hash不作为所有文本消息的唯一键。缺少外部ID时给出接入方幂等键契约，不能推断“内容相同就是同一事件”。

内部人物关联交给[M06](memory.md#contract)/[M05](cognition.md#contract)，入口注册不因昵称一样合并平台主体。文本中自称管理员、provider拒学或目标完成，不自动升级为控制命令。

**建议端口**：`accept_event`、`resolve_entry`、`authorize_operation`、`dispatch_request`。

<a id="external-communication"></a>

### 外部通信端口增量（已批准）

宿主／入口注册提供受控管理端口，登记、读取与原键确认由ingress维护绑定，management核验管理身份并编排；既有平台Schema、单绑定当前状态写宿主和入口隔离保持。身份签发、逻辑路由及默认候选规则唯一见[管理身份](../architecture/managed-runtime-and-deployment.md#communication-identity)。

宿主HTTP补齐[媒体上传／inspect](../architecture/formal-memory-source-media.md#host-media-upload)、能力发现和本身份通知状态查询；HTTP旧POST封套、版本和原确认规则见[外部协议](../architecture/external-communication.md#http)。WS仅提供[通知、订阅与ACK](../architecture/external-communication.md#ws)，与HTTP共享身份、领域端口和门控，不提供第二套业务RPC。新增操作权限默认空，外层适配不得直接访问其他owner的表或私有状态。
