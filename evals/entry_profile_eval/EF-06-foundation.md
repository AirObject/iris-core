# EF-05／EF-06：入口画像记忆底层（PR A）

本 PR 实现真实 SQLite 底层和读路径隔离，生产入口任务及宿主 `entry_profile` 仍沿用 v2。PR B 才接通任务适配器、持久游标、调度、旧画像转换和记忆投影。本轮没有调用真实模型，没有外判质量成绩；`gate_eligible=false`。

底层实现提交 `5efa5b2`，人物测试兼容提交 `c548e88`，历史范围读取修复 `ff940af`；最终 PR head 另含本报告。基线为 `origin/main@3f58952`。分支 `feat/entry-profile-memory-v2` 普通 merge 跟进主线，没有 rebase、force push 或修改 main。沿用 EF-03 的独立任务、提示词、模拟运行器及测试；评分／格式两份文件的批准提交为 `0e26051`，正文未再修改。运行器的批准状态同步为 `approved_by_planner_20261013`，模拟模式仍不能给出门槛通过结论。

完整日志、请求、返回和对照脚本存于仓库外 `iris-eval-artifacts/EF-05-20261013/`。此前 `scope-audit.md` 的 learning／goals 范围阻塞已由 EF-06 授权解除。修改文件前核对了 `git log origin/main -1 -- <文件>`；没有改 PE 的 persona.py 或其在途测试。

## 实现、规则和验证

| 底层项 | 实现与边界 | 测试覆盖 |
| --- | --- | --- |
| 入口主体 | 迁移 **031_entry_profile_memories.sql** 扩展 subjects.kind；entry_subjects 一入口一稳定主体、双向唯一。旧入口补建，新入口触发创建，改名保留 ID，禁止当人物合并／改类。删除空入口清理其主体，保留原行为 | 稳定唯一映射、改名／重启、030 升级、旧触发器／FK 保留、空入口删除 |
| 人物隔离 | 人物列表、详情、联系、别名、姓名锚点、人物摘要、学习主体快照限定 person／self；DB 同时禁止入口主体充当别名归属、平台身份、人物联系或消息作者 | 同名入口不制造人物歧义，人物列表／摘要／联系拒绝入口主体 |
| 硬范围 | memories.owner_entry_id 加永久 memory_owner_scopes，与真实来源范围相交。无入口上下文不可见，即使库内只有一个入口也不能退化为全局可见。来源边／合并时传播，解绑来源不能放宽；多个不同 owner 的交集为空 | 单入口无上下文不可见、跨入口不可见、派生、来源解绑、不同硬范围相交、合并／按旧内容新建继承 |
| 统一写入 | `write_entry_profile_memory` 提供 new／confirm／revise／retire；附表存入口、topic／saying／rule、manual_created。about 为入口主体，speaker=self，自动规则转述／主题说法推断，人工设定，world=real，event_time=null | 各字段、自动 importance 上限69、目标 CAS、来源修订／消息快照变化、删后不复活、事务回滚 |
| 来源与确认 | 来源绑定消息 ID、字段、连续原话摘录、消息快照 SHA；间接普通记忆还绑定修订号并验证来源可达。事务内重核入口、可见性、原话和修订。消息 ID 与作者＋正文哈希双回执，重复读取／引用同条正文不再加分；一次操作最多加一次，调用普通 confirm_retention 原子增加且不改修订 | 原话／引用重放、跨入口／过期来源拒绝、并发重复确认、按旧内容新建／合并继承回执 |
| 修订与人工保护 | 修正留 memory_revisions 并清向量；当前修订单列实际依据，历史依据继续供审计和清除。最近正文由人工编辑或人工创建者不能被自动改写／撤下，只能以真实新源确认 | 人工编辑后又调强度／重要性仍受保护、当前来源与旧来源分离、CAS 撤下、重建对象来源关联 |
| 普通材料排除 | prepare／search 默认候选、人物要点、学习检索、人物摘要排除画像 kind；向量截断／相对阈值前先排除，避免抢占名额。persona 复用无入口硬范围过滤，画像不会进入全局 persona；未改 persona.py。旧 v2 取材也排除新画像以免自证 | 全文／向量／空查询、prepare、学习材料、人物摘要、persona 选材排除；显式画像查询须带入口且只见本入口 |
| 目标与设置隔离 | EF-06 仅在目标依据建立、快照／详情／诊断材料排除画像。人工画像的 initial_setting 来源不能被角色背景替换，也不进入重建 persona 的初始依据 | 目标与普通记忆共用原话仍不选画像、遗留目标关联防御性过滤、修改角色名／背景不撤销人工画像 |
| 整理隔离 | 画像与普通 kind 不配对；不同入口／子类不配对；硬范围不同不合并。人工创建画像沿用既有人工保护。同分区的自动画像复用普通整理，DB 将回执与当前来源带到保留对象 | 跨 kind／入口／子类拒绝、同分区假模型合并、来源／回执／后续清除 |
| 清除 | 沿派生与 merged_into 闭包连带清除画像正文、所有修订、附表、回执及来源；复用现有索引清理旧投影版本和原始输出、拦截迟到输出。普通派生记忆的既有政策不变。单条／预览／批量回执增加 derived_profile_ids | 派生且人工编辑过的画像清除、历史投影／原始输出／迟到写回、合并链、跨批量分块的连带清除与重复请求 |
| 生命周期 | 自动画像 importance≤69，无自动置顶，进入普通衰减／遗忘／确认恢复；不改通用维护策略。人工创建按普通人工记忆规则 | 69仍衰减、达到遗忘阈值、真实新证据确认恢复且修订不变 |
| 导出导入 | 现有物理 SQLite 导出／导入保留新表、kind、入口主体、硬范围、修订来源与回执，无需另改备份格式 | 备份往返逐表比较与 FK 检查 |

迁移采用 SQLite 表重建：在事务外暂时关闭 FK，事务内重建并恢复 subjects 的既有索引／触发器，提交前 foreign_key_check，失败回滚并恢复 PRAGMA。升级前沿用现有备份机制。历史读取仅在迁移031尚未应用且确实缺少新表时解释为“无此类硬范围”；031之后缺表仍抛错，不能静默放宽。没有转换／删除旧 entry_profile_versions，也没有执行旧管理员群规迁移；这些属于 PR B。

写入口是生成／检查通过后的类型化数据层。独立讨论、同场活动、接龙／刷屏、性别代词、引文隐私、含义和主题稳定性仍由 EF-03 的确定性检查与模型检查配合；此处的哈希回执不能证明语义独立，也可能保守地拒绝同一作者日后逐字重复的真实确认。底层不会把多次读取同一消息当成新证据。语义近似改写的去重依赖生产任务／整理，精确同正文、同入口、同子类的新建可确定性转确认。没有给模拟器补造生产投影能力。

## EF-06 两处最小改动

- learning.py 仅 `_snapshot` 的 subjects、aliases、platform_identities 三个查询添加 person／self 过滤；姓名解析继续使用该快照。没有修改提示词、材料选取、检索参数或其他学习逻辑。
- goals.py 仅过滤画像依据／材料；缺失普通依据仍保留原有诊断行为。
- `ef06-red.log`：这两项测试先 **2 failed**；`ef06-green.log`：修复后 **2 passed**。后续用真实031 schema再验证。
- 入口基础4项、隔离4项分别先失败；批量连带清除先复现跨分块冲突再修复。角色背景更改误删人工画像先 **1 failed／1 passed**，修复后当时全部新底层测试 **26 passed**（3.24s）；随后增加031后缺表必须拒绝的测试，现共27项新底层测试。

## 对照与全量验证

最终对照使用同一个 main 基线、同一批公开语料、固定时钟与确定性假模型；没有读取模型配置。学习脚本沿用 `evals/compare_learning_requests.py`，仅在仓库外副本的 CORPORA 常量追加仓库脚本漏列的 learning_v5，以覆盖全部116段；原脚本没有改。副本差异及 SHA 在 comparison-driver-manifest.json。

| 检查 | 最终结果 |
| --- | --- |
| 公开学习 learning_v1—v5 | 116段、272批；逐字请求差异 **0**；156批有相关已有记忆，两边均产出1925条假模型记忆 |
| 七份公开召回，五模式 | 228查询×5＝1140组返回差异 **0**；214次判分请求完全一致 |

学习请求两边 SHA-256：`147c831b46c19fa6f83c4055ff8a61c5f4838165453976a73687c5f2f485c08c`。最终源码指纹：main `85bc54b8edbaddd7739b3112891d36fabe98d92d4ebd74821e659a313f924e69`，候选 `0115201582cddd9ade15b1e0ae622db6590b8e25bfb71a27db21b05cca2eeadf`。主线新增 #111 只改前端／文档，Python 对照源码指纹与上一基线相同。最终对照目录为 learning-comparison-complete、recall-comparison-complete；驱动与结果 SHA 另存 complete-comparison-manifest.json。

| 完整验证 | 结果 |
| --- | --- |
| Python 3.13.13：`uv run pytest` | **3317 passed**，4 warnings，959.57s |
| Python 3.12.12：`uv run --locked --isolated --python 3.12 pytest` | **3317 passed**，4 warnings，943.65s |
| `uv build` | wheel／sdist成功；wheel内031迁移、memory_ops.py和两份新提示词逐字一致 |

完整日志为 pytest-313-complete.log、pytest-312-complete.log、build.log；构建指纹见 build-manifest.json。验证跨2026-10-13／14进行，源码保持同一版本。两轮依次执行，均有4条依赖警告（jieba转义和Starlette httpx弃用）。最终已删除本工作区 `.pytest-tmp`；`.venv` 仍为3.13.13。

召回对照运行正式公开运行器的七份语料、243条固定记忆／228条查询，包括 no_answer_v1；五种模式 jieba_fts、trigram_fts、jieba_hybrid、trigram_hybrid、default_judged 全部比较。只有 providers 替换为离线确定性 embedding／judge；返回内容、顺序、分数、依据、提示及判分请求均比较。固定生成时钟／参与者 ID，只去掉 recall_id 与 judgment／hints 内耗时。该对照证明路径等价，不代表真实模型召回质量。

过程记录：没有真实模型运行，作废真实运行0。recall-comparison-1 未归一化 hints 内 duration_ms，出现511条仅耗时差异；补齐计时归一化后 recall-comparison-2 为0差异。learning-comparison-1 为0差异，二者为角色设置修复前的中间验证；final 目录为历史范围兼容修复前的中间验证，最终引用 complete 目录。首次3.13全量主动中断于332 passed（118.22s），为补角色设置回归并跟进 main；不计作全量通过。第二次全量中断于95 failed／2130 passed（605.00s）：旧人物测试把新增入口主体也计入了人物名称／数量，全部95处失败均来自这两个测试文件。只给7处测试查询加 person 限定，原断言数值、名称及产品代码不变；两个文件随后176 passed（26.58s）。其后完整3.13轮为8 failed／3308 passed（934.03s）：8项旧版本升级场景以当前读函数构造028数据库，新硬范围表尚不存在。没有改动这8项旧测试；补迁移版本限定的兼容读法，并测试031后缺表必须报错，81项画像／清除／升级回归通过（10.42s）。最终整库结果见上表，前两份中断及这份失败日志不算全量通过。bulk-cascade-green.log 使用了错误测试文件名未执行；两次中间批量测试的独立记忆夹具未绑定入口，修正夹具后45项相关测试通过；原始日志均保留，没有质量评测作废轮次。

## 文件范围

本轮新增迁移031、三个新测试文件及本汇总；既有人物测试 test_learning_fixes.py／test_learning_subject_refs.py 仅限定其人物统计查询的 kind；修改 db.py、memory_ops.py、retrieval.py、learning_retrieval.py、query_analysis.py、people.py、person_summary.py、admin_data.py、api.py、consolidation.py，以及 EF-06 批准的 learning.py、goals.py。entry_profile.py 仅给原文档取材加新kind排除；EF-03新增任务保持未接入。memory_runner.py 仅同步已批准的评分状态。maintenance.py、备份模块无需修改，已有实现经新增测试复用。前端／构建产物和文档只通过主线 merge 原样跟进，本 PR 相对 main 不含这些文件的修改。

## PR B 计划及待规划者确认的接口细节

PR A 合并后，再实现真实 MemoryProfileRepository：读取入口主体和范围快照；在短事务内复核源／目标修订、设置／游标、人工保护，原子写记忆操作及游标；模型调用始终在事务外。随后接调度、生命周期状态对应的投影和缓存失效，转换 legacy versions：模型正文退役并按新规范自动重建，管理员手写群规逐条转人工记忆。受保护手写群规第一阶段持续有效，迁移可用现有置顶规则保障，自动任务只能增源／确认，冲突只诊断，不增加人工批准环节。

需要规划者在 PR B 接口实现前确认的具体形状：

1. 宿主 v1 的 entry_profile 保持现有 text／version／generated_at／possibly_stale 字段与类型；兼容新增 `memory_ids: list[int]`。version 使用单调投影版本号，不能把单条记忆修订冒充整个投影版本。
2. 管理详情兼容新增 memory_id、memory_revision、category、owner_entry_id、manual_created 与投影版本。逐条编辑走现有记忆 CAS；旧整版编辑／回滚／确认／拒绝路由的过渡响应及弃用方式应与规划者、UX 一起确认，避免将旧请求静默变成多条写入。
3. 管理记忆 kind 筛选新增入口画像、入口视图展示关联记忆和冲突诊断；自动生成不要求管理员确认。前端由 UX 线实现，本线只给接口。

PR A 已有兼容接口新增：宿主 search.kinds 接受“入口画像”，此时必须传 entry_id；未指定该kind的普通查询维持排除。清除预览／回执增加 derived_profile_ids。无新不兼容的宿主字段类型变化。

真实写入口、硬范围、清除**及宿主投影**全部接通后才置 gate_eligible=true。PR B 再按已批准 scoring_v3 做公开 entry_profile_v1 的两名全新独立外判，从严聚合，再交规划者隐藏判定。本 PR 不宣称该质量门槛通过，也不合并。
