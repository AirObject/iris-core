# ADR-0009: Phase 2 Reliability Spine — Observation Identity, Integer Cursors, Outbox Fencing and Bounded Catch-up

- 状态：Accepted
- 日期：2026-08-30
- 影响阶段：Phase 2（阶段 3+ 依赖本决策的语义）

## 背景

Phase 2 交付 Observation Journal、Transactional Outbox 与持久 Schedule/Tick。基线（§8、§16、§17）定义了行为要求，但若干实现边界需要冻结为可审计决策：观察记录的身份语义、游标的表现形式、Worker 提交的 fencing 强度、Coalescing 白名单、补算边界与 Job Kind 启用策略。

## 决策

### 1. Observation 的五重身份语义互相独立

`(tenant, agent)` 域内：

| 字段 | 语义 | 唯一约束 |
| --- | --- | --- |
| `source_stream` + `source_cursor` | 流内顺序 | UNIQUE(tenant, agent, stream, cursor) |
| `source_event_id` | 平台事件身份 | UNIQUE(tenant, agent, event_id) |
| `occurrence_id` | 逻辑效果身份（同一真实发生的重投递收敛到同一 Observation） | UNIQUE(tenant, agent, occurrence_id) |
| `idempotency_key` | 记录级请求重试安全；同键不同指纹 → `idempotency_key_reused`，同键同指纹 → duplicate | UNIQUE(tenant, agent, key) |

`occurrence_id` 是对基线 §8.1 的**加性可选字段**（ADR-0006 允许），用于宿主在平台缺少稳定 event id 时表达"同一逻辑效果"。游标不替代幂等键，二者分别保证源顺序与请求重试安全。

**Actor 归属不信任调用方内部 ID**（二轮复核修复，§6.4）：`actor_entity_id_at_ingest` 是"提交时的服务端解析结果"，不是自由请求字段——请求携带它时**必须**同时携带 `actor_external_identity_id`，且必须命中该身份已确认（verified）的绑定（沿 redirect 链比较终端实体，接受合并；墓碑实体拒绝）。只带内部 Entity ID 而不带外部身份的请求是 `invalid_request`：即使该实体真实存在且同租户，也不存在"调用方自行声明 actor"的路径。

### 2. 游标限定为十进制整数位置

Phase 2 的 `source_cursor` 只接受十进制整数字符串（无前导零）。理由：单调性比较、gap 检测（`> current + 1`）与对账都需要全序；不透明游标需要额外的位置登记协议，推迟到有真实平台需求时再以新 ADR 引入。无稳定游标的平台按基线使用 `source_event_id + idempotency_key`。首见游标位置任意；此后旧游标返回已提交结果或抛 `cursor_gap`（带回拨详情），绝不覆盖新游标。Gap 策略（accept/reject/mark）是流级配置，由管理平面显式修改并审计。

### 3. Worker 完成提交的四重 CAS

完成/重试/死信转换必须在**同一个**写事务内通过以下谓词 CAS，rowcount≠1 即 `lease_fenced` 且整个事务（含业务结果）回滚：

```sql
WHERE id = :job
  AND lease_owner = :owner
  AND lease_generation = :generation
  AND lease_expires_us > :now
  AND source_revision = :source_revision
  AND status = 'leased'
```

过期或被接管的 Worker 即使计算成功也绝不能提交。该谓词覆盖**完成、重试与死信**三个转换路径——复核修复后 `mark_retryable`/`mark_dead` 同样携带 `source_revision` 谓词，聚合已被 coalesce 推进的在途 Worker 无法把自己的失败记账写到别人的行上。外部非事务副作用（HTTP 等）无法被数据库 fencing 撤回，Handler 契约强制要求可重放消息模式：以外键 `(job_id, generation)` 作为外部消息键，由外部系统去重。稳定去重键 `UNIQUE(tenant, dedupe_key)` 保证入队幂等；Dead Letter 重放生成新 outbox id 并以 `replay_of` 引用原任务，重放本身幂等（存在未决重放即返回），旧 generation 不可复活。重放任务的 payload **逐字节沿用原任务**（同一 dict、同一 `payload_version`）：重放必须是 Handler 已认识的同一条可重放消息，重放谱系只存在于 `replay_of` 列与 dedupe key，绝不包装成新信封。调度 Tick 对应的任务进入死信时，其 Tick 账本行同步落为 `failed`（0003 的 CHECK 允许带 outbox 引用的 `failed` 终态）——补发失败被显式记账，绝不消失在 DLQ 后面。

### 4. Coalescing 是白名单而非黑名单

仅 State/Profile/Graph 刷新类（`recent_context.maintenance`、`focus.maintenance`、`profile.refresh`、`graph.refresh`）允许按 `(tenant, agent, kind, coalesce_key)` 合并 Pending 项并保留最高 Source Revision；leased 行永不合并（会破坏在途 Worker 的 CAS）。其他一切 kind（含 Observation、Forget、Task、Persona Proposal、审计相邻任务）禁止丢弃式合并——对禁用 kind 传 coalesce_key 即 `invalid_request`。白名单模式意味着新增 kind 默认不可合并，需要显式评审。

**Dedupe key 命名一份内容**（三轮复核修复；四轮复核补齐遗漏分支）：入队先按 dedupe key 查找，命中行的 canonical payload 逐字节比较——相同则在任何状态（pending/retryable/leased/completed/dead）下吸收返回既有行：queued/in-flight 是在途重复，settled 是迟到的幂等重放（该键的结果已终局）；leased 行的相同 payload 重试不要求 revision 不增（内容相同即同一工作，revision 差异只是元数据）。**不同 payload 命中未决行是无条件 `idempotency_key_reused`**——三轮实现曾在 pending/retryable 且新任务带 coalesce_key 时保留"原地合并"例外，四轮复核证明该例外既与本规则冲突、又只检查新任务的 coalesce_key 而不看既有行的 kind 元组，使跨 kind 同键碰撞可以改写他 kind 行的 payload（kind/payload 不匹配 + 新工作丢失）；例外已删除。与 Observation 层同键异指纹的语义对齐：调用方想更新内容必须换新 dedupe key，而这正是 leased 行背后 follower 的创建方式（新键 + 同 coalesce key → 新 pending 行）；换派生 key 的 follower 方案被否决，因为它会让一个逻辑键映射多行、破坏 `UNIQUE(tenant, dedupe_key)` 的去重意义，且重试行为取决于无关行的存在与否。**合并只发生在 dedupe key 未命中之后**，经限定 `(tenant, agent, kind, coalesce_key)` 全元组的查找进入——dedupe 命中永不改写行，跨 kind 变异因此被结构性排除，而非依赖调用方纪律。

### 5. 补算边界与受限日程语法

- Schedule 语法限定 `interval`（every_seconds）与 `daily`（HH:MM + IANA 时区 + DST skip/postpone、ambiguous first/second）。cron 等语法推迟到后续阶段，避免隐式语义。
- Occurrence Key = `schedule:scheduled_at_us:policy_version`，UNIQUE 约束使重启、时钟回拨后同一 occurrence 只产生一次逻辑效果。
- misfire grace 约束所有 catch-up 策略的回溯窗口；单次 advance 的 enqueued 数受 `max_ticks_per_run` 上限；超限 occurrence 显式记为 skipped（`catch_up_cap_exceeded`），绝不静默丢弃，也绝不产生无界任务风暴；调度标记（next_tick_at）单调不回拨。

### 6. Job Kind 注册表默认只启用安全 Handler

注册表登记 §17.4 全部周期 kind 及其元数据（默认优先级、lane、catch-up、coalesce 类别），但本构建仅启用已有安全 Handler：`maintenance.selfcheck`（只读自检种子 Handler）。未启用的 kind：Scheduler 拒绝为其创建 Schedule（避免堆积不可领取任务），Worker 不领取其任务。Payload 显式版本化（version 1）；未知 payload version 的行不被领取、保持 pending（fail closed），入队侧拒绝高于当前版本的 payload。

### 7. 背压判定含投影值

入队/Observe 的压力检查计入"即将写入的这批任务"（jobs+1、bytes+incoming），保证不接受之后必然丢失的工作；所有生产者（Observe、Scheduler Tick、Surface 撤销通知、显式入队）都经由同一个带背压判定的入队入口——直接写仓储行绕过判定是不允许的；**Dead Letter 重放同样经过该判定**（二轮复核修复）：重放插入的是真实新行，满队列时重放与普通入队一样得到 `storage_full`，不存在"重放可以把队列从 N 推到 N+1"的旁路。压力维度为全局/租户/Agent 三级的 jobs 与 bytes。

入队的压力投影按其**真实足迹**计算（三轮复核修复）：投影函数在入队发生前镜像仓储的判定——dedupe key 命中 settled 行、或未决行的逐字节相同 payload 重试，足迹为零（成功的幂等重放，满队列下绝不误报 `storage_full`）；可合并 kind 在 dedupe key 未命中后经 `(tenant, agent, kind, coalesce_key)` 查找命中 pending 目标时**不新增行但可能增大 payload**（四轮复核修复：合并仅此一条路径——dedupe 命中永不合并，见 §4），此时按 `max(新 payload 字节 − 旧 payload 字节, 0)` 投影 bytes 增量（revision 更老不替换 payload，增量为零）——"不新增行"绝不等于"不新增压力"，字节硬阈值不能被合并路径穿透；真正新增行的入队照常全额判定（jobs+1、bytes+全量）。同键不同 payload 命中未决行在投影阶段即抛 `idempotency_key_reused`（见 §4），不进入压力判定。`storage_full` 只在硬阈值触发；safety lane 的配额只统计 safety lane 任务，独立于普通队列压力（ADR-0005 的优先通道）。磁盘硬阈值的恢复滞回为进程内状态：触发（低于 hard）后须回到 `hard + recovery_hysteresis_bytes` 之上才清除，滞回区间内保持 tripped 以杜绝阈值抖动。

### 8. Observation 容器引用必须构成单一 Scope 层级

`space_group_id`、`space_id`、`session_id` 不只是三个独立授权维度——它们必须能构造出**一个**合法 Scope（基线 §5.2，三轮复核修复）：

- **`session_id` 非空时 `space_id` 必须非空**：session 永远住在 space 里，请求级结构规则（`invalid_request`，整批零写入），并由契约 JSON Schema 的 `dependentRequired` 与双 SDK 校验器同步表达；
- **`space_group_id` 与 `space_id` 同时存在时必须命中真实绑定**：成员关系只存在于 append-only 的 `space_group_bindings` 历史——接受空间的当前活跃绑定，或覆盖记录 `occurred_us` 的历史绑定（解绑保留发生时归属，基线 §5.3：迟到观察仍可命名事实发生时空间所在的组；发生在解绑之后的事实不得再引用旧组）。`spaces.space_group_id` 列不是成员关系的事实来源。

不满足层级关系的记录是 `invalid_request`（请求体无法构造合法 Scope，持久化会污染之后所有按维过滤的读取），与横向授权检查同批执行、零写入。

## 后果

- Schema 3 引入 8 张表；0001/0002 字节不变；Outbox/Tick/Cursor 不得通过 Down Migration 删除或回拨。
- 错误码追加 `storage_full`、`cursor_gap`、`lease_held`、`lease_expired`、`lease_fenced`、`invalid_request`（域内实体化）。
- 备份恢复不变量扩展：enqueued tick 必有 outbox、completed job 必有完成时间且不残留租约字段、lease epoch 单调、每 agent 至多一个 active lease。
