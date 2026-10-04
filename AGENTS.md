# Iris 后续实现说明

产品行为以 `companion_memory_cognition_system_design_integrated.md` 和 `DECISIONS.md` 为准。当前代码完成 M1 学习核心、回复准备、查询与反馈接口及第五轮召回质量改进；后台调度、HTTP 学习触发、界面、首次设置和鉴权尚未实现。不要复制 `dev-0`、`dev-1` 分支的代码。

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

`evals/learning_v1.jsonl` 的 36 段全部为 dev；`learning_v2.jsonl` 的 16 段历史 holdout 已在 PR #2 分析，依 2026-09-29 决定只具回归意义，本轮保留原 split 作最终对照。`learning_v3.jsonl` 的 10 段手写 dev 和 `scoring_v3.md` 在学习提示词 v4 前单独提交冻结，概况与边界修订理由见 `evals/learning_v3_notes.md`。迭代只运行 dev。PR #5 第二轮按用户要求，最终代码固定后全部学习集双判独立运行两次；历史 holdout 仅作回归，不作为未见验收。不要为了达标修改样本或放宽评分；真实标注错误须逐条说明理由。评测按入口真实节奏分批，最终每段判两次，分歧取不利结论；迭代允许 --judge-runs 1，不能把单判当成最终双判结果。

默认评测读取三份仓库 JSONL，报告写入 `evals/reports/`。`--corpus` 替换输入集，`--out` 替换报告目录，均支持仓库外路径。隐藏验收集由规划者维护，执行者不得寻找或读取；最终门槛须隐藏集和仓库评测同时达到，不能仅凭仓库结果宣称 M1 最终通过。报告单列长度截断批次、别名覆盖率／精确率及双判分歧。

学习提示词使用 v5，学习和判分输出上限均为 16000 token。学习请求总超时本轮仍为 120 秒（已决定的 180 秒改动留到调度 PR）；评测判分因真实请求连续超时单独延长到 240 秒，报告标明两者。`model_calls` 记录 finish_reason、输出用量和所属批次。校验前做确定性规整：计划立场、数字 M 引用、单元素名字数组和无效 derived_from；通过证据校验的自身亲历／观点补齐 about 中的“我”；推断只在实际涉及我时由学习输出包含“我”，不再无条件补齐。正文和 tags 在校验后消除批次 P 编号，禁止推断性别，不同事实分开。原始输出及规整记录都保留。消息／引用作者显示参与者编号，归属用主体 ID 校验。同名账号不合并；本人别名写 `subject_aliases`，未知同一人用 `same_as`，虚构扮演用 `roleplay`。

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

recall_v2 在 `95549fc` 单独冻结，历史 22 条 holdout 已分析。第二轮新增 `15cdd48` 手写冻结的 recall_conversation_v1：33 条记忆、24 条查询、6 条无答案，查询和参与者均为 null，含私聊／群聊／直播。默认三个公开集合各自隔离入库，全部按 dev 合并标定；原文件 split 保留作来源记录，不新增公开 holdout。验收以规划者的隐藏集为准。

回复准备先选相关记忆，再按参与者最近发言顺序轮流补人物要点，宿主显式顺序优先，要点合计最多三条，排除 self／scene；每条有 reason，相关项为 relevant，要点为 person_highlight。search 不补人物要点。参与者只轻度加权，不排除其他人；文本点名（含别名）只保留 about／speaker／正文提及该主体的记忆，同名主体都算锚点。不得重新引入属性或话题词表判断答案。QUERY_STOP 只留通用语法词，删词清单见 PR5_EXECUTION。

学习材料由 learning_retrieval.py 独立选取，默认恢复 PR #4：jieba 词项覆盖 0.5、向量下限 0.65、无问题前缀或相对截断、不按锚点或参与者排除，人物要点仍含本批参与者及提及者。专用 runtime_settings.learning_retrieval 不读回复标定设置；以后修改先用学习评测证明质量不降。

回复默认 trigram＋2048 float32 向量，有前缀、绝对下限 0.45、相对比例 0.75、向量权重 2／全文权重 1；无向量时降级 jieba，保留短词检索。完整参数见 retrieval_defaults.json 与本轮报告。新数据库写入 runtime_settings.retrieval；已有设置保留。更换模型或维度须重新标定，未标定退回全文。SQLite 存 float32，内存默认 float32。索引只在事务提交后更新；评分用不可变向量块快照，不持数据库写锁。读准备／查询只写召回记录，不增加保留强度。

Python 3.12 和 3.13 使用独立环境测试，不能在运行评测时切换或重建同一虚拟环境，也不要在模型评测运行期间修改源码或迁移。完整学习案例按源码／语料／模型与维度／判分次数指纹保存在忽略的 data/lc/<16位短名>，meta.json 校验完整 SHA-256；外部 --out 的检查点与完整明细留在外部。只复用相同输入的完成案例，不按分数选择或改写结果。

本 PR 覆盖 R01、R04—R13；用户已确认 R14 按设计留到 M4，R02、R03 同样留到 M4。不要在本轮添加可见范围、宿主令牌、自动遗忘和恢复。合成性能数据由 `evals/benchmark_retrieval.py` 生成，不属于质量评测语料。
