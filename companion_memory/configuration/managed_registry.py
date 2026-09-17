"""One native registry for managed setup; browser input contains values only.

All owner metadata comes from code declarations. A saved draft cannot register
parameters, override permissions, supply validation hooks, or create a snapshot
until every domain has passed the complete resolver.
"""
from __future__ import annotations

from dataclasses import fields
from typing import cast

from . import Ok, create_registry_builder
from .definitions import ParameterDefinition, ParameterDefinitionInput, MetadataValue, Declared, NoDefault, Bound
from .deployment import DeploymentSettings, definition
from .managed_bootstrap import bootstrap_snapshot
from .managed_schema import material_definitions
from .managed_resolution import PROTECTED_KEYS, bind_managed_material, resolve_managed_configuration
from .daily_schema import DAILY_RUNTIME_REQUIREMENTS, DAILY_CONTENT_REQUIREMENTS
from .dream_schema import PROVIDER_REQUIREMENTS
from .information_schema import information_definitions
from .runtime_schema import platform_requirements, RuntimeRequirement
from .persistent_codec import _encode, _decode
from .registry import ReadOnlyRegistry
from companion_memory.persistence.schema import valid_identifier


def runtime_definition(requirement: RuntimeRequirement, *, platform: bool = False, content: bool = False) -> ParameterDefinitionInput:
    kind = 'integer' if requirement.limits is not None else 'string' if content else 'boolean'
    result = definition(requirement.key, kind=kind, limits=requirement.limits, unit=requirement.unit,
        sensitivity='administrator' if requirement.key in PROTECTED_KEYS else 'public')
    result.update(owner_module=requirement.owner, consumers=requirement.consumers,
        apply_mode='NEXT_BATCH' if platform else 'INITIALIZE_ONLY',
        scope=('platform',) if platform else ('instance',), dependencies=requirement.dependencies,
        validator=(requirement.validator,) if requirement.validator else ())
    return result


def registries(settings: DeploymentSettings, directories: dict[str, tuple[str, ...]], platform_id: str) -> dict[str, ReadOnlyRegistry]:
    """Build all six exact domains without choosing a business value."""
    if not valid_identifier(platform_id):
        raise ValueError('Invalid platform identity.')
    foundation = []
    for d in bootstrap_snapshot(settings, directories).get_registry().list_definitions():
        foundation.append(cast(ParameterDefinitionInput,
            {f.name: _decode(_encode(getattr(d, f.name))) for f in fields(ParameterDefinition)}))
    for r in PROVIDER_REQUIREMENTS:
        d = definition(r.key, kind=r.type, limits=r.limits, unit=r.unit)
        d.update(owner_module='provider', consumers=('provider', 'runtime'), apply_mode='INITIALIZE_ONLY',
            dependencies=r.dependencies, validator=(r.validator,) if r.validator else ())
        if r.key == 'provider.profiles':
            d['apply_mode'] = 'NEXT_REQUEST'
        foundation.append(d)
    domains = {'foundation': foundation,
        'runtime': [runtime_definition(r) for r in DAILY_RUNTIME_REQUIREMENTS],
        'content': [runtime_definition(r, content=True) for r in DAILY_CONTENT_REQUIREMENTS],
        'platform': [runtime_definition(r, platform=True) for r in platform_requirements(platform_id)],
        'information': list(information_definitions()), 'text': list(material_definitions())}
    from .daily_resolution import freeze_daily_domains
    return freeze_daily_domains(domains)


def resolve_values(settings: DeploymentSettings, directories: dict[str, tuple[str, ...]],
                   platform_id: str, values: dict[str, object]):
    """Resolve a complete value document with deployment paths fixed by startup."""
    if set(values) != {'foundation', 'runtime', 'content', 'platform', 'information', 'text'}:
        raise ValueError('All six configuration domains are required.')
    native = registries(settings, directories, platform_id)
    domains = {}
    for name, registry in native.items():
        explicit = values[name]
        if type(explicit) is not dict:
            raise ValueError('Configuration domains must contain declared values.')
        domains[name] = {'registry': registry, 'explicit_values': explicit}
    root = settings.text('deployment.data_root')
    foundation = cast(dict[str, object], values['foundation'])
    content = cast(dict[str, object], values['content'])
    if (foundation.get('storage.database_file') != root + '/db/memory.sqlite3'
            or foundation.get('logging.file_directory') != root + '/logs/runtime'
            or content.get('media.root_directory') != root + '/blobs'
            or content.get('media.staging_directory') != root + '/upload_staging'):
        raise ValueError('Business resources must match trusted startup.')
    local = bootstrap_snapshot(settings, directories)
    from .snapshots import PresentValue
    for entry in local.list_entries():
        if entry.definition.key.startswith(('storage.', 'audit.')) and type(entry.state) is PresentValue:
            if foundation.get(entry.definition.key) != entry.state.value:
                raise ValueError('Business storage settings must match trusted startup.')
    return resolve_managed_configuration(domains['foundation'], domains['runtime'],
        [{'platform_id': platform_id, **domains['platform']}], domains['content'], domains['information'],
        domains['text'], directories, [bind_managed_material(platform_id)])


def schema_view(settings: DeploymentSettings, directories: dict[str, tuple[str, ...]], platform_id: str) -> dict[str, object]:
    """Return a bounded administrator projection, including unfilled required keys."""
    domains = registries(settings, directories, platform_id)
    result: dict[str, object] = {}
    for name, registry in domains.items():
        entries = []
        for d in registry.list_definitions():
            limits = {'minimum': d.range.value.lower.value, 'maximum': d.range.value.upper.value} if (type(d.range) is Declared
                and type(d.range.value.lower) is Bound and type(d.range.value.upper) is Bound) else None
            entries.append({'key': d.key, 'type': d.type, 'required': d.required,
                'sensitivity': d.sensitivity, 'owner': d.owner_module, 'limits': limits,
                'has_default': type(d.default) is not NoDefault, 'boundary': d.apply_mode})
        result[name] = entries
    return result
