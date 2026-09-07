"""Statically registered resource routes backed by the application read service."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Request
from starlette.concurrency import run_in_threadpool

from iris_memory_core.api.console.auth import body, idempotency_key, security
from iris_memory_core.api.console.errors import ConsoleError
from iris_memory_core.api.console.key_views import time_us
from iris_memory_core.api.console.resource_views import (
    descriptor,
    page,
    read_query,
    resource_view,
    source_ref,
)
from iris_memory_core.api.console.routes_auth import Principal
from iris_memory_core.api.console.views import envelope
from iris_memory_core.application.console.artifacts import ConsoleArtifactCommands
from iris_memory_core.application.console.claims import ConsoleClaimCommands
from iris_memory_core.application.console.episodes import ConsoleEpisodeCommands
from iris_memory_core.application.console.events import ConsoleEventCommands
from iris_memory_core.application.console.focus import ConsoleFocusCommands
from iris_memory_core.application.console.forget import ConsoleForgetCommands
from iris_memory_core.application.console.identity import ConsoleIdentityCommands
from iris_memory_core.application.console.notes import ConsoleNoteCommands
from iris_memory_core.application.console.observations import ConsoleObservationCommands
from iris_memory_core.application.console.personas import ConsolePersonaCommands
from iris_memory_core.application.console.reads import ConsoleReadService
from iris_memory_core.application.console.relations import ConsoleRelationCommands
from iris_memory_core.application.console.resources import BY_COLLECTION, LOOKUPS, SUBRESOURCES
from iris_memory_core.application.console.states import ConsoleStateCommands
from iris_memory_core.application.console.tasks import ConsoleTaskCommands
from iris_memory_core.domain.scope import Scope

router = APIRouter(prefix="/v1")


def _service(request: Request) -> ConsoleReadService:
    return ConsoleReadService(security(request))


@router.get("/memory/resource-types", operation_id="consoleMemoryResourceTypes")
def resource_types(request: Request, principal: Principal) -> dict[str, Any]:
    if request.query_params:
        raise ConsoleError("invalid_request", kind="validation_failed", status=400)
    return envelope(
        request,
        [
            descriptor(
                name,
                writable="memory.write" in principal.permissions,
                forgettable="memory.forget" in principal.permissions,
            )
            for name in _service(request).descriptors(principal)
        ],
        now_us=security(request).clock.now_us(),
    )


@router.post("/memory/artifacts", operation_id="consoleCreateArtifact", status_code=201)
async def create_artifact(request: Request, principal: Principal) -> dict[str, Any]:
    value = await body(request, "ConsoleArtifactCreateRequest")
    record = await run_in_threadpool(
        ConsoleArtifactCommands(security(request)).create,
        principal,
        scope=Scope(principal.key.tenant_id, **value["scope"]),
        fields=value["fields"],
        privacy_labels=value.get("privacy_labels", []),
        source_refs=value.get("source_refs", []),
        reason=value["reason_code"],
        idempotency_key=idempotency_key(request),
    )
    return envelope(
        request, resource_view(request, record), now_us=security(request).clock.now_us()
    )


def _relation_fields(value: dict[str, Any]) -> dict[str, Any]:
    fields = dict(value["fields"])
    for external, internal in (
        ("valid_from_at", "valid_from_us"),
        ("valid_until_at", "valid_until_us"),
    ):
        if external in fields:
            raw = fields.pop(external)
            fields[internal] = time_us(raw) if raw is not None else None
    return fields


@router.post("/memory/relations", operation_id="consoleCreateRelation", status_code=201)
async def create_relation(request: Request, principal: Principal) -> dict[str, Any]:
    value = await body(request, "ConsoleRelationCreateRequest")
    record = await run_in_threadpool(
        ConsoleRelationCommands(security(request)).create,
        principal,
        scope=Scope(principal.key.tenant_id, **value["scope"]),
        fields=_relation_fields(value),
        privacy_labels=value.get("privacy_labels", []),
        evidence=value["evidence"],
        reason=value["reason_code"],
        idempotency_key=idempotency_key(request),
    )
    return envelope(
        request, resource_view(request, record), now_us=security(request).clock.now_us()
    )


@router.post("/memory/relations/{id}:correct", operation_id="consoleCorrectRelation")
async def correct_relation(request: Request, principal: Principal, id: str) -> dict[str, Any]:
    value = await body(request, "ConsoleRelationCorrectRequest")
    if request.query_params or not 1 <= len(id) <= 128:
        raise ConsoleError("invalid_request", kind="validation_failed", status=400)
    record = await run_in_threadpool(
        ConsoleRelationCommands(security(request)).mutate,
        principal,
        id,
        operation="relation.correct",
        expected_revision=value["expected_revision"],
        fields=_relation_fields(value),
        evidence=value["evidence"],
        reason=value["reason_code"],
        idempotency_key=idempotency_key(request),
    )
    return envelope(
        request, resource_view(request, record), now_us=security(request).clock.now_us()
    )


@router.post("/memory/relations/{id}:transition", operation_id="consoleTransitionRelation")
async def transition_relation(request: Request, principal: Principal, id: str) -> dict[str, Any]:
    value = await body(request, "ConsoleRelationTransitionRequest")
    if request.query_params or not 1 <= len(id) <= 128:
        raise ConsoleError("invalid_request", kind="validation_failed", status=400)
    record = await run_in_threadpool(
        ConsoleRelationCommands(security(request)).mutate,
        principal,
        id,
        operation="relation.transition",
        expected_revision=value["expected_revision"],
        fields={"target_status": value["target_status"]},
        evidence=[],
        reason=value["reason_code"],
        idempotency_key=idempotency_key(request),
    )
    return envelope(
        request, resource_view(request, record), now_us=security(request).clock.now_us()
    )


def _episode_fields(value: dict[str, Any]) -> dict[str, Any]:
    fields = dict(value["fields"])
    for external, internal in (("started_at", "started_at_us"), ("ended_at", "ended_at_us")):
        if external in fields:
            raw = fields.pop(external)
            fields[internal] = time_us(raw) if raw is not None else None
    if "source_refs" in value:
        fields["source_refs"] = value["source_refs"]
    return fields


@router.post("/memory/episodes", operation_id="consoleCreateEpisode", status_code=201)
async def create_episode(request: Request, principal: Principal) -> dict[str, Any]:
    value = await body(request, "ConsoleEpisodeCreateRequest")
    record = await run_in_threadpool(
        ConsoleEpisodeCommands(security(request)).create,
        principal,
        scope=Scope(principal.key.tenant_id, **value["scope"]),
        fields=_episode_fields(value),
        privacy_labels=value.get("privacy_labels", []),
        reason=value["reason_code"],
        idempotency_key=idempotency_key(request),
    )
    return envelope(
        request, resource_view(request, record), now_us=security(request).clock.now_us()
    )


@router.patch("/memory/episodes/{id}", operation_id="consoleUpdateEpisode")
async def update_episode(request: Request, principal: Principal, id: str) -> dict[str, Any]:
    value = await body(request, "ConsoleEpisodeUpdateRequest")
    if request.query_params or not 1 <= len(id) <= 128:
        raise ConsoleError("invalid_request", kind="validation_failed", status=400)
    record = await run_in_threadpool(
        ConsoleEpisodeCommands(security(request)).mutate,
        principal,
        id,
        operation="episode.update",
        expected_revision=value["expected_revision"],
        fields=_episode_fields(value),
        reason=value["reason_code"],
        idempotency_key=idempotency_key(request),
    )
    return envelope(
        request, resource_view(request, record), now_us=security(request).clock.now_us()
    )


@router.post("/memory/episodes/{id}:transition", operation_id="consoleTransitionEpisode")
async def transition_episode(request: Request, principal: Principal, id: str) -> dict[str, Any]:
    value = await body(request, "ConsoleEpisodeTransitionRequest")
    if request.query_params or not 1 <= len(id) <= 128:
        raise ConsoleError("invalid_request", kind="validation_failed", status=400)
    record = await run_in_threadpool(
        ConsoleEpisodeCommands(security(request)).mutate,
        principal,
        id,
        operation="episode.transition",
        expected_revision=value["expected_revision"],
        fields={"target_status": value["target_status"]},
        reason=value["reason_code"],
        idempotency_key=idempotency_key(request),
    )
    return envelope(
        request, resource_view(request, record), now_us=security(request).clock.now_us()
    )


@router.post("/memory/claims", operation_id="consoleCreateClaim", status_code=201)
async def create_claim(request: Request, principal: Principal) -> dict[str, Any]:
    value = await body(request, "ConsoleClaimCreateRequest")
    fields = dict(value["fields"])
    for external, internal in (
        ("valid_from_at", "valid_from_us"),
        ("valid_until_at", "valid_until_us"),
    ):
        if external in fields:
            fields[internal] = time_us(fields.pop(external))
    record = await run_in_threadpool(
        ConsoleClaimCommands(security(request)).create,
        principal,
        scope=Scope(principal.key.tenant_id, **value["scope"]),
        fields=fields,
        privacy_labels=value.get("privacy_labels", []),
        evidence=value["evidence"],
        reason=value["reason_code"],
        idempotency_key=idempotency_key(request),
    )
    return envelope(
        request, resource_view(request, record), now_us=security(request).clock.now_us()
    )


@router.post("/memory/claims/{id}:correct", operation_id="consoleCorrectClaim")
async def correct_claim(request: Request, principal: Principal, id: str) -> dict[str, Any]:
    value = await body(request, "ConsoleClaimCorrectRequest")
    if request.query_params or not 1 <= len(id) <= 128:
        raise ConsoleError("invalid_request", kind="validation_failed", status=400)
    record = await run_in_threadpool(
        ConsoleClaimCommands(security(request)).correct,
        principal,
        id,
        expected_revision=value["expected_revision"],
        fields=dict(value["fields"]),
        evidence=value.get("evidence", []),
        reason=value["reason_code"],
        idempotency_key=idempotency_key(request),
    )
    return envelope(
        request, resource_view(request, record), now_us=security(request).clock.now_us()
    )


@router.post("/memory/observations", operation_id="consoleCreateObservation", status_code=201)
async def create_observation(request: Request, principal: Principal) -> dict[str, Any]:
    value = await body(request, "ConsoleObservationCreateRequest")
    record = await run_in_threadpool(
        ConsoleObservationCommands(security(request)).create,
        principal,
        scope=Scope(principal.key.tenant_id, **value["scope"]),
        fields=dict(value["fields"]),
        privacy_labels=value.get("privacy_labels", []),
        reason=value["reason_code"],
        idempotency_key=idempotency_key(request),
    )
    return envelope(
        request, resource_view(request, record), now_us=security(request).clock.now_us()
    )


@router.post("/memory/observations/{id}:annotate", operation_id="consoleAnnotateObservation")
async def annotate_observation(request: Request, principal: Principal, id: str) -> dict[str, Any]:
    value = await body(request, "ConsoleObservationAnnotateRequest")
    if request.query_params or not 1 <= len(id) <= 128:
        raise ConsoleError("invalid_request", kind="validation_failed", status=400)
    record = await run_in_threadpool(
        ConsoleObservationCommands(security(request)).annotate,
        principal,
        id,
        expected_revision=value["expected_revision"],
        fields=dict(value["fields"]),
        reason=value["reason_code"],
        idempotency_key=idempotency_key(request),
    )
    return envelope(
        request, resource_view(request, record), now_us=security(request).clock.now_us()
    )


@router.post("/memory/notes", operation_id="consoleCreateNote", status_code=201)
async def create_note(request: Request, principal: Principal) -> dict[str, Any]:
    value = await body(request, "ConsoleNoteCreateRequest")
    fields = dict(value["fields"])
    for external, internal in (("review_after_at", "review_after_us"), ("due_at", "due_at_us")):
        if external in fields:
            instant = fields.pop(external)
            fields[internal] = time_us(instant) if instant is not None else None
    record = await run_in_threadpool(
        ConsoleNoteCommands(security(request)).create,
        principal,
        scope=Scope(principal.key.tenant_id, **value["scope"]),
        fields=fields,
        privacy_labels=value.get("privacy_labels", []),
        source_refs=value.get("source_refs", []),
        reason=value["reason_code"],
        idempotency_key=idempotency_key(request),
    )
    return envelope(
        request, resource_view(request, record), now_us=security(request).clock.now_us()
    )


@router.patch("/memory/notes/{id}", operation_id="consoleUpdateNote")
async def update_note(request: Request, principal: Principal, id: str) -> dict[str, Any]:
    value = await body(request, "ConsoleNoteUpdateRequest")
    return await _mutate_note(request, principal, id, "note.update", value, dict(value["fields"]))


@router.post("/memory/notes/{id}:transition", operation_id="consoleTransitionNote")
async def transition_note(request: Request, principal: Principal, id: str) -> dict[str, Any]:
    value = await body(request, "ConsoleNoteTransitionRequest")
    fields = {
        key: item for key, item in value.items() if key not in {"expected_revision", "reason_code"}
    }
    return await _mutate_note(request, principal, id, "note.transition", value, fields)


async def _mutate_note(
    request: Request,
    principal: Principal,
    identifier: str,
    operation: str,
    value: dict[str, Any],
    fields: dict[str, Any],
) -> dict[str, Any]:
    if request.query_params or not 1 <= len(identifier) <= 128:
        raise ConsoleError("invalid_request", kind="validation_failed", status=400)
    for external, internal in (
        ("review_after_at", "review_after_us"),
        ("due_at", "due_at_us"),
        ("snooze_until_at", "snooze_until_us"),
    ):
        if external in fields:
            fields[internal] = time_us(fields.pop(external))
    record = await run_in_threadpool(
        ConsoleNoteCommands(security(request)).mutate,
        principal,
        identifier,
        operation=operation,
        expected_revision=value["expected_revision"],
        fields=fields,
        reason=value["reason_code"],
        idempotency_key=idempotency_key(request),
    )
    return envelope(
        request, resource_view(request, record), now_us=security(request).clock.now_us()
    )


@router.post("/memory/tasks", operation_id="consoleCreateTask", status_code=201)
async def create_task(request: Request, principal: Principal) -> dict[str, Any]:
    value = await body(request, "ConsoleTaskCreateRequest")
    fields = dict(value["fields"])
    for external, internal in (("due_at", "due_at_us"),):
        if external in fields:
            instant = fields.pop(external)
            fields[internal] = time_us(instant) if instant is not None else None
    record = await run_in_threadpool(
        ConsoleTaskCommands(security(request)).create,
        principal,
        scope=Scope(principal.key.tenant_id, **value["scope"]),
        fields=fields,
        privacy_labels=value.get("privacy_labels", []),
        source_refs=value.get("source_refs", []),
        reason=value["reason_code"],
        idempotency_key=idempotency_key(request),
    )
    return envelope(
        request, resource_view(request, record), now_us=security(request).clock.now_us()
    )


@router.patch("/memory/tasks/{id}", operation_id="consoleUpdateTask")
async def update_task(request: Request, principal: Principal, id: str) -> dict[str, Any]:
    value = await body(request, "ConsoleTaskUpdateRequest")
    return await _mutate_task(request, principal, id, "task.update", value, dict(value["fields"]))


@router.post("/memory/tasks/{id}:transition", operation_id="consoleTransitionTask")
async def transition_task(request: Request, principal: Principal, id: str) -> dict[str, Any]:
    value = await body(request, "ConsoleTaskTransitionRequest")
    fields = {
        key: item for key, item in value.items() if key not in {"expected_revision", "reason_code"}
    }
    return await _mutate_task(request, principal, id, "task.transition", value, fields)


async def _mutate_task(
    request: Request,
    principal: Principal,
    identifier: str,
    operation: str,
    value: dict[str, Any],
    fields: dict[str, Any],
) -> dict[str, Any]:
    if request.query_params or not 1 <= len(identifier) <= 128:
        raise ConsoleError("invalid_request", kind="validation_failed", status=400)
    if "due_at" in fields:
        fields["due_at_us"] = time_us(fields.pop("due_at"))
    record = await run_in_threadpool(
        ConsoleTaskCommands(security(request)).mutate,
        principal,
        identifier,
        operation=operation,
        expected_revision=value["expected_revision"],
        fields=fields,
        reason=value["reason_code"],
        idempotency_key=idempotency_key(request),
    )
    return envelope(
        request, resource_view(request, record), now_us=security(request).clock.now_us()
    )


@router.post("/memory/focus-items", operation_id="consoleCreateFocus", status_code=201)
async def create_focus(request: Request, principal: Principal) -> dict[str, Any]:
    value = await body(request, "ConsoleFocusCreateRequest")
    fields = dict(value["fields"])
    for external, internal in (("expires_at", "expires_us"),):
        if external in fields:
            instant = fields.pop(external)
            fields[internal] = time_us(instant) if instant is not None else None
    record = await run_in_threadpool(
        ConsoleFocusCommands(security(request)).create,
        principal,
        scope=Scope(principal.key.tenant_id, **value["scope"]),
        fields=fields,
        privacy_labels=value.get("privacy_labels", []),
        source_refs=value.get("source_refs", []),
        reason=value["reason_code"],
        idempotency_key=idempotency_key(request),
    )
    return envelope(
        request, resource_view(request, record), now_us=security(request).clock.now_us()
    )


@router.patch("/memory/focus-items/{id}", operation_id="consoleUpdateFocus")
async def update_focus(request: Request, principal: Principal, id: str) -> dict[str, Any]:
    value = await body(request, "ConsoleFocusUpdateRequest")
    return await _mutate_focus(request, principal, id, "focus.update", value, dict(value["fields"]))


@router.post("/memory/focus-items/{id}:transition", operation_id="consoleTransitionFocus")
async def transition_focus(request: Request, principal: Principal, id: str) -> dict[str, Any]:
    value = await body(request, "ConsoleFocusTransitionRequest")
    fields = {
        key: item for key, item in value.items() if key not in {"expected_revision", "reason_code"}
    }
    return await _mutate_focus(request, principal, id, "focus.transition", value, fields)


async def _mutate_focus(
    request: Request,
    principal: Principal,
    identifier: str,
    operation: str,
    value: dict[str, Any],
    fields: dict[str, Any],
) -> dict[str, Any]:
    if request.query_params or not 1 <= len(identifier) <= 128:
        raise ConsoleError("invalid_request", kind="validation_failed", status=400)
    record = await run_in_threadpool(
        ConsoleFocusCommands(security(request)).mutate,
        principal,
        identifier,
        operation=operation,
        expected_revision=value["expected_revision"],
        fields=fields,
        reason=value["reason_code"],
        idempotency_key=idempotency_key(request),
    )
    return envelope(
        request, resource_view(request, record), now_us=security(request).clock.now_us()
    )


@router.post("/memory/focus-items/{id}:activate", operation_id="consoleActivateFocus")
async def activate_focus(request: Request, principal: Principal, id: str) -> dict[str, Any]:
    value = await body(request, "ConsoleFocusActivateRequest")
    return await _mutate_focus(request, principal, id, "focus.activate", value, {})


@router.post("/memory/cognitive-events/{id}:dismiss", operation_id="consoleDismissCognitiveEvent")
async def dismiss_event(request: Request, principal: Principal, id: str) -> dict[str, Any]:
    value = await body(request, "ConsoleEventDismissRequest")
    if request.query_params or not 1 <= len(id) <= 128:
        raise ConsoleError("invalid_request", kind="validation_failed", status=400)
    result = await run_in_threadpool(
        ConsoleEventCommands(security(request)).dismiss,
        principal,
        id,
        expected_revision=value["expected_revision"],
        reason=value["reason_code"],
        idempotency_key=idempotency_key(request),
    )
    return envelope(request, asdict(result), now_us=security(request).clock.now_us())


@router.post("/memory/states", operation_id="consoleCreateState", status_code=201)
async def create_state(request: Request, principal: Principal) -> dict[str, Any]:
    value = await body(request, "ConsoleStateCreateRequest")
    result = await run_in_threadpool(
        ConsoleStateCommands(security(request)).create,
        principal,
        scope=Scope(principal.key.tenant_id, **value["scope"]),
        fields=_state_fields(value["fields"]),
        expected_revision=value["expected_revision"],
        reason=value["reason_code"],
        idempotency_key=idempotency_key(request),
    )
    return envelope(request, asdict(result), now_us=security(request).clock.now_us())


@router.patch("/memory/states/{id}", operation_id="consoleUpdateState")
async def update_state(request: Request, principal: Principal, id: str) -> dict[str, Any]:
    value = await body(request, "ConsoleStateUpdateRequest")
    return await _mutate_state(
        request, principal, id, "state.update", value, _state_fields(value["fields"])
    )


@router.post("/memory/states/{id}:expire", operation_id="consoleExpireState")
async def expire_state(request: Request, principal: Principal, id: str) -> dict[str, Any]:
    value = await body(request, "ConsoleStateExpireRequest")
    return await _mutate_state(request, principal, id, "state.expire", value, {})


def _state_fields(raw: dict[str, Any]) -> dict[str, Any]:
    fields = dict(raw)
    if "ttl_us" in fields:
        fields["ttl_us"] = int(fields["ttl_us"])
    if "expires_at" in fields:
        fields["expires_us"] = time_us(fields.pop("expires_at"))
    return fields


async def _mutate_state(
    request: Request,
    principal: Principal,
    identifier: str,
    operation: str,
    value: dict[str, Any],
    fields: dict[str, Any],
) -> dict[str, Any]:
    if not 1 <= len(identifier) <= 128:
        raise ConsoleError("invalid_request", kind="validation_failed", status=400)
    result = await run_in_threadpool(
        ConsoleStateCommands(security(request)).mutate,
        principal,
        identifier,
        operation=operation,
        fields=fields,
        expected_revision=value["expected_revision"],
        reason=value["reason_code"],
        idempotency_key=idempotency_key(request),
    )
    return envelope(request, asdict(result), now_us=security(request).clock.now_us())


@router.post(
    "/memory/tasks/{id}/triggers", operation_id="consoleCreateTaskTrigger", status_code=201
)
async def create_task_trigger(request: Request, principal: Principal, id: str) -> dict[str, Any]:
    value = await body(request, "ConsoleTaskTriggerCreateRequest")
    return await _mutate_task_trigger(
        request, principal, id, value, "task.trigger.create", dict(value["fields"])
    )


@router.post(
    "/memory/tasks/{id}/triggers/{trigger_id}:enabled",
    operation_id="consoleSetTaskTriggerEnabled",
)
async def set_task_trigger_enabled(
    request: Request, principal: Principal, id: str, trigger_id: str
) -> dict[str, Any]:
    value = await body(request, "ConsoleTaskTriggerEnabledRequest")
    fields: dict[str, Any] = {"enabled": value["enabled"]}
    return await _mutate_task_trigger(
        request, principal, id, value, "task.trigger.enabled", fields, trigger_id=trigger_id
    )


async def _mutate_task_trigger(
    request: Request,
    principal: Principal,
    task_id: str,
    value: dict[str, Any],
    operation: str,
    fields: dict[str, Any],
    *,
    trigger_id: str | None = None,
) -> dict[str, Any]:
    if not 1 <= len(task_id) <= 128 or (trigger_id is not None and not 1 <= len(trigger_id) <= 128):
        raise ConsoleError("invalid_request", kind="validation_failed", status=400)
    result = await run_in_threadpool(
        ConsoleTaskCommands(security(request)).trigger,
        principal,
        task_id,
        operation=operation,
        fields=fields,
        expected_revision=value["expected_revision"],
        trigger_id=trigger_id,
        child_expected_revision=value.get("child_expected_revision"),
        reason=value["reason_code"],
        idempotency_key=idempotency_key(request),
    )
    return envelope(request, result, now_us=security(request).clock.now_us())


@router.patch("/memory/tasks/{id}/triggers/{trigger_id}", operation_id="consoleUpdateTaskTrigger")
async def update_task_trigger(
    request: Request, principal: Principal, id: str, trigger_id: str
) -> dict[str, Any]:
    value = await body(request, "ConsoleTaskTriggerUpdateRequest")
    return await _mutate_task_trigger(
        request,
        principal,
        id,
        value,
        "task.trigger.update",
        dict(value["fields"]),
        trigger_id=trigger_id,
    )


@router.post(
    "/memory/tasks/{id}/dependencies", operation_id="consoleCreateTaskDependency", status_code=201
)
async def create_task_dependency(request: Request, principal: Principal, id: str) -> dict[str, Any]:
    value = await body(request, "ConsoleTaskDependencyCreateRequest")
    return await _mutate_task_dependency(
        request, principal, id, value, "task.dependency.create", dict(value["fields"])
    )


@router.post(
    "/memory/tasks/{id}/dependencies/{dependency_id}:remove",
    operation_id="consoleRemoveTaskDependency",
)
async def remove_task_dependency(
    request: Request, principal: Principal, id: str, dependency_id: str
) -> dict[str, Any]:
    value = await body(request, "ConsoleTaskDependencyRemoveRequest")
    fields: dict[str, Any] = {}
    return await _mutate_task_dependency(
        request, principal, id, value, "task.dependency.remove", fields, dependency_id=dependency_id
    )


async def _mutate_task_dependency(
    request: Request,
    principal: Principal,
    task_id: str,
    value: dict[str, Any],
    operation: str,
    fields: dict[str, Any],
    *,
    dependency_id: str | None = None,
) -> dict[str, Any]:
    if not 1 <= len(task_id) <= 128 or (
        dependency_id is not None and not 1 <= len(dependency_id) <= 128
    ):
        raise ConsoleError("invalid_request", kind="validation_failed", status=400)
    result = await run_in_threadpool(
        ConsoleTaskCommands(security(request)).dependency,
        principal,
        task_id,
        operation=operation,
        fields=fields,
        expected_revision=value["expected_revision"],
        dependency_id=dependency_id,
        child_expected_revision=value.get("child_expected_revision"),
        reason=value["reason_code"],
        idempotency_key=idempotency_key(request),
    )
    return envelope(request, result, now_us=security(request).clock.now_us())


@router.post("/memory/tasks/{id}/steps", operation_id="consoleCreateTaskStep", status_code=201)
async def create_task_step(request: Request, principal: Principal, id: str) -> dict[str, Any]:
    value = await body(request, "ConsoleTaskStepCreateRequest")
    return await _mutate_task_step(
        request, principal, id, value, "task.step.create", dict(value["fields"])
    )


@router.post(
    "/memory/tasks/{id}/steps/{step_id}:transition", operation_id="consoleTransitionTaskStep"
)
async def transition_task_step(
    request: Request, principal: Principal, id: str, step_id: str
) -> dict[str, Any]:
    value = await body(request, "ConsoleTaskStepTransitionRequest")
    fields = {
        key: item
        for key, item in value.items()
        if key not in {"expected_revision", "child_expected_revision", "reason_code"}
    }
    return await _mutate_task_step(
        request, principal, id, value, "task.step.transition", fields, step_id=step_id
    )


async def _mutate_task_step(
    request: Request,
    principal: Principal,
    task_id: str,
    value: dict[str, Any],
    operation: str,
    fields: dict[str, Any],
    *,
    step_id: str | None = None,
) -> dict[str, Any]:
    if not 1 <= len(task_id) <= 128 or (step_id is not None and not 1 <= len(step_id) <= 128):
        raise ConsoleError("invalid_request", kind="validation_failed", status=400)
    result = await run_in_threadpool(
        ConsoleTaskCommands(security(request)).step,
        principal,
        task_id,
        operation=operation,
        fields=fields,
        expected_revision=value["expected_revision"],
        step_id=step_id,
        child_expected_revision=value.get("child_expected_revision"),
        reason=value["reason_code"],
        idempotency_key=idempotency_key(request),
    )
    return envelope(request, result, now_us=security(request).clock.now_us())


def _listing(
    collection: str, *, subresource: bool = False, persona: bool = False, history: bool = False
) -> Callable[..., dict[str, Any]]:
    def endpoint(
        request: Request, principal: Principal, id: str = "", agent_id: str = ""
    ) -> dict[str, Any]:
        now = security(request).clock.now_us()
        query = read_query(request, principal, now)
        result = _service(request).listing(
            principal,
            collection,
            query,
            parent_id=(id if subresource else agent_id) if (subresource or persona) else None,
            history_id=id if history else None,
        )
        step_actions: dict[str, tuple[str, ...]] = {}
        child_writable = False
        if collection == "steps" and subresource:
            child_writable, step_actions = ConsoleTaskCommands(
                security(request)
            ).step_listing_actions(principal, id, result.records)
        if collection == "dependencies" and subresource:
            child_writable, step_actions = ConsoleTaskCommands(
                security(request)
            ).dependency_listing_actions(principal, id, result.records)
        if collection == "triggers" and subresource:
            child_writable, step_actions = ConsoleTaskCommands(
                security(request)
            ).trigger_listing_actions(principal, id, result.records)

        value = envelope(
            request,
            [
                resource_view(request, row, actions=step_actions.get(row.id))
                for row in result.records
            ],
            now_us=now,
        )
        if collection == "steps" and subresource:
            from iris_memory_core.api.console.command_descriptors import step_descriptor

            value["meta"]["descriptor"] = step_descriptor(writable=child_writable)
        if collection == "dependencies" and subresource:
            from iris_memory_core.api.console.command_descriptors import dependency_descriptor

            value["meta"]["descriptor"] = dependency_descriptor(id, writable=child_writable)
        if collection == "triggers" and subresource:
            from iris_memory_core.api.console.command_descriptors import trigger_descriptor

            value["meta"]["descriptor"] = trigger_descriptor(id, writable=child_writable)

        value["meta"]["page"] = page(
            request,
            principal,
            query,
            now,
            result.records[-1].key if result.records else None,
            result.has_more,
        )
        if query.include_total:
            value["meta"]["total"] = {
                "value": str(result.total) if result.total is not None else None,
                "exact": result.total_exact,
                "duration_us": str(result.total_duration_us),
            }
        if result.warnings:
            value["meta"]["warnings"] = list(result.warnings)
        return value

    return endpoint


def _detail(collection: str) -> Callable[..., dict[str, Any]]:
    def endpoint(request: Request, principal: Principal, id: str) -> dict[str, Any]:
        if request.query_params:
            raise ConsoleError("invalid_request", kind="validation_failed", status=400)
        record = _service(request).detail(principal, collection, id)
        actions = (
            ConsoleNoteCommands(security(request)).available_actions(principal, record)
            if collection == "notes"
            else None
        )
        if collection == "entities":
            actions = ConsoleIdentityCommands(security(request)).entity_actions(principal, record)
        if collection == "bindings":
            actions = ConsoleIdentityCommands(security(request)).binding_actions(principal, record)
        if collection == "relations":
            actions = ConsoleRelationCommands(security(request)).available_actions(
                principal, record
            )
        if collection == "episodes":
            actions = ConsoleEpisodeCommands(security(request)).available_actions(principal, record)
        if collection == "claims":
            actions = ConsoleClaimCommands(security(request)).available_actions(principal, record)
        if collection == "observations":
            actions = ConsoleObservationCommands(security(request)).available_actions(
                principal, record
            )
        if collection == "focus-items":
            actions = ConsoleFocusCommands(security(request)).available_actions(principal, record)
        if collection == "tasks":
            actions = ConsoleTaskCommands(security(request)).available_actions(principal, record)
        if collection == "states":
            actions = ConsoleStateCommands(security(request)).available_actions(principal, record)
        if collection == "cognitive-events":
            actions = ConsoleEventCommands(security(request)).available_actions(principal, record)
        actions = tuple(actions or ()) + ConsoleForgetCommands(security(request)).available_actions(
            principal, record
        )
        return envelope(
            request,
            resource_view(request, record, actions=actions),
            now_us=security(request).clock.now_us(),
        )

    return endpoint


def _references(collection: str) -> Callable[..., dict[str, Any]]:
    def endpoint(request: Request, principal: Principal, id: str) -> dict[str, Any]:
        now = security(request).clock.now_us()
        query = read_query(request, principal, now, filters=False)
        rows, more = _service(request).references(principal, collection, id, query)
        data = [
            {
                "id": row.id,
                "direction": row.direction,
                "relation": row.relation,
                "resource": source_ref(row.resource),
            }
            for row in rows
        ]
        result = envelope(request, data, now_us=now)
        result["meta"]["page"] = page(
            request, principal, query, now, rows[-1].key if rows else None, more
        )
        return result

    return endpoint


for collection in BY_COLLECTION:
    name = "".join(part.title() for part in collection.split("-"))
    path = "/memory/" + collection
    router.add_api_route(
        path, _listing(collection), methods=["GET"], operation_id="consoleList" + name
    )
    router.add_api_route(
        path + "/{id}", _detail(collection), methods=["GET"], operation_id="consoleGet" + name
    )
    router.add_api_route(
        path + "/{id}/history",
        _listing(collection, history=True),
        methods=["GET"],
        operation_id="consoleHistory" + name,
    )
    router.add_api_route(
        path + "/{id}/references",
        _references(collection),
        methods=["GET"],
        operation_id="consoleReferences" + name,
    )

for collection in LOOKUPS:
    name = "".join(part.title() for part in collection.split("-"))
    router.add_api_route(
        "/lookups/" + collection,
        _listing(collection),
        methods=["GET"],
        operation_id="consoleLookup" + name,
    )


@router.get("/memory/tasks/{id}/triggers/{trigger_id}", operation_id="consoleGetTaskTrigger")
def get_task_trigger(
    request: Request, principal: Principal, id: str, trigger_id: str
) -> dict[str, Any]:
    if request.query_params or not 1 <= len(id) <= 128 or not 1 <= len(trigger_id) <= 128:
        raise ConsoleError("invalid_request", kind="validation_failed", status=400)
    record = _service(request).detail(principal, "triggers", trigger_id, parent_id=id)
    _, actions = ConsoleTaskCommands(security(request)).trigger_listing_actions(
        principal, id, (record,)
    )
    return envelope(
        request,
        resource_view(request, record, actions=actions.get(record.id, ())),
        now_us=security(request).clock.now_us(),
    )


for collection in SUBRESOURCES:
    router.add_api_route(
        "/memory/tasks/{id}/" + collection,
        _listing(collection, subresource=True),
        methods=["GET"],
        operation_id="consoleTask" + collection.title(),
    )


@router.get("/personas/{agent_id}", operation_id="consolePersona")
def persona(request: Request, principal: Principal, agent_id: str) -> dict[str, Any]:
    if request.query_params:
        raise ConsoleError("invalid_request", kind="validation_failed", status=400)
    record, actions = ConsolePersonaCommands(security(request)).current(principal, agent_id)
    return envelope(
        request,
        resource_view(request, record, actions=actions),
        now_us=security(request).clock.now_us(),
    )


router.add_api_route(
    "/personas/{agent_id}/history",
    _listing("persona", persona=True),
    methods=["GET"],
    operation_id="consolePersonaHistory",
)
router.add_api_route(
    "/personas/{agent_id}/proposals",
    _listing("persona-proposals", persona=True),
    methods=["GET"],
    operation_id="consolePersonaProposals",
)
