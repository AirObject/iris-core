"""Dependency-free asynchronous HTTP client skeleton."""

from __future__ import annotations

import asyncio
import json
from typing import Any, cast
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from iris_memory_sdk.models import CapabilitiesEnvelope, ErrorEnvelope


class IrisMemoryApiError(RuntimeError):
    def __init__(self, envelope: ErrorEnvelope) -> None:
        self.envelope = envelope
        super().__init__(f"{envelope.code}: {envelope.message}")


class AsyncIrisMemoryClient:
    def __init__(self, base_url: str, *, timeout_seconds: float = 5.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds

    async def capabilities(self) -> CapabilitiesEnvelope:
        value = await asyncio.to_thread(self._request_json, "GET", "/v1/capabilities", None)
        return CapabilitiesEnvelope.from_value(value)

    async def negotiate(self, api_versions: tuple[str, ...] = ("v1",)) -> CapabilitiesEnvelope:
        value = await asyncio.to_thread(
            self._request_json,
            "POST",
            "/v1/negotiation",
            {"api_versions": list(api_versions)},
        )
        return CapabilitiesEnvelope.from_value(value)

    # -- Phase 2: observation journal ------------------------------------

    async def observe_batch(
        self,
        records: list[dict[str, Any]],
        *,
        idempotency_key: str | None = None,
        lease_id: str | None = None,
        lease_epoch: int | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"records": records}
        if lease_id is not None:
            body["lease_id"] = lease_id
        if lease_epoch is not None:
            body["lease_epoch"] = lease_epoch
        value = await asyncio.to_thread(
            self._request_json,
            "POST",
            "/v1/observations:batch",
            body,
            extra_headers={"Idempotency-Key": idempotency_key} if idempotency_key else None,
        )
        return cast(dict[str, Any], value)

    async def source_cursor(self, agent_id: str, source_stream: str) -> dict[str, Any]:
        from urllib.parse import quote

        stream = quote(source_stream, safe="")
        agent = quote(agent_id, safe="")
        path = f"/v1/observations/cursors/{stream}?agent_id={agent}"
        value = await asyncio.to_thread(self._request_json, "GET", path, None)
        return cast(dict[str, Any], value)

    # -- Phase 2: active surface ------------------------------------------

    async def acquire_surface_lease(
        self,
        agent_id: str,
        *,
        holder_app_instance_id: str,
        ttl_us: int,
        priority: int = 0,
        allow_preempt: bool = False,
        reason: str | None = None,
    ) -> dict[str, Any]:
        value = await asyncio.to_thread(
            self._request_json,
            "POST",
            "/v1/active-surfaces:acquire",
            {
                "agent_id": agent_id,
                "holder_app_instance_id": holder_app_instance_id,
                "ttl_us": ttl_us,
                "priority": priority,
                "allow_preempt": allow_preempt,
                **({"reason": reason} if reason is not None else {}),
            },
        )
        return cast(dict[str, Any], value)

    async def heartbeat_surface_lease(
        self,
        lease_id: str,
        *,
        lease_epoch: int,
        holder_app_instance_id: str,
        ttl_us: int,
    ) -> dict[str, Any]:
        from urllib.parse import quote

        value = await asyncio.to_thread(
            self._request_json,
            "POST",
            f"/v1/active-surfaces/{quote(lease_id, safe='')}:heartbeat",
            {
                "lease_epoch": lease_epoch,
                "holder_app_instance_id": holder_app_instance_id,
                "ttl_us": ttl_us,
            },
        )
        return cast(dict[str, Any], value)

    async def release_surface_lease(
        self,
        lease_id: str,
        *,
        lease_epoch: int,
        holder_app_instance_id: str,
        reason: str | None = None,
    ) -> dict[str, Any]:
        from urllib.parse import quote

        value = await asyncio.to_thread(
            self._request_json,
            "POST",
            f"/v1/active-surfaces/{quote(lease_id, safe='')}:release",
            {
                "lease_epoch": lease_epoch,
                "holder_app_instance_id": holder_app_instance_id,
                **({"reason": reason} if reason is not None else {}),
            },
        )
        return cast(dict[str, Any], value)

    async def current_surface_lease(self, agent_id: str) -> dict[str, Any] | None:
        from urllib.parse import quote

        value = await asyncio.to_thread(
            self._request_json,
            "GET",
            f"/v1/active-surfaces/current?agent_id={quote(agent_id, safe='')}",
            None,
        )
        return cast(dict[str, Any] | None, value)

    # -- Phase 2: health -----------------------------------------------------

    async def readiness(self) -> dict[str, Any]:
        value = await asyncio.to_thread(self._request_json, "GET", "/health/ready", None)
        return cast(dict[str, Any], value)

    # -- Phase 2: admin plane (jobs & schedules) ------------------------------

    async def list_admin_jobs(
        self,
        *,
        status: str | None = None,
        job_kind: str | None = None,
    ) -> dict[str, Any]:
        from urllib.parse import quote

        query = "&".join(
            f"{key}={quote(value, safe='')}"
            for key, value in (("status", status), ("job_kind", job_kind))
            if value is not None
        )
        path = "/v1/admin/jobs" + (f"?{query}" if query else "")
        value = await asyncio.to_thread(self._request_json, "GET", path, None)
        return cast(dict[str, Any], value)

    async def retry_admin_job(self, job_id: str, *, reason: str) -> dict[str, Any]:
        from urllib.parse import quote

        value = await asyncio.to_thread(
            self._request_json,
            "POST",
            f"/v1/admin/jobs/{quote(job_id, safe='')}:retry",
            {"reason": reason},
        )
        return cast(dict[str, Any], value)

    async def create_schedule(
        self,
        *,
        job_kind: str,
        schedule_spec: dict[str, Any],
        reason: str,
        agent_id: str | None = None,
        timezone_name: str = "UTC",
        catch_up_policy: str = "latest",
    ) -> dict[str, Any]:
        value = await asyncio.to_thread(
            self._request_json,
            "POST",
            "/v1/admin/schedules",
            {
                "job_kind": job_kind,
                "schedule_spec": schedule_spec,
                "reason": reason,
                "agent_id": agent_id,
                "timezone": timezone_name,
                "catch_up_policy": catch_up_policy,
            },
        )
        return cast(dict[str, Any], value)

    async def run_schedule_now(self, schedule_id: str, *, reason: str) -> dict[str, Any]:
        from urllib.parse import quote

        value = await asyncio.to_thread(
            self._request_json,
            "POST",
            f"/v1/admin/schedules/{quote(schedule_id, safe='')}:run",
            {"reason": reason},
        )
        return cast(dict[str, Any], value)

    def _request_json(
        self,
        method: str,
        path: str,
        body: object | None,
        *,
        extra_headers: dict[str, str] | None = None,
    ) -> object:
        data = None if body is None else json.dumps(body).encode()
        headers = {"Content-Type": "application/json"}
        if extra_headers:
            headers.update(extra_headers)
        request = Request(
            f"{self._base_url}{path}",
            data=data,
            headers=headers,
            method=method,
        )
        try:
            with urlopen(request, timeout=self._timeout_seconds) as response:
                return cast(object, json.loads(response.read()))
        except HTTPError as error:
            value = cast(object, json.loads(error.read()))
            raise IrisMemoryApiError(ErrorEnvelope.from_value(value)) from error
