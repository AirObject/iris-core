# Iris Memory Python SDK

2026-09-08 新实施项：[Observation 全量上下文与批量总结](../../docs/development/observation-context.md)。统一使用 Observation 保存背景/交互原始事件，复用 Episode 保存分组摘要；自动总结显式开启，背景默认保留 30 天可配置。已接通实现，使用方式与本轮验证进度见链接说明；下文历史验收记录仅代表当时版本。

Core-independent asynchronous client using httpx for the Core public `/v1` API. Version
`0.12.0` covers observations and cursors, leases, jobs, explicit memory,
recall/search/usage, profiles, personas, identity and administrative resources.
The separate Phase 13 console `/console/v1` API is not part of this SDK.

## Transport limits

The client uses a bounded asynchronous connection pool. The default deadline
is 5 seconds including admission queue time, with 8 in-flight and 64 pending
requests. Use `async with AsyncIrisMemoryClient(...)` or `await client.aclose()`
to cancel outstanding requests and release owned connections. A supplied
`http_client=` remains owned by the caller. No retries or SSE subscription are
added implicitly.

Transport failures expose `IrisMemoryTransportError` with a request ID and
`result_unknown` flag; preserve the original idempotency key to reconcile
uncertain writes. Socket timeouts, whole-call deadlines and caller cancellation
have distinct outcomes. Business failures retain `IrisMemoryApiError.envelope`.
`current_persona()` and `source_cursor()` return dictionaries, while negotiation
returns the existing capability DTO. See the [plugin integration guide](../../docs/development/plugin-integration.md).

## Validation

From the repository root:

```sh
UV_CACHE_DIR=.uv-cache uv run pytest sdk/python/tests -q --no-cov
```

This checks shared fixtures against the checkout. It does not verify registry
installation or a real host process. Core contract tests exercise the ASGI
application; `tools/mock_server.py` is an offline test double. Supported
release combinations and SDK distribution must be verified separately before
host release; the current Core package/Schema version is recorded in
[`version-manifest.json`](../../schemas/version-manifest.json).

### Task 子资源与父修订

步骤、依赖和触发器写入会在同一事务推进父 Task 修订。子资源返回自己的修订；后续更新或转换父 Task 前，先用公共 Task 列表接口读取当前父修订。不要复用创建子资源之前的父修订。幂等重放不额外推进父修订。

## 异步关闭与插件对接

本开发快照使用 httpx 异步连接池，仍不依赖 Core/FAISS/NumPy/Web 框架。推荐 `async with AsyncIrisMemoryClient(...)`；支持 `max_in_flight`、`max_pending`、`aclose(timeout_seconds=...)`。`http_client=` 为借用资源，关闭 SDK 不关闭宿主客户端。`negotiate(required_capabilities=...)` 与 `get_entity_profile(...)` 已支持。传输错误的 `result_unknown` 表示写入可能已提交，不自动重试。完整语义见 Core 的插件接入指南。

## Observation 原始上下文与总结（0.12.0）

`observe_batch` 的记录可使用 `context_kind="background"`，与直接交互共用 Observation。写入和读取不调用模型。`observation_context` 返回原文、结构化信息、处理状态、已有 Episode 摘要及分页标记：

```python
body = {"scope": {"agent_id": agent_id, "space_id": space_id}, "limit": 100}
page = await client.observation_context(body)
if page["has_more"]:
    following = await client.observation_context({**body, "cursor": page["next_cursor"]})
accepted = await client.summarize_observations(
    {"scope": body["scope"]}, idempotency_key="summary-request-1"
)
```

读取需要 `observation-context.v1`；总结还需要 `consolidation.v1` 和配置好的认知 Provider。总结返回入队结果，不代表已经生成记忆。自动总结默认关闭；配置与费用由 Core 的 Worker/Embedded 管理，SDK 不创建本地计时任务。背景原文默认 30 天，仅被摘要或其他记忆明确引用的来源继续保留。完整语义见 [Observation 接入说明](../../docs/development/observation-context.md)。
