# W06 认知 Provider 运行接线

状态：In progress。外部真实允许模型质量验收 Pending；本报告不得用于关闭 W06。

## 范围和实施前冻结

[ADR-0050](../adr/0050-cognitive-provider-deployment.md) 冻结有限 `openai-compatible-chat` 适配器、明确 model/version、逐 tenant 数据授权/secret_ref、固定最小字段和保守成本上界。`model_id` 现有列保存规范 JSON `[model,model_version]`，两部分独立且无歧义进入版本身份；不引入 Schema 23。

[6 组完全合成评估材料](../../tests/fixtures/cognitive/w06-evaluation.json) 与阈值在任何外部评估前登记：摘要事实覆盖 ≥90%，反事实为零；提取 precision ≥90%、recall ≥80%；越权 Evidence/Core/active-task 接受零；同源三次服务重放最多形成一次新资源。尚未执行真实模型评估，不报告虚构质量分数。

## 当前实现

- 受限 0600 部署文件由 CLI/ENV/TOML 选择真实适配器，精确 tenant 的 secret_ref、隐私标签、输入/输出和每次成本上限必须显式授权；不得设置任意 prompt/header。
- 共享 W05 pinned transport 的同一 `request_json` 边界。仍验证 DNS/实际连接 IP、Host/SNI/证书、允许主机/私网、禁止代理和重定向、请求/响应大小、连接/读取/总超时。
- 生产 Worker 不再默认 fake。无配置不领取认知作业，未授权租户不得外呼；API capability 删除不可用声明，Reflection 管理请求返回 not_ready。
- 摘要和提取通过现有 ProviderGovernance、持久成本/熔断及 ReflectionPipeline 落地。模型输入不含 structured_payload/Scope/隐私标签；省略的候选 Scope/标签由服务器固定窗口补齐，显式越权值继续拒绝。
- 四类适配器 Port 均实现；当前管线网络调用为摘要与提取。memory.reconciliation / persona.evaluation 保留确定性协调和 Proposal/Policy 提交，不为增加模型调用而放宽 Canonical 边界。
- 适配器的 retryable/dead 原因通过治理保留，不把 invalid-output/401 强制升级为临时故障。提取前重验来源，最终提交仍重验固定 Watermark、当前 Evidence 与 Outbox lease/generation。

## 验证记录

- 原有认知管线及运行生命周期：15 passed（`/private/tmp/w06-first.log`）。
- `.work-package-runs/W06/local-http-001`：沙箱禁止绑定回环端口，且新增测试使用了错误表名；失败完整保留。
- `local-http-002`：部署 loader 少传 master_key_file 参数，以及测试把 pending 写成 queued；失败完整保留。
- `local-http-003`：15 passed。真实本机 HTTP → 生产 Worker → 非空 Episode 摘要 → Evidence 候选 → 正常 Claim 服务完成；成本持久化为实际部署上界，模型/版本持久身份正确。还覆盖 429/500/401、无效 JSON、配置拒绝、数据授权/输入预算和默认无配置行为。这是隔离服务证据，不能计作真实模型质量验收。
- `structural-001/002`：初始类型问题已修复；最后针对 7 个影响文件的 mypy 已通过。`structural-003` 的 uv 尝试重新构建独立副本并因网络权限失败，未当作成功；后续使用已安装工具及禁止自动 sync 运行。
- `cognitive-regression-001/002` 误包含整个 cognition 目录的大量无关 W03 数量验收，主动终止并保存 stop-reason，未当作通过。
- `cognitive-regression-003`：2197 passed（58.82 秒）；覆盖已有认知管线/Watermark/Evidence/fencing/重放、实际 pinned HTTP/TLS 及生产部署回归。
- `cost-and-http-001`：2134 passed、1 failed；仅新增测试将既有 `rejected_count` 写成 `reject_count`，已修正。该批次成本修复、原管线与领域回归均通过。
- `cost-and-http-002`：22 passed（12.68 秒）；实际 HTTP 失败两次累计成本 200、失败后成功累计 200、第二次被预算拒绝只计首次 100，均与持久预算一致。另证明越权 Evidence 被正常管线拒绝、模型执行期间 SQLite 写可用及真实请求总超时。
- 追加模型身份长度边界定向验收：1 passed（22 deselected）；规范 model/version tuple 严格满足既有 256 字符持久限制。最终影响文件 mypy、Ruff、import boundaries 通过；docs 全库检查仅报告副本按要求未复制的 W01–W05 证据链接，需要根目录合并后复验。

失败/重试成本通过仅内部的 `ProviderUnavailableError.charged_cost_microunits` 传播，不写入公共错误 details；每个已开始尝试的保守预算保留，成功/失败 outcome 均包括先前重试。这样不会将已经发生的消耗隐藏成零。

## 未完成验收

真实允许的外部 endpoint/model/version、数据授权、凭据引用和成本上界尚未提供；需使用冻结材料证明质量与拒绝结果。完成此门禁、根线程合并影响面与最终 make ci 前，W06 保持 In progress。Phase 11/12 保持 Deferred。
