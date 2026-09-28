# Iris 学习核心（M1 第 2 个 PR）

需要 [uv](https://docs.astral.sh/uv/) 和 Python 3.12 及以上。uv 会直接使用机器上已有的兼容 Python；没有时才下载。国内网络下载解释器较慢时，可设置 `UV_PYTHON_INSTALL_MIRROR`，或用 `uv run --python <解释器绝对路径>` 指定已安装的 Python 3.12／3.13。

```powershell
uv sync
uv run pytest
```

可用 `uv run iris setup --name Iris` 创建初始角色。学习命令行可用 `uv run iris ingest messages.jsonl` 接收 UTF-8 消息，再执行 `uv run iris learn <入口标识> --force`。JSONL 每行至少包含 `entry_id`、`platform`、`sender`、`content`、带时区的 ISO `occurred_at` 和入口内唯一的 `dedupe_key`；可选 `kind` 为 `message`、`self_output`、`action_result` 或 `event`。引用可带 `quote_author`、`quote_author_account_id` 和 `quote_content`；场景事件有固定的“场景”主体，行动结果属于“我”。

把 `test-models.example.toml` 复制为仓库根目录的 `test-models.toml` 并填写测试模型配置；该文件被 Git 忽略。也可设置 `IRIS_TEST_MODELS` 指向工作树外的配置文件。然后运行：

```powershell
uv run iris models check
uv run iris eval learning
```

本 PR 更新学习质量与可信评测：v2 长对话集、双次判分、真实节奏分批和学习提示词 v3。HTTP 服务、试用页和一条命令启动在后续 PR 完成。
