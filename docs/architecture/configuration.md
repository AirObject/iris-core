# 统一配置注册表、快照与安全热修改

> 本文件是权威原文的阅读视图，不是独立需求。原文仍是权威依据；后续修改规则时，先更新对应原文，再同步受影响的视图与相对链接；[覆盖映射](../work/ORGANIZATION_REPORT.md#coverage)仅作整理历史保留，不要求持续更新。保留原文“已确定、建议、示例、待确认”的性质；下列导读不新增决策。正文含原有编号，仅限文档追踪。

适用主题与局部定义：Schema描述参数，revision标识配置版本，snapshot是操作取得的完整不可变视图；生效计划负责让版本在指定边界启用。业务对象和已使用预算不是配置。

来源：§11.1–11.8对应[原文基线 L815–L950](../../companion_memory_module_design_provider_logging_config.md#section-11)；新增§11.9对应[详细契约原文](../../companion_memory_module_design_provider_logging_config.md#configuration-registry-contract)。旧行号锚点保留整理基线含义，新增章节、当前行号与哈希见[增量覆盖记录](../work/ORGANIZATION_REPORT.md#configuration-contract-review)。

按关联工作联合阅读：[配置模块契约](../modules/configuration.md)；[配置事务](persistence-and-transactions.md#t12)；[参数已定与未定](../product/decisions-and-delivery.md#section-23)；[模式与权限](../modules/runtime.md)；[日志参与者](logging.md#source-line-807)。

本次切片：[当前有效契约](#configuration-registry-contract)、[已有要求／建议／缺失](#configuration-registry-evidence)、[批准范围](#configuration-registry-decisions)。

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
