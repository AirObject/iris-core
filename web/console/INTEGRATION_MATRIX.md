# Console 前端对接矩阵

核查日期：2026-09-07。当前行为以 [Console OpenAPI](../../schemas/openapi/console.json) 和实际服务为准；目标语义见 [Console 设计与接入边界](../../docs/design/console-backend.md)，测试范围见 [合并验证记录](../../docs/reports/phase-13-verification.md)。本表只维护接入差距，不另存 API 规格或历史执行流水。

当前契约已有 <!-- contract-count:console:paths -->154<!-- /contract-count --> 个路径、<!-- contract-count:console:operations -->184<!-- /contract-count --> 个 HTTP 操作；前端类型已同步；Reflection/Candidate 不透明 ID 已通过契约回归。读面真实浏览器证据见 [Phase 14 报告](../../docs/reports/phase-14-verification.md)，表中其余“已接线”不等于完整验收。

| 功能 | 当前契约/后端 | 当前前端与证据 | 下一项真实验证 |
| --- | --- | --- | --- |
| bootstrap / 同源部署 | 已实现；按授权发布 keys/service_credentials/memory | 生成类型与动态导航；历史认证流验证 | 生产 HTTPS/可信代理；新 memory 模块导航 |
| 登录/session/refresh/reauth/logout | 已实现；第 2 步历史完整 CI | 内存 CSRF、single-flight、同键重放；有限真实 E2E | 生产 Cookie、多标签时序、会话变更时旧响应隔离 |
| 运营密钥/会话 | 已实现 | 签发/轮换/吊销/一次明文与会话页面；签发/吊销真实 E2E | KeyView 无 available/blocked_actions，Owner/委托由服务器最终判定；真实轮换交接 |
| application 宿主凭据 | 已实现，只发布 application 平面 | 使用独立生成类型与页面 | 真实签发、交叠轮换、派生任务失权 |
| 注册表/lookup/通用记忆读面 | 第 3 步已接线；含 15 类资源列表/详情/历史/引用 | 已适配正式列/排序和空写 Schema；真实 Note 列表/详情/历史/刷新通过 | 扩展更多资源的真实浏览器分页/计数/来源覆盖 |
| Task 步骤 | GET/创建/状态流转、父子 CAS、连带就绪修改授权已接线 | 正式 Meta 描述与真实 Required 浏览器创建/开始/完成/重载 | 删除恢复全链 |
| Task 依赖 | GET/创建/解除及复用 ID 恢复已接线；父子 CAS、两端授权、删除优先 | 正式步骤 Lookup；真实创建/解除/恢复/刷新浏览器通过 | 生产规模和完整 Forget/恢复链 |
| Task 触发器 | 列表/授权详情、创建/编辑/启停已接线；父子 CAS 和完整来源授权 | 正式描述与真实创建/编辑/禁用/启用/重载浏览器通过 | 生产调度规模和完整 Forget/恢复链 |
| Persona 读面 | 当前/历史/proposals 已接线 | 其余专属页面仍为设计适配 | 正式动作和真实业务验收 |
| Note 创建/编辑/状态转换 | 已接通严格契约、管理授权与共享领域事务 | 正式动作描述、授权 lookup、CAS、真实浏览器创建/编辑/归档 | 生产规模与完整权限矩阵 |
| Focus 创建/编辑/激活/状态转换 | 已接通严格契约、容量受影响目标授权与共享领域事务 | 真实 Required 模式浏览器创建/编辑/休眠/激活/提升为 Task/重载 | 四类真实提升及来源/目标重授权已接线；完整 Forget/生产规模验收仍待完成 |
| State 创建/更正/过期 | 已接通严格请求、固定 user 来源、namespace 策略、自然键 CAS 和无正文回执 | 正式字段描述、授权 lookup、Revision 和过期动作 | 生产规模与删除/恢复全链 |
| Task 创建/编辑/状态流转 | 已接通严格契约、CAS、所有者授权及有效完成证据 | 真实 Required 浏览器创建/编辑/等待/取消/重载 | 证据关联失效与恢复全链 |
| Observation 写动作 | 手工记录和注释已接通严格来源与授权 | 真实创建/注释/历史浏览器通过 | 生产规模与完整权限矩阵 |
| Claim/Episode/Relation/Artifact 写动作 | 创建/修订及受限文本和附件接入已完成 | 真实命令、附件、历史与重载浏览器通过 | 生产规模与导入/导出验收 |
| 身份写动作 | Entity/ExternalIdentity/Binding、重定向和属性已接通 | 注册/绑定/重定向/属性及 Entity soft 删除真实浏览器通过；新旧账本恢复通过 | 生产规模 |
| CognitiveEvent | 正式 dismiss、当前权限/CAS、审计/失效、待投递 Recall 重验已整合 | Required 真实浏览器取消 pending/delivered，历史和 Task 保留 | 当前组合/安装与 15 项真实浏览器回归通过；完整发布门禁另计 |
| Reflection/Candidate/投影视图 | Canonical 只读已接线且 ID 契约回归通过；管理动作和投影 Console 路由未发布 | review/dry-run/replay/来源跳转为设计适配 | 定义正式动作与投影发现 |
| Console Forget 与 Operation | 六类内容、Entity/Focus/State/Task，显式或筛选固定 1–500 项，至多 50 项同步，否则 202/分批执行 | 正式 Operation 响应、分页/问题/取消页面已整合；真实 51 项/首批 50 项/取消流程通过专项 | Schema 20 当前完整组合验收已通过；其他资源与清理状态查询 |
| Persona 发布与回滚 | ADR-0044 实际 CommandActor、Policy/Persona CAS、来源权限、最小回执已整合 | Current 独立页面；发布/重新认证/回滚/刷新与只读角色真实浏览器专项通过 | 主目录组合验收待运行；State/Proposal/Draft/Policy 管理未完成 |
| 保留/Hold 管理 | 未发布 | 保留表单仍为模拟 | 保留影响和 Hold 生命周期管理 |
| 统计八个面 | 未发布 | metric/时间桶、null/stale/warnings/coverage 等模拟 | 正式 metric 结构、可见组聚合、有界查询与真实延迟采集 |
| 导出/下载 | 未发布 | 格式/原因、Operation/行数/bytes/hash/过期 UI | 格式发现、白名单快照、Tombstone 失效、真实 JSONL 往返、Blob 内存上限 |
| 导入 | 未发布 | 字节上传→映射→报告/review→commit，resume/cancel/补偿模拟 | records/映射 Schema、报告绑定、备份 blocked、事务断点、去重/删除优先 |
| Provider | W05 候选；整包验收未完成 | 真实配置/探测/激活/回滚/重建/取消、服务器计划、部署/Worker/Recall 同代装配；90 HTTP/认证回归和 Console 构建通过 | 23 项真实浏览器、双租户 40 次并发及 CI 功能/性能通过；安装包 TLS 失败单独补验通过；真实允许外部 Provider 仍待验收 |
| Settings | 未发布 | registry/validate/原子保存/历史/reset/rollback/待重启模拟 | reset/rollback validate intent、每键行为与实例修订传播 |
| Operation/运维/审计 | Schema21 类型化 memory_forget/trusted_backup Operation 已整合 | 真实批量删除及可信备份创建/重新认证/独立 Worker/进度/刷新通过；完整 CI 通过 | 其余运维与审计由 W14 承接；内部备份不提供内容下载或 Web 恢复入口 |

Task 步骤的 `meta.descriptor` 已正式接线；其余 `src/api/design.ts` 与 mock 描述仍仅为开发模型，不能要求后端迁就其形状。正式切片发布时同时替换类型/适配器和测试 Fixture，再补真实浏览器联调；生成类型通过本身不证明页面兼容。模拟器不得在请求失败后自动启用。

Observation 当前人工提交与注释接入真实 Console 后端：服务端记录当前时间与操作者，注释创建关联 Note，原事件保持不可变；覆盖权限、Scope/Restricted、CAS、重放删除拒绝与事务回滚。真实浏览器已验证当前提交→注释→关联便签→刷新保留原事件，结果见 Phase 14 报告。

W01 Core 接线候选：API/Worker 共用 Recall 装配，Graph 默认接入，Vector 显式配置后可用；真实 ASGI/独立安装 Core 与 Worker/公共 SDK 的 Note 向量命中和两跳 Graph 已验证，2026-09-08 收尾 `ci-002` 已完整通过。Console Provider 页面仍未发布，由 W05 交付配置存储、探测/激活/回滚和真实浏览器闭环；本包没有把该页面标作已交付。证据见 [W01 报告](../../docs/reports/w01-http-recall-assembly.md)。
