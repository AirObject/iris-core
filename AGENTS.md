# Iris 后续实现说明

产品行为以 `companion_memory_cognition_system_design_integrated.md` 和 `DECISIONS.md` 为准。当前代码完成 M1 学习核心、回复准备、查询与反馈接口及第五轮召回质量改进；后台调度、HTTP 学习触发、界面、首次设置和鉴权尚未实现。不要复制 `dev-0`、`dev-1` 分支的代码。

2026-10-05 起，开发与评测的对话模型改为 glm-5.3-flash，开发设备改为 macOS。MiniMax-M3 时期的评测结果全部作废；GLM 重测计划及新设备召回复现见 `evals/README.md`。

## 分工

规划者（监督者）负责规划、审查和文档，并在仓库外维护隐藏验收集；执行者负责代码、测试和评测。每个执行任务在自己的 git worktree 中进行。可以同时有多个执行会话。同时修改产品代码（`src/`、`tests/`、迁移和提示词）的会话，改动的文件不能重叠；有重叠就排队，由规划者安排顺序。其余会话只运行评测、整理报告，或做互不重叠的配置。

评测判分由执行者所用的模型按评分说明完成，不调用对话模型；`iris eval learning` 默认保留内置对话模型判分，只作预览，不作门槛依据。两次判分在互不可见的独立会话中完成，第二次不能看到第一次的结果；用于门槛判定的判分，由没有参与该轮代码和提示词修改的执行会话完成。目前只在 macOS 上验证，暂不做 Windows 验证。

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
uv run iris eval learning --judge-mode external --out <运行目录>
uv run iris eval learning-export --checkpoints <检查点指纹目录> --checkpoint-report <同次报告.json> --out <材料目录>
uv run iris eval learning-score --materials <材料目录> --judgments <第一轮目录> --judgments <第二轮目录> --judge-model <执行者模型名称> --out <报告目录>
```

外部模式只学习并导出 `judging-materials-<时间>/`，不调用对话模型判分。已有学习用 `learning-export` 离线导出；旧检查点需同次报告以保留真实来源，新检查点有 `metadata.json` 可省略 `--checkpoint-report`。每轮目录将 `round-template.json` 复制为 `manifest.json`，按清单 `judgment_file` 保存每案例的 scoring_v3 JSON；严格核对数组长度、布尔类型与记忆 ID／顺序，理由可附但不计分。`learning-score` 的一次／两次 `--judgments` 决定轮数，`--judge-model` 必填。两轮只共享评分说明和材料，不得互看结果；格式、隔离方式和例子见 `evals/README.md`。默认模型判分及 `--judge-runs` 只作预览。材料与报告优先放仓库外，完整材料不提交。

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
- `src/iris/learning_retrieval.py`：独立的学习材料检索，不继承回复调参。
- `src/iris/api.py`：FastAPI 宿主接口；serve 只绑定回环地址。
- `src/iris/evaluation.py`、`recall_evaluation.py`、`evals/`：隔离数据库的学习和召回评测。
- `tests/`：确定性逻辑和假模型集成测试。

## 密钥与工作规则

`test-models.toml` 含 API key，必须保持在 `.gitignore` 中。不要把密钥写入代码、测试、报告、日志、终端输出、提交或 PR 描述。每次提交前运行 `git check-ignore test-models.toml`、查看 `git diff --cached`，并核查暂存内容不含密钥。若发现密钥已提交或推送，立即停止并通知用户更换，不改写历史。

只在功能分支提交和推送，不直接修改 main，不 force push、不合并 PR。新增行为先有确定性测试或假模型集成测试，再用真实评测检查效果。模型调用必须在数据库事务之外；记忆正文和判断的修改需校验修订号，保留强度使用原子增量，不加修订号。文件和 HTTP JSON 都使用 UTF-8。

## 召回与验证

默认召回评测读取 recall_v1、recall_v2、recall_conversation_v1、recall_short_terms_v1 四份公开语料，共 139 条固定记忆、144 条查询，各自隔离入库。v1 在 `a8af9b1` 冻结，v2 在 `95549fc` 冻结，对话集在 `15cdd48` 冻结，短词集在 `830efb6` 冻结；来源与格式见 `evals/README.md`，语料及 notes 保持原样。禁止用脚本或模板生成质量语料。GLM 阶段全部公开样本按 dev 使用；文件中的原 split 仅作历史分组，不代表未见验收。隐藏验收集由规划者维护，执行者不得寻找或读取。

`iris eval recall --corpus <JSON 或 JSONL> --out <目录>` 支持外部集。`--calibrate` 只允许 `--split dev`，报告完整网格，不修改在用数据库。选参先比较 `Q=(Recall@8+nDCG@8)/2`，与全网格最佳相差不足 0.01 视为持平，再比较 relevant 标注精确率、较低无关误返率，随后比较 Q，完全相同保留网格顺序；不逐项链式平分。PR #5 原选参使用冻结 split 的 106 条 dev，排除 v2 已分析的 22 条历史 holdout。新设备第一阶段已复现；PR #5 第四轮加入 16 条新 dev，第五轮继续用全部 122 条 dev 重新标定，原 v2 的 22 条历史 holdout 仍不参与选择。

回复准备先选相关记忆，再按参与者最近发言顺序轮流补人物要点，宿主显式顺序优先，要点合计最多三条，排除 self／scene。每条有 reason：相关项为 relevant，要点为 person_highlight；search 不补人物要点。参与者只轻度加权，不排除其他人；文本点名（含别名）只保留 about／speaker／正文提及该主体的记忆，同名主体都算锚点。点名检查仅对全文匹配、达到向量绝对下限和人物要点的候选按 ID 读取正文并缓存；锚点筛选先于 200 条截断，相对下限取被点名范围内的最佳余弦。不得用属性或话题词表判断答案；回复 QUERY_STOP 只保留通用语法词。

trigram 查询中的一字／两字实词同时查已有 jieba 索引，与三字窗口候选按名次轮流合并、去重，共用 200 条全文候选上限。只有已知姓名、没有剩余话题词时，以涉及人／说话人关系及姓名／别名的全文短语取候选；正文仍校验最长标签和词边界。所有路径保留结构过滤，不因存在短词而退化为正文全表扫描。已有通用停用词允许跨 jieba 分词单元识别，不新增词表，不改变入库或学习分词。全文入选检查全查询的完整词项覆盖率，短词补查也以全部长短词为分母；重叠子词不重复计票。三个字符以上的稀有词片段可按整库文档频率入选，最多读取 lexical_max_df+1 个 rowid 判断，一／两字词不能绕过覆盖率。原三集和对话中准备须分别守住 Recall、误返最多增加一条、relevant 精确率降幅不超过 0.03。

学习材料由 learning_retrieval.py 独立选取，保持 PR #4 设置：jieba 词项覆盖 0.5、向量下限 0.65、RRF k=60 等权、无问题前缀或相对截断、不按锚点或参与者排除，人物要点和人物加权只使用本批发言人，不额外加入正文提及者。专用 runtime_settings.learning_retrieval 不读回复标定设置；以后修改先用学习评测证明质量不降。按 2026-10-06 决定，第一阶段真实学习对照已作废；只改召回时，用 evals/compare_learning_requests.py 在 main 和本分支上逐批比较全部 86 段公开语料的假模型学习请求。

回复默认 trigram、RRF k=60、2048 维 float32，查询前缀为“为这个问题检索能回答它的个人记忆：”，向量绝对下限 0.35、相对比例 0.75、向量权重 1／全文权重 1；全文覆盖率 0.75、长片段最大文档频率 2。无向量时降级 trigram，并保留 jieba 短词补查。完整参数见 `src/iris/retrieval_defaults.json`。新数据库写入 runtime_settings.retrieval，已有设置保留。更换模型或维度须重新标定，未标定退回全文。SQLite 存 float32，内存默认 float32。索引只在事务提交后更新；评分用不可变向量块快照，不持数据库写锁。读准备／查询只写召回记录，不增加保留强度。

不能在运行评测时切换或重建同一虚拟环境，也不要在模型评测运行期间修改源码或迁移。完整学习案例按源码／语料／模型端点、ID、维度／判分次数指纹保存在忽略的 `data/lc/<16位指纹>`，meta.json 校验完整 SHA-256；外部 --out 的检查点在 `<out>/.lc/<16位指纹>`，完整明细也留在外部。只复用相同输入的完成案例，不按分数选择或改写结果。

本分支覆盖召回验收 R01、R04—R13；R02、R03、R14 经用户确认留到 M4。可见范围和宿主令牌在 M4，自动遗忘和恢复在 M2，未到对应阶段不要添加。合成性能数据由 `evals/benchmark_retrieval.py` 生成，不属于质量评测语料；加 `--default-config` 测量当前默认的 5 千／5 万条、点名／不点名四组 prepare。
