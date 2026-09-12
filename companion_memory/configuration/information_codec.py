"""Complete five-domain canonical persistence, retaining all definitions and values.

The old four-domain format remains independent. Information entries use the
existing typed entry encoding with a tighter per-entry bound and an explicitly
bound catalog version; no configuration value is reconstructed from defaults.
"""
from typing import cast
from .content_codec import (
    candidate_values as content_values, candidate_inputs as content_inputs,
    encode_content_entry, entries_digest, dump,
)
from .persistent_codec import decode_json
from .information_resolution import InformationConfigurationCandidate


def candidate_values(candidate: InformationConfigurationCandidate) -> dict[str, object]:
    """Bound all 113 full entry bodies and the independently versioned catalog."""
    original = content_values(candidate._content_candidate)
    domains = cast(list[dict[str, object]], original['domains'])
    entries = [{'parameter_key': e.definition.key, 'body': encode_content_entry(e)} for e in candidate.information.list_entries()]
    if len(entries) != 8 or any(len(entry['body'].encode('utf-8')) > 4096 for entry in entries):
        raise ValueError('Information entry exceeds its complete encoding budget.')
    count = sum(len(cast(list[object], domain['entries'])) for domain in domains)
    if count != 105 or len(domains) != 4:
        raise ValueError('The complete supported configuration requires 113 entries.')
    domains.append({'domain_id': 'information', 'digest': entries_digest(tuple((e['parameter_key'], e['body']) for e in entries)), 'entries': entries})
    total = sum(len(entry['body'].encode('utf-8')) for d in domains for entry in cast(list[dict[str, str]], d['entries']))
    if total > 294912:
        raise ValueError('Complete configuration exceeds its aggregate encoding budget.')
    catalog = decode_json(cast(str, original['catalog']))
    if type(catalog) is not dict:
        raise ValueError('A complete catalog is required.')
    catalog['version'] = 3
    catalog['domains'].append({'domain_id': 'information', 'revision': 1})
    text = dump(catalog)
    if len(text.encode('utf-8')) > 8192:
        raise ValueError('Configuration catalog exceeds its encoding budget.')
    return {'domains': domains, 'catalog': text}


def candidate_inputs(candidate: InformationConfigurationCandidate, protected_directories: object) -> tuple[object, ...]:
    """Reconstruct all original explicit carriers for publication preflight."""
    from .content_codec import decode_content_entry
    base = content_inputs(candidate._content_candidate, protected_directories)
    explicit = {}
    for entry in candidate.information.list_entries():
        definition, present, value, source = decode_content_entry(encode_content_entry(entry))
        if present and source == 'EXPLICIT':
            explicit[definition['key']] = value
    return (*base[:4], {'registry': candidate.information.get_registry(), 'explicit_values': explicit}, *base[4:])
