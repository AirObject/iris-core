# Phase 1–10、13–14 完成情况核查

核查日期：2026-09-07。代码基线：`e2a6bbd7793f7bd4b73a51d32149eb8e3015ed33`，开始时工作区干净。Core 0.13.0、Schema 20、业务 Contract 1.10.0、Console Contract 1.1.0。本次仅核查并记录结果，不修改业务实现，不启动后续工作包。Phase 11/12 保持 Deferred，不列入本次完成度分母。

核查依据为阶段计划、原量化退出目标、最新验证记录、当前代码及定向实测。历史 Completed 表示历史交付快照；本报告分别判断领域实现、实际服务接线和验收证据，不把三者合并为一个百分比。

## 1. 逐阶段结论

| Phase | 核查结论 | 已交付与当前限制 |
| --- | --- | --- |
| 1 持久化、身份、Scope | 开发基线已完成 | SQLite/UoW、Scope/Privacy、身份绑定双视图、修订/幂等、备份恢复均有实现和历史退出证据，本次身份及三轮恢复抽查通过。生产 SQLite、密钥轮换、规模 RTO 由 Phase 14 承接。 |
| 2 Observation、Outbox、Scheduler | 开发基线已完成 | Observation/Outbox 原子性、Lease/Epoch/Fencing、背压、持久调度已实现，本次 Observation/Outbox 和 Required Surface 回归通过。生产进程故障与多客户端装配证明仍属 Phase 14。 |
| 3 Recent、State、Focus | 开发基线已完成 | 短期上下文、State 修订/TTL/合并写、Focus 生命周期与恢复已实现；旧报告中的 promotion seam 已补为实际创建 Note/Task/Episode/Claim，本次回归通过。 |
| 4 Note、Task、Event | 开发基线已完成 | Note、Task/Step/Dependency/Trigger、事件投递/ACK/重试均已实现。旧 Focus/Recall Required Lease 旁路已由后续实现关闭。 |
| 5 长期记忆、Episode | 开发基线已完成 | Claim 记忆/纠正、Episode/Relation/Artifact、Forget/Tombstone、删除账本与恢复保护有实质实现和历史回归证据。不能据此代替生产规模删除/恢复验收。 |
| 6 FTS Recall | 开发基线已完成 | FTS、Canonical 最终复核、预算/截止时间、Usage/幂等已经交付并接入 HTTP。当前性能复测通过；生产负载稳定性仍待验收。 |
| 7 Vector Recall | 领域实现完成，HTTP 集成未闭环 | FAISS、Provider Port/HTTP Embedding 适配器、Generation 生命周期及 VectorRoute 存在，但默认 HTTP Recall 未装配 VectorRoute；CLI Worker 使用确定性 Embedding。详见发现 A。 |
| 8 Profile、Graph | 领域实现完成，Graph HTTP 集成与量化补证未闭环 | Profile 与有界 Graph 投影/图召回已实现，HTTP 已接入 Profile 和直接 RelationRoute，但没有 GraphRoute；部分规模/竞争验收证据弱于原目标。 |
| 9 Persona | 主功能完成，原量化验收有欠项 | Revision/State/Policy/Proposal、发布/回滚已实现；每项 Policy 200 固定种子性质案例、TTL 时钟回拨等未找到完整证据。宿主重连/采用验收随 Phase 11/12 暂缓；通用 Core 证据仍须补齐。 |
| 10 巩固、Reflection、HTTP | 框架与传输层完成，运行能力未闭环 | 固定 Watermark 流水线、Provider 治理、ASGI、认证、serve/worker 已实现；默认认知 Provider 是 fake，且 HTTP 未装配 Phase 7/8 两条 Recall 路由。生产真实进程生命周期/慢消费者等另待 Phase 14。 |
| 13 Web Console | 部分完成 | 骨架/认证/读面及大量管理写入已交付；Draft、Reflection/Candidate 管理、Retention/Hold、统计、导出导入、Provider、Settings 和剩余运维未完成。详见下表。 |
| 14 硬化、发布 | 部分完成，整体暂停 | 打包资源、公开接口检查、安装消费、Required Lease、统一开发 CI 和受控发布工作流已具备；生产装配、安全、24h Soak、三轮升级恢复回退及冻结 RC 发布验收未完成。 |

Phase 1–6 的判断结合当前代码抽查与历史证据，并非本次重新执行了这些阶段的全部故障与规模验收。历史证据见 [Phase 1](phase-01-verification.md)、[Phase 2](phase-02-verification.md)、[Phase 3](phase-03-verification.md)、[Phase 4](phase-04-verification.md)、[Phase 5](phase-05-verification.md)、[Phase 6](phase-06-verification.md)。

## 2. 关键发现

### A. HTTP 声明支持 Vector/Graph，但实际上没有执行这两条路由

当前 `src/iris_memory_core/api/app.py:523–535` 创建 `StructuredRecallOrchestrator` 时只传入 FTS 和 Profile，没有传入 `vector`、`graph`。`application/recall.py:1884–1887` 仅在相应实例非空时注册 VectorRoute/GraphRoute。`create_app` 同样没有这两项配置注入入口。

与此同时，`api/app.py:1636–1654` 从契约静态读取能力列表；`contracts/source/contracts.json` 包含 `recall.vector.v1` 和 `recall.graph.v1`。因此这不只是生产 Provider 尚未配置，而是当前服务承诺与实际执行路径不一致。

本次用全新临时数据库、真实应用凭据和真实 ASGI 应用复现；未替换 HTTP 路由或认证。先重建已装配的 FTS/Profile，排除冷投影降级的干扰：

```json
{
  "capabilities_http_status": 200,
  "advertised": ["recall.vector.v1", "recall.graph.v1"],
  "recall_http_status": 200,
  "completed_routes": ["tasks", "recent_context", "state", "focus", "claims", "relations", "fts", "profile"],
  "degraded_routes": [],
  "partial": false
}
```

Trace 中也没有 vector/graph。调用方会看到完整响应，却无法获知所声明的两路能力根本没有执行。该缺口应纳入 Phase 7/8→10/14.0 的集成闭环：完成实际接线与端到端召回验证，或收紧能力声明并显式报告不可用。当前领域测试通过不足以关闭该问题。

本地复现脚本及原始输出位于 `/tmp/iris-phase-audit-20260907/probe_recall_composition.py`、`recall-composition-result.json`，执行命令为：

```sh
PYTHONPATH=.:src:sdk/python/src .venv/bin/python /tmp/iris-phase-audit-20260907/probe_recall_composition.py
```

以上临时文件不是长期发布附件；本报告保留关键结果和源码定位。

### B. 默认 Worker 没有真实认知模型与语义向量运行接线

`src/iris_memory_core/runtime.py:241–247` 直接创建 `DeterministicCognitiveProvider()`；`providers/cognitive.py:486–523` 明确其为 fake，默认提取候选为空，默认摘要标题/正文为空。`runtime.py:256–262` 使用 `deterministic-local` 32 维确定性 Embedding。

真实 HTTP Embedding 适配器已经存在于 `providers/embedding.py`，不能说整个项目没有适配器；缺的是受支持的运行配置、API/Worker 一致接线和实际能力/质量验收。Phase 10 已交付流水线与治理机制，但不能据此宣称默认服务已具备真实自动知识提取和语义检索效果。[Phase 14 的生产装配计划](../development/phase-14-hardening-release.md#142-容器生产装配与可观测性)明确承接此项。

### C. Persona 的 Completed 不代表原量化目标全部满足

[Phase 9 原阶段验收目标](phase-09-verification.md#原阶段验收目标)要求各类 Policy/证据限制每项至少 200 个固定种子案例，以及 TTL 前跳/回拨、暂停/重启和相同时钟轨迹重复验证。

当前 `tests/unit/test_persona_domain.py:20–73` 的四组 200 案例主要覆盖 Core 注入、State 值域、TTL 数值边界与 Hash/Magnitude；`tests/integration/cognition/test_persona.py:508–595` 的 Policy 约束测试没有覆盖成上述每项 200 的性质矩阵。后续 Console Policy、State、Proposal 的 HTTP/恢复测试补强了管理能力，但不能代替这些原定矩阵。`test_console_persona_state_recovery.py:59` 的前跳测试也不能作为回拨证明。

[Phase 9 已知限制](phase-09-verification.md#5-已知限制)明确记录回拨会延迟过期且当时未单测，Phase 14 仍要求补证。宿主通知丢失/重连/采用/回滚链路随适配器延期，保留为 Deferred，不将其算作已通过，也不在本次重新开启。

### D. Graph 部分原量化目标尚需更直接的证据

`tests/integration/recall/test_graph_profile_graph.py:450` 的恶意图为深度 2、扇出 160、约 321 节点；满足扇出 10 倍条件，但未覆盖深度 2 与节点 64 上限各 10 倍的原规模目标。该文件 `487–530` 的部分节点/深度检查由测试内复制的 BFS 自行约束；真实 Recall 调用主要断言候选数，因此不能把全部图预算性质都认定为已通过真实执行路径验收。

同文件 `980` 的 Redirect 变更为单次顺序场景，`test_graph_profile_jobs.py:109` 的 SpaceGroup 场景主要验证入队，也不足以独立证明原要求的每类 50 次查询竞态。此项是验收覆盖不足，不直接推断生产 Graph 算法存在越界或竞态缺陷。

## 3. Phase 13 的实际切片进度

| 工作包 | 已交付 | 剩余 |
| --- | --- | --- |
| 13.1 / 13.2 | 独立契约/子应用、默认关闭、密钥、会话、刷新/reauth、CSRF、应用凭据管理 | 生产 HTTPS/可信反代、凭据真实交叠轮换与恢复验收 |
| 13.3 | 15 类资源读面、lookup、Task 子资源、Persona、不透明 ID 修复、生成类型及真实读面 | 生产权限/容量验证 |
| 13.4 | Observation、State、Note、Focus、Task/Step/Dependency/Trigger、Claim/Episode/Relation/Artifact、身份、Event dismiss、Persona 发布/回滚/State/Proposal/Policy | Persona Draft、Reflection/Candidate 的 dry-run/replay/review/reject、投影管理动作 |
| 13.5 | 10 类目标的删除预览/提交、至多 500 条预览/50 条事务、批量 Operation/取消 | Retention/Hold 管理、异步清理状态查询与相关完整链路 |
| 13.6 | 前端设计/mock | 八个统计面、注册表、rollup/回填、耗时采集及真实后端 |
| 13.7 / 13.8 | 前端设计/mock | 导出/下载、导入 staging/审核/备份/提交/去重/断点/补偿 |
| 13.9 / 13.10 | 前端设计/mock | Provider 配置/探测/激活及真实运行接线；Settings 注册表、修订与行为生效 |
| 13.11 | `memory_forget` Operation 列表/详情/问题/取消与 Worker 执行 | 任务/DLQ、重建/备份、只读审计、完整探针和运行手册 |

当前 Console OpenAPI 为 **125 路径、152 操作**。`api/console/app.py:166–180` 的 bootstrap 只发布 keys/service_credentials/memory/personas/operations；统计、导入导出、Provider、Settings 等尚无正式 Console 路由。Core 已有的 `/v1/admin` 运维能力不能替代这些 Console 工作包。

本轮浏览器 **20 passed**，其中 **17 条为真实后端流程，3 条为 mock API 流程**。`web/console/tests/browser/console.spec.ts:424、492、536` 的统计/窄屏展示、导入、Provider/Settings 用例截获 API；这些证明前端行为，不能证明对应后端已经完成。真实用例采用临时数据库、生产前端构建和 Required 模式。

## 4. Phase 14 的实际进度

| 工作包 | 当前判断 |
| --- | --- |
| 14.0 前置闭环 | 部分完成：ID/读面/Required Lease/接口快照已修复；Phase 13 剩余能力、上述 Recall 集成缺口和完整兼容/权限矩阵未闭环。 |
| 14.1 打包、CI | 工程主干已落地，本轮全新 Core sdist/wheel、独立 SDK、隔离安装和公开接口 Smoke 通过；Console 已进入 `make ci`。安装仍带 `--allow-local-sqlite`，不满足全部生产/无模拟运行的退出条件。 |
| 14.2 生产装配 | 未完成：未发现 Docker/Compose 交付；生产 SQLite Allowlist、真实 Provider、独立 OS 身份/目录隔离、多进程 SSE/生命周期证据缺失。 |
| 14.3 安全、供应链 | 未完成：安全代码和 SECURITY 文件存在，但完整扫描/SBOM、静态加密、凭据/密钥轮换与恢复拒绝矩阵尚未验收。 |
| 14.4 性能、容量、故障 | 开发环境 11 项性能测试本轮通过；固定生产硬件/规模、24h Soak、每进程边界至少 20 次 SIGKILL 尚未执行。 |
| 14.5 恢复、升级、回退 | 有单元/集成备份恢复及删除账本基础，缺三轮生产规模完整演练及冻结的 RPO/RTO 证据。 |
| 14.6 发布 | 发布工作流已新增，默认不上传；同一冻结 RC 的全部验收、不可变 Manifest、外部 PyPI 配置和发布后安装回读未完成。 |

依据：[Phase 14 退出门禁](../development/phase-14-hardening-release.md#退出门禁)、[发布流程说明](../operations/publishing.md)、`.github/workflows/publish.yml:47–62`。发布工作流与安装门禁仍明确使用开发 SQLite Override；存在发布 YAML 不等于已完成生产发布。

## 5. 文档漂移与待办漏项

1. `docs/development/README.md:27` 和 Phase 13 首页/需求表只描述较早期的 State/Note/Focus/Task 进度，落后于实际管理写入、Forget、Event 和 Persona 实现。
2. `docs/development/phase-13-web-console.md:63` 仍写运行时 Schema 15，首页及真源已为 Schema 20；部分退出框仍保留已修复的 ID/类型失败。
3. `docs/reports/phase-13-verification.md:129` 仍称 `make ci` 不包含 Console；当前 `Makefile:53–59` 已纳入完整 check/browser。
4. `docs/development/work-packages.md:7` 仍称最新全量为 11680 passed / 1 failed，但 Phase 14 报告末尾已有 11681 全过；随后[工程复验报告 §6.2](structure-and-release-readiness-2026-09-07.md#62-本次验证)还记录功能 11676 + 性能 11 项分段通过。旧失败应保留为历史，不能继续当作当前最新状态。
5. `web/console/INTEGRATION_MATRIX.md:28` 中部分 Persona 状态也落后于后续验收。浏览器总数须注明 17 真后端 + 3 mock，避免进一步高估。
6. 工作队列将 13.4 剩余项只列为 Persona Draft；设计 `docs/design/console-backend.md:261` 仍要求 Reflection/Candidate 管理动作，当前只有读路由。应明确承接这些遗漏，或正式调整范围。
7. Phase 3 的旧 Focus promotion seam、Phase 4 的旧 Focus/Recall Lease 旁路已关闭，不应重复登记为当前缺口。挑战码在 Phase 1 是建议/未来流程，也不属于当期必需退出项。

本次只新增核查报告，未改写历史报告或阶段状态；以上是需要随后同步的文档项。

## 6. 本次实测与边界

| 检查 | 结果 |
| --- | --- |
| Python unit + contract（排除网络 mock）+ Python SDK + migrations + release_surface + release_packaging | **9603 passed，24.22s**；`--no-cov`，2 条第三方弃用警告 |
| HTTP mock 契约/SDK | **54 passed，27.35s**；首次沙箱禁止回环 bind，允许监听后同组全过 |
| Phase 1–4 定向集成 | **236 passed，32.69s**；与上述 release_surface 有重叠，不能直接累加为唯一用例数 |
| 静态/类型/生成/公开接口 | Ruff 367 文件、mypy 364 文件、文档/计数/导入边界、TS 类型、契约生成/兼容、公开接口检查全部通过 |
| TypeScript SDK | **19 passed** |
| Console check | **29 passed**；生成类型/lint/typecheck/生产 build 全过 |
| Console 浏览器 | **20 passed，1.3m**；17 真实后端 + 3 mock API；使用本机 Chrome，`CI=1` 禁用旧服务复用 |
| 独立性能阶段 | **11 passed，45.28s**；无 coverage；Structured p95 32.68ms、FTS p95 35.74ms，保持原门槛 |
| 全新构建/隔离安装 | **通过**；Schema 20，Required SDK/Worker Smoke 通过，4 个私有路径拒绝通过，SIGTERM 排空约 0.126s；使用开发 SQLite Override |
| HTTP Recall 接线探针 | **复现能力声明与运行路由不一致**，详见发现 A |

主检查命令：

```sh
.venv/bin/python -m pytest tests/unit tests/contract sdk/python/tests \
  tests/integration/runtime/test_release_surface.py \
  tests/integration/recovery/test_release_packaging.py tests/integration/migrations \
  --ignore=tests/contract/test_mock_server.py --no-cov -q -p no:cacheprovider
.venv/bin/python -m pytest tests/contract/test_mock_server.py --no-cov -q -p no:cacheprovider
make format-check lint typecheck contracts-check public-api-check sdk-test
npm run check --prefix web/console
CI=1 CONSOLE_BROWSER_EXECUTABLE='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome' \
  npm run test:browser --prefix web/console
make package-check
make test-performance
```

本轮没有重新运行完整功能回归/覆盖率、完整故障矩阵、GitHub CI、生产部署、安全扫描、Soak、发布或三轮生产恢复验收。报告中引用的 11676 功能通过、85.28% 覆盖率属于既有工程复验记录，不能标作本轮结果。没有从测试通过率推导阶段完成百分比。

根审计的临时日志在 `/tmp/iris-phase-audit-20260907/`，包括 `python-checks.log`、`static-sdk-checks.log`、`mock-http.log`、`mock-http-local.log`、`package-check.log`、`performance.log`。它们清理后会丢失，本报告保留主要结果与复现入口。

处理顺序建议：首先关闭或准确降级 HTTP Vector/Graph 能力声明；随后按独立工作包补 Provider 运行接线及 Persona 欠缺验收，再继续 Phase 13 已规划切片并补齐队列漏项；最后以同一冻结候选执行 Phase 14 生产门禁。阶段摘要与最新证据应同步维护，历史通过和历史失败都保留准确日期与候选范围。
