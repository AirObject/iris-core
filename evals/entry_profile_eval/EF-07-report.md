# EF-07／EF-08：入口画像记忆接入（PR B）

本报告基于 `origin/main=782644018bf71e94cc0d13d89a6e5e66811513f3`（已含 PR A #112）。功能分支 `feat/entry-profile-memory-b`，独立 worktree；不合并 PR、不改前端、学习或 persona。产品实现 head `669f4c5`，评测装配 head `2873a87`。本轮真实 GLM 在 EP002 检查步骤超时，按 EF-07 约束整轮作废并停止。尚未开 PR；未双判、未声称门槛通过。最新测试修正 head 为 `c18009e`；最终报告提交号见回复。

## 接入和边界

- 迁移 **032_entry_profile_memory_tasks.sql** 新增每入口任务状态、游标／原始消息锚点、计划回执、投影脏标记及逐条旧画像迁移记录。SQL 只做结构；启动后确定性转换人工项，模型重建在入口任务中异步执行。
- 默认入口任务使用 entry_profile_memory_generate_v1／check_v1，生成 new／confirm／revise／retire，经 PR A 统一记忆写入口提交。读取快照结束后调用模型，写回同一短事务复核游标、设置／时区／入口类型、所有目标、消息与普通记忆修订和范围；原子提交操作、回执、游标、投影与成功任务状态。重复计划重放不重复增加强度。
- 自动 importance≤69、event_time=null、不置顶，沿用普通生命周期。retire 是普通遗忘，允许新的独立确认按现行规则恢复；不是删除。人工创建／人工正文编辑受保护，自动只可加真实来源或确认，冲突记在任务诊断。
- 增量消息按有界前缀取材，每页最多128条、新旧原始锚点64条、普通事实32条；消息／记忆预算6000／2500估算token、请求最多12000、操作最多12。没有固定证据年龄窗口。未扫到的消息不推进游标；未来时间消息不被永久跳过。保留锚点不足等取材损失单列。
- 原话需与绑定消息／当前普通记忆匹配；topic 要跨本地日独立讨论，排除重复、转引和接龙；saying 排除同一分钟及连续刷屏。正文性别代词、账号、来源硬范围确定性检查；隐式同一事件、隐私、释义逐词关系、稳定性由检查模型核对，不能把字面存在当语义有依据。删项不会放宽标准。
- 画像投影只在写事务末重算；显示集合变化时每入口 version 加一，读取、重复确认不改版本。生命周期、正文、依据、范围和时区变化均可使条目移出。known_memory_ids 与 text／memory_ids 同步过滤。手写项优先且不被自动展示预算隐藏；自动项按类别和ID稳定排序，6条／500估算token预算的省略ID记录在诊断。
- 普通 ranked memories 保持排除画像；宿主四个原字段类型不变，新增 memory_ids。彻底清除在同一事务清理来源相关正文、修订、旧历史、任务原始输出及投影；中间生成输出也接既有清除索引。模型调用完成后即保存进度，检查期间可读 stage=checking。

## 接口清单和 UX

完整路由、请求、响应和错误码见 [EF-07-interfaces.md](EF-07-interfaces.md)。核心变化：

- `POST /api/v1/entries/{id}/prepare` 的 entry_profile 投影兼容新增 memory_ids。
- `GET /admin/api/entries/{id}/profile` 返回 mode=memory、projection、memories、diagnostics、migration、cursor、rebuild_pending；current 兼容指向 projection，旧文档 history 标 legacy=true。
- `POST /admin/api/memories` 支持人工创建入口画像，显式 owner_entry_id，category 可空。逐条编辑、删除、遗忘、恢复、彻底清除使用既有通用记忆接口与 expected_revision。
- 旧整版 PUT／确认／拒绝／回滚返回410、entry_profile_legacy_write_retired；all_manual 返回410、entry_profile_publish_mode_retired。enabled 保留；设置和重新生成使用投影 expected_version。
- 新任务 version_status=null，不关联恰好同号的旧文档候选。生成输出持久化后 stage=checking，完成后 finished。

EF-08 要求的先推送已执行：`f83e987` 包含获批的整理运行器单处开关及接口清单；`bee3c73` 补来源／时区／版本状态边界；`669f4c5` 补中间进度保存。UX 可基于本分支并行开发，本线没有修改 frontend 或 web 构建产物。核对 PE 文件清单后，已用新提交撤出 test_purge_review.py 的本线改动；少数旧文档用例由 tests/conftest.py＋profile_legacy.py 选择历史 schema，最终差异不包含 PE 的该文件。

## 旧画像迁移

只转换当前已发布版本的人工条目，不复活已被后来版本撤去的历史规则。已知类别逐条转为人工记忆；未知类别原样保留（包括换行），category=null、投影标“管理员说明”。旧模型正文不导入记忆，cursor=0、rebuild_pending 等待异步从真实依据重建。all_manual 自动转 check_auto，并写操作记录；enabled 不变。历史仍只读，人工迁移记忆ID挂入历史清除索引。

真实031数据库升级夹具的逐条结果：

| 原条目 | disposition | 新记忆／类别 |
| --- | --- | --- |
| 模型主题 | model_rebuild_pending | 不导入 |
| 人工规则 | manual_imported | memory 1／rule |
| 未分类人工说明（含换行） | manual_unclassified | memory 2／null |

重复打开没有重复导入；all_manual 只记一次转换；清除人工规则同时清除对应旧历史，另一条未分类说明保留。此为确定性夹具验证，没有访问或转换用户生产数据库。详细材料位于仓库外 `EF-07-20261014/migration-smoke/summary.json`；实际升级报告由管理读取接口 migration 逐条返回。

## 提示词与正式评测登记

提示词沿用 PR A 文件，本次不修改已批准的 scoring_v3 或 memory_format_v2，也不修改冻结语料。

| 实际提示词 | SHA-256 |
| --- | --- |
| entry_profile_memory_generate_v1 | e3bbdd0f6fca36cfc4f5b307fab2dd33b1aa40ca8f0bc1e48101f4c759911a69 |
| entry_profile_memory_check_v1 | fce1f43c09ab44d8797fcf390ea9f5121e95d4818d800c289a255ac1ff49cfcf |

真实运行器使用 SQLite、统一写入口与真实投影，生成和自检原始输出保存在独立追踪及数据库中；双判材料递归白名单移除自检结论。假模型始终 gate_eligible=false。真实运行遇到第一次429或超时立即作废、停止，不重试，已用假HTTP429验证只有一次调用并保留材料。公开目标预登记：模型条目有依据≥90%、可用输出禁止内容0；覆盖和阶段损失作诊断。预登记要求由两位全新、未参与修改的 gpt-6-astra max 判分者互不可见地判分，分歧取不利结论。隐藏集没有寻找或读取。

正式运行：**1轮，已作废；不重跑、不双判**。连通检查 chat／embedding 成功，high，0超时／0个429。正式运行共4次调用：3次成功、1次超时、0个429；未发起后续四例的模型调用。gate_eligible=false。

| 案例 | 执行结论 | 模型调用 |
| --- | --- | --- |
| EP001 | 检查协议失败：六项检查结果均漏必填 violations；没有写入 | 生成44.311秒、检查99.187秒，HTTP均200 |
| EP002 | 第一页检查119.983秒超时；触发整轮作废 | 生成64.329秒成功，检查超时；未重试 |
| EP003—EP006 | not_run（earlier_timeout_or_429） | 0 |

双判结果、模型有依据率、禁止内容和各阶段覆盖均**未判分**，不能按空输出记作通过，也不把生成条目数当覆盖。原始输出、全部请求材料、失败诊断和未运行案例均保留在 formal-glm-run/；formal-invalidated-summary.json 绑定 run.json SHA-256。没有创建判分代理。EP001 的格式遗漏应在下一次获批迭代前单独处理，本轮没有更改提示词、协议或评分来接纳它。

## 回归对照

- 学习：使用 evals/compare_learning_requests.py 的仓库外既有全公开版（仅补第五份语料列表），116段／272批，系统与用户请求、顺序、目的和token上限零差异；两侧均1925条假模型记忆。固定生成时钟，不改正文或标签。不是学习质量评测。
- 召回：七份公开语料、243条记忆、228查询×5模式=1140组返回零差异；214个判断请求一致。仅移除随机 recall_id 和耗时；其余响应字段均比较。无画像数据时 prepare 没有额外变化；画像分区的有数据行为由宿主契约测试单列。
- 整理：EF-08 仅授权 run.py 装配增加 entry_profile_enabled=False。40例×两种既有假回答，共78个模型请求完全一致。记忆、来源、修订、整理操作、保留强度、检查和工作结果一致。原始导出有64项差异：main 会生成 kind=entry_profile、reason=insufficient_messages 的无写入空操作诊断，新装配不再生成；只排除这些逐项核实的非整理跳过标记后结果零差异。原始对照及完整排除列表保留；不声称原始导出字节完全相同。

## 测试与作废记录

已先红后绿覆盖间接来源日期重建、实际提示词元数据、同号旧候选隔离、时区改变导致依据失效、中间生成／检查状态等边界；任务、HTTP、迁移、假429、幂等／重启、冲突回滚、手写保护、清除、既有维护两次调用预算均有确定性或假模型测试。

- EF-07 初次全量3.13：3291 passed、40 failed、10 errors，均集中在整理运行器；触发范围暂停，EF-08授权后修正。相关回归123项通过。
- 早期 live-red 夹具漏填 kind、attempt-red 夹具 source 枚举填错，均不当功能红测。修正夹具后在main／当前实现真实复现失败的日志另存。
- 补任务进度持久化和评测案例时钟前分别中断两轮未完成3.13全量；不计最终验证，日志保留为 pytest-313-interrupted-progress.log／pytest-313-interrupted-runner-clock.log。没有真实模型参与。
- 评测案例时钟先红后绿，相关16项通过；完整6例公开假模型流程通过（不判质量，gate_eligible=false）。
- 后续完整3.13：3346 passed、1 failed（test_profile_quality_followup 的旧 v2 指纹用例仍使用当前 schema）。已将该单项改用真实031历史 schema，人物摘要部分保持当前 schema；相关81项通过。日志 pytest-313-legacy-test-failure.log／legacy-fixture-green.log。
- 最新3.13全量重跑与正式GLM并行，未重建环境。GLM超时后按“停下报告”约束中断；没有最终全绿结论。3.12与uv build尚未启动。日志 pytest-313-stopped-after-model-timeout.log；中断引发的pytest夹具清理异常保留，不当产品失败或成功。
- 已清理本 worktree 的 .pytest-tmp。仍待规划者安排恢复后的两个版本全量、构建、新的真实运行与独立双判，然后才能开 PR B。

全部完整材料在 `/Users/cassia/Local/Code/iris-eval-artifacts/EF-07-20261014/`；仓库仅本摘要与接口清单。不复制密钥或虚拟环境，不作CPU压测。全量测试结束后清理本 worktree 的 .pytest-tmp。

## 局限

确定性独立性是保守必要检查，不能识别所有改写、同一活动延续、普通方言或隐私；模型自检仍需独立外判。锚点和请求预算有限，长历史的遗漏有诊断；不保证一次任务扫完整库。投影重算目前对可能受影响的画像入口保守标脏，没有进行性能测量。管理员说明不自动猜类别；受保护手写内容的自动冲突处理仍留到以后阶段。本PR没有新增审批环节。
