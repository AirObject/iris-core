# Phase 11 验证报告：Bellis Adapter

> 状态：In progress  
> 当前复核：2026-09-06；历史实现批次：2026-09-05  
> 当前 Core 工作树：0.12.0 / API v1 / Schema 14 / Contract 1.9.0；以下历史验证执行于 Schema 13，后续 0014 见[联合修复报告](phase-05-06-07-08-review-fixes.md)。  
> Provider：Bellis `providers/memory-iris/`，`@iris-memory/bellis-provider` 0.1.0  
> TypeScript SDK：0.11.1  
> 阶段与门禁：[Phase 11](../development/phase-11-bellis-adapter.md) · 决策：[ADR-0020](../adr/0020-bellis-adapter-plugin-seam.md)

本报告区分当前实测、源码检查和历史证据。两个仓库均含未提交工作，因此本次结果属于当前工作树，不能作为已发布版本证明。未启动真实 Core + Bellis Runtime；未进行公开 registry 发布或重新核查公开 registry 状态。

## 当前交付与源码核查

| 范围 | 2026-09-06 核查结果 |
| --- | --- |
| Core SDK | Adapter 读接口支持 AbortSignal；Persona/Cursor 具备类型；SSE 支持有限轮询与 Last-Event-ID；Recall/Usage 返回值执行契约校验 |
| Bellis 公共边界 | `@bellis/contracts/memory` 与 Testkit Conformance Harness 存在；Core 无 Bellis 运行时依赖 |
| Candidate 映射 | 默认以 `resource_ref.resource_type` 为主键；已知类型下新 category 保留并审计，未知类型丢弃；宿主过滤存在。`categoryMap` 覆盖的边界缺口见下文 |
| Persona | Hash 复核、结构化快照、缓存失效、期限和状态处理存在 |
| 实际效果 | Provider 过滤 committed/partial、非空助手正文及 tool 的 `effectApplied=true`；可信实际确认事实仍须由宿主新协议证明，不能从 Scene Commit 推定 |
| Observe/Usage 交付 | 新调用直接等待 Core 成功；远端失败返回宿主 Outbox。旧报告“新请求进入 Adapter 本地重试 Outbox”的表述不再适用于当前代码 |
| 本地状态 | Cursor、Persona 缓存与历史 pending；容量、TTL、owner-only 文件权限、原子替换和可选 AES-256-GCM；过期旧 pending 保留待对账，不静默丢弃 |
| 恢复 | `reconcile()` 仅在本地 cursor 领先时记录 `cursor.remote_behind`；无补投，也未处理远端领先 |
| 宿主集成 | Provider 存在；当前 Bellis 运行时代码尚无 Memory Gateway/Context Builder 接线，Provider 仍不在根 workspace |

## 本次执行的针对性验证

| 位置 | 命令 | 结果与范围 |
| --- | --- | --- |
| Bellis `providers/memory-iris/` | `pnpm typecheck` | 通过；消费已安装的 `@iris-memory/sdk`，未使用 Core 源码 alias |
| 同上 | `pnpm lint`、`pnpm format:check` | 通过 |
| 同上 | `pnpm test` | 2 files / **23 passed**；FakeIrisClient、Conformance、映射、效果、交付确认、Persona、旧队列与 Required Lease 边界 |
| Core `sdk/typescript/` | `npm test` | 构建成功，**18 passed**；Fixture、wire、AbortSignal 和有限 SSE |
| 同上 | `npm run typecheck` | 通过 |
| Core 根目录 | `UV_CACHE_DIR=.uv-cache uv run pytest sdk/python/tests -q --no-cov` | **2 passed**；仅 Python SDK 共享 Fixture，不能作为 AstrBot 验收 |

Provider 的 `pnpm build`、仓库全量 CI、安装后的重新解析/打包、发布、真实双进程 E2E 和强杀矩阵未在本轮执行。Python 测试首次使用默认 uv cache 因目录权限失败，改用仓库缓存后通过。

## 未关闭项与当前基线差异

1. **当前 Core 兼容缺口。** Provider 默认最小/最大 Schema 均为 11；`compatibility-matrix.json` 指向 Core 0.11.x / Schema 11。当前 Core 是 0.12.0 / Schema 14，默认协商会拒绝它；存在静态 Persona 降级也不代表已兼容。需要真实消费测试与矩阵更新，不能仅改配置上限。
2. **分类策略和覆盖边界。** 矩阵 `unknownCoreCategories` 仍是 `drop-and-diagnose`，与默认已知 resource type 下保留未知 category 的行为不符。`mapping.ts` 中 `categoryMap` 优先于类型检查，配置覆盖可让未知类型通过，并可产生 viewer；因此 ADR 的“未知类型恒失败关闭/viewer 恒不产生”尚不能覆盖所有配置。
3. **宿主注册点、实际效果与可靠交付未闭合。** Memory Gateway、Context Builder、Persona Slot、宿主 Outbox 及运行时注册仍待交付。Bellis 2026-09-06 ADR 0006 §3/§4 已将 Scene Commit 与独立 effect/progress 确认分开：确认绑定 Session、连接代际、Scene/Cue 和 segment 范围；取消/失败仍保留已确认前缀，只有未确认内容不得提交。当前 Provider 的远端失败传播测试不能证明此协议或宿主持久化重试完成。
4. **Cursor 对账没有补投。** 诊断不能满足“已确认事件无重复/漏交”；需要与宿主 Commit Log 和双进程崩溃窗口一起验收。
5. **分发中间态未关闭。** 本地 scope registry、`link:` 宿主依赖和独立 Provider 目录仍存在，根 workspace 不含 `providers/*`。2026-09-05 记录的公开 npm 未发布状态在本轮没有网络复查，当前没有可用的公开发布验收证据。
6. **真实 E2E/恢复矩阵未执行。** 未有真实 Core ASGI + Bellis Runtime 连续 100 Cycle、各崩溃窗口每侧 20 次、完整 Lease/Fencing 组合和最小/当前/最大消费矩阵的证据。

7. **审计与来源 Hash 尚未闭环。** ADR 要求的 scores/final_score/scope/subject_entity_id 未单独进入当前返回 audit；现有 audit 只有丢弃/过滤/未知类别信息与可选 trace。Candidate contentHash 仅透传，宿主 Context Builder 的来源校验没有接线证据。
8. **Persona 撤销与刷新失败恢复待补。** `#loadInitialPersona()` 对 refresh 错误统一回退旧缓存，未区分网络不可达、revoked 和 Hash 不符。事件轮询先推进 cursor，再失效缓存并刷新；刷新失败后没有可靠完成该次刷新的证据。这些是源码检查发现的未闭合路径，本轮未新增故障测试，不能宣称撤销失败关闭和断线追平已验收。

这些项目进入 [Phase 14](../development/phase-14-hardening-release.md) 的前置闭环；关闭前 Phase 11 保持 In progress。

## 历史证据与已替代结论

以下保留历史批次的可追溯结果；未按当前工作树重新宣称通过。

| 2026-09-05 记录 | 当时结果 | 当前适用边界 |
| --- | --- | --- |
| 批次 1 Provider | 12 tests；本地 Outbox 重启/幂等模拟 | 最初 SDK/Vitest alias 掩盖实际安装物问题；交付语义随后变化，不能替代当前交付验证 |
| 批次 2 Provider | 包内 lint/format/typecheck/test/build；21 tests | 已删除 SDK/Vitest alias，消费本地 registry SDK 0.11.1；当前用例为 23 |
| Bellis 全量 | `pnpm check && pnpm build` 通过 | 只代表当时宿主基线，未包含真实 Memory Runtime 接线 |
| Core 全量 | 完整 pytest **10338 passed**；format/lint/typecheck/contract/compatibility 通过 | Core 0.11.0 / Schema 11 基线；先前 54 个 loopback setup error 经允许端口的同例重跑闭合 |
| SDK | TypeScript 18 passed；pack 4 files / 22.3 kB；本地 registry 发布 0.11.1 | 本地分发证据，不是公开 npm 发布或当前 Core Schema 13 兼容证明 |

当前 ACK/实际效果解释还参照 Bellis `docs/adr/0006-documentation-and-delivery-boundaries.md`（2026-09-06，Accepted）。它替代旧 ADR 0005 中 Scene Commit 代表生效、取消/失败全部丢弃的解释，并明确远端持久接收才算 ACK、Adapter 内存入队不算确认；宿主对应实现仍待完成。

历史修复集中保存在 [ADR-0020 §12](../adr/0020-bellis-adapter-plugin-seam.md#12-修订2026-09-05落地复核后)。修复内容包括：从 category 主键改为 resource type，覆盖 Core 的 23 个类别取值并补宿主过滤；移除 SDK 源码 alias，修正 SDK 实际安装物并推进到 0.11.1；移除 Vitest 双实例 alias，补齐包内工具依赖。旧的逐批复述与已经推翻的结论合并至本节，不再作为当前规范重复维护。
