# W01 验收证据索引

[验收环境](environment.json)记录本地运行时、工具与关键依赖版本、依赖锁摘要以及开发 SQLite/Provider 的适用范围。

[Core 构建输入](package-inputs.json)记录下一次包构建的输入，包含更新后的 README；README 会影响 sdist 和 wheel 元数据，不能当作包外报告忽略。CI 完成时还须核对该摘要，并归档实际构建产物摘要。`ci-001` 在浏览器阶段失败，未进入包构建。

[批次清单](completed-batches.json)记录已结束定向/诊断批次的真实命令、时限、状态、起止时间及原始文件 SHA-256。每批次保留 `task.json`、`result.json`、`output.log`；失败与后续通过分开保存。此清单不把历史子集通过合并成最终 CI 通过。

[公共 API 审查](public-api-review/review.json)及[逐行差异](public-api-review/reviewed.diff)限定此次接受的契约变化；没有扩大 Python/TS/CLI 导出。

[源文件审计](source-audit.json)确认 20 个旧 Migration 与开始 Commit 逐字节一致，版本清单中的 contract source SHA-256 匹配当前 source；这不替代最终 CI 的迁移/兼容回归。

`ci-001` 的[完整日志](ci-001/output.log)、[失败结果](ci-001/result.json)与候选摘要已归档；缺少浏览器可执行文件，未进入包构建。环境修复及最终复验状态统一见 [W01 报告](../../w01-http-recall-assembly.md)。

[首轮性能测量](ci-001-performance.json)区分实际打印值、通过断言和并发排队观测；不能把该子阶段通过视作完整 CI 或生产性能验收通过。

失败安装物的合成数据库只保留在本机 `.work-package-runs/W01/installed-003/failed-installed-recall-fixture/` 供诊断；不把私有运行库或 Provider 凭据打包进 Core，也不把该目录视为最终验收附件。

最终 [ci-002 完整日志](ci-002/output.log)及[成功结果](ci-002/result.json)证明完整收尾 CI 通过；[产物摘要](artifact-digests.json)和[最终候选审计](final-candidate-audit.json)绑定实际构建与无实质漂移的候选。W01 完成不代表 W02–W20 或稳定发布已完成。
