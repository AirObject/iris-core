# ADR-0033：Console 有界原始附件上传

状态：Accepted，2026-09-07。继续 [ADR-0032](0032-console-immutable-text-artifacts.md) 的不可变 Artifact，完成 [资源矩阵](../design/console-backend.md#51-资源矩阵) 的受限原始文件上传。

## 协议与接收边界

`POST /console/v1/memory/artifacts:upload` 使用 application/octet-stream 原始请求体。唯一 query 参数 metadata 是 UTF-8 JSON，OpenAPI 通过参数的 `content["application/json"]` Schema 明确描述；不是服务器路径或外部下载地址。元数据仅含 Scope、受支持的媒体类型、隐私标签、至多一个来源和操作原因，最多 8 KiB；编码后的 query 总长最多 32 KiB。未知或重复参数、重复 JSON 字段、非法 Unicode、路径、URL、文件名和客户端哈希均拒绝。

文件限 1 字节至 8 MiB，每个 API 进程最多两个正在接收或提交的上传，接收超时 60 秒。声明长度过大时立即拒绝，实际流字节数仍是最终上限；重复 Content-Length、长度与 Transfer-Encoding 并存、实际长度不符以及 Content-Encoding 均拒绝。全局 bootstrap 的 50 MiB 文件上界不等于本模块上界；Artifact 描述器与界面明确显示 8 MiB。

浏览器会话、Origin、CSRF 和幂等键沿用管理认证。读取文件前检查当前 Grant、Scope 和来源，接收期间不持有 SQLite 写事务。接收完成后，管理幂等事务再次验证身份、Grant、来源与实际目标，不继承上传开始时的授权。进程内并发限额在所有完成、超时与断开路径释放；部署多个进程时分别应用限额，进程级容量与反向代理预算继续由生产验收约束。

原始字节复用现有浏览器 Blob/XHR 传输，显示进度并等待服务器确认；关闭对话框或会话切换会终止客户端请求。响应丢失后保留原文件和幂等键；更改文件、元数据或原因时生成新键。客户端文件名仅用于本地文件选择，不传入 metadata、不决定服务器存储位置。Console 自身访问日志不记录 query 或正文；部署访问日志也不得记录完整 metadata。

## 存储、失败与完整性

明确的 artifact.upload 管理入口复用 ArtifactService local_blob 入库。ID、shard locator、SHA-256 和大小由服务器生成。每次成功写入立即按记录大小和哈希验证；命中去重目标先重新授权、验证文件完整性，再检查不可变媒体类型和来源，不能借上传覆盖原文件。

当前调用记录成功分配的文件。事务、提交或后续结果处理失败时，只清理本次分配且 Canonical 中仍不存在对应 ID 的文件；已经提交的文件和去重复用的文件保留。这使提交失败不会留下普通异常路径的孤立文件，提交成功但响应丢失仍可安全重放。SIGKILL 不能由异常处理恢复，孤立文件的进程故障对账仍属于后续生产恢复门禁，不能把本切片称为文件系统与 SQLite 的原子事务。

本地 blob 读取先检查受控路径，以 NOFOLLOW 打开最终文件、使用 NONBLOCK 避免 FIFO 阻塞，验证打开句柄为普通文件及记录长度，再最多读取 expected_size + 1 字节并校验 SHA-256。记录长度本身不得超过既有 Core 64 MiB 存储上限。这样损坏的大文件、FIFO、符号链接及读取时的长度变化不会导致无界读取。普通业务接口签名与既有媒体类型集合不变。

成功与重放返回授权的 Artifact 元数据，包含十进制字符串大小，不返回本地 locator、任意下载 URL 或二进制正文。文件不自动解析、执行或在同源 iframe 中渲染。Forget 使用既有 Canonical Tombstone 和提交后清理，恢复后仍可验证原始字节及再次执行擦除。

## 契约与验证

Console 开发候选新增一个操作、三个 Schema、描述器可选 upload/upload_schema；生成器支持显式 request_media_type，其余请求继续使用 JSON。没有新增 Migration，Core 0.13.0 / Schema 15、业务 HTTP、SDK 和 CLI 保持现状。

验证包括精确字节边界、流式超限/超时、模糊 HTTP framing、接收前拒绝、并发限额、上传途中来源删除与会话失效、Restricted/只读/跨 Scope、不可变去重、提交前后故障与重放、损坏文件和 FIFO、真实 Forget、备份恢复，以及真实浏览器文件上传。具体结果见 [Phase 14 报告](../reports/phase-14-verification.md)。Console Forget 编排及生产隔离、进程故障与发布门禁仍单独验收。
