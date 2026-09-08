"""Private, transport-independent business composition and contract validation."""

from __future__ import annotations

import base64
import hashlib
import json
import uuid
from collections.abc import Mapping
from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any, Protocol, cast

from jsonschema import Draft202012Validator

from iris_memory_core import business_views as views
from iris_memory_core.application.artifacts import ArtifactService
from iris_memory_core.application.episodes import EpisodeService, RelationService
from iris_memory_core.application.events import CognitiveEventService
from iris_memory_core.application.focus import FocusService
from iris_memory_core.application.forget import ForgetService
from iris_memory_core.application.identity import IdentityService
from iris_memory_core.application.memory import ClaimService
from iris_memory_core.application.notes import NoteService
from iris_memory_core.application.observation import ObservationService
from iris_memory_core.application.observation_context import ObservationContextService
from iris_memory_core.application.outbox import OutboxService
from iris_memory_core.application.persona import PersonaService
from iris_memory_core.application.ports.transaction import UnitOfWork
from iris_memory_core.application.provisioning import ProvisioningService
from iris_memory_core.application.recall import (
    ExternalActorRef,
    RecallService,
    RecallUsageReportInput,
    RecallUsageService,
    SearchService,
    StructuredRecallOrchestrator,
    StructuredRecallRequest,
)
from iris_memory_core.application.recent import RecentContextService
from iris_memory_core.application.retention import RetentionService
from iris_memory_core.application.scheduler import SchedulerService
from iris_memory_core.application.security import CredentialService
from iris_memory_core.application.state import StateService
from iris_memory_core.application.surface import SurfaceCoordinatorService
from iris_memory_core.application.tasks import TaskService
from iris_memory_core.domain.access import AccessContext
from iris_memory_core.domain.errors import (
    AccessDeniedError,
    InvalidRequestError,
    NotFoundError,
    NotReadyError,
    ProviderUnavailableError,
    UnsupportedVersionError,
)
from iris_memory_core.domain.hashing import canonical_json, request_fingerprint
from iris_memory_core.domain.identity import BindingMethod
from iris_memory_core.domain.jobs import NewOutboxJob
from iris_memory_core.domain.observation_context import ObservationContextConfig
from iris_memory_core.domain.profile import ProfileSubjectKey
from iris_memory_core.domain.scope import Scope
from iris_memory_core.indexing.fts import FtsDegradedError
from iris_memory_core.recall_runtime import RecallAssemblyConfig, assemble_recall
from iris_memory_core.storage.admin_archives import AdminArchiveService
from iris_memory_core.storage.idempotency import IdempotencyManager


class Request(Protocol):
    @property
    def headers(self) -> Mapping[str, str]: ...
    @property
    def path_params(self) -> Mapping[str, Any]: ...
    @property
    def query_params(self) -> Any: ...
    @property
    def state(self) -> Any: ...


INDEX_JOB_KIND = {
    "recent-context": "recent_context.maintenance",
    "recent_context": "recent_context.maintenance",
    "fts": "fts.rebuild",
    "vector": "vector.rebuild",
    "profile": "profile.rebuild",
    "graph": "graph.rebuild",
}

MANAGEMENT_CAPABILITY = {
    "rebuildIndex": "admin.index-rebuild.v1",
    "rebuildRecentContext": "admin.recent-context-rebuild.v1",
    "createBackup": "admin.backup.v1",
    "createExport": "admin.export.v1",
    "listAuditEvents": "admin.audit.v1",
    "dryRunReflection": "reflection.v1",
    "replayReflection": "reflection.v1",
    "listAdminJobs": "outbox.jobs.v1",
    "replayDeadLetter": "outbox.jobs.v1",
    "createSchedule": "schedules.v1",
    "runScheduleNow": "schedules.v1",
}


def _default_contract_path() -> Path:
    from iris_memory_core._resources import runtime_resource

    return runtime_resource("schemas/openapi/openapi.json")


def _load_contract(path: Path | None = None) -> dict[str, Any]:
    value = json.loads((path or _default_contract_path()).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError("frozen OpenAPI contract is not an object")
    return value


def _jsonable(value: object) -> object:
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value) and not isinstance(value, type):
        return {key: _jsonable(item) for key, item in asdict(value).items()}
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(item) for item in value]
    return value


def _resolve_schema(contract: Mapping[str, Any], schema: Mapping[str, Any]) -> Mapping[str, Any]:
    ref = schema.get("$ref")
    if not isinstance(ref, str):
        return schema
    prefix = "#/components/schemas/"
    if not ref.startswith(prefix):
        raise RuntimeError("external OpenAPI references are not supported")
    components = cast(Mapping[str, Any], contract["components"])
    schemas = cast(Mapping[str, Any], components["schemas"])
    return cast(Mapping[str, Any], schemas[ref.removeprefix(prefix)])


def _dereference_schema(contract: Mapping[str, Any], value: object) -> object:
    """Resolve local component references for standalone JSON Schema validation."""
    if isinstance(value, Mapping):
        if "$ref" in value:
            resolved = _resolve_schema(contract, cast(Mapping[str, Any], value))
            siblings = {key: item for key, item in value.items() if key != "$ref"}
            return _dereference_schema(contract, {**resolved, **siblings})
        return {str(key): _dereference_schema(contract, item) for key, item in value.items()}
    if isinstance(value, list):
        return [_dereference_schema(contract, item) for item in value]
    return value


def _request_schema(operation: Mapping[str, Any]) -> Mapping[str, Any] | None:
    request_body = operation.get("requestBody")
    if not isinstance(request_body, Mapping):
        return None
    content = request_body.get("content")
    if not isinstance(content, Mapping):
        return None
    media = content.get("application/json")
    if not isinstance(media, Mapping) or not isinstance(media.get("schema"), Mapping):
        return None
    return cast(Mapping[str, Any], media["schema"])


def _validate_parameters(
    request: Request, contract: Mapping[str, Any], operation: Mapping[str, Any]
) -> None:
    for raw in operation.get("parameters", []):
        if not isinstance(raw, Mapping):
            continue
        parameter = cast(Mapping[str, Any], _dereference_schema(contract, raw))
        name = str(parameter.get("name", ""))
        location = str(parameter.get("in", ""))
        if location == "header":
            value: object | None = request.headers.get(name)
        elif location == "query":
            values = request.query_params.getlist(name)
            value = values if len(values) > 1 else (values[0] if values else None)
        elif location == "path":
            value = request.path_params.get(name)
        else:
            continue
        if value is None:
            if parameter.get("required") is True:
                raise InvalidRequestError(f"required {location} parameter is missing")
            continue
        raw_schema = parameter.get("schema", {})
        if not isinstance(raw_schema, Mapping):
            continue
        schema = cast(Mapping[str, Any], _dereference_schema(contract, raw_schema))
        candidate: object = value
        expected_type = schema.get("type")
        try:
            if expected_type == "integer" and isinstance(value, str):
                candidate = int(value)
            elif expected_type == "number" and isinstance(value, str):
                candidate = float(value)
            elif expected_type == "boolean" and isinstance(value, str):
                if value not in {"true", "false"}:
                    raise ValueError
                candidate = value == "true"
            elif expected_type == "array" and isinstance(value, str):
                candidate = [value]
        except ValueError:
            raise InvalidRequestError(f"invalid {location} parameter") from None
        if any(Draft202012Validator(schema).iter_errors(candidate)):
            raise InvalidRequestError(f"invalid {location} parameter")


def _narrow(access: AccessContext, body: Mapping[str, object], request: Request) -> None:
    if body.get("tenant_id") not in (None, access.tenant_id):
        raise AccessDeniedError("request cannot widen tenant scope")
    merged: dict[str, object] = {**request.path_params, **request.query_params, **body}
    nested_scope = body.get("scope")
    scopes: tuple[Mapping[str, object], ...] = (
        (nested_scope,) if isinstance(nested_scope, Mapping) else ()
    )
    for name, allowed in (
        ("agent_id", access.agent_ids),
        ("space_group_id", access.allowed_space_group_ids),
        ("space_id", access.allowed_space_ids),
        ("entity_id", access.consent_subject_entity_ids),
    ):
        values = [merged.get(name), *(scope.get(name) for scope in scopes)]
        # A management credential's admin bit is its explicit tenant-wide
        # resource grant; application credentials remain bounded by every
        # registered id set.  Tenant isolation above is never bypassed.
        if not access.admin and any(
            value is not None and str(value) not in allowed for value in values
        ):
            raise AccessDeniedError(f"{name} is outside the authenticated envelope")
    requested_caps = body.get("capabilities")
    if requested_caps is not None and (
        not isinstance(requested_caps, list)
        or not set(map(str, requested_caps)) <= set(access.capabilities)
    ):
        raise AccessDeniedError("requested capabilities widen the credential")
    purpose = body.get("purpose")
    if purpose is not None and str(purpose) not in access.data_purposes:
        raise AccessDeniedError("purpose is outside the authenticated envelope")


def _require_management(access: AccessContext, operation_id: str) -> None:
    capability = MANAGEMENT_CAPABILITY.get(operation_id)
    if capability is not None and (not access.admin or capability not in access.capabilities):
        raise AccessDeniedError("management capability required")


def _entity_view(entity: Any) -> dict[str, object]:
    return views.entity_view(entity)


def _identity_view(identity: Any) -> dict[str, object]:
    return views.identity_view(
        identity, hashlib.sha256(identity.external_id.encode()).hexdigest()[:16]
    )


def _binding_view(binding: Any) -> dict[str, object]:
    return views.binding_view(binding)


def _scoped_agent(access: AccessContext, request: Request) -> str:
    """The agent a scoped read runs as: the explicit narrowing wins, otherwise
    the credential's single agent. A multi-agent credential must name one
    (ADR-0019 §5) — the transport never picks arbitrarily on its behalf."""
    requested = request.query_params.get("agent_id")
    if requested is not None:
        return str(requested)
    agents = sorted(access.agent_ids)
    if len(agents) != 1:
        raise InvalidRequestError(
            "agent_id is required when the credential grants more than one agent"
        )
    return agents[0]


def _opaque(value: str) -> str:
    """Stable, non-reversible label for admin surfaces (§29: no raw tenant ids)."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _require(value: Any) -> Any:
    """Fail closed when an aggregate the caller just wrote is not visible."""
    if value is None:
        raise NotFoundError("resource is not visible")
    return value


def _revisioned_view(value: tuple[object, object] | None, id_name: str) -> dict[str, object]:
    if value is None:
        raise NotFoundError("resource is not visible")
    current, revision = value
    current_value = cast(dict[str, object], _jsonable(current))
    revision_value = cast(dict[str, object], _jsonable(revision))
    merged = {**current_value, **revision_value}
    resource_id = current_value.get("id")
    merged.pop("id", None)
    merged.pop("tenant_id", None)
    merged.pop("scope_key", None)
    merged.pop("current_revision", None)
    merged[id_name] = resource_id
    return merged


def _query_int(request: Request, name: str, default: int) -> int:
    raw = request.query_params.get(name)
    return default if raw is None else int(raw)


def _statuses(query: Any) -> dict[str, tuple[str, ...]]:
    """Only forward an explicit ``status`` filter.

    Every list service defaults to its own visible-status set; passing
    ``None`` would override that default with an empty filter, so an absent
    query parameter must drop the keyword entirely rather than pass a null.
    """
    values = tuple(query.getlist("status"))
    return {"statuses": values} if values else {}


def _query_bool(request: Request, name: str, default: bool = False) -> bool:
    raw = request.query_params.get(name)
    return default if raw is None else raw == "true"


def _timestamp_us(value: str) -> int:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise InvalidRequestError("timestamp must be RFC 3339") from None
    if parsed.tzinfo is None:
        raise InvalidRequestError("timestamp must include a timezone")
    return int(parsed.timestamp() * 1_000_000)


def _watermark(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(str(value))
    except ValueError:
        raise InvalidRequestError("watermark must be a decimal integer") from None


def _group_view(tx: Any, group: Any) -> dict[str, object]:
    bindings = tx.list_group_bindings(group.id)
    return views.space_group_view(
        group, [item.space_id for item in bindings if item.unbound_us is None]
    )


class BusinessRuntime:
    """Shared business operations for HTTP and embedded delivery."""

    def __init__(
        self,
        uow: UnitOfWork,
        credentials: CredentialService,
        archives: AdminArchiveService | None = None,
        *,
        sse_enabled: bool = True,
        recall_config: RecallAssemblyConfig | None = None,
        cognitive_tenants: frozenset[str] = frozenset(),
        observation_context_config: ObservationContextConfig | None = None,
    ) -> None:
        self.uow = uow
        self.credentials = credentials
        self.cognitive_tenants = cognitive_tenants
        self.clock = cast(Any, uow).clock
        self.idempotency = IdempotencyManager(cast(Any, uow))
        self.surface = SurfaceCoordinatorService(uow, self.clock)
        self.observations = ObservationService(uow, self.idempotency, surface=self.surface)
        self.observation_context = ObservationContextService(
            uow, self.clock, idempotency=self.idempotency, config=observation_context_config
        )
        self.recent = RecentContextService(uow, self.clock)
        self.states = StateService(uow, self.clock, idempotency=self.idempotency)
        self.focus = FocusService(
            uow, self.clock, idempotency=self.idempotency, surface=self.surface
        )
        self.notes = NoteService(
            uow, self.clock, idempotency=self.idempotency, surface=self.surface
        )
        self.tasks = TaskService(
            uow, self.clock, idempotency=self.idempotency, surface=self.surface
        )
        self.events = CognitiveEventService(
            uow, self.clock, idempotency=self.idempotency, surface=self.surface
        )
        self.claims = ClaimService(
            uow, self.clock, idempotency=self.idempotency, surface=self.surface
        )
        self.episodes = EpisodeService(
            uow, self.clock, idempotency=self.idempotency, surface=self.surface
        )
        self.identities = IdentityService(uow, self.idempotency)
        self.relations = RelationService(
            uow, self.clock, idempotency=self.idempotency, surface=self.surface
        )
        self.artifacts = ArtifactService(
            uow, self.clock, idempotency=self.idempotency, surface=self.surface
        )
        self.forget = ForgetService(
            uow, self.clock, idempotency=self.idempotency, surface=self.surface
        )
        self.retention = RetentionService(uow, self.clock, forget=self.forget)
        self.personas = PersonaService(uow, self.clock, self.idempotency)
        self.scheduler = SchedulerService(uow, self.clock)
        self.projections = assemble_recall(uow, self.clock, recall_config)
        self.fts = self.projections.fts
        self.profiles = self.projections.profile
        orchestrator = StructuredRecallOrchestrator(
            uow,
            self.recent,
            self.states,
            self.focus,
            clock=self.clock,
            tasks=self.tasks,
            events=self.events,
            relations_enabled=True,
            claims_enabled=True,
            fts=self.fts,
            profile=self.profiles,
            graph=self.projections.graph,
            vector=self.projections.vector,
        )
        self.recall = RecallService(orchestrator, uow, self.clock, surface=self.surface)
        self.recall_usage = RecallUsageService(uow, self.clock)
        self.search = SearchService(uow, self.clock, self.fts)
        self.provisioning = ProvisioningService(uow, self.idempotency)
        self.outbox = OutboxService(uow, self.clock)
        self.archives = archives
        self.sse_enabled = sse_enabled

    def _recall_input(
        self, body: dict[str, Any]
    ) -> tuple[StructuredRecallRequest, tuple[ExternalActorRef, ...]]:
        scope = cast(dict[str, Any], body["scope"])
        actors = tuple(
            ExternalActorRef(
                provider=str(item["provider"]),
                external_id=str(item["external_id"]),
                realm=str(item.get("realm", "default")),
                weight=float(item.get("weight", 1.0)),
            )
            for item in cast(list[dict[str, Any]], body["actors"])
        )
        recall_request = self.recall.build_request(
            request_id=str(body["request_id"]),
            agent_id=str(scope["agent_id"]),
            space_id=str(scope["space_id"]),
            session_id=cast(str | None, scope.get("session_id")),
            space_group_id=cast(str | None, scope.get("space_group_id")),
            topic=str(body["topic"]),
            purpose=str(body["purpose"]),
            token_budget=int(body["token_budget"]),
            deadline_at_us=_timestamp_us(str(body["deadline_at"])),
            layer_budgets=cast(dict[str, int] | None, body.get("layer_budgets")),
            candidate_limits=cast(dict[str, int] | None, body.get("candidate_limits")),
            categories=(
                frozenset(map(str, cast(list[object], body["categories"])))
                if "categories" in body
                else None
            ),
            resource_types=(
                frozenset(map(str, cast(list[object], body["resource_types"])))
                if "resource_types" in body
                else None
            ),
            requested_privacy_labels=(
                frozenset(map(str, cast(list[object], body["requested_privacy_labels"])))
                if "requested_privacy_labels" in body
                else None
            ),
            as_of_us=(_timestamp_us(str(body["as_of"])) if body.get("as_of") is not None else None),
            minimum_watermark=_watermark(body.get("minimum_watermark")),
            include_trace=bool(body.get("include_trace", False)),
            allow_partial=bool(body.get("allow_partial", True)),
        )
        return recall_request, actors

    def dispatch(
        self,
        operation_id: str,
        access: AccessContext,
        request: Request,
        body: dict[str, Any],
    ) -> tuple[int, object] | None:
        idem = request.headers.get("idempotency-key")
        path = request.path_params
        # Dispatch is intentionally table-like and each branch returns a
        # different application DTO.  Keep the shared scratch names dynamic
        # while type-checking every branch and service call normally.
        result: Any
        values: Any
        value: Any
        current: Any
        method: Any
        common: dict[str, Any]
        if (
            operation_id in {"dryRunReflection", "replayReflection"}
            and access.tenant_id not in self.cognitive_tenants
        ):
            raise NotReadyError("cognitive provider is disabled for this tenant")
        if operation_id == "getCapabilities":
            return 200, self.capabilities(access)
        if operation_id == "negotiateCapabilities":
            if "v1" not in body.get("api_versions", []):
                raise UnsupportedVersionError("no mutually supported API version")
            available = self.capabilities(access)
            required = set(body.get("required_capabilities", []))
            if required - set(cast(list[str], available["capabilities"])):
                raise UnsupportedVersionError("required runtime capability is unavailable")
            return 200, available
        if operation_id == "getCurrentSurfaceLease":
            lease = self.surface.current(access, str(request.query_params["agent_id"]))
            if lease is None:
                raise NotFoundError("surface lease is not visible")
            return 200, views.lease_view(lease)
        if operation_id == "acquireSurfaceLease":
            outcome = self.surface.acquire(
                access,
                str(body["agent_id"]),
                holder_app_instance_id=str(body["holder_app_instance_id"]),
                holder_space_id=cast(str | None, body.get("holder_space_id")),
                ttl_us=int(body["ttl_us"]),
                priority=int(body.get("priority", 0)),
                allow_preempt=bool(body.get("allow_preempt", False)),
                reason=cast(str | None, body.get("reason")),
            )
            return 200, views.lease_view(outcome.lease)
        if operation_id in {"heartbeatSurfaceLease", "releaseSurfaceLease"}:
            lease_id = str(path["lease_id"])
            if operation_id == "heartbeatSurfaceLease":
                lease = self.surface.heartbeat(
                    access,
                    lease_id,
                    expected_epoch=int(body["lease_epoch"]),
                    ttl_us=int(body["ttl_us"]),
                    holder_app_instance_id=str(body["holder_app_instance_id"]),
                )
            else:
                lease = self.surface.release(
                    access,
                    lease_id,
                    expected_epoch=int(body["lease_epoch"]),
                    reason=cast(str | None, body.get("reason")),
                    holder_app_instance_id=str(body["holder_app_instance_id"]),
                )
            return 200, views.lease_view(lease)
        if operation_id == "observeBatch":
            result = self.observations.observe_batch(
                access,
                cast(list[dict[str, Any]], body["records"]),
                idempotency_key=idem,
                lease_id=cast(str | None, body.get("lease_id")),
                lease_epoch=cast(int | None, body.get("lease_epoch")),
            )
            return 200, views.observation_batch_view(result)
        if operation_id in {"readObservationContext", "summarizeObservationContext"}:
            if "observation-context.v1" not in access.capabilities and not access.admin:
                raise AccessDeniedError("observation-context.v1 is not granted")
            if operation_id == "readObservationContext":
                return 200, self.observation_context.read(access, body)
            if access.tenant_id not in self.cognitive_tenants:
                raise ProviderUnavailableError(
                    reason_code="cognitive_provider_not_configured", retryable=False
                )
            if "consolidation.v1" not in access.capabilities and not access.admin:
                raise AccessDeniedError("consolidation.v1 is not granted")
            return 202, self.observation_context.enqueue(access, body, idempotency_key=idem or "")
        if operation_id == "getSourceCursor":
            cursor, gap_policy = self.observations.get_cursor(
                access,
                str(request.query_params["agent_id"]),
                str(path["source_stream"]),
            )
            return 200, views.source_cursor_view(str(path["source_stream"]), cursor, gap_policy)
        if operation_id == "getRecentContext":
            result = self.recent.get(
                access,
                agent_id=str(request.query_params["agent_id"]),
                space_id=str(request.query_params["space_id"]),
                session_id=request.query_params.get("session_id"),
                minimum_watermark=(
                    int(request.query_params["minimum_watermark"])
                    if "minimum_watermark" in request.query_params
                    else None
                ),
            )
            return 200, views.recent_context_view(result)
        if operation_id == "putState":
            result = self.states.put(
                access,
                str(path["namespace"]),
                str(path["key"]),
                agent_id=str(body["agent_id"]),
                value=cast(dict[str, Any], body["value"]),
                source_authority=str(body["source_authority"]),
                space_id=cast(str | None, body.get("space_id")),
                session_id=cast(str | None, body.get("session_id")),
                observed_us=cast(int | None, body.get("observed_us")),
                ttl_us=cast(int | None, body.get("ttl_us")),
                expires_us=cast(int | None, body.get("expires_us")),
                source_ref=cast(str | None, body.get("source_ref")),
                coalesce_key=cast(str | None, body.get("coalesce_key")),
                idempotency_key=idem,
                expected_revision=cast(int | None, body.get("expected_revision")),
            )
            entry = self.states.get(
                access,
                str(path["namespace"]),
                str(path["key"]),
                agent_id=str(body["agent_id"]),
                space_id=cast(str | None, body.get("space_id")),
                session_id=cast(str | None, body.get("session_id")),
            )
            if entry is None:
                raise NotFoundError("state is not visible after the write")
            del result
            return 200, views.state_view(entry)
        if operation_id in {"getState", "getStateHistory", "listStates"}:
            query = request.query_params
            common = {
                "agent_id": str(query["agent_id"]),
                "space_id": query.get("space_id"),
                "session_id": query.get("session_id"),
            }
            if operation_id == "getState":
                value = self.states.get(
                    access,
                    str(path["namespace"]),
                    str(path["key"]),
                    **common,
                    include_expired=_query_bool(request, "include_expired"),
                )
                if value is None:
                    raise NotFoundError("state is not visible")
                return 200, views.state_view(value)
            if operation_id == "getStateHistory":
                values = self.states.history(
                    access,
                    str(path["namespace"]),
                    str(path["key"]),
                    **common,
                    limit=_query_int(request, "limit", 50),
                )
                if not values:
                    raise NotFoundError("state history is not visible")
                return 200, {
                    "record_id": values[0].record_id,
                    "namespace": str(path["namespace"]),
                    "key": str(path["key"]),
                    "revisions": [views.state_revision_view(item) for item in values],
                }
            values = self.states.list_scope(
                access,
                **common,
                namespace=query.get("namespace"),
                prefix=query.get("prefix"),
                limit=_query_int(request, "limit", 100),
            )
            return 200, {"items": [views.state_view(item) for item in values]}
        if operation_id in {
            "createFocusItem",
            "getFocusItem",
            "listFocusItems",
            "activateFocusItem",
            "dismissFocusItem",
            "setFocusDormant",
            "expireFocusItem",
            "promoteFocusItem",
        }:
            if operation_id == "createFocusItem":
                result = self.focus.create(access, idempotency_key=idem, **body)
                return 200, views.focus_view(_require(self.focus.get(access, result.item_id)))
            if operation_id == "listFocusItems":
                query = request.query_params
                values = self.focus.list_items(
                    access,
                    agent_id=str(query["agent_id"]),
                    **_statuses(query),
                    kind=query.get("kind"),
                    space_id=query.get("space_id"),
                    session_id=query.get("session_id"),
                    limit=_query_int(request, "limit", 100),
                )
                return 200, {"items": [views.focus_view(item) for item in values]}
            item_id = str(path["focus_item_id"])
            if operation_id == "getFocusItem":
                return 200, views.focus_view(
                    _require(
                        self.focus.get(
                            access,
                            item_id,
                            include_dormant=_query_bool(request, "include_dormant"),
                        )
                    )
                )
            if operation_id == "activateFocusItem":
                revision = self.focus.activate(
                    access,
                    item_id,
                    expected_revision=int(body["expected_revision"]),
                    reason=str(body["reason"]),
                    idempotency_key=idem,
                    lease_id=cast(str | None, body.get("lease_id")),
                    lease_epoch=cast(int | None, body.get("lease_epoch")),
                )
            else:
                target = {
                    "dismissFocusItem": "dismissed",
                    "setFocusDormant": "dormant",
                    "expireFocusItem": "expired",
                    "promoteFocusItem": "promoted",
                }[operation_id]
                revision = self.focus.transition(
                    access,
                    item_id,
                    target,
                    expected_revision=int(body["expected_revision"]),
                    reason=str(body["reason"]),
                    promotion_target_type=cast(str | None, body.get("promotion_target_type")),
                    idempotency_key=idem,
                    lease_id=cast(str | None, body.get("lease_id")),
                    lease_epoch=cast(int | None, body.get("lease_epoch")),
                )
            # The working-set read deliberately hides terminal items, so the
            # response is built from the revision the transition just wrote:
            # the caller still gets the published view of its own write.
            with self.uow.read() as tx:
                current = tx.focus.get(item_id)
            return 200, views.focus_view((current, revision))
        if operation_id in {"createNote", "updateNote", "archiveNote", "promoteNote", "listNotes"}:
            if operation_id == "listNotes":
                query = request.query_params
                values = self.notes.list_notes(
                    access,
                    agent_id=str(query["agent_id"]),
                    **_statuses(query),
                    kind=query.get("kind"),
                    space_id=query.get("space_id"),
                    session_id=query.get("session_id"),
                    limit=_query_int(request, "limit", 100),
                )
                return 200, {"items": [views.note_view(item) for item in values]}
            if operation_id == "createNote":
                result = self.notes.create(access, idempotency_key=idem, **body)
                return 200, views.note_view(_require(self.notes.get(access, result.note_id)))
            note_id = str(path["note_id"])
            if operation_id == "updateNote":
                self.notes.update(access, note_id, idempotency_key=idem, **body)
            else:
                self.notes.transition(
                    access,
                    note_id,
                    "archived" if operation_id == "archiveNote" else "promoted",
                    idempotency_key=idem,
                    **body,
                )
            return 200, views.note_view(_require(self.notes.get(access, note_id)))
        if operation_id in {
            "createTask",
            "updateTask",
            "transitionTask",
            "listTasks",
            "createTaskStep",
            "transitionTaskStep",
            "createTaskDependency",
            "createTaskTrigger",
        }:
            if operation_id == "listTasks":
                query = request.query_params
                values = self.tasks.list_tasks(
                    access,
                    agent_id=str(query["agent_id"]),
                    **_statuses(query),
                    space_id=query.get("space_id"),
                    session_id=query.get("session_id"),
                    limit=_query_int(request, "limit", 100),
                )
                return 200, {"items": [views.task_view(item) for item in values]}
            if operation_id == "createTask":
                result = self.tasks.create(access, idempotency_key=idem, **body)
                return 200, self._task_view(access, result.task_id)
            task_id = str(path["task_id"])
            if operation_id == "updateTask":
                self.tasks.patch(access, task_id, idempotency_key=idem, **body)
                return 200, self._task_view(access, task_id)
            if operation_id == "transitionTask":
                self.tasks.transition(access, task_id, idempotency_key=idem, **body)
                return 200, self._task_view(access, task_id)
            if operation_id == "createTaskStep":
                written = self.tasks.create_step(access, task_id, idempotency_key=idem, **body)
                return 200, self._step_view(access, task_id, written.step_id)
            if operation_id == "transitionTaskStep":
                step_id = str(path["step_id"])
                self.tasks.transition_step(access, task_id, step_id, idempotency_key=idem, **body)
                return 200, self._step_view(access, task_id, step_id)
            if operation_id == "createTaskDependency":
                edge = self.tasks.add_dependency(access, task_id, idempotency_key=idem, **body)
                return 200, {
                    "dependency_id": edge.dependency_id,
                    "task_id": edge.task_id,
                    "predecessor_step_id": edge.predecessor_step_id,
                    "successor_step_id": edge.successor_step_id,
                    "condition": _jsonable(edge.condition),
                    "revision": edge.current_revision,
                }
            written_trigger = self.tasks.create_trigger(
                access, task_id, idempotency_key=idem, **body
            )
            return 200, self._trigger_view(access, task_id, written_trigger.trigger_id)
        if operation_id in {"listCognitiveEvents", "ackCognitiveEvent"}:
            if operation_id == "listCognitiveEvents":
                query = request.query_params
                if _query_bool(request, "pull"):
                    value = self.events.pull(
                        access,
                        agent_id=str(query["agent_id"]),
                        limit=_query_int(request, "limit", 100),
                        lease_id=query.get("lease_id"),
                        lease_epoch=(int(query["lease_epoch"]) if "lease_epoch" in query else None),
                    )
                    return 200, {
                        "items": [
                            views.cognitive_event_view(current, revision)
                            for current, revision in value.events
                        ],
                        "expired_during_pull": value.expired,
                        "lease_warning": value.lease_warning,
                    }
                values = self.events.list_events(
                    access,
                    agent_id=str(query["agent_id"]),
                    **_statuses(query),
                    limit=_query_int(request, "limit", 100),
                )
                return 200, {
                    "items": [
                        views.cognitive_event_view(current, revision)
                        for current, revision in values
                    ],
                    "expired_during_pull": 0,
                }
            ack = self.events.ack(
                access,
                str(path["event_id"]),
                ack_token=cast(str | None, body.get("ack_token")),
                lease_id=cast(str | None, body.get("lease_id")),
                lease_epoch=cast(int | None, body.get("lease_epoch")),
                idempotency_key=idem,
            )
            with self.uow.read() as tx:
                current = tx.events.get(ack.event_id)
            return 200, views.cognitive_event_view(current, None) | {"ack_id": ack.ack_id}
        if operation_id in {
            "createClaimRemember",
            "correctClaim",
            "getClaim",
            "claimHistory",
            "searchClaims",
        }:
            if operation_id == "createClaimRemember":
                result = self.claims.remember(access, idempotency_key=idem, **body)
                return 200, views.claim_view(_require(self.claims.get(access, result.claim_id)))
            if operation_id == "correctClaim":
                claim_id = str(path["claim_id"])
                self.claims.correct(access, claim_id, idempotency_key=idem, **body)
                return 200, views.claim_view(_require(self.claims.get(access, claim_id)))
            if operation_id == "getClaim":
                value = self.claims.get(access, str(path["claim_id"]))
                if value is None:
                    raise NotFoundError("claim is not visible")
                return 200, views.claim_view(value)
            if operation_id == "claimHistory":
                claim_id = str(path["claim_id"])
                revisions = self.claims.history(
                    access, claim_id, limit=_query_int(request, "limit", 100)
                )
                return 200, {
                    "claim_id": claim_id,
                    "revisions": [views.claim_revision_view(item) for item in revisions],
                }
            query = request.query_params
            values = self.claims.search(
                access,
                agent_id=str(query["agent_id"]),
                space_id=query.get("space_id"),
                session_id=query.get("session_id"),
                subject_entity_id=query.get("subject_entity_id"),
                predicate=query.get("predicate"),
                category=query.get("category"),
                **_statuses(query),
                valid_at_us=(int(query["valid_at_us"]) if "valid_at_us" in query else None),
                as_of_us=(int(query["as_of_us"]) if "as_of_us" in query else None),
                limit=_query_int(request, "limit", 100),
            )
            return 200, {"items": [views.claim_view(item) for item in values]}
        if operation_id in {"createEpisode", "getEpisode", "transitionEpisode"}:
            if operation_id == "createEpisode":
                result = self.episodes.create(access, idempotency_key=idem, **body)
                return 200, views.episode_view(
                    _require(self.episodes.get(access, result.episode_id))
                )
            episode_id = str(path["episode_id"])
            if operation_id == "transitionEpisode":
                self.episodes.transition(access, episode_id, idempotency_key=idem, **body)
            return 200, views.episode_view(_require(self.episodes.get(access, episode_id)))
        if operation_id in {"createRelation", "getRelation"}:
            if operation_id == "createRelation":
                result = self.relations.create(access, idempotency_key=idem, **body)
                return 200, views.relation_view(
                    _require(self.relations.get(access, result.relation_id))
                )
            return 200, views.relation_view(
                _require(self.relations.get(access, str(path["relation_id"])))
            )
        if operation_id in {"createArtifact", "getArtifact"}:
            if operation_id == "getArtifact":
                value = self.artifacts.get(access, str(path["artifact_id"]))
                if value is None:
                    raise NotFoundError("artifact is not visible")
                return 200, views.artifact_view(value)
            storage_kind = str(body.pop("storage_kind"))
            content_value = body.pop("content_base64", None)
            if storage_kind in {"inline", "local_blob"}:
                try:
                    content = base64.b64decode(str(content_value), validate=True)
                except (ValueError, TypeError):
                    raise InvalidRequestError("content_base64 is invalid") from None
                method = (
                    self.artifacts.ingest_inline
                    if storage_kind == "inline"
                    else self.artifacts.ingest_local_blob
                )
                result = method(access, content=content, idempotency_key=idem, **body)
            else:
                external_url = body.pop("external_url", None)
                result = self.artifacts.register_external_ref(
                    access,
                    url=str(external_url),
                    idempotency_key=idem,
                    **body,
                )
            return 200, views.artifact_view(
                _require(self.artifacts.get(access, result.artifact_id))
            )
        if operation_id in {
            "listRetentionPolicies",
            "setRetentionPolicy",
            "createLegalHold",
            "releaseLegalHold",
            "forgetMemory",
            "exportDeletionLedger",
        }:
            if operation_id == "listRetentionPolicies":
                return 200, {
                    "items": [
                        views.retention_policy_view(item)
                        for item in self.retention.list_policies(access)
                    ]
                }
            if operation_id == "setRetentionPolicy":
                return 200, views.retention_policy_view(self.retention.set_policy(access, **body))
            if operation_id == "createLegalHold":
                clean = {key: value for key, value in body.items() if not key.startswith("lease_")}
                return 200, views.legal_hold_view(self.retention.create_legal_hold(access, **clean))
            if operation_id == "releaseLegalHold":
                return 200, views.legal_hold_view(
                    self.retention.release_legal_hold(
                        access, str(path["legal_hold_id"]), reason=str(body["reason"])
                    )
                )
            if operation_id == "forgetMemory":
                from iris_memory_core.domain.retention import ForgetSelector

                selector = ForgetSelector(**cast(dict[str, Any], body["selector"]))
                return 200, views.forget_view(
                    self.forget.forget(
                        access,
                        selector,
                        reason=str(body["reason"]),
                        erase_content=bool(body.get("erase_content", True)),
                        lease_id=cast(str | None, body.get("lease_id")),
                        lease_epoch=cast(int | None, body.get("lease_epoch")),
                        idempotency_key=idem,
                    )
                )
            return 200, {
                "requests": [
                    views.deletion_ledger_entry(item)
                    for item in self.forget.export_deletion_ledger(
                        access,
                        created_after_us=(
                            int(request.query_params["created_after_us"])
                            if "created_after_us" in request.query_params
                            else 0
                        ),
                    )
                ]
            }
        if operation_id in {
            "getCurrentPersona",
            "getPersonaHistory",
            "publishPersonaRevision",
            "updatePersonaState",
            "createPersonaEvolutionProposal",
            "approvePersonaEvolutionProposal",
            "rejectPersonaEvolutionProposal",
            "rollbackPersona",
        }:
            agent_id = str(path["agent_id"])
            if operation_id == "getCurrentPersona":
                return 200, views.persona_current_view(self.personas.current(access, agent_id))
            if operation_id == "getPersonaHistory":
                return 200, {
                    "items": [
                        views.persona_revision_view(item)
                        for item in self.personas.history(
                            access, agent_id, limit=_query_int(request, "limit", 100)
                        )
                    ]
                }
            if operation_id == "publishPersonaRevision":
                return 201, views.persona_revision_view(
                    self.personas.publish_revision(access, agent_id, idempotency_key=idem, **body)
                )
            if operation_id == "updatePersonaState":
                return 200, views.persona_state_view(
                    self.personas.update_state(access, agent_id, idempotency_key=idem, **body)
                )
            if operation_id == "createPersonaEvolutionProposal":
                return 201, views.persona_proposal_view(
                    self.personas.create_proposal(access, agent_id, idempotency_key=idem, **body)
                )
            if operation_id in {
                "approvePersonaEvolutionProposal",
                "rejectPersonaEvolutionProposal",
            }:
                method = (
                    self.personas.approve
                    if operation_id == "approvePersonaEvolutionProposal"
                    else self.personas.reject
                )
                return 200, views.persona_proposal_view(
                    method(
                        access,
                        agent_id,
                        str(path["proposal_id"]),
                        reason=str(body["reason"]),
                        idempotency_key=idem,
                    )
                )
            return 201, views.persona_revision_view(
                self.personas.rollback(access, agent_id, idempotency_key=idem, **body)
            )
        if operation_id in {"listAdminJobs", "replayDeadLetter"}:
            if operation_id == "replayDeadLetter":
                replayed = self.outbox.replay_dead_letter(
                    access, str(path["job_id"]), reason=str(body["reason"])
                )
                return 200, views.admin_job_view(replayed, tenant_hash=_opaque(access.tenant_id))
            query = request.query_params
            values = self.outbox.list_jobs(
                access,
                tenant_id=access.tenant_id,
                status=query.get("status"),
                job_kind=query.get("job_kind"),
                limit=_query_int(request, "limit", 100),
            )
            return 200, {
                "jobs": [
                    views.admin_job_view(item, tenant_hash=_opaque(access.tenant_id))
                    for item in values
                ]
            }
        if operation_id in {"createSchedule", "runScheduleNow"}:
            if operation_id == "createSchedule":
                value = self.scheduler.create_schedule(
                    access,
                    agent_id=cast(str | None, body.get("agent_id")),
                    job_kind=str(body["job_kind"]),
                    spec=cast(dict[str, object], body["schedule_spec"]),
                    timezone_name=str(body.get("timezone", "UTC")),
                    catch_up_policy=str(body.get("catch_up_policy", "latest")),
                    misfire_grace_us=int(body.get("misfire_grace_us", 60_000_000)),
                    max_ticks_per_run=int(body.get("max_ticks_per_run", 100)),
                    reason=str(body["reason"]),
                )
                return 201, views.schedule_view(
                    value,
                    tenant_hash=_opaque(access.tenant_id),
                    agent_hash=(_opaque(str(body["agent_id"])) if body.get("agent_id") else None),
                )
            tick = self.scheduler.run_now(
                access, str(path["schedule_id"]), reason=str(body["reason"])
            )
            return 200, {
                "tick_id": tick.id,
                "schedule_id": tick.schedule_id,
                "occurrence_key": tick.occurrence_key,
                "scheduled_at_us": tick.scheduled_at_us,
                "status": _jsonable(tick.status),
            }
        if operation_id == "getEntity":
            entity = self.identities.get_entity(
                access, Scope(tenant_id=access.tenant_id), str(path["entity_id"])
            )
            return 200, _entity_view(entity)
        if operation_id == "getEntityProfile":
            entity_id = str(path["entity_id"])
            self.identities.get_entity(access, Scope(tenant_id=access.tenant_id), entity_id)
            agent_id = _scoped_agent(access, request)
            request_scope = Scope(
                tenant_id=access.tenant_id,
                agent_id=agent_id,
                space_group_id=request.query_params.get("space_group_id"),
                space_id=request.query_params.get("space_id"),
                session_id=request.query_params.get("session_id"),
            )
            access.authorize_scope(request_scope)
            value = self.profiles.read_profile(
                access.tenant_id,
                ProfileSubjectKey("entity", entity_id),
                agent_id=agent_id,
                minimum_watermark=_watermark(request.query_params.get("minimum_watermark")),
                access=access,
                request_scope=request_scope,
            )
            return 200, views.profile_view(value.subject.kind, value.subject.subject_id, value)
        if operation_id == "getEntityRelations":
            entity_id = str(path["entity_id"])
            self.identities.get_entity(access, Scope(tenant_id=access.tenant_id), entity_id)
            with self.uow.read() as tx:
                relation_ids = tx.relations.relations_for_entity(access.tenant_id, entity_id)
            visible: list[dict[str, object]] = []
            for relation_id in relation_ids:
                value = self.relations.get(access, relation_id)
                if value is None:
                    continue
                visible.append(views.relation_view(value))
            return 200, {"relations": visible}
        if operation_id == "search":
            try:
                hits = self.search.search(
                    access,
                    agent_id=str(body["agent_id"]),
                    space_id=cast(str | None, body.get("space_id")),
                    session_id=cast(str | None, body.get("session_id")),
                    query=str(body["query"]),
                    limit=int(body.get("limit", 50)),
                )
            except FtsDegradedError as degraded:
                # The search surface has no partial mode: a degraded index
                # returns the stable not_ready envelope with the projection's
                # own reason code, never untrustworthy hits (ADR-0014 §1).
                raise NotReadyError(
                    "search index is degraded",
                    details={"reason_code": degraded.reason_code},
                ) from None
            return 200, {"results": [views.search_result_view(item) for item in hits]}
        if operation_id == "revalidateRecall":
            items = tuple(
                self._recall_input({**item, "deadline_at": body["deadline_at"]})
                for item in body["requests"]
            )
            checked_us, verdicts = self.recall.revalidate(access, items)
            return 200, {
                "schema_version": 1,
                "checked_at": datetime.fromtimestamp(checked_us / 1_000_000, tz=UTC)
                .isoformat()
                .replace("+00:00", "Z"),
                "results": [
                    {"request_id": request_id, "status": "valid" if valid else "unavailable"}
                    for request_id, valid in verdicts
                ],
            }
        if operation_id == "recall":
            recall_request, actors = self._recall_input(body)
            result = self.recall.recall(
                access,
                recall_request,
                actors=actors,
                lease_id=cast(str | None, body.get("lease_id")),
                lease_epoch=cast(int | None, body.get("lease_epoch")),
            )
            return 200, views.recall_response_view(
                result,
                schema_version=int(body["schema_version"]),
                cache_until_us=None,
                next_wake_us=None,
            )
        if operation_id == "reportRecallUsage":
            report = RecallUsageReportInput(
                request_id=str(path["request_id"]),
                host_cycle_id=str(body["host_cycle_id"]),
                returned_candidate_ids=tuple(map(str, body["returned_candidate_ids"])),
                host_selected_candidate_ids=tuple(map(str, body["host_selected_candidate_ids"])),
                model_visible_candidate_ids=tuple(map(str, body["model_visible_candidate_ids"])),
                persona_revision=int(body["persona_revision"]),
                reported_at_us=_timestamp_us(str(body["reported_at"])),
            )
            return 200, views.recall_usage_view(
                self.recall_usage.report(access, report), str(path["request_id"])
            )
        if operation_id == "createIdentity":
            identity = self.identities.register_external_identity(
                access,
                str(body["provider"]),
                str(body["realm"]),
                str(body["subject"]),
                entity_id=cast(str | None, body.get("entity_id")),
                idempotency_key=idem,
            )
            return 201, _identity_view(identity)
        if operation_id == "prepareBinding":
            proof = str(body.get("proof", ""))
            raw_method = str(body.get("method", "admin_confirmation"))
            try:
                method_value = BindingMethod(raw_method)
            except ValueError:
                # A value outside the domain enum is a client error, never an
                # internal fault: the envelope names the field, not the cause.
                raise InvalidRequestError("binding method is not supported") from None
            binding = self.identities.propose_binding(
                access,
                str(body["external_identity_id"]),
                str(body["entity_id"]),
                method=method_value,
                confidence=float(body.get("confidence", 1.0)),
                proof_digest=hashlib.sha256(proof.encode()).hexdigest(),
                reason=str(body["reason"]),
                idempotency_key=idem,
            )
            return 201, _binding_view(binding)
        if operation_id in {"confirmBinding", "revokeBinding"}:
            method = (
                self.identities.confirm_binding
                if operation_id == "confirmBinding"
                else self.identities.revoke_binding
            )
            binding = method(
                access,
                str(path["binding_id"]),
                expected_revision=int(body["expected_revision"]),
                reason=str(body["reason"]),
                idempotency_key=idem,
            )
            return 200, _binding_view(binding)
        if operation_id == "listSpaceGroups":
            with self.uow.read() as tx:
                groups = tx.list_space_groups(access.tenant_id)
                return 200, {"space_groups": [_group_view(tx, item) for item in groups]}
        if operation_id == "createSpaceGroup":
            group = self.provisioning.create_space_group(
                access,
                str(body["name"]),
                description=str(body.get("description", "")),
                reason=str(body["reason"]),
                idempotency_key=idem,
            )
            with self.uow.read() as tx:
                return 201, _group_view(tx, group)
        if operation_id in {"bindSpaceGroup", "unbindSpaceGroup"}:
            space_id = str(path["space_id"])
            if operation_id == "bindSpaceGroup":
                self.provisioning.bind_space_to_group(
                    access,
                    space_id,
                    str(path["space_group_id"]),
                    expected_revision=int(body["expected_revision"]),
                    reason=str(body["reason"]),
                    idempotency_key=idem,
                )
            else:
                self.provisioning.unbind_space_from_group(
                    access,
                    space_id,
                    expected_revision=int(body["expected_revision"]),
                    reason=str(body["reason"]),
                    idempotency_key=idem,
                )
            with self.uow.read() as tx:
                group = tx.get_space_group(str(path["space_group_id"]))
                return 200, _group_view(tx, group)
        if operation_id == "listAuditEvents":
            return 200, self._audit_events(access, request)
        if operation_id in {"rebuildIndex", "rebuildRecentContext"}:
            return self._rebuild(access, request, body)
        if operation_id in {
            "createBackup",
            "createExport",
            "dryRunReflection",
            "replayReflection",
        }:
            return self._admin_request(operation_id, access, request, body)
        return None

    def _task_view(self, access: AccessContext, task_id: str) -> dict[str, object]:
        pair = _require(self.tasks.get_task_view(access, task_id))
        with self.uow.read() as tx:
            steps = [
                views.step_view(step, tx.tasks.current_step_revision_row(step.id))
                for step in tx.tasks.steps_for_task(task_id)
            ]
        return views.task_view(pair, steps)

    def _step_view(self, access: AccessContext, task_id: str, step_id: str) -> dict[str, object]:
        _require(self.tasks.get_task_view(access, task_id))
        with self.uow.read() as tx:
            step = tx.tasks.get_step(step_id)
            revision = tx.tasks.current_step_revision_row(step_id)
        if step.task_id != task_id:
            raise NotFoundError("task step is not visible")
        return views.step_view(step, revision)

    def _trigger_view(
        self, access: AccessContext, task_id: str, trigger_id: str
    ) -> dict[str, object]:
        _require(self.tasks.get_task_view(access, task_id))
        with self.uow.read() as tx:
            trigger = tx.tasks.get_trigger(trigger_id)
            revision = tx.tasks.current_trigger_revision_row(trigger_id)
        if trigger.task_id != task_id:
            raise NotFoundError("task trigger is not visible")
        return views.trigger_view(trigger, revision)

    def _audit_events(self, access: AccessContext, request: Request) -> dict[str, object]:
        reason = request.query_params["reason"]
        with self.uow.write() as tx:
            tx.audit(
                tenant_id=access.tenant_id,
                actor=f"access:{access.app_instance_id}",
                action="admin.audit_events.read",
                resource_type="audit_log",
                resource_id=str(request.state.request_id),
                reason_code=reason,
                details={"after_us": int(request.query_params.get("after_us", "0"))},
            )
        with self.uow.read() as tx:
            events = tx.list_audit_events(
                access.tenant_id,
                after_us=int(request.query_params.get("after_us", "0")),
                limit=min(500, int(request.query_params.get("limit", "100"))),
            )
        return {"events": [views.audit_event_view(event) for event in events]}

    def _rebuild(
        self, access: AccessContext, request: Request, body: dict[str, Any]
    ) -> tuple[int, object]:
        kind = str(request.path_params.get("kind", "recent-context"))
        job_kind = INDEX_JOB_KIND.get(kind)
        if job_kind is None:
            raise InvalidRequestError("unsupported index rebuild kind")
        agent_id = cast(str | None, body.get("agent_id"))
        if job_kind == "recent_context.maintenance" and (
            agent_id is None or not isinstance(body.get("space_id"), str)
        ):
            raise InvalidRequestError("recent-context rebuild requires agent_id and space_id")
        idem = request.headers.get("idempotency-key")
        if not idem:
            raise InvalidRequestError("Idempotency-Key is required")

        def execute(tx: Any) -> tuple[str, str, list[str]]:
            state = tx.watermark(access.tenant_id, agent_id) if agent_id else None
            source = state.current_seq if state else 0
            job, _ = tx.outbox.enqueue(
                NewOutboxJob(
                    tenant_id=access.tenant_id,
                    agent_id=agent_id,
                    job_kind=job_kind,
                    aggregate_type="admin_rebuild",
                    aggregate_id=kind,
                    source_revision=source,
                    payload={
                        "version": 1,
                        "job_kind": job_kind,
                        "agent_id": agent_id,
                        "space_id": body.get("space_id"),
                        "session_id": body.get("session_id"),
                    },
                    dedupe_key=f"admin-rebuild:{kind}:{agent_id or 'tenant'}:{source}",
                    available_at_us=self.clock.now_us(),
                    priority=3,
                )
            )
            response = {
                "operation_id": job.id,
                "kind": kind,
                "status": job.status,
                "created_us": job.created_us,
            }
            tx.audit(
                tenant_id=access.tenant_id,
                actor=f"access:{access.app_instance_id}",
                action="admin.index_rebuild.requested",
                resource_type="admin_operation",
                resource_id=job.id,
                reason_code=str(body["reason"]),
                details={"kind": kind},
            )
            return "accepted", canonical_json(response), [job.id]

        result = self.idempotency.run(
            tenant_id=access.tenant_id,
            app_instance_id=access.app_instance_id,
            operation="admin:index-rebuild",
            idempotency_key=idem,
            request_fingerprint=request_fingerprint(
                "admin:index-rebuild", {"kind": kind, "body": body}
            ),
            execute=execute,
        )
        if kind == "recent-context" and "kind" not in request.path_params:
            # The deprecated Phase 8 path publishes the rebuilt projection
            # itself (200); the Phase 10 unified endpoint publishes the
            # accepted admin operation (202). The enqueued job is the same.
            del result
            return 200, views.recent_context_view(
                self.recent.get(
                    access,
                    agent_id=str(body["agent_id"]),
                    space_id=str(body["space_id"]),
                    session_id=cast(str | None, body.get("session_id")),
                )
            )
        return 202, json.loads(result.body)

    def _admin_request(
        self,
        operation: str,
        access: AccessContext,
        request: Request,
        body: dict[str, Any],
    ) -> tuple[int, object]:
        kind = {
            "createBackup": "backup",
            "createExport": "export",
            "dryRunReflection": "reflection_dry_run",
            "replayReflection": "reflection_replay",
        }[operation]
        idempotency_key = request.headers.get("idempotency-key")
        if not idempotency_key:
            raise InvalidRequestError("Idempotency-Key is required")
        fingerprint = request_fingerprint(
            f"admin:{kind}",
            {"path": dict(request.path_params), "body": body},
        )
        admission = self.idempotency.begin(
            tenant_id=access.tenant_id,
            app_instance_id=access.app_instance_id,
            operation=f"admin:{kind}",
            idempotency_key=idempotency_key,
            request_fingerprint=fingerprint,
        )
        if admission.status == "completed":
            return 202, json.loads(admission.response_body or "{}")
        operation_id = f"admin:{fingerprint[:32]}"
        outcome: dict[str, object] | None = None
        try:
            if operation == "createBackup":
                if self.archives is None:
                    raise InvalidRequestError("backup adapter is not configured")
                outcome = self.archives.create_backup(operation_id)
            elif operation == "createExport":
                if self.archives is None:
                    raise InvalidRequestError("export adapter is not configured")
                outcome = self.archives.create_export(operation_id, tenant_id=access.tenant_id)
            elif operation in {"dryRunReflection", "replayReflection"}:
                if operation == "dryRunReflection":
                    window_id = body.get("window_id")
                    replay_of = None
                    if not isinstance(window_id, str) or not window_id:
                        raise InvalidRequestError("reflection dry-run requires window_id")
                    with self.uow.read() as tx:
                        window = tx.reflection.get_window(window_id)
                else:
                    replay_of = str(request.path_params["reflection_id"])
                    with self.uow.read() as tx:
                        run = tx.reflection.get_run(replay_of)
                        window = tx.reflection.get_window(run.window_id)
                    window_id = window.id
                job, _ = self.outbox.enqueue(
                    NewOutboxJob(
                        tenant_id=access.tenant_id,
                        agent_id=window.agent_id,
                        job_kind="reflection.generate",
                        aggregate_type="consolidation_window",
                        aggregate_id=window_id,
                        source_revision=window.source_watermark,
                        payload={
                            "version": 2,
                            "job_kind": "reflection.generate",
                            "window_id": window_id,
                            "commit_mode": (
                                "dry_run" if operation == "dryRunReflection" else "commit"
                            ),
                            "replay_of": replay_of,
                        },
                        dedupe_key=f"admin-{kind}:{operation_id}",
                        available_at_us=self.clock.now_us(),
                        priority=7,
                    )
                )
                operation_id = job.id
                outcome = {
                    "operation_id": operation_id,
                    "kind": kind,
                    "status": job.status,
                    "created_us": job.created_us,
                }
        except BaseException:
            self.idempotency.abandon(admission)
            raise

        def audit(tx: Any) -> None:
            tx.audit(
                tenant_id=access.tenant_id,
                actor=f"access:{access.app_instance_id}",
                action=f"admin.{kind}.requested",
                resource_type="admin_operation",
                resource_id=operation_id,
                reason_code=str(body["reason"]),
                details={"kind": kind},
            )

        response = outcome or {
            "operation_id": operation_id,
            "kind": kind,
            "status": "accepted",
            "created_us": self.clock.now_us(),
        }
        try:
            completed = self.idempotency.complete_with(
                admission,
                response_code="accepted",
                response_body=canonical_json(response),
                resource_refs=[operation_id],
                transaction_ref=str(uuid.uuid4()),
                mutate=audit,
            )
        except BaseException:
            self.idempotency.abandon(admission)
            raise
        return 202, json.loads(completed.response_body or "{}")

    def capabilities(self, access: AccessContext) -> dict[str, object]:
        from iris_memory_core._resources import runtime_resource

        path = runtime_resource("contracts/source/contracts.json")
        source = json.loads(path.read_text(encoding="utf-8"))
        advertised = set(source["capabilities"])
        if access.tenant_id not in self.cognitive_tenants:
            advertised.difference_update({"reflection.v1", "consolidation.v1"})
        if self.projections.vector is None:
            advertised.difference_update({"recall.vector.v1", "embedding.v1"})
        if not self.sse_enabled:
            advertised.discard("events.sse.v1")
            advertised.discard("events.checkpoint.v1")
        advertised &= set(access.capabilities) | {
            "contract.negotiation",
            "error-envelope.v1",
            "health.v1",
        }
        return {
            "api_version": source["api_version"],
            "schema_version": source["schema_version"],
            "capabilities": sorted(advertised),
            "deprecated_capabilities": (
                ["admin.recent-context-rebuild.v1"]
                if "admin.recent-context-rebuild.v1" in advertised
                else []
            ),
            "deprecated": {"recent-context-rebuild": "/v1/admin/indexes/recent_context:rebuild"},
        }
