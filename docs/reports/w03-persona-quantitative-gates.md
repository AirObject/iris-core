# W03 Persona 原量化门禁

状态：Completed，完整收尾验收通过。范围：Phase 9、14.0-D；Phase 11/12 保持 Deferred。

## 开工基线

- 开始 Commit：`4c6277b296cbf5bce46ca5e099532de753350991`，W02 完整 CI 和独立提交已完成，开工前工作区干净。
- Core 0.13.0、Schema 20、业务 Contract 1.11.0、Python SDK 0.11.1、TS SDK 0.11.2。
- 复用 Accepted ADR-0018 的 Policy/Proposal、原子发布和 State expiry fencing，不改变 UTC 回拨语义；若发现需要改变语义，先记录决策。
- 定时 automation 已删除；后台验证保存真实会话、日志和结果，按任务完成结果续作。

## 必需门禁

| 原要求 | 状态 |
| --- | --- |
| Persona Current 读取 p95 ≤ 20ms，声明硬件、并发与缓存状态 | `ci-001` 已完成性能阶段：200 次暖缓存读取、并发 1，p95 1.89ms / median 1.35ms；完整 CI 已通过 |
| locked/manual/bounded_auto、allowlist、单次/累计幅度、Evidence 数量/多样性/时间窗、冷却期、Stale Base，每项至少 200 固定种子案例 | `policy-001` 2800 passed；`field-paths-001` 全字段增强 400 passed |
| 真实服务授权、CAS、事务桥接回归 | 既有测试原样保留，`regression-001` 142 passed |
| 50 客户端同 Base 发布恰好一成功，Current 与 Recall 顶层 Revision/Hash 一致 | `http-001` 通过：50 个独立凭据和 TestClient，1 个发布成功、49 个 `revision_mismatch`，50/50 读取一致 |
| TTL UTC/前跳/回拨/暂停/重启 Catch-up，连续三次相同轨迹逻辑修订和值一致；旧 expiry 不覆盖新 State | `clock-001` 通过，三次 11 步轨迹完全一致 |
| 完整 make ci、报告/队列/证据和独立提交 | `ci-001` 完整通过，报告/队列/证据与独立提交一同交付 |

真实网络断线/通知丢失与通用客户端恢复证明归 W16，宿主专属采用链继续 Deferred。W03 完成提交后才启动 W04。

## Policy 原目标映射

[真实服务性质测试](../../tests/integration/cognition/test_persona_policy_quantitative.py)为每项运行 200 个独立数据库案例，种子为 `903000 + case`。通过真实 PersonaService 创建 Policy/Proposal、审批和发布；Evidence 来自真实 TaskService 的两个不同 Canonical Task。幅度使用精确二进制分数，边界包含等号与一个最小步长外的拒绝；失败保留具体性质名、种子及稳定拒绝原因。没有调用内部 `_evaluate` 代替服务验收。

| 性质 | 案例数 | 实际断言 |
| --- | --- | --- |
| locked | 200 | 合法 Evidence 也不能创建提案，Current 不变 |
| manual | 200 | 创建后仅 Proposed，应用凭据审批拒绝，管理员审批后只推进一格 |
| bounded_auto | 200 | 满足约束直接 Published，Core 不变、Current 只推进一格 |
| 字段 allowlist | 200 | 固定种子选择 Trait/Narrative 字段，名单内允许、名单外精确拒绝 |
| 单次幅度 | 200 | 等于阈值允许，超过阈值 `1/1024` 拒绝 |
| 累计幅度 | 200 | 先真实发布消耗额度；第二次在累计边界内允许、边界外拒绝 |
| Evidence 数量 | 200 | 两条真实来源，在最低 2/3 条门槛下分别允许/拒绝 |
| Evidence 多样性 | 200 | 不同 Task 满足多样性，重复同源引用不能制造新来源 |
| Evidence 时间跨度 | 200 | Task 创建时间跨度等于门槛允许，少 1 微秒拒绝 |
| 冷却期 | 200 | 真实发布后，在冷却截止前 1 微秒拒绝，到点允许 |
| Stale Base | 200 | Current 被其他发布推进后，旧提案审批与旧 Base 创建都拒绝；有效 Base 可发布 |
| Confidence | 200 | 精确阈值允许，低一个步长拒绝 |
| 观察窗口 | 200 | 与 Evidence 时间跨度独立配置，精确边界允许/拒绝 |
| 敏感字段 | 200 | 命中敏感字段只能等待管理员审核，非敏感字段可自动发布 |

## HTTP 与时钟证明

[HTTP 并发测试](../../tests/integration/cognition/test_persona_http_concurrency.py)建立 50 个独立 management 凭据及 HTTP 客户端，以事件屏障同时提交同一个 Expected Revision。恰好 1 个 201、49 个 409，失败错误精确为 `revision_mismatch`；胜者幂等重放不再推进，历史仅 Bootstrap 和一个新 Revision。50 个客户端各自读取 Current 与真实 HTTP Recall，顶层 Revision/Hash 全部一致。

[时钟轨迹测试](../../tests/integration/cognition/test_persona_clock_trajectories.py)从 UTC 日界前开始，跨日、前跳、回拨、暂停 Worker、重启服务并 Catch-up、执行迟到任务以及回拨后的新 State 均调用真实存储与 Outbox handler。每次使用独立 Agent，三次轨迹的相对时间、逻辑 Revision、State 值与到期点完全一致。修订序列为 `1,2,2,2,2,2,3,3,3,4,5`；迟到任务不覆盖当前状态，已提交 baseline 不因时钟回拨复活旧 State。

ADR-0018 新增的验证澄清记录既有 UTC 壁钟语义：回拨会推迟尚未提交的过期处理，Current 读不执行过期写入；Catch-up 和 Worker 均按 ID/Revision 终检。没有改变到期规则或生产代码。

## 后台验证记录

- `policy-smoke-001`：120 秒到期，`-k '0 or 1'` 实际选择 1792 项，超过小批次预期；未完成，不计作通过。保留原日志，完整批次按实际规模使用 600 秒固定上限。
- `policy-001`：2800 passed，232.54 秒；之后将 allowlist/敏感字段从单字段扩为全部合法 Trait/Narrative 字段，由 `field-paths-001` 复验相关 400 项。
- `clock-001`：1 passed，0.23 秒，包含三次完整 11 步轨迹。
- `http-001`：1 passed，3.91 秒，50 个真实 ASGI 客户端；这不替代 W16 的独立网络进程和离线恢复要求。
- `static-001`：format、lint、文档、Python/TS 类型、契约一致性/兼容与公共 API 均通过；字段增强后新增三文件 mypy 再次通过。
- `field-paths-001`：400 passed，33.57 秒；`regression-001`：142 passed，33.72 秒，包含既有 Persona 应用服务、Console 授权/CAS/共享事务/恢复、迁移与领域性质。
- 已结束批次的命令、结果与原始日志见[证据清单](evidence/w03/completed-batches.json)；[基线审计](evidence/w03/baseline-audit.json)确认生产代码、Contract/Schema/SDK 不变，旧 SQL 字节不变。
- 收尾完整 `make ci`（`ci-001`，session `38934`，supervisor `15418`）已通过：2026-09-07 17:24:32–17:47:44 UTC，退出 0。功能 **14768 passed**（1242.43 秒），覆盖率 **85.55%**；性能 11、TS SDK 19、Console 单元 29、真实浏览器 20 全通过，Core/SDK 新构建及隔离安装、独立 API/Worker/SDK Recall Smoke 通过。

## 当前 CI 性能证据与交接边界

`ci-001` 的性能阶段已经结束：11 passed，42.49 秒。Persona Current 的原 20ms 门槛保留，200 次暖文件系统/SQLite 页缓存读取、并发 1，p95 **1.89ms**、median **1.35ms**；环境为本机 arm64/macOS、Python 3.12.13、SQLite 3.50.4 显式本地例外。该证据只证明本地开发性能门禁，不替代 W18 的固定生产环境容量/Soak。

本包三个新增测试文件、ADR 既有语义澄清和报告构成候选；生产代码、Migration、Contract 与 SDK 均无变更。完整 CI、候选与 211 个打包输入摘要已核对通过，本包完成后以独立提交交接，再创建 W04。


## 收尾证据

- [量化结果](evidence/w03/quantitative-results.json)从成功日志逐项核对 14×200 个唯一案例 ID、全字段增强 2×200 个案例、三次 11 步轨迹和 50 客户端结果。
- [最终候选审计](evidence/w03/final-candidate-audit.json)确认 CI 之后的差异仅为报告/证据/检查点；[打包输入](evidence/w03/package-inputs.json)全部与 CI 启动快照一致。
- [产物摘要](evidence/w03/artifact-digests.json)记录四个新构建安装物，其字节摘要全部与 W02 一致；本包没有生产行为、Schema 或公共契约变更。
- 开始 Commit `4c6277b`；结束 Commit 是携带本报告与 W03 变更的独立提交，完整哈希由提交后的串行检查点记录，避免提交自引用。

本轮开发集成验证不替代 W16/W18 的网络、生产部署与 Soak，也不改变 Phase 11/12 Deferred。
