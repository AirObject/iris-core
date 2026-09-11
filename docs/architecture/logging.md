# 日志等级、输出、审计隔离与背压

> 本文件是本主题的现行正文，在此唯一维护。既有要求、已批准契约、建议和待批准事项保持各自状态；迁移不新增产品决定或实现授权。文档关系见[总入口](../INDEX.md)。

适用主题与局部定义：sink是日志输出端。运行诊断可以有界丢弃，必须的业务审计随业务事务提交；模型计量是Provider自己的持久化账本。

设计／审核参考：[冻结原始文档](../reference/companion_memory_module_design_provider_logging_config.md)。仅供追溯，不作为现行约束。

按关联工作联合阅读：[日志模块契约](../modules/logging.md)；[审计事务](persistence-and-transactions.md#t13)；[热配置协议](configuration.md#source-line-895)；[产品故障与权限](../product/operations-and-management.md#section-17)；[参考标记](references.md)。

返回[文档总入口](../INDEX.md)；实际进度见[工作状态](../work/STATUS.md)。

已批准契约：[统一运行诊断日志：控制台与文件](#runtime-diagnostics-contract)；[集中决定](#runtime-diagnostics-decisions)。[配置补充契约](configuration.md#configuration-additional-validation-contract)已另行批准；实施前仍须落实[Schema与目录规格](#runtime-diagnostics-schema)，G1已批准，G2见[待定事项](#runtime-diagnostics-prerequisite-decisions)。本文件为唯一维护正文。

已批准契约：[同事务审计详细契约](#transactional-audit-contract)，已随[持久化事务基础整体决定](persistence-and-transactions.md#persistence-foundation-decisions)获批，待实现授权；运行诊断L1–L4的批准范围不变。

<a id="section-10"></a>

<a id="source-line-737"></a>

## 10. 独立日志模块：等级、输出、审计与运行安全

<a id="source-line-739"></a>

### 10.1 统一日志模型与三个记录类别

所有模块、HTTP访问、后台执行器和受支持SDK使用[M14](../modules/logging.md#contract)提供的logger或桥接适配器。正式业务代码不以`print()`、自建FileHandler或私自写日志文件代替统一记录；应急启动/崩溃通道是明确例外。

| 类别 | 权威存放与用途 | 是否受普通日志等级影响 |
| --- | --- | --- |
| 运行诊断日志 `runtime` | [M14](../modules/logging.md#contract)结构化事件，输出控制台、文件、Web，排查运行过程 | 是，按模块与sink规则过滤 |
| 业务与安全审计 `audit` | [M14](../modules/logging.md#contract)通过业务UoW写入受保护审计表，记录认知变更、配置修改、权限/删除操作等 | 否，必记事件不因设为ERROR而消失 |
| 模型计量账本 `provider_usage` | [M13](../modules/provider.md#contract)持久化真实请求/attempt/usage及费用，[M14](../modules/logging.md#contract)只记录关联摘要 | 否，计量不依赖logger是否输出 |

`AUDIT`和`USAGE`是事件类别，不新增为高于CRITICAL的日志等级；审计展示可以有severity，但它不是是否持久化的开关。审计正文可能包含记忆历史，由权限独立保护，不默认向全部sink广播。普通记录完整覆盖系统活动，不等于将所有用户原文、prompt和模型返回都打印三份。

<a id="source-line-751"></a>

### 10.2 标准等级与使用规则

优先Python实现时采用标准库既有等级，不额外创建SUCCESS/TRACE等数值；第三方别名经桥接规范化。以下数值和基本严重性对应Python官方定义，应用举例是本设计约定。[S15](references.md#s15)

| 等级 | 数值 | 本系统适用例子 |
| --- | --- | --- |
| DEBUG | 10 | 分段选择、候选数量、预算计算细节；默认不含敏感正文 |
| INFO | 20 | 服务启动、批次成功、梦境开始/结束、配置版本激活 |
| WARNING | 30 | 可恢复降级、临近预算、局部媒体拒绝、日志丢弃或磁盘余量偏低 |
| ERROR | 40 | 普通学习最终失败、整轮拒学造成学习缺口、配置激活失败、某sink不可用 |
| CRITICAL | 50 | 核心持久化不可用、无法安全恢复、关键状态损坏导致无法继续 |

`NOTSET`用于等级继承/处理策略，不是业务严重性。`exception()`表达附带异常栈的ERROR记录，不是第六个等级；ERROR不自动等于退出进程。`DREAMING`是正常模式拒绝，按正常访问记录或受控采样记录，不刷屏成ERROR。

支持实例默认等级、模块覆写和各sink最低等级。模块采集层不得先丢掉文件sink所需的DEBUG；默认模块级有效采集阈值应允许所有启用sink需要的最低等级，再按sink过滤。用户显式提高模块阈值时，Web显示由此被各sink共同丢弃的范围。级别修改不清理已保存记录。

<a id="source-line-767"></a>

### 10.3 结构化字段与关联

统一事件至少包含`event_id`、UTC时间、`level_name/level_number`、`logger/module`、稳定`event_code`、可读message、schema版本及必要上下文。可选字段包含`trace_id/span_id/request_id`、`run_id/batch_id/dream_run_id/entry_id`、`provider_request_id/attempt_id`、`config_snapshot_id/runtime_policy_revision`、进程/任务标识、结果状态、耗时和脱敏异常栈。

记录发生时间与观察时间可以分开，耗时用单调时钟计算，避免系统时钟校正产生负延迟。跨模块使用相同关联ID，不把一个请求拆成无法追踪的文本。OpenTelemetry日志模型提供timestamp、severity、body、trace等通用字段，可作为映射目标；无需为此在首期部署额外collector。[S16](references.md#s16)

logger/SDK桥接集中安装一次，避免父子handler重复输出。同一事件经三个sink展示仍是一个event_id，不计为三次业务事件或三笔费用。格式化失败使用经过脱敏的简化备用记录，不能触发无限异常递归。

<a id="source-line-775"></a>

### 10.4 控制台、文件与Web三个输出端

| 输出端 | 首期能力与边界 |
| --- | --- |
| 控制台 | stdout/stderr分流可配置；交互开发可读文本，Docker可JSON；等级、颜色、字段可调，非TTY默认无颜色 |
| 文件 | `/data/logs/runtime/`的JSONL或配置格式；按大小/时间轮转，保留天数/份数/总字节可控，旧段压缩后台执行 |
| Web | 脱敏实时流及历史查询；按时间、等级、模块、事件码、run/request/attempt过滤；分页/游标、暂停滚动、断线续读、受控导出 |

建议Web实时用SSE实现单向订阅，也可采用等价传输；它订阅[M14](../modules/logging.md#contract)服务，不让浏览器读取任意容器路径。历史从持久文件分段与可重建索引查找，不在每次刷新时全盘扫描。已经超出保留范围的游标返回`LOG_GAP`及可用范围，不保证无限回放。

文件、Web、控制台可分别设等级和格式。若文件只保留INFO而实时Web允许DEBUG，重启后历史DEBUG不可找回，应在页面显示持久化等级边界；需要DEBUG历史时同时配置相应持久sink。关闭Web页面不影响文件落盘；Web实时订阅断线不影响Provider账本。

Python提供队列式handler/listener和轮转handler，适合把慢输出从请求线程移出，但默认队列满行为和handler串行关系需要显式配置，不能直接套用默认就宣称不丢日志。[S17](references.md#s17)

<a id="source-line-789"></a>

### 10.5 异步输出、背压与故障

诊断日志先规范化和脱敏，再进入受限队列，分别向各sink投递；慢Web客户端或故障文件sink不阻塞一秒召回路径，也不拖住其他sink。线程/队列数量和容量可配置，不与梦境消息“无条数上限”混为一谈。

诊断队列饱和时按已配置策略优先丢弃/采样DEBUG与INFO，并持有可观察的`dropped_events{sink,level}`计数；WARNING以上使用预留容量和有界应急通道。无通道可写时仍可能丢诊断记录，必须通过健康状态和后续恢复记录说明，不能承诺资源耗尽时绝对零丢失。不能为了保日志无限等待或无限占用内存。

审计不走可丢的诊断队列：受审计业务操作须把审计行与其变更同事务提交，具体参与与失败协议见[同事务审计契约](#transactional-audit-contract)。审计/Provider账本存储失败时，对应写入或新付费调用按系统故障处理；单纯控制台或运行日志文件失败则可降级，不撤销已提交认知。

支持sink故障状态、最近成功写入、排队长度、丢弃数和磁盘告警；日志模块自身故障走最小stderr应急记录，不经同一故障sink反复记录自己。关闭时有界drain/flush；强制kill或掉电可能丢尚未刷出的诊断尾部，业务审计和已提交计量仍遵守数据库持久化保证。

<a id="source-line-799"></a>

### 10.6 脱敏、权限与上下文隔离

入队前统一移除Authorization、API key、cookie、令牌、带签名URL参数等敏感字段；异常文本、SDK调试输出和HTTP请求日志同样处理。普通日志默认不包含聊天全文、原图/音频、完整prompt/response或原始来源。字段名和结构使用白名单，不只做容易漏过的字符串替换。

需要排查时可在正常模式经[M15](../modules/configuration.md#contract)启用限定模块/run、限定时长/字节数的诊断内容捕获；必须提示隐私和存储影响，保留脱敏、自动到期与独立访问权限，密钥始终不可记录。媒体只记录blob标识和状态，不复制二进制。用户文本显示为转义文本，过滤换行/控制字符，Web不执行其中HTML或脚本。

运行日志、敏感审计、Provider详情权限分离。认知agent只收到必要的业务错误码/恢复句柄，不取得日志搜索、audit查询、任意文件读取或计费账本下载工具。开发者日志查看不算外部实际使用记忆，不提高保留强度。日志不参与embedding和记忆学习，已删除记忆不能从日志通道自动复活。

<a id="source-line-807"></a>

### 10.7 热配置与实现约束

等级、模块覆写、格式、Web过滤和保留策略由[M15](../modules/configuration.md#contract)统一变更。普通等级修改可对下一条记录生效；handler拓扑、文件目标等需先创建并校验新资源，在安全边界切换，再排空关闭旧资源，不粗暴清空全部handler导致窗口性无日志或双输出。

Web只接受本系统类型化白名单配置，不直接加载用户提交的任意`dictConfig`、可导入类或可执行表达式。Python官方提醒日志配置中的对象构造/监听机制可能执行代码，因此不把它作为一个未鉴权的通用远程配置入口。[S18](references.md#s18)

[M14](../modules/logging.md#contract)不读取[M15](../modules/configuration.md#contract)之外的另一份“logging.yaml”作为并行真相。配置回退仍通过新版本审计执行；关闭诊断输出不能关闭强制审计，也不能关闭Provider已知usage记录。

<a id="runtime-diagnostics-contract"></a>

### 10.8 统一运行诊断日志：控制台与文件输出最小契约

**状态：日志服务契约已批准，日志服务未授权实现。** 用户已批准本节L1–L4四组决定、参数表中的具体建议，以及文件恢复一致性和独立flush等待期限两项修订；只转换批准状态，不改变行为或验收预期。本节只细化运行诊断日志切片，不代表完整日志模块或§15.1第一行完成。§10.1–10.7、模块所有权和T13保持原样；§11.9、§11.10的已批准配置行为不变。配置纯内存校验按[补充契约](configuration.md#configuration-additional-validation-contract)独立授权；后续能力和未定事项不随本节获准，日志服务实现仍须另行授权，服务例子全部未执行。

<a id="runtime-diagnostics-scope"></a>

#### 10.8.1 依据、批准范围与后续边界

| 性质 | 内容 |
| --- | --- |
| 已有要求 | §5.3及M14：统一日志入口、可信启动装配、最小应急stderr；§10：五个标准等级、同事件同ID、分端过滤、入队前脱敏、有界背压、故障隔离；T13：审计同业务事务提交，诊断文件不能冒充该事务 |
| 本次已批准 | 下述事件准入、过滤公式、JSONL子集、初始化／关闭、逐端投递回执、容量／故障保障和参数表中的具体建议，含文件恢复与独立flush超时修订。集中决定见本节末表 |
| 已有实现事实 | 配置注册表和显式值解析／不可变快照已验收并独立提交；快照只表示受限内存解析结果，没有快照ID、运行revision、权限执行或激活承诺 |
| 后续能力，归属不变 | M14的Web订阅、历史查询、导出、SDK桥接、受控诊断正文捕获、更多格式／时间轮转／压缩、热切换及事务审计；M13的Provider与计量账本；M15的完整校验／加载／激活；I01数据库基础。此切片不提供这些接口或返回成功的占位实现 |

运行诊断、敏感审计和Provider详情的权限仍分离；产品§17.5、§20.3的审计隔离与管理权限继续适用。日志及健康端口仅交可信装配方和内部消费者，不成为agent工具，不用于学习、embedding、记忆使用强化或已删除正文恢复。此处的文件访问权由部署方限制，不宣称本地只读句柄已经实现管理鉴权。

<a id="runtime-diagnostics-events"></a>

#### 10.8.2 事件、输入和安全准入（已批准）

只提供`runtime`事件入口。公开`emit(event)`接收精确内建dict；必填`level`、`event_code`，可选`context`、`attributes`，后二者仅收精确dict。等级仅接受精确str：DEBUG／INFO／WARNING／ERROR／CRITICAL，对应10／20／30／40／50；不接收数值、别名、NOTSET、AUDIT或USAGE作为事件等级。ERROR不终止进程。调用方不能提供event_id、时间、schema版本或logger身份；这些由服务生成。

| 输出白名单 | 来源及条件 |
| --- | --- |
| `schema_version`、`category` | 固定为整数1及`runtime`；是事件格式版本，不是配置版本 |
| `event_id` | 服务在准入成功、过滤之前生成一次随机UUID（规范小写带连字符文本）；每次emit是新事件，不做业务去重。分端复制、格式化备用记录和高等级应急摘要沿用此ID，不用正文哈希或配置对象地址生成 |
| `timestamp` | 从可信装配注入的UTC墙钟取得一次接收时间，RFC 3339格式、固定微秒及Z后缀；不是外部业务发生时间。所有输出沿用该值；墙钟回拨不排序或重写事件 |
| `level_name`、`level_number`、`logger`、`event_code`、`message` | 等级来自已校验输入；logger来自已绑定模块句柄；事件码查静态白名单，message取固定模板，禁止调用方插值 |
| `context` | 仅可含trace_id、span_id、request_id、run_id、batch_id、dream_run_id、entry_id、provider_request_id、attempt_id；值须为可信调用方已确认不含秘密的内部不透明ID，精确str，匹配`[A-Za-z0-9][A-Za-z0-9._:-]{0,127}`；无值就省略，不填空串或伪ID |
| `attributes` | 仅可含count（0至2^63−1的精确int）、duration_ms（同范围的精确int，调用方由单调时钟差计算）、outcome（SUCCESS／FAILURE／DEGRADED）、error_code（TIMEOUT／IO_FAILURE／VALIDATION_FAILED／INTERNAL_FAILURE）；枚举值仅接受精确str，无值就省略，不隐式转换bool、Decimal或float |
| `redacted`、`exception_omitted` | 服务生成的布尔值；前者表示至少一个输入字段被移除，后者表示顶层exception字段存在且已移除；不保存被移除字段名、数量明细或内容 |

初始静态事件码及message为：`DIAGNOSTIC_READY`→“运行诊断服务已就绪。”、`OPERATION_COMPLETED`→“操作已完成。”、`OPERATION_FAILED`→“操作未完成。”、`DELIVERY_RECOVERED`→“诊断输出已恢复。”。事件码说明事实，不代表审计已提交；调用方负责事实与等级匹配。新增模板须经代码审查，不能由运行配置或用户文本注册。内部恢复记录使用DELIVERY_RECOVERED／WARNING；不在本切片引入通用事件注册服务。

`get_logger(module)`仅接受§5.1十五个模块的英文语义名及bootstrap，精确匹配；logging_service为日志模块名，不使用M14等编号。上下文由每次调用显式传入，无隐式全局／线程上下文，也不推测当前批次。config_snapshot_id、runtime_policy_revision及其他自报保留字段按非白名单移除；不得用schema_revision、对象身份、随机数或空占位伪造现有配置模块没有的标识。

安全处理顺序：先核验载体；每层dict最多32项且所有键必须是长度不超过128的精确str（允许空键，随后按非白名单移除），之后才按固定字段读取白名单值。顶层message、exception及所有非白名单字段直接移除，不遍历其值、不求str／repr、不格式化异常、不读取异常args、cause、traceback、局部变量、源码行或SDK对象。context／attributes的非白名单项同样整项移除。因此Authorization、API key、cookie、token、URL、prompt、response、原始文本和附件字段均不进入队列；嵌套敏感内容和带签名URL不能靠改大小写绕过白名单。自由文本及原始异常不提供“自称已脱敏”放行通道；安全错误摘要用固定error_code表达，本切片不输出异常栈。

保留字段载体／范围不合格时拒绝整条，不字符串化或截断后伪装有效。ID语法只控制结构和体积，不证明无秘密；原始外部request ID、用户名、令牌或URL必须由可信调用方先映射为真实内部非秘密关联ID，否则省略，不能因字段在白名单或配置标为public就直接透传。此边界约束受支持内部调用，不是同进程恶意代码沙箱。

入队前完成所有移除、隔离、模板选择和JSON编码，含尾部LF的UTF-8记录不得超过配置上限E；超限拒绝，不分行、不截断。JSON转义换行和控制字符，所有输出恰好一条记录一行，禁用颜色。固定模板编码意外失败时仅尝试一次安全备用记录：保留已生成的ID／时间／等级／logger、固定事件码`DIAGNOSTIC_FORMAT_FAILED`及message“诊断记录格式化失败。”，省略context／attributes，标redacted=true；备用再失败则拒绝并仅走常量应急通知，不回显原始异常。DIAGNOSTIC_FORMAT_FAILED仅由服务生成，不接受调用方提交；固定模板也不是格式字符串。ID源或时钟不能取得合法结果时返回EVENT_BUILD_FAILED，无事件回执，不以空值补齐，至多尝试常量应急通知。

返回前取得所有保留数据和编码结果的独立不可变所有权；不保留原dict、异常或被移除对象引用。emit期间调用方保持输入稳定，返回后任意修改原dict及嵌套dict不影响已接收事件。安全发布后的logger支持并发emit／健康查询；initialize／flush／close由装配方串行调用，允许与emit／健康查询并发。并发emit无跨调用全局顺序保证，同一sink按成功接收的本地顺序写出；flush／close的截点与并发emit原子划分，截点前后归属必须明确。emit取得投递资格与close停止接收原子互斥：尚在规范化且未取得资格的调用，在关闭后返回SERVICE_CLOSED；已取得资格的事件须在关闭截点前确定全部端的入队结果，不出现关门后的迟到入队。线程、锁及队列具体实现不作为批准项。

<a id="runtime-diagnostics-filtering"></a>

#### 10.8.3 等级确定顺序（已批准）

实例默认I必须是五个标准等级之一。sink阈值S可为五等级或NOTSET，NOTSET解析为I；禁用sink不参与最低阈值计算。无模块覆写或模块值为NOTSET时，采集阈值C取`min(I, 所有启用sink解析后阈值)`；没有启用端时只返回DISABLED。模块显式覆写为具体等级时，C直接取该等级，可有意过滤各端共同需要的低等级。模块精确匹配，不按名称父子前缀继承。

一条安全事件先按C判断，再独立按每个启用端S判断，只有`level >= C 且 level >= S`才投递该端。不在根logger或中间handler额外套用更高阈值，不向父logger传播产生重复输出。I=INFO、console=INFO、file=DEBUG、无模块覆写时C=DEBUG；DEBUG只进文件。若模块显式WARNING，则两端都过滤DEBUG／INFO。健康结果公开各模块C及各端解析后的S，供可信管理调用方解释共同过滤范围；本切片不实现Web显示或热修改。

<a id="runtime-diagnostics-output"></a>

#### 10.8.4 输出、资源与生命周期（已批准）

两端只支持上述UTF-8 JSONL。控制台支持stderr（全部等级）或split（DEBUG／INFO到stdout，WARNING以上到stderr），流由可信装配方借出；服务不关闭宿主流、不改动宿主／根logger的handler。文件为经批准诊断专用目录下固定活动文件runtime.jsonl；只支持单进程独占写入、大小轮转和关闭段份数保留，不支持任意handler类、路径模板、时间轮转、压缩或外部logrotate共写。

文件目录须预先存在、非符号链接、可写且由部署方授予本服务独占使用权；须与业务blob、审计、账本及备份目录隔离。每个打开／重命名／删除目标都需限制在该目录、拒绝符号链接和非普通文件；启动发现非本服务命名的条目或无法证明独占所有权时拒绝，绝不清空目录。已有合法日志段可恢复追加；活动文件有未完成尾行或段序异常时拒绝初始化，不擅自截断修复。受信部署方负责不在运行中替换目录；运行时仍检查目标，权限或路径校验失败令文件端故障。

**文件恢复一致性条件（已批准）：** 短写、写入或轮转／保留故障之后，原I/O结束且文件重新可写只是恢复的必要条件。重新接收文件投递前，还须在独占访问下确认活动文件处于完整JSONL记录边界（空文件或每条记录完整且末尾为LF，无残缺尾行）、关闭段及活动文件归属明确、段序与下一段序号一致且无冲突、轮转没有未确认的中间状态，并满足本节关闭段份数、单段大小及稳定状态保留边界。不能仅检查最后一个字节、重新打开成功或一次可写探测就宣告一致；不得将下一条JSON或恢复通知直接追加到残缺尾行。检查只观察既有内容和资源状态，不通过试写业务记录证明可写；仍受既有有界I/O和探测约束，期限内无法确认也视为未通过。任一条件无法确认时文件端保持FAULTED，last_reason为FILE_STATE_UNCONFIRMED，不自动截断、删除、重放、重命名或修复既有内容；本切片不提供修复接口。其他输出端继续按原契约过滤、接收和写出。

设轮转大小B，关闭段上限K，必须E≤B。下一条完整编码记录长L，活动文件非空且当前字节数+L>B时先轮转，等于B可写；禁止拆分事件。关闭段用单调递增段序命名`runtime.<正整数>.jsonl`，启动从已有合法段恢复下一序号，不使用墙钟决定先后。轮转按段序删除最旧关闭段至最多K份，保留不足或删除失败则停用文件端，不继续无界产生新段。当前活动文件、其他目录或未识别条目永不作为清理候选。稳定状态总内容字节≤(K+1)B；一次轮转允许最多额外B的临时空间，磁盘不足进入故障。合法既有段超过B、K或临时空间边界时初始化失败，由管理方处理，不在启动时悄悄执行超范围清理。

生命周期为NEW→READY→CLOSING→CLOSED；初始化失败且资源已收回则回NEW，可显式重试；收回未完成则FAULTED，只允许健康查询和有界close。一个服务成功initialize后再次调用，无论同一快照还是新快照，都返回ALREADY_INITIALIZED，不增加handler、不打开第二份文件、不替换配置；并发initialize不支持。CLOSING／CLOSED／FAULTED不能重初始化。另一个实例竞争同一目录返回RESOURCE_CONFLICT；同进程多实例也不能绕过独占边界。

initialize先检查快照、配置适用性及资源，再准备全部启用端，全部成功才发布READY；任一失败没有可用logger。清理由本次创建的资源负责，已借出的流不关闭，既有文件内容不删改；可能留下本次创建的空活动文件，结果须用布尔cleanup_pending说明是否尚待回收，不能宣称文件系统无副作用。两个端都禁用可READY，emit明确DISABLED；应急通道仍独立存在。成功初始化不等于系统配置激活或业务恢复完成。

接收仅表示输入准入和事件构造成功；入队仅表示某端取得有界投递所有权；写出表示该端完整write返回；flush表示该端缓冲刷新返回。以上状态均不等于介质持久化，不提供fsync收据、崩溃重放或业务事务提交承诺。短写／异常可能已输出前缀，列为UNKNOWN，不重发该事件，避免制造重复。关闭正常结束可确认已入队目标的写出及flush状态，但不能保证终端用户已看到，也不能保证掉电后文件存在。诊断失败不撤销已提交业务，不影响应由独立事务保证的审计或账本。

<a id="runtime-diagnostics-delivery"></a>

#### 10.8.5 有界投递、故障与健康（已批准）

每端配置总容量Q及WARNING以上预留R，约束0<R<Q；计入待写及正在写的记录，不能把在途记录移出容量统计。DEBUG／INFO只在该端总占用<Q−R时接收，否则丢弃新到低等级事件；WARNING以上只要占用<Q即可接收。无抢占、采样或淘汰旧事件。每端最多Q×E编码字节；两端合计不超过2QE，UNKNOWN的在途缓冲直到实际I/O结束仍计占用。容器开销另有有限上界。保留并发规范化槽位P，其中至少一个仅供WARNING以上使用；低等级最多P−1个槽，高等级可用全部P个，耗尽立即返回ADMISSION_BUSY。单次输入遍历受字段数、保留值长度及E限制；不创建无界辅助缓冲或无限增长的logger／事件码／指标标签集合。模块自有编码缓冲总预算为最多2QE+PE+512×应急容量字节，包含入队前在途编码与备用编码；实现须将临时副本计入对应槽位上界，不能因复制另开无限预算。此预算不包含调用方原输入或操作系统缓存。

emit不等待任何sink I/O、磁盘空间或队列腾位；仅作有界本地准入，不给普通调用链追加输出超时。同步处理和竞争必须有界，不作绝对硬实时承诺，仍须在后续真实一秒查询集成中测量开销。两端独立决定入队结果：文件阻塞不延迟控制台入队和写出，反之亦然；不能把两端串行调用挂在同一个会阻塞的输出执行上下文上。

任一目标端对WARNING以上无法入队（满载、故障）时，整个事件最多尝试一次独立stderr应急摘要，只含event_id、level、固定reason和固定message“高等级诊断未完成常规投递。”，不携带context、attributes或原始输入。摘要不是完整事件已送达；即使摘要成功，目标端丢弃仍计数。规范化槽耗尽时尚未生成event_id，只能发不带ID的常量告警，不伪造原事件标识。应急通道也必须有独立容量、单条字节上限及频率限制；满载／限流／stderr阻塞均可失败，不在调用线程同步兜底写stderr。

输出I/O超过配置时限或出现打开／写／flush／轮转错误时，端状态转FAULTED，故障端拒绝新目标投递，其他端继续。对尚未开始写的积压明确丢弃并计数；已经开始但结果未确认的目标列UNKNOWN。每端至多一个未完成I/O，不为卡死调用无限创建替代执行器。原调用结束后才能按配置探测间隔进行一次恢复探测；永久阻塞保持FAULTED而非虚报恢复。探测失败不重放旧事件、不循环递归记日志；文件端的探测成功必须同时满足§10.8.4文件恢复一致性条件，仅原调用结束或资源恢复可写不得转READY；相应端的全部恢复条件通过才恢复READY，并有界尝试一次DELIVERY_RECOVERED记录，attributes.count为自上次恢复以来该端新增的确定丢弃目标数（饱和规则同计数器）；UNKNOWN另由健康结果说明。恢复时累计丢弃／未知计数不清零；该恢复记录也可能被过滤或丢弃，健康查询仍是可观察依据。

固定备用／应急通道不调用emit，不再记录自己的失败；按频率上限合并故障通知，累计emergency_suppressed／emergency_failed，避免递归死锁和日志风暴。普通console采用stderr时与应急仍须投递隔离：借用同一物理流失败时两者均可能不可用，不宣称独立物理故障域。不承诺资源耗尽、强杀或掉电下诊断零丢失。

`get_sink_health()`返回深不可变内存视图：服务生命周期；按console、file固定顺序的DISABLED／READY／FAULTED／CLOSED状态及固定last_reason；解析后阈值；每端queued_events、in_flight_events、容量、最近完整成功写出的UTC时间（无成功为None）、written_events、flushed_events、unknown_events、`dropped_events{sink,level,reason}`；另有filtered_events、rejected_events、emergency_suppressed／emergency_failed、flush_deadline_exceeded及cleanup_pending。flush_deadline_exceeded按独立flush调用计数：到期时任一启用端仍未完成则增加一次，不按端重复增加，沿用饱和规则；仅等待到期不修改端last_reason或丢弃／未知计数。sink故障不隐含磁盘已满，磁盘告警为OK／LOW／UNKNOWN，仅在资源适配层能确认空间不足时LOW，探测不可用为UNKNOWN；不虚构余量。时间来自注入时钟，耗时／deadline用单调时钟。

丢弃计数以每个目标端的每次未投递为一项，同事件两端失败可计两项；等级过滤／端禁用不计丢弃，非法输入只计rejected，UNKNOWN不重复计丢弃。成功入队后未开始写却被故障／关闭放弃也计丢弃；格式备用成功不计丢弃。计数仅本进程内有效，使用有界饱和计数器（2^63−1封顶并置counters_saturated=true），不要求落盘。标签限上述固定枚举，禁止请求ID、用户键、路径或异常文本成为指标标签。UNKNOWN是曾出现无法确认写入的累计诊断，不在迟到完成后改写旧回执或冒充确知丢失。报告对截点前曾成功入队的目标，在报告生成时分为written／dropped／unknown／pending_queued／pending_in_flight五个互斥类别；前三项按该截点累计，pending_queued表示尚未开始写、pending_in_flight表示已开始写但仍在正常I/O期限内等待结果，后二项只是当前未完成数量，不是终态或丢弃原因码。未饱和时五项之和等于该端截点内曾入队目标数。flushed另为written的子集，只计实际刷新已确认的目标；写出完成但刷新未确认时仍计written，不虚增flushed。入队前丢弃只在EmitReceipt和健康计数体现。UNKNOWN仅用于实际I/O故障／I/O超时或关闭放弃后无法确认的写入结果，不用于独立flush等待期限到达；被归为UNKNOWN的迟到完成不重新计入written／flushed，不重复增加unknown，最近成功写出时间仍可反映实际迟到成功。反之，pending目标随后正常完成时，健康queued_events／in_flight_events反映占用减少，written_events增加，实际刷新确认后flushed_events才增加；后续报告按新观察分类，已经返回的不可变报告不修改，也不为这次等待超时补记UNKNOWN或dropped。

**独立flush等待期限（已批准）：** flush取调用截点，等待该点之前已入队目标完成并刷新两端，使用配置的总deadline，不因两端分别等待而翻倍；并发emit的后续事件不延长截点。deadline到达只结束本次等待，受影响端报告INCOMPLETE／DEADLINE_EXCEEDED（已记录故障仍按首错规则优先），服务保持READY，输出端不因等待到期从READY转FAULTED或CLOSED；原已故障／禁用端也不因此恢复。截点前尚未开始的目标留在有界队列，已开始且未发生独立I/O故障的目标继续在途，分别报告pending_queued／pending_in_flight，仍计容量；两者均不增加dropped／unknown／written。截点后新事件继续按原等级、健康和容量规则接收，可能正常入队或因满载／端故障被拒绝投递，不因flush到期使用SERVICE_CLOSED或SHUTDOWN_DROPPED。

flush报告仅描述本次截点，不建立事件重试、关闭或持久化承诺。到期时未发出的本次缓冲刷新请求不另留后台任务；正常事件写出继续，已开始的刷新I/O则在既有单端I/O期限内继续，不为同一端开启第二个并行I/O。后续flush可以等待现有I/O后再刷新自己的截点，不积累每次超时调用专属的无界等待者；没有实际刷新确认时不能因写出迟到完成就宣称FLUSHED。单端I/O自身随后失败或达到io_timeout_ms，仍按前述故障规则转FAULTED并分类目标；这是独立故障，不是flush等待到期的隐含副作用。

**close关闭期限：** close先原子停止新接收，再在一个总deadline内drain／flush及回收自有资源，应急也计入此deadline；重复close返回首个CloseReport，不重新执行写出或重复清理。在关闭处理中，close到期后不再启动新写出，实际放弃的未开始目标计SHUTDOWN_DROPPED，在途标UNKNOWN；不可取消的I/O可能迟到写出，CloseReport须含cleanup_pending=true，路径独占权直到实际资源回收才释放，不能承诺方法返回即所有资源已关闭。后台仅允许完成既有I/O与回收，不无限重试；健康视图可反映回收完成，原报告保持不变。宿主应在退出进程时处理永久阻塞资源，本切片不承诺杀死线程或进程级恢复。

<a id="runtime-diagnostics-configuration"></a>

#### 10.8.6 配置接入、已批准参数及前置缺口

可信装配方通过统一配置模块注册完整定义、冻结并显式解析，取得原生EffectiveSnapshot后调用initialize；服务只用get_registry／get_entry／list_entries读取绑定的不可变内容，不读环境、文件、数据库，不自行resolve或建立defaults副本。资源能力（借用流、时钟、UUID源、目录访问与独占能力）由启动代码／基础设施注入，不携带可覆盖配置值的第二套参数。快照在服务生命周期内固定，无热替换、fallback、内容哈希ID或运行revision。缺项或能力不足时拒绝初始化，不默默补值。

静态核对依据为[已批准解析子集](configuration.md#configuration-resolution-support)及configuration的resolution.py、snapshots.py、resolution_results.py，与test_resolution_boundaries.py、test_snapshots.py相关断言。对应版本的文本检查与执行记录见[CURRENT_TASK](../work/CURRENT_TASK.md)。原resolve_configuration入口支持六类精确载体、required／nullable、单参数range／enum；整个冻结集合的validator和dependencies必须为空，scope仅instance、override_policy仅no_override、sensitivity仅public，拒绝兼容升级声明。对象内部没有字段Schema、长度约束或映射键规则；角色和生效字段只保存声明。public不是已脱敏或已授权证明。

**原解析入口不能证明日志配置完整有效；生产装配仍有独立前置缺口。** 模块覆写映射的模块名／等级／数量、路径隔离、R<Q、E≤B等需要附加或跨参数校验。定义方必须保留必要validator／dependencies；原resolve_configuration入口将如实返回UNSUPPORTED_RESOLUTION_SEMANTICS（VALIDATOR_NOT_SUPPORTED优先于同定义的DEPENDENCIES_NOT_SUPPORTED），包括未提供值的定义。不能清空声明、删掉不支持定义、只截取可解析子集或通过日志私有校验器制造成功快照。

M15的受限、静态白名单验证能力已另行获批：执行明确标识的纯内存validator、读取已声明dependencies完成本表约束、未知验证器或依赖语义拒绝、全部检查成功才产生原有形态的不可变快照；不开放任意回调／动态导入，不要求同时实现加载、热配置或权限系统。目录真实存在、可写、独占与符号链接等易变事实由日志资源准备再次核验，不能被纯内存校验替代。该补充的端口和错误协议见[最小配置校验补充契约](configuration.md#configuration-additional-validation-contract)；§11.9／§11.10的既有行为不变。本轮仅授权该显式扩展入口的纯内存实现及合成测试，实际进度和验证见[CURRENT_TASK](../work/CURRENT_TASK.md)。生产路径若需非public敏感级别，也必须另补相应安全能力，不能为了适配解析器降级标签。完整生产配置接入在此缺口解决前不可宣称可实施通过。

下表的**新增生产参数、具体默认建议及约束已批准**，尚不表示参数已注册，也不是合成测试值。共同元信息中的具体建议同获批准，明确未定项仍待决定：owner／consumer为logging_service，作用域instance、无配置层级覆写（no_override），required=true、nullable=false，仅初始化生效（声明需重建实例），角色为可信运维读写；普通数值／枚举为public，目录须部署安全审查。模块“覆写”是一个实例参数内的日志路由语义，不是配置解析器的多层override_policy。参数定义及唯一默认只能放M15；下方[共同元信息与逐项差异](#runtime-diagnostics-schema)给出§11.9全部字段落点，尚未决定的内容明确标出，不能统一填空validator／dependencies或把待定当NotApplicable。

| 已批准键（logging.前缀） | 类型、已批准默认方案及约束／理由 |
| --- | --- |
| instance_level、module_levels | string INFO；object {}，仅§10.8.2模块名→五等级或NOTSET，至多16项；控制默认采集与精确模块覆写，映射需validator |
| console_enabled、file_enabled | boolean，各true；允许分别禁用，不影响审计／账本 |
| console_level、file_level | string，分别INFO、DEBUG；枚举五等级及NOTSET |
| console_stream | string stderr；枚举stderr／split；JSONL为固定子集，无格式／颜色可执行配置 |
| file_directory | string，明确NoDefault且必填；绝对诊断专用目录，最长4096字符、不含控制字符／凭据；需路径validator，真实资源准备再次核验 |
| event_max_bytes | integer，4096；512至65536字节，含LF；限制单条记录与备用记录 |
| sink_capacity、warning_reserve | integer，分别1024、128；Q在2至65536，R在1至65535且R<Q；reserve声明依赖capacity，需validator |
| preparation_capacity | integer，16；2至256；限制同时处理输入的槽位，包含一个高等级预留槽 |
| rotation_bytes、retained_segments | integer，分别10485760、5；B在512至1073741824且B≥E，K在1至100；rotation声明依赖event_max_bytes，限制文件占用 |
| io_timeout_ms、probe_interval_ms | integer，分别200、1000；各1至60000毫秒；单端I/O时限和恢复探测最小间隔 |
| flush_timeout_ms、close_timeout_ms | integer，分别1000、2000；各1至60000毫秒；每次调用的总等待上限 |
| emergency_capacity、emergency_interval_ms | integer，分别8、1000；分别1至64项、1至60000毫秒；应急单条固定上限512字节，每间隔最多一条，超额合并计数 |

不将不变量（脱敏、审计隔离、无递归、禁止伪造版本）做成可关闭参数。私有执行器／队列数量如实现需调节，仍归M15声明，不新增局部默认；本契约不为选择某线程库另设审批项。

<a id="runtime-diagnostics-schema"></a>

**日志实施前置规格：共同Schema元信息与逐项差异。** 上表20个完整键、类型、默认值、范围和日志行为已批准，配置§11.11的C1–C4也已另行批准；本段不改它们。下列具体表示及定义匹配细节已作为G1成组批准；目录事实、分级与资源核验仍归待定G2，不阻止明确合成夹具的纯内存验证。说明性文字已在本轮补齐，不作为逐项审批清单。每项定义由上表对应键、下列共同元信息及差异行组成，字段没有隐含省略；存在待定字段不等于可注册完整生产Schema，不能将文档中的“待定”传入注册表。

| §11.9字段 | 共同定义及逐项取值位置 | 依据／状态 |
| --- | --- | --- |
| key、type、default | key为`logging.`加上表单个后缀；type取对应类型。default只从上表取值：无默认项为NoDefault()，其余为LiteralDefault(上表对应原值)，保留精确bool／int／str／dict载体，不另存默认表 | 已批准内容的结构化表达 |
| owner_module、consumers | 分别为`logging_service`和`["logging_service"]` | 既有owner／consumer；列表是完整生产定义建议，消费权限并不由自报声明授予 |
| schema_revision | 建议统一`runtime_logging`，仅作不透明Schema标识；不含任务编号，不代表配置值、快照或激活revision | G1已批准该表示；匹配不依赖这个具体字符串 |
| required、nullable、scope、override_policy | 分别为true、false、`["instance"]`、`no_override` | 已批准；file_enabled=false也不豁免必需值 |
| sensitivity | 除file_directory外统一`public`，其中module_levels仅含已限定的模块名与等级，不接收任意对象字段；file_directory须经G2确认，不预填public | 普通数值／枚举public及G1完整逐类型表示已批准；目录分级仍待定 |
| read_roles、write_roles | 两者均为`["trusted_operator"]`，仅表示既定“可信运维读写”；G1已批准该标识，不声明成员、不建立角色继承或权限实现 | 角色范围及G1具体绑定标识已批准；不可用空列表冒充决定 |
| apply_mode、activation_group | 采用`INITIALIZE_ONLY`及NotApplicable("日志服务仅在初始化时取得完整配置，不参与在线激活分组。")；前者表示重建日志服务实例后方可采用新值，不要求或声称执行进程重启 | 初始化生效行为及G1表示已批准，不扩充全局apply_mode封闭枚举 |
| unit | 差异表有单位者用Declared(对应标识)；其余为NotApplicable("该参数为开关或文本选择，不使用计量单位。") | 量纲承接已批准行为，G1标识拼写已批准 |
| range | 所有integer用Declared(RangeDescriptor(Bound(上表该键下界,true), Bound(上表该键上界,true)))；非integer为NotApplicable("该参数不是数值范围参数。")。R<Q、E≤B是附加检查，不能变成从其他值计算range | 只引用原闭区间，不复制数值或引入求值 |
| enum | instance_level用Declared(五个标准等级按严重性升序排列的列表)；console_level、file_level在该列表末尾追加NOTSET；console_stream用Declared(["stderr","split"])；其余为NotApplicable("此参数不使用整值枚举；其他约束由类型、范围及必要验证器执行。") | 枚举成员已批准；生产列表顺序仅为表示，不限制语义相同的成员顺序 |
| validator、dependencies | 四项必需验证器及两项必需依赖按[配置白名单契约](configuration.md#configuration-additional-validation-contract)绑定：module_levels、file_directory、warning_reserve、rotation_bytes各使用唯一对应标识；warning_reserve引用logging.sink_capacity，rotation_bytes引用logging.event_max_bytes。其余生产定义建议为[]；目录清单是显式上下文，不伪造为配置依赖键 | 必需声明已批准；最小列表不收紧C2允许的额外依赖，详见[匹配边界](configuration.md#configuration-logging-definition-match) |
| deprecated、replacement、upgrade_rule | false；NotApplicable("本参数未废弃，无替代键。")；NotApplicable("本定义不提供历史配置值升级规则。") | 承接已批准解析支持边界；不实施兼容升级 |
| cost_impact | "该参数只影响本地运行诊断资源开销，不发起付费模型调用或计量结算；不执行费用估算。" | 本轮说明文字，不改变计费或Provider所有权 |
| migration_impact | "配置只在新建日志服务时采用，不自动搬迁或修复既有日志，不迁移业务数据库、审计或账本；既有文件不符合初始化条件时须由管理方处理。" | 本轮说明文字，承接初始化与文件恢复边界 |
| description、rationale、validation_method | 逐项取下表三列完整文本；validation_method前加共同文本“使用显式合成输入核对边界、安全错误和不可变性；” | 本轮普通说明；描述预期验证方式，不声称执行过 |

下表只列单位及说明差异；类型、默认与数值范围继续取上方唯一参数表。“无”按共同unit规则生成NotApplicable，不是新增运行时标记。bytes／events／slots／segments／milliseconds分别表示字节、事件项、规范化槽位、关闭文件段及毫秒，不提供单位换算。

| 键后缀 | unit标识 | description | rationale | validation_method差异文本 |
| --- | --- | --- | --- | --- |
| instance_level | 无 | 实例默认诊断等级。 | 提供统一的等级继承基准。 | 核对标准等级准入及启用输出端的最低采集阈值。 |
| module_levels | 无 | 各模块的精确等级覆写映射。 | 允许按模块控制采集范围。 | 核对模块白名单、项数、等级及NOTSET继承。 |
| console_enabled | 无 | 是否启用常规控制台输出。 | 允许独立控制控制台诊断。 | 核对禁用回执、独立应急通道及文件端不受影响。 |
| file_enabled | 无 | 是否启用常规文件输出。 | 允许独立控制文件诊断。 | 核对禁用端无常规投递，配置约束仍完整检查。 |
| console_level | 无 | 控制台输出的最低诊断等级。 | 控制控制台记录量。 | 核对分端阈值及NOTSET取实例默认等级。 |
| file_level | 无 | 文件输出的最低诊断等级。 | 控制持有的诊断等级范围。 | 核对低等级文件记录不被其他端提前过滤。 |
| console_stream | 无 | 控制台等级与标准流的路由方式。 | 适配宿主的标准流接收方式。 | 核对stderr和split路由且不关闭宿主借出流。 |
| file_directory | 无 | 诊断日志的独占输出目录。 | 隔离日志轮转与业务数据。 | 核对路径语法、目录隔离及独立资源准备失败。 |
| event_max_bytes | bytes | 单条编码记录含换行的最大字节数。 | 限制事件编码和队列内存占用。 | 核对UTF-8字节边界、超限拒绝及与轮转大小的关系。 |
| sink_capacity | events | 每个输出端的事件总容量。 | 限制排队与在途记录的总占用。 | 核对排队和在途共同计容量且达到上限拒绝新投递。 |
| warning_reserve | events | 每个输出端为高等级保留的容量。 | 在低等级拥塞时保留告警接收空间。 | 核对预留小于总容量及高低等级各自准入边界。 |
| preparation_capacity | slots | 同时进行事件规范化的槽位总数。 | 限制入队前的并发处理与编码内存。 | 核对高等级预留槽及耗尽时立即拒绝。 |
| rotation_bytes | bytes | 活动文件按大小轮转的字节阈值。 | 限制单段大小且避免拆分事件。 | 核对等于阈值可写、超过前轮转及不小于事件上限。 |
| retained_segments | segments | 保留的关闭文件段最大份数。 | 限制既有诊断文件占用。 | 核对按段序删除最旧关闭段及删除失败停用文件端。 |
| io_timeout_ms | milliseconds | 单次输出I/O的等待时限。 | 隔离阻塞或故障输出端。 | 核对I/O超时故障、UNKNOWN计数及每端至多一个在途I/O。 |
| probe_interval_ms | milliseconds | 输出端恢复探测的最小间隔。 | 限制故障探测频率。 | 核对原I/O结束后才探测且间隔不小于配置值。 |
| flush_timeout_ms | milliseconds | 一次独立flush的总等待期限。 | 限制调用方等待时间。 | 核对到期仅结束等待，不关闭服务或丢弃正常在途目标。 |
| close_timeout_ms | milliseconds | 一次关闭操作的总等待期限。 | 限制排空、刷新与回收的总耗时。 | 核对关闭截点、期限内回收及未完成资源的占用标记。 |
| emergency_capacity | events | 应急摘要通道的容量。 | 限制常规输出故障时的额外内存。 | 核对容量耗尽、独立投递和常量摘要上限。 |
| emergency_interval_ms | milliseconds | 应急摘要发送的最小间隔。 | 抑制重复故障通知。 | 核对限频、合并计数及不会递归记录应急失败。 |

<a id="runtime-diagnostics-directory-sources"></a>

**五类受保护目录的来源核对。** [现行部署候选§2.3](deployment-candidates.md#source-line-123)是一份候选容器布局，并非真实挂载清单或目录存在性证明；下表路径仅复述候选，不成为默认值。资源持久化归[I01](ownership.md#i01)，可信启动装配方须从最终确认的同一部署布局提供§11.11的完整清单；不从日志参数推算或反向发现业务路径。

| 输入类别 | 现行来源及可知内容 | 缺少的信息与最小建议（待确认G2） |
| --- | --- | --- |
| media | 候选有`/data/blobs/sha256/`及`/data/upload_staging/`；前者为原始媒体，后者为未提交上传 | 实际媒体根、暂存及其他媒体落地目录未确认；建议同时覆盖正式blob与上传暂存，不能只保护hash叶目录 |
| database | 候选`/data/db/`容纳权威库及WAL等伴随文件，SQLite仍为候选 | 实际库所在目录与其他伴随／临时落地位置未确认；建议由基础设施给出完整受保护目录，不要求本轮选择数据库实现 |
| audit | [审计事务](persistence-and-transactions.md#t13)要求同业务事务保存；I01承载审计持久化，候选另有可选`/data/audit_exports/` | 实际审计存放目录及是否启用导出未确认；若同库则复用已确认database目录，并加入实际启用的导出目录，不能只填导出目录代替审计正文所在目录 |
| provider_usage | [Provider拥有持久账本](../modules/provider.md#contract)，I01承载其持久化；候选没有独立账本目录 | 实际是否同库及其他账本落地目录未确认；若同库可重复列入database目录，不凭空创建usage专属路径 |
| backup | [备份边界](persistence-and-transactions.md#source-line-226)要求覆盖数据库、媒体与必要配置；候选目录树未列备份目的地 | 备份目的目录、暂存／导出位置及是否只有远端存储均未知；建议明确提供所有本地备份落地目录。仅远端或尚无布局不能伪填空列表，须先解决与已批准非空目录输入的衔接 |

此外，日志自己的候选位置为`/data/logs/runtime/`，仍不是file_directory的默认或已选生产值。最终清单须提供日志服务实际使用的同一文件系统命名空间中的绝对目录；候选的尾部斜线只是目录展示，不直接当作§11.11的规范输入。宿主挂载、真实存在性、符号链接／别名、可写与独占均未在本轮核验；原有资源准备检查不减免。不能将整个共同数据父目录填作某一类别来掩盖未知布局，否则可能与合法兄弟目录的日志位置产生祖先重合。

<a id="runtime-diagnostics-prerequisite-decisions"></a>

**前置事项状态（G1已批准，G2待定）：**

| 事项 | 最小建议与界限 |
| --- | --- |
| G1 Schema表示与定义匹配（已批准） | 已成组批准上述标识表示、非路径字段分级和可信运维角色绑定，以及[LOGGING_DEFINITION_MISMATCH比较口径](configuration.md#configuration-logging-definition-match)。采用runtime_logging、INITIALIZE_ONLY、trusted_operator及差异表单位标识；不逐条批准普通说明文字，不将不透明schema_revision变成准入版本号，不收紧已批准依赖语义 |
| G2 部署目录与路径安全分级（待定） | 由部署方提供日志目录及五类受保护目录的真实清单，确认同库关系、可选导出、备份／暂存和同一命名空间；候选可供选择但本轮不选生产路径。确认file_directory是否确属可按public处理的非秘密路径；若需非public，保持真实分级并另补能力，不降低标签通过解析 |

G1已批准；G2真实目录、路径敏感分级及真实资源核验仍是生产装配前置。本轮授权范围仅为配置模块的纯内存校验，可用明确非秘密的public合成路径、完整合成Schema与五类目录清单验证，不创建这些目录，不把夹具变为生产默认或真实安全分级。已批准默认及匹配依据在配置模块内统一维护，不从文档读取运行规则、不建日志私有配置副本、不自动注册参数。未授权日志服务、真实资源准备、生产装配、加载、持久化、权限或热修改；日志服务验收例子仍不代表已执行。

<a id="runtime-diagnostics-results"></a>

#### 10.8.7 最小公开接口、结果和错误（已批准）

以下为语义签名；不要求类名机械照抄，但实现公开接口需完整表达这些行为。结果及嵌套记录均深不可变。LoggingOk／LoggingErr独立于配置模块原有结果协议；预期错误不抛携带输入的异常，不经日志再打印错误对象。签名缺少实参／未知关键字按语言规则拒绝；资源耗尽等无法完成结果构造的非预期故障不伪装成功。

| 接口 | 结果及边界 |
| --- | --- |
| create_logging_service() → Service | NEW，无外部I/O、导入即注册或全局handler副作用；装配方持有生命周期端口 |
| Service.initialize(snapshot, resources) → LoggingResult<Unit> | 全部准备后READY；没有部分可用成功。失败含cleanup_pending，不返回文件路径或底层异常 |
| Service.get_logger(module) → LoggingResult<Logger> | 绑定已知模块的受限emit句柄；重复取得不增加输出资源，不要求对象身份相同 |
| Logger.emit(event) → LoggingResult<EmitReceipt> | 准入成功即有event_id；按console、file固定顺序给出DISABLED／FILTERED／ENQUEUED／DROPPED及reason，并给emergency=NOT_NEEDED／SCHEDULED／SUPPRESSED／UNAVAILABLE；SCHEDULED仅为摘要入队。全部FILTERED／DISABLED仍是可解释回执，全部DROPPED也不伪装已写出 |
| Service.get_sink_health() → HealthSnapshot | §10.8.5内存健康与安全计数；任何生命周期可查，NEW无成功时间、容量及阈值未装配时为None，公开C／S，不返回快照原值、路径或事件正文 |
| Service.flush() → LoggingResult<FlushReport> | READY可调用；固定截点前每端给FLUSHED／INCOMPLETE／DISABLED，以及written、flushed、dropped、unknown、pending_queued、pending_in_flight计数和固定reason；FLUSHED要求该截点目标全部完整写出且flush成功，否则INCOMPLETE。总deadline只限制此次等待，不关闭服务、丢弃队列或把正常在途记为UNKNOWN；报告返回后固定，迟到完成通过健康和后续flush报告观察，不承诺持久化 |
| Service.close() → CloseReport | 任意状态可调用；首调用停止接收，返回每端DRAINED／INCOMPLETE／DISABLED及同口径计数、reason、cleanup_pending；关闭完成分类后pending_queued／pending_in_flight均为0，实际放弃归dropped／unknown，资源可能仍在途并以cleanup_pending说明；DRAINED还要求flush及自有资源回收完成。服务进入CLOSED；重复返回首报告，不使旧logger恢复 |

LoggingError恰含code、operation、field（单个固定字段标识）、reason、cleanup_pending；只有initialize清理未完成可将后者置true。无任意message、输入键／值、路径、异常或对象引用。EmitReceipt不含正文和关联字段。所有固定原因码如下，均为大写；报告的成功／无动作reason为NONE：

| code或报告位置 | 固定reason与触发 |
| --- | --- |
| INVALID_STATE | NOT_INITIALIZED（NEW下取logger／emit／flush）、ALREADY_INITIALIZED（READY下initialize）、SERVICE_CLOSED（CLOSING／CLOSED）、SERVICE_FAULTED（FAULTED，仅健康及close可用） |
| INVALID_CONFIGURATION | SNAPSHOT_REQUIRED（非原生快照）、CONFIGURATION_REQUIRED（必要键未注册／MissingValue）、CONFIGURATION_UNSUPPORTED（尚缺日志所需校验能力或声明不符）、CONFIGURATION_INVALID（安全可判定的值约束失败）；不改配置模块错误协议 |
| INITIALIZATION_FAILED | RESOURCE_CONFLICT（无法独占）、RESOURCE_INVALID（路径／流／注入能力不符合资源契约）、RESOURCE_OPEN_FAILED（准备I/O失败）、IO_TIMEOUT（准备超时） |
| INVALID_LOGGER | MODULE_NOT_ALLOWED；不回显模块输入 |
| INVALID_EVENT | INVALID_SHAPE、INPUT_LIMIT_EXCEEDED、MISSING_FIELD、LEVEL_NOT_ALLOWED、EVENT_CODE_NOT_ALLOWED、FIELD_VALUE_INVALID、EVENT_TOO_LARGE、FORMAT_FAILED；按输入及编码步骤分别触发 |
| ADMISSION_REJECTED | ADMISSION_BUSY：规范化槽位满，尚未取得事件所有权；EVENT_BUILD_FAILED：ID／时钟源失败或返回非法结果。二者均无事件回执；可信已核验等级可用于固定计数及高等级常量应急通知，不回显事件 |
| FILTERED／DISABLED | MODULE_THRESHOLD、SINK_THRESHOLD、SINK_DISABLED；无日志损失计数 |
| DROPPED及健康丢弃标签 | QUEUE_FULL、SINK_UNAVAILABLE、SHUTDOWN_DROPPED；分别为无容量、故障未开始写、实际关闭放弃的未开始写目标；独立flush期限到达不使用SHUTDOWN_DROPPED |
| 健康last_reason／报告INCOMPLETE | WRITE_FAILED、FLUSH_FAILED、ROTATION_FAILED、RETENTION_FAILED、FILE_STATE_UNCONFIRMED、IO_TIMEOUT、DEADLINE_EXCEEDED、DELIVERY_LOSS、RESOURCE_CLOSE_FAILED；FILE_STATE_UNCONFIRMED表示文件恢复一致性未确认；DEADLINE_EXCEEDED用于报告等待未完成，独立flush到期不写入端last_reason；实际已开始写的故障计UNKNOWN，正常等待未完成使用pending计数 |
| 应急／备用诊断 | EMERGENCY_LIMIT、EMERGENCY_UNAVAILABLE、FORMAT_FAILED；只用于固定通知或计数，绝不拼入原输入 |

必要首错顺序固定：生命周期→载体／形状→字段合法性→资源／投递；失败不再执行依赖后续步骤。initialize在NEW先核验原生快照，再按参数表完整键Unicode排序逐项检查必要定义及存在状态，再检查支持能力，再检查值约束，最后资源准备（console先file）；值约束问题按键排序返回首个。缺口未补时返回CONFIGURATION_UNSUPPORTED，不调用私有快照构造或文件准备；READY下即使传坏快照仍先ALREADY_INITIALIZED。

get_logger先生命周期再模块精确准入。emit先生命周期，随后顶层dict载体／项数／全部键载体及长度；然后按level、event_code顺序检查存在及准入；再申请规范化槽位，满则ADMISSION_BUSY。取得槽位后按context、attributes顺序核验dict形状／项数／全部键，再按本节白名单列举顺序核验保留值；未知字段仅移除。之后生成ID／时间、格式化／大小检查，最后模块过滤、逐端判断。sink判断顺序为禁用→模块过滤→sink过滤→故障→容量，不因端故障把本来应FILTERED的事件计丢弃。未知码与非法context同时出现先EVENT_CODE_NOT_ALLOWED；非法输入即使等级会被过滤也先拒绝。检查键格式时不访问不支持键的哈希、比较或转换钩子。

错误field只可为state、snapshot、configuration、resources、module、event、level、event_code、context、attributes，其他细节由固定reason表达；非白名单键名不进入错误路径。后台多个失败全部按固定端记录；单端报告只取首个障碍，顺序为已记录I/O故障→文件恢复一致性未确认→本次deadline→已有投递损失→资源回收失败，其他问题留健康计数，避免用较晚异常掩盖已知写入失败。flush／close不是无条件LoggingOk(Unit)；INCOMPLETE必须通过报告显式处理。

<a id="runtime-diagnostics-examples"></a>

#### 10.8.8 具体验收例子（合成，全部未执行）

以下只定义输入和预期，不是现有测试或生产默认。共用合成场景：module=bootstrap；I=INFO、console=INFO、file=DEBUG、无模块覆写、两端启用、console_stream=stderr；E=4096、Q=4、R=1、P=2、B=8192、K=2；I/O时限20ms、探测间隔100ms、flush／close总时限50ms；应急容量2、间隔100ms。文件目录为隔离临时目录中显式准备的空目录，双流用可控资源替身，事件ID源在观察时记为e1／e2（不是实际UUID值），UTC时钟固定为2026-09-09T00:00:00.000000Z。其余必要值显式给出；需要完整初始化的例子以配置前置能力已另行批准并具备为前提，不通过删validator／dependencies构造假成功快照。

| 输入／操作 | 预期可观察结果（未执行） |
| --- | --- |
| emit({level: DEBUG, event_code: OPERATION_COMPLETED}) | C=DEBUG；console FILTERED／SINK_THRESHOLD，file ENQUEUED；正常drain后仅文件一行DEBUG／10，不误丢DEBUG |
| emit({level: INFO, event_code: OPERATION_COMPLETED, context: {request_id: request-7}, attributes: {count: 2}}) | 两端各一行，event_id都为同一个e1，时间／schema_version／message相同；重复emit得到不同e2，不去重 |
| 独立实例将bootstrap覆写WARNING；再发DEBUG和INFO | 两端均FILTERED／MODULE_THRESHOLD，不计dropped；覆写NOTSET则回到C=DEBUG。将file_level改NOTSET的独立实例，其S=INFO，DEBUG两端过滤 |
| 输入增加Authorization: Bearer secret-demo、message: 带签名URL、exception: 带Authorization文本的异常；context增加token，attributes增加嵌套prompt | 输入仍含合法必填项时成功，所有增加内容整项移除，redacted=true、exception_omitted=true；输出只含固定message，三条通道和所有错误均无secret-demo、URL或异常正文。移除字段中的对象即使str／repr会抛错也不调用 |
| attributes.count=True，同时顶层含秘密字段；或context.request_id含换行／URL | INVALID_EVENT／FIELD_VALUE_INVALID，field分别attributes／context；错误不含原值。非法event_code与坏context并存先EVENT_CODE_NOT_ALLOWED；超过32顶层项先INPUT_LIMIT_EXCEEDED，无递归扫描 |
| 增加config_snapshot_id: invented及runtime_policy_revision: 9 | 两字段移除，redacted=true；输出、回执、健康不生成替代配置ID；schema_version仍是事件格式1 |
| INFO事件成功emit后将原context.request_id从request-7改为request-8，将attributes.count从2改为9 | 已入队两端仍为request-7、2；公开回执和健康嵌套结构不可改。两线程各发送一次合法事件不会共享可变上下文或重复安装handler |
| 同Service initialize成功后，用同一快照再调用，或用坏快照再调用 | 均ALREADY_INITIALIZED，原端和配置不变；之后一个INFO仍每端一行。第二Service用同目录→RESOURCE_CONFLICT，原实例正常工作 |
| file准备失败，而console已准备 | INITIALIZATION_FAILED／RESOURCE_OPEN_FAILED，无READY句柄，借用流仍打开；资源收回则NEW，可显式重试；若回收阻塞则FAULTED及cleanup_pending=true |
| 冻结定义含logging.warning_reserve的非空validator和dependencies，再调用现有resolve_configuration | UNSUPPORTED_RESOLUTION_SEMANTICS／VALIDATOR_NOT_SUPPORTED，无快照；仅dependencies非空时DEPENDENCIES_NOT_SUPPORTED。不给日志初始化假快照；此例不以现有解析器“通过”作为预期 |
| 两端均暂停写出；依次发3条INFO，再发1条INFO，再发1条WARNING | 前3条两端入队占用3；第4条INFO两端QUEUE_FULL，各计一次丢弃；WARNING占用预留后两端为4，ENQUEUED；再发ERROR则两端QUEUE_FULL，仅尝试一次同ID应急摘要，两端丢弃各增1 |
| Q已满且stderr也阻塞／应急限流；再发CRITICAL | emit有界返回DROPPED；已知不可用／限流时emergency为UNAVAILABLE／SUPPRESSED，尚未知阻塞时可为SCHEDULED，随后健康标应急失败；不会同步等待stderr，不承诺该记录保留。P槽均被占时新请求ADMISSION_BUSY，无事件ID；低等级只占一个槽时WARNING仍可用第二槽 |
| file写出挂起超过20ms，console可写 | file FAULTED／IO_TIMEOUT，在途UNKNOWN、未开始积压SINK_UNAVAILABLE计丢弃；console继续写；新DEBUG目标文件DROPPED且console仍FILTERED。挂起未结束不新增无限探测工作；结束后还须确认完整记录边界、段序及保留状态一致并探测通过才恢复，旧记录不重发，累计计数保留 |
| 单次write只写前缀后报错；或格式化主记录失败 | 前者UNKNOWN／WRITE_FAILED，无重放；后者安全备用沿用同一ID、固定DIAGNOSTIC_FORMAT_FAILED且无异常正文，备用再失败为FORMAT_FAILED并走常量应急 |
| 部分写入失败：e1仅写出JSON前缀、无LF后报错；原I/O结束，文件重新可写，探测发现残缺尾行；随后emit一条INFO事件e2（合成，未执行） | e1仍为UNKNOWN；file保持FAULTED、last_reason=FILE_STATE_UNCONFIRMED，不追加e2或DELIVERY_RECOVERED，不自动截断／删除／重放／修复。e2的file目标DROPPED／SINK_UNAVAILABLE，console正常ENQUEUED并写出e2；既有文件字节不因探测改变。轮转后若段序冲突或关闭段超过K，即使尾行完整且文件可写也同样不得恢复 |
| B=8192，活动文件8191字节，下一条合法JSONL为512字节 | 先轮转后写入完整512字节记录；若已有两关闭段，则最旧段删除，最终仍两关闭段及一个活动文件。合成8191字节由若干合法完整行构成；活动恰8192也在下一条前轮转 |
| 清理旧段权限失败；或目录中出现非服务文件／符号链接 | 前者file FAULTED／RETENTION_FAILED，不继续增长，活动文件不删除；后者初始化RESOURCE_INVALID或运行资源故障，无删除外部文件／业务blob副作用 |
| 正常emit后flush，两端均完成；随后close再close，旧logger再emit | FlushReport两端FLUSHED；首次CloseReport DRAINED、cleanup_pending=false，借用流仍打开；重复close返回原报告，不重复输出；旧logger SERVICE_CLOSED |
| 独立实例io_timeout_ms=100、flush_timeout_ms=10；先投递两条INFO（e1／e2），t=0调用flush，文件e1刚开始写、e2排队；t=10仍如此，console已写出并刷新两条。t=11再发INFO e3；文件e1在t=20完整写出，e2在t=21、e3在t=22完整写出；t=23再次flush并于t=24完成两端刷新（时间均为毫秒；合成，未执行） | 首次报告console FLUSHED；file INCOMPLETE／DEADLINE_EXCEEDED，written=flushed=dropped=unknown=0、pending_queued=1、pending_in_flight=1；服务和两端仍READY，健康flush_deadline_exceeded增1，file队列／在途占用各1，无SHUTDOWN_DROPPED或UNKNOWN。e3按容量正常入队但不进入首次截点；t=22文件健康written_events=3、queued_events=in_flight_events=0，仅已确认刷新才计flushed_events，首次报告不变。第二次报告file FLUSHED，written=flushed=3，其余四类目标计数为0；不因首次等待超时重复计数、重发或关闭资源 |
| 独立实例io_timeout_ms=100；close时一端在途阻塞，另有一条未开始；到50ms仍未结束 | 总deadline内返回INCOMPLETE／DEADLINE_EXCEEDED（若截点前已有I/O故障则该故障优先），在途UNKNOWN、未开始SHUTDOWN_DROPPED；cleanup_pending=true，不释放尚占用路径；迟到写出可能发生，原报告不改为成功 |
| 已入队事件因故障明确丢弃后调用flush；两端禁用后emit | 前者受影响端INCOMPLETE／已知故障或DELIVERY_LOSS，不能仅因队列空就FLUSHED；后者两端DISABLED，无dropped，诊断关闭不提供或关闭任何审计／账本能力 |

这些例子未证明V57–V68整体通过：Web、SDK、完整审计及计量仍缺失；轮转只覆盖大小／份数子集。实际实现后须分别验证输入安全、并发、资源故障、时间边界和文件保留；文本审查不等于性能、持久化、崩溃恢复或业务验收。

<a id="runtime-diagnostics-decisions"></a>

#### 10.8.9 集中已批准决定与授权边界

| 已批准公开决定 | 批准内容 |
| --- | --- |
| L1 事件与安全入口 | 固定模板和字段白名单；自由文本／原始异常整项移除，关联ID由可信调用方确认；一次生成同事件ID与时间，输入隔离、安全首错及固定大写原因码 |
| L2 分端路由与最小输出 | C／S过滤公式及显式模块门槛；两端JSONL、stderr／split、大小轮转和关闭段份数保留、单写者目录边界 |
| L3 投递与生命周期保障 | 逐端回执，WARNING以上预留与有限应急；故障隔离、丢弃／UNKNOWN计数、总deadline关闭及迟到I/O边界；重复初始化拒绝、重复关闭返回首报告 |
| L4 配置接入与参数 | 固定快照装配、上表新增生产参数及具体默认建议已批准；承认并另行处理M15校验前置缺口，不以本契约批准替代配置补充契约或既有行为 |

上述四组及文件恢复一致性、独立flush等待期限修订均已批准，不要求批准某个线程数布局、队列库或锁实现。既有审计事务／权限要求无需重新审批；配置补充C1–C4已[另行批准](configuration.md#configuration-additional-validation-decisions)，本轮新增规格的[待定事项](#runtime-diagnostics-prerequisite-decisions)、后续能力和其他未定事项不因此自动获准。契约批准不等于编码、安装依赖、提交、推送或部署授权；当前目标、检查和停止点见[CURRENT_TASK](../work/CURRENT_TASK.md)。

<a id="transactional-audit-contract"></a>

### 10.9 同事务审计详细契约

**状态：契约已批准，待实现授权。** 本节是[持久化事务基础整体交付](persistence-and-transactions.md#persistence-foundation-contract)的审计正文，已批准决定集中在其[P4](persistence-and-transactions.md#persistence-foundation-decisions)行，包含本节固定失败协议，不新建独立批准表。既有§10.1、§10.5、§10.6和[模块所有权](../modules/logging.md#contract)继续有效；运行诊断已批准行为不改变。此范围只有同库追加审计与最小按操作读取，不含审计导出、Web、历史全文搜索、内容捕获、保留清理或Provider账本实现。

日志模块拥有审计记录Schema、事件准入、输入安全、追加及查询边界；各业务模块拥有本模块事件的业务含义、必要性、对象引用和合法变更。基础设施仅承载受限审计仓储，将其加入同一UoW；不由业务仓储直接写审计表，也不让日志模块获得业务表通用读写能力。实际表／索引名称与编码实现属于私有细节。

<a id="transactional-audit-record"></a>

#### 10.9.1 最小记录与必要事件

每条持久审计记录至少包含下列信息；大小与每操作行数由[统一配置定义](configuration.md#configuration-persistence-definitions)中的audit参数约束。

| 字段组 | 语义与来源 |
| --- | --- |
| schema_version、audit_id | 固定支持的审计格式版本和唯一记录ID；日志模块验证格式、生成ID，已提交后不变 |
| database_id、commit_id、operation_identity、event_slot | 库与提交关联由UoW提供，operation_identity沿用[操作身份协议](persistence-and-transactions.md#persistence-foundation-idempotency)，不再另定义幂等范围；event_slot为该操作内模块绑定的必要事件位置，不取调用方自由文本 |
| recorded_at | 注入UTC时钟生成的记录时间，不充当事务提交证据或严格顺序；按操作读取使用稳定event_slot次序 |
| owner_module、event_code、event_version | 由可信装配绑定的模块、允许事件种类和其语义版本；不是诊断日志level，不接受动态注册、通配或未知版本 |
| actor_kind、actor_ref、reason_code | 经可信编排核验的操作者类别（SYSTEM或OPERATOR）、不透明主体引用及固定理由码；系统执行也须明确来源，不能默认匿名；不是认证凭据或自由理由正文 |
| target_refs、change | 模块核验的有界目标ID／修订引用及版本化类型化变更摘要；没有旧对象时前修订可明确为空；不复制整行业务对象或任意kwargs |

同库约束须保证审计关联到本次有效回执，`operation_identity + event_slot`唯一，所有必要事件位置齐全；检查在COMMIT前完成。必要事件清单（模块、位置、事件种类／版本）作为日志模块拥有的受保护元数据随该事务保存，用于重新打开及读取时核验，不能只保留内存声明。不能仅凭“有至少一条审计”批准含多个必要事件的事务。回执自身的字段和结果恢复规则只在[事务正文](persistence-and-transactions.md#persistence-foundation-idempotency)维护；日志模块不生成第二份业务结果或计费真相。

每个受审计模块命令在受控端口登记其必要事件位置与Schema，随UoW收集为必须完成的集合；调用方不能以`audit=false`、空事件列表、日志等级或捕获错误将其取消。只有模块合法变更才能填充对应位置，每个位置恰好一次；重复追加、缺失、额外未知位置或跨模块冒填使UoW失败。提交前日志模块验证必要集合已完整写入，基础设施才允许结束事务。此结构不自行决定哪些未来业务需审计；后续模块按自己的契约登记，本阶段只由测试合成命令声明两条必要事件。

本阶段change只接收事件Schema明确列出的非秘密结构化字段，支持的值为精确bool、有限范围整数、受限枚举文本、不透明ID、修订和这些值的有界记录／序列；缺失／空值须由对应字段显式允许，不自动转换类型。只有合成测试事件使用source／target的旧值、新值和转移数量。没有任意message、原始异常、聊天／记忆全文、prompt／response、媒体二进制、凭据或路径字段；未来确需敏感历史正文时另定义内容Schema和权限，本最小Schema不冒充已支持该能力。

输入须在调用期间稳定，日志模块先验证精确载体、版本、字段和限制，再取得独立不可变所有权；未知字段拒绝，不能静默丢掉审计必需内容。超限整体失败，不截断、摘要替换或退到运行日志。拒绝自定义序列化钩子、循环容器和惰性输入；不通过repr、异常拼接或深拷贝钩子处理非法对象。时间／ID源或编码失败同样不能生成“内容不详”的成功审计。

<a id="transactional-audit-ports"></a>

#### 10.9.2 写入与最小受控读取

审计能力独立于现有运行诊断服务装配，使用同一日志模块所有权；诊断禁用、尚未初始化或已关闭不取消必需审计。审计句柄的有效期受其存储绑定控制，过期后须重新绑定已验证的新存储服务和配置快照；不能通过EmitReceipt、flush或文件sink完成审计。

| 语义端口 | 所需能力及行为 |
| --- | --- |
| bind_audit(snapshot, storage_binding) → BOUND(受限审计能力)或安全错误 | 可信装配绑定已就绪存储及原生快照，核验必要audit定义和值；只作内存绑定与公开快照查询，不创建数据库、文件或运行诊断服务，不自行resolve配置 |
| append_audit(uow, event) → STAGED或安全错误 | 对应模块的绑定写能力＋同库活动UoW；校验并在原事务内追加。STAGED只含稳定事件位置／审计ID，绝不表示已持久提交；无自开连接或独立commit，无后台诊断队列 |
| check_required_audits(uow) → COMPLETE或安全错误／只交给事务协调端口 | 核对§10.9.1的集合、唯一性和完整性；缺失／失败阻止提交。COMPLETE仍是事务内核验结果，不是COMMITTED；模块参加不要求模块知道审计物理表布局 |
| read_audit(operation_identity) → FOUND(records)／NOT_FOUND／安全错误 | 可信装配绑定的开发者审计读取能力，限定库与scope；仅按一次完整操作点查，在一个短读快照中读取其回执与审计。无通用SQL、任意文件、筛选表达式、全历史列表或跨操作分页 |

开发者身份由受信任管理／启动装配验证后授予读取能力，不能靠调用方传`role="developer"`或知道operation_key取得权限。此范围用明确测试能力验证授权边界，不实现登录、角色继承或完整权限引擎。业务恢复端口只返回原提交回执及必要业务引用，不附审计历史；普通模块点读和agent工具均无审计读取能力，也不能通过当前对象接口或错误响应间接取得历史。

read_audit有回执时返回该操作完整、有界、按event_slot稳定排序的深不可变记录；没有回执返回NOT_FOUND，存储／权限／完整性错误分别失败，不以空序列冒充不可读。回执存在但必需记录不齐返回AUDIT_INCONSISTENT并要求存储停止写入；没有回执的孤立审计也是完整性故障，不能作为成功操作展示。NOT_FOUND同样只为观察值，不授予重放权限。历史记录上限遵守[原结果读取规则](persistence-and-transactions.md#persistence-foundation-idempotency)，读等待、连接回收和结果确认采用[事务资源协议](persistence-and-transactions.md#persistence-foundation-lifecycle)，不另建超时默认。

本阶段无更新、删除、到期清理或导出端口，审计行随验证数据库保留；后续审计保留策略仍由统一配置定义且须保护恢复引用。追加约束不等于防篡改证明：没有签名、哈希链、外部见证或抵抗宿主管理员修改数据库的承诺。

<a id="transactional-audit-failure"></a>

#### 10.9.3 失败与诊断隔离

AuditError恰含code、operation、field、reason、cleanup_pending，字段只为state、capability、configuration、event、transaction、query；结果及嵌套字段深不可变，无输入内容、路径、SQL、异常或对象引用。operation固定为bind_audit、append_audit、check_required_audits、read_audit之一；没有私有函数名、任意事件码或“audit”通用字符串替代。BOUND／STAGED／COMPLETE／FOUND／NOT_FOUND不带error；安全失败为AuditErr(error)，不是业务COMMITTED／NOT_COMMITTED的别名。签名错误按语言规则拒绝，无法构造安全结果的非预期故障不伪装成功。

以下为**完整code→reason及端口映射**，不扩充现有LoggingError枚举，不透传底层异常或未知码：

| code | 全部允许reason、触发及field | 适用operation |
| --- | --- | --- |
| INVALID_INPUT | AUDIT_INPUT_INVALID：事件／查询载体、字段或事件版本非法，field为event或query；AUDIT_LIMIT_EXCEEDED：新事件大小／操作行数超限，field=event | 前者append_audit／read_audit；后者append_audit／check_required_audits |
| ACCESS_DENIED | AUDIT_ACCESS_DENIED：绑定能力、调用方、scope／库／UoW执行归属不符，field=capability | 全部四端口 |
| INVALID_STATE | AUDIT_STATE_INVALID：存储未就绪／故障／关闭、审计绑定过期或UoW非活动，field=state | 全部四端口 |
| CONFIGURATION_UNSUPPORTED | AUDIT_CONFIGURATION_UNSUPPORTED：快照非原生，或按[配置定义](configuration.md#configuration-persistence-definitions)核验audit必需定义、支持能力和值不通过，field=configuration | bind_audit |
| AUDIT_CONFLICT | AUDIT_EVENT_CONFLICT：重复事件位置、额外未知位置或与登记事件语义不符，field=event | append_audit／check_required_audits |
| AUDIT_INCOMPLETE | AUDIT_REQUIRED：必要事件未完整追加，field=transaction | check_required_audits |
| AUDIT_FAILED | AUDIT_WRITE_FAILED：追加或提交前核验的资源／时钟／ID／编码故障，field=transaction；AUDIT_READ_FAILED：受控读取的准入、锁等待、总期限或资源故障，field=query | 前者append_audit／check_required_audits；后者read_audit |
| INTEGRITY_FAILURE | AUDIT_INCONSISTENT：已有记录、回执或必要事件清单关联损坏，field=transaction（事务内）或query（读取） | append_audit／check_required_audits／read_audit |

已绑定端口先检查绑定生命周期、再检查调用方／UoW能力、再检查载体／Schema／限额、事件位置冲突，最后执行存储操作或完整性核验。bind_audit尚无现成生命周期，先核验storage_binding的精确能力，再检查其就绪状态，最后快照；伪造能力不能触发方法／比较钩子。权限失败不查库、不透露操作是否存在。缺必要事件在确认清单可正常读取后报告AUDIT_REQUIRED；无法读取清单只能AUDIT_WRITE_FAILED，不能猜测为空集合。

审计端对下层安全失败固定映射：能力／状态分别映射AUDIT_ACCESS_DENIED／AUDIT_STATE_INVALID；有效输入下存储完整性故障映射AUDIT_INCONSISTENT；其余准入／锁／时限／I/O／资源释放故障按当前端口映射AUDIT_WRITE_FAILED或AUDIT_READ_FAILED，保留真实cleanup_pending。事件校验和集合检查按上表直接产生原因，不借底层错误替换。没有更早故障的普通时钟／ID／编码异常使用AUDIT_WRITE_FAILED；不输出异常文本。只读审计失败不生成新的失败审计记录。

事务协调端口的映射也固定：check_required_audits的AUDIT_REQUIRED → PersistenceError(TRANSACTION_FAILED, execute, audit, AUDIT_REQUIRED, cleanup_pending)；其余必要追加／核验的AuditErr → 同结构的AUDIT_FAILED。已确认持久数据损坏的AUDIT_INCONSISTENT还须使存储FAULTED；不会因外层使用AUDIT_FAILED而降为可继续写入的普通业务拒绝。bind_audit／read_audit不属于受审计事务，不作此execute映射。

首个审计失败的code／reason／field在本次调用中保留；后续清理只更新cleanup_pending，不覆盖首错。事务结果及是否允许重试由[持久化证据协议](persistence-and-transactions.md#persistence-foundation-errors)决定：同一个AUDIT_FAILED在确认回滚后可对应NOT_COMMITTED，在回滚／迟到提交无法排除时必须UNCONFIRMED；不能只用AuditError推断全无。跨模块映射后保留的是外层已确定的首个映射原因，内部原始错误不作为嵌套负载返回。

组合例子（全部未执行）：无读取能力且查询身份也非法时，先ACCESS_DENIED／AUDIT_ACCESS_DENIED、operation=read_audit，不查库；有效写能力下，事件Schema非法且位置重复时，先INVALID_INPUT／AUDIT_INPUT_INVALID、operation=append_audit，不以重复位置掩盖输入错误；全部已写事件合法但缺一条必要事件、同时诊断关闭时，为AUDIT_INCOMPLETE／AUDIT_REQUIRED、operation=check_required_audits，外层映射TRANSACTION_FAILED／AUDIT_REQUIRED。审计失败加回滚／清理失败的结果提升例子唯一见[事务组合表](persistence-and-transactions.md#persistence-foundation-errors)。

必要审计验证、编码、大小检查或数据库写入失败均使受审计UoW不可提交，不提供内存缓存成功、事后补写、独立数据库或诊断文件兜底。失败事务不会留下“成功审计”或成功回执；无法证明回滚完成时保持结果未知，不能声称全无。失败尝试只能作为安全诊断观察，并不自动成为另一个已持久审计事件；未来需要独立安全事件时须单独定义事务。

运行诊断在事务外通过现有公开Logger记录允许的固定事件码及白名单内错误分类／计数摘要；不能把audit事件对象、change、完整回执、输入指纹或恢复句柄传给emit。统一日志的白名单、等级、队列、轮转和flush只控制诊断；诊断关闭或故障不改变审计准入、提交和查询，不能撤销已获COMMITTED证据的业务。反之，运行日志成功写出也不能替代缺失的审计事实。

审计故障不会通过同一失败路径递归审计自身；诊断也不可用时仍保留安全错误返回和存储健康观察，不承诺一定能落下另一条故障记录。审计记录不进入agent上下文、记忆学习、embedding或persona，开发者读取不触发记忆使用强化。Provider usage继续由Provider拥有，任何审计关联均不复制一份费用账本。

#### 10.9.4 验收与停止边界

本节与存储基础一起按[整体验收矩阵](persistence-and-transactions.md#persistence-foundation-acceptance)验证必要事件完整性、全有或全无、重复与重新打开、审计故障、诊断隔离和受控读取；例子全部未执行，不再复制一份验收表。参数及当前配置缺口见[配置唯一补充正文](configuration.md#configuration-persistence-validation-contract)。本轮仅完成已批准契约的文档定稿，不新增审计实现、测试或数据库，实现授权及停止点见[CURRENT_TASK](../work/CURRENT_TASK.md)。

<a id="runtime-web-observation-draft"></a>

### 10.10 接入与批次运行的最小Web诊断观察（已批准）

**状态：本节推荐的观察权限及结果绑定审计增量已获用户批准。** 属于[持久接入与批次运行整体契约](durable-ingress-and-batch-runtime.md)；§10.8双端服务、§10.9必要审计及其已批准错误协议保持。这里新增有界当前进程日志窗口及受控查询，不把§10.4所列完整历史／导出／实时订阅一次全部实现。参数只在[配置§11.14.3](configuration.md#runtime-observation-parameters)维护，权限及HTTP只在[整体§9](durable-ingress-and-batch-runtime.md#observation)维护。

#### 10.10.1 接入统一日志及输出所有权

新增日志模块拥有的`RuntimeLogWindow`，在可信装配时作为显式可选观察输出接入统一规范化路径。事件只生成一次event_id／timestamp，先完成§10.8的安全处理和有界编码，再以同一不可变安全记录分别交既有输出和窗口。旧console／file准入、FIFO、饱和、flush／close报告及EmitReceipt两端字段不变；没有装配窗口的旧服务行为不变。窗口状态从自己的健康端口观察，不把旧EmitReceipt改成持久业务回执。

窗口继承模块采集过滤；文件关闭／故障不阻止窗口接收已合法采集的安全事件，窗口满或查询慢也不阻止console／file。窗口保留最新有界事件，覆盖最旧者并计`evicted_events`；这是短诊断保留策略，不能用于业务梦境积压、审计、Provider结果交接或已接收输入。所有序号／计数饱和时标saturated，不回绕为零。

实现只能共享已隔离安全记录或有界复制，不能把调用方原event、异常对象或格式化回调保存给Web。查询不执行用户代码，不重新格式化原异常，也不从任意文件回填。窗口引入的索引、锁、复制和查询执行都有限；慢读者不长期钉住环形区或阻止写出。取得一页不可变记录后释放短锁，HTTP传输在锁外；无单客户端无界队列。

<a id="runtime-log-cursor-privacy"></a>

#### 10.10.2 最小公开读取、游标与缺口

| 端口 | 行为及结果 |
| --- | --- |
| bind_runtime_log_reader(observation_grant) | 仅可信管理装配签发，绑定实例、入口集合、是否可看实例级事件及diagnostics.observe；另有独立diagnostics.window_metadata授权位。无日志写、审计或文件能力 |
| query_runtime_logs(query) | 异步有界读取当前进程窗口；返回LogPage、LogGap或LogReadFailed，不触发业务恢复、flush或文件扫描 |
| read_window_health() | 无I/O、受限只读；按下表投影，不把内部全局健康对象交给部分入口观察者 |

query为精确记录：cursor可空、limit、可空UTC开始／结束时间、可空最低标准等级、有限模块／事件码集合、可空entry_id／run_id／request_id／attempt_id。仅允许既有安全ID／固定码精确匹配，无正则、子串全文、任意排序或表达式。一次过滤至多检查一个当前有界窗口；按窗口内部接收顺序升序，墙钟倒退不倒序。先核验授权，再按授权可见集合和查询过滤；无权entry过滤直接拒绝，不以空结果泄露其存在。

内部仍可使用全局generation、固定as_of_sequence和最后扫描位置实现有界分页；这些值默认仅在**不透明cursor**内使用。cursor须绑定签发代次、库外窗口身份、权限及过滤指纹、分页轮次水位和到期边界，防伪造／篡改／跨能力复用，并不得从令牌正文、长度或相邻令牌差值解出全局序号。仅对明文offset签名或base64编码不满足“不透明”；可用受控密封令牌或有界服务端映射，具体机制由实现者选择，不能无界保留会话状态。

两类观察者的**全部响应字段边界**如下；是否具有全局元信息权不能由scope大小推断。独立元信息权不扩大events的入口或正文权限，instance_diagnostics也不隐含此权。

| 响应 | 仅作用域观察者（含部分入口） | 额外获diagnostics.window_metadata者 |
| --- | --- | --- |
| LogPage | events、next_cursor、has_more、observed_at、coverage=CURRENT_PROCESS_WINDOW；事件投影移除内部全局位置，不返回generation／as_of_sequence／oldest_available_sequence、全局保留／覆盖计数 | 相同受控events及分页字段；另返回window_metadata={generation, as_of_sequence, oldest_available_sequence}，明确是全局窗口信息 |
| read_window_health | observed_at、availability、该能力自身query_pending、coverage；未获全局计数权的字段直接不存在，不填0或unknown占位计数 | 另有window_metadata={generation, oldest_sequence, latest_sequence, retained_events, capacity, evicted_events, query_occupancy, saturated} |
| LogGap | reason=CONTINUITY_UNCONFIRMED、observed_at、coverage、restart_cursor、lost_authorized_events=UNKNOWN；无全局缺口原因、序号范围、覆盖量、当前代次或未授权事件ID | 同时可有global_gap={reason=WINDOW_EVICTED/PROCESS_RESTARTED/CURSOR_EXPIRED, generation, available_range, lost_count或UNKNOWN}；global丢失数只在可证明时给出，不称为授权过滤集合的丢失数 |

两类LogPage的has_more都只表示固定分页水位内还有**授权且匹配过滤的记录**，不是“还有全局位置未扫描”。须在当前有界窗口内确认这一事实，不能用全局最新序号比较代替。空授权页可推进内部扫描位置，但只交回不透明next_cursor及has_more=false；它不因另一入口有大量事件而变true。新刷新从上次扫描截点续查，跨页允许覆盖，不保证连续历史导出。

旧cursor无法再证明原分页范围可读时须显式返回LogGap，不能静默跳到最新并声称无缺口。对仅作用域观察者，CONTINUITY_UNCONFIRMED只表示该读取能力无法确认连续性，不断言授权记录确实遗失；不得给出由全局覆盖差值计算的授权丢失数或未授权范围。可证明仅覆盖无权记录、授权分页仍完整时不报授权缺口；不能证明时保守返回上述未知连续性。首次无cursor只承诺当前窗口；scope／过滤与cursor不符为ACCESS_DENIED／INVALID_QUERY，无有效权限时不透露gap。重查须使用新的受控cursor，重新核验当前授权。

窗口不持久化；最小查询不回填已有文件sink。页面显示“仅本次进程窗口，历史文件查询未提供”，完整历史索引、SSE、导出、诊断正文捕获仍为后续能力。该权限方案隔离显式内容和元信息，不承诺消除共享窗口覆盖或响应时延的一切侧信道，也不把无权记录诱发的未知连续性解释为已知授权缺口。

LogReadFailed恰含code、operation=query_runtime_logs、field、reason、cleanup_pending；code／reason为ACCESS_DENIED（GRANT_INVALID/SCOPE_DENIED）、INVALID_QUERY（INVALID_SHAPE/FILTER_UNSUPPORTED/CURSOR_MISMATCH/LIMIT_EXCEEDED）、RESOURCE_BUSY（QUERY_CAPACITY）、TIMEOUT（QUERY_DEADLINE）、UNAVAILABLE（WINDOW_CLOSED/RESOURCE_FAILURE）。field限capability/query/state，错误无输入值、秘密、路径、异常或原cursor。关闭后拒绝新查询；超时只结束等待，仍工作的读取保留槽位直到实际结束。read_window_health权限在绑定及当前授权核验中执行，不能绕过此投影读取全局健康。

#### 10.10.3 脱敏、权限与必要事务审计

Web事件仅取§10.8的安全字段与固定message模板。entry／run等关联ID仍是非秘密内部ID；不暴露原始平台标识、用户名、IP／cookie／Authorization、聊天／prompt／response、完整来源、媒体内容或审计change。普通用户文本即使碰巧通过ID字符集也不得放入关联字段。浏览器渲染纯文本，控制字符转义，不能执行HTML／脚本。当前config_snapshot_id不在旧诊断白名单中仍保持不输出；批次配置关联由运行元信息视图提供，不为了展示版本扩大所有日志正文。

无entry_id的实例级事件仅对明确获得instance_diagnostics权限的观察者显示；不能把“字段缺失”解释成对所有入口用户公开。认证／过滤在受控查询层执行，Web不能取得全量窗口后在浏览器中删掉无权项。查看行为不形成学习输入、embedding、使用反馈或恢复依据；agent不获得本读取能力。敏感审计及Provider详情仍为独立能力，本文不新增它们的Web路由。

新增领域事务应登记以下**必需审计语义槽位**；字段继续使用§10.9安全Schema，不增加任意正文或自由理由。凡含事务分配的seq／epoch／配置身份等值，须使用下方[结果绑定审计增量](#runtime-derived-audit-draft)，不能声称现有完整预冻结事件接口已支持：

| 完整操作 | 必记事实（同一业务UoW） |
| --- | --- |
| 入口登记／接收 | 绑定内部入口／宿主，原事件内部ID、entry_seq、位置、接收模式epoch；不记录外部ID、原文、内容hash或正文 |
| 冻结／工作claim／候选交接 | batch／run、入口、目标／辅助数量和范围、配置快照引用、owner_generation、候选／交接内部引用；不复制材料 |
| 成功／普通失败／敏感拒学终结 | 明确终态、目标范围、结果数、尾部数／历史清空、独占引用变化计数、失败／突然失忆风险及时间范围；合成结果须带来源类型 |
| 模式切换／恢复检查点 | 前后模式／epoch、dream_run、转换操作、收尾／恢复固定原因、有效发布引用；不冒填persona正文 |
| 回流页 | 入口、原／新cursor、移交范围及条数、剩余状态；不附消息正文 |
| 配置初始化 | 配置域／revision、组合snapshot_id、有效指针、SYSTEM／OPERATOR内部身份和固定理由；不记录路径和配置内容 |

时间范围使用声明的有界整数UTC微秒，空值明确允许；内部修订／序列不得以字符串塞入正文。每个模块只有自己事件槽位的写权，跨模块终结至少核验缓存终结、运行工作和结果／引用参与者各自必要槽位。动态数量用有界摘要，不以超长目标列表使审计绕过上限；需要逐对象引用时由声明的有界序列承载，超过范围拒绝整次事务。必要清单随回执持久化，原键命中不追加审计。

候选暂存或claim有自己的本地事务审计，不等于最终成功审计；终结回滚不得留下一份“成功学习”的审计。观察查询不写业务审计；失败尝试只经事务外安全诊断，不以独立事后审计补偿缺失的必需事件。诊断窗口／文件不可用不撤销已提交结果；必要审计失败阻止业务提交、新模型登记仍按Provider规则停止。

<a id="runtime-derived-audit-draft"></a>

#### 10.10.4 结果绑定必要审计（已批准精确增量）

命令、指纹及新旧持久证据的完整协议唯一见[持久化增量](persistence-and-transactions.md#runtime-result-bound-audit)。日志模块继续拥有AuditRequirement、安全事件Schema、必要清单、审计ID／时间及读取权；接收seq／路由由缓存／运行所有者负责，配置revision／snapshot_id由配置所有者负责，不由日志推测或生成。

只给事务协调能力的check_required_audits增加显式关键字frozen_result：旧命令保持原无参数调用及检查行为；显式结果绑定命令必须在handler完成、结果Schema及字节限额通过后提供同一个原生深不可变结果。日志先按已冻结意图和静态路径映射物化每个完整事件，再通过绑定追加能力写入，并检查必要清单；返回COMPLETE仍不等于提交。协调者随后不得替换结果或改动参与仓储，回执保存的必须是该结果。模块调用者无此物化权；提前向结果绑定slot调用append_audit或替换事件为AUDIT_EVENT_CONFLICT并毒化UoW，不能成为任意动态审计入口。

新增关键字的精确载体不符映射INVALID_INPUT／AUDIT_INPUT_INVALID、operation=check_required_audits、field=event（仅这一新增调用形态扩展该原因的适用范围）；非协调能力先AUDIT_ACCESS_DENIED。绑定字段须在可信静态装配时全部可类型校验；运行时映射缺值／不符也为该AUDIT_INPUT_INVALID，不用空值补齐。物化后事件仍经原append_audit校验与限额；缺slot为AUDIT_REQUIRED，重复／额外slot为AUDIT_EVENT_CONFLICT，时钟／ID／写入／编码故障为AUDIT_WRITE_FAILED，持久证据不符为AUDIT_INCONSISTENT。各结果按§10.9.3既有事务映射与首错规则处理，不削弱回滚不确定性提升。

日志拥有的新结果绑定证据封套与必要清单、审计行同事务写入；其字段和版本只在持久化补充维护。重开、回执确认和审计读取必须按原回执结果重建并逐值比对完整事件，不能仅验证Schema和“有一条审计”。read_audit仍只返回经授权的一次操作记录，不暴露封套中的指纹／意图或授予Web／agent历史权。现有预冻结完整事件逐值检查和旧记录格式保持不变。

新增验收场景（对应版本的实际覆盖和未执行变体见[CURRENT_TASK](../work/CURRENT_TASK.md)）：

| 触发 | 断言 |
| --- | --- |
| 两条命令先冻结相同规则／不同事件身份，之后按两种顺序接收；中间切模式 | 每次已提交业务行、原回执与审计中的seq／位置／epoch一致；原事件指纹不受竞争改变，审计槽位各一次 |
| handler内试填派生slot并捕获拒绝，或物化时超限／写入失败 | 捕获不能恢复UoW可提交性；无成功业务／审计／回执，回滚不明则未知 |
| 配置初始化事务生成身份后提交但任何返回前退出 | 原意图能重建恢复输入，查回相同revision／snapshot及对应审计；不预读或重造身份 |
| 修改新证据的意图、映射身份、派生序列或原回执结果之一 | 重开及各受控确认／读取报完整性故障；不按当前行重算出“成功” |
| 部分入口观察者查询；仅改变其他入口记录且授权可用范围不变 | events／has_more及可见健康一致，无全局计数／水位／位置；opaque cursor不暴露差值，过滤无匹配即has_more=false |
| 同一过滤分别使用两类观察能力，覆盖授权记录／重启／游标失效 | 部分入口仅得到未知连续性和受控重查游标；额外权限才见全局原因／范围；全局丢失数不伪装成授权丢失数 |
| 无权entry过滤且旧cursor也失效，或拿别的scope cursor重用 | 先权限拒绝，不以LogGap／空页泄露对象、全局覆盖或有效游标信息 |

整体实现须以[统一矩阵](durable-ingress-and-batch-runtime.md#acceptance)覆盖旧双端及旧审计兼容、第三观察输出隔离、元信息授权、有界性和新审计关联完整性。实际执行证据及未执行变体见[CURRENT_TASK](../work/CURRENT_TASK.md)。

<a id="memory-history-audit"></a>

### 10.11 正式对象修订历史与必要审计补充（推荐已批准）

本节仅为[正式记忆／来源／媒体集中契约](formal-memory-source-media.md)补齐修订历史及新操作必要审计，推荐方案已随主契约获用户批准。旧AuditRequirement.change仍递归禁止BoundedTextSchema；旧审计输入、指纹、结果绑定证据和读取格式不改。不能把“旧正文仅供开发者审计”解释成现有审计已经支持正文。

推荐在logging_service所有权下新增专属不可变`ObjectHistoryRecord`，同库受限仓储、同一原对象变更UoW追加。它不是运行日志、第二份memory当前值或通用内容捕获：只接受固定类型MEMORY／RELATION的旧当前快照及原关联，或SUBJECT旧标签／身份记录；不接聊天窗口、模型请求／响应、媒体二进制、凭据或任意附加数据。创建对象不产生虚构“旧正文”。对实际正文／关系／分数修订和删除，必要历史记录必须存在；没有语义变化不产生历史版本。

记录恰含`history_version=1, history_id, object_id, object_kind, previous_revision, resulting_revision, action=REPLACE/SCORE_CHANGE/DELETE/SUBJECT_CHANGE, previous_value, previous_links, recorded_at_us, operation_identity, commit_id`。previous_value是对应对象的完整旧Schema，previous_links为旧source／依据ID、目标／辅助锚点的有界集合；前后修订及所属对象必须与memory参与者实际读到的旧值一致。旧值≤4096、旧关联合计≤2048，历史外层固定身份／键／整数／时间开销≤2048，嵌套按结构编码而非再次转成字符串，所以每件≤8192、件数≤8；这保证合法当前对象能完整归档。超限在任何对象变更前拒绝整组，不能丢部分历史或把全文截成摘要。媒体指针只作历史解释，不建立业务blob保护。

新增`append_object_history(uow, approved_record)`只交日志专属写参与者，memory通过类型化旧值交接提出必要记录，不能直接操作日志表。新增`read_object_history(operation_identity, history_id)`只给独立开发者history.inspect能力，限定库／scope／对象范围，按一次操作明确ID点读一个有界记录；无历史列表、全文搜索、导出、文件读取、agent工具或Web路由。普通`read_audit`仍返回安全摘要及不透明历史ID，不能因有ID就自动返回正文。旧接口无正文规则保持。

每条历史通过唯一`operation_identity + object_id + previous_revision`关联该次变更的必要slot；历史ID／旧值摘要／对应修订写入日志私有证据（不是通用业务回执），与历史正文、摘要、原回执同事务提交。新历史关联格式为独立`history_evidence_version=1`，不改变旧materialization_version=1解释。提交前和每次回执／审计确认须核验：本次必要历史集合恰好完整、与实际前修订／动作一致、历史内容Schema及摘要正确、commit／库／操作关联一致。重开同样全检；缺件或篡改为完整性故障，不能只凭一行摘要返回原成功。

当前正文删除或修改后记忆服务只留当前值／墓碑；历史读取不向记忆agent、检索、embedding、学习、persona或Provider结果所有者开放，不得从历史“恢复对象”。内部合法来源可以含相似内容，仍与日志历史访问分离。历史正文和必要证据随验证库保留，本次无删除／过期／压缩接口；未来保留政策须另定恢复与审计完整性，不自动将验证库保留解释为生产永久保留。

新必要审计slot按操作固定声明，结果绑定只包含安全ID、revision、状态和有界计数，不复制旧runtime的整份change到每个owner：

| 操作 | 必需语义及所属slot |
| --- | --- |
| 上传意图／READY／放弃／GC | media的原操作、blob内部ID／generation、状态、长度、出现／引用变动计数；无内容hash、路径或原件 |
| 接收含媒体 | ingress原接收身份事实、buffers序列／位置、media出现／引用事实；同T01、无事后补绑定 |
| 理解准备／结果持久／复用 | runtime的实际准备登记／领取／停放／失效／冻结转交，以及ingress／media的实际保护变化，按固定命令选择；media记录逐出现工作／解释／request／原结果引用、状态、origin及授权scope内部ID、是否复用。外部REFUSED标EXTERNAL_REPORT且guard_created=false；已核验Provider敏感终态的保护创建标PROVIDER_CONFIRMED及原终态证据引用；复用保护不造新拒绝／费用。Provider计量仍只归原Providerslot |
| 候选保存 | cognition的候选清单／叶数／拟议终态、runtime工作身份、必要保护owner的计数；不是正式发布 |
| 终结 | ingress消费／原文释放、buffers目标／历史、runtime终态、cognition候选处置、memory对象／来源／待办、media引用转交；修改删除含logging_service历史关联，最多8slot |
| 释放计划保存／替换 | 计划所属memory或runtime记录root／plan_ordinal／计划身份、固定分支、预期效果计数及前计划已确认未提交的关联；这是实际计划变更，不记对象已删除／批次已终结。计划保存与业务提交分别有原回执 |
| 独立对象修改／删除 | memory当前前后revision／状态、source holder／退役及待办计数、logging_service必要旧值；最后source释放还包括ingress的引用释放／payload删除或保留计数，及media的实际OBJECT／SOURCE／EVENT／理解引用变化。buffer没有队列变化时不得加slot |
| source最后释放／本地恢复 | memory来源处置、ingress原文寿命引用／payload处置、media出现／解释／blob引用的真实变更及原操作身份；理解保留计数与blob文件删除区分。仅恢复已提交结果不重复这些事件；纯只读确认不新增业务成功审计 |

固定分支、事务内重查及竞争后如何选新执行键只在[持久化分支协议](persistence-and-transactions.md#release-command-branches)维护。审计完整性须核验root、plan及实际owner变更向量匹配：无媒体的最后source释放不能漏ingress；有其他H保护而payload未删除仍须记录实际SOURCE引用减少；没有memory对象变更的准备处置不得写对象成功slot。OWNERSHIP_CHANGED的业务事务全无，不追加对象历史或虚假失败后的成功审计；已提交的独立计划审计仍存在且标其本来含义。未知执行计划不能由后来一条审计“说明未提交”代替原键确认。

理解超限的必要摘要仅记录media原工作／request、固定RESULT_LIMIT_EXCEEDED／RESULT_INVALID、最终FAILED和原结果来源；不保存超限文本、长度之外的自由错误或响应。已核验敏感终态只能记录固定REFUSED及保护事实，不能因输出超限改为普通失败。保底记录／保护／选择／必要摘要同UoW；写失败时全无，确认未知保留原工作，不产生一条假RESULT_STORED审计。

每slot实际摘要格式≤4096字节，新audit.event_max_bytes推荐8192只是外层上限，不能据此产生8份最大8192再放进一个读取响应。必要摘要最多8×4096＋8192完整操作封套=40960≤底层65536；历史件逐件点读≤6×8192＋8192=57344。必要证据总编码≤32768，历史正文完整值及关联再受每件8192／命令总量核验。带正文的slot元信息仅含历史ID／修订，不在安全change保存摘要hash。新增固定格式超出预留是实现不符，须调整方案再审，不能隐式截断。

新独立HistoryAuditError仍为五字段安全封套，operation仅append_object_history／read_object_history／check_object_history，field限capability/state/record/transaction/query；code／reason完整集合为INVALID_INPUT(HISTORY_INPUT_INVALID、HISTORY_LIMIT_EXCEEDED)、ACCESS_DENIED(HISTORY_ACCESS_DENIED)、INVALID_STATE(HISTORY_STATE_INVALID)、HISTORY_CONFLICT(HISTORY_EVENT_CONFLICT)、HISTORY_INCOMPLETE(HISTORY_REQUIRED)、HISTORY_FAILED(HISTORY_WRITE_FAILED、HISTORY_READ_FAILED)、INTEGRITY_FAILURE(HISTORY_INCONSISTENT)。先绑定／权限、后生命周期、载体／版本／限额、必要位置、资源；查询权限先于是否存在。只读失败不写失败审计，文本／底层异常不回显。

必要历史追加／校验失败按原事务协调映射为TRANSACTION_FAILED／AUDIT_FAILED（缺必要件为AUDIT_REQUIRED），完整性损坏另使存储FAULTED；保持首错及cleanup_pending，回滚不明仍UNCONFIRMED。历史成功STAGED不是COMMITTED，诊断关闭／错误不改变必要历史；COMMITTED后日志诊断失败不能撤销对象变更。

运行诊断继续使用原Logger白名单，推荐只复用已允许的模块事件及固定安全错误分类，不增加正文捕获、任意reason或新日志等级；旧白名单放不下的详细状态由memory／media安全观察端口表达。运行观察仍不具有历史正文权。验证包括旧摘要文本拒绝、新历史全有／全无及篡改核验、删除后权限隔离、仅审计不保护媒体、重复命令不重复历史；统一纳入[整体矩阵](formal-memory-source-media.md#acceptance)，验证结果见CURRENT_TASK。
