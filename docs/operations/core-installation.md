# Core 安装与可信初始化

[English](core-installation.en.md)

本页说明当前开发候选的安装路径；生产运行时、隔离、恢复与发布验收仍按 [Phase 14](../development/phase-14-hardening-release.md) 执行。Core、Python SDK、TS SDK、Console 静态文件分别交付，不包含 Bellis/AstrBot 适配器。

## 构建与安装物检查

```sh
make package-check
```

该门禁从 sdist 重建 Core wheel，扫描运行资源、依赖、extra、CLI，独立安装 SDK，并启动真实 Core 供 SDK 消费。它使用开发 SQLite 显式覆盖；生产验收必须不带该覆盖运行。Core 包装配含内部 SQLite/FAISS/队列实现，内部模块不属于调用方业务 API。

## 可信操作者初始化

安装经过验证的 wheel 后，由 Core 专用 OS 身份执行。先为数据和凭据配置专用目录及权限，再运行下面的命令。不要在客户端目录或前端静态目录内保存 Core 数据。

```sh
iris-memory-core init \
  --database /var/lib/iris-core/data/core.sqlite3 \
  --tenant tenant-example \
  --agent-name assistant \
  --app-instance client-example \
  --credential-file /var/lib/iris-core/provisioning/client.json \
  --surface-mode required
```

`init` 执行 Migration，创建租户、带初始 Persona 的 Agent、local Space 和 application 平面凭据；凭据只授权该 Agent/Space 和 reply purpose，不获得管理权限。文件必须不存在，创建模式为 0600；命令输出只有资源 ID。已有文件或符号链接被拒绝。过期默认 30 天，可用 `--expires-days` 指定 1–365 天。

需要新租户立即具备搜索投影时，显式添加 `--initialize-search`。它在同一个初始化事务内构建并校验 FTS generation，成功后输出 `search_generation_id`；Worker 随后增量维护该 generation。索引不可用或校验失败会回滚租户/身份/凭据及索引，不留下可用凭据文件。该选项不授予客户端管理权限，也不用于升级已有数据库；已有库仍遵守下方的离线迁移要求。

Recall 的 speaker 需要已有的已确认身份。首次初始化时，可信操作者可显式添加 `--actor-provider PROVIDER --actor-subject SUBJECT`，在同一事务内登记新的身份、主体和 admin-confirmed Binding。该操作表示操作者已经确认对应关系；已存在的身份不能通过 bootstrap 重新绑定。不要用它批量推测真实用户身份。

当前 SDK 的能力、超时与版本限制见 [Python SDK](../../sdk/python/README.md)。业务调用方只获得服务端点和对应受限凭据，由可信操作者安全交付客户端凭据文件；不共享 Core 数据、密钥、备份目录或服务身份。

## 启动与业务 Lease

```sh
iris-memory-core serve --database /var/lib/iris-core/data/core.sqlite3 --host 127.0.0.1
iris-memory-core worker --database /var/lib/iris-core/data/core.sqlite3
```

默认 Console 关闭。启用时单独安装 console extra，部署独立构建的静态文件并设置 HTTPS Origin/Host/可信代理，见 [Console README](../../web/console/README.md)。生产不使用 `--allow-local-sqlite` 或 `--console-dev-http`。当前默认 Provider 的生产限制见 [根 README](../../README.md#requirements)。

Required 模式下，SDK 先使用 `acquire_surface_lease` 获得属于自身 app_instance 的 Proof，再把 `lease_id`、`lease_epoch` 传入支持的在线请求。Recall 请求字典和 Focus create 字典携带这两个字段；`focus_transition` 使用同名可选关键字。相同业务幂等键可在重新取得有效租约后重试；没有有效租约的旧成功响应也不会被重放。

Core 无公共嵌入式 façade；不要通过 storage/runtime/application 私有类、SQL、FAISS 文件或队列记录执行客户端业务。普通 Python 导入不是安全沙箱，生产隔离仍依赖独立 OS 身份、私有目录权限和不向客户端挂载数据卷。


## Core 0.13.0 / Schema 20 升级

运行时只支持 Schema 20。首次空库由 init/serve/worker 应用完整迁移；已有库禁止自动应用 0015、0017 或 0018。0016 的持久删除预览表允许在线升级，但 0017 扫描历史 Entity Tombstone 并补写删除账本，要求停机与验证备份。0018 重建 State 主/修订表并添加删除代际唯一索引，同样需要停机和验证备份。0019 仅增加 Task 删除查找索引，0020 新增固定批量删除 Operation 表，Schema 18/19 可在线升级。从 Schema 17 或更早版本升级须停止 API 和 Worker，使用以下流程：

```sh
iris-memory-core migrate /var/lib/iris-core/data/core.sqlite3 \
  --allow-offline \
  --with-backup /var/lib/iris-core/backups/before-schema-20
```

CLI 创建并验证备份后才执行迁移。备份认证可另传 `--backup-key-file`；目录必须独立且保存好旧安装物。成功应为 `schema_version=20 applied=6`（从 Schema 14 升级），从 Schema 15/16/17 升级的 applied 分别为 5/4/3；Schema 18/19 只需普通 migrate，结果分别为 `schema_version=20 applied=2` 与 `schema_version=20 applied=1`。重复执行不再应用迁移。确认 schema-version 和 Ready 后再启动两个进程。

回退需要先停新进程，再隔离恢复升级前备份并使用与其 Schema 匹配的原安装物（例如 Schema 14 配套旧 Core 0.12.0）；不对 Schema 20 原地降级。升级后的新增内容需另行保全核对。中断的首次建库也按已有库检查、备份和继续迁移，不自动视为空库。完整约束见 [ADR-0026](../adr/0026-task-dependency-lifecycle.md)、[ADR-0037](../adr/0037-console-durable-forget-previews.md)、[ADR-0038](../adr/0038-console-entity-tombstone-ledger.md)、[ADR-0040](../adr/0040-console-state-forget-generations.md) 、[ADR-0041](../adr/0041-console-task-forget-cascade.md) 和 [ADR-0042](../adr/0042-console-forget-operations.md)。

Trusted `init` credentials include the application read capability `events.sse.v1`, allowing a host to negotiate and consume public invalidation notifications within the credential scope. This does not grant administration rights or update existing credentials. Hosts must acknowledge their own durable processing before advancing the stream cursor.

恢复 Schema 20 备份时，最新删除账本先保护已提交内容。尚未完成的删除 Operation 在目录切换前核对进度并转为 `blocked / restore_requires_review`，不会恢复后自动续删；须重新查看现存目标并生成新预览。取消不会撤销已提交删除。完整凭据重置和生产恢复演练仍待 Phase 14.5。
