# EF-07／PR B：给 UX 的接口清单

基于规划者 2026-10-14 决定。入口画像切换为记忆；旧整版写接口退役。本文是本 PR 的接口实施报告，不修改设计文档或冻结评分。前端由 UX 实现。本线不新增管理员审批。

## 宿主 v1

`POST /api/v1/entries/{entry_id}/prepare` 请求不变。`entry_profile` 仍为 null 或对象：

```json
{"text":"明确群规／禁忌：禁止广告。","version":1,"generated_at":"2026-10-14T00:00:00+00:00","possibly_stale":false,"memory_ids":[42]}
```

四个原字段类型不变。新增 `memory_ids: list[int]`，按 text 的条目顺序一一对应；正文不拼入引用原话。EF-10将每条正文中的换行及连续空白合为一个空格后投影，因此多行“管理员说明”仍只占一条、一行和一个memory_id；管理返回的body与数据库原文保留换行，不丢失内容。`known_memory_ids` 同时排除画像分区中的对应条目，过滤不回填。version 是入口投影版本，与任何单条 memory.revision、旧文档 version 均不是同一标识。

新增、正文修改、撤下、生命周期、依据或可见范围变化导致实际展示集合变化时，同一写事务将 version 加一；同一事务多次变化只加一次。确认强度变化而集合不变、分页查询和读取不加版本。generated_at 仅在投影变化时更新。存在未处理消息、观察缺口、排队／失败检查或待重建时 possibly_stale=true；仅缺第二次证据的候选不单独令它陈旧；依据失效的自动条目直接移出投影。禁用／私聊／无可展示条目返回 null。

自动条目按 rule、topic、saying 及 ID 排序，复用 6 条／500 估算 token 的展示预算；人工条目优先，并不因预算被隐藏。未展示 ID 在管理 diagnostics.omitted_memory_ids 中列出。管理者主动遗忘／删除仍有效；“手写规则始终有效”限制的是自动任务不能改写或撤下。

普通 ranked memories 继续排除入口画像。`POST /api/v1/memories/search` 显式 `kinds:["入口画像"]` 仍必须带 entry_id，继承 PR A 的硬范围规则。

## 管理读取

以下路由继续使用既有管理员会话；分页参数 limit／offset 不变。

| 路由 | 返回／新增字段 |
| --- | --- |
| GET `/admin/api/entries/{entry_id}/profile` | `mode:"memory"`、settings、projection（宿主同形）、memories、diagnostics、migration、cursor、rebuild_pending、latest_attempt；pending=null、pending_count=0、legacy_write_status=410。current 兼容指向 projection，不再是文档版本详情 |
| GET `/admin/api/entries/{entry_id}/profile/versions` | 旧文档历史分页；每个版本 `legacy:true`、is_current=false |
| GET `/admin/api/entries/{entry_id}/profile/versions/{version}` | 旧版本只读详情，`legacy:true`；清除标记和原有依据信息保留 |
| GET `/admin/api/entries/{entry_id}/profile/diff` | before_version／after_version 仅比较旧文档历史，不接受投影版本替代 |
| GET `/admin/api/entries/{entry_id}/profile/attempts/{attempt_id}` | 既有任务状态、原因、stage、时间；version 表示该任务的投影版本；新任务 version_status=null，不关联同号的旧文档候选。生成原始输出保存后 stage=checking，任务结束为 finished |
| GET `/admin/api/memories?kind=入口画像&entry_id=...` | 通用记忆分页；新增 kind 枚举。画像项附 owner_entry_id、category、manual_created |
| GET `/admin/api/memories/{memory_id}` | 通用详情、sources、修订、操作、清除预览；画像项同样附 owner_entry_id、category、manual_created |

profile.memories 中每条包含 memory_id、revision、category、body、author、manual_protected、manual_created、lifecycle、usable、sources、last_confirmed_at 等；memory_id 和 revision 才是逐条编辑的 ID／CAS 值。不可直接使用 projection.version 编辑记忆。category 为 topic／saying／rule／null；null 显示“管理员说明”。EF-10兼容新增 last_confirmed_at: string|null，直接返回记忆最近确认时间；读取不改变它，确认时间变化但展示集合不变时不增加投影版本。

migration 每行包含 entry_id、legacy_version、sentence_index、disposition、memory_id（可空）、category、created_at。disposition 为 manual_imported／manual_unclassified／model_rebuild_pending／cleared_history；不另复制一份可变正文，正文从原只读版本或对应记忆读取，仍受清除约束。

## 管理写入

写请求继续要求 JSON、管理员会话和 X-Iris-CSRF。下列逐条写入复用现行记忆接口，不增加确认候选阶段。

| 路由 | JSON 请求 | 成功返回 |
| --- | --- | --- |
| POST `/admin/api/memories` | `{"kind":"入口画像","owner_entry_id":"入口 ID","category":"rule","content":"禁止广告。"}`；category 可为 topic／saying／rule／null，content 1—1000 字、非空白；入口须为 group／live | 201，通用记忆详情。人工创建，入口硬范围、self／设定，importance=80 |
| PATCH `/admin/api/memories/{id}` | `{"expected_revision":1,"content":"新正文"}` | 更新后的通用详情 |
| DELETE `/admin/api/memories/{id}` | `{"expected_revision":1}` | 删除后的详情；删除 ID 不复活 |
| POST `/admin/api/memories/{id}/forget` | `{"expected_revision":1}` | 遗忘后的详情 |
| POST `/admin/api/memories/{id}/restore` | `{"expected_revision":1}` | 恢复遗忘记忆后的详情；不是恢复已删除 ID |
| PATCH `/admin/api/memories/{id}/lifecycle` | expected_revision 加现行 pinned／importance／retention 字段 | 通用详情；自动画像 importance 不能超过69 |
| POST `/admin/api/memories/{id}/purge` | `{"expected_revision":1,"confirm":true}` | 现行清除回执；派生画像、历史、投影与原始输出一并清理 |
| PATCH `/admin/api/entries/{entry_id}/profile/settings` | expected_version（当前投影版本）＋ enabled；publish_mode 只能省略或 check_auto | 当前 settings。all_manual 已退役，升级时自动转换并留操作记录 |
| POST `/admin/api/entries/{entry_id}/profile/regenerate` | `{"expected_version":0}` | 202，accepted、attempt，Location 指向任务状态；无新证据时任务记 skipped，不重复确认 |

旧 PUT `/entries/{entry_id}/profile`、POST `/profile/versions/{version}/confirm`、`/reject`、`/rollback`，在切换后均返回 HTTP **410**、`error.code="entry_profile_legacy_write_retired"`。settings 写 all_manual 返回 **410**、`entry_profile_publish_mode_retired`。这些管理路径均有 `/admin/api` 前缀。

错误仍采用 `{"error":{"code":"...","message":"..."}}`：投影版本／设置冲突 409 entry_profile_conflict；已有任务 409 entry_profile_generation_in_progress；记忆修订冲突 409 revision_conflict；对象缺失 404 not_found；参数错误 400 invalid_request。认证／CSRF 错误沿用原接口。

## 每日增量任务（EF-13，替代 EF-09／10 的整包任务描述）

自动生成继续开启，没有 auto_generate 设置。enabled 仍控制整个画像功能。每日维护处理角色时区已经结束的日期；“立即运行”也处理当日尾块。请求与 HTTP 202／409 不变，不新增审批。

候选本身是 kind=入口画像 的记忆，importance=30；来源够独立且逐条检查通过才准入、importance 提为 50，继续普通衰减。失去准入回到候选且停止投影，importance 不回降。人工条目不走自动准入。

`profile.memories[]` 兼容新增：

| 字段 | 类型／含义 |
| --- | --- |
| admission_state | manual／candidate／pending／accepted／rejected；pending 是模型检查排队，非管理员待确认 |
| independent_days | integer，按角色时区及确定性独立性规则累计的本地日期数 |
| independent_uses | integer，去掉转引、复述、接龙、刷屏等后的独立依据次数 |
| admission_reason | string 或 null，例如 evidence_budget_exceeded、basis_changed、reject、invalid_output、timeout |
| sources | 已审依据；自动候选可为空，不将未审引文作为已发布依据。人工条目新增的自动确认引文也先进入 pending_sources，人工正文照常可用 |
| pending_sources | 尚未准入检查的绑定依据，仅管理侧诊断；继续受硬范围和清除约束 |

profile 顶层及 latest_attempt／attempt 详情兼容加 `progress`：scan_cursor（已持久排队位置）、applied_cursor（连续完成位置）、observation_chunks、completed_chunks、pending_checks、failed_checks、rejected_checks、observation_gaps、unscanned_messages、waiting_candidates、oldest_pending_at（string 或 null）。两类游标都是数据库消息 ID，不是发生时间。待审候选不等于失败；observation_gaps 非零时 applied_cursor 不越过缺口。

任务详情兼容加 `work[]`，每项为 id、kind=observe／admit、state=queued／running／applied／failed／rejected／cancelled／deferred、local_day、memory_id、cursor_before／cursor_after、attempts、reason、retry_at、created_at／finished_at。返回进度元数据，不返回模型原始输出。顶层 stage 仍为 generating／checking／finished，可在一轮中多次切换；state=published 表示提交了部分观察或检查结果，不代表所有候选都已展示，细节以 work 和 progress 为准。

每个观察、每个单条检查分别有 180 秒预算，初次、HTTP 重试、一次 JSON 修正与解析共享截止。每次实际调用（包括修正）独立扣维护轮 max_calls；入口轮转，额度不足留队。生产超时／429 按条延期，其他已提交条目保留；同输入至多两次后续重试。格式修正仍失败为该工作项 invalid_output，不回滚此前观察，也不发布未经准入的候选。原始输出内部按 observe／observe_repair、admit／admit_repair 保存，重试追加 attempt 编号，仍接清除索引。

旧文档已保存的 generate_timeout_seconds／check_timeout_seconds 保留且按原规则校验；新路径使用独立 IncrementalProfileLimits（180 秒），不改写旧设置。观察 48 条／3000 估算 token，目录 32 条／1500 token；检查最多 6 条证据／2000 token。必要核心证据本身超预算时保留诊断，不截证据凑准入。EF-14 的多页检查见下文。

## UX 过渡

入口页展示当前画像记忆列表，逐条编辑／删除／遗忘／恢复；把旧版本入口明确标成只读历史。隐藏旧的整版编辑、候选确认／拒绝和 all_manual 选项。显示“管理员说明”原文，不因 category=null 丢弃。展示任务进度、冲突与预算诊断，不向自动生成引入管理员确认步骤。

## EF-10变更清单

仅兼容新增profile.memories[].last_confirmed_at；多行管理员说明的宿主投影合为同一行，memory_ids对应关系不变。JSON修正新增内部purpose后缀_repair与诊断，不新增路由、请求参数或阶段枚举。生成原始输出保存后仍使用既有checking阶段（包含格式修正期间），最终失败reason区分invalid_output、timeout及call_budget。其余410／CAS／历史与UX过渡规则不变。

## EF-13 兼容性说明

宿主四原字段及 memory_ids、人工逐条 CRUD／CAS、旧写接口 410、legacy 历史、last_confirmed_at 和多行管理员说明规则均保留。显式按画像 kind 搜索只返回已准入自动条目或有效人工条目；管理员仍可看到未准入候选。新字段均为兼容新增，UX 不需修改写请求格式。


## EF-14 兼容新增与分页准入

任务 `work[]` 兼容新增 `check_page: integer|null`、`check_pages: integer|null`：admit 为本组从 1 开始的页码和总页数，observe 为 null；旧的单页 admit 按 1/1 返回。不新增路由或写入字段。pending_checks／failed_checks／rejected_checks 按工作页计数，waiting_candidates 按记忆条目计数，不能相加当作条目数量。

累计来源超过单次预算时，每页带共同的最小独立依据、释义／对象／群规范围依据，其余来源按稳定顺序分页；每页仍最多 6 条／2000 估算 token，不丢弃已知反证。最多 32 个唯一来源、8 页；必要核心本身放不下则 admission_reason=evidence_budget_exceeded。全部页通过且最终修订／来源／设置复核一致，才准入和提升 importance；部分页 applied 不等于已准入。通过页重启复用，后页拒绝取消未执行同组页，失败与超时保留诊断、不得部分发布。

`admission_reason` 可新增 rule_scope_not_established、unbound_entities:<逗号分隔对象>；work.reason 可有 bundle_rejected／stale_check 等。每条独立日期／次数继续只用真实作者及独立讨论组计算，跨日后续不另算。宿主投影结构、人工保护、410、CAS、last_confirmed_at 均保持。
