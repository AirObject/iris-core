# 验证报告索引

报告记录当次候选、环境、命令、结果与限制；历史成功不是当前工作区或稳定发布通过证明。阶段状态统一见 [路线图](../development/README.md)。

| 阶段 | 验证证据 |
| --- | --- |
| 0 | [工程骨架](./phase-00-verification.md) |
| 1 | [持久化、身份与空间](./phase-01-verification.md) |
| 2 | [Observation、Outbox 与调度](./phase-02-verification.md) |
| 3 | [Recent、State 与 Focus](./phase-03-verification.md) |
| 4 | [Note、Task 与 Event](./phase-04-verification.md) |
| 5 | [长期记忆与 Episode](./phase-05-verification.md) |
| 6 | [FTS Recall](./phase-06-verification.md) |
| 7 | [Vector Recall](./phase-07-verification.md) |
| 8 | [Profile 与 Graph](./phase-08-verification.md) |
| 9 | [Persona](./phase-09-verification.md) |
| 10 | [巩固、Reflection 与 HTTP](./phase-10-verification.md) |
| 11 | Deferred，暂不执行且不阻塞当前发布；[Bellis Adapter](./phase-11-verification.md) 保留历史实现与复测，宿主 E2E 尚未闭环 |
| 12 | Deferred，暂不执行且不阻塞当前发布；无实施验证报告，保留 [Phase 12 清单](../development/phase-12-astrbot-bridge.md) |
| 13 | [Console 合并报告](./phase-13-verification.md)：认证历史 CI、读面失败和前端真实/模拟验证边界 |
| 14 | [实施与验证记录](./phase-14-verification.md)：读面、Required Lease、安装物与 CI；尚非稳定发布验收 |

[登录计时原始数据](./phase-13-step-02-login-timing.json) 由 Phase 13 报告解释，不能脱离当时环境用作新的性能承诺。本目录不为未执行工作建立空的“通过报告”。

## 横向审计

- [2026-09-07 Phase 1–10、13–14 完成情况](phase-completion-audit-2026-09-07.md)：区分领域实现、HTTP 接线与验收证据，记录 Vector/Graph 能力声明缺口、Console 实际切片和本轮定向实测。
- [2026-09-07 项目组织与发布就绪度](structure-and-release-readiness-2026-09-07.md)：§6 记录逐项核实、工程修复、复验和保留的功能/生产验收清单。
- [2026-09-07 测试分层与 pytest 耗时诊断](test-layering-analysis-2026-09-07.md)：本机数据的诊断分析而非阶段验收；优化方案未实施，1,080.53 s 为多次运行的拼接估算而非一次实测，不代表 CI 数据。

- [W01 HTTP Recall 接线与能力声明](./w01-http-recall-assembly.md)：Completed，完整 `ci-002` 通过，独立提交 `0959259`。

- [W02 Graph 原量化门禁](w02-graph-quantitative-gates.md)：Completed，真实路由预算、五类各 50 次竞态及完整 CI 已通过。

- [W03 Persona 原量化门禁](w03-persona-quantitative-gates.md)：Completed，Policy 每项 200 案例、50 客户端发布、三次时钟轨迹及完整 CI 通过。
