# astrbot_plugin_iris_memory_api

> 状态：未开工（2026-09-06 复核）。本目录只有 README，无插件实现、依赖声明、配置元数据或测试。

这是 [Phase 12：AstrBot Bridge](../../docs/development/phase-12-astrbot-bridge.md)
预留的临时交付目录。目标是经 Python SDK 接入身份映射、Recall/Persona、记忆工具和实际发送效果；这些能力尚未交付。

实施入口、实际效果边界、SDK 能力/分发、身份映射、打包门禁和拆分时间盒统一维护在
[Phase 12 的前置裁决](../../docs/development/phase-12-astrbot-bridge.md#工作包)。
ADR-0021 尚不存在，前置裁决未完成前不进入插件实施。

本目录遵循 [宿主接入隔离不变量](../README.md#隔离不变量)。位置、SDK 分发和 Bridge 实现各自验收；Core 或 Python SDK 测试通过不代表 AstrBot Bridge 完成。
