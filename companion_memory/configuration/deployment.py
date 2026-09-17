"""Validated deployment parameters for the protected, pre-business bootstrap.

This registry owns every service limit and path before a business configuration
exists. Its snapshot grants no model, ingress, or memory capability. Protected
paths remain administrator metadata and are never exported to anonymous health.
"""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import cast

from . import create_registry_builder, Ok
from .definitions import (Bound, Declared, LiteralDefault, MetadataValue, NoDefault,
                          NotApplicable, ParameterDefinitionInput, RangeDescriptor)
from .resolution import _prepare_resolution, _resolve_entries
from .resolution_results import ResolutionOk
from .snapshots import EffectiveSnapshot, PresentValue


LIMITS = {
    'deployment.port': (8080, 1024, 65535, 'port'),
    'deployment.free_reserve_bytes': (2147483648, 2147483648, 8589934592, 'bytes'),
    'management.body_max_bytes': (1048576, 1048576, 1048576, 'bytes'),
    'management.http_connections': (16, 1, 64, 'connections'),
    'management.http_timeout_seconds': (15, 1, 60, 'seconds'),
    'management.session_seconds': (3600, 60, 86400, 'seconds'),
    'management.session_limit': (16, 1, 64, 'sessions'),
    'management.token_limit': (32, 1, 128, 'tokens'),
    'management.token_max_seconds': (2592000, 60, 31536000, 'seconds'),
    'management.login_attempts': (5, 1, 20, 'attempts'),
    'management.login_window_seconds': (300, 60, 3600, 'seconds'),
    'management.page_size': (32, 1, 64, 'records'),
    'management.backup_file_limit': (8192, 1, 65536, 'files'),
    'management.backup_max_bytes': (1073741824, 1048576, 2147483648, 'bytes'),
    'management.maintenance_timeout_seconds': (30, 1, 60, 'seconds'),
}


def environment_settings() -> DeploymentSettings:
    """Read one bounded deployment value document; credentials remain mounted files."""
    import json
    import os
    raw = os.environ.get('IRIS_DEPLOYMENT', '{}')
    if len(raw.encode()) > 8192:
        raise ValueError('Deployment settings exceed the startup carrier.')
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('Duplicate deployment field.')
            result[key] = value
        return result
    values = json.loads(raw, object_pairs_hook=pairs)
    if type(values) is not dict:
        raise ValueError('Deployment values must be an object.')
    return resolve_deployment(values)


def definition(key: str, *, kind: str, default: object = None,
               limits: tuple[int, int] | None = None, unit: str | None = None,
               sensitivity: str = 'public') -> ParameterDefinitionInput:
    """Declare deployment metadata once; callers still resolve explicit overrides."""
    na = NotApplicable('This parameter does not use this metadata field.')
    return cast(ParameterDefinitionInput, dict(
        key=key, owner_module='configuration', schema_revision='managed_deployment_v1',
        type=kind, default=NoDefault() if default is None else LiteralDefault(default),
        required=True, nullable=False, unit=Declared(unit) if unit else na,
        range=Declared(RangeDescriptor(Bound(limits[0], True), Bound(limits[1], True))) if limits else na,
        enum=na, validator=[], dependencies=[], scope=['instance'], override_policy='no_override',
        sensitivity=sensitivity, read_roles=['trusted_operator'], write_roles=['trusted_operator'],
        apply_mode='RESTART_REQUIRED', activation_group=na,
        cost_impact='Local service resources; no model requests.',
        migration_impact='Requires a controlled restart with the original resource identity.',
        description=key, deprecated=False, replacement=na, upgrade_rule=na,
        rationale='Bound local resources and protect deployment identity.',
        consumers=['management', 'runtime', 'persistence'],
        validation_method='Exact registry, closed fields, bounded values and physical startup verification.'))


def definitions() -> tuple[ParameterDefinitionInput, ...]:
    """Complete deployment registry; secrets are references, never stored values."""
    return (
        definition('deployment.data_root', kind='string', default='/data', sensitivity='administrator'),
        definition('deployment.secret_root', kind='string', default='/run/secrets', sensitivity='administrator'),
        definition('deployment.origin', kind='string', default='http://127.0.0.1:8080'),
        definition('deployment.bind', kind='string', default='0.0.0.0'),
        *(definition(key, kind='integer', default=value, limits=(lower, upper), unit=unit)
          for key, (value, lower, upper, unit) in LIMITS.items()),
    )


@dataclass(frozen=True, slots=True)
class DeploymentSettings:
    """Immutable settings issued only after a complete deployment validation."""
    snapshot: EffectiveSnapshot

    def value(self, key: str) -> str | int:
        found = self.snapshot.get_entry(key)
        if type(found) is not ResolutionOk or type(found.value.state) is not PresentValue or type(found.value.state.value) not in (str, int):
            raise ValueError('A declared deployment setting is required.')
        return cast(str | int, found.value.state.value)

    def integer(self, key: str) -> int:
        value = self.value(key)
        if type(value) is not int:
            raise ValueError('An integer deployment setting is required.')
        return value

    def text(self, key: str) -> str:
        value = self.value(key)
        if type(value) is not str:
            raise ValueError('A text deployment setting is required.')
        return value


def resolve_deployment(explicit: dict[str, MetadataValue]) -> DeploymentSettings:
    """Reject unknown fields and invalid origins without opening files or sockets."""
    from urllib.parse import urlsplit
    builder = create_registry_builder()
    for item in definitions():
        if type(builder.register(item)) is not Ok:
            raise ValueError('Invalid deployment definition.')
    frozen = builder.freeze()
    if type(frozen) is not Ok:
        raise ValueError('Invalid deployment registry.')
    prepared = _prepare_resolution(frozen.value, explicit)
    if type(prepared) is not ResolutionOk:
        raise ValueError('Invalid deployment input.')
    resolved = _resolve_entries(prepared.value, explicit)
    if type(resolved) is not ResolutionOk:
        raise ValueError('Invalid deployment value.')
    settings = DeploymentSettings(EffectiveSnapshot._from_entries(frozen.value, resolved.value))
    for key in ('deployment.data_root', 'deployment.secret_root'):
        path = settings.text(key)
        if not path.startswith('/') or str(PurePosixPath(path)) != path or '..' in PurePosixPath(path).parts or any(ord(c) < 32 for c in path):
            raise ValueError('Invalid protected path.')
    root, secret = (PurePosixPath(settings.text(k)) for k in ('deployment.data_root', 'deployment.secret_root'))
    if root == PurePosixPath('/') or root.is_relative_to(secret) or secret.is_relative_to(root):
        raise ValueError('Protected resources overlap.')
    origin = urlsplit(settings.text('deployment.origin'))
    if (origin.scheme not in ('http', 'https') or not origin.hostname or origin.username or origin.password
            or origin.path or origin.query or origin.fragment or origin.port != settings.integer('deployment.port')):
        raise ValueError('An exact service origin with explicit port is required.')
    if settings.text('deployment.bind') not in ('0.0.0.0', '127.0.0.1'):
        raise ValueError('Unsupported local listener.')
    return settings
