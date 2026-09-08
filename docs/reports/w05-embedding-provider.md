# W05 Embedding Provider 全链路

状态：In progress。范围：Console 13.9、14.2 Provider 装配；Phase 11/12 保持 Deferred。

## 当前集成状态（2026-09-08）

最新用户授权允许无依赖包并行并按功能小提交，覆盖下文历史检查点中的串行限制。W05 的本地实现和受影响验收已完成；真实允许外部 Provider 的端点、模型/维度、凭据引用和成本上限仍待提供，因此本包不标记 Completed。

`ci-002` 已运行完整 `make ci`：结构、类型、契约、公共 API、11 项性能、15,103 项功能（覆盖率 85.78%）、SDK、29 项前端及 23 项真实浏览器通过。最后安装包阶段因 PyPI TLS EOF 失败，原整轮结果保持 failed。`package-001` 用 `UV_OFFLINE=1 make package-check` 从本机缓存执行同一门禁，真实构建 wheel/sdist、独立安装、公共 API 和实际服务/Worker/SDK Recall smoke 均通过；未更改依赖或放宽检查。后续集成新工作包后运行新的完整 CI。

当前分组提交包括传输/秘密/存储/命令/探测/激活/恢复、按代运行绑定 `0115a1c`、Console HTTP `d8a9673` 和部署/轮换文档与测试 `9b15203`。契约、生产前端及证据继续分组收尾，检查点提交不代表本包外部验收通过。

## 开工基线

- 开始 Commit：`d4998edff6986180328a218440c8c239f26461a9`；W04 全部门禁通过并已独立提交，开工时工作区干净。
- Core 0.14.0、Schema 21、业务 Contract 1.11.0、Console Contract 1.1.0、Python SDK 0.11.1、TS SDK 0.11.2。
- 复用 W01 的 HTTP/Worker Recall 装配、现有 HTTP Embedding 适配器、Vector Generation 和 W04 类型化 Operation/Outbox/Lease。
- 不创建定时 automation；长任务保存会话、日志和检查点，依实际完成事件续作。

## 必需门禁

| 要求 | 状态 |
| --- | --- |
| ADR 与不可变配置修订，draft→probe→activate/retire 及回滚 | 实际 HTTP、真实浏览器生命周期、历史代回滚及清理后重建已验证 |
| 配置存储、Worker 索引构建、API 当前 Generation 查询一致接线 | 部署选择、真实 CLI Worker、ASGI 按代绑定及 Console HTTP/浏览器已验证 |
| 模型/维度/metric/normalization/template/builder 变化触发新空间；旧代连续服务 | 真实 ASGI Recall 与完整浏览器旧代/新模型切换闭环已验证 |
| secret_ref / sealed，两种主密钥隔离、租户秘密隔离 | 基础、启动、离线轮换及受控部署文件装配已验证 |
| 固定无业务文本探测，DNS/实际连接一致、出站限制、禁止重定向、大小/超时/成本预算 | 真实隔离传输及持久化探测预算已验证；外部授权成本门禁待完成 |
| 未探测、超过 30 分钟或维度不符禁止激活；初次启用显式降级 | 激活门禁、初次启用实际 API 降级与 HTTP readiness 已验证 |
| 真实浏览器配置→探测→激活→观察重建→查询→回滚 | 真实 Chrome、独立 Worker、HTTP Provider/Recall 闭环通过；23 项完整浏览器回归通过 |
| 真实允许 HTTP Provider 与隔离服务分别覆盖正常/故障；密钥/SSRF/并发切换负例 | 隔离 HTTP/TLS、故障及双租户 40 次并发查询已验证；真实允许外部 Provider 条件尚待提供 |
| 受影响验证、完整 make ci、报告/队列及小提交 | ci-002 除末尾 PyPI TLS 失败均通过；package-001 独立补验通过；报告/提交收尾中 |

无真实目标 Provider 凭据时保留真实出站验收待办；隔离测试服务不能代替生产接线证明。按最新授权，W06/W07/W08 的独立实现并行；W05 外部验收未通过前保持 In progress。

## 实施检查点

ADR-0049 已先行接受，明确不可变修订、Generation 与 Provider 在同一读快照绑定、既有 Outbox fence 发布、租户限定秘密引用和 DNS/IP 固定连接。当前环境未发现 IRIS_MEMORY_/OPENAI_/EMBEDDING_ 相关变量名；已请求真实目标端点、模型/维度、成本上限及凭据引用，继续实施独立边界和隔离验收，不将本包提前标为完成。

受控传输基础已实现：固定已校验 IP 连接、保留 TLS SNI/Host/证书验证、拒绝重定向/私网与元数据/混合 DNS 答案、请求响应大小限制、DNS 并发有界、总 deadline 覆盖缓慢响应头和正文。transport-003 的 41 项真实隔离 HTTP/TLS 测试通过；前两轮分别为 34 passed/6 failed、39 passed/1 failed，已定位修复并保留[原始批次证据](evidence/w05/completed-batches.json)。此传输仍待接入配置生命周期及实际 API/Worker，不能据此关闭 W05。

密钥基础适配层已实现：精确租户引用允许表、非阻塞且逐级拒绝符号链接的受限文件读取、独立 32 字节 Provider 主密钥、AES-GCM/AAD、脱敏视图和离线重封装原语。secrets-002 的 42 项测试通过；真实文件验证权限/链接/FIFO，实际密码库验证租户/config/revision 绑定、随机 nonce、错误主密钥及重封装。特殊权限位和错误属主用明确的 fstat 元数据注入验证；首轮 39 passed/1 failed 因 macOS 沙箱不保留 setuid 测试位，证据已归档。配置持久化、启动校验、完整离线轮换命令及 HTTP/Worker 接线尚待实现，不将原语视为完整功能交付。

Schema22 增量迁移及配置仓库已实现，0001–0021 未改写。Core 候选推进至 0.15.0，生成契约运行版本与安装 smoke 同步；旧客户端方法不变。内容修订不可变、sealed 密文可独立重封装，生命周期及 serving epoch 使用 CAS，数据库限制每租户一个 active/activating。仓库已接入 Transaction.providers。storage-001 的 51 项通过，包括实际 Schema21 备份/恢复/升级和旧 Forget/备份历史逐值相等，6 写者恰好一个成功、65 配置深分页与预算回拨。types-001 全仓 Python/TS SDK 类型检查通过。更广迁移/备份/恢复门禁 migrations-001 后台运行（session 17092、cell 431）；配置应用服务、真实处理器、公开契约与浏览器尚待接通。

扩大迁移/备份/恢复门禁 migrations-001 为 226 passed/2 failed；失败仅为模拟 Schema18 的 Task 恢复夹具保留了新 Provider 表，导致重走历史迁移时外键指向错误的历史结构。夹具现依据后续 SQL 表清单逐一确认空表后移除，保留所有 Task/删除账本/恢复断言；migrations-002（session 72726、cell 436）正在复验。

扩大迁移/备份/恢复门禁 migrations-002 已完成：228 passed，exit 0，终止时间 2026-09-07T20:38:14Z；原始任务标识、日志及结果已归档。W05 继续实现配置应用层与运行装配。

服务 open_store 与直接 Recall 装配现校验全部 sealed 历史，主密钥通过 IRIS_MEMORY_SECRET_KEY_FILE/ServiceConfig/RecallAssemblyConfig 传入；同时拒绝复用 Console 认证主密钥的路径或材料。新增 provider rotate-master-key 离线 CLI，需停机声明与实际创建并验证的备份，单事务重封装、逐修订审计，不覆盖主密钥文件。rotation-001 的 49 项通过：启动监听前拒绝、105 条修订跨游标批次、后行损坏导致已处理行和审计一起回滚、真实备份以旧密钥恢复。types-002 全仓类型门禁通过。

ConfiguredEmbeddingFactory 将不可变内容修订、租户及实际解析凭据指纹绑定到实例；查询/构建与固定最多两条探测使用 PinnedEmbeddingTransport，保留限流熔断，实例缓存有界。configured-002 真实回环 HTTP 的 9 项通过；configured-001 的 9 个端口夹具因沙箱禁 bind 未执行请求，提升到获准本机验证后通过，原始结果均保留。探测现在独立报告实际维度和规范化，configured-003 正在扩大到传输和秘密回归。以上尚未接通持久化探测预算、完整 Console 生命周期或当前 Generation 动态绑定，不视为 W05 全链路交付。

configured-003 已通过 100 项（真实 HTTP/TLS、配置实例、固定探测、秘密与启动轮换）；types-003 全仓类型通过；runtime-001 的 84 项既有 HTTP Recall/故障/发布面与 CLI 回归通过。新增[离线主密钥轮换手册](../development/provider-secret-rotation.md)。

草稿应用层 ProviderConfigCommands 已接入实际 Console principal、创建前与事务内权限复验、Key revision/Grant fingerprint 不变检查、CommandActor、幂等和审计。创建/PATCH/discard 保留不可变历史，保留 sealed 密钥时按新内容修订重新绑定 AAD；读面及幂等回执仅含脱敏字段。config-commands-003 通过 16 项，包含实际另一租户凭据读/改/丢弃拒绝、system.read 与 providers.manage 分离、重放载入后撤权、6 并发写者恰好一个成功。初轮 10 passed/1 failed 因证据断言使用错误表名，改为实际 idempotency_records 后通过；types-004 仅测试帮助函数 Any 返回未注解，修正后 types-005 通过。探测/激活/回滚处理器、HTTP 路由和持久化配置到 Generation 接线仍待实施，尚未发布新 Operation kind。

types-006 已通过最终草稿权限测试加入后的全仓 Python/TS SDK 类型检查；当前没有未收取的后台验收结果，下一步为异步探测处理器。W05 仍 In progress；W06 未启动。

## 异步探测及激活门禁检查点

新增实际 `console.embedding_probe` 处理器并通过 Phase14 Worker 有限注册。探针文本与 31 字符预算常量统一置于 Domain；每次实际尝试前在短事务中校验真实租约 owner/generation/expiry/source revision、操作/配置状态及 Operator 权限，再预留租户额度。网络在写事务外执行，最终由既有 Outbox 完成 CAS 同事务提交；失败也消耗额度，服务重建不清除额度。默认每 60 秒 12 次/372 字符，部署可信参数可进一步收紧；这不是后台任务定时推进，也不替代真实外部 Provider 的授权成本上限。

探测结果保存实际维度/规范化与固定 outcome，不保存向量或原始响应。实际观测时间独立于提交时间，防止迟到提交把旧探测刷新为新鲜。提交前复验当前权限与实际凭据完整指纹；取消、死信、权限变化及凭据变化解除配置在途意图，保留旧服务代。观测维度允许 0..33554432（受最大响应字节硬界约束），以如实记录空输出/超出支持维度的硬失败；可配置空间维度仍为 1..65536。

`probes-001` 的 28 项真实 HTTP/Worker 验证通过，包括两个 Worker 租约接管、过期 Worker 禁止发布、取消前/后网络边界、限额持久化、密钥变更、recent reauth、真实死信释放。扩大旧 Operation/Outbox 回归 `probe-regression-001` 为 135 passed/1 failed，唯一失败为启用 Job 清单遗漏真实新增处理器；更新显式清单后 `probe-regression-002` 的 90 项通过。`types-007` 的测试常量旧导入修正后 `types-008` 全仓通过。

ProviderActivationPlanner 已实现成功探测/完成 Operation 绑定、最多 30 分钟（边界含等号）、时钟回拨拒绝、实际凭据指纹复验；计划在一个读快照绑定配置、服务代/向量 epoch、删除水位及实际 Canonical 资源数，空间/端点/适配器身份变化必须提供准确 `rebuild_plan_hash`。当前仅交付门禁与计划计算，还没有执行激活发布或回滚；不将计划测试视为完整切换证明。`activation-plans-001` 的 8 项及扩大后的 `probes-002` 共 444 项通过，包含真实 FAISS Generation 的热限制与端点/模型/维度变更判断。

Console 候选契约推进至 1.2.0，业务契约仍 1.11.0。新增已实现的 embedding_provider Operation 类型、provider_configuration phase 和归一化问题码，生成正反例保持 Forget 500 限制及 Backup 类型约束；通过实际 Console HTTP 列表/详情/问题页读取已执行探测结果。`structural-001` 的全仓 format-check/lint/typecheck/contracts-check 通过。原始日志、失败批次及终态都已归档，当前无尚未收取的后台结果。

仍待完成：真实激活/回滚处理器、COW 发布与旧代连续服务、按租户 Generation/Provider 一致的 API 查询及 Worker 增量装配、部署引用/出站策略装配、完整配置 HTTP/Console 浏览器闭环、恢复验证、真实外部 Provider 和最终 make ci。W05 保持 In progress，W06 未启动，Phase11/12 Deferred。

## 实际 COW 激活检查点

ProviderActivations 与 ProviderGenerationRuntime 已接入真实 `console.embedding_activate` Worker。接受时将服务器计划固化为内部 plan_json，Schema22 增加只允许 Generation 结果字段变化的 Provider Operation 意图触发器。模型/空间变化调用实际 VectorProjectionService.prepare_generation；每批 Embedding 请求前后复验取消、租约、权限、当前服务 epoch 及已验证探测。索引准备不触碰当前指针，旧模型与旧 FAISS 代在准备结束后仍可查询。

最终通过既有 Outbox 四项 CAS 的同一事务完成 Vector 指针、旧配置 retired、新配置 active、Generation 原始配置关联、当前 serving epoch 及 Operation 完成。热限制变化先加载并验证实际当前 FAISS 文件，再只切配置绑定，保留原始 Generation 关联且不额外调用 Embedding。发布成功后才发送 Generation 指标。

`activations-001` 8 项真实 HTTP/FAISS 测试通过。扩大 `activations-002` 为 47 passed/1 failed，发现真实并发 Canonical 写入触发的 snapshot_moved 被通用 ConflictError 判为永久失败；现仅将这一明确的临时快照竞争映射到既有 Outbox 有限次数/退避重试，其他冲突不放宽。`activations-003` 12 项通过，涵盖按批取消、取消后禁止发布、权限/凭据变化、两个实际 Worker 接管、Vector 已写入后中途失败导致整个事务回滚、并发新增资源重新构建后再发布。`structural-002` 全仓结构/类型/契约门禁通过。

上述旧代查询证明使用真实 Provider 与 FAISS 服务直接在读事务中验证；API 的自动按代查询绑定仍未交付，不能将此替代真实 HTTP Recall 端到端门禁。最新扩大批次 activations-004（session 30434，supervisor 1137）正在验证实际激活、不可变计划、探测、备份、Outbox、迁移与 Console 契约。回滚和后续接线仍待完成，W05 不关闭。

activations-004 已完成：567 passed，exit 0，结束于 2026-09-07T21:42:20Z；包括最终不可变计划触发器验证。types-010 最终新增测试后的全仓 Python/TS SDK 类型检查通过。所有后台终态与原始日志已归档，当前无在途验收；下一步继续实际历史代回滚及 API/常规 Worker 按代装配。


## 历史回滚与恢复检查点

ADR-0049 先行补充历史代复用要求，随后实现真实 FAISS 文件及摘要、原始配置身份、完整 Canonical 内容/修订/surrogate 集合和删除水位的双重校验。可复用时在同一 Outbox 发布事务复活旧代、恢复 id-map 空间及成员戳并切换 Provider/Vector 指针；文件被清理、损坏、Canonical 变化或准备后文件失效时以历史模型重建，沿用有限重试和租约 fence。热限制配置回滚保留原始 Generation 绑定。退休配置可重新固定探测，成功、失败、取消均保留 retired 状态。

rollbacks-001 的 12 项真实 HTTP/FAISS 验证通过，types-011 全仓类型通过。真实备份恢复路径另发现 Provider serving 在 Vector Generation 删除后悬空，以及在途 Provider Operation 被当成 Forget 解析的缺口，现已修复。恢复事务先撤销服务和派生关联，清空当前探测与在途意图；保留不可变内容修订、秘密、历史探测、历史 Generation 标识、探测预算及稳定 ID MAP。原 active 进入 retired，需要当前权限重新探测并重建；旧操作阻塞为 restore_requires_review，既有排队/租约任务不能发起外部调用或发布结果。

恢复不变量新增配置/服务/Vector 指针、空间六字段、原始代绑定、探测所属修订及 sealed envelope 完整性检查。restore-001 为 2 passed/6 failed，失败为恢复测试装配遗漏 IdempotencyManager；restore-002 为 2 passed/11 failed，失败为测试历史时钟与真实恢复时间不一致以及错误用 immutable 读取尚未检查点的测试 WAL。修正夹具后 restore-003 的 13 项通过；没有放宽恢复门禁。types-012 全仓类型通过。structural-003 发现历史回滚改动的三个 lint 问题，等价修正后 structural-004 全仓 format/lint/import/docs/types/contracts/compatibility 均通过。

restore-004（session 21890，supervisor 11124，原生完成监听 cell 584）继续扩大至 319 项，增加已准备结果在恢复后迟到提交的场景，并覆盖既有完整 recovery、migration、Vector 和秘密轮换回归。此批后台终态尚待收取；不提前声称通过。最终 make ci 尚未运行，W05 不关闭，W06 不启动。下一步仍为按租户及同一读快照绑定 API 查询、常规 Worker 增量/重建、部署配置与完整 HTTP/浏览器闭环。真实允许 Provider 凭据及成本授权仍待提供，继续独立可做工作。

restore-004 已完成：319 passed，exit 0，结束于 2026-09-07T22:15:31Z。包括探测/激活/回滚各自 queued、leased、prepared 三种旧工作恢复后禁止发布、重新探测和实际重建成功；历史迁移及既有恢复回归均通过。原生会话及完成监听均已收取，全部原始批次（含失败）与候选清单已归档；当前无在途验收。W05 保持 In progress，后续工作按任务完成事件推进。


## 按代查询与常规 Worker 接线检查点

新增 ManagedVectorProjection，Recall 的同一读事务先验证租户/config 内容修订、已完成探测、服务指针、Generation 原始身份及全部空间字段，再绑定精确凭据实例进行 query embedding 和 FAISS search。进程内有界缓存含 tenant/config/revision/内容摘要/实际凭据指纹；并发切换保留旧读快照模型，新请求使用新模型。已在服务的 Generation 不因激活探测超过 30 分钟而自行失效；未来凭据解析使用轮换后的材料，在途实例保留自己的材料。健康检查复用成功配置探测及实际 breaker/FAISS 文件验证，不隐式发送大文本探测，也不借其他租户的 Provider 声称可用。

常规 vector.rebuild 的发布事务同时更新 Vector 指针、Generation 原始配置绑定、active 配置的历史代标识和 Provider serving epoch；热限制切换同样使旧准备结果失效。并发准备发现共享 ID MAP 的空间标记会被另一个构建覆盖，Vector 发布现用已验证 survivor/surrogate 恢复本代标记并原子盖成员戳。实际交错构建测试证明新空间发布后 ID MAP 与 Generation 一致、旧构建被拒。抽取已有本地 cleanup 逻辑为 VectorProjectionMaintenance，清理无需解析秘密，继续遵守既有年龄窗口、两代保留、事务后删文件和全租户 orphan 保护。

managed-001/002 各 3 passed/2 failed，均为真实 HTTP 测试未完整建立 Tenant ExternalIdentity/verified binding，已通过实际 propose/confirm 补齐，没有绕过主体认证。managed-003 为 3 passed/2 failed：修复初次启用 capability 的 VectorDegradedError 边界；测试请求改为独立 request_id，避免既有 Recall 回执缓存掩盖真实请求。managed-004 的 7 项实际 HTTP/FAISS 通过。扩大 managed-005 为 132 passed/1 failed（包含既有 HTTP Recall、向量并发/ID MAP/Recall、激活/回滚/恢复），唯一失败是清理夹具没有超出既有两代保留数量。补足真实后继 Generation 后 managed-006 的 9 项通过，包括秘密轮换和秘密不可用时真实 Worker 清理。types-013/014 仅类型/测试构造问题，types-015 全仓通过；structural-005 的 TestClient 类型收窄修正后 structural-006 的全仓结构/类型/契约门禁通过。

目前通过显式 RecallAssemblyConfig.embedding_runtime 接入真实 ASGI/Worker 验证；ServiceConfig/CLI 还未选择受控部署配置，实际常驻 phase14_handlers 仍待注入配置运行时及 Generation 协调器。因此不能称生产部署闭环完成。managed-007（session 20283）正在补齐 HTTP readiness 及已服务配置跨 30 分钟探测窗口的行为；终态待收取。随后继续部署引用/出站策略、普通长重建的批间租约/配置复验、真实双租户并发、Provider HTTP/Console 浏览器全流程及真实外部 Provider 验收。最终 make ci 尚未运行，W05 不关闭。

managed-007 已完成：10 passed，exit 0，结束于 2026-09-07T22:36:21Z。实际 HTTP readiness 在未建代/故障时报告 vector_capability unavailable，健康时报告 ok；已服务配置超过探测新鲜窗口仍真实查询且不发送隐式探测。上述后台会话终态已收取，types-016 仅验证最后新增测试后的全仓类型，随后继续受控部署装配。

types-016 全仓 Python/TS SDK 类型检查通过，终态及原始证据已归档；当前无在途验收。继续 W05 受控部署配置与常驻 Worker 装配，W06 未启动。


## 受控部署与常驻 Worker 检查点

新增独立 provider_config_file，支持 CLI、IRIS_MEMORY_PROVIDER_CONFIG_FILE 与 TOML，严格读取属主/0600/非链接普通文件（64 KiB 上限），仅接受 schema_version=1、租户 secret_references 和 outbound。重复键、未知字段、类型/数量/大小/时间越界及 NaN 拒绝；错误不回显原文。引用沿用精确租户允许表，禁止跨租户共用，Provider/Console 主密钥和部署文件本身不可解析为凭据。回环出站须部署 allow_loopback 和服务 development_embedding 双重声明；有部署文件时不会自动构造 deterministic。见[部署说明](../development/embedding-deployment.md)。

API、挂载/独立 Console 和实际 worker() 共用装配入口，RecallProjections 提供配置运行时及 ProviderGenerationRuntime；常驻 phase14_handlers 注入两者。CLI 增加 Provider 文件、主密钥、向量根目录、Required 和显式开发开关。deployment-001 的 22 项通过，含真实 CLI Worker 子进程执行固定探测、真实 HTTP 和 FAISS 激活，早已构造的 ASGI 应用随后自动使用新服务代查询，不依赖重启 API 才识别配置。

普通 vector.rebuild 复用 ProviderGenerationRuntime 的批量边界：每批前后重新验证真实租约 owner/generation/source/expiry、当前服务 epoch 与实际凭据指纹。Worker 为长重建续租；失租后不发送后续批次且不能发布，替代 Worker 可继续完成。短暂秘密缺失使用既有有限 Outbox 重试预算；读请求明确降级。deployment-002 的 118 项通过，包括真实 CLI/HTTP、批间第二 Worker 接管、秘密改变、旧 Recall/激活/回滚/恢复回归。

types-017 全仓通过；types-018 的 BaseRoute→Mount 测试类型收窄修正后 structural-007 全仓通过；加入批间检查后 structural-008 同样通过。managed-008 的 12 passed/1 failed 为新恢复测试错误使用 attempts 字段，已改为实际 attempt_count；有限重试及不发布断言已通过。structural-009 仅要求格式化最终 helper，已完成。deployment-003 与 structural-010 正在后台复验最终秘密恢复、独立 Console 监听与结构门禁，终态待收取。

W05 仍 In progress。下一步是 Provider HTTP 契约/路由、服务器 side_effects/重建计划读面、完整 Console 浏览器流程及实际双租户 HTTP 并发；真实允许外部 Provider 的端点/模型/维度/秘密引用/成本授权仍待提供。最终 make ci 和独立提交尚未进行，W06 不启动。

deployment-003 已完成：58 passed，exit 0，结束于 2026-09-07T22:54:12Z；实际秘密缺失→有限重试→恢复后构建及挂载/独立 Console 回归通过。structural-010 全仓格式、lint、导入/文档、Python/TS SDK 类型、契约生成和兼容检查通过。全部后台终态已收取，原始日志、候选清单及失败记录已归档；当前无在途验收，继续 Provider HTTP/Console 流程。

## HTTP and production Console checkpoint

Console 1.2 now defines 14 real Provider HTTP operations: directory, masked configuration, immutable revision cursors, asynchronous fixed probe, activation, rollback, rebuild list/detail and cancellation. Plans come from one server snapshot and include resource estimates, rebuild decision, Worker requirement and request-duration estimate explicitly excluding queue/index I/O. Rebuild history uses the immutable saved plan and actual Operation; result and serving generations remain separate. Read authorization follows design section 9: either system.read or providers.manage, with console.manage purpose. Writes retain providers.manage and recent reauthentication for sensitive commands. Other tenants cannot observe these records; another operator in the same tenant cannot cancel the owner's work.

provider-http-contract-001 passed 472 source-owned fixtures and route inventory tests. provider-http-001 passed six actual lifecycle assertions but failed because helper tested was accidentally collected and the affected-only invocation inherited the global coverage threshold. The helper was renamed and existing make test-affected used. provider-http-002 passed 84/failed six: an output assertion matched the allowed provider_kind value rather than a forbidden vector property. Correcting the exact JSON property check yielded provider-http-003: 90 passed, including real HTTP/FAISS lifecycle, retained rollback, pagination binding, cancellation continuity, both read permissions, tenant/owner isolation, changed credentials, and actual malformed/302/429/503/dimension/normalization failures without returning secrets or vectors. structural-011/012 found new dictionary type annotations; structural-013 passed all format/import/docs/types/contracts checks after correction.

The production Provider page consumes generated schemas and server adapter rules. It clears submitted credentials, displays only masked state, renders server plans in activation/rollback dialogs, uses the target historical revision for rollback, and refreshes configuration/generation only from actual asynchronous operation results. It supports revision/rebuild pages, cancellation and read-only access. useQuery refresh callbacks are stable; Operation cancellation now sends the actual result to its parent. console-001 found potentially missing required numeric descriptors; explicit guards corrected this. console-002/003 passed generated types, lint, TypeScript, 29 frontend tests and production asset checks.

A disposable browser tenant uses a real loopback HTTP Provider, private deployment reference file, independent finite Worker process and actual HTTP Recall. The old mock Provider browser segment was replaced by a real flow; the existing Settings test remains. provider-browser-001 failed at startup because of test AccessContext aliases; provider-browser-002 failed because the macOS temporary path crossed /var's symlink. The fixture now uses actual field names and a resolved private path, without weakening production checks. provider-browser-003 passed both tests: configure/probe/activate/observe/query/rollback and system.read viewing. It proves initial degradation, old-model queries while a new configuration is queued, new-model queries after publication, and exact retained-generation restoration. browser-regression-001 passed all 23 browser cases, including existing management flows. Console-wide integration/contracts console-regression-001 passed 1,341 tests.

The simultaneous two-tenant test uses one ASGI Recall runtime, separate real credentials and verified actors, two actual HTTP-managed configurations, different model/dimension/Provider secrets, and 20 concurrent requests per tenant. tenant-http-001 passed all 40 queries with exact outgoing model/credential pairs and no cross-tenant candidates. structural-014 passed the whole structural/type/contract gate. These are isolated service proofs; no real allowed external Provider acceptance is claimed. Its previously requested endpoint/model/dimension/credential reference/cost authorization is still pending. W05 remains In progress; full make ci and final package report/queue/commit are still required before W06.
