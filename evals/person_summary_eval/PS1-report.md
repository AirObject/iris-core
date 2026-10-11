# PS1：人物摘要后端第一阶段

实现基线：main `38c257cb42651ac2351df14d7e3fd4160580b373`（含 #82、#83、#84）。本轮仅假模型／假 HTTP 传输，不读取人物摘要 dev 集或隐藏集，不运行真实模型。不改整理、宿主接口、管理路由、学习、召回、记忆操作或前端文件。

## 数据与内容

迁移 `027_person_summary.sql`，接在当前 main 的 026 之后：

| 表／设置 | 主要字段与用途 |
| --- | --- |
| `person_summary_versions` | subject_id＋version 唯一；base_version、content、sentences_json（类别、正文、依据 ID／revision／指纹、作者、时间范围）、checks_json、change_degree、status（candidate/published/rejected）、author、source、generated_at/published_at、settings_revision/subject_revision、material_json、memory_state_json、invalidated_reason/cleared_at |
| `person_summary_settings` | 每人 enabled、publish_mode（check_auto/all_manual）、revision、current_version、needs_regeneration、updated_at |
| `person_summary_attempts` | subject_id、run_id、base_version、状态／原因／起止时间／版本、材料、原始输出、调用统计、清空时间；同人最多一个 running，同一整理 run 每人仅一次 |
| `person_summary_version_memories`／`person_summary_attempt_memories` | 只保存数字依赖 ID，关联最终条目、选入材料、旧版参考文字的依据和已知祖先；不以外键阻止记忆清除，也不在来源边被删后丢失关联 |
| `runtime_settings.person_summary` | enabled=true、publish_mode=check_auto、memory_tokens=3000、min_memories=8、min_changed=5、update_days=14、max_subjects_per_run=10、max_items=5、max_chars=200、max_tokens=250、生成／检查各 120 秒 |

类别为与我的关系（至多一条）、稳定事实、偏好与习惯、近期重要事项。模型每条绑定 1—3 条记忆；所有标签、时间范围和手写条目都计入 5 条／200 字／250 token 的硬上限。示例：

```text
稳定事实：正在学习木工。
近期重要事项：准备木工作品展示（截至2026年10月20日）。
```

这只是格式示例；真实内容及截止日期必须由该条绑定记忆支持。宿主投影不带记忆编号，管理详情保留逐条依据。

## 生成、核对和发布

1. 排除 self／scene、非 person、已被合并主体及关闭的人物。相关记忆通过 speaker／about 或唯一、边界匹配的姓名／别名确定；同名且没有明确 speaker/about 时不借用。
2. active、未彻底清除，且同时满足有效全局可见和声明 shared。递归检查直接消息来源、保留的范围根和派生祖先；不读取消息正文。单入口 entry_only 或 entries 覆盖当前全集仍排除。排序为重要度、保留强度、新近程度、ID；材料最多 3000 token，单条正文最多约 600 token 并标 truncated。
3. 有至少 8 条合格记忆才生成。首版；之后至少 5 条记忆新增／变化，或 14 天且有变化；已过时或主体合并标记可触发提前重生成。保留强度衰减本身不算变化。手动重生成跳过更新等待条件，但不跳过数量／开关／预算。
4. 生成在事务外，严格 JSON；M 编号只解析本次选材，拒绝未知、重复或超过 3 个引用、错误类别／结构／时间范围、多条关系。逐条检查只提供该条绑定的记忆，检查完整支持、单次泛化、敏感／保密、性别、评价、转述、他人事项、指令和到期。
5. 代码删条与检查删条共用分母：模型原始条目数（管理员条目不稀释分母），超过一半整版 rejected；恰好一半可保留剩余合格条目。空、超长或检查失败也保留上一版。默认检查通过即发布；all_manual 或手写条目违规警告保留候选，管理员内容不会被模型删掉。
6. 写回复核当前版本、主体／设置修订、运行设置、选入记忆及旧版参考依据的修订／内容指纹／生命周期／可见声明，检查在途清除。一个人在途仅一个生成。中断保存为失败，同整理 run 不自动重跑；编辑、回滚、开关和生成请求留操作记录。

读取不触发模型或写操作。当前依据变更、删除、遗忘、可见范围变化、主体变化标 possibly_stale；依据受限、被清空或事项到期时宿主整份不返回。声明受限的版本在原入口也不返回，保持摘要全局派生的边界；管理投影仍能看到过时原因。

主体合并通过新迁移中的触发器原子处理：源版本作废，源候选拒绝，源／目标在途生成 conflict；目标标 needs_regeneration。保留历史记录，不迁移源摘要文字到目标。

## CF 清除衔接

提供 `purge_memory_references(conn, memory_ids)`，必须在调用方的同一写事务中调用；它不自行提交。清空所有关联版本，不限当前／候选／被拒／历史状态：content、逐条正文／time_range、检查理由及检查中的原候选／材料、material_json；尝试的 material_json／outputs_json 整段清空，不做字符串替换。保留 ID、修订号、类别、作者、状态、时间和数值统计，检查标记“因彻底清除而清空”，当前版标需重生成。在途 attempt 转 conflict，后到响应不能回填。整理 gateway 还在 consolidation_calls 保存副本；因为该账本没有按人物的 attempt ID，清除受影响 run 下人物摘要两个 purpose 的全部 raw_output（其他整理用途不变），error 标固定清空原因，保留 ID／时间／调用统计。即使 attempt 已清空也再次核查此账本，覆盖后到响应。

关联范围故意包括选入而最终未被引用的材料、用于措辞比较的旧版依据、已知派生祖先：这些位置也可能留有被清除正文。独立管理员新写内容不猜测其来源；保持原样的模型条目仍带原依据。

**第二阶段接入位置**：CF 合并后，由记忆操作维护者在 `memory_ops.purge_memory` 现有 `_conn` 写事务内调用，传成功清除的 ID（批量操作可传列表）。函数可在清除前或后调用，依赖关系已有独立 ID 索引。调用方同时沿用 CF 对学习／整理公共原始输出的清空规则。本 PR 不改 memory_ops.py，不提前在服务中开启人物摘要生成。当前模块即使没接 CF，读取也隐藏已清除依据；在途失败路径还会发现已清除依赖并清掉自身尝试。

## 共用部分

`profile_common.py` 只抽取确定性 JSON 指纹、runtime setting 读取、空白规整、去除人工截断标记的摘录、拒绝重复键的 JSON 对象解析、共享 chat 每日额度门禁。entry_profile.py 原函数保留为委托／继承，提示词、门槛、渲染、发布和数据库行为不改。人物摘要的全局范围、记忆关联、内容策略和 purge 留在新模块，避免把人物与入口的证据规则混在一起。

## 给第二阶段的接口

| 接入点 | 已提供的函数／第二阶段工作 |
| --- | --- |
| prepare | `summary_context(conn, subject_id, entry_id, clock=...)` 只读返回 `{subject_id,name,text,version,generated_at,possibly_stale}` 或 None；调用者按显式参与者顺序／最近发言顺序取至多 3 人，独立 people_limit token 预算，0 禁用，search 不调用。每人最多 250 token；合计预算默认值由第二阶段接口规格确定。继续使用请求的授权入口 |
| 管理只读 | `current_summary(store, subject_id)`、`summary_settings`、`list_versions(limit,offset)`、`get_version`、`version_diff`；current_summary 指定 entry_id 时返回与 summary_context 相同的六字段宿主投影，不带管理材料快照。管理页面可展示类别、正文、依据、检查、删除记录、变化程度、候选／发布／清空／过时状态 |
| 管理写入 | `admin_edit`、`rollback`、`confirm_candidate`、`reject_candidate`、`set_settings`、`regenerate_summary(store,gateway,subject_id,expected_version=...)`；均需 expected_version。路由继续使用管理员会话／CSRF；Conflict/Busy 映射 409，其他校验映射适当 400/404；异步任务与前端后续接 |
| 整理 | `update_summaries_for_run(store,gateway,run,config,clock=...,stop=...)`；默认最多 10 人，最近消息时间（只读元数据）优先，无消息则用最近记忆确认时间。传现有整理的预算记账 gateway（与入口画像／persona 相同）；预检两次调用空间，每个 HTTP 尝试由 gateway 记入 consolidation_calls 并检查共享额度。维护报告 outcome=person_summary，返回 False 表示中断／另一个在途任务 |
| CO 后续改动 | 在整理中选定步骤调用上述函数。当前通用调用账本已能计入新 purpose；若希望重试生成时仍预留一次检查调用，可由 CO 将 `consolidation_person_summary_generate` 加入 `Consolidation.reserve` 的 required=2 分支（本阶段不改 consolidation.py） |
| CF 后续改动 | 按上一节在同一清除事务调用 purge 函数，启用生成前先完成衔接 |
| UX | 人物页支持短正文（无需 150 字下限）、分类／时间范围、逐条记忆依据、检查与删条记录、版本／对比／候选确认、编辑／回滚／重生成／开关。清空状态展示固定原因，禁止从清空旧版回滚复活正文 |

## 给 GD 的格式与评测

详见 [运行器与语料格式](README.md) 和 [判分说明](scoring.md)。顶层 format_version=1＋cases；每例声明 target subject、其他 subjects、entries、memories、as_of、must_cover、forbidden，可手写 leak_probes。语料记忆用字符串 ID，source_entries 和 derived_from 表示来源范围。入口限定内容不进入模型材料，must_cover／forbidden 标签只供导出和判分，记忆自身的 tags 仍进入材料。

运行器输出 PR #7 风格 manifest／run／cases／scoring／round-template，全量指纹绑定。严格校验外部判分的 item_results、must_cover、forbidden；双判正项 AND、禁项 OR、违规并集。独立泄漏检查覆盖实际材料 ID、最终依据 ID、所有尝试的解码后原始输出、最终渲染和精确探针；报告 probe 数，语义改写还需外部 forbidden／依据判断。覆盖率是诊断，不为分数放宽标注。

## 验证结果

按要求依次完成以下验证，两个 Python 版本没有并行运行：

| 命令 | 结果 |
| --- | --- |
| `uv run pytest` | Python 3.13.15；2490 passed，1047.20 秒 |
| `uv run --locked --isolated --python 3.12 pytest` | Python 3.12.14；2490 passed，716.78 秒 |
| `uv build` | wheel、sdist 均成功；新增模块／迁移／提示词及入口画像 v1／v2 提示词共 9 个文件逐字匹配源码；敏感配置未打包 |

两轮各有 4 条既有第三方警告（jieba 转义、Starlette TestClient 弃用），无失败。工作区虚拟环境仍为 Python 3.13.15。人物摘要新增测试共 53 项，包含假 HTTP Gateway、可见性声明、删条门槛、并发、事务外调用、人工保留、清除、合并、共享额度和运行器；入口画像原测试保持不改。运行器 10 项测试包括 Unicode 转义及带引号／换行的泄漏探针。

提交前再次 fetch 的 origin/main 为 `28495ed13fd48ef5046e9d9e89d17e8dcfc42edb`（后续 #85、#88、#90），最新迁移仍为 026，027 没有冲突。上述全量测试和下列对照均使用报告开头的固定实现基线 `38c257c`。

已完成的假模型对照：五份公开学习语料 116 案例／272 批次，请求逐字一致，SHA-256 同为 `9988666d3634cdde81ded0d1a540cf92cbda5d30b65bf8ae5a85301274861092`；七份召回语料 228 查询、全文／混合／判断后三路径 684 返回零差异，216 判断请求一致（仅排除 recall_id 与耗时）。入口画像五种流程的完整提示词／参数／版本／渲染／依据／检查结果与 main 相同。

极小手写临时样例完成 CLI 生成、材料导出及固定布尔值的外部计分格式演练，入口限定泄漏为 0，引用核对 1/1。`fake-format-only` 的布尔值不是语义评分；不据此宣称 dev 或隐藏门槛通过。

完整日志／请求／返回／极小手写样例位于仓库外 `/Users/cassia/Local/Code/iris-eval-artifacts/PS1-20261011/`。其中 `baseline/` 是本任务通过 git worktree add 建立的固定 main 对照 worktree，留给规划者清理；未删除任何 worktree 或分支。
