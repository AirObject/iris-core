"""Explicit original-key fixed-material management, with no model side effects.

Callers retain each complete frozen envelope for resolve. The port never replaces
an expected revision or timestamp after an unconfirmed local commit.
"""
from collections.abc import Callable
from types import MappingProxyType
from companion_memory.persistence import ResultBoundCommandDefinition,ResultBoundCommand
from companion_memory.persistence.content_codec import encode_content
from companion_memory.persistence.deadlines import DeadlineScope
from companion_memory.persistence.semantic_records import Record,string,number,isolate
from .fixed_memory_commands import PAYLOADS
from companion_memory.ingress.events import plain
from companion_memory.persistence.schema import InvalidValue
from .fixed_memory import FixedMemorySets


class FixedMemoryPort:
    """Trusted assembly's restricted four-command management and confirmation port."""
    def __init__(self,owner: FixedMemorySets,definitions: tuple[ResultBoundCommandDefinition,...],checkpoint: Callable[[],None]):
        if type(owner) is not FixedMemorySets:raise ValueError('A native reviewed fixed-set owner is required.')
        self._owner=owner;self._definitions={d.operation_kind:d for d in definitions if d.operation_kind.startswith('fixed_')}
        if set(self._definitions)!={'fixed_begin','fixed_add_member','fixed_seal','fixed_establish'}:raise InvalidValue()
        self._checkpoint=checkpoint;self._closed=False

    def envelope(self,kind: str,key: str,payload: Record,observed_at: int) -> Record:
        """Freeze the exact input before the first transaction is submitted."""
        if self._closed or kind not in self._definitions:raise InvalidValue()
        payload=isolate(PAYLOADS[kind][0],payload,24576)
        expected=[]
        if kind!='fixed_begin':expected.append(MappingProxyType({'object_id':payload['set_id'],'revision':payload['expected_revision']}))
        if kind=='fixed_establish':
            from companion_memory.persistence.semantic_records import identity
            expected.append(MappingProxyType({'object_id':identity('fixed-member',payload['set_id'],payload['ordinal']),
                'revision':payload['expected_member_revision']}))
        return MappingProxyType({'binding_id':self._owner.review.claims['review_ref'],'request_key':key,
            'expected':tuple(expected),'observed_at':observed_at,'payload':encode_content(payload,24576).decode()})

    def _command(self,kind: str,envelope: Record) -> ResultBoundCommand:
        if self._closed or envelope['binding_id']!=self._owner.review.claims['review_ref']:raise InvalidValue()
        definition=self._definitions[kind]
        return ResultBoundCommand(definition.command_version,plain(envelope),
            {audit.event_slot:{'actor':self._owner.review.claims['reviewed_by']} for audit in definition.required_audits})

    async def _execute(self,kind: str,envelope: Record,deadline: float):
        with DeadlineScope(deadline):
            self._checkpoint()
            return await self._owner.storage.bind_operation(self._definitions[kind],self._owner.instance).execute(
                string(envelope['request_key']),self._command(kind,envelope))

    async def begin(self,envelope: Record,deadline: float):
        """Create only the reviewed set root; it does not establish memories."""
        return await self._execute('fixed_begin',envelope,deadline)

    async def add_member(self,envelope: Record,deadline: float):
        """Store one complete reviewed candidate member under its frozen key."""
        return await self._execute('fixed_add_member',envelope,deadline)

    async def seal(self,envelope: Record,deadline: float):
        """Seal only a complete manifest whose digest matches the review grant."""
        return await self._execute('fixed_seal',envelope,deadline)

    async def establish_one(self,envelope: Record,deadline: float):
        """Atomically establish one member, source and coverage gap."""
        return await self._execute('fixed_establish',envelope,deadline)

    async def resolve(self,kind: str,envelope: Record,deadline: float):
        """Read only the original receipt; no preparation, establishment or resend."""
        with DeadlineScope(deadline):
            port=self._owner.storage.bind_operation(self._definitions[kind],self._owner.instance)
            handle=port.recovery_handle(string(envelope['request_key']),self._command(kind,envelope))
            return await port.resolve_operation(handle)

    def close(self) -> None:
        """Revoke new invocations; underlying owners retain actual in-flight leases."""
        self._closed=True
