# Observation 全量上下文与批量总结实施说明

状态：Completed（开发实现与分段复验完成）。2026-09-08 用户批准在 main 实施；先将 dev/chores 快进合入 main，再修改相关文档和实现。当前已合入 7629152，原有内嵌接入和指南未提交改动逐文件保留。完整 CI 当次失败及修正后的复验结果见[验证报告](../reports/observation-context.md)，本文不代表生产验收。

## 已确认的产品决定

- 不新增独立环境记录库。宿主获授权的背景消息与直接交互都进入 Observation，入库不调用模型，也不做逐条相关性判断。
- Observation 继续表示已确认的原始事件，保留作者、事件时间、源事件身份和原文；摘要不改写原文、不成为新的外部效果证据。
- 新增背景/交互用途标记及可选来源线程、回复来源标识。旧客户端缺省保持交互语义；新增关联信息不是跨空间授权。
- 对外提供有界实时上下文：原始消息、已发布 Episode 摘要、来源引用、分页/截断与处理覆盖。读取不触发模型。
- 用户确认自动总结必须显式开启；开启后按数量或等待时间触发。未配置认知 Provider 时不生成假摘要，原始读写继续可用。
- 用户确认背景 Observation 默认保留 30 天，可配置。摘要、长期记忆等仍依赖的原文不参与普通到期删除；主动遗忘走既有删除账本和派生失效。
- 总结按明确 Scope 和有界批次分组，可选择不连续消息；无价值内容可不形成 Episode，但被处理不等于立即删除原始上下文。
- 保留直接 Observation 接口和原有字段语义；新接口和可选字段遵循兼容契约、独立 SDK 与内嵌入口。

## 数据与证据

Observation 增量元数据描述输入用途与平台关系。事件 ID、时间、角色、源游标仍由原有事务与去重路径处理。入库确认与总结完成是独立状态。

复用 Episode 的 summary / observation_refs / source_refs 保存批量结果。成员消息保存一次；摘要正文独立版本。每组须引用本批真实来源，未知 ID、重复成员、跨 Scope、超预算、已删除或变化来源拒绝提交。模型输入是数据，不能激活 Task、伪造发送成功或改写人格。

用户进一步确认：只有摘要明确引用的消息获得依赖保留；被忽略的同批消息继续按 30 天清理。每批限定同一隐私边界，摘要引用须支持摘要内容，摘要及来源一起授权；模型不得用未引用的消息支撑摘要事实。

批次持久保存处理成员、结果 Episode 引用和后台 Job 关联；空结果也是可解释的处理结果。按持久成员账本识别未处理消息，按事件时间提供上下文，迟到消息不会被单一发生时间水位跳过。并发扫描、重启、模型重试不得重复发布同一批次。

## 读取与窗口

实时读取以明确 Agent/Space/Session、用途及当前授权为边界。限制每页条数与输入时间范围，使用固定快照和游标；响应明确是否仍有后续数据。消息包含时间与关联元数据。

此接口中的已整理摘要来自新批次账本，旧 Episode 继续可通过原有 Episode/Recall 接口使用。新处理状态以该账本为准，不回填历史巩固状态；第一次开启自动总结可能整理尚未登记的既有 Observation，应结合 Provider 日预算控制首次历史处理成本。

已整理摘要与实时原文分开返回，提供来源引用以便去重和展开；摘要覆盖与扫描处理进度不是同一个概念。相关源被遗忘或失权时，摘要不得继续被返回。直接交互在近期工作窗口中优先得到容量，背景流不能轻易挤掉它；返回的消息仍按时间呈现。

## 后台与配置

显式配置 auto_summary_enabled，默认 false。数量、最长等待、每批大小和背景保留天数有界可配置。独立 Worker 和 Embedded run_pending/background 共用调度/消费逻辑；HTTP 请求本身不运行后台模型。

自动扫描只处理有实际认知 Provider 的 Agent/租户，按 Scope 和隐私包络隔离。优先处理已有工作，限制每轮发现/入队数量。周期 Scheduler 的 advance 接入真实维护循环，但未配置模型的周期认知任务不能因注册表名称存在而被宣称可执行。

模型阶段在数据库写事务外，沿用 Provider 超时、预算、并发与熔断治理；提交在 Outbox fencing 事务内重新检查来源。显式总结入口也经过同一管线，保持业务幂等。

背景普通到期清理必须复用 Forget，跳过有效引用、Hold 与待处理来源；不另造仅删正文却能被旧任务重建的通道。配置为不自动清理时维持现有 Retention，主动遗忘始终不受该开关阻挡。

## 实施步骤

1. 文档先行：本说明、ADR、架构、相关阶段、接入和 Provider 文档、SDK/测试入口同步；标记目标与现状。
2. 持久模型：追加 Migration，保留 Observation 兼容默认，新增处理账本与分页查询接口，验证前一 Schema 的有数据升级及恢复。
3. 应用服务：授权实时读取、Episode 来源展开、有界总结入队、批量分组/空结果和证据提交、背景保留。
4. 运行接线：独立 Worker、Embedded、Provider，显式开关和数量/时间触发，周期推进与无模型降级。
5. 公共入口：业务契约、生成 Schema、能力、Python/TypeScript SDK、Embedded、公开接口候选审查；Console 既有资源读取与必要元数据适配。
6. 验证：定向行为/兼容/迁移/授权/删除/并发/重启/真实入口验证，最后完整 make ci；保留失败及生产未验证边界。

## 验收要求

- 无模型时，背景和直接 Observation 写入、实时读取不调用任何 Provider。
- 相同源事件经重试/不同接入用途不重复生成原始事件；旧客户端和既有证据/Task 行为保持。
- 时间混排、迟到消息、同一事务多条消息和交错话题可处理；覆盖/分页无静默遗漏。
- 自动总结关闭不调用模型；打开后数量/等待触发、重启和并发不重复发布。
- 总结输出包含可核验原始引用；未知来源、删除竞态、错误格式、超时与空结果有确定行为。
- 实时查询执行 Scope/Privacy/Purpose/主体授权，跨 Space 或隐藏来源不通过摘要泄漏。
- 30 天背景清理仅在有效配置下执行，被引用源与 Hold 保留；主动遗忘使原文、摘要和相关读取立即失效，重启/重放不复活。
- 本地 Embedded 与真实 HTTP/SDK 返回等价；公开能力准确表达实际支持。
- 文档、格式、类型、契约、兼容、测试、前端和安装门禁全部记录真实结果。

## 当前进度

- [x] dev/chores 快进合入 main，保留既有未提交工作。
- [x] 用户决策：自动总结显式开启，背景 Observation 默认 30 天。
- [x] 更新相关手写文档后开始代码。
- [x] 持久模型与实时读取。
- [x] 分组总结、保留与运行接线。
- [x] 契约、SDK、安装与组合验证（完整运行发现的问题已分段复验，保留当次失败结果）。

实现结果集中记录在本说明的进度及专属报告中，不用旧工作包的历史测试结果代替本轮验收。

## 接入示例与运行参数

本增量对应 Core **0.16.0**、数据库 **Schema 25**、HTTP 契约 **1.13.0**、Python/TypeScript SDK **0.12.0**。旧记录迁移后自动使用 `context_kind="interaction"`，原文、ID 和旧幂等指纹保持。打开旧库前需要运行迁移；已经发布的 0001–0024 SQL 没有修改。

宿主在实际收到平台消息后直接写入。无须先问模型“这条消息有没有记忆价值”。`context_kind` 表示接入用途，由宿主选择；`background` 也属于 Observation。

```python
# memory 是已启动的 EmbeddedMemory；远程 AsyncIrisMemoryClient 方法相同。
await memory.observe_batch([{
    "agent_id": agent_id,
    "space_id": space_id,
    "role": "external",
    "kind": "message.text",
    "context_kind": "background",
    "source_event_id": platform_message_id,
    "source_thread_id": platform_thread_id,       # 可省略
    "reply_to_source_event_id": replied_message_id,  # 可省略
    "idempotency_key": platform_message_id,
    "occurred_us": platform_timestamp_us,
    "committed_us": received_timestamp_us,
    "content": original_text,
}])
request = {"scope": {"agent_id": agent_id, "space_id": space_id}, "limit": 100}
page = await memory.observation_context(request)
# 将 messages 原文与 summaries 摘要交给外部主体；根据 observation_refs 去重。
while page["has_more"]:
    page = await memory.observation_context({**request, "cursor": page["next_cursor"]})
```

背景消息可以来自同一群聊的任何获授权发言人，不要求提到 Agent。实际发言人仍应通过原有身份绑定接口记录，不能把群友消息冒充 Agent 的已执行结果。回复关系 ID 是平台引用信息，不能据此跨 Space 读取原文。消息不需要源线程时省略该字段，勿传空字符串。来源事件 ID 应在同一租户/Agent 下唯一；跨平台或群聊的宿主应加入稳定命名空间，回复来源 ID 使用同一种编码。

| 入口 | HTTP | Python / Embedded | TypeScript |
| --- | --- | --- | --- |
| 原始写入 | `POST /v1/observations:batch` | `observe_batch` | `observeBatch` |
| 实时读取 | `POST /v1/observations:context` | `observation_context` | `observationContext` |
| 显式总结 | `POST /v1/observations:summarize` | `summarize_observations` | `summarizeObservations` |

实时读取要求 `observation-context.v1`、明确 Agent/Space 和已授权用途；Session 省略表示仅空间级消息，并非所有会话的通配符。每页默认 100、最多 200 条，按 `occurred_us / committed_us / id` 排序；固定入库水位防止分页期间的迟到消息干扰当前扫描。下一轮实时读取应去掉旧 cursor，从新快照开始。cursor 绑定应用及请求参数，15 分钟过期；不要将它作为永久消费位点。游标固定原文的入库范围，不冻结后台处理状态；摘要和处理状态在每次读取时仍会复查当前有效性与授权。恢复备份后旧 cursor 作废。

`messages` 包含正文、结构化载荷、效果状态、时间、来源关系及 `processing_status`：`unprocessed` 尚未分配总结；`pending` 任务待处理或重试中；`processed` 已完成本批分析，包括被忽略的闲聊；`failed` 需要查看死信并显式重放。处理完成不代表消息已形成长期事实，也不代表可以立即删除。

原始消息每次读取都会复查当前权限及删除状态。为控制扫描成本，一页最多检查 1000 条原始候选；即使本页可见消息很少，只要 `has_more=true` 就应继续分页。摘要独立返回最多 16 个近期结果，`summaries_partial` 表示摘要候选超出本次上限；它们不是原始消息分页的完整历史摘要。历史 Episode 可用既有 Episode 接口读取。

```python
accepted = await memory.summarize_observations(
    {"scope": {"agent_id": agent_id, "space_id": space_id}},
    idempotency_key="stable-summary-request-id",
)
# HTTP 返回 202；这里只表示持久入队，完成情况以后续读取为准。
# 未配置认知 Provider 会明确报错。读取上下文仍可用。
```

显式总结还要求 `consolidation.v1`。它只安排一个有界批次；已分配的消息不会重复安排。模型输出错误、凭证撤销、来源删除等失败不会自动重新新建收费批次；沿用后台任务管理的死信诊断和显式重放。Provider 自身的有限重试仍受既有预算和退避控制。

| 配置 | 默认值 | 含义 |
| --- | --- | --- |
| `auto_summary_enabled` | `false` | 显式开启后台批量认知工作 |
| `summary_min_messages` | `50` | 同一范围和隐私条件下的数量触发阈值，1–500 |
| `summary_max_wait_seconds` | `120` | 最早未处理消息等待时间，1–86400 秒 |
| `summary_batch_size` | `100` | 每批上限，1–500；不得小于数量阈值 |
| `background_retention_days` | `30` | 按入库时间计算原文期限，0–3650 天；0 关闭这项自动清理 |

配置了认知 Provider 不会自动开启总结。独立服务在 `[service]` TOML 或 `IRIS_MEMORY_AUTO_SUMMARY_ENABLED` 等同名环境变量中设置这些项；HTTP 服务和 Worker 应使用同一配置。`serve` 提供接口，独立 `worker` 执行后台工作。Embedded 在 `EmbeddedConfig(...)` 中配置，使用 `background=True` 或由宿主调用 `run_pending()` 驱动。

```toml
[service]
auto_summary_enabled = true
summary_min_messages = 50
summary_max_wait_seconds = 120
summary_batch_size = 100
background_retention_days = 30
```

每轮发现最多 32 个范围，持久记录扫描顺序以轮转；遇到连续不可读取或超预算消息时保存扫描位置，后续轮次继续，避免阻塞后面的可读消息。单批正文与结构化载荷合计最多 200000 个字符。自动总结只选 `effect_state="committed"` 的消息；`partial` 保留在原始上下文中，不作为完整成功结果进行总结。数量或等待满足其一即可入队。等待时间是启动条件，执行时间还取决于 Worker、队列、Provider 可用性和预算，不是完成期限。每个产生 Episode 的分组还会进入既有候选提炼/协调流程，因此费用不一定只有一次总结调用；空分组不再调用提炼模型。

## 认知 Provider 的新输出

新任务使用 `prompt_version=schema_version="summary.groups.v1"`。输入包含 ID、角色、正文、时间、用途及线程/回复元数据。宿主回调应返回：

```json
{
  "groups": [
    {"title": "发布安排", "summary": "团队将于周五发布，尚待测试确认。", "observation_ids": ["obs-a", "obs-c"]}
  ],
  "ignored_observation_ids": ["obs-b"]
}
```

允许不同话题交错，最多 32 组；一条消息可支持不同组，但同组不得重复，同一来源集合不得重复建组。每条输入必须被引用或明确忽略，不能两者同时；来源必须来自本批。摘要事实只能由该组明确引用的消息支持。Core 能检查来源身份、权限、版本和覆盖，无法仅靠引用 ID 保证模型的语义判断完全正确。现有 `{"title": ..., "summary": ...}` 回调仍兼容，会把整批作为一组来源；如需筛掉闲聊，应升级到分组格式。

总结完成不会修改原始 Observation。`Episode.observation_refs` 和 `source_refs` 指向被引用的消息；未被引用的消息保留原有到期时间。除摘要外，既有 Claim/Relation 证据、Note/Task/Focus 等保存的来源依赖也会阻止普通清理。主动 Forget、法定 Hold 等继续遵循既有规则。

30 天指原文的可读保留时间，清理复用删除账本、正文擦除和墓碑机制，最小身份/审计元数据仍会保留，数据库文件也不会立即缩小。被引用原文可能超过 30 天；这属于证据保留，不是新的“环境库”。普通到期每轮最多处理 50 条，积压清理由后续维护轮次继续完成。
