# Iris 宿主接口 v1 接入契约

路径前缀 `/api/v1`，请求与响应为 UTF-8 JSON；有正文时使用 `Content-Type: application/json`。Iris 只监听本机回环地址。完成首次设置并登录管理界面后，可在 `/docs` 浏览交互文档，在 `/openapi.json` 取得中文 OpenAPI 3.1 文档。管理接口 `/admin/api` 不属于本契约。

v1 冻结基线为 `tests/test_host_api_contract.py`。兼容更新可以增加可选请求字段、响应字段、操作和运行提示；不能删除、重命名已有字段，改变类型、含义或已承诺的默认行为，也不能把可选字段改成必填。宿主须忽略未知响应字段和未知 `hints[].code`。破坏性变更须由规划者记录决定并使用新版本，不在 v1 静默发生。本文明确标注的 VS 过渡行为不代表入口隐私过滤已经完成。

## 鉴权与入口范围

每个请求带 `Authorization: Bearer <宿主令牌>`。凭据由管理员创建，只返回一次；使用环境变量或宿主的秘密存储，不放在代码、URL、消息正文或日志中。管理员 Cookie 和宿主令牌不能互相代替。令牌来源名称用于状态报告、目标和操作记录；请求中的旧 `host` 字段不能覆盖它。

入口 ID 是宿主提供的稳定字符串，区分大小写，长 1—200 个字符，不能全为空白或含 `?`、`#`、控制字符（Unicode Cc，包括制表符、换行和 DEL）；非法 ID 返回统一 400。历史令牌范围若包含现已禁止的字符，该令牌返回 401，须由管理员重新签发。路径按 ASGI 解码后的入口段检查，与路由使用同一字符串，冒号等合法字符不变。令牌可以授权全部入口、指定列表或原字符前缀；`%`、`_` 不作通配符。前缀建议带连接器名字，例如 `astrbot:`。范围可以包含尚未创建的入口，首次接收消息自动创建入口。令牌撤销后立即失效；不同令牌的限流桶互不影响，默认持续 20 次/秒、突发 60 次。

鉴权和入口路径权限检查发生在解析正文之前。HTTP Host 只接受 `127.0.0.1`、`localhost`、`[::1]`（可带端口）；不接受转发 Host，不开放 CORS。Host 检查先于就绪检查和鉴权。

权限与内容可见范围是两层规则：入口原始消息只通过有权访问的本入口准备接口返回；目标、提醒、入口积压先按令牌范围过滤。全局目标及角色全局状态对宿主共享；创建或修改没有 `entry_id` 的全局目标必须使用 `all` 范围令牌，受限令牌返回 403，也不能经去重合并修改全局目标。记忆在入口间的隐私过滤由 VS 的检索实现负责，令牌范围不能替代记忆可见范围设置。

**AP/VS 合入顺序：** `POST /memories/search` 新增可选 `entry_id`。给出时检查令牌权限，无权返回 403；只有 `Retrieval.search` 支持该参数时才向它传递。省略或传 `null` 时完全不传递参数。当前 AP 基线的旧检索实现尚无该参数，仍按既有共享记忆规则返回；部署依赖入口隐私隔离的连接器须同时包含 VS。VS 合入后，省略入口只能查到对全部入口可见的记忆。这个兼容适配不改召回排序、选材或检索实现。

## 操作清单

以下路径省略 `/api/v1`。

| 方法与路径 | 成功码 | 主要返回 |
| --- | --- | --- |
| `POST /entries/{entry_id}/messages` | 200 | `message_ids`, `pending_count` |
| `POST /media` | 201 | `id`, `sha256`, `content_type`, `size_bytes`, `kind`, 理解来源与文本 |
| `POST /entries/{entry_id}/prepare` | 200 | 六个分区、`judgment`, `recall_id` |
| `POST /memories/search` | 200 | `memories`, `hints`, `recall_id`，可选 `goals` / `state` |
| `POST /feedback` | 200 | `recall_id`, `accepted`, `strengthened` |
| `GET /state` | 200 | 当前状态或 `{}` |
| `PUT /state` | 200 | 开始或替换后的状态 |
| `PATCH /state` | 200 | 更新后的状态或 `{}` |
| `DELETE /state` | 200 | `{}` |
| `GET /goals` | 200 | `items`, `total`, `limit`, `offset` |
| `POST /goals` | 201 | `goal`, `submitted_id`, `dedup` |
| `PATCH /goals/{goal_id}` | 200 | 修改后的目标 |
| `GET /notifications` | 200 | `items`, `next_cursor`, `has_more` |
| `POST /entries/{entry_id}/learn` | 200 | `accepted`, `pending_count`, `paused`, `reason` |
| `GET /status` | 200 | 健康状态、用量、积压和批次结果 |

## 接收消息与去重

```json
{
  "platform": "chat",
  "sender": "小林",
  "account_id": "lin-001",
  "content": "今晚看星星",
  "occurred_at": "2026-10-10T20:00:00+08:00",
  "dedupe_key": "platform-message-001",
  "kind": "message",
  "media_ids": [],
  "pace": "standard"
}
```

必填 `sender`、`content`、带时区的 `occurred_at`、`dedupe_key`；正文可为空（例如只有媒体）。稳定的 `account_id` 用来区分同名账号。`kind` 可为 `message`、`self_output`、`action_result`、`event`。可提供 `quote_author`、`quote_author_account_id`、`quote_content` 标明被引用的原话。`entry_name`、`entry_kind` 和 `pace` 用于首次创建入口；之后沿用已保存的节奏。自定义节奏示例：`{"count":6,"idle_seconds":30,"max_wait_seconds":180}`。

接口也接受 1—1000 个上述对象的数组，整个数组原子提交：任何新消息的字段、媒体或权限校验失败都会回滚。响应例如 `{"message_ids":[1,2],"pending_count":2}`，ID 顺序对应输入顺序。相同入口的相同 `dedupe_key` 返回原消息 ID；重试不覆盖正文、发生时间或媒体引用。宿主须持久保存平台去重键，切勿每次重试另造键。

单条 `content` 和 `quote_content` 各最多 32768 个 UTF-8 字节，整个消息请求体最多 64 MiB，字节超限返回 413，不截断。字段字符上限：`sender`、`entry_name`、`account_id`、`scene_identity`、`quote_author`、`quote_author_account_id` 各 200；`platform`、`entry_kind`、`occurred_at` 各 100；`dedupe_key` 300；`quote_content` 另有 32768 字符上限。字段字符超限返回统一 400，数组内任一字段超限均不接收整批。`pending_count` 是本入口 `pending` / `batched` 消息数，不是新生成记忆数。接收成功后，由后台按入口节奏学习；发生时间用于事实和相对日期，等待节奏按接收时间计算。

`POST /entries/{entry_id}/learn` 无需正文，返回例如 `{"accepted":true,"pending_count":2,"paused":false,"reason":null}`。模型暂停时也接受请求，待恢复后处理；调用后到 `/status` 观察批次状态，不能据此宣布已记住。

## 媒体上传和引用

`POST /media` 使用 JSON Base64，不使用 multipart，不接收 URL、本地文件路径或客户端文件名。

```python
import base64
import os
from pathlib import Path
import httpx

with httpx.Client(base_url="http://127.0.0.1:8080", timeout=30,
                  headers={"Authorization": "Bearer " + os.environ["IRIS_HOST_TOKEN"]}) as client:
    response = client.post("/api/v1/media", json={
        "entry_id": "astrbot:group-a",
        "content_type": "image/png",
        "data_base64": base64.b64encode(Path("photo.png").read_bytes()).decode("ascii"),
        "understanding_text": "窗边有猫。",
    })
    response.raise_for_status()
    media_id = response.json()["id"]
    accepted = client.post("/api/v1/entries/astrbot:group-a/messages", json={
        "sender": "小林", "content": "今天拍的照片", "dedupe_key": "platform-photo-001",
        "occurred_at": "2026-10-10T20:00:00+08:00", "media_ids": [media_id],
    })
    accepted.raise_for_status()
```

字段：`entry_id`、`content_type`、`data_base64` 必填，`understanding_text` 可选。Base64 使用标准字母表和填充，不含 `data:` 前缀或换行。解码后最多 **10 MiB = 10,485,760 字节**；包括 Base64、理解文本和 JSON 在内的传输正文最多 **14 MiB**，实际流式接收也计数，不信任 `Content-Length`。理解文本最多 32768 UTF-8 字节；空白说明视作未理解。

白名单同时校验 MIME 与文件头：

| 类型 | MIME |
| --- | --- |
| PNG、JPEG、GIF、WebP | `image/png`, `image/jpeg`, `image/gif`, `image/webp` |
| MP3、WAV、Ogg、FLAC | `audio/mpeg`, `audio/wav`, `audio/ogg`, `audio/flac` |
| MP4、WebM | `video/mp4`, `video/webm` |

类型不支持或文件头不匹配返回 400，文件或传输正文超限返回 413。不接收 SVG、HTML 或任意 `application/*` 附件；文件头校验不保证内容能完整解码。

响应包含 `id`（用于 `media_ids`）、`sha256`、`content_type`、`size_bytes`、`kind`、`understanding_source`、`understanding_text`、`completed_at`、`last_error`。每次上传产生新的对象 ID，同字节文件按 SHA-256 共享存储，宿主说明各自独立。消息的 `media_ids` 最多 100 个、不得重复、保留顺序。上传入口和接收入口可以不同，但令牌必须同时有权访问两者；仅猜中对象 ID 不授予权限。对象不存在、已清理或不是宿主上传对象返回 404。重复的消息去重键沿用原有引用，不重新验证已失效的重试引用。

上传和消息接收不调用模型。宿主提供的理解直接复用；其他图片在学习批次领取后、学习材料组成前理解，整批理解共享最多 120 秒预算，失败用 `[图片，未理解]`；明确内容安全拒绝用“敏感信息无法访问”，不换方式重试。音视频只作附件和占位，不调用音视频模型。上传响应是当时的理解状态；本版没有媒体下载、列举或理解状态轮询接口。

媒体上传与消息接收是两个请求。重试上传产生新的对象而共享文件；先成功上传、再发送引用，接收失败后可在宽限期内重试。没有消息引用的文件宽限 1 天后由维护清理，仍被任何消息引用的共享文件不删除。

接线实现：`media.save_host_media` 保存并写入 `admin_operations` 的 `media_uploaded` 操作（对象类型 `media`、对象 ID、`details_json.entry_id`），这是持久的入口授权记录；`require_host_media` 在消息事务中复核。记录不包含文件或理解正文，不算文件引用。导出导入实现须连同对象保留这条授权记录；缺失时 HTTP 采用 404 关闭访问，不能把内部媒体默认为公开上传。保存完成但授权记录未提交的中断上传同样不可引用，之后按宽限清理。

## 回复准备的六个分区

`POST /entries/{entry_id}/prepare` 请求 `{}` 即使用本入口自适应近期消息。`text` 省略与空字符串不同：省略自动构造查询，空字符串显式不提供查询文本。可传主体 ID / 无歧义名字组成的 `participants`（空数组不取人物要点），以及宿主已有的 `known_memory_ids`。

| 分区 | 宿主应如何使用 |
| --- | --- |
| `persona` | 当前角色描述、版本和更新提示；不是收到的原始消息 |
| `memories` | 按相关性及人物要点选择的记忆；保留相信程度、立场、生命周期、出处和 `reason` |
| `recent_messages` | 仅本入口近期原始消息，按顺序返回，`unlearned` 标明尚未成功学习；不应与记忆当成两次独立经历 |
| `state` | 宿主报告的当前活动，可能过时；无活动为 `{}` |
| `goals` | 当前适用的目标和询问；包含截止、到期和依据变化提示 |
| `hints` | 运行情况、降级、记忆缺口和其他授权入口的待学习计数；不是事实或新经历 |

另返回 `recall_id` 用于使用反馈，`judgment` 说明召回判断是否应用、关闭或降级。`memories[].reason` 区分 `relevant` 和 `person_highlight`；`subject_annotations` 标明可能同一人与虚构扮演，不提供联系证据原文。已删除记忆不返回，宿主上下文中已有的记忆由 `known_memory_ids` 排除。

宿主 prepare 和 search 返回的 `memories[].sources` 保留来源条目及 `message_id`；若来源入口不在当前令牌范围内，`entry_id`、`entry_name`、`occurred_at` 均为 `null`。这只隐藏来源元数据，不改变可见记忆的 ID、正文、顺序或 reason；管理员来源投影保持完整。

默认 `recent_limit=20`（最多 100）、`memory_limit=8`（最多 8）、`token_budget=1500`（最多 1500）、`goal_limit=10`（最多 10）。`judge=false` 可关闭召回判断；默认判断预算 10 秒，`judge_budget_seconds` 只能缩短且须大于 0。模型失败正常返回仍有效的信息并在 `hints` 中说明，不改为接口 503。

其他入口提示示例：

```json
{
  "code": "other_entries_pending",
  "entry_count": 2,
  "time_from": "2026-10-10T12:10:00+00:00",
  "time_to": "2026-10-10T12:13:00+00:00",
  "time_basis": "received_at",
  "message": "另有 2 个入口有尚未学习的新消息（2026-10-10T12:10:00+00:00—2026-10-10T12:13:00+00:00）"
}
```

先按本令牌范围筛选，再汇总本入口以外的 `pending` / `batched` 消息；同一入口多条消息只计一个入口。只含数量和 UTC 接收时间上下界，不返回其他入口的 ID、名字、正文、摘要、主题或人物。没有待学习的其他授权入口时不增加这条 hint。已过滤、已学习、终止失败的消息不进入此计数；失败区间由既有缺口提示表达。没有收到消息不等于其他入口没有发生事件。

## 定向查询和使用反馈

`POST /memories/search` 示例：`{"text":"观星","entry_id":"astrbot:group-a","include_forgotten":false,"include_goals":true,"include_state":true,"limit":8}`。可按 `people`、`kinds`、`stances`、`time_from` / `time_to` 筛选，`limit` 最多 100。入口参数的权限和 VS 过渡行为见前文；这不是接收消息，不产生新经历，也不生成回复。开启 `include_forgotten` 仅深度读取，不恢复遗忘记忆；未开启的 `goals` / `state` 分区不返回。

回复生成后，将实际使用的记忆提交 `POST /feedback`：`{"recall_id":"召回标识","memory_ids":[1,2]}`。不能自动把所有召回项当作使用；完全没用记忆时可传空数组。召回属于发起它的令牌，更换令牌后须重新查询；有效期 24 小时。`accepted` 与 `strengthened` 都是 ID 数组，同一召回和记忆重试不会重复强化。召回过期或记忆不属于这次召回返回 400，已删除的记忆返回 404。

## 当前状态、目标和提醒

`PUT /state` 报告 `activity`，可附 `details`、`mood`、`entry_id` 和带时区的 `started_at`。相同活动保留开始时间；省略开始时间时 `start_time_basis=first_report`，宿主显式提供时为 `host`。`PATCH /state` 局部更新细节或情绪，空对象为心跳，不替换活动；细节值 `null` 删除该项。每个细节有自己的更新时间，响应包含持续秒数和 `possibly_stale`。`DELETE /state` 结束活动但保留报告历史，可无正文或提供 `entry_id`。无活动时读取、心跳或结束均返回 `{}`。

`POST /goals` 必填 `content`，可传 `kind=normal|question`、`people`、`entry_id`、`deadline`、`reminder_minutes`、`host_key`。日期型截止按角色时区当天末尾解释；询问没有截止提醒。响应中的 `goal` 是去重后保留的对象，`submitted_id` 是提交对象，`dedup` 说明新建、合并或可能重复及后台判断情况。相同绑定宿主的 `host_key` 重试返回最初回执，不修改原内容。

`GET /goals` 可按状态、类型、到期情况和入口筛选并分页。`PATCH /goals/{id}` 可完成、放弃、修改截止时间与提醒提前量；带 `expected_revision` 防止覆盖并发修改。收到 `revision_conflict` 后重新读取并判断是否仍要执行；`goal_merged` 附 `canonical_id`，宿主应读取保留对象后明确提交，不能自动跟随修改。修改不包含编辑目标正文的能力。

`GET /notifications?after=0&limit=30` 使用递增游标，最多 100 条，响应 `items`、`next_cursor`、`has_more`。权限过滤在分页和标记已取走之前完成。`items[].id` 用来去重；同一游标可重取，宿主持久处理后再保存新游标。拉取会标记 `taken`，不证明已发给用户，不完成目标；只有明确完成操作才改变目标状态。

## 错误与重试

所有 `/api/v1` 错误使用同一外层对象：

```json
{
  "error": {
    "code": "invalid_request",
    "message": "请求格式或内容不合法",
    "fields": [{"field": "body.occurred_at", "message": "需要带时区的 ISO 时间"}],
    "retry_after_seconds": null,
    "retry_at": null
  }
}
```

固定字段为 `code`、`message`、`fields`、`retry_after_seconds`、`retry_at`。`fields` 总是数组，字段路径可含 `body`、`query`、`header`、`path` 与数组索引；单条/数组联合请求的错误路径还可能含 `Message` 等分支名。不要靠 `message` 做程序判断，使用 HTTP 状态和 `code`。部分错误可加 `canonical_id`；旧消息超限响应的 `field` 兼容字段保留，统一字段说明在 `fields`。

| HTTP | 含义及宿主处理 |
| --- | --- |
| 400 | 请求格式、字段、内容类型或媒体类型不合法；看 `fields` 修正请求。未支持的 HTTP 方法也归为 400，保留 `Allow` 头 |
| 401 | 缺失、无效、撤销的令牌；响应 `WWW-Authenticate: Bearer`，更换凭据后重试 |
| 403 | 入口、媒体来源、目标或召回无权访问；受限令牌创建／修改全局目标（包括合并后的全局目标）也返回 `entry_forbidden`。勿通过重试规避范围 |
| 404 | 对象不存在、已删除或媒体不可引用；不把已删除记忆重新反馈为使用 |
| 409 | 修订冲突 `revision_conflict` 或目标已合并 `goal_merged`；重新读取再决定 |
| 413 | 单条消息、媒体文件或 JSON 传输正文超限；缩小或拆分请求 |
| 429 | 单令牌限流；等待整数秒 `Retry-After` 后重试 |
| 503 | 启动、迁移、数据库或媒体存储暂不可用；按 `Retry-After` 重试 |

429 / 503 同时返回正整数 `retry_after_seconds` 和带时区的 `retry_at`，表示预计可以再次尝试的时间，**不是服务保证恢复的期限**；无法估算的临时不可用给出 1 秒重试建议。其余错误两个字段为 `null`。Host 错误优先于就绪状态，令牌错误优先于正文解析。宿主响应和错误均禁止缓存。

有正文的状态、目标请求最多 32768 UTF-8 字节；媒体请求 14 MiB，消息批量请求 64 MiB，其他宿主写请求 256 KiB。包括没有 `Content-Length` 的分块请求也受限。单文件和单条内容仍各自遵守前文上限。

连接超时后不能假设写入失败：接收消息重用原 `dedupe_key`，目标重用原 `host_key`，反馈重用原 `recall_id` 与实际使用 ID。上传重试可能产生新对象 ID，但文件共享。提醒拉取重用原游标。普通状态/修改请求在不确定是否提交时先读取状态；携带修订号的目标修改在冲突后先刷新。不要把模型用途暂停与 HTTP 不可用混为一谈。

## 不可混淆的状态

- 已接收 ≠ 已学习；批次结束 ≠ 学习成功；学习成功 ≠ 形成了记忆。
- 放弃或拒绝 ≠ 已经记住；被召回 ≠ 被使用；被使用 ≠ 内容可信。
- 读取遗忘记忆 ≠ 恢复；提醒被取走 ≠ 已送达；已送达 ≠ 目标完成。

`GET /status` 的 `batches[].state` 区分 `waiting`、`running`、`succeeded`、`abandoned`、`refused`，`result` 用来判断实际产出。`batches` 保留全部 `waiting`／`running` 批次，以及每个入口按 ID 倒序选取的最近 20 个已结束批次，最终仍按 ID 升序返回；字段不变，不再返回全部历史，完整历史使用管理端批次分页接口。`entries[].current_batch/latest_batch` 仍表示该入口当前／最近已结束批次；近 24 小时调用和延迟统计不受这个批次数上限截断。`entries` / `backlog` 是当前积压快照，`model_health`、`usage`、`timeouts_seconds` 用于运行诊断。全局用量与入口明细的统计范围不同。状态轮询不会变成召回，也不会自动强化记忆。

2026-10-10 上线修复经规划者批准收紧 v1：非法入口 ID 与消息字段上限返回 400／413；受限令牌全局目标写操作返回 403；来源元数据按令牌脱敏；status 批次采用上述有界历史。
