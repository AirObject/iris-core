# ADR-0049：Embedding 配置修订、受控出站与 Generation 切换

状态：Accepted，2026-09-08。范围为 [W05](../reports/w05-embedding-provider.md)，承接 ADR-0015、W01 接线和 ADR-0048；本决定先于新增配置表及迁移实施。

## 不可变配置与唯一服务代

配置内容以 tenant/config_id/revision 绑定不可变修订；草稿 PATCH 创建下一内容修订，保留旧值，废止旧探测和重建计划。生命周期及当前修订指针独立维护，状态遵守 Console 设计 §9 的 draft/probing/probed/activating/active/retired/discarded。每租户最多一个 active；认知配置不进入本包写面。

迁移采用新的 Schema22，不改写 0001–0021；Core 0.15.0 运行窗口推进至 Schema22。新增配置、探测、类型化 Provider Operation 明细和 Generation→配置修订关联。旧业务及 Forget/备份历史原样保留；版本与离线/备份门禁在迁移中声明，已有库以已验证备份升级，回退使用升级前备份和前版 Core。

配置接受与所有状态发布使用真实 Console CommandActor、当前权限/Grant/Session/recent reauth、CAS、幂等、Audit 和既有 Outbox/Lease；仅注册有真实处理器的 Provider 操作，不开放任意 kind 或模块。取消阻止后续发布，旧 active 和 Generation 保持服务。探测和向量构建均在写事务外执行；结果提交在同一 Outbox fence 事务内再次校验配置、权限、取消和当前代。

VectorSpaceConfig 的六项身份严格保持。空间变化创建新的 COW Generation；prepare_generation 不改当前指针。最终 switch_in_tx 与配置 active/retired、Generation 配置关联、Operation 完成在同一事务切换。旧代在此之前持续服务；首次启用无旧代时显式降级。API 在同一 Recall 读快照中选取当前 Generation 绑定的配置和 Provider，再执行查询与可信索引校验；缓存键含租户及配置修订，禁止共享可变全局查询模型。热限制修订可切换绑定，但不得混淆空间变化；新增内容与普通 vector.apply/rebuild 继续使用该租户当前配置。

回滚重新激活 retained 的历史配置：关联 Generation 仍存在且通过身份/内容/删除水位验证时可直接切换，否则重新构建；历史不删除。恢复使探测、在途 Operation 和旧外部构建意图失效，要求当前权限重新发起，不能复活旧授权。

## 两类密钥与部署边界

Provider 主密钥由 IRIS_MEMORY_SECRET_KEY_FILE 指定，独立于 Console 认证/刷新密钥。sealed 使用惰性导入 AES-256-GCM，AAD 绑定 tenant_id|config_id|revision，单次写入不回显；主密钥为 32 原始字节、仅属主可读写的普通文件。存在 sealed 修订而未提供可解密主密钥时启动失败。离线轮换对全部 sealed 修订重新封装，保留 AAD 和配置内容身份，不提供 Web 主密钥管理。

secret_ref 仅允许部署为该租户登记的精确 env:NAME 或 file:/abs/path 引用，不能借任意环境名或路径读取另一租户/Console 认证秘密。文件拒绝符号链接、非普通文件和宽于 0600 的权限，并有读取大小上限。部署允许引用集合不可由 Console 修改。读面仅提供设计规定的脱敏 hint/digest_prefix/resolved；响应、审计、日志和错误均不携带明文、密文或原始 Provider 响应。有效配置不代表当前秘密必然可解析，运行时失败必须明确降级。

## 受控真实传输与预算

有限 openai-compatible 适配器沿用 HttpEmbeddingProvider 的校验、限流和熔断，配置流程及实际 API/Worker 均使用同一受控传输。仅 HTTPS，开发回环须显式开关；部署可提供精确 hostname 和受限网络允许策略，Web 不能放宽。默认拒绝私网、链路本地/云元数据、回环、组播、保留地址及危险 IPv4 映射/过渡地址。每次调用只解析一次，检查所有解析地址，直接连接选定 IP；TLS 的 SNI/证书校验及 HTTP Host 仍为原始 hostname。不使用环境 HTTP 代理、不跟随重定向，不把 Authorization 转发到另一个地址。

解析、连接、TLS、响应头与响应体共同受单次总 deadline 约束；DNS 有固定并发上限，不创建无限队列。请求/响应字节、输入条数/字符、单次和租户预算均有上限。探测最多两个固定无业务文本；错误只返回稳定 outcome、实际维度与规范化说明，不返回向量。探测按租户计数和字符预算预留额度，失败也计成本；Provider 请求受现有限流/熔断。探测结果绑定完整配置修订，过期超过 30 分钟、配置变化、维度不符均不能激活。

## 可发现的真实流程与验收

按 Console 设计 §9.5 实现配置目录、草稿/历史、探测、激活计划确认、回滚、重建状态与取消；读写权限严格区分，side_effects 和重建计划摘要来自服务器。deterministic 仅显式开发开关可用，生产不得默认选择。配置页面不变成认知模型、任意请求模板或模型自动发现平台。

验收覆盖真实库迁移、配置/秘密隔离与负例、真实隔离 HTTP/TLS 传输和 DNS rebinding/私网/元数据/重定向/大小/超时拒绝、两个 Worker 和并发 API 读写时不混空间、旧代连续服务、取消/重试/恢复、真实浏览器闭环和安装装配。真实允许 Provider 的出站测试单独记录端点/模型/维度、授权及成本预算，缺少凭据保留该门禁；不以 deterministic 或隔离服务器替代。每包最终完整 make ci 和独立提交之后才启动 W06，Phase11/12 Deferred。

## 保留代回滚的发布约束

回滚复用仅在真实文件/FAISS 校验、原始 Generation 配置身份、全部 Canonical 资源身份/修订/内容摘要及 surrogate 集合一致时成立。准备和发布各验证一次；源或删除水位回退时拒绝复用。索引字节和内容摘要保持不可变，发布事务可将生命周期从 retired 恢复为 verified，并在已证明逻辑内容完全相同的条件下单调推进源/删除/Agent 水位，重建当前 id-map 的空间标记及 Generation 成员戳，再切指针。任何一项失败或历史文件/记录已清理，使用原配置重新构建，不删除配置历史。

retired 配置允许显式重新探测，成功或失败都保留 retired 身份；取消和失败亦回到原历史状态。回滚仍要求成功且未过期的真实探测与 recent reauth，不通过刷新元数据时间绕过 30 分钟门禁。回滚命令已明确授权在历史代不可复用时重建；公开计划应区分可复用历史代、热绑定与新建代，Worker 在实际执行时复验复用条件。

## 部署配置入口

采用独立受限 JSON 文件，由 `provider_config_file` / `IRIS_MEMORY_PROVIDER_CONFIG_FILE` / `--provider-config-file` 指定，固定 schema_version=1，仅包含 tenant secret_references 和 outbound 策略；主密钥仍通过独立字段提供。文件只在启动读取，API/Console 与所有 Worker 必须一致发布；秘密材料按引用独立解析。未知字段、重复键、越界/错误类型及宽权限/链接文件均在监听前拒绝。回环出站须同时声明部署 allow_loopback 与服务 development_embedding，不能因此自动选择 deterministic。参见[部署说明](../development/embedding-deployment.md)。
