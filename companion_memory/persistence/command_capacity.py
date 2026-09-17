"""Native configuration initialization capacity bound to one trusted publisher.

A name, numeric limit or copied command cannot grant this policy. Its issuer and
exact registered definition remain bound together. Ordinary command descriptors
omit the policy entirely, preserving their existing bytes and fingerprints.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast
from .schema import InvalidValue
if TYPE_CHECKING:
    from .definitions import CommandSpec

_ISSUER = object()


@dataclass(frozen=True, slots=True, init=False)
class ConfigurationInitializationCapacity:
    """Opaque authority for the native text configuration publisher only."""
    _owner: object
    _issuer: object

    def __init__(self) -> None:
        raise TypeError('A trusted configuration publisher must issue the policy.')


def _issue_text_configuration_capacity(owner: object) -> ConfigurationInitializationCapacity:
    from companion_memory.configuration.text_persistence import TextConfigurationAssembly
    if type(owner) is not TextConfigurationAssembly:
        raise InvalidValue()
    result = object.__new__(ConfigurationInitializationCapacity)
    object.__setattr__(result, '_owner', owner)
    object.__setattr__(result, '_issuer', _ISSUER)
    return result


def _issue_semantic_configuration_capacity(owner: object) -> ConfigurationInitializationCapacity:
    from companion_memory.configuration.semantic_persistence import SemanticConfigurationAssembly
    if type(owner) is not SemanticConfigurationAssembly:
        raise InvalidValue()
    result=object.__new__(ConfigurationInitializationCapacity)
    object.__setattr__(result,'_owner',owner);object.__setattr__(result,'_issuer',_ISSUER)
    return result


def _issue_daily_configuration_capacity(owner: object) -> ConfigurationInitializationCapacity:
    from companion_memory.configuration.daily_persistence import DailyConfigurationAssembly
    if type(owner) is not DailyConfigurationAssembly:raise InvalidValue()
    result=object.__new__(ConfigurationInitializationCapacity)
    object.__setattr__(result,'_owner',owner);object.__setattr__(result,'_issuer',_ISSUER)
    return result


def _issue_dream_configuration_capacity(owner: object) -> ConfigurationInitializationCapacity:
    """Issue the new publisher's exception without changing older owner identity."""
    from companion_memory.configuration.dream_persistence import DreamConfigurationAssembly
    if type(owner) is not DreamConfigurationAssembly:
        raise InvalidValue()
    result=object.__new__(ConfigurationInitializationCapacity)
    object.__setattr__(result,'_owner',owner)
    object.__setattr__(result,'_issuer',_ISSUER)
    return result


def _issue_managed_configuration_capacity(owner: object) -> ConfigurationInitializationCapacity:
    """Issue only the managed publisher's initial complete configuration carrier."""
    from companion_memory.configuration.managed_persistence import ManagedConfigurationAssembly
    if type(owner) is not ManagedConfigurationAssembly:
        raise InvalidValue()
    result=object.__new__(ConfigurationInitializationCapacity)
    object.__setattr__(result, '_owner', owner)
    object.__setattr__(result, '_issuer', _ISSUER)
    return result


def declared_capacity(definition: CommandSpec) -> int | None:
    """Validate native identity before declaring or applying the fixed exception."""
    from .definitions import ResultBoundCommandDefinition
    if type(definition) is not ResultBoundCommandDefinition or definition.capacity_policy is None:
        return None
    policy = definition.capacity_policy
    from companion_memory.configuration.text_persistence import TextConfigurationAssembly
    from companion_memory.configuration.semantic_persistence import SemanticConfigurationAssembly
    from companion_memory.configuration.daily_persistence import DailyConfigurationAssembly
    from companion_memory.configuration.dream_persistence import DreamConfigurationAssembly
    from companion_memory.configuration.managed_persistence import ManagedConfigurationAssembly
    if (type(policy) is not ConfigurationInitializationCapacity or getattr(policy, '_issuer', None) is not _ISSUER
            or type(getattr(policy, '_owner', None)) not in (TextConfigurationAssembly,SemanticConfigurationAssembly,DailyConfigurationAssembly,DreamConfigurationAssembly,ManagedConfigurationAssembly)):
        raise InvalidValue()
    if type(policy._owner) is ManagedConfigurationAssembly:
        managed = policy._owner
        if (managed.commands != (definition,) or managed.commands[0] is not definition
                or definition.handler != managed._handle or definition.owner_namespace != 'configuration'
                or definition.operation_kind != 'initialize_managed_configuration'
                or definition.participants != (managed.repository.definition,)
                or managed.repository.definition.schema_version != 8):
            raise InvalidValue()
        return 2097152
    owner = cast(TextConfigurationAssembly | SemanticConfigurationAssembly | DailyConfigurationAssembly | DreamConfigurationAssembly, policy._owner)
    if type(owner) is DreamConfigurationAssembly:
        if (owner.commands != (definition,) or owner.commands[0] is not definition
                or definition.handler != owner._handle or definition.owner_namespace != 'configuration'
                or definition.operation_kind != 'initialize_dream_configuration'
                or definition.participants != (owner.repository.definition,)
                or owner.repository.definition.schema_version != 7):
            raise InvalidValue()
        return 2097152
    daily=type(owner) is DailyConfigurationAssembly
    semantic=type(owner) is SemanticConfigurationAssembly
    if (owner.commands != (definition,) or owner.commands[0] is not definition
            or definition.handler != owner._handle
            or definition.owner_namespace != 'configuration' or definition.operation_kind != ('initialize_daily_configuration' if daily else 'initialize_semantic' if semantic else 'initialize_text_learning')
            or definition.participants != ((owner.repository.definition,) if daily else owner.repositories) or owner.repository.definition.schema_version != (6 if daily else 5 if semantic else 4)):
        raise InvalidValue()
    return 2097152


def command_capacity(definition: CommandSpec, ordinary_limit: int) -> int:
    """Return the single policy shared by preflight, fingerprint and execution."""
    if type(ordinary_limit) is not int or ordinary_limit < 1:
        raise InvalidValue()
    return declared_capacity(definition) or min(ordinary_limit,1048576)


def validate_command_values(definition: CommandSpec, values) -> None:
    """Enforce the independent aggregate body limit before transaction admission."""
    if declared_capacity(definition) is None:
        return
    from companion_memory.configuration.text_codec import CONFIGURATION_BODY_LIMIT as TEXT_BODY_LIMIT
    from companion_memory.configuration.semantic_codec import CONFIGURATION_BODY_LIMIT as SEMANTIC_BODY_LIMIT
    from companion_memory.configuration.semantic_schema import semantic_definitions
    daily=definition.operation_kind=='initialize_daily_configuration'
    dream=definition.operation_kind in ('initialize_dream_configuration','initialize_managed_configuration')
    semantic=definition.operation_kind=='initialize_semantic'
    limit=SEMANTIC_BODY_LIMIT if semantic else TEXT_BODY_LIMIT
    added={d['key'] for d in semantic_definitions()} if semantic else set()
    from companion_memory.configuration.content_codec import decode_content_entry, entries_digest
    domains=values['domains']
    if len(domains)!=6 or len({d['domain_id'] for d in domains})!=6:
        raise InvalidValue()
    count=total=inherited=0
    for domain in domains:
        entries=domain['entries'];count+=len(entries)
        if len({e['parameter_key'] for e in entries})!=len(entries):raise InvalidValue()
        for entry in entries:
            declaration,_,_,_=decode_content_entry(entry['body'])
            if declaration['key']!=entry['parameter_key']:raise InvalidValue()
            total+=len(entry['body'].encode('utf-8'))
            if entry['parameter_key'] not in added: inherited+=len(entry['body'].encode('utf-8'))
        if entries_digest(tuple((e['parameter_key'],e['body']) for e in entries))!=domain['digest']:raise InvalidValue()
    if count!=(136 if dream else 130 if daily else 124 if semantic else 118):raise InvalidValue()
    if total>limit or inherited>TEXT_BODY_LIMIT:
        from .schema import ValueTooLarge
        raise ValueTooLarge()
