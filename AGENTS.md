# Iris 后续实现说明

产品行为以 `companion_memory_cognition_system_design_integrated.md` 和 `DECISIONS.md` 为准。当前代码完成 M1 学习核心及第四轮召回、回复准备、查询与反馈接口；后台调度、HTTP 学习触发、界面、首次设置和鉴权尚未实现。不要复制 `dev-0`、`dev-1` 分支的代码。

2026-10-05 起，开发与评测的对话模型改为 glm-5.3-flash，开发设备改为 macOS。MiniMax-M3 时期的评测结果全部作废，仓库里暂无有效报告；重测计划见 `evals/README.md`。

## 分工

规划者（监督者）负责规划、审查和文档，并在仓库外维护隐藏验收集；执行者负责代码、测试和评测。每个执行任务在自己的 git worktree 中进行。可以同时有多个执行会话。同时修改产品代码（`src/`、`tests/`、迁移和提示词）的会话，改动的文件不能重叠；有重叠就排队，由规划者安排顺序。其余会话只运行评测、整理报告，或做互不重叠的配置。

评测判分由执行者所用的模型按评分说明完成，不调用对话模型；在判分通道合入之前，`iris eval learning` 内置的对话模型判分只作预览，不作门槛依据。两次判分在互不可见的独立会话中完成，第二次不能看到第一次的结果；用于门槛判定的判分，由没有参与该轮代码和提示词修改的执行会话完成。目前只在 macOS 上验证，暂不做 Windows 验证。

## 安装和运行

需要 uv 和 Python 3.12 及以上。以下命令在 macOS、Linux 和 Windows PowerShell 中相同：

```bash
uv sync --python 3.13
uv run pytest
uv run iris --help
uv run iris serve
```

仓库内的 `.venv` 使用 Python 3.13。提 PR 前另用临时环境跑一次 3.12：

```bash
uv run --locked --isolated --python 3.12 pytest
```

不要省略 `--isolated` 直接写 `--python 3.12`，否则 uv 会把 `.venv` 重建成 3.12。两个版本的测试共用 `.pytest-tmp`，不要同时运行。

测试模型配置可放在当前目录的 `test-models.toml`，或用环境变量 `IRIS_TEST_MODELS` 指向绝对路径；在其他 worktree 中工作时用后者指向同一份配置，不要复制含密钥的文件。参见 `test-models.example.toml`。验证连接及运行真实评测：

```bash
uv run iris models check
uv run iris eval learning --split dev --judge-runs 1
uv run iris eval learning --judge-runs 2
uv run iris eval recall
uv run iris eval recall --split dev --calibrate
uv run iris eval learning --corpus <外部 JSONL> --out <外部目录>
```

`evals/learning_v1.jsonl` 的 36 段全部为 dev；`learning_v2.jsonl` 的 16 段历史 holdout 已在 PR #2 分析，依 2026-09-29 决定只具回归意义，报告中单列但不作 holdout 判定。`learning_v3.jsonl` 的 10 段手写 dev 和 `scoring_v3.md` 在学习提示词 v4 前单独提交冻结，概况与边界修订理由见 `evals/learning_v3_notes.md`。GLM 阶段全部公开样本按 dev 使用。不要为了达标修改样本或放宽评分；真实标注错误须逐条说明理由。评测按入口真实节奏分批，最终每段判两次，分歧取不利结论；迭代允许 --judge-runs 1，不能把单判当成最终双判结果。

默认评测读取三份仓库 JSONL，报告写入 `evals/reports/`。`--corpus` 替换输入集，`--out` 替换报告目录，均支持仓库外路径。隐藏验收集由规划者维护，执行者不得寻找或读取；最终门槛须隐藏集和全部公开样本同时达到，不能仅凭仓库结果宣称 M1 最终通过。报告单列长度截断批次、别名覆盖率／精确率及双判分歧。

学习提示词使用 v5，学习和判分输出上限均为 16000 token。学习请求总超时 120 秒，评测判分 240 秒，报告标明两者。`model_calls` 记录 finish_reason、输出用量和所属批次。校验前做确定性规整：计划立场、数字 M 引用、单元素名字数组和无效 derived_from；通过证据校验的自身亲历／观点补齐 about 中的“我”；推断只在实际涉及我时由学习输出包含“我”，不再无条件补齐。正文和 tags 在校验后消除批次 P 编号，禁止推断性别，不同事实分开。原始输出及规整记录都保留。消息／引用作者显示参与者编号，归属用主体 ID 校验。同名账号不合并；本人别名写 `subject_aliases`，未知同一人用 `same_as`，虚构扮演用 `roleplay`。

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

不能在运行评测时切换或重建同一虚拟环境，也不要在模型评测运行期间修改源码或迁移。完整学习案例按源码／语料／模型／判分次数指纹保存在忽略的 data/learning-checkpoints；外部 --out 的检查点与完整明细留在外部。只复用相同输入的完成案例，不按分数选择或改写结果。

main 覆盖召回验收 R01、R04—R13；R02、R03、R14 经用户确认留到 M4。可见范围和宿主令牌在 M4，自动遗忘和恢复在 M2，未到对应阶段不要添加。合成性能数据由 `evals/benchmark_retrieval.py` 生成，不属于质量评测语料。
