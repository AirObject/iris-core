# 异步embedding及语义检索闭环

**状态：核心技术方案、参数、完整实施、自查、定点及关联回归和范围内修复已获授权；真实小包调用已获用户批准，800GiB方案不获批准。** 核心推荐及明确选择在本授权范围生效，未采纳替代不生效；正文“拟／建议”表示技术设计措辞，不再要求重复批准核心实现。用户2026-09-15确认用量已分配并批准原18次真实请求；本次仅记录用量的适配及发送条件见§7.2，费用与账户截图不再阻塞本包。具体材料由用户委托监督者审查，不能由执行者自签审核。真实调用范围及前置条件唯一见§7，大档仅保留封闭定义与未验证边界，不启动完整资格或资源扩容。技术批准不等于实现完成、真实效果或容量资格通过。[STATUS](../work/STATUS.md)记录本阶段交付，[CURRENT_TASK](../work/CURRENT_TASK.md)记录实际进度；[质量暂缓](../work/DEFERRED_ISSUES.md#text-learning-quality)和[统一验证口径](../CODING_STANDARDS.md#validation-environment)继续生效。

## 1. 能力范围与独立验收对象

拟贯通：独立Provider实际embedding与计量 → 正式记忆提交后的持久工作及向量产物 → 索引发布与新进程恢复 → 查询向量缓存及词法／语义融合 → 截止时间内降级、权限与修订核验 → 只读运行观察。使用人工确认的固定正式记忆集、来源和相关性标签，独立验证此链路；不以生成式学习质量作为输入资格或验收前置。

正式记忆提交不等待供应商，索引失败不撤销记忆。已付费向量属于持久产物，检索快照属于可重建索引；二者有不同的恢复与回收责任。本文不纳入rerank、媒体理解、稀疏向量、梦境、完整热配置、旧库迁移、时区工程及生产性能保证；文本送入名称含vision的模型不等于纳入媒体能力。现有明确偏移接口、UTC持久时间及语义字段保持。

依据：[检索模块](../modules/retrieval.md)、[Provider模块](../modules/provider.md)、[配置](configuration.md)、[本地信息闭环](local-information-feedback.md)、[请求路径](request-paths.md)、[成本与上下文](context-and-cost.md)、[持久化](persistence-and-transactions.md)。这些已批准边界继续约束新方案；本文已批准的存储格式、核心配置及能力增量在新装配内落实；未批准资源、协议替代和产品扩展不因此生效。

## 2. 当前实现可复用端口与必要增量

实施规划基线：main／`9936385d8697b7add662562c3465c041a48d76fa`，上阶段72份产物已提交，语义检索草案未混入该提交。原端口核对基于其父提交`c2be9327b80be3b3ccb12262af7ff15c4539ea2d`及当时工作区；该历史提交树的578份受测文件指纹为`16b7d88d466e9549d800604140a9d9de353b2534563628492a331b1139de0582`，不代表当前工作区受测快照。当前实施状态及有效指纹见CURRENT_TASK；表中“拟增”不是现有API保证。

| 现有实际入口／owner | 已有能力 | 拟增或必须保留的限制 |
| --- | --- | --- |
| [Provider端口](../../companion_memory/provider/ports.py) `WorkPort.embed`、`lookup_request`、`get_request`、`verify_unsent` | 独立请求键、登记确认、账本和受限恢复接口 | 当前真实传输只接生成；embed存在不等于真实向量已接通。缺登记不单独授权重发，UNKNOWN不重新派发 |
| 同文件 `ResultOwnerPort.recover_result`、`verify_terminal`、`confirm_completion` | 结果交接、原请求确认、消费者结束 | 部分真实终态确认当前绑定文本能力；拟补独立embedding结果版本和同事务接收证明，不能直接复用文本身份 |
| [Provider装配](../../companion_memory/provider/service.py) `bind_work`；[规范化](../../companion_memory/provider/normalization.py) | 模拟embedding的精确payload及结果结构 | 当前真实文本装配只接受GENERATION与LEARNING/PERSONA；拟新增EMBEDDING角色／资源装配，不借用文本任务身份 |
| [内容公开运行入口](../../companion_memory/runtime/content_service.py) `execute`／`confirm_command`；[记忆事务](../../companion_memory/memory/transactions.py) `apply_change_set` | 正式对象、来源、回执、审计原子提交 | 增加同事务semantic dirty事实；后续网络不在UoW中。固定记忆集仍经可信候选及公开终结，不直接写库 |
| [变更跟踪](../../companion_memory/memory/information_tracking.py) `changed`、`current_page`、`coverage_view` | 变更序列、当前修订、词法gap与发布进度 | 复用变更序列；新增独立语义覆盖／ack，不能让语义发布清掉词法gap或改变既有root修订算式 |
| [本地索引](../../companion_memory/retrieval/index.py) `LocalIndex`；[索引工作器](../../companion_memory/information/index_worker.py) `LocalIndexWorker.run` | 持久generation、分页检查点、发布、读者与退休收尾 | 当前为词法；拟新增语义产物、空间及发布指针，保留旧LOCAL_LEXICAL_V1数据和原键恢复 |
| [查询服务](../../companion_memory/retrieval/query_service.py) `search_memory`／`deep_recall`／`prepare_reply`／`resolve_recall` | 1000ms总期、权限过滤、最多128候选和8项交付、回执 | [当前格式](../../companion_memory/retrieval/query_formats.py)只接受LOCAL_LEXICAL_V1、rerank=false；拟增明确版本。现有文本候选要求词法重叠，不能把此条件继续套到语义召回候选 |
| [交付核验](../../companion_memory/retrieval/delivery.py) | 编码及交付前修订、授权与模式核验 | 融合分数不替代最终owner读取和交付屏障；semantic命中仍经同一安全出口 |
| [信息宿主](../../companion_memory/runtime/information_host.py)、[文本宿主](../../companion_memory/runtime/text_host.py) | 生命周期、查询绑定、局部工作器 | 拟增受宿主管理的有限embedding工作器及语义发布／恢复，不另起脱离宿主的worker来绕过超时 |
| [信息观察](../../companion_memory/information/observation.py) `InformationObserver.read`；Provider `ObserverPort` | 受限只读HTTP、请求／usage／预算观察 | 拟增加有界语义状态，不返回向量、查询原文、完整来源或审计正文 |
| [文本配置持久化](../../companion_memory/configuration/text_persistence.py) | 独立静态组合、完整配置身份、原子初始化与恢复 | 拟增独立semantic组合；准备文件不是运行配置，旧组合格式／指纹不改写 |

## 3. 供应商事实、协议候选与计费缺口

<a id="supplier-binding"></a>

### 3.1 本地非秘密投影及公开证据

2026-09-14只读`.local/provider-tests/embedding.json`的已知白名单前缀，得到`format=local_provider_test_preparation`、`format_version=1`、`enabled=true`、`capability=EMBEDDING`、`wire_protocol=OPENAI_EMBEDDINGS`、`provider_name=volc_embedding`、`base_url=https://ark.cn-beijing.volces.com`、`endpoint_path=/api/coding/v3`、`model=doubao-embedding-vision`。读取在credential字段值之前停止，未读取key、未输出完整文件、未给准备文件计算指纹。后续字段未检查，不将未检查写成缺失。enabled只是准备声明，不是发送许可。

| 项目 | 本轮可确认的公开事实 | 不能据此推定的部分 |
| --- | --- | --- |
| Coding Plan绑定 | [火山引擎官方团队说明](https://developer.volcengine.com/articles/7628812787703087110)给出上述模型及`https://ark.cn-beijing.volces.com/api/coding/v3`基础路径，展示工具接入 | 准备文件只到前缀；模型别名背后的固定版本、账户许可、实际计费、每个操作的能力未由本地文件证明 |
| 普通向量客户端形状 | [官方SDK资源](https://raw.githubusercontent.com/volcengine/volcengine-python-sdk/3b116781c5b8bb648f215b29587f2ec74d9e5d02/volcenginesdkarkruntime/resources/embeddings.py)发`POST /embeddings`，input为字符串或字符串列表，支持dimensions及encoding_format参数 | SDK形状不能证明Coding模型支持数组批量、具体最大批量、输入上限或指定维度 |
| 普通响应 | [官方SDK响应类型](https://raw.githubusercontent.com/volcengine/volcengine-python-sdk/3b116781c5b8bb648f215b29587f2ec74d9e5d02/volcenginesdkarkruntime/types/create_embedding_response.py)为id/created/model/object/data/usage，data为数组；[元素类型](https://raw.githubusercontent.com/volcengine/volcengine-python-sdk/3b116781c5b8bb648f215b29587f2ec74d9e5d02/volcenginesdkarkruntime/types/embedding.py)含index/embedding/object；usage含prompt_tokens及total_tokens | 尚未取得此账户与路由的实际响应；缓存、账户扣额或其他扩展不能猜值 |
| 多模态操作 | [官方SDK资源](https://raw.githubusercontent.com/volcengine/volcengine-python-sdk/3b116781c5b8bb648f215b29587f2ec74d9e5d02/volcenginesdkarkruntime/resources/multimodal_embeddings.py)另发`/embeddings/multimodal`及typed input；[响应](https://raw.githubusercontent.com/volcengine/volcengine-python-sdk/3b116781c5b8bb648f215b29587f2ec74d9e5d02/volcenginesdkarkruntime/types/multimodal_embedding/embedding_response.py)的data是单对象 | input数组不能机械解释成每项各一向量，普通及多模态解析不可合并放宽 |
| 维度和批量 | 官方团队集成示例包含1024维；[官方OpenViking配置](https://github.com/volcengine/OpenViking/blob/main/docs/zh/guides/01-configuration.md)存在维度／输入／batch配置示例 | 工具示例不是供应商上限证明。本文不把示例batch_size=32或其他产品的2048维作为当前路由契约 |
| 价格 | [官方豆包产品页](https://www.volcengine.com/product/doubao)公开文本向量单价0.7元／百万tokens，图片1.8元／百万tokens，本轮日期核对 | 通用按量价不等于Coding订阅账户账单，不能宣称本账户每次收费0.7费率或¥0；图片不纳入本包 |

普通[API文档](https://www.volcengine.com/docs/82379/1521766)、[多模态API](https://www.volcengine.com/docs/82379/1523520)、[模型说明](https://www.volcengine.com/docs/82379/1409291)与[Coding指南](https://www.volcengine.com/docs/82379/2279748)须区分资源及协议。既有采集受到重定向／脚本页面限制；现已取得的Coding说明和普通多模态文档仍不证明Coding具体操作及账户账单。当前用户直接验证授权、计量分支及准入唯一见§7.2，不再把缺费率或书面操作证明当作发送阻塞。SDK引用固定如下，不引入SDK依赖，不把客户端代码当服务端承诺。


### 3.1.1 SDK依据冻结

本轮仅下载公开客户端源码用于核对／摘要，未导入或执行SDK。固定官方仓库提交`3b116781c5b8bb648f215b29587f2ec74d9e5d02`；以下SHA256按下载的原始文件bytes计算，不含任何本地准备文件或凭据。路径以volcenginesdkarkruntime/为前缀，表中链接固定提交，不再依赖master漂移。

| 文件 | 完整内容SHA256 | 字节数 |
| --- | --- | ---: |
| [resources/embeddings.py](https://raw.githubusercontent.com/volcengine/volcengine-python-sdk/3b116781c5b8bb648f215b29587f2ec74d9e5d02/volcenginesdkarkruntime/resources/embeddings.py) | `e329090aab2037acadde43fe9fb40ab1bcbe4a7d13e90e2f396e19948f88c31b` | 4350 |
| [types/create_embedding_response.py](https://raw.githubusercontent.com/volcengine/volcengine-python-sdk/3b116781c5b8bb648f215b29587f2ec74d9e5d02/volcenginesdkarkruntime/types/create_embedding_response.py) | `4a45cbb6d0f53880a3135f8452507cb9a64ea10c58f10c4ddf669fca19d9a983` | 1314 |
| [types/embedding.py](https://raw.githubusercontent.com/volcengine/volcengine-python-sdk/3b116781c5b8bb648f215b29587f2ec74d9e5d02/volcenginesdkarkruntime/types/embedding.py) | `8e8f0d231af6ddf34687fcd038728be0161453bb59fbaeb4eb332502184586da` | 919 |
| [resources/multimodal_embeddings.py](https://raw.githubusercontent.com/volcengine/volcengine-python-sdk/3b116781c5b8bb648f215b29587f2ec74d9e5d02/volcenginesdkarkruntime/resources/multimodal_embeddings.py) | `1545ddf0472c0a0b62b219d9e36e66f0bc49bfde2754ee8faef823f5a216f8ca` | 4883 |
| [types/multimodal_embedding/embedding_response.py](https://raw.githubusercontent.com/volcengine/volcengine-python-sdk/3b116781c5b8bb648f215b29587f2ec74d9e5d02/volcenginesdkarkruntime/types/multimodal_embedding/embedding_response.py) | `59c16ca03253dee1152caaa42ca844da23d5e7ef488ad97582273687040d4074` | 1193 |
| [types/multimodal_embedding/embedding_usage.py](https://raw.githubusercontent.com/volcengine/volcengine-python-sdk/3b116781c5b8bb648f215b29587f2ec74d9e5d02/volcenginesdkarkruntime/types/multimodal_embedding/embedding_usage.py) | `cbfd6aabb2ab0a9bf3755b011c61f4a2a700c14462e93aa369186e6154fba7de` | 1037 |

客户端input类型、POST相对路径及返回类型只构成形状依据；服务端是否支持本Coding资源、具体dimensions和批量、计费规则不能从这些文件推出。SDK更新不会自动改变本协议，新的资料差异须显式修订。

### 3.2 独立封闭协议（技术方案已批准，真实兼容性分别验证）

建议首个分支名`ARK_CODING_DENSE_TEXT_V1`，精确绑定`volc_embedding`、上述host、`/api/coding/v3/embeddings`、`doubao-embedding-vision`、1024维、每请求1个文本。此操作尚无真实成功证据；用户已按§7.2授权以首个正式DOCUMENT直接验证，不要求先取得操作逐字段书面确认。请求字节仍冻结；若实测不兼容，保留本次失败并停止新增发送，不在运行中尝试两个端点、typed-input、替换模型或退到普通`/api/v3`。

拟请求根字段恰`model,input,encoding_format,dimensions`，input为长度1的字符串数组，encoding_format固定float，dimensions固定1024；若服务不接受dimensions，须修改冻结协议后重新审查，不能运行时删字段探测。无messages、tools、stream、response_format、任意extra_body或HTTP自动重试。DOCUMENT与QUERY只是原生用途身份；外部输入使用同一版本的确定性文本渲染，不添加未经模型官方支持的instruction前缀。

拟响应根恰`id,created,model,object,data,usage`，object=list；data长度1，元素恰`object,index,embedding`，object=embedding、index=0。字符串身份非空且限长，created为合法非负整数；model必须命中冻结的明确返回身份集合，不允许模糊前缀匹配。embedding恰1024个有限数值，排除bool、NaN、Infinity、重复索引、缺项、多项、零范数。完整JSON拒绝重复键、混杂正文、代码围栏及截断，不修JSON。有效数值由新适配器明确转为原生float，模拟分支原校验不改变。

TOKEN_METERED的usage只接受prompt_tokens及total_tokens精确非负整数，要求两者相等；未知字段或不一致不得补零结算。本次USAGE_ONLY_TRIAL分支的独立usage格式、观察不完整及继续条件由§7.2统一规定；不改变本段旧计量分支。若服务器提供缓存或细分字段，先取得官方定义，再增加版本化分类，不能把未知字段悄悄丢弃。响应语法／身份错误和usage是否完整分开保存；完整覆盖的已知错误按原终态结算，否则保留责任。保留首错、最终终态错误、远程状态、本地提交、清理五类事实。

原生payload继续恰`texts,purpose,dimensions`，用途DOCUMENT/QUERY、texts为长度1的不可变字符串序列；原生结果继续恰`vectors,dimensions,space_id,model_id,input_items`，只增加明确绑定的新结果容量与持久版本。当前模拟规范化把`len(texts)`作为input_units，不能直接用于真实token计费。拟增独立真实embedding计量：input_items=1与input_tokens分列，tokens来自供应商usage；TOKEN_METERED预留来自可证明的输入责任界，本次仅记录用量分支不做货币预留；费率按百万tokens分母有理数取整，不能把每token小数金额塞进旧模拟整数单价，也不能伪造SIMULATED_REPORTED。缺缓存细分保留null；已知总输入可以按覆盖全部输入的保守费率估算，不能声称已取得缓存账单。

空间ID拟由协议版本、端点部署身份、模型及可证实版本／显式部署epoch、维度、渲染版本、用途兼容约定和归一化算法摘要决定；凭据、账单和查询正文不进入ID或日志。DOCUMENT与QUERY在同一已确认兼容空间内，但请求键／缓存键包含各自用途。服务器不暴露稳定模型版本时，记录别名与“后端版本未披露”，由显式新epoch管理变更；不能保证检测供应商静默升级，此限制须用户接受。配置恢复身份相同不证明远端向量空间永远不变。

## 4. 持久结构、命令及恢复契约（已批准）

### 4.1 类型、数量与所有权

补齐后建议为**15张新增表、26条新增持久命令、1条初始化命令替换、6项新增配置（总124键）**。原12表缺少memory自己的持久应用证明及固定人工材料的根／成员，增加semantic_ack、fixed_memory_set、fixed_memory_member。原20条中mark/ack/publish改为跨owner事务参与方法，不单独注册，剩17条；新增4条固定材料命令、resume、generation_fail、record_cleanup，另加文件封存与Provider分片退休2条，共26条。显式预热复用prepare而不是暗增一种收费命令；初始化替换不计入26条。表数不是能力边界，不能为凑旧数量省略恢复证据。

下表为完整领域记录；不是SQL自由JSON。统一记法：`ID`为现有安全ASCII标识符，1–128字节；`H`为64位小写SHA256；`N`为0..2^63−1精确整数；`P`为1..2^63−1；`T`为原UTC微秒整数；`B`为精确bool；`S(n)`为严格UTF-8字节界；`?`表示必需但可null；`[X,a..b]`为有序有限数组，`{…}`为恰含所列字段的对象。无缺省字段、无额外键、无bool充int、无非有限浮点。每行有`v=1,revision:P`，不可变叶revision固定1；`row_id:ID`为确定性域分离摘要ID，复合唯一键仍另建，不用摘要替代唯一关系。以下字段不再隐含其他metadata；每个完整行≤8192字节，另列更小界时从严。

引用类型：`Config={database_id:ID,instance_id:ID,snapshot_id:ID}`；`Object={object_id:ID,revision:P}`；`Request={request_id:ID,attempt_id:ID?}`；`Receipt={kind:ID,key:ID,fingerprint:H}`；`Intent={package_id:ID,slot_id:ID,authorization_digest:H,request_digest:H,expires_at:T}`；`Error`为`NONE/INPUT_LIMIT/IDENTITY/PROTOCOL/USAGE/BUDGET/MODE/DEADLINE/STORAGE/AUDIT/RESOURCE/CANCELLED/KNOWN_PROVIDER_FAILURE`，原Provider详细首错／终错留在其原记录，不由工作表替换。外层slot单独持久且只消费一次；Intent不是调用者自签的许可。

| owner／表 | 除v、revision、row_id外的完整字段 | 键、引用及状态约束 |
| --- | --- | --- |
| memory.semantic_gap | space_id:ID, object_id:ID, object_revision:P, first_uncovered_seq:P, latest_change_seq:P, action:UPSERT/DELETE | UNIQUE(space_id,object_id)，gap.revision=object_revision；first≤latest；合并只改latest、revision及action，first不前移；保留删除修订，不能把不存在误作删除 |
| memory.semantic_ack | space_id:ID, object_id:ID, object_revision:P, resolved_from_seq:P, applied_seq:P, action:UPSERT/DELETE, material_digest:H?, artifact_id:ID?, evidence_receipt:Receipt | UNIQUE(space_id,object_id)；UPSERT必须有摘要及已接收DOCUMENT产物；DELETE两者null且核验原删除事实；证据引用本次apply回执，from≤applied；旧版本证明在原回执／审计，不被改写 |
| memory.semantic_publication | space_id:ID, config:Config, material_seq:N, published_seq:N, generation_id:ID?, published_at:T? | UNIQUE(space_id)；初值0/0/null/null；published≤material≤memory实际last_seq；generation/time同null；词法root／ack不改 |
| retrieval.embedding_work | 公共字段恰work_id:ID, config:Config, space_id:ID, kind:EMBED/DELETE_LOCAL, state:下述分支枚举, object_ref:Object?, change_seq:N?, superseded_by_revision:P?, error:Error, cleanup_pending:B, created_at:T, completion_ref:Receipt?；另恰含下述对应分支字段 | UNIQUE(work_id)；EMBED另有UNIQUE(config.instance_id,original_request_key)，DELETE_LOCAL不进入此请求键索引；完整封闭分支及终态要求见下文，不允许混合字段 |
| retrieval.embedding_input_leaf | work_id:ID, ordinal:0..2, byte_count:1..3072, digest:H, data_base64:S(4096) | UNIQUE(work_id,ordinal)，FK work；拼接严格等于input_bytes及material_digest对应的原渲染文本，除末叶均3072字节；不删除在途／UNKNOWN材料 |
| retrieval.embedding_artifact | artifact_id:ID, config:Config, space_id:ID, purpose:DOCUMENT/QUERY, partition_id:ID, material_digest:H, request_ref:Request, dimension:1024, vector_digest:H, vector_leaf_count:2, usage_ref:Receipt, completion_ref:Receipt, received_at:T | UNIQUE(artifact_id)、UNIQUE(request_ref.request_id)；request attempt非null；无“空向量成功”；摘要按2叶float64小端字节，已付费产物默认永久保留，当前不提供销毁命令 |
| retrieval.embedding_vector_leaf | artifact_id:ID, ordinal:0..1, byte_count:4096, digest:H, data_base64:S(5464) | UNIQUE(artifact_id,ordinal)，FK artifact；两个叶齐全且总摘要正确才可接收；只存原规范化float64，不用float32替代 |
| retrieval.semantic_generation | generation_id:ID, space_id:ID, config:Config, state:BUILDING/READY/PUBLISHED/RETIRING/RETIRED/FAILED, captured_seq:N, member_count:0..4096, page_count:0..512, confirmed_pages:0..512, file_name:ID, file_bytes:0..34344960, file_digest:H?, member_digest:H?, build_cursor:ID?, retired_cursor:ID?, error:Error, created_at:T, published_at:T? | UNIQUE(generation_id)、UNIQUE(space_id,file_name)；文件名由ID确定，只能相对自有目录；READY需全页确认、摘要和实际文件发布证明；PUBLISHED必须有time；RETIRING直到真实读者退出后才能清理 |
| retrieval.semantic_member | generation_id:ID, ordinal:0..4095, object_id:ID, object_revision:P, ack_revision:P, applied_seq:P, artifact_id:ID, vector_digest:H | UNIQUE(generation_id,ordinal)、UNIQUE(generation_id,object_id)，FK generation/artifact；仅UPSERT ack；按object_id字节顺序；这是冻结索引副本，不是权限依据 |
| retrieval.semantic_page | generation_id:ID, page_no:0..511, first_ordinal:0..4095, count:1..8, members_digest:H, state:STAGED/CONFIRMED, byte_offset:N, byte_count:1..67072 | UNIQUE(generation_id,page_no)，FK generation；first=8×page_no；除末页count=8；精确对应成员和文件记录区，确认不能跨页／跳洞 |
| retrieval.semantic_control | space_id:ID, config:Config, scheduler:PAUSED/ENABLED, pause_reason:NONE/USER/BUDGET/MODE/UNKNOWN/RESOURCE/INTEGRITY, current_generation:ID?, building_generation:ID?, retiring_generation:ID?, authorization_digest:H?, gc_cursor:ID?, last_cleanup_at:T?, operation_count:N | UNIQUE(space_id)；building和retiring不能同时非null，两代合计≤2；初始PAUSED/USER；operation_count只计retrieval自己的实际持久步骤，整包操作上限由原持久回执计数及统一准入监控核验；ENABLED不能带pause_reason；authorization_digest只有已激活包可用，不允许resume伪造批准 |
| retrieval.query_embedding_cache | cache_key:H, space_id:ID, partition_id:ID, material_digest:H, artifact_id:ID, state:ACTIVE/EXPIRED, expires_at:T, bound_at:T | UNIQUE(space_id,partition_id,cache_key)，FK QUERY artifact；key绑定实例、用途、渲染版本及原查询摘要；有效期不作为付费artifact删除许可；命中只读，不暗增更新时间命令 |
| provider.embedding_handoff_leaf | handoff_id:ID, request_id:ID, attempt_id:ID, ordinal:0..9, byte_count:1..4096, digest:H, data_base64:S(5464) | UNIQUE(handoff_id,ordinal)，引用Provider原交接根／原attempt；只有新embedding格式使用，根见下文；未获消费者完整接收证明不得删叶 |
| cognition.fixed_memory_set | set_id:ID, config:Config, state:OPEN/SEALED/ESTABLISHED, expected_members:12/4096, stored_members:0..4096, established_members:0..4096, manifest_digest:H, review_ref:ID, review_digest:H, reviewed_by:S(32), created_at:T | UNIQUE(set_id)；reviewed_by只能由受信任审核能力填实际审核者；本轮用户已委托监督者审查，必须区分用户授权与监督审核，不能冒签用户亲自标注或由调用者自报；数量必须等于§6.3.1固定档位；0≤established≤stored≤expected；SEALED需expected个叶且摘要一致，ESTABLISHED需expected个不同成员的原终结回执 |
| cognition.fixed_memory_member | set_id:ID, ordinal:0..4095, member_id:ID, event_json:S(2048), memory_json:S(4096), content_digest:H, state:STORED/ESTABLISHED, object_ref:Object?, source_id:ID?, establishment_ref:Receipt? | UNIQUE(set_id,ordinal)、UNIQUE(set_id,member_id)，FK set；ordinal<expected_members；event/memory用原领域封闭codec再校验，不接受任意业务对象；ESTABLISHED后三个引用齐全，正文不替换；完整行8192限制仍适用，字段极大值不保证可同时取到 |

**embedding_work的两个互斥封闭分支：** 公共字段均必填，分支字段不得互相出现。EMBED额外恰含`purpose:DOCUMENT/QUERY,partition_id:ID,material_digest:H,input_leaf_count:1..3,input_bytes:1..8192,original_request_key:ID,intent:Intent?,request_ref:Request?,artifact_id:ID?,deadline_at:T?`；state仅`PREPARED/BOUND/REMOTE_UNKNOWN/RESULT_STORED/APPLIED/SUPERSEDED/KNOWN_FAILED/NOT_SENT`。DOCUMENT的object_ref/change_seq均非null且seq≥1，QUERY两者null；RESULT_STORED和普通APPLIED始终要求完整artifact与原接收证明，登记后request_ref不清空。BOUND表示已有原Provider登记的工作观察，不是bind替Provider登记；UNKNOWN不因修订过期改为SUPERSEDED。APPLIED/SUPERSEDED/KNOWN_FAILED/NOT_SENT必须有completion_ref，RESULT_STORED须保留接收回执；无产物不得冒充普通APPLIED。

DELETE_LOCAL额外**仅**含`deletion_ref:Receipt`；object_ref/change_seq必须为确证删除的原对象／删除修订及seq≥1，state仅`LOCAL_PREPARED/LOCAL_APPLIED/LOCAL_SUPERSEDED/LOCAL_FAILED`。purpose、partition_id、material_digest、input_leaf_count、input_bytes、original_request_key、intent、request_ref、artifact_id、deadline_at全部是**禁止出现的字段**，不是允许填null的发送分支。无embedding_input_leaf、artifact或Provider关联；公共completion_ref只指本地命令回执。LOCAL_APPLIED/LOCAL_SUPERSEDED/LOCAL_FAILED均须原本地终态回执，初始error=NONE、cleanup_pending=false；后续pending只表示本地UoW／资源未收尾。输入叶FK额外要求work.kind=EMBED；artifact、缓存、bind、record_result及Provider三条交接命令均只接受EMBED原关联，调度候选读取也硬过滤kind=EMBED，不由空文本或空artifact推断种类。

Provider原handoff根只在新embedding格式增加封闭`embedding_payload={format_version:1,byte_count:1..40960,leaf_count:1..10,payload_digest:H}`，另加`embedding_cleanup={received_receipt:Receipt?,retired_through:N?,state:HELD/RELEASABLE/RETIRED}`；其原请求／owner／摘要／保留状态字段沿原根逐项保留；不内嵌所有叶。旧请求、attempt和handoff版本不改变，原PREPARED跨进程不获得额外“未发送”含义。新增表结构、固定审计及根扩展共同进入打开比较／装配签名，不迁移原试验库。

渲染资源TEXT_MEMORY_RENDER_V1也属于本候选封闭定义：DOCUMENT从当前已授权MEMORY取恰`body,world_scope,subject_ids,category,stance,occurred_range,applicable_range`，按此键序输出无多余空白、ensure_ascii=false的严格UTF-8 JSON，subject_ids按ID字节排序；原领域null／世界／时间结构原样保留。QUERY为原query_text UTF-8，不trim、不词法归一化，不加入测试标签或工具指令；两种用途绑定同一版本资源的明确分支。material_digest=SHA256这些完整发送文本bytes，输入叶只保存这些bytes。instance／space／artifact的域分离身份按固定前缀＋规范JSON输入的SHA256定义，空间身份材料仅使用§3.2所列字段；不得把原请求key与缓存key互换。DELETE_LOCAL不调用TEXT_MEMORY_RENDER_V1，不制造tombstone发送文本或空输入；prepare只从memory公开端口取得并冻结类型化`{object_ref,change_seq,deletion_ref}`本地删除证明。删除work_id由实例／space／DELETE_LOCAL／该证明域分离生成，命令原键仍存在于通用持久封套，绝不作为Provider请求键。

### 4.2 gap、应用证明和发布水位

memory仍是变更序列唯一owner。每个实际对象变更在原事务内取得seq；MEMORY可索引变更执行mark：无gap则first=latest=seq，有gap则保留first、latest=seq并替换为当前修订／动作。反馈导致revision增加但渲染摘要不变时，同事务也标gap，后续可复用原artifact，不能凭“只是反馈”跳过修订核验。非MEMORY对象的seq由原owner证明不属于语义索引集合，不制造向量工作。

apply按kind在同一UoW内核验gap最新revision/seq：EMBED/DOCUMENT要求当前对象、完整artifact及接收证明；DELETE_LOCAL要求原删除回执、当前删除修订及删除gap，禁止artifact。两分支均写semantic_ack后删除该精确gap。ack的resolved_from_seq取gap.first，applied_seq取latest，表示该对象在此区间的先前修订已被当前版本覆盖，**不表示区间中其他对象已覆盖**。没有gap的原同内容重放只返回原回执，不作假写。supersede只记录旧工作已落后及当前修订证明；它本身不删gap、不推进水位。旧请求仍UNKNOWN时保持REMOTE_UNKNOWN及费用责任，仅记superseded_by_revision。

设L为同一事务的memory.last_seq，G为当前空间尚未解决的gap集合：`material_seq = L`（G空），否则`min(g.first_uncovered_seq)-1`。该值用有索引的最小值及计数求得，不用max已见seq；每次mark/apply与semantic_publication同步维护，其他非MEMORY变更也更新此派生值。`published_seq`只在成功切换索引指针时前进，不能随artifact接收上升。查询区分材料覆盖与已发布索引覆盖。

begin_generation冻结captured_seq=L；依object_id分页复制已核验当前ack及artifact。publish事务要求：捕获值仍等于当前L、G空、每个当前可索引MEMORY有完全匹配的member/ack、无额外／删除成员、所有页确认、文件摘要和长度已验证；否则REVISION_CONFLICT，保持原指针。检查未能在本地期限内完成则未就绪，不假装全检通过。持续写入可能使建造无法发布，正确结果是滞后／降级；不宣称快照锁能无限期阻挡正式写入。FAILED generation清理不删除ack或付费artifact，下次按current_page重建不依赖旧gap仍存在。

例：初始published=material=10；A:r1@11、B:r1@12、A:r2@13后，A gap=[11,13]，B=[12,12]，material=10。A:r1的付费结果迟到只存产物并记superseded，不改变两个gap。A:r2应用后仅清A，material=11；B应用后G空，material=13，published仍10。以13建造时A删除@14，发布13必须失败；删除apply以真实删除revision形成DELETE ack、清A gap，material=14。新generation14排除A后发布，published=14；旧A产物仍保留。若A旧请求UNKNOWN，删除／本地发布可合法进行，但该账户后续发送仍暂停。

再次连续修改A:r3@15、r4@16时first保持15。r3应用与r4提交竞争只有两种结果：r3先提交则ack15后新gap16；r4先提交则r3的expected_revision校验失败、gap仍[15,16]。不能通过更新gap.first到16掩盖未应用15，也不能让过期DELETE证明删除一个后来重新建立的不同身份对象。

### 4.3 公共入口、共同命令封套与模式

拟新增SemanticHost静态装配；`initialize_semantic`替换新组合的`initialize_text_learning`，只支持空的新独立库（已有数据用旧组合打开，不自动导入）。同事务初始化完整配置、memory.semantic_publication和retrieval.semantic_control；初始无自动发送、scheduler=PAUSED。`open_existing`只启动§4.5检查，只有用户／原激活包驱动的`SemanticManagementPort.resume`才进入调度。

固定材料通过新的受限`FixedMemoryPort.begin/add_member/seal/establish_one/resolve`；显式预热通过`SemanticManagementPort.prewarm(set_id,query_id,exact_text,key)`；工作调度通过同端口`run_pending(limit=1)`；暂停／继续通过`pause/resume`；原键确认通过`resolve(kind,key,original_payload)`。这些是明确拟新增的公开入口，不宣称现有TextHost已实现。固定成员需使用的主体先由现有`register_initial_subjects`对应的`register_initial_subjects`命令受控登记（接口实际类名见[self_model管理端口](../../companion_memory/self_model/management.py)），复用原最多6项及既有SELF初始化前置，不生成或发布persona；名单属于固定包审核内容，不能由固定记忆正文注册。新真实检索资格以search_memory/deep_recall为主，prepare_reply若缺persona按原required区规则拒绝，不能借本包额外调用persona。固定材料无模型调用；prewarm只prepare一个QUERY工作，run_pending才可能发送；允许冷查询调用prepare的身份同样必须出现在激活包QUERY槽中，否则直接降级。

记`W={work_id:ID,expected_revision:P}`、`G={generation_id:ID,expected_revision:P}`、`F={set_id:ID,expected_revision:P}`。下表输入是恰含所列字段的payload，W/G/F展开后不可再有额外字段。全部使用现有持久外封套`{binding_id,request_key,expected,observed_at,payload}`；原payload规范化后以受限文本承载，expected为本表各根expected_revision的确定性集合，observed_at取首次可信UTC微秒并冻结，不能重试时取当前时间替代。完整原键绑定数据库／实例／原生能力／kind／全部payload；普通调用者不能提交owner、模式例外或审计actor来扩大权限。

模式：`N`表示仅普通运行且调度获准；`L`表示各模式均可做原工作本地收尾／确认，不授予新发送；`I`表示空库初始化；`U`表示普通运行的显式用户管理能力。发布属于N；梦境门控关闭后只做L。外部UNKNOWN独立账户例外只来自新包明确授权，不来自resume参数。每个调用沿原绝对期限和真实worker所有权，L也不能越过故障存储的合法确认边界。

通用结果恰`{outcome:APPLIED,targets:[Target,1..16],items:[ID,0..16],facts:{每个声明writer:Fact}}`；Target沿原object_id/previous_revision?/revision正整数，Fact沿原owner固定事实Schema。物理删叶不伪造新叶revision：targets指实际推进的父根／GC根，删除数量放其Fact；撤销gap保留ack及回执证明。必要审计每个writer一个slot，事件码固定为命令kind大写，reason=APPLY；actor来自原生绑定。targets只含表中确实变更根／叶的安全ID，不含正文、向量或未改正式对象；参与只读owner无假审计slot。

统一失败分支`R`：格式／授权／模式／过期／预期修订错误在写前拒绝；领域前置失败NotCommitted且业务、审计、回执全无；COMMIT未确认返回原RecoveryHandle及cleanup_pending，之后仅resolve原封套；Found返回原回执，不重做副作用；同键不同内容冲突；审计／存储损坏停受影响写入，不用失败回执覆盖原成功。下表每行都适用R，另列其特殊分支。读取确认无新审计／attempt。

### 4.3.1 固定集逐项原子建立与恢复

set依次OPEN→SEALED→ESTABLISHED；SEALED包含established_members从0到expected−1的**合法持久部分完成**，不另设全批事务或回滚已成功成员。固定真实小包expected=12；4096离线资格另见§6.3.1。所有材料均先add完整、seal后才establish_one，成员STORED→ESTABLISHED的单次UoW同时写新正式对象／完整来源与链接／semantic gap、member三个结果引用、set计数及原回执和两份必要事务审计；新建没有旧正文历史。其失败只使**该成员**全无或全有，其他成员保持；不能把集合状态等同索引完成。

begin、每个add、seal、每个establish有分别冻结的确定性原键／完整封套；establish按ordinal串行，key绑定set、ordinal及原content_digest，原expected修订与observed_at在首次尝试前保留。成功后成员有唯一establishment_ref；全局成功操作及stored/established计数各只增一次。响应丢失先resolve原键：Found读取原object_ref/source_id/回执且不再增计数；可靠NotCommitted仅在原owner结束、原封套和原预期仍有效时有限重试同本地键；仍Unknown停止后续成员。并发／修订冲突不能重造对象或用新键掩盖原未确认。单项回执已Found后，才为下一ordinal冻结新的set预期修订。

最后一个成员的事务还须核对其余expected−1个成员均有同库同set有效建立证明（有索引计数及逐项可恢复核验，不能仅信调用者计数），使established_members=stored_members=expected并原子将set置ESTABLISHED。不要求一次返回4096份对象或在一个命令里承载全manifest；可恢复分页核验如未完成则不能宣称集合完成，时限与完整回执上限不扩大。

例：12项集合中ordinal0–4已提交，第5项COMMIT响应丢失。持久状态只能为5项完成或6项完成，集合均SEALED；确认第5项Found后恢复同一个第5项对象，再继续第6项。第11项成功才ESTABLISHED；若第11项拒绝则11项保持、集合SEALED，不能删除此前11项模拟全批回滚。4096档相同规则；零发送、无persona／学习代签。begin＋N次add＋seal＋N次establish正常成功共`2N+2`次，不因重开或只读确认增加成功次数；额外有写的合法收尾仍计总操作上限。

### 4.4 26条新增命令及受影响原命令

| kind／输入（完整payload） | 模式／权限；owner及实际写入 | 状态、必要targets／回执items；特殊失败（另含R） |
| --- | --- | --- |
| prepare：封闭二选一：`{kind:EMBED,work_id,config,space_id,purpose,object_ref?,change_seq?,partition_id,rendered_text:S(8192),original_request_key}`或`{kind:DELETE_LOCAL,work_id,config,space_id,object_ref:Object,change_seq:P,deletion_ref:Receipt}` | EMBED为N，原DOCUMENT/QUERY能力；DELETE_LOCAL为L，仅原已提交删除的受限收尾能力；retrieval写work/control，只有EMBED写1–3输入叶 | EMBED无→PREPARED，或核验同空间／用途／分区／摘要完整原artifact后→RESULT_STORED，原request_ref可null；DELETE_LOCAL无→LOCAL_PREPARED，memory现场验证删除证明／gap，禁止文本渲染、reserve及Provider访问；targets=work/control及实际EMBED输入叶，items=work；种类混填／删除证据缺失拒绝 |
| bind：W,intent:Intent,deadline_at:T | 仅EMBED；N；调度能力＋原外层reserve证明；retrieval改work/control | PREPARED且intent为空→绑定原槽／期限，state不变；targets=work/control，items=work；已经消费的其他槽或摘要不同拒绝；Provider登记身份由record_result/record_cleanup核实带回，不接受调用者伪造 |
| record_result：W,request_ref:Request,provider_completion:Receipt | 仅EMBED；L；Provider结果owner证明；retrieval写artifact及2叶、work/control | PREPARED/BOUND/REMOTE_UNKNOWN→RESULT_STORED；targets=work/artifact/2叶/control；items=artifact；从受限recover_result取得完整向量，payload不接受自由向量；接收事务不确认原Provider账单为供应商账单 |
| apply：封闭二选一：`{kind:EMBED,W,object_ref:Object,latest_seq:P,artifact_id:ID}`或`{kind:DELETE_LOCAL,W,object_ref:Object,latest_seq:P,deletion_ref:Receipt}` | L；memory与retrieval原生参与；写ack、清精确gap、更新material_seq、work/control | EMBED/DOCUMENT仅RESULT_STORED→APPLIED；DELETE_LOCAL仅LOCAL_PREPARED→LOCAL_APPLIED且ack.action=DELETE、material_digest/artifact_id均null；核验原删除与当前gap同revision/seq，不能以NotFound替代证明；targets=ack/publication/work/control，items=object；所有owner同UoW，旧修订冲突不清gap；原键确认返回原结果 |
| supersede：W,current_object_revision:P,current_change_seq:P | L；memory只读证明，retrieval写work/control | EMBED已有完整结果或可靠未发送终态才→SUPERSEDED，UNKNOWN仍UNKNOWN并仅记录superseded_by；DELETE_LOCAL仅LOCAL_PREPARED→LOCAL_SUPERSEDED，须更晚权威修订证明，无Provider查询；targets=work/control；两者均不删gap、不改旧产物／费用 |
| fail：封闭二选一：`{kind:EMBED,W,error:Error,request_ref:Request?,terminal_receipt:Receipt?}`或`{kind:DELETE_LOCAL,W,error:Error}` | L；EMBED需原Provider终态或可靠未登记证明；DELETE_LOCAL由本地owner验证确定性拒绝，非用户自签；retrieval写work/control | EMBED无发送→NOT_SENT、已知失败→KNOWN_FAILED、可能发送→REMOTE_UNKNOWN；DELETE_LOCAL仅LOCAL_PREPARED→LOCAL_FAILED，无Provider身份且不清gap；targets=work/control；error不能NONE；本地COMMIT未知只resolve，不另写失败覆盖 |
| pause：space_id,expected_revision:P,reason:USER/BUDGET/MODE/UNKNOWN/RESOURCE/INTEGRITY | U或L原故障处理；retrieval写control | ENABLED→PAUSED；targets=control；不改work、预算和原槽；同状态同原因只原键确认，不假写 |
| begin_generation：generation_id,space_id,captured_seq:N,expected_control_revision:P | N；索引能力＋memory当前读取；retrieval写generation/control | 无→BUILDING；targets=generation/control；两代已满、G非空或捕获值不等L拒绝；无远程 |
| append_page：G,page_no:0..511,after_object_id:ID? | N；索引能力，memory ack只读；retrieval写最多8member、page、generation、control | BUILDING，按确定性owner页写入，memory.last_seq必须仍等于captured_seq，targets≤11；items=member IDs；不接任意成员内容；末页和空集合明确，修订变化拒绝并可generation_fail |
| confirm_page：G,page_no:0..511,page_digest:H | N；受控文件owner证明；retrieval改page/generation/control | STAGED→CONFIRMED；targets=page/generation/control；文件页未写全、摘要／偏移不匹配拒绝，不以SDK响应代替文件证据 |
| seal_generation：G,file_digest:H,file_bytes:N | N；文件owner只读证明，retrieval写generation/control | BUILDING→READY；全页CONFIRMED或零成员，实际头／长度／摘要／fsync与rename已确认；targets=generation/control；失败保留BUILDING或generation_fail，不能切查询指针 |
| publish_generation：G,expected_control_revision:P,file_digest:H,file_bytes:N | N；文件owner及memory参与；retrieval改新／旧generation与control，memory改publication | READY→PUBLISHED，旧→RETIRING；targets最多新旧generation/control/publication；items=新generation；完整§4.2复核失败保持旧指针；文件发布回执未确认只原键resolve |
| retire_page：G,after_row_id:ID?,expected_control_revision:P | L；原退休／失败generation的清理能力；retrieval删至多8member/page、推进generation/control | RETIRING或FAILED，有读者则不删；最后页实际文件unlink及目录fsync确认后→RETIRED；targets=generation/control；数据库删行和游标推进同UoW；末页前已有RETIRING/FAILED根作为文件删除意图，文件owner在事务外unlink/fsync后提交最终retire_page。已不存在须核实原归属及删除意图，原键确认不再unlink |
| cache_bind：cache_key:H,work_id:ID,expected_work_revision:P,expires_at:T | 仅EMBED；L；QUERY结果owner；retrieval写cache/work/control | RESULT_STORED→APPLIED；targets=cache/work/control；expiry=首次bound_at＋配置TTL（ms转UTC微秒），bound_at取原冻结observed_at；同摘要旧付费artifact复用通过prepare/本地接收证明，不新增请求 |
| cache_expire：cache_key:H,expected_revision:P,observed_at:T | L；缓存维护；retrieval改cache/control | ACTIVE→EXPIRED，targets=cache/control；未到期拒绝；不删artifact、不自动付费重建 |
| gc_page：space_id,expected_control_revision:P,after_cache_key:H? | L；清理能力；retrieval删最多8个无在途引用的EXPIRED cache，推进control | targets=control，items=已处置缓存键的安全ID；不销毁artifact/input/原work/UNKNOWN；有引用跳过并推进有限扫描游标 |
| store_embedding_handoff：request_ref:Request,original_request_digest:H,terminal_evidence_ref:Receipt | L；仅原Provider执行owner；Provider写原终态／usage／reservation／budget／handoff根及最多10叶 | 原本地完成事务的embedding专用形式；targets=原请求／attempt／handoff等实际根，items=handoff；正文从被持有的原响应解析器取得，不能由外层伪造；全部叶和终态同事务，失败仍保留原结果所有权 |
| confirm_embedding_handoff：request_ref:Request,receipt:Receipt,artifact_id:ID | L；Provider与retrieval受限核验，只有Provider写根／清理状态 | 必须有完整接收原回执；targets=handoff根，items=artifact；消费者尚在用时保留叶和cleanup_pending；确认只置接收证明，不删除叶；后续由retire_embedding_handoff有限清理，不能把一次确认当已释放全部 |
| retire_embedding_handoff：request_ref:Request,expected_handoff_revision:P,after_ordinal:N? | L；仅Provider清理owner；Provider删最多8叶并推进原handoff根清理游标 | 已有接收确认且真实消费者结束才准入；targets=handoff根，items=已删叶ID；最多两页，未完成保持cleanup_pending；每页独立确定性本地键，不以重复confirm回执执行隐藏删除 |
| fixed_begin：set_id,config,manifest_digest:H,review_ref:ID,review_digest:H | U；用户授权、监督者受托审核的受信任grant；cognition写fixed_set | 无→OPEN，expected_members由已冻结配置档位确定、reviewed_by由grant物化；targets=set；只录实际审核冻结的完整manifest（离线合成集也须明确来源与审核），用户委托审核不自动批准尚未提供的具体材料 |
| fixed_add_member：F,ordinal:0..4095,member_id,event_json:S(2048),memory_json:S(4096),content_digest:H | U；同set审核grant；cognition写member/set | OPEN；targets=member/set；原领域Schema、WORLD／主体及来源材料核验，完整行超8192拒绝而不截断；已存在ordinal同内容原键恢复 |
| fixed_seal：F | U；同set审核grant；cognition改set | OPEN→SEALED，expected_members个成员排序聚合与原manifest一致；targets=set；缺项／重复／改字拒绝 |
| fixed_establish：F,ordinal:0..4095,expected_member_revision:P | U；固定材料专属grant；实际业务writer恰cognition＋memory，memory是来源owner；两者各自必要安全审计 | SEALED成员→ESTABLISHED，改member、set.established_members，最后一项将set也置ESTABLISHED；用原MemoryTransactions.apply_change_set及memory持有的固定来源参与方法，仅建立1个新正式对象／完整来源／semantic gap，回填原对象、来源、回执；targets为这些实际根，items=object/source；仅本次一个成员的全部效果原子，集合允许持久部分完成，见§4.3.1。不得修改或覆盖已有对象，不产生旧正文历史；原键确认返回原建立结果。不得伪造学习批次或Provider成功，来源标USER_REVIEWED_FIXED；不触发run_learning，缺已授权主体拒绝 |
| resume：space_id,expected_revision:P,authorization_digest:H | U；管理grant＋原激活包；retrieval改control | PAUSED→ENABLED；targets=control；仅BUDGET/MODE/RESOURCE/USER原因已实际解除且无UNKNOWN/INTEGRITY/未清理，才可继续。原包不足／到期拒绝；不重置槽、次数、工作deadline或旧终态 |
| generation_fail：G,error:Error | L；原索引owner；retrieval改generation/control | BUILDING/READY→FAILED；targets=generation/control；不得失败已发布代，不撤销memory／ack／artifact；以后原材料重建0调用 |
| record_cleanup：封闭二选一：`{kind:EMBED,W,request_ref:Request?,provider_receipt:Receipt?,cleanup_pending:B}`或`{kind:DELETE_LOCAL,W,completion_receipt:Receipt,cleanup_pending:B}` | L；原Provider／本地完成owner核验证明；retrieval写work/control | EMBED按原证据更新关联／UNKNOWN观察；DELETE_LOCAL仅记录本地原事务实际收尾，不触及Provider，不能借清理推进LOCAL_APPLIED；targets=work/control；bool不是清理证据，缺证明保持pending；不自动调度 |

表中序列化输入的每个裸ID字段均为ID，未另标数组的字段为单值；W/G/F展开不覆盖另列字段。Provider三命令仍使用原provider_change审计规则而非虚构通用业务owner。

fixed_establish仅创建一个新对象，其实际业务writer恰为cognition、memory；结果facts及必要slot仅含这两个owner，分别沿固定cognition_semantic、memory_semantic审计声明，由统一日志审计能力在同一UoW物化。审计基础设施仍实际保存这两份审计，但不因此将logging_service再声明为业务writer。来源及source link归memory，同一UoW建立并进入其原Fact；media没有实际写入，不声明media writer或必要slot。创建没有旧正文，不声明object_history slot、不追加ObjectHistoryRecord，不伪造previous_revision=0、空旧值、REPLACE动作或空写入。原键恢复及启动核验按本命令恰两份必要审计及原结果验证，不索取不存在的历史证据；新键碰到已有对象按原创建冲突拒绝，不能转为替换。

此处共同对齐[来源所有权](formal-memory-source-media.md#sources-candidates)和[创建不产生旧正文的历史规则](logging.md#memory-history-audit)，不改旧历史Schema／DDL，不增表或命令。其他修改／删除命令所需的真实旧正文历史及审计保持；以后引入媒体变更或新建历史格式须另审明确分支，不在本固定命令暗加。新命令完整封套≤1MiB、回执≤65536，每slot事件≤8192，至多8slot；输入／文件／handoff叶写入可由一个受限命令内部有限循环完成，不绕过静态参与者。

受影响原命令：

| 原入口／命令族 | 新组合变化；输入／权限／模式 | 实际写入、必要审计与恢复 |
| --- | --- | --- |
| configuration.initialize_text_learning → initialize_semantic（替换1条） | 输入完整124键六域快照＋固定表／命令声明，初始化能力I；只空新库 | 原初始化各owner事实另含memory publication、retrieval control；各真实writer必要slot，targets为真实初始化根；唯一2MiB冻结载体。OPEN_EXISTING只核验原键／原配置，不再初始化 |
| 所有调用MemoryInformation.changed的正式创建／修改／删除、usage_change/usage_restore及固定建立 | 原命令输入、普通／内部模式授权和原gold无变；只在新组合追加semantic mark参与 | 同事务更新gap/material_seq，原memory Fact包含这些实际行数，targets保留原正式变更对象／来源且总≤16；memory Fact按下文增加语义事实；完整载体超限整事务拒绝。旧组合字节签名不变；新组合签名与原键格式版本明确分支 |
| Provider register/prepare/evidence/finish/recover_unknown | 原登记及费用预留语义；新EMBEDDING profile/usage及handoff分片 | 不放宽旧PREPARED恢复；结果专用三命令协作，必要provider_change与原回执核验保持，0attempt准入终态不复活 |
| ticket_issue_normal/ticket_issue_deep及resolve_recall | 新混合响应使用version2 ticket摘要域，输入原ticket/member结构的版本分支；原权限／状态复核不变 | 同事务确认正式revision、来源和原票据；新版摘要覆盖§5.4完整稳定语义字段，旧version1原键结果不重算。仅加查询文本范围不能自动扩大原512字节界 |
| 旧index_*、文本学习／persona及旧配置打开 | 无新能力注入，输入和恢复保持原版 | semantic不清词法gap，不改旧已发布persona／试验账本；所有既有实际写owner继续其原审计 |

受影响正式写命令的精确选择以原静态definition为身份：`commit_content_published`及其`_with_media`变体；`apply_candidate_changes`及其已声明media/goals变体；`apply_memory_none/apply_memory_ingress/apply_memory_media/apply_memory_ingress_media`；`usage_change/usage_restore`；`register_initial_subjects`，以及本轮新增fixed_establish。前三组在信息装配中的持久kind带原`information_`前缀。`plan_memory_change`和`plan_candidate_application`只准备释放计划，不能先标正式gap；没有正式MEMORY改变的SELF／主体登记只同步派生material_seq，不造embedding工作。对应原完整输入、writer和模式分别以[内容终结](../../companion_memory/runtime/content_assembly.py)、[候选应用](../../companion_memory/runtime/candidate_application.py)、[维护四分支](../../companion_memory/memory/maintenance.py)、[反馈管理](../../companion_memory/information/management.py)和[主体登记](../../companion_memory/memory/initial_subjects.py)的封闭声明保留；本增量不另造宽泛任意“memory write”入口。维护原允许NORMAL/DRAINING；学习终结／反馈沿其原门控，semantic参与不能授予新模式权限。

原正式回执结果及历史命令名字保持可解释；新组合为上述实际变更命令增加语义参与的格式签名，原输入无需新增可伪造权限字段。原targets仍指该命令实际修改的正式对象／来源，不逐个追加派生gap以挤占原16项；memory的新版固定Fact增加恰`semantic_root:ID,semantic_from_seq:N,semantic_to_seq:N,semantic_gap_delta:-8..8`（最多8个正式变更叶），记录本次派生行的真实影响及同事务根。它们不是新通用自由字段，新增完整编码预算512字节纳入该命令分支的静态增量与原8192审计总界联合验证；不减少原变更叶数或漏记原必需slot来凑数。

补充的读端口不注册写命令：memory.semantic_current/coverage/ack_page（每页8）；retrieval.work_state/artifact/page；Provider原lookup/verify_terminal/recover_result/confirm_completion及拟`send_verified_first`。跨owner只能持有这些原生句柄。固定来源由memory来源owner的`prepare_fixed_source`在fixed_establish的UoW内消费封存成员，经原来源／主体／世界验证生成实际根；不是自由来源导入，也不新增隐含写命令。

固定来源在现有来源存储载体内增加独立封闭格式FIXED_REVIEWED_TEXT_V1，根正文恰`{format,source_id:ID,entry_id:ID,world_scope:原WORLD,event:原无媒体Event,set_id:ID,member_id:ID,review_ref:ID,review_digest:H}`，完整≤8192；Event仍按原2048字节领域结构校验。现有source读取／核验端口增加该明确分支，原学习来源格式与恢复不变。不能填假batch_id、假Provider回执或将人工材料标为模型生成；原source持有者／反向引用／释放规则保持，fixed_establish同事务建立真实source link。此源格式分支计入受影响旧声明262144字节增量，不新增通用blob表。

### 4.5 启动检查与恢复后调度的发送边界

**启动／open_existing／原键resolve／观察永远零发送。** 它们分页检查配置、原业务根、gap/ack、Provider原回执、已付费产物及文件，保留有界未完成状态；不会因为检查结束而自行启动run_pending。随后获准调度属于另一明确入口，按以下表判定，不笼统承诺所有“恢复后”都零发送。

| 原持久状态 | 启动检查／确认动作（均0发送） | 恢复后run_pending能否首次发送 |
| --- | --- | --- |
| DELETE_LOCAL的LOCAL_PREPARED／LOCAL_APPLIED或本地COMMIT未确认 | 只核验删除原回执、gap／ack及本地原键；Found保留原LOCAL_APPLIED，可靠未提交且原预期有效才重做原apply；过期删除转LOCAL_SUPERSEDED，不清新gap | 永远不发送、不reserve、不分配attempt；无Provider查找或未发送证明要求，按L收尾仍服从持久与资源准入 |
| EMBED的PREPARED，尚无原生登记、尚无外层slot | 重建原文本／摘要／权限，lookup仅取得候选事实，原工作未过期 | 只有仍有效激活包可为该已批准用途首次reserve并bind；再获原生REGISTRATION_ABSENT证明，才可首次发送1次；无包只保留待办 |
| PREPARED，已有外层slot但未登记 | 核验原slot、请求摘要、授权及deadline，不重新reserve或计数 | 可以使用**同一**slot和原发送意图，经原生absence seal及当前门控，进行该槽唯一一次首次发送；若槽已记终态NOT_SENT／CANCELLED，不能复活 |
| Provider已有0attempt的MODE_BLOCKED/PAUSED_BUDGET终态 | verify_unsent可得ZERO_ATTEMPT_ADMISSION_TERMINAL；保留原终态及已消费slot | 不允许。该证明用于忠实记录未发送，不授予改键或复活终态；继续其他原包QUEUED用途不等于重试此槽 |
| Provider已登记PREPARED attempt，原进程已退出，无持久完整结果 | 按现行Provider转REMOTE_RESULT_UNKNOWN并保留held；同进程仍存活的原worker则仅等待／确认其收尾 | 新worker不允许接手发送。内存“尚未调用”、lease过期、Adapter未见请求都不是跨进程证明；本方案不新增已登记attempt的转交发送能力 |
| 已发送／发送可能发生，远程UNKNOWN | 原键只读或合法已启动收尾；无供应商查询探针 | 不允许重发，不消耗其他槽代替；账户停止新增发送，本地查询／结果确认可继续 |
| 已有完整Provider终态／handoff，artifact尚未接收 | 原键verify_terminal、recover_result并通过本地record_result接收 | 不发送；缺叶／摘要矛盾为完整性故障，不能重新调用重建 |
| artifact或发布本地COMMIT未确认 | 使用原RecoveryHandle及原payload确认；可靠NotCommitted且完整原材料仍有owner时，仅重做原本地事务 | 不发送；原结果确已丢失则保留UNKNOWN／FAULTED，不造模型结果 |
| 已APPLIED／缓存命中／索引丢失 | 核验原绑定并从付费artifact重建文件 | 不发送，新增调用必须为0；索引损坏不是收费理由 |

现有`verify_unsent`会封住原键，`WorkPort._work`在seal持有时拒绝；因此拟补`WorkPort.send_verified_first(original_request,absence_seal,outer_intent)`：仅EMBEDDING，严格REGISTRATION_ABSENT，原生同一串行区内重查存储／无活跃owner／原描述、核验外层绑定、消耗seal并保留唯一首次执行owner，再沿原登记事务和门控发送。证明不序列化成可伪造许可、不直接丢seal后裸调用embed；两个竞争调度者只有一个可消费，另一者只查原键。此为新增的受限适配衔接，**不改变Provider既有已登记恢复保证**。

以下网络期限及absence seal规则只适用EMBED；DELETE_LOCAL不进入候选发送队列，也不取得工作发送意图。其本地prepare/apply沿原单操作期限，COMMIT未确认只确认原键，不把本地故障叫REMOTE_UNKNOWN。

prepare不开始网络期限；bind在首次确有slot及准入时冻结deadline_at（UTC）和对应进程内单调期限。重启后只取该原UTC剩余时间与当前单调时钟建立保守上界；时钟倒退／无法证明剩余时拒绝发送，不能重置60秒。原deadline到期即只可本地确认或以可靠未发送证明记NOT_SENT，slot不退还。resume只恢复调度开关；新gap尚未prepare的工作可在其第一次bind时获得新期限，已绑定工作不续期。

触发例：slot D03已reserve，Provider尚未登记时进程退出；启动查到NotFound仍0发送。用户／原激活调度resume后，D03原意图未过期且absence seal可靠，首次登记attempt A03并发送，外层仍消费原D03一次。若退出发生在A03登记COMMIT之后，恢复必须UNKNOWN，即使实际网络字节为0也不再次发送；若退出发生在handoff COMMIT之后，则接收原向量并应用，0新增attempt。三者在验收中使用不同故障截点，不共用“恢复0发送”的含混断言。

## 5. 索引发布及检索

### 5.1 建议首版使用有界精确向量检索

建议先做1024维float64、余弦相似度的精确扫描，单空间最多4096个可索引对象；不引入ANN库、训练或额外模型。此为新增语义能力的候选容量，不缩小正式记忆原容量，也不宣称超过4096条时仍有完整语义覆盖。超过上限保留正式记忆和dirty事实、报告SEMANTIC_CAPACITY及降级；完整覆盖资格不通过。用户若要求原正式记忆最大容量全部语义可用，应扩大索引方案后再批准。

索引文件是从已持久向量构造的不可变generation：临时文件写完、校验摘要及长度、fsync文件、原子重命名并fsync目录之后，才在UoW内发布指针与覆盖事实。文件完成但事务未确认时按原发布键核实，不重算向量。发布前退出留下的文件由持久建造记录认领；指针不得指向缺失或损坏文件。重开缺文件时用artifact重建、期间词法降级，零模型调用。进程中断验证不冒充物理掉电保证。

单个空间最多一个建造者、两个generation（当前＋建造或退休）和既有两个查询读者；旧读者未结束时第三代不准入。读取者持有generation直到真实计算与交付结束，超时返回不提前释放mmap／文件／目录。跨空间物理目录、发布指针及成员索引隔离；禁止混合不同模型／维度／epoch的向量做相似度。模型配置变更只允许新静态实例／空间，完整热迁移不在范围。

扫描拟以只读文件映射及最多128个候选堆工作，不构造4096组Python浮点列表；每16个成员检查取消和剩余期，归一化采用避免中间溢出的计算并验证有限范数。两代文件映射最多80MiB，查询临时工作集建议每路不超过8MiB，实际驻留峰值还需把Provider缓冲、SQLite页缓存及宿主原资源计入。真实4096规模不能在既有总期内完成时保持降级，不宣称已通过最大容量性能资格。

### 5.2 查询缓存、候选准入与融合

拟新增REAL_HYBRID_V1，保持旧LOCAL_LEXICAL_V1原响应／排名。查询正文沿原512 UTF-8字节界；文档渲染仍8192。缓存键绑定实例、数据授权分区、空间、QUERY用途、原完整文本和渲染版本，最多128项，有效期86400000ms。过期仅取消命中资格；同空间同材料的已有付费QUERY产物经原生证明可再次cache_bind，不自动重复付费。缓存不是授权快照，交付仍读当前owner。

必须在RRF之前决定候选能否进入，RRF只排序，不能把“总有一个最近向量”变成相关性。三种选择均不调用额外模型：

| 策略（选择已批准） | 无相关输入及代价 | 建议 |
| --- | --- | --- |
| 仅取向量Top-K | 非空索引几乎总返回结果，没有拒绝依据 | 不采用 |
| 最高分与次高分差值门槛 | 可能拒绝两个同样相关对象；全部不相关时也可能有大差值，单候选无法比较 | 不采用为首版主门槛 |
| 绝对余弦下限＋独立词法证据下限 | 可确定性拒绝两路证据均不足的对象，但阈值与模型空间相关，需固定集独立验证 | **推荐**下述固定值；不是普遍语义正确保证 |

建议语义cosine≥0.70才进入语义列表，按原float64稳定计算，恰等于门槛通过，不先四舍五入。词法列表在新混合模式内须满足`distinct matched query bigrams / distinct query bigrams ≥0.60`；分母按现有NFKC/casefold及L/N字符段相邻双字符项去重，停用词不新增。没有双字符项时，仅允许至少一个完整规范化L/N段与对象完整段精确相等；单字符“塔”可命中完整单字段“塔”，不能仅因“塔楼”含一个字就通过。无查询词项则进入结构查询规则，不计算这个比值。原词法评分仍决定已准入词法候选的名次，新门槛不追溯改变旧LOCAL_LEXICAL_V1。

任一路达到其门槛即可参与融合，语义命中不要求词法重叠；两路均不达标则拒绝。建议每路64候选，去重与显式ID／结构候选合计≤128；先保留最多8个显式ID，再按两路RRF排序填剩余候选位；文本存在时结构条件是AND过滤，不能仅凭符合世界／日期就绕过两个文本门槛。显式ID表示调用者明确选择，绕过分数门槛，但不能绕过授权、当前revision、lifecycle或其他AND条件；纯结构查询不调用embedding，按原结构排序返回。

RRF等权，排名从1开始，分数为各路`1/(60+rank)`之和，未入该路为0，同分按object_id字节序，最终最多8项。有效候选数为0时合法返回memories=[]、recall_id=null，不能降低门槛补满。semantic扫描失败／无可用查询向量时采用经过上述词法门槛的LEXICAL_ONLY降级；它仍可能误召回，不能声称降级结果具备语义相关证明。纯显式／结构请求为STRUCTURAL_ONLY，不伪装混合命中。

deep_recall只按原许可读取FORGOTTEN，普通查询限ACTIVE，DELETED永不交付。物理索引预过滤和最终memory权威读取／交付屏障同时保留；末次核验撤权／旧修订时丢弃，可在剩余原期限内从已备候选补位，不追加远程请求。索引完整但全部低分属于合法拒绝；索引不全／超时导致空集属于降级，二者在响应中区分。

固定验收建议6个实际查询中至少2个为用户委托监督者在真实输出前确认为无相关的文本查询；不带显式ID，不以纯结构选择混淆标签。每个无相关样本在完整索引＋有效查询向量情况下必须memories为空、recall_id=null、decision=NO_MATCH，误召回对象数0、错误非空查询数0，两项均须全通过；timeout/UNKNOWN/不完整索引不算空集通过，记未完成并使该资格未通过。分别保留词法基线、混合及受控LEXICAL_ONLY降级的原输出；受控降级的相同无相关样本亦要求空集，否则降级拒绝资格失败，不拿DEGRADED掩盖误召回。阈值在真实输出前冻结，失败后不事后调阈值改分。

触发例（合成分数不是模型实测）：无显式ID、同世界对象cos=.69、词法覆盖=.50时必须排除；cos=.70但词法0可进入语义；cos=.30、词法=.80可进入词法。用户指定ID且授权有效时即使cos=.10也返回该选择，这不属于“无相关文本检索”验收。发布索引缺失时cos不存在，词法=.50须空降级，不能从向量Top-K兜底。

### 5.3 期限、降级和实际清理

查询仍服从原1000ms绝对期限，排队、缓存、可选query embedding、索引、过滤、编码与交付均消费同一期限。冷查询远程最多250ms且预留至少200ms本地尾部；实际期限取原剩余与各阶段上限最小值，不重置deadline。没有已授权QUERY槽或余量不足时不发送，直接有原因的词法降级。固定18次试验默认只显式预热，普通查询没有额外付费许可。

冷缺失同键single-flight，只能一个受宿主管理的实际owner；其他查询按各自剩余期限等待或降级。同步返回不会释放仍在途worker，迟到完整结果可按原键本地收妥供以后使用，本次已交付响应不改写。UNKNOWN保留held、原工作材料和实际资源，暂停该账户新增请求；本地查询／合法原键收尾可继续。不会用新worker替旧worker消除超时。

文档和显式预热首次bind后总期60000ms、attempt／读取30000ms、连接10000ms，各段服从剩余期限；重启适用§4.5，不重新给原工作60秒。索引／GC每次本地5秒、每页8项、扫描每16成员检查取消。只一个付费worker、零内存发送等待队列，磁盘gap不是无限内存任务。资源未释放或目录仍被旧owner占用时不能进入下一次发送。

### 5.4 混合响应的封闭版本

新查询输入沿现有QUERY/PREPARE封闭字段，仅retrieval_mode新增REAL_HYBRID_V1；query_text仍512、完整输入4096、rerank=false。旧模式返回version1不变；新模式完整响应≤131072，`response_version=2`，根**恰含**：

`response_version,request_id,recall_id,availability,observed_at,mode_epoch,config_snapshot_id,requested_mode,actual_mode,sections,coverage,truncation,capabilities,query_vector,admission,decision,timing`。

| 字段 | 完整封闭类型及关系 |
| --- | --- |
| request_id／recall_id／observed_at／mode_epoch／config_snapshot_id | ID／ID?／T／N／ID，沿原身份和epoch；无memory则recall_id=null |
| requested_mode／actual_mode | requested固定REAL_HYBRID_V1；actual为HYBRID/LEXICAL_ONLY/STRUCTURAL_ONLY；HYBRID表示已执行完整语义路径，不要求实际有候选 |
| availability | COMPLETE/DEGRADED；COMPLETE仅两路应执行部分全部可用、无截断／缺口；纯结构不需语义向量，仍可COMPLETE |
| sections | 原封闭section格式逐项保留：memories及按search/deep/prepare和include标志选入的state/goals/persona等原允许段；对象投影、世界、来源不新增自由字段，不复制或放宽原段Schema |
| coverage | 恰`{lexical:LexicalCoverage,semantic:SemanticCoverage}`；LexicalCoverage恰captured_seq:N,contiguous_seq:N,pending_count:N,generation:ID?,preprocess_id:LOCAL_LEXICAL_V1,unicode_version:S(32),posting_visits:N,candidate_count:0..128,observed_at:T，原语义不改 |
| SemanticCoverage | 恰`{state:COMPLETE/PARTIAL/NOT_READY/UNAVAILABLE/NOT_APPLICABLE,space_id:ID?,generation_id:ID?,captured_seq:N?,material_seq:N?,published_seq:N?,first_uncovered_seq:P?,pending_count:N?,scanned_count:0..4096,observed_at:T}`；不可知数字为null不补0；COMPLETE需published=captured、无gap且扫描所需候选范围完成；NOT_APPLICABLE只给纯结构；无gap的first=null不能单独推出索引完整 |
| truncation | 恰`{reasons:[Reason,0..16],omitted_memories:N?}`，Reason取下文封闭集合，去重并按枚举顺序排序；未知遗漏数量null；不得以对象字节截断伪完整 |
| query_vector | 恰`{state:CACHE_HIT/REUSED_ARTIFACT/REMOTE_RESULT/UNAVAILABLE/NOT_REQUIRED,space_id:ID?,artifact_id:ID?,request_ref:Request?}`；没有向量后三者可null，不能返回向量或原文；REMOTE_RESULT须确有本次原请求完整结果 |
| admission | 恰`{policy:ABSOLUTE_COSINE_LEXICAL_V1,semantic_min_millionths:700000,lexical_min_millionths:600000,semantic_accepted:0..64,lexical_accepted:0..64,explicit_accepted:0..8}`；阈值是规则身份，非事后估分；不返回跨权限被拒对象ID |
| decision | MATCH/NO_MATCH/INCOMPLETE_EMPTY；有memory为MATCH；完整评估后空为NO_MATCH；降级空为INCOMPLETE_EMPTY，不当无相关资格通过 |
| capabilities | 恰`{generative_query:false,embedding:true,rerank:false,semantic_equivalence:false,real_persona:B,persona_origin:REMOTE_PROVIDER/SYNTHETIC/UNAVAILABLE}`；embedding为本装配声明，actual_mode和query_vector才说明本次实际执行 |
| timing | 恰`{base_ms:N,budget_ms:1000}`，base包含原全部路径；超时沿原Rejected/RecallPending/Unconfirmed封套，不返回伪成功version2 |

Reason封闭集合：`QUERY_VECTOR_MISSING,BUDGET_PAUSED,PROVIDER_UNKNOWN,PROVIDER_KNOWN_FAILURE,DEADLINE,INDEX_NOT_READY,INDEX_LAG_PARTIAL,INDEX_FORMAT_LIMIT,SEMANTIC_CAPACITY,CANDIDATE_LIMIT,PERMISSION_CHANGED,REVISION_CHANGED,SECTION_LIMIT,RESOURCE_BUSY,INTEGRITY_UNAVAILABLE`。低于分数门槛本身不是降级原因；故完整NO_MATCH没有理由。原词法截断reason在新分支逐项映射到上述枚举，未知理由归INTEGRITY_UNAVAILABLE并拒绝strict完整成功，不吞掉；不能默默扩大集合。实际权限撤销若原契约要求拒绝整个交付，仍拒绝，不能只发PERMISSION_CHANGED绕过原屏障。

require_complete=true或allow_partial=false时，只要本次必需路径非完整／存在Reason就沿原INDEX_NOT_READY或对应安全错误返回，不发送version2降级成功。strict无相关样本只有实际完整NO_MATCH算通过。新ticket response_digest绑定上述根中除timing外的全部稳定语义字段及原binding（根生成在票据事务之前），resolve返回原CONFIRMED_ONLY及摘要确认，零重新检索／embedding；不使用旧仅binding＋sections摘要冒充新版模式和覆盖被确认。

## 6. 完整配置、二进制格式和容量（核心定义已批准）

### 6.1 六项新增配置的封闭结构

以下六项统一为instance/no_override/public、初始化一次生效、不可null、**无默认值**，必须显式提供；字面数值默认指SMALL_REAL_TRIAL候选；仅§6.3.1逐项列出的OFFLINE_CAPACITY候选替换值可组成另一完整静态向量，不能交叉任选或热改。ID／H／路径须绑定实际资源。每项完整值≤8192；新增结构内部不再定义动态键。配置owner统一configuration，表中负责方提供固定验证器；注册数由118到124，六域与128键上限不变。校验器不读取key，不以配置解析替代实际环境准入。

| 新键／负责方、依赖 | 完整对象字段（恰含；本表v=1，用量传输分支另见§7.2） |
| --- | --- |
| retrieval.embedding／retrieval；依赖provider.profiles、provider.role_profiles及memory.usage | `{v:1,qualification_profile:SMALL_REAL_TRIAL/OFFLINE_CAPACITY,document_profile:ID,query_profile:ID,render_id:TEXT_MEMORY_RENDER_V1,render_digest:H,document_max_bytes:8192,query_max_bytes:512,max_active_work:64,paid_workers:1,queue_slots:0,request_timeout_ms:60000,dispatch_gap_ms:30000,page_size:8}` |
| retrieval.semantic／retrieval；依赖retrieval.embedding及provider.profiles | `{v:1,space_id:ID,deployment_epoch:ID,algorithm:EXACT_COSINE_F64_V1,dimension:1024,object_limit:4096,generation_limit:2,query_readers:2,lexical_candidates:64,semantic_candidates:64,combined_candidates:128,result_limit:8,rrf_k:60,semantic_min_millionths:700000,lexical_min_millionths:600000,admission_policy:ABSOLUTE_COSINE_LEXICAL_V1,scan_check_members:16}` |
| retrieval.query_vectors／retrieval；依赖retrieval.semantic及retrieval.embedding | `{v:1,cache_limit:128,ttl_ms:86400000,cold_remote_ms:250,local_tail_ms:200,cold_policy:AUTHORIZED_SLOT_ONLY,single_flight:true,expired_policy:REUSE_PAID_OR_DEGRADE}` |
| retrieval.semantic_storage／retrieval与持久化；依赖storage原限额及retrieval.semantic | `{v:1,index_root:S(1024),artifact_limit:8192,file_limit_bytes:41943040,index_total_bytes:83886080,query_scratch_bytes:8388608,database_stop_bytes:2147483648,wal_stop_bytes:1073741824,backup_stop_bytes:2147483648,temporary_stop_bytes:134217728,directory_stop_bytes:8589934592,free_reserve_bytes:2147483648,normal_operation_limit:1000,completion_operation_reserve:200,local_step_ms:5000}` |
| provider.embedding_transport／Provider；依赖profiles/accounts及凭据引用定义，完整OFFLINE分支见§6.3.1 | `{v:1,profile_refs:[ID,2..2],protocol:ARK_CODING_DENSE_TEXT_V1,origin:https://ark.cn-beijing.volces.com,endpoint_path:/api/coding/v3/embeddings,secret_ref:ID,secret_revision:ID,expected_reported_models:[ID,1..1],server_evidence_ref:ID,sdk_evidence_digest:H,request_max_bytes:65536,response_max_bytes:65536,normalized_max_bytes:40960,number_token_max_bytes:32,header_max_bytes:16384,header_count:100,chunk_bytes:8192,connect_timeout_ms:10000,read_timeout_ms:30000,network_slots:1,queue_slots:0}` |
| management.semantic_observation／management；依赖retrieval.semantic及Provider观察范围 | `{v:1,page_size:16,max_concurrent:2,timeout_ms:2000,include_vectors:false,include_query_text:false,include_source_text:false,include_audit:false}` |

跨字段要求：两个profile ID不同而指同一账户／模型／空间，分别只授DOCUMENT与QUERY；SMALL_REAL_TRIAL可选下列TOKEN_METERED或§7.2完整USAGE_ONLY_TRIAL分支，OFFLINE_CAPACITY按§6.3.1，各完整分支不得混装；维度、space及协议精确一致。TOKEN_METERED的expected_reported_models唯一值须有服务端依据，本次用量分支的明确集合见§7.2；均不能填宽泛前缀。60000总期≥30000 attempt/read≥10000连接，cold250＋tail200≤1000且实际服从剩余；memory原对象上限不被object_limit4096替换。index_root须为新的绝对自有目录，拒绝路径别名／symlink／重叠旧目录，具体物理验证在实际启动。数据库与WAL上限从属于原持久化安全限额，不由此绕过原更严限制。

受影响继承配置精确限定：provider.accounts/profiles/role_profiles增加新封闭分支，provider.result_max_bytes及离线provider.transport分支按§6.3.1固定；runtime.operation_timeout_ms在新组合60000，信息查询仍自有1000、索引本地5000；唯一初始化body界见§6.3。除此以外其他旧键、旧组合值和旧role行为不变。

以下账户／profile字段仅指SMALL_REAL_TRIAL的TOKEN_METERED分支；本次USAGE_ONLY_TRIAL完整替代在§7.2，OFFLINE_CAPACITY的完整替代在§6.3.1。新增账户分支恰`{account_id:ID,window_id:ID,currency:CNY,max_in_flight:1,attempt_limit:18,cost_limit_atoms:5000000,atom_scale:1000000,billing_mode:TOKEN_METERED,price:Price,quota:null,evidence_ref:ID}`。Price恰`{revision_ref:ID,source_url:S(512),checked_date:S(10),input_atoms_per_million:N,cached_atoms_per_million:N?,output_atoms_per_million:0,per_attempt_money_bound:N?}`；缓存价如存在必须有官方定义，本首包不推断缓存分项。新profile恰`{profile_id:ID,account_id:ID,model_id:doubao-embedding-vision,wire_protocol:ARK_CODING_DENSE_TEXT_V1,capability:EMBEDDING,max_attempts:1,attempt_timeout_ms:30000,max_input_units:P≤1048576,max_output_units:0,max_items:1,dimensions:1024,space_id:ID,media_tasks:[],embedding_ref:ID,billing_mode:TOKEN_METERED}`；max_input_units为获证明的token责任界，不能填文本项数。role_profiles在本组合的新增映射恰EMBEDDING_DOCUMENT:[document_profile]、EMBEDDING_QUERY:[query_profile]，每个数组长度1；继承角色的原值另保持，不授予新角色GENERATION或RERANK。

当前Coding账户不得伪装为TOKEN_METERED；本次已批准按§7.2的USAGE_ONLY_TRIAL独立分支实施及激活，不再等待订阅扣减换算或费用证明。完整SUBSCRIPTION结算不在本次范围。旧账户14–16次约束不被全局改成18次，18只属于本新包；旧按token计费与模拟分支的Schema、责任和恢复语义保持。

### 6.2 索引二进制格式

固定`EXACT_COSINE_F64_V1`：整数全为无符号little-endian，浮点IEEE754 binary64 little-endian，禁止NaN/Infinity、负零规范化差异须按原产物字节保留并在余弦中等价处理；不把归一化后的值写回。文件恰4096字节头＋N个8384字节记录，N=0..4096，无尾随数据。文件名为generation域摘要的安全固定ID，不接受调用者路径。

| 头偏移／长度 | 值 |
| --- | --- |
| 0／8 | ASCII `IRISVEC1` |
| 8／4，12／4，16／4，20／4 | format=1，header_bytes=4096，dimension=1024，record_bytes=8384 |
| 24／4，28／4，32／8，40／8 | member_count，page_count=ceil(N/8)，captured_seq，records_offset=4096 |
| 48／32，80／32 | space_id域摘要、generation_id域摘要（二进制SHA256）；恢复核对完整ID对应关系 |
| 112／32，144／32 | 按ordinal的成员绑定规范化摘要、整个记录区SHA256 |
| 176／8，184／4，188／4 | file_bytes，endian marker=0x01020304，float_encoding=1 |
| 192／3904 | 全0保留区；非0拒绝，不暗中扩展版本 |

| 每条记录相对偏移／长度 | 值 |
| --- | --- |
| 0／2，2／128，130／2 | object_id字节数1..128、ASCII ID按0补齐、全0对齐 |
| 132／8，140／8 | object_revision、applied_seq |
| 148／32，180／8，188／4 | artifact域摘要、ack_revision、全0保留 |
| 192／8192 | 1024个原规范化float64向量值 |

artifact ID固定`embedding-artifact:`加64位域摘要，可从32字节还原，不把任意128字节ID截成32字节；generation/space亦有明确域摘要绑定。成员按object_id原字节严格升序、无重复；ordinal隐含于文件位置，SQLite member与其完全一致。成员摘要输入为固定版本的有序`(object_id,object_revision,ack_revision,applied_seq,artifact_id,vector_digest)`规范化列表。完整文件摘要存在generation根，避免把文件自身摘要嵌入自身造成循环。

append_page只持久成员声明；文件owner按该页写记录。confirm_page验证实际对应记录字节；新增seal_generation验证全部页、头、文件长度和摘要，执行fsync文件、原子rename及目录fsync，然后将BUILDING→READY，尚不切换查询指针。N=0时page_count=0，直接seal空记录区及SHA256(empty)，不创建伪空页。publish_generation再执行跨owner最终事务；因此文件READY、索引PUBLISHED和旧文件实际清理分别有证据。

最大记录区`4096×8384=34340864`，文件`+4096=34344960`字节；每页最多`8×8384=67072`，最多512页；两代`68689920`字节，小于80MiB额度。原8320估算遗漏完整128字节ID及绑定元数据，已改为8384，表／页／文件上限同步。临时写入必须属于两代中的建造代，不能在两代之外再造一整代副本；旧读者未退出就暂停下一建造。

### 6.3 完整载体与存储容量推导

以下均为**静态上界／准入规则，不是实测**。完整记录同时满足字段及总编码界，不能从每字段合法推导任意组合都合法。

| 载体 | 推导／完整界 |
| --- | --- |
| 请求 | 文档8192或查询512原UTF-8，JSON最坏6倍转义＋4096封套：文档`8192×6+4096=53248≤65536`；不裁正文，原生完整请求同样计身份／权限封套 |
| 响应／规范化向量 | HTTP≤65536；单数值词法≤32，1024个数与分隔最多33792，另≤4096封套，合计37888≤40960；原规范化器最短float64重编码另须守40960，解析不修复非法数 |
| Provider交接 | 40960最多10叶，每叶4096原始bytes→base64最多5464；加2680元数据仍≤8192。根完整≤4096；总payload上界`10×8192+4096=86016`，因此必须分片持久且回执只引用根，不内嵌全部叶 |
| artifact／输入 | artifact两向量叶，每叶完整8192；根8192，单产物最多24576。work输入3叶×8192＋work根8192=32768；3072原字节叶base64≤4096，余量用于封套。缓存／member/page/control/ack等根各8192，外层字段全计，不把SQLite页开销算入此payload界 |
| 配置初始化 | 初始化的2MiB例外单独计，不套普通单操作1.25MiB估算。六新值最多49152；总body573440，`3×573440+124×256+6×8192+65536=1866752≤2097152`。适用前提是继承值替换仍在原524288以内；超出拒绝，不扩大普通命令1MiB或回执65536 |
| 静态注册 | 15新表的完整声明分别限32768，共491520；26新命令每份完整输入／结果／参与／审计定义限8192，共212992；6键定义限8192共49152；1初始化替换及受影响旧命令分支总增量限262144。新增上界1015808，加旧静态总界3145728＝4161536≤4194304（4MiB） |
| 新增表数量分配 | memory3、retrieval9、Provider1、cognition2＝15；每声明实际完整编码必须符合上行，不得将Schema拆到未计量私有注册表 |
| 持久数量 | 付费artifact≤8192；活动work≤64，历史work／input和已确认回执不自动删，受累计操作与目录界。generation活动≤2，各member≤4096、page≤512；cache≤128；固定set每包1，真实小包member12，离线资格member4096（§6.3.1）。gap/ack按原正式变更事实保留，不能用64或4096限制正式对象 |
| 最大向量产物逻辑量 | `8192×24576=201326592`（192MiB），含根与2叶；两代member+page根粗界`2×(4096+512)×8192=75497472`（72MiB）；另加文件68689920。不是整库物理大小 |
| 单试验操作止损 | 原10000候选未包含回执／审计累积，改为普通1000次＋原工作收尾专用200次，合计≤1200。每新操作的持久命令证据、回执、审计及绑定保守留1.25MiB逻辑额，`1200×1310720=1572864000`；业务根／叶及原基础配置另外按各表计，非凭此声称SQLite已足够 |
| 物理空间准入 | 原1GiB小包假设撤回；建议新小包目录8GiB、空闲预留2GiB，db≤2GiB、WAL≤1GiB、一份备份≤2GiB、临时≤128MiB、索引≤80MiB；这些同时上界加2GiB余量为7GiB＋208MiB＜8GiB。WAL、备份及临时重复拷贝不得漏计；实际放大触界即停新工作 |

新静态声明总界若按实际完整Schema编码超过4MiB，就属于该候选不通过，必须回报实值，不能用省略字段过关。15表／26命令的名称及全部新增规则已在本文展开；数值上界的未来编码资格不等于内部Schema仍待设计。物理空间上限是受监控准入值，不是填满宿主磁盘的权限或无损掉电保证；磁盘无法量化、监控失联及预留不足时停止新工作，原本地确认仍保留所有权。

操作计划上界：固定建立26次；12文档＋6查询每项最多16个本地持久步骤（准备／绑定、原生登记／完成、接收／确认／分片清理、应用／缓存、观察收尾及其必要有限恢复），合计288；首次索引12成员的begin/2 append/2 confirm/seal/publish为7；6次正式查询出票最多6；pause/resume及状态维护预留20；总347＜1000（另计初始化及一次主体登记后仍小于1000）。每项16步骤是待验收硬限，不含无限恢复重试；失败提前停止，普通剩余额度不能作为远程重试。额外200只服务已启动工作的合法有限收尾，不创建新收费意图，若仍不足保持未确认并集中报告。纯读取确认不记新持久操作。最大容量离线资格必须使用下述完整候选向量，不把小包物理界或4096资格外推至100000对象的P/X保证。

### 6.3.1 4096对象离线资格的完整档位、操作与物理预算（定义保留，完整运行不授权）

保留SMALL_REAL_TRIAL的1000普通＋200收尾、8GiB目录建议；它不能承担4096对象建立。建议另设**OFFLINE_CAPACITY**固定档位，由上述retrieval.embedding.qualification_profile选择；仍6个新键／总124键、15表／26命令，无测试旁路写库。两档均为全新、独立且一次冻结的配置／实例／数据库／目录；从小包换档必须另获资源与实施授权，不能给已运行小包重置计数或热改限额。本节封闭定义、校验和低资源组件边界属于已批准核心方案；用户明确不批准800GiB资源方案，因此不分配／扩容该卷、不启动4102次模拟执行及约34小时的完整资格。以下大档物理预算仅用于解释未验证限制，不作为后续执行者申请或启动资源的默认要求。小档继续交付，不能缩小对象数、删除必要证据或重置预算后宣称4096资格通过。

| 完整配置组合的差异位置 | OFFLINE_CAPACITY精确候选；未列字段保持§6.1原值及原封闭类型 |
| --- | --- |
| retrieval.embedding | qualification_profile=OFFLINE_CAPACITY；所有输入、渲染、1024维、并发1、活动work64、队列0、60000总期、30000调度间隔仍保持。真实小包qualification_profile=SMALL_REAL_TRIAL |
| retrieval.semantic_storage | normal_operation_limit=90000，completion_operation_reserve=2000；database_stop_bytes=274877906944（256GiB），wal_stop_bytes=34359738368（32GiB），backup_stop_bytes=274877906944（256GiB），temporary_stop_bytes=8589934592（8GiB），directory_stop_bytes=687194767360（640GiB），free_reserve_bytes=137438953472（128GiB）。artifact8192／两文件80MiB／scratch8MiB／单步5000ms等其余字段不变 |
| provider.embedding_transport | 整个值为独立封闭对象`{v:1,profile_refs:[ID,2..2],protocol:SIMULATED,normalized_max_bytes:40960}`；无origin、endpoint、secret、SDK、HTTP或任意场景脚本字段。不能把真实分支字段填null混入；模拟资源仅由可信测试装配持有有界固定场景，不经配置传可执行内容 |
| provider.accounts | 复用[模拟账户封闭结构](configuration.md)：唯一项恰`{account_id:ID,window_id:ID,currency:TEST,max_in_flight:1,attempt_limit:4102,cost_limit_atoms:4102}`；每个attempt最大责任1 TEST atom。无CNY、TOKEN_METERED、价格URL、真实额度／¥0账单声称 |
| provider.profiles | 两项各恰`{profile_id:ID,account_id:ID,model_id:synthetic_dense,wire_protocol:SIMULATED,capability:EMBEDDING,max_attempts:1,attempt_timeout_ms:30000,max_input_units:1,max_output_units:0,max_items:1,input_price_atoms:1,output_price_atoms:0,dimensions:1024,space_id:ID,media_tasks:[]}`；模拟计量input_items=1、input_units=1，不能冒充tokens，仍另受8192／512字节输入界 |
| provider.role_profiles及旧生成传输 | 新组合只给EMBEDDING_DOCUMENT/EMBEDDING_QUERY各自一个profile；无生成工作句柄。124键静态全集中的旧provider.transport离线值明确采用新增封闭不启用分支`{v:1,enabled:false}`，仅在OFFLINE_CAPACITY适用，旧组合仍按原真实结构拒绝此值。其余继承键仍必须完整验证，不加载任何secret resolver或真实传输 |
| 完整载体与绑定 | 仅这两个新semantic组合将继承键provider.result_max_bytes固定为40960（原定义若需扩展也仅限此组合），embedding结果通过40960规范化载体／handoff分片；旧SIMULATED服务的8192限制与完整配置匹配保持。OFFLINE的Provider账本／结果显式SIMULATED、usage.source=SIMULATED_REPORTED；新的持久配置绑定与原键恢复进入semantic装配版本，不冒充旧无版本模拟装配。普通命令1MiB、初始化2MiB、静态声明4MiB及单行8192保持；新增分支必须一起算入原注册预算 |
| 其余新键及继承关系 | retrieval.semantic、retrieval.query_vectors、management.semantic_observation全部字段不变；runtime总期60000、query1000及storage原同步／事务／读写槽等不变。原更严资源检查不得被绕过：新档位的统一准入必须静态匹配本行大档限额；不挂靠原F档20000操作／12GiB运行声明。无法同时通过完整向量校验即资格未就绪，禁止运行时覆写局部限额 |

这里规定校验器应接受的完整分支及固定取值，实际实现与验证见CURRENT_TASK。仅复用SIMULATED能力形状，1024维分片交接和新组合配置衔接属于已批准核心增量；若实物完整Schema超过§6.3界限，该档不通过，不能省字段。真实分支的授权与发送条件另按§7.2，不由离线结果证明。

固定集N=4096（真实小包N=12），ordinal为0..N−1；相同4条固定入口，不新增批量建库命令。离线集是合成材料，冻结生成模板、完整逐项manifest和审核证据后，通过真实用户材料grant建立；reviewed_by仍来自实际审核，不能把“离线”当自动批准。4096条均为合法原领域正式对象，完整正文／来源不缩限，向量由原生Provider模拟分支逐项产生并走同一接收／apply；不能直接向artifact或索引灌入数组。批准整个确定性集合不声称用户逐字标注4096条相关性，效果标签仍只来自独立已确认查询集。

离线外层控制复用持久授权格式的明确SIMULATED/TEST分支（单条完整日志≤8192字节，不含正文或向量）：用途槽DOCUMENT4096＋QUERY6，费用责任4102 TEST atoms，绑定模板／逐项manifest／6查询／场景资源摘要、配置和本地执行授权引用；该记录不会激活任何真实账户。resume核验这一原冻结离线许可，reserve先于原生attempt，计数不退回，UNKNOWN仍停止后续模拟attempt；启动检查和原键确认0适配器执行，恢复后首次模拟执行也须§4.5原意图及Provider证明。实际供应商发送恒0；原18次真实包既不消费也不借给它。离线场景是资格夹具，不是语义模型效果或供应商协议证明。

**正常计划的完整操作上界：** 计数单位为一次获准的持久写步骤；同一UoW的多个owner只计一次，外层日志的追加另计，不能只看retrieval.control计数。未提交但已登记的操作准入占用仍保留；只读resolve不新增写步骤；需要实际重做的原键本地步骤占用写预算，但不重复增加业务成功数。每个EMBED工作最多16步细分为prepare/bind 2、原Provider登记／attempt／许可证据／完成及handoff至多5（同事务重叠只算一次）、record_result 1、confirm_handoff 1、两页retire_handoff 2、apply或cache_bind 1、record_cleanup 1、必要额外本地确认／收尾写至多3。原生若需要更多步骤须先报告实际分解，不能把16称为已测事实；UNKNOWN不在这3步内获得重发许可。

| 正常写入类别 | 上界算式／操作数 |
| --- | ---: |
| 初始化／主体登记／基础根与受控维护 | 总分配100：CREATE_NEW、initialize_semantic、SELF初始化、一次主体登记、离线许可激活、材料包封存各≤1（共6）；末尾一次原删除命令＋DELETE_LOCAL prepare/apply/record_cleanup共4；其余90只作有限基础确认／故障标记／本地清理写的上界，不追加成员、向量或索引代。凡初始化实际合并同事务只计一次；这些不是100次必定发生的成功写 |
| 固定集 | begin＋4096 add＋seal＋4096 establish＝8194 |
| 4096文档＋6查询的原生工作全生命周期 | 4102×16＝65632；最多4102个模拟attempt，0供应商attempt |
| 外层授权日志 | reserve与结果各一次：4102×2＝8204；未发送也占原槽，不回收 |
| 语义两代完整建立／发布 | 每代begin＋512 append＋512 confirm＋seal＋publish＝1027；两代2054；第二代复用全部原artifact，0新attempt |
| 原语义代退休 | 4096 member＋512 page，共4608行，每步最多8行＝576页，另留最终文件收尾步骤1；合计577；文件unlink/fsync非额外SQL事务但仍纳入实际资源确认 |
| 词法首代建立／发布 | 原index_begin 1＋256页index_claim／index_confirm_page各1＋4096次index_apply_object＋index_publish 1＝4610；16对象页不冒充每页一次对象提交；不触及旧词法保证 |
| 公开查询及票据处置 | 最多128次出票＋128次逐票处置＝256；只读命中／恢复不另发embedding |
| 查询缓存过期／GC | 6次expire＋1次8项以内GC＝7，保留artifact及原账本 |
| pause/resume及剩余控制写 | 20，不重置任何账户、槽、deadline或全局累计数 |
| **总计划** | **89654＜90000普通上限，余346**；额外2000仅原已启动工作的合法异常收尾，不供固定建立、正常索引、预热或新槽使用 |

上述计划仅含两代语义／一代词法、6个已冻结查询的有界重复；修订／删除的更多排列、全部故障注入和其他矩阵案例另用各自从一开始冻结的独立离线案例资源及操作预算，不能在同一4096运行中清计数后重演；也不能将另一案例当本次失败重跑通过。完成90000次或任一空间限额后停止**所有新普通写入**，L模式不天然获得2000收尾额度，仍需证明是原已启动工作的确认／清理。正常必做步骤均已按普通预算计，禁止计划性借用收尾额度。保持dispatch_gap=30秒时4102次模拟执行间隔合计至少123030秒（约34.18小时），该资源／时长明确排除于本轮，不在计时资格中隐藏；不借缩短间隔混用真实小包。

小包原347步只列部分业务操作：补外层36、词法12对象＋claim/confirm/begin/publish共16、6张票据处置6、初始化等保守100后为505＜1000。正常串行确认不重复记成功业务，但其额外写仍占总数。该补计不扩大真实18槽、不挪用原200收尾；小包仍需未来实物大小资格。

**物理推导（仅静态候选，不是峰值实测）：** 完整计入92000普通及收尾步骤的逻辑证据责任`92000×1310720=120586240000 bytes=115000MiB`；初始化2MiB另归基础项。为每种业务副本再独立留以下逻辑预算，不用小文本／复用向量省掉最大责任：

| 组成 | 静态逻辑界或候选物理止损 |
| --- | --- |
| 向量、输入及Provider交接 | 8192 artifact≤192MiB；4102 work及输入≤128.1875MiB；即使4102个10叶handoff全部保留也≤336.4921875MiB；两个语义代成员／页≤72MiB；4096固定member≤32MiB |
| 正式对象／来源／历史等 | 单项按正式对象、来源、链接、历史和覆盖根等合计保守128KiB，4096项≤512MiB；此界未来须按实际完整行核验，不能减少必需行。其余控制、缓存、票据等128MiB；前述业务小计＜2GiB，统一预留2048MiB |
| 词法 | 沿原硬界每对象4096词项×256bytes，两代预留`2×4096×4096×256=8589934592 bytes`（8192MiB）；本操作计划只建一代，但空间同时留两代，合法最大词项可达性仍按原正文，不声称本轮已证明 |
| 基础格式及其他旧元信息 | 1024MiB逻辑余量，含初始化／静态定义；不能据此允许无界新增旧任务 |
| DB物理候选 | `(115000+2048+8192+1024)MiB=126264MiB`逻辑；SQLite页、索引和空闲页暂按2倍预算＝252528MiB（246.609375GiB）＜256GiB。**2倍是待验证预算因子，不是SQLite承诺**；实际放大更大即不通过／提前止损，不得称静态可证明一定装得下 |
| WAL／备份／临时／其他文件 | 分别32GiB／一份256GiB／8GiB／8GiB；其他含外层日志、普通日志、报告及监控证据，不把它们归零；备份额外副本与WAL独立占用 |
| 目录／卷 | DB256＋WAL32＋备份256＋临时8＋其他8＋index80MiB＝560GiB＋80MiB＜640GiB目录止损；卷空闲始终≥128GiB，建议至少800GiB独占可用卷，640＋128=768GiB，余量容纳文件系统元信息。内存／CPU候选4vCPU、8GiB，2读者／8MiB scratch不变 |

该大档是保留完整回执／审计和最大行责任后得出的保守资源建议，不能由34MiB单索引文件推导小卷足够。没有上述资源或实际放大／操作分解超界则4096资格未完成，保留已完成小档证据；不得临时删除账本、压缩必要审计、缩小正式对象数或清零计数来宣称通过。精确物理峰值、完整编码、恢复时长及4096扫描期限均待获准后在Docker Linux实际核验；本轮没有运行编码器、项目或容器。

### 6.4 只读观察

沿原观察端口增加封闭`{v:1,space_id,generation_id?,captured_seq?,material_seq?,published_seq?,first_uncovered_seq?,pending_count?,work_counts,cache_counts,pause_reason,cleanup_pending,items:[WorkObservation,0..16],observed_at}`。work_counts恰PREPARED/BOUND/REMOTE_UNKNOWN/RESULT_STORED/APPLIED/SUPERSEDED/KNOWN_FAILED/NOT_SENT/LOCAL_PREPARED/LOCAL_APPLIED/LOCAL_SUPERSEDED/LOCAL_FAILED对应N?；cache_counts恰active/expired/hits/misses对应N?，hits/misses明确仅本进程统计；WorkObservation恰work_id/kind/state/request_ref?/error/cleanup_pending，DELETE_LOCAL观察的request_ref固定null（只读投影，不在工作行新增该字段）。未知为null，不伪造0。原Provider观察提供原生usage／预算，不能由此聚合推断账单或发送许可。无向量、正文、跨scope对象或审计内容；2并发、2秒、只读，不新增隐含写命令。

## 7. 独立真实试验授权及外部前置

### 7.1 固定材料与调用分配（调用已批准，材料委托审核）

用户已批准在一个全新Docker Linux实例／数据库／目录／授权包内开展12次DOCUMENT及6次显式QUERY预热，最多18个attempt，无备用、无自动重试，不扩展到LLM／persona或额外探针。2026-09-15用户进一步确认用量已分配，批准全部原用途请求并免除本包金额准入，完整生效边界见§7.2。旧MiniMax UNKNOWN及原费用责任保留；新的Coding embedding包使用独立本地账户／ledger身份，不结清、不借用或改写旧包。无需补交供应商费用池独立性截图；实际账户是否共享供应商额度仍如实记未知，不能伪称已验证。授权不因执行者停点或交接重复申请，但只对应原已激活包；次数消耗见[STATUS](../work/STATUS.md)，重建新库或进入后续阶段不产生新额度。

固定集覆盖同义无词重叠、词法精确、否定／不确定、同名人物不同世界、无相关记忆及修订／删除／权限变化。先由受托监督者审查并冻结正文、来源、主体／世界、对象映射规则和逐查询相关性等级，再取摘要，不将旧生成输出自动视作gold。正式建立复用可信候选、来源校验及原生原子终结端口；若现有试验装配只能合成模型产物，应补明确FIXED_REVIEWED材料入口及证据标签，不能伪造ACTUAL_PROVIDER或绕过业务owner。具体12条正文和6组标签须作为发送前集中包提交监督者；本授权不是具体内容已审核。执行者先生成非秘密JSON包、精确编码与摘要，在补完工程和离线验证的同一停点集中交付。监督者负责来源支持、相关性、否定／不确定、世界／权限及无相关标签审查；普通明确项直接裁定，涉及产品取舍或难以确定的项才按“材料／备选／影响／推荐”表交用户决定。审核身份如实记录为受托监督者，不能写成用户逐项亲自标注、盲审或独立人工质量证明；保留原USER_REVIEWED_FIXED来源枚举，通过review_ref、review_digest及实际reviewed_by追溯用户委托与监督结论，不新增字段。审核前不能固定建库、激活或发送此真实包；审核后相同摘要无需重复请求调用授权。

6次QUERY建议显式预热，以异步完整期限收妥付费向量，再通过公开查询验证缓存和融合；不得隐式预热更多查询。首次DOCUMENT请求同时承担协议验证，无hello／账户探针。预热结果已持久后，原查询重复、启动检查及这些已完成工作的恢复、索引重建和纯权限变化均新增调用0；未登记待办在恢复后调度的首次发送另按§4.5计原槽；需要新语义内容向量的修订放入离线受控矩阵，不挪用预算重试。真实冷查询250ms路径的成功率本建议不宣称验证，冷缺失／迟到／UNKNOWN降级由受控传输验证；如用户要求真实冷查询，应从这6次中明确改配，不新增第19次。

每次外层持久reserve先于原生attempt登记，原请求本地终态及实际清理均确认后至少30秒才准下一次；计量分支的继续条件见§7.2。外层未发送但已消费槽不复用。身份／协议结果错误、UNKNOWN、提交未确认、资源监控失联或实际清理未结束，停止新增发送并合法收尾，失败保留原结果。用户授权不代称协议成功；首个正式请求的真实结果用于完成兼容性验证。

本真实小包的固定集manifest_digest只聚合按ordinal排列的12个memory成员content_digest；人工审核的完整包另包括6个query_id／query_text摘要／相关性标签，review_digest绑定该审核原文。prewarm只能选择新激活包列明的query_id及精确文本，不能从“6次”推导任意6个查询授权。外层授权沿既有持久reserve／结果登记工具：TOKEN_METERED沿SEMANTIC_TRIAL_AUTH_V1，本次用量分支使用§7.2的独立版本。固定包身份、实际代码／配置／协议／材料摘要、用户决定引用、18个用途槽（DOCUMENT12/QUERY6）及原slot递增日志保持；费用准入按实际分支表达，无泛化供应商适配逃生口。外层控制是试验文件，不计作第16张业务表，其原日志完整记录与Provider账本分别核验，槽终态和发送次数不能互相替代。

本包分级标签及计分口径在真实输出前冻结：0为无相关／世界不匹配，1为间接背景，2为部分直接相关，3为直接回答；Recall@8把grade≥1计为相关，nDCG@8使用gain=2^grade−1、discount=log2(rank+1)，rank从1开始。两指标分别对4个非空相关集作宏平均，2个空相关集不进入均值，按独立无相关硬门槛判定；超时或不完整空响应不算通过。相关背景不是命题成立的充分证据，不能以召回标签代替来源支持或推断许可。具体已审核材料、标签及绑定摘要只在[固定审核材料](#material-review)定位，不复制或回写原提案和历史输出。

<a id="material-review"></a>

### 7.1.1 固定审核材料

监督者已按用户委托审查[原提案](/private/tmp/iris-semantic-completion-vci76bqc/pre-send/proposal.canonical.json)及其18份发送文本／来源投影。接受12条事件和记忆正文、6条查询及原世界筛选、4项非SELF主体、SYNTHETIC_FIXTURE／NO_PRESET初始化；不生成persona。全部是合成试验材料，REAL是试验内世界分类，不代表用户现实经历。相对时间原文及null时间范围保留，本包不证明相对日期解析能力。

72项标签中71项保持；仅query:02／member:01由0改1：“咖啡不加糖”是甜味偏好的间接背景，不能独立证明不喜欢甜食。其直接否定证据仍为member:04。最终非零标签如下，未列成员均为0；分级与计分规则只维护于[本节计分规则](#71-固定材料与调用分配调用已批准材料委托审核)。

| 查询 | 最终非零标签 | 独立无相关资格 |
| --- | --- | --- |
| query:00 人力通勤 | member:00＝3 | 否 |
| query:01 燕麦拿铁 | member:01＝3 | 否 |
| query:02 喜欢甜食吗 | member:04＝3，member:01＝1 | 否 |
| query:03 雾港夜航渔船 | member:06＝3，member:07＝1 | 否 |
| query:04 火星氧气储备 | 全部0 | 是 |
| query:05 量子计算机纠错 | 全部0 | 是 |

[正式受托审核决定](/private/tmp/iris-semantic-decision-r_zcpstq/material-review-decision.json)记录完整72项最终标签、唯一修订及审核身份codex_supervisor；无待用户裁定的材料项。原proposal SHA256为`de10ad424d1a9db3f813ffa9baeb2e9761c4dde9f415e48b2dea5eee078ca86d`，仅按决定修改这一标签grade和explanation后的规范JSON预期摘要为`e93241a2235b56b99774cc4bd61d4cfa7948ac71649cd4648369d592a0a7df2f`。原文件不覆写；执行者生成派生包，保留原status字段作为提交历史，实际批准来自独立审核决定与受信任grant，不自签审批。

12条正文content_digest及manifest_digest保持，manifest为`77c4aa17afb0b67fd8e89a13da1a0784c51954eee1c060fc01b4f32e4301b49f`；18份渲染文本和HTTP body字节保持。review_digest改绑上述审核后proposal，派生来源根／来源摘要、外层包及完整请求身份一并重新编码核验。审核决定文件SHA256为`2643f1a824b6454213cabdf5bcef34fea50b373998e5687dc5a6cfb2f4394a81`。精确修订、摘要匹配及其余前置满足后不再申请材料或相同额度批准；实物编码和最终配置绑定由执行者验证，监督未运行项目编码器。

本轮[派生材料包](/private/tmp/iris-semantic-integration-ls2c_fb_/pre-send/material-package.json)已精确落实上述决定，监督逐项核对proposal摘要、18份发送正文及12份来源改绑；无需再次审核材料。包摘要为`0ef0c4dbc7c8b8ffbc50fd9e2270f663138f674079046c2ff57f8476fd03796f`。本材料决定和实际配置／请求绑定分别留证；真实执行版本和结果见[STATUS](../work/STATUS.md)。

此审核只确认材料和标签，不代称账单或真实效果通过；同一材料不重复审核，也不因重新绑定或建立新库产生额外调用额度。

<a id="72-条件预算与发送前四项证据"></a>
<a id="allocated-usage-trial"></a>

### 7.2 已分配用量的真实小包（已批准）

2026-09-15用户明确决定：“不考虑成本，因为已经分配了用量，直接使用，请批准全部真实请求”。本节据此替代本包原5元金额上限及发送前四项证明要求；授权仍恰12 DOCUMENT＋6 QUERY，18个原用途槽，没有备用、自动重试或额外探针。这是本次用户承担已分配用量的试验决定，不是免费调用、供应商账单结清或完整订阅计费支持。

[官方Coding Embedding说明](https://docs.volcengine.com/docs/82379/2279748?lang=zh)说明使用套餐调用额度，并列明Coding base URL、doubao-embedding-vision及对应版本doubao-embedding-vision-251215；[官方团队介绍](https://developer.volcengine.com/articles/7628812787703087110)说明记忆／上下文检索用途。这些资料不证明本账户余额、精确扣减、独立费用池或全部操作字段。该差别必须如实记录，但不再要求账户截图、扣额公式、完整输入金额责任、公开费率或自建宿主书面资格后才能执行本已授权包；不伪装其他客户端，也不绕过服务实际认证、权限或限额拒绝。

#### 配置与静态版本

本次批准新增完整USAGE_ONLY_TRIAL分支，连续完成必要实现、局部验证及真实执行，无需再次申请方案或实现授权。124键、六域、15张新增表、26条新增命令及1条初始化替换的数量不变；只扩充本组合的封闭值／格式，旧真实计费、文本usage-only及OFFLINE_CAPACITY均保持原值和解释。不是把真实账户填入模拟分支，也不允许任意extra。

| 载体 | 本次封闭定义及语义 |
| --- | --- |
| provider.accounts唯一账户 | 恰`{account_id:ID,window_id:ID,currency:CNY,max_in_flight:1,attempt_limit:18,cost_limit_atoms:null,atom_scale:1000000,billing_mode:USAGE_ONLY_TRIAL,price:null,quota:null,evidence_ref:ID}`。window_id是本地试验窗口，不冒充供应商结算周期；evidence_ref绑定本次用户已分配用量决定。null明确表示不作货币／供应商扣额准入，不能填0费率声称零费用 |
| 两个provider.profiles | 字段全集沿§6.1的新embedding profile，只将billing_mode固定USAGE_ONLY_TRIAL、max_input_units固定null，其余保持；null表示未声明token责任上界，8192／512 UTF-8字节与完整请求65536字节等实际输入界仍逐项执行，不能把字节或项数当token。两个profile仍指同一账户／模型／1024维空间并分别限定用途 |
| provider.embedding_transport | 字段全集沿§6.1真实传输，v固定2，仅expected_reported_models允许1..2项，取自精确集合`doubao-embedding-vision`、`doubao-embedding-vision-251215`，无重复／模糊匹配；实际选定集合进入配置摘要。协议名、端点及完整请求字节仍为§3.2，其他字段及上限保持。server_evidence_ref绑定上述兼容资料与本次直接验证决定，不声称已获逐字段服务端确认 |
| 新原生存储声明 | 用量分支独立使用Provider请求格式／指纹及Provider命令版本4；通用持久化命令证据／回执编码仍按原1／2格式解释。受影响旧6账本根、usage及必要审计／回执声明按本节封闭替代；旧版本3及其hash保持，不在原库原位迁移。新格式必须在空新库初始化，旧格式双向拒绝混装；未受影响的handoff叶、artifact、索引等格式保持 |
| 外层可信授权 | 新SEMANTIC_TRIAL_AUTH_V2沿V1绑定字段，新增且固定`verification_mode:USER_ALLOCATED_USAGE_TRIAL`。account_evidence_ref绑定本次用户决定，input_evidence_ref绑定已审18文本／本地字节限额核验，server_evidence_ref绑定公开依据及尚待首个正式请求验证的操作。其余原包／代码／配置／协议／SDK／材料／审核摘要、期限与18槽绑定保持；本地journal头使用新版本、旧日志不覆写。不能把决定来源写成供应商已证实或执行者自签材料 |

账号、凭据引用仍由受控装配提供，秘密不写入配置快照／日志／镜像层。执行者可对新鲜的非秘密派生配置、版本和授权封套作必要编码与绑定，但12记忆、6查询、来源、世界、72标签及18份发送正文不变。只改变计量或身份名单不授权重渲染正文、切换模型／向量空间或增加调用。

#### 原生用量、责任及继续条件

新增embedding usage格式版本2仅用于USAGE_ONLY_TRIAL，沿旧固定字段全集，修改如下：billing_mode固定USAGE_ONLY_TRIAL；coverage允许COMPLETE/PARTIAL/UNAVAILABLE；known_cost_atoms、estimated_cost_atoms、reported_cost_atoms、price_revision固定null，cost_complete固定false；每个原input费用项的price_numerator、price_denominator、cost_atoms固定null；known_subtotal_atoms、held_atoms固定0，仅表示没有本地货币计提，不表示供应商费用为0。quota_known固定null、quota_held固定0，分别表示未获得供应商扣额和没有本地供应商单位预留；本地attempt_count及18槽计数独立、持久、递增。

source仍用PROVIDER_REPORTED/UNAVAILABLE；fields和raw_usage只保存可核实的原封闭字段及明确null，不把input_items=1或embedding_dimensions=1024当供应商扣额。新增observation_reason恰OK/MISSING/INVALID/UNSUPPORTED_FIELDS：prompt_tokens和total_tokens均为合法非负整数且相等时才可valid=true、coverage=COMPLETE、reason=OK；仅部分可核验时保留实际字段并标PARTIAL/valid=false，计量完全缺失或非法则保留可证明的字段与UNAVAILABLE/valid=false，不能用补0修复关系；MISSING指usage内没有可用计量值，不新增省略响应根字段的协议分支。未知usage字段的原文留在有界原始响应中，标UNSUPPORTED_FIELDS，不擅自赋予扣额含义；保留已明确的token观察也不表示费用可计算。所有整数仍受原63位限额。响应JSON语法、根／data／模型身份／向量的严格校验保持，不因免金额准入放宽。

Provider四类事实继续分别表达：有效结果及本地提交；远程结果已知／UNKNOWN；usage与供应商扣额完整性；实际资源清理。有效向量已按原生端口提交并完成清理时，即使usage部分／不可用或费用未知，也允许本包按原18槽和30秒间隔继续；保留观察原因和原始响应，不把计量缺失改写成结果未知或已知零费用。终态NOT_SENT仍须原证明，逻辑无发送不生成供应商用量；REMOTE_UNKNOWN即使held_atoms=0、quota_held=0仍必须独立阻断后续新发送，不能靠金额为零放行。预算根／reservation／cost_items及审计的金额和quota字段按上述null／0语义一致表达，必要声明增加同一封闭billing_mode及usage版本判别；不引入额外业务owner、表或命令。

TOKEN_METERED旧分支仍必须完整费用责任准入、预留及结算；本次用量分支不能settle、释放或复用旧MiniMax UNKNOWN／旧按量账户责任。新的本地试验账户身份隔离可以由装配核验，供应商费用池独立性只记事实已知或未知，不再作为本包前置。

#### 一次连续执行及提前停止

按顺序连续完成：本节适配及关联回归→最终完整配置／原生18请求绑定→首个正式DOCUMENT→余下11 DOCUMENT与6 QUERY预热→公开混合查询／固定标签计分→新进程原键及产物／索引／缓存恢复。首个DOCUMENT就是正常材料请求兼协议验证，不再因缺书面逐字段证明暂停，也不先发探针；仅成功、原生提交和清理满足后进入余下原槽。沿原生Provider执行，保持TLS、限时、有界请求／响应、单并发及至少30秒间隔，不改为临时curl或直接HTTP旁路。

服务实际拒绝认证／权限／额度，协议或向量无效、返回身份错配、UNKNOWN、本地未确认、实际清理未结束、磁盘预留不足或监控失联时停止新增发送，完成合法本地收尾及独立工作后集中交回；不自动补槽、换key／端点、删字段重发或绕过服务限制。单纯缺费率、账单、账户截图、token金额上界、usage／扣额不完整或5元估算不再触发停止。若18次均成功，继续完成全部真实闭环与阶段材料，不停在“已适配／第一条成功”。

验收新增必查：完整配置及静态声明实物容量、旧格式拒绝混装；正常／部分／非法usage与有效向量分别表达；0货币预留下UNKNOWN仍停发；18槽和原键跨进程不重置；外层槽、原生登记、实际发送、usage／费用未知及实际清理逐项一致。真实评价判据仍为§8，失败或未完成不得靠阈值／标签调整变通过；本次不验证完整订阅结算、最大输入token资格或供应商账单。

<a id="semantic-acceptance"></a>

## 8. Docker Linux单端验收矩阵（已批准判据，结果分别核验）

通用环境、检查策略和类型要求只维护于[代码规范](../CODING_STANDARDS.md#validation-environment)。上一轮只读核实Docker client/server为29.7.2，已有镜像`sha256:e1196578279b9386460f3873e3f8cef90a22a84dbbc20e5ecd91d0ca88b9cf4f`为linux/arm64，未启动容器或执行项目。历史Python3.12.14及Linux SQLite3.53.4不作为本轮新运行证明；实际实施启动时核对解释器、SQLite、锁定依赖、CA/TLS、非root、真实持久卷、目录独占、可用空间和资源监控。

| 资格 | 固定触发／截点 | 未来断言（尚未执行） |
| --- | --- | --- |
| 封闭声明与事务 | 15表字段全集、26命令／1替换、124键，额外字段／错误null／超界，固定建立每个COMMIT截点 | 完整Schema、writer、targets、审计及回执一致；每次fixed_establish的一项来源／对象／计数／回执全无或全有，集合可持久部分完成；5／6项恢复及最后一项条件按§4.3.1断言，无学习或供应商调用；static推导与实物编码分列 |
| DELETE本地分支 | 原删除COMMIT→prepare→apply的前后中断，伪删除NotFound、混入intent/artifact、并发更晚修订 | 仅LOCAL_PREPARED→LOCAL_APPLIED；同事务DELETE ack／清gap／水位，原键重开无重复写；无输入叶、artifact、Provider查找／request／attempt／槽，发送恒0；普通APPLIED缺artifact仍拒绝 |
| 连续覆盖 | §4.2 A11/B12/A13及删除14；两种r3/r4事务顺序 | first不前移，旧结果不清新gap，material分别10→11→13→14，published仅新代原子切换推进；旧UNKNOWN责任独立保留 |
| 启动检查 | 各种未登记、已登记、UNKNOWN、已完成及本地未确认状态 | open_existing、lookup、resolve、观察总发送0；未完成检查不得自动调度，不以NotFound或lease证明可发 |
| 恢复后首次调度 | D03已reserve未登记，原意图有效／过期／摘要错／终态slot，两个并发absence seal消费者 | 仅可靠REGISTRATION_ABSENT且原包／门控／期限有效者可首次发送1次；槽仍D03，另一者查原键；其余0，不重新reserve或退还计数 |
| 已登记未发送外观 | PREPARED登记COMMIT后强杀，Adapter尚未见请求；0attempt准入终态 | 前者按原Provider恢复UNKNOWN、held保留且0发送；后者保持终态0发送，不能复活或改键重试 |
| 付费结果／本地确认 | handoff根与各叶同事务、接收artifact、完成确认及分片退休中断 | 已持久结果各路径新增发送0；原请求／attempt／费用／摘要不变，UNKNOWN不重放；本地未确认与真实清理分别报告 |
| 文件布局与发布 | N=0/1/8/9/4096、错误ID补齐／endian／尾随字节／摘要；seal后publish前退出、旧读者在途 | 精确8384记录、512页上界；READY不等PUBLISHED；原键确认／付费artifact重建0发送，旧文件不提前释放 |
| 无相关资格 | 至少2个预先人工无相关文本样本；§5.2 .69/.50、.70/0、.30/.80边界；降级同样本 | 完整无相关每例0对象、0错误非空查询、recall_id=null、NO_MATCH；超时／UNKNOWN／不完整不能当空通过；合成门槛与真实模型结果分列 |
| 融合及闭合响应 | 显式ID、纯结构、语义无词重叠、词法降级、权限撤销、require_complete | 先准入后RRF；显式ID仍权限过滤；version2精确actual_mode／coverage／reason，strict拒绝降级；旧version1及原键摘要解释不变 |
| 期限与资源 | 250ms冷查询迟到、总1000ms耗尽、bind后跨进程期限、暂停后resume | 不续期／换worker；slot未批不发送；实际清理未结束不准下一次；缓存命中／过期产物复用不新调用 |
| 固定集效果 | 12人工记忆／6查询，至少2无相关，其余非空gold；全部词法和混合结果 | 非空gold macro Recall@8≥0.90、nDCG@8≥0.85；无相关依独立硬门槛，权限泄漏0；任何失败不删样本、不事后改标签／阈值 |
| 容量与观察 | 小包1000＋200／8GiB；另以§6.3.1 OFFLINE_CAPACITY完整配置建立4096对象、两代、6缓存，正常计划89654／上限90000、收尾2000 | 逐项原键及外层／原生累计不重置；实际完整向量校验拒绝混档；DB／WAL／备份／全目录／卷空闲联合测量，2倍放大仅候选；超界暂停、不借收尾做正常造数；4096完整扫描及恢复实际结果分列，0供应商请求，未完成不得宣称最大资格通过 |
| 实际18次闭环 | §7.2用量分支及原生绑定完成后，12 DOCUMENT＋6显式QUERY预热 | 原生attempt／usage／责任和外层槽逐次核对；失败全留，已完成路径恢复0发送；未执行的首次调度单独计，不把启动0发送外推为所有后续调度0发送 |

相关性规则及阈值已批准，具体固定材料按§7由受托监督者在输出前审核，与旧学习质量评分无替代关系。本固定包的[质量结果及用户不阻塞推进决定](../work/DEFERRED_ISSUES.md#semantic-retrieval-quality)独立维护，不将原FAIL改写成PASS，也不豁免工程、权限和恢复保证。上述矩阵列出验收断言，不代表已执行或通过；实际结果与未验证项见CURRENT_TASK。800GiB完整资格排除于本轮执行，不能因此冒称最大资格通过。

## 9. 集中批准状态与停止点

| 决定组 | 批准范围 | 保留的前置／限制 |
| --- | --- | --- |
| 供应商协议与账户 | 独立dense文本、1024维、batch1及§7.2用量分支／直接验证 | 首个正式DOCUMENT验证实际操作；未知账单／额度仍如实表达，不试探替代端点 |
| 事务及持久产物 | 15表／26新命令＋1初始化替换，gap／ack／发布、handoff与artifact、DELETE_LOCAL、固定集逐项原子及部分恢复 | fixed_establish按§4.4仅cognition、memory两个业务writer与必要审计；无新建旧正文历史或media占位，旧库／UNKNOWN不改 |
| 索引、配置与容量 | 124键、40960结果、8384记录、静态4MiB；小档8GiB及1000＋200；两档封闭定义与校验 | 800GiB方案不批准，不跑4102次／约34小时完整资格；4096实物文件边界与小档完整链路不代称最大负载资格 |
| 查询及观察 | 0.70／0.60准入、等权RRF、version2、缓存128／24h、250ms冷请求、1000ms总期 | 阈值不随真实结果调优；权限、严格完整性和合法降级沿正文，效果独立记录 |
| 固定材料及效果 | 用户委托监督者审核12记忆／6查询／标签及必要主体／世界 | 具体材料未提供前不能说已审核；难以决定的产品语义集中交用户，普通材料不重复用户审批 |
| 真实调用 | 单Docker Linux、独立新包原18次、已分配用量、免货币准入，无备用或自动重试 | 适配和绑定后直接连续执行§7.2；不重批材料或原槽，不改写旧UNKNOWN，实际失败沿停止规则 |

完整工程、真实执行、效果及恢复证据在整阶段停点交监督者集中核对；具体材料已审核，执行者完成§7.2适配及绑定后直接在持续有效的授权内完成真实小包。真实失败、核心未完成和最大资格未验证分别报告，不互相抵销。实施进度及当前阻塞只维护于[CURRENT_TASK](../work/CURRENT_TASK.md)，后续交付顺序见[剩余路线](implementation-options.md#source-line-1202)；执行者不修改文档，不自动提交或启动下一阶段。
