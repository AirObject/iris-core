# Iris Memory Python SDK

Dependency-free asynchronous client for the Core public `/v1` API. Version
`0.11.1` covers observations and cursors, leases, jobs, explicit memory,
recall/search/usage, profiles, personas, identity and administrative resources.
The separate Phase 13 console `/console/v1` API is not part of this SDK.

## Transport limits

The client runs `urllib.request.urlopen` through `asyncio.to_thread`, with a
client-wide timeout (5 seconds by default). It has no per-call deadline,
transport cancellation or SSE client. Cancelling the awaiting coroutine does
not stop its in-flight worker request. `current_persona()` and
`source_cursor()` return dictionaries rather than dedicated response types.
These gaps are tracked in [Phase 12](../../docs/development/phase-12-astrbot-bridge.md);
the SDK is not yet evidence of a working AstrBot integration.

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
