# 对话中准备：最新提问点名与查询组成（2026-10-08）

选定最近两条他人消息，只有最新一条中的点名作为锚点。新长对话 dev 的默认混合 Recall@8／nDCG@8 从 0.8889／0.8889 提高到 1.0000／1.0000，18 条有答案全部命中；无答案误返 3/6 → 2/6，relevant 标注精确率 0.7273 → 0.8182。原四集 144 条在各对照路径上的返回 ID、顺序、reason 全部逐条一致，包含 120 条显式文本查询。全部现有召回守则满足。

这是公开 dev 和流程验证，不是隐藏集验收。没有运行学习质量评测、端到端判分或查询内容特判；学习文件、提示词、学习检索、评分和旧语料保持原样。

## 基线、冻结与方法

开始前 fetch origin，确认 PR #15 已以 `9ee15e6` 合入；本分支从 `38ef833` 新建。仓库外 git archive 保存同一 main 代码，用本分支 Python 3.13 环境运行基线。新集和候选方案在任何实现修改之前以 `115b53023efe4452d8e735cbd84919db91a52283` 单独提交冻结，冻结后未修改正文、来源或标签。

新集为手写的 30 条固定记忆、24 条 dev 查询（18 有答案、6 无答案），每条 6—20 条消息，私聊 9、群聊 9、直播 6；105 条他人消息、75 条角色输出、2 条事件、1 条行动结果。全为独立虚构，没有预演人名和事实；既有 kind 格式已经支持区分消息类型，不需要迁移。缺省 kind 仍为 message。

基线和最终默认评测都覆盖五份公开语料：169 条记忆、168 条查询。候选选择仅使用 recall_conversation_v1＋v2 的 48 条 dev，各语料独立入库；其他 120 条查询只检查回归。固定参数仍为混合 trigram、2048 维 float32、向量绝对下限 0.35／相对比例 0.75／权重 1、全文覆盖率 0.75／长片段最大文档频率 2、原问题前缀。纯全文使用既有独立默认 jieba／覆盖率 0／长片段频率通路关闭。没有重新标定上述任何参数。

基线新增 54 个公开语料 embedding 输入；候选最终使用相同显式 2048 维缓存，新输入 99 个。最终正式 `iris eval recall` 新请求为 0，基线与候选使用同一批记忆向量及相同输入的查询向量。预取早期发现配置未显式填写维度时的缓存键不同，尚未产生候选分数就停止该次预取，改为统一显式 2048 维；已取得的向量仍留在缓存，未混用不同键来选择结果。

## 预演五次漏召回的本地复现

只复制指定的 data/iris.db 到仓库外；读取指定 report.md 和两份消息／记忆 evidence。没有读取或复制 secrets.json、.admin-password、logs。按问题消息 ID 截断后续消息，按创建时间隐藏未来记忆，逆序恢复之后的正文修订并移除未来来源；固定排名时钟，重建当时最近五条查询、名称锚点、词项覆盖率与 R11 来源判定。历史返回从副本的 recalls／recall_items 读取，表中原始 ID 与预演一致；这不是在最新完整对话上重问。

| 问题消息 → 角色回复 | 当时最近五条消息 | 历史返回 | 应召回 | 本地确认的阻断 |
| --- | --- | --- | --- | --- |
| #14 → #15（复合提问） | 3,4,5,6,14 | 6,8 | 9（同事计划） | 9 可通过锚点且不是 R11 冗余，但完整词项覆盖仅 1/64=0.0156，无稀有长片段；全文不入选。混合查询包含饮品／出差／另一人等多话题。原查询向量未保存，向量分数与相对下限未重测。 |
| #22 → #23（运动习惯） | 10,11,12,13,22 | 9,10 | 11 | 旧消息中的阿哲成为唯一锚点，11 不涉及此人，锚点直接排除；全文覆盖 6/64=0.0938，也无稀有片段。11 的来源在其他入口，R11 不排除它。 |
| #24 → #25（再次追问） | 12,13,22,23,24 | 9,10 | 11 | 阿哲仍在旧消息中，11 再次被锚点排除；覆盖 7/64=0.1094。角色此前声称不记得的输出又进入查询。 |
| #28 → #29（取物安排） | 18,19,20,21,28 | 6,8 | 12 | 旧消息中的小岚与角色称呼形成锚点集合（self 与小岚），12 关于用户，不在此范围。覆盖 3/64=0.0469；并非数据库丢失。 |
| #32 → #33（含地点的追问） | 28,29,30,31,32 | 6,8 | 12 | 旧消息仍点名小岚，12 被锚点排除。覆盖 8/53=0.1509 虽低，但已经命中照相馆等稀有长片段；仍不能越过锚点。 |

对用户提出的 R11 猜测，慢跑记忆 #11 在 #28／#32 时虽然确实全部来源都在近期消息中，但它已在更早的锚点阶段被排除，不能成为计算相对下限的最佳锚内候选。所以这条慢跑记忆抬高相对下限并不是这两次取物漏召回的原因。其他候选的精确余弦影响尚不能量化，不把它写成已证实。R11、R12、R13 和向量相对下限的原实现均保留。

自动审批拒绝了向原 embedding 服务发送这五次预演文本的诊断请求，理由是尚未明确授权预演内容向该远程服务传出。已单独向用户询问；本报告写入时尚未获得该授权，因此没有绕过限制调用服务。历史查询向量本来未持久保存，不能用虚构向量补成“完整混合复现”。本地证据足以确认四次锚点阻断和第五次全文覆盖不足，但尚未完成五次历史向量分数／修复后真实混合返回的复测。

## 实现与确定性验证

自动 prepare 的查询和锚点文本在同一个读快照中、embedding 调用前固定。只选 kind=message 且发送者不是 self／scene 的最新两条，按时间顺序拼接；只有其中最新一条决定主体范围。角色输出、行动结果和场景事件仍能返回给宿主作为近期上下文，但不进入自动查询。较早消息提供省略问句上下文，不能限制锚点。

显式 text（包括空字符串）原样走原路径。最新点名仍约束全文、向量与人物要点候选；只有姓名的短问题仍使用最新消息的姓名候选通路。模型等待期间新到达的消息可出现在响应快照中，但不会把新提问的锚点配给旧查询向量。prepare_query 与实际 prepare 共用派生函数，评测预取一致。

首次确定性测试在旧实现上 12 失败、2 通过，之后才实现。最终新增 20 项覆盖混合／纯全文、历史点名、最新点名及“是谁”、三种非他人消息、无他人消息、显式查询、调用期间消息到达、四个候选的组成，以及跨入口来源和原文隔离。

## 预定方案与选择

四个候选在冻结 notes 中先写定：latest_1／latest_2／latest_3 为最近 1／2／3 条他人消息，window_5 为最近五条所有消息中去掉非他人消息。所有候选只以最新他人消息点名。anchor_only 是保持旧五条查询、仅更换锚点的诊断对照，不参与推荐。

Q=(Recall@8+nDCG@8)/2；距全网格最高 Q 不足 0.01 才算持平，再依次比较 relevant 精确率、较低误返、Q 和冻结顺序。没有逐项链式平分或选择某个集合的局部赢家。

| 方案（混合，48 条） | Recall@8 | nDCG@8 | Q | relevant 精确率 | 无答案误返 | 平均返回 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| latest_1 | 0.9028 | 0.9063 | 0.9045 | 0.8500 | 4/12 | 1.6667 |
| latest_2（选定） | 0.9722 | 0.9539 | 0.9631 | 0.8000 | 4/12 | 1.7708 |
| latest_3 | 0.9722 | 0.9539 | 0.9631 | 0.7826 | 4/12 | 1.7917 |
| window_5 | 0.9722 | 0.9539 | 0.9631 | 0.7826 | 4/12 | 1.7917 |
| anchor_only | 0.9722 | 0.9539 | 0.9631 | 0.7500 | 5/12 | 1.8333 |

latest_2、latest_3、window_5 的 Q 完全相同，latest_2 的 relevant 精确率最高。latest_1 丢失必要前文，额外漏掉旧集 T12／T13 和新集 U07，不满足旧集 Recall 不降。仅改锚点已能恢复新集答案，但仍有 3/6 无答案误返；移除角色回复并缩短到两条进一步降至 2/6。旧集 T07 的原有漏召回仍存在，没有为它新增特判。

## 各语料前后对照

数值为 main → 本分支，误返以查询计数；relevant 精确率不计人物要点。Recall／nDCG 按全部实际返回（含人物要点）计算。v2 的历史 holdout 只作回归报告，不参与选择。

### 默认混合

| 语料 | Recall@8 | nDCG@8 | 无答案误返 | relevant 精确率 | 平均返回 |
| --- | ---: | ---: | ---: | ---: | ---: |
| all | 0.9701 → 0.9857 | 0.9501 → 0.9658 | 24/40 → 23/40 | 0.6121 → 0.6215 | 1.6905 → 1.6905 |
| original_three | 0.9813 → 0.9813 | 0.9553 → 0.9553 | 21/30 → 21/30 | 0.5722 → 0.5722 | 1.9375 → 1.9375 |
| old_four | 0.9833 → 0.9833 | 0.9602 → 0.9602 | 21/34 → 21/34 | 0.5990 → 0.5990 | 1.8056 → 1.8056 |
| recall_v1 | 0.9833 → 0.9833 | 0.9759 → 0.9759 | 6/8 → 6/8 | 0.6545 → 0.6545 | 1.4474 → 1.4474 |
| recall_v2 | 0.9933 → 0.9933 | 0.9600 → 0.9600 | 13/16 → 13/16 | 0.4804 → 0.4804 | 2.0000 → 2.0000 |
| recall_conversation_v1 | 0.9444 → 0.9444 | 0.9078 → 0.9078 | 2/6 → 2/6 | 0.7826 → 0.7826 | 2.5417 → 2.5417 |
| recall_short_terms_v1 | 1.0000 → 1.0000 | 1.0000 → 1.0000 | 0/4 → 0/4 | 1.0000 → 1.0000 | 0.7500 → 0.7500 |
| recall_conversation_v2 | 0.8889 → 1.0000 | 0.8889 → 1.0000 | 3/6 → 2/6 | 0.7273 → 0.8182 | 1.0000 → 1.0000 |

### 默认纯全文降级

| 语料 | Recall@8 | nDCG@8 | 无答案误返 | relevant 精确率 | 平均返回 |
| --- | ---: | ---: | ---: | ---: | ---: |
| all | 0.8216 → 0.8372 | 0.7849 → 0.8005 | 20/40 → 20/40 | 0.2692 → 0.2961 | 2.8095 → 2.6310 |
| original_three | 0.7874 → 0.7874 | 0.7395 → 0.7395 | 15/30 → 15/30 | 0.3022 → 0.3022 | 2.6094 → 2.6094 |
| old_four | 0.8106 → 0.8106 | 0.7679 → 0.7679 | 15/34 → 15/34 | 0.3310 → 0.3310 | 2.4028 → 2.4028 |
| recall_v1 | 0.9833 → 0.9833 | 0.9162 → 0.9162 | 3/8 → 3/8 | 0.4861 → 0.4861 | 1.9211 → 1.9211 |
| recall_v2 | 0.6733 → 0.6733 | 0.6402 → 0.6402 | 8/16 → 8/16 | 0.3119 → 0.3119 | 2.0606 → 2.0606 |
| recall_conversation_v1 | 0.7778 → 0.7778 | 0.7206 → 0.7206 | 4/6 → 4/6 | 0.1546 → 0.1546 | 5.2083 → 5.2083 |
| recall_short_terms_v1 | 1.0000 → 1.0000 | 1.0000 → 1.0000 | 0/4 → 0/4 | 1.0000 → 1.0000 | 0.7500 → 0.7500 |
| recall_conversation_v2 | 0.8889 → 1.0000 | 0.8889 → 1.0000 | 5/6 → 5/6 | 0.1270 → 0.1895 | 5.2500 → 4.0000 |

original_three 为 recall_v1＋recall_v2＋recall_conversation_v1；old_four 再加短词集；all 再加新长对话集。原三集每集及合计、旧对话集各自的 Recall／误返／精确率守则都满足，而且旧四集完全没有返回变化。原四条评测对照路径（jieba／trigram × 全文／混合）的显式文本 120 条也全部逐条一致。

## 四组默认 R10

原脚本 `uv run python evals/benchmark_retrieval.py --default-config --out <仓库外目录>`；每组预热 5 次、采样 60 次，5 千／5 万条、2048 维 float32，查询向量预生成，HTTP 为真实应用的 ASGI TestClient 路径。没有改基准脚本。开始前进程检查没有其他 pytest 或 benchmark_retrieval；两版本测试与本轮 e2e 均已结束。

机器负载（uptime 原值）：`13:16  up 9 days, 12:32, 2 users, load averages: 10.76 4.80 3.65`。负载不等于本任务并行数；脚本还记录每规模的开始／结束负载。

| 规模／点名 | prepare P50/P95 ms | HTTP P50/P95 ms | 向量 P50/P95 ms | 查询后 RSS MiB | 索引 MiB |
| --- | ---: | ---: | ---: | ---: | ---: |
| 5000/unnamed | 15.71/16.55 | 17.94/20.06 | 2.80/3.02 | 138.2 | 40.1 |
| 5000/named | 13.25/36.88 | 15.54/29.59 | 2.85/6.63 | 138.2 | 40.1 |
| 50000/unnamed | 108.35/155.55 | 110.46/126.04 | 28.17/33.25 | 501.8 | 392.8 |
| 50000/named | 101.69/134.36 | 102.85/130.60 | 28.40/30.51 | 501.8 | 392.8 |

四组 prepare P95 均 ≤500ms。直接调用与 HTTP 交替采样，部分 HTTP P95 低于 prepare P95，记录为本次测量波动，不倒推出 HTTP 更快。5 万条最慢的 prepare P95 为 155.55ms，余量足够，本轮未为了性能改变排序结果。

## 四个端到端脚本（仅流程，不判分）

命令：`uv run iris eval e2e --judge-mode external --script E001 --script E005 --script E007 --script E011 --out <仓库外目录>`。用原配置 GLM low、产品 180 秒预算；每个脚本只跑一次，没有对子集挑选或重判。四个检查点都未传查询文本，全部 recent_message_isolation 通过，服务排空检查成功；8 个学习批次全部 succeeded，各一次尝试。这里仅逐字列实际返回，不作语义正确率或门槛结论。

| 脚本／检查点 | 记忆 ID | reason | 实际正文 |
| --- | ---: | --- | --- |
| E001/music-after-restart | 1 | relevant | 我喜欢轻柔的纯音乐，尤其是钢琴独奏，工作时听着很舒服。 |
| E005/self-preference | 1 | relevant | Iris 更喜欢自然纪录片，特别是海洋主题，不喜欢恐怖片的突然惊吓；在薄荷和石头猜测 Iris 最爱恐怖片后明确纠正，并表示不推恐怖片。 |
| E007/promise | 1 | relevant | 我答应南桥在周五收到材料后帮忙检查申请材料，重点看是否漏了附件、日期有没有填错；南桥说明只需检查，不用替其提交，提交由南桥自己决定。 |
| E007/promise | 2 | relevant | 南桥的申请材料还差最后一遍检查，打算在周五（2026-10-09）发给Iris。 |
| E011/group-memory-in-dm | 1 | relevant | 许宁说河岸清洁活动的集合地点改了，原桥头集合点因施工改为2026年10月11日早上九点在社区东门集合。 |
| E011/group-memory-in-dm | 2 | relevant | 我答应10月11日早上替许宁带一壶热茶，并说明杯子请大家自备。 |
| E011/group-memory-in-dm | 3 | relevant | 顾雨表示在10月11日的河岸清洁活动中负责带夹子和垃圾袋。 |

E011 的三条来源均为 cleanup-group，返回近期消息全属于 ning-dm，未泄漏其他入口原始正文。E005 仍可形成复合记忆、E011 还返回分工记忆；本轮按要求不评分、不修改学习行为，不能把“有返回”写成学习质量已通过。

## 测试、构建与学习隔离

Python 3.13：552 passed，1 个上游 Starlette/httpx 弃用警告，78.96 秒；随后 `uv run --locked --isolated --python 3.12 pytest`：552 passed，4 个警告，79.96 秒（另有 jieba 转义提示）。未并行运行两个 Python 版本。`uv build` 成功生成 sdist 与 wheel。

确定性学习请求比较：全部 86 段公开学习语料，两边各 203 批，其中 117 批学习材料非空，不一致 0 批。使用相同可重复假对话模型及假 embedding，先写入记忆再继续后续批次；只固定运行时 now()／检索时钟为 2026-10-06T00:00:00+00:00，按案例 ID 与批次序号对齐，不改请求正文、顺序、引用、空白或语料时间。没有读取模型配置或调用真实学习。

源码指纹（按评测器规则）：

- main：`d3421def25e880ea4b367c1eb220bf8ecb217ba2dd60f3e6afbdf080252e411d`
- 最终本分支：`eb1503a8b943117c27bb8cab4b22d2d1a5db4d678e63a1be3adc7873613bff12`

## 逐查询候选结果

下表返回按实际顺序列出；后缀 H 为 person_highlight，其余均为 relevant；∅ 表示空。标注列为期望记忆 ID。只看返回数量不能判断精确率，尤其是全文降级。完整派生查询文本、锚点文本、各返回与分数在仓库外 candidates/comparison.json；原查询原文在冻结语料。

### 混合逐查询

| 语料／查询 | 标注 | main | latest_1 | latest_2 | latest_3 | window_5 | anchor_only |
| --- | --- | --- | --- | --- | --- | --- | --- |
| recall_conversation_v1/T01 | C01 | C01, C13 H, C14 H | C01, C13 H, C14 H | C01, C13 H, C14 H | C01, C13 H, C14 H | C01, C13 H, C14 H | C01, C13 H, C14 H |
| recall_conversation_v1/T02 | C02 | C02, C15 H, C16 H | C02, C15 H, C16 H | C02, C15 H, C16 H | C02, C15 H, C16 H | C02, C15 H, C16 H | C02, C15 H, C16 H |
| recall_conversation_v1/T03 | C06 | C06 | C06 | C06 | C06 | C06 | C06 |
| recall_conversation_v1/T04 | C30 | C30, C17 H, C15 H, C18 H | C30, C17 H, C15 H, C18 H | C30, C17 H, C15 H, C18 H | C30, C17 H, C15 H, C18 H | C30, C17 H, C15 H, C18 H | C30, C17 H, C15 H, C18 H |
| recall_conversation_v1/T05 | C04 | C04, C13 H, C14 H | C04, C13 H, C14 H | C04, C13 H, C14 H | C04, C13 H, C14 H | C04, C13 H, C14 H | C04, C13 H, C14 H |
| recall_conversation_v1/T06 | C07 | C07, C17 H, C15 H, C18 H | C07, C17 H, C15 H, C18 H | C07, C17 H, C15 H, C18 H | C07, C17 H, C15 H, C18 H | C07, C17 H, C15 H, C18 H | C07, C17 H, C15 H, C18 H |
| recall_conversation_v1/T07 | C09, C10 | C15 H, C13 H, C16 H | C09, C15 H, C13 H, C16 H | C15 H, C13 H, C16 H | C15 H, C13 H, C16 H | C15 H, C13 H, C16 H | C15 H, C13 H, C16 H |
| recall_conversation_v1/T08 | C11 | C11 | C11 | C11 | C11 | C11 | C11 |
| recall_conversation_v1/T09 | C07 | C33, C07 | C07, C33 | C33, C07 | C33, C07 | C33, C07 | C33, C07 |
| recall_conversation_v1/T10 | C27 | C27 | C27 | C27 | C27 | C27 | C27 |
| recall_conversation_v1/T11 | C10, C09 | C09, C10 | C09, C10 | C09, C10 | C09, C10 | C09, C10 | C09, C10 |
| recall_conversation_v1/T12 | C24 | C24, C13 H, C17 H, C14 H | C13 H, C17 H, C14 H | C24, C13 H, C17 H, C14 H | C24, C13 H, C17 H, C14 H | C24, C13 H, C17 H, C14 H | C24, C13 H, C17 H, C14 H |
| recall_conversation_v1/T13 | C31 | C31, C15 H, C16 H | C15 H, C16 H | C31, C15 H, C16 H | C31, C15 H, C16 H | C31, C15 H, C16 H | C31, C15 H, C16 H |
| recall_conversation_v1/T14 | C22 | C22 | C22 | C22 | C22 | C22 | C22 |
| recall_conversation_v1/T15 | C26 | C26, C20, C17 H, C15 H, C13 H | C26, C17 H, C15 H, C13 H | C26, C20, C17 H, C15 H, C13 H | C26, C20, C17 H, C15 H, C13 H | C26, C20, C17 H, C15 H, C13 H | C26, C20, C17 H, C15 H, C13 H |
| recall_conversation_v1/T16 | C05 | C05, C13 H, C14 H | C05, C13 H, C14 H | C05, C13 H, C14 H | C05, C13 H, C14 H | C05, C13 H, C14 H | C05, C13 H, C14 H |
| recall_conversation_v1/T17 | C32 | C32 | C32 | C32 | C32 | C32 | C32 |
| recall_conversation_v1/T18 | C29 | C29, C17 H, C15 H, C18 H | C29, C17 H, C15 H, C18 H | C29, C17 H, C15 H, C18 H | C29, C17 H, C15 H, C18 H | C29, C17 H, C15 H, C18 H | C29, C17 H, C15 H, C18 H |
| recall_conversation_v1/T19 | ∅ | C15 H, C16 H | C15 H, C16 H | C15 H, C16 H | C15 H, C16 H | C15 H, C16 H | C15 H, C16 H |
| recall_conversation_v1/T20 | ∅ | C20, C26, C17 H, C13 H, C18 H | C17 H, C13 H, C18 H | C20, C26, C17 H, C13 H, C18 H | C20, C26, C17 H, C13 H, C18 H | C20, C26, C17 H, C13 H, C18 H | C20, C26, C17 H, C13 H, C18 H |
| recall_conversation_v1/T21 | ∅ | ∅ | C25 | ∅ | ∅ | ∅ | ∅ |
| recall_conversation_v1/T22 | ∅ | C33 | C33 | C33 | C33 | C33 | C33 |
| recall_conversation_v1/T23 | ∅ | C17 H, C15 H, C18 H | C17 H, C15 H, C18 H | C17 H, C15 H, C18 H | C17 H, C15 H, C18 H | C17 H, C15 H, C18 H | C17 H, C15 H, C18 H |
| recall_conversation_v1/T24 | ∅ | C13 H, C14 H | C13 H, C14 H | C13 H, C14 H | C13 H, C14 H | C13 H, C14 H | C13 H, C14 H |
| recall_conversation_v2/U01 | D01 | D03, D30 | D01 | D01 | D01, D03 | D01, D03 | D01, D03, D30 |
| recall_conversation_v2/U02 | D07 | D07 | D07 | D07 | D07 | D07 | D07 |
| recall_conversation_v2/U03 | D08 | D08 | D08 | D08 | D08 | D08 | D08 |
| recall_conversation_v2/U04 | D04 | D04 | D04 | D04 | D04 | D04 | D04 |
| recall_conversation_v2/U05 | D06 | D06 | D06 | D06 | D06 | D06 | D06 |
| recall_conversation_v2/U06 | D15 | D15 | D15 | D15, D16 | D15, D16 | D15, D16 | D15, D16 |
| recall_conversation_v2/U07 | D09 | D09 | ∅ | D09 | D09 | D09 | D09 |
| recall_conversation_v2/U08 | D13 | D13 | D13 | D13 | D13 | D13 | D13 |
| recall_conversation_v2/U09 | D11 | D11 | D11 | D11 | D11 | D11 | D11 |
| recall_conversation_v2/U10 | D12 | D12 | D12 | D12 | D12 | D12 | D12 |
| recall_conversation_v2/U11 | D10 | D10 | D10 | D10 | D10 | D10 | D10 |
| recall_conversation_v2/U12 | D02 | D02, D26 | D02, D26 | D02, D26 | D02, D26 | D02, D26 | D02, D26 |
| recall_conversation_v2/U13 | D24 | D24 | D24 | D24 | D24 | D24 | D24 |
| recall_conversation_v2/U14 | D18 | D18 | D18 | D18 | D18 | D18 | D18 |
| recall_conversation_v2/U15 | D19 | ∅ | D19 | D19 | D19 | D19 | D19 |
| recall_conversation_v2/U16 | D25 | D25 | D25 | D25 | D25 | D25 | D25 |
| recall_conversation_v2/U17 | D28 | D28 | D28 | D28 | D28 | D28 | D28 |
| recall_conversation_v2/U18 | D21 | D21 | D21 | D21 | D21 | D21 | D21 |
| recall_conversation_v2/U19 | ∅ | D19 | D19 | D19 | D19 | D19 | D19 |
| recall_conversation_v2/U20 | ∅ | D15 | D15 | ∅ | ∅ | ∅ | D15 |
| recall_conversation_v2/U21 | ∅ | D07 H, D17 H | D07 H, D17 H | D07 H, D17 H | D07 H, D17 H | D07 H, D17 H | D07 H, D17 H |
| recall_conversation_v2/U22 | ∅ | ∅ | ∅ | ∅ | ∅ | ∅ | ∅ |
| recall_conversation_v2/U23 | ∅ | D29 | ∅ | D29 | D29 | D29 | D29 |
| recall_conversation_v2/U24 | ∅ | ∅ | ∅ | ∅ | ∅ | ∅ | ∅ |

### 默认纯全文逐查询

| 语料／查询 | 标注 | main | latest_1 | latest_2 | latest_3 | window_5 | anchor_only |
| --- | --- | --- | --- | --- | --- | --- | --- |
| recall_conversation_v1/T01 | C01 | C01, C28, C24, C17, C13 H, C14 H | C28, C24, C17, C13 H, C14 H | C01, C28, C24, C17, C13 H, C14 H | C01, C28, C24, C17, C13 H, C14 H | C01, C28, C24, C17, C13 H, C14 H | C01, C28, C24, C17, C13 H, C14 H |
| recall_conversation_v1/T02 | C02 | C02, C17, C12, C15, C22, C29, C08, C33 | C02, C17, C15, C08, C33, C10, C16 H | C02, C17, C12, C15, C22, C29, C08, C33 | C02, C17, C12, C15, C22, C29, C08, C33 | C02, C17, C12, C15, C22, C29, C08, C33 | C02, C17, C12, C15, C22, C29, C08, C33 |
| recall_conversation_v1/T03 | C06 | C06, C04, C17, C19 | C06, C04, C19 | C06, C04, C17, C19 | C06, C04, C17, C19 | C06, C04, C17, C19 | C06, C04, C17, C19 |
| recall_conversation_v1/T04 | C30 | C30, C05, C03, C22, C15, C14, C17 H, C18 H | C30, C05, C03, C15, C14, C17 H, C18 H, C16 H | C30, C05, C03, C22, C15, C14, C17 H, C18 H | C30, C05, C03, C22, C15, C14, C17 H, C18 H | C30, C05, C03, C22, C15, C14, C17 H, C18 H | C30, C05, C03, C22, C15, C14, C17 H, C18 H |
| recall_conversation_v1/T05 | C04 | C22, C20, C18, C27, C16, C17, C11, C13 H | C27, C16, C17, C11, C13 H, C14 H | C22, C20, C18, C27, C16, C17, C11, C13 H | C22, C20, C18, C27, C16, C17, C11, C13 H | C22, C20, C18, C27, C16, C17, C11, C13 H | C22, C20, C18, C27, C16, C17, C11, C13 H |
| recall_conversation_v1/T06 | C07 | C17, C07, C15, C24, C02, C16, C21, C31 | C17, C15, C24, C07, C16, C21, C31, C27 | C17, C07, C15, C24, C02, C16, C21, C31 | C17, C07, C15, C24, C02, C16, C21, C31 | C17, C07, C15, C24, C02, C16, C21, C31 | C17, C07, C15, C24, C02, C16, C21, C31 |
| recall_conversation_v1/T07 | C09, C10 | C21, C31, C17, C15 H, C13 H, C16 H | C21, C31, C17, C15 H, C13 H, C16 H | C21, C31, C17, C15 H, C13 H, C16 H | C21, C31, C17, C15 H, C13 H, C16 H | C21, C31, C17, C15 H, C13 H, C16 H | C21, C31, C17, C15 H, C13 H, C16 H |
| recall_conversation_v1/T08 | C11 | C11, C10, C12, C27, C22, C16, C17, C29 | C11, C12, C27, C22, C16, C17, C29 | C11, C10, C12, C27, C22, C16, C17, C29 | C11, C10, C12, C27, C22, C16, C17, C29 | C11, C10, C12, C27, C22, C16, C17, C29 | C11, C10, C12, C27, C22, C16, C17, C29 |
| recall_conversation_v1/T09 | C07 | ∅ | ∅ | ∅ | ∅ | ∅ | ∅ |
| recall_conversation_v1/T10 | C27 | C27 | C27 | C27 | C27 | C27 | C27 |
| recall_conversation_v1/T11 | C10, C09 | C09, C10 | C09, C10 | C09, C10 | C09, C10 | C09, C10 | C09, C10 |
| recall_conversation_v1/T12 | C24 | C17, C24, C05, C28, C13 H, C14 H, C18 H | C17, C05, C28, C24, C13 H, C14 H, C18 H | C17, C24, C05, C28, C13 H, C14 H, C18 H | C17, C24, C05, C28, C13 H, C14 H, C18 H | C17, C24, C05, C28, C13 H, C14 H, C18 H | C17, C24, C05, C28, C13 H, C14 H, C18 H |
| recall_conversation_v1/T13 | C31 | C31, C12, C17, C22, C29, C15 H, C16 H | C31, C12, C17, C22, C29, C15 H, C16 H | C31, C12, C17, C22, C29, C15 H, C16 H | C31, C12, C17, C22, C29, C15 H, C16 H | C31, C12, C17, C22, C29, C15 H, C16 H | C31, C12, C17, C22, C29, C15 H, C16 H |
| recall_conversation_v1/T14 | C22 | C22, C18, C32, C12, C02, C29 | C22, C32, C12, C02, C29 | C22, C18, C32, C12, C02, C29 | C22, C18, C32, C12, C02, C29 | C22, C18, C32, C12, C02, C29 | C22, C18, C32, C12, C02, C29 |
| recall_conversation_v1/T15 | C26 | C26, C20, C19, C12, C22, C29, C17 H, C15 H | C19, C17 H, C15 H, C13 H | C26, C20, C19, C12, C22, C29, C17 H, C15 H | C26, C20, C19, C12, C22, C29, C17 H, C15 H | C26, C20, C19, C12, C22, C29, C17 H, C15 H | C26, C20, C19, C12, C22, C29, C17 H, C15 H |
| recall_conversation_v1/T16 | C05 | C05, C13 H, C14 H | C05, C13 H, C14 H | C05, C13 H, C14 H | C05, C13 H, C14 H | C05, C13 H, C14 H | C05, C13 H, C14 H |
| recall_conversation_v1/T17 | C32 | C32, C02, C22 | C32, C02, C22 | C32, C02, C22 | C32, C02, C22 | C32, C02, C22 | C32, C02, C22 |
| recall_conversation_v1/T18 | C29 | C03, C15, C30, C14, C17 H, C18 H, C16 H | C17 H, C15 H, C18 H | C03, C15, C30, C14, C17 H, C18 H, C16 H | C03, C15, C30, C14, C17 H, C18 H, C16 H | C03, C15, C30, C14, C17 H, C18 H, C16 H | C03, C15, C30, C14, C17 H, C18 H, C16 H |
| recall_conversation_v1/T19 | ∅ | C26, C25, C19, C20, C15 H, C16 H | C15 H, C16 H | C26, C25, C19, C20, C15 H, C16 H | C26, C25, C19, C20, C15 H, C16 H | C26, C25, C19, C20, C15 H, C16 H | C26, C25, C19, C20, C15 H, C16 H |
| recall_conversation_v1/T20 | ∅ | C20, C26, C15, C19, C08, C33, C10, C17 H | C20, C08, C33, C15, C10, C17 H, C13 H, C18 H | C20, C26, C15, C19, C08, C33, C10, C17 H | C20, C26, C15, C19, C08, C33, C10, C17 H | C20, C26, C15, C19, C08, C33, C10, C17 H | C20, C26, C15, C19, C08, C33, C10, C17 H |
| recall_conversation_v1/T21 | ∅ | C06, C04, C02 | C06, C04, C02 | C06, C04, C02 | C06, C04, C02 | C06, C04, C02 | C06, C04, C02 |
| recall_conversation_v1/T22 | ∅ | ∅ | ∅ | ∅ | ∅ | ∅ | ∅ |
| recall_conversation_v1/T23 | ∅ | C16, C25, C24, C17 H, C15 H, C18 H | C16, C25, C17 H, C15 H, C18 H | C16, C25, C24, C17 H, C15 H, C18 H | C16, C25, C24, C17 H, C15 H, C18 H | C16, C25, C24, C17 H, C15 H, C18 H | C16, C25, C24, C17 H, C15 H, C18 H |
| recall_conversation_v1/T24 | ∅ | C13 H, C14 H | C13 H, C14 H | C13 H, C14 H | C13 H, C14 H | C13 H, C14 H | C13 H, C14 H |
| recall_conversation_v2/U01 | D01 | D03, D30 | D01, D18, D28 | D01, D18, D25, D05, D04, D28 | D01, D07, D03, D24, D18, D25, D05, D09 | D01, D07, D03, D24, D18, D25, D05, D09 | D01, D03, D07, D30, D24, D18, D25, D05 |
| recall_conversation_v2/U02 | D07 | D07, D10, D14, D25, D08, D04, D21, D30 | D07, D10, D08, D21 | D07, D14, D10, D08, D21 | D07, D10, D14, D25, D08, D04, D21, D30 | D07, D10, D14, D25, D08, D04, D21, D30 | D07, D10, D14, D25, D08, D04, D21, D30 |
| recall_conversation_v2/U03 | D08 | D08, D15, D30, D24, D27, D05, D16, D21 | D08, D21, D02 | D08, D24, D21, D02 | D08, D24, D05, D16, D21, D02, D12, D23 | D08, D24, D05, D16, D21, D02, D12, D23 | D08, D15, D30, D24, D27, D05, D16, D21 |
| recall_conversation_v2/U04 | D04 | D04, D18, D25, D23, D28 | D04, D18, D25, D28, D30, D10 | D04, D18, D25, D28, D23, D30, D10 | D04, D18, D25, D23, D28, D30, D10 | D04, D18, D25, D23, D28, D30, D10 | D04, D18, D23, D25, D28, D10, D27, D30 |
| recall_conversation_v2/U05 | D06 | D06 | D06 | D06 | D06 | D06 | D06 |
| recall_conversation_v2/U06 | D15 | D15, D05 | D15 | D15, D16, D05, D25, D04 | D15, D16, D20, D18, D21, D05, D25, D04 | D15, D16, D20, D18, D21, D05, D25, D04 | D15, D16, D20, D07, D18, D21, D05, D25 |
| recall_conversation_v2/U07 | D09 | D09, D20, D29, D23, D18, D05, D16 | ∅ | D09, D29 | D09, D20, D29, D18 | D09, D20, D29, D23, D18 | D09, D20, D29, D23, D18, D05, D16 |
| recall_conversation_v2/U08 | D13 | D13, D14 | D13 | D13 | D13 | D13 | D13 |
| recall_conversation_v2/U09 | D11 | D11, D15, D10, D08, D27, D02 | D11, D27 | D11, D10, D27 | D11, D10, D27 | D11, D10, D27 | D11, D15, D10, D08, D27, D02 |
| recall_conversation_v2/U10 | D12 | D12, D29, D05, D23, D25, D19, D04 | D12, D23, D19 | D12, D29, D23, D19 | D12, D29, D23, D19 | D12, D29, D05, D23, D25, D19, D04 | D12, D29, D05, D23, D25, D19, D04 |
| recall_conversation_v2/U11 | D10 | D10, D27 | D10, D27 | D10, D27 | D10, D27 | D10, D27 | D10, D27 |
| recall_conversation_v2/U12 | D02 | D02, D26 | D02, D26 | D02, D26 | D02, D26 | D02, D26 | D02, D26 |
| recall_conversation_v2/U13 | D24 | D24, D22, D30, D07, D08, D29, D02, D12 | D24, D30, D22 | D24, D22, D30, D29, D12, D19 | D24, D22, D30, D07, D08, D29, D02, D12 | D24, D22, D30, D07, D08, D29, D02, D12 | D24, D22, D30, D07, D08, D29, D02, D12 |
| recall_conversation_v2/U14 | D18 | D18, D01, D04, D26, D07, D15, D13, D10 | D18, D04, D01, D15, D28 | D18, D07, D04, D15, D01, D28 | D18, D01, D04, D26, D07, D15, D25, D17 | D18, D01, D04, D26, D07, D15, D25, D17 | D18, D01, D04, D26, D07, D15, D13, D10 |
| recall_conversation_v2/U15 | D19 | D22 | D19, D29, D22, D13, D12, D21, D25 | D19, D29, D22, D13, D12, D21, D25 | D19, D29, D22, D13, D12, D21, D25 | D19, D29, D22, D13, D12, D21, D25 | D19, D25, D29, D22, D13, D05, D12, D21 |
| recall_conversation_v2/U16 | D25 | D25, D15, D22, D18, D02, D05, D16, D04 | D25, D18, D22, D04, D30, D10 | D25, D18, D15, D22, D04, D30, D10 | D25, D15, D22, D18, D05, D16, D04, D30 | D25, D15, D22, D18, D05, D16, D04, D30 | D25, D15, D22, D18, D02, D05, D16, D04 |
| recall_conversation_v2/U17 | D28 | D28, D10, D15, D25, D04, D23, D30 | D28, D15, D23 | D28, D15, D23, D10 | D28, D15, D23, D10 | D28, D15, D23, D10 | D28, D10, D15, D25, D04, D23, D30 |
| recall_conversation_v2/U18 | D21 | D21, D29, D30, D14, D24, D27 | D21, D29 | D21, D29, D24, D27 | D21, D29, D24, D27 | D21, D29, D24, D27 | D21, D29, D30, D14, D24, D27 |
| recall_conversation_v2/U19 | ∅ | D19, D24, D10, D08, D05, D16, D21 | D19 | D19, D08, D21 | D19, D24, D08, D05, D16, D21 | D19, D24, D08, D05, D16, D21 | D19, D24, D10, D08, D05, D16, D21 |
| recall_conversation_v2/U20 | ∅ | ∅ | ∅ | ∅ | ∅ | ∅ | ∅ |
| recall_conversation_v2/U21 | ∅ | D07, D01, D12, D27, D30, D13, D21, D19 | D07, D30, D13, D19, D17 H | D07, D30, D13, D21, D19, D17 H | D07, D30, D13, D21, D19, D17 H | D07, D30, D13, D21, D19, D17 H | D07, D01, D12, D27, D30, D13, D21, D19 |
| recall_conversation_v2/U22 | ∅ | D25, D04, D30, D29, D05, D10 | D25, D30, D04, D10 | D25, D30, D04, D10 | D25, D04, D29, D05, D30, D10 | D25, D04, D29, D05, D30, D10 | D25, D04, D30, D29, D05, D10 |
| recall_conversation_v2/U23 | ∅ | D29, D21, D14, D23, D02, D12, D25 | D29 | D29, D23, D02, D12, D21, D25 | D29, D21, D23, D02, D12, D25 | D29, D21, D23, D02, D12, D25 | D29, D21, D14, D23, D02, D12, D25 |
| recall_conversation_v2/U24 | ∅ | D07, D13, D26, D18, D10, D12, D23, D19 | ∅ | D10 | D13, D26, D18, D10, D12, D23, D19 | D13, D26, D18, D10, D12, D23, D19 | D07, D13, D26, D18, D10, D12, D23, D19 |

## 问题、材料与交付边界

- 新集仍有 2/6 无答案查询返回 relevant，默认全文仍为 5/6；拒绝无答案问题没有在本轮解决。旧集 T07 的原有漏召回保留。
- R11 的一般性先截断后去冗余顺序没有修改；本次确认特定慢跑记忆已先被锚点排除，不能把它当成相对下限根因。五次预演历史向量未保存，远程重取仍待明确授权。
- 候选预取最初的未显式维度缓存键不一致在评分之前纠正；中断预取及缓存仍保留，没有用它的结果选参。最终召回复用缓存，零新网络请求；公开 e2e 四个检查点没有降级提示或失败批次。
- 仅 macOS 验证；未寻找或读取隐藏验收集，不声明 M1 验收通过。
- 原始数据库副本、完整返回、缓存、候选驱动脚本、学习请求、测试和审计均在仓库外；提交只包含冻结 dev、实现／测试、召回文档和本报告。每次提交前执行 git check-ignore、查看暂存 diff 并检查密钥模式。

仓库外工作材料：`/private/tmp/iris-conversation-prepare-20261008/`。稳定归档目录：`/Users/cassia/Local/Code/iris-eval-artifacts/conversation-prepare-20261008/`。预演数据只留本机归档，不随报告提交。

## 第二轮

本轮新增语料在 `0ba3452` 单独冻结：26 条记忆、20 条 dev，四类各 4 条有答案，另 4 条无答案；近期消息 6—9 条，包含 self_output。原五集和 v3 的正文、来源与标签未修改。候选定义见 `evals/recall_conversation_v3_notes.md`。开始前 fetch origin，分支 `fix/conversation-prepare`、HEAD `1e81f3f1329587205fa02828a68c0ef428aa5696`、工作区干净。main 固定为 `38ef833f4924d26c4a2b6578bc79638c8715a0d1`。

### 选择结果与守则

三种预定候选的混合 Q 相同，按冻结的精确率顺序得到 adaptive_6。但它在 conversation_v2 的 relevant 精确率由 0.8182 降至 0.7500，降幅 0.0682 > 0.03；其余两种同样不满足。因此本轮尚无可选入默认配置的方案，不能把公开 dev 召回改善写成守则通过。默认仍为第一轮 latest_2。已向规划者报告，等待是否另行冻结下一组通用候选的决定。

| 候选（混合，68 条） | Recall@8 | nDCG@8 | Q | relevant 精确率 | 无答案误返 | 平均返回 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| main | 0.8269 | 0.8000 | 0.8135 | 0.7414 | 6/16 | 1.9559 |
| latest_2 | 0.8462 | 0.8264 | 0.8363 | 0.8000 | 5/16 | 1.9265 |
| session_6 | 0.9808 | 0.9681 | 0.9744 | 0.7324 | 8/16 | 2.1618 |
| speaker_6 | 0.9808 | 0.9681 | 0.9744 | 0.7429 | 7/16 | 2.1471 |
| adaptive_6 | 0.9808 | 0.9681 | 0.9744 | 0.7647 | 7/16 | 2.1176 |

选参只使用三份 conversation 的 68 条 dev，其他三集不参与。查询参数、向量维度、全文参数和判定守则不变。v3 的 16 条有答案在三种候选上都 Recall/nDCG=1；adaptive_6 的 v3 无答案误返为 3/4，第一轮为 1/4，拒绝无答案的问题仍未解决。v2 混合只有 U07 发生返回变化：`D09` → `D09, D10, D27`，后两条均为 relevant，属于较早前文的另一个人物话题。conversation_v1 的结果逐条不变。

### 各语料前后对照

下表顺序为 main / 第一轮 latest_2 / 本轮最高分候选 adaptive_6。无答案仅统计 relevant；H 人物要点不算误返。Recall/nDCG 计全部实际返回。

#### 默认混合

| 语料 | Recall@8 | nDCG@8 | 无答案误返 | relevant 精确率 |
| --- | --- | --- | --- | --- |
| recall_v1 | 0.9833 / 0.9833 / 0.9833 | 0.9759 / 0.9759 / 0.9759 | 6/8 / 6/8 / 6/8 | 0.6545 / 0.6545 / 0.6545 |
| recall_v2 | 0.9933 / 0.9933 / 0.9933 | 0.9600 / 0.9600 / 0.9600 | 13/16 / 13/16 / 13/16 | 0.4804 / 0.4804 / 0.4804 |
| recall_conversation_v1 | 0.9444 / 0.9444 / 0.9444 | 0.9078 / 0.9078 / 0.9078 | 2/6 / 2/6 / 2/6 | 0.7826 / 0.7826 / 0.7826 |
| recall_short_terms_v1 | 1.0000 / 1.0000 / 1.0000 | 1.0000 / 1.0000 / 1.0000 | 0/4 / 0/4 / 0/4 | 1.0000 / 1.0000 / 1.0000 |
| recall_conversation_v2 | 0.8889 / 1.0000 / 1.0000 | 0.8889 / 1.0000 / 1.0000 | 3/6 / 2/6 / 2/6 | 0.7273 / 0.8182 / 0.7500 |
| recall_conversation_v3 | 0.6250 / 0.5625 / 1.0000 | 0.5789 / 0.5394 / 1.0000 | 1/4 / 1/4 / 3/4 | 0.6923 / 0.8000 / 0.7619 |

#### 默认纯全文降级

| 语料 | Recall@8 | nDCG@8 | 无答案误返 | relevant 精确率 |
| --- | --- | --- | --- | --- |
| recall_v1 | 0.9833 / 0.9833 / 0.9833 | 0.9162 / 0.9162 / 0.9162 | 3/8 / 3/8 / 3/8 | 0.4861 / 0.4861 / 0.4861 |
| recall_v2 | 0.6733 / 0.6733 / 0.6733 | 0.6402 / 0.6402 / 0.6402 | 8/16 / 8/16 / 8/16 | 0.3119 / 0.3119 / 0.3119 |
| recall_conversation_v1 | 0.7778 / 0.7778 / 0.7778 | 0.7206 / 0.7206 / 0.7206 | 4/6 / 4/6 / 4/6 | 0.1546 / 0.1546 / 0.1546 |
| recall_short_terms_v1 | 1.0000 / 1.0000 / 1.0000 | 1.0000 / 1.0000 / 1.0000 | 0/4 / 0/4 / 0/4 | 1.0000 / 1.0000 / 1.0000 |
| recall_conversation_v2 | 0.8889 / 1.0000 / 1.0000 | 0.8889 / 1.0000 / 1.0000 | 5/6 / 5/6 / 4/6 | 0.1270 / 0.1895 / 0.1731 |
| recall_conversation_v3 | 0.6875 / 0.5625 / 1.0000 | 0.5522 / 0.5082 / 0.8847 | 4/4 / 2/4 / 4/4 | 0.1235 / 0.1739 / 0.1600 |

recall_v1、recall_v2、recall_short_terms_v1 的 120 条查询在以上两条默认路径上，每个候选与第一轮的返回 ID、顺序、reason 逐条一致。没有新发出这三集的网络请求：226 个所需输入全部命中既有精确键缓存。三份对话集共补 125 个向量；缓存键严格包含端点、模型、2048 维及实际输入文本。不同 corpus 独立数据库；main、第一轮、本轮共享相同记忆向量和相同文本的查询向量，排序时钟固定为各语料 as_of。

### 五次预演复放

按授权仅复制 `/private/tmp/iris-m1-demo-20261008/data/iris.db` 到自己的仓库外目录。未打开、复制或读取同目录 secrets.json、.admin-password、logs。每个方案、每条触发消息都从该副本重新复制一个数据库：物理删除 ID 更大的消息、创建时间晚于触发 received_at 的记忆及关联来源；逆序还原触发后正文编辑，删除未来来源，修订恢复的记忆重新计算向量。排序时间固定为触发 received_at；每次直接由 Gateway 调用配置中的真实 embedding，随后调用 prepare，不传 text / participants。全部十次无降级提示。

H 为 person_highlight；其余 relevant。下列是完整有序返回列表。

| 触发 → 预期 | main 返回 | main 命中 | adaptive_6 返回 | 候选命中 |
| --- | --- | --- | --- | --- |
| 14 → 9 | 6, 8 | 否 | 9, 8 H, 6 H | 是 |
| 22 → 11 | 10, 9 | 否 | 11, 10, 1 H, 2 H | 是 |
| 24 → 11 | 10, 9 | 否 | 11, 1 H, 2 H, 3 H | 是 |
| 28 → 12 | 6, 8 H | 否 | 12, 1 H, 7 H, 2 H | 是 |
| 32 → 12 | 8 H, 6 H | 否 | 12, 1 H, 7 H, 2 H | 是 |

这项复放修好了五次指定目标，但不是通过守则的替代证据。它是数据库内容截断后的检索复放，不重跑学习或回复生成；已有 retention 数值没有完整的逐次增量历史，因此保持副本值，不能声称完全重建当时所有评分状态。

### 逐查询候选结果

下表只列用于选择的三份对话集，标注列为应召回 ID。H 为 person_highlight；无 H 的全部为 relevant；∅ 为空。

#### 默认混合逐查询

| 语料／查询 | 标注 | main | latest_2 | session_6 | speaker_6 | adaptive_6 |
| --- | --- | --- | --- | --- | --- | --- |
| recall_conversation_v1/T01 | C01 | C01, C13 H, C14 H | C01, C13 H, C14 H | C01, C13 H, C14 H | C01, C13 H, C14 H | C01, C13 H, C14 H |
| recall_conversation_v1/T02 | C02 | C02, C15 H, C16 H | C02, C15 H, C16 H | C02, C15 H, C16 H | C02, C15 H, C16 H | C02, C15 H, C16 H |
| recall_conversation_v1/T03 | C06 | C06 | C06 | C06 | C06 | C06 |
| recall_conversation_v1/T04 | C30 | C30, C17 H, C15 H, C18 H | C30, C17 H, C15 H, C18 H | C30, C17 H, C15 H, C18 H | C30, C17 H, C15 H, C18 H | C30, C17 H, C15 H, C18 H |
| recall_conversation_v1/T05 | C04 | C04, C13 H, C14 H | C04, C13 H, C14 H | C04, C13 H, C14 H | C04, C13 H, C14 H | C04, C13 H, C14 H |
| recall_conversation_v1/T06 | C07 | C07, C17 H, C15 H, C18 H | C07, C17 H, C15 H, C18 H | C07, C17 H, C15 H, C18 H | C07, C17 H, C15 H, C18 H | C07, C17 H, C15 H, C18 H |
| recall_conversation_v1/T07 | C09, C10 | C15 H, C13 H, C16 H | C15 H, C13 H, C16 H | C15 H, C13 H, C16 H | C15 H, C13 H, C16 H | C15 H, C13 H, C16 H |
| recall_conversation_v1/T08 | C11 | C11 | C11 | C11 | C11 | C11 |
| recall_conversation_v1/T09 | C07 | C33, C07 | C33, C07 | C33, C07 | C33, C07 | C33, C07 |
| recall_conversation_v1/T10 | C27 | C27 | C27 | C27 | C27 | C27 |
| recall_conversation_v1/T11 | C10, C09 | C09, C10 | C09, C10 | C09, C10 | C09, C10 | C09, C10 |
| recall_conversation_v1/T12 | C24 | C24, C13 H, C17 H, C14 H | C24, C13 H, C17 H, C14 H | C24, C13 H, C17 H, C14 H | C24, C13 H, C17 H, C14 H | C24, C13 H, C17 H, C14 H |
| recall_conversation_v1/T13 | C31 | C31, C15 H, C16 H | C31, C15 H, C16 H | C31, C15 H, C16 H | C31, C15 H, C16 H | C31, C15 H, C16 H |
| recall_conversation_v1/T14 | C22 | C22 | C22 | C22 | C22 | C22 |
| recall_conversation_v1/T15 | C26 | C26, C20, C17 H, C15 H, C13 H | C26, C20, C17 H, C15 H, C13 H | C26, C20, C17 H, C15 H, C13 H | C26, C20, C17 H, C15 H, C13 H | C26, C20, C17 H, C15 H, C13 H |
| recall_conversation_v1/T16 | C05 | C05, C13 H, C14 H | C05, C13 H, C14 H | C05, C13 H, C14 H | C05, C13 H, C14 H | C05, C13 H, C14 H |
| recall_conversation_v1/T17 | C32 | C32 | C32 | C32 | C32 | C32 |
| recall_conversation_v1/T18 | C29 | C29, C17 H, C15 H, C18 H | C29, C17 H, C15 H, C18 H | C29, C17 H, C15 H, C18 H | C29, C17 H, C15 H, C18 H | C29, C17 H, C15 H, C18 H |
| recall_conversation_v1/T19 | ∅ | C15 H, C16 H | C15 H, C16 H | C15 H, C16 H | C15 H, C16 H | C15 H, C16 H |
| recall_conversation_v1/T20 | ∅ | C20, C26, C17 H, C13 H, C18 H | C20, C26, C17 H, C13 H, C18 H | C20, C26, C17 H, C13 H, C18 H | C20, C26, C17 H, C13 H, C18 H | C20, C26, C17 H, C13 H, C18 H |
| recall_conversation_v1/T21 | ∅ | ∅ | ∅ | ∅ | ∅ | ∅ |
| recall_conversation_v1/T22 | ∅ | C33 | C33 | C33 | C33 | C33 |
| recall_conversation_v1/T23 | ∅ | C17 H, C15 H, C18 H | C17 H, C15 H, C18 H | C17 H, C15 H, C18 H | C17 H, C15 H, C18 H | C17 H, C15 H, C18 H |
| recall_conversation_v1/T24 | ∅ | C13 H, C14 H | C13 H, C14 H | C13 H, C14 H | C13 H, C14 H | C13 H, C14 H |
| recall_conversation_v2/U01 | D01 | D03, D30 | D01 | D01, D03 | D01, D03 | D01 |
| recall_conversation_v2/U02 | D07 | D07 | D07 | D07 | D07 | D07 |
| recall_conversation_v2/U03 | D08 | D08 | D08 | D08 | D08 | D08 |
| recall_conversation_v2/U04 | D04 | D04 | D04 | D04, D22 | D04, D22 | D04 |
| recall_conversation_v2/U05 | D06 | D06 | D06 | D06 | D06 | D06 |
| recall_conversation_v2/U06 | D15 | D15 | D15, D16 | D15, D16 | D15, D16 | D15, D16 |
| recall_conversation_v2/U07 | D09 | D09 | D09 | D09, D10, D27 | D09, D10, D27 | D09, D10, D27 |
| recall_conversation_v2/U08 | D13 | D13 | D13 | D13 | D13 | D13 |
| recall_conversation_v2/U09 | D11 | D11 | D11 | D11 | D11 | D11 |
| recall_conversation_v2/U10 | D12 | D12 | D12 | D12 | D12 | D12 |
| recall_conversation_v2/U11 | D10 | D10 | D10 | D10 | D10 | D10 |
| recall_conversation_v2/U12 | D02 | D02, D26 | D02, D26 | D02, D26 | D02, D26 | D02, D26 |
| recall_conversation_v2/U13 | D24 | D24 | D24 | D24 | D24 | D24 |
| recall_conversation_v2/U14 | D18 | D18 | D18 | D18 | D18 | D18 |
| recall_conversation_v2/U15 | D19 | ∅ | D19 | D19 | D19 | D19 |
| recall_conversation_v2/U16 | D25 | D25 | D25 | D25 | D25 | D25 |
| recall_conversation_v2/U17 | D28 | D28 | D28 | D28 | D28 | D28 |
| recall_conversation_v2/U18 | D21 | D21 | D21 | D21 | D21 | D21 |
| recall_conversation_v2/U19 | ∅ | D19 | D19 | D19 | D19 | D19 |
| recall_conversation_v2/U20 | ∅ | D15 | ∅ | ∅ | ∅ | ∅ |
| recall_conversation_v2/U21 | ∅ | D07 H, D17 H | D07 H, D17 H | D07 H, D17 H | D07 H, D17 H | D07 H, D17 H |
| recall_conversation_v2/U22 | ∅ | ∅ | ∅ | ∅ | ∅ | ∅ |
| recall_conversation_v2/U23 | ∅ | D29 | D29 | D29 | D29 | D29 |
| recall_conversation_v2/U24 | ∅ | ∅ | ∅ | D17 | ∅ | ∅ |
| recall_conversation_v3/V01 | F01 | F01, F17, F14 H, F26 H | F01, F14 H, F17 H, F26 H | F01, F14 H, F17 H, F26 H | F01, F14 H, F17 H, F26 H | F01, F14 H, F17 H, F26 H |
| recall_conversation_v3/V02 | F02 | F18, F02, F08 H, F07 H, F12 H | F18, F02, F08 H, F07 H, F12 H | F02, F08 H, F07 H, F12 H | F02, F08 H, F07 H, F12 H | F02, F08 H, F07 H, F12 H |
| recall_conversation_v3/V03 | F03 | F03 | F03 | F03 | F03 | F03 |
| recall_conversation_v3/V04 | F04 | F04 | F04 | F04 | F04 | F04 |
| recall_conversation_v3/V05 | F05 | F09 H, F07 H, F21 H | F09 H, F07 H, F21 H | F05, F25, F09 H, F07 H, F21 H | F05, F25, F09 H, F07 H, F21 H | F05, F25, F09 H, F07 H, F21 H |
| recall_conversation_v3/V06 | F06 | ∅ | ∅ | F06 | F06 | F06 |
| recall_conversation_v3/V07 | F07 | F03 H, F08 H, F14 H | F03 H, F08 H, F14 H | F07, F03 H, F08 H, F14 H | F07, F03 H, F08 H, F14 H | F07, F03 H, F08 H, F14 H |
| recall_conversation_v3/V08 | F08 | F08 H, F09 H, F12 H | F08 H, F09 H, F12 H | F08, F09 H, F12 H, F21 H | F08, F09 H, F12 H, F21 H | F08, F09 H, F12 H, F21 H |
| recall_conversation_v3/V09 | F09 | F09, F21 H | F09, F21 H | F09, F21 H | F09, F21 H | F09, F21 H |
| recall_conversation_v3/V10 | F10 | F08, F10, F03 H, F20 H, F12 H | F10, F03 H, F08 H, F20 H | F10, F03 H, F08 H, F20 H | F10, F03 H, F08 H, F20 H | F10, F03 H, F08 H, F20 H |
| recall_conversation_v3/V11 | F11 | F11, F07 H, F18 H, F24 H | F11, F07 H, F18 H, F24 H | F11, F23, F07 H, F18 H, F24 H | F11, F23, F07 H, F18 H, F24 H | F11, F23, F07 H, F18 H, F24 H |
| recall_conversation_v3/V12 | F12 | F12 | F12 | F12 | F12 | F12 |
| recall_conversation_v3/V13 | F13 | F13, F14 H, F17 H, F26 H | F14 H, F17 H, F26 H | F13, F14 H, F17 H, F26 H | F13, F14 H, F17 H, F26 H | F13, F14 H, F17 H, F26 H |
| recall_conversation_v3/V14 | F14 | F03 H, F20 H | F03 H, F20 H | F14, F03 H, F20 H | F14, F03 H, F20 H | F14, F03 H, F20 H |
| recall_conversation_v3/V15 | F15 | F09 H, F21 H | F09 H, F21 H | F15, F09 H, F21 H | F15, F09 H, F21 H | F15, F09 H, F21 H |
| recall_conversation_v3/V16 | F16 | ∅ | ∅ | F16 | F16 | F16 |
| recall_conversation_v3/V17 | ∅ | F17, F14 H, F26 H | F17, F14 H, F26 H | F14 H, F17 H, F26 H | F14 H, F17 H, F26 H | F14 H, F17 H, F26 H |
| recall_conversation_v3/V18 | ∅ | F03 H, F09 H, F20 H | F03 H, F09 H, F20 H | F07, F03 H, F09 H, F20 H | F07, F03 H, F09 H, F20 H | F07, F03 H, F09 H, F20 H |
| recall_conversation_v3/V19 | ∅ | F09 H, F21 H | F09 H, F21 H | F09, F21 H | F09, F21 H | F09, F21 H |
| recall_conversation_v3/V20 | ∅ | ∅ | ∅ | F04 | F04 | F04 |

#### 默认纯全文降级逐查询

| 语料／查询 | 标注 | main | latest_2 | session_6 | speaker_6 | adaptive_6 |
| --- | --- | --- | --- | --- | --- | --- |
| recall_conversation_v1/T01 | C01 | C01, C28, C24, C17, C13 H, C14 H | C01, C28, C24, C17, C13 H, C14 H | C01, C28, C24, C17, C13 H, C14 H | C01, C28, C24, C17, C13 H, C14 H | C01, C28, C24, C17, C13 H, C14 H |
| recall_conversation_v1/T02 | C02 | C02, C17, C12, C15, C22, C29, C08, C33 | C02, C17, C12, C15, C22, C29, C08, C33 | C02, C17, C12, C15, C22, C29, C08, C33 | C02, C17, C12, C15, C22, C29, C08, C33 | C02, C17, C12, C15, C22, C29, C08, C33 |
| recall_conversation_v1/T03 | C06 | C06, C04, C17, C19 | C06, C04, C17, C19 | C06, C04, C17, C19 | C06, C04, C17, C19 | C06, C04, C17, C19 |
| recall_conversation_v1/T04 | C30 | C30, C05, C03, C22, C15, C14, C17 H, C18 H | C30, C05, C03, C22, C15, C14, C17 H, C18 H | C30, C05, C03, C22, C15, C14, C17 H, C18 H | C30, C05, C03, C22, C15, C14, C17 H, C18 H | C30, C05, C03, C22, C15, C14, C17 H, C18 H |
| recall_conversation_v1/T05 | C04 | C22, C20, C18, C27, C16, C17, C11, C13 H | C22, C20, C18, C27, C16, C17, C11, C13 H | C22, C20, C18, C27, C16, C17, C11, C13 H | C22, C20, C18, C27, C16, C17, C11, C13 H | C22, C20, C18, C27, C16, C17, C11, C13 H |
| recall_conversation_v1/T06 | C07 | C17, C07, C15, C24, C02, C16, C21, C31 | C17, C07, C15, C24, C02, C16, C21, C31 | C17, C07, C15, C24, C02, C16, C21, C31 | C17, C07, C15, C24, C02, C16, C21, C31 | C17, C07, C15, C24, C02, C16, C21, C31 |
| recall_conversation_v1/T07 | C09, C10 | C21, C31, C17, C15 H, C13 H, C16 H | C21, C31, C17, C15 H, C13 H, C16 H | C21, C31, C17, C15 H, C13 H, C16 H | C21, C31, C17, C15 H, C13 H, C16 H | C21, C31, C17, C15 H, C13 H, C16 H |
| recall_conversation_v1/T08 | C11 | C11, C10, C12, C27, C22, C16, C17, C29 | C11, C10, C12, C27, C22, C16, C17, C29 | C11, C10, C12, C27, C22, C16, C17, C29 | C11, C10, C12, C27, C22, C16, C17, C29 | C11, C10, C12, C27, C22, C16, C17, C29 |
| recall_conversation_v1/T09 | C07 | ∅ | ∅ | ∅ | ∅ | ∅ |
| recall_conversation_v1/T10 | C27 | C27 | C27 | C27 | C27 | C27 |
| recall_conversation_v1/T11 | C10, C09 | C09, C10 | C09, C10 | C09, C10 | C09, C10 | C09, C10 |
| recall_conversation_v1/T12 | C24 | C17, C24, C05, C28, C13 H, C14 H, C18 H | C17, C24, C05, C28, C13 H, C14 H, C18 H | C17, C24, C05, C28, C13 H, C14 H, C18 H | C17, C24, C05, C28, C13 H, C14 H, C18 H | C17, C24, C05, C28, C13 H, C14 H, C18 H |
| recall_conversation_v1/T13 | C31 | C31, C12, C17, C22, C29, C15 H, C16 H | C31, C12, C17, C22, C29, C15 H, C16 H | C31, C12, C17, C22, C29, C15 H, C16 H | C31, C12, C17, C22, C29, C15 H, C16 H | C31, C12, C17, C22, C29, C15 H, C16 H |
| recall_conversation_v1/T14 | C22 | C22, C18, C32, C12, C02, C29 | C22, C18, C32, C12, C02, C29 | C22, C18, C32, C12, C02, C29 | C22, C18, C32, C12, C02, C29 | C22, C18, C32, C12, C02, C29 |
| recall_conversation_v1/T15 | C26 | C26, C20, C19, C12, C22, C29, C17 H, C15 H | C26, C20, C19, C12, C22, C29, C17 H, C15 H | C26, C20, C19, C12, C22, C29, C17 H, C15 H | C26, C20, C19, C12, C22, C29, C17 H, C15 H | C26, C20, C19, C12, C22, C29, C17 H, C15 H |
| recall_conversation_v1/T16 | C05 | C05, C13 H, C14 H | C05, C13 H, C14 H | C05, C13 H, C14 H | C05, C13 H, C14 H | C05, C13 H, C14 H |
| recall_conversation_v1/T17 | C32 | C32, C02, C22 | C32, C02, C22 | C32, C02, C22 | C32, C02, C22 | C32, C02, C22 |
| recall_conversation_v1/T18 | C29 | C03, C15, C30, C14, C17 H, C18 H, C16 H | C03, C15, C30, C14, C17 H, C18 H, C16 H | C03, C15, C30, C14, C17 H, C18 H, C16 H | C03, C15, C30, C14, C17 H, C18 H, C16 H | C03, C15, C30, C14, C17 H, C18 H, C16 H |
| recall_conversation_v1/T19 | ∅ | C26, C25, C19, C20, C15 H, C16 H | C26, C25, C19, C20, C15 H, C16 H | C26, C25, C19, C20, C15 H, C16 H | C26, C25, C19, C20, C15 H, C16 H | C26, C25, C19, C20, C15 H, C16 H |
| recall_conversation_v1/T20 | ∅ | C20, C26, C15, C19, C08, C33, C10, C17 H | C20, C26, C15, C19, C08, C33, C10, C17 H | C20, C26, C15, C19, C08, C33, C10, C17 H | C20, C26, C15, C19, C08, C33, C10, C17 H | C20, C26, C15, C19, C08, C33, C10, C17 H |
| recall_conversation_v1/T21 | ∅ | C06, C04, C02 | C06, C04, C02 | C06, C04, C02 | C06, C04, C02 | C06, C04, C02 |
| recall_conversation_v1/T22 | ∅ | ∅ | ∅ | ∅ | ∅ | ∅ |
| recall_conversation_v1/T23 | ∅ | C16, C25, C24, C17 H, C15 H, C18 H | C16, C25, C24, C17 H, C15 H, C18 H | C16, C25, C24, C17 H, C15 H, C18 H | C16, C25, C24, C17 H, C15 H, C18 H | C16, C25, C24, C17 H, C15 H, C18 H |
| recall_conversation_v1/T24 | ∅ | C13 H, C14 H | C13 H, C14 H | C13 H, C14 H | C13 H, C14 H | C13 H, C14 H |
| recall_conversation_v2/U01 | D01 | D03, D30 | D01, D18, D25, D05, D04, D28 | D01, D03, D24, D07, D10, D15, D16, D18 | D01, D03, D24, D07, D10, D15, D16, D18 | D01, D18, D28 |
| recall_conversation_v2/U02 | D07 | D07, D10, D14, D25, D08, D04, D21, D30 | D07, D14, D10, D08, D21 | D07, D10, D14, D23, D25, D08, D04, D21 | D07, D14, D10, D08, D21 | D07, D10, D14, D23, D25, D08, D04, D21 |
| recall_conversation_v2/U03 | D08 | D08, D15, D30, D24, D27, D05, D16, D21 | D08, D24, D21, D02 | D08, D29, D24, D05, D16, D21, D02, D12 | D08, D29, D24, D05, D16, D21, D02, D12 | D08, D21, D02 |
| recall_conversation_v2/U04 | D04 | D04, D18, D25, D23, D28 | D04, D18, D25, D28, D23, D30, D10 | D04, D18, D25, D23, D28, D30, D10 | D04, D18, D25, D23, D28, D30, D10 | D04, D18, D25, D28, D30, D10 |
| recall_conversation_v2/U05 | D06 | D06 | D06 | D06 | D06 | D06 |
| recall_conversation_v2/U06 | D15 | D15, D05 | D15, D16, D05, D25, D04 | D15, D16, D20, D18, D21, D05, D25, D04 | D15, D16, D20, D18, D21, D05, D25, D04 | D15, D16, D20, D18, D21, D05, D25, D04 |
| recall_conversation_v2/U07 | D09 | D09, D20, D29, D23, D18, D05, D16 | D09, D29 | D09, D20, D29, D23, D18 | D09, D20, D29, D18 | D09, D20, D29, D23, D18 |
| recall_conversation_v2/U08 | D13 | D13, D14 | D13 | D13 | D13 | D13 |
| recall_conversation_v2/U09 | D11 | D11, D15, D10, D08, D27, D02 | D11, D10, D27 | D11, D20, D24, D04, D10, D08, D27, D02 | D11, D20, D24, D04, D10, D08, D27, D02 | D11, D20, D24, D04, D10, D08, D27, D02 |
| recall_conversation_v2/U10 | D12 | D12, D29, D05, D23, D25, D19, D04 | D12, D29, D23, D19 | D12, D29, D05, D23, D25, D19, D04 | D12, D29, D23, D19 | D12, D23, D19 |
| recall_conversation_v2/U11 | D10 | D10, D27 | D10, D27 | D10, D27 | D10, D27 | D10, D27 |
| recall_conversation_v2/U12 | D02 | D02, D26 | D02, D26 | D02, D26 | D02, D26 | D02, D26 |
| recall_conversation_v2/U13 | D24 | D24, D22, D30, D07, D08, D29, D02, D12 | D24, D22, D30, D29, D12, D19 | D24, D22, D30, D07, D08, D29, D02, D12 | D24, D22, D30, D07, D08, D29, D02, D12 | D24, D30, D22 |
| recall_conversation_v2/U14 | D18 | D18, D01, D04, D26, D07, D15, D13, D10 | D18, D07, D04, D15, D01, D28 | D18, D01, D04, D26, D07, D15, D25, D17 | D18, D01, D04, D26, D07, D15, D25, D17 | D18, D04, D01, D15, D28 |
| recall_conversation_v2/U15 | D19 | D22 | D19, D29, D22, D13, D12, D21, D25 | D19, D29, D22, D13, D12, D21, D25 | D19, D29, D22, D13, D12, D21, D25 | D19, D29, D22, D13, D12, D21, D25 |
| recall_conversation_v2/U16 | D25 | D25, D15, D22, D18, D02, D05, D16, D04 | D25, D18, D15, D22, D04, D30, D10 | D25, D15, D18, D22, D05, D16, D04, D30 | D25, D15, D22, D18, D05, D16, D04, D30 | D25, D18, D22, D04, D30, D10 |
| recall_conversation_v2/U17 | D28 | D28, D10, D15, D25, D04, D23, D30 | D28, D15, D23, D10 | D28, D23, D01, D10, D07, D15, D08, D02 | D28, D23, D01, D10, D07, D15, D08, D02 | D28, D23, D01, D10, D07, D15, D08, D02 |
| recall_conversation_v2/U18 | D21 | D21, D29, D30, D14, D24, D27 | D21, D29, D24, D27 | D21, D29, D24, D27 | D21, D29, D24, D27 | D21, D29, D24, D27 |
| recall_conversation_v2/U19 | ∅ | D19, D24, D10, D08, D05, D16, D21 | D19, D08, D21 | D19, D24, D26, D08, D05, D16, D21 | D19, D24, D26, D08, D05, D16, D21 | D19 |
| recall_conversation_v2/U20 | ∅ | ∅ | ∅ | ∅ | ∅ | ∅ |
| recall_conversation_v2/U21 | ∅ | D07, D01, D12, D27, D30, D13, D21, D19 | D07, D30, D13, D21, D19, D17 H | D07, D30, D13, D21, D19, D02, D17 H | D07, D30, D13, D21, D19, D02, D17 H | D07, D30, D13, D21, D19, D02, D17 H |
| recall_conversation_v2/U22 | ∅ | D25, D04, D30, D29, D05, D10 | D25, D30, D04, D10 | D25, D04, D18, D29, D05, D30, D10 | D18, D25, D30, D04, D10 | D25, D04, D18, D29, D05, D30, D10 |
| recall_conversation_v2/U23 | ∅ | D29, D21, D14, D23, D02, D12, D25 | D29, D23, D02, D12, D21, D25 | D29, D21, D22, D23, D02, D12, D25 | D29, D21, D22, D23, D02, D12, D25 | D29, D21, D22, D23, D02, D12, D25 |
| recall_conversation_v2/U24 | ∅ | D07, D13, D26, D18, D10, D12, D23, D19 | D10 | D17, D23, D29, D13, D26, D18, D10, D12 | D13, D26, D18, D10, D12, D23, D19 | ∅ |
| recall_conversation_v3/V01 | F01 | F01, F04, F17, F05, F13, F06, F22, F21 | F01, F04, F13, F17, F22, F21, F16, F18 | F01, F22, F21, F16, F06, F18, F19, F04 | F01, F22, F21, F16, F06, F18, F19, F04 | F01, F22, F21, F16, F06, F18, F19, F04 |
| recall_conversation_v3/V02 | F02 | F18, F12, F08, F02, F24, F03, F04, F01 | F18, F08, F02, F03, F24, F12, F01, F26 | F08, F02, F19, F16, F06, F03, F04, F01 | F08, F02, F19, F16, F06, F03, F04, F01 | F08, F02, F19, F16, F06, F03, F04, F01 |
| recall_conversation_v3/V03 | F03 | F03 | F03 | F03 | F03 | F03 |
| recall_conversation_v3/V04 | F04 | F04, F06, F08, F20, F16, F01, F12, F18 | F04, F16, F06, F20, F08, F12, F01, F19 | F04, F16, F06, F20, F08, F12, F01, F19 | F04, F16, F06, F20, F08, F12, F01, F19 | F04, F16, F06, F08, F01, F19, F03 |
| recall_conversation_v3/V05 | F05 | F01, F13, F06, F10, F05, F17, F09 H, F07 H | F13, F09 H, F07 H, F21 H | F13, F05, F25, F10, F09 H, F07 H, F21 H | F13, F05, F25, F10, F09 H, F07 H, F21 H | F13, F05, F25, F10, F09 H, F07 H, F21 H |
| recall_conversation_v3/V06 | F06 | F26 | F26 | F06, F04, F19, F13, F16, F26, F22, F03 | F06, F04, F13, F19, F16, F26, F03, F01 | F06, F04, F19, F13, F16, F26, F22, F03 |
| recall_conversation_v3/V07 | F07 | F03 H, F08 H, F14 H | F03 H, F08 H, F14 H | F08, F07, F06, F03 H, F14 H, F20 H | F08, F07, F06, F03 H, F14 H, F20 H | F08, F07, F06, F03 H, F14 H, F20 H |
| recall_conversation_v3/V08 | F08 | F15, F13, F04, F01, F08 H, F09 H, F12 H | F08 H, F09 H, F12 H | F08, F04, F06, F22, F09 H, F12 H, F21 H | F08, F04, F06, F22, F09 H, F12 H, F21 H | F08, F04, F06, F22, F09 H, F12 H, F21 H |
| recall_conversation_v3/V09 | F09 | F09, F16, F22, F23, F06, F01, F18, F17 | F09, F16, F18, F21 H | F09, F16, F23, F06, F01, F18, F17, F21 H | F09, F16, F23, F06, F01, F18, F17, F21 H | F09, F16, F23, F06, F01, F18, F17, F21 H |
| recall_conversation_v3/V10 | F10 | F10, F22, F06, F17, F01, F03 H, F08 H, F20 H | F10, F06, F17, F01, F03 H, F08 H, F20 H | F10, F13, F01, F05, F06, F17, F15, F04 | F10, F13, F01, F05, F06, F17, F15, F04 | F10, F13, F01, F05, F06, F17, F15, F04 |
| recall_conversation_v3/V11 | F11 | F11, F16, F06, F23, F03, F07 H, F18 H, F24 H | F11, F23, F07 H, F18 H, F24 H | F11, F23, F07 H, F18 H, F24 H | F11, F23, F07 H, F18 H, F24 H | F11, F23, F07 H, F18 H, F24 H |
| recall_conversation_v3/V12 | F12 | F24, F12, F22, F03 | F24, F12, F03 | F12, F24, F06, F03, F15, F13, F04, F01 | F24, F12, F06, F03 | F12, F24, F06, F03, F15, F13, F04, F01 |
| recall_conversation_v3/V13 | F13 | F13, F18, F14 H, F17 H, F26 H | F14 H, F17 H, F26 H | F13, F18, F11, F14 H, F17 H, F26 H | F13, F18, F11, F14 H, F17 H, F26 H | F13, F18, F11, F14 H, F17 H, F26 H |
| recall_conversation_v3/V14 | F14 | F03 H, F20 H | F03 H, F20 H | F04, F14, F01, F06, F26, F03 H, F20 H | F04, F14, F01, F06, F26, F03 H, F20 H | F04, F14, F01, F06, F26, F03 H, F20 H |
| recall_conversation_v3/V15 | F15 | F09 H, F21 H | F09 H, F21 H | F06, F15, F02, F01, F17, F09 H, F21 H | F06, F15, F02, F01, F17, F09 H, F21 H | F06, F15, F02, F01, F17, F09 H, F21 H |
| recall_conversation_v3/V16 | F16 | F06, F10, F08, F04, F17, F01 | ∅ | F16, F06, F13, F18, F01, F17, F23, F11 | F16, F06, F13, F18, F01, F17, F23, F11 | F16, F06, F13, F18, F01, F17, F23, F11 |
| recall_conversation_v3/V17 | ∅ | F17, F04, F16, F06, F22, F15, F05, F01 | F17, F04, F22, F05, F16, F14 H, F26 H | F16, F04, F14 H, F17 H, F26 H | F16, F04, F14 H, F17 H, F26 H | F16, F04, F14 H, F17 H, F26 H |
| recall_conversation_v3/V18 | ∅ | F13, F04, F03 H, F09 H, F20 H | F03 H, F09 H, F20 H | F07, F11, F13, F03 H, F09 H, F20 H | F07, F11, F03 H, F09 H, F20 H | F07, F11, F13, F03 H, F09 H, F20 H |
| recall_conversation_v3/V19 | ∅ | F06, F01, F26, F09 H, F21 H | F01, F26, F09 H, F21 H | F06, F01, F04, F26, F09 H, F21 H | F06, F01, F04, F26, F09 H, F21 H | F06, F01, F04, F26, F09 H, F21 H |
| recall_conversation_v3/V20 | ∅ | F13, F10 | ∅ | F04, F16 | F04, F16 | F04, F16 |

### 验证与材料

确定性新测试先于实现运行：21 失败、20 通过；实现后对话准备与召回评测测试合计 51 通过。86 段公开学习语料 main / 本轮各 203 批、117 批材料非空、1322 条假模型记忆，学习请求不一致 0。没有修改 learning.py、learning_retrieval.py、trial.py、界面、迁移或学习提示词。

最初全量预取被自动审批拒绝，理由是可能发送超出三份对话集范围的文本。改为强制校验其他三集全部命中缓存，只有已指定的公开对话 dev 可以新发请求，复核后获准执行；没有绕过拒绝或读取隐藏集。预演复放本轮已有用户明确授权，获准执行。

完整材料在 `/private/tmp/iris-conversation-r2-20261008/`：comparison.json、逐候选查询组成、recall-embeddings.db、demo-replay.json、各截断副本和 learning-requests/。完整材料、执行日志和预演正文不提交。

### 两版本测试与构建

依次运行 `uv run pytest`：Python 3.13，573 passed，1 warning，78.39 秒；`uv run --locked --isolated --python 3.12 pytest`：573 passed，4 warnings，85.10 秒。警告为上游 Starlette/httpx 弃用和 jieba 转义。两个版本没有同时运行，没有重建工作区的 3.13 环境。`uv build` 成功生成 sdist 与 wheel。

测试和构建对应本分支：默认 latest_2 不变，三个冻结候选可通过既有构造参数复现。下面的 E2E/R10 为仓库外 adaptive-review 副本，只把默认常量换成 adaptive_6；它仍是未通过精确率守则的评审候选，不能称为本分支新默认或最终验收。副本源码 SHA-256：`2f7d06e4bfd234b56baf8b1c4069a66a6dc3e05dcbcc40377b75f2ddbc759bb1`。本分支源码 SHA-256：`b68c3bcdb5472abf427324c3e532b739a8ea49ee479a27660a80030df410a94d`。

### 候选的四个端到端脚本

命令为 `iris eval e2e --judge-mode external --script E001 --script E005 --script E007 --script E011`，以副本 src 为 PYTHONPATH，通过同一 Python 3.13 环境的 `uv run python -m iris.cli` 启动。GLM 5.3 flash、reasoning_effort=low、180 秒学习预算。每个脚本一次，不调用对话模型判分，不作学习质量或隐藏门槛结论。总时长 36.47 秒，8 个批次全部 succeeded、各一次尝试；四个检查点均无降级提示、recent_message_isolation 通过，排空成功。

| 脚本／检查点 | 记忆 ID（顺序） | reason | 完整返回正文 |
| --- | ---: | --- | --- |
| E001/music-after-restart | 1 | relevant | 我喜欢轻柔的纯音乐，尤其是钢琴独奏，工作时听着很舒服。 |
| E005/self-preference | 1 | relevant | Iris不喜欢恐怖片的突然惊吓，更喜欢自然纪录片，特别是海洋主题。 |
| E007/promise | 1 | relevant | 我答应在2026年10月9日（周五）帮南桥检查申请材料，重点看是否漏了附件以及日期有没有填错；检查即可，提交由南桥自己决定。 |
| E007/promise | 2 | relevant | 南桥计划周五（2026年10月9日）把申请材料发给Iris检查，并明确只需要检查，不用替其提交。 |
| E011/group-memory-in-dm | 3 | relevant | 我答应在星期天的河岸清洁活动时替许宁带一壶热茶，并请大家自备杯子。 |
| E011/group-memory-in-dm | 1 | relevant | 许宁通知，星期天的河岸清洁活动集合地点改为明早九点在社区东门，因桥头那边施工。 |
| E011/group-memory-in-dm | 2 | relevant | 顾雨表示在星期天的河岸清洁活动中负责带夹子和垃圾袋。 |

E011 三条记忆均来自 cleanup-group，近期消息全部属于 ning-dm。该检查只说明已返回的事实材料及入口隔离，不把这些返回当作本轮实现者的门槛判分。

### 候选的默认配置四组 R10

使用未修改的 `evals/benchmark_retrieval.py --default-config`，以 adaptive-review/src 为 PYTHONPATH；两版本测试和 E2E 均已结束。每组预热 5 次、采样 60 次，2048 维 float32，向量预生成，prepare 和 ASGI HTTP 路径均包含召回记录写入，不含外部 embedding 等待。

| 规模／点名 | prepare P50/P95 ms | HTTP P50/P95 ms | 向量 P50/P95 ms | 查询后 RSS MiB |
| --- | ---: | ---: | ---: | ---: |
| 5000/unnamed | 16.67/20.23 | 19.12/24.45 | 2.95/3.34 | 162.3 |
| 5000/named | 13.91/14.62 | 16.31/17.10 | 2.95/3.04 | 162.3 |
| 50000/unnamed | 110.25/127.08 | 113.39/138.18 | 29.30/31.35 | 501.5 |
| 50000/named | 104.32/121.10 | 106.38/120.98 | 29.59/31.46 | 501.5 |

四组 prepare P95 均 ≤500ms。5 千条开始/结束负载分别为 `[3.0166,3.1992,3.1968]` / `[3.0151,3.1958,3.1953]`，5 万条为 `[3.0151,3.1958,3.1953]` / `[3.7153,3.3467,3.2495]`。两个规模的日志各出现一次合成后台学习 `AttributeError`：基准假 Gateway 只有 embedding，没有学习调用所需的 json_chat；对应批次处于 waiting，记忆数仍严格为 5000 / 50000。保留这次全部样本，不过滤或挑选；这不是产品真实模型的学习失败，但说明性能脚本会启动与本测量无关的假学习任务。本轮按范围未修改基准脚本或调度器。
