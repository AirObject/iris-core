"""Native closed mutation discriminator for the registered text ledger only.

The static signature names this fixed policy. Preflight validates every bounded
body before freezing a command; only one handoff may carry a payload. The policy
grants no larger storage limit and cannot be copied to another definition.
"""
from __future__ import annotations
from dataclasses import dataclass
from collections.abc import Sequence
from types import MappingProxyType
from typing import TYPE_CHECKING, cast
from companion_memory.persistence.schema import InvalidValue, Value
from .values import InvalidData, load
from .text_stored_schema import validate
if TYPE_CHECKING:
    from companion_memory.persistence.definitions import CommandSpec
    from companion_memory.persistence.results import Receipt
    from companion_memory.logging_service.audit_records import AuditRecord

_ISSUER = object()


@dataclass(frozen=True, slots=True, init=False)
class TextMutationPolicy:
    """Opaque exact-assembly authority for a closed command input format."""
    owner: object
    _issuer: object

    def __init__(self):
        raise TypeError('Only the native text ledger issues its mutation policy.')


def issue(owner: object) -> TextMutationPolicy:
    from .ledger import LedgerAssembly
    if type(owner) is not LedgerAssembly or not (owner.text_generation or owner.embedding_format):
        raise InvalidValue()
    policy = object.__new__(TextMutationPolicy)
    object.__setattr__(policy, 'owner', owner)
    object.__setattr__(policy, '_issuer', _ISSUER)
    return policy


def declared(definition: CommandSpec) -> bool:
    """Reject a copied, renamed or fabricated policy before signature creation."""
    from companion_memory.persistence.definitions import CommandDefinition
    from .ledger import LedgerAssembly
    if type(definition) is not CommandDefinition or definition.input_policy is None:
        return False
    policy = definition.input_policy
    if (type(policy) is not TextMutationPolicy or getattr(policy, '_issuer', None) is not _ISSUER
            or type(getattr(policy, 'owner', None)) is not LedgerAssembly):
        raise InvalidValue()
    owner = cast(LedgerAssembly, policy.owner)
    if not (owner.text_generation or owner.embedding_format) or not any(command is definition for command in owner.commands):
        raise InvalidValue()
    return True


def validate_values(definition: CommandSpec, values: MappingProxyType[str, Value]) -> None:
    """Validate all changes before storage admits a transaction or hashes input."""
    if not declared(definition):
        return
    changes = cast(tuple[MappingProxyType[str, Value], ...], values['changes'])
    handoffs = 0
    try:
        for change in changes:
            table = cast(str, change['table'])
            body = load(change['body'])
            from .ledger import LedgerAssembly
            owner=cast(LedgerAssembly,cast(TextMutationPolicy,definition.input_policy).owner)
            if owner.daily_format:
                from .daily_stored_schema import validate as validate_daily
                validate_daily(table,body)
            elif owner.embedding_format:
                from .embedding_stored_schema import validate as validate_embedding
                if owner.embedding_usage_only:validate_embedding(table,body,simulated=False,usage_only=True)
                else:
                    try:validate_embedding(table,body,simulated=False)
                    except InvalidValue:validate_embedding(table,body,simulated=True)
            else:validate(table, body)
            if body['object_id'] != change['object_id']:
                raise InvalidData()
            if table == 'handoffs':
                handoffs += 1
                if type(change['payload']) is not str:
                    raise InvalidData()
            elif change['payload'] is not None:
                raise InvalidData()
        if handoffs > 1:
            raise InvalidData()
    except (InvalidData, ValueError, TypeError, KeyError):
        raise InvalidValue() from None


def validate_receipt_targets(definition: CommandSpec, receipt: Receipt, audits: Sequence[AuditRecord]) -> None:
    """Bind a text ledger's original result to its required audited root.

    The ledger returns the first actual mutation root. Confirmation must verify
    that root against the retained audit, even when the receipt is read without
    the original command body. No audit content leaves the storage owner.
    """
    if not declared(definition):
        return
    if len(audits) != 1 or audits[0].event_slot != 'provider_change':
        raise InvalidValue()
    result = receipt.result
    audit = audits[0]
    targets = audit.target_refs
    change = audit.change
    if type(result) is not MappingProxyType or type(targets) is not tuple or not targets or type(change) is not MappingProxyType:
        raise InvalidValue()
    target = targets[0]
    if type(target) is not MappingProxyType:
        raise InvalidValue()
    if (result['object_id'] != target['object_id'] or result['revision'] != target['revision']
            or change['revision'] != target['revision']
            or change['previous_revision'] != target['previous_revision']):
        raise InvalidValue()
