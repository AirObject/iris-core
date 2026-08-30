# 阶段 14：硬化、容器与发布

> 状态：Planned  
> 前置阶段：[阶段 13](./phase-13-legacy-migration.md)  
> 目标版本：1.0.0  
> 架构依据：[§29 安全与隐私](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#29-安全与隐私)、[§30 性能与容量](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#30-性能与容量目标)、[§31 可观测性](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#31-可观测性)、[§35 部署与运维](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#35-部署与运维)、[§36 阶段 14](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#阶段-14硬化容器与发布)、[§38 顶层验收](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#38-顶层验收标准)

## 阶段目标

把已经闭环的服务、SDK、两个 Adapter 和迁移工具硬化为可重复安装、升级、备份、恢复和回滚的首个稳定发布，并以安全扫描、性能基线、24h+ Soak、故障注入和恢复演练证明可运维性。

## 架构约束

- 默认拓扑是单机 SQLite 写主与本地持久卷；不把共享网络文件系统或多主写入包装成 v1 能力。
- 镜像非 Root、最小且锁定 Digest，根文件系统只读，Secret 不进入镜像、日志、备份或诊断包。
- Ready 必须反映 SQLite Runtime/Schema、磁盘、Persona Pointer、必要 Provider、Migration、Worker/Scheduler 和索引降级状态。
- 优雅关闭先撤 Ready，再停止新请求/Job，最后 Flush/释放；强制终止依赖事务、Lease、Tick 和幂等恢复。
- 备份用于灾难恢复，导出用于授权数据携带，两者权限、格式、保留和密钥分离。

## 需求追踪

| 需求 ID | 基线要求 | 工作包 | 验证门禁 |
| --- | --- | --- | --- |
| P14-IMAGE-01 | 锁定 Runtime、非 Root、只读根、专用卷与优雅关闭 | 14.1 | 镜像配置、权限、启动/停止和干净安装测试 |
| P14-SUPPLY-01 | SBOM、依赖/镜像/许可证/Secret 扫描与密钥隔离 | 14.2 | 发布扫描、例外审批和轮换演练 |
| P14-SECURITY-01 | Scope/Privacy/Artifact/日志/导出/备份安全回归 | 14.2 | 越权、路径、泄漏和恢复后删除测试 |
| P14-PERF-01 | 全部 p95、容量保护、24h+ Soak 与故障注入 | 14.3 | 性能报告、资源趋势和 Soak 后一致性 |
| P14-RECOVERY-01 | Online Backup、隔离 Restore、重建、RPO/RTO | 14.4 | 灾难恢复、Smoke、Runbook 与告警演练 |
| P14-RELEASE-01 | Core/Schema/SDK/Adapter/Migration/镜像版本绑定 | 14.5 | Manifest、安装、升级、回滚和兼容矩阵 |
| P14-ACCEPTANCE-01 | 基线 §38 全部顶层验收可定位到证据 | 14.1–14.5 | 双向追踪表和发布签字 |

## 工作包

### 14.1 镜像与 Compose

- 构建 API/Worker 同镜像不同命令，锁定 Python/SQLite Runtime 与依赖 Digest。
- 配置非 Root、只读根、专用 `/data`、受限临时目录、Healthcheck、Stop Grace、资源限制和日志轮转。
- 提供干净环境 Compose、配置示例和 Secret/File 注入，不把早期开发运行方式强制替换掉。

### 14.2 安全与供应链

- 生成 SBOM，执行依赖、许可证、镜像、Secret 和已知漏洞扫描。
- 验证 TLS/Unix Socket 权限、Artifact 路径、管理/应用凭据隔离、备份静态加密和 Secret 轮换。
- 执行日志/Metric/Trace/错误/导出泄漏扫描及跨 Scope 越权回归。

### 14.3 性能、容量与 Soak

- 在声明硬件、数据规模、文本长度、Candidate/并发和冷/热索引条件下测全部目标 p95。
- 混合 Observe/Recall/State/Task/Worker 运行至少 24 小时，监测 WAL、Checkpoint、内存、句柄、Lag 和 Tail Latency。
- 注入 Provider 慢/断、Worker Crash、FAISS 损坏、磁盘阈值、时钟异常和备份负载。

### 14.4 备份、恢复与运维手册

- 完成含 Manifest/Checksum/Artifact/可选索引的 Online Backup 和隔离恢复。
- 演练 Integrity/Foreign Key/Tombstone/Pointer/Outbox/Tick、投影重建、Smoke Recall/Write 和原子切换。
- 编写数据库/WAL、索引重建、Dead Letter、磁盘满、Provider/模型迁移、Persona Pointer、Credential、删除和 Adapter 兼容手册。

### 14.5 Release Train

- 绑定 Core、Schema、SDK、Fixture、Adapter 支持范围、Migration 和镜像 Digest 的 Release Manifest。
- 建立安装、Online-safe Upgrade、需停机 Migration、Rollback、Breaking Change 和数据版本策略。
- 在干净环境执行 Install → Seed → Upgrade → Backup → Restore → Rollback 演练。

## 数据、契约与回退策略

- Release Manifest 锁定 Core Commit/Version、Schema/SDK/Fixture、Migration Checksum、SQLite Runtime、Provider/Builder、Adapter/迁移工具版本、镜像 Digest、SBOM 与兼容范围；发布物签名后不可原地替换。
- Schema Upgrade 遵循 Expand/Validate/Cutover；Online-safe 与需停机 Migration 分开声明。先升级兼容 Reader/Worker，再启用新 Writer/Job，最后按回退窗口清理旧形态。
- Backup Manifest 覆盖 SQLite、Artifact、Vector Generation 和必要配置引用；Export 使用独立授权 Schema、密钥和保留策略。恢复在隔离目录校验后才原子切换数据路径。
- 索引、Provider 和 Adapter 能力按 Capability 分阶段启用；发布/回退顺序尊重 Core/Schema/SDK 与两个 Adapter 的最小/最大兼容矩阵，未知必需能力 Fail Closed。
- 回退优先回滚二进制/Adapter 并保留向前兼容 Schema；若数据版本不兼容，则从切换前已验证备份恢复、重放授权增量和 Tombstone，再执行 Pointer/Outbox/Tick/Recall/Write Smoke，禁止破坏性 Down Migration。

## 量化验收基线

- 在同一声明环境复测：Observation p95 ≤ 30 ms、100 条 Batch p95 ≤ 150 ms、结构化 Recall p95 ≤ 50 ms、FTS p95 ≤ 100 ms、Hybrid p95 ≤ 250 ms、State Coalesced Write p95 ≤ 25 ms、Persona Current p95 ≤ 20 ms、Forget Canonical 生效 p95 ≤ 100 ms。
- 混合 Observe/Recall/State/Task/Worker 负载连续运行至少 24 小时；报告 WAL/Checkpoint、RSS、文件句柄、Queue/Schedule Lag、磁盘、错误率和 Tail Latency，结束后 Integrity、Foreign Key、投影重建与 Recall 抽样全部通过。
- 优雅关闭默认 ≤ 30 s；API/Worker 在关闭各步骤和事务/Lease/Tick 边界的 Kill -9 场景各至少重复 20 次，已确认 Canonical 事务丢失数必须为 0。
- 干净环境的 Install → Seed → Upgrade → Backup → Restore → Rollback 连续通过 3 轮；每轮记录 RPO/RTO、Migration/Manifest Checksum、Tombstone/Pointer/Outbox/Tick 与 Smoke 结果。
- 发布门禁要求 Secret 泄漏数为 0、未审批 Critical/High 漏洞数为 0、SBOM 覆盖所有镜像/语言依赖；所有例外有负责人、到期时间、缓解措施和发布审批。
- §38 每条顶层验收都必须映射到至少一项自动测试或演练证据，缺失项为 0；Core/SDK/Schema/Bellis/AstrBot/Migration 的最小、最大和当前版本矩阵全部通过。

## 退出门禁

- [ ] 架构基线第 38 章全部顶层验收项有对应自动测试或演练证据。
- [ ] 容器以非 Root/只读根运行，Secret、SBOM 和扫描策略通过发布门禁。
- [ ] 性能目标在已声明条件达成，24h+ Soak 后 Integrity、重建和 Recall 抽样一致。
- [ ] 优雅关闭和 Kill -9 恢复不丢失已确认 Canonical 事务。
- [ ] 备份/恢复/导出、Tombstone 保留、索引重建和故障手册演练通过。
- [ ] Core/SDK/Schema/Bellis/AstrBot/迁移版本矩阵与 Upgrade/Rollback 流程通过。
- [ ] 干净环境可以重复完成安装、升级、备份和恢复。
- [ ] Release Manifest、版本兼容/数据回退方案、需求追踪和全部交付证据已完成发布评审。

## 交付证据

- Release Manifest/镜像 Digest：待补充
- SBOM/安全扫描报告：待补充
- 性能/Soak/故障注入报告：待补充
- Backup/Restore/Upgrade/Rollback 演练：待补充
- 运维手册与告警清单：待补充

## 明确不做

- 不扩展为 SQLite 多主、跨主机共享数据库或无边界水平写扩展。
- 不为赶发布跳过不兼容 Migration、恢复演练或 Adapter 版本门禁。
- 不将数据库备份直接作为用户数据导出。

## 交接条件

全部退出门禁和顶层验收证据齐全后，项目可标记首个稳定发布；后续工作进入版本化 Backlog/ADR，而不是继续修改已完成阶段的隐含语义。
