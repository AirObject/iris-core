"""Local administrator and independently scoped host-token authority.

Password derivation occurs outside SQLite. Every permission-changing command
uses an expected revision and mandatory audit. Request bodies cannot supply a
principal. Final authorization is rechecked in the business commit transaction.
"""
from __future__ import annotations
import hashlib
import asyncio
import hmac
import secrets
import time
from dataclasses import dataclass
from typing import cast

from companion_memory.configuration.deployment import DeploymentSettings
from companion_memory.logging_service import AuditRequirement
from companion_memory.persistence import (Field, RecordSchema, SequenceSchema, ResultBoundCommandDefinition,
    ResultBoundCommand, AuditFieldBinding, AuditResultBinding, PersistenceService, UnitOfWork, Committed)
from companion_memory.persistence.daily_records import DailyRows, Record, ID, UINT, DIGEST
from companion_memory.persistence.daily_results import FACT, TARGETS, target
from companion_memory.persistence.owned_statements import OwnerFailure
from .records import DISPATCH, TABLES, ACCOUNT, SESSION, TOKEN, WIZARD, WIZARD_PART, CONFIRMATION, management_catalog


def digest(value: str | bytes) -> str:
    return hashlib.sha256(value.encode() if type(value) is str else cast(bytes,value)).hexdigest()


def password_digest(password: str, salt: str) -> str:
    """Derive a password verifier with bounded memory, rejecting trivial lengths."""
    if type(password) is not str or not 12 <= len(password.encode()) <= 1024:
        raise OwnerFailure('INVALID_INPUT', 'credential', 'INVALID_SHAPE')
    return hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1,
                          maxmem=64 * 1024 * 1024, dklen=64).hex()


@dataclass(frozen=True, slots=True)
class Principal:
    """Verified request capability; expiry and revocation still require rechecking."""
    kind: str
    identity: str
    verifier: str
    revision: int
    host_id: str | None
    entries: tuple[str, ...]
    operations: tuple[str, ...]
    csrf_digest: str | None
    expires_at_us: int = 0


class IdentityAuthority:
    """Management owner for bounded credentials, sessions, wizard and previews."""
    def __init__(self):
        self.catalog = management_catalog()
        self.bound = False
        self.closed = False
        self.login_lock = asyncio.Lock()
        self.delivery_lock = asyncio.Lock()
        self.commands = tuple(self._definition(kind, table, schema) for kind, table, schema in (
            ('establish_administrator', 'account', ACCOUNT), ('record_login', 'account', ACCOUNT),
            ('change_password', 'account', ACCOUNT), ('create_session', 'sessions', SESSION),
            ('revoke_session', 'sessions', SESSION), ('create_host_token', 'host_tokens', TOKEN),
            ('revoke_host_token', 'host_tokens', TOKEN), ('save_wizard', 'wizard', WIZARD),
            ('advance_wizard', 'wizard', WIZARD),
            ('set_model_dispatch', 'model_dispatch', DISPATCH),
            ('prepare_object_confirmation', 'confirmations', CONFIRMATION),
            ('consume_object_confirmation', 'confirmations', CONFIRMATION),
        ))

    def _definition(self, kind: str, table: str, schema: RecordSchema) -> ResultBoundCommandDefinition:
        # Retired verification rows are bounded ephemeral resources. Their exact
        # identities/revisions stay in the necessary audit and original receipt.
        fact = RecordSchema(FACT.fields + (Field('retired', SequenceSchema(
            RecordSchema((Field('object_id', ID), Field('revision', UINT))), 0, 4)),)) if kind in ('create_session', 'create_host_token') else FACT
        requirement = AuditRequirement('management', 'management_changed', kind.upper(), 1, ('APPLY',), fact)
        binding = AuditResultBinding('management_changed', 1, (
            AuditFieldBinding('actor_kind', 'CONSTANT', constant='SYSTEM'),
            AuditFieldBinding('actor_ref', 'INTENT', ('actor',)),
            AuditFieldBinding('reason_code', 'CONSTANT', constant='APPLY'),
            AuditFieldBinding('target_refs', 'RESULT', ('targets',)),
            AuditFieldBinding('change', 'RESULT', ('fact',)),
        ))
        def apply(uow: UnitOfWork, values: Record) -> object:
            return self._apply(kind, table, uow, values)
        extra = (Field('parts', SequenceSchema(WIZARD_PART, 0, 32)),) if kind in ('save_wizard', 'advance_wizard') else ()
        return ResultBoundCommandDefinition('management', kind, 1,
            RecordSchema((Field('request_digest', DIGEST), Field('expected_revision', UINT, nullable=True), Field('record', schema)) + extra),
            1, RecordSchema((Field('request_digest', DIGEST), Field('targets', TARGETS), Field('fact', fact))),
            (self.catalog.definition,), (requirement,), apply, RecordSchema((Field('actor', ID),)), (binding,))

    def bind(self, storage: PersistenceService, database: str, instance: str,
             settings: DeploymentSettings, bootstrap_digest: str) -> None:
        """Acquire management ownership only after the declared database is ready."""
        if self.bound:
            raise ValueError('Management authority is already bound.')
        self.storage, self.database, self.instance, self.settings = storage, database, instance, settings
        self.bootstrap_digest = bootstrap_digest
        self.lease = storage.claim_module_owner(self.catalog.definition)
        if self.lease is None:
            raise ValueError('Management authority is already owned.')
        self.rows = DailyRows(self.catalog, TABLES, storage, database, instance, 'managed-bootstrap')
        self.operations = {d.operation_kind: storage.bind_operation(d, instance) for d in self.commands}
        self.bound = True

    def _apply(self, kind: str, table: str, uow: UnitOfWork, values: Record) -> object:
        if not self.bound or self.closed:
            raise OwnerFailure('ACCESS_DENIED', 'identity', 'NOT_READY')
        value = cast(Record, values['record'])
        old = self.rows.get(table, uow, cast(str, value['object_id']))
        expected = values['expected_revision']
        if (old is None) != (expected is None) or old is not None and old['revision'] != expected:
            raise OwnerFailure('PRECONDITION_FAILED', 'revision', 'REVISION_CONFLICT')
        if kind == 'establish_administrator':
            if value['object_id'] != 'administrator' or old is not None or value['bootstrap_digest'] != self.bootstrap_digest:
                raise OwnerFailure('ACCESS_DENIED', 'identity', 'BOOTSTRAP_REJECTED')
        elif table == 'account':
            if old is None or value['object_id'] != 'administrator' or value['bootstrap_digest'] != old['bootstrap_digest']:
                raise OwnerFailure('ACCESS_DENIED', 'identity', 'BINDING_MISMATCH')
            if kind == 'record_login' and any(value[k] != old[k] for k in ('password_salt', 'password_verifier', 'credential_revision')):
                raise OwnerFailure('ACCESS_DENIED', 'identity', 'BINDING_MISMATCH')
            if kind == 'change_password' and value['credential_revision'] != cast(int, old['credential_revision']) + 1:
                raise OwnerFailure('PRECONDITION_FAILED', 'revision', 'REVISION_CONFLICT')
        elif kind.startswith('revoke_'):
            if old is None or value['revoked'] is not True or any(value[k] != old[k] for k in old if k not in ('revision', 'updated_at_us', 'revoked')):
                raise OwnerFailure('ACCESS_DENIED', 'identity', 'BINDING_MISMATCH')
        elif kind == 'create_session':
            account = self.rows.get('account', uow, 'administrator')
            if account is None or value['credential_revision'] != account['credential_revision']:
                raise OwnerFailure('ACCESS_DENIED', 'identity', 'CREDENTIAL_CHANGED')
        retired: tuple[Record, ...] = ()
        if kind in ('create_session', 'create_host_token'):
            criteria: dict[str, object] = {'now': time.time_ns() // 1000}
            if kind == 'create_session':
                criteria['credential_revision'] = value['credential_revision']
            rows = self.rows.rows.stage(table + '_retired_page', uow, criteria)
            removed = []
            for raw in rows:
                obsolete = self.rows.decode(table, raw)
                reference = {'object_id': obsolete['object_id'], 'revision': obsolete['revision']}
                deleted = self.rows.rows.stage(table + '_retire', uow, reference)
                if len(deleted) != 1:
                    raise OwnerFailure('PRECONDITION_FAILED', 'credential', 'REVISION_CONFLICT')
                removed.append(deleted[0])
            retired = tuple(removed)
            count = self.rows.rows.stage(table + '_total_count', uow, {})
            limit = self.settings.integer('management.session_limit' if kind == 'create_session' else 'management.token_limit')
            if len(count) != 1 or cast(int, count[0]['count']) >= limit:
                raise OwnerFailure('RESOURCE_BUSY', 'credential', 'IDENTITY_LIMIT_REACHED')
        changed = 1
        if kind in ('save_wizard', 'advance_wizard'):
            parts = cast(tuple[Record, ...], values['parts'])
            if len(parts) != value['part_count'] or value['object_id'] != 'wizard':
                raise OwnerFailure('INVALID_INPUT', 'wizard', 'INVALID_SHAPE')
            content = ''.join(cast(str, part['body']) for part in parts)
            if digest(content) != value['content_digest']:
                raise OwnerFailure('INVALID_INPUT', 'wizard', 'CONTENT_MISMATCH')
            if kind == 'save_wizard' and old is not None and old['state'] in ('INITIALIZING', 'AWAITING_REVIEW', 'COMPLETE'):
                raise OwnerFailure('PRECONDITION_FAILED', 'wizard', 'INITIALIZATION_STARTED')
            if kind == 'advance_wizard':
                allowed = {'DRAFT': ('INITIALIZING',), 'INITIALIZING': ('AWAITING_REVIEW',),
                    'AWAITING_REVIEW': ('AWAITING_REVIEW', 'REJECTED', 'COMPLETE'), 'REJECTED': ('AWAITING_REVIEW',)}
                if (old is None or value['state'] not in allowed.get(cast(str, old['state']), ())
                        or value['content_digest'] != old['content_digest']
                        or old['initialization_key'] is not None and value['initialization_key'] != old['initialization_key']):
                    raise OwnerFailure('PRECONDITION_FAILED', 'wizard', 'STATE_MISMATCH')
            for ordinal, part in enumerate(parts):
                if (part['wizard_revision'] != value['revision'] or part['ordinal'] != ordinal
                        or part['object_id'] != f'wizard-{value["revision"]}-{ordinal}'):
                    raise OwnerFailure('INVALID_INPUT', 'wizard', 'BINDING_MISMATCH')
                self.rows.write('wizard_parts', uow, part, None)
                changed += 1
        saved = self.rows.write(table, uow, value, cast(int | None, expected))
        targets = (target(cast(str, saved['object_id']), cast(int, saved['revision']), cast(int | None, expected)),)
        fact_value: dict[str, object] = {'rows_changed': changed + len(retired), 'targets': targets}
        if kind in ('create_session', 'create_host_token'):
            fact_value['retired'] = retired
        return {'request_digest': values['request_digest'], 'targets': targets, 'fact': fact_value}

    def row(self, identity: str, fields: dict[str, object], old: Record | None = None) -> dict[str, object]:
        """Create the canonical next revision; business callers never get row access."""
        now = time.time_ns() // 1000
        return cast(dict[str, object], dict(old or {}) | {'format_version': 1, 'object_id': identity,
            'revision': 1 if old is None else cast(int, old['revision']) + 1,
            'database_id': self.database, 'instance_id': self.instance,
            'config_snapshot_id': 'managed-bootstrap',
            'created_at_us': now if old is None else old['created_at_us'], 'updated_at_us': now, **fields})

    async def write(self, kind: str, key: str, row: dict[str, object], expected: int | None,
                    request_digest: str, actor: str = 'administrator', *, parts: tuple[dict[str, object], ...] | None = None):
        """Keep original request identity; a duplicate key never mints new secrets."""
        definition = next(d for d in self.commands if d.operation_kind == kind)
        prior = await self.confirm_request(kind, key, request_digest)
        if prior is not None:
            return prior
        values = {'request_digest': request_digest, 'expected_revision': expected, 'record': row}
        if parts is not None:
            values['parts'] = parts
        command = ResultBoundCommand(1, values,
                                     {a.event_slot: {'actor': actor} for a in definition.required_audits})
        return await self.operations[kind].execute(key, command)

    async def confirm_request(self, kind: str, key: str, request_digest: str):
        """Resolve original evidence before checking mutable current revisions."""
        from companion_memory.persistence import Found, NotFound
        prior = await self.operations[kind].read_receipt(key)
        if type(prior) is Found:
            if cast(Record, prior.value.result)['request_digest'] != request_digest:
                raise OwnerFailure('IDEMPOTENCY_CONFLICT', 'operation', 'CONTENT_MISMATCH')
            return Committed(prior.value, 'EXISTING')
        return None if type(prior) is NotFound else prior

    async def establish(self, key: str, bootstrap_secret: str, password: str):
        """Consume a deployment-held credential; anonymous first arrival grants nothing."""
        if not hmac.compare_digest(digest(bootstrap_secret), self.bootstrap_digest):
            raise OwnerFailure('ACCESS_DENIED', 'credential', 'BOOTSTRAP_REJECTED')
        salt = hmac.new(bootstrap_secret.encode(), key.encode(), hashlib.sha256).hexdigest()
        verifier = await asyncio.to_thread(password_digest, password, salt)
        request = digest(key + verifier)
        return await self.write('establish_administrator', key, self.row('administrator', {
            'password_salt': salt, 'password_verifier': verifier, 'credential_revision': 1,
            'failed_attempts': 0, 'window_started_at_us': 0, 'bootstrap_digest': self.bootstrap_digest,
            'establishment_key': key}), None, request, 'bootstrap')

    async def authenticate(self, credential: str, *, host: bool) -> Principal:
        """Lookup by one-way bearer identity; no principal is accepted from JSON."""
        if type(credential) is not str or not 32 <= len(credential) <= 256:
            raise OwnerFailure('ACCESS_DENIED', 'credential', 'AUTHENTICATION_REQUIRED')
        verifier = digest(credential)
        row = await self.rows.read('host_tokens' if host else 'sessions', verifier)
        account = None if host else await self.rows.read('account', 'administrator')
        now = time.time_ns() // 1000
        if (row is None or row['revoked'] is not False or cast(int, row['expires_at_us']) <= now
                or not hmac.compare_digest(cast(str, row['verifier']), verifier)
                or not host and (account is None or row['credential_revision'] != account['credential_revision'])):
            raise OwnerFailure('ACCESS_DENIED', 'credential', 'AUTHENTICATION_REQUIRED')
        return Principal('host' if host else 'administrator', verifier, verifier, cast(int, row['revision']),
            cast(str, row['host_id']) if host else None, cast(tuple[str, ...], row['entries']) if host else (),
            cast(tuple[str, ...], row['operations']) if host else (), None if host else cast(str, row['csrf_digest']), cast(int, row['expires_at_us']))

    def check_commit(self, principal: Principal, uow: UnitOfWork, operation: str, entry: str | None) -> bool:
        """Recheck current authority in the writer transaction immediately before commit."""
        row = self.rows.get('host_tokens' if principal.kind == 'host' else 'sessions', uow, principal.identity)
        if (row is None or row['revoked'] is not False or row['revision'] != principal.revision
                or cast(int, row['expires_at_us']) <= time.time_ns() // 1000):
            return False
        if principal.kind == 'host':
            return operation in cast(tuple[str, ...], row['operations']) and entry in cast(tuple[str, ...], row['entries'])
        account = self.rows.get('account', uow, 'administrator')
        return account is not None and row['credential_revision'] == account['credential_revision']

    async def login(self, key: str, password: str):
        """Persist failed-attempt limits and issue a finite random browser session."""
        if self.login_lock.locked():
            raise OwnerFailure('RESOURCE_BUSY', 'credential', 'LOGIN_BUSY')
        async with self.login_lock:
            return await self._login(key, password)

    async def _login(self, key: str, password: str):
        account = await self.rows.read('account', 'administrator')
        if account is None:
            raise OwnerFailure('ACCESS_DENIED', 'credential', 'AUTHENTICATION_REQUIRED')
        now = time.time_ns() // 1000
        window = cast(int, account['window_started_at_us'])
        attempts = cast(int, account['failed_attempts'])
        if now - window >= self.settings.integer('management.login_window_seconds') * 1000000:
            window, attempts = now, 0
        if attempts >= self.settings.integer('management.login_attempts'):
            raise OwnerFailure('RESOURCE_BUSY', 'credential', 'LOGIN_RATE_LIMITED')
        try:
            verifier = await asyncio.to_thread(password_digest, password, cast(str, account['password_salt']))
            accepted = hmac.compare_digest(verifier, cast(str, account['password_verifier']))
        except OwnerFailure:
            accepted = False
        request = hmac.new(bytes.fromhex(self.bootstrap_digest), (key + ':' + password).encode(), hashlib.sha256).hexdigest()
        outcome = await self.write('record_login', key, self.row('administrator', {
            'failed_attempts': 0 if accepted else attempts + 1, 'window_started_at_us': window}, account),
            cast(int, account['revision']), request, 'login')
        if type(outcome) is not Committed:
            return outcome, None
        if not accepted:
            raise OwnerFailure('ACCESS_DENIED', 'credential', 'AUTHENTICATION_REQUIRED')
        raw, csrf = secrets.token_urlsafe(48), secrets.token_urlsafe(32)
        session = self.row(digest(raw), {'verifier': digest(raw), 'csrf_digest': digest(csrf),
            'credential_revision': account['credential_revision'], 'expires_at_us': now + self.settings.integer('management.session_seconds') * 1000000,
            'revoked': False})
        outcome = await self.write('create_session', key, session, None, digest(key), 'login')
        return outcome, {'session': raw, 'csrf': csrf} if type(outcome) is Committed and outcome.source == 'NEW' else None

    def close(self) -> bool:
        if self.closed:
            return True
        if self.bound and self.lease is not None and not self.lease.release():
            return False
        self.lease = None
        self.closed = True
        return True

    async def revoke(self, key: str, identity: str, expected_revision: int, *, host: bool):
        """Revoke exactly the displayed identity revision; never undo business facts."""
        table = 'host_tokens' if host else 'sessions'
        kind = 'revoke_host_token' if host else 'revoke_session'
        request = digest(f'{identity}:{expected_revision}')
        prior = await self.confirm_request(kind, key, request)
        if prior is not None:
            return prior
        old = await self.rows.read(table, identity)
        if old is None or old['revision'] != expected_revision:
            raise OwnerFailure('PRECONDITION_FAILED', 'revision', 'REVISION_CONFLICT')
        return await self.write(kind, key, self.row(identity, {'revoked': True}, old), expected_revision, request)

    async def create_token(self, key: str, host_id: str, entries: tuple[str, ...],
                           operations: tuple[str, ...], expires_at_us: int):
        """Issue independent authority with explicit finite host, entry and operation sets."""
        from .records import HOST_OPERATIONS
        from companion_memory.persistence.schema import valid_identifier
        if (not valid_identifier(host_id) or not 1 <= len(entries) <= 64 or len(set(entries)) != len(entries)
                or any(not valid_identifier(entry) for entry in entries) or not operations
                or len(set(operations)) != len(operations) or not set(operations) <= set(HOST_OPERATIONS)
                or type(expires_at_us) is not int):
            raise OwnerFailure('INVALID_INPUT', 'scope', 'INVALID_SHAPE')
        import json
        request = digest(json.dumps([host_id, entries, operations, expires_at_us], separators=(',', ':')))
        prior = await self.confirm_request('create_host_token', key, request)
        if prior is not None:
            return prior, None
        if not time.time_ns() // 1000 < expires_at_us <= time.time_ns() // 1000 + self.settings.integer('management.token_max_seconds') * 1000000:
            raise OwnerFailure('INVALID_INPUT', 'scope', 'INVALID_SHAPE')
        raw = secrets.token_urlsafe(48)
        value = self.row(digest(raw), {'verifier': digest(raw), 'host_id': host_id,
            'entries': entries, 'operations': operations, 'expires_at_us': expires_at_us, 'revoked': False})
        outcome = await self.write('create_host_token', key, value, None, request)
        return outcome, raw if type(outcome) is Committed and outcome.source == 'NEW' else None

    async def update_password(self, key: str, expected_revision: int, current_password: str, password: str):
        """Verify current credentials and invalidate every older session atomically."""
        import asyncio
        request = hmac.new(bytes.fromhex(self.bootstrap_digest),
            (key + ':' + str(expected_revision) + ':' + password).encode(), hashlib.sha256).hexdigest()
        prior = await self.confirm_request('change_password', key, request)
        if prior is not None:
            return prior
        old = await self.rows.read('account', 'administrator')
        if old is None or old['revision'] != expected_revision:
            raise OwnerFailure('PRECONDITION_FAILED', 'revision', 'REVISION_CONFLICT')
        verified = await asyncio.to_thread(password_digest, current_password, cast(str, old['password_salt']))
        if not hmac.compare_digest(verified, cast(str, old['password_verifier'])):
            raise OwnerFailure('ACCESS_DENIED', 'credential', 'AUTHENTICATION_REQUIRED')
        salt = secrets.token_hex(32)
        verifier = await asyncio.to_thread(password_digest, password, salt)
        return await self.write('change_password', key, self.row('administrator', {
            'password_salt': salt, 'password_verifier': verifier,
            'credential_revision': cast(int, old['credential_revision']) + 1}, old),
            expected_revision, request)

    def permission(self, principal: Principal, operation: str, entry: str | None = None):
        """Carry authenticated authority through the actual transaction boundary."""
        if self.lease is None or self.closed:
            raise OwnerFailure('ACCESS_DENIED', 'identity', 'NOT_READY')
        return self.storage.managed_request_permission(self.lease, self.catalog.definition,
            lambda uow: self.check_commit(principal, uow, operation, entry))

    async def recheck(self, principal: Principal) -> None:
        """Recheck identity after bounded projections without exposing bearer secrets."""
        row = await self.rows.read('host_tokens' if principal.kind == 'host' else 'sessions', principal.identity)
        if row is None or row['revision'] != principal.revision or row['revoked'] or cast(int, row['expires_at_us']) <= time.time_ns() // 1000:
            raise OwnerFailure('ACCESS_DENIED', 'credential', 'AUTHENTICATION_REQUIRED')
        if principal.kind == 'administrator':
            account = await self.rows.read('account', 'administrator')
            if account is None or account['credential_revision'] != row['credential_revision']:
                raise OwnerFailure('ACCESS_DENIED', 'credential', 'AUTHENTICATION_REQUIRED')

    def stage_object_confirmation(self, uow: UnitOfWork, confirmation_id: str, action: str, target_id: str,
                                  target_revision: int, impact_digest: str, expires_at_us: int):
        """Participate in a memory-owned preview without exposing management tables."""
        if self.rows.get('confirmations', uow, confirmation_id) is not None:
            raise OwnerFailure('IDEMPOTENCY_CONFLICT', 'confirmation', 'ALREADY_EXISTS')
        value = self.row(confirmation_id, {'action': action, 'target_id': target_id, 'target_revision': target_revision,
            'impact_digest': impact_digest, 'expires_at_us': expires_at_us, 'consumed_by': None})
        return self.rows.write('confirmations', uow, value, None)

    def consume_object_confirmation(self, uow: UnitOfWork, confirmation_id: str, key: str, action: str,
                                    target_id: str, target_revision: int, impact_digest: str, now: int):
        """Consume exactly one unexpired preview in the same object transaction."""
        old = self.rows.get('confirmations', uow, confirmation_id)
        if (old is None or old['consumed_by'] is not None or cast(int, old['expires_at_us']) <= now
                or any(old[name] != expected for name, expected in (('action', action), ('target_id', target_id),
                    ('target_revision', target_revision), ('impact_digest', impact_digest)))):
            raise OwnerFailure('PRECONDITION_FAILED', 'confirmation', 'CONFIRMATION_INVALID')
        return self.rows.write('confirmations', uow, self.row(confirmation_id, {'consumed_by': key}, old), cast(int, old['revision']))

    async def save_draft(self, key: str, expected_revision: int | None, draft: dict[str, object]):
        """Atomically save a bounded draft and its immutable content chunks."""
        import json
        body = json.dumps(draft, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
        if len(body.encode()) > 393216:
            raise OwnerFailure('INVALID_INPUT', 'wizard', 'LIMIT_EXCEEDED')
        old = await self.rows.read('wizard', 'wizard')
        revision = 1 if expected_revision is None else expected_revision + 1
        # Unicode characters are never split across persistent chunks.
        chunks = tuple(body[offset:offset + 4096] for offset in range(0, len(body), 4096))
        if len(chunks) > 32:
            raise OwnerFailure('INVALID_INPUT', 'wizard', 'LIMIT_EXCEEDED')
        parts = tuple(self.row(f'wizard-{revision}-{ordinal}', {'wizard_revision': revision,
            'ordinal': ordinal, 'body': chunk}) for ordinal, chunk in enumerate(chunks))
        return await self.write('save_wizard', key, self.row('wizard', {'state': 'DRAFT',
            'part_count': len(parts), 'content_digest': digest(body), 'initialization_key': None,
            'persona_run_id': None}, old), expected_revision, digest(str(expected_revision) + body), parts=parts)

    async def read_draft(self) -> dict[str, object]:
        """Recover only the committed wizard version; partial chunks are invisible."""
        import json
        wizard = await self.rows.read('wizard', 'wizard')
        if wizard is None:
            return {'state': 'DRAFT', 'revision': None, 'draft': {}}
        count = cast(int, wizard['part_count'])
        if not 0 <= count <= 32:
            raise OwnerFailure('INTEGRITY_FAILURE', 'wizard', 'RECORD_INVALID')
        chunks = []
        for ordinal in range(count):
            part = await self.rows.read('wizard_parts', f'wizard-{wizard["revision"]}-{ordinal}')
            if part is None or part['wizard_revision'] != wizard['revision'] or part['ordinal'] != ordinal:
                raise OwnerFailure('INTEGRITY_FAILURE', 'wizard', 'RECORD_INVALID')
            chunks.append(cast(str, part['body']))
        body = ''.join(chunks)
        if digest(body) != wizard['content_digest']:
            raise OwnerFailure('INTEGRITY_FAILURE', 'wizard', 'RECORD_INVALID')
        return {'state': wizard['state'], 'revision': wizard['revision'], 'draft': json.loads(body),
            'initialization_key': wizard['initialization_key'], 'persona_run_id': wizard['persona_run_id']}

    async def advance_wizard(self, key: str, expected_revision: int, state: str,
                             initialization_key: str, persona_run_id: str | None = None):
        """Preserve original wizard material across explicit initialization phases."""
        import json
        current = await self.read_draft()
        old = await self.rows.read('wizard', 'wizard')
        if old is None or old['revision'] != current['revision']:
            raise OwnerFailure('PRECONDITION_FAILED', 'wizard', 'REVISION_CONFLICT')
        body = json.dumps(current['draft'], ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        next_revision = expected_revision + 1
        parts = tuple(self.row(f'wizard-{next_revision}-{ordinal}', {'wizard_revision': next_revision,
            'ordinal': ordinal, 'body': body[offset:offset + 4096]}) for ordinal, offset in enumerate(range(0, len(body), 4096)))
        return await self.write('advance_wizard', key, self.row('wizard', {'state': state,
            'initialization_key': initialization_key, 'persona_run_id': persona_run_id}, old), expected_revision,
            digest(json.dumps([expected_revision, state, initialization_key, persona_run_id], separators=(',', ':'))), parts=parts)
