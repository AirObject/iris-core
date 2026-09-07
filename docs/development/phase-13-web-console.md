# 阶段 13：Web 管理控制台与手动数据导入导出

> 状态：In progress（第 1、2 步历史验收通过；第 3 步已修复契约/前端并通过真实浏览器读面验证；第 4 步 State/Note/Focus 与 Task 主资源、步骤及依赖写面已接通，其余命令和第 5–10 步继续实施）  
> 核查日期：2026-09-06；开始日期：2026-09-05  
> 前置阶段：[阶段 10](./phase-10-consolidation-reflection.md)；本阶段继续执行，不依赖已暂缓的 Phase 11/12 适配器  
> 当前版本：Schema 20 / Python 0.13.0 / Console Contract 1.1.0；业务契约以生成真源为准  
> 决策：[ADR-0022](../adr/0022-management-console-plane.md)（Accepted，保留其原有状态）；原自动旧数据迁移的取代与保留要求见其迁移影响表  
> 设计：[Console 设计与接入边界](../design/console-backend.md)；证据：[合并验证记录](../reports/phase-13-verification.md)
> 架构依据：[§19 Remember/Correct/Forget 与保留](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#19-remembercorrectforget-与保留)、[§21 备份恢复与导出](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#21-备份恢复与导出)、[§23 HTTP API 与能力协商](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#23-http-api-与能力协商)、[§24 Provider 边界](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#24-provider-边界)、[§29 安全与隐私](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#29-安全与隐私)、[§31 可观测性](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#31-可观测性)、[§34 配置与运行模式](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#34-配置与运行模式)、[§37 旧 Iris 数据迁移](../IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md#37-旧-iris-数据迁移)

## 阶段目标

交付默认关闭、同源且独立的 `/console/v1` 管理平面。运营者通过离线签发的密钥查看和管理授权范围内的数据、手动导入导出、查看统计、配置 Embedding Provider 与调整参数。业务写入复用现有应用服务；上传只接收用户主动提供的数据文件。

认证和两类密钥具备历史完整 CI 证据；读面与类型修复的当前证据见 [Phase 14 记录](../reports/phase-14-verification.md)。未发布业务功能不能作为阶段 14 的现成依赖。

## 架构约束

- Console 与 `/v1` 分离契约、凭据和会话；逐资源、逐事务复核 Grant、Scope、Purpose、Privacy、主体同意与 Tombstone，列表和计数在可见集合计算。
- 业务写入进入共享应用服务与 UnitOfWork，保留 CAS、Revision、Evidence、Audit、Outbox；管理分支只免除宿主 Surface Lease，不跳过其他门禁。
- 删除必须预览后提交 Forget；保护资源、Legal Hold、Tombstone 在 HTTP 与 Worker 两侧均不可绕过。
- 导入拒绝凭据、配置、投影和运行账本；报告哈希、已验证备份、来源去重与同事务 checkpoint 是提交前提。旧材料不会直接成为 Active Claim 或 Persona Current。
- Provider 必须同时接入配置存储、Worker 构建和 API 召回；换向量空间用新 Generation 并 fenced 切换。参数只有真正改变服务行为后才可发布。
- 统计需有出处、新鲜度和有界成本；浏览器 Cookie/CSRF/Origin/上传语义只存在于 API 适配层。

## 需求追踪

| 需求 | 工作包 | 当前状态与完成证据要求 |
| --- | --- | --- |
| P13-PLANE-01 | 13.1 | 已有历史通过：独立契约、默认关闭、`/v1` 兼容、真实路由覆盖 |
| P13-AUTH-01/02 | 13.2 | 已有历史通过：离线 Owner、会话/CSRF/reauth/锁定、密钥隔离和轮换 |
| P13-AUTHZ-01 | 13.3 | 读面与不透明 ID 契约回归通过；完整生产权限/容量验收仍待执行 |
| P13-CRUD-01 / P13-FORGET-01 | 13.4–13.5 | State 创建/更正/过期、Note 创建/编辑/状态转换及 Focus 创建/编辑/激活/状态转换已接通；其他类型专属命令与 Forget 全链待实施 |
| P13-STATS-01 | 13.6 | 未实施：八个统计面、超时降级、rollup lag 与口径 |
| P13-EXPORT-01 | 13.7 | 未实施：一致快照、字段白名单、下载重授权/失效、CSV 转义 |
| P13-IMPORT-01/02 / P13-MIGRATION-01 | 13.8 | 未实施：拒绝类型全覆盖、备份 blocked、报告失效、去重/断点/补偿；ADR-0022 保留的迁移安全要求逐条映射 |
| P13-PROVIDER-01/02 | 13.9 | 未实施：三处接线、探测安全、密钥隔离、Generation 切换/回滚 |
| P13-SETTINGS-01/02 | 13.10 | 未实施：注册表、原子多键修订、每键行为测试、实例生效与 pending_restart |
| P13-OPS-01 | 13.11 | Phase 14 的 memory_forget Operation 已通过当前完整组合验收；任务/DLQ、重建/备份、只读审计、真实就绪维度仍未实施 |

## 工作包

下表保留原工作包编号。历史后端报告采用 10 步编号：第 4 步合并 13.4/13.5，因此其第 5–10 步对应 13.6–13.11，避免把不同编号误读为完成范围。

| 工作包 | 交付内容与依赖 |
| --- | --- |
| 13.1 契约与骨架 | Console 生成链、子应用、启用/独立监听、静态隔离与请求安全头；已实现 |
| 13.2 密钥与会话 | 离线签发、认证、刷新/reauth、运营密钥/会话与 application 凭据管理；已实现 |
| 13.3 授权与读面 | 资源描述、有界列表/详情/历史/引用、lookup/Task 子资源/Persona、签名 cursor；先解决资源 ID 契约并适配前端正式 descriptor |
| 13.4 管理命令 | 已实现 CommandActor/共享事务执行器及 State/Note/Focus 与 Task 主资源、步骤及依赖写入；Event dismiss 与待投递 Recall 重验已按 ADR-0043 通过当前完整组合验收；Persona 发布回滚按 ADR-0044 已通过当前完整组合验收；PersonaState 管理按 ADR-0045 已通过完整组合验收；Proposal 按 ADR-0046 已通过完整组合验收；Policy 按 ADR-0047 已通过完整组合验收；继续补齐其他未实施资源 |
| 13.5 Forget | 固定集合预览/提交、50 条事务/500 条预览上限、批量 Operation；补齐 State/Focus/Task 擦除与关联失效 |
| 13.6 统计 | 指标注册表、八个统计面、rollup/回填、SQLite 有界查询、Recall 耗时采集 |
| 13.7 导出 | 快照、imc-data/v1 JSONL、CSV 报表、产物保留与下载授权 |
| 13.8 导入 | 在导出之后实施；解析/staging→映射/审核→报告/备份→提交/验证，来源账本、批次 checkpoint、取消与补偿 |
| 13.9 Provider | 草稿/探测/激活、出站策略、secret_ref/sealed、向量空间变更重建、三处接线与回滚 |
| 13.10 Settings | 类型注册表、分层解析、水位/实例修订、服务接线、validate/原子写/history/reset/rollback |
| 13.11 运维与收尾 | 统一 Operation、任务/DLQ/调度/Worker、重建、备份、只读审计、探针、部署手册与前后端联合验收 |

每个切片同步契约、真实 Fixture、后端实现、前端生成类型和必要的真实联调；不先发布空壳路由。细化字段与安全语义只在设计文档维护。

## 数据、契约与回退策略

Console 新增 `0012_console_authentication.sql`（3 张认证表、宿主凭据 7 个可空列）及 `0013_console_read_indexes.sql`（附加读索引）；后续 Recall 租户隔离修复新增 `0014_recall_tenant_identity.sql`，此前为 Schema 14；当前依赖生命周期新增 0015，运行时为 Schema 15，离线升级规则见 [ADR-0026](../adr/0026-task-dependency-lifecycle.md)。Console 两项迁移声明 online_safe；旧 SQL 不改写，实际锁时及升级兼容仍需阶段 14 演练。

Console 契约独立生成与冻结；宿主契约兼容另行校验。第 3 步已扩充的契约不代表已通过完整验收。未来 Provider、Settings、导入/导出等表尚未创建，不能引用目标设计声称存在。

关闭 Console 开关不会撤销迁移或丢弃数据；没有已验证的删表式回退。数据库恢复、应用兼容与 `console-auth.key` 的备份/恢复一起进入阶段 14 离线演练。业务导出不是备份，Console 不提供恢复入口。未来导入失败只停止后续批次，已提交部分走显式补偿；Provider/Settings 回滚均保留历史修订。

## 量化验收基线

- 三组不同 Grant 的列表、计数、历史、引用与未来统计无越权泄漏；深页第 20 页之后无重复且命中索引，授权变化后 cursor 全部失效。
- 未知/错误/过期/吊销登录使用相同错误形状；历史每类 20 次耗时测量只是本机有限证据，不能推定无网络侧信道。
- Forget 对保护/Hold/旧 Revision 全部拒绝；erase 后在 Canonical、FTS、Vector、Profile、Graph 与 Recall 的目标内容命中为 0。
- 同文件重复提交两次及每个批次边界中断续跑，重复逻辑资源数为 0；可识别删除 lineage 全部跳过。导出再导入的承诺字段/来源范围一致，差异全部入报告。
- 每类导入拒绝内容都有负例；Provider 的维度、私网/元数据、重定向、DNS rebinding、超限/超时负例全部拒绝；切换完成前旧 Generation 继续服务。
- 每个可用 Settings 键有“改值改变行为”测试；统计超时返回 warnings、落后标 stale。性能数据规模、硬件、阈值和无 coverage 测量方法在阶段 14 冻结后执行，不以未测设计值充当结果。

## 退出门禁

- [x] 13.1/13.2 历史切片具备完整 CI 记录，默认关闭、认证/轮换与宿主契约隔离有证据。
- [ ] 13.3 资源 ID 契约分歧闭环；读面整组及当前工作区完整 CI 通过。
- [ ] 前端生成类型无漂移，正式 descriptor 替换旧设计模型；真实读面/业务 E2E 通过。
- [ ] 13.4–13.11 的业务命令、Forget、统计、导入导出、Provider、Settings、运维完成对应需求门禁。
- [ ] 所有已声明路由、权限/动作发现和页面能力一致；不可用能力保持不可用。
- [ ] 生产同源 HTTPS、可信代理、Cookie/CSP、部署与离线密钥恢复完成验证。
- [ ] 当前工作区全仓库 CI 与独立前端门禁均通过，合并验证报告补入对应版本证据。

## 交付证据

唯一证据入口为 [Phase 13 合并验证记录](../reports/phase-13-verification.md)，其中区分历史切片、有限真实前端联调、mock/Fixture 与本轮复测。

2026-09-06 修复前的历史读面专项为 **53 passed / 1 failed**（Reflection ID 不符合 UUIDv7 Schema）；前端 `npm run types:check` 为 **失败，Console types drift**。当前工作区存在大量未提交实现，不为它虚构提交或新的完整 CI 通过。上述失败已在 Phase 14 修复，当前读面与 Note 写入证据见 [Phase 14 记录](../reports/phase-14-verification.md)。历史认证切片的 10,481 passed/83.90% coverage 不能覆盖新增能力。

## 明确不做

- 不自动连接源库、扫描旧目录、双写/增量追平或编排 Adapter 切换；旧 Iris 专属格式须取得样本后单独确认。
- 不提供 Web 恢复、SQL/镜像导入、任意路径/表达式/脚本、自动身份合并或跨租户超级管理员。
- 不通过参数关闭安全门禁；不在 validate/review 时把上传原文发往 LLM/Embedding。
- 不保证识别身份与内容均改写且无 lineage 的旧删除材料；不保证回收已下载副本。
- 多运营者以密钥 ID 记名；独立 operator/RBAC 和 TOTP 不在本次必交范围。

## 交接条件

[阶段 14](./phase-14-hardening-release.md) 首先收口第 3 步和 13.4–13.11，再进入完整发布硬化；当前可复用认证/托管/读面代码及历史证据，不能稳定依赖尚不存在的其他 Operation 类型、导入报告、Provider/Settings 表或探针维度。导入测试数据集只能在真实导入链交付后用于发布验收，之前使用应用服务构造的明确测试夹具。

手动迁入需要运营者自行导出源数据并分批操作，不再提供跨系统一致性切换工具；大体量历史数据迁入成本这一代价仍需在运行手册中说明。
