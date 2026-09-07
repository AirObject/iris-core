# W01 HTTP Recall 接线与能力声明

状态：Completed。对应 Phase 7/8/10、14.0-D/F。此报告记录当前工作包，不代表后续包或稳定发布验收通过。

## 候选与范围

- 开始 Commit：`e2a6bbd7793f7bd4b73a51d32149eb8e3015ed33`；结束 Commit 为首次将本报告标记 Completed 的 W01 独立提交，准确哈希由提交后的串行检查点记录。
- 当前版本真源：Core 0.13.0、Schema 20、开工 Contract 1.10.0；当前候选 Contract 1.11.0，Core/Schema 不变，无新迁移。
- 开工已有未提交的构建指南、工作队列、阶段/报告索引及核查报告；保留原内容，以用户最新串行授权覆盖旧停止规则。
- 实施 API/Worker 共用的私有 Recall 装配接缝，默认接入 Graph；Vector 未配置时不声明支持，显式配置后保留路由，由原有投影/Provider 信任门负责故障降级。
- `IRIS_MEMORY_DEVELOPMENT_EMBEDDING=true` 显式启用开发 deterministic Provider；API/Worker 使用相同配置和索引根。W05 承接真实 Embedding 配置存储与出站验收。

## 决策、兼容与回退

沿用 ADR-0015/0016 的 Generation、空间信任门、Canonical 终检、降级语义；未改写旧 Migration。未配置 Vector 的 Worker 不领取 `vector.*`，已有任务保留 pending，由启用 Provider 后的 Worker 接管，不丢弃任务或伪造成功。需要监测积压，生产运行配置完整证明仍属 W05/W16。

[只读源审计](evidence/w01/source-audit.json)确认 20 个旧 SQL Migration 与开始 Commit 逐字节相同，版本清单的 source SHA-256 匹配当前契约。

装配类型保持 Core 私有，不加入公共 SDK 导出；未通过公共接口快照检查前不接受未知导出。关闭开发配置会撤掉 Vector 支持声明；既有向量文件和 Canonical 不删除。

## 验证记录

[验收环境清单](evidence/w01/environment.json)记录 macOS arm64、Python 3.12.13、SQLite 3.50.4、Node 26.8.1、工具/关键依赖版本及依赖锁摘要。安装 Smoke 显式允许本地 SQLite 版本；生产 allowlist 未改动。本机 CI 性能阶段独立使用 `--no-cov -s`，功能阶段保留 80% 联合覆盖率门槛；结果不替代 W16 生产运行组合或 W18 性能环境证明。

后台批次 `W01/affected-001`：supervisor PID `65288`，总时限 600 秒；命令：

```sh
make test-affected TESTS="tests/integration/runtime/test_http_recall_assembly.py tests/contract/test_http_operation_matrix.py tests/integration/runtime/test_release_surface.py"
```

任务元数据、候选差异、日志与原子结果位于 `.work-package-runs/W01/affected-001/`，结果：52 passed / 3 failed；新增请求遗漏必需 `schema_version`，原有 HTTP/Lease 项通过。收尾将把重要输出归档到报告证据目录；临时目录不能作为最终唯一证据。后台启动器为 `tools/background_validation.py`，每次使用新目录，固定时限，失败不自动重跑。

## 后续批次与接口审查

- `affected-002`：补齐请求后，5 passed，0.82 秒；Vector 来源与 Graph 两跳通过。
- `affected-003`：173 passed / 3 failed，23.99 秒；新增故障断言误用了内部字段 `reason`，已按冻结响应改用 `reason_code`。
- `affected-004`：53 passed，13.16 秒；含 18 项新增 ASGI 用例、Vector 并发/崩溃/隔离、Provider 截止时间与熔断。
- `static-001`：lint、文档、依赖边界通过，mypy 发现新增测试的 Optional Speaker 与 TestClient ASGI 类型窄化问题，已修正；未把失败记为通过。
- `static-002`：mypy 发现后台任务候选摘要字典的 Optional 值类型未声明，已修正；原日志已归档。
- `static-003`：全部静态门禁通过。`recall-regression-001`：555 passed，268.46 秒，覆盖全部 Recall 集成、W01 ASGI、Required Lease 与 HTTP operation matrix。
- `tcp-faults-001`：5 passed，2.72 秒；真实 loopback TCP Provider timeout/disconnect、允许/禁止部分结果、恢复后命中，以及 rebuild 失败后原 Generation 继续通过 HTTP 命中。
- `static-004`：369 文件 mypy、TS 类型、契约生成一致性/兼容与公共 API 快照全部通过。
- `installed-001`：原安装门禁通过，新增 SDK Relation 请求因 Evidence 缺少 relation/source_authority 被契约拒绝，已修正；后续复验和真实 Worker 缺陷见下节。

Contract 1.11.0 为 `/v1/negotiation` 增加可选 `required_capabilities` 严格字符串数组；未知、未授权或未配置的必需能力返回既有 `unsupported_version`。旧请求保持兼容。生成物来自 source。公共 API 候选已逐差异审查：仅该 operation 的契约摘要和 Contract 版本变化，Python/TS 导出、CLI 与 DTO 无变化；据此更新白名单，无未知导出被接受。

已完成批次的原始命令/结果/日志已归档到[证据索引](evidence/w01/README.md)；[批次与摘要清单](evidence/w01/completed-batches.json)记录文件 SHA-256，保留失败与复验，不依赖 `/tmp`。

## 后续修复与最终候选

- `installed-002`：新增真实 Worker Vector 重建超出 40 秒验收窗口。`installed-003` 诊断保留了失败合成库与 Job 状态，确认连续 `internal_error`，没有把重试算作成功。
- `affected-005`：17 passed / 1 failed，使用所有默认路由和 Note 的增强样本定位到 `_note_entry` 错读不存在的 `NoteCurrent.revision`。改为不可变 NoteRevision 的修订号，并给适配函数加真实领域类型。
- `affected-006`：54 passed，29.46 秒；含修复后的完整路由、Vector 文件和 id_map 回归。
- `installed-004`：真实 wheel/sdist 构建、归档边界、干净 venv 安装、公共 API 检查、原安装 Smoke 与新增独立 `serve`/`worker` + 公共 SDK 全部通过，11.59 秒。新样本不关闭其他路由，Vector Note 命中 1 条、两跳 Graph 2 条、Required Lease 及四类 Worker rebuild 完成。可信初始化仅配置身份/凭据；Note/Observation/Relation 写入、管理重建与 Recall 均经独立 SDK/真实 HTTP。使用显式开发 Embedding 与本地 SQLite 例外，不冒充 W05/W16 生产证明。
- 增强 ASGI 对照：同一 Note、无关键词重合、全部默认路由开启；配置 Vector 时返回，撤除 Vector 装配时不返回。
- 收紧能力协商：空能力集合也执行凭据交集，不能把空集解释为全部授权。`affected-007`：121 passed，12.83 秒，覆盖新增空/窄授权负例、HTTP 操作矩阵和 Required Lease。
- 沿用 ADR-0015 的实际就绪探针，将 `vector_required` 运行配置接到 HealthService：未配置/Provider 故障时必需部署 503，可选部署报告不可用；失败探测保留缓存/冷却。`affected-008`：100 passed，9.05 秒。`static-005`：lint、文档、mypy、TS、契约与公共 API 全部通过。
- `ci-001`：2026-09-07 15:35:19–15:51:05 UTC，退出 2。静态/契约门禁通过，性能 11 passed（39.69 秒），Python 功能 11705 passed（882.94 秒），联合覆盖率 85.48%，TS SDK 19 passed，Console 单元 29 passed、生产构建通过。20 项浏览器用例均在启动前因缺少项目预期的 Chromium headless shell 1187 失败；未执行最终 package-check，不能称完整 CI 通过。原始日志及结果已归档至 [ci-001](evidence/w01/ci-001/output.log)。
- 环境修复：按指南允许的本机浏览器路径，使用已安装 Google Chrome 和项目现有 `CONSOLE_BROWSER_EXECUTABLE` 开关；`browser-001` 后台执行 `env CI=1 CONSOLE_BROWSER_EXECUTABLE=… make console-browser`，PID 81778、时限 600 秒。该批次已通过：20 项真实浏览器用例（1.3 分钟）、29 项 Console 单元和生产构建通过；使用同一环境启动 `ci-002` 全量收尾复验。

[首轮性能记录](evidence/w01/ci-001-performance.json)逐项保存样本数和实际输出：Graph/Profile Hybrid p95 29.5ms、Vector Hybrid 30.2ms，均为 40 样本、预算 250ms。其余基线及原始日志也保留；Forget 仅有通过断言、无打印数值，未补造延迟。State 并发突发 145.11ms 作为排队观测，不能混入顺序写 25ms 门槛。这些直接应用服务基线不替代 W18 安装客户端的生产测量。功能回归的两个警告来自 Starlette TestClient 对 httpx/AnyIO 别名的弃用提示；本包依赖锁未改变，未为消除警告扩大依赖升级范围。

## 装配与回退使用

API 和 Worker 使用同一个配置文件或同值环境变量，数据库/向量根使用绝对路径。默认不配置 Embedding；显式开发验证设 `IRIS_MEMORY_DEVELOPMENT_EMBEDDING=true`，可用 `IRIS_MEMORY_VECTOR_ROOT` 指定受控私有根。`IRIS_MEMORY_VECTOR_REQUIRED=true` 让就绪门要求实际 Embedding 能力。W05 复用私有 `RecallAssemblyConfig.embedding`/`provider.space` 接缝接入真实运行配置，不能把此私有入口包装成业务 SDK。禁用 Provider/回退代码不删除 Canonical/Generation/账本；Schema 20 不变，旧 Worker 的 v1/v2 payload 兼容门保持（本包没有改变 payload；当前安装 Worker 能完成实际 rebuild，不将此开发安装证明扩写为旧二进制生产升级演练）；无新增持久数据、无需 Down Migration。

## W01 验收映射

| 原退出要求 | 当前证据 | 状态 |
| --- | --- | --- |
| API/Worker 共用装配、根目录/空间一致 | recall_runtime + runtime；installed-004 四类重建/独立进程 | 通过 |
| 向量独有命中、两跳 Graph 来源 | 增强 ASGI 有/无 Vector 对照；installed-004 SDK 全默认路由 | 通过 |
| 未配置与暂不可用区分、必需能力拒绝 | affected-007/008；Contract 1.11.0 协商和真实 probe | 通过 |
| 无 Generation、超时/断开、模型不匹配、旧代保留 | ASGI 故障测试、tcp-faults-001、affected-008 | 通过 |
| 权限/删除竞争、Required Lease | 555 项 Recall 全集、采集后 Forget ASGI、release_surface、安装 Required Lease | 通过 |
| capabilities/Trace/completed/degraded/partial 一致 | 未建代原探针及实际 TCP 故障/恢复、禁止 partial、授权负例 | 通过 |
| 契约/版本/安装边界 | source→OpenAPI/version manifest；审查后的 public-api 快照；static-005/installed-004 | 通过 |
| 报告/阶段摘要/对接矩阵/队列 | 当前文件、Phase 7/8/10/14 摘要、Console 矩阵/公共 API 文档 | 通过 |
| 每包收尾 make ci、独立提交 | `ci-002` 完整通过；本报告与实现/证据一起作为 W01 独立提交 | 通过 |

## 最终收尾与后续归属

`ci-002` 于 2026-09-07 15:57:51–16:14:52 UTC 执行完整 `make ci`，退出 0。格式、lint、文档、mypy/TS、契约兼容和公共接口均通过；性能 11 passed（43.30 秒）；Python 功能 11705 passed（870.87 秒），联合覆盖率 **85.50%**；TS SDK 19 passed；Console 单元 29 passed、生产构建及真实浏览器 20 passed；最终 Core/SDK wheel/sdist 边界、隔离安装、公共 SDK 与独立 API/Worker Recall Smoke 全部通过。

[完整最终 CI 日志](evidence/w01/ci-002/output.log)、[结果](evidence/w01/ci-002/result.json)、[四项构建产物摘要](evidence/w01/artifact-digests.json)和[候选审计](evidence/w01/final-candidate-audit.json)已归档。CI 启动后代码、契约、测试、工具与构建输入均无漂移；后续仅更新报告、证据和检查点，并单独通过文档链接/差异检查。原始失败日志保持字节不变，保留其诊断空白字符；源码与文档差异检查排除原始日志。

W01 的 HTTP 漏接线问题与专属验收已关闭。W02 承接 Graph 原深度/扇出/节点与五类竞态量化门禁；W05 承接生产 Embedding 配置、真实目标 Provider 和未配置向量任务积压治理；W06 承接认知 Provider；W16/W18/W19 承接生产环境、长跑与恢复。当前本机开发验证不替代这些后续要求。W01 独立提交后立即进入 W02。

原定时自动续作 `w01-w20` 已按 2026-09-08 用户要求取消；现由 active goal 按后台任务完成结果续作，不等待定时点。机器检查点见 [serial-execution-state.json](../development/serial-execution-state.json)。Phase 11/12 保持 Deferred。
