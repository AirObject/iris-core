# Iris Memory Core 分阶段开发路线图

> 状态：In progress；2026-09-07 核查补充了当前集成缺口与后续执行指导。  
> 架构：[架构基线](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md)；决策：[ADR 索引](../adr/README.md)。

本页是阶段状态与依赖的统一入口。项目编号为 Phase 0–14；最新完成情况核查覆盖 Phase 1–10、13–14，不能把“已经推进到 Phase 13”理解为所有前序门禁均已通过。Phase 0–10 保留历史切片报告，Phase 13 仍有未完成功能；Phase 11/12 均按项目负责人 2026-09-06 要求暂缓。Phase 14 的 pip 范围仅为 Core 功能与既定公共接口，不包含两个适配器，也不以其验收或分发为前置条件。

2026-09-07：整体 Phase 14 goal 已停止；后续按[构建指导](./next-build-guide.md)和[单一工作包队列](./work-packages.md)逐包启动。最新用户授权已启动 W01→W20 串行执行，W01 专属接线门禁及完整 CI 已通过，进入 W02 前完成独立提交；每包全部验收和单独提交后立即续作，长任务后台运行并按实际完成结果续作，原定时 automation 已取消。见[队列及执行检查点](./work-packages.md#当前执行授权)。

## 阶段状态与证据

| 阶段 | 状态 | 已交付范围与证据 |
| --- | --- | --- |
| [00 架构与工程骨架](./phase-00-architecture-scaffold.md) | Completed | 工程、契约、CI；[报告](../reports/phase-00-verification.md) |
| [01 持久化、身份与空间](./phase-01-persistence-identity-scope.md) | Completed | Canonical Store、权限与 Persona Bootstrap；[报告](../reports/phase-01-verification.md) |
| [02 Observation、Outbox 与调度](./phase-02-observation-outbox-scheduler.md) | Completed | 持久可靠性与 Active Surface Lease；[报告](../reports/phase-02-verification.md) |
| [03 Recent、State 与 Focus](./phase-03-recent-state-focus.md) | Completed | 短期上下文、修订和恢复；[报告](../reports/phase-03-verification.md) |
| [04 Note、Task 与 Event](./phase-04-notes-tasks-events.md) | Completed | 捕获、计划、提醒与 ACK；[报告](../reports/phase-04-verification.md) |
| [05 长期记忆与 Episode](./phase-05-long-term-memory.md) | Completed | Remember/Correct/Forget、Artifact 与删除账本；[报告](../reports/phase-05-verification.md) |
| [06 FTS Recall](./phase-06-fts-recall.md) | Completed | 完整 Recall 契约、FTS 与 Usage；[报告](../reports/phase-06-verification.md) |
| [07 Vector Recall](./phase-07-vector-recall.md) | Completed（历史切片） | Provider/FAISS/混合路由有实现；W01 候选已接 HTTP Vector 并通过真实 SDK 命中，完整 `ci-002` 已通过；生产 Provider 配置由 W05 承接；[历史报告](../reports/phase-07-verification.md) |
| [08 Profile 与 Graph](./phase-08-profile-graph.md) | Completed（历史切片） | Profile 已接 HTTP；W01 候选已接 GraphRoute 并通过真实两跳命中，完整 `ci-002` 已通过；原量化门禁由 W02 补齐；[历史报告](../reports/phase-08-verification.md) |
| [09 Persona](./phase-09-persona.md) | Completed（历史切片） | State/Policy/Proposal、发布回滚已实现；原 Policy 性质、TTL 回拨及通用客户端证据由 W03/W16 补齐；[历史报告](../reports/phase-09-verification.md) |
| [10 巩固、Reflection 与 HTTP](./phase-10-consolidation-reflection.md) | Completed（历史切片） | 流水线/ASGI/serve/worker 已实现；W01 Recall 组合专属门禁和完整 `ci-002` 均通过；真实认知 Provider/生产进程仍待 W06/W16；[历史报告](../reports/phase-10-verification.md) |
| [11 Bellis Adapter](./phase-11-bellis-adapter.md) | Deferred | 暂时不再执行，移出当前发布门禁；已有插件、历史验证及未完成项保留；[报告](../reports/phase-11-verification.md) |
| [12 AstrBot Bridge](./phase-12-astrbot-bridge.md) | Deferred | 暂时不再执行，移出当前发布门禁；保留裁决、SDK、Bridge 与验证待办，恢复时重新确认依赖 |
| [13 Web 管理控制台](./phase-13-web-console.md) | In progress | 骨架/认证/读面、大量管理写入、Forget、Event 与 Persona 管理已交付；Draft、候选管理、统计、导入导出、Provider、Settings、Hold 和剩余运维由 W04–W14 承接；[当前核查](../reports/phase-completion-audit-2026-09-07.md) |
| [14 前置闭环、生产硬化与稳定发布](./phase-14-hardening-release.md) | In progress | W01 串行构建启动；读面/Required Lease、Core 包资源与独立 SDK 安装门禁已实施；[实施报告](../reports/phase-14-verification.md)，尚未通过稳定发布验收 |

`Completed` 保留历史阶段关闭记录，不表示原计划每个量化目标、当前服务集成或生产发布均已通过；本轮已确认 Phase 7–10 存在上述缺口。测试日期、环境、失败与未验证范围以[当前核查](../reports/phase-completion-audit-2026-09-07.md)及对应历史报告为准。

## 阶段依赖

```mermaid
flowchart LR
    P0[0 工程骨架] --> P1[1 持久化与身份]
    P1 --> P2[2 可靠性脊柱]
    P2 --> P3[3 Recent/State/Focus]
    P3 --> P4[4 Note/Task/Event]
    P4 --> P5[5 长期记忆]
    P5 --> P6[6 FTS Recall]
    P6 --> P7[7 Vector Recall]
    P7 --> P8[8 Profile/Graph]
    P8 --> P9[9 Persona]
    P9 --> P10[10 巩固与HTTP]
    P10 -.-> P11[11 Bellis 暂缓]
    P10 -.-> P12[12 AstrBot 暂缓]
    P10 --> P13[13 Console]
    P10 --> G[14.0 Core 前置闭环]
    P13 --> G
    G --> R[14.1–14.6 稳定发布]
```

Phase 11 保留 Bellis 仓库现有插件（ADR-0020），Phase 12 保留本仓库 `hosts/` 预留位置，二者均暂缓。Phase 13 继续维护 Core 独立 `/console/v1` 管理平面及配套前端，不因适配器暂缓而取消。14.1 打包/CI 与 14.2 部署准备可提前开展；当前稳定发布验收 Core 与 Phase 13，调用方只经既定公共方法访问 Core。pip 内容、独立 SDK/前端产物及验收边界见 [Phase 14 发布路径](./phase-14-hardening-release.md#pip-包发布路径)。

## 仍有效的规划决定

- Phase 1 的最小 Published Persona/Current Pointer 与 Phase 9 完整 Persona 是分次交付；Phase 2 的持久 Lease/Epoch/Fencing 仍须通过真实 Core 服务上的多客户端竞争验证，Bellis/AstrBot 专属宿主验证随 Phase 11/12 暂缓。
- Phase 6 冻结 Recall 协议，Phase 10 才交付真实 HTTP 与进程入口；历史 mock 不代表真实宿主验证。后续适配器消费契约，不为宿主改写 Canonical Domain。
- [ADR-0022](../adr/0022-management-console-plane.md) 已接受：原 Phase 13 的自动旧库迁移改为控制台手动文件导入导出。旧库扫描、增量追平、双写切换已取消；来源、幂等、断点、Tombstone 优先、隔离候选和可审计性继续由导入门禁承担。数据库 Schema Migration 始终保留。
- Phase 14 复用前期实现，重点补兼容闭环、生产部署、安全/凭据、24h Soak 和恢复证据。[Phase 14 工作包](./phase-14-hardening-release.md#工作包)保留需求与发布门槛；具体构建步骤维护在[后续构建指导](./next-build-guide.md)，执行状态维护在[工作队列](./work-packages.md)。

## 统一阶段规则

进入实施前确认依赖证据、冻结边界和契约 Fixture；变更持久化时同时定义迁移、兼容与回退。阶段状态使用 `Planned`、`In progress`、`Blocked`、`Deferred`、`Completed`；`Deferred` 表示经明确范围决定暂缓，须记录恢复条件与发布依赖影响；阻塞原因必须具体，不能用阶段编号推断完成度。

完成时检查领域行为、接口、迁移、安全、可观测性和对应测试；交付证据包含变更/版本、ADR、环境、可复现命令、结果与限制。没有证据的门禁保持未完成；遗留项须有明确承接工作包。状态更新同时修改本页与原阶段，ADR 冲突说明随决定更新。

所有阶段持续遵守：先 Scope/Privacy 后排序；写入幂等与 Expected Revision；Canonical/Outbox 原子提交；Tombstone 优先；Provider 有界与显式降级；日志/指标/错误不泄漏 Secret 或敏感正文。

## 文档模板

新阶段使用 [阶段模板](./phase-template.md)。已完成阶段保留短交付摘要、关键边界、量化门禁和证据链接；详细规范在架构/ADR，实际测试与复审结果在报告，避免复制三套正文。维护与验证命令见 [贡献指南](../../CONTRIBUTING.md)。
