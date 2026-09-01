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

    # -- Phase 3: recent context / state / focus --------------------------

    async def recent_context(
        self,
        agent_id: str,
        space_id: str,
        *,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        from urllib.parse import quote

        query = f"agent_id={quote(agent_id, safe='')}&space_id={quote(space_id, safe='')}"
        if session_id is not None:
            query += f"&session_id={quote(session_id, safe='')}"
        value = await asyncio.to_thread(
            self._request_json, "GET", f"/v1/recent-context?{query}", None
        )
        return cast(dict[str, Any], value)

    async def put_state(
        self,
        namespace: str,
        key: str,
        *,
        agent_id: str,
        value: dict[str, Any],
        source_authority: str,
        idempotency_key: str,
        space_id: str | None = None,
        session_id: str | None = None,
        ttl_us: int | None = None,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        from urllib.parse import quote

        body: dict[str, Any] = {
            "agent_id": agent_id,
            "value": value,
            "source_authority": source_authority,
        }
        if space_id is not None:
            body["space_id"] = space_id
        if session_id is not None:
            body["session_id"] = session_id
        if ttl_us is not None:
            body["ttl_us"] = ttl_us
        if expected_revision is not None:
            body["expected_revision"] = expected_revision
        result = await asyncio.to_thread(
            self._request_json,
            "PUT",
            f"/v1/state/{quote(namespace, safe='')}/{quote(key, safe='')}",
            body,
            extra_headers={"Idempotency-Key": idempotency_key},
        )
        return cast(dict[str, Any], result)

    async def get_state(
        self,
        namespace: str,
        key: str,
        *,
        agent_id: str,
        space_id: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any] | None:
        from urllib.parse import quote

        query = f"agent_id={quote(agent_id, safe='')}"
        if space_id is not None:
            query += f"&space_id={quote(space_id, safe='')}"
        if session_id is not None:
            query += f"&session_id={quote(session_id, safe='')}"
        value = await asyncio.to_thread(
            self._request_json,
            "GET",
            f"/v1/state/{quote(namespace, safe='')}/{quote(key, safe='')}?{query}",
            None,
        )
        return cast(dict[str, Any] | None, value)

    async def list_states(
        self,
        agent_id: str,
        *,
        namespace: str | None = None,
        space_id: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        from urllib.parse import quote

        query = f"agent_id={quote(agent_id, safe='')}"
        if namespace is not None:
            query += f"&namespace={quote(namespace, safe='')}"
        if space_id is not None:
            query += f"&space_id={quote(space_id, safe='')}"
        if session_id is not None:
            query += f"&session_id={quote(session_id, safe='')}"
        value = await asyncio.to_thread(self._request_json, "GET", f"/v1/state?{query}", None)
        return cast(dict[str, Any], value)

    async def state_history(
        self,
        namespace: str,
        key: str,
        *,
        agent_id: str,
        space_id: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        from urllib.parse import quote

        query = f"agent_id={quote(agent_id, safe='')}"
        if space_id is not None:
            query += f"&space_id={quote(space_id, safe='')}"
        if session_id is not None:
            query += f"&session_id={quote(session_id, safe='')}"
        value = await asyncio.to_thread(
            self._request_json,
            "GET",
            f"/v1/state/{quote(namespace, safe='')}/{quote(key, safe='')}/history?{query}",
            None,
        )
        return cast(dict[str, Any], value)

    async def create_focus_item(
        self,
        record: dict[str, Any],
        *,
        idempotency_key: str,
    ) -> dict[str, Any]:
        value = await asyncio.to_thread(
            self._request_json,
            "POST",
            "/v1/focus-items",
            record,
            extra_headers={"Idempotency-Key": idempotency_key},
        )
        return cast(dict[str, Any], value)

    async def get_focus_item(self, focus_item_id: str) -> dict[str, Any]:
        from urllib.parse import quote

        value = await asyncio.to_thread(
            self._request_json,
            "GET",
            f"/v1/focus-items/{quote(focus_item_id, safe='')}",
            None,
        )
        return cast(dict[str, Any], value)

    async def list_focus_items(
        self,
        agent_id: str,
        *,
        status: str | None = None,
        kind: str | None = None,
        space_id: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        from urllib.parse import quote

        query = f"agent_id={quote(agent_id, safe='')}"
        if status is not None:
            query += f"&status={quote(status, safe='')}"
        if kind is not None:
            query += f"&kind={quote(kind, safe='')}"
        if space_id is not None:
            query += f"&space_id={quote(space_id, safe='')}"
        if session_id is not None:
            query += f"&session_id={quote(session_id, safe='')}"
        value = await asyncio.to_thread(self._request_json, "GET", f"/v1/focus-items?{query}", None)
        return cast(dict[str, Any], value)

    async def focus_transition(
        self,
        focus_item_id: str,
        action: str,
        *,
        expected_revision: int,
        reason: str,
        idempotency_key: str,
        promotion_target_type: str | None = None,
    ) -> dict[str, Any]:
        from urllib.parse import quote

        if action not in ("activate", "dormant", "dismiss", "expire", "promote"):
            raise ValueError(f"unknown focus action: {action!r}")
        body: dict[str, Any] = {
            "expected_revision": expected_revision,
            "reason": reason,
        }
        if promotion_target_type is not None:
            body["promotion_target_type"] = promotion_target_type
        value = await asyncio.to_thread(
            self._request_json,
            "POST",
            f"/v1/focus-items/{quote(focus_item_id, safe='')}:{action}",
            body,
            extra_headers={"Idempotency-Key": idempotency_key},
        )
        return cast(dict[str, Any], value)

    async def rebuild_recent_context(
        self,
        agent_id: str,
        space_id: str,
        *,
        reason: str,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "agent_id": agent_id,
            "space_id": space_id,
            "reason": reason,
        }
        if session_id is not None:
            body["session_id"] = session_id
        value = await asyncio.to_thread(
            self._request_json, "POST", "/v1/admin/recent-context:rebuild", body
        )
        return cast(dict[str, Any], value)

    # -- Phase 4: notes -------------------------------------------------------

    async def create_note(
        self,
        record: dict[str, Any],
        *,
        idempotency_key: str,
    ) -> dict[str, Any]:
        value = await asyncio.to_thread(
            self._request_json,
            "POST",
            "/v1/notes",
            record,
            extra_headers={"Idempotency-Key": idempotency_key},
        )
        return cast(dict[str, Any], value)

    async def list_notes(
        self,
        agent_id: str,
        *,
        status: str | None = None,
        kind: str | None = None,
        space_id: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        from urllib.parse import quote

        query = f"agent_id={quote(agent_id, safe='')}"
        for key, item in (
            ("status", status),
            ("kind", kind),
            ("space_id", space_id),
            ("session_id", session_id),
        ):
            if item is not None:
                query += f"&{key}={quote(item, safe='')}"
        value = await asyncio.to_thread(self._request_json, "GET", f"/v1/notes?{query}", None)
        return cast(dict[str, Any], value)

    async def update_note(
        self,
        note_id: str,
        body: dict[str, Any],
        *,
        idempotency_key: str,
    ) -> dict[str, Any]:
        from urllib.parse import quote

        value = await asyncio.to_thread(
            self._request_json,
            "PATCH",
            f"/v1/notes/{quote(note_id, safe='')}",
            body,
            extra_headers={"Idempotency-Key": idempotency_key},
        )
        return cast(dict[str, Any], value)

    async def note_action(
        self,
        note_id: str,
        action: str,
        body: dict[str, Any],
        *,
        idempotency_key: str,
    ) -> dict[str, Any]:
        from urllib.parse import quote

        if action not in ("archive", "promote"):
            raise ValueError(f"unknown note action: {action!r}")
        value = await asyncio.to_thread(
            self._request_json,
            "POST",
            f"/v1/notes/{quote(note_id, safe='')}:{action}",
            body,
            extra_headers={"Idempotency-Key": idempotency_key},
        )
        return cast(dict[str, Any], value)

    # -- Phase 4: tasks ---------------------------------------------------------

    async def create_task(
        self,
        record: dict[str, Any],
        *,
        idempotency_key: str,
    ) -> dict[str, Any]:
        value = await asyncio.to_thread(
            self._request_json,
            "POST",
            "/v1/tasks",
            record,
            extra_headers={"Idempotency-Key": idempotency_key},
        )
        return cast(dict[str, Any], value)

    async def list_tasks(
        self,
        agent_id: str,
        *,
        status: str | None = None,
        space_id: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        from urllib.parse import quote

        query = f"agent_id={quote(agent_id, safe='')}"
        for key, item in (
            ("status", status),
            ("space_id", space_id),
            ("session_id", session_id),
        ):
            if item is not None:
                query += f"&{key}={quote(item, safe='')}"
        value = await asyncio.to_thread(self._request_json, "GET", f"/v1/tasks?{query}", None)
        return cast(dict[str, Any], value)

    async def update_task(
        self,
        task_id: str,
        body: dict[str, Any],
        *,
        idempotency_key: str,
    ) -> dict[str, Any]:
        from urllib.parse import quote

        value = await asyncio.to_thread(
            self._request_json,
            "PATCH",
            f"/v1/tasks/{quote(task_id, safe='')}",
            body,
            extra_headers={"Idempotency-Key": idempotency_key},
        )
        return cast(dict[str, Any], value)

    async def transition_task(
        self,
        task_id: str,
        body: dict[str, Any],
        *,
        idempotency_key: str,
    ) -> dict[str, Any]:
        from urllib.parse import quote

        value = await asyncio.to_thread(
            self._request_json,
            "POST",
            f"/v1/tasks/{quote(task_id, safe='')}:transition",
            body,
            extra_headers={"Idempotency-Key": idempotency_key},
        )
        return cast(dict[str, Any], value)

    async def create_task_step(
        self,
        task_id: str,
        body: dict[str, Any],
        *,
        idempotency_key: str,
    ) -> dict[str, Any]:
        from urllib.parse import quote

        value = await asyncio.to_thread(
            self._request_json,
            "POST",
            f"/v1/tasks/{quote(task_id, safe='')}/steps",
            body,
            extra_headers={"Idempotency-Key": idempotency_key},
        )
        return cast(dict[str, Any], value)

    async def transition_task_step(
        self,
        task_id: str,
        step_id: str,
        body: dict[str, Any],
        *,
        idempotency_key: str,
    ) -> dict[str, Any]:
        from urllib.parse import quote

        value = await asyncio.to_thread(
            self._request_json,
            "POST",
            f"/v1/tasks/{quote(task_id, safe='')}/steps/{quote(step_id, safe='')}:transition",
            body,
            extra_headers={"Idempotency-Key": idempotency_key},
        )
        return cast(dict[str, Any], value)

    async def create_task_dependency(
        self,
        task_id: str,
        body: dict[str, Any],
        *,
        idempotency_key: str,
    ) -> dict[str, Any]:
        from urllib.parse import quote

        value = await asyncio.to_thread(
            self._request_json,
            "POST",
            f"/v1/tasks/{quote(task_id, safe='')}/dependencies",
            body,
            extra_headers={"Idempotency-Key": idempotency_key},
        )
        return cast(dict[str, Any], value)

    async def create_task_trigger(
        self,
        task_id: str,
        body: dict[str, Any],
        *,
        idempotency_key: str,
    ) -> dict[str, Any]:
        from urllib.parse import quote

        value = await asyncio.to_thread(
            self._request_json,
            "POST",
            f"/v1/tasks/{quote(task_id, safe='')}/triggers",
            body,
            extra_headers={"Idempotency-Key": idempotency_key},
        )
        return cast(dict[str, Any], value)

    # -- Phase 4: cognitive events -----------------------------------------------

    async def list_cognitive_events(
        self,
        agent_id: str,
        *,
        status: str | None = None,
        pull: bool = False,
        lease_id: str | None = None,
        lease_epoch: int | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        from urllib.parse import quote

        query = f"agent_id={quote(agent_id, safe='')}"
        if status is not None:
            query += f"&status={quote(status, safe='')}"
        if pull:
            query += "&pull=true"
        if lease_id is not None:
            query += f"&lease_id={quote(lease_id, safe='')}"
        if lease_epoch is not None:
            query += f"&lease_epoch={lease_epoch}"
        if limit is not None:
            query += f"&limit={limit}"
        value = await asyncio.to_thread(
            self._request_json, "GET", f"/v1/cognitive-events?{query}", None
        )
        return cast(dict[str, Any], value)

    async def ack_cognitive_event(
        self,
        event_id: str,
        *,
        idempotency_key: str,
        ack_token: str | None = None,
        lease_id: str | None = None,
        lease_epoch: int | None = None,
    ) -> dict[str, Any]:
        """ACK a delivered event.

        Under required surface mode the caller must present its lease proof
        (``lease_id`` + ``lease_epoch``); both ride in the request body,
        exactly like every other Phase 4 write.
        """
        from urllib.parse import quote

        body: dict[str, Any] = {}
        if ack_token is not None:
            body["ack_token"] = ack_token
        if lease_id is not None:
            body["lease_id"] = lease_id
        if lease_epoch is not None:
            body["lease_epoch"] = lease_epoch
        value = await asyncio.to_thread(
            self._request_json,
            "POST",
            f"/v1/cognitive-events/{quote(event_id, safe='')}:ack",
            body,
            extra_headers={"Idempotency-Key": idempotency_key},
        )
        return cast(dict[str, Any], value)

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
