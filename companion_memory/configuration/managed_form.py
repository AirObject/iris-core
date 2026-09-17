"""Finite form descriptions derived from the same native value validators.

The scaffold is an incomplete draft, not a resolved candidate or supplier
attestation. Only declared defaults, fixed protocol values and trusted resource
bindings are filled. Operator choices remain empty until explicitly provided.
"""
from __future__ import annotations
from types import MappingProxyType
from typing import Any
from companion_memory.persistence.schema import ScalarSchema, BoundedTextSchema, RecordSchema, SequenceSchema
from .definitions import Declared, LiteralDefault, Bound
from .managed_schema import MATERIAL_VALUES
from .dream_schema import GENERATION_PROFILE, ROLE_PROFILES, ROLES
from .daily_schema import ACCOUNT, IMAGE_PROFILE, EMBEDDING_PROFILE
from .information_schema import SUPPORTED_VALUES
from .snapshots import PresentValue


def plain(value):
    if type(value) in (dict, MappingProxyType):
        return {key: plain(item) for key, item in value.items()}
    if type(value) in (tuple, list):
        return [plain(item) for item in value]
    return value


def shape(schema) -> dict[str, Any]:
    """Export only closed data; no validators, executable expressions or paths."""
    if type(schema) is RecordSchema:
        return {'type': 'object', 'fields': [{'name': field.name, 'nullable': field.nullable,
            'optional': field.optional, 'schema': shape(field.schema)} for field in schema.fields]}
    if type(schema) is SequenceSchema:
        return {'type': 'array', 'minimum': schema.minimum, 'maximum': schema.maximum, 'item': shape(schema.item)}
    if type(schema) is BoundedTextSchema:
        return {'type': 'string', 'max_utf8_bytes': schema.max_utf8_bytes}
    if type(schema) is ScalarSchema:
        if schema.kind == 'enum':
            return {'type': 'string', 'choices': list(schema.choices)}
        if schema.kind == 'integer':
            return {'type': 'integer', 'minimum': schema.minimum, 'maximum': schema.maximum}
        return {'type': 'boolean'} if schema.kind == 'boolean' else {'type': 'string', 'max_utf8_bytes': 128}
    raise ValueError('No native form declaration.')


def fixed(value):
    if type(value) is dict:
        return {'type': 'object', 'fields': [{'name': key, 'nullable': item is None,
            'schema': fixed(item)} for key, item in value.items()]}
    if type(value) is list:
        return {'type': 'array', 'minimum': len(value), 'maximum': len(value), 'items': [fixed(item) for item in value]}
    return {'type': 'boolean' if type(value) is bool else 'integer' if type(value) is int else 'string', 'constant': value}


def parameter_shape(definition):
    key = definition.key
    if key in MATERIAL_VALUES:
        return shape(MATERIAL_VALUES[key])
    if key in SUPPORTED_VALUES:
        return fixed(plain(SUPPORTED_VALUES[key]))
    if key == 'provider.accounts':
        return shape(SequenceSchema(ACCOUNT, 2, 3))
    if key == 'provider.profiles':
        items = [shape(IMAGE_PROFILE if role == 'MEDIA' else EMBEDDING_PROFILE if role.startswith('EMBEDDING') else GENERATION_PROFILE) for role in ROLES]
        for role, item in zip(ROLES, items):
            for field in item['fields']:
                if field['name'] == 'material_role':
                    field['schema'] = {'type': 'string', 'constant': role}
        return {'type': 'array', 'minimum': len(items), 'maximum': len(items), 'items': items}
    if key == 'provider.role_profiles':
        return shape(ROLE_PROFILES)
    if key == 'logging.module_levels':
        from companion_memory.logging_service._rules import _MODULES, _LEVEL_NUMBERS
        return {'type': 'object', 'fields': [{'name': name, 'optional': True, 'nullable': False,
            'schema': {'type': 'string', 'choices': list(_LEVEL_NUMBERS)}} for name in _MODULES]}
    result = {'type': definition.type}
    if type(definition.enum) is Declared:
        result['choices'] = plain(definition.enum.value)
    if type(definition.range) is Declared:
        for name, bound in (('minimum', definition.range.value.lower), ('maximum', definition.range.value.upper)):
            if type(bound) is Bound:
                result[name] = bound.value
    return result


def scaffold(schema):
    if 'constant' in schema:
        return schema['constant']
    if schema['type'] == 'object':
        return {field['name']: None if field.get('nullable') else scaffold(field['schema'])
            for field in schema['fields'] if not field.get('optional')}
    if schema['type'] == 'array':
        return [scaffold(item) for item in schema['items']] if 'items' in schema else [scaffold(schema['item']) for _ in range(schema['minimum'])]
    if len(schema.get('choices', [])) == 1:
        return schema['choices'][0]
    if schema['type'] == 'integer' and schema.get('minimum') == schema.get('maximum') and 'minimum' in schema:
        return schema['minimum']
    return None


def form_view(domains, bootstrap, root: str):
    """Supply editable schema plus explicitly incomplete unvalidated draft values."""
    paths = {'media.root_directory': root + '/blobs', 'media.staging_directory': root + '/upload_staging'}
    protected = {entry.definition.key: plain(entry.state.value) for entry in bootstrap.list_entries() if type(entry.state) is PresentValue}
    output = {}
    for name, registry in domains.items():
        parameters = []
        for definition in registry.list_definitions():
            schema = parameter_shape(definition)
            initial = (protected[definition.key] if definition.key in protected else paths[definition.key] if definition.key in paths
                else plain(definition.default.value) if type(definition.default) is LiteralDefault else scaffold(schema))
            parameters.append({'key': definition.key, 'schema': schema, 'initial': initial,
                'boundary': definition.apply_mode, 'sensitivity': definition.sensitivity})
        output[name] = parameters
    return {'domains': output, 'complete': False, 'business_ready': False}
