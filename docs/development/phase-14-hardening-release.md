# 阶段 14：前置闭环、生产硬化与稳定发布

> 状态：In progress（2026-09-08 最新授权按依赖并行推进至 W20，W01–W04 已完成，W05 本地收尾、W06/W08 集成验收、W07/W09/W10 开发；此前整体 goal 已停止；2026-09-07 已实施读面、State/Note/Focus/Observation/Claim/Episode/Relation 与 Task 管理写入、Artifact 文本创建与附件上传、Recall/Focus Lease 与安装门禁；Identity 注册/绑定、实体重定向与属性已验收；六类内容删除、Entity 软删除与恢复账本及 Focus/State/Task 固定集合删除已验收；固定筛选与 Operation 批量删除、Event dismiss 及待投递 Recall 重验已通过 Schema 20 当前完整组合验收，Persona 发布回滚已按 ADR-0044 通过当前完整组合验收，PersonaState 管理按 ADR-0045 已通过完整组合验收，Proposal 按 ADR-0046 已通过完整组合验收，Policy 按 ADR-0047 已通过完整组合验收；尚未进入发布验收）  
> 前置阶段：[Phase 0–10](./README.md) 的 Core 基线与 [Phase 13](./phase-13-web-console.md) 的管理平面；Phase 11/12 均已暂缓，移出当前发布依赖  
> pip 范围：仅 Core 功能及其必要运行资源；不包含 Bellis/AstrBot 适配器；业务调用只经冻结的公共方法与契约  
> 目标版本：1.0.0（发布目标，非当前版本；RC 编号在 14.0 冻结）  
> 架构依据：[§29 安全](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#29-安全与隐私)、[§30 性能](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#30-性能与容量目标)、[§35 运维](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#35-部署与运维)、[§38 验收](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#38-顶层验收标准)

2026-09-07 执行方式调整：停止整体 Phase 14 goal，保留未完成状态。后续按[构建指导](./next-build-guide.md)与[单一工作包队列](./work-packages.md)逐个下达；新发现的 Recall 接线与原量化验收欠项一并承接。本页继续定义发布范围与硬门槛。过程中运行受影响子集，每实施工作包收尾一次全量 CI。14.4/14.5 独立排期为有明确输入、时限和产物的后台批次，当前未启动。

当前执行覆盖：用户已授权按 W01→W20 严格串行自动续作，完成一包的全部验收与单独提交后进入下一包。W01 开发与后台定向验证中，尚未进入发布验收；详见[W01 报告](../reports/w01-http-recall-assembly.md)。原逐包另行下达的描述由最新授权覆盖。

## 阶段目标

将 Core 功能交付为可重复安装、升级、运行和恢复的 pip 包，业务调用方只使用既定公共方法；Core 自己管理 SQLite、FAISS、内部队列与私有组件。先关闭 Core 和管理平面的实际缺口，再用同一候选发布物完成接口封装、访问隔离、安全、容量、故障和恢复验证。历史阶段通过记录只证明当时切片，不能代替当前组合的发布证据。

2026-09-06 范围决定：按项目负责人要求，Phase 11/12 均暂时不再执行，14.0-A/B 标为 Deferred。Bellis/AstrBot 适配代码、宿主依赖、专属 SDK 扩展、插件分发、真实宿主 E2E 和宿主恢复证明全部移出当前 pip 产物及发布门禁；不声明这两个适配器受支持。通用身份隔离、Lease/Fencing 和多客户端一致性继续在真实 Core 进程上验证。Phase 13 的 Core 管理功能继续执行，配套前端单独交付；恢复任何适配器阶段时再更新范围与基线 §38 追踪。

### pip 内容与交付边界

本表约束 `iris-memory-core` 的 wheel、sdist、extras、依赖闭包与入口点。只包含 Core 功能不等于移除 Core 运行所需的存储、索引或队列实现；这些组件留在服务内部，不能变为调用方可操作的 API。

| 范围 | 当前发布决定 |
| --- | --- |
| Core 功能 | 包含身份/Scope、Observation、Recent/State/Focus、Note/Task/Event、Memory/Episode/Artifact、Recall/Usage、Profile/Graph、Persona、巩固/Reflection、Lease、持久任务及已定义的 Core 管理能力 |
| Core 运行物 | 包含服务与 Worker、版本化运维 CLI、必要 Migration/运行契约、内部 SQLite/FAISS/队列实现及实际运行依赖；不携带运行数据库、索引快照、队列状态、凭据或测试密钥 |
| Phase 11/12 | Bellis Provider、AstrBot Bridge、平台 Hook、宿主类型/SDK/配置模板不进入 Core wheel/sdist、extras、依赖闭包或 entry points；不构建、不发布、不执行其宿主验收 |
| 公共 SDK | Python/TS SDK 是独立的通用协议客户端，保留兼容检查，源码不并入 Core 包。Python SDK 单独的 pip 产物和 TS npm 产物不因 Core 发包而自动发布，也不恢复宿主专属扩展任务 |
| Phase 13 | Core 的 `/console/v1` 后端、认证及必要契约属于可选管理功能，默认关闭。Web 前端按独立静态产物交付，不把 Node 工程、node_modules 或开发服务器打入 Core pip 包；SDK 不自动获得管理权限 |
| 工程与部署 | Docker/Compose、前端和客户端可用于配套部署及验收，分别记录产物。sdist 只包含重建 Core 所需的源码/资源/构建元数据及 Core 文档/验证材料；排除仓库根 `hosts/` 宿主目录、独立 SDK/前端工程和开发缓存。`src/iris_memory_core/application/` 属于 Core 内部应用层，不能误删 |

### 公共方法与内部访问边界

当前已设定的业务入口为版本化 SDK 方法与 HTTP 契约：Python 通过 `iris_memory_sdk.AsyncIrisMemoryClient` 的已核准方法消费 Core `/v1`，Core pip 提供服务与受控运维 CLI。Core 根模块当前仅导出版本信息，尚无冻结的进程内业务 façade；不能把现有 `application` 服务类或 `runtime` 对象自动升级为公共接口。本轮不新增可取得内部对象的嵌入式调用模式。

| 接口面 | 允许公开 | 必须保持私有或拒绝 |
| --- | --- | --- |
| Python 调用 | 14.0-F 白名单中的客户端入口、显式业务方法、请求/响应 DTO、版本和受控异常；如 `observe_batch`、`recall`、`report_recall_usage`、`remember_claim`、`forget_memory`、`current_persona` | 不导出或返回 Store、Repository、UnitOfWork、SQLite connection/cursor、FAISS handle、Worker/Queue、ServiceContainer 或 Provider 实例；不新增 `get_store`、`get_runtime`、通用对象查找等逃逸入口 |
| 业务 HTTP | 公共契约明确列出的 operationId/path/method，认证后按 Scope/Privacy、Expected Revision、幂等和 Lease 规则执行 | 不提供任意 SQL、表名/列名直查、索引文件路径/读写、队列 dump/pop/push、任意内部函数/模块调用、对象反序列化或脚本执行接口 |
| 管理与运维 | 既定契约内的 Job 状态/重试、投影重建、Backup/Export、凭据管理及受控 CLI；要求相应管理权限/操作原因/审计，离线恢复限定可信操作者 | 不把管理 Token 或 CLI 权限授予普通客户端；Job/Index 操作只接受核准的类型及不透明 ID，不能借管理接口取得原始队列/数据库/索引句柄 |
| 返回与配置 | 契约规定的值对象、资源 ID、分页结果和受权的领域数据导出；服务操作者按 Schema 配置数据目录/Provider/预算 | 不把可变内部对象、数据库路径、FAISS 路径、内部队列 payload 或私有异常/堆栈放入业务结果；业务请求不能传入自造的授权上下文、裸连接、任意类路径或回调来接管 Core |

白名单必须逐项记录导入路径/方法签名、DTO 与错误、对应 operationId 或 CLI 命令、权限/Scope、幂等/Revision/Lease 语义、稳定级别及弃用窗口；示例中的方法只说明既有能力，不代替完整清单。现有 SDK 方法也须与当前服务契约核对，不能仅因类上存在一个方法就认定支持。公开数据类型不得在类型标注、继承、返回对象或属性中间接暴露上述内部组件。

“不能直接访问”包含两层验收：公共封装不提供通路，部署隔离阻止调用方读取运行资源。Python 的 `__all__`、下划线命名或包目录调整只约束受支持 API，不是针对同进程任意 Python 代码的安全沙箱。受支持的隔离部署必须让业务调用方只持有端点和受限凭据；Core API/Worker 以独立 OS 身份运行，SQLite/WAL/SHM、FAISS、内部状态、密钥和备份目录仅向 Core/可信运维授权，客户端不可挂载或读取。拥有 Core OS 身份或管理员权限的本地代码不在该隔离承诺内；不能宣称 pip 安装本身能阻止它读取代码或文件。

### 重规划依据

| 当前事实（2026-09-06） | 对本阶段的影响 |
| --- | --- |
| Phase 0–10 已有领域、真实 HTTP、Worker、迁移、备份/恢复、性能和事务级故障测试 | 复用已有能力与测试，补生产装配和进程级证据 |
| 工作区 Core 0.12.0、Schema 14；`/v1` Contract 1.9.0；Python SDK 0.11.0、TS SDK 0.11.1 | 版本分别演进；冻结实际兼容矩阵，不能按阶段号推算包版本 |
| Bellis 插件和定向测试存在，宿主兼容/effect/对账/E2E 未齐；现已 Deferred | 14.0-A 暂缓并移出当前门禁，保留原阶段证据，不声明 Bellis 已交付 |
| Phase 12 仅有接入文档，无 Bridge 实现和 ADR-0021；现已 Deferred | 14.0-B 暂缓并移出当前门禁，不声明 AstrBot 已交付 |
| Console 认证切片有历史 CI；读面本轮 53 passed / 1 failed，前端 `types:check` 失败；其余业务前端主要有模拟验证 | 14.0-C 修契约/类型漂移及页面适配，再完成未发布切片与真实联调 |
| `make ci` 尚不包含 Console 前端；没有生产镜像、Compose、发布流水线和 24h Soak 证据 | 14.1–14.6 建立完整发布门禁 |
| 原 Phase 13 自动迁移已由 ADR-0022 替换为手动文件导入导出 | 删除旧库扫描、增量追平、双写切换和迁移工具发布物；保留 Core 数据库 Migration 和删除安全要求 |

版本真源见 [version manifest](../../schemas/version-manifest.json) 与各包元数据；接入和控制台现状见对应阶段与验证报告。以上是本次重规划快照，不另建手工版本清单。

## 架构约束

- 单机 SQLite 写主、本地持久卷；API/Worker 可以独立进程，Lease/Fencing 和幂等仍由 Core 保障。
- `/v1` 宿主凭据与 `/console/v1` 运营密钥/浏览器会话隔离；控制台默认关闭。生产不能用 `--allow-local-sqlite` 或 `--console-dev-http` 绕过运行要求。
- 运行包必须带齐所需资源；从源码目录运行成功不等于 wheel、SDK 包或镜像可安装。
- pip 内容与公共方法严格遵循上述范围及白名单；普通调用方不能直接访问 Core SQLite、FAISS、内部队列或私有组件。内置 Provider/存储实现属于 Core，任何可选配置也不能引入 Bellis/AstrBot 依赖或绕过公共访问边界。
- Scope/Privacy、Revision、Tombstone、Canonical/Projection 与审计不变量贯穿导入、导出、恢复、后台任务和宿主副作用。
- 恢复是离线可信操作；Console 不提供恢复按钮。Backup 与授权 Export 的格式、权限、保留和密钥各自管理。
- FAISS 文件目前不随备份交付，恢复后必须重建。Backup HMAC 提供完整性/真实性验证，不等于静态加密。

## 需求追踪

| 需求 ID | 工作包 | 交付与门禁 |
| --- | --- | --- |
| P14-BASELINE-01 | 14.0 | 前置阶段退出证据、资源 ID 裁决、零类型漂移、目标版本兼容矩阵 |
| P14-PACKAGE-01 | 14.1 | Core wheel/sdist 及依赖闭包不含适配器；脱离源码安装可运行，配套 SDK/前端独立记账 |
| P14-PUBLIC-API-01 | 14.0-F、14.1 | 公共方法/导出/DTO/HTTP/CLI 白名单、契约映射与兼容检查；内部对象与逃逸入口暴露为零 |
| P14-ISOLATION-01 | 14.2、14.3 | Core/客户端进程身份和文件权限隔离；普通客户端经私有导入、路径、网络或管理接口直达存储/索引/队列成功数为零 |
| P14-DEPLOY-01 | 14.2 | 非 Root/只读根镜像、实际 SQLite Allowlist、Compose 启停与生产 Provider 接线 |
| P14-SECURITY-01 | 14.3 | 跨平面越权、凭据生命周期、泄漏与供应链扫描、加密/密钥策略演练 |
| P14-PERF-01 | 14.4 | 同条件性能基线、容量上限、24h+ 混合 Soak、进程故障证据 |
| P14-RECOVERY-01 | 14.5 | 隔离恢复、删除账本/凭据处理、投影重建、3 轮升级回退与 RPO/RTO |
| P14-RELEASE-01 | 14.6 | 不可变 Release Manifest、兼容矩阵、运行手册及 §38 双向验收追踪 |

## 工作包

### 14.0 前置阶段闭环与候选范围冻结

本工作包只组织 Core 与 Phase 13 的欠缺项、pip 内容和公共接口冻结；实施和验收仍回写原阶段。Phase 11/12 的宿主待办保留并暂缓，不能借通用 SDK 或“端到端验证”重新引入。

- **14.0-A Bellis（Deferred）**：暂时不执行，不阻塞 Core 发布；宿主接线、当前 Schema 兼容、effect/ACK、Cursor 补投、Persona 恢复、插件与 npm 分发、宿主 workspace/CI 和 E2E 留在 Phase 11。已有代码、ADR 与历史报告保留，恢复后重新验收。
- **14.0-B AstrBot（Deferred）**：暂时不执行，不阻塞 Core 发布；ADR-0021、AstrBot 所需 SDK 扩展、Bridge 与生命周期测试保留在 Phase 12，恢复后重新确认兼容与安装门禁。通用客户端的兼容检查与适配器分发分别记账。
- **14.0-C Console**：先裁决 Canonical 非 UUID ID 与 `ResourcePage.id` 契约冲突，补兼容 Fixture 并修复读面失败；同步 OpenAPI 生成前端类型，并将读页面接到实际的 `list_columns`、结构化 `sorts`、`create_schema/update_schema` 注册表。再按 Phase 13 完成记忆命令/Forget、统计、手动导出导入、Embedding Provider、运行参数和 Operation/运维面。每个模块须从契约、后端、前端连到真实浏览器验证，模拟测试不能充作真实联调。冻结导入格式、来源映射、幂等/报告哈希与恢复边界。
- **14.0-D Core 遗留**：核对历史报告中已解除与仍有效的限制。生产认知 Provider 的能力声明、SSE 多进程重连与慢消费者、真实 `serve` 排空/SIGKILL、指标采集、Dead Letter 运维、旧 rebuild 端点弃用窗口分别归入下列工作包；保持未知必需能力失败关闭。
- **14.0-E Required Surface**：补齐在线 Recall/Focus 的 Lease 门禁覆盖并核对其余在线操作；现有 SurfaceService 不等于所有应用入口已接线。先对齐契约携带的 Lease Proof、传输入口与服务端检查，再验证缺失/过期/旧 Epoch/非 Holder 请求失败关闭，不能只依赖 Adapter 自律。
- **14.0-F 公共接口冻结**：基于 `contracts/source/contracts.json`、独立 Console 契约、CLI 与现有通用 SDK，产出机器可检查的公共接口清单和用户方法文档。逐项审核现有方法、公开 DTO/异常及权限；SDK 独有但服务未实现的方法不得列为支持。Core 顶层不导出内部服务；若提供顶层便捷方法，只能映射到已核准的公共操作并保持同等校验，不得获取内部对象。正式确定导出模块、方法签名、支持范围、弃用窗口与 CI 快照，14.1 不得按模块遍历自动公开所有方法。
- 冻结候选版本、支持平台/硬件/数据规模、责任人、最小和最大 Core/Contract/通用客户端兼容版本、验收清单；在测量前固定 Console 列表/统计 p95、导入吞吐/事务预算、最大导出与 Blob 回退内存预算及 RPO/RTO。稳定范围仅为 Core（含 Phase 13 管理功能），配套前端单独交付；Phase 11/12 均不计入必需版本矩阵。

**通过条件**：Core/Phase 13 退出证据、pip 内容及公共方法白名单齐全，当前契约与类型漂移为零，通用客户端兼容矩阵可执行；Phase 11/12 均为 Deferred，不计作已完成能力或发布前置条件。

### 14.1 可安装产物与统一 CI

- 检查 wheel/sdist 的 Migration、契约运行资源与路径解析；目前 `default_migrations_path()` 依赖仓库目录布局，必须在没有源码 checkout 的空环境证明 migrate/serve/worker 可运行。
- 用显式包含/排除规则构建 Core wheel/sdist，并从 sdist 再构建 wheel 检查内容一致；扫描安装文件、元数据依赖、extras、入口点和自动发现机制，禁止带入两个适配器或宿主运行时。保留内部 Core 实现及必要运行资源，不能为了隐藏组件删掉服务所需代码。
- 配套通用 SDK 和 Console 静态产物独立构建/安装用于消费验证，不并入 Core wheel/sdist，也不要求本轮公开发布 SDK/npm；记录各自产物与锁文件摘要。Core 客户端黑盒测试只经核准公共方法调用真实服务，不能 import `storage`、`indexing`、`jobs`、内部 `application` 或读取 Core 数据目录来完成业务操作。
- 对已安装产物检查公共导出、方法签名、DTO/异常及 CLI/HTTP 清单与 14.0-F 一致；未知导出或入口令 CI 失败。SDK 不依赖 Core 私有模块，文档示例不访问 `_request_json` 等私有传输方法；检测返回对象/属性/类型标注中是否泄漏连接、索引、队列或容器。
- 在现有 `make ci` 基础上接入 Console `types:check`、lint、typecheck、单元测试、生产 build 和真实后端 browser 测试；统一覆盖 Core 两套契约、通用客户端消费、公共接口边界与安装 Smoke，不执行 Phase 11/12 的插件分发或宿主 E2E。已有白盒领域/存储测试保留，不能用它们代替公共接口黑盒验收。
- 文档检查覆盖根目录、`docs/`、SDK、`hosts/` 和 `web/console/` 的手写 Markdown 路径/锚点；生成清单只校验真源与生成物，不复制到说明文档。

**通过条件**：Core 产物及运行资源齐备；干净环境安装及统一 CI 全过，宿主依赖/适配器代码/测试密钥/生产模拟模式为零，公开导出与白名单完全一致，公共方法不返回或接收私有组件。

#### pip 包发布路径

发布可用的 Core pip 预览包可先于完整 1.0 稳定验收；两者都严格限定为上述 Core 内容与公共接口，不含 Phase 11/12。Phase 13 管理功能继续执行，前端与通用客户端独立交付。预览版须明确版本、支持范围及未完成能力，不能提前标记 Phase 14 Completed，也不能放宽内部访问隔离。以下打包探针是本次范围修订前的历史实测，SDK 探针不代表把 SDK 纳入 Core 发行包。

2026-09-06 对 Commit `692de12` 的源码进行了以下定向核查（本轮仅修改文档）：

- `UV_CACHE_DIR=.uv-cache uv build --offline --out-dir /tmp/iris-pip-audit-core-20260906` 及 `uv build sdk/python --offline --out-dir /tmp/iris-pip-audit-sdk-20260906` 均生成 sdist 和 wheel，Core 为 0.12.0，Python SDK 为 0.11.0。Core sdist 有 14 个 SQL 与两套 OpenAPI，但由该 sdist 构建的 wheel 不含 SQL、OpenAPI、`contracts/source` 或 Console 静态产物。Core wheel 有 LICENSE；SDK 的 wheel/sdist 均无 LICENSE，两者均无 `py.typed`。
- 在 `/tmp` 新 venv 以 `uv pip install --offline --no-deps` 安装两个 wheel，使用 `python -I`，仅追加现有环境的第三方依赖目录且不加载其 editable `.pth`。确认 Core 从新 venv 的 `site-packages` 导入：迁移发现数量为 0，`migrate` 返回成功却输出 `schema_version=0 applied=0`；宿主 `_load_contract()` 与 Console `load_contract()` 均抛 `FileNotFoundError`。这是安装物资源缺失的定向复现，未启动网络服务。
- 完整离线依赖安装因缓存缺少 `rpds-py` 等产物失败，属于本次环境限制，不作为项目依赖不可安装的证据。仍须补真正干净环境下的联网依赖解析与支持平台矩阵；未执行 `twine check`、TestPyPI 或公开发布，未核实 PyPI 名称归属。
- `.venv/bin/python -m pytest tests/integration/test_console_reads.py::test_reflection_and_candidate_views_reauthorize_the_input_closure -q --no-cov` 仍为 1 failed（Reflection 非 UUID）；`npm run types:check --prefix web/console` 仍报 `Console types drift`。本轮未重跑全量 CI，最近完整结果见 [Phase 13 报告](../reports/phase-13-verification.md#工作区提交复验2026-09-06)。临时构建物位于 `/tmp`，不是正式发布产物。

| 顺序 | 剩余任务 | 完成证据与归属 |
| --- | --- | --- |
| 1 | 冻结 Core 内容和公共接口，修复资源打包 | 14.0-F 确定方法/导出白名单；14.1 排除适配器，包含当前 15 个 Migration、两套运行契约及 capability 资源并用包资源 API 读取，缺失报错；安装后经受控 CLI 和公共方法完成 Migration/serve/worker/Recall Smoke |
| 2 | 修复现有失败并统一门禁 | 14.0-C 解决 ID 契约、生成类型与真实页面 descriptor；全量 `make ci`、Console 检查与适用的真实浏览器用例通过，CI 增加构建/安装 Smoke。预览版若不支持 Console，须显式禁用并记录范围，不能把失败用例当已通过 |
| 3 | 闭合运行与隔离 | 经既定运维入口初始化租户/Agent/凭据；验证 Python/SQLite/FAISS/NumPy、真实 Provider 或明确关闭未支持能力。Core 与调用方分离 OS 身份及数据权限；Console 静态产物独立交付；见 14.2、14.3、14.5 |
| 4 | 完成 Core 元数据与通用客户端兼容 | Core 许可证、公开方法/类型说明、项目链接、安装说明、变更日志和兼容矩阵齐全；消费独立 SDK 安装物验证。SDK 自身许可证/类型标记缺口在其独立产物中处理，不把 SDK 源码或适配器扩展并入 Core |
| 5 | 建立 Core PyPI 发布链 | 确认 `iris-memory-core` 名称、账号和发布权限，配置发布身份；校验同一 wheel/sdist（含 `twine check`）、白名单、依赖与摘要，先 TestPyPI 安装回读，再发布并回读验证。不连带发布两个适配器或 SDK/npm |
| 6 | 完成 Core 1.0 稳定门禁 | Phase 13 剩余写入/Forget、统计、导入导出、Provider、Settings 与运维面；14.2–14.6 的生产隔离、安全、24h Soak、真实 Core 进程故障、3 轮恢复升级回退及 Release Manifest。客户端使用通用契约测试工具，两个宿主的专属验收均暂缓 |

构建、上传与安装回读流程参考 [PyPA 打包指南](https://packaging.python.org/en/latest/tutorials/packaging-projects/)；CI 发布身份参考 [PyPI Trusted Publishers](https://docs.pypi.org/trusted-publishers/)。这些是分发流程，项目自身的 1.0 质量门禁仍按下列工作包执行。

### 14.2 容器、生产装配与可观测性

- API/Worker 同镜像不同命令，构建并锁定实际满足 Allowlist 的 Python/SQLite 组合；提供镜像 Digest、非 Root、只读根、`/data` 卷、受限临时目录、Healthcheck、Stop Grace 和资源限制。
- 提供 Compose、配置与 File Secret 示例；Console 静态资源和数据/备份/导出目录隔离，明确 Origin、Host、可信反代和 TLS 终止配置。
- Core API/Worker 使用专用服务身份；普通 SDK/HTTP 客户端没有该身份、服务密钥或私有卷挂载。仅向 Core/可信运维授权数据库及 WAL/SHM、FAISS、内部状态、认证/Provider 密钥和备份目录；本机客户端即使知道路径也不得读取。`--database`、迁移/恢复输入和私有目录配置只属于受控运维入口，不能从业务方法接收或返回。
- 接通 Phase 13 的 Embedding Provider 配置与 `runtime` 的在线查询/后台索引路径；认知 Provider 目前随包为确定性实现，须验证可用于声明用途的真实 Provider 或明确禁用相应生产能力，不能以 Fake 测试代替质量验收。
- Ready/Liveness 验证 SQLite、Schema、Persona Pointer、磁盘、必要 Provider、Worker/Scheduler、投影降级及 Console 待重启配置。对每租户 JSON `/metrics` 选择实际采集方案，补 Queue/Schedule Lag、WAL/Checkpoint、RSS/句柄和告警演练。
- 真实网络进程验证停止顺序、SSE 重连与慢消费者、API/Worker 共享配置与多进程一致性；退出时先撤 Ready，再排空请求/Job，最后释放资源。

**通过条件**：生产配置下无需开发绕过参数即可启停；健康、指标、告警、Provider 与 Core/客户端身份和文件隔离均有实测证据。

### 14.3 安全、凭据与供应链

- 回归跨 Tenant/Agent/Scope/Privacy、管理/应用平面隔离、CSRF/Origin/reauth、权限变化后 Cursor/下载失效、Artifact 路径、CSV 公式转义、导入拒绝清单和 Provider SSRF。
- 增加公共接口负例：未知方法/路径/operation、原始 SQL/表名、私有对象/模块名、索引文件路径、内部队列操作、伪造授权上下文和越权管理请求均被拒绝；错误、DTO、诊断、配置及业务导出不泄漏私有句柄/路径/队列记录。按公共领域 ID 查询 Job 状态、请求受权重试或重建仍须执行契约与审计，不提供任意队列 payload 读写。
- 在独立客户端 OS 身份下验证 SQLite/WAL/SHM、FAISS、密钥、内部状态及备份目录读写失败；业务 Token 调用运维/管理入口失败。私有模块即使能被导入，也不能借其代码获得服务身份或打开 Core 数据。白名单检查、同进程对象封装检查和 OS 拒绝证据分别记录，不以 `ImportError` 或文档声明替代真实访问隔离。
- 演练运营密钥、宿主凭据、Session、刷新重试材料、Provider Secret 与备份验签密钥的签发/轮换/撤销。明确恢复旧快照后会话失效、已撤销凭据不复活的实现与密钥托管/恢复策略。独立的 `console-auth.key` 与 Provider sealed 主密钥、Backup HMAC 密钥分别管理；当前含会话的数据库在认证密钥文件缺失时会拒绝启动，须覆盖密钥缺失/错误和可信恢复的负例。
- 选择并实现备份静态加密和密钥分离；同时保留 Manifest/Checksum/HMAC 校验。扫描快照与诊断包的凭据材料，不能凭 `secrets_included=false` 元数据宣称整库已脱敏。
- 为 Core Python 产物及实际配套前端/镜像分别生成 SBOM，执行依赖/镜像/许可证/Secret 扫描；额外扫描 Core 依赖、extras 和入口点，Bellis/AstrBot 依赖与插件注册为零。签名、来源证明和例外记录按产物归档，不为暂缓的适配器生成发布承诺。

**通过条件**：私有组件/句柄暴露及普通客户端直接读写 Core 运行资源成功数为零；Secret/敏感正文越界泄漏为零；无未处置 Critical/High 漏洞，例外有负责人、期限和缓解证据；密钥与恢复安全演练通过。

### 14.4 性能、容量、Soak 与进程故障

- 复用 `tests/performance/` 与事务边界 Kill -9 测试，在同一目标镜像/硬件/数据集，通过已安装的公共客户端/HTTP 方法补真实 Core、Console 及多客户端竞争结果；记录冷/热索引、文本长度、候选数、并发、Provider 延迟和采样方法。Bellis/AstrBot 专属宿主 E2E 随 Phase 11/12 暂缓；通用客户端结果不宣称证明平台实际发送效果。
- 对 enqueue 的 O(pending) 聚合、高频 State 写、Recent/FTS/Graph/Profile 重建、Vector 切换窗口、5000 目标 Forget 上限做容量实验，形成拒绝/分批/维护窗口和增长阈值。覆盖保守水位降级、图扇出窗口漏召回与磁盘滞回的运行限制；无实测需要不新增缓存或改写投影架构。
- 混合 Observe/Recall/State/Task/Worker/Console 与通用客户端负载运行至少 24h，注入 Provider 慢/断、Worker 与 API Crash、FAISS 损坏、磁盘阈值、时钟异常及并发备份；补 PersonaState TTL 在时钟回拨、Worker 停机及客户端重连后的恢复证据。破坏内部文件等故障注入只由隔离环境的可信测试操作者执行，业务客户端仍使用公共方法。
- 真实 `serve`/`worker` 进程与容器关闭边界各重复至少 20 次 SIGKILL；验证 Core 已确认 Canonical 事务不丢、任务可恢复、客户端按公共 Cursor/幂等接口重试与对账。不能只以事务测试替代端口/排空验证，也不以 Core ACK 代替宿主发送效果证明。

**通过条件**：下述延迟目标与数据不变量通过；Soak 无无界资源增长/永久积压，异常有可观测结果及恢复证据。

### 14.5 备份、恢复、升级与运行手册

- 复用已有 Online Backup、Manifest/HMAC、Artifact 校验、隔离 Restore 与 `recover-switch`；补生产规模、加密封装、Console 新表/凭据、独立 `console-auth.key` 和最新删除账本的恢复覆盖；数据库备份本身不能替代认证密钥文件的受保护恢复。
- 恢复后检查 Integrity、Foreign Key、Tombstone、Persona Pointer、Outbox、Tick 与 Artifact；重建 FTS/Vector/Profile/Graph，明确重建期间降级与 Ready。禁止从旧快照恢复已删除数据或已撤销访问权限。
- 分别声明 online-safe 与需停机 Migration；兼容 Schema 优先回滚二进制，否则隔离恢复已验证快照并重放授权增量和删除账本。禁止破坏性 Down Migration。
- 在隔离环境演练 Install → Seed → Upgrade → Backup → Restore → Rollback，共 3 轮；记录恢复点、暂停/恢复写入、切换时刻、RPO/RTO 和公共 Recall/Write/Console/客户端 Smoke。业务 Seed 经授权公共 API 提交，服务初始化与离线恢复经受控 CLI 完成；不得让业务脚本 import Repository 或直接写库。导入验收完成后另跑手动导入链，不能预先依赖尚未实现的导入器；恢复后复测客户端文件权限与管理拒绝矩阵。
- 形成运行手册：初始化租户/首把密钥、公共方法与角色权限、备份与离线恢复、升级回退、WAL/磁盘满、DLQ、投影重建、Provider/模型切换、Persona Pointer、凭据轮换、Forget/保留、Core/通用客户端兼容和告警响应。面向调用方的示例只使用白名单方法，不指导其打开数据库/FAISS/队列；可信运维手册与业务接入文档分开。

**通过条件**：3 轮完整演练通过；RPO/RTO 在声明规模达到 14.0 冻结的预算，步骤可由其他操作者复现。

### 14.6 候选发布验收与稳定交付

- Core Release Manifest 绑定 Commit/版本、wheel/sdist 摘要及文件/依赖清单、公共接口白名单版本/摘要、两套契约和 Fixture、Migration Checksum、SQLite Runtime、Provider/Builder、隔离部署与测试报告；配套客户端、Console、镜像/SBOM 分别列出消费或部署版本，不作为 Core 内含组件。Phase 11/12 明确标记 Deferred/excluded，不要求适配器版本、npm 发布或宿主 E2E 证明。
- 将基线 §38 中当前 Core 范围的每项映射到真实测试/演练，Phase 11/12 专属项注明暂缓及范围决定，不记为通过；反向检查所有承诺能力均有验证。公共方法、隐藏内部组件及运行资源权限必须有安装物与真实部署证据，历史数字、模拟前端、源码安装成功均不能顶替。
- 复核安装、配置、运行手册、兼容/已知限制与弃用说明；旧 rebuild 端点按已声明窗口处理，不为清理文档直接删接口。
- 对同一不可变 RC 完成全部门禁，记录发布评审结果后产生 1.0.0；失败项须修复并复跑受影响链路，不能原地覆盖已签名产物。

## 数据、契约与回退策略

以当前工作区 Schema 14 为重规划起点，后续新增表/字段按顺序申请 Migration；本计划不预占编号。Core 包版本、公共 Python 方法清单、数据库 Schema、`/v1`、`/console/v1` 与通用 SDK 的兼容范围分别记录，不能要求它们机械使用同一版本号。Phase 11/12 适配器矩阵保留历史值且移出本轮候选验收。

Core Provider 与 Console 新能力先完成兼容 Reader/Worker，再启用 Writer/Job。未知必需能力失败关闭；独立 Console 契约保持公共 `/v1` 兼容。公开方法/签名/DTO 变更遵守 [ADR-0006](../adr/0006-api-version-and-compatibility.md)，私有内部重排不得改变公共行为；现有公开契约不得以收紧内部边界为由无声删除。回退同时考虑数据、删除账本、权限/凭据与索引，不承诺仅替换镜像即可跨不兼容 Schema 回滚。

## 量化验收基线

| 项目 | 发布门槛 |
| --- | --- |
| Core 延迟 | Observation p95 ≤30 ms；100 条 Batch ≤150 ms；结构化 Recall ≤50 ms；FTS ≤100 ms；Hybrid ≤250 ms；State Coalesced Write ≤25 ms；Persona Current ≤20 ms；Forget Canonical 生效 ≤100 ms |
| 公共接口与产物 | Core wheel/sdist/依赖闭包中 Phase 11/12 适配器和宿主依赖=0；白名单外公开方法/入口=0；DTO/属性/异常暴露内部组件=0；所有声明支持的公共方法有真实 Core 消费证据 |
| 访问隔离 | 独立普通客户端身份直接读写 Core SQLite/WAL/SHM、FAISS、内部状态或密钥/备份目录成功=0；业务 Token 通过私有/越权管理接口访问成功=0 |
| Console 与客户端 | 使用公共方法验证真实 Core 多客户端竞争/重连与 Lease 矩阵；Phase 11/12 专属验收均暂缓。Console 按 14.0 冻结的列表/统计 p95、导入吞吐及导出内存预算验收，区分端到端与 Core 时延 |
| Soak | 连续 ≥24h；记录 WAL/Checkpoint、RSS/句柄、Queue/Schedule Lag、磁盘、错误率与 Tail Latency；结束后完整性/投影重建/Recall 抽样全过 |
| 关闭与 Crash | 正常排空默认 ≤30s；每个进程故障边界 ≥20 次；已确认 Canonical 丢失=0 |
| 恢复 | 3 轮完整安装/升级/备份/恢复/回退；RPO/RTO 的规模及目标须在执行前冻结，禁止按实测倒填门槛 |
| 安全与覆盖 | 泄漏=0；未处置 Critical/High=0；§38 当前 Core 范围证据缺项=0，Phase 11/12 专属项记录为暂缓而非通过；所有声明支持的版本组合通过 |

## 退出门禁

- [ ] 14.0：Core/Phase 13、兼容、公共方法白名单及权限边界冻结；Phase 11/12 均 Deferred，不纳入当前门禁。
- [ ] 14.1：Core wheel/sdist 及依赖闭包无适配器，脱离源码可用；公开导出与白名单一致，统一 CI 含契约/通用客户端/Console/公共接口边界。
- [ ] 14.2：镜像、Compose、SQLite、Provider、Ready/指标、进程生命周期及客户端身份/文件隔离通过。
- [ ] 14.3：私有对象与运行资源直达负例、安全/供应链扫描、密钥轮换及恢复后访问控制通过。
- [ ] 14.4：量化性能、容量、24h Soak 与真实进程故障门禁通过。
- [ ] 14.5：3 轮隔离恢复/升级回退、删除不复活和运行手册演练通过。
- [ ] 14.6：兼容矩阵、§38 追踪、Manifest 与不可变候选的全部证据完成发布评审。

## 交付证据

最新实施与实测见 [Phase 14 验证记录](../reports/phase-14-verification.md)：当前读面漂移与安装资源缺失已修复，Required Recall/Focus 接线和安装消费门禁已实现；完整发布门禁仍未完成。下段保留重规划时的历史状态。

本次修订仅交付 Phase 11/12 暂缓、Core pip 范围与公共方法/隔离门禁的计划更新；上文保留此前 pip 定向核查，未因文档更新重跑构建或实现公共封装、权限隔离。尚未执行完整发布验收，以上门禁均未通过。后续报告按工作包记录日期、候选 Commit/产物摘要、环境、命令、结果、失败/限制和长期证据，不提前创建空报告或虚构 Digest。

- 前置证据：[Phase 10 报告](../reports/phase-10-verification.md)、[Phase 13 报告](../reports/phase-13-verification.md)。暂缓范围及历史证据见 [Phase 11 计划](./phase-11-bellis-adapter.md)、[Phase 11 报告](../reports/phase-11-verification.md)、[Phase 12 计划](./phase-12-astrbot-bridge.md)，不作为本轮未完成前置门禁。
- 发布待产物：Core wheel/sdist 与内容/依赖清单、公共方法白名单及消费报告、访问隔离负例、Release Manifest、SBOM/扫描、性能/Soak/故障报告、通用兼容矩阵、3 轮恢复记录、运行手册与 §38 范围追踪表；在对应工作包完成时链接实际文件。

## 明确不做

- 不重做 Phase 0–10 的领域模型，不扩展 SQLite 多主或共享网络文件系统部署。
- 不恢复自动旧库扫描、增量追平或双写切换工具链；不把 Backup 当授权 Export。
- 不实施或分发 Phase 11/12 适配器，不把宿主 E2E、npm 发布或宿主专属 SDK 扩展重新列为 Core pip 前置条件；暂缓不等于完成。
- 不向业务调用方暴露 SQLite、FAISS、内部队列、Repository、运行容器或其他私有组件；不新增通用 SQL/文件/对象/脚本执行接口，不把同进程 Python 封装宣称为安全沙箱。
- 不因适配器暂缓取消 Core/Phase 13 的正确性、公共方法、访问隔离或安全门禁；不把 Core 内部存储/索引/任务引擎误当作待排除的宿主适配器。
- 不把 Recall Cache、FTS 历史检索、Graph 反向遍历、多租户索引拆分等优化无条件扩成发布前需求；违反承诺容量/安全门槛时才立项。

## 交接条件

14.1 的打包/CI 和 14.2 的部署准备可在 14.0 闭环期间并行；最终性能、安全、恢复和稳定验收必须针对冻结后的同一候选。全部门禁通过才标记 Phase 14 Completed；未完成项保留需求 ID、原因和依赖，后续变更进入版本化 Backlog/ADR。
