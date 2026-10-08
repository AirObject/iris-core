# Iris：试用对话、记忆管理与宿主接口

需要 [uv](https://docs.astral.sh/uv/) 和 Python 3.12 及以上。开发设备为 macOS，仓库内 `.venv` 使用 Python 3.13；以下命令也适用于 Linux 和 Windows PowerShell：

```bash
uv sync --python 3.13
uv run pytest
uv run --locked --isolated --python 3.12 pytest
```

两个版本的测试依次运行，不同时运行；3.12 使用临时环境，保持仓库 `.venv` 为 3.13。目前只在 macOS 验证，Windows 验证暂停。

可用 `uv run iris setup --name Iris` 创建初始角色。学习命令行可用 `uv run iris ingest messages.jsonl` 接收 UTF-8 消息，再执行 `uv run iris learn <入口标识> --force`。JSONL 每行至少包含 `entry_id`、`platform`、`sender`、`content`、带时区的 ISO `occurred_at` 和入口内唯一的 `dedupe_key`；可选 `kind` 为 `message`、`self_output`、`action_result` 或 `event`。引用可带 `quote_author`、`quote_author_account_id` 和 `quote_content`；场景事件有固定的“场景”主体，行动结果属于“我”。

把 `test-models.example.toml` 复制为仓库根目录的 `test-models.toml` 并填写测试模型配置；该文件被 Git 忽略。在其他 worktree 中设置 `IRIS_TEST_MODELS` 指向同一份配置的绝对路径，不复制密钥文件。2026-10-05 起对话模型为 glm-5.3-flash，embedding 为 doubao-embedding-vision。方舟 GLM 在 `[chat]` 中显式设置 `reasoning_effort = "high"`（依据 `DECISIONS.md` 2026-10-08「GLM 默认推理档位改为 high」）；它不能关闭推理。high 的学习调用更慢（隐藏集上批次 P95 约 35—43 秒），实时入口的 1 分钟目标需在预演中复核。此字段是可选的服务商字符串，未配置时请求中不发送，其他 OpenAI 兼容模型按自身文档选择。`iris models check` 显示当前档位。然后运行：

```bash
uv run iris models check
uv run iris eval learning
```

## 本机服务

```bash
uv run iris
```

首次运行会创建 `./data` 并自动在系统浏览器打开 `http://127.0.0.1:8080/setup`。在页面设置管理员密码、角色名、可选背景和模型即可进入试用；不配置模型也可以完成，顶部持续提示“未配置模型，暂不学习”，接收与全文召回照常。`uv run iris serve --no-open` 关闭自动打开。运行服务无需 Node；Docker 与对外监听留到后续阶段，目前只在 macOS 验证。

部署优先级为 **命令行 > 环境变量 > iris.toml > 默认值**。环境变量为 `IRIS_DATA_DIR`、`IRIS_HOST`、`IRIS_PORT`；当前目录的 `iris.toml` 只接受以下顶层项，可用 `--config <文件>` 指定其他文件。文件中的相对数据目录相对该配置文件解析。`--data-dir` 放在子命令前；兼容原来的 `--db`，显式传入时其父目录就是数据目录。

```toml
data_dir = "./data"
host = "127.0.0.1"
port = 8080
```

```bash
uv run iris --data-dir /path/to/iris-data serve --port 8081 --no-open
```

角色、模型的非敏感字段、每日 token 上限与学习并发保存在 `iris.db`；API key 仅在同目录的 `secrets.json`，Unix 权限为 0600，页面与接口只显示是否已设置。模型配置优先级：**serve --models-config <文件> > IRIS_TEST_MODELS > 数据库设置＋secrets.json**。前两种模式明确显示“配置来自外部文件，只读”，不自动打开浏览器，但管理界面仍要求设置密码／登录。未显式指定时，serve 不会隐式读取当前目录的 test-models.toml；评测和旧离线命令仍沿用测试模型配置约定。

开发时可用下面的命令导入，文件由程序读取，命令不显示密钥。导入可以在服务运行时执行；本地配置模式下自动重载并恢复对应用途，外部配置模式仍以外部文件为准。要在页面编辑导入后的模型，启动服务时须取消 IRIS_TEST_MODELS。

```bash
IRIS_TEST_MODELS=/absolute/path/test-models.toml uv run iris --data-dir /path/to/iris-data models import
# 也可用 models import --from /absolute/path/test-models.toml
# 随后在没有 IRIS_TEST_MODELS 的环境中启动：
uv run iris --data-dir /path/to/iris-data serve
```

`/setup`、`/login` 及其静态资源可在本机打开；完成设置后，试用／记忆／入口与学习／状态／设置页与管理接口均要求管理员会话。交互接口文档 `/docs`、`/openapi.json` 也要求登录。宿主 `/api/v1` 保持原有行为，宿主令牌在 M4；本阶段继续只绑定回环地址。

服务还会在所有路由之前校验 HTTP `Host`，仅接受 `127.0.0.1`、`localhost`、`[::1]`（可带端口）。其他域名即使解析到回环地址，也返回 400，不返回业务数据或执行操作；界面、静态资源、`/admin/api`、`/api/v1`、接口文档均受保护。此检查用于阻断 DNS 重绑定，不解析域名、不信任转发 Host，也不启用 CORS。它与管理员会话和 CSRF 校验共同生效。浏览器和宿主客户端请使用上述本机地址，不使用自定义域名。

## 首次设置、登录与设置

首次设置先建立管理员密码，再填写角色（默认 Iris、浏览器时区）及可选模型。对话模型提供火山方舟 GLM（Agent Plan，reasoning_effort=high）、DeepSeek 和本机服务预设；模型名和可选推理档位以服务商为准。方舟 GLM 档位依据上述 2026-10-08 决定，仅影响以后选用该预设的配置，已保存的模型配置不自动改写。方舟预设使用 [Agent Plan 官方接入地址](https://docs.volcengine.com/docs/ark/agent-plan-enterprise-other-tools?lang=zh) `/api/plan/v3`，应使用对应套餐密钥；其他方舟产品请按所用产品填写自定义地址。“测试连接”只发送固定短请求，单次总预算 10 秒，展示结果和毫秒耗时，不返回服务商正文；它不保存草稿，测试已保存配置时更新现有用途健康状态。

设置页只提供本阶段的角色名称／背景／时区、对话和 embedding 模型、每日 token 上限、学习并发（1—32，默认 2）与模型立即重试。保存后调用既有内部接口对新工作生效，401 显示“密钥无效”；接收和召回仍可用，改对后恢复学习。API key 留空保留原值，勾选“移除”才清除。更换背景撤销旧的初始设定对象、保存修订并建立新设定，学习所得记忆保留；角色名变化不重复建立初始记忆，persona 保留版本历史。修改均写入操作记录，设置页显示最近 30 条。

管理员密码以随机盐＋scrypt（N=32768、r=8、p=3）保存，使用 [OWASP 的 scrypt 参数组合](https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html#scrypt)。会话为 256 位随机值，数据库仅存摘要，绝对有效期 12 小时，登录轮换、退出撤销；Cookie 为 HttpOnly、SameSite=Strict、Path=/，HTTPS 时增加 Secure（M1 本机 HTTP 不启用 TLS）。写请求必须携带会话绑定的 `X-Iris-CSRF` 与 JSON Content-Type，同时检查 Origin 和 Sec-Fetch-Site；不能只依赖 SameSite。五分钟内五次错误密码后暂停登录一分钟，计数持久保存。首次设置核对实际客户端回环地址，serve 不信任代理转发地址；Host 白名单继续覆盖所有路由，不启用 CORS。

管理接口未登录返回 401，未完成设置返回 409，JSON 的 error.redirect 指向 /login 或 /setup；页面本身返回 303。`GET /admin/api/session` 返回状态与 CSRF token，并建立短期匿名会话，首次设置和登录也需要该 token。匿名响应不含角色、记忆或模型配置。页面／管理响应不缓存，页面拒绝被 iframe 嵌入。

| 方法与 `/admin/api` 下的路径 | 用途 |
| --- | --- |
| `GET /session` | 登录／设置状态和会话绑定的 CSRF token |
| `POST /setup/password`、`POST /setup/complete` | 首次管理员密码、完成角色设置 |
| `POST /login`、`POST /logout` | 登录、撤销当前会话 |
| `GET /settings` | 角色、脱敏模型、限制、健康和最近操作 |
| `PATCH /settings/role` | 保存 name、background、timezone |
| `PUT /settings/models/{chat\|embedding}` | 保存模型；api_key 省略保留，空字符串清除，enabled=false 停用 |
| `POST /settings/models/{chat\|embedding}/test` | 空 JSON 测已保存配置，模型 JSON 测草稿 |
| `POST /settings/models/{chat\|embedding}/retry` | 对暂时不可用／账户问题请求重试 |
| `PATCH /settings/limits` | daily_token_limit（null 不限）、learning_concurrency |

## 中文试用与管理界面

服务根路径 `/` 提供 React＋Vite＋TypeScript 界面，适配桌面和手机；`/#/trial` 是试用对话，`/#/memories` 是记忆管理，`/#/learning` 是入口与学习，`/#/status` 是运行状态，`/#/settings` 是设置。构建产物已经提交到 `src/iris/web/` 并随 wheel 分发，运行服务不需要 Node，也不从 CDN 加载脚本或字体。

- **试用对话**：先创建一个群聊或私聊入口，默认发言人“我（用户）”是他人主体，区别于角色的 `self`。可添加虚拟发言人；同一发言人跨试用入口保持身份，同名新增发言人仍是独立主体。入口默认实时节奏，可请求立即学习。消息列表每次取最近 100 条，支持读取更早消息。
- **角色回复**：新入口默认关闭，按入口在当前浏览器本地保存选择；切换入口或刷新后恢复。存储不可用或读写失败时不保存选择，当前页面仍可开启，刷新或重新挂载后回到关闭。开启后通过现有 `prepare` 取 persona、记忆、本入口近期消息、状态和目标，再用 `trial_reply_v1` 和 `json_chat` 生成 JSON。只有校验后的 `reply` 正文发布为角色实际输出 `self_output`，进入正常学习队列；失败保留原消息且不发布输出。每入口最多一个回复调用，同一消息成功回复后重试复用已发布输出（含服务重启后）；没有成功输出且原消息已超出近期 20 条上下文时拒绝生成。不自动把召回记忆标成已使用。对外 `/api/v1` 继续只准备材料，不生成回复。
- **学习动态**：每两秒刷新待学习数量、当前／最近批次、本入口最近 12 条新增或更新记忆，可展开来源与前后文。接收、学习成功、形成记忆、等待重试、放弃和拒绝分别显示；回复准备结果只在发送或请求回复时获取，轮询不会反复记召回记录。Persona、当前状态和目标为只读；当前状态仍是 M1 空分区。
- **记忆管理**：按正文／标签文本、人物、类型、出处入口、事件日期筛选（无事件日期时用创建时间），按文本相关度、更新时间或保留强度排序，分页每页 30 条。全文使用已有 jieba 索引，管理浏览不调用模型、不写召回记录。已删除历史可按原正文子串查询。详情显示分数、人物、来源前后文、派生关系、修订和人工操作、真实召回／使用次数。正文编辑和删除都携带 `expected_revision`；409 时保留草稿并要求重新读取最新修订。
- **删除**：只把目标对象标为 deleted，保留它的 ID、来源和修订，不删除共享消息或其他记忆；原 ID 不会复活，也不参与普通／深度召回。后端允许编辑／删除遗忘记忆；M2 的置顶、遗忘、恢复、分数调整、彻底清除与按旧内容新建已提供管理 API，页面控件由界面线接入。
- **入口与学习**：同时列出试用与宿主入口的名称、平台、类型、节奏、待学习数量和最近／当前批次。批次列表可按入口和组成日期筛选；详情包括冻结的历史／目标／后续消息、消息类型与发送者、每次尝试的时间／总耗时／解析结果、模型请求的结束原因与推理档位、原始正文与 JSON 修正正文、规整及丢弃记录，以及新增／更新／确认的记忆详情链接。原始正文默认折叠，页面不会展示服务商推理字段。
- **重新学习与记忆缺口**：仅放弃或内容拒绝的批次可重新学习；保留原批次 ID、三段范围、历次尝试与缺口，将状态改回 waiting、目标消息改为 batched，本轮尝试数归零（重新获得 4 次）。历次尝试编号持续递增。已在等待／运行／成功的批次返回 409，重复操作不会排队两次；模型暂停时排队等待恢复，同入口在途批次结束后调度。失败仍只保留该批次的一条缺口，原因与时间按最新终止结果更新；第 4 次尝试被重启中断时也替换该缺口，避免唯一键冲突阻止启动。缺口接口与页面使用稳定的 `batch_id` 标识；**只有学习成功的事务才清除缺口**（设计 7.6、17.7）。缺口页按入口和消息时间区间筛选，重新排队／学习时标为“重新学习中”。只读浏览不调用模型、不写召回或学习状态。
- **运行状态**：复用既有状态数据，显示对话／embedding 的健康与错误、队列、今日 token 用量、近 24 小时学习耗时／超时，以及最近用途和调度错误摘要。没有定价信息时不估算费用；状态接口不提供滚动日志全文。

管理请求和响应使用 UTF-8 JSON，额外字段和非法值被拒绝；消息正文沿用 32KB UTF-8 上限。所有管理接口在 `/admin/api`，与宿主接口分开：

| 方法与路径 | 用途 |
| --- | --- |
| `GET /admin/api/trial` | 试用入口、发言人、角色名 |
| `POST /admin/api/trial/entries` | 建立入口：`name`、`kind=group/private` |
| `POST /admin/api/trial/speakers` | 添加虚拟发言人：`name` |
| `GET /admin/api/trial/entries/{id}` | 对话、最近记忆与角色上下文；可传 `before` 读取早期消息 |
| `POST /admin/api/trial/entries/{id}/messages` | 接收：`speaker_id`、`content`、`dedupe_key` |
| `POST /admin/api/trial/entries/{id}/learn` | 请求立即学习，暂停时仍接受请求 |
| `POST /admin/api/trial/entries/{id}/prepare` | 获取回复准备材料 |
| `POST /admin/api/trial/entries/{id}/reply` | 为 `message_id` 生成并发布试用回复 |
| `GET /admin/api/catalog` | 记忆筛选用人物、入口目录 |
| `GET /admin/api/memories` | `text/person_id/kind/entry_id/time_from/time_to/lifecycle/sort/limit/offset` |
| `GET /admin/api/memories/{id}` | 详情（包括已删除历史） |
| `PATCH /admin/api/memories/{id}` | 编辑正文：`expected_revision`、`content` |
| `DELETE /admin/api/memories/{id}` | 撤销对象：JSON 中传 `expected_revision` |
| `GET /admin/api/entries` | 所有入口及待学习数量、当前／最近批次 |
| `GET /admin/api/batches` | 批次分页：`entry_id/time_from/time_to/limit/offset` |
| `GET /admin/api/batches/{id}` | 三段消息、历次尝试、正文、规整／丢弃／记忆结果 |
| `POST /admin/api/batches/{id}/relearn` | JSON `{}`；受管理员会话和 CSRF 保护，已排队等状态返回 409 |
| `GET /admin/api/memory-gaps` | 缺口分页：同批次参数，日期匹配消息时间区间的交集 |
| `GET /admin/api/status` | 现有服务状态的管理入口 |

批次／缺口分页默认 30 条、最多 100 条；日期按角色时区解释，结束日期含整天，带时区时间按实际时刻比较。重新学习与 `admin_operations` 中的 `batch_relearn` 记录在同一事务提交。运行状态的近 24 小时学习调用按 `created_at DESC, id DESC` 返回，页面取前 12 条，耗时分位数仍使用全部记录。调用诊断按同一批次和尝试起止时间关联；历史记录不足或进程中断无法准确关联时单列，不用当前模型配置推断。详情只展示持久保存的正文，未返回正文的拒绝／网络失败明确显示“没有记录”。规整和丢弃信息来自已提交的批次结果，失败尝试只展示已保存的正文与诊断。

编辑／删除在同一事务中写 `memory_revisions`；该记录包含对象、操作者、时间、前后内容、动作理由，详情分别投影为修订历史与人工操作记录。迁移 008 将人工修订的标识汇入 admin_operations，其他生命周期操作也在其中记录，不重复保存正文。人工编辑与在途学习的冲突通过修订号拦截；最近一次正文修改来自人工编辑时，后续学习也不会自动改写正文或相信程度，会跳过该项并记录原因。再次确认仍增加保留强度（设计 12.5）。来源修订不匹配的派生记忆在详情标为待复核，确定性的依据失效扣减见下文，模型重整留到 M3。

只有修改界面时需要 [Node 与 npm](https://vite.dev/guide/)（Node 22.12 及以上）：

```bash
cd frontend
npm ci
npm test
npm run build
cd ..
uv run pytest
uv run --locked --isolated --python 3.12 pytest
uv build
```

`npm test` 使用 Vitest＋Testing Library＋jsdom，覆盖开发代理的同源 CSRF、首次设置、登录／退出、模型设置及外部只读，以及发送、角色回复、编辑冲突、删除、筛选、状态页、入口列表、批次详情、重新学习／冲突和缺口筛选。`npm run build` 先执行 TypeScript 检查，再更新 Python 包内的资源；源码和产物要一起提交。`npm run format` 格式化前端。开发时先启动 `iris serve`，再在 `frontend/` 执行 `npm run dev`；Vite 仅监听回环地址，代理 `/admin/api` 到本机 8080 端口，保留浏览器 Host，使 Origin／CSRF 校验仍按浏览器同源执行。静态包没有前端开发依赖。

## 宿主接入

下面的 Python 示例依次调用接收消息、回复准备和使用反馈；请求与响应均使用 UTF-8 JSON：

```python
import httpx

with httpx.Client(base_url="http://127.0.0.1:8080", timeout=10) as client:
    received = client.post("/api/v1/entries/group-a/messages", json={
        "platform": "chat", "sender": "小林", "account_id": "lin-001",
        "content": "我喜欢桂花乌龙茶", "occurred_at": "2026-09-29T20:00:00+08:00",
        "dedupe_key": "host-message-001", "kind": "message",
    })
    received.raise_for_status()
    print(received.json())  # message_ids、pending_count；重复键返回原消息

    prepared = client.post("/api/v1/entries/group-a/prepare", json={
        "text": "给小林准备什么饮料？", "participants": ["小林"],
        "known_memory_ids": [], "recent_limit": 20,
    })
    prepared.raise_for_status()
    material = prepared.json()
    print(material["persona"], material["memories"], material["recent_messages"])

    # 宿主生成回复后，仅填写确实使用的记忆；不要把所有返回记忆自动当作已使用。
    actually_used = []
    feedback = client.post("/api/v1/feedback", json={
        "recall_id": material["recall_id"], "memory_ids": actually_used,
    })
    feedback.raise_for_status()
```

接收接口也接受上述消息对象的数组，整批提交在一个事务中完成。正文上限为 32768 个 UTF-8 字节；超限返回 413，不截断。首次入口自动创建，可以在消息中用 `pace` 选择 `realtime`、`standard`（默认）、`economy`，或对象 `{"count":6,"idle_seconds":30,"max_wait_seconds":180}` 自定义节奏；入口创建后沿用已保存的节奏。接收返回后，由后台按数量、空闲、最长等待、关注信号触发学习。等待时间按接收时间计算，历史消息的发生时间仍用于学习事实和相对日期。

离线 `iris learn group-a --force` 只允许在服务停止时使用。服务和离线写入命令使用操作系统文件锁，避免两个进程共同写同一数据库；崩溃后锁自动释放。不要手动删除锁文件来绕过它。

实时／标准／省流的数量分别为 4／12／30 条，空闲为 5 秒／10 分钟／30 分钟，最长等待为 1 分钟／1 小时／4 小时。待学习消息提到角色名、`@`、`记住` 或 `别忘了` 时，空闲条件缩短到至多 1 分钟。手动触发接受后立即返回，暂停期间也接受并给出原因：

```python
print(httpx.post("http://127.0.0.1:8080/api/v1/entries/group-a/learn").json())
# {"accepted": true, "pending_count": 2, "paused": false, "reason": null}
```

### 高流量入口过滤（M2）

`GET /admin/api/entries/{entry_id}/settings` 读取入口设置，`PATCH` 修改已存在入口的节奏和过滤。沿用管理员会话、JSON Content-Type、X-Iris-CSRF、Origin／Sec-Fetch-Site 及 Host 校验；无效值返回 400，不存在的入口返回 404。写入与 `entry_settings_updated` 操作记录在同一事务完成，记录前后设置。界面后续接入，当前可通过管理接口设置：

```json
{
  "pace": "economy",
  "filters": {
    "min_chars": 3,
    "mention_only": true,
    "context_messages": 2,
    "max_batches_per_hour": 10
  }
}
```

PATCH 可只提供 `pace` 或部分 `filters` 字段，其余沿用旧值。`pace` 支持原三种预设，或完整自定义对象（count 1—1000、idle_seconds 1—86400、max_wait_seconds 1—604800）。过滤默认全部关闭：min_chars=0、mention_only=false、context_messages=0、max_batches_per_hour=0。整数严格校验，不接受布尔值或数字字符串；min_chars 范围 0—32768、context_messages 0—100、max_batches_per_hour 0—1000。长度与上限的 0 表示关闭；窗口为 0 时仅保留提及消息。

- 长度为正文去掉首尾空白后的 Unicode 字符数，包含标点、emoji 和内部空白，不按 UTF-8 字节或汉字分词计数。
- 提及匹配正文中的当前角色名或 `subject_aliases` 中属于 self 的已确认别名，采用与既有角色名关注信号相同的区分大小写的子串匹配，包含 `@角色名`／`@别名`。仅有发送者名、引用作者、引用正文、其他人的别名、单独 `@别人` 或“记住”“别忘了”均不能通过提及过滤。
- 前后 K 条按本入口原始接收顺序计数，所有消息类型和短消息都占位置，其他入口不占位置。每条消息等到 K 条后续消息到达，或调度器观察到入口达到现有空闲条件后定案；关注信号仍将空闲缩短到至多 1 分钟。长度不足可立即过滤，但短提及本身仍能让相邻的足长消息通过。长度与窗口同时开启时取交集。
- 等待窗口的尾部仍是待处理消息，不进入三段；已有足够后文的前部可以先组批。空闲关闭窗口后不再追溯恢复已过滤消息。手动学习遵守窗口与小时上限。三种节奏的数值、关注信号和 token 上限保持不变；调度触发后只从已经定案的可学习消息组成批次，可能少于条数上限。
- `filtered` 是已处理的最终状态，不形成记忆缺口，不计入待学习积压，也不进入任何新批次的历史、目标或后续段。原文继续出现在本入口近期消息中，超过消息保留期且无既有引用保护时清理。放宽／关闭过滤不恢复已经过滤的消息。
- 设置对尚未冻结的待处理消息立即生效。已进入某批任一段的消息保留原入选决定，包括已冻结后续段转为下一批目标的情况；已有三段、重试与学习请求保持不变。每个新批次保存当时的设置快照，详情以 `entry_settings` 返回，旧批次为 null。

小时上限按本入口滚动 60 分钟内**新冻结的批次**计数，所有结果均占额；同批重试和重新学习不重复计数。检查与冻结在同一写事务内完成，重启不重置额度。满额时消息按原顺序继续等待，不丢弃，也不因消息保留期而清理待学习积压。长期流入速度高于允许的处理速度时，积压与磁盘占用会持续增长；提高上限或减少流入后按原顺序消化，不扩大批次绕过 token／条数限制。

管理 `/entries` 的每项返回 `filters`、`queue_wait`；`/status` 的 entries／backlog 及 `/batches` 的独立 `entry_waits` 列表也提供 `queue_wait`。`reason=hourly_batch_limit` 表示上限等待，`retry_at` 为最早释放额度的时刻（不是完成承诺）；`reason=filter_context` 表示只有尚待窗口定案的消息。另有 limit、batches_last_hour（未启用上限为 null）、filter_waiting_count、filtered_count。`entry_waits` 是当前入口状态，按 entry_id 筛选，不受批次日期／分页影响；它不伪造空批次。宿主状态与回复准备的既有结构不改，管理读接口不会触发过滤或学习。

默认同时学习 2 个批次，同一入口严格串行。可用 `iris serve --learning-concurrency 1` 修改并持久保存并发数（1—32）。手动请求持久保存到请求当时的消息位置，重启后继续处理该范围的尾部。正常关闭服务时等待在途任务完成；强制终止后由下次启动恢复。

准备结果还包含空的 `state`、未结束的 `goals`、运行 `hints` 和 `recall_id`。查询省略或为 null 时，使用默认 `adaptive_6`：取最新他人消息及其之前五分钟内连续的他人消息，按原顺序组成查询，最多 6 条；角色输出、行动结果和场景事件不进入查询。可用消息超过两条时，最新消息本身足够完整、又不含指代，就只用最新消息；含指代或内容不足时带上前文。只有 1—2 条可用消息时保留全部短上下文。锚点只取最新一条他人消息中的姓名／别名，更早的人名仅提供上下文，不限制返回范围。显式查询文本（包括空字符串）保持原有行为。`participants` 接受主体 ID 或无歧义名字／别名，同名账号用 ID；省略或为 null 时从最近 20 条消息推断，按最近发言顺序排列，传空数组不取人物要点。参与者不会排除关于我自己或其他人的记忆。

相关记忆先入选（`reason: "relevant"`），剩余名额按参与者轮流用其重要记忆补位（`reason: "person_highlight"`），要点合计最多三条，不包含我和场景。人物要点是对方的背景，不表示回答了当前问题。显式查询文本点名的主体或别名限定范围；自动查询仅以最新他人消息点名：记忆须涉及、出自或正文提及此人，同名主体都保留。`recent_limit=0` 关闭近期消息返回，记忆合计最多 8 条、1500 个估算 token，目标最多 10 个。模型失败时说明全文检索降级。定向 search 不补人物要点。

回复准备默认对前 8 个 `relevant` 候选做召回判断，支持分 ≥50 才保留；人物要点不判断，删去的相关项不改为人物要点，也不补位，其他顺序不变。`/memories/search` 不判断。试用回复使用同一个 prepare。判断请求中的别名只涉及前 8 个相关候选的说话人和涉及者、参与者、近期消息发送者及固定查询中点名的主体。

prepare 可以传 `"judge": false` 关闭本次判断，或 `"judge_budget_seconds": 3` 缩短预算（必须 >0 且 ≤10 秒）。响应的 `judgment` 和 `hints` 标明 `applied`／`disabled`／`degraded`、`reason`、`duration_ms`、`queue_ms`、`network_ms` 和 `removed_memory_ids`。未配置、暂停、额度耗尽、排队满、超时或非法输出都保留仍有效的原候选；调用后逐条核对修订号、生命周期、宿主已知记忆，并以最终返回的近期消息检查冗余；失效项记入 `stale_memory_ids`。新消息或别名变化不废弃整次判断，其他候选仍按原判断保留或移除，状态保持 applied。调用中关闭判断的配置仍生效。只有最终返回的记忆取得本次反馈资格。

`recall_judge` 是独立模型用途，未单独配置时继承 chat 的接口、密钥和模型，推理档位固定默认 high；chat 的暂停不暂停判断，判断的暂停也不暂停学习。可在测试配置中增加 `[recall_judge]`，或通过已有管理员接口 `PUT /admin/api/settings/models/recall_judge` 保存独立模型及档位；`enabled:false` 清除该独立配置、恢复继承。全局关闭使用 `PATCH /admin/api/settings/recall-judge` 的 `enabled:false`。此 PATCH 还支持 `concurrency`（1—8，默认 1）和 `queue_limit`（0—64，默认 8），省略字段保留现值；模型来自外部文件时模型配置仍只读。管理设置响应列出实际配置、`inherited` 和独立健康状态，现有设置页随后接入。

判断最长总预算 10 秒，包含排队，只有一次网络尝试，不修正 JSON。达到并发／队列上限时有界等待或立即降级；429 及账户限额按服务商错误码优先分类。前两次临时限流进入独立 `rate_limited` 退避，有有效 `Retry-After` 时按它等待（支持秒数或 HTTP 日期），没有时分别等待 2、4 秒；`retry_at` 标明截止时间。期间新 prepare 直接降级，不排队、不发请求，到期由下次业务请求恢复，无需探测。连续第三次可重试错误（含限流）进入暂停，后台按 60／120／240／480／600 秒探测；更长的 Retry-After 或仍未到期的退避均保留。迟到的成功不提前解除退避，迟到的限流不缩短已有等待；成功业务请求或成功探测清零计数。账户限额继续按账户问题暂停。判断与学习使用独立并发槽位；同账号仍共享服务商额度，不能保证没有账号限流。已达每日 token 上限时不再开始判断或判断恢复探测，基础召回继续。

定向查询和状态示例：

```python
import httpx

result = httpx.post("http://127.0.0.1:8080/api/v1/memories/search", json={
    "text": "观星", "people": ["小林"], "kinds": ["计划"],
    "stances": ["亲历"], "time_from": "2026-09-01", "time_to": "2026-10-31",
    "include_forgotten": False, "limit": 8,
}).json()
print(result)
print(httpx.get("http://127.0.0.1:8080/api/v1/status").json())
```

深度查询显式设置 include_forgotten，结果的 lifecycle 标明 active／forgotten；读取不改变状态。反馈有效期为 24 小时，同次召回同条记忆只强化一次（默认 +8，上限 100），按双阈值决定恢复，不改变相信程度或修订号；已删除对象不能反馈。错误码：400 参数／反馈无效、404 不存在／已删除、413 消息过大、503 暂不可用；409 保留用于修订冲突，当前宿主接口不修改记忆正文。

## M2 生命周期与每日维护（后端）

保留强度限制在 0—100：有效记忆低于 F（默认 20）进入遗忘并记下时间；遗忘记忆达到 H（默认 35）恢复并清除遗忘时间；两阈值之间保持原状。管理员手动遗忘把强度降至至多 F−1 并取消置顶，恢复把强度提至至少 H，不重新置顶。置顶不自动恢复，也不受维护的自动变化：不衰减、不受依据失效扣减、不自动删除。重要度改变增加修订号；置顶、状态与保留强度调整不增加修订号，但所有人工操作仍核对 expected_revision 并留操作记录。

默认每天按实例 timezone 的 03:00 维护，不调用模型；接收、学习、准备和反馈照常。启动时超过 24 小时没有完成运行，才在随后所有入口安静满 10 分钟时补跑一次；安静时间从启动或最近接收时间中较晚者计算。中断的运行优先续跑，阶段游标、检查计数和项目变更在同一小事务中提交，已完成衰减不重做；续跑中新进入遗忘的时间记为实际执行时间，保留期不从旧请求时间起算。一次运行固定设置和对象上界，新设置对以后接受的运行生效，反馈与人工操作立即读取最新值。报告只为实际变更及失败保存逐项 ID；检查数量按阶段汇总，冲突／跳过按原因计数，无变更项目不逐条留记录。

衰减只对有效、未置顶的记忆按实际执行次数计算：重要度 <40 每次减 1；40—69 的每条记忆累计三次符合条件的维护后减 1；≥70 不衰减。遗忘期间不衰减、不增加衰减计数，自动删除计时照常；未置顶者仍按新设置的 H 检查恢复，保留强度不变。中等重要度计数只在符合条件且修订校验成功时累加，暂时置顶、遗忘或改到其他区间保留余数；停机、错过的日子不补算，手动运行也计一次。维护会按阈值转换状态、删除遗忘满 180 天的记忆，并对实例时区前一天放弃的批次复用“重新学习”，每批一生只自动重试一次；拒绝批次不自动重试，缺口仍仅在成功事务中清除。

依据记忆进入遗忘或删除时保存依赖事件；维护对 sources.kind=memory 的每对（依赖记忆、依据记忆）只扣一次，默认 −10，不改相信程度。依赖对象置顶时保留待处理事件，取消置顶后由后续维护处理。依据在维护前恢复也不丢事件，恢复不返还，循环依赖不会反复扣减。模型判断独立证据与纠正后的重新整理留到 M3。

消息按接收时间超过 30 天且已学习／已放弃／内容拒绝／已过滤才可清理，未学习消息保留。记忆来源、goal_sources、主体别名／联系证据、立即学习位置继续保护；批次三段只在 waiting／running 时保护，succeeded／abandoned／refused 的批次和试用回复去重键不保护消息，回复本身按相同规则清理。批次仍保留原消息 ID，详情对缺失消息显示占位；目标段缺失时不能重新学习，管理请求返回 409 并说明“已清理”，自动重试跳过，记忆缺口保留。消息 ID 使用 AUTOINCREMENT，清理最大 ID 后也不会串到后来收到的消息。

普通删除仍保留历史和来源。彻底清除必须显式传 confirm=true，删除修订、标签、涉及人及自身来源关系；来源消息使用同一套引用保护规则，只有没有其他引用时才删除，响应列出删除及受保护的消息 ID／原因。**已知限制：按设计 12.4，彻底清除删除修订历史和只被该记忆引用的来源消息，不清除批次尝试中的模型原始输出及调用记录里已有的内容。**数据库保留无正文、无向量的 deleted 占位行，管理列表／详情不再展示，确保 SQLite 也不会复用最大 ID；其他记忆的依据引用继续指向这个已清除对象。按旧内容新建使用所选修订的正文／判断，生成新 ID，按 30＋重要度×0.4 初始化强度，不继承置顶、遗忘时间或向量；沿用当前 about、tags 和现存来源（包括来源记忆修订号），因为旧修订并未完整快照这些关系，不伪造历史来源。

以下接口沿用管理员会话、CSRF 与 Host 校验；记忆写请求都带 expected_revision：

| `/admin/api` 下的方法与路径 | 参数／用途 |
| --- | --- |
| `PATCH /memories/{id}/lifecycle` | pinned、importance、retention 至少一项 |
| `POST /memories/{id}/forget`、`/restore` | 手动遗忘／恢复 |
| `POST /memories/{id}/purge` | confirm=true；不可撤销 |
| `POST /memories/{id}/recreate` | source_revision；返回 201 与新对象 |
| `GET /memories/upcoming-deletion` | 即将删除及已到期未处理的非置顶对象；默认窗口 14 天，关闭自动删除时为空 |
| `POST /maintenance` | JSON `{}`，202 返回 run_id；已有未完成运行时复用它 |
| `GET /maintenance`、`/maintenance/{run_id}` | 分页运行列表／完整逐项报告 |
| `GET /operations` | time_from、time_to、action、actor、object_type、object_id；limit／offset 分页 |
| `PATCH /settings/lifecycle` | 部分更新生命周期设置；`GET /settings` 的 lifecycle 返回完整值 |

列表默认每页 30 条、最多 100；操作记录日期按实例时区解释，结束日期含整天。管理员写操作、宿主每次立即学习／反馈、每个批次结果和每次维护摘要进入操作记录；宿主接收消息不逐条记。记录只含 ID、计数、分数、设置和结果类别，不含消息正文、凭据或 Cookie。

生命周期设置：forget_threshold=20、restore_threshold=35（1≤F<H≤100）；feedback_increment=8、confirmation_increment=5、decay_amount=1、dependency_penalty=10（幅度 0—100）；auto_delete_enabled=true、auto_delete_days=180、upcoming_delete_days=14、message_retention_days=30（天数 1—36500）；maintenance_time="03:00"（严格 HH:MM）、abandoned_retry_enabled=true。学习确认的可复用接口已经提供，learning.py 的接入由学习线完成，当前学习 SQL 尚保持原 +5 行为。

## 模型故障与状态

学习首次请求、调用内重试和 JSON 修正共享 180 秒总预算；其他生成请求为 120 秒，embedding 为 30 秒，召回查询 embedding 仍限 2 秒且不重试，评测判分为 240 秒。召回判断另有包含排队的 10 秒总预算、仅一次尝试。其他调用内最多再试两次；有有效 `Retry-After` 时按秒数或 HTTP 日期等待，否则分别以 2、4 秒为基数，加上 0 到基数之间的均匀随机抖动。等待和后续请求不得超过同一个总预算。批次重试间隔为 1／5／15 分钟，共四次尝试。

对话和 embedding 分别维护状态：`normal`、`temporarily_unavailable`、`invalid_key`、`configuration_error`、`account_problem`；每日上限造成学习暂停时，对话用途显示 `usage_limit`。连续三次可重试的网络／超时／限流／5xx 请求错误会暂停该用途（包含调用内重试的失败），触发暂停的批次失败不扣尝试次数。探测使用固定短请求，间隔为 1、2、4、8、10 分钟，之后保持 10 分钟；成功即恢复。分类优先识别结构化 `error.code`，其次兼容已知 `error.type`，最后按 HTTP 状态兜底。401 是密钥无效，404 及 `InvalidParameter`／`MissingParameter` 是配置错误，只在配置实际改变后恢复；403 权限、欠费、订阅及 `QuotaExceeded`／`SetLimitExceeded` 属账户问题，不作调用内短间隔重试，保留定时探测和内部立即重试。`AccountRateLimitExceeded`、`ServerOverloaded`、`RequestBurstTooFast`、500、传输错误、超时及审核服务故障 `ContentSecurityDetectionError` 可重试。方舟 `SensitiveContentDetected` 家族、输入／输出文本审核和风控命中，以及 `finish_reason=content_filter`，直接进入内容拒绝终态，不修正 JSON、不写记忆；原 MiniMax 敏感字段与 `base_resp` 识别保留。码表来源见[方舟官方文档](https://docs.volcengine.com/docs/ark/error-codes?lang=zh)。

服务会重新读取当前模型配置来源，修改模型配置（含推理档位）后对新工作生效。管理设置接口调用 `Gateway.replace_config(kind, ModelConfig(...))`、`Gateway.retry_now(kind)`，凭据只保存到 secrets.json；外部文件模式保持只读。`ModelHealth.set_daily_token_limit(整数或 None)` 设置每日 token 上限，默认不限；按角色 `timezone` 的次日零点或调高上限恢复。额度根据服务商已报告用量计算，在途请求可能越过上限，未报告 token 的调用不能计量。接收和召回始终照常。

embedding 暂停时网关直接返回可降级错误，回复准备和查询使用全文，并附 `model_paused` 提示。缺少向量的记忆照常进入全文索引，恢复后后台按修订号补算。embedding 维度参与配置变化检测、恢复探测和旧向量补算；改变模型或维度后仍须重新标定召回。`GET /api/v1/status` 提供 `model_health`、各入口 `current_batch`／`latest_batch`、`memory_gap_count`、今日／本周 `usage`、`learning_latency_24h`（P50／P95／最大耗时／超时数）和 `scheduler.running`；兼容保留最近调用 `models` 和积压 `backlog`。

迁移 006 为 `model_calls` 增加 `reasoning_effort`、`reasoning_present`、`reasoning_chars`；历史记录保持 NULL。推理字符数按 Unicode 字符计，已返回但非文本的推理字段记为未知；正文解析只用 `message.content`，不存 `reasoning_content`／`reasoning` 全文。`reasoning_tokens` 取 `usage.completion_tokens_details.reasoning_tokens`，缺失记 NULL，不从字符数估算；completion 用量已包含推理，不能再次相加。状态额外提供当前 `chat_reasoning_effort` 及逐次学习诊断。用量合计只覆盖已报告值，不能把未知调用当成零消耗。

运行日志同时输出控制台和数据库所在目录的 `logs/iris.log`，每 2 MB 滚动，保留 3 份旧文件；只记录状态、标识和错误类别，默认不记录消息正文或 API key。

## 评测

```bash
uv run iris eval learning --split dev --judge-runs 1
uv run iris eval learning --judge-runs 2
uv run iris eval learning --judge-mode external --out <运行目录>
uv run iris eval e2e --judge-mode external --out <运行目录>
uv run iris eval e2e --judge-mode external --script E001 --script E011 --out <流程检查目录>
uv run iris eval e2e-score --materials <材料目录> --judgments <第一轮目录> --judgments <第二轮目录> --judge-model <执行者模型名称> --out <报告目录>
uv run iris eval recall
uv run iris eval recall --split dev --calibrate
# 如需重新比较 1024／2048 维，再加 --compare-embeddings
uv run iris eval recall --corpus <外部 JSON 或 JSONL> --out <外部目录>
uv run python evals/benchmark_retrieval.py --default-config
```

语料格式、完整外部明细、标定方法和报告见 [evals/README.md](evals/README.md)。学习和召回的最终验收仍需规划者运行隐藏集。

当前回复检索默认 trigram＋2048 维 float32，向量下限 0.35、相对比例 0.75、向量／全文权重 1:1，查询加“为这个问题检索能回答它的个人记忆：”前缀；全文覆盖率 0.75，长片段最大文档频率 2。无向量时使用独立标定的 jieba 全文降级，覆盖率为 0，长片段文档频率通路关闭。新数据库写入这些默认值，已有设置保留。学习材料保持独立的 PR #4 设置。

trigram 查询中的一字／两字实词会补查已有 jieba 索引；只询问已知姓名时，也能按涉及人、说话人及正文提及取回候选。长短词结果按全查询词项覆盖率筛选，稀有长词片段也可按整库文档频率入选，之后合并去重，人物与类型等过滤继续生效，混合检索和全文降级均覆盖短词。

召回评测默认运行七份公开集（含 recall_no_answer_v1），共 243 条固定记忆、228 条查询。`default_judged` 使用正式 prepare 判断路径；原全文／混合对照关闭判断。报告单列判断降级次数和占比；有降级的运行不能作为正常路径结果。GLM 阶段全部按 dev 使用，保留原 split 作历史分组；第四轮新增 16 条手写短词查询，先单独冻结；第五轮继续按原规则在 122 条 dev 上重新标定。选参、逐项对照和已知问题见 [evals/README.md](evals/README.md)。本次对话查询默认值由规划者批准为守则例外：conversation_v2 relevant 精确率下降 0.0682，conversation_v3 无答案误返由 1/4 增至 3/4，见 [第二轮报告](evals/reports/conversation-prepare-20261008.md#第二轮)。召回判断按 PR #25 已批准方案固定，不重新选参。

点名检查仅读取检索和人物要点候选的正文。性能脚本加 `--default-config` 覆盖当前默认的 5 千／5 万条、点名／不点名四组；不带该参数还会比较维度和 dtype，并在合成测试库中关闭向量截断。性能方法和本次报告入口见 [evals/README.md](evals/README.md)。学习影响按 2026-10-06 决定改为全部公开学习语料的确定性请求比较；第一阶段真实学习对照已作废。

学习与端到端评测默认仍调用对话模型判分，只作预览；正式评测使用外部执行者判分。端到端外部模式导出每检查点一份材料，不调用判分模型。材料目录含 manifest.json、scoring.md、cases/*.json、run.json 和 round-template.json；每轮把模板复制为 manifest.json，按清单写 facts／forbidden 判分。e2e-score 离线严格校验后生成原格式 JSON 报告，一次／两次 --judgments 分别为单判／双判，事实覆盖取 AND、禁止说法取 OR，U04 单列。完整文件约定及独立判分要求见 [evals/README.md](evals/README.md#端到端外部判分流程与文件格式)。小样本单判只用于流程检查，不作门槛结论。

端到端公开集现为 11 个手写脚本，包括群聊学习、重启后私聊提问的 E011。prepare 的查询文本只省略或使用提问原文；评测器另行校验近期原始消息没有跨入口。跨入口来源格式见 [评测说明](evals/README.md)。

评测启动子进程时，临时 TOML 只记录端点、模型和 `api_key_env` 变量名，密钥仅通过该子进程环境传递；不会修改调用者环境。评测的配置来源仍为 `test-models.toml`／`IRIS_TEST_MODELS`，serve 的配置优先级见上文。需要时也可在模型配置组中用 `api_key_env` 引用已设置的环境变量，不能与 `api_key` 同时填写；变量缺失会明确报错。

GLM 服务商默认 max 推理可能耗尽 180 秒学习预算；依据 `DECISIONS.md` 2026-10-08「GLM 默认推理档位改为 high」，设置页的方舟预设及开发配置使用 high，未指定档位的外部配置仍由服务商决定。学习仍共享 180 秒预算。学习提示词默认 v6，先筛长期价值，再核对完整证据、人物归属、别名声明和时间一致性；v5 保留用于对照。推理档位和方舟错误码适配已合入；M1 的评测门槛在定稿代码上以公开样本和规划者隐藏集的独立双判达到，M1 已完成（DECISIONS.md 2026-10-08）；公开样本的单判及流程试用仍不能作为门槛结论。
