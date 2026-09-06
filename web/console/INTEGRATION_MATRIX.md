# Console 前端对接矩阵

核查日期：2026-09-06。当前行为以 [Console OpenAPI](../../schemas/openapi/console.json) 和实际服务为准；目标语义见 [Console 设计与接入边界](../../docs/design/console-backend.md)，测试范围见 [合并验证记录](../../docs/reports/phase-13-verification.md)。本表只维护接入差距，不另存 API 规格或历史执行流水。

当前契约已有 88 个路径、90 个 HTTP 操作；前端 `types:check` 失败，生成类型仍落后。表中“已接线”不等于通过完整验收；第 3 步读面还有真实 Reflection/Candidate ID 与 UUIDv7 Schema 冲突。

| 功能 | 当前契约/后端 | 当前前端与证据 | 下一项真实验证 |
| --- | --- | --- | --- |
| bootstrap / 同源部署 | 已实现；按授权发布 keys/service_credentials/memory | 生成类型与动态导航；历史认证流验证 | 生产 HTTPS/可信代理；新 memory 模块导航 |
| 登录/session/refresh/reauth/logout | 已实现；第 2 步历史完整 CI | 内存 CSRF、single-flight、同键重放；有限真实 E2E | 生产 Cookie、多标签时序、会话变更时旧响应隔离 |
| 运营密钥/会话 | 已实现 | 签发/轮换/吊销/一次明文与会话页面；签发/吊销真实 E2E | KeyView 无 available/blocked_actions，Owner/委托由服务器最终判定；真实轮换交接 |
| application 宿主凭据 | 已实现，只发布 application 平面 | 使用独立生成类型与页面 | 真实签发、交叠轮换、派生任务失权 |
| 注册表/lookup/通用记忆读面 | 第 3 步已接线；含 15 类资源列表/详情/历史/引用 | 仍用设计 ResourceType；未适配当前 list_columns、结构化 sorts、create_schema/update_schema | 修复生成漂移并替换模型，使用真实数据和 Grant 验证分页/正文/计数/来源 |
| Task 子资源、Persona 读面 | GET steps/dependencies/triggers、Persona 当前/历史/proposals 已接线 | 专属页面仍为设计适配，无真实业务验收 | 子资源 Envelope、父 Revision、权限与正式类型 |
| Observation/State/Focus/Note/Task 写动作 | 未发布；描述中的写 Schema 为 null、动作 not_implemented | annotate/revision/expire/activate/transition 等模拟交互 | 真实命令字段、来源权威、Evidence、状态机与关联失效 |
| Claim/Episode/Relation/Artifact/身份写动作 | 未发布 | correct、新 Artifact、显式 Binding/redirect 等模拟交互 | 专属 Schema、受限附件、可见目标/身份历史；不得自动合并 |
| CognitiveEvent/Reflection/Candidate/投影视图 | Canonical 只读已接线但 ID 契约未通过；管理动作和投影 Console 路由未发布 | dismiss/review/dry-run/replay/来源跳转为设计适配 | 修复真实资源 ID；再定义正式动作与投影发现 |
| Forget/Persona 发布回滚/保留/Hold | 未发布 | 固定预览确认、Proposal/Policy/Evidence、保留表单均为模拟 | 预览字段、保护/Hold/CAS、清理链、人格指针和保留影响 |
| 统计八个面 | 未发布 | metric/时间桶、null/stale/warnings/coverage 等模拟 | 正式 metric 结构、可见组聚合、有界查询与真实延迟采集 |
| 导出/下载 | 未发布 | 格式/原因、Operation/行数/bytes/hash/过期 UI | 格式发现、白名单快照、Tombstone 失效、真实 JSONL 往返、Blob 内存上限 |
| 导入 | 未发布 | 字节上传→映射→报告/review→commit，resume/cancel/补偿模拟 | records/映射 Schema、报告绑定、备份 blocked、事务断点、去重/删除优先 |
| Provider | 未发布 | 草稿/探测/激活/rebuild_ack/history/rollback 模拟 | 异步探测结果路径、SSRF、配置/Worker/API 三处接线与 Generation 服务切换 |
| Settings | 未发布 | registry/validate/原子保存/历史/reset/rollback/待重启模拟 | reset/rollback validate intent、每键行为与实例修订传播 |
| Operation/运维/审计 | 未发布 | 退避轮询、问题页、任务/DLQ/调度/Worker/索引/备份/只读审计模拟 | 集合和动作 descriptor、真实操作效果；备份无 Web 恢复入口 |

`src/api/design.ts` 与 mock 的 `meta.descriptor` 仅为开发模型，不能要求后端迁就其形状。正式切片发布时同时替换类型/适配器和测试 Fixture，再补真实浏览器联调；生成类型通过本身不证明页面兼容。模拟器不得在请求失败后自动启用。
