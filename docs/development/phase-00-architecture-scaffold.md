# 阶段 0：架构冻结与工程骨架

> 状态：Completed（历史交付快照；不代表当前发布验收）  
> 前置阶段：无  
> 阶段交付版本：0.1.0  
> 开始日期：2026-08-29  
> 完成日期：2026-08-29  
> 架构依据：[§2 架构原则](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#2-架构原则与不变量)、[§4 公共约定](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#4-公共约定)、[§28 SDK](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#28-sdk-与契约发布)、[§33 仓库组织](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#33-仓库与代码组织)、[§36 阶段 0](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#阶段-0架构冻结与工程骨架)

本页归档交付范围；测试实测、复审过程与限制集中在[验证报告](../reports/phase-00-verification.md)。后续状态与未关闭工作以[阶段索引](README.md)及[Phase 14](phase-14-hardening-release.md)为准。

## 阶段目标

交付 Monorepo、领域依赖边界、公共类型、契约生成与兼容检查、双语言 Fixture 和迁移/CI 骨架。业务实现由后续阶段完成。

## 架构约束

- `domain` 只能依赖标准库和领域安全原语；依赖方向固定为 `api → application → domain`。
- 公共协议使用通用领域术语，不暴露 Bellis、AstrBot 或旧 L1/L2/L3 类型。
- API、JSON Schema、SDK Fixture 和稳定错误码属于同一契约发布面。
- 冻结边界发生变化必须先走 ADR，不允许在实现代码中形成隐式新架构。

## 需求追踪

| 需求 ID | 历史需求范围 |
| --- | --- |
| P0-ARCH-01 | Canonical/Projection、Scope、Identity、Revision、Tombstone 等冻结边界 |
| P0-BOUNDARY-01 | `api → application → domain`，Domain 不依赖框架 |
| P0-CONTRACT-01 | OpenAPI 3.1、JSON Schema、稳定错误和版本协商 |
| P0-MIGRATION-01 | 顺序 Migration、Checksum 与 Schema Version |
| P0-CI-01 | 干净环境可重复执行质量门禁 |

需求对应的实现、测试与复审证据统一见[验证报告](../reports/phase-00-verification.md)。

## 工作包

0.1 决策记录；0.2 Python 工程与模块边界；0.3 契约与 SDK 骨架；0.4 Migration 与 CI。

实现结果和复审记录统一见验证报告。

## 数据、契约与回退策略

本阶段 Migration、契约版本、兼容窗口和恢复证据见[验证报告](../reports/phase-00-verification.md)；决策依据：[ADR-0001](../adr/0001-canonical-projection-boundary.md)、[ADR-0006](../adr/0006-api-version-and-compatibility.md)、[ADR-0007](../adr/0007-repository-boundaries.md)。阶段版本是历史快照，不能作为当前部署支持范围；当前版本读取 [version-manifest](../../schemas/version-manifest.json)，升级/回退按[Phase 14](phase-14-hardening-release.md)验证。

## 量化验收基线

干净依赖环境 bootstrap/CI、导入边界正反例、双语言 Fixture 与迁移校验的历史实测与规模条件见[验证报告](../reports/phase-00-verification.md)；原计划的逐项门槛保存在[原阶段验收目标](../reports/phase-00-verification.md#原阶段验收目标)，未测目标不会因归档消失。

## 退出门禁

本阶段按[验证报告](../reports/phase-00-verification.md)记录关闭；报告中的测试规模、版本和基准只适用于当时快照。限制项不因 Completed 状态自动消失，正式发布须重新通过 Phase 14 门禁。

## 交付证据

- [阶段验证报告](../reports/phase-00-verification.md)：实现/测试追踪、复审修复、历史门禁和限制。
- 决策：[ADR-0001](../adr/0001-canonical-projection-boundary.md)、[ADR-0006](../adr/0006-api-version-and-compatibility.md)、[ADR-0007](../adr/0007-repository-boundaries.md)。
- [当前契约源](../../contracts/source/contracts.json)与[迁移文件](../../migrations/)；不在阶段摘要中复制版本、checksum 或端点清单。

## 已知限制

以[验证报告中的限制](../reports/phase-00-verification.md#known-limitations)为唯一记录；报告同时标出已由后续阶段关闭的历史缺口。

## 明确不做

- 不实现具体领域表、HTTP 业务端点、FAISS、宿主 Adapter 或生产容器。
- 不选择未经需求驱动的具体 LLM/Embedding Provider。
- 不以占位 DTO 绕过 Scope、Revision 或错误协议的 ADR 决策。

## 交接条件

本阶段能力已进入后续集成基线。新增工作遵循[阶段索引](README.md)与[Phase 14](phase-14-hardening-release.md)的依赖和验收，不重复执行本页历史工作包。
