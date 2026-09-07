# Phase 13 验证记录

- 核查日期：2026-09-06；阶段状态：**In progress**。
- 本文合并后端第 1、2、3 步及前端验证记录，替代相互覆盖的状态说明。历史数字保留其原执行范围，不表示当前工作区完整通过。
- 历史后端基线：`80468a41ec569a3be99e36e160f3a0d231613b45`；当前后端与召回修复提交 `2e4d4be`，前端提交 `5583697`。提交保存开发成果，不代表阶段验收完成。
- 该历史基线版本：Schema 14 / Python 0.12.0 / `/v1` Contract 1.9.0 / Console Contract 1.0.0；当时 OpenAPI 为 88 个路径、90 个 HTTP 操作。后续 0014 的历史与验证见[联合修复报告](phase-05-06-07-08-review-fixes.md)。
- 阶段：[Phase 13](../development/phase-13-web-console.md)；语义：[Console 设计与接入边界](../design/console-backend.md)；前后端差距：[对接矩阵](../../web/console/INTEGRATION_MATRIX.md)。

## Phase 14 后续修复（2026-09-06）

原 Reflection UUID 冲突、生成类型漂移与 Memory descriptor 已修复，真实浏览器覆盖 Note 列表、详情、历史与相同筛选刷新；见 [Phase 14 当前实施记录](phase-14-verification.md)。当前已推进至 Core 0.13.0 / Schema 15、业务 Contract 1.10.0 / Console 1.1.0，State/Note/Focus 与 Task 主资源、步骤及依赖管理已接通；完整组合和真实浏览器证据统一记录于上述 Phase 14 报告。以下为修复前历史复验，保留失败证据，不能误认为这些失败仍未处置。剩余管理写入等模块仍未交付。

## 工作区提交复验（2026-09-06）

候选为上述两个提交对应的实现及 SDK `ba1d6e0`；环境 macOS arm64、Python 3.12.13、Node 26.8.1。测试启动本机回环服务并使用独立测试数据；最初沙箱禁止监听的执行已中止或失败，以下后端与浏览器结果来自允许回环监听的重跑。

| 命令 | 本次结果 |
| --- | --- |
| `make ci` | 格式、lint、导入/文档边界、mypy、TypeScript、生成契约与兼容检查通过；Python **10604 passed / 1 failed**，576.55s，覆盖率 **84.10%**；唯一失败仍为下述 Reflection UUID 契约冲突，CI 未通过 |
| `npm test --prefix sdk/typescript`（独立执行） | **18 passed**；根 CI 在 Python 失败后未执行 sdk-test，不能将此独立结果算作完整 CI 成功 |
| `npm run check --prefix web/console` | 在 `types:check` 失败：**Console types drift** |
| Console 独立执行 `lint`、`typecheck`、`test`、`build` | 全部通过，**29 tests passed**，生产产物扫描通过；不抵消生成类型漂移 |
| `CONSOLE_BROWSER_EXECUTABLE='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome' npm run test:browser --prefix web/console` | **5 passed**，7.2s；真实认证/密钥与显式业务 Fixture 的边界仍如下文所述，未补齐真实 Memory 读面联调 |

另复核全部 65 份手写 Markdown 的本地链接与锚点（包含 application/frontend），无断链。临时执行日志为 `/tmp/iris-workspace-ci-local.log`、`/tmp/iris-workspace-sdk.log`、`/tmp/iris-console-check.log`、`/tmp/iris-console-browser.log`；这些本机文件不作为长期仓库产物。

## 文档整理时的核查与复测

此前仅整理文档，不修改既有实现、契约或生成类型。两条针对已知交接风险的检查结果如下；当时未为文档整理重跑完整 CI 或浏览器端到端套件，后续完整复验见上一节。

| 命令（仓库根目录，除非标明） | 2026-09-06 结果 | 结论 |
| --- | --- | --- |
| `.venv/bin/python -m pytest tests/integration/test_console_reads.py -q --no-cov` | **53 passed / 1 failed**，14.00s，2 个上游弃用警告 | 第 3 步未验收；失败为真实 Reflection 标识违反 ResourcePage.id 的 UUIDv7 Schema |
| `cd web/console && npm run types:check` | **失败：Console types drift** | `generated.d.ts` 落后于当前 Console OpenAPI；既有前端通过报告不适用于新增读面 |

失败用例为 `test_reflection_and_candidate_views_reauthorize_the_input_closure`。实际服务产生 `reflection:<32 hex fingerprint>`，当前 Schema 同时要求 `format=uuid` 与 UUIDv7 pattern；测试在 Reflection 视图校验处停止，因此不能把后续 Candidate 验证认定通过。存储层也使用稳定的 `candidate:<raw hash>` 标识。

待裁决方案：沿用各领域 Canonical 标识，把资源 ID 作为有长度上限的不透明字符串；请求 ID/幂等键保持各自规则，再同步真源、Fixture、类型和真实读面测试。本轮未放宽 Schema，也未改写任何旧对象 ID。

代码核查还发现：bootstrap 已按授权发布 `memory`；后端 ResourceTypeDescriptor 已使用 `list_columns`、结构化 `sorts`、`create_schema/update_schema`，前端 Memory 页仍消费设计层 `columns`、`sorts: string[]`、`create`。因此修复生成漂移后仍须改造真实读面适配并补浏览器 E2E。其余管理写入、Forget、统计、导入导出、Provider、Settings 和 Operation/运维均未有 Console 后端路由，不把 mock 当作已接线服务。

## 历史第 1 步：契约、骨架与启用

日期 2026-09-06；需求 P13-PLANE-01。该切片只有 bootstrap，Schema 11，无持久化变更；生产认证尚未实现，bootstrap 成功测试使用依赖替换，部署没有匿名成功路径。后续第 2/3 步已经推进这些状态。

交付独立 Console 真源/生成链、valid/invalid/forward Fixture 与兼容基线；`api/console/` 子应用、白名单错误、安全头、Host/HTTPS/可信代理、静态与 API 隔离、默认关闭及双监听生命周期。`/v1` 三个冻结文件的字节比较在当时契约测试通过。

| 历史执行 | 结果与边界 |
| --- | --- |
| 实施前 `make ci` | 10,338 Python passed / 81.50% coverage；SDK 18 passed |
| 定向测试 | 37 passed，覆盖生成确定性/兼容/路由、默认关闭、认证隔离、错误脱敏、SPA/静态安全、CLI 与双监听 |
| 首次完整 CI | 10,374 passed / 1 failed / 83.70%；既有 `test_graph_profile_hybrid_recall_latency` 第 26 次 Recall 超时 |
| 性能独立复验 | 1 passed；无 coverage，p50 25.5ms / p95 27.4ms / max 27.8ms，门限 250ms；不替代完整 CI |
| 最终完整 CI | **10,375 passed / 83.71% coverage / SDK 18 passed**；484.02s；format/lint/边界/docs/mypy 215 文件/TS/契约门禁通过 |

最初 mock-server 测试因沙箱禁止 `socket.bind` 失败，获准回环监听后重跑通过，未跳过网络用例。之后完整 CI 的性能失败没有通过修改阈值规避。日志原位于 `/tmp/imc-phase13-baseline-ci-unrestricted.log`、`imc-phase13-step1-targeted.log`、`imc-phase13-step1-ci.log`、`imc-phase13-step1-ci-retry.log`；均是临时本机日志，不是长期仓库产物，本轮未重新确认其留存。

第 2 步刷新响应短期加密的依赖疑问已于 2026-09-06 经任务负责人确认：启用 Console 可要求惰性导入的 cryptography；Provider sealed 主密钥仍独立配置。此历史问题已经解决，不继续列为阻塞。

## 历史第 2 步：密钥、会话与宿主凭据

日期 2026-09-06；需求 P13-AUTH-01/02，及 P13-AUTHZ-01 的密钥列表/cursor 前置。迁移 0012 新增 3 张认证表与宿主凭据 7 个可空列，Schema 12 / Python 0.12.0，当时运行时兼容 11–12；当前已推进到 Schema 14。切片声明的 18 条路径当时均有实现。

交付不可变 Grant、委托约束、最后可恢复 Owner 保护、轮换交接、CAS、同事务幂等/审计、独立认证主密钥、短期加密刷新回放、严格 JSON/Origin/CSRF、离线签发和会话恢复、限流/锁定，以及 application 宿主凭据管理；不向宿主 dispatch 添加管理分支。

安全复核引入的有效约束：

- Grant、有效期、Owner 模板、主体同意的委托与可恢复继任者覆盖均在事务内复核。发现轮换委托遗漏后主动中止首轮 CI（退出 130），不算通过。
- 宿主凭据没有原生 Revision，新增可空 `console_revision`，旧行视为 1；签发/交叠轮换复用 CredentialService 事务命令。
- 宿主应用凭据不表达 Session selector，普通写路径也不按 capability 名称自动只读；Web 签发/轮换要求 memory.read、memory.write、all Session、委托权以及范围/Purpose/同意子集，不提供 management 凭据。
- 宿主旧式空 Purpose 表示“不限制”，Console 空集合表示无授权。Web 新签发必须非空，旧凭据按全部宿主 Purpose 判断可见性，轮换展开为显式集合，保留宿主旧语义。
- 登录并发槽包含读 Body，Body 读取限时、取消释放槽，非法 Unicode/NUL 在进入存储前拒绝；secret_available/secret 具备条件 Schema，读模型有前向兼容 Fixture。
- 迁移测试明确安装历史前缀；已应用的旧 SQL 不修改，不放宽历史校验。

| 历史执行 | 结果及后续状态 |
| --- | --- |
| 安全专项 / 迁移专项 | 116 passed / 34 passed |
| 中间完整 CI | 10,456 passed / 83.87% / SDK 18；518.30s，之后发现宿主空 Purpose 差异，不能当最终证据 |
| Purpose 收紧后 CI | 10,463 passed / 1 failed / 83.87%；State p95 47.02ms 超 25ms；独立复测 9.86ms，取消/超时/性能专项共 57 passed；未改 State 或门限 |
| 输入与取消修复后 | 安全/契约专项 109 passed；完整 CI 10,472 passed / 83.90% / SDK 18，566.32s；之后补充 Fixture，最新专项 118 passed |
| 最终完整 CI | **10,481 passed / 83.90% coverage / SDK 18 passed**，505.85s；format/lint/边界/docs/mypy/契约 drift+compat/Python coverage/TS 均通过 |

相对第 1 步新增 106 个 pytest 用例，含参数化负例和 Fixture。最终日志为 `/tmp/imc-phase13-step2-ci-complete.log`；其他原日志包括 `/tmp/imc-console-step2-security-review.log`、`imc-console-step2-migration-final.log` 和 `imc-phase13-step2-ci{,-final,-release,-verified}.log`。这些临时日志本轮未重验，不作为当前运行证据。

[登录失败耗时原始数据](./phase-13-step-02-login-timing.json) 保留：未知、错误、过期、吊销各 20 次，80 次同为相同 401 对象；各组 p50 103.885–104.151ms、p95 105.037–105.392ms，中位数极差 0.266ms。这只说明当时本机有限样本，不能宣称不存在所有网络侧信道。

## 历史第 3 步：授权与有界读面

日期 2026-09-06；需求 P13-AUTHZ-01；未完成，未跑本切片完整 `make ci`。

交付 `application/console/{authorization,resources,reads}.py`、`storage/console_reads.py`、`routes_memory.py` 与 resource views：实际 Space/Session 拓扑、Purpose/Privacy/同意/Tombstone、来源闭包、历史与引用重新授权、有界列表/计数、query_only 和 SQLite 进度回调。引用包括已有链接账本与行内 SourceRef/Evidence，虚拟引用 ID 不写入 Canonical。0013 只加分页/引用索引；本步骤交付时运行时 Schema 上限为 13（后续[联合修复](phase-05-06-07-08-review-fixes.md)新增 0014），底层兼容 11–12 不等于旧库可直接使用全部 Console 读路径。

| 历史专项 | 记录 |
| --- | --- |
| 认证/契约/边界 | 180 passed，`/tmp/imc-step3-first-tests.log` |
| 迁移 | 119 passed，`/tmp/imc-step3-migrations.log` |
| 首批读面 | 45 passed：三组 Grant 列表/计数/详情，历史与引用再授权，第 20 页后无重复，主表查询计划命中索引，真实 SQLite 中断与只读连接 |
| 真实领域夹具 | State/Focus/Task 子资源/Claim/Episode/Relation/Artifact/Identity/Binding/CognitiveEvent 服务夹具；`/tmp/imc-step3-domain-views.log` |
| 最后读面整组 | 53 passed / 1 failed，`/tmp/imc-step3-reads-tests.log`；Reflection UUID 契约冲突。后来加入的 Python 循环预算当时未重跑整组，本轮已重跑，结果见首节 |
| mypy | 237 个源文件通过，`/tmp/imc-step3-types.log` |

上述日志同样为历史临时路径。Current/History/References 能返回 HTTP 200 不表示其响应已通过发布 Schema；必须保留真实领域数据验证。

## 历史前端：真实联调与模拟分界

交付 `web/console/`，React 19 / strict TypeScript / Vite、独立锁文件、生成类型、客户端、页面、mock 和浏览器测试。历史前端检查使用当时 16 路径的 Console OpenAPI，SHA-256 `03aeb2b80a7b69cda18216aaf29e2d737093b6441062aac2d517df41be25135f`；该哈希不是当前 88 路径契约。

| 历史检查 | 结果 |
| --- | --- |
| npm ci / types:check / lint / strict typecheck | 通过；依赖源离线/超时后使用公共 npm 镜像补齐固定依赖 |
| Vitest | 29/29 通过 |
| production build / 产物扫描 | 通过；JS 约 291kB、CSS 约 6.8kB（未压缩）；生产 mock 模式明确拒绝 |
| Playwright | 5/5 通过，7.2s，使用本机 Chrome 独立临时 profile |
| 后端定向 pytest | 143/143 通过，12.50s，2 个上游弃用警告；范围为 Console contract/plane/authentication/migration，不含后续 reads |
| 全仓库 make ci | 前端任务未重跑 |

原日志为 `/tmp/imc-console-frontend-check.log`、`imc-console-browser-verification.log`、`imc-console-backend-verification.log`，不是长期产物。启动、测试浏览器和代理配置统一见 [工程 README](../../web/console/README.md)。

真实后端浏览器用例仅验证：无会话 401→JSON 登录→bootstrap→运营密钥列表→刷新恢复 Cookie 会话→logout 204；另一个实际触发 reauth_required→重新认证→重放签发→确认清除一次性明文→吊销测试密钥。测试通过应用服务初始化独立临时库、离线签发测试密钥，没有 require_session 替换或生产数据库连接。

其余 3 个浏览器用例在同一生产构建下显式截获业务 API：纯文本与巨大十进制数/null、390px 窄屏布局；导入上传/映射/报告失效/cancelled_partial；Provider 探测重建确认和 Settings 高风险/pending_restart。它们验证前端状态编排，不能称为真实业务联调。应用内浏览器还检查过显式 mock 登录、动态导航、Note 列表/详情/动作入口，mock 标识持续可见。

历史 CSP 检查采用真实后端响应：script/style/img/font/connect 均为 self，无 unsafe-inline/unsafe-eval，无内联 style 或 CSP 拒绝错误；深层 `/console/keys` 刷新可用，API 缺失路径返回 JSON 404。使用显式回环 HTTP，**未替代生产 TLS/可信反向代理验收**。曾修复窄屏导航宽度溢出、验证 Operation 切到提交 Operation 后显示旧终态的问题，并补回归，没有跳过失败用例。

## 未完成门禁与交接

阶段 14 先收口资源 ID、前端类型/正式读 descriptor 与当前完整 CI，再实施 Phase 13 剩余业务工作包。必须验证真实授权/计数隔离、Forget/Hold/清理、JSONL 往返、备份 blocked、导入审核/同事务断点、Provider 出站/Generation 切换、实例参数水位、任务/DLQ/备份等服务器效果。

生产 Cookie/多标签刷新时序、TLS/代理、application 凭据真实交叠轮换及派生任务失权、最大下载文件下 Blob 回退内存仍待验证。每条新增正式业务链须同时检查前端与后端；当前 `make ci` 没有覆盖独立 `web/console` 门禁，发布阶段需要显式补入。

## Phase 14 Note 管理命令切片

2026-09-06，按 [ADR-0025](../adr/0025-console-command-authorization.md) 实施 Console Note 创建、编辑和状态转换。Console 契约升至 1.1.0；复用原领域 Revision/CAS、提升、Audit 与 Outbox，管理授权不借用宿主 Lease。详细测试与剩余项见 [Phase 14 实施记录](./phase-14-verification.md)。其他资源命令、Forget 和第 5–10 步仍未完成。
