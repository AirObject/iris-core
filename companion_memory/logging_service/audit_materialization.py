"""Static result-to-audit projections and self-contained historical evidence.

Only safe typed intentions are retained. Historical validation uses the original
receipt result, never mutable business rows, and leaves legacy manifests intact.
"""
from dataclasses import fields
import hashlib
from types import MappingProxyType
from typing import cast

from companion_memory.persistence.definitions import ResultBoundCommandDefinition
from companion_memory.persistence.schema import (
    Field, InvalidValue, RecordSchema, ScalarSchema, SequenceSchema, Value,
    encode_value, freeze_value,
)
from .audit_records import AuditRequirement, audit_event_schema, audit_manifest, freeze_audit_event


def binding_value(definition: ResultBoundCommandDefinition) -> Value:
    """Canonical safe description of all immutable projection declarations."""
    return tuple(MappingProxyType({
        'slot': b.event_slot, 'version': b.version,
        'fields': tuple(MappingProxyType({f.name: getattr(item, f.name) for f in fields(item)})
                        for item in sorted(b.fields, key=lambda item: item.field)),
    }) for b in sorted(definition.audit_bindings, key=lambda item: item.event_slot))


def _path_schema(schema: RecordSchema, path: tuple[str, ...]) -> Field:
    if type(path) is not tuple or not path:
        raise InvalidValue()
    current = Field('', schema)
    for key in path:
        if type(key) is not str or type(current.schema) is not RecordSchema or current.optional or current.nullable:
            raise InvalidValue()
        matches = [f for f in current.schema.fields if f.name == key]
        if len(matches) != 1:
            raise InvalidValue()
        current = matches[0]
    return current


def _fits(source: Field, target: Field) -> bool:
    if source.optional or (source.nullable and not target.nullable):
        return False
    a, b = source.schema, target.schema
    if type(a) is not type(b):
        return False
    if type(a) is ScalarSchema and type(b) is ScalarSchema:
        return a.kind == b.kind and (a.kind != 'integer' or (a.minimum >= b.minimum and a.maximum <= b.maximum)) and (a.kind != 'enum' or set(a.choices) <= set(b.choices))
    if type(a) is SequenceSchema and type(b) is SequenceSchema:
        return a.minimum >= b.minimum and a.maximum <= b.maximum and _fits(Field('', a.item), Field('', b.item))
    if type(a) is RecordSchema and type(b) is RecordSchema:
        aa, bb = {f.name: f for f in a.fields}, {f.name: f for f in b.fields}
        return set(aa) <= set(bb) and all(f.optional or f.name in aa for f in b.fields) and all(_fits(f, bb[k]) for k, f in aa.items())
    return False


def validate_bindings(definition: ResultBoundCommandDefinition) -> None:
    """Reject incomplete, unsafe or incompatible trusted declarations at assembly."""
    # Reuse audit's recursive safety validator to prohibit arbitrary text intentions.
    AuditRequirement('validation', 'validation', 'VALIDATION', 1, ('VALIDATION',), definition.audit_intent_schema)
    required = {r.event_slot: r for r in definition.required_audits}
    if (type(definition.audit_bindings) is not tuple or len(definition.audit_bindings) != len(required)
            or {b.event_slot for b in definition.audit_bindings} != set(required)):
        raise InvalidValue()
    for binding in definition.audit_bindings:
        if type(binding.version) is not int or binding.version != 1:
            raise InvalidValue()
        targets = {f.name: f for f in audit_event_schema(required[binding.event_slot]).fields if f.name != 'event_version'}
        if len(binding.fields) != len(targets) or {f.field for f in binding.fields} != set(targets):
            raise InvalidValue()
        for f in binding.fields:
            target = targets[f.field]
            if f.source == 'CONSTANT':
                if f.path:
                    raise InvalidValue()
                freeze_value(target.schema, f.constant, owned=True)
            elif f.source in ('INTENT', 'RESULT'):
                if f.constant is not None or not _fits(_path_schema(definition.audit_intent_schema if f.source == 'INTENT' else definition.result_schema, f.path), target):
                    raise InvalidValue()
            else:
                raise InvalidValue()
    encode_value(binding_value(definition), 65536)


def freeze_intents(definition: ResultBoundCommandDefinition, source: object, *, owned: bool = False) -> MappingProxyType[str, Value]:
    """Own the exact per-slot intention set without running submitted hooks."""
    schema = RecordSchema(tuple(Field(r.event_slot, definition.audit_intent_schema) for r in definition.required_audits))
    return cast(MappingProxyType[str, Value], freeze_value(schema, source, owned=owned))


def materialize_events(definition: ResultBoundCommandDefinition, intentions: MappingProxyType[str, Value], result: Value) -> MappingProxyType[str, Value]:
    """Project safe event bodies from one already frozen result and stable intent."""
    requirements = {r.event_slot: r for r in definition.required_audits}
    events: dict[str, Value] = {}
    for binding in definition.audit_bindings:
        values: dict[str, Value] = {'event_version': requirements[binding.event_slot].event_version}
        for f in binding.fields:
            value = f.constant if f.source == 'CONSTANT' else intentions[binding.event_slot] if f.source == 'INTENT' else result
            for key in f.path:
                if type(value) is not MappingProxyType or key not in value:
                    raise InvalidValue()
                value = value[key]
            values[f.field] = value
        events[binding.event_slot] = freeze_audit_event(requirements[binding.event_slot], values, owned=True)
    return MappingProxyType(events)


def evidence_value(definition: ResultBoundCommandDefinition, intentions: MappingProxyType[str, Value], fingerprint: str, commit_id: str, descriptor_digest: str) -> Value:
    """Create bounded linked evidence, without storing business command values."""
    payload: dict[str, Value] = {
        'materialization_version': 1, 'required': audit_manifest(definition.required_audits),
        'descriptor_digest': descriptor_digest, 'intentions': intentions,
        'fingerprint_version': 2, 'fingerprint': fingerprint, 'commit_id': commit_id,
    }
    payload['evidence_digest'] = hashlib.sha256(encode_value(MappingProxyType(payload), 65536)).hexdigest()
    return MappingProxyType(payload)
