# Phase 5/6/7/8 未闭合复审项修复（2026-09-06）

本报告补充原阶段报告，记录用户提供的未闭合评审清单及本轮发现的证据级联缺陷。历史报告中的 CI 数字保持原样，不作为本次修复的验证结果。

| 阶段 | 级别 | 问题与修复 |
| --- | --- | --- |
| 5 | P1 | 菱形证据链的汇合节点先处理时仍有证据，随后另一分支撤回却不会再次入队。级联现在只去重待处理队列，允许部分存活节点在再次失去证据后重新处理；Forget 与显式 retract 同步覆盖。 |
| 6 | P1 | Recall 回放指纹升级为 v2：先解析 actor，再绑定完整 actor 列表、终态 speaker、app 与 AccessContext 授权快照；改变授权或 actor 的同 ID 回放拒绝。 |
| 6 | P1 | FTS backlog 同时统计 claim/episode/note.changed、memory.invalidated 与 fts.apply，包括本租户无 Agent 的事件；前驱尚未生成 apply 时不能假装追平。 |
| 6 | P1 | 首次 Recall 发布在写事务内二次读取请求记录：并发同 ID 同指纹返回已落库胜者，不同指纹拒绝。 |
| 6 | P1 | 发布事务内重新检查候选资源的 tombstone，与 Forget 的 tombstone/scrub 共用 Writer Gate；删除先提交时拒绝发布，发布先提交时由 Forget 擦除响应。 |
| 6 | P1 | 新增 0014 迁移，将 recall_requests 主键改为 (tenant_id, id)，usage 使用复合外键。保留原请求及 usage 数据，历史迁移 SQL 不变。 |
| 7 | P1 | 限流等待遵守请求 deadline，睡眠不持有限流锁；短 deadline 请求不必等待其他请求的整个补充令牌间隔，等待超时释放熔断探测占位。 |
| 7 | P2 | open 状态丢弃历史成功探测，half-open 必须实际重新 probe，成功且 circuit 回到 closed 才恢复可用。 |
| 8 | P1 | Coalesce 将 payload 与 payload_version 同步持久化，优先保留较高版本，旧版本不能把未来语义降级；同 dedupe 身份的版本冲突同样拒绝。 |
| 8 | P1 | verify 失败在同事务内将具体 generation 退役并写 pending_rebuild；其他租户重建即使恢复全局 ready，也无法让已退役的损坏 generation 通过读门。 |
| 8 | P1 | Graph 构建和逐边可见性拒绝 REDIRECTED 端点；actor 绑定解析跟随 Redirect 链，拒绝没有可解析终态的身份。已删除主体沿用既有路由过滤行为，不额外令整个 Recall 请求失败。 |
| 8 | P2 | Profile apply 根据已持久化来源查回旧主体，与当前修订的新主体一起重新派生，删除 target 变更留下的旧 relationship 配对。 |
| 8 | P2 | Entity Profile 读取先解析 Redirect 终态，再派生及校验终态权限；HTTP 响应 subject_id 与实际终态 Profile 一致。 |

## 验证

- 新增定向回归：`tests/integration/test_phase568_review_regressions.py` 与 `tests/integration/test_phase7_review_deadline_probe.py`。
- 并发 Recall 用屏障确保两个调用均越过首次查重；Forget 竞态在实际 Rehydrate 返回后、响应发布前同步提交删除。
- Graph/Profile 两种投影分别验证跨租户 rebuild 不解除损坏判定，以及本租户 rebuild 可恢复。
- 0014 从真实 Schema 13 升级，核对请求与 usage 行保留、复合外键和 `foreign_key_check`；另以两个租户复用普通 request ID 验证隔离。
- 新增 **31 个用例全部通过**：联合复审文件 27 个，Phase 7 deadline/probe 文件 4 个。
- 最终 Graph/Profile 联合回归：`test_phase8_profile.py`、`test_phase8_graph.py`、`test_phase568_review_regressions.py`，**177 passed / 0 failed**，62.63s。
- 真实旧备份夹具同步删除后续迁移在旧表上新增的索引，防止伪造的旧快照在升级时发生重复索引错误；round 4/5 回归 **20 passed / 0 failed**，3.26s。
- 最终 `make format-check lint typecheck contracts-check` 全过：241 个格式检查文件、mypy 239 个文件、TypeScript 类型检查、契约生成/兼容与文档检查通过。
- TypeScript SDK 独立补跑：**18 passed / 0 failed**。

完整 `make ci` 在允许本机回环端口的环境执行：**10603 passed / 2 failed**，557.64s，覆盖率 **83.84%**（要求 ≥80%）。两项失败分别为：

1. 本轮 actor 解析对已删除主体过早报错，改变了既有“请求保留、画像候选被过滤”的语义。随后收窄检查，保留 Redirect 终态解析；上述最终 177 项回归包含原失败用例，全部通过。
2. **既存 Phase 13 阻断项**：`test_reflection_and_candidate_views_reauthorize_the_input_closure` 中实际 `reflection:<fingerprint>` ID 不满足 Console ResourcePage 的 UUIDv7 Schema。与 [Phase 13 报告](phase-13-verification.md)原有记录一致，本次未修改 Console Resource ID 契约。

最终兼容修正后未再重复整套 `make ci`；已重跑受影响的 Graph/Profile 与本轮联合回归，以及全部静态门禁。因此不宣称全量 CI 为绿色。执行日志：`/tmp/iris-review-ci-final.log`、`/tmp/iris-review-profile-final.log`、`/tmp/iris-review-checks-final.log`、`/tmp/iris-review-sdk.log`。

## 兼容性说明

后续工作区提交前已针对最终实现完成一次全量复验：**10604 passed / 1 failed**，576.55s，覆盖率 **84.10%**；唯一失败仍为 Console Reflection UUID 契约冲突，已删除主体的 Profile 行为不再失败。静态与契约门禁通过，SDK 独立 18 项通过；候选提交与完整命令见 [Phase 13 工作区提交复验](phase-13-verification.md#工作区提交复验2026-09-06)。本段是后续证据，不改写上文历史执行记录。

- Schema 上限升为 14；已有 11–13 库需执行正常迁移才能获得复合请求主键。
- v1 指纹无法证明原 actor/授权上下文，升级后旧请求 ID 回放拒绝，调用方应使用新 request ID。旧 usage 与审计记录保留。
- FTS backlog 采用保守计数：前驱与后继都必须结算；仅重建 FTS 不会结算仍需驱动其他投影的前驱事件。
- Graph/Profile 保留既有全局健康标记，但损坏拒绝判定另外绑定具体 generation，只有该租户的新 generation 能恢复服务。
