# Provider能力、调用控制、计量与安全

> 本文件是权威原文的阅读视图，不是独立需求。原文仍是权威依据；后续修改规则时，先更新对应原文，再同步受影响的视图与相对链接；[覆盖映射](../work/ORGANIZATION_REPORT.md#coverage)仅作整理历史保留，不要求持续更新。保留原文“已确定、建议、示例、待确认”的性质；下列导读不新增决策。正文含原有编号，仅限文档追踪。

适用主题与局部定义：Provider是本系统和受支持扩展的唯一模型出口。一次逻辑请求可包含零次或多次有限网络尝试；profile配置真相在统一配置模块，费用和尝试执行账本在Provider。

来源：[原文 L534–L681](../../companion_memory_module_design_provider_logging_config.md#section-08)。行号对应整理时的哈希基线。

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
