# Iris Memory Core 文档索引

先看 [阶段路线图](./development/README.md) 了解实际进度，再阅读 [重规划后的 Phase 14](./development/phase-14-hardening-release.md)。阶段编号、实现存在、测试通过与稳定发布是不同证据，当前状态以路线图和对应报告为准。

## 文档分工

| 入口 | 唯一职责 |
| --- | --- |
| [项目 README](../README.md) | 安装、启动、常用命令与功能入口 |
| [架构基线](./IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md) | 系统边界、领域模型、不变量和顶层验收；保留稳定章节锚点 |
| [ADR 索引](./adr/README.md) | 设计决定、接受状态、取代关系及尚未裁决的边界 |
| [阶段路线图](./development/README.md) | 阶段状态与依赖；各阶段维护交付摘要、剩余工作和退出门禁 |
| [验证报告索引](./reports/README.md) | 历史实测、当前复测、失败与限制；不把旧测试数字当当前结果 |
| [Console 设计与实现边界](./design/console-backend.md) | 合并后的后端/前端对接规范，区分已发布切片与后续设计 |
| [Console 对接矩阵](../web/console/INTEGRATION_MATRIX.md) | 各功能的契约、后端、前端和真实联调差距 |
| [贡献指南](../CONTRIBUTING.md) | 开发约束、文档维护与检查命令 |

## 协议真源

- 宿主 API：[契约源](../contracts/source/contracts.json) → [OpenAPI](../schemas/openapi/openapi.json) 与 [JSON Schema](../schemas/jsonschema/)。
- 管理 API：[Console 契约源](../contracts/source/console.json) → [Console OpenAPI](../schemas/openapi/console.json) 与 [Console JSON Schema](../schemas/jsonschema/console/)。
- 兼容与版本：[兼容基线](../schemas/compatibility/)、[Fixture](../schemas/fixtures/)、[版本 Manifest](../schemas/version-manifest.json)。
- 使用与接入：[Python SDK](../sdk/python/README.md)、[TypeScript SDK](../sdk/typescript/README.md)、[宿主接入](../hosts/README.md)、[Console 运行说明](../web/console/README.md)。

## 本轮整理与阅读规则

2026-09-06 整理覆盖根目录、架构、Phase 0–14、ADR 索引、验证报告、Console 设计、SDK 和接入说明。已完成阶段的执行清单压缩为证据摘要；架构中的重复实施路线改为链接；原自动旧库迁移方案按 ADR-0022 替换，数据安全约束保留。

Console 后端设计吸收原前端对接说明的有效约定，原独立文件删除；第 1/2/3 步和前端验证记录合并为 [Phase 13 验证报告](./reports/phase-13-verification.md)，保留日期、历史失败、联调边界及原始登录计时数据。阶段模板修齐必需结构。

Accepted ADR 保留决策历史，历史报告保留实测证据；后续解除的限制注明由哪个阶段解决，仍有效的限制在当前阶段与 Phase 14 有归属。生成契约、Schema、Fixture、锁文件和数据库 Migration 各有机器用途，不作为“重复说明文档”删除。原文档路径与锚点引用随合并更新，文档维护只遵循贡献指南的一套规则。
