# Iris Memory TypeScript SDK

2026-09-08 新实施项：[Observation 全量上下文与批量总结](../../docs/development/observation-context.md)。统一使用 Observation 保存背景/交互原始事件，复用 Episode 保存分组摘要；自动总结显式开启，背景默认保留 30 天可配置。已接通实现，使用方式与本轮验证进度见链接说明；下文历史验收记录仅代表当时版本。

Dependency-free client for the Core public `/v1` API. Version `0.12.0` covers
observations and cursors, leases, jobs, explicit memory, recall/search/usage,
profiles, personas, identity and administrative resources. The separate
Phase 13 console `/console/v1` API is not part of this SDK.

Adapter-facing reads accept `AbortSignal`; typed Persona and source-cursor
responses and finite SSE polling with `Last-Event-ID` are included. Hosts own
the repeated poll/reconnection loop. Shared contract validators support
forward-compatible optional fields.

When Core advertises `events.checkpoint.v1`, `events({ after, afterEventId, signal })`
can verify that the last saved cursor still names the same visible event before
reading successors. Persist both values from the same accepted `CoreEvent`.
Core returns HTTP 410 (`history_unavailable`) if the checkpoint is missing,
changed or no longer visible. Numeric gaps caused by authorization filtering
remain valid. A 410 is an error, not an empty poll: the host must establish a
read barrier and reconcile history before selecting a new checkpoint. This
source capability does not change an already installed older SDK package.

## Validation

From this directory:

```sh
npm test
npm run typecheck
```

`npm test` builds the SDK and checks fixtures and wire behavior. Core contract
tests exercise the ASGI application; `tools/mock_server.py` is an offline test
double. Consumer acceptance also requires testing the installed registry
package, without aliasing it to this source directory.

## Distribution and host compatibility

The package contains publish metadata and has historical local registry
publication evidence. Public npm publication is still an open acceptance item
in [Phase 11](../../docs/development/phase-11-bellis-adapter.md); this README
does not claim a public release. See the [verification report](../../docs/reports/phase-11-verification.md)
for current tests and limitations.

Core is now `0.12.0` / Schema `14`, while the Bellis Provider defaults and its
compatibility matrix still target Schema `11`. Passing SDK tests alone does
not establish compatibility of that Provider with the current Core. The
release baseline comes from [`version-manifest.json`](../../schemas/version-manifest.json).

### Task 子资源与父修订

步骤、依赖和触发器写入会在同一事务推进父 Task 修订。子资源返回自己的修订；后续更新或转换父 Task 前，先用公共 Task 列表接口读取当前父修订。不要复用创建子资源之前的父修订。幂等重放不额外推进父修订。

## Observation context (0.12.0)

Background messages use `context_kind: "background"` in `observeBatch`. They are ordinary Observations; ingestion and context reads do not call a model.

```typescript
const scope = { agent_id: agentId, space_id: spaceId };
const page = await client.observationContext({ scope, limit: 100 });
if (page.has_more && page.next_cursor) {
  const following = await client.observationContext({ scope, limit: 100, cursor: page.next_cursor });
}
const accepted = await client.summarizeObservations(
  { scope }, { idempotencyKey: "summary-request-1" },
);
```

`ObservationContextResponse` contains raw messages, processing status, Episode summaries, source references and explicit pagination flags. Reading requires `observation-context.v1`; requesting a summary additionally requires `consolidation.v1` and a configured cognitive provider. The summary response acknowledges queue admission. Automatic summaries are opt-in on Core, and the SDK starts no background model work. Background content expires after 30 days by default, while explicitly cited evidence is preserved. See the [full contract and configuration guide](../../docs/development/observation-context.md).
