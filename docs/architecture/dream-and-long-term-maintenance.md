# 梦境整理、长期维护与周期 persona

**批准状态：**2026-09-16用户批准完整阶段，并分别批准下列四项产品选择；必要的真实模型请求已预授权，不以费用金额作为本阶段发送门槛。本文由监督者在该范围内定稿，执行者连续完成整阶段实现、自查、测试及范围内修复；新增产品决定仍交用户判断。实施、验证与停止点只在[CURRENT_TASK](../work/CURRENT_TASK.md)维护。

<a id="decisions"></a>

## 1. 产品选择与交付边界

| 已批准事项 | 本组合行为 | 边界 |
| --- | --- | --- |
| 梦境默认触发 | 每天实例时区03:00一次专注梦境，支持手动和非专注；漏过多日只补一个有预算的运行，重启不自动补发模型 | 自动调度启用；不连续追赶所有错过日期 |
| 周期 persona | 本地结构／来源校验及独立监管模型检查通过后自动原子发布；延续原已批准 Linux persona 对本阶段合成新空实例的原文导入例外 | 自动检查不冒充用户人工批准；自由文本替换首次persona仍禁止 |
| 时间衰减 | 仅保留强度每满24小时减1，每对象每次运行至多减7，余下时间债保留；相信程度不自动降低 | 本组合启用；沿用既有遗忘20、恢复35、连续遗忘30天删除 |
| 受控管理例外 | 可信管理身份可暂停／续办／中止指定run，核验revision与epoch；提交未知、实际资源未结束或远程UNKNOWN不能强行解锁 | 增加窄的管理写端口，不开放通用FAULTED解除、配置修改或UNKNOWN结案 |

交付一套可持续运行的“日常认知→增量维护／梦境→稳定persona→有序回流”宿主，包含必要配置、端口、持久化、模型交接、权限、观察、恢复及测试。专注与非专注是同一协调器的运行方式，不另建两套认知系统，不按表或内部机制拆成审批阶段。

继承[梦境产品](../product/dream.md)、[自我与persona](../product/self-and-persona.md)、[生命周期](../product/lifecycle.md)和[所有权](ownership.md)。复用[日常宿主](daily-cognition-and-image-learning.md)、[固定释放计划](formal-memory-source-media.md#source-release)、[模式及回流](durable-ingress-and-batch-runtime.md#modes)、[信息反馈](local-information-feedback.md)、[语义索引](async-embedding-semantic-retrieval.md)和[Provider](provider.md)；本文只增加梦境完整整理及其所需的受限权限。

不包含完整管理Web、生产鉴权／G2、在线配置激活、存量迁移、备份升级、外部行动执行、音视频测试、rerank、大档资格或质量调优。[质量暂缓](../work/DEFERRED_ISSUES.md)继续有效；无效模型输出必须严格拒绝，不能为了宣称真实闭环而降低来源、协议或原子性保障。

<a id="integration"></a>

## 2. 实际复用点与装配

| 既有实现 | 必要增量及边界 |
| --- | --- |
| [日常装配](../../companion_memory/runtime/daily_assembly.py)、[宿主](../../companion_memory/runtime/daily_host.py) | 新梦境能力与原日常公开链路运行在同一数据库／实例、Provider和资源管理中；复用领域端口，不复制完整旧宿主 |
| [模式](../../companion_memory/runtime/content_modes.py)、[首次persona模式](../../companion_memory/runtime/daily_persona_mode.py) | 现有日常模式只支持首次persona，不假定已支持重复入梦；新装配显式区分首次建立、常规梦境、原确认与回流 |
| [对象维护](../../companion_memory/memory/maintenance.py)、[受控维护](../../companion_memory/memory/maintenance_access.py) | 普通维护原门控保持；增加绑定run／step的内部维护能力，短事务合并真实业务效果和步骤确认 |
| [依赖脏项](../../companion_memory/memory/repository.py)、[到期扫描](../../companion_memory/information/expiry.py) | 增量游标、因果去重与时间锚点；复用已有到期删除，不启动第二个无协调的删除器 |
| [当前persona](../../companion_memory/self_model/daily_current.py) | 新装配统一当前发布指针；首次导入／生成与周期发布由同一公开读取能力解释，不保留两个互不一致的“当前” |
| Provider原请求／结果所有者、语义dirty与索引发布 | 整理、摘要生成、独立监管分别记账；对象修订、遗忘、恢复、删除仍驱动原生索引，不借梦境另造向量库 |

新generation角色闭集只对新装配增加DREAM_REVIEW、PERSONA_DREAM和PERSONA_REVIEW，不修改旧角色集合或冒充首次PERSONA。新增独立静态装配格式`DREAM_MAINTENANCE_V1`，新库显式CREATE_NEW，之后按同一声明OPEN_EXISTING；旧装配、旧命令描述字节、指纹、配置与原键行为保持。不原位迁移旧库，不通过伪造旧类型、放宽旧精确身份检查或运行时补表取得兼容。实例初始化继续使用已确认的配置发布、业务根意图及可确认的分步骤初始化；增加的实际owner有真实根元数据，不制造业务占位对象或空targets审计。

物理表／索引／命令数由实现完整Schema生成清单证明，**不把尚未编码的估算数量定为产品闭集**。逻辑状态、字段意义、owner、命令权限和容量约束以本文为准；创建库前必须固定全部分支和Schema，禁止动态SQL或运行中追加命令。字段分表、索引和有类型端口拆分由执行者在范围内完成。

<a id="runtime"></a>

## 3. 调度、模式和公开端口

统一可信端口：`start_dream`、`inspect_dream`、`confirm_dream_step`、`close`；已批准的管理例外增加`pause_dream`、`resume_dream`、`abort_dream`。变更输入固定原操作键、scope、run_id、expected_revision、mode_epoch；运行创建另含FOCUSED／BACKGROUND及MANUAL／SCHEDULED原因，不能由普通宿主或模型自行签发权限。查阅和原确认不发送。封套分别表达本地提交确认、远程结果、清理占用、业务完成与待续办工作。沿用固定code／operation／field／reason，梦境特有原因限定RUN_ACTIVE、RUN_NOT_RESUMABLE、WORK_DEFERRED、CLOCK_REGRESSED、PERSONA_REVIEW_REJECTED、PERSONA_UNCHANGED；冲突、权限、容量、存储及Provider原因保持原分类。非法形状和非法状态组合分开，不泄漏底层异常或依靠assert校验。

运行状态闭集：PREPARING、RUNNING、PAUSING、PAUSED、FINALIZING、DRAINING、COMPLETED、ABORTED、FAILED、RECOVERY_REQUIRED。COMPLETED仅表示该次有预算的运行结束，另报remaining_work／coverage，不冒充全库已整理。已有步骤提交不可回滚成“从未执行”；ABORTED保留完成步骤。UNKNOWN是独立事实，不用某个run状态覆盖它。

默认触发按§1的已批准值。时区取已持久配置，首次默认遵守[环境时区决定](../product/operations-and-management.md#initial-default-timezone)。按实例／本地日期／调度版本去重；夏令时重复时刻只选第一次，跳过的时刻取当日随后第一个有效时刻；跨多日不排出无限补跑队列。已有run未终结或仍在回流，不创建第二个梦境。跨重启只恢复本地事实，新的远程请求须在可信管理端显式恢复运行之后重新准入；新实例首次实际启动也必须显式开启，配置enabled本身不触发模型。恢复开关不重试已发送或UNKNOWN的原请求。

专注入场先关闭新的普通派发，已消费许可的调用仅按原身份收尾；未发送工作停放，新消息进入原入口梦境序列。实际在途资源、原提交及Provider交接未安定时，不声称进入可写认知的FOCUSED。内部梦境许可由runtime原生签发，绑定库、run、step、epoch、owner和用途；不能靠`role=DREAM`字符串越过门控。门控覆盖普通学习、媒体、目标判断、embedding和所有非例外业务；只读状态与原始接收保持原例外。

非专注运行不切换业务门控、不改输入去向。材料冻结及末端提交核验对象／来源／当前persona修订；竞争返回有类型冲突，不用旧候选覆盖新事实。该项保留待重新规划，下次新工作可重新读取，已发送原请求仅确认。两种方式共用有限资源与公平调度，不能由梦境独占网络线程直到普通业务饿死。

专注结束的原子证据允许`PUBLISHED_NEW`、`KEPT_PREVIOUS`、`NO_PERSONA_CHANGE`、`ABORTED_SAFELY`，不能为退出伪造一份persona发布。只有新发布才写self_model；其余由dream保存真实完成／处置记录，runtime确认门控转换，同UoW绑定必要审计。确认后进入DRAINING并恢复原允许的业务；每入口旧积压先于新输入，移交完成才解除暂存引用。缓存清空不等于学习完成，不强制冲刷尾段。

暂停禁止新步骤和发送，已开始步骤只合法收尾；续办不重置绝对期限或已消费次数。中止在可靠确认本地处置且实际清理结束后可保留旧persona退出；存储损坏、无法证明提交、真实资源未结束或远程UNKNOWN时保持RECOVERY_REQUIRED及必要门控。管理例外只对本原生梦境状态生效，不解除其他故障或修改旧格式。

<a id="maintenance"></a>

## 4. 增量维护与来源影响

### 4.1 时间、遗忘和删除

时间衰减按§1的已批准值启用；正常运行和梦境不能各自独立扣同一时间段。memory拥有每对象的`accounted_until`、实际使用锚点及revision。初次基准为创建时刻，之后从上次已结算衰减的锚点按UTC绝对24小时计算完整区间，单次上限后的时间债不丢弃。有效使用继续给予原保留增益，但不抵销已有时间债；读取、使用和正文修改均不直接把衰减锚点前移。生效分数、时间锚点、对象revision、生命周期转换、dirty、历史及梦境步骤确认同UoW提交。旧键确认不重复扣分；有效反馈竞争须重查，普通读取不改变锚点。时钟倒退不扣分也不把锚点回退，记录时钟异常；恢复不能仅凭新的墙钟重算已完成效果。

自动衰减只改retention，不改belief；阈值、持续遗忘起点和原使用增益沿所选日常组合。删除使用原到期索引及固定释放计划：复核连续遗忘时长／revision，原键确认后才允许重规划，共享来源、主体来源及媒体引用仍保留。到期工作使用持久到期索引及分页游标，不每天全库装载再筛选。dream与正常到期扫描使用统一领取／原键策略；模型无权缩短保留期。模型可提出对象修正或保留评价，但本阶段不增加“模型立即永久删除”入口，永久删除仍是现行到期规则。

### 4.2 影响工作和防循环

memory的当前依赖、内容修订及遗忘／恢复／删除事实是权威；dream仅保存领取与处理进度。新装配须提供稳定单调变更序列，事件由真实对象变化同UoW产生，字段含origin_event_id、changed_object_id、revision、reason、cause_root、sequence。同一事件的下游枚举以固定范围／键游标分页，不能只遍历当时第一页，也不能把旧只含PENDING的记录假装已有完整确认协议。

影响效果唯一键为原事件／被影响对象／效果种类，并保存实际读取修订和因果根。同一支持丧失事件至多一次扣减，后续时间衰减独立；循环图用持久已访问关系和因果根去重，不把本步骤新产生的同根事件无限当作新证据。不同独立新证据仍可触发后续评价。支持恢复后只重新评估，不机械把过去扣分全部加回，也不从审计复活旧正文。

原来源遗忘／删除表示该支持当前不可用，不直接证明命题为假。相同根输入、persona复述和自述不算独立依据；剩余独立支持必须保留。工作结果闭集为APPLIED、UNCHANGED、DEFERRED_CAPACITY、DEFERRED_CONFLICT、FAILED、RECOVERY_REQUIRED；延期不等于已处理完，分页扫描要有公平续办位置，不能被一个超限对象永久挡住其他对象。不得丢失延期原因或以重复写检查点消耗空闲资源。

### 4.3 模型整理与候选

每次冻结至多一个待评对象、其至多8个当前依据对象、受授权完整来源及当前persona；原始普通／梦境队列、审计历史、其他入口未公开缓存、配置秘密不进入材料。完整来源按当前权限返回，不截断为“完整”；所需材料超限时延期并记录范围，不能以缺失依据的材料批准有损变更。远程请求在事务外，固定材料／Schema／配置摘要、原请求和结果交接保存后才形成候选。

整理输出仅为`{schema_version:1,decision:KEEP/CHANGE/DEFER,reason,actions}`，reason≤512字节，actions为0..8项。KEEP／DEFER必须为空；CHANGE非空。动作仅复用日常的CREATE_MEMORY、REPLACE_CURRENT、SET_SCORES、CREATE_RELATION、CREATE_GOAL语义，SET_SCORES的每次delta仍为−10..10；不含REGISTER_SUBJECT、外部当前状态、目标完成／改期、配置或persona写入。所有主体必须已登记，期限有真实来源才可提议。未知主体延期，不在梦境从旧昵称猜测平台身份。

梦境候选采用独立origin，不伪造批次TARGET、外部输入或实际经历。动作共同依据是冻结正式对象／revision及其合法source绑定；新对象、关系和目标通过memory／goals原生参与者保存真实来源关系，不能让cognition直接写他方表。一个候选的业务效果、来源取得／释放、必要旧值历史、dirty和step确认原子提交；固定分支按实际writer集合选择，零效果只写dream真实处置，不制造memory、goals或logging历史占位。共享引用竞争遵守既有固定释放协议。

<a id="persona"></a>

## 5. 自我视图、周期persona与监管

self_model只拥有派生视图、候选／监管结果和发布指针，自我事实继续存memory。视图读取当前SELF相关对象，保留独立来源根、revision、世界及外部设定身份；扫描分页有界，不给自我认知总数量另设业务上限。发布摘要不声称覆盖全部自我事实，保存实际选用范围、扫描水位、未覆盖／待复核原因。候选选择兼顾当前发布依赖复核与新变化的公平轮转，不能永久只读最早几项。

每次候选基于当前已发布摘要、至多16项当前自我对象和它们所需的合法依据；旧persona明确是派生摘要，不作为独立证据。已删除／遗忘／修正的旧依赖须标为不可沿用或重新取证；过时正文只能从当前合法事实纠正，不能读取审计填补。外部监管目标和prompt以配置摘要绑定，模型不可自改；临时活动／情绪不覆盖稳定身份，也不能凭同名、角色扮演或虚构来源制造现实经历。

生成请求与监管请求为两个独立Provider调用。候选封套固定`{schema_version:1,text,basis_refs,change_reason}`：text≤6144 UTF-8字节，basis_refs为0..16项已提供的object_id／revision，change_reason≤512。监管看到完整原persona、完整候选、依据和外部规则，返回`{schema_version:1,decision:APPROVE/REJECT/UNCHANGED,reason}`，reason≤1024。重复键、尾随内容、截断、额外字段、越界引用均拒绝，不修补JSON。UNCHANGED不发布新正文；REJECT或任一步失败保留旧版，不立即重问模型。

用户已批准周期候选自动检查发布，不逐份申请人工审核。自动策略必须通过确定性来源／格式／范围校验及独立监管，记录MODEL_REVIEWED，不能写成人工APPROVED。发布核验run、候选／监管的原Provider证据、旧指针revision、配置和所有选用依据修订；self_model新发布／指针、dream步骤及必要审计同UoW提交。没有memory变化不声明memory writer；第一份初始化仍按原严格协议。只读回复、查询和HTTP取得同一个当前指针，不能继续返回导入时的固定原文。

周期发布不因普通查询即时重跑；来源变化只写待复核元信息。候选／视图的持有不阻止合法业务删除，不把审核工作变成永久保留所有来源的理由；发生竞争使候选失效，原已发布persona按既有可见滞后规则保持。历史persona正文仍限审计权限。连续两轮更新、无变化、拒绝、过期和崩溃均必须可解释。

本阶段合成新空实例的指定原文导入已按§1批准延续例外；复用[原persona证据和完整身份](daily-cognition-and-image-learning.md#persona-import)，不改正文、不补造新模型请求、不导入任意自由文本，也不在已有实例覆盖当前发布。

<a id="persistence"></a>

## 6. 记录、事务和恢复

下表是逻辑记录契约；具体类型必须封闭，不接受任意JSON扩展。公共ID遵守原identifier规则，revision／sequence／UTC微秒为非负63位整数，状态和reason均闭集。每份恢复正文≤8192字节；候选text与引用清单必要时分叶保存，不能要求完整16384字节候选强塞单叶。材料上限与profile真实输入能力不相容时配置整组拒绝，不让运行时自动减字段。列表分页，不把整个run历史装入一个root。

| owner／记录 | 必要身份、内容和约束 |
| --- | --- |
| dream／schedule与run | scope、schedule_revision、本地调度日期、run_id、FOCUSED/BACKGROUND、状态／revision、原请求键、配置身份、mode_epoch、原绝对期限、页游标、预算已用、完成／延期计数、结束原因、当前persona结果引用；同实例至多一个未终结run |
| dream／step与领取 | run／step／ordinal、类型、原影响或时间段、受控许可摘要、冻结对象／来源／修订、材料及候选引用、固定原请求、Provider request／handoff、原执行键／计划、状态、结果摘要；唯一领取与确认可跨进程重建 |
| memory／维护时间及影响 | 对象时间锚点、有效使用修订、原影响事件／因果根／单调序列、依赖枚举进度、已应用效果唯一标识；与实际对象变化同UoW保持一致，不由dream直接写表 |
| cognition／整理材料与候选 | 原生DREAM origin、只读许可、完整冻结材料及摘要、输出版本、Provider结果绑定、合法变更叶／引用映射；不得挂接虚构batch/source |
| self_model／视图、候选、监管与发布 | 原run／step、当前发布身份及revision、实际依据清单／水位、目标／监管摘要、生成与监管两个原请求、候选摘要、决定／原因、新旧指针、发布出处；不可伪造人工批准 |
| runtime／模式与初始化 | 原模式epoch、原生梦境许可、真实完成／退出证据、初始化意图及原回执关系；scope／库绑定；不扩权普通宿主 |

完整命令族在创建库前声明：初始化各owner真实根；创建／暂停／续办／中止run；领取／冻结／绑定原模型意图；保存整理候选或安全失败；原子应用维护／来源／目标及步骤；保存／检查／发布周期persona；结束并转换模式／回流；安全清理已结束临时资源。Provider仍用其原登记、准入、结果与确认命令，dream不能代写账本。每个命令完整输入、结果、必要owner、targets、审计结果绑定及错误闭集形成机器可读清单，随实现交付。

所有实际业务writer各有必要审计；只读确认和零业务变化不能占位。对象创建无旧值历史，修改／删除才记录实际旧值。初始化只写已有owner的真实元数据，不为了凑writer创建空记忆／目标。同一owner多处变化合为明确的非空targets，超过上限按预先声明的真实分支分步骤，不在运行中临时拼审计集合。

远程提交登记、结果落盘、候选应用、persona发布、模式退出、引用释放分别原键恢复；本地确认只使用原不可变输入。启动、读取、重开、原键核账零发送；持久结果已知但未应用时只补本地原事务。可靠NOT_SENT且旧任务结束时可由显式续办准入原未发送工作；已发送失败、协议拒绝或UNKNOWN不能换键伪装未发送。

存储确认未明、远程结果未知、实际清理待完成分别暴露。底层操作结束通知必须绑定本次操作并原子登记，不等待全服务的读／写计数归零；公开超时有界返回，原任务／槽／句柄保持到真实结束。暂停／关闭不取消他方已取得所有权的连接，不用sleep忙等假装回收，也不启动迟到准备的新写入。永久阻塞如实保留实际占用。

已结束的进程内Task／能力／观察记录限容；持久步骤只在原结果、业务出处和恢复所需最小证据保留后回收可释放材料。业务审计按原保留规则，不能以TTL删除未决责任。依赖影响重复确认、run复开、空扫描都不得递归新增检查点。

<a id="configuration"></a>

## 7. 配置、材料与资源

旧组合的130键／6域及各自格式不改；新装配继承原键并对本节列明的字段采用新版本，在原`daily_cognition`域增加下列6个必填object键，共136键／6域。均required=true、nullable=false、NoDefault()、初始化生效、普通模型只读；表中数值是本组合显式候选，不是解析器隐式默认。§1的产品取值已固定。各字段严格类型、未知字段拒绝，owner按键名，不能注册无人读取的开关。

| 键 | 封闭字段及本组合值 |
| --- | --- |
| dream.schedule | enabled=true；local_time="03:00"；focus_default=true；tick_ms=1000；missed_policy="ONE_BOUNDED_RUN"；resume_policy="EXPLICIT_AFTER_OPEN" |
| dream.resources | run_timeout_ms=1200000；step_timeout_ms=180000；operation_timeout_ms=10000；close_timeout_ms=10000；page_size=16；objects_per_run=256；dependency_edges_per_run=1024；model_calls_per_run=8；active_runs=1；active_steps=1；completed_observations=128 |
| memory.long_term_maintenance | decay_enabled=true；interval_seconds=86400；retention_decrement=1；max_intervals_per_object_run=7；clock_policy="NO_REWIND"；time_basis="CREATED_OR_ACCOUNTED_TIME"；delete_policy="EXISTING_FORGOTTEN_EXPIRY" |
| self_model.periodic_persona | publication_policy="LOCAL_AND_INDEPENDENT_MODEL_REVIEW"；text_max_bytes=6144；basis_limit=16；material_max_bytes=262144；output_max_bytes=16384；change_reason_max_bytes=512；review_reason_max_bytes=1024；policy_source="self_model.initial_persona"（读取该唯一配置中的generation_goal与supervision_prompt，不复制第二份权威值） |
| provider.dream_profiles | 恰DREAM_REVIEW、PERSONA_DREAM、PERSONA_REVIEW三个字段，各为{profile_id,max_output_tokens:4096,attempt_limit:1}；profile_id绑定显式generation profile，account／协议／凭据引用由该profile确定；不内含密钥 |
| dream.management | control_enabled=true；scope_policy="BOUND_TRUSTED_ADMIN"；conflict_policy="KEEP_ORIGINAL_AND_DEFER"；unknown_policy="NO_FORCE_RELEASE_OR_RESEND" |

新增字段的固定推荐数值在本组合采用；调度／衰减／控制开关只接受bool，时间为严格HH:MM，profile_id为原ID，其余固定策略为表中单值枚举。原自我目标和监管文本继续执行其既有1024字节限制。

本组合继承键的明确增量：`provider.profiles`与`provider.role_profiles`增加三个新generation用途（原6角色变9角色）；`provider.generation`和`provider.transport`的角色资源由4项变7项，新增角色各有独立prompt／Schema引用和摘要，所有映射须一致。`cognition.text_context`使用新的context_version=3，persona_projection_max_bytes由2048变8192，其余叶／材料总限保持；只改新装配验证器，不放宽旧精确类型。日常材料、读取端口、查询投影及周期监管同步支持完整新persona，完整公开投影≤8192，不把6144字节摘要再塞进旧2048编码器；投影含真实来源类别及监管状态，完整依据清单仍走受限端口。新梦境材料／候选另有独立版本和origin，旧日常候选语义不扩权。以上是字段分支扩展，不新增其他配置键。

以上新增object单值完整编码≤8192，配置总量仍≤524288；日常域每域条目上限128须实际校验。静态装配上限8MiB、唯一配置初始化命令2MiB、普通命令1MiB、必要证据32768、原回执与叶限保持。材料不放进事务大字符串：完整材料≤262144，采用已确认的分片引用；请求完整body≤1048576，输出按角色完整封套校验，profile输入能力须覆盖实际编码、SYSTEM、Schema和封套余量。

1个对象、8个依据对象和16项自我视图是读取／请求预算，不是全库业务上限。最坏合法完整source、原persona、新候选和监管材料必须分别实际编码；组合超限返回明确延期／能力原因，绝不静默截断依据或声称穷尽。验收同时证明在本阶段冻结的正常测试材料上可以持续完成，不以“所有工作都延期”交付。

运行资源沿用日常共享网络worker=1、等待队列=0、真实请求间隔至少30秒和已批准小档。梦境model_calls_per_run计入生成及监管，余下公平预算不足则下次运行继续；不把普通embedding和目标判断偷偷记到免费内部调用。模型调用期间不持有数据库写事务，索引／文件I/O各保留自己的实际占用。磁盘至少2GiB可用预留，不申请800GiB，不删除用户数据或原证据腾空间。

<a id="observation"></a>

## 8. 观察及管理隔离

只读HTTP增加dream、maintenance、persona三个命名分区：当前run／模式／epoch、实际步骤／预算／积压、延期原因、已用及剩余时间债、persona生成／发布时点及待复核状态、Provider本地确认／UNKNOWN／清理；原查询version2、票据与权限保持。不能通过计数、游标、错误内容泄露无权查看的入口或审计正文，不平铺覆盖其他owner字段。

原始模型材料、候选全文、监管全文和历史persona只由既有对应权限端读取，不因只读页面存在就公开。按已批准管理例外提供可信宿主绑定端口及受控HTTP测试入口；无公共自助提权、任意scope、通用SQL／文件读取、强制解锁按钮。生产管理鉴权和完整Web仍留下一完整交付。

<a id="live"></a>

## 9. 真实验证预授权与停止规则

用户已预批准本阶段必要真实请求，无须再次申请供应商调用或费用上限。执行者使用已准备的非秘密profile信息和受保护凭据引用，经Provider发送；监督者不读取密钥、不调用模型。实际usage如实记录，费用与扣额无依据时保持未知，不能以¥0、模拟usage或绕开Provider替代。

既有授权的出站范围在此精确列明，供用户手动转交及正常工具审批核对；不新增供应商、数据种类或调用额度：

| HTTPS POST目的地 | 允许发送的内容与用途 |
| --- | --- |
| `https://api.deepseek.com/chat/completions` | 下表已审合成材料、已批准原文导入的persona，以及它们在本实例内形成的必要来源、候选、监管材料和确定性身份／时间封套；包含获准的运行时prompt／Schema。用途仅为整理、persona生成、独立监管、日常学习兼容及目标判断 |
| `https://ark.cn-beijing.volces.com/api/coding/v3/embeddings` | 上述合成材料形成且符合原生准入的记忆文档、下文两条冻结查询及相应embedding协议封套；用途仅为DOCUMENT与QUERY |

凭据只由执行者经受保护引用解析，并作为认证信息发送至其所属供应商；不放进业务正文或日志。上述范围不包含用户聊天、工作区任意文件或项目秘密，不授权额外目的地。自动审批若要求具体目的地及数据范围，随原命令提交本节和用户手动转交的明确指令；监督的范围核对不等于工具审批已通过，不通过更换工具、脚本或目的地绕过拒绝。

为防无限试错，首个整阶段计划分配**最多32个已登记attempt**：整理6、persona生成4、独立监管4、日常学习兼容2、目标判断2、DOCUMENT12、QUERY2；无需要的不发送，不跨用途挪用。该数是本次工程执行上界，不是新费用门槛。旧日常包的29个停止槽及历史失败不复活，新建阶段身份与逐用途账目，继承同账户未决请求阻止规则，不能利用新包绕过UNKNOWN。

真实材料采用小规模明确合成数据，通过公开接口建立，保存来源及预期。监督已按下表审核本阶段语义种子；ID、UTC时点、revision及封套由测试资源确定性生成，不改变正文含义。复用公开固定集／原生合成候选参与端口，不新增绕过日常学习的通用生产写入口，不直接写SQL，也不伪造真实模型出处。

| 种子正文／场景 | 已审来源与预期 |
| --- | --- |
| “合成参与者青禾说：我偏好在安静的地方阅读。”及“青禾自述偏好安静阅读。” | 同一原始事件的两个对象，共享来源；不能算两份独立支持 |
| “合成观察记录：青禾在另一日选择安静的阅览室阅读。” | 第二个独立合成根，只按其实际观察范围提供支持 |
| “安静的阅读环境可能适合青禾。” | 依赖上述根的有限推论；删除或遗忘一个根不机械删除仍有支持的推论 |
| “这是一条等待维护的合成旧提醒说明。” | 用受控时钟验证衰减／遗忘／恢复／到期；不得混入真实当前状态或声称已执行提醒 |
| “合成试验中的表达应明确区分观察、转述与推测。” | SELF来源保持外部合成设定，不增加真实经历；persona不可把自己旧摘要当第二根 |
| “虚构故事里的角色苔灯可以飞行。” | 明确独立虚构世界，不作为现实SELF能力，不进入现实persona |
| “合成参与者希望在明确记录的将来时刻整理资料，尚未表示开始或完成。” | 目标建议有源，不能写外部当前状态或把目标自动标为完成；时点在冻结材料中明确给出 |

循环依赖、共享holder竞争、到期及非法响应主要由模拟适配器和实际临时存储验证，不为了覆盖矩阵逐个消耗真实槽。两条真实QUERY固定为“青禾适合怎样的阅读环境？”和“苔灯在现实中会飞吗？”，后者不得把虚构能力作为现实事实；评分按实际结果记录，质量不足不阻塞工程。

材料、prompt／Schema、profile、代码指纹、数据身份及用途在发送前冻结于执行者JSON清单；实现可编码上述已审语义而不再次申请材料批准。只用本表及已有获准合成资料，不外发用户聊天或项目秘密。新增人物事实、修改已审语义、引入新产品选择或须扩大权限时，交监督或用户集中判断；继续不受影响的工程。

协议错误、供应商拒绝、超限、远程UNKNOWN或账目／清理无法确认时，先持久停止对应受控包，原请求绝不修补结果、换键重发或自动修复模型输出。已知本地编码／协议适配缺陷可在离线修复和定点回归后，建立新的派生包使用**尚未消耗且未被其他活跃包占用的同用途槽**继续不同已冻结工作：本阶段最多两份派生包，父包停发证据、剩余槽转移及新版本先原子登记；原工作及已登记attempt不退还，不以该机制重试已失败学习。UNKNOWN或实际在途未清理时不启用派生包。没有可证明本地缺陷的纯模型格式失败保留为真实验证缺口，不反复换prompt刷成功。

到达上界或真实协议无法继续，只停止相关真实验证，仍完成受控工程、恢复、核账和全部独立矩阵；不把一次失败当作整个工程未完成，也不把模拟／合成结果写成真实成功。恢复、原键确认及已命中缓存验证新增发送必须为0。质量低按已批准暂缓处理，非法结果未入库与真实链路是否完成仍分别验收。

<a id="acceptance"></a>

## 10. 连续实施与集中验收

以下是同一阶段内部顺序，**不是新的逐步审批点**：

1. 核对基线、既有保留改动及资源，完成新完整配置／Schema／命令／初始化／兼容，生成实际容量清单。
2. 接通同一宿主的调度、模式、增量时间维护和来源影响，复用原子来源释放及索引；完成两种梦境与回流。
3. 接通自我视图、独立生成／监管、原子发布与统一当前指针，补齐管理、观察、原键恢复及清理。
4. 集中自查并完成定点／关联回归，随后按预授权执行必要真实验证；工程缺陷一次收敛处理，最后全量Pyright及完整文件指纹交付。

| 组 | 必须有的实际断言 |
| --- | --- |
| 调度与时间 | 手动／定时、时区／夏令时、漏跑合并、重复tick、重启先零发送、同实例唯一run |
| 专注准入 | 进入与已发送／未发送学习、媒体、目标、embedding竞争；原始接收不丢；普通业务拒绝、只读观察可用 |
| 非专注协调 | 普通业务保持开放；模型等待期间对象／来源／persona变更使旧候选不能覆盖 |
| 时间衰减 | 整24小时、分数边界、单次上限／余债、实际使用竞争、时钟倒退、重复确认及跨进程不重复扣分 |
| 遗忘／恢复／删除 | 双阈值、30天边界、有效反馈竞争、共享source及SUBJECT／媒体holder、旧计划确认后重规划 |
| 来源影响 | 改写／遗忘／删除／恢复、两个独立根、同根persona重复、循环及分页超过一页、延期公平续办 |
| 候选与权限 | 五类合法动作、空结果、错误世界／主体／revision／来源、批次伪造、越权状态／删除／目标执行全拒绝 |
| 原子提交 | 各实际owner、非空targets、必要审计、零业务写、失败原子性；真实SQLITE_FULL／只读／锁竞争及I/O失败 |
| persona | 初始与周期同一指针、连续两轮、独立监管、拒绝／无变化、来源变更、历史权限、无半份发布 |
| 中止与回流 | 合法管理身份、pause／resume／abort竞争、KEEP_PREVIOUS安全退出、各入口FIFO及新消息不能越过积压 |
| 运行恢复 | 模型登记前后、发送后、结果持久后、应用／发布／退出确认未知、原进程结束后新解释器恢复；新增发送0 |
| 资源所有权 | 迟到任务不能启动新副作用、完成通知竞争、Task／许可／句柄有界回收、close有界但保留真实占用 |
| 查询／索引／HTTP | 修订／遗忘／恢复／删除对词法及向量生效、版本2兼容、观察分区／权限／游标、慢连接隔离 |
| 完整配置与容量 | 所有注册键实际消费、完整候选及最大合法封套实物编码／存储、声明字节、初始化中断续办、旧装配原键及重开 |
| 真实调用与归因 | 逐用途原账本、真实／模拟明确、协议错误停止、派生包无超额或旧槽复活、恢复／缓存零发送、费用未知保持未知 |
| 阶段集成 | 同一宿主日常→专注→回流→非专注→第二次persona→关闭／重开；模拟适配器完整成功路径与真实执行范围分别给证据 |

采用[单端Docker Linux和定点验证规则](../CODING_STANDARDS.md#validation-environment)，完整类型检查按[Pyright规范](../CODING_STANDARDS.md#python-type-checking)。不机械重跑全部unittest；涉及的旧兼容、门控／原键／清理和完整公开链路必须保留关联回归。最终全量Pyright覆盖新增文件，不能靠Any／排除路径弱化检查；Pylance单独说明。

执行者不改项目文档、AGENTS或参考资料；交付自有临时目录JSON索引、实际环境、命令及退出码、失败和成功原始日志、断言映射、全部差异分类、逐用途真实核账、受测文件清单与收尾复核。清单聚合沿路径排序“路径＋NUL＋文件SHA256＋LF”再SHA256。阶段结束交监督技术验收，未获指定提交授权不得暂存、提交、推送或进入下一阶段。
