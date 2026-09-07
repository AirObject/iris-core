# Architecture Decision Records

Accepted decisions are immutable historical records; replacements use a new ADR with explicit supersession and migration impact. ADR-0001 to ADR-0008 froze the boundaries Phase 1 needed; each later phase adds the decisions that phase froze.

Every ADR carries context, decision, rejected alternatives, consequences, and migration impact. Within-phase review rounds are appended to the phase's own ADR as dated revision sections rather than filed as new ADRs; a decision that changes another phase's frozen boundary gets a new ADR.

| ADR | Decision | Status |
| --- | --- | --- |
| [0001](./0001-canonical-projection-boundary.md) | Canonical/Projection boundary | Accepted |
| [0002](./0002-scope-null-semantics.md) | Scope Null semantics | Accepted |
| [0003](./0003-identity-and-binding.md) | Identity and Binding | Accepted |
| [0004](./0004-immutable-revisions.md) | Immutable Revision and Current Pointer | Accepted |
| [0005](./0005-tombstone-priority.md) | Tombstone priority | Accepted |
| [0006](./0006-api-version-and-compatibility.md) | API version and compatibility | Accepted |
| [0007](./0007-repository-boundaries.md) | Repository and dependency boundaries | Accepted |
| [0008](./0008-persona-bootstrap-seam.md) | Persona Bootstrap seam | Accepted |
| [0009](./0009-phase2-reliability-spine.md) | Phase 2 reliability spine (observation identity, integer cursors, outbox fencing, bounded catch-up) | Accepted |
| [0010](./0010-active-surface-coordinator.md) | Active Surface Coordinator (lease/epoch/modes) | Accepted |
| [0011](./0011-phase3-recent-state-focus.md) | Phase 3 recent generations, state coalesced stream, focus decay model, structured-recall skeleton | Accepted |
| [0012](./0012-phase4-notes-tasks-events.md) | Phase 4 note lifecycle, task evidence semantics, trigger/occurrence identity, at-least-once host delivery | Accepted |
| [0013](./0013-phase5-long-term-memory.md) | Phase 5 explicit long-term memory, bi-temporal claims, evidence invariants, artifact security, non-resurrecting erasure | Accepted |
| [0014](./0014-phase6-fts-recall.md) | Phase 6 FTS5 generations, full recall protocol, fresh rehydrate boundary, deterministic fusion/budgets, usage four stages | Accepted |
| [0015](./0015-phase7-vector-recall.md) | Phase 7 embedding provider port, FAISS generation lifecycle with fenced COW swap, surrogate ID map, delta ledger freshness, vector route + hybrid ranker v3 | Accepted |
| [0016](./0016-phase8-profile-graph.md) | Phase 8 profile projection (field-level sourced summaries), relation graph allowlist projection with fenced generations, budget-bound graph route + profile route, canonical fallback | Accepted |
| [0017](./0017-contract-surface-alignment.md) | Contract surface alignment: error-code supersession, endpoint naming adjudication, HTTP transport layer assigned to Phase 10 | Accepted |
| [0018](./0018-phase9-persona.md) | Phase 9 complete Persona: additive bootstrap expansion, policy/evidence gates, atomic publication, state TTL and rollback-by-new-revision | Accepted |
| [0019](./0019-phase10-consolidation-transport.md) | Phase 10 fixed-watermark consolidation/reflection, provider governance, credential/SSE transport, replay and process lifecycle | Accepted |
| [0020](./0020-bellis-adapter-plugin-seam.md) | Phase 11 Bellis Adapter: in-host plugin shape, host-monorepo delivery, staged SDK distribution, candidate/block mapping, persona source of truth in Core | Accepted（历史修订 §11/12；当前差异见 §13） |
| 0021 | *(reserved)* Phase 12 AstrBot Bridge prerequisite adjudications — effect boundary, delivery-location time-box, Python SDK distribution, identity mapping | Not yet written |
| [0022](./0022-management-console-plane.md) | Phase 13 management console plane supersedes the automated legacy migration: separate `/console/v1` contract, operator keys and browser sessions, manual import/export, versioned embedding provider config, typed runtime settings | Accepted |
| [0023](./0023-release-resources-and-console-identifiers.md) | Core installed runtime resources and opaque Console Canonical identifiers | Accepted |
| [0024](./0024-online-recall-focus-lease.md) | Required Surface proof for online Recall and Focus, including replay and commit fencing | Accepted |
| [0025](./0025-console-command-authorization.md) | Console command authorization, transaction reuse and replay reauthorization | Accepted |

## 当前未闭环边界（2026-09-06）

- **Phase 11（Deferred）**：按项目负责人 2026-09-06 要求暂时不再执行，移出当前发布门禁；ADR-0020 保持有效，已有插件与历史证据保留。Bellis effect/progress、远端 ACK、兼容、映射与 Persona 恢复待办见 [ADR-0020 §13](./0020-bellis-adapter-plugin-seam.md#13-现状核对2026-09-06) 与 [验证报告](../reports/phase-11-verification.md)，恢复后再验收。
- **Phase 12（Deferred）**：按项目负责人 2026-09-06 要求暂时不再执行，移出当前发布门禁。ADR-0021 仍未撰写，effect、交付位置、SDK 与身份映射等裁决保留到恢复时处理，见 [12.0](../development/phase-12-astrbot-bridge.md)。不能声明这些边界已经裁决。
- **Phase 13**：ADR-0022 已接受；ADR-0023 已解决不透明 ID 冲突，ADR-0025 接通管理命令与 Note 写入；见 [合并验证报告](../reports/phase-13-verification.md)。其他写入与管理模块继续实施。
- **Phase 14**：仅承接 Core/Phase 13 缺口、Required Surface 入口覆盖、Core pip 内容和公共方法白名单/隔离要求，见 [14.0](../development/phase-14-hardening-release.md#140-前置阶段闭环与候选范围冻结)；Phase 11/12 待办已移出，发布硬化尚未验收。

ADR-0022 supersedes the delivery path of the original Phase 13 ("legacy Iris data migration"), not its data-safety requirements; the retained requirements and their new home are tabulated in [ADR-0022 迁移影响](./0022-management-console-plane.md#迁移影响). It was accepted on 2026-09-05 as Phase 13 implementation started.

[ADR-0026](0026-task-dependency-lifecycle.md) 规定依赖生命周期、Core 0.13.0 / Schema 15 的离线升级与首次建库边界。

[ADR-0027](0027-focus-promotion-materialization.md) 规定 Focus 的真实目标、原始证据与管理授权。

- [ADR-0028：当前人工 Observation 与不可变原事件注释](0028-console-manual-observations.md)

- [ADR-0029：Claim 管理创建与证据化纠正](0029-console-claim-commands.md)

- [ADR-0030：Episode 管理修订与当前指针同步](0030-console-episode-revisions.md)

- [ADR-0031：有向 Relation 更正、证据修订与状态管理](0031-console-relation-corrections.md)
- [ADR-0032：Console 不可变原始文本 Artifact](0032-console-immutable-text-artifacts.md)
- [ADR-0033：Console 有界原始附件上传](0033-console-bounded-artifact-upload.md)

- [ADR-0034：Console 身份注册与显式绑定生命周期](0034-console-identity-registry.md)

- [ADR-0035：Console 受控实体重定向与关联链授权](0035-console-bounded-entity-redirect.md)

- [ADR-0036：Console 实体属性快照与权威合并](0036-console-entity-attribute-snapshots.md)

- [ADR-0037：Console 固定删除预览与无正文回执](0037-console-durable-forget-previews.md)

- [ADR-0038：Entity tombstone 的完整删除账本与恢复](0038-console-entity-tombstone-ledger.md)

- [ADR-0039：Focus 删除、工作集容量与恢复](0039-console-focus-forget.md)

- [ADR-0040：State 删除与同键新代记录](0040-console-state-forget-generations.md)

- [ADR-0041：Task 删除、子对象与投递事实](0041-console-task-forget-cascade.md)

- [ADR-0042：固定筛选集合与可停止的批量删除 Operation](0042-console-forget-operations.md)

- [ADR-0043：Console CognitiveEvent dismiss](0043-console-event-dismiss.md)

- [ADR-0044：Console Persona 发布与回滚](0044-console-persona-publication.md)
- [ADR-0045：Console PersonaState 管理](0045-console-persona-state.md)
- [ADR-0046：Console Persona 提案管理](0046-console-persona-proposals.md)
- [ADR-0047：Console Persona Policy 管理](0047-console-persona-policy.md)
