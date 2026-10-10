# 上线前最终复验公开部分（2026-10-11）

执行者 MG，仅运行、组织独立判分和报告，未参与候选实现。本报告不作上线结论；未寻找或读取隐藏验收集。入口画像按用户决定仅作诊断。

候选：`fb1a8ada58a7f9f0da9e202a46fe7dabdf1a060d`；对照：M4 定稿 `c6bf57d48df0cd53a1dfdffe4a93309caa5ae104`。候选与基线分别从 detached worktree 运行；报告提交时才在候选上建立 `eval/final-check`。插件独立检出 `dev.4`：`305cd020a3ca044f0e33f55c4b65e66f459393a6`，并与其上游分支核对一致。

完整材料根目录（下文 `R`）：`/Users/cassia/Local/Code/iris-eval-artifacts/final-check-20261011/`。候选目录（`W`）：`/Users/cassia/Local/Code/iris-core-final-check`；基线目录（`B`）：`/Users/cassia/Local/Code/iris-core-final-baseline`。命令、UTC 起止时间、退出码和墙钟耗时逐项记录在 `R/results/*.json`，原始输出在 `R/logs/`，外部适配脚本在 `R/scripts/`。报告日期为 Asia/Shanghai；执行记录使用 UTC。

运行设备：macOS 26.6.2 arm64、10 个逻辑核、16 GiB 内存；候选环境 Python 3.13.15，另以 locked isolated 3.12 运行全量测试。真实模型运行依次为端到端、隐私、入口画像；每次启动检查评测进程并获取互斥锁。只由产品读取指定 `IRIS_TEST_MODELS` 配置；未打开、复制或输出密钥文件。运行前后源码、提示词和公开材料指纹相同。所有实际试用数据均新建于材料目录，未访问用户已有 Iris 数据目录。

| 项目 | 结果 | 主要证据 |
| --- | --- | --- |
| Python 全量测试、构建 | 3.13 与 3.12 各 2339 passed；wheel 与 sdist 构建成功 | `logs/pytest313.log`、`pytest312.log`、`build.log` |
| 前端可复现 | 28 文件、328 测试通过；3 个提交产物文件名、内容 SHA-256 全部一致，无缺失或多余文件 | `results/frontend-reproducibility.json` |
| 学习请求回归 | 5 份公开语料、116 案例、272 批，0 差异 | `learning-regression/summary.json` |
| 召回回归 | 7 份公开语料、228 查询、每版 445 条返回项，ID／顺序／reason 0 差异 | `recall-regression/summary.json` |
| R10 | 四组带令牌 HTTP prepare P95 为 80.65／122.81／220.54／274.30 ms，均 ≤500 ms | `r10-01/retrieval-performance-pr5-r3.json` |
| 端到端 | E001—E011 双判 11/11；26 项判定中 0 分歧；请求要求至少 9/11 | `e2e-score/e2e-20261010T200416100963Z.json` |
| 隐私 | 32 案例、99/99 查询；泄漏 0；6 个 no_merge 检查无误合并；可见记忆命中 93/94 | `visibility-01/report.json` |
| 入口画像（仅诊断） | 模型句依据 30/34（88.24%）；forbidden 0，另有泛化 4、个人信息 1；must_cover 14/40；拒绝 1/6；字段分歧 6 | `entry-profile-score/entry-profile-score-20261010T202832-7018bf58.json` |
| 画像返回范围 | 未发布时为空；A／B 各只返回本入口已发布画像；窄范围令牌访问 B 得 403；search 不含画像 | `actual/summary.json` |
| 升级、备份、运行记录 | 024→026；迁移前备份成立；原数据 0 差异；scheduled 备份成功且 keep=2 生效；启动／周期心跳／停止均有记录 | `actual/` |
| 插件 dev.4 | 全部 319 测试通过，真实 iris-core 集成执行且通过，指向候选 | `logs/plugin-tests-02.log`、`plugin-pytest-tmp/test_real_core_receive_prepare0/integration-result.json` |

**测试、构建和可复现性。** 依次执行 `uv sync --python 3.13`、`uv run pytest`、`uv run --locked --isolated --python 3.12 pytest`、`uv build --out-dir R/build`；前端依次 `npm ci`、`npm test`、`npm run build`。Python 测试内部计时为 403.60／406.05 秒，命令墙钟为 408.28／416.66 秒；构建 2.41 秒；三个 npm 步骤为 8.09／8.04／2.38 秒。构建后候选跟踪文件无差异。Python 各有 4 条第三方警告（jieba 转义与 Starlette TestClient 弃用）；npm 提示 fsevents 的安装脚本未获 allowScripts 授权，但安装、测试与构建成功，未更改配置。未在真实模型运行期间修改源码、迁移或虚拟环境。

**回归口径。** 学习使用 `uv run python evals/visibility_eval/compare_learning.py --baseline B --candidate W --out R/learning-regression`，该包装器调用学习请求比较器并显式包含 learning_v1—v5 全部公开语料。固定生成时间和检索时钟，不规整提示内容、空白、引用或顺序。两版均为 272 批，其中 156 批含相关记忆、假学习累计 1925 条记忆；完整请求 SHA-256 同为 `9988666d3634cdde81ded0d1a540cf92cbda5d30b65bf8ae5a85301274861092`。墙钟 27.87 秒。

召回使用 `uv run python R/scripts/recall_regression.py`，全部七份为 recall_v1、recall_v2、recall_conversation_v1、recall_short_terms_v1、recall_conversation_v2、recall_conversation_v3、recall_no_answer_v1。通过真实鉴权的 ASGI HTTP 宿主路由和 all 范围令牌调用，`judge=false`；固定语料时钟、入库 UUID，关闭后台调度以保护固定样本，不规整返回值。复用上一轮公开召回的 471 个固定向量，缓存 SHA-256 为 `546d5e81ba97b97aaad2acb345b62b20c08999fb4cf7c9c12dbfe80af67867cf`。有效运行 46.24 秒，无模型网络调用。基线与候选的共享公开语料字节相同，指纹见 `results/environment.json`。

**R10 负载与计时。** 使用与上次相同的仓库外适配方法，命令 `uv run python R/scripts/benchmark_authenticated.py --default-config --out R/r10-01`。每个规模各自新进程，100 入口（50 个 entry_only）、所有记忆带入口来源、200 目标、persona 与当前状态；2048 维 float32。每种查询先预热 5 次再测 60 次。通过 ASGI TestClient 执行带 all 令牌的真实 HTTP 路由鉴权，未提高令牌限流；请求节奏等待不计入样本。查询向量预计算，召回判断经真实 Gateway 接即时假 HTTP 应答，计入本地处理成本，不计外部模型网络延迟；这里不是 TCP 网络或模型服务延迟基准。

| 记忆数／查询 | HTTP prepare P95 ms | 直接 prepare P95 ms | 纯向量 P95 ms |
| --- | ---: | ---: | ---: |
| 5,000／不点名 | 80.65 | 56.79 | 4.10 |
| 5,000／点名 | 122.81 | 108.57 | 4.08 |
| 50,000／不点名 | 220.54 | 213.93 | 32.79 |
| 50,000／点名 | 274.30 | 252.16 | 33.25 |

5 千组加载 0.138 秒，测量前后 load average（1／5／15 分钟）为 8.59／4.70／3.59 → 6.58／4.49／3.54；5 万组加载 0.561 秒，6.16／4.50／3.56 → 4.02／4.19／3.51。整项墙钟 96.44 秒。RSS、逐样本耗时、返回 ID 和判断状态保留于原始 JSON；本项运行时本执行会话没有并行测试或模型评测。

**端到端与双判。** 执行等价 `iris eval e2e --judge-mode external --out R/e2e-01` 的仓库外保留材料包装器 `scripts/e2e_preserve.py`，唯一适配是保留原本会自动删除的服务临时目录，以便审计调用和服务日志。11 个脚本全部到达，共 12 检查点。真实运行内部 242.23 秒，命令墙钟 243.10 秒；80 次调用（学习 18、记忆 embedding 20、查询 embedding 30、召回判断 12），0 失败、0 次 429、0 超时、0 降级。学习批次耗时 P50 6.548 秒、P95 22.284 秒、最长 43.145 秒。对话模型 glm-5.3-flash，high；embedding 模型 doubao-embedding-vision。调用上报输入 86,145、输出 9,733 token（其中推理 6,999）。

两轮分别由全新、`fork_turns=none` 的 `gpt-6-astra`／max 子代理 `final_e2e_amber`、`final_e2e_cobalt` 执行，只共享同一冻结材料与评分说明，不读源码、旧判分或另一轮目录。采用 `iris eval e2e-score` 的两次 `--judgments`，`--judge-model 'gpt-6-astra (max)'`；正向事实 AND、禁止项 OR，确定性基础设施检查由原计分器保留。E001—E011 各自 passed=true；双判分歧 0/26，judge_inconsistencies 为空。材料在 `e2e-01/judging-materials-20261010T200123412256Z/`，独立判分在 `e2e-judgments-amber/`、`e2e-judgments-cobalt/`。

**隐私与过度过滤。** 命令 `uv run python evals/visibility_eval/run.py --corpus evals/visibility_v1.json --judge --http-search --embedding-cache <上一轮公开向量缓存> --out R/visibility-01`。默认混合检索，召回判断开启，61 个 search 经带令牌 HTTP、38 个 prepare；真实新判断 18 次，其余 prepare 无需网络调用，全部状态 applied。复用的是公开语料 embedding 缓存，未复用判断；缓存副本及 SHA-256 已留存于 `cache/visibility-embeddings.db` 和 `results/visibility-cache-copy.json`。墙钟 51.52 秒，判断调用合计 44.390 秒；0 失败、429、超时或降级。

99 查询共检查 74 个禁止返回项，泄漏总数 0，include_goals 违规 0；6 个 no_merge 对经 merge_exclusion 与独立快照上的 _plan_pair 检查，误合并 0；4 次可见范围变更完成。可见记忆诊断命中 93/94（98.94%），目标 18/18；唯一未命中为 VS019/q1 的 m1，被召回判断过滤，属于过度过滤诊断，不计隐私泄漏。逐案例／逐查询返回、禁止项和漏召原因见 `visibility-01/report.json`，模型调用审计见同目录 `model-call-audit.json`。

**入口画像（仅诊断）。** 默认运行命令 `uv run python -m evals.entry_profile_eval run --corpus evals/entry_profile_v1.json --out R/entry-profile-02`，六组共 456 条公开消息，完整有效运行 699.67 秒。12 次调用（生成 6、检查 6）均成功，无 429、超时、暂停或失败；调用总耗时 697.702 秒，P50 60.023 秒、P95 85.015 秒。上报输入 63,781、输出 39,162 token（其中推理 34,528）；自动检查共删除 8/46 句（17.39%）。5 组发布，1/6 拒绝（16.67%），拒绝项为 EP001，原因 too_many_deleted。

指令所写 `prompts/entry_profile_scoring_v1.md` 在候选中不存在；运行器实际导出冻结的 `evals/entry_profile_eval/scoring.md`，版本标识仍为 `entry_profile_scoring_v1`，SHA-256 为 `7431053435ad78a462f43aed5fb33d459e36a848d8372e65744aeef20243f661`。两轮均使用该原样导出的评分说明，未修改文件。两位全新 `gpt-6-astra`／max 子代理 `final_profile_amber`、`final_profile_cobalt` 均 `fork_turns=none`，只读同一份材料并写各自目录，不看另一轮、实现或历史结果。正式计分使用 `python -m evals.entry_profile_eval score --materials R/entry-profile-02/entry-profile-20261010T200942-0bede6ee/judging-materials --judgments R/profile-judgments-amber --judgments R/profile-judgments-cobalt --judge-model 'gpt-6-astra (max)' --out R/entry-profile-score`。

分歧按 supported AND、violations 并集、must_cover AND、forbidden OR 合并。可用画像的模型句有依据 30/34（88.24%）；手写管理员句 0，不进入自动句分母。可用画像 forbidden 命中 0；除“无依据”之外的违规为：单条消息泛化 4、个人信息或隐私 1。must_cover 覆盖 14/40（35.00%），分母保留所有六组的要点。被拒绝候选的违规单列为：无依据 2，不计入可用画像依据率和禁止项汇总。这里只报告诊断，不将计分器中的阈值布尔值用作上线门槛。依据不足定位为 EP004 第 6 句、EP005 第 1／4／5 句；单条泛化为 EP002 第 6 句、EP004 第 7 句、EP005 第 4／8 句；个人信息违规为 EP003 第 6 句，两轮对此一致。forbidden 列表不是禁止内容的穷举，因此清单命中为 0 不能解释为画像无违规。

| 案例 | 状态 | 模型句有依据 | must_cover | forbidden 命中 | 句子违规（含无依据） |
| --- | --- | --- | --- | --- | --- |
| EP001 | rejected | 2/4（拒绝项，单列） | 0/8 | 0 | 无依据 2 |
| EP002 | published | 6/6 | 2/7 | 0 | 单条消息泛化 1 |
| EP003 | published | 6/6 | 5/6 | 0 | 个人信息或隐私 1 |
| EP004 | published | 7/8 | 3/5 | 0 | 无依据 1、单条消息泛化 1 |
| EP005 | published | 5/8 | 2/7 | 0 | 无依据 3、单条消息泛化 2 |
| EP006 | published | 6/6 | 2/7 | 0 | 0 |

双判有 6 个字段判定分歧；逐项如下，均由计分器取不利结果，未人工覆盖。

| 案例 | 字段／序号（从 1 开始） | amber | cobalt |
| --- | --- | --- | --- |
| EP004 | `sentence_results.supported` #6 | true | false |
| EP004 | `sentence_results.violations` #6 | [] | 无依据 |
| EP004 | `must_cover` #5 | true | false |
| EP005 | `sentence_results.supported` #5 | false | true |
| EP005 | `sentence_results.violations` #5 | 无依据 | [] |
| EP005 | `must_cover` #3 | true | false |

冻结材料及原始输出在 `entry-profile-02/entry-profile-20261010T200942-0bede6ee/`；判分在 `profile-judgments-amber/`、`profile-judgments-cobalt/`；双判完整理由及合并报告在 `entry-profile-score/entry-profile-score-20261010T202832-7018bf58.json`。

**实际检查与边界。** `uv run python R/scripts/actual_checks.py`，墙钟 315.28 秒。在全新隔离目录先用基线真实服务，经 HTTP 建 2 个入口、3 条消息、1 条初始背景记忆和 1 个目标；正常停服后由候选启动同一目录。迁移从 024 至 025_launch_runtime、026_entry_profile，自动生成的迁移前备份确实仍为 024 且保留原数据。业务表及关系逐字段比较 0 差异；integrity_check=ok、foreign_key_check 为空。本项只核查迁移与机制，未调用真实模型，不把手工初始记忆作为学习质量成绩。

使用两条手写的机制测试画像分别在 A／B 入口发布，确认 prepare 仅返回当前入口的已发布画像，尚未发布时 entry_profile 为空；只授权 A 的令牌 prepare B 得统一错误格式的 403；search 不返回入口画像。该机制检查与上述真实模型画像质量分开记录。

经管理 HTTP 触发一次每日维护，生成不含密钥的备份，记录 `purpose=scheduled`、`trigger=scheduled`、`status=succeeded`。正常 SIGTERM 停服后，设置 keep=2，并只在仓库外驱动注入后两日的 Maintenance.clock，通过原维护路径各触发一次：共三次成功记录，仅保留最新两个归属本数据目录的 scheduled ZIP。此保留检查使用时钟注入，未冒充跨三天实跑；第一次备份来自实际运行中的维护请求。ZIP 均为 0600，清单无密钥。候选进程记录启动 1 次、心跳 3 次、停止 1 次，其中实际等待到的周期心跳间隔 300.013 秒。生成的测试密码和令牌仅在内存用于操作与日志匹配，日志命中 0；未输出这些值。材料为 `actual/summary.json`、`scheduled-retention.json`、`runtime-events.json`、`runtime-summary.json` 和升级前后快照。

**插件。** 原插件仓库只读，独立克隆在 `R/plugin/`，检出上述 dev.4 提交。安装后执行 `IRIS_CORE_PATH=W uv run --locked python -m pytest -q`（另将 pytest cache 与 basetemp 指向材料目录）：319 passed，内部 48.28 秒、墙钟 48.83 秒。真实 iris-core 集成不是跳过项，结果记录的 core_head 与候选一致；覆盖接收／去重／prepare／persona／search／feedback／learn／目标／提醒及完成动作，模型调用预期为 0。未修改插件文件；原 README 控制台 pytest 入口的复现问题单列于作废表。

**作废的运行与替代。** 未按分数选择运行；只有下表中的基础设施／执行失败作废。其余有效真实运行中 429、超时、暂停和降级均为 0。画像产品检查正常拒绝的 EP001 保留在有效运行中，不视为基础设施失败。

| 运行 | 现象／最小复现 | 处理与材料 |
| --- | --- | --- |
| recall-regression（无真实模型，5.47 秒） | 外部适配器误用 `POST /api/v1/search`，基线 Q38 返回 404；正确路由为 `/api/v1/memories/search` | 只修正仓库外驱动并完整重跑，recall-regression-02 为有效结果；原驱动 `scripts/recall_regression_invalid01.py`、原材料 `recall-regression-invalid01/`、日志均保留 |
| plugin-tests（无真实模型，1.59 秒） | 在 dev.4 新克隆中按 README 使用 `IRIS_CORE_PATH=W uv run --locked pytest -q`，收集阶段 `ModuleNotFoundError: tests`，退出码 4，未执行测试 | 改用 `uv run --locked python -m pytest` 运行相同全套测试，319 passed；未改插件，见 `logs/plugin-tests.log`、`logs/plugin-tests-02.log` |
| entry-profile-01（真实模型，321.93 秒后停止） | EP002 的生成调用在固定 120 秒预算耗尽，记录 `retryable`、`timed_out=1`、120,007 ms；发现时 EP003 已开始 | 整轮作废并停止，未使用任何质量分数；不改默认配置／预算／语料，从全新目录完整重跑六组；有效替代 entry-profile-02。失败状态、调用与数据库保留在 `entry-profile-01/invalid-run.json` 和其子目录 |

作废清单：`results/invalid-runs.json`。其中真实模型作废 1 轮；有效入口画像重跑 699.67 秒，12 次调用均成功。首次固定预算超时是本次观察到的运行波动，未通过延长预算消除记录。

**材料与报告范围。** 全量原始输出、服务数据库、判分和适配脚本只留在 `R`，仓库提交仅本报告。`results/judging-protocol.json` 保存判分隔离约定；各真实运行的 `model-call-audit.json` 保存失败／超时／降级核查。公开样本、一次性能测量、小规模升级和时钟注入保留检查的范围不能外推为长期真实负载结论；未额外运行 24 小时模拟。上述过度过滤、画像诊断和插件命令差异供规划者审阅，本报告不作“可以上线”的判断。
