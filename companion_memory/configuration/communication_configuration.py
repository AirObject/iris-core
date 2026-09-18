"""Native communication definitions appended to a complete managed candidate.

Original birth versions remain byte-for-byte readable. An explicit candidate
can append the complete disabled policy; activation still uses the existing
configuration decision and runtime consumer acknowledgement protocol.
"""
from __future__ import annotations

from typing import Any, cast
from .communication_schema import definitions, VALUES
from .content_codec import decode_content_entry, encode_content_entry
from .daily_resolution import freeze_daily_domains
from .managed_codec import candidate_inputs
from .managed_resolution import ManagedConfigurationCandidate, ManagedConfigurationOk, resolve_managed_configuration


def extend_candidate(candidate: ManagedConfigurationCandidate) -> ManagedConfigurationCandidate:
    """Add all native metadata and defaults together without changing old values."""
    if any(e.definition.key == 'communication.ws' for e in candidate.text.list_entries()):
        return candidate
    raw = cast(list[Any], list(candidate_inputs(candidate, dict(candidate._directories))))
    domains = {'foundation': raw[0], 'runtime': raw[1], 'platform': raw[2][0],
        'content': raw[3], 'information': raw[4], 'text': raw[5]}
    declarations = {name: [decode_content_entry(encode_content_entry(entry))[0]
        for entry in view.list_entries()] for name, view in (
            ('foundation', candidate.foundation), ('runtime', candidate.runtime),
            ('platform', candidate.platforms[0]), ('content', candidate.content),
            ('information', candidate.information), ('text', candidate.text))}
    declarations['text'].extend(definitions())
    for name, registry in freeze_daily_domains(declarations).items():
        domains[name]['registry'] = registry
    result = resolve_managed_configuration(*raw)
    if type(result) is not ManagedConfigurationOk:
        raise ValueError('Complete communication candidate failed validation.')
    return result.value


def policy(candidate: ManagedConfigurationCandidate):
    """Resolve the new policy or the explicit closed-listener upgrade default."""
    from .communication_schema import validate
    has_policy = any(e.definition.key == 'communication.ws' for e in candidate.text.list_entries())
    return validate({key: dict(candidate.text.record(key) if has_policy else value) for key, value in VALUES.items()})
