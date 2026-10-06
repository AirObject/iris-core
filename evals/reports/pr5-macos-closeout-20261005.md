# PR #5 新设备收尾（2026-10-05）

本次在 `/Users/cassia/Local/Code/iris-core-pr5` 收尾 `feat/m1-recall-quality`，起点 `8d8f3fc699dc785eadaf23387ef2e2d1e77b7ce8`，以 merge 提交 `25edc97` 合入 `origin/main` 的 `509aa0181d9bd8e70603fb21c1cb8c13514f46f2`。本次不修改产品代码、测试、迁移、提示词、检索参数或语料，不运行学习评测，不合并 PR。

## 合并与文档

开始前执行 git fetch origin。远端不存在题述标题“docs: 换用 GLM 并重建评测计划”；实际提交是 `509aa01 chore: 清理 MiniMax-M3 评测产物`，包含指定的 GLM／macOS 决策与重测计划，按其内容合入。起始 PR 头已核对为 8d8f3fc。

冲突仅在 AGENTS.md、ARCHITECTURE.md、evals/README.md。按 main 的文档结构保留 GLM 模型、macOS 环境和重测要求，再补回 PR #5 的默认参数、点名锚点、人物要点补位、学习检索独立及语料／选参方法；README.md 同步整理。evals/README.md 的“当前状态”“GLM 重测计划”逐字保留 main 原文，新结果在独立章节追加。

删除分支上的全部 68 份旧评测报告及 PR5_EXECUTION.md，并接受 main 对 PR3_EXECUTION.md、PR4_EXECUTION.md、PR4_TEST_COVERAGE.md 的删除；清除对应文档链接及旧结果数字。所有评测语料和 notes 与起点相同。源码 29 个文件逐字节匹配 8d8f3fc 的 Git blob，tests、默认参数、依赖锁和性能脚本的 Git diff 均为空。

## 环境、连接和测试

Apple M4，macOS-26.6.2-arm64-arm-64bit-Mach-O，10 个逻辑 CPU，16 GiB 物理内存；Python 3.13.15、SQLite 3.53.1、NumPy 2.5.3。

运行 uv sync --python 3.13。IRIS_TEST_MODELS 指向主工作树的 test-models.toml；未复制、手动打开或打印配置。连接检查使用临时目录 check.db：glm-5.3-flash 成功（3674ms），doubao-embedding-vision 成功（242ms）。连接时间只用于本次可用性检查。

| 检查 | 结果 |
| --- | --- |
| uv run pytest / Python 3.13.15 | 215 passed，3.29s，1 warning |
| uv run --locked --isolated --python 3.12 pytest / Python 3.12.14 | 215 passed，2.79s，4 warnings |
| uv build | sdist 与 wheel 均构建成功 |
| 测试后 worktree .venv | Python 3.13.15，未被 3.12 重建 |

两个版本先后运行，共用的 .pytest-tmp 未并发使用。警告为既有 Starlette TestClient/httpx 弃用提示，以及 Python 3.12 环境中 jieba 的三条无效转义 SyntaxWarning；连接检查首次导入 jieba 也出现同样三条提示。未修改依赖消除警告。

## 召回复现

运行默认 `uv run iris eval recall`，不传 --calibrate／--compare-embeddings。三份公开语料、123 条记忆、128 条查询，重新请求 251 条 embedding，251 次成功，输入 9720 token，墙钟 310.6s。原始 [Markdown](recall-20261005T142650553081Z-all.md)／[JSON](recall-20261005T142650553081Z-all.json) 和 [完整历史对照](pr5-macos-recall-comparison-20261005.md) 保留。

历史内容只通过 git show 8d8f3fc 读取，旧文件未恢复。核对 200 行 Markdown 指标以及同名 JSON 的全部未四舍五入指标、查询返回、顺序与 reason。纯全文两组完全一致；默认 trigram_hybrid 有 5 条 v2 查询变化，jieba_hybrid 有 6 条，未在输出不一致后调参。

默认 Recall@8=0.9812925170、nDCG@8=0.9561115939、无关误返率=0.7000，与历史一致；relevant 标注精确率 0.5364583333 → 0.5478723404，标注命中仍为 103，relevant 返回 192 → 188。按语料和类别的完整表见对照报告。因此本次是部分复现，是否重新标定由规划者决定。

变化仅见于混合路径、且本次重新获取向量，embedding 服务与旧缓存的差异是可能原因；旧报告没有向量原值，无法证实。另有历史指纹异常：旧报告的源码 SHA-256 为 32276100…，当前为 f1e4af46…；尽管当前 29 个源码文件和 8d8f3fc Git blob 完全相同，常见换行／路径排序变体仍不能解释旧指纹，留待规划者追溯。

无关误返仍为 21/30，未达到 M2 ≤0.10 参考值；无答案拒绝按已确认范围留到 M2。没有寻找或读取隐藏验收集，也不以公开复现结果宣称 M1 最终通过。

## 默认配置性能

| 记忆数 | 查询 | prepare P50 ms | prepare P95 ms | HTTP P95 ms | 向量 P95 ms |
| ---: | --- | ---: | ---: | ---: | ---: |
| 5000 | 不点名 | 13.2 | 14.5 | 15.4 | 3.0 |
| 5000 | 点名 | 24.7 | 25.9 | 27.6 | 3.0 |
| 50000 | 不点名 | 75.5 | 83.2 | 84.6 | 30.0 |
| 50000 | 点名 | 198.7 | 208.4 | 210.6 | 29.3 |

R10 默认四组 通过，最大 prepare P95 208.4ms。索引块、载入 RSS 增量、查询后 RSS、原命令的全部扩展对照和方法见 [性能报告](retrieval-performance-macos-20261005.md)。原命令扩展对照有 3 组 prepare P95 超过 500ms，已完整报告；没有用重跑挑选较快结果。macOS 原脚本的 peak_working_set_mib 实为末尾 RSS，未冒充峰值。

## 本轮范围与待决定事项

学习影响由「test: GLM 学习基线与 PR #5 对照」另行测量。本任务未运行学习评测。提交前检查 git check-ignore test-models.toml 和 git diff --cached，仅暂存文档与新报告，并扫描凭据模式；不读取真实配置来做比较。

待规划者决定：召回精确率／逐条返回变化是否需要重新标定，以及历史报告源码指纹的追溯。模型／参数／语料维持冻结，PR 仅更新并推送，不合并。
