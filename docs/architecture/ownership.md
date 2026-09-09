# 十五个逻辑模块、基础设施与依赖

> 本文件是权威原文的阅读视图，不是独立需求。原文仍是权威依据；后续修改规则时，先更新对应原文，再同步受影响的视图与相对链接；[覆盖映射](../work/ORGANIZATION_REPORT.md#coverage)仅作整理历史保留，不要求持续更新。保留原文“已确定、建议、示例、待确认”的性质；下列导读不新增决策。正文含原有编号，仅限文档追踪。

适用主题与局部定义：此处完整保留所有权表与依赖规则；M01—M15是原模块，I01—I02是原基础设施，不新增模块，也不将其解读为微服务。

来源：[原文 L232–L285](../../companion_memory_module_design_provider_logging_config.md#section-05)。行号对应整理时的哈希基线。

按关联工作联合阅读：[跨模块事务](persistence-and-transactions.md#section-07)；[数据分组草图](request-paths.md#source-line-955)；[代码规范](../../CODING_STANDARDS.md)。

返回[文档总入口](../INDEX.md)；实际进度见[工作状态](../work/STATUS.md)。

<a id="section-05"></a>

<a id="source-line-234"></a>

## 5. 模块总览与数据所有权

<a id="source-line-236"></a>

### 5.1 十五个逻辑模块

| ID | 模块 | 拥有的数据与主要职责 | 明确不负责 |
| --- | --- | --- | --- |
| <a id="m01"></a>M01 | 接入与入口注册 `ingress` | 宿主身份、入口绑定、输入契约、外部事件幂等 | 总结、评分、自己解释人物同一关系 |
| <a id="m02"></a>M02 | 运行模式与工作调度 `runtime` | 模式、epoch、执行租约、持久化任务、优先级及恢复控制 | 直接决定记忆正文或偷偷重试失败学习 |
| <a id="m03"></a>M03 | 入口缓存与批次 `buffers` | 原始输入的队列归属、S1/S2/S3、冻结批次、梦境暂存与回流、终结轮转 | 生成认知、跨入口混批 |
| <a id="m04"></a>M04 | 媒体 `media` | 原始blob、hash、业务引用、理解结果和无引用清理 | 将重复出现的事件合并、即时删除共享文件 |
| <a id="m05"></a>M05 | 认知加工 `cognition` | 学习运行、agent工具调用计划、候选变更集、上下文清单 | 绕过生命周期、直接写数据库或改外部当前状态 |
| <a id="m06"></a>M06 | 记忆、来源与关系 `memory` | 记忆当前值、源快照、实体/关系、相信程度、保留强度、生命周期、使用强化收据 | 向外提供未授权缓存、返回开发者历史日志 |
| <a id="m07"></a>M07 | 自我与persona `self_model` | 自我认知视图、persona候选/当前发布指针、依据与监管检查 | 复制出第二套自我事实库、按每次回复重写persona |
| <a id="m08"></a>M08 | 检索与信息提供 `retrieval` | 词法/向量索引映射、查询缓存、召回凭据、分区结果、rerank | 在普通查询暗中深度召回、实时长链LLM推理 |
| <a id="m09"></a>M09 | 当前状态 `state` | 外部报告状态、活动/子状态时间、来源与陈旧标记 | 内部猜测当前事实或将目标当正在执行 |
| <a id="m10"></a>M10 | 多目标与意图 `goals` | 目标、外部注入、相似去重、合并别名、提醒计划及有限投递记录 | 具体执行与效果监控 |
| <a id="m11"></a>M11 | 梦境整理 `dream` | 梦境运行、认知脏标记处理、检查点、persona发布请求、整理报告 | 私自改变业务门控、不经[M03](../modules/buffers.md#contract)移交积压 |
| <a id="m12"></a>M12 | Web与管理入口 `management` | 初始化流程、管理会话与权限、各模块只读页面、管理操作编排 | 自己另存配置、另算用量、直接读日志文件或绕过专注门控 |
| <a id="m13"></a>M13 | 模型Provider `provider` | 全部模型请求入口、协议适配、能力/路由、有限尝试、配额与费用预留、调用账本和统计 | 决定记忆如何轮转、保存第二份配置真相、把未知费用记为零 |
| <a id="m14"></a>M14 | 日志与审计 `logging_service` | 统一结构化日志、标准等级、控制台/Web/文件输出、脱敏、轮转、审计事件与受控查询 | 以诊断日志代替费用账本、因日志等级过滤业务审计、向认知agent开放日志 |
| <a id="m15"></a>M15 | 统一配置 `configuration` | 配置Schema、默认值、层级合并、校验、版本、快照、热修改、生效计划、敏感字段引用 | 直接修改正在运行的批次、自动迁移全部向量、把业务对象当配置 |

上述所有权是逻辑上的唯一修改入口，允许多个模块的数据存在同一数据库内。跨模块原子操作由应用编排层建立一个Unit of Work，让各模块加入同一事务；不要求每个模块建立自己的数据库连接后单独提交。

<a id="source-line-258"></a>

### 5.2 基础设施

| ID | 基础设施 | 内容 |
| --- | --- | --- |
| <a id="i01"></a>I01 | 持久化与事务 | Repository实现、Unit of Work、迁移、短事务、唯一键、检查点和备份；承载Provider账本、配置版本和审计的持久化实现 |
| <a id="i02"></a>I02 | 本地索引与运行支持 | 词法/向量引擎、文件发布与同步、时钟、受限执行器、底层网络与密钥提供器实现 |

Provider的模型SDK与协议适配器只存在于[M13](../modules/provider.md#contract)私有实现中。日志库、输出处理器只由[M14](../modules/logging.md#contract)集中初始化，配置文件解析和环境变量读取只由启动代码/[M15](../modules/configuration.md#contract)执行。基础设施不拥有认知规则；业务模块不硬编码供应商字段、文件路径或模型名称。

<a id="source-line-267"></a>

### 5.3 依赖约束

```text
HTTP/Web入口 → 用例编排 → 领域模块公开端口 → Repository/Index接口
                                  ├→ M13 Provider：唯一模型出口
                                  ├→ M14 日志：统一记录与审计
                                  └→ M15 配置：只读类型化快照/受控变更
                                        ↑
                           基础设施实现由启动代码注入
```

[M08](../modules/retrieval.md#contract)可以读[M06](../modules/memory.md#contract)、[M07](../modules/self-model.md#contract)、[M09](../modules/state.md#contract)、[M10](../modules/goals.md#contract)和[M03](../modules/buffers.md#contract)的限定视图，但不能直接修改它们的表。[M05](../modules/cognition.md#contract)和[M11](../modules/dream.md#contract)可以通过受控命令提出认知变化；[M06](../modules/memory.md#contract)负责执行合法状态转换。[M02](../modules/runtime.md#contract)拥有模式，[M11](../modules/dream.md#contract)只请求转换；[M03](../modules/buffers.md#contract)拥有实际队列，[M11](../modules/dream.md#contract)不另复制一套“梦境消息数据库”。

[M13](../modules/provider.md#contract)读取[M15](../modules/configuration.md#contract)的已生效模型/策略快照，并向[M14](../modules/logging.md#contract)发诊断事件；[M15](../modules/configuration.md#contract)的变更通过[M14](../modules/logging.md#contract)审计，但日志热配置使用类型化参与者端口，不令日志模块反过来动态读取配置数据库。启动阶段使用最小应急stderr记录器，配置恢复后切换正式日志，避免“先有配置才能日志、先有日志才能配置”的循环。[M12](../modules/management.md#contract)只做鉴权与展示，不成为所有模块的唯一调用中转。

模型网络出口用导入约束、客户端注入和架构测试限制；禁止[M04](../modules/media.md#contract)/[M05](../modules/cognition.md#contract)/[M07](../modules/self-model.md#contract)/[M08](../modules/retrieval.md#contract)/[M10](../modules/goals.md#contract)/[M11](../modules/dream.md#contract)及agent框架自行实例化模型SDK或访问模型端点。可信扩展只能取得带`caller_module/extension_id`的ProviderPort，不取得密钥或裸客户端。此为受支持代码的架构约束，不宣称能在同进程内沙箱化任意恶意Python代码；不可信扩展需另行进程/网络隔离。

共享部分仅保留ID、时间、错误、事务接口等小型契约。禁止出现万能`MemoryManager`、全模块可访问的数据库对象或带任意SQL/文件读取能力的agent工具。


图示／代码框中的文档标记定位：[M13](../modules/provider.md#contract)、[M14](../modules/logging.md#contract)、[M15](../modules/configuration.md#contract)。这些标记仅用于本文阅读，不能进入未来实现。


<a id="section-06"></a>
<a id="module-contracts"></a>

## 模块契约阅读入口

技术原文第6节的总标题在此索引；其十五个完整模块正文分别保留在下列文件。[原文第6节](../../companion_memory_module_design_provider_logging_config.md#section-06)。

| 原模块 | 完整契约入口 |
| --- | --- |
| M01 | [接入与入口注册](../modules/ingress.md#contract) |
| M02 | [运行模式与工作调度](../modules/runtime.md#contract) |
| M03 | [入口缓存与批次](../modules/buffers.md#contract) |
| M04 | [媒体](../modules/media.md#contract) |
| M05 | [认知加工](../modules/cognition.md#contract) |
| M06 | [记忆、来源与关系](../modules/memory.md#contract) |
| M07 | [自我与persona](../modules/self-model.md#contract) |
| M08 | [检索与信息提供](../modules/retrieval.md#contract) |
| M09 | [当前状态](../modules/state.md#contract) |
| M10 | [多目标与意图](../modules/goals.md#contract) |
| M11 | [梦境整理](../modules/dream.md#contract) |
| M12 | [Web与管理入口](../modules/management.md#contract) |
| M13 | [模型Provider](../modules/provider.md#contract) |
| M14 | [日志与审计](../modules/logging.md#contract) |
| M15 | [统一配置](../modules/configuration.md#contract) |

基础设施仍为原文I01与I02，详细恢复边界见[持久化与事务](persistence-and-transactions.md)；不另建第三个基础设施模块。
