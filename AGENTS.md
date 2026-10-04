# Iris 后续实现说明

产品行为以 `companion_memory_cognition_system_design_integrated.md` 和 `DECISIONS.md` 为准。当前代码完成 M1 学习核心、召回与宿主接口，以及后台调度、模型用途暂停恢复、向量补算、HTTP 立即学习和端到端评测；界面、首次设置、设置页、secrets.json 和鉴权尚未实现。不要复制 `dev-0`、`dev-1` 分支的代码。

## 安装和运行

需要 uv 和 Python 3.12 及以上。Windows PowerShell：

```powershell
uv sync
uv run pytest
uv run iris --help
uv run iris serve
```

测试模型配置可放在当前目录的 `test-models.toml`，或用环境变量 `IRIS_TEST_MODELS` 指向绝对路径。参见 `test-models.example.toml`。验证连接及运行真实评测：

```powershell
uv run iris models check
uv run iris eval learning --split dev --judge-runs 1
uv run iris eval learning --judge-runs 2
uv run iris eval recall
uv run iris eval recall --split dev --calibrate
uv run iris eval learning --corpus C:\path\to\cases.jsonl --out C:\path\to\reports
```

`evals/learning_v1.jsonl` 的 36 段全部为 dev；`learning_v2.jsonl` 的 16 段历史 holdout 已在 PR #2 分析，依 2026-09-29 决定只具回归意义，本轮保留原 split 作最终对照。`learning_v3.jsonl` 的 10 段手写 dev 和 `scoring_v3.md` 在学习提示词 v4 前单独提交冻结，概况与边界修订理由见 `evals/learning_v3_notes.md`。迭代只运行 dev，历史 holdout 本轮只在最后运行一次。不要为了达标修改样本或放宽评分；真实标注错误须逐条说明理由。评测按入口真实节奏分批，最终每段判两次，分歧取不利结论；迭代允许 --judge-runs 1，不能把单判当成最终双判结果。

默认评测读取三份仓库 JSONL，报告写入 `evals/reports/`。`--corpus` 替换输入集，`--out` 替换报告目录，均支持仓库外路径。隐藏验收集由规划者维护，执行者不得寻找或读取；最终门槛须隐藏集和仓库评测同时达到，不能仅凭仓库结果宣称 M1 最终通过。报告单列长度截断批次、别名覆盖率／精确率及双判分歧。

学习提示词使用 v5，学习和判分输出上限均为 16000 token。学习请求（含调用内重试和 JSON 修正）共享 180 秒总预算；其他生成 120 秒、embedding 30 秒、召回查询 embedding 2 秒、评测判分 240 秒，报告和状态接口标明超时。`model_calls` 记录 finish_reason、输出用量和所属批次。校验前做确定性规整：计划立场、数字 M 引用、单元素名字数组和无效 derived_from；通过证据校验的自身亲历／观点补齐 about 中的“我”；推断只在实际涉及我时由学习输出包含“我”，不再无条件补齐。正文和 tags 在校验后消除批次 P 编号，禁止推断性别，不同事实分开。原始输出及规整记录都保留。消息／引用作者显示参与者编号，归属用主体 ID 校验。同名账号不合并；本人别名写 `subject_aliases`，未知同一人用 `same_as`，虚构扮演用 `roleplay`。

## 目录

- `src/iris/db.py`、`migrations/`：SQLite 单写连接、只读快照、顺序迁移与备份。
- `src/iris/queue.py`：入口、消息、节奏判断与冻结批次。
- `src/iris/models.py`：OpenAI 兼容模型网关与调用记录。
- `src/iris/learning.py`、`prompts/`：材料、校验、写入、学习提示词和评分说明。
- `src/iris/memory_ops.py`：初始设定和数据层记忆操作。
- `src/iris/retrieval.py`、`vector_index.py`、`search_text.py`：FTS5、numpy 向量、回复准备和反馈。
- `src/iris/api.py`：FastAPI 宿主接口；serve 只绑定回环地址。
- `src/iris/evaluation.py`、`recall_evaluation.py`、`evals/`：隔离数据库的学习和召回评测。
- `tests/`：确定性逻辑和假模型集成测试。

## 密钥与工作规则

`test-models.toml` 含 API key，必须保持在 `.gitignore` 中。不要把密钥写入代码、测试、报告、日志、终端输出、提交或 PR 描述。每次提交前运行 `git check-ignore test-models.toml`、查看 `git diff --cached`，并核查暂存内容不含密钥。若发现密钥已提交或推送，立即停止并通知用户更换，不改写历史。

只在功能分支提交和推送，不直接修改 main，不 force push、不合并 PR。新增行为先有确定性测试或假模型集成测试，再用真实评测检查效果。模型调用必须在数据库事务之外；记忆正文和判断的修改需校验修订号，保留强度使用原子增量，不加修订号。文件和 HTTP JSON 都使用 UTF-8。

## 召回与验证

`recall_v1.json` 是单独冻结的 40 条固定记忆、38 条手写 dev 查询（8 条无答案）；禁止用脚本或模板生成质量语料。`iris eval recall --corpus <JSON 或 JSONL> --out <目录>` 可用于规划者的外部集，格式见 evals/README。`--calibrate` 只允许 `--split dev`，报告参数选择，不修改在用数据库；不要在隐藏集上调参。

默认 jieba、RRF k=60、2048 维 float32，文本覆盖阈值 0.5、向量余弦阈值 0.65，按当前 doubao-embedding-vision dev 标定。初次打开数据库将 `retrieval_defaults.json` 写入 `runtime_settings.retrieval`；保留已有设置。更换模型必须重新标定，未经标定退回全文检索。SQLite 存 float32；内存 dtype 可配置 float16，但本机实测更慢。索引更新只能在事务提交后，不能在写事务里全表读取记忆和向量。读准备／查询只写召回记录，不增加保留强度。

Python 3.12 和 3.13 使用独立环境测试，不能在运行评测时切换或重建同一虚拟环境，也不要在模型评测运行期间修改源码或迁移。完整学习案例按源码／语料／模型／判分次数指纹保存在忽略的 data/learning-checkpoints；外部 --out 的检查点与完整明细留在外部。只复用相同输入的完成案例，不按分数选择或改写结果。

本 PR 覆盖 R01、R04—R13；用户已确认 R14 按设计留到 M4，R02、R03 同样留到 M4。不要在本轮添加可见范围、宿主令牌、自动遗忘和恢复。合成性能数据由 `evals/benchmark_retrieval.py` 生成，不属于质量评测语料。

## 调度与端到端（M1-6）

服务启动先恢复中断批次，默认同时学习两个批次，同入口串行；学习状态以 batches 为准。手动请求的消息位置持久保存在 entries，向量补算由 memories 的缺失向量推导。暂不建立通用任务表，梦境整理阶段再引入。接收后不能假设消息已经被学习；等待状态接口的批次结果。

模型健康按 chat／embedding 分开并持久保存，连续三次可重试的网络请求错误（包括调用内重试）暂停用途；触发暂停的批次失败不扣尝试次数。探测间隔 60／120／240／480／600 秒。401／403 与 404 分别为密钥无效、配置错误，只在配置变化后恢复。每日 token 上限默认不限，按角色时区恢复；在途调用及服务商未报告的 token 无法事先扣减。模型配置仍只从 test-models.toml／IRIS_TEST_MODELS 加载，不把凭据存入数据库。

内部设置接口为 Gateway.replace_config、Gateway.retry_now、ModelHealth.set_daily_token_limit、Scheduler.set_concurrency；后续设置页调用它们。serve 自动重新读取模型配置。离线 iris learn 必须停止服务后运行，服务／离线命令用操作系统锁互斥。模型网络调用仍在事务外，向量补算写回必须核对修订号；不要改变 learning_context 的选材。

新增 `uv run iris eval e2e --judge-runs 2 --out C:\eval-results\e2e`。11 个脚本为手写 dev（原 10 个加单独冻结的跨入口 E011），评分文件 e2e_scoring_v1.md 独立于学习评分。评测使用真实 serve 子进程，只经 HTTP 接收、观察学习和准备回复，强制重启后才提问。Windows 需结束虚拟环境解释器启动的整个子进程树。默认双判、分歧取不利结论；单判仅用于迭代。完整返回只写到仓库外 --out，隐藏集由规划者运行。仓库通过比例至少 80%（当前 9/11）不代表隐藏门槛通过。跨入口来源用 sources 的 entry_id/key 对象，近期原始消息隔离必须独立于模型判分检查。

本轮与 PR #5 并行，禁止修改 retrieval.py、query_analysis.py、search_text.py、vector_index.py、recall_evaluation.py、evals/recall_*、学习提示词和学习评分。PR #5 合并后以 merge 合入 origin/main，重跑全量测试和端到端双判，不 rebase。不在模型评测运行中修改源码或迁移。
