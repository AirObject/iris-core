# 工程验收与产品要求映射完整表

> 本文件是权威原文的阅读视图，不是独立需求。原文仍是权威依据；后续修改规则时，先更新对应原文，再同步本视图与[覆盖映射](../work/ORGANIZATION_REPORT.md#coverage)。保留原文“已确定、建议、示例、待确认”的性质；下列导读不新增决策。正文含原有编号，仅限文档追踪。

适用主题与局部定义：保留V01—V90完整表及压测、模型质量验证要求；这些是未来验收条件，当前仓库没有测试实现或通过结果。

来源：[原文 L1095–L1197](../../companion_memory_module_design_provider_logging_config.md#section-14)。行号对应整理时的哈希基线。

按关联工作联合阅读：[产品验收](../product/acceptance.md)；[事务与恢复](persistence-and-transactions.md)；[实际工作状态](../work/STATUS.md)。

返回[文档总入口](../INDEX.md)；实际进度见[工作状态](../work/STATUS.md)。

<a id="section-14"></a>

<a id="source-line-1097"></a>

## 14. 工程验收与原需求映射

原需求文档的[A01](../product/acceptance.md#a01)—[A108](../product/acceptance.md#a108)继续约束产品行为；下表补充当前工程约束产生的验收，不表示已实现或已压测通过。

| ID | 工程验收 | 主要模块/契约 |
| --- | --- | --- |
| <a id="v01"></a>V01 | 正常输入接收成功后强制结束进程，重启仍有相同消息与队列位置 | [M01](../modules/ingress.md#contract)/[M03](../modules/buffers.md#contract)，[T01](persistence-and-transactions.md#t01) |
| <a id="v02"></a>V02 | 专注梦境接收的多入口积压重启后保持入口隔离与顺序 | [M02](../modules/runtime.md#contract)/[M03](../modules/buffers.md#contract)，[T01](persistence-and-transactions.md#t01)/[T06](persistence-and-transactions.md#t06) |
| <a id="v03"></a>V03 | ACK发送前断线，同幂等键重发不产生第二条事件 | [M01](../modules/ingress.md#contract)/[I01](ownership.md#i01)，[T01](persistence-and-transactions.md#t01) |
| <a id="v04"></a>V04 | 普通provider失败耗尽终结后重启，不重学原批次，S3正确留尾 | [M03](../modules/buffers.md#contract)/[M05](../modules/cognition.md#contract)，[T04](persistence-and-transactions.md#t04) |
| <a id="v05"></a>V05 | 敏感拒学终结后重启，S3为空，S1与新消息仍存在 | [M03](../modules/buffers.md#contract)，[T05](persistence-and-transactions.md#t05) |
| <a id="v06"></a>V06 | 保存候选后、正式事务前崩溃，只重试本地提交不额外调用LLM | [M05](../modules/cognition.md#contract)/[I01](ownership.md#i01)，[T03](persistence-and-transactions.md#t03) |
| <a id="v07"></a>V07 | [T03](persistence-and-transactions.md#t03)提交中断，记忆、来源、轮转、引用和审计全有或全无 | 多模块/[I01](ownership.md#i01)，[T03](persistence-and-transactions.md#t03) |
| <a id="v08"></a>V08 | 提交成功但确认丢失，再处理同操作ID不生成重复记忆或重复轮转 | [M03](../modules/buffers.md#contract)/[M06](../modules/memory.md#contract)/[I01](ownership.md#i01) |
| <a id="v09"></a>V09 | 远程结果未知不伪装成本地事务已撤销远程账单 | [M05](../modules/cognition.md#contract)/[M13](../modules/provider.md#contract)，恢复契约 |
| <a id="v10"></a>V10 | 同一媒体字节被多次上传只保存一份blob，但各次消息与来源独立 | [M04](../modules/media.md#contract)/[M03](../modules/buffers.md#contract) |
| <a id="v11"></a>V11 | 文件发布后数据库提交前崩溃，只留下可清理孤立文件，不确认完整事件 | [M04](../modules/media.md#contract)/[I01](ownership.md#i01) |
| <a id="v12"></a>V12 | 有S3、梦境积压或来源引用的媒体不被GC删除 | [M04](../modules/media.md#contract)/[M03](../modules/buffers.md#contract)/[M06](../modules/memory.md#contract) |
| <a id="v13"></a>V13 | 媒体仅剩审计引用可以清理，agent不能借旧链接找回 | [M04](../modules/media.md#contract)/[M14](../modules/logging.md#contract) |
| <a id="v14"></a>V14 | 外部已有媒体理解零次内部理解；同blob情境解释不错误复用 | [M04](../modules/media.md#contract)/[M13](../modules/provider.md#contract) |
| <a id="v15"></a>V15 | 媒体敏感占位不导致整批失败，不自动换模型反复理解 | [M04](../modules/media.md#contract)/[M05](../modules/cognition.md#contract) |
| <a id="v16"></a>V16 | 普通查询无embedding缓存且API很慢，在预算内词法降级并明确标记 | [M08](../modules/retrieval.md#contract)/[M13](../modules/provider.md#contract) |
| <a id="v17"></a>V17 | rerank默认不发生请求；启用后基础与rerank时间分别计算 | [M08](../modules/retrieval.md#contract)/[M13](../modules/provider.md#contract) |
| <a id="v18"></a>V18 | 全文/向量/缓存候选包含已删除或已遗忘对象时，按查询模式正确过滤 | [M08](../modules/retrieval.md#contract)/[M06](../modules/memory.md#contract) |
| <a id="v19"></a>V19 | 同一使用反馈跨重启重复提交仅强化一次 | [M06](../modules/memory.md#contract)/[M08](../modules/retrieval.md#contract)，[T07](persistence-and-transactions.md#t07) |
| <a id="v20"></a>V20 | 深度读取后无实际使用反馈不恢复；达到H才恢复 | [M06](../modules/memory.md#contract)/[M08](../modules/retrieval.md#contract)，[T07](persistence-and-transactions.md#t07) |
| <a id="v21"></a>V21 | 保存embedding后重启重建本地索引，不重复请求已存在相同空间的向量 | [M08](../modules/retrieval.md#contract)/[I02](ownership.md#i02) |
| <a id="v22"></a>V22 | 索引构建落后或损坏不丢正式记忆，明确降级及覆盖水位 | [M08](../modules/retrieval.md#contract)/[M06](../modules/memory.md#contract) |
| <a id="v23"></a>V23 | 新模型空间不混入旧向量索引；旧修订embedding不能覆盖新正文 | [M08](../modules/retrieval.md#contract)/[M13](../modules/provider.md#contract) |
| <a id="v24"></a>V24 | 专注模式重启后先恢复门控，Web可查但业务不能短暂放行 | [M02](../modules/runtime.md#contract)/[M12](../modules/management.md#contract) |
| <a id="v25"></a>V25 | 回流移交中断不丢消息、不重复消费，后到消息不超越旧积压 | [M03](../modules/buffers.md#contract)，[T06](persistence-and-transactions.md#t06) |
| <a id="v26"></a>V26 | 梦境步骤重复恢复不重复扣分，不发布半份persona | [M11](../modules/dream.md#contract)/[M07](../modules/self-model.md#contract)，[T09](persistence-and-transactions.md#t09) |
| <a id="v27"></a>V27 | 当前状态及活动开始时间跨重启保留，持续时间不从重启时归零 | [M09](../modules/state.md#contract) |
| <a id="v28"></a>V28 | 目标注入一秒内返回已持久化对象与去重状态，后续合并保留外部约束 | [M10](../modules/goals.md#contract) |
| <a id="v29"></a>V29 | 外部完成目标后崩溃恢复，不重新建立旧提醒 | [M10](../modules/goals.md#contract)，[T08](persistence-and-transactions.md#t08) |
| <a id="v30"></a>V30 | 正常/梦境/媒体调用的预算分开，预算暂停不冒充敏感拒学或成功 | [M02](../modules/runtime.md#contract)/[M05](../modules/cognition.md#contract)/[M13](../modules/provider.md#contract) |
| <a id="v31"></a>V31 | 不支持工具或媒体的兼容模型返回能力错误，不静默删除关键输入 | [M13](../modules/provider.md#contract) |
| <a id="v32"></a>V32 | SDK和网关不嵌套倍增重试；在线请求使用统一绝对截止 | [M13](../modules/provider.md#contract) |
| <a id="v33"></a>V33 | 中文两字昵称、别名、中英混排和改名场景具有可测召回质量 | [M08](../modules/retrieval.md#contract)/[I02](ownership.md#i02) |
| <a id="v34"></a>V34 | 原需求[A01](../product/acceptance.md#a01)—[A108](../product/acceptance.md#a108)中已确定规则映射到自动化或可重复人工验收 | 所有模块 |
| <a id="v35"></a>V35 | Linux容器重建保留卷；本地绑定端口、不暴露数据目录；密钥不进prompt | 部署/[M12](../modules/management.md#contract)/[M13](../modules/provider.md#contract)/[M15](../modules/configuration.md#contract) |
| <a id="v36"></a>V36 | 从备份恢复队列、媒体、来源、persona与目标，核对一致性而非只检查能启动 | [I01](ownership.md#i01)/[M12](../modules/management.md#contract) |
| <a id="v37"></a>V37 | 所有学习、梦境、persona、媒体、embedding和rerank请求只能由Provider发送；架构测试阻止业务SDK直连 | [M13](../modules/provider.md#contract)/架构测试 |
| <a id="v38"></a>V38 | Web模型测试同样带caller和purpose并计量；只读统计绝不额外调用模型 | [M12](../modules/management.md#contract)/[M13](../modules/provider.md#contract) |
| <a id="v39"></a>V39 | 一次逻辑请求两次网络尝试后成功，统计为1个逻辑请求/2次attempt且费用按attempt记录 | [M13](../modules/provider.md#contract)，[T10](persistence-and-transactions.md#t10)/[T11](persistence-and-transactions.md#t11) |
| <a id="v40"></a>V40 | 失败或取消请求无usage时显示未知而不是0；传输成功与业务校验失败可以并存 | [M13](../modules/provider.md#contract)/[M05](../modules/cognition.md#contract) |
| <a id="v41"></a>V41 | OpenAI输入缓存/输出reasoning子项不重复相加，Anthropic缓存输入按适配器口径归一 | [M13](../modules/provider.md#contract)协议契约 |
| <a id="v42"></a>V42 | 流式累计usage反复到达不重复计token，终结回调重入不重复扣费 | [M13](../modules/provider.md#contract)，[T11](persistence-and-transactions.md#t11) |
| <a id="v43"></a>V43 | 批量embedding一请求多文本，与多个HTTP请求统计明确区分 | [M13](../modules/provider.md#contract)/[M08](../modules/retrieval.md#contract) |
| <a id="v44"></a>V44 | rerank可按token以外的供应商单位统计；不以候选文档数伪装付费请求数 | [M13](../modules/provider.md#contract) |
| <a id="v45"></a>V45 | 本地/外部结果复用无网络attempt，不重复记入历史产物生成费用 | [M04](../modules/media.md#contract)/[M08](../modules/retrieval.md#contract)/[M13](../modules/provider.md#contract) |
| <a id="v46"></a>V46 | 媒体能力底层复用生成适配器时，同一个出站attempt只计一次费用 | [M13](../modules/provider.md#contract) |
| <a id="v47"></a>V47 | 同账户多个profile共享限额；并发预算预留不能各自超发 | [M13](../modules/provider.md#contract)/[I01](ownership.md#i01) |
| <a id="v48"></a>V48 | 多次尝试共同使用一个绝对deadline；SDK与业务层不再叠加重试 | [M13](../modules/provider.md#contract) |
| <a id="v49"></a>V49 | 预算不足暂停未启动工作，不转为敏感拒学或普通失败来清空队列 | [M02](../modules/runtime.md#contract)/[M03](../modules/buffers.md#contract)/[M13](../modules/provider.md#contract) |
| <a id="v50"></a>V50 | 敏感拒绝不自动切备用供应商；embedding fallback不跨不兼容空间 | [M13](../modules/provider.md#contract)/[M08](../modules/retrieval.md#contract) |
| <a id="v51"></a>V51 | Provider账本/预算预留跨重启保留，聚合重做同一checkpoint不重复累加 | [M13](../modules/provider.md#contract)，[T10](persistence-and-transactions.md#t10)/[T11](persistence-and-transactions.md#t11)/[T13](persistence-and-transactions.md#t13) |
| <a id="v52"></a>V52 | 调整日志级别、关闭Web或日志轮转后Provider统计结果不变 | [M13](../modules/provider.md#contract)/[M14](../modules/logging.md#contract) |
| <a id="v53"></a>V53 | 价格变更不改历史原始估算；未知价格单列，多币种不直接相加 | [M13](../modules/provider.md#contract)/[M15](../modules/configuration.md#contract) |
| <a id="v54"></a>V54 | 平均延迟与分位数使用正确样本，不平均分窗口p95；图表显示覆盖区间 | [M13](../modules/provider.md#contract)/[M12](../modules/management.md#contract) |
| <a id="v55"></a>V55 | Provider账本存储失败阻止新付费尝试；诊断sink故障与账本故障分别处理 | [M13](../modules/provider.md#contract)/[M14](../modules/logging.md#contract)/[I01](ownership.md#i01) |
| <a id="v56"></a>V56 | 进程在远程返回落盘前退出，遗留attempt显示REMOTE_RESULT_UNKNOWN，不伪装零成本或自动退款 | [M13](../modules/provider.md#contract)恢复契约 |
| <a id="v57"></a>V57 | 五个标准等级和NOTSET语义一致，ERROR不等于进程退出，AUDIT不作为新增severity | [M14](../modules/logging.md#contract) |
| <a id="v58"></a>V58 | console INFO、file DEBUG、Web WARNING时按各自等级输出，采集层不误吞文件所需DEBUG | [M14](../modules/logging.md#contract) |
| <a id="v59"></a>V59 | 同一事件三个sink使用同一event_id；重复初始化或父logger传播不重复输出 | [M14](../modules/logging.md#contract)/bootstrap |
| <a id="v60"></a>V60 | 所有模块与SDK诊断桥接统一，秘密字段在入队前脱敏，异常栈不泄漏Authorization | [M14](../modules/logging.md#contract)/[M13](../modules/provider.md#contract) |
| <a id="v61"></a>V61 | Web日志支持实时/分页/过滤/断线游标，过期游标明确LOG_GAP；慢订阅者不阻塞业务 | [M14](../modules/logging.md#contract)/[M12](../modules/management.md#contract) |
| <a id="v62"></a>V62 | 日志文件轮转、压缩和保留清理正确，当前活动文件和业务blob不被误删 | [M14](../modules/logging.md#contract)/[I02](ownership.md#i02) |
| <a id="v63"></a>V63 | 诊断队列饱和按策略处理并报告dropped_events，不无限占用内存或阻塞一秒查询 | [M14](../modules/logging.md#contract)/[M08](../modules/retrieval.md#contract) |
| <a id="v64"></a>V64 | sink异常有独立应急通道和健康指标，不递归记录导致死锁或无限日志 | [M14](../modules/logging.md#contract) |
| <a id="v65"></a>V65 | 强制崩溃可丢未刷诊断尾部但已提交审计/Provider账本仍存在，页面不承诺无限日志保留 | [M14](../modules/logging.md#contract)/[M13](../modules/provider.md#contract)/[I01](ownership.md#i01) |
| <a id="v66"></a>V66 | 日志级别ERROR或诊断关闭不影响必须的业务/配置审计入库 | [M14](../modules/logging.md#contract)/[M15](../modules/configuration.md#contract)，[T13](persistence-and-transactions.md#t13) |
| <a id="v67"></a>V67 | 审计入库失败时受审计写命令不报告成功；文件投递失败不回滚已提交业务 | [M14](../modules/logging.md#contract)/[I01](ownership.md#i01) |
| <a id="v68"></a>V68 | agent不能读取runtime/audit/usage正文或通过日志找回删除记忆；开发者查看不强化记忆 | [M14](../modules/logging.md#contract)/[M06](../modules/memory.md#contract)/[M12](../modules/management.md#contract) |
| <a id="v69"></a>V69 | Web安全转义日志正文，导出只读允许路径，带令牌URL和secret均不回显 | [M14](../modules/logging.md#contract)/[M12](../modules/management.md#contract) |
| <a id="v70"></a>V70 | 诊断内容捕获有范围/期限/上限，自动到期且仍不记录密钥 | [M14](../modules/logging.md#contract)/[M15](../modules/configuration.md#contract) |
| <a id="v71"></a>V71 | 日志热修改安全更换handler，不产生双输出/窗口丢失；旧sink排空后关闭 | [M14](../modules/logging.md#contract)/[M15](../modules/configuration.md#contract) |
| <a id="v72"></a>V72 | Web拒绝任意可执行logging配置和任意类导入，不开放原生未鉴权配置socket | [M14](../modules/logging.md#contract)/[M15](../modules/configuration.md#contract) |
| <a id="v73"></a>V73 | 业务模块没有直接getenv/私有YAML/带私设默认值的配置旁路 | [M15](../modules/configuration.md#contract)/架构测试 |
| <a id="v74"></a>V74 | 每个参数可追溯类型、单位、默认值、作用域、权限、apply_mode；未知键被拒绝 | [M15](../modules/configuration.md#contract) |
| <a id="v75"></a>V75 | F/H、三段、模型预算和日志路径跨字段校验失败，不发布部分配置 | [M15](../modules/configuration.md#contract)/[M03](../modules/buffers.md#contract)/[M06](../modules/memory.md#contract)/[M13](../modules/provider.md#contract)/[M14](../modules/logging.md#contract) |
| <a id="v76"></a>V76 | 环境强制覆写在Web显示来源/锁定；无效Web保存不显示已生效 | [M15](../modules/configuration.md#contract)/[M12](../modules/management.md#contract) |
| <a id="v77"></a>V77 | expected_revision冲突返回冲突，不覆盖另一管理员修改 | [M15](../modules/configuration.md#contract)，[T12](persistence-and-transactions.md#t12) |
| <a id="v78"></a>V78 | 普通检索参数与rerank开关仅对新请求生效，在途请求配置和deadline不跳变 | [M15](../modules/configuration.md#contract)/[M08](../modules/retrieval.md#contract)/[M13](../modules/provider.md#contract) |
| <a id="v79"></a>V79 | S2/S3配置热修改不改变冻结批次及本轮留尾；下个批次采用新快照 | [M15](../modules/configuration.md#contract)/[M03](../modules/buffers.md#contract) |
| <a id="v80"></a>V80 | persona监管prompt在下次梦境生效，不在普通请求或当前梦境中途改写 | [M15](../modules/configuration.md#contract)/[M07](../modules/self-model.md#contract)/[M11](../modules/dream.md#contract) |
| <a id="v81"></a>V81 | 专注梦境拒绝配置写入/回退/试调用，只允许脱敏日志、Provider统计和配置状态观察 | [M02](../modules/runtime.md#contract)/[M12](../modules/management.md#contract)/[M13](../modules/provider.md#contract)/[M14](../modules/logging.md#contract)/[M15](../modules/configuration.md#contract) |
| <a id="v82"></a>V82 | 配置prepare失败保留旧有效版本；DB指针提交后崩溃，启动先恢复快照再开放业务 | [M15](../modules/configuration.md#contract)/[M02](../modules/runtime.md#contract)/[I01](ownership.md#i01) |
| <a id="v83"></a>V83 | 配置回退生成新版本和审计，不撤销已发生调用、费用或已删除文件 | [M15](../modules/configuration.md#contract)/[M14](../modules/logging.md#contract) |
| <a id="v84"></a>V84 | embedding空间变更先迁移后切换；只轮换key/调价/修改超时不重算全库向量 | [M15](../modules/configuration.md#contract)/[M08](../modules/retrieval.md#contract)/[M13](../modules/provider.md#contract) |
| <a id="v85"></a>V85 | 配置密钥仅引用/掩码；secret轮换和禁用阻止不合法后续尝试，历史统计不泄漏旧key | [M15](../modules/configuration.md#contract)/[M13](../modules/provider.md#contract)/[M14](../modules/logging.md#contract) |
| <a id="v86"></a>V86 | 数据库路径/监听等重启字段保存为待重启，不声称立即生效 | [M15](../modules/configuration.md#contract)/bootstrap |
| <a id="v87"></a>V87 | 仍被批次、梦境或未知attempt引用的旧配置/产物不被配置GC清理 | [M15](../modules/configuration.md#contract)/[M03](../modules/buffers.md#contract)/[M11](../modules/dream.md#contract)/[M13](../modules/provider.md#contract) |
| <a id="v88"></a>V88 | 预算热降低不把已使用量清零，也不因旧快照绕过发送前的实时准入 | [M15](../modules/configuration.md#contract)/[M13](../modules/provider.md#contract) |
| <a id="v89"></a>V89 | 同一热变更涉及多个生效类别时显示分组与实际快照，不混用半套新旧字段 | [M15](../modules/configuration.md#contract)/[M12](../modules/management.md#contract) |
| <a id="v90"></a>V90 | 生成配置文档与默认值一致；日志/统计新增开销纳入基础一秒响应和存储压力测试 | [M15](../modules/configuration.md#contract)/[M14](../modules/logging.md#contract)/[M13](../modules/provider.md#contract)/[M08](../modules/retrieval.md#contract) |

压测至少分开统计：文本输入、媒体导入、当前状态高频更新、回复准备、普通/深度查询、学习批次提交、梦境和索引构建。以10万、100万条正式记忆建立测试档，依据实际累计增长增加更大档；输入平均速率与突发速率分别验证。没有CPU、内存、磁盘与峰值数据前，不承诺某个候选库达到所有档位的一秒要求。

模型质量另建小型可审查样例：只总结中段、引用解释、多人的不同说法、跨平台身份不确定、扮演、长文本、反讽、媒体占位、persona稳定及来源断裂。API协议通过不等于认知正确率通过。
