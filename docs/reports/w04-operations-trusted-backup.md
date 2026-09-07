# W04 Operation 扩展与可信备份流程

状态：Completed，完整收尾验收通过。范围：Console 13.11 最小前置；Phase 11/12 保持 Deferred。

## 开工基线

- 开始 Commit：`460f7dd1f3fd35a3480a9e19e76cba94e0a69cd8`；W03 完整 CI 与独立提交已完成，开工时工作区干净。
- 当前 Core 0.13.0、Schema 20、业务 Contract 1.11.0、Python SDK 0.11.1、TS SDK 0.11.2；新迁移/契约版本在审查实际真源后分配。
- 复用 ADR-0042、Console 设计 §11、BackupService/AdminArchiveService，以及既有 Outbox/Worker/Lease。
- 已取消定时 automation；长任务记录实际会话、日志与结果，按完成事件续作。

## 必需门禁

| 要求 | 状态 |
| --- | --- |
| 先 ADR 与增量迁移；通用 Operation 元数据与类型专属载荷/权限/结果/取消分离 | 已实现，ci-003 完整通过 |
| Schema20 旧 Forget 历史可读，原 500 条及 preview/hold 约束保留 | 140 项迁移用例及旧 Forget 回归通过 |
| 首个新增消费者：真实创建并校验可信备份，仅返回内部不透明引用 | 实际服务、HTTP、独立 Worker 与真实浏览器通过 |
| 使用既有 Outbox/Worker/Lease，双 Worker fencing、重试、取消、接管、恢复 | 实际双 Worker 重叠、租约续期及恢复检查通过 |
| 未校验/摘要不符/失败不能成功；备份不可用可阻止后续导入提交 | 内部 prerequisite 门禁、损坏摘要及伪造完成状态验证通过 |
| 实际权限边界，普通运营者不能读取跨租户内容、路径或下载全实例备份 | 独立 backups.write、最近认证、归属隔离与 0700 目录验证通过 |
| 受影响验收、完整 make ci、报告/队列与独立提交 | 受影响复验及 ci-003 完整通过，报告/队列/证据随独立提交交付 |

本包不完成全部运维页面，也不注册尚无 handler 的未来 Operation 类型。全部验收和独立提交后才启动 W05。

## 实施与开发验证

- ADR-0048 已先行接受；新增 0021 增量迁移，0001–0020 不变。Core 0.14.0 / Schema 21，业务 Contract 1.11.0 和 SDK 方法不变；Console 1.1.0 候选增加 backups.write、POST /console/v1/backups 与 trusted_backup 类型。
- 共用 Operation 元数据、Forget 明细、备份明细分表；原 Forget 预览唯一性、500 根/问题限制、Hold/水位/固定快照和历史保留。载荷与进度 CAS 使用局部 savepoint 保证同事务回滚。
- 真实 CommandActor 来自当前 Operator Key/Session，接受与提交均复核权限；备份接入已有 Outbox/Worker/Lease，按作业 ID/代次隔离内部目录，真实 SQLite/Artifact 快照和完整校验后才能登记摘要及内部引用。Console 不返回引用或路径；后续导入可复用 prerequisite 实时校验门禁。
- 新备份 16 项服务/HTTP/恢复用例通过（backup-003）；原 Forget 27 项回归通过（forget-regression-001）；迁移 140 项通过（migrations-002），包括真实 Schema20 历史备份、恢复、离线门禁和 Schema21 升级。首次失败批次保留原始日志，不计为通过。
- Python 类型检查通过。公共接口快照逐项审查：新增创建备份路由/请求、Operation 枚举及权限枚举、Core 版本；无业务 SDK 方法变化。Console 页面已接入创建、重新认证、进度和取消，新真实浏览器定向验收已通过，完整 CI 中再次验证。

组合回归 affected-002 记录 1846 passed、13 failed、54 setup errors。其中 8 个失败及 54 个 setup errors 为沙箱禁止本地端口；另有历史升级 fixture、Schema/版本真源与已实现作业注册清单需要同步。修复后 fixes-001 在允许本地端口的环境中完成 570 passed。此失败批次不计为通过，不掩盖首次失败。

browser-001 使用实际 Chrome，页面创建→最近重新认证→202 queued→独立 Worker→completed→刷新，1 passed；Console 29 项单元、类型、lint 与构建通过。lease-final-001 的备份及 Outbox 60 项通过，补充真实续租跨原始 30 秒有效期、两个实际 Worker 重叠接管、类型载荷 CAS 回滚、受限备份运营者和数据库伪造成功状态拒绝。原始已结束批次见 [证据索引](evidence/w04/completed-batches.json)，旧 20 个 Migration 的哈希见 [基线核对](evidence/w04/migration-baseline-audit.json)。

首轮完整 `make ci`（ci-001）于 2026-09-07T18:34:12Z 开始、18:54:41Z 结束，exit 2：14,789 passed、4 failed，覆盖率 85.59%；性能阶段 11 passed / 41.32s。失败来自三个测试文件中遗漏同步的 Schema 21 迁移列表和新作业注册清单，尚未执行后续 SDK/浏览器/安装阶段。该批次不计为最终通过。本包仍 In progress，不启动 W05。

收尾契约审查发现，实际问题页的 `backup_unavailable` 未列入公开问题码，通用 `input_index` 还保留了 Forget 的 499 上限。新增实际 HTTP 响应校验在 problems-red-001 中明确复现两项失败；真源已补齐问题码并移除通用序号上限，Forget 自身 500 根及问题序号限制继续由其类型约束保护。同步生成契约、fixtures、TS 类型并审查公共接口快照，变动仅为 ConsoleOperationProblem。该修复发生在 ci-001 启动后，因此 ci-001 无论结果如何均不能作为本包最终候选验收；待其结束后须对修正候选重新运行完整 make ci。

问题页修正后 problems-green-001 的 431 项实际 HTTP 与契约用例全部通过。首轮 CI 发现的迁移列表/已实现处理器清单断言修正后，ci-fixes-002 的 83 项全部通过。修正候选完整 CI 已作为 ci-002 后台启动，session 9168、supervisor PID 46599，原生完成监听 cell 331；当前等待实际终态结果，尚未关闭本包。

第二轮完整 CI（ci-002）于 2026-09-07T19:20:26Z 结束，exit 2：14,795 项功能与 11 项性能测试通过，浏览器 20 passed / 1 failed，后续安装产物门禁未执行。失败页面明确显示实际登录 HTTP 429 和 Retry-After：新增备份浏览器用例使同一来源的登录预算更早耗尽，Task 用例未处理该响应。已为该用例及新增备份用例加入有上限的一次重试，等待真实 UI 按服务端冷却时间重新启用按钮；不改变生产限流。整组真实浏览器 browser-002 正在后台复验（session 81352，cell 346），通过后对修正候选重新执行完整 make ci。

browser-002 仍为 20 passed / 1 failed：首次重试辅助方法错误地把密钥清空后禁用的按钮当成冷却指示。实际 UI 每次提交后都会清空密钥，并不维护冷却倒计时；因此改为测试后台按真实 Retry-After 作一次有界延迟、重新填入密钥并重试，无密集轮询或生产阈值调整。browser-003（session 71563、cell 357）已启动。package-001 的全新 Core/SDK wheel/sdist、归档边界及独立 Schema21 HTTP/Worker/SDK/Recall 安装验收通过，摘要已保存在原始日志；最终完整 CI 仍须通过。

browser-003 记录 20 passed / 1 failed：按首次 Retry-After 等待后，服务端仍因独立密钥窗口尚未到期返回第二次 429（19 秒）。实际 consume_attempt 保持各自固定窗口；测试辅助方法现最多重试两次，每次遵守真实响应和 60 秒上限，不修改认证实现。browser-004（session 79885、cell 362）复验中，失败批次均已归档。

browser-004 已通过：21 项真实浏览器用例、29 项 Console 单元及类型/lint/build 全部通过。包括触发两个真实登录限流窗口后完成 Task 生命周期与 Forget，以及新增备份真实流程。修正候选最终完整 make ci 已启动为 ci-003，session 93121、supervisor PID 58721、完成监听 cell 365。当前仍 In progress，等待最终完整验收，W05 未启动。

## 退出条件与可核验依据

以下对应关系基于实际测试断言核对；定向结果不代替仍在运行的 ci-003。

| W04 要求 | 实际断言与证据 |
| --- | --- |
| 增量迁移保留旧 Forget 历史及限制 | [Schema20 迁移测试](../../tests/integration/migrations/test_typed_operations_migration.py)创建真实旧库与备份，对升级/恢复后的完整 Operation 值逐项相等比较，保留问题序号 499，并拒绝总数 501、问题序号 500、错误预览摘要及任意 kind；核对前 20 个 SQL 的字节摘要。migrations-002 通过。 |
| 类型专属授权与内部文件保管 | [备份测试](../../tests/integration/console/test_console_backup_operations.py)使用仅 backups.write 的受限运营密钥，拒绝读取另一创建者的 Operation，验证公开 result_ref 为 null、内部目录无 group/other 权限；真实 HTTP 创建和幂等回放通过。 |
| 真实 Worker 接管和租约保护 | 同文件的 `test_two_actual_workers_overlap_preparation_and_only_takeover_can_publish` 使用两个 OutboxWorker、真实归档和事件屏障，断言旧 Worker fenced、只有接管者结果可用；续租测试通过真实 heartbeat 越过初始租约期限。lease-final-001 通过。 |
| 当前授权与取消阻止晚到提交 | 准备完成后分别改变权限、epoch、会话有效期、吊销状态和最近认证有效期，断言 blocked 且无可信引用；准备前/后取消均不允许晚到结果改变取消终态。 |
| 重试与失败不得生成成功凭证 | 注入 OSError 后走既有 retryable 状态，恢复真实归档后成功；未校验适配器进入 dead/failed，损坏 manifest 或登记摘要时 prerequisite 拒绝；伪造 completed 数据库不变量被可信归档验证拒绝。 |
| 新类型不继承 Forget 的 500 上限 | 备份总数 1,000,000 及问题序号 9000 通过存储与实际 HTTP 问题页契约校验；原 Forget 上限由其类型约束保留。problems-green-001 的 431 项通过。 |
| 恢复策略与后续导入前置条件 | 完成及排队中的备份经真实恢复后均清空引用、当前作业和进度，标记 restore_requires_review；重复恢复仍保持该边界。不可用、未校验、跨租户及损坏结果均由内部 prerequisite 拒绝。本包未注册导入处理器。 |
| 浏览器、安装及完整收尾 | browser-004 的 21 项真实浏览器及 package-001 的独立 Schema21 HTTP/Worker/SDK/Recall 通过；原始日志和摘要见[批次索引](evidence/w04/completed-batches.json)。最终 ci-003 全部通过，候选与打包输入摘要一致。 |

## 最终验收与交接

最终批次为 `ci-003`：2026-09-07T19:30:07Z 开始，19:52:37Z 完成，exit 0。命令为 `CI=1 CONSOLE_BROWSER_EXECUTABLE="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" make ci`，由后台验证器保存任务、候选文件摘要及原始日志。上文各轮 In progress 与失败描述是开发过程记录，不替代本节的最终结果。

| 完整 CI 门禁 | 结果 |
| --- | --- |
| 格式、lint、类型、导入边界、文档、契约生成/兼容与公开接口快照 | 全部通过 |
| 性能 | 11 passed / 41.08s；Graph+Profile Recall p95 30.1ms（预算 250ms），PersonaCurrent p95 1.47ms（预算 20ms） |
| 功能及原有 SDK Python 测试 | 14,795 passed / 1181.16s；行/分支联合覆盖率 85.58%，阈值 80% |
| TypeScript SDK | 19 passed |
| Console | 29 单元测试、生成类型/lint/typecheck/生产构建通过；21 项真实浏览器用例通过 |
| 新构建与安装消费 | Core/SDK 各 wheel 和 sdist；归档边界、独立环境 HTTP/SDK、Required Lease、Worker once 和四类投影重建通过；Graph/Vector 真实 Recall 命中 |

环境：macOS 26.6.2 ARM64、Python 3.12.13、SQLite 3.50.4、Node 26.8.1、本机 Chrome。安装检查明确使用 development_sqlite_override；独立 Recall 的 deterministic Embedding 明确为开发配置。以上不构成 W05 真实生产 Provider 出站证明，也不构成 W18/W19 的生产 Soak/恢复发布验收。

[最终候选核对](evidence/w04/final-candidate-audit.json)确认 1,283 个非 docs 候选文件没有变化，[216 个打包输入](evidence/w04/package-inputs.json)摘要一致。[四个实际产物摘要](evidence/w04/artifact-digests.json)来自最终 CI 的全新构建输出；Core 0.14.0 / Schema 21 / 业务 Contract 1.11.0 / Console Contract 1.1.0，Python SDK 0.11.1、TS SDK 0.11.2，无 SDK 方法变更和发布上传。

升级须先停止 API/Worker，创建并校验当前 Schema20 备份，再按 ADR-0048 以离线及已备份门禁执行 0021 迁移；空库使用已声明的 bootstrap 例外。回退恢复升级前备份并使用 Core 0.13.0，不执行原地降级。Schema21 恢复会使备份 Operation 的外部引用和待执行意图失效，Forget 继续遵守已提交删除前缀核对。取消可能留下系统内部未引用归档；不提供备份路径、跨租户内容或 Web 下载/恢复能力。

独立完成 Commit：本包提交后由紧接的 W05 检查点登记实际哈希，避免自引用。W04 已完成自身最小前置范围，W05 承接 Embedding Provider 全链路；13.11 的其余运维动作由 W14 承接。Phase 11/12 保持 Deferred，Phase 13/14 不因此标为整体完成。
