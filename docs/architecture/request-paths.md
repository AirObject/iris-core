# 同步时限、数据所有权草图与外部接口

> 本文件是权威原文的阅读视图，不是独立需求。原文仍是权威依据；后续修改规则时，先更新对应原文，再同步本视图与[覆盖映射](../work/ORGANIZATION_REPORT.md#coverage)。保留原文“已确定、建议、示例、待确认”的性质；下列导读不新增决策。正文含原有编号，仅限文档追踪。

适用主题与局部定义：基础响应与可选rerank分开计时。表名、字段和端口是表达责任的草图；模块编号是文档追踪信息，不进入实现。

来源：[原文 L143–L181](../../companion_memory_module_design_provider_logging_config.md#section-03)；[原文 L951–L1021](../../companion_memory_module_design_provider_logging_config.md#section-12)。行号对应整理时的哈希基线。

按关联工作联合阅读：[外部产品行为](../product/retrieval.md)；[持久化与事务](persistence-and-transactions.md)；[检索模块](../modules/retrieval.md)；[Provider请求控制](provider.md)。

返回[文档总入口](../INDEX.md)；实际进度见[工作状态](../work/STATUS.md)。

<a id="section-03"></a>

<a id="source-line-145"></a>

## 3. 同步时延与异步工作边界

<a id="source-line-147"></a>

### 3.1 一秒接口契约

本草稿将一秒目标落实为**回复准备、普通查询和深度召回的基础响应截止预算**：从API收到可处理请求到形成完整返回，包含内部排队、查询embedding、检索、过滤、召回凭据持久化和序列化。可选rerank另记耗时。客户端端到端耗时还需预留网络和上传开销。

这不是将一秒要求替换为“平均一秒”或“首个token一秒”。实测报告同时给出完整响应时延分布、超时和错误比例；专注拒绝、错误响应、降级成功与完整成功分开统计。

远程embedding不能从这一秒中排除。草稿采用本地基础召回始终可运行的路径，远程查询embedding仅在剩余预算内作为增强。若要求每次必须等待新的远程向量再成功返回，则不能同时无条件保证一秒；此时需要批准更长时限或失败响应，而非隐瞒等待。

<a id="source-line-155"></a>

### 3.2 回复准备路径

```text
请求 → 鉴权/绑定入口/检查模式
     → 本地读取persona、当前状态、目标、本入口近期输入、其他入口计数
     → 结构化/实体/词法基础检索
     → 已缓存查询向量直接使用；未命中时按短截止预算请求embedding
     → 有向量则加入向量候选；超时/故障则使用基础候选
     → 去重/有限关系展开/核验生命周期与修订
     → 可选rerank（单独预算）
     → 再次核验模式/对象有效性 → 保存召回记录 → 返回分区信息
```

查询不调用生成式LLM改写问题、生成persona或临时总结来源。外部可以提交结构化主体/事件信息帮助检索，但不能被要求额外生成一份LLM解释才能使用接口。

建议基础服务内部以700—800毫秒为工作目标，为序列化和链路留余量；远程查询embedding预算可先测试200—300毫秒量级。数值是试验起点，不是实测保证。超预算后不能继续等底层默认长重试，应使用同一个绝对截止时间约束所有尝试。

返回应明确`retrieval_mode`、`degraded_reason`、`index_revision`、`persona_revision`、`observed_at`和基础/embedding/rerank耗时。词法降级可能降低语义召回质量，不得声称与完整向量检索等价。没有相关内容时可以为空；数据库故障则应返回故障，不能返回空记忆冒充成功。

rerank默认关闭，开启后只处理有上限的候选集合；远程失败或超时按配置回退原排序并标明。最终序列化、对象核验等开销仍属于基础耗时，不藏入rerank计时。

<a id="source-line-176"></a>

### 3.3 写入类请求

输入、当前状态、使用反馈和目标注入，以短事务完成持久化后回复。需要模型的后续工作不阻塞接收确认。目标注入先创建对象、返回其标识及`dedup_pending`，再执行相似去重；不会因为一秒时限改变“先加入再去重”。

大媒体使用独立上传/导入步骤，先取得持久化`blob_id`，再把事件与其关联。不能在上传尚未完成时承诺完整附件已持久化。只接收到外部URL时，仅能确认URL元信息，不等于原文件已经保存；必须提供明确的附件状态或要求先完成导入。


<a id="section-12"></a>

<a id="source-line-953"></a>

## 12. 数据与接口草图

<a id="source-line-955"></a>

### 12.1 权威业务数据分组

| 数据组 | 拥有模块 | 关键约束 |
| --- | --- | --- |
| `hosts / entries / access_bindings` | [M01](../modules/ingress.md#contract) | 稳定身份、缓存入口授权 |
| `runtime_modes / work_items / work_leases` | [M02](../modules/runtime.md#contract) | 持久化mode_epoch、单次工作归属、无终态重放 |
| `raw_inputs / queue_members / batches / batch_members` | [M03](../modules/buffers.md#contract) | 同入口有序、三段角色、批次唯一目标、引用不冒充学习成功 |
| `blobs / blob_refs / media_interpretations` | [M04](../modules/media.md#contract) | 文件hash唯一、业务引用独立、理解带profile和状态 |
| `cognitive_runs / staged_change_sets / context_manifests` | [M05](../modules/cognition.md#contract) | 调用在事务外、候选持久化；只引用[M13](../modules/provider.md#contract)请求ID，不另存attempt账本 |
| `memories / entities / relations / source_snapshots / source_links` | [M06](../modules/memory.md#contract) | 当前值、真实来源、双指标、对象级生命周期 |
| `usage_receipts / dirty_dependencies` | [M06](../modules/memory.md#contract) | 使用幂等、受影响对象延迟整理 |
| `persona_candidates / persona_publication` | [M07](../modules/self-model.md#contract) | 只读当前发布，候选与发布分开 |
| `embedding_artifacts / index_work / index_manifests / recall_records` | [M08](../modules/retrieval.md#contract) | 模型空间、修订核验、持久化已付费结果、可重建索引 |
| `current_states` | [M09](../modules/state.md#contract) | 外部权威、开始与更新时间分开 |
| `goals / goal_aliases / goal_dedup_work / reminder_plans / deliveries` | [M10](../modules/goals.md#contract) | 多目标、先加入后去重、期限不被梦境顺延 |
| `dream_runs / dream_steps` | [M11](../modules/dream.md#contract) | 阶段检查点、增量工作、persona发布结果 |
| `management_sessions / setup_progress / admin_permissions` | [M12](../modules/management.md#contract) | 只做管理流程和授权，不存第二份模型/日志/配置真相 |
| `provider_requests / provider_attempts / provider_result_handoffs` | [M13](../modules/provider.md#contract) | 请求与attempt分层、发送前登记、结果交接可恢复，原文受控 |
| `provider_usage / cost_items / budget_reservations / usage_aggregates` | [M13](../modules/provider.md#contract) | 已知/估算/未知分开、价目版本、幂等结算、统计不受日志等级影响 |
| `audit_events / log_segments / log_index_checkpoints` | [M14](../modules/logging.md#contract) | 审计同事务；运行日志文件为主、历史索引可重建；不提供给agent |
| `config_schema / config_versions / config_activations / effective_snapshots / secret_refs` | [M15](../modules/configuration.md#contract) | 类型校验、作用域、生效状态、快照引用、秘密不明文回显 |
| `prompt_versions / pricing_versions` | [M15](../modules/configuration.md#contract) | 外部监督与价目配置统一版本；[M13](../modules/provider.md#contract)/[M05](../modules/cognition.md#contract)/[M07](../modules/self-model.md#contract)只消费快照 |

表名是表达数据责任的草案，不要求逐字建表。允许为性能合并表或增加只读投影，但不能消失其语义。队列成员和批次成员可以共享不可变原始消息；并非每出现一次就复制整条JSON。

<a id="source-line-980"></a>

### 12.2 外部能力接口

路径仅示例，正式OpenAPI/SDK在接口阶段冻结。

| 能力 | 草拟行为 | 专注模式 |
| --- | --- | --- |
| 输入接收 | 事件持久化，返回event_id与NORMAL_QUEUED/DREAM_QUEUED | 允许，仅暂存 |
| 媒体上传 | 流式保存原件，返回blob_id与持久化状态，不运行理解 | 作为原始输入附件接收允许 |
| 回复准备 | 返回persona、相关记忆、本入口近期输入、状态、目标及数量/时间提示 | 拒绝 |
| 普通查询 | 仅有效正式记忆 | 拒绝 |
| 深度召回 | 可含遗忘对象，读取不恢复 | 拒绝 |
| 实际使用反馈 | 验证recall_id及版本，原子增强与检查阈值 | 拒绝且不排队 |
| 当前状态操作 | 外部更新与结束，提交后确认 | 拒绝且不排队 |
| 目标注入/状态操作 | 先创建再去重；完成/放弃/截止更新 | 拒绝且不排队 |
| Web运行状态 | 模式、各入口积压、故障、索引覆盖、成本等脱敏只读视图 | 允许 |
| Provider统计/请求元信息 | 查询已持久化调用、尝试、usage、费用、未知项和预算；不含原始prompt/密钥 | 只读允许 |
| Provider连接测试/模型试调用 | 受控测试且计入[M13](../modules/provider.md#contract)诊断调用，不返回秘密 | 拒绝 |
| 运行日志查询/订阅/导出 | [M14](../modules/logging.md#contract)分页/游标与过滤，始终脱敏，导出受权限和资源预算限制 | 只读状态范围允许 |
| 配置Schema/当前生效状态 | 返回类型、来源、版本、apply_mode、迁移/重启提示，秘密掩码 | 只读允许 |
| 配置变更/回退/密钥更新 | expected_revision+校验+生效计划+审计 | 拒绝且不排队 |
| 开发者审计 | 独立管理权限；完整历史正文不属于普通状态观察，不作为记忆召回入口 | 仅脱敏操作状态；不默认开放正文 |

敏感拒学、普通失败与DREAMING使用稳定的业务状态区分。正常请求超时不能被网关统一包装成空成功。专注期媒体上传的例外只保存字节与引用，不扩大为媒体理解、召回或来源读取例外。

<a id="source-line-1004"></a>

### 12.3 回复准备的逻辑返回

```text
ReplyInformation
  request_id / recall_id
  observed_at / runtime_mode / mode_epoch / config_snapshot_id
  persona: {text, revision, generated_at, review_status}
  memories: [{id, revision, kind, belief, lifecycle, content, provenance_metadata}]
  recent_context: {entry_id, messages, scope: current_entry_only}
  current_state: {activity, fields, started_at, updated_at, duration, time_basis}
  goals: [{id, status, deadline, reminder_lead, suggestion}]
  other_pending: {entry_count, earliest_at, latest_at}
  retrieval: {mode, degraded_reason, index_revision, index_coverage}
  timing: {base_ms, query_embedding_ms, rerank_ms, total_ms}
```

`other_pending`不包含其他入口正文、摘要或人物。`provenance_metadata`不隐式包含跨入口完整来源。`recent_context`不是正式记忆，因此不通过memory_id使用强化。rerank返回后仍需核对记忆状态，不直接照发之前的过期候选。
