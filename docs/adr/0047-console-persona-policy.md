# ADR-0047：Console Persona Policy 管理

状态：Accepted，2026-09-07。继续 [ADR-0018](0018-phase9-persona.md)、[ADR-0025](0025-console-command-authorization.md)、[ADR-0044](0044-console-persona-publication.md) 和 [ADR-0046](0046-console-persona-proposals.md)。决定接受与候选验收分别记账。

## 权限与数据边界

以独立 `/personas/{agent_id}/policy` 读取和完整替换当前演进策略。读取要求实际 memory.read、console.manage 和 Agent 可见；策略只包含字段名称、模式及数值限制，不复制 Persona 内容或来源。写入要求实际 persona.publish、覆盖 Agent 父范围的 Grant 和五分钟内重新认证。策略可以影响后续后台自动演进，因此包括 locked 在内的所有替换均为敏感操作，要求原因、会话 CSRF 和幂等键。

命令使用实际 CommandActor，不接受客户端身份、宿主 Capability 或绕过权限标志。缓存回执只包含 Policy ID、Agent、Revision 和内容 Hash；稳定 Agent 为幂等目标和返回后权限重验对象，不伪造 Policy Resource 集合。

## 独立版本与事务

Policy Revision 独立于 Persona/State。完整替换须提交已审阅的 expected_revision，复用既有配置校验、current Policy CAS、不可变配置行和 superseded 生命周期。同一事务保存新策略、切换当前策略、审计、实际 Policy 水位及成功回执；晚期失败回滚全部副作用。

替换不发布 Persona，不改写当前人格的历史 policy_id、内容或指针。创建和批准 Proposal 继续读取当时的当前策略，已打开的旧 Policy 审阅表单在策略替换后失败，操作者须重新审阅。宿主和 Reflection 的既有行为不变；本接口不增加自动发布捷径。

## 契约和页面

GET 返回独立 Policy DTO 和实际可用动作，PUT 接受严格的完整配置。十三项配置均为必填：模式、允许字段、敏感字段、四个比例阈值及六个整数限制。字段路径限制于既有 Trait/Narrative 字段；locked 策略的 allowed_fields 为空。

六个整数使用非负规范十进制字符串传输，最大为 SQLite 有符号 64 位整数；服务端验证后转为领域整数。这确保浏览器 JSON、表单、回执后的重新读取不会舍入超过安全整数范围的时长或证据计数。比例仍为 0–1 数值，持久化和内容 Hash 使用领域规范配置。

页面展示全部配置并提供独立字段表单。打开表单固定已审阅的 Policy 版本，替换要求敏感确认和真实重新认证。版本冲突读取最新 Policy，展示差异并保留本地草稿，禁止自动覆盖提交；重新打开表单后再审阅。ActionDialog 的可选最新值读取回调保留其他资源的既有行为。

复用 Schema20，新增两项 Console 操作和六个 DTO，不改变业务 SDK 方法或版本。契约真源、Fixture、生成物和公共清单同步维护。

## 验证及剩余边界

专项验证覆盖实际父范围权限、近期认证、缓存命中后撤权、独立版本 CAS、陈旧 Proposal Policy 围栏和晚期失败回滚。实际备份恢复检查当前与历史 Policy、精确大整数、Persona 指针及其原始策略引用。真实浏览器验证 Required 模式、最小权限、两页面冲突与草稿保留、重新认证、大整数刷新以及只读权限。

整合后必须在同一主目录候选执行完整 CI、真实浏览器与独立安装。Draft 生命周期、保留管理、生产隔离、时钟回拨、Soak 和完整灾备仍属后续工作。本决定不构成完整 Phase14 验收，证据见 [验证报告](../reports/phase-14-verification.md)。
