# 阶段 14：前置闭环、生产硬化与稳定发布

> 状态：Planned（2026-09-06 按工作区执行证据重规划；尚未进入发布验收）  
> 前置阶段：[Phase 11](./phase-11-bellis-adapter.md)、[Phase 12](./phase-12-astrbot-bridge.md)、[Phase 13](./phase-13-web-console.md)；均须完成退出门禁  
> 目标版本：1.0.0（发布目标，非当前版本；RC 编号在 14.0 冻结）  
> 架构依据：[§29 安全](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#29-安全与隐私)、[§30 性能](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#30-性能与容量目标)、[§35 运维](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#35-部署与运维)、[§38 验收](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#38-顶层验收标准)

## 阶段目标

将已有 Core 能力、两个宿主适配器和管理控制台交付为可重复安装、升级、运行和恢复的稳定版本。先关闭实际存在的前置缺口，再用同一候选发布物完成安全、容量、故障和恢复验证。历史阶段通过记录只证明当时切片，不能代替当前组合的发布证据。

### 重规划依据

| 当前事实（2026-09-06） | 对本阶段的影响 |
| --- | --- |
| Phase 0–10 已有领域、真实 HTTP、Worker、迁移、备份/恢复、性能和事务级故障测试 | 复用已有能力与测试，补生产装配和进程级证据 |
| 工作区 Core 0.12.0、Schema 14；`/v1` Contract 1.9.0；Python SDK 0.11.0、TS SDK 0.11.1 | 版本分别演进；冻结实际兼容矩阵，不能按阶段号推算包版本 |
| Bellis 插件和定向测试存在，但默认最多接受 Schema 11；宿主 effect、对账和真实端到端门禁仍未齐 | 14.0-A 关闭 Phase 11 遗留并与目标 Core 重测 |
| Phase 12 仅有接入文档，无 Bridge 实现和 ADR-0021 | 14.0-B 完成前置裁决、实现与验收，不能把 AstrBot 写成已交付 |
| Console 认证切片有历史 CI；读面本轮 53 passed / 1 failed，前端 `types:check` 失败；其余业务前端主要有模拟验证 | 14.0-C 修契约/类型漂移及页面适配，再完成未发布切片与真实联调 |
| `make ci` 尚不包含 Console 前端；没有生产镜像、Compose、发布流水线和 24h Soak 证据 | 14.1–14.6 建立完整发布门禁 |
| 原 Phase 13 自动迁移已由 ADR-0022 替换为手动文件导入导出 | 删除旧库扫描、增量追平、双写切换和迁移工具发布物；保留 Core 数据库 Migration 和删除安全要求 |

版本真源见 [version manifest](../../schemas/version-manifest.json) 与各包元数据；接入和控制台现状见对应阶段与验证报告。以上是本次重规划快照，不另建手工版本清单。

## 架构约束

- 单机 SQLite 写主、本地持久卷；API/Worker 可以独立进程，Lease/Fencing 和幂等仍由 Core 保障。
- `/v1` 宿主凭据与 `/console/v1` 运营密钥/浏览器会话隔离；控制台默认关闭。生产不能用 `--allow-local-sqlite` 或 `--console-dev-http` 绕过运行要求。
- 运行包必须带齐所需资源；从源码目录运行成功不等于 wheel、SDK 包或镜像可安装。
- Scope/Privacy、Revision、Tombstone、Canonical/Projection 与审计不变量贯穿导入、导出、恢复、后台任务和宿主副作用。
- 恢复是离线可信操作；Console 不提供恢复按钮。Backup 与授权 Export 的格式、权限、保留和密钥各自管理。
- FAISS 文件目前不随备份交付，恢复后必须重建。Backup HMAC 提供完整性/真实性验证，不等于静态加密。

## 需求追踪

| 需求 ID | 工作包 | 交付与门禁 |
| --- | --- | --- |
| P14-BASELINE-01 | 14.0 | 前置阶段退出证据、资源 ID 裁决、零类型漂移、目标版本兼容矩阵 |
| P14-PACKAGE-01 | 14.1 | 脱离源码目录安装 wheel/SDK/前端产物；完整 CI 与契约生成可重复 |
| P14-DEPLOY-01 | 14.2 | 非 Root/只读根镜像、实际 SQLite Allowlist、Compose 启停与生产 Provider 接线 |
| P14-SECURITY-01 | 14.3 | 跨平面越权、凭据生命周期、泄漏与供应链扫描、加密/密钥策略演练 |
| P14-PERF-01 | 14.4 | 同条件性能基线、容量上限、24h+ 混合 Soak、进程故障证据 |
| P14-RECOVERY-01 | 14.5 | 隔离恢复、删除账本/凭据处理、投影重建、3 轮升级回退与 RPO/RTO |
| P14-RELEASE-01 | 14.6 | 不可变 Release Manifest、兼容矩阵、运行手册及 §38 双向验收追踪 |

## 工作包

### 14.0 前置阶段闭环与候选范围冻结

本工作包组织 Phase 11–13 的欠缺项，实施和验收仍回写原阶段，不能通过搬移待办将原阶段改为 Completed。

- **14.0-A Bellis**：在实际 Core/Schema 上验证协商及失败关闭，更新插件默认兼容范围和矩阵；落实宿主独立 effect/progress 确认（连接代际、Scene/Cue、片段范围），完成宿主 Outbox 远端持久 ACK、reconcile 补投、Usage、取消与真实模型请求的闭环证据。取消/失败中的未确认内容为零，已确认前缀仍按 Partial 记录。保留当前无 SDK 源码 alias 的 registry 消费，完成公开 npm 分发、Provider 并入宿主 workspace/CI；修正分类兼容矩阵、categoryMap 越界、审计/Hash 和 Persona 撤销/刷新恢复缺口，详见 Phase 11 报告。不得仅调大 Schema 上界就宣称兼容。
- **14.0-B AstrBot**：按 Phase 12 完成 ADR-0021（effect 边界、交付位置、Python SDK 分发、身份映射）、所需 SDK 能力、Bridge 与宿主生命周期测试。插件缺失、宿主 ACK 语义不明确或无安装证据时，双 Adapter 发布门禁保持未通过。
- **14.0-C Console**：先裁决 Canonical 非 UUID ID 与 `ResourcePage.id` 契约冲突，补兼容 Fixture 并修复读面失败；同步 OpenAPI 生成前端类型，并将读页面接到实际的 `list_columns`、结构化 `sorts`、`create_schema/update_schema` 注册表。再按 Phase 13 完成记忆命令/Forget、统计、手动导出导入、Embedding Provider、运行参数和 Operation/运维面。每个模块须从契约、后端、前端连到真实浏览器验证，模拟测试不能充作真实联调。冻结导入格式、来源映射、幂等/报告哈希与恢复边界。
- **14.0-D Core 遗留**：核对历史报告中已解除与仍有效的限制。生产认知 Provider 的能力声明、SSE 多进程重连与慢消费者、真实 `serve` 排空/SIGKILL、指标采集、Dead Letter 运维、旧 rebuild 端点弃用窗口分别归入下列工作包；保持未知必需能力失败关闭。
- **14.0-E Required Surface**：补齐在线 Recall/Focus 的 Lease 门禁覆盖并核对其余在线操作；现有 SurfaceService 不等于所有应用入口已接线。先对齐契约携带的 Lease Proof、传输入口与服务端检查，再验证缺失/过期/旧 Epoch/非 Holder 请求失败关闭，不能只依赖 Adapter 自律。
- 冻结候选版本、支持平台/硬件/数据规模、责任人、最小和最大兼容版本、验收清单；在测量前固定 Console 列表/统计 p95、导入吞吐/事务预算、最大导出与 Blob 回退内存预算及 RPO/RTO。此计划继续保留两个 Adapter 与完整 Phase 13 的目标；缩小发布范围须单独记录范围决定及对基线验收的影响。

**通过条件**：Phase 11–13 退出门禁全部有证据，当前契约与类型漂移为零，兼容矩阵能在当前候选上执行。

### 14.1 可安装产物与统一 CI

- 检查 wheel/sdist 的 Migration、契约运行资源与路径解析；目前 `default_migrations_path()` 依赖仓库目录布局，必须在没有源码 checkout 的空环境证明 migrate/serve/worker 可运行。
- 分别构建 Python SDK、TS SDK、Bridge 和 Console 静态包；验证通过安装产物接入，不依赖开发机工作区路径。产物与锁文件一并记录摘要。
- 在现有 `make ci` 基础上接入 Console `types:check`、lint、typecheck、单元测试、生产 build 和真实后端 browser 测试；把 Core 两套契约、SDK 和 Adapter 门禁纳入同一候选版本流水线。
- 文档检查覆盖根目录、`docs/`、SDK、`application/` 和 `web/console/` 的手写 Markdown 路径/锚点；生成清单只校验真源与生成物，不复制到说明文档。

**通过条件**：锁定依赖与产物齐备；干净环境安装及统一 CI 全过，产物不包含测试密钥或生产模拟模式。

### 14.2 容器、生产装配与可观测性

- API/Worker 同镜像不同命令，构建并锁定实际满足 Allowlist 的 Python/SQLite 组合；提供镜像 Digest、非 Root、只读根、`/data` 卷、受限临时目录、Healthcheck、Stop Grace 和资源限制。
- 提供 Compose、配置与 File Secret 示例；Console 静态资源和数据/备份/导出目录隔离，明确 Origin、Host、可信反代和 TLS 终止配置。
- 接通 Phase 13 的 Embedding Provider 配置与 `runtime` 的在线查询/后台索引路径；认知 Provider 目前随包为确定性实现，须验证可用于声明用途的真实 Provider 或明确禁用相应生产能力，不能以 Fake 测试代替质量验收。
- Ready/Liveness 验证 SQLite、Schema、Persona Pointer、磁盘、必要 Provider、Worker/Scheduler、投影降级及 Console 待重启配置。对每租户 JSON `/metrics` 选择实际采集方案，补 Queue/Schedule Lag、WAL/Checkpoint、RSS/句柄和告警演练。
- 真实网络进程验证停止顺序、SSE 重连与慢消费者、API/Worker 共享配置与多进程一致性；退出时先撤 Ready，再排空请求/Job，最后释放资源。

**通过条件**：生产配置下无需开发绕过参数即可启停；健康、指标、告警和 Provider 行为有实测证据。

### 14.3 安全、凭据与供应链

- 回归跨 Tenant/Agent/Scope/Privacy、管理/应用平面隔离、CSRF/Origin/reauth、权限变化后 Cursor/下载失效、Artifact 路径、CSV 公式转义、导入拒绝清单和 Provider SSRF。
- 演练运营密钥、宿主凭据、Session、刷新重试材料、Provider Secret 与备份验签密钥的签发/轮换/撤销。明确恢复旧快照后会话失效、已撤销凭据不复活的实现与密钥托管/恢复策略。独立的 `console-auth.key` 与 Provider sealed 主密钥、Backup HMAC 密钥分别管理；当前含会话的数据库在认证密钥文件缺失时会拒绝启动，须覆盖密钥缺失/错误和可信恢复的负例。
- 选择并实现备份静态加密和密钥分离；同时保留 Manifest/Checksum/HMAC 校验。扫描快照与诊断包的凭据材料，不能凭 `secrets_included=false` 元数据宣称整库已脱敏。
- 生成覆盖 Python、Node、前端静态包与基础镜像的 SBOM，执行依赖/镜像/许可证/Secret 扫描；签名、来源证明和例外记录随发布物归档。

**通过条件**：Secret/敏感正文越界泄漏为零；无未处置 Critical/High 漏洞，例外有负责人、期限和缓解证据；密钥与恢复安全演练通过。

### 14.4 性能、容量、Soak 与进程故障

- 复用 `tests/performance/` 与事务边界 Kill -9 测试，在同一目标镜像/硬件/数据集补 HTTP、Console、两个宿主的端到端结果；记录冷/热索引、文本长度、候选数、并发、Provider 延迟和采样方法。
- 对 enqueue 的 O(pending) 聚合、高频 State 写、Recent/FTS/Graph/Profile 重建、Vector 切换窗口、5000 目标 Forget 上限做容量实验，形成拒绝/分批/维护窗口和增长阈值。覆盖保守水位降级、图扇出窗口漏召回与磁盘滞回的运行限制；无实测需要不新增缓存或改写投影架构。
- 混合 Observe/Recall/State/Task/Worker/Console/Adapter 负载运行至少 24h，注入 Provider 慢/断、Worker 与 API Crash、FAISS 损坏、磁盘阈值、时钟异常及并发备份；补 PersonaState TTL 在时钟回拨、Worker 停机及宿主重连后的恢复证据。
- 真实 `serve`/`worker` 进程与容器关闭边界各重复至少 20 次 SIGKILL；验证已确认 Canonical 事务不丢、任务可恢复、宿主副作用可对账，不能只以事务测试替代端口/排空验证。

**通过条件**：下述延迟目标与数据不变量通过；Soak 无无界资源增长/永久积压，异常有可观测结果及恢复证据。

### 14.5 备份、恢复、升级与运行手册

- 复用已有 Online Backup、Manifest/HMAC、Artifact 校验、隔离 Restore 与 `recover-switch`；补生产规模、加密封装、Console 新表/凭据、独立 `console-auth.key` 和最新删除账本的恢复覆盖；数据库备份本身不能替代认证密钥文件的受保护恢复。
- 恢复后检查 Integrity、Foreign Key、Tombstone、Persona Pointer、Outbox、Tick 与 Artifact；重建 FTS/Vector/Profile/Graph，明确重建期间降级与 Ready。禁止从旧快照恢复已删除数据或已撤销访问权限。
- 分别声明 online-safe 与需停机 Migration；兼容 Schema 优先回滚二进制，否则隔离恢复已验证快照并重放授权增量和删除账本。禁止破坏性 Down Migration。
- 在隔离环境演练 Install → Seed → Upgrade → Backup → Restore → Rollback，共 3 轮；记录恢复点、暂停/恢复写入、切换时刻、RPO/RTO 和 Recall/Write/Console/Adapter Smoke。Seed 可用授权 API/Fixture；导入验收完成后另跑手动导入链，不能预先依赖尚未实现的导入器。
- 形成运行手册：初始化租户/首把密钥、备份与离线恢复、升级回退、WAL/磁盘满、DLQ、投影重建、Provider/模型切换、Persona Pointer、凭据轮换、Forget/保留、Adapter 兼容和告警响应。

**通过条件**：3 轮完整演练通过；RPO/RTO 在声明规模达到 14.0 冻结的预算，步骤可由其他操作者复现。

### 14.6 候选发布验收与稳定交付

- Release Manifest 绑定 Core Commit/版本、两套契约及 Fixture 摘要、SDK/Adapter/Console 版本、Migration Checksum、SQLite Runtime、Provider/Builder、镜像 Digest、SBOM、支持范围与测试报告。Core 数据库 Migration 保留；自动旧库迁移工具不再是发布物。
- 将基线 §38 每项映射到真实测试/演练，再反向检查所有承诺能力均有验证。历史数字、模拟前端、源码安装成功均不能顶替当前候选证据。
- 复核安装、配置、运行手册、兼容/已知限制与弃用说明；旧 rebuild 端点按已声明窗口处理，不为清理文档直接删接口。
- 对同一不可变 RC 完成全部门禁，记录发布评审结果后产生 1.0.0；失败项须修复并复跑受影响链路，不能原地覆盖已签名产物。

## 数据、契约与回退策略

以当前工作区 Schema 14 为重规划起点，后续新增表/字段按顺序申请 Migration；本计划不预占编号。Core 包版本、数据库 Schema、`/v1`、`/console/v1`、SDK 与宿主插件的兼容范围分别记录，不能要求它们机械使用同一版本号。

适配器、Provider 与 Console 新能力先完成兼容 Reader/Worker，再启用 Writer/Job。未知必需能力失败关闭；独立 Console 契约保持宿主协议兼容。回退必须同时考虑数据、删除账本、权限/凭据状态和派生索引，不承诺仅替换镜像即可跨不兼容 Schema 回滚。

## 量化验收基线

| 项目 | 发布门槛 |
| --- | --- |
| Core 延迟 | Observation p95 ≤30 ms；100 条 Batch ≤150 ms；结构化 Recall ≤50 ms；FTS ≤100 ms；Hybrid ≤250 ms；State Coalesced Write ≤25 ms；Persona Current ≤20 ms；Forget Canonical 生效 ≤100 ms |
| Console 与宿主 | 宿主按 Phase 11/12 门禁复测；Console 按 14.0 预先冻结的列表/统计 p95、导入吞吐及导出内存预算验收，区分端到端与 Core 时延 |
| Soak | 连续 ≥24h；记录 WAL/Checkpoint、RSS/句柄、Queue/Schedule Lag、磁盘、错误率与 Tail Latency；结束后完整性/投影重建/Recall 抽样全过 |
| 关闭与 Crash | 正常排空默认 ≤30s；每个进程故障边界 ≥20 次；已确认 Canonical 丢失=0 |
| 恢复 | 3 轮完整安装/升级/备份/恢复/回退；RPO/RTO 的规模及目标须在执行前冻结，禁止按实测倒填门槛 |
| 安全与覆盖 | 泄漏=0；未处置 Critical/High=0；§38 证据缺项=0；所有声明支持的版本组合通过 |

## 退出门禁

- [ ] 14.0：Phase 11/12/13 实施、ADR、兼容及真实联调证据齐全。
- [ ] 14.1：安装产物脱离源码可用；统一 CI 含两套契约、SDK、Console 和宿主门禁。
- [ ] 14.2：镜像、Compose、SQLite、Provider、Ready/指标及进程生命周期验证通过。
- [ ] 14.3：安全/供应链扫描、密钥轮换与恢复后访问控制通过。
- [ ] 14.4：量化性能、容量、24h Soak 与真实进程故障门禁通过。
- [ ] 14.5：3 轮隔离恢复/升级回退、删除不复活和运行手册演练通过。
- [ ] 14.6：兼容矩阵、§38 追踪、Manifest 与不可变候选的全部证据完成发布评审。

## 交付证据

本次只交付重规划与文档整理，以上发布门禁均未执行。后续验证报告按工作包记录实际日期、候选 Commit/产物摘要、环境、完整命令、结果、失败/限制和长期可访问证据；不提前创建空报告或虚构 Digest。

- 前置证据：[Phase 10 报告](../reports/phase-10-verification.md)、[Phase 11 报告](../reports/phase-11-verification.md)、[Phase 12 计划](./phase-12-astrbot-bridge.md)、[Phase 13 报告](../reports/phase-13-verification.md)。
- 发布待产物：Release Manifest、SBOM/扫描、性能/Soak/故障报告、兼容矩阵、3 轮恢复记录、运行手册与 §38 追踪表；在对应工作包完成时链接实际文件。

## 明确不做

- 不重做 Phase 0–10 的领域模型，不扩展 SQLite 多主或共享网络文件系统部署。
- 不恢复自动旧库扫描、增量追平或双写切换工具链；不把 Backup 当授权 Export。
- 不为达到阶段编号而绕过未完成的 Adapter/Console 门禁，亦不默认为用户取消这些范围。
- 不把 Recall Cache、FTS 历史检索、Graph 反向遍历、多租户索引拆分等优化无条件扩成发布前需求；违反承诺容量/安全门槛时才立项。

## 交接条件

14.1 的打包/CI 和 14.2 的部署准备可在 14.0 闭环期间并行；最终性能、安全、恢复和稳定验收必须针对冻结后的同一候选。全部门禁通过才标记 Phase 14 Completed；未完成项保留需求 ID、原因和依赖，后续变更进入版本化 Backlog/ADR。
