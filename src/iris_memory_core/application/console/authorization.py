"""Console resource authority: explicit Grant intersection, never an admin role."""

from __future__ import annotations

from iris_memory_core.application.console.resources import ReadRecord
from iris_memory_core.application.ports.transaction import Transaction
from iris_memory_core.domain.console import OperatorGrant
from iris_memory_core.domain.errors import NotFoundError
from iris_memory_core.domain.privacy import InvalidPrivacyLabelError, parse_label
from iris_memory_core.domain.scope import OPTIONAL_DIMENSIONS, Scope

SELECTOR_NAMES = ("agent_selector", "space_group_selector", "space_selector", "session_selector")
LABEL_DIMENSIONS = {
    "agent": "agent_id",
    "space_group": "space_group_id",
    "space": "space_id",
    "session": "session_id",
}


class ConsoleAuthorization:
    def __init__(self, tenant_id: str, grant: OperatorGrant) -> None:
        self.tenant_id, self.grant = tenant_id, grant
        self._contexts: tuple[Scope, ...] | None = None

    def _complete(self, tx: Transaction, scope: Scope) -> Scope | None:
        values = {key: getattr(scope, key) for key in OPTIONAL_DIMENSIONS}

        def merge(dimension: str, value: str | None) -> bool:
            if value is not None:
                if values[dimension] is not None and values[dimension] != value:
                    return False
                values[dimension] = value
            return True

        try:
            if scope.session_id:
                session = tx.get_session(scope.session_id)
                if session.tenant_id != self.tenant_id or not merge("space_id", session.space_id):
                    return None
            if values["space_id"]:
                space = tx.get_space(values["space_id"])
                if space.tenant_id != self.tenant_id:
                    return None
                if not merge("agent_id", space.agent_id) or not merge(
                    "space_group_id", space.space_group_id
                ):
                    return None
            if values["agent_id"] and tx.get_agent(values["agent_id"]).tenant_id != self.tenant_id:
                return None
            if (
                values["space_group_id"]
                and tx.get_space_group(values["space_group_id"]).tenant_id != self.tenant_id
            ):
                return None
            return Scope(self.tenant_id, **values)
        except NotFoundError:
            return None

    def _grant_contexts(self, tx: Transaction) -> tuple[Scope, ...]:
        if self._contexts is None:
            contexts = []
            if self.grant.session_selector.mode == "ids":
                for identifier in self.grant.session_selector.ids:
                    try:
                        session = tx.get_session(identifier)
                    except NotFoundError:
                        continue
                    if session.tenant_id == self.tenant_id:
                        contexts.append(
                            Scope(self.tenant_id, space_id=session.space_id, session_id=identifier)
                        )
            elif self.grant.space_selector.mode == "ids":
                contexts = [
                    Scope(self.tenant_id, space_id=identifier)
                    for identifier in self.grant.space_selector.ids
                ]
            else:
                contexts = [Scope(self.tenant_id)]
            completed = [self._complete(tx, scope) for scope in contexts]
            self._contexts = tuple(
                scope for scope in completed if scope is not None and self.scope_visible(scope)
            )
        return self._contexts

    def scope_visible(self, scope: Scope, labels: tuple[str, ...] = ()) -> bool:
        if scope.tenant_id != self.tenant_id or "console.manage" not in self.grant.data_purposes:
            return False
        # Stored null remains downward-visible. A nonempty Grant denotes a set
        # of possible complete contexts; an empty selector denotes no context.
        dimensions = {key: getattr(scope, key) for key in OPTIONAL_DIMENSIONS}
        for label in labels:
            try:
                kind, qualifier = parse_label(label)
            except InvalidPrivacyLabelError:
                return False
            if kind == "restricted" and not self.grant.allow_restricted:
                return False
            if kind == "custom" and label not in self.grant.custom_privacy_labels:
                return False
            if kind == "entity" and qualifier not in self.grant.subject_entity_ids:
                return False
            if kind in LABEL_DIMENSIONS:
                dimension = LABEL_DIMENSIONS[kind]
                if dimensions[dimension] is not None and dimensions[dimension] != qualifier:
                    return False
                dimensions[dimension] = qualifier
        for dimension, selector_name in zip(OPTIONAL_DIMENSIONS, SELECTOR_NAMES, strict=True):
            selector = getattr(self.grant, selector_name)
            if selector.mode == "ids" and not selector.ids:
                return False
            value = dimensions[dimension]
            if value is not None and not selector.contains(value):
                return False
        return True

    def visible(self, tx: Transaction, record: ReadRecord) -> bool:
        if not self.scope_visible(record.scope, record.privacy_labels):
            return False
        dimensions = {key: getattr(record.scope, key) for key in OPTIONAL_DIMENSIONS}
        for label in record.privacy_labels:
            kind, qualifier = parse_label(label)
            if kind in LABEL_DIMENSIONS:
                dimensions[LABEL_DIMENSIONS[kind]] = qualifier
        # A qualified session label obtains its space from the stored session,
        # never by treating a request-side omission as an unconstrained value.
        if dimensions["session_id"] and dimensions["space_id"] is None:
            try:
                dimensions["space_id"] = tx.get_session(dimensions["session_id"]).space_id
            except NotFoundError:
                return False
        completed = self._complete(tx, Scope(self.tenant_id, **dimensions))
        if completed is None or not self.scope_visible(completed):
            return False
        if not any(
            all(
                getattr(completed, key) is None
                or getattr(context, key) is None
                or getattr(completed, key) == getattr(context, key)
                for key in OPTIONAL_DIMENSIONS
            )
            for context in self._grant_contexts(tx)
        ):
            return False
        if tx.is_tombstoned(self.tenant_id, record.resource_type, record.id):
            return False
        if record.status in {"tombstoned", "erased"}:
            return False
        # Scope and subject tombstones also invalidate retained child content.
        for dimension in OPTIONAL_DIMENSIONS:
            identifier = getattr(record.scope, dimension)
            if identifier and tx.is_tombstoned(self.tenant_id, dimension[:-3], identifier):
                return False
        for label in record.privacy_labels:
            kind, qualifier = parse_label(label)
            if (
                kind == "entity"
                and qualifier
                and tx.is_tombstoned(self.tenant_id, "entity", qualifier)
            ):
                return False
        return True

    def mutable(self, tx: Transaction, record: ReadRecord) -> bool:
        """A downward-visible parent is not necessarily writable by its child.

        A restricted selector cannot edit data applying to every value of that
        dimension. Qualified privacy labels may narrow that effective scope.
        """
        if not self.visible(tx, record):
            return False
        dimensions = {key: getattr(record.scope, key) for key in OPTIONAL_DIMENSIONS}
        for label in record.privacy_labels:
            kind, qualifier = parse_label(label)
            if kind in LABEL_DIMENSIONS:
                dimensions[LABEL_DIMENSIONS[kind]] = qualifier
        completed = self._complete(tx, Scope(self.tenant_id, **dimensions))
        if completed is None:
            return False
        return all(
            getattr(self.grant, selector_name).mode == "all"
            or (
                getattr(completed, dimension) is not None
                and getattr(self.grant, selector_name).contains(getattr(completed, dimension))
            )
            for dimension, selector_name in zip(OPTIONAL_DIMENSIONS, SELECTOR_NAMES, strict=True)
        )
