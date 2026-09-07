# 阶段 11：Bellis Adapter

> 状态：Deferred（2026-09-06 按项目负责人要求暂时不再执行；已有插件与历史离线验证保留，宿主接线、兼容与发布验收未完成）  
> 复核日期：2026-09-06  
> 前置阶段：[阶段 10](./phase-10-consolidation-reflection.md)  
> 交付版本：`@iris-memory/bellis-provider` 0.1.0；TypeScript SDK 0.11.1  
> 决策记录：[ADR-0020](../adr/0020-bellis-adapter-plugin-seam.md)  
> 架构依据：[§26 Bellis Adapter](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#26-bellis-adapter)、[§28 SDK 与契约发布](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#28-sdk-与契约发布)、[§32.8 Adapter E2E](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#328-adapter-e2e)

## 阶段目标

本阶段暂缓，连同 Phase 12 移出当前 Core pip 包及 Phase 14 稳定发布的前置门禁。已有实现、ADR-0020 与历史验证证据保留，不表示完成或永久取消；下列未关闭任务只在明确恢复本阶段后执行，届时重新冻结 Core/Schema/SDK 兼容矩阵与发布依赖。当前发布不声明 Bellis 适配器支持，也不启动宿主接线、插件分发或仅为 Bellis 所需的 SDK 扩展。

经公共 SDK 把 Bellis 用户事件、Recall、可信 Persona、ContextBlock、Usage 和实际生效输出接入 Core。插件已在 Bellis `providers/memory-iris/` 实现；存在插件不代表宿主运行时闭环已验收。

## 架构约束

映射、可信槽位和插件分发以 [ADR-0020](../adr/0020-bellis-adapter-plugin-seam.md) 为准；Observe/ACK 另须落实 Bellis 已接受的 ADR 0006（`docs/adr/0006-documentation-and-delivery-boundaries.md`，2026-09-06）修订。Core 不引入 Bellis 类型或依赖；普通记忆不能升级为系统指令。Scene Commit 是播放前意图，实际效果由独立 effect/progress 确认事实证明；取消或失败后仍保留已确认前缀，未确认、未播放内容不能形成 Assistant Observation。

## 需求追踪

| 需求 ID | 当前交付 | 剩余验收 |
| --- | --- | --- |
| P11-CONTRACT-01 | 独立插件包、SDK 安装物消费与兼容矩阵 | 当前 Core Schema 14 兼容；公开分发；宿主 CI；最小/当前/最大组合 |
| P11-CONTEXT-01 | 以 resource type 为主键的字段/类别映射、预算与宿主过滤 | 真实 Core 候选与 Context Builder E2E；审计字段/Hash；分类覆盖边界与矩阵策略 |
| P11-PERSONA-01 | Hash 校验、可信结构、缓存与失效逻辑 | 宿主槽位接线；撤销/Hash 失败禁用旧缓存；刷新失败恢复与安全优先级 E2E |
| P11-EFFECT-01 | 已确认输出/工具效果过滤；Core 成功后才确认交付 | 独立 effect/progress ACK、连接代际/片段范围及取消后已确认前缀闭环 |
| P11-USAGE-01 | Usage 子集校验；远端失败返回宿主 | 宿主 Outbox 持久化与异步重试，不阻塞当前回复 |
| P11-RECOVERY-01 | Lease 模式、旧队列兼容、Cursor 诊断 | 双向对账补投、进程崩溃恢复及完整 Fencing 矩阵 |

## 工作包

原 11.1–11.5 的实现清单已合并为上表；不再重复安排已有代码。以下未关闭工作随本阶段暂缓，不再由 [Phase 14](./phase-14-hardening-release.md) 排期或作为 Core 发布条件；恢复后按实际验收更新状态。

1. 更新 Provider 默认协商范围和兼容矩阵并验证 Schema 14；当前最大值为 11，默认配置无法正常协商当前 Core 0.12.0 / Schema 14。
2. 完成 Bellis Memory Gateway、Context Builder、Persona Slot、持久化 Outbox 与运行时接线；独立确认事实绑定 Session、连接代际、Scene/Cue 和 segment 范围，验证乱序/重复/取消后已确认前缀。实现 Cursor 补投、远端领先处理与 Persona 撤销/刷新失败恢复。
3. 保留无 SDK 源码 alias 的 registry 消费，完成公开 npm 发布及宿主 workspace/CI 中间态退出；补齐映射审计和 categoryMap 覆盖边界，执行真实双进程与故障矩阵，形成可复现交付证据。

## 数据、契约与回退策略

- 当前新 Observe/Usage 请求直接等待 Core 返回；失败由宿主负责持久化重试。Adapter 仅保留历史 pending 队列兼容、Cursor 和经校验的 Persona 快照，不能当作已完成的宿主 Outbox。
- 发布顺序、SDK 安装物验证及中间态退出遵循 ADR-0020。回退前停止新 Cycle、保存已确认事件与 Cursor，随后回滚至已验证的兼容组合。
- 当前矩阵仍指向 Core 0.11.x / Schema 11；不能只上调配置上限后宣称兼容。最低、当前、最高版本须有真实消费结果。

## 量化验收基线

以下是尚需完成的验收要求，离线测试数不能替代：

- 支持范围的最小/当前/最大 Core、Schema、SDK 组合均通过成功、错误、未知字段/枚举和降级 Fixture。
- 同一 Commit、Partial Segment、Tool Effect、Usage 各重放 100 次，只形成一次逻辑效果；未播放、未生效内容及取消/失败中的未确认内容产生的 Assistant Observation 为 0；已确认前缀以 Partial 保留，不能因最终取消/失败而丢失。
- Observe 前后、Cursor 持久化前后、Usage 入队前后及 Lease 抢占窗口中，Adapter/Core 各至少 20 次崩溃恢复，已确认事件重复与漏交均为 0。
- Off/Advisory/Required 与 Acquire/Heartbeat/Preempt/Expiry/Fencing 的适用组合各至少 20 次；旧 Epoch 成功数为 0，Required 无有效 Lease 不进入回复链路。
- 真实 Core ASGI + Bellis Runtime 连续至少 100 Cycle，逐次核对 Persona Revision/Hash、ContextBlock、Token Budget、Model-visible Usage 和实际输出 Observation。

## 退出门禁

- [x] 当前包内 typecheck/lint/format/test 通过；23 个离线用例，SDK 经已安装包解析，无 Core 源码 alias。
- [ ] 当前 Core/Schema 兼容、真实候选词表、最小/当前/最大消费矩阵完成。
- [ ] 宿主接线、Scene/Persona/Usage E2E 完成；插件通过不替代宿主通过。
- [ ] Cursor 双向对账与补投、完整重启和 Lease 故障矩阵完成。
- [ ] SDK 公开发布、`providers/*` 并入宿主 workspace、`link:` 替换及宿主 CI 门禁完成。
- [ ] 安装物 build/pack、升级/回退、版本矩阵和发布证据完成本次候选版本复核。

## 交付证据

[Phase 11 验证报告](../reports/phase-11-verification.md) 集中保存本次实测、历史记录与未完成项。2026-09-06 实测：Provider 23 tests，TypeScript SDK 18 tests；当前 Provider 构建和双进程 E2E 未在本轮重跑。

## 明确不做

不在 Adapter 内建立第二记忆库、复制 Core 检索/画像逻辑、拼接最终 Prompt 或绕过宿主安全与输出编排。

## 交接条件

本阶段保持 Deferred，直至项目负责人明确恢复；恢复时核对现有插件与届时 Core 公共接口、版本、凭据及宿主确认语义，重新安排真实 E2E 和分发验收。Phase 14 当前只验收 Core，既不承接本页宿主待办，也不把它们标为完成。Phase 13 管理控制台继续按 [ADR-0022](../adr/0022-management-console-plane.md) 审阅、导出和手动导入数据；它不依赖本 Adapter 完成。
