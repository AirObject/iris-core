# ADR-0025：Console 管理命令的授权事务

状态：Accepted，2026-09-06；落实 ADR-0022 与 Phase 13/14 的唯一写路径要求。

## 决定

内部 `CommandActor` 从真实运营会话构造，记录 Console 来源、运营密钥及修订、Grant 指纹、会话 Epoch、目标 Scope、操作和 reason。它不是 HTTP DTO，也不能作为 `/v1` 的 AccessContext 或 Bearer 凭据。`manual_import` 预留来源只有后续导入执行器接线后才能使用；当前工厂只产生 Console actor。

管理命令以静态操作策略进入 `ConsoleCommandExecutor`。授权在幂等缓存之前、领域提交事务内部和结果发布之前执行；Grant 在预检查和写事务之间发生修订时拒绝提交。业务幂等分区与宿主写入分离，指纹包括目标 Scope、资源、来源引用、Privacy、reason 和业务输入；授权快照不是业务输入，新有效授权可重试同一业务请求。结果引用重新检查当前可见性，不能凭历史成功恢复权限。

读者看到上级 Scope 的数据，不表示可以修改影响全部下级的数据。有限 selector 只能修改该维度被 Scope 或合格 Privacy 标签明确限制在 Grant 内的资源；空 selector 永远无权修改。完整关系、Tenant、Subject、Restricted、自定义标签、Tombstone 和来源引用都在同一事务检查。该规则仅增加管理写授权，不改变既有读取的向下可见语义。

领域命令必须复用现有 Revision/CAS、Evidence、保护、Watermark、Audit 和 Outbox 实现。接线管理来源时只明确跳过在线宿主 Surface Lease 条件，不能临时装配 `surface=None`，不能制造应用凭据或设置租户 admin。该执行器不提供发送消息、Usage、ACK、任意 SQL、队列载荷或函数查找能力。

## 当前实施边界与验证

已接通 Note 创建、编辑和状态转换的领域命令、严格请求 Schema、描述接口和真实浏览器消费。编辑复用 CAS；Task/Claim/Episode 提升复用现有领域实现。Focus 创建、编辑、激活和状态转换也已接线；其余资源写入和 Forget 尚未接线。验证包括有限 Grant 的父 Scope 写拒绝、来源 Privacy、会话/密钥过期吊销、提交窗口和缓存窗口授权变化、幂等隔离及事务回滚。

Restricted 标签按显式 Console Grant 授权；仍执行 Subject、自定义标签及合格 Scope 标签校验。幂等结果保存原修订及同事务更新时间，重放先复核当前权限与目标，再返回原修订，后续编辑不会把创建结果改成最新正文。详情页动作根据当前权限和状态发现，提交时再次判断。

无数据库迁移，无 `/v1` 变更。回退可以关闭管理写路由；不能把请求导向在线宿主接口绕过同一授权。

## Focus 容量副作用

管理创建、摘要编辑和 dormant 激活可能触发容量淘汰。每个被淘汰条目先独立复核同一运营者的当前 Grant 与完整 Scope/Privacy，再进入既有 dormant 修订事务；不能只验证请求的主目标。任一目标无权修改时整笔事务回滚。相关结果引用参与幂等结果重授权，重放不会再次淘汰。管理摘要编辑只计算自然衰减，不人为提高 activation；显式激活继续使用既有受限提升。

Focus 真实提升与来源隐私继承现按 [ADR-0027](0027-focus-promotion-materialization.md) 实施；初始仅发布 dormant/dismissed/expired 的限制由该决定替代。

## State 的来源、过期与幂等回执

State 管理创建和更正固定 `source_authority=user`，不接受 source authority、观测时间、调度 coalesce key 或任意来源字符串；只允许当前 Namespace Policy 接受 user 的 namespace。包括 runtime/environment 在内的其他 namespace 仍拒绝，不能因运营者为 Owner 而升级来源。策略在幂等缓存之前、写事务和结果发布时复核。

创建显式 expected_revision=0 表示自然键必须不存在；已有自然键返回修订冲突，不能把创建变成覆盖。更正和过期要求当前正整数修订。更正复用原 PUT 的 TTL/字节/Scope 校验、修订、历史裁剪、CAS、Watermark、Audit 和 coalesced Outbox。显式 expire 写入一条 user 来源、观测时刻等于到期时刻的零有效期修订，不伪造更早的观测时间；在线读立即排除它，Console 可见其过期状态。

State 历史可按 namespace 策略只保留当前修订，因此写响应为无正文的 ID/Revision 提交回执。幂等重放不需要恢复已裁剪的旧值，不把旧正文额外写进幂等缓存。当前权限、策略与 Tombstone 仍高于历史成功。State Forget、自然键删除后显式重建与恢复全链另行实施，不因新增 expire 而声明已完成。

## Task 的完成证据与人工操作

Task 管理创建、字段更新和生命周期转换进入既有 TaskService 事务。创建以明确的人工操作激活计划，审计来源为 console；不接收 origin，也不设置 AccessContext.admin。所有者实体与输入来源必须当前可见，space_group 所有者要求对应 Scope。修改和转换要求父 Task 的当前 Revision。

管理完成必须提交非空 Observation/Artifact 证据。除 Console Grant 的可见性外，继续验证证据已提交、effect 非 partial/failed、Tenant/Agent/Scope、Privacy 与 Tombstone，并检查全部步骤已终结。完成证据写入同条 TaskRevision 的 source_refs 和 completion_evidence 关联；失败整笔回滚。每个输入数组至多 100 项，内部授权允许原来源、完成证据及所有者共 201 项；不扩大公开数组预算。重放仍复核输入证据，删除后的证据不能凭成功缓存重新发布。

当前已接通 Task 主资源及步骤创建/状态流转；依赖创建、解除与恢复见 [ADR-0026](0026-task-dependency-lifecycle.md)。触发器的创建、编辑和启停规则见下文。证据关联不代表 Forget、恢复与 Final Rehydrate 的完整验证已完成。既有在线 Task 契约与 Lease 规则保持原行为。

## Task 子修订与跨平面聚合 CAS

步骤创建与转换要求父 expected_revision，转换另需 child_expected_revision。授权覆盖父 Task、目标步骤及就绪状态实际改变的每个后继；任一检查失败，子修订、父修订、审计、Watermark 与 Outbox 全部回滚。成功命令返回无正文的 Task/Child ID 与两项 Revision 回执；父修订只推进一次，步骤可能因初始就绪推导从修订 1 变为 2。

聚合修订必须覆盖两个调用平面：既有在线步骤创建/转换、依赖创建、触发器创建和内部启用/禁用也在原事务中推进父 Task 修订。幂等重放不再次推进。既有请求、响应与 SDK 方法签名不变，但调用方不得继续复用子资源写入之前的父修订；后续修改 Task 时先重新读取当前修订。这是修复跨平面并发检测，不把旧子资源写入排除在管理 CAS 之外。

状态触发器使用最近一次真正改变 status 的修订作为 occurrence 身份，忽略其后的字段或子对象修订；否则新增聚合修订会漏掉 from_status 匹配或重复触发同一状态。该读取复用不可变修订历史，保持现有 Scope/Tombstone 检查与初始状态触发语义。


## Task 触发器的配置与调度进度

管理创建、编辑和启停复用 TaskService 的 TriggerRevision、父子 CAS、Watermark、Audit 与 Outbox。每次配置操作仅推进一次父 Task 修订；Worker 的 next_fire_at/last_scan 进度不推进配置或父修订。终结父计划、跨父触发器、失效修订和被删除对象拒绝修改。启停保留调度游标；编辑明确创建新配置并从当前时刻重新安排周期，条件触发可再次匹配。表单显示该重新安排语义。幂等重放返回原提交回执，不重置已推进的游标。

仅接受 at_time、interval/daily 周期、Observation 类型、State 比较和 Task/Step 状态条件，保留既有时区/DST、catch-up 与 misfire 规则。不接受脚本、Worker 进度、事件/投递结果或任意来源字段。父计划、关联步骤、被观察 Task/Step 都须当前授权；跨 Scope 来源、隐藏来源与 Tombstone 在预检查、提交及结果发布时失败关闭。原在线条件的延迟绑定行为保持兼容，由 Worker 在最终匹配时检查来源；管理写入要求引用已存在且当前可见。Worker 另检查触发器本身与关联步骤的删除标记。

列表只返回摘要，结构化时间计划和条件不从列表恢复编辑草稿。单个触发器详情接口在同一读事务检查父对象归属与完整来源可见性，表单读取详情后编辑。严格请求 Schema 与独立 Console 1.1.0 开发候选同步生成；正文为空的提交回执只含父/子 ID 和修订。当前实现使用 Schema 15 的既有触发器表，无新增迁移或业务 SDK 方法。


触发器详情的父路径归属直接核对 Canonical task_id，不以来源引用中的任意 Task 代替。恢复校验核对 current 与所指配置修订及真实父计划/关联步骤的一致性；调度进度不参与该静态配置比较。
