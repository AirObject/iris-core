# ADR-0048：类型化 Operation 与内部可信备份

状态：Accepted，2026-09-08。承接 [ADR-0042](0042-console-forget-operations.md)，实施与验收记录在 [W04](../reports/w04-operations-trusted-backup.md)。本决定先于 Schema 21 实施。

## 持久化与类型边界

Schema 21 将 console_operations 拆为共用归属、状态、Revision CAS、进度、时间与当前作业元数据，以及独立的 Forget 和 trusted_backup 明细。Forget 的 preview、mode、私有目标快照、删除水位、Hold 指纹和预览唯一性原样迁移；原 1–500 根、最多 500 问题及原输入序号约束只适用于 Forget。备份进度以步骤计，不继承根集合限制。注册表只包含有真实处理器的 memory_forget 和 trusted_backup；请求不能提交任意 kind、函数、模块或文件路径。

迁移声明 offline、backup、bootstrap_safe；已有数据库必须停止 API/Worker 并完成真实备份校验后迁移。0001–0020 不改写。Core 推进到 0.14.0，运行窗口仅 Schema 21；首次空库可按已声明的 bootstrap 例外创建。回退恢复升级前备份并使用原 Core 0.13.0，不提供原地降级。旧 Schema 20 备份仍可隔离恢复并随后升级。

## 授权与执行

备份创建是独立的 Console 命令，要求 backups.write、console.manage 数据用途和最近重新认证。全实例快照由系统保管；该授权只允许请求保护性快照，绝不授予读取其他租户、目录列表、文件路径或下载权限。请求只接受 operator_request 和幂等键。使用实际 Operator Key、Revision、Grant 指纹、Session ID/epoch；不伪造管理员 CommandActor。读取、问题页和取消仍按当前创建者归属，并使用对应类型的权限。

接受事务同时写入 Operation、类型明细、Audit、幂等回执和既有 Outbox。Worker 在真实授权检查后，通过注入的受控归档端口在写事务外创建并校验备份，再在既有 Outbox Lease fence 的同一提交事务内复核原凭据、Operation Revision/作业代次和取消状态，发布内部结果。既有 owner、generation、expiry、source_revision fence 保持有效，不增加队列。外部归档按 Outbox ID 与 Lease generation 派生独立不透明引用；同代重放复用已校验内容，接管使用新目录，旧 Worker 不能覆盖新文件或发布结果。损坏或部分写入不得被当作成功。

备份结果保存随机不透明引用和实际 manifest 摘要；只有实际 verify 通过且摘要与受控文件一致才可提交 completed。内部前置条件端口在使用时再次校验文件与已登记摘要；未完成、不可用、已损坏或恢复后失效的备份返回稳定阻塞原因。该端口供后续导入消费，本包不注册导入处理器。Console 元数据保持 result_ref 为 null，内部引用不成为跨租户下载能力。

取消在结果发布前使 Operation 终态化，并阻止晚到结果提交；可能已经生成的未引用归档只保留在内部目录，取消不承诺中断 SQLite 快照或删除已保管备份。执行异常通过现有有界重试与死信处理，绝不提前签发成功凭证。配置不可用为 blocked / backup_unavailable；权限变化为 blocked / authority_changed。元数据读取不修复进度、不执行备份。

包含备份的 Worker 批次在准备期间调用既有 Outbox heartbeat，间隔为配置租约长度的三分之一，保护同批已领取但尚未执行的作业；批次结束立即停止续租。续租仍校验真实 owner/generation/live expiry，不能复活已失效租约，续租失败不绕过最终提交 fence。此协议续租不创建 automation，也不按时间切换工作包。

可信备份除了文件校验、签名（如配置）和 manifest 摘要，还校验 Canonical 数据库结构不变量及备份身份；普通文件校验与可供内部前置条件使用的可信凭证保持明确区别。

## 恢复与契约

隔离恢复继续按 ADR-0042 核对 Forget 的已提交删除前缀。所有备份 Operation 的外部文件引用均不能随数据库快照自动恢复为可信：清除结果、停用待执行意图，并将相关记录标为 blocked / restore_requires_review；旧作业不得自动生成新备份。重复恢复维持该边界。历史 Forget 明细、问题和终态保持可读。

业务 Contract 1.11.0 和业务 SDK 方法不变。Console 1.1.0 未发布候选增加备份创建路由及已实现 kind，更新真源、生成 OpenAPI/JSON Schema/TS 类型、fixtures 和接口快照。继续沿用现有 Operation 查询与取消界面；本包不完成其他运维页面。

## 验收

真实 Schema 20 历史升级及备份恢复；原 Forget 回归与 500 根约束；备份真实创建、校验、幂等、权限变化、取消、双 Worker 失租与接管、失败重试/死信、不可用和摘要不符；恢复清除可信引用。确认所有 HTTP 元数据无路径、内容、摘要或下载入口。受影响检查后运行完整 make ci，记录候选哈希与原始证据，独立提交后才启动 W05。
