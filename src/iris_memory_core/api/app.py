"""FastAPI ASGI transport driven by the frozen generated OpenAPI contract."""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response, StreamingResponse
from jsonschema import Draft202012Validator
from pydantic import ConfigDict, TypeAdapter, ValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from iris_memory_core.api.console.config import ConsoleConfig
from iris_memory_core.api.errors import (
    domain_error_handler,
    envelope,
    http_error_handler,
    unknown_error_handler,
    validation_error_handler,
)
from iris_memory_core.application.backpressure import BackpressureConfig, BackpressureGauge
from iris_memory_core.application.health import HealthService
from iris_memory_core.application.ports.transaction import UnitOfWork
from iris_memory_core.application.security import CredentialService
from iris_memory_core.business import (
    BusinessRuntime as TransportRuntime,
)
from iris_memory_core.business import (
    _dereference_schema,
    _jsonable,
    _load_contract,
    _narrow,
    _request_schema,
    _require_management,
    _resolve_schema,
    _validate_parameters,
)
from iris_memory_core.domain.access import AccessContext
from iris_memory_core.domain.errors import (
    AccessDeniedError,
    DomainError,
    HistoryUnavailableError,
    InvalidRequestError,
    NotReadyError,
    UnsupportedVersionError,
)
from iris_memory_core.recall_runtime import RecallAssemblyConfig
from iris_memory_core.storage.admin_archives import AdminArchiveService
from iris_memory_core.storage.uow import Store

HTTP_METHODS = frozenset({"get", "post", "put", "patch", "delete"})
_STRICT_OBJECT = TypeAdapter(dict[str, Any], config=ConfigDict(strict=True))


async def _body(
    request: Request, contract: Mapping[str, Any], operation: Mapping[str, Any]
) -> dict[str, Any]:
    schema = _request_schema(operation)
    if schema is None:
        return {}
    try:
        if operation.get("operationId") == "revalidateRecall":
            chunks = bytearray()
            async for chunk in request.stream():
                if len(chunks) + len(chunk) > 1024 * 1024:
                    raise InvalidRequestError("revalidation body exceeds 1 MiB")
                chunks.extend(chunk)
            raw = json.loads(chunks)
        else:
            raw = await request.json()
        value = _STRICT_OBJECT.validate_python(raw, strict=True)
    except (json.JSONDecodeError, UnicodeDecodeError, ValidationError):
        raise InvalidRequestError("request body must be a strict JSON object") from None
    resolved = cast(Mapping[str, Any], _dereference_schema(contract, schema))
    errors = sorted(
        Draft202012Validator(resolved).iter_errors(value), key=lambda item: list(item.path)
    )
    if errors:
        raise InvalidRequestError("request body failed the frozen schema")
    return value


def _authorization_token(request: Request) -> str:
    value = request.headers.get("authorization", "")
    scheme, separator, token = value.partition(" ")
    if separator != " " or scheme.lower() != "bearer" or not token or " " in token:
        raise AccessDeniedError("bearer authentication required", details={"authentication": True})
    return token


def _authenticate(request: Request, credentials: CredentialService) -> AccessContext:
    try:
        return credentials.authenticate(_authorization_token(request))
    except AccessDeniedError:
        raise AccessDeniedError(
            "invalid bearer credential", details={"authentication": True}
        ) from None


def _sample(contract: Mapping[str, Any], schema: Mapping[str, Any], *, name: str) -> object:
    schema = _resolve_schema(contract, schema)
    if "const" in schema:
        return schema["const"]
    enum = schema.get("enum")
    if isinstance(enum, list) and enum:
        return enum[0]
    alternatives = schema.get("oneOf", schema.get("anyOf"))
    if isinstance(alternatives, list) and alternatives:
        chosen = next(
            (
                item
                for item in alternatives
                if isinstance(item, Mapping) and item.get("type") != "null"
            ),
            alternatives[0],
        )
        return _sample(contract, cast(Mapping[str, Any], chosen), name=name)
    kind = schema.get("type")
    if isinstance(kind, list):
        kind = next((item for item in kind if item != "null"), "null")
    if kind == "object" or "properties" in schema:
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        if not isinstance(properties, Mapping) or not isinstance(required, list):
            return {}
        return {
            str(key): _sample(contract, cast(Mapping[str, Any], properties[key]), name=str(key))
            for key in required
            if key in properties
        }
    if kind == "array":
        count = max(0, int(schema.get("minItems", 0)))
        item_schema = cast(Mapping[str, Any], schema.get("items", {}))
        return [_sample(contract, item_schema, name=name) for _ in range(count)]
    if kind == "integer":
        return max(int(schema.get("minimum", 0)), 1 if "revision" in name else 0)
    if kind == "number":
        return float(schema.get("minimum", 0))
    if kind == "boolean":
        return False
    if kind == "null":
        return None
    minimum = int(schema.get("minLength", 1))
    base = f"{name}-value"
    return base + "x" * max(0, minimum - len(base))


def _success_sample(
    contract: Mapping[str, Any], operation: Mapping[str, Any]
) -> tuple[int, object]:
    responses = cast(Mapping[str, Any], operation.get("responses", {}))
    code = next((int(key) for key in sorted(responses) if str(key).startswith("2")), 200)
    response = responses.get(str(code), {})
    if not isinstance(response, Mapping):
        return code, {}
    content = response.get("content", {})
    if not isinstance(content, Mapping):
        return code, {}
    media = content.get("application/json")
    if not isinstance(media, Mapping) or not isinstance(media.get("schema"), Mapping):
        return code, {}
    return code, _sample(contract, cast(Mapping[str, Any], media["schema"]), name="value")


def create_app(
    uow: UnitOfWork,
    *,
    credentials: CredentialService | None = None,
    contract_path: Path | None = None,
    sse_enabled: bool = True,
    backup_root: Path | None = None,
    export_root: Path | None = None,
    backup_signing_key: bytes | None = None,
    enable_console: bool = False,
    console_config: ConsoleConfig | None = None,
    recall_config: RecallAssemblyConfig | None = None,
    cognitive_tenants: frozenset[str] = frozenset(),
    observation_context_config: Any = None,
) -> FastAPI:
    contract = _load_contract(contract_path)
    credential_service = credentials or CredentialService(uow, cast(Any, uow).clock)
    archives: AdminArchiveService | None = None
    if isinstance(uow, Store):
        data_root = uow.runtime.database.parent
        archives = AdminArchiveService(
            uow,
            backup_root=backup_root or data_root / "backups",
            export_root=export_root or data_root / "exports",
            backup_signing_key=backup_signing_key,
        )
    runtime = TransportRuntime(
        uow,
        credential_service,
        archives,
        sse_enabled=sse_enabled,
        recall_config=recall_config,
        cognitive_tenants=cognitive_tenants,
        observation_context_config=observation_context_config,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.accepting = True
        app.state.ready = True
        yield
        app.state.ready = False
        app.state.accepting = False

    app = FastAPI(
        title="Iris Memory Core",
        version=str(contract["info"]["version"]),
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.accepting = True
    app.state.ready = True
    app.state.inflight = 0
    app.state.runtime = runtime
    app.state.frozen_openapi = contract
    app.openapi = lambda: contract  # type: ignore[method-assign]
    if enable_console:
        from iris_memory_core.api.console.app import create_console_app

        if not isinstance(uow, Store):
            raise ValueError("Console requires a configured SQLite Store")
        app.mount(
            "/console",
            create_console_app(
                store=uow,
                config=console_config,
                archives=archives,
                embedding_runtime=runtime.projections.embedding_runtime,
                provider_generations=runtime.projections.provider_generations,
            ),
            name="console",
        )
    app.add_exception_handler(DomainError, domain_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(RequestValidationError, validation_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(StarletteHTTPException, http_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(Exception, unknown_error_handler)

    @app.middleware("http")
    async def transport_context(request: Request, call_next: Any) -> Response:
        request.state.request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
        request.state.trace_id = request.headers.get("traceparent", "")[:128]
        if not app.state.accepting and request.url.path != "/health/live":
            return envelope(
                request,
                code="not_ready",
                message="service is draining",
                retryable=True,
                status_code=503,
            )
        app.state.inflight += 1
        try:
            response = await call_next(request)
        finally:
            app.state.inflight -= 1
        response.headers["X-Request-ID"] = request.state.request_id
        return cast(Response, response)

    async def event_stream(request: Request) -> StreamingResponse:
        if not sse_enabled:
            raise NotReadyError("event stream capability is disabled")
        access = _authenticate(request, credential_service)
        raw_cursor = request.headers.get("last-event-id", request.query_params.get("after", "0"))
        try:
            cursor = int(raw_cursor)
        except ValueError:
            raise InvalidRequestError("SSE cursor must be an integer") from None
        if cursor < 0 or cursor > 2**63 - 1:
            raise InvalidRequestError("SSE cursor is outside the supported range")
        expected_event_id = request.headers.get("x-iris-after-event-id")
        if expected_event_id is not None and (
            cursor == 0
            or not 1 <= len(expected_event_id) <= 512
            or any(ord(char) < 33 or ord(char) > 126 for char in expected_event_id)
        ):
            raise InvalidRequestError("SSE checkpoint requires a cursor and valid event identity")

        # Validate the saved identity and read its successors in one snapshot,
        # before sending a 200 response. Filtering may legitimately skip numeric
        # cursors; a missing, changed or no-longer-visible anchor needs revalidation.
        with uow.read() as tx:
            if expected_event_id is not None:
                anchor = tx.reflection.events_after(
                    tenant_id=access.tenant_id,
                    agent_ids=sorted(access.agent_ids),
                    space_group_ids=sorted(access.allowed_space_group_ids),
                    space_ids=sorted(access.allowed_space_ids),
                    after_cursor=cursor - 1,
                    limit=1,
                )
                if not anchor or anchor[0].cursor != cursor or anchor[0].id != expected_event_id:
                    raise HistoryUnavailableError("SSE checkpoint cannot be verified")
            events = tx.reflection.events_after(
                tenant_id=access.tenant_id,
                agent_ids=sorted(access.agent_ids),
                space_group_ids=sorted(access.allowed_space_group_ids),
                space_ids=sorted(access.allowed_space_ids),
                after_cursor=cursor,
                limit=100,
            )

        async def generate() -> AsyncIterator[str]:
            if not events:
                yield ": keep-alive\n\n"
                return
            for event in events:
                if await request.is_disconnected():
                    return
                value = {
                    "event_id": event.id,
                    "event_type": event.event_type,
                    "occurred_at": datetime.fromtimestamp(event.occurred_us / 1_000_000, tz=UTC)
                    .isoformat()
                    .replace("+00:00", "Z"),
                    "source_watermark": event.source_watermark,
                    "resource_refs": list(event.resource_refs),
                }
                data = json.dumps(value, separators=(",", ":"))
                yield f"id: {event.cursor}\nevent: {event.event_type}\ndata: {data}\n\n"

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    app.add_api_route("/v1/events", event_stream, methods=["GET"], name="streamEvents")

    for path, item in cast(Mapping[str, Any], contract["paths"]).items():
        if path == "/v1/events" or not isinstance(item, Mapping):
            continue
        for method, raw_operation in item.items():
            if method not in HTTP_METHODS or not isinstance(raw_operation, Mapping):
                continue
            operation = dict(raw_operation)
            operation_id = str(operation["operationId"])

            def make_endpoint(frozen_operation: dict[str, Any], frozen_operation_id: str) -> Any:
                async def endpoint(request: Request) -> Response:
                    if frozen_operation_id == "getLiveness":
                        _validate_parameters(request, contract, frozen_operation)
                        return JSONResponse({"status": "live"})
                    # Authentication precedes every other check so an
                    # anonymous caller learns nothing about parameter shapes,
                    # resource existence or request-body schemas: the reply is
                    # the same access_denied envelope for all of them.
                    access = _authenticate(request, credential_service)
                    _validate_parameters(request, contract, frozen_operation)
                    body = await _body(request, contract, frozen_operation)
                    _narrow(access, body, request)
                    _require_management(access, frozen_operation_id)
                    if frozen_operation_id == "getReadiness":
                        return _readiness(app, uow, access)
                    if frozen_operation_id == "getMetrics":
                        return _metrics(app, uow, access)
                    result = runtime.dispatch(frozen_operation_id, access, request, body)
                    if result is None:
                        raise UnsupportedVersionError(
                            "operation is present in the contract but unavailable at runtime"
                        )
                    status, value = result
                    return JSONResponse(cast(Any, _jsonable(value)), status_code=status)

                return endpoint

            app.add_api_route(
                path,
                make_endpoint(operation, operation_id),
                methods=[method.upper()],
                name=operation_id,
                include_in_schema=False,
            )
    return app


def _readiness(app: FastAPI, uow: UnitOfWork, access: AccessContext) -> JSONResponse:
    database_path = uow.runtime.database if isinstance(uow, Store) else None
    projections = app.state.runtime.projections
    report = HealthService(
        uow,
        cast(Any, uow).clock,
        gauge=BackpressureGauge(BackpressureConfig(), database_path=database_path),
        vector_required=projections.vector_required,
        vector_capability=(
            (lambda: projections.vector.capability_available(access.tenant_id))
            if projections.vector
            else lambda: False
        ),
    ).readiness()
    try:
        with uow.read() as tx:
            circuit_rows = (
                cast(Any, tx)
                .raw()
                .execute(
                    "SELECT provider_kind,state FROM provider_circuit_states "
                    "WHERE tenant_id=? ORDER BY provider_kind",
                    (access.tenant_id,),
                )
                .fetchall()
            )
    except Exception:
        circuit_rows = ()
    checks = dict(report.checks)
    circuits = {str(row["provider_kind"]): str(row["state"]) for row in circuit_rows}
    checks["provider_circuits"] = circuits
    reasons = list(report.reasons)
    status = report.status
    if any(state in {"open", "half_open"} for state in circuits.values()):
        if "provider_circuit_open" not in reasons:
            reasons.append("provider_circuit_open")
        if status == "ready":
            status = "degraded"
    if not app.state.ready:
        status = "not_ready"
        reasons.append("process_draining")
    return JSONResponse(
        {"status": status, "checks": checks, "reasons": sorted(set(reasons))},
        status_code=503 if status == "not_ready" else 200,
    )


def _metrics(app: FastAPI, uow: UnitOfWork, access: AccessContext) -> JSONResponse:
    with uow.read() as tx:
        connection = cast(Any, tx).raw()
        job_rows = connection.execute(
            "SELECT job_kind,status,COUNT(*) AS count FROM outbox_jobs "
            "WHERE tenant_id=? GROUP BY job_kind,status ORDER BY job_kind,status",
            (access.tenant_id,),
        ).fetchall()
        candidate_rows = connection.execute(
            "SELECT decision,COUNT(*) AS count FROM cognitive_candidates "
            "WHERE tenant_id=? GROUP BY decision ORDER BY decision",
            (access.tenant_id,),
        ).fetchall()
        provider_rows = connection.execute(
            "SELECT provider_kind,outcome,COUNT(*) AS count,"
            "COALESCE(SUM(cost_microunits),0) AS cost FROM provider_outcomes "
            "WHERE tenant_id=? GROUP BY provider_kind,outcome "
            "ORDER BY provider_kind,outcome",
            (access.tenant_id,),
        ).fetchall()
        oldest_row = connection.execute(
            "SELECT MIN(created_us) AS oldest FROM outbox_jobs "
            "WHERE tenant_id=? AND status IN ('pending','retryable','leased')",
            (access.tenant_id,),
        ).fetchone()
    counters = [
        {
            "name": "iris_outbox_jobs",
            "labels": {"job_kind": str(row["job_kind"]), "status": str(row["status"])},
            "value": int(row["count"]),
        }
        for row in job_rows
    ]
    counters.extend(
        {
            "name": "iris_reflection_candidates",
            "labels": {"decision": str(row["decision"])},
            "value": int(row["count"]),
        }
        for row in candidate_rows
    )
    for row in provider_rows:
        labels = {
            "provider_kind": str(row["provider_kind"]),
            "outcome": str(row["outcome"]),
        }
        counters.append(
            {"name": "iris_provider_outcomes", "labels": labels, "value": int(row["count"])}
        )
        counters.append(
            {"name": "iris_provider_cost_microunits", "labels": labels, "value": int(row["cost"])}
        )
    oldest = oldest_row["oldest"] if oldest_row is not None else None
    gauges = [{"name": "iris_http_inflight", "labels": {}, "value": app.state.inflight}]
    gauges.append(
        {
            "name": "iris_outbox_oldest_pending_age_us",
            "labels": {},
            "value": 0 if oldest is None else max(0, cast(Any, uow).clock.now_us() - int(oldest)),
        }
    )
    return JSONResponse({"counters": counters, "gauges": gauges})


__all__ = ["HTTP_METHODS", "TransportRuntime", "create_app"]
