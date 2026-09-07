"""Owner-scoped metadata and cancellation for durable management work."""

from typing import Any

from fastapi import APIRouter, Request
from starlette.concurrency import run_in_threadpool

from iris_memory_core.api.console.auth import body, idempotency_key, security
from iris_memory_core.api.console.errors import ConsoleError
from iris_memory_core.api.console.key_views import decode_page, page_meta, time_us
from iris_memory_core.api.console.routes_auth import Principal
from iris_memory_core.api.console.views import envelope, timestamp
from iris_memory_core.application.console.backup_operations import BackupOperations
from iris_memory_core.application.console.operations import OPERATION_KINDS, ConsoleOperations
from iris_memory_core.domain.console_operations import ConsoleOperation, OperationSummary

router = APIRouter(prefix="/v1")


def operation_view(
    operation: ConsoleOperation | OperationSummary, *, key_id: str
) -> dict[str, Any]:
    return operation_timestamps(ConsoleOperations.metadata(operation, key_id=key_id))


def operation_timestamps(value: dict[str, Any]) -> dict[str, Any]:
    for name in ("created", "started", "finished"):
        instant = value.pop(name + "_us")
        value[name + "_at"] = timestamp(instant) if instant is not None else None
    return value


def _no_query(request: Request) -> None:
    if request.query_params:
        raise ConsoleError("invalid_request", kind="validation_failed", status=400)


@router.get("/operations", operation_id="consoleOperations")
def operations(request: Request, principal: Principal) -> dict[str, Any]:
    service = security(request)
    now = service.clock.now_us()
    limit, after = decode_page(
        request,
        principal,
        now,
        filters=frozenset({"kind", "status", "created_from", "created_before"}),
    )
    query = request.query_params
    try:
        if query.get("kind") is not None and query["kind"] not in OPERATION_KINDS:
            raise ValueError
        lower = time_us(query["created_from"]) if "created_from" in query else None
        upper = time_us(query["created_before"]) if "created_before" in query else None
        if (lower is not None and lower < 0) or (upper is not None and upper < 0):
            raise ValueError
        if lower is not None and upper is not None and lower >= upper:
            raise ValueError
    except (ValueError, TypeError, OverflowError):
        raise ConsoleError("invalid_request", kind="validation_failed", status=400) from None
    rows = ConsoleOperations(service).list_owned(
        principal,
        kind=query.get("kind"),
        status=query.get("status"),
        created_from=lower,
        created_before=upper,
        after=after,
        limit=limit + 1,
    )
    selected = rows[:limit]
    value = envelope(
        request, [operation_view(row, key_id=principal.key.id) for row in selected], now_us=now
    )
    value["meta"]["page"] = page_meta(
        request,
        principal,
        now,
        limit,
        last=(selected[-1].created_us, selected[-1].id) if selected else None,
        has_more=len(rows) > limit,
    )
    return value


@router.get("/operations/{id}", operation_id="consoleOperation")
def detail(request: Request, principal: Principal, id: str) -> dict[str, Any]:
    _no_query(request)
    service = security(request)
    operation = ConsoleOperations(service).detail(principal, id)
    return envelope(
        request, operation_view(operation, key_id=principal.key.id), now_us=service.clock.now_us()
    )


@router.get("/operations/{id}/problems", operation_id="consoleOperationProblems")
def problems(request: Request, principal: Principal, id: str) -> dict[str, Any]:
    service = security(request)
    now = service.clock.now_us()
    limit, after = decode_page(request, principal, now)
    rows = ConsoleOperations(service).problems(
        principal, id, after=after[0] if after else -2, limit=limit + 1
    )
    selected = rows[:limit]
    value = envelope(
        request,
        [
            {
                "input_index": row.input_index if row.input_index >= 0 else None,
                "code": row.code,
                "created_at": timestamp(row.created_us),
            }
            for row in selected
        ],
        now_us=now,
    )
    value["meta"]["page"] = page_meta(
        request,
        principal,
        now,
        limit,
        last=(selected[-1].input_index, id) if selected else None,
        has_more=len(rows) > limit,
    )
    return value


@router.post("/operations/{id}:cancel", operation_id="consoleCancelOperation")
async def cancel(request: Request, principal: Principal, id: str) -> dict[str, Any]:
    _no_query(request)
    value = await body(request, "ConsoleOperationCancelRequest")
    service = security(request)
    operation = await run_in_threadpool(
        ConsoleOperations(service).cancel,
        principal,
        id,
        reason=value["reason_code"],
        idempotency_key=idempotency_key(request),
    )
    return envelope(
        request, operation_view(operation, key_id=principal.key.id), now_us=service.clock.now_us()
    )


@router.post("/backups", operation_id="consoleCreateBackup", status_code=202)
async def create_backup(request: Request, principal: Principal) -> dict[str, Any]:
    _no_query(request)
    value = await body(request, "ConsoleBackupCreateRequest")
    service = security(request)
    operation = await run_in_threadpool(
        BackupOperations(service, request.app.state.archives).create,
        principal,
        reason=value["reason_code"],
        idempotency_key=idempotency_key(request),
    )
    return envelope(
        request, operation_view(operation, key_id=principal.key.id), now_us=service.clock.now_us()
    )
