"""Initial managed values for safe local limits and one generic platform.

These values fill setup drafts before an operator selects a model supplier.
They do not create provider identities, credentials, model material or authority
to send requests. The native complete resolver still validates every candidate.
"""
from __future__ import annotations
from copy import deepcopy


DEFAULT_VALUES: dict[str, dict[str, int | bool]] = {
    'foundation': {
        'provider.close_timeout_ms': 10000,
        'provider.max_in_flight': 1,
        'provider.query_row_limit': 100,
        'provider.request_max_bytes': 1048576,
        'provider.request_timeout_ms': 60000,
        'provider.retry_delay_ms': 0,
    },
    'runtime': {
        'ingress.event_max_bytes': 8192,
        'learning.input_units_limit': 262144,
        'learning.material_max_bytes': 262144,
        'learning.output_units_limit': 4096,
        'logging.web_query_max_bytes': 32768,
        'logging.web_query_row_limit': 32,
        'logging.web_query_timeout_ms': 1000,
        'logging.web_window_events': 512,
        'management.observation_concurrency': 2,
        'management.observation_max_bytes': 32768,
        'management.observation_row_limit': 32,
        'management.observation_timeout_ms': 2000,
        'management.refresh_min_interval_ms': 1000,
        'runtime.claim_lease_ms': 15000,
        'runtime.close_timeout_ms': 5000,
        'runtime.focus_drain_timeout_ms': 30000,
        'runtime.local_retry_limit': 1,
        'runtime.max_active_entries': 1,
        'runtime.operation_timeout_ms': 60000,
        'runtime.read_page_size': 16,
        'runtime.recovery_timeout_ms': 30000,
        'runtime.transfer_page_size': 16,
    },
    'content': {
        'audit.history_item_max_bytes': 8192,
        'audit.history_items_per_operation': 8,
        'cognition.candidate_item_limit': 8,
        'cognition.candidate_item_max_bytes': 8192,
        'cognition.candidate_max_bytes': 73728,
        'media.blob_max_bytes': 1048576,
        'media.close_timeout_ms': 10000,
        'media.event_occurrence_limit': 2,
        'media.file_worker_capacity': 5,
        'media.gc_interval_ms': 600000,
        'media.gc_page_size': 16,
        'media.gc_unreferenced_grace_ms': 600000,
        'media.interpretation_record_max_bytes': 2048,
        'media.interpretation_text_max_bytes': 512,
        'media.io_timeout_ms': 5000,
        'media.occurrence_total_timeout_ms': 60000,
        'media.operation_timeout_ms': 10000,
        'media.preparation_total_timeout_ms': 900000,
        'media.processing_suspect_after_ms': 120000,
        'media.read_chunk_bytes': 65536,
        'media.read_concurrency': 2,
        'media.recovery_timeout_ms': 60000,
        'media.unbound_upload_retention_ms': 3600000,
        'media.upload_chunk_bytes': 65536,
        'media.upload_concurrency': 2,
        'media.upload_total_timeout_ms': 60000,
        'memory.current_max_bytes': 4096,
        'memory.forget_below': 20,
        'memory.initial_retention': 50,
        'memory.read_concurrency': 2,
        'memory.read_page_size': 16,
        'memory.read_timeout_ms': 2000,
        'memory.restore_at': 35,
    },
    'platform': {
        'history_context_count': 1,
        'recent_context_count': 1,
        'target_count': 2,
        'normal_soft_limit': 1000,
        'explicit_short_enabled': False,
        'idle_tail_enabled': False,
        'idle_timeout_ms': 0,
    },
}


def initial_override(domain: str, key: str) -> int | bool | None:
    """Return an explicit managed setup default, if one was approved."""
    lookup = key.rsplit('.buffer.', 1)[-1] if domain == 'platform' else key
    return DEFAULT_VALUES.get(domain, {}).get(lookup)


TEXT_FIELD_DEFAULTS: dict[str, dict[str, object]] = {
    'cognition.tool_policy': {'names': ['search_memories', 'read_memories', 'read_subjects', 'list_goals']},
    'dream.management': {'control_enabled': True},
    'dream.schedule': {'enabled': False, 'local_time': '03:00', 'focus_default': False},
    'goals.semantic_deduplication': {'enabled': True},
    'retrieval.query_vectors': {'single_flight': True},
    'management.semantic_observation': {
        'include_vectors': False, 'include_query_text': False,
        'include_source_text': False, 'include_audit': False,
    },
    'memory.long_term_maintenance': {'decay_enabled': False},
    'runtime.learning_scheduler': {'enabled_on_create': False},
    'self_model.initial_persona': {
        'generation_goal': '维持稳定的发言风格，并充分认识自我。',
    },
}


def complete_initial(domain: str, key: str, initial: object, root: str) -> object:
    """Fill safe nested setup fields while leaving supplier bindings unresolved."""
    if domain != 'text' or type(initial) is not dict:
        return initial
    selected = dict(initial)
    selected.update(deepcopy(TEXT_FIELD_DEFAULTS.get(key, {})))
    if key == 'retrieval.semantic_storage':
        selected['index_root'] = root + '/indexes'
    return selected
