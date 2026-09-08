# ADR-0050：生产认知 Provider 部署绑定

- 状态：Accepted
- 日期：2026-09-08
- 范围：W06；继承 ADR-0019，Phase 11/12 保持 Deferred

部署文件显式选择 `openai-compatible-chat` 适配器及非空 model/model_version；不提供默认模型，不在 Console 中选型。模型版本进入持久 run identity。配置按 tenant 精确授权，只允许通过 W05 受限 env/file secret_ref 解析凭据；固定提示词只发送窗口内 observation 的 id/revision/content/role，排除 structured_payload、时间、隐私标签、租户授权信息和内部路径。Secret 与原始模型响应均不写库。

部署方必须声明授权编号、允许的 privacy_labels、最大输入字符数、输出 token 上限、每请求保守成本上界、每日预算以及各 Port 治理限制；配置中不接受原始 prompt 或任意 HTTP header。超过授权标签或字符预算时不调用模型。每次重试都按成本上界预扣；提供商低报 usage 不降低预扣。每次 request 同时受固定 IP/DNS/SNI/Host、禁止代理/重定向、请求响应字节限制和总超时约束。网络调用始终在 SQLite 写事务外。

没有配置的租户不声明 reflection.v1；生产 Worker 不以空候选 fake 完成认知工作。四类 Port 由受控适配器实现；摘要和提取生成不可信结果，Reconciliation 的最终决定以及 Persona Proposal/Policy 提交继续使用现有确定性边界。模型不得直接发布 Persona、绑定或完成任务。

在任何真实模型评估前冻结 `tests/fixtures/cognitive/w06-evaluation.json`：摘要必须覆盖至少 90% 标注事实且无标注反事实；提取 precision ≥ 0.90、recall ≥ 0.80；越权 Evidence、直接 Core/active task 指令接受数必须为 0；三次相同来源的服务重放 Canonical 新增次数不超过一次。HTTP 成功不能替代质量。

本地隔离 HTTP 适配器验收只证明运行/治理/契约，不计为真实模型质量验收。外部真实允许模型尚未提供 model/version、凭据、数据授权与成本上限，相关门禁保持 Pending；不得据此关闭 W06。
