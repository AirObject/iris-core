# ADR-0038：Entity tombstone 的完整删除账本与恢复

状态：Accepted，2026-09-07。继续 [ADR-0037](0037-console-durable-forget-previews.md)，补齐 Entity 删除及历史恢复缺口。

## 账本与历史数据

既有 IdentityService.tombstone_entity 只写 Entity 状态、Tombstone、审计和 Graph/Profile apply，没有 ForgetRequest。因此最新删除账本无法阻止更早备份恢复已删除实体。本次将原领域事务抽取为共享方法，把 Entity 状态、Tombstone、删除账本、审计、缓存清理与投影任务放入同一事务。

账本使用 resource/entity selector，使用可由 SQL 构造的专属 selector_key `entity-tombstone:v1:<id>`。这不扩大普通业务 ForgetService 的六类内容准入。Entity 删除保持 soft，不擦除历史 Binding、ExternalIdentity、属性或 Relation 的原始 ID。

Migration 0017 为已有 Entity Tombstone 补写缺失账本，保留原 actor/reason/time/sequence。补写条目采用原 Tombstone UUID 作为账本 ID，明确以迁移命名空间及原 UUID 标识请求，不伪造原业务幂等键。已存在该实体专属账本的对象跳过。迁移一次物化已有账本再按租户和 selector 查重，避免按历史对象反复扫描整租户。迁移扫描历史数据，要求停机与验证备份；新空库允许 bootstrap。旧 Migration 文件不改写。

恢复按原账本身份重放，不能把一个 Entity 条目当 Artifact。重复重放不重复修改，恢复后原有或发生时身份解析均不能返回已删除实体。若备份早于实体创建，也记录该 ID 的 Tombstone，避免后续旧输入恢复它。恢复阶段重现既成删除，不以备份内后来失效的 Hold 或保护状态撤销删除事实。

## 权限、保护和预览

Entity 加入既有持久预览与原子提交，最多 50 个显式目标。仅允许 soft；注册表提供 forget_modes，界面按目标集合取交集，服务端请求 Schema 与领域协调器均拒绝包含 Entity 的 erase 请求。

静态 entity.forget 要求 memory.forget/read、console.manage、当前真实 CommandActor 和租户全局修改权限。使用最新 Scope/Privacy 与重定向链授权。Entity 属性可不增加 Entity 修订，因此删除摘要另外绑定完整受限属性快照版本；预览后属性变化也要求重新预览。

实际 Agent→Entity self_entity 关系属于结构保护；Subject Legal Hold 阻止 Entity tombstone。这两项在共享领域入口执行，原 admin 入口也不能绕过，必须先依法/按业务流程处理限制。普通 admin 能力门禁仍保留，Console 不伪造 admin。保护查询使用现有 target 索引、150ms/200万步预算，Hold 集合超过 500 条失败关闭，不截断后继续写入。

## 批量顺序与结果

全部初始授权、修订、属性、Hold 与水位检查完成后，根据当前 ReadRecord.requires 对已选择目标排序，先删除引用方，再删除其必要目标。例如，输入先写 Entity 后写 Claim，实际先处理 Claim；重定向源先于目标。这样不会因为本事务刚删除了目标，就把同批已授权的引用方当作外部并发变化。目标集合和输入序号保持不变，不追加任何关联对象。

提交仍返回不含正文的最小回执。Binding/ExternalIdentity 原始 ID 与历史记录保留；普通详情、属性、历史和当前/发生时身份解析持续受 Tombstone 限制。Graph/Profile 通过持久任务清理，不能据 Canonical 成功宣称异步工作全部完成。

## 版本与验收

Core 0.13.0 开发候选采用 Schema 17，SDK 保持 0.11.1。运行窗口为 17；0017 升级须停 API/Worker 并验证备份，回退使用升级前安装物和隔离备份，不原地降级。

验证包含新旧 Entity 删除账本、历史 backfill、备份早于创建/删除、重复恢复、事务失败回滚、保护/Hold、当前权限、属性变化、混合目标顺序、历史 ID 保留及真实浏览器仅 soft 流程。结果见 [Phase 14 验证报告](../reports/phase-14-verification.md)。大批次 Operation、筛选集合、其他类型删除与生产发布门禁仍未完成。
