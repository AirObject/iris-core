# 真实模型驱动的文本学习闭环

**状态：用户已批准六组推荐、首次persona、最多三代请求、专注期初始化管理例外、独立配置边界及费用取整方案；授权文档定稿、整阶段实现、自查、测试与范围内修复。** 本文“推荐”表示已采纳方案；未采纳替代、供应商事实、实际费用与资格证据及范围外事项保持各自状态；用户已批准§8的本阶段质量门槛。实施、技术验收、提交授权及停止点见[CURRENT_TASK](../work/CURRENT_TASK.md)，已验收里程碑见[STATUS](../work/STATUS.md)。批准来源为用户核验的主草案SHA256 `83dffe3d015ca50e092a5396daf6ca31c08ae1369e283b4b16dc4f1e7ffe6a21`。

## 1. 目标、范围与集中批准决定

后续状态导航：用户已批准[学习质量暂缓](../work/DEFERRED_ISSUES.md#text-learning-quality)，后续验证遵循[统一检查口径](../CODING_STANDARDS.md#validation-environment)。本契约历史试验、原阈值及原始证据保持原解释；当前工作见[CURRENT_TASK](../work/CURRENT_TASK.md)。

形成可独立验收的链路：真实文本输入 → 按入口冻结完整材料 → 真实Provider生成 → 本地校验及持久候选 → 正式记忆／完整来源同事务终结 → 本地查询 → 跨进程恢复。这里“真实”指实际输入来源、供应商执行证据和对象所有者效果；不保证模型陈述为事实，不将格式合法当作学习质量。

[实施顺序§15.1](implementation-options.md#source-line-1202)是整体方向。本候选先落实其中真实生成与结构化文本学习；异步embedding、安全热发布仍另议。embedding、rerank、完整梦境／周期persona更新、语义目标agent、生产媒体理解、迁移、生产运维、Top-K观察字段均不纳入。保留既有只读查询、反馈、状态／目标、关闭和旧装配兼容，不借学习入口增加外发或审计读取能力。

| 集中决定（六组推荐已批准） | 推荐与影响 | 未采纳替代／仍缺条件 |
| --- | --- | --- |
| 供应商与协议 | 用户已选择OpenAI兼容Chat Completions；用户现选择MiniMax Token Plan、`MiniMax-M3`及`https://api.minimax.cn/v1/chat/completions`，替代此前方舟试验选择。非流式、严格结构化文本方案已批准；仅GENERATION | 协议选择不等于资源适用性确认；账号资格、实际后端身份、结构化能力及费用缺口见§3，不自行换端点／模型 |
| 适配器与重试 | Provider自有标准库HTTPS适配器、每逻辑请求最多1个attempt；无SDK、网关或业务重试 | SDK可减少协议维护，但须另固定版本并关闭重试；本推荐不新增SDK或tokenizer依赖 |
| 学习动作 | 普通学习首包仅CREATE_MEMORY，0–8项完整候选；主体由可信登记映射，不由模型创建 | 模型修改／删除、关系／目标建议及工具循环待另行批准；既有获准原生维护能力继续保留，不能将本子集称完整认知agent |
| 首份persona | 纳入有限的一次初始化整理（已知失败后显式最多2次重试见§5.1）：外部初始自我材料 → 真实Provider摘要 → 用户确认候选 → 原子首次发布；缺失时普通学习暂停 | §5新增产品决定已批准。仅要求已有真实persona可减少范围，但现有仓库没有其产生路径；空串、TestPersona或导入自由文本冒充生成发布均不可用 |
| 格式与装配 | 独立真实文本装配、新配置组合及版本化Provider／候选来源；旧组合原样可恢复，新旧组合双向拒绝 | 不迁移旧库；不改旧SIMULATED／SYNTHETIC记录标签，不把已有94命令／26新增表的实测当新装配实测 |
| 验证与费用 | §8有限离线协议、实库恢复、两平台受控真实调用和用户质量审核分别验收；真实调用包合计至多16个attempt；本次MiniMax试验按用户批准只记录用量，试验费用口径¥0（不构成供应商账单结论） | 请求上限与本版合成材料已批准；保留本地完整Schema校验、人工persona审核及结果标注；原生账本未知事实不得改成已结清，无密钥／无出站时只报告本地验证范围 |

既有产品规则只引用：[批次冻结与三终态](../product/batches-and-learning.md)、[来源与记忆](../product/provenance-and-memory.md)、[self与persona](../product/self-and-persona.md)、[内部上下文与成本](context-and-cost.md)。新增细化在本集中契约维护，所属模块只保留导航。

## 2. 实际端口核对及必要工程增量

实施基线为main／`8bc948c1822afb931625b77563df710fc9bbf20d`，已包含上一阶段本地信息提交。下表描述该基线的原有能力与本契约批准的增量；当前工作区实现、实际构造和验证证据统一见[CURRENT_TASK](../work/CURRENT_TASK.md)，不以此对照表代替实测。

| 实际位置／可复用端口 | 已有能力及限制 | 已批准增量与唯一owner |
| --- | --- | --- |
| [Provider资源](../../companion_memory/provider/resources.py)、[服务](../../companion_memory/provider/service.py) | ProviderResources.adapter及initialize精确检查SimulationAdapter；GateBinding.dispatch、WorkGrant与取消／实际完成所有权可复用 | Provider签发封闭的真实适配器资源、凭据能力和生命周期；不改成任意回调或只放宽isinstance |
| [Provider端口](../../companion_memory/provider/ports.py) | WorkPort.generate／lookup_request／verify_unsent；ResultOwnerPort.verify_terminal／recover_result；观察与结果权分离 | 新格式的结构化生成载体和Typed结果；同键确认保留原执行证据，恢复无需密钥且不可发送 |
| [规范化](../../companion_memory/provider/normalization.py)、[账本](../../companion_memory/provider/ledger.py)、[持久Schema](../../companion_memory/provider/stored_schema.py) | 模拟生成输入按UTF-8字节计units；SIMULATED来源、TEST金额、两项计价和无版本配置写入均有固定分支 | 新Chat协议／usage／token或订阅计量及真实持久配置证据；升级对应固定记录及查询投影，旧格式解释不变 |
| [配置Provider定义](../../companion_memory/configuration/provider_schema.py)、[信息组合持久化](../../companion_memory/configuration/information_persistence.py) | 原校验只支持SIMULATED／TEST；已有完整定义和值、snapshot／域revision及原键恢复 | configuration拥有新独立resolve／persist／load文本组合、真实profile／价格／secret引用校验；Provider只持执行事实，不创建配置真相 |
| [学习入口与资源](../../companion_memory/runtime/content_service.py)、[编排](../../companion_memory/runtime/content_learning.py) | ContentEntryPort.accept_event／run_learning；原关联／确认／stage／finalize、保存后只本地恢复；构造只接受Synthetic类 | runtime增加显式真实学习参与绑定、persona前置及原上下文保存；以公开Provider准入配额端口替代依赖私有字段的接法；所有模式／权限由可信gate签发 |
| [完整材料声明](../../companion_memory/configuration/content_material.py) | complete_source_base64可恢复来源，但SYSTEM固定为Verification records；不含persona／模型Schema | cognition的文本上下文构建器与独立材料版本；来源仍由memory持有，不将原Base64验证模板称为生产prompt |
| [候选](../../companion_memory/cognition/candidates.py)、[对象格式](../../companion_memory/memory/formats.py)、[变更](../../companion_memory/memory/changes.py) | CandidateBinding.stage／load／verify_stored、稳定身份、一次清单＋全部叶；origin仅SIMULATED／SYNTHETIC（对象另有NONE／OPERATOR） | 新candidate_version=3及object_version=2支持经证据核验的REMOTE_PROVIDER／MODEL_VALIDATED；语义变换与来源核验属于cognition／memory，不信任模型自报来源 |
| [候选应用](../../companion_memory/runtime/candidate_application.py)、[memory事务](../../companion_memory/memory/transactions.py) | 固定释放分支、同UoW真实对象／来源／引用／审计／终态与原回执 | 真实格式使用相同事务保证，扩展固定结果绑定；不能只改候选回调而沿用合成来源事实 |
| [InformationHost](../../companion_memory/runtime/information_host.py)、[模式发布](../../companion_memory/runtime/content_modes.py) | 完整启动／恢复／关闭；构造强制模拟；ContentPublication只规定bind／verify和静态声明，无persona生成 | 新TextLearningHost组合，self_model真实首次发布参与者及当前persona读取；启动最终截点与关闭仲裁、借入／自建资源失败原子性继续复用 |
| [查询服务](../../companion_memory/retrieval/query_service.py) | 本地词法、权威末端核验；只有明确TestPersona，real_persona固定false | 增加self_model只读当前发布投影，末端核验发布revision；查询不生成persona、不调用模型。缺失依旧明确UNAVAILABLE |

模块所有权：[provider](../modules/provider.md)、[cognition](../modules/cognition.md)、[memory](../modules/memory.md)、[runtime](../modules/runtime.md)。复用[正式记忆闭环](formal-memory-source-media.md#sources-candidates)、[本地信息发布／恢复](local-information-feedback.md#capacity)及[Provider原发送与恢复](provider.md#provider-foundation-transactions)。新签名及实现要求如下；是否已可调用以执行验证为准。

## 3. 官方协议事实与适配边界

### 3.1 本地准备与官方依据的边界

2026-09-13用户改选**MiniMax Token Plan / MiniMax-M3**，本地LLM非秘密白名单为`GENERATION / minimax_llm / https://api.minimax.cn/v1 / /chat/completions / MiniMax-M3`。用户声明已取得本用途的平台许可、速率限制1 rps，并确认平台没有明确承诺strict JSON。凭据内容不进入文档、日志或指纹；embedding材料不读取。本次执行串行，前次实际收尾后至少间隔30秒，不自动重试。

| 核对项 | 当前依据及界限（2026-09-13访问） |
| --- | --- |
| 端点、模型与用量 | [MiniMax官方Chat Completions](https://platform.minimax.cn/docs/api-reference/text-chat-openai)确认精确端点、MiniMax-M3、非流式、模型／响应身份及token usage；1M上下文不能独立证明完整协议输入责任上界。未调用模型或账号诊断API |
| strict输出 | 同页及官方公开OpenAPI没有`response_format/json_schema/strict`声明，用户许可也未明确支持。用户现已批准MiniMax独立JSON prompt协议（§3.3.1），不要求服务端strict证明，也不发送这些字段；本地完整校验继续保留 |
| 思考及协议差异 | [官方SDK兼容说明](https://platform.minimax.cn/docs/api-reference/text-openai-api)说明M3默认thinking、可输出think标签，max_tokens为旧字段。不能沿用方舟受控夹具证明M3正文可直接按现行JSON Schema解码；尚未定稿的适配不增加任意extra_body |
| 本次计量口径 | 用户明确Token Plan，只记录usage，费用可按¥0计。该数是**用户指定的试验统计口径**，不是reported_cost、供应商零价或每attempt扣额结清证据；不再要求用户另设金额／套餐额度实值。14标准／至多16共享attempt仍有效，额外2次仍另批。本次例外仅属试验授权，不改既有通用TOKEN_METERED／SUBSCRIPTION账本事实或清除held |
| 已有适配的边界 | 已验收的方舟固定配置与传输保留用于既有确定性回归；方舟资格、Auto和价格资料不再作为本次MiniMax发送前置。用户已批准M3独立协议、usage-only原生装配与直接首次persona验证；旧待审包保留，新冻结执行包及实际结果见CURRENT_TASK |

本版合成初始自我、监管目标、全部13条H/T/R材料及10个标准命题已由用户冻结确认；原包与审核证据见[CURRENT_TASK](../work/CURRENT_TASK.md)。真实persona发布和最终命题标注仍由用户另行审核，不由执行者代签。

### 3.2 Chat请求、传输及凭据（已批准）

既有已实现方舟适配将base_url与endpoint_path组合为`https://ark.cn-beijing.volces.com/api/coding/v3/chat/completions`；本次MiniMax独立分支见§3.3.1，以下方舟wire形状不适用于MiniMax，不因用户换key而复用方舟能力证明。对任一已核验绑定，只允许配置绑定的精确HTTPS origin和路径，无任意宿主URL、重定向、代理改写或路径猜测。POST UTF-8 JSON，ensure_ascii=false、C0按JSON转义。请求字段集合恰为`model,messages,max_tokens,n,stream,response_format`：model为当前绑定别名，messages恰SYSTEM／USER两条（线上role为小写system/user，content为文本）；max_tokens=2048、n=1、stream=false；response_format恰`{type:json_schema,json_schema:{name,schema,strict:true}}`。schema为版本化封闭资源，name恰text_learning或initial_persona，按工作角色固定；不是宿主自由字段。该形状以**实际路由支持核实**为发送前置，不能因为本地可编码而发送探测。

不发送`text.format/max_output_tokens/background/store/truncation/previous_response_id/conversation`等Responses方案参数；不发送tools、functions、图片、文件、reasoning控制或未明确支持的自由参数。工具权限固定空，收到工具请求明确失败。temperature等未选参数保持协议默认并记录未指定，不宣称模型重跑确定性。若需要禁思考／其他模型参数，先补确切官方依据和固定Schema，不增加任意extra_body。服务端上下文拒绝后不裁掉冻结目标或重发。

标准库HTTPS worker属于Provider，TLS校验主机与证书；DNS至本地结算共用绝对期限。响应体262144、头总16384／100项、块8192字节；累计读取限额不依赖Content-Length可信，只接受identity编码，不自动解压。外层超时不能证明底层socket／DNS或存储I/O结束，尚未结束继续占槽，不能创建替代worker。

`RealGenerationResources`恰绑定原生gate、固定适配器、credential_resolver、时钟、操作完成通知。配置仅持`secret_ref,secret_revision,account_ref`三个安全ID≤128字节；可信外层将引用绑定到受保护资源，凭据不是配置值或模型材料。resolver返回Available(opaque lease≤4096字节)/Unavailable/Revoked/Failed；仅Provider取得lease，撤销先于发送则不发。旧结果本地恢复不解析秘密；不把密钥或密钥摘要用于日志、指纹或审计。借入resolver不得误关，自有lease按实际完成通知释放。

受控Linux启动器可先在原生凭据模块中打开受保护文件描述符，再永久降为固定非root UID；打开时只读元信息，秘密字节仍仅由Provider调用resolver后读取。持有者核对原device／inode、单链接、私有权限及大小；Docker Desktop同一挂载inode的所有者投影只允许原UID或预先固定的接收UID，不接受任意owner变化。启动器只在全部Provider消费者实际结束后关闭自有描述符；恢复入口不建立该绑定。

### 3.3 规范化与真实计量（已批准）

Chat成功要求恰一个choice、index=0、message.role=assistant、完整非空content、finish_reason=stop、无tool_calls/function_call/非空refusal；未知finish_reason／内容类型失败。顶层只接受id/object/created/model/choices/usage/system_fingerprint/service_tier，后两项只保存有界安全观察，不主动发送tier；多余字段按协议不支持处理。message只接受role/content/refusal/reasoning_content/tool_calls/function_call，choice只接受index/message/finish_reason/logprobs/moderation_hit_type；logprobs必须null，reasoning_content可null或有界文本但不作为学习正文、工具或新增依据，受网络总界且不持久为记忆。禁止从混合响应截出一个成功片段。

`StructuredGenerationResult`恰含`format_version=2,output,stop_reason,provider_response_ref,requested_model_id,reported_model_id,resolved_model_id,output_schema_ref,raw_output_digest`；ID≤128（resolved可null），digest64hex，stop_reason固定STOP。原输出文本及规范化output各≤6144字节；output为深不可变嵌套JSON对象，禁止重复键、非法Unicode、非有限数。其余元信息＋外层≤2048、完整交接≤8192；不得把JSON再次作为字符串后仍套6144＋2048公式。本地Schema不合法可有已知Provider完成事实，cognition按完整结果生成失败候选；超交接上限为已知INVALID_RESPONSE，保留合法usage，不截断假成功。

| 观察 | 固定归一化／后续行为 |
| --- | --- |
| stop＋完整合法Chat载体 | SUCCEEDED交接，本地学习继续独立校验 |
| content_filter或明确非空refusal | OTHER_REFUSAL，保留普通失败历史；不推断SENSITIVE_REFUSAL |
| length | 仅新文本格式ADAPTER_FAILED/adapter/OUTPUT_LIMIT；完整响应形成request TERMINAL/FAILED及已发送attempt COMPLETED/FAILED，无成功handoff、不使用半份JSON、不改为REMOTE_RESULT_UNKNOWN；usage／费用完整性、提交确认与清理独立，保留未决责任和首个真实错误；新文本attempt.terminal_error独立记录最终OUTPUT_LIMIT，迟到完整length也不覆盖原超时first_error。persona为KNOWN_FAILED/OUTPUT_LIMIT，普通学习按已知失败终结，不自动重试 |
| tool_calls/function_call、非文本、多choice、结构错配 | INVALID_RESPONSE，零工具执行 |
| 完整认证／权限／参数拒绝 | 已知失败分类，费用缺失仍非零；不得换endpoint／model重试 |
| 429／5xx、断链、超时、发送后取消 | 不自动重试；只能证明HTTP状态时保留该事实，无法证明远程终态则REMOTE_RESULT_UNKNOWN |
| 本地存储／审计未确认 | 原完成键本地确认；LOCAL_COMMIT_UNCONFIRMED／FAULTED，不消费为普通学习失败 |

usage精确映射prompt_tokens→input_tokens、completion_tokens→output_tokens、total_tokens→total_tokens、cached_tokens→cache_read_tokens、reasoning_tokens→reasoning_tokens。全部精确非负63位整数或null，bool拒绝；total已知须等于input＋output，cache_read≤input、reasoning≤output。缺字段为null，不用字节units补token。细分的provisioned/audio等如存在非零而本包无计价覆盖，保留费用不完整并停止后续发送。白名单raw_usage≤2048，完整UsageV2≤4096；计量独立于正文成败，错误正文不进安全错误。

既有原生费用方案由§7闭合配置区分TOKEN_METERED与SUBSCRIPTION（本次MiniMax的已批准试验统计例外见§3.1及§8，以下不重新要求用户审批金额／套餐额度）：前者保存精确币种／atom尺度、非缓存输入／缓存输入／输出三个互斥单价及责任上界；后者独立保存套餐窗口、供应商请求折算上界和已知额度证据，不能把套餐每请求假换token价格，也不能将订阅额度充足推成金额0。这两类原生方案要求实际值完整，null不会作为零价通过；本次usage-only试验的原生装配未完成前，不伪填这些字段。使用整数微币单位（10^6 atoms/币种单位，CNY或USD显式），三项有理单价以atoms/百万token表达，逐项向上取整作为估算责任，实际账单金额仍null直到有独立证据。

无tokenizer时input_token_estimate=null，request_bytes单列；输入责任上界必须有确切模型／允许路由集合的官方上下文依据，不能沿用上版1,047,576。SUBSCRIPTION预留为明确的per_attempt_quota_bound，并同时持有有证据的金额上界（不能仅靠订阅价格推每次费用）。未知远程／计费责任不释放；16次共享预算不随重启或persona重试重置。没有充分上界时PAUSED_BUDGET/UNBOUNDED_COST，仍可完成离线及本地恢复验证。SDK和网关重试语义未知不能当作max_attempts=1的远端恰好一次证明。

**TOKEN_METERED的分项取整及预留（已批准）**：令`M=1000000`，`C(x)=ceil(x/M)`，输入责任上界I、输出责任上界O，非缓存／缓存／输出单价分别u、c、o（均atoms/百万token）。真实usage中的总输入P、缓存K、输出Q必须满足`0≤K≤P≤I`、`0≤Q≤O`；非缓存量为`P−K`。分项估算结算为`S=C((P−K)×u)+C(K×c)+C(Q×o)`，保留三个独立cost_items，不能先合并分子取整再声称与分项一致。未有供应商独立金额证据时S仍为LOCALLY_ESTIMATED，不是PROVIDER_REPORTED账单。

指定反例仅为**算术示例，非方舟实际费率**：I=P=128000，K=1，u=240000、c=120000，输出项暂取0。原输入预留`C(128000×240000)=30720`；分项为`C(127999×240000)=30720`及`C(1×120000)=1`，合计30721 atoms，原公式少预留1。不修改usage或合并cost_items掩盖该差额。

修正后的保守责任上界为`R=C(I×max(u,c))+δ+C(O×o)`，其中仅当`I≥2且u>0且c>0`时δ=1，否则δ=0。依据：两个正费用输入分项满足`ceil(a)+ceil(b)≤ceil(a+b)+1`，其合并分子不超过`I×max(u,c)`；无法同时出现两个正分项时无额外取整。输出独立取整；因此全部合法usage均有S≤R，无需推定缓存比例，也不要求证明R为最紧上界。上述反例的输入预留修正为30721。每次prepare前在同一预算事务验证`known_subtotal+held+reserved+R≤cost_limit_atoms`（三类现有责任互斥，本次R尚未计入），才能持有R；结算按原状态机原子转移／保留责任，未完整确认不能释放差额。若供应商尚有未覆盖计费项、Auto路由或重试倍率，则R不是充分责任上界，拒绝新发送，不能用δ替代这些未知费用。

63位规则：数量、费率、每个乘积、C的结果、δ加法、三项和、单请求R、窗口累计金额／quota均须在`0..2^63−1`，bool／负数／float不接受；运算前用除法检查乘积是否越界，用`a≤MAX−b`检查非负加法。C用`q=x div M, r=x mod M, C=q+[r>0]`，不以可能溢出的`x+M−1`取整；即使数学除后可放入63位，超限乘积也拒绝，不依赖大整数或饱和截断使其通过。配置解析及发送预留先验此规则；返回usage超过绑定I/O、费用覆盖失效或结算运算溢出时，保留有界原usage及异常证据，按费用未完整覆盖停止新请求，原预留不释放，超出责任不得伪写成已知完整S。UNKNOWN和原键确认沿原持久状态机，不重算旧价格或重复扣费。

### 3.3.1 MiniMax版本化JSON prompt与usage-only试验（用户已批准）

用户明确批准“MiniMax独立协议调整，key有效，直接发起验证”。固定组合为MiniMax-M3／MINIMAX_CHAT_JSON_V1／JSON_PROMPT_V1／USAGE_ONLY_TRIAL，仅在新配置中同时成立；不能把它拼到方舟或模拟profile。旧TOKEN_METERED、SUBSCRIPTION及服务端strict解释保持。端点固定https://api.minimax.cn/v1/chat/completions，标准库Provider持有实际发送权，不接受任意URL、重试或SDK旁路。

请求恰model、messages、max_completion_tokens=2048、stream=false、thinking={type:disabled}。M3的disabled取值与新输出限额字段有官方依据；不发送n、max_tokens、response_format、json_schema、strict或tools。原SYSTEM指令及USER材料保持，Provider在SYSTEM末尾加入MINIMAX_JSON_PROMPT_V1固定指令和完整业务Schema；新增协议指令与Schema同时纳入prompt版本／摘要及完整wire摘要，重开从原材料和资源复建逐字节相同。逻辑SYSTEM仍≤4096；线上追加的固定ASCII指令及Schema≤12544，线上SYSTEM≤16640；完整请求保守118784≤131072，原context、原输出及静态界不扩。完整编码实物仍须核验，标准答案不加入请求。

MiniMax响应独立验证官方封套及示例字段：base_resp、敏感标记／类型、message.name／audio_content，以及usage.total_characters。只接受已限定类型和界，音频必须空；base_resp非零按实际错误分类，1004鉴权失败、1001/1002/1013保持远程未决，其他明确拒绝为已知失败。敏感标记为真沿OTHER_REFUSAL，不推断敏感来源。正文必须是完整JSON对象；无截取、去围栏、猜测、修复或额外请求。thinking已显式关闭，仍不把任何reasoning字段或think混合正文变成记忆；本地完整业务Schema／来源／主体／世界／权限／容量及人工审核继续生效。

USAGE_ONLY_TRIAL是此受控试验的计量政策，不宣称供应商结清。新UsageV3及其计量项与旧UsageV2分开验证：真实prompt/completion/total必须齐全且一致、精确非负整数；缓存／reasoning等未返回保持null。有效总输入／输出不能超绑定上界，未知扩展或矛盾停止后续请求。真实金额、估算金额和套餐扣额均null；cost_complete=false、金额known小计0且不预留金额／套餐quota，0只表示此政策无货币预留，不表示账单零。三项token观察和reported未知金额项持久入原表，原预算attempt_count仍真实递增，必要审计保留cost_complete=false。已知未发送可本地恢复确认；实际已发送时只有该独立policy的完整有效usage允许后续准入，不能据此释放旧账户held或放行旧计费模式。外层¥0注明OPERATOR_CONVENTION_NOT_SUPPLIER_BILL。

显式冻结授权启动器连接共享原14＋2包和原生Provider：每平台首次persona只占标准persona槽；外层持久reserve先于native登记，每次实际attempt仍记入原生独立账本。新进程或换库不能重置外层槽位；UNKNOWN、身份错配、缺usage／审计／提交或实际清理未结束均阻止下一请求；已知普通失败保留原样、不自动重放。原始HTTP事实只进入受控脱敏证据，不进普通运行日志；恢复路径不解析凭据、不发送。首次macOS原请求收尾明确后等待≥30秒再Linux；用户审核前不得发布或继续学习。

### 3.3.2 DeepSeek独立适配与真实试验（技术增量与新增调用已批准）

DeepSeek是独立账户／实例／目录／配置的新组合，不迁移MiniMax业务状态或改写其UNKNOWN。用户已批准协议适配、提示词补齐和期限修正，并于2026-09-14明确批准**14个新attempt、两平台共享30元及旧UNKNOWN保持时独立开展新试验**，不重复询问。原未激活候选包保持；另建激活记录绑定其摘要、实际受测代码与该用户决定，批准者仅记录用户及原消息引用，不伪造姓名或签名。旧10个attempt、旧停止证据及剩余6槽保持，旧槽不转用。新包为每平台persona一次及六次学习、无备用、无自动重试，历史最多24次；外层共享授权必须先于原生attempt登记，两套账本均保留。原§3.1、§3.3.1的14＋2与¥0仅属于旧MiniMax包。

外层证据采集错误与原生UNKNOWN必须分开：若原停止条目已绑定的原始／恢复产物证明仅为提交后本地查询RESOURCE_BUSY、原生request与attempt从未UNKNOWN、usage与费用完整、同一终结／对象／来源／发布身份保持、公开查询已恢复且实际清理结束，可在原哈希链追加受限的观测更正。原停止条目、所有attempt、原保守金额责任和总包上限保持，不清零、不重建或重放。实际UNKNOWN、费用缺失、身份错配、提交未确认、资源监控失联或清理未完不适用此更正；这落实原生事实与外层观察分离，不扩大业务恢复权限。

固定新绑定：provider_name=deepseek_llm（外部非秘密选择名）；transport.origin=https://api.deepseek.com、base_path空、endpoint_path=/chat/completions；Generation/Profile的model_id=deepseek-flash，protocol/wire_protocol=DEEPSEEK_CHAT_JSON_V1，response_mode=JSON_OBJECT_V1；expected_reported_models只有deepseek-flash、resolved_model_id=null。Account/Profile为TOKEN_METERED、CNY、atom_scale=1000000。没有新增配置键、表或命令；其他模型的原组合、解析和恢复规则保持。

线上请求恰model、两条SYSTEM/USER消息、max_tokens=2048、stream=false、thinking={type:disabled}、response_format={type:json_object}，不用MiniMax的max_completion_tokens、n、strict Schema、tools、Responses或extra_body。Provider自己编码HTTP，不经过SDK；首次persona同时验证真实协议，不额外探测。依据：[Chat Completion](https://api-docs.deepseek.com/zh-cn/api/create-chat-completion/)、[JSON Output](https://api-docs.deepseek.com/zh-cn/guides/json_mode/)、[思考模式](https://api-docs.deepseek.com/zh-cn/guides/thinking_mode/)，核对日期2026-09-13，思考模式2026-09-14再次核对。

JSON Object不提供业务Schema保证。封套只接受官方闭合字段id/object/created/model/choices/system_fingerprint及usage；choice恰index/message/finish_reason/logprobs（index=0、logprobs=null）；assistant message只有content及可选reasoning_content/tool_calls，本组合后两者只能为null或空reasoning字符串，不接受工具列表。model须精确匹配；stop再验证整个content为JSON对象，空白、重复键、围栏、混合正文或截断一律失败，不提取或修补。length保留OUTPUT_LIMIT，content_filter为OTHER_REFUSAL；tool_calls、insufficient_system_resource、aborted为已知不支持的INVALID_RESPONSE，未知结束值拒绝。结果身份、语法和业务校验与usage覆盖分别表达；原first_error与terminal_error不同职责保留。

新UsageV4保留prompt_tokens/completion_tokens/total_tokens、prompt_cache_hit_tokens/prompt_cache_miss_tokens及可选prompt_tokens_details.cached_tokens原值。P=hit+miss、T=P+C，所有数量为精确非负整数；可选alias若出现须等于hit，缺失保留null；completion_tokens_details只接受reasoning_tokens且本非思考组合只能缺失/null/0。未知计费扩展、缺必需数量、矛盾或超I/O责任界，不能完整结算，不把未知补零。估算分项复用§3.3的互斥缓存计量及保守取整；usage版本4不等于计量表版本4，cost_items仍用原v2，request/attempt/预算/预留/handoff仍原版本2。旧UsageV2/V3与新协议逐层隔离验证。

费率冻结取[官方价格](https://api-docs.deepseek.com/zh-cn/quick_start/pricing/)2026-09-13值，2026-09-14复核一致：每百万tokens缓存命中0.04元、未命中2元、输出8元。统一采用高峰费率作为预算上界，不以无法预知的请求时段套较低价；supplier bill始终另列。官方上下文1M，输入责任保守取1048576、输出2048，不减去输出凑预算。单次R=ceil(1048576×2000000/1000000)+1+ceil(2048×8000000/1000000)=2113537 atoms；14次29589518 atoms（29.589518元），比30元少410482 atoms。实际估算依真实usage和冻结费率，不宣称逐笔供应商扣款；无usage时保留全额R。

仅新DeepSeek组合的固定向量将runtime.operation_timeout_ms由5000改60000，Provider总期60000、attempt与读取上限30000、连接上限10000保持；其他组合仍5000。入口在准入时生成一次绝对期限，准备、关联、Provider及本地終结均消费剩余时间。连接与TLS握手共用该连接阶段的剩余限额，不重新开始10秒。调用者期限耗尽而本地提交被拒绝时保留未确认及原资源所有权，不能清除DeadlineScope或换worker伪成功；合法原键恢复独立确认原PREPARED/UNKNOWN，不新增发送。跨过5秒、实际30秒网络超时、调用者耗尽及迟到原worker终态的断言见[DeepSeek期限测试](../../tests/provider_trials/test_deepseek_lifecycle.py)。

新学习资源TEXT_LEARNING_JSON_V2及persona示例INITIAL_PERSONA_JSON_V2只用于DeepSeek，旧prompt字节不变。本地Schema不改语义；模型可见规则明确BODY/EVENT.item_index=null、QUOTATION实际零基索引、UTF-8字节半开范围与双null整段引用、TARGET专属锚点、HISTORY/RECENT专属辅助引用、授权主体/世界/对象revision及否定/不确定/转述/虚构边界。中性格式示例不是标准答案，13条材料、六批窗口、10个标准命题和质量门槛不变。完整Schema连同DEEPSEEK_JSON_OBJECT_V1指令追加于SYSTEM并纳入prompt摘要；逻辑SYSTEM≤4096、追加≤12544、线上SYSTEM≤16640、完整HTTP≤131072，其他容量沿§7.2。完整实际学习请求依新人工批准persona及原生ID冻结，发送前逐字节取摘要；合成资格请求不得冒充未来真实请求。

准备工具只输出可复核的完整配置、资源和未激活模板，见[DeepSeek准备](../../tests/provider_trials/deepseek_prepare.py)；两平台回环资格见[合成链路](../../tests/provider_trials/deepseek_qualification.py)，授权控制见[独立授权](../../tests/provider_trials/deepseek_authorization.py)。普通测试不加载真实key。实际发送前仍须核实两平台非root、持久路径、当前TLS/CA、受控HTTPS、原生凭据解析及当前资源监控；旧TLS证明不能替代。新候选必须分别人工审核，旧macOS的APPROVE不迁移；发布后才允许该平台六批学习。每次实际清理结束后至少30秒，新增UNKNOWN、费用覆盖缺失、身份错配、提交不明或清理未结束立即停止后续发送。

### 3.4 embedding准备缺口（不纳入本阶段）

embedding.json是`EMBEDDING / volc_embedding / https://ark.cn-beijing.volces.com / /api/coding/v3 / doubao-embedding-vision`。现endpoint_path只到base前缀，缺操作路径。火山方舟官方团队文章给出的Coding专属base为`https://ark.cn-beijing.volces.com/api/coding/v3`、模型名doubao-embedding-vision，并分别展示OpenAI风格工具与多模态工具集成；它不证明任意两种payload可互换。[官方团队说明](https://developer.volcengine.com/articles/7628812787703087110)

普通向量应独立核对`/embeddings`的texts/input、批量顺序、data数组；多模态独立核对`/embeddings/multimodal`的typed input、混合模态、data结构、维度与空间。官方普通／多模态[API入口](https://www.volcengine.com/docs/82379/1521766)、[多模态入口](https://www.volcengine.com/docs/82379/1523520)上一轮无法读取；[官方SDK普通向量资源](https://github.com/volcengine/volcengine-python-sdk/blob/master/volcenginesdkarkruntime/resources/embeddings.py)只能支持其客户端形状核对，不替代Coding路由能力／订阅资格。不得擅自填写最终路径或替换普通/api/v3端点，也不修改本地四文件。embedding的账号、实际版本、向量维度／稀疏支持、计量和费用均留待独立准备与批准；本学习包不绑定embedding profile、不调用embed。

## 4. 冻结材料、输出Schema与候选（已批准）

### 4.1 输入与有界上下文

真实文本输入仍走注册入口的`accept_event`，原消息／主体／平台／世界／时间由可信接入事实提供，完整事件上限2048字节；本新学习能力只接受无媒体事件，媒体能力明确不支持。复用H=1、T=2、R=1的完整窗口上限4，实际目标可提前不足但非空；同一入口最多一个冻结工作。正文不是系统指令，引用或文本内的角色标记不改变结构化身份。输入接收成功、冻结成功、学习成功分别返回原有持久确认语义。

普通学习前必须有已核验当前persona；在冻结前完成材料字节／能力预检。目标冻结后不能为适配预算裁目标或扩大来源；资源不足保持未发送／受保护状态，暂停原因明确。辅助缺项、首次H空均按真实情况记录。

`TextContextManifest`即§6.1的cognition_learning_contexts完整记录；该处唯一维护清单、成员、相关对象、叶、版本与恢复字段。

推荐先用受控本地词法查询选至多2份ACTIVE相关记忆，不增加embedding或生成查询。cognition通过memory受限当前读取端口获取快照，记忆每份完整≤4096；选中不存在／变更／权限不符则重新本地预检或暂停，不能偷偷改已冻结context。context保留这次确实发送的快照，属于原工作暂存证据，不对agent开放历史检索；以后恢复用它重建原请求，不读当前值替换。persona是解释指导而非独立支持来源，候选的支持依据只能来自已授权真实目标或显式引用的当前对象及其来源根。

USER材料为明确分区的UTF-8 JSON，包含完整冻结成员、persona发布摘要及相关对象；不使用旧验证prompt或要求模型从Base64自行猜字段。SYSTEM模板与输出Schema作为独立版本化运行资源，提供任务、分类、量表、范围、注入隔离及输出规则，不读取设计文档作为prompt。SYSTEM的授权数据段由可信owner读取的主体ID／revision／kind和明确WORLD集合规范编码，随完整渲染文本冻结；它不授予注册主体或扩大读取的权限，恢复不重新取当前名单。模板资源摘要与完整渲染上下文摘要分别保留，整个SYSTEM仍≤4096字节。模型工具集合为空、工具步数0、生成调用数1；任何输出工具要求都拒绝，不能执行SQL、代码、URL或读开发者审计。

### 4.2 模型输出的精确子集

逻辑输出根恰`{schema_version:1, memories:[...]}`；0–8项。每项恰：

| 字段 | 类型与本地约束 |
| --- | --- |
| action | 固定CREATE_MEMORY；不得携带target_id或数据库写命令 |
| category／stance | 沿[MEMORY.content](formal-memory-source-media.md#objects)的四分类／四立场 |
| body | 非空UTF-8正文≤1024字节（本模型输出子集；正式对象原2048上限不变） |
| subject_ids／speaker_subject_id | 0–4个已在材料中授权的主体ID／可null且来自该集合；不因同名建立新主体 |
| world_scope | 原WORLD结构；仅可选材料中明确授权的世界，不能把角色经历归入REAL |
| occurred_range／applicable_range | 原时间结构或null；不从接收时间推造发生时间 |
| belief／belief_reason | 0–100精确整数／非空文本≤256字节；说话发生与命题成立按不同对象表达 |
| target_anchors | 1–2项，恰message_id、part=BODY/QUOTATION/EVENT、item_index、start_utf8、end_utf8；后3项按原锚点可null条件，范围≤2048且UTF-8边界正确；只来自本次真实T |
| auxiliary_refs | 0–2项，恰message_id、purpose=CONTEXT/CITES，只来自本次H／R；不算独立目标 |
| basis_refs | 0–2项，恰object_id、expected_revision、kind=SUPPORTS/REFUTES/CITES/CONTEXT；仅限本次提供且有权读的对象；相关不等于支持 |

JSON Schema各object统一`additionalProperties=false`，所有列required，允许null用显式联合类型；数组和字符串范围同时由本地封闭验证器限制。若供应商支持子集不能表达UTF-8字节或跨字段关系，这些约束保留本地检查，不能声称strict代替它们。原JSON文本及规范化output分别≤6144，因此8项每项字段极大值不能同时达到；超限整组失败，不丢末尾条目或自动拆成多次收费请求。0项合法成功须有完成响应和有效根结构；空HTTP正文不是零记忆成功。

转换器从原冻结context、VerifiedTerminal和已确认handoff确定性构造完整候选：稳定ID沿[候选身份](formal-memory-source-media.md#sources-candidates)，transform_revision进入身份域；retention及其理由从已冻结配置物化，creation time取原工作可信时间，不在重开时取当前时钟。source_id来自原冻结材料，score_basis／evidence_roots通过owner对锚点和依据的验证生成，不能采用模型自行声称的根。每条直接结果至少一真实T锚点，不用“第一个目标”替模型缺失锚点兜底。

### 4.3 校验、来源与兼容

本地检查顺序：原身份与权限 → 完整载体／数量／字节 → 类别与分数 → 目标及辅助范围／片段 → 人物／世界／时间 → 依据存在性与revision → 完整候选编码／容量。失败从已保存完整交接确定性得出FAILED_DROPPED候选，保留安全原因，不再请求修JSON。引用在候选之后发生冲突则按现行SYSTEM_BLOCKED／REVISION_CONFLICT处理，不把新对象内容套到旧revision；存储错误与模型无效输出分开。

真实候选／对象的精确版本及origin差量统一见§6.2。来源性质由可信owner从原请求、handoff、context和candidate共同核验，宿主和模型不能自报切换。输入另保留ACTUAL_INPUT／SYNTHETIC_FIXTURE事实；真实供应商处理测试语料仍是测试语料，MODEL_VALIDATED仅指完成本地校验，不表示命题真实或人工认可。

旧candidate v1/v2、object v1、Provider格式1及原指纹逐字保留，不能靠新枚举重新解释旧记录。新装配仅CREATE_NEW，OPEN_EXISTING须同新格式和原配置；老装配读老库继续原键确认，新旧组合拒绝。模型不可用时明确MODEL_UNAVAILABLE；拒绝、缺persona、未发送预算暂停、远程UNKNOWN、候选已存和正式已提交分别可观察。

候选stage与正式finalize仍两次独立命令；stage同UoW保存完整清单、所有叶和保护引用，返回只确认候选。finalize按[既有同事务协议](formal-memory-source-media.md#sources-candidates)提交正式对象／完整source／引用转移／索引dirty／批次三终态、全部必要审计及原回执，任一失败全无部分成功。零结果不制造正式对象、永久source或只读owner写审计。查询只在commit后通过当前owner权威核验交付；后续索引发布继续memory／retrieval两owner原子事实与双代次恢复。

## 5. 首次persona依赖及有限初始化路径（新增产品决定已批准）

[self-and-persona §12.2–12.4](../product/self-and-persona.md#section-12)要求唯一内部角色、真实外部设定来源及首份可用摘要；常规persona更新只在梦境。当前ContentPublication不是生成器，TestPersona不是该初始化的替代。这是尚缺依赖，不是两个现行正文冲突；本节推荐已单独列入用户批准，按此有限路径实现。

推荐首次启用由可信管理能力完成下列一次流程，不自动在initialize、query或普通run_learning内调用模型：

1. `register_initial_self(original_key, initial_input)`登记用户给出的初始自我材料及唯一SELF。可无预设姓名／背景，仍保存“外部选择无预设”的实际管理输入及actor，不能伪造经历；材料非凭据，完整记录≤8192、正文≤2048字节。memory拥有原始管理来源和SELF身份；memory必要审计、结果事实／回执同事务，原键确认不重复创建。外部详细设定在本有限包超限时明确拒绝，不声称已支持无限初始材料。
2. `prepare_initial_persona(original_key, expected_self_revision)`由self_model登记首次整理run、原输入／配置／生成目标／监管prompt、原Provider操作键；runtime原有ENTER→DREAM_PREPARING→DREAM_FOCUSED截点关闭普通准入并确认无在途阻塞。没有先发布persona才允许此run的循环依赖：只对真实首次初始化run签发`internal_dream=true`的PERSONA grant，唯一实例、同一首次run内每代最多一次；它不能授给普通LEARNING角色或通过HTTP自填角色取得。
3. 显式`generate_initial_persona(run_id, original_key)`通过同一真实Provider协议及§6状态机，输出独立固定Schema`{schema_version:1, text:<非空≤1024字节>, initial_input_ids:<1项真实输入ID>}`。无预设身份也须只据“无预设”事实说明自我与限制，不补造姓名／经历。持久候选与原交接绑定后进入WAITING_REVIEW；不在初始化恢复中自动调用。
4. 用户审核候选原文及依据，`publish_initial_persona(original_key, candidate_id, digest, expected_revision)`只允许确认该候选，不接受改写正文。self_model核验原交接与来源／监督配置、首次未发布条件；runtime核验同一个专注run。当前指针、不可变发布记录、run终态、runtime FINISH至DRAINING、两owner必要审计和原回执在同UoW提交。memory在此只被核验，不伪造writer审计；不在发布时改长期自我对象。source／persona引用由各owner固定参与，不清理仍受引用的初始化材料。
5. 按原FIFO完成回流，再进入NORMAL并开放普通学习。发布先提交、交付后关闭的合法结果保留；关闭先发生或恢复尚未完成时不能重新READY。首次生成失败／拒绝／远程未知／用户未确认时不以虚构发布退出专注；维持原材料及模式，不解除FAULTED。已知终态后的用户显式有界重试统一见§5.1；放弃整个run、手工改摘要及未知结案仍不纳入。

六表中首次persona所需四表及其唯一约束、字节上限、代次记录和恢复规则统一见§6.1；发布当前指针由唯一publication表达，不另建可随意覆写的历史库。

`CurrentPersonaPort.read_current`／`verify_current`为self_model公开只读投影及短事务核验端口；cognition保存原版本，retrieval在现有persona分区预算内投影文本／生成时间／revision／审阅状态／REMOTE_PROVIDER来源。完整分区受原2048字节限额，故文本子限1024；若长元信息导致完整分区不合格，候选发布前拒绝而非发布后截断。来源变更后的滞后按原产品规则可见，不在普通查询同步重生成。

替代方案A：仅消费另一个已验收self_model提供的真实首份发布，减少本包实现，但当前无该资源，整条新库闭环不能验收。替代方案B：用户直接输入摘要作为persona，需要改变“首次形成摘要”的产品流程和来源性质，不应在技术装配中偷偷替代。推荐上面的有限首次整理，保留完整梦境、周期更新、复杂自我推理及编辑器引导界面为范围外；首次管理能力可由受控本机向导宿主调用，不把测试身份签发当生产鉴权。

<a id="initial-persona-retry"></a>

### 5.1 失败后显式有界重试（集中产品决定，已批准）

推荐同一首次run最多3代（首代＋至多2次用户重试），每代仅一个Provider逻辑请求且一个attempt；另受整个验证包16次及原账户金额／额度约束。旧请求、结果候选（含失败）、用户决定及费用保留；不是SDK重试、原键重发或新run重置。替代为维持一次性停放：更少状态与写命令，但已知可恢复的首次失败仍阻止新库学习。若选替代，generation固定1且retry命令不装配，须另重核对应声明；本文容量和计数统一采用推荐的3代方案。

只允许前代为KNOWN_FAILED或USER_REJECTED、对应Provider结果KNOWN_TERMINAL（或可信NOT_SENT）、本地终态及所有必要回执COMMITTED、当前操作实际清理ENDED、Provider与存储无FAULTED、当前持久模式为同run的DREAM_FOCUSED且无publication。任一REMOTE_RESULT_UNKNOWN、提交UNCONFIRMED、费用责任不完整、在途执行／清理或DREAM_PREPARING均拒绝新代。用户未审核的WAITING_REVIEW不能视为失败；已批准候选也不能换键要求再生成。用户拒绝需先持久review(REJECT)。已完成结果如格式失败可记KNOWN_FAILED；敏感拒学不会被自动重试，该供应商当前不宣称支持该分类。

`retry_initial_persona(key, run_id, expected_revision, expected_generation, prior_resolution_id)`由显式用户管理意图驱动，原子CAS run增加generation并记录新provider_operation_key，runtime同事务更新该梦境的执行代次记录；旧mode state不变、epoch按真实更新递增，所有旧发送许可失效。新请求键由`database_id/instance_id/run_id/generation/GENERATION`确定性派生，与旧请求必不同；不同操作键并发重试只能一条CAS成功。同键确认不增代；失败回滚不占代，COMMIT未知只查原回执。随后仍需显式generate，不自动出站。配置、初始材料、监督资源、模型绑定及累计预算不变；要更换其中任何项不属于该重试。

普通业务在专注期仍按[dream §16.3](../product/dream.md#source-line-780)拒绝。**新增产品例外已批准**：允许受控本机初始化管理能力读取本run待审候选、记录APPROVE/REJECT、请求上述有界重试并授权首次发布；它只驱动已经登记的首次内部整理，不给普通HTTP、学习端口或模型签发内部权限。不将其伪装成现有普通写入豁免；管理动作仅向该受控原生能力开放。该例外与现行“外部业务拒绝”的边界已由用户集中裁定，不能由实现继续扩展。

未来直接验收：已知失败／用户拒绝后分别重试，3代封顶和跨平台总预算不重置；双重试CAS、原键重入、旧代迟到结果、新旧请求关联；UNKNOWN／未决回执／清理未完／费用未决／FAULTED／PREPARING全部拒绝；重开仅恢复原记录，零自动发送；发布与重试／关闭两种截点顺序；全部旧回执／必要审计／实际占用不变。

## 6. 登记、发送、交接、候选与终结恢复（已批准细化）

继续使用[持久确认](persistence-and-transactions.md#persistence-foundation-idempotency)及[Provider本地恢复](provider.md#provider-foundation-transactions)。三条事实轴分开：本地提交COMMITTED／NOT_COMMITTED／UNCONFIRMED；远端NOT_SENT／KNOWN_TERMINAL／REMOTE_RESULT_UNKNOWN；实际清理ENDED／PENDING。已知业务结果可以与费用部分未知并存；UNKNOWN费用不是仍有活worker的证明，worker结束也不是费用已结清的证明。

| 持久截点 | 同事务／所有者事实 | 重新进入或新进程的唯一允许动作 |
| --- | --- | --- |
| 已接收、尚未冻结 | ingress原回执与队列所有权；persona／预算前置不满足显示暂停 | 恢复原输入和入口顺序；initialize不触发模型，需新的显式获准调度事件才开始未发送工作 |
| FROZEN＋CONTEXT_STORED | 原source、不可变context清单与叶、模板／Schema／persona／配置、稳定原请求键及digest；runtime工作关联与cognition持有同UoW | 核验每叶、版本及所有权；无原context不得改读最新值；只本地恢复／停放 |
| REQUEST_ASSOCIATED | runtime先保存完整可重建请求的关联，再允许Provider副作用 | WorkPort.lookup_request按原材料确认；NotFound不是“没发送”，需要verify_unsent和旧执行者可靠隔离证据 |
| Provider登记request／attempt／预留已提交，尚未发送 | Provider登记必要审计和回执；只有本次活跃执行者持有一次发送权 | 活跃者在deadline及gate截点通过才发；崩溃遗留PREPARED保守转UNKNOWN，恢复不发送 |
| 请求实际开始、远端结果未获知 | 一attempt所有权、预算责任及发送观察 | 超时／取消只结束等待；保留原任务／socket／结果缓冲，UNKNOWN不自动换键、重发或调用远端retrieve |
| 已收完整响应、本地结算未确认 | 原owner内存持有唯一规范化结果及已知usage；完成键／内容固定 | 仅确认原本地完成事务，可靠未提交可有限同键重试；进程丢失且无handoff为UNKNOWN，不能再调用模型复原 |
| HANDOFF_COMMITTED | Provider终态、usage／费用／责任、完整handoff与必要审计同事务 | ResultOwnerPort按精确授权request取回；未保存候选时用原context及原transform确定性重建，无网络 |
| CANDIDATE_STORED | cognition全部叶、清单、真实来源及保护已获原stage回执；runtime记录原关联 | 直接读取同候选，终结前owner重验；丢失stage返回用原键确认，不再变换出新身份或请求模型 |
| 正式终结提交前／中未知 | 原candidate、source、固定释放计划、当前revision核验及全owner事实 | 只查原finalize回执；NOT_COMMITTED按原候选本地重试，UNCONFIRMED保留保护；损坏／审计缺失拒绝READY |
| TERMINAL_COMMITTED | 正式对象／来源／索引dirty／引用轮转／三终态／全部必要审计及原回执 | 返回原对象ID／revision／终态，零重复收费、零重复候选、零重复轮转；索引worker在正常启动后处理dirty |
| 首次persona候选待审／发布已提交 | self_model原候选与确认意图；发布两owner原事实 | 只恢复候选待审或原发布回执；不能自动代用户批准、临时NORMAL或再生成 |

原请求登记后到结果返回的任何中断都不能由“未看到响应”推导NOT_SENT。一次明确HTTP失败且费用未知可终结业务失败，但预算继续持有未决责任；远端结果本身未知则批次继续REMOTE_UNKNOWN／SYSTEM_BLOCKED，不按普通失败消费。Provider的Completed只证明其本地终态，正式学习成功仍以finalize回执为准。

上下文的两表、完整叶界、终结释放与命令writer／必要slot统一见§6.1–6.3。普通agent不因恢复交接存在而取得历史审计读取权；source仍由原数据owner管理。

启动顺序为存储全校验 → 配置持久恢复 → 绑定所有owner／关闭发送gate → Provider本地账本恢复 → memory／cognition／self_model本地核验 → 原运行工作本地恢复及跨owner交叉核验 → 当前模式和健康核验 → 最终一次性发布可用能力。缺persona可开放已允许的接收／诊断及明确降级查询，但`learning_ready=false`；不以总体READY冒充完整学习就绪。持久专注模式恢复仅用受限本地恢复授权，不给普通调用者发送或读取缓存的豁免。

关闭立即关闭新的准入／发送；对已启动工作沿各操作自己的完成通知保留占用，按宿主→业务调度／运行→Provider／参与owner→存储→日志的实际依赖顺序释放。锁内只仲裁状态／许可，不等数据库或网络。最后空扫描后和最终READY发布前都复核生命周期／恢复阶段／健康；不能在CLOSING启动服务或调度器。重复close保留首次报告，迟到完成只反映到当前health，不能改旧回执或冒称已清理。

新增错误及原封套兼容统一见§6.3；新端口使用明确类型和原生签发能力，禁止Any／裸异常兜底。

### 6.1 封闭记录与索引声明（已批准）

以下为穷尽字段集合，复用记为“旧Schema＋精确差量”，不是任意扩展字典。全部字段required，`?`仅表示值可null；未知／缺失字段拒绝。ID沿原Identifier且UTF-8≤128，D为64小写hex，U为0..2^63−1精确int，R为1..2^63−1，T为U微秒UTC；布尔不得充整数。E为所列枚举。Text(n)同时限制原UTF-8及**完整记录**确定性编码界（任一不满足即拒绝）；每字段上限不承诺同时极值可装入。所有身份、时间和revision由可信绑定／原语义命令给出，宿主只能给用户材料和原操作键；所有写CAS，恢复不补写修复。

六表每行物理列恰`scope_id TEXT NOT NULL, object_id TEXT NOT NULL, revision INTEGER NOT NULL, body TEXT NOT NULL`，PK(scope_id,object_id)，revision≥1；body为下面对应完整记录，object_id/revision必须与列一致。scope_id=真实instance_id；database_id来自持久存储绑定。new object_id由带类型域的database_id/instance_id及原语义身份SHA256派生；input/run/publication按实例唯一，candidate按run＋generation，context按batch，leaf按context＋ordinal，均不从宿主自由ID采用。索引下述JSON字段按固定json_extract表达式建，不接宿主SQL；表内所有revision初值1。表名按owner前缀固定，不改已交付26张表。

共同字段B恰`format_version=1, object_id:ID, revision:R, database_id:ID, instance_id:ID, config_snapshot_id:ID, created_at_us:T`，各表在B之外只有下表字段。NOT_SENT且Provider未登记时，candidate.terminal_receipt指本次record_initial_persona_resolution自身真实本地操作引用；该分支另须原生verify_unsent证据且可靠隔离旧执行者，不伪造Provider终态回执。其他分支terminal_receipt指Provider原完成操作。K为原持久操作引用`{owner_namespace:ID,operation_kind:ID,scope_id:ID,operation_key:ID}`；不嵌套旧回执或数据库路径。

record_initial_persona_resolution的evidence_revision独立使用U（精确整数0..2^63−1），其他记录revision、expected_revision及epoch继续R。已登记分支provider_reference是实际request_id，evidence_revision严格等于原请求真实根revision，含合法0；仍核验完整原请求／run／generation／配置及原生终态完成证明，terminal_receipt使用实际Provider完成K，零attempt不能替代该证明或改变真实终态原因。

未登记NOT_SENT分支provider_reference固定本代原provider_operation_key，evidence_revision固定0；仅Provider原生verify_unsent的REGISTRATION_ABSENT加完整原请求／本代身份绑定及可靠旧执行者隔离可登记。NotFound、超时、没有响应或调用方0不构成证据。candidate的provider_request_id／handoff_id／text／text_digest为空，resolution=NOT_SENT，review=NOT_APPLICABLE，failure_reason=NONE仅指没有已知失败原因，其他已知失败保持真实原因。candidate.terminal_receipt取本次self_model结果登记真实K，candidate、run及必要审计同事务。显式重试与OPEN_EXISTING核验该原self_model回执的输入指纹、结果根及必要审计，并重新取得原生隔离证明，不要求不存在的Provider K、不补造请求／attempt／handoff或审计。证明随实际操作保留至事务和底层清理结束；公开超时、回滚或提交未确认不提前释放占用，原键确认仍可取原回执。重试另在同事务核对当前预算，并沿§5.1最多三代及同run专注模式／未发布保障；恢复不发送。

| 表／owner／完整上限 | B之外字段（完整） | 唯一约束／辅助索引 |
| --- | --- | --- |
| memory_initial_self_inputs／memory／8192 | self_subject_id:ID, self_revision:R, input_kind:E(PRESET,NO_PRESET), body:Text(2048), input_digest:D, actor_ref:ID, operation:K, input_origin:E(ACTUAL_INPUT,SYNTHETIC_FIXTURE) | UNIQUE(scope_id,self_subject_id)，UNIQUE(scope_id,json operation.operation_key)；仅此2辅助索引 |
| self_model_initial_persona_runs／self_model／4096 | input_id:ID, input_digest:D, self_subject_id:ID, self_revision:R, generation:1..3, state:E(PREPARED,REQUEST_ASSOCIATED,WAITING_REVIEW,APPROVED,KNOWN_FAILED,USER_REJECTED,REMOTE_UNKNOWN,PUBLISHED), provider_operation_key:ID, provider_request_id:ID?, resolution_id:ID?, publication_id:ID?, mode_epoch:R, prompt_ref:ID, schema_ref:ID, transform_ref:ID, account_id:ID, window_id:ID, binding_digest:D, original_operation:K, last_operation:K, updated_at_us:T | UNIQUE(scope_id)确保一个首次run；UNIQUE(scope_id,provider_operation_key)；仅此2项 |
| self_model_initial_persona_candidates／self_model／4096 | run_id:ID, generation:1..3, provider_operation_key:ID, provider_request_id:ID?, handoff_id:ID?, terminal_receipt:K, resolution:E(SUCCEEDED,KNOWN_FAILED,NOT_SENT), failure_reason:E(NONE,OTHER_REFUSAL,OUTPUT_LIMIT,INVALID_RESPONSE,AUTHENTICATION_FAILED,CONFIGURATION_REJECTED,CANCELLED,TIMED_OUT,PAUSED_BUDGET,MODE_BLOCKED), text:Text(1024)?, text_digest:D?, input_id:ID, input_digest:D, binding_digest:D, review:E(NOT_APPLICABLE,PENDING,APPROVED,REJECTED), reviewed_by:ID?, reviewed_at_us:T?, review_operation:K? | UNIQUE(scope_id,run_id,generation)，UNIQUE(scope_id,provider_operation_key)，UNIQUE(scope_id,provider_request_id) WHERE request_id非null；3项 |
| self_model_persona_publications／self_model／4096 | run_id:ID, generation:1..3, candidate_id:ID, candidate_revision:R, candidate_digest:D, input_id:ID, input_digest:D, self_subject_id:ID, self_revision:R, provider_request_id:ID, handoff_id:ID, requested_model_id:ID, reported_model_id:ID, resolved_model_id:ID?, prompt_ref:ID, schema_ref:ID, transform_ref:ID, text:Text(1024), generated_at_us:T, reviewed_by:ID, review_operation:K, publication_operation:K | UNIQUE(scope_id)即唯一当前首份发布；UNIQUE(scope_id,candidate_id)，UNIQUE(scope_id,run_id)；3项 |
| cognition_learning_contexts／cognition／8192 | context_version=1, batch_id:ID, run_id:ID, source_id:ID, state:E(STORED,RELEASED), persona_publication_id:ID, persona_revision:R, prompt_ref:ID, schema_ref:ID, transform_ref:ID, model_binding_digest:D, ordered_members:Member[1..4], related_objects:Basis[0..2], leaf_refs:LeafRef[1..8], payload_digest:D, wire_digest:D, byte_count:0..65536, input_token_estimate:U?, reservation_input_bound:U, original_operation:K, terminal_operation:K? | UNIQUE(scope_id,batch_id)，UNIQUE(scope_id,run_id)；2项 |
| cognition_learning_context_leaves／cognition／8192 | context_id:ID, ordinal:0..7, text:Text(7168), text_bytes:0..7168, digest:D | UNIQUE(scope_id,context_id,ordinal)，INDEX(scope_id,context_id)；2项 |

Member恰`{role:E(HISTORY,TARGET,RECENT),message_id:ID,payload_digest:D}`；顺序及成员身份与原冻结source逐项一致、TARGET1..2且总≤4。Basis恰`{object_id:ID,revision:R,grant_ref:ID,snapshot_digest:D}`，grant_ref仅标识当时有效读取授权，恢复本身不获得该授权。LeafRef恰`{object_id:ID,ordinal:0..7,digest:D,byte_count:0..7168}`；序号0起连续、ID唯一，body保存重建请求所需原文本片段，拼接后解出固定RequestContext（下一段）。每叶完整8192而非正文8192；text累计≤57344，清单＋全部完整叶≤73728，预算不能把叶头挤掉。RELEASED保留leaf_refs／digest／byte_count用于原证据确认而实际叶必须0；STORED必须逐一齐全，不允许部分删。初始输入不可变，NO_PRESET也有真实用户意思的非空材料；不把空表初始化做成外部设定。

RequestContext恰`{context_version:1,system_text:Text(4096),user:{members:[{member:Member,event:<原完整无媒体事件>}],persona:<CurrentPersonaProjection>,related:[<原完整MEMORY对象>],identity:{database_id,instance_id,batch_id,run_id,source_id,config_snapshot_id}},resources:{prompt_ref:ID,prompt_digest:D,schema_ref:ID,schema_digest:D,transform_ref:ID,transform_digest:D},model_binding:<GenerationBinding>}`；members1..4，related0..2，identity全ID，user完整≤32768，完整可重建context≤57344。schema／prompt正文为配置绑定运行资源；缺失或摘要不符恢复拒绝，不能重新生成。GenerationBinding恰`{profile_id:ID,config_snapshot_id:ID,profile_revision:ID,price_revision:ID,protocol:OPENAI_CHAT_COMPLETIONS,model_id:ID,capability_evidence_ref:ID,billing_evidence_ref:ID,request_digest:D}`，完整≤2048；request_digest不包含自身或投递deadline，包含材料、参数和全部资源版本。

摘要无自引用规则：context_digest是RequestContext删除model_binding.request_digest后的完整材料投影摘要；先计算它，再构造含该context_digest的Provider规范化请求及request_digest，最后保存GenerationBinding、完整context叶的payload_digest及最终HTTP wire_digest。model_binding_digest是完整GenerationBinding摘要。candidate/work中的context_digest引用第一步材料摘要，不能混用payload_digest或manifest_digest。首次persona没有learning_context行，同样从不可变initial_input＋原run配置资源组成材料投影，按此顺序重建。恢复逐步重新计算和比较；不得从待验证摘要反填材料。

**可信未发送后的重新准入关联修订已批准**：每批唯一context始终保留首个请求的完整材料、键及GenerationBinding.request_digest；其清单、叶、payload／model_binding／wire摘要不改写。仅在原Provider可信NOT_SENT、旧执行者已隔离及原关闭准入回执COMMITTED后，由既有显式新事件或已恢复mode触发reopen_learning_admission。该命令同事务增加admission_generation，以原冻结材料和既有确定性键派生规则得到本代请求；work.model_binding.request_digest记录**本代**完整规范化请求摘要，context_id／context_digest及资源版本保持，不能清空绑定或重新读取当前输入。新旧请求只允许operation_key不同，SYSTEM／USER、参数、attribution及全部资源逐项相同；candidate关联本代原Provider结果，仍引用首个完整context。关联、发送、候选核验、原键确认和OPEN_EXISTING均分别重算首个context摘要与本代work摘要，不能混用。UNKNOWN、未确认提交或尚在途消费者不准重新准入；重复原触发无新发送。不增表、持久命令或配置键，旧组合编码与语义保持。

related_objects.grant_ref为memory对实际已签发读取范围的确定性引用，包含真实数据库／实例及该能力的对象和操作集合。引用本身不是能力，也不能恢复读权；context发布事务仍持有原生MemoryReadPort，逐项复核当前对象、revision及此引用。恢复仅使用已成功发布的原context作为本次原工作证据，不向agent开放历史读取。

CurrentPersonaProjection恰`{publication_id:ID,revision:R,text:Text(1024),generated_at_us:T,review:APPROVED,model_origin:REMOTE_PROVIDER,stale:bool}`，完整≤2048；stale由现行来源变化规则读出，不覆写publication。公开只读不存在返回UNAVAILABLE，不给模型初始化管理写权。

run在prepare写PREPARED且request_id=null；关联后REQUEST_ASSOCIATED，Provider未知时REMOTE_UNKNOWN且无伪造候选；已知失败候选text/digest/handoff均null（如有合法handoff但业务Schema失败可保留其ID，text仍null），review=NOT_APPLICABLE；SUCCEEDED才有非空text/digest/handoff/request且review=PENDING。review只允许一次PENDING→APPROVED/REJECTED并同步run。publication仅APPROVED可建，run的publication_id与唯一publication一致。retry先保留前代候选，CAS清空当前request/resolution引用、递增generation、写新key；不得删除前代。每代请求上限1，数据库新进程依据候选(1..generation−1)与当前run还原全部原请求关联，不能只统计最新一代。

初次管理可信绑定恰`{database_id:ID,instance_id:ID,actor_ref:ID,self_subject_id:ID,self_label:Text(256),input_origin:E(ACTUAL_INPUT,SYNTHETIC_FIXTURE),clock:<原生时钟>,scope:<原生初始化管理能力>}`；SELF由memory注册一次，label不得从模型输出形成。初始化管理输入的input_origin必须与此绑定一致。各K的database身份从B取得，候选Provider终结引用亦仅同库，不能跨库按相同操作键认领。candidate_digest为候选B及正文／来源／Provider／generation等不可变字段的确定性摘要，排除revision/review/reviewed_by/reviewed_at_us/review_operation；review与publish均同时核验该摘要及真实candidate_revision。publication记审核后的candidate_revision，用户审核不改变候选正文身份。run.mode_epoch是最近一次prepare/retry捕获值；READY可使runtime epoch继续增加，恢复要求按保留的真实模式回执链对应，不机械要求历史epoch永等当前值。

### 6.2 受影响既有格式的精确差量（已批准）

下表之外的旧格式、固定分支和旧装配字节保持；新装配版本是独立选择，不让旧解析器接受新增字段。扩展集合是**闭合集合并集／替换**，不接受未列字段。

| 格式 | 精确增量／上限与恢复核验 |
| --- | --- |
| cognition MANIFEST v3 | [原MANIFEST字段](../../companion_memory/cognition/candidates.py)不删，candidate_version改3，新增context_id:ID、context_digest:D、output_schema_revision:ID；origin仅ACTUAL/REMOTE_PROVIDER/MODEL_VALIDATED/database_id，ordered_change_refs动作只CREATE_MEMORY且0..8。其余原字段／稳定ID算法保持，完整4096；leaf沿原完整CREATE_MEMORY变更，object使用下行v2，单叶8192／总73728。非成功保留原handoff_ref可空规则；真实拒绝不能伪造成功交接 |
| MEMORY／RELATION对象v2及其变更 | [原COMMON_FIELDS/ORIGIN/各内容及链接](../../companion_memory/memory/formats.py)字段不增删，object_version改2；origin.model_origin增加REMOTE_PROVIDER、candidate_origin增加MODEL_VALIDATED。新实际学习限定DIRECT_LEARNING＋二者真值，初始人工沿NONE/OPERATOR；模型不产RELATION或维护动作。REGISTER_SUBJECT及SOURCE格式不改；SOURCE_LINK原occurrence_id/interpretation_id必须null且补齐原8字段锚点，不因模型简写省字段。生命周期变更／历史／恢复均保留v2来源，不能构造根时丢失 |
| runtime work.model_binding | 原[关联绑定](../../companion_memory/runtime/content_learning.py)五字段profile_id/prompt_revision/transform_version/candidate_source_fingerprint/request_digest，新增context_id:ID、context_digest:D、output_schema_revision:ID、config_snapshot_id:ID；不含mutation_authority/goal_route_ids（本子集不生成该类候选）。work物理字段不改、嵌套完整8192；source/config/run及context必须一致 |
| runtime模式记录 | 原字段不增删；首次run使用原run_id，epoch真实CAS，首次FINISH只能由下表publish同UoW调用。retry不改mode state但真实更新epoch／修改时间，不能把只读runtime列作writer |
| Provider requests v2 | [stored_schema.requests](../../companion_memory/provider/stored_schema.py)完整旧字段集合不变；format_version/fingerprint_version=2，source=REMOTE_PROVIDER，configuration_origin=PERSISTED_CONFIGURATION；config_snapshot_id/profile_revision/price_revision必为真实非nullID。execution_evidence的profile/account改§7结构，其余字段不变；attribution及phase/terminal枚举沿原，只接受LEARNING/PERSONA、GENERATION。完整8192，旧UNVERSIONED分支不重解释 |
| Provider attempts v2 | 原字段保留，仅新增required、nullable的terminal_error，使用现有固定code/field/reason封套，不增错误枚举。PREPARED／REMOTE_RESULT_UNKNOWN及SUCCEEDED为null；已知失败与已知NOT_SENT保存真实最终错误，first_error保留首个真实错误。wire_protocol=OPENAI_CHAT_COMPLETIONS、ordinal固定1，usage用UsageV2；state沿PREPARED/COMPLETED/NOT_SENT/REMOTE_RESULT_UNKNOWN。model身份分别在handoff与配置证据，不把request别名写成已确认后端；完整8192 |
| Provider预算／预留／cost_items | budget_windows保留原字段并增format_version=2、quota_reserved:U、quota_known:U、quota_held:U；policy替换§7Account。reservations保留原字段并增format_version=2、quota_reserved/known/held:U。cost_items统一原共同字段＋format_version=2、quantity:U?、price_numerator:U?、price_denominator:U?，删除分支price_atoms；item封闭input/cached_input/output/subscription_request/reported，unit为TOKEN/SUBSCRIPTION_REQUEST/CURRENCY_ATOM，source为PROVIDER_REPORTED/LOCALLY_ESTIMATED/UNAVAILABLE；每项不重复计总数，唯一attempt_id＋item保持。完整每行8192，不增加表 |
| UsageV2 | 原usage顶层fields/raw_usage/source/coverage/cost_complete/known_cost_atoms/known_subtotal_atoms/held_atoms/estimated_cost_atoms/reported_cost_atoms/price_revision/items/valid/cost_disagreement全保留；新增format_version=2、currency:CNY或USD、atom_scale=1000000、billing_mode:TOKEN_METERED或SUBSCRIPTION、quota_known:U?、quota_held:U。fields在原10字段加total_tokens；非生成字段全null；raw_usage恰prompt_tokens/completion_tokens/total_tokens/cached_tokens/reasoning_tokens/provisioned_input_tokens/provisioned_output_tokens（均U?）。items恰§7计费模式的3个token项或1个subscription_request项，每项{item,quantity:U?,price_numerator:U?,price_denominator:U?,cost_atoms:U?}；estimated_cost_atoms变U?，不能把未知估算填0。source按§3，price_revision必ID；完整≤4096 |
| Provider handoffs及结果端口 | 原字段不删，format_version=2、source=REMOTE_PROVIDER；payload为成功时完整StructuredGenerationResult的规范化JSON，失败沿原无payload分支；旧字符串持久叶仍8192，整个行＋body点读需校验≤65536。原TerminalVerified/VerifiedTerminal原生字段不增删，request指向上述v2完整记录、result指向v2交接；由此取得config/profile/price及requested/reported/resolved model证据，不能另造未绑定证书；结果owner授权不增加历史审计读取权 |
| 业务结果／审计事实 | 原内容命令结果中的model_adapter/candidate_origin仅在实际真实关联的分支扩展REMOTE_PROVIDER/MODEL_VALIDATED；不相关旧维护结果不改标。新增命令用§6.3 NewResult。旧事实schema／slot及memory from_seq/to_seq原样，新增引用只在原references容量内。Provider CHANGE增加billing_mode、currency、quota_known:U?、quota_held:U、config_snapshot_id:ID，原必要slot provider_change不删 |
| 配置／装配 | 完整持久条目编码v2、六域目录（新增text_learning）及§7定义／文本专属关系；配置catalog版本从3改4，configuration仓储schema_version从3改4（字段结构不改，目录新增text_learning且全域绑定）；顶层语义格式MODEL_TEXT_LEARNING_V1绑定材料版本及媒体模型关闭能力，旧载体读取分支不改。新initialize_text_learning替换本组合initialize_information，原owner及原回执／结果结构不变，domains／entries范围至6／118；读取／适用性检查重验完整元信息与关系，不能仅比较118条或保存值；新旧组合双向拒绝，无迁移 |

恢复次序：各owner先独立全检精确Schema、唯一约束、完整字节、revision／摘要，再经公开端口核对self_subject与input、run每代Provider原操作与结果／预算、candidate与handoff／输入／资源、publication与APPROVED candidate及原发布回执／runtime模式。learning context核对冻结source、原persona及请求指纹，再核对v3 candidate、正式对象／来源与finalize。release必须对应原终结回执，不能凭缺叶当已释放。必要审计与结果绑定逐slot校验；被保留的原回执而非当前值是确认依据。跨owner任一缺失／错版本／错身份／重复／revision不符均固定INTEGRITY_FAILURE，拒绝相关READY；不迁移、清审计或自动生成。历史publication输入保留是该首份persona的来源依赖，不向普通记忆检索开放失败候选。

### 6.3 命令、分支、真实writer与审计（已批准）

本表是新组合的闭合差量，非可执行声明。共同输入I恰`operation_id:ID`加各行参数；审计意图恰actor:ID，由原生权限绑定，不能由模型选择。所有写在同UoW中核对原键／完整内容指纹，再owner写、必要审计、绑定证据及回执；用户可见返回沿COMMITTED/NOT_COMMITTED/REJECTED/UNCONFIRMED原封套，cleanup独立。只读前置与原键Found不进handler、不写新审计。不同键遇到已经完成／已存在返回PRECONDITION_FAILED（可先只读显示原结果），不制造零写成功回执。

新增统一NewResult恰`operation_id:ID,state:<本命令封闭成功态>,references:Ref[1..8],facts:{<本分支owner>:NewFact},targets:Target[1..8]`；Ref恰`{kind:E(INPUT,SELF,RUN,RESOLUTION,PUBLICATION,CONTEXT,WORK,MODE),object_id:ID,revision:R}`；Target恰`{object_id:ID,previous_revision:R?,revision:R}`；NewFact恰`{rows_changed:1..16,references:Ref[1..8]}`。各owner事实完整≤2048、result≤8192。targets是本次真实根记录写入的去重集合，references可含该根真实叶写入的计数，不造placeholder；新建previous_revision=null，更新必须+1。每owner一个`<owner>_text_learning`必要slot，event_code为固定大写命令名，reason=APPLY，target_refs绑定RESULT.targets、change绑定RESULT.facts.owner、actor_ref绑定INTENT.actor；root空targets在装配拒绝。基础保留命令继续其原slot名称。

register_initial_subjects使用独立窄结果：Ref.kind仅SUBJECT、references及targets为1..6，memory.rows_changed为1..6，其余结构沿NewResult，旧Ref枚举不扩大。一次完整登记后拒绝其他新键；同原键完整输入先确认历史回执，即使当前已进入persona模式也不重新写入。每主体完整编码≤1024，完整输入≤8192、事实≤2048、结果≤8192、原回执≤65536。memory增加一条同scope的只读有界存在性查询（LIMIT 1）用于拒绝已有非SELF主体；无新增表、索引或配置键。不允许SELF、更新、删除、重名归并、模型造主体或普通HTTP登记。首次准备与登记同事务核对持久run／publication／mode，原生管理调用及commit permission持续保有到真实清理结束。主体原格式全检、必要审计及原键恢复遵守原owner规则，新装配签名不迁移旧库。

| 新增命令（10条）／成功分支 | I之外完整参数；成功state | 实际写owner／slot数量；targets来源 |
| --- | --- | --- |
| register_initial_self／PRESET或NO_PRESET | input_kind、body:Text(2048)、input_origin；REGISTERED。ID／label等由可信初始化绑定，NO_PRESET仍有原用户输入 | memory／1；真实SELF与input新行；输入和身份一次提交，已存在SELF或input拒绝，不改名重建 |
| register_initial_subjects／FIRST | subjects:原SUBJECT[1..6]、input_origin；REGISTERED。原键、完整输入及actor来自可信本机初始化能力；已有SELF/input且首次persona准备前、NORMAL模式才能执行 | memory／1；一次新建最多6项非SELF主体（PLATFORM_PERSON、FICTIONAL_CHARACTER、CONTEXT各至多2），所有revision=1且实例及身份唯一。复用memory.apply_change_set REGISTER_SUBJECT，同事务主体／必要审计／原回执；只读self_model／runtime不写审计 |
| prepare_initial_persona／FIRST | input_id:ID, expected_self_revision:R, expected_epoch:R；PREPARED | self_model＋runtime／2；新run、原mode ENTER至PREPARING。真实input只读不写memory审计；原READY截点后才可generate |
| associate_initial_persona_request／PREPARED | run_id:ID, expected_revision:R, generation:1..3, expected_epoch:R；REQUEST_ASSOCIATED | self_model／1；run更新（已冻结key及GenerationBinding摘要）；发送在提交后，runtime gate同截点核验但只读 |
| confirm_initial_persona_request／ASSOCIATED | run_id:ID, expected_revision:R, generation:1..3, request_id:ID；REQUEST_ASSOCIATED | self_model／1；run填真实request_id，与Provider原键确认一致；不同值拒绝，同值只读返回 |
| record_initial_persona_resolution／SUCCESS、KNOWN_FAILURE、NOT_SENT、UNKNOWN | run_id:ID, expected_revision:R, generation:1..3, provider_reference:ID, evidence_revision:U；WAITING_REVIEW、KNOWN_FAILED或REMOTE_UNKNOWN | self_model／1；成功／已知失败写本代candidate和run，UNKNOWN只更新run无candidate。正文由公开owner恢复结果派生，不接宿主自由候选；provider_reference在已登记分支是request_id，在未登记NOT_SENT分支是原provider_operation_key；NOT_SENT须可信verify_unsent且旧执行者隔离证据 |
| review_initial_persona／APPROVE或REJECT | run_id:ID, expected_revision:R, candidate_id:ID, candidate_revision:R, candidate_digest:D, decision:E(APPROVE,REJECT)；APPROVED或USER_REJECTED | self_model／1；candidate的review及run状态同时更新；用户身份来自管理能力；不接受正文替换 |
| retry_initial_persona／NEXT | run_id:ID, expected_revision:R, expected_generation:1..2, prior_resolution_id:ID, expected_epoch:R；PREPARED | self_model＋runtime／2；run代次＋原mode epoch真实CAS，同一持久专注模式；§5.1任一不满足零写拒绝 |
| publish_initial_persona／FIRST | run_id:ID, expected_revision:R, candidate_id:ID, candidate_revision:R, candidate_digest:D, expected_epoch:R；PUBLISHED | self_model＋runtime／2；新publication、run终态、原mode FINISH至DRAINING；memory／Provider只读，候选已APPROVED不再改review |
| stage_learning_context／FROZEN | batch_id:ID, expected_revision:R, generation:R, manifest:Text(8192), leaves:Text(8192)[1..8]；CONTEXT_STORED | cognition＋runtime／2；新context／全部叶，work.model_binding原子关联context（work仍FROZEN）；不提前关联Provider发送，原冻结所有者只读 |

所有失败分支统一安全拒绝／回滚，不生成正文、对象或审计；提交是否确认沿持久封套，不能把UNKNOWN包装成handler零写。record_resolution的重复相同证据只查原命令回执，已知终态不允许不同证据覆盖；UNKNOWN→已知只接受原Provider保留迟到证据且本地服务仍允许该写，不能远程查询补造。generate_initial_persona是上述associate→Provider→confirm→resolution的受控编排端口，**不是额外持久命令**。

| 改变原命令（13条） | 分支／输入输出与实际writer、slot、targets |
| --- | --- |
| configuration.initialize_information→initialize_text_learning（替换1条） | FIRST及原键确认；输入完整六域配置，输出沿原初始化结果；configuration／configuration_initialized 1slot，targets为原snapshot根及真实六域初始化事实（原结果结构仅domains上限改6），定义和值一起持久。零变更原键确认不审计 |
| Provider register | OPEN／已知准入拒绝TERMINAL；原changes[1..16]、event≤2048输入，结果原object_id/revision；改v2格式。provider／provider_change 1slot，原request真实revision |
| Provider prepare | 唯一首attempt，request/attempt/reservation/budget真实变化；同原输入输出，provider 1slot，attempt实际ID／revision；不会采用第二attempt |
| Provider settle | SUCCEEDED／KNOWN_FAILURE／NOT_SENT／UNKNOWN；同原输入输出，provider 1slot；request、attempt、usage／费用项、budget／reservation、成功handoff一事务。实际变更≤16，原attempt及request引用必匹配 |
| Provider terminate | 无attempt或已知终态逻辑终结；provider 1slot，原request引用，保留费用责任 |
| Provider recover | 原无主PREPARED→REMOTE_RESULT_UNKNOWN；provider 1slot，真实attempt／request；不出站、不判成功 |
| Provider evidence | 原活owner迟到已知证据补全；provider 1slot，证据revision和原attempt；重复证据原键确认，不二次释放责任 |
| Provider initialize_budget | 新account/window；provider 1slot，真实budget根；原键不重置金额／额度 |
| information_associate_content_request | 原参数和runtime_content slot不删，model_binding用§6.2；原work从FROZEN/PARKED→REQUEST_ASSOCIATED。仅runtime writer，targets为真实work记录；必须已有完整context，重复不出站 |
| information_store_content_candidate | 原参数保持，manifest改v3；runtime／cognition／ingress各原content slot，3writer，source保护、candidate叶和work一次提交；无媒体分支，不增media slot；targets为实际candidate/work/source保护根 |
| information_commit_content_published | 原参数保持、v3+v2，SUCCEEDED且1..8条；runtime/cognition/ingress/buffers/memory五个原content slot，memory含真实from_seq/to_seq；context全部叶释放及RELEASED墓记并入原cognition效果，targets与原真实对象／协调根相符 |
| information_commit_content_without_objects | 原输入保持，SUCCEEDED零条／FAILED_DROPPED／SENSITIVE_DROPPED三个原终态；runtime/cognition/ingress/buffers四原slot，cognition同事务释放context；memory无写不审计，无虚假正式对象／source。供应商不给细粒度敏感证明时真实路径不能选择敏感终态 |
| change_content_mode | ENTER/READY/FINISH/FAULT/DRAINED原输入／输出与runtime_mode 1slot保留；对首次run禁普通外部FINISH，仅publish内固定调用同owner效果，不能额外嵌套执行本命令；READY读取run绑定、健康及旧资源，重复／错epoch零写拒绝 |

计数：原94条中13条在新组合替换定义，另新增10条，**新组合104条**；原组合94条不变，新增表由原26增至本新组合32（另6表），并非“旧库加表迁移”。新组合保留其余81条原定义及原所有writer／固定释放分支，仅新v2对象解析由明确owner格式选择；真实学习不会走with_media／goal／mutation分支，旧原生维护仍可对v2合法对象执行，不获模型授权。上述改动输入／结果格式变为新装配签名，不能在旧装配重解释原命令键。


新文本attempt的最终错误已批准独立持久保存。先超时并持久UNKNOWN、同一worker后交回完整length时，request保持原超时first_error并转TERMINAL/FAILED；attempt保持原超时first_error、转COMPLETED/FAILED，terminal_error固定ADAPTER_FAILED/adapter/OUTPUT_LIMIT，persona读取原生最终原因形成KNOWN_FAILED/OUTPUT_LIMIT。普通超时没有完整最终证据仍为UNKNOWN，不推断length；成功终态terminal_error为null。已登记零attempt沿真实请求终态，不补造attempt。原生终态证明按实际attempt完整记录推导最终原因，完成K确认及事务核验重新比对同一份最终证据；原生公开证明字段不增加，内部有限保留的原attempt随证明释放。首次错误、费用／提交确认／实际清理分别核验，费用缺失继续持有责任。

最终错误纳入原evidence命令指纹及重复内容比较；相同完整证据幂等，既知terminal_error／终态／usage／结果指纹冲突安全拒绝，不能覆盖原回执或已知事实。新文本命令的固定input_policy签名为TEXT_PROVIDER_MUTATIONS_V3，打开比较拒绝旧文本声明；不做迁移。旧模拟及信息组合字节／指纹保持，命令为104、六张新增表、118键；各行8192、回执65536、普通命令1MiB、静态预算及唯一配置初始化2MiB均不变，新增字段与完整封套须实际编码验证。

固定错误封套沿各owner已有error结构。Provider新增固定错误组合为UNSUPPORTED_CAPABILITY/capability/PROTOCOL_UNSUPPORTED、CONFIGURATION_REJECTED/configuration/MODEL_BINDING_MISMATCH、PAUSED_BUDGET/budget/BILLING_EVIDENCE_MISSING及ADAPTER_FAILED/adapter/OUTPUT_LIMIT；runtime新增reason仅PERSONA_REQUIRED、CONTEXT_UNRECOVERABLE、INITIAL_RETRY_NOT_ALLOWED（code分别PRECONDITION_FAILED、INTEGRITY_FAILURE、PRECONDITION_FAILED）。configuration仍用现有VALUE_INVALID/DEFINITION_MISMATCH/CAPACITY_INSUFFICIENT语义及固定field，新增operation为resolve/persist/load_text_learning_configuration与text_learning_snapshot_issue。新self_model封套恰code/operation/field/reason：code为INVALID_INPUT/ACCESS_DENIED/PRECONDITION_FAILED/INTEGRITY_FAILURE/RESOURCE_BUSY/PERSISTENCE_FAILED；operation限本节8个persona／self管理命令及read_current/verify_current/recover_local；field为input/identity/state/revision/provider/configuration/storage/resource；reason为INVALID_SHAPE/BOUNDARY_DENIED/REVISION_CONFLICT/STATE_MISMATCH/BINDING_MISMATCH/RECORD_INVALID/CLEANUP_PENDING/COMMIT_UNCONFIRMED。固定合法组合为INVALID_INPUT/input/INVALID_SHAPE、ACCESS_DENIED/identity/BOUNDARY_DENIED、PRECONDITION_FAILED/revision/REVISION_CONFLICT、PRECONDITION_FAILED/state/STATE_MISMATCH、INTEGRITY_FAILURE/identity/BINDING_MISMATCH、INTEGRITY_FAILURE/storage/RECORD_INVALID、RESOURCE_BUSY/resource/CLEANUP_PENDING、PERSISTENCE_FAILED/storage/COMMIT_UNCONFIRMED；其他组合拒绝。不允许外部指定code/reason；详细原始异常、路径及供应商消息不外泄。

### 6.4 公开端口与原生恢复授权（已批准）

以下均精确原生类型，错误封套见§6.3。跨owner只经端口；序列深不可变、有界，不传游标、任意Any、SQL或回调。UoW内端口同步且无网络／等待；外部读取使用共享绝对deadline和本操作完成通知。只读端口不计入104条持久命令。

| 端口 | 完整输入／返回与权限 |
| --- | --- |
| InitialSelfPort.read_initial | (input_id:ID, deadline:<原生绝对期限>)→Found(§6.1完整input, 原SELF记录)/Missing/Failed(固定error)。只签发给本实例首次整理owner，不开放所有memory表 |
| CurrentPersonaPort.read_current | (deadline)→Available(CurrentPersonaProjection)/Unavailable/Failed；完整投影≤2048，交付复用已有末端原生权限／HTTP会话／mode仲裁 |
| CurrentPersonaPort.verify_current | (uow:原生UoW, publication_id:ID, expected_revision:R)→Matched/Conflict/Failed；必须同实例当前唯一发布，验证source滞后与scope但不生成 |
| PersonaInitializationPort | 9条管理命令的类型化输入见§6.3，输出原持久封套；generate(run_id,generation,original_key,deadline)是编排入口，返回SavedResolution/RemoteUnknown/LocalUnconfirmed/Rejected。SavedResolution含已持久SUCCEEDED／KNOWN_FAILED／NOT_SENT候选及真实结果登记回执；未登记NOT_SENT的request引用为空，安全run引用及cleanup_pending保留。依赖同一原生初始化管理grant，禁止普通调用者伪造internal_dream |
| LearningContextPort.stage／release | stage(uow,完整manifest,完整leaves)→Staged(context_ref)/Rejected；release(uow,context_id,原finalize身份)→Staged(revision,deleted_leaf_count)/Rejected；只参加§6.3固定UoW，不自行开事务 |
| LearningContextPort.load | (context_id,deadline)→Stored(manifest,leaves)/Released(manifest)/Failed；仅对应run原生恢复grant。STORED缺叶安全失败，RELEASED不重新收集现有输入 |
| Provider.bind_generation_resources | (已持久文本配置视图,RealGenerationResources)→Bound/Rejected；只Provider绑定网络和凭据，实例并发／请求登记容量由公开typed ResourceLimits(max_in_flight:1,registered_work_limit:1)预检，不写Provider私有_registration_limit。绑定失败仅回收本次持有的能力 |
| WorkPort.generate及原键确认 | 原顶层请求／attribution字段沿[基础请求](provider.md#provider-foundation-ports)，替换GENERATION payload恰{format_version:2,messages:[{role:SYSTEM,text:Text(4096)},{role:USER,text:Text(32768)}],schema_ref:ID,schema_digest:D,output_tokens:1..2048,reservation_input_bound:U,context_digest:D}；完整语义请求131072。生成资源用固定角色选择Schema，模型profile和能力须与持久配置匹配。lookup_request/verify_unsent仍基于原完整请求；deadline/取消等投递字段不进身份 |
| ResultOwnerPort.verify_terminal／recover_result | 原精确request allowlist；返回原VerifiedTerminal／v2完整交接，不能请求任意batch历史。request已知终态、已确认本地回执及consumers_ended分别核验；只有原native permit的dispatch能新发，恢复grant没有该permit |
| ResultOwnerPort.confirm_completion | 对同一原生VerifiedTerminal在原精确request allowlist内确认§6.1实际完成K；返回绑定原terminal的原生ConfirmedCompletion(terminal,operation:K)或沿recover_result固定Failed封套。Provider仅枚举自身固定终结路径的原键，核验实际回执结果根、必要审计及当前request完整版本，恰一个匹配才交付；不按revision猜K。旧VerifiedTerminal字段不变，不增业务命令／表／键、审计历史读取权或发送许可；确认使用同一有界读占用、共享绝对期限及交付前撤权核验，清理与费用完整性仍须独立核验 |
| Provider同事务原记录核验 | WorkPort.verify_request_in_transaction(uow,request_id,完整规范化原请求)→Found(原request)/Failed；ResultOwnerPort.verify_completion_in_transaction(uow,原生ConfirmedCompletion,retry_work?)→Found({request_id,revision})/Failed。仅新文本装配声明6条固定只读查询，按原生工作caller_scope连接Provider共享账本，已登记分支另与实际request.caller_scope核对，保留原存储UoW同作用域校验；不授予任意作用域或账本写权。完成证明必须仍绑定同Provider、原allowlist与当前完整request版本。WorkPort.verify_unsent_in_transaction(uow,原生VerifiedUnsent,完整规范化原请求,retry=false)额外用两条查询核对本scope原键实际登记缺席及可选的当前账户窗口预算；仅self_model/PERSONA原生工作能力可用，未登记证明必须仍由同Provider持有原键封闭准入，消费者及所有查询已结束。retry_work仅接受同Provider已签发self_model／PERSONA原工作能力，事务内核验本代实际消费者已结束、费用／额度完整及当前累计预算，不预留新费用、不发送。交付与提交前重新核验撤权和健康；必要Provider写审计不因只读参与而增加，原七命令不增删 |
| recover_local | (原native recovery grant,deadline)→LocallyRecovered/Pending/Failed；先各owner本地再交叉，结果附mode／learning_ready／cleanup_pending安全观察。不会签发普通或发送grant，不读取秘密；宿主最终READY仍经过生命周期／健康截点，不由本返回单独开放服务 |

## 7. 完整配置差量与静态容量（方案已批准；实物验证另记）

### 7.1 定义、计数与封闭值

新独立resolve_text_learning_configuration／persist／load沿[配置注册与显式解析](configuration.md#configuration-registry-contract)、[持久信息组合](configuration.md#local-information-configuration-draft)，不修改旧入口。完整单平台组合为foundation40＋runtime22＋platform7＋content36＋information8＋text_learning5＝**118键、6域**。113键原集合中只有下列明确替换值／定义，其他[原推荐向量](configuration.md#local-information-configuration-draft)逐项继承；本包不注册embedding profile，也不提供动态扩展键。118为本声明静态计数；实际构造与受测版本见[当前任务](../work/CURRENT_TASK.md)。

五个新增object键恰`provider.transport, provider.generation, cognition.text_context, cognition.text_output, self_model.initial_persona`，在新text_learning域；对应owner为provider/provider/cognition/cognition/self_model，consumers分别provider；provider,cognition,self_model,runtime；cognition,runtime；cognition,memory,runtime；self_model,runtime,retrieval。五个validator恰text_transport/text_generation/text_context/text_output/initial_persona，各只绑定同名对应键，无运行时动态装载。

每项的28字段定义统一：key如上，schema_revision=text_learning_v1；type=object；default=NoDefault；required=true，nullable=false；unit/range/enum/activation_group/replacement/upgrade_rule均NotApplicable(reason固定“Closed initialization record”)；scope=[instance]，override_policy=no_override，sensitivity=public，read_roles/write_roles=[trusted_operator]，apply_mode=INITIALIZE_ONLY，deprecated=false；owner_module/consumers如上；validator=[对应固定项]；dependencies见下行。description/rationale/validation_method/cost_impact/migration_impact分别用固定说明“Bounded <key> configuration”、“Explicit immutable input”、“Closed schema and cross-owner capacity validation”、“Requests and costs remain within the bound policy”、“New assembly only; no existing database migration”。说明字段每项≤192 UTF-8，NotApplicable.reason≤64，Identifier≤128，单条完整编码≤8192；不在说明中塞Schema或秘密。

五项definition.dependencies的同域精确最小集合：provider.transport→[]；provider.generation→[provider.transport]；cognition.text_context→[provider.generation]；cognition.text_output→[cognition.text_context]；self_model.initial_persona→[provider.generation,provider.transport]。另外由新完整组合入口固定核验跨域关系：transport→foundation的provider.accounts/provider.request_timeout_ms；generation→foundation的provider.profiles/provider.accounts；text_context→runtime的ingress.event_max_bytes/learning.material_max_bytes/learning.input_units_limit；text_output→content的cognition.candidate_item_limit/cognition.candidate_item_max_bytes/cognition.candidate_max_bytes/memory.current_max_bytes；initial_persona→foundation的provider.accounts及runtime.focus_drain_timeout_ms。跨域关系不伪装成单域registry中已注册依赖，也不允许删除关系逃过复检。各值结构验证后依赖／跨域验证，先定义再值，确定键排序；失败沿原封套，不允许循环读取环境或在线试调用。运行资源正文只持ID／digest，不复制入配置条目。

| 继承键／新组合取值或精确Schema替换 | 消费者／定义差量 |
| --- | --- |
| provider.max_in_flight=1，request_timeout_ms=60000，retry_delay_ms=0，request_max_bytes=131072 | 原integer范围／单位／owner/consumer不变；只新显式值变化 |
| provider.close_timeout_ms=10000，result_max_bytes=8192，query_row_limit=100 | 值和定义不变，列出以避免隐藏默认 |
| provider.accounts／provider.profiles／provider.role_profiles | 原顶层类型保留，validator改text_accounts/text_profiles/text_role_profiles；嵌套替换为下文Account[1]／Profile[1]／恰{LEARNING:[profile_id],PERSONA:[同profile_id]}；不保留MEDIA映射假称真实理解可用；definition.dependencies沿原foundation域必要集合，provider.generation由完整组合交叉核验（不将text_learning键塞进foundation registry）。owner=provider，consumers含provider/runtime |
| learning.material_max_bytes=73728 | 仅文本组合将integer闭区间从256–49152改为**256–73728**；单位bytes，owner=cognition，必要consumers仍runtime/buffers/cognition。限制§6.1完整context清单＋全部叶，不是单行、原base64材料或token；还须满足下文实际材料总界，不能仅凭值在范围内通过 |
| learning.input_units_limit=49152 | 值及integer闭区间256–1048576保留，定义单位由simulated_input_units改为**bytes**；owner=cognition，consumers=cognition/runtime。只限本地SYSTEM＋USER两段文本的UTF-8字节总和，不含持久叶头／manifest；**不与真实token数量比较或传作token** |
| learning.output_units_limit=2048 | 新组合定义单位由simulated_output_units改tokens，integer范围1–8192及值2048保持，owner=cognition、consumers=cognition/runtime/provider；generation.max_tokens必须相同。不能将旧SIMULATED输出units重标 |
| runtime.max_active_entries=1 | 值、integer范围1–8、entries单位、owner=runtime及consumer=runtime保持；validator由runtime_limits替换为text_runtime_limits；同runtime域必要dependencies仍为ingress.event_max_bytes及三个learning键。只在文本组合执行下文窗口／字节／token分域关系，不能进入旧base64关系后豁免错误 |
| media.processing_concurrency=0 | 仅文本组合将integer范围1–2替换为**0–0**，单位jobs，owner=media、consumers=media/runtime，其他元信息保持。这是固定无媒体模型处理能力，不是可热改禁用开关；值1或2均拒绝，不建立模拟处理任务 |
| media.blob_max_bytes=1048576 | 值／integer范围1–1048576／bytes／owner=media／consumers=media/ingress保持；validator由media_resource_limits替换为text_media_resource_limits，原content域media必要dependencies保持。只替换下文MEDIA与模型并发关系，全部本地文件、目录、读取、GC及容量保障保留 |

因此新增5键；113个继承键中Provider7项、learning3项、runtime1项及media2项共**13个受影响键**，其余100项的定义／显式值沿用。新增键owner／consumer见上；表中未列的继承元信息（包括无默认、初始化生效及必要依赖）保持原正文，不用近似范围替代完整定义。固定验证器新增5个object项＋3个Provider替换项＋text_runtime_limits／text_media_resource_limits，共10个新绑定名；后两个只绑定对应表行，不新注册媒体开关或占位profile。仅新文本组合的单条和body总限额为8192／524288，唯一配置初始化完整冻结载体2MiB；域上限6、条目上限128保持。原information五域不接受这些替换。

**文本组合精确关系替换（已批准）**：原规则位置为[配置§11.15／§11.16](configuration.md#local-information-configuration-draft)、[content_resolution._relationships](../../companion_memory/configuration/content_resolution.py)和[validate_content_relationships](../../companion_memory/configuration/content_validation.py)。下表是新组合的闭集替换；旧resolve_content_configuration／resolve_information_configuration及其snapshot_issue／persist／load保持原定义和关系，不能先通过新规则再伪装旧candidate，也不能捕获旧错误后继续签发新candidate。

| 原关系／职责 | MODEL_TEXT_LEARNING_V1的精确规则 |
| --- | --- |
| 原ContentMaterialContract及`window_bound≤min(material,input_units)` | 新原生材料声明固定§6.1 context_version=1、H/T/R=1/2/1、事件完整≤2048且无媒体、完整USER≤32768、SYSTEM≤4096；拒绝旧complete_source_base64声明。分别核验SYSTEM＋USER字节≤learning.input_units_limit（最坏36864≤49152），可重建context≤57344，完整manifest＋至多8叶≤learning.material_max_bytes，且`text_context.total_max_bytes=learning.material_max_bytes=73728`。完整73728不得再与49152取min；manifest／叶各≤8192，单叶text≤7168，编码命令另受§7.2约束。全部跨域等值／上下界由configuration新入口校验，不能删除材料检查 |
| 原input_units≤LEARNING profile.max_input_units及output比较 | 删除**本新组合**的字节对token比较；必须有且仅有下文一个真实GENERATION Profile，LEARNING／PERSONA共用。输入profile.max_input_units=reservation_input_bound（token责任），输出`learning.output_units_limit=Profile.max_output_units=generation.max_tokens=2048`；同单位校验`reservation_input_bound+max_tokens≤model_context_tokens`，该context值须为官方证据覆盖全部允许后端的共享输入＋输出窗口下界；input bound另须覆盖本请求所有计费输入（含Schema／协议开销），不能仅从前式反推它足够。若供应商窗口定义不同或无法证明这两个前置，则本封闭组合不进入发送就绪，不能偷偷改比较口径。未有token估算仍为null，不用49152或HTTP字节推token。Profile.max_items=2且请求消息恰2；实际请求和Chat线上编码分别受131072硬限，替换原`4096+base64预算+行分隔符`证明 |
| 原媒体模型并发＋runtime入口数≤Provider上限 | 处理并发固定0，runtime.max_active_entries=1，provider.max_in_flight=1；`0+1≤1`成立。首次persona与普通学习以原模式互斥，所有生成还共用Account.max_in_flight=1及network_slots=1，无独立persona旁路槽；排队0。关闭／超时仍在途时继续占原槽，不能因为模式切换、处理配置0或客户端有界返回而释放 |
| 原必须存在MEDIA_UNDERSTANDING profile且blob≤其输入字节界 | 新文本组合要求role_profiles键恰LEARNING／PERSONA，Profile集合恰一个GENERATION、media_tasks=[]；任何MEDIA角色／媒体profile／模拟补位适配器或processing_concurrency>0都拒绝。只有此固定无媒体模型能力分支取消“必须存在MEDIA profile”及其blob对模型输入比较；不按任意缺profile泛化为通过。宿主不绑定媒体模型发送端口或启动理解worker，文本接收在持久冻结／Provider登记前拒绝含媒体事件；发现待媒体理解持久work则绑定不匹配，恢复不补适配器、不发送 |
| 原媒体本地资源与其他内容关系 | 全部继续核验：`file_worker_capacity≥upload+read+processing`（保持5≥2+2+0=4），块≤blob，processing_suspect_after>Provider总期（120000>60000）、occurrence总期≥Provider总期（60000≥60000），preparation总期≥`4×2×60000+4×30000=600000`。即便理解不可用也保留这些已有配置相容约束，不把超时配置解释成理解已启用。目录／staging关系、完整保护目录上下文、实际同文件系统／隔离／独占身份、读写／GC／关闭和在途资源校验照旧；文件服务只按原授权端口提供本地能力，不新增媒体接入授权。F<H、候选／历史容量、存储57344及933888下限、观察预算与information全部关系均保留，不整体跳过content校验 |

上述替换须在新完整解析、适用性复检、保存及OPEN_EXISTING按同一封闭规则执行；媒体目录不能填空、错目录不能因processing=0通过。新快照绑定真实材料／无媒体模型能力，旧库与旧快照不迁移；旧组合的material=73728或processing=0、缺MEDIA仍按旧规则拒绝，新组合带旧单位／范围／验证器／材料版本也拒绝。这个独立组合增量已在本契约批准，旧组合仍遵守[配置正文](configuration.md)。

DeepSeek新组合的精确补充及已批准调用授权见§3.3.2；下表保留旧组合定义。

本次MiniMax采用§3.3.1的精确闭合替换：transport固定api.minimax.cn／v1；Generation／Profile模型MiniMax-M3、protocol／wire_protocol=MINIMAX_CHAT_JSON_V1、response_mode=JSON_PROMPT_V1、expected_reported_models仅MiniMax-M3、resolved_model_id=null。其余方舟字段结构保留，max_tokens/n是本地语义约束，不作为MiniMax线上字段。Account／Profile的billing_mode新增USAGE_ONLY_TRIAL，Account.cost_limit_atoms允许且仅该模式必须0；Price的全部金额字段及Quota均null。完整usage使用§3.3.1的UsageV3与计量项v3；request、attempt、预算、预留和handoff的既有版本及字段结构不改变，恢复逐层核对协议／账户组合。prompt资源摘要覆盖完整附加指令与Schema。此段是下表方舟定义的已批准精确补充，不要求为MiniMax提供下表方舟价格／订阅扣额。

以下列举所有嵌套字段；记号沿§6.1，全部required，?显式nullable。字节边界是整个对象上限，校验不接受待定占位。`待供给`只存在本文，运行配置必须提供实际值；当前不能解析成可发送快照。

| 结构（完整字段） | 固定值／限制、来源与跨字段条件 |
| --- | --- |
| provider.transport | origin:Text(256), base_path:Text(128), endpoint_path:Text(64), secret_ref:ID, secret_revision:ID, account_ref:ID, connect_timeout_ms:1..10000, read_timeout_ms:1..30000, response_max_bytes:256..262144, headers_max_bytes:256..16384, header_count:1..100, chunk_bytes:256..8192, network_slots:1, queue_slots:0。推荐取上述数量最大值；origin=https://ark.cn-beijing.volces.com、base_path=/api/coding/v3、endpoint_path=/chat/completions。完整≤2048；无query/userinfo/fragment，不含secret值。前三项固定本绑定，不能自由地址试探；account_ref匹配Account |
| provider.generation | protocol:OPENAI_CHAT_COMPLETIONS, model_id:ID, expected_reported_models:ID[1..8], resolved_model_id:ID?, capability_evidence_ref:ID, billing_evidence_ref:ID, eligibility_evidence_ref:ID, schema_ref:ID, schema_digest:D, prompt_ref:ID, prompt_digest:D, transform_ref:ID, transform_digest:D, model_context_tokens:1..1048576, reservation_input_bound:1..1048576, max_tokens:1..2048, n:1, stream:false, response_mode:JSON_SCHEMA_STRICT, schema_max_bytes:1..12288, local_generation_limit:1, tool_steps:0。model_id固定ark-code-latest；max_tokens在本试验包显式2048；schema_max_bytes=12288；三证据引用／实际模型集合／context上界待供给。input_bound须覆盖实际模型可能输入责任且有依据；不能取一个小数冒称token预检。完整≤4096；schema资源为§4或§5按角色固定选择，persona资源的独立身份在initial_persona；模型身份／费用漂移先停止 |
| Account | account_id:ID, window_id:ID, currency:E(CNY,USD), max_in_flight:1, attempt_limit:1..16（本试验包16）, cost_limit_atoms:1..10^12, atom_scale:1000000, billing_mode:E(TOKEN_METERED,SUBSCRIPTION), price:<Price>, quota:<Quota>?, evidence_ref:ID。账号／币种／金额及证据待供给，不从文件存在推定；两角色共账户。完整≤4096；无自动重置或热改 |
| Price | revision_ref:ID, source_url:Text(512), checked_date:Text(10), input_atoms_per_million:U?, cached_atoms_per_million:U?, output_atoms_per_million:U?, per_attempt_money_bound:U?。三单价仅TOKEN_METERED全非null且≤10^12，SUBSCRIPTION全null且per_attempt_money_bound必须为有依据U；TOKEN模式该字段null，按公式推预留。日期严格YYYY-MM-DD，URL仅官方无秘密HTTPS；revision_ref为配置内价格资源标识，持久price_revision由configuration签发，不能自报快照版本 |
| Quota | subscription_ref:ID, unit:SUBSCRIPTION_REQUEST, window_limit:1..10^12, per_attempt_bound:1..10^6, consumed_before_test:U, evidence_ref:ID。仅SUBSCRIPTION必非null，TOKEN必null；consumed≤window_limit，剩余必须覆盖下一attempt预留，不能假定一逻辑请求一定只耗一套餐单位；独立于本地16attempt。没有网关／模型折算上界则不得发送 |
| Profile | profile_id:ID, account_id:ID, model_id:ID, wire_protocol:OPENAI_CHAT_COMPLETIONS, capability:GENERATION, max_attempts:1, attempt_timeout_ms:1..30000（推荐30000）, max_input_units:1..1048576, max_output_units:1..2048, max_items:2, dimensions:null, space_id:null, media_tasks:[], generation_ref:ID, billing_mode:E(TOKEN_METERED,SUBSCRIPTION)。删除旧模拟input_price_atoms/output_price_atoms，价只在Account.price维护；max_input_units与generation.reservation_input_bound一致（V2 unit=token liability），max_output_units与max_tokens一致；完整≤2048。generation_ref指该generation资源，不是任意key |
| cognition.text_context | event_max_bytes:2048, member_limit:4, target_limit:2, related_limit:2, related_record_max_bytes:4096, user_max_bytes:32768, system_max_bytes:4096, persona_projection_max_bytes:2048, manifest_max_bytes:8192, leaf_max_bytes:8192, leaf_text_max_bytes:7168, leaf_limit:8, total_max_bytes:73728, work_timeout_ms:180000。本文首包均为精确支持值，完整≤2048；沿H1/T2/R1且TARGET非空；全部下层等待取原总期剩余 |
| cognition.text_output | schema_version:1, action:CREATE_MEMORY, item_limit:8, raw_output_max_bytes:6144, canonical_output_max_bytes:6144, body_max_bytes:1024, belief_reason_max_bytes:256, subject_limit:4, target_anchor_limit:2, auxiliary_limit:2, basis_limit:2, manifest_max_bytes:4096, item_max_bytes:8192, total_max_bytes:73728。均精确支持值，完整≤2048；不能通过改值放宽产品动作／正式对象界 |
| self_model.initial_persona | generation_limit:3, retry_policy:EXPLICIT_KNOWN_TERMINAL, input_max_bytes:2048, input_record_max_bytes:8192, run_max_bytes:4096, candidate_max_bytes:4096, publication_max_bytes:4096, text_max_bytes:1024, projection_max_bytes:2048, prompt_ref:ID, prompt_digest:D, schema_ref:ID, schema_digest:D, transform_ref:ID, transform_digest:D, generation_goal:Text(1024), supervision_prompt:Text(1024)。精确数量如列，文本和六资源标识由可信用户供给；完整≤4096且说明＋模板＋目标＋监管prompt共同SYSTEM≤4096；重试不重置Account/window |

每个新增定义及其值的完整条目≤8192，原accounts/profiles/role_profiles分别≤8192；新增真实值不沿用原SIMULATED三组。金额、quota及窗口累计按§3.3唯一取整／63位规则预检，数据字段max并不保证乘法仍合法；TOKEN_METERED的R必须包含分项取整余量。本用户Coding Plan准备优先对应待供证据的SUBSCRIPTION分支，TOKEN_METERED保留为封闭备选，不能据算术示例替用户选择按量计费。配置纯解析可验证结构但不能确认账号服务资格，可信资源装配另核验上述官方证据／凭据引用；任何缺失都不进入发送就绪。实际本地JSON只作为外部准备材料，由可信适配输入统一配置；运行时不能绕过配置模块直接读这两份文件。

### 7.2 静态容量推导及实施后的实物门槛

下列记录批准时按完整声明给出的保守上界／准入界，不代表实物验证通过；实际构造结果与阻塞见[CURRENT_TASK](../work/CURRENT_TASK.md)。原实测基线仅用于**未改变声明的固定字节**，不把真实协议／新模型能返回的最大合法样本视为已验证。body完整界在编码前后都要校验；过界是拒绝，不削slot、删Schema或截断正文。

| 层 | 静态账目与未执行证明 |
| --- | --- |
| SYSTEM／USER及HTTP | 原事件4×(2048＋1024头)＋相关2×4096＋persona2048＋封套4096＝26624≤32768；Chat内USER JSON文本转义≤2倍、SYSTEM≤6倍、response_format schema12288、其他4096，`2×32768＋6×4096＋12288＋4096＝106496≤131072`。请求无Responses字段；真实token不由106496推算 |
| context／叶 | 8×完整8192＋manifest8192＝73728；每叶text7168＋完整头封套1024≤8192，实际头必须证明≤1024，超限整条拒绝。原context可重建语义≤8×7168＝57344；stage命令保守6×73728＋65536＝507904≤1048576 |
| 模型交接及点读 | 原content≤6144、规范化output≤6144、完整成功交接≤8192；payload再次装在存储点读时6×8192＋8192＝57344≤65536。usage≤4096、原usage≤2048均完整界，不能删除未知责任字段凑界 |
| Provider最大命令 | 原changes最多16，每项body8192＋payload8192（保守允许同时极值），事件2048，身份／操作／审计意图预留16384；`16×(6×8192＋6×8192＋1024)＋6×2048＋16384＝1617920`超过1MiB。**修订声明：新真实Provider变化项payload仅handoffs分支可非null且每命令至多1份**，其他15项固定null；同16项完整body界保留，得`16×(6×8192＋1024)＋6×8192＋6×2048＋16384＝880640≤1048576`。需新封闭判别验证器逐项检查，不把违规完整包交存储再碰运气；不删原必要变化／slot |
| 候选／发布命令 | candidate manifest4096＋8叶8192＝69632，stage至多6×69632＋65536＝483328；首次发布内派生3记录各4096＋封套65536，保守139264。输入body与result是不同载体，不能相加当成单次HTTP限额 |
| 新命令结果／回执／证据 | NewResult完整8192、facts每owner2048／至多2owner、targets最多8；统一整结果界阻止最大引用组合超载。回执业务结果编码至多6×8192＋身份封套8192＝57344≤65536；结果绑定证据≤32768。旧终结结果／回执沿既有界，不把新result8192强加所有旧命令 |
| 审计 | 新slot每项完整≤8192（沿批准事件硬限），至多2新slot／操作；既有最多16slot保持。NewFact≤2048＋8×Target(每项≤256)＋安全审计封套2048＝6144≤8192；若编码再转义放大，必须核完整事件，不能把6144当编码实测。Provider沿原1slot，仅加有界配置／quota事实；现有终结五／四slot及审计历史另照原界 |
| 配置条目body | 新文本组合完整118项定义和值实际编码之和≤524288，每项≤8192，六域及原字段限制保持。撤销100／13／5固定分组配额；完整总量准入，不缩写元信息、删字段或压短目录制造通过；原100项样本及完整118项须实际编码并核验 |
| 配置初始化 | 唯一initialize_text_learning完整冻结载体≤2097152，包含命令定义、全部值和必要审计意图。条件推导`3×524288＋118×256＋6×8192＋65536＝1717760≤2097152`，须重新证明转义系数及全部封套并构造实际载体。该例外由可信新装配和已注册原生命令绑定，纳入静态声明、签名、打开比较及原键恢复；预检、指纹、执行、确认和恢复共用同一规则。调用参数、命令名称或普通端口不能授予扩容。旧组合字节／指纹／上限保持，普通命令仍受storage.command_max_bytes及1MiB约束；回执65536、审计／结果界不变。初始化仍同事务一次发布；静态装配及载体预算不扩，不增命令／表／键 |
| DDL与仓储 | 6表＋14辅助索引＝20新DDL；原30554固定仓储字节＋20×2048DDL文本界＋新增每表8个固定语句声明×6×1024＝120666≤131072。DDL与语句名均ASCII；表每个只允许get/insert/CAS/recovery_page及最多4个唯一点读／叶释放语句，不允许任意SQL。实际必要语句超该预算必须重新审查；每表至多3辅助索引保持 |
| 命令描述 | 原固定描述803445字节作为不删减保守基数，13个替换及10新增最多各73728（按每命令输入／结果／意图≤192个Schema节点×256、最多5slot每slot≤4096绑定声明＋命令封套4096＝73728；Provider输入以bounded body而非在descriptor无限嵌套业务JSON）。保守`803445＋23×73728＝2499189≤2621440`。包含旧替换声明被重复计入的安全余量；节点及实际slot绑定必须由未来实际声明逐项核验，此处不是已经构造104条 |
| 完整静态装配 | 以上descriptor2425461＋repositories120666＋outer8192＝2554319≤2760704，余206385；静态载体3145728不变。若任一声明超过对应推导界则该总界不成立，须停下提交差额，不能只引用总余量直接扩限 |
| 运行资源 | 并发1／排队0，至多131072请求＋262144网络体＋8192交接＋73728context＋73728候选，至多3份同时载荷副本≤2MiB（只计载荷，非RSS）；深度≤12／节点≤20000的JSON解析及真实线程／连接RSS须未来实测。原F目录20GiB候选、12GiB停止、4GiB可用下限不变 |

新DDL固定语句闭集：每表get/insert/recovery_page；初始输入及publication不设CAS（不可变），各增加按self/run或instance唯一点读；run设CAS及by_instance；candidate设CAS(review only)、by_run_generation、by_request；context设CAS(release only)、by_batch；leaf设by_context及delete_by_context。每表至多8是保守预算而非必须生成8条。SQLite主键／唯一索引含空值的语义须以candidate非null部分唯一约束实物验证；解析验证的数据库身份不能被SQL外键替代。

此前Provider双payload上界冲突已在**已批准新格式**中用仅handoff可带payload的精确分支收敛；旧Provider命令不修改。本轮材料／媒体关系及取整修订不增键、表、命令、cost_items或字段；整数仍最多19位，118条／6域、104命令及descriptor／DDL声明预算不变，但13个变更项的完整定义大小必须重新实物证明。这些推导不替代编码证明；必须对104真实命令、32张阶段新增表、所有必要索引／结果／slot／回执／完整配置做实际构造、最大值编码、持久写入、读取比较及OPEN_EXISTING，不以模拟材料上限或上述分配预算代替实物资格。若13个变更项之外出现必要配置增量，先更新计数／定义和容量再批准，不隐式占用剩余10键。

## 8. 独立验收矩阵、环境与停止条件（已批准）

下列为独立验收要求，实际执行状态与证据见[CURRENT_TASK](../work/CURRENT_TASK.md)。每类证据单列来源，DETERMINISTIC_PROTOCOL表示受控协议响应，ACTUAL_STORAGE表示真实SQLite／文件，ACTUAL_PROVIDER仅给实际供应商请求，USER_REVIEWED只给用户完成的审核；SYNTHETIC材料可用于受控真实API试验，但不能称来自真实日常对话。历史73组相关性只证明本地词法范围，不作为生成学习质量成绩。

| 独立验收项 | 必须断言／证据 |
| --- | --- |
| 确定性协议与传输 | 回环HTTP/TLS受控端：完整结构输出、拒绝、length、未知choice／字段、重复JSON键、usage部分／缺失／矛盾、缓存／reasoning不重复计量、长头／body超限、重定向／TLS错误、断链、慢读、deadline与取消；逐次attempt登记／发送数一致，无隐藏重试，密钥不出日志／错误 |
| OUTPUT_LIMIT持久终结 | 完整length在新格式记录固定错误组合、request TERMINAL/FAILED、已发送attempt COMPLETED/FAILED，无成功handoff、无半份JSON、无自动重试；usage完整／缺失／矛盾及未决责任分别核对，persona保留KNOWN_FAILED/OUTPUT_LIMIT，普通学习按FAILED_DROPPED终结原批次；新进程恢复不发送。公开断言见[终态回归](../../tests/text_learning/test_persona_terminal.py)、[普通学习](../../tests/text_learning/test_output_limit_learning.py)及[新进程截点](../../tests/text_learning/test_persona_terminal_recovery.py) |
| 迟到最终错误 | 确定性原worker屏障：先确认UNKNOWN及TIMEOUT首错已持久，再允许完整length；terminal_error与first_error并存，persona原因准确，usage完整／缺失、实际完成K／原键确认、相同／冲突证据分别核验。迟到结算COMMIT前后新解释器恢复零发送；成功、其他已知失败、未知、NOT_SENT及零attempt字段组合严格校验。公开断言见[迟到终态回归](../../tests/text_learning/test_late_terminal_error.py)与[迟到跨进程恢复](../../tests/text_learning/test_late_terminal_recovery.py) |
| persona零revision与未登记NOT_SENT | 已登记实际revision=0／正数及错误revision拒绝；未登记原生REGISTRATION_ABSENT、完整原键／run／generation、同事务candidate＋run＋必要审计及真实self_model K；原键重放、最多三代显式重试和OPEN_EXISTING。伪造0／证明、仅NotFound、错键／跨代／撤权／迟到旧执行者拒绝；回滚、COMMIT前后、未确认提交及清理未结束保持状态／审计／回执／隔离一致。NONE仅用于无已知原因的未登记NOT_SENT，其他失败不放宽；恢复历史候选不能误用下一代PREPARED状态。证据导航同[终态回归](../../tests/text_learning/test_persona_terminal.py)及[新进程截点](../../tests/text_learning/test_persona_terminal_recovery.py) |
| 新旧配置与材料兼容 | 新完整118项经解析／适用性复检／保存／新进程load逐项相等；旧content/information的49152材料与MEDIA配置保持合法，旧组合拒73728、processing=0或缺MEDIA。新组合拒旧范围／单位／validator／base64声明、正处理并发、MEDIA或模拟补位profile；无媒体文本验证闭合格式的可达完整样本、清单／叶全部封套上界及越界拒绝；完整清单＋叶仍≤73728，不要求合法样本恰达到该硬界，不新增填充字段。SYSTEM＋USER不超过49152，独立token责任与线上字节检查保持。即使处理为0，空／重叠／错绑定目录、文件槽不足、块超blob、计时／GC／实际在途资源错误仍拒绝；验证不启动媒体理解worker、两生成角色共用一个实际槽，错误不留下部分配置或绑定 |
| 缓存费用取整与责任 | §3.3指定128000/1/127999反例直接断言输入分项30720＋1、R输入30721；K=0、1、P−1、P，P=0/1/2/I，费率相等／缓存更高／更低／任一0，以及整除／非整除、输出独立取整均验证S≤R。费用限额恰R可预留、R−1拒绝；UNKNOWN／提交未决保留责任，确认只结算一次。63位最大合法值、乘积超界但商可容纳、加δ或窗口累加超界、ceil加M−1会溢出的边界分别断言；真实usage超I/O不伪结算或释放。上述为确定性协议／账本回归要求，是否通过须以对应版本实测为准，示例费率不绑定方舟价格 |
| Auto身份及费用证据 | 固定请求别名、不同响应model、响应仍是别名且resolved=null、可核验后端与不可核验后端分列；Auto已配置不替代资格／strict证据。允许集合／计价维度外漂移、任一允许路由责任无上界则不继续发送；不挑便宜后端预留，不修改历史请求身份，不为核验发探测请求 |
| 真实临时SQLite与文件 | 从CREATE_NEW持久完整配置／候选／来源／审计／回执，实际新组合全命令／DDL／结果绑定；注册表与格式故意错配拒绝；分别记录最大合法载体和超限拒绝，不能只跑纯分词／纯JSON夹具 |
| 新进程恢复 | §6每截点前后真实进程终止；原请求确认、handoff已存候选未存、stage／finalize回执丢失、Provider UNKNOWN、费用缺失、旧worker实际在途、原版本缺失；重新打开零模型调用，实际请求／候选／费用／正式对象／审计各一次 |
| 模式、关闭、权限 | 普通与持久DREAM_PREPARING／DREAM_FOCUSED，恢复最后空页／close双向顺序，发送许可撤销，交付前撤权，绑定失败纠正重试，借入资源不误关；初始化结果、scheduler启动次数、原回执、实际占用和重复close直接断言 |
| 受控初始主体 | 最多6项、跨实例／重复／SELF／修改／额外登记拒绝，普通端口／伪造管理能力拒绝；主体、必要审计及回执各故障点整体回滚；原键在persona准备后与新进程只确认一次。完整输入／结果／审计实物容量及104命令装配验证 |
| 首次persona | 无预设／有外部设定两类；唯一SELF及真实管理来源、真实原请求／用户确认／两owner发布、缺persona暂停、旧TestPersona不能通过、重开不生成、用户拒绝或未审不发布、普通学习不更新persona |
| 文本业务原子性 | 0／1／8条与9条拒绝，辅助单独成记忆拒绝，假目标锚点／错UTF-8／越权主体／世界／旧revision拒绝；每owner及每必要审计失败都整体无部分成功；真实目标／后到输入／共享source／失败历史／明确敏感三终态分别断言 |
| 本地查询和索引恢复 | 真实生成后正式对象可查；dirty与active一致语义、双代次写删／发布竞争、原票据／反馈与旧读者；重开后persona和来源保持原绑定，查询零Provider调用；无同义保证，不用模型复答替本地查询 |
| 受控真实供应商 | 先批准配置、账号／模型、出站及材料清单，再执行固定请求包；记录真实model、response/request关联、usage／估算费用／未知责任、拒绝与协议适用性。两平台各1次首次persona、6次独立文本批次（共14）；余2次仅供明确批准的persona有界重试或失败诊断，共享16次总量，无自动补跑 |
| 用户学习质量 | 每平台6批至少覆盖事实／事件区别、否定／不确定、辅助指代、人物与世界、零记忆、输入内恶意指令。冻结后先由用户审核值得记住的命题及目标锚点，再审所有生成结果与遗漏、无依据内容、分类／分数和persona；保留失败／拒绝样本，不选择较好平台。报告命题precision／recall及分母、空目标集、源锚点正确率、世界／人物错误数；本阶段质量门槛precision≥0.90、recall≥0.80、锚点正确率1.0且越权0已获用户批准，少样本不承诺泛化 |
| 类型及工程回归 | 默认定点测试与受影响关联回归，平台相关用例双平台验证；仅影响广且定点不足或用户明确要求时全量unittest，并记录原因。代码稳定后统一一次按[锁定Pyright要求](../CODING_STANDARDS.md#python-type-checking)覆盖源码／测试／新增文件，全量0错误；编译、相关JS、锁与链接／全部差异检查。Pylance独立记录；新集合不得复用为已运行旧全量 |

人工质量按预先确认的原子命题集计分：precision分母为全部输出命题（重复输出逐项占分母，不能作为新增命中），recall分母为人工认为应学习的唯一目标命题；匹配关系须人工确认，不能由模型或已返回对象反推相关集。空人工集的recall为不适用，另报是否零输出及误生成数；没有输出时precision为不适用，不能记1。拒绝／格式失败／未决分别计批次覆盖，不从报告中删除；两平台分别展示原结果和审核，不移植数据库ID或挑较好结果。来源锚点正确率的分母为全部输出直接命题，纯语法锚点检查与人工支持性审阅分列。

资源候选使用现有macOS arm64自有目录与Linux arm64非root真实ext4持久卷，Linux 2CPU／4GiB、无swap、128pid、源／根只读；禁止tmpfs替代实库恢复。历史可用环境为Python3.12.14、macOS SQLite3.53.1／Linux3.53.4及Pyright1.1.413，镜像`sha256:e1196578279b9386460f3873e3f8cef90a22a84dbbc20e5ecd91d0ca88b9cf4f`；只是准备参照，执行前复核实际平台／TLS／CA／镜像／依赖与权限，不能把历史版本当当前事实。标准库实现无需新增SDK或GPU；真实阶段需由用户提供安全凭据注入和受控官方HTTPS出站，原无外网Linux剖面不能直接声称可调用。生产认证、amd64性能、掉电、P/X及24小时试验不在该矩阵内。

通用自动停止新请求／造数条件：16次已登记attempt、已批准的实际金额／订阅额度上限（已知小计＋held责任）、任一UNKNOWN远程结果／未决费用、任一认证／权限／模型／计费维度错配、持久故障、审计缺失、费用预留超额或预算计量不完整；任一先到即停止。普通已知失败保存原批次终态，不自动替换样本；格式／容量错误先完成本地分析，不能用真实反复请求调试Schema。目录累计12GiB、可用≤4GiB、持久操作20000或资源监控失联也停新增负载；保留在途确认和关闭空间，不能取消后假释放。真实受控试验先离线协议通过，跨平台费用共享总包上限，由父级授权清单防止两库各消费16次。

本次MiniMax试验按§3.1用户批准的usage-only／¥0统计口径替代金额与套餐额度审批门槛；不改变未知远程结果、资源、完整审计和请求上限停止条件，也不把未知账本事实伪结清。未完成相应原生装配前，不允许绕过Provider预算或自行释放held。

每次记录完整argv、退出码、原始脱敏输出、断言位置、平台／镜像、资源量、请求数与费用覆盖；模型正文证据只进受控审核产物，不进普通诊断或秘密导出。按排序“路径＋NUL＋文件SHA256＋LF”对受测代码／测试／资源／工程文件生成清单和聚合SHA，执行后核对未变；文档另记摘要。人工质量未审、真实供应商未调用、最大合法词项资格和Pylance分别保持缺口，任何一项不能由模拟或静态核算替代。

## 9. 实施前置与监督技术审查

本文批准一个带首次persona依赖的有限文本学习整体。监督重点检查真实／模拟格式隔离、原请求与版本身份、refusal不误清历史、未知预算责任、完整上下文恢复、每条真实目标锚点、首次发布及学习终结同事务、配置与静态装配完整容量。

MiniMax独立协议及直接首次persona验证已获明确批准，执行不再要求strict证明；供应商选择、材料、计量口径及非SELF登记已按§3.1／§6.3获用户批准，persona发布与最终结果审核仍分别保留。资料缺口只暂停真实发送，不暂停独立实现与本地验证。所有最长字段实际编码、持久化及恢复须按§8证明；不得把批准后的静态推导称为运行结果。若发现真实契约冲突、容量无法满足或必须扩大产品／权限边界，列出双方位置、影响和推荐决定，停止受影响部分，继续独立工作。

本文的固定试验协议用于解释对应请求、审核及计量记录，不是可重复使用的调用授权。各包实际状态与已交付版本见[STATUS](../work/STATUS.md)，质量结果见[暂缓记录](../work/DEFERRED_ISSUES.md#text-learning-quality)；新增执行、调用与提交授权统一见[CURRENT_TASK](../work/CURRENT_TASK.md)。
