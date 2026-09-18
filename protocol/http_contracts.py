"""Closed JSON contracts for original events, confirmations and public results.

Native record schemas are exported by generate.py. Conditional wire shapes
which are validated by ingress rather than RecordSchema are expressed here.
UTF-8 and canonical-byte budgets are extensions alongside JSON Schema bounds.
"""
from __future__ import annotations
from typing import Any


def obj(properties: dict[str, Any], optional: tuple[str, ...] = ()) -> dict[str, Any]:
    return {'type': 'object', 'additionalProperties': False, 'properties': properties,
        'required': [name for name in properties if name not in optional]}


def nullable(value: dict) -> dict:
    return {'anyOf': [value, {'type': 'null'}]}


def text(limit: int, minimum: int = 0) -> dict:
    return {'type': 'string', 'minLength': minimum, 'maxLength': limit, 'x-max-utf8-bytes': limit}


def enum(*values: str) -> dict:
    return {'type': 'string', 'enum': list(values)}


def array(item: dict, maximum: int, minimum: int = 0) -> dict:
    return {'type': 'array', 'items': item, 'minItems': minimum, 'maxItems': maximum}


ID = {'type': 'string', 'pattern': '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$', 'maxLength': 128}
TIME = {'type': 'integer', 'minimum': 0, 'maximum': 2**63-1}
BOOL = {'type': 'boolean'}
TIMESTAMP = text(64, 1) | {'format': 'date-time'}


def event_schema(version: int) -> dict:
    report = obj({'status': enum('MISSING', 'COMPLETE', 'EMPTY', 'PARTIAL', 'FAILED', 'REFUSED'),
        'text': nullable(text(512)), 'source_ref': nullable(text(128, 1)),
        'coverage': enum('COMPLETE', 'EXPLICIT_PARTIAL', 'UNSPECIFIED')})
    report['allOf'] = [
        {'if': {'properties': {'status': enum('MISSING', 'FAILED')}},
            'then': {'properties': {'text': {'type': 'null'}, 'coverage': {'const': 'UNSPECIFIED'}}}},
        {'if': {'properties': {'status': {'const': 'MISSING'}}}, 'then': {'properties': {'source_ref': {'type': 'null'}}},
            'else': {'properties': {'source_ref': text(128, 1)}}},
        {'if': {'properties': {'status': enum('COMPLETE', 'PARTIAL')}}, 'then': {'properties': {'text': text(512, 1)}}},
        {'if': {'properties': {'status': {'const': 'EMPTY'}}}, 'then': {'properties': {'text': {'const': ''}}}},
        {'if': {'properties': {'status': {'const': 'REFUSED'}}}, 'then': {'properties': {'text': {'const': '敏感信息无法访问'}, 'coverage': {'const': 'UNSPECIFIED'}}}},
        {'if': {'properties': {'status': enum('COMPLETE', 'EMPTY')}}, 'then': {'properties': {'coverage': {'const': 'COMPLETE'}}}},
        {'if': {'properties': {'status': {'const': 'PARTIAL'}}}, 'then': {'properties': {'coverage': {'const': 'EXPLICIT_PARTIAL'}}}},
    ]
    medium = {'reference_id': ID, 'occurrence_id': ID, 'modality': enum('IMAGE', 'AUDIO', 'VIDEO')}
    if version == 2: medium['interpretation'] = nullable(report)
    else: medium.update(understanding=nullable(text(8192)), understanding_source=enum('EXTERNAL', 'NONE'),
        understanding_state=enum('AVAILABLE', 'MISSING', 'REFUSED'))
    result = obj({'event_version': {'const': version}, 'external_event_id': text(512, 1), 'client_event_key': ID,
        'event_kind': enum('MESSAGE', 'PERCEPTION', 'SELF_OUTPUT', 'ACTION_RESULT'),
        'sender': obj({'subject_id': text(512, 1), 'display_name': nullable(text(512, 1)), 'role': ID, 'identity_source': ID}),
        'occurred_at': nullable(TIMESTAMP), 'body': text(8192),
        'quotation': array(obj({'body': text(8192), 'author': nullable(text(512, 1)),
            'event_id': nullable(text(512, 1)), 'occurred_at': nullable(TIMESTAMP)}), 16),
        'media': array(obj(medium), 2),
        'correlation': nullable(obj({'correlation_id': ID, 'state': enum('INTENDED', 'PREPARED', 'EMITTED', 'RESULT')})),
        'extensions': obj({})}, ('external_event_id', 'client_event_key', 'occurred_at'))
    result['oneOf'] = [{'required': ['external_event_id'], 'not': {'required': ['client_event_key']}},
        {'required': ['client_event_key'], 'not': {'required': ['external_event_id']}}]
    result['anyOf'] = [{'properties': {'body': text(8192, 1)}}, {'properties': {'media': {'minItems': 1}}}]
    result['x-canonical-max-bytes'] = 8192
    return result


def extend(http: dict, native_schema) -> None:
    from companion_memory.retrieval.query_formats import SEMANTIC_QUERY, SEMANTIC_PREPARE
    from companion_memory.information.business import WRITE_KINDS, wire_schema
    from companion_memory.information.management import SCHEMAS
    from companion_memory.retrieval.tickets import CONSUME
    import copy
    paths = http['paths']; definitions = http['components']['schemas']
    definitions['Event'] = {'oneOf': [event_schema(1), event_schema(2)]}
    accept = obj({'entry_id': ID, 'input': obj({'key': ID, 'event': {'$ref': '#/components/schemas/Event'}})})
    extra = {'accept': (accept, 'accept', 'INPUT_ATTACHMENTS'), 'accept/resolve': (accept, 'confirm', 'ORIGINAL_CONFIRMATION')}
    operations = []
    for operation, kind in WRITE_KINDS.items():
        shape = native_schema(wire_schema(SCHEMAS[kind]))
        shape['properties']['operation_key'] = ID;shape['required'].append('operation_key')
        operations.append(obj({'operation': {'const': operation}, 'input': shape}))
    usage = native_schema(wire_schema(CONSUME));usage['properties']['operation_key'] = ID;usage['required'].append('operation_key')
    operations.append(obj({'operation': {'const': 'record_usage'}, 'input': usage}))
    extra['operations/resolve'] = (obj({'entry_id': ID, 'input': {'oneOf': operations}}), 'confirm', 'ORIGINAL_CONFIRMATION')
    extra['recalls/resolve'] = (obj({'entry_id': ID, 'input': {'oneOf': [obj({'query': native_schema(shape),
        'prepared': {'const': prepared}, 'deep': {'const': deep}}) for shape, prepared, deep in
        ((SEMANTIC_PREPARE, True, False), (SEMANTIC_QUERY, False, False), (SEMANTIC_QUERY, False, True))]}}), 'confirm', 'ORIGINAL_CONFIRMATION')
    for name, (shape, capability, mode) in extra.items():
        path = '/api/host/' + name
        operation = copy.deepcopy(paths['/api/host/media/begin']['post'])
        operation.update(operationId=path.strip('/').replace('/', '_'))
        operation['requestBody']['content']['application/json']['schema'] = shape
        operation['x-capability'] = capability;operation['x-mode'] = mode
        operation['x-original-confirmation'] = '/api/host/accept/resolve' if name == 'accept' else None
        paths[path] = {'post': operation}
    definitions['MediaProgress'] = obj({'upload_id': ID, 'offset': TIME, 'state': {'const': 'VOLATILE_PROGRESS'}})
    definitions['OperationIdentity'] = obj({key: ID for key in ('database_id', 'owner_namespace', 'operation_kind', 'scope_id', 'operation_key')})
    definitions['Error'] = obj({**{key: ID for key in ('code', 'operation', 'field', 'reason')}, 'cleanup_pending': BOOL}, ('cleanup_pending',))
    # The complete native result schemas also describe retained original
    # receipts; no path, database row or private Python object is serialized.
    from companion_memory.runtime.managed_bootstrap import ManagedBootstrap
    from companion_memory.configuration.deployment import resolve_deployment
    assembly = ManagedBootstrap(resolve_deployment({})).assembly
    results = {}
    for command in assembly.commands:
        name = command.owner_namespace + '_' + command.operation_kind + '_result'
        results[name] = native_schema(command.result_schema)
    definitions.update(results)
    definitions['Receipt'] = obj({'schema_version': {'const': 1}, 'identity': {'$ref': '#/components/schemas/OperationIdentity'},
        'command_version': TIME, 'fingerprint_version': TIME, 'fingerprint': text(64, 64), 'commit_id': ID,
        'recorded_at': TIMESTAMP, 'result_schema_version': TIME,
        'result': {'anyOf': [{'$ref': '#/components/schemas/' + name} for name in results]}})
    definitions['MediaInspection'] = obj({'upload_id': ID, 'state': ID, 'volatile_offset': nullable(TIME),
        'reupload_required': BOOL, 'completion': nullable({'$ref': '#/components/schemas/media_publish_media_upload_result'}),
        'observed_at_us': TIME, 'progress_durable': {'const': False}})
    definitions['Capabilities'] = obj({'protocol_version': {'const': 1}, 'http_envelope_version': {'const': 1},
        'entries': array(ID, 8), 'operations': array(ID, 32), 'route_ids': array(ID, 16), 'event_types': array(ID, 4),
        'media': obj({'blob_max_bytes': {'const': 1048576}, 'chunk_max_bytes': {'const': 65536},
            'progress_durable': {'const': False}, 'ready_before_reference': {'const': True}}),
        'websocket': obj({'path': {'const': '/api/host/ws'}, 'subprotocol': {'const': 'iris.communication.v1'}}),
        'confirmation': {'const': 'ORIGINAL_KEY_AND_INPUT'}, 'unknown_retry': {'const': False}})
    for path, name in (('/api/host/capabilities', 'Capabilities'), ('/api/host/media/inspect', 'MediaInspection'),
            ('/api/host/media/chunk', 'MediaProgress')):
        response = copy.deepcopy(definitions['Envelope'])
        response['properties']['data'] = {'$ref': '#/components/schemas/' + name}
        paths[path]['post']['responses']['200']['content']['application/json']['schema'] = response
    http['x-wire-rules'] = {'duplicateFields': 'REJECT', 'unknownFields': 'REJECT', 'nonfiniteNumbers': 'REJECT',
        'timeOffsets': 'REQUIRED_WHEN_TIMESTAMP_PRESENT', 'bodyLimitAppliesBeforeJSON': True,
        'originalConfirmation': 'SAME_KEY_SAME_INPUT_NO_NEW_SEND', 'origin': 'SCHEME_HOST_EFFECTIVE_PORT',
        'host': 'INDEPENDENT_AUTHORITY_CHECK', 'forwardedHeaders': 'NEVER_AUTHORITATIVE'}
