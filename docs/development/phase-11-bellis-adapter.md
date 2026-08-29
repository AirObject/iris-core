# 阶段 11：Bellis Adapter

> 状态：Planned  
> 前置阶段：[阶段 10](./phase-10-consolidation-reflection.md)  
> 可与：[阶段 12](./phase-12-astrbot-bridge.md) 并行  
> 架构依据：[Bellis Adapter、SDK 发布与阶段 11](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md)

## 阶段目标

在独立 `iris-memory-bellis-adapter` 仓库中完成 Bellis 与 Core 公共协议的薄映射，打通用户事件、Recall、可信 Persona Slot、ContextBlock、Usage、实际生效输出和重启对账的端到端闭环。

## 架构约束

- Adapter 只依赖发布的 TypeScript SDK、Schema、Fixture 和 HTTP/Event 契约，不复制 Core Domain Model 或访问 SQLite/FAISS 文件。
- Scene 只有在持久化 Commit 且输出开始生效后才形成 Assistant Observation；Cancel/未播放内容不得提交。
- 普通 Candidate 不能升级为 System/Persona 内容；Persona Revision/Hash 必须通过专用 Schema 校验。
- Core 不可用时只使用 Bellis 自有 Session 短期上下文，不伪造长期记忆或未知 Persona。
- Usage Report 重试不能阻塞当前回复，并且必须准确反映真正 Model-visible 的 Candidate。

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

## 退出门禁

- [ ] ContextBlock 每个字段、Content Hash、类别、Token 和 SourceRefs 通过 Consumer Contract。
- [ ] Scene Commit/Cancel/Partial/未播放和工具成功/失败边界测试通过。
- [ ] Persona Slot Revision/Hash、失效通知、离线重连和安全优先级测试通过。
- [ ] Deadline、Partial、Vector 降级、Core 超时和不兼容版本行为符合契约。
- [ ] Adapter/Core 任一侧重启后 Cursor 对账无重复或漏交 Observation。
- [ ] 用户事件 → Recall/Persona → Model-visible Usage → 实际输出 Observation 的 Bellis E2E 通过。

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
