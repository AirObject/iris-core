# 日志等级、输出、审计隔离与背压

> 本文件是权威原文的阅读视图，不是独立需求。原文仍是权威依据；后续修改规则时，先更新对应原文，再同步受影响的视图与相对链接；[覆盖映射](../work/ORGANIZATION_REPORT.md#coverage)仅作整理历史保留，不要求持续更新。保留原文“已确定、建议、示例、待确认”的性质；下列导读不新增决策。正文含原有编号，仅限文档追踪。

适用主题与局部定义：sink是日志输出端。运行诊断可以有界丢弃，必须的业务审计随业务事务提交；模型计量是Provider自己的持久化账本。

来源：[原文 L735–L814](../../companion_memory_module_design_provider_logging_config.md#section-10)。行号对应整理时的哈希基线。

按关联工作联合阅读：[日志模块契约](../modules/logging.md)；[审计事务](persistence-and-transactions.md#t13)；[热配置协议](configuration.md#source-line-895)；[产品故障与权限](../product/operations-and-management.md#section-17)；[参考标记](references.md)。

返回[文档总入口](../INDEX.md)；实际进度见[工作状态](../work/STATUS.md)。

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

审计不走可丢的诊断队列：受审计业务操作须把审计行与其变更同事务提交。审计/Provider账本存储失败时，对应写入或新付费调用按系统故障处理；单纯控制台或运行日志文件失败则可降级，不撤销已提交认知。

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
