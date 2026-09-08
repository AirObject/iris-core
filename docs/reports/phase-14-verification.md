# Phase 14 实施与验证记录

日期：2026-09-06。状态：**In progress**；这是实施切片记录，不是稳定发布验收报告。

基于 `692de12b4b9a9d8d47ebfdd938ba9622d152f0d9` 的未提交工作区实施，保留开始时已有的范围规划修改。初始切片当时为 Core 0.13.0 / Schema 15；业务 Contract 1.10.0、Console Contract 1.1.0；独立 Python SDK 0.11.1、TS SDK 0.11.2。环境为 macOS ARM64、Python 3.12.13、SQLite 3.50.4、Node 26.8.1、Chrome。没有创建 RC、签名、上传或发布产物。

当前生成契约计数（由 `make lint` 核对）：业务 <!-- contract-count:openapi:paths -->86<!-- /contract-count --> 个路径、<!-- contract-count:openapi:operations -->92<!-- /contract-count --> 个操作；Console <!-- contract-count:console:paths -->144<!-- /contract-count --> 个路径、<!-- contract-count:console:operations -->174<!-- /contract-count --> 个操作。下文逐切片的接口与测试数字保留当时的历史口径。

## 已落地切片

- **14.0-C 读面闭环**：按 [ADR-0023](../adr/0023-release-resources-and-console-identifiers.md) 保留 Canonical 不透明 ID，补非 UUID、空 ID、超长 ID Fixture；重新生成类型并适配 `list_columns`、结构化 `sorts`、空写 Schema 和 history/references 能力。真实浏览器发现并修复相同筛选清空列表的问题，补表头和排序选择器的可访问语义。此处只证明读面，不包含未发布写动作。
- **14.0-E Required Lease**：按 [ADR-0024](../adr/0024-online-recall-focus-lease.md) 接通 Recall 与 Focus 创建/激活/状态转换。授权先于 Proof 检查，重放重新验权，写事务/响应发布再次 fencing；Proof 不进入业务指纹。Python/TS 客户端携带可选 Proof。
- **14.0-F 接口变更门禁**：增加 [显式方法映射](../development/public-api.md) 和机器快照，核对 79 个 Python/79 个 TS 方法、91 个业务及 111 个 Console operation、导出/签名/DTO/异常属性、CLI 子命令/参数、Schema 摘要。源码和隔离安装物均受检查；候选输出不能直接覆盖已审阅快照。此项不替代逐方法权限矩阵、真实进程消费和 OS 隔离验收。
- **版本来源一致性**：修复 Console bootstrap/meta/前端合成响应遗留的 1.0.0 常量，并从发布契约读取版本；修复 `/v1/capabilities` 仍宣告 Schema 11 的问题，安装 Smoke 强制校验协商为 Schema 15，源版本与包版本/Console runtime_versions 交叉检查。
- **14.1 安装资源**：Core wheel 显式包含 15 个 Migration、两套 OpenAPI 和 capability 真源，sdist 白名单排除独立客户端、前端、根 hosts 宿主目录及缓存。资源从包内读取；缺失/空 Migration 失败关闭。验证脚本检查资源字节、归档成员、依赖、extras 和唯一 CLI entry point。
- **14.1 公共客户端消费**：新增受控离线 `init`，事务内创建租户/Agent/空间/受限业务凭据；可由可信操作者显式登记新身份、确认 Binding 并设置 Surface 模式。凭据文件独占创建、0600、拒绝覆盖和符号链接，标准输出不含 Token。独立 SDK 补 LICENSE/py.typed，不并入 Core。
- **14.1 CI**：`make ci` 纳入 Console 生成检查/lint/typecheck/测试/生产构建/浏览器用例和全新构建安装 Smoke；CI 安装 Chromium。文档门禁扩展至根目录、hosts、SDK、Console 手写 Markdown，跳过依赖与构建缓存。

## 执行证据

下表完整 CI 和安装记录产生于 Note 管理写入切片之前，只证明当时版本；当前 Note 切片在下一节单独记录。

| 命令/范围 | 实测结果 |
| --- | --- |
| `.venv/bin/python -m pytest -q`，Lease 接线前的完整回归 | **10,614 passed**，592.92s，覆盖率 **83.82%**；后续 Lease/SDK 变更另行复验，不能据此宣称最终组合 CI 已通过 |
| 修复版本漂移后 `make ci` 的完整 Python 回归 | **10,667 passed**，556.93s，覆盖率 **84.12%**；当次后续浏览器阶段遇到 Node JSON 导入属性错误，修复后按下一行复验受影响门禁 |
| `make public-api-check console-browser package-check` | **通过**；前端类型/lint/typecheck、**29 tests**、生产构建、**6 browser tests（8.3s）**、源码及安装物公开接口、全新 sdist/wheel 构建及隔离安装 Smoke。公开接口新增 **6 个负例**另由下述 114 项组合验证 |
| Console 读面与契约定向验证 | **161 passed**；解决原 Reflection/Candidate UUID 冲突 |
| `npm run check --prefix web/console` | 类型生成、lint、typecheck、**29 tests** 与生产构建通过；之后表头/筛选修复再构建并由浏览器复验 |
| `CONSOLE_BROWSER_EXECUTABLE=… npm run test:browser --prefix web/console` | 当时 **6 passed**，8.3s；真实记忆注册表/Note 列表/详情/历史/重载，真实认证、密钥签发吊销；其余业务场景仍是显式 Fixture |
| Phase 14 Lease/打包与 Console 契约组合，缓存竞争补强前 | **153 passed**，4.44s；随后增加缓存重放窗口抢占用例，Lease 合计 **48 项** |
| Console bootstrap/meta 版本修复后，Console Plane/Contract + Lease | **178 passed**，5.61s |
| 公开接口负例与 Console 契约/版本组合 | **114 passed**，0.35s；包括未知 SDK 方法、丢失 operation、TS 逃逸方法、嵌套 CLI 漂移及 DTO 数据库连接拒绝 |
| Phase 14 Lease、Phase 3 Domain/Jobs、Phase 6 Recall/Usage | **113 passed**，74.64s |
| `npm test --prefix sdk/typescript` | **18 passed** |
| `.venv/bin/mypy`、Ruff、导入边界、契约生成/兼容、文档检查 | 通过；最新 mypy **250 个文件** |
| `UV_CACHE_DIR=.uv-cache .venv/bin/python -m tools.check_packages` | 全新目录构建 Core sdist → wheel；独立 SDK 构建；新 venv 完整安装依赖；`python -I` 在源码目录外运行真实 Core 与独立 SDK；**通过** |

最后一项在 Required 模式验证 Focus 缺少 Proof 被拒、持有租约后 Focus/Note/Observation 写入、幂等重放、Cursor、Persona、Recall、Schema 14 协商，及四个私有/管理路径拒绝。`worker --once` 通过；SIGTERM 后记录 lifespan shutdown complete，排空约 **0.254s**。当前 Uvicorn 排空后重新抛出 SIGTERM，退出码 -15；验收同时检查完成日志，不能仅凭信号退出判断正常排空。

初次沙箱禁止回环监听的 pytest/browser 尝试失败，已使用获准回环监听的进程重跑。初次离线安装缺少缓存依赖，随后联网下载运行依赖，完整新 venv 安装通过。上表安装 Smoke 显式使用 `--allow-local-sqlite` 并在机器输出标记 `development_sqlite_override=true`；这不构成生产 Allowlist 或 OS 身份隔离证据。

## Console Note 管理写入（后续切片）

按 [ADR-0025](../adr/0025-console-command-authorization.md) 新增 `CommandActor` 与共享命令执行器；请求只接受领域字段，服务端决定 Tenant、来源和权限。预检查、写事务与幂等结果发布均重验会话、密钥、Grant、Scope、Privacy 与引用。有限 Scope 的读者不能编辑影响范围更广的上级资源。领域服务保留原 Revision/CAS、状态机、Watermark、Audit 与 Outbox；管理分支只免除在线宿主 Lease。

Console 1.1.0 已接通 Note 创建、编辑和状态转换，含 Task/Claim/Episode 的真实提升。三个路由均带严格请求 Schema、CSRF、幂等及错误封装。创建/编辑重放返回原修订和同事务保存的更新时间；当前授权失效后不返回缓存。详情动作依据当前权限与状态，其他资源写入和 Forget 保持不可用。

真实浏览器发现并修复两个前端契约问题：有 Revision 的资源优先提交 expected_revision；可清空正文，状态转换省略无关的 null 字段。Lookup 使用正式 ResourcePage，并在会话/Grant Epoch 变化时清除旧集合。

| 当前切片验证 | 结果 |
| --- | --- |
| Console Note HTTP / 命令授权 / 两组契约检查 | **177 passed**，9.15s；覆盖 CAS/重放、父 Scope 与隐藏资源拒绝、Restricted 显式授权、三个提升目标、事务与缓存授权竞争 |
| 原 Note / Phase 4 Surface / Jobs / Phase 10 operation / Console 读面 | **116 passed**，22.56s；原在线 Lease 门禁保持有效 |
| `make console-browser`（本机 Chrome） | 类型/lint/typecheck、**29 tests**、生产构建及 **7 browser tests（9.3s）** 通过；新增真实 Required 模式的 Note 创建→编辑→归档→重载。其他业务 Fixture 仍不代表真实后端 |
| mypy / Ruff / 导入边界 / 文档 / 生成与兼容 / 公共接口快照 | 全部通过；mypy **254 个文件**。快照只新增 Console 写 operation/Schema，Python/TS/CLI 与业务 HTTP 未变 |
| 当前源码新建 sdist → wheel / 独立 SDK / 新 venv 安装 | **通过**；安装物公共接口匹配，Required 模式 SDK Smoke 和 Worker 通过，SIGTERM 完成排空约 **0.144s** |

当前安装仍显式使用开发 SQLite override；这条证据不包括生产 Allowlist、容器身份隔离或当前 Note 新增项的完整全仓 CI。无数据库迁移，Core/Schema 和业务契约版本保持不变。

## Console Focus 管理写入（后续切片）

Console 1.1.0 增加 Focus 创建、编辑、激活、dormant/dismissed/expired 转换。严格契约与真实页面动作同时接线；摘要编辑保留 activation_base 和最后激活时间，只计算自然衰减。Focus 当前没有待消费的变更 Outbox 类型，继续使用原有 Canonical Revision/Pointer/Watermark/Audit 路径，不伪造新 Job。

管理容量变更逐个检查受影响条目的权限；父 Scope、其他空间和 Restricted 目标不因创建、编辑或重新激活而被越权淘汰。失败整笔回滚，成功保留运营者 provenance，重放不重复淘汰。Core 的 Focus promotion 尚不物化目标，管理契约明确拒绝 promoted；完整提升属于剩余 Core 缺口。

| 验证 | 结果 |
| --- | --- |
| Focus/Note 管理命令、真实 HTTP 与 Console 契约组合 | **194 passed**，10.63s；随后增加两个容量副作用负例，由下一行覆盖 |
| Focus 命令、原 Focus/Phase 3 安全与 Jobs、Phase 14 Lease | **105 passed**，20.41s |
| `make console-browser` | 生成、lint/typecheck、**29 tests**、生产构建与 **8 browser tests（10.9s）** 通过；新增真实 Required 模式 Focus 创建→编辑→休眠→激活→刷新 |
| 公开接口候选审阅 | 仅新增四个 Console operation、六个请求/字段 Schema 和动作字段枚举；Python/TS/CLI 与业务 HTTP 快照保持一致 |
| 当前 Focus 切片新建 sdist/wheel 与新 venv 安装 | **通过**；安装物公共接口匹配、Required SDK Smoke 与 Worker 通过；SIGTERM 完成排空约 **0.234s**，仍显式使用开发 SQLite override |

本切片尚未作为不可变 RC 运行完整全仓 CI。生产 Docker 运行时当前机器未发现；后续生产镜像、OS 身份隔离与长时间故障测试没有据此标记通过。

## Console State 管理写入（后续切片）

按 ADR-0025 接通 State 创建、更正和显式过期。管理来源固定为 user，当前 Namespace Policy 不允许该来源时拒绝，策略变更使缓存回执失效。自然键创建要求 expected_revision=0；更正和过期复用 CAS、TTL 上限、历史裁剪及 coalesced Outbox。过期修订使用零有效期，在线读取立即排除；不视为 Forget。

State 返回 ID/Revision 回执，不缓存旧正文；当前修订已替换、旧历史已裁剪时仍能重放回执。通用读序列化与各类型表单描述分离到专属模块，继续由正式契约生成类型。

| 验证 | 结果 |
| --- | --- |
| State HTTP 与 Console 契约，增加父 Scope/TTL 负例前 | **163 passed**，2.90s |
| 当前 State HTTP | **13 passed**，3.76s；包含权限、自然键冲突、TTL 上限、来源伪造拒绝、历史裁剪后回执重放和过期可见性 |
| 原 State / Phase 3 安全与 Jobs / 管理授权 / Console 读面 | **123 passed**，20.48s |
| 公开接口候选审阅 | 仅新增三个 Console operation、七个 State 请求/回执 Schema 和动作元数据；业务 HTTP、SDK 与 CLI 未变 |

初次新增 State 浏览器用例因共用密钥触发每分钟五次登录限制。夹具改用独立受限 State 密钥，未放宽生产限流；独立复验为 **9 browser tests passed（11.7s）**。

之后对包含 State/Note/Focus 的同一代码组合执行完整 `make ci`，**退出码 0**：后端 **10,786 passed（608.21s）**、覆盖率 **84.20%**；TS SDK **18 tests**；Console **29 tests**、生产构建与 **9 browser tests（11.9s）**；mypy **260 个文件**、Ruff、导入边界、文档、契约、公开接口均通过；新 sdist/wheel 与新 venv 安装通过，Required SDK/Worker Smoke 通过，排空约 **0.235s**。这是 Task 管理命令接线之前的完整开发组合证据；SQLite override 和剩余生产门禁仍适用，不构成最终 RC 验收。

## Console Task 主资源管理写入（后续切片）

接通 Task 创建、编辑和状态流转三个 Console operation 与六个严格请求/字段/证据 Schema，当前 Console 为 103 个操作、93 个路径。管理创建记录 console provenance，所有者和来源授权在事务中复核；完成必须有有效 Observation/Artifact 证据，沿用全部步骤终结校验。完成证明写入同条 Revision 与资源关联；幂等结果发布重新验证当前证据可见性。没有数据库迁移或业务 SDK/HTTP 行为变更。

已完成首轮组合回归 **195 passed（6.21s）**，覆盖原 Task 领域、Console 契约、管理 CAS/状态机、伪造 origin/无效输入、有限 Grant 父 Scope 拒绝、跨空间/partial 证据拒绝和已删除证据的缓存重放拒绝。随后补充 Artifact、所有者和 lookup 契约校验，最终 Task/原 Task 领域/管理授权/State/Note/Focus HTTP/Console 契约组合为 **257 passed（19.78s）**。

Console 类型生成校验、lint/typecheck、**29 tests** 与生产构建通过；真实后端浏览器 **10 passed（13.1s）**，新增 Required 模式 Task 创建、编辑、等待、取消及刷新。公开接口候选审阅确认仅新增上述 Task operation/Schema，既有 Console 操作、业务 HTTP、Python/TS SDK 和 CLI 快照均未变化；额外增加 CommandFieldSpec 的 entities lookup 枚举，以提供授权的所有者实体选择。

最新表单复验仍为 **10 browser tests passed（13.1s）**；mypy **262 个文件**、Ruff、文档、导入边界、契约兼容和公开接口检查通过。新建 sdist/wheel 及隔离 venv 安装 **通过**，安装物快照与 Required SDK/Worker Smoke 通过，SIGTERM 排空约 **0.259s**，仍显式使用开发 SQLite override。

这仍是 Task 主资源切片；Task 子资源写入、证据关联失效、Forget/恢复、其余 Console 模块与生产发布门禁继续实施。上一节的完整 CI 不包含本切片，不将其作为新组合的完整 CI 证据。

## Task 步骤与跨平面聚合修订（后续切片）

新增 Console 步骤创建与状态流转两个操作、五个请求/回执 Schema 和可选 Meta descriptor；当前为 **105 个操作、94 个路径**。写事务同时验证父 Task 修订及目标 Step 修订，检查证据和后继步骤的实际就绪副作用权限，任一失败全部回滚。真实表单发送两项修订，成功后刷新父/子视图。

进一步修复在线子资源写入没有推进父 Task 修订的缺口：步骤、依赖、触发器创建以及内部触发器启用/禁用均复用同一父修订写入。没有新字段或方法签名，但后续更新父 Task 必须重新读取修订；旧修订应冲突。状态触发器按最近一次真实 status 变化建立 occurrence，避免字段/子对象修订导致重复触发或丢失 from_status 匹配。

| 验证 | 结果 |
| --- | --- |
| 步骤/原 Task/Trigger/Task HTTP/管理授权/Console 契约组合 | **275 passed（15.10s）**，包含父子 CAS、跨父拒绝、终止父拒绝、两次并发仅一次提交、跨平面陈旧修订、幂等不重复推进、证据与隐藏后继回滚 |
| Console check | 类型、lint/typecheck、**29 tests**、生产 build 通过 |
| 真实浏览器（在线父修订统一前） | **11 passed（14.1s）**；步骤创建→开始→完成，检查父/子修订并重载；其后共享领域变化由组合测试及后续全仓 CI 覆盖 |
| 公开接口候选审核 | 仅新增上述 Console 操作/Schema 与可选 Meta descriptor；SDK/CLI/业务 HTTP 的结构快照保持一致。聚合 CAS 行为变化另在 ADR-0025 和 SDK 使用说明明示 |

依赖移除与触发器管理仍未开放；本切片不代替 Forget/恢复全链或生产发布验收。首轮完整开发组合 `make ci` 的 Python 段为 **10,829 passed / 4 failed（591.50s）**，覆盖率 **84.21%**；四项均是 Phase 4 Lease 夹具在创建两个 Step 和一个 Trigger 后仍使用父修订 1。夹具现从准备完成的数据库读取实际父修订，未改 Lease 断言，定向复验 **34 passed（4.06s）**。第二轮完整 CI **退出码 0**：**10,833 passed（597.87s）**、覆盖率 **84.19%**；TS SDK **18 tests**；Console **29 tests**、生产 build 与 **11 browser tests（14.4s）**；mypy **263 个文件**、Ruff **265 个文件**、文档/导入边界/契约/公开接口检查通过；新 sdist/wheel 和隔离安装通过，Required SDK/Worker Smoke 通过，SIGTERM 排空约 **0.240s**。本结果仍使用开发 SQLite override，不构成生产 RC 验收。

## Task 依赖生命周期与 Schema 15（后续切片）

按 [ADR-0026](../adr/0026-task-dependency-lifecycle.md) 接通创建、解除和恢复依赖；Console 1.1.0 开发候选现有 **107 个操作、95 个路径**。两端步骤都需当前授权，父/子 CAS 和终结父对象约束在同一事务执行。解除追加 removed 修订，恢复相同步骤对复用 ID 并重新检查环；图和就绪推导只使用 active 边。原在线创建的成功重放保留原 condition/revision，不随后续恢复变化。补充真实 Tombstone 负例后，修复在线依赖成功重放与重新激活未检查依赖自身删除标记的问题；管理路径原本已回滚，两个路径现在都在修改/发布前拒绝。

新增 0015，只增加两列状态及索引，0001–0014 字节保持不变。Core **0.13.0** 仅打开 Schema **15**。已有库必须停机并使用 CLI 创建、验证备份后升级；明确空库可在首次 Runner 调用中应用带 bootstrap_safe 标记的迁移。中断或已有数据不获得该例外。安装 Smoke 已改为校验 Schema 15，uv.lock 只更新 Core 项目版本。

| 验证 | 当前结果 |
| --- | --- |
| 依赖生命周期与迁移闭环 | **19 passed（5.21s）**；父/子陈旧修订、错误父对象、终结父对象、两端 Restricted 授权、恢复环、在线原修订重放、真实 CLI 备份升级、解除后备份恢复及损坏状态拒绝 |
| 依赖/迁移/原 Task/Trigger/备份/Console 契约组合 | 初次 **414 passed / 1 failed（21.85s）**；失败的损坏快照夹具只改了 WAL，恢复验证读取不可变数据库文件。夹具显式 checkpoint 后定向 **1 passed（0.62s）**，并包含在上述 19 项成功回归中；未放宽恢复校验 |
| Console check 与浏览器 | 类型生成/lint/typecheck、**29 tests**、生产 build 通过；**12 browser tests（16.1s）**，新增依赖创建→解除→复用 ID 恢复→刷新 |
| 公开接口审核 | 只新增两个 Console operation、三个 Schema、动作/Lookup/回执枚举和 Core 版本；Python/TS SDK、CLI 及业务 HTTP 结构保持一致 |
| 静态与生成检查 | Ruff、mypy **265 个文件**、文档、导入边界、生成/兼容和公开接口通过；主工作区更新的初始化身份回执及其 **5 项测试**保留 |
| 合入后的首轮完整 `make ci` | **10,853 passed / 8 failed（608.36s）**，覆盖率 **84.24%**。6 项是旧迁移/恢复夹具未声明离线升级、旧窗口预期或旧快照遗漏新增列；另有 Forget p95 **151.80ms** 与 State p95 **56.13ms** 超预算。夹具修正后与新增删除拒绝组合 **76 passed（16.23s）**；两项性能保持覆盖率插桩、原预算单独复测 **2 passed（21.20s）**。只为定向诊断关闭全仓覆盖率比例门槛，未修改项目 CI 配置；第二轮完整 CI 结果见下一行 |
| 修正后的完整 `make ci` | **退出码 0**；**10,864 passed（606.44s）**，覆盖率 **84.25%**，两项性能在原预算下通过。TS SDK **18 tests**；Console **29 tests**、生产 build 和 **12 browser tests（16.9s）**。Ruff、mypy **265 个文件**、文档/导入/契约/公开接口通过；新 sdist/wheel、隔离安装与 Required SDK/Worker Smoke 通过，Schema **15**，SIGTERM 排空约 **0.238s**。仍显式使用开发 SQLite override，不作为生产 RC 验收 |

恢复校验新增依赖当前状态、condition、Tenant/Task 和两端步骤与所指修订的一致性检查。Schema 15 不支持原地降级；回退需要隔离恢复升级前 Schema 14 备份及旧安装物，并处理升级后新增数据。触发器管理、其余 Core/Console 功能与生产发布门禁继续实施。

## 可复现入口

- `make package-check`：每次创建全新构建和安装目录，避免旧版本 glob 混入，结束清理临时凭据与数据。
- `tools/check_distribution.py`：可用 `--report` 输出实际归档文件、依赖与 SHA256 清单。
- `tools/check_installation.py CORE_WHEEL SDK_WHEEL --report PATH`：安装后只经 CLI 与 SDK 验证；开发机可显式加 `--allow-local-sqlite`。
- `make public-api-check`：核对源码公开接口；安装检查另用 `python -I …/tools/check_public_api.py --installed` 检查实际 wheel，拒绝 editable 源码回退。
- [安装与初始化操作说明](../operations/core-installation.md)；[阶段门禁](../development/phase-14-hardening-release.md#退出门禁)。

## 未完成门禁

| 需求 | 剩余工作 |
| --- | --- |
| P14-BASELINE-01 / P14-PUBLIC-API-01 | Phase 13 其余资源写入/Forget、统计、手动导入导出、Provider、Settings、Operation 全链；逐方法权限和真实进程消费矩阵及稳定冻结；同一最终 RC 的完整 CI，各实施切片单独记录验证，不能替代最终候选验收 |
| P14-DEPLOY-01 / P14-ISOLATION-01 | 生产 Python/SQLite 镜像、Compose、真实 Provider 或明确关闭、指标、SSE 多进程与客户端 OS 身份/文件隔离 |
| P14-SECURITY-01 | 供应链扫描/SBOM、静态加密、凭据/密钥轮换与恢复拒绝矩阵 |
| P14-PERF-01 | 固定生产硬件与预算后测量容量、24h Soak、每个进程边界 20 次 SIGKILL |
| P14-RECOVERY-01 | 3 轮生产规模升级/恢复/回退，最新删除账本与撤销权限不复活，RPO/RTO |
| P14-RELEASE-01 | §38 双向追踪、不可变 RC/Release Manifest、发布评审及注册表安装回读 |

以上未完成项继续按原计划实施，Phase 14 不标记 Completed。Phase 11/12 保持 Deferred，不恢复宿主适配或分发。


## Console Task 触发器管理（后续切片）

按 [ADR-0025](../adr/0025-console-command-authorization.md#task-触发器的配置与调度进度) 接通创建、配置编辑、启停和授权详情，共新增 4 个 Console operation、4 个请求/字段 Schema；当前独立 Console 1.1.0 开发候选为 **111 个操作、97 个路径**。另扩展字段描述的布尔控件/default、enabled 动作及 task_trigger 回执类型。审阅机器快照确认业务 HTTP、Python/TS 公共接口和 CLI 均未改变，Core 0.13.0 / Schema 15 不变。

父子修订冲突、跨父路径、终结父计划、未知来源字段与 Worker 进度写入失败时事务回滚。关联及被观察步骤的 Restricted Grant 覆盖创建/编辑/启停和详情读取；Worker 不消费被删除触发器或关联步骤。编辑重新安排调度，幂等成功重放不回退已执行的游标，删除后不能重放成功缓存。

真实浏览器首次 **12 passed / 1 failed（18.0s）**：列表摘要正常省略 schedule_spec，却被表单当作完整详情提交，编辑返回 400。增加授权详情读取后 **13 passed（18.4s）**，覆盖创建、默认启用值、修改配置、禁用/启用及刷新持久化。保留列表摘要策略，不扩大列表正文披露。Console check 的 **29 tests**、类型/lint/typecheck/生产构建通过；触发器与正式契约组合 **214 passed（6.44s）**。后续补充周期重排、缓存进度保持与跨父/删除详情负例后，触发器专项 **22 passed（6.60s）**。首轮完整 CI 的 Python 段 **10,897 passed / 1 failed（629.10s）**，覆盖率 **84.32%**；图/Profile 混合 Recall 在 perf-22 请求的全部路由超过 250ms deadline。保留覆盖率插桩和原 250ms 预算单独诊断 **1 passed（5.25s）**，40 个测量样本 p50 **27.2ms**、p95 **28.5ms**、max **37.2ms**。仅因定向测量不覆盖全仓而关闭该次命令的总覆盖率比例门槛，未改项目 CI 配置或延迟门槛。该首轮结果不算全量通过；修正后的最终复验见下文。


第二轮全量复验在只读审查发现详情归属边界后主动中止，不计为通过：触发器可同时引用父 Task 与被观察 Task，不能用任意 Task 来源引用证明父路径归属。现同事务核对触发器实际 task_id，新增“被观察 Task 不是父对象”负例，触发器/读面组合 **77 passed（21.31s）**。

同步补齐恢复校验：触发器 current 与所指 Revision 的 kind、关联步骤、时区、catch-up、misfire、每轮上限、enabled、Tenant/Task 必须一致，并核对真实父计划的 Agent/Tenant 与关联步骤归属。next_fire_at/last_scan 属于正常 Worker 进度，不与配置修订比较。新增真实 Console 编辑 → Worker 执行 → 备份/恢复 → 9 种配置损坏拒绝，配合既有备份组合 **25 passed（3.47s）**；mypy **267 个文件**、Ruff **269 个文件**通过。修正后的完整 `make ci` **退出码 0**：**10,900 passed（663.40s）**、覆盖率 **84.33%**，图/Profile 与其余性能在原门槛下通过；TS SDK **18 tests**；Console **29 tests**、生产 build 和 **13 browser tests（18.6s）**；mypy **267 个文件**、Ruff **269 个文件**、文档/导入边界/契约/公开接口门禁通过。新 sdist → wheel、独立 SDK 与新 venv 隔离安装通过，安装物公共接口匹配；Required 模式 SDK Smoke、Worker once 和 4 个私有/管理路径拒绝通过，Schema **15**，SIGTERM 完成排空约 **0.251s**。

完整复验仍显式使用 `development_sqlite_override=true`，不作为生产 SQLite Allowlist、OS 身份隔离或 RC 发布验收。触发器管理切片已闭环；Focus 真实提升、其余 Core/Console 模块及 Phase 14 生产硬化与发布门禁继续实施。


## Focus 真实提升（后续切片）

按 [ADR-0027](../adr/0027-focus-promotion-materialization.md) 补齐 Note/Task/Episode/Claim 四类目标；目标由所属服务写入，Focus 状态与目标事务一致。共享不可变来源快照，复用 Note 原有目标写入及 Note 创建的修订/审计/Outbox 逻辑。Claim 不以 Focus 本身为 Evidence，使用 agent_inference；Episode 必须有有效原始 Observation。新目标保留完整摘要、来源与来源隐私约束，Task 为 proposed，不自动激活。

原 Note/Claim/Episode 组合 **82 passed（9.97s）**，既有 Focus/Console Focus **39 passed（19.00s）**。首次新增测试暴露两个接线缺口：在线目标删除检查误放在 activate 返回路径，Console Focus 读记录未含目标字段；修正后 11 项通过。补充 Restricted 显式授权和来源隐私继承后 **19 passed（9.66s）**；再将原始来源加入管理幂等预检查，覆盖来源/目标删除后的双平面重放，专项 **27 passed（16.36s）**。较早的共享写入/Focus/Console/契约组合 **337 passed（91.92s）**。

Console check 的 **29 tests**、生成类型/lint/typecheck/生产 build 通过。真实浏览器 **13 passed（58.1s）**，在原 Focus 用例追加提升后读取实际 Task 与重载持久化；并行领域测试期间的耗时不作为性能基线。已审阅接口候选，只修改 ConsoleFocusTransitionRequest，业务 HTTP、Python/TS 和 CLI 不变，Console 仍 **111 个操作、97 个路径**，Core 0.13.0 / Schema 15 不变。当前完整 CI 尚待本切片复验。


后续 Required Surface/提升组合 **75 passed（30.99s）**，覆盖缺失/过期/旧 Epoch/非 Holder 和重放发布 fencing；目标现在为真实 Note。新增普通读取与历史读取的目标标识重授权，Console 同步隐藏来源/目标失效后的目标引用；提升专项仍 **27 passed（14.26s）**。活动 Focus 的常规读取不增加目标查询。最新 mypy **269 个文件**，Ruff **271 个文件**通过；修正后完整 CI 待记录。


首轮完整 CI 因临时存储不足中止（退出码 130）：出现 database or disk is full、无法创建测试目录与随后空数据库 Schema 0 的连锁错误；当时磁盘仅余约 2.5 GiB。确认作业结束后清理本任务 pytest-1258 与两个可重建 npm 缓存，保留日志和源码，释放临时空间；原先受影响的备份/Console 平面/读面在独立临时目录复测 **100 passed（25.78s）**。将 pytest tmp_path_retention_policy 设为 failed，仅改变成功用例临时产物的保留时机，失败产物仍保留；测试内容、覆盖率与性能预算不变。随后使用独立临时根目录运行完整门禁。

独立临时目录的完整 CI Python 段为 **10,929 passed / 2 failed（820.77s）**、覆盖率 **84.42%**，make 退出码 2；没有再出现磁盘满。失败为图路由在 perf-1 超时，以及结构化 Recall p95 **58.74ms** 超过原 50ms 预算。当时系统 Storage/StorageManagement 与 fseventsd 占用较高，但不能据此将失败直接归为环境问题。保持覆盖率插桩与原预算的两项定向复测为 **1 passed / 1 failed（12.33s）**：图/Profile p95 **63.4ms**，结构化 Recall p95 **56.59ms** 仍失败。

随后发现 Focus 列表在未提升项上重复执行完整内容授权与当前修订查询；改为与 get_in_tx 一致，仅在确有提升目标时追加目标重授权。既有列表的 Scope/Privacy/Tombstone 过滤保留，并补充来源或目标删除后的列表/历史目标隐藏断言。修正后同样的覆盖率插桩与原预算复测 **2 passed（11.73s）**，图/Profile p50 **50.1ms**、p95 **55.7ms**、max **59.3ms**；结构化 Recall 通过原 50ms 门槛。定向命令只关闭全仓总覆盖率比例检查，不修改测试门槛、覆盖率插桩或项目 CI 配置；完整 CI 仍需对修正后的代码复验。

修正列表路径后的 Focus/管理命令/Required Surface 组合 **113 passed（32.27s）**。重新启动的完整 CI 在来源闭包边界审查后主动中止（退出码 130），不计为全量通过：原计数只包含已完成节点，尚在递归路径中的来源未计入，67 个三节点分支（201 个对象、直接来源与深度均未超限）仍可通过。增加已访问与活动路径的合计后，200 个对象允许、201 个对象拒绝；上限和权限规则不变。该问题的回归测试使用固定规范读记录验证整个闭包预算，不修改在线请求上限。

2026-09-07 最终修正复验：来源数量边界与四类提升组合 **29 passed（7.19s）**；新增测试的返回值与引用字典类型标注补齐后，mypy **270 个文件**通过。随后对同一代码运行完整 `make ci`，**退出码 0**：**10,933 passed（595.23s）**、覆盖率 **84.41%**；图/Profile、结构化 Recall 和其余性能均通过原预算。TS SDK **18 tests**；Console **29 tests**、生产 build 及 **13 browser tests（19.5s）**；Ruff **272 个文件**、mypy **270 个文件**、文档/导入/契约/公开接口检查通过。

新 sdist → wheel、独立 SDK、隔离新 venv 安装与安装物公共接口检查通过；真实 Required 模式公共 SDK Smoke、Worker once 和 4 个私有/管理路径拒绝通过。Core **0.13.0**、Schema **15**、Python SDK **0.11.1**；SIGTERM 排空约 **0.239s**，服务收到信号后退出码 -15，关闭流程已完成。该次安装检查仍显式使用 `development_sqlite_override=true`，不代表生产 SQLite Allowlist、OS 身份隔离或不可变 RC 发布验收。

Focus 真实提升切片现已闭环。其余 Observation/Claim/Episode/Relation/Artifact/Identity/Persona/Event 管理命令、Forget、统计、导入导出、Provider/Settings/Operation，以及生产硬化、Soak、恢复和发布门禁仍按阶段计划实施，Phase 14 不标记 Completed。

## Observation 当前人工提交与注释（后续切片）

2026-09-07 按 [ADR-0028](../adr/0028-console-manual-observations.md) 接通当前人工提交与关联便签注释。服务端固定 role=user、kind=console.manual_submission、发生/确认时间与真实操作者审计标识；不接受历史时间、外部身份/效果、游标或任意来源字段。原始 Observation 保持不可变，annotate 由 NoteService 追加 important/inbox 便签、原观察固定来源引用和 annotated_by 关联，继承原观察隐私标签。创建及注释均复用管理命令授权、幂等和所属领域事务；在线 Observe 的 Required Lease 门禁保留。

独立 Console 1.1.0 开发候选现为 **113 个操作、98 个路径**。新增两个操作、四个字段/请求 Schema 与 annotate 描述枚举；已审阅公开接口快照，业务 HTTP、Python/TS SDK 与 CLI 不变，Core 0.13.0 / Schema 15 不变。

契约编辑脚本曾因非法 Unicode 负例的序列化错误中断写入；已从刚通过完整 CI 的生成物和原始候选恢复源文件，并验证 **284 个生成文档逐项完全一致**后再添加新接口。后续先完成安全编码再写入，保留所有非法 Unicode 夹具；当前正式契约测试通过，未删除负例。

首轮 HTTP **8 passed / 1 failed（2.56s）** 是测试将审计标识前缀误写为 operator，修正为真实 console/key 标识后原 Journal/Console/契约组合 **279 passed（6.74s）**。权限扩展首轮 **12 passed / 2 failed（4.23s）** 是种子操作者没有显式 Restricted Grant，系统正确返回 403；改为显式授权的种子后 **15 passed（4.41s）**，覆盖 Restricted 允许/拒绝、只读/跨空间拒绝、Required 管理与在线门禁、CAS、来源/结果删除后的重放及便签写入后故障的整笔回滚。

浏览器新增独立用例首次 **13 passed / 1 failed（47.5s）**，短时重复登录触发现有限流；将 Observation 流程接入已有登录后管理测试。下一轮 **12 passed / 1 failed（23.8s）** 已完成提交和注释，测试跳转便签使用了错误 URL；修正为现有 ?id= 路由后 **13 passed（17.9s）**，验证当前人工提交→注释→读取实际便签→刷新保持两者，生产认证限流未改变。Console check 的 **29 tests**、类型/lint/typecheck/生产 build 通过。

最终 Journal/Note/管理事务/Required Surface/Console 契约组合 **356 passed（19.33s）**；Ruff **274 个文件**、mypy **272 个文件**、生成/文档/公开接口检查通过。当前切片完整 CI 仍待复验，不以 Focus 切片的上一轮 CI 代替。

Observation 切片完整 `make ci` **退出码 0**：**10,964 passed（599.01s）**、覆盖率 **84.42%**，全部性能门槛通过；TS SDK **18 tests**，Console **29 tests**、生产 build 和真实浏览器全过。Ruff **274 个文件**、mypy **272 个文件**、文档/导入/契约/公开接口检查通过。新 sdist → wheel、独立 SDK、新 venv 隔离安装、安装物公开接口及 Required 模式 SDK/Worker Smoke 通过；4 个私有/管理路径拒绝通过。Core **0.13.0**、Schema **15**，SIGTERM 排空约 **0.204s**，完成关闭后退出码 -15。

当前人工 Observation 与注释切片已闭环。安装检查仍显式使用开发 SQLite override，不作为生产 Allowlist/OS 隔离或 RC 验收。其余管理模块、Forget、手动导入导出、统计/Provider/Settings/Operation 和 Phase 14 生产硬化、长期运行、恢复及发布门禁继续实施。

## Claim 管理创建与纠正（后续切片）

2026-09-07 按 [ADR-0029](../adr/0029-console-claim-commands.md) 接通 Claim 创建与 supersede/dispute/retract。真实管理身份验证后复用所属 ClaimService 的证据、主体、去重、修订、CAS、双时间、审计/水位/Outbox 与撤回级联。创建固定 user_statement，纠正复用现有 explicit_correction 规则；请求不能自造来源权威或覆盖不可变主体。去重实际目标单独重新授权，普通业务 Required Surface 门禁保留。

独立 Console 1.1.0 开发候选现为 **115 个操作、99 个路径**，新增两个 POST 操作、五个 Schema 和 correct 动作枚举。已逐项审阅公开接口快照，只有上述 Console 变化；业务 HTTP、Python/TS SDK 与 CLI 不变，Core 0.13.0 / Schema 15 不变。

HTTP 首轮 **6 passed / 1 failed（2.06s）** 定位出管理适配层错误假设领域纠正收据含 claim_id；改为在适配层补入已授权目标 ID，原业务契约未改。随后生命周期 **7 passed（2.23s）**。新增对抗测试的回滚断言先误计入测试登录审计，修正为登录后拍摄事务基线；最终 **20 passed（6.71s）**，覆盖 Restricted 显式允许/拒绝、只读/跨空间拒绝、隐藏目标去重、无效/缺失/错误修订/伪造权威证据、来源和结果删除后重放、整笔纠正回滚，以及普通业务 Required Surface 拒绝无 Lease 写入。

Console check 的 **29 tests**、类型/lint/typecheck/生产 build 通过，mypy **274 个文件**通过。真实浏览器首轮已完成创建但错误断言详情标题为正文，实际标题使用资源名称；改为核对正文后 **13 passed（19.1s）**。验证当前 Observation 证据创建 Claim、更正/争议/撤回、每步刷新及原内容历史可读，终态动作禁用。当前完整 CI 尚待本切片复验。

Claim/Focus 提升/管理事务/Required Surface/Console 契约组合 **380 passed（23.29s）**；Ruff **276 个文件**、mypy **274 个文件**、文档链接检查通过。完整 `make ci` 已启动，结果待核验。

Claim 切片完整 `make ci` **退出码 0**：**11,000 passed（608.16s）**、覆盖率 **84.42%**，全部性能门槛通过；TS SDK **18 tests**，Console **29 tests**、生产 build、真实浏览器 **13 passed（19.6s）**。Ruff **276 个文件**、mypy **274 个文件**、文档/导入/契约/公开接口检查通过。新 sdist → wheel、独立 SDK、新 venv 隔离安装、安装物公开接口、Required SDK/Worker Smoke 与 4 个私有/管理路径拒绝通过；Core **0.13.0**、Schema **15**、Python SDK **0.11.1**。SIGTERM 排空约 **0.209s**，完成关闭后退出码 -15。安装检查仍显式使用 `development_sqlite_override=true`，不代替生产 Allowlist/OS 身份隔离或 RC 发布验收。

Claim 创建与纠正切片已闭环，后续继续 Episode 等管理模块及 Phase 14 剩余生产门禁。

## Episode 创建、修订与状态管理（后续切片）

2026-09-07 按 [ADR-0030](../adr/0030-console-episode-revisions.md) 接通 Episode 创建、摘要/边界/参与实体/来源更新及状态动作。真实 CommandActor/当前 Grant 验证后复用所属领域创建与状态机；更新追加不可变修订、CAS、审计、水位和 Outbox。所有来源与参与实体逐项授权，Observation 不允许跨空间拼接。Current 标题、重要度和时间边界随指针从新修订同步；普通业务 Required Surface 检查保留。

首轮新 HTTP 与原 Episode/Relation 组合 **22 passed / 2 failed（4.71s）**，失败来自测试时钟固定为 2023 年但测试输入开始于 2026 年，管理封存正确拒绝反向时间；修正历史时间后 **24 passed（4.86s）**，并另加未来时间拒绝测试。扩展权限/重放/回滚/契约组合 **281 passed（9.33s）**。最终新专项 **28 passed（8.58s）**，覆盖三类命令的来源/参与实体/结果删除后重放、Restricted/只读/跨空间、错误来源修订、完整事务回滚、在线 Required 拒绝无 Lease 写入、真实召回收集期间更新导致 Final Rehydrate 丢弃旧候选，以及备份恢复保留修订与删除标记。

Console check 的 **29 tests**、生成类型/lint/typecheck/生产 build 通过。真实浏览器 **13 passed（20.2s）**，复用已有登录验证当前 Observation 引用创建 Episode、编辑摘要和开始时间、封存/归档、刷新与历史原摘要。Ruff **278 个文件**、mypy **276 个文件**通过。独立 Console 1.1.0 开发候选现为 **118 个操作、100 个路径**，新增三个操作、六个 Schema，无新动作枚举或 Core Migration。

接口核对发现工作区同时新增离线 `init --initialize-search`，不是 Episode 管理实现的改动；已保留并将其现有成功/失败回滚测试纳入组合验证。公开接口候选需同时审阅这一 CLI 参数，不能宣称本轮 CLI 完全未变。完整 CI 尚待当前组合复验。

Episode/原 Episode-Relation/Focus 提升/Console 读面/Required Surface/契约及初始化组合 **421 passed（35.38s）**。已审阅并接受公开接口快照，只有三个 Console 操作、六个 Schema 和既有工作区的可选初始化搜索参数变化，业务 HTTP 与 Python/TS SDK 未变。完整 CI 已启动，待核验结果。

Episode 与当前初始化搜索组合完整 `make ci` **退出码 0**：**11,047 passed（639.31s）**、覆盖率 **84.53%**，全部性能门槛通过；TS SDK **18 tests**，Console **29 tests**、生产 build、真实浏览器 **13 passed（48.5s）**。Ruff **278 个文件**、mypy **276 个文件**、文档/导入/契约/公开接口检查通过。新 sdist → wheel、独立 SDK、新 venv 隔离安装、安装物公开接口、Required SDK/Worker Smoke 与 4 个私有/管理路径拒绝通过。Core **0.13.0**、Schema **15**、Python SDK **0.11.1**；SIGTERM 排空约 **0.128s**，完成关闭后退出码 -15。CI 前后 **916 个代码/契约等输入文件的 SHA-256 一致**。

Episode 管理切片已闭环；安装仍显式使用开发 SQLite override，生产 Allowlist、OS 隔离、长期运行和发布门禁不据此标记完成。后续继续 Relation 等模块。

## Relation 创建、有向更正与状态管理（后续切片）

2026-09-07 按 [ADR-0031](../adr/0031-console-relation-corrections.md) 接通 Relation 创建、correct 和 transition。更正可以改变端点、方向、类型、评分及有效期，保留旧修订，通过 CAS 同步 Current 并追加纠正证据。管理去重命中实际目标后重新授权；实际新增证据会创建新修订及完整发布记录，完全重复不追加。撤回等非可编辑状态不能借创建复活。普通业务 Required Surface 门禁保留，不伪造 admin 或高权威来源。

首轮新 HTTP/原 Episode-Relation 组合 **20 passed（3.53s）**；描述器复用局部变量导致 mypy 推断 tuple 长度冲突，改为独立 relation_dependencies 后通过。扩展新 HTTP **27 passed（8.97s）**；最终新专项 **30 passed（9.83s）**，覆盖 Restricted/只读/跨空间、隐藏去重/碰撞、证据与端点伪造、三种命令的来源/端点/结果删除后重放、方向及双时间历史、去重/更正/状态事务回滚、原证据 Forget 后更正关系级联撤回及恢复，以及真实召回期间改向导致旧图候选丢弃。有效证据读取增加可选过滤与 501 条查询上限，旧调用默认行为不变。

有界证据读取改动后的 Relation/Claim/Episode/端点隐私/Required Surface/管理事务/契约组合 **424 passed（35.24s）**。Ruff 与 mypy **278 个源码文件**检查通过（格式化总文件数另计）；Console check **29 tests**、生成类型/lint/typecheck/生产 build 通过。真实浏览器 **13 passed（49.1s）**，在隔离夹具中增加两个真实 Entity，用当前 Observation 证据创建关系、更正为反向新类型、刷新、撤回并读取原关系历史。

已审阅公开接口候选，仅新增三个 Console 操作和五个 Schema，共 **121 个操作、102 个路径**；新增 17 个契约夹具。业务 HTTP、Python/TS SDK、CLI 及 Core 0.13.0 / Schema 15 不变。完整 CI 尚待当前切片复验。

补充真实 GraphProjection 增量验收 **1 passed（0.53s）**：创建关系后构建真实 generation；管理更正方向后、投影更新前，旧边已不能作为遍历跳板；增量 apply 后只保留新方向与新修订，撤回后移除关系边，manifest 校验通过。此前召回竞争用例使用测试路由在真实 Orchestrator 收集期间提交更正；这次另行验证实际持久化图边。新 Relation 专项共 31 项。Ruff **280 个文件**、mypy **278 个文件**及公开接口/文档检查通过，完整 CI 已启动，结果待核验。

Relation 当前切片完整 `make ci` **退出码 0**：**11,095 passed（664.27s）**，覆盖率 **84.65%**；TS SDK **18 tests**，Console **29 tests**、生产 build、真实浏览器 **13 passed（46.3s）**。Ruff **280 个文件**、mypy **278 个文件**及文档/导入/契约/公开接口检查通过。新 sdist → wheel、独立 SDK、新 venv 隔离安装、安装物公开接口、Required SDK/Worker 和 4 个私有/管理路径拒绝均通过。Core **0.13.0**、Schema **15**、Python SDK **0.11.1**；SIGTERM 约 **0.223s**，完成关闭后退出码 -15。CI 前后 **940 个输入文件 SHA-256 一致**。开发 SQLite override 仍开启；本证据不代替生产 Allowlist、OS 隔离、Soak 和发布验收。

## Artifact 不可变文本创建与完整性（后续切片）

2026-09-07 按 [ADR-0032](../adr/0032-console-immutable-text-artifacts.md) 接通 Artifact inline 文本创建。管理入口复用已有 ArtifactService，验证真实操作者、Scope、隐私、至多一个来源，以及实际去重目标。正文、媒体类型和来源不可原地替换；同内容但不同元数据返回冲突。客户端无法指定存储 locator、URL、哈希或权威，普通业务 Required Surface 门禁保留。

Console 文本披露增加实际字节、记录大小和 SHA-256 校验，严格 UTF-8 解码；校验在授权之后执行。去重时损坏对象不能继续累加引用。新增 22 个专项 HTTP/应用验证，覆盖字节上限、权限、去重、重放、损坏、回滚、Forget 与备份恢复。首轮 **35 passed / 1 failed（6.69s）**，失败为测试引用 LeaseExpiredError 的模块位置错误，修正后连同原 Artifact、Console 读面、管理事务、契约组合 **385 passed（24.99s）**。mypy **280 个源码文件**及 Ruff 通过；Console check **29 tests**、生产构建通过。

公开接口候选经审阅只增加一个 Console 操作与两个 Schema，11 个新增契约夹具；业务 HTTP、SDK 与 CLI 不变。真实浏览器和完整 CI 待核验。本切片只关闭原始文本入口，受限文件上传、Console Forget、其他管理模块和生产门禁仍待实施，不据此声明 Artifact 或 Phase 14 整体完成。

真实浏览器 **13 passed（48.2s）**：使用同一真实登录会话创建 Markdown 原始文本和 Observation 来源，刷新后保持内容；脚本文本作为普通文字显示，未执行。Ruff **282 个文件**、mypy **280 个文件**、生成契约与公开接口快照一致。完整 CI 开始复验，结果待核验。

Artifact 文本切片完整 `make ci` **退出码 0**：**11,128 passed（639.34s）**、覆盖率 **84.64%**；TS SDK **18 tests**，Console **29 tests**、生产 build、真实浏览器 **13 passed（48.0s）**。Ruff **282 个文件**、mypy **280 个文件**及文档/导入/契约/公开接口检查通过。新 sdist → wheel、独立 SDK、新 venv 隔离安装、Required SDK/Worker 和 4 个私有/管理路径拒绝均通过。Core **0.13.0**、Schema **15**、Python SDK **0.11.1**；SIGTERM 约 **0.131s**，完成关闭后退出码 -15。CI 前后 **955 个输入文件 SHA-256 一致**。继续附件上传切片；开发 SQLite override、生产隔离/Soak/发布门禁的限制保持明确。

## Artifact 原始文件上传与有界文件读取（后续切片）

2026-09-07 按 [ADR-0033](../adr/0033-console-bounded-artifact-upload.md) 实现独立原始字节上传及 metadata 契约。每个文件 1 字节至 8 MiB、每个 API 进程两个并发上传、60 秒接收超时；接收前和事务内分别检查授权，拒绝未知元数据、客户端路径/URL/哈希及模糊长度 framing。沿用 ArtifactService 的不可变 local_blob、当前去重目标授权和隐私规则；原业务 Surface 门禁不变。

跟踪本次分配的文件，异常后仅清理未提交 ID；提交成功但响应丢失保留文件供幂等重放，失败的去重调用不删除原附件。本地文件读取改为验证打开的普通文件句柄与长度，最多读 expected_size + 1，再检查哈希；FIFO、超大损坏文件和不安全路径拒绝。SIGKILL 孤立文件对账仍待生产恢复验收。

新增 36 个专项验证；首轮新上传、文本与原 Artifact 组合 **71 passed / 1 failed（15.37s）**，失败是只读测试夹具漏传 restricted 参数，已修正。前端 upload 可选字段曾误放入分页 descriptor 的类型声明，已移入 ResourceType。mypy **282 个源码文件**、Ruff **284 个文件**通过；Console check **29 tests**、生产 build 通过。新上传/文本/原 Artifact/备份恢复/Console 读面/事务/Required Surface/契约组合 **478 passed（38.69s）**。

浏览器首轮 **12 passed / 1 failed（22.7s）**：文件已实际上传且服务返回 201，失败断言试图使用 Chrome 未提供的 File postDataBuffer（返回 null）。改为用同一会话、原始二进制与相同幂等键重放：服务器请求指纹必须相同且返回原 ID，才能证明浏览器实际传输内容一致，避免以客户端网络事件充作字节证据。

公开接口候选仅增加一个 Console 上传操作、三个 Schema 和 ResourceTypeDescriptor 可选 upload/upload_schema，8 个新增契约夹具；原业务 HTTP、Python/TS SDK、CLI 和 Core 0.13.0 / Schema 15 不变。完整 CI 尚待当前组合复验。

修正后的真实浏览器专项 **1 passed（8.2s，业务流 6.5s）**：选择含 NUL/非 UTF-8 字节的本地文件，原始上传成功；以相同幂等键和预期二进制重放获得同一 ID，确认服务器收到一致内容；客户端文件名未进入 URL，响应不含 locator，刷新后保留 local_blob 元数据。完整 CI 开始复验，结果待核验。

首轮完整 CI **11,168 passed / 4 failed（691.69s）**，失败均为已有 Focus/Note/State/Task 接口对整个 ResourceTypePage 的真实响应校验：新 upload 动作的 id 和 description 尚未加入严格的 CommandActionSpec，上传专项只校验了内嵌 upload_schema，未覆盖整个注册表。CI 前后 **969 个输入文件哈希一致**，不是并发工作区漂移。已补齐 upload 枚举与有界 description，上传专项同时校验完整 ResourceTypePage；公开接口修订仅更新该动作 Schema。正在重验相关接口并重新执行完整门禁，失败轮次不作为通过证据。

注册表契约修正后，新上传与原 Focus/Note/State/Task HTTP、契约组合 **367 passed（23.85s）**；mypy **282 个文件**、Console check **29 tests** 与生产 build 通过，文档和公开接口一致。开始第二轮完整 CI。

Artifact 上传修正轮完整 `make ci` **退出码 0**：**11,172 passed（659.88s）**、覆盖率 **84.67%**；TS SDK **18 tests**，Console **29 tests**、生产 build、真实浏览器 **13 passed（49.3s）**。Ruff **284 个文件**、mypy **282 个文件**及契约/公开接口检查通过。新 sdist → wheel、独立 SDK、新 venv 隔离安装、Required SDK/Worker 和 4 个私有/管理路径拒绝通过。Core **0.13.0**、Schema **15**、Python SDK **0.11.1**；SIGTERM 约 **0.207s**，完成关闭后退出码 -15。CI 前后 **969 个输入文件 SHA-256 一致**。开发 SQLite override 仍开启；生产隔离、Soak、SIGKILL 孤立文件对账及完整发布门禁仍待验收。

## Identity 注册与绑定生命周期（后续切片）

2026-09-07 按 [ADR-0034](../adr/0034-console-identity-registry.md) 实施实体创建、外部身份注册、绑定提议/确认/撤销及身份 lookup。提取 IdentityService 共享事务方法，保留原业务管理员门禁；Console 使用当前操作者和 Grant，不伪造 admin。同名不合并，未确认关联不成为 verified；竞争绑定保留提交冲突状态，确认和撤销保留发生时身份历史。

暂存事务提取与管理接线分别跑原有身份测试，均 **29 passed**（1.35s、1.19s）。接入后新旧身份组合 **55 passed（9.40s）**，新增 26 项覆盖严格契约、全局写授权、隐藏自然键/竞争目标、显式确认与撤销、CAS、幂等历史回执、删除后重放和事务回滚。初次 mypy 发现测试局部 body 的推断类型冲突，已明确标注；修正后 mypy **285 个文件**通过。前端生成类型已同步，Console **29 tests**、生产 build 通过。

扩展组合首轮 **583 passed / 54 errors（38.70s）**，54 个错误均为沙箱禁止契约夹具绑定本地端口，正以允许监听的执行权限复跑。首轮真实浏览器在实体、身份和绑定提议创建后发现确认动作请求携带多余空 fields，严格契约正确返回 400；已修复通用表单中 confirm/revoke 的编码，正在复验。已审阅并接受公开接口候选：只增加六个 Console 操作、七个 Schema 与动作/lookup 枚举，业务 HTTP、SDK、CLI 没有漂移。完整 CI 尚未启动。

允许本地监听后的扩展组合 **637 passed（63.67s）**；修正请求编码后的真实浏览器专项 **1 passed（9.1s，业务流 7.7s）**，完成实际身份注册、绑定确认/撤销、刷新与历史查看。额外回滚验证首轮 **27 passed / 3 failed（9.48s）**，失败是测试错误地要求幂等 admission 行消失；既有协议会保留 failed_replayable 租约。断言已改为 Canonical/审计/成功 outcome 全部回滚、租约无成功响应，并验证同键重试及重放只产生一个实体/身份/绑定。隔离备份恢复确认撤销不复活、发生时身份仍保留。

最终 Identity HTTP 专项 **30 passed（9.62s）**。Ruff **287 个文件**、mypy **285 个文件**、生成契约/公开接口、Console **29 tests** 与生产 build 通过。开始完整 CI；生产隔离、Entity 属性/redirect/tombstone 以及其他 Phase 14 门禁仍未完成。

Identity 注册/绑定切片完整 `make ci` **退出码 0**：**11,222 passed（669.98s）**、覆盖率 **84.73%**；TS SDK **18 tests**，Console **29 tests**、生产 build、真实浏览器 **13 passed（47.5s）**。Ruff **287 个文件**、mypy **285 个文件**及文档/导入/契约/公开接口检查通过。新 sdist → wheel、独立 SDK、新 venv 隔离安装、Required SDK/Worker 和 4 个私有/管理路径拒绝通过。Core **0.13.0**、Schema **15**、Python SDK **0.11.1**；SIGTERM 约 **0.256s**，完成关闭后退出码 -15。CI 前后 **998 个输入文件 SHA-256 一致**。Console 开发候选共 **129 个操作、106 个路径**。继续实体重定向；开发 SQLite override、生产隔离/Soak/发布门禁保持未完成。

## Entity 受控重定向（后续切片）

2026-09-07 按 [ADR-0035](../adr/0035-console-bounded-entity-redirect.md) 接通实体重定向。复用 IdentityService 共享事务；原公开管理员门禁保持。Console 当前授权覆盖两端与整条关联链，重定向不改写历史 Binding/Relation 引用；补足长链尾部新增导致前驱超过 16 层的边界，并限制前驱查询的行数、时间与 SQLite 步数。

原身份测试暂存验证 **29 passed（1.07s）**。隔离临时副本首轮新旧组合 **68 passed / 6 failed（16.66s）**，原因是误用会永久设置 query_only 的读预算工具，后续写入被拒绝；改为独立、有清理保证的遍历预算。修正轮 **70 passed / 4 failed（16.35s）**，余下是测试调用不存在的 add_tombstone 和混淆历史/当前 IdentityView 返回约定，修正后 **74 passed（18.13s）**。额外 5000 个前驱容量负例 **1 passed（0.75s）**，未提交尾部边。

接入主代码后新重定向/Identity/原身份规则/Console 读面与事务/Console 契约组合 **458 passed（36.44s）**；mypy **286 个文件**、Console check **29 tests** 与生产 build 通过。真实浏览器专项 **1 passed（10.1s，业务流 8.5s）**：选择明确目标、提交重定向、刷新后动作禁用、历史保留 provisional。已审阅并接受公开接口快照，仅增加一个 Console 操作、一个 Schema 与动作枚举，六个新增夹具；业务 HTTP、SDK、CLI 未变。正补图投影与隔离恢复验证，完整 CI 尚未启动。

重定向专项与原 Graph 组合 **145 passed（54.62s）**，验证重定向后实际图边移除而 Canonical Binding 原实体 ID 保持，隔离备份恢复保留发生时身份与后续重定向链。Ruff **288 个文件**、mypy **286 个文件**、契约/公开接口、前端构建通过；开始完整 CI。

Entity 重定向切片完整 `make ci` **退出码 0**：**11,244 passed（679.98s）**、覆盖率 **84.76%**；TS SDK **18 tests**，Console **29 tests**、生产 build、真实浏览器 **13 passed（46.5s）**。Ruff **288 个文件**、mypy **286 个文件**及文档/导入/契约/公开接口检查通过。新 sdist → wheel、独立 SDK、新 venv 隔离安装、Required SDK/Worker 和 4 个私有/管理路径拒绝均通过。Core **0.13.0**、Schema **15**、Python SDK **0.11.1**；SIGTERM 约 **0.248s**，完成关闭后退出码 -15。CI 前后 **1006 个输入文件 SHA-256 一致**。Console 开发候选共 **130 个操作、107 个路径**。实体属性、tombstone/Forget 及生产隔离、Soak 与发布门禁仍未完成。

## Entity 属性快照与显式记录（后续切片）

2026-09-07 按 [ADR-0036](../adr/0036-console-entity-attribute-snapshots.md) 接通独立属性读取与记录。属性写入沿用 IdentityService 权威规则，不改 Entity 基础标签或修订；POST 同时检查 Entity 修订和完整属性快照，阻止相同 Entity 修订下的并发覆盖。原公开入口能力检查保持，Console 使用真实操作员及当前 Grant，不伪造 admin。界面区分生效、忽略和冲突结果。

共享属性事务提取的原身份测试 **29 passed（1.16s）**；初次接入新旧身份/重定向组合 **83 passed（20.68s）**。扩展属性/身份/重定向/原身份规则/Console 读面与事务/Console 契约组合 **485 passed（40.99s）**。最终新增 HTTP 专项 **19 passed（6.59s）**，涵盖快照竞争、全部权威结果、实际操作者 provenance、当前权限、删除后重放、超大值预检、容量回滚、失败重试、旧异常时间和隔离恢复。

Ruff **290 个文件**、mypy **288 个文件**、Console check **29 tests** 与生产 build 通过。真实浏览器专项 **1 passed（10.5s，业务流 8.8s）**：确认、更正、同级冲突反馈与刷新后的当前值/冲突均通过。已审阅并接受公开接口候选：两个 Console 操作、两个 Schema、十个夹具及 attributes 动作枚举，业务 HTTP、SDK、CLI 无漂移。

实体属性切片完整 `make ci` **退出码 0**：**11,273 passed（686.77s）**；Console **29 tests**、生产 build、真实浏览器 **13 passed（47.2s）**，公开接口检查、新 sdist → wheel、独立 SDK、新 venv 隔离安装、Required SDK/Worker 与 4 个私有/管理路径拒绝均通过。Core **0.13.0**、Schema **15**、Python SDK **0.11.1**；SIGTERM 约 **0.208s**，关闭后退出码 -15。CI 前后 **1021 个输入文件 SHA-256 一致**。删除预览、Forget、Entity tombstone 及生产隔离、Soak 和发布门禁仍未完成。

## 六类内容的持久删除预览与原子 Forget

2026-09-07 按 [ADR-0037](../adr/0037-console-durable-forget-previews.md) 接通 Note、Observation、Claim、Episode、Relation、Artifact 显式 1–50 项删除预览与提交。预览绑定实际操作员、Grant、固定目标修订/状态、Hold 与删除水位；提交复用 Core 删除账本和 outbox，不伪造 admin。界面区分 soft/erase，明确确认后提交；筛选集合入口保持关闭。Schema 16 新增预览持久表，旧 Migration 不变。

HTTP 专项 **30 passed（9.06s）**，另加 **50 项上限 1 passed（0.94s）**；覆盖六类内容 Required 模式、当前权限/隐私/全局范围修改拒绝、严格输入、保护/Hold、预览变更/过期、回执重放、整批晚期回滚、旧备份删除重放、soft 文件保留、erase 文件擦除与清理失败后的持久任务重试。新迁移 **7 passed（0.50s）**；验证 15→16 保留旧行/校验和、重复执行、外键、有效期、模式、JSON/大小和消费状态约束。组合验收 **387 passed（13.40s）**，包括最终 31 项 HTTP、7 项迁移、原 Forget 与 Console 契约。

较早的扩展回归 **360 passed / 2 failed（80.14s）** 中，两处失败为测试夹具错误（pin 缺少必需 reason；试图创建 Schema 禁止的全局 Artifact），已修正并由最终专项覆盖。早期迁移版本固定值与新动作数组断言同步；生产 Artifact agent_id 非空约束保持。临时契约写入遇到非法 Unicode 后，使用 SHA-256 与属性 CI 基线完全相同的备份恢复源文件，再以转义 Unicode 重新生成，没有丢失先前契约。

Ruff **294 个文件**、mypy **292 个文件**、文档/导入/公开接口与兼容检查通过。Console **29 tests** 与生产 build 通过。真实浏览器专项 **1 passed（16.8s，业务流 14.6s）**，验证预览、确认、删除回执、详情/历史 404 及刷新后正文不可见。公开候选已审阅并接受：两个 Console 操作、七个 Schema、forget 动作和能力描述字段；业务 HTTP/SDK/CLI 未扩张。

本切片完整 CI 正在执行；输入快照 **1046 个文件**。不据专项结果提前宣称完整 CI、Entity tombstone、大批次/筛选集合 Operation、清理查询或 Phase 14 发布验收完成。

Forget 第一轮完整 CI：**11,323 passed / 1 failed（697.61s）**、覆盖率 **84.85%**。唯一失败为 `test_phase568_review_regressions` 的既有迁移结果断言仍为 `[14,15]`，实际正确应用 `[14,15,16]`；已补齐预期，业务读写未发生失败。CI 前后 **1046 个输入 SHA-256 一致**。此轮停在 Python 测试门禁，未把未执行的前端、浏览器和隔离安装记作通过；修正后重新执行完整 CI。

修正后的第二轮完整 CI：Python **11,324 passed（708.15s）**、覆盖率 **84.84%**；TS SDK **18 tests**，Console **29 tests**、生产 build、真实浏览器 **13 passed（48.2s）**。Ruff **294 个文件**、mypy **292 个文件**与公开接口/契约/文档检查通过。此轮在隔离安装脚本另一处旧字符串断言（schema-version 仍期望 `15`）停止；运行时正确返回 `16`。CI 前后 **1046 个输入 SHA-256 一致**。

仅修正 `tools/smoke_installed.py` 的这一断言后，独立重跑完整 package-check **退出码 0**：新 sdist → wheel、独立 SDK、新 venv 安装、公开接口、Required SDK/Worker 与 4 个私有路径拒绝全部通过。Core **0.13.0**、Schema **16**、Python SDK **0.11.1**；SIGTERM 约 **0.258s**，关闭后退出码 -15。两个输入快照的差异仅为该 smoke 检查脚本，业务源码、契约、SDK 和前端均未改变；package-check 前后 **1046 个输入一致**。这是分段补齐后的全部门禁通过，未将第二轮 `make ci` 的非零退出码改记为零。


## Entity 软删除与历史账本恢复（后续切片）

2026-09-07 按 [ADR-0038](../adr/0038-console-entity-tombstone-ledger.md) 将 Entity 接入固定集合删除预览与原子提交，只支持 soft。共享领域事务同时写 Entity 状态、Tombstone、删除账本、真实 actor 审计、Recall 响应清理及 Graph/Profile 持久任务。实际 Agent self_entity 关系和 Subject Legal Hold 在 Console 与原管理员入口都受保护。原业务 Forget 的六类准入保持不变。

Schema 17 的 Migration 0017 为历史 Entity Tombstone 补账，保留原 actor/reason/time/sequence，已存在专属账本的对象跳过。已有库必须停机且验证备份；空库允许 bootstrap。最新删除账本可在 Schema 16 备份上重放，之后再离线升级；备份早于创建或早于删除都不会复活对象，重复重放和升级不重复补账。Binding/ExternalIdentity 的原始 ID 和修订保留，普通详情/历史以及当前、发生时身份解析均拒绝已删除实体。

删除预览增加属性快照摘要，因此不增加 Entity 修订的属性变化仍使旧预览失效。批量提交按当前必需引用先删引用方，再删目标，覆盖 Claim→Entity 与重定向源→目标的逆序输入。注册表发布允许的 forget_modes，界面按所选类型取交集，严格请求 Schema 同时拒绝 Entity erase。

| 此轮验收 | 结果 |
| --- | --- |
| 主代码首次 Entity/Forget/契约组合 | **521 passed / 54 errors（34.74s）**；错误来自沙箱禁止 mock 服务绑定回环端口，定向运行也未满足全量覆盖率门槛；获准监听后按下一行重跑 |
| Entity/原 Identity/六类 Forget/迁移/完整契约组合 | **706 passed（59.61s）**，定向运行关闭全量覆盖率门槛 |
| 补混合 Entity+Note 事务失败回滚、同键重试与模式发现 | **41 passed（12.90s）** |
| 新旧账本、补账不重复、离线升级门禁和备份恢复 | **5 passed（0.52s）** |
| 控制台生产检查 | 类型生成、lint、typecheck、**29 tests** 和 build 通过 |
| 真实浏览器首次 | **12 passed / 1 failed（29.9s）**；页面已只有 soft，测试按 label 精确定位未匹配，改用可访问 combobox 名称 |
| 修正后的真实完整业务流专项 | **1 passed（10.7s，业务流 9.7s）**；Entity soft 预览、确认、提交、详情/历史/属性 404 和重载通过，后续 Note 删除仍通过 |

公开接口候选已审阅：仅 Console ForgetTarget、PreviewRequest、Preview 与资源描述的模式能力摘要变化；无新增 HTTP 操作、业务 SDK 或 CLI 漂移。Ruff **297 个文件**、mypy **295 个文件**、文档、生成契约与公开接口检查通过。完整 `make ci` 正在执行，开始时 **1052 个输入文件**；未提前记为通过。大集合 Operation、其他类型 Forget、完整生产隔离/安全/Soak/故障与发布门禁仍未完成。


CI 运行期间补查发现历史补账的相关子查询仅使用既有索引的 tenant 前缀，形成重复租户扫描。隔离 SQLite 实验中 2,000 个历史 Tombstone 与 2,000 条既有账本需要约 2,414 万条 VM 指令；一次物化既有账本后约 18.4 万条，输出一致。主动中止首轮 CI（不记为通过），中止前后 **1052 个输入文件摘要一致**。修正仍在本轮未发布的 Migration 0017 内，未改旧迁移；真实 Schema 增加每次 200 万 VM 指令预算的 2,000 项回归，并验证重复执行无重复。受影响迁移/恢复组合 **32 passed（1.02s）**。随后重新启动完整 CI。


Entity 切片最终完整 `make ci` **退出码 0**：**11,342 passed（708.95s）**、覆盖率 **84.86%**；TS SDK **18 tests**，Console **29 tests** 与生产 build，真实浏览器 **13 passed（46.6s）**。文档/导入边界/类型/契约/公开接口、新 sdist → wheel、独立 SDK、新 venv 隔离安装、Required SDK/Worker 和 4 个私有/管理路径拒绝全部通过。Core **0.13.0**、Schema **17**、Python SDK **0.11.1**；SIGTERM 约 **0.261s**，完成关闭后退出码 -15。CI 前后 **1052 个输入文件 SHA-256 一致**。此结果关闭 Entity 切片验收，不代表开发 SQLite override、生产隔离、Soak 或完整发布门禁已完成。


## Focus 删除与工作集清理（后续切片）

2026-09-07 按 [ADR-0039](../adr/0039-console-focus-forget.md) 接通 Console Focus soft/erase，复用固定预览、当前 Grant/Scope/Privacy、Hold、最近重新认证、真实操作者账本/审计、失效任务与 Recall 响应清理。普通业务 Forget 的六类准入未扩大。删除保留原 ID、状态、修订与提升目标，erase 清除所有历史正文；SQL 擦除超时或超过步骤预算时整体回滚，可按原幂等键重试。

容量、列表与维护查询在排序/LIMIT 前排除 Tombstone，避免删除项继续占用工作集或挤占扫描名额。备份早于创建时重放原 ID 的 Tombstone，已有旧 Focus 则按原 soft/erase 模式恢复删除，重复重放不重复写入。不需要新 Migration，继续 Core 0.13.0 / Schema 17。

| 当前验证 | 结果 |
| --- | --- |
| 隔离草稿的 Focus/原 Forget 组合 | **78 passed（31.09s）**；之后补容量/维护过滤，相关组合 **48 passed（19.25s）** |
| 主代码 Focus/Entity/旧账本/原 Focus 与 Forget 组合 | **98 passed（33.10s）** |
| 最终 Focus 专项 | **13 passed（6.28s）**；soft/erase 全修订、原 Focus 当前/历史拒绝、两种备份时点恢复、50 项原子提交、混合事务回滚、Recall 响应清理、持久失效处理、Hold、查询容量，以及擦除超时回滚/同键重试和当前受限 Grant/密钥重校验 |
| 真实浏览器专项 | **1 passed（3.3s，业务流 2.3s）**；Focus 创建/更正/休眠/激活/提升 Task 后预览删除，Focus 详情/历史 404，提升的 Task 仍可读取，重载通过 |
| 前端和静态检查 | Console **29 tests**、生产 build、Ruff **298 个文件**、mypy **296 个文件**、生成契约/文档检查通过 |

公开接口候选审阅仅 Console ForgetTarget 与 Preview 中目标枚举摘要变化，无新增路由、SDK 或 CLI 变化，已接受。完整 CI 即将执行；State/终止 Task 删除、Operation、其他管理模块与生产/发布门禁仍未完成。


Focus 完整 Python 回归 **11,356 passed（700.33s）**、覆盖率 **84.92%**；TS SDK **18 tests**，Console **29 tests** 和生产构建通过。完整 CI 的 13 项浏览器用例执行成功后，Playwright worker 在退出阶段闲置；采样停留在 Node 事件循环且无业务网络连接。先发 SIGTERM 无效，再终止本任务 worker，流程继续执行安装门禁，最终 `make ci` **退出码 0**，浏览器报告 **13 passed（4.1m）**。因此这次完整 CI 含人工处理测试进程，不能称为自然无干预完成。

未修改代码随后重新执行整套浏览器测试，**13 passed（27.4s）且自然退出**。全新 sdist → wheel、独立 SDK、新 venv 隔离安装、Required SDK/Worker 和 4 个私有/管理路径拒绝均通过。Core **0.13.0**、Schema **17**、Python SDK **0.11.1**；SIGTERM 约 **0.247s**，完成关闭后退出码 -15。CI 前后及浏览器复跑后 **1054 个输入文件 SHA-256 一致**。此切片验收完成；State/终止 Task 删除及其余 Phase 14 生产、恢复、Soak、发布门禁仍未完成。


## State 删除与同键新代（后续切片）

2026-09-07 按 [ADR-0040](../adr/0040-console-state-forget-generations.md) 实现 State soft/erase，接入同一固定预览和删除账本事务；命名空间 user 权限、当前 Grant/Scope、Hold、Revision 与最近认证继续生效。erase 清理全部保留修订的值、来源和 coalesce_key，保留不可变身份元数据。

新增离线 Migration 0018：重建 State 主/修订表并保留原行与外键，Tombstone 派生的 deleted_us 释放已删除记录的自然键。只有显式 Console create(expected_revision=0) 可创建不同 ID 的新代；旧 ID、旧回执、恢复账本不能复活内容，普通 State upsert 不隐式重建。运行窗口为 Schema 18，空库可 bootstrap，已有库必须停机并验证备份。既有 Migration 未修改。

隔离测试先通过 11 个接口行为和 6 个迁移/恢复行为；扩展回归发现既有动作断言需要加入 forget，以及预算测试未达到回调步数/默认历史分页 50 的夹具错误，修正后定向预算中断/回滚/同键重试 **1 passed（1.94s）**。主工作区首轮组合 **633 passed / 3 failed / 54 errors（31.22s）**：三个失败为新增支持 Schema 18 后的未来版本拒绝断言需改为 19，54 个错误为沙箱不允许 Mock Server 绑定本地端口。已修正断言并在允许回环端口的环境重跑；未将首轮记为通过。

真实浏览器 State 创建、更正、过期、soft 删除、旧详情/历史 404、同名新建不同 ID/revision=1、刷新后读取新内容及旧 ID 仍 404：**1 passed（3.2s，业务流 2.1s）**。Console **29 tests** 和生产 build、mypy **298 个文件**通过。公共接口候选仅改变 Console ForgetTarget/Preview 两个目标枚举摘要，已审阅接受；没有新业务路由或 SDK 方法。完整 CI 与安装验证待完成，Phase 14 尚未进入发布验收。

State 主工作区的授权环境组合重跑 **690 passed（57.30s）**。之后补充恢复账本的逻辑身份断言：按现有恢复语义保留 app_instance、幂等键、selector、模式与原创建时间，恢复审计使用 `restore:deletion_ledger`，不把新账本的物理 ID 误当作原请求 ID；最终迁移专项 **6 passed（0.51s）**。

首轮完整 CI 在旧 Schema 6 备份回归暴露两个夹具遗漏：模拟旧库时只移除新表/索引，没有移除 0018 的 State 触发器和新主表结构，升级时触发器引用了临时已移除的表。隔离副本复现后主动中止该轮（**1,895 passed / 2 failed，548.54s**；未到安装门禁），中止前后 **1058 个输入文件 SHA-256 一致**。修正只在测试夹具中按不可变 0004 DDL 重建真实旧 State 主/修订表，移除现代触发器/列，保留数据与原唯一键并检查外键；未修改生产 Migration。旧备份/删除/迁移组合 **26 passed（2.26s）**，随后重启完整 CI。

State 修正后的完整 `make ci` **退出码 0**：Python **11,375 passed（743.34s）**、覆盖率 **84.96%**，TS SDK **18 tests**，Console **29 tests** 和生产构建，真实浏览器 **13 passed（47.1s，自然退出）**。新 sdist → wheel、独立 SDK、新 venv、公开接口快照、Required SDK/Worker 与 4 个私有/管理路径拒绝通过；Core 0.13.0 / Schema 18 / SDK 0.11.1，SIGTERM **0.209s**、退出码 -15，仍使用开发 SQLite override。

**组合证据限制**：CI 前后都记录 1058 个输入文件，但最终有 6 个摘要变化，涉及并行进行的 SSE/checkpoint 源契约、API、凭据能力、bootstrap、生成器及 TS SDK。中途一次摘要检查仍一致，变化发生在后段；这些文件没有被本 State 切片的安装脚本修改。已保留并行改动，不能把本轮称为同一不可变代码快照的完整验收，也不能据此声明这些 SSE 新改动已通过全部门禁。State 专项、迁移和浏览器结果有效；最终组合需要等并行改动稳定后重新验证。Task 仍在隔离草稿中，尚未整合；其余 Phase 14 发布门禁未完成。

## Task 固定删除与 Schema 19（2026-09-07，组合验收通过）

Core 0.13.0 开发工作区按 [ADR-0041](../adr/0041-console-task-forget-cascade.md) 整合 Task soft/erase、子对象 Tombstone、事件取消与恢复账本。Schema 19 增加五个有界查询索引；保留独立的业务状态、ACK、投递次数和历史身份。非终态、未兑现来源承诺、未删除独立子计划和 Hold 保持保护，子资源逐项检查权限；单个或合计超过 500 个级联成员的固定批次完整拒绝。

本轮 Task HTTP/存储共 22 个场景，包含两种删除模式、Schema 18/19 与创建前备份恢复、实际依赖、事件 ACK/投递保留、混合批次晚期失败回滚、失效任务执行、50 个根目标、500 成员上限、严格子资源隐私、承诺归档、独立子计划、引用顺序和恢复旧 active Task 后的列表/扫描不饥饿。初次主库组合为 **819 passed / 1 failed（83.09s）**：旧契约反例仍将 Task 作为不支持目标。反例已改成不接受独立删除的 TaskStep，同时增加 Task soft/erase 正例；相关契约与 Task 复验 **355 passed（13.46s）**。

真实浏览器覆盖触发器创建/编辑/启停、活跃 Task 隐藏删除动作、取消后生成含子对象数量的删除预览、提交、父资源/历史/触发器 404 及刷新后的 not_found：**1 passed（3.7s，业务流 2.7s）**。首次浏览器运行仅最后一处断言错误地要求刷新后保留临时成功提示；改为实际持久读取结果后重跑通过。Console **29 tests** 与生产 build、mypy **301 个文件**、Ruff/格式 **303 个文件**、契约兼容及文档检查通过。公共接口候选仅接受 Console ForgetTarget/Preview 两个枚举摘要变化，SDK 方法不扩大。

整合使用逐文件摘要保护，未覆盖并行 SSE/checkpoint 改动或 State 旧备份夹具修复。整合期间检查的八个 SSE 实现/测试文件摘要保持一致。当前 **1064 个输入文件**已记录基线，完整 `make ci` 正在运行；最终组合、安装产物与发布门禁尚不能据此宣布通过。此段替代上一段“Task 仍在隔离草稿”的当前状态，保留此前 CI 的证据限制。

Task 首轮完整 CI 在 Python 门禁结束：**11,402 passed / 1 failed（740.40s）**，覆盖率 **84.95%**，未进入后续 SDK 测试、浏览器和安装门禁。失败为另一处通用删除接口负例仍把 `task` 当作不支持类型；已改为独立 `task_step` 负例，相关删除接口组合 **51 passed（26.01s）**。该轮前后 **1064 个输入文件摘要完全一致**，没有并行代码漂移；不能因唯一失败来自旧测试预期而将 CI 记为通过。修正后已重新冻结输入并启动完整 CI，结果待补。

修正后的完整 `make ci` **退出码 0**：Python **11,403 passed（748.41s）**、覆盖率 **84.95%**，TS SDK **19 tests**，Console **29 tests** 与生产构建，真实浏览器 **13 passed（45.9s，自然退出，无干预）**。新 sdist → wheel、独立 Python SDK、新 venv、公共接口快照、Required SDK/Worker 与 4 个私有/管理路径拒绝通过。安装组合为 Core **0.13.0** / Schema **19** / Python SDK **0.11.1**，配套 TS SDK 为 **0.11.2**；SIGTERM **0.262s**，退出码 -15，仍使用开发 SQLite override。

该轮 **1064 个输入文件**在开始、中途和结束时 SHA-256 全部一致。当前 State/Task 与已保留 SSE/checkpoint 改动的组合门禁因此闭合，替代前一 State 轮次的混合输入限制；历史失败与限制仍按原记录保留。本结论只覆盖当前开发候选，不代替生产隔离、正式 SQLite Allowlist、24h Soak、进程故障、三轮恢复或发布物验收。Operation 大集合删除继续在隔离草稿实施，尚未进入主库功能面。


## 固定筛选与 Operation / Schema 20（2026-09-07，已整合，完整组合验收中）

依据 ADR-0042，将 1–500 个显式/筛选目标固化在预览事务中。至多 50 项继续同步提交，较大集合返回 202 并复用实际 Outbox/Worker/Lease；每批真实凭据、Hold、版本和水位复核。目标/子对象、删除账本、进度、下一作业与 fence 原子提交；晚期错误和失租回滚，死信更新匹配 Operation，取消仅停止后续。Schema20 恢复在切换前核对原请求账本，确认连续进度，完成或阻塞旧任务，绝不自动续删未提交集合。

隔离候选迁移/Task 删除/Operation/筛选/契约组合 **532 passed（46.04s）**；独立跨批 Task 引用顺序 **2 passed（2.23s）**。早期 23 项迁移失败均由隔离副本缺 Git 历史导致，接入只读历史后通过；新迁移夹具两处字段/metadata 访问错误已修正，并包含在 532 项的最终结果内。筛选、授权读面与 HTTP 组合 **62 passed（24.86s）**；实际 51/500 根、新 Worker、Hold、晚期 erase 失败、失租回滚/重试、取消及恢复均有集成覆盖。恢复专项 **8 passed（6.66s）**，含旧快照搭配最新账本、重复恢复与异常保持旧目录。

实际浏览器专项 **1 passed（3.4s，自然退出）**：筛选 51 条便签，202 接受，独立 Worker 进程提交 50 条，通过批量操作页面取消，再执行 Worker 后最后一项仍存活。完整浏览器首轮 13 passed/1 failed，暴露共享 Operation 组件的删除文案误用于导入模拟；已按 kind 区分。第二轮 13 passed/1 failed，新增用例触发真实来源地址登录限流；测试改为按 Retry-After 等待并重新输入一次性测试密钥，没有修改生产限流。最终完整浏览器与 Main 完整 CI 结果待补。

整合前保留主工作区并行的 Claim revision.invalidated.v1 发布和 Recall 返回/重放重新校验，组合 **68 passed（39.03s）**。新增 Claim 事件测试两处类型错误仅修正空值断言与变量命名，**6 passed（0.65s）**；mypy **319 个文件通过**。正式公共清单人工核对并接受 4 个 Operation 路由、新 DTO、Forget 202 响应和预览上限/selector 共 15 处差异；没有新增业务 SDK 方法或内部导出。

按照逐文件 SHA-256 校验整合 **223 个文件**，未覆盖并行 memory.py/recall.py 及其新测试语义。Console 29 tests、类型/静态检查与生产构建通过。此处证据来自隔离与专项组合；Main 的完整不可变 CI、安装产物和当前输入摘要尚未闭合，不能据此宣布发布可用。Phase14 的其他业务管理、生产隔离、安全、Soak、完整恢复凭据重置与发布验收继续未完成。


## 2026-09-07 Event dismiss 与 Schema20 组合修复（主目录已整合，完整验收待重跑）

[ADR-0043](../adr/0043-console-event-dismiss.md) 接受事件管理命令。主目录按 SHA-256 保护整合 171 个代码、契约、类型、夹具与测试文件，保留并行 Recall 批量重验协议。事件 pending/delivered → cancelled 使用真实 CommandActor、权限、父对象隐私和 CAS；无 ACK、Task 完成或 Tombstone。回执重放重新验权，历史/审计/水位/失效作业同事务处理。恢复已包含取消的真实备份保持 cancelled、历史与 Task 原状；不把业务取消当作永久删除。

实际回归发现 Recall 仍可能发布或重放取消前的 pending_event_ids，已补原有界 ID 集合的当前状态/Scope/过期/Tombstone 重验；不重选新增匹配，不修改原 Usage 记录。新增 51 个更早匹配事件也不会挤掉仍然有效的原 ID。应用、HTTP、域事件、Console 契约、Recall 发布及批量重验组合 **426 passed（16.30s）**；前端 **29 tests**、类型/静态检查及生产构建通过。公共接口清单仅新增一个 Console route 和三个 DTO，没有扩大业务 SDK 管理权限。

真实浏览器完整回归 **15 passed（1.1m）**，自然退出 0；包括事件 pending/delivered 两种状态取消、刷新后投递历史与 Task 不变，以及既有 Operation 首批提交与取消。初次运行发现通用表单多发空 fields，已修正；后续修正真实夹具的初始 active 状态，并为新增业务流提供独立临时凭据，避免旧夹具达到五个活动 Session 上限。生产认证与限流未放宽。

整合前主目录 `make ci`：静态/文档/契约/公共 API 检查通过，Python **11472 passed / 13 failed（885.40s）**，覆盖率见当次日志；完整 1201 个输入（包括文档）前后摘要相同，SDK/浏览器/安装后续门禁未运行。失败包括：Console 读预算退出错误恢复写权限；两处旧库升级断言遗漏 Schema20；四个 Worker 启用集合/Handler 表遗漏 console.memory_forget；Recall 新路由矩阵/路径和并发夹具缺少当前 Persona 元数据；一次 50 条 State 预览在覆盖率与并行负载下触及真实读取预算。普通只读事务预算结束后继续 query_only，仅真实写事务恢复原模式。集合/路由/夹具修复组合 **96 passed（10.42s）**，矩阵 **2 passed（1.39s）**。50 条 State 不放宽生产预算或测试断言，单项含覆盖率重验通过（5.07s），仍需本轮完整 CI 验证稳定性。

主目录整合后的静态、类型、生成契约、兼容及公共清单预检已通过。以上不是当前全量通过或发布证据；下一轮冻结输入后重跑完整 CI、浏览器与独立安装。Persona、保留策略、导入导出、统计、Provider/Settings/运维、生产隔离、安全、Soak、完整恢复凭据重置和稳定发布仍未完成。


### Operation / Event 当前完整组合验收通过（2026-09-07）

上节失败与待重跑状态由本节替代。本轮以 Core **0.13.0 / Schema20**、Python SDK **0.11.1**、TypeScript SDK **0.11.2** 的同一未提交候选执行 `UV_CACHE_DIR=.uv-cache CONSOLE_BROWSER_EXECUTABLE=… make ci`，全链自然退出 **0**。macOS ARM64 / Python3.12.13 / SQLite3.50.4 / Chrome；安装报告明确使用开发 SQLite Allowlist Override，不能据此证明生产平台验收。

- Python/Core/SDK 测试 **11513 passed（869.44s）**，覆盖率 **85.13%**；上轮十三处失败均不再出现，50 条 State 仍使用原生产预算与严格断言。
- TypeScript SDK **19 passed**；Console **29 passed**，生成类型、lint、typecheck 和生产构建通过。
- 完整真实浏览器 **15 passed（1.1m）**，自然结束。固定筛选 51 条接受、真实 Worker 提交 50 条、取消保留最后一条，以及事件 pending/delivered 取消后刷新和 Task 保留均通过。
- 格式、导入边界、文档、mypy（324 个文件）、生成契约、兼容及公共 API 清单通过。全新 wheel/sdist 重建、包边界、独立 venv 安装、安装后公共清单、真实 Required 模式 SDK 调用/Worker 与四个私有接口拒绝全部通过。安装报告 serve 退出码 -15，排空退出约 0.142s。
- CI 开始和自然完成之间 **1213 个输入文件（包括文档）SHA-256 完全一致**。本节和状态更新在完成并核对摘要之后写入，未在测试期间修改候选。

本轮验收关闭 Operation/Schema20 与 Event 管理切片；Phase14 仍为 In progress。Persona、保留策略、导入导出、统计、Provider/Settings/运维、生产权限隔离、安全/供应链、24h Soak、完整灾备及凭据恢复、RC/1.0 发布仍待实施和验收。未创建提交、RC、签名或发布产物。

## 2026-09-07 Persona 发布与回滚（主目录组合验收通过，历史过程如下）

[ADR-0044](../adr/0044-console-persona-publication.md) 接受实际 Console Persona 发布/回滚边界。按已有 Event 候选摘要核对并整合 **180 个文件**，不覆盖并行 Recall/SDK 工作。Core 0.13.0 / Schema20 / Console Contract 1.1.0；无新迁移或业务 SDK 管理方法。公开清单仅新增三个 Console 操作和六个 DTO，单独生成候选后审阅差异。

隔离副本运行 Persona 应用/HTTP/恢复、既有 Persona、Console 读面和契约测试：**467 passed（24.47s）**；覆盖父范围权限、近期认证、并发 CAS、缓存后撤权、失败回滚、PersonaState 来源链、过期/替换/不可见 Evidence、严格请求、Recall 失效和原 Usage 保留，以及备份恢复 Current/历史。Console 检查含 **29 tests**、类型、Lint 与生产构建通过；实际 Chrome 浏览器 **2 passed（3.6s）**，验证 Required 模式下只读角色及发布→重新认证→刷新→回滚→刷新，保留三版历史与 Bootstrap 原始 Hash。首次浏览器运行受本机监听沙箱限制，后续授权本机测试运行；模块发现和测试定位问题已修正。

主目录生成契约和文档预检通过；以上副本专项结果不等于主目录完整 CI 或发布验收。下一步冻结同一主目录输入并运行完整 Python/SDK/Console/浏览器/独立安装门禁。PersonaState 管理、Proposal 创建/审批/拒绝、Draft 生命周期、Policy 管理及其他 Phase14 工作仍未完成，阶段保持 In progress。

### 主目录首次组合结果与性能复查

首个完整 CI 在测试文件一处行宽检查停止，修正后第二轮自然退出：**11555 passed / 1 failed（903.50s）**，覆盖率 **85.14%**。唯一失败是结构化 Recall p95 **52.170458 ms > 50 ms**；SDK、真实浏览器与安装后续门禁未执行。包括文档在内的 **1239 个输入摘要前后完全一致**，没有把运行期间的改动混入结果。

保留相同分支覆盖率插桩、样本量与 50 ms 阈值，单项诊断 **1 passed（4.07s）**、p95 **25.673 ms**；整组性能诊断 **11 passed（43.42s）**，其中结构化 Recall p95 **26.145 ms**。诊断插件仅在仓库外记录原测量函数返回的完整样本，部分测试运行不要求全库覆盖率达到 80%；正式 CI 仍要求 80%。未改动 Recall 热路径、数据集、样本量或性能阈值，尚未证明首次差异的稳定原因。此结果不能替代完整组合验收，下一轮重新冻结输入并运行全部门禁。

### Persona 发布与回滚当前完整组合验收通过

上节待重跑状态由本节替代。相同代码重新冻结后执行 `UV_CACHE_DIR=.uv-cache CONSOLE_BROWSER_EXECUTABLE=… make ci`，完整命令自然退出 **0**。候选为未提交的 Core **0.13.0 / Schema20**、Python SDK **0.11.1**、TypeScript SDK **0.11.2**；macOS ARM64 / Python3.12.13 / SQLite3.50.4 / Chrome。

- Python **11556 passed（859.04s）**，覆盖率 **85.15%**，全部原性能门槛通过；没有放宽 Recall 的 50 ms 阈值。此前单次延迟差异的原因仍未确认。
- TypeScript SDK **19 passed**；Console **29 passed**，类型生成、Lint、类型检查和生产构建通过。
- 完整真实浏览器 **17 passed（1.1m）**，包括 Persona 只读权限、实际发布/重新认证/回滚与刷新后不可变历史。主目录的冒号回滚路由和撤销历史版本拒绝均纳入本轮候选。
- 格式、导入边界、文档、mypy、生成契约、兼容及公共 API 清单通过。全新 wheel/sdist、包边界、隔离 venv 安装、安装后公共清单、Required 模式真实 SDK/Worker 及四个私有接口拒绝全部通过。安装服务退出码 -15，关闭耗时约 **0.267s**。
- 开始与自然完成之间 **1239 个输入文件（包括文档）SHA-256 完全一致**；验收状态在核对完成后更新。安装使用开发 SQLite Allowlist Override，本记录不证明生产平台或稳定发布通过。

本轮关闭 Persona 发布与回滚切片。PersonaState 管理尚在隔离目录实施，Proposal、Draft、Policy 和其余 Phase14 工作仍未完成；阶段继续 In progress。未创建提交、RC 或发布产物。

## 2026-09-07 PersonaState 管理（主目录组合验收通过，历史过程如下）

[ADR-0045](../adr/0045-console-persona-state.md) 接受独立 State 版本、真实权限和到期基线回归边界。在已验收的 Persona 发布/回滚候选上，按逐文件摘要保护整合 **183 个文件**。复用 Schema20；公共清单经独立候选审阅，仅新增三个 Console 操作和六个 DTO，全部旧公共接口深比较一致。

实现实际 Current State 读取、更新和到期清理，同一事务执行独立 State CAS、不可变状态行、指针、审计、水位和到期任务；Persona Current 不变。页面固定打开表单时的状态版本，展示基线和到期时间；只读用户没有写动作。手工清理复用既有 Worker/catch-up 的基线与空当前来源语义，并要求原状态及其来源可见，保留旧行与旧来源引用。

隔离副本首轮 **31 passed / 1 failed（7.63s）**，失败为测试误用记录属性名，修正后扩大至 State/Persona/契约组合 **457 passed（19.60s）**。额外真实备份恢复和 catch-up/旧 Job 围栏 **1 passed（0.41s）**。覆盖独立版本、并发竞争、父范围权限、HTTP 严格请求、撤权重放、不可见证据、入队失败回滚、延迟任务及历史保留。

Console **29 tests**、生成类型、Lint、类型检查与生产构建通过。实际 Chrome 浏览器 **3 passed（6.8s）**，包括真实 State writer 更新、TTL 后清理、页面刷新、Persona Current 保留及既有发布/回滚。夹具使用独立最小权限临时凭据，测试结束删除；Required 模式和生产权限校验不放宽。

以上为隔离专项证据，主目录完整 CI、浏览器与安装组合尚待执行。系统时钟回拨仍可能推迟既有 TTL 回归；本切片没有完成 Proposal、Draft、Policy、生产硬化或 Phase14 稳定发布门禁。

主目录首轮完整命令在 mypy 阶段自然退出 2：恢复测试读取 Optional 状态后缺少非空断言。1264 个输入摘要前后相同，Python/SDK/浏览器/安装后续门禁未运行。补充状态存在断言后，完整 mypy **334 个文件**通过；重新冻结候选执行全链验证。

第二轮完整命令自然退出 **0**：Python **11601 passed（842.21s）**、覆盖率 **85.17%**；SDK TS 19、Console 29、真实浏览器 **18 passed（1.2m）**，静态/契约/公共 API、全新包和独立 Required 模式 SDK/Worker 安装验证通过。1264 个输入摘要前后相同。安装使用开发 SQLite Override，服务退出码 -15、关闭约 0.209s。

复查发现上述浏览器流程漏检了日期展示：API 返回 `expires_at`，页面误读 `expires_us`。完整 CI 通过没有证明这个显示要求已完成。随后仅修正页面字段并增加有效日期断言，Console 29 tests/检查/构建通过，真实浏览器 **3 passed（6.5s）**。修正后的候选仍需完整组合确认，不能直接沿用第二轮结果宣布最终验收。

### PersonaState 当前完整组合验收通过

日期修正和新增断言纳入同一冻结候选后，第三轮 `UV_CACHE_DIR=.uv-cache CONSOLE_BROWSER_EXECUTABLE=… make ci` 自然退出 **0**。Core 0.13.0 / Schema20 / Python SDK 0.11.1 / TS SDK 0.11.2；macOS ARM64、Python3.12.13、SQLite3.50.4、Chrome。

- Python **11601 passed（844.71s）**、覆盖率 **85.17%**，原有性能门槛全部通过。
- TypeScript SDK **19 passed**；Console **29 passed**，生成类型、Lint、类型检查和生产构建通过；完整真实浏览器 **18 passed（1.2m）**，包含有效到期日期断言。
- 格式、导入边界、文档、mypy、契约、兼容和公共接口清单通过。全新包构建、边界、隔离安装、Required 模式 SDK/Worker 及四个私有接口拒绝通过。安装服务退出 -15，关闭约 **0.262s**，仍使用开发 SQLite Override。
- **1264 个输入文件（包括文档）前后摘要完全一致**，本验收更新在命令结束并核对之后写入。

本节关闭 PersonaState 管理切片；Proposal、Draft、Policy 及其余 Phase14 发布工作继续实施。开发平台证据不代表生产隔离、安全、Soak 或完整灾备通过。

## 2026-09-07 Persona Proposal 管理（完整组合验收通过，历史过程如下）

[ADR-0046](../adr/0046-console-persona-proposals.md) 接受实际 Console 提案创建与人工审批边界。以已验收的 PersonaState 候选为基准，逐文件摘要保护整合 **192 个文件**。公共清单只新增五个 Console 操作与七个 DTO；移除新增项后，完整旧接口深比较一致。无迁移、业务 SDK 方法或版本变化。

普通 memory.write 创建始终保存待审提案，bounded_auto 亦不直接发布；generator 和审计身份来自实际操作者。批准/拒绝要求 persona.publish、父范围权限与近期认证，审批采用真实状态 CAS 和基准/Policy 检查；批准重新验证 Evidence 并原子发布，拒绝允许关闭基准已变化或已过期的提案且保留历史。页面已接通分页、详情、固定已审阅版本、敏感确认、重新认证和刷新。

隔离副本应用/HTTP、State/Persona、Phase9、Phase10 pipeline 与 Console 契约组合 **508 passed（56.97s）**。覆盖 Policy/过期/不可见或替换 Evidence、父范围权限、并发审阅、撤权重放、通知失败回滚及实际备份恢复后的 published/rejected/proposed、人格历史和审阅事件。完整 mypy **339 文件**、格式、Lint、公共接口清单和兼容检查通过。

Console **29 tests**、生成类型、Lint、类型检查和生产构建通过。实际 Chrome 浏览器 **4 passed（9.2s）**：独立 Agent、Required 模式和最小权限临时凭据下创建两份待审提案，审批人重新认证批准一份，拒绝基准已变化的另一份，刷新后状态和 Current 保持正确；包含既有 Persona/State 流程。新增审计原因断言纳入上述 508 项组合回归。

主目录组合验收尚待运行。Policy 管理当前仅有隔离原型，Draft 生命周期和其他 Phase14 工作继续实施；此结果不等于完整 Persona、生产运行或稳定发布验收。

### Persona Proposal 当前完整组合验收通过

在已整合且冻结的主目录运行 `UV_CACHE_DIR=.uv-cache CONSOLE_BROWSER_EXECUTABLE=… make ci`，自然退出 **0**。Core 0.13.0 / Schema20 / Python SDK 0.11.1 / TS SDK 0.11.2；macOS ARM64、Python3.12.13、SQLite3.50.4、Chrome。

- Python **11639 passed（855.73s）**，覆盖率 **85.20%**，原有性能门槛通过；TypeScript SDK **19 passed**。
- Console **29 passed**、生成类型、Lint、类型检查、生产构建与完整真实浏览器 **19 passed（1.2m）**。
- 格式、导入边界、文档、mypy、生成契约、兼容、公共清单及安装后公共接口检查通过。全新 wheel/sdist、边界检查、隔离安装、Required 模式 SDK/Worker 和四个私有接口拒绝通过。
- **1293 个输入文件（含文档）在运行前后 SHA-256 完全一致**。安装服务退出 -15，关闭约 **0.138s**；开发 SQLite Override 仍不代表生产环境验收。

本节关闭 Proposal 创建与人工审阅切片。Policy 当前仍在隔离副本实施，Draft、保留管理及其余 Phase14 发布门禁继续推进。没有提交、RC 或对外发布。

## 2026-09-07 Persona Policy 管理（完整组合验收通过，历史过程如下）

[ADR-0047](../adr/0047-console-persona-policy.md) 接受独立演进策略读取与敏感完整替换。以已验收 Proposal 候选为基准，逐文件摘要保护整合 **197 个文件**，包含契约生成物。公共清单经独立候选审阅，仅增加两项 Console 操作与六个 DTO；移除新增项后全部旧公共面深比较一致。继续 Schema20，无业务 SDK 方法、迁移或版本变化。

实现实际父范围 persona.publish、memory.read、console.manage 和近期认证，独立 Policy CAS、原子历史/指针/审计/水位与幂等回执。替换保留原 Persona 内容与 policy_id，旧 Policy 表单继续围栏 Proposal 创建/审批。整数配置通过十进制字符串传输，保留 SQLite 64 位范围的精度。页面提供十三项配置编辑、实际动作元数据、敏感确认、重新认证和冲突后最新策略差异，保留本地草稿并阻止旧版本再次提交。

隔离副本专项安全/恢复首轮 **29 passed / 1 failed（7.64s）**，失败为新增恢复断言将已有 current 状态误写为 active；修正测试后恢复 **1 passed（0.41s）**。扩大至完整 Persona/State/Proposal/Policy、Phase9、Phase10 pipeline 和 Console 契约回归，**550 passed（63.50s）**。包含缓存查询后撤销权限/密钥/会话、父范围拒绝、Policy 替换后旧 Proposal 审阅、晚期回滚、真实备份恢复及精确大整数。完整 mypy **344 文件**、全目录格式/Lint 和公共接口检查通过。

Console **29 tests**、生成类型、Lint、类型检查和生产构建通过；真实 Chrome **5 passed（10.9s）**。独立 Agent、Required 模式、最小权限临时密钥下，两页面竞争替换策略，实际重新认证后提交，旧页面展示新版本与本地草稿并禁止提交；刷新保持大整数、当前 Persona ID/Hash。包含旧 Persona/State/Proposal 真实流程和只读无策略写动作。测试临时凭据在退出时清理。

主目录完整组合尚待运行。Draft、保留管理和其他 Phase14 工作继续推进，本记录不代表完整 Persona、生产隔离、安全、Soak 或完整灾备通过。

### Persona Policy 当前完整组合验收通过

在整合并冻结的主目录执行 `UV_CACHE_DIR=.uv-cache CONSOLE_BROWSER_EXECUTABLE=… make ci`，自然退出 **0**。Core 0.13.0 / Schema20 / Python SDK 0.11.1 / TS SDK 0.11.2；macOS ARM64、Python3.12.13、SQLite3.50.4、Chrome。

- Python **11681 passed（882.83s）**，覆盖率 **85.22%**，原有性能门槛保持通过；TypeScript SDK **19 passed**。
- Console **29 passed**，生成类型、Lint、类型检查和生产构建通过；完整真实浏览器 **20 passed（1.2m）**。
- 格式、导入边界、文档、mypy、两套契约、兼容和公共接口清单通过。全新 wheel/sdist、边界、隔离安装、安装公共清单、Required 模式 SDK/Worker 和四个私有接口拒绝通过。
- **1319 个输入文件（包括文档）前后 SHA-256 完全一致**。安装服务退出 -15、关闭约 **0.209s**；仍为开发 SQLite Override，未据此声明生产隔离通过。

本节关闭 Policy 管理切片。后续并发编辑反馈补齐、独立 Draft 生命周期、保留管理及其余 Phase14 发布门禁仍需实施和验证。

## 2026-09-07 Persona 并发编辑反馈补齐（已整合，组合验收未通过）

复查发现已有服务端 CAS 正确拒绝覆盖，但 Persona 发布默认冲突读取误用 `/revisions`，State/Proposal 表单没有最新值读取器，因此这些路径不能完整展示设计要求的差异。基于已验收 Policy 候选，逐文件摘要保护整合 **8 个界面与真实浏览器测试文件**，不修改后端、契约、Schema、业务 SDK 或版本。

发布/回滚读取实际 Current 及其 Policy，State 读取实际独立版本，Proposal 读取实际创建或审批上下文；基准过期与状态变化同样展示最新上下文并保留本地草稿，旧提交禁用。关闭后刷新对应资源和父页面，避免重新打开表单继续使用旧元数据。未知或不可见资源仍由后端拒绝，未自动重设审阅版本。

隔离 Console 首轮类型检查发现生成 DTO 的只读字段为 unknown、通用 Resource revision 可空；改为适合只读差异展示的类型后，**29 tests**、生成类型、Lint、类型检查和生产构建通过。真实浏览器首轮 **4 passed / 1 failed（10.6s）**，失败为新增多页面测试误用 Playwright 的单页托管上下文；改用显式 BrowserContext 后 Proposal **1 passed（4.9s）**，最终浏览器文件类型检查通过。

已验证旧人格草稿在另一页面发布后保留、Persona 版本不变但 Policy 变化时展示正确的新策略、State 首次写入竞争、Proposal 创建与批准基准过期后的实际差异和禁止覆盖，并继续验证发布/回滚历史、到期 State 清理、Proposal 拒绝与只读动作边界。主目录完整组合待运行，暂不沿用此前 Policy 候选结果宣布本次修正最终验收。


并发反馈修正的首轮主目录完整 CI 自然退出 **2**：Python **11680 passed / 1 failed（962.73s）**，失败为 Hybrid Recall 第 32 个测量请求 `bench-31` 的所有路由超出 250ms deadline，抛出 deadline_exceeded，尚未生成该轮 p95 汇总；不能将其简写为已测得 p95 超标。SDK、Console 浏览器和安装后续门禁未执行。**1319 个输入文件前后摘要完全一致**，本轮修改只有前述八个 UI/浏览器文件，尚无证据确定延迟异常原因。

随后保持实现与性能测试阈值不变，单独运行同一 Hybrid 测试并保留 coverage instrumentation，**2 passed（5.88s）**。该诊断只关闭局部 coverage 总量阈值（全仓 CI 继续为 80%），不替代完整组合验收；40 个样本实测 p50 33.5ms、p95 39.3ms、最大 43.8ms，Embedding p50 0.09ms、FAISS p50 1.92ms。没有修改 Recall 热路径、250ms deadline 或 p95 门槛。原定完整 CI 重跑尚未启动；按项目负责人本次停止指令取消重跑，保留未验收状态。

## 2026-09-07 Recall 性能采样预热 ID 碰撞修复

候选：基于 `49d871cbe3f1d22245c77566fc6763f86090bb36` 的未提交工作区修改；Core 0.13.0 / Schema 20。范围为 `tests/performance/test_recall_latency.py` 与本报告，无生产热路径、契约、迁移或预算变更。环境：Mac16,12 / Apple M4（10 核、16 GiB RAM）、macOS 26.6.2 ARM64、Python 3.12.13、SQLite 3.50.4，本机开发 SQLite Allowlist；测量前系统 1/5/15 分钟 load average 为 2.47/2.27/2.33，未启动其他验证任务并行竞争，但未控制整机后台负载。

**问题确认**：原 `_measure()` 每次从 0 编号，两项门禁各自的三次预热与正式样本共用 `perf-req-<topic>-0/1/2`。`RecallService.recall()` 查询到同 ID 的存档后经权限与有效性检查返回重放，跳过 orchestrator 和新请求持久化。因此原 40 个样本混入三次较快重放，污染分布及 p50/p95 统计；不能把此前通过记录视为 40 次真实 Recall 的证据。现预热使用 `perf-warmup-<topic>-0..2`，正式测量保持 `perf-req-<topic>-0..39`，两项测试均已修复。

测量口径：每项测试独立临时 SQLite 数据库，FTS 已构建，先三次预热再测 40 次顺序请求；内部路由并发上限 4，无外部 Provider 调用。实际数据为 60 条 identity/fact Claim（文本 40–120 字符范围内）、20 条对话 Observation 加 60 条 Evidence Observation，FTS 为 60 条 Claim 文档；没有 State/Focus/Relation 记录。每结构化路由默认 Candidate 上限 20，Token budget 100,000；请求 deadline 沿用 60 秒。计时仅包围 `RecallService.recall()`，包含授权、路由、新鲜 Rehydrate 与 Usage 持久化，不含请求构造、建库、索引构建及预热；p50/p95 沿用排序后零基下标 `round(fraction * (n - 1))`（40 项的 p95 为下标 37），不插值。文件 docstring 已纠正预热、实际数据集及统计方法，deadline 注释由错误的 60 ms 更正为 60 s。

修复后的独立无覆盖率测量命令：

```bash
.venv/bin/python -m pytest tests/performance/test_recall_latency.py --no-cov -q -s
```

| 项目 | 真实正式样本 | p50 | p95 | max | 原有门槛/结果 |
| --- | --- | --- | --- | --- | --- |
| Structured Recall | 40 | 22.92 ms | **24.31 ms** | 29.23 ms | ≤ 50.0 ms，通过 |
| FTS Recall | 40 | 25.17 ms | **26.16 ms** | 27.36 ms | ≤ 100.0 ms，通过 |

结果 **2 passed（3.61s）**。这些是修复后本次本机实测值；不沿用问题描述中的约 24 ms 数字作为本次证据。

同类审计（其余六个文件无需修改）：

| 文件 | 排除同类碰撞的依据 |
| --- | --- |
| `test_hybrid_recall_latency.py` | 预热 `bench-warm-{i}`，正式 `bench-{i}`；阶段分解用独立 Fixture 和 `bench-stages` |
| `test_graph_profile_recall_latency.py` | 一个 `range(WARMUP + SAMPLES)` 连续生成 `perf-{round_index}`，只在统计时切掉前三项 |
| `test_forget_latency.py` | 预热 `perf-warm`，正式 `perf-{sample}`；时钟推进且每轮新建目标；单资源测试无预热，使用 `perf-single-{sample}` |
| `test_observe_latency.py` | 预热 `warm-`，正式单条 `s-` / 批量 `b-`，cursor 持续递增 |
| `test_persona_latency.py` | `current()` 为读调用，没有 request ID / 幂等重放键 |
| `test_state_latency.py` | 同一 `itertools.count()` 覆盖预热、正式及竞争阶段，`w-{ticket}` 不重复；种子另用 `corpus-` / `hot-seed` |

**独立待决项：负载敏感性（14.4 / P14-PERF-01 承接）**。问题提交者提供的此前本机证据：同一结构化门禁在完整 make ci-style 运行中带 coverage 的 p95 为 **59.19 ms**，紧接着无 coverage 为 **53.47 ms**，均超过 50 ms；空闲机器上约 **24 ms**，余量明显。这两次失败保留为提交者提供的历史观察，未提供可核对的完整命令/日志/候选摘要，本次不冒充重新复现，也不归因为 coverage 单一因素。预热 ID 修复解决样本真实性，未解决整机负载导致的门禁不稳定；需另行决定固定测量主机及负载条件，或引入不依赖墙钟负载的工作量指标，并明确其与 §30 延迟基线的关系。现有 50/100 ms 门槛保持不变，不以单机低负载通过关闭该项，也不据此宣布 14.4、Soak 或稳定发布验收通过。

另做一次真实服务诊断，将重放入口临时替换为失败断言，两项原性能测试仍 **2 passed（3.59s），replay_calls=0**；该诊断不作为上表的无插桩延迟数据，也没有修改生产服务。可复现命令：

```bash
.venv/bin/python - <<'PY'
import pytest
from unittest.mock import patch
from iris_memory_core.application.recall import RecallService

with patch.object(RecallService, "_replay", side_effect=AssertionError("performance sample replayed")) as replay:
    result = pytest.main(["tests/performance/test_recall_latency.py", "--no-cov", "-q"])
    print(f"replay_calls={replay.call_count}")
    assert replay.call_count == 0
    raise SystemExit(result)
PY
```

收尾命令：`UV_CACHE_DIR=.uv-cache CONSOLE_BROWSER_EXECUTABLE='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome' make ci`。首轮沙箱运行通过格式、Lint、导入边界、文档、mypy、TS 类型、契约/兼容与公共接口检查；进入 Python 回归后，Mock HTTP server 在 `socket.bind()` 遇到 `PermissionError: [Errno 1] Operation not permitted`。用 `.venv/bin/python -m pytest tests/contract/test_mock_server.py --no-cov -q -x` 确认同一环境错误（1 error，0.07s）后主动中断无效全量运行（中断时 530 passed / 54 errors，65.07s），未将中断记录为完整测试结果。原始本地诊断日志保留于 `/tmp/recall-warmup-ci-20260907.log`；随后申请允许本地监听的执行环境，复验结果在下文补记。


同一修复候选在允许本地监听的执行环境中运行上述完整 `make ci`，自然退出 **0**（原始本地日志 `/tmp/recall-warmup-ci-20260907-local.log`）：

- Python **11,681 passed、2 warnings（894.29s）**，覆盖率 **85.23%**，保持原有 80% 覆盖率门槛；七个被审计性能文件均通过，包括修复后的 Structured/FTS 两项带覆盖率门禁。
- TS SDK **19 passed**；Console **29 passed**，类型生成、Lint、类型检查和生产构建通过；真实 Chrome 浏览器 **20 passed（1.3m）**。
- 格式、导入边界、文档、mypy、契约生成/兼容、公共接口快照均通过；全新 sdist/wheel、归档边界、隔离安装、Required 模式 SDK/Worker 与四个私有接口拒绝检查通过。安装服务退出 -15，关闭约 **0.256s**，仍显式使用开发 SQLite Override。

本轮完整组合通过不否定此前负载条件下的失败，也不证明门禁已稳定；14.4 的固定测量环境/指标决策仍待完成。没有修改门槛、提交代码、创建 RC 或发布产物。

## W04 类型化 Operation 与可信备份（2026-09-08）

[W04 独立报告](w04-operations-trusted-backup.md)记录 ADR-0048、Core 0.14.0 / Schema21 增量迁移、旧 Forget 历史保持及内部可信备份真实流程。最终 ci-003 全部通过：14795 项功能 / 85.58% 覆盖率、11 项性能、19 项 TS SDK、29 项 Console 单元、21 项真实浏览器和独立安装消费。报告附逐项证据、全部失败/复验日志与最终产物摘要。本切片只完成 13.11 最小前置；Phase 14 整体保持 In progress，Phase 11/12 Deferred。
