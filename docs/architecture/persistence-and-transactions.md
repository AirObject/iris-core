# 持久化、事务、检查点与恢复

梦境新装配的实际owner、原子步骤、发布／退出及恢复见[梦境持久化契约](dream-and-long-term-maintenance.md#persistence)；原格式不追溯修改。

日常认知新组合的封闭记录、实际writer／审计、原子终结及恢复，统一见[集中契约§6](daily-cognition-and-image-learning.md#6-持久记录事务与恢复)；原组合格式与必要保证不追溯放宽。

> 本文件是本主题的现行正文，在此唯一维护。既有要求、已批准契约、建议和待批准事项保持各自状态；迁移不新增产品决定或实现授权。文档关系见[总入口](../INDEX.md)。

适用主题与局部定义：Unit of Work（UoW）指应用编排层让相关模块加入同一次本地事务的工作单元。Provider是唯一模型出口；本地事务恢复、有限远程尝试和结果未知分别处理。

设计／审核参考：[冻结原始文档](../reference/companion_memory_module_design_provider_logging_config.md)。仅供追溯，不作为现行约束。

按关联工作联合阅读：[队列语义](../product/batches-and-learning.md)；[数据所有者](ownership.md)；[Provider账本](provider.md)；[配置激活协议](configuration.md#source-line-895)。

返回[文档总入口](../INDEX.md)；实际进度见[工作状态](../work/STATUS.md)。

已批准契约：[持久化事务基础与同事务审计整体契约](#persistence-foundation-contract)；[集中已批准决定与前置缺口](#persistence-foundation-decisions)。既有业务事务要求保持原状态；本次批准不授权实现。

<a id="section-04"></a>

<a id="source-line-184"></a>

## 4. 持久化与允许损失范围

<a id="source-line-186"></a>

### 4.1 接收承诺

**成功确认接收 = 对应数据及其业务归属已提交到持久化存储。** 容器重建、应用进程崩溃或正常重启，不应使这些已确认的业务数据凭空消失。数据库事务保证本地变更全有或全无，而不是保证所有外部效果也能撤销。[S06](references.md#s06)

持久化不等于永不删除。成功批次轮转、普通失败留尾丢弃、敏感拒学不留尾、遗忘期满删除以及无引用媒体清理，仍是合法、可审计的业务操作。

| 数据 | 跨重启要求 | 允许损失/移除的边界 |
| --- | --- | --- |
| 已确认接收输入、引用与附件关联 | 必须保留 | 只能按已终结批次与引用生命周期释放 |
| 各入口S1/S2/S3、冻结批次、梦境序列与游标 | 必须保留 | 仅通过轮转、移交和已定义终态改变 |
| 正式记忆、双指标、来源、实体/关系 | 必须保留 | 仅明确修改、遗忘或对象删除；不能因索引失败删除正文 |
| 当前状态、目标、截止与提醒状态 | 必须保留 | 状态可陈旧但不能假造更新；按业务操作修改 |
| persona、初始化配置、监管prompt | 必须保留 | 未通过检查的候选不能覆盖已发布摘要 |
| 已接收的使用反馈与幂等凭据 | 有效期内必须保留 | 到期按明确策略清理，不能重启后重复强化 |
| Provider逻辑请求、网络尝试、原始usage、费用条目与预算预留 | 策略保留期内必须持久化 | 聚合可重建，已知记录不因重启变成零；远程未知显式保留 |
| 配置版本、有效指针、生效计划、在用快照和密钥版本引用 | 必须持久化 | 历史在仍被批次/梦境/调用引用时不可提前清理 |
| 普通诊断日志文件 | 按运行日志保留策略持久化 | 异步队列在强制崩溃时可丢末尾，必须说明；不承诺与事务审计相同保障 |
| 学习结果暂存、任务检查点、终态凭据 | 有恢复用途时必须保留 | 终结并按来源/审计规则处置 |
| 审计历史与必要操作记录 | 按审计保留策略持久化 | 不向agent开放；可按开发者策略到期清理 |
| 已获得的embedding向量与媒体理解 | 应持久保存并复用 | 理论可再生成，但有费用/版本差异，不作为日常可丢弃优化缓存 |
| 向量ANN结构、词法索引、热点对象副本 | 建议持久化加速启动 | 可从保留数据重建；重建期间明确降级，不重学原始输入 |
| 内存锁、唤醒信号、临时排序、未完成响应拼装 | 可丢失 | 由持久化状态恢复或由外部重新请求 |
| 未确认上传完成的临时文件、未送达消息 | 可丢失 | 不报告完整接收成功；未完成上传清理不得碰到已引用文件 |
| 未送达外部的提醒、未接收的使用/目标反馈 | 允许有限丢失 | 遵守原有限投递规则，不补造反馈或偷偷无限重发 |

发布过的embedding结果和索引结构不同：重建本地ANN不应默认重新调用远程embedding。删除/修正记忆后，旧向量即使尚未物理清除，也不得经检索绕过当前正文与状态核验。

<a id="source-line-213"></a>

### 4.2 三种“重试”严格分开

| 场景 | 草稿规则 |
| --- | --- |
| 本地事务竞争、提交中断或确认丢失 | 先查操作标识是否已提交，再幂等重试本地命令；临时错误有限退避，永久故障报警；确认条件由[结果确认契约](#persistence-foundation-idempotency)细化 |
| provider普通失败且底层尝试已耗尽 | 批次失败终结、留尾，不重启整个学习批次 |
| provider明确敏感拒学 | 敏感终结、不留尾，不改provider自动绕过拒绝 |
| 进程中断时远程请求结果未知 | 保存为结果未知；优先恢复已落盘结果或按供应商能力查询；无确认途径时不声称回滚了远程请求 |

本地事务不包含已经发出的HTTP请求、供应商账单或普通文件操作。不能在`BEGIN`之后调用远程模型，再认为数据库回滚会退还调用次数。

远程结果未知且没有已保存结果时，建议暂停该批次进入管理可见的`REMOTE_RESULT_UNKNOWN`，由明确恢复操作决定重新调用或按普通失败留尾终结。重新调用可能产生额外费用，必须记录为新的尝试。这是异常契约建议，不是默认重放所有失败批次。

<a id="source-line-226"></a>

### 4.3 备份与物理故障

事务和持久卷不能代替备份。磁盘损坏、卷误删和宿主丢失需要独立恢复方案。备份需覆盖权威数据库与其一致状态、仍被引用的媒体、必要配置；热索引可以在恢复后重建。

首期可通过维护窗口生成一致备份，并在备份期间暂缓媒体GC；不中断服务的备份需要数据库一致快照和媒体引用清单协调。恢复演练验证原始输入、来源、队列位置、目标和persona，而不仅验证数据库文件能打开。


<a id="section-07"></a>

<a id="source-line-473"></a>

## 7. 原子操作、检查点与恢复契约

<a id="source-line-475"></a>

### 7.1 三层状态

业务数据库内保持三层区别：原始输入/当前业务对象；尚未正式发布但已持久化的候选与工作检查点；开发者审计历史。当前候选可供对应运行恢复，历史日志不可供记忆agent找回旧认知。

应用编排层负责跨模块的事务开始与结束，模块命令通过同一个Unit of Work修改各自拥有的数据。持久化任务表只是本地待办与恢复记录，不是外部消息无限重投系统。

<a id="source-line-481"></a>

### 7.2 必须原子的业务操作

| ID | 操作 | 同一次本地事务必须覆盖 |
| --- | --- | --- |
| <a id="t01"></a>T01 | 接收输入 | 幂等键、原始事件、入口序列、正常/梦境归属、READY附件引用、接收回执标识 |
| <a id="t02"></a>T02 | 冻结批次 | 批次ID、S3/S2/S1成员、目标ID、配置版本、状态及执行归属 |
| <a id="t03"></a>T03 | 学习成功提交 | 新记忆及修订、来源/关系、必要目标建议、批次成功、S2消费、新S3、引用变化、审计、索引待更新标记 |
| <a id="t04"></a>T04 | 普通失败终结 | 失败原因/终态、精确释放目标、S2尾部保留及失败标记、暂存候选释放、引用和审计 |
| <a id="t05"></a>T05 | 敏感拒学终结 | 拒学终态、独占未提交内容释放、历史辅助清空、突然失忆风险、引用和审计 |
| <a id="t06"></a>T06 | 梦境消息移交 | 同一消息正常位置建立与梦境位置解除、入口游标、媒体引用连续性 |
| <a id="t07"></a>T07 | 使用强化 | 召回凭据核验、反馈幂等收据、分数变化、状态阈值检查、恢复影响待办及审计 |
| <a id="t08"></a>T08 | 对象修改/删除或目标管理 | 当前值/状态、修订与必要墓碑、索引/关联待办、提醒状态和审计 |
| <a id="t09"></a>T09 | 梦境步骤/模式/persona发布 | 每个短步骤的操作键与效果；切换时的模式epoch、发布指针或回流启用标记 |
| <a id="t10"></a>T10 | Provider准入与发送登记 | 逻辑请求/操作ID、固定profile/config版本、预算预留、attempt准备状态；提交后才能发送网络请求 |
| <a id="t11"></a>T11 | Provider完成与计量结算 | 同attempt的规范化结果状态、已知usage、价格版本/费用条目、预留结算/未知状态、聚合待更新；重复完成幂等 |
| <a id="t12"></a>T12 | 配置变更与激活记录 | expected_revision校验、不可变版本、变更人/理由、持久化生效计划或有效指针、脱敏审计；内存指针按恢复协议同步 |
| <a id="t13"></a>T13 | 审计与统计维护 | 审计随原业务事务提交，详细要求见[独立审计契约](logging.md#transactional-audit-contract)；统计聚合checkpoint与增量应用原子，避免重启重复累计；诊断文件输出不伪装为同库事务 |

这些操作只包含本地可事务化数据，不在事务中等待LLM、embedding、rerank或HTTP通知。索引文件和媒体文件采用独立的阶段协议，不声称它们自动被SQLite事务回滚。

<a id="source-line-501"></a>

### 7.3 学习提交的具体流程

```text
1. T02冻结批次并提交，记录运行ID和请求指纹。
2. 在事务外组装受限上下文、调用provider。
3. M13持久化T11的调用结果状态与已知usage；通过同一UoW把必要结果交接引用和M05暂存候选一并保存，或先保存持久化交接产物再由M05幂等取走。计量只由M13写入，不由M05再累计。
4. 本地校验候选，建立确定性变更集。
5. T03一次提交正式记忆、来源、轮转、引用与派生工作标记。
6. 事务确认后才将本轮计为成功；索引工作读取已提交修订。
```

发生普通provider终结错误执行[T04](persistence-and-transactions.md#t04)，明确敏感拒学执行[T05](persistence-and-transactions.md#t05)。存储I/O失败不是[T04](persistence-and-transactions.md#t04)/[T05](persistence-and-transactions.md#t05)，应该进入系统故障状态并保留尚未消费的输入，恢复后完成本地事务。

若[T03](persistence-and-transactions.md#t03)提交已成功但确认丢失，按`batch_id + operation_id`读取提交凭据：存在则返回已提交结果，不再次调用LLM、不重新生成记忆ID、不重复旋转S3。不存在则用已保存候选重试本地事务；“不存在”的可确认条件由[结果确认契约](#persistence-foundation-idempotency)细化，不能从查询故障推导不存在。

所有生成的对象ID或操作去重键必须在重试间稳定。单纯依赖“数据库会回滚”而每次重试随机生成新对象，会在提交确认未知时产生重复数据。

<a id="source-line-518"></a>

### 7.4 外部结果未知的限制

HTTP发送、provider执行与本地持久化之间没有跨供应商原子事务。进程可能在provider已经生成结果而本地尚未保存时崩溃。此时不能证明调用没有发生，也不能保证再次发送不会计费。

优先从本地已保存结果恢复；供应商协议明确支持按已知请求/响应ID查询时可使用该能力。没有结果且无法查询时标记未知，不把它伪装成敏感拒学或已完成学习。恢复决策应明确且有限，不能用一个自动循环掩盖重复费用。

外部消息接收也有类似的确认丢失窗口：数据已经提交但调用方没有收到回执时，调用方重发同一幂等键，系统返回原接收结果，不再追加一条事件。

<a id="source-line-526"></a>

### 7.5 索引一致性

记忆、来源和生命周期是权威；索引为派生。事务内记录`memory_id + revision + index_task`，事务外构建向量/词法投影。索引应用按ID与版本幂等。

新向量到达时先核对当前正文版本与embedding空间；过时结果不能覆盖新版本。索引快照带generation和覆盖水位，落盘采用写新文件再切换指针的方式。重启后从水位与待更新记录修复本地索引，不重新学习已成功批次。

一条已提交记忆即使尚无向量，也可按ID和已有本地索引使用。语义覆盖可能暂缺，必须可观察；这与遗忘或删除完全不同。


图示／代码框中的文档标记定位：[M05](../modules/cognition.md#contract)、[M13](../modules/provider.md#contract)、[T02](persistence-and-transactions.md#t02)、[T03](persistence-and-transactions.md#t03)、[T11](persistence-and-transactions.md#t11)。这些标记仅用于本文阅读，不能进入未来实现。

<a id="persistence-foundation-contract"></a>

## 8. 持久化事务基础与同事务审计：整体契约

**状态：契约已批准。** 本节及其引用的[审计详细契约](logging.md#transactional-audit-contract)、[配置补充§11.12](configuration.md#configuration-persistence-validation-contract)、[SQLite验证采用契约](deployment-candidates.md#sqlite-validation-candidate)构成一项整体交付，公开行为和保障统一列入[集中已批准决定](#persistence-foundation-decisions)。用户已批准P1–P6，包括建库身份补充、固定失败协议及配置§11.12；不改变上文既有要求的状态；执行授权见[CURRENT_TASK](../work/CURRENT_TASK.md)。私有类、辅助函数、SQL布局、锁和执行器实现由实现者选择，不拆分为审批或验收任务。

整体交付包含：存储初始化和版本拒绝、模块仓储协作的短事务、操作幂等和持久回执、同事务审计、最小受控读取、关闭后重新打开及故障恢复。用测试专属的两个合成业务仓储贯通验证；正式包只提供基础设施和日志模块的审计能力，不提前建立记忆、入口、批次、Provider或配置激活的业务表／命令。索引、媒体、Web、审计导出、备份系统、自动修复和生产部署不在范围内。

<a id="persistence-foundation-ownership"></a>

### 8.1 所有权与最小端口

[模块所有权](ownership.md#source-line-236)与[基础设施职责](ownership.md#i01)继续适用。可信装配方持有存储生命周期和仓储绑定能力；应用用例编排层发起一次完整操作，由事务协调端口落实开始与唯一提交。业务模块只通过本模块受限仓储端口写自己的数据，日志模块提供[审计参与端口](logging.md#transactional-audit-ports)，基础设施承载SQL和事务资源。回执和库格式元数据由基础设施拥有，业务结果语义仍由对应模块拥有。

以下名称是语义签名，具体语言类型可调整，但行为和互斥结果不得省略：

| 端口／持有者 | 公开行为 |
| --- | --- |
| 创建、initialize(snapshot, resources, mode)／可信装配 | 创建不做I/O；mode明确为CREATE_NEW或OPEN_EXISTING。resources显式绑定装配方在任何建库副作用前准备并保留的expected_database_id，按§8.4核验。只在全部准备成功后交付可用服务；失败及建库确认未知按下方固定结果协议返回，不交出半就绪服务、裸连接、路径或SQL执行器 |
| execute(operation, local_command)／应用编排 | 一个受控本地命令，协调端口开始事务、去重、调用模块参与者、核验必要审计和回执后提交；返回下节规定的完整结果，不以退出上下文或排入队列当成功 |
| 模块参与仓储／对应模块 | 仅在本次UoW内执行预先装配的类型化读写，参与者无begin／commit／rollback权限；不按调用方字符串选择表、模块或SQL |
| read_receipt(operation_identity)／作用域绑定的编排端口 | 点查已提交回执，返回FOUND或NOT_FOUND观察值；后者只代表该次快照未命中，不是重试许可 |
| resolve_operation(recovery_handle)／作用域绑定的恢复端口 | 只确认已尝试操作的状态，不执行业务命令；按§8.3返回已提交、确认未提交或仍未知 |
| 模块受控点读／对应模块 | 按本模块对象ID读取当前已提交视图；多行结果取同一短读快照。返回深不可变值或明确NOT_FOUND，不带连接、游标、惰性迭代器和审计历史 |
| get_health()、close()／可信装配 | 健康为无数据库I/O的内存观察；关闭按§8.2有界结束并报告资源占用。重新打开使用新服务实例，旧句柄不复活 |

业务模块收到的UoW只是绑定到库身份、本次操作和参与者权限的有限期能力；跨服务、跨操作、跨执行所有者、过期或伪造句柄在任何SQL前拒绝。只允许可信程序静态装配仓储实现和命令处理器，无用户注册SQL、运行时插件发现或可执行表达式。能力隔离约束受支持代码，不宣称可在同进程沙箱化恶意Python。审计读取授权在[日志正文](logging.md#transactional-audit-ports)唯一维护。

<a id="persistence-foundation-lifecycle"></a>

### 8.2 短事务、并发、时限与生命周期

服务生命周期为NEW → READY → CLOSING → CLOSED；初始化前置失败且已完全释放资源时仍为NEW。存储故障、提交未知或清理未完成转FAULTED，停止新写入；此时只允许健康、关闭及受控结果确认，不能因诊断服务恢复就恢复数据库写入。旧服务资源确认释放后，以OPEN_EXISTING重新验证并建立新实例，不自动后台重开或重放命令。

一次写操作的顺序固定：安全输入与作用域核验 → 取得受控写所有权 → 显式`BEGIN IMMEDIATE` → 在同事务中核对幂等记录 → 模块仓储变更及必要审计 → 冻结结果并写入提交回执 → 唯一COMMIT → 返回提交确认。去重命中时不进入模块命令。`BEGIN IMMEDIATE`用于在业务写入前取得SQLite写事务；它仍可能因竞争返回BUSY。[SQLite事务说明](https://sqlite.org/lang_transaction.html)

应用编排对一次execute承担整体命令责任，基础设施代其执行上述开始与结束；没有嵌套公开事务或模块自行提交。模块返回的“已暂存”只说明参加成功。任一模块拒绝、语句错误、结果编码失败、回执写入失败或[审计失败](logging.md#transactional-audit-failure)均使整个UoW不可再提交，即使上层捕获了该错误；须回滚并确认事务结束。不得依赖某条失败语句只撤销自身的数据库默认行为。私有savepoint不能让必需变更或审计被跳过后提交其余部分。

明确的COMMIT竞争且事务仍活动时，本次在剩余预算内回滚并确认，不重跑命令；无法确认提交／回滚状态则进入未知。不能仅凭抛出了数据库异常推断事务已回滚；适配器须依据实际事务状态和回执证据分类。应用在NOT_COMMITTED后可另次有限重试原命令，期限与操作键不因内部循环被隐式重置。

写连接同一时刻只有一个所有者，一份服务只容纳一个在途写操作；新调用无槽位即安全拒绝，不积累无界命令队列。SQLite锁继续约束其他连接／进程，不因进程内锁存在而省略数据库唯一约束。另设配置限定数量的只读连接，每次点读拥有短快照，禁止read_uncommitted及向调用方泄漏游标。读取不能观察未提交的业务、审计或回执，旧读快照也不能用来确认回执不存在。跨进程竞争仅验证拒绝、有限等待及原子性，不批准多进程业务调度。

事务内只执行有界本地校验、SQL和必要编码；远程调用、文件发布、等待外部任务、诊断flush及用户交互均在事务外。准备耗时内容、生成重试间稳定的对象ID和语义输入应在取得写锁前完成，依赖当前修订的条件在事务内重查。参与者不通过回调偷偷开始异步工作；execute不会自动重跑整个local_command。

所有公开存储I/O操作使用统一配置的总等待期限，从准入开始以单调时钟计时，锁等待、SQL、提交、结果确认及清理共用剩余预算；锁等待取配置锁时限与剩余预算的较小值，不能逐条语句重置总时限。连接上的调用是同步的，调用方等待与资源执行所有权必须分离；不能阻塞异步服务的事件循环，不能把Python等待超时当作底层I/O已被取消。

期限到达时，尚未开始的工作不得后来自动开始；未发出COMMIT的活动事务转入回滚／清理，已进入COMMIT而无完成证据的操作按§8.3未知处理。阻塞I/O仍占用原连接、执行槽和路径所有权，健康置cleanup_pending；不得并发关闭／复用同一连接或无限创建替代工作器。迟到完成只更新健康和后续结果确认，不修改已返回对象。若底层系统调用永久阻塞，只承诺结束调用方等待，不承诺线程或锁已释放；恢复前须证明原所有者结束，必要时由受控进程终止完成隔离。

close停止准入，在配置的关闭总期限内结束既有事务和自有连接；它不因关闭而提交尚未完成的命令。关闭结果为CLOSED或INCOMPLETE，包含固定reason和cleanup_pending；只有实际释放全部自有资源才能报CLOSED。重复close返回首个不可变报告，迟到清理由get_health观察，借用的诊断句柄、时钟和资源工厂不被关闭。连接关闭／checkpoint故障不回溯撤销已有提交回执。

checkpoint由基础设施受控安排：按配置页数显式设置自动checkpoint阈值，维护只作有限尝试，不靠库的隐式默认、不让长读无限钉住快照。未完成checkpoint保持可观察并释放已完成的读取资源，不删除WAL强行“恢复”；提交回执的有效性不以checkpoint成功为条件。磁盘占用仍随保留数据增长，本阶段没有自动审计／回执清理或磁盘容量保障。

<a id="persistence-foundation-idempotency"></a>

### 8.3 幂等、持久回执与结果确认

幂等身份为`database_id + owner_namespace + operation_kind + scope_id + operation_key`。database_id按[建库身份协议](#persistence-foundation-initialization)预先准备、建库时保存且重新打开不变；命名空间／操作种类由可信装配绑定，scope_id由上层按真实隔离边界绑定，不由底层猜测入口或默认成空作用域。operation_key由调用方在首次尝试前持有并跨重试稳定；另一库或另一作用域不是同一操作。主体鉴权和业务归属仍由对应模块负责，键本身不授予权限。

首次执行前由可信模块适配器冻结版本化语义命令，计算内容指纹；不能信任外部自报hash。指纹覆盖全部影响结果的输入，包括参与模块、目标ID、预期修订、命令值、业务策略版本／引用，以及必要审计的行为人、理由码和事件语义；排除连接、deadline、观察时间等投递信息。使用确定性的类型敏感编码和抗碰撞摘要，缺失与空值、布尔与整数不同，不做隐式文本清洗；超出配置字节限额、无法稳定编码或不支持的命令版本在写前拒绝。算法／编码版本进入持久记录，升级不能让旧键绕过去重；不持久化整份输入到通用回执表。命令没有真实配置快照ID时不伪造一个，见§8.5。

同一身份由数据库唯一约束裁决：同键同指纹返回原回执；同键不同内容或不兼容命令版本返回IDEMPOTENCY_CONFLICT，保留原事实，不覆盖、不当作新请求。同键并发只能有一份已提交效果；另一调用在有限竞争结束后重查回执，或报告竞争／未知，不返回第二份成功。事务中生成的内部审计ID在已提交后以原记录为准；业务对象ID、命令语义和对外结果标识不能在确认丢失后重新生成。

**提交回执最小结构：** 回执协议版本、上述完整身份、命令版本及内容指纹版本／值、唯一commit_id、提交记录的UTC时间、结果Schema版本和有界类型化业务结果（稳定对象ID、修订、结果状态或受控持久产物引用）。UTC时间为事务内记录时间，不宣称精确物理刷盘时间；不以它判断先后。回执与业务变更在同库同事务保存，必要审计的关联结构见[审计记录](logging.md#transactional-audit-record)。回执结果足以原样恢复本次对外确认，不从可能已改变的当前业务行重算；大产物只保存由模块负责且有恢复保留保证的引用。不得放审计历史正文、SQL、路径、秘密或远程原始response。

execute返回互斥的深不可变分支：

| 结果 | 证据、含义及调用方处理 |
| --- | --- |
| COMMITTED(receipt, source) | COMMIT明确成功，或从可信存储查到且校验一致的原回执；source为NEW或EXISTING，放在返回封套中，不改原回执。只有此分支可对外确认成功 |
| NOT_COMMITTED(error) | 本次在独占写事务中确认无旧回执，业务未执行或整个事务已确认回滚，且原执行者不会迟到提交；这是可重新提交同一命令的证据。execute中error保留本次失败；resolve_operation仅正常确认不存在时error为None，不制造错误原因。重试仍须重新去重，不保证稍后没有别的调用提交 |
| REJECTED(error) | 输入、权限、生命周期、准入容量／锁竞争或键冲突等使本次请求不能完成；不表示同键历史不存在，也不返回成功结果。竞争后使用同键查询／确认，不换键绕过 |
| UNCONFIRMED(recovery_handle, error) | 已开始的操作无法确认最终提交状态，或回滚／旧执行者结束无法证明。恢复句柄只含已校验库身份、作用域和操作身份、命令／指纹版本与值；调用方保留原命令，只做有限结果确认，禁止把未知视为失败后重放 |

回执确认丢失不是新的业务操作。resolve_operation先核对原库身份、格式和权限；有匹配回执即返回COMMITTED／EXISTING。**确认不存在**必须同时满足：原执行所有者已结束或被可靠隔离、SQLite恢复已完成、取得排除并发写者的短事务后在新快照中查无回执；否则保持UNCONFIRMED。持有写锁期间核对后释放，返回NOT_COMMITTED仅为该截点证据，后续重试继续以唯一键保护。数据库不可读、锁未取得、旧快照未命中、查到不同库、残缺回执或格式校验失败都不是“不存在”。

提交后回复通道中断时可能根本没有结果返回，调用方仍凭首次请求前持有的身份和命令构造受控确认请求，不能依赖收到recovery_handle才有恢复资格。服务发现回执但其结果／审计关联损坏时报告INTEGRITY_FAILURE并停止写入，不能只因存在一行就返回成功。数据库被替换或回退到另一份副本不在普通重启保证内；库身份校验不能识别所有同身份旧备份，备份恢复另循既有[物理恢复边界](#source-line-226)。

回执查询及去重命中的FOUND／COMMITTED都须通过对应格式和[必要审计关联](logging.md#transactional-audit-record)核验。降低新实例的写入限额不能截断旧记录或让有效回执被当作不存在；历史读取按记录格式允许的上限处理，本契约格式的字节／条数上限采用[配置定义](configuration.md#configuration-persistence-definitions)相应范围上界，当前配置值只约束新写入。无法在读取预算内完成则返回明确错误／未知，不制造空结果。

本阶段不提供回执过期、键复用、删除或压缩接口；所有已提交回执随验证数据库保留。未决结果也不能因重启、超时或诊断保留期被清理。未来保留策略须保障仍可查询的结果及恢复引用，不在此制定永久业务归档政策。

<a id="persistence-foundation-initialization"></a>

### 8.4 初始化、版本和安全错误协议

**建库前身份准备与保留：** 可信装配方在调用CREATE_NEW、打开／创建目标或任何建库副作用之前，用其受控ID源准备一个目标database_id，并先保留“该身份＋本次目标资源绑定”。两种mode都通过resources.expected_database_id显式提供该身份；缺失、非法或不能证明绑定能力时在I/O前拒绝。存储服务不生成替代库身份、不从路径hash推导身份，也不在第一次成功返回时才让调用方获得身份。该值是资源身份，不是新配置参数或第二份数据库路径；路径仍来自统一快照，装配记录只保持其对应关系，不覆盖配置值。

调用方负责使上述记录在所承诺的恢复范围内可取回，至少覆盖建库执行进程退出；若调用方与建库执行者同进程，单纯进程内变量不足，须在发起前具备可跨该进程退出恢复的受控输入／装配记录，否则不得开始建库。合成测试可由未退出的父进程保留身份并交给建库子进程；不因此实现生产身份存储、路径发现或配置持久化系统。身份记录不是成功证据，也不向agent提供枚举或发现入口。

CREATE_NEW仅接受可信资源能力确认的不存在目标，以排他方式建立；遇到已存在文件、零字节文件、孤立WAL／SHM或竞争创建均拒绝，不将“空文件”当新库。只初始化当前发布的基础格式及显式装配参与仓储的Schema清单：库身份、应用格式标识、格式版本、模块Schema版本与必要表／约束在建库事务内一并发布。合成仓储定义仅在测试装配中存在；不在正式包预建测试业务表。

建库事务写入的database_id必须精确等于上述预先绑定身份。CREATE_NEW的返回无论是成功、UNCONFIRMED，还是进程退出导致完全未送达，都不改变调用方的保留责任。恢复方从原装配记录取回身份和对应目标，在原执行者已结束或可靠隔离后，用新服务OPEN_EXISTING并重新提供同一expected_database_id；不要求任何成功回执或UNCONFIRMED句柄曾送达。响应中若附恢复绑定，只能重复调用前已有的身份，不是恢复的唯一来源。

OPEN_EXISTING目标缺失即报错，不使用能静默建库的打开模式。识别应用格式后先比对期望库身份，再检查支持版本、完整Schema及一致性，全部通过才能授予仓储能力；不将实际库身份读回后自动替换期望值。应用版本与SQLite库版本分开核验。此范围只支持当前格式，较旧／较新版本、未知参与者版本、缺表／约束不符、未完成初始化分别拒绝，禁止自动升降级、补表、删除或“修复后成功”。无法识别完整建库元数据时报告未完成／格式故障而非接受未知身份；身份不匹配安全拒绝且不返回实际身份。恢复路径不再次CREATE_NEW覆盖，也不新增按路径读取／枚举身份的旁路。

已有支持格式按SQLite协议恢复其自身WAL／日志，再检查结构、外键和回执／审计必要关联的一致性，全部通过才READY；检查失败或超过等待期限就报告未就绪。检查不是介质无损证明。对不支持或无法识别的库不执行应用DDL、格式切换或自行修复；不静默移走、重建、清空数据库或删除伴随文件。失败释放本次自有资源；释放未确认则FAULTED并保持占用记录。运行库和逐连接设置的具体准入见[存储候选](deployment-candidates.md#sqlite-validation-candidate)。

<a id="persistence-foundation-errors"></a>

**固定失败协议（已批准）：** PersistenceError恰含code、operation、field、reason、cleanup_pending，均深不可变；不复用或扩充配置／运行诊断的错误枚举。错误不含任意message、异常／栈、SQL、路径、用户键值、审计内容或输入对象引用；已校验恢复身份仅在独立受限绑定中返回。签名使用错误按语言规则拒绝，无法构造安全结果的资源耗尽／进程控制故障不伪装成功。

operation是被调用的语义端口，不是SQL语句或下游私有函数名；允许值及结果固定如下。不存在的领域错误不能用NONE填进PersistenceError；NONE仅供无故障健康状态，成功封套无error。

| 端口／固定operation | 结果与失败承载 |
| --- | --- |
| initialize | READY(库信息)，或REJECTED(error)，或UNCONFIRMED(原目标绑定, error)。REJECTED只表示本次未就绪，不能推导目标文件不存在；建库效果待确认用UNCONFIRMED。OPEN_EXISTING无法确认原建库结果亦用此分支；输入／能力／已确定格式不符仍REJECTED |
| execute | §8.3的COMMITTED／NOT_COMMITTED／REJECTED／UNCONFIRMED；error只说明原因，分支由提交证据决定 |
| participate | 模块参与仓储的类型化读写统一使用此operation；仅STAGED(本地结果)或失败error，不授予提交确认。模块具体方法名不进入operation枚举 |
| read_receipt、read_object | 分别用于回执点查、模块对象点查；FOUND／NOT_FOUND或FAILED(error)。FAILED表示本次读取失败，不表示业务操作已失败或未提交 |
| resolve_operation | 与§8.3相同四分支，COMMITTED的source固定EXISTING；正常证实不存在为NOT_COMMITTED(None)，无新业务命令。非法请求／能力／键冲突REJECTED；有效绑定下的恢复查询故障或证据不足UNCONFIRMED |
| close | §8.2的CLOSED／INCOMPLETE报告附error或None；INCOMPLETE必须有error。资源占用取error.cleanup_pending，成功为false；不另维护一份可不同步的失败原因 |

创建服务和get_health不产生预期领域错误，不进入上述operation枚举；前者无I/O，后者只读内存。CLOSING与CLOSED对新请求统一SERVICE_CLOSED；FAULTED拒绝execute／participate／普通点读，仍允许受控resolve_operation和close。NEW只允许initialize、健康与close；READY再initialize报ALREADY_INITIALIZED。绑定能力先核验精确身份再读取内容，不调用伪造对象钩子。

下表是**完整code→reason集合及适用端口**，没有“代表性”、自由后缀或任意底层码透传。field限state、configuration、resources、format、operation、transaction、receipt、audit、query；每行给出其落点。

| code | 全部允许reason、固定映射及field | 适用operation |
| --- | --- | --- |
| INVALID_INPUT | INVALID_SHAPE：载体／固定字段非法；UNSUPPORTED_COMMAND：命令／结果确认输入版本不支持；LIMIT_EXCEEDED：输入编码超限。initialize的mode／资源输入定位resources；命令及恢复身份定位operation；点读输入定位query | 除close外；UNSUPPORTED_COMMAND仅execute／participate／resolve_operation，LIMIT_EXCEEDED仅execute／participate／两种点读／resolve_operation |
| ACCESS_DENIED | CAPABILITY_MISMATCH：绑定能力、作用域、库／执行者或UoW不符；field为resources（initialize）或operation（其他端口） | 除close外 |
| INVALID_STATE | NOT_INITIALIZED、ALREADY_INITIALIZED、SERVICE_FAULTED、SERVICE_CLOSED：按上述生命周期；field=state | 除close外，ALREADY_INITIALIZED仅initialize |
| CONFIGURATION_UNSUPPORTED | SNAPSHOT_REQUIRED：非原生快照；DEFINITION_MISMATCH：必要定义缺失／不符；CAPABILITY_MISSING：声明语义不受支持；VALUE_INVALID：必要值缺失／非法；依次检查，field=configuration | initialize |
| RUNTIME_UNSUPPORTED | SQLITE_VERSION_UNSUPPORTED：运行库版本不满足候选准入；SQLITE_CAPABILITY_MISSING：线程／WAL／同步／外键等必要能力或设置回读不符；field=resources | initialize |
| STORAGE_UNAVAILABLE | TARGET_MISSING：OPEN_EXISTING目标缺失；TARGET_EXISTS：CREATE_NEW目标或孤立伴随文件已存在；RESOURCE_INVALID：实际文件类型／路径隔离／资源身份不可用；OPEN_FAILED：其他打开失败；READ_ONLY：明确只读；NO_SPACE：明确空间不足；IO_FAILED：其余资源调用故障。field=resources | 所有含存储I/O的端口，close除外；TARGET_MISSING／TARGET_EXISTS／OPEN_FAILED仅initialize |
| RESOURCE_BUSY | ADMISSION_BUSY：无服务准入槽位；LOCK_DEADLINE：SQLite锁竞争累计等待额度耗尽；field=resources | initialize、execute、participate、两种点读、resolve_operation |
| FORMAT_UNSUPPORTED | FOREIGN_DATABASE：非本应用格式；INITIALIZATION_INCOMPLETE：建库元数据未完整发布；SCHEMA_VERSION_UNSUPPORTED：已识别应用／模块版本不支持；field=format | initialize、resolve_operation |
| INTEGRITY_FAILURE | DATABASE_ID_MISMATCH：已识别库身份与期望不符；SCHEMA_MISMATCH：声明格式的表／约束不符；DATA_INCONSISTENT：数据、回执或必要审计关联不一致；field分别resources、format、receipt（回执关联）或transaction（其余数据） | initialize、execute、participate、两种点读、resolve_operation |
| IDEMPOTENCY_CONFLICT | CONTENT_MISMATCH：同键内容或其既存版本不兼容；field=operation | execute、resolve_operation |
| TRANSACTION_FAILED | PARTICIPANT_REJECTED：模块拒绝或本地处理器普通异常；CONSTRAINT_FAILED：非幂等唯一键冲突的约束失败；RECEIPT_FAILED：回执构造／编码／大小或写入失败；AUDIT_REQUIRED、AUDIT_FAILED：按[审计映射](logging.md#transactional-audit-failure)。前三项field分别transaction、transaction、receipt，审计项field=audit | execute、participate；RECEIPT_FAILED及审计映射只由execute返回 |
| RESULT_UNCONFIRMED | COMMIT_UNCONFIRMED：没有更早原因但提交完成信号丢失；RECOVERY_UNAVAILABLE：没有更早原因但原执行者隔离或恢复证据不足。field=transaction | initialize、execute、resolve_operation |
| DEADLINE_EXCEEDED | OPERATION_DEADLINE：本次总等待到期；field=resources，不因COMMIT在途自动改写成另一reason | 所有上述operation |
| CLEANUP_INCOMPLETE | RESOURCE_CLOSE_FAILED：没有更早故障且自有资源释放失败；field=resources | initialize、execute、participate、两种点读、resolve_operation、close |

输入阶段顺序为生命周期→绑定能力→精确载体／字段→语义／大小→配置→资源，前序失败不执行后续步骤；端口不涉及的阶段跳过。初始化资源阶段先核验运行库和资源能力，再打开目标、识别应用格式及完整身份元数据、比较期望身份，随后版本、Schema和数据一致性；无法识别应用格式先FOREIGN_DATABASE，可识别建库残留但元数据不全先INITIALIZATION_INCOMPLETE，不能伪报身份已匹配。

数据库已返回结构化错误时，不解析异常文本：在幂等检查点的同键内容差异固定映射CONTENT_MISMATCH；明确约束拒绝映射CONSTRAINT_FAILED；明确只读、空间不足分别READ_ONLY／NO_SPACE；BUSY／LOCKED用有限等待协议，耗尽锁额度为LOCK_DEADLINE，总期限先耗尽则OPERATION_DEADLINE；结构／数据校验失败映射相应INTEGRITY_FAILURE；其他底层故障为IO_FAILED。回执写入和必要审计是有专属映射的边界：在那里发生的故障分别收敛到RECEIPT_FAILED或日志正文的映射，不再对外保留第二份底层异常。模块仅转发存储参与端口失败时保留该固定code／reason／field，将operation归为调用方execute；不能一概改成PARTICIPANT_REJECTED。

**原因与结果分开裁决：** 一次调用保留按上述顺序首次确定的code／reason／field，operation始终为该公开端口；后续回滚、释放、诊断错误不能覆盖它。cleanup_pending只说明本次自有资源尚未确认释放；已确认回滚且没有迟到提交可能时，即使清理未完成仍可NOT_COMMITTED。反之，无法证明回滚或原执行者结束时提升为UNCONFIRMED，保留原error并如实设置cleanup_pending，不能用“已有失败原因”证明全无。没有更早失败才选表中的确认／清理原因。COMMITTED证据已取得后，清理或诊断故障不改写业务结果，存入健康；close自己的失败报告不撤销旧回执。不可变旧报告不因迟到完成而更新。

少量组合例子固定优先级：

| 组合 | 唯一预期 |
| --- | --- |
| NEW调用execute，命令和能力同时非法；READY的initialize再传坏快照 | 分别REJECTED／INVALID_STATE／NOT_INITIALIZED、REJECTED／INVALID_STATE／ALREADY_INITIALIZED，不访问后续输入 |
| OPEN_EXISTING识别到不同database_id，同时缺少审计表 | REJECTED／INTEGRITY_FAILURE／DATABASE_ID_MISMATCH，operation=initialize；不补表、不返回实际身份 |
| 必要审计失败，之后回滚无法确认并有资源未释放 | execute返回UNCONFIRMED；仍为TRANSACTION_FAILED／AUDIT_FAILED、field=audit、cleanup_pending=true，不改成RESOURCE_CLOSE_FAILED。审计端自己的首错按其正文保留 |
| 普通业务SQL明确NO_SPACE，回滚已确认，但连接释放失败 | execute返回NOT_COMMITTED，仍为STORAGE_UNAVAILABLE／NO_SPACE、cleanup_pending=true；不因释放失败否定已有回滚证据 |
| COMMIT在途时总期限先到；另例COMMIT已明确成功后诊断或清理失败 | 前者UNCONFIRMED／DEADLINE_EXCEEDED／OPERATION_DEADLINE；后者仍COMMITTED，无业务error，故障经健康观察 |

健康至少包含生命周期、固定last_reason、在途写／读数量、未完成清理、checkpoint是否未完成及已知未决操作数量；仅限本服务观察，重启后不伪称覆盖全部历史故障。不返回文件路径、SQL、命令或审计内容。故障可经现有公开Logger由可信装配层记录其已批准白名单能表达的固定事件及错误分类，不另建logger或新增模块名／事件码；详细持久化原因通过本模块安全错误和健康观察，不直接塞入日志的error_code。错误返回和健康本身不依赖诊断成功；审计与诊断的故障隔离唯一见[日志正文](logging.md#transactional-audit-failure)。

<a id="persistence-foundation-configuration"></a>

### 8.5 配置用途、资源与补充契约导航

统一配置仍是所有参数及唯一默认值的所有者；存储和审计服务只通过原生EffectiveSnapshot的get_registry／get_entry／list_entries读取绑定定义与值。生命周期内固定快照，不读环境或配置文件、不私自resolve、不构造私有快照、不建立默认副本，不提供热替换或配置激活。真实配置持久版本尚不存在时不以Schema修订、内容hash或库格式版本充当config_snapshot_id。

**配置补充已批准。** 参数类型、范围、唯一默认声明及完整匹配现已集中在[配置§11.12.2](configuration.md#configuration-persistence-definitions)，入口、日志组、目录上下文、顺序和固定错误只在[配置补充正文](configuration.md#configuration-persistence-validation-contract)维护。本表仅说明消费用途，不构成另一份定义表；超时时取较小剩余预算仍是事务协议。

| 完整键 | 消费用途 |
| --- | --- |
| storage.database_file | 本次权威数据库的显式文件目标；文件及伴随资源由初始化核验 |
| storage.operation_timeout_ms | 每次初始化、执行、点读或结果确认的总等待上限 |
| storage.lock_wait_ms | SQLite锁竞争的单次操作累计等待上限，零值表示立即报告竞争 |
| storage.close_timeout_ms | 关闭调用的总等待上限 |
| storage.read_capacity | 同时占用的短读连接上限，无空闲槽位即拒绝 |
| storage.command_max_bytes | 冻结语义命令编码上限 |
| storage.receipt_max_bytes | 完整持久回执编码上限，含结果及关联元数据 |
| storage.wal_checkpoint_pages | 显式自动checkpoint页数阈值，不代表WAL硬大小上限 |
| audit.event_max_bytes | 完整单条审计编码上限，超限拒绝而非截断 |
| audit.events_per_operation | 每次新操作审计行数上限，历史读取采用§8.3的格式上限 |

固定协议规则包括唯一提交、同键冲突、未知不重放、格式拒绝和本次验证采用的同步要求；不能做成关闭开关。资源能力则包括实际SQLite连接工厂、目录／文件身份和所有权核验、期望库身份、受控执行与单调／UTC时钟、ID源及可选受限诊断句柄。它们由可信启动代码／基础设施注入，不携带另一份路径、超时、容量或同步配置以覆盖快照；期望库身份是持久资源标识，不是调参。资源适配器再次验证文件类型、路径别名／符号链接、可写性、数据库与诊断目录隔离和伴随文件归属，保证绑定同一实际文件；无法证明即拒绝，不把纯文本校验当真实安全证明。测试故障注入只存在于测试装配，不能经生产参数启用。

本组合所需解析能力见[已批准配置补充](configuration.md#configuration-persistence-validation-contract)，实现及验证版本见[STATUS](../work/STATUS.md)。参数仍只由统一配置注册和解析，存储不以私有校验、默认值或环境读取绕过入口。

现有[诊断公开接口](../../companion_memory/logging_service/__init__.py)和[结果](../../companion_memory/logging_service/results.py)只确认诊断准入／写出，不是审计或事务端口；[集成测试](../../tests/logging_service/test_service_integration.py)、[故障测试](../../tests/logging_service/test_service_recovery.py)、[真实文件测试](../../tests/logging_service/test_file_resources.py)不构成数据库持久性证据。独立审计由[同事务审计契约](logging.md#transactional-audit-contract)补足，不扩充EmitReceipt或借用运行日志队列保存审计。

<a id="persistence-foundation-provider"></a>

### 8.6 Provider依赖边界

本阶段仅提供使[T10发送登记](#t10)、[T11完成结算](#t11)及[Provider持久化要求](provider.md#source-line-664)可依赖的事务语义，不建立Provider表或进行模型调用。发送登记须取得COMMITTED回执后，Provider才能在事务外决定发送；登记UNKNOWN时只确认本地记录，不直接发送。已查回登记也不能证明远程尚未发送，远程尝试的恢复仍由[Provider未知结果协议](#source-line-518)负责。

完成结算可让Provider仓储与结果交接仓储加入同一UoW，回执确认丢失后恢复原本地结果；不重复计量，不触发新网络尝试。结果暂存、账本归属、预留及远程UNKNOWN不由通用事务基础解释或清零。此依赖通过合成的“登记提交后才触发事务外标记”和“完成幂等”例子验证，不以一个合成标记宣称Provider已实现。

<a id="persistence-foundation-acceptance"></a>

### 8.7 整体验收与证据分层

验收对象是完整公开流程，不是逐个内部类；须保留配置／诊断兼容行为。下表维护验收要求，实际覆盖与版本见[STATUS](../work/STATUS.md)，当前执行范围及类型检查按[统一验证规则](../CODING_STANDARDS.md#validation-environment)，不能将其他版本的通过结果外推为本组合验证。

共用合成场景：测试专属两个仓储分别持有source和target计数，初值10／0；transfer_units命令以固定对象ID、预期修订、scope=sample_scope、key=move_7转移3，成功为7／3并各增一次修订，要求两条模块变更审计和一份含原结果的回执。所有标识与审计值均明确合成。完整配置经拟补入口显式解析；可用测试值为操作时限1000ms、锁等待50ms、关闭1000ms、读容量2、命令4096字节、回执4096字节、checkpoint 100页、审计单条2048字节及每操作8条；路径在获准测试时由测试自有临时目录实际提供。这些值不是默认值或性能承诺。

| 场景与后续具体验证方法 | 整体预期 |
| --- | --- |
| 父进程在建库前准备并保留目标身份与资源绑定，子进程CREATE_NEW的COMMIT已成功，但任何initialize结果／UNCONFIRMED句柄返回前退出；父进程等待其结束后用新进程OPEN_EXISTING | 仅凭预先保留的同一expected_database_id和目标绑定确认完整初始化，库身份及Schema不变，不要求曾收到任何返回；改传另一身份为INTEGRITY_FAILURE／DATABASE_ID_MISMATCH。另例在建库COMMIT前退出并遗留文件时，无应用格式的空库为FORMAT_UNSUPPORTED／FOREIGN_DATABASE；可识别建库残留而元数据不全为FORMAT_UNSUPPORTED／INITIALIZATION_INCOMPLETE。均不补表、清空或自动修复（本例未执行） |
| 创建新库→绑定两个合成仓储和审计→执行transfer_units→受控读取业务、审计和回执 | 7／3、两条审计、一份回执同时可见；返回值和嵌套数据不可变，回执能表达原结果 |
| 分别在第一仓储写后、第二仓储写后、审计插入、回执编码／插入、COMMIT前注入故障 | 确认回滚／恢复核验后，新业务变更、审计、回执全无；此前无法确认时保持UNCONFIRMED。已存在的旧操作完整保留。审计插入后再使业务约束失败也回滚审计；调用者捕获参与者错误仍不能提交 |
| 对当前格式库close后新建服务OPEN_EXISTING；重复同一命令；再改变当前业务值后重复旧命令 | 已提交结果保留；重复返回首次原回执、不重写业务／审计、不重算旧结果。更改数量或预期修订但复用原键报冲突 |
| 真实COMMIT成功后在返回前截断响应；另用可控执行屏障使COMMIT结果暂不可见，再放行 | 前者按原身份查回原结果；后者先UNCONFIRMED，不重放，迟到完成后查回结果；旧报告不改写 |
| 单独持有旧读快照、写锁或阻塞I/O；再点查未命中／到期确认；原执行者结束后重新确认 | 旧快照NOT_FOUND、超时和无法读取均不能授权重试；只有符合§8.3全部条件才NOT_COMMITTED，后续同键最多一份效果 |
| 两个线程并发相同／不同内容的同键；另一连接及受控子进程占写锁超过预算；释放锁后恢复 | 一份已提交效果；竞争有限等待或立即拒绝；不部分写入、不伪造成功；同键不同内容最终明确冲突 |
| 注入打开失败、只读、写满、写I/O失败、回滚失败、永久阻塞和关闭失败 | 固定安全错误，未确认回滚不冒充失败已终结；在途资源不被并发复用，cleanup_pending真实，期限不逐步翻倍，不无限增生执行器 |
| 用测试自有副本准备较新／较旧格式、非本应用库、零字节文件、缺表／破损回执／孤立伴随文件；目标缺失时OPEN_EXISTING；路径别名／符号链接 | 明确拒绝；不自动建库、改版本、补表、清空或删除异常内容；受保护诊断与数据库资源不重叠 |
| 两端诊断关闭，或注入emit／文件sink故障，再执行受审计操作；用无审计能力的agent式句柄尝试读取 | 审计事实照常独立提交；诊断故障不能逆转COMMITTED。无授权、跨scope／库、过期UoW读取拒绝，无正文进入日志、错误或业务返回 |
| 合成登记命令提交前／未知时尝试触发事务外标记；提交后标记；合成完成命令重复 | 未确认登记不触发标记；提交成功后才允许编排推进；完成本地操作幂等。无HTTP、SDK或真实Provider调用 |

证据必须按层记录对应提交／工作区、解释器及实际SQLite库、操作系统／架构、文件系统与资源方式、命令、结果及限制，不把“测试退出0”扩写为下列所有层都通过：

| 证据层 | 验证方法与可声称范围 |
| --- | --- |
| 可控故障注入 | 内存替身／真实连接边界屏障覆盖COMMIT前后、响应丢失、锁和迟到I/O；证明协议分支，不能证明操作系统取消或介质持久性 |
| 本机真实文件与普通重启 | 自有临时库真实提交、close／重新打开；另启动独立解释器读取同一路径，检查原回执、计数、审计与完整性；只证明本机所测运行库及普通重启 |
| 受控进程中断 | 父进程在子进程“事务内未提交”和“COMMIT已返回但响应未发”屏障分别终止子进程，等待退出后重新打开核对全无／全有；无明确COMMIT证据的随机中断只要求无部分效果并经回执确认，不预言全无；不运行退出清理钩子，不混同正常close |
| Linux文件系统与容器停止 | 在拟部署Linux镜像、实际架构及具体本地卷／文件系统上重复上述流程，核验SQLite链接库、锁竞争、UID／权限、只读／空间不足、符号链接与伴随文件、停止后资源释放；报告实际fs和挂载选项。macOS结果不能替代此层，此层也不自动批准生产布局／G2 |
| 掉电／内核或宿主故障 | 需另行授权的可丢弃VM／设备或存储故障框架，记录同步调用与持久介质模型，在提交确认边界故障后检查已确认操作及全库一致性；真实设备掉电承诺还需文件系统、控制器和硬件缓存证据。SIGKILL、mock、FULL设置和容器重启均不足以单独证明 |

前两类流程及受控进程中断构成整体基础验收的本机证据；Linux层须独立列结果或明确未执行，未具备前不能声称Linux部署就绪。掉电与长期容量／性能不是本次基础验收可自动得到的承诺。若必要运行库不满足准入，则真实数据库部分为前置阻塞，不能仅靠替身交付完整持久化能力。

<a id="persistence-foundation-decisions"></a>

### 8.8 集中已批准决定与真实前置缺口

下列P1–P6已由用户作为同一整体契约批准，包含建库身份补充、固定失败协议及配置§11.12；不是实施任务拆分，既有所有权、事务确认和审计隔离要求保持原状态。后续配置补充、事务、审计及整体验证作为一次完整交付；契约批准不授权安装、实现、提交或部署。

| 已批准组 | 公开行为与关键保障的唯一正文 |
| --- | --- |
| P1 验证采用与资源范围 | [SQLite验证采用](deployment-candidates.md#sqlite-validation-candidate)：实际链接库准入、单库同步策略及合成资源采用；不批准生产选型／性能／G2 |
| P2 事务协作与生命周期 | [§8.1](#persistence-foundation-ownership)、[§8.2](#persistence-foundation-lifecycle)：受限模块参与、唯一提交、有界读写与等待、故障后停止写入、资源未释放不冒充关闭 |
| P3 幂等与确认协议 | [§8.3](#persistence-foundation-idempotency)：键作用域与内容冲突、持久原结果、四类执行结果、可信不存在证据和未知不重放、验证范围不清理回执 |
| P4 独立同事务审计 | [日志§10.9](logging.md#transactional-audit-contract)：记录Schema、必要审计完成条件、写入所有权、按操作受控读取、正文及诊断隔离 |
| P5 初始化、故障与验收范围 | [§8.4](#persistence-foundation-initialization)、[§8.6](#persistence-foundation-provider)、[§8.7](#persistence-foundation-acceptance)：副作用前准备并保留建库身份、显式创建／打开、版本拒绝、固定错误及结果证据映射、Provider依赖与整体验收证据分层 |
| P6 配置最小补充 | [配置§11.12](configuration.md#configuration-persistence-validation-contract)：显式入口、完整定义／日志组、目录上下文、全集合顺序与固定安全错误；[§8.5](#persistence-foundation-configuration)仅保留用途和资源边界 |

生产前置仍须独立落实[可信建库身份](#persistence-foundation-initialization)及[G2](logging.md#runtime-diagnostics-prerequisite-decisions)的生产路径、安全分级和完整资源清单，不能用合成夹具替代。已交付的显式配置校验、审计／事务及所测SQLite／Linux版本见[STATUS](../work/STATUS.md)；历史验证不证明新环境就绪，变更环境时按[验证规则](../CODING_STANDARDS.md#validation-environment)核验。

本节只维护已批准契约；当前授权及停止点见[CURRENT_TASK](../work/CURRENT_TASK.md)，验收和提交证据见[STATUS](../work/STATUS.md)。

<a id="provider-persistence-bridge"></a>

## 9. Provider有界文本持久化衔接契约

**状态：契约已批准。** 本节随[Provider整体F3](provider.md#provider-foundation-decisions)由主会话按用户授权审查批准，不表示用户亲自验收或实现通过。上文已批准§8、既有结果／安全错误、同事务审计值集合和旧格式行为保持不变。本节仅补充新Provider静态仓储的有界结果交接和非秘密执行配置证据；不建立其他业务模块，不新增SQL、裸连接、任意序列化或事务控制旁路。

静态源码核对：现有[ScalarSchema及编码](../../companion_memory/persistence/schema.py)仅接受boolean、integer、enum、identifier，Value已含str；[描述编码](../../companion_memory/persistence/_codec.py)将Schema语义纳入装配／命令指纹；[StatementPort](../../companion_memory/persistence/service.py)受限点读受回执字节预算约束。已批准增加独立`BoundedTextSchema(max_utf8_bytes)`，而不放宽identifier／enum或把正文拆成假ID。max_utf8_bytes须为精确int、闭区间1–65536；新字段仍通过Field明确缺失／null，新标量仅接精确str，UTF-8严格编码、拒绝孤立surrogate，接受正常Unicode和需转义的控制字符，不修剪／规范化／截断。先核对字符数≤字节上限，再在有界内严格编码核验字节数；内存与工作量受显式上限约束。

Value载体仍是str，不新增bytes、Decimal或任意对象。Provider自己的类型化payload／向量编码和业务格式校验由[Provider仓储契约](provider.md#provider-foundation-storage)拥有；基础设施仅验证静态文本边界及既有总命令／回执大小。execute／participate输入非法载体或UTF-8继续用现有INVALID_INPUT／INVALID_SHAPE，文本或总编码超限用现有LIMIT_EXCEEDED；受限点读中的存储／解码失败沿用既有STORAGE_UNAVAILABLE／IO_FAILED，不伪造不存在。Provider在自己的完整性核验中发现已存Schema／payload坏数据则映射LEDGER_INCONSISTENT并停止新尝试；通用回执损坏仍按原INTEGRITY_FAILURE／DATA_INCONSISTENT。均不透传原文或编码异常、不增加任意message或错误码，不覆盖既有首错；读取失败不能变成NotFound。

新Schema描述使用独立tag和max_utf8_bytes；旧Scalar／Record／Sequence所有描述、序列化字节、指纹版本与回执格式保持逐字兼容，无全局format_version升级。只在包含新Provider定义的新装配使用新tag；旧测试／旧装配仍可用旧库原样OPEN_EXISTING，旧程序或没有匹配新装配的服务继续按已有格式规则拒绝Provider库。不为新模块对旧库补表或迁移；若无法在此约束下保持兼容，停止受影响实现并提交具体差异，不自行改版本协议。

**审计限制单独保持：** AuditRequirement.change_schema仍只允许[日志§10.9.1](logging.md#transactional-audit-record)的布尔、受限整数、枚举、ID及有界记录／序列。可信装配时必须递归拒绝其中任何BoundedTextSchema，不能因复用freeze_value而自动把正文加入审计；原审计输入／错误／读取／总字节限制全部保留。通用提交回执仍只接受模块声明的安全结果；Provider命令结果Schema只含状态／稳定引用，不含新文本正文。

将新增描述与旧描述的逐字兼容、精确类型／UTF-8边界、控制字符／surrogate、文本超限、恶意子类钩子、嵌套审计文本拒绝、旧装配重新打开及新Provider库真实交接恢复纳入[整体合成矩阵](provider.md#provider-foundation-acceptance)。实际执行覆盖和结果见[CURRENT_TASK](../work/CURRENT_TASK.md)。此扩展不改变存储生命周期、已确认发送前置、审计必要清单、UNKNOWN确认和无自动迁移保障。

<a id="ingress-runtime-storage-draft"></a>

## 10. 持久接入与批次运行的存储装配和兼容（已批准）

**状态：本节新装配、结果绑定审计及兼容方案已获用户批准；§10.4生产方案仍待批准。** 属于[整体契约](durable-ingress-and-batch-runtime.md)；§4、§7、已批准§8／§9的确认、唯一提交、资源占用、格式拒绝及审计保证不变。仅在自有测试资源执行验证，不实现迁移；执行授权及证据见CURRENT_TASK。整体状态、行为和验收在主契约；本节只维护新装配、类型化事务参与、命令／回执兼容和生产资源边界；事务派生审计所需的精确增量见下文，未将其写入已批准§8／§9。

### 10.1 可复用端口与新增模块仓储

静态查看[持久化公开入口](../../companion_memory/persistence/__init__.py)、[声明类型](../../companion_memory/persistence/definitions.py)和[OperationPort／StatementPort](../../companion_memory/persistence/service.py)：现有公开能力支持静态RepositoryDefinition／CommandDefinition、有界记录／序列／文本、同UoW参与及按原键确认。已有[Provider仓储](../../companion_memory/persistence/provider_repository.py)使用固定SQL及只读投影；这证明有可复用的装配形状，不证明新的业务仓储已实现。公开端口不向领域调用者交出SQL、连接或任意表名；新增固定查询由基础设施承载。

拟议新完整装配包括原回执／审计／Provider，加如下独立模块Schema。模块所有权不因同库而变化，合成仓储只由测试装配增加：

| 所有者 | 逻辑持久内容及必要约束 |
| --- | --- |
| ingress | 入口注册、原始不可变事件、客户端身份／语义指纹；入口外部绑定唯一，入口内事件身份唯一；message_id原身份复核，禁止同键覆写 |
| buffers | 每入口序列计数、正常位置／历史引用／专注位置、三段冻结成员及原文引用、批次终结、目标占有、移交cursor／计数；同入口序列唯一、目标不得被两个活跃批次占有、终结至多一次 |
| runtime | 模式／epoch、唯一调度owner代次、工作／claim／转换／恢复检查点、受限准入代次、梦境发布引用；expected revision更新和当前owner约束 |
| configuration | [§11.14持久配置](configuration.md#runtime-configuration-identity)的域、版本、完整值／定义、组合快照和有效指针；不可变版本、精确引用，不存业务私有配置 |
| 受控学习结果所有者 | 对应批次候选、稳定对象ID／变更集及Provider交接引用；候选对单批次／run／owner绑定；正式认知未实现时用测试仓储 |
| 合成来源／媒体／发布参与者 | 同库合成对象、完整来源保护引用、媒体出现引用、梦境检查点／合成发布指针；仅验证事务及所有权，不发布正式业务能力 |

原始大文本使用已有BoundedTextSchema承载；事件、候选、配置等各有**领域**版本化确定性编码和硬上限，严格UTF-8、固定字段、未知字段／重复键／非法类型拒绝。通用回执仍只保存稳定ID／状态／修订，不能放入原始正文；审计change继续禁止BoundedTextSchema。不能把ID字段放宽成任意文本、序列化可执行对象、加通用blob表绕过类型和所有权。

复合唯一键、外键和状态CHECK在静态Schema中表达；终结和模式前置在同写事务重查。跨模块命令只能使各所属参与仓储在同一个UoW内操作，域仓储没有commit。只读视图通过固定参数与索引投影取得单次短快照；多行页有总编码上限，不能暴露游标／惰性迭代器。分页仅减少单次数据量，不能让缺失引用被当成无数据或让全库状态检查变成未经验证的就绪。

### 10.2 本地事务及稳定重建输入

完整操作沿用T01–T06、T09和必要审计，不改变T10／T11由Provider拥有。批次／候选／模式／页命令在首执行前固定身份、语义输入及预期修订，未知后不能换键或取当前最新材料重建“原命令”。冻结及候选不要求一次读回全部原文；成员与引用持久、按固定页／点读还原材料，最终摘要核验完整性。若完整有界变更集超过command限额，先用模块事务保存不可变受保护候选，最终事务只使用经确认的引用；候选保存自身仍有上限，不能靠拆分终结或无限分片规避原子性与容量。

<a id="runtime-result-bound-audit"></a>

#### 10.2.1 事务派生审计的最小增量（已批准）

静态基线的[prepare_command](../../companion_memory/persistence/_codec.py)在execute前冻结完整LocalCommand.audit_events，并连同定义、输入计算fingerprint_version=1；[服务](../../companion_memory/persistence/service.py)的_stage_audit要求实际事件与该冻结事件逐值相等。现有OperationPort.recovery_handle也须从同一完整命令重建指纹。因此“锁内生成entry_seq／mode、锁外预先填好完整审计”不能用现有接口直接同时满足；锁外预读下一序号、占位后覆写事件、去掉审计指纹字段或仅按键跳过内容校验均不可用。配置事务分配revision／snapshot_id同样受此限制。

推荐增量是**显式选择的结果绑定命令**，不是放宽旧命令检查。以下为已批准语义签名与固定行为：

| 新声明／载体 | 完整要求 |
| --- | --- |
| ResultBoundCommandDefinition | 保留原定义的owner、kind、command_version、输入／结果Schema、参与模块、required_audits及本地handler；另显式声明audit_intent_schema和版本化audit_bindings。只供新命令种类，旧CommandDefinition不隐式转换 |
| ResultBoundCommand(version, values, audit_intents) | 精确载体；values是全部稳定业务输入，audit_intents是按必要slot的完整稳定意图。actor_kind／actor_ref／reason_code必须来自已验证、可保留的意图；任何事务派生字段不得由调用方提供或用null占位 |
| audit_bindings | 每个必要事件的每个业务字段恰有一个静态来源：声明中的定值、意图中固定路径或最终类型化结果中固定路径；允许取一个有界完整子树，不允许表达式、回调、运行时字段名、SQL或读当前业务行。目标与change仍满足日志安全Schema；事件码／版本／slot由原AuditRequirement绑定 |

调用方须在首次execute前保留key、原values、audit_intents及绑定命令版本；接收方的稳定内部主体引用不因重投时凭据／网络追踪变化而改变，当前权限仍独立核验。prepare阶段一次深冻结上述全部输入，完整静态描述（含意图Schema、来源映射、必要事件及语义版本）一并进入类型敏感指纹；新命令使用fingerprint_version=2。该指纹承诺完整的“输入＋审计生成规则”，并不把尚未生成的事务结果当作请求内容。不得让任意旧事件缺字段后自动采用版本2。

执行仍先取得写所有权、BEGIN IMMEDIATE、按完整原身份／指纹去重。命中直接验证并返回原回执，handler、ID／时钟源及审计生成均不再执行。仅首次执行运行一次handler：各模块通过受限仓储实际变更，handler返回包含所需派生事实的安全结果；协调端口先冻结并核验结果，再由日志能力按静态映射物化完整审计事件，逐事件使用同UoW追加、核验必要集合，最后保存同一结果的原回执并唯一COMMIT。每个slot仍须有对应模块合法变更，不得靠handler返回一个声称发生过的事实绕过仓储前置和受信任模块责任。

结果Schema须包含恢复和审计投影所需的全部非秘密事实，例如接收序列／原路由、转换前后epoch、配置版本引用；不得把审计正文或完整命令藏进业务回执。接收时间只记录首次事务取得值，不能在恢复时重算。尚未提交的重新尝试可重新分配事务序列；任何已提交的结果标识只从原回执恢复，仍受§8.3“不重新生成”约束。

结果绑定slot由协调端口在结果冻结后使用日志写能力完成；模块不能在handler内自行提前填充或替换它。日志侧的物化、追加和错误边界唯一见[日志增量](logging.md#runtime-derived-audit-draft)。即使调用者捕获任何审计拒绝，该UoW仍不可提交；不以事后审计补齐。

<a id="runtime-audit-proof-compatibility"></a>

#### 10.2.2 持久证据、确认与版本兼容（已批准）

推荐保留基础表布局、外层库格式号及旧记录解释；仅新完整装配声明结果绑定命令及其新描述。新回执沿用原有字段和结果Schema版本，但明确fingerprint_version=2。必要审计元数据新增带显式materialization_version=1的结果绑定封套，保存原必要清单、静态映射描述身份、冻结的安全audit_intents、操作指纹版本／值和commit关联。此为日志所有的审计证据，不写入通用回执结果，也不保存完整业务values；它与业务、审计行及回执同事务发布。新写该封套受storage.receipt_max_bytes约束，历史读取受原格式65536字节上限约束，超限拒绝，不引入私有默认值。

提交前、OPEN_EXISTING全回执校验、去重、read_receipt、resolve_operation及read_audit均须核验：命令描述与记录版本匹配，证据与原回执指纹／commit一致，必要slot完整唯一，按“持久意图＋原回执结果＋静态映射”重建的事件业务字段与实际审计逐值相等，原审计ID／时间／库关联继续通过既有核验。不得从当前接收行、当前模式或当前active配置重算；这些可能合法变化或正文已合法释放。破坏任一派生值或证据关联均为完整性故障，不返回历史成功。这是新增关联保障，不承诺防宿主管理员篡改。

OperationPort的execute、recovery_handle、resolve_operation和read_receipt保留原职责；增量只使显式新命令接受上述载体／指纹2。recovery_handle(key, 原ResultBoundCommand)纯内存重建，不要求收到任何返回、知道原entry_seq／mode／snapshot_id或读取审计历史。调用方在可信存储就绪后用它resolve_operation：匹配返回原COMMITTED／EXISTING；不同原输入或意图为CONTENT_MISMATCH；可靠证实不存在才可同键重新execute；其余UNCONFIRMED保持不重放。read_receipt未命中仍不是不存在证据。

旧CommandDefinition／LocalCommand的描述编码、fingerprint_version=1、RecoveryHandle、回执及原必要审计清单字节解释全部保持，旧_stage_audit逐值相等要求不变。新读取器仅按装配中声明的命令类型选择1或2，不按“失败后试另一版本”降级；不重新解释既有operation_kind来绕过旧键。包含旧Provider命令与新命令的组合库同时保留两条明确路径，Provider记录仍用旧协议。旧程序／旧装配不识别新完整装配时按原格式／Schema拒绝，不自动补表、升级或改写旧回执。

新载体非法／未知版本／超限在执行前分别使用原INVALID_INPUT的INVALID_SHAPE／UNSUPPORTED_COMMAND／LIMIT_EXCEEDED，原同键内容冲突仍CONTENT_MISMATCH；读取新证据损坏映射原INTEGRITY_FAILURE／DATA_INCONSISTENT并FAULTED，审计端仍用AUDIT_INCONSISTENT，审计故障与结果证据映射见日志正文。无新错误自由文本、重放许可或诊断字段。跨能力、首错、回滚不确定性和cleanup_pending规则不变。

<a id="runtime-storage-startup-boundary"></a>

#### 10.2.3 存储启动能力边界

本范围推荐**不扩展存储initialize为可续进度扫描**。[现有initialize／_check_all_receipts](../../companion_memory/persistence/service.py)先验证目标／格式／Schema／一致性，再在受控读取事务内遍历全部回执及必要审计，完成才READY；没有可持久传递的扫描cursor。其一次storage.operation_timeout_ms预算不会因运行层分页而延长，也不会被runtime.recovery_timeout_ms覆盖。

存储启动超时／清理／确认结果继续按[§8.4](#persistence-foundation-errors)返回Rejected或InitializationUnconfirmed及首错，不映射成“已完成若干运行层检查点”。任何未完成初始化的服务均不给运行层仓储就绪能力。旧所有者仍在I/O时保留连接、槽位及路径，由健康／close观察释放或由可信进程隔离；只有实际结束后，才可在允许NEW状态重新初始化或显式新建服务OPEN_EXISTING，均从头执行完整校验。原建库确认丢失只用预先保留身份打开；未完成建库仍拒绝、不修复。

回执／审计随库增长，若完整校验长期超出已配置存储预算，重复启动仍可长期不能READY；运行层或Provider检查点不能解除它。受本契约“初始化后配置不可改且bootstrap须匹配”约束，同库也不能临时调高持久storage值来绕开；完整性损坏更不能靠增加期限忽略。若产品必须支持这类大库启动，须另审可跨调用的存储校验能力及其一致性快照、扫描期间写入隔离、版本绑定、资源和故障协议，或明确的配置／迁移方案；本契约不包含该扩展。分层启动与下一次合法操作只在[整体恢复表](durable-ingress-and-batch-runtime.md#runtime-startup-layers)维护。

### 10.3 新库、旧库与格式兼容

推荐继续在**自有临时本地资源**上使用[已批准SQLite验证采用](deployment-candidates.md#sqlite-validation-candidate)的运行库准入及WAL／FULL／外键等要求；这是本组合能力的已批准验证装配，不能将既有基础实验的批准扩大成生产选型。新业务只在明确CREATE_NEW的完整新装配中建表，之后以相同完整模块／命令清单OPEN_EXISTING。

基础库表布局、旧Scalar／BoundedText／Record／Sequence描述、旧命令／回执指纹、原ProviderSchema和旧错误字节解释保持不变；新结果绑定协议按[精确版本增量](#runtime-audit-proof-compatibility)区分，不能将“旧格式保持”写成没有任何新记录解释。新增模块有独立schema_version和固定声明，其新库的完整装配指纹自然不同；不通过全局格式号升级让旧库自动获新表。旧装配打开旧库应保持原键、回执及恢复行为；带新模块的装配打开缺表旧库必须拒绝，旧装配打开新组合库也拒绝不匹配。拒绝不得补表、清空、删除WAL／SHM、改身份或迁移。

新文本／事件等领域格式在静态声明中可区分，读取旧持久记录采用原格式硬上限和版本解释，不因新运行限额降低截断数据。无法解释的格式、缺配置域、破坏引用／终结唯一性、必要审计损坏均安全拒绝并停止受影响写入；不能把存量损坏归成Provider普通失败。未在上述结果绑定增量中定义的其他基础格式变更仍须先给出具体不兼容字段／旧记录影响，停止该受影响部分，不自行修复或恢复旧参考规则。

建库前可信装配先保留expected_database_id及资源目标绑定，至少跨执行进程退出可恢复；CREATE_NEW回执未送达也能用原身份OPEN_EXISTING。测试由父进程或测试专属受控记录持有该事实，不能从重新打开的实际库读取身份后反向当作“期望身份”。初始化配置尚未完成时的模式保持RECOVERING，参见[配置初始化恢复](configuration.md#runtime-configuration-identity)。

格式兼容测试须以基线旧程序／旧装配创建真实临时库，再用未来实现的对应旧装配打开、原键读取及继续原操作；另验新组合库重启。源码级比对和同进程内存结果不足以替代。该实验只使用自有测试库，不对用户存量数据库演练迁移。

<a id="runtime-production-prerequisites"></a>

### 10.4 自有验证资源与生产前置方案

以下是如果生产资源成为必要前置时提交用户的具体方案，**均待批准**；推荐本组合验收先限定自有临时资源，不让这些生产事项被夹具隐含决定。

| 前置 | 具体推荐方案 | 替代与影响 |
| --- | --- | --- |
| 生产路径与G2 | 部署方提交同一命名空间下日志目录、数据库及WAL／SHM／临时位置、媒体／上传暂存、同库审计／Provider、备份／导出全部实际绝对目录；说明同库复用和禁用能力。逐项确认路径敏感分级，再按真实资源身份、符号链接／别名、可写性及独占关系核验 | 本范围不选任何`/data`候选作默认。若路径属非public，新增统一配置敏感路径能力并批准后再接生产，不降低标签；五类目录未实存或只有远端时不能伪填空清单通过旧校验 |
| 建库身份与启动配置保留 | 在任何CREATE_NEW副作用前，由受控部署装配记录持久保留目标database_id、启动配置版本来源与明确资源绑定；文件如采用则用访问受限的原子写／同步方案，存储位置和恢复责任一并批准 | 独立可信外部配置／密钥管理系统可替代本地记录；仅进程内变量、读目标库反推或路径hash均不足。此方案不恢复／替代BOOTSTRAP_GUIDE |
| 生产观察鉴权 | 推荐交由已有受信任反向代理／身份服务认证，服务仅在明确私有监听上接受受验证的短期主体证明，映射runtime.observe／diagnostics.observe及入口集合；批准TLS终止、代理来源白名单、过期／撤权及秘密保管方式 | 独立本地账号／会话服务需额外登录、口令／会话保护与运维契约；测试会话、loopback或用户自报角色不替代生产鉴权。无既有身份服务时保留未就绪，不匿名开放 |
| 沿用生产存量库 | 先只读列明原database_id／完整装配版本和实际数据范围，批准维护窗口与一致备份；用独立离线迁移工具在备份副本构造明确新模块Schema，保留原回执／Provider记录及身份，验证引用／审计／恢复后再批准切换；迁移产生独立审计／报告 | 推荐新建空的新资源用于本能力验证，旧库保持可由旧装配读取；不自动迁移或补表。切换前原库仍为权威，回退须说明切换后新接收数据如何保全，不承诺复制旧备份即可无损回退 |

实际临时资源验证须记录目录所有权及测试范围，避免访问生产目录、填满宿主磁盘或变更系统权限。可通过自有临时SQLite页上限、独立子进程锁、只读打开及受控文件失败验证真实错误路径；模拟异常／线程阻塞另标证据。进程终止恢复不等于掉电、磁盘损坏恢复、备份有效、Linux或生产性能已验收。全部新增验证在取得实际证据前仍标未执行，见[整体矩阵](durable-ingress-and-batch-runtime.md#acceptance)。

<a id="formal-memory-media-storage"></a>

## 11. 正式记忆、来源与媒体的存储装配补充（推荐已批准）

本节是[集中技术契约](formal-memory-source-media.md)的存储与格式唯一补充，推荐方案已与其同时获用户批准。基础库身份、SQLite验证采用、唯一提交、结果绑定指纹版本2、旧命令指纹1、必要审计完整性、原键确认和无自动修复保证保持。新业务字段／状态及文件发布／GC语义在集中契约唯一维护，不复制在本节。

### 11.1 静态装配与格式边界

推荐新独立完整装配显式增加memory（当前对象／主体／来源／依据边／墓碑／待办）、cognition（候选清单／叶／原请求恢复关联）、media（blob／出现／引用／上传／处理／解释／GC意图）及logging_service的专属历史正文仓储。所有业务写由所属类型化参与仓储执行，应用协调者只组织同UoW；无通用JSON业务库、任意表访问、模块自开连接或文件读写SQL。新领域单表／查询可以复用基础StatementDefinition，但不得让memory写media引用表或借buffers直接编辑来源内容。

基础外层格式号及旧Schema描述编码不变；新增参与者独立schema_version=1，实际需改布局的ingress／buffers／runtime／configuration采用新模块schema_version=2及新装配清单。语义新增命令使用独立operation_kind（如accept_media_event、store_content_candidate、commit_content_batch），command_version=1和结果绑定协议；旧accept／stage／finalize及其结果Schema、审计映射逐字保留在旧装配，不能仅用同kind改handler而改变旧指纹。新组合只对其支持的新路径授予执行能力。

旧event_version=1／synthetic材料／候选及新event_version=2／complete_source材料显式分派，不按失败后猜另一格式。新配置参数完整编码版本2的DEL转义及容量证明只在[配置补充](configuration.md#formal-memory-media-configuration)维护，旧encode_entry版本1解释不改。新raw事件保留原T01身份原则，但记录完整event_version及映射版本；同一业务事件身份不得通过切换版本绕过去重。新真实装配不接旧合成READY授权；旧原回执恢复只能在旧对应装配，不把旧候选转正。若以后需同库混合读旧记录，须追加明确迁移决定，本次不自动提供。

新正文记录的JSON统一固定字段、键按Unicode码点排序、紧凑编码、严格UTF-8，C0（U+0000–001F）按六字节小写转义，其他合法字符直接UTF-8、不规范化（DEL不额外转义；配置条目的独立编码例外见前段链接）；文本内引号／反斜线按JSON规则。版本、整数及null均类型敏感。所有字段为基础已支持的标量／文本／有界结构；不用任意二进制进数据库来绕过领域及文件所有权。SHA-256摘要用于完整性和身份碰撞复核，不是审计权限或配置版本。

| 新格式硬边界 | 最大值与保存方式 |
| --- | --- |
| 新完整原始事件 | 8192字节历史格式硬限，新推荐写E更低；每事件最多2媒体，原其他引用／标识硬限保持；纯输入不授予文件权限 |
| 当前对象正文快照 | 4096编码字节；body≤2048原UTF-8字节，两评分理由各≤512；主体≤4，字段组合再受总量限；来源／依据关联有独立有界叶 |
| 主体身份记录 | 1024编码字节；外部主体标识≤512、label≤256原UTF-8字节，全部实际组合再核总量 |
| 理解记录 | 2048完整编码字节历史硬限；精确字段、各来源／状态元信息界、可变text及固定拒绝占位只在[理解封套](formal-memory-source-media.md#interpretation-envelope)维护，不以字段上限代替组合校验 |
| source成员与清单 | 单成员由≤1024头、事件、最多2解释重建；source清单≤4096，成员最多4用于当前新材料装配；领域历史格式最大成员128的旧含义不改，新装配超4须重新核算／批准 |
| 候选 | 最多8叶，每叶≤8192、清单≤4096，总硬限73728；每叶一个语义变更及其引用，非任意分片。candidate引用的当前对象／source／解释同事务核验，stage一次提交全部叶 |
| 每项对象关联 | source联系≤2、目标锚点≤2、辅助≤2、依据≤8，全部当前关联合计编码≤2048；新主体登记计入8变更叶，不能用内嵌无限主体列表绕过。需要超过上限整体拒绝 |
| 准备／逐出现工作 | 准备清单≤4096，最多4成员／8出现；每个工作／原请求描述各≤4096，消费者为独立有界行，不能无限累入工作JSON。phase、唯一领取、期限及去重语义仅见[媒体准备](formal-memory-source-media.md#media-preparation) |
| 释放计划 | 版本1，root≤4096；最多16个旧source叶、每叶≤8192，完整计划≤135168；每叶记录至多4个payload和8组出现／理解／blob代次的引用修订及效果，重复资源按ID复核一致。每叶头≤512、payload组各≤384、媒体组各≤768，合计8192；组仅含本计划必要ID、修订、动作位，原文／解释／任意holder清单不入计划 |
| 审计历史正文 | 每件≤8192，每操作≤8；内容格式与权限仅见[日志补充](logging.md#memory-history-audit) |
| 新只读与安全结果 | 正文点读每次一个≤8192叶；元信息行≤1024、页≤16且完整页≤32768；新命令安全结果≤8192、完整回执≤16384、必要证据≤32768；全局基础上限65536仍适用 |

这些硬限不是默认配置。新写还受[配置显式值](configuration.md#formal-memory-media-configuration)约束；历史读取使用其记录版本硬限，无法在预算内读取是失败／未知，不是缺失。不得将逻辑候选／材料整体塞入一个BoundedTextSchema(65536)后忽略点读最坏转义；完整编码仍须通过底层command／receipt限制。预检须在事务前对原始命令、结果Schema最坏包及必要事件进行，运行时对实际完整编码再次核验。

### 11.2 原子性、证据和清理

新候选事务一次写全叶／清单、原请求恢复身份和保护，不发布正式结果；终结只引用该受保护候选，写事务中验证完整摘要、当前修订、所有权／模式前置和引用目标，由各owner执行一次。不会因为命令只含ID而省略候选大小／内容完整性校验。参与者无法取消必要审计，任何失败毒化UoW。

终结安全结果至少有batch／candidate／source身份、terminal、目标范围／历史计数、对象ID和原／新revision列表、主体／关系／索引／依赖变更计数、引用取得／释放计数及三种能力来源；各必要slot通过静态映射取自己事实。按保存候选的三终态、是否发布正式对象及是否涉及历史正文选择有限静态命令变体，必要集合仍在execute前固定；没有memory变更的零结果／失败路径不冒填memory成功slot。结果计数可为零，但审计slot须有对应所有者合法变更；不能漏掉已写所有者的审计。通用回执不存正文和完整命令；历史正文关联完整性由日志能力在同UoW校验。

<a id="release-command-branches"></a>

#### 11.2.1 共享释放的固定分支与原键确认

本协议适用于对象修改／删除、最后source释放及批次／准备处置中受共享holder影响的释放；具体数据所有者和原子效果只见[来源释放正文](formal-memory-source-media.md#source-release)。全部命令在CREATE_NEW前固定装配：按语义家族（对象维护、source释放、三终态、准备处置）、不变的对象／候选效果和历史要求，再选择释放mask=`NONE/INGRESS/MEDIA/INGRESS_MEDIA`。mask只增加基础效果尚未覆盖的真实owner；同owner只一个slot。组合不可能产生实际变更的分支不授予执行能力，不能声明“可选必要slot”。读前置可以调用更多owner的只读参与端口，读不产生变更slot。

例：无媒体的非最后source对象删除只需memory变更及logging_service旧值；无媒体的最后source删除另有ingress引用／可能的payload释放；含媒体最后source删除再加media。即使原文仍被H共享，ingress释放SOURCE引用是真实变更，仍必要；buffers没有队列变化就不加入。准备处置基础为runtime，是否实际释放ingress／media引用由其持久清单及原计划决定；批次固定基础依三终态和冻结清单，不能照抄旧共享change。

**固定计划、分离两种键：** 对外逻辑root绑定原操作键及完整不变语义（对象expected_revision／变更值，或batch＋candidate＋终态）。维护／独立source释放的计划由memory拥有，批次／准备计划由runtime拥有；其类型化计划命令只写本owner的计划记录及对应必要审计，不修改业务对象。计划包含root、plan_ordinal、完整释放叶摘要、预期references_revision、确切变更mask、静态command_kind/version、执行operation_key及前计划处置。全部叶及root一次提交，确认后才执行业务命令；计划保存回执不是删除／终结成功。

业务执行键从root＋plan_ordinal＋计划摘要确定并先保存，指纹包含完整计划绑定，不能把新mask或引用修订塞回旧执行键。一次业务execute在BEGIN IMMEDIATE后、任何业务写入前重查对象／候选修订、所有资源引用修订及真实holder，重新计算取得和释放后的完整效果向量，并要求它与计划及所选静态分支**完全一致**。不匹配即PRECONDITION_FAILED/OWNERSHIP_CHANGED，事务不产生对象／source／payload／media变更或该次业务审计；已有独立计划审计如实保留。通过后各owner才按固定计划修改，root的唯一成功关联、业务效果、必要审计和原执行回执同UoW提交，root唯一约束防两个计划各成功。

**竞争后的合法动作：** 先确认旧执行键；只有REJECTED或可靠NOT_COMMITTED且旧writer已结束、root无已提交结果时，下一次显式本地调用才可保存plan_ordinal+1及新的执行键。仅重新读取引用形状，不改变对象expected_revision、候选、目标或终态；每次公开调用至多一个业务执行，不内置无界重规划循环。对象修订变化仍REVISION_CONFLICT，不能借重规划解决。旧计划／原键／处置关联保留，无在内存中临时新建必要集合；未知、NotFound观察、读失败、旧writer未结束一律不创建下一计划。前计划已失效且新计划保存确认未知，先确认该计划保存键，不跳代。

root的原键确认先读其计划链及唯一成功关联，再按已保存执行键／完整意图调用受控确认；返回成功计划的原回执及原对象／source身份。root重投相同语义在已成功时不重进参与者，异语义为CONTENT_MISMATCH。没有成功但旧计划执行未知时返回UNCONFIRMED，不能因root成功索引未命中认定未提交。跨进程从root／计划可重建全部原确认输入；一个root同一时刻仅有一个未处置计划，历史计划分页读取，不把历史无限装进root JSON。该增量只用于新业务家族，不改旧accept／finalize的原键字节和结果含义。

计划清单的逐叶点读仍≤8192；最大保存命令`6×135168+65536=876544≤1048576`。业务命令只引用已确认计划，在事务内有界重读；不能将最大候选、释放计划及旧历史正文作为三个字符串再一起塞入一个命令。写入每个旧source／payload／media的实际处置、分支及安全计数进入结果绑定证据；必要集合≤8、证据≤32768及摘要读取预算保持。新增全部静态分支描述须计入原装配总预算，超额先拒绝建库，不临时删审计定义来凑容量。

删除当前正文／候选／来源释放后，旧回执仍能独立解释原结果；指向已按契约释放正文的模块引用须可点读到RELEASED／DELETED处置元信息，不能既要求原件永久存在又允许同一引用直接悬空。引用处置不是修改旧回执。未决上传／处理中／T01／T03／GC恢复输入及所有权保留到可可靠确认，TTL不清除恢复责任。

文件发布及GC采用[独立有序协议](formal-memory-source-media.md#files-gc)，数据库原子性不含文件操作。媒体根和数据库／日志／导出／备份目录的资源身份显式绑定并隔离；只在自有临时资源验证，同文件系统发布、文件同步及目录同步未确认就不给READY。持久意图／代次和真实文件身份结合，不把路径文本或一个孤立文件当接收成功证据。

新增历史正文关联核验必须接入提交前、OPEN_EXISTING、重复命令、read_receipt／resolve_operation及授权审计读取；不能仅检查存在正文行。原完整性故障仍按基础错误映射并FAULTED。旧审计事件／指纹／物化证据逐字兼容，不能放宽旧change接受任意文本。主存储初始化仍一次全检，无新可续游标；媒体域的分页恢复不能替代它。

### 11.3 兼容与验证资源

新完整装配仅CREATE_NEW于明确空目标，之后以相同声明OPEN_EXISTING；新装配开旧库、旧装配开新库或任何模块版本／命令清单不匹配均拒绝，不补表、改身份、迁移、清空、移走文件或删除WAL／SHM。旧程序创建真实临时旧库后，未来实现保留对应旧装配的重开／原键／继续操作验证；不只有源码描述比对。新媒体根也须保存建库前绑定，可由测试父进程持有并交独立子进程，首次响应丢失不妨碍恢复。

修改现有配置或新装配需要不同声明并不构成现行正文冲突；旧装配及其已存格式保持原状态。若实现发现必须更改旧命令描述字节、基础格式号／审计协议或旧Provider语义，超出本补充所列精确增量，暂停列明位置、兼容影响和推荐选择，不自行降级。

没有自动存量迁移。必须迁移时的输入范围、旧合成性质、原回执／未知责任、离线副本、备份和切换决定统一见[集中兼容方案](formal-memory-source-media.md#assembly-recovery)。所有真实文件／SQLite／跨进程／故障／类型检查均为[整体验收矩阵](formal-memory-source-media.md#acceptance)，执行状态见CURRENT_TASK。
