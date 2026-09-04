# ADR-0019：Phase 10 巩固、Reflection、Provider 治理与 HTTP 传输协议

- 状态：Accepted
- 日期：2026-09-04
- 影响阶段：Phase 10；Phase 11/12 只依赖本 ADR 发布的 SDK、Schema、HTTP 与事件契约
- 基线：§3.1–3.2、§5.4、§15.3、§15.6、§16–17、§21、§23–24、§29、§31–32、
  §34–35.4、§36 Phase 10；ADR-0001–0007、0009–0018

## 背景

Phase 0–9 已形成 Canonical 应用服务、生成契约与离线 mock server，但尚无真实 ASGI
传输层；Episode Consolidation、Extraction、Reconciliation、Reflection 与 Persona Evaluation
仍是禁用 job kind。Phase 10 必须在不改变 v1 已发布语义的前提下，同时冻结后台认知管线、
Provider 故障边界、凭据与事件传输、重放运维及进程生命周期。

## 决策

### 1. Consolidation Window、Watermark 与版本语义

- 每个输入窗口由 `(tenant, agent, space_group?, space?, session?, topic_key,
  window_start_us, window_end_us, source_watermark)` 唯一命名。选择只包含在固定
  `source_watermark` 时已经 committed、未 Tombstone、且发生时刻位于半开区间
  `[start, end)` 的 Observation；窗口有明确的最大记录数、最大字符数和最大跨度。
- Watermark 是本次计算的不可变上界，不随执行期间的新 Observation 前移。迟到 Observation
  若其提交水位高于已封闭窗口的 Watermark，不改写旧 Episode，而由后续窗口或显式 replay
  在新 Watermark 下产生新决策。重复任务以窗口身份与版本组收敛。
- Builder 版本属于输出身份；升级 Builder 不改写历史 Episode/ReflectionRecord。相同窗口在
  新 Builder 下先 Dry Run，采用后以新的确定性 fingerprint 形成新运行记录；旧版本仍可审计
  重放和回退。
- 提交事务重新读取 Agent Watermark、全部 Source Revision、Tombstone、Scope、Privacy、
  Policy 以及 job owner/generation/lease；任何来源变化或 fence 使本次 Canonical 提交为零。

### 2. Candidate、Evidence Span 与确定性身份

- `ReflectionRecord` 保存固定 Evidence Window、来源图、版本引用、Provider outcome 和候选/
  拒绝计数，不保存原始 Provider 响应、Prompt 展开文本或 Chain-of-Thought。
- Candidate 类型只允许 `claim|relation|note|task|persona_proposal`。Candidate fingerprint 是
  `sha256(canonical_json(type, normalized payload, sorted evidence spans, scope, privacy,
  version set))`；ID 是 fingerprint 的稳定 UUID 形表示。相同输入及版本三次运行产生相同
  fingerprint、ID、Evidence 顺序与拒绝原因。
- Evidence Span 必须引用本窗口中存在的 Canonical Observation 及其固定 revision，并满足
  `0 <= start < end <= source_length`。Candidate 不能引用 Reflection、Candidate、Provider
  response 或不在输入 Allowlist 中的 Entity/Resource ID。
- 无来源、未知 Entity、越权 Scope/Privacy/Authority、超长/坏枚举/坏值域、任意代码 Trigger、
  自动 Active Task、直接 Persona Core 修改与自循环来源全部拒绝并持久化稳定 reason；拒绝
  记录只保存低敏摘要和 payload hash。

### 3. 独立版本空间与协调规则

- `prompt_version`、`provider_schema_version`、`builder_version`、`policy_version`、
  `reconciliation_version`、`model_id/model_version` 独立演进，全部进入 run identity 和
  ReflectionRecord。任一版本改变都不会冒充旧版本的确定性重放。
- Provider 只生成候选。服务端确定性 Reconciliation 按精确逻辑身份、来源权威、有效时间、
  独立 Evidence 与当前 Canonical 状态排序；精确重复走既有应用服务去重，冲突事实走既有
  `dispute` 语义，绝不由模型静默择一。
- Reflection 输出不能成为同一结论的新独立 Evidence，也不能写成 Observation。Persona
  Candidate 只能调用 Phase 9 Proposal/Policy 边界，不能直接发布、修改 Current 或 Core。
- 只有 fenced Canonical 提交成功后，才在同一事务产生 refs-only FTS/Vector/Profile/Graph/
  Persona 后续 Outbox；Dry Run 永不产生 Canonical、Outbox 或 usage 激励。

### 4. Provider 治理与持久状态边界

- Extraction、Summarization、Reconciliation、PersonaEvolution 都是 application Port；具体
  SDK/HTTP 适配器只存在于 providers 包。网络、模型推理和长计算位于 SQLite 写事务外。
- 每个 job kind 独立配置 timeout、最大 retry、令牌桶 rate limit、每日预算和并发 semaphore；
  调度按 tenant/agent 轮转，不能由单一积压方长期占满。
- Circuit Breaker 记录连续失败、open-until 与单个 half-open probe。持久化只保存低敏状态、
  计数、成本和错误分类；进程内 semaphore/token bucket 不持久化。恢复 probe 有界且一次只
  放行一个请求，积压不会同时击穿恢复中的 Provider。
- timeout、429/5xx、临时传输故障、open circuit 为 retryable；Schema/ID/Evidence/Policy
  违规为 dead；预算耗尽在预算窗口内 retryable、超过任务有效期后 dead。Provider 缺失只使
  相关 capability degraded，Canonical 在线 API 保持正确。
- Credential 不保存明文。应用与管理 Bearer credential 使用随机 token，数据库仅保存
  `sha256(token)`、tenant/app/grants/capabilities/purposes、plane、创建/过期/撤销/轮换时间。
  Provider secret 仅以环境变量或权限受限文件引用存在，不进入 SQLite、备份、导出或诊断。

### 5. AccessContext 与应用/管理平面隔离

- 认证中间件以 constant-time digest comparison 查找未过期、未撤销 credential；再从服务端
  credential grant、Agent/Space/Group 注册关系构造 `AccessContext`。请求体与路由参数只能
  与之取交集并收窄，绝不加入 grant。
- 应用 credential 不能调用管理端点；管理 credential 不能冒充应用面活动宿主。Binding、
  SpaceGroup bind/unbind、index rebuild、backup、export、audit read 分别要求独立 capability
  与非空 reason code，并各自写审计。
- 未认证、无权限和不可见资源统一映射 `access_denied`；授权检查先于资源存在性回显，避免
  存在性探测。

### 6. SSE 事件面

- 发布 `GET /v1/events`，capability 为 `events.sse.v1`，可由配置关闭；关闭时能力协商不声明
  该 capability，端点返回 `not_ready`。Bearer 认证及 Scope/Privacy 终检与普通读取相同。
- SSE envelope 固定为 `{event_id,event_type,occurred_at,resource_refs,source_watermark}`，
  首版 event type 为 `persona.revised.v1`、`surface.lease_revoked.v1`、
  `revision.invalidated.v1`、`cognitive_event.ready.v1`；不携带正文、Secret 或完整 scope。
- `id:` 使用单调持久 event cursor；客户端以 `Last-Event-ID` 恢复。服务端按 credential 信封
  重新过滤；断线后指数退避重连。每连接使用有界队列，慢消费者到上限即断开并从最后确认
  cursor 恢复，不阻塞 Outbox/Canonical 提交，也不丢失持久事件。

### 7. Dry Run、diff、replay、Dead Letter 与版本回退

- Dry Run 固定 Watermark 与完整版本集，只写可审计 run/candidate/reject/provider outcome；
  `commit_mode=dry_run` 禁止创建 Canonical 资源。diff 以 fingerprint 集合产生
  `added|removed|changed|unchanged` 低敏摘要。
- 审计 replay 必须有 admin capability、reason 和原 run 引用；默认沿用原版本集，显式选择新
  版本时产生新 run identity。Replay 从 Canonical sources 重验，不从保存的 Provider response
  重放。
- Dead Letter replay 创建新 Outbox ID，以 `replay_of` 指向原 job，不复用 lease/generation；
  相同 replay request 幂等收敛，Canonical 逻辑资源继续由 fingerprint/既有领域去重。Candidate
  永不引用自己，重放不增加对自身的 evidence weight。
- 回退按 job kind 关闭 capability 或切回上一组 Prompt/Builder/Policy/Reconciliation/Model；
  历史记录不改写。旧 worker 不领取未知 kind/version；Phase 10 worker 理解 payload v1 与 v2，
  并把 v1 规范化为 v2 的缺省版本集后执行。

### 8. `serve` / `worker` 配置与生命周期

- 配置优先级固定为 CLI > `IRIS_MEMORY_*` 环境变量 > TOML 配置文件 > 安全默认。数据目录
  必须是明确专用目录，不能是 `/`、用户主目录或仓库根；Secret 只接受 env/file 引用。
- `serve` 启动：验证配置/runtime → 打开 SQLite 并执行 online-safe migration → 验证 Persona/
  索引/Provider/capability → 启动 ASGI → Ready。关闭：先撤 Ready、拒绝新请求 → 等待 in-flight
  到 Grace Deadline → flush audit/metrics/checkpoint → 关闭索引 handle 与 SQLite。
- `worker` 启动同样先验证/migrate/readiness，再启动 Scheduler 与 claim loop。关闭先停止领取
  新 tick/job，短任务在 Grace Deadline 内提交；长任务停止后令 lease 自然过期或显式安全
  release，不伪造 completion。强杀恢复只依赖事务、Outbox lease、Tick ledger 与幂等记录。
- 默认 Grace Deadline 30 秒，可向下配置但必须为正；信号处理 `SIGINT|SIGTERM` 幂等。

### 9. Recall Usage 激励

- ADR-0014 留给 Phase 10 的路径本阶段启用：只有已通过 Phase 6 防伪校验并持久化的
  `host_selected`/`model_visible` 能形成 usage activation ledger。
- `model_visible` 获得高于 `host_selected` 的有界增量；相同 `(request,host_cycle,candidate)`
  幂等。Claim 只更新 Accessibility 的新 Revision；Focus 只经 Phase 3 `activate` 纯函数边界
  更新 Activation。不存在对 Confidence、Authority、事实内容或 Evidence count 的修改。
- Usage 激励同样服从当前 Tombstone、Scope、Privacy 与 revision 终检；已删除或已被替换的
  Candidate 不被激活。该路径写审计和 refs-only projection Outbox。

### 10. HTTP 契约与兼容

- 使用 FastAPI/Pydantic 2 实现真实 ASGI 服务，但冻结 OpenAPI 仍由
  `contracts/source/contracts.json` + `tools/generate_contracts.py` 生成，框架文档不能覆盖它。
- 新端点与字段只做 additive 变更。统一 rebuild 的 kind 只接受
  `{recent_context,fts,vector,graph,profile}`；旧 `/v1/admin/recent-context:rebuild` 保留本发布
  窗口，capability `admin.recent-context-rebuild.v1` 标记 deprecated，并由 negotiation 返回
  `deprecated_capabilities`。
- Backup 与 Export 是独立资源、独立目录、独立 capability、独立审计和保留策略；Export
  使用公共版本化格式，永不直接返回 backup 文件。
- 未知异常统一为 `internal_error`；稳定错误只从显式映射表产生。错误、日志、指标、trace、
  fixture 均不得包含 stack、数据库/文件路径、token、secret、请求正文或敏感完整 ID。

## 否决的替代方案

- 用最新 Watermark 提交已在旧 Watermark 计算的结果：会让输入集合不可证明、无法重放。
- 保存原始 Provider response 以便重放：扩大敏感面且不能证明来源现势；重放必须重读
  Canonical sources。
- 把 Reflection 写成 Observation 或让 Candidate 为自身作证：形成循环 Evidence 权重。
- 让管理 token 同时作为应用宿主 token：会绕过 active-surface holder 与用途隔离。
- SSE 使用仅内存队列：断线无法恢复且慢消费者会把通知可靠性变成进程生命周期问题。
- Usage 修改 Confidence：检索使用不是事实真实性证据，违反 §15.6 与 ADR-0014。
- 由 FastAPI 自动生成并覆盖冻结 OpenAPI：会改变 Phase 0–9 已发布语义与 SDK 基线。

## 后果与迁移影响

- 新增 Schema 11 expand migration；兼容窗口推进为 `[10,11]`。不提供 Down Migration；回退
  使用 0.11.0 二进制运行 Schema 11，若必须退回 0.10.0 则隔离恢复 Schema 10 备份。
- Core/Python SDK/TypeScript SDK 升至 0.11.0；Contract 按实际 additive capability、错误、
  Schema 与 endpoint 变更提升 minor。旧路径和旧 payload v1 在发布窗口内保持可读。
- 运行态可分别关闭 consolidation/reflection/persona evaluation/SSE/provider；关闭不会影响
  Canonical 在线写、读取与现有 Recall 路由。
