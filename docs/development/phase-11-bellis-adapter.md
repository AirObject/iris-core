# 阶段 11：Bellis Adapter

> 状态：Planned  
> 前置阶段：[阶段 10](./phase-10-consolidation-reflection.md)  
> 可与：[阶段 12](./phase-12-astrbot-bridge.md) 并行  
> 目标版本：Bellis Adapter 0.1.0（Core API/Schema v1 兼容范围在 Phase 10 冻结）  
> 架构依据：[§26 Bellis Adapter](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#26-bellis-adapter)、[§28 SDK 与契约发布](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#28-sdk-与契约发布)、[§32.8 Adapter E2E](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#328-adapter-e2e)、[§36 阶段 11](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#阶段-11bellis-adapter)

## 阶段目标

在独立 `iris-memory-bellis-adapter` 仓库中完成 Bellis 与 Core 公共协议的薄映射，打通用户事件、Recall、可信 Persona Slot、ContextBlock、Usage、实际生效输出和重启对账的端到端闭环。

## 架构约束

- Adapter 只依赖发布的 TypeScript SDK、Schema、Fixture 和 HTTP/Event 契约，不复制 Core Domain Model 或访问 SQLite/FAISS 文件。
- Scene 只有在持久化 Commit 且输出开始生效后才形成 Assistant Observation；Cancel/未播放内容不得提交。
- 普通 Candidate 不能升级为 System/Persona 内容；Persona Revision/Hash 必须通过专用 Schema 校验。
- Core 不可用时只使用 Bellis 自有 Session 短期上下文，不伪造长期记忆或未知 Persona。
- Usage Report 重试不能阻塞当前回复，并且必须准确反映真正 Model-visible 的 Candidate。

## 需求追踪

| 需求 ID | 基线要求 | 工作包 | 验证门禁 |
| --- | --- | --- | --- |
| P11-CONTRACT-01 | 独立仓库、SDK 支持范围、Capability 与 Consumer Contract | 11.1 | 最小/最大版本 Fixture 与 CI 矩阵 |
| P11-CONTEXT-01 | Recall Candidate 到 ContextBlock 的无损显式映射 | 11.2 | 字段、Hash、预算、Partial 与降级测试 |
| P11-PERSONA-01 | 专用可信 Persona Slot、Revision/Hash 与失效 | 11.3 | 缓存、通知、离线重连和安全优先级测试 |
| P11-EFFECT-01 | Scene Commit/Cancel/Partial 与实际效果 Observation 边界 | 11.4 | 生命周期、工具效果、Cursor 和幂等测试 |
| P11-USAGE-01 | Usage 精确反映 Model-visible 且异步重试 | 11.4 | 子集、失败恢复、本地 Outbox 和隐私测试 |
| P11-RECOVERY-01 | Core/Adapter 重启、Lease 与显式降级 | 11.5 | 对账、Fencing、模式矩阵和 E2E |

## 工作包

### 11.1 独立仓库与契约

- 建立独立仓库、TypeScript SDK 版本范围、Capability Negotiation、Consumer Contract 和 CI。
- 定义 Bellis Session/Scene/Audience/State 到 Tenant/Agent/SpaceGroup/Space/Session 的配置映射。
- 对最小和最大支持的 Core API/Schema 版本运行固定 Fixture。

### 11.2 Recall 与 ContextBlock

- 在 Bellis Deadline 内调用 Recall，映射 Candidate ID/Revision/Hash/Category/Placement/Token/Privacy/Source/Expiry。
- 保留 Core 排序与类别的显式语义，不从文本猜测 Placement 或 Priority。
- 处理 Completed/Degraded Routes、Partial、Minimum Watermark、Cache Until 和 Next Wake。

### 11.3 Persona Slot

- 获取和缓存 `(agent_id, persona_revision, content_hash)`，注入 Bellis 预留可信 Persona Slot。
- 监听 Revision Invalidated/Persona Revised；版本或哈希不一致立即丢弃缓存。
- Persona 不覆盖 Bellis 安全层、工具权限和输出策略。

### 11.4 Observe、State 与 Usage

- 将已提交 Session Record、实际生效输出、确认工具效果和 State Stream 映射到 Observation/StateRecord。
- Commit Log 使用稳定 Event ID、Source Stream/Cursor、Idempotency Key；Partial Output 保存确认片段范围。
- 回传 returned/host_selected/model_visible，失败进入有界本地 Outbox。

### 11.5 恢复、降级与可选 Lease

- Adapter Crash 后从 Core Cursor 与 Bellis Commit Log 对账。
- 实现 Off/Advisory/Required Active Surface 行为、Heartbeat/Fencing 和抢占停止。
- Core/Vector/Persona/版本协商故障采用显式降级，记录低敏诊断。

## 数据、契约与回退策略

- 本阶段代码、配置、Fixture 和发布物只进入独立 `iris-memory-bellis-adapter` 仓库；Core Monorepo 只维护公共 SDK/Schema/Mock Server 与兼容矩阵，不引入 Bellis 类型或运行时依赖。
- Adapter 本地只持久化有界重试元数据、Cursor/Commit 对账位置和已验证 Persona Cache Key，不形成第二 Canonical Store；Payload 设置容量、TTL、文件权限和可选静态加密。
- 建立 Bellis 事件/Scene/State 与 Core Scope/Observation/State/Usage 的版本化映射表；Consumer Contract 同时对 Adapter 支持范围的最小、最大和当前 Core Patch 运行。
- 发布顺序为兼容 Core/Schema/SDK → Adapter；新增 Core 可选字段或能力先协商后启用。未知必需 Schema、Persona Hash 不符或 Required Lease 无效时 Fail Closed，不猜测映射。
- 回退优先回滚 Adapter 至仍在兼容矩阵内的版本；切换前停止新 Cycle、Flush 已确认事件并保存 Cursor。Core 回退不得早于仍被 Adapter 使用的最小版本，本地队列按稳定 Event ID 继续幂等提交。

## 量化验收基线

- Consumer Contract 必须覆盖支持范围的最小、最大和当前 Core API/Schema/SDK 组合；所有固定成功、错误、未知可选字段、未知枚举和降级 Fixture 结果一致。
- 同一 Commit/Partial Segment/Tool Effect/Usage 事件重放 100 次，只产生一次 Core 逻辑效果；Cancel、未播放、失败和未生效输出的 Assistant Observation 数必须为 0。
- 在 Observe 前后、Cursor 持久化前后、Usage 入队前后和 Lease 抢占时各执行至少 20 次 Adapter/Core 崩溃恢复，对账后重复或漏交的已确认事件数必须为 0。
- Off/Advisory/Required、Acquire/Heartbeat/Preempt/Expiry/Fencing 每个组合至少运行 20 次；旧 Epoch 的在线请求成功数必须为 0，Required 无 Lease 时不得进入回复链路。
- Bellis E2E 连续运行至少 100 个 Cycle，逐 Cycle 验证 Persona Revision/Hash、ContextBlock Hash、Token Budget、Model-visible Usage 与实际输出 Observation 闭环。

## 退出门禁

- [ ] ContextBlock 每个字段、Content Hash、类别、Token 和 SourceRefs 通过 Consumer Contract。
- [ ] Scene Commit/Cancel/Partial/未播放和工具成功/失败边界测试通过。
- [ ] Persona Slot Revision/Hash、失效通知、离线重连和安全优先级测试通过。
- [ ] Deadline、Partial、Vector 降级、Core 超时和不兼容版本行为符合契约。
- [ ] Adapter/Core 任一侧重启后 Cursor 对账无重复或漏交 Observation。
- [ ] 用户事件 → Recall/Persona → Model-visible Usage → 实际输出 Observation 的 Bellis E2E 通过。
- [ ] 版本矩阵、本地状态迁移、升级/回退方案、需求追踪和交付证据已完成评审。

## 交付证据

- Adapter 仓库/版本：待补充
- Core/SDK/Schema 兼容矩阵：待补充
- Consumer Contract：待补充
- E2E/恢复报告：待补充
- 已知限制：待补充

## 明确不做

- 不在 Adapter 内实现第二套记忆、画像、Persona 或检索排序。
- 不直接拼接最终 Prompt 或绕过 Bellis 的安全/输出编排。
- 不提交生成但未生效、取消或失败的输出。

## 交接条件

Phase 13 可以把 Bellis 切换和 Cursor 追平纳入迁移运行手册；Phase 14 可以将本 Adapter 纳入版本矩阵、升级和回滚演练。
