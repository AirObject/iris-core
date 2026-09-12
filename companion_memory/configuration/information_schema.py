"""Closed local information declarations and explicitly supported value vectors.

These records have no defaults. Callers supply every value and every complete
metadata definition; only the disabled sink and bound loopback test sink vectors
are supported. No model profile, free URL or executable option is accepted.
"""
from types import MappingProxyType
from typing import cast
from dataclasses import fields
from .definitions import ParameterDefinition, ParameterDefinitionInput, NoDefault, NotApplicable
from .content_codec import encode_content_entry
from .snapshots import SnapshotEntry, PresentValue

type SettingValue = int | bool | str

SUPPORTED_VALUES: MappingProxyType[str, MappingProxyType[str, SettingValue]] = MappingProxyType({
    'retrieval.local': MappingProxyType({'mode': 'LOCAL_LEXICAL_V1', 'query_max_bytes': 512, 'normalized_max_bytes': 8192, 'tokens_per_object': 4096, 'posting_visit_limit': 4096, 'candidate_limit': 128, 'dirty_overlay_limit': 128, 'relation_expansion_limit': 0, 'index_page_size': 16, 'index_workers': 1, 'rebuild_generations': 2, 'index_step_timeout_ms': 5000, 'index_recovery_timeout_ms': 30000, 'close_timeout_ms': 10000}),
    'retrieval.reply': MappingProxyType({'base_deadline_ms': 1000, 'concurrency': 2, 'queue_capacity': 0, 'memory_limit': 8, 'recent_limit': 4, 'goal_limit': 8, 'persona_max_bytes': 8192, 'state_max_bytes': 4096, 'response_max_bytes': 131072, 'persona_policy': 'ALLOW_EXPLICIT_PARTIAL', 'rerank_enabled': False, 'require_complete_index': False}),
    'retrieval.tickets': MappingProxyType({'ttl_seconds': 86400, 'live_limit': 10000, 'root_max_bytes': 2048, 'member_max_bytes': 512, 'consumption_max_bytes': 1024, 'member_limit': 8, 'cleanup_page_size': 16, 'cleanup_interval_ms': 60000}),
    'memory.usage': MappingProxyType({'gain': 8, 'forgotten_retention_seconds': 2592000, 'expiry_scan_enabled': True, 'expiry_scan_interval_ms': 60000, 'expiry_scan_page_size': 16, 'existing_object_limit': 100000, 'feedback_member_limit': 8, 'operation_timeout_ms': 5000}),
    'state.external': MappingProxyType({'record_max_bytes': 4096, 'field_text_max_bytes': 512, 'stale_after_seconds': 300, 'future_tolerance_seconds': 60, 'writer_policy': 'SINGLE_BOUND_HOST', 'operation_timeout_ms': 5000}),
    'goals.lifecycle': MappingProxyType({'record_max_bytes': 4096, 'content_max_bytes': 2048, 'open_goal_limit': 1000, 'source_limit': 8, 'aliases_per_goal': 64, 'list_page_size': 8, 'dedup_candidate_limit': 32, 'dedup_workers': 1, 'dedup_wait_timeout_ms': 5000, 'operation_timeout_ms': 5000, 'dedup_mode': 'EXACT_ONLY', 'semantic_policy': 'MARK_UNAVAILABLE'}),
    'goals.delivery': MappingProxyType({'sink_mode': 'DISABLED', 'workers': 1, 'scan_page_size': 16, 'scan_interval_ms': 1000, 'request_max_bytes': 2048, 'response_max_bytes': 1024, 'attempt_limit': 2, 'attempt_timeout_ms': 1000, 'total_timeout_ms': 2500, 'retry_delay_ms': 10, 'overdue_policy': 'COALESCE_ONCE', 'repeat_expired': False}),
    'management.host': MappingProxyType({'request_max_bytes': 16384, 'header_max_bytes': 8192, 'response_max_bytes': 131072, 'connections': 4, 'body_timeout_ms': 2000, 'write_timeout_ms': 2000, 'session_limit': 16, 'session_ttl_seconds': 3600, 'command_concurrency': 1, 'command_queue_capacity': 0}),
})

_DECLARATIONS = (('retrieval.local', 'retrieval', ('retrieval', 'memory', 'runtime'), 'local_retrieval_limits'), ('retrieval.reply', 'retrieval', ('retrieval', 'management'), 'reply_partition_limits'), ('retrieval.tickets', 'retrieval', ('retrieval', 'memory'), 'recall_ticket_limits'), ('memory.usage', 'memory', ('memory', 'retrieval', 'runtime'), 'memory_usage_limits'), ('state.external', 'state', ('state', 'retrieval'), 'external_state_limits'), ('goals.lifecycle', 'goals', ('goals', 'retrieval', 'runtime'), 'goal_lifecycle_limits'), ('goals.delivery', 'goals', ('goals', 'runtime', 'management'), 'goal_delivery_limits'), ('management.host', 'management', ('management', 'retrieval', 'state', 'goals'), 'host_interface_limits'))
_DEPENDENCIES = {'retrieval.local': (), 'retrieval.reply': ('goals.lifecycle', 'management.host', 'retrieval.local', 'retrieval.tickets', 'state.external'), 'retrieval.tickets': ('memory.usage', 'retrieval.reply'), 'memory.usage': ('retrieval.tickets',), 'state.external': (), 'goals.lifecycle': ('goals.delivery',), 'goals.delivery': ('goals.lifecycle', 'management.host'), 'management.host': ('retrieval.reply',)}


def information_definitions() -> tuple[ParameterDefinitionInput, ...]:
    """Return complete native definitions for trusted registry construction."""
    return tuple(ParameterDefinitionInput(
        key=key, owner_module=owner, schema_revision='local_information_v1',
        type='object', default=NoDefault(), required=True, nullable=False,
        unit=NotApplicable('The complete record has no single unit.'),
        range=NotApplicable('The fixed validator checks each field and cross-field bound.'),
        enum=NotApplicable('There is no whole-record enumeration.'),
        validator=(validator,), dependencies=_DEPENDENCIES[key], scope=('instance',),
        override_policy='no_override', sensitivity='public', read_roles=('trusted_operator',),
        write_roles=('trusted_operator',), apply_mode='INITIALIZE_ONLY',
        activation_group=NotApplicable('No live activation.'),
        cost_impact='Local CPU and storage only; no query model calls.',
        migration_impact='New assembly and new database; existing formats remain unchanged.',
        description='Explicit bounded local information settings.', deprecated=False,
        replacement=NotApplicable('No replacement.'), upgrade_rule=NotApplicable('No automatic migration.'),
        rationale='Preserve bounded work, ownership and durable feedback evidence.', consumers=consumers,
        validation_method='Check exact fields, finite values, complete encoding and assembly bounds.',
    ) for key, owner, consumers, validator in _DECLARATIONS)


def matches_information_definition(definition: ParameterDefinition) -> bool:
    """Require every metadata field, including roles and explanatory declarations."""
    expected = next((item for item in information_definitions() if item['key'] == definition.key), None)
    if expected is None:
        return False
    return all(type(getattr(definition, field.name)) is type(expected[field.name])
               and getattr(definition, field.name) == expected[field.name]
               for field in fields(ParameterDefinition))


def valid_information_entries(entries: tuple[SnapshotEntry, ...]) -> bool:
    """Check exact nested types, supported vectors and whole entry byte limits."""
    if tuple(entry.definition.key for entry in entries) != tuple(sorted(SUPPORTED_VALUES)):
        return False
    for entry in entries:
        if not matches_information_definition(entry.definition) or type(entry.state) is not PresentValue or entry.state.source != 'EXPLICIT':
            return False
        value = entry.state.value
        if type(value) is not MappingProxyType or set(value) != set(SUPPORTED_VALUES[entry.definition.key]):
            return False
        for key, expected in SUPPORTED_VALUES[entry.definition.key].items():
            actual = value[key]
            if type(actual) is not type(expected):
                return False
            if entry.definition.key == 'goals.delivery' and key == 'sink_mode':
                if actual not in ('DISABLED', 'TEST_HTTP'):
                    return False
            elif actual != expected:
                return False
        if len(encode_content_entry(entry).encode('utf-8')) > 4096:
            return False
    return True
