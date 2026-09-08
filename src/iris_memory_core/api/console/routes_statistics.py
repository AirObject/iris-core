"""Registry-discovered, authorized Console statistics and explicit backfill."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from iris_memory_core.api.console.auth import body, idempotency_key, security
from iris_memory_core.api.console.errors import ConsoleError
from iris_memory_core.api.console.key_views import time_us
from iris_memory_core.api.console.routes_auth import Principal
from iris_memory_core.api.console.routes_operations import operation_view
from iris_memory_core.api.console.views import envelope, timestamp
from iris_memory_core.application.console.statistics import Statistics, scope_fingerprint
from iris_memory_core.application.console.statistics_operations import StatisticsOperations
from iris_memory_core.domain.statistics import DIMENSIONS

router = APIRouter(prefix="/v1/stats")


def statistics_view(request: Request, result: dict[str, Any]) -> dict[str, Any]:
    meta = result["meta"]
    now = meta.pop("as_of_us")
    for field in ("computed_at", "coverage_from"):
        instant = meta.pop(field + "_us")
        meta[field] = timestamp(instant) if instant is not None else None
    for point in result["data"]:
        instant = point.pop("bucket_us")
        point["bucket"] = timestamp(instant) if instant is not None else None
    value = envelope(request, result["data"], now_us=now)
    value["meta"].update(meta)
    return value


@router.get("/metrics", operation_id="consoleStatsMetrics")
def metrics(request: Request, principal: Principal) -> dict[str, Any]:
    if request.query_params:
        raise ConsoleError("invalid_request", kind="validation_failed", status=400)
    context = security(request)
    specs, coverage = Statistics(context).registry(principal)
    result = envelope(
        request,
        [
            spec.as_dict(timestamp(coverage) if spec.metric_id.startswith("recall.") else None)
            for spec in specs
        ],
        now_us=context.clock.now_us(),
    )
    result["meta"].update(
        {
            "source": "live",
            "computed_at": timestamp(context.clock.now_us()),
            "stale": False,
            "coverage_from": timestamp(coverage),
            "scope_fingerprint": scope_fingerprint(principal),
            "warnings": [],
            "partial": False,
            "rollup_lag_us": None,
            "instance_local": False,
        }
    )
    return result


def _query(request: Request, principal: Principal, panel: str) -> dict[str, Any]:
    allowed = {"metric_id", "granularity", "group_by", "from", "to", *DIMENSIONS}
    if set(request.query_params) - allowed or any(
        len(request.query_params.getlist(key)) != 1 for key in request.query_params
    ):
        raise ConsoleError("invalid_request", kind="validation_failed", status=400)
    query = request.query_params
    try:
        lower = time_us(query["from"]) if "from" in query else None
        upper = time_us(query["to"]) if "to" in query else None
    except (ValueError, OverflowError, TypeError):
        raise ConsoleError("invalid_request", kind="validation_failed", status=400) from None
    result = Statistics(security(request)).query(
        principal,
        panel,
        metric_id=query.get("metric_id"),
        granularity=query.get("granularity", "hour"),
        lower=lower,
        upper=upper,
        group_by=query.get("group_by") or None,
        filters={key: query[key] for key in DIMENSIONS if key in query},
    )
    return statistics_view(request, result)


@router.get("/overview", operation_id="consoleStatsOverview")
def overview(request: Request, principal: Principal) -> dict[str, Any]:
    return _query(request, principal, "overview")


@router.get("/timeseries", operation_id="consoleStatsTimeseries")
def timeseries(request: Request, principal: Principal) -> dict[str, Any]:
    return _query(request, principal, "timeseries")


@router.get("/pipeline", operation_id="consoleStatsPipeline")
def pipeline(request: Request, principal: Principal) -> dict[str, Any]:
    return _query(request, principal, "pipeline")


@router.get("/projections", operation_id="consoleStatsProjections")
def projections(request: Request, principal: Principal) -> dict[str, Any]:
    return _query(request, principal, "projections")


@router.get("/recall", operation_id="consoleStatsRecall")
def recall(request: Request, principal: Principal) -> dict[str, Any]:
    return _query(request, principal, "recall")


@router.get("/providers", operation_id="consoleStatsProviders")
def providers(request: Request, principal: Principal) -> dict[str, Any]:
    return _query(request, principal, "providers")


@router.get("/storage", operation_id="consoleStatsStorage")
def storage(request: Request, principal: Principal) -> dict[str, Any]:
    return _query(request, principal, "storage")


@router.get("/security", operation_id="consoleStatsSecurity")
def security_panel(request: Request, principal: Principal) -> dict[str, Any]:
    return _query(request, principal, "security")


@router.post("/rollups:backfill", operation_id="consoleStatsBackfill")
async def backfill(request: Request, principal: Principal) -> JSONResponse:
    if request.query_params:
        raise ConsoleError("invalid_request", kind="validation_failed", status=400)
    payload = await body(request, "ConsoleStatisticsBackfillRequest")
    context = security(request)
    operation = await run_in_threadpool(
        StatisticsOperations(context).create,
        principal,
        lower=time_us(payload["from"]),
        upper=time_us(payload["to"]),
        reason=payload["reason_code"],
        idempotency_key=idempotency_key(request),
    )
    return JSONResponse(
        envelope(
            request,
            operation_view(operation, key_id=principal.key.id),
            now_us=context.clock.now_us(),
        ),
        status_code=202,
    )
