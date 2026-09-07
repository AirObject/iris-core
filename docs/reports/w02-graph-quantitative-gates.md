# W02 Graph 原量化门禁

状态：Completed，完整收尾验收通过。范围：Phase 8、14.0-D；Phase 11/12 保持 Deferred。

## 开工基线

- 开始 Commit：`095925932ce789ffeba8126a3f96604afbf60caf`；W01 完整 CI 已通过，报告见 [W01](w01-http-recall-assembly.md)。开工时工作区干净。
- Core 0.13.0、Schema 20、业务 Contract 1.11.0、Python SDK 0.11.1、TS SDK 0.11.2。
- 复用已接受 ADR-0016 的预算、逐边授权、新鲜 Rehydrate 约定，以及后续修复；不改写旧 Migration，不为验收引入缓存或复制 BFS。现有 RecallService 会持久化幂等请求响应，本包直接验证该响应的重放终检。
- 用户已取消定时 automation；长任务后台运行并保存进程/会话、日志、摘要与检查点，按真实完成事件续作。

## 必需门禁

| 原要求 | 当前状态 |
| --- | --- |
| 分别至少 10 倍深度、扇出、节点的对抗图，真实路由读取/扩展/返回有界 | `budgets-004` 12 项通过；深度 20、扇出 160、节点 80 的独立压力图 |
| 真实 Deadline 后零继续扩展 | `budgets-004` 通过；覆盖真实读取、隐私、Canonical admissibility、候选构造期间到期 |
| Binding、Redirect、SpaceGroup、Correct、Forget 查询竞态每类至少 50 次 | `affected-003` 中 250 项通过，包含重建后重放及 Correct/Forget/Redirect 的独立 Profile 读取 |
| 提交后旧字段/边/请求缓存返回为零，准确降级/回退原因 | `affected-003` 全通过；pending、deadline、运行异常分别为精确 `graph_rebuild_pending`、`route_deadline_exceeded`、`route_failed`，Canonical Claim 继续返回 |
| 保留至少 200 案例的授权、状态、时间与墓碑性质测试 | 原 Scope/Privacy/Status/Valid Time/Tombstone 各 200 案例全部保留并在 `affected-003` 通过 |
| 受影响验证、完整 make ci、证据/队列/阶段摘要及独立提交 | `affected-003` 456 passed，`ci-001` 完整通过；证据/队列/阶段摘要与本包独立提交一同交付 |

此包完成后才启动 W03。

## 实现与回归证明

`GraphExecutionProbe` 观察真实 `GraphRoute.collect` 执行帧中的 visited、depth、fanout、candidate 状态，并包装实际 SQLite `edges_for_source` 记录调用次数、读取窗口和返回行数；不实现另一份 BFS。各压力图分别达到配置上限的至少 10 倍，且断言实际触及受压边界，避免空图或提前退出产生的虚假通过。HTTP 另外验证候选上限与 Trace 的计数一致。

`affected-003` 的实际计数如下；节点上限压力使用显式配置 8，其他两图使用默认 64，深度/每层扇出均为默认 2/16。读取窗口始终为每节点 64 行，读取次数与访问节点数分别受断言约束。

| 压力 | 输入 | 实际访问节点 | Repository 读取次数 | 返回候选 | 最大循环深度 |
| --- | --- | --- | --- | --- | --- |
| 深度至少 10 倍 | 长度 20 的链 | 3 | 2 | 2 | 2 |
| 扇出至少 10 倍 | 根节点 160 条边 | 17 | 17（包含第二层空邻接读取） | 16 | 2 |
| 节点至少 10 倍 | 80 个目标节点，上限 8 | 8 | 1 | 7 | 2 |

HTTP 160 扇出图另以候选上限 5 验证：真实路由与响应 Trace 都为 5，访问 6 个节点。`TestPerEdgeProperties` 的 Scope、Privacy、Status、Valid Time、Tombstone 五项各 200 个固定种子案例原样保留，未以新增 HTTP 案例替代或减少。

Deadline 测试先执行实际数据库/过滤工作，再让受控单调时钟到期。原实现可在隐私或 Canonical 检查、候选构造耗尽时间后继续扩展/返回。本包在这些步骤后立即检查 Deadline；每层扇出耗尽后也停止读取其余 frontier 节点。

竞态测试先通过真实服务写入带唯一旧值标记的 relationship Claim，并建立 Graph/Profile 投影。HTTP 完成两条真实路由的候选收集后，以事件屏障暂停该请求；另一个线程提交 Binding 撤销、Entity 重定向、SpaceGroup 解绑、Correct 或 Forget，再释放请求。每类 50 个隔离案例，验证在途响应、已保存请求重放及重建后新请求，旧值均不得返回。

`races-003` 复现 Binding 撤销后 HTTP 在途响应仍返回旧主体 Claim 的漏洞。资源 Revision 的新鲜检查不能替代 Actor 权限检查；本包在响应持久化的同一写事务中重新解析外部 Actor，撤销返回 `identity_not_found`，当前主体改变返回 `conflict`。SpaceGroup 继续复用当前成员关系授权检查，Correct/Forget 继续复用新鲜 Rehydrate 和请求响应终检。没有改变契约、领域语义或迁移。

## 当前验证记录

- `budgets-001`、`races-001`、`affected-001` 是错误测试选择器导致的零执行，不能计入验收；均已改用仓库内实际路径/节点。
- `budgets-002`：9 passed / 3 failed，定位 Deadline 边界缺陷；`budgets-003`：11 passed / 1 failed，剩余是静态方法包装器绑定错误；`budgets-004`：12 passed。
- `races-002`：5 failed，测试错误地显式传入 ClaimService 不支持的 SpaceGroup 参数，改为真实服务 Scope 推导；`races-003`：3 passed / 2 failed，分别为真实 Actor 撤销漏洞和 Redirect 测试未读取绑定后的实际 Entity Revision。
- `races-004`：250 passed，56.23 秒。
- `static-001`：format、lint、文档链接/计数、Python/TS 类型、契约兼容与公共 API 快照均通过。
- `affected-002`：306 passed / 150 failed，142.65 秒。新增直接 Profile 读取未在测试凭据中授予 Entity，真实 API 返回 `access_denied`；`profile-diagnostic-001` 确认该原因后补齐显式 Entity 授权。
- `affected-003`：**456 passed**，149.80 秒；包含增强后的 250 次竞态、预算、HTTP 回退、原 Graph 性质/并发、W01 接线、Recall 发布与缓存重放回归。
- 完整 `make ci`（`ci-001`，session `58678`，supervisor `99656`）已通过：2026-09-07 16:41:20–17:00:51 UTC，退出 0。功能 **11966 passed**（1019.64 秒），联合覆盖率 **85.52%**；性能 11 passed（43.53 秒）；TS SDK 19、Console 单元 29、真实浏览器 20 全通过；Core/SDK wheel/sdist、新建隔离 venv 安装、公共接口检查及独立 API/Worker/SDK Recall Smoke 全通过。
- 已结束批次的原始日志、命令、退出状态和摘要见[证据清单](evidence/w02/completed-batches.json)；[基线审计](evidence/w02/baseline-audit.json)确认 20 个旧 SQL 字节不变、Contract/Schema/SDK 未改动。

## 收尾与交接

运行环境沿用 W01 已验证的本地配置：macOS arm64，Python 3.12.13、SQLite 3.50.4 显式本地例外，`CI=1` 与 `CONSOLE_BROWSER_EXECUTABLE=/Applications/Google Chrome.app/Contents/MacOS/Google Chrome`。这是开发集成验收，生产环境、真实出站 Provider 和稳定发布验收仍由后包承担。

[最终候选审计](evidence/w02/final-candidate-audit.json)确认 CI 启动后的代码/测试/配置没有漂移，差异仅为报告、证据与检查点；[打包输入](evidence/w02/package-inputs.json)与启动快照一致。[产物摘要](evidence/w02/artifact-digests.json)记录本次新构建四个安装物，未发布到外部仓库。全部原始失败均保留，只有 `ci-001` 是完整 CI 收尾结果。

开始 Commit 为 `0959259`；结束 Commit 为携带本报告与本包变更的独立提交，完整哈希由提交后的串行检查点记录，避免提交自引用。W03 仅在该提交完成后启动。
