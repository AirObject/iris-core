# 日常认知、图片理解与目标处理闭环

**批准状态：**2026-09-15用户要求继续推进并交付整阶段实施prompt，已明确批准受控主体登记、保守目标自动合并，以及本阶段合成验证新空实例的指定persona原文导入。本文对既有产品行为的实现细化由监督者在该范围内定稿；新增真实调用包的批准状态单独见[验证包](#live-package)。音频／视频测试按[暂缓决定](../work/DEFERRED_ISSUES.md#audio-video-validation)暂缓。旧质量问题不阻塞，权限、来源、事务和恢复保证继续执行。实施及验收进度只在[CURRENT_TASK](../work/CURRENT_TASK.md)、[STATUS](../work/STATUS.md)维护。

<a id="scope"></a>

## 1. 完整交付与实际基线

实际Git基线、工作区和有效验证只在[CURRENT_TASK](../work/CURRENT_TASK.md)维护，已提交交付见[STATUS](../work/STATUS.md)。旧文本、语义、合成装配继续按原格式打开，新组合不原位迁移旧库。

交付为同一宿主内的“接收文本／图片→逐出现准备→三段冻结→单次结构化加工或有限读取工具循环→稳定完整候选→正式记忆／主体／关系／目标及来源原子终结→异步向量与公开信息查询→关闭／恢复”。包含必要配置、公开端口、Provider持久账本、模式协调、观察和验证；不把内部类、表、协议适配拆成审批阶段。

沿用[批次与自治](../product/batches-and-learning.md)、[输入与媒体](../product/input-and-media.md)、[来源与关系](../product/provenance-and-memory.md)、[目标规则](../product/goals.md)、[上下文边界](context-and-cost.md)。详细原子语义继承[正式记忆契约](formal-memory-source-media.md)，信息／提醒继承[本地信息契约](local-information-feedback.md)，首次persona继承[文本契约](model-driven-text-learning.md)，向量、发布与查询继承[语义检索契约](async-embedding-semantic-retrieval.md)。本组合的明确增量在本文，旧组合闭集不追溯放宽。

不纳入周期梦境、周期persona发布、长期衰减／遗忘调度、内容质量调优、生产迁移、完整管理Web、热配置、生产鉴权或新外部行动工具。使用现有梦境门控及合成整理参与者验证隔离，不宣称梦境能力完整。默认时区的底层固定配置在本组合落实，Web初次确认／修改仍由最终管理阶段完成。

| 实际复用位置 | 本次必要增量 |
| --- | --- |
| [TextHost](../../companion_memory/runtime/text_host.py)、[SemanticHost](../../companion_memory/runtime/semantic_host.py) | 新`DailyCognitionHost`组合；单一配置身份、数据库、业务gate及明确资源所有权，不能套两个各自就绪而互不相通的宿主 |
| [ContentRuntimeService](../../companion_memory/runtime/content_service.py)、[学习协调](../../companion_memory/runtime/content_learning.py) | 新组合显式认知参与者、调度、真实媒体和embedding共存；旧精确类型校验继续，禁止通过任意回调替代可信能力 |
| [文本上下文](../../companion_memory/cognition/text_context.py)、[候选](../../companion_memory/cognition/candidates.py) | 新材料／候选格式支持完整媒体选择、工具轨迹和获准动作，原CREATE_MEMORY格式不改 |
| [MemoryTransactions](../../companion_memory/memory/transactions.py)、[固定释放](../../companion_memory/runtime/candidate_application.py)、[目标参与](../../companion_memory/runtime/candidate_goals.py) | 新真实候选的受限权限、SUBJECT来源持有、混合目标效果、历史与语义dirty在同一事务衔接 |
| [媒体工作](../../companion_memory/media/work_transactions.py)、[Provider端口](../../companion_memory/provider/ports.py) | 原媒体描述符、逐出现原键、保护与结果交接接通真实图片；资源通过公开能力读取，不穿透另一owner私有字典 |
| [LedgerAssembly](../../companion_memory/provider/ledger.py)、[原生embedding](../../companion_memory/provider/embedding_service.py) | 新组合支持按角色判别的完整账本格式，统一账户准入，同时独立归因生成、媒体、embedding和目标判断 |
| [目标所有者](../../companion_memory/goals/service.py)、[信息查询](../../companion_memory/retrieval/query_service.py) | 有界语义判断与保守合并；内部只读查询不签反馈票、不强化、不伪装外部使用 |

## 2. 公开端口与生命周期

所有下列端口是可信装配签发的原生能力，调用者不能凭字符串role、object_id、包ID或任意Python对象制造权限。具体Python模块拆分、私有helper和静态索引命名由执行者选择，公共行为及封闭载体按本文；新源码使用日常认知、推理、图片、学习调度等语义命名，不为已有content前缀做全库重命名。

| owner／端口 | 输入与行为 | 结果及边界 |
| --- | --- | --- |
| runtime：initialize／close | 完整新配置与显式借入资源；CREATE_NEW或OPEN_EXISTING固定 | NEW→RECOVERING→READY→CLOSING→CLOSED；完整验证失败保持未就绪。初始化及恢复不解析凭据、不发送；后续显式resume才允许新工作 |
| runtime：bind_entry／accept_event | 已登记host／platform／entry能力，原键及既有事件格式 | 接收承诺、FIFO、专注暂存及原回执不变；接受不等于立即学习 |
| runtime：request_learning | 原键、entry_id、reason=THRESHOLD/FOCUS/ACTIVE、可选已接收目标上界entry_seq | 调度阈值由系统签发；FOCUS属于入口能力，ACTIVE属于内部运行能力。冻结前合并同入口同覆盖触发，不合并不同入口；空目标返回NO_TARGET且零模型请求 |
| runtime：resume／pause_learning | 可信管理能力、原键及当前epoch | 控制是否准入新学习／媒体／目标判断／embedding；不取消已提交事实、不重置期限／额度。暂停状态持久，重开仍需显式恢复发送开关 |
| cognition：process_batch／confirm_run | 原冻结source、读写许可、当前persona、完整配置、原运行ID | 单次完整输出或有限工具循环；确认只恢复原本地步骤。提交确认与远程未知、实际cleanup分别表达 |
| cognition：read tools | 见§3的闭集及当轮可信范围 | 只读正式当前值，不返回其他入口缓存、开发审计、历史正文、密钥或任意文件；完整有界结果持久后才可用于下一次模型请求 |
| provider：generate／understand_media／embed／lookup／result_owner | 角色、原请求、受控材料租约、固定配置 | 原生登记确认后才发送；同原键只确认。租约绑定实际owner，终态与消费者结束分别确认；不让业务模块直接HTTP |
| memory／goals：apply_candidate | 完整候选、原权限、expected revisions和必要释放计划 | 正式owner执行，拒绝整个无效候选；零输出合法。模型不能直接写库、指定运行ID或扩大权限 |
| management：观察与只读HTTP | 原观察能力、有限页／cursor | 复用version2查询及受限观察。新字段分命名空间：learning、media、goal_dedup、provider；同名键不得静默覆盖。普通宿主只看自身范围；内部工具不获得管理观察权 |

运行错误沿既有固定code／operation／field／reason封套，追加本组合原因`TOOL_LIMIT_EXCEEDED`、`TOOL_NOT_GRANTED`、`INPUT_FORMAT_UNSUPPORTED`、`IMAGE_LIMIT_EXCEEDED`、`MATERIAL_CHANGED`、`SCOPE_CHANGED`、`DEDUP_CONFLICT`、`IMPORT_EVIDENCE_INVALID`。权限拒绝、容量拒绝、普通Provider失败、持久损坏及模式阻断分别映射，不把未知异常改成敏感拒学或成功空结果。生产校验不依赖assert。

调度按入口轮转，单个入口内部按原序；阈值、关注和主动触发共用去重及一个实际学习槽。页扫描64项、有界等待唤醒；无新输入／状态变化不持续写检查点。旧失败目标不因新trigger复活。专注门控切换后已接收输入按原例外保存，拒绝新的非例外业务；已合法启动收尾在原epoch和所有权下结束。总期限自第一步准备开始固定，所有等待／工具／Provider／终结消耗同一期限。超时后不重新给满期；只保留原任务和实际占用，完成通知只对应本次I/O，不能轮询服务全局忙计数回收本次槽。

<a id="reasoning"></a>

## 3. 有限工具循环和输出

默认直接生成最终候选，不预先强制跑工具。工具请求使用模型**完整JSON业务输出**表达，协议仍为Chat Completions；本组合不启用供应商native tool_calls，不需把多轮自动执行权交给供应商。每次请求仍只有SYSTEM／USER两消息，已完成步骤以有界、明确标为工具结果和模型建议的材料放入后续USER，不作为新的用户指令。最多3次生成、2个工具轮、每轮至多2个工具、合计4步；每次生成一个attempt，非法JSON不“修复重试”。工具失败可作为有类型结果交给下一轮，但存储损坏／越权／模式变化停止相应工作，不能绕过。

模型输出闭集二选一，拒绝多余字段、重复JSON键、截断、Markdown包裹和工具／FINAL混装：

- TOOL：`{schema_version:1,kind:TOOL,tools:[{name,arguments}]}`，1..2项。name及arguments精确按下表；不允许模型传request_id、权限、owner、时间或路径。
- FINAL：`{schema_version:1,kind:FINAL,actions:[...]}`，0..8项，动作按§3.2。第3次生成必须FINAL，再请求工具为TOOL_LIMIT_EXCEEDED并普通失败终结，不能把已做的部分动作发布。

### 3.1 读取工具

所有ID最长128字节、revision为1..2^63−1；query≤512 UTF-8字节。工具基于冻结工作许可，末端复核权限／revision，世界取值只能来自原许可或已核验输入，不允许模型扩展。每次工具结果完整编码≤8192字节，最多4对象；少于请求数量、丢弃、缺失／超期须显式原因，不能称完整无匹配。工具结果正文跨重启保留到工作终结，只可内部读取。

| name | arguments封闭字段 | 返回范围 |
| --- | --- | --- |
| search_memories | query:string；world_scope:原WORLD；limit:1..4 | 正式ACTIVE记忆，语义缓存可用则融合，不可用按现行降级；本工具本身不冷发送query embedding，不签recall票、不强化。related结构只读，不把向量作为LLM正文 |
| read_memories | refs:[{object_id,expected_revision}]，1..4 | 原许可可读的当前对象，含MEMORY／RELATION、分数、性质、revision及真实来源根元信息；不含历史正文或完整来源窗口 |
| read_subjects | subject_ids:ID[1..4] | 已授权主体及明确关系元信息；相同昵称不自动解析为同一人；未知ID明确NOT_FOUND，不新建 |
| list_goals | world_scope:原授权世界ID；limit:1..4 | 同入口／同世界的未结束目标、原截止／提前量／提醒去向及revision；不执行目标或提醒 |

每步保存工具名、完整arguments、原读许可摘要、输入／结果摘要、结果记录、实际读到的对象ID／revision、开始／结束时点和类型状态。工具执行前登记步骤，返回后原子存结果；崩溃在结果未存窗口可重新执行该**只读工具**，但已经绑定下一轮材料的结果不得替换成较新值。读取任务迟到时先保留实际清理，再判断原工作是否仍可接收，不启动下一次远程请求。

### 3.2 候选动作与可信转换

共用字段：action、target_anchors（原ANCHOR 1..2项，必须属于本轮TARGET）、auxiliary_refs（0..2，仅HISTORY／RECENT）、basis_refs（0..2，受控当前对象及expected revision，kind为SUPPORTS/REFUTES/CITES/CONTEXT）。所有动作必须有目标依据；引用完整媒体选择时锚点含确切occurrence_id／interpretation_id，必须是冻结选择，不能指向FAILED或REFUSED并声称看见其内容。subject注册依据可引用EVENT及实际发言身份，但不能把昵称当外部身份。

每个action另有`local_ref`（0..7的整数，本候选唯一）。模型以`{existing_id:ID}`或`{local_ref:0..7}`引用主体／对象，二者互斥；引用local_ref必须指向本候选较早的合法创建项，禁止循环。正式ID、candidate_id、时间、revision、retention初始值及请求出处由确定性转换器依据数据库、批次、最终handoff、变换版本和ordinal产生，不能由模型自签。最多8项计入全部主体、记忆、关系和目标，不是每类8项。

| action | 额外字段（全部必填，可空处明确） | 确定性边界 |
| --- | --- | --- |
| REGISTER_SUBJECT | subject_kind=PLATFORM_PERSON/THING/FICTIONAL_CHARACTER/CONTEXT；label≤256；platform_id:ID?；external_subject_id≤512? | PLATFORM_PERSON身份必须逐字匹配本轮目标事件中的平台／参与者身份；非PLATFORM_PERSON的platform_id／external_subject_id均为null。不允许SELF。存在同平台外部ID则复用原主体，不能创建冲突主体或改名；复用映射在候选定稿前冻结并在事务复核 |
| CREATE_MEMORY | 原MEMORY_CONTENT（body≤1024），主体引用采用上述封闭引用；belief:0..100，belief_reason≤256 | 生成object_version=2当前对象及完整来源；retention取配置初始值，不能用模型内容改变当前外部状态 |
| REPLACE_CURRENT | object_id、expected_revision；content:对应现有MEMORY_CONTENT或RELATION_CONTENT；belief、belief_reason | 目标须在当轮可写许可且已读取，整体新当前修订；保留原created_at，retention不变。旧正文及旧来源按正式记忆原释放／历史规则；不能把“换正文”当删除或改身份 |
| SET_SCORES | object_id、expected_revision；belief、belief_reason；retention_delta（−10..10）、retention_reason≤256 | 两分数独立；retention在0..100钳制是量表规则，delta及原因须保存。不改内容，不重算同内容embedding；越过遗忘阈值按原生命周期写入并同步索引可见性 |
| CREATE_RELATION | 原RELATION_CONTENT（两端可用已解析local_ref）；belief、belief_reason | SAME_SUBJECT／PLAYS_ROLE／支持／反驳／派生／引用／上下文／相关分开。两端及world必须符合已有领域规则；关系不物理合并主体，也不转移现实经历 |
| CREATE_GOAL | content≤2048；subject_refs≤4；world_scope:授权ID；deadline:UTC微秒或null；reminder_lead_seconds:0..31536000或null；route_id:许可ID或null；basis_action_refs:0..2；basis_refs:0..2 | 至少一个合法已存在或本候选实际形成的记忆／关系依据，主体与来源受限；期限必须有明确依据。不能把普通聊天变成外部目标完成／放弃／改期命令 |

新增日常候选版本4，完整沿candidate_version=3的真实origin、上下文／最终Provider绑定，并扩展动作集合为上表；增加`reasoning_run_id`、`transcript_digest`、`action_mapping`。后者恰为0..8项`{model_ordinal:0..7,local_ref:0..7,object_id:ID,effect_ordinal:0..7|null,reused:boolean}`，同候选内ordinal／local_ref各唯一；复用主体对应reused=true、effect_ordinal=null，其余实际项对应false及唯一叶序。原版本1/2/3各保留，不用allow_goals与text_format的旧互斥开关拼出未声明组合。candidate manifest≤8192、每叶≤8192、全部≤73728；若复用主体导致某创建项无实际写，定稿映射与叶序保持确定、不得以占位写或空审计满足原写者集合。模型ordinal／local_ref和实际变更叶ordinal分别保存确定映射，复用项不挤掉其他项的稳定ID，也不把压缩后序号误当原模型引用。

模型无DELETE_OBJECT、遗忘／复活专用命令、目标完成／放弃／改期、persona写入、配置写入或日志读取权。上述明确未授动作即使领域层已有接口也必须拒绝。SET_SCORES的量表变化与生命周期联动按已有产品规则，不等于授予任意对象清除权。输出结构有效不保证事实正确；质量单独记录，不取消来源／权限校验。

### 3.3 受控主体与来源

用户已批准受控登记。平台人物的唯一键继续是平台＋外部身份；同名跨平台不合并。THING／FICTIONAL_CHARACTER／CONTEXT保留其类别与本轮目标来源，模型提出的同一联系只形成关系及相信程度。内部注册的主体记录须能追溯原candidate、batch、source、锚点及实际创建操作。

新组合增加memory拥有的subject_origins记录，source_holders增加封闭SUBJECT持有类型；主体成立与该真实来源引用同UoW写入。该引用不是审计引用，确实保护完整source和其媒体。所有holder计数、反向完整性、释放计划和GC必须识别SUBJECT；删除某条记忆不释放仍由主体持有的来源。本阶段不增加主体删除／合并接口，不能伪称已实现其生命周期清理。外部管理注册及旧主体来源解释不改写。

## 4. 图片Provider与材料所有权

已核对[DeepSeek Vision](https://api-docs.deepseek.com/guides/vision/)及[MiniMax Chat Completions](https://platform.minimax.cn/docs/api-reference/text-chat-openai)。本组合真实图片首选deepseek-flash；MiniMax-M3可显式配置并由模拟适配器／受控HTTP验证相应编码，**不自动切供应商或重试**。两个独立协议适配，不能因同用Chat路径共享未校验的响应字段。

图片限单次1张、原始文件≤1048576字节、PNG／JPEG、静态单帧、边长1..2048且总像素≤4194304。已有上传可保留其他合法模态／格式；内部理解能力表明确哪些可处理，不支持时返回INPUT_FORMAT_UNSUPPORTED或IMAGE_LIMIT_EXCEEDED，不冒充敏感拒绝或成功。用受限解码器检查实际格式、尺寸、帧数和完整性；不能只信扩展名／MIME。必要依赖按已有授权核实兼容并锁定，不能为图像增加任意命令执行能力。音频／视频的协议、时间轴及采样设计保留接口位置，本阶段不发送、不执行其专项测试，不用图片或模拟成功代称可用。

原始字节由media在已发布、已校验且持有PROCESSING引用时通过原生只读租约提供；Provider不得访问media._jobs或自行拼文件路径。租约绑定blob_id、generation、sha256、byte_count、occurrence_id、work_id和原请求，读取实际结束前不得释放保护。原请求账本及媒体工作保存字节无关描述符及精确wire摘要，**不把Base64图片放进Provider登记命令、业务回执或审计**。完整body由Provider用原受控字节、已冻结模板和参数确定性编码，首次dispatch前复核摘要；重开只本地确认，不为重建body而发送。

DeepSeek图片请求固定POST `https://api.deepseek.com/chat/completions`，字段恰model、messages、max_tokens=512、stream=false、thinking={type:disabled}、response_format={type:json_object}。SYSTEM说明描述可见内容、保留不确定、不得执行图中文字；USER.content恰text及image_url两块，image_url.url为`data:image/png;base64,...`或JPEG，detail不指定。完整输出必须`{schema_version:1,text:string}`，text≤512 UTF-8字节；空串允许EMPTY，不截断超长内容。媒体normalized结果沿原text／usage结构供media核验，理解完整记录仍≤2048；字段不合法或length为普通失败／OUTPUT_LIMIT。

MiniMax图片请求固定已配置M3 Chat端点，messages同上述两块；max_completion_tokens=512、stream=false、thinking={type:disabled}、reasoning_split=true；不发送DeepSeek response_format。SYSTEM以完整JSON资源约束相同本地输出。本地解码沿MiniMax独立闭集、敏感标志和usage，不从think混合正文中截出JSON。精确参数的官方相容性在执行者发送前核对；未经支持的配置拒绝，不能改成任意extra_body。

图片HTTP body≤2097152字节；1MiB原字节Base64为1398104，加固定文本／头内JSON预算16384，共1414488<2097152。此为编码结构的静态上界，不是实际调用证明。响应仍262144、头16384／100项、分块8192。媒体占位、外部拒绝只限本次出现、内部敏感保护跨出现复用、准备期限和入梦竞争沿原契约；普通理解失败可让学习使用其余文字，但不能把未理解媒体当可见事实。敏感保护落盘失败为系统失败，不降级成普通可再做工作。

## 5. 目标语义判断与合并

用户批准保守自动合并。外部目标仍先原子创建，然后去重；正文相同的原确定性合并先执行。日常组合扩展GoalRoot版本，增加可信entry_id以固定范围，旧GoalRoot及旧库不改；world_scope仍用原已登记世界ID，不能用标签替代身份。

语义候选仅限同主体集合、entry、world、OPEN状态、deadline、提前量、route完全一致且仍为canonical的目标。先用固定结构过滤，再按本地词法相关和created_at／goal_id稳定排序取≤8项；无词重叠时可在相同结构组补足最早项，候选截断明确记录，不能称全库穷尽。采用一次LLM比较同一目标与完整候选集合，**不为每对目标另调用embedding或LLM**；语义判断由goals结果owner核验，模型身份、原材料和revision全部绑定。

输出恰`{schema_version:1,decision:DISTINCT/UNSURE/MERGE,canonical_id:ID|null,reason:string≤512}`。MERGE的ID必须在原候选中且早于待合并目标；DISTINCT／UNSURE对应ID必须null。末端重新检查全部结构约束、两目标revision、来源容量≤8、别名容量≤64、去重任务代次及模式。发生竞争或冲突整体不合并，记NEEDS_SEMANTIC_REVIEW及DEDUP_CONFLICT，不自动重规划或重问模型；原目标继续存在。

合并只改变canonical／alias和去重状态，不改写保留目标的正文、状态、截止时间、提前量或提醒去向。来源去重后迁移，保留原目标ID与所有原输入、结果、解释；别名一跳，已有别名需要重挂或超限时不合并。本组合增加SEMANTIC_MERGED状态，与EXACT_MERGED、DISTINCT、NEEDS_SEMANTIC_REVIEW和FAILED分开；不得以旧EXACT_MERGED掩盖模型决策。

去重判断不得延长原提醒等待时限。确定性阶段先按原任务期限完成：没有精确合并且有语义候选时持久NEEDS_SEMANTIC_REVIEW，再创建独立semantic_decision；不能把旧5秒任务留在RUNNING等远程60秒。没有候选时零模型、零新判断记录，不为凑材料或审计造空对象。原5秒等待到期后提醒按原计划有限继续；若任一目标已有ATTEMPTING、ACKNOWLEDGED或UNKNOWN提醒，语义分支保守不合并，避免迁移时伪称未发送或重复提醒。无此风险时同UoW迁移有效来源、写别名／两根／任务／判断状态，并取消被合并目标未发送计划；不新增提醒请求。判断失败保持原目标；UNKNOWN还停止同账户新请求，已发生提醒事实不回写。

## 6. 持久记录、事务与恢复

### 6.1 新组合和封闭记录

静态装配标识`DAILY_COGNITION_V1`，仅空新库创建；老组合按原字节及原hash恢复，双向拒绝。runtime／cognition／memory／goals／self_model的新记录按下表，Provider保持六类根及已有有界材料叶，使用新静态声明版本5，按capability、task_role及billing_mode选完整封闭分支。GENERATION、MEDIA_UNDERSTANDING和EMBEDDING可以共存，仍分别归因；不因表合并而混用profile、账户、向量空间或结果owner。

通用BASE恰format_version=1、object_id、revision、database_id、instance_id、config_snapshot_id、created_at_us、updated_at_us。ID≤128字节；N为非负63位整数，时间／计数采用N，revision≥1；D为64字符小写十六进制SHA256；OP为原四字段命令身份。?为显式null而非省略。除表列字段及BASE外拒绝额外字段，引用的既有Schema按链接对应精确字段继承；原始正文叶不接受任意业务动作。

| 新记录及唯一owner | BASE之外的完整字段；完整编码硬限 | 生命周期／约束 |
| --- | --- | --- |
| runtime.learning_schedule | state=PAUSED/ENABLED；last_entry_id:ID?；last_kind=LEARNING/GOAL/EMBEDDING?；mode_epoch:N；last_operation:OP；2048 | 每实例一根；pause／resume真实CAS，空轮询不更新；重开发送开关始终关闭，持久ENABLED也不跳过显式resume |
| runtime.learning_triggers | entry_id、request_key:ID；reason=THRESHOLD/FOCUS/ACTIVE；target_through_seq:N；phase=QUEUED/CLAIMED/TERMINAL；batch_id:ID?；original_operation:OP；terminal_operation:OP?；2048 | 原键唯一；同入口覆盖可合并但保留原回执关联。单入口至多一个CLAIMED，终态短记录可在原回执可确认后分页移除 |
| cognition.reasoning_runs | batch_id、context_id、context_digest:D、authority_digest:D；phase=FROZEN/TURN_PREPARED/TURN_CONFIRMED/TOOLS_READY/WAITING_ADMISSION/REMOTE_UNKNOWN/CANDIDATE_STORED/TERMINAL；turn_count:0..3；tool_count:0..4；active_turn_id:ID?；candidate_id:ID?；deadline_at_us:N；terminal_operation:OP?；transcript_digest:D；8192 | batch唯一；与runtime原work一一对应，不能复制Provider尝试／费用账本 |
| cognition.reasoning_turns | run_id；ordinal:0..2；phase=PREPARED/ASSOCIATED/RESULT_STORED/RELEASED；material_id、material_digest:D、wire_digest:D；provider_operation_key；provider_request_id:ID?；handoff_id:ID?；result_kind=TOOL/FINAL/FAILED/SENSITIVE?；result_ref:ID?；result_digest:D?；previous_turn_digest:D?；original_operation:OP；8192 | run＋ordinal唯一；登记与结果原键，材料摘要固定后不可替换；完整输出用原context叶承载，root不重复塞全文 |
| cognition.reasoning_tools | run_id、turn_id；ordinal:0..3；name:§3工具枚举；arguments:该工具闭合记录；state=PREPARED/RESULT_STORED/FAILED/RELEASED；grant_digest:D；result_ref:ID?；result_digest:D?；started_at_us:N?；ended_at_us:N?；failure:原固定错误封套?；original_operation:OP；4096 | run＋ordinal唯一；结果放有界材料叶；失败code/reason使用相同类型封套，不能含未限远端异常文本 |
| memory.subject_origins | subject_id、candidate_id、batch_id、source_id；target_anchors:原ANCHOR[1..2]；created_operation:OP；4096 | 每次实际内部创建一行且唯一subject_id；与SUBJECT holder反向一致；不为复用主体另造创建来源 |
| goals.semantic_decisions | task_id、goal_id、goal_revision；state=PREPARED/ASSOCIATED/RESULT_STORED/APPLIED/UNRESOLVED；candidates:[{goal_id,revision,digest:D}][0..8]；material_id、material_digest:D；provider_operation_key；provider_request_id:ID?；handoff_id:ID?；decision:§5枚举?；canonical_id:ID?；reason≤512?；deadline_at_us；original_operation:OP；terminal_operation:OP?；8192 | 每task唯一；不改旧原始注入；结果owner=goals。完整冻结目标材料放配置绑定的cognition通用材料叶，通过只读租约借出；goals是此材料的业务持有者 |
| self_model.persona_imports | original_database_id、original_publication_id、original_candidate_id；original_candidate_revision；original_candidate_digest:D；original_text_digest:D；original_text_utf8_digest:D；review_evidence_digest:D；import_grant_id；self_subject_id；text≤1024；import_operation:OP；4096 | 本阶段专用受控导入，一实例仅一次；只允许§9指定正文／证据，正常首次生成路径仍在 |

原cognition上下文两表采用本组合独立版本：manifest最大16384、完整材料262144，叶完整≤8192／正文≤6144、最多48叶；context_kind=LEARNING/TOOL_RESULT/GOAL_DEDUP/PROVIDER_RESULT，owner_ref指实际run或goal decision；ordered_members≤4、初始related≤4，工具追加见§7。完整context身份、材料／叶摘要、顺序／总长、原操作及状态STORED/RELEASED均保持；删除叶只在所有实际消费者结束且业务终态确认后分页执行。已释放后保留原摘要和身份用于回执确认，不重构虚假的空成功结果。

Provider新声明：请求字段沿原请求根，format_version／fingerprint_version为5；execution_evidence含对应完整role profile／account、原材料描述符、输出格式和有界传输配置。attempt沿含terminal_error的现行完整字段，单ordinal=1；预算／reservation／费用项按真实计量分支沿用独立算法。新媒体与生成描述符`{v:1,material_owner,material_id,material_digest,wire_digest,byte_count,role,profile_id,config_snapshot_id}`，media另绑定原artifact／occurrence／work，不接受调用方任意路径。embedding payload／handoff叶、分片与账本关系沿原声明，仅在本组合版本下核验同一账户和用途；不能借版本5放宽向量、金额、首错／终错或UNKNOWN门控。

Provider提供公开的受控材料读取／完成端口；cognition／media／goals只向原请求签发租约，不能通过通用file路径或caller supplied function扩权。新组合验证所有同账户请求的实际在途、UNKNOWN和用量，不能因媒体账本或零held分支另开网络槽。生产全局统计、各角色usage和费用应能核对同一批账户事实，业务表只持引用，不另算第二份费用。

### 6.2 完整命令族与必要审计

保留新组合中适用的原领域命令及其审计，替换只限下列需要真实能力和新Schema的分支。采用精确按kind分支的输入／结果Schema，不以一个任意payload字典替代定义。输入含原操作键、可信绑定、expected revisions和固定业务载体；有大材料时每页至多4叶，单叶完整≤8192，不把全部材料装成一条超限命令。

| 新增／替换语义命令族 | 实际写owner及审计 | 原子结果与零写边界 |
| --- | --- | --- |
| initialize_daily_configuration | configuration，1份必要审计 | 完整130键／6域一次发布，仅此初始化享2MiB例外；不借配置写伪造业务初始化 |
| initialize_daily_schedule；resume_learning；pause_learning；enqueue_learning；claim_learning；complete_trigger；retire_trigger_page | runtime，各1份 | 只审计真实schedule／trigger变化；空页、空目标、已完成原键不占未来键、不造revision |
| stage_reasoning_run及stage_material_page／seal_material | cognition；仅建立对应runtime关联的分支再有runtime | 材料逐页持久但未seal不可使用；seal完整校验所有叶、原权限及run；不得提前创建正式source |
| prepare_reasoning_turn；associate_reasoning_request；store_reasoning_result；prepare_reasoning_tools；store_reasoning_tool_result | cognition，各1份 | 原请求与完整结果关联，在事务内通过Provider公开只读证据复核；Provider不实际写时不声明Provider审计slot |
| stage_daily_candidate | cognition＋runtime，各1份 | 最终完整候选与原work关联；内存证据不能先pop后提交；重复读原回执，不重做转换或生成 |
| finish_daily_empty／finish_daily_failure／finish_daily_sensitive | runtime＋cognition＋ingress＋buffers，必要时media | 三终态和引用释放沿原规则；零对象不写memory或object_history占位。敏感清理不能带正文审计 |
| plan_daily_candidate；apply_daily_candidate各固定owner分支 | 计划memory；应用runtime＋cognition＋memory＋ingress＋buffers，按实际效果加logging_service／media／goals | 创建、修订、评分、关系、主体／SUBJECT持有、目标、dirty、原子轮转；只有真实历史写才包含logging_service。含目标且无记忆对象的分支不得伪造memory创建；有真实来源／主体写可据实审计 |
| prepare_goal_semantic；associate_goal_semantic；store_goal_semantic_result；finish_goal_semantic／merge_goal_semantic | goals；创建／释放完整材料的分支另含cognition | goals自己的结果证据和目标／来源／alias／任务／提醒同事务；纯Provider确认不伪造第二账本写；UNSURE与冲突留原目标 |
| import_approved_persona | self_model＋memory（仅首次SELF／输入实际写），或self_model单owner分支 | §9原文导入及新库指针；SELF已合法存在时不重复写memory；审计标导入，不伪造新模型审核／请求 |
| retire_reasoning_material／retire_goal_material | cognition＋实际业务持有者（run属cognition，goal属goals） | 仅确认终态且对应实际消费者结束后分页释放；原回执／摘要保留 |
| 原Provider登记／结算／晚证据、媒体工作与三终态、信息发布、语义gap／ACK／两代发布 | 原实际owners及必要slot | 本组合完整闭集替换，原key／恢复关系不变；score-only与DELETE原路径不能制造embedding发送 |

每个结果绑定的targets为真实写入或进度记录，1..16；各必要slot只绑定本owner实际事实，包括ID、前后revision、effect counts及真实dirty序列。只读participant不产生审计，零写入走只读确认结果；不能为凑writer数量写占位。owner动态组合须在装配前生成有限静态分支，由完整类型／binding校验验证，不能运行时改声明。

**物理表数／命令数不是新的产品保证。** 上表8类新持久记录、已有格式的明确替换和命令分支是封闭业务集合；具体DDL、辅助索引、分支拆成的静态命令数量由执行者在这一集合内实现并用原始JSON清单准确交付，不能自行增加业务状态／权限或删除必要审计。这样避免再次以臆测“恰若干表”阻断本来必需的真实writer；完整集合必须可核对、容量实测，不能用此条省略清单或把未实现分支算完成。

### 6.3 恢复与终结

恢复顺序：存储完整性／原静态格式→完整配置→Provider原请求及账本→媒体与文件→来源／候选／subject holder→persona→runtime／推理／工具／目标任务→索引覆盖与当前查询。所有检查完成前不声称READY；部分只读能力是否可用按原能力声明，不将未知当空数据。

可信初始化中断须有完成路径：已经确认原CREATE_NEW意图、配置／资源／授权身份且尚未完成首次初始化时，可通过受控的原初始化续办能力，按原owner／原键确认已提交步骤，仅补齐明确尚未提交的初始化步骤，全部通过后进入READY，过程零模型发送。初始化进度必须有持久、可核验的有限记录，允许本组合为此补齐必要元数据及原生审计命令；不能仅凭缺行推断“从未初始化”。已完成初始化后缺失业务根、授权不匹配或数据损坏仍拒绝，不自动补造。NOT_READY是继续确认或故障停点，不能替代合法初始化中断的最终恢复能力；persona仍核验原批准导入证据，不重生成或覆盖已有发布。

每个turn原请求、每个工具步骤及最终candidate各有原键。已登记／已发送的生成或图片请求恢复只查原请求／原终态／原结果；UNKNOWN不自动重发。不曾登记的PREPARED工作在启动只停放，新的显式resume后才可持可信未发送证明发送该原工作，仍消费原用途槽、原材料及原绝对期限。不得把重开代次当模型重试预算。

Provider终态已知而本地候选未持久，可从原handoff确定性转换；已持久候选先确认原应用命令。确认未提交后，仅针对所有权变化在原授权内重建一次释放计划，不重新查询／生成候选；业务revision或权限冲突按普通候选失败终结，存储未确认／损坏保持系统故障，不当成普通学习失败轮转。完成原key的回执不能被后来错误覆盖。

本阶段新复杂动作都通过同一UoW实现正式对象、来源、历史、subject、目标和语义dirty联动；以创建项对local_ref排序，只对已冻结合法映射应用。任何叶、必要审计、写者、门控或提交失败整体回滚，不部分发布。语义索引远程计算在正式提交之后，正式提交不等模型；零记忆、纯主体、纯关系、纯分数操作不凭空花embedding预算。MEMORY正文改变产生真实dirty，删除／可见性变更沿本地清除／复用分支。

新memory装配须同时接受各自完整核验的DIRECT_LEARNING来源和原FIXED_REVIEWED来源，按来源判别选择既有封闭格式；后者仍由专用审核能力建立，前者必须经本轮真实候选，不能交换授权或伪造origin。旧semantic_format只允许固定来源、旧text_format只允许文本候选的限制仍只约束各旧组合；新组合不能复用这些互斥开关来隐式放宽所有旧库。

## 7. 完整配置与容量

本组合一个平台、6域，在现124键基础上新增6键，合计130。全部可调值经configuration注册，required=true、nullable=false、NoDefault、instance／no_override／INITIALIZE_ONLY，公开值由trusted_operator读写；真正秘密仅secret_ref。新增键精确如下，角色预算和参数不得成为无人读取的配置。

| 键／owner／consumer | 完整值字段（均必填） |
| --- | --- |
| runtime.learning_scheduler／runtime／runtime | v=1；enabled_on_create=false；entry_scan_page=64；tick_ms=1000；trigger_limit=128；active_learning=1；max_generations=3；max_tool_rounds=2；max_tools=4；work_timeout_ms=1200000；trigger_retention=128 |
| cognition.tool_policy／cognition／cognition,runtime | v=1；names=[search_memories,read_memories,read_subjects,list_goals]；tools_per_round=2；rows_per_tool=4；tool_result_max_bytes=8192；tool_timeout_ms=2000；related_initial_limit=4；related_total_limit=8；subject_roster_limit=16；input_utf8_max_bytes=262144；output_max_bytes=24576；max_output_tokens=4096 |
| media.image_understanding／media／media,provider,runtime | v=1；profile_id:ID；protocol=DEEPSEEK_IMAGE_JSON_V1或MINIMAX_IMAGE_JSON_V1；image_formats=[PNG,JPEG]；image_max_bytes=1048576；edge_max=2048；pixel_max=4194304；frames=1；images_per_request=1；wire_max_bytes=2097152；text_max_bytes=512；max_output_tokens=512；prompt_ref、prompt_digest:D、schema_ref、schema_digest:D；scope_kind=CONTENT |
| goals.semantic_deduplication／goals／goals,provider,runtime | v=1；enabled=true；profile_id:ID；candidate_limit=8；model_calls_per_task=1；result_max_bytes=2048；max_output_tokens=512；work_timeout_ms=60000；conflict_policy=KEEP_SEPARATE；scope_policy=EXACT_STRUCTURAL_SCOPE；prompt_ref、prompt_digest:D、schema_ref、schema_digest:D |
| runtime.daily_resources／runtime／runtime,provider,persistence | v=1；network_workers=1；network_queue=0；model_dispatch_gap_ms=30000；normal_operation_limit=20000；completion_operation_reserve=2000；free_reserve_bytes=2147483648；directory_stop_bytes=8589934592；database_stop_bytes=2147483648；wal_stop_bytes=1073741824；material_total_bytes=268435456；retired_run_limit=128 |
| runtime.timezone／configuration／runtime,cognition,goals,management | IANA时区名称≤128字节。可信初始化器读取实际进程环境时区并形成显式值，无法确定合法IANA时区则明确要求配置；不在纯解析器读环境，不默默猜Asia/Shanghai。已持久值重开保持，外部带offset的绝对时间不再套时区；Web首次确认／修改另阶段实现 |

本组合独立持久格式的第六个配置域固定命名为`daily_cognition`，承载继承的11项及新增6项，共17项；六域总计130项不变。2026-09-15监督在本阶段范围内批准该内部标识细化；它替换本组合先前沿用`text`域名的方案，不改旧文本／语义格式的域名、摘要或打开规则，也不授予旧库迁移。runtime.timezone同属完整候选注册表，由configuration作为owner。新定义schema_revision使用daily_cognition_v1。每键dependencies列出实际使用的上述键及继承键，不生成自循环；校验完整元数据、scope、unit／范围、owner、consumers及完整值。执行者交付完整130项非秘密候选和值来源，不能以“124项沿用”代替实际解析／持久恢复。

继承键的本组合修订：ingress.event_max_bytes=8192，H/T/R=1/2/1；media.processing_concurrency=1，原上传及读写限额维持有界；learning.material_max_bytes和learning.input_units_limit=262144（字节），learning.output_units_limit=4096（tokens），单次生成实际输出另受24576字节限制。provider.max_in_flight=1、provider.request_timeout_ms=60000；运行学习槽、媒体处理槽与实际网络槽分开计，等待不占不存在的网络worker。provider.result_max_bytes仍40960，完整生成／图片／向量结果都须在原生有界交接中重建；大于根的合法结果使用已持久叶，不塞单根。

仅本组合将旧`media.processing_concurrency + runtime.max_active_entries ≤ provider.max_in_flight`校验替换为共享实际网络仲裁：媒体准备与认知分别有逻辑槽，同一批次先结束媒体，再准入认知；只有发送时领取唯一网络槽，不能持有它等待另一个阶段。媒体文件worker仍须覆盖上传／读取／处理的真实占用。media.occurrence_total_timeout_ms=60000、media.processing_suspect_after_ms=120000、media.preparation_total_timeout_ms=900000；storage.operation_timeout_ms沿5000。4成员×2出现×(60000请求＋30000间隔)＋4×5000=740000≤900000；准备900000＋3轮×(60000＋30000)＋4工具×2000＋4×5000=1198000≤1200000。实际清理永久阻塞不保证在该时间完成；超时仅停止后续准入并保留真实占用。每个请求自身期限从获准准备时计，排队和间隔仍消耗业务总期；低优先级目标／embedding不得插队使学习用满预算后仍无服务。旧组合的原校验与期限保持。

媒体原文件字节限额、内联HTTP字节限额与供应商输入／context token上限分别校验；本组合不能再用旧`blob_max_bytes ≤ profile.max_input_units`将字节和token作比较。字节责任来自完整图像／材料及线协议，输入责任来自对应供应商已核实的计数规则和profile配置，未知依据不伪造token上界放行。纯金额预留所需的价表及计价上界与实际输入硬限分开，本次已批准包按[§10](#live-package)取消金额准入前置；usage沿原生Provider记录。

provider.accounts／profiles／role_profiles采用本组合完整闭合分支：角色LEARNING、MEDIA、GOAL_DEDUP、PERSONA、EMBEDDING_DOCUMENT、EMBEDDING_QUERY，分别唯一profile；PERSONA保留首次原生生成能力但本验证包不授槽。生成及图片／目标可共用同DeepSeek账户，embedding独立本地账户；所有账户实际总并发受1限制，远程UNKNOWN按账户阻断。金额责任在启用金额准入的分支继续约束发送；本次已批准包的仅计量分支及费用未知处理只见[§10](#live-package)。profile使用现有全部通用字段，增加material_role判别及对应资源引用；model／wire_protocol限定本文选择，dimensions／space仅embedding非null，media_tasks仅媒体DESCRIBE。max_attempts=1；总请求60000ms、attempt/read30000ms、connect10000ms，受业务剩余期限更严者约束。各配置声明不能把不同协议响应套进同一松散字段集。

provider.transport／provider.generation／cognition.text_context／cognition.text_output在本组合选择日常独立版本：role→固定协议与资源映射、§3输出、§6材料／候选格式及单轮输入上界；不使用旧tool_steps=0、CREATE_MEMORY-only、media=0或generation-disabled候选伪装新配置。self_model.initial_persona保持原生成／审核规则，另有§9窄导入入口。retrieval的1024维、阈值0.70／0.60、RRF、两代文件、缓存、version2响应不调优；qualification_profile新增DAILY_INTEGRATION，复用小档物理界及本文正常／收尾操作额度，旧SMALL_REAL_TRIAL／OFFLINE_CAPACITY严格保持。向量首次发送须同时满足通用账户／角色用途授权和原工作gate，不再强制新日常工作绑定旧12条fixed set。

容量硬界：普通完整命令1MiB、回执65536不变；只initialize_daily_configuration允许2MiB，其他命令不能借operation名绕过；完整配置体总524288，单域／单记录按完整实物分配。新静态装配声明、保存、读取、比较和恢复专用上限8MiB；旧静态格式继续原1／3／4MiB及原hash。禁止仅放宽编码器而不放宽相应读回校验，或只改校验不实际持久读写。

可实现构造边界：4事件＋最多8份完整理解为4×8192＋8×2048=49152；初始4当前对象≤16384；4工具结果≤32768；2份先前输出≤49152；persona2048、主体16×1024、SYSTEM及完整Schema合计≤40960、上下文身份／元信息预留16384，总和223232<262144。此预算中的各材料区包含其完整JSON编码，追加工具结果不能再重复追加全份对象；网络JSON转义及完整请求另受生成1MiB／图片2MiB限制。超过声明拒绝，不截TARGET、不删步骤后称完成。48×6144=294912足以承载262144完整材料；根／叶元信息单独受16384／8192限制。

以上是结构容量与限额，不声称已实际编码。执行者必须先在**实际完整新装配**验证全部DDL／命令分支、result binding、130键初始化、每类最大合法记录、一次满工具循环、释放计划、审计和原回执，随后继续实现整阶段；不得把“新增组件测试通过”当整阶段完成。允许在上述硬界内调整物理分片、SQL索引、声明复用和内部实现，不再为无产品影响的表数变化申请批准；若最大合法公开输入仍不可服务或必须扩大硬界，集中报告精确反例及最小修订。

## 8. 整阶段验收矩阵

默认仅单端Docker Linux arm64、必要定点＋受影响关联，最终锁定全量Pyright覆盖源码／测试／新文件；不机械全量unittest、双平台或旧12＋6包重跑。测试按行为命名，矩阵编号只留文档与临时JSON映射。所有正常／失败原始输出、实际命令、退出码、版本、实际资源性质及受测指纹交监督核对。Pylance未看则明确未验证。

| 场景组 | 必须证明的完整保障 |
| --- | --- |
| 配置／兼容 | 完整130键、各角色profile、8MiB静态与唯一2MiB初始化实物；旧组合hash及重开不变；混装拒绝，未完成启动不开放发送 |
| 正常文本 | 阈值、FOCUS、ACTIVE；两入口竞争／公平；空目标0调用；正确三段、完整原来源、一次终结、后来输入不丢 |
| 认知动作 | 六类动作、local_ref及复用主体、混合目标；公开链路验证实际对象／主体／关系／目标和来源，不仅调用转换函数 |
| 工具循环 | 0工具／多工具／第3次FINAL；步骤越界、非法工具、已存结果复用、半步崩溃和材料版本竞争；原期限不重置 |
| 图片 | PNG／JPEG真实临时文件、base64完整请求、超尺寸／帧／类型、外部完整结果优先、内部缓存跨出现、新出现普通失败语义；每种来源独立可辨 |
| 敏感与失败 | 模拟适配器及受控HTTP敏感元数据、图片局部拒绝／学习整批拒学区分；普通超时／格式错误不变敏感；拒绝保护和GC／入梦竞争 |
| 主体来源 | 不造SELF／平台ID、同名不合并、虚构不混现实；SUBJECT holder与source／media反向一致，最后memory删除不能释放主体仍持有的来源 |
| 写竞争与审计 | 修订、评分、关系两端、目标／source释放双向竞争；每个真实writer故障／必要审计缺失整体回滚；零写分支无占位审计；同key原回执 |
| 目标语义 | 同义保守合并、期限／route／entry／subject冲突保持分离；来源／别名容量及已有提醒竞争；5秒等待不被远程延长；恢复不重复判断／投递 |
| 统一语义联动 | 学习提交、MEMORY正文dirty、分数复用、删除本地清理、发布水位、version2信息／HTTP；工具只读不签票、不强化、不触发新冷调用 |
| 确认／所有权 | 登记前、登记确认不明、发送后、结果未持久、候选未应用、清理未完窗口；实际临时SQLite屏障、原子完成通知、close拒新保留合法收尾；无全局忙等 |
| 跨进程恢复 | 确认原进程退出后的新解释器，本地核对Provider／材料／候选／目标／SUBJECT／persona／索引；已登记工作0新增发送，未登记停放待显式resume |
| persona导入 | 精确批准正文及证据；错摘要／错候选／任意文本／已有发布／非专用grant拒绝；实际新发布审计和恢复，不增加模型attempt |
| 资源和观察 | 稳定限容、真实I/O结束才释放、页清理及原回执保留；各owner健康公开端口、权限分区、超限固定错误；不伪称生产／4096资格 |
| 真实小包 | 仅§10获准范围发送，逐请求原生账本、引用、材料及计数；全部结果包括失败／零输出保持，真实质量不阻塞但工程违规阻塞 |

<a id="persona-import"></a>

## 9. 本阶段指定persona导入例外（已批准）

仅本阶段合成验证的新空DailyCognitionHost实例允许；不是通用自由persona编辑，也不是旧库迁移。原对象为[用户已审核的Linux DeepSeek候选](/private/tmp/iris-deepseek-live-jve6u6qk/persona-review.md)，candidate_id=`persona-candidate:3591e66cfa92792b8b7afaf3b19e511e9ed580b9086bf4c744d2e24274aa7772`，原审核所对revision=1、candidate_digest=`2b2ae798d9a5e5f317136e7946eb9f6c28c19c61cee0857871dc6a28af4d796d`，原系统text_digest=`6d8693396cb608748694c8a3e824e1b0233c628beeb752e20c333f6eec36fdec`（SHA256作用于ensure_ascii=false的完整JSON字符串，含两端引号），裸UTF-8正文558字节SHA256=`620673861f22bdf522112beac6e1bc502378b290dffd771dbd031109802334b8`。此前确认问题把前者简称正文SHA256；这里澄清算法，批准的候选和正文未改变。原包中[原子发布结果](/private/tmp/iris-deepseek-live-jve6u6qk/continuation/linux-publish/result.json)与[发布／恢复核账](/private/tmp/iris-deepseek-live-jve6u6qk/continuation/linux-publication-reconciliation.json)须一并读取核验，不能把persona-review.md里的历史PENDING当批准依据或篡改该报告。

用户本轮明确批准该原文导入。可信装配给self_model签发仅针对原candidate／digest／新database／instance的专用grant；导入先校验完整来源证据、新实例尚无persona及合法唯一SELF，然后同UoW保存import、当前发布指针及必要审计。生成新的本地publication_id，保留original_database/publication/candidate及审核出处，projection明确publication_origin=IMPORTED_APPROVED，model_origin只描述原始生成来源，不冒充本库新生成。旧generation-based publication格式仍支持；新组合的当前投影独立版本允许这两种明确origin。

首次SELF的登记仍使用原可信初始化来源；导入不得伪造SELF正式记忆、梦境发布或外部经历。已有合法SELF时复用且不写占位；若其身份或原初始输入与指定导入实例冲突则拒绝，不通过改名解决。

正文逐字节不改，不为旧文本里的“尚未发布”措辞改写内容；当前批准／发布事实来自结构化元信息。原数据库只读，不复制旧Provider attempt到新库，不结清或删除旧UNKNOWN，不读取旧密钥。本入口不给普通宿主或模型，已有正式persona后不可覆盖；错误证据拒绝。新原生首次生成／用户审核路径保留，可在受控测试中验证，但此次真实包不新增persona请求。

<a id="live-package"></a>

## 10. 新受控真实调用包（已批准）

2026-09-16用户明确授权“同时批准真实llm请求，不要考虑成本”。本阶段以下完整受控包已获发送授权，取消此前推荐的30元金额上限和金额准入前置；不复用此前已耗尽18槽，不将本授权扩大为不限次数或其他用途。仅一个全新Docker Linux实例，包含本次导入persona、合成初始主体和固定场景；同一包使用精确冻结的代码／配置／资源／材料摘要。账户原生attempt、外层槽和各角色分别核账；秘密仍由本地受控resolver使用，不打印或入库。

已批准**最多32个attempt**：图片4、日常学习12（4批，每批最多3轮）、目标判断2、DOCUMENT embedding12、QUERY embedding2。全部无备用、无自动重试；未使用槽不移作其他用途。DeepSeek承担图片／学习／目标，最多18次；Coding embedding最多14次。本包的费用及实际扣额缺失不阻塞发送，也不要求为金额预算补取余额、价表或账单证明；已知usage及可核实金额仍保留原证据，缺失不伪造0费用，估算不冒充供应商账单。

**2026-09-16明确重跑授权：**用户要求“修复并重跑”，批准修复后一次新的完整32槽验证，分配及材料沿本节，使用新的包／run／数据库／实例身份。前包保留其已消耗3次及全部失败事实，剩余29槽停止使用；两轮累计最多35个attempt，不含更早阶段历史调用。新包记录前包及本次授权的关联，不修改旧grant／回执／费用／停止原因，也不将旧失败槽退还或伪装为原请求重试。新包没有额外备用，不能自行开启第三包；遇到下述停发条件仍保留原结果并停止新增发送。仅一个新实例获得本次重跑能力，其他实例及旧库不因该授权开放。允许修复协议必需措辞并重冻结运行资源；图像／事件／查询／目标场景、原审核标签及persona正文不改。

本次授权的工程落点为**新日常组合的`USAGE_ONLY_TRIAL`封闭分支**，同时覆盖生成、图片和embedding，不通过跳过整个Provider准入实现。该分支account及对应profile的billing_mode必须一致；account.price、cost_limit_atoms、quota显式null，其他账户身份／并发／窗口字段保持。生成／图片账户attempt上限合计18，embedding账户14，仍由可信包的逐用途原槽进一步收紧。PERSONA可保留同账户配置的兼容分支，但本包没有PERSONA槽，不能因配置可表示而发送。保留原TOKEN_METERED路径和所有旧文本／语义／模拟格式，不放宽旧生产或其他实例的金额政策。

仅计量分支保留原生成／图片usage数量字段、白名单及输入／缓存／输出不重计规则；原生账本按billing_mode判别核验请求、attempt、reservation、费用项及观察投影。未知价格、price_revision、金额和不存在价表的费用项价格字段采用显式null，不能把必填TOKEN_METERED字段填0或虚构价表来通过校验。金额reserved／held为0仅表示本分支不实行金额预留；已知小计按真实已知项求和，费用完整性及未知状态独立表达，不能据held=0推出费用完整。可信NOT_SENT证据沿原规则，已发送却没有金额证据时费用保持未知。格式／容量、结果绑定、同事务审计和恢复须覆盖此分支；没有本包原生授权能力时不能靠billing_mode字符串获得真实发送许可。

请求远程结果已知、原提交已确认且实际消费者清理结束时，**单独的费用未知允许下一个合法槽继续**；重开不把这种未知误判成需要阻断的远程UNKNOWN，也不自动恢复发送。远程结果未知、认证／身份／协议错误、提交未确认、资源不足或清理未完仍停止新增发送，保留原结果与合法本地收尾。业务输出不合法仅作为该批已知失败，不换材料或补发“修复”请求。实际供应商返回的额度不足属于服务不可用，不伪装成功；金额授权不能替代协议、输入硬限或身份核验。

每次实际提交且消费者清理结束后至少30秒才下一次，所有账户共用单实际worker；Provider原生attempt及外层已消费槽在登记前后分别核对。启动、初始化导入、缓存复用、原键及新进程恢复新增发送0。不为测试授权额外hello、账户、token计数或模型列表探针。已知模型输出不足／零输出留证，不为了产生记忆耗用别的槽。

### 10.1 已由监督者审核的合成材料规格

不使用用户私密资料、真人照片、外部网页或旧失败模型输出重写标签。以下规格是发送内容的边界；执行者机械构建版本化运行资源，发送前核对完整像素／文本与本规格及摘要，不自行润色、增加图像文字或替换场景。压缩格式字节摘要在生成后由工具冻结，不声称监督者亲自执行了编码器。

四图均为256×256、RGB白底PNG，无文字、元数据、透明度或动画；坐标原点左上，整数像素，未指定像素保持白。A：红矩形x=32..111、y=80..175；蓝圆满足(x−184)^2+(y−128)^2≤32^2。B：A的颜色交换，几何位置不变。C：绿色矩形x=32..95及160..223、y=32..95；黄色三角形顶点(128,128)、(64,224)、(192,224)，按闭边界像素中心包含。D：纯白。标准可见事实分别为“左红矩形右蓝圆”“左蓝矩形右红圆”“上方两个绿矩形、下方黄三角”“无有色图形”；不要求模型按固定句式输出。固定SYSTEM要求简短描述颜色、数量、位置并保持不确定，不把任何图形当真实经历。

一平台、两个独立入口，每入口5个按顺序到达的事件，H/T/R=1/2/1；先入前3事件运行一批，再入后2事件运行一批，合计4批。所有人和场景均为合成，实际时间由固定ISO-8601绝对时间转换，禁止以运行当天替换。每批对模型明确最多2个MEMORY创建／正文修订目标，其他动作仍共用总8项预算。

| 入口／事件 | 冻结正文与附件 |
| --- | --- |
| display-real／1 | 林青：“这是本次合成展台的展板甲。”附图A |
| display-real／2 | 林青：“这是展板乙。它与甲的颜色布局不同。”附图B |
| display-real／3 | 林青：“本次展示计划在2030-01-02T10:00:00+08:00开始；希望提前600秒提醒。” |
| display-real／4 | 周岚：“我称林青为阿青，这是这个入口的称呼；不要据此判断其他平台的阿青也是他。” |
| display-real／5 | 林青：“这里只是合成展示记录，没有其他行动结果。” |
| story-fiction／1 | 叙述者：“虚构的雾港故事里，角色青禾看见展板丙。”附图C |
| story-fiction／2 | 叙述者：“同一故事中的展板丁没有可见的有色图形。”附图D |
| story-fiction／3 | 叙述者：“青禾是虚构角色；不能把这些故事经历记到现实的林青身上。” |
| story-fiction／4 | 叙述者：“故事中的旧推测‘所有展板都有圆形’与丙、丁的观察不一致。” |
| story-fiction／5 | 叙述者：“本段仍只是雾港的虚构叙述。” |

平台身份由可信夹具固定，林青、周岚、叙述者分别有不同external_subject_id；story-fiction的CONTEXT由受控注册，青禾可由本轮候选登记；不把SELF改名为任何参与者。此前已批准的12条固定记忆可经原FIXED_REVIEWED端口取前4条在新实例建立，只用作查询工具的公开已有记忆，来源／world／标签保持、不是模型新产物。DOCUMENT12槽最多包含这4条和4批至多8个产生实际MEMORY语义内容的结果；未产生则对应槽不用，不能合成候选填满。

两个目标判断场景均由公开目标注入端口建立原目标后执行：同主体／入口／世界／时间／提前量／route下，“核对合成展板的颜色”与“检查合成展板颜色是否正确”为同意图候选；另一对截止时间不同，必须本地保持分离，不因有2个判断槽强行发送。第二个实际模型槽用于结构相同的“核对展板颜色”与“清点展板数量”不同意图。两对结构一致组的判断结果全部保留；不自动投递外部提醒，sink沿DISABLED。

QUERY恰“合成展板有哪些颜色和形状？”（REAL筛选）与“雾港故事里的展板有什么图形？”（原FICTIONAL context），先显式预热，再走公开version2查询／HTTP及工具只读；不追加查询向量。真实生成结果、领域拒绝、来源支持、世界隔离、合并决定和漏／多输出按原始项呈现；工程断言独立判断，不把受控模拟结果补到真实指标里。本阶段不设置新的相关性及文本质量门槛阻挡推进，也不把未达标记为通过。

### 10.2 停点

连续完成本阶段范围内修复、仅计量分支及必要定点／关联验证后，核对实际endpoint／模型／协议、输入硬限、账户／凭据引用及实例绑定，按本次授权激活实际冻结包，无需再次请求发送或金额批准。工程／配置变更后重新冻结实际身份并保留旧包证据，不篡改旧受测清单或挪用已用槽；原合成材料及persona批准不重审。原包内连续执行可执行槽、原键核账、关闭和新进程恢复；旧质量及本轮输出质量不足只如实记录，不阻塞后续工程。中途出现真实外部／契约阻塞只暂停相关部分，其他已授权工程继续。本阶段正常停在监督技术验收，未自行宣布用户最终验收，未暂存、提交、推送、部署或启动下一阶段。
