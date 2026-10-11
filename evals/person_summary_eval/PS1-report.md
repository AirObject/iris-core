# PS1：人物摘要后端第一阶段

最终验证基线：main `db640af7c40736cdb23489348626e0c852808d36`（含 #87），已通过普通 merge 跟进（合并提交 `13d26a2`、`e315c1a`），保留已推送历史。本轮仅假模型／假 HTTP 传输，不读取人物摘要 dev 集或隐藏集，不运行真实模型。不改整理、宿主接口、管理路由、学习、召回、记忆操作或前端文件。

## 数据与内容

迁移 `028_person_summary.sql`，接在 main 的 `027_leading_think_diagnostics.sql` 之后：

| 表／设置 | 主要字段与用途 |
| --- | --- |
| `person_summary_versions` | subject_id＋version 唯一；base_version、content、sentences_json（类别、正文、依据 ID／revision／指纹、作者、时间范围）、checks_json、change_degree、status（candidate/published/rejected）、author、source、generated_at/published_at、settings_revision/subject_revision、material_json、memory_state_json、invalidated_reason/cleared_at |
| `person_summary_settings` | 每人 enabled、publish_mode（check_auto/all_manual）、revision、current_version、needs_regeneration、updated_at |
| `person_summary_attempts` | subject_id、run_id、base_version、状态／原因／起止时间／版本、材料、原始输出、调用统计、memory_state_json（尝试时相关事实指纹）、清空时间；subject_id/id 与 run_id 索引；同人最多一个 running，同一整理 run 每人仅一次 |
| `person_summary_version_memories`／`person_summary_attempt_memories` | 只保存数字依赖 ID，关联最终条目、选入材料、旧版参考文字的依据和已知祖先；不以外键阻止记忆清除，也不在来源边被删后丢失关联 |
| `runtime_settings.person_summary` | enabled=true、publish_mode=check_auto、memory_tokens=3000、min_memories=8、min_changed=5、update_days=14、max_subjects_per_run=10、max_items=5、max_chars=200、max_tokens=250、生成／检查各 120 秒 |

类别为与我的关系（至多一条）、稳定事实、偏好与习惯、近期重要事项。模型每条绑定 1—3 条记忆；所有标签、时间范围和手写条目都计入 5 条／200 字／250 token 的硬上限。示例：

```text
稳定事实：正在学习木工。
近期重要事项：准备木工作品展示（截至2026年10月20日）。
```

这只是格式示例；真实内容及截止日期必须由该条绑定记忆支持。宿主投影不带记忆编号，管理详情保留逐条依据。

## 生成、核对和发布

1. 排除 self／scene、非 person、已被合并主体及关闭的人物。相关记忆通过 speaker／about 或唯一、边界匹配的姓名／别名确定；同名且没有明确 speaker/about 时不借用。人物摘要专用匹配器仅加载主体名、source_message_id=NULL 的手工／合并别名及来源入口声明 shared 的别名；选材、材料别名、过时与写回复核共用该口径，不调用全局未过滤的 SubjectNames 构造函数。
2. active、未彻底清除，且同时满足有效全局可见和声明 shared。递归检查直接消息来源、保留的范围根和派生祖先；不读取消息正文。单入口 entry_only 或 entries 覆盖当前全集仍排除。先以 speaker_subject_id 索引、memory_subjects_by_subject 及过滤后名字／别名的全文查询取候选，再做最长名字与正文边界核对。三个字符及以上用 trigram 短语，短名用 jieba；始终保留纯姓名关联的候选通路。来源检查只沿候选 ID 向祖先递归，不计算整库闭包，不读 embedding。排序为重要度、保留强度、新近程度、ID；材料最多 3000 token，单条正文最多约 600 token 并标 truncated；累加 JSON 字符分类数精确计算现有 token 估算，预算满即停止摘录。
3. 有至少 8 条合格记忆才生成。首版；之后至少 5 条记忆新增／变化，或 14 天且有变化；已过时或主体合并标记可触发提前重生成。确认与保留强度衰减不算变化；指纹只含修订、正文、主体、立场、相信程度、事件时间、标签和 world。手动重生成跳过更新等待条件，但不跳过数量／开关／预算。失败或被拒（含管理员拒绝）后，相关事实指纹未变化则不再尝试，手动请求同样遵守；普通 not_due 等跳过不会覆盖上次失败快照。
4. 生成在事务外，严格 JSON；近期事项没有 valid_until 时，取该条依据的最新事件时间（缺省或不可解析则用创建时间）加 30 天作为 expires_at，到时逐条失效；M 编号只解析本次选材，拒绝未知、重复或超过 3 个引用、错误类别／结构／时间范围、多条关系。逐条检查只提供该条绑定的记忆，检查完整支持、单次泛化、敏感／保密、性别、评价、转述、他人事项、指令和到期。
5. 代码删条与检查删条共用分母：模型原始条目数（管理员条目不稀释分母），超过一半整版 rejected；恰好一半可保留剩余合格条目。空、超长或检查失败也保留上一版。默认检查通过即发布；all_manual 或手写条目违规警告保留候选，管理员内容不会被模型删掉。
6. 写回复核当前版本、主体／设置修订、运行设置、选入记忆及旧版参考依据的修订／内容指纹／生命周期／可见声明，检查在途清除。一个人在途仅一个生成。due 和 admin_edit 的选材均在读快照进行；写事务只做有界依据／版本核对、接纳人数 CAS 和记录写入。编辑保留的旧条目同时复核当前依据及版本清空标记，阻止读快照后彻底清除的正文被重新写回。中断保存为失败，同整理 run 不自动重跑；编辑、回滚、开关和生成请求留操作记录。

读取不触发模型或写操作。依据删除、遗忘或彻底清除时，宿主先移除含这些依据的条目；到期近期事项同样逐条移除，渲染剩余条目，空则不返回，同时标 possibly_stale 并满足提前重生成条件。其他依据变化或主体变化也标过时。来源或身份关联因可见声明变化而失效时整份隐藏，在原入口也不返回。宿主读取只递归所引依据的祖先与来源，不构建整库闭包；管理详情保留过时原因。CF 已整段清空的版本没有可返回的剩余正文。

主体合并通过新迁移中的触发器原子处理：源版本作废，源候选拒绝，源／目标在途生成 conflict；目标标 needs_regeneration；updated_at 在 merged_at 缺省时用当前时间兜底。保留历史记录，不迁移源摘要文字到目标。

## CF 清除衔接

提供 `purge_memory_references(conn, memory_ids)`，必须在调用方的同一写事务中调用；它不自行提交。清空所有关联版本，不限当前／候选／被拒／历史状态：content、逐条正文／time_range、检查理由及检查中的原候选／材料、material_json；尝试的 material_json／outputs_json 整段清空，不做字符串替换。保留 ID、修订号、类别、作者、状态、时间和数值统计，检查标记“因彻底清除而清空”，当前版标需重生成。在途 attempt 转 conflict，后到响应不能回填。整理 gateway 还在 consolidation_calls 保存副本；因为该账本没有按人物的 attempt ID，清除受影响 run 下人物摘要两个 purpose 的全部 raw_output（其他整理用途不变），error 标固定清空原因，保留 ID／时间／调用统计。即使 attempt 已清空也再次核查此账本，覆盖后到响应。

关联范围故意包括选入而最终未被引用的材料、用于措辞比较的旧版依据、已知派生祖先：这些位置也可能留有被清除正文。独立管理员新写内容不猜测其来源；保持原样的模型条目仍带原依据。

**第二阶段接入位置**：CF 合并后，由记忆操作维护者在 `memory_ops.purge_memory` 现有 `_conn` 写事务内调用，传成功清除的 ID（批量操作可传列表）。函数可在清除前或后调用，依赖关系已有独立 ID 索引。调用方同时沿用 CF 对学习／整理公共原始输出的清空规则。本 PR 不改 memory_ops.py，不提前在服务中开启人物摘要生成。当前模块即使没接 CF，读取也隐藏已清除依据；在途失败路径还会发现已清除依赖并清掉自身尝试。

## 共用部分

`profile_common.py` 只抽取确定性 JSON 指纹、runtime setting 读取、空白规整、去除人工截断标记的摘录、拒绝重复键的 JSON 对象解析、共享 chat 每日额度门禁。entry_profile.py 原函数保留为委托／继承，提示词、门槛、渲染、发布和数据库行为不改。人物摘要的全局范围、记忆关联、内容策略和 purge 留在新模块，避免把人物与入口的证据规则混在一起。

## 给第二阶段的接口

| 接入点 | 已提供的函数／第二阶段工作 |
| --- | --- |
| prepare | `summary_context(conn, subject_id, entry_id, clock=...)` 只读返回 `{subject_id,name,text,version,generated_at,possibly_stale}` 或 None；调用者按显式参与者顺序／最近发言顺序取至多 3 人，独立 people_limit token 预算，0 禁用，search 不调用。每人最多 250 token；合计预算默认值由第二阶段接口规格确定。继续使用请求的授权入口。第二阶段考虑让 people[].name 使用宿主传入的参与者名；本阶段仍返回主体当前名字 |
| 管理只读 | `current_summary(store, subject_id)`、`summary_settings`、`list_versions(limit,offset)`、`get_version`、`version_diff`；current_summary 指定 entry_id 时返回与 summary_context 相同的六字段宿主投影，不带管理材料快照。管理页面可展示类别、正文、依据、检查、删除记录、变化程度、候选／发布／清空／过时状态 |
| 管理写入 | `admin_edit`、`rollback`、`confirm_candidate`、`reject_candidate`、`set_settings`、`regenerate_summary(store,gateway,subject_id,expected_version=...)`；均需 expected_version。路由继续使用管理员会话／CSRF；Conflict/Busy 映射 409，其他校验映射适当 400/404；异步任务与前端后续接 |
| 整理 | `update_summaries_for_run(store,gateway,run,config,clock=...,stop=...)`；默认最多 10 人，最近消息时间（只读元数据）优先，无消息则用最近记忆确认时间；过时只提前满足条件，不改变活跃排序。先按索引预筛不足 8 条的主体，再在读快照计算 due；接纳满 N 人立即停止计算后续 due。已接纳人物的报告修复单独执行。传现有整理的预算记账 gateway（与入口画像／persona 相同）；预检两次调用空间，每个 HTTP 尝试由 gateway 记入 consolidation_calls 并检查共享额度。维护报告 outcome=person_summary，返回 False 表示中断／另一个在途任务 |
| CO 后续改动 | 在整理中选定步骤调用上述函数。当前通用调用账本已能计入新 purpose；若希望重试生成时仍预留一次检查调用，可由 CO 将 `consolidation_person_summary_generate` 加入 `Consolidation.reserve` 的 required=2 分支（本阶段不改 consolidation.py） |
| CF 后续改动 | 按上一节在同一清除事务调用 purge 函数，启用生成前先完成衔接 |
| UX | 人物页支持短正文（无需 150 字下限）、分类／时间范围、逐条记忆依据、检查与删条记录、版本／对比／候选确认、编辑／回滚／重生成／开关。清空状态展示固定原因，禁止从清空旧版回滚复活正文 |

## 给 GD 的格式与评测

详见 [运行器与语料格式](README.md) 和 [判分说明](scoring.md)。顶层 format_version=1＋cases；每例声明 target subject、其他 subjects、entries、memories、as_of、must_cover、forbidden，可手写 leak_probes。语料记忆用字符串 ID，source_entries 和 derived_from 表示来源范围。入口限定内容不进入模型材料，must_cover／forbidden 标签只供导出和判分，记忆自身的 tags 仍进入材料。aliases 字符串数组保持原样，新增可选 alias_source_entries 映射（别名 → 入口 ID 或 null），未填或 null 视为 shared；旧格式不用修改。

运行器输出 PR #7 风格 manifest／run／cases／scoring／round-template，全量指纹绑定。严格校验外部判分的 item_results、must_cover、forbidden；双判正项 AND、禁项 OR、违规并集。独立泄漏检查覆盖实际材料 ID、最终依据 ID、所有尝试的解码后原始输出、最终渲染和精确探针；报告 probe 数，语义改写还需外部 forbidden／依据判断。独立检查也覆盖仅通过受限别名关联的全局记忆和材料中的受限别名。检查调用 ModelError 或 unconfigured／paused／usage_limit／temporary_unavailable 等跳过均算未完成；只有实际输出经引用、长度、语义或 invalid_output 判定才算完成。覆盖率是诊断，不为分数放宽标注。

## 验证结果

定向测试 86 passed（原有 53 项，本轮新增 33 项），先用回归探针确认失败，再修复。源代码在 `e315c1a` 合并最新 main 后固定；下面测试与对照均使用这份代码，两个 Python 版本按顺序执行。

| 验证 | 结果 |
| --- | --- |
| `uv run pytest` | Python 3.13.15：2674 passed，4 warnings，615.98 秒 |
| `uv run --locked --isolated --python 3.12 pytest` | Python 3.12.14：2674 passed，4 warnings，647.54 秒 |
| `uv build` 与包内容检查 | wheel／sdist 均成功；9 个模块／迁移／提示词逐字匹配源码；028 在包内，旧 027_person_summary 不在包内，敏感配置未打包 |

两轮各四条警告，均来自既有 jieba 转义和 Starlette TestClient 弃用。工作区虚拟环境仍为 Python 3.13.15。入口画像原测试未改；新回归覆盖受限别名选材／返还／在途复核，确认不变更事实指纹，失效依据逐条剔除，近期事项默认到期，失败／管理员拒绝退避，读写事务边界，索引预筛和接纳上限，以及编辑快照后清除不能回填。

对照固定 main `db640af`，全部假模型：

- 五份公开学习语料 116 案例／272 批次，请求逐字一致；SHA-256 同为 `9988666d3634cdde81ded0d1a540cf92cbda5d30b65bf8ae5a85301274861092`。
- 七份召回语料 228 查询，全文／混合／判断后三路径的 684 个返回零差异，216 个判断请求一致；仅排除 recall_id 和耗时字段。
- 入口画像五种流程的完整提示词／参数／版本／渲染／依据／检查结果完全一致。
- 旧格式的极小手写临时样例完成最终 CLI 生成、PR #7 材料导出和固定布尔值计分演练；未完成案例 0、入口限定泄漏 0、引用核对 1/1。`fake-format-only` 不是语义评分，不据此宣称 dev 或隐藏集门槛通过。

## 一次性规模计时

计时设备为 macOS arm64、Apple M4、16 GiB 内存，使用工作区 Python 3.13.15。仓库外 `measure_completed_once.py` 建立 50,000 条合成记忆、1,000 个主体，每条附 8192 字节未使用的向量数据，并包含 speaker／about、受限 roots 和派生来源。建库不计入耗时；测量一次 `update_summaries_for_run`，接纳并发布默认的 10 人，生成与检查共 20 个确定性假响应，没有真实模型调用。其他测试和构建结束后单独测量，开始前检查没有其他计算重负载，不做压测或重复取样。

| 单次完整计时 | 结果 |
| --- | --- |
| update_summaries_for_run 总耗时（含假响应） | 730.451 ms |
| 假响应耗时 | 0.581 ms |
| 非模型耗时（总耗时减假响应） | 729.870 ms |
| 写事务次数 | 90 |
| 单个写事务最长时长 | 4.205 ms，低于 100 ms 目标 |

写事务计时包含锁获取、BEGIN、事务体、COMMIT 及 Store 提交后 bookkeeping，作为保守上界；假模型也经过正式整理 gateway 的调用预算和账本路径。计时明细在仓库外 `measurement.json`，脚本 SHA-256 为 `7613ddc192456d88180dd5b91fddaa065e943343f08609fd1e90ca59bb254dc2`。

首次启动因外部脚本 Fake.chat 未接收位置参数 max_tokens，在第一个假响应前中断，没有完整计时结果。按规划者明确答复，该次不算有效测量；修正适配器后仅补做了上述一次完整计时，未改产品代码。失败脚本、合成库及日志与完整测量的独立数据库和日志均保留。

完整日志、请求／返回对照和合成规模数据位于仓库外 `/Users/cassia/Local/Code/iris-eval-artifacts/PS1-review-20261012/`。最终对照使用 baseline-db640af/；baseline/ 保留上一固定 main 的中间结果。两者均为本任务通过 git worktree add 新建，留给规划者清理；本轮未读取 person_summary_v1 dev 集或隐藏集。
