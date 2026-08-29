# 阶段 0：架构冻结与工程骨架

> 状态：Completed  
> 前置阶段：无  
> 负责人：Iris Memory Core Team  
> 目标版本：0.1.0  
> 开始日期：2026-08-29  
> 完成日期：2026-08-29  
> 架构依据：[§2 架构原则](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#2-架构原则与不变量)、[§4 公共约定](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#4-公共约定)、[§28 SDK](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#28-sdk-与契约发布)、[§33 仓库组织](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#33-仓库与代码组织)、[§36 阶段 0](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#阶段-0架构冻结与工程骨架)

## 阶段目标

建立可以安全承载后续领域实现的 Monorepo、契约生成链和 CI 门禁。阶段结束时，空业务骨架应能运行、测试、生成并校验 Python/TypeScript 契约，且领域层不依赖 Web、数据库、索引或宿主框架。

## 架构约束

- `domain` 只能依赖标准库和领域安全原语；依赖方向固定为 `api → application → domain`。
- 公共协议使用通用领域术语，不暴露 Bellis、AstrBot 或旧 L1/L2/L3 类型。
- API、JSON Schema、SDK Fixture 和稳定错误码属于同一契约发布面。
- 冻结边界发生变化必须先走 ADR，不允许在实现代码中形成隐式新架构。

## 需求追踪

| 需求 ID | 基线要求 | 工作包 | 验证门禁 |
| --- | --- | --- | --- |
| P0-ARCH-01 | Canonical/Projection、Scope、Identity、Revision、Tombstone 等冻结边界 | 0.1 | ADR 状态与开放冲突检查 |
| P0-BOUNDARY-01 | `api → application → domain`，Domain 不依赖框架 | 0.2 | Import Boundary 正反例测试 |
| P0-CONTRACT-01 | OpenAPI 3.1、JSON Schema、稳定错误和版本协商 | 0.3 | 生成无漂移、兼容检查、Fixture 双语言验证 |
| P0-MIGRATION-01 | 顺序 Migration、Checksum 与 Schema Version | 0.4 | 空库升级与篡改拒绝测试 |
| P0-CI-01 | 干净环境可重复执行质量门禁 | 0.2–0.4 | `make ci` 全流程报告 |

## 工作包

### 0.1 决策记录

- 建立 `docs/adr/`，记录 Canonical/Projection、Scope Null、Identity、Revision、Tombstone、API Version 和 Repository Boundary。
- 补充 Persona Bootstrap seam：Agent 何时获得最小 Published Persona，以及 Phase 9 如何无破坏扩展完整 Persona。
- 每份 ADR 包含上下文、决策、被否决方案、后果、迁移影响和状态。

### 0.2 Python 工程与模块边界

- 建立 `pyproject.toml`、Python 3.12+ 包、CLI 入口和 `src/iris_memory_core` 分层目录。
- 配置格式化、Lint、静态类型、单元测试和覆盖率基线。
- 通过 import-boundary 测试禁止 Domain 导入 FastAPI、SQLite、FAISS、Provider SDK 或宿主类型。

### 0.3 契约与 SDK 骨架

- 建立 OpenAPI 3.1、领域 JSON Schema、错误 Envelope、Capability 和版本 Manifest 的生成流程。
- 创建 Python Async SDK、TypeScript SDK 的最小生成/验证骨架及 Mock Server。
- 固定正向、错误、未知可选字段和未知枚举 Fixture；双语言验证同一 Fixture。

### 0.4 Migration 与 CI

- 建立顺序 Migration Runner、Checksum、Schema Version 表和空库升级测试。
- CI 至少执行 Format Check、Lint、Type Check、Unit/Coverage、Schema 生成无漂移、兼容检查和双 SDK Fixture 验证。
- 添加 AGPL-3.0、开发环境、测试命令、架构导航和贡献说明。

## 数据、契约与回退策略

- Phase 0 只创建基础设施元数据，不创建业务领域表；Migration 采用只升不降、文件不可变和 SHA-256 Checksum 策略。
- 已应用 Migration 不允许原地编辑。修正通过新 Migration 完成；篡改必须在启动和 CI 中被拒绝。
- OpenAPI/JSON Schema 以生成源为事实源，生成物必须无漂移；兼容检查禁止静默删除既有端点、方法、Schema 或必填字段。
- Phase 0 尚无生产数据。安装失败时删除专用测试数据库后从 0 重建；进入 Phase 1 后，回退必须使用兼容二进制或经校验备份，不执行破坏性 Down Migration。

## 量化验收基线

- Python 3.12+ 与 Node.js 22+ 的干净环境均能复现；CI 固定 Python 3.12。
- 测试覆盖率不低于 80%，Format、Lint、Type Check 和全部测试零错误。
- 契约连续生成两次字节一致；兼容基线、Python Fixture 和 TypeScript Fixture 结果一致。
- Migration 至少覆盖空库升级、重复运行、未知文件名和已应用文件篡改四类场景。

## 退出门禁

- [x] 七项基础 ADR 与 Persona Bootstrap seam 已接受，没有影响 Phase 1 的开放架构冲突。
- [x] Domain import-boundary 测试能够主动捕获一次非法框架依赖。
- [x] OpenAPI/JSON Schema 可重复生成，工作区无未提交生成漂移。
- [x] Python 与 TypeScript 同时接受合法 Fixture、拒绝非法 Fixture。
- [x] 空库 Migration 可从 0 升级到当前版本，并拒绝校验和被篡改的 Migration。
- [x] CI 在干净环境一次完成，README 能让新开发者复现。
- [x] 需求追踪、交付证据、已知限制和下一阶段迁移约束均已更新。

## 交付证据

- 代码/变更：本地提交 `a3a3e1d5dba70dbb3b2a385fd443a2ca8cf6c557`；[工程入口](../../README.md)、[CI](../../.github/workflows/ci.yml)、[质量门禁](../../Makefile)
- ADR：[ADR 索引及八项 Accepted 决策](../adr/README.md)
- Schema/Migration：[Version Manifest](../../schemas/version-manifest.json)、[OpenAPI 3.1](../../schemas/openapi/openapi.json)、[`0001_phase0_metadata.sql`](../../migrations/0001_phase0_metadata.sql)
- 测试报告：[Phase 0 Verification Report](../reports/phase-00-verification.md)
- 已知限制：仅交付 Phase 0 基础设施；业务领域表和端点、生产部署及远端 GitHub Actions 运行均属于后续交付或首次推送验证，不影响本地与干净副本门禁结论。

## 明确不做

- 不实现具体领域表、HTTP 业务端点、FAISS、宿主 Adapter 或生产容器。
- 不选择未经需求驱动的具体 LLM/Embedding Provider。
- 不以占位 DTO 绕过 Scope、Revision 或错误协议的 ADR 决策。

## 交接条件

Phase 1 可以稳定依赖模块边界、Migration Runner、契约生成入口、稳定错误 Envelope 和 CI 门禁；后续代码无需重组仓库才能接入持久化领域。
