# 阶段 12：AstrBot Bridge

> 状态：Planned  
> 前置阶段：[阶段 10](./phase-10-consolidation-reflection.md)  
> 可与：[阶段 11](./phase-11-bellis-adapter.md) 并行  
> 目标版本：AstrBot Bridge 0.1.0（Core API/Schema v1 兼容范围在 Phase 10 冻结）  
> 架构依据：[§27 AstrBot Bridge](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#27-astrbot-bridge)、[§28 SDK 与契约发布](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#28-sdk-与契约发布)、[§32.8 Adapter E2E](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#328-adapter-e2e)、[§36 阶段 12](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#阶段-12astrbot-bridge)

## 阶段目标

在独立 `iris-memory-astrbot-bridge` 仓库中完成 AstrBot 生命周期、QQ 群/私聊、稳定身份、Recall/Persona、记忆工具、实际发送回调和重启对账的薄桥接闭环。

## 架构约束

- Bridge 只依赖发布 SDK/Schema/Fixture；平台昵称、群名片和 Session ID 不能成为长期 Person 主键。
- 用户消息在框架确认接收后提交；助手消息只在 `after_message_sent` 或等价成功回调提交。
- `on_llm_response`、生成完成或发送前 Hook 不是实际效果边界。
- Persona 与 Memory 分别进入框架允许的可信槽位，普通记忆不能覆盖系统或安全指令。
- Required Coordinator 下无有效 Lease 时不请求回复 Recall、不调用回复模型、不注入上下文也不提交在线聊天 Observation。

## 需求追踪

| 需求 ID | 基线要求 | 工作包 | 验证门禁 |
| --- | --- | --- | --- |
| P12-LIFECYCLE-01 | 独立仓库、加载/协商/Ready/卸载与 Consumer Contract | 12.1 | 生命周期、配置、版本矩阵和 CI |
| P12-MAPPING-01 | Bot/平台/群私聊/Session/ExternalIdentity 稳定映射 | 12.2 | 命名空间、重命名、历史绑定和隔离测试 |
| P12-CONTEXT-01 | Recall/Persona 可信注入、Usage 与记忆工具契约 | 12.3 | 顺序、预算、权限、Revision 和错误映射 |
| P12-EFFECT-01 | 用户接收与 `after_message_sent` 实际效果边界 | 12.4 | 成功/失败/重试/重复 Hook/审核测试 |
| P12-RECOVERY-01 | Cursor、本地队列、Core/Bridge 重启与对账 | 12.4–12.5 | 故障注入、容量、TTL 和幂等测试 |
| P12-SURFACE-01 | Off/Advisory/Required 与旧 Epoch Fencing | 12.5 | 模式矩阵、抢占和非活动入口测试 |

## 工作包

### 12.1 独立仓库与生命周期

- 建立独立仓库、Python SDK 版本范围、配置/Secret 引用、Consumer Contract 和 CI。
- 实现 Load → Capability/Negotiate → 映射注册 → Persona → 可选 Lease → Ready。
- 卸载时停止新请求、Flush 已确认 Observation、尽力提交 Usage、释放 Lease 并保留本地重试元数据。

### 12.2 Space、Session 与 Identity 映射

- 对 Bot Instance、协议/平台 Realm、群聊、私聊和会话建立稳定映射。
- 使用平台稳定账号构建 ExternalIdentity；昵称/群名片只作为版本化属性。
- 明确 QQ Group、Private、SpaceGroup 和历史绑定的配置/管理边界。

### 12.3 Recall、Persona 与工具

- 在 LLM 请求前调用 Recall，并按 Token Budget 注入可信 Persona 和结构化 Memory。
- 回传实际 Model-visible Candidate，处理 Partial/Degraded/Cache/Watermark。
- 实现 Remember/Correct/Forget/Note/Task 工具的权限、幂等键、Expected Revision 和用户可理解错误映射。

### 12.4 事件提交与 Cursor

- 用户接收与 `after_message_sent` 分别生成稳定事件 ID；重复 Hook 幂等。
- 发送失败、审核拦截和未生效撤销只进入本地/审计失败记录。
- 平台支持时使用 Source Cursor；重启后对 Core Cursor 与本地已确认事件对账。

### 12.5 降级与活动入口

- 实现 Core 超时/不可用、版本不兼容、Persona 未知和 Route 降级的显式宿主行为。
- 实现 Off/Advisory/Required、Acquire/Heartbeat/Release、Lease 抢占与旧 Epoch Fencing。
- 本地重试队列有容量、TTL、加密/权限和去重，不形成第二事实源。

## 数据、契约与回退策略

- 本阶段代码、配置、Fixture 和发布物只进入独立 `iris-memory-astrbot-bridge` 仓库；Core 不引入 AstrBot/QQ 类型、Hook 或平台 SDK。
- Bridge 本地只保存有界重试元数据、平台事件 ID、Cursor 对账位置和已验证 Persona Cache Key；设置容量、TTL、最小文件权限和 Secret 引用，不能从本地队列参与 Recall。
- 明确版本化映射 Bot Instance、Provider/Realm、Group/Private、Session 与 ExternalIdentity；昵称、群名片和显示属性更新只形成属性 Revision，不改变唯一键。
- Consumer Contract 对支持范围的最小、最大和当前 Core Patch 运行；记忆工具完整暴露 Idempotency、Expected Revision、Scope/Privacy 与稳定错误，不用宿主默认值扩大权限。
- 回退前停止新 Hook、Flush 已确认事件、持久化 Cursor 并释放 Lease；回滚到兼容 Bridge 后按稳定平台 Event ID 续传。未知必需 Schema、Persona 无可信缓存或 Required 无 Lease 时禁止进入回复链路。

## 量化验收基线

- Consumer Contract 覆盖最小、最大和当前 Core API/Schema/SDK 组合，以及合法、错误、未知可选字段、未知枚举、Partial/Degraded Fixture。
- 群聊/私聊、不同平台 Realm、重命名昵称/群名片和 Bot 重启各至少生成 200 个映射案例；跨 Realm ID 碰撞、跨 Space 泄漏和昵称触发主体合并数必须为 0。
- 同一用户接收/发送成功/工具效果/Usage Hook 重放 100 次只产生一次逻辑效果；发送失败、审核拦截、发送前 Hook 的 Assistant Observation 数必须为 0。
- 在 Hook、入队、Cursor 持久化、Core 提交和卸载各边界至少执行 20 次 Bridge/Core 崩溃恢复，对账后重复或漏交的已确认事件数必须为 0。
- 群聊与私聊 E2E 各连续运行至少 100 个 Cycle，验证 Persona/Memory 注入、Token Budget、Model-visible Usage、`after_message_sent` Observation 和 Off/Advisory/Required 行为。

## 退出门禁

- [ ] 群聊/私聊、账号命名空间、Bot Instance 和 Session 映射契约通过。
- [ ] 用户接收、发送成功、失败、重试、重复 Hook 和审核拦截边界测试通过。
- [ ] Persona/Memory 注入顺序、Token Budget、Revision/Hash 和安全优先级测试通过。
- [ ] 工具权限、幂等、Revision Conflict 和错误映射测试通过。
- [ ] Bridge/Core 重启后的 Cursor 对账无重复或漏交。
- [ ] 用户消息 → Recall/Persona → Usage → `after_message_sent` Observation 的群聊/私聊 E2E 通过。
- [ ] 版本矩阵、本地状态迁移、升级/回退方案、需求追踪和交付证据已完成评审。

## 交付证据

- Bridge 仓库/版本：待补充
- Core/SDK/Schema 兼容矩阵：待补充
- Consumer Contract：待补充
- E2E/恢复报告：待补充
- 已知限制：待补充

## 明确不做

- 不用昵称、头像、文本相似或单条声明自动绑定跨平台主体。
- 不在发送前保存 Assistant Observation。
- 不在 Bridge 内维护长期画像、向量索引或 Persona Current。

## 交接条件

Phase 13 可以把 AstrBot 切换、双写/冻结和 Cursor 追平纳入迁移运行手册；Phase 14 可以将 Bridge 纳入兼容、升级和故障演练。
