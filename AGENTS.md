# Iris 后续实现说明

产品行为以 `companion_memory_cognition_system_design_integrated.md` 和 `DECISIONS.md` 为准。当前代码完成 M1 学习核心、第二轮学习质量评测及第三轮输出长度／归属／别名修正；检索等后续功能尚未实现。不要复制 `dev-0`、`dev-1` 分支的代码。

## 安装和运行

需要 uv 和 Python 3.12 及以上。Windows PowerShell：

```powershell
uv sync
uv run pytest
uv run iris --help
```

测试模型配置可放在当前目录的 `test-models.toml`，或用环境变量 `IRIS_TEST_MODELS` 指向绝对路径。参见 `test-models.example.toml`。验证连接及运行真实评测：

```powershell
uv run iris models check
uv run iris eval learning --split dev
uv run iris eval learning
uv run iris eval learning --corpus C:\path\to\cases.jsonl --out C:\path\to\reports
```

`evals/learning_v1.jsonl` 的 36 段全部为 dev；`learning_v2.jsonl` 的 16 段历史 holdout 已在 PR #2 分析，依 2026-09-29 决定只具回归意义，本轮保留原 split 作最终对照。`learning_v3.jsonl` 的 10 段手写 dev 和 `scoring_v3.md` 在学习提示词 v4 前单独提交冻结，概况与边界修订理由见 `evals/learning_v3_notes.md`。迭代只运行 dev，历史 holdout 本轮只在最后运行一次。不要为了达标修改样本或放宽评分；真实标注错误须逐条说明理由。评测按入口真实节奏分批，每段判两次，分歧取不利结论。

默认评测读取三份仓库 JSONL，报告写入 `evals/reports/`。`--corpus` 替换输入集，`--out` 替换报告目录，均支持仓库外路径。隐藏验收集由规划者维护，执行者不得寻找或读取；最终门槛须隐藏集和仓库评测同时达到，不能仅凭仓库结果宣称 M1 最终通过。报告单列长度截断批次、别名覆盖率／精确率及双判分歧。

学习提示词使用 v4，学习和判分输出上限均为 16000 token。学习请求总超时仍为 120 秒；评测判分因真实请求连续超时单独延长到 240 秒，报告标明两者。`model_calls` 记录 finish_reason、输出用量和所属批次。校验前做确定性规整：计划立场、数字 M 引用、单元素名字数组和无效 derived_from；通过证据校验的自身亲历／观点／推断补齐 about 中的“我”。原始输出及规整记录都保留。消息／引用作者显示参与者编号，归属用主体 ID 校验。同名账号不合并；本人别名写 `subject_aliases`，未知同一人用 `same_as`，虚构扮演用 `roleplay`。

## 目录

- `src/iris/db.py`、`migrations/`：SQLite 单写连接、只读快照、顺序迁移与备份。
- `src/iris/queue.py`：入口、消息、节奏判断与冻结批次。
- `src/iris/models.py`：OpenAI 兼容模型网关与调用记录。
- `src/iris/learning.py`、`prompts/`：材料、校验、写入、学习提示词和评分说明。
- `src/iris/memory_ops.py`：初始设定和数据层记忆操作。
- `src/iris/evaluation.py`、`evals/`：隔离数据库的真实模型评测。
- `tests/`：确定性逻辑和假模型集成测试。

## 密钥与工作规则

`test-models.toml` 含 API key，必须保持在 `.gitignore` 中。不要把密钥写入代码、测试、报告、日志、终端输出、提交或 PR 描述。每次提交前运行 `git check-ignore test-models.toml`、查看 `git diff --cached`，并核查暂存内容不含密钥。若发现密钥已提交或推送，立即停止并通知用户更换，不改写历史。

只在功能分支提交和推送，不直接修改 main，不 force push、不合并 PR。新增行为先有确定性测试或假模型集成测试，再用真实评测检查效果。模型调用必须在数据库事务之外；记忆正文和判断的修改需校验修订号，保留强度使用原子增量，不加修订号。文件和 HTTP JSON 都使用 UTF-8。
