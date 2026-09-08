# ADR-0052：独立 Persona 草稿、发布和删除恢复

状态：Accepted，2026-09-08。设计决定接受与包验收分别记录。继续 ADR-0018、ADR-0025、ADR-0044–0047。

## 原型审查与生命周期

已核对保存对象 `e639a2d`：它是尚未验收的 22 文件原型，其 Schema21 已被可信备份占用；不能直接整包合入。当前增量使用 Schema23，保留 Schema1–22 SQL 不变。当前 ports 已拆分为按上下文的包，采用独立 persona_drafts 端口。

草稿独立于 persona_revisions，拥有稳定 ID、独立整数 Revision，以及 draft → published 或 discarded 两种不可逆终态。保存完整且已验证的 Core/Trait/Narrative、当前可见来源与明确审阅的 Persona/Policy 基准。创建和替换同时校验这两个基准，替换另校验草稿 Revision；不默默更新基准。草稿不进入 Recall Current、不消费发布序号、不产生模型或自动演进任务。Core 只接受人工获授权发布，模型 Proposal 仍不能修改 Core。

## 真实授权与原子发布

完整人格草稿要求实际 persona.publish、memory.read、console.manage 及 Agent 父范围写权限。发布和不可撤销丢弃另需五分钟内重新认证。使用共享 CommandActor/ConsoleCommandExecutor，不接受调用者指定身份、Capability 或绕过标志。读取和成功幂等回执均再次检查当前授权及来源依赖。

发布只使用存储中被审阅的草稿内容和来源，在一个事务内重验草稿 CAS、Persona Current/Policy 双 CAS 和当前 Evidence，通过既有 PersonaService 生成不可变 Revision、推进 Current/通知/水位，再把草稿标为 published。晚期失败回滚人格、草稿、审计与回执。Published 草稿与人格历史禁止更新或丢弃。

## Hold、擦除和恢复

丢弃只作用于未发布草稿；当前 Hold 按已有 space/session/subject 维度保护草稿引用的来源闭包，包含其必需的间接来源。检查有界且在同一写事务内；超预算失败关闭。由于替换会覆盖保存的内容和引用，已受 Hold 保护的草稿也不能替换以移除保护。Hold 不授予读取正文的权限。发布保留原草稿与不可变人格内容，不属于擦除。

丢弃清空正文与来源，保留最小 ID/Revision/状态/内容 Hash 元数据，并在同事务追加既有 ForgetRequest 删除账本、Tombstone、审计与水位。独立 discard 记录用于状态完整性，不能代替全局恢复输入。可信离线恢复回放最新删除账本，使旧快照及创建草稿之前的快照均不能复活正文；不存在草稿表的旧快照仍先写 Tombstone，随后升级只创建空表。已执行的删除事实优先于旧快照中的 Hold，恢复不重新撤销已承诺的删除。

## 契约、界面和验证

提供有界分页、创建上下文、详情、创建、完整替换、显式发布与丢弃路由。动作元数据由服务器按当前授权、基准及 Hold 发布；真实界面保存已审阅的版本，冲突保留本地内容并显示最新差异，不自动合并。原 22 项原型测试只是线索；实际 HTTP/浏览器、Hold/删除恢复、全部支持迁移路径与完整 CI 才能完成 W08。Phase11/12 保持 Deferred。
