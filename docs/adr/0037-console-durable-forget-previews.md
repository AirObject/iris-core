# ADR-0037：Console 固定删除预览与无正文回执

状态：Accepted，2026-09-07。依据 Console 后端设计 §5.4，继续 Phase 14 的管理面闭环。

## 范围与交互

接通 `POST /console/v1/memory:forget-preview` 和 `POST /console/v1/memory:forget`。本切片仅支持已有 Core Forget 账本的 Note、Observation、Claim、Episode、Relation、Artifact，每次显式选择 1–50 个目标，整批原子提交。拒绝重复目标和未知字段。注册表发布 `forget_max_targets=50`、`forget_selector=false`，界面不提供筛选集合入口。500 目标、固定筛选集合、大批次 Operation、其他资源删除和细化关联影响报告仍待后续实现。

预览逐项返回 allowed/protected/held/not_visible；不可见或不可修改目标只返回输入序号，不确认 ID 存在。保护/Hold 复用 Core 规则，soft 和 erase 均不能绕过。界面默认 soft，必须重新选择原因、生成预览并勾选不可撤销确认；切换模式或重新预览会更换幂等键。

## 持久状态与权限

Migration 0016 新增 `console_command_previews`。预览绑定租户、实际操作员 key ID/revision、Grant 指纹、模式、原因、集合摘要和十分钟有效期。保存的目标仅包含 ID、期望修订、状态及完整当前记录的哈希，不保存正文、原始凭据或不可见 ID。目标哈希包含 Scope、隐私、来源和状态；同时保存活动 Hold 指纹和删除水位。

每次预览和提交重新检查当前会话、key 与 Grant。六个静态 `.forget` 操作要求 `memory.forget`、`memory.read`、`console.manage`，并按实际对象检查完整写权限。erase 提交和已提交回执重放都要求五分钟内重新认证。只具备 memory.forget/read 的操作员无需额外 memory.write。新增领域方法接受实际 CommandActor，原业务入口 Surface/AccessContext 检查保持，不伪造 admin。

提交写事务在任何业务修改前重读全部对象、修订、保护、Hold 和水位；变化返回 `conflict / preview_stale`。同事务完成 Core Tombstone、内容擦除、证据失效、级联、删除账本、审计、缓存失效和 outbox，再原子消费预览。事务后验证实际 Tombstone 和当前凭据；普通 ResourceReader 不获得读取已删除内容的例外。

## 回执、清理与恢复

已消费预览清除目标 payload，只保留最小回执：预览 ID、目标数量、模式、Canonical 删除状态、删除序号和待清理状态。同一请求键绑定同一输入；同一已提交预览重复提交只返回原回执，不新增账本。十分钟期限只限制未提交预览；已提交回执保留七天以上的短期重试窗口，并在后续预览时分批清理。每次清理最多删除 200 条过期预览和 200 条旧回执。

Canonical 提交后即禁止普通内容与历史读取。soft 保留受控内容，erase 追加内容与文件擦除。文件操作在 SQLite 提交后执行；失败仍返回准确的 pending 回执，由同事务持久化的 `memory.invalidated` 任务重试。回执不是清理状态查询接口，不将异步工作伪报完成。

备份恢复使用原 Core 删除账本重放，验证旧备份恢复后仍禁止读取，erase 的本地文件不复活。Entity tombstone 现有入口尚未写 ForgetRequest 账本，不能据此开放 Console 实体删除；需另补账本及恢复语义。Artifact 的实际 Schema 要求 agent_id 非空，本切片保持这一不变量，不新增租户全局附件。

## 迁移与验证

Core 仍为 0.13.0 开发候选，Schema 升至 16，Python SDK 0.11.1。0016 仅新增空表和索引，online-safe；运行窗口为 16。经过 0015 的旧库仍须按 ADR-0026 停机、验证备份后升级。旧 Migration 字节保持，单独验证 15→16 升级、空库全量安装、数据库约束、重复执行与旧数据保留。回退用对应候选安装物与升级前备份，不原地降级。

公开候选仅新增两个 Console 操作、七个 Schema、forget 动作及能力描述字段；原业务 HTTP、SDK 和 CLI 未扩张。专项覆盖严格输入、当前权限与隐私、双提交、过期/变化、Hold/保护、整批回滚、六类内容、恢复重放、文件清理失败重试及真实浏览器。具体结果见 [Phase 14 验证报告](../reports/phase-14-verification.md)。这不替代生产规模、隔离、Soak 或发布门禁。
