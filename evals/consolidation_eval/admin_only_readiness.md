# CO-01 整理建议仅管理员可见（2026-10-10）

依据规划者对 PR #46 / cf888a9 的审查，保留已接受的 broad 合并；将矛盾与依赖产生的标注限定为管理员可核对的整理建议。宿主 prepare/search 完全移除 consolidation_annotations 字段，待确认、已确认的建议均不返回；选取、排序、名额和召回判断不变。

## 管理接口与状态

- 管理记忆详情仍使用 consolidation_annotations 字段，标明“整理建议（模型建议）”、model_suggestion=true、visibility=admin_only，显示 pending/confirmed 状态与确认时间／确认者。正文、相信程度、保留强度、修订号和生命周期不因采纳改变。
- POST /admin/api/memories/{memory_id}/annotations/{annotation_id}/confirm 接受 expected_revision，仅将建议标为已确认，沿用管理员鉴权、CSRF、修订号校验和操作记录。重复采纳幂等，写入一次操作记录；采纳不触发新的模型调用。
- 既有 DELETE /admin/api/memories/{memory_id}/annotations/{annotation_id} 清除建议；已确认的也可以清除。已清除建议不能重新采纳，不复活同一配对／结论的重复建议。
- 整理报告与详情中的结论文字显式前缀“模型建议：”，保留原有来源摘录；报告另显示建议当前待确认、已确认、已清除状态。记忆之后修订，仍保留“标注后已修改”提示。
- 新增 017_consolidation_suggestion_review.sql，在本分支 015/016 后追加 confirmed_at、confirmed_by 及报告查询索引。当前 origin/main 为 6be0bb0，最新迁移 014。已有建议不会自动确认。
- 本步在授权的管理 API 中提供显示、采纳与清除；未改前端文件。

## 生成逻辑与历史质量

本回合真实模型调用为 0，未重跑真实评测。相对 b4f5eae，Consolidation 执行类及所有整理提示词逐字／AST 核对不变；已有顶层函数只有 memory_annotations 的管理员投影改变，另增审核状态投影和确认函数。合并判断、正文校验、来源继承、标注生成及模型材料不变。

两轮真实评测全部数字和失败类型仍保留在 [final_validation.md](final_validation.md) 与 [final_validation.json](final_validation.json)：合并两轮均 0/12 误合并、12/12 识别，保护 4/4、相信程度上限 12/12；矛盾 6/10、6/10，依赖 5/8、7/8；原始损坏双判 4/40、2/40（第一轮包含 1 例仅报告错误，实际标注损坏至少 3/40、2/40）。错误姓名／来源、无依据细节和性别、错误 disputed 分类、漏处理与无效证据弃项并未修复；本次通过取消宿主可见范围，交由管理员核对处理。原始审计口径差异完整保留。

## 本轮验证

先补假模型测试，旧实现 6 failed / 8 passed；实现后相关测试 105 passed。最终源码上依次运行：

- uv run pytest（Python 3.13.15）：1600 passed，199.36 秒。
- uv run --locked --isolated --python 3.12 pytest（Python 3.12.14）：1600 passed，204.59 秒。各 4 项既有第三方警告。
- uv build 成功；wheel 中的实现和 017 迁移与源码一致，项目环境仍为 Python 3.13.15。
- 学习：沿用未修改的 compare_learning_requests.worker，仓库外包装覆盖五份公开语料；对照 main 6be0bb0，116 段／272 批的 messages、purpose、max_tokens 逐字一致。
- 召回：七份公开集 × 混合／全文降级，judge=false，456 次对照、2216 条记忆返回与 main 逐项完全一致，包括字段和顺序；没有剔除字段或做归一化。建议存在且分别为待确认／已确认时，宿主隔离另由假模型测试验证。
- 采纳／清除覆盖未登录、CSRF、错误对象、修订冲突、幂等、重启持久化、清除后拒绝采纳、正文与相信程度不变，以及不触发重跑。
- R10 使用既有默认基准，含 persona、当前状态和目标，每组 5 次预热、60 次采样；在其他验证结束后独立运行，本地假网关，无外部模型请求。

| 记忆数 | 查询 | prepare P95 ms | HTTP P95 ms |
| --- | --- | --- | --- |
| 5000 | 不点名 | 20.76 | 29.18 |
| 5000 | 点名 | 36.60 | 38.30 |
| 50000 | 不点名 | 114.78 | 146.00 |
| 50000 | 点名 | 143.96 | 167.00 |

四组 prepare P95 均低于 500ms。完整日志、main 快照、逐批请求、逐条返回和基准数据仅在 `/Users/cassia/Local/Code/iris-eval-artifacts/co01-admin-suggestions-20261010`；仓库只放此汇总和 JSON 指标。
