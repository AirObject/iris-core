# 运行模式与工作调度模块入口

> 本文件是权威原文的阅读视图，不是独立需求。原文仍是权威依据；后续修改规则时，先更新对应原文，再同步本视图与[覆盖映射](../work/ORGANIZATION_REPORT.md#coverage)。保留原文“已确定、建议、示例、待确认”的性质；下列导读不新增决策。正文含原有编号，仅限文档追踪。

适用主题与局部定义：模式及epoch（门控代次）、工作租约和调度归属；队列归缓存模块，模型准入账本归Provider。

来源：[原文 L303–L316](../../companion_memory_module_design_provider_logging_config.md#section-06)。行号对应整理时的哈希基线。

按关联工作联合阅读：[全部实现表达规范](../../CODING_STANDARDS.md)；[产品：梦境整理、专注隔离与入口回流](../product/dream.md)。

返回[文档总入口](../INDEX.md)；实际进度见[工作状态](../work/STATUS.md)。

## 按边界定位

下表是阅读路线；完整模块原文在本文件后半部，共享事务和详细设计仅通过链接引用。

| 关注点 | 阅读位置与边界 |
| --- | --- |
| 职责、非职责、数据所有权 | [模块所有权行](../architecture/ownership.md#m02)；[本模块完整契约](runtime.md#contract) |
| 公开能力与依赖 | 本模块正文的“建议端口”仍为草名；[统一依赖与启动边界](../architecture/ownership.md#source-line-267) |
| 状态、提交和恢复 | [接收承诺与允许损失](../architecture/persistence-and-transactions.md#section-04)；[T02](../architecture/persistence-and-transactions.md#t02)、[T09](../architecture/persistence-and-transactions.md#t09)；[远程结果未知](../architecture/persistence-and-transactions.md#source-line-518)（仅涉及外部调用时） |
| 配置生效 | [统一快照及本模块相关参数](../architecture/configuration.md#source-line-878)；[命名空间与消费者](../architecture/configuration.md#source-line-858)。具体键按Schema固定，不自行填写默认值 |
| 产品验收定位 | [A67](../product/acceptance.md#a67)、[A93](../product/acceptance.md#a93)、[A94](../product/acceptance.md#a94)、[A106](../product/acceptance.md#a106)（任务范围扩大时按完整表补充） |
| 工程验收定位 | [V02](../architecture/acceptance.md#v02)、[V24](../architecture/acceptance.md#v24)、[V30](../architecture/acceptance.md#v30)、[V49](../architecture/acceptance.md#v49)、[V81](../architecture/acceptance.md#v81)（预期行为，未执行） |
| 尚未决定 | 入梦在途收尾、发布后开放点和异常恢复管理操作仍待批准。 [产品剩余契约](../product/decisions-and-delivery.md#section-22)；[工程冻结点](../architecture/implementation-options.md#source-line-1216)；[整理待确认项](../work/ORGANIZATION_REPORT.md#open-items) |

<a id="contract"></a>

## 完整模块原文阅读视图

<a id="source-line-303"></a>

### M02 运行模式与工作调度

**拥有**：`runtime_mode`、`mode_epoch`、调度租约、任务类型/状态、恢复进度及任务级执行配额；模型请求配额、token/费用预留与尝试账本由[M13](provider.md#contract)拥有。任务保存到数据库；内存只负责唤醒和有限执行。

建议运行模式为`RECOVERING`、`NORMAL`、`DREAM_PREPARING`、`DREAM_FOCUSED`、`DRAINING`、`FAULTED`。非专注梦境是可并行的整理任务，不自动进入全面拒绝模式。专注门控与各入口是否仍在回流是两个维度，不能要求所有入口清空后其他入口才继续工作。

服务启动先恢复模式与批次，再放行业务接口；原来正处专注模式时不能因内存标志消失瞬间放行。进入专注前停止启动普通学习，已运行批次安全收尾；新输入先转专用暂存。退出时采用“梦境完成发布后开放业务、各入口独立回流”的建议策略，正式接口冻结时批准这一切换点。

调度按入口公平推进并申请[M13](provider.md#contract)的模型调用准入；全局API并发、账户配额和费用预算由[M13](provider.md#contract)统一执行，不在[M02](runtime.md#contract)再维护一套独立限流计数。首期认知写入可串行，远程I/O和只读请求可有限并发；不能让一个直播入口的积压饿死其他入口。每入口最多一个目标批次执行者，以租约和唯一约束防重复消费。

任务类型要区分新学习、梦境步骤、索引更新、本地事务恢复、回流、媒体理解和提醒投递。已终结失败学习不因重新启动scheduler再次执行。索引本地重建、事务恢复与首次处理尚未执行任务，不是业务重试失败批次。

**建议端口**：`check_gate`、`schedule_work`、`claim_work`、`enter_focus`、`finish_focus`、`recover_runtime`。
