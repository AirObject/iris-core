# 阶段 14：硬化、容器与发布

> 状态：Planned  
> 前置阶段：[阶段 13](./phase-13-legacy-migration.md)  
> 架构依据：[安全、性能、可观测性、部署运维、顶层验收与阶段 14](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md)

## 阶段目标

把已经闭环的服务、SDK、两个 Adapter 和迁移工具硬化为可重复安装、升级、备份、恢复和回滚的首个稳定发布，并以安全扫描、性能基线、24h+ Soak、故障注入和恢复演练证明可运维性。

## 架构约束

- 默认拓扑是单机 SQLite 写主与本地持久卷；不把共享网络文件系统或多主写入包装成 v1 能力。
- 镜像非 Root、最小且锁定 Digest，根文件系统只读，Secret 不进入镜像、日志、备份或诊断包。
- Ready 必须反映 SQLite Runtime/Schema、磁盘、Persona Pointer、必要 Provider、Migration、Worker/Scheduler 和索引降级状态。
- 优雅关闭先撤 Ready，再停止新请求/Job，最后 Flush/释放；强制终止依赖事务、Lease、Tick 和幂等恢复。
- 备份用于灾难恢复，导出用于授权数据携带，两者权限、格式、保留和密钥分离。

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

## 退出门禁

- [ ] 架构基线第 38 章全部顶层验收项有对应自动测试或演练证据。
- [ ] 容器以非 Root/只读根运行，Secret、SBOM 和扫描策略通过发布门禁。
- [ ] 性能目标在已声明条件达成，24h+ Soak 后 Integrity、重建和 Recall 抽样一致。
- [ ] 优雅关闭和 Kill -9 恢复不丢失已确认 Canonical 事务。
- [ ] 备份/恢复/导出、Tombstone 保留、索引重建和故障手册演练通过。
- [ ] Core/SDK/Schema/Bellis/AstrBot/迁移版本矩阵与 Upgrade/Rollback 流程通过。
- [ ] 干净环境可以重复完成安装、升级、备份和恢复。

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
