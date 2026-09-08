# 认知 Provider 部署

W06 采用 [ADR-0050](../adr/0050-cognitive-provider-deployment.md)。生产默认无认知模型；未配置时 Worker 不领取认知任务，API 不声明 `consolidation.v1` / `reflection.v1`，在线 Canonical/Recall 保持可用。已有待处理任务保留，不伪造空候选完成。

API 与 Worker 使用同一 `--cognitive-config-file /absolute/private/cognitive.json`；等价环境变量为 `IRIS_MEMORY_COGNITIVE_CONFIG_FILE`，TOML 为 `[service].cognitive_config_file`。优先级 CLI > 环境 > TOML。文件必须是当前用户持有、普通单链接、无符号链接且权限不宽于 0600；重启两个进程生效。明文凭据仅存在于明确授权的 env/file 引用。

配置结构（下例 model/version 和授权号均为占位值，不可作为外部评估批准）：

```json
{
  "schema_version": 1,
  "outbound": {"allowed_hosts": ["provider.example.invalid"]},
  "limits": {
    "extraction": {"timeout_seconds": 5, "max_retries": 1, "max_qps": 1, "daily_budget_microunits": 10000, "concurrency": 1, "breaker_failures": 3, "breaker_cooldown_seconds": 30},
    "summarization": {"timeout_seconds": 5, "max_retries": 1, "max_qps": 1, "daily_budget_microunits": 10000, "concurrency": 1, "breaker_failures": 3, "breaker_cooldown_seconds": 30},
    "reconciliation": {"timeout_seconds": 5, "max_retries": 1, "max_qps": 1, "daily_budget_microunits": 10000, "concurrency": 1, "breaker_failures": 3, "breaker_cooldown_seconds": 30},
    "persona_evolution": {"timeout_seconds": 5, "max_retries": 1, "max_qps": 1, "daily_budget_microunits": 10000, "concurrency": 1, "breaker_failures": 3, "breaker_cooldown_seconds": 30}
  },
  "tenants": {
    "explicit-tenant-id": {
      "adapter": "openai-compatible-chat",
      "endpoint": "https://provider.example.invalid/v1/chat/completions",
      "model": "operator-approved-model",
      "model_version": "operator-approved-immutable-version",
      "secret_ref": "env:IRIS_COGNITIVE_KEY",
      "authorization": {
        "authorization_id": "operator-approved-data-and-cost-record",
        "privacy_labels": [],
        "max_input_chars": 4000,
        "max_output_tokens": 512,
        "request_cost_microunits": 100
      }
    }
  }
}
```

部署方确认 `request_cost_microunits` 是在已选 model/version 和输入/输出上限下的每次尝试成本上界；每日预算按 tenant/Port 持久预扣，重启不清零。模型返回的 usage 不降低扣额。预算单位由部署方统一定义，不把字符数假装真实账单。

出站仅包含窗口内 observation 的 id/revision/role/content，以及固定系统提示和模型/输出参数；structured_payload、隐私标签、Scope、租户授权、凭据和存储路径不进正文。隐私标签必须全部属于精确 tenant 授权，否则请求拒绝。除授权的开发回环外必须 HTTPS；`--development-cognitive` 与 `outbound.allow_loopback=true` 必须同时开启才能测试 HTTP 回环。

受支持的四个适配器 Port 是 extraction/summarization/reconciliation/persona_evolution。现有生产管线在网络边界调用前两者；后两类作业的最终落地沿用确定性协调与 Persona Proposal/Policy。任何模型输出都只是候选，仍需当前 Evidence、Scope、隐私和 Outbox fencing 验证。

[预冻结质量评估集](../../tests/fixtures/cognitive/w06-evaluation.json) 必须使用允许的真实模型评估。隔离 HTTP fixture 成功不能替代质量验收；W06 门禁与未满足项见[报告](../reports/w06-cognitive-provider.md)。
