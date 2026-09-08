# main 本轮实现概要

核查日期：2026-09-08。本文集中说明功能、实现模块和对外接入面，便于快速了解本轮交付。

**范围**：已提交基线为本地 `main@7629152`，相对本地记录的 `origin/main@e2a6bbd` 增加 42 个提交；另包含当前工作区尚未提交的插件内嵌接入、异步 SDK 和 Observation 上下文实现。下文明确区分两者。当前源码版本为 Core **0.16.0**、Schema **25**、业务契约 **1.13.0**、Console 契约 **1.3.0**、Python/TypeScript SDK **0.12.0**，属于未发布的开发候选。

本次分支整理已切换到 `main` 并删除本地 `dev/chores`；两者原先指向同一提交，没有丢弃独有提交，原有未提交修改保留。远端状态未重新拉取，未执行提交或推送。

## 功能

Core 是独立于宿主的认知记忆服务：以 SQLite 保存权威记录，通过可重建索引提供检索，并用持久后台任务推进整理、调度与清理。

| 能力 | 当前实现 |
| --- | --- |
| 身份与权限 | Tenant、Agent、Space、Session 和外部账号绑定；按 Scope、Privacy、主体授权及用途限制读写，支持 Surface Lease 协调在线调用。 |
| 信息接入与工作记忆 | Observation 批量幂等写入与来源游标；Recent 近期上下文、带 TTL 的 State、可晋升的 Focus 和 Note。 |
| 长期记忆 | Claim、Evidence、Episode、Relation、Artifact；支持来源追溯、修订历史、更正、争议、归档和遗忘。 |
| 检索与画像 | Recall 组合近期信息、权威记忆、FTS、可选 Vector、Profile 和 Graph；检查权限、版本与删除状态，返回来源和降级信息，支持结果重验与使用反馈。 |
| 任务与主动事件 | Task、步骤、依赖、触发器、Schedule 和 Cognitive Event；持久排队、租约、重试及事件确认。实际工具执行和消息发送由宿主负责。 |
| 人格管理 | Persona 当前版本、历史、发布、回滚、状态和演进提案；独立草稿支持编辑、发布、丢弃及冲突恢复。 |
| 模型与管理台 | 可配置 Embedding/认知 HTTP Provider；提供预算、重试和并发治理。可选 Console 管理资源、人格草稿、Provider 生命周期、后台操作和统计。 |
| 数据治理与恢复 | 删除账本、保留策略、Legal Hold、索引重建、审计、导出、校验备份及恢复；防止过期后台结果或旧索引重新暴露已遗忘内容。 |

**本轮已提交的主要增量**：接通 HTTP/Worker 共用的 Graph 与 Vector Recall，补齐检索权限及人格并发约束；完善 Console 类型化操作和可信备份；实现 Embedding 配置修订、探测、索引代准备、激活/回滚、秘密轮换与恢复失效；接入受授权的认知 HTTP Provider 和重试成本记录；实现 Persona 独立草稿；实现按当前授权过滤的统计及可恢复历史回填，并加强 JSONL 边界/摘要校验与历史迁移测试。

**当前工作区新增实现（尚未提交）**：

- **插件内嵌入口**：无需监听端口即可调用 Core；支持显式启停、目录互斥、有界执行、后台维护、多人格注册和宿主异步模型回调。HTTP 与内嵌入口共享业务层；基础、server、vector、console 依赖拆分。
- **远程 SDK 与人格镜像**：Python SDK 使用原生异步连接池，支持取消、关闭、借用客户端和结果未知错误；补齐实体画像便捷读取，并提供限定 Agent 范围的人格内容镜像授权。
- **Observation 全量上下文**：背景消息和直接交互统一落库，保存可选线程/回复关系；提供固定水位分页原文、处理状态和已有摘要。入库与读取不调用模型，Recent 优先保留直接交互。
- **批量总结与保留**：显式排队或按数量/等待时间自动总结，生成带来源的 Episode 分组摘要，持久记录处理进度并支持重试/重启；自动总结默认关闭。背景原文默认保留 30 天，可配置；有效证据引用、未完成总结及 Hold 受保留保护，主动遗忘仍生效。

## 实现模块

下表是源码定位，不表示这些内部模块均为公共 Python API。

| 模块 | 职责 |
| --- | --- |
| [domain](../src/iris_memory_core/domain/) | 领域对象、范围/隐私、状态机、修订与业务不变量。 |
| [application](../src/iris_memory_core/application/) 与 [ports](../src/iris_memory_core/application/ports/) | 记忆、检索、人格、任务、遗忘等用例及存储/Provider 抽象；新增上下文读取和总结服务。 |
| [storage](../src/iris_memory_core/storage/) 与 [migrations](../migrations/) | SQLite 事务、仓储、版本历史、账本、备份恢复；当前工作区新增 Schema 25 上下文迁移。 |
| [indexing](../src/iris_memory_core/indexing/) 与 [Recall 装配](../src/iris_memory_core/recall_runtime.py) | FTS、FAISS Vector、Profile、Graph、索引代与检索路线装配。 |
| [jobs](../src/iris_memory_core/jobs/) 与 [runtime](../src/iris_memory_core/runtime.py) | Worker、Outbox、调度、重试、持久维护和服务运行配置。 |
| [providers](../src/iris_memory_core/providers/) | Embedding/认知 HTTP 适配、配置与秘密管理、模型调用治理。 |
| [business](../src/iris_memory_core/business.py) 与 [business_views](../src/iris_memory_core/business_views.py) | 本轮抽出的共享业务装配、契约校验、操作分发与响应转换，供 HTTP/Embedded 复用。 |
| [api](../src/iris_memory_core/api/)、[Console 用例](../src/iris_memory_core/application/console/) 与 [Web Console](../web/console/) | 业务 HTTP、健康/指标/SSE、独立 Console 管理 API 和前端。 |
| [embedded](../src/iris_memory_core/embedded.py) 与 [embedded_providers](../src/iris_memory_core/embedded_providers.py) | 公开本地实例、生命周期、宿主注册及同步/异步模型适配入口。 |
| [sdk](../sdk/)、[contracts](../contracts/) 与 [schemas](../schemas/) | 独立 Python/TypeScript 客户端、声明式契约、生成 OpenAPI/JSON Schema、公共接口快照及兼容校验。 |

## 对外接口

### 远程 HTTP 与 SDK

业务接口使用 `/v1`，覆盖能力协商、身份/空间绑定、Observation、近期上下文、State、Focus、Note、长期记忆、Recall/Search、任务/事件、Persona、Surface Lease、保留与受权管理操作。健康探针为 `/health/live`、`/health/ready`，指标为 `/metrics`，事件流为 `/v1/events`。

典型消费链为 `observe_batch → recall → report_recall_usage`；显式长期记忆使用 `remember_claim / correct_claim / forget_memory`。本轮新增上下文方法为 `observation_context` 和 `summarize_observations`，分别读取原文/摘要及接纳一次总结任务，排队成功不代表模型已经完成总结。

- **Python**：独立包 `iris_memory_sdk.AsyncIrisMemoryClient`，支持 `async with`、`aclose()`；公开业务错误为 `IrisMemoryApiError`，传输错误为 `IrisMemoryTransportError`。写入结果未知时需要保留原幂等键对账。
- **TypeScript**：独立 [TypeScript SDK](../sdk/typescript/README.md)，采用对应 camelCase 方法，例如 `observeBatch`、`observationContext`、`summarizeObservations`。两套 SDK 的便捷方法覆盖并非完全相同。
- **管理面**：`/console/v1` 提供独立管理会话、资源操作、Persona 草稿、Provider 配置/探测/激活/回滚、统计与回填等；普通业务 Token 不因此获得 Console 权限，Console 默认关闭。

字段、路径、错误、幂等键要求和完整操作列表以[业务 OpenAPI](../schemas/openapi/openapi.json)、[Console OpenAPI](../schemas/openapi/console.json)及[公共方法快照](../contracts/public-api.json)为准；本文按能力归纳，接口详情集中维护在这些契约中。

### 本地内嵌

公开入口为 `iris_memory_core.embedded.EmbeddedMemory`，配合 `EmbeddedConfig`、`LocalBootstrap`：

- 生命周期与维护：`start()`、`aclose()`、`run_pending()`、`diagnostics()`。
- 宿主注册：`register_actor()`、`provision_agent()`。
- 业务便捷方法：`capabilities()`、`observe_batch()`、`observation_context()`、`summarize_observations()`、`remember_claim()`、`correct_claim()`、`forget_memory()`、`recall()`。
- 其他已实现且获授权的业务操作通过 `execute(operation_id, ...)` 调用；不自动包含 HTTP 健康、SSE 或 Console。
- 模型注入通过 `iris_memory_core.embedded_providers` 的公开 Provider 接口及 `AsyncEmbeddingAdapter`、`AsyncCognitiveAdapter` 完成。

宿主负责平台消息、身份映射、模型适配和实际发送；业务调用不直接操作 Store、SQLite、索引文件或 Worker。写入和 Recall 继续受权限、版本及适用的 Lease Proof 约束。

### 可信运维 CLI

`iris-memory-core` 提供初始化、迁移、Schema 查询、HTTP 服务、Worker、备份恢复/中断切换恢复、Console 离线凭据及 Provider 主密钥轮换。它面向可信部署操作者；具体参数见[安装指南](operations/core-installation.md)和[CLI 实现](../src/iris_memory_core/cli.py)。

## 验证与完成边界

本文依据提交记录、当前源码、生成契约及既有验收报告整理，本次仅进行分支整理和文档检查，没有重新执行业务测试或发布。

既有 [Observation 验证报告](reports/observation-context.md)记录：完整 CI 曾有 61 项失败，修正后这 61 项逐一复验通过，相关迁移、契约、SDK、浏览器和隔离安装检查也有通过记录；不能据此改写成一次完整 CI 全绿。[内嵌接入报告](reports/plugin-integration.md)保存生命周期、HTTP 等价与安装消费证据。

真实外部模型质量/成本、AstrBot/Bellis 完整宿主适配、生产平台矩阵、大规模吞吐、长稳与最终恢复/发布验收仍有待完成。本轮实现不等于稳定版本已经发布。
