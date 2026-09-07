# ADR-0028：当前人工 Observation 与不可变原事件注释

状态：Accepted，2026-09-07。实现 [Console 资源矩阵](../design/console-backend.md#51-资源矩阵) 的 Observation 管理能力，沿用 [ADR-0025](0025-console-command-authorization.md) 的管理命令事务边界。

## 当前人工提交

Console 创建只接受 Scope、正文、隐私标签和操作原因。实际提交被记录为 role=user、kind=console.manual_submission；发生与确认时间取本次服务端时钟，app_instance_id 保留真实 CommandActor 的 console/key 审计标识。它表示操作者此刻向 Core 提交的内容，不声称来自某个平台用户或证明某次历史外部效果。

请求不能传入历史时间、来源游标、外部事件/发生 ID、外部身份、发生时 Entity、效果状态/证明、任意结构化 payload 或附件。历史消息继续通过审核导入。每笔管理请求使用原有幂等事务，服务端分配内部记录幂等身份，重放返回原 Observation，不追加第二个事件。

ObservationService 提供明确的 create_for_command 接口，验证真实 CommandActor 与当前 Grant，再复用原有 Draft、容器/身份验证、Journal/Outbox/Watermark/Audit 原子写入。Required Surface 只对该已授权管理分支分流；普通 observe_batch 的写前与事务内 Lease 检查保留。管理命令不伪造 bearer 凭据，不构造 admin 上下文，也不通过关闭 Surface 服务来绕过门禁。

## 注释保留原始事实

`observations/{id}:annotate` 在同一事务检查原 Observation 的 Scope/Privacy/Tombstone 和 expected_revision。NoteService 创建 important/inbox 便签，保存操作者输入的标题、正文、重要度，默认七日复查。便签继承原观察隐私标签、固定原观察 SourceRef，并追加 annotated_by 关联。原观察正文、修订、发生时间、发生时身份、效果和游标均不更新。

当前动作产出关联 Note，不伪造新的外部 Observation 或自动将说明认定为已验证事实。后续使用该 Note 作为 Evidence 仍遵守证据所属领域规则。失败时便签、修订、关联、审计、水位和 Outbox 一起回滚；原观察不受影响。请求重放同时重新授权原观察与实际便签，来源/结果删除或授权失效不能返回历史成功。

## 契约与界面

独立 Console 1.1.0 开发候选新增两个 POST 操作、四个请求/字段 Schema。Observation 保持 append_only、无 update_schema；可写描述发布当前提交表单与 annotate 动作，详情按当前 Grant 发现动作。浏览器创建当前观察、添加关联便签，并在刷新后读取两者。业务 `/v1`、SDK 和 CLI 不变；继续使用 Core 0.13.0 / Schema 15，无新迁移。

本决定不代替 Forget、历史导入审核或生产发布门禁。实施证据记录在 [Phase 14 验证报告](../reports/phase-14-verification.md)。
