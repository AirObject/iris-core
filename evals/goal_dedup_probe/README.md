# 目标去重探测

候选和提示词冻结提交：`9cacbd02d38f0200dd757316c7b8c8c101dde113`。A／B／C 及选择规则见 [METHODS.md](METHODS.md)，该文件与判断提示词在首次模型调用前提交，此后未修改。独占窗口内 A／B／C 双轮已完成，按冻结规则选中 C，产品默认开启。5 秒预算下也完成连续双轮有效运行；此前 12 次作废尝试均保留，全部数字和作废原因见 [RESULTS.md](RESULTS.md)。

每例固定时钟、独立数据库，按真实 host／internal／admin 路径创建并复核。只将产品材料送给模型；expected 和 category 只在执行结束后的独立评分中读取。保存实际合并记录、候选指向、请求及正文输出、耗时和 token；不保存模型推理原文、请求头或配置。所有完整结果必须放仓库外，输出目录须不存在。

```sh
uv run python evals/goal_dedup_probe/run.py --method C --fake uncertain --out /absolute/artifacts/fake-C-1
```

`--fake same/different/uncertain` 固定返回对应判断，`--fake timeout/invalid` 注入降级。假模式用 httpx MockTransport，完全不读取模型配置，也不访问网络。report.json 中 simulation=true，选型函数拒绝这类报告；不能据此报告质量或延迟门槛。

收到独占模型窗口授权后才运行真实探测（省略 --fake），每个候选两次完整运行，使用不同外部目录：

```sh
IRIS_TEST_MODELS=/absolute/path/test-models.toml uv run python evals/goal_dedup_probe/run.py --method B --out /absolute/artifacts/B-1
```

`--corpus /absolute/path/corpus.json` 可替换输入；默认只读公开 goal_dedup_v1。测量预算默认每目标 60 秒，B 的多请求共用；`--budget-seconds` 支持验证最终产品预算（至多 10 秒）。遇到降级立即保存部分运行，返回退出码 2；整次无效，以新目录重跑，不能续拼或筛选成功案例。遇到其他真实评测并发也整次作废，不覆盖原材料。

score_case／report 输出误合并率、应合并识别率、separate／uncertain 可能重复比例、可选 expected.candidates 命中与越界数、逐类别和逐例结果、降级／待复核、调用、P50／P95 和用量。0 次实际合并为 0/0，rate=null，不可行。select_method 对每候选至少两次有效运行取保守数值，再按冻结规则选择；不把模拟或无效运行混入选择。

在冻结后修复了响应外围 Markdown 分隔符处理、角色时区投影和原始正文输出留存；没有改变提示词、候选规则、标签或评分公式。旧的无效运行保存在 `iris-eval-artifacts/m3-goal-dedup-20261009/`，作废原因见仓库外 run-validity.json，后续不参与选型。

前一回合离线就绪检查见 [READINESS.md](READINESS.md)（历史记录，包含当时的草稿默认值和迁移编号）；当前结果与验证见 [RESULTS.md](RESULTS.md)。
