# Observation 上下文与批量总结验证记录

日期：2026-09-08。状态：实现完成，本地检查已分段复验。本报告保留完整 CI 的失败结果，不代表一次完整 CI 全绿、已发布或真实模型质量验收。

## 已交付范围

- `dev/chores` 快进并入 `main`（7629152），原有 44 个未提交/未跟踪文件在转移分支时已逐文件核对保存。
- 先更新 ADR、设计与相关手写指南，再实现统一 Observation 背景/交互记录、原始上下文分页、Episode 分组摘要、持久处理账本、Worker/Embedded 维护、保留和公共接口。
- 用户批准自动总结显式开启；背景原文默认 30 天可配置；仅摘要明确引用的消息及其他有效记忆证据获得依赖保留，同批被忽略的消息仍到期清理。
- Core 0.16.0、Schema 25、业务 Contract 1.13.0、Python/TypeScript SDK 0.12.0。Console 沿用既有资源读取，Observation 明细补充用途、线程和回复字段。

设计和调用方式见[实施说明](../development/observation-context.md)，决定见 [ADR-0054](../adr/0054-observation-context-and-batch-summaries.md)。

候选是 `main` 的 `7629152` 加本轮未提交工作区。验证环境：macOS 26.6.2 / ARM64、Python 3.12.13、SQLite 3.50.4、Node 26.8.1，真实浏览器使用本机 Google Chrome。执行 `UV_OFFLINE=1 make ci`；失败后按 CONTRIBUTING 复验受影响集合，使用 `CONSOLE_BROWSER_EXECUTABLE` 指定现有浏览器补齐 `make console-browser package-check`。未运行上传/注册表发布。

## 本轮验证

新增行为测试覆盖无模型接入、固定水位分页、同时间消息、迟到消息、游标绑定、隐藏来源、分组与空摘要、未知引用、删除竞态、凭证撤销、数量/等待/默认关闭、重启、失败批次、长前缀扫描、背景洪峰下交互保留、普通到期与主动遗忘、Schema 24 有数据升级、公共 SDK/Embedded 与 HTTP 适配器。

- 新增核心上下文行为测试：15 项通过。
- 恢复问题修复后，相关 Console/备份/遗忘回归：两组共 215 项通过。
- 新接口全操作矩阵：2 项通过，响应经冻结 JSON Schema 验证。
- 所有迁移与 Persona 历史前缀升级：180 项通过，包含 Schema 1–24 到 25 的顺序及原迁移校验和保护。
- 本轮修正升级夹具后，Task 管理/遗忘 37 项、Entity 删除账本 6 项、Schema 6 历史恢复 8 项通过。
- TypeScript SDK：21 项通过，包含新增接口的路径、请求体、幂等键检查。
- Console 单元测试 29 项、真实浏览器 25 项和统计浏览器 1 项通过；生产构建与资源检查通过。
- 全新 sdist/wheel、SDK 独立依赖边界、隔离安装、已安装 Embedded、HTTP SDK/Required Lease、索引与 Worker smoke 通过，安装物报告 Core 0.16.0 / Schema 25 / SDK 0.12.0。
- 格式、导入边界、类型、文档、生成契约、兼容检查和公共 API 候选审查已通过。
- 完整 `make ci`：性能 11 项通过；后端 **15,289 passed / 61 failed**，耗时 **1,807.61 秒**，覆盖率 **85.37%**，达到既有 80% 门槛。命令退出码 2，未改记为 0。
- 对该完整运行的 **61 个失败节点逐一映射并复验，61 passed（15.40 秒）**。复验保留覆盖率采集，仅不把局部测试覆盖率当成全仓门槛；全仓门槛仍引用上面的完整运行。修改后的新增 Schema 24 前缀用例也在 180 项迁移矩阵中通过。
- 61 项中，两项是 Console 读预算到期（Focus 50 条原子删除预览和 Operation 恢复夹具的预览）。独立及带覆盖率的复验均通过，未延长生产预算，也没有删除其断言。这说明本轮可复验通过，不能据此宣称已消除高负载下的计时波动。其余是已修正的版本/注册表期待及历史夹具形状。

早期完整检查发现新接口覆盖遗漏及恢复游标清理连接未关闭问题，已修复并通过相应回归；另补齐不可读消息前缀下的持久扫描进度。最终组合检查也揭示历史升级夹具遗漏新版本清单、附加列及视图，已修正并通过迁移回归。中途停止的检查未被计为完整通过。

浏览器默认 Chromium 缓存缺失时改用本机 Google Chrome。第一次实际浏览器组合中，Persona reader 被共享测试后端的真实登录限流阻挡，其他 24 项通过；测试已改为按服务端 `Retry-After` 等待重试，未修改生产限流。

已归档：[完整 CI（测试临时 CSRF 已脱敏）](evidence/observation-context/full-ci.txt)、[61 项带覆盖率复验](evidence/observation-context/failures-rerun.txt)、[核心行为](evidence/observation-context/core-context.txt)、[迁移矩阵](evidence/observation-context/migrations.txt)、[历史恢复](evidence/observation-context/legacy-recovery.txt)、[Entity 账本](evidence/observation-context/entity-ledger.txt)、[静态与契约](evidence/observation-context/static-contracts.txt)、[最终文档/格式检查](evidence/observation-context/final-lint.txt)、[SDK](evidence/observation-context/sdk.txt)、[浏览器与安装包](evidence/observation-context/browser-packages.txt)。

## 验证边界

真实模型的摘要准确度、噪声筛选效果和收费水平仍由宿主选定模型决定。本轮使用确定性测试模型、宿主异步回调和本机 HTTP 模型替身验证集成，不调用收费外部模型。

Source ID、版本、权限和引用覆盖验证能阻止未知或无权来源提交，不能证明模型摘要的所有语义推断正确。旧单摘要回调会引用整批；需要筛除闲聊时应使用分组输出格式。

保留清理由维护循环推进，不保证在第 30 天的精确时刻删除；有效引用、未完成总结及 Hold 会阻止普通清理。最小审计元数据/墓碑保留，数据库文件不会立即缩小。跨批次话题自动合并、真实超大群聊吞吐、生产模型质量和新平台实机适配未在本轮宣称完成。
