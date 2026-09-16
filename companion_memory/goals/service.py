"""Atomic goal lifecycle, bounded exact deduplication and reminder facts.

Only this owner writes goals, source attribution, aliases, tasks and delivery
records. Every handler returns the revision of an actual changed progress root;
remote I/O belongs to the separately bounded dispatcher after intent commit.
"""
from dataclasses import dataclass
from types import MappingProxyType
from collections.abc import Callable
from hashlib import sha256
from companion_memory.persistence.content_codec import encode_content
import unicodedata
from companion_memory.persistence import UnitOfWork, Value
from companion_memory.persistence.owned_statements import OwnerFailure
from companion_memory.information.records import Record, checked, record, integer, text, identity, fact
from .initialization import GoalsOwner
from .inputs import SCHEMAS, valid_world_scope


@dataclass(frozen=True, slots=True)
class GoalAuthority:
    """Trusted route membership and verified source basis for one operation."""
    route_ids: tuple[str, ...]
    basis_id: str
    internal_basis: Callable[[UnitOfWork, str], bool] | None = None
    entry_id: str | None = None


@dataclass(frozen=True, slots=True)
class GoalEffect:
    """Actual owner summary and the command's directly changed roots."""
    summary: Record
    targets: tuple[Record, ...]


def signature(value: Record) -> str:
    """Exact merge identity; no original text or attribution is normalized away."""
    subjects = value['subject_ids']
    if type(subjects) is not tuple:
        raise OwnerFailure('STORAGE_FAILED', 'storage', 'INTEGRITY_FAILURE')
    normalized = ' '.join(unicodedata.normalize('NFKC', text(value['content'])).casefold().split())
    parts = (normalized, tuple(sorted(text(v) for v in subjects)), value['world_scope'], value['deadline'], value['reminder_lead_seconds'], value['route_id'])
    return identity('goal_signature', *parts, value['entry_id']) if 'entry_id' in value else identity('goal_signature', *parts)


def letter_terms(content: str) -> frozenset[str]:
    """Local overlap proposes review candidates and never semantic equivalence."""
    return frozenset(character for character in unicodedata.normalize('NFKC', content).casefold()
        if unicodedata.category(character)[0] in ('L', 'N'))


class GoalTransaction:
    """One goals-owned UoW; its write count comes only from successful statements."""
    def __init__(self, owner: 'GoalsService', uow: UnitOfWork, now: int):
        self.owner, self.uow, self.now, self.changed = owner, uow, now, 0

    def get(self, name: str, keys: dict[str, Value]) -> Record:
        value = self.owner._records.get(name, self.uow, keys)
        if value is None:
            raise OwnerFailure('PRECONDITION_FAILED', 'revision', 'REVISION_CONFLICT')
        return value

    def children(self, name: str, keys: dict[str, Value]) -> tuple[Record, ...]:
        return tuple(self.owner._records.unpack(name, r) for r in self.owner._records.rows.stage(name + '_children', self.uow, keys))

    def write(self, name: str, value: Record | dict[str, Value], before: Record | None = None) -> Record:
        projections: dict[str, Value] | None = {'signature': signature(MappingProxyType(dict(value)))} if name == 'goal' else None
        result = self.owner._records.write(name, self.uow, value, expected_revision=integer(before['revision']) if before else None, projections=projections)
        self.changed += 1
        return result

    def update(self, name: str, before: Record, **values: Value) -> Record:
        updated = dict(before); updated.update(values); updated['revision'] = integer(before['revision']) + 1
        return self.write(name, updated, before)

    def check_revision(self, value: Record, expected: Value) -> None:
        if value['revision'] != expected:
            raise OwnerFailure('PRECONDITION_FAILED', 'revision', 'REVISION_CONFLICT')

    def resolve(self, goal_id: str) -> Record:
        value = self.get('goal', {'goal_id': goal_id})
        if value['canonical_id'] != value['goal_id']:
            # Callers must obtain the returned canonical revision before updating.
            raise OwnerFailure('PRECONDITION_FAILED', 'revision', 'REVISION_CONFLICT')
        return value

    def plans(self, goal: Record, revision: int) -> None:
        if goal['deadline'] is None:
            return
        deadline = integer(goal['deadline']); lead = integer(goal['reminder_lead_seconds']) * 1000000
        kinds = ('UPCOMING', 'DUE') if lead > 0 else ('DUE',)
        for kind in kinds:
            due = deadline if kind == 'DUE' else deadline - lead
            state = 'SUPERSEDED' if kind == 'UPCOMING' and self.now >= deadline else 'WAIT_DEDUP' if goal['dedup_state'] in ('PENDING', 'RUNNING') else 'PENDING'
            self.write('reminder_plan', {'plan_id': identity('reminder', goal['goal_id'], revision, kind), 'goal_id': goal['goal_id'],
                'route_id': goal['route_id'], 'config_snapshot_id': self.owner.binding.config_snapshot_id,
                'deadline_revision': revision, 'kind': kind, 'due_at': due, 'deadline': deadline, 'status': state,
                'delivery_id': None, 'revision': 1, 'updated_at': self.now})

    def cancel_plans(self, goal_id: Value) -> None:
        for plan in self.children('reminder_plan', {'goal_id': goal_id}):
            self.update('reminder_plan', plan, status='CANCELLED', updated_at=self.now)

    def finish_task(self, task: Record, goal: Record, status: str) -> Record:
        final = self.update('dedup_task', task, status=status)
        self.update('goal', goal, dedup_state=status, updated_at=self.now)
        for plan in self.children('reminder_plan', {'goal_id': goal['goal_id']}):
            if plan['status'] == 'WAIT_DEDUP':
                self.update('reminder_plan', plan, status='SUPERSEDED' if plan['kind'] == 'UPCOMING' and self.now >= integer(plan['deadline']) else 'PENDING', updated_at=self.now)
        return final


class GoalsService(GoalsOwner):
    """Goals metadata and all business records share a single native owner lease."""
    async def recover(self, expected_fact: Record) -> Record:
        from .recovery import verify_goals
        metadata = await super().recover(expected_fact)
        self.ready = False
        await verify_goals(self)
        self.ready = True
        return metadata

    def _terms(self, value: Record, authority: GoalAuthority) -> None:
        if 'world_scope' in value and not valid_world_scope(text(value['world_scope'])):
            raise OwnerFailure('INVALID_INPUT', 'goal', 'INVALID_SHAPE')
        if value['deadline'] is not None and (value['reminder_lead_seconds'] is None or value['route_id'] is None):
            raise OwnerFailure('INVALID_INPUT', 'goal', 'INVALID_SHAPE')
        if value['route_id'] is not None and value['route_id'] not in authority.route_ids:
            raise OwnerFailure('ACCESS_DENIED', 'goal', 'BINDING_MISMATCH')

    def apply(self, kind: str, uow: UnitOfWork, payload: object, key: str, now: int, authority: GoalAuthority, *, trusted_goal_id: str | None = None) -> GoalEffect:
        """Apply one statically selected command, with every dependent row atomic."""
        if not self.ready or kind not in SCHEMAS:
            raise OwnerFailure('INVALID_STATE', 'goal', 'NOT_READY')
        value = checked(SCHEMAS[kind], payload, 4096)
        if trusted_goal_id is not None and kind != 'goal_inject_internal':
            raise OwnerFailure('ACCESS_DENIED', 'goal', 'OPERATION_NOT_GRANTED')
        tx = GoalTransaction(self, uow, now)
        before: Record | None = None
        direct: tuple[Record, ...] | None = None
        if kind in ('goal_inject_external', 'goal_inject_internal'):
            self._terms(value, authority)
            if self.daily_format and authority.entry_id is None:
                raise OwnerFailure('ACCESS_DENIED','goal','BINDING_MISMATCH')
            subjects = value['subject_ids']
            if type(subjects) is not tuple or tuple(sorted(set(text(v) for v in subjects))) != subjects:
                raise OwnerFailure('INVALID_INPUT', 'goal', 'INVALID_SHAPE')
            if integer(self._records.rows.stage('open_count', uow, {})[0]['count']) >= 1000:
                raise OwnerFailure('RESOURCE_BUSY', 'goal', 'CAPACITY_REACHED')
            internal = kind == 'goal_inject_internal'
            if internal and (authority.internal_basis is None or not authority.internal_basis(uow, text(value['basis_id']))):
                raise OwnerFailure('ACCESS_DENIED', 'goal', 'OPERATION_NOT_GRANTED')
            goal_id = trusted_goal_id if trusted_goal_id is not None else identity('goal', self.binding.instance_id, key)
            goal = tx.write('goal', {name: value[name] for name in ('content', 'subject_ids', 'world_scope', 'deadline', 'reminder_lead_seconds', 'route_id')} |
                {'goal_id': goal_id, 'canonical_id': goal_id, 'revision': 1, 'status': 'OPEN', 'created_at': now, 'updated_at': now,
                 'source_count': 1, 'alias_count': 0, 'dedup_state': 'PENDING'} | ({'entry_id': authority.entry_id} if self.daily_format else {}))
            tx.write('source', {'goal_id': goal_id, 'source_id': value['source_id'], 'basis_id': value['basis_id'] if internal else authority.basis_id,
                'origin': 'TRUSTED_INTERNAL' if internal else 'EXTERNAL', 'created_at': now})
            tx.write('dedup_task', {'task_id': identity('goal_dedup', goal_id), 'goal_id': goal_id, 'config_snapshot_id': self.binding.config_snapshot_id,
                'owner_id': None, 'revision': 1, 'goal_revision': 1, 'status': 'PENDING', 'created_at': now, 'started_at': None,
                'deadline_at': now + 5000000, 'candidate_count': 0, 'cursor': None, 'operation_key': key})
            tx.plans(goal, 1); after = goal; object_id = goal_id
        elif kind in ('goal_status', 'goal_deadline'):
            before = tx.resolve(text(value['goal_id'])); tx.check_revision(before, value['expected_revision'])
            if before['status'] != 'OPEN':
                raise OwnerFailure('PRECONDITION_FAILED', 'goal', 'NO_CHANGE')
            if kind == 'goal_status':
                after = tx.update('goal', before, status=value['status'], updated_at=now)
            else:
                self._terms(value, authority)
                terms = {name: value[name] for name in ('deadline', 'reminder_lead_seconds', 'route_id')}
                if all(before[n] == v for n, v in terms.items()):
                    raise OwnerFailure('PRECONDITION_FAILED', 'goal', 'NO_CHANGE')
                after = tx.update('goal', before, **terms, updated_at=now)
            tx.cancel_plans(before['goal_id'])
            if kind == 'goal_deadline': tx.plans(after, integer(after['revision']))
            object_id = text(after['goal_id'])
        elif kind.startswith('goal_dedup_') or kind == 'goal_exact_merge':
            before = tx.get('dedup_task', {'task_id': value['task_id']}); tx.check_revision(before, value['expected_revision'])
            goal = tx.resolve(text(before['goal_id']))
            if kind == 'goal_dedup_claim':
                if before['status'] != 'PENDING':
                    raise OwnerFailure('PRECONDITION_FAILED', 'goal', 'NO_CHANGE')
                candidates = tuple(self._records.unpack('goal', r) for r in self._records.rows.stage('exact_candidates', uow,
                    {'signature': signature(goal), 'goal_id': goal['goal_id']}))
                if len(candidates) < 32 and not self.daily_format:
                    near = tuple(self._records.unpack('goal', r) for r in self._records.rows.stage('near_candidates', uow,
                        {'signature': signature(goal), 'goal_id': goal['goal_id'], 'world_scope': goal['world_scope']}))
                    terms = letter_terms(text(goal['content']))
                    candidates += tuple(candidate for candidate in near if terms.intersection(letter_terms(text(candidate['content']))))[:32 - len(candidates)]
                if self.daily_format:
                    near = self.structural_candidates(uow, goal)[0]
                    exact_ids = {text(candidate['goal_id']) for candidate in candidates}
                    candidates += tuple(candidate for candidate in near if candidate['goal_id'] not in exact_ids)[:32-len(candidates)]
                for candidate in candidates:
                    tx.write('dedup_candidate', {'task_id': before['task_id'], 'candidate_id': candidate['goal_id'], 'revision': candidate['revision']})
                changed_goal = tx.update('goal', goal, dedup_state='RUNNING', updated_at=now)
                after = tx.update('dedup_task', before, owner_id=value['owner_id'], status='RUNNING', started_at=now,
                    goal_revision=changed_goal['revision'], candidate_count=len(candidates), operation_key=key)
            else:
                stale_failure = kind == 'goal_dedup_finish' and value['status'] == 'FAILED'
                if before['status'] != 'RUNNING' or before['owner_id'] != value['owner_id'] or before['goal_revision'] != goal['revision'] and not stale_failure:
                    raise OwnerFailure('PRECONDITION_FAILED', 'revision', 'REVISION_CONFLICT')
                if kind == 'goal_dedup_finish':
                    after = tx.finish_task(before, goal, text(value['status']))
                else:
                    canonical_before = tx.resolve(text(value['canonical_id']))
                    self._merge(tx, before, goal, value)
                    current_goal = tx.get('goal', {'goal_id': goal['goal_id']})
                    canonical_after = tx.get('goal', {'goal_id': canonical_before['goal_id']})
                    direct = (MappingProxyType({'object_id': goal['goal_id'], 'previous_revision': goal['revision'], 'revision': current_goal['revision']}),)
                    if canonical_after['revision'] != canonical_before['revision']:
                        direct += (MappingProxyType({'object_id': canonical_before['goal_id'], 'previous_revision': canonical_before['revision'], 'revision': canonical_after['revision']}),)
                    before, after = goal, current_goal
            object_id = text(after['goal_id'] if kind == 'goal_exact_merge' else after['task_id'])
        elif kind in ('goal_plan_advance', 'goal_attempt_begin'):
            before = tx.get('reminder_plan', {'plan_id': value['plan_id']}); tx.check_revision(before, value['expected_revision'])
            goal = tx.get('goal', {'goal_id': before['goal_id']})
            if before['status'] not in ('WAIT_DEDUP', 'PENDING') or integer(before['due_at']) > now:
                raise OwnerFailure('PRECONDITION_FAILED', 'goal', 'NO_CHANGE')
            task = tx.get('dedup_task', {'task_id': identity('goal_dedup', goal['goal_id'])})
            waiting = task['status'] in ('PENDING', 'RUNNING') and now < integer(task['deadline_at'])
            if waiting:
                raise OwnerFailure('PRECONDITION_FAILED', 'goal', 'NO_CHANGE')
            superseded = goal['canonical_id'] != goal['goal_id'] or before['kind'] == 'UPCOMING' and now >= integer(before['deadline'])
            cancelled = goal['status'] != 'OPEN' or goal['deadline'] != before['deadline']
            disabled = self.configuration.candidate.information.record('goals.delivery')['sink_mode'] == 'DISABLED'
            if kind == 'goal_plan_advance':
                status = 'CANCELLED' if cancelled else 'SUPERSEDED' if superseded else 'UNSENT_UNAVAILABLE' if disabled else 'PENDING'
                if before['status'] == status:
                    raise OwnerFailure('PRECONDITION_FAILED', 'goal', 'NO_CHANGE')
                after = tx.update('reminder_plan', before, status=status, updated_at=now)
                object_id = text(after['plan_id'])
            else:
                if disabled or cancelled or superseded or before['route_id'] not in authority.route_ids:
                    raise OwnerFailure('CAPABILITY_UNAVAILABLE', 'route', 'REMINDER_SINK_UNAVAILABLE')
                if len(text(value['request_digest'])) != 64 or any(c not in '0123456789abcdef' for c in text(value['request_digest'])):
                    raise OwnerFailure('INVALID_INPUT', 'goal', 'INVALID_SHAPE')
                delivery_id = identity('delivery', before['plan_id'], key)
                intent = reminder_intent(before, goal, delivery_id, now)
                if value['request_digest'] != sha256(encode_content(intent, 2048)).hexdigest():
                    raise OwnerFailure('PRECONDITION_FAILED', 'revision', 'REVISION_CONFLICT')
                after = tx.write('attempt', {'delivery_id': delivery_id, 'plan_id': before['plan_id'], 'operation_key': key, 'attempt_no': 1,
                    'revision': 1, 'started_at': now, 'finished_at': None, 'state': 'REGISTERED', 'request_digest': value['request_digest'], 'reason': 'NOT_READY'})
                tx.update('reminder_plan', before, status='ATTEMPTING', delivery_id=delivery_id, updated_at=now)
                before = None; object_id = delivery_id
        elif kind == 'goal_attempt_finish':
            before = tx.get('attempt', {'delivery_id': value['delivery_id']}); tx.check_revision(before, value['expected_revision'])
            if before['state'] != 'REGISTERED':
                raise OwnerFailure('PRECONDITION_FAILED', 'goal', 'NO_CHANGE')
            plan = tx.get('reminder_plan', {'plan_id': before['plan_id']})
            if plan['delivery_id'] != before['delivery_id'] or plan['status'] != 'ATTEMPTING':
                raise OwnerFailure('STORAGE_FAILED', 'storage', 'INTEGRITY_FAILURE')
            after = tx.update('attempt', before, state=value['state'], finished_at=now, reason=value['reason'])
            tx.update('reminder_plan', plan, status='UNSENT_UNAVAILABLE' if value['state'] == 'NOT_SENT' else value['state'], updated_at=now)
            object_id = text(after['delivery_id'])
        else:
            raise OwnerFailure('INVALID_INPUT', 'goal', 'INVALID_SHAPE')
        summary = fact(object_id, integer(before['revision']) if before else None, integer(after['revision']), now, changed=tx.changed)
        targets = direct or (MappingProxyType({n: summary[n] for n in ('object_id', 'previous_revision', 'revision')}),)
        return GoalEffect(summary, tuple(sorted(targets, key=lambda item: text(item['object_id']))))

    def structural_candidates(self, uow: UnitOfWork, goal: Record) -> tuple[tuple[Record, ...], bool]:
        """Rank the complete same-structure group using pages of eight rows.

        Only eight complete candidates are retained. The loop is bounded by the
        existing 1000-open-goal capacity and the caller's absolute transaction
        deadline; every read uses the active native UoW.
        """
        if not self.daily_format:
            raise OwnerFailure('ACCESS_DENIED','goal','OPERATION_NOT_GRANTED')
        terms = letter_terms(text(goal['content']))
        params = {name: goal[name] for name in ('entry_id','world_scope','deadline','reminder_lead_seconds','route_id','created_at','goal_id')}
        params.update(subject_ids=encode_content(goal['subject_ids'],1024).decode(), after_at=-(2**62), after_id='')
        selected: list[Record] = []; count = 0
        while page := self._records.rows.stage('daily_structural_candidates',uow,params):
            values = [self._records.unpack('goal',row) for row in page]
            count += len(values)
            if count > 1000:
                raise OwnerFailure('STORAGE_FAILED','goal','INTEGRITY_FAILURE')
            selected = sorted(selected + values, key=lambda candidate: (
                -len(terms.intersection(letter_terms(text(candidate['content'])))), integer(candidate['created_at']),text(candidate['goal_id'])))[:8]
            params.update(after_at=values[-1]['created_at'], after_id=values[-1]['goal_id'])
        return tuple(selected), count > len(selected)

    def _merge(self, tx: GoalTransaction, task: Record, goal: Record, value: Record) -> Record:
        canonical = tx.resolve(text(value['canonical_id'])); tx.check_revision(canonical, value['canonical_revision'])
        frozen = tx.children('dedup_candidate', {'task_id': task['task_id']})
        if not any(c['candidate_id'] == canonical['goal_id'] and c['revision'] == canonical['revision'] for c in frozen):
            raise OwnerFailure('PRECONDITION_FAILED', 'revision', 'REVISION_CONFLICT')
        if (goal['status'] != 'OPEN' or canonical['status'] != 'OPEN' or signature(goal) != signature(canonical)
                or (integer(canonical['created_at']), text(canonical['goal_id'])) >= (integer(goal['created_at']), text(goal['goal_id']))):
            raise OwnerFailure('PRECONDITION_FAILED', 'revision', 'REVISION_CONFLICT')
        if goal['alias_count'] != 0:
            return tx.finish_task(task, goal, 'NEEDS_SEMANTIC_REVIEW')
        sources = tx.children('source', {'goal_id': goal['goal_id']})
        existing = {text(s['source_id']): s for s in tx.children('source', {'goal_id': canonical['goal_id']})}
        for source in sources:
            duplicate = existing.get(text(source['source_id']))
            if duplicate is not None and any(source[n] != duplicate[n] for n in ('basis_id', 'origin')):
                return tx.finish_task(task, goal, 'NEEDS_SEMANTIC_REVIEW')
        additions = tuple(s for s in sources if s['source_id'] not in existing)
        if len(existing) + len(additions) > 8 or integer(canonical['alias_count']) >= 64:
            return tx.finish_task(task, goal, 'NEEDS_SEMANTIC_REVIEW')
        for source in additions:
            tx.write('source', dict(source) | {'goal_id': canonical['goal_id']})
        tx.write('alias', {'alias_id': goal['goal_id'], 'canonical_id': canonical['goal_id'], 'revision': 1, 'created_at': tx.now})
        tx.update('goal', canonical, source_count=len(existing) + len(additions), alias_count=integer(canonical['alias_count']) + 1, updated_at=tx.now)
        tx.update('goal', goal, canonical_id=canonical['goal_id'], dedup_state='EXACT_MERGED', updated_at=tx.now)
        tx.cancel_plans(goal['goal_id'])
        return tx.update('dedup_task', task, status='EXACT_MERGED')

    async def read_learning_goals(self,entry_id:str,world_scope:str,limit:int):
        """Current open goals restricted in SQL to the frozen learning entry/world."""
        from companion_memory.persistence.schema import valid_identifier
        if not self.daily_format or not self.ready or not valid_identifier(entry_id) or not valid_world_scope(world_scope) or type(limit) is not int or not 1<=limit<=4:
            raise OwnerFailure('ACCESS_DENIED','goal','TOOL_NOT_GRANTED')
        roots=await self._records.rows.read('daily_tool_goals',{'entry_id':entry_id,'world_scope':world_scope})
        values=tuple(self._records.unpack('goal',row) for row in roots)
        if any(value['entry_id']!=entry_id or value['world_scope']!=world_scope or value['status']!='OPEN' or value['canonical_id']!=value['goal_id'] for value in values):
            raise OwnerFailure('STORAGE_FAILED','storage','INTEGRITY_FAILURE')
        return values[:limit],len(values)>limit

    async def list_open(self, now: int, after_at: int = -(2**62), after_id: str = '') -> tuple[Record, ...]:
        """Fixed eight-goal page with explicit dedup and deadline limitations."""
        if not self.ready:
            raise OwnerFailure('INVALID_STATE', 'goal', 'NOT_READY')
        rows = await self._records.rows.read('open_goals', {'after_at': after_at, 'after_id': after_id})
        result: list[Record] = []
        for row in rows:
            goal = self._records.unpack('goal', row)
            if row['signature'] != signature(goal):
                raise OwnerFailure('STORAGE_FAILED', 'storage', 'INTEGRITY_FAILURE')
            sources = await self._records.rows.read('source_children', {'goal_id': goal['goal_id']})
            if not sources or len(sources) != goal['source_count']:
                raise OwnerFailure('STORAGE_FAILED', 'storage', 'INTEGRITY_FAILURE')
            expired = goal['deadline'] is not None and now >= integer(goal['deadline'])
            waiting = goal['dedup_state'] in ('PENDING', 'RUNNING') and now >= integer(goal['created_at']) + integer(self.configuration.candidate.information.record('goals.lifecycle')['dedup_wait_timeout_ms']) * 1000
            result.append(MappingProxyType(dict(goal) | {'source_refs': tuple(self._records.unpack('source', s) for s in sources),
                'expired': expired, 'dedup_unresolved': waiting,
                'suggestion': 'CONSIDER_ABANDON_OR_CHANGE_DEADLINE' if expired else None,
                'semantic_review_available': self.daily_format, 'observed_at': now}))
        return tuple(result)

    async def has_open_after(self, last: Record) -> bool:
        """Exact presence of a later root; eight returned items alone prove no overflow."""
        rows = await self._records.rows.read('open_after', {'after_at': last['created_at'], 'after_id': last['goal_id']})
        return integer(rows[0]['remaining']) == 1

    async def list_page(self, now: int, cursor: str | None = None) -> Record:
        """Complete goals and source attribution, with an explicit finite next cursor.

        Cursor values are ordering hints, never authority. The host checks its
        read capability and mutation checkpoint around this bounded projection.
        """
        from companion_memory.information.payload_limits import bounded_items
        from companion_memory.persistence.schema import valid_identifier
        after_at, after_id = -(2**62), ''
        if cursor is not None:
            if type(cursor) is not str or len(cursor) > 160:
                raise OwnerFailure('INVALID_INPUT', 'goal', 'INVALID_SHAPE')
            raw_time, separator, after_id = cursor.partition(':')
            try: after_at = int(raw_time)
            except ValueError: raise OwnerFailure('INVALID_INPUT', 'goal', 'INVALID_SHAPE') from None
            if separator != ':' or raw_time != str(after_at) or not -(2**62) <= after_at < 2**62 or not valid_identifier(after_id):
                raise OwnerFailure('INVALID_INPUT', 'goal', 'INVALID_SHAPE')
        goals = await self.list_open(now, after_at, after_id)
        more = await self.has_open_after(goals[-1]) if goals else False
        metadata: dict[str, Value] = {'has_more': False, 'omitted_count': 2**63 - 1, 'observed_at': now, 'next_cursor': 'x' * 160}
        limit = integer(self.configuration.candidate.information.record('retrieval.reply')['goal_limit']) * 4096
        selected, omitted = bounded_items(goals, limit, metadata)
        if goals and not selected:
            raise OwnerFailure('INVALID_INPUT', 'goal', 'LIMIT_EXCEEDED')
        more = more or bool(omitted)
        next_cursor = str(selected[-1]['created_at']) + ':' + text(selected[-1]['goal_id']) if more else None
        return MappingProxyType(metadata | {'items': selected, 'has_more': more, 'omitted_count': None if more else 0, 'next_cursor': next_cursor})

    async def lookup(self, goal_id: str) -> Record | None:
        """Resolve one durable direct alias without applying an old revision."""
        value = await self._records.read('goal', {'goal_id': goal_id})
        if value is not None and value['canonical_id'] != goal_id:
            alias = await self._records.read('alias', {'alias_id': goal_id})
            if alias is None or alias['canonical_id'] != value['canonical_id']:
                raise OwnerFailure('STORAGE_FAILED', 'storage', 'INTEGRITY_FAILURE')
            value = await self._records.read('goal', {'goal_id': alias['canonical_id']})
            if value is None or value['canonical_id'] != value['goal_id']:
                raise OwnerFailure('STORAGE_FAILED', 'storage', 'INTEGRITY_FAILURE')
        return value

    async def pending_tasks(self) -> tuple[Record, ...]:
        return tuple(self._records.unpack('dedup_task', row) for row in await self._records.rows.read('pending_dedup', {}))

    async def running_tasks(self) -> tuple[Record, ...]:
        """A bounded recovery page; retained candidates and owner identity remain authoritative."""
        return tuple(self._records.unpack('dedup_task', row) for row in await self._records.rows.read('running_dedup', {}))

    async def task_decision(self, task_id: str) -> tuple[Record, str, Record | None]:
        """Bounded local decision from frozen candidates; no semantic model claim."""
        task = await self._records.read('dedup_task', {'task_id': task_id})
        if task is None: raise OwnerFailure('STORAGE_FAILED', 'goal', 'INTEGRITY_FAILURE')
        goal = await self.lookup(text(task['goal_id']))
        if goal is None or task['goal_revision'] != goal['revision'] or goal['canonical_id'] != task['goal_id']:
            return task, 'FAILED', None
        rows = await self._records.rows.read('dedup_candidate_children', {'task_id': task_id})
        if len(rows) != task['candidate_count']: raise OwnerFailure('STORAGE_FAILED', 'goal', 'INTEGRITY_FAILURE')
        exact: list[Record] = []; near = False
        for row in rows:
            candidate = self._records.unpack('dedup_candidate', row)
            current = await self.lookup(text(candidate['candidate_id']))
            if current is None or current['revision'] != candidate['revision']:
                return task, 'FAILED', None
            if current['status'] != 'OPEN': continue
            if signature(current) == signature(goal):
                if (integer(current['created_at']), text(current['goal_id'])) < (integer(goal['created_at']), text(goal['goal_id'])):
                    exact.append(current)
            else: near = True
        if exact:
            canonical = min(exact, key=lambda item: (integer(item['created_at']), text(item['goal_id'])))
            return task, 'MERGE', canonical
        return task, 'NEEDS_SEMANTIC_REVIEW' if near else 'DISTINCT', None

    async def due_plans(self, now: int) -> tuple[Record, ...]:
        return tuple(self._records.unpack('reminder_plan', row) for row in await self._records.rows.read('due_plans', {'now': now}))

    async def observation(self, now: int) -> Record:
        """Aggregate only; observation conveys no goal, source or route authority."""
        rows = await self._records.rows.read('observation', {'now': now})
        return MappingProxyType(dict(rows[0]) | {'observed_at': now, 'sink_mode': self.configuration.candidate.information.record('goals.delivery')['sink_mode'], 'semantic_review_available': self.daily_format})

    async def prepare_reminder(self, plan_id: str, revision: int, key: str, at_us: int) -> Record:
        """Prepare immutable metadata; the registration UoW verifies its digest."""
        plan = await self._records.read('reminder_plan', {'plan_id': plan_id})
        if plan is None or plan['revision'] != revision:
            raise OwnerFailure('PRECONDITION_FAILED', 'revision', 'REVISION_CONFLICT')
        goal = await self.lookup(text(plan['goal_id']))
        if goal is None: raise OwnerFailure('STORAGE_FAILED', 'goal', 'INTEGRITY_FAILURE')
        return reminder_intent(plan, goal, identity('delivery', plan_id, key), at_us)

    async def permit_reminder(self, intent: Record, route_id: str) -> bool:
        """Read actual cancellation and current goal revision before first send."""
        attempt = await self._records.read('attempt', {'delivery_id': intent['delivery_id']})
        if attempt is None or attempt['state'] != 'REGISTERED' or attempt['request_digest'] != sha256(encode_content(intent, 2048)).hexdigest(): return False
        plan = await self._records.read('reminder_plan', {'plan_id': attempt['plan_id']})
        if plan is None or plan['delivery_id'] != attempt['delivery_id'] or plan['route_id'] != route_id or plan['status'] != 'ATTEMPTING': return False
        goal = await self.lookup(text(plan['goal_id']))
        return goal is not None and goal['status'] == 'OPEN' and goal['goal_id'] == intent['canonical_goal_id'] and goal['revision'] == intent['revision'] and goal['deadline'] == intent['deadline']

    async def unresolved_attempts(self) -> tuple[Record, ...]:
        return tuple(self._records.unpack('attempt', row) for row in await self._records.rows.read('unresolved_attempts', {}))


def reminder_intent(plan: Record, goal: Record, delivery_id: str, at_us: int) -> Record:
    """The complete transport payload contains no goal text or executable action."""
    value = MappingProxyType({'delivery_id': delivery_id, 'canonical_goal_id': goal['canonical_id'], 'revision': goal['revision'],
        'kind': plan['kind'], 'deadline': plan['deadline'], 'observed_at': at_us,
        'suggestion': 'UPCOMING' if plan['kind'] == 'UPCOMING' else 'CONSIDER_ABANDON_OR_CHANGE_DEADLINE'})
    encode_content(value, 2048)
    return value
