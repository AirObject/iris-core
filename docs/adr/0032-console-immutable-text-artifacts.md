# ADR-0032：Console 不可变原始文本 Artifact

状态：Accepted，2026-09-07。本切片实现 [资源矩阵](../design/console-backend.md#51-资源矩阵) 的 inline 文本创建，复用 [ADR-0013](0013-phase5-long-term-memory.md) 的内容寻址存储和 [ADR-0025](0025-console-command-authorization.md) 的管理命令边界。受限二进制上传继续作为独立待完成项。

## 创建与不可变内容

`POST /console/v1/memory/artifacts` 接受 Scope、原始 UTF-8 文本、text/plain 或 text/markdown、隐私标签、至多一个类型化来源和操作原因。正文非空、无 NUL，按编码后的字节数执行既有 256 KiB inline 上限；JSON 请求另受 1 MiB、深度、重复字段及非法 Unicode 限制。客户端不能指定租户、来源权威、origin、存储类型、路径、URL、内容哈希、大小或发生时间。

ArtifactService 的明确管理入口验证当前 CommandActor、Scope、隐私与来源，再复用已有入库事务。存储 ID、locator、哈希和大小由服务器生成。管理不伪造业务凭据或 admin，原在线入口的 Required Surface 门禁继续生效。

内容不可 PATCH，更换正文需创建新的 Artifact，再使用引用资源的正式更正命令。媒体类型和原始来源也不在去重时悄悄替换。相同 Scope、隐私集合、存储类型和内容哈希命中已有对象时，先检查真实目标的当前读写授权与完整性；媒体类型或来源不一致返回冲突。完全相同输入复用原 ID，仅按照现有领域规则增加引用计数；同一幂等键重放不重复增加。正文改变创建新 ID，不覆盖旧对象。

## 授权、完整性与删除

创建、返回结果和幂等重放都重查当前会话与 Grant；引用的来源或目标已删除时不能返回缓存成功。去重目标不能用新对象的假定权限代替实际对象授权。Restricted 仅在明确 Grant 检查通过后进入管理分支，不改变业务读写的隐私规则。

Console 在完成授权后的文本披露阶段校验实际 bytes、记录大小和 SHA-256，再严格解码 UTF-8。列表摘要、详情、命令结果和去重命中均使用这一校验；不返回损坏内容，也不通过错误暴露数据库或正文。界面以普通文本显示 Markdown 和 HTML 字符串，不执行上传文本中的脚本。所有视图继续隐藏文件系统 locator。

新行、引用计数、水位与审计共用管理幂等事务，后续故障整体回滚。Artifact 不直接参与现有变更投影，没有新增虚假的 artifact.changed Outbox 类型。Forget 继续调用既有擦除语义，删除正文并保留删除账本；备份恢复不得复活内容。Console Forget 编排、原始附件的流式上传和异步本地 blob 清理验收尚不由本切片声明完成。

## 契约与验证

Console 1.1.0 开发候选新增一个操作和两个请求 Schema，提供文本创建表单；无内容编辑、自动 URL 抓取或任意服务端路径输入。Core 0.13.0 / Schema 15、业务 HTTP、Python/TS SDK 和 CLI 保持现状。

验证包括真实 Required 模式创建、去重与不可变替换、隐私和 Scope、非法元数据、编码字节上限、来源/目标删除后重放、损坏内容失败关闭、事务回滚、真实 Forget 和备份恢复，以及浏览器保存、刷新和安全显示。执行证据见 [Phase 14 报告](../reports/phase-14-verification.md)。
