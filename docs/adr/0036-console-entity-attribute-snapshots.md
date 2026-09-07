# ADR-0036：Console 实体属性快照与权威合并

状态：Accepted，2026-09-07。继续 [ADR-0035](0035-console-bounded-entity-redirect.md)，实现实体属性读取与记录；tombstone/Forget 仍单独实施。

## 独立属性语义与并发

既有 IdentityService.record_attribute 写 identity_attributes，不修改 Entity.display_name 或增加 Entity 修订。Console 保留这一行为，不把独立属性记录伪装成 Entity 基础标签编辑，也不向旧 Entity 修订附加今天的属性。

`GET /console/v1/memory/entities/{id}/attributes` 读取当前授权实体，返回包含当前属性和冲突的 ResourceViewEnvelope，以及完整快照的 attributes_version。属性显示 ID、字段、值、权威、状态和精确 RFC3339 时间；不直接披露旧业务输入的任意 source_ref 字符串。独立 Entity 列表、详情和修订历史保持原字段语义。

`POST` 同一路径要求 expected_revision 与 expected_attributes_version，分别校验实体状态和属性集合。只校验 Entity 修订不能发现其他业务调用方的属性写入。属性快照以完整当前/冲突记录计算 SHA-256；这是并发令牌，不能代替当前 Grant 和关联实体授权。写事务重新读取并比较两项，任何变化返回 revision_mismatch，界面保留草稿、显示最新差异并要求重新打开编辑。

## 权威、审计与结果

请求字段仅为属性名称、值和 confirmation/correction 模式，另带 operator_request 原因。字段名 1–128 字符，值 1–4096 字符，拒绝 NUL 和非法 Unicode。authority、source_ref、状态、有效时间和租户不能由请求伪造。

静态 entity.attribute 使用 memory.write、租户全局写授权和真实 CommandActor；已重定向或删除实体不接受新记录。服务器根据显式操作者动作选择 admin_confirmed 或 explicit_correction，以实际 console 操作者生成 provenance，时间由服务器产生。共享事务方法复用原权威规则，原公开业务入口的能力检查保持，不构造 admin AccessContext。

较高权威替代当前值；相同或较低权威的重复值被忽略；较低权威不能覆盖；同级不同值保留为 conflict，即使两者都是 explicit_correction。界面明确显示“属性已记录并生效”“保留原属性，未覆盖”或“冲突已记录，原值继续生效”。旧 superseded 记录仍在 Canonical 中保留，本切片的面板明确仅展示当前值与冲突，不宣称提供完整属性历史分页或冲突裁决接口。

属性、领域审计、Console 审计和成功回执同事务提交。回执保存首次属性快照与合并结果，后续属性变化不改变幂等重放响应；返回前仍检查当前实体、密钥、Grant 和重定向关联链，删除或隐藏后不能通过缓存取回。普通失败保留可重试的 admission 租约，不伪造成功结果。

## 读取预算与兼容

每个属性快照最多 250 条当前/冲突记录，field/value/source_ref 的原始 UTF-8 字节合计不超过 256 KiB。先在 SQLite 查询长度，满足预算后才把完整值载入 Python；旧业务超大值也不能绕过预算。查询另设 150 ms、200 万 SQLite 步数限制，全部路径清除进度处理器，不改变写连接的 query_only。超过预算拒绝整个快照，不计算部分集合的并发令牌；写入后的集合超限会回滚。不可表示的旧时间返回稳定的完整性冲突。

新增两个 Console 操作、两个请求 Schema、十个夹具和 attributes 动作枚举。没有 Migration，Core 0.13.0、Schema 15、业务 HTTP、SDK 和 CLI 保持不变。专项覆盖并发快照、全部权威结果、不可伪造 provenance、当前 Grant、关联链删除、容量、失败重试、隔离恢复和真实浏览器。结果见 [Phase 14 验证报告](../reports/phase-14-verification.md)。生产容量、属性历史分页/裁决和完整发布门禁不据此宣称完成。
