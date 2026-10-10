# M4 VS-01：main / HTTP 集成复验

本轮把 `origin/main@43f8221b1ba482f98748667c20ff3014b9ce2e32`（接口 v1，PR #66）以 `git merge` 合入 `feat/m4-visibility`，保留已推送的 `17044d2` 历史。没有 rebase、force push 或合并 PR。导出导入 PR #64 已包含在首轮提交的历史中。

合并没有冲突，README 自动合并；admin.py 的备份接口与可见范围接口均保留。api.py、media.py、queue.py 及 AP 的契约测试直接取上游版本，没有手工修改。主线最新迁移为 022，VS 仍用 `023_visibility.sql`，没有编号冲突。

## HTTP 隐私边界

AP 的 Search.entry_id 字段先做令牌入口权限检查，再将非空值传给 Retrieval.search(entry_id=...)。省略／null 不传参，落到检索层默认的全局可见限制。API 中兼容旧检索签名的分支保留，实际支持 entry_id 的分支已通过本轮 HTTP 测试。

新增两个使用正式宿主令牌的 HTTP 测试，覆盖指定 A／B 入口、省略 entry_id、include_goals、include_forgotten、HTTP prepare，以及单入口令牌无权访问 B 时的 `403 entry_forbidden`。403 在检索调用前发生；全部入口令牌也不能绕过可见范围，单入口令牌也不会放宽省略入口的查询。另增加评测的 HTTP 全语料分支和 HTTP 错误使运行无效的检查，VS 专项共 30 项测试。

`evals/visibility_eval/run.py --http-search` 通过进程内 ASGI TestClient 使用真实 Host 校验、Bearer 认证、路由验证、参数转交、HostRetrieval 和 JSON 响应。只停用无关后台调度、固定检索时钟及观察候选，不替换 search 或目标授权。prepare 的 38 条查询仍经原精确近期窗口适配；所有 61 条 search 均经 HTTP，逐查询记录 transport 与状态码。

## visibility_v1 复验

默认配置、真实 embedding、重新调用召回判断，使用已完成的公开 embedding 缓存副本。

| 项目 | 结果 |
| --- | --- |
| 案例 / 查询 | 32 / 99，全完成，运行有效 |
| HTTP search | 61/61，均 200 |
| forbidden / 泄漏 | 74 / **0** |
| no_merge / 跨范围合并 | 6 对 / **0** |
| 动态设置变更 | 4 次，均按查询 at 应用 |
| expected 记忆 | **93/94（98.94%）** |
| expected_goals | **18/18（100%）** |
| 判断降级 / embedding 降级 / HTTP 错误 | **0 / 0 / 0** |

唯一诊断未命中仍为 VS019/q1/m1，由召回判断过滤，可见范围没有误拦截。no_merge 直接检查产品 merge_exclusion 和 _plan_pair 的确定性结果，不调用整理模型。离线直接／HTTP 两种通路在全量测试中均零泄漏，诊断为 94/94 与 18/18。门槛只按 forbidden 与跨范围合并判定，诊断不用于放宽门槛。

冻结语料 SHA-256：`5792555e24dba6ed7ab2b83258b87e6d588db88f27fc8e321dba13ca78260e1e`。
产品源码／迁移指纹：`a68fada397ebffc04bdede99da3af136243d65258bb0a3910977b858b36df205`（沿用 privacy runner 的路径加内容算法）。

## 默认 shared 对照

全部对照使用 `43f8221` 的仓库外源码快照，候选为本轮合并后的代码。学习 5 份公开语料、116 段、272 批请求逐字一致，差异 0；双方非空材料 156 批、记忆 1925 条。请求 SHA-256 均为 `9988666d3634cdde81ded0d1a540cf92cbda5d30b65bf8ae5a85301274861092`。

七份公开召回语料共 243 条记忆、228 条查询；5 个变体、1140 次查询对照，返回 key、顺序、reason 差异 **0**。固定双方入库主体 ID，复用公开向量缓存，主线重新采集 191 次真实判断；候选只回放完整输入及候选 ID 逐字一致的对应结果。双方输入一致，降级均为 0，运行有效。

整理 40 例的材料、配对与依赖判断一致；persona 65 个检查点选材一致（合计 50 例），差异 0。这些是确定性回归对照，不是新质量判分。

## R10、测试和构建

100 个入口中 50 个 entry_only，每条合成记忆有来源；沿用默认召回配置，排除模型网络，包含本地判断处理。每种数据量独立进程、5 次预热、每组 60 次。

| 记忆数 | 点名 | P95（ms） | 首次调用（ms） |
| --- | --- | ---: | ---: |
| 5,000 | 否 | 21.36 | 30.28 |
| 5,000 | 是 | 29.52 | 30.01 |
| 50,000 | 否 | 136.57 | 187.02 |
| 50,000 | 是 | 177.72 | 183.60 |

四组均 ≤500 ms。本机 macOS arm64；合成串行负载，不代表并发吞吐。

- `uv run pytest`（Python 3.13.15）：**2025 passed**，298.92 秒。
- `uv run --locked --isolated --python 3.12 pytest`（Python 3.12.14）：**2025 passed**，308.66 秒。
- `uv build`：**成功**，sdist、wheel 均生成，wheel 包含迁移 023。

两个测试版本顺序运行，各有 4 条既有依赖警告；3.12 在全部模型评测结束后启动，worktree 的 .venv 保持 3.13.15。评测期间没有修改源码、迁移或重建环境。

## 原始材料

完整材料、隔离数据库、原始判断、返回和日志均在仓库外：`/Users/cassia/Local/Code/iris-eval-artifacts/m4-visibility-vs01-integration/`。

对应 `privacy-http/report.json`、`learning/summary.json`、`recall/summary.json`、`cognition/summary.json`、`performance/summary.json`、`pytest-313.log`、`pytest-312.log`、`build.log`。首轮材料与报告保留。本轮没有读取隐藏集、改动冻结语料／提示词／评分说明，公开 dev 零泄漏不代表隐藏验收或整个 M4 门槛通过。
