# ADR-0031：有向 Relation 更正、证据修订与状态管理

状态：Accepted，2026-09-07。实现 [Console 资源矩阵](../design/console-backend.md#51-资源矩阵) 的 Relation 创建、更正与状态动作，遵守 [ADR-0013](0013-phase5-long-term-memory.md) 的证据与双时间规则，以及 [ADR-0025](0025-console-command-authorization.md) 的真实管理身份授权。

## 有向关系的专属更正

Relation 是有方向、有类型、由证据支撑的 Entity 关系。管理创建要求两个不同且当前可见的实体、非空关系类型和有效支持证据；Scope、隐私和证据经过当前操作者 Grant 验证，不能以昵称相似性创建关系，也不能自造 source_authority、租户或 origin。

correct 是专属领域动作，可以更正起点、终点、关系类型、评分或有效时间，但必须提供新的纠正证据输入和 expected_revision。保持 Scope 与隐私信封，只有 active/disputed 关系可以更正；更正后的状态为 active。新方向、端点与有效期写入新 Revision，并在同一 CAS 中更新 Current。旧修订保留旧方向、类型、评分、证据和有效期，唯一允许追加的历史字段是其系统时间终点。

如果拟议身份（两端、方向、类型、Scope 和有效窗口）已对应另一个关系 ID，操作拒绝，不自动合并。隐藏冲突目标使用未找到结果，不披露其内容；更正不修改 Graph 表，后台通过 canonical relation.changed 更新投影，Final Rehydrate 在修订改变时丢弃旧图候选。

## 证据与重复关系

输入证据沿用已支持的 Observation/Artifact/Episode/Claim/Note 类型，每次最多 32 条。创建只接受 supports；更正将默认 supports 转为 corrects，显式证据关系按既有领域语义保留。源必须有效、当前授权可见、未删除并位于关系的 Scope 信封内。操作者输入不自动证明陈述为真，不能声明平台或管理员来源权威。

更正保留原证据并追加对应证据，符合 Claim 更正已有的历史纪律。不采用“先失效所有证据再重新插入”的方式：规范化 Evidence 的唯一身份包含已失效行，误恢复它们可能复活删除证明。新 Relation 修订保存当前有效证据集合；管理读取新修订时只查询有效证据，最多取 501 条以识别超过 500 条的读面上限，不把所有失效历史加载进内存。

管理创建若命中已有关系，必须重新授权真实去重目标，且只允许 active/disputed。完全重复的证据返回现有修订；实际新增证据则追加内容不变的新修订、系统时间终点、CAS、水位、审计和 Outbox。这样新的关系证据不会因沿用旧修订的 Outbox 去重键而漏掉发布。普通业务 create 的既有接口与去重行为保持不变；这是明确管理命令的追加修订语义。撤回、被取代或归档关系不能借创建命令重新激活。

## 状态、删除与原子性

transition 只接受状态、expected_revision 和操作原因，复用 RELATION_TRANSITIONS；不能原地修改状态或用通用 PATCH 改字段。active/disputed 必须仍有有效证据。每次状态流转追加新修订，旧修订终点取 `max(now, created_us + 1)`，确保非零系统时间区间。

修订、前驱终点、规范化证据、Current 方向/字段/计数、审计、水位与 Outbox 在同一事务提交，任一步故障整体回滚。来源 Forget 使用既有证据死亡级联；即使关系经过更正，所有证明失效后也必须转 retracted，恢复不能复活来源或关系证据。

管理身份只在经过 command_access 的明确服务入口使用；普通业务创建的 Required Surface 门禁保留。重放重新授权当前操作者、实际目标、两端及所有来源，删除后不返回历史成功。响应固定 Relation 修订，关联证据仍沿用既有读面的当前集合语义，不把该集合声明为历史冻结快照。

## 契约与验证

Console 1.1.0 开发候选新增三个 POST 操作和五个 Schema；提供创建、方向/类型更正、状态动作和历史浏览，继续不提供通用 Relation PATCH。Core 0.13.0 / Schema 15 无新 Migration，业务 HTTP、SDK 和 CLI 不变。

验证覆盖 Required 模式真实创建/更正/流转、隐藏去重和碰撞目标、权限与证据负例、幂等/Revision 冲突、整笔故障回滚、原证据 Forget 级联及恢复、召回期间方向更正、真实浏览器创建/反向更正/撤回/历史。证据见 [Phase 14 报告](../reports/phase-14-verification.md)，不代替其余管理模块和生产门禁。
