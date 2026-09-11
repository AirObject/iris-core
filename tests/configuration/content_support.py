"""Explicit content settings for isolated local resources and simulated Provider.

The selected values are test inputs, never service defaults. The complete domain
registries are created before persistence resources are initialized.
"""
from pathlib import Path
from typing import cast
from companion_memory.configuration import MetadataValue, NotApplicable
from companion_memory.configuration.content_material import bind_content_material
from companion_memory.configuration.content_schema import CONTENT_REQUIREMENTS, CONTENT_RUNTIME_REQUIREMENTS
from companion_memory.configuration.content_resolution import resolve_content_configuration, ContentConfigurationOk
from tests.runtime.configuration_support import inputs as runtime_inputs, definitions, registry

CONTENT_VALUES: dict[str, MetadataValue] = {
    'memory.current_max_bytes': 4096, 'memory.forget_below': 20, 'memory.restore_at': 35,
    'memory.initial_retention': 50, 'memory.read_timeout_ms': 2000, 'memory.read_concurrency': 2,
    'memory.read_page_size': 16, 'cognition.candidate_item_limit': 8,
    'cognition.candidate_item_max_bytes': 8192, 'cognition.candidate_max_bytes': 73728,
    'media.blob_max_bytes': 1048576, 'media.event_occurrence_limit': 2,
    'media.upload_chunk_bytes': 65536, 'media.upload_concurrency': 2,
    'media.processing_concurrency': 1, 'media.file_worker_capacity': 5,
    'media.read_concurrency': 2, 'media.read_chunk_bytes': 65536,
    'media.interpretation_text_max_bytes': 512, 'media.interpretation_record_max_bytes': 2048,
    'media.operation_timeout_ms': 10000, 'media.upload_total_timeout_ms': 60000,
    'media.occurrence_total_timeout_ms': 60000, 'media.preparation_total_timeout_ms': 600000,
    'media.io_timeout_ms': 5000, 'media.close_timeout_ms': 10000, 'media.recovery_timeout_ms': 60000,
    'media.processing_suspect_after_ms': 120000, 'media.unbound_upload_retention_ms': 3600000,
    'media.gc_interval_ms': 600000, 'media.gc_unreferenced_grace_ms': 600000,
    'media.gc_page_size': 16, 'audit.history_item_max_bytes': 8192, 'audit.history_items_per_operation': 8,
}


def inputs(root: Path, changes: dict[str, MetadataValue] | None = None):
    foundation, runtime, platforms, directories, _ = runtime_inputs(root)
    root = root.resolve()
    (root / 'media' / 'staging').mkdir(exist_ok=True)
    values = foundation['explicit_values']
    values.update({'provider.request_max_bytes': 65536, 'provider.result_max_bytes': 8192,
                   'provider.request_timeout_ms': 30000, 'provider.close_timeout_ms': 10000,
                   'storage.operation_timeout_ms': 30000, 'storage.close_timeout_ms': 10000,
                   'audit.event_max_bytes': 8192, 'audit.events_per_operation': 16})
    profiles = cast(list[dict[str, MetadataValue]], values['provider.profiles'])
    profiles[0].update(max_input_units=49152, max_output_units=2048, attempt_timeout_ms=5000)
    profiles[3].update(profile_id='sample_media', model_id='sample_media_model',
                       max_input_units=1048576, input_price_atoms=1, attempt_timeout_ms=5000)
    values['provider.profiles'] = [profiles[0], profiles[3]]
    values['provider.role_profiles'] = {'LEARNING': ['sample_learning'], 'MEDIA': ['sample_media']}
    accounts = cast(list[dict[str, MetadataValue]], values['provider.accounts'])
    accounts[0].update(attempt_limit=10000, cost_limit_atoms=1000000000)
    runtime['explicit_values'].update({'ingress.event_max_bytes': 2048, 'learning.material_max_bytes': 49152,
                                       'learning.input_units_limit': 49152, 'learning.output_units_limit': 2048})
    runtime['registry'] = registry(definitions(CONTENT_RUNTIME_REQUIREMENTS))
    content_definitions = definitions(CONTENT_REQUIREMENTS)
    for definition in content_definitions:
        definition['schema_revision'] = 'complete_content'
        if definition['key'] in ('media.root_directory', 'media.staging_directory'):
            definition.update(type='string', unit=NotApplicable('A directory path has no numeric unit.'),
                              range=NotApplicable('A directory path is validated as text.'))
    content = {'registry': registry(content_definitions), 'explicit_values': {
        **CONTENT_VALUES, 'media.root_directory': str(root / 'media'),
        'media.staging_directory': str(root / 'media' / 'staging'), **(changes or {})}}
    directories['media'].append(str(root / 'media' / 'staging'))
    return foundation, runtime, platforms, content, directories, [bind_content_material('sample_platform')]


def candidate(root: Path, changes: dict[str, MetadataValue] | None = None):
    supplied = inputs(root, changes)
    result = resolve_content_configuration(*supplied)
    assert type(result) is ContentConfigurationOk, result
    return result.value, supplied


def maximum_inputs(root: Path):
    """Fill actual entry encodings to the supported aggregate and item count."""
    from companion_memory.configuration.content_codec import candidate_values, decode_content_entry
    from companion_memory.configuration.content_resolution import ContentConfigurationOk, resolve_content_configuration
    from tests.configuration.resolution_support import resolution_definition
    supplied = inputs(root)
    base, _ = candidate(root)
    payload = candidate_values(base)
    domains = cast(list[dict[str, object]], payload['domains'])
    content_domain = next(domain for domain in domains if domain['domain_id'] == 'content')
    declarations = [decode_content_entry(item['body'])[0] for item in cast(list[dict[str, str]], content_domain['entries'])]
    content = cast(dict[str, object], supplied[3])
    explicit = cast(dict[str, MetadataValue], content['explicit_values'])
    count = sum(len(cast(list, domain['entries'])) for domain in domains)
    extras = []
    for ordinal in range(128 - count):
        key = 'content.extra_' + str(ordinal)
        extra = resolution_definition(key=key, owner_module='memory', schema_revision='content_extension',
                                      enum=NotApplicable('No text enumeration.'), read_roles=['trusted_operator'],
                                      write_roles=['trusted_operator'], apply_mode='INITIALIZE_ONLY', consumers=['memory'])
        extras.append(extra)
        explicit[key] = 'x'
    declarations.extend(extras)
    content['registry'] = registry(declarations)
    first = resolve_content_configuration(*supplied)
    assert type(first) is ContentConfigurationOk, first
    encoded = candidate_values(first.value)
    used = sum(len(entry['body'].encode('utf-8')) for domain in cast(list[dict[str, object]], encoded['domains'])
               for entry in cast(list[dict[str, str]], domain['entries']))
    remaining = 262144 - used
    for extra in extras:
        amount = min(remaining, 3000)
        extra['description'] += 'é' * (amount // 2) + 'x' * (amount % 2)
        remaining -= amount
    assert remaining == 0
    content['registry'] = registry(declarations)
    return supplied, declarations
