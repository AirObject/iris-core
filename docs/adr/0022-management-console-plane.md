# ADR-0022：管理控制台平面（Console Plane）取代自动化旧数据迁移，并冻结运营密钥、手动导入与可调运行参数的边界

- 状态：Accepted
- 日期：2026-09-05
- 影响阶段：Phase 13（**取代**原"旧 Iris 数据迁移"阶段）；Phase 14 继承其发布与恢复门禁
- 取代关系：本 ADR 取代 [§37 旧 Iris 数据迁移](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#37-旧-iris-数据迁移) 中"连接源库、自动扫描、增量追平、双写切换"的实施路径；不取代其**数据安全要求**（见"迁移影响"）
- 基线：[§5.4](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#5-租户agent-与空间模型)、[§19](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#19-remembercorrectforget-与保留)、[§21](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#21-备份恢复与导出)、[§22](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#22-fts5faiss-与派生投影)、[§23](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#23-http-api-与能力协商)、[§24](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#24-provider-边界)、[§29](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#29-安全与隐私)、[§31](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#31-可观测性)、[§34](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#34-配置与运行模式)、[§37](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#37-旧-iris-数据迁移)；ADR-0001、0004、0005、0006、0015、0017、0019
- 设计依据：[Console 设计与接入边界](../design/console-backend.md)；实施状态与证据见 [Phase 13](../development/phase-13-web-console.md) 和 [验证记录](../reports/phase-13-verification.md)
- 状态说明：本次整理保留原 Accepted 决策；Accepted 表示边界已接受，不表示全部功能已实现。

## 背景

以下是决策时（Phase 10 基线）的背景，部分认证/读面缺口已在 Phase 13 推进，当前状态以阶段与验证记录为准。Phase 10 交付了 `/v1` HTTP 传输层与不透明 Bearer 凭据，当时缺少面向人类运营者的入口，源文件核查确认了六个缺口：

- `CredentialService.issue()` 存在，却既无 CLI 也无 HTTP 面——第一把管理凭据无法被创建，凭据无法列举、轮换或吊销。
- 记忆数据只能按 Agent 视角逐条读写；没有跨 Scope 的有界列表、过滤与审阅面，也没有 State/Focus/Task/Episode/Relation 的管理更正命令与完整 Forget 链。
- `/metrics` 只有 4 组租户级计数器，回答不了"数据在增长吗、投影落后多少、哪条召回路由在降级"。
- `POST /v1/admin/exports` 只能整租户逐表导出（含 `audit_events` 等内部行），没有下载契约，也**完全没有导入面**。
- `providers/embedding.py` 有完整的 `HttpEmbeddingProvider`，但 `runtime.py` 固定构造 `DeterministicEmbeddingProvider` 与 `model="deterministic-local", dimension=32`，没有任何装配路径。
- 召回预算、融合权重、Focus 衰减、巩固阈值、背压门限全部是代码内的 dataclass 默认值。§34 要求"动态领域状态不通过环境变量修改"，而它们连数据库都不在。

与此同时，原 Phase 13 计划以"只读扫描旧 Iris 库 → 映射 → 分批导入 → 增量 Cursor 追平 → 双写切换 → 回退演练"完成历史数据迁移。该路径的前提是能够稳定访问并解析旧库结构，而旧插件的真实数据结构至今未经核查；它同时要求 Core 长期维护一条只为一次性迁移存在的、直连外部数据库的写入通道。

## 决策

### 1. 用管理控制台的手动导入取代自动化源库迁移

Phase 13 的交付物改为 **Web 管理控制台后端**：密钥认证、记忆数据管理、统计、导入导出、Embedding Provider 配置、运行参数调整。旧数据的兼容性**只存在于运营者主动上传文件的导入流程**。

因此被取消的是：连接旧数据库、自动发现旧目录、只读扫描器、删除账本导入、双写、增量 Cursor 追平、Adapter 切换编排与切换回退演练。被保留的是它们背后的数据安全要求（见"迁移影响"）。

**被否决的替代方案**：保留自动迁移工具链并额外交付控制台。理由是它要求同时冻结两套导入语义（直连源库与文件上传），而两者的授权、可信度与去重模型并不相同；在旧库结构未经核查的前提下，先冻结一套针对它的映射契约会把未验证的假设变成需要长期兼容的承诺。灾难恢复仍由 Phase 14 的离线 `restore`/`recover-switch` 承担，不被 Web 导入替代。

### 2. 独立的 Console Plane，而不是扩张 `/v1`

- 新增平面 `/console/v1`，契约有**独立真源与生成物**：`contracts/source/console.json` → `schemas/openapi/console.json`、`schemas/jsonschema/console/*`、`schemas/fixtures/console/*`、`schemas/compatibility/console-baseline-v1.json`，起始版本 `1.0.0`。
- `/v1` 的 OpenAPI、能力清单、错误码清单、兼容基线与 SDK **不因 Console 变更而变更**。Console 复用既有错误 `code`，浏览器特有分支用 Console 契约内定义的 `details.kind` 表达，不往宿主 Capability 清单里塞浏览器概念。
- Console 默认**关闭**，需显式 `--enable-console` 启用；可选独立监听端口。静态资源与 API 同源，第一版不开放跨源 Cookie/CORS。

**被否决的替代方案**：在 `/v1/admin` 下继续堆端点。`/v1` 的兼容窗口由 Adapter 与 SDK 支持矩阵决定，控制台迭代快一个数量级；共用一份兼容基线会让一次页面改版触发 Adapter CI 矩阵重跑，并把 Cookie/CSRF 语义泄漏进宿主协议。

### 3. 唯一写路径不变量

**Console 的每一次业务写入都必须进入与 `/v1` 相同的应用服务与 UnitOfWork，不得在路由中执行领域表 INSERT/UPDATE，也不得为批量任务复制一套领域校验。**

Console 因此继承 Scope 收窄、Privacy 前置、Expected Revision、Idempotency、Tombstone 优先、Outbox 原子提交与 Audit。新增内部 `CommandActor`（携带 `origin=console|manual_import`、运营密钥 ID、授权快照版本）只能由服务端构造，客户端不能在 Payload 中指定来源。管理命令分支只跳过"宿主正在产生外部效果"所需的 Surface Lease 校验，其余门禁全部保留。

Console 只被允许新增**读侧**的有界查询与聚合，以及三类不触碰 Canonical 领域的自有表：会话/限流、`provider_configs`、`runtime_settings`。任何"控制台专用捷径写入"视为缺陷。

### 4. 两类密钥结构性隔离，会话不是 Bearer

- **运营密钥**存于新表 `console_operator_keys`，只被 `/console/v1/auth/login` 接受；**宿主服务凭据**留在 `service_credentials`，只被 `/v1` Bearer 入口接受。不复用同一张表，从结构上防止有限权限的 Console 密钥绕到 `/v1/admin` 取得旧式租户特权。
- 第一把 Owner 密钥**只能离线签发**，明文只显示一次。Web 不提供匿名 bootstrap、默认 root 密钥或租户创建入口；全锁定恢复走同一离线通道。
- 浏览器会话是服务端存摘要的不透明令牌，放在 `__Secure-imc_console` Cookie（`HttpOnly; Secure; SameSite=Strict; Path=/console`），配合会话绑定的同步 CSRF 令牌与 Origin 校验。**长期密钥绝不留在浏览器存储里。**
- 授权是逐资源、事务内复核的显式 Grant（动作权限 + 各维度 selector + Purpose/Privacy/主体同意），空列表表示无授权而非全部。`domain/access.py` 的 `admin` 布尔值**不是角色系统**，Console 不得把它当作特权来源。
- 密钥 Grant 不可变：变更授权必须轮换出新密钥，`PATCH` 只改名称、说明与**缩短**有效期。理由是 `AccessContext` 由这些字段推导，就地放宽等于让已发出的令牌静默扩权。
- 敏感动作要求 5 分钟内 recent reauth；最后一把可用 Owner 不能被 Web 吊销或轮换丢失。

### 5. 没有物理删除：控制台的"删除"是 Forget，且必须先预览

删除统一走 `POST /memory:forget-preview` → `POST /memory:forget`，提交只接受先前预览的 `preview_id/hash`，服务端再次授权并校验集合、Revision、保护状态与删除水位；任何变化返回 `preview_stale` 要求重新预览。禁止"按可变过滤器直接删除"。`soft` 是 Tombstone + 投影摘除，`erase` 追加内容擦除；Legal Hold 与保护资源一律拒绝，HTTP 与 Worker 都不能绕过。Console 不提供撤销 Tombstone 或查看已删除正文的路径。

### 6. 导入只接受数据，永不复活已删除内容

- 只接受纯数据格式（首版 `imc-data/v1` JSONL 与 `manual-records/v1`）；拒绝 `.db`/SQL dump/pickle/压缩包/备份目录，拒绝远程 URL、服务器路径与源库连接串——上传接口只接受字节流。
- 固定拒绝的记录类型：凭据、会话、权限、Provider 配置与 Secret、Settings、Audit、Tombstone、幂等/Outbox/调度/投递/Usage 行，以及全部投影。目标投影在导入后**重建**，不导入。
- 流程固定为 `upload → validate → (review) → commit`：`validate` 只写 staging 与报告；`commit` 必须携带服务端生成并存储的 `report_hash`，客户端上传的报告 JSON 不被信任；默认要求已校验的本地备份，备份不可用时停在 `blocked`。
- 目标 tenant 永远来自会话，文件中的 scope 仅作来源说明；目标 resource ID 全部由服务器生成；现有资源冲突只支持 skip 或 quarantine，第一版不覆盖 Canonical。
- **Tombstone 优先于导入**（ADR-0005）：长期 `import_source_refs` 账本记录来源键与删除标记，换目标 UUID 或换 `dataset_id` 都不能复活。但必须诚实声明其边界——身份与内容均被改写、且不含本系统 lineage 的外部材料无法被证明与旧删除对象相同，这类记录按新的不可信材料审核。
- 上传成功不等于来源可信：历史 Assistant/Tool/System 文本默认进入隔离候选或原始 Artifact，Claim/Relation 必须引用可见有效 Evidence，Task 导入为草稿且触发器 disabled，Persona 一律为待审 Proposal。
- 逐条写入走应用服务，去重键为 `(tenant, source_namespace, resource_type, source_id)`，业务写入与进度 checkpoint 在**同一事务**提交——先提交业务再记进度会在崩溃后重复创建，属禁止实现。

### 7. Provider 配置是"草稿—探测—激活"的不可变修订

- `provider_configs` 每 `(tenant, provider_kind)` 至多一个 `active`；生命周期 `draft → probing → probed → active → retired`，激活前必须有 30 分钟内的成功探测。
- **向量空间身份变化必须重建**：`model`/`dimension`/`metric`/`normalization`/`template_version`/`builder_version` 任一变化构成新的 `VectorSpaceConfig`（ADR-0015 §5），激活创建新 Generation 并入队 `vector.rebuild`，旧 Generation 在 fenced COW swap 完成前继续服务。只改限流/超时/批量为热生效。维度不匹配是激活前的硬失败。
- 配置存储、Worker 投影构建与 API 召回读路径**必须一起接线**。只改配置表会产生"页面显示已启用、实际仍是 deterministic"的假象，这是禁止的交付形态。
- 密钥有两种承载：`secret_ref`（`env:`/`file:` 引用，Console 永不接触本体）与 `sealed`（AES-GCM 封装，AAD 绑定 `tenant|config|revision`，依赖惰性导入的 `cryptography`）。未配置主密钥时 `sealed` 直接拒绝，不静默降级为明文。读接口只返回 hint 与摘要前缀。
- 服务端探测是本阶段主要的 SSRF 面，按出站策略约束：仅 https 或显式回环、默认拒绝私网与云元数据地址、解析一次并连接该 IP、不跟随重定向、响应大小与超时上限、固定无业务内容的探针文本、不返回向量与原始响应体。

### 8. 运行参数由类型化注册表驱动

- 代码内维护唯一注册表（key、类型、约束、默认值、可用 scope、生效方式 `online|worker|restart`、副作用、所需权限、风险等级）。`GET /settings/registry` 直接发布它，**文档不复制清单**——与错误码/能力清单同一条规则。
- 取值按 `agent → tenant → global → 代码默认` 回落；写入是多键原子提交 + `expected_revision` + `reason_code`，响应显式返回 `side_effects`（入队了哪些重建、是否 `pending_restart`）。
- 生效通过单调递增的 `settings_watermark` 传播；"已保存"与"已生效"是两个状态，`restart` 类参数在生效前始终可见为待重启。
- 参数只有在**被服务真正读取**之后才允许在注册表中标为可用；仅有存储没有接线的参数组不得发布。
- 注册表中**根本不存在**关闭安全门禁的键（认证、CSRF、Tombstone 优先、Legal Hold、Evidence 要求、Scope 校验、出站策略）。"没有这个开关"比"有开关但默认关闭"更可靠。

### 9. 统计是有出处、有新鲜度的投影

- 每个指标在**当前会话可见集合**上计算——数量本身就是信息泄漏；同一指标对不同密钥可以返回不同数值，响应带出可见范围指纹。统计一律不返回正文、高频词、向量或 Provider 响应。
- 三条来源：预聚合 rollup、即时有界查询、进程内计数器。在线路径不允许全表 `COUNT(*)`；即时查询在 `query_only` 连接上带语句级超时，超时降级并写 `warnings`，不返回 500，也不安静地给错数。
- rollup 复用既有 Outbox/Scheduler/Lease，写入可随时重建的 `console_stat_rollups`；落后超阈值时响应标 `stale=true`。
- 指标清单的真源在服务端（`GET /stats/metrics`），前端不硬编码。
- Recall 延迟需要新数据：`recall_requests` 增补可空列 `duration_us`，历史区间返回 `null` 并标注 `coverage_from`，不允许用替代量假装成延迟。

### 10. 披露策略：Console 保留 403/404 区分

`/v1` 的资源路由把 `access_denied`/`scope_violation` 掩码成 404 以关闭存在性预言机。Console 平面不做该掩码（与 `/v1/admin` 一致）：调用方已通过运营密钥与会话认证，可诊断性优先。按 ID 请求不可见对象仍统一 404；缺少页面动作权限返回 403。Console 的错误 `details` 使用**显式字段白名单**，不依赖"字符串里是否含 secret"来判断安全，且禁止令牌、Provider Key、原始正文、数据库路径与堆栈。

## 后果

**预期正面影响（全部切片完成后）**

- `/v1` 契约、SDK 与 Adapter 消费者契约完全不受控制台迭代影响。
- 第一把管理凭据、密钥轮换与吊销从"未实现"变为有离线与 Web 两条可审计路径。
- Embedding Provider 从硬编码变为可配置、可探测、可回滚，且换模型必然伴随受控重建，而不是静默的向量空间污染。
- 运行参数从代码常量变为有类型、有历史、有副作用声明的领域状态，符合 §34 的分层。
- 旧数据的处置从"一次性工具链"变为长期存在的产品能力：任何来源的数据都走同一条经过审核的导入路径。

**负面与代价**

- 新增一个必须长期维护的契约面与其兼容基线。
- 新增认证、导入/导出、Operation、Provider、Settings 与统计持久化；实际对象/迁移编号随切片分配，不能把设计估算视为已安装 Schema。
- 导入是本系统第一次接受外部数据写入 Canonical，威胁面显著扩大；它的门禁（备份、报告哈希、Tombstone 账本、逐条重新授权、拒绝类型清单、隔离候选）不是可选项。
- 服务端探测引入 SSRF 面，出站策略成为必须维护的安全配置。
- 浏览器专属关注点（Cookie、CSRF、CSP、上传流）进入本仓库，必须限制在 `api/console/` 内。
- 旧数据迁移从"可自动化、可演练回退"降级为"运营者手动分批操作"：大体量历史数据的迁入时间显著变长，且依赖运营者自行从旧环境导出可接受的数据文件。**这是本决策最实质的代价，应在阶段文档的已知限制中长期保留。**

## 迁移影响

- 迁移以附加式变更为目标；当前 0012/0013 声明 `online_safe=true`。实际锁时、兼容与回退须按迁移清单演练，不能把直接删表/列当作已验证降级路径。升级后 Console 仍默认关闭；认证主密钥随备份/恢复单独保护。
- 原 Phase 13 文档 `phase-13-legacy-migration.md` 由 `phase-13-web-console.md` 取代。**下列原始要求不随实施路径一起作废**，改由导入流程承担，并在新阶段文档中逐条追踪：

  | 原要求 | 新承载位置 |
  | --- | --- |
  | 每个目标对象保留来源引用与转换版本 | `import_source_refs` 与导入报告的 lineage 字段 |
  | 稳定幂等键、可重跑、断点续传 | 去重键 + checkpoint 与业务写入同事务提交 |
  | Tombstone 优先于其他内容 | 长期去重账本的删除标记 + 提交前重新校验删除水位 |
  | 无法证明主体/Scope/时间/来源的内容进入隔离，不自动成为 Active Claim | 隔离候选与人工 review 流程 |
  | 投影不作为事实迁移，从 Canonical 重建 | 导入拒绝投影记录类型 + 导入后重建 |
  | 未知身份不自动创建或按同名合并 | 映射必须指向已存在且已授权的目标 |
  | 迁移结果可追溯到来源与决策 | 逐条报告 + 审计 + `report_hash` |

- 不再承载的原要求：源库只读扫描与前后哈希校验、双写/冻结策略、增量 Cursor 追平、Adapter 切换与切换回退演练。运营者需要自行在其原环境导出数据文件；跨系统的一致性切换不再由本项目提供工具。
- Phase 14 继承性能、Soak、备份恢复与升级门禁，先收口 Phase 13 未完成切片。控制台导入测试数据集必须等真实导入链验收后使用，此前由应用服务构造明确测试夹具。
- Phase 11/12 不再把 Adapter 切换与 Cursor 追平交给 Phase 13；手动数据导入不提供跨系统一致性切换工具。

## 未决问题

- 多运营者身份目前是"每把运营密钥即一名运营者"，审计以密钥 ID 记名；独立的 operator 实体与 RBAC 角色留待后续。
- TOTP 第二因子作为可选工作包，默认关闭，不在退出门禁内。
- 旧 Iris 专属格式的解析器只有在取得脱敏样本、确认 schema/version 并写出映射 Fixture 后才能加入 `GET /imports/formats`；本 ADR 不承诺兼容任何具体的历史版本。
- 跨租户运维视图不在范围内：会话严格绑定单一租户，多租户通过多把密钥切换。
