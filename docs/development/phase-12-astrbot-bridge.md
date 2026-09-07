# 阶段 12：AstrBot Bridge

> 状态：Deferred（2026-09-06 按项目负责人要求暂时不再执行；前置裁决未完成，无 Bridge 实现）  
> 复核日期：2026-09-06  
> 前置阶段：[阶段 10](./phase-10-consolidation-reflection.md)；本阶段与 [阶段 11](./phase-11-bellis-adapter.md) 当前均暂缓  
> 目标：AstrBot Bridge 0.1.0；实际版本、Core/SDK 兼容范围待验收确定  
> 决策记录：ADR-0021 尚不存在；交付位置已定，其余裁决见工作包  
> 架构依据：[§27 AstrBot Bridge](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#27-astrbot-bridge)、[§28 SDK 与契约发布](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#28-sdk-与契约发布)、[§32.8 Adapter E2E](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#328-adapter-e2e)

## 阶段目标

本阶段暂缓，移出当前 pip 包与 Phase 14 稳定发布的前置门禁；不表示完成或永久取消。以下裁决、工作包和验收基线保留为恢复执行时的清单，当前不启动 ADR-0021、Bridge 实现或仅为 AstrBot 所需的 SDK 扩展。恢复须明确更新阶段状态、兼容范围及发布依赖；Core 通用协议与多宿主隔离不变量仍然有效。

通过 Python SDK 把 AstrBot 生命周期、稳定身份、群聊/私聊、Recall/Persona、记忆工具、实际发送效果和重启对账接入 Core。[预留目录](../../hosts/astrbot_plugin_iris_memory_api/) 当前只有 README，无插件代码、依赖声明、元数据、测试或版本矩阵；Core 和 Python SDK 已存在不等于 Bridge 已实施。

## 架构约束

- Core 发布物与依赖闭包不引入 AstrBot/QQ 类型、Hook 或平台 SDK；临时同仓布局须满足 [hosts 隔离不变量](../../hosts/README.md#隔离不变量)。实现只能依赖公共 SDK，不读取 Core 数据库。
- 用户消息在确认接收后提交；助手只在经裁决的实际效果边界提交。`on_llm_response` 和 `after_message_sent` 均不能直接视作发送成功证明。
- Realm 使用平台实例 `platform_id`；Space/Session/ExternalIdentity 从结构化平台、群和账号标识推导，不能以昵称或 `unified_msg_origin` 作为长期唯一键。
- Persona 与普通 Memory 分槽，Hash/Revision 必须校验；Required 模式无有效 Lease 时不 Recall、不调用回复模型、不注入上下文，也不提交在线聊天 Observation。
- 跨仓库 SDK 消费必须通过 registry 安装物验证，不用源码 alias 代替；包内门禁由本目录声明依赖独立运行。分发与拆分位置各自记账验收。

## 需求追踪

| 需求 ID | 未完成范围 | 验收依据 |
| --- | --- | --- |
| P12-DECISION-01 | 效果边界、交付拆分、SDK、映射、打包裁决 | ADR-0021 Accepted，保留核查事实与否决理由 |
| P12-SDK-01 | per-call deadline、真正取消、SSE、typed Persona/Cursor | 安装物符号、真实取消与断线续传测试 |
| P12-LIFECYCLE-01 | 加载、协商、注册、Persona、Lease、Ready、卸载 | 独立 CI、配置与版本消费矩阵 |
| P12-MAPPING-01 | 多实例、群/私聊、Session 与身份映射 | `unique_session` 翻转、重命名、隔离与稳定性案例 |
| P12-CONTEXT-01 | 可信注入、预算、Usage 与记忆工具 | 真实词表覆盖、权限/幂等/Revision 与错误映射 |
| P12-EFFECT-01 | 实际发送成功、失败、部分发送、审核与重复 Hook | 平台回执或等效信号及量化边界验证 |
| P12-RECOVERY-01 | Cursor、宿主队列、双向对账和补投 | 真实进程重启/强杀，无重复与漏交 |
| P12-SURFACE-01 | Off/Advisory/Required、Heartbeat/Release/Fencing | 模式、抢占、过期和非活动入口矩阵 |

## 工作包

### 12.0 前置裁决

ADR-0021 定稿前不进入 12.1。下列六项合并了旧文档散布的裁决清单，事实与未决事项不能互相替代。

| 裁决 | 当前事实及待定内容 |
| --- | --- |
| 12.0-1 实际效果边界 | 当前 AstrBot 普通/分段发送路径捕获 `event.send()` 异常后仍可触发 `OnAfterMessageSentEvent`；基类 `send()` 返回 None，无统一成功回执。需裁决包装逐次发送、采用平台回执、推动携带结果的宿主 Hook，或明确降级为尽力边界。未裁决前不得宣称失败发送的 Observation 为 0 |
| 12.0-2 交付位置 | 已定为 `hosts/astrbot_plugin_iris_memory_api/` 临时同仓、后转独立仓库；ADR 仍须写明拆分触发条件、负责人和截止时间盒。不得破坏 hosts 的隔离不变量 |
| 12.0-3 Python SDK 分发 | 当前 SDK 为 0.11.0，没有 Bridge 安装物/registry 消费证据。须选择分发通道及中间态退出条件；同仓布局不允许用源码 alias 或可编辑安装充当发布验收 |
| 12.0-4 SDK 能力归属 | 当前 `urlopen` + `asyncio.to_thread` 仅有构造级 5 秒超时，无真正取消、per-call deadline 或 SSE，Persona/Cursor 返回 dict。需裁决公共 SDK 升级或薄封装归属与版本规则 |
| 12.0-5 身份映射 | `unique_session` 可重写群 session_id，内置 builder 表外返回 None；同类型平台可有多个实例。固定结构化输入与 Realm 命名空间，明确历史绑定和人工导入语义 |
| 12.0-6 打包与门禁 | 兼容 AstrBot 插件元数据/requirements 形态，同时独立声明 lint/format/typecheck/test/build 工具。依赖来源确定后落地，并自动检查 SDK 安装物符号与 Core 隔离 |

工程约束保留原标识：**12.0-G1** 禁止 SDK 源码 alias；**12.0-G2** 发布物与消费符号一致、已占版本不覆盖；**12.0-G3** 包内门禁独立可跑。三者须落实 CI，不能仅留在文档。

### 12.1–12.5 实现与验收

1. **12.1 生命周期/SDK：**完成上述能力、分发与独立门禁；Load → Capability/Negotiate → 映射注册 → Persona → 可选 Lease → Ready。卸载停止新请求、Flush 已确认事件、保存重试元数据并释放 Lease。
2. **12.2 映射：**以 `(platform_id, message_type, group_id, sender_id)` 等稳定结构化标识建立映射；`unique_session` 翻转及昵称更新不能改变唯一键；绑定操作需显式授权。
3. **12.3 上下文/工具：**LLM 请求前 Recall，处理 Persona、Budget、Partial/Degraded、Watermark、Cache 和真实 Model-visible Usage；Remember/Correct/Forget/Note/Task 保留权限、幂等和 Expected Revision。候选映射以结构化类型为主键，用独立固定 Core 词表测试覆盖，不能只测试自造样例。
4. **12.4 效果/恢复：**用户接收、经裁决的发送效果、工具效果使用稳定事件 ID；失败/审核/未生效撤销进入失败记录。Cursor 对账必须包含真实补投和远端领先处理；仅记诊断不算完成。
5. **12.5 降级/活动入口：**完成 Core 超时、不兼容、Persona 未知与 Route 降级行为，Lease 的模式/抢占/Fencing；本地队列限制容量、TTL、权限和加密，不参与 Recall。

## 数据、契约与回退策略

发布顺序为 Core → Contract/Schema → SDK → Bridge。每个候选版本验证最小/当前/最大组合及安装物符号；不能继续把 Phase 10 的 Schema 11 视作当前 Core Schema 14 已兼容。回退前停止新 Hook、保存已确认事件与 Cursor、释放 Lease，再按稳定平台 Event ID 续传。

旧插件的历史消息可能记录于 `on_llm_response`，与实际发送边界不同。按 [ADR-0022](../adr/0022-management-console-plane.md) 由运营者导出并经 Phase 13 手动导入，差异需在映射和人工审核中保留；不连接旧库，不恢复已取消的双写/冻结/追平工具。

## 量化验收基线

以下全部为未来门禁，本轮没有 Bridge 测试或真实宿主 E2E。

- 最小/当前/最大 Core/API/Schema/SDK 组合覆盖成功、错误、未知字段/枚举、Partial/Degraded Fixture；Core 真实词表未映射数为 0。
- 在途 Recall 取消后底层 HTTP 在 100ms 内中断，连续 100 次残留请求为 0。
- 群/私聊、多 Realm、昵称更新、`unique_session` 翻转、Bot 重启各至少 200 个映射案例；ID 碰撞、跨 Space 泄漏、误合并和唯一键变化均为 0。
- 同一接收/发送效果/工具/Usage Hook 各重放 100 次，只形成一次逻辑效果。
- 若采用有成功证明的效果边界，发送失败、审核拦截、发送前 Hook 的 Assistant Observation 为 0；若 ADR 明确接受尽力边界，则在 200 次注入发送失败中公开实测误提交率，不能仍宣称为 0。
- Hook、入队、Cursor 持久化、Core 提交、卸载各边界，Bridge/Core 各至少 20 次崩溃恢复；已确认事件无重复/漏交，且至少 20 次验证补投真实发生。
- 真实 AstrBot + Core ASGI 进程的群聊/私聊各至少 100 Cycle，核验 Persona/Memory、Token Budget、Model-visible Usage、效果边界与三种 Lease 模式。

## 退出门禁

- [ ] ADR-0021 的六项裁决完成；SDK 分发与仓库拆分分别记录时间盒、负责人及关闭证据。
- [ ] SDK deadline/取消/SSE/typed 响应有测试；registry 安装物符号和独立 CI 通过，无源码 alias。
- [ ] hosts 隔离不变量有构建/依赖/导入/测试收集证据，Bridge 生命周期和配置完成。
- [ ] 映射、Persona/Memory、工具、效果边界、Usage 与 Lease 门禁通过。
- [ ] Cursor 对账含补投，真实双进程群/私聊 E2E 与崩溃恢复达到上述量化标准。
- [ ] 兼容/升级/回退与发布证据完整，两个中间态分别关闭；任何延期须明确范围和接受记录。

## 交付证据

2026-09-06 已执行只读检查：预留目录仅 README、ADR-0021 缺失；本地 AstrBot 的 `respond/stage.py`、`waking_check/stage.py`、`astr_message_event.py` 仍体现上列发送/Session 边界；Python SDK 客户端仍有上列传输限制；根构建、mypy、pytest 配置未收录宿主目录。

执行 `UV_CACHE_DIR=.uv-cache uv run pytest sdk/python/tests -q --no-cov` 得到 **2 passed**，仅证明 SDK Fixture 校验。没有运行 AstrBot Bridge 测试、宿主进程或平台真实发送，也没有重新查询公开 registry；旧文档的未发布/旧插件版本核查不能当作本次发布状态证明。

## 明确不做

不按昵称/文本相似自动绑定主体，不把生成完成当实际发送，不维护第二画像/向量/Persona 事实源，不以本地构建或 Core 测试替代安装物和宿主 E2E。

## 交接条件

[Phase 14](./phase-14-hardening-release.md) 必须把 Phase 12 裁决、实现和验收作为未完成前置工作，不能把“已有 Core/SDK”写成 Bridge 已完成，也不能在未明示范围调整时静默取消 AstrBot 交付。
