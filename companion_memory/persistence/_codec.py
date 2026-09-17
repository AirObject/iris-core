"""Durable identity/receipt encodings and static assembly signatures.

Command content fingerprints cover typed input, participants, schema semantics
and complete mandatory audit intentions. Receipts keep original results only;
command inputs are never persisted into the general receipt table.
"""

from dataclasses import fields
from datetime import datetime
import hashlib
from types import MappingProxyType
from typing import Literal, cast

from companion_memory.logging_service.audit_records import AuditRecord, audit_manifest, freeze_audit_event
from .definitions import CommandSpec, CommandDefinition, LocalCommand, RepositoryDefinition, ResultBoundCommand, ResultBoundCommandDefinition, TableDefinition
from .results import OperationIdentity, Receipt, RecoveryHandle
from .schema import (
    BoundedTextSchema, InvalidValue, RecordSchema, ScalarSchema, SequenceSchema, Value,
    decode_value, encode_value, freeze_value, utc_text, valid_identifier,
)


def identity_value(identity: OperationIdentity) -> MappingProxyType[str, Value]:
    return MappingProxyType({field.name: getattr(identity, field.name) for field in fields(identity)})


def valid_identity(identity: object) -> bool:
    return type(identity) is OperationIdentity and all(valid_identifier(getattr(identity, field.name)) for field in fields(identity))


def valid_utc(value: object) -> bool:
    if type(value) is not str or len(value) != 27 or not value.endswith("Z"):
        return False
    try:
        return utc_text(datetime.fromisoformat(value)) == value
    except (ValueError, InvalidValue):
        return False


def schema_value(schema: ScalarSchema | BoundedTextSchema | RecordSchema | SequenceSchema) -> Value:
    """A finite trusted schema is part of the command's durable interpretation."""
    if type(schema) is BoundedTextSchema:
        return MappingProxyType({"bounded_text": schema.max_utf8_bytes})
    if type(schema) is ScalarSchema:
        return MappingProxyType({"kind": schema.kind, "minimum": schema.minimum,
                                 "maximum": schema.maximum, "choices": schema.choices})
    if type(schema) is SequenceSchema:
        return MappingProxyType({"sequence": schema_value(schema.item), "minimum": schema.minimum, "maximum": schema.maximum})
    assert type(schema) is RecordSchema
    return tuple(MappingProxyType({"field": field.name, "schema": schema_value(field.schema),
                                  "nullable": field.nullable, "optional": field.optional}) for field in schema.fields)


def command_descriptor(definition: CommandSpec) -> Value:
    result: dict[str, Value] = {
        "owner": definition.owner_namespace, "kind": definition.operation_kind,
        "version": definition.command_version, "input": schema_value(definition.input_schema),
        "result_version": definition.result_schema_version, "result": schema_value(definition.result_schema),
        "participants": tuple(sorted(item.owner_module for item in definition.participants)),
        "audit_manifest": audit_manifest(definition.required_audits),
        "audit_schemas": tuple(MappingProxyType({"slot": item.event_slot, "change": schema_value(item.change_schema),
                                                "reasons": item.reason_codes, "targets": item.target_limit})
                               for item in sorted(definition.required_audits, key=lambda item: item.event_slot)),
    }
    if type(definition) is ResultBoundCommandDefinition:
        from companion_memory.logging_service.audit_materialization import binding_value
        result.update({"intent_schema": schema_value(definition.audit_intent_schema), "audit_bindings": binding_value(definition), "fingerprint_version": 2})
    from .command_capacity import declared_capacity
    capacity = declared_capacity(definition)
    if capacity is not None:
        result['frozen_carrier_policy'] = MappingProxyType({'kind': 'DREAM_CONFIGURATION_INITIALIZATION' if definition.operation_kind=='initialize_dream_configuration' else 'DAILY_CONFIGURATION_INITIALIZATION' if definition.operation_kind=='initialize_daily_configuration' else 'SEMANTIC_CONFIGURATION_INITIALIZATION' if definition.operation_kind=='initialize_semantic' else 'TEXT_CONFIGURATION_INITIALIZATION', 'max_bytes': capacity})
    from companion_memory.provider.text_command_policy import declared
    if declared(definition):
        result['input_policy'] = 'TEXT_PROVIDER_MUTATIONS_V3'
    from .semantic_commands import declared as semantic_declared,descriptor as semantic_descriptor
    if semantic_declared(definition):
        return semantic_descriptor(definition,MappingProxyType(result))
    return MappingProxyType(result)


type AssemblyFormat = Literal['LEGACY', 'LOCAL_INFORMATION_V1', 'MODEL_TEXT_LEARNING_V1', 'ASYNC_SEMANTIC_V1', 'DAILY_COGNITION_V1', 'DREAM_MAINTENANCE_V1']


def table_descriptor(table: TableDefinition) -> Value:
    """Include complete new body schemas while preserving old table bytes."""
    fields: dict[str,Value]={'name':table.name,'sql':table.sql.strip().rstrip(';')}
    if table.record_schemas:
        fields['record_schemas']=tuple(schema_value(schema) for schema in table.record_schemas)
    return MappingProxyType(fields)


def assembly_value(repositories: tuple[RepositoryDefinition, ...], commands: tuple[CommandSpec, ...],
                   *, assembly_format: AssemblyFormat = 'LEGACY') -> bytes:
    """Encode an explicitly selected static format without changing command limits.

    Legacy bytes remain identical. The information format carries its own marker
    and enforces independent descriptor, repository and enclosing byte budgets.
    Readers compare this complete canonical value when opening an existing file.
    """
    if type(assembly_format) is not str or assembly_format not in ('LEGACY', 'LOCAL_INFORMATION_V1', 'MODEL_TEXT_LEARNING_V1', 'ASYNC_SEMANTIC_V1', 'DAILY_COGNITION_V1','DREAM_MAINTENANCE_V1'):
        raise InvalidValue()
    from .command_capacity import declared_capacity
    policies = sum(declared_capacity(command) is not None for command in commands)
    from companion_memory.provider.text_command_policy import declared
    from .semantic_commands import declared as semantic_declared
    if assembly_format not in ('ASYNC_SEMANTIC_V1','DAILY_COGNITION_V1','DREAM_MAINTENANCE_V1') and any(semantic_declared(command) for command in commands):
        raise InvalidValue()
    if assembly_format not in ('MODEL_TEXT_LEARNING_V1','ASYNC_SEMANTIC_V1', 'DAILY_COGNITION_V1','DREAM_MAINTENANCE_V1') and any(declared(command) for command in commands):
        raise InvalidValue()
    if policies != (1 if assembly_format in ('MODEL_TEXT_LEARNING_V1','ASYNC_SEMANTIC_V1', 'DAILY_COGNITION_V1','DREAM_MAINTENANCE_V1') else 0):
        raise InvalidValue()
    expected_initialization={'MODEL_TEXT_LEARNING_V1':'initialize_text_learning',
        'ASYNC_SEMANTIC_V1':'initialize_semantic','DAILY_COGNITION_V1':'initialize_daily_configuration','DREAM_MAINTENANCE_V1':'initialize_dream_configuration'}.get(assembly_format)
    if any(declared_capacity(c) is not None and c.operation_kind!=expected_initialization for c in commands):
        raise InvalidValue()
    contents: dict[str, Value] = {
        "repositories": tuple(MappingProxyType({"owner": item.owner_module, "version": item.schema_version,
                                               "tables": tuple(table_descriptor(table) for table in item.tables)})
                              for item in sorted(repositories, key=lambda item: item.owner_module)),
        "commands": tuple(command_descriptor(item) for item in sorted(commands, key=lambda item: (item.owner_namespace, item.operation_kind))),
    }
    if assembly_format == 'LEGACY':
        return encode_value(MappingProxyType(contents), 1048576)
    contents['static_format'] = assembly_format
    if assembly_format in ('ASYNC_SEMANTIC_V1','DAILY_COGNITION_V1','DREAM_MAINTENANCE_V1'):
        for repository in repositories:
            for table in repository.tables:
                if table.record_schemas:encode_value(table_descriptor(table),8388608 if assembly_format in ('DAILY_COGNITION_V1','DREAM_MAINTENANCE_V1') else 32768)
        return encode_value(MappingProxyType(contents),8388608 if assembly_format in ('DAILY_COGNITION_V1','DREAM_MAINTENANCE_V1') else 4194304)
    encoded = encode_value(MappingProxyType(contents), 3145728)
    descriptors = sum(len(encode_value(command_descriptor(command), 1048576)) for command in commands)
    repository_size = len(encode_value(contents['repositories'], 131072))
    envelope = len(encoded) - descriptors - repository_size
    if descriptors > 2621440 or envelope > 8192 or len(encoded) > 2760704:
        raise InvalidValue()
    return encoded


def valid_assembly_encoding(data: object, assembly_format: AssemblyFormat) -> bool:
    """Validate the chosen bounded canonical carrier before identity comparison."""
    if type(data) is not bytes:
        return False
    limit = 8388608 if assembly_format in ('DAILY_COGNITION_V1','DREAM_MAINTENANCE_V1') else 4194304 if assembly_format=='ASYNC_SEMANTIC_V1' else 3145728 if assembly_format in ('LOCAL_INFORMATION_V1', 'MODEL_TEXT_LEARNING_V1') else 1048576
    try:
        value = decode_value(data, limit)
        keys = {'repositories', 'commands'}
        if assembly_format in ('LOCAL_INFORMATION_V1', 'MODEL_TEXT_LEARNING_V1','ASYNC_SEMANTIC_V1', 'DAILY_COGNITION_V1','DREAM_MAINTENANCE_V1'):
            keys.add('static_format')
        if type(value) is not dict or set(value) != keys:
            return False
        if assembly_format in ('LOCAL_INFORMATION_V1', 'MODEL_TEXT_LEARNING_V1','ASYNC_SEMANTIC_V1', 'DAILY_COGNITION_V1','DREAM_MAINTENANCE_V1') and value['static_format'] != assembly_format:
            return False
        # The stored blob is compared with the trusted canonical declaration by
        # the caller; decoding here additionally bounds and rejects malformed JSON.
        return True
    except (InvalidValue, ValueError, TypeError):
        return False


def prepare_command(definition: CommandSpec, identity: OperationIdentity, command: LocalCommand | ResultBoundCommand, limit: int) -> tuple[RecoveryHandle, MappingProxyType[str, Value], MappingProxyType[str, Value]]:
    from .command_capacity import command_capacity
    limit = command_capacity(definition, limit)
    values = cast(MappingProxyType[str, Value], freeze_value(definition.input_schema, command.values))
    from .command_capacity import validate_command_values
    try:
        validate_command_values(definition, values)
        from companion_memory.provider.text_command_policy import validate_values
        validate_values(definition, values)
        from .semantic_commands import validate_values as validate_semantic_values
        validate_semantic_values(definition,values)
    except (ValueError, TypeError, UnicodeError):
        raise InvalidValue() from None
    if type(definition) is ResultBoundCommandDefinition:
        from companion_memory.logging_service.audit_materialization import freeze_intents
        if type(command) is not ResultBoundCommand:
            raise InvalidValue()
        intentions = freeze_intents(definition, command.audit_intents)
        encoded = encode_value(MappingProxyType({"definition": command_descriptor(definition), "values": values, "intentions": intentions}), limit)
        return RecoveryHandle(identity, definition.command_version, 2, hashlib.sha256(encoded).hexdigest()), values, intentions
    if type(command) is not LocalCommand:
        raise InvalidValue()
    events = command.audit_events
    if type(events) is not dict or any(type(key) is not str for key in events):
        raise InvalidValue()
    if set(events) != {item.event_slot for item in definition.required_audits}:
        raise InvalidValue()
    owned_events = MappingProxyType({item.event_slot: freeze_audit_event(item, events[item.event_slot]) for item in definition.required_audits})
    encoded = encode_value(MappingProxyType({"definition": command_descriptor(definition), "values": values, "audits": owned_events}), limit)
    return RecoveryHandle(identity, definition.command_version, 1, hashlib.sha256(encoded).hexdigest()), values, cast(MappingProxyType[str, Value], owned_events)


def receipt_value(receipt: Receipt) -> Value:
    return MappingProxyType({
        "schema_version": receipt.schema_version, "identity": identity_value(receipt.identity),
        "command_version": receipt.command_version, "fingerprint_version": receipt.fingerprint_version,
        "fingerprint": receipt.fingerprint, "commit_id": receipt.commit_id, "recorded_at": receipt.recorded_at,
        "result_schema_version": receipt.result_schema_version, "result": receipt.result,
    })


def decode_receipt(data: bytes, definition: CommandSpec) -> Receipt:
    raw = decode_value(data, 65536)
    if type(raw) is not dict or set(raw) != {field.name for field in fields(Receipt)}:
        raise InvalidValue()
    identity = raw["identity"]
    if type(identity) is not dict or set(identity) != {field.name for field in fields(OperationIdentity)}:
        raise InvalidValue()
    identity = OperationIdentity(**identity)
    if (not valid_identity(identity) or type(raw["schema_version"]) is not int or raw["schema_version"] != 1
            or type(raw["fingerprint_version"]) is not int or raw["fingerprint_version"] != (2 if type(definition) is ResultBoundCommandDefinition else 1)
            or type(raw["command_version"]) is not int or raw["command_version"] != definition.command_version
            or type(raw["result_schema_version"]) is not int or raw["result_schema_version"] != definition.result_schema_version
            or type(raw["fingerprint"]) is not str or len(raw["fingerprint"]) != 64
            or any(character not in "0123456789abcdef" for character in raw["fingerprint"])
            or not valid_identifier(raw["commit_id"]) or not valid_utc(raw["recorded_at"])):
        raise InvalidValue()
    result = freeze_value(definition.result_schema, raw["result"])
    return Receipt(1, identity, raw["command_version"], raw["fingerprint_version"], raw["fingerprint"], raw["commit_id"],
                   raw["recorded_at"], raw["result_schema_version"], result)


def decode_audit(data: bytes) -> AuditRecord:
    raw = decode_value(data, 65536)
    if type(raw) is not dict or set(raw) != {field.name for field in fields(AuditRecord)}:
        raise InvalidValue()
    identity = raw["operation_identity"]
    if type(identity) is not dict or set(identity) != {field.name for field in fields(OperationIdentity)}:
        raise InvalidValue()
    raw["operation_identity"] = OperationIdentity(**identity)
    if not valid_utc(raw["recorded_at"]):
        raise InvalidValue()
    # Full target/change ownership follows the module's event schema at read time.
    return AuditRecord(**raw)
