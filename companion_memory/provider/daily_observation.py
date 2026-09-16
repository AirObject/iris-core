"""Native mixed-ledger observation with no payload or execution authority.

Only this instance's configured accounts and roles are visible. All usage comes
from the same ledger aggregate; missing observations remain explicit counts.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
from types import MappingProxyType
from typing import cast
from companion_memory.persistence.owned_statements import OwnerFailure
from .daily_service import DailyProvider
from .values import Found, NotFound, as_record, freeze, dump, is_identifier, InvalidData
from .daily_stored_schema import ROLES

_ISSUER = object()


@dataclass(frozen=True, slots=True, init=False)
class DailyProviderObserver:
    owner: DailyProvider
    _issuer: object

    def __init__(self):
        raise TypeError('A native daily host must issue observation.')

    def ready(self):
        if self._issuer is not _ISSUER or not self.owner.ready:
            raise OwnerFailure('INVALID_STATE', 'state', 'SERVICE_CLOSED')

    def origins(self):
        if self.owner.transport is None:raise InvalidData()
        return MappingProxyType({role: transport.execution_kind for role, transport in self.owner.transports.items()} |
            {'EMBEDDING_DOCUMENT': self.owner.transport.execution_kind, 'EMBEDDING_QUERY': self.owner.transport.execution_kind})

    async def get_request(self, request_id: object):
        self.ready()
        if not is_identifier(request_id):
            raise InvalidData()
        visible = await self.owner.ledger.read('requests_visible', {'object_id': request_id,
            'scopes': dump((self.owner.instance,)), 'caller_module': None, 'extension_id': None})
        if not visible:
            return NotFound()
        request = visible[0]
        attempts = await self.owner.ledger.read('attempts_for_request', {'request_id': request_id})
        keys = ('object_id', 'revision', 'account_id', 'profile_id', 'capability', 'task_role', 'phase', 'outcome', 'attempt_count')
        attempt_keys = ('object_id', 'revision', 'state', 'logical_outcome', 'confirmed_started', 'usage')
        handoff = await self.owner.ledger.get('handoffs', cast(str,request['handoff_id'])) if request['handoff_id'] is not None else None
        self.ready()
        return Found(MappingProxyType({'request': MappingProxyType({k: request[k] for k in keys}),
            'attempts': tuple(MappingProxyType({k: a[k] for k in attempt_keys}) for a in attempts),
            'adapter': self.origins()[cast(str,request['task_role'])], 'local_commit': 'CONFIRMED',
            'consumer_cleanup': as_record(handoff['embedding_cleanup'])['state'] if handoff else None,
            'actual_cleanup_pending': self.owner.cleanup_pending}))

    async def get_budget_state(self):
        self.ready()
        accounts = {cast(str,p['account_id']) for p in self.owner.profiles}
        budgets = await self.owner.ledger.read('budget_windows_page', {'after': '', 'limit': 4})
        keys = ('object_id', 'account_id', 'window_id', 'attempt_count', 'known_subtotal_atoms', 'held_atoms', 'risk_state', 'billing_mode')
        rows = []
        for budget in budgets:
            if budget['account_id'] not in accounts:
                raise OwnerFailure('STORAGE_FAILED', 'storage', 'INTEGRITY_FAILURE')
            policy = as_record(budget['policy'])
            rows.append(MappingProxyType({k: budget[k] for k in keys} | {'cost_limit_atoms': policy['cost_limit_atoms'],
                'available_atoms': None if policy['billing_mode'] == 'USAGE_ONLY_TRIAL' else cast(int, policy['cost_limit_atoms']) - cast(int, budget['known_subtotal_atoms']) - cast(int, budget['held_atoms'])}))
        self.ready()
        return Found(tuple(rows))

    async def query_usage(self, raw: object):
        self.ready(); query = as_record(freeze(raw, 8192))
        if set(query) != {'start', 'end', 'caller_scope', 'capability', 'task_role', 'profile_id', 'account_id', 'group_by'}:
            raise InvalidData()
        if query['group_by'] not in ('NONE', 'CAPABILITY', 'TASK_ROLE', 'PROFILE', 'ACCOUNT'):
            raise InvalidData()
        times = {}
        for name in ('start', 'end'):
            value = query[name]
            if type(value) is not str or len(value) > 40:
                raise InvalidData()
            parsed = datetime.fromisoformat(value)
            if parsed.tzinfo is not timezone.utc:
                raise InvalidData()
            times[name] = parsed
        if times['start'] >= times['end']:
            raise InvalidData()
        for name in ('caller_scope', 'capability', 'task_role', 'profile_id', 'account_id'):
            if query[name] is not None and not is_identifier(query[name]):
                raise InvalidData()
        if (query['caller_scope'] not in (None, self.owner.instance)
                or query['account_id'] not in (None, *(p['account_id'] for p in self.owner.profiles))
                or query['profile_id'] not in (None, *(p['profile_id'] for p in self.owner.profiles))):
            raise OwnerFailure('ACCESS_DENIED', 'capability', 'OPERATION_NOT_GRANTED')
        if query['capability'] not in (None, 'GENERATION', 'MEDIA_UNDERSTANDING', 'EMBEDDING') or query['task_role'] not in (None, *ROLES):
            raise InvalidData()
        parameters:dict[str,object] = {n: query[n] for n in ('capability', 'task_role', 'profile_id', 'account_id', 'group_by')}
        parameters.update({n: v.isoformat(timespec='microseconds') for n, v in times.items()})
        parameters.update(scopes=dump((self.owner.instance,)), limit=17)
        rows = await self.owner.ledger.read('usage_aggregate', parameters)
        if len(rows) > 16 or any(r['invalid_count'] != 0 for r in rows):
            raise OwnerFailure('STORAGE_FAILED', 'storage', 'INTEGRITY_FAILURE')
        self.ready()
        return Found(MappingProxyType({'range': query, 'rows': rows, 'adapters': self.origins(),
            'sample_count': sum(cast(int, r['request_count']) for r in rows),
            'cost_semantics': 'KNOWN_SUBTOTAL_WITH_MISSING_COUNTS', 'actual_cleanup_pending': self.owner.cleanup_pending}))


def observer(owner: DailyProvider) -> DailyProviderObserver:
    if type(owner) is not DailyProvider or not owner.ready:
        raise ValueError('Native initialized daily Provider required.')
    port = object.__new__(DailyProviderObserver)
    object.__setattr__(port, 'owner', owner); object.__setattr__(port, '_issuer', _ISSUER)
    return port
