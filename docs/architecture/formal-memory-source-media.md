# 正式记忆、完整来源与媒体持久化闭环

日常认知真实候选、受控主体及SUBJECT来源持有、图片理解的组合增量，见[独立契约](daily-cognition-and-image-learning.md#33-受控主体与来源)；不改变旧组合格式。

**状态：五组推荐及本契约所链接的四份所属补充已获用户明确批准。** 批准范围限于当前推荐方案；未采纳替代、范围外能力及其他正文待定事项不随之批准。本文与所属补充共同约束实现；实施、验证及监督技术审查进度只在[当前任务](../work/CURRENT_TASK.md)维护，[STATUS](../work/STATUS.md)只记录已验收里程碑。

本契约定义正式记忆／完整来源／媒体装配及其对基础运行装配的兼容边界；旧装配验证不自动证明新增能力。实际实现、验收及提交版本见[STATUS](../work/STATUS.md)。

<a id="scope"></a>

## 1. 交付边界与集中决定

交付实际记忆／来源／媒体服务与现有接入、缓存、运行、配置、审计和Provider的完整闭环：真实字节发布，接收确认，媒体理解或明确局部状态，冻结完整材料，保存稳定候选，所有者加入一次批次终结UoW，按ID读取当前正式对象、受控来源及运行观察，重启后确认和恢复，最终按引用安全清理。

学习候选可以由显式测试参与者合成，模型仍只用Provider模拟适配器；实际记忆、来源、媒体不得由默认成功替身提供。观察分别标`storage_execution=ACTUAL`、`model_adapter=SIMULATED`、`candidate_origin=SYNTHETIC`，不能合并成一个“真实／合成”布尔。梦境发布仍可用既有显式合成参与者，不能宣称persona整理完成。

本次维护对象的当前值、分数、状态和变更待办；不实现自动衰减、使用反馈、检索排序／索引引擎、关系推理、梦境整理、目标执行或persona生成，不取消这些模块的最终职责。没有真实供应商、生产鉴权、生产目录／G2、配置编辑、FAULTED解除、UNKNOWN人工结案、迁移或部署。本文的“目标锚点”指学习目标消息，不是未来目标对象；不借此建立目标模块。候选要求尚未装配的目标写入等效果时明确拒绝，不能丢掉该效果后成功终结。

| 集中已批准组 | 推荐及具体影响 | 可选替代及代价 | 验收依据 |
| --- | --- | --- | --- |
| 对象与关系 | 采用§3的封闭Schema、整数双指标和整组修订冲突；§4.4明确删除／最后来源释放的所有者闭包。固定释放计划及审计分支须事务内重查，竞争时整组退回；当前对象最多2项source联系 | 更宽关系／来源扇出需重算释放计划；自动合并、部分提交或事后补审计不推荐；延迟释放来源须另设持久待办及保留语义 | M01–M08、M22、M29 |
| 媒体状态与复用 | §5区分外部出现级REFUSED与已核验Provider跨事件保护；外部结果不能签发保护。冻结前有持久准备身份、逐出现工作和原请求关联，同一出现不因换窗口重复调用 | 信任外部拒绝建立全域保护会让输入者阻断其他事件；只按事件保护Provider拒绝易被换事件绕过。内部部分结果本包无生产路径；补齐需另定协议 | M09–M12、M24、M27–M28 |
| 上传／发布／GC | 原1 MiB上传／有序发布／CAS清理保持；准备总期推荐10分钟、单出现总期1分钟，未知所有者仍保护。窗口失效只撤消费者，不取消在途请求；释放与新引用竞争按固定协议处理 | TTL夺权不安全；无限延长准备会饥饿；期限后自动换键会扩大调用。未开始工作仅在后续合法触发及明确未发送证据下重获准入 | M13–M19、M25、M28–M29 |
| 容量 | E=2048、每事件2媒体、H/T/R=1/2/1保持；理解记录推荐2048、完整材料49152，候选最多8项。§5.4逐来源／状态给出元信息精确界，§8连同释放计划重算 | 保留I=1024装不下最大合法失败／拒绝记录；较小文本或ID会限制归因。允许控制字符输出但超总量时保存固定FAILED，敏感拒绝独立保留 | M20–M21、M26、M30 |
| 装配／兼容／权限 | 新临时库和真实文件、旧装配独立兼容验证保持；新增准备及释放计划格式只属于新装配，原逻辑键与执行计划键分离；外部无来源窗口、保护签发或恢复重发权 | 迁移须另列范围和切换决定；开放完整来源／拒绝解除／UNKNOWN结案均需新增授权，不能借本地确认实现 | M02、M07、M18–M23、M27–M29 |

推荐值的唯一声明在[配置补充](configuration.md#formal-memory-media-configuration)；以上为决策导航，不形成另一套默认值。选择某个替代后须同步其受影响正文、上界和整体验收条件，再交用户集中判断，不自动批准其余选项。

<a id="baseline-ports"></a>

## 2. 静态公开端口核对与必要增量

| 基线实际位置 | 可复用事实 | 必要增量／证据限制 |
| --- | --- | --- |
| [持久化声明](../../companion_memory/persistence/definitions.py)、[服务](../../companion_memory/persistence/service.py)、[Schema](../../companion_memory/persistence/schema.py) | 静态仓储／命令、受限StatementPort、同UoW、结果绑定审计、原键确认；BoundedTextSchema最高65536，点读仍受完整编码预算约束 | 新所有者固定Schema／查询、跨所有者约束与历史正文参与；不新增任意SQL／通用文件端口。不能因字段能声明65536就认为点读能装下 |
| [RuntimeAssembly／LearningParticipant](../../companion_memory/runtime/assembly.py) | 显式stage／finalize／recover，事务协调者唯一提交，缺参与者拒绝 | 当前学习参与者同时承担候选和合成来源发布，须拆成候选所有者与真实记忆／来源的受限参与能力；运行编排不取得对象任意写权 |
| [ModelWork](../../companion_memory/runtime/model_work.py)、[材料声明](../../companion_memory/configuration/material_contracts.py) | 原请求关联、Provider原键查询、已持久交接恢复、候选后本地终结 | 当前协议检查、WorkGrant结果owner、模板均固定为synthetic；新增显式材料／候选装配描述和媒体准备步骤，不能只换一个回调就宣称支持 |
| [MediaParticipant](../../companion_memory/ingress/media.py)、[接收与终结](../../companion_memory/runtime/assembly.py) | retain_event／settle_event／release_event在同UoW参与；payload最后一个运行／来源引用释放才可清理 | 新媒体出现绑定、文件代次、理解快照和处理中保护；回流沿同一事件owner连续持有，不要求再复制blob |
| [合成媒体](../../tests/runtime/media_participant.py)、[合成学习／发布](../../tests/runtime/participants.py) | 显式临时SQLite仓储、失败点及共享保护形状 | SyntheticMedia仅声明READY并保存清单，无文件；学习结果仅result_count及目标ID，无正式正文、双指标或关系。保留作旧装配验证，不能转正 |
| [事件格式](../../companion_memory/ingress/events.py) | 完整不可变原始事件，身份与正文分离，引用／发生时间保留 | 旧媒体状态仅AVAILABLE／MISSING／REFUSED，新增event_version=2及精确状态结构；旧格式解释保持，不覆写旧接收原文 |
| [Provider端口](../../companion_memory/provider/ports.py)、[媒体授权](../../companion_memory/provider/service.py) | understand_media、lookup_request、recover_result；字节上限1 MiB，固定模态任务；真实账本和模拟适配器 | 基础装配的authorize_media仅声明合成bytes；新真实存储绑定与字节能力生命周期见[Provider补充](provider.md#stored-media-provider-bridge)，不绕过Provider |
| [配置持久化](../../companion_memory/configuration/persistence.py)、[编码](../../companion_memory/configuration/persistent_codec.py) | 完整值／定义、材料声明、真实snapshot身份、原键初始化和恢复 | 新组合入口／域／材料版本；旧初始化整命令、单参数8192和目录上限必须同时核算，平台结构上限不是容量证明 |
| [运行结果Schema](../../companion_memory/runtime/transaction_schema.py)、[观察](../../companion_memory/runtime/observation.py) | 安全计数、原始回执／当前观察分离、有限当前进程确认观察 | 旧facts固定synthetic及共享change不适合正式对象；新命令结果精确列对象／来源／引用事实，独立能力来源标签。扩展固定投影，保留有限窗口约束 |
| [同事务审计](logging.md#transactional-audit-contract) | 必要slot、结果绑定、同事务失败毒化、独立开发者读取 | 现有change禁止正文；修订历史采用[专属审计正文补充](logging.md#memory-history-audit)，不放宽旧摘要或向agent开放审计 |

上表对比基础运行装配与本契约扩展所需能力，用于解释接口和兼容边界；不作为当前未实现清单。实际版本见[STATUS](../work/STATUS.md)。若后续变更必须改变旧格式字节解释或无法满足以下上界，须按[项目指引](../../AGENTS.md)报告具体冲突并暂停受影响部分。

<a id="objects"></a>

## 3. 正式对象、修订与依赖模型（已批准）

### 3.1 统一载体与当前对象

新领域记录采用确定性、精确类型、严格UTF-8的版本化JSON：固定字段／枚举、有界数组，未知字段、重复键、bool冒充int、浮点分数、NaN、孤立surrogate及自定义钩子拒绝。JSON键排序、转义和缺失／null按[新格式边界](persistence-and-transactions.md#formal-memory-media-storage)解释；每个记录同时受字段上限和完整编码上限约束，不保证所有可选字段能同时取各自最大值。ID遵守现有安全ID格式；不透明ID不是权限。以下字段除明确可空／可选外均必需。

| 记录／唯一修改者memory | 完整字段组与约束 |
| --- | --- |
| 当前认知对象 | `object_version=1, object_id, instance_id, kind=MEMORY/RELATION, revision, created_at_us, modified_at_us, lifecycle, forgotten_since_us, retention_policy_ref, content, scores, origin`；revision从1递增到正63位整数上限，禁止回绕；同库object_id不复用 |
| MEMORY.content | `category=EVENT/FACT/INFERENCE/OPINION, body, subject_ids, speaker_subject_id, stance=ASSERTED/DENIED/UNCERTAIN/SELF_ENDORSED, world_scope, occurred_range, applicable_range`；body为非空有界正文；主体列表0–4、不重复；speaker可空；时间范围可空，非空恰含可空start_us/end_us及precision=UNKNOWN/SECOND/MICROSECOND，已知起止不得倒序 |
| 范围 | `world_scope={kind:REAL/FICTIONAL/ROLEPLAY, context_id}`；REAL时context_id为空，其他非空且指向场景主体。entry是出处，不推断全局适用；时间缺失不由接收时间补齐 |
| 双指标 | `belief`、`retention`均为0–100精确整数；`scale_id=acceptance_100_v1`，`belief_reason`、`retention_reason`为有界非空文本，`score_basis`为本对象已声明的依据引用ID列表0–8；50沿用未定语义。低belief不直接改变生命周期，读取不加分 |
| 来源性质 | `origin={kind:DIRECT_LEARNING/DERIVED/OPERATOR_INPUT, candidate_id, batch_id, actor_ref, model_origin, candidate_origin}`；DIRECT有batch／candidate，DERIVED须有真实依据对象，OPERATOR_INPUT用管理输入ID并由权限认可的操作者登记，不伪造批次或模型。不支持的初始化来源须明确拒绝 |
| 主体身份 | `subject_version, subject_id, instance_id, kind=SELF/PLATFORM_PERSON/THING/FICTIONAL_CHARACTER/CONTEXT, platform_id, external_subject_id, label, revision`；平台人物的platform＋外部主体标识唯一，其他类型对应字段为空；SELF一实例唯一。外部标识是有界正文，不进入日志ID。主体登记不带“已确认同一人”含义 |
| RELATION.content | `relation_type=SAME_SUBJECT/PLAYS_ROLE/SUPPORTS/REFUTES/DERIVED_FROM/CITES/CONTEXT/RELATED, from_ref, to_ref, assertion, world_scope`；ref恰含type=SUBJECT/OBJECT、id及expected_revision（OBJECT必填，SUBJECT按登记修订）；关系本身也是有分数／生命周期／来源的对象 |
| 对象来源联系 | `(object_id, object_revision, source_id, link_role=DIRECT/CONTEXT, target_anchors, auxiliary_refs)`；每对象最多2项不同source联系，当前关联不保存历史正文，旧关联随修订审计保存。直接学习必须至少一项非空目标锚点，来源材料保存和真正支持分开；所有当前来源／依据关联合计编码≤2048字节 |
| 对象依据联系 | `(dependent_id, dependent_revision, basis_id, basis_revision, kind=SUPPORTS/REFUTES/DERIVED_FROM/CITES/CONTEXT, evidence_roots)`；最多8项；根为真实message_id或管理输入ID，不以引用条数当独立证据数 |

SAME_SUBJECT两端为不同SUBJECT，规范化端点顺序使同一断言不因方向重复；不合并主体行或转移平台权限。PLAYS_ROLE的起点为现实主体、终点为虚构角色，context必填。SUPPORTS／REFUTES／DERIVED_FROM指向认知OBJECT；CITES／CONTEXT／RELATED按明确ref类型表达，不能当支持。禁止对象自指；其他环不在本次做推理求解，存储可表达并标待审查，不递归传播分数。没有可执行表达式、任意关系类型或因“同名”自动建边。

关系和记忆共用ID空间及revision；同一次候选内引用新对象须使用候选预先分配的ID，所有引用在同UoW验证。已删除端点不能新增为有效支持；遗忘端点可供内部明确深读／整理引用，标`basis_state=FORGOTTEN`，不能冒充活跃支持或自动恢复。保存时存在性、当前修订、读取权和世界范围共同核验。过后来源修订或删除不会同步撤销邻接对象，只形成待办。

### 3.2 状态与修改删除

`lifecycle=ACTIVE/FORGOTTEN`用于尚存在对象；DELETED只在无正文墓碑中表达。创建先按ACTIVE及显式R执行[既有滞回](../product/lifecycle.md#source-line-463)，R低于F直接进入FORGOTTEN。之后所有分数命令在同UoW检查状态；F/H推荐值及初始强度只在配置补充声明。belief必须由候选或操作者明确给出；retention省略时仅可由配置所有者提供已批准后的初值并在候选保存前物化，禁止恢复时重新取默认。

每次有语义效果的正文、关系、评分或状态变更revision加1；同键重复返回原回执，不加版本。新键而值完全相同返回`REJECTED/PRECONDITION_FAILED/NO_CHANGE`，无新写入、审计或修订／待办，不伪造一次模块变更。修改全部使用expected_revision；时间只记录事实，顺序由revision／事务决定。遗忘时设置forgotten_since_us，连续遗忘中不重置；真实恢复清空它。自动衰减、按保留期限扫描删除、使用反馈凭据及增益不在本次实现；不得给“自动生命周期完成”成功状态。

`delete_object`可直接撤销错误／重复／有害对象，无须先遗忘。原子效果：删除当前正文及当前关联所有权，保留`object_id, kind, last_revision, deletion_revision, deleted_at_us, reason_code, operation_ref`墓碑，释放本对象来源／媒体引用，写索引失效及依赖待办，必要历史正文归审计。墓碑没有正文、摘要、分数理由、原始主体名或能向agent恢复正文的审计指针；普通／深度读取都不可返回删除正文。新键再次删已删对象返回REJECTED／OBJECT_DELETED，原ID永不重建。主体注册身份不随对象删除合并或删除；本次没有主体注销／跨平台合并命令。

正文历史仅经[日志正文专属接口](logging.md#memory-history-audit)读取。当前来源仍被其他对象共享时保留完整窗口；不承诺内容抹除、阻止同源重新学习或即时修改persona。

### 3.3 冲突与待办

候选中任一修改的expected_revision或引用对象修订不匹配，整次正式提交不发生，批次保持受保护、工作为SYSTEM_BLOCKED且reason=REVISION_CONFLICT；不是第四业务终态或FAILED_DROPPED。确定性重验仅在所有原预期仍满足，或已命中原提交回执时可完成；不自动把旧候选套到新修订，不局部合并，不发“修复候选”模型请求。原键不可改内容；需要改变候选的冲突解决／放弃批次属于本次未开放的后续明确决定。独立维护命令冲突返回PRECONDITION_FAILED，不自动重试新版本。

同UoW写`index_dirty(object_id, revision, action=UPSERT/REMOVE)`及`dependency_dirty(changed_id, changed_revision, reason=CONTENT_CHANGED/FORGOTTEN/RESTORED/DELETED, state=PENDING)`。前者可合并到更新修订，后者按变更身份去重；不得丢失未检查的删除／恢复事实。不在对象修改事务遍历无界反向关系：待办是失效事件，消费者将来按固定页反查当前依据边及其修订。`list_dirty_dependencies`只读、有界；本次不消费为“已整理”，不创建假persona任务或运行索引算法。读取权威当前状态可立即挡住旧索引／已删ID。

<a id="sources-candidates"></a>

## 4. 完整来源、候选与一次终结（已批准）

### 4.1 来源快照及目标依据

来源所有者为memory，逻辑来源包含冻结的全部H／T／R：`source_version, source_id, batch_id, run_id, entry_id, host_id, platform_id, config_snapshot_id, domain_revisions, material_contract_ref, frozen_at_us, ordered_members, digest`。每成员保存角色、不可变message_id、entry_seq、原文摘要、系统接收时间、独立移交时间或null，以及选用的媒体出现／理解版本清单。原始发送主体、角色、原发生时间／偏移／精度、正文、引用及外部理解全部由不可变原始事件重建。

推荐复用受保护的不可变接入payload，来源持有显式、可核验的独立引用；不复制第二份可编辑原文，也不依赖位置表。source成员及选用的媒体理解记录不可变，完整数据在源payload或版本化理解所有者中存在。冻结后新理解不得改变旧快照；允许有完整成员清单的分页读取，但缺任一成员不得返回“完整”。材料与来源均保留原始已有理解及实际选用结果，不能只保留最后一段模型文本。

同一batch的正式结果共享一份source，source_id在候选保存前稳定，唯一性为batch＋材料版本／摘要；同源不同内容冲突。无正式结果时不发布永久source，原运行材料按三终态释放。已有正式对象共享source时，删除一个只移除其link；最后一个对象及候选／工作／显式来源持有者释放时，按[§4.4](#source-release)在同UoW解除source成员、理解及原文引用，留无正文处置元信息。遗忘对象仍持有source。只剩审计／Provider交接不保护业务source或blob。

目标锚点为`message_id, part=BODY/QUOTATION/MEDIA/EVENT, item_index, start_utf8, end_utf8`：EVENT使用整个事件、其余定位实际字段；无片段时起止均null，非空片段为半开UTF-8字节区间且不得切断字符。媒体锚点还绑定occurrence_id及interpretation_id；MISSING／FAILED／REFUSED不能充当已理解内容事实的证据。直接结果至少一个T锚点；辅助引用只能来自本批H／R，独立保存用途，不把它们计入目标或独立支持。跨批同message_id是同根证据，来源快照数不是证据独立份数。对“某人发送过媒体”的事件事实可锚定原事件，但不据缺失理解推断其内容。

### 4.2 稳定身份与候选

候选所有者为cognition，候选可合成但必须真实持久：`candidate_version, candidate_id, batch_id, run_id, work_generation, config_snapshot_id, provider_request_id, handoff_ref, transform_version, source_id, manifest_digest, terminal_proposal, ordered_change_refs, origin`。每个变更恰为CREATE_MEMORY／CREATE_RELATION／REGISTER_SUBJECT／REPLACE_CURRENT／SET_SCORES／DELETE_OBJECT之一，含稳定目标ID、预期修订、完整拟议值及来源／依据引用。上限是工程输出限制，不假定记忆数≤输入数。

ID推荐按带类型域的SHA-256从`database_id + batch_id + durable_handoff_id + transform_version + candidate_ordinal + object_kind`确定性产生，前缀加小写hex符合安全ID；源ID由batch及冻结材料身份产生，媒体出现身份由事件身份＋媒体序号产生。身份原组成同时保存并复核，hash碰撞报完整性故障，不合并；跨库、对象kind、序号不同不混淆。独立维护操作的键及对象ID由调用方在首次副作用前保留。确认丢失不得随机重造ID。

Provider交接已持久但候选未保存时，只从授权原交接确定性变换；变换版本／配置须可恢复。候选采用有限叶记录＋清单，全部叶及保护引用在一次候选事务共同提交；不允许部分叶先成为可用候选。每叶、条数、总量见§8，无无限分片。stage和finalize有独立操作键；stage回执只确认候选可恢复，绝不是正式记忆成功。

### 4.3 真实所有者加入终结UoW

事务外完成媒体准备、原材料验证、模型／合成候选构造和候选持久确认。finalize逻辑意图绑定已确认candidate、source基底、work／owner和预期修订；具体执行再绑定[固定释放计划](persistence-and-transactions.md#release-command-branches)。所有会影响结果的身份／意图进入对应指纹；在写事务内重新读取并校验受保护候选及目标，不接受调用方只给一个自报摘要。

| 同一终结事务的所有者 | 必须完成的效果 |
| --- | --- |
| cognition | 验证原交接／候选来源、完整清单及处置状态；确认成功提案或明确Provider终态；释放独占候选正文，保留恢复处置元信息 |
| memory | 成功时应用全部对象／主体／关系、当前修订、完整source和逐对象锚点／依据边、索引／依赖待办；失败／拒学不发布候选，不创建空的永久source |
| media | 在source取得引用后释放运行保护；核验READY物理代次与持续保护事实；无物理文件I/O、模型调用或独立commit |
| buffers／ingress | 按冻结目标精确消费，轮转历史，保留后到／最新／其他入口输入；最后一个业务引用才释放原文，原接收身份及回执继续存在 |
| runtime | 持久三终态、结果数、工作终结与原候选关联；候选／引用／记忆任何失败均不终结 |
| logging_service／persistence | 必要安全审计、修改／删除涉及的受限历史正文、结果绑定证据、完整原回执，与上述共同COMMIT；任一失败毒化整个UoW |

成功含零结果，仍精确消费T并留T末尾作新H；普通学习失败留尾并标失败；明确学习敏感拒绝清本入口旧H且不留T尾。媒体局部拒绝不是学习拒绝。所有情况均保护R、后到输入、共享source和其他入口。源／媒体引用不是靠“最后写审计成功”推导，须由所属参与仓储验证实际变化。

原终结键匹配已提交回执时，直接返回原对象ID／revision／计数，不再进入参与者或转动历史。确认缺失只有符合[原确认条件](persistence-and-transactions.md#persistence-foundation-idempotency)才能用保存候选重做本地事务；NotFound、读失败或旧writer在途均不授权重放。存储故障不是普通学习失败，未确认的目标与候选继续受保护。

<a id="source-release"></a>

### 4.4 对象删除、最后来源释放及共享竞争

对象修改也可撤销旧source联系，适用同一协议。独立删除以对象原revision为语义前置；批次候选中的删除另保留整组候选约束。协调者在任何业务写入前准备有界释放计划，列本操作确切撤销／取得的持有关系、预计最后释放集合和所有者变更集合；该计划不是删除授权。事务内原子闭包如下：

| 唯一所有者 | 同一UoW中的实际变更；没有该效果时不得伪造slot |
| --- | --- |
| memory | 修改／删除对象、移除其source holder、写墓碑和依赖／索引待办；撤销最后source holder时重查全部holder类别，删来源清单／成员正文及自身联系，留source RELEASED元信息。非最后持有者只移除本对象边，source正文不变 |
| ingress | 拥有不可变原始payload及其寿命引用登记；释放每个已退役source的SOURCE payload引用，逐message事务内重查位置／历史／批次／候选／准备／处理恢复／其他source等真实引用。仍有引用则仅改引用登记，无正文删除；最后引用消失才删payload并标PAYLOAD_RELEASED。原事件身份、摘要和接收回执保留 |
| media | 释放本对象确实持有的OBJECT引用及退役source的SOURCE理解／blob引用；payload最后释放时由ingress提出类型化release_event，media删除相应EVENT引用与出现的业务选择关系、保留无正文出现处置身份。其他出现／来源／READ／PROCESSING继续保护；不在此UoW删文件 |
| media对理解版本 | 只解除本次消费者关系并推进引用修订，不修改不可变理解text／来源／状态。零业务持有后保留理解为DETACHED_REUSABLE及必要拒绝证据，按§5权限复用；外部EVENT结果无有效出现时不对外可读、不推广为内容缓存。理解保留本身不保护blob，也不复活已释放payload/source |
| logging_service | 本次对象修改／删除的完整旧值及原关联、上述实际变更的安全必要审计，与原回执同提交。仅释放source不复制聊天原文进历史审计；历史指针不成为业务holder |
| buffers／cognition／runtime | 独立对象删除不修改队列、候选或运行状态，不能为凑slot给它们写无意义revision；批次终结／候选释放／准备处置时，仅相应所有者按本来职责修改并参与同UoW |

ingress的引用登记只由其类型化参与端口修改，其他owner在同UoW交接取得／释放意图；buffers仍唯一拥有队列位置和历史槽位，不因删除跨模块代写。每个source、payload及媒体引用集合均有非回绕`references_revision`，取得、转交、释放同事务推进。检查使用固定主键／反向索引的存在性及计数核验，不枚举所有共享holder；负数、缺边或修订不符不能当“无引用”。PROCESSING所需原请求／字节／原文恢复保护只能在确认不再需要且执行者结束后释放。

同一原子变更集合先计算提交后关系：例如同时删A并让合法候选B取得S，S仍被持有，不能先执行“最后删除”再临时重建。已经RELEASED的S不能重新attach；新正式学习必须保存新的合法source身份及完整材料，不能从旧回执或审计恢复S。OBJECT媒体引用只允许指向该对象现有source成员的子集，不能用任意blob列表绕过有界释放。

必要审计集合及命令变体只由[持久化固定分支协议](persistence-and-transactions.md#release-command-branches)选择，实际slot内容只在[日志补充](logging.md#memory-history-audit)维护。事务外看见“最后一个”不构成删除许可；事务内对所有相关references_revision、真实holder和计划效果重查，不匹配则任何业务写入前整组拒绝`PRECONDITION_FAILED/OWNERSHIP_CHANGED`。不能在handler临时增加必要集合、漏掉ingress／media，或为了匹配已有集合给没变更的模块伪造事件。

具体竞争：A是S最后对象，计划将释放S；B先取得S并提交，删除A的旧计划全无、无对象历史／删除审计，需按新计划确认S保留后再执行。反向顺序中删除先提交，则B收到SOURCE_CHANGED且不能取得已退役S。若预读S仍有B、B先释放，A旧计划也必须全无，重规划后的命令须包含真正最后释放涉及的ingress／media及必要审计。若仅payload仍被H持有，source可退役且ingress仍有引用变更审计，原文和EVENT保护保留；以后H轮转的同UoW才完成剩余释放。安全回执逐项区分`source_retired, payload_references_released, payloads_deleted, interpretation_references_released, blob_references_released`，不能把引用减少当文件删除。

每对象最多2个source、每source最多4成员／8媒体、一次最多8对象，因此释放计划最多16个旧source叶；并列多个holder只做存在性重查。新source取得和批次窗口本身另由其已冻结清单界定，不能把任意历史遍历塞入释放计划。合法后续只有原键确认、在可靠未提交后以不变语义重新规划本地分支，或向调用者报告竞争／修订阻塞；后者不批准重做候选、模型、故障解除或扩大对象集合。M29须验证上述两种共享竞态、H保留及最后H释放的全部所有者／审计。

<a id="interpretation"></a>

## 5. 媒体理解状态与复用（已批准）

### 5.1 原件、出现与理解分离

媒体所有者维护`blob_id, sha256, byte_count, declared_modality, detected_type_or_null, physical_generation, state`；出现维护`occurrence_id, message_id, media_index, entry_id, blob_id, generation, received_binding`。相同字节只存一份blob，但不同事件、同事件不同媒体位置均有独立出现；外部主体／发送时间来自原事件。相同事件重投不新增出现。原件存储不承诺解码、转码、病毒检查、感知去重或媒体内容真实；不解压、不抓URL、不执行媒体。

理解记录为不可变版本，精确19字段及各来源／状态限额只在[§5.4](#interpretation-envelope)维护。EXTERNAL是外部报告归因，不赋予Provider身份／读取权；外部结果固定EVENT范围及原event_id，内部内容分析可CONTENT并令event_id为空，情境分析必须EVENT。本次不实现分片拼接。拒绝的权限及复用优先级见下一节，不能从正文或单个status字符串推断内部终态。

新event_version=2沿用原事件身份及顶层字段；media每项恰含`reference_id, occurrence_id, modality, interpretation`，interpretation为空表示未提供，否则含`status, text, source_ref, coverage`。reference_id是媒体所有者给该授权入口签发且可重建的上传绑定ID，occurrence_id与事件身份／位置复核，不是裸blob授权。旧版本字段和AVAILABLE状态不就地重解释，兼容见§9。

### 5.2 状态矩阵

| 状态 | 精确含义／输入组合 | 正常／回流处理及复用 |
| --- | --- | --- |
| MISSING | 未提供结果；text=null | 先找合格内容缓存，无则登记一次媒体工作，经Provider补充 |
| COMPLETE | 成功且非空、非纯空白text，coverage=COMPLETE | 优先复用外部本次完整结果；否则使用匹配内部结果。外部结果不自动推广到别的事件 |
| EMPTY | 成功明确无可用描述／语音，text恰为空串，coverage=COMPLETE | 作为已经处理的空结果持久复用，不触发补做，不捏造内容事实 |
| PARTIAL | 外部报告有非空text、coverage=EXPLICIT_PARTIAL | 保存并可作为明确部分信息；本次不自动补齐或宣称完整。当前Provider桥接没有内部PARTIAL输出路径，INTERNAL＋PARTIAL拒绝 |
| FAILED | 已确认普通失败／其他拒绝／结果不合规，text=null | 固定原因元信息独立保存，错误文本不作描述。该出现不业务重试；本轮学习可使用其他信息及明确缺口 |
| REFUSED | 外部报告或核验过的Provider敏感终态，text恰为“敏感信息无法访问”，coverage=UNSPECIFIED；两种origin不得混同 | 外部仅约束该出现；只有下述Provider证据能建立跨事件保护。固定占位不作内容证据，均不触发整批拒学 |

外部空字符串只有显式EMPTY才有效；COMPLETE＋空串／纯空白、EMPTY＋非空、FAILED＋描述、REFUSED＋任意替代正文整体拒绝该新事件，不默默修复后确认接收。未提供解释可在原始输入中显式MISSING。Provider媒体成功空串映射EMPTY；非空规范化成功映射COMPLETE；纯空白成功视RESULT_INVALID并持久局部FAILED。该结果校验不改原Provider账本。Provider未决、准入阻止、存储失败分别是工作状态UNKNOWN／WAITING／SYSTEM_BLOCKED，不是假造上述已完成结果。

推荐跨出现的内部内容复用键为`blob sha256 + byte_count + task + modality + interpretation_fingerprint + prompt_revision + interpretation_scope`；fingerprint覆盖实际模拟profile／模型及影响解释的策略，配置身份另存。scope最宽为同实例内容分析授权域；情境解释必须绑定事件／入口，不能仅按hash共享。读取缓存前验证当前调用授权，不提供全局hash存在性探针。外部理解默认event范围，只复用本事件；外部自报“全局可复用”不授予权限。

正常缺失结果可复用同键COMPLETE／EMPTY；外部PARTIAL仅在本事件复用为同一部分结果，不当完整。复用建立版本化`occurrence_selection(occurrence_id, current_generation, selection_revision, interpretation_id, selection_kind=ORIGINAL/CONTENT_REUSE/PROTECTED_REFUSAL)`，media以CAS更新当前选择指针，旧选择和已冻结清单不变。解释保留其原产生代次，新出现保护当前实际文件代次，须核验相同sha256／长度、任务及授权域，不能改写解释的原归因。PROTECTED_REFUSAL仅表示本次命中§5.2的保护，不将原EVENT解释伪装成本事件内容分析。已删除原件后仍保留的内容理解可按原键复用，摘要复用依赖SHA-256抗碰撞假设，不宣称对已不存在的旧字节再次逐字比较。

FAILED不永久负缓存；同一出现的重复调度、恢复、回流或更换准备窗口不是新事件，不再请求。不同已确认事件且无已有外部结果时，可登记新的媒体工作，关联previous_failure，受正常门控／Provider预算约束。

**拒绝权限协议：** 外部输入能力只能提交自己事件中该媒体位置的`EXTERNAL/REFUSED`；它保留报告来源、固定占位及事件归属，不写拒绝保护索引，不借source_ref／自报request_id升级为Provider结果。另一个事件的MISSING仍可正常申请。只有media的原生Provider结果所有者核验同库、原请求／结果owner、MEDIA_UNDERSTANDING能力、原artifact及字节摘要／长度、任务／模态、授权域，以及已持久的`SENSITIVE_REFUSAL/SENSITIVE_INFORMATION`明确终态，才能在**保存内部REFUSED的同一UoW**写保护与必要审计；核验入口见[Provider补充](provider.md#stored-media-provider-bridge)。未知、OTHER_REFUSAL、外部报告和自由错误文本均不具有此权。

保护唯一键为`instance_id + authorization_domain_id + sha256 + byte_count + modality + task`。authorization_domain来自可信授权绑定，不从事件字段获取；跨实例／跨授权域不传播，物理去重不合并权限。保护覆盖同域不同entry／事件及内部EVENT、CONTENT分析，不含prompt／profile版本；这是对已核验敏感终态的保守产品选择，可能阻止同域其他情境的内部理解。记录含原interpretation_id、request_id、核验终态证据引用、建立操作身份；没有敏感原响应或自动过期／解除接口。复用保护只引用原内部记录，不伪造一次新Provider拒绝或费用。

选择优先级固定为：先校验该出现的外部报告；合法COMPLETE／EMPTY／PARTIAL／FAILED／REFUSED均选本次外部记录并结束该出现的准备，MISSING才进入内部路径。内部路径先核验同域拒绝保护，再查当前出现已完成工作，最后查合格COMPLETE／EMPTY内容缓存；仍无结果才申请首次工作。保护优先于内部成功缓存和新dispatch，每次冻结前及最后发送许可前重查保护修订。新外部完整结果可用于新事件，但不清除旧保护、不改旧出现／已冻结来源、不触发内部验证。保护后来建立时，未冻结选择须重新核验并选固定拒绝记录；已冻结快照保持原版本，不回写历史。

例：事件A外部报告REFUSED、同字节事件B为MISSING，B不受A阻止；B经Provider明确敏感拒绝后，事件C为MISSING即复用B的保护，零内部请求。事件D带外部COMPLETE可按外部报告使用；D不能解除B的保护。若保护写入和另一请求dispatch竞争，保护先获共用门控串行区则零start；dispatch先消费许可则只收尾原已开始请求，不再attempt，其返回的内部成功不覆盖保护。授权、原键确认及M27的双向顺序断言均是该推荐的验收条件。

<a id="media-preparation"></a>

### 5.3 冻结前媒体准备的持久协议

准备不是目标冻结或一次学习。runtime在一次短事务内按FIFO选择当前H／T／R，登记`preparation_version=1, preparation_id, entry_id, trigger_key, proposed_batch_id, proposed_run_id, config_snapshot_id, material_contract_ref, window_digest, ordered_members, checked_entry_revision, revision, owner_generation, mode_epoch, phase, started_at_us, deadline_at_us, spent_ms, last_observed_at_us`；成员包含角色、message_id／entry_seq、payload摘要和逐出现绑定。ID在首次登记前由库／entry／原trigger确定并保留，最多4成员／8出现。runtime保存清单及入口唯一预约，ingress取得独立PREPARATION原文引用，media取得对应PREPARATION媒体／解释引用，必要审计及原回执同UoW。这只预约本入口调度，既不消费T，也不把批次计入三终态。

数据库唯一约束让一个入口最多有一个非终结准备或目标批次占有同一调度预约。并发不同trigger命中预约时只返回`WAITING/EXISTING_PREPARATION`及有权查看的原身份，不生成第二份媒体工作；相同trigger原键返回原回执。`claim_preparation(preparation_id, expected_revision, owner_generation)`仅由现有唯一运行owner执行CAS，成功后签发绑定epoch的能力；旧代次写入为WORK_FENCED。租约或总期到达不授予接管权。

media独立持久`occurrence_work_id`，唯一键为`instance_id + occurrence_id + task`；初次内部申请时冻结模态、授权域、原blob／generation、策略／prompt／配置及结果owner，此后换窗口／配置不得变更同一出现的工作意图。同事件不同位置可有不同工作，同一位置重复加入H／T／R只加消费者引用。媒体领取同样CAS并受processing_concurrency限制；每准入代次至多一个活跃执行者及一个原Provider逻辑请求，旧代次必须按下文可靠未发送例外结清后才可有下一代。已完成结果按出现保留；普通失败／REFUSED不因新准备而重做。

媒体工作保存`revision, owner_generation, phase, consumer_refs, selection_revision, interpretation_id_or_null, admission_generation, original_request_descriptor, original_operation_key, provider_request_id_or_null, original_result_owner, request_association_state, started_at_us, deadline_at_us, spent_ms, last_observed_at_us`。原请求描述是可完整重建Provider规范化请求的有界清单，包含artifact／字节摘要／长度、scope、profile／任务及配置身份，**不含媒体字节**。首次调用Provider前，先在本地同事务保存描述／键与PROCESSING保护并确认COMMITTED；请求ID尚未知可空。随后只以此原请求调用；登记／返回确认丢失只lookup原请求，得到ID后本地关联，不能再调understand_media猜测原登记是否发生。

| 持久相位／变化 | 进入条件、可见结果和唯一后续动作 |
| --- | --- |
| 准备SELECTED→CLAIMED | 已确认预约及全部保护，CAS唯一领取；未领取不发模型 |
| 逐出现UNRESOLVED→RESULT_STORED | 外部结果、出现原结果或合格缓存已验证，保存不可变selection及复用事实；无Provider调用 |
| UNRESOLVED→READY_TO_REQUEST→REQUEST_ASSOCIATED | 首次媒体领取及原请求描述已确认；Provider调用至多一次，原ID按确认结果关联；普通预算／门控阻止为WAITING_ADMISSION，无假FAILED |
| REQUEST_ASSOCIATED→RESULT_STORED | 已核验持久交接／终态，按§5.4保存结果；敏感结果与保护一起落盘；请求普通终态失败才成为局部FAILED |
| 任一本地写确认未知／Provider未决 | 分别LOCAL_UNCONFIRMED／REMOTE_UNKNOWN观察，保存原键及保护；不替换phase为结果终态，不冻结，不重发 |
| 全部选择完成：CLAIMED→MEDIA_READY→FROZEN | 重建完整材料并核预算；冻结事务重查模式／预约／原文／选择／保护修订和当前入口修订，转交全部保护、持久批次与工作、释放准备预约；同提交仅一个batch |
| SELECTED／CLAIMED／MEDIA_READY→PARKED | 入梦关门，未开始部分持久停放并撤其发送能力；已开始部分只完成原结果本地保存，全部安全后才能入FOCUSED |
| 未冻结准备→INVALIDATED或EXPIRED | 成员选择实质变化或准备总期耗尽，原批次不生成；消费者释放按下述闭包。原trigger确认仍返回登记时原回执，当前处置由独立观察给出，不重写原确认或自动新建准备 |

**窗口核验：** 固定窗口是当时合法FIFO选择，不等待新消息。尾部追加只提高entry_revision，若H／T／R的角色、ID、摘要、最旧T、配置和出现选择仍相同，不废弃已付费理解；在短事务重新读取并更新checked_entry_revision，下一冻结仍CAS当前修订，继续追加可导致再次短核验而不会扩窗。H替换、最旧T改变、已选R改变、来源损坏或影响所选理解的保护变化才是实质变化：前三者INVALIDATED；损坏SYSTEM_BLOCKED；保护变化使选择重新核验后再准备，不能复用旧MEDIA_READY摘要。新窗口可引用同一出现已持久的结果，不重新发送该出现。

**释放闭包：** INVALIDATED／EXPIRED最后消费者解除时，runtime处置准备及预约；ingress只释放其PREPARATION原文引用；media只释放对应PREPARATION引用／选择持有关系。三者有实际变化才按[固定分支协议](persistence-and-transactions.md#release-command-branches)加入同UoW及必要审计，原始EVENT／H／来源等其他保护不受影响。已经登记请求、字节能力、未结束I/O或UNKNOWN由独立PROCESSING及其原文恢复引用继续持有；先转交恢复保护再释放消费者，绝无空窗。无消费者的已完成内容结果仍可持久复用；释放不能删除Provider账本或变造局部FAILED。

**总期限：** [配置补充](configuration.md#formal-memory-media-configuration)分别限定单出现和准备从首次领取／首次SELECTED起的总期，包含等待准入、文件I/O、本地登记、Provider、保存、确认及停放，不能逐媒体／每次查询重置准备总期。进程内取单调剩余与持久绝对期限、累计spent_ms的最小值；跨进程不比较旧单调时钟，依据deadline_at_us、已计耗时及last_observed_at_us保守取剩余，发现墙钟回退不能证明剩余时按EXPIRED关闭新发送。总期到不制造FAILED：未开始者停放，已开始者保留原请求并仅作有界本地收尾／恢复。Provider明确已执行且结束的普通超时仍按其终态映射FAILED。

过期／失效后，后续**新的合法调度触发**可建立新准备并复用已完成结果。只有原逐出现工作可证明从未发送、全部旧本地任务已结束、原Provider登记可靠不存在，或已确认零attempt的门控／预算拒绝时，才可在正常门控重新取得准入代次及该次期限；先持久保存前代结论、新键和关联，不修改原请求。这是尚未执行工作的准入，不适用于曾发送、普通FAILED、REFUSED或任何UNKNOWN；不自动循环换trigger／键。读NotFound本身不是上述证据，恢复阶段不执行该新准入。

**并发与入梦：** 准备登记／冻结与enter_focus共用入口预约及模式核验；关门先发生则不登记新准备、不冻结、dispatch零start，既有准备按原范围PARKED。媒体dispatch先发生则纳入既有入梦截点，只允许原结果保存与安全停放，不能接着冻结／学习或进入下一attempt。窗口已失效也不把该调用移出截点；UNKNOWN、写入未决或未结束资源使入梦依原期限FAULTED。恢复先隔离旧owner，确认准备／领取／关联／结果／释放原键，重建消费者及处理保护；无请求的工作只停放，已有原请求仅lookup／recover_result，本地选择已确认则原ID复用。新进程不发送模型、延长期限或悄悄解除FAULTED。例与回归断言见M28。

<a id="interpretation-envelope"></a>

### 5.4 理解记录精确界限及失败记录可保存性

完整记录恰含19字段：`interpretation_version, interpretation_id, blob_id, generation, task, modality, origin, status, text, coverage, source_ref, interpretation_fingerprint, prompt_revision, scope_kind, scope_id, event_id, provider_request_id, created_at_us, failure_reason`，无可选扩展或省略字段。版本恰1；generation为1至2^63−1、created_at_us为0至2^63−1，十进制至多19位。interpretation_id、blob_id、scope_id以及非null的event_id／provider_request_id均为现有安全格式的1–128字节ASCII；字段两侧引号另计。

| 元信息／来源 | 精确约束 |
| --- | --- |
| task／modality | 仅IMAGE/DESCRIBE、AUDIO/TRANSCRIBE、VIDEO/DESCRIBE合法配对，最长task为10字节、modality为5字节 |
| INTERNAL | origin恰INTERNAL；fingerprint恰64小写hex，prompt_revision为1–64安全ASCII；scope_kind仅CONTENT或EVENT、对应event_id为null或128上限ID；provider_request_id必需。source_ref为1–128安全ASCII的持久交接ID（COMPLETE／EMPTY），或原request_id（FAILED／REFUSED，没有交接时不伪造artifact） |
| EXTERNAL | origin恰EXTERNAL，scope_kind恰EVENT，event_id必需；fingerprint、prompt_revision、provider_request_id均null。source_ref为非空报告归因文本，其**完整JSON字符串值**编码≤130字节，且原UTF-8≤128；不规范化、不截断，外部不能填内部字段 |
| COMPLETE／EMPTY | coverage恰COMPLETE；COMPLETE的text非空且非纯空白，EMPTY的text恰空串；failure_reason=null |
| PARTIAL | 仅EXTERNAL，coverage恰EXPLICIT_PARTIAL，text非空且非纯空白，failure_reason=null |
| FAILED | text=null，coverage恰UNSPECIFIED；EXTERNAL仅EXTERNAL_FAILURE；INTERNAL仅PROVIDER_FAILURE／OTHER_REFUSAL／RESULT_INVALID／RESULT_LIMIT_EXCEEDED，最长20字节。原Provider细分错误保留在其账本，不复制自由错误消息 |
| REFUSED | text恰固定占位“敏感信息无法访问”（24 UTF-8字节），coverage恰UNSPECIFIED、failure_reason=null；权限证据仅按§5.2，外部不能自报已核验 |
| MISSING | 是出现的未提供状态，不创建理解版本。event内显式MISSING须text=null、source_ref=null、coverage=UNSPECIFIED；只有完成上述五种状态之一才有可冻结的选用记录 |

文本字段限额仅约束COMPLETE／PARTIAL，不约束固定拒绝占位。JSON使用[存储确定编码](persistence-and-transactions.md#formal-memory-media-storage)：C0字符一律六字节、引号／反斜线两字节、其他Unicode原UTF-8，无Unicode归一化。以下逐字面量核算含**全部键、标点、引号及最长合法元信息**，元信息界M已含text的空串引号；因此非空正文的转义后内容长度L直接相加。INTERNAL取最长合法AUDIO/TRANSCRIBE及EVENT范围，CONTENT少124字节；无自由元信息尾部。

| origin／status | 完整固定部分或固定结果上界（字节） | 推荐包下的可保存结论 |
| --- | --- | --- |
| INTERNAL COMPLETE | M=1305；完整为1305＋L | 512普通ASCII／无转义UTF-8字节时1817；512个C0为4377，超I，须保存下述失败 |
| INTERNAL EMPTY | 1302 | 完整空成功必能保存 |
| INTERNAL FAILED | 1327（最长RESULT_LIMIT_EXCEEDED） | 不含原超限正文，最大合法归因仍能保存 |
| INTERNAL REFUSED | 1331（已含24字节占位） | 固定敏感结果必能保存，绝不改普通失败 |
| EXTERNAL COMPLETE／PARTIAL | M分别1055／1062；完整为M＋L | 512普通字节分别1567／1574；仍与完整event共同受接收预算约束 |
| EXTERNAL EMPTY／FAILED／REFUSED | 分别1052／1072／1081 | 含最长报告source_ref、ID、整数及各自固定状态；FAILED按15字节EXTERNAL_FAILURE计 |

配置必须在任何建库／接收／发送前证明`I≥1331`，并容纳此Schema全部终态及最大原请求归因；少1即准入失败，不能等敏感结果回来才发现放不下。推荐I=2048；1327字节失败记录及1331字节拒绝记录是独立保底，降低可变text限额不影响它们。超出固定元信息边界属于输入／绑定配置不合法，不能将超长身份截成另一个合法身份；内部工作须在发送前核验原请求到这些字段的无损映射。

三类失败严格分开：①配置I不足、模型绑定的元信息无法无损映射或跨层账目不合格，按[配置首错及装配映射](configuration.md#formal-memory-media-configuration)拒绝（I=1330为VALUE_INVALID/RANGE_INVALID），零接收／零发送；②外部事件有非法状态、过长报告字段、text或完整理解／event超限，T01前`REJECTED/INVALID_INPUT/LIMIT_EXCEEDED`（组合错误用INVALID_STATE_COMBINATION），无新payload／出现／选择／保护；③内部Provider已确认普通成功，但text单字段超限、实际完整记录>I或格式非法，media保存`INTERNAL/FAILED`及RESULT_LIMIT_EXCEEDED或RESULT_INVALID，source_ref绑定原request，text=null。Provider账本和交接不改，该出现不重发；只有该失败记录COMMITTED才返回RESULT_STORED，真实存储失败仍SYSTEM_BLOCKED／UNCONFIRMED，不能声称保底已经保存。

核验过的Provider敏感终态优先于任何可变文本校验：只构造固定REFUSED、原请求证据与保护；不复制拒绝附带正文，不因它超长而降为FAILED。未核验的外部“敏感”字符串不能走此通道。以512个U+0000的已确认普通成功为例：4377>2048，失败记录1327≤2048，可确认局部FAILED并继续其他信息；若保存时SQLite失败，则保留原请求及保护等待本地恢复。M30须分别断言配置拒绝、外部接收拒绝、内部失败成功落盘与敏感固定结果，不能合成一个“超限测试”。

<a id="files-gc"></a>

## 6. 原始字节、文件发布、引用与GC（已批准）

### 6.1 所有权、公开上传与正常路径

推荐可信装配分别持有新自有媒体根、上传暂存目录及同文件系统的不可变发布目录；这些路径经统一配置，物理身份／独占能力由基础设施验证。没有生产路径默认。上传端口只取授权入口、稳定upload_key、declared_modality和受限字节块，禁止客户端路径／URL、可执行回调或按hash直接附着。

`begin_upload(key, metadata)`先提交上传意图和独占资源绑定；`append_upload(upload_ref, offset, bytes)`逐块验证连续offset、总限额和有界I/O，返回的进度只表示临时写入，不承诺重启保留已上传offset。重复块仅在原字节相同且原写者已结束时确认，不并发重写。`finish_upload(upload_ref)`完成流长／SHA-256、内容检查和同步，再发布并提交READY绑定；只有此提交回执可用于事件接收。调用方在首次副作用前持有key和元信息；finish确认丢失时查原上传操作，不能换键生成假新blob。

上传未完成即崩溃可要求从offset=0重新传完整字节，显式报告REUPLOAD_REQUIRED；同一upload_key绑定的已确定摘要不能改变。调用方结束／取消等待不表示后台写者已结束。与已READY绑定关联的事件接收仍须T01成功才算“接收完整事件”；上传READY不是“已学习”或无限期存储保证。

精确去重：流式计算SHA-256及长度，在同内容锁下核验现有READY物理文件身份／长度／hash，并流式逐字节比较。相同则复用blob、创建独立上传／出现绑定；hash＋长度相同但字节不同报HASH_COLLISION，保留原件、不覆盖或合并。重编码／元数据差异产生不同blob；不作感知去重。可跨入口复用物理字节，但只有授权出现可读，不公开去重命中细节。

发布顺序固定为：持久上传意图 → 自有临时文件写完 → 校验和文件同步 → 短事务封口意图 → 同文件系统不可覆盖发布 → 发布目录同步 → 短事务建立READY代次、上传保护和必要审计／原回执。封口持久保存精确长度／SHA-256、临时资源身份、稳定blob_id、拟议generation和原发布键；取得COMMITTED后才发布，未知先原键确认。封口后不再追加或更换字节；未封口退出可要求完整重传，不能把新字节当作旧已确认内容。不能在数据库事务中等待文件I/O，不能先READY后补文件。已有目标只在完整核验确属同blob时复用；不得覆盖未知文件、符号链接或其他库资源。

### 6.2 发布中断与恢复

| 中断窗口 | 恢复与承诺 |
| --- | --- |
| 意图未提交／临时文件未建立 | 不确认上传；原键确认后可重新开始，不清理非自有文件 |
| 意图已提交，写入中／校验前退出 | UPLOADING不作为READY；旧owner隔离后标REUPLOAD_REQUIRED或回收其确切临时文件；不声称部分字节可靠可续 |
| 写完但文件同步未确认 | 不发布、不确认；原owner结束后重新核验完整字节并完成本地同步，不能仅凭长度猜成功 |
| 文件已同步，封口未提交／确认丢失 | 先查原封口键，未确认不发布；可靠未提交且原owner结束后重新核验完整文件并以原输入封口，字节改变则冲突 |
| 封口已提交，发布前 | 凭持久意图／摘要／资源身份幂等发布；目标若已存在先比较，不覆盖 |
| 发布完成，目录同步前或确认丢失 | 文件可能存在但尚无READY；保留意图，核验后补同步，不向接入签发可用引用 |
| 文件／目录已同步，READY事务前／回滚 | 存在可恢复孤立文件；恢复优先确认原READY键、在保护下补齐本地事务或将明确无主产物交GC；事件不能确认 |
| READY COMMIT后，回执未送达 | 按原key／意图查回同blob／generation／绑定；不重写文件；原执行者未结束则仍保留清理占用 |
| 上传READY，事件T01未提交／确认未知 | 上传保护有效期内可建立事件引用；T01未知时仍保留协调保护，先原键确认，不能按超时GC；T01已提交返回原事件序列 |
| 已确认业务引用的文件缺失／损坏 | 完整性故障，阻止相关读取／新提交／新模型；保留引用和错误证据，不转MISSING理解、不重下或自动生成替代文件 |

文件／目录同步及进程终止实验只证明指定本机资源行为，不是掉电或介质损坏保证。没有可靠隔离旧owner就没有接管发布／清理资格。

### 6.3 引用连续性

引用主键为`(owner_kind, owner_id, blob_id, generation, occurrence_id或null)`，owner_kind封闭为UPLOAD／EVENT／PREPARATION／BATCH／CANDIDATE／SOURCE／OBJECT／PROCESSING／READ；审计和Provider交接均不在保护集合。计数可作派生加速，但是否可删以受限查询中的真实引用／保护行及代次为准，负数或不一致是完整性故障。

| 生命周期动作 | 引用效果 |
| --- | --- |
| T01接收 | 同UoW校验READY和上传绑定，建立独立出现及EVENT引用，撤销已消费上传保护；事件同键命中不再次附着 |
| 冻结／候选 | 在清单保存时取得BATCH／CANDIDATE所需保护；共享EVENT仍由原文所有权保护，不重复发同一出现 |
| 成功／零结果／普通失败／拒学 | 依§4提交source／对象引用与轮转释放；仅被新H持有的失败媒体继续保护；拒学也走定时GC |
| 梦境回流 | 同事件及其EVENT引用保持，位置和cursor同事务转移；如实现显式位置引用，先在同UoW建立正常引用再解除梦境引用 |
| 对象遗忘／删除、source最后释放 | 遗忘不释放；删除只释放该对象持有的边；最后source释放才解除其成员和媒体版本保护 |
| 内部理解／原件读取 | 先在短事务取得PROCESSING／READ保护，再在事务外读文件；完成并确认工作者结束后释放，不把等待超时当释放 |

### 6.4 GC状态机与新引用竞争

blob物理状态为PUBLISHING／READY／DELETE_PENDING／DELETED／FAULTED；generation每次从已删除状态重新发布递增，旧上传回执只表示当时状态，不能使旧generation重新可读。理解文本及其元信息可以保留复用，但没有业务引用时不单独保护二进制；旧审计指针可失效。

GC按固定页从候选索引读取，持同blob发布／清理互斥所有权；短事务再次核验READY、expected_generation、无业务引用、无活跃或未知保护、已过无引用宽限，CAS为DELETE_PENDING并持久记录gc_operation／文件身份。此后新attach／pin必须拒绝BLOB_RETIRING或等待一个新的明确操作，不能加引用后让旧GC继续删。若新引用先提交，CAS条件失败，GC跳过。检查和标记之间不留可插入引用的空窗。

DELETE_PENDING确认后才在事务外unlink已绑定的确切物理代次并同步父目录；成功后本地事务写DELETED、gc回执和审计。unlink前退出、unlink后确认丢失、目录同步未确认、DELETED事务未知均凭同gc操作恢复，先核验文件身份／状态，不将其他文件的NOT_FOUND当删除成功。该操作已确认拥有的文件确实不存在可继续同步／补本地DELETED；文件存在则重核身份后继续。DELETE_PENDING期间不复用同物理位置发布新一代；完成DELETED后新上传才可发布generation+1。避免迟到unlink删除新文件。

上传／处理保护的时间仅决定“可检查”，不是删除许可。活跃线程／连接／文件句柄、未决T01／发布／GC、Provider UNKNOWN即使超过保护期仍保留；旧进程可靠退出后按恢复表确认，必要恢复工作有自己的持久保护。永久阻塞保持槽位／根目录占用，不增生替代worker；没有“强制释放”按钮。

清理仅由媒体所有者执行，其他模块只释放本模块引用。过期但未绑定上传在owner结束且不存在未决操作时先原子标ABANDONED，再交统一GC；临时孤儿只清理可证明由本次资源身份生成的登记文件。未知路径或无法归属文件报告异常，不递归清空目录。GC与发布共享有限文件执行资源并公平排队；GC页超时保留进度和实际占用，不拖长调用方期限。正常接收失败不得触发紧急删业务文件。

<a id="ports-permissions"></a>

## 7. 必要公开端口、权限投影与安全错误（已批准）

### 7.1 能力与读取

| 端口／获得者 | 范围及结果 |
| --- | --- |
| memory的`apply_change_set`／内部批次协调者 | 仅同库、绑定候选／work／owner代次的UoW参与；返回STAGED，不能自己commit。候选提出者不直接写当前对象 |
| `replace_current`、`set_scores`、`delete_object`／可信内部维护工作或单独memory.maintain能力 | 显式操作键、expected_revision、类型化命令／固定理由；独立完整事务及必要审计。普通宿主／观察者不授予，正文中的“管理员”不生效 |
| `release_source_holder`／拥有该holder的内部工作参与能力 | 仅释放本工作实际持有的source关系；若最后释放，按§4.4协调全部所有者及固定计划。不能释放其他对象／工作，也不能因持有source_id获得删除权；对外无独立清理路由 |
| `get_current(id)`／memory.read_current | 当前ACTIVE对象、双指标、世界范围及安全出处元信息；未命中／遗忘／已删均无正文；不包含完整窗口或审计 |
| `get_for_deep_read(id)`／内部审查或独立memory.read_forgotten | 可读现存FORGOTTEN且标原状态；零强化、零恢复。这是按ID存储读口，不宣称已完成深度检索服务或使用反馈 |
| `read_source_manifest(source_id)`、`read_source_member(source_id, ordinal)`／内部source.review或独立开发者source.inspect | 仅已授权对象所持的完整source，先验证对象／来源访问范围；按不可变清单点读，返回完整性／总数及固定成员。内部work只取得自己冻结材料；外部无来源窗口入口 |
| `read_basis_status(ids)`、`list_dirty_dependencies(query)`／内部审查 | 当前ID／修订／生命周期或墓碑元信息，固定页；不返回旧正文，不运行推理。query无任意表达式 |
| 上传三端口、`resolve_upload(key, original_intent)`／绑定入口媒体能力 | 上传进度、READY原回执或明确未确认；授权先于命中查询。确认不上传／理解，原件字节由调用方在需要重传时提供 |
| `read_occurrence(id)`、`read_original(id, offset, length)`／内部材料工作或独立开发者media.inspect | 授权出现的元信息／限定块字节；先取得读保护，范围、并发、期限受限。HTTP观察层不取得此能力，无裸hash／路径下载 |
| `prepare_interpretation`、`claim_preparation`／运行签发的准备／media工作能力 | 按§5.3绑定持久准备、唯一逐出现工作及原请求；获得持久解释快照或WAITING／UNKNOWN／SYSTEM_BLOCKED，原trigger可读INVALIDATED／EXPIRED处置；实际理解一律经Provider，调用后不自动发学习 |
| `collect_unreferenced`、`recover_media`、`close`／可信生命周期编排 | 只有媒体自己的清理／恢复；恢复零模型；close返回CLOSED或INCOMPLETE并报告真实占用 |
| `read_memory_media_status(query)`／独立观察能力 | 有时点的计数、阻塞原因、版本／结果来源、GC进度及清理占用；无对象正文、来源窗口、内容hash、路径、原始主体或恢复句柄 |

可信装配绑定instance、对象／入口集合及操作集合，签发原生封闭能力。跨入口正式对象是否可读与原始缓存授权分离；当前对象读取可按既有产品规则跨入口，但只返回`source_type, platform_id, entry_id（获准元信息范围）, batch_id, occurred_range, target_count`等安全出处，不附H／R正文、理解或媒体下载能力。无权查询先拒绝，不借对象存在性、blob命中／全局计数／游标泄露其他范围。开发者审计读取不隐含授予外部agent。

普通对象读取和维护均走现有业务门控，PREPARING／FOCUSED拒绝DREAMING；有效内部梦境工作仅在原授权范围内可读／写，不能靠字符串角色越权。独立开发者只读观察仍可用；完整来源／原件审查也须明确独立权限，不通过观察默认打开。原键确认是受限恢复读取，不生成业务效果，不解除门控。source清单／成员每次读取须在同一短读快照内重新核验授权对象的当前关联；页间不持长事务，若最后持有者已释放source，后续读取明确SOURCE_CHANGED，不拼出一份伪完整来源。原件流读取另有持续READ保护。

最小观察可扩展现有`/status`及固定GET `/api/observe/memory`、`/api/observe/media`，沿用测试会话、刷新／字节／并发限制；对象正文、上传、维护、完整来源及审计正文不增加Web路由。观察按授权scope在服务端投影，局部入口只见局部数量；实例全局GC／存储统计另需实例权限。组合数据带各自observed_at／revision及COMPOSITE_OBSERVATION；未知为UNAVAILABLE或STALE，不填0。计数使用固定索引／有界维护摘要，不能每次刷新扫描全部正文。

### 7.2 封套和错误

持久写复用COMMITTED／NOT_COMMITTED／REJECTED／UNCONFIRMED四分支及原确认含义；只读FOUND／NOT_FOUND／FAILED；上传追加为明确VOLATILE_PROGRESS，媒体工作为WAITING／UNKNOWN／RESULT_STORED。不能把EMPTY、零候选或读不到当作错误恢复后的成功。返回值深不可变，固定错误恰为`code, operation, field, reason, cleanup_pending`；无自由message、嵌套底层错误、路径、SQL、原文、hash或权限对象。

新增独立MemoryError／MediaError，不扩写旧PersistenceError／ProviderError。operation限本节对应公开语义名；field限capability/state/input/object/revision/source/candidate/media/upload/interpretation/configuration/storage/query。所有端口先核验能力／授权，后核验生命周期允许操作、精确载体与格式，再查原键，只有新工作才查当前模式、修订及新写限额。错误先后按此顺序，同记录按Schema顺序，同列表按索引；只保留首个原因。

| code | 完整reason集合及适用行为 |
| --- | --- |
| INVALID_INPUT | INVALID_SHAPE、INVALID_IDENTIFIER、UNSUPPORTED_VERSION、LIMIT_EXCEEDED、INVALID_SCORE、INVALID_ANCHOR、INVALID_STATE_COMBINATION；有输入端口，分别定位input/object/source/media |
| ACCESS_DENIED | BINDING_MISMATCH、OPERATION_NOT_GRANTED；所有受限端口，field=capability，不查无权对象 |
| PRECONDITION_FAILED | REVISION_CONFLICT、OWNERSHIP_CHANGED、WINDOW_CHANGED、WORK_FENCED、BASIS_UNAVAILABLE、SOURCE_CHANGED、OBJECT_DELETED、NO_CHANGE、BLOB_NOT_READY、BLOB_RETIRING、UPLOAD_EXPIRED、OFFSET_MISMATCH；对应新写前置、准备成员改变或来源分页中关联变化，field=revision/candidate/source/object/media/upload |
| IDEMPOTENCY_CONFLICT | CONTENT_MISMATCH；写／原键确认，field=input；不覆盖合法旧回执 |
| CAPABILITY_UNAVAILABLE | OWNER_MISSING、FORMAT_NOT_SUPPORTED、BUSINESS_NOT_IMPLEMENTED；初始化或新工作，field=capability |
| MODE_BLOCKED | DREAMING、RECOVERING、RUNTIME_FAULTED；业务读／写／媒体准备，field=state |
| RESOURCE_BUSY | ADMISSION_FULL、OWNER_ACTIVE、LOCK_BUSY；I/O准入，field=state；不排无界队列 |
| CONFIGURATION_UNSUPPORTED | SNAPSHOT_REQUIRED、DEFINITION_MISMATCH、CAPACITY_INSUFFICIENT、FORMAT_BINDING_MISMATCH；装配／初始化，field=configuration |
| STORAGE_FAILED | WRITE_NOT_COMMITTED、COMMIT_UNCONFIRMED、READ_FAILED、FORMAT_UNSUPPORTED、INTEGRITY_FAILURE；持久端口，field=storage；分支另按提交证据决定 |
| FILE_FAILED | NO_SPACE、READ_ONLY、IO_FAILED、SYNC_FAILED、RESOURCE_IDENTITY_MISMATCH、HASH_COLLISION、CONTENT_MISSING、CONTENT_CORRUPT；媒体文件端口，field=media/upload；损坏阻止相关能力就绪 |
| TIMEOUT | DEADLINE_EXCEEDED、PREPARATION_EXPIRED、OCCURRENCE_EXPIRED；I/O／准备端口，field=state；后两者是工作期限观察，不伪造理解FAILED或推断底层结束 |
| INVALID_STATE | NOT_READY、SERVICE_CLOSED、SERVICE_FAULTED；所有状态受限端口，field=state |

媒体Provider普通结果是§5的领域状态，不把整份ProviderError透传。Provider Pending映射工作UNKNOWN并保留原请求身份，准入阻止映射WAITING，账本／读取故障映射SYSTEM_BLOCKED；仅核验过的持久敏感终态可生成INTERNAL/REFUSED及跨事件保护，外部出现REFUSED保持报告性质。WAITING/EXISTING_PREPARATION是已授权触发命中现有预约的状态，不是错误或新准备成功。下层存储结果按表中固定原因映射，已存在首错不被回滚／关闭错误覆盖；证据不足提升UNCONFIRMED，cleanup_pending按真实占用保留。COMMITTED之后清理故障只进入新健康观察，不撤销原回执。

<a id="capacity"></a>

## 8. 新材料与跨层容量核算（已批准；容量须实测）

### 8.1 可复核的新格式

新测试材料绑定`participant=bounded_learning, event_format=source_event_json:2, material_format=complete_source_base64:1, template=target_source_records:1, bound_rule=complete_source_bound:1`。它用于测试实际所有者的完整材料与恢复，不声称正式认知质量。配置只引用可信固定声明；旧synthetic_window_bound不适用。

每个完整源成员先编码为一个有界JSON记录：`header`包含message_id／entry_seq／接收／移交时间／原文摘要及所选理解ID，`event`为完整原始事件，`interpretations`为按媒体序号排列的全部选用记录。header连同外层键、逗号、括号的完整开销上限K=1024字节（固定至多4个128字节ID、2个64字节摘要、4个19位整数、枚举与键标点合计不超过308字节）；每事件至多A媒体、每选用理解完整编码≤I，event≤E。于是D≤K＋E＋A×I。缺项不伪造空成功，RAW已有外部理解仍在event内，另计选用记录是保守计重，不漏算。

GENERATION恰SYSTEM／USER两条。SYSTEM.text固定为以下ASCII行加一个LF（字面量98字节，核算采用≤256字节保守界，没有隐含其他prompt）：

```text
Verification records only. Decode full sources. Learn target messages only; context is auxiliary.
```

USER依次为`material=2`行，instance／host／platform／entry／batch／run／config七条`key=<真实ID>`行，然后H→T→R成员行，最后`end`行，全部以一个LF结束；ID最大128 ASCII。成员行恰`角色|message_id|entry_seq|received_at_utc|源成员JSON的标准Base64`加LF；时间固定27字节UTC。Base64含必要填充，无换行，源记录JSON内嵌为结构、不先转成字符串再重复转义。原事件、引用、来源、选择的理解均可逆还原。

头960、尾4、SYSTEM保守256，B=1220；每项引用为1＋4＋128＋19＋27＋1=180。新上界`W(E,A,I)=180＋4×ceil((1024＋E＋A×I)/3)`，`Q=B＋(H＋T＋R)×W`，模拟输入units同两text UTF-8总量。固定上界余量不得用于拼接其他记忆／persona／工具历史；任何新增材料需重新绑定并核算。

### 8.2 推荐包代入与失败边界

具体可调值只在[配置表](configuration.md#formal-memory-media-configuration)维护；下表代入其推荐包。格式上限在[存储补充](persistence-and-transactions.md#formal-memory-media-storage)维护。

| 项目 | 静态上界／结论 |
| --- | --- |
| 满窗口材料 | E=2048、A=2、I=2048、N=4；D=7168，Base64=9560，W=9740；Q=1220＋4×9740=40180≤49152，余8972。N=5时49920>49152，新配置拒绝，不以首次无H通过 |
| 模拟生成完整请求 | 新材料仅ASCII和LF；满窗口14个LF，外层最多8个标量ID和1个entry、原固定封套／payload合计≤4096字节（原同形字段账目1437以内，此处含版本预留）。请求≤40180＋14＋4096=44290≤65536；不把request_max_bytes当输入units |
| 媒体理解请求 | 原字节≤1048576，独立byte计量，不Base64写入生成请求。Provider媒体语义请求只含绑定身份／hash／长度／任务，完整编码≤4096；内存原字节另计。1 MiB＋1整体拒绝上传；大视频不会被静默切片 |
| Provider结果及理解 | result_max_bytes=8192保持；512字节可变理解text的Provider规范化封套仍按3072＋2048=5120预留。media自己的精确19字段封套见§5.4：内部EMPTY1302、FAILED1327、REFUSED1331均≤I=2048；最长元信息＋512普通字节1817可保存，512个C0则4377并转固定1327字节FAILED。不是把Provider8192上限当media2048可用空间；生成合成回复仍≤1024 |
| 来源逻辑大小 | 一份完整source最多4×7168＋4096清单=32768字节；原始payload共享保存，不能把此数当每表都要复制的物理大小。单次点读事件或一个解释／清单，不一页读4份嵌套正文 |
| 准备与原请求关联 | 准备清单4096＋8×（工作4096＋原请求描述4096）=69632逻辑字节；独立叶点读，不把该总量持久为一个body。即使按一次同包保存核算，6×69632＋65536=483328≤1048576；实际按协议分登记／领取／关联步骤确认，原件字节另存文件 |
| 候选 | 最多8个变更叶，每叶完整编码≤8192，清单≤4096；8×8192＋4096=69632≤逻辑总限73728。一次stage事务保存全部叶；总命令最坏6×69632＋65536=483328≤1048576，预留包括完整command_descriptor和意图。最多8正式结果不受T=2限制，9项拒绝而非自动拆批 |
| 共享释放计划 | 每对象2个旧source、8对象得16叶；每叶512＋4×384＋8×768=8192，完整计划16×8192＋4096=135168。计划保存命令6×135168＋65536=876544≤1048576。执行只引用已保存计划；候选／计划／旧历史不在同一命令重复编码，未知计划不得临时换分支 |
| 当前对象及审计正文 | 当前完整正文快照≤4096；对象关联在有界独立叶中；一次按对象点读≤6×4096＋8192=32768≤65536。历史正文每件≤8192，最多8件，整体写命令与候选同类上界；修改不双存旧正文到memory |
| 单叶／固定点读 | 最大有正文叶8192，最坏编码6×8192＋8192=57344≤receipt_max_bytes=65536。不能将49152材料、69632候选或135168计划写入一个通用body再声称点读安全；材料从固定叶重建 |
| 元信息页 | 最多16行，每行完整编码≤1024，页封套≤4096，20480≤32768观察响应且≤65536底层读取；真实HTTP编码再限容。正文读取每次一成员／一对象，不适用16份最大正文 |
| 回执与证据 | 新终结最多8对象结果引用、1个新source及16个退役source ID，其他释放按安全计数和计划关联表达，完整安全结果≤8192；通用回执含身份、指纹和描述关联预留8192，总≤16384≤65536。结果绑定必要清单／意图独立≤32768≤65536，不把释放计划全文或正文混在一个回执结果 |
| 必要审计 | 最多8必要slot，实际摘要格式每slot≤4096，8×4096＋8192完整操作封套=40960≤65536；event配置8192和每操作16slot不扩大本包实际格式。历史正文逐件点读，见日志补充 |
| 配置初始化 | 新增两个总期限键及修改后的I／材料／profile值都计入同一个4域、128条、单条8192、body合计262144的准入包，不额外放宽。参数键各≤128、域目录≤8192。body按配置补充版本2转义C0和DEL，嵌入原基础ensure_ascii至多3倍；完整命令仍3×262144＋128×(128＋128)＋6×8192＋65536=933888≤1048576。最后65536含完整command_descriptor、意图和域元信息；实际声明编码须证明全包满足这些限额，不能把本轮公式当配置解析已通过 |
| 静态存储装配 | 基础assembly_value还有1048576硬限。新完整装配限定命令描述总编码≤655360、仓储Schema／DDL描述≤131072、外层≤8192，合计≤794624；所有旧Provider与新运行命令均计入，不能只计算values。该预算须在静态声明装配时逐字节验证，超出则不得CREATE_NEW |
| 并发与内存 | 上传2×64 KiB块=128 KiB，不把全部上传驻留；理解并发1、原件≤1 MiB，Provider及适配器允许至多3份有界字节副本，按3 MiB核算，另有1个≤73728候选、≤49152材料及≤135168计划。读原件并发2×64 KiB=128 KiB；这是有效载荷界，不是Python RSS保证 |
| 模拟费用／次数 | learning最多49152×2＋2048×3=104448 atoms/attempt；media最多1048576×1=1048576。满窗最坏8媒体各2attempt＋生成2attempt：16×1048576＋2×104448=16986112 atoms、18attempt，均小于新测试账户预算；期限／门控可能更早停止，无自动重置，不保证长期积压永远有额度 |

总编码上限是准入约束，不是用预留字节掩盖未知字段的证明：实现须逐字节验证各固定封套≤其预留，超出即实现不符，不能只依赖最终拒绝来声称推荐最大包可用。上面的6倍是UTF-8 JSON最坏转义保守界，Base64材料只在已经完整计入内层后按实际字符集核算。

理解容量不再使用一组短ID样例推断所有状态可写；[§5.4](#interpretation-envelope)给出最长合法元信息的穷举上界及固定失败构造。边界例：最长INTERNAL COMPLETE元信息1305＋L，I=2048时L≤743，L=744即超限；COMPLETE的text还须≤512原字节，因此512普通ASCII合法，而124个U+0000的转义内容744字节会超完整记录，保存固定FAILED。REFUSED的固定24字节不受text配置降低影响。以上为格式核算；实际编码器与Provider的验证结果只在CURRENT_TASK记录。

边界例：沿用最小无媒体事件的245字节空body结构，新event_version仍为一位数，仅普通ASCII正文时E留下1803字节；3字节汉字最多601字，六字节控制字符最多300个。加入媒体描述、长主体ID、原始引用共同占E，所以不承诺同时达到每字段最大值。A=3即便总事件能装下也拒绝；I配置1330或2049、单候选叶8193、第9叶、总候选73729、释放计划第17叶均拒绝；实际理解记录2049字节按外部接收拒绝／内部结果失败分别处理。单字段恰到上限而完整记录超限仍不能截断。

### 8.3 累计磁盘与能力限制

二进制容量按唯一blob数U计算：`U×1 MiB`；逻辑原始事件按N_events×E，source按仍持有的不可变成员／解释及清单，当前对象按O×4096，历史正文按实际修订次数累加，候选只对未终结工作长期保护。举例1000个最大唯一blob约1000 MiB，10000次各2 KiB事件约19.53 MiB；若它们全引用这1000个blob，不因出现次数再乘文件容量。10000份各32768字节逻辑source上界约312.5 MiB，10000当前对象约39.06 MiB；源原文共享可减少物理字节，不能据此承诺实际SQLite文件大小。准备／逐出现恢复元信息、不可变理解及释放计划历史另按真实记录数累积，不能因source释放便从磁盘预算中扣掉这些保留项。

回执、Provider交接、必要审计、墓碑和未处理依赖仍随验证库增长：若100000次操作每次按16 KiB回执＋32 KiB证据＋64 KiB审计摘要保守包络，约10.68 GiB，另加历史正文、账本、页／索引／WAL／临时文件。该包络不是每次实际开销或存储上限；WAL checkpoint阈值不是磁盘硬上限。未决操作和有业务引用内容不为腾空间自动删除。

完整回执／审计启动扫描仍受一次存储期限，库增长可持续无法READY；新运行／媒体恢复分页不能跳过该检查，重启不自动提高持久配置值。新包推荐较长存储等待仅适用于明确新库，不是大库性能保证。没有数据库／审计总保留政策、线上扩容或故障解除承诺。本次容量验证以实际证据为准，不承诺RSS、延迟或掉电保证。

<a id="assembly-recovery"></a>

## 9. 新旧装配、版本与恢复（已批准）

具体Schema、命令版本与兼容规则唯一见[持久化补充](persistence-and-transactions.md#formal-memory-media-storage)。推荐新完整装配在新自有临时资源CREATE_NEW，随后同清单OPEN_EXISTING；旧库用对应旧装配读取。新装配缺任一真实所有者即相关能力不可用；不能默认装SyntheticMedia／SyntheticLearning或将旧合成结果转为正式对象。

恢复仍先验证原库身份和旧owner隔离，依次存储READY、完整配置、Provider本地恢复、媒体本地发布／引用完整性、记忆／来源／候选完整性、运行工作恢复，最后解除RECOVERING覆盖层。Provider与媒体的启动依赖分开：Provider先仅恢复账本，不取得媒体发送能力；媒体在恢复旧请求时再签发受限原字节查询能力。新服务媒体根身份和期望库绑定必须预先保留，不能读取实际库ID反向当期望。

媒体／来源恢复采用固定元信息页及有界单记录核验，checkpoint绑定库、模块版本、配置、根资源身份、generation和扫描水位。跨进程必须重新验证当前一致性，不能信任旧“已扫描”跳过被修改行；总期限到时RECOVERY_PENDING，未结束任务保留owner、连接和文件根。启动要求每个被引用READY文件存在且长度／摘要匹配；文件多时可多次显式推进媒体层恢复，仍不延长或绕过存储initialize自身全检。恢复不通过模型重建缺失文件或理解。

| 已持久事实 | 恢复动作 |
| --- | --- |
| 已接收原始事件／梦境输入 | 恢复原序列／位置及EVENT引用，确认同一物理代次；不重发学习、不改发生时间 |
| 准备已登记／已领取、窗口后到输入或已失效 | 按§5.3恢复同一预约／窗口／消费者／原请求及期限；尾部追加不改选定成员，失效释放不丢PROCESSING保护，旧owner隔离后才接管本地工作 |
| MEDIA工作已关联原Provider请求但无返回 | 用原操作键／完整请求重建lookup_request；再由原结果所有者recover_result。NotFound不发送，PREPARED按Provider UNKNOWN处理 |
| Provider交接已确认、媒体或认知结果未保存 | 确定性本地保存原结果，ID／版本不变；需要旧blob原件时已有PROCESSING保护不可提前释放 |
| 候选已保存／finalize未知 | 原键确认；有回执返回原对象／source／终态；可靠未提交才重做原本地事务，修订冲突阻塞 |
| 三终态已提交且source／媒体还有其他owner | 不重新消费或释放；按当前引用继续保护，原终结回执不从当前对象重算 |
| 删除／GC／文件发布中断 | 按原操作、代次和具体中断窗口恢复；无正文墓碑不被候选或审计恢复成对象 |
| 删除／来源释放计划或执行确认丢失 | 从root及已保存计划重建原执行键／固定分支；未确认不得重规划，已成功返回原业务回执；仅可靠未提交后可在后续显式本地调用重算holder形状 |
| 模式为PREPARING／FOCUSED／FAULTED | 沿[既有模式恢复](durable-ingress-and-batch-runtime.md#recovery)继续关闭；媒体本地完成不代表梦境完成或故障解除 |

推荐兼容测试用基线旧程序在临时库建旧装配，未来程序以对应旧装配重开、原键确认并继续旧操作；新装配另建独立库验证关闭重开和跨进程恢复。新／旧错配必须拒绝且文件／库不被自动变更。旧AVAILABLE为空串等原本合法数据保持旧格式事实，不自动变成新EMPTY、触发新模型或更改旧指纹；新服务若须读取旧素材，只能通过显式版本适配视图且不能发布为新正式结果。本文不授权该存量转换路径。

如以后必须迁移，单列：旧库完整装配／格式及真实数据范围、原接收／回执／审计／Provider未知责任、旧合成表处置、文件引用与根目录身份、备份、离线副本验证、维护窗口、切换／回退中新接收数据保全。用户须决定是否迁移、迁哪些记录、如何保留合成性质及切换权威；不自动补表、清空旧库、复制旧备份冒充无损回退或将旧合成结果转正。

<a id="acceptance"></a>

## 10. 一次整体验收矩阵

实施与验证已获用户授权；验收结论须由监督技术审查确认。真实服务必须通过公开端口贯通，不只测私有函数。文件与SQLite采用测试自有临时资源；故障注入、真实资源错误、跨进程终止、模拟模型和静态核算分别记录证据，不相互替代。

| 项 | 场景／验证方式 | 必须同时成立的断言 |
| --- | --- | --- |
| M01 | 正常含媒体的完整接收→媒体准备→满H/T/R→多记忆／关系／主体→终结→新服务按ID读 | 原字节一致、独立出现、完整source可还原、T锚点正确、分数／修订／范围正确、回执和审计同提交；模型与候选来源如实标记 |
| M02 | 轮转释放队列后读取共享source；同一R后来成为T | 冻结原文／理解版本不变，原ID／时间保留，不因曾在来源中就跳过学习，不把跨批复用当独立证据 |
| M03 | SUCCEEDED零结果、普通失败、明确学习敏感拒绝，各加新输入／其他入口／共享source | 三终态一次；成功与失败留T尾，拒学清本入口H；无永久空source、无失败候选发布，不误删R／后到／共享媒体 |
| M04 | 每个所有者写后、来源建立、媒体引用转交、审计／历史正文、回执写入及COMMIT前故障 | 确认后全无或全有；存储失败不变FAILED_DROPPED，未确认保持目标与候选保护；捕获参与者异常也不能提交 |
| M05 | 两命令基于revision=1竞争修改；候选保存后被维护命令修改其依据 | 一个合法变更，另个整组冲突；不覆盖、不部分发布、不重跑模型；SYSTEM_BLOCKED可见且不是第四终态 |
| M06 | R=19/20/34/35边界，ACTIVE／FORGOTTEN分别输入；低belief高R；纯读／重复同键 | 严格F/H比较与历史滞回，连续遗忘时点正确，读取不计分；无自动衰减／反馈宣称 |
| M07 | 删除共享source的一个对象、再删最后持有者；使用旧ID／旧索引／旧依据／审计指针 | 墓碑无正文，普通／深读不可复活；他对象来源仍在，最后引用的原文／理解／媒体按§4.4处置；依赖待办持久，persona不假更新 |
| M08 | 跨平台同名、低置信SAME_SUBJECT、PLAYS_ROLE场景、支持／引用边、缺失或过时依据 | 主体不合并、不升级权限；现实／扮演不混淆；支持语义区分，当前依据校验，变更待办不无界同步传播 |
| M09 | 外部COMPLETE／EMPTY／PARTIAL／FAILED／REFUSED及所有非法字段组合 | 完整／空零内部调用、部分不自动补齐、普通失败非成功；外部拒绝只影响该出现、不能写保护；非法新事件在接收前拒绝 |
| M10 | 同blob同scope内容复用、不同事件情境、不同模型／prompt／任务、外部解释 | 满足键才复用，情境不串用；外部结果只用于其事件；复用不新增Providerattempt或费用 |
| M11 | 普通媒体失败后同事件重投／恢复／回流／换窗口，对照新事件；Provider敏感拒绝后换事件／profile | 同出现无业务重试；新事件可按明确规则申请；已核验敏感保护不被自动绕过，新外部结果不清旧拒绝 |
| M12 | 专注输入缺失媒体；入梦与媒体dispatch两种先后；结果迟到、UNKNOWN | 只存原件／已有结果，先闭门则零发送；已启动只本地收尾，不启动学习；UNKNOWN保护／FAULTED语义不被TTL解除 |
| M13 | 相同原字节多次／多入口上传，同事件两次出现、重投；不同编码；强制hash碰撞夹具 | 一份物理blob、多独立出现；重投一次；不同字节不误合并；碰撞拒绝且不覆盖 |
| M14 | §6.2每个发布窗口，以真实文件和同步边界／父子进程屏障逐项终止 | READY和T01确认不指向未发布文件；孤立文件可恢复／可清理；原意图与原键不依赖收到任何响应 |
| M15 | GC CAS之前／之后建立新引用，unlink前／后退出、目录同步／DELETED确认丢失，再上传新一代 | 新引用赢则不删；GC赢则新attach拒绝；迟到旧GC不删新generation；同gc键不重复释放 |
| M16 | H、梦境、source、FORGOTTEN、PROCESSING／READ／UNKNOWN、仅审计分别作唯一持有者 | 前述业务／处理中不删，只有审计可清理；过期但活跃不夺权；未结束读者保留资源 |
| M17 | 真实SQLite受限页造成SQLITE_FULL、真实只读资源／受控文件写失败；同步失败、读缺失／损坏、关闭阻塞 | 固定安全原因与正确提交分支，原文不丢、无假READY／空结果；有界返回但占用如实；不填满宿主磁盘 |
| M18 | 候选／T01／T03／删除／GC COMMIT后任何返回前终止子进程 | 新解释器仅凭原身份／意图恢复相同回执；对象ID、计数、source、费用、轮转至多一次；零模型调用 |
| M19 | COMMIT前退出、旧读快照NotFound、旧writer在途、读库失败与迟到完成反向排序 | 只有完整确认条件允许本地重做；UNKNOWN不重放；COMMITTED不被迟到失败降级，资源清理与提交事实分离 |
| M20 | 最大合法事件＋2理解＋满窗口；Unicode／引号／控制符、Base64三余数；完整请求编码 | 逐字节还原完整原事件／解释，验证B/K/W/Q及完整请求预留；不借旧7249合成账目证明新格式 |
| M21 | 8叶候选／第9叶、字段边界／总记录超限、结果／回执／必要证据／审计／配置域总容量±1 | 全部边界明确拒绝／接受、失败原子；最大推荐包真实可写可读；不分终结、不截断；原审计权限不放宽 |
| M22 | 无权／跨库／跨scope／伪造／过期能力，猜object/source/blob，含特殊HTML／控制符正文 | 授权先于存在性，当前对象不泄完整窗口；agent无审计，观察无正文／hash／路径；专注拒绝业务但受限观察可读 |
| M23 | 基线旧程序／旧装配真实临时建库，未来旧装配重开；新旧装配双向错配，新库跨进程恢复 | 原键／字节解释不变；不兼容拒绝无迁移／补表；新真实服务不导入旧合成结果 |
| M24 | Provider登记未知、完整交接后媒体业务落盘前退出、纯内存结果丢失 | 只恢复真实交接，未知不再发；新媒体字节能力有限且释放，不泄文件权，不重复计量 |
| M25 | 并发上传／理解／读取／GC饱和，任务取消、期限到达及永久阻塞；多轮结束后观察 | worker／Task／句柄／保护有界可回收；真正未结束者仍占槽，公平且无无限后台队列；根不被提前重开 |
| M26 | 增长库启动全检超时，媒体恢复多页及进程退出；日志关闭／诊断失败 | 不绕过存储就绪、不假续存储cursor；保护和恢复位置正确；审计独立、诊断不撤销COMMITTED，不自动解除故障 |
| M27 | A外部REFUSED→同字节B缺失→B的Provider敏感终态→C缺失／D外部COMPLETE；伪造request／跨库／跨域；保护与dispatch双向竞争 | A不建立保护，B可首发；B原终态权限完整核验后与保护同提交，C零请求、D仅用外部报告；内部旧成功缓存不覆盖保护。保护先则start=0，dispatch先只收尾一次且无下一attempt；固定拒绝不是整批拒学，所有来源／审计如实 |
| M28 | 两trigger／两claim竞争；同事件两媒体位置；准备中尾部追加对照H／T／R变化；各登记／领取／原请求关联／结果／冻结／释放点前后中断；入梦双向竞争、总期及墙钟回退 | 每入口一个准备／目标预约、每出现一工作；追加不重复已付费理解，窗口变化不重发同出现；原键回执／唯一claim／模式与selection修订可恢复，未知只查原请求。关门先零新发送／冻结，先dispatch保留截点；过期不伪造FAILED，未结束者持PROCESSING且恢复零模型 |
| M29 | S最后holder计划后新增B，及S非最后计划后B释放；H继续持原文后再轮转；无媒体／有媒体／共享解释／独立维护和批次删除；计划／执行COMMIT前后跨进程退出 | 旧分支OWNERSHIP_CHANGED时业务全无；确认未提交后新计划才选正确静态mask，UNKNOWN不重规划。memory／ingress／media及历史审计严格匹配实际变化，buffers未变不写slot；原文最后释放、解释保留、文件仅GC、root至多成功一次；必要集合不得临时扩大或漏项 |
| M30 | 最长合法元信息遍历各来源／状态；I=1330/1331/2048；固定REFUSED且text限额0；512普通字节和512个U+0000；外部报告／完整event超限；内部失败保存时SQLite错误 | 精确验证§5.4各上界及±1；配置不足零副作用，外部非法T01全无，内部普通成功超限仅存1327字节FAILED且原Provider交接不变。敏感固定1331字节不得降FAILED；保底保存失败仍未知／阻塞、不假RESULT_STORED；新满窗40180／请求44290／释放计划876544／配置933888按真实编码逐项验证 |

回归范围按[统一验证规则](../CODING_STANDARDS.md#validation-environment)选择，保持真实临时文件／SQLite、独立进程重新打开及终止恢复的相关保障；全量Pyright覆盖`companion_memory/`、`tests/`及所有新增／未跟踪Python文件，新增维护目录须进入检查范围。按[代码规范](../CODING_STANDARDS.md#python-type-checking)记录实际版本、命令、退出码与诊断数量；Pylance另需实际编辑器workspace诊断证据，不能用Pyright替代。实际执行结果和未验证条件只在CURRENT_TASK记录。
