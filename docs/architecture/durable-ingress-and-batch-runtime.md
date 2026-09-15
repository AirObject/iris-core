# 持久接入、三段批次、专注门控与恢复及最小只读观察

**状态：完整技术契约已获用户批准。** 本文及链接的配置、日志、持久化、产品补充共同定义一次可独立验收的能力范围。既有批准保证保持有效；下面的“须／不得”表达已批准实现的验收约束。集中批准决定见[§12](#decisions)，执行授权及停止点见[CURRENT_TASK](../work/CURRENT_TASK.md)。

本契约定义基础运行装配及模拟／合成参与者边界；后续正式记忆、文本学习等扩展不改变该装配的兼容性要求。实际实现、验收与提交版本见[STATUS](../work/STATUS.md)。

<a id="scope"></a>

## 1. 整体能力与唯一正文

整体交付是：身份绑定的原始输入取得持久回执，按入口进入正常或专注暂存序列；正常路径按固定平台配置冻结三段，由每入口唯一执行者取得受控结果，以三种业务终态原子轮转；模式切换、发送竞争、本地确认及跨进程恢复不会重复模型调用或消费；最小Web展示模式、积压、缺口和脱敏诊断。实际运行接入、缓存、模式、配置持久身份、存储、审计、Provider服务和只读查询，模型侧继续仅用模拟适配器；认知等衔接用[受控参与者](#participants)验证。

| 唯一规则位置 | 本文使用的边界 |
| --- | --- |
| [模块所有权§5](ownership.md#section-05)、[实施顺序§15](implementation-options.md#section-15) | 不新建万能管理模块；本范围是接入／运行／缓存及最小观察的连续实施范围 |
| [输入与引用](../product/input-and-media.md)、[三段及三终态](../product/batches-and-learning.md)、[来源§8](../product/provenance-and-memory.md#section-08) | 产品保证不被工程简化；本文细化结构、端口和并发裁决 |
| [梦境门控与回流](../product/dream.md)、[本次产品选择](../product/decisions-and-delivery.md#ingress-runtime-product-options) | 已批准关闭短批次／空闲尾部，并采用指定收尾与开放点；额外异常管理操作未批准 |
| [持久化§4／§7](persistence-and-transactions.md#section-04)、[基础端口§8](persistence-and-transactions.md#persistence-foundation-contract)、[本次装配补充](persistence-and-transactions.md#ingress-runtime-storage-draft) | 原提交确认与未知恢复不变；新格式／参与仓储在所属正文维护 |
| [配置补充§11.14](configuration.md#ingress-runtime-configuration-draft) | 参数、作用域、统一校验、持久版本和快照唯一维护于配置模块 |
| [Provider§9](provider.md#provider-foundation-contract) | 请求归属、原子发送许可、账本及交接、UNKNOWN和清理占用复用现有协议 |
| [日志补充§10.10](logging.md#runtime-web-observation-draft) | 最小Web诊断查询、有界性、审计事件安全和诊断隔离唯一维护于日志模块 |

不包含真实供应商、媒体上传／解码／GC、正式记忆／来源服务、结构化认知agent、召回／反馈／状态／目标、persona或梦境整理算法、非专注整理的一致性方案、完整热激活、生产路径／G2、生产鉴权、存量迁移、部署和备份产品。缺少参与者时相应能力返回`CAPABILITY_UNAVAILABLE`，不能把空实现装成学习成功或梦境完成。已有的产品能力要求不因此取消。

<a id="ports"></a>

## 2. 所有权、公开端口与结果

应用编排层拥有完整用例的事务责任；领域端口只使用其静态装配的仓储／操作能力，无裸数据库、任意SQL、路径读取或其他模块写权限。以下名称是已批准的语义签名，实现可作符合语言习惯的命名调整。所有I/O均异步有界，返回深不可变值；等待结束与底层所有者结束分开。

| 持有者／端口 | 输入、输出、副作用与重复边界 |
| --- | --- |
| 可信启动：`initialize_runtime(bootstrap, bindings, resources)` | 按§8分层核验存储、配置、Provider及运行状态；全部完成才READY。未完成返回带stage的RECOVERY_PENDING或安全失败，stage不表示可续进度；不自动建库或把存储未就绪当运行检查点 |
| 接入装配：`register_entry(operation_key, registration)` | 绑定实例、宿主、平台及原入口标识，持久创建入口及初始游标；同键返回原注册回执，冲突不重绑；仅可信初始化能力可用，无Web注册／删除页面 |
| 可信授权方：`bind_ingress(principal, entry_grant)` | 生成签发者、实例、宿主、平台、具体入口和允许操作绑定的原生句柄；只交付已核验授权，不凭请求角色字符串发权 |
| 接入句柄：`accept_event(event)`、`lookup_acceptance(event_identity)` | 前者按§3持久接收；后者只读原回执元信息。未命中不构成重新执行许可；无全文、媒体字节或其他入口枚举 |
| 受限恢复：`resolve_acceptance(event_identity, original_event)` | 用原绑定身份、事件及审计意图重建结果绑定命令，按§3.4的已批准指纹增量调用受控结果确认；无新序列、队列移动或模型调用，不依赖曾收到恢复句柄 |
| 运行调度：`request_learning(trigger)`、`claim_work(work_id, expected_revision)` | 触发幂等登记及公平候选调度；按§4冻结或返回`NO_TARGET`、`NOT_READY`、`BLOCKED`；claim产生有持久代次的封闭工作能力，同入口不得并发签发两个目标执行者 |
| 缓存内部：`get_batch_snapshot(batch_id)` | 仅向该批次工作／结果所有者返回冻结清单和按清单读取的原始材料；读取不扩展来源、不消费；Web无此句柄 |
| 批次编排：`stage_candidate(work, outcome)`、`finalize_batch(work, candidate_ref)` | 保存受验证的可恢复结果，再按§5一次本地提交；业务成功只由提交回执确认。候选引用不是成功，重复终结不重复轮转 |
| 运行控制：`enter_focus(key, expected_epoch)`、`finish_focus(key, completion_ref, expected_epoch)` | 仅绑定的内部梦境协调能力；请求模式事务及发布证据核验。普通输入、Web观察者和自由`dream_run_id`无此权；同键同内容返回原结果 |
| 缓存回流：`transfer_dream_page(entry_work, expected_cursor)` | 读取有界最旧页并原子移交；返回移交范围、游标和当前积压状态；无重编号、无模型调用，重复页只移交一次 |
| 本地恢复：`recover_runtime(recovery_binding)` | §8的确认、重建和幂等本地收尾；不包含普通学习启动、媒体理解或梦境模型调用 |
| 观察能力：`read_runtime_view(query)`、`read_entry_status(query)` | 返回§9受限元信息；无副作用，不因为读而恢复数据库或解除故障 |
| 可信启动：`get_health()`、`close()` | 无存储I/O健康观察；关闭停止新工作并有界等待，返回`CLOSED`或`INCOMPLETE(cleanup_pending)`；借用服务按装配所有权关闭 |

新增领域写端口统一封套：`Committed(receipt, source)`、`NotCommitted(error)`、`Rejected(error)`、`Unconfirmed(reference, error)`。其证据严格沿用[持久化结果协议](persistence-and-transactions.md#persistence-foundation-idempotency)，不得仅凭异常名称映射成未提交。`source=NEW/EXISTING`不改原回执。只读返回`Found(value)`、`NotFound`或`Failed(error)`；调度的`NO_TARGET/NOT_READY/BLOCKED`是明确无新批次的结果，既不等于失败终态，也不假称成功学习。

领域安全错误固定为`code、operation、field、reason、cleanup_pending`；不透传下层错误对象、正文、外部原始ID、路径、SQL、异常栈或恢复能力。`field`限`identity/event/entry/trigger/batch/work/mode/configuration/storage/participant/query/state`；operation为上表当前公开操作。code／reason集合如下，新的原因不能临时放入任意message：

| code | reason及使用边界 |
| --- | --- |
| INVALID_INPUT | INVALID_SHAPE、INVALID_IDENTIFIER、INVALID_TIME、LIMIT_EXCEEDED、UNSUPPORTED_VERSION；用于有输入端口 |
| ACCESS_DENIED | BINDING_MISMATCH、OPERATION_NOT_GRANTED；所有受限端口，先于对象存在性查询 |
| IDEMPOTENCY_CONFLICT | CONTENT_MISMATCH；注册、接收、触发、候选、终结、模式和移交写端口 |
| PRECONDITION_FAILED | REVISION_CHANGED、WORK_FENCED、TARGET_CHANGED、TRANSFER_CURSOR_CHANGED、PUBLICATION_MISSING；不静默换键重试 |
| MODE_BLOCKED | DREAMING、RECOVERING、RUNTIME_FAULTED；缓存原始接收按§6的独立例外处理 |
| CAPABILITY_UNAVAILABLE | MEDIA_NOT_SUPPORTED、LEARNING_PARTICIPANT_MISSING、DREAM_PARTICIPANT_MISSING、BUSINESS_NOT_IMPLEMENTED；不返回空业务成功 |
| RESOURCE_BUSY | ADMISSION_FULL、OWNER_ACTIVE、LOCK_BUSY；无无界等待队列 |
| CONFIGURATION_UNSUPPORTED | CONFIGURATION_REQUIRED、CONFIGURATION_MISMATCH、CONFIGURATION_UNRECOVERABLE、CONTEXT_LIMIT；不从本地常量补默认 |
| STORAGE_FAILED | WRITE_NOT_COMMITTED、COMMIT_UNCONFIRMED、READ_FAILED、FORMAT_UNSUPPORTED、INTEGRITY_FAILURE；前两项仍须配真实证据封套，后两项阻止受影响能力就绪 |
| PARTICIPANT_FAILED | RESULT_INVALID、PARTICIPANT_UNAVAILABLE、PUBLICATION_INVALID；本地系统故障，不自动等价Provider普通失败 |
| TIMEOUT／INVALID_STATE | DEADLINE_EXCEEDED；NOT_READY、SERVICE_CLOSED、SERVICE_FAULTED；有界等待及生命周期结果 |

先核验原生能力／当前授权，再核验生命周期可用操作、载体安全和格式硬上限，再查幂等原事实，最后核验新工作的模式／修订／当前限额。这样既不泄露无权对象，也不把当前模式改变或限额降低变成旧回执失效。恢复和故障模式下仍可返回可可靠读取的原接收确认；存储无法读取则返回失败／未知，不制造未命中。首个已确定原因保留；后续确认不充分提升到Unconfirmed，清理只能更新占用事实。未实现业务端口在NORMAL返回BUSINESS_NOT_IMPLEMENTED，在专注闭门状态优先明确DREAMING；均不排队代执行。

<a id="ingress"></a>

## 3. 身份、原始事件、接收序列与回执

### 3.1 绑定及幂等身份

入口登记唯一约束为`instance_id + host_id + platform_id + external_entry_id`，登记后产生稳定内部`entry_id`；同一平台不同入口不共享游标。`external_entry_id`为受限原始文本，由登记适配器编码保存，不能直接冒充内部ID。宿主身份由可信认证边界绑定；sender是发言主体，不是调用主体。相同昵称、正文中的身份自称、引用作者或请求传入的入口ID都不能改变授权。初次登记及重启重绑不合并主体；跨入口正式认知关系仍归记忆模块。

接收业务身份为`instance_id + host_id + entry_id + identity_kind + identity_value`。有外部事件ID时，kind为`EXTERNAL_EVENT`，value为平台稳定事件ID；没有时kind为`CLIENT_EVENT`，必须提供接入方在第一次发送前生成并持有的`client_event_key`。同一实际发生记录的所有重投须复用该键；有意再次发生即使用新键。缺两者拒绝，服务不以正文hash、当前时间或每次生成新键“补齐”。两者同时提供时拒绝歧义；外部ID恢复后不得给同一事件换身份域重新提交。

业务身份经版本化确定性编码映射到持久化`operation_key`和内部`message_id`，使用抗碰撞摘要仅压缩**身份**，不合并相同内容；原身份比较字段保存在接入仓储内。数据库唯一键和原身份复核共同防碰撞／跨域混淆。映射版本随记录保存；同格式升级不改变旧键解释，不以摘要替代实际配置版本。接入凭据轮换但宿主绑定不变时，原事件身份不变；真正撤权后不再开放原回执查询。

同键指纹由服务从隔离后的完整事件计算，覆盖影响语义的全部客户端字段和入口绑定，类型敏感、缺失与null不同、不修剪正文、不Unicode归一化。排除重投时间、deadline、网络追踪值、系统接收时间、生成序列和路由模式；它们不是客户端的事件修订。外部对已接收事件“纠错”须另用尚未定义的显式修订契约，本接口同键异内容返回CONTENT_MISMATCH，不能覆盖原始发生记录。不同ID同正文仍分配不同message_id／序列。

### 3.2 原始事件格式

输入只接受版本化精确记录及原生有限容器；未知字段、重复JSON键、非有限数字、自定义对象／钩子、非法UTF-8、循环或超深结构整体拒绝。传输解码先受请求字节限额约束。字段完整性和量限由[配置规格](configuration.md#runtime-envelope-parameters)约束，不接受任意bytes、URL抓取或文件路径。

| 字段 | 类型、来源及可空规则 |
| --- | --- |
| event_version | 支持的协议整数；未知版本拒绝，不能作为配置版本 |
| external_event_id／client_event_key | 恰有一个非空有界字符串；前者是外部标识，后者按内部安全ID规则；身份选择见上文 |
| event_kind | MESSAGE、PERCEPTION、SELF_OUTPUT、ACTION_RESULT；管理命令不在此集合 |
| sender | 平台主体原始标识、显示名可空、场景角色枚举；允许UNKNOWN角色但不据此猜主体；保留宿主报告的身份来源 |
| occurred_at | 带UTC偏移的时间或显式null；原偏移和原精度保留，缺失标UNKNOWN；不能由接收／回流时间冒填 |
| body | 原始正文字符串，可为空；与附件／感知数据不能全部为空；控制字符按数据保存，展示时转义 |
| quotation | 有界引用列表，每项含原文、可空原作者／原事件标识／发生时间；这些是报告内容，不授予读取另一入口原事件的权利 |
| media | 有界引用列表：受授权的READY对象绑定、出现记录ID、模态、外部已给理解及其来源／状态；不含路径或任意可抓取URL。可为空；实际媒体支持界限见§10 |
| correlation | 可空关联ID；自身输出／行动区分INTENDED、PREPARED、EMITTED、RESULT，保留行动关联，不能把拟发内容当已实际发出 |
| extensions | 按已登记场景格式的有界精确记录；无匹配格式只允许空记录，不接受任意字典作为未来管理旁路 |
| 系统写入字段 | instance／host／platform／entry绑定、message_id、entry_seq、received_at_utc、payload_format／digest；不能由请求覆写 |

内部ID统一满足现有Provider安全ID格式；外部原始标识和显示内容另用有界文本，不把它们传入审计ID／日志上下文。正文自称拒学、管理员或目标完成只作为原始材料；所有业务操作仍须独立端口与权限。

### 3.3 接收提交与顺序

同入口`entry_seq`为持久、严格递增正整数，由接收事务独占分配；排序只用它，不用事件时间、锁外到达时刻或客户端序号。并发输入以成功写事务顺序裁决，未提交不占用可见序列；不要求客户端观察无间隙。计数达到格式上限时如实拒绝新接收并报故障，不回绕。不同入口无全局业务顺序承诺。

接收短事务同时完成：原键冲突／重复判定、分配序列、原始不可变事件保存、正常或专注待回流位置、连续的READY媒体引用、入口计数／唤醒待办、必要审计和原接收回执。模式／回流状态在同一事务重查；切换竞争结果按该事务线性顺序决定。重复命中不重新取时间、路由、序列或重新唤醒学习。唤醒丢失由持久待办恢复，不能靠内存queue证明接收成功。

回执的稳定字段为`receipt_version、acceptance_id、message_id、entry_id、entry_seq、received_at_utc、accepted_placement、mode_at_accept、mode_epoch_at_accept、learning_state=NOT_LEARNED`及通用提交关联。accepted_placement区分NORMAL_PENDING、FOCUS_STAGED、DRAIN_STAGED，只表明**当时**接收去向；之后重复投递仍返回原回执，不用当前已学习状态重写它。最新处理状态经独立受控观察查询。回执不含正文、外部原始ID、附件字节或“已记住”承诺。

只有COMMITTED才能确认已接收。新接收的物理存储失败返回失败／未知，保留调用方用原键确认的途径；不在内存排队后返回成功。无条数上限的专注序列仍受每请求有界和实际磁盘可用性约束；无隐式旧输入淘汰、缩减正文或容量满后自动调用模型。

<a id="acceptance-audit-binding"></a>

### 3.4 命令、事务事实和审计的衔接（已批准增量）

现有持久化命令要求execute前完整冻结audit_events，事务内追加又逐值核对；本节不能直接把事务生成值写进该旧载体。推荐采用[结果绑定命令的精确增量](persistence-and-transactions.md#runtime-result-bound-audit)：接收命令冻结原事件、完整绑定、稳定message_id／操作键、规则版本及actor／理由意图；必要审计的全部派生路径也预先固定。生成seq、received_at、placement、mode／epoch属于首次事务结果，只从同一次仓储变更产生，并随原回执和[必要审计](logging.md#runtime-derived-audit-draft)一起提交。该增量已批准，执行证据见CURRENT_TASK；不删掉审计字段或放宽旧指纹来换取可执行性。

下列关系及恢复步骤均为已批准验收预期；实际执行覆盖和未执行变体见[当前验证记录](../work/CURRENT_TASK.md)：

| 触发 | 冻结命令 → 事务事实 → 审计／原回执 |
| --- | --- |
| 首次接收A，入口已有seq=6、模式NORMAL／epoch=2 | A身份／原事件及稳定意图在execute前冻结；写事务去重未命中后分配7、读取NORMAL／2和正常位置；原回执结果及必要审计都记录同一7／NORMAL／2，只有共同COMMIT后确认接收 |
| A、B在锁外均已准备，竞争同一入口 | 若A先提交则7／8，若B先提交则B=7、A=8；输入指纹均不预占序号。竞争拒绝者保持原命令，先按原键确认，可靠不存在后才重试；同身份重复仅一次变更／审计，异内容仍冲突 |
| 接收与NORMAL／2→DREAM_PREPARING／3切换互为先后 | 接收先提交则保留NORMAL／2及正常位置，切换先提交则新接收为PREPARING／3及专注位置；模式命令预先冻结expected_epoch=2、目标转换、run和理由，实际前后模式／epoch进入该模式原结果及审计。已提交A在切换后重投仍返回A原位置，不重新路由 |
| A的COMMIT已成功、任何回执或恢复句柄送达前退出 | 新实例先按§8完成存储／配置就绪和旧owner隔离；调用方从原事件身份、完整原事件、原主体绑定及稳定审计意图重建recovery_handle，resolve_operation返回原7／原时间／原路由；handler和审计物化次数均为0。当前mode或后续seq变化不影响恢复 |
| 同样断点但提交证据不能确认 | 只做有限resolve；锁／I/O故障保持UNCONFIRMED，不拿NOT_FOUND当不存在；只有原owner结束并满足基础确认协议后返回NOT_COMMITTED，才可同键重做首次接收 |

模式转换、回流页和配置初始化只要含事务派生事实，也按同一增量声明；稳定的预期修订、目标转换、候选／页起点和理由仍在指纹内。配置初始化的具体派生项由[配置正文](configuration.md#runtime-configuration-identity)唯一规定。基础回执仅保存安全结果所需字段，审计历史及其意图证据不进入接收接口、诊断或agent。

<a id="batch"></a>

## 4. 三段推进、冻结与唯一执行者

### 4.1 持久结构及正常推进

缓存拥有各入口的正常未消费序列、当前历史辅助槽位、冻结成员、终态、引用位置和回流游标；接入拥有原始事件身份及原文。队列／来源持有显式引用，不靠队列位置充当永久来源ID。记忆参与者将来取得完整来源保护权，媒体模块将来取得物理文件生命周期权。

设平台配置中的最新辅助长度为R、目标长度为T、历史辅助长度为H。未冻结正常输入按entry_seq升序形成U；较新的`min(R, |U|)`项为最新辅助候选，其前缀为目标候选。相当于新输入进入最新段，超出保留长度的最旧项向目标候选推进。R=0时新输入可直接成为目标候选；H=0时不留历史。正常触发取目标候选最旧T项作为非空目标；后面紧邻的最多R项作为本批次最新辅助快照，剩余U保留在后续待处理区。这避免积压很大时跳过中间消息、只取全队列最末尾作为“较新上下文”。未处理消息每次成为目标的顺序仍是FIFO。

有活跃批次时，当前三段已经固定；后到输入只追加未处理区，不能改该批次成员。普通容量参数只作可见软水位及调度提示，本契约不据它删除未消费输入、历史引用或在途目标；若将来需要容量淘汰，另有合法终态／审计契约后才能加入。

短批次／空闲尾部的建议策略及替代方案只在[产品选择](../product/decisions-and-delivery.md#ingress-runtime-product-options)维护；触发类型是THRESHOLD、EXPLICIT_SHORT、IDLE_TAIL。无权限、未启用或类型未支持须明确拒绝；不能把任意普通消息字段解释成触发。显式触发有独立稳定trigger_key，重复不会建第二批；无目标返回NO_TARGET，不调用模型。空闲时钟仅用于调度，重启重新计算等待，不把单调时间戳跨进程持久比较。

### 4.2 冻结事务

冻结前在有界本地处理中准备候选范围和估算材料大小；事务内重新核对入口修订、最旧未消费目标、当前历史槽位及模式。只有模式允许、配置可恢复、必要参与者就绪、没有本入口活跃目标批次时才能冻结。预算按[配置正文](configuration.md#runtime-batch-validation)检查，推荐合成候选的材料与模板只按[§10.1](#synthetic-window-material)构造；不裁掉原始正文或静默缩窗。如果最旧目标连同既定上下文无法完整容纳，返回CONTEXT_LIMIT并可见阻塞，不跳过旧目标，也不在持久性失败时按普通失败轮转。配置初始化后不可改时，这可形成同库持续阻塞；具体推荐值例子、预算相容约束和替代方案只在[产品补充](../product/decisions-and-delivery.md#runtime-context-overflow-options)维护，不能将重启或未实现的配置管理当作解法。

同一冻结事务保存以下内容：

| 记录 | 必须可恢复的内容 |
| --- | --- |
| 批次身份 | batch_id、entry_id、run_id、trigger身份／类型、首次创建时间、批次格式与revision |
| 冻结清单 | history_context／target／recent_context的有序message_id＋entry_seq、每项不可变原文修订／摘要、实际数量和可空辅助范围；三个角色在该批次内不重叠 |
| 语义依据 | 真实config_snapshot_id、实例／平台配置版本、模板／参与者格式版本、实际预算及完整材料摘要；参数细节只由配置模块解释 |
| 所有权 | 目标唯一占有关系、原始／媒体保护引用、持久工作状态及执行代次；不存在“先冻结内存以后再登记”窗口 |
| 来源基底 | 完整三段材料的可恢复引用清单；不只指向可轮转队列位置。后续成功参与者在同事务取得独立来源引用 |

冻结只固定成员及语义，不宣称模型已经发送。冻结事务与模型登记是两次短事务；间隔内的模式切换可以阻止发送。源材料按冻结引用读取，摘要／所有者不符是完整性故障；不能改读最新事件或重建假来源。

### 4.3 执行唯一性与公平

实例仅有一个活跃运行调度所有者，生命周期独占由基础设施资源能力保护；每入口数据库唯一约束再保证最多一个未终结目标批次。执行claim使用`work_id + expected_revision + owner_generation`条件更新，向参与者签发有限授权；所有候选和终结命令重查当前owner及批次修订，旧代次迟到不能写。

租约到时只标可疑／需隔离，不能凭时间超限即让第二执行者接手：必须证明旧进程退出或旧本地工作／适配器和存储任务已结束并被隔离。cleanup_pending期间保留执行槽和资源所有权。此范围不批准双进程业务调度；另一个进程仅可用于竞争拒绝和恢复验证。

调度从持久就绪入口按轮询公平取有限工作，每次每入口至多一项；执行容量来自统一配置，Provider全局／账户并发与费用仍只由Provider执行。积压规模不等于内存队列长度，不一次加载全部入口或全部历史。移交和批次运行分别使用有界页及短任务，单入口故障标记不吞掉其他入口的公平机会。

<a id="terminal"></a>

## 5. 候选、三终态与一次轮转

业务终态只有`SUCCEEDED`、`FAILED_DROPPED`、`SENSITIVE_DROPPED`；成功零结果是SUCCEEDED的`result_count=0`。其余都是工作／确认状态，不扩成第四种业务终态。

| 工作状态 | 进入证据及允许下一步 |
| --- | --- |
| FROZEN | 冻结已提交，尚未取得执行归属；可claim |
| WAITING_ADMISSION | 未开始模型，或可证从未发送的门控／预算／容量阻止；保留冻结范围，记录阻止原因，不消费目标 |
| EXECUTING | 有活跃owner及受控调用归属；重复触发不另建工作 |
| CANDIDATE_STORED | Provider持久交接已确认，受控参与者已保存完整确定性候选／失败证据；只允许本地验证与提交 |
| LOCAL_COMMIT_UNCONFIRMED | 本地候选／终结确认未知；只确认原操作，不能重学 |
| REMOTE_RESULT_UNKNOWN | Provider已登记但无可靠完成证据；保留目标、引用、调用及费用责任，停止该批次新发送 |
| SYSTEM_BLOCKED | 配置、参与者、所有权或存储无法安全继续；不伪装Provider失败；记录恢复需要 |
| TERMINAL | 三终态之一及唯一终结回执已持久提交；不再调度该批次学习 |

Provider结果映射须同时核验工作／批次归属、请求ID、能力和明确结果类别：

- `Completed(SUCCEEDED)`只提供模型结果，须由受控学习参与者校验结果及目标锚点、取得稳定候选／对象ID，再保存候选；普通文本成功不是直接发布记忆的权力。
- 明确学习请求的`SENSITIVE_REFUSAL`才可形成SENSITIVE_DROPPED；媒体请求的敏感拒绝始终为媒体局部状态。用户正文及OTHER_REFUSAL不能触发清空历史。
- 实际已执行学习的普通模型失败、OTHER_REFUSAL、已明确结束的超时／取消，且Provider不再进行合法有限尝试时，可提议FAILED_DROPPED；先持久保存明确终结依据，再本地轮转。格式错误须区分Provider确认的INVALID_RESPONSE与本地参与者／存储完整性错误，后者SYSTEM_BLOCKED。
- MODE_BLOCKED、PAUSED_BUDGET、RESOURCE_BUSY、配置／能力不可用不是学习失败终态。从未发送且原请求已确定关闭者可保持WAITING_ADMISSION。Provider同键被阻止结果不会自动变成功；仅按获批准的[恢复选择](#recovery)为尚未开始的工作重新申请，不能机械反复调用同键或换键刷请求。
- Pending、本地提交未知及读失败不轮转。曾有发送、尚不能按以上规则确定终结的“部分尝试后被门控阻止”归SYSTEM_BLOCKED，等待明确恢复决定，不扩大“从未发送”的例外。

候选记录包含batch／run、原配置身份、Provider请求／交接引用、outcome、稳定对象ID和确定性变更集、直接目标锚点、辅助解释引用及候选摘要。生成ID在首次候选保存前固定，同键异候选冲突；恢复只用这份候选和已持久交接，本地确定性变换可以重做，不重新请求模型。结果完整性失败保留输入并报告故障，不当作零结果。

终结单次UoW的参与者为缓存、运行、接入原文所有者、受控结果所有者、必要引用所有者及日志审计；成功时另有记忆／来源参与者（本范围用合成者）。事务重新核验候选、三段、配置身份和owner，按产品三终态执行目标ID消费、历史槽位更新和独占候选引用释放；正式结果／来源或合成结果、terminal状态、终结回执、引用变化、待办与必要审计全有或全无。任何必要参与者／审计失败毒化整个事务。

成功和普通失败的新历史来自**本批次目标快照**尾部，长度为min(H,实际目标数)，普通失败附失败来源标记；敏感拒学按产品规则清空本入口历史槽位且不留目标尾部。最新辅助、冻结后输入、其他入口、其他批次共享来源和已提交对象不在删除集合。轮转是解除具体owner的引用，不是按message_id做跨所有者级联删除。物理媒体删除仅归将来的统一GC，不在本次终结内。

接入的原文payload与幂等身份／原接收回执分开保留。终结时释放冻结材料的运行引用及被消费队列引用，由接入所有者在同UoW删除已无任何业务引用的原文payload；仍被新历史、后续输入或共享来源持有的payload必须保留。终结批次仅留成员ID／摘要／计数元信息，不继续用完整运行快照保护本应清理的原文。幂等身份、指纹和接收回执仍保留，同键重投比对原指纹、返回原确认，不重新写回已合法释放的正文。

候选独占正文在终结后释放，候选元信息保留处置状态及terminal引用，使旧候选保存回执仍可解释为“当时已暂存、现已终结”，不返回悬空的成功正文。未终结候选和必要恢复产物不得提前清理。Provider自己的交接／账本仍按其既有保留协议，本服务没有交接删除权；不能把清理业务候选解释为清空Provider或审计历史。

终结幂等身份固定到batch_id和已持久候选／终态语义；唯一约束保证一个batch至多一个终结。重复同语义返回原回执，不再执行参与者；不同终态或结果返回冲突。旧终结回执返回后，当前最新S3可能已属于另一批，不能依据“再清一遍”恢复。FAILED_DROPPED及SENSITIVE_DROPPED不计待处理，分别计学习缺口和突然失忆风险；候选尚未确认或UNKNOWN继续计受保护未终结输入。

<a id="modes"></a>

## 6. 模式、在途收尾与发送许可

### 6.1 两层状态与持久门控

持久模式为NORMAL、DREAM_PREPARING、DREAM_FOCUSED、DRAINING、FAULTED；每次实际模式改变单调递增mode_epoch。RECOVERING为启动期间强制生效的服务门控覆盖层，同时显示原持久模式，不能先把原状态改成NORMAL。每入口另有`transfer_state=NONE/PENDING/TRANSFERRING/BLOCKED`、游标和数量；它与实例是否允许普通业务分离。

| 模式 | 新原始输入 | 普通业务／学习／媒体／主动投递 | 内部专注整理 | 只读观察 |
| --- | --- | --- | --- | --- |
| RECOVERING | 新接收拒绝RECOVERING；原回执可靠可读时可确认 | 拒绝，不发送 | 仅本地恢复 | 允许受控健康／恢复进度 |
| NORMAL | 正常位置；有本入口遗留回流时接回流尾 | 仅已实现且获授权者可用 | 非专注整理未实现，明确能力不可用 | 允许 |
| DREAM_PREPARING | 本入口专注暂存 | 新工作及新普通发送拒绝DREAMING；已消费许可的在途按§6.2收尾 | 尚不启动 | 允许 |
| DREAM_FOCUSED | 本入口专注暂存 | 全部拒绝DREAMING，不暗中排管理命令 | 仅有效梦境run及当前epoch授权的工作 | 允许 |
| DRAINING | 本入口有旧积压则接回流尾，否则正常接入 | 开放点采用已批准产品选择：发布确认后开放，入口独立回流 | 原梦境已结束，无旧梦境发送许可 | 允许 |
| FAULTED | 存储／身份／路由仍可可靠确认时仅暂存且回执标故障；否则如实拒绝／未知 | 不发送、不写业务对象 | 不发送 | 尽力提供故障元信息，标数据时点 |

FAULTED接收例外不能越过已FAULTED存储服务的停止写入要求：只有模式任务故障而存储仍READY、可信暂存路由可在事务内确认时才允许。未知持久模式时不猜去向。所有模式都不因打开Web授予业务／管理写入；原始事件不会在回流时变成被拒绝操作的重放。

持久转换仅允许下表边；其他组合返回PRECONDITION_FAILED，不把重复同键调用当成第二次转换。epoch只随已提交转换变化，读取／启动覆盖层不增加一次虚假的业务转换。

| 起点→终点 | 事务前置及恢复边界 |
| --- | --- |
| 初始化→NORMAL | 完整新配置／入口基础已确认、无未恢复工作、所宣告能力就绪；初始化回执原样幂等 |
| NORMAL→DREAM_PREPARING | 有效内部dream run、expected_epoch匹配、无前轮回流，建立收尾截点 |
| DREAM_PREPARING→DREAM_FOCUSED | 截点集合全部安全收尾／停放、无未决写／远程结果／资源占用 |
| DREAM_FOCUSED→DRAINING | 对应run完整发布证据已确认，梦境发送和写者已结束，原子启用回流 |
| DRAINING→NORMAL | 持久待移交入口计数为零，且无与最后页竞争的旧积压；不以此宣称所有输入学习完成 |
| 任一运行模式→FAULTED | 核心一致性、模式执行或收尾失败的可靠证据；库已不可写时仅健康覆盖层标故障，不能声称该转换已持久提交 |
| FAULTED→其他持久模式 | 本范围无自动边或Web命令；仅能确认此前已发且实际提交的原转换，新的故障解除操作仍待批准 |

重启RECOVERING完成后仅解除覆盖层、恢复可靠持久模式；若旧模式为FAULTED则仍关闭，若为FOCUSED且run中断则按§8故障处理。局部入口的预算阻止、CONTEXT_LIMIT或REMOTE_RESULT_UNKNOWN只阻止该入口／工作，正常模式的其他入口仍可推进，前提是共享存储与Provider仍READY；CONTEXT_LIMIT不因模式恢复自动解除，FAULTED也不因确认一个原操作就自动转NORMAL；核心存储故障和专注切换中的未决状态才关闭实例。对原文／引用完整性损坏不能未经核验就声称仅影响一个入口。

### 6.2 入梦事务及在途范围（已批准方案）

推荐产品行为详见[产品选择](../product/decisions-and-delivery.md#ingress-runtime-product-options)。`enter_focus`首先取得短门控串行区并关闭新普通发送；持有切换中的拒绝标记，事务外不等待模型。模式事务按expected_epoch保存PREPARING、新epoch、dream_run_id、持久转换操作及截点前工作集合／收尾状态，同时开启新输入暂存。提交确认后发布内存门控；若确认未知保持关闭并进入恢复观察，不先开放再补记录。内存关闭早于提交可短暂保守拒绝；只有确认未提交且没有其他故障时才可恢复原门控。

入梦前未claim的冻结批次，以及确定从未发送且无业务写副作用的工作，可以按原范围持久停放，owner撤销，不清空输入；它们不算在途认知写入。已经消费许可的模型调用是在途：允许原owner持久交接、候选校验和一次本地终结；不批准新的普通attempt、工具模型调用、备用供应商或主动投递。收尾期间暂停普通配置变更和后续批次冻结，输入继续专注暂存。

只有截点前在途工作全部已确认终结或确认安全停放、无未决本地写／远程结果／资源占用，才可提交FOCUSED并为梦境工作签发新epoch授权。超过收尾总期限、出现UNKNOWN或无法证明旧写者结束，转FAULTED并保持关闭，不把它算作梦境已开始或学习普通失败。故障后自动解除、强制中止及重新发送都属于待批准管理选择；本契约推荐不提供这些管理动作。

### 6.3 最后发送竞争

通过现有[GateBinding／bind_gate](../../companion_memory/provider/resources.py)注入运行服务的可信适配器，不改Provider账本所有权。运行服务从持久claim恢复并登记原生WorkGrant与具体work／owner_generation／mode_epoch的关系，Provider角色、entry_ids或`internal_dream`标记均须匹配该关系；不能用字段值自行构造授权。发送开始只在Provider登记获COMMITTED之后。

`authorized`验证调用及结果归属，`check`用于初始准入；`dispatch(grant,start)`在与模式切换／撤权共用的短串行区中重查当前epoch、工作代次、任务允许集合及所有者，并至多调用一次Provider提供的有界启动回调。回调只启动受限工作器，不在锁中执行模型、不等待存储。许可消费与调用开始是这个不可分边界，不能先拿许可后排入另一个可无限等待队列。

若切换关闭先发生，dispatch拒绝且start次数为零；若dispatch先发生，该次已启动调用纳入收尾集合，切换不得假称取消了远程效果。下一attempt仍须重新检查。若持久mode更新已提交但内存发布前崩溃，重启RECOVERING关闭所有发送；PREPARED的恢复由Provider按UNKNOWN解释，不能因“运行服务没记录start”断言未发送。

模式身份不足、Provider所有者未隔离、日志说已发送但账本不可读、内存epoch与持久代次不一致时均关闭发送。普通远程调用只由Provider做其一层有限尝试；运行服务无第二套retry loop／费用计数。

<a id="transfer"></a>

## 7. 梦境完成、入口回流与新输入

`finish_focus`需要本梦境run的持久完成证据：整理参与者已完成所要求的步骤、完整发布回执／稳定发布指针、配置及run一致、无未决写和在途梦境发送。语义完成与发布由梦境／persona所有者负责；运行服务不能凭一句“成功”或者一个内存boolean开门。本范围由合成发布参与者提供同样约束的合成证据，页面必须显式标记。

先将关闭该梦境发送的门控标记置入串行区，再用模式事务核验发布证据、写DRAINING／新epoch／回流启用标记及必要审计。只有确认该事务COMMITTED才按推荐方案开放普通业务。发布已提交但模式切换未确认时，恢复只确认／重做原本地模式事务；不重跑梦境、重发模型或改发新persona。没有完成证据的中断保留上次发布指针及暂存序列，FAULTED如实显示失败。

回流事务按入口序列取最旧有界页；在同一次UoW内建立正常待处理位置及其引用、移除原专注／待回流位置、更新单调游标和移交数、必要审计及回执。媒体正常处理可在移交后异步执行；“正常路径已取得消息与引用”是解除暂存所有权的前提，不要求模型理解完成才移交。引用在事务前后均受保护，回滚没有空窗。

接收和移交共用该入口事务修订：只要仍有未移交旧积压，新输入追加同入口待回流队尾；最后一页移交在事务内确认队列为空才关闭该入口接尾标记。并发新输入先提交则包含在后续页，移交先提交且已空则新输入入正常队尾，两者均不越过旧消息。回流cursor按entry_seq推进，页操作key及起始游标固定，重复提交不双加移交数；游标冲突只重新读取元信息，不换旧页内容重试。

梦境前普通未终结输入始终排在梦境期输入之前，原始entry_seq／发生时间／接收时间不改；另记transferred_at及移交run用于观察。回流清空只表示已移交；正常未满目标和最新辅助留待正常合法事件。移交、开始学习、成功学习、失败终结分别计数。另一入口积压、故障或未清空不阻止已清空入口正常接收与学习。

全实例无待回流入口后可用短模式事务由DRAINING转NORMAL，不扫描全部正文；持久待回流入口索引及计数须同移交更新。新入梦请求在仍有上一轮回流时的推荐处理为返回PRECONDITION_FAILED，不嵌套两轮回流；替代策略另见产品选择，不能隐式混用dream_run_id。

<a id="recovery"></a>

## 8. 故障、确认与跨进程恢复

### 8.1 三个互相独立的事实

| 事实 | 权威、状态及恢复责任 |
| --- | --- |
| 本地提交是否发生 | 持久化COMMITTED／NOT_COMMITTED／UNCONFIRMED及原回执；编排只能按受控确认协议核实，不从NotFound、超时、断线推导回滚 |
| 远程是否得到结果 | Provider的PREPARED／REMOTE_RESULT_UNKNOWN／已持久完成交接；运行服务仅关联，不改费用，不把本地恢复变成模型重发 |
| 本地资源是否仍占用 | 原存储／Provider／执行器所有者的cleanup_pending；即使已有提交回执或远程结果已知，也可能继续占线程、槽位、目录或句柄 |

健康及页面须同时显示三者，不能用一个FAILED遮蔽未知和清理。原结果返回后不变；迟到完成只更新新的只读观察。对未知批次的保护仍有效；已有失败终态的批次不因此恢复成目标。

### 8.2 启动顺序与恢复表

<a id="runtime-startup-layers"></a>

可信启动先关闭业务和发送，串行核验原资源身份及旧所有者隔离。**存储READY → 配置身份恢复／初始化确认 → Provider READY → 运行层恢复完成**是门槛顺序；上游未就绪不能启动下游扫描。主控未完成状态显式带stage=STORAGE/CONFIGURATION/PROVIDER/RUNTIME、固定blocked_reason、cleanup_pending及可公开的进度，不能用单一RECOVERY_PENDING暗示所有层都能续进度。阶段状态只在受控观察投影，底层错误／句柄不透传。

| 层／实际入口 | 续进度、期限结果及所有权 | 下一次合法操作 |
| --- | --- | --- |
| 存储initialize(OPEN_EXISTING) | **不支持续进度**；一次storage.operation_timeout_ms内完成格式、Schema、完整性及全部回执／必要审计校验。按原协议返回Ready、Rejected或InitializationUnconfirmed；总等待结束不取消I/O，cleanup_pending时仍持连接／槽位／路径。新审计证据也必须纳入全校验 | 只观察健康／受控close和既有合法确认；不得向未READY服务要运行仓储。原任务／资源确已结束后，在允许NEW状态再initialize，或关闭原服务、新建实例OPEN_EXISTING，均从头校验；不接运行checkpoint、不跳过旧回执 |
| 配置恢复／初始化确认 | 仅存储READY后按配置正文校验完整持久域并签发视图；本范围不新增可跨调用的配置校验扫描cursor。失败原子、超时不发半份快照，若底层读取／初始化写未结束保留其所有权 | 已有完整配置则从头加载并核验；原初始化未知先以原键／候选确认，可靠未提交才本地补齐。存储重新FAULTED则退回存储阶段，不能先建Provider |
| Provider.initialize | 已有有界分页和幂等本地恢复；同实例NEW且原初始化任务已结束时，可继续其保留扫描位置。跨进程不承诺持久扫描cursor，而是从头核验、复用已提交本地恢复事实。扫描在自身期限到达时可返回RecoveryPending(TIMEOUT／DEADLINE_EXCEEDED)；外层等待先结束、任务仍在途时可返回RecoveryPending(PERSISTENCE_FAILED／LEDGER_UNCONFIRMED, cleanup_pending=true)，不能取消任务或释放绑定 | 同实例任务仍在途时只观察，不启动第二恢复者；结束后按实际生命周期，NEW可再显式initialize，READY无需重复，FAULTED／关闭须待资源释放／隔离后重新装配。原完整性校验不可跳过，恢复不调用模型 |
| 运行层initialize_runtime／recover_runtime（新增） | 只在以上层已就绪后，按有界页重建入口／引用／工作及模式。新增检查点绑定database_id、配置快照、恢复协议版本、当前恢复代次、扫描范围／位置及已验证修订；每页核验、必要本地收尾和checkpoint同事务提交。总runtime.recovery_timeout_ms不因分页重置；到期返回RECOVERY_PENDING(stage=RUNTIME)，在途任务继续持运行owner及借用资源 | 同代次、上游仍READY且原页任务已结束时，以原页键确认后续进度；跨进程先重新完成全部上游初始化，再验证检查点绑定及业务引用，重扫不能可靠证明的页。不得信任旧“检查完成”标记跳过当前完整性核验；完成才解除RECOVERING覆盖层 |

Provider初始化仍遵守[既有恢复契约](provider.md#provider-foundation-transactions)和[initialize／_bootstrap](../../companion_memory/provider/service.py)的相关边界。已批准最小增量[WorkPort原键只读确认](provider.md#91-公开端口输入与能力)，用于首次返回前退出后，从原冻结请求确认request_id／状态，再经原结果所有者端口恢复持久交接；未命中不授予重放许可，不扩展初始化扫描或远程重发。运行层checkpoint是本契约新增模块状态，不能作为持久化或Provider内部扫描凭据；其审计派生值按[结果绑定增量](persistence-and-transactions.md#runtime-result-bound-audit)处理。

[存储启动限制](persistence-and-transactions.md#runtime-storage-startup-boundary)意味着完整回执校验若长期超过存储预算，上述流程可一直停在STORAGE，运行检查点再完整也不能使实例READY；本范围推荐不扩展该基础能力。某个原提交终于确认或cleanup_pending变false，只解除对应事实的不确定性／占用，不自动解除持久FAULTED、上下文预算阻塞或尚未批准的管理故障状态。

| 重启／异常截点 | 必须恢复的结果；均不调用模型 |
| --- | --- |
| 接收COMMIT前退出 | 在旧执行者隔离后由原身份确认；确认未提交时调用方可同键重新接收，未送达不声称已接收 |
| 接收COMMIT后回执未送达 | 按§3.4原命令／意图重建恢复输入，返回原接收回执、序列与路由，校验结果绑定审计，不再追加 |
| 冻结已提交，尚无Provider逻辑登记 | 恢复FROZEN／WAITING_ADMISSION及原配置；恢复阶段只重建。NORMAL门控开放后的首次调度才可能发送，属于尚未执行工作 |
| Provider登记确认丢失／PREPARED且旧owner已退出 | 先按Provider本地确认；仍不能证明远程未发即UNKNOWN，保留输入和预算，不派新attempt |
| 规范化结果及交接已落盘，候选尚未保存 | 授权结果所有者取回同一交接，本地确定性校验／生成稳定候选；无模型调用 |
| 结果仅在已退出进程内存，未持久交接 | UNKNOWN，无“再次生成同一结果”恢复 |
| 候选已保存，终结尚未提交或确认丢失 | 先确认原终结键；存在返回原结果；可靠确认未提交则只按已保存候选重做本地事务；不可读则保持未知 |
| 已提交三终态，任意后续重启／重复触发 | 返回原终结及一次轮转事实；不得重新学习失败批次或把历史尾部重新当目标 |
| PREPARING中断 | 恢复持久截点集合，已持久结果只做本地收尾；未决远程／旧owner不能结束则FAULTED，不自行进梦／开门 |
| FOCUSED中断，尚无发布证据 | 保持门控关闭，标INTERRUPTED／FAULTED及原暂存；不自动再次梦境调用、不发布半份结果 |
| 发布已提交，退出模式或最后回流页确认丢失 | 查原发布／模式／页回执，幂等完成本地切换或移交；正文、引用、游标和计数只变一次 |
| 物理写失败或必要审计失败 | 保存能可靠保存的故障元信息；停止受影响写／新模型；失败本地事务不将批次记为Provider普通失败；恢复后仅确认及本地收尾 |
| 原底层I/O仍运行 | 不签发替代owner、不重开同一路径；有界返回占用状态，由可信进程生命周期控制完成隔离后才重新装配 |

新服务就绪后，普通首次调度与恢复是独立动作。对于从未发送且因准入被阻止的工作，推荐仅在模式恢复或明确的内部重新准入事件后申请一次新Provider逻辑键，持久递增admission_generation、保留旧阻止记录及原batch；不周期刷预算、不改原冻结材料。每个重新准入事件去重且受次数／期限约束；这属于[已批准业务选择](../product/decisions-and-delivery.md#ingress-runtime-product-options)；没有明确重新准入事件时保持WAITING_ADMISSION。

### 8.3 本地重试和管理边界

本地execute返回NOT_COMMITTED时，推荐在原总期限内至多一次明确本地重试，操作键、候选、目标ID和语义不变；每次仍由唯一键去重。UNCONFIRMED只能有限确认；确认失败保持阻塞。故障存储服务不能后台重开，新实例须由可信生命周期编排在原资源释放后显式建立。上述策略的数值在配置补充中维护。

本契约不提供管理员“强制完成／忽略UNKNOWN／清空积压／重放失败批次／释放未知费用／终止线程”按钮。重启不等于管理员批准这些动作。需要以后增加时，须有单独操作身份、expected_epoch／revision、权限、风险预览、确认、同事务审计和Provider费用责任协议；具体建议与替代方案集中在§12，不能从观察页面推导授权。

<a id="observation"></a>

## 9. 最小Web与运行状态只读契约

### 9.1 受控视图与数据时点

管理层只取得原生`RuntimeObserver`、入口状态观察能力和[日志查询能力](logging.md#runtime-web-observation-draft)；没有业务仓储／OperationPort／完整配置快照／审计读取／模型工作／结果正文能力。运行模块通过各所有者的固定元信息查询组合视图，不给Web内部数据库对象；Web不扫描日志文件、不直读业务表、不自动探针或修复。

| 视图 | 最小可见字段 |
| --- | --- |
| 实例模式 | observed_at、snapshot_revision、持久模式、生效门控覆盖层、epoch、发布完成与否、当前梦境run、收尾／恢复进度、blocked_reason、是否可接收／是否允许普通业务、能力来源ACTUAL/SIMULATED/SYNTHETIC |
| 入口状态 | 授权内部entry_id／平台、正常未消费数、目标候选数、活跃目标数、最新辅助数、历史辅助数、专注待移交数、移交游标／累计量、最早／最新接收时间、入口阻塞原因；辅助重叠数不重复加进总待处理 |
| 批次／缺口 | batch_id、阶段／业务终态、实际三段数量、config_snapshot_id、结果数量、合成标记、失败／拒学及受影响序列／时间范围、本地确认状态、remote_unknown、cleanup_pending；无任何三段正文 |
| 服务健康 | 存储可用性、运行／Provider执行占用、配置可恢复状态、日志健康／缺口、上次可靠观察时点；不能用未知数据填零 |

入口元信息的一页取同一短数据库快照；实例模式与该页如不能同快照读取，分别携带revision／observed_at并标`COMPOSITE_OBSERVATION`，不宣称全实例同一瞬间。Provider／日志内存健康与业务快照亦分别标时点。`pending_total`按尚未终结且仍待处理的原始message_id去重，包含活跃目标和暂存，不包含只留作历史辅助的已终结目标；最新辅助在U内只计一次。

数据库故障时Web仍可读内存健康及上次成功元信息，必须带`STALE`和时间；从未读到则`UNAVAILABLE`，不能显示零积压或“正常”。Web进程自身不可达时无自我显示保证；外部监控不在本范围。

### 9.2 最小HTTP映射和权限

推荐提供只读页面`/status`及GET `/api/observe/runtime`、`/api/observe/entries`、`/api/observe/batches`、`/api/observe/logs`。查询白名单为授权入口／批次ID、固定状态过滤、页大小及不透明cursor；没有任意字段、排序表达式、SQL、全文搜索、文件路径、导出或回放命令。日志过滤由日志正文规定。前端仅呈现转义文本，页面刷新无业务副作用。

只读会话需要独立`runtime.observe`及`diagnostics.observe`权限，绑定实例／入口集合；没有权限的过滤条件在查询前拒绝，不能借计数推断其他入口存在。Provider用量、敏感审计、完整来源、配置编辑等权限不隐含授予。运行观察权限不给agent工具使用；用户在事件正文写role或token不影响会话权限。

本范围推荐可信测试装配签发的短期、不可猜测观察会话，由实际HTTP鉴权适配边界换成原生只读能力；浏览器测试由测试宿主注入会话凭证，页面不内置秘密，不提供登录／管理员创建接口。无会话401、权限不足403、参数非法400、冲突409、明确超过速率／容量429、服务状态不可用503；日志游标缺口返回409及LOG_GAP。成功读取200，空页为有时点的真实空结果。错误不暴露授权对象存在性。

仅使用自有测试资源／回环HTTP监听的认证夹具证据，不宣称生产登录、密钥保管、账号撤销或公网安全已实现。生产HTTP身份来源、TLS／代理信任及会话机制须按[存储与生产前置](persistence-and-transactions.md#runtime-production-prerequisites)提交具体方案批准；未满足时不提供匿名生产降级。所有写方法／管理路由不装配，405／不存在路径均不得改变状态；专注期只读GET仍走相同权限。

日志两类观察者的完整字段及缺口投影只按[日志游标权限正文](logging.md#runtime-log-cursor-privacy)返回；实例级事件权不含全局水位／计数权，部分入口页面不能借服务健康卡片转发被禁止字段。普通运行观察中的实例模式／实例恢复进度须另有明确实例观察范围，只有入口范围时只返回本入口状态及对它适用的门控／故障，不汇总其他入口。

分页cursor绑定授权范围、过滤条件、数据水位和稳定排序键；改scope、过滤、过期或重启不兼容就拒绝／要求重查。每请求页数、行数、总字节、等待时间、并发与客户端刷新间隔全部由[配置](configuration.md#runtime-observation-parameters)限制。没有长读事务、无限SSE队列或全库扫描；慢客户端只影响自己的有界响应。状态列表按索引keyset分页，页间允许新状态变化，水位绑定在不透明cursor内；只有获授权的元信息才显示，不承诺持续一致导出。

<a id="participants"></a>

## 10. 受控参与者及证据边界

实际记忆／来源／媒体所有者的后续替换方案见[集中已批准契约](formal-memory-source-media.md#baseline-ports)；本节既有合成参与者及材料证据边界保持，不自动转正。

| 参与者 | 必须验证的端口／约束 | 可以得出的证据与不能宣称的能力 |
| --- | --- | --- |
| 实际接入／缓存／模式／配置服务 | 本文端口、持久格式、真实状态机及事务 | 实际代码、同库账本、回执和恢复按整体验收；执行证据见[当前任务](../work/CURRENT_TASK.md) |
| 实际Provider服务＋模拟适配器 | 既有WorkGrant／GateBinding、登记后发送、受限结果交接 | 实际服务门控和本地计量，模型回应／usage／金额均SIMULATED；不代表真实模型质量、调用协议或计费 |
| 合成学习参与者 | `prepare_outcome(batch_snapshot, provider_handoff)`确定性生成有界候选，核验非空目标锚点及独立辅助引用；输出synthetic=true | 可验证只总结目标的结构约束、零结果／多结果／普通失败／敏感拒学和恢复；不能以固定答案证明认知质量或自主agent工具规划 |
| 合成记忆／来源参与者 | 同一UoW中`stage_result(candidate)`、`retain_source(manifest)`；共享来源仍引用原始完整材料；可注入必要参与者失败 | 真实临时SQLite中的合成对象可证明原子发布、来源保护及共享引用；此参与者不提供正式记忆、生命周期、关系推理或来源读取服务；正式记忆与来源扩展见[独立契约](formal-memory-source-media.md) |
| 合成媒体引用参与者 | 向单条出现记录签发READY引用，绑定库／入口／对象；UoW内retain／release／transfer引用，不触碰物理文件 | 可证明引用连续及共享保护；只在测试装配签发非秘密合成资源，无实际上传、解码、理解或GC保证 |
| 合成梦境／发布参与者 | 持久run／检查点、完整发布引用、失败不改上次指针、同UoW校验完成证据；可用DREAM角色模拟调用测试门控 | 证明模式依赖有效发布、失败可见和回流；不能把合成“发布成功”展示为persona更新／梦境整理完成 |
| 实际Web／日志查询＋测试会话 | HTTP只读路由、授权过滤、转义、有界查询和慢客户端隔离 | 可证明实现的权限边界及页面行为；测试身份提供器不等于生产鉴权 |

合成业务仓储、失败屏障和场景控制只在tests／可信测试装配中存在，不作为正式包的默认成功实现；正式装配缺参与者即明确拒绝相应能力。测试使用实际UoW和临时数据库，不以纯内存字典替代原子性证据。每次结果和观察页同时保留服务执行来源、模型适配来源、结果参与者来源，避免用一枚“真实”标签覆盖三种证据。

普通可验收接入子集是无媒体的原始文本／感知事件。含实际附件的接收在媒体所有者未装配时整体返回MEDIA_NOT_SUPPORTED，不能确认完整接收后丢掉附件，也不把未支持格式误填为“敏感信息无法访问”。合成READY引用可覆盖事务引用路径；外部已有理解原样保存并标来源，缺失理解在专注期绝不触发Provider，正常／回流期由媒体参与者明确报告可处理／缺能力／局部拒绝。没有真实媒体参与者时不宣称正常媒体处理已经完成。

本范围的学习参与者只发一个有限的生成逻辑请求，不实现多步工具循环；推荐候选使用下方唯一材料协议与[确定参数及代入](configuration.md#runtime-compatible-budget-candidate)，不从参与者私有默认值取得预算。未来加入认知步骤、模板变更、实际媒体或正式记忆参与者时，须沿用这些工作／结果端口并另验业务能力，不把本次合成结果自动迁移成正式认知。

<a id="synthetic-window-material"></a>

### 10.1 可核算的合成材料与模板（已批准）

此协议只供受控合成学习参与者，保留完整正文与FIFO，使用固定目标数，不选择可变目标前缀。它不声称真实模型能正确理解编码材料，也不替代正式认知模板；Base64是可逆传输编码，不能作为摘要、截断、秘密保护或日志脱敏。实际参数在[配置候选](configuration.md#runtime-compatible-budget-candidate)唯一维护，本节唯一维护格式、模板和上界依据。

**固定版本绑定。** material_contract_ref精确绑定参与者synthetic_learning／1、事件格式synthetic_event_json／1、材料格式synthetic_window_base64／1、模板synthetic_target_refs／1、上界规则synthetic_window_bound／1。可信静态装配按此组合签发material_contracts，不接收调用方自报B／W／Q或替换模板字符串。配置快照、批次冻结、重启恢复及发送前须匹配同一组合；其他版本不能沿用这里的字节结论。规则和文字是已批准协议固定内容，不是可调配置默认值。

**单事件编码。** 对§3.2已校验的完整客户端事件取确定性JSON：对象键按Unicode码点升序、数组顺序不变、无空白或尾换行，整数用十进制最短表示，bool／null用JSON字面量，字符串的引号与反斜线分别用双字节转义，U+0000–001F统一用六字节小写形式`\u00xx`，其余合法Unicode直接严格UTF-8编码。拒绝孤立surrogate；不修剪、归一化或变更字符串内容。保留字段缺失与null区别：两个事件身份键只保留实际选择的一个，不能为了编码改变原身份。sender在此候选的字段名为subject_id、display_name、role、identity_source，分别承载原主体标识、可空显示名、原场景角色和身份来源；UNKNOWN／HOST是下方最小例的明确合成值，不是自动补值。

令C为该完整事件JSON的UTF-8字节数，E约束C；传输请求的原有字节检查仍需通过。系统绑定／分配字段不混入客户端语义编码，而在以下材料头与逐项引用中单独计入。quotation的原文、原作者／原事件ID／发生时间，以及media中的全部已给理解／来源／状态、extensions等均留在事件JSON中并共同占E，不另展开或重复拼接；缺实际媒体能力仍按原规则拒绝。引用不触发额外原文查找、媒体解码或模型调用。规范事件JSON的UTF-8字节及各字段原文可完整恢复，不用内容hash代替正文。

Provider GENERATION的messages恰为两项，按SYSTEM、USER排列；角色枚举本身不计input_units，但下面所有text字节都计入。SYSTEM.text为下行ASCII加**一个LF**，无代码围栏、空格补齐或其他隐藏模板，共93字节：

```text
Synthetic records only. Decode each event. Use H and R as context. Return T references only.
```

USER.text固定从下面8行开始，顺序不得改变；每行以一个LF结束。占位符替换为冻结的真实内部ID，每项1–128个合法ASCII字符，不包含尖括号；无值不能填占位ID。它们分别为实例、宿主、平台、入口、批次、run和配置快照的原绑定：

```text
material=1
instance=<instance_id>
host=<host_id>
platform=<platform_id>
entry=<entry_id>
batch=<batch_id>
run=<run_id>
config=<config_snapshot_id>
```

随后按冻结history、target、recent顺序逐项追加一行，段内保持entry_seq升序；每项只出现一次，空辅助段不额外输出占位记录。行格式为“角色字母|message_id|entry_seq|received_at_utc|事件Base64＋LF”：角色恰为H／T／R之一；message_id最多128个合法ASCII字符；entry_seq为无前导零的正64位有符号整数，最多19位；系统received_at_utc为固定27字节UTC格式YYYY-MM-DDTHH:MM:SS.ffffffZ。事件编码采用标准Base64字母表、必要的=填充、无换行／空白，长度精确为4×ceil(C/3)。Base64和ID均不含分隔符|，时间和序号位置固定。最后追加字面量end及一个LF，共4字节；除此之外没有段标题、空行、对话历史或生成前缀。

这同时计入三段角色、稳定来源ID／序列、接收时间及全部报告引用；来源完整性摘要、原文修订等仍在冻结清单中逐项核验，不作为额外模型文本隐式拼入。材料的确定性字节定义为SYSTEM.text的UTF-8紧接USER.text的UTF-8，无额外连接符；角色及两项顺序已被固定版本绑定。合成参与者须可解码还原全部原事件和三段引用，再核对冻结清单；后续任何额外拼接或模板变化都须重新声明预算，不能继续声称此上界覆盖。

**完整上界账目。** 下表所有值是协议格式界限；对应实际编码器验证见[CURRENT_TASK](../work/CURRENT_TASK.md)：

| 项目 | ASCII／UTF-8字节上界及依据 |
| --- | --- |
| 固定模板 | SYSTEM.text=93 |
| 材料头 | material=1＋LF为11；7条ID行分别为138、134、138、135、135、133、136（字段名＋=＋128字节ID＋LF），合计960 |
| 材料尾 | end＋LF为4 |
| 每项引用与角色 | 角色1＋4个分隔符＋ID128＋序号19＋UTC时间27＋LF1=180 |
| 每项完整事件 | C≤E，Base64长度为4×ceil(C/3)≤4×ceil(E/3)；JSON自身的转义展开已计入C，并非原始正文长度E |

因此B=93＋960＋4=1057，W(E)=180＋4×ceil(E/3)，满历史窗口N=H＋T＋R时材料上界为B＋N×W(E)。现有模拟输入计量恰取这两项text的UTF-8总和，故Q(N,E)=1057＋N×[180＋4×ceil(E/3)]，不是W(E)=E，也不把编码前正文量当模型输入单位。messages项数恒为2，而不是N；USER内的多项记录不增加Provider消息数。实际少历史／少辅助只减少记录，完整冻结与固定T规则不变。

**最小事件可用性。** 下行只用于静态长度账目，空body且无媒体／感知内容时本身不得被接收；用一个x替换空正文即为本候选最小合法MESSAGE事件，客户端与sender绑定仍须获授权：

```json
{"body":"","client_event_key":"e","correlation":null,"event_kind":"MESSAGE","event_version":1,"extensions":{},"media":[],"occurred_at":null,"quotation":[],"sender":{"display_name":null,"identity_source":"HOST","role":"UNKNOWN","subject_id":"s"}}
```

空正文账目为245字节：顶层对象标点／键及除sender以外的值165，sender完整对象80；该行没有换行计入事件。加入x后为246字节，满足身份键唯一、主体来源明确、原始时间null及非空正文。推荐E下剩余正文空间及产品影响见[配置代入](configuration.md#runtime-compatible-budget-candidate)和[产品选择](../product/decisions-and-delivery.md#runtime-compatible-budget-impact)；不是“任何字段都可同时取最大值”的承诺。更长身份、真实时间、显示名或引用会占用同一E，超限仍拒绝，不能裁正文腾空间。

新增格式验证预期（实际证据及未执行变体见[当前任务](../work/CURRENT_TASK.md)）：逐字节核对上述模板／头尾与空正文账目；含引号、反斜线、控制字符、多字节Unicode、完整引用及合成媒体理解的事件可逆还原；C在Base64的三种余数边界长度符合公式；满历史窗口覆盖最大ID／序列／时间及E边界，无隐藏模板；版本、冻结来源或任意实际总长不符时拒绝发送，不借裁断放行。正常零／多合成结果、失败与敏感拒学继续沿用原参与者场景，不从该模板获得真实认知质量证明。

<a id="acceptance"></a>

## 11. 一次整体验收矩阵与证据要求

实施后按下表同时验收，不拆成“接口已写但恢复以后再补”的完成声明。证据标签：**S**为受控模拟／合成语义，**T**为实际服务及真实自有临时文件／SQLite，**P**为原进程确已退出后新解释器同资源恢复，**H**为实际只读HTTP／页面。标签表示所需证据；对应版本的实际覆盖及未执行变体只在[CURRENT_TASK](../work/CURRENT_TASK.md)记录。

| 场景 | 可判定断言 | 必需证据 |
| --- | --- | --- |
| 身份与入口隔离 | 改entry_id、宿主、scope、角色或伪造句柄先拒绝；同平台两入口独立序列／批次／游标；无权查询不泄露是否存在 | S、T、H |
| 重复投递与原确认 | 同键同内容并发／重启后仅一份事件、一序列、一回执；同键异正文／引用／媒体冲突；无外部ID须原client key；不同ID同正文是两事件 | T、P |
| 接收与模式竞争及派生审计 | §3.4首次、A／B并发、模式切换前后双序；原命令无预占seq，业务／审计／回执派生值相等。COMMIT后任何返回前退出，凭原输入／意图确认，handler及审计不再执行 | S屏障＋T、P |
| 三段推进／积压 | 正常、R=0、H=0、首次无历史、多个满批积压、已批准短／尾部策略；目标为最旧前缀且辅助紧邻，不取跨入口或未来输入 | S、T |
| 冻结后到达／持续超限 | 冻结三段／配置／来源摘要不变。保留产品超限反例作拒绝样本：该配置解析即整组拒绝；另验已批准相容配置的最坏窗口。不实现允许反例建库的未采纳分支 | S、T、P |
| 固定合成材料／预算候选 | 按§10.1逐字节验证模板、完整引用和可逆编码；最小合法事件确有正文空间，满历史与最大ID／序列不超材料／模拟profile及请求容量；严格版本匹配，不能以实际短样本冒充最坏上界证明 | S、T |
| 唯一执行者 | 同入口并发claim／冻结、旧代次迟到写、租约过期但owner活跃、线程cleanup_pending；始终无第二消费／发送者 | S屏障＋T、P |
| 成功含零结果 | 正式替身结果／来源、目标消费、新历史、引用／审计及回执一次原子提交；零结果仍成功，无假记忆 | S＋T |
| 普通失败与敏感拒学 | 普通失败留尾并标失败；明确学习敏感拒学不留尾、清旧历史；正文关键词／媒体拒绝不触发整轮拒学；保护最新辅助／后到／共享来源／其他入口 | S＋T |
| 候选／终结确认丢失 | COMMIT前／后各断点；原候选和稳定ID恢复，无再次模型调用、双轮转、双计数或重复引用释放；回执／必要审计坏数据拒绝成功 | T、P |
| 跨进程远程未知 | 登记后／模型开始后／结果到内存但未落盘各强杀窗口；PREPARED按UNKNOWN恢复，保留费用责任，恢复进程模拟器调用数为零 | S＋T、P |
| 交接及本地恢复 | 已持久结果但未候选、已候选未终结、终结回执未送达：本地恢复原产物且计量一次；无已持久结果则不重造 | T、P |
| 门控竞争 | 最后dispatch与切换／撤权两侧屏障；切换先则零start，start先则属于在途；普通后续attempt阻止，有效梦境可发，伪造DREAM字段不可发 | S＋T |
| 入梦／异常／重启 | 收尾上限、未决写、UNKNOWN、资源未退、FOCUSED无发布、发布已提交模式未提交；不会瞬间NORMAL或发布半份结果 | S＋T、P |
| 回流与新输入 | 梦境前正常消息先，暂存FIFO，再接尾新输入；最后一页与新接收双序竞争；引用无空窗；页确认丢失游标／计数一次；一入口卡住不挡另一入口 | S＋T、P |
| 配置身份、派生审计和兼容 | 配置COMMIT后任何返回前退出，原候选／意图恢复相同revision及snapshot；旧值与新事务分配值不能混填审计。同键异候选／actor冲突，缺行／摘要坏拒绝；原四解析入口不变，Provider版本None不伪造 | S＋T、P |
| 真实存储失败 | 实际临时SQLite被另一进程持写锁、只读资源、页上限触发实际SQLITE_FULL、真实文件I/O失败；失败／未知证据正确，无已确认接收丢失，无系统故障假轮转 | T、P；不填满宿主磁盘 |
| 分层初始化与清理 | 存储全回执校验中超时：无READY、无运行checkpoint复用，下次合法OPEN_EXISTING重做全检；Provider分页到期：已落盘收尾幂等、同实例可继续而跨进程重检；运行页已提交确认丢失：原页确认后才推进。各层任务迟到均保留槽位／owner／路径，损坏旧审计／引用不得因已有checkpoint而跳过 | S阻塞资源＋T、P |
| 观察权限与故障可见 | 无会话／错scope／写方法拒绝；专注仍读脱敏状态，故障显示STALE或UNAVAILABLE；无正文／SQL／路径／凭据／来源历史泄漏 | H＋T |
| 日志有界与元信息隔离 | 新feed不改旧双sink回执；仅其他入口有匹配记录时部分观察者空页has_more=false，无全局水位／计数。两类权限分别验覆盖、重启、cursor失效的字段白名单及授权先于gap；慢客户端不阻塞事务，无读取学习副作用 | S＋T、H |
| 格式与资源装配 | 旧程序生成的指纹1库由新实现旧装配打开，描述／原回执字节及确认保持；新组合库并存旧Provider指纹1和新命令指纹2。篡改派生审计／意图／回执之一，所有确认拒绝成功；未知版本、新旧装配不匹配拒绝无补表 | T、P |

实施验证须运行全量项目单元／集成测试及**全量Pyright**，覆盖`companion_memory/`、`tests/`及新增／未跟踪Python文件；若新增维护目录须纳入同一项目类型检查。按[代码规范](../CODING_STANDARDS.md#python-type-checking)记录版本、范围、实际命令、退出码及error／warning／information；Pyright不得代称Pylance。另按实际变更运行编译和离线锁文件一致性检查，未安装依赖或未执行的项目如实记录；不以本轮文档检查替代。

跨进程测试由父进程保留原database_id／配置装配输入，在明确屏障强杀子进程并确认退出，用新解释器OPEN_EXISTING，检查业务／回执／审计／引用及模拟调用计数；另做接收客户端未取得回执的重投。真实SQLITE_FULL须通过自有临时库的测试资源限制触发实际存储引擎失败，并记录平台／SQLite版本；异常注入另列，不替代真实失败。H使用真实HTTP处理和浏览器行为或等价协议客户端检查，测试会话与生产认证分列。

交付报告按最终受测文件版本列出S/T/P/H实际覆盖，说明未执行变体；不能从一次进程强杀外推介质掉电、Linux／Docker、生产卷、真实模型、生产鉴权或完整认知。生产性能、无限磁盘保证、完整日志历史／导出、编辑器Pylance均不在默认通过范围。

<a id="decisions"></a>

## 12. 集中批准决定与交付边界

以下五组推荐已由用户明确批准，并由用户手动转交授权连续定稿、实施与验证。下表替代方案未采纳；生产前置及额外管理能力仍未批准。无需审批私有文件拆分、SQL名称等常规实现细节。实施及技术审查状态只在CURRENT_TASK记录。

| 已批准决定组 | 采用方案及唯一细节 | 影响与未采纳替代 |
| --- | --- | --- |
| 批次触发、全部数值与超限 | [固定合成材料／模板](#synthetic-window-material)＋[确定参数候选](configuration.md#runtime-compatible-budget-candidate)：新库按material_contracts强制完整窗口相容；推荐E=1024、H/T/R=1/2/1、材料／输入8192、输出1024，短／尾部仍关闭 | 满历史上界依据唯一维护，实际编码及边界验证见CURRENT_TASK；[产品影响](../product/decisions-and-delivery.md#runtime-compatible-budget-impact)明确单条空间缩小、阈值触发更频繁、辅助各1条。原超限值单列反例，同库无配置解除入口；不选择可变目标前缀或自动截断／跳过／丢弃 |
| 模式收尾、分层恢复及故障解除 | [模式选择](../product/decisions-and-delivery.md#ingress-runtime-product-options)与[分层恢复](#runtime-startup-layers)：只收尾已启动调用和本地提交；发布确认后开放、入口独立回流；推荐不扩展存储初始化续进度 | 存储超时重做全检，Provider幂等恢复不等于运行checkpoint；持久FAULTED／UNKNOWN及清理占用无自动解除。存储校验长期超预算可持续不就绪；若必须解除须批准具体基础扩展／配置或管理方案，不能以重启／延长外层期限绕过 |
| 最小持久配置身份 | [配置§11.14](configuration.md#runtime-configuration-identity)：初始化一次、重启完整恢复；配置分配revision／snapshot的审计采用第5组结果绑定增量 | 保留副作用前原候选／key／意图，确认丢失能查原身份；不能预占序号或改用假ID。Provider仍保留原无版本模拟证据；无在线修改、热激活或已阻塞输入自动修复 |
| 参与者与最小观察 | [§9](#observation)、[§10](#participants)及[日志游标权限](logging.md#runtime-log-cursor-privacy)：实际服务、模拟模型、合成业务参与者、只读HTTP测试会话；内部全局位置不透明，额外全局元信息须独立授权 | 部分入口观察者的events、has_more、健康及gap均投影，缺口不能借全局差值假称授权数据丢失；保守未知连续性有局限。真实业务、完整日志历史、生产认证仍需扩大前置，不能从测试会话获批 |
| 审计基础增量、兼容与生产前置 | [持久化精确增量](persistence-and-transactions.md#runtime-result-bound-audit)与[日志物化端口](logging.md#runtime-derived-audit-draft)：新命令冻结完整意图和结果映射、指纹2、同事务物化及原结果关联校验；旧命令／回执／Provider指纹1不变。自有临时新完整装配，无自动补表／迁移 | 这是已批准的必要公开基础扩展，不能仅复用旧接口声称可实现动态审计。生产路径、G2、鉴权、身份保留、存量迁移仍依[具体前置](persistence-and-transactions.md#runtime-production-prerequisites)，本轮不选择 |

实际验证与验收版本见[STATUS](../work/STATUS.md)，不能将某一基础模块的历史测试外推为其他装配通过。执行授权、文档维护及停止点按[CURRENT_TASK](../work/CURRENT_TASK.md)和[项目分工](../../AGENTS.md)管理。
