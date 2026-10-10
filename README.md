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

开发时可用下面的命令导入，文件由程序读取，命令不显示密钥。导入可以在服务运行时执行；本地配置模式下自动重载并恢复对应用途，外部配置模式仍以外部文件为准。导入和页面保存遇到配置锁占用时最多等待 3 秒，超时提示稍后重试；后台重载遇忙留到下次轮询。要在页面编辑导入后的模型，启动服务时须取消 IRIS_TEST_MODELS。

```bash
IRIS_TEST_MODELS=/absolute/path/test-models.toml uv run iris --data-dir /path/to/iris-data models import
# 也可用 models import --from /absolute/path/test-models.toml
# 随后在没有 IRIS_TEST_MODELS 的环境中启动：
uv run iris --data-dir /path/to/iris-data serve
```

`/setup`、`/login` 及其静态资源可在本机打开；完成设置后，试用／记忆／入口与学习／状态／设置页与管理接口均要求管理员会话。交互接口文档 `/docs`、`/openapi.json` 也要求登录。宿主 `/api/v1` 自 M4 起需要宿主令牌（见下文「宿主接入」）；服务继续只绑定回环地址。

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
- **学习动态**：每两秒刷新待学习数量、当前／最近批次、本入口最近 12 条新增或更新记忆，可展开来源与前后文。接收、学习成功、形成记忆、等待重试、放弃和拒绝分别显示；回复准备结果只在发送或请求回复时获取，轮询不会反复记召回记录。Persona、当前状态和目标为只读；右侧显示当前状态摘要（活动、情绪、持续时长、可能过时），只读取管理只读接口，不调用回复准备。
- **记忆管理**：按正文／标签文本、人物、类型、出处入口、事件日期筛选（无事件日期时用创建时间），按文本相关度、更新时间或保留强度排序，分页每页 30 条。全文使用已有 jieba 索引，管理浏览不调用模型、不写召回记录。已删除历史可按原正文子串查询。详情显示分数、人物、来源前后文、派生关系、修订和人工操作、真实召回／使用次数。正文编辑和删除都携带 `expected_revision`；409 时保留草稿并要求重新读取最新修订。
- **删除**：只把目标对象标为 deleted，保留它的 ID、来源和修订，不删除共享消息或其他记忆；原 ID 不会复活，也不参与普通／深度召回。后端允许编辑／删除遗忘记忆；M2 的置顶、遗忘、恢复、分数调整、彻底清除与按旧内容新建在记忆详情页提供，另有“即将删除”列表。
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

宿主 **v1 契约已冻结**，完整接入说明、15 个操作、错误与重试及状态区分见 [宿主 API v1 接入契约](docs/host-api.md)。新增可选字段和响应字段保持兼容；删除、重命名或改变已有字段类型／含义须使用新版本。完成首次设置并登录管理界面后，可查看 `/docs` 和 `/openapi.json` 中的中文说明与示例。

本轮接通 `POST /api/v1/media`：用 JSON 的 `entry_id`、`content_type`、`data_base64` 上传，可附 `understanding_text`，成功 201 返回 `id`；消息的 `media_ids` 数组按顺序引用媒体。单文件 10 MiB、整个媒体 JSON 14 MiB，超限 413；支持 PNG/JPEG/GIF/WebP、MP3/WAV/Ogg/FLAC、MP4/WebM，须匹配 MIME 与文件头。上传与接收均不等待模型。上传对象绑定入口，引用时同时检查上传入口与接收入口权限；文件按内容哈希共享。本文末尾 MD 后端阶段“尚由 AP 线接入”的说明为历史状态，以本接入节为准。

回复准备只在 `hints` 追加 `other_entries_pending`：其他授权入口中的待学习入口数、UTC 接收时间范围，不含入口标识、正文或人物。`POST /api/v1/memories/search` 可传 `entry_id` 并校验令牌范围；省略时不传递入口上下文。本分支兼容 VS 前后的检索签名，入口隐私过滤须与 VS 一起部署，详见接入契约的过渡说明。

宿主错误统一为 `{"error":{"code":"invalid_request","message":"请求格式或内容不合法","fields":[{"field":"body","message":"字段说明"}],"retry_after_seconds":null,"retry_at":null}}`。状态为 400／401／403／404／409／413／429／503；429 和 503 带 `Retry-After`、建议等待秒数及预计重试时间。状态和目标的超大请求统一返回 413，管理接口行为不变。

宿主与 Iris 在同一台电脑运行，Iris 继续只监听回环地址并校验 Host。所有 `/api/v1` 请求都需要 `Authorization: Bearer <令牌>`；管理员 Cookie 不能代替宿主令牌，宿主令牌也不能登录管理界面。连接器经独立的 AstrBot 插件接入，平台不需要直接连接 Iris。下文及其他小节的宿主请求示例均须携带这个请求头。

停服后可用离线命令创建、列出或撤销令牌（沿用服务的数据库互斥锁，服务运行时命令失败）：

```bash
uv run iris tokens create --host astrbot --prefix 'astrbot:'
uv run iris tokens create --host local-tool --entry group-a --entry private-a
uv run iris tokens create --host trusted-host --all
uv run iris tokens list
uv run iris tokens revoke <令牌ID>
```

`--data-dir`／`--db` 等全局参数放在 `tokens` 前。创建输出一份 JSON，含 `id`、`host`、`scope` 和仅此一次返回的 `token`；妥善交给宿主保存，不把终端输出重定向到日志。后续列表只显示 ID、宿主名称、范围、创建／最近使用／撤销时间。凭据由随机 256 位秘密值构成；数据库只保存独立随机盐和 SHA-256 摘要。它不是用户口令，不需要使用耗时的口令派生算法。撤销提交后，后续请求立即失效。

在线管理接口使用现有管理员会话；写入还须 JSON Content-Type 和 `X-Iris-CSRF`：

| 接口 | 请求或返回 |
| --- | --- |
| `POST /admin/api/tokens` | `{"host":"astrbot","scope":{"kind":"prefix","prefix":"astrbot:"}}`；201，一次性返回令牌 |
| `GET /admin/api/tokens` | `items` 为元数据列表，另附 `rate_limits`；不返回盐、摘要或凭据 |
| `POST /admin/api/tokens/{id}/revoke` | 正文 `{}`；重复撤销仍成功，仅首次写撤销记录 |
| `PATCH /admin/api/settings/host-tokens` | `{"rate_per_second":20,"burst":60}`；保存限流设置并记录操作 |

范围为 `{"kind":"all"}`、`{"kind":"entries","entries":["group-a","private-a"]}` 或 `{"kind":"prefix","prefix":"astrbot:"}`。列表与前缀均按入口 ID 原字符、区分大小写匹配；`%`、`_` 不解释为通配符，可以授权尚未创建的入口。接收消息、回复准备（含近期消息）和立即学习在正文解析及业务执行前检查入口权限；服务状态中的入口、批次和关联学习调用按范围筛选。无效／缺失令牌返回 401 和 `WWW-Authenticate: Bearer`，越界入口返回 403。

目标以自己的 `entry_id` 判定归属；没有 `entry_id` 的全局目标对所有令牌可见。列表计数、分页以及 prepare/search 的目标分区都先按范围过滤；去重候选与可能重复 ID 同样过滤，后台复核持久保存注入时的范围，重启后继续遵守，写回前复核修订号和范围。范围外的目标不能被宿主修改或合并，提醒在分页和标记“已取走”之前按所属目标过滤。同名宿主换用更窄范围的令牌，也不能通过旧 `host_key` 回执或合并跳转取得范围外目标。

按设计 11.4，已形成的记忆仍是跨入口共享的角色知识，prepare/search 的记忆项、排序和出处不因令牌范围而改写；当前状态与 persona 也是角色全局信息。令牌范围保护入口原始消息和目标，不代替后续可见范围工作线的记忆隐私规则。状态中的入口明细按范围筛选，模型健康、用量等全局统计保留。

每令牌默认持续 **20 次/秒、突发 60 次**，所有宿主路由共用该令牌的桶，不影响其他令牌或管理员会话。超过返回 429 和整数秒 `Retry-After`。配置保存在 `runtime_settings.host_tokens`，运行中立即读取；桶在单进程内维护，重启后补满。最近使用时间记录通过认证及限流的请求，包括随后被入口权限拒绝的请求。创建、撤销和修改限流均写入操作记录。

当前状态报告和宿主目标注入的 `host` 兼容字段仍接受校验，但来源一律取令牌绑定名称；冲突时忽略请求自报名称。目标的可选 `host_key` 按绑定宿主名称隔离；同名宿主的不同令牌共用这个命名空间。使用反馈只接受本令牌发起的召回；换令牌后需重新准备／查询。状态报告历史、目标来源及宿主操作记录保留绑定名称。

端到端评测在首次启动服务前调用同一个离线创建命令，仅在内存捕获一次性标准输出；所有 HTTP 请求携带该令牌，强制重启继续使用它。令牌不进入临时模型配置、服务日志或评测报告。本接入节覆盖前面历史实现说明中“宿主令牌仍在 M4”的旧状态；下文状态报告中的宿主名称也以令牌为准。

下面的 Python 示例依次调用接收消息、回复准备和使用反馈；请求与响应均使用 UTF-8 JSON：

```python
import os
import httpx

with httpx.Client(base_url="http://127.0.0.1:8080", timeout=10,
                  headers={"Authorization": "Bearer " + os.environ["IRIS_HOST_TOKEN"]}) as client:
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

`GET /admin/api/entries/{entry_id}/settings` 读取入口设置，`PATCH` 修改已存在入口的节奏和过滤。沿用管理员会话、JSON Content-Type、X-Iris-CSRF、Origin／Sec-Fetch-Site 及 Host 校验；无效值返回 400，不存在的入口返回 404。写入与 `entry_settings_updated` 操作记录在同一事务完成，记录前后设置。入口与学习页可以查看和修改这些设置，也可以直接调用管理接口：

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

`recall_judge` 是独立模型用途，未单独配置时继承 chat 的接口、密钥和模型，推理档位固定默认 high；chat 的暂停不暂停判断，判断的暂停也不暂停学习。可在测试配置中增加 `[recall_judge]`，或通过已有管理员接口 `PUT /admin/api/settings/models/recall_judge` 保存独立模型及档位；`enabled:false` 清除该独立配置、恢复继承。全局关闭使用 `PATCH /admin/api/settings/recall-judge` 的 `enabled:false`。此 PATCH 还支持 `concurrency`（1—8，默认 1）和 `queue_limit`（0—64，默认 8），省略字段保留现值；模型来自外部文件时模型配置仍只读。管理设置响应列出实际配置、`inherited` 和独立健康状态；设置页的模型部分可以配置、关闭和测试召回判断，运行状态页显示它的健康状态，试用页显示每次回复准备的判断结果。

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

## M3 persona（后端）

自我认知记忆保存具体经历与观点，persona 从中提炼相对稳定的自我描述。当前活动、情绪和待办分别留在状态与目标分区。首次设置仍立即生成原有模板正文，不调用模型；角色名和模板有设定依据。后续生成使用有效、涉及“我”、且说话人是“我”或立场为“设定”的记忆，按置顶、重要度和保留强度排序，材料最多 6000 估算 token。别人对我的评价本身不会入选。

候选逐句记录正文、来源、依据记忆 ID 与修订号，以及依据追溯到的不同日期数。先检查非空、依据有效、没有遗留编号和全文不超过 800 字，再由模型逐句检查依据支持、虚构、监管要求和单一日期的场景限定。少于 300 字只提示，不靠扩写凑字。上一版仅帮助保持措辞稳定，不是新的事实依据；失效依据支持的句子不能因此保留。删改管理员手写句会确定性地把变化程度提升为“大”。管理员直接编辑只做确定性检查，改动或新增的句子标为手写，原样保留的句子沿用原依据。

自 2026-10-10 起，默认“全部人工确认”：检查通过的小、中、大变化都带逐句依据和检查结果进入待确认，管理员确认前继续使用当前版。M3 persona 门槛尚未达到，因此先由管理员把关；这是发布策略调整，不代表模型质量已达标。检查不通过保留当前版并记录原因。新的候选取代旧待确认候选，管理员编辑和回滚也会取代它。版本状态为 `current`（当前）、`pending`（待确认）、`rejected`（被拒绝）、`superseded`（被取代）、`history`（历史）；来源为 `initial_setting`、`periodic`、`regenerate`、`admin_edit`、`rollback`。回滚只接受曾发布的版本，新建一个正文与逐句依据相同的当前版本，历史保持线性；若旧依据已失效，新版本仍显示待更新。

当前版本的任一依据记忆被修改、遗忘或删除，会只读计算出“待更新”，并列出失效依据，不在回复准备时重写 persona。数据层 `persona_due` 从当前版本的生成时间起算：至少五条自我记忆新增或变化，或经过至少 168 小时且有任何变化，才满足定期更新条件。确认候选不重置生成时间；管理员编辑与回滚从新版本创建时刻起算。梦境整理的定期调用由整理线接入。

以下管理接口均需管理员会话，写请求还需 JSON 和会话绑定的 `X-Iris-CSRF`，沿用 Host、Origin／Sec-Fetch-Site 检查。版本 ID 是全局递增整数；写请求中的 `expected_version` 必须是当前版本 ID，不能传候选 ID。版本冲突或候选依据已变化返回 409，刷新后由管理员决定下一步；不存在返回 404，参数或正文检查失败返回 400。

| 方法与路径 | 请求／结果 |
| --- | --- |
| `GET /admin/api/persona` | `current`、`pending` 摘要，`needs_update`、`stale_basis_count`、`stale_basis`，以及 `latest_attempt`；没有对象时为 null |
| `GET /admin/api/persona/versions` | 可按 `status`、`source` 筛选；列表含正文、变化程度 `small/medium/large`、`generated_at`、`published_at`、基准版本与回滚来源 |
| `GET /admin/api/persona/versions/{id}` | 另含 `sentences`、`checks`、`settings`、`rejection_reasons`；每句有 `text`、`origin`、`admin_written`、`basis`、`dates/date_count`；依据含记录的修订、该修订正文 `content_at_revision` 和当前记忆 `memory` |
| `GET /admin/api/persona/diff` | 必填 `before_version`、`after_version`；逐句 `insert/delete/replace/basis_changed` 差异，位置从 1 开始 |
| `GET /admin/api/persona/self-memories` | 有效自我记忆，按置顶、重要度、保留强度排序；便于与逐句依据对照 |
| `PUT /admin/api/persona` | `{expected_version, content}`；直接编辑并发布，返回版本详情，不调用模型 |
| `POST /admin/api/persona/versions/{id}/confirm` | `{expected_version}`；确认待确认候选，返回版本详情 |
| `POST /admin/api/persona/versions/{id}/reject` | `{expected_version, reason?}`；拒绝待确认候选，原因最多 1000 字 |
| `POST /admin/api/persona/versions/{id}/rollback` | `{expected_version}`；按旧版本新建并发布，返回新版本详情 |
| `POST /admin/api/persona/regenerate` | `{expected_version}`；立即返回 202 `{accepted: true, attempt: {...}}`，`Location` 指向任务查询地址 |
| `GET /admin/api/persona/attempts`、`/attempts/{id}` | 任务列表／详情；不返回原始模型输出 |
| `PATCH /admin/api/settings/persona` | 局部修改 `goal`、`rules`、`publish_mode`，返回完整设置快照；`GET /admin/api/settings` 的 `persona` 字段读取当前设置 |

版本、自我记忆和任务列表均返回 `{items, total, limit, offset}`；`limit` 默认 30、范围 1—100，`offset` 默认 0、最大 1000000。手写标记不是模型支持结论；版本详情的 `checks.deterministic`、`checks.model` 和 `rejection_reasons` 应分开展示。失效依据按记忆 ID 去重，包含记录的修订、当前修订和 `modified/forgotten/deleted/no_longer_self_memory` 原因；彻底清除或不存在的记忆投影为 null。

重新生成使用独立的单线程后台执行器。接受请求时持久化任务、基准版本与设置快照；同一时刻已有 `queued/running` 任务就返回 409 `persona_generation_in_progress`，不排第二个任务。页面按 `Location` 轮询：`state` 从 `queued` 到 `running`，`stage` 区分 `queued/generating/checking/finished`；终态是 `current/pending/rejected/skipped/conflict/failed`。终态可能没有 `version_id`，须展示 `reason`（如 `daily_token_limit`、`no_self_evidence`、`evidence_changed`、`interrupted`），不要把 202 当成发布成功。生成期间其他编辑仍可进行，发布前重新核对版本及依据，冲突不覆盖新版本。正常关闭等待已接受任务；进程异常中断后标记失败，不自动重跑。

生成目标默认“维持稳定的发言风格，并充分认识自我”，监管要求沿用设计 13.3 全文。`goal` 最多 4000 字、`rules` 最多 16000 字，去首尾空白后不可为空，至少提交一个字段。发布方式 `all_manual` 为“全部人工确认”（默认），设置页仍可选择 `small_medium_auto`（小或中自动发布）或 `all_auto`（全部自动）；任何方式均不能跳过候选检查。保存设置记录操作者和改动字段名，不记录目标／规则全文。新设置仅影响之后接受的生成任务，不重新检查或自动发布已有候选；学习和整理不能改这些设置。

升级迁移 020 只调整未被管理员选择过的旧默认：有效值为 `small_medium_auto`，且 `admin_operations` 没有 `actor=admin`、`action=persona_settings_saved` 记录时，改为 `all_manual`。任意一次 persona 设置保存都算，包括只改目标／规则、或显式保存旧默认；这些数据库和其他已选发布方式保持原值。缺失设置行先按旧隐式默认补齐，再作同样判断。当前版、待确认候选和已接受任务的快照均不改写；首次设置模板仍立即创建。

待确认数量沿用 `GET /admin/api/persona/versions?status=pending` 的 `total`，取值为 0 或 1；新的待确认候选取代旧候选，确认或拒绝后归零。现有界面继续显示待确认候选，不新增宿主通知。

persona 评测的发布方式独立于产品默认。语料未指定时，评测在隔离数据库中显式写入 `small_medium_auto`，保持冻结 persona_v1 的预期；可用 `persona_publish_mode` 指定模式（JSONL 的时间线字段，或 JSON 的顶层默认／时间线覆盖）。实际模式进入规范化输入指纹、候选设置快照和外部判分材料，不借用产品默认，也不修改冻结语料。

宿主 `prepare` 的 persona 分区如下，版本未初始化时为 `version: null`、空正文、`generated_at: null`、`needs_update: false`、计数 0。其他分区保持独立。

```json
{"persona": {"version": 3, "content": "……", "generated_at": "2026-10-10T08:00:00+00:00", "needs_update": true, "stale_basis_count": 1}}
```

## M3 当前状态

当前状态是一份由宿主报告的主要活动，所有入口共享。它供当下回复使用，不进入学习材料、不逐条写成记忆、不更新 persona，也不自动建立目标。值得长期记住的结果仍由宿主另发消息、场景事件或行动结果，经原学习流程处理。逐帧画面、技能冷却等控制信息留在宿主中。管理界面的“状态与目标”页显示当前状态与分页报告历史，“可能过时”的分钟数在设置页修改；管理员不能编辑状态。

| 方法与路径 | 请求／行为 |
| --- | --- |
| `GET /api/v1/state` | 当前状态；无活动时返回 `{}` |
| `PUT /api/v1/state` | 必填 `activity`；可选 `details`、`mood`、`started_at`、`host`、`entry_id`，开始／更换活动 |
| `PATCH /api/v1/state` | 可选 `details`、`mood`、`host`、`entry_id`；`{}` 为心跳。无活动时返回 400，须先 PUT |
| `DELETE /api/v1/state` | 结束活动，返回 `{}`；可不带正文，或携带 `host`／`entry_id`。重复结束仍保留报告 |
| `GET /admin/api/state` | 管理员只读当前状态 |
| `GET /admin/api/state/reports` | 管理员只读报告历史，`limit` 默认 30、1—100；`offset` 默认 0、0—1000000；按报告 ID 倒序 |
| `PATCH /admin/api/settings/state` | `{"stale_after_minutes":30}`；整数 1—525600，省略保留原值；写入 `state_settings_saved` 操作记录 |

PUT 的活动经去除首尾空白后，按区分大小写的完整字符串判断是否相同；不合并近义词、内部空白、标点或 Unicode 写法。相同活动按报告／心跳处理，保留开始时间及未报告的细节、情绪。更换活动时清空旧细节，保留情绪及其原更新时间，除非本次 PUT 显式提供 mood（null 清除）；开始时间取本次首次报告时间。DELETE 结束活动时仍清空全部状态。宿主在 PUT 显式给出带时区的 ISO `started_at` 时采用它，包括校正同一活动的开始时间；不接受 null 或晚于本次报告的时间。PATCH 不接受活动或开始时间。

`details` 是最多 32 项的键值对：键长 1—64 字符、不能全空白，值为最多 1000 字符的字符串、有限数字或布尔值；null 删除该细节。按键部分更新，`{}` 保留现有细节；合并后仍受 32 项及 32KB UTF-8 的限制。`mood` 最多 200 字符，null 清空。被明确报告的细节／情绪即使值相同也刷新各自时间，未报告的字段保持原时间。`activity` 长 1—200 字符；`host` 为可选来源名称（1—100 字符），`entry_id` 为可选来源标识（1—200 字符），可先于消息入口创建。每次报告的来源独立保存，省略／null 表示未知，不沿用上一个宿主。此来源名称不是身份认证；M4 起宿主名称以令牌为准（见「宿主接入」）。

状态请求正文上限 32768 UTF-8 字节，必须使用 JSON；字段非法、未知字段、超限或无活动时 PATCH 均返回 400，`error.fields` 给出字段说明。Host 校验沿用现有规则。管理读取要求管理员会话；设置写入还要求 JSON Content-Type、CSRF、Origin／Sec-Fetch-Site 校验。管理员没有编辑或结束活动的管理接口。

例如宿主报告：

```json
{"activity":"探索海岛","details":{"scene":"海岸","progress":2},"mood":"紧张","host":"game","entry_id":"game-a"}
```

当前状态的响应形状如下（时间按角色时区输出带偏移的 ISO 格式；未设置时区时使用 Asia/Shanghai）：

```json
{
  "activity": "探索海岛",
  "activity_updated_at": "2026-10-09T12:00:00+08:00",
  "details": {
    "scene": {"value": "海岸", "updated_at": "2026-10-09T12:00:00+08:00"},
    "progress": {"value": 2, "updated_at": "2026-10-09T12:00:00+08:00"}
  },
  "mood": "紧张",
  "mood_updated_at": "2026-10-09T12:00:00+08:00",
  "started_at": "2026-10-09T12:00:00+08:00",
  "start_time_basis": "first_report",
  "duration_seconds": 0.0,
  "updated_at": "2026-10-09T12:00:00+08:00",
  "possibly_stale": false,
  "stale_after_minutes": 30,
  "host": "game",
  "entry_id": "game-a"
}
```

`start_time_basis=host` 表示“宿主提供”，`first_report` 表示“自首次报告起”。持续秒数在读取时按真实时间差计算（跨时区／夏令时照常），系统时钟回退时最低为 0。`activity_updated_at` 是最近一次 PUT 报告活动的时间，`updated_at` 是最近一次任何报告的时间。情绪从未报告时 `mood` 和 `mood_updated_at` 都是 null，明确清空情绪仍记录时间。

严格超过阈值未更新时 `possibly_stale=true`，恰好 30 分钟仍为 false；只标注，不自动结束活动。设置修改即时影响后续读取。prepare 的 `state` 总是包含当前状态，召回判断完成后再读取，以免返回判断期间已经结束的活动；search 仅在 `include_state=true` 时提供此分区。它们都不改变记忆 ID、顺序、reason 或记忆预算。

每次有效 PUT／PATCH／DELETE 与当前值在同一事务保存报告，包括没有值变化的心跳。历史返回 `id/host/entry_id/reported_at/method/action/reported/changes`；`action` 为 start、replace、update、heartbeat 或 end，`reported` 保存本次明确报告的字段（来源单列），`changes` 保存实际值变化的 before／after，细节按键列出。并发宿主按提交序号先后写入，最新报告的同一字段覆盖旧值；矛盾的旧报告仍保留在历史中，不形成确认事实。结束后历史仍可查询，读取历史不调用模型或增加召回次数。

## M3 目标、询问与提醒（后端）

目标属于角色，所有入口共享，入口只记录产生位置；它是待办意图，不是当前活动、记忆或 persona。学习仍可同时保存承诺记忆与内部目标，目标本身不加入学习材料，也不会因聊天中的含糊说法自动完成。询问为 `kind=question`，没有截止时间或提醒，宿主可直接注入，并决定何时问。

| 方法与路径 | 内容 |
| --- | --- |
| `GET /api/v1/goals` | `state`、`kind`、`overdue`、`due_soon` 筛选，`limit`（1—100，默认 30）、`offset` 分页 |
| `POST /api/v1/goals` | `content` 必填；可选 `kind`（normal／question）、`deadline`、`reminder_minutes`、`people`（主体 ID 数组）、`entry_id`、`host_key`；返回 201 和 `{goal, submitted_id, dedup}` |
| `PATCH /api/v1/goals/{id}` | `state=completed/abandoned`、`deadline`、`reminder_minutes`；可带 `expected_revision`，不匹配返回 409 |
| `GET /api/v1/notifications` | `after`（默认 0）、`limit` 拉取；返回 `{items,next_cursor,has_more}` |

宿主和管理接口的目标写请求最多 32KB UTF-8，正文 1—4000 字符，涉及的人最多 100 个，入口／宿主键最多 200 字符；未知字段和非法值返回 400 并标明字段。截止时间接受带时区的 ISO 时间或 `YYYY-MM-DD`；日期解释为角色时区当天 23:59:59，返回按角色时区转换的 ISO 时间。提前量为 0—525600 分钟，`null` 使用默认值 60。`people` 排除我和场景，同名但不同主体不会合并；已确认的人物合并按当前主体解析。

目标返回 `id/content/kind/origin/state/deadline/reminder_minutes/effective_reminder_minutes/people/entry_id/host_key/revision`，以及创建／更新时间、`closed_at/closed_by`、`merged_into`、`overdue/due_soon` 和 `possible_duplicate/possible_duplicate_ids`。`due_soon` 表示已到临近时刻，过期时也为 true。学习旧输出中无法解析的期限保留原文，并标记 `deadline_unresolved=true`，不臆测时间或生成提醒；宿主和管理员可修改成有效期限。

注入先保存原始目标，再跨入口在同 kind 的未结束目标中去重。`decide_dedup(candidates, proposed)` 返回 `{status,target_id}`：`created`、`merged`、`possible_duplicate` 或模型判断待复核的 `pending`。关闭目标去重判断时沿用确定性规则：仅正文压缩连续空白、忽略大小写和句末标点后完全一致，且涉及人集合相同、期限兼容时自动合并。内部标点和英文词界保留，复用 M2 的中文／阿拉伯数字与否定序列规则；一个期限为空兼容，两个明确期限不同不兼容。其他规范化文本相似度至少 0.88 的兼容项只标记可能重复。管理员新建（`origin=admin`）也执行同一判断，但 `merged` 结果降为 `possible_duplicate`，保留两条目标，等待管理员明确合并或驳回；宿主和学习产生的新目标仍按上述规则自动合并。这不是目标去重模型质量门槛的结论。

合并保留较早目标，原目标及其依据保留可追溯，占位的 `merged_into` 指向保留项；来源消息、同证据角色记忆与未取提醒归入保留项。明确截止时间补入空值；两个明确提前量不同则采用较早提醒（较大分钟数），原值仍在被合并项中。合并后的同阶段待取提醒只留一条，其余保留为取消记录。相同 `host_key` 的重试返回首次创建的完整回执快照，即使后来目标已修改；读取最新状态用列表。修改已合并 ID 返回 409 `goal_merged` 和 `canonical_id`，需要明确选择保留项再提交。

有期限的普通目标在临近、到期时发布提醒；创建／修改时已经错过提醒时刻则立即发布一条，未来的到期提醒仍保留。服务恢复时同一目标错过的阶段只发布一条即时提醒，不逐条补发。提前量为 0 时，临近和到期同刻，合为一条。过期后的周期提醒按角色时区每天最多一次，内容请宿主选择放弃或修改期限；停机多日只生成当前一天的一条。系统不自动失败、放弃或顺延。

通知的 `id` 是实际发布时分配的单调游标，未来计划不提前占用游标。目标提醒每条有 `kind=goal_reminder`、`reminder_kind=soon/due/overdue/immediate`、`goal_id`、`content`、发布时的期限 `deadline_at`、计划／发布时间、`status=pending/taken/cancelled` 及取走／取消时间。取走仅表示宿主读到；相同游标可重放已取记录，方便恢复丢失的 HTTP 响应，宿主按通知 ID 去重。取走不等于送达，也不完成目标。修改期限／提前量或完成／放弃会更新未来计划、取消未取的目标提醒；已取记录和去重结果通知保留。默认提前量变动重排使用默认值的未来目标，保留已过期的待取提醒。

prepare 与 `search(include_goals=true)` 返回最多 10 个未结束目标／询问：临近和过期优先，其次本入口、涉及当前参与者，再按期限与创建时间。此分区不改变记忆选取。管理员接口沿用会话、CSRF 和操作记录：`GET/POST /admin/api/goals`，`GET/PATCH /admin/api/goals/{id}`；管理修改须带 `expected_revision`，可编辑正文。列表另支持 `entry_id/possible_duplicate/deadline_from/deadline_to`，日期筛选包含整天。详情含 `sources`、`promise_memories`（同证据角色记忆及当时修订，并非新增语义承诺分类）、`merged_goals`、提醒计划和通知历史。

可能重复关系通过 `POST /admin/api/goals/{id}/duplicates/{other_id}/merge` 或 `/dismiss` 处理，正文须含两方 `expected_revision/other_revision`；合并仍守住类型、人物、期限、数字与否定边界。`GET /admin/api/notifications` 按 `status/goal_id` 分页，只读不取走。`PATCH /admin/api/settings/goals` 设置 `default_reminder_minutes` 和 `overdue_reminders`（默认 true），写入操作记录。页面接入留到后续界面阶段。

旧状态映射：`done/completed` → completed，`cancelled/canceled/abandoned` → abandoned，其余（包括 in_progress、pending、failed 和未知值）→ open；过期／失败不推导放弃。未知旧来源映射 internal。无法确定的历史完成时间和操作者保留 null。

初版验证（2026-10-09，head `5eec156`，基线 `be3a98a`）：Python 3.13／3.12 各 1244 项通过，`uv build` 通过。未修改的 `compare_learning_requests.py` worker 由仓库外脚本补入公开 v5，116 案例、272 批，system/user 字符串、顺序、purpose 与 max_tokens 均逐字一致；候选库另有当前状态、宿主目标和询问，仍无请求差异。原脚本不输出目标，另对 14 个公开目标案例手写并冻结固定输出、在全部 116 案例重放（102 案例无目标输出）：两边请求和输出仍一致，未合并目标 14→14、依据 19→19；物理行 14→19，差异仅 L009 1→2、M211 1→2、M218 1→3、N312 1→2，均为重申承诺保存后指向较早目标的占位，正文／kind／截止时刻和全部来源保留。该重放只测写入，不是模型质量评测。

初版召回验证用七份公开语料独立入库，固定各自时钟和相同真实向量缓存，`judge=false`，分别比较默认混合检索与全文降级的有序 `(memory_id, reason)`：228 查询 × 2 路径共 456 组，零差异。审查修订只调整管理员创建的去重结果和目标分区读取，不改记忆选取、判断、排序或 reason，本轮未重跑召回语料。

审查修订验证：Python 3.13／隔离 3.12 各 1257 项通过（新增 13 项），`uv build` 通过。对 main `a9af8ec` 重跑五份公开学习语料，116 案例、272 批逐字一致；候选仍含状态、宿主目标和询问，完整请求 SHA-256 与初版相同。迁移保持 012。

性能先作分段归因，再在没有其他模型评测／无头浏览器测试的窗口测量。初版 5 万记忆 prepare P95 为 319.7／413.9ms，当时系统负载由 4.7 升至 8.6；这些跨次数字不能作为目标数量的因果对照。本轮观测到并行 Node／无头 Chrome 测试及 persona 评测，受干扰的测量已中止或仅作诊断，原始记录全部保留。分段计时显示较大的尖峰在未修改的 `_rank` 中，墙钟明显高于进程 CPU 时间；旧目标分区自身的主要成本是读取全部目标并构造 Python 对象，其次是逐行排序。不能仅凭旧日志还原当时每项等待的来源。

正式配对对照使用两份预置为 0／1000 个目标的合成库，同一进程共享相同的只读向量索引、默认召回配置、固定时钟及原基准的假模型；交替旧／新实现及目标数量，每组预热 5 次后测 60 次。测量期间不修改目标数量，关闭两边调度器以隔离分区成本，在 prepare 内直接计时。SQL 排序并 LIMIT 后只对前 10 个投影，主体合并链、旧日期和微秒边界有回归测试。下表单位为 ms；增量为逐对目标分区耗时差的 P95：

| 记忆数／查询 | 旧分区 P95（0／1000 目标） | 新分区 P95（0／1000 目标） | 新分区增量 P95 |
| --- | ---: | ---: | ---: |
| 5 千／不点名 | 0.057／4.210 | 0.247／0.990 | 0.762 |
| 5 千／点名 | 0.047／4.238 | 0.250／0.749 | 0.541 |
| 5 万／不点名 | 0.087／5.070 | 0.327／1.125 | 0.802 |
| 5 万／点名 | 0.070／6.563 | 0.587／1.199 | 0.891 |

| 记忆数／查询 | 旧 prepare P95（0／1000 目标） | 新 prepare P95（0／1000 目标） | 新 prepare P95 差值 |
| --- | ---: | ---: | ---: |
| 5 千／不点名 | 25.0／27.3 | 25.1／26.9 | +1.8 |
| 5 千／点名 | 27.2／33.0 | 28.2／28.7 | +0.5 |
| 5 万／不点名 | 156.9／208.8 | 192.3／137.1 | -55.2 |
| 5 万／点名 | 226.2／735.6 | 570.0／585.8 | +15.8 |

目标分区增量 P95 最大 0.891ms，完整 prepare 的 P95 差值最大 15.8ms，均在 30ms 内。双库配对中的整体墙钟仍有排序／等待波动，包含负差值，不能把两个独立 P95 的差当作确定的 CPU 节省；标准 R10 的绝对时延另用原单库基准测量。

随后在无并行评测／浏览器测试的窗口，运行未修改的 `uv run python evals/benchmark_retrieval.py --default-config`（每库 1000 个未结束目标、10 条待取提醒；保留原调度器，预热 5 次、测量 60 次），四组全部 ≤500ms：

| 记忆数／查询 | prepare P95（ms） | HTTP prepare P95（ms） |
| --- | ---: | ---: |
| 5 千／不点名 | 20.0 | 32.7 |
| 5 千／点名 | 31.7 | 35.1 |
| 5 万／不点名 | 140.1 | 139.8 |
| 5 万／点名 | 154.4 | 156.8 |

完整材料、脚本、日志及逐案例差异保存在仓库外 `iris-eval-artifacts/m3-goals-20261009/` 和 `iris-eval-artifacts/m3-goals-review-20261009/`；模型去重与方法选择留待下一步。

### 来源前后文、修订快照与依据复核（GO 第五步）

管理详情的每个 `sources` 项保留原来的消息字段，增加 `message`、`context`、`missing`、`notice`。前后文沿用记忆详情：同一入口按消息入库 ID 取前两条、来源本条、后两条，不跨入口拼接；包含发言人名和原始消息类型，不回灌为新经历、不增加召回或使用次数。来源消息不可用时保留 `message_id`，返回 `message=null`、`context=[]`、`missing=true` 和“来源消息已清理或不可用”；当前清理逻辑仍保护被目标引用的消息。

| 管理接口 | 内容 |
| --- | --- |
| `GET /admin/api/goals/{id}/sources` | 来源消息及各自前后文，`limit`／`offset` 分页；详情也提供同样的前后文 |
| `GET /admin/api/goals/{id}/revisions` | 按快照 ID 倒序分页，返回 `revision_before/revision_after/action/before/after/actor/reason/created_at` |
| `DELETE /admin/api/goals/{id}/basis-annotations/{annotation_id}` | 清除该目标的一条活动标注；JSON 须带 `expected_revision`，可带 `reason`；沿用管理员会话、CSRF、Host／Origin 校验和操作记录 |

分页沿用 `limit=1—100`（默认 30）、`offset≥0`，未知字段、非法字段返回 400，目标／标注不存在返回 404，修订不匹配或标注已被后续复核替代返回 409。对已合并目标的写入仍返回 `goal_merged`，需明确选择保留目标。管理编辑、合并、驳回和清除标注可提供 1—500 字符的非空白 `reason`；省略时记录对应操作的固定原因，不允许只提交原因而不修改目标。

迁移 018 新增 `goal_revisions`、`goal_basis_annotations`。创建记录初始值；正文、期限、提前量、状态、合并／重定向、可能重复及其驳回、依据标注的变化都在同一事务保存前后值、操作者、时间与原因。快照只存发生变化的领域字段；关系按 ID／状态记录，原始消息、记忆正文、未变的目标正文、宿主回执和模型任务信息不重复复制。修改操作记录引用快照 ID，不重复写正文。无实质变化和失败／冲突不产生快照；合并也记录被合并项、被改写的旧占位和受影响的重复关系，相关修订号随之更新。新目标的历史状态为 `complete`；迁移前已有目标返回 `history_status=pre_migration_no_snapshot`、`missing_through_revision` 和“迁移前无快照”，不伪造旧历史，之后的修改正常记入。详情的 `revision_history` 同样给出这项提示。

目标列表、详情、prepare 和 `search(include_goals=true)` 新增 `basis_needs_review` 及 `basis_annotations`，不改排序或记忆返回。每条标注指出 `memory_id`、当时的 `basis_revision`、观察到的修订／生命周期／彻底清除／合并去向、`changes`、时间和“依据可能不成立”说明。变化代码为 `revision_changed/forgotten/deleted/purged/missing`；不推断承诺是否取消或目标是否完成。

确定性复核只检查未结束、未合并的内部目标（含询问），也检查吸收了内部目标的保留项，避免跨来源合并后丢失内部依据。比较 `goal_memories` 的原始依据修订与当前记忆，不读取记忆修订历史、不调用模型、不改记忆，不完成／放弃目标、不改正文／期限／提前量／提醒。过期仍按当前时间计算，不写成新状态。

记忆仅从遗忘恢复、且仍为原依据修订时，标注自动结束；修订不同则保留“修订已变化”，不会把恢复当成重新确认承诺。当前观察变化时旧标注变为 `superseded`，已恢复或依据关联移除时为 `resolved`。管理员清除记为 `cleared`：同一依据修订和同一观察状态不重复提醒，新的修订、生命周期或清除状态变化再复核；不改写原依据修订。合并复制依据、标注和清除记录，原记录保留追溯；相同依据事件已有目标记录优先。

给每日维护的接口均在 `iris.goals`，本步提供函数，由整理线接入调度：

```python
# 已持有写事务：检查单个目标，修订不符抛 GoalConflict。
review_goal_basis(conn, current, goal_id=goal_id, expected_revision=revision)
# 也支持省略 goal_id，在调用者现有事务内复核全部符合条件的目标。

# 推荐每日维护使用：一次有界扫描，逐目标一个短事务。
page = review_goal_basis_items(store, current, after_id=0, limit=100)
# 用 page["next_after_id"] 续扫，直到 page["has_more"] 为 False。
```

`current` 为带时区的 `datetime`，可注入固定时钟。返回 `checked`、`changed_goal_ids`、`annotation_ids`、`ended_annotation_ids`（含恢复结束或被新观察替代的旧标注）；逐项版本另返回 `conflicts/next_after_id/has_more`，`limit` 为 1—1000。逐项版本写入前核对扫描时的目标修订，冲突跳过、留下一次从 ID 0 开始的完整复核；记忆的修订和状态只在该项写事务内读取，不从事务外的旧观察写回。可中断续扫，重复运行无重复快照或标注。本步不修改或调用每日维护、模型整理及其调度入口。

本步基线为 main `5a52b57d7c84b306518968cdc8f0335c9ebe3a8c`；35 项新增确定性／接口测试。学习逐批对照覆盖 v1—v5 的 116 段、272 批（156 批带已有记忆），与 main 逐字一致，请求 SHA-256 均为 `9988666d3634cdde81ded0d1a540cf92cbda5d30b65bf8ae5a85301274861092`。召回在两个独立进程中给七份公开语料同样的固定 2048 维 float32 假向量，关闭召回判断，混合召回／纯全文降级共 228×2=456 次查询的记忆 ID、顺序和 reason 差异为 0；候选库额外含依据标注、快照、目标、询问、状态和待取提醒。这是返回回归检查，不是真实模型质量重评。

Python 3.13 与隔离的 3.12 按顺序运行，各通过 1635 个测试；`uv build` 成功。R10 沿用未修改的 `benchmark_retrieval.py --default-config`，main 与本分支串行运行相同的合成数据库：每库额外有 1000 个未结束目标、1000 条活动依据标注和 20 条待取提醒。每组预热 5 次、测量 60 次，使用固定假判断；四组 prepare 及 HTTP P95 均低于 500 ms。数值为本次运行结果，不把小幅计时波动解释为加速。

| 记忆数／查询 | main prepare P95 | 本分支 prepare P95 | main HTTP P95 | 本分支 HTTP P95 |
| --- | ---: | ---: | ---: | ---: |
| 5000／不点名 | 28.8 ms | 33.0 ms | 27.1 ms | 27.1 ms |
| 5000／点名 | 32.7 ms | 25.3 ms | 27.1 ms | 41.0 ms |
| 50000／不点名 | 130.3 ms | 123.3 ms | 145.7 ms | 146.8 ms |
| 50000／点名 | 129.0 ms | 125.1 ms | 140.6 ms | 129.2 ms |

完整材料、离线脚本、首次失败和最终日志保存在仓库外 `/Users/cassia/Local/Code/iris-eval-artifacts/m3-goal-review-20261010/`，不读取模型配置，不调用真实模型，不访问隐藏集。

## M3 目标去重判断（GO 第三、四步）

目标去重默认开启 C2：沿用 C 的整批候选判断，另为数字／否定差异的相近配对标记可能重复。C2 在 `82a053c26022771b0d34983b01e70bd4c7452491` 单独冻结；公开 goal_dedup_v1 两轮均为 0/13 误合并、28/28 应合并识别，相比 C 的 0/14、14/28 提高识别且未增加误合并，按 GO 第四步规则采用。不应合并中标可能重复由 6/39 增至 22/39，增加了人工复核量；没有放宽合并。两轮各调用 38 次，均无降级。详见 [C2 冻结规则](evals/goal_dedup_probe/C2_METHOD.md) 和 [C2 探测报告](evals/goal_dedup_probe/C2_RESULTS.md)。A／B／C 及原提示词在 `9cacbd02d38f0200dd757316c7b8c8c101dde113` 冻结，原方法选择与 C 的 5 秒预算验证保留在 [GO 第三步报告](evals/goal_dedup_probe/RESULTS.md)。

B／C 只判断同 kind、未结束且未合并的候选。明确的人物集合、期限、数字或否定序列冲突先排除；人物信息缺失按未知处理。正文相似度至少 0.25 或有共同涉及人即召回，最多 8 个；模型材料只有正文及时间、涉及人、期限、入口类型和来源摘录。即使正文完全相同也判断来源是否指同一事件。只有唯一 `same` 且其余没有 `same/uncertain` 才自动合并；有歧义或候选超限则保留并标可能重复。管理员创建始终不自动合并。只有一方缺少涉及人时，确认合并保留已知人物；明确不同的人物仍拒绝合并。

C2 将被数字／否定序列排除的相近配对只标为可能重复：同 kind、未结束且未合并、涉及人兼容、非空期限一致，并满足规整正文相似度 ≥0.25，或同一非空入口且该入口的来源消息有共同的非 self／scene 发言人（按已确认身份取规范 ID）。按相似度、创建时间、ID 取最多 8 个，不占模型候选名额，不送模型，不放宽自动或手动合并限制。只有这些配对时立即返回 possible_duplicate；还有模型候选时标记立即可见，即使模型另有唯一 same，也先保留为可能重复供人工复核。管理员与宿主使用现有 possible_duplicate_ids 查看，可驳回；若要合并不兼容内容，须先明确编辑。显式关闭判断仍沿用 A。

宿主 POST 先提交目标，再在请求内判断。超时、排队满、限流、暂停或在途修订变化返回 `dedup.status=pending`，由调度器补做。学习仍只在原事务内执行确定性候选检索、可能重复标记和入队，提交后才调用模型判断，不改学习材料／校验／提示词。宿主键重试保留首次响应，后台结果不改写回执。补做结束写操作记录，并发布 `kind=goal_dedup_result` 通知；宿主仍按通知发布游标拉取，查看最新目标列表中的合并与可能重复状态。

迁移 014 新增 `goal_dedup_jobs`，保存任务状态、两边修订号、尝试次数、下次执行时间及可过期租约。调度器只查询到期任务，不重新去重历史目标；失效租约可在重启后恢复。模型调用在事务外，写回核对租约、本目标和全部候选修订，并再次核对人物／来源投影；失效结果不落地。完成或放弃取消未完成复核。管理目标详情增加只读 `dedup_review`，含状态、方法、输入修订、尝试次数、结果、下次执行和更新时间，不暴露租约凭据。

`goal_dedup_judge` 用途默认继承对话端点及 high 档位，健康状态、并发和有界排队独立于学习及召回判断。前两次 429 按 Retry-After 或 2／4 秒退避，第三次连续可重试错误暂停并探测；每日 token 上限同样阻止判断。`PATCH /admin/api/settings/goal-dedup-judge` 沿用会话、CSRF 和操作记录，设置 `enabled`、`budget_seconds`（大于 0 且至多 10 秒）、`concurrency`（1—8）和 `queue_limit`（0—64）。默认预算按有效探测较高 P95 加 20% 向上取整为 5 秒，并发 1、排队 8；已保存的显式设置保留。此前 C 在 5 秒产品预算下的有效双轮宿主同步 P50/P95 为 1.05/3.44 秒、1.19/2.83 秒；该阶段含作废尝试的宿主降级为 10/459，见 GO 第三步报告。本轮 C2 按冻结的 60 秒测量预算运行，宿主 P50/P95 为 1.10/2.91 秒、1.18/2.58 秒；这不改变产品的 5 秒时限，也不代表以后不会超时。显式关闭 enabled 使用 A；模型不可用时保留待复核，不自动切换到 A 合并。独立模型端点的保存／测试／重试沿用 `/admin/api/settings/models/goal_dedup_judge` 系列接口。

探测脚本支持仓库外 `--corpus` 和 `--out`；`--fake` 使用本地传输，不读取模型配置、不发送网络请求，结果明确标为模拟并被选型函数拒绝。使用方法与本轮验证汇总见 [探测说明](evals/goal_dedup_probe/README.md)。

## M2 人物与身份联系（后端）

人物管理接口沿用管理员会话、CSRF、JSON Content-Type 和本机 Host 校验；人物页提供列表、详情、否认、确认合并和别名管理，有待确认联系时导航和试用页会提示。

| 接口 | 输入与行为 |
| --- | --- |
| `GET /admin/api/people` | `text` 搜索名字／有效别名；`pending_only=true` 只列有待确认联系的人；`include_merged=true` 包含占位。支持 `limit`、`offset`，返回 `pending_links`、别名、记忆数与主体 `revision`。默认不列 self／scene。 |
| `GET /admin/api/people/{id}` | 平台身份、别名、记忆数及按生命周期分组、`memories_url`、same_as 联系的相信程度和证据消息、扮演关系。合并占位带 `merged_into`、`canonical_id`。 |
| `POST /admin/api/people/links/{id}/deny` | `{ "expected_revision": 1 }`，使用联系修订号；保留该联系为 denied，后续学习不能恢复或反向重建。 |
| `POST /admin/api/people/links/{id}/confirm` | `{ "target_id": "A", "expected_revision": 1, "expected_source_revision": 2, "expected_target_revision": 2 }`；A 必须为联系一方，另一方 B 合并到 A。确认直接合并，没有暂存的确认态。 |
| `POST /admin/api/people/{id}/aliases` | `{ "alias": "阿林", "expected_revision": 2 }`，使用主体修订号；返回稳定别名 ID。 |
| `DELETE /admin/api/people/{id}/aliases/{alias_id}` | `{ "expected_revision": 3 }`；移除别名及折叠证据，并阻止学习自动加回这个别名。管理员显式添加可以恢复。 |

成功操作与变更在同一写事务保存到 `/admin/api/operations`。修订过期、重复确认或操作已合并主体返回 409；对象不存在为 404，输入错误为 400。合并拒绝 self／scene、扮演者与其角色、从属人物与其祖先之间的合并。

B 的账号、有效别名、记忆涉及人和说话人转到 A，B 的名字成为 A 的别名（与 A 同名时不重复添加）；从属于 B 的人物改从属于 A。记忆正文、修订号、相信程度、保留强度和向量不变，也不因身份合并去重现存记忆。B 和历史消息作者保留用于追溯，新消息的 B 账号解析到 A。合并记录保存平台账号键、记忆／别名／联系／从属主体 ID，以及折叠目标 ID，不保存记忆或消息正文。

同一对主体的联系折叠时，否认优先；状态相同时保留目标方已有联系的相信程度，不取最大分。扮演保持方向，折叠时保留每个原场景。折叠的旧联系和重复别名保留为证据档案，仍保护证据消息；人物页读取有效联系时汇总这些证据。手动删掉的别名在合并后仍不会自动回来；确认身份本身明确授权把 B 的显示名添加为 A 的别名。

prepare 和 search 为每条最终返回的记忆追加 `subject_annotations`，涉及者按 `about` 和 `speaker_subject_id` 判断：

```json
{
  "possible_same_as": [
    {"link_id": 1, "belief": 70, "subjects": [{"id": "A", "name": "小林"}, {"id": "B", "name": "林同学"}]}
  ],
  "roleplay": [
    {"link_id": 2, "actor": {"id": "A", "name": "小林"}, "character": {"id": "C", "name": "船长"}, "worlds": ["海岛游戏"], "belief": 90, "fictional": true}
  ]
}
```

场景未知时 `worlds` 中保留 null，不假称现实。被否认的联系不出现在标注中；标注不返回证据原文，也不将扮演者与角色的经历相互归属。标注在名额、记忆 token 预算及召回判断完成后追加，因此不占用原来的记忆选取预算，不改变顺序、reason、选取名额或判断请求。宿主应将这些附加元数据计入自身的完整上下文预算。学习选材不增加这些字段。

记忆管理列表支持可选布尔 `pinned`：省略为全部，`true` 为已置顶，`false` 为未置顶；分页与 total 使用相同筛选。记忆列表页的置顶筛选使用这个参数。

学习 v7 已接入同一个最终主体解析接口：新快照排除占位和折叠别名，把历史消息／引用作者映射为最终主体；提交事务内在去重之前重新解析说话人、涉及人、别名所属人、联系双方和新从属主体的 parent。两端映射为同一人的 same_as 丢弃，别名若在合并后等于保留主体的显示名也丢弃。回复 R13 与学习共用数字／否定词比较，中文数字（如周三／周五）不同的记忆不会被近似去重。

## M2 生命周期与每日维护（后端）

保留强度限制在 0—100：有效记忆低于 F（默认 20）进入遗忘并记下时间；遗忘记忆达到 H（默认 35）恢复并清除遗忘时间；两阈值之间保持原状。管理员手动遗忘把强度降至至多 F−1 并取消置顶，恢复把强度提至至少 H，不重新置顶。置顶不自动恢复，也不受维护的自动变化：不衰减、不受依据失效扣减、不自动删除。重要度改变增加修订号；置顶、状态与保留强度调整不增加修订号，但所有人工操作仍核对 expected_revision 并留操作记录。

默认每天按实例 timezone 的 03:00 维护；本节的 M2 确定性阶段不调用模型，随后执行下节的 M3 梦境整理。接收、学习、准备和反馈照常。启动时超过 24 小时没有完成运行，才在随后所有入口安静满 10 分钟时补跑一次；安静时间从启动或最近接收时间中较晚者计算。中断的运行优先续跑，阶段游标、检查计数和项目变更在同一小事务中提交，已完成衰减不重做；续跑中新进入遗忘的时间记为实际执行时间，保留期不从旧请求时间起算。一次运行固定设置和对象上界，新设置对以后接受的运行生效，反馈与人工操作立即读取最新值。报告只为实际变更及失败保存逐项 ID；检查数量按阶段汇总，冲突／跳过按原因计数，无变更项目不逐条留记录。

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

生命周期设置：forget_threshold=20、restore_threshold=35（1≤F<H≤100）；feedback_increment=8、confirmation_increment=5、decay_amount=1、dependency_penalty=10（幅度 0—100）；auto_delete_enabled=true、auto_delete_days=180、upcoming_delete_days=14、message_retention_days=30（天数 1—36500）；maintenance_time="03:00"（严格 HH:MM）、abandoned_retry_enabled=true。学习再次确认已接入 `memory_ops.confirm_retention`，同一批次每条记忆只增加一次，默认 +5；遗忘记忆达到 H 时恢复有效。

## M3 梦境整理

一次每日维护依次执行：确定性衰减／状态转换、到期删除、依据失效扣减、消息清理、放弃批次重试；然后执行记忆模型整理、persona 定期更新、目标依据复核。手动触发与启动补跑走同一流程。每项用短事务提交，模型调用在事务外；记忆、persona 及目标写回前核对修订／版本和依据，冲突留到下一次。整理不改当前状态，不阻塞接收、学习、回复准备或目标操作。

合并沿用已选的 broad：确定性找候选并排除不兼容的说话人、立场、涉及人、时间、数字、否定，模型确认同一事实后才合并；置顶和最近正文修订为人工编辑的记忆不参与。保留较完整的原正文（同长取较早者），不生成第三种说法，并校验正文及来源；结果为有效状态、继承全部来源，相信程度不超过输入的最大值。被并入方用 `deleted + merged_into` 保留永久占位，原 ID 不复活、不召回、不接受反馈。同轮矛盾对不合并。

依据 DECISIONS 2026-10-10，矛盾和依赖复核只生成**模型建议**及可撤销标注（争议、依据不足、被某条记忆替代），不改正文、相信程度或删除对象，不重复 M2 的保留强度扣减。材料附双方来源摘录，每条最多取最早一条与最近三条，各最多 300 token。建议只在管理员整理报告和记忆详情中显示，附来源供核对；可采纳为“已确认”或清除，重复结论不重加，之后正文修订显示“标注后已修改”。未确认和已确认的建议都不进入回复准备或查询返回。置顶及人工编辑的记忆也可收到建议。

persona 在记忆整理后调用既有到期判断：自上一版至少五条自我记忆新增／变化，或满七天且有变化；“待更新”的依据计入变化。只在生成候选、逐句检查和版本／依据校验全部通过后发布，变化大时默认待管理员确认，仍遵循 persona 的 `small_medium_auto / all_auto / all_manual` 设置。未到期、缺模型、用途暂停、每日 token 限额或预算不足都有报告状态，原版保持可用。目标复核随后逐目标检查内部依据的当前状态，添加／结束依据标注，保留目标正文和状态；过期沿用目标模块按截止时间的只读标识，不自动完成或放弃。

全部模型阶段共享每次最多 50 次调用的持久预算，包括 HTTP 重试和 JSON 修正。记忆任务按重要度及变化时间先处理，未完成留到下次。persona 开始前至少剩两次额度，生成的重试／修正还须为检查保留一次；预算中途不足或检查失败不会发布部分候选。目标复核不调用模型，预算耗尽或模型暂停仍能执行。无 gateway 的离线调用跳过模型阶段，确定性维护和目标复核照常。中断后已完成项目不重做；已完成 persona 尝试可补回报告，不重新生成，未完成尝试标为中断后留待下一次整理。

管理员接口沿用会话、CSRF 和 Host 校验，前端接入点：

| 接口／字段 | 含义 |
| --- | --- |
| `GET /admin/api/settings` 的 `consolidation` | 返回以下七个设置字段 |
| `PATCH /admin/api/settings/consolidation` | 部分更新，未提供字段保持原值；写 `consolidation_settings_saved` 操作记录 |
| `maintenance_time` | 默认 `03:00`，严格 `HH:MM`；与 `lifecycle.maintenance_time` 共用存储，两条设置接口保持同步 |
| `max_calls` | 默认 50，整数 0—50；0 停止模型调用，目标复核照常 |
| `merge_enabled / conflict_enabled / dependency_enabled` | 合并／矛盾建议／模型依赖复核开关，默认全部 true；不关闭 M2 确定性维护 |
| `persona_enabled / goal_review_enabled` | 定期 persona／目标依据复核开关，默认全部 true |
| `POST /admin/api/maintenance` | `{}`，202 返回 `run_id`；已有未完成运行时返回同一个 ID |
| `GET /admin/api/maintenance`、`/maintenance/{run_id}` | 分页列表及完整报告 |

一次整理在接受请求时固定设置、时区与对象上界；改时间、预算或开关只影响之后接受的运行，续跑使用原快照。persona 的发布方式仍从既有 `/settings/persona` 修改，并在该次 persona 尝试被接受时固定。

报告的 `items` 只列实际变化与失败：合并与继承来源、带摘录的矛盾／依赖模型建议、persona 发布／待确认／拒绝、目标依据变更及失败原因。`summary.skipped.reasons` 汇总跳过，`summary.model_calls` 汇总调用数、已报告输入／输出 token、耗时及未知用量调用数；`consolidation` 给出本次设置快照、延期项数及停止原因。`persona` 给出 `status`（`published / pending / rejected / not_due / skipped / failed / conflict`，运行中为 `running`）、`reason`、`due`、尝试与版本 ID，候选结果附正文、检查结果和变化程度；`goal_review` 给出是否开启、检查数或关闭原因。未到期和无变化成功检查不逐条留日志；来源报告中的建议不等于已确认事实。

## 模型故障与状态

学习首次请求、调用内重试和 JSON 修正共享 180 秒总预算；其他生成请求为 120 秒，embedding 为 30 秒，召回查询 embedding 仍限 2 秒且不重试，评测判分为 240 秒。召回判断另有包含排队的 10 秒总预算、仅一次尝试。其他调用内最多再试两次；有有效 `Retry-After` 时按秒数或 HTTP 日期等待，否则分别以 2、4 秒为基数，加上 0 到基数之间的均匀随机抖动。等待和后续请求不得超过同一个总预算。批次重试间隔为 1／5／15 分钟，共四次尝试。

对话和 embedding 分别维护状态：`normal`、`temporarily_unavailable`、`invalid_key`、`configuration_error`、`account_problem`；每日上限造成学习暂停时，对话用途显示 `usage_limit`。连续三次可重试的网络／超时／限流／5xx 请求错误会暂停该用途（包含调用内重试的失败），触发暂停的批次失败不扣尝试次数。探测使用固定短请求，间隔为 1、2、4、8、10 分钟，之后保持 10 分钟；成功即恢复。分类优先识别结构化 `error.code`，其次兼容已知 `error.type`，最后按 HTTP 状态兜底。401 是密钥无效，404 及 `InvalidParameter`／`MissingParameter` 是配置错误，只在配置实际改变后恢复；403 权限、欠费、订阅及 `QuotaExceeded`／`SetLimitExceeded` 属账户问题，不作调用内短间隔重试，保留定时探测和内部立即重试。`AccountRateLimitExceeded`、`ServerOverloaded`、`RequestBurstTooFast`、500、传输错误、超时及审核服务故障 `ContentSecurityDetectionError` 可重试。方舟 `SensitiveContentDetected` 家族、输入／输出文本审核和风控命中，以及 `finish_reason=content_filter`，直接进入内容拒绝终态，不修正 JSON、不写记忆；原 MiniMax 敏感字段与 `base_resp` 识别保留。码表来源见[方舟官方文档](https://docs.volcengine.com/docs/ark/error-codes?lang=zh)。

服务会重新读取当前模型配置来源，修改模型配置（含推理档位）后对新工作生效。管理设置接口调用 `Gateway.replace_config(kind, ModelConfig(...))`、`Gateway.retry_now(kind)`，凭据只保存到 secrets.json；外部文件模式保持只读。`ModelHealth.set_daily_token_limit(整数或 None)` 设置每日 token 上限，默认不限；按角色 `timezone` 的次日零点或调高上限恢复。额度根据服务商已报告用量计算，在途请求可能越过上限，未报告 token 的调用不能计量。接收和召回始终照常。

embedding 暂停时网关直接返回可降级错误，回复准备和查询使用全文，并附 `model_paused` 提示。缺少向量的记忆照常进入全文索引，恢复后后台按修订号补算。embedding 维度参与配置变化检测、恢复探测和旧向量补算；改变模型或维度后仍须重新标定召回。`GET /api/v1/status` 提供 `model_health`、各入口 `current_batch`／`latest_batch`、`memory_gap_count`、今日／本周 `usage`、`learning_latency_24h`（P50／P95／最大耗时／超时数）和 `scheduler.running`；兼容保留最近调用 `models` 和积压 `backlog`。

迁移 006 为 `model_calls` 增加 `reasoning_effort`、`reasoning_present`、`reasoning_chars`；历史记录保持 NULL。推理字符数按 Unicode 字符计，已返回但非文本的推理字段记为未知；正文解析只用 `message.content`，不存 `reasoning_content`／`reasoning` 全文。`reasoning_tokens` 取 `usage.completion_tokens_details.reasoning_tokens`，缺失记 NULL，不从字符数估算；completion 用量已包含推理，不能再次相加。状态额外提供当前 `chat_reasoning_effort` 及逐次学习诊断。用量合计只覆盖已报告值，不能把未知调用当成零消耗。

运行日志同时输出控制台和数据库所在目录的 `logs/iris.log`，每 2 MB 滚动，保留 3 份旧文件；只记录状态、标识和错误类别，默认不记录消息正文或 API key。

## 运行记录

上线后的只读检查使用迁移 `024_runtime_journal.sql` 的两张元数据表。服务每次启动分配随机 `instance_id`；正常退出前等待已接受的后台工作结束，留下最终积压快照和停止记录。启动后每 **300 秒**由独立线程记录心跳，调度器卡住也不会阻止该线程采样。没有正常停止记录的退出，可结合心跳中断、下一次启动和进程监管日志推断。心跳表示进程记录线程活跃，不等于外部 HTTP 探测成功；启动记录对应服务生命周期开始，不是端口已经绑定的证明。

`runtime_events` 是追加表，无正文或自由格式 JSON：

| 字段 | 含义 |
| --- | --- |
| `id` | 单调递增事件 ID；同一时刻按 ID 排序 |
| `instance_id` | 32 位随机十六进制 ID；离线操作为 NULL，不能当作在线运行证据 |
| `occurred_at` | UTC、带偏移的 ISO 8601 记录时刻 |
| `event` | 下表的固定事件名 |
| `entry_id` | prepare 的入口 ID；鉴权／路由前被拒绝时可能为 NULL |
| `model_kind`, `configured` | 健康用途及是否已有非空端点和模型（0／1）；不记录端点、模型名称或配置摘要 |
| `previous_state`, `state`, `reason` | 固定状态与固定原因，不保存异常原文 |
| `run_id`, `phase`, `scheduled_at` | 维护运行 ID、阶段及预定 UTC 时间；`phase` 为 `maintenance`／`consolidation`／`persona` |
| `duration_ms`, `result` | 单次执行的总耗时（单调时钟、毫秒）及 prepare 分类 |
| `plan_id`, `notification_id` | 被取消／跳过的提醒计划 ID，以及替代它的通知 ID（如有） |

除 `id`、`occurred_at`、`event` 外，列只在对应事件有意义时填写，其余为 NULL。

| `event` | 取值和解释 |
| --- | --- |
| `process_started`／`process_stopped` | 生命周期开始／正常停止；不补造历史启动、崩溃或停止时间 |
| `heartbeat` | 启动、每 5 分钟和正常停止前各一次；与其所有入口快照同一短写事务提交 |
| `model_state` | `model_kind` 为 `chat`、`embedding`、`recall_judge`、`goal_dedup_judge`、`image_understanding`；整理、persona 等沿用它们实际使用的 chat 健康状态。状态为 `normal`、`configuration_error`、`invalid_key`、`account_problem`、`temporarily_unavailable`、`rate_limited`、`usage_limit`。可选用途 `configured=0` 表示尚未配置，不应当成等待自动恢复的故障。 |
| `maintenance_scheduled` | 提前记录下一个每日时刻；运行繁忙时仍观察计划。按角色时区计算后存 UTC。重启可能重复观察同一时刻，按 `scheduled_at` 合并计划，不把它们当多次执行。 |
| `maintenance_started`／`maintenance_completed` | 一次维护调用或整理／persona 阶段的开始与完成；`run_id` 对应 `maintenance_runs.id`。重启续跑会产生新的开始，耗时按各次调用单列；persona 阶段完成表示计算结束，是否仍待人工确认看既有 `maintenance_persona.status`。 |
| `maintenance_skipped`／`maintenance_failed` | 跳过／中断或失败及固定原因。`shutdown`／`interrupted` 表示可继续的工作，本次结束不代表整日完成；`schedule_changed` 只撤销原先尚未到时的计划。 |
| `reminder_resolved` | 只补计划 `cancelled`／`skipped` 的变化时间。`plan_id` 关联 `goal_reminder_plans.id`；合并过期阶段时 `notification_id` 指向实际替代通知。 |
| `prepare` | 每次宿主 POST prepare 的服务端 HTTP 总处理时间，含鉴权、排队、校验、召回、判断、提示和 JSON 序列化；不含网络传输和记录写入本身。分类 `normal`／`judge_degraded`／`fulltext_degraded`／`error`。错误含校验、鉴权、限流和未捕获异常，不保存错误详情。全文降级与判断降级同时发生时，分类优先 `fulltext_degraded`。 |

健康原因：`startup`（本实例初始观察）、`network`（网络／超时／可重试服务端故障）、`rate_limit`、`invalid_key`、`configuration`、`account`、`daily_limit`、`daily_reset`、`limit_changed`、`manual_retry`、`configuration_change`、`probe_success`、`response_received`、`cooldown_elapsed`。冷却到期和每日额度是计算状态，记录的是下一次观察到转换的时刻（调度轮询或调用时），不是虚构的精确服务商恢复时刻。配置变化即使仍为 normal 也留记录；跨实例使用 startup 作为初始状态，不将它误配为一次自动恢复。

维护原因白名单：`schedule_changed`、`shutdown`、`interrupted`、`internal_error`、`disabled`、`unconfigured`、`model_items_disabled`、`call_budget`、`usage_limit`、`paused`、`not_due`、`no_changes`、`already_running`、`revision_conflict`、`invalid_output`、`other`。提醒原因：`goal_changed`、`goal_closed`、`configuration_change`、`superseded`、`overdue_disabled`、`already_reminded`。无法映射的已有维护诊断只记 `other`。

`runtime_backlog` 用 `(heartbeat_id, entry_id)` 作主键，外键 `heartbeat_id → runtime_events.id`；包括空入口，删除心跳时级联清理：

| 字段 | 含义 |
| --- | --- |
| `pending_messages` | `messages.learning_state IN ('pending','batched')` 的未学消息总数 |
| `waiting_batches`, `running_batches` | 对应 `batches.state` 的批次数 |
| `oldest_wait_seconds` | 最老未学消息的 **received_at** 到采样时的秒数（非 occurred_at）；没有未学消息时为 NULL，无法解析接收时间时亦可能为 NULL |
| `memory_gaps` | 此时 `memory_gaps` 中该入口的缺口数 |

积压在一个只读快照中按索引汇总，采样时间写入心跳 `occurred_at`；随后单独提交事件与快照，不持有业务写锁扫描消息，不读取正文。记录使用现有单写连接，锁等待和 SQLite busy 等待各最多 50 ms；记录失败只写固定日志 `runtime journal write failed`，清理失败为 `runtime journal cleanup failed`，继续业务。新记录保留 **30 天**，每日维护完成时每批最多删除 1000 条过期事件及其积压快照；停服、维护中断或清理失败会延迟清理。既有业务表的保留策略不变。

提醒已有证据不重复记：`goal_reminder_plans.created_at/scheduled_at` 是计划建立／预定时间；已发布计划的通知可按 `goal_id + scheduled_at` 与 `notifications` 对应，目标合并另按现有目标合并关系追溯。`notifications.published_at/taken_at/cancelled_at` 是发布、首次取走及取消时间；取走不代表平台送达。新表只补过去无法确定时间的计划取消／跳过，早期没有记录的终态仍是未知。判断降级的独立明细继续读取 `recalls.request_json.judgment`，不以其判断耗时冒充 prepare 总耗时。

连接器 `tools/live_review/` 可据此补充判定（需连接器线适配本 schema）：

- 服务连续性：按实例读取开始、心跳、停止，检查窗口覆盖与心跳缺口，并结合监管／宿主成功记录。缺失、记录失败或超过保留期不能计为正常。
- 入口卡住／积压清空：逐入口比较 5 分钟快照、最老等待时间以及既有 `admin_operations.batch_result`／`batch_attempts` 的成功进度；持续一小时无进展需标出。最终清空须有窗口末附近的零积压快照或该时刻一致读证据，不能拿当前状态代替历史结束时刻。
- 自动恢复：同一实例、同一模型用途配对暂停到 normal，区分 `probe_success`／`cooldown_elapsed`／`daily_reset` 与 `manual_retry`／`configuration_change`／`limit_changed`；再核对恢复后的积压。未配对、跨实例、缺日志不判通过。
- 每日整理：将事先记录的每日时刻与 `maintenance_started` 和完成事件、既有冻结 `maintenance_runs` 关联；300 秒延迟、缺开始／完成、阶段跳过和失败各自列出。手动／catchup 不能替代缺失的 scheduled 运行，缺未来计划也不能凭无错误宣称未漏跑。
- 提醒：结合计划、补充取消／跳过事件和既有通知时间，检查 300 秒发布／取走延迟及到期未发布；及时取消与合并通知单列，不能把被取走当作送达。

## 评测

```bash
uv run iris eval learning --split dev --judge-runs 1
uv run iris eval learning --judge-runs 2
uv run iris eval learning --judge-mode external --out <运行目录>
uv run iris eval e2e --judge-mode external --out <运行目录>
uv run iris eval e2e --judge-mode external --script E001 --script E011 --out <流程检查目录>
uv run iris eval e2e-score --materials <材料目录> --judgments <第一轮目录> --judgments <第二轮目录> --judge-model <执行者模型名称> --out <报告目录>
uv run iris eval persona --corpus evals/persona_v1.json --judge-mode external --out <运行目录>
uv run iris eval persona-score --materials <材料目录> --judgments <第一轮目录> --judgments <第二轮目录> --judge-model <执行者模型名称> --out <报告目录>
uv run iris eval recall
uv run iris eval recall --split dev --calibrate
# 如需重新比较 1024／2048 维，再加 --compare-embeddings
uv run iris eval recall --corpus <外部 JSON 或 JSONL> --out <外部目录>
uv run python evals/benchmark_retrieval.py --default-config
```

默认公开学习集为 learning_v1—v5，共 116 段；v5 的 12 段手写 dev 在 `905f955` 冻结，覆盖活动协调、游戏过程与关键结果、跨批改述、亲友近况和转达通知。报告按 v1—v5 分组，同时列出 v1＋v3＋v4 和 v1＋v3＋v4＋v5 合计、近似重复与再次确认。语料格式、完整外部明细、标定方法和报告见 [evals/README.md](evals/README.md)。学习和召回的最终验收仍需规划者运行隐藏集。

当前回复检索默认 trigram＋2048 维 float32，向量下限 0.35、相对比例 0.75、向量／全文权重 1:1，查询加“为这个问题检索能回答它的个人记忆：”前缀；全文覆盖率 0.75，长片段最大文档频率 2。无向量时使用独立标定的 jieba 全文降级，覆盖率为 0，长片段文档频率通路关闭。新数据库写入这些默认值，已有设置保留。学习材料保持独立的 PR #4 设置。

trigram 查询中的一字／两字实词会补查已有 jieba 索引；只询问已知姓名时，也能按涉及人、说话人及正文提及取回候选。长短词结果按全查询词项覆盖率筛选，稀有长词片段也可按整库文档频率入选，之后合并去重，人物与类型等过滤继续生效，混合检索和全文降级均覆盖短词。

召回评测默认运行七份公开集（含 recall_no_answer_v1），共 243 条固定记忆、228 条查询。`default_judged` 使用正式 prepare 判断路径；原全文／混合对照关闭判断。报告单列判断降级次数和占比；有降级的运行不能作为正常路径结果。GLM 阶段全部按 dev 使用，保留原 split 作历史分组；第四轮新增 16 条手写短词查询，先单独冻结；第五轮继续按原规则在 122 条 dev 上重新标定。选参、逐项对照和已知问题见 [evals/README.md](evals/README.md)。本次对话查询默认值由规划者批准为守则例外：conversation_v2 relevant 精确率下降 0.0682，conversation_v3 无答案误返由 1/4 增至 3/4，见 [第二轮报告](evals/reports/conversation-prepare-20261008.md#第二轮)。召回判断按 PR #25 已批准方案固定，不重新选参。

点名检查仅读取检索和人物要点候选的正文。性能脚本加 `--default-config` 覆盖当前默认的 5 千／5 万条、点名／不点名四组；不带该参数还会比较维度和 dtype，并在合成测试库中关闭向量截断。性能方法和本次报告入口见 [evals/README.md](evals/README.md)。学习影响按 2026-10-06 决定改为全部公开学习语料的确定性请求比较；第一阶段真实学习对照已作废。

学习与端到端评测默认仍调用对话模型判分，只作预览；正式评测使用外部执行者判分。端到端外部模式导出每检查点一份材料，不调用判分模型。材料目录含 manifest.json、scoring.md、cases/*.json、run.json 和 round-template.json；每轮把模板复制为 manifest.json，按清单写 facts／forbidden 判分。e2e-score 离线严格校验后生成原格式 JSON 报告，一次／两次 --judgments 分别为单判／双判，事实覆盖取 AND、禁止说法取 OR，U04 单列。完整文件约定及独立判分要求见 [evals/README.md](evals/README.md#端到端外部判分流程与文件格式)。小样本单判只用于流程检查，不作门槛结论。

端到端公开集现为 11 个手写脚本，包括群聊学习、重启后私聊提问的 E011。prepare 的查询文本只省略或使用提问原文；评测器另行校验近期原始消息没有跨入口。跨入口来源格式见 [评测说明](evals/README.md)。

评测启动子进程时，临时 TOML 只记录端点、模型和 `api_key_env` 变量名，密钥仅通过该子进程环境传递；不会修改调用者环境。评测的配置来源仍为 `test-models.toml`／`IRIS_TEST_MODELS`，serve 的配置优先级见上文。需要时也可在模型配置组中用 `api_key_env` 引用已设置的环境变量，不能与 `api_key` 同时填写；变量缺失会明确报错。

GLM 服务商默认 max 推理可能耗尽 180 秒学习预算；依据 `DECISIONS.md` 2026-10-08「GLM 默认推理档位改为 high」，设置页的方舟预设及开发配置使用 high，未指定档位的外部配置仍由服务商决定。学习仍共享 180 秒预算。学习提示词默认 v7，先筛长期价值，再核对完整证据、人物归属、别名声明和时间一致性；补充转述视角、扮演双方主体、周一周界和跨批更新要求，v5、v6 保留。学习精确率问题列为已知问题，延后到上线后优化，M2 不再为它迭代提示词。[learning_v8.md](src/iris/prompts/learning_v8.md) 已冻结、未启用，留作上线后的精度优化起点；冻结信息及本轮保留的确定性修复见[收尾说明](evals/reports/m2-learning-v8-20261009.md)。参与者编号紧接完整已知姓名、带空格或括号时均解析回同一主体；编号与姓名不匹配则弃项，不按拼接后的名字新建主体。正文和标签中的括号显示名只展开一次；未使用的 null 关系字段可省略，实际关系的唯一性与证据要求不变。学习再次遇到同一命题时确认原记忆，包含遗忘中的记忆；每批仅增加一次保留强度（默认 +5），达到恢复阈值 H 后恢复有效，已删除对象不复活。数值或否定序列不同的命题不作近似重复合并。推理档位和方舟错误码适配已合入；M1 的评测门槛在定稿代码上以公开样本和规划者隐藏集的独立双判达到，M1 已完成（DECISIONS.md 2026-10-08）；公开样本的单判及流程试用仍不能作为门槛结论。

## M4 备份：在线导出与离线导入

服务运行时，管理员可通过 `POST /admin/api/backups/export` 下载备份，JSON 体默认为 `{}`；显式传入 `{"include_secrets":true}` 才包含数据目录内的模型密钥。请求沿用管理员会话、JSON Content-Type 和 `X-Iris-CSRF`，宿主令牌不能代替管理员会话。响应为 ZIP 下载，`Cache-Control: no-store`；含密钥时文件名带 `-with-secrets`，`X-Iris-Backup-Includes-Secrets` 为 `true`，清单和导出记录也明确标记。`GET /admin/api/backups?limit=30` 返回最近导出记录（最多 100 条）、是否含密钥及离线导入说明。前端入口由 M4 界面线接入。

停服后也可使用命令行；这些命令不加载测试模型配置、不调用模型：

```bash
uv run iris --data-dir /path/to/iris-data backup export --out /path/to/backup.zip
# 需要将本地模型密钥一起备份时，显式加 --include-secrets
uv run iris --data-dir /path/to/new-data backup import /path/to/backup.zip
# 覆盖已有实例：先停止服务及其他配置命令，再显式确认
uv run iris --data-dir /path/to/iris-data backup import /path/to/backup.zip --confirm-overwrite
# 如需把现有模型密钥留在覆盖前自动备份中，再加 --backup-include-secrets
```

导出包格式版本为 1：`manifest.json`（UTF-8）列出备份 ID、创建时间、迁移版本及完整迁移序列、是否含模型密钥、是否包含媒体目录，以及各文件的字节数和 SHA-256；内容为 `iris.db`、已有的 `media/` 文件，以及选择包含时的 `secrets.json`。不包含日志、旧备份、部署配置、外部 TOML 或环境变量中的密钥。媒体路径沿用 `media/<sha256>`，数据库快照同时保留媒体对象、理解文本和消息引用；导出与导入均校验登记的哈希、大小与文件一致。媒体目录在快照和复制期间发生变化时整次导出失败，可重试；不会发布缺损包。

数据库来自独立只读连接上的 SQLite 在线备份，固定同一已提交快照，不占用接收或学习使用的写锁。模型配置锁只覆盖快照建立和可选的密钥捕获。CLI 输出和服务端下载临时文件权限为 `0600`，浏览器保存后的权限由浏览器决定；同名输出文件不会被覆盖。导出和导入操作记录只存标识、版本、数量与密钥包含标志，不存文件内容或本机路径。备份仍包含原始消息、记忆、管理员密码摘要和宿主令牌摘要，应按实例数据私密保存；选择包含的模型密钥在 ZIP 内不加密。

默认无密钥的备份会清除**副本**中的模型密钥引用，保留端点、模型名等非敏感配置，原实例不变。导入这种备份也不会沿用目标目录原有的密钥文件；需要重新配置模型密钥，或继续使用原来的外部配置方式。宿主令牌摘要、撤销状态和入口范围原样恢复，令牌明文不会重新生成。

导入仅提供离线命令，选择该方式是为了在整体替换数据库、媒体和可选密钥时没有运行中的连接继续访问旧数据。程序持有服务锁和配置锁；先校验清单、每个文件的完整性、数据库完整性与外键、迁移序列，再在私有临时目录依次迁移旧版本。较新或不兼容的版本、截断包、路径越界和符号链接条目会被拒绝。覆盖前自动生成 `数据目录名.pre-import-<随机ID>.zip`，放在数据目录旁并在命令结果中返回路径；默认同样不含模型密钥，加入 `--backup-include-secrets` 才包含。自动备份失败不会覆盖数据。

校验和迁移成功后，通过 macOS／Linux 的原子目录交换一次发布完整数据；服务锁在交换前后保持同一文件身份。失败或进程在交换点中断时，目标目录只会是完整旧实例或完整新实例。其他文件（如日志）保留；崩溃可能留下私有 `.iris-import-*` 临时目录，可在确认恢复结果后删除。此导入实现要求数据目录可在同一文件系统原子交换，不支持直接替换挂载点或 Windows；当前验证平台为 macOS。恢复后按原方式启动服务，未完成批次由既有重启恢复流程处理。


## M4 媒体与图片理解（后端）

媒体保存在数据目录的 `media/`，按 SHA-256 共享文件；每次保存返回独立媒体对象，不覆盖同文件其他对象的宿主说明。单文件默认最多 10 MiB（10 × 1024 × 1024 字节），检查 MIME 和文件头。图片白名单为 PNG、JPEG、GIF、WebP（`image/png`、`image/jpeg`、`image/gif`、`image/webp`），不接受 SVG、HTML 或任意文件类型。音频支持 MP3、WAV、Ogg、FLAC，视频支持 MP4、WebM；仅保存附件，不调用音视频理解模型。无宿主说明时分别使用 `[音频，未理解]`、`[视频，未理解]`。文件头校验不等同于完整解码，损坏图片的模型失败仍退回占位。

在 `test-models.toml` 中由用户新增 `[image_understanding]`，填写 `base_url`、`model`、`api_key`（或省略 `api_key`，填写 `api_key_env` 所指的环境变量名）；可选 `reasoning_effort`，只在所选模型支持时设置。不需要 `dimensions`。该组使用方舟视觉模型，端点和模型 ID 以用户控制台为准，不自动沿用对话模型。请求使用 OpenAI 兼容 `/chat/completions` 的 `image_url` data URL，见[方舟图片输入契约](https://docs.volcengine.com/docs/ark/chat-api?lang=zh&redirect=1)。本线仅以假模型验证，未读取测试密钥，真实视觉模型冒烟需用户配置后另行运行。

本地模型配置、导入、热重载和脱敏设置投影自动包含 `image_understanding`。管理接口 `PUT /admin/api/settings/models/image_understanding`、同路径的 `POST /test`、`POST /retry` 沿用管理员会话和 CSRF；`enabled=false` 停用。测试连接使用固定 64×64 PNG、总预算 10 秒；探测图片满足[方舟输入尺寸下限](https://docs.volcengine.com/docs/ark/image-understanding?lang=zh)。设置页已提供独立的图片理解模型组。

处理顺序是：保存媒体 → 消息引用 `media_ids` → 按原节奏冻结批次 → 领取批次为 `running` → 补充图片理解 → 读取学习快照、组成材料 → 学习。上传和接收消息不等待模型。整批图片（目标优先，再历史、后续）共享最多 120 秒，包含排队、网络和调用内重试；随后学习仍有独立的 180 秒预算。含媒体消息在冻结时预留原有的每消息 1500 token 上限。图片理解并发最多 2、排队最多 8，独立线程池和健康状态；不等待其他批次已在理解的同一对象，本次使用其已有占位。图片较慢时端到端延迟可能超过实时入口的一分钟目标。

每个对象优先直接使用宿主理解文本（UTF-8，最多 32 KB）；其余图片在配置可用时逐个理解。一般网络、超时、429、5xx 和无效响应最多再试两次，间隔 2、8 秒，服务商的 `Retry-After` 优先，始终受整批预算限制。失败或未配置用 `[图片，未理解]` 继续学习；图片失败不触发学习批次重试。明确安全拒绝固定为“敏感信息无法访问”，标记为被拒绝，同文件保留期间不再自动理解；识别[方舟图片安全错误码](https://docs.volcengine.com/docs/ark/error-codes?lang=zh)，不将安全检测服务故障误当内容拒绝。已结束的理解尝试不会因为历史段重复出现或批次重新学习而自动重做；未配置、用途已暂停或排队不足而未完成的对象，在以后组成材料时可再尝试。没有额外图片后台补算任务。

材料在对应消息正文后追加一行，媒体按消息引用顺序编号，例如：

```text
媒体（数据）=[{"序号":1,"类型":"图片","来源":"宿主提供","被拒绝":false,"理解文本":"窗边有猫。"},{"序号":2,"类型":"图片","来源":"被拒绝","被拒绝":true,"理解文本":"敏感信息无法访问"}]
```

来源固定为“宿主提供／本系统理解／被拒绝／未理解”；文本使用 JSON 转义。正文与媒体文本合并后沿用单条 1500 token 截断，原始正文和理解文本完整保存；材料超过原总预算时沿用去除上下文的规则。无媒体消息材料逐字不变，学习检索仍使用原正文和原选材规则，召回算法与返回材料未增加媒体字段。

消息清理继续遵守来源引用保护；最后一条消息引用消失后才开始 1 天宽限，文件在后续每日维护中删除。仍被任一消息引用的共享文件不删；操作记录不算引用。未绑定消息的上传也有 1 天引用窗口，崩溃遗留的临时／未登记文件在一天后清理。删除文件同时移除其无引用媒体对象；正在理解的迟到结果不能重建它们。

宿主上传 `POST /api/v1/media` 和消息 `media_ids` 已接通，见[宿主接入文档](docs/host-api.md)。保存函数约定见 [ARCHITECTURE.md 的媒体一节](ARCHITECTURE.md#m4-媒体保存与接线契约)。

管理员媒体接口（管理员会话；写请求同时带 `X-Iris-CSRF`，遵守现有 Origin 与 JSON Content-Type 检查）：

| 接口 | 返回与用途 |
| --- | --- |
| `POST /admin/api/media` | 201，上传试用图片，JSON 为 `{"content_type":"image/png","data_base64":"标准 Base64"}`；返回下面的媒体投影 |
| `GET /admin/api/media/{media_id}/file` | 200，原文件字节；管理员可读取所有入口及内部媒体，宿主 Bearer 令牌不能替代管理员会话 |
| `POST /admin/api/trial/entries/{entry_id}/messages` | 既有 201 回执；增加可选 `media_ids`，最多 100 个、不重复，按数组顺序引用图片 |
| `GET /admin/api/trial/entries/{entry_id}` | 每条 `messages[]` 增加 `media[]`，轮询可读取最新理解结果 |
| `GET /admin/api/batches/{batch_id}` | 三段现存消息均增加 `media[]`；已清理的消息沿用 `missing` 占位对象 |
| `GET /admin/api/memories/{memory_id}` | 来源消息和前后文均增加 `media[]`；目标来源前后文沿用同一媒体投影 |
| `GET /admin/api/status` | 新增 `usage.by_purpose.image_understanding.today/week`，以及 `timeouts_seconds.image_understanding=120` |

先上传每张图片，再把返回的 `id` 放进试用消息的 `media_ids`。`content` 可以为 `""`，但正文和图片不能同时为空；保留原 `speaker_id`、`dedupe_key`。新消息的媒体无效时整条消息回滚；重复去重键返回原消息，不覆盖原正文或图片。管理员上传只接受 PNG/JPEG/GIF/WebP，解码后最多 10 MiB，完整 JSON 最多 14 MiB（也限制实际接收的分块正文）；超限 413，非法类型、文件头、Base64 或字段为 400。上传不接受文件路径、URL、客户端文件名或伪造理解文本；新对象以未理解状态进入原有学习流程。未发送的上传沿用一天清理宽限。独立管理员上传不会向宿主令牌授予媒体引用权限。

`media[]` 包含 `id`、`kind`、`content_type`、`size_bytes`、`understanding_text`、`understanding_source`、`understanding_source_label`、`completed_at`、`file_url`。来源代码为 `host/system/refused/unprocessed`，中文标签为“宿主提供／本系统理解／被拒绝／未理解”。`completed_at` 是宿主说明接收或本系统理解尝试完成的时间，未完成为 null；一般失败也可有完成时间，须结合来源代码区分成功。读取不触发理解、学习、召回或保留强度变化。无媒体消息返回空数组。管理投影展示所有入口媒体，不受宿主可见范围筛选；文件系统路径和处理租约不返回。

`file_url` 可用于同源图片预览；提供原图，由界面缩放。响应使用登记的图片/音频/视频 MIME，`Cache-Control: no-store`、`X-Content-Type-Options: nosniff`、`Cross-Origin-Resource-Policy: same-origin`，不公开缓存。读取只接受媒体对象 ID，从数据库解析内容哈希，固定目录和文件描述符，拒绝符号链接，复核大小、哈希与文件头；不存在、已清理或文件不可用均为 404，不回显磁盘路径，也不重建目录。

图片用量按角色时区统计今日和本周（周一开始），结束于查询时刻。每个分桶包含 `calls`、`prompt_tokens`、`completion_tokens`、`reasoning_tokens`、`tokens`（输入＋输出）、`calls_without_usage`、`failures`、`refusals`、`failure_rate`、`duration_ms`（总耗时）、`p50_ms`、`p95_ms`、`max_ms`、`timeouts`。统计图片用途的实际调用记录，包含调用内重试、设置连接测试和恢复探测；旧记录按图片 purpose 兼容归类。失败包含全部非成功调用，拒绝是其中 `content_rejection` 的子集；无调用时延迟和失败率为 null，缺失 token 不作推算。图片调用已计入原有总用量，界面不要再次相加。

## M4 入口的记忆可见范围（VS）

入口默认 `shared`（全部共享），可改为 `entry_only`（本入口）或 `entries`（指定入口列表，始终包含本入口）。管理员通过已有会话与 CSRF 调用：

```http
PATCH /admin/api/entries/入口ID/visibility
Content-Type: application/json
X-Iris-CSRF: <当前会话的 CSRF 值>

{"visibility":"entries","visible_in":["另一个入口ID"]}
```

入口必须已经存在；名单完整替换，不能引用未知入口。改动写入管理操作记录。`GET /admin/api/entries` 和入口 `/settings` 返回 `visibility`、`visible_in`；记忆详情返回 `visibility.shared` 与计算后的 `visibility.visible_in`，空列表且 `shared=false` 表示仅管理员可见。

记忆范围是全部来源消息的入口范围与派生依据范围的交集；目标还与所属入口范围取交集。无来源目标按所属入口计算，无所属入口的目标全局可见。计算使用当前设置，收紧、放宽、撤换名单对旧对象和派生链立即生效，不必重新学习。原始消息仍只返回本次入口的近期窗口。

回复准备、人物要点、点名检索、深度召回、目标分区及其附带引用均按查询入口过滤。底层 `Retrieval.search(entry_id=...)` 支持指定入口；省略入口时仅返回全局可见的记忆和目标，不按宿主令牌范围放宽。HTTP `POST /api/v1/memories/search` 已接通该入口参数，令牌无权访问指定入口时返回 `403 entry_forbidden`。令牌范围另行限制目标分区；本节取代前文接入过渡说明中的待接线状态。不同实际范围的对象不做召回去重、学习再次确认、目标去重或整理合并；整理的矛盾与依赖材料也不跨范围。persona 的依据仅限全局可见的自我记忆，已发布句子的依据被改为私有时，该句立即停止进入宿主和学习材料，等待后续更新。

隐私 dev 检查与回归命令（完整输出放在仓库外）：

```bash
uv run python evals/visibility_eval/run.py --out <外部目录> --judge --http-search
uv run python evals/visibility_eval/run.py --corpus <JSON路径> --out <外部目录> --offline
uv run python evals/visibility_eval/compare_learning.py --baseline <主线源码目录> --candidate . --out <外部目录>
uv run python evals/visibility_eval/compare_recall.py --baseline <主线源码目录> --candidate . --out <外部目录>
uv run python evals/visibility_eval/benchmark.py --out <外部目录>
```

隐私运行使用默认召回配置；`--http-search` 让所有 search 查询经带真实宿主令牌的 ASGI HTTP 接口，prepare 保留语料的精确近期窗口。HTTP 错误会令整次运行无效。`--embedding-cache <已完成的公开向量缓存>` 可复用 embedding，召回判断仍重新调用。`--offline` 检查默认全文降级路径，不加载模型配置。`--judge` 开启真实召回判断，判断降级或查询未完成会将整次运行标为无效。门槛只检查 forbidden 和跨范围合并；expected / expected_goals 只作为过度过滤诊断。公开集零泄漏不等于 M4 隐藏验收通过。
