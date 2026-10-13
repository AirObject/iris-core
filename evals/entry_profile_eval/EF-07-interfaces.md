# EF-07／PR B：给 UX 的接口清单

基于规划者 2026-10-14 决定。入口画像切换为记忆；旧整版写接口退役。本文是本 PR 的接口实施报告，不修改设计文档或冻结评分。前端由 UX 实现。本线不新增管理员审批。

## 宿主 v1

`POST /api/v1/entries/{entry_id}/prepare` 请求不变。`entry_profile` 仍为 null 或对象：

```json
{"text":"明确群规／禁忌：禁止广告。","version":1,"generated_at":"2026-10-14T00:00:00+00:00","possibly_stale":false,"memory_ids":[42]}
```

四个原字段类型不变。新增 `memory_ids: list[int]`，按 text 的条目顺序一一对应；正文不拼入引用原话。`known_memory_ids` 同时排除画像分区中的对应条目，过滤不回填。version 是入口投影版本，与任何单条 memory.revision、旧文档 version 均不是同一标识。

新增、正文修改、撤下、生命周期、依据或可见范围变化导致实际展示集合变化时，同一写事务将 version 加一；同一事务多次变化只加一次。确认强度变化而集合不变、分页查询和读取不加版本。generated_at 仅在投影变化时更新。存在未处理消息／待重建时 possibly_stale=true；依据失效的自动条目直接移出投影。禁用／私聊／无可展示条目返回 null。

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

profile.memories 中每条包含 memory_id、revision、category、body、author、manual_protected、manual_created、lifecycle、usable、sources 等；memory_id 和 revision 才是逐条编辑的 ID／CAS 值。不可直接使用 projection.version 编辑记忆。category 为 topic／saying／rule／null；null 显示“管理员说明”。

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

## 后台任务预算（EF-09）

记忆形式任务的生成与检查各有 180 秒请求预算，重试共用本次请求截止时间。到期状态为 failed、reason=timeout；保留调用诊断，不写回记忆、游标或投影。调用诊断兼容新增 timeout_seconds。没有新增路由、请求参数或错误码。旧文档路径及其已保存的 generate_timeout_seconds／check_timeout_seconds 不改；记忆路径沿用独立 MemoryProfileLimits，不以这两个旧设置控制超时；旧字段仍按原运行配置规则校验。

## UX 过渡

入口页展示当前画像记忆列表，逐条编辑／删除／遗忘／恢复；把旧版本入口明确标成只读历史。隐藏旧的整版编辑、候选确认／拒绝和 all_manual 选项。显示“管理员说明”原文，不因 category=null 丢弃。展示任务进度、冲突与预算诊断，不向自动生成引入管理员确认步骤。
