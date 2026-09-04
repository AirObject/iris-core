"""HTTP transport boundary — intentionally empty until Phase 10.

Phases 0-9 deliver application-layer services plus the generated contract
and a mock server; the published OpenAPI is a frozen contract shape, not a
running server. The ASGI application, authentication, AccessContext
construction, error-envelope mapping and the ``serve``/``worker`` process
entry points are Phase 10 deliverables (ADR-0017 §3).
"""
