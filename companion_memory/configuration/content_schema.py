"""Closed content configuration metadata and cross-owner dependency declarations.

These bounds describe supported formats. Effective values are always explicit
and issued by the configuration resolver, including all resource directories.
"""
from dataclasses import replace
from .definitions import NotApplicable, ParameterDefinition
from .runtime_schema import RuntimeRequirement, RUNTIME_REQUIREMENTS, matches_runtime_definition

CONTENT_RUNTIME_REQUIREMENTS = tuple(
    replace(item, limits=(256, 49152)) if item.key == 'learning.material_max_bytes' else item
    for item in RUNTIME_REQUIREMENTS
)


def requirement(key: str, owner: str, unit: str | None, low: int, high: int,
                consumers: tuple[str, ...]) -> RuntimeRequirement:
    return RuntimeRequirement(key, owner, unit, (low, high), consumers)


_CONTENT = (
    requirement('memory.current_max_bytes', 'memory', 'bytes', 1024, 4096, ('memory', 'cognition')),
    requirement('memory.forget_below', 'memory', 'score', 0, 99, ('memory',)),
    requirement('memory.restore_at', 'memory', 'score', 1, 100, ('memory',)),
    requirement('memory.initial_retention', 'memory', 'score', 0, 100, ('memory', 'cognition')),
    requirement('memory.read_timeout_ms', 'memory', 'milliseconds', 1, 60000, ('memory',)),
    requirement('memory.read_concurrency', 'memory', 'requests', 1, 16, ('memory',)),
    requirement('memory.read_page_size', 'memory', 'rows', 1, 16, ('memory',)),
    requirement('cognition.candidate_item_limit', 'cognition', 'items', 1, 8, ('cognition', 'memory')),
    requirement('cognition.candidate_item_max_bytes', 'cognition', 'bytes', 1024, 8192, ('cognition', 'memory')),
    requirement('cognition.candidate_max_bytes', 'cognition', 'bytes', 4096, 73728, ('cognition', 'memory')),
    RuntimeRequirement('media.root_directory', 'media', None, None, ('media',)),
    RuntimeRequirement('media.staging_directory', 'media', None, None, ('media',)),
    requirement('media.blob_max_bytes', 'media', 'bytes', 1, 1048576, ('media', 'ingress')),
    requirement('media.event_occurrence_limit', 'media', 'items', 1, 2, ('media', 'ingress', 'cognition')),
    requirement('media.upload_chunk_bytes', 'media', 'bytes', 4096, 65536, ('media',)),
    requirement('media.upload_concurrency', 'media', 'uploads', 1, 4, ('media',)),
    requirement('media.processing_concurrency', 'media', 'jobs', 1, 2, ('media', 'runtime')),
    requirement('media.file_worker_capacity', 'media', 'workers', 1, 8, ('media',)),
    requirement('media.read_concurrency', 'media', 'reads', 1, 4, ('media',)),
    requirement('media.read_chunk_bytes', 'media', 'bytes', 4096, 65536, ('media',)),
    requirement('media.interpretation_text_max_bytes', 'media', 'bytes', 0, 512, ('media',)),
    requirement('media.interpretation_record_max_bytes', 'media', 'bytes', 1331, 2048, ('media', 'cognition')),
    requirement('media.operation_timeout_ms', 'media', 'milliseconds', 1, 60000, ('media', 'ingress')),
    requirement('media.upload_total_timeout_ms', 'media', 'milliseconds', 1, 60000, ('media',)),
    requirement('media.occurrence_total_timeout_ms', 'media', 'milliseconds', 1, 600000, ('media', 'runtime')),
    requirement('media.preparation_total_timeout_ms', 'runtime', 'milliseconds', 1, 3600000, ('runtime', 'media')),
    requirement('media.io_timeout_ms', 'media', 'milliseconds', 1, 60000, ('media',)),
    requirement('media.close_timeout_ms', 'media', 'milliseconds', 1, 60000, ('media',)),
    requirement('media.recovery_timeout_ms', 'media', 'milliseconds', 1, 60000, ('media', 'runtime')),
    requirement('media.processing_suspect_after_ms', 'media', 'milliseconds', 1000, 3600000, ('media', 'runtime')),
    requirement('media.unbound_upload_retention_ms', 'media', 'milliseconds', 60000, 86400000, ('media',)),
    requirement('media.gc_interval_ms', 'media', 'milliseconds', 1000, 86400000, ('media',)),
    requirement('media.gc_unreferenced_grace_ms', 'media', 'milliseconds', 0, 86400000, ('media',)),
    requirement('media.gc_page_size', 'media', 'blobs', 1, 64, ('media',)),
    requirement('audit.history_item_max_bytes', 'logging_service', 'bytes', 1024, 8192, ('logging_service', 'memory')),
    requirement('audit.history_items_per_operation', 'logging_service', 'items', 1, 8, ('logging_service', 'memory')),
)
_BINDINGS = {
    'memory.current_max_bytes': ('memory_object_limits', ('memory.', 'cognition.')),
    'media.blob_max_bytes': ('media_resource_limits', ('media.',)),
    'media.root_directory': ('media_resource_paths', ('media.root_directory', 'media.staging_directory')),
    'media.staging_directory': ('media_resource_paths', ('media.root_directory', 'media.staging_directory')),
    'audit.history_item_max_bytes': ('content_audit_limits', ('audit.', 'memory.current_max_bytes', 'cognition.candidate_item_limit')),
}
CONTENT_REQUIREMENTS = tuple(
    replace(item, validator=_BINDINGS[item.key][0], dependencies=tuple(
        other.key for other in _CONTENT if other.key != item.key
        and any(other.key.startswith(prefix) for prefix in _BINDINGS[item.key][1])
    )) if item.key in _BINDINGS else item for item in _CONTENT
)


def matches_content_definition(definition: ParameterDefinition, required: RuntimeRequirement, *, sensitivity: str = 'public') -> bool:
    """Check exact numeric or path metadata without evaluating submitted hooks."""
    if required.limits is not None:
        return matches_runtime_definition(definition, required, False, sensitivity=sensitivity)
    if definition.type != 'string' or type(definition.unit) is not NotApplicable or type(definition.range) is not NotApplicable:
        return False
    # Boolean and path declarations have identical non-numeric metadata. Compare
    # that metadata through the shared checker after an internal immutable copy.
    return matches_runtime_definition(replace(definition, type='boolean'), required, False, sensitivity=sensitivity)
