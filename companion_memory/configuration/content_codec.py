"""Complete content configuration serialization with independent version binding.

Entry bodies preserve metadata, values and provenance. Control and DEL escapes
bound expansion inside the existing persistence envelope without changing it.
"""
from __future__ import annotations
from dataclasses import fields
import hashlib
import json
from typing import TYPE_CHECKING, cast
from .definitions import ParameterDefinition, ParameterDefinitionInput, MetadataValue
from .persistent_codec import _encode, _decode, decode_json
from .snapshots import SnapshotEntry, MissingValue, PresentValue
if TYPE_CHECKING:
    from .content_resolution import ContentConfigurationCandidate


def dump(value: object) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
    import re
    short = {'b': '0008', 'f': '000c', 'n': '000a', 'r': '000d', 't': '0009'}
    text = re.sub(r'\\(?:["\\/bfnrt]|u[0-9a-fA-F]{4})',
                  lambda match: '\\u' + short[match.group()[1]]
                  if match.group()[1] in short else match.group(), text)
    return text.replace('\x7f', '\\u007f')


def encode_content_entry(entry: SnapshotEntry) -> str:
    """Encode a complete immutable entry in the explicit content format."""
    value = {'version': 2, 'definition': {field.name: _encode(getattr(entry.definition, field.name))
                                        for field in fields(ParameterDefinition)},
             'state': 'MISSING' if type(entry.state) is MissingValue else 'PRESENT'}
    if type(entry.state) is PresentValue:
        value.update(value=_encode(entry.state.value), source=entry.state.source)
    encoded = dump(value)
    if len(encoded.encode('utf-8', errors='strict')) > 8192:
        raise ValueError('Configuration entry exceeds the complete encoding bound.')
    return encoded


def decode_content_entry(encoded: str) -> tuple[ParameterDefinitionInput, bool, MetadataValue | None, str | None]:
    """Decode only canonical versioned content metadata; reject duplicate keys."""
    if type(encoded) is not str or len(encoded.encode('utf-8', errors='strict')) > 8192:
        raise ValueError('Invalid configuration entry size.')
    value = decode_json(encoded)
    if type(value) is not dict or type(value.get('version')) is not int or value['version'] != 2 or type(value.get('definition')) is not dict or dump(value) != encoded:
        raise ValueError('Unsupported configuration entry format.')
    present = value.get('state') == 'PRESENT'
    if set(value) != ({'version', 'definition', 'state', 'value', 'source'} if present else {'version', 'definition', 'state'}) or not present and value['state'] != 'MISSING':
        raise ValueError('Invalid configuration value state.')
    source = value.get('source')
    if present and source not in ('EXPLICIT', 'DEFAULT'):
        raise ValueError('Invalid configuration provenance.')
    definition = cast(ParameterDefinitionInput, {k: _decode(v) for k, v in value['definition'].items()})
    return definition, present, cast(MetadataValue, _decode(value['value']) if present else None), source


def platform_domain_id(platform_id: str) -> str:
    return 'platform:' + hashlib.sha256(platform_id.encode('utf-8')).hexdigest()


def entries_digest(entries: tuple[tuple[str, str], ...]) -> str:
    return hashlib.sha256(dump(entries).encode('utf-8')).hexdigest()


def candidate_values(candidate: ContentConfigurationCandidate) -> dict[str, object]:
    """Build and bound the complete four-domain initialization payload."""
    domains = []
    count = 0
    total = 0
    for identity, view in [('foundation', candidate.foundation), ('runtime', candidate.runtime), ('content', candidate.content)] + [(platform_domain_id(p.platform_id), p) for p in candidate.platforms]:
        entries = tuple((e.definition.key, encode_content_entry(e)) for e in view.list_entries())
        count += len(entries)
        total += sum(len(body.encode('utf-8')) for _, body in entries)
        if not entries or any(len(key.encode('utf-8')) > 128 for key, _ in entries):
            raise ValueError('Configuration keys exceed the supported encoding bound.')
        domains.append({'domain_id': identity, 'digest': entries_digest(entries),
                        'entries': [{'parameter_key': key, 'body': body} for key, body in entries]})
    if len(domains) > 4 or count > 128 or total > 262144:
        raise ValueError('Complete configuration exceeds the supported encoding bound.')
    catalog = dump({'version': 2, 'domains': [{'domain_id': d['domain_id'], 'revision': 1} for d in domains],
                    'materials': [{field.name: getattr(m, field.name) for field in fields(m)} for m in candidate.material_contracts]})
    if len(catalog.encode('utf-8')) > 8192:
        raise ValueError('Configuration catalog exceeds the supported encoding bound.')
    return {'domains': domains, 'catalog': catalog}


def candidate_inputs(candidate: ContentConfigurationCandidate, protected_directories: object) -> tuple[object, ...]:
    """Reconstruct original native parser inputs for a side-effect-free preflight."""
    def domain(view):
        explicit = {}
        for entry in view.list_entries():
            definition, present, value, source = decode_content_entry(encode_content_entry(entry))
            if present and source == 'EXPLICIT':
                explicit[definition['key']] = value
        return {'registry': view.get_registry(), 'explicit_values': explicit}
    return (domain(candidate.foundation), domain(candidate.runtime),
            [{'platform_id': platform.platform_id, **domain(platform)} for platform in candidate.platforms],
            domain(candidate.content), protected_directories, candidate.material_contracts)
