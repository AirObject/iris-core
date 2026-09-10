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
