# M2 无答案拒绝探测（实验工具）

仅分析七份指定公开 dev，不被产品导入；不改默认配置、源语料或学习模块。新集先以 `7d696a7d1145b701edbe7431e3e5ab4539c6e415` 单独冻结。正式结论与外部材料索引见 `../reports/m2-no-answer-probe-20261009.md`。

## 复现

在功能分支 worktree、Python 3.13 环境中运行。`IRIS_TEST_MODELS` 指向既有绝对路径，通过既有加载器使用配置，不查看／复制／输出配置文件。`--out` 必须在仓库外，每轮使用一个新目录；协议包含当次 git HEAD、源码和脚本 SHA-256，不能将不同提交的执行记录拼在一起。

```sh
uv run pytest evals/no_answer_probe/test_probe.py
IRIS_TEST_MODELS=/absolute/path/test-models.toml uv run python evals/no_answer_probe/account_probe.py --out /external/probe
IRIS_TEST_MODELS=/absolute/path/test-models.toml uv run python evals/no_answer_probe/probe.py capture --out /external/probe/run
IRIS_TEST_MODELS=/absolute/path/test-models.toml uv run python evals/no_answer_probe/probe.py judge --out /external/probe/run
uv run python evals/no_answer_probe/probe.py summarize --out /external/probe/run
IRIS_TEST_MODELS=/absolute/path/test-models.toml uv run python evals/no_answer_probe/live_check.py --out /external/probe/run
# 完整低速复核，保留前一轮；不是只重试失败样本
IRIS_TEST_MODELS=/absolute/path/test-models.toml uv run python evals/no_answer_probe/live_check.py --out /external/probe/run --label rate-check --concurrency 1 --interval 1
```

capture 可选 `--cache-source <已知公开评测的 recall-embeddings.db>`，只按本轮公开输入的端点／模型／2048 维／完整文本键查询匹配记录，不扫描其他输入。七份语料各自建库，不读取用户在用数据库。所有历史 split 均按公开 dev 选择，原标记保留。

## 比较边界

通过未修改的 `Retrieval.prepare/search` 取得基线。额外的 `TracedRetrieval` 只记录已有信号，同时逐查询断言返回 ID、顺序和 reason 与普通 Retrieval 完全相同；它的额外诊断查询不计入基线的本地延迟。

全部方法只过滤基线已选中的 relevant 项，保持原顺序，保留原 person_highlight，不补位，不把被拒绝项改成人物要点。K 指前 K 条 relevant 项；本轮最多 5 条，K=4/8 不是候选池扩容实验。K 之外的 relevant 项在成功时删除，失败时恢复整个基线。Recall/nDCG 计全部返回，误返／精确率只计 relevant，沿用正式 `recall_metrics`。

确定性网格（固定顺序 99 个）：

1. cosine：候选余弦 ≥ `{0,.35,.4,.45,.5,.55,.6,.65,.7,.75,.8,.85,.9}`。
2. coverage：去姓名后完整词项的全文覆盖 ≥ `{0,.1,.25,.35,.5,.65,.75,.9,1}`。
3. agreement：现默认两路都入选，且余弦 ≥ `{.35,.45,.55,.65,.75}`。
4. margin：当前锚点范围候选的最高余弦 ≥ `{.35,.45,.55,.65,.75}`，前两名余弦差 ≥ `{0,.02,.05,.1,.15}`，整条查询开／关。
5. normalized：候选余弦 ≥ `{.35,.45,.55,.65,.75}`，且相对最高余弦比例 ≥ `{.8,.9,.95,1}`。
6. cosine_or_coverage：余弦 ≥ `{.35,.45,.55,.65,.75}` **或**覆盖 ≥ `{.25,.5,.75,1}`。
7. blend：`0.7*cosine + 0.25*coverage + 0.05*两路一致` ≥ `{.3,.4,.5,.6,.7,.8,.9}`。

没有话题、属性、领域词表；姓名仅沿用产品锚点和别名解析，不增加姓名匹配分。余弦来自现有模型，覆盖用现有 query_analysis.coverage_tokens 和已存全文索引。gap 的范围是已通过结构／姓名条件的候选池，包括现有人物要点，完整定义见 TracedRetrieval。

模型候选顺序：low/K4、low/K8、high/K8；实际按查询交错发送三组请求；通用提示词为 judgment_prompt.txt。实际请求单次总预算 60 秒、4096 输出 token、temperature=0、并发 2，不重试／修正 JSON；不关闭推理。只传当前文本、近期上下文、已确认别名和候选正文及归属，不传标签、类别、split、特征或无答案状态。support 是未校准的整数支持度，不是概率。按 `{1,25,50,75,90,100}` 阈值过滤，并回放 `{2,5,10,20,60}` 秒预算。空候选不调用。仅保存响应正文和推理出现标志／字符数／用量，不保存推理全文。

组合：整条查询只在最高 relevant 候选余弦 ≥ `{.45,.55,.65}` **或**最高覆盖 ≥ `{.5,.75}` 时调用相同的 low/K8 或 high/K8；否则删除 relevant。通过预筛时请求与单模型完全相同，复用同一真实调用，属于可精确回放的组合，不声称另作了独立模型运行。未采用改变候选正文后仍复用旧分数的方法。

固定候选顺序：baseline → 99 个确定性网格 → 三组模型（分数阈值升序，再超时升序）→ 六个预筛（余弦外层、覆盖内层；low 再 high；阈值、超时升序）。总计 550 个。选择遵守 M2 规则；Q 使用 Decimal，严格小于 0.01 才持平，最终平分不再次比较 Q。没有可行候选时在 Recall/nDCG 合格者中取最低误返，完全相同按固定顺序。live_check 在选定后以实际总超时重新调用全量公开集，不加入候选池重新挑分。

所有错误、超时、截断、拒绝、数组错位／长度错误／非整数分数均返回完整原始召回；每轮失败记录也保留，不按分数选择缓存。超时回放的用量来自实际 60 秒调用，不冒充提前取消后的实际账单。纯全文降级单列，不参与混合门槛。

生成本轮报告：`uv run python evals/no_answer_probe/report.py --artifacts <外部根目录> --report-dir evals/reports`。重画曲线可在独立临时工具环境安装 matplotlib 后加 `--plots`，不要更换正在评测的项目虚拟环境。此报告工具针对本轮 `run-v1` 布局和日期；完整输入、调用输出、返回和逐题损失都留在外部，仓库 CSV 只含候选／语料汇总及退化查询 ID。
