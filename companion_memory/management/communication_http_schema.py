"""Closed additive HTTP protocol inputs and native compatibility route bindings.

Binary chunks have a 65536-byte body and exact X-Iris-Entry, X-Iris-Upload and
X-Iris-Offset headers. The offset is the canonical decimal integer; uploads
retain the native operation key and never use filenames or remote URLs.
The listener installs the media adapters separately from these declarations.
"""
from dataclasses import dataclass
from types import MappingProxyType
from companion_memory.persistence import Field, RecordSchema, ScalarSchema, SequenceSchema, BoundedTextSchema
from companion_memory.information.records import ID, TIME, REVISION, choice
from companion_memory.information.business import WRITE_KINDS, wire_schema
from companion_memory.information.management import SCHEMAS as NATIVE
from companion_memory.retrieval.tickets import CONSUME
from .managed_host_http import ROUTES
from companion_memory.retrieval.query_formats import SEMANTIC_QUERY, SEMANTIC_PREPARE
from .communication_records import EVENTS


@dataclass(frozen=True, slots=True)
class HTTPRoute:
    path: str
    capability: str
    input_schema: RecordSchema
    body_bytes: int
    mode: str
    original_confirmation: str | None
    content_type: str = 'application/json'


def envelope(schema):
    return RecordSchema((Field('entry_id', ID), Field('input', schema)))


MEDIA_BEGIN = RecordSchema((Field('key', ID), Field('modality', choice('IMAGE', 'AUDIO', 'VIDEO'))))
MEDIA_FINISH = RecordSchema((Field('upload_id', ID),))
MEDIA_INSPECT = RecordSchema((Field('key', ID), Field('modality', choice('IMAGE', 'AUDIO', 'VIDEO'))))
MEDIA_CHUNK_HEADERS = RecordSchema((Field('entry_id', ID), Field('upload_id', ID),
    Field('offset', ScalarSchema('integer', 0, 1048576))))
REGISTRATION = RecordSchema((Field('key', ID), Field('entry_id', ID), Field('host_id', ID),
    Field('platform_id', ID), Field('external_entry_id', BoundedTextSchema(512))))
NOTIFICATION_RESULTS = RecordSchema((Field('route_id', ID), Field('after', BoundedTextSchema(128))))
HTTP_ROUTES = (
    HTTPRoute('/api/host/media/begin', 'media_upload', envelope(MEDIA_BEGIN), 16384, 'INPUT_ATTACHMENTS', '/api/host/media/resolve'),
    HTTPRoute('/api/host/media/chunk', 'media_upload', MEDIA_CHUNK_HEADERS, 65536, 'INPUT_ATTACHMENTS', None, 'application/octet-stream'),
    HTTPRoute('/api/host/media/finish', 'media_upload', envelope(MEDIA_FINISH), 16384, 'INPUT_ATTACHMENTS', '/api/host/media/resolve'),
    HTTPRoute('/api/host/media/resolve', 'confirm', envelope(MEDIA_BEGIN), 16384, 'ORIGINAL_CONFIRMATION', None),
    HTTPRoute('/api/host/media/inspect', 'media_inspect', envelope(MEDIA_INSPECT), 16384, 'OBSERVE', None),
    HTTPRoute('/api/host/capabilities', 'authenticated', RecordSchema(()), 16384, 'OBSERVE', None),
    HTTPRoute('/api/host/notifications/status', 'notifications', RecordSchema((Field('route_ids', SequenceSchema(ID, 1, 16)),)), 16384, 'OBSERVE', None),
    HTTPRoute('/api/host/notifications/results', 'notifications', NOTIFICATION_RESULTS, 16384, 'OBSERVE', None),
    *(HTTPRoute('/api/connections/hosts/' + action, 'administrator', REGISTRATION, 16384,
        'NORMAL' if action == 'register' else 'ORIGINAL_CONFIRMATION', '/api/connections/hosts/confirm') for action in ('register', 'confirm')),
    HTTPRoute('/api/host/state', 'state_read', envelope(RecordSchema(())), 16384, 'NORMAL', None),
    HTTPRoute('/api/host/goals', 'goal_read', envelope(RecordSchema((Field('cursor', BoundedTextSchema(4096), optional=True),))), 16384, 'NORMAL', None),
    *(HTTPRoute('/api/host/' + path, capability, envelope(shape), 16384, 'NORMAL', '/api/host/recalls/resolve')
        for path, capability, shape in (('prepare', 'prepare', SEMANTIC_PREPARE),
            ('memory/search', 'query', SEMANTIC_QUERY), ('memory/deep-recall', 'deep_recall', SEMANTIC_QUERY))),
    *(HTTPRoute('/api/connections/' + path, 'administrator', shape, 16384, mode, None)
        for path, shape, mode in (
            ('overview', RecordSchema(()), 'OBSERVE'),
            *((name + '/list', RecordSchema((Field('after', BoundedTextSchema(128)),)), 'OBSERVE') for name in ('hosts', 'routes', 'probes')),
            ('plans/list', RecordSchema((Field('route_id', ID), Field('after', BoundedTextSchema(128)))), 'OBSERVE'),
            ('tickets/create', RecordSchema((Field('route_id', ID),)), 'NORMAL'),
            ('probes/run', RecordSchema((Field('key', ID), Field('route_id', ID), Field('connection_id', ID, nullable=True))), 'NORMAL'),
            ('connections/disconnect', RecordSchema((Field('connection_id', ID), Field('route_ids', SequenceSchema(ID, 0, 8)))), 'CONTROL'),
            ('routes/create', RecordSchema((Field('key', ID), Field('route_id', ID), Field('host_id', ID),
                Field('entries', SequenceSchema(ID, 1, 8)), Field('event_types', EVENTS))), 'NORMAL'),
            ('routes/enable', RecordSchema((Field('key', ID), Field('route_id', ID), Field('expected_revision', REVISION),
                Field('enabled', ScalarSchema('boolean')))), 'NORMAL'))),
    *(HTTPRoute('/api/host/' + path, capability,
        envelope(RecordSchema((Field('operation_key', ID), *wire_schema(CONSUME if operation == 'record_usage' else NATIVE[WRITE_KINDS[operation]]).fields))),
        16384, 'NORMAL', '/api/host/operations/resolve')
        for path, (capability, operation, _, _) in ROUTES.items() if operation in WRITE_KINDS or operation == 'record_usage'),
)
BY_PATH = MappingProxyType({route.path: route for route in HTTP_ROUTES})
ENVELOPE_VERSION = 1
ADDITIVE_PROTOCOL_VERSION = 1
