# Iris 后续实现说明

产品行为以 `companion_memory_cognition_system_design_integrated.md` 和 `DECISIONS.md` 为准。当前代码只完成 M1 第 1 个 PR：学习核心及学习评测。不要复制 `dev-0`、`dev-1` 分支的代码。

## 安装和运行

需要 uv 和 Python 3.12。Windows PowerShell：

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
```

评测集 `evals/learning_v1.jsonl` 已先于提示词冻结；dev 可迭代，holdout 只用于最终评估。报告在 `evals/reports/`。不要为了达标修改样本或放宽 `scoring_v1.md`；真实标注错误须说明修改理由。

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
