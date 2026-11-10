# people_merge_v1 公开开发集说明

people_merge_v1 是 Iris“人物同一人自动合并”的中文公开开发集，用于检查和迭代对 possible/same_as 联系的复核判断。案例以日常群聊、私聊及跨平台协作为背景，混合账号迁移、误认、转述、共用设备和角色扮演等情境。账号编号为案例内部的虚构编号，所有案例均为公开 dev 数据。

## 规模与格式

语料文件为 people_merge_v1.json，format_version=1，name=people_merge_v1，timezone=Asia/Shanghai，role_name=Iris。共 30 例，ID 为 PM001—PM030，split 全为 dev。

全集包含 588 条消息、200 个主体、59 条记忆和 70 条待复核联系；其中 24 条标为 merge，46 条标为 keep_possible。另有 5 条历史 denied 和 2 条 roleplay 辅助联系，用于表达既有状态，不作为新的待复核对象。每例有 16—24 条消息。

入口、主体、账号及消息等局部编号按案例隔离；定位一条消息或联系时应同时带上案例 ID。不同案例之间不延续人物身份。时间以 at 的显式偏移量换算到 Asia/Shanghai；部分消息特意采用 UTC 或 -04:00，以覆盖本地日期边界。

## 覆盖的类别

覆盖按实际联系及其诊断依据登记，一个案例可以覆盖多个类别。category 字段只表示该例的主场景；每份 15 例有 15 种主 category，但按联系均覆盖全部 16 类。family_shared 由 PM010/l2、PM015/l2、PM027/l2、PM029/l2 覆盖，未单独占用主 category。下表按不同案例计数，同一例多条同类联系只计一次。

| 类别 | 含义 | 期望 | 不同案例数 |
| --- | --- | --- | ---: |
| true_same | 本人一条与另一日期的熟人明确确认 | merge | 7 |
| self_twice | 本人在两个日期分别说明账号归属 | merge | 5 |
| two_others | 两名不同熟人在不同日期独立明确指认 | merge | 5 |
| cross_entry | 有效依据分散在群聊、私聊或不同平台入口 | merge | 6 |
| same_name | 同名或相似称呼导致误认 | keep_possible | 4 |
| single_guess | 只有一名熟人的一次推测 | keep_possible | 4 |
| same_day | 原始消息不同，但依据发送于同一本地日期 | keep_possible | 4 |
| same_source | 不同转贴记录引用同一条原始消息 | keep_possible | 4 |
| same_other | 同一名旁观者跨日重复指认 | keep_possible | 4 |
| role_only | 身份说法只出自 Iris 自己 | keep_possible | 4 |
| self_denial | 本人明确否认误认 | keep_possible | 4 |
| counter_evidence | 表面依据充足，但存在两人互相对话等反证 | keep_possible | 4 |
| roleplay | 账号承载虚构角色，操作者与角色需要区分 | keep_possible | 4 |
| forward | 代发或转发窗口保留原作者的第一人称 | keep_possible | 4 |
| family_shared | 表亲、伴侣或长辈等家人共用账号 | keep_possible | 4 |
| denied_chain | 当前主体的历史合并链上已有永久否认 | keep_possible | 4 |

以上计数允许类别交叠，不能相加作为案例总数。完整“类别—案例—联系”清单及逐联系核对见随修改稿保存的 EDITS-people-merge-v1.md。

## 标签口径

merge 要求身份语义支持同一人，并至少有两条独立依据：不同的原始消息、不同的本地发送日期，且至少一条由联系的一方本人发出，或两条由两名不同的其他人发出。“其他人”不包括联系双方与 Iris。self_output 和 sender=self 均不作为依据，即使改成 message 类型或进入记忆，也不会变为合格依据。引用同一条原话的多次转贴只算一条；回述事件的日期不替代消息发送日期。

合并还要求没有反证、历史 denied、roleplay 或祖先/后代关系阻挡。target 选择 created_at 较早的一方；创建时间相同取编号较小者。本集同时保留了同刻按编号选择，以及较大编号主体创建更早的例子。

keep_possible 表示本次不应自动合并，可以是证据不足、主体语义不同，或已有永久否认等规则阻挡；该标签不要求自动否认联系，也不代表两人一定不同。已有 denied 状态永久保留。

expected.evidence 仅作诊断。正例列出满足门槛的依据；负例可以为空，也可以列出需要排除的候选依据，需结合完整上下文及结构状态理解。category、expected、reason 属于标注信息，不得进入模型材料。聊天中的姓名、别名与账号关系不能单凭显示名相同来确认。

历史否认例按格式直接构造快照，利用 status=denied 与 merged_into 保留旧主体记录；应遍历双方历史主体后核对否认。本集未验证这些快照可由基线普通操作顺序复现，也未覆盖 folded_into 上的历史否认或直接以祖先、后代作为候选双方的门槛。指纹去重、修订冲突、拆分与回滚等运行行为需另行验证。

本次交付已完成静态结构、引用、时间、覆盖统计和逐联系人工语义核对；未调用模型、运行评测或测量识别率。

语料文件 SHA-256：`9938e05a743d5979eb14a1cc14c10b14b78cdc912bb548fdd27bfb31be64b7c8`。
