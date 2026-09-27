# 学习评测

`learning_v1.jsonl` 是 36 段完全虚构的固定评测集：24 段 dev、12 段 holdout。它在提示词调整前提交（`e5895eb`）。每行一个 JSON 对象：

- `id`、`split`、`tags`、`entry_type`；
- `messages`：按时间排列，每条含 `at`、`speaker`、`type`、`content`，可含 `quote_author`；
- `must`：必须记住的事实，每项含 `fact`、关键 `speaker`、`about` 和 `stance`；
- `forbidden`：不应记住的内容；
- `links`：应识别的人物联系；
- `goals`：应产生的内部目标。

覆盖历史/后续段、跨批重复、指令注入、角色扮演、昵称、相对时间、角色承诺、敏感信息、闲聊、多人转述、外部评价和刷屏。每段六条消息，评测目标段最多三条，故每段跨两个批次。每段创建独立临时数据库，走正式消息接收、批次及学习代码；只用对话模型按 `src/iris/prompts/scoring_v1.md` 判分。

```powershell
uv run iris eval learning --split dev
uv run iris eval learning
```

报告存于 `evals/reports/`，包含 dev、holdout、全部指标、前次结果对比，以及固定随机种子抽取至少 10% 的人工核对清单。模型自己判自己可能偏高，抽查结论需用户人工填写。评分规则和评测集不随达标情况放宽；仅真实标注错误可修正，并须在报告说明理由。

本次结果摘要见 `evals/reports/` 中最新的 `*-all.md`。
