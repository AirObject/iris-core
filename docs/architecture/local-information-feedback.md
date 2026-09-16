# 面向宿主的本地信息获取与使用反馈闭环

**状态：五组推荐及配置补充已获用户批准，授权文档定稿、整阶段实现、自查、测试和范围内修复。** 本文“推荐”表示本次采纳的方案；替代方案仅在明确采纳的TEST_HTTP隔离验证范围内获准，其余未采纳替代与范围外能力保持未批准。F档功能与资源验证已授权，P/X资源及负载尚待确认。批准不代表已实现、已验证或已验收；执行事实与停止点见[CURRENT_TASK](../work/CURRENT_TASK.md)，不授权提交。

静态核对基线：`main`，HEAD `f89cbbe773b119a7e683367410f17eda4538739a`，父提交 `6a41859fdfc8c0f80305e792221e19fdab2a9fa2`。首轮起草前工作区干净；本次定点补齐开始时已有5份文档差异、暂存区为空，全部保留。上一阶段验收证据及限制在[STATUS](../work/STATUS.md)；本轮检查与停止点在[CURRENT_TASK](../work/CURRENT_TASK.md)。

<a id="scope"></a>

## 1. 现行依据、范围与集中决定

产品依据：[召回与入口隔离](../product/retrieval.md)、[生命周期与反馈](../product/lifecycle.md)、[外部当前状态](../product/current-state.md)、[多目标](../product/goals.md)。模块所有权保持[retrieval](../modules/retrieval.md)、[state](../modules/state.md)、[goals](../modules/goals.md)及[统一依赖](ownership.md#source-line-267)。[request-paths](request-paths.md)仍是接口草图；本文提出其中本地路径的具体选择，不批准其远程增强示例。[实施顺序](implementation-options.md#source-line-1202)将本地闭环放在真实Provider和persona发布之前；[剩余产品决定](../product/decisions-and-delivery.md#section-22)中的相关事项通过下表集中处理。

已批准支持：本地中文词法检索与结构过滤、显式普通／深度模式、索引更新／重建、分区回复准备、持久召回凭据及反馈、外部状态、多目标管理、确定性去重与可见语义待办、持久提醒计划及明确投递结果、受控宿主HTTP和只读观察。查询同步路径不调用生成式LLM，也不生成persona、摘要、同义改写或临时目标建议。

继续继承[正式记忆／来源／媒体契约](formal-memory-source-media.md)和[批次／门控／恢复契约](durable-ingress-and-batch-runtime.md)：完整来源保护、原子提交、三终态、原键确认、实际资源结束后释放不变。本包不实现自动衰减、真实认知／梦境、persona生成发布、真实供应商适配、目标语义agent、目标执行、生产身份服务、配置热改、存量迁移、FAULTED解除或审计清理。自动删除采用下文候选的有界维护扫描，仍通过既有删除所有者和释放计划，不直接删表。

| 已批准决定组 | 推荐、影响 | 替代及代价 | 验收依据 |
| --- | --- | --- | --- |
| 本地检索与四项依赖 | §2–3：明确声明LOCAL_LEXICAL_V1；确定性中文字粒度索引，无查询模型；缺persona允许显式部分准备，语义去重及提醒接收能力单列。先交付可用本地召回，不以真实API为默认前置 | FTS5中文适配需另验构建／分词；真实向量＋rerank需实际适配器、空间、质量、预算及网络权限。要求完整persona或语义去重时相关能力保持不可用 | Q01–Q05、Q13 |
| 回复分区与一秒预算 | §4、§8、§10：典型规模P档的一秒包含排队到完整编码，最大词项压力X档另报性能、不承诺一秒；有界完整分区；缓存只取本入口正常S2／S1，其他入口只汇总数量／时间；要求全能力时缺任一必要分区失败 | 一律拒绝缺persona的准备更严格但减少本轮可用范围；更大返回／规模或并发必须重测并改选预算；不得暗读S3或他入口缓存 | Q06–Q08、Q15 |
| 反馈与生命周期 | §5：24小时凭据、严格匹配全部对象revision、每凭据每对象至多一次强化；增益8、F/H沿用20/35；连续遗忘30天后可删除，反馈与删除串行竞争；必要审计、凭据、收据同事务。10000票／24小时只支持低持续签票率：推荐工作负载≤0.08票/s；4/s×10分钟是需2400空槽的单次峰值，非持续服务承诺；16票/分钟清理及历史证据累积见§9.3，消费叶改1024字节 | 内容专用修订可容忍评分变更，但须新增修订语义和旧库方案；持续4票/s需至少345600活票及更快清理、另批磁盘与保留策略；缩短TTL会缩短可反馈窗口，扩大票池则增加存储；读取延长删除保护不推荐，会改变深读不恢复的边界 | Q09–Q12、Q16 |
| 状态、多目标与提醒 | §6：外部状态CAS、UTC时间、未知／陈旧明确；目标先加入再去重，仅精确相同约束自动合并，别名直接指主对象；提醒跨期合并一次，投递有限尽力，接收端缺失不假成功 | 语义自动合并需真实agent依赖；冲突自动择期限会丢外部约束；周期提醒／默认广播／自动延期均不推荐。仅按拉取显示是可选完整替代 | Q13–Q14、Q16 |
| 装配、容量与环境 | §7–10及[配置补充](configuration.md#local-information-configuration-draft)：新独立组合／新库；推荐仅新静态装配描述2.5 MiB、仓储131072、外层8192、合计2760704，静态载体硬限3 MiB；普通命令含初始化仍1 MiB。配置body总限294912，新增每项≤4096。按F/P/X三档分别批准资源和验收；Linux arm64优先，amd64资格单列 | 保留原794624与静态1 MiB须另做描述压缩方案并证明完整审计相容，目前不能推荐可用；扩大限额增加启动校验／恢复成本，旧装配与指纹不变。F档即可先验证功能，100 GiB／1 TiB仅P/X候选，不要求立即准备 | Q15–Q18 |

用户已一次性批准五组完整推荐包，并另已裁定增加goals初始化元数据表：保留四个实际写owner及四个必要slot，完整命令数仍为94。未采纳替代不自动获准，改变已批准保证仍须同步受影响参数、失败协议及验收并交用户决定。API模型约束在选择启用模型时继续有效，本地模式不实现另一条模型出口；尚未实现与待定细节不以代码现状冒充已决定。

<a id="baseline"></a>

## 2. 实际端口及依赖核对

本节仅对基线源码静态核对，不重新审查整个已验收阶段。

| 实际位置／端口 | 已有事实 | 本包必要增量 |
| --- | --- | --- |
| [memory/service.py](../../companion_memory/memory/service.py)：MemoryReadPort.get_current、get_for_deep_read、read_basis_status、list_dirty_dependencies | 有界按ID读取，句柄绑定明确对象集合；深读不强化；两次权限／门控核验 | 非全文搜索；新增受控候选枚举、批量末端核验与反馈参与端口，不能从索引ID自行扩大原对象授权 |
| [memory/repository.py](../../companion_memory/memory/repository.py)：index_dirty、dependency_dirty、basis_reverse | index_dirty按object_id合并，只有revision／UPSERT或REMOVE；依赖待办只读、没有索引消费者 | 新装配增加可恢复change_seq和受控当前对象／待办分页；保留依赖事件未消费状态，提供固定分页／CAS索引确认；现有revision不可当全库水位 |
| [memory/maintenance.py](../../companion_memory/memory/maintenance.py)、[release_plans.py](../../companion_memory/memory/release_plans.py) | 维护与删除已有expected_revision、释放计划和固定审计分支 | 反馈由memory同UoW参与；删除扫描只是受控触发，不绕过计划／最后来源释放 |
| [runtime/content_host.py](../../companion_memory/runtime/content_host.py)、[content_assembly.py](../../companion_memory/runtime/content_assembly.py) | 显式ContentHost、真实所有者；候选类型仍限定Synthetic输入，publication可缺；无retrieval／state／goals服务 | 新独立宿主组合；有界业务读取和提醒工作加入模式截点；不把可选publication等价于正式persona发布 |
| [content_gate.py](../../companion_memory/runtime/content_gate.py)、[content_modes.py](../../companion_memory/runtime/content_modes.py) | 发送准入与关门共用短锁、epoch；UNKNOWN不夺权 | 新回复交付及提醒最后发送仲裁；锁内不做数据库／网络I/O |
| [configuration/content_resolution.py](../../companion_memory/configuration/content_resolution.py)、[content_persistence.py](../../companion_memory/configuration/content_persistence.py)、[content_budget.py](../../companion_memory/runtime/content_budget.py) | 原配置精确匹配、4域／128条、版本2编码；静态完整装配受794624字节子预算和1 MiB基础硬限约束 | 新配置组合与独立域；增加总域数必须明确批准，不能把新键剔除后传旧解析器 |
| [management/http.py](../../companion_memory/management/http.py)：ReadOnlyHTTP | 只允许GET、无请求体；loopback临时端口；测试会话。现有runtime／entries／batches／memory／media／logs观察 | 不存在业务POST或Provider统计路由；另建受控业务适配器，旧观察能力不升级为写入权限 |
| [provider/ports.py](../../companion_memory/provider/ports.py)：WorkPort、ObserverPort、ResultOwnerPort | generate/embed/rerank/understand_media及lookup_request；query_usage/get_request/get_budget_state均有真实账本、模拟来源 | 页面只投影现有统计；查询本地模式零模型调用。模拟embed/rerank不得冒充真实语义 |

| 独立依赖 | 本轮推荐支持／缺失返回 | 推荐及替代 |
| --- | --- | --- |
| persona发布 | 基线无self_model服务、无受监管的真实发布读口；查询可用，prepare的persona区为UNAVAILABLE/PUBLICATION_MISSING，text/revision/generated_at均null。只有请求明确allow_partial且配置允许才返回DEGRADED；required时整体CAPABILITY_UNAVAILABLE | 预留只读已发布persona端口，核验实例、发布revision、监管状态与来源；真正接入另需[self-and-persona](../product/self-and-persona.md)发布契约。可用显式合成参与者测形状，但标SYNTHETIC且不能通过真实发布验收；不得把宿主自由文本当已发布persona |
| 真实embedding／rerank | Provider只有模拟适配器；本包LOCAL_LEXICAL_V1不申请向量、不调rerank，声明UNSUPPORTED_REAL_MODEL。请求REAL_HYBRID或rerank=true明确拒绝，不能自动改本地成功 | 后续真实API统一经Provider，持久向量绑定空间及正文指纹、查询缓存同空间；模型／维度／预处理变化重建，密钥或仅超时变化不重建。rerank可选另计耗时，末端核验仍必须；不得生成假向量填覆盖 |
| 目标语义判定 | 本地精确去重实际执行；语义相似候选标NEEDS_SEMANTIC_REVIEW，不能标“语义判定完成” | 推荐保留不同目标供宿主管理。接入真实agent后须有固定候选上限、冻结修订、Provider原请求恢复、明确合并证据和有限成本；本轮不授予自动语义合并 |
| 提醒接收端 | 无已装配真实接收端；推荐DISABLED明确建计划、到时记录UNSENT_UNAVAILABLE，目标持续可拉取；不记DELIVERED | 验证可显式启用TEST_HTTP并用真实loopback接收器证明传输／UNKNOWN；不证明生产宿主执行。生产需要稳定route绑定、认证、协议确认与地址策略，不能接受任意URL或广播 |

<a id="retrieval"></a>

## 3. 本地检索、索引与生命周期末端核验

### 3.1 中文能力和确定排序

推荐`LOCAL_LEXICAL_V1`以SQLite普通受限表／B-tree维护倒排，首包不依赖FTS5扩展或向量库。固定预处理版本：仅索引副本做Unicode NFKC＋casefold；按规范化后字符的Unicode类别L/N形成连续段；每段生成单字符及相邻双字符项，其他字符为边界。原权威正文不归一化。索引字符规则绑定解释器Unicode数据版本，换版本需显式重建。长英文亦用字粒度匹配，数字／中文混合可查；单字可查但更易触及候选上限。不自动繁简转换、纠错、同义词扩写或解释隐含语义。

索引内容仅当前正式对象的正文／关系断言、明确主体ID、类别、世界范围和已保存时间字段；禁止缓存、来源窗口、媒体原件、审计历史进入检索材料。主体标签只能来自已授权主体的当前登记，不推断同名同人。输入提供query_text及可空的subject_ids、category、world_scope、time_range、object_ids；所有过滤是AND，主体集合按ANY匹配，时间只匹配已知且相交范围，缺失时间不冒称匹配。至少一个非空文本或结构过滤；空全库枚举使用另行有界管理能力，不借查询暗开。

候选预算先取显式ID，再取结构过滤命中和倒排候选；每类按稳定object_id分页，读取posting总数和候选总数受配置硬限。排序用可解释元组：显式ID命中、结构匹配字段数、查询双字符覆盖率、单字符覆盖率、规范化完整短语命中、object_id升序；覆盖率按整数交叉乘比较，分母为去重查询项数，零分母为0。不把词法分数标成概率，不按相信程度过滤争议，不用低分隐藏真实状态。无文本的结构查询按匹配字段数／ID排序。

关联展开只走当前ACTIVE关系对象，RELATED／CONTEXT／CITES等返回时保留类型，最多一跳、最多配置新增候选数，不传播相信程度或同一主体结论。普通路径关系和端点都必须ACTIVE；深度路径可读取现存FORGOTTEN并单独标状态，但不因此取得其未授权端点。推荐首包展开上限为0，非零扩展为替代方案，不预先实现推理。

### 3.2 增量、重建与覆盖

为满足“新记忆先可按本地词法使用”，推荐在**新装配**中增加memory拥有的单调change_seq和固定当前对象／待办分页端口；对象提交只保存权威对象、修订及失效元信息，**索引更新仍在记忆提交之后**。新记忆在索引未就绪时，由查询的有界dirty覆盖层直接读取当前对象并确定性匹配词项；按ID／结构读取同样经memory。此路径不提供无界全库正文扫描。新元信息的事务／格式增量计入新装配，不更改旧命令描述。

retrieval拥有可重建的查询加速代次、倒排、清单、覆盖及工作。对象提交继续写index_dirty；新字段change_seq只给本实例本库定义提交顺序，不能用墙钟或对象局部revision比较全库。合并待办额外保存first_uncovered_seq和latest_change_seq，后续改版不抬高尚未覆盖的起点；仅所有目标代次对当前修订已确认索引化或移除后才能清除该对象的缺口。全局连续水位不得越过任何first_uncovered_seq，重建页未完成也构成缺口；不以待办最新seq覆盖旧缺口。增量worker按固定页获取待办，重读权威当前对象及revision，将更新和自己的覆盖证据保存，再由memory在同UoW仅在待办revision仍一致时确认；竞争产生新修订就保留待办。UPSERT已变删除时消费REMOVE，绝不复制旧正文。

加速索引滞后时合并memory当前对象的dirty覆盖层；覆盖层页有上限，超过上限可返回明确DEGRADED/INDEX_LAG_PARTIAL及candidate_exhausted，不能声称搜索完整。新对象按ID和结构查询始终可经权威路径访问；本地词法路径存在但大积压不保证一次查全。请求require_complete=true时覆盖不全返回INDEX_NOT_READY。覆盖清单含generation、format/preprocess版本、captured_change_seq、contiguous_applied_seq、pending_count、observed_at及缺口；不能以“最大已见seq”谎称中间全部覆盖。

`rebuild_local_index(key, expected_generation)`只由索引维护能力登记任务，业务返回已持久任务ID，不等待全库。重建登记与对象变更共用同库事务边界：登记后新变更待办绑定活动和构建两个目标代次，分别保存确认水位；旧worker不能因自己已应用就删除构建代次仍需的变更。登记前的对象由初始扫描覆盖，登记后的修改／删除由这些保留待办追平；不只依赖可能已被旧worker清空的单一dirty行。固定排序扫描权威当前对象到私有新代次并保存页检查点，扫描后追平截点变化；当前对象修订与dirty覆盖保证并发写不漏。发布事务核验所有页／清单／连续覆盖、当前截点与配置版本，原子替换active指针。未完成／失败旧代次继续服务；没有旧代次时走有界当前对象匹配并声明构建中或拒绝完整查询。最多一个重建者；不调用模型。

删除期间新旧代次都可能残留候选，但不可返回正文；旧代次待实际读者退出后按页清理。超时、进程崩溃、WAL回放或索引损坏不把半代次设active；恢复核对database_id、代次、格式及检查点，只有确定未提交步骤才能原键本地补齐。索引可重建不意味着凭据、反馈或已付费向量也可丢弃重算。

**发布事实（已批准）：** memory根的published_generation_id／published_seq首次初始化为null／0。index_publish仍为原固定命令、retrieval与memory两个实际writer；在同一UoW核验待发布代次、配置、页／清单、全部目标代次缺口和当前连续覆盖已追平memory.last_seq。memory保存实际generation_id及发布截点，根revision递增而last_seq不变；retrieval原子将旧代次设RETIRING、新代次设ACTIVE并切换Coordinator、结束构建租约。两owner必要审计、真实代次及Coordinator targets、结果绑定证据和原回执同事务提交。memory fact引用真实change_sequence根及前后revision，from_seq=to_seq=本次截点；retrieval fact引用实际发布代次及前后revision，同样保存该截点。后续所有对象变更保留发布字段，不因last_seq或覆盖推进自动改写published_seq。

空库真实发布为实际generation_id／0，不能将0一概解释成未发布。同代次不能换键再次发布；原键确认只读原结果。不同代次无新增变更时允许相同published_seq。完整根Schema拒绝缺字段／未知字段，512字节总限及全部原预算保持，不新增配置／表／命令。

恢复先完成owner本地核验，再通过公开owner端口交叉核验。持久化端口按实际提交顺序、每页至多16份读取该实例index_publish原回执并核验完整审计／结果绑定；逐次从真实成功回执计数。第k次发布的memory previous_revision必须等于该次发布截点＋k，revision为前者＋1；截点不倒退、与retrieval发布fact一致，代次／配置／状态和真实targets相符。最终根revision必须等于last_seq＋实际成功发布数＋1，根发布ID／截点与最后回执及活动指针一致；已发布代次数独立核对，不能从待验证根revision反推次数或仅统计剩余物理页。后续last_seq与代次覆盖可超过原发布截点，不能要求一直相等，也不能只靠大小关系证明历史发布。退役仅回收物理索引，不删除代次根、原发布回执／必要审计。全部核验通过才开放相关就绪能力；不一致按固定STORAGE_FAILED/INTEGRITY_FAILURE拒绝，不补写、迁移、清审计或解除FAULTED。

### 3.3 最后核验和发布界点

候选ID并非读取授权。排序后由memory在同一个有界权威事务快照核验授权范围、当前revision、正文、生命周期及来源元信息；遗忘普通候选／墓碑丢弃，旧revision用当前值重新算词法匹配和排序。有变化可在同一请求剩余预算内至多再做一次有限候选补充；不足K但候选已尽可正常返回，截断或截止导致未查完则明确降级／错误。

最终取值、凭据成员写入及必要审计在同UoW形成一个发布截点。返回对象revision就是凭据revision；不将查询早期正文与后期凭据拼接。编码在截点后只用不可变值，不再点读旧索引正文。模式关门／撤权／删除与响应开始的竞争另受§7的发布协议约束：不能把“查询开始时还有效”当最后有效性保证，也不承诺撤回网络上已交付的内容。

<a id="reply"></a>

## 4. 回复准备与返回分区

`prepare_reply`输入本次绑定入口、参与者ID、情境文本及检索选项；宿主自己编排context。`search_memory`只查ACTIVE，`deep_recall`需独立授权且可含FORGOTTEN；两者可显式附带目标／状态，不附缓存。成功结果采用版本化封套：request_id、recall_id或null、availability、observed_at、mode_epoch、config_snapshot_id、各区revision／observed_at、retrieval_mode、coverage、truncation、timing。分区是组合观察，不冒充跨入口全局实时快照。

| 分区 | 推荐可返回形状与边界 |
| --- | --- |
| persona | availability、text、revision、generated_at、review_status、origin；缺失按§2，不动态生成，旧发布标STALE／待复核 |
| memories | 每项完整当前对象快照、id/revision、lifecycle、belief及有限出处元信息；不含完整source、历史正文或媒体下载能力 |
| recent_context | scope=CURRENT_ENTRY_ONLY；正常S2目标候选和S1最新辅助的有限末尾，按entry_seq顺序、保留角色、message_id及时间。处理中的目标标FROZEN；不提供S3／失败留尾或未回流梦境正文；只读不会消费输入 |
| current_state | 最后报告状态、活动／子状态起点和更新时间、duration_basis、stale、来源；无报告为ABSENT，存储失败为UNAVAILABLE |
| goals | 多个未结束目标，未合并主ID、来源、期限／提前量、dedup状态、过期标记和固定动作建议；不生成行动计划或宣称完成 |
| other_pending | 仅其他待处理入口数、earliest_received_at、latest_received_at、observed_at、coverage；无入口ID、正文、摘要、人物、主题或hash |
| runtime | 本入口可见拒学缺口／终结失败、模式／积压新鲜度、能力来源和固定原因；不包含其他入口内容 |

pending计数由buffers拥有的索引／计数投影产生：正常未作为目标终结的S2／S1与尚未回流暂存算待处理；已成功／失败／拒学目标及S3不算，移交时同一输入不重复计数。仅覆盖系统已持久接收的范围，不推断平台断线期间零消息。计数不可读不填0。即使拥有多个入口缓存能力，一次prepare也只绑定一个入口；跨入口正式记忆共享沿既有产品边界，不宣称额外披露隔离。

预算不足时按固定分区预算选完整对象／完整事件，返回has_more、omitted_count（可知时）及原因；不在JSON字段内截正文或将部分source标完整。persona／当前状态单区放不下时明确UNAVAILABLE/LIMIT_EXCEEDED；required区失败使整体失败。memories不超过K是接口选择上限，实际候选未遍历完、缓存／目标被预算省略须明确标记。查询词法质量局限常驻能力声明，不把合法零命中等价于故障。

<a id="feedback"></a>

## 5. 持久凭据、反馈与删除竞争

### 5.1 凭据及有效期

retrieval拥有`recall_ticket`：version、recall_id、database_id、instance_id、principal_binding_id、host_id、entry_id、query_mode=NORMAL/DEEP、config_snapshot_id、issued_at_us、expires_at_us、clock_observation、response_digest、intent_digest、request_key、member_count及不可变成员叶。response_digest只覆盖固定分区内容及绑定元信息，不含自身、提交后timing或HTTP封套，避免对尚未知的提交耗时求摘要。成员叶包含recall_id、object_id、returned_revision、returned_lifecycle；根不保存查询原文或完整响应。ID不可猜测但不代替授权；实例／宿主／入口／主体绑定都必须匹配，不能转交另一个宿主强化。零正式记忆结果不签发空凭据，recall_id=null。

每次查询须提供稳定request_key，绑定宿主／入口及完整规范化查询意图摘要；签发命令据此取得幂等身份。相同key异意图拒绝；同key已签发时只返回CONFIRMED_ONLY及原票据安全元信息，不重返旧正文、不再签票，宿主要新观察须明确发起新key。CONFIRMED_ONLY不计作本次查询成功；原键未确认时只走resolve_recall，不因查不到记录立即换键自动再查。摘要不替代绑定权限，恢复所需原意图由宿主重交并核验。

推荐有效窗口为`issued_at_us ≤ server_now_us < expires_at_us`，固定24小时；从最后取值事务的受控UTC观察时刻起计，跨重启不重新起算。宿主used_at可记录但不能延长窗口，不能凭宿主自报时间接受过期反馈。墙钟回退到已保存的时钟高水位之前时，新反馈返回CLOCK_UNCERTAIN，既有已提交确认仍可读取；重启不比较旧进程单调时钟。自然时钟修复后重新核验，不自动改截止。

凭据只证明系统曾承诺交付这些版本，不证明网络到达或宿主真的用了；后者来自经过授权的显式反馈。必须先持久COMMITTED再交付带凭据的结果。持久失败没有正文成功响应；UNCONFIRMED只返回安全确认身份，不返回未确认正文。HTTP连接中断可能留下未使用凭据，不能因此自动计分。`resolve_recall`只给原确认与成员元信息，不重放旧正文；需要当前正文重新查询并核生命周期。

推荐票池上限同时约束“有效票＋已过期但有效负载尚未安全处置的票”；签发在同事务预占槽，清理完成才能释放槽，不能只按expires_at过滤后无限积存。推荐有效负载到期后有界清理，保留无正文处置标识、操作回执、已用成员唯一键及必要审计；未决事务／恢复引用未结束不得清。空间不会在24小时后归零，持久收据／审计的累积见§9；本包无自动清除幂等证据的保留政策。每分钟至多处置16根及其每根至多8成员，逐票短事务；未决恢复引用跳过并保留占槽，以持久游标轮转，不能被页首未决票永久阻挡其他可清票。每次轮转最多检查4×cleanup_page_size＝64根，未获足够合格票时实际释放率可以低于16/分钟；暂停／失败不补做无界清理。消费叶改为1024字节以容纳全部绑定身份，唯一键不缩短。

### 5.2 反馈原子协议

`record_usage(operation_key, recall_id, used_members)`由外部usage.report能力发起，used_members为1至K项object_id＋returned_revision，不能反馈缓存、persona、状态、目标、未返回对象或同一请求内重复ID。memory拥有强化收据，retrieval仅在同UoW核票及记录成员消费，应用协调者唯一commit。

步骤：权限及格式→原操作键事实／冲突→新工作模式→同事务查票、有效期、成员和当前对象→按静态分支写全部效果、审计及回执。任一成员越权、过期、被删或修订不匹配，整次拒绝，无部分加分。推荐严格比对**全部revision**（现有评分变化也加revision）；不新增content_revision猜测正文相同。因而别的反馈或维护改变分数后旧票也可能失效；宿主需重新召回后反馈，不能让服务自动换版本。

唯一消费键为`database_id + principal_binding_id + recall_id + object_id`，防同一实际使用换operation_key重复加分；同键同内容返回原回执，异内容IDEMPOTENCY_CONFLICT。新操作键再次报告已用成员时，先识别原消费并返回ALREADY_APPLIED引用，不能因为原强化已加revision而先报冲突；混合已用／未用成员时，仅未用成员需当前修订核验并一次提交，结果逐项标明。所有成员均已用时只读返回原效果关联，不写虚假变更审计。不同凭据代表不同使用机会，可各强化一次，但仍须通过严格修订。

`R_new=min(100,R_old+gain)`；belief不变。沿既有F/H比较符号执行遗忘／恢复，真实FORGOTTEN→ACTIVE才清forgotten_since并写RESTORED依赖待办；低于H仍遗忘，不恢复整个关系网。memory在新Schema的独立使用元信息中记录last_used_at（未用时null），其变更参与同一对象revision；旧对象正文格式不添加隐含字段。取值为本次服务端接受时刻与已有值的较大者，保留宿主报告时间的非权威身份；有实际分数／时间／状态变化时revision加1、index_dirty推进；饱和且同时间无对象变化也须保存首次消费收据，但不虚报memory对象修订。旧正文历史／审计参与遵守[历史正文边界](logging.md#memory-history-audit)，不在票据存第二份正文。

恢复影响待办继续由既有memory拥有，不由反馈模块消费为已修复。新反馈提交含retrieval消费、memory收据及实际对象变更、必要日志slot；静态分支区分仅消费／有评分变化／有恢复，不声明可选必要审计。COMMIT后确认丢失从原键、票据和稳定输入确认，不重新强化；NOT_FOUND观察、超时或writer未结束不能成为重新执行许可。

### 5.3 连续遗忘与删除

推荐新增受控维护扫描：按`forgotten_since_us, object_id`有界分页，连续遗忘达到配置期限后提交既有delete_object语义；无自动衰减任务，不将定期扫描声称完整生命周期自动化。扫描在NORMAL／DRAINING运行，专注期普通扫描暂停，内部梦境已有维护权限不扩大。

删除与反馈共用权威对象修订和同库写串行边界。删除先提交，迟到票无论是否仍在24小时内都拒绝OBJECT_DELETED；反馈先提交且恢复，旧删除计划因revision／生命周期改变而失败，新的扫描不能沿用旧forgotten_since。反馈先提交但未恢复，原连续遗忘起点不变，仍可被新合法计划删除。达到期限仅表示删除资格；实际删除前仍可接受有效反馈，是否胜出由事务顺序决定。深读和发票不延长删除期、不建立source／blob保护；不为在途票保留对象。

删除后凭据和旧回执不复活原ID，原ID永不重用。反馈与删除确认均可返回历史安全结果，当前状态另查，不能把“当时恢复成功”表述为“现在仍存在”。

<a id="state-goals"></a>

## 6. 外部当前状态、多目标与提醒

### 6.1 当前状态

state唯一写者为绑定外部管理能力。记录包含instance、activity_id、revision、activity_value、fields、started_at、first_reported_at、reported_at、received_at、ended_at、last_host_id／entry_id。fields是封闭业务字段`scene, progress, emotion`，每项有value、started_at或null、first_reported_at、updated_at；不同子状态分别计时，未填为空／未知，不分拆人格。

传输时间接受明确UTC偏移的RFC3339、至多6位小数，拒绝无时区时间／闰秒；存储为UTC整数微秒并保留原偏移分钟。推荐未来时钟容差60秒，超过时拒绝INVALID_TIME；被容差接受的未来起点duration取0并标CLOCK_AHEAD，不伪造已持续负数。持续时间按观察时刻减可靠started_at；无实际起点按first_reported_at且basis=FIRST_REPORT；两者无可靠证据时UNKNOWN。reported_at是宿主报告时间，received_at是本次服务端确认观察，不用于偷换活动起点。

`set_state`创建新activity_id；已有活动须expected_revision且显式replace_activity=true结束旧包后原子更换。`update_state`只更新同活动，心跳不重置起点；子状态值变更重新建立该字段首次报告／显式起点，同值仅更新时间。显式null清除字段，其余未列字段保持。`end_activity`按activity_id及revision结束；结束后当前活动区为空并带最后结束元信息，不继续累加持续时间。可靠结束早于起点拒绝。每次有效更新revision递增；无变化可返回NO_CHANGE。

推荐以服务端最后接收时间间隔判陈旧；stale只作标识，不结束活动、不改情绪。重启恢复原时间，专注期拒绝不排队；不能从聊天、回流或历史日志恢复“现在”。首包一实例一绑定写宿主，多个入口保留来源；第二写宿主返回ACCESS_DENIED，未定义跨宿主自动仲裁。

### 6.2 目标对象与去重任务

goals拥有目标、来源叶、别名、去重任务、提醒计划和投递事实。目标视图字段：goal_id、revision、content、subject_ids、world_scope、status=OPEN/COMPLETED/ABANDONED、deadline或null、reminder_lead_seconds或null、route_id或null、created_at／updated_at、source_refs、dedup_state、canonical_id。过期是OPEN且now≥deadline，不是互斥业务状态。无deadline时lead和route可null，不产生虚构到期；有deadline时lead必须外部或可信内部候选显式提供，route必须绑定已声明宿主，即使投递能力DISABLED也保留目的归属。

`inject_goal`一次短事务保存目标、完整输入、稳定dedup任务和提醒计划后确认，返回goal_id与dedup_pending=true；不等候模型。内部create_internal_goal只接可信工作提交的已成形目标及真实依据，普通宿主不可自报internal来源，基线合成认知含目标效果仍不能丢弃该效果后成功。更新status／deadline／lead均需expected_revision、原键与必要审计；未结束目标可以修改，结束目标不重开，若需新目标用新ID明确建立。完成／放弃原子取消未发提醒，不自动执行任务。

可信内部独立入口在原生装配时冻结完整目标意图和原键，并另绑定memory签发的有限读取能力。source_id必须指向cognition真实已提交、已处置的成功候选，basis_id必须在该候选的真实对象效果中；同一目标UoW由cognition核验原manifest／配置／出处，由memory核验当前有效依据、对应保留来源及主体读取权限，再由goals唯一写入。普通management权限不能代替这份工作绑定，HTTP无内部来源自报路径；既有原键确认不重新要求被删除的历史依据仍存在。基线参与者继续明确标记SIMULATED模型／SYNTHETIC候选，不由此宣称真实目标生成。候选自身携带的目标效果仍按§9.2.1与原正式记忆一起提交，不能改走此独立入口。

本地去重worker对新目标执行一次有界候选检查。精确自动合并条件为规范化content、排序主体集合、world_scope、deadline、lead、route和OPEN状态全部相同；原content仍保留，来源归因不丢。不同对象、世界、截止或路由均保持并行。候选按创建顺序＋ID选最早主对象；在同UoW重查双方revision／状态／所有约束，合并来源、别名、任务及未发计划。来源总量超限则NEEDS_REVIEW/SOURCE_LIMIT，不截断归因。近似候选只记录NEEDS_SEMANTIC_REVIEW，基线无agent就不能标判断完成。

旧ID形成持久直接别名指向canonical主ID，禁止环和多跳；主对象一旦承接别名不再迁往另一个主对象，本包该类后续冲突保留并标人工复核，避免一次重写无界别名。状态更新旧ID先解析主ID，返回canonical_id及其当前revision，调用方必须显式按该revision更新，不能把旧ID旧revision自动套用。目标业务状态不含MERGED，别名不等于现实完成。每个主对象来源／别名上限固定，超过保留独立目标，不静默丢弃。

去重任务从PENDING→RUNNING→EXACT_MERGED／DISTINCT／NEEDS_SEMANTIC_REVIEW／FAILED；RUNNING用唯一owner和冻结候选修订，进程恢复只继续未完成本地判断。普通失败没有无限重试；未知提交原键确认。首次精确检查前提醒保持去重等待，超过检查等待期限则显式DEDUP_UNRESOLVED并按独立目标推进计划，避免永久静默。没有语义合并保证时可能多条提醒，获取结果必须可见此限制。

目标只读页按created_at、goal_id稳定排序；每页至多8项，同时按§9.2.4完整编码预算保留整个目标及其全部来源。恰好8项不自动表示has_more，须检查后续真实主对象；返回items、has_more、omitted_count（总数不可知时null）、next_cursor及observed_at。可选cursor由前一页next_cursor原样回传，最长160字节，只是有界排序位置，不授予读取权限；原生list_open_goals与GET /api/host/goals?cursor=…共用同一所有者分页。拒绝重复／未知参数或损坏游标，不截断单个来源或正文来凑页。

### 6.3 提醒时序、发送与未知

每个deadline_revision至多临近、到期两份计划，键为goal_id＋deadline_revision＋kind；修改deadline或lead原子取消未发旧计划、生成新计划。lead≥0，lead=0时只保留到期；lead大于距截止的剩余时长时临近计划可立即到期。创建已过期、截止改到过去、去重等待或专注跨过两个时刻：只派当前仍需的一次到期提示并将被覆盖临近计划标SUPERSEDED；若仅跨过临近而尚未到期，可派一次临近。没有周期性补发。

意图负载仅delivery_id、canonical_goal_id、revision、kind、deadline、observed_at及固定建议`CONSIDER_ABANDON_OR_CHANGE_DEADLINE`或`UPCOMING`，不带完整目标正文、自由URL、模型输出或自动延期命令。route来自可信宿主绑定；宿主再经授权目标查询取得内容。合并时同deadline／route的计划去重；已经发出的事实不能撤回或谎称从未发送。

推荐默认DISABLED：计划到时持久终结UNSENT_UNAVAILABLE，查询与观察明确“接收端未装配”；不回报DELIVERED，不让恢复扫描因路由后来可用补发。TEST_HTTP为显式替代测试包，接收器必须真实监听、回显匹配delivery_id的有限确认才记ACKNOWLEDGED；仅表示接收而非目标执行成功。

发送前先持久登记尝试意图并确认，再在与模式关闭／目标取消共用的短串行区重查许可并开始一次有界HTTP。有限传输尝试只允许连接层在能证明没有发送时重试；可能已发送、超时或进程中断为UNKNOWN，不能重发以猜结果。没有可靠外部查询协议时UNKNOWN持久保留，重启只本地核对，绝不发送。关闭前未开始的计划可按当前时序重算一次；开始后的计划纳入既有收尾截点，有限期限不证明底层结束。

取消先获串行区则零发送；发送先开始则完成原尝试并保留结果，目标仍可随后完成／放弃，不能承诺撤回在途提醒。专注期不投递、不顺延deadline，也不把普通消息当作目标管理命令重放。

<a id="interfaces"></a>

## 7. 受控接口、门控、失败与观察

所有名称／路径均是已批准语义签名；新HTTP与新原生句柄共用用例服务，不通过HTTP获得裸SQL、Provider工作口或完整来源。业务身份由可信装配绑定instance／principal／host／entry及操作集合，request字段不能签发权限。测试只监听loopback，随机有期Bearer凭据只在请求头，不写URL、页面或日志；生产鉴权另待批准。

| 原生能力／候选HTTP | 输入与效果 | 正常结果 |
| --- | --- | --- |
| prepare_reply／POST /api/host/prepare | 固定入口＋情境＋查询选项；只持久召回凭据，不强化 | COMPLETE／DEGRADED分区 |
| search_memory、deep_recall／POST /api/host/memory/search、/deep-recall | 普通与独立深读权限；固定结构过滤 | 结果及凭据或明确零命中 |
| record_usage／POST /api/host/usage | 原键、recall_id、成员；§5原子强化 | 持久回执、逐项结果 |
| resolve_recall、resolve_usage／POST /api/host/recalls/resolve、/usage/resolve | 原绑定、原键和原安全意图；只确认 | 原回执元信息／未确认，不重返旧正文 |
| set_state、update_state、end_activity、get_state_view／POST /api/host/state/set、/update、/end；GET /api/host/state | 类型化活动操作；写用CAS和原键 | COMMITTED或当前有时点视图 |
| inject_goal、update_goal_status、change_deadline、list_open_goals／POST /api/host/goals/inject、/status、/deadline；GET /api/host/goals | 显式来源／期限／绑定route；多目标固定分页 | 回执、canonical_id或目标页 |
| resolve_management／POST /api/host/operations/resolve | 原状态／目标命令种类、原键／意图，只读 | 原COMMITTED证据或未知，无代执行 |
| update_index、rebuild_local_index、scan_expired_memories、dispatch_due_intent | 仅可信索引／维护／运行能力，无普通宿主HTTP | 持久任务／页回执／明确阻止 |
| create_internal_goal | 仅§6.2的原生可信成形工作，固定原键／完整意图与独立依据能力；无普通宿主HTTP | goals单writer回执；原键通过resolve_management只读确认 |
| 只读观察／GET /api/observe/retrieval、/state、/goals、/provider/usage、/provider/requests、/provider/budget | 固定分页／过滤，独立scope与账户汇总授权 | 脱敏计数、覆盖、时点、状态及现有账本统计 |

除原键只读确认和受控观察外，全部新业务读写在DREAM_PREPARING／DREAM_FOCUSED拒绝DREAMING，在RECOVERING／FAULTED拒绝对应原因；DRAINING只在既有发布确认开放点允许。未实现业务在专注期仍优先DREAMING，不以CAPABILITY_UNAVAILABLE遮住门控。只读观察不取得目标／状态正文、查询词、票据秘密或完整审计。Provider query_usage保持原有有界全集／超限失败语义，不虚构其不存在的游标；页面超限要求缩窄过滤。模拟usage、已知／估算／未知费用、未发送／可能发送保持原分类，不从日志另算账。

**发布与写入竞争：** 请求准入取得绑定epoch的有限发布工作；最终取值／票据事务前再次核验。回复编码完成后，短串行区重查mode_epoch、当前授权和相关对象变更屏障，再调用有界“开始交付”回调。对象删除／修订及关门在其事务前关闭相关新交付，未确认保持关闭；先关门则丢弃未交付编码结果并明确拒绝，票据可留为未使用。若交付先开始，则承诺内容在该截点有效，随后变更不能撤回已送字节。不能锁内等SQLite／socket，也不承诺客户端收到最后字节时对象仍未被修改。状态和目标区各带自己的观察修订，不声明跨区同一全局事务。

新业务写在准入后、commit前与关门串行核验；关门先发生不新写，事务已获提交许可则纳入在途截点，只作本地收尾，不新增工作。未决写／资源不能在入梦时被忽略。HTTP断线不撤销已提交回执，也不因重发POST绕过原键。

写操作复用COMMITTED／NOT_COMMITTED／REJECTED／UNCONFIRMED四分支；只读用FOUND／ABSENT／FAILED，业务响应另有COMPLETE／DEGRADED。UNCONFIRMED并非已接受异步业务，返回安全operation_key／确认类型，禁止自动换键重试。HTTP建议COMMITTED／成功读200，未知确认202且body明确UNCONFIRMED，格式400、未认证401、无权403、有效期或修订冲突409、超大输入413、饱和429、能力／存储／模式503、截止504；业务ABSENT在200内显式表达，不与鉴权泄露混合。

新增领域错误固定`code, operation, field, reason, cleanup_pending`，不扩大旧错误类。operation限本节公开语义，field限capability/input/query/ticket/member/revision/state/goal/route/index/configuration/storage/time。封闭原因组：INVALID_INPUT（INVALID_SHAPE／INVALID_TIME／LIMIT_EXCEEDED／UNSUPPORTED_VERSION）；ACCESS_DENIED（BINDING_MISMATCH／OPERATION_NOT_GRANTED）；IDEMPOTENCY_CONFLICT（CONTENT_MISMATCH）；PRECONDITION_FAILED（REVISION_CONFLICT／TICKET_EXPIRED／MEMBER_NOT_RETURNED／OBJECT_DELETED／CLOCK_UNCERTAIN／NO_CHANGE）；CAPABILITY_UNAVAILABLE（PUBLICATION_MISSING／REAL_MODEL_UNAVAILABLE／SEMANTIC_REVIEW_UNAVAILABLE／REMINDER_SINK_UNAVAILABLE／INDEX_NOT_READY）；MODE_BLOCKED（DREAMING／RECOVERING／RUNTIME_FAULTED）；RESOURCE_BUSY（ADMISSION_FULL／LOCK_BUSY／CAPACITY_REACHED）；STORAGE_FAILED（READ_FAILED／WRITE_NOT_COMMITTED／COMMIT_UNCONFIRMED／INTEGRITY_FAILURE／FORMAT_UNSUPPORTED）；TIMEOUT（DEADLINE_EXCEEDED）；CONFIGURATION_UNSUPPORTED（DEFINITION_MISMATCH／CAPACITY_INSUFFICIENT／FORMAT_BINDING_MISMATCH）；INVALID_STATE（NOT_READY／SERVICE_CLOSED）。

先认证／能力，再生命周期、精确载体／格式硬限、原键，最后新模式／前置与额度。降级原因仅PUBLICATION_MISSING／INDEX_BUILDING／INDEX_LAG_PARTIAL／CANDIDATE_LIMIT／SECTION_LIMIT／INDEX_FORMAT_LIMIT；数据库读失败、凭据未知、必需能力缺失和无法完成末端核验均不是降级成功。首错保留，回滚不明提升UNCONFIRMED，实际占用保留cleanup_pending；COMMITTED不因后续诊断或close失败撤销。

必要审计由日志所有者固定Schema追加：召回签发／消费／到期处置、反馈及真实恢复、状态更换／更新／结束、目标创建／管理／别名合并、去重终结、提醒计划／尝试／终态、索引任务／页确认／发布。各slot只含有界内部ID、前后revision／枚举、计数与时点；查询正文、完整票据、缓存、外部自由时间文本／错误不入诊断。索引词项本身不做审计正文；对象旧历史仍归原专属历史接口。无实际变更不伪造slot；涉及哪个owner就选择预声明静态分支，不能动态免审计。审计失败整个UoW失败；恢复与OPEN_EXISTING验证新结果绑定关联。

<a id="latency"></a>

## 8. 一秒目标及成功分类

以下为**已批准验证目标，非实测保证**。基础总预算1000ms：从HTTP收齐有界请求头及body的当刻起（不等待调度或鉴权后才起表），到完整响应体序列化并准备首次写出为止；含认证、内部排队、配置／模式读取、本地检索、所有者读取、末端核验、凭据与必要审计持久提交。上传／网络到达该界点之前和最后socket排空另报告；同时测真实HTTP客户端首发至完整响应的端到端耗时，不能用排除网络来掩盖服务端慢消费。解析前读取也有独立头／体期限，超时归错误。

推荐内部分配：排队／认证100、候选300、分区读取150、末端事务／凭据300、编码／交付检查150ms，总1000。这是共享绝对deadline的分配参考，可借未用余量，不能各步骤重置；底层现有2秒memory读／30秒storage等待必须支持取本次剩余更小值的受控增量，外层wait_for不足以保证底层释放。资源未结束即使返回504仍占槽；这些超时不能计作一秒成功。

可选rerank在现行草图另计耗时，本推荐包关闭且请求启用拒绝；未来启用要同时报告base_ms、rerank_ms、total_ms、HTTP端到端，远程query embedding仍包含base，不可挪给rerank。调用生成式LLM次数必须为0；本地包所有Provider查询增强次数也为0。

测试剖面：一实例、同一平台3入口、最多100000现存正式对象（其中10000遗忘）、每对象完整快照≤4096字节／正文≤2048原UTF-8、1000未结束目标、每入口1000正常未处理输入；凭据容量边界另测10000占槽；有签票的性能测试须明确初始占槽≤7600，不在满池上把429算作召回吞吐。对象数、目标数是本候选准入上限，输入积压1000是测试剖面，**不限制专注暂存或淘汰已确认输入**。三入口仍只一个平台配置域，不将原单平台包放宽为多平台。

验收按§10三档事先固定：F档小规模功能／恢复，P档100000对象、每对象128词项的典型性能，X档100000对象、每对象4096词项的最大压力。P档另含空库及10000对象参照；满尺寸编码另测，不能混称所有对象都满词项。热索引、冷进程／冷缓存标明，重建中另报。并发1与2，查询4请求/秒持续10分钟（2400请求）；同时1次状态或目标写/秒与1个索引worker，定期穿插反馈／删除／模式竞争。实际接入峰值未知，不由历史每日输入平均值推断。另测超过2并发的429与超限请求；不将拒绝当成功吞吐。

P档稳定热索引的每条成功base≤1000ms；冷启动、重建与X档分别记录截止／降级分布，不承诺X档一秒达标。此处支持剖面修订已随第二／第五决定组批准，不能事后降低已批准验收。分别报告COMPLETE、DEGRADED、合法零命中、DREAMING拒绝、429、其他错误、504、UNCONFIRMED的数量和比例，以及成功p50/p95/p99/max和端到端分布。稳定全能力夹具下推荐COMPLETE≥99%，包括persona缺失的推荐包单独报告预期DEGRADED，不用它混入完整成功率；任何超deadline响应单列违约。无法达到则提交实测规模／原因并改选参数或支持剖面，不能事后删慢样本。当前无真实persona，完整准备的性能只能用明确标记的发布夹具验证工程开销，不能宣称真实persona能力完成。

<a id="capacity"></a>

## 9. 参数、容量、所有权与兼容

### 9.1 配置和固定格式

全部可调参数及完整显式组合唯一在[配置补充](configuration.md#local-information-configuration-draft)。新增8个封闭object参数是有明确定义的配置记录，不是任意kwargs／可执行策略。新独立组合解析／持久化／加载端口，统一configuration签发真实身份；新information域与原foundation/runtime/content/platform共5域，旧4域解析器／持久记录不变。每次请求、凭据、索引工作、目标去重／提醒冻结配置身份；无热编辑、不在恢复时换当前值。

新格式JSON继续严格UTF-8、确定编码、拒绝重复键／非有限数／未知字段／bool冒充int；ID沿现有安全ASCII、最长128字节，计数／revision为有界63位整数。时间对外RFC3339在固定64字节字段内，持久UTC微秒。正文／集合同时受字段与完整编码限额。新票据根≤2048、成员叶≤512；反馈消费叶≤1024；当前状态完整≤4096、目标完整≤4096；索引词项叶≤8192，单对象规范化材料≤8192且至多4096词项，词项每行含token（≤8 UTF-8字节）、object_id、revision、ordinal，编码≤256。超出规范化材料／词项上限的对象仍按原memory契约保存，索引工作标INDEX_FORMAT_LIMIT及覆盖缺口；按ID仍可读取，不截断材料后标全覆盖。dirty路径遇同样限制须说明缺口，require_complete=true拒绝。

词项最坏数量按规范化字符数L有`L+max(0,L−1)`；NFKC可扩张，必须验证规范化后界，不能只按原正文2048字节宣称必然小于4096项。索引计算在事务外，索引提交前由memory重查正文摘要／预期修订，再同UoW写retrieval词项及memory待办确认；预处理定义改变须新代次重建，不改变正式对象原可接收范围。

### 9.2 封闭记录、命令与装配账目（静态推导）

本节保留起草时的三类依据：基线源文件静态形状、历史执行输出和批准方案的静态格式上界；这些推导不作为当前编码器执行证据，实际实现与验证另见工作记录。KiB=1024字节、MiB=2^20、GiB=2^30。记录的“完整字节限额”包括字段名、标点、转义、空值及元信息，是Schema的一部分，不能仅检查正文。除配置v2的3倍专门界，嵌套文本沿原6倍保守界。

<a id="closed-schemas"></a>

#### 9.2.1 公共类型与封闭数据Schema

记法：`ID`为现行安全ASCII标识符≤128字节；`H`为64位小写十六进制摘要；`N`为0至2^63−1整数；`T`为有符号63位UTC微秒；`B(n)`为严格UTF-8文本≤n字节；`?`表示必填且可null，只有明确写“patch”的字段可缺省。记录拒绝未知键、重复键、错误类型和同集合重复身份；数组长度下限默认0。持久化表格式版本固定1；仅字段表显式列version者将版本写入body，其他由owner Schema版本绑定；下列父记录持有的子叶必须在同一数据库、owner和根ID范围内，不能靠可猜ID跨授权点读。ID数组按稳定顺序编码。状态枚举取§5–7的封闭集合，不允许自由状态文本。

| 记录／完整编码限额 | 恰有字段与集合限制 |
| --- | --- |
| TicketRoot／2048 | version:N=1；recall_id,database_id,instance_id,principal_binding_id,host_id,entry_id,config_snapshot_id,request_key:ID；query_mode:NORMAL/DEEP；issued_at_us,expires_at_us,clock_observation:T；response_digest,intent_digest:H；member_count:N≤8。16个键最长24字节：键封套≤16×28＋2=450，8ID值≤1040、2H≤132、5数值≤100、模式≤8，合计≤1730，余318；时间／version／count已计5数值 |
| TicketMember／512 | recall_id,object_id:ID；returned_revision:N；returned_lifecycle:ACTIVE/FORGOTTEN。两ID260＋数20＋枚举11＋四键封套≤112=403；(recall_id,object_id)唯一；根member_count须等于叶数 |
| UsageEvidence／1024（retrieval消费叶及memory效果收据各自完整记录） | database_id,principal_binding_id,recall_id,object_id,operation_key:ID；returned_revision,result_revision:N；applied_at_us:T；effect:CONSUMED/CHANGED/RESTORED；retention_before,retention_after:N≤100。5ID650＋3数60＋2分数6＋枚举10＋11键封套≤264=990。唯一消费键仍为四ID，不因分叶删绑定；已用成员关联原operation_key，不复制完整响应 |
| TicketDisposition／1024 | recall_id,database_id,principal_binding_id,request_key:ID；intent_digest:H；expired_at_us,disposed_at_us:T；member_count:N≤8；outcome:EXPIRED；version:N=1。与原键／消费证据关联保留，无查询正文；不可清除未决确认依据 |
| StateRoot／4096 | instance_id,activity_id,last_host_id,last_entry_id:ID；revision:N；activity_value:B(512)；started_at,ended_at:T?；first_reported_at,reported_at,received_at:T；reported_offset_minutes:整数−1439..1439；fields恰scene/progress/emotion各为StateField?。StateField恰value:B(512),started_at:T?,first_reported_at:T,updated_at:T,offset_minutes:同前。父子全部计入4096，不能把三个512当作总包必然相容 |
| GoalRoot／4096；GoalSource／512；GoalAlias／512 | 根：goal_id,canonical_id:ID；revision:N；content:B(2048)；subject_ids:ID[0..4]；world_scope:ID；status:OPEN/COMPLETED/ABANDONED；deadline:T?；reminder_lead_seconds:N?（≤31536000）；route_id:ID?；created_at,updated_at:T；source_count:N≤8；alias_count:N≤64；dedup_state:§6枚举。来源叶恰goal_id,source_id,basis_id:ID、origin:EXTERNAL/TRUSTED_INTERNAL、created_at:T；完整来源输入保留在原owner，basis_id为受控引用，最多8叶，不复制原文。别名叶恰alias_id,canonical_id:ID、revision:N、created_at:T；最多64，禁止多跳 |
| DedupTask／2048；候选叶／512 | 根：task_id,goal_id,config_snapshot_id:ID；owner_id:ID?；revision,goal_revision:N；status:PENDING/RUNNING/EXACT_MERGED/DISTINCT/NEEDS_SEMANTIC_REVIEW/FAILED；created_at,started_at,deadline_at:T（started_at可null）；candidate_count:N≤32；cursor:ID?；operation_key:ID。叶恰task_id,candidate_id:ID、revision:N；≤32叶，冻结候选，结束事实持久保留 |
| ReminderPlan／2048；Attempt／2048 | 计划恰plan_id,goal_id,route_id,config_snapshot_id:ID；deadline_revision:N；kind:UPCOMING/DUE；due_at,deadline:T；status:WAIT_DEDUP/PENDING/SUPERSEDED/CANCELLED/UNSENT_UNAVAILABLE/ATTEMPTING/ACKNOWLEDGED/UNKNOWN/FAILED；delivery_id:ID?；revision:N；updated_at:T。尝试恰delivery_id,plan_id,operation_key:ID；attempt_no:N=1..2；revision:N；started_at,finished_at:T（后者可null）；state:REGISTERED/NOT_SENT/ACKNOWLEDGED/UNKNOWN/FAILED；request_digest:H；reason:§7固定原因；没有远端自由错误或URL |
| MemoryGap／512；GenerationAck／512 | 缺口恰object_id:ID、revision,first_uncovered_seq,latest_change_seq:N、action:UPSERT/REMOVE；每对象1根。确认叶恰object_id,generation_id:ID、revision,applied_seq:N、action:同前；最多2代次。memory自有change_sequence单行恰instance_id:ID、last_seq:N、revision:N、published_generation_id:ID?、published_seq:N，完整编码≤512字节；usage_object保存object_id:ID,last_used_at:T?；不改旧正文形状 |
| IndexGeneration／2048；IndexPage／2048；IndexObject／512；Posting／256 | 代次恰generation_id,config_snapshot_id,preprocess_id,unicode_version:ID；format_version:N=1；revision:N；captured_seq,contiguous_seq,pending_count:N；status:BUILDING/ACTIVE/RETIRING/FAILED；scan_cursor:ID?；observed_at:T。页恰page_id,generation_id,operation_key:ID、owner_id:ID?、revision:N、cursor:ID?、count:N≤16、status:PENDING/RUNNING/COMMITTED/FAILED、updated_at:T。对象清单恰generation_id,object_id:ID、revision:N、body_digest:H、term_count:N≤4096。词项仅token:B(8)、object_id:ID、revision:N、ordinal:N≤4095；generation由所属表分区持有，完整键计入该分区索引开销，不再将generation_id重复塞入256字节payload |
| Lease／1024；Coordinator／2048 | lease恰work_id,owner_id,instance_id,operation_key:ID、epoch:N、status:RUNNING/RECOVERY_PENDING/ENDED、observed_at:T；实际任务结束后才释放。协调根恰instance_id,database_id,config_snapshot_id:ID、clock_high_water:T、occupied_tickets:N≤10000、cleanup_cursor:ID?、active_generation:ID?、building_generation:ID?、revision:N；不以租期届满代替旧owner隔离 |

完整State/Goal内层文本极值可能超完整包：此时原子拒绝LIMIT_EXCEEDED，允许的完整记录集合明确是“字段约束且总编码约束”。推荐普通中文样例必须能构造有效记录，最大合法组合以总包边界验收；不宣称每字段任意极值组合均可接受。每个审计／工作记录也用相同规则。索引词项≤8 UTF-8在基础ASCII编码中至多48字节；object_id130、revision≤20、ordinal≤4、键名及标点≤46，共≤248≤256；SQL索引结构另计，不把payload当磁盘物理大小。

**候选中的成形目标格式：** 新装配的cognition模块格式为2，保留原候选v1解释，并明确绑定目标候选v2；旧装配只解释v1，旧DDL不迁移。v2保留原4096字节manifest全部字段及顺序摘要规则，只将candidate_version固定为2，ordered_change_refs允许CREATE_GOAL。目标叶完整≤8192，恰change_version=1、action=CREATE_GOAL、target_id:ID、expected_revision=null、proposed_value、links=null；proposed_value恰GoalRoot的content/subject_ids/world_scope/deadline/reminder_lead_seconds/route_id约束及basis_id:ID，完整≤4096。目标ID由原database/batch/handoff/transform/ordinal及GOAL域推导；来源ID取原candidate_id，不由普通宿主指定。目标叶和原记忆变更叶合计≤8、全候选仍≤73728。

两个目标应用固定分支沿原candidate release-plan语义，包含至少一个真实既有对象变更，保留原object_history实际写入；目标叶不会传给memory变更解释器。有限主体／对象权限及路由来自原工作绑定，basis在同UoW由memory核验，目标由goals写入；正文、来源、任务和计划写入失败均回滚原候选、记忆、历史和批次终态。恢复重新校验完整v2及原键，不调用模型、不把目标拆为事后注入。尚无对应原记忆变更的目标由已有外部／可信内部目标端口表达，不制造记忆变更来适配必要审计。

<a id="goals-initialization"></a>

**GoalsMetadata（已批准，完整编码≤1024字节）：** 恰有metadata_id、database_id、instance_id、config_snapshot_id四个ID，以及format_version:N=1、revision:N=1、initialized_at_us:T；所有字段必填、不可null，拒绝未知字段。goals独占metadata表及公开初始化／恢复端口。metadata_id由可信database_id、instance_id按固定域分离摘要产生；database／instance来自保留资源绑定，config_snapshot_id来自已持久配置，format_version来自固定模块格式，initialized_at_us来自原初始化命令冻结的可信观察，均不得由普通宿主覆盖。metadata_id为主键，instance_id及database_id分别唯一；scope与实例绑定一致，每实例／库仅一行。

首次初始化在同一UoW写入retrieval协调根、state空指针、memory信息格式／change_sequence、goals元数据，以及四份必要审计、结果绑定证据和原回执；goals七张业务表保持空。targets仍共用真实Coordinator；goals自己的事实必须引用真实metadata_id、previous_revision=null、revision=1、changed_count=1及初始化时点，不以共享targets代替实际写入。原键确认不重写或增加审计。

OPEN_EXISTING在存储验证原Schema／回执／审计后，由goals公开恢复端口核验唯一元数据、全部绑定、完整规范编码、format_version=1、revision=1及其与原初始化事实／时点的一致性。缺失、重复、损坏返回STORAGE_FAILED/INTEGRITY_FAILURE；格式不支持返回STORAGE_FAILED/FORMAT_UNSUPPORTED；可信绑定不符返回ACCESS_DENIED/BINDING_MISMATCH。核验未完成不进入READY，不补表、补写、迁移或自动修复；仅明确CREATE_NEW且四owner均未初始化时允许首次创建。

#### 9.2.2 输入、结果与必要审计形状

以下外部输入中的时间用`TimeWire=B(64)`（§6的有偏移RFC3339）；下列T记号在外部输入处表示TimeWire，归一化后才成为持久T整数，不能同时接受无声明的整数时间协议。外部查询恰`request_key:ID, entry_id:ID, query_text:B(512), subject_ids:ID[0..8], object_ids:ID[0..8], category:ID?, world_scope:ID?, time_range:{start:T?,end:T?}?, allow_partial:bool, require_complete:bool, retrieval_mode:LOCAL_LEXICAL_V1, rerank:false, include_state:bool, include_goals:bool`；prepare另外有`participant_ids:ID[0..8], situation:B(512)`，合计完整查询≤4096，入口仍须等于可信绑定。反馈恰`operation_key,recall_id:ID, used_members:{object_id:ID,returned_revision:N}[1..8], used_at:T?`，完整≤4096。状态输入为`operation_key:ID,activity_id:ID?,expected_revision:N?,replace_activity:bool,patch`，patch仅StateRoot的activity_value、started_at、reported_at、reported_offset_minutes和fields；子字段仅value、started_at可显式更换，服务器首次／接收时间不可自报。目标输入为`operation_key:ID,goal_id:ID?,expected_revision:N?,content:B(2048),subject_ids:ID[0..4],world_scope:ID,deadline:T?,reminder_lead_seconds:N?,route_id:ID?,source_id:ID`；状态／期限更新只接对应字段，内部依据从可信候选读，不接受宿主提供basis_id冒充内部来源。每操作只接受自己的固定字段子集，set/update/end/inject/status/deadline不共用任意patch。

新领域持久命令采用统一**外形、各kind独立的封闭payload**：`binding_id:ID, request_key:ID, expected:{object_id:ID,revision:N}[0..16], observed_at:T, payload:B(24576)`。payload为上表或下表指定记录的规范化JSON；解码后再次核对应kind的精确Schema、完整限额及权限，不能接受自由命令、SQL、自由owner或表达式。最多两目标、8来源、8反馈对象；索引payload是1对象ID／revision／正文摘要和≤8192规范化文本，页命令只用16个有界引用／检查点。原键摘要覆盖全部payload及静态定义，非原owner不得以引用换取写权限。配置初始化另用既有专属形状，见下节。

除下述保留旧形状的候选应用分支外，新结果恰`outcome`（各kind从§5–7固定枚举选，最多16项）、`targets:{object_id:ID,previous_revision:N?,revision:N}[1..16]`、`items:{object_id:ID,status:APPLIED/ALREADY_APPLIED/NO_CHANGE,operation_key:ID,revision:N}[0..8]`、`facts`（仅该kind必需owner的固定记录）。每份owner fact恰`object_id:ID,previous_revision:N?,revision:N,changed_count:N,restored_count:N,from_seq:N,to_seq:N,at_us:T`，无变化owner不进入该静态kind；结果总≤12288，含持久身份／指纹／commit_id的完整回执≤16384，结果绑定证据≤32768。两个新增candidate-with-goals分支例外：完整保留原result_schema的operation_id/entry_id/batch_id/candidate_id/source_id/terminal/object_refs/history/retired_source_ids/targets/storage_execution/model_adapter/candidate_origin和各owner_fact，只加goals fact，沿原完整回执65536限；不能为了套12288而删原确认依据。任何logging_service参与的object_history slot继续使用原owner_fact：rows_changed、state、`references[0..20]{name,object_id,revision}`、`counts[0..16]{name,count}`、`history_ids[0..8]`，不替换成通用八字段摘要；相关feedback回执上界提高至32768、证据仍32768。大正文历史仍用原专属接口与独立叶，不增加自由result附件。返回票据成员／正文由业务只读投影构造，不塞进此结果。

每个实际写owner恰一个必要slot（logging_service的slot即原object_history，不再重复计一个通用slot），slot/code分别固定为`{owner}_{kind}`／kind大写，reason固定APPLY；不能根据运行结果删slot。新反馈三个kind把“首次仅消费”“实际改变但不恢复”“至少一项恢复”静态分开；混合成员选择最强实际分支，所有必要owner同时提交。intent恰`actor:ID`。每slot五项绑定保持完整：actor_kind←常量SYSTEM、actor_ref←intent.actor、reason_code←常量APPLY、target_refs←result.targets、change←result.facts对应owner；manifest版本、change_schema、reasons、这些直接共用targets的新增slot固定target_limit=16、绑定path与空constant均进入描述。各slot完整审计≤8192且每操作≤8slot（≤65536审计预算）；无正文slot通常可收紧到4096，但核算不靠收紧。已有source释放、对象历史、Provider计费审计原样保留。4096/8192是完整事件限，历史叶另按原8192×8上限。

<a id="audit-target-compatibility"></a>

**与既有审计端口的静态相容性：**[日志§10.10.4](logging.md#runtime-derived-audit-draft)要求可信装配先完成类型校验。[audit_event_schema](../../companion_memory/logging_service/audit_records.py)的target_refs恰为必填、不可null的Sequence，范围1..requirement.target_limit，元素恰含identifier类型object_id、可null的integer类型previous_revision、不可null的integer类型revision。[validate_bindings／_fits](../../companion_memory/logging_service/audit_materialization.py)要求源序列下限不小于目标下限、上限不大于目标上限，并递归核验元素；[PersistenceService构造入口](../../companion_memory/persistence/service.py)在生成assembly及资源初始化之前调用该校验。因此本包新增直接映射的result.targets必填、不可null、范围固定1..16，所有接收该完整数组的必要slot均声明target_limit=16；ID/N及nullable属性按上述类型对应。不能以“handler实际总是非空”替代声明，也不改变日志Schema、旧候选结果、旧命令或必要slot。

**真实目标与零写入：** targets是本事务的有限真实目标引用，不要求每个slot各造一份对象；同一不可变结果可供多个必要slot绑定，owner自己的change仍分别核验。业务对象未改变而仅保存使用事实时，可引用其真实当前修订并令前后相同，不能加虚假业务revision。没有业务对象的进度写入引用下表中的实际进度根及其本次修订，不以instance_id、operation_key或常量1无条件补位。首次真实创建记录的previous_revision=null、revision=1；更新取同UoW读到的前值与实际写入的后值。目标身份须能定位对应owner／库内的记录，不能拿Schema版本、格式版本、时间或计数冒充修订。

为使进度目标有明确来源，上节已有IndexGeneration、IndexPage、DedupTask、Attempt四种封闭记录补齐各自`revision:N`，用于已有创建／状态／检查点变更的持久版本核验：首次实际创建为1，此后有实际记录变更才递增；不新增进度对象、空事务、审计slot或产品行为。其余已有revision的Coordinator、StateRoot、GoalRoot、ReminderPlan等继续取原字段；票据格式version不能当业务revision。无可领取工作、空清理页、全成员ALREADY_APPLIED、状态NO_CHANGE、原键确认等零写入路径只读返回既有事实或既有拒绝，不执行带必要审计的空变更命令，不为了满足下限创建票据、推进修订或追加占位事件。空扫描若确实完成并持久更新了原任务检查点则属于进度写入，须引用该实际检查点；不存在该写入时仍走零写入路径。

| §9.2.3现有命令分支 | result.targets的真实来源；无业务对象时的处理 |
| --- | --- |
| 配置初始化／initialize information owners | 前者沿现有configuration初始化结果，引用该事务新建的真实snapshot_id及初始修订；后者引用实际创建的retrieval Coordinator根（由instance绑定定位）及其revision，承载初始化／时钟／指针事实，四个必要slot共用该结果，不能因state/goals为空而造活动或目标。已初始化的原键确认不追加审计 |
| index begin / claim / apply-object / confirm-page / publish / fail / retire-page | begin引用实际新建IndexGeneration；claim、confirm-page、retire-page引用本次创建或推进的IndexPage；publish引用实际发布的IndexGeneration和实际改动的Coordinator；fail引用实际被终结的IndexPage或IndexGeneration。apply-object引用实际写入的IndexObject所绑定权威对象ID／revision，并以本次推进的IndexPage或代次根承载进度；REMOVE时不得伪造现存对象，引用实际推进的页／代次根，memory缺口确认仍在该owner事实中保留。每项修订来自同事务真实记录，不用captured_seq、count或format_version替代 |
| issue NORMAL / DEEP | 引用签发同事务增加occupied_tickets的实际Coordinator根及前后revision；recall_id、成员和签发效果仍在原票据／回执事实关联中。零命中不签票，也不推进占槽修订 |
| expire-ticket / trim-index-page | expire引用处置同事务实际释放槽位／推进cleanup_cursor的Coordinator根及修订，TicketDisposition和被处置recall_id保留关联；trim引用实际推进的退役IndexPage及修订，不能把已物理删除的posting冒充当前对象。尚未过期／受恢复保护且未推进任何游标、或空页零写入，不制造审计目标 |
| usage consume / change / restore | 取本次确实首次保存消费事实的1..8个成员object_id，前后值来自权威当前对象及UsageEvidence.returned_revision/result_revision；consume未改变对象时前后修订相同，change/restore取真实新修订。混合已用／未用仅以本次产生新效果的成员为目标，已用项继续关联原回执；全部已用时只读确认，不提交空反馈或虚假恢复审计 |
| state set / update / end | 引用本次创建／更新／结束的StateRoot.activity_id及真实revision；更换活动可同时引用旧活动终结和新活动创建两项。结束后没有当前活动正文，不等于没有真实结束记录；NO_CHANGE不递增 |
| goal inject external / internal、status、deadline、exact merge | 引用本次真实写入的GoalRoot（合并至多两根，保留canonical／alias关联）；前后revision由goals实际事务给出。来源／计划变化仍在原必要事实中核验，不能靠重复目标引用挤满数组或为只读别名解析造修订 |
| dedup claim / finish、plan advance、attempt begin / finish | 分别引用真实DedupTask、ReminderPlan、Attempt进度根及其revision；可以没有目标正文变化，仍有实际领取／终结／计划推进／尝试事实。DISABLED只有持久写UNSENT_UNAVAILABLE时才审计该计划；无到期任务或对已终态的纯确认不另写 |
| 两个candidate-with-goals及旧候选／其他复用分支 | 保留原result_schema、原targets来源、object_history及全部必要slot，不套用新的进度补位逻辑或重写旧结果；goals新增事实仍与原候选效果同事务。其完整装配也须参加下述未来绑定校验 |

上述目标按真实记录身份去重、稳定排序，总数仍≤16；只列该命令的直接目标／进度根，来源、别名、词项及成员明细保留原有界事实／叶，不把所有叶展开进targets。所列新增分支最多为8个反馈对象，或索引对象加页／代次根，或两目标／两活动，均在原16项预算内；不是以截断实际必需目标方式相容。

#### 9.2.3 全命令族、固定分支与owner清单

基线历史验收记录记载`command_count=64, command_descriptors=551815, repository_descriptors=21579, envelope=94, assembly=573488`。原记录引用临时unittest输出第691行；该临时文件现已不可用，本轮未重新核验原始输出，旧引用可通过Git定位。这是对应已验收版本的历史记录，不是新包测量。[command_descriptor／assembly_value](../../companion_memory/persistence/_codec.py)、[content_budget](../../companion_memory/runtime/content_budget.py)静态确认：participants只编码owner名；DDL表名和SQL进入仓储描述，查询statement的Schema不进入此字段；所有结果Schema、必要审计Schema／manifest、intent和五字段绑定均进入命令描述，不能只数业务入参。

| 基线族（合计64） | 新组合处理与实际写owner／必要审计 |
| --- | --- |
| content主族22 | 在新组合全部以独立information kind替换，保守保留原全部描述成本；15主命令加accept/select/complete/freeze/store/commit published/commit without的7个media固定分支。initialize写runtime；register/accept写ingress,buffers；select/freeze/commit写runtime,buffers,ingress；claim/complete/request关联及确认/admission开关/park写runtime；store写runtime,cognition,ingress；commit另写cognition，published写memory；media分支加media；每个实际writer保持原owner_content必要slot，涉及memory同时推进change_seq及gap |
| candidate application 3、memory maintenance 5 | 替换为新memory Schema的独立kind。candidate计划memory；应用写runtime,cognition,memory,logging_service,ingress,buffers及可选media两个固定分支。维护计划memory，应用按none/ingress/media/ingress_media四个固定释放分支，memory和logging_service必需；logging_service的object_history及各owner_content均保留，不能把删除释放改成单memory写 |
| 配置初始化1 | 替换为独立information初始化kind，域数5、body总294912；configuration的configuration_initialized必要slot和五项结果绑定保留，结果targets上限16；具体完整输入账目见下一节 |
| transfer 1、mode 3、disposal 3、runtime media协调6 | 复用13条原描述。transfer写buffers,ingress并保留owner_transfer；mode三个kind（change/park/resume）写runtime并保留runtime_mode，ENTER/READY/FINISH/FAULT/DRAINED仍为固定action；disposal计划runtime、应用runtime/buffers/ingress及media分支，owner_disposal保持；媒体协调的admission/register/release写media,ingress，reuse/associate/store写media，owner_content保持 |
| media service 13 | 复用原13命令：bind staging、block integrity、resume upload、require reupload、initialize root、begin/seal/publish upload、retire/delete blob、acquire/release read、abandon upload；唯一写owner media，每条media_changed必要slot不变 |
| Provider 7 | 原register/prepare/settle/terminate/recover/evidence/initialize_budget描述与账本语义全部复用，provider_change及原fingerprint_version=1不改；不为本地查询新造模型profile或省去结算分支 |

计数：替换31（22＋3＋5＋1），复用33（13＋13＋7）；新组合保留64个对应位置，并新增下列30个静态kind，共94。只读查询、原键确认、状态／目标观察、Provider统计和过期对象扫描不增加写命令；后者调用上表已有计划／删除固定分支。表中P为参与仓储capability，W为实际写owner；P至少包含W，额外只读参与者不产生虚假审计，不能把旧代码“整个仓储集合participants”误算成所有owner都写。

| 新增固定kind族／数量 | 封闭payload与P/W／必要slot |
| --- | --- |
| index begin/claim/apply-object/confirm-page/publish/fail/retire-page，7 | payload分别为代次、页、对象材料、页确认、代次发布、失败事实、16个退役引用；P=retrieval,memory；apply-object与publish的W=retrieval,memory（当前修订／缺口CAS及发布截点），其余W=retrieval；各W必要slot，不能用可选memory slot混成一条 |
| issue NORMAL / DEEP，2 | TicketRoot及≤8成员；P=retrieval,memory；W=retrieval，memory仅权威末端读取；retrieval签发slot。无命中只读，不造空票 |
| expire-ticket / trim-index-page，2 | 前者1根过期票及成员引用，后者≤16个已退役索引对象引用；W=retrieval，处置／回收审计保留；不删消费证据。退役状态切换与物理分批清理分别确认 |
| usage consume / change / restore，3 | UsageEvidence所需票及≤8对象输入；consume的W=retrieval,memory（仍有memory收据）；change/restore的W再含logging_service历史参与者；P同W；各owner及object_history必要slot；RESTORED依赖待办归memory |
| state set / update / end，3 | StateRoot创建／固定patch／结束身份；W=state，1个状态必要slot；替换活动与结束前一活动为同一owner事务 |
| goal inject external / internal、status、deadline、dedup claim、dedup finish、exact merge、plan advance、attempt begin、attempt finish，10 | 依次GoalRoot＋来源引用／可信候选引用／状态／期限／任务／任务终态／两目标冻结引用／计划／尝试／确认事实；W=goals，每条1个goals必要slot覆盖来源、别名、去重、计划及投递的计数／修订，完整业务明细在相应持久叶，不写可丢失自由摘要。内部注入P再含memory,cognition以核依据，未提交候选效果走下一行 |
| candidate apply with goals / with media and goals，2 | 原冻结candidate／release plan引用；W=runtime,cognition,memory,logging_service,ingress,buffers,goals及第二分支media；8个owner上限，object_history及其余原必要slot全部保留；goals效果与memory效果同事务，不把已确认候选目标拆成事后尽力 |
| initialize information owners，1 | configuration身份＋database/instance绑定；W=retrieval,state,goals,memory；初始化retrieval协调根、时钟高水位、state空指针、memory初始化记录和goals元数据，4个必要slot；P再含configuration只读核验 |

新owner仓储采用固定列索引＋规范化封闭body，禁止运行时生成DDL。新增表共26：memory 6（信息格式、change_sequence、index_gap、generation_ack、usage_receipt、usage_object），retrieval 10（ticket、member、consumption、disposition、index_generation、posting、index_object、index_page、lease、coordinator），state 2（activity、current_pointer），goals 8（metadata、goal、source、alias、dedup_task、dedup_candidate、reminder_plan、attempt）。每表至多3个辅助索引，共≤104份新增DDL声明；唯一性／CAS索引含在其中。旧表DDL原样保留，memory新表导致独立schema_version；各表字段为上节body加该记录身份、revision、状态／调度／分页所需固定列，禁止第二份无界正文。新DDL每份规范化ASCII SQL≤640字节（无控制字符／双引号／反斜线），表名和SQL JSON封套≤128；必要唯一、时间及分页索引须在此预算内给出未来实际DDL，不能省约束凑数。

#### 9.2.4 可复核编码上界、原限额缺口及一项修订推荐

新增kind不复用无限展开的业务大Schema。按§9.2.2固定外形逐项封顶：全部input/result/intent/change_schema展开后的Field出现次数≤256（包括result.facts与各审计重复），每Field封套含≤32字节field名及nullable/optional标志≤96字节；叶／sequence／enum描述合计≤21504字节（其中普通标量≤192项×96，容器≤16×64，枚举总choices串≤2048）。manifest＋8slot的完整五字段binding及slot/version/reasons/targets≤8192，顶层身份／participants≤4096。因此每新增descriptor≤256×96＋21504＋8192＋4096=58368≤65536字节。这是对具体外形的保守构造预算，payload内部闭合集合由对应固定validator版本约束，不能当任意JSON后门；若未来将payload展开为更多Schema节点必须重新核算。通常8-owner外形的Field展开≤155；保留原candidate的references/counts及history_ids后，两个候选应用分支input≤7、result含各owner嵌套≤120、intent1、8份change≤96，合计≤224，低于256。上述计数包括数组item的Schema一次，不乘运行时数组长度。

本次targets下限0→1只收紧合法集合，上限16、元素形状、slot数及绑定路径不变；描述中的minimum仍是一位整数，不增加原最大结果、审计、回执或命令预算。四种已有进度根各补一个revision字段，按键／分隔及整数保守至多32字节，仍各受原完整2048字节硬限；四种记录逐一均不超过6个128字节ID、1个64字节摘要、8个整数、2个32字节枚举及18个32字节字段名；统一保守界为6×130＋66＋8×20＋2×34＋18×36＋2＝1724≤2048，已含新增revision。字段位于原封闭body内，该进度revision补齐不新增表／索引／命令；另批准的goals元数据表计入下节。payload仍为原有界文本，descriptor节点不展开这些body，故§9.2.4的2694098装配及1007616初始化静态包络不变，配置§11.16同步新增元数据表的仓储容量结论。这里是字段和包络推导，不是绑定校验或编码器执行结果。

31替换kind保留原形成本已包括在551815内；新增kind名／version、明确participants增量、memory change_seq审计的from_seq/to_seq、配置第五域等相对增量共给65536字节：31条kind顶层增量≤31×256=7936，最多31条每条2处owner fact各加两个数值Field≤31×2×2×192=23808，participants新增owner名总≤12288，配置Schema／intent等额外≤8192，合计52224≤65536。其他旧定义不变；超出这个明确增量集合不能引用该证明。

| 核算对象 | 静态上界／历史证据 | 与原限额分别比较 |
| --- | --- | --- |
| command_descriptors | 历史551815＋替换增量65536＋新增30×65536＝2583431 | 原655360只余103545；本保守包络比原限多1928071，**不能证明原限可用**；不是断言未来实编码一定超出这么多 |
| repositories | 历史21579＋104×(640 SQL＋128封套)＋1024 owner/version/数组增量＝102475 | ≤131072，余28597；statement查询Schema不误加进此项，所需新表／索引并未省去 |
| 外层 | 两数组／外壳／94命令分隔及排序索引封套保守8192 | ≤8192；实际历史94不能当新值，排序本身不增加持久内容 |
| 完整assembly | 2583431＋102475＋8192＝2694098 | 原794624不能由此证明相容；基线assembly_value的1048576硬限也不适用新包络，须使用显式新静态格式 |
| 推荐仅新装配限额 | 描述2621440＋仓储131072＋外层8192＝2760704；静态assembly载体硬限3145728 | 推导包2694098≤2760704，余66606；单项描述余38009。旧ContentHost仍655360/131072/8192/794624及1 MiB静态载体 |

**第五决定组已批准修订：**采用上述InformationHost专属静态格式／限额，加配置新body总294912，普通命令仍1 MiB。未来须为静态assembly的编码、存储、解码、比较和OPEN_EXISTING校验提供显式格式绑定的3 MiB通路，不能仅改content_budget数字；旧格式按原1 MiB读取，原指纹字节不改。该通路属于已批准的持久层工程增量，实现和验证事实另行记录。上述静态推导只证明所列有限构造预算在修订包络内，不证明实际DDL、descriptor和初始化已装配成功。若坚持原限额，具体缺口是30新kind及31替换增量没有≤103545的压缩证明，需要另提完整Schema表示方案；初始化时拒绝只是一条失败协议，不是推荐包可用证据。

| 完整输入／结果及运行载荷 | 静态上界及限制 |
| --- | --- |
| 业务HTTP输入 | 头8192／body16384分开；query完整4096，状态／目标记录4096，外层仍计body；超限413，不截正文 |
| 回复及目标页 | 8×(4096＋1024)＋4×(2048＋512)＋8192 persona＋4096 state＋8×4096 goals＋8192封套＝104448≤131072。目标页8×4096＋4096＝36864；观察仍32768。各区是直接JSON结构，不再次串化；所有元信息计入各区 |
| 票据及签发 | 根2048＋8叶512＝6144；完整持久签发命令≤65536 descriptor＋6×6144 payload＋8192外形／intent＝110592≤1048576；票据不保存104448字节响应 |
| 反馈／状态／目标／索引命令 | 最大payload24576，descriptor65536、其他values／intent／封套8192：6×24576＋65536＋8192＝221184≤1048576。索引传≤8192材料而非4096词项行；页传16引用而非16 MiB行数组。两目标合并来源结果≤8，否则保持独立 |
| 结果、点读与审计 | 新结果≤12288、完整回执≤16384≤65536、结果证据≤32768；feedback完整回执≤32768、candidate-with-goals≤65536；原8192叶点读6×8192＋8192＝57344≤65536；消费叶1024点读≤14336。8slot×8192＝65536审计逻辑预算，逐条事件独立保存／点读，不将所有事件再嵌套入一份65536回执；旧历史8×8192另计 |
| 配置定义和值 | 旧105项以原有效包完整body≤262144为保守基数；新8项各≤4096（配置§11.16定义上界），增加32768，合计≤294912。113≤128，单条仍≤8192，5域catalog≤8192；不凭105条平均大小假定可塞原总262144 |
| **完整配置初始化命令** | input仍catalog＋5域各domain_id/digest/entries(parameter_key,body)；result为snapshot_id、5域revision、targets及change，intent一slot actor，专属descriptor≤32768。3×294912 body＋128×256键及entry封套＋6×8192 catalog＋32768 descriptor＋8192域／外层／intent＝1007616≤1048576，余40960。计入definition、values、intentions三部分，不能只算candidate_values；descriptor32768可用上面公式按单slot／≤64Field收紧证明（12288＋4096＋4096＋4096＝24576） |
| 索引行及覆盖 | 单对象4096×256＝1 MiB逻辑词项行；16对象页16 MiB，推荐逐对象提交＋页检查点，不扩大命令参数。100000对象×(512缺口＋2×512确认)＝146.484 MiB；删除待办／历史另增，不能按现存对象数清除 |
| 并发载荷 | 2查询×(131072响应＋128×512候选＋4096×256 posting＋128×8192 dirty材料)＝4.375 MiB；另索引最多16 MiB逻辑页、1 MiB命令、最多3 MiB静态assembly、旧媒体3 MiB副本及日志队列。此为材料包络，非RSS／B-tree／WAL实测 |

全包实物验证须同时满足两项约束：26表及其唯一／分页索引实际SQL须满足每份640字节构造；94条实际描述须满足上述替换增量和节点限额并保留全审计语义。以上是已批准的上界，超出时须重新裁定，不能将编码失败包装为完成初始化；实际验证事实见[CURRENT_TASK](../work/CURRENT_TASK.md)。配置旧定义／路径沿已批准格式的有效集合，任意超长目录／自定义元信息不在该有效集合内，仍须显式提供真实绑定后验证。

### 9.3 累计磁盘与无保留政策的代价

100000对象×4096约0.381 GiB；词项按每对象4096×256保守上界约97.656 GiB/代次。活动加速代次＋构建代次共2份，最坏195.313 GiB；退役代次在实际读者未释放时仍计入两代次限额，因此新重建须先确认只余活动代次且有足够空间，不能再开第三代次。此界刻意包括ID／行封套，实际B-tree页面／索引结构及WAL还未计；不声称100000最大对象适合小磁盘。

较典型但**未经测量**的演算样本：每对象128个posting，2份×100000×128×256约6.104 GiB。必须分别报告典型与最大包；降低词项数会扩大不支持索引的对象集合，不得暗截后仍报完整覆盖。

10000占槽票据×6144约58.594 MiB；retrieval消费和memory效果收据两份各8×1024／票，合计另约156.250 MiB，处置根还可累计10000×1024＝9.766 MiB。消费叶／收据是两owner的实际依据，不因票据过期清除；以上仅逻辑payload，未计数据库索引。票据根2048、处置根1024、成员512及两份消费1024的所有完整键均计入，不能靠省database／principal绑定省容量。

| 票池使用剖面 | 24小时TTL、10000占槽及清理的具体代价 |
| --- | --- |
| 4请求/s×600秒峰值 | 最多2400张新票（每次都有正式命中且新key）；实验开始占槽≤7600，所有同期签票包含在这2400内。空结果／原键确认不发票，必须另报，不能用其稀释成本。峰值不要求稳定池已满仍能签票 |
| 持续4票/s | 24小时需要345600张有效票；空池在2500秒（41分40秒）到达10000，随后新签票429。首批24小时后才可处置，且16票/分钟仅0.2667票/s，远小于4，不能长期平衡；单纯改为345600容量仍缺持续清理与历史存储方案 |
| 保留推荐参数的持续工作负载 | 理论TTL均值上限10000/86400≈0.11574票/s，仅假设即时清理；推荐实验持续≤0.08票/s（6912票/天），不是新增运行时速率默认或生产SLA。均匀负载每分钟4.8票，正常16票/分钟清理有余；6912有效票＋最多5张一个清理周期延迟约6917，余3083槽。若先积累该负载再插入一次4/s×600秒，替换原同期48票，增加2352票，约9269占槽，余731；需核验实际空槽且无受保护积压 |
| 峰值重复、清理暂停及恢复 | 上述一次峰值到期后每分钟最多240张过期而只能清16张，产生约2240张清理积压；其后在均匀4.8张/分钟负载下净释放约11.2张/分钟，需约200分钟。故不能承诺每隔24小时再来同样峰值；下一次峰值必须等旧峰清理完成且有2400空槽。专注／故障／时钟不确定／未决事务会更久，界面报告occupied/live/expired_pending/protected_pending及oldest时间，不能把过期视为已释放 |
| 每分钟的工作界限 | 最多扫描4×cleanup_page_size＝64根、处置16根和128成员叶，逐票事务；扫描使用(expires_at,recall_id)到期索引，不遍历尚有效票；不可处置项跳过／轮转。实际16/分钟只是上界，未来须验证完成率与后台竞争。有效反馈依据和未决恢复证据不清理；回执／审计只读确认保留 |

高频成本按操作类别单列：签票最多16 KiB回执＋32 KiB证据＋64 KiB审计＝112 KiB；feedback因保留object_history关联采用32＋32＋64＝128 KiB，再加最多8×8192历史叶＝64 KiB，共192 KiB；每次召回随一次反馈的证据／审计／历史合计304 KiB。持续4/s一天345600次的**反事实需求演算**为签发36.914 GiB，签发＋反馈100.195 GiB，另加票据／消费payload、索引、来源、Provider及SQLite开销；当前票池不能实际持续完成此负载。10分钟2400次签发约0.256 GiB，均反馈约0.696 GiB；0.08/s一天6912次签发约0.738 GiB，均反馈约2.004 GiB，仍逐日累计。两个candidate-with-goals沿旧65536回执和结果证据边界另按每操作≤256 KiB含审计／历史预留，不混入112 KiB票据预算。所有数值为静态逻辑上界，非实测平均；30秒重开全校验在累计账本下是否可用亦未验证。

输入／完整来源／媒体累计继续按[既有存储账目](formal-memory-source-media.md#capacity)：共享不重复计算同一payload／blob，未决工作和有效来源不为腾空间删除。目标上限为未结束主对象1000；结束目标、别名／来源、提醒终态及去重历史独立增长，100000次普通目标管理事务按112 KiB约10.681 GiB；带原候选／历史的最大事务按256 KiB约24.414 GiB。新包限制新对象／新票准入，保留原回执查询；若学习候选因现存对象上限不能提交，保持候选和来源保护并标SYSTEM_BLOCKED/CAPACITY_REACHED，不能变成FAILED_DROPPED或丢弃部分对象；不得限制已经承诺的专注暂存数量。磁盘不足时新写失败／未知，状态不可读不填空成功。

生产长期吞吐需要独立批准票据容量、压缩或回执／审计保留与可验证清理协议；本次不实现，也不以TTL清票冒充磁盘封顶。存储OPEN_EXISTING一次全校验仍受原30秒候选期限，新增审计可能让重开持续超时；索引有页检查点不能绕过这层限制。

### 9.4 新旧装配和资源生命周期

推荐新独立InformationHost组合拥有retrieval／state／goals及业务HTTP；memory因新变更序列／反馈改用独立新Schema和命令kind，旧ContentHost、旧Runtime装配、旧记录版本／指纹保持。Provider行为不改；基础存储仅新增显式Information静态装配3 MiB格式通路，普通命令及旧格式不变，详见§9.2.4；不能偷偷扩大所有存储编码限额。新装配仅在明确新自有空临时资源CREATE_NEW；相同完整清单OPEN_EXISTING，新装配开旧库／旧装配开新库明确拒绝，无补表／迁移／自动选择版本。

索引同库独立owner，降低跨文件发布复杂度；主库、日志、媒体根及临时／备份路径仍由各原资源所有者持有，检索不能任意打开路径。HTTP拥有listener／连接／任务与会话；查询拥有有限任务和快照；retrieval拥有索引读代次与worker；goals拥有调度／发送任务；应用借用Provider／日志／配置，不擅自关闭它们。新只读能力只取得固定投影，不能以scope字符串构造任意查询。

关闭顺序：先关HTTP新业务／索引维护／提醒准入和最后交付，保留原键确认与合法本地提交；等待或观察实际在途结束，再回收自有worker／代次读保护，关闭领域绑定，最后由宿主关闭借用者的真正所有者及存储。沿已验收关闭协议保留唯一清理者与完成通知：超时INCOMPLETE、实际占用继续保留，旧报告不可变，全部资源实际释放后才交还根目录。不用TTL、任务取消或进程内异常证明系统调用结束。

恢复顺序仍为存储全校验→配置→Provider→运行／领域本地恢复→索引覆盖／票据／目标计划核验→开放已就绪能力；缺persona等只影响声明的能力，不伪造全就绪。恢复不调用模型或发送提醒，不从日志回填业务。新进程先隔离旧资源owner，按原键核对票据／反馈／目标／发布事实；外部提醒UNKNOWN保持未知。没有安全恢复证据时保守关闭相关能力，不自动解除持久FAULTED。

<a id="validation"></a>

## 10. 整体验收要求与环境准备

以下为必须取得的验证证据，批准本身不代表通过。F档及基础验证已授权；P/X资源与长期负载未授权，实际断言与原始输出见执行记录。

| 编号 | 验证及必须保留的证据 |
| --- | --- |
| Q01 | 真实自有临时SQLite中创建正式中文对象；普通／深度／结构过滤／单字／中英混合各返回当前对象；固定排序可复现，零查询生成式LLM、零本地包embed/rerank |
| Q02 | 中文质量样例至少60组，人工标相关集／不相关集及世界／人物边界；分别统计precision@5、recall@8、MRR及候选截断比例。字面可匹配子集推荐recall@8≥0.90；纯同义／繁简／否定语义单列局限，不以其零命中伪称有语义理解 |
| Q03 | 样例含“周末去北京看展／北京的展览”、全角ＡＩ／ai、中英型号RTX 4090、单字“雨”、同名不同平台、角色扮演与现实、昨天说去／现在目标、否定“没有去北京”；准确标期望而非仅断言有结果；同义“难过／伤心”不要求词法必命中 |
| Q04 | 新记忆提交后立即本地词法可见；索引待办合并与CAS确认双向竞争；评分修订可复用词项；删除／正文改版／遗忘末端拦旧候选；覆盖缺口不得升连续水位 |
| Q05 | 重建中持续写／删、进程在页提交和指针发布前后退出；跨进程重开只发布完整代次；旧读者／损坏索引／不足空间／工作超时不丢原权威记录 |
| Q06 | 三入口正常队列、冻结目标、S1、S3、失败与拒学终结、梦境暂存／回流；prepare只返回本入口S2／S1，other_pending只数量／时间，双持有移交不重计 |
| Q07 | 缺persona、无状态、无目标、真实索引零命中、索引未就绪、数据库故障、缺真实模型分别匹配COMPLETE／DEGRADED／ABSENT／FAILED；合成发布不能通过真实能力断言 |
| Q08 | 真实HTTP认证／过期／撤权、错入口、跨宿主票据、独立深读权限、头体超限／重复键／慢请求／连接断开／并发饱和；旧GET观察HTTP继续兼容 |
| Q09 | 真实票据COMMIT前后屏障及HTTP写出前断开，跨进程原键确认；未确认不返正文，响应丢失不自动强化，票据不重放删除旧正文 |
| Q10 | 24小时界点、时钟回退／重启、全部revision匹配、换反馈键、混合已用／未用、并发首次使用、单项非法整组回滚；F/H边界19→27→重新召回再35，100饱和、belief不变 |
| Q11 | 反馈先／删除先，遗忘期内／到期、票有效但对象已删，源最后释放／共享引用、模式切换双向竞争；无原ID复活、无票据隐性媒体保护 |
| Q12 | 四owner真实初始化、goals七张业务表为空；任一owner／必要审计失败整体回滚；元数据缺失／重复／损坏／错绑定不进入READY。必要slot缺失／篡改、提交未知、结果绑定／原键重开、诊断关闭／故障；合法非空targets的原回执与全部必要审计target_refs逐项一致（ID、前后revision、顺序），含真实进度根及跨进程确认；零写入不造对象／修订／占位审计。真实SQLite故障与注入分别标记，不因诊断失败漏写审计 |
| Q13 | 状态活动更换／心跳／子状态／缺起点／陈旧／偏移／未来时间／结束／旧revision；多个目标、deadline冲突、精确合并、来源／别名上限、别名旧revision、语义依赖缺失 |
| Q14 | 创建临近／过期、修改到过去、lead=0、跨专注／去重等待、完成与发送双向顺序；DISABLED明确未发送；获准TEST_HTTP用真实接收端、确认丢失／进程退出UNKNOWN、恢复零重发 |
| Q15 | 全参数及最大合法编码逐字段计数：94命令／分支、26新表及必要索引、叶、结果、票据、审计、完整定义和值、1 MiB初始化及新旧静态装配；逐项核对§9.2保守上界；完整94命令新装配在PersistenceService构造入口通过全部静态绑定校验，直接映射却声明targets下限0的反例须在装配时拒绝，即使其handler声称总返回非空。词项部分的F资格按§10.1已批准口径，精确最大合法词项及4096可达性另列最大压力资格缺口，不以此替代其他完整编码边界。按下表F/P/X预定规模分别执行，P档核§8一秒与分布，X档核最大压力；清理／签票持续试验另计，超限不截原文、不删慢样本 |
| Q16 | 资源任务超时但真实仍占用、连接close抛错、重复close、清理通知先后、读代次释放、根目录重开隔离；旧回执不变、INCOMPLETE不假释放 |
| Q17 | 从基线旧程序／对应旧装配创建真实临时旧库，再用未来版本对应旧装配重开／原键／继续操作；新装配与旧库双向拒绝、新库跨进程恢复；不用用户存量库 |
| Q18 | 相关回归＋全量unittest、锁定全量Pyright覆盖源码／测试／新增文件；Pylance独立记录。获准范围内Linux真实容器执行，记录平台／镜像摘要／资源／原始输出／逐项指纹；macOS结果不代替Linux |

<a id="f-qualification"></a>

### 10.1 分级资源与验收地位（F已授权，P/X待确认）

F档已获功能／恢复及范围内资源验证授权，P与X仅在各自资源及测试权限批准后执行。下表磁盘均是独立测试卷候选，包含SQLite／WAL／日志／媒体／证据的总预算；小样用于估计物理放大和判断是否批准下一档，不能把小样结果替代原定规模。资源不足或碰停止条件，记录该档INCOMPLETE及原数据量，申请调整后另起剖面，不删除失败／慢样本后算通过。

**F词项验收口径调整已获用户批准：** 采用合法2237词项正式对象的实际持久化、索引发布、查询及新进程恢复，结合4096分词硬限及超限检查、合法正文归一化超限后的拒绝和恢复证据。精确最大合法词项及4096可达性保留为最大压力资格缺口，不再作为F收尾门槛。2237是已达到的合法下界，不称已证明最大合法对象；运行时上限及其他F规模、满返回、恢复和失败保障均不变。实际证据与技术验收范围由[STATUS](../work/STATUS.md)定位，不在本正文复制执行记录。

| 档位／固定数据量 | 候选资源预算（未核验） | 写入停止条件／验收地位 |
| --- | --- | --- |
| F：小规模功能／恢复 | 0、100、1000正式对象；≤100遗忘、100目标、3入口各100输入；票池通常≤1000，10000票边界用独立子案例；含本节已批准词项案例、满返回极值及两代次重建；媒体≤100 MiB。候选2 vCPU／4 GiB RAM／20 GiB自有卷；受控持久操作总≤20000 | 测试目录累计12 GiB、卷可用≤4 GiB或操作20000任一先到即停止新造数／写负载，保留确认与收尾空间；不能用清理有效依据续跑。证明Q01–Q14/Q16–Q17功能和跨进程协议；可先测中文60组；不证明100000规模一秒或长期稳定。新全量类型／回归检查资源另记录 |
| P：典型性能 | 100000正式对象，其中10000遗忘、每对象128词项、完整快照≤4096；1000目标、3入口各1000输入；一活动一构建代次约6.104 GiB逻辑索引；媒体≤1 GiB。候选4 vCPU／8 GiB RAM／100 GiB自有卷；持久操作总≤300000，包含造数、索引页／对象、查询、反馈、状态与清理 | 目录累计60 GiB、卷可用≤20 GiB或操作300000任一先到停写；先单独记录建库开销，预算不足不暗减正式对象。按§8并发1/2、4请求/s十分钟、有足够票槽的原峰值验一秒；再独立24小时≤0.08票/s持续与到期清理试验，使用原TTL实际等待，跨进程恢复和重开耗时单列。未完成持续试验不能称长期容量通过 |
| X：最大规模压力 | 100000正式对象、每对象4096词项（上界可达性尚未证明，须先构造合法规范化材料；若不可达须另报可达最大剖面，不能称4096压力通过）；1000目标、3入口各1000输入；10000占槽边界；两代次词项约195.313 GiB逻辑量。候选8 vCPU／16 GiB RAM／1 TiB自有卷；媒体≤1 GiB，持久操作总≤1000000 | 目录累计700 GiB、卷可用≤200 GiB或操作1000000任一先到停写；容量监控失联／无法量化余量也停新写。测最大合法记录、重建／删除竞争、回收、超限失败与恢复，统计响应分布但不承诺一秒。X未完成时只可发布F/P已验证支持范围，最大压力资格明确缺失 |

阈值由测试父进程在每轮有界造数／请求前监测；在途写可能继续增长，须预留WAL及关闭／确认余量，不能把阈值当硬物理沙箱或允许填满主机。磁盘全量故障仅在另批的有限自有子卷／配额案例注入。达到停止条件不删未决库；先结束／隔离真实owner，保存原始证据再按已批准保留／清理权限处理。每档同时记录逻辑payload、实际db/index/WAL、RSS峰值、操作数量及剩余空间；100 GiB／1 TiB不是用户必须立即准备的环境，也不是已证明足够。

### 10.2 各档共用环境与权限缺口

| 环境项目 | 候选目标／准备清单 | 当前缺口与测试权限 |
| --- | --- | --- |
| Linux Docker架构 | 首选原生linux/arm64，另列linux/amd64发布资格；一容器、单实例／调度领导者、非root、私有监听，自有本地持久卷，无GPU | 目标机器架构、Docker版本／可用daemon、基础镜像及摘要、SQLite编译能力／Unicode版本尚未核验；amd64在arm宿主仿真须标EMULATED，不能代替原生性能 |
| CPU／内存 | 按F/P/X表逐档选择；P/X无swap性能包；同步并发2＋索引1，所有旧日志／媒体工作一并计入 | 不是最低需求或实测RSS；实际主机CPU、容器CPU配额、磁盘介质及可用内存未知。测试须有记录cgroup／峰值RSS／CPU和容器统计的权限 |
| 磁盘 | F档候选20 GiB；100 GiB与1 TiB仅为以后P/X单独评估候选，按上表止损及验收地位执行 | 页面／辅助索引／WAL／审计／备份可能继续放大；1 TiB亦非已证明足够。应先小样测实际放大再批准最大样本，禁止填满用户磁盘；可用空间和卷类型未核验 |
| 临时存储／恢复 | 测试父进程保留database_id、配置和资源绑定；真实SQLite、独立解释器、COMMIT屏障、原进程结束／隔离后重开 | 需创建／清理自有目录、文件锁／fsync／有限子进程终止的测试权限；只读／页上限故障用自有库，不改系统权限或真实生产库 |
| HTTP／提醒 | 真实loopback业务与观察服务器、隔离真实TEST_HTTP接收器，无公网入站 | 需本机／容器回环端口权限；路由身份和外发范围须明确。默认关闭提醒外发，真实供应商密钥／额度／出站网络另审，不作为本地包前置 |
| 工具链／依赖 | Python 3.12、项目锁定依赖与Node／Pyright；不新增分词／向量依赖即可验证推荐本地模式 | 须实查本机与容器内部版本，不复用历史环境结论；F档依赖／镜像准备与范围内验证已授权，P/X仍待确认；当前环境及Pylance检查事实见工作记录 |

未来执行命令应按[类型检查规范](../CODING_STANDARDS.md#python-type-checking)及批准后的实际测试文件确定并记录完整argv／退出码／原始输出；本文不生成替代启动指南或不存在的测试命令。每份证据绑定实际提交／工作区、受测文件清单和指纹，区分ACTUAL存储／HTTP、SIMULATED模型、SYNTHETIC业务材料、注入故障与真实进程中断。完成整阶段实现、自查、测试与范围内修复后停在监督技术审查，由用户手动转交；不提交。
