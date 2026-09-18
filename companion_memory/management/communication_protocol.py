"""Closed versioned wire declarations shared by communication adapters.

No WebSocket dispatcher is installed by importing these declarations. Full
messages are UTF-8, duplicate keys are rejected before native schema validation,
and encoded limits include envelope fields. Compression and binary frames are
not in this protocol. Goal text and credentials never occur in notifications.
"""
from types import MappingProxyType
from companion_memory.persistence import Field, RecordSchema, SequenceSchema, ScalarSchema, BoundedTextSchema
from companion_memory.information.records import ID, TIME, REVISION, VERSION, choice
from .communication_records import EVENTS

SUBPROTOCOL = 'iris.communication.v1'
WS_PATH = '/api/host/ws'
COMMON = (Field('version', VERSION),)
REQUEST = COMMON + (Field('request_id', ID),)
ROUTES = SequenceSchema(ID, 1, 8)
GOAL_PAYLOAD = RecordSchema((Field('canonical_goal_id', ID), Field('revision', REVISION),
    Field('kind', choice('UPCOMING', 'DUE')), Field('deadline', TIME),
    Field('suggestion', choice('UPCOMING', 'CONSIDER_ABANDON_OR_CHANGE_DEADLINE'))))
NOTICE = COMMON + (Field('route_id', ID), Field('route_revision', REVISION), Field('observed_at', TIME))
SCHEMAS = MappingProxyType({
    'authenticate': RecordSchema(REQUEST + (Field('type', choice('authenticate')), Field('ticket', BoundedTextSchema(128)))),
    'subscribe': RecordSchema(REQUEST + (Field('type', choice('subscribe')), Field('route_ids', ROUTES), Field('event_types', EVENTS), Field('takeover', ScalarSchema('boolean')))),
    'unsubscribe': RecordSchema(REQUEST + (Field('type', choice('unsubscribe')), Field('route_ids', ROUTES))),
    'ack': RecordSchema(COMMON + (Field('type', choice('ack')), Field('route_id', ID), Field('delivery_id', ID), Field('status', choice('RECEIVED')))),
    'ready': RecordSchema(COMMON + (Field('type', choice('ready')), Field('connection_id', ID), Field('route_ids', SequenceSchema(ID, 0, 16)), Field('event_types', EVENTS), Field('ping_seconds', ScalarSchema('integer', 20, 20)), Field('pong_seconds', ScalarSchema('integer', 10, 10)))),
    'subscription_result': RecordSchema(REQUEST + (Field('type', choice('subscription_result')), Field('route_ids', ROUTES), Field('subscription_id', ID), Field('state', choice('SUBSCRIBED', 'UNSUBSCRIBED')))),
    'subscription_changed': RecordSchema(COMMON + (Field('type', choice('subscription_changed')), Field('route_id', ID), Field('subscription_id', ID), Field('reason', choice('TAKEN_OVER', 'DISABLED', 'REVOKED', 'EXPIRED')))),
    'goal': RecordSchema(NOTICE + (Field('type', choice('notification')), Field('event', choice('goal.upcoming', 'goal.due')), Field('delivery_id', ID), Field('payload', GOAL_PAYLOAD))),
    'mode': RecordSchema(NOTICE + (Field('type', choice('notification')), Field('event', choice('core.mode_changed')), Field('mode_epoch', REVISION), Field('hint', choice('REFRESH_AVAILABILITY')))),
    'probe': RecordSchema(NOTICE + (Field('type', choice('notification')), Field('event', choice('connection.probe')), Field('probe_id', ID), Field('delivery_id', ID), Field('message', choice('CONNECTION_TEST')))),
    'ack_result': RecordSchema(COMMON + (Field('type', choice('ack_result')), Field('route_id', ID), Field('delivery_id', ID), Field('outcome', choice('COMMITTED', 'UNCONFIRMED', 'REJECTED')), Field('cleanup_pending', ScalarSchema('boolean')))),
    'error': RecordSchema(REQUEST + (Field('type', choice('error')), Field('code', choice('INVALID_INPUT', 'ACCESS_DENIED', 'RESOURCE_BUSY', 'MODE_BLOCKED', 'STORAGE_FAILED', 'TIMEOUT')), Field('reason', choice('INVALID_SHAPE', 'UNSUPPORTED_VERSION', 'LIMIT_EXCEEDED', 'BINDING_MISMATCH', 'ROUTE_OCCUPIED', 'TAKEOVER_LIMIT', 'CLEANUP_PENDING', 'NOT_READY', 'CONFIRM_ORIGINAL')), Field('reconnect', ScalarSchema('boolean')))),
    'closing': RecordSchema(COMMON + (Field('type', choice('closing')), Field('reason', choice('SHUTDOWN', 'EXPIRED', 'REVOKED', 'BACKPRESSURE', 'HEARTBEAT_TIMEOUT', 'PROTOCOL_ERROR')), Field('reconnect', ScalarSchema('boolean')))),
})
LIMITS = MappingProxyType({name: 1024 if name in ('ack', 'ack_result') else 2048 if name in ('goal', 'mode', 'probe') else 8192 for name in SCHEMAS})
