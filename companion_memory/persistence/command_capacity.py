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


def declared_capacity(definition: CommandSpec) -> int | None:
    """Validate native identity before declaring or applying the fixed exception."""
    from .definitions import ResultBoundCommandDefinition
    if type(definition) is not ResultBoundCommandDefinition or definition.capacity_policy is None:
        return None
    policy = definition.capacity_policy
    from companion_memory.configuration.text_persistence import TextConfigurationAssembly
    from companion_memory.configuration.semantic_persistence import SemanticConfigurationAssembly
    if (type(policy) is not ConfigurationInitializationCapacity or getattr(policy, '_issuer', None) is not _ISSUER
            or type(getattr(policy, '_owner', None)) not in (TextConfigurationAssembly,SemanticConfigurationAssembly)):
        raise InvalidValue()
    owner = cast(TextConfigurationAssembly | SemanticConfigurationAssembly, policy._owner)
    semantic=type(owner) is SemanticConfigurationAssembly
    if (owner.commands != (definition,) or owner.commands[0] is not definition
            or definition.handler != owner._handle
            or definition.owner_namespace != 'configuration' or definition.operation_kind != ('initialize_semantic' if semantic else 'initialize_text_learning')
            or definition.participants != owner.repositories or owner.repository.definition.schema_version != (5 if semantic else 4)):
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
    if count!=(124 if semantic else 118):raise InvalidValue()
    if total>limit or inherited>TEXT_BODY_LIMIT:
        from .schema import ValueTooLarge
        raise ValueTooLarge()
