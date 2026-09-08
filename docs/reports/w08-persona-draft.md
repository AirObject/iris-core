# W08 Persona Draft 验证报告

状态：Ready for integration（尚未 Completed）。W08 的功能候选在独立副本 `/private/tmp/imc-w08-work` 实现；主目录整合、同候选完整 `make ci` 及版本/公共接口审查尚未完成，不能标记 Completed。Phase11/12 保持 Deferred。

## 范围与已实现行为

审查历史对象 `e639a2d` 后，按 [ADR-0052](../adr/0052-console-persona-drafts.md) 重新整合。旧 Schema21 草案未合入；新增 Schema23 独立草稿表，已有 Schema1–22 保持不变。草稿保存与替换具独立 Revision、显式 Persona/Policy 双基准；不消费人格发布序号、不更新 Current、不产生模型作业。

实际 Console CommandActor 校验 Agent 父范围、persona.publish、memory.read 与 console.manage。发布和丢弃要求五分钟内重新认证。发布使用存储内容，经现有 PersonaService 在一事务重验 Current/Policy CAS、当前 Evidence 后创建不可变人格，再关闭草稿；失败整体回滚。终态禁止再编辑或丢弃。

原型遗漏的 Hold 已补齐：按既有 space/session/subject Hold 匹配来源闭包，包括 PersonaState 的间接 Task 来源。有界查询、同事务重验，既阻止丢弃，也阻止用替换移除受保护来源。释放后原失败命令可正常重试。丢弃擦除正文/来源，在同事务写入全局 ForgetRequest、Tombstone、最小丢弃记录、审计和水位。旧快照回放同一可信删除账本，不建立平行恢复流程；已删除草稿 ID 不能复用。

七个真实 HTTP 操作覆盖有界列表、上下文、详情、创建、完整替换、发布、丢弃。严格请求拒绝未知权限字段、非法内容和 Revision。动作由实际授权和状态发现。正式 Persona 页面支持完整表单、双页面冲突、本地内容保留、重新认证、发布和不可逆丢弃；使用既有 ActionDialog，不增加自动覆盖。

## 已运行批次

全部批次通过 `tools.background_validation --attached` 保存任务标识、候选摘要/差异、日志和终态，存于独立副本 `.work-package-runs/W08/`。没有创建定时任务。

| 批次 | 实际结果 |
| --- | --- |
| prototype-adapted-001 | 环境失败：副本 uv 自动同步尝试下载 hatchling，受限网络失败；未运行测试。后续设 UV_NO_SYNC=1，明确 PYTHONPATH 指向副本，避免修改共用虚拟环境或误测主目录。 |
| prototype-adapted-002 | 收集失败：旧原型 fixture 导入路径已重排；适配为当前 tests/integration/console 后继续。 |
| prototype-adapted-003 | 22 passed；独立存储、原子发布/丢弃、幂等撤权、双基准、旧快照删除账本回放。 |
| http-contract-001 | 508 passed，1 failed；12 项新增真实 HTTP 全通过，失败仅 Console runtime_versions 仍是22，已统一为候选23。 |
| holds-001 | 1 passed，11 failed；新增已删除 ID 检查误用了旧表名，改为实际 resource_tombstones。 |
| holds-http-002 | 520 passed，1 failed；Hold 测试构造缺少既有 RetentionService forget 依赖，已修正。 |
| holds-003 | 3 passed；间接来源 Hold、保护替换/丢弃、释放后重试、当前 Evidence 到期与有界查询。 |
| console-check-001 | Passed；29 个前端单元、生成类型、ESLint、TypeScript、正式静态构建通过。 |
| types-001 | 失败后修复：Hold subjects 类型标注与测试所需 forget 依赖；structural-004 已复跑通过。 |
| browser-001 | 2 passed；真实 Chrome、实际 HTTP/SQLite、Required 模式、只读角色、创建、双页面编辑冲突、本地内容保留、reauth 发布、刷新、丢弃和404，独立8868端口/临时凭据。 |
| migration-concurrency-001 | 43 passed；Schema1–22 全部历史前缀→23、旧迁移 checksum 不变、真实备份前置、已删除 ID 不能复用、8份草稿并发发布仅一个 Current 赢家和跨Tenant拒绝。 |
| structural-002 | Ruff 的中文标点规则失败；已改用句号，未放宽规则。 |
| structural-003 | 副本最初按隔离要求未复制旧 evidence，文档链接检查失败；改为只读链接主目录 evidence 后通过。 |
| structural-004 | Passed；格式、Ruff、领域导入边界、文档及契约计数、426文件 mypy、SDK TS 类型、生成物一致性和契约兼容。 |
| affected-001 | 4511项：4443 passed、68 failed。68失败逐项定位为旧测试固定Schema22断言、副本缺少已发布迁移Git历史对象，以及草稿表缺少通用tenant/created/id分页索引。未屏蔽或放宽测试。 |
| repair-001 | 187 passed；增加实际idx_console_persona_drafts_created覆盖索引、旧断言推进到Schema23（未来版本负例改24）、副本只读引用主目录历史对象后，原失败所涉全部迁移、分页、Task删除/依赖和备份恢复范围复验通过。 |
| structural-final | Passed；修复后的格式、静态检查、导入边界、文档、类型和契约兼容再次通过。 |

## 整合交付与剩余验收

当前实现和已定位失败均已定向验证。交付位于 `/private/tmp/imc-w08-delivery/`：按生命周期、Console管理、契约增量、真实UI、版本断言拆分，单件16–59KiB；manifest记录基线和SHA。契约仅追加8个Schema、25个Fixture、7项操作，全部已有Schema/Fixture/Operation语义不变。生成物与公开接口白名单未放入补丁，主线程在合并W06/W07后统一生成和审查。

必须在主目录最终整合候选运行完整 `make ci`，然后更新队列和正式提交。独立副本未执行最终整合CI，本报告不会把定向回归或浏览器通过冒充该门禁。Phase11/12保持Deferred。

## 主目录整合检查点

2026-09-08 已分组整合生命周期、受保护发布/删除恢复、实际 Console HTTP、Schema23 升级窗口、Console1.3 契约与真实前端。新增七项操作，旧 SDK/业务 HTTP/CLI 保持，公共接口差异已逐项审查。integrated-structural-001 仅文档接口计数过期导致失败；更新至144路径/174操作后 integrated-structural-002 全部格式、lint、类型、契约与前端29项/构建通过。W06 随后集成后的结构/公共API复验同样通过。完整 CI 将在固定候选的独立工作树执行，结果未完成前不关闭 W08。

## 固定候选全量CI与修复

候选2983255的ci-w06-w08-001已实际结束：11项独立性能通过，功能15207 passed/6 failed，覆盖率85.85%。失败为认知调度fixture默认启用假设、四项历史Schema断言遗漏和一项Focus发布503；该轮未进入SDK/浏览器/安装阶段。监督进程未留下终态result.json，故用单独[观察记录](evidence/w08/ci-w06-w08-001-observation.json)说明，原输出归档于[CI证据](evidence/w08/ci-w06-w08-001.tar.xz)，不伪造监督结果。

认知fixture修复ef7c159定向2 passed。Schema24整合后相关77项回归76 passed（包含此前Focus失败的完整参数化用例），1项是新增两个nullable统计列使原SELECT星号元组长度变化；显式验证旧值及新增NULL后单项通过。没有修改Focus业务实现或放宽其时间预算，原受并发负载影响的503保留为本轮失败记录；最终完整CI仍需重新通过。[修复批次](evidence/w08/integration-repairs.tar.xz)保留全部失败和复验。W08尚未Completed。
