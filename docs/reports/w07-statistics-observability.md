# W07 统计与运行观测：本地实现交接

状态：In progress；独立副本本地实现及定向验证已通过，整合后的每包 `make ci`、Schema24 升级矩阵及公共契约候选审查由主整合任务执行，未标记 Completed。生产环境、预算、真实 Provider 和发布配置按最新用户授权全部待定，暂不实施生产验收。Phase 11/12 Deferred。

## 范围与候选

对应构建指导 W07、Console 设计 §8 / Phase13.6。副本起点 `b1c517e53f88cb2f3f7ce2eeb63977be7ee6cd17`，交接为未提交行为差异，未改主仓。独立副本 Core0.15.0 / Console0.1.0 / ConsoleContract1.2.0，以临时Schema23验证；根主线业务Contract1.11/ConsoleContract1.3，W07整合将ConsoleContract升至1.4。主整合编号固定为0024，排在 W08 Schema23 后。临时迁移不能作为主线0023发布。

实现八个统计面和服务端指标注册表，当前 Grant 与 scope fingerprint、按小时桶和日/周重算、缺失桶 null、coverage_from、computed_at、新鲜度、rollup lag、partial/warnings、固定对数耗时分位直方图及真实前端。当前查询执行 Scope/Purpose/Privacy/主体同意、资源引用与墓碑终检；不返回正文或隐藏余数。按隐私标签、来源授权、状态、Agent/Space/Session 可发现分组，不在前端另建指标清单。

Recall 的 nullable duration_us 与 observation JSON 来自同一个 RecallTrace 单调时间区间，即便响应不含 Trace 也可观测；不读 response_json 回算历史。旧记录保持 NULL，覆盖起点为迁移启用时刻。

即时 SQLite 子指标运行在 query_only 和默认250ms进度回调内；投影读取最多4001个原子、实时源最多1001行，超限/超时保留已完成子指标并明确不完整。目录统计另受250ms/4000项界限，跳过符号链接，不返回路径。文件字节严格标实例局部，不能解释为当前租户占用。

## 运行与事务

`console.stats.rollup` 使用现有 Scheduler/Outbox/Worker；小时调度可通过已有 SchedulerService 创建，固定24h滚动范围。显式 `POST /stats/rollups:backfill` 要求 stats.read + system.read + console.manage Purpose + reauth，最多31天完整UTC小时。Operation和租户构建Lease、每批200行、阶段游标、租约fence、COW发布与重复键替换保证中断续作/重放不累计重复值。取消或权限变更不发布未完成投影；恢复将原统计构建置为需审核，避免旧统计凭据复活。

Worker claim与真实heartbeat写入nullable低敏时间列；心跳年龄和队列/DLQ有限脱敏样本在当前可见Job集合内计算。登录失败与锁定拒绝来自审计，按当前可见密钥Grant求交；无法归属租户的未知密钥失败不包含在统计中。向量目录在Recall真实装配时注册，导出目录在服务Store装配时注册；未配置源明确不可用。

Provider相关面保持 null + projection_unavailable：当前副本没有可按资源授权包络隔离的持久Provider调用原子，既有租户聚合预算/熔断表不能直接透出给局部Grant。符合W07对未启用/缺失Provider数据的不可用表达；不作为真实Provider生产验收。

## 兼容与整合

见 [ADR-0051](../adr/0051-authorized-console-observability.md)。新增迁移重建既有Operation多态表，并保留Forget/Backup/Provider约束和外键；原0001–0022 SQL不改写。迁移声明offline、lock_ms=100、需可信备份；该锁时为声明上限而非生产测量结果。旧二进制Schema窗口拒绝新Schema；无破坏性down migration，回退使用隔离备份恢复。新增历史耗时/心跳列NULL不会被补成0。

主整合须按交付manifest按行为合并共享的Operation/Worker/Store/Recall/runtime接缝，迁移最终命名0024；仅合并source contract增量后统一生成schema/OpenAPI/类型，不覆盖真源。完成Schema23含W08数据升级到24和支持升级矩阵；报告尚未把临时副本旧Schema22→临时23结果替代该证据。公共API检查目前仅发现副本基线W05 CLI provider/secret配置漂移，未擅自接受快照。

独立真实统计浏览器移至 `web/console/tests/statistics`，专用配置启动真实后端；`test:browser`串联默认浏览器和统计配置，防止默认服务误跑特殊fixture且纳入每包CI。

## 实际验证与失败保留

环境：macOS arm64，Python3.12.13，Node26.8.1，显式副本PYTHONPATH和UV_NO_SYNC=1；共享venv只读，不运行sync。开发SQLite allowlist只用于本机测试。

- 统计HTTP/Projection/Operation/Migration/Recall五文件最初23 passed；补真实登录/目录/心跳后26 passed（8.73s）。
- 受影响Outbox、Console认证、Worker与上述统计合跑131 passed（25.05s）；之后新增真实Scheduler回归1 passed（0.49s），未改此前业务代码。
- Console check：确定性类型、ESLint、TS类型、29个单测、生产Vite构建均通过。页面只加载同源生产资产，无mock。
- 真实本机Chrome：注册表→分组/时间/Agent筛选→缺失桶→落后→reauth→真实Worker回填→completed→新鲜度恢复，1 passed（11.5s）。最初端口绑定因沙盒拒绝，经授权localhost执行后启动；第一轮真实浏览器暴露agent_id模糊标签严格模式冲突，修正为精确textbox后通过。没有将失败省略或把mock算真实证明。
- 全副本mypy：426 source files通过。受影响Python Ruff和格式通过；source contract重新生成检查通过。
- 共享Outbox旧allowlist测试首次104 passed/1 failed，差异为已实现统计job未列入预期；补入真实handler已验证的kind后上述131合跑通过。
- 公共接口检查首次因副本缺TS node_modules无法运行，复用只读依赖后暴露基线W05 CLI漂移；需主整合统一审查，未修改白名单。

复现定向命令：

```sh
UV_NO_SYNC=1 PYTHONPATH=src:sdk/python/src .venv/bin/python -m pytest tests/integration/runtime/test_outbox.py tests/integration/console/test_console_authentication.py tests/integration/console/test_console_worker_execution.py tests/integration/console/test_statistics_http.py tests/integration/console/test_statistics_operations.py tests/integration/console/test_statistics_projection.py tests/integration/migrations/test_statistics_migration.py tests/integration/recall/test_statistics_observations.py -q --no-cov
npm run check --prefix web/console
cd web/console
CI=1 CONSOLE_BROWSER_EXECUTABLE='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome' npx playwright test --config playwright.statistics.config.ts
```

主整合完成后须执行一次完整 `make ci`，保留80%行/分支联合覆盖率及原性能/安装门禁，记录真实候选和队列状态后才评定W07完成。本报告不宣称主线整合或生产验证已完成。

## 主目录整合记录

已按持久化/查询/后台回填/契约/前端分为4cbfd19、ceb4350、38aaf14、509826b、97a8d89五个行为提交。实际Core0.15.0、业务Contract1.11.0、ConsoleContract1.4.0、Schema24；原Schema1–23 SQL不变。公共接口增量审查证明旧HTTP操作、业务API、Python/TS SDK及CLI均不变，新增10个统计操作及6个Schema。

首轮131范围回归中的HTTP失败为Meta常量未同步；首轮224项迁移/恢复200过24失败，归因为同一Meta常量、旧Schema20夹具未去除新payload、Draft旧库升级未显式验证备份后允许离线迁移，以及合成Schema18快照保留统计bootstrap与新列。没有放宽门禁；修复后355项353通过、2项合成历史快照失败，再修复并定向4 passed。额外Schema23 populated Draft和删除账本→24、原Provider外键迁移2 passed。Schema1–23所有历史前缀进入24已验证。

integrated-structural-001失败为文档计数，002失败为前端生成类型过期；修复后003完整格式/lint/mypy/契约/前端29项/正式构建通过。此前未提交源未被错误当作完成候选。公共快照另已审查并检查通过。最终每包make ci仍待固定整合候选。

独立副本原始证据及明确标注的早期会话摘录归档于[独立证据](evidence/w07/independent-evidence.tar.xz)，主目录成功/失败原批次在[整合证据](evidence/w07/integration-batches.tar.xz)，SHA清单见[manifest](evidence/w07/archive-manifest.json)。不以临时目录作为永久附件。
