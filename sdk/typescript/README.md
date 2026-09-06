# Iris Memory TypeScript SDK

Dependency-free client for the Core public `/v1` API. Version `0.11.1` covers
observations and cursors, leases, jobs, explicit memory, recall/search/usage,
profiles, personas, identity and administrative resources. The separate
Phase 13 console `/console/v1` API is not part of this SDK.

Adapter-facing reads accept `AbortSignal`; typed Persona and source-cursor
responses and finite SSE polling with `Last-Event-ID` are included. Hosts own
the repeated poll/reconnection loop. Shared contract validators support
forward-compatible optional fields.

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
