"""Export exact native communication declarations as maintained protocol resources."""
from __future__ import annotations
import json
from collections import Counter
from hashlib import sha256
from pathlib import Path
from companion_memory.persistence import RecordSchema, ScalarSchema, SequenceSchema, BoundedTextSchema
from companion_memory.management.communication_protocol import SCHEMAS, LIMITS, SUBPROTOCOL
from companion_memory.management.communication_http_schema import HTTP_ROUTES


def factor_shared_schemas(document: dict) -> dict:
    """Share schema nodes only; properties maps and literal values stay literal."""
    import copy
    document = copy.deepcopy(document)
    roots = [(document['components']['schemas'], name) for name in document['components']['schemas']]
    for path in document['paths'].values():
        operation = path['post']
        roots.extend((parameter, 'schema') for parameter in operation.get('parameters', ()) if 'schema' in parameter)
        contents = [operation['requestBody']['content']]
        contents.extend(response['content'] for response in operation['responses'].values())
        for content in contents:
            roots.extend((media, 'schema') for media in content.values() if 'schema' in media)
    def children(value):
        if type(value) is not dict: return []
        found = []
        for name in ('properties', '$defs', 'patternProperties', 'dependentSchemas'):
            mapping = value.get(name, {})
            found.extend((mapping, key) for key in mapping)
        for name in ('allOf', 'anyOf', 'oneOf', 'prefixItems'):
            sequence = value.get(name, [])
            found.extend((sequence, index) for index in range(len(sequence)))
        for name in ('items', 'additionalProperties', 'not', 'if', 'then', 'else', 'contains',
                     'propertyNames', 'unevaluatedProperties', 'unevaluatedItems'):
            if name in value: found.append((value, name))
        return found
    counts: Counter[str] = Counter()
    shapes: dict[str, dict] = {}
    def scan(value):
        if type(value) is not dict: return
        encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'))
        if len(encoded) >= 180:
            counts[encoded] += 1
            shapes[encoded] = value
        for parent, key in children(value): scan(parent[key])
    for parent, key in roots: scan(parent[key])
    names = {encoded: 'SharedSchema_' + sha256(encoded.encode()).hexdigest()[:16]
        for encoded, count in counts.items() if count > 1}
    def rewrite(value, *, reference=True):
        if type(value) is not dict: return value
        encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'))
        if reference and encoded in names:
            return {'$ref': '#/components/schemas/' + names[encoded]}
        result = copy.deepcopy(value)
        for parent, key in children(result): parent[key] = rewrite(parent[key])
        return result
    for parent, key in roots: parent[key] = rewrite(parent[key])
    document['components']['schemas'].update({name: rewrite(shapes[encoded], reference=False)
        for encoded, name in names.items()})
    return document


def schema(value) -> dict:
    if type(value) is RecordSchema:
        properties = {}
        for field in value.fields:
            nested = schema(field.schema)
            properties[field.name] = {'anyOf': [nested, {'type': 'null'}]} if field.nullable else nested
        return {'type': 'object', 'additionalProperties': False, 'properties': properties,
            'required': [field.name for field in value.fields if not field.optional]}
    if type(value) is SequenceSchema:
        return {'type': 'array', 'items': schema(value.item), 'minItems': value.minimum, 'maxItems': value.maximum}
    if type(value) is BoundedTextSchema:
        return {'type': 'string', 'x-max-utf8-bytes': value.max_utf8_bytes}
    if type(value) is ScalarSchema:
        if value.kind == 'enum': return {'type': 'string', 'enum': list(value.choices)}
        if value.kind == 'boolean': return {'type': 'boolean'}
        if value.kind == 'integer': return {'type': 'integer'} | {key: bound for key, bound in (('minimum', value.minimum), ('maximum', value.maximum)) if bound is not None}
        return {'type': 'string', 'maxLength': 128, 'pattern': '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$'}
    raise TypeError('Unsupported native schema.')


def generate(directory: Path) -> None:
    ws = {'$schema': 'https://json-schema.org/draft/2020-12/schema', '$id': 'urn:iris:communication:ws:1',
        'title': SUBPROTOCOL, 'oneOf': [{'$ref': '#/$defs/' + key} for key in SCHEMAS],
        '$defs': {key: schema(value) | {'x-max-utf8-bytes': LIMITS[key]} for key, value in SCHEMAS.items()},
        'x-transport': {'compression': False, 'binaryMessages': False, 'reassemblyBytes': 8192,
            'credentialsInUrl': False, 'unknownReplay': False, 'ackMeaning': 'RECEIVED_NOT_COMPLETED'}}
    paths = {}
    for route in HTTP_ROUTES:
        chunk = route.content_type == 'application/octet-stream'
        paths[route.path] = {'post': {'operationId': route.path.strip('/').replace('/', '_'),
            'security': [{'hostBearer': []}] if route.path.startswith('/api/host/') else [{'administratorCookie': [], 'csrf': []}],
            'x-capability': route.capability, 'x-mode': route.mode, 'x-original-confirmation': route.original_confirmation,
            'requestBody': {'required': True, 'content': {route.content_type: {'schema':
                {'type': 'string', 'format': 'binary', 'maxLength': route.body_bytes} if chunk else schema(route.input_schema)}}},
            'x-max-body-bytes': route.body_bytes,
            'parameters': [{'in': 'header', 'name': name, 'required': True, 'schema': schema(route.input_schema.fields[i].schema)}
                for i, name in enumerate(('X-Iris-Entry', 'X-Iris-Upload', 'X-Iris-Offset'))] if chunk else [],
            'responses': {str(code): {'description': desc, 'content': {'application/json': {'schema': {'$ref': '#/components/schemas/Envelope'}}}}
                for code, desc in ((200, 'Confirmed or observed original fact'), (202, 'Unconfirmed: preserve original key and input'),
                    (400, 'Closed schema violation'), (403, 'Authority denied'), (409, 'Revision, mode or original-operation conflict'),
                    (429, 'Finite capacity exhausted'), (503, 'Resource or persistence unavailable'))}}}
    http = {'openapi': '3.1.0', 'info': {'title': 'Iris Core external communication', 'version': '1.0.0'},
        'paths': paths, 'components': {'securitySchemes': {
            'hostBearer': {'type': 'http', 'scheme': 'bearer'},
            'administratorCookie': {'type': 'apiKey', 'in': 'cookie', 'name': 'iris_session'},
            'csrf': {'type': 'apiKey', 'in': 'header', 'name': 'X-CSRF-Token'}}, 'schemas': {
            'Envelope': {'type': 'object', 'additionalProperties': False, 'required': ['version', 'outcome', 'cleanup_pending'],
                'properties': {'version': {'const': 1}, 'outcome': {'enum': ['COMMITTED', 'OBSERVED', 'ABSENT', 'UNCONFIRMED', 'NOT_COMMITTED', 'REJECTED', 'FAILED']},
                    'cleanup_pending': {'type': 'boolean'}, 'data': {'type': 'object'},
                    'error': {'type': 'object', 'additionalProperties': False, 'required': ['code', 'operation', 'field', 'reason'],
                        'properties': {key: {'type': 'string', 'maxLength': 128} for key in ('code', 'operation', 'field', 'reason')}}}}}}}
    from protocol.http_contracts import extend
    extend(http, schema)
    from protocol.public_results import extend as public_results
    public_results(http, schema)
    from protocol.query_results import extend as query_results
    query_results(http, schema)
    http = factor_shared_schemas(http)
    directory.mkdir(parents=True, exist_ok=True)
    for name, value in (('http', http), ('ws', ws)):
        encoded = json.dumps(value, ensure_ascii=False, separators=(',', ':')) + '\n'
        if len(encoded.encode()) > 1048576: raise ValueError('Protocol resource exceeds the maintained HTTP response limit.')
        (directory / (name + '.json')).write_text(encoded)


if __name__ == '__main__':
    generate(Path(__file__).parent)
