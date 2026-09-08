# ADR-0053：插件内嵌业务入口与受限人格镜像

- 状态：接受，开发候选，2026-09-08。
- 范围：G-01～G-03、G-06～G-09 的 Core/SDK 接缝；不是 W01～W20 发布门禁的替代。

## 决策

新增 `iris_memory_core.embedded` 公共模块，导出 `EmbeddedMemory`、`EmbeddedConfig`、`LocalBootstrap`、`EmbeddedError`。构造无 I/O，`start()` 在宿主已有事件循环中启动，`aclose()` 关闭。业务 DTO 使用业务 OpenAPI 的 JSON 结构，通用 `execute(operation_id, ...)` 和常用方法均调用同一个私有 `BusinessRuntime`。HTTP 也调用该模块；它不依赖 ASGI Request、路由或 localhost。存储、队列、投影、AccessContext 和 Worker 不成为公共对象。

宿主以 `LocalBootstrap` 显式授权受信任的本地初始化，默认业务权限与管理权限分开。初始 Agent/Space 注册持久保存；另行开启 `manage_agents` 后可用稳定插件键预配多人格 Agent/Space，复用一个 Store/Worker/索引装配。`manage_identities` 允许初始化平台身份，但已有身份的无效绑定需要显式处理，不自动重绑。配置不能替代业务 Scope、用途、幂等、修订、删除账本或 Required Lease 检查。

同进程是信任边界，不是 Python 安全沙箱。目录锁使用 OS advisory flock，保护合作的 Embedded 实例跨进程互斥；直接打开 SQLite 或另起独立服务属于目录所有者必须避免的外部写入。独立服务的 OS 隔离承诺不延伸到宿主进程。

阻塞应用服务在实例自有的单线程执行器中运行；待处理调用数有限，用户取消不谎称中止已进入的 SQLite 写入。停止超时后保留目录占用和 closing 状态，可从诊断读取在途请求并再次关闭。宿主 Provider 永远借用，关闭只取消本实例创建的调用，绝不关闭共享模型客户端。异步回调适配现有同步 Provider ports，在宿主循环运行；取消失效的宿主回调不会无限累积新调用。

Core 基础依赖仅保留 JSON Schema 验证；`server`、`vector`、`console` 为显式可选依赖。SDK 独立分发，采用 httpx 原生异步连接池，无 Core/FAISS/NumPy/ASGI 框架依赖；旧方法参数保持兼容，新增有限并发、整次 deadline、明确关闭、借用客户端所有权和可消费的传输错误。

业务契约 1.12.0 新增 `persona.mirror.v1`。该独立授权允许 application 凭据在自身 Agent 范围发布、回滚人格内容；不授予 Policy 编辑或 Console 管理。旧普通 application 凭据即使带 `persona.manage.v1`，没有管理面身份或新镜像授权仍不能发布。离线 `init --persona-mirror` 是显式授予机制。

## 验收与边界

见 [插件接入指南与回填](../development/plugin-integration.md)。本轮公共 API、安装和 Fake Provider 验收不代替真实模型质量、真实 AstrBot、代表规模容量、生产 SQLite、恢复演练或注册表发布。Core、SDK 和 API/Schema 版本分别管理，开发快照必须以产物摘要辨识。
