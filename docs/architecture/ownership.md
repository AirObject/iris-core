# 十五个逻辑模块、基础设施与依赖

> 本文件是本主题的现行正文，在此唯一维护。既有要求、已批准契约、建议和待批准事项保持各自状态；迁移不新增产品决定或实现授权。文档关系见[总入口](../INDEX.md)。

适用主题与局部定义：此处完整保留所有权表与依赖规则；M01—M15是逻辑模块，I01—I02是基础设施，不新增模块，也不将其解读为微服务。

设计／审核参考：[冻结原始文档](../reference/companion_memory_module_design_provider_logging_config.md)。仅供追溯，不作为现行约束。

按关联工作联合阅读：[跨模块事务](persistence-and-transactions.md#section-07)；[数据分组草图](request-paths.md#source-line-955)；[代码规范](../CODING_STANDARDS.md)。

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

<a id="implementation-packages"></a>

#### 逻辑模块与实现包的对应

Python包、命令命名空间、配置域及测试目录不与M01—M15一一对应；数量不能用来判定新增逻辑模块。`information`是[已批准本地信息闭环](local-information-feedback.md)的应用协调包，不登记为M16，也不获得独立业务表所有权。其代码中的`owner_namespace='information'`标识跨模块命令及幂等身份，不能据此取得参与者表的修改权，重构不能改写既有持久命令身份。

| 实现职责 | 唯一数据所有者／边界 |
| --- | --- |
| information业务查询、初始化、反馈及维护协调 | 应用编排建立同一UoW；retrieval持有索引、凭据和协调根，memory持有正式对象及强化收据，state持有外部状态；没有information私有业务仓储 |
| information提醒和定时唤醒 | goals持有目标、计划及attempt；runtime持有模式、租约和门控；协调器不以发送调用或内存定时器代替持久事实 |
| information管理能力及宿主绑定 | management负责认证／权限，ingress负责入口绑定；协调器只能调用绑定后的有限端口 |
| runtime宿主、assembly和各特性用例 | 属于图中的“用例编排”与启动装配；它们读取多个公开端口，不因此扩大M02的模式／调度数据所有权 |
| cognition中的梦境材料、模型审查和候选 | M05持有认知材料与候选；M11持有梦境运行、检查点和发布请求；不能因同属梦境功能就把两种数据所有权合并 |
| configuration的各类快照、编解码和持久适配 | M15统一拥有配置；文件数量及不同已批准格式不构成新模块或第二份配置真相 |

共享闭合记录与固定仓储声明由I01的[record_primitives](../../companion_memory/persistence/record_primitives.py)、[record_repository](../../companion_memory/persistence/record_repository.py)承载；information原路径只作兼容导出，生产调用方直接依赖I01。Provider所需的输入端口由Provider自身定义，媒体业务对象仍属于media，无需新增共享契约包。

<a id="external-communication-ownership"></a>

#### 外部通信所有权（已批准）

| 责任 | 所有者及边界 |
| --- | --- |
| HTTP／WS解析、握手、帧、连接生命周期 | 服务器与通信适配；不直接访问业务表，不生成业务成功回执 |
| 宿主、入口、允许操作、令牌撤销 | ingress维护入口绑定；management维护认证及权限；消息自报身份不授予权限 |
| 逻辑通知路由、路由revision、事件授权 | management的受控管理记录；配置模块只保存策略参数，不把路由对象、令牌和在线会话塞进配置版本 |
| 目标计划、宽限计时、发送尝试、ACK事实 | goals沿用唯一账本；门控所有者提供可信转换依据，goals经受控交接保存计时／恢复事实；通信层提供受限发送端口，不复制目标或第二份投递真相 |
| 运行模式提示 | runtime提供已确认模式转换的受限观察；提示不建投递记录、不要求ACK，宿主以HTTP查询的当前状态为准 |
| WS在线连接与订阅 | 进程内有限会话，由持久权限与路由签发；接管只更换进程内消费者，不改路由持久事实；重启全部失效，不靠恢复socket取得发送权 |
| 通信参数、默认值、生效指针 | configuration唯一注册／解析／激活，Web为表单与投影 |
| 诊断及必要审计 | logging统一收集；路由／权限／配置变更和投递账本需要的审计随各自事务提交 |

管理部署负责持久身份／路由与探针，通信适配只维护有界在线会话；详细协议与实施依赖见[外部通信](external-communication.md)。

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

<a id="executable-boundaries"></a>

#### 可执行的依赖边界

[架构测试](../../tests/architecture/test_import_boundaries.py)扫描生产源码的绝对／相对导入、函数内导入、TYPE_CHECKING及字面量动态导入；[允许边清单](../../tests/architecture/rules.py)限制包间协作，并对共享工具和持久层配置接入施加更窄的文件级约束。新增包、允许边或动态加载方式须连同职责说明审查，不能运行扫描后自动把现有违规边全部加入清单。它是静态防线，不证明Python进程内任意反射或恶意代码隔离，也不宣称整个现有文件依赖图已经无环。

Provider定义[媒体输入端口](../../companion_memory/provider/media_input.py)，仅依赖事务与实际完成通知的基础契约。[媒体适配器](../../companion_memory/media/provider_source.py)在可信装配处核验实际MediaService或图片租约，转换为Provider需要的不可变字节、请求归属与有限校验回调；MediaError、ProcessingRead、CheckedImage、DailyImageLease及完整媒体记录继续由media定义和管理。Provider不导入媒体实现、读取媒体路径或获取媒体SQL／租约释放能力。

输入转换复用原字节和实际完成通知；图片校验回调每次回到原所有者验证原租约及必要的事务证据，原租约释放后输入即失效。原库／scope校验、PROCESSING保护、请求持久标识和占用释放边界保持不变。goals通过[只读路由端口](../../companion_memory/goals/notification_port.py)参加原权限所有者的事务，不依赖management的具体IdentityAuthority类。

`persistence ↔ configuration`须按文件职责判断：配置持久适配调用I01；I01读取已验证快照，并在现有显式格式／容量能力处核验原配置签发者。这些接入在允许清单逐项限定，不能把整个configuration包作为任意反向依赖口。网络规则限制模型客户端与外连；管理CLI的本地健康请求和goals的显式回环测试接收器单列，不将HTTP服务端或纯URL解析误判为模型出口。


图示／代码框中的文档标记定位：[M13](../modules/provider.md#contract)、[M14](../modules/logging.md#contract)、[M15](../modules/configuration.md#contract)。这些标记仅用于本文阅读，不能进入未来实现。


<a id="section-06"></a>
<a id="module-contracts"></a>

## 模块契约阅读入口

十五个模块的职责与端口分别在下列文件唯一维护；本表仅提供入口。详细公共契约与跨模块规则通过链接引用，不复制维护。

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

基础设施为I01与I02，详细恢复边界见[持久化与事务](persistence-and-transactions.md)；不另建第三个基础设施模块。
