# 宿主接入目录

本目录临时存放独立于 Core 发布物的宿主接入；与 Core 内部应用层
`src/iris_memory_core/application/` 无代码或依赖共享。

Phase 11 Bellis Adapter 与 Phase 12 AstrBot Bridge 均已按项目负责人 2026-09-06 要求暂缓，移出当前 Core pip 产物及发布门禁；Bellis 的已有插件保留在 Bellis 仓库。本目录继续保留说明和隔离约束，不随本轮 Core 发布恢复适配器实施。

| 目录 | 当前状态 | 计划与剩余裁决 |
| --- | --- | --- |
| [astrbot_plugin_iris_memory_api](./astrbot_plugin_iris_memory_api/) | Deferred：暂时不再执行；只有说明文件，移出当前发布门禁 | [Phase 12](../docs/development/phase-12-astrbot-bridge.md) |

## 隔离不变量

这是“Core 不引入宿主类型或运行时依赖”在同仓布局下的要求，后续宿主目录必须持续满足：

1. 自带依赖声明，不写入 Core 根项目依赖或 dependency-groups，不加入 uv workspace。
2. 不进入 Core wheel 的 packages；Core 发布物不含宿主代码。
3. 不进入 Core mypy files 或 pytest testpaths；宿主门禁由本目录声明的工具独立执行。
4. Core 不 import 宿主目录；只能由宿主依赖公共 SDK。
5. 根 ruff 仍可扫描宿主目录，共享纯静态风格检查。

2026-09-06 源码/配置检查未发现以上隔离被破坏；当前只有 README，尚不能据此声称插件安装、wheel 隔离或独立 CI 已验收。交付位置的拆分触发条件、负责人和时间盒由 Phase 12 的 ADR-0021 定稿，不能与 SDK 分发退出合并验收。
