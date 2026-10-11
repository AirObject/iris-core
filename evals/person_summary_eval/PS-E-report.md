# PS-E：人物摘要评测运行器补充

2026-10-12；分支 `eval/person-summary-runner`，独立 worktree `iris-core-ps-runner`。基于 main `4e70ee6`（用 `git merge origin/main` 快进到上游新增文档）。只修改本评测目录和对应测试；`src/`、冻结的 person_summary_v1 语料及 PR #95 均未改动。未读取隐藏集，未使用真实模型。

## 预期不生成

- 新增可选 `expect_none`，必须是真布尔值，缺省 false；true 时 must_cover 必须为空数组（省略按 []）。支持零条记忆的案例。
- 只有 `skipped`＋`insufficient_memories` 且没有生成痕迹才通过；空 skipped 操作记录允许存在。每例保留 `due.global_memories`。
- 对数据库新增版本元数据、全部尝试、材料、原始输出、调用记录独立核对。任何 published／candidate／rejected 版本或生成尝试（含失败）都记 `expect_none_violations` 并阻止门槛通过。`initial_summary` 的入库管理员版本不计为本次生成。
- 检查状态为 passed／failed／incomplete；出现禁止的生成行为时已有确定性失败结论，complete=true 表示已判定，不代表通过。其余无生成的跳过或基础设施错误均为未完成，不能当作正确不生成。
- 采用**空判分任务**，保留 PR #7 的每例文件／清单对应关系，标记 `judgment_mode="empty"`，评分只允许 `{"item_results":[],"must_cover":[],"forbidden":[]}`。不影响普通任务的质量／覆盖率分母，外部判分不能覆盖违规。全部案例都为 expect_none 时有依据条目率为 null，不宣称 ≥90% 门槛通过。

## 截断诊断

普通生成案例记录入选条数、全局可用条数、`material_truncated` 和被挤出的语料 ID／隔离库数字 ID。优先读取实际尝试材料及其候选快照；尚未生成材料的跳过案例使用生成前只读选材快照，明确标为 `pre_generation_snapshot`，不冒充模型实际输入。expect_none 不选材。

汇总 `material_truncated_cases`、`material_truncated_memories`、逐例 `material_truncations`；快照不可用另计 `material_diagnostics_unavailable`。预算挤出整条记忆才属于此诊断，正文 excerpt 截断另由原字段表示；不减少 must_cover 分母。

## 非 active 检查及格式变化

从语料 lifecycle 独立计算 forgotten／deleted／purged ID，不依赖生产选材器、当前数据库状态或外部判分。检查全部尝试的入选记忆、上一版依据、管理员条目依据、随材料传给模型的祖先 dependency_ids，以及最终材料与逐条依据。`non_active_violations` 和受限来源泄漏并列，任何违规均阻止门槛通过；详情保留语料 ID、数据库 ID、lifecycle 和位置。同一 ID 出现在不同位置分别计数，另列被检查的非 active 记忆数量。

语料的 `lifecycle` 新增可选值 `purged`，仅供评测表示彻底清除：隔离库写入 deleted＋purged_at=as_of，正文置空，content 可填空字符串。原 active／forgotten／deleted 格式及未声明 expect_none 的案例均兼容。未修改冻结语料；新增的机制夹具只在测试中使用，不作为质量语料。

## 验证

先补失败测试，再实现；运行器从 22 项增至 74 项（含参数化）。测试覆盖：严格布尔与空 must_cover、零记忆、所有非 skipped 尝试状态、伪装为 skipped 的材料／输出／版本、丢失返回结果后仍残留的数据库版本、模型错误、空任务分母、预算截断、健康跳过、三种非 active 生命周期、祖先 ID，以及独立于选材器与外部判分的门槛阻断。

最终版本依次执行：

| 检查 | 结果 |
| --- | --- |
| Python 3.13.15 相关测试组 | 199 passed，18.76s |
| Python 3.12.14，`--locked --isolated --python 3.12`，同一相关组 | 199 passed，18.91s |
| `uv build` | wheel／sdist 成功 |
| 最终假模型 CLI：隔离入库 → 生成／预期不生成 → 材料导出 → 离线计分 | 通过；一个普通生成案例、一个 expect_none 案例；不作真实质量判定 |

相关测试组：`test_person_summary_eval.py`、`test_person_summary.py`、`test_person_summary_review.py`、`test_person_summary_consolidation.py`、`test_external_judging.py`、`test_entry_profile_eval.py`。两版本顺序运行，3.13 环境未重建。现有 jieba 转义 SyntaxWarning 不影响通过。

完整日志、手写极小 CLI 机制样例、假响应、隔离数据库、材料与假判分均在仓库外 `/Users/cassia/Local/Code/iris-eval-artifacts/PS-E-20261012/`；最终 CLI 记录为 `cli-final/`。本文件仅为汇总。
