# ADR-0017: 契约面对齐 — 错误码取代关系、端点命名裁定与 HTTP 传输层归属

- 状态：Accepted
- 日期：2026-09-03
- 影响阶段：Phase 0–8（补记既有分叉）、Phase 10（新增交付范围）、Phase 11/12（依赖本决策的接入时点）
- 基线：§18.3、§18.6、§23、§28、§33.1、§35.2、§36、§39；ADR-0006/0008/0011/0014/0015/0016

## 背景

一次覆盖全部文档的审计发现：基线 §23 的契约面描述与 Phase 1–8 **实际发布**的
`contracts/source/contracts.json` / OpenAPI 在三个方向上已经分叉，而分叉从未被记录。

1. **错误码**：§23.4 声明"至少冻结以下错误码"的 29 项中，15 项从未发布；其中 10 项
   是被改名（而非新增）——`unauthorized`/`forbidden`/`privacy_denied` 收敛为
   `access_denied`，`scope_denied` 二分为 `scope_violation`/`invalid_scope`，
   `invalid_transition` → `invalid_state_transition`，`internal` → `internal_error`，
   `schema_version_unsupported` 二分为 `schema_incompatible`/`unsupported_version`。
   ADR-0006 明确"删除既有字段/契约需要新的 API Major Version"，ADR-0012/0014 也确实
   逐条核对过"是否在 §23.4 冻结清单内"——说明清单一直被当作权威，只是从未回头修订。
2. **端点命名**：`/v1/negotiate`、`/v1/memories:remember|correct|forget`、
   focus 的单一 `:transition`、`/v1/admin/indexes/{kind}:rebuild` 四处与已发布 OpenAPI
   不一致。ADR-0014 §3 曾就 `task`/`tasks` 路由名做过同类裁定，但未推广到端点面。
3. **传输层归属**：`src/iris_memory_core/api/` 至今是空包，`grep -c fastapi|pydantic`
   为 0，CLI 无 `serve`/`worker` 入口，而 OpenAPI 已声明 61 条路径、双 SDK 已发真实
   HTTP 请求。§36 的 15 个阶段中**没有任何一个阶段**负责实现 HTTP 服务，
   Phase 11/12 的关键路径因此悬空。Phase 2/3/4 曾把这一点写进"已知限制"，
   Phase 5/6/7/8 则不再提及——项目最大的未完成项从交付记录中消失。

本 ADR 一次性裁定三者，使"契约已发布"这句话在文档与仓库之间重新自洽。

## 决策

### 1. 错误码清单的真源与取代关系

**`contracts/source/contracts.json` 的 `error_codes` 是稳定错误码的唯一真源。**
基线 §23.4 自本 ADR 起只保留分类说明、Envelope 形状与"只增不改语义"的兼容规则，
不再复制清单——一份必须手工同步的副本正是本次分叉的成因。

以下取代关系补记为已生效（均发生在 Phase 1–6，属 v1 内部收敛，不构成 Major 变更，
理由见每行）：

| §23.4 原名 | 实际发布 | 裁定理由 |
| --- | --- | --- |
| `unauthorized`、`forbidden`、`privacy_denied` | `access_denied` | 单一授权失败码不向调用方泄漏"未认证"与"已认证但无权"、"资源不存在"与"无权可见"的差异；探测面收敛是 §29.1 威胁模型的直接要求 |
| `scope_denied` | `scope_violation`（数据侧越界）+ `invalid_scope`（请求侧 Scope 结构非法） | 两者可重试性与调用方处置完全不同：前者不可重试且不应重构请求，后者是请求体缺陷 |
| `invalid_transition` | `invalid_state_transition` | 与领域错误类 `InvalidTransitionError` 的稳定码保持单一拼写；ADR-0011 §3 起已在测试中锁定 |
| `internal` | `internal_error` | 与其余码统一 `<名词>_<状态>` 构词 |
| `schema_version_unsupported` | `schema_incompatible`（数据库 Schema 越出兼容窗口）+ `unsupported_version`（契约/payload 版本不被理解） | §20.7 与 §23.5 是两个独立的版本面，合并成一个码会让调用方无法判断该升级二进制还是升级客户端 |
| `tenant_not_found`、`agent_not_found`、`space_group_not_found`、`space_not_found` | `not_found` + `details.resource_type` | §23.4 的 Envelope 已规定 `details` 携带 `resource_type`；每资源一个码会使错误码集随领域对象线性增长，且与"客户端逻辑只依赖 `code` 与结构化 `details`"的约定重复。`identity_not_found` 是例外并保留独立码：它是 Recall speaker 解析的一等失败（ADR-0014 §3），调用方处置路径不同 |

以下两项裁定为**不进入 HTTP 错误码集**，从 §23.4 移除：

- `capability_not_supported`：能力缺失在 §23.2 的协商阶段失败（连接建立期），不是
  请求级错误。SDK 协商失败即拒绝启动，不存在返回该码的请求路径。
- `integrity_check_failed`：备份校验与恢复是 CLI/管理平面操作，以退出码、
  `migration_runs` 与 backup catalog 记录表达（ADR-0013 §9/§10），不经 HTTP。

以下两项**保留在冻结清单内、尚未启用**，由 Phase 9 交付：
`persona_policy_denied`、`persona_base_revision_stale`。

### 2. 端点命名裁定

已发布拼写为准，基线 §23.3 按下表更正：

| §23.3 原写法 | 已发布 | 裁定理由 |
| --- | --- | --- |
| `POST /v1/negotiate` | `POST /v1/negotiation` | 与同组的 `GET /v1/capabilities` 保持名词化资源命名；动词式路径在 v1 只用于 `:verb` 形式的状态转换 |
| `POST /v1/memories:remember` | `POST /v1/claims:remember` | 操作的目标聚合是 Claim；v1 不存在名为 "memories" 的聚合或集合资源 |
| `POST /v1/memories:correct` | `POST /v1/claims/{claim_id}:correct` | Correct 必须携带目标 `claim_id` 与 `expected_revision`（ADR-0013 §2），是资源级动作而非集合级动作 |
| `POST /v1/memories:forget` | `POST /v1/memory:forget` | Forget 的目标是 selector（resource / subject_predicate / session / space / data_request），不是某一个 Claim；留在不可寻址的 `memory` 单数名下以避免暗示"删除某条 memories 记录" |
| `POST /v1/focus-items/{id}:transition` | `:activate` `:dormant` `:dismiss` `:expire` `:promote` 五个端点 | ADR-0011 §3 的状态机对不同转换要求不同必填参数（`promote` 需要 `promotion_target_type`）。单一 `:transition` 会把请求体退化成联合类型，OpenAPI 无法表达 per-transition 的必填约束，且与"状态机合法性检查先于参数检查"的错误排序冲突 |
| `POST /v1/admin/indexes/{kind}:rebuild` | 未发布 | 见 §3：与其余管理端点一并归 Phase 10；届时按本拼写发布，`kind ∈ {recent_context, fts, vector, graph, profile}`，现有的 `POST /v1/admin/recent-context:rebuild` 保留一个发布窗口后由 Capability 标记弃用 |

### 3. HTTP 传输层与进程入口归属 Phase 10

**决策**：传输层与进程入口是 Phase 10 的交付范围，作为"Core 功能冻结点"的组成部分。
具体包括：ASGI 应用与路由、Bearer 认证与 `AccessContext` 构造、领域错误到稳定
Envelope 的映射、`Idempotency-Key` 头与 `expected_revision` 的传输层接线、
`/v1/capabilities` 与 `/v1/negotiation`、`/health/*` 与 `/metrics`、§23.1 的可选 SSE
事件面，以及 `iris-memory-core serve` / `iris-memory-core worker` 两个进程入口（§3.1）。

同时在 Phase 10 补齐**应用层能力已存在、仅缺传输面**的端点：
`GET /v1/entities/{entity_id}`、`/v1/entities/{entity_id}/relations`、
`POST /v1/identities`、`/v1/bindings:prepare|confirm|revoke`、`/v1/space-groups*`
（Phase 1 能力）、`/v1/admin/indexes/{kind}:rebuild`、`/v1/admin/backups`、
`/v1/admin/exports`、`/v1/admin/audit-events`（Phase 1/5 能力）。

**理由**：

- Phase 11/12 是传输层的首个真实消费者；在此之前"应用层服务 + 生成契约 +
  mock server"这一组合已足以冻结请求/响应形状，并已被 Phase 2–8 的契约测试反复验证。
- 传输层跨越全部领域面。若在 Phase 2 建立，则每个后续阶段都要改动它；集中在
  Core 功能冻结点交付，其形状恰好已被前 9 个阶段的契约完全确定。
- Phase 14（硬化）预设"部署包可在干净环境重复安装、升级、备份和恢复"，
  这要求进程入口在 Phase 14 之前就已存在且稳定。

**推翻的既有表述**（§36 与路线图"规划结论"）：
"Phase 6 是首个可接入的 Recall 增量……可作为早期宿主集成基线"更正为
"Phase 6 冻结首个完整 Recall 契约形状；真实宿主接入自 Phase 10 交付传输层后开始"。
Phase 6 的交付物与退出门禁不变，改变的只是"可接入"的时点声明。

**记录纪律**：Phase 5–8 的"已知限制"补记"无 HTTP 传输层（应用层契约 + mock server
模式，归 Phase 10，ADR-0017 §3）"。该项在项目完成传输层之前不得再从任何阶段的
已知限制中消失。

### 4. `persona_revision = 0` 是不可用信号，不是修订号

§14 与 ADR-0008 的不变量不变：Agent 创建事务同时建立 Published Persona 与 Current
Pointer，没有有效 Persona 的 Agent 不能进入 Ready。因此 `persona_revision = 0` /
`persona_content_hash = ""` **不是合法运行态**，而是 Recall 对"Persona 当前不可解析"
的 fail-visible 表达（ADR-0014 §4 的"SDK 不隐藏该状态"由此获得确切含义）：

- 宿主收到 `persona_revision = 0` 时**必须**按"Persona 不可用"处置——不注入
  Persona Slot、不缓存、按自身策略进入 Degraded 或 Not Ready（§26.4 已有同语义），
  绝不把它当作"第 0 版人格"。
- 解析失败的原因只能是"Agent 不存在或已 Tombstone"与"Current Pointer 为空"两类。
  其余异常（存储故障、行损坏）**不得**被伪装成该 sentinel——本 ADR 要求
  `_persona_metadata` 只捕获 `NotFoundError`，其余异常照常上抛为请求级错误。
- Phase 9 的 readiness 门禁（§31.4"Persona Current Pointer 完整性"）使该状态在生产
  部署中不可达；契约保留它是为了让不可达状态在真的发生时可见，而不是静默。

### 5. 向量目录位置：由部署注入，不由 db 路径派生

ADR-0015 §5 的"目录（§35.2，由 db 路径派生 `<db>/vector/`）"表述不准确：
`VectorProjectionService` 的 `vector_root` 是**注入参数**，测试装置恰好传入
db 同级目录，生产部署按 §35.2 的 `/data/vector/` 布局注入。两处描述据此对齐：
ADR-0015 §5 改为"由部署注入 `vector_root`（§35.2 推荐 `/data/vector/`）"，
§35.2 的 `vector/current.json` 标注为"预留，当前未实现（ADR-0015 §5：SQLite
`vector_current` 是唯一权威指针）"。

### 6. 基线 §18.6 的排序公式更正

原式 `final_score = relevance + authority_weight + confidence_weight + …` 相加的是
**权重**而非加权分量，与 ADR-0014 §5 已实现并锁定的语义不符（缺失分量不参与归一、
显式 0.0 是真实零分）。基线更正为 ADR-0014 §5 的加权归一形式，并指向该 ADR 作为
权重表与 Ranker 版本的真源。稳定排序键不变。

### 7. 冻结集勘误（ADR-0014、ADR-0015）

- ADR-0014 §10 的"Degraded 原因码冻结集"漏列已发布的 `fts_as_of_unsupported`
  （见该 ADR 自身 §12.2 的 as_of 语义与 phase-06 验证报告已知限制 #4），补齐为 8 项。
- ADR-0015 §1 写"降级原因码 +`vector_*` **6 个**"，其 §7 冻结集与已发布契约均为
  **7 个**（含 `vector_space_mismatch`），更正计数。

两处均为记录勘误，不改变任何已发布契约字节。

补记两处可追溯性缺口（同样不改变契约字节）：

- **`health.readiness.v2` 没有 v1**：Phase 2 把 `/health/ready` 的响应从 Phase 0/1 的简单
  形态升级为结构化 `ReadinessReport`，能力名直接取了 `v2`；被它取代的 v1 形态当时归在
  `health.v1` 之下，从未单独命名。名字保留（改名是破坏性变更），语义由本条记录。
- **fixture 计数断链 52 → 54**：ADR-0012 记 `fixtures 35→52`，ADR-0013 记 `54→85`。
  中间的 2 个是 ADR-0012 §9.10 与 §12 的审核轮次补充的（`task-create-request-minimum`
  与 lease proof 相关 fixture），落在两份 ADR 的记账时点之间。当前总数以
  `schemas/fixtures/manifest.json` 为准（113 例），阶段文档中的增量数字只作为该次变更的
  说明，不作为总量真源。

### 8. Canonical relations 路由补齐端点实体隐私评估

ADR-0016 §11.8 让 Graph 遍历逐边评估**两端实体自身**的 privacy labels，但 canonical
`relations` 路由与最终 relation rehydrate 从未获得同一判定（Phase 8 已知限制 #9 记为
「留待独立裁定」）。本 ADR 裁定：**同一份数据不能有两套隐私口径**，`relations` 路由与
rehydrate 补齐该检查。

理由：relation 候选的 `text` 与 `source_refs` **点名了两端实体**
（`"<source> <relation_type> <target>"`），因此一条公开 relation 指向 `restricted`
实体时，它对该实体的披露程度与实体行本身相同。攻击面是具体的：Graph 路由拒绝返回的
边，换一条 canonical 路由就能拿到。把它留作「已知限制」等于让最严格的那道检查形同虚设。

实现：抽出共享的 `endpoint_entity_visible` / `relation_endpoints_visible`
（`application/recall.py`），由 `GraphRoute._entity_visible`、`RelationsRoute.collect`
与 `_rehydrate_relation` 共同调用——单一判定，不可能再漂移。rehydrate 的新拒绝理由是
内部理由码 `endpoint_privacy_blocked`（内部诊断，不进契约错误码集）。实体是租户全局的，
其标签按租户级 data scope 评估；解析不到的端点 fail closed。

回归：`tests/integration/test_relation_endpoint_privacy.py`——移除路由侧或 rehydrate 侧
任一半，都各有一个用例失败。

## 否决的替代方案

- **为传输层新增 Phase 15（或在 10 与 11 之间插入新阶段）**：会重排 §36 的阶段编号
  与全部跨文档引用（16 份 ADR、15 份阶段文档、9 份报告均按编号互链），代价远大于
  收益；Phase 10 的"Core 功能冻结点"定位本就与传输层冻结重合。
- **把传输层追认为 Phase 2 的遗漏并回填**：Phase 2 已 Completed 且其退出门禁不含
  传输层；回填会让"Completed 代表可被下一阶段安全依赖"这一状态语义失效。
- **保留 §23.4 的错误码清单副本并手工同步**：本次分叉恰恰证明手工同步会失败。
  清单必须只有一份，且必须是被 CI 校验的那一份。
- **把 `unauthorized`/`forbidden` 等原名作为别名补回契约**：会给调用方两套等价码，
  违反 §23.4"客户端逻辑只依赖 `code`"的单一含义要求，且扩大而非收敛探测面。
- **删除 `persona_revision` 的 0 sentinel、改为可空字段**：`null` 需要所有客户端处理
  可空分支，而该状态本应不可达；保留 0 并明确规定宿主处置，比放宽类型更安全。
- **把 `/v1/admin/recent-context:rebuild` 直接改名为 `/v1/admin/indexes/recent_context:rebuild`**：
  是破坏性变更（ADR-0006）；改为新增统一端点 + 保留一个弃用窗口。

- **继续把 relations 路由的端点隐私缺口记为「已知限制」**：它不是可用性 quirk，而是同一份数据的两套隐私口径——最严格的那道检查可以被换一条路由绕过（§8）。

## 后果

- 基线 §18.6、§23.2、§23.3、§23.4、§33.1、§35.2、§36、§39 与路线图"规划结论"按本
  ADR 更正；ADR-0014 §10、ADR-0015 §1/§5 补勘误注记。契约字节零变化。
- Phase 10 的范围扩大到含传输层与进程入口，其阶段文档新增工作包 10.6 与对应门禁；
  目标版本不变（0.11.0）。
- `application/recall.py::_persona_metadata` 收窄异常捕获（唯一代码变更）。
- Phase 5–8 的已知限制补记传输层缺口；该记录在传输层交付前不得移除。

## 迁移影响

- 无 Schema 变更、无 migration、无契约字节变更，因此无兼容窗口影响：本 ADR 修订的是
  **记录**与**后续阶段范围**，不是已发布的 wire 形状。
- Phase 10 发布 `/v1/admin/indexes/{kind}:rebuild` 时，`/v1/admin/recent-context:rebuild`
  保留至少一个发布窗口并在 capabilities 中标记弃用，符合 ADR-0006 的兼容策略。
- 传输层落地时若发现某条已发布路径无法由应用层能力直接承载，按 ADR-0006 走新增
  可选字段或新 Capability，不得静默改变既有路径语义。
