# 公开客户端方法与接口变更门禁

当前清单适用于 Core 0.15.0、Schema 22、业务契约 1.11.0、Console 契约 1.2.0、Python SDK 0.11.1、TS SDK 0.11.2。稳定级别为开发候选；尚未构成 Phase 14 的逐方法生产验收。Core 和 SDK 分别分发。

## 导入和错误

Core 顶层只有 `iris_memory_core.__version__`；业务调用入口为独立包的 `iris_memory_sdk.AsyncIrisMemoryClient`。其构造参数是 URL、可选超时和 Bearer Token，没有 Store、连接、Provider、Worker 或通用对象查找参数。

Python 根导出 `AsyncIrisMemoryClient`、`CapabilitiesEnvelope`、`ErrorEnvelope`、`ContractValidationError`、`validate_contract`，并提供版本属性 `__version__`。服务错误使用 `iris_memory_sdk.client.IrisMemoryApiError`，其 `envelope` 是由公开错误响应构造的 DTO；网络/超时错误仍由标准库抛出。其余结果为 JSON 字典或具名 Capabilities DTO，不能将这些字典视为客户端授予的授权上下文。

类型与签名、DTO 字段和继承、两套 HTTP operation/Schema 摘要、CLI 子命令和参数全部记录在 [机器清单](../../contracts/public-api.json)。TS 公开声明也在该快照内；TS 方法使用对应 camelCase 名称，缺少 Python 的 `current_surface_lease`，另有 `events`。`getEntityProfile`、Liveness、Metrics 没有 SDK 便捷方法；可以按公开 HTTP 契约消费，不能通过私有 `_request_json` 代替受支持方法。

## 共同语义

认证从服务端凭据构造权限，请求只能缩小 Tenant/Agent/Space/Subject 范围；领域读写还检查资源 Scope、Privacy、用途及业务权限。管理路径和管理业务需要对应管理授权，普通 application Token 不因此获得访问权。逐方法权限矩阵和消费证据仍在 Phase 14 继续补齐。

具体请求、响应、错误、Idempotency-Key 必选性及 expected_revision 字段由下表 operationId 对应的 [业务 OpenAPI](../../schemas/openapi/openapi.json) 定义。每个 operation 的 Schema 引用和内容摘要受快照约束。Required Surface 下在线写入和 Recall 必须携带有效 Lease Proof；重试保留业务幂等键，可以更新 Proof，旧 Proof 不获得成功重放。

`rebuild_recent_context` 已弃用，替代为 `rebuild_index("recent_context", ...)`。现有 /v1 窗口继续保留旧入口，不承诺在 1.0 时删除；移除按 [ADR-0006](../adr/0006-api-version-and-compatibility.md) 另开版本和迁移窗口。其余方法不设隐式删除期限。

业务协商 `/v1/negotiation` 在 Contract 1.11.0 增加可选 `required_capabilities`；未配置、未授权或未知的必需能力返回既有 `unsupported_version`，旧客户端无此字段时仍兼容。配置后暂不可用的 Vector 保留支持声明，通过 Recall 降级与运行就绪探针表达当前故障。SDK `negotiate()` 的既有签名保持兼容；需要显式必需能力集合的调用方使用公开 HTTP 请求并核查响应。

## Python 方法映射

下列方法都属于 `iris_memory_sdk.AsyncIrisMemoryClient`。精确签名（含位置/关键字参数、默认值和返回类型）见机器清单；映射按显式策略维护，新增类方法不会自动进入白名单。

| 方法 | operationId |
| --- | --- |
| `ack_cognitive_event` | `ackCognitiveEvent` |
| `acquire_surface_lease` | `acquireSurfaceLease` |
| `capabilities` | `getCapabilities` |
| `claim_history` | `claimHistory` |
| `correct_claim` | `correctClaim` |
| `create_artifact` | `createArtifact` |
| `create_backup` | `createBackup` |
| `create_episode` | `createEpisode` |
| `create_export` | `createExport` |
| `create_focus_item` | `createFocusItem` |
| `create_identity` | `createIdentity` |
| `create_legal_hold` | `createLegalHold` |
| `create_note` | `createNote` |
| `create_persona_proposal` | `createPersonaEvolutionProposal` |
| `create_relation` | `createRelation` |
| `create_schedule` | `createSchedule` |
| `create_space_group` | `createSpaceGroup` |
| `create_task` | `createTask` |
| `create_task_dependency` | `createTaskDependency` |
| `create_task_step` | `createTaskStep` |
| `create_task_trigger` | `createTaskTrigger` |
| `current_persona` | `getCurrentPersona` |
| `current_surface_lease` | `getCurrentSurfaceLease` |
| `dry_run_reflection` | `dryRunReflection` |
| `export_deletion_ledger` | `exportDeletionLedger` |
| `focus_transition` | `activateFocusItem`, `setFocusDormant`, `dismissFocusItem`, `expireFocusItem`, `promoteFocusItem` |
| `forget_memory` | `forgetMemory` |
| `get_artifact` | `getArtifact` |
| `get_claim` | `getClaim` |
| `get_entity` | `getEntity` |
| `get_entity_relations` | `getEntityRelations` |
| `get_episode` | `getEpisode` |
| `get_focus_item` | `getFocusItem` |
| `get_relation` | `getRelation` |
| `get_state` | `getState` |
| `heartbeat_surface_lease` | `heartbeatSurfaceLease` |
| `list_admin_jobs` | `listAdminJobs` |
| `list_audit_events` | `listAuditEvents` |
| `list_cognitive_events` | `listCognitiveEvents` |
| `list_focus_items` | `listFocusItems` |
| `list_notes` | `listNotes` |
| `list_retention_policies` | `listRetentionPolicies` |
| `list_space_groups` | `listSpaceGroups` |
| `list_states` | `listStates` |
| `list_tasks` | `listTasks` |
| `negotiate` | `negotiateCapabilities` |
| `note_action` | `archiveNote`, `promoteNote` |
| `observe_batch` | `observeBatch` |
| `persona_history` | `getPersonaHistory` |
| `prepare_binding` | `prepareBinding` |
| `publish_persona_revision` | `publishPersonaRevision` |
| `put_state` | `putState` |
| `readiness` | `getReadiness` |
| `rebuild_index` | `rebuildIndex` |
| `rebuild_recent_context` | `rebuildRecentContext` |
| `recall` | `recall` |
| `recent_context` | `getRecentContext` |
| `release_legal_hold` | `releaseLegalHold` |
| `release_surface_lease` | `releaseSurfaceLease` |
| `remember_claim` | `createClaimRemember` |
| `replay_reflection` | `replayReflection` |
| `report_recall_usage` | `reportRecallUsage` |
| `retry_admin_job` | `replayDeadLetter` |
| `review_binding` | `confirmBinding`, `revokeBinding` |
| `review_persona_proposal` | `approvePersonaEvolutionProposal`, `rejectPersonaEvolutionProposal` |
| `rollback_persona` | `rollbackPersona` |
| `run_schedule_now` | `runScheduleNow` |
| `search` | `search` |
| `search_claims` | `searchClaims` |
| `set_retention_policy` | `setRetentionPolicy` |
| `set_space_group_binding` | `bindSpaceGroup`, `unbindSpaceGroup` |
| `source_cursor` | `getSourceCursor` |
| `state_history` | `getStateHistory` |
| `transition_episode` | `transitionEpisode` |
| `transition_task` | `transitionTask` |
| `transition_task_step` | `transitionTaskStep` |
| `update_note` | `updateNote` |
| `update_persona_state` | `updatePersonaState` |
| `update_task` | `updateTask` |

## 校验和变更流程

运行 `make public-api-check` 检查源码，`make package-check` 在干净 venv 内用 `python -I` 检查实际 wheel。未知导出、方法、DTO、签名、HTTP operation/Schema、CLI 参数或 TypeScript 声明变化都会失败；SDK 静态导入 Core 实现也会失败。

需要变更时，先更新显式映射 `tools/public_api_policy.py` 和对应契约/兼容测试，再用 `python -m tools.check_public_api --candidate /tmp/public-api-candidate.json` 生成独立候选文件。此命令拒绝覆盖机器清单；比较候选差异并审阅后才更新清单。不能只为消除 CI 失败而接受未实现方法。

快照检查不是 Python 沙箱，也不替代权限测试、响应泄漏测试或 OS 文件隔离。可信 CLI 允许操作者指定数据库/备份路径，不能把它作为普通 SDK 用户的业务接口；参见 [可信初始化](../operations/core-installation.md)。

W05 已逐项审查独立候选：仅增加 14 个已实现的 Console Provider 操作、有限 Operation 类型/问题码、离线秘密轮换命令和 serve/worker 的五个可选部署参数。既有 CLI 参数/默认值、两套 SDK 方法及映射、业务 HTTP 均逐值不变，因此 SDK 显式映射无需修改。证据与候选摘要见[审查记录](../reports/evidence/w05/public-api-review.json)；公开接口清单更新不代表 W05 的真实外部 Provider 门禁已完成。
