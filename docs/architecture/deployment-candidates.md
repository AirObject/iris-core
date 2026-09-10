# 工程约束、规模与部署存储候选

> 本文件是本主题的现行正文，在此唯一维护。既有要求、已批准契约、建议和待批准事项保持各自状态；迁移不新增产品决定或实现授权。文档关系见[总入口](../INDEX.md)。

适用主题与局部定义：已给定工程约束、容量推导与待验证候选在正文分别保留；一个逻辑模块不等于一个进程或容器。本文不能作为生产选型已获批准或性能已验证的证明；本阶段SQLite验证采用的批准范围见[验证采用契约](#sqlite-validation-candidate)。

设计／审核参考：[冻结原始文档](../reference/companion_memory_module_design_provider_logging_config.md)。仅供追溯，不作为现行约束。

按关联工作联合阅读：[持久化承诺](persistence-and-transactions.md)；[正式决定待批准](implementation-options.md#source-line-1216)；[参考标记](references.md)。

返回[文档总入口](../INDEX.md)；实际进度见[工作状态](../work/STATUS.md)。

# 独立陪伴角色记忆与认知系统：技术架构与模块设计草稿

> 文档性质：模块与工程契约草稿，不是实现完成报告或最终技术栈批准记录。  
> 需求依据：[现行产品行为](../product/overview.md)，以及本文列出的部署、规模、时延、持久化和远程模型约束。
> 标识约定：[M01](../modules/ingress.md#contract)—[M15](../modules/configuration.md#contract)为逻辑模块；[I01](ownership.md#i01)—[I02](ownership.md#i02)为基础设施；[T01](persistence-and-transactions.md#t01)—[T13](persistence-and-transactions.md#t13)为事务与恢复契约；[V01](acceptance.md#v01)—[V90](acceptance.md#v90)为工程验收。模块不等于进程、容器、仓库或独立agent。


<a id="section-01"></a>

<a id="source-line-28"></a>

## 1. 架构结论与适用边界

本设计将**Provider、日志、配置分别作为独立的一等模块**。所有系统内部及受支持扩展发起的模型请求必须经过Provider；控制台、Web和文件使用统一日志记录与路由；所有可调参数进入配置注册表并通过版本化生效流程修改。独立是职责、端口和数据所有权独立，不意味着增加三个容器。

三个模块不合并为万能管理器：Provider负责调用控制和计量账本；日志负责事件记录、审计和输出；配置负责参数定义、权限、版本与生效；Web只组织交互并调用这些模块的受控接口。

首期建议采用**模块化单体：一个Linux Docker容器、一份权威业务数据库、按入口持久化的队列、受控的后台工作器、本地检索索引、内容寻址媒体目录，以及独立的Provider、日志、统一配置模块**。Web前端构建为静态资源，由同一应用提供，不额外运行前端开发服务器。

LLM、embedding、rerank及必要的多媒体理解均通过外部API完成；本地负责数据处理、规则执行、调度、检索和文件管理，不承担基础模型推理。首期不默认引入独立消息中间件、图数据库、远程向量数据库或多个自治agent框架。

建议先采用一个应用进程承载HTTP服务与异步调度，CPU密集的索引构建、文件校验等进入受限线程或子进程执行器。运行时只允许一个有效的实例级调度领导者。不能简单增加多个Web worker，让每个worker同时启动学习与梦境调度器。

模块边界以数据所有权和业务契约为准。候选技术替换不得改变三段式学习、失败终结、梦境隔离、对象级遗忘与删除、外部状态管理或实际使用反馈规则。

<a id="source-line-42"></a>

### 1.1 已给定的工程约束

| 项目 | 约束 |
| --- | --- |
| 部署 | Linux Docker，尽量单容器，不暴露公网；开发平台为macOS |
| 主体与入口 | 一个实例一个角色，首期2—3个具体入口，分别维护缓存与学习队列 |
| 输入 | 每天20,000—30,000条；峰值、活动时段集中程度和单条大小尚无实测 |
| 记忆 | 预计累计记忆数量不超过累计输入；这是容量预估，不是每条输入最多生成一条记忆的业务限制 |
| 多媒体 | 约5%—10%的输入涉及媒体，存在重复；相同原始文件按hash只保存一份 |
| 响应 | 基础响应目标为请求1秒内，不含可选rerank耗时；rerank默认关闭 |
| 持久化 | 缓存、梦境积压、正式记忆及业务状态均必须跨重启保留；允许损失范围单独定义 |
| 提交恢复 | 本地提交中断使用事务与幂等机制恢复、重试；不能误当作provider学习失败 |
| 模型 | LLM、embedding、rerank均走API，按协议兼容，不将具体供应商硬编码到业务模块 |
| 成本 | 批处理、缓存和有限上下文；默认不逐消息调用LLM、不频繁重新生成persona |

“不暴露公网”指服务入站边界，不等于离线：模型请求仍需要出站网络；发送给API的内容会离开部署机器。不得在界面中将单容器或本地数据库描述为“所有数据绝不外发”。

<a id="source-line-59"></a>

### 1.2 规模推导：用于设计，不是压测结论

由给定输入量计算，平均输入约0.23—0.35条/秒，30天约60万—90万条，365天约730万—1,095万条。平均值不能代替直播等集中时段的峰值。

5%—10%对应约1,000—3,000条含媒体消息/天。它不是已知的附件个数，也不是唯一文件数；一条消息可能含多个附件。唯一文件空间可按下式估算：

```text
每日唯一媒体字节 ≈ 每日输入数 × 含媒体比例 × 每条含媒体消息平均附件数
                  × 字节级不重复比例 × 唯一附件平均大小
```

“记忆不超过输入量”仍可能意味着百万乃至千万级累计条目。以100万条、每条1024维float32向量为纯计算示例，仅向量元素即为4.096 GB，尚未包括索引、正文、来源和关系。1024维不是选定模型参数。不能把小规模原型的全表向量扫描视为长期的一秒性能保证。

来源按完整消息ID和快照引用复用，媒体按内容hash复用；实际长期原始内容空间取决于被来源和队列继续引用的消息集合，而不是默认永久归档所有接收过的消息。


<a id="section-02"></a>

<a id="source-line-76"></a>

## 2. 运行结构与部署候选

<a id="source-line-78"></a>

### 2.1 单容器内部结构

```text
外部宿主/薄兼容层                 浏览器Web
         │                           │
         └──────── 同一HTTP服务 ──────┘
                         │
                  身份鉴别与运行门控
                         │
       ┌─────────────────┼───────────────────┐
       │                 │                   │
  低时延查询路径     持久化输入/命令路径     只读管理路径
  本地召回/信息拼装  短事务后确认接收        状态/计数/故障
       │                 │
       │          持久化任务及入口队列
       │                 │
       │       受控学习/媒体/索引/梦境工作器
       │                 │
       └────── 领域模块与统一事务边界 ────────┐
                                            │
                       权威数据库 + 媒体目录 + 派生索引
                                            │
                                                M13 Provider → 外部模型API

横切模块：M14 日志（Console / Web / File）
         M15 配置（Schema / Revision / Snapshot / Activation）
管理页面：M12分别读取Provider统计、日志查询与配置状态，不复制账本
```

这里的后台工作器是部署后程序的组成部分，不是额外的部署服务；异步处理也不意味着输入只存在内存中。

<a id="source-line-109"></a>

### 2.2 存储候选

**首期优先验证SQLite作为权威业务数据库，配合WAL和合适的同步策略；全文索引优先验证FTS5，本地向量索引通过接口选择具体实现。** 这是一项候选架构，不是已经通过百万级压测的性能承诺。

SQLite WAL可让读取与写入并行，但同时仍只有一个写者，并要求同主机的合适本地文件系统。因此采用短事务、受控写入和有限并发，而不是在一个长写事务内等待远程模型。[S01](references.md#s01)

需要严格的提交持久性时，候选配置采用`journal_mode=WAL`、`synchronous=FULL`、开启外键约束，并验证底层存储同步行为。官方文档区分了WAL下FULL与NORMAL的掉电持久性，不能为获得时延数字而悄悄调低持久性。[S02](references.md#s02)

使用经过验证的SQLite运行库版本；本次验证采用的已批准准入要求和实际链接库核验要求集中在[下节](#sqlite-validation-candidate)，不以Python包版本代替运行库证据。

FTS5提供tokenizer扩展能力。中文昵称、两字词、连续中文和中英混排需要独立检索测试；不把默认英文式分词效果当成中文效果已经达标。词法索引可以采用中文分词、字符索引和别名精确索引的组合，具体方案通过测试选定。[S03](references.md#s03)

向量索引只能返回候选ID，不能独自决定一条记忆是否仍有效。正式内容、修订与生命周期始终回权威数据库核对。来源关系先用带索引的关系表表达；受限关联展开不要求首期引入图数据库。

<a id="sqlite-validation-candidate"></a>

#### 持久化事务基础的SQLite验证采用（契约已批准）

已批准在[整体事务契约](persistence-and-transactions.md#persistence-foundation-contract)范围内验证采用Python标准库`sqlite3`适配器：同主机本地文件系统、一份数据库保存参与仓储、审计和提交回执，一个应用进程受控写入，独立连接用于受控短读取。验证仅使用测试自有临时资源与合成仓储；不批准生产选型、生产路径、G2或多实例部署，不包含FTS5、向量扩展、媒体和备份实现。此验证采用契约已随[集中决定P1](persistence-and-transactions.md#persistence-foundation-decisions)获批，待实现授权，不另设技术选型批准表；不表示Linux就绪、性能达标或掉电保证获准。

**运行库前置：** 官方已记录WAL-reset问题及修复：3.51.3及后续版本，另有3.44.6、3.50.7回补。已批准的验证准入下限为`3.51.3`，不默认接纳旧分支或无法核实补丁的定制构建；确需回补分支时须提出明确构建证据和准入修订。版本下限只排除已知问题，不表示后续版本均已验证。[SQLite官方WAL-reset说明](https://sqlite.org/wal.html)

后续获准执行时，先在实际项目解释器、再在目标Linux镜像记录Python版本、`sqlite3.sqlite_version_info`、`sqlite3.threadsafety`；用自有探针库读取`sqlite_version()`、`sqlite_source_id()`和`PRAGMA compile_options`，核验线程安全模式、WAL、外键及适配器使用的能力。Python的`sqlite3.sqlite_version_info`指向实际运行库；不得用命令行sqlite3、Python版本或锁文件替代该证据。禁止单线程构建进入并发服务。[Python sqlite3运行库与线程安全说明](https://docs.python.org/3.12/library/sqlite3.html#sqlite3.sqlite_version_info)

本阶段验证采用的已批准同步要求为上方WAL／FULL／外键组合；每个实际连接须在事务外显式设置适用项并回读，WAL返回值不符或能力不支持即拒绝初始化，不退回NORMAL、DELETE或内存库。外键开关在事务内设置不会生效，不能只检查发过设置语句。[SQLite PRAGMA说明](https://sqlite.org/pragma.html#pragma_foreign_keys) 持久性依赖操作系统、文件系统和设备正确履行同步请求，FULL不构成已执行掉电测试的证明。[同步级别说明](https://sqlite.org/pragma.html#pragma_synchronous)

连接上的SQL调用采用同步执行；服务的等待、并发和资源所有权按[事务生命周期](persistence-and-transactions.md#persistence-foundation-lifecycle)约束。Python适配器必须明确选择事务控制方式，确保显式BEGIN／COMMIT／ROLLBACK实际生效，不依赖会随Python变化的隐式默认；验证DDL、提交和回滚的实际边界。[Python事务控制说明](https://docs.python.org/3.12/library/sqlite3.html#transaction-control) 不共享同一连接并发执行，不开放shared-cache、ATTACH多库事务或网络文件系统；这些不属于本次能力。

后续重新打开、进程中断及平台证据要求唯一维护于[整体验收](persistence-and-transactions.md#persistence-foundation-acceptance)。当前未核验本机实际链接库，未运行上述探针、数据库操作、Linux或性能验证；运行库不足时报告前置缺口，依赖／运行时变更须另获授权，不自行安装或降低准入标准。给定容量与一秒响应仍是待验证目标。

<a id="source-line-123"></a>

### 2.3 持久化目录与网络

```text
/data/
  db/                   权威数据库及其WAL等伴随文件
  blobs/sha256/         不可变原始媒体，分层hash路径
  upload_staging/       尚未完成提交的上传
  indexes/              可重建的词法/向量索引快照
  logs/runtime/         结构化诊断日志分段文件，按大小/时间轮转
  logs/index/           可重建的Web日志查询索引
  audit_exports/        可选开发者审计导出，不进入agent工具
  config_exports/       可选脱敏配置导出；不是另一份在线配置真相
```

`/data`挂载到Docker命名卷或明确的本地持久化目录，不能仅放在容器可写层。Docker卷的生命周期可以独立于容器，但卷被删除、磁盘损坏或宿主机丢失仍需要备份解决。[S04](references.md#s04)

本机访问时发布到宿主回环地址；与其他容器交互可使用私有Docker网络。默认端口发布可能扩大可访问范围，因此配置时显式限定，而不是以“不打算公网使用”代替网络约束。[S05](references.md#s05)

建议生产容器非root运行，配置密钥通过环境或受保护文件引用提供；数据库、媒体和索引不暴露为静态目录。macOS用于开发，Linux镜像执行持久化、权限、停止与恢复测试。需要的CPU架构分别构建验证，不假定开发机与部署机完全相同。


图示／代码框中的文档标记定位：[M12](../modules/management.md#contract)、[M13](../modules/provider.md#contract)、[M14](../modules/logging.md#contract)、[M15](../modules/configuration.md#contract)。这些标记仅用于本文阅读，不能进入未来实现。
