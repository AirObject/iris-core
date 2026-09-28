# 学习评测

`learning_v1.jsonl` 是 PR #1 的 36 段短对话。原来的 12 段 holdout 已用于错误分析，因此 36 段现在全部为 dev，不再用于最终门槛判定。v1 仍保留作为回归材料。

`learning_v2.jsonl` 是本 PR 在修改学习提示词前冻结的 40 段完全虚构对话，共 1056 条消息、160 条 must。24 段 dev，16 段新 holdout；新 holdout 共 64 条 must。20 段私聊各 18 条，12 段群聊各 34 条，8 段直播各 36 条；群聊和直播每段有至少五位参与者。材料中穿插闲聊、插话、口语、省略、表情、中英混用及与记忆无关的事件。作者数据见 `build_learning_v2.py`，固定 JSONL 是正式评测集；冻结后不为提高分数再生成或更改。

覆盖：指令夹带、历史／后续段、跨批重复与修正、扮演、同名昵称、相对时间、角色承诺、敏感内容、低信息闲聊、多人转述、外部评价、刷屏、被引用作者同时在场、场景事件与行动结果交错、长消息和跨午夜相对时间。每段至少四条 must。真实标注错误可以修正，但须逐条记录原因；不能为提高成绩修改样本或放宽评分。

每行一个 JSON 对象：

- `id`、`split`、`tags`、`entry_type`；
- `messages`：时间顺序，每条含 `at`、`speaker`、`type`、`content`；可含 `account_id`、`quote_author`、`quote_author_account_id`、`quote_content`；
- `must`：事实、说话人、必需的 `about`、可选的 `about_optional`、立场；
- `forbidden`、`links`、`goals`：不应作为长期记忆的内容、应识别的人物联系和内部目标。

评测按入口实际节奏分批：私聊与群聊每批最多 12 条和 4000 token，直播用实时节奏每批最多 4 条。每段使用独立临时数据库，走正式接收、冻结批次和学习代码。评分固定为 `src/iris/prompts/scoring_v2.md`，每段同样输入独立判两次，不一致按不利结论计分并列清单。模型自己判自己可能偏高，最终报告仍须人工抽查至少 10%。

```powershell
uv run iris eval learning --split dev
uv run iris eval learning
```

迭代只运行 dev。新 holdout 仅在最终评测运行一次，之前不看其模型输出；一旦分析了 holdout 失败，该组就归入 dev，下一轮须另写 holdout。M1 学习门槛要求全部样本和 holdout 同时达标。报告在 `evals/reports/`。

## v1 标注审计（2026-09-29）

- L025—L036：原 holdout 已在 PR #1 审查中被分析，逐段改为 dev；事实和其他标注未改。
- L018 第一条 must：小林说的是母亲养狗及狗名，即转述另一人的状况，立场由“亲历”改为“转述”。其余 must 按 8.3 逐条核对，未发现需更正的立场。

## PR #1 结果（历史对照）

旧报告：`evals/reports/learning-20260927T173659625582Z-all.md`。当时使用学习提示词 `learning_v2`、评分说明 `scoring_v1`，对话模型 MiniMax-M3，embedding 模型 doubao-embedding-vision。直接解析成功率 97.2%，报告记忆精确率 93.2%，事实召回率 dev 92.9%、holdout 58.3%。审查发现同模型评分偏宽，精确率不宜直接视为真实值。
