"""Exact inherited values admitted by the local information configuration pack.

Resource paths remain explicit validated bindings. These are support constraints,
not defaults: callers must supply complete original registries and value sources.
The existing content configuration parser retains its broader independent scope.
"""
from types import MappingProxyType
from typing import cast
import json
from .content_resolution import ContentConfigurationCandidate
from .snapshots import PresentValue

_VECTOR = '{"content":{"audit.history_item_max_bytes":8192,"audit.history_items_per_operation":8,"cognition.candidate_item_limit":8,"cognition.candidate_item_max_bytes":8192,"cognition.candidate_max_bytes":73728,"media.blob_max_bytes":1048576,"media.close_timeout_ms":10000,"media.event_occurrence_limit":2,"media.file_worker_capacity":5,"media.gc_interval_ms":600000,"media.gc_page_size":16,"media.gc_unreferenced_grace_ms":600000,"media.interpretation_record_max_bytes":2048,"media.interpretation_text_max_bytes":512,"media.io_timeout_ms":5000,"media.occurrence_total_timeout_ms":60000,"media.operation_timeout_ms":10000,"media.preparation_total_timeout_ms":600000,"media.processing_concurrency":1,"media.processing_suspect_after_ms":120000,"media.read_chunk_bytes":65536,"media.read_concurrency":2,"media.recovery_timeout_ms":60000,"media.unbound_upload_retention_ms":3600000,"media.upload_chunk_bytes":65536,"media.upload_concurrency":2,"media.upload_total_timeout_ms":60000,"memory.current_max_bytes":4096,"memory.forget_below":20,"memory.initial_retention":50,"memory.read_concurrency":2,"memory.read_page_size":16,"memory.read_timeout_ms":2000,"memory.restore_at":35},"foundation":{"audit.event_max_bytes":8192,"audit.events_per_operation":16,"logging.close_timeout_ms":2000,"logging.console_enabled":true,"logging.console_level":"INFO","logging.console_stream":"stderr","logging.emergency_capacity":8,"logging.emergency_interval_ms":1000,"logging.event_max_bytes":4096,"logging.file_enabled":true,"logging.file_level":"DEBUG","logging.flush_timeout_ms":1000,"logging.instance_level":"INFO","logging.io_timeout_ms":200,"logging.module_levels":{},"logging.preparation_capacity":16,"logging.probe_interval_ms":1000,"logging.retained_segments":5,"logging.rotation_bytes":10485760,"logging.sink_capacity":1024,"logging.warning_reserve":128,"provider.accounts":[{"account_id":"sample_account","attempt_limit":10000,"cost_limit_atoms":1000000000,"currency":"TEST","max_in_flight":1,"window_id":"sample_window"}],"provider.close_timeout_ms":10000,"provider.max_in_flight":2,"provider.profiles":[{"account_id":"sample_account","attempt_timeout_ms":5000,"capability":"GENERATION","dimensions":null,"input_price_atoms":2,"max_attempts":2,"max_input_units":49152,"max_items":2,"max_output_units":2048,"media_tasks":[],"model_id":"sample_generation","output_price_atoms":3,"profile_id":"sample_learning","space_id":null,"wire_protocol":"SIMULATED"},{"account_id":"sample_account","attempt_timeout_ms":5000,"capability":"MEDIA_UNDERSTANDING","dimensions":null,"input_price_atoms":1,"max_attempts":2,"max_input_units":1048576,"max_items":1,"max_output_units":0,"media_tasks":[{"modality":"IMAGE","task":"DESCRIBE"},{"modality":"AUDIO","task":"TRANSCRIBE"},{"modality":"VIDEO","task":"DESCRIBE"}],"model_id":"sample_media_model","output_price_atoms":0,"profile_id":"sample_media","space_id":null,"wire_protocol":"SIMULATED"}],"provider.query_row_limit":100,"provider.request_max_bytes":65536,"provider.request_timeout_ms":30000,"provider.result_max_bytes":8192,"provider.retry_delay_ms":10,"provider.role_profiles":{"LEARNING":["sample_learning"],"MEDIA":["sample_media"]},"storage.close_timeout_ms":10000,"storage.command_max_bytes":1048576,"storage.lock_wait_ms":50,"storage.operation_timeout_ms":30000,"storage.read_capacity":2,"storage.receipt_max_bytes":65536,"storage.wal_checkpoint_pages":100},"runtime":{"ingress.event_max_bytes":2048,"learning.input_units_limit":49152,"learning.material_max_bytes":49152,"learning.output_units_limit":2048,"logging.web_query_max_bytes":32768,"logging.web_query_row_limit":32,"logging.web_query_timeout_ms":1000,"logging.web_window_events":512,"management.observation_concurrency":2,"management.observation_max_bytes":32768,"management.observation_row_limit":32,"management.observation_timeout_ms":2000,"management.refresh_min_interval_ms":1000,"runtime.claim_lease_ms":15000,"runtime.close_timeout_ms":5000,"runtime.focus_drain_timeout_ms":30000,"runtime.local_retry_limit":1,"runtime.max_active_entries":1,"runtime.operation_timeout_ms":5000,"runtime.read_page_size":16,"runtime.recovery_timeout_ms":30000,"runtime.transfer_page_size":16}}'


def _matches(actual: object, expected: object) -> bool:
    if type(expected) is dict:
        if type(actual) is not MappingProxyType or set(actual) != set(expected):
            return False
        return all(_matches(actual[key], expected[key]) for key in expected)
    if type(expected) is list:
        return type(actual) is tuple and len(actual) == len(expected) and all(_matches(a, b) for a, b in zip(actual, expected))
    return type(actual) is type(expected) and actual == expected


def supports_inherited_values(candidate: ContentConfigurationCandidate) -> bool:
    """Require the full inherited vector without substituting any value."""
    expected = cast(dict[str, dict[str, object]], json.loads(_VECTOR))
    for name, values in expected.items():
        view = candidate.foundation if name == 'foundation' else candidate.runtime if name == 'runtime' else candidate.content
        actual = {entry.definition.key: entry.state.value for entry in view.list_entries() if type(entry.state) is PresentValue}
        if any(key not in actual or not _matches(actual[key], value) for key, value in values.items()):
            return False
    for platform in candidate.platforms:
        selected = {'history_context_count': 1, 'recent_context_count': 1, 'target_count': 2,
                    'normal_soft_limit': 1000, 'explicit_short_enabled': False,
                    'idle_tail_enabled': False, 'idle_timeout_ms': 0}
        if any(not _matches(platform.parameter(key), expected) for key, expected in selected.items()):
            return False
    return True
