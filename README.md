# Iris 学习核心（M1 第 1 个 PR）

需要 [uv](https://docs.astral.sh/uv/)；项目固定使用 Python 3.12。

```powershell
uv sync
uv run pytest
```

把 `test-models.example.toml` 复制为仓库根目录的 `test-models.toml` 并填写测试模型配置；该文件被 Git 忽略。也可设置 `IRIS_TEST_MODELS` 指向工作树外的配置文件。然后运行：

```powershell
uv run iris models check
uv run iris eval learning
```

本 PR 提供学习命令行、学习评测和初始角色设定。HTTP 服务、试用页和一条命令启动在后续 PR 完成。
