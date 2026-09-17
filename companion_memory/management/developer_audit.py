"""Independent, locally provisioned developer reads over native audit owners.

A private read-only mounted grant binds one database, instance, expiry and finite
operation/history references. It contains only a bearer verifier. Replacement or
removal revokes existing requests; both owner reads and HTTP first delivery check
the current grant. Administrator sessions and host/agent tokens confer no access.
"""
from __future__ import annotations

from dataclasses import dataclass
import hmac
import json
import time
from typing import TYPE_CHECKING, cast

from companion_memory.logging_service.audit import AuditAccess, AuditBound, bind_audit
from companion_memory.persistence import OperationIdentity
from companion_memory.persistence.owned_statements import OwnerFailure
from companion_memory.persistence.schema import valid_identifier
from .identity import Principal, digest

if TYPE_CHECKING:
    from .managed_application import ManagedApplication

GRANT_REFERENCE = 'developer_audit'
OPERATION_FIELDS = ('owner_namespace', 'operation_kind', 'operation_key')
HISTORY_FIELDS = OPERATION_FIELDS + ('history_id', 'object_id')


def closed_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate grant field.')
        result[key] = value
    return result


def references(value: object, fields: tuple[str, ...]) -> tuple[tuple[str, ...], ...]:
    """Decode a finite explicit allowlist; there are no wildcards or role inheritance."""
    if type(value) is not list or len(value) > 8:
        raise ValueError('A finite audit scope is required.')
    result = []
    for item in value:
        if type(item) is not dict or set(item) != set(fields) or any(not valid_identifier(item[field]) for field in fields):
            raise ValueError('Invalid audit scope.')
        result.append(tuple(cast(str, item[field]) for field in fields))
    if len(set(result)) != len(result):
        raise ValueError('Duplicate audit scope.')
    return tuple(result)


@dataclass(frozen=True, slots=True)
class AuditGrant:
    identity: str
    revision: int
    database_id: str
    instance_id: str
    verifier: str
    expires_at_us: int
    operations: tuple[tuple[str, ...], ...]
    histories: tuple[tuple[str, ...], ...]
    fingerprint: str


def decode_grant(encoded: bytes) -> AuditGrant:
    """Validate the complete local authorization carrier before any domain read."""
    if not 32 <= len(encoded) <= 4096:
        raise ValueError('Invalid audit grant size.')
    value = json.loads(encoded, object_pairs_hook=closed_object)
    names = {'format', 'grant_id', 'revision', 'database_id', 'instance_id', 'verifier', 'expires_at_us', 'operations', 'histories'}
    if type(value) is not dict or set(value) != names or value['format'] != 'DEVELOPER_AUDIT_V1':
        raise ValueError('Invalid audit grant format.')
    if any(not valid_identifier(value[name]) for name in ('grant_id', 'database_id', 'instance_id')):
        raise ValueError('Invalid audit binding.')
    if any(type(value[name]) is not int or not 0 < value[name] < 2**63 for name in ('revision', 'expires_at_us')):
        raise ValueError('Invalid audit lifetime.')
    if type(value['verifier']) is not str or len(value['verifier']) != 64 or any(c not in '0123456789abcdef' for c in value['verifier']):
        raise ValueError('Invalid audit verifier.')
    operations = references(value['operations'], OPERATION_FIELDS)
    histories = references(value['histories'], HISTORY_FIELDS)
    if not operations and not histories:
        raise ValueError('Empty audit authority.')
    return AuditGrant(value['grant_id'], value['revision'], value['database_id'], value['instance_id'],
        value['verifier'], value['expires_at_us'], operations, histories, digest(encoded))


class DeveloperAudit:
    """Trusted application assembly holds native readers; HTTP receives no storage port."""
    def __init__(self, application: ManagedApplication):
        self.application = application
        self.reader: AuditAccess | None = None
        self.reader_storage: object = None
        self.reader_snapshot: object = None

    def current(self) -> AuditGrant:
        app = self.application
        try:
            grant = decode_grant(app.resources.read_secret(GRANT_REFERENCE))
            if grant.database_id != app.resources.database_id or grant.instance_id != app.resources.instance_id:
                raise ValueError('Different instance.')
            if grant.expires_at_us <= time.time_ns() // 1000:
                raise ValueError('Expired grant.')
            return grant
        except (ValueError, OSError, UnicodeError, RecursionError):
            raise OwnerFailure('ACCESS_DENIED', 'capability', 'AUDIT_ACCESS_DENIED') from None

    def authenticate(self, credential: str) -> Principal:
        grant = self.current()
        if type(credential) is not str or len(credential) > 256 or not hmac.compare_digest(digest(credential), grant.verifier):
            raise OwnerFailure('ACCESS_DENIED', 'credential', 'AUDIT_ACCESS_DENIED')
        return Principal('developer_audit', grant.identity, grant.fingerprint, grant.revision, None, (), ('audit',),
            digest(digest('audit-csrf:' + credential)), grant.expires_at_us)

    def recheck(self, principal: Principal) -> AuditGrant:
        grant = self.current()
        if (type(principal) is not Principal or principal.kind != 'developer_audit'
                or principal.identity != grant.identity or principal.revision != grant.revision
                or not hmac.compare_digest(principal.verifier, grant.fingerprint)
                or principal.expires_at_us != grant.expires_at_us):
            raise OwnerFailure('ACCESS_DENIED', 'capability', 'AUDIT_ACCESS_DENIED')
        return grant

    def authorize_delivery(self, principal: Principal, path: str) -> None:
        """Recheck independent authority synchronously at first delivery.

        Native audit owners check their own readiness and record integrity.
        Ordinary recall checkpoints fence mutable business views, not committed
        audit evidence; focus or a concurrent business writer cannot revoke this
        separately issued read authority.
        """
        self.recheck(principal)

    async def dispatch(self, principal: Principal | None, method: str, path: str, payload: dict[str, object]):
        from .managed_application import fields, text
        if path == '/api/audit/login' and method == 'POST':
            fields(payload, {'credential'})
            credential = text(payload['credential'], maximum=256)
            self.authenticate(credential)
            return {'audit_session': {'credential': credential, 'csrf': digest('audit-csrf:' + credential)}}
        if principal is None:
            raise OwnerFailure('ACCESS_DENIED', 'capability', 'AUDIT_ACCESS_DENIED')
        grant = self.recheck(principal)
        if path == '/api/audit/status' and method == 'GET':
            fields(payload, set())
            return {'expires_at_us': grant.expires_at_us,
                'operations': [dict(zip(OPERATION_FIELDS, item)) for item in grant.operations],
                'histories': [dict(zip(HISTORY_FIELDS, item)) for item in grant.histories]}
        if path == '/api/audit/logout' and method == 'POST':
            fields(payload, set())
            return {'signed_out': True}
        history = path == '/api/audit/history'
        allowed = grant.histories if history else grant.operations
        names = HISTORY_FIELDS if history else OPERATION_FIELDS
        if method != 'POST' or path not in ('/api/audit/operation', '/api/audit/history'):
            raise OwnerFailure('ACCESS_DENIED', 'capability', 'AUDIT_ACCESS_DENIED')
        # Scope denial precedes owner readiness, existence, count or integrity reads.
        if set(payload) != set(names) or tuple(payload.get(name) for name in names) not in allowed:
            raise OwnerFailure('ACCESS_DENIED', 'capability', 'AUDIT_ACCESS_DENIED')
        identity = OperationIdentity(grant.database_id, cast(str, payload['owner_namespace']),
            cast(str, payload['operation_kind']), grant.instance_id, cast(str, payload['operation_key']))
        app = self.application
        host = app.business.host
        if history:
            if host is None or not app.business.initialized:
                raise OwnerFailure('INVALID_STATE', 'state', 'NOT_READY')
            owner = host.assembly.history
            try:
                port = owner.bind_inspection((cast(str, payload['object_id']),))
            except ValueError:
                # No read started: a closed or occupied native issuer is a
                # definite refusal, not an unconfirmed business operation.
                raise OwnerFailure('INVALID_STATE', 'audit', 'NOT_READY') from None
            result = await owner.read_object_history(port, identity, payload['history_id'], payload['object_id'])
        else:
            storage = app.bootstrap.assembly.storage
            snapshot = host.configuration.foundation if host is not None and app.business.initialized else app.bootstrap.snapshot
            if self.reader is None or self.reader_storage is not storage or self.reader_snapshot is not snapshot:
                bound = bind_audit(snapshot, storage.bind_audit_reader(grant.instance_id))
                if type(bound) is not AuditBound:
                    raise OwnerFailure('INVALID_STATE', 'audit', 'NOT_READY')
                self.reader = bound.value
                self.reader_storage, self.reader_snapshot = storage, snapshot
            result = await self.reader.read_audit(identity)
        self.authorize_delivery(principal, path)
        from companion_memory.logging_service.audit_records import AuditFound, AuditNotFound
        if type(result) is AuditFound:
            return {'status': 'FOUND', 'records': result.records}
        if type(result) is AuditNotFound:
            return {'status': 'ABSENT'}
        return result
