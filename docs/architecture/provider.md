# Provider能力、调用控制、计量与安全

日常认知的图片协议、有限生成／工具请求、混合能力账本和材料租约增量，统一见[集中契约](daily-cognition-and-image-learning.md#6-持久记录事务与恢复)；真实调用包独立授权。

> 本文件是本主题的现行正文，在此唯一维护。既有要求、已批准契约、建议和待批准事项保持各自状态；迁移不新增产品决定或实现授权。文档关系见[总入口](../INDEX.md)。

适用主题与局部定义：Provider是本系统和受支持扩展的唯一模型出口。一次逻辑请求可包含零次或多次有限网络尝试；profile配置真相在统一配置模块，费用和尝试执行账本在Provider。

设计／审核参考：[冻结原始文档](../reference/companion_memory_module_design_provider_logging_config.md)。仅供追溯，不作为现行约束。

按关联工作联合阅读：[Provider模块契约](../modules/provider.md)；[上下文与成本](context-and-cost.md)；[发送与结算事务](persistence-and-transactions.md#t10)；[统一配置](configuration.md)；[参考标记](references.md)。

返回[文档总入口](../INDEX.md)；实际进度见[工作状态](../work/STATUS.md)。

<a id="section-08"></a>

<a id="source-line-536"></a>

## 8. 模型能力接口与协议适配

<a id="source-line-538"></a>

### 8.1 能力接口，不是三个通用URL

建议先定义四类端口：

| 端口 | 输入 | 输出 |
| --- | --- | --- |
| `GenerationPort` | 规范化消息/内容块、工具定义、输出schema、模型profile、预算、deadline | 规范化文本/工具调用/结构结果、停止原因、拒绝、用量、请求标识 |
| `EmbeddingPort` | 一批文本、模型profile、维度/用途、deadline | 对齐输入顺序的向量、模型空间信息、用量和状态 |
| `RerankPort` | query、受限候选列表、top_n、模型profile、deadline | 候选原ID及相关性分数/顺序，不返回虚构新记忆 |
| `MediaUnderstandingPort` | 已授权的blob或内容块、模态、处理任务、可选外部结果 | 内容理解、处理来源、版本与成功/拒绝/不支持状态 |

生成侧分别实现OpenAI Chat Completions兼容、OpenAI Responses兼容、Anthropic Messages兼容。旧的纯文本completion可作为受限的可选适配器，不与聊天/工具/多模态能力混为一谈。Chat Completions与Responses在输入、工具定义和结果结构等方面不同，不能只是替换路径。[S07](references.md#s07)

Anthropic Messages是独立消息协议；其官方文档也明确Anthropic自身不提供embedding模型，因此不能要求一个Anthropic配置天然同时满足向量生成。[S08](references.md#s08)[S09](references.md#s09)

rerank单独按供应商协议适配，例如query与documents输入、排序结果输出。它不是把候选拼进任意completion接口就能声明完成的同一个能力。[S10](references.md#s10)

<a id="source-line-555"></a>

### 8.2 ModelProfile与能力声明

每个profile建议记录：协议类型、base URL、密钥引用、模型ID、请求参数、输入模态、结构化输出/工具能力、上下文预算、输出上限、超时与有限尝试策略，以及embedding维度/空间或rerank候选上限。

业务角色如`learning`、`dream`、`persona`、`vision`、`embedding`、`rerank`映射到profile；多个角色可共用一个profile，不强制配置多个供应商。映射改变时生成配置版本，已冻结运行仍记录实际使用版本。

能力不支持时返回`UNSUPPORTED_CAPABILITY`，不静默丢掉工具定义、图像或输出schema。某个“兼容接口”并不保证所有具体模型能力相同。

结构化输出能减少格式不一致，但仍需本地检查来源、数值与权限。Anthropic等协议提供自己的结构化输出配置方式，适配器将统一schema映射到支持的形式；不支持时采用明确降级策略，而不是假装得到相同保证。[S11](references.md#s11)

<a id="source-line-565"></a>

### 8.3 规范化错误与重试归属

建议错误分为：显式敏感拒绝、其他拒绝、普通超时/限流/网络错误、认证错误、模型能力不支持、结果格式错误、本地持久化错误、预算不足和运行模式拒绝。

只有有证据表明“敏感信息导致本轮无法学习”的拒绝信号进入敏感终态。其他拒绝无法判因时不能直接清空历史缓冲。媒体拒绝保持局部占位，不升级成整轮拒学。

有限网络尝试统一由[M13](../modules/provider.md#contract)底层客户端负责，避免SDK、网关、agent框架和scheduler各重试多次而乘法放大。每次调用有一个整体deadline与尝试账本；在线召回deadline不因底层重试重新计时。预算不足时不要先消费批次再假报普通失败：保留尚未启动的工作并显示预算暂停；多步运行中耗尽预算时持久化检查点，不把准入拒绝当作provider已经调用失败。

本地事务恢复由[I01](ownership.md#i01)负责，不调用provider。已经普通失败或敏感终结的批次，[M02](../modules/runtime.md#contract)不得重新调度。

<a id="source-line-575"></a>

### 8.4 唯一入口、请求归因与执行流水线

所有模型请求，包括学习、梦境、persona提炼、媒体理解、记忆embedding、查询embedding、rerank、目标相似判断及Web测试，都通过[M13](../modules/provider.md#contract)。只读统计查询不发模型请求。远程token计数、模型发现与诊断作为独立`operation`记录，不冒充生成调用，也不默认额外运行一次远程计数来控制每次请求。

每次能力请求至少携带下列上下文，缺少必要归因由调用入口补齐或拒绝，不能落入无来源的“other”并长期漏账：

| 字段 | 用途 |
| --- | --- |
| `provider_request_id / operation_key` | 唯一逻辑请求及本地幂等；相同调用恢复不重复新增逻辑次数 |
| `run_id / parent_request_id / trace_id` | 归属学习、梦境、检索或管理测试，支持工具多轮追踪 |
| `caller_module / extension_id / task_role` | 调用模块、可信扩展、learning/dream/persona/media等角色 |
| `entry_id / batch_id / dream_run_id` | 可为空但不能伪造的业务来源；多入口共享工作使用实际来源集合 |
| `capability / wire_protocol / provider_account_ref` | 区分生成/向量/重排/媒体能力、实际协议与共享配额账户，不暴露密钥 |
| `profile_revision / model_id / config_snapshot_id / prompt_revision` | 固定本次语义与参数，用于恢复、问题定位和统计 |
| `absolute_deadline / output_limit / priority / cancellation` | 统一时限、输出预算、在线与后台优先级、取消信号 |
| `input_fingerprint / artifact_refs` | 有作用域的输入指纹与受控产物引用，不将全文和密钥写入普通日志 |

统一流程：

```text
规范化请求并绑定快照
  → 检查M02模式令牌及调用权限
  → 本地Schema、能力与上下文/输出预算校验
  → 检查已持久化结果或由产物所有者报告的精确复用
  → M13配额/预算准入与预留（T10）
  → 受控客户端/适配器发送attempt（事务外）
  → 规范化响应、错误与usage
  → 持久化终态、费用与结果交接（T11）
  → 返回业务模块；诊断摘要经M14输出；统计聚合读取账本
```

[M13](../modules/provider.md#contract)在每次实际发送前核验模式和执行归属；专注期只有合法梦境内部工作可发模型请求。普通输入暂存、被拒绝查询或Web配置测试不能伪装成梦境任务。正常业务层门控与Provider防漏检同时存在，但Provider不自行改变系统模式。

<a id="source-line-608"></a>

### 8.5 逻辑请求、网络尝试与缓存命中分开计数

一次逻辑请求可能零次出站，也可能有多次有限尝试。至少保存`provider_requests`和`provider_attempts`两层；成功、失败、敏感拒绝、其他拒绝、取消、超时、配置/能力拒绝、预算阻止和结果未知要能分别统计。

示例：一轮生成第一次HTTP超时、第二次成功，计为**1个逻辑请求、2个网络尝试、1次逻辑成功**。第一次超时的费用如果无法确认则为未知，不能自动记零。模型HTTP成功但业务变更集校验失败，Provider的传输/响应状态与[M05](../modules/cognition.md#contract)的业务失败分开展示。

本地embedding缓存或[M04](../modules/media.md#contract)的已有理解复用，可以报告`LOCAL_REUSE`，逻辑请求/复用次数增加，但`remote_attempts=0`，本次新增远程费用为0；历史生成该产物的费用不重复记入。外部提供理解记为`EXTERNAL_RESULT_REUSED`，本服务成本为0，不声称外部没有花费。

同一次媒体能力底层使用生成协议时，不能外层记一次“媒体费用”、内层再记一次“LLM费用”。真实远程attempt只记一次，允许按能力和底层协议分别切片。父run显示子attempt合计，不与子项重复相加；批量embedding同样是一个HTTP attempt及多个输入项，不按向量条数伪造请求次数。

<a id="source-line-618"></a>

### 8.6 完整统计信息与统一口径

| 统计组 | 必须支持的字段或指标 |
| --- | --- |
| 量与结果 | 逻辑请求数、发送次数、批量输入项数、成功/失败/拒绝/取消/超时/未知、准入阻止、正在排队/执行数 |
| 用量 | 输入/输出token、输入缓存读取/写入明细、reasoning等输出明细、embedding文本数与维度、rerank查询/候选/实际计费单位、媒体张数/时长/字节等已知量 |
| 时延 | 配额排队、每attempt网络时间、首个输出时间（适用时）、完整输出时间、本地解析/记账时间、总调用时间；分开p50/p95/p99 |
| 成本 | 按attempt的价格版本、币种、计价单位、估算费用、供应商明确报告值、未知用量和未知费用数量、预算已使用/预留/可用 |
| 复用 | 本地结果复用率、外部理解复用率、provider输入缓存token占比；三种缓存不得混为一项 |
| 健康 | 账户/profile/模型维度的错误类型、限流、熔断/暂停、连接健康、重试增加量、统计覆盖与最近更新时间 |
| 业务归因 | 按时间、模块、角色、入口、模型profile及实际模型汇总；可下钻run→request→attempt及日志关联ID |

不同统计不能无条件加总。首token时间对非流式或某些向量请求可为空；批量文档数不是token数；百分位按原始样本或可合并直方图计算，不能平均各窗口p95。高基数request/entry等下钻字段留在账本或日志中，不默认展开为无限数量的监控时间序列。

统计定义需给出分母：例如“远程逻辑成功率”仅对已经实际发送且结果已终结的逻辑请求计算；本地复用、准入拒绝、在途和未知数量单列。Web同时显示样本数、时间范围、排除项和数据覆盖，不能通过忽略未知项制造100%成功率。

<a id="source-line-634"></a>

### 8.7 Usage规范化与费用计算

适配器同时保存脱敏的原始`usage`和规范化结果，并标明字段来源为`PROVIDER_REPORTED`、`LOCALLY_ESTIMATED`或`UNAVAILABLE`。缺失是null/unknown，不是零。

跨协议必须明确字段关系：OpenAI响应中的cached input是输入总量的明细，reasoning通常是输出明细，不能在总量之外再加一次；Anthropic的普通输入、cache creation和cache read按其协议定义组合，不能把一个供应商的字段名称直接套用到另一个供应商。[S14](references.md#s14)[S13](references.md#s13)

建议规范化为输入总量、非缓存输入、缓存读/写及其适用子类、输出总量和输出子类，并保存“子集/互斥/不可分解”的描述。无法可靠拆解时只保留已知总量，不编造组成。流式响应中的累计usage更新同一attempt最终值或计算真实增量，不逐块重复累加累计总数；取消和断流可能只有部分用量或完全未知。

费用按非重叠计费项计算：

```text
estimated_cost = Σ(该计费项的已知数量 / 单价单位数量 × 对应版本单价)
```

计费项可为token、缓存token、请求、重排搜索单位、图像或音频时长等；按具体协议与配置价目表解释。Cohere的rerank响应有自身的billed_units，不能假定所有重排都按completion token计费。[S10](references.md#s10)

使用十进制定点金额或等价精确表示，币种分别汇总；没有配置和版本化汇率不得把不同币种直接相加。未知价目、失败请求可能计费、取消后远程仍在执行、供应商服务端未暴露的尝试等均明确标未知。已知部分可给小计，同时显示覆盖率，不能把小计称为完整账单。费用估算与供应商最终账单严格分开。

调用绑定价格版本，后续调价不静默改写历史。可显式进行历史重估并保留重估版本，但原始用量与当时估算不覆盖。人工配置价格不宣称自动取得供应商实时账单。

<a id="source-line-654"></a>

### 8.8 配额、预算与成本保护

[M13](../modules/provider.md#contract)统一执行实例、供应商账户、profile、任务角色和单run的并发/速率/token/费用预算。不同profile共用同一账户时计入共同限额，不能靠建立多个profile绕过配额。[M02](../modules/runtime.md#contract)负责工作公平调度，[M13](../modules/provider.md#contract)负责真正出站前的准入；业务模块不得另起客户端逃避限制。

发送前用本地估算及输出上限预留预算，结束后按已知用量结算，重试尝试也消耗相应预算。并发预留以短事务或等价原子账本完成；重启恢复预留状态。未知结果保留未知责任或保守预留，不自动释放为“零费用”。统计聚合可异步，预算准入不能依赖滞后的报表。

预算控制是本地策略，不是供应商账单绝对上限保证；隐藏计费项和未知结果可能产生偏差。无可靠上界的价格配置必须显示风险。在线请求配额等待计入绝对deadline，超时按原查询降级；后台预算不足显示`PAUSED_BUDGET`，不伪造provider失败来丢弃批次。

默认只有一个底层有限尝试机制，SDK内建自动重试需关闭或统一由该层配置。可配置的可重试错误及总次数不能突破单次deadline/总预算；敏感拒绝不换供应商绕过，认证/参数错误不进行盲目重复。若允许普通故障备用路由，也必须显式配置、全部尝试统一记账，且embedding不得跨不兼容空间自动切换。

<a id="source-line-664"></a>

### 8.9 持久化、统计恢复与故障

请求准入/attempt状态先提交，再进行远程调用。结果和usage尽快持久化；在同库事务可行时与业务暂存结果交接一起保存，避免“已返回模型结果但没有恢复材料”。聚合表通过账本ID/修订和checkpoint幂等更新，普通日志轮转不影响账本。

进程可能在发送、供应商完成和本地落盘之间中断；本地状态不能证明远程执行与收费恰好一次。遗留在途attempt进入`REMOTE_RESULT_UNKNOWN`，优先恢复已有结果，无法确认时依原异常契约显式处理。账本必须包含这个缺口，而不是对未知费用补零。

Provider账本存储故障时停止开启新的付费尝试并报告系统故障；已经发出的请求可能形成未知结果，不将该故障归类为敏感拒学。仅运行日志sink故障不自动停止模型业务，只要调用账本和必须的业务审计仍可提交。

详细请求/usage记录、聚合和原始返回各有保留策略。详细记录清理后仍要保留需要的累计值、统计覆盖区间和未决未知记录；超过保留期不承诺按任意新维度完整重建历史。未知结果和仍被恢复任务引用的产物不按普通到期清理。

<a id="source-line-674"></a>

### 8.10 配置与安全边界

[M13](../modules/provider.md#contract)通过[M15](../modules/configuration.md#contract)取得模型profile、业务角色映射、价格、配额和重试政策。正在运行的逻辑请求固定版本，不在第二次尝试时偷偷切到刚编辑的新模型。新请求在正常生效边界使用新版本；旧客户端待引用释放后关闭。

密钥只以版本化secret引用存在于配置和账本，通过受保护环境/文件或加密秘密存储解析；不进prompt、普通日志、Web回显、配置差异或导出。必要的凭据撤销/禁用由发送前的安全准入检查阻止后续尝试，不能因为旧任务固定快照就继续使用已被禁用的凭据。该检查不代表专注期允许Web配置写入；模式权限仍优先。

模型更换按能力语义处理：生成模型仅影响后续相应任务；embedding空间变更由[M15](../modules/configuration.md#contract)发出迁移计划、[M08](../modules/retrieval.md#contract)构建独立空间，再按覆盖策略切换，不把新查询向量直接用于旧索引。密钥、价格或网络参数的变动不能触发全库重新embedding。


图示／代码框中的文档标记定位：[M02](../modules/runtime.md#contract)、[M13](../modules/provider.md#contract)、[M14](../modules/logging.md#contract)、[T10](persistence-and-transactions.md#t10)、[T11](persistence-and-transactions.md#t11)。这些标记仅用于本文阅读，不能进入未来实现。

<a id="provider-foundation-contract"></a>

## 9. Provider基础服务与模拟适配器：整体契约

**状态：契约已批准。** 主会话按用户授权审查批准F1–F5及关联配置补充和有界文本衔接；这不是用户亲自验收本阶段，也不表示实现已通过。本节细化本次整体交付，已批准决定集中在[§9.12](#provider-foundation-decisions)。§8及其他模块既有规则、建议和待定事项保持原状态；这里的能力子集不表示完整Provider模块已经实现。授权、实际源码基线和检查结果只记在[CURRENT_TASK](../work/CURRENT_TASK.md)。

交付包含四类规范化能力、可控模拟适配器、真实同库逻辑请求／attempt／usage／费用与预算执行账本、有界调用与恢复、只读查询、结果交接及现有配置／事务／审计／诊断公开端口集成。只有适配器模拟模型行为；持久化、计量、权限和事务确认均走正式服务。所有执行标为SIMULATED，模拟金额不代表真实供应商费用。

真实HTTP、SDK、密钥和计费、模型发现／远程计数／远程probe、生产G2、Linux／Docker、Web、运行模式服务、梦境和业务模块、配置持久版本与热激活、生产完整配额和预算周期均范围外。未支持的端口不提供空成功或默认联网实现；生产配置不能误接模拟装配。未来完整边界仍见[§8](#section-08)、[运行模式](../modules/runtime.md#contract)和[上下文与成本](context-and-cost.md)。

<a id="provider-foundation-ports"></a>

### 9.1 公开端口、输入与能力

以下是语义签名；实现可按Python类型习惯命名，但不得省略能力、状态或有界性。创建与绑定为可信启动代码使用的公开装配端口，业务调用者只得到已经绑定的句柄。所有I/O端口为异步等待接口，不在事件循环执行阻塞适配器或SQLite调用。

| 端口／持有者 | 输入和输出 |
| --- | --- |
| 创建Provider装配／可信启动 | 静态Provider仓储／命令声明由持久化实现承载；装配可在建库前取得这些声明。服务创建无I/O，不自动建库或初始化日志 |
| initialize(snapshot, storage_binding, resources)／可信启动 | 原生快照、同一已READY存储的受限Provider绑定、受控门控／时钟／模拟适配器资源；校验及本地恢复完成才READY，否则REJECTED或RECOVERY_PENDING。不关闭借用的存储和日志 |
| bind_work、bind_observer、bind_result_owner／可信启动 | 静态身份及权限声明生成独立封闭句柄；分别授予模型工作、受限账本观察、指定请求结果恢复。无公开任意角色字符串鉴权、SQL、连接或裸适配器 |
| generate(request)、embed(request)、rerank(request)、understand_media(request)／工作句柄 | §9.2的共同封套＋下表对应负载；返回§9.4的Completed、Pending或Rejected；一个调用最多一个逻辑请求，所有attempt归它 |
| inspect_capabilities()／工作或观察句柄 | 返回当前绑定的本地静态能力声明、profile／账户关联、SIMULATED来源和无版本事实；无远程调用、无计费或逻辑次数新增 |
| get_request(request_id)、query_usage(query)、get_budget_state()／观察句柄 | §9.9的只读有界账本视图；没有模型调用、写入、自动恢复或审计全文 |
| recover_result(request_id)／结果所有者句柄 | 仅返回该所有者授权请求已持久化的规范化结果或明确未就绪／未知；不执行模型，不提供任意产物列举 |
| get_health()、close()／可信启动 | 无I/O内存健康；有界停止准入与结束自有工作，CLOSED或INCOMPLETE不可变报告。迟到清理由健康观察 |

四类请求的最小能力及格式如下。仅支持非流式完整响应，不运行工具、不实现agent循环或业务候选校验；流式、工具定义／调用和任意JSON Schema输出请求以UNSUPPORTED_CAPABILITY拒绝，不能静默忽略。未来扩充须同时定义规范化结构和对应能力声明。

| 能力 | 完整输入负载 | 完整成功负载及约束 |
| --- | --- | --- |
| GENERATION | 非空有界消息序列，每项恰为role（SYSTEM／USER／ASSISTANT）和text；input_units_limit、output_units_limit由请求给出并受profile上限约束 | 文本及stop_reason（STOP／OUTPUT_LIMIT）；不解释其业务意义。消息角色只是模型内容，不能授权调用 |
| EMBEDDING | 非空有界文本序列、purpose（DOCUMENT／QUERY）、dimensions；不得含重复位置省略或自动拆成多个逻辑请求 | 与输入一一对齐的有限float向量；精确维度、space_id、model_id和输入项数。空间由profile显式声明，不能凭同名模型猜测兼容 |
| RERANK | query、非空候选序列（candidate_id、text）、top_n；候选ID在请求内唯一，1≤top_n≤候选数 | 恰含top_n个原候选ID及有限float分数，按返回排名排列；无新增ID、重复ID、越界索引。分数相等保留适配器给出的稳定次序 |
| MEDIA_UNDERSTANDING | 授权媒体句柄、modality（IMAGE／AUDIO／VIDEO）、task（DESCRIBE／TRANSCRIBE）；本次只接受单项合成bytes及明确模态／任务，不接URL、文件路径或裸blob ID | 规范化text、modality、task、source=SIMULATED和实际profile／model归因；拒绝只影响本次媒体能力结果，不形成学习敏感终态 |

媒体句柄由可信装配校验来源和调用方后签发，绑定调用scope、所有者、artifact_id、精确不可变bytes及模态；Provider在使用前检查签发身份／绑定。测试可持有显式非秘密合成媒体；不因此实现媒体存储、解码或生产blob授权。Provider不落盘输入媒体，也不取得blob删除权。四类输入统一接受精确原生记录及tuple／list／str／int等指定载体，深度、条数、UTF-8字节和输出容量受[配置规格](configuration.md#configuration-provider-definitions)限制；不接惰性迭代器、回调、鸭子类型、子类或未知字段，不调用对象转换钩子。提交期间输入稳定，成功隔离后不留可变引用。

Generation的本地输入计量取所有消息text的UTF-8字节数作为模拟input_units，输出预算单位为模拟token；这不是真实tokenizer估算。Embedding取文本条数，Rerank取候选条数，媒体取输入bytes数。适配器返回的token／媒体明细独立记录，不把这些模拟计价单位混称为真实供应商token。文本或媒体超限整项拒绝，不截断；向量／结果超限为结果格式失败，不向业务返回部分成功。

<a id="provider-foundation-identity"></a>

新增已批准的`WorkPort.lookup_request(operation, original_request)`只读确认端口：operation恰为generate／embed／rerank／understand_media，原请求复用对应工作入口的完整输入及归属校验；deadline与cancellation仅约束本次确认等待，不进入原内容指纹。先核验原生WorkPort、当前归属权限、能力及profile允许集合，再按caller_module／caller_scope／extension_id／operation_key查原记录，并用原记录的execution_evidence核对完整原语义指纹。匹配返回与get_request相同的Found元信息（包括原request_id、状态及交接存在性）；未命中返回NotFound，不登记、不生成request_id、不发送模型、不取得执行所有权，也不授予重放许可。同键不同内容返回Failed(IDEMPOTENCY_CONFLICT, lookup_request, request, CONTENT_MISMATCH)，不放宽旧指纹协议。不向观察句柄或结果所有者句柄开放此能力；结果正文仍经原ResultOwnerPort的明确request_id授权读取。运行层须在首次Provider副作用前持久保留能重建完整原请求的冻结工作及原操作键，确认不依赖任何首次返回已送达。

该增量的固定错误复用§9.4：lookup_request属于“读”；另允许IDEMPOTENCY_CONFLICT／CONTENT_MISMATCH、CANCELLED／CANCEL_REQUESTED和RESOURCE_FAILED／RESOURCE_FAILURE，分别用于原内容冲突、本次等待取消和可信时钟等资源故障。能力、归属及输入失败优先于读库；查不到不转换为提交未发生。增量验收预期（执行证据见[CURRENT_TASK](../work/CURRENT_TASK.md)）：任何返回前退出后原键查回、原键异内容冲突、撤销／跨能力拒绝、未命中零登记零发送及原结果隔离。

### 9.2 身份、配置证据与幂等范围

工作句柄绑定真实的caller_module、可空extension_id、caller_scope、task_role、允许capability／profile集合和结果所有者。caller_module使用现行模块语义名；task_role取LEARNING／DREAM／PERSONA／MEDIA／EMBEDDING／RERANK／GOAL／DIAGNOSTIC。请求不能覆盖这些绑定。run_id必须为已分配的不透明ID；可选parent_request_id、trace_id、entry_ids（去重有序集合）、batch_id、dream_run_id、prompt_revision无值用None／空集合表达，不能捏造业务来源。绑定能力负责检查这些引用属于调用scope；测试来源明确合成。

请求共同字段恰为operation_key、run_id、上述可选归因、profile_id、能力负载、deadline和取消观察令牌。operation_key在首次调用前由调用方持有并跨恢复稳定；新请求的profile_id须在工作能力允许集合与当前角色配置映射的交集中，历史恢复另按下段。deadline由可信单调时钟域表达，取消由原生取消源签发的令牌表达；二者不授予权限。ID精确匹配`[A-Za-z0-9][A-Za-z0-9._:-]{0,127}`，外部原始ID、用户名、URL或秘密先由可信上层映射，不直接透传。Provider生成的request_id、attempt_id、artifact_id及本地命令键在首次使用前稳定，重试不能另造身份。

逻辑唯一键为`database_id + caller_scope + caller_module + extension_id（可空）+ operation_key`，由数据库唯一约束实施；请求能力属于语义内容，不用于把同键改能力伪装成新请求。规范化指纹版本固定，Provider对已隔离内容计算类型敏感、确定性编码／SHA-256；覆盖全部归因、绑定角色／所有者、profile选择、实际执行配置证据、能力负载与预算，媒体包含内容摘要及授权来源。排除deadline、取消状态和观察时间；不信任请求自报hash，不将全文保存到通用回执。

首次逻辑登记时保存完整、不可编辑的执行配置证据：实际profile字段、账户ID、模型／空间、能力、预算和有限尝试值、模拟价格字段及SIMULATED来源。它是这次请求如何执行的历史事实，既不能被当作新的配置查询源，也不能被修改或用于重新激活配置。当前EffectiveSnapshot没有持久版本：`configuration_origin=UNVERSIONED_CONFIGURATION`，`config_snapshot_id/profile_revision/price_revision`恰为None；schema_revision、指纹、数据库ID和profile_id都不是这些版本。真实价格版本及生产持久快照仍待其所属配置契约落实。本服务只允许该无版本的模拟装配；生产装配不能借此获得豁免。

同键命中先用原记录绑定的执行证据比较本次语义，不用后来服务配置替换它：相同请求返回原逻辑事实及可恢复结果，标source=EXISTING，不新建request／attempt／费用；不同内容、归因、profile选择或结果所有者返回IDEMPOTENCY_CONFLICT，原记录不变。安全载体和权限先核验，历史比较／读取使用原执行证据及本格式硬上限（请求1048576字节、结果8192字节、消息／文本／候选64项、维度1024），新配置降低输入／维度上限、撤下profile或改变角色配置映射不会使旧结果失效。当前主体访问权被可信授权方真正撤销时仍ACCESS_DENIED，不把配置改变冒充权限撤销。新服务当前配置和原执行证据不同不触发旧请求重跑；旧开放记录也不凭原证据自动发送。知道键或request_id不授予查询结果权限。并发同键只能有一份登记，另一调用查回原事实或报告BUSY／Pending，不换键绕过。

<a id="provider-foundation-gates"></a>

### 9.3 可信模式／权限门控与最小预算

Provider不拥有运行模式、epoch或业务执行租约。可信启动注入门控端口；它校验原生工作能力及真实运行归属，返回短期发送许可。无门控、未恢复、未知模式、过期epoch、已撤销工作、错scope或签发者均关闭准入。`task_role=DREAM`、`dream_run_id`或`role="developer"`本身均不能取得许可。

检查点为初始准入及每次实际适配器调用前。最后一次核验与调用开始须以门控端口的原子“消费一次发送许可”边界协调，不能检查后先等待队列再发送；模式切换若先于许可消费则拒绝，消费后已开始的调用属于在途，后续许可仍须新核验。Provider不决定入梦收尾策略或强行取消既有业务批次。FOCUSED仅允许由门控方证明有效的内部梦境工作，普通学习、积压媒体、查询和DIAGNOSTIC均拒绝；非专注梦境不额外扩大普通业务权限。恢复／故障模式一律不发送。

运行模式服务未实现时，测试专属门控夹具持有私有签发权和可控epoch／许可撤销屏障，覆盖正常、专注、恢复和撤销竞争；正式Provider没有默认放行门控，没有从自由文本构造有效授权的入口。测试证明Provider检查边界，不声称已实现生产模式恢复或Web鉴权。

最小预算采用**每数据库、每账户、每显式预算窗口**的共同账本，窗口ID是测试配置提供的实际业务标识，不是价格版本。窗口不按时钟自动切换；范围内无重置、释放UNKNOWN、人工补账或新窗口激活端口。相同窗口的币种／上限／策略及账户身份在数据库首次初始化记录，重新打开须精确核对；不符拒绝，不能用重启或改profile清零。此策略不影响未来完整的实例／角色／run／周期／速率体系，后者仍未实现。

配置给出账户attempt总额度、费用上限、账户并发及实例并发；同账户不同profile共同消费。窗口计数按**获准登记attempt**计数（保守包括可能未发送者），不冒称确认远程次数。每attempt的保守预留为`R = input_units_bound × input_price_atoms + output_units_bound × output_price_atoms`；单位价来自已冻结模拟配置，非生成output_units_bound为0。输入界由§9.1计价输入确定，生成输出由显式输出上限确定；没有可靠上界或不受支持价格单位不能开启尝试。重试重新检查并预留一次，不复用前次额度。

金额仅用精确整数atoms，固定`10^6 atoms = 1 TEST`，TEST是合成计量单位，不是现实货币或外汇。单值、乘积及累计值须在0至2^63−1范围内，先检查溢出再落库；禁止float金额、隐式舍入和缺失补零。attempt保存`known_cost_atoms`（未知为None）、`known_subtotal_atoms`、`cost_complete`及`held_atoms`：完整已知K时计入K并释放R；部分已知K时计入已知小计且继续持有`max(R−K,0)`，全未知保持R。已知K超过R也照实记账，不截断费用；在同一结算事务将**该账户／窗口**risk_state从CLEAR不可逆置RESERVATION_OVERRUN，所有共用该账户的profile／scope后续尝试均以PAUSED_BUDGET／RESERVATION_OVERRUN拒绝，即使窗口余额充足。该状态重启保留，无人工解除／重置端口，其他账户不受它自动阻止。预算可用值按额度减已知小计减持有责任计算；低于零保留真实赤字，不能用零掩盖超额。并发和预算在同一短写事务重查并原子预留；准入读取权威行，不依赖报表。

预算不足以PAUSED_BUDGET记录逻辑准入阻止、零attempt，门控阻止为MODE_BLOCKED。这不是批次失败／敏感终结，Provider不消费、丢弃或调度业务输入。相同键复查保持原阻止结果；未来上层明确重新申请尚未启动工作时使用新逻辑键并保留来源关联，本服务没有自动重排或重试预算拒绝。实例／账户槽位无空闲即BUSY，不建立无界等待队列。

<a id="provider-foundation-results"></a>

### 9.4 持久状态、结果分支与固定失败协议

请求有phase=OPEN／TERMINAL／REMOTE_RESULT_UNKNOWN，outcome在TERMINAL时恰为SUCCEEDED、FAILED、SENSITIVE_REFUSAL、OTHER_REFUSAL、CANCELLED、TIMED_OUT、UNSUPPORTED_CAPABILITY、CONFIGURATION_REJECTED、PAUSED_BUDGET、MODE_BLOCKED之一；其他phase的outcome为None。状态分别统计，不能把OPEN当失败、UNKNOWN当超时已终结。非法载体或无归因／权限请求在落库前REJECTED，不编造匿名逻辑记录；统计覆盖明确排除这些未登记调用。

attempt按request_id及从1开始的序号唯一，状态为PREPARED、COMPLETED、NOT_SENT、REMOTE_RESULT_UNKNOWN。PREPARED只说明发送登记已提交，不能证明已发送或未发送；COMPLETED含明确结果类别和usage／费用覆盖，NOT_SENT必须由原活跃执行者证明许可未消费且适配器未开始；其余失联／崩溃窗口保守UNKNOWN。已完成attempt不可覆盖，未知责任不因请求业务结果可见而消失。请求状态与attempt是模型侧事实，不替业务模块宣布学习三终态。

| 公共分支 | 唯一证据和含义 |
| --- | --- |
| Completed(record, result_or_none, source) | 请求终态及必要结算／交接已取得本地COMMITTED或可信原结果证据；SUCCEEDED才带规范化成功结果，其他终态只带固定原因。source为NEW／EXISTING，不改原持久记录 |
| Pending(reference, observation, error_or_none) | 有已校验请求身份，但请求开放、远程UNKNOWN或本地提交未确认；observation为IN_PROGRESS／REMOTE_RESULT_UNKNOWN／LOCAL_COMMIT_UNCONFIRMED。reference仅含受限库／请求／操作身份；无模型正文、密钥或原始RecoveryHandle外泄。不表示发送授权或允许业务终结 |
| Rejected(error) | 当前输入、绑定、生命周期、容量、冲突或无法登记等失败；不声称同键历史不存在，不含部分成功结果 |
| Found(value)／NotFound／Failed(error) | 只读端口结果；NotFound仅该次短快照观察，不允许重发。recover_result还可返回Pending；它不能返回未持久结果 |

ProviderError恰含`code、operation、field、reason、cleanup_pending`，深不可变，不包含message、原异常、栈、SQL、路径、输入片段、URL、任意响应或嵌套下层错误。operation为initialize、generate、embed、rerank、understand_media、get_request、lookup_request、query_usage、get_budget_state、recover_result、close之一；inspect_capabilities/get_health无预期I/O失败。field仅state、capability、configuration、request、payload、gate、budget、adapter、ledger、query。签名错误按Python规则拒绝；无法构造安全结果的资源耗尽和进程控制不伪装成功。

| code | 完整reason集合／field及触发 |
| --- | --- |
| INVALID_INPUT | INVALID_SHAPE、INVALID_IDENTIFIER、LIMIT_EXCEEDED：request／payload／query按当前端口；不支持的载体、字段、有限性／条数／字节超限 |
| ACCESS_DENIED | CAPABILITY_MISMATCH：capability；有效签发者、scope、工作／查询／结果所有者或资源绑定不符 |
| INVALID_STATE | NOT_INITIALIZED、ALREADY_INITIALIZED、SERVICE_FAULTED、SERVICE_CLOSED：state；ALREADY_INITIALIZED仅initialize |
| CONFIGURATION_UNSUPPORTED | SNAPSHOT_REQUIRED、DEFINITION_MISMATCH、CAPABILITY_MISSING、VALUE_INVALID、STORED_POLICY_MISMATCH：configuration；最后一项指已存预算／账户策略与当前装配不符 |
| UNSUPPORTED_CAPABILITY | CAPABILITY_NOT_SUPPORTED：capability；PROFILE_NOT_AVAILABLE：configuration；明确不支持能力、负载功能或profile |
| MODE_BLOCKED | GATE_DENIED、GATE_UNAVAILABLE：gate；拒绝与无法取得可靠门控证据分别记录 |
| PAUSED_BUDGET | ATTEMPT_LIMIT、COST_LIMIT、UNBOUNDED_COST、RESERVATION_OVERRUN：budget；窗口attempt额度、费用不足、无法形成可靠模拟上界或账户持久超额风险 |
| RESOURCE_BUSY | ADMISSION_BUSY：state；实例／账户／受控执行槽位占满 |
| IDEMPOTENCY_CONFLICT | CONTENT_MISMATCH：request；同键内容／持久完成内容不符 |
| CANCELLED | CANCEL_REQUESTED：request；确知取消；可能已发送但结果未知的分支仍为Pending |
| TIMEOUT | DEADLINE_EXCEEDED：request；ATTEMPT_TIMEOUT：adapter；总期限或单attempt期限到达 |
| MODEL_REFUSAL | SENSITIVE_INFORMATION、OTHER_REFUSAL：adapter；必须有明确、受支持模拟信号，非自由错误文本推断 |
| ADAPTER_FAILED | TRANSIENT_FAILURE、RATE_LIMITED、AUTHENTICATION_FAILED、INVALID_RESPONSE、ADAPTER_EXCEPTION、RETRY_EXHAUSTED：adapter；固定适配器结果／校验故障，普通异常安全归一化 |
| PERSISTENCE_FAILED | LEDGER_REJECTED、LEDGER_NOT_COMMITTED、LEDGER_UNCONFIRMED、LEDGER_READ_FAILED、LEDGER_INCONSISTENT：ledger或只读query；按下一段证据映射 |
| RESOURCE_FAILED | RESOURCE_INVALID、RESOURCE_FAILURE：capability或state；可信资源／时钟／ID异常；CLOSE_INCOMPLETE：state，仅close |

端口适用集合固定：以下“工作”恰指四个能力调用，“读”恰指get_request／lookup_request／query_usage／get_budget_state／recover_result。INVALID_INPUT用于工作和读；ACCESS_DENIED用于initialize、工作和读；INVALID_STATE用于initialize、工作和读，其中NOT_INITIALIZED／SERVICE_FAULTED／SERVICE_CLOSED按实际生命周期，ALREADY_INITIALIZED仅initialize。CONFIGURATION_UNSUPPORTED仅initialize。UNSUPPORTED_CAPABILITY、MODE_BLOCKED、PAUSED_BUDGET、ADAPTER_FAILED、MODEL_REFUSAL仅工作；IDEMPOTENCY_CONFLICT、CANCELLED用于工作和lookup_request；RESOURCE_BUSY用于工作和读；TIMEOUT用于initialize、工作和读，ATTEMPT_TIMEOUT仅工作。PERSISTENCE_FAILED用于initialize、工作和读，其中LEDGER_NOT_COMMITTED／LEDGER_UNCONFIRMED仅initialize和工作，LEDGER_READ_FAILED／LEDGER_REJECTED／LEDGER_INCONSISTENT三者均可；工作端口内部历史查询失败也用LEDGER_READ_FAILED并归因于该工作operation，不改报不存在或写入失败。RESOURCE_FAILED用于initialize、工作、lookup_request和close，RESOURCE_INVALID仅initialize，CLOSE_INCOMPLETE仅close。持久记录中的失败类别不受查询端口限制，查询只是返回原事实而非产生一次该错误。

已绑定调用顺序固定：精确句柄类型及签发身份 → 服务生命周期 → 当前主体工作／读取权限 → 输入精确载体／ID／安全完整树和**格式硬上限** → 按逻辑键查历史及原证据匹配／冲突 → 仅新请求检查当前profile可用性、角色配置映射、能力和**当前新写限额** → 初始模式及预算 → 登记 → 发送前门控 → 适配器 → 结算／交接 → 事务外诊断。只读调用完成前四项后执行固定查询，不经过新工作准入。initialize没有工作句柄，顺序为资源／storage_binding精确签发身份 → Provider生命周期 → 存储READY → 原生快照 → 必要定义 → 必要能力 → 值 → 持久策略核对／恢复。不得通过错误查看无权限键是否存在。安全树检查先于语义判断，未知字段／非原生对象不遍历其值；同一序列按下标、固定记录按声明顺序报首错。

本次调用首个确定的外层code／reason／field固定；后续存储／清理只能提升Pending证据和cleanup_pending，不能把先前TIMEOUT或拒绝改为虚假的成功。每个attempt独立保存自己的首错，成功重试请求可SUCCEEDED但保留先前attempt失败。只有后续独立查询返回新的观察，已返回对象不变。下层COMMITTED是确认，NOT_COMMITTED映射LEDGER_NOT_COMMITTED且只说明本地无提交；REJECTED映射LEDGER_REJECTED；UNCONFIRMED映射LEDGER_UNCONFIRMED并进入Pending；完整性失败映射LEDGER_INCONSISTENT并FAULTED，其余读取故障LEDGER_READ_FAILED。所有映射保留真实cleanup_pending；若已有更早外层原因则不覆盖它。

只有明确信号SENSITIVE_INFORMATION形成Provider的敏感拒绝；未知拒绝为OTHER_REFUSAL，认证、超时、预算、门控和存储故障均不得归入敏感。媒体敏感拒绝仍是媒体局部结果，只有拥有业务语义的调用模块才能按既有规则判定整轮学习终态。

<a id="provider-foundation-storage"></a>

### 9.5 真实账本、仓储装配与结果所有权

下表是逻辑持久模型，SQL及固定StatementDefinition由持久化实现承载，状态转换和规范化Schema由Provider拥有；所有变更由受限仓储及原OperationPort协调，无Provider自行连接SQLite或直接SQL。一个已绑定数据库内Provider使用固定内部存储scope，业务caller_scope另列并按签发能力过滤；账户预算不按业务scope各建一份，防止跨入口／profile绕过共同额度。调用方无法选择该内部存储scope。

| Provider拥有的记录 | 最小持久内容／约束 |
| --- | --- |
| provider_requests | request_id、逻辑唯一键、规范化格式／指纹版本和值、归因／结果所有者、实际执行配置证据及缺失版本标记、phase／outcome／首错、修订、创建／更新UTC时间；唯一键及状态CHECK |
| provider_attempts | attempt_id、request_id＋ordinal唯一、固定账户／profile／能力／SIMULATED协议、执行者代次、状态／首错、许可消费证据覆盖、各段已知持续时间、规范化usage、模拟原始usage白名单、费用完整性／小计／责任及结果交接引用；外键绑定request |
| provider_cost_items | attempt_id＋固定计费项唯一、模拟价格来源及实际单位价、单位、数量或None、known subtotal／complete、证据修订；同attempt每项只建一行，后续只允许按§9.7补足未知字段，不复制业务父run费用、不混币 |
| provider_budget_windows、provider_reservations | 账户＋窗口唯一、固定策略、额度／已知小计／责任／attempt数、risk_state；每attempt一项预留，修订／状态及K／R；同事务保持账本和预算一致 |
| provider_handoffs | 唯一artifact_id／request_id、owner_binding、结果格式、完整有界规范化payload、内容校验摘要、创建时间；仅持久恢复交接用，不做全文搜索、学习、embedding或历史日志入口 |

request／attempt／预算／交接更新与必要审计及通用回执在**同一SQLite本地事务**提交。账本不是诊断文件或第二库。通用回执只保存稳定request／attempt／交接引用、修订、状态和原确认，不保存结果正文。本次没有异步聚合表：query_usage直接从账本固定查询获得相同短快照汇总，无聚合checkpoint漂移或重复应用；完整生产统计／百分位后续按§8.6实现。

模型原始响应对象仅在有界执行所有者内存中；Provider规范化后只持久化必要交接payload。后续媒体／认知／检索模块是业务结果所有者，Provider暂持可恢复交接产物，不能编辑、发布业务正文、宣称生成记忆或授予agent历史读取。测试用绑定的合成结果所有者恢复并验证相同payload，不创建真实业务模块表。没有正文检索、任意artifact读取、交接删除、保留清理或“已取走即删除”；本次所有交接、未知、原结果和幂等回执随验证数据库保留，不决定永久生产保留期。

现有持久化标量不足以保存文本交接，最小扩展只按[有界文本衔接契约](persistence-and-transactions.md#provider-persistence-bridge)实施。Provider拥有独立有界规范化payload编码：固定字段、版本、确定性UTF-8 JSON、拒绝重复键／未知字段／深度超限；有限float向量和分数用精确float.hex文本保存并按原Schema复原，金额始终整数。执行证据不包含秘密、URL、请求正文；输入正文只在计算指纹与发送时驻留，不写provider_requests。

ProviderSchema为新增的独立模块格式；只能在明确CREATE_NEW的新验证数据库装配，随后以相同完整装配OPEN_EXISTING。缺Provider表的既有库、Schema清单不同或损坏均拒绝，不自动迁移、补表、修改旧库或把旧库误判为空；生产存量迁移超出授权。契约批准不意味着该Schema已落地。

<a id="provider-foundation-transactions"></a>

### 9.6 登记、发送、完成与崩溃窗口

正常顺序为：隔离输入、绑定固定配置证据和逻辑键 → 一次短事务登记request／首attempt／预算预留及必要审计 → 获得COMMITTED且属于本次活跃执行所有者 → 事务外消费新鲜发送许可并调用适配器 → 验证完整结果／usage → 一次短事务提交attempt完成、费用／责任、请求终态、持久交接和必要审计 → 获COMMITTED才返回Completed。可归因的预算／模式／能力阻止用独立登记分支保存零attempt请求。后续attempt也各先登记，不能直接从重试循环跳进适配器。

每个本地命令有固定语义操作种类、版本及稳定键，登记、各attempt准备／完成、请求终结、恢复UNKNOWN各用独立身份。Provider命令前置状态与expected revision在事务内重查，不能把模块返回STAGED当成功。重复完成同键同内容返回原回执，不新增费用或交接；不同内容拒绝且原事实保留。状态变化已发生但回复丢失时从账本／原回执恢复，不能靠当前配置重造完成结果。必要业务引用和费用一起落盘后才交付结果。

| 中断／失败截点 | 恢复与发送责任 |
| --- | --- |
| 登记未获COMMITTED，包括存储UNCONFIRMED | 禁止调用适配器；只做本地结果确认，不能假定失败后重登记或换键。下层NOT_COMMITTED允许在原期限内有限本地重试原命令，仍不发送；每本地命令最多一次显式重试，BUSY不自动重跑 |
| 本次登记COMMITTED，但发送前门控撤销／取消／deadline | 原活跃所有者在确认许可未消费且适配器未开始后，以本地命令标NOT_SENT、释放费用预留、终结相应请求；已登记attempt额度仍计入保守窗口计数 |
| 登记COMMITTED后进程退出，无法证明是否消费许可 | 新实例将PREPARED转REMOTE_RESULT_UNKNOWN，保留预留，不发送；即使模拟适配器记录“未看见调用”，也不能用另进程内存观察证明从未发送 |
| 适配器已开始，返回前超时／取消／失联／普通异常且无法证明完成 | Pending／REMOTE_RESULT_UNKNOWN；尽力持久标未知，账本不可写则FAULTED并保留PREPARED和责任，重启按未知恢复 |
| 已收到明确结果，但完成事务未确认或执行者退出 | 在有界内存所有者仍持有结果时只确认／有限重试**本地完成命令**；结果未落盘且已丢失则UNKNOWN，不能重新调用模型来“恢复” |
| 完成COMMITTED、返回调用方前退出 | 从原账本和交接取回完整相同结果，计量一次，无适配器调用 |
| 请求OPEN，全部已有attempt已明确完成，但进程退出在重试准备前 | 恢复只用已落盘attempt终结为其已知失败／超时／拒绝，不自动新增attempt；不会重新执行失败业务批次 |

已有本地完成回执和完整交接优先于UNKNOWN判断；成功回执引用缺失／payload损坏／预算关系不一致为LEDGER_INCONSISTENT并拒绝就绪或FAULTED，不能自动重建正文、清零费用或改成NotFound。Provider启动恢复按有界分页／短事务扫描未终结项，旧执行者须已结束或可靠隔离；如果底层仍持有资源，保持RECOVERY_PENDING。分页只是限内存，一次initialize总期限不因页数重置；未完成不READY，下一次显式初始化可继续已幂等完成的本地恢复，不发送模型。

未知没有自动重发、人工结清、放弃费用或删除端口；账本和受限观察清楚显示未决责任。未来供应商查询、重新调用或管理终结必须按既有[外部未知限制](persistence-and-transactions.md#source-line-518)另定明确、有限和可计费的操作，本轮不代用户批准这些产品恢复决定。

<a id="provider-foundation-execution"></a>

### 9.7 有限尝试、总deadline、取消与生命周期

max_attempts包括首attempt，只在配置上限内；固定可重试原因仅TRANSIENT_FAILURE和RATE_LIMITED，且适配器明确证明前次已结束、结果类别可重试、费用覆盖完整。认证、敏感／其他拒绝、能力、格式、预算、门控、取消以及远程UNKNOWN均不自动重试，也无供应商备用路由。适配器本身不重试，调用方不得通过重复逻辑键乘法放大尝试。

总deadline取调用方绝对期限与准入时刻＋provider.request_timeout_ms较小者；输入规范化、取得槽位、登记、本地确认、退避、每attempt、结算及返回均计入同一剩余额度。单attempt取总剩余与profile.attempt_timeout_ms较小值；固定retry_delay_ms等待亦计入总期限，不重置在线请求计时。过期前未启动的适配器任务不得迟到启动，不能在结束等待后偷偷继续重试。

底层存储当前每调用有独立配置期限；Provider用自身有界任务管理剩余等待，不修改其总时限或把取消等待当回滚。Provider期限先到时报告Pending；已启动的本地事务由原持久化所有者完成／回滚，Provider不并发复用其连接，也不等待额外完整storage期限后才对外返回。本轮不改旧OperationPort签名或另建全局事务时钟。输入／编码CPU工作须先限制规模，再分段检查单调剩余期限，不能在事件循环进行无界转换。

取消源为原生显式对象，签发只读令牌、取消不可逆；一个调用观察源属于该调用，不能由载体中的任意方法执行取消。取消发生在发送许可消费前按NOT_SENT；消费后只有明确完成证据可终结，否则UNKNOWN。调用方取消asyncio等待只结束其等待，由服务保留已准入工作的有界所有权和恢复记录；不得将CancelledError吞掉后向被取消调用返回假成功，也不能把它当成远程取消证明。

服务生命周期为NEW→READY→CLOSING→CLOSED；初始化失败且自有资源全释放为NEW，持久完整性／未确认写入或无法继续安全结算为FAULTED，停止新attempt。每服务同时最多provider.max_in_flight个逻辑执行槽；每槽最多一个适配器工作器、一个有界结果缓冲、一个受控本地存储任务，无无界executor队列。等待超时／取消不释放仍在运行的工作器槽，不增生替代线程；健康包含in_flight、unknown_observations、cleanup_pending、ledger_faulted及固定原因，计数不溢出或假减到零。

模拟适配器可协作取消，也可按测试控制永久阻塞；线程永久阻塞只承诺调用方有界返回。迟到响应只由原执行所有者处理，禁止再次调用适配器或修改已返回报告：若原未知记录仍允许补全且存储READY，可用同一attempt的专用幂等“补充已知证据”命令保存结果和费用、调整保守责任，并保留ever_unknown=true及原UNKNOWN原因。该命令冻结证据ID／指纹，事务内核对expected evidence revision，仅填补之前未知的数量、金额或结果。原已知字段必须逐值相同，任何相异均CONTENT_MISMATCH且不覆盖旧事实。

补证据按差额原子记账：原小计K0／持有H0，新完整性计算K1／H1；账户小计只增加`K1−K0`，责任只增加`H1−H0`，cost_items同一行在证据修订下补足未知字段，request／attempt／交接／预算／风险／审计一并提交。非负计价条目的补足不能使已确认小计减少；重复同证据返回原回执、不重加K1、重复建费用项或释放两次责任。完整已知记录不接其他证据覆盖；不同内容同证据键或已知字段冲突均安全失败。补全仍检查K1>R风险并持久保持既有RESERVATION_OVERRUN。

这只是利用已收到证据的本地结算，不能推断零费用；完成后新查询可恢复结果，已返回Pending不变。原Provider若因账本故障FAULTED则不启动新写，只允许受控本地确认原已发完成命令，未落盘结果可能丢失并继续UNKNOWN。无原活跃所有者的重启UNKNOWN不会自动产生这种证据。

close立即停止准入和新attempt，取消尚未消费许可工作；在配置close_timeout_ms内等待自有执行／本地结算，超时返回INCOMPLETE及cleanup_pending，不等待无界线程、不提前关闭仍使用的适配器。重复close返回首次不可变报告，后续资源结束见health；借用存储、门控和日志由装配方按所有权关闭。只有实际释放全部自有资源才CLOSED。关闭、初始化和恢复由可信装配串行协调，业务调用可有限并发；同一存储身份仅签发一个活跃Provider所有者绑定，旧所有者实际结束或可靠隔离前不签发新执行／恢复绑定，不把活跃PREPARED误当重启遗留。实例／账户并发槽均覆盖尚未结束的适配器和本地任务，与持久UNKNOWN费用责任分别管理。

<a id="provider-foundation-simulation"></a>

### 9.8 可控模拟适配器与usage

模拟适配器按可信测试装配持有的有限场景表返回规范化协议结果；场景及事件屏障不放进配置键、用户请求、环境变量或生产扩展发现。资源装配精确声明SIMULATED，无真实endpoint／secret resolver，无HTTP／SDK依赖，不接受任意调用方执行脚本。每次接收attempt_id及已隔离请求，适配器不接触账本／UoW，不自己分配逻辑请求或重试。

最低场景集合：四能力成功、明确敏感拒绝、其他拒绝、已结束且费用完整的限流／瞬时失败后成功、认证失败、不支持、错维度／候选／非有限值／超限输出、发送前取消、发送后协作取消、未知超时、迟到完成、普通异常、永久阻塞。测试时钟／事件控制等待，不靠长sleep碰运气。未知异常不能通过检查自由异常文本判因；测试标记可控失败发生位置也不自动代表未发送，证据由许可／执行所有权决定。

usage是固定字段不可变记录：input_tokens、output_tokens、cache_read_tokens、cache_write_tokens、reasoning_tokens、input_items、embedding_dimensions、rerank_candidates、media_bytes、media_duration_ms各为有界非负整数或None，另有source、coverage和字段关系。source为SIMULATED_REPORTED／LOCALLY_ESTIMATED／UNAVAILABLE，coverage为COMPLETE／PARTIAL／UNAVAILABLE；不声称模拟器是供应商。cache_read／cache_write为input_tokens的互斥子集，reasoning为output_tokens子集，已知组合不一致为INVALID_RESPONSE；未知总量不推造组成，单个字段0必须有显式证据。

适配器原始usage只保留上述同名数量及固定模拟billing_input_units／billing_output_units、known_cost_atoms的安全白名单，未知字段整项不遍历／不记录；白名单字段非法则拒绝响应。provider_cost_items按互不重叠的input／output模拟计价项计算，不把缓存／reasoning再累计。reported费用与按模拟价表估计值分字段，二者相异时保留两者和不一致标记，不冒称估算是账单；最小预算以已明确报告费用为K，缺报告但计价数量与价表完整则以估算K结算并标LOCALLY_ESTIMATED，任一欠缺保留未知责任。解析失败不得把返回usage计为0；能独立安全验证的已知小计保留，其余未知。

没有LOCAL_REUSE／EXTERNAL_RESULT_REUSED写端口或业务缓存本轮实现；现行§8.5仍规定未来归属和计量。相同逻辑键原结果查询是幂等命中，不算一次新的本地复用逻辑请求；媒体内部采用生成模拟处理也只有一份attempt／费用。

<a id="provider-foundation-observation"></a>

### 9.9 只读计量、审计与诊断隔离

观察句柄由可信装配绑定可见caller_scope集合及是否允许账户预算汇总；普通工作句柄只可定位自己的请求状态，不能获得全账户使用明细。结果读取需独立所有者能力；统计、审计读取能力都不能取得payload。查询不接SQL、自由表达式、任意角色或原始存储句柄。

get_request返回归因、配置来源／缺失版本、request和有界attempt明细、费用／usage覆盖、状态和交接存在性，不含prompt、媒体、结果正文和审计历史。query_usage参数恰为UTC起止范围、可空的允许scope／capability／task_role／profile／account过滤及固定group_by（NONE／CAPABILITY／TASK_ROLE／PROFILE／ACCOUNT）；权限缩窄先于查库，不能扩张句柄集合。同一查询最多配置query_row_limit行，按稳定维度ID排序；需要更多时返回LIMIT_EXCEEDED，不截断后声称全集成功。时间采用request登记时间的半开区间，不从墙钟排序推断提交顺序。

汇总只覆盖已持久请求，返回范围、as_of、样本数、SIMULATED标识、逻辑各状态数、prepared／completed／not_sent／unknown attempt数、确认适配器已开始次数与发送未知数、各已知usage小计及缺失项数、已知费用／估算／未知责任。PREPARED或重启UNKNOWN不能冒称确认调用数；逻辑数与attempt数、item数分开，父run不再次计费。默认不提供成功率或百分位；后续可依据§8.6定义分母再补，不将缺失样本当零或生成100%。金额过大安全失败，不SQLite溢出转浮点；同一次读取使用同一短快照，不返回游标或惰性结果。查询超时／不可读是Failed，不能返回空成功。

get_budget_state读取当前装配账户／窗口的额度、已登记attempt数、已知小计、held责任、实际可用／赤字、risk_state和覆盖；账户范围由句柄绑定。这里可展示跨scope共享账户合计，但只有显式授予账户汇总权的观察者可读。预算准入不调用此报表端口。

Provider受审计命令各声明一个必需位置provider_change，事件码为PROVIDER_REQUEST_REGISTERED、PROVIDER_ATTEMPT_PREPARED、PROVIDER_ATTEMPT_SETTLED、PROVIDER_REQUEST_TERMINATED、PROVIDER_UNKNOWN_RECORDED、PROVIDER_EVIDENCE_RECORDED之一，版本1；首次request＋attempt登记用REGISTERED。actor_kind固定SYSTEM，actor_ref由绑定的可信执行主体提供，理由码分别REGISTER／PREPARE／SETTLE／TERMINATE／RECOVER／LATE_EVIDENCE。change仅包含request_id、可空attempt_id、前后修订、前后状态枚举及费用覆盖布尔；目标引用绑定实际request或attempt。数量和金额真相只在Provider账本，不复制第二份费用审计表；无输入指纹、配置证据或模型正文。预算初始化用单独PROVIDER_BUDGET_INITIALIZED／INITIALIZE事件和账户ID目标，不复用工作身份。审计追加与必要清单由[既有审计服务](logging.md#transactional-audit-contract)执行，不新增日志错误码或动态事件注册。

诊断在事务外仅调用现有provider Logger及OPERATION_COMPLETED／OPERATION_FAILED模板、合法内部关联ID和允许的count／duration_ms／outcome／error_code。不把ProviderError整体、usage、账本行、恢复引用、配置证据、payload或原始异常交给emit；错误分类按现有TIMEOUT／IO_FAILURE／VALIDATION_FAILED／INTERNAL_FAILURE投影。诊断过滤／关闭／抛普通异常不撤销提交、漏记费用或递归审计；审计／账本故障仍阻止新attempt，日志成功不作替代证据。

<a id="provider-foundation-configuration"></a>

### 9.10 配置接入与实现边界

完整Schema、嵌套字段、单位、范围、元信息匹配、固定验证器、独立解析入口和旧入口兼容唯一见[配置§11.13契约](configuration.md#configuration-provider-validation-contract)。Provider只通过配置公开端口读取原生快照；初始化再次调用配置拥有的适用性检查，不从私有定义／环境／默认参数补值。配置无值revision，所以初始化固定这份实际值，逻辑执行证据按§9.2保存；新旧服务配置不能在同一开放逻辑请求中混用。

运行时仍采用已验证的Python 3.12环境和标准库路线；本契约不要求重装运行时或引入新依赖。公开结构不可变、文件／注释／测试自描述和全量Pyright要求沿用[代码规范](../CODING_STANDARDS.md)。Provider包、配置补充、必要持久化衔接及对应测试是后续实现的整体范围；不因普通技术辅助类型新增而分拆实现审批。

<a id="provider-foundation-acceptance"></a>

### 9.11 完整合成验收矩阵（预期）

所有例子均为已批准验收预期；具体执行覆盖、版本和结果见[CURRENT_TASK](../work/CURRENT_TASK.md)，不由单项检查推断整个矩阵通过。验证使用同一个公开装配流程、真实自有临时SQLite库及模拟适配器；建库前身份由父进程保留，物理资源及环境证据沿用[持久化验收分层](persistence-and-transactions.md#persistence-foundation-acceptance)，不外推Linux／掉电／生产保障。

共用显式夹具：实例并发2、账户并发1、max_attempts=2、attempt_timeout_ms=100、request_timeout_ms=1000、retry_delay_ms=10、close_timeout_ms=1000、query_row_limit=100、request_max_bytes=4096、result_max_bytes=4096；账户sample_account、窗口sample_window、额度20attempt和1000000atoms。generation价格input=2atoms/unit、output=3atoms/unit，合成请求输入10units、输出上限20units，预留80atoms；明确返回输入10／输出5计价单位则估算35atoms。两个profile可共用账户，其他三能力各提供明确声明／界限。完整配置其余必需值按规格显式提供，不将这些示例当默认或性能指标。

| 合成场景 | 预期及检查证据 |
| --- | --- |
| 四能力成功，经公开端口登记→调用→结算→查询／recover_result | 每次一请求一attempt、真实账本／预算／交接／审计／回执一致；向量对齐、空间正确、rerank仅原ID、媒体不触发生成双重计费 |
| 配置全集合与未知validator、组缺项／错误元信息、路径上下文、对象字段、账户／profile引用和单位关系 | 新入口按固定首错拒绝，旧三个入口保持既有拒绝；无清空声明、私有快照或假版本。成功后修改原输入不能改变配置、请求或结果 |
| 同键同内容并发，完成后重复；后续配置改变后重复旧请求；同键改负载／归因／profile／所有者 | 唯一原事实，查询返回原结果，无新attempt；内容改变冲突，当前配置不覆盖旧证据 |
| 账户共用的两个profile／两个caller_scope并发争预留；额度刚够／少1atom／attempt耗尽 | 原子共同额度、无透支新准入，PAUSED_BUDGET独立于普通失败；BUSY无无界排队；边界整数精确 |
| 完整费用35、部分已知35、全未知、已知100大于80预留、金额运算溢出 | 分别held=0／45／80／0；超额同事务置账户RESERVATION_OVERRUN，余额仍大也阻止该账户新attempt且重启保持；已知和未知分别汇总，溢出拒绝不转float或截断；缓存／reasoning不重复计价 |
| TRANSIENT_FAILURE或RATE_LIMITED已明确结束且费用完整后成功；敏感／认证／格式／UNKNOWN | 前两者最多2attempt且统一deadline／预算；后者不自动重试。敏感与其他拒绝分开，媒体拒绝不调用业务轮转 |
| 门控在登记前／登记后／许可消费竞争点改变epoch、FOCUSED普通请求／真实授权梦境、缺门控及假DREAM字符串 | 每次实际调用前检查，撤销胜出则零调用；只有受签发内部梦境能力可在FOCUSED发送；测试不能冒称生产模式完成 |
| 伪造／子类／错服务／错scope／已关闭句柄、普通观察者查结果／全账户、恶意repr或迭代对象 | 权限与精确载体先拒绝，无查询或适配器副作用，无正文／秘密／路径／异常泄漏 |
| 总deadline比storage期限短；退避后剩余不足；取消发生在许可前后；外部任务取消 | 调用方等待有界，无迟到开始、deadline重置或重复发送；已启动所有者保持，返回Pending而非假回滚 |
| 适配器永久阻塞、迟到已知结果、槽位饱和、重复close；已记部分35后迟到完整50，再重复或改证据 | 线程／任务数固定上界，INCOMPLETE及cleanup_pending真实；迟到仅本地补证据、保留曾未知状态；差额仅加15且释放旧held，重复无效果、相异已知内容冲突，不改旧报告或新开attempt |
| T10各写点／审计／COMMIT前后故障或确认丢失，再尝试调用适配器 | 未确认登记绝无调用；确认原登记也不能给重启者发送权；失败不部分预留或漏审计 |
| T11各写点、交接／费用／审计／回执编码故障、COMMIT后回复丢失、重复完成／不同完成内容 | 同事务全无／全有；只有确认后可返回结果，原结果可恢复且计量一次；不同内容冲突，无不完整正文发布 |
| 父进程在登记COMMIT后、适配器开始后、结果收到但未落盘、完成COMMIT后四屏障终止子进程并等待退出 | 新解释器同库恢复：前三者无可恢复完成时UNKNOWN且责任不释放、零自动重发；最后者原结果恢复。许可未消费仅内存已知的第一屏障也不能跨进程声称NOT_SENT |
| OPEN但已知失败attempt之后、重试登记之前中断；已有未知记录反复重启 | 从已知事实终结，不补attempt；UNKNOWN不复位、不清零、不删除，查询可观察 |
| 交接缺失／损坏、费用与预留不一致、旧Schema或无Provider表数据库、低新读取限额 | 完整性或格式拒绝，无自动补表／迁移／覆盖；历史原结果按格式上限读，不用新配置截断，读超时明确失败 |
| 诊断禁用／过滤ERROR／sink故障，与必须审计失败分别组合；统计和恢复查询 | 前者不影响账本和费用，后者阻止提交／新attempt；全部只读查询无模型调用、无审计历史／payload旁路 |

实现轮应记录当时文件集合／Git基线、Python／实际SQLite／Pyright版本及命令结果；保留原测试行为，新增有意义的公开流程和故障测试，按[统一验证规则](../CODING_STANDARDS.md#validation-environment)选择定点及关联回归，完成全量Pyright和必要的编译、锁文件及含新增文件的差异检查。静态架构审查确认Provider外没有模型出口、SQL旁路和配置私设默认；具体执行结果只记在[CURRENT_TASK](../work/CURRENT_TASK.md)。

<a id="provider-foundation-decisions"></a>

### 9.12 集中已批准决定与审批边界

以下F1–F5及关联配置补充、有界文本衔接已由**主会话按用户授权审查批准**，共同构成同一整体阶段的技术契约；不扩大产品决定范围，不表示用户亲自验收本阶段，也不拆为独立小阶段：

| 已批准组 | 唯一详细位置／理由 |
| --- | --- |
| F1 四能力、身份与边界 | [§9.1–9.2](#provider-foundation-ports)：非流式最小能力、封闭句柄、真实缺失版本和不可编辑执行证据，避免以假快照／角色绕过现有边界 |
| F2 门控、预算和失败协议 | [§9.3–9.4](#provider-foundation-gates)：测试专属签发门控、共享账户固定窗口／整数TEST金额、保守UNKNOWN与固定首错；无生产模式或完整预算体系的伪实现 |
| F3 真账本与恢复交接 | [§9.5–9.6](#provider-foundation-storage)：同库受限仓储、登记确认后发送、完整结果持久交接、无自动重发；[持久化衔接](persistence-and-transactions.md#provider-persistence-bridge)只加有界文本且不放宽审计 |
| F4 有界执行和查询 | [§9.7–9.9](#provider-foundation-execution)：有限attempt／总deadline／取消所有权、迟到证据本地结算、账本直接查询、审计／诊断／正文权限分离 |
| F5 配置与整体验证 | [配置§11.13](configuration.md#configuration-provider-validation-contract)、[§9.11](#provider-foundation-acceptance)：新显式完整校验入口及保留旧语义、真实临时数据库的完整合成矩阵 |

本次未发现需要以现行正文互相覆盖解决的产品冲突。ProviderSchema／服务／配置新入口属于已批准契约的实现内容。真实持久配置版本、价格版本、生产门控／秘密／路径／预算周期、存量格式迁移及UNKNOWN管理恢复决定仍是范围外前置；模拟测试不将它们批准或宣称完成。实际冲突、降低持久性／权限／隔离／有界性、改变敏感拒绝或UNKNOWN责任、新供应商／真实支出、生产路径或技术栈／Python主次版本改变须交用户，不由本表代决。实际授权和停止点见[CURRENT_TASK](../work/CURRENT_TASK.md)；全程不暂存或创建提交。

<a id="stored-media-provider-bridge"></a>

### 9.13 真实媒体字节与模拟Provider桥接（推荐已批准）

本节仅补齐[正式记忆／来源／媒体集中契约](formal-memory-source-media.md)所需的真实媒体授权桥接，推荐方案已随主契约获用户批准。模型仍用SimulationAdapter，四能力、账本／费用、UNKNOWN、结果交接及原配置证据协议保持；不新增真实SDK、URL抓取、解码、流式供应商或价格版本。真实文件只证明媒体所有者保存了实际字节，不能标成真实模型理解。

基线`ProviderService.authorize_media`仅声明合成bytes且上限1048576，生成的AuthorizedMedia是进程内能力，不是可持久blob授权。推荐新增可信装配专属`bind_stored_media_authority`及该能力的`authorize_stored_media`／`release_media_authorization`：先验证原生媒体服务、同库／caller_scope、结果所有者、实际occurrence和READY generation、已建立的PROCESSING／READ保护，再接收媒体所有者读回并核验的精确不可变bytes。输入包含稳定artifact_id、modality及来源绑定；不从调用者自报hash签发，不交Provider路径或删除权。

新能力只能由media服务的可信桥接获得，普通WorkPort、外部宿主、合成事件字段均不能生成它。Provider的规范化媒体请求继续采用原scope／owner／artifact_id／byte_count／sha256及模态／task，原请求指纹字节格式不变；物理generation、授权出现、处理策略／prompt、真实配置snapshot及原操作键在媒体工作所有者中完整持久保存，并由稳定artifact_id关联。artifact_id不可跨不同字节重用；重新授权恢复时须验证同一实际字节，不用新文件替换旧请求的内容。

旧合成入口／旧AuthorizedMedia读取按原协议保持，新真实授权加入独立签发类别和有限生命周期：每个在途媒体处理工作最多一个字节能力及一个底层任务；原件暂按1 MiB界限全部读入，文件读取和校验在受限执行器中完成。Provider及模拟适配器不另保存无界字节副本；迟到响应／本地确认结束之前不可撤销仍在使用的能力或释放文件保护。等待超时仍保留原任务和槽，release只在无在途消费者时清除对应登记及强引用；重复release幂等，不影响其他工作。旧合成兼容测试也须保留其原公开语义，不能靠全清Provider能力表释放新资源。

媒体工作在首次Provider登记之前保存原请求所需身份／字节摘要／generation／策略和operation_key；原件由持久保护保留到Provider查询／本地结果落盘可以不再依赖它为止。跨进程恢复先按原权限重新签发有限字节能力，调用既有`lookup_request(understand_media, original_request)`，再按原request_id授予结果所有者`recover_result`。媒体临时能力不进入通用回执、审计、运行日志或Web；查不到、字节缺失／损坏或结果未知均不能新发模型。已明确完成且本地解释已保存的复用不再调用Provider、不重复计费。

原生结果所有者核验终态时，除恢复有正文交接，还须通过原请求受限状态读取确认持久outcome及明确reason。只有原MEDIA_UNDERSTANDING请求的`SENSITIVE_REFUSAL/SENSITIVE_INFORMATION`匹配库／owner／artifact／scope／任务及字节绑定，才可交给media构造内部拒绝证据；失败／拒绝没有正文交接时引用原request，不伪造handoff。普通事件的status、source_ref或正文没有这项权力。保护scope、跨事件优先级及竞争结果唯一见[媒体拒绝协议](formal-memory-source-media.md#interpretation)，本桥接不授予外部签发／解除保护权。

每次媒体dispatch除原epoch／owner外，须在共用短门控串行区重查该内容的已发布拒绝保护修订；保护持久化期间先关闭对应新发送，COMMITTED后发布缓存视图，未知保持关闭。若保护原事务可靠未提交、无其他故障且原owner结束，才可撤此保守阻止；跨进程先关闭发送并恢复保护，不能漏读保护后使用旧成功缓存。已经消费许可的调用按原Provider协议收尾，任何下一attempt仍重查。该桥接不改变Provider账本或原请求指纹格式。

媒体准备／逐出现领取／原请求关联的持久状态及期限只见[准备协议](formal-memory-source-media.md#media-preparation)。Provider能力的artifact及owner由该唯一工作确定；新窗口只是消费者，不签发第二份请求。media在调用前核验原请求元信息能无损映射到[理解记录封套](formal-memory-source-media.md#interpretation-envelope)。内部成功的业务结果超限只影响media本地FAILED记录，不重写Provider已确认结果；敏感终态先独立核验，固定占位及证据不受可变结果正文限额影响。

运行门控须将MEDIA工作及其owner／epoch纳入现有GateBinding，不能由`task_role=MEDIA`字符串自行获得权限。PREPARING／FOCUSED禁止新的补充理解；已消费许可的请求仅按既有收尾规则落本地结果，无下一attempt或补充学习。恢复与只读观察不调用适配器。

桥接失败使用独立MediaError的安全分类，Provider内部请求仍返回原ProviderError；不扩大旧错误枚举或透传文件异常。源绑定非法为BINDING_MISMATCH，超限为LIMIT_EXCEEDED，未READY／退役为BLOB_NOT_READY／BLOB_RETIRING，实际字节不符为CONTENT_CORRUPT，其他映射见[集中错误表](formal-memory-source-media.md#ports-permissions)。结果source仍SIMULATED，实际存储／候选来源由媒体和记忆观察分别声明。

必要验证为真实临时文件→媒体授权→原Provider模拟请求→真实SQLite交接→媒体结果持久复用，包含跨进程原键查询、字节损坏、授权隔离、敏感拒绝、门控竞争、等待取消、强引用回收和旧模拟端口兼容；实际验证状态见CURRENT_TASK，统一纳入[整体验收](formal-memory-source-media.md#acceptance)。
