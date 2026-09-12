# 统一配置注册表、快照与安全热修改

> 本文件是本主题的现行正文，在此唯一维护。既有要求、已批准契约、建议和待批准事项保持各自状态；迁移不新增产品决定或实现授权。文档关系见[总入口](../INDEX.md)。

适用主题与局部定义：Schema描述参数，revision标识配置版本，snapshot是操作取得的完整不可变视图；生效计划负责让版本在指定边界启用。业务对象和已使用预算不是配置。

设计／审核参考：[冻结原始文档](../reference/companion_memory_module_design_provider_logging_config.md)。仅供追溯，不作为现行约束。

按关联工作联合阅读：[配置模块契约](../modules/configuration.md)；[配置事务](persistence-and-transactions.md#t12)；[参数已定与未定](../product/decisions-and-delivery.md#section-23)；[模式与权限](../modules/runtime.md)；[日志参与者](logging.md#source-line-807)。

已批准契约：[参数定义与只读注册表](#configuration-registry-contract)、[显式解析与不可变快照](#configuration-resolution-contract)、[持久化与审计所需配置校验](#configuration-persistence-validation-contract)。相关输入、支持边界、接口、错误和例子见各节；实现及验收进度只见[STATUS](../work/STATUS.md)。

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

<a id="configuration-registry-contract"></a>

### 11.9 配置参数定义与只读注册表契约

**状态：本切片契约及实现范围已批准。** 用户已批准本节注册、冻结、查询、枚举、六类错误及定义静态校验修订；工具链固定为Python 3.12，由uv管理，使用标准库decimal.Decimal与unittest，不增加第三方依赖。本文是当前有效契约；被替代的条款不再用于实现或验收。决定编号仅用于文档追踪，不进入源码、测试或注释。功能完成度以实际工作记录和测试结果为准，不因批准而宣称已实现完整配置模块。

<a id="configuration-registry-evidence"></a>

#### 11.9.1 依据、确定程度与范围

| 依据 | 当前切片承担的要求 | 不扩大到的职责 |
| --- | --- | --- |
| 本文M15、§11.1–11.2 | 参数元信息完整可追溯、唯一键、未知键拒绝、统一定义来源 | 不接受运行时有效配置值或提供加载旁路 |
| 本文§11.7及代码规范 | 唯一默认来源、示例显式注入、理由／消费者／验证方式；不变量不作为普通配置开关 | 不填入未批准生产参数或默认值 |
| 本文§5.3、§11.8 | 权限只作声明，秘密不通过错误泄漏 | 不执行权限判定、秘密解析、日志或Provider服务 |
| 本文T12、§11.4–11.6 | 定义冻结与配置版本／快照／激活严格区分 | 不履行持久化、热修改、恢复或跨参数业务校验 |
| 用户批准的静态校验修订 | 六类声明类型、顶层nullable、默认自洽、类型敏感枚举、基本数值范围、无级联错误 | 不执行运行时解析、远程检查或依赖求值 |

仅实现参数定义、注册、冻结、查询、枚举及静态校验和相应测试。没有配置持久化、环境／文件加载、有效配置解析、完整权限系统、跨参数业务校验、热修改、Web、Provider、日志系统或数据库迁移，也不建立其他模块占位骨架。旧的“仅结构检查且不核对默认自洽”提案及其允许错误默认的例子已被批准修订替代，历史过程只留在工作记录中。

<a id="configuration-registry-data"></a>

#### 11.9.2 数据定义

区分四个概念：`ParameterDefinitionInput`是定义方提交的元信息；`ParameterDefinition`是注册表取得所有权后的深不可变定义；`RegistryBuilder`只负责收集定义；`ReadOnlyRegistry`只提供冻结集合的查询。它们都不是配置有效值、`ConfigSnapshot`或持久化revision。

基础表示如下：

| 记法 | 定义 |
| --- | --- |
| `Identifier` | 非空且不含空白字符的文本；按Unicode码点精确相等，不修剪、不改大小写、不做Unicode归一化。此处不增加生产命名空间语法或别名 |
| `Text` | 至少包含一个非空白字符的说明文本；保留原内容，不隐式清洗或补写 |
| `MetadataValue` | 仅含空值、布尔、整数、有限十进制数、文本、有限序列、文本键映射的无环数据树；映射键唯一。序列元素和映射值可递归嵌套上述种类，其他种类均不支持。布尔与整数保持不同种类；不做字符串转数值。明确拒绝未支持的自定义对象、循环值结构、回调、表达式、动态导入和惰性迭代器；不调用任意对象序列化钩子，不建设通用冻结框架。Python输入仅支持精确的内建bool、int、str、list、tuple、dict，以及None和decimal.Decimal，不接受其自定义子类；十进制必须有限。注册后数组为tuple、对象为自有映射的只读视图，保留逻辑类型 |
| `Declared(T)` / `NotApplicable(reason)` | 显式声明T，或用非空说明解释该字段不适用；不适用不是待定。仅下表允许的字段可用此表示，不能给所有字段统一填空 |
| `NoDefault` / `LiteralDefault(MetadataValue)` | 前者明确没有默认值；后者是Schema中唯一声明的默认元信息。`LiteralDefault(空值)`与`NoDefault`不同；两者都不同于遗漏字段或“待批准” |
| `IdentifierList` | 显式给出的有限标识序列；不重复，保留提交顺序；允许空序列的字段在下表单独说明。注册后序列不可变 |

定义输入完整提供下列字段（新增必填nullable），未知结构字段拒绝。输入采用ParameterDefinitionInput类型化字典，运行时接收精确的内建dict以便缺失字段由注册接口返回定义错误；输出ParameterDefinition及标记对象使用不可变记录。只有明确的NoDefault、不适用标记和允许为空的列表可表达相应缺省状态，不能省略字段或统一用空值代填。

| 字段 | 表示与结构条件 | 语义和边界 |
| --- | --- | --- |
| `key` | `Identifier` | 参数唯一键；实例或平台实际覆写值不存于此 |
| `owner_module` | `Identifier` | 维护模块；自报此字段不证明调用方拥有该模块权限 |
| `schema_revision` | `Identifier` | 定义所属Schema修订；不解释为自增整数、时间或SemVer，不等于配置值revision |
| `type` | 六种固定标识 | boolean、integer、decimal、string、array、object；其他声明类型拒绝 |
| `default` | `NoDefault`或`LiteralDefault` | 定义方显式声明；不从环境、文件、其他参数或调用方缺省参数补值 |
| `required` | 显式布尔 | 未来解析后的有效配置是否必须具有该项值，不强制调用方显式填写；本切片不执行解析 |
| `nullable` | 显式布尔 | 仅控制参数顶层能否为空；不限制array／object内部MetadataValue的空值 |
| `unit` | `Declared(Identifier)`或`NotApplicable(Text)` | 明确单位或说明无量纲；不自动换算 |
| `range` | `Declared(RangeDescriptor)`或`NotApplicable(Text)` | RangeDescriptor(lower, upper)，每界为Unbounded或Bound(value, inclusive)；仅integer／decimal适用；边界类型、有限性、顺序与非空区间均须检查 |
| `enum` | `Declared(非空MetadataValue序列)`或`NotApplicable(Text)` | 逐项校验类型、nullable和适用范围；按类型敏感深度相等拒绝重复，顺序保留；启用枚举时默认值必须属于允许项 |
| `validator` | `IdentifierList`，可为空 | 验证器标识；空列表明确无附加验证器。不存函数、不加载或调用验证器 |
| `dependencies` | `IdentifierList`，可为空 | 所依赖参数的完整键；可前向声明。仅在冻结时核对键存在，不执行跨参数约束 |
| `scope` | 非空`IdentifierList` | 声明允许的作用域种类；不创建平台、角色或profile实例 |
| `override_policy` | `Identifier` | 显式声明覆写策略；不据此实现继承、优先级或部署锁定 |
| `sensitivity` | `Identifier` | 显式声明敏感级别；不在本节另定级别枚举或秘密来源 |
| `read_roles` / `write_roles` | 各为`IdentifierList`，可为空 | 空列表只表示未列出任何角色，不隐含公共权限；此接口不做身份认证、授权计算或角色继承 |
| `apply_mode` | `Identifier` | 生效方式元信息；不把§11.4推荐分类升级为封闭类型或自动为具体键分配类别 |
| `activation_group` | `Declared(Identifier)`或`NotApplicable(Text)` | 协调组声明；不执行准备、屏障或激活 |
| `cost_impact` / `migration_impact` | 各为`Text` | 必须说明影响或明确无影响及理由；不估算费用、不生成迁移计划 |
| `description` | `Text` | 解释参数用途；不能只有文档编号 |
| `deprecated` | 显式布尔 | 废弃声明；不自动屏蔽键或转换输入 |
| `replacement` | `Declared(Identifier)`或`NotApplicable(Text)` | 替代路径元信息；不执行重命名、别名查询或兼容迁移，也不按dependencies规则解析 |
| `upgrade_rule` | `Declared(Text)`或`NotApplicable(Text)` | 兼容／升级说明；不接受可执行迁移脚本 |
| `rationale` / `consumers` / `validation_method` | `Text`／非空`IdentifierList`／`Text` | 分别落实§11.7的理由、消费者和验证方式；消费者可与维护模块不同 |

不把“待批准”作为可注册的运行时占位类型。生产参数的类型、默认值或策略尚未决定时，仍留在设计待定项，不能填`NoDefault`、空值、空列表或`NotApplicable`伪装已决定。定义结构通过也不证明该生产参数已经获批准；批准记录由文档审查承担，不增加运行时审批系统。

默认元信息若存在只在定义中声明一次，其他读取只是该声明的只读视图；本切片不另建defaults文件。`required=true`配显式默认与`required=true`配`NoDefault`均可描述，注册表不替未来配置求值作决定。真实秘密、带凭据内容和业务状态不得塞进默认值或说明；这来自既有边界，结构检查本身不能识别伪装成普通文本的秘密，也不承担秘密扫描服务。

<a id="configuration-registry-validation"></a>

#### 11.9.3 校验与依赖边界

注册成功前检查定义结构、支持类型、默认存在状态，以及显式默认值与类型、nullable、枚举和基本范围的一致性。校验全部成功才取得独立、深不可变的定义；任何失败不修改集合或原输入。不作隐式类型转换。

| 声明类型 | Python顶层非空值 | 约束 |
| --- | --- | --- |
| boolean | 精确bool | 不以整数代替 |
| integer | 精确int | 不接受bool |
| decimal | 精确decimal.Decimal | 有限且精确，不隐式接受整数、float或数字字符串；比较不依赖舍入或尾零表示 |
| string | 精确str | 允许空字符串及纯空白；说明性Text仍要求非空白字符 |
| array | 精确list或tuple | 内部递归MetadataValue，无嵌套元素Schema |
| object | 精确dict，键为精确str | 内部递归MetadataValue，无嵌套字段Schema |

`default`缺失是定义错误；`NoDefault`明确无默认，不要求调用方显式提供未来配置值。`LiteralDefault(None)`仅在nullable为真且枚举等约束允许时合法。数值range约束非空数值，空值不作端点比较；nullable和枚举单独控制其合法性。array／object内部的空值不受顶层nullable限制。

枚举使用类型敏感的深度相等：不同逻辑类型不相等；Decimal按数值比较，忽略无意义尾零；字符串精确比较；序列按顺序逐元素比较；映射忽略排列顺序，键和值均精确比较。拒绝重复成员，不去重；全部成员均须符合声明类型、nullable及适用range。启用枚举时，空值必须明确列为成员才可作为合法默认值。

range仅适用于integer和decimal，其他类型必须使用NotApplicable。RangeDescriptor包含lower和upper，每界为Unbounded或Bound(value, inclusive)，inclusive为显式布尔。整数边界只接受int而非bool，十进制边界只接受有限Decimal；至少有一界。检查顺序和非空区间；相等边界只有两端闭合才非空，整数范围还须按端点开闭计算实际可取整数，不能把(1, 2)视为非空整数区间。不实现长度约束，不以Decimal上下文舍入改变边界。

结构或类型等前置检查失败时跳过依赖它的检查；例如不支持的type不引发默认类型、范围适用性或枚举成员的级联判断；无效range不再用于默认／枚举越界判断；无效枚举不再用于默认成员判断。其他独立可检查字段继续检查；错误按字段及下标顺序报告。

validator、作用域、角色、覆写策略和生效方式仅保存声明，不加载执行器或核验外部服务。dependencies也仅为参数键声明，冻结检查引用存在，允许前向引用、自引用及关系环，不求值、不推导执行顺序；这不验证未来求值规则。循环值结构仍拒绝。冻结失败保留原集合与BUILDING态，补齐定义可再次冻结。

不执行运行时配置解析、权限判定、跨参数业务约束、远程检查或历史升级规则；静态自洽不能被宣称为有效配置已解析、已激活或完整配置系统已完成。

<a id="configuration-registry-lifecycle"></a>

#### 11.9.4 注册、所有权与冻结

生命周期只有`BUILDING`与`FROZEN`。构建器由可信的程序装配方持有，各模块显式提交自己拥有的定义；消费者只取得冻结后的只读接口。不作导入即注册、全局单例发现或运行中插件追加。

| 当前状态／操作 | 结果 | 失败及既有内容 |
| --- | --- | --- |
| 创建构建器 | 得到空的`BUILDING`构建器 | 不读取环境、文件、数据库或网络 |
| `BUILDING`注册新键 | 全部静态校验与深不可变化完成后，加入一项自有定义 | 任何预期失败都不加入部分内容；调用方原输入不被修改 |
| `BUILDING`重复注册 | 拒绝；内容、维护模块或Schema修订是否相同均不改变结果 | 保留首次成功注册的定义；不覆盖、不合并、不作为幂等成功 |
| `BUILDING`冻结 | 依赖键核对全部成功后固定完整集合，返回只读注册表并进入`FROZEN`；允许空集合 | 依赖缺失则仍为`BUILDING`，不交出只读注册表；可补注册依赖后重新冻结 |
| `FROZEN`再次冻结 | 返回同一逻辑内容的只读注册表 | 幂等；不承诺句柄对象身份相同 |
| `FROZEN`继续注册 | 拒绝任何键，包括原键和新键 | 内容保持不变，不解冻 |
| 冻结后查询 | 只读取完整定义集合 | 无更新、删除、重命名或撤销注册接口 |

在**每次注册成功前**完成嵌套数据的所有权隔离，不能等最终冻结才复制；因此注册后、冻结前修改原输入也不能污染定义。定义、默认元信息、范围描述、允许项、角色列表、依赖列表，以及查询和枚举返回的所有嵌套容器均不可经公开接口修改。只给最外层只读映射、底层仍共享调用方可变列表不满足本契约；只返回可变副本也不满足返回定义本身只读的要求。采用不可变记录、tuple及自有字典的只读视图；公开赋值或容器修改由Python拒绝，不固定修改操作的具体异常类别。

构建期仅支持装配方串行调用，输入在调用期间不得并发修改；不承诺并发注册或注册与冻结竞争。冻结对象通过语言支持的安全发布机制交给消费者后，可并发只读访问；此处不指定锁、线程库或进程共享方案。普通只读接口不是同进程恶意代码沙箱，也不是给Web或agent的权限边界。

修改Schema需要另建定义集合，但新集合如何替换运行中的服务不属于本切片；不提供替换或激活入口。注册／冻结成功只确认本地内存状态，进程退出后不作恢复承诺，不能用于声称T12已完成。

<a id="configuration-registry-ports"></a>

#### 11.9.5 公开接口

以下为公开签名；`Result<T, RegistryError>`表示成功或可识别的预期错误二选一，`Unit`表示操作成功且无数据载荷。Python以Ok(value)或Err(error)表示两个分支，Unit为None；预期失败不抛业务异常，也不是HTTP协议。

| 接口 | 前置条件与输入 | 成功输出／副作用 | 预期失败与重复调用 |
| --- | --- | --- | --- |
| `create_registry_builder() → RegistryBuilder` | 显式由装配方创建，无默认参数 | 新的独立空构建器；无外部副作用 | 无领域错误；每次创建独立集合，不复用全局注册状态 |
| `RegistryBuilder.register(definition) → Result<Unit, RegistryError>` | `BUILDING`；完整定义输入；串行调用 | 内存加入一项自有不可变定义 | 非法输入、重复键或已冻结；失败集合不变；重复键总是失败 |
| `RegistryBuilder.freeze() → Result<ReadOnlyRegistry, RegistryError>` | 串行调用；无输入配置值 | 首次成功固定定义集合；后续成功返回等价只读内容 | 缺失依赖则不冻结；已冻结调用幂等 |
| `ReadOnlyRegistry.get_definition(key) → Result<ParameterDefinition, RegistryError>` | 有效`Identifier`，按原键精确查询 | 一个深不可变定义；无副作用 | 键格式非法或未知键；不接受fallback/default参数，不返回空定义 |
| `ReadOnlyRegistry.list_definitions() → 不可变定义序列` | 已取得只读注册表；无筛选或分页参数 | 按key的Unicode码点字典序返回全体深不可变定义；空集合返回空序列 | 无领域错误；重复查询内容与顺序一致，不承诺对象身份 |

只读接口在冻结成功之前不存在；不添加可观察半成品的查询端口。上述接口不返回当前值、有效值、配置版本、生效状态或持久化收据。M15原有`get_schema`等建议端口不会因本表而自动获准；本表也不提供`validate_patch`、提交、回退、订阅、导出、联网验证等空成功入口。

<a id="configuration-registry-errors"></a>

#### 11.9.6 错误契约

所有预期失败用`RegistryError`分支返回：`code`为下表语义码，`operation`为公开操作名，`issues`为非空不可变问题序列；每项仅有结构路径`field_path`与固定原因码`reason`；所有固定原因码统一使用`UPPER_SNAKE_CASE`，不接受旧小写值，不提供兼容转换。不携带原输入、默认值、秘密、堆栈或任意对象字符串，不将用户提交的映射键拼入错误路径。可用固定、可读语句说明违反的静态规则、操作未完成且集合未变；规则说明不拼接输入全文、默认值正文、密钥或任意对象的完整表示。这不增加任意诊断载荷字段。预期错误无外部副作用，不产生“部分成功”或远程结果未知；内存耗尽等运行时故障不伪装成领域错误或成功。

| `code` | 触发条件 | 调用方处理与状态保证 |
| --- | --- | --- |
| `INVALID_DEFINITION` | 必填字段遗漏、未知结构字段、标识非法、表示不符、不支持的元信息对象、有环输入或定义静态自洽错误 | 修正定义后显式重试；注册集合与调用方输入不变 |
| `DUPLICATE_PARAMETER` | 在构建期提交已注册的精确键 | 修正重复声明来源；旧定义保留，不按Schema修订覆盖 |
| `REGISTRY_FROZEN` | 冻结成功后调用注册 | 不能重试修改此集合；不开放解冻或更新 |
| `UNRESOLVED_DEPENDENCY` | 尚未成功冻结，执行冻结检查时有依赖参数键不在集合 | 保持构建态，补齐定义后显式再次冻结；不自动补值或隐式跳过 |
| `INVALID_PARAMETER_KEY` | 查询键不是有效标识 | 修正键后重试；无查询回退 |
| `UNKNOWN_PARAMETER` | 有效查询键未注册 | 调用方处理缺失；不生成默认定义、不尝试别名 |

错误优先级固定：注册先检查是否已冻结，再检查key结构，再查重复键，最后检查其余字段；因此已冻结的坏输入返回`REGISTRY_FROZEN`，构建期重复键即使其他字段坏也返回`DUPLICATE_PARAMETER`。key结构错误归入`INVALID_DEFINITION`。其余定义问题按本节字段表顺序及容器下标顺序报告；未知字段只报告固定原因，不回显名称。查询先检查键结构再查存在性。冻结按注册键排序、再按依赖提交顺序报告所有缺失引用。

结构及操作原因码固定为：`MISSING_FIELD`、`UNKNOWN_FIELD`、`INVALID_IDENTIFIER`、`INVALID_SHAPE`、`DUPLICATE_IDENTIFIER`、`UNSUPPORTED_VALUE`、`CYCLIC_VALUE`、`DUPLICATE_KEY`、`REGISTRY_FROZEN`、`MISSING_DEPENDENCY`、`UNKNOWN_KEY`。问题路径只含本契约公开字段名和序列下标；冻结问题用排序后定义下标与依赖下标定位，嵌套映射问题指向该映射字段而不回显键。先发现不支持的节点或环即停止遍历该子树，其他可独立检查的字段继续；码外的具体语句不作为稳定协议。修改返回对象的尝试由所选语言的不可变类型拒绝，不假定它会通过注册表接口返回`RegistryError`。

静态校验原因固定为：UNSUPPORTED_DECLARED_TYPE、NULL_NOT_ALLOWED、TYPE_MISMATCH、NON_FINITE_NUMBER、RANGE_NOT_APPLICABLE、INVALID_RANGE、OUT_OF_RANGE、EMPTY_ENUM、DUPLICATE_ENUM_MEMBER、NOT_IN_ENUM，均归入INVALID_DEFINITION。前置条件失败不产生依赖其结果的错误；不新增顶层错误类别。

<a id="configuration-registry-examples"></a>

#### 11.9.7 验收例子

以下是当前有效的验收预期，实际是否通过以测试结果为准；不证明完整M15、V73/V74或T12完成。夹具均为显式合成数据，不注册生产参数或生产默认值。

| 字段组 | 显式夹具内容 |
| --- | --- |
| 身份与类型 | key=`demo.label`；owner_module=`demo_owner`；schema_revision=`demo_revision`；type=`string` |
| 默认与约束 | default=`NoDefault`；required=`true`；nullable=`false`；unit=`NotApplicable(文本标签无量纲)`；range=`NotApplicable(文本标签不使用数值范围)`；enum=`Declared(["alpha", "beta"])`；validator=`[]`；dependencies=`[]` |
| 范围与权限 | scope=`[demo_instance]`；override_policy=`demo_no_override`；sensitivity=`demo_public`；read_roles=`[demo_reader]`；write_roles=`[]` |
| 生效与影响 | apply_mode=`demo_next_operation`；activation_group=`NotApplicable(示例没有协调切换)`；cost_impact=`示例不发起调用，无费用影响`；migration_impact=`示例不持久化，无数据迁移影响` |
| 说明与兼容 | description=`供合成消费者展示的标签`；deprecated=`false`；replacement=`NotApplicable(示例无替代键)`；upgrade_rule=`NotApplicable(示例不涉及历史升级)` |
| 理由与使用 | rationale=`验证完整参数元信息可查询`；consumers=`[demo_consumer]`；validation_method=`核对定义静态合法性、完整往返及只读行为` |

| 场景 | 预期结果 |
| --- | --- |
| 完整往返 | 注册完整夹具、冻结后逐字段查询，定义无有效配置值或激活状态 |
| 缺失与无默认 | 遗漏default或nullable为INVALID_DEFINITION，reason为`MISSING_FIELD`；NoDefault不冒充缺失；required为真不强制显式默认 |
| 六类声明类型 | 分别接受对应精确载体；integer拒绝bool，decimal拒绝int／float／数字字符串；string接受空白 |
| 空值 | nullable为假拒绝顶层None；nullable为真且枚举允许才接受；数组／对象内部空值不受顶层nullable限制 |
| 默认自洽 | string声明搭配数值默认失败；默认不属于枚举或超出有效range失败 |
| 枚举相等 | 重复整数、Decimal尾零等价、递归等价数组／不同排列映射均拒绝；不同逻辑类型不误判相等 |
| 枚举合法性 | 空枚举、类型错误／空值不允许／越界成员均失败；无效枚举不触发默认NOT_IN_ENUM级联错误 |
| 数值范围 | 至少一界；边界类型精确且有限；拒绝倒置、空区间和整数(1, 2)；极大整数及高精度Decimal不受舍入改变 |
| 前置失败 | 不支持类型、坏边界或坏枚举仅报告有依据的错误；独立字段错误继续收集 |
| 值结构安全 | 自定义对象、回调、循环值结构、非有限Decimal、非文本映射键拒绝；不调用对象序列化或完整表示 |
| 输入隔离与深只读 | 注册后修改原始列表／映射不改变内部定义；所有查询／枚举及其嵌套内容只读 |
| 精确键与重复 | 同键内容／owner／revision相同或不同都拒绝并保留首次定义；大小写及Unicode归一化不同键不合并 |
| 查询与排序 | 构建期无查询／枚举接口；冻结后未知键明确失败，枚举按Unicode码点排序 |
| 依赖与重试 | 缺失引用冻结失败且原集合不变，可补齐后重试；前向引用、自引用及声明关系环只记录 |
| 空集合与重复冻结 | 空集合可冻结、枚举为空；重复冻结内容等价，不保证句柄相同；冻结后注册一律失败 |
| 优先级与错误安全 | 已冻结优先于坏输入；key结构优先于重复，重复优先于其余字段；错误仅含安全路径／类别／规则，不含输入值；固定reason均为`UPPER_SNAKE_CASE` |

<a id="configuration-registry-decisions"></a>

#### 11.9.8 批准范围与停止点

D1–D7的公开表示、默认与静态校验、精确键和重复策略、深不可变、构建与冻结、五项能力和六类错误已由用户批准；D2以本节当前静态自洽规则替代原排除条款。新增nullable、六种固定声明类型、枚举深度比较、数值范围和十种静态原因均为已批准内容。Python 3.12、uv管理和标准库最小测试方式已确定。

实现只位于companion_memory/configuration及相应tests目录，根包标记仅用于导入；uv项目不发布安装包，不引入第三方依赖。内部容器选择不改变类型语义、输入隔离和深不可变保证。真实生产配置项、配置解析、持久化、权限执行、热修改及其他模块不属于本次批准范围。

完成实现与真实检查后，将当前任务标为“实现完成，待验收”，报告文件、公开接口、测试命令与结果和残余限制；不标记整个配置模块完成，不自动提交、推送、合并、部署或开始下一任务。

<a id="configuration-resolution-contract"></a>

### 11.10 显式配置值解析与不可变有效快照：最小契约

**状态：契约已批准。** 实现与验收进度见[STATUS](../work/STATUS.md)，本节不提供新的执行授权。 用户已批准main／`d042e1a`工作区中本节现稿的完整条款，包括S1–S6、缺失组合表、支持子集、公开接口、所有权、错误优先级及合成验收条件，集中见[已批准决定表](#configuration-resolution-decisions)。§11.9及其注册表验收事实不变；其“本切片”仍指注册表切片。本节的“有效”仅指通过下述受限解析规则的完整内存结果，不表示配置值已批准、已持久化或已对运行任务激活。

<a id="configuration-resolution-evidence"></a>

#### 11.10.1 已有依据与本次边界

| 已有要求或事实 | 本契约如何承接 | 本次已批准的新增部分 |
| --- | --- | --- |
| §11.1、§11.7：统一配置入口、唯一默认来源、显式合成输入 | 只从冻结注册表取定义及默认值，只接收调用方显式提交的内存值 | 单层dict输入、逐键选择及结果表示 |
| §11.9.2–11.9.3：required约束解析后存在性；nullable仅约束顶层；六种类型、精确载体、范围与枚举语义已批准 | 沿用这些值语义，不把required改为“必须显式填写”，不放宽注册表校验 | 将既有值规则应用到显式输入，以及缺失／失败的解析协议 |
| §11.2、§11.9：未知键拒绝、精确键、定义深不可变；依赖冻结只检查引用存在 | 以原注册表为唯一完整定义集合；不重新注册或改写定义 | 解析支持子集、快照绑定、查询及错误顺序 |
| §11.4–11.5、T12：任务固定快照；激活、持久化与恢复另有承诺 | 新结果不改写旧结果，不更改任何当前有效指针 | 本地快照接口暂不提供持久标识或激活状态 |
| §15.1：前期包含类型化Schema／快照，安全热发布与迁移后续开展 | 本契约作为注册表之后的独立小切片设计 | 不因此授权同阶段日志、Provider、I01或后续切片实现 |

契约形成时的静态审查基线为main／`d042e1a`（历史实现观察，当前进度见STATUS）：现有configuration源码及相关测试提供定义、校验、注册／冻结和只读查询；没有配置值解析或快照接口。现有`Err`及`Result`限定于`RegistryError`，不能把新增错误直接当成既有注册表错误。源码中默认值已转为不可变载体，后续解析须区分可信冻结默认与外部原始输入，不能因默认对象已是只读映射而误拒绝它。

本节只依赖成功冻结的注册表和显式内存输入，不读取文件、环境、数据库、时间、随机源或网络；不调用validator、日志、Provider、秘密提供器或其他模块。没有生产参数、加载层、权限引擎、变更补丁、热发布、订阅、回退、迁移或恢复接口。以下规则已批准；后续实现工作仍须用户明确授权。

<a id="configuration-resolution-input"></a>

#### 11.10.2 输入、取值与缺失组合（已批准）

输入为`resolve_configuration(registry, explicit_values)`，两项均须显式提供。registry必须是§11.9成功冻结所得的原生`ReadOnlyRegistry`；构建器、鸭子类型对象及自定义子类均不接受，也不隐式调用freeze。explicit_values仅接受精确内建`dict`：键为§11.9的精确`Identifier`，值为原始`MetadataValue`。不接受通用Mapping、只读映射、键值对序列、JSON文本、惰性迭代器、回调或自定义子类；类型准入使用身份判断，不触发自定义类型的比较、哈希或转换钩子。

该dict是一份完整提交的单层配置值集合，键中的点只是原键字符；不展开嵌套命名空间。object参数内部的dict是一个整体值，不是配置补丁。重复键在dict构造前若已被覆盖，接口无法追溯或承诺检测；本接口不接收含重复键的文本格式。未知顶层键全部拒绝；object内部没有字段Schema，其任意精确str键（包括空字符串）不按注册键检查。

选择顺序唯一为：键存在时用显式值；键不存在且定义为LiteralDefault时用该默认；否则按required决定失败或保留缺失。显式输入不合法时整次失败，不以默认掩盖。None是显式空值，NoDefault及本节缺失标记均不是可提交的配置值；删除某键只表示本次未提供，该调用不继承上次结果。

下表穷尽未提供键时的required × nullable × 默认形态。“非空”仅指不是None，包含合法空字符串和空容器；非空默认d须已满足注册表类型、范围与枚举规则。“注册即拒绝”不是本解析器的新错误。

| required | nullable | NoDefault | LiteralDefault(d)，d非空 | LiteralDefault(None) |
| --- | --- | --- | --- | --- |
| false | false | MissingValue | PresentValue(d, DEFAULT) | 注册即拒绝，不能成为合法冻结输入 |
| false | true | MissingValue | PresentValue(d, DEFAULT) | PresentValue(None, DEFAULT)，枚举须允许 |
| true | false | REQUIRED_VALUE_MISSING | PresentValue(d, DEFAULT) | 注册即拒绝，不能成为合法冻结输入 |
| true | true | REQUIRED_VALUE_MISSING | PresentValue(d, DEFAULT) | PresentValue(None, DEFAULT)，枚举须允许 |

下表覆盖键已提供时的组合；required及默认的所有合法组合均不改变结果。

| 显式值 | nullable=false | nullable=true |
| --- | --- | --- |
| None | NULL_NOT_ALLOWED | 枚举未启用或包含None时为PresentValue(None, EXPLICIT)，否则NOT_IN_ENUM；不比较数值范围 |
| 合法非空值v | PresentValue(v, EXPLICIT) | PresentValue(v, EXPLICIT) |
| 非空但类型／结构／范围／枚举非法 | 按§11.10.5失败 | 按§11.10.5失败 |

因此required=true、nullable=true可由合法None满足；nullable=true不会自动补None。false、0、Decimal("0")、空字符串、纯空白字符串、空数组和空对象均是已提供的值，按各自类型与约束检查，不被当作缺失。数组／对象内部None不受顶层nullable限制。

显式值沿用§11.9.3六种载体及类型敏感枚举相等规则：bool不充当int；decimal只收有限Decimal；不做字符串转数值、int转Decimal、float转换、单位换算、修剪、大小写处理、Unicode归一化、数组拼接或对象合并。保留Decimal精确数值，不按当前上下文舍入；不承诺序列化后的字节表示。唯一表示变化是所有权隔离后数组变tuple、对象变自有只读映射，逻辑类型不变。支持有限无环数据树及共享的无环子树；拒绝非有限Decimal、自定义对象、循环、非str映射键及所有未支持载体，不隐加长度、嵌套字段或生产数值约束。

<a id="configuration-resolution-support"></a>

#### 11.10.3 未实现语义的支持与拒绝边界（已批准）

解析前检查**整个冻结集合**是否处于下表支持子集，包含本次未提供且可缺失的键；不剔除不支持的定义来取得部分成功。这是新解析器的能力限制，不改变§11.9允许注册的元信息或冻结规则。

| 声明／能力 | 本切片处理 | 理由与后续边界 |
| --- | --- | --- |
| type、default、required、nullable、range、enum | 支持上述解析和单参数值校验 | 定义静态合法不等于显式输入合法 |
| validator | 必须为空；非空一律拒绝 | 没有执行器，不能把跳过附加校验标为有效 |
| dependencies | 必须为空；非空一律拒绝，包括自引用、前向引用及关系环 | 冻结只保证键存在；不臆定“依赖值须存在”或推导求值顺序 |
| scope | 仅接受单项`("instance",)` | 本次只解析一份实例值集合；多作用域、其他标识或实例选择请求不支持 |
| override_policy | 仅接受`"no_override"` | 只支持显式值与Schema默认的选择；不支持平台／角色／profile／sink继承、部署锁定及多来源覆写 |
| sensitivity | 仅接受`"public"`，其他标识拒绝 | 本接口没有秘密解析或脱敏出口；public也不证明文本不含秘密，调用方不得提交秘密 |
| deprecated、replacement、upgrade_rule | 分别须为false、NotApplicable、NotApplicable，否则拒绝 | 本次不解释历史版本、别名、替代或升级规则 |
| unit、owner_module、schema_revision、consumers及说明性字段 | 保留定义，可查询；unit不作换算 | 标识和说明不证明权限、外部存在性或服务能力 |
| read_roles、write_roles | 保留声明，不执行授权，也不因空列表授予公共访问 | 只供可信内部装配方及其显式交付的消费者使用；不得作为Web／agent配置读取入口 |
| apply_mode、activation_group、cost_impact、migration_impact | 保留声明，不执行生效、协调、费用估算或迁移；即使声明需重启／迁移也只得到本地解析结果 | 解析无当前运行状态，不能回答何时可启用；§11.4–11.5与T12承诺仍全部在后续 |

`instance`、`no_override`、`public`这三个精确标识是本节的解析准入约定，**已批准**，不是对注册表新增封闭枚举或生产Schema批准。任意其他标识（包括既有合成夹具的demo／sample标识）不得被猜测映射为这些语义；注册成功但解析不支持是明确可观察的失败。对真实业务需执行的跨字段约束，定义方仍须如实声明validator／dependencies；不可清空它们以绕过本子集限制。

接口不接收scope选择器、分层来源、actor、秘密解析器、validator回调或expected_revision等额外参数；不提供返回空成功的占位端口。Python调用缺少实参或添加未知关键字属签名使用错误，按语言调用规则拒绝，不伪装领域成功。

<a id="configuration-resolution-snapshot"></a>

#### 11.10.4 输出、公开接口、所有权与标识（已批准）

新增公开结果为`ResolutionResult<T> = ResolutionOk(value: T) | ResolutionErr(error: ResolutionError)`，均为深不可变记录；不改变已有Ok／Err／Result及RegistryError协议。新增记录、类型和下表接口由配置包公开入口导出；不存在公开构造有效快照、修改、刷新、补丁或回退的入口。

| 公开类型／记录 | 字段与含义 |
| --- | --- |
| MissingValue | 无字段；仅表示已注册、非required、无默认且本次未提供；没有value或source字段 |
| PresentValue | value为深不可变值（可为None）；source只为`EXPLICIT`或`DEFAULT`，是选择来源，不是权限或部署优先级 |
| SnapshotEntry | definition为绑定注册表中的深不可变ParameterDefinition；state为MissingValue或PresentValue；由definition.key、type、schema_revision解释该值 |
| EffectiveSnapshot | 只读绑定注册表与完整条目集合，经成功解析取得；每个注册键恰好一条，允许条目明确缺失，不存在未检查键 |

| 公开接口 | 成功输出／保证 | 预期失败 |
| --- | --- | --- |
| `resolve_configuration(registry, explicit_values) → ResolutionResult<EffectiveSnapshot>` | 完整检查及隔离后一次返回新结果；不改变注册表、旧快照或任何全局指针 | §11.10.5定义的输入、能力或值错误；不返回部分快照 |
| `EffectiveSnapshot.get_registry() → ReadOnlyRegistry` | 返回本次解析传入的同一个冻结注册表句柄；不另查最新Schema | 无领域错误 |
| `EffectiveSnapshot.get_entry(key) → ResolutionResult<SnapshotEntry>` | 精确键查询；已注册但缺失时成功返回MissingValue条目；显式None是PresentValue | INVALID_PARAMETER_KEY或UNKNOWN_PARAMETER；不接受fallback/default参数 |
| `EffectiveSnapshot.list_entries() → tuple[SnapshotEntry, ...]` | 按definition.key的Unicode码点字典序列出全体条目，包括MissingValue；空注册表配空dict可成功且返回空tuple | 无领域错误；无筛选、分页或动态求值 |

每次resolve均从所传完整注册表与本次dict重新解析，两个独立注册表即使键及schema_revision相同也不混用；快照永久绑定其输入句柄。修改Schema仍须另建注册表，不能将旧快照重新绑定。重复相同输入的条目内容、来源与顺序相同；不承诺快照／条目对象身份、缓存命中、去重或对象哈希／相等运算协议。

本切片**不新增snapshot_id、config_snapshot_id、effective_snapshot_id、配置值revision或registry_id字段，也不接受调用方自报这些ID**。schema_revision仍只是每项定义原有的不透明Schema标识，可不同、不可排序，不是整份注册表或配置内容的唯一身份。绑定可通过get_registry核对，定义修订可逐条读取；本地对象身份不能写入持久账本充当版本。§11.4–11.5要求的持久快照标识、各域配置版本清单、runtime_policy_revision及恢复解释能力需后续单独设计；本快照不能冒充该完整协议。已批准暂缓ID而不引入内容哈希规范、全局计数器、时钟、随机源或持久版本库。

成功返回之前取得所有显式嵌套可变值的独立所有权；修改原dict、list或其任意深层对象不会改变结果。数组、对象、条目、状态、定义、查询／枚举结果及错误都不可经公开赋值或容器操作修改；不以“返回可变副本”代替输出不可变。可共享注册表已经自有且深不可变的默认与定义，无须把它们当外部dict再导入。共享输入子树不保证输出别名身份；不得残留调用方可变引用。拒绝修改使用语言不可变机制，不固定其异常类别。

任何预期失败均只返回ResolutionErr；原输入、注册表及已有快照保持原样，无部分成功、外部写入、激活或补偿动作。修正输入后须显式再次调用。输入在一次调用期间由调用方保持稳定；不承诺与并发修改输入竞争。安全发布后的快照允许并发只读；不规定私有锁或容器实现，也不声称能防同进程恶意反射。内存耗尽等非预期运行故障不伪装领域错误或成功；无成功返回就没有取得快照的承诺。进程退出后不承诺保留或恢复本结果。

<a id="configuration-resolution-errors"></a>

#### 11.10.5 错误、优先级与安全路径（已批准）

ResolutionError仅有code、operation、issues；operation为`resolve_configuration`或`get_entry`，issues为**恰含一项**ResolutionIssue的不可变tuple。ResolutionIssue仅有field_path（固定字段名及整数下标的tuple）与固定大写reason。按下述已批准顺序返回首个问题，修正后重试；本切片不做聚合诊断，避免跨阶段错误及级联解释。错误记录不含输入键文本、值、默认、原对象、异常、堆栈或任意对象表示；不为错误自行记录日志或加载诊断服务。

| code | reason及触发条件 | field_path |
| --- | --- | --- |
| INVALID_RESOLUTION_INPUT | REGISTRY_REQUIRED：不是支持的冻结注册表；INVALID_SHAPE：explicit_values不是精确dict | 分别为`("registry",)`、`("explicit_values",)` |
| INVALID_PARAMETER_KEY | INVALID_IDENTIFIER：输入键／查询键不满足Identifier | 输入为`("explicit_values", i, "key")`，查询为`("key",)` |
| UNKNOWN_PARAMETER | UNKNOWN_KEY：有效格式的键未在绑定注册表中 | 同上，不回显该键 |
| UNSUPPORTED_RESOLUTION_SEMANTICS | VALIDATOR_NOT_SUPPORTED、DEPENDENCIES_NOT_SUPPORTED、SCOPE_NOT_SUPPORTED、OVERRIDE_NOT_SUPPORTED、SENSITIVITY_NOT_SUPPORTED；COMPATIBILITY_NOT_SUPPORTED用于deprecated、replacement或upgrade_rule不满足支持表 | `("definitions", j, 字段名)`；一次只报告首个不支持字段，不枚举其内容 |
| REQUIRED_VALUE_MISSING | MISSING_REQUIRED：required键未提供且无默认 | `("definitions", j, "value")` |
| INVALID_CONFIGURATION_VALUE | UNSUPPORTED_VALUE、CYCLIC_VALUE、NON_FINITE_NUMBER、NULL_NOT_ALLOWED、TYPE_MISMATCH、OUT_OF_RANGE、NOT_IN_ENUM | `("definitions", j, "value", …)`，嵌套路径按下述规则 |

resolve的阶段优先级固定为：

1. 核验registry载体；失败不调用其方法。随后核验explicit_values载体。
2. 按dict插入顺序检查**所有顶层键格式**；有非法键则返回首个，先于任何未知键、能力或值错误。仅核验键，不访问不受支持键的哈希／比较钩子。
3. 键格式全合法后按同一插入顺序检查未知键，返回首个未知键；不遍历这些键对应的值。
4. 按注册表键排序检查整个集合的支持边界。同一定义内顺序为validator、dependencies、scope、override_policy、sensitivity、deprecated、replacement、upgrade_rule。先遇到的问题立即失败，即使该键未提交或其他键有值错误。
5. 按注册表键排序逐项选择值。无值且required时立即返回MISSING_REQUIRED；允许缺失则生成MissingValue。已冻结默认沿用其静态校验与不可变内容。显式值先检查完整数据树安全性，再检查顶层nullable／类型，再range，最后enum；任何失败停止，后续检查不运行。例如范围失败不追加NOT_IN_ENUM，非法显式值不转为缺失或默认。
6. 全部成功才返回完整快照。get_entry单独按“键格式→键存在性”检查；可缺失的已注册键不属于错误。

i为explicit_values插入顺序中的零基下标，j为绑定注册表list_definitions中的零基下标；改变输入插入顺序可能改变第2–3阶段首错，这属于本协议。显式值树按深度优先、序列下标升序及dict插入顺序检查：先判节点精确载体，再判环／有限性等；映射先检查全部键是否精确str，存在非法键则报该映射UNSUPPORTED_VALUE并停止其子树。序列子项追加下标；映射子值沿用所在映射路径，不追加用户键，可能多个位置共用安全路径。循环只检测当前祖先链，共享无环子树不报环。非有限Decimal及非法节点先于顶层类型错误；未支持节点不调用repr、序列化、深拷贝、迭代或比较钩子。

字段名范围限定为registry、explicit_values、key、definitions、value及支持表列出的八个定义字段。新增错误与原因码属于解析接口，既有注册／冻结／查询的六类错误、原因码、聚合方式与优先级全部不变。

<a id="configuration-resolution-examples"></a>

#### 11.10.6 完整合成验收例子（已批准，未执行）

下面是有限组可复现的契约预期，均为合成值，不是生产参数或已执行测试。为避免缺字段，先定义完整模板，再逐例列出全部差异；每例另建构建器，完整注册指定定义并确认freeze成功后才调用resolve。`NA`在本节例子中仅简写为`NotApplicable("合成例子不使用该能力")`，不是运行时新增标记。表内true／false／None及Decimal记法对应既有Python载体。解析成功统一指ResolutionOk(snapshot)，查询成功指ResolutionOk(entry)；错误统一指ResolutionErr(ResolutionError(code, operation, (ResolutionIssue(field_path, reason),)))，下文列出其具体载荷。

| 完整模板字段组 | 显式内容 |
| --- | --- |
| 身份与类型 | key=`demo.label`；owner_module=`demo_owner`；schema_revision=`demo_schema`；type=`string` |
| 默认与约束 | default=`NoDefault()`；required=true；nullable=false；unit=NA；range=NA；enum=`Declared(["alpha", "beta"])`；validator=[]；dependencies=[] |
| 作用域与权限 | scope=["instance"]；override_policy="no_override"；sensitivity="public"；read_roles=["demo_reader"]；write_roles=[] |
| 生效与影响 | apply_mode="demo_next_operation"；activation_group=NA；cost_impact="合成例子不发起调用"；migration_impact="合成例子不持久化" |
| 说明与兼容 | description="合成标签"；deprecated=false；replacement=NA；upgrade_rule=NA |
| 依据与使用 | rationale="验证显式解析及完整查询"；consumers=["demo_consumer"]；validation_method="比对条目状态、来源、安全错误与不可变性" |

**例一：同一快照内同时有默认、显式空值与缺失。** 注册三项：模板原键改为default=LiteralDefault("alpha")；第二项从模板改key="demo.note"、nullable=true、enum=Declared([None, "beta"])；第三项从模板改key="demo.optional"、required=false。输入`{"demo.note": None}`。resolve成功；get_registry返回传入句柄；list_entries按label、note、optional排序，state依次为PresentValue("alpha", DEFAULT)、PresentValue(None, EXPLICIT)、MissingValue。各条目的definition与上述完整定义一致，schema_revision均为demo_schema。get_entry("demo.optional")成功，查询"demo.unknown"返回UNKNOWN_PARAMETER／UNKNOWN_KEY／`("key",)`，查询" "返回INVALID_PARAMETER_KEY／INVALID_IDENTIFIER／`("key",)`。再次输入`{"demo.label": "beta", "demo.note": None}`得到label的EXPLICIT值；旧快照label仍为alpha。去掉note而不更改定义，则REQUIRED_VALUE_MISSING／MISSING_REQUIRED／`("definitions", 1, "value")`；不得以nullable补空。

**例二：默认不掩盖非法显式输入，空值与前置错误独立。** 只注册模板的以下完整变体：key="demo.amount"、type="decimal"、default=LiteralDefault(Decimal("1"))、nullable=true、range=Declared(RangeDescriptor(Bound(Decimal("0"), true), Bound(Decimal("2"), true)))、enum=Declared([None, Decimal("1")])，其他字段原样。逐次独立调用如下；错误operation均为resolve_configuration，单项路径均为`("definitions", 0, "value")`。

| 本次explicit_values | 唯一预期 |
| --- | --- |
| {} | 成功，PresentValue(Decimal("1"), DEFAULT) |
| {"demo.amount": None} | 成功，PresentValue(None, EXPLICIT)，required仍被满足 |
| {"demo.amount": Decimal("1.00")} | 成功，EXPLICIT，精确值等于枚举中的1；不改写来源为DEFAULT |
| {"demo.amount": "1"} 或 {"demo.amount": 1} | INVALID_CONFIGURATION_VALUE／TYPE_MISMATCH |
| {"demo.amount": 1.0} | INVALID_CONFIGURATION_VALUE／UNSUPPORTED_VALUE |
| {"demo.amount": Decimal("NaN")} | INVALID_CONFIGURATION_VALUE／NON_FINITE_NUMBER |
| {"demo.amount": Decimal("3")} | INVALID_CONFIGURATION_VALUE／OUT_OF_RANGE，虽也不在枚举，不追加原因 |
| {"demo.amount": Decimal("0")} | INVALID_CONFIGURATION_VALUE／NOT_IN_ENUM，0是已提供值 |

再独立注册上述amount定义的两个变体：其一enum=Declared([Decimal("1")])、nullable仍为true，其二在其一基础上nullable=false；两者显式输入None分别返回NOT_IN_ENUM及NULL_NOT_ALLOWED。第三个独立变体令default=NoDefault()、required=false；空dict成功返回MissingValue，不自动补None。§11.10.2缺失矩阵的其余格按模板仅修改required／nullable／default并使enum显式允许相应默认即可构造；两个nullable=false配空默认的格应在注册失败，不送解析器。

**例三：嵌套所有权与整体替换。** 只注册模板变体key="demo.payload"、type="object"、enum=NA、default=LiteralDefault({"kept": [1]})。提交`{"demo.payload": {"items": [None, {"label": "alpha"}]}}`。成功条目为EXPLICIT，value只有items键，其值为tuple，内部label映射只读；kept不与显式对象合并。调用完成后清空原dict、给原items追加值、修改原label均不改变快照。通过get_entry和list_entries修改任何层级必须被语言拒绝；随后以{}解析可取得DEFAULT的只读kept对象，旧快照仍不变。另次令cycle为空列表，再追加它自身，提交{"demo.payload": {"items": cycle}}，返回INVALID_CONFIGURATION_VALUE／CYCLIC_VALUE／`("definitions", 0, "value", 0)`；在该位置改放带抛错repr／迭代／深拷贝钩子的自定义对象，改报UNSUPPORTED_VALUE且不得调用钩子。两次失败均不影响先前结果；共享但不成环的子列表则允许。

**例四：全集合能力拒绝、错误优先级与空集合。** 只注册模板变体validator=["demo_check"]、dependencies=["demo.label"]；该自引用允许注册并冻结。resolve输入`{"unknown": object(), "bad key": None}`先返回INVALID_PARAMETER_KEY／INVALID_IDENTIFIER／`("explicit_values", 1, "key")`；删除bad key后返回UNKNOWN_PARAMETER／UNKNOWN_KEY／`("explicit_values", 0, "key")`，均不得检查unknown值。输入{}或`{"demo.label": "gamma"}`均先返回UNSUPPORTED_RESOLUTION_SEMANTICS／VALIDATOR_NOT_SUPPORTED／`("definitions", 0, "validator")`；不执行验证器。另建定义仅清空validator后，同样输入返回DEPENDENCIES_NOT_SUPPORTED／`("definitions", 0, "dependencies")`，不把引用存在当作依赖校验完成。另建独立模板变体，逐一把scope改为["platform"]、override_policy改为"layered"、sensitivity改为"secret"、deprecated改为true、replacement改为Declared("demo.other")、upgrade_rule改为Declared("合成升级说明")，这些独立变体各自同时令required=false并输入{}，分别按支持表的原因及对应字段路径拒绝。多作用域及任意未知策略标识同样拒绝。未修改的完整模板配{"demo.label": "alpha"}可正常解析；空注册表配{}成功且list_entries为()，配{"unknown": None}仍拒绝未知键。以构建器代替registry并同时提交非dict输入，首错为INVALID_RESOLUTION_INPUT／REGISTRY_REQUIRED／`("registry",)`；冻结空注册表配[]则为INVALID_SHAPE／`("explicit_values",)`。

未来验收还须用上述完整模板的单字段变体覆盖六种载体及所有合法假值、键精确匹配／Unicode差异、输入自定义子类与类型比较钩子、非str嵌套键、足够深的有限树及低精度Decimal上下文；核对首错顺序、所有公开输出深只读、修正重试、两个独立注册表绑定互不串用及无外部副作用。它们是已批准验收条件，尚未执行，不是本轮运行结果。

<a id="configuration-resolution-decisions"></a>

#### 11.10.7 集中已批准决定与停止点

| 决定 | 已批准方案 | 理由 |
| --- | --- | --- |
| S1 缺失与默认 | 显式值→唯一Schema默认→required失败／MissingValue；合法None满足required；非法显式值不回退；采用两张完整组合表 | 承接已有required／nullable含义，区分缺失、空值和默认 |
| S2 输入与值规则 | 精确dict及六类原始载体，精确键，未知键拒绝；无类型转换、分层合并或旧快照继承；沿用范围与类型敏感枚举 | 能由冻结定义与显式内存值独立验收，不引入加载或生产参数 |
| S3 能力支持子集 | 全集合检查；非空validator／dependencies拒绝；仅instance／no_override／public；拒绝兼容升级声明；角色与激活信息仅保留元数据 | 未执行的语义不伪装通过；三个新标识只约束解析准入，不改注册表 |
| S4 快照与公开查询 | ResolutionOk／ResolutionErr独立结果；MissingValue／PresentValue／SnapshotEntry；四项接口；绑定原冻结句柄，完整稳定枚举，缺失可查询且无fallback | 不改既有注册表协议，值的存在性、类型、定义与来源可明确检查 |
| S5 标识与不可变边界 | 不新增快照／配置revision ID；schema_revision仅保留原义；深隔离、旧快照不变、失败无部分结果；仅本地内存承诺 | 避免假造T12持久身份与激活收据，保留后续恢复设计空间 |
| S6 错误与验收 | 固定解析错误与大写reason、确定阶段顺序、仅首个安全问题；以上完整合成例子及边界条件作为后续验收预期 | 失败可识别且不泄漏输入，验收能区分支持、拒绝与缺失 |

普通私有函数拆分、临时容器、遍历实现及是否共享既有不可变对象不单列审批项，只须满足公开行为。本轮到契约定稿及一致性检查为止；不实现、不运行项目代码、不增依赖、不提交或推送。本契约已批准，但不等于授权编码，下一步实现须由用户明确授权；更不授权后续激活、持久化、权限或完整配置模块。

<a id="configuration-additional-validation-contract"></a>

### 11.11 日志所需附加与跨参数校验：最小补充契约

**状态：C1–C4及G1表示／定义匹配规格均已批准。** 本轮仅授权日志所需纯内存配置校验，使用明确的完整合成Schema、五类目录清单及非秘密public路径验证；G2真实目录、路径敏感分级及资源核验仍是生产装配前置，不阻止合成验证。实现进度与实际检查见[CURRENT_TASK](../work/CURRENT_TASK.md)。本节仅补足[日志配置前置缺口](logging.md#runtime-diagnostics-configuration)；日志参数、默认值和数值范围仍以该处唯一参数表为准，不在此复制或补齐另一套生产定义。§11.9／§11.10正文、公开类型和既有拒绝语义保持不变。已批准决定集中见[末表](#configuration-additional-validation-decisions)。

#### 11.11.1 显式扩展入口与兼容

新增`resolve_configuration_with_logging_validation(registry, explicit_values, protected_directories) → CheckedResolutionResult<EffectiveSnapshot>`，三项均显式提供。前两项沿用§11.10的精确载体、全集合检查、唯一默认取值及输入稳定要求；第三项仅提供下述路径隔离事实，不提供配置覆写或验证器。成功为深不可变`CheckedResolutionOk(value)`，失败为独立的`CheckedResolutionErr(error)`；不扩充原ResolutionResult／RegistryError的码或operation枚举。成功快照沿用原生EffectiveSnapshot、原注册表绑定和三个查询接口；get_entry仍用原ResolutionResult，无新快照构造器、ID、能力证书或激活收据。

原`resolve_configuration(registry, explicit_values)`仍按§11.10拒绝任何非空validator／dependencies，额外关键字仍按语言调用规则拒绝；不自动转调新入口，不先删除声明调用原入口再补校验。新入口核对完整日志参数集合及其定义符合日志表和本节所需声明，缺项或不符即拒绝；其余注册参数也完整保留、解析和检查。除下述validator／dependencies语义外，scope、override_policy、sensitivity及兼容升级等支持边界仍沿用§11.10；不因file_enabled=false或参数未显式提供跳过校验。日志initialize仍须核对必要定义与校验能力，不将一般解析成功当作日志适用性证明。

#### 11.11.2 静态声明、查找与只读依赖

validator仍是§11.9的标识序列；M15随程序发布固定白名单，精确查找以下四个标识及其绑定，定义仅存标识。没有调用方注册／替换验证器、插件发现、任意回调、动态导入、表达式或按名字反射执行的入口；扩充白名单须另审公开语义。基础类型／范围／枚举通过并取得独立所有权后，验证器只接收本项不可变候选值、已声明依赖的只读候选条目，以及路径验证器专用的已隔离目录输入；不能读取原始输入、未声明键、环境、文件、网络、时钟或日志，也不能改写值、补默认或返回派生值。

| 静态验证器标识 | 绑定参数与必要声明 | 唯一附加检查 |
| --- | --- | --- |
| `logging_module_levels` | `logging.module_levels`，无必要依赖 | 按[日志事件入口](logging.md#runtime-diagnostics-events)的模块集合及[参数表](logging.md#runtime-diagnostics-configuration)核对映射项数、精确模块名与等级；拒绝未知模块、嵌套对象、空值、非文本等级，不转换或继承前缀 |
| `logging_warning_reserve` | `logging.warning_reserve`，dependencies必须含`logging.sink_capacity` | 候选值满足R<Q；各自单参数上下界仍由参数表对应range检查 |
| `logging_rotation_bytes` | `logging.rotation_bytes`，dependencies必须含`logging.event_max_bytes` | 候选值满足E≤B，等号允许；不将此关系误用于flush／I/O时限 |
| `logging_file_directory` | `logging.file_directory`，无必要参数依赖；使用protected_directories | 按下节检查路径文本及目录隔离；不检查易变资源事实 |

未知标识在能力检查阶段拒绝；已知标识绑定错误的键／类型、缺少本表必要验证器或必要依赖同样拒绝，不能静默跳过。固定验证器只返回通过或下表固定失败原因；意外抛出普通异常或返回非法结果时以VALIDATOR_FAILED安全失败，不保留异常／堆栈，不回退默认或发布结果。内存耗尽等无法完成结果构造的运行故障沿用§11.10边界，不伪装领域成功。

dependencies**只声明从完整候选集合读取值，不定义求值或执行顺序**。所有参数先独立完成显式值／默认／缺失选择及基础校验，再检查依赖并执行验证器；无拓扑求值、递归解析、固定点迭代或计算引擎。允许额外的已注册依赖、无validator的依赖声明、前向引用、自引用及声明环；它们只要求所引用条目为PresentValue，不自动产生其他约束或额外执行。可选且无值的被依赖项报DEPENDENCY_VALUE_MISSING，不跳过、不填None；被依赖项required且无值先按基础解析报MISSING_REQUIRED。PresentValue(None)不算缺失，是否合法由基础约束及明确验证器决定；有validator的本项为MissingValue时报VALIDATED_VALUE_MISSING。依赖指向未知键仍在§11.9冻结时报UNRESOLVED_DEPENDENCY，不改变其允许声明环的行为；声明环与仍须拒绝的数据树循环是两回事。

#### 11.11.3 路径输入来源与资源边界

protected_directories为精确dict，恰含`media`、`database`、`audit`、`provider_usage`、`backup`五个固定键；每项是非空精确list／tuple，成员为精确str目录文本。同一目录可因共库存储出现在多个类别。可信启动装配方从本次部署已明确的媒体／blob、数据库、审计、账本及备份资源布局显式提供完整目录清单；数据库文件提供所属受保护目录。清单须覆盖本次装配全部对应资源，不得用空项、猜测路径或省略尚未知类别换取成功；来源尚未具备则前置条件未满足。本节不决定这些资源的生产配置键、默认路径或加载方式，不让日志模块自行扫描或读取环境补齐。

logging.file_directory及上述目录文本都须满足日志参数表的路径长度、无控制字符及无凭据要求；纯内存语法限定为规范POSIX绝对目录文本：单个起始`/`，除根外无尾部`/`，无重复分隔符、`.`或`..`组件，不展开`~`、环境变量或URL，不隐式清洗或改写。日志目录不能为根；按精确路径组件比较，日志目录与任一受保护目录相同、为其祖先或后代均拒绝，不能仅用字符串前缀比较。无凭据仍由可信装配方保证，语法检查不能证明任意普通字符串不是秘密；public也不是安全审查证明。

仅输入形状、语法及上述词法隔离通过不代表物理隔离成立：真实存在性、全部相关路径的符号链接／别名导致的实际重合、可写性、目标文件类型及独占所有权仍由[日志资源准备](logging.md#runtime-diagnostics-output)核验，无法确认则拒绝。配置阶段无stat、realpath、试写或资源打开；目录清单在本次调用内取得独立不可变所有权，只供检查，不存进配置值或公开快照。装配方须保证校验与日志资源准备针对同一部署布局；不能把一次内存结果当作以后资源状态的保证。非public路径支持、生产目录布局及部署安全审查仍是独立前置缺口。

#### 11.11.4 一次发布与安全首错

阶段顺序为：沿用§11.10的registry／explicit_values载体、全部键格式、未知键检查 → 整个注册集合的能力检查（按定义排序，字段顺序沿用§11.10；validator按声明顺序）→ 核对日志参数完整性与必需声明（按完整键排序）→ protected_directories形状、文本语法及隔离输入所有权 → 全集合基础值选择／校验／隔离 → 全集合依赖值存在性（定义排序、依赖声明顺序）→ 验证器（定义排序、声明顺序）→ 一次返回完整快照。目录输入按上节五键顺序及成员下标检查；文件目录自身的语法与隔离在其验证器内检查。前一阶段失败不运行后续阶段；默认值也须经过附加检查。所有中间候选只在M15内部可见，失败不交出部分快照，不修改注册表、原输入、旧快照或任何有效指针；公开结果、错误及快照均深不可变，成功后支持并发只读。

CheckedResolutionError仅有code、operation、issues；operation固定为新入口名，issues恰含一个不可变问题（field_path、reason）。基础失败沿用§11.10对应code／reason／安全路径语义，但以新结果类型返回；新错误限定如下，不改变原接口错误集合：

| 新code | 固定reason及触发 | 安全field_path |
| --- | --- | --- |
| UNSUPPORTED_VALIDATION_DECLARATION | UNKNOWN_VALIDATOR：非白名单；VALIDATOR_BINDING_INVALID：绑定键／类型不符；REQUIRED_VALIDATOR_MISSING、REQUIRED_DEPENDENCY_MISSING：漏必要声明 | `("definitions", j, "validator")`或`("definitions", j, "dependencies")`，未知标识／绑定错误追加其validator声明下标，缺少声明不追加下标 |
| INVALID_LOGGING_SCHEMA | LOGGING_DEFINITION_MISSING、LOGGING_DEFINITION_MISMATCH：完整日志表所需定义缺失／其他元信息不符 | `("logging_definitions", k)`，k按日志表展开完整键的排序定位 |
| INVALID_VALIDATION_CONTEXT | INVALID_SHAPE、PATH_SYNTAX_INVALID：目录输入形状／语法不合要求 | `("protected_directories",)`或追加上节固定类别名及成员下标 |
| ADDITIONAL_VALIDATION_FAILED | DEPENDENCY_VALUE_MISSING、VALIDATED_VALUE_MISSING：前述值缺失；MODULE_LEVELS_INVALID、RESERVE_NOT_LESS_THAN_CAPACITY、EVENT_EXCEEDS_ROTATION、PATH_SYNTAX_INVALID、PATH_OVERLAP：相应检查不通过；VALIDATOR_FAILED：验证器异常或非法结果 | 依赖为`("definitions", j, "dependencies", d)`；其余为`("definitions", j, "value")` |

j／d是注册表排序下标／依赖声明下标。所有路径只含上述固定字段及整数下标；不回显输入键、模块名、路径值、默认、异常、对象引用或任意表示，不调用输入对象钩子，也不为错误自行记日志。新增检查失败不得清空声明重试、降级敏感标签或以日志私有验证制造成功。

<a id="configuration-logging-definition-match"></a>

**LOGGING_DEFINITION_MISMATCH的比较边界（G1已批准）：** 只在既有“核对日志参数完整性与必需声明”阶段比较下表；不新增错误码、字段、检查阶段或运行时审批功能。所需键缺失仍为LOGGING_DEFINITION_MISSING；未知验证器、错误绑定及缺少必要声明仍使用原有专用原因。全集合能力拒绝先于本阶段，例如非public、兼容升级声明不支持时，不能改报MISMATCH。各定义按日志完整键排序，只报首个问题，路径仍为`("logging_definitions", k)`，不回显字段值。

| 字段组 | 必须满足的行为／元信息约束与比较方式 |
| --- | --- |
| key、owner_module、type、default、required、nullable | key精确匹配参数表完整键；owner_module为logging_service；其余按[唯一参数表与字段规格](logging.md#runtime-diagnostics-schema)检查。default区分NoDefault与LiteralDefault，后者按§11.9类型敏感深度相等比较；不允许换默认，即使本次提交了显式值 |
| unit、range、enum | unit采用字段规格指定标识，不换算；integer的上下界数值及闭合性必须与参数表相同，不能放宽、收窄或省略；其他类型range为NotApplicable。enum按类型敏感成员集合比较，顺序不影响允许值，不能增删成员；无枚举项须为NotApplicable，不以附加validator代替必需的range／enum |
| validator、dependencies | 核对必要验证器及必要依赖，缺少者报原专用原因；已知验证器仍只能绑定其指定键／类型。**不要求dependencies等于生产定义建议中的最小列表**，§11.11.2已批准的额外已注册依赖、无validator依赖、自引用及声明环继续允许，并接受原有缺值检查；不能用MISMATCH撤销这些语义 |
| scope、override_policy、sensitivity、deprecated、replacement、upgrade_rule | 沿用已批准支持边界；能到达本阶段的定义仍须满足字段规格中的既定要求。目录敏感级别未确认时不能生成假定为public的生产定义；这属于规格前置缺口，不是新增运行时“审批状态” |
| consumers、read_roles、write_roles、apply_mode、activation_group | consumers必须包含logging_service，不以元信息声明授权额外消费者；角色按最终批准的可信运维标识集合比较，不按列表顺序判差异、不执行鉴权；apply_mode精确采用初始化生效标识，activation_group采用规定的NotApplicable标记。不从自由说明文字推断生效行为或角色权限 |
| schema_revision及说明性内容 | schema_revision仅须满足§11.9的Identifier规则，保留各定义原标识，不与某个魔法版本串比较。description、rationale、validation_method、cost_impact、migration_impact及所有NotApplicable.reason须满足原有非空说明规则并保留；不逐字匹配本规格说明、不从文本求值。说明不得谎称已执行验收、持久化或权限落实，属文档／代码审查责任，不增设文本语义分析器 |

说明文字完善不改变通过条件，也不逐条申请批准。既有行为的表示、角色绑定和比较口径已在[G1](logging.md#runtime-diagnostics-prerequisite-decisions)获批；§11.9／§11.10及本节原有错误优先级不改。比如仅改description或换合法schema_revision不应触发MISMATCH；改默认、改变范围端点或误标即时生效则应拒绝；这是已批准的匹配预期；具体执行结果只记录在CURRENT_TASK。

#### 11.11.5 代表性验收例子（已批准）

以下是契约预期，非当前测试结果；本轮合成测试的实际执行记录见[CURRENT_TASK](../work/CURRENT_TASK.md)。日志例子以完整Schema、表中所需声明、其余值合法、显式完整目录清单为前提；示例数值只用于边界说明，不另设默认。依赖声明例子可用§11.10.6完整合成模板构造额外参数。

| 合成输入／操作 | 契约预期 |
| --- | --- |
| 同一完整日志注册表调用原入口；再调用新入口 | 原入口仍按原首错顺序报VALIDATOR_NOT_SUPPORTED／DEPENDENCIES_NOT_SUPPORTED；新入口全部检查通过才给原生完整快照，不删声明 |
| module_levels={bootstrap: NOTSET}；另次改为未知模块或等级对象 | 前者通过；后者MODULE_LEVELS_INVALID，无模块名或值回显；若值是带repr钩子的自定义对象，先由基础安全检查拒绝且钩子不执行 |
| 各次独立提交R=Q；E=B；E>B，其余值合法 | 分别RESERVE_NOT_LESS_THAN_CAPACITY、通过、EVENT_EXCEEDS_ROTATION；文件端禁用时仍检查；使用默认的候选也检查 |
| 额外合成参数a、b互相依赖（另例a自依赖），无validator，两者均有合法文本值；互依例另次令可选b无默认且缺失 | 声明环不触发求值且可通过；缺失时报DEPENDENCY_VALUE_MISSING，无部分结果；未知依赖键在冻结时失败，未知validator在新入口能力阶段失败 |
| 受保护media目录为/srv/media，日志目录分别为/srv/media/logs、/srv、/srv/media2、/srv/../logs，其余类别为互不重合的合成目录 | 前两项PATH_OVERLAP；第三项词法隔离可通过；第四项PATH_SYNTAX_INVALID。未提供完整目录类别则INVALID_VALIDATION_CONTEXT；符号链接等资源事实不由此例证明 |
| 全部成功后修改原module_levels及目录清单；另次跨参数失败或固定验证器抛普通异常 | 已返回快照及旧快照不变、公开嵌套修改被拒绝；失败无快照，异常只报VALIDATOR_FAILED且不泄漏异常。日志资源准备仍独立核验真实目录 |

<a id="configuration-additional-validation-decisions"></a>

#### 11.11.6 集中已批准决定与前置缺口

| 已批准决定 | 已批准方案 |
| --- | --- |
| C1 扩展入口及兼容 | 专用显式入口、独立结果／错误类型，复用原生快照；原resolve_configuration及§11.9／§11.10全部行为不变 |
| C2 白名单与依赖 | 四项固定验证器及绑定，完整候选先形成；依赖只读、不求值，缺值失败，允许声明环；未知声明及验证器异常安全失败 |
| C3 路径隔离输入 | 可信装配方提供五类完整目录清单；规范POSIX文本和组件隔离，真实资源核验仍归日志准备，不引入加载或秘密识别能力 |
| C4 发布、错误与验收 | 上述确定首错顺序、独立安全错误、完整校验后一次发布、深不可变与输入隔离，以及上述合成验收预期 |

日志完整字段的落点、目录来源核对及剩余决定见[日志实施前置规格](logging.md#runtime-diagnostics-schema)。本补充及G1表示／匹配细化已批准；真实目录布局、安全分级与资源核验仍按[G2待定事项](logging.md#runtime-diagnostics-prerequisite-decisions)处理，不能把候选布局或合成夹具当作真实部署；若路径需非public则另定对应支持。本轮仅授权本节纯内存实现，不自动注册参数、不选生产路径、不创建夹具目录；生产装配及日志服务仍须另行授权。本节不扩展加载、持久化、秘密解析、权限、热修改、配置计算引擎或后续日志能力。

<a id="configuration-persistence-validation-contract"></a>

### 11.12 持久化与审计所需配置校验：已批准契约

**状态：契约已批准，待实现授权。** 本节是[持久化事务基础整体契约](persistence-and-transactions.md#persistence-foundation-contract)的配置唯一正文，已随其[集中决定P6](persistence-and-transactions.md#persistence-foundation-decisions)获批；不扩大整体交付范围。§11.9–11.11及现有两个解析入口、公开类型、日志定义匹配和拒绝语义均不改变，不自动转调本入口，不新增实现授权。

#### 11.12.1 显式入口、载体与日志组

已批准新增`resolve_configuration_with_persistence_validation(registry, explicit_values, protected_directories) → PersistenceResolutionResult<EffectiveSnapshot>`。三项都须显式提供，无默认实参；不接受额外的actor、validator、回调、路径发现、加载、scope、快照ID或激活参数。缺少实参／未知关键字按语言签名拒绝，不包装为领域成功。

registry和explicit_values完整沿用[§11.10.2](#configuration-resolution-input)的原生冻结ReadOnlyRegistry、精确内建dict、Identifier／MetadataValue、精确类型身份、输入稳定及禁止对象钩子的要求；不能传构建器、鸭子类型、子类、通用Mapping或JSON文本。第三项是下表规定的精确None或目录dict，不携带配置覆写或验证器。

| 日志组判定 | protected_directories与检查责任 |
| --- | --- |
| 冻结注册表没有任何键以精确前缀`logging.`开头 | 明确传None；任何其他载体均为CONTEXT_NOT_APPLICABLE，不遍历它。检查完整storage／audit组及所有额外注册定义；不虚设日志参数或五类目录，不声称检查了日志目录隔离 |
| 冻结注册表至少有一个键以`logging.`开头 | 必须具备[日志唯一参数表](logging.md#runtime-diagnostics-configuration)中的完整20项定义，再按已批准[日志匹配](#configuration-logging-definition-match)检查；第三项必须为§11.11.3规定的完整精确dict。None为CONTEXT_REQUIRED；其他形状／内容仍按该节检查 |

触发只取决于注册表键，不取决于显式值是否提供、输出端是否禁用、consumer或schema_revision。仅注册logging自定义键也触发完整日志组检查；缺任一必需定义都不能按“无日志组”通过。点仍是原键字符，不展开嵌套对象，不改大小写；显式提交未注册的logging键先按原未知键规则拒绝。完整必需组不排除其他已注册参数：它们全部保留并检查，不能截取子集生成快照。

有日志组时，目录上下文的固定类别、非空成员、规范文本、组件隔离及所有权隔离只按[§11.11.3–11.11.4](#configuration-additional-validation-contract)执行；其真实完整性仍由可信装配方保证，须描述本次storage.database_file及同库审计的实际所属目录，不能以无关目录充数。本入口不扫描、推算或补齐部署清单，不将目录上下文存入快照。无日志组不豁免[存储资源准备](persistence-and-transactions.md#persistence-foundation-configuration)的物理安全核验。生产目录分级／G2保持待定。

<a id="configuration-persistence-definitions"></a>

#### 11.12.2 完整storage／audit定义与匹配

以下10项为本入口始终必需的完整集合；参数用途在[事务正文](persistence-and-transactions.md#persistence-foundation-configuration)导航，类型、约束、默认及匹配只在本节维护。全部required=true、nullable=false、default=NoDefault()，无生产默认；下界／上界均含端点，integer使用精确整数Bound。所有enum为NotApplicable，非数值range为NotApplicable；不换算单位。

| key | type | unit | range／必需validator |
| --- | --- | --- | --- |
| storage.database_file | string | NotApplicable | range不适用；validator必须含storage_database_file |
| storage.operation_timeout_ms | integer | milliseconds | 1–60000 |
| storage.lock_wait_ms | integer | milliseconds | 0–60000 |
| storage.close_timeout_ms | integer | milliseconds | 1–60000 |
| storage.read_capacity | integer | connections | 1–16 |
| storage.command_max_bytes | integer | bytes | 256–1048576 |
| storage.receipt_max_bytes | integer | bytes | 256–65536 |
| storage.wal_checkpoint_pages | integer | pages | 1–65536 |
| audit.event_max_bytes | integer | bytes | 256–65536 |
| audit.events_per_operation | integer | events | 1–256 |

storage项owner_module=persistence，consumers的最小声明为[persistence]；audit项owner_module=logging_service，consumers的最小声明为[logging_service]。每个consumer集合必须包含相应消费者，额外声明不授予权限。共同scope=[instance]、override_policy=no_override、read_roles／write_roles均为[trusted_operator]、apply_mode=INITIALIZE_ONLY、activation_group=NotApplicable；deprecated=false，replacement／upgrade_rule均为NotApplicable。数值sensitivity=public；路径只对明确非秘密合成值按public声明，生产分级未定时不能伪造完整生产定义。

schema_revision的建议标识仍为persistence_audit，但匹配只检查原Identifier合法性，不与该字符串比较。description／rationale据参数用途完整说明，validation_method说明精确载体、范围及固定验证器检查；cost_impact说明仅影响本地资源、不执行付费模型调用，migration_impact说明重建服务采用、不迁移数据库或改写既有事实。所有说明和NotApplicable.reason只按§11.9的非空Text条件保存，不逐字匹配或求值，不把未决定的字段填成NotApplicable。

匹配时依次检查每个必需键存在、必需validator声明，然后检查其余元信息；任一不符即停止。key／owner_module／type／required／nullable／apply_mode精确匹配上述规格；NoDefault与LiteralDefault严格区分，显式值存在也不能换默认。unit、数值range的数值与闭合性须完全一致；非数值range及全部enum须用NotApplicable，不允许用validator替代范围。角色按上述标识集合比较，consumers按必要成员检查；其余元信息按本节共同要求和§11.9标记载体判断。支持边界先于匹配，例如非public或兼容升级声明仍先报原不支持原因，不改为DEFINITION_MISMATCH。

storage.database_file以外的9项不需要附加validator；五项白名单各自的精确绑定使它们不能借用别的参数的validator。10项均无必需dependencies，推荐最小声明为空；**额外已注册依赖继续按§11.11.2检查存在值**，不强制等于空列表、不排除前向／自引用／声明环。日志组仍严格使用其已有匹配正文，不能套用本节storage／audit的默认或角色规格覆盖日志规则。

#### 11.12.3 固定验证能力

本入口的validator白名单恰为§11.11.2的四项日志验证器，加`storage_database_file`；前四项绑定和行为不变，新项只能绑定同名storage.database_file键且type=string。未知标识、错误绑定和漏必需声明安全拒绝，不能动态注册或把声明清空后调用旧入口。

新路径验证器只接收已隔离的本项值，纯内存检查：精确str，长度1–4096字符，规范POSIX绝对文件文本；单个起始`/`、非根、无尾部`/`、无重复分隔符、`.`或`..`组件，不含U+0000–U+001F或U+007F，不接受URI或`:memory:`，不展开`~`或环境变量，不修剪／归一化。不含凭据仍须可信调用方保证，解析器不声称识别秘密。只返回通过或PATH_SYNTAX_INVALID；普通异常／非法返回为VALIDATOR_FAILED，无原始异常泄漏。它不读取目录上下文、其他参数、文件、环境、网络、时钟、日志或数据库。

其余依赖和日志验证器完全复用§11.11.2的只读候选、依赖存在性、已声明键访问及禁止求值规则；没有计算引擎、派生值或回调注入。内存耗尽／进程控制等非预期故障不伪装为领域成功。

<a id="configuration-persistence-order"></a>

#### 11.12.4 全集合顺序与失败原子性

严格按下列顺序执行，先失败即停止；日志组判定本身只查看已经核验的注册表键，不提前检查第三项输入。

1. 先registry、再explicit_values载体；再按dict插入顺序检查全部键格式，全部合法后才按同序找首个未知键。此时不访问值或目录上下文。
2. 整个注册集合按完整键Unicode排序检查能力，包含未显式提供或可缺失的定义。同一定义先按声明顺序检查五项validator白名单和键／类型绑定，再沿用§11.10的其余支持字段顺序；dependencies在此仅接受§11.11.2语义，其候选值不在此求值。
3. 先按storage／audit必需10键排序核对完整性、必需声明和上述定义匹配，再在触发日志组时按原日志20键排序执行§11.11的完整性、必需声明及G1匹配。两组同时残缺先报告storage／audit；不合并改变日志组内优先级。
4. 无日志组检查第三项为None；有日志组先拒绝None，再执行原目录形状、语法和所有权隔离顺序。形状在顶层定位，固定类别按media、database、audit、provider_usage、backup及成员下标检查。
5. 全注册集合按键排序选择值、检查完整树安全性、nullable／类型、range、enum并隔离。缺失／默认／空值、错误优先级和安全路径复用§11.10.2、§11.10.5；值非法不回退默认，不省略额外参数。
6. 对全体候选按定义排序、依赖声明顺序检查PresentValue；缺失和已显式空值按§11.11.2区分，无拓扑求值。
7. 按定义排序、validator声明顺序执行固定验证器；含validator但本项MissingValue先报VALIDATED_VALUE_MISSING。日志目录的词法隔离仍在其原验证器执行；新存储路径只作§11.12.3的纯文本检查。
8. 全部成功才一次生成绑定原注册表的原生EffectiveSnapshot，包含每个注册键及原EXPLICIT／DEFAULT来源。任何失败无部分快照，无注册表、输入、旧快照或有效指针变化。

结果、issues、快照及嵌套数据均深不可变；目录只作本次检查输入，不随快照发布。新结果不会赋予快照ID、持久性、权限或资源就绪证明。服务只通过现有三个快照公开查询读取并核对必要定义；不得调用私有构造器或清空validator绕过本入口。失败后只有调用方显式修正并重调，没有清理声明重试或外部补偿动作。

<a id="configuration-persistence-errors"></a>

#### 11.12.5 固定错误与安全路径

`PersistenceResolutionResult<T> = PersistenceResolutionOk(value) | PersistenceResolutionErr(error)`；其error恰含code、operation、issues，operation固定为`resolve_configuration_with_persistence_validation`，issues恰含一个不可变`(field_path, reason)`问题。该类型与事务PersistenceError、RegistryError、ResolutionError、CheckedResolutionError分别独立；转换已有规则的失败时只保留已核验code／reason／安全路径，operation换为本入口名，不保留嵌套原错误或输入引用。

允许code／reason／路径集合**封闭为下表并集**；被引用的既有表只用于复用已列明规则，不为其他未来原因开放通配。不复制维护既有错误全文，不改变原入口operation或枚举。

| 来源／code | 固定原因与路径规则 |
| --- | --- |
| §11.10.5的六类基础code | 只采用该[错误表](#configuration-resolution-errors)中解析调用的原因及路径，排除VALIDATOR_NOT_SUPPORTED、DEPENDENCIES_NOT_SUPPORTED；这两类声明在本入口按§11.12.3及步骤6处理。get_entry保持原ResolutionResult，不产生本入口operation |
| §11.11.4的四类附加code | 采用[原错误表](#configuration-additional-validation-contract)已列出的全部code／reason／路径；日志Schema专用错误只在日志组触发时产生，新路径验证器复用ADDITIONAL_VALIDATION_FAILED／PATH_SYNTAX_INVALID，定位`("definitions", j, "value")`。未知／绑定错误和漏声明同样复用该表的definitions路径 |
| INVALID_PERSISTENCE_SCHEMA | PERSISTENCE_DEFINITION_MISSING、PERSISTENCE_DEFINITION_MISMATCH；路径恰为`("persistence_definitions", k)`，k为本节10个必需完整键的排序下标。缺必需validator仍优先使用既有REQUIRED_VALIDATOR_MISSING及definitions路径 |
| INVALID_VALIDATION_CONTEXT的两项补充 | CONTEXT_REQUIRED：有日志组却传None；CONTEXT_NOT_APPLICABLE：无日志组却传非None。路径恰为`("protected_directories",)`；不回显或遍历不适用的对象 |

i／j／d及原日志k的下标含义沿用所属既有表；新增persistence_definitions的k只定位本节必需键清单，不是注册表j。目录错误路径最多为`("protected_directories", 固定类别, 成员下标)`；顶层或类别形状错误分别止于顶层／类别，不输出未知类别名。其余只包含被引用表的固定字段名及整数下标，不含提交键、默认、路径值、模块名、异常、栈或对象表示，不调用其哈希／比较／转换钩子，也不自行记录诊断。无法构造安全错误的非预期运行故障按既有边界处理，不交付快照。

#### 11.12.6 组合验收例子（全部未执行）

前提为完整合成storage／audit定义，参数值取[整体合成场景](persistence-and-transactions.md#persistence-foundation-acceptance)，路径仅用非秘密合成文本；有日志组时追加完整已批准日志定义、合法显式值和五类目录文本。不创建目录或数据库；以下仅列后续测试预期。

| 组合输入 | 首个结果与边界 |
| --- | --- |
| 无日志组，protected_directories=None | 全集合通过才返回原生快照；含10项和所有额外注册参数，无日志定义或目录发现动作 |
| 完整日志组，console_enabled和file_enabled均false，完整合法目录上下文 | 仍执行日志全部匹配／值／路径校验；成功快照包含完整两组和额外参数，不因关闭输出免检 |
| 仅缺logging.file_directory定义，未提交该显式键，且上下文为None | 第3步INVALID_LOGGING_SCHEMA／LOGGING_DEFINITION_MISSING，先于CONTEXT_REQUIRED；不能当作无日志组 |
| storage／audit及日志各缺一个必需定义，未显式提交缺项，目录亦非法 | 先报按10键排序的PERSISTENCE_DEFINITION_MISSING；补齐后才可能报日志缺项，再后才检查目录 |
| 完整组但额外定义有未知validator，另有缺少的必要storage定义及非法值 | 第2步UNKNOWN_VALIDATOR先于第3步缺定义及第5步值错误；即使额外项可缺失仍拒绝 |
| 非原生registry＋非dict值＋非法上下文；另次合法registry但显式dict同时有非法键和未知键 | 分别先REGISTRY_REQUIRED、INVALID_IDENTIFIER；不访问后续对象或值 |
| 无日志组传{}且某必需值缺失；有完整日志组传None且某值越界；有日志组传残缺目录dict且某值越界 | 分别CONTEXT_NOT_APPLICABLE、CONTEXT_REQUIRED、原INVALID_SHAPE，均先于第5步值错误 |
| storage.database_file缺必需validator且default换为LiteralDefault；另次只换默认或数值闭区间端点 | 分别先REQUIRED_VALIDATOR_MISSING、PERSISTENCE_DEFINITION_MISMATCH；路径仍不回显定义或值 |
| 额外可选依赖缺值，同时存储路径非法；补齐依赖后重调 | 先DEPENDENCY_VALUE_MISSING，后PATH_SYNTAX_INVALID；无部分成功，不靠清空dependencies通过 |
| 固定验证器抛普通异常；成功后调用方修改原输入和目录清单 | 前者仅VALIDATOR_FAILED且旧快照不变；后者已发布快照及嵌套数据不变。两者均不读取环境或资源 |

本节补充契约已批准、尚未实现，现有[配置公开入口](../../companion_memory/configuration/__init__.py)、[普通解析](../../companion_memory/configuration/resolution.py)、[日志附加解析](../../companion_memory/configuration/checked_resolution.py)尚无此能力；以上接口静态核对不代表执行通过。当前授权及停止点见[CURRENT_TASK](../work/CURRENT_TASK.md)，不开始实现或自动注册参数。

<a id="configuration-provider-validation-contract"></a>

### 11.13 Provider基础服务所需配置校验：已批准契约

**状态：契约已批准。** 本节随[Provider整体F5](provider.md#provider-foundation-decisions)由主会话按用户授权审查批准，不表示用户亲自验收或实现通过。本节是新配置的唯一详细规格；不改§11.9–11.12已批准正文、现有三个解析入口或旧错误枚举。本次只有纯内存校验及消费者适用性检查，没有加载、激活、配置版本库、secret引用解析或生产默认。这里的SIMULATED／TEST明确为模拟协议和合成金额；真实HTTP／价格／凭据的配置不符合本子集，不能静默退回模拟执行。

#### 11.13.1 独立入口与适用性

已批准新增`resolve_configuration_with_provider_validation(registry, explicit_values, protected_directories) → ProviderResolutionResult<EffectiveSnapshot>`，三项均显式传入；成功为ProviderResolutionOk，失败为ProviderResolutionErr。输入精确载体、完整集合、未知键／深不可变／输入稳定要求沿用[§11.10.2](#configuration-resolution-input)。始终要求本节Provider完整10键和§11.12完整storage／audit10键；任一logging.注册键触发原完整日志组和目录上下文，否则第三项恰为None。日志目录清单中的provider_usage须覆盖本次同库账本所属目录，资源真实性仍由可信装配负责。

普通入口仍拒绝非空validator／dependencies；日志入口仍只接受原四项验证器且要求完整日志组；持久化入口仍只接受原五项且按原顺序检查。均不得转调新入口、扩大白名单、剪掉Provider声明后解析，或因provider未启用而跳过其定义。新入口保留和检查所有额外注册参数，只增本节固定能力。

同时新增只读`provider_snapshot_issue(snapshot)`供Provider初始化：结果None表示本子集适用，或固定SNAPSHOT_REQUIRED／DEFINITION_MISMATCH／CAPABILITY_MISSING／VALUE_INVALID。先核验原生EffectiveSnapshot，再全部必要定义（Provider和storage／audit，触发时含日志），再必要能力及依赖声明，最后完整有效值和跨参数关系。实现仅用配置公开快照查询及模块内共享固定验证逻辑；不重新解析／发布快照，不返回ID、证书、默认或资源授权。目录上下文不保存在快照，适用性检查不能重造它或证明物理安全；日志／存储各自资源绑定继续核验。

<a id="configuration-provider-definitions"></a>

#### 11.13.2 完整定义、嵌套字段和匹配

Provider十项均required=true、nullable=false、default=NoDefault()，owner_module=provider、consumers至少含provider；scope=[instance]、override_policy=no_override、sensitivity=public、read_roles/write_roles=[trusted_operator]、apply_mode=INITIALIZE_ONLY、activation_group=NotApplicable，deprecated=false、replacement/upgrade_rule=NotApplicable。schema_revision只校验合法Identifier，不当配置值版本；说明字段及NotApplicable.reason按原非空Text规则，不逐字匹配，不从说明文字执行逻辑。所有值仅为明确无秘密的内部标识、有限数量和合成价格，不能存URL、凭据或文件路径。

| key | type／unit | 完整range或验证器／必要dependencies |
| --- | --- | --- |
| provider.max_in_flight | integer／requests | 闭区间1–8；provider_resource_limits；依赖provider.result_max_bytes、storage.command_max_bytes、storage.receipt_max_bytes |
| provider.request_timeout_ms | integer／milliseconds | 闭区间1–60000 |
| provider.close_timeout_ms | integer／milliseconds | 闭区间1–60000 |
| provider.retry_delay_ms | integer／milliseconds | 闭区间0–60000 |
| provider.request_max_bytes | integer／bytes | 闭区间256–1048576 |
| provider.result_max_bytes | integer／bytes | 闭区间256–8192 |
| provider.query_row_limit | integer／rows | 闭区间1–1000 |
| provider.accounts | array／NotApplicable | provider_accounts；依赖provider.max_in_flight |
| provider.profiles | array／NotApplicable | provider_profiles；依赖provider.accounts、provider.request_timeout_ms |
| provider.role_profiles | object／NotApplicable | provider_role_profiles；依赖provider.profiles |

全部enum为NotApplicable；array／object的range为NotApplicable，由固定验证器验证嵌套结构。元信息比较按§11.12.2相同规则：必需键按排序核验，缺必要validator／dependency用专用原因，其余key／type／owner／单位／范围／默认／角色／生效模式精确匹配，consumers只要求包含必要消费者。dependencies允许额外已注册项、自引用、前向和声明环；仍检查值存在，不强制等于最小列表。定义不能用相近范围或本次显式值掩盖错误默认。

嵌套结构如下；所有列均必填，None只在明确列出的条件允许，未知字段拒绝。标识统一为精确Identifier，整数是精确int且不得用bool代替，枚举是精确str。解析前数组接受原精确list／tuple，隔离后tuple；对象接受精确dict，隔离后自有只读映射。固定验证器不接受任意参数名或动态协议字段。

| 值 | 完整字段与规则 |
| --- | --- |
| accounts | 1–4项，account_id唯一；每项恰含account_id、window_id（真实合成业务标识）、currency（仅TEST）、max_in_flight（1–8且≤实例上限）、attempt_limit（1–1000000）、cost_limit_atoms（0–10^12）。无自动周期／重置／真实账单功能 |
| profiles共同字段 | 1–16项，profile_id唯一；每项恰含profile_id、account_id、model_id、wire_protocol（仅SIMULATED）、capability（GENERATION／EMBEDDING／RERANK／MEDIA_UNDERSTANDING）、max_attempts（1–4，含首attempt）、attempt_timeout_ms（1–60000且≤总请求配置期限）、max_input_units（1–1048576）、max_output_units（见下）、max_items（1–64）、input_price_atoms（0–10^6）、output_price_atoms（0–10^6）、dimensions、space_id、media_tasks。account_id须引用accounts中实际项；不自动建账户、不因多个profile复制额度 |
| generation特定值 | max_output_units=1–1048576；dimensions／space_id=None，media_tasks为空tuple；max_items限定消息数，max_input_units的模拟字节口径见[请求协议](provider.md#provider-foundation-ports) |
| embedding特定值 | max_output_units=0、output_price_atoms=0；dimensions为1–1024，space_id为非空Identifier，media_tasks为空tuple；max_items限定文本条数，max_input_units也须≥max_items。空间和维度显式固定，不做跨空间降级 |
| rerank特定值 | max_output_units=0、output_price_atoms=0、dimensions／space_id=None、media_tasks为空tuple；max_items限定候选数，max_input_units须≥max_items |
| media特定值 | max_output_units=0、output_price_atoms=0、dimensions／space_id=None、max_items=1；media_tasks为1–3项互不重复的固定记录，每项恰含modality和task，允许组合仅IMAGE＋DESCRIBE、AUDIO＋TRANSCRIBE、VIDEO＋DESCRIBE；max_input_units限定bytes |
| role_profiles | 1–8个成员，键仅LEARNING／DREAM／PERSONA／MEDIA／EMBEDDING／RERANK／GOAL／DIAGNOSTIC；值为1–16个互不重复的已注册profile_id。映射仅是可选profile配置，不签发角色或权限；工作句柄允许集合与本映射取交集，调用方不能靠添加键获得授权 |

金额乘法`max_input_units × input_price_atoms + max_output_units × output_price_atoms`必须≤2^63−1，不能float转换或隐式舍入；TEST/atoms表示只在[Provider预算契约](provider.md#provider-foundation-gates)解释。上述上界是本地支持范围，没有生产推荐值。配置明确不提供config_snapshot_id/profile_revision/price_revision；缺失与实际字段证据按[Provider身份契约](provider.md#provider-foundation-identity)分别表达，不能通过合法schema_revision冒充配置持久版本。

#### 11.13.3 固定验证器与存储容量衔接

新入口白名单恰为原五项加下列四项；精确绑定，不动态注册、替换、执行表达式或调用用户回调。执行只读已隔离候选值及显式依赖，无文件／环境／时钟／网络／日志访问，无派生配置或默认补全。

| 标识／唯一绑定 | 检查及固定失败原因 |
| --- | --- |
| provider_accounts／provider.accounts(array) | 完整accounts结构、唯一ID、范围／币种及并发关系；失败ACCOUNTS_INVALID |
| provider_profiles／provider.profiles(array) | 完整profiles结构、引用／能力专属字段、金额乘法和时限关系；失败PROFILES_INVALID |
| provider_role_profiles／provider.role_profiles(object) | 完整角色集合、非空已注册profile映射、无重复；失败ROLE_PROFILES_INVALID |
| provider_resource_limits／provider.max_in_flight(integer) | storage.command_max_bytes与storage.receipt_max_bytes都须≥57344，且`6 × provider.result_max_bytes + 8192 ≤ storage.receipt_max_bytes`；失败STORAGE_CAPACITY_INSUFFICIENT |

容量下限为有界文本最坏JSON转义及固定信封预留空间的保守接口条件：执行配置证据上限8192 UTF-8字节，单次结果payload上限取result_max_bytes；回执仅保存引用，查询含正文时仍受持久化行编码限额。实现须对完整编码再验证，不以估算放行超限数据；文本上限不赋予审计正文权限。请求文本不写通用命令，语义指纹在Provider隔离后生成；超大输入拒绝而非截断。格式字段和Provider编码需保证固定信封≤8192字节，否则归为实现错误并安全失败，不能隐式提高配置限额。

每个Provider验证器在依赖值存在且自身基础校验成功后执行；依赖结构本身不合法时安全报当前固定失败，不读未知字段或解释不受支持对象，错误顺序不靠配置字典输入顺序碰运气。原日志／持久化验证器行为和路径清单保持各自原规则。非预期普通异常／非法返回统一VALIDATOR_FAILED；原资源耗尽和进程控制边界不变。

<a id="configuration-provider-errors"></a>

#### 11.13.4 全集合顺序与固定错误

严格顺序：registry及explicit_values精确载体 → 全部顶层键格式 → 全部未知键 → 全注册集合按排序检查九项白名单／精确绑定及原支持边界 → storage／audit完整匹配 → Provider十键完整匹配／必要依赖 → 触发时原日志组完整匹配 → 原条件目录上下文 → 全集合值选择／树安全／类型／range／enum与隔离 → 全集合依赖存在性 → 按定义排序和validator声明顺序执行验证器 → 一次发布原生EffectiveSnapshot。前一步失败不访问后续输入；无部分快照、注册表／旧值／全局状态变化。

ProviderResolutionError仅含code、operation、issues；operation固定resolve_configuration_with_provider_validation，issues恰含一个不可变(field_path, reason)。独立于原配置结果和Provider运行错误，不嵌套原错误或输入引用。允许集合封闭如下，引用只复用现有固定项，不向未来枚举自动开放：

| code来源／新增code | 固定reason与安全路径 |
| --- | --- |
| [§11.12.5](#configuration-persistence-errors)完整配置错误并集 | 复用其固定原因和路径，operation换新入口；validator白名单按本节扩充但旧入口不变。签名和get_entry仍用原协议 |
| INVALID_PROVIDER_SCHEMA | PROVIDER_DEFINITION_MISSING、PROVIDER_DEFINITION_MISMATCH；`("provider_definitions", k)`，k为本节十键排序下标。缺必要validator／dependency仍优先REQUIRED_VALIDATOR_MISSING／REQUIRED_DEPENDENCY_MISSING及原definitions路径 |
| ADDITIONAL_VALIDATION_FAILED新增原因 | ACCOUNTS_INVALID、PROFILES_INVALID、ROLE_PROFILES_INVALID、STORAGE_CAPACITY_INSUFFICIENT；`("definitions", j, "value")`，不追加嵌套用户字段／profile／账户ID。VALIDATOR_FAILED和缺依赖值继续复用原表 |

精确载体失败不调用repr、比较、迭代、转换或自定义哈希钩子；路径仅含固定字段名和整数下标，不含原键、值、默认、异常、路径、账户、模型或原对象。完整深不可变及并发只读沿用原快照契约；配置角色字段只保存声明，没有鉴权副作用。

#### 11.13.5 合成检查预期

共用Provider示例见[整体矩阵](provider.md#provider-foundation-acceptance)；storage.command_max_bytes显式65536、storage.receipt_max_bytes显式65536，其他storage／audit和可选日志值按既有完整规格显式提供，路径仅自有合成资源。这些例子保持验收预期，实际执行覆盖和结果见[CURRENT_TASK](../work/CURRENT_TASK.md)。

| 组合 | 预期首个结果 |
| --- | --- |
| Provider＋storage／audit完整、无日志、None上下文；另加完整日志及真实布局对应合成目录 | 全集合通过才完整快照；无假版本／秘密／资源就绪声明 |
| 额外参数未知validator、三组同时缺定义且上下文非法 | 先UNKNOWN_VALIDATOR；修复后依次按storage／audit、Provider、日志组顺序报缺项，再到目录，不省略未使用定义 |
| accounts重复ID／错误币种、profile未知账户／错误能力字段／欠缺必填None字段、role映射未知profile | 各固定附加错误；无自动账户生成／协议降级／默认配置 |
| 将金额传bool／float、改元信息范围／默认、storage回执容量不足、关闭输出但日志组残缺 | 分别安全树／固定附加校验、定义不符、STORAGE_CAPACITY_INSUFFICIENT、原日志缺定义；无不完整成功 |
| 额外依赖值缺失、实际嵌套值也非法；旧三个入口提交新的声明；成功后深层修改源容器 | 缺值优先附加验证；旧入口各维持原UNKNOWN_VALIDATOR或既有不支持原因；新快照稳定不可变 |

新增配置入口和纯内存适用性检查属于已批准整体契约的实现内容；不把生产版本／G2等范围外设施作为伪造配置字段的理由。

<a id="ingress-runtime-configuration-draft"></a>

### 11.14 持久接入与批次运行所需配置补充（已批准）

**状态：本节推荐方案及完整显式参数集已获用户批准。** 本节属于[持久接入与批次运行整体契约](durable-ingress-and-batch-runtime.md)，不修改§11.9–11.13批准内容或现有四个解析入口。参数、作用域和持久配置身份只在本节维护；集中决定见[整体批准表](durable-ingress-and-batch-runtime.md#decisions)。所有选定数字均为显式合成配置，非生产默认；新参数一律`NoDefault()`。执行授权及验证进度见CURRENT_TASK。

#### 11.14.1 能力缺口与独立组合入口

现有普通解析只支持instance／no_override／public并拒绝validator／dependencies；三个独立增强入口分别增加日志、持久化及Provider固定校验，仍不执行平台作用域、持久版本或热激活。EffectiveSnapshot是可信配置模块签发的内存结果，没有可持久身份；`schema_revision`、Python对象身份、内容摘要或随机ID不能替代配置版本。

新增独立入口`resolve_runtime_configuration(foundation, runtime, platforms, protected_directories, material_contracts)`，只作完整纯内存校验，返回`RuntimeConfigurationOk(ConfigurationCandidate)`或`RuntimeConfigurationErr(error)`。五项均须显式传入，不读文件／环境／数据库，不做权限检查或联网；material_contracts是推荐预算相容方案所需的已批准输入增量，不修改既有四个解析入口。仅运行时超限方案未采纳，不能以None绕过此输入及相容保障。输入分组是实际配置域的边界，由调用方明确提交，不能从一个不支持的注册表中剔除字段取得成功：

| 输入域 | 载体与范围 |
| --- | --- |
| foundation | 精确记录，含原生冻结registry和精确explicit_values；完整保留§11.13的Provider、storage／audit及条件日志组，按原Provider校验解析为原生EffectiveSnapshot |
| runtime | 同样含冻结registry及explicit_values；仅本节实例级接入／调度／观察定义及受支持额外定义；新原生RuntimeSettingsSnapshot包含全部条目，不冒充EffectiveSnapshot |
| platforms | 非空有界列表，每项含内部platform_id、该平台冻结registry及explicit_values；平台集合由可信接入装配登记，唯一、不按输入消息动态添加。生成PlatformSettingsSnapshot，绑定精确platform_id |
| protected_directories | 原条件目录上下文；日志组启用时完整非空五类清单；只用于foundation解析和跨域资源描述核对，不变成另一份路径配置 |
| material_contracts | 推荐相容方案下为与platforms一一匹配的可信原生、不可变材料能力声明集合，绑定参与者／格式、模板及有限上界规则版本；无额外／缺失平台，不从消息或动态插件取得。声明仅有受支持的固定有限规则及有界参数，不含可执行回调或模板正文；相容性检查见§11.14.4 |

输出ConfigurationCandidate包含上述完整不可变结果及域／定义格式清单，尚无生效指针、持久revision或snapshot_id；不能直接作为可恢复批次配置。三个域有独立所有者和显式结构，无同名键跨域覆写。新组合入口核验**整个**输入集合后一次交付，任何一域失败不发布其他域；旧入口不转调新入口，旧错误／拒绝语义保持。内部可复用已有固定校验器，但不得清空真实validator／dependencies、伪造scope或构造私有快照。

平台键模板为`platforms.<platform_id>.buffer.<name>`，在可信注册时展开为有限确定键，平台ID仍作为快照作用域绑定；不是用户提供字符串的动态查找规则。scope精确为[platform]，override_policy=no_override，无实例继承、入口覆写或部署强制覆写。不同平台可明确提交不同值；一个平台值集只适用于其入口。缺平台域就拒绝相关入口启动，不能借另一个平台的值补齐。完整层级合并和运行中编辑平台参数不在本子能力内。

新RuntimeSettingsSnapshot／PlatformSettingsSnapshot只由该入口签发，具有`get_entry`、`list_entries`、`get_registry`及平台绑定只读访问；精确值、缺失、null、深不可变和拒绝对象钩子沿用原解析原则。旧基础服务只接原foundation EffectiveSnapshot；运行／缓存使用新配置模块签发的绑定视图，不向旧服务塞入新validator的注册表。三个域由统一配置所有者统一管理，不是业务模块私有副本。

<a id="runtime-envelope-parameters"></a>

#### 11.14.2 完整新参数及推荐显式值

以下全部required=true、nullable=false、default=NoDefault()，sensitivity=public仅用于非秘密测试值；read_roles／write_roles=[trusted_operator]，deprecated=false，replacement／upgrade_rule=NotApplicable。标识／说明载体和精确类型匹配沿用§11.9；说明文字不作为执行表达式。range均闭区间，integer拒绝bool；boolean的range／enum为NotApplicable。平台参数apply_mode=NEXT_BATCH（本子能力无热改入口），其他参数INITIALIZE_ONLY；activation_group=NotApplicable，override_policy=no_override。schema_revision只验证定义标识，不能当值版本。consumers至少包含所列使用者，附加声明不授予权限。

| 键（平台行省略`platforms.<id>.`前缀） | 类型／单位／合法范围 | 推荐显式值（已批准） | owner／最小消费者 |
| --- | --- | --- | --- |
| ingress.event_max_bytes | integer／bytes／256–8192 | 1024 | ingress／ingress |
| runtime.max_active_entries | integer／entries／1–8 | 1 | runtime／runtime |
| runtime.operation_timeout_ms | integer／milliseconds／1–60000 | 5000 | runtime／runtime、ingress、buffers |
| runtime.recovery_timeout_ms | integer／milliseconds／1–60000 | 30000 | runtime／runtime |
| runtime.close_timeout_ms | integer／milliseconds／1–60000 | 5000 | runtime／runtime |
| runtime.focus_drain_timeout_ms | integer／milliseconds／1–60000 | 30000 | runtime／runtime |
| runtime.claim_lease_ms | integer／milliseconds／1–60000 | 15000 | runtime／runtime；只检测失联，不自动夺权 |
| runtime.local_retry_limit | integer／attempts／0–1 | 1 | runtime／runtime、ingress、buffers；额外本地尝试次数 |
| runtime.read_page_size | integer／rows／1–128 | 16 | runtime／runtime、buffers |
| runtime.transfer_page_size | integer／rows／1–128 | 16 | buffers／buffers |
| learning.material_max_bytes | integer／bytes／256–8192 | 8192 | cognition／runtime、buffers、cognition |
| learning.input_units_limit | integer／simulated_input_units／256–1048576 | 8192 | cognition／cognition、runtime |
| learning.output_units_limit | integer／simulated_output_units／1–8192 | 1024 | cognition／cognition、runtime |
| buffer.history_context_count | integer／messages／0–128 | 1 | buffers／buffers |
| buffer.recent_context_count | integer／messages／0–128 | 1 | buffers／buffers |
| buffer.target_count | integer／messages／1–128 | 2 | buffers／buffers |
| buffer.normal_soft_limit | integer／messages／1–1000000 | 1000 | buffers／buffers、runtime；只作软水位，无自动删除 |
| buffer.explicit_short_enabled | boolean | false | buffers／buffers、runtime |
| buffer.idle_tail_enabled | boolean | false | buffers／buffers、runtime |
| buffer.idle_timeout_ms | integer／milliseconds／0–86400000 | 0 | buffers／buffers、runtime；关闭时恰为0，开启时正值 |

本表推荐候选现取E=1024、H/T/R=1/2/1、材料限额8192，其他本表值保持；这是用户明确选定的合成配置，不是解析时自动调整旧值。格式依据及完整代入见[§11.14.6](#runtime-compatible-budget-candidate)。原E=4096、H/T/R=2/8/2、材料限额6144仅保留为[超限反例](../product/decisions-and-delivery.md#runtime-context-overflow-options)，不再与推荐列混用。本节推荐候选已批准，静态相容不代表实际初始化或执行通过。

学习输入／输出单位与[当前Provider模拟计量](provider.md#provider-foundation-ports)一致，不能称为真实token估算。计量取Provider消息text的UTF-8字节总和；模板及三段原文、参与者渲染进text的角色标签都计入，不能将独立role枚举冒称为已计量文本。发送前与选定GENERATION profile的能力和上限核对。真实tokenizer、真实模型预算及更大的输出格式须另行扩充，不通过忽略token限制来宣称支持生产。这里只提出合成材料／模板及相关模拟profile的完整容量候选，定义见[材料唯一正文](durable-ingress-and-batch-runtime.md#synthetic-window-material)及[参数代入](#runtime-compatible-budget-candidate)；不指定真实模型、生产价格或真实认知模板。

新接收事件版本的拟议**格式硬上限**为：外部标识／显示名各512 UTF-8字节；单事件引用和媒体各至多16项；extensions至多16个已登记字段、容器深度至多4层，值只限布尔、64位整数、有界字符串及声明的有限数组／记录；事件完整确定性编码不超过8192字节。内部ID沿用Provider安全ID规则。三段总成员至多128；平台域数至多64；每项配置参数的定义／有效值／来源持久编码至多8192字节，域版本保存条目计数及内容摘要，逐项保存完整记录；组合目录只存域版本引用不塞全部值。硬上限用于格式兼容和历史有界读取，也是本契约已批准内容；不是可由消息覆盖的参数，不代表生产容量承诺。事件当前新写限额取event_max_bytes，旧原事实读取取其格式上限。

<a id="runtime-observation-parameters"></a>

#### 11.14.3 观察参数

共同元信息同上，scope=[instance]、INITIALIZE_ONLY；观察不会因Schema中read_roles声明就自动获权，实际端口按[管理边界](durable-ingress-and-batch-runtime.md#observation)执行。

| 键 | 类型／单位／合法范围 | 推荐显式值（已批准） | owner／消费者 |
| --- | --- | --- | --- |
| management.observation_row_limit | integer／rows／1–128 | 32 | management／management、runtime |
| management.observation_max_bytes | integer／bytes／1024–65536 | 32768 | management／management、runtime |
| management.observation_timeout_ms | integer／milliseconds／1–60000 | 2000 | management／management、runtime |
| management.observation_concurrency | integer／requests／1–16 | 2 | management／management |
| management.refresh_min_interval_ms | integer／milliseconds／100–60000 | 1000 | management／management |
| logging.web_window_events | integer／events／1–65536 | 512 | logging_service／logging_service |
| logging.web_query_row_limit | integer／rows／1–128 | 32 | logging_service／logging_service、management |
| logging.web_query_max_bytes | integer／bytes／1024–65536 | 32768 | logging_service／logging_service |
| logging.web_query_timeout_ms | integer／milliseconds／1–60000 | 1000 | logging_service／logging_service |

本节logging.web_*归新runtime配置域，不能单独送进旧日志解析入口；foundation域仍按原规则保留完整日志组。新组合入口要求foundation存在完整且启用的实际日志服务配置，才可启用本次Web窗口；不减免旧日志组检查。Web等级直接继承该日志服务的已批准模块采集阈值，最小窗口不另提供等级热编辑或事件正文捕获开关。

窗口最大内存以`web_window_events × 原日志event_max_bytes`加固定有界索引计，不动态无限增长；JSON／HTTP响应必须再校验完整编码字节。窗口容量和返回行数分别约束存留与一次读取，不能把一次查询上限当诊断丢弃策略。客户端速率限制按认证会话执行，连接总并发由同一观察服务限制；无客户端通过重连绕过实例容量的承诺，生产反滥用另定。

<a id="runtime-batch-validation"></a>

#### 11.14.4 固定校验、容量与错误

新runtime及platform域的固定验证器白名单为`runtime_limits`、`batch_windows`、`batch_triggers`、`observation_limits`，分别绑定runtime.max_active_entries、各平台buffer.target_count、各平台buffer.idle_tail_enabled、management.observation_max_bytes；必需依赖声明覆盖各自表内读取的新参数。foundation跨域依赖由组合入口显式校验，不伪造为另一个registry中已注册的普通依赖。额外注册依赖仍按原“必须有值”语义核验，不擅自改变自引用／前向／声明环规则；未知验证器、错误scope、错误绑定或缺必要定义拒绝，无运行时回调。

| 校验 | 拟议固定规则 |
| --- | --- |
| 批次窗口 | H、R非负、T正，H＋T＋R≤128；normal_soft_limit≥T＋R。H大于实际目标时只保留实际尾部，不制造消息。按已批准预算相容方案，还须通过每平台完整窗口的最坏材料／单位校验；结构合法不等于可发送 |
| 触发 | idle关闭时timeout=0；开启须timeout>0；显式短批与空闲尾部独立。策略的具体切分在产品正文唯一维护 |
| 工作及期限 | runtime.max_active_entries≤provider.max_in_flight；持久化操作与Provider保留原各自期限，外层等待取剩余时间，不修改旧端口或把timeout当取消底层I/O |
| 事件／材料 | 完整事件新写≤event_max_bytes；完整三段引用及读取有格式上限；实际模型材料≤material_max_bytes，输入单位总量≤input_units_limit，输出单位≤output_units_limit，均不得超过所绑定profile限制 |
| 存储容量 | 原Provider容量条件必须同时满足。事件点读／文本域读取按最坏JSON转义：`6 × max(event_max_bytes, material_max_bytes, 8192) + 8192 ≤ storage.receipt_max_bytes`及storage.command_max_bytes。当前域、清单、元信息页与所有审计／回执在完整编码后再次核验，不靠估算放行 |
| 分页 | 状态／回流页只含内部ID、修订及计数，逐条原文点读，不在一页内返回page_size份最大原文。每个固定查询有结果Schema和总编码上限；超限明确LIMIT_EXCEEDED，可按原授权减少页大小再读，不裁断单条原事实 |
| 观察 | log row_limit≤management row_limit；log query_max_bytes≤management max_bytes；两个timeout≤对应外层观察总期限。完整结果装不下即明确超限，不显示伪完整截断页 |
| 来源及版本 | 批次必须绑定配置模块签发的已持久快照；域版本缺失、格式／内容不一致拒绝，不退回默认。无本地能力时拒绝真实模型单位／secret／生产路径解读 |

三段／预算匹配在冻结前及发送前分别核验：冻结不能为了凑上限删掉原始输入、减少已固定辅助范围或跨过最旧目标；过大的单条／窗口阻止该入口并显示CONTEXT_LIMIT，下一入口仍可调度。完整来源引用不因模型预算而截断。本次采用以下初始化准入约束；持续阻塞反例及未采纳替代仍在[产品选项](../product/decisions-and-delivery.md#runtime-context-overflow-options)维护。

推荐的**接收限额与窗口预算相容校验（已批准）**：每个平台令E为event_max_bytes、N为H＋T＋R；可信装配中固定版本的材料格式／合成参与者须给出并可验证单事件在完整材料中的最坏贡献W(E)、固定模板等开销B，以及相应Provider模拟输入单位上界Q(N,E)。W须覆盖允许的正文、引用、角色标记和编码展开，不能直接假设W(E)=E；不支持的媒体仍按能力拒绝。要求B＋N×W(E)≤learning.material_max_bytes，Q(N,E)≤learning.input_units_limit≤绑定GENERATION profile.max_input_units，输出限额与profile匹配，消息项数同时不超过profile.max_items。满历史为最坏窗口，首次空历史不能代替此校验。

B／W／Q是受信任且版本化的材料协议能力描述，不是用户事件、自报hash、可执行配置或另一套默认参数。ConfigurationCandidate及持久组合快照元数据须保存material_contract_ref，明确参与者／格式、模板和上界规则版本；声明由可信静态装配重新提供，恢复时逐项匹配并重验，不持久化可执行对象。描述须绑定批次模板／参与者版本并随配置引用可恢复；无描述、版本不支持、无法证明有限上界或关系不成立，使用现有草案错误VALUE_INVALID／BUDGET_INVALID、operation=resolve_runtime_configuration、field=platforms，整组失败原子。它作为新域固定／跨域关系检查的一项，沿既有同层稳定顺序执行，不改变原四解析入口。

该约束会拒绝保留的原超限反例；本次提交[确定候选](#runtime-compatible-budget-candidate)作为已批准合成配置，不再只给“以后调小参数”的建议。其他组合仍可在现有合法范围内明确提出并核算，但不得自动降低接收限额或改窗口。本范围材料硬上限仍8192字节，不能为了让原数值通过就私自升限。允许超限的替代方案未采纳，不提供关闭最坏预算准入的入口；反例仅用于说明持续阻塞与拒绝场景。

现有已持久配置没有更新入口。跨进程恢复必须恢复原值与原策略版本；新增相容规则不反向改写旧配置，也不把重新解析成功当作原阻塞输入已处理。按旧策略建立而已发生阻塞的同库输入，需另有批准的配置／业务处置或迁移方案才能改变条件；本范围不提供该解除操作。

错误是新RuntimeConfigurationError，恰含code、operation、field、reason；不扩充旧错误枚举。operation为resolve_runtime_configuration、persist_initial_configuration、load_configuration_snapshot或runtime_snapshot_issue。field限foundation/runtime/platforms/definition/value/context/storage/identity。code／reason集合：INVALID_INPUT（INVALID_SHAPE、UNKNOWN_KEY、DUPLICATE_PLATFORM、LIMIT_EXCEEDED）、UNSUPPORTED_CAPABILITY（SCOPE_UNSUPPORTED、VALIDATOR_UNSUPPORTED、FOUNDATION_UNSUPPORTED）、DEFINITION_MISMATCH（REQUIRED_DEFINITION、METADATA_MISMATCH、DEPENDENCY_REQUIRED）、VALUE_INVALID（RANGE_INVALID、DEPENDENCY_MISSING、WINDOW_INVALID、TRIGGER_INVALID、BUDGET_INVALID、CAPACITY_INSUFFICIENT）、PERSISTENCE_FAILED（NOT_COMMITTED、UNCONFIRMED、READ_FAILED）、INTEGRITY_FAILURE（VERSION_MISSING、CONTENT_MISMATCH、FORMAT_UNSUPPORTED）、ACCESS_DENIED（BINDING_MISMATCH）。持久端口仍须配原提交证据，错误不单独证明事务全无。

固定顺序为：全部精确载体／键安全 → 域身份和未知键（含材料能力平台集合一一匹配）→ 全域声明／支持边界 → 必需完整定义 → foundation原校验（失败映射FOUNDATION_UNSUPPORTED或VALUE_INVALID，保留固定类别不携带原值）→ 新域完整值选择和深隔离 → 全部依赖存在性 → 新域固定校验及跨域关系 → 一次返回候选。同层按域kind、platform_id、key排序；字段使用固定标签，不回显外部平台名、路径和非法键。只读适用性`runtime_snapshot_issue`验证原生身份、全定义／值及可恢复引用，不重做配置解析或资源授权。

<a id="runtime-configuration-identity"></a>

#### 11.14.5 配置所有者的持久版本与恢复

推荐增加配置模块专属仓储，沿用受限UoW，不另开配置数据库。它持久保存：`configuration_domains(kind, scope_id)`、每域不可变`revision`及完整有界定义／有效值／值来源、组合`configuration_snapshots`及域版本清单、`active_configuration`指针和必要审计。scope为实例或明确平台ID；foundation、runtime、platform分别是不同kind。revision为该库该域事务分配的递增整数；首次为1，后续版本协议可解释但本子能力不开放更新。`config_snapshot_id`由配置仓储以持久序列分配并同清单提交；其意义来自可查回的完整版本映射，不能由调用方传入随机ID占位。

每域版本的参数子记录包含独立值格式版本、该项完整定义、完整解析后值和EXPLICIT／DEFAULT来源，按域／revision／key唯一；域元数据保存完整清单计数和按key排序的内容校验摘要。摘要仅校验损坏，schema_revision仅解释定义，二者均不是revision。必要定义、值、来源及组合映射必须能在新解释器恢复；不能只持久摘要或给每批保存一份业务私有配置。全配置初始化命令的完整编码仍须小于storage.command_max_bytes，超过则整组拒绝，不分多个独立有效提交或静默提高限额。日志目录上下文／资源授权、认证凭据、ID源和文件句柄不进入配置值；它们由可信装配另行重建和核验，见[存储装配补充](persistence-and-transactions.md#ingress-runtime-storage-draft)。

`persist_initial_configuration(operation_key, candidate)`只交可信初始化者，且仅完整新装配、尚无active指针时允许。一次UoW保存全部域、组合快照、有效指针和脱敏审计；无一半有效的平台列表。revision和config_snapshot_id均为事务派生值：初始化前冻结完整候选、域清单、操作者／固定理由和规则版本，使用[结果绑定命令增量](persistence-and-transactions.md#runtime-result-bound-audit)，不得预读序列来填旧audit_events。结果中保存该次域revision列表、snapshot_id和active引用，必要审计由同一结果投影；不把配置正文／路径写入审计。返回COMMITTED后配置模块才签发`StoredRuntimeConfiguration`，其中foundation为经该模块恢复的原生EffectiveSnapshot，runtime／platforms为原生绑定视图，并可按具体入口取得`BatchConfiguration`。批次只持该只读视图及真实config_snapshot_id／域revision引用。

在任何初始化副作用前，可信调用方保留原operation_key、完整候选及稳定审计意图。建库后配置COMMIT已成功而任何返回／句柄未送达时，先完成原存储OPEN_EXISTING全校验，再以这些原材料调用recovery_handle／resolve_operation查回原revision／snapshot_id；无需先知道这些派生值。确认后按原引用加载、完整校验并签发视图。可靠未提交时才同键补齐，未知保持RECOVERING；修改候选或操作者／理由但复用原键仍冲突，不能把“active存在”当成省略内容检查的捷径。

已存在active时同键同内容返回原初始化回执；其他新键不能再初始化覆盖配置。没有在线patch、热发布、回退、默认重导入或批次中途换值接口。NEXT_BATCH元信息的长期含义保持；本子能力生命周期内配置不变，不能把“没有实现热改”描述成已实现下一批激活。测试可在不同新库中使用不同明确配置，不直接改活跃库来伪造热切换证据。

OPEN_EXISTING中`load_configuration_snapshot(snapshot_id)`只按可信绑定库及受限配置身份读取。检查组合清单、域版本和摘要、受支持定义格式，重建相同注册表及有效值／来源并重新验证，匹配后才签发原生视图；定义旧版本不受当前实现支持则CONFIGURATION_UNRECOVERABLE，不能偷偷改按新默认解释。引用中的snapshot不存在或域缺行是完整性故障。配置版本、在用快照和对应定义随验证库保留，无清理／压缩端口。

建库前必须先取得foundation中存储打开所需的最小已校验输入，解决配置库尚未打开的启动依赖：这些仍由统一配置入口解析，可信装配只保留该目标绑定及expected_database_id。首次建库后提交完整配置；若基础格式已完整建成且本次存储初始化已返回READY，但配置初始化未提交，保持RECOVERING，由原操作键与原输入确认／补齐初始化，不把空配置当默认NORMAL。若存储自身initialize仍失败／未知，必须先按[分层恢复](durable-ingress-and-batch-runtime.md#runtime-startup-layers)处理；配置补齐和运行检查点均不能让未完成的存储校验继续或跳过。重启打开后，bootstrap实际存储目标及影响资源的foundation值必须与已持久版本相符；不符拒绝，不拿启动参数覆盖active。生产保留这种启动身份／路径信息的方式另按生产前置批准。

Provider当前已批准服务仅支持无版本模拟装配，因此本子能力不修改其`configuration_origin=UNVERSIONED_CONFIGURATION`、`config_snapshot_id/profile_revision/price_revision=None`及原执行证据协议。新的**批次**真实配置身份属于运行／配置关联，记录在批次及配置仓储；通过Provider既有batch_id／run_id可定位，Provider接收的foundation值由配置模块从该持久域恢复。不能把这些新ID硬填到未支持字段，不能将None解释为“批次也无版本”，也不能反过来宣称Provider已支持持久profile／价格版本。若未来要求Provider端直接携带版本，须独立扩展其配置契约及兼容，而不是在本次草案暗改。

本节的纯内存解析、实际配置仓储、同事务初始化／确认和跨进程恢复均须随[整体矩阵](durable-ingress-and-batch-runtime.md#acceptance)验证；推荐值与产品策略已批准；实际验证及执行状态见CURRENT_TASK。

<a id="runtime-compatible-budget-candidate"></a>

#### 11.14.6 接收与完整窗口相容的确定候选（已批准合成配置；代入为静态证据）

本候选针对一个受控合成平台的固定阈值学习，材料声明精确使用[合成协议及上界依据](durable-ingress-and-batch-runtime.md#synthetic-window-material)。材料／模板文字、事件编码、B／W／Q公式只在该处维护，不能由这里的参数任意替换。其他平台须一一绑定声明并独立检查；不以单平台计算证明64个平台全部配置内容都装得下。

| 候选范围 | 确定值／沿用来源 |
| --- | --- |
| 接收与三段 | §11.14.2推荐列：E=1024，H=1、T=2、R=1；T仍是正常阈值固定目标数，N=4 |
| 学习容量 | material_max_bytes=8192，input_units_limit=8192，output_units_limit=1024；前两者分别为材料bytes及模拟输入units，输出仍是Provider既有模拟输出预算，不声称真实token保证 |
| LEARNING生成profile | profile_id=sample_learning、account_id=sample_account、model_id=sample_generation、wire_protocol=SIMULATED、capability=GENERATION；max_input_units=8192、max_output_units=1024、max_items=2；dimensions／space_id=None，media_tasks=空tuple。LEARNING候选映射仅此profile，不授予工作权限；其他角色／能力不因本候选改写 |
| Provider外层容量 | provider.request_max_bytes=16384，provider.result_max_bytes=4096；覆盖原Provider合成例中的request_max_bytes=4096，不能继续沿用会装不下本材料的旧请求限额 |
| 持久化容量 | storage.command_max_bytes=1048576，storage.receipt_max_bytes=65536；覆盖旧合成例中较小的两值。上限仍在原批准范围内，实际完整命令／回执／审计及配置初始化编码仍须逐项校验 |
| 其他新参数 | §11.14.2余下全部推荐值及§11.14.3完整观察表原样沿用，包括短批false、空闲尾部false／timeout=0、normal_soft_limit=1000；无遗漏补默认 |
| 其他Provider数值 | 明确沿用[Provider共用合成例](provider.md#provider-foundation-acceptance)：实例并发2、账户并发1、max_attempts=2、attempt_timeout_ms=100、request_timeout_ms=1000、retry_delay_ms=10、close_timeout_ms=1000、query_row_limit=100；sample_account／sample_window、TEST、attempt_limit=20、cost_limit_atoms=1000000，生成价格input=2／output=3 atoms/unit。profile除上行特定字段外使用本行max_attempts／attempt_timeout及价格 |
| 其他存储／审计数值 | 明确沿用[持久化共用合成例](persistence-and-transactions.md#persistence-foundation-acceptance)：operation_timeout_ms=1000、lock_wait_ms=50、close_timeout_ms=1000、read_capacity=2、wal_checkpoint_pages=100、audit.event_max_bytes=2048、audit.events_per_operation=8；只覆盖上行两项存储容量 |

foundation日志仍须按原规则提交完整合法配置；其候选与目录资源条件不因材料计算改写。生产真实路径、身份保留与鉴权仍待批准；已修订审计及分层恢复已获批准，本节不把数字候选伪装成已装配资源。固定模板／边界声明来自受控参与者能力，所有可调参数仍经统一配置显式注册与解析。

**满历史窗口的静态代入：**

| 检查 | 静态代入与结论 |
| --- | --- |
| 事件展开 | E=1024时Base64上界4×ceil(1024/3)=1368；加入材料协议逐项角色／引用180，单项上界1548 |
| 完整材料 | 满历史N=1＋2＋1=4；固定1057＋4×1548=7249 bytes ≤8192，余量943；已包含SYSTEM模板、7个最长ID头、所有段标签、序列／时间、完整事件及尾标 |
| 模拟输入计量 | 两项text总上界为7249 units ≤请求input_units_limit 8192=profile.max_input_units；Provider外层role枚举不冒充文本，段角色已计在逐项180中 |
| 输出与消息数 | output_units_limit 1024=profile.max_output_units；messages恰2项≤max_items 2≤既有格式64；不把4条来源事件误算成4个Provider消息 |
| 请求完整编码 | 本候选entry_ids仅当前入口；最多8个标量归因／操作／profile ID加1个entry ID，每个≤128字节。归一化请求的顶层键／对象标点146，8个带引号ID值1040，单entry数组132，含两项空text的固定payload结构／角色／限额119；材料只有LF需在外层JSON额外转义，满窗共14个LF。总上界146＋1040＋132＋119＋7249＋14=8700 bytes ≤request_max_bytes 16384；deadline／取消能力按既有Provider协议不属于该持久语义请求编码 |
| 存储容量约束 | 6×max(1024,8192,8192)＋8192=57344 ≤receipt_max_bytes 65536且≤command_max_bytes 1048576；原Provider最低57344同时满足。6×result_max_bytes 4096＋8192=32768≤65536。保守六倍转义仍保留，不用Base64字母表另降旧基础门槛 |
| 并发／窗口／费用 | runtime.max_active_entries 1≤provider.max_in_flight 2，账户并发1≤2；normal_soft_limit 1000≥T＋R=3；H＋T＋R=4≤128。单attempt最大声明费用8192×2＋1024×3=19456 atoms≤账户1000000且≤2^63−1；账户窗口有限，不能据此保证积压永远获准发送 |
| 最小事件空间 | 材料正文的空body账目245，加入x后246≤E；只含不需JSON转义ASCII正文时可用1024−245=779字节。若正文全为3字节UTF-8汉字则最多259字，全部为六字节转义控制字符则最多129个；额外身份／引用占用须从同一E扣减，不将这些数当通用字符配额 |

请求编码账目按[现有Provider精确请求字段](provider.md#provider-foundation-identity)和[模拟计量／规范化](../../companion_memory/provider/normalization.py)静态核对；材料公式对输入的引号／Unicode／引用成立，是因为它们先进入C≤E的完整事件编码，再整体Base64，不能把未经转义的原始正文当C。输出计量、规范化结果和候选产物仍独立受原result_max_bytes及语义校验约束，以上不证明任意模拟响应都合法。运行／Provider／存储期限也没有性能证明；晚完成和故障恢复边界不变。

这组数值及固定格式已获批准。静态字面量计数与代入不是编码器、配置解析、数据库或模型执行结果；材料边界、最小事件、满历史、请求编码及整体验收的对应版本证据只在[CURRENT_TASK](../work/CURRENT_TASK.md)记录。静态核算不能替代实际验证。

<a id="formal-memory-media-configuration"></a>

### 11.15 正式记忆、来源与媒体的配置补充（推荐已批准）

本节是[集中契约](formal-memory-source-media.md)的参数唯一正文；推荐方案已与主契约一并获用户批准，未采纳替代及范围外事项不随之批准。旧入口、旧定义、旧材料硬上限及已存快照解释保持。新增独立`resolve_content_configuration(foundation, runtime, platforms, content, protected_directories, material_contracts)`及对应只读适用性／持久初始化／加载能力，采用新的组合类型，不能把新值送入旧解析器或剔除字段绕过校验。它仍由configuration唯一拥有，不读文件／环境／数据库来解析，不提供编辑／激活接口。

新content域为实例作用域；foundation保留原全部定义及原生EffectiveSnapshot，runtime／platform延续原身份和窗口语义，新组合仅显式支持下表的材料范围增量。content域和新材料声明随一次完整配置初始化、同事务审计和真实snapshot映射提交，旧组合记录格式不重解释。新组合共foundation、runtime、content及每平台一域；推荐单平台共4域。可调字段都须完整注册并显式供值，`required=true, nullable=false, default=NoDefault(), scope=[instance], override_policy=no_override, sensitivity=public（仅非秘密验证资源）, read_roles/write_roles=[trusted_operator], apply_mode=INITIALIZE_ONLY, activation_group=NotApplicable`，无废弃／替代／升级默认。平台参数沿原NEXT_BATCH声明，但仍无在线改值能力。字段说明按原规范填写，不能省略定义或用说明文字求值。

整数均精确int、闭区间，拒绝bool；字符串路径只允许明确非秘密测试绝对路径，值由授权测试自有临时目录实际提供，不选生产目录。下列max_bytes类在新写和完整编码后同时核验，降低限额不截断旧记录。

| 新键 | 类型／单位／范围 | 推荐显式值 | owner／必要消费者 |
| --- | --- | --- | --- |
| memory.current_max_bytes | integer／bytes／1024–4096 | 4096 | memory／memory、cognition |
| memory.forget_below | integer／score／0–99 | 20 | memory／memory |
| memory.restore_at | integer／score／1–100 | 35 | memory／memory |
| memory.initial_retention | integer／score／0–100 | 50 | memory／memory、cognition |
| memory.read_timeout_ms | integer／milliseconds／1–60000 | 2000 | memory／memory |
| memory.read_concurrency | integer／requests／1–16 | 2 | memory／memory |
| memory.read_page_size | integer／rows／1–16 | 16 | memory／memory |
| cognition.candidate_item_limit | integer／items／1–8 | 8 | cognition／cognition、memory |
| cognition.candidate_item_max_bytes | integer／bytes／1024–8192 | 8192 | cognition／cognition、memory |
| cognition.candidate_max_bytes | integer／bytes／4096–73728 | 73728 | cognition／cognition、memory |
| media.root_directory | string／NotApplicable／media_resource_paths | 测试自有独占绝对目录 | media／media |
| media.staging_directory | string／NotApplicable／media_resource_paths | 上述根下独立staging子目录 | media／media |
| media.blob_max_bytes | integer／bytes／1–1048576 | 1048576 | media／media、ingress |
| media.event_occurrence_limit | integer／items／1–2 | 2 | media／media、ingress、cognition |
| media.upload_chunk_bytes | integer／bytes／4096–65536 | 65536 | media／media |
| media.upload_concurrency | integer／uploads／1–4 | 2 | media／media |
| media.processing_concurrency | integer／jobs／1–2 | 1 | media／media、runtime |
| media.file_worker_capacity | integer／workers／1–8 | 5 | media／media |
| media.read_concurrency | integer／reads／1–4 | 2 | media／media |
| media.read_chunk_bytes | integer／bytes／4096–65536 | 65536 | media／media |
| media.interpretation_text_max_bytes | integer／bytes／0–512 | 512 | media／media |
| media.interpretation_record_max_bytes | integer／bytes／1331–2048 | 2048 | media／media、cognition |
| media.operation_timeout_ms | integer／milliseconds／1–60000 | 10000 | media／media、ingress |
| media.upload_total_timeout_ms | integer／milliseconds／1–60000 | 60000 | media／media |
| media.occurrence_total_timeout_ms | integer／milliseconds／1–600000 | 60000 | media／media、runtime |
| media.preparation_total_timeout_ms | integer／milliseconds／1–3600000 | 600000 | runtime／runtime、media |
| media.io_timeout_ms | integer／milliseconds／1–60000 | 5000 | media／media |
| media.close_timeout_ms | integer／milliseconds／1–60000 | 10000 | media／media |
| media.recovery_timeout_ms | integer／milliseconds／1–60000 | 60000 | media／media、runtime |
| media.processing_suspect_after_ms | integer／milliseconds／1000–3600000 | 120000 | media／media、runtime |
| media.unbound_upload_retention_ms | integer／milliseconds／60000–86400000 | 3600000 | media／media |
| media.gc_interval_ms | integer／milliseconds／1000–86400000 | 600000 | media／media |
| media.gc_unreferenced_grace_ms | integer／milliseconds／0–86400000 | 600000 | media／media |
| media.gc_page_size | integer／blobs／1–64 | 16 | media／media |
| audit.history_item_max_bytes | integer／bytes／1024–8192 | 8192 | logging_service／logging_service、memory |
| audit.history_items_per_operation | integer／items／1–8 | 8 | logging_service／logging_service、memory |

每个数值条目的validator由唯一组入口绑定：`memory_object_limits`绑定memory.current_max_bytes，检查本组memory／cognition容量、F<H及初值；`media_resource_limits`绑定media.blob_max_bytes，检查全部media数值与Provider／执行预算；`media_resource_paths`绑定两个路径，只校验文本及完整保护目录上下文，物理同文件系统／隔离／身份另由资源初始化证明；`content_audit_limits`绑定audit.history_item_max_bytes，检查历史件数、逐件及汇总。必要dependencies须显式覆盖各组读取的同域键；跨foundation/runtime/platform关系由新组合入口检查，不能伪造跨registry依赖。extra已注册依赖仍按原存在性规则；无动态验证器、回调或忽略开关。

| 原有键／配置组在新组合中的推荐选择 | 明确增量或沿用 |
| --- | --- |
| ingress.event_max_bytes=2048；H/T/R=1/2/1；normal_soft_limit=1000；短批false／空闲false／idle_timeout=0 | 原合法范围内的新库显式值，不改变固定目标及安静尾部的产品限制；event_version=2媒体限制由新声明核验 |
| learning.material_max_bytes=49152、input_units_limit=49152、output_units_limit=2048 | **只有新组合**将material范围改为256–49152，旧范围256–8192保持；新硬格式／分叶重建与上界绑定是同一已批准增量，不能旧接口直接升限 |
| runtime.max_active_entries=1，其余runtime／management／logging.web_* | 原§11.14推荐值全部沿用；媒体发送／处理另有持久work与配额，不假称是普通学习槽 |
| provider.request_max_bytes=65536、result_max_bytes=8192；max_in_flight=2；request_timeout_ms=30000；close_timeout_ms=10000；retry_delay_ms=10；query_row_limit=100 | 均在原范围内；新库显式值。Provider输入／结果／原查询格式不扩容 |
| 同一模拟账户sample_account／sample_window | currency=TEST、max_in_flight=1、attempt_limit=10000、cost_limit_atoms=1000000000；无自动重置或真实收费含义 |
| LEARNING的sample_learning profile | SIMULATED／GENERATION，model_id=sample_generation，max_input_units=49152、max_output_units=2048、max_items=2、max_attempts=2、attempt_timeout_ms=5000、input_price_atoms=2、output_price_atoms=3；dimensions／space_id=null、media_tasks空；role映射LEARNING只此profile |
| MEDIA的sample_media profile | SIMULATED／MEDIA_UNDERSTANDING，model_id=sample_media_model，max_input_units=1048576、max_output_units=0、max_items=1、max_attempts=2、attempt_timeout_ms=5000、input_price_atoms=1、output_price_atoms=0；dimensions／space_id=null，media_tasks为IMAGE/DESCRIBE、AUDIO/TRANSCRIBE、VIDEO/DESCRIBE；role映射MEDIA只此profile |
| Provider其他角色 | 新推荐包不配置未使用角色；需要既有梦境门控兼容场景时显式增加原模拟DREAM profile并重新核算配置总量，不借此批准梦境业务 |
| storage.command_max_bytes=1048576、receipt_max_bytes=65536、operation_timeout_ms=30000、lock_wait_ms=50、close_timeout_ms=10000、read_capacity=2、wal_checkpoint_pages=100 | 原范围内；更长存储期限不代表读取可续进度或性能承诺 |
| audit.event_max_bytes=8192、events_per_operation=16 | 原范围内；实际新命令必要slot≤8、每slot摘要格式≤4096，完整读取另计封套，不能把16×8192视作单页可读 |
| foundation日志组及目录上下文 | 完整沿用§11.14启用日志服务的要求、旧日志推荐显式数值和精确元信息；增加真实自有媒体／暂存目录到保护清单，不能以空路径／空清单通过。此处不复制其默认声明 |

跨参数必须同时满足：F<H；初始R仍经滞回；blob限额≤所选媒体profile字节上限；理解／学习并发之和≤provider.max_in_flight，账户并发另按Provider裁决；`file_worker_capacity≥upload_concurrency+read_concurrency+processing_concurrency`（推荐5）。GC借用已有文件槽，不能另建无限队列；同一理解槽同时最多一份文件读取和一个Provider任务。块大小≤blob_max_bytes，read_chunk同理。各I/O等待取本次剩余与io_timeout较小者，upload总期与单次operation不反复重置；超时仍在途时不释放槽。processing_suspect_after大于新Provider总期，但只是失联告警，绝不作为夺权／释放条件；无主上传到期和GC宽限同样须满足所有权前置。

理解准入按[精确封套](formal-memory-source-media.md#interpretation-envelope)同时验证各来源／状态：I必须覆盖1331字节固定拒绝和1327字节最大失败元信息，不能仅用一个成功样例证明；text限额0–512只管可变成功／部分正文，EMPTY和固定REFUSED不随它缩减。绑定的原请求ID／scope／profile策略必须在发送前可无损映射到固定元信息界，不能依赖结果超限后的截断。I=1330在resolve_content_configuration返回VALUE_INVALID/RANGE_INVALID、field=value；值在范围内但材料关系不成立为VALUE_INVALID/BUDGET_INVALID、field=platforms；完整存储／配置容量或装配绑定无法满足为VALUE_INVALID/CAPACITY_INSUFFICIENT、field=content。服务装配层映射为CONFIGURATION_UNSUPPORTED/CAPACITY_INSUFFICIENT；均不得开始新接收／发送。外部事件超限及内部结果超限的不同处置只见该封套正文。

准备总期按选定成员最大数N和每事件媒体上限A校验`preparation_total_timeout_ms ≥ N×A×occurrence_total_timeout_ms + 4×storage.operation_timeout_ms`，单出现总期≥provider.request_timeout_ms；推荐`4×2×60000+4×30000=600000`。四个存储等待预留覆盖准备登记／领取／核验／冻结，不给每个逐出现步骤额外重置总期；逐出现原请求登记、I/O、Provider、保存及确认共享自己的剩余。所有下层deadline取剩余最小值，这只是时间预算相容性，不保证各下层同时耗尽自身最大时间后仍能成功。停放、重启、窗口变化与未发送的新准入代次仅按[准备协议](formal-memory-source-media.md#media-preparation)处理，配置不能授权TTL夺权或自动重发。

新材料上界、候选分叶、完整请求和配置初始化账目见[集中容量表](formal-memory-source-media.md#capacity)。新组合必须逐平台验B/W/Q并固定完整版本；继续原Provider的`57344`及`6×result_max_bytes+8192`基础下限。旧`6×max(event,material,8192)+8192`仅用于旧整材料文本装配；新组合精确改为对**每个实际持久叶**取该关系，另核验重建材料总量和完整命令。这不是取消总容量检查，49152材料不得持久为单个正文行。旧组合不得选择这条新规则。

新组合准入还限制：全域条目数≤128、每条编码≤8192、条目编码总和≤262144、参数键≤128字节、目录编码≤8192、域数≤4；推荐因此只承诺单平台包。新组合参数持久编码版本2在完整保留值／定义／来源的前提下，将U+0000–001F及U+007F均转成六字节JSON转义，其他合法Unicode直接UTF-8；不修改旧encode_entry版本1字节或其读取解释。这样条目body无这些字面控制字符，嵌入原基础ensure_ascii命令编码至多3倍；须专门验证DEL、两字节Unicode和补充平面边界，不能把普通encode_entry版本1未经核验套入此证明。这项局部证明不改变任意正文的原6倍保守界。旧平台结构上限64保持旧含义，新包若要多平台应另给完整值／定义及目录编码账目，并集中批准所需范围；不自动放宽或拆分配置初始化。编码预检在建库副作用前完成，重新打开按原记录版本及格式硬限校验。

新ContentConfigurationError恰含code、operation、field、reason：operation为resolve_content_configuration／persist_content_configuration／load_content_configuration／content_snapshot_issue，field为foundation/runtime/platforms/content/definition/value/context/storage/identity。code／reason闭合集合沿§11.14的分类及枚举，新预算／目录／域总量失败用VALUE_INVALID/BUDGET_INVALID或CAPACITY_INSUFFICIENT，不新增自由原因；不透传路径／非法键。顺序：精确载体→域身份／未知键→全部声明／支持→完整定义→foundation原校验→全部值隔离→依赖→固定及跨域／完整编码→一次候选。旧错误类／旧首错不变。持久初始化错误另配真实提交证据，纯解析没有I/O成功含义。

没有自动衰减率、使用反馈增益／有效期、自动删除保留天数、在线配置或解除保护参数；那些是后续模块未实现职责，不用占位值伪装已支持。新参数推荐及路径能力已批准，全包核算须以实际验证证明，验收统一见[整体矩阵](formal-memory-source-media.md#acceptance)。

<a id="local-information-configuration-draft"></a>

### 11.16 面向宿主的本地信息与反馈：配置补充（推荐已批准）

本节是[集中契约](local-information-feedback.md)的配置唯一补充，**新增定义、数值、完整组合及持久层工程增量已随五组推荐获用户批准**；旧§11.15及其他已批准定义不变。已授权注册参数、实现配置与装配及范围内验证；不批准未采纳替代、热改或存量迁移。推荐新独立`resolve_information_configuration`、对应persist/load及只读适用性端口，输入为完整foundation/runtime/platforms/content/information、受保护目录及材料声明；旧resolve_content_configuration继续拒绝新域，不能删除新键或读环境补齐。

#### 完整显式组合及旧值来源

采用§11.15全部推荐值／版本2材料／单平台组合，增加information域；一个平台可绑定3入口，不增平台域。foundation/runtime/content各一域、platform一域、information一域，合计5；**仅新组合**将完整域数上限从4改5，总条目128、旧单条8192、目录8192和完整普通命令1 MiB保持；新增8项单条≤4096，新body总限改294912。仅Information静态装配改为描述2621440、仓储131072、外层8192、合计2760704和载体3145728；旧装配预算保持。按基线声明静态计数，旧包105条（foundation40＋runtime22＋platform7＋content36），加8条得113≤128；该计数不证明定义编码容量。新增8个封闭object参数，不是8个任意扩展包；每个字段都是Schema规定的可调值，不接受未知字段／缺失值／动态表达式。若未来更改嵌套结构，须改相应固定Schema版本及重新核算，不能绕过注册表。

| 全集组成 | 本候选的取值来源与精确增量 |
| --- | --- |
| content全部36项 | 原样取本文件§11.15推荐列，包括两资源目录、F=20/H=35、初始R=50、memory读取2并发／2000ms、媒体与候选的全部限额；新本地包不修改旧值或旧正文量表 |
| runtime及平台全部项 | §11.15对§11.14的明确覆盖：E=2048、材料／input=49152、output=2048；其余§11.14.2和§11.14.3推荐列，包括H/T/R=1/2/1、短批／空闲关闭、1活跃入口、恢复30秒、关闭5秒、模式收尾30秒、观察32768字节／2并发；平台身份由可信测试绑定sample_platform |
| foundation存储／审计10项及Provider10项 | 原样采用§11.15旧组推荐表：命令1048576、回执65536、storage读并发2／操作30000ms／锁50ms／关闭10000ms／checkpoint100页；审计8192／16。Provider请求65536／结果8192／并发2／总期30000ms／关闭10000ms／retry10ms／查询100；单TEST账户和LEARNING／MEDIA两个模拟profile及其完整值仍由§11.15声明，无检索或目标语义profile |
| foundation日志20项 | 取[日志已批准数值定义](logging.md#runtime-diagnostics-configuration)的既有推荐：INFO、module_levels={}、console/file启用、console_level=INFO、file_level=DEBUG、stderr、event4096、sink1024、reserve128、preparation16、rotation10485760、retained5、I/O200ms、probe1000ms、flush1000ms、close2000ms、emergency8／1000ms；file_directory取实际自有目录。本行是本包明确选值，旧唯一默认声明不变 |
| 新域8项 | 下表全部字段显式供值；profile不存在用受支持能力枚举表达，不能用虚假成功或省略必填字段 |
| 资源／身份 | 未来测试用实际独占临时root，database、logs、media及media/staging、backup分别绑定；audit／provider_usage明确共享database，目录上下文仍全覆盖。路径、database_id、instance_id、configuration_key、宿主／入口／principal／route及有期测试会话由可信测试父进程保留；它们不是可用假值、生产默认目录或本文生成的秘密 |

静态核对取值时可对照[既有content夹具声明](../../tests/configuration/content_support.py)、[runtime夹具声明](../../tests/runtime/configuration_support.py)、[Provider夹具声明](../../tests/configuration/provider_support.py)及[日志声明](../../tests/configuration/logging_support.py)，这些夹具仅在自有隔离资源中运行，实际定义和值须核对正文。配置正文与夹具如有真实差异应列明并停止受影响选值，不以夹具默认覆盖正文。

#### 新参数完整定义与字段

八项共用元信息：`type=object, required=true, nullable=false, default=NoDefault(), scope=[instance], override_policy=no_override, sensitivity=public, read_roles/write_roles=[trusted_operator], apply_mode=INITIALIZE_ONLY, schema_revision=local_information_v1, deprecated=false`。unit/range/enum为NotApplicable并说明“完整封闭记录无单一单位／范围／整值枚举”；逐字段规则如下，不能因此跳过内层校验。activation_group/replacement/upgrade_rule为NotApplicable且分别说明无热激活／替代／自动迁移。每项description、rationale、validation_method及cost_impact/migration_impact采用下方完整定义模板的固定文本；行中职责说明为人读说明，不在运行时解析或自由拼入定义。

| key／owner；消费者；唯一固定validator | 完整字段与推荐显式值 |
| --- | --- |
| retrieval.local／retrieval；retrieval,memory,runtime；local_retrieval_limits | mode=LOCAL_LEXICAL_V1；query_max_bytes=512；normalized_max_bytes=8192；tokens_per_object=4096；posting_visit_limit=4096；candidate_limit=128；dirty_overlay_limit=128；relation_expansion_limit=0；index_page_size=16；index_workers=1；rebuild_generations=2；index_step_timeout_ms=5000；index_recovery_timeout_ms=30000；close_timeout_ms=10000 |
| retrieval.reply／retrieval；retrieval,management；reply_partition_limits | base_deadline_ms=1000；concurrency=2；queue_capacity=0；memory_limit=8；recent_limit=4；goal_limit=8；persona_max_bytes=8192；state_max_bytes=4096；response_max_bytes=131072；persona_policy=ALLOW_EXPLICIT_PARTIAL；rerank_enabled=false；require_complete_index=false |
| retrieval.tickets／retrieval；retrieval,memory；recall_ticket_limits | ttl_seconds=86400；live_limit=10000；root_max_bytes=2048；member_max_bytes=512；consumption_max_bytes=1024；member_limit=8；cleanup_page_size=16；cleanup_interval_ms=60000 |
| memory.usage／memory；memory,retrieval,runtime；memory_usage_limits | gain=8；forgotten_retention_seconds=2592000；expiry_scan_enabled=true；expiry_scan_interval_ms=60000；expiry_scan_page_size=16；existing_object_limit=100000；feedback_member_limit=8；operation_timeout_ms=5000 |
| state.external／state；state,retrieval；external_state_limits | record_max_bytes=4096；field_text_max_bytes=512；stale_after_seconds=300；future_tolerance_seconds=60；writer_policy=SINGLE_BOUND_HOST；operation_timeout_ms=5000 |
| goals.lifecycle／goals；goals,retrieval,runtime；goal_lifecycle_limits | record_max_bytes=4096；content_max_bytes=2048；open_goal_limit=1000；source_limit=8；aliases_per_goal=64；list_page_size=8；dedup_candidate_limit=32；dedup_workers=1；dedup_wait_timeout_ms=5000；operation_timeout_ms=5000；dedup_mode=EXACT_ONLY；semantic_policy=MARK_UNAVAILABLE |
| goals.delivery／goals；goals,runtime,management；goal_delivery_limits | sink_mode=DISABLED；workers=1；scan_page_size=16；scan_interval_ms=1000；request_max_bytes=2048；response_max_bytes=1024；attempt_limit=2；attempt_timeout_ms=1000；total_timeout_ms=2500；retry_delay_ms=10；overdue_policy=COALESCE_ONCE；repeat_expired=false |
| management.host／management；management,retrieval,state,goals；host_interface_limits | request_max_bytes=16384；header_max_bytes=8192；response_max_bytes=131072；connections=4；body_timeout_ms=2000；write_timeout_ms=2000；session_limit=16；session_ttl_seconds=3600；command_concurrency=1；command_queue_capacity=0 |

内层值均精确类型。bytes字段为整数1至本行推荐值，query／normalized／record／root／member／consumption须同时满足集中草案的完整记录预算；其余未单列的数量型整数（含tokens_per_object、aliases_per_goal）为1至本行推荐值；queue_capacity、command_queue_capacity、relation_expansion_limit只支持0，rebuild_generations只支持2。各毫秒字段1–60000；seconds字段：ttl 60–86400、forgotten_retention 86400–31536000、stale 1–86400、future_tolerance 0–300、session_ttl 1–86400；gain为1–100。布尔只支持表内推荐值。枚举只支持表内值，唯sink_mode可另显式选择TEST_HTTP以验证真实本地接收；该选择已获隔离测试授权，且必需可信已绑定测试接收器。字符串无自由回调、URL或供应商名。

首包支持组合明确限定为下方完整推荐向量，或只把sink_mode改为TEST_HTTP且提供真实绑定的替代向量；上面的类型／范围只是定义边界，其他数值组合需另给完整核算并获批准，不能因为逐字段落入范围就宣称可运行。本候选通过缩小字段值并不保证仍相容；下列跨参数及固定记录编码规则会进一步拒绝。record／payload完整上限仍检查全部元信息和转义；合法取值范围不等于所有字段极大值可同时使用。模式和方案枚举只声明支持，不在运行时读取本草案判断批准状态。

同information域dependencies按下方完整映射声明。这些是存在性／一次全值校验关系，不按拓扑执行或进行递归求值；其余为空。跨旧域关系由新组合固定校验，不伪造同registry依赖。

#### 新八项的完整定义模板、持久载体及上界

本模板给出28个元信息字段的精确值，逐行替入上表key、owner_module、consumers、validator及下述dependencies；没有省略的默认字段。说明字符串固定ASCII（不从中文说明生成），标识符≤32字节、owner≤16、schema_revision≤24；各说明≤96字节、NotApplicable.reason≤64。继承105项仍保留其原定义／default／来源，不能用本模板批量重定义。列表转不可变结构、Declared／NotApplicable／NoDefault等标记均经现有配置v2类型包装，不把下列人读记法直接当持久JSON。

```text
key=<该行key>; owner_module=<该行owner>; schema_revision="local_information_v1"
type="object"; default=NoDefault(); required=true; nullable=false
unit=NotApplicable("The complete record has no single unit.")
range=NotApplicable("The fixed validator checks each field and cross-field bound.")
enum=NotApplicable("There is no whole-record enumeration.")
validator=[<该行唯一validator>]; dependencies=<下述对应key数组>
scope=["instance"]; override_policy="no_override"; sensitivity="public"
read_roles=["trusted_operator"]; write_roles=["trusted_operator"]
apply_mode="INITIALIZE_ONLY"; activation_group=NotApplicable("No live activation.")
cost_impact="Local CPU and storage only; no query model calls."
migration_impact="New assembly and new database; existing formats remain unchanged."
description="Explicit bounded local information settings."
deprecated=false; replacement=NotApplicable("No replacement.")
upgrade_rule=NotApplicable("No automatic migration.")
rationale="Preserve bounded work, ownership and durable feedback evidence."
consumers=<该行消费者数组>
validation_method="Check exact fields, finite values, complete encoding and assembly bounds."
```

dependencies完整闭合映射：`retrieval.local=[]; retrieval.reply=[retrieval.local,retrieval.tickets,state.external,goals.lifecycle,management.host]; retrieval.tickets=[retrieval.reply,memory.usage]; memory.usage=[retrieval.tickets]; state.external=[]; goals.lifecycle=[goals.delivery]; goals.delivery=[goals.lifecycle,management.host]; management.host=[retrieval.reply]`。集合按稳定key顺序冻结，不改变依赖的存在性语义。

新每项输入值恰上表所列字段（至多16字段，字段名≤32字节；枚举文本≤32，其余有限int/bool），缺失／未知字段拒绝。新增单条v2持久对象恰`version=2, definition=<上述28字段经_encode>, state=PRESENT, value=<record类型包装>, source=EXPLICIT`；原105项允许其原来的EXPLICIT/DEFAULT来源，不能把显式示例反向改原default。完整定义编码保守分账：28键／标点≤800；12个标量文本含引号≤640；3布尔≤15；NoDefault包装≤32；6份NotApplicable含包装各≤112计672；6组列表（validator/dependencies/scope/read_roles/write_roles/consumers，元素总≤12，每ID带引号≤34，包装总≤192）≤600；definition≤2759。value的16字段、类型包装及分隔≤1184；entry封套≤128；合计≤4071≤4096。模板实际说明和表内值都在上述ASCII范围内；该上界不是编码器实测。

原105项沿原已批准有效包body≤262144；不能从历史551815命令描述倒推出配置body大小。8×4096＋262144＝294912，在新的初始化3倍嵌套证明中保守覆盖全部定义、类型标记和值。命令仍采用catalog及每域domain_id/digest/entries数组，域ID≤128、digest固定64，113≤128条；catalog恰version、5域revision清单及原单平台材料声明，总≤8192；存储快照和活跃指针同事务，必要configuration_initialized审计不变。配置对象不含HTTP密钥、真实API key或任意callback。

#### 完整推荐值示例（文档数据，未解析／未运行）

以下一次列出105个继承值和8个新object的全部值。`<TEST_ROOT>`是将来授权测试创建并绑定的实际独占临时绝对目录，本文没有创建它；方括号中的平台仅一个，三入口身份及有期Bearer由可信父进程另行保留。所有目录须满足原有效配置body及保护目录约束，不能将这个符号作为字面路径运行。旧105项registry定义严格使用§11.15所引用的完整批准定义；新8项使用上面精确模板，二者都随快照保存。此处的JSON只展示值，不是新增工程配置文件或替代启动指南。

```json
{
  "foundation": {
    "storage.database_file": "<TEST_ROOT>/database/runtime.sqlite3",
    "storage.operation_timeout_ms": 30000, "storage.lock_wait_ms": 50,
    "storage.close_timeout_ms": 10000, "storage.read_capacity": 2,
    "storage.command_max_bytes": 1048576, "storage.receipt_max_bytes": 65536,
    "storage.wal_checkpoint_pages": 100, "audit.event_max_bytes": 8192,
    "audit.events_per_operation": 16,
    "provider.max_in_flight": 2, "provider.request_timeout_ms": 30000,
    "provider.close_timeout_ms": 10000, "provider.retry_delay_ms": 10,
    "provider.request_max_bytes": 65536, "provider.result_max_bytes": 8192,
    "provider.query_row_limit": 100,
    "provider.accounts": [{"account_id":"sample_account","window_id":"sample_window","currency":"TEST","max_in_flight":1,"attempt_limit":10000,"cost_limit_atoms":1000000000}],
    "provider.profiles": [
      {"profile_id":"sample_learning","account_id":"sample_account","model_id":"sample_generation","wire_protocol":"SIMULATED","capability":"GENERATION","max_attempts":2,"attempt_timeout_ms":5000,"max_input_units":49152,"max_output_units":2048,"max_items":2,"input_price_atoms":2,"output_price_atoms":3,"dimensions":null,"space_id":null,"media_tasks":[]},
      {"profile_id":"sample_media","account_id":"sample_account","model_id":"sample_media_model","wire_protocol":"SIMULATED","capability":"MEDIA_UNDERSTANDING","max_attempts":2,"attempt_timeout_ms":5000,"max_input_units":1048576,"max_output_units":0,"max_items":1,"input_price_atoms":1,"output_price_atoms":0,"dimensions":null,"space_id":null,"media_tasks":[{"modality":"IMAGE","task":"DESCRIBE"},{"modality":"AUDIO","task":"TRANSCRIBE"},{"modality":"VIDEO","task":"DESCRIBE"}]}
    ],
    "provider.role_profiles": {"LEARNING":["sample_learning"],"MEDIA":["sample_media"]},
    "logging.instance_level":"INFO", "logging.module_levels":{},
    "logging.console_enabled":true, "logging.file_enabled":true,
    "logging.console_level":"INFO", "logging.file_level":"DEBUG", "logging.console_stream":"stderr",
    "logging.file_directory":"<TEST_ROOT>/logs", "logging.event_max_bytes":4096,
    "logging.sink_capacity":1024, "logging.warning_reserve":128, "logging.preparation_capacity":16,
    "logging.rotation_bytes":10485760, "logging.retained_segments":5,
    "logging.io_timeout_ms":200, "logging.probe_interval_ms":1000, "logging.flush_timeout_ms":1000,
    "logging.close_timeout_ms":2000, "logging.emergency_capacity":8, "logging.emergency_interval_ms":1000
  },
  "runtime": {
    "ingress.event_max_bytes":2048, "runtime.max_active_entries":1, "runtime.operation_timeout_ms":5000,
    "runtime.recovery_timeout_ms":30000, "runtime.close_timeout_ms":5000, "runtime.focus_drain_timeout_ms":30000,
    "runtime.claim_lease_ms":15000, "runtime.local_retry_limit":1, "runtime.read_page_size":16, "runtime.transfer_page_size":16,
    "learning.material_max_bytes":49152, "learning.input_units_limit":49152, "learning.output_units_limit":2048,
    "management.observation_row_limit":32, "management.observation_max_bytes":32768, "management.observation_timeout_ms":2000,
    "management.observation_concurrency":2, "management.refresh_min_interval_ms":1000,
    "logging.web_window_events":512, "logging.web_query_row_limit":32, "logging.web_query_max_bytes":32768, "logging.web_query_timeout_ms":1000
  },
  "platforms": [{"platform_id":"sample_platform", "values":{
    "platforms.sample_platform.buffer.history_context_count":1,
    "platforms.sample_platform.buffer.recent_context_count":1,
    "platforms.sample_platform.buffer.target_count":2,
    "platforms.sample_platform.buffer.normal_soft_limit":1000,
    "platforms.sample_platform.buffer.explicit_short_enabled":false,
    "platforms.sample_platform.buffer.idle_tail_enabled":false,
    "platforms.sample_platform.buffer.idle_timeout_ms":0
  }}],
  "content": {
    "memory.current_max_bytes":4096, "memory.forget_below":20, "memory.restore_at":35,
    "memory.initial_retention":50, "memory.read_timeout_ms":2000, "memory.read_concurrency":2, "memory.read_page_size":16,
    "cognition.candidate_item_limit":8, "cognition.candidate_item_max_bytes":8192, "cognition.candidate_max_bytes":73728,
    "media.root_directory":"<TEST_ROOT>/media", "media.staging_directory":"<TEST_ROOT>/media/staging",
    "media.blob_max_bytes":1048576, "media.event_occurrence_limit":2, "media.upload_chunk_bytes":65536,
    "media.upload_concurrency":2, "media.processing_concurrency":1, "media.file_worker_capacity":5,
    "media.read_concurrency":2, "media.read_chunk_bytes":65536,
    "media.interpretation_text_max_bytes":512, "media.interpretation_record_max_bytes":2048,
    "media.operation_timeout_ms":10000, "media.upload_total_timeout_ms":60000,
    "media.occurrence_total_timeout_ms":60000, "media.preparation_total_timeout_ms":600000,
    "media.io_timeout_ms":5000, "media.close_timeout_ms":10000, "media.recovery_timeout_ms":60000,
    "media.processing_suspect_after_ms":120000, "media.unbound_upload_retention_ms":3600000,
    "media.gc_interval_ms":600000, "media.gc_unreferenced_grace_ms":600000, "media.gc_page_size":16,
    "audit.history_item_max_bytes":8192, "audit.history_items_per_operation":8
  },
  "information": {
    "retrieval.local":{"mode":"LOCAL_LEXICAL_V1","query_max_bytes":512,"normalized_max_bytes":8192,"tokens_per_object":4096,"posting_visit_limit":4096,"candidate_limit":128,"dirty_overlay_limit":128,"relation_expansion_limit":0,"index_page_size":16,"index_workers":1,"rebuild_generations":2,"index_step_timeout_ms":5000,"index_recovery_timeout_ms":30000,"close_timeout_ms":10000},
    "retrieval.reply":{"base_deadline_ms":1000,"concurrency":2,"queue_capacity":0,"memory_limit":8,"recent_limit":4,"goal_limit":8,"persona_max_bytes":8192,"state_max_bytes":4096,"response_max_bytes":131072,"persona_policy":"ALLOW_EXPLICIT_PARTIAL","rerank_enabled":false,"require_complete_index":false},
    "retrieval.tickets":{"ttl_seconds":86400,"live_limit":10000,"root_max_bytes":2048,"member_max_bytes":512,"consumption_max_bytes":1024,"member_limit":8,"cleanup_page_size":16,"cleanup_interval_ms":60000},
    "memory.usage":{"gain":8,"forgotten_retention_seconds":2592000,"expiry_scan_enabled":true,"expiry_scan_interval_ms":60000,"expiry_scan_page_size":16,"existing_object_limit":100000,"feedback_member_limit":8,"operation_timeout_ms":5000},
    "state.external":{"record_max_bytes":4096,"field_text_max_bytes":512,"stale_after_seconds":300,"future_tolerance_seconds":60,"writer_policy":"SINGLE_BOUND_HOST","operation_timeout_ms":5000},
    "goals.lifecycle":{"record_max_bytes":4096,"content_max_bytes":2048,"open_goal_limit":1000,"source_limit":8,"aliases_per_goal":64,"list_page_size":8,"dedup_candidate_limit":32,"dedup_workers":1,"dedup_wait_timeout_ms":5000,"operation_timeout_ms":5000,"dedup_mode":"EXACT_ONLY","semantic_policy":"MARK_UNAVAILABLE"},
    "goals.delivery":{"sink_mode":"DISABLED","workers":1,"scan_page_size":16,"scan_interval_ms":1000,"request_max_bytes":2048,"response_max_bytes":1024,"attempt_limit":2,"attempt_timeout_ms":1000,"total_timeout_ms":2500,"retry_delay_ms":10,"overdue_policy":"COALESCE_ONCE","repeat_expired":false},
    "management.host":{"request_max_bytes":16384,"header_max_bytes":8192,"response_max_bytes":131072,"connections":4,"body_timeout_ms":2000,"write_timeout_ms":2000,"session_limit":16,"session_ttl_seconds":3600,"command_concurrency":1,"command_queue_capacity":0}
  }
}
```

资源上下文另外恰`media=[<TEST_ROOT>/media,<TEST_ROOT>/media/staging], database=[<TEST_ROOT>/database], audit=[<TEST_ROOT>/database], provider_usage=[<TEST_ROOT>/database], backup=[<TEST_ROOT>/backup]`；日志目录是显式参数且与保护上下文一起核验；材料声明为原content单平台版本2可信绑定，原B/W/Q规则不复制或修改。registry、实际目录、材料能力和database/instance身份必须全部实际装配，值示例本身不能签发身份或证明环境就绪。

#### 跨层相容性、错误与未实测项目

- 全包先按精确载体／键／声明、旧组元信息与值、内层隔离、dependencies存在性、固定预算和完整编码依序校验，再一次签发不可变候选；不接受半份可运行配置。旧组公开解析及首错语义保持。
- memory_limit≤ticket.member_limit=feedback_member_limit≤8；K×(memory.current_max_bytes+1024)＋recent_limit×(event_max_bytes+512)＋persona_max_bytes＋state_max_bytes＋goal_limit×goal_record_max_bytes＋8192≤reply.response_max_bytes≤management.host.response_max_bytes。persona区包含全部发布元信息；单独text必须留封套余量。
- live_limit为有效票及过期但尚未安全处置票的总占槽上限，consumption_max_bytes=1024同时约束retrieval消费叶及memory效果收据；16票/分钟清理不删除有效反馈或未决证据。≤0.08票/s持续与4/s十分钟峰值的负载／空间条件及731槽示例余量只在[票据容量账目](local-information-feedback.md#capacity)维护，不是新的隐含速率参数；改TTL/容量/清理参数须重验该关系。
- 新信息检索／状态／目标点读使用原最大8192叶及65536回执限；票据完整根、成员、命令和审计按[容量账目](local-information-feedback.md#capacity)预检。F/H继续取content原已批准值且F<H，增益单独取本节；feedback总期不改变票据有效期或连续遗忘时长。
- query／dirty候选有界，不持完整库在内存；query并发≤memory.read_concurrency且≤storage.read_capacity，后台读竞争仍计入剩余deadline，无“每路各占2连接”的旁路。索引／去重工作者各1，单库写仍串行；HTTP连接4不等于能同时执行4个查询。
- 基础deadline≤1000ms；下层等待取调用剩余和其原配置上限的最小值，内层超时不夺走实际owner。HTTP读体和排空单列，不能扩大成功基础deadline。提醒total≥attempt_limit×attempt_timeout＋(attempt_limit−1)×retry_delay，推荐2500≥2010；只有明确未发送才可进行第二次尝试。
- 所有新object参数的**定义和值**都进入持久body，单项≤8192、总条数仍128。新8项单项完整body另限4096，原105项完整body≤262144，新合计≤294912；全113项而非8个裸值都持久化。完整初始化含descriptor/values/intentions的保守上界1007616≤1048576；94命令的完整新静态装配推导2694098≤2760704，分别核对描述2621440、仓储131072、外层8192。该修订与3 MiB静态格式通路在[集中装配决定](local-information-feedback.md#capacity)统一获批准，旧794624／旧格式1 MiB不变。未来实际描述／DDL须符合构造预算；初始化拒绝是失败协议，不是相容证明，不能删slot或隐藏定义。
- 新InformationConfigurationError恰`code, operation, field, reason`；操作限resolve/persist/load_information_configuration及information_snapshot_issue；field限foundation/runtime/platforms/content/information/definition/value/context/storage/identity。沿原组合错误分类及安全首错，新增预算失败用VALUE_INVALID/BUDGET_INVALID或CAPACITY_INSUFFICIENT，装配映射CONFIGURATION_UNSUPPORTED；不回显路径、非法键、秘密或底层异常。持久成功／未知仍配原四分支，纯解析没有存储成功含义。

本节不新增真实embedding/rerank模型／维度参数、persona监管参数、目标语义prompt、生产route URL／凭据或自动审计保留配置；这些能力未支持，缺失须按[独立依赖](local-information-feedback.md#baseline)明确返回，而非配置空值后假成功。完整最大包、跨进程、真实HTTP、中文质量、门控竞争、资源及获准Linux验证见[验收矩阵](local-information-feedback.md#validation)。
