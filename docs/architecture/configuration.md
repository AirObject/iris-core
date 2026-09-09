# 统一配置注册表、快照与安全热修改

> 本文件是权威原文的阅读视图，不是独立需求。原文仍是权威依据；后续修改规则时，先更新对应原文，再同步本视图与[覆盖映射](../work/ORGANIZATION_REPORT.md#coverage)。保留原文“已确定、建议、示例、待确认”的性质；下列导读不新增决策。正文含原有编号，仅限文档追踪。

适用主题与局部定义：Schema描述参数，revision标识配置版本，snapshot是操作取得的完整不可变视图；生效计划负责让版本在指定边界启用。业务对象和已使用预算不是配置。

来源：[原文 L815–L950](../../companion_memory_module_design_provider_logging_config.md#section-11)。行号对应整理时的哈希基线。

按关联工作联合阅读：[配置模块契约](../modules/configuration.md)；[配置事务](persistence-and-transactions.md#t12)；[参数已定与未定](../product/decisions-and-delivery.md#section-23)；[模式与权限](../modules/runtime.md)；[日志参与者](logging.md#source-line-807)。

返回[文档总入口](../INDEX.md)；实际进度见[工作状态](../work/STATUS.md)。

<a id="section-11"></a>

<a id="source-line-817"></a>

## 11. 独立配置模块：统一参数、版本快照与安全热修改

<a id="source-line-819"></a>

### 11.1 一个权威配置源，不是各模块各读一份文件

[M15](../modules/configuration.md#contract)提供配置注册表、Schema、默认值、作用域、版本和生效服务。各模块注册自己拥有的参数定义与验证器，通过只读类型化快照取值；不直接读取环境变量、不自行解析配置文件、不私自增加未注册默认值。

首期建议采用以下来源模型：

| 来源 | 作用与优先级 |
| --- | --- |
| 随程序发布的Schema与唯一默认值 | 缺省值、类型与校验定义；升级有版本，不自动覆盖已保存用户设置 |
| 初次启用导入文件/初始化表单 | 作为受校验的配置变更进入数据库，之后不是绕过版本服务的实时权威源 |
| 数据库中的当前批准版本 | 日常Web/CLI配置的唯一持久化真相；含版本、作用域和生效指针 |
| 明确允许的部署强制覆写 | 仅白名单环境/启动参数；[M15](../modules/configuration.md#contract)统一合并并显示来源、锁定及重启要求；不能被Web保存一个实际无效的值却显示已生效 |
| 秘密提供器 | 解析secret引用，不将明文密钥合并进可读配置导出 |

同一配置的生效规则按注册表定义：实例默认→允许的特定平台/任务角色/profile/sink覆写→部署锁定覆写。没有Schema声明的跨作用域继承不发生；平台窗口分别配置，不能由某一入口输入正文改其他平台配置。初期不建设额外的分布式配置服务。

配置数据库可用前的启动项（数据目录、秘密根引用、基本监听）由最小bootstrap契约读取；它们仍归入统一Schema与管理展示。配置文件导入、CLI和Web走相同的校验/审计入口；磁盘文件被手工改动不得触发无法追溯的旁路热修改。

<a id="source-line-837"></a>

### 11.2 参数注册表

每项参数至少有以下元信息：

| 字段 | 含义 |
| --- | --- |
| `key / owner_module / schema_revision` | 唯一键、维护模块、Schema版本 |
| `type / default / required / unit` | 类型、唯一默认来源、是否必需、毫秒/字节/token等明确单位 |
| `range / enum / validator / dependencies` | 数值范围、允许项、跨字段与跨模块约束 |
| `scope / override_policy` | 实例、平台、角色、profile、sink等允许范围 |
| `sensitivity / read_roles / write_roles` | 秘密字段、可读身份、可修改身份 |
| `apply_mode / activation_group` | 生效时点、必须协调切换的参数组 |
| `cost_impact / migration_impact / description` | 调用费用/数据迁移影响、面向用户的解释 |
| `deprecated / replacement / upgrade_rule` | 废弃键、替代路径和升级规则 |

Schema生成Web表单、帮助文档和导出说明。未知键默认拒绝而非静默忽略；删除/重命名键有明确兼容策略。配置数值通过相应字段使用，不散落`timeout=30`、`top_k=20`等无法追溯的字面值。

跨字段校验至少包括：恢复阈值高于遗忘阈值；分数阈值在量表内；S1/S2/S3非负且目标合法；模型输入/输出预算与能力相容；日志目录不能指向媒体或数据库目录；日志保留量不能为非法值；rerank开启时有可用能力profile；embedding查询与索引空间一致；profile引用存在且适配器支持其参数。

配置profile“保存成功”只说明结构与引用合法，不保证远程凭据可用。联网测试是单独显式操作，经过[M13](../modules/provider.md#contract)、计量及模式门控；不能每次保存配置都偷偷调用付费模型。

<a id="source-line-858"></a>

### 11.3 配置命名空间与修改范围

| 命名空间 | 示例内容 | 主要消费模块 |
| --- | --- | --- |
| `deployment.*` | 监听、数据根、秘密来源、数据库连接 | bootstrap、[I01](ownership.md#i01) |
| `runtime.*` | 工作并行、调度权重、入梦收尾预算、恢复策略 | [M02](../modules/runtime.md#contract) |
| `platforms.<id>.buffer.*` | S1/S2/S3、正常触发和空闲尾部策略 | [M03](../modules/buffers.md#contract) |
| `learning.* / prompts.*` | 输入/输出预算、工具步数、学习事件、模板版本 | [M05](../modules/cognition.md#contract) |
| `memory.*` | 双阈值、强化和衰减、遗忘保留期、来源策略参数 | [M06](../modules/memory.md#contract) |
| `retrieval.*` | 一秒基础预算、候选上限、向量短等待、rerank默认开关 | [M08](../modules/retrieval.md#contract) |
| `media.*` | 导入限制、理解策略、GC周期、隔离目录策略 | [M04](../modules/media.md#contract) |
| `state.* / goals.*` | 陈旧时间阈值、去重参数、提醒与时区展示策略 | [M09](../modules/state.md#contract)/[M10](../modules/goals.md#contract) |
| `dream.* / persona.*` | 周期、专注开关、单步预算、外部监管prompt、摘要限制 | [M11](../modules/dream.md#contract)/[M07](../modules/self-model.md#contract) |
| `provider.profiles.* / provider.routes.*` | 协议、模型、能力、secret引用、角色路由 | [M13](../modules/provider.md#contract) |
| `provider.limits.* / provider.pricing.*` | 账户限流、并发、预算、有限尝试、版本化价目 | [M13](../modules/provider.md#contract) |
| `logging.* / audit.*` | 模块等级、sink、轮转、脱敏、日志/审计保留策略 | [M14](../modules/logging.md#contract) |
| `management.*` | 显示、管理会话、配置权限和导出策略 | [M12](../modules/management.md#contract) |

目标的某次截止和提前量是[M10](../modules/goals.md#contract)业务数据；配置里只能放其创建政策或默认建议。外部当前状态属于[M09](../modules/state.md#contract)。Provider预算的已使用数是[M13](../modules/provider.md#contract)状态，不得被“重载配置”归零。

<a id="source-line-878"></a>

### 11.4 热修改的生效类型

“支持热修改”指在正常运行中接受、校验并按正确边界应用参数，而不是所有字段即时覆盖所有运行对象。

| `apply_mode` | 例子 | 行为 |
| --- | --- | --- |
| `IMMEDIATE` | 诊断日志等级、管理显示、发送准入的实时限制 | 下一条事件/下一次准入检查使用新版本；不撤销已经发送的HTTP请求 |
| `NEXT_REQUEST` | rerank开关、检索候选量、普通调用超时/profile路由 | 新请求取得新快照，正在执行的请求继续原快照 |
| `NEXT_BATCH` | 平台三段长度、学习模板、结构化输出预算 | 新批次建立时采用，当前S2快照与轮转用旧参数完成 |
| `NEXT_DREAM` | persona监管prompt、梦境步骤策略、关联整理预算 | 下一轮梦境采用；本轮不在半途切换自我提炼规则 |
| `MIGRATION_REQUIRED` | embedding模型/维度/预处理空间、blob寻址格式 | 先建立受控迁移计划；覆盖/校验通过后切换，不能简单reload |
| `RESTART_REQUIRED` | 数据库文件位置、根存储介质、基本监听、必须重建进程的运行方式 | 保存待重启状态并明确显示，不宣称已在线应用 |

具体键的apply_mode在Schema固定；表中分类是本草稿推荐。涉及多个类别的同一变更必须分成明确activation group或按最严格边界一起生效，Web展示所有子组，不能只显示一个含糊的“保存成功”。

即时限流/凭据禁用等是独立的运行安全策略，可阻止旧快照的后续尝试，不能改写旧批次的目标、模型输入或历史解释。每次操作同时记录语义`config_snapshot_id`与必要的`runtime_policy_revision`，以便解释为什么旧批次在新限额下被暂停。原本已发出的请求不会因为配置改动自动取消、退款或改成另一个模型。

<a id="source-line-895"></a>

### 11.5 变更、准备、发布与恢复

建议的流程：

```text
读取当前有效版本
  → 提交ConfigPatch(expected_revision, actor, reason)
  → Schema/跨字段/能力/权限/专注门控校验
  → 预览差异、成本与迁移影响
  → 持久化候选版本和生效计划
  → 参与模块prepare（预建客户端、日志资源等，不改变当前有效值）
  → 在对应安全边界提交有效指针、计划状态和审计
  → 对新操作发布完整不可变快照
  → 老任务继续固定快照，引用释放后关闭旧资源
```

校验或prepare失败继续使用旧版本，释放未激活资源并记录失败；不能出现配置已对外生效、某个模块还在半初始化。`expected_revision`冲突返回冲突和新基线，不静默覆盖另一管理员的修改。

数据库有效指针、内存对象、文件handler和网络客户端不是一个数据库事务。实现应对新操作的入场设置短激活屏障，用“准备→持久化指针→发布快照”的协议协调；若中间崩溃，重启处于RECOVERING，先按持久化生效状态重建，再放行。某模块无法重建时记录`ACTIVATION_FAILED`并保持受影响能力不可用或执行已审计回退，不能混用新旧半套规则。

即刻/下请求版本、待下批版本和待梦境版本可以同时存在，但每个操作引用的是自己完整一致的快照，且`effective_snapshot_id`能够列出各配置域的版本。通知是唤醒手段，数据库版本是事实；漏收通知时可比较版本恢复，不把一个内存订阅当唯一生效机制。

回退生成一个新的、引用旧内容的配置版本，同样校验、审计并按边界生效；回退配置不回滚已经发生的记忆变化、文件清理、模型调用或费用。仍被任务/账本引用的历史版本必须保留。

<a id="source-line-919"></a>

### 11.6 运行中修改的具体例子

**修改S2长度。** 当前批次固定原长度和来源范围，继续执行原终结规则；下个批次采用新长度。缩小S1/S3不会即时删除正在处理中或仍被来源引用的原始消息，也不对梦境暂存施加普通上限。

**关闭rerank。** 下一个回复准备不发送rerank；已经发出的rerank请求按原deadline及取消契约收尾并如实计费。不能同时重算当次请求的历史配置。

**更新persona监管prompt。** 正常期可保存并计划下次梦境使用；不会立即改写已发布persona。专注期的Web写入仍返回DREAMING，不因支持热修改就绕过原门控。

**更换embedding模型。** [M15](../modules/configuration.md#contract)提交迁移请求，[M08](../modules/retrieval.md#contract)按新的空间建立产物与索引，[M13](../modules/provider.md#contract)在批准预算内执行必要embedding。旧空间继续服务，或按明确降级策略提供本地检索；只有达到批准的覆盖条件后切换有效空间。精确的模型/维度/预处理变动要迁移，密钥、限流和价格变动不重算向量。

**轮换API密钥。** secret版本准备后，新请求使用新引用，旧请求正常完成或按凭据撤销策略中止后续尝试。若密钥来自进程环境，仅在外部shell修改变量不会改变运行进程；UI必须提示更换受支持的秘密来源、更新引用或重启，不能声称任意环境变量都能热更新。

**缩短日志保留期。** 显示即将清理的范围并按权限确认，异步清理；配置回退不能找回已经删掉的历史文件。审计和Provider账本各自执行其独立保留约束，不随运行日志删除。

<a id="source-line-933"></a>

### 11.7 少魔法变量，不把系统规则全部变成开关

策略参数应集中配置：数量、时间、预算、日志级别、路径、模型profile、相似阈值、保留期、调度权重等。默认值在Schema或由同一Schema生成的defaults文件中只有一处定义；示例与测试必须显式注入配置，不隐含替代生产默认值。

协议常量、日志标准等级、状态枚举、来源字段类型、事务全有或全无、只总结S2、深度读取不自动恢复、日志不得给agent读取等属于已确定的不变量。它们应使用有名称的常量、类型和测试，不提供“关闭约束”的普通配置开关。相信量表的语义修改也不是随意数字热改，需要独立版本与存量解释方案。

Schema未批准的数值保持显式待定或示例，不由AI随手填入代码。配置项必须有理由、消费者和验证方式；不把每个局部变量暴露给用户造成不可管理的面板。

<a id="source-line-941"></a>

### 11.8 安全、权限与Web展示

正常管理员可编辑其授权的运行参数；agent只能通过受限配置能力提出或修改允许的学习关注事件等策略，不能更改监管prompt、费用总上限、日志访问权限、secret或部署根路径。普通外部输入不是配置命令。

秘密字段写入后只回显引用/掩码，不进入diff正文、审计明文或下载；秘密值由受保护文件或外部根密钥加密存储，不能把解密密钥与明文一起导出到配置。密钥保管方案需要独立工程验证，配置模块不自行发明密码方案。

Web为每个键显示当前值、默认值、来源/作用域、是否锁定、当前版本、期望版本、生效时点、待迁移/待重启标记和上次错误。配置修改页面提供验证、预览、提交、执行状态、版本历史和受控回退；只读状态在专注梦境中仍可查看。

默认不提供任意代码表达式、任意Python导入、任意文件路径写入、未验证YAML对象构造或无鉴权文件监听式热更新。日志重配置同样走这一流程，不单独开放一个原生logging配置socket。
