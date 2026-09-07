# 后续构建指导：Core 集成闭环、Console 补齐与稳定发布

> 状态：In progress；2026-09-07 最新用户授权已启动 W01→W20 串行执行，W01 已独立提交，当前 W02。  
> 更新日期：2026-09-07  
> 起始代码基线：`e2a6bbd7793f7bd4b73a51d32149eb8e3015ed33`；Core 0.13.0 / Schema 20。实际开工时重新读取版本真源与工作区状态。  
> 依据：[完成情况核查](../reports/phase-completion-audit-2026-09-07.md)、[架构基线](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md)、[Console 设计](../design/console-backend.md)、[Phase 14 发布要求](phase-14-hardening-release.md)。

本文回答“剩余工作如何构建和验收”。[工作队列](work-packages.md)只维护执行顺序、状态与交接；阶段文档保留需求和退出门禁；ADR 定义领域语义；报告记录实际证据。本文的 W01–W20 是工作包标识，不是新 Phase，也不预占 Migration、API 或包版本号。

## 1. 目标与范围

先消除声明能力与真实执行路径不一致，再补齐既定管理功能，最后对同一候选完成生产验收。Phase 1–6、已交付的 Persona 管理、Console 读写/Forget、Required Lease、打包和开发 CI 作为已有基础复用。

Phase 11/12 继续 Deferred：不开发 Bellis/AstrBot 适配、不做其分发或专属宿主 E2E，不把它们算作已支持能力。仍须完成通用客户端、真实 Core 多进程、Lease/事件重连和权限隔离验证。自动扫描旧库、双写切换、Web 恢复、自动身份合并不重新进入范围。

稳定交付范围包含 Core 及既定 Console 管理功能；前端和 SDK 是独立产物。移除能力声明可作为临时诚实降级，但不能据此把原计划的 Vector/Graph 或其他未完成能力标记 Completed。收缩稳定发布范围必须单独记录决定及受影响需求。

## 2. 开工规则与通用交付要求

最新用户授权覆盖本文原“完成后停止、下一包另行下达”规则：严格 W01→W20 串行，每包全部验收、报告/队列更新并单独提交后立即续作，无需逐包确认。构建、测试、Soak、恢复通过后台任务的实际完成结果续作；2026-09-08 已按用户要求取消定时 automation；结果未完成不关闭、不跳包。具体状态见[队列](work-packages.md#当前执行授权)。本文下方原下达/停止描述保留为历史规则，以此授权为准。

每次只启动一个明确工作包，完成后记录结果并交接。实现可拆为可独立审查的行为提交；契约、迁移、生成物、代码及必要测试随相同行为一起交付。不要因本指南列出全部剩余项而启动一个贯穿发布的整体 goal。

每包开工先确认：当前 HEAD/未提交变更、依赖包报告、已接受 ADR、实际 Schema/Contract/SDK 版本及受影响路径。新建迁移使用当时下一可用编号，旧 SQL 不改写；Draft 分支上的 Schema 21 原型不能直接当作主线下一编号。

所有有写入行为的包同时交付以下证据：

- 服务端推导身份与 Scope/Privacy；Console 使用实际 CommandActor 与共享应用事务，保留 CAS、幂等、Evidence、Audit、Outbox 和 Tombstone 终检。
- source contract → 生成 OpenAPI/Schema/Fixture/前端类型一致；公共接口变化生成并审查候选快照，不为消除报错直接接受未知导出。
- 空库迁移、受支持旧库升级、旧 Worker/Reader 兼容和回退方式；停机迁移必须说明锁时与备份前提。删除/撤权账本覆盖新增 Canonical 对象。
- 对应的成功、失败、并发/崩溃及授权负例；测试验证真实行为，不复制算法后自行断言。计划规定的样本数不得静默缩减。
- Console 功能从发现元数据、契约、后端到生产前端构建连通，使用真实测试后端完成浏览器流程；mock 只记作前端测试。
- 新增可用能力须有调用证据；缺失或降级须被明确表达。日志、错误、指标不含 Secret、敏感正文或内部存储句柄。

完整共用收尾门禁见第 7 节，各包下方只列其专属退出条件。纯文档交付运行文档及差异检查，不因编写本指南执行全仓业务回归。

## 3. 建议顺序与依赖

**当前执行 W02：Graph 原量化门禁。** W01 HTTP Recall 接线与能力声明已完成并独立提交；原先能力声明与路由漏接线问题的修复和完整验收见 [W01 报告](../reports/w01-http-recall-assembly.md)。

| 顺序 | 工作包 | 对应原范围 | 必需依赖 |
| --- | --- | --- | --- |
| 1 | W01 HTTP Recall 接线与能力声明 | Phase 7/8/10；14.0-D/F | 当前 Core 基线 |
| 2 | W02 Graph 原量化门禁 | Phase 8；14.0-D | W01 的 Graph 路由 |
| 3 | W03 Persona 原量化门禁 | Phase 9；14.0-D | 当前 Persona 基线 |
| 4 | W04 Operation 扩展与可信备份流程 | 13.11 的最小前置切片 | 当前 memory_forget Operation |
| 5 | W05 Embedding Provider 全链路 | 13.9；14.2 Provider 装配 | W01、W04 |
| 6 | W06 认知 Provider 运行接线 | Phase 10；14.0-D/14.2 | 已有 ProviderGovernance；复用 W05 的适用运行配置边界 |
| 7 | W07 统计与运行观测 | 13.6 | W04；W05/W06 未启用能力按不可用表达 |
| 8 | W08 Persona Draft | 13.4 剩余切片 | W03、当前 Policy/Proposal/发布实现 |
| 9 | W09 Reflection/Candidate 管理 | 13.4 剩余切片 | W04、现有 ReflectionPipeline；真实模型验收使用 W06 |
| 10 | W10 Retention/Hold 与清理状态 | 13.5 剩余切片 | 现有 Forget；覆盖 W08 的新增草稿对象 |
| 11 | W11 业务导出与授权下载 | 13.7 | W04、W10 |
| 12 | W12 手动导入闭环 | 13.8 | W04、W08/W09 的草稿与候选落点、W10、W11 |
| 13 | W13 Settings 与实际生效 | 13.10 | 各被配置服务，包括 W05/W06/W07 |
| 14 | W14 其余运维与部署前读面 | 13.11 | W04–W13 中相应业务能力 |
| 15 | W15 兼容、产物与验收输入冻结 | 14.0/14.1 剩余闭环 | W01–W14 |
| 16 | W16 生产装配与进程验证 | 14.2 | W15；镜像与部署脚本可提前准备 |
| 17 | W17 安全、密钥与供应链 | 14.3 | W16；功能安全负例随各包先完成 |
| 18 | W18 性能、容量、Soak、进程故障 | 14.4 独立后台批次 | W15–W17、冻结 RC、固定环境与预算 |
| 19 | W19 恢复、升级、回退 | 14.5 独立后台批次 | W15–W17、与 W18 相同 RC、恢复输入 |
| 20 | W20 发布验收与交付 | 14.6 | W18/W19 合格证据及全部剩余门禁 |

这是建议调度顺序；独立准备工作可并行，但状态验收必须遵守依赖。W07 不因缺 Provider 数据而扩展开发模型管理；W08 使用既有 Hold 领域能力验证草稿保护，无需等待 W10 的 Console 管理页面；W12 依赖可信备份能力，不能等待整个 W14 的备份页面。W18/W19 的脚本提前开发，正式长跑另行下达；若并行执行，使用隔离资源，不能污染性能测量环境。

## 4. Core 集成与原阶段验收

### W01 HTTP Recall 接线与能力声明

**复用入口**：[HTTP 组合根](../../src/iris_memory_core/api/app.py)、[Recall 编排](../../src/iris_memory_core/application/recall.py)、[服务与 Worker](../../src/iris_memory_core/runtime.py)、现有 Graph/Vector 投影服务、[ADR-0015](../adr/0015-phase7-vector-recall.md)。

实现 API/Worker 共用的受控运行装配，注入 GraphRoute 和可用的 VectorRoute。索引根目录、租户、VectorSpaceConfig、Generation 与有效 Provider 配置必须一致。W01 先定义供 W05 消费的装配接缝，不提前复制一套 Console Provider 配置库。

能力协商按实际受支持运行能力生成，保留契约兼容；区分“支持但投影/Provider 暂不可用”和“未配置/不支持”。未知或缺失的必需能力失败关闭；可选能力按既有降级协议处理。测试 Provider 只能在显式测试/开发装配中使用，不能成为生产静默回退。

**退出条件**：通过真实 ASGI 和安装物/公共 SDK，构造只能由向量命中及需要两跳图扩展的样本，证明路由参与并返回预期来源；仅检查 HTTP 200 不合格。另覆盖首次无 Generation、Provider 超时/断开、模型不匹配、旧代回退、权限/删除竞争、Required Lease；capabilities、completed/degraded routes、partial 与 Trace 一致。原“宣称两路能力但 Trace 缺失且 partial=false”探针必须变为回归测试。W01 关闭漏接线问题，生产真实 Embedding 完成仍依赖 W05。

### W02 Graph 原量化门禁

**复用入口**：[Graph 测试](../../tests/integration/recall/test_graph_profile_graph.py)、[并发测试](../../tests/integration/recall/test_graph_profile_concurrency.py)、[原验收目标](../reports/phase-08-verification.md#原阶段验收目标)。

准备分别达到配置深度、扇出、节点上限至少 10 倍的对抗图。用真实 GraphRoute/HTTP 执行及可观察的 repository 读取、访问节点和截止时间计数验收，替换测试内复制 BFS 的自证部分；不要求构造无必要的指数级完整树，可分别设计保持各边界压力的稀疏图。

**退出条件**：实际读取/扩展/返回均不突破预算，Deadline 后不继续扩展；Binding、Redirect、SpaceGroup、Correct、Forget 与查询竞态每类至少 50 次，提交后旧字段/边/Cache 返回为零，回退原因准确。保留已有至少 200 案例的授权、状态、时间和墓碑性质测试；将原目标逐项映射到真实执行断言。

### W03 Persona 原量化门禁

**复用入口**：[Persona 领域与服务](../../src/iris_memory_core/application/persona.py)、[原验收目标](../reports/phase-09-verification.md#原阶段验收目标)、[ADR-0018](../adr/0018-phase9-persona.md)。

补齐 locked/manual/bounded_auto、字段 allowlist、单次/累计幅度、Evidence 数量/多样性/时间窗、冷却期、Stale Base 等每项至少 200 个固定种子案例；失败需能定位到具体性质。保留真实服务的授权/CAS/事务桥接用例，不只测试值域函数。

**退出条件**：50 客户端同基准发布恰好一个成功；Persona Current 与 Recall 顶层 Revision/Hash 一致。TTL 覆盖 UTC、前跳、回拨、Worker 暂停/重启 Catch-up，连续三次相同时钟轨迹得到相同逻辑修订推进与状态值，过期任务不能覆盖较新 State。回拨期间行为按 ADR 明确记录；如需改变过期语义，先补决策再改代码。通用客户端通知丢失/离线恢复与缓存协议的实际网络证明归 W16；Bellis/AstrBot 专属采用链保留 Deferred。

## 5. Console 与 Provider 剩余建设

以下包共同遵循 [Console 设计](../design/console-backend.md)。已有登录、读面、业务写入、Persona 发布/State/Proposal/Policy、Forget 和 Operation 取消保持回归，不从 mock 页面重新实现领域规则。

### W04 Operation 扩展与可信备份流程

**复用入口**：[Operation 服务](../../src/iris_memory_core/application/console/operations.py)、[现有迁移](../../migrations/0020_console_operations.sql)、[备份服务](../../src/iris_memory_core/storage/backup.py)、[受控归档服务](../../src/iris_memory_core/storage/admin_archives.py)、设计 §11。

当前表和服务硬绑定 memory_forget：kind、mode、preview/holds、500 条计数均有约束。先做 ADR 和增量迁移，分离共用元数据与类型专属 payload/权限/结果/取消边界；保留旧 Forget 约束和历史记录。所有执行继续使用既有 Outbox/Worker/Lease，不增加第二套队列，不开放任意 kind、模块名或函数执行。

以“创建并校验可信备份”作为首个真实新增消费者，交付 Operation 与内部备份结果引用；系统内部保管全实例备份，不让普通运营者获得跨租户内容、目录路径或下载能力。后续各包只在自己实现对应 handler 后注册其 Operation 类型；不先发布未实现类型。

**退出条件**：旧 Schema/旧 Forget 历史升级保持可读；新类型的授权、双 Worker fence、重试、取消、接管和恢复策略明确，不能套用 Forget 的 500 条约束。备份失败/未校验/摘要不符不能返回成功凭证；备份能力不可用能让后续导入提交 blocked。无需在本包完成所有运维页面。

### W05 Embedding Provider 全链路

**复用入口**：[HTTP Embedding 适配器](../../src/iris_memory_core/providers/embedding.py)、W01 装配接缝、W04 Operation、设计 §9。

交付不可变配置修订、draft→probe→activate/retire、回滚，以及配置存储→Worker 索引构建→API 查询三处一致接线。API 查询使用当前 Generation 对应的配置/向量空间；新空间构建完成前保留旧代和旧查询模型。模型、维度、metric、normalization、template/builder 变化触发重建；热限制变化不冒充空间切换。当前默认 deterministic 不能在生产隐式使用。

按设计提供 secret_ref 和 sealed；分别处理 Provider 主密钥和 Console 认证密钥。探测使用固定无业务文本，执行 DNS/实际连接一致校验、出站限制、禁止重定向、大小/超时/成本预算；未探测、超过 30 分钟或维度不符的配置不能激活。仅管理 Embedding，不扩展为 Console 认知模型选型平台。

**退出条件**：真实浏览器完成配置→探测→激活→观察重建→查询→回滚；真实允许的 HTTP Provider 与隔离测试服务分别验证正常运行和故障。初次启用显式降级、旧代连续服务、并发切换不混空间、跨租户不复用秘密、密钥/SSRF 负例均通过。没有目标 Provider 凭据时保留真实出站验收待办，不能用 mock 关闭本包生产接线证明。

### W06 认知 Provider 运行接线

**复用入口**：[认知适配器与治理](../../src/iris_memory_core/providers/cognitive.py)、[ReflectionPipeline](../../src/iris_memory_core/application/reflection.py)、[ADR-0019](../adr/0019-phase10-consolidation-transport.md)。

由部署配置选择受支持的真实认知适配器，替换默认 fake 的生产装配。复用既有四类 Port、timeout/retry/预算/熔断、来源 Watermark 与提交 fencing；不引入在 SQLite 写事务内调用模型的路径。模型输出只成为候选，仍由确定性协调和当前 Evidence/Policy 决定提交，不能直接修改 Persona Core、绑定或完成任务。

实施前登记支持的 Provider/model/version、最小出站字段、数据授权、成本上限及脱敏评估集。效果阈值在评估前确定，不能用 HTTP 成功率代表提取质量。没有配置时明确禁用相应生产能力，禁止以空候选的 fake 成功冒充正常处理。

**退出条件**：真实允许模型处理固定已标注材料，证明摘要/提取实际发生，合格候选经正常服务落地；记录质量和拒绝结果。429/5xx、超时、无效 Schema、越权 Evidence、来源删除、预算耗尽及重放均走既有治理；在线写/Recall 不等待后台模型。若最终选择禁用该稳定能力，必须明确记录范围裁决、收紧声明并保留未交付需求，不能单靠禁用关闭原功能目标。

### W07 统计与运行观测

**复用入口**：设计 §8，现有 RecallTrace、Outbox/Scheduler、投影水位和低敏指标。指标注册表只在服务端维护。

实现八个统计面、小时桶及日/周聚合、显式回填和真实前端。补充低敏持久耗时/覆盖起点，不能扫描可清理的 response_json 假装历史数据完整，也不重造与现有 Trace 不一致的计时。统计在当前可见集合上计算，预聚合维度必须足以按 Grant 过滤；不能按全租户聚合后只隐藏标签。

**退出条件**：至少三组 Grant 的统计无数量泄漏；即时只读查询按设计使用默认 250ms 语句预算，超时保留部分结果与 warning。缺失桶为 null，coverage_from、新鲜度、rollup lag、实例局部指标明确；回填走 W04 类型化 Operation，重复/中断不重复计数。Provider 等缺失数据如实不可用。真实浏览器验证指标发现、筛选、缺失/落后和回填，不再只依赖统计 mock。

### W08 Persona Draft

**复用入口**：当前 Persona/Policy/Proposal 应用服务；保存分支 `codex/phase14-persona-drafts-checkpoint` 的 `e639a2d` 只作为待审原型，开工时核对是否仍存在及差异。

先审查 Proposed ADR 与当前代码差异，冻结独立草稿生命周期和允许编辑/丢弃/发布的边界，再逐行为整合领域、持久化、共享 CommandActor、API、契约与 UI。不要整包直接合入旧 Schema 21 原型，也不要把“已发布 Revision”当可删除草稿。

**退出条件**：真实 HTTP/浏览器完成草稿创建、修改、冲突、丢弃及获授权的发布路径；Current/Policy 双 CAS、当前 Evidence、reauth 和防 Core 自动演进成立。Hold/删除账本与旧快照恢复不复活；新增迁移重排后验证全部支持升级路径。原 22 项局部测试只作历史线索，不能替代整合验收。

### W09 Reflection/Candidate 管理

**复用入口**：现有 ReflectionPipeline、只读 Console 资源、W04 Operation；设计 §5.1 与 ADR-0019 的 dry-run/replay 语义。

补齐遗漏的 dry-run、差异、replay、review/reject 和授权后的动作发现。明确可审核候选类型、审核决定与既有应用服务的映射，不把任意模型 JSON 直接更新到数据库。需要异步执行的动作通过类型化 Operation，来源/版本/审批变化在执行前重新检验。

**退出条件**：dry-run 执行不写业务 Canonical，不产生协调/领域提交/投影等派生 Outbox，也不产生 usage 激励；接收异步管理请求的调度入口与运行结果产生的后续业务作业明确区分。replay 沿固定或显式选择的版本集产生可审计结果，不自循环增强 Evidence。旧/删除/越权来源、无权限审核、并发决定、幂等重放均有负例；真实浏览器完成运行→查看差异/候选→审核/拒绝→刷新。投影重建能力由 W14 提供，不在本包另造索引控制器。

### W10 Retention/Hold 与清理状态

**复用入口**：[RetentionService](../../src/iris_memory_core/application/retention.py)、现有 Forget/删除账本与批量 Operation；设计 §5.3–5.4。

增加策略读取/修订、Hold 创建/释放及明确原因；补共享 CommandActor 事务入口，不能用自造 admin=True 绕过真实运营者权限。缩短保留期先预览影响、再有界扫描；Hold 保护内容与允许读取/导出分别判定。展示 Canonical 已删除与后台投影/Blob 清理的不同状态。

**退出条件**：预览后新增 Hold、批次间撤权/新增 Hold、释放后重试、后台扫描和 W08 草稿删除均遵守实际授权与保护状态；不得复活 Tombstone。十类已有 Forget 目标、相应 soft/erase 支持矩阵和分批上限保持；异步清理卡住有可见状态与重试证据；真实浏览器完成策略预览、Hold、阻止删除、释放及清理观察。

### W11 业务导出与授权下载

**复用入口**：设计 §7、W04 Operation、现有 Snapshot/Artifact/Scope 规则。`AdminArchiveService` 的内部表 dump 不能直接成为 `imc-data/v1`，必须按业务字段白名单和允许的来源闭包重新构造。

实现固定 Canonical/Tombstone 水位的一致快照、imc-data/v1 JSONL、CSV 报表、取消/过期/删除产物和有界字节流下载。备份、业务数据包、历史/审计报表分别处理，不导出凭据、配置、运行账本、投影或内部 locator；JSONL 与 CSV 不作同等无损承诺。

**退出条件**：跨资源快照不撕裂，来源闭包逐条授权；导出完成、下载开始和分块边界重验授权/删除水位，撤权或删除会停止后续发送并使产物失效。CSV 公式转义、hash/计数、过期清理、路径隔离和最大文件/Blob 回退内存均验证。默认 24h 保留、1GiB 最大文件、15 分钟生成预算沿用设计，不能当作已测性能；真实浏览器完成创建→进度→下载→失效。

### W12 手动导入闭环

**复用入口**：设计 §6、W04 的可信备份与 Operation、W08/W09 的草稿/候选落点、W10 Hold/Forget、W11 格式与来源约定。

按上传→解析/staging→映射/审核→有效报告→可信备份→commit→verify 实现，支持设计内 imc-data/v1 与 manual-records/v1。未知旧 Iris 格式先取得脱敏样本和映射 Fixture，不扫描源库或导入任意 SQLite。验证阶段无业务 Canonical 写入、无 Provider 出站。可分为 staging/审核和 commit/恢复两个可独立验收的切片，前者未完成时不发布提交能力。

按设计 §6.1–6.2 对拒绝内容逐类建立负例，包括合法 JSONL 中夹带的凭据/会话/权限、Provider 配置/Secret、Settings、Audit/Tombstone、幂等/Outbox/调度/投递/Usage、投影/缓存，以及 SQL/对象反序列化/压缩包/远程地址。默认文件 50MiB、100000 条、单条 256KiB、JSON 20 层、租户暂存 500MiB、同时 2 个任务均在服务端执行；缺失 Content-Length 也计数，重复 JSON 键和非有限数值拒绝，禁止动态映射表达式和路径穿越。

report_hash 绑定文件、解析/映射版本、权限、决定、冲突修订及来源/删除状态；备份由内部服务校验，失败时 blocked。批次按依赖拓扑提交，默认最多 100 条、事务目标不超过 100ms，必要时缩批；业务写入、Revision/Outbox、来源账本及 checkpoint 同事务。不能依靠新 dataset_id 或目标 UUID 绕过来源去重/墓碑。

**退出条件**：拒绝类型与服务端各上限均有负例；相同文件重提、不同内容同来源、每批边界崩溃续跑、双 Worker、撤权/Hold、过期报告和备份失败全部验证。已提交部分取消为 cancelled_partial；补偿只 Forget 本次新增且仍可安全删除的目标，不恢复整库。不得自动激活历史 State、完成 Task、发布 Persona 或把历史 Assistant 文本冒充已确认效果；Task 触发器默认 disabled，启用必须另行人工授权。JSONL 往返差异和总数守恒进入报告；真实浏览器完成上传、审核、blocked、续跑和补偿。

### W13 Settings 与实际生效

**复用入口**：设计 §10，当前 Recall/Focus/Recent/Reflection/Retention/Worker/Backpressure/Provider/Console 配置点。

建立类型注册表、agent→tenant→global→默认的分层解析、原子多键修订、历史/reset/rollback 和单调 settings 水位。注册表只发布确实接线的键；Provider 端点/密钥、Persona Policy、Retention Policy 仍是专属资源，不塞进通用设置。

**退出条件**：每个发布键都有“改值改变实际行为”的测试；online/worker/restart 生效边界、跨进程水位、失败原子性和 pending_restart 可见。历史衰减/Reflection 版本可复算，副作用按注册表执行且幂等。回滚产生新修订；未重启实例不能被前端显示为已应用。真实浏览器验证多键冲突、风险确认、保存/生效区分和回退。

### W14 其余运维与部署前读面

**复用入口**：设计 §11、W04 Operation、既有管理端点/Outbox/Scheduler/Archive 服务。

补任务分页/DLQ 重放、Schedule 查看/受控立即运行、Worker 心跳、各投影重建、备份列表/校验状态、只读 Audit/报表和 Console Ready 维度。重建使用现有 fenced Generation/旧代服务，不创建重复队列；同一 tenant/kind 的重复请求收敛。

**退出条件**：业务 Token 无法管理、操作只接受核准类型、不暴露底层队列 payload 或路径；DLQ 重放、新旧 Worker、取消/重启与同键重建验证。审计只读，备份不能经 Console 下载/恢复。导入进行中、rollup 落后按设计 degraded，不错误阻断无关 Core 读写；真实浏览器覆盖各实际运维流程。指标采集和生产告警演练继续交给 W16。

## 6. 生产与发布建设

### W15 兼容、产物与验收输入冻结

复用 [公开接口白名单](public-api.md)、现有 make ci、package-check、发布工作流与独立 SDK。重新建立“需求→能力/方法→权限/Scope/Revision/Lease→真实调用测试→产物”的双向矩阵；不能只检查路由有成功/失败响应。

明确 Core/Contract/Console/Python SDK/TS SDK 的支持窗口、平台和最低依赖，完成独立 wheel/sdist 及支持平台安装/升级测试。扫描依赖闭包、extras、入口与打包资源；无适配器/测试密钥泄漏，公开接口只返回值对象，黑盒消费不 import Core 私有组件。保留旧 rebuild 路径的既定弃用窗口。

**开测前必须固定的输入**：生产 OS/架构/Python/SQLite Allowlist、CPU/内存/磁盘与数据规模；支持的真实 Provider/model；Console 列表/统计 p95、导入吞吐、导出内存、RPO/RTO；操作人与日志/密钥保管方式。这些当前未决定的值保持待定，不由开发机实测倒填。

**退出条件**：所有声明能力和白名单方法都有真实服务消费映射；Core 与可选 Console 安装路径齐全，开发 SQLite Override 的验证不冒充生产安装。形成可执行支持矩阵、验收配置及 RC 生成流程。此时允许 W16/W17 继续改进部署/安全；W18/W19 开跑前才锁定最终不可变代码与产物摘要。

### W16 生产装配与进程验证

交付同镜像的 API/Worker 命令、生产 SQLite 组合、Compose、非 Root、只读根、数据/备份/密钥/静态文件隔离、资源限制/Healthcheck/Stop Grace、TLS/Host/Origin/可信代理配置。普通客户端使用独立 OS 身份且只持端点和受限凭据，不挂载 Core 私有卷。

接通实际指标采集与告警：Queue/Schedule lag、WAL/Checkpoint、RSS/句柄、磁盘、Provider/投影降级、实例 settings 水位。真实服务验证 Ready 先撤、请求/Job 排空、停止领取任务及资源释放。

**退出条件**：无需 `--allow-local-sqlite`/`--console-dev-http` 可启停；独立客户端直接读写 SQLite/WAL/SHM、FAISS、内部状态、密钥/备份成功数为零。真实网络多客户端验证 Lease/Epoch/Fencing、SSE checkpoint/重连/慢消费者、Persona Current/Hash 收敛；通用发布/丢通知/离线重连/缓存恢复/回滚链各至少 20 次。正常关闭默认不超过 30s，Provider 缺失/磁盘阈值/Worker 停止的 Ready 与告警行为符合声明。长时间和强杀矩阵归 W18。

### W17 安全、密钥与供应链

以最终功能面和 W16 部署执行跨 Tenant/Agent/Scope/Privacy、两平面、CSRF/Origin/reauth、下载撤权、上传/路径、Provider SSRF、私有接口/句柄拒绝矩阵。各功能包已经实施基础防护；此包补组合攻击、部署权限和恢复场景。

演练运营密钥、业务凭据、Session、Provider secret_ref/sealed 主密钥及备份验签密钥的签发/轮换/撤销。单独保护 console-auth.key；恢复旧快照后旧会话/已撤权凭据不得重新生效。实现备份静态加密与密钥分离，Manifest/Checksum/HMAC 继续用于完整性校验，不能把 HMAC 当作加密。

**退出条件**：生成实际 Core/前端/镜像 SBOM，归档依赖/镜像/许可证/Secret 扫描及处置。无未处置 Critical/High，例外须有负责人、期限与缓解证据；敏感数据越界泄漏为零；密钥缺失/错误/轮换中断、已撤权材料恢复等负例通过。SECURITY 文件存在不等于本包完成。

### W18 性能、容量、Soak 与进程故障

先交付有固定参数、总时限、清理和证据归档的运行脚本；实际执行作为独立后台批次下达，输入为 W15–W17 通过的同一冻结 RC、镜像/数据摘要和目标硬件。性能跑法遵循现有无 coverage 独立阶段，不与重负载测试争用测量主机。

使用安装后的公共客户端执行单条/批量 Observe、各路 Recall、State、Persona、Forget 和 Console；补冷/热索引、并发、真实 Provider 延迟、队列增长和大集合删除容量。保持 [Phase 14 量化基线](phase-14-hardening-release.md#量化验收基线) 中的门槛：Observe 30ms/100 条 Batch 150ms，Structured 50ms、FTS 100ms、Hybrid 250ms，State 25ms、Persona 20ms、Forget 100ms，均按原 p95/对应计量口径验收。

**退出条件**：连续至少 24h 混合 Soak；覆盖 Provider 慢/断、Worker/API Crash、FAISS 损坏、磁盘阈值、时钟异常和并发备份。真实 serve/worker 及声明的容器关闭边界各至少 20 次 SIGKILL；已确认 Canonical 丢失为零，重试不重复业务效果。资源无无界增长/永久积压，完整性与重建/Recall 抽样通过。输出延迟/容量/资源曲线、失败与恢复记录；失败保留现场并结束，不循环改候选或延长批次掩盖失败。

### W19 恢复、升级与回退

使用与 W18 相同的不可变候选，在独立环境运行三轮 Install→Seed→Upgrade→Backup→Restore→Rollback。业务 Seed 只用授权公共接口，备份/恢复/故障注入由可信运维执行；包括 W12 的实际导入数据链。

覆盖所有新 Console 表、来源/删除/撤权账本、Persona Pointer、Outbox/Tick、Artifact、独立认证/Provider/备份密钥与加密封装。恢复后检查 Integrity/Foreign Key，并重建 FTS/Vector/Profile/Graph；FAISS 文件不在当前备份中，不能假设恢复即就绪。禁止破坏性 Down Migration；兼容二进制回退与隔离备份恢复分别说明。

**退出条件**：三轮均达到开跑前冻结的 RPO/RTO；已删除内容、已撤销权限和不可用会话不复活，重建期间降级准确，恢复后客户端文件隔离仍成立。独立操作者按运行手册可复现，记录暂停/恢复写入、切换时刻与损失窗口。设定每轮/总时限，失败保留结果，不把局部恢复单测记作本批次成功。

### W20 发布验收与交付

建立不可变 Release Manifest，绑定 Commit、各包版本与 wheel/sdist 摘要、契约/Fixture、Migration checksum、接口白名单、SQLite/Provider/Builder、Console/SDK/镜像/SBOM、部署与全部测试报告。完成架构 §38 当前 Core 范围的双向追踪；Phase 11/12 标 Deferred/excluded。

复核实际发布产物范围：现有 workflow 可同时上传 Core 和 Python SDK，但 SDK 独立发布不能因为 Core 发包自动发生。实现或配置明确的产物选择、分别审查版本和 Trusted Publisher；Console 静态产物独立交付。先完成候选、清单和验收材料，使发布操作成为最后一步。

**退出条件**：同一冻结 RC 的完整 CI、平台兼容、W18/W19 和安全证据齐全；实际 GitHub 发布流程、环境保护、OIDC/Trusted Publisher 及 PyPI 注册表安装回读有证据。任何候选内容变化须评估并重新执行受影响验收，不能原地覆盖已签名产物。完成发布评审和用户授权的上传后，才能将 Phase 14 标 Completed；只通过构建或上传预览包不满足 1.0 门禁。

## 7. 验证、记录与交接模板

开发中按受影响范围验证，避免每次修改都跑全量。现有命令已经覆盖前端、SDK、安装与性能，无需重建 CI：

```sh
# 例：W01 的定向回归，实施时补入新测试路径。
make test-affected TESTS="tests/contract/test_http_operation_matrix.py tests/integration/runtime/test_release_surface.py"
make contracts-check public-api-check

# Console 包按需执行 check 和对应真实浏览器用例。
make console-check
# 首次准备使用项目规定的 Playwright Chromium；本机可使用已配置浏览器。
CI=1 npm run test:browser --prefix web/console

# 工作包达到可验收状态后执行一次。
make ci
```

全仓功能回归保留 80% 行/分支联合覆盖率门槛；性能单独无 coverage 测量，保留原预算和样本。失败先定位并复测受影响项，只在代码变化或明确集成疑点需要时再跑完整 CI；禁止重复无变化候选直到偶然全绿。浏览器报告逐条区分真实后端和 mock。性能本机通过不是生产 Soak 证明。

每包在 `docs/reports/` 保存可复现报告，并按以下内容交接：

| 字段 | 必须填写 |
| --- | --- |
| 范围与状态 | W 编号、原需求 ID、已完成行为及未完成项；无证据不得填 Completed |
| 候选 | 开始/结束 Commit，工作区漂移检查，实际 Schema/Contract/包版本及产物摘要 |
| 决策与数据 | ADR、迁移、兼容窗口、启用/回退步骤和数据不复活策略 |
| 验证 | 环境、依赖、真实命令、样本数、结果、覆盖率/性能口径，失败与复验分别保留 |
| 链路 | 权限/契约/应用事务/Worker/真实 HTTP/浏览器/安装物各层证据 |
| 后续 | 有名有归属的剩余项、阻塞输入、下一包可依赖的接口；不自动启动下一包 |

更新工作队列状态、阶段摘要、对接矩阵与证据链接；保留旧报告的历史语境。报告中的临时日志须在清理前归档重要输出，不能把 `/tmp` 当作永久发布附件。

本指南编写完成不改变任何工作包的实施状态。当前下一步是下达 W01，其余保持待执行；生产凭据、测量环境、预算和发布配置等输入在对应包开工前明确，不因尚未确定这些输入阻止无依赖的代码与文档准备。
