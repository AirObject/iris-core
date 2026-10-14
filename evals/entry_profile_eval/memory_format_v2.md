# EF-03 主题化评分与运行器输出格式（已批准）

状态：approved_by_planner_20261013；规划者批准日期 2026-10-13。新口径与输出契约已批准，后续实现不得把模拟阶段的机制测试当成门槛成绩。当前生产入口画像 v2、人物摘要均不变。

## 与 scoring_v2 的逐项差异

| v2 项 | v3（已批准） | 理由 |
| --- | --- | --- |
| 文档 sentences，index 定位 | 普通画像记忆列表，以 ID＋revision＋index 定位 | 每条画像是一条可修订的记忆；避免错评旧修订 |
| 具体讨论对象和正文原话 | 有依据的稳定主题正文；原话移到 sources | 用户要求主题化，不再把具体事件流水账作为画像 |
| 原始消息／可见记忆依据、作者归属 | 保持，并显式禁止画像自证与无新证据确认 | 更换表示不能削弱来源要求或放大保留强度 |
| 话题两次跨日独立讨论 | 保持必要下限，无固定证据年龄上限 | 跨日不等于独立事件；长期低流量入口仍可被确认 |
| 群内说法的用法／含义 | 显式要求独立使用；逐词及关系核对含义 | 对齐失败类型；无解释仍可只记说法 |
| 长期群规、手写例外 | 保持依据与禁止要求；手写冲突仅诊断，第一阶段始终保留 | 按 EF-03 决定，不新增管理员审批、不让模型撤下手写规则 |
| 八类禁止 | 原八类原义保留，新增“容易过时的描述” | 具体事件／日期／一次安排／个人经历不适合中长期画像 |
| 引文也检查隐私 | 检查最终保留摘录；完整上下文仅作核对 | 不能把别人的私事作为对外依据，也不能因审计上下文存在私事误罚 |
| 人工条目单列，模型分母 | 保持；分母改为最终可用模型记忆 | 不能靠展示预算藏起不合格的已写入记忆 |
| v2 适用性排除表 | 全量原标签保留；覆盖只作诊断 | 不能为了主题化结果默默改冻结标签、缩小分母 |
| 拒绝候选单列 | 分阶段导出候选和覆盖，候选不入最终分母 | 准确诊断确定性／模型删条／投影预算分别丢失哪些要点 |
| profile.checks 可混入材料 | 自检结论只在独立 trace，判分输入不含它 | 避免产品自评影响独立外判 |
| 双判从严、90%／0 | 保持，空分母不通过；底层未接通阶段不输出门槛通过结论 | 目标未放宽；实施和批准解耦 |

这里不新增／改写质量语料。现有 entry_profile_v1 cases／groups 输入解析沿用：messages、memories、initial_profile、as_of、timezone 等不变。initial_profile 原样种为受保护人工记忆，不让模型重写；普通 memories 是事实源，不能被直接当成画像成品。

## 输出契约（format_version=2）

新路径独立于现有 format_version=1 文档运行器。evaluation=`entry_profile_memory`，scoring_version=`entry_profile_scoring_v3`，scoring_status=`approved_by_planner_20261013`，gate_eligible=false。第一阶段 repository_mode=`simulation`；真实写入口尚未授权实现。

外部目录结构：run.json（完整追踪，含生成／自检输出及操作诊断）、judging-materials/manifest.json、round-template.json、scoring.md、cases/*.json。判分者只获得 judging-materials/。所有 JSON 为 UTF-8。文件散列、源码／语料／评分指纹和真实加载的 prompt_versions／prompt_sha256 绑定进 manifest 与轮模板。原始 trace 不复制进判分目录，也不透传模型配置。底层未接通阶段仅离线 fake／显式调用适配函数，不提供默认真实模型命令。

case 文档外层为 format_version、evaluation、case_id、input。input 只包含：

- entry（ID／kind）、as_of、timezone；不把群名当事实。
- memories：最终可用画像列表，字段为 memory_id、revision、kind、category、body、author、speaker、stance、about、owner_entry_id、lifecycle、event_time、importance、sources。sources 包含消息身份、作者、日期、原话摘录和核对上下文；间接来源附 memory_id／revision／body 和展开的本入口消息。不得含模型 supported、violations、自检 reason、置信判分等结论。
- projection：text、memory_ids，与实际投影一致；本阶段仅模拟投影，不声称已验证宿主 v1。实际宿主字段兼容由后续接入负责。
- stages：generated、deterministic、checked、projected 四组候选正文和绑定依据。阶段名称只表示流水线位置，不提供自检理由。原始生成中无效引用保留为可核对的无效绑定，不补造事实。
- must_cover、forbidden：原标签原顺序；不把标签传入生成或检查提示词。

完整 run.json 另存模型原始正文、删项原因、操作回执、游标前后／未扫完、取材截断、修订冲突、重复证据、手写群规冲突。引用存在率单列，不能代替语义有依据率。中间覆盖由外判填写 stage_coverage；程序只能计数与比较索引，不能自行猜测覆盖。

最终统计输出有依据分子／分母、禁止内容条目及投影违规、原 forbidden 命中、人工条目数、全量覆盖与阶段损失、两轮逐项分歧；同时固定标记 scoring_status 和 gate_eligible=false。批准状态由规划者于 2026-10-13 登记，不能由命令行自行改变；真实写入口、硬范围、清除与投影接通后才可将 gate_eligible 设为 true。

## 第一阶段接口边界

entry_profile.py 负责快照后的有界取材、增量游标建议、生成／检查及确定性操作校验；模型调用在快照读事务结束后、写回之前。只输出 new／confirm／revise／retire。当前运行服务仍使用旧 v2 文档任务。

存储适配接口负责：读取入口主体／硬范围与来源快照，在同一短事务复核入口设置版本、游标、目标及来源修订、生命周期、权限、人工保护、独立证据回执；原子提交普通记忆操作和游标。失败不推进游标。没有新的独立来源不得确认；被本轮判为不足的证据可推进“已检查”游标，但须保留有界历史锚点供后续自动再取材，不能清除原消息。跨页锚点不足必须诊断，不能声称扫过全部历史。

自动 importance 最大 69（低于当前不衰减门槛 70），event_time=null，不自动置顶。手写内容保持原规则，自动冲突仅记录。缺少底层适配器时新任务拒绝运行；不把模拟存储写到现有数据库，不变更通用生命周期。

待排队的真实底层、迁移、范围传递、清除、调度、召回、宿主、管理及前端接入另行授权。模拟器的成功只证明接口机制，不代表这些能力已实现。
