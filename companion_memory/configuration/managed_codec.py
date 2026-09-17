"""Canonical six-domain configuration bodies with one aggregate admission bound.

Every complete definition, state, provenance and value uses the existing v2
entry encoder. Dream catalog v7 binds the independent material format. Aggregate
admission neither truncates fields nor redistributes per-group quotas.
"""
from __future__ import annotations
from dataclasses import fields
from typing import TYPE_CHECKING, TypedDict
from .content_codec import encode_content_entry, decode_content_entry, dump, entries_digest, platform_domain_id
if TYPE_CHECKING:
    from .managed_resolution import ManagedConfigurationCandidate

CONFIGURATION_BODY_LIMIT=524288


class ConfigurationCapacityExceeded(ValueError):
    """A fully encoded configuration cannot fit its immutable carrier limits."""


class EncodedEntry(TypedDict):
    """Complete canonical entry with its original key."""
    parameter_key: str
    body: str


class EncodedDomain(TypedDict):
    """One digest-bound complete configuration domain."""
    domain_id: str
    digest: str
    entries: list[EncodedEntry]


class InitializationValues(TypedDict):
    """All configuration bodies and their single immutable publication catalog."""
    domains: list[EncodedDomain]
    catalog: str


def candidate_values(candidate: ManagedConfigurationCandidate) -> InitializationValues:
    domains: list[EncodedDomain]=[];total=0;count=0
    selected=[('foundation',candidate.foundation,40),('runtime',candidate.runtime,22),('content',candidate.content,36),
              *((platform_domain_id(p.platform_id),p,7) for p in candidate.platforms),
              ('information',candidate.information,8),('daily_cognition',candidate.text,23)]
    for name,view,expected in selected:
        try:
            entries=tuple((e.definition.key,encode_content_entry(e)) for e in view.list_entries())
        except ValueError:
            raise ConfigurationCapacityExceeded() from None
        if len(entries)!=expected:raise ValueError('A complete fixed configuration domain is required.')
        total+=sum(len(body.encode('utf-8')) for _,body in entries);count+=len(entries)
        domains.append({'domain_id':name,'digest':entries_digest(entries),
                        'entries':[{'parameter_key':key,'body':body} for key,body in entries]})
    if count!=136 or len(domains)!=6 or total>CONFIGURATION_BODY_LIMIT:
        raise ConfigurationCapacityExceeded()
    catalog=dump({'version':8,'domains':[{'domain_id':d['domain_id'],'revision':1} for d in domains],
                  'materials':[{f.name:getattr(m,f.name) for f in fields(m)} for m in candidate.material_contracts]})
    if len(catalog.encode('utf-8'))>8192:raise ConfigurationCapacityExceeded()
    return {'domains':domains,'catalog':catalog}


def candidate_inputs(candidate: ManagedConfigurationCandidate,protected_directories:object) -> tuple[object,...]:
    """Rebuild explicit inputs with their complete original metadata and provenance."""
    def domain(view):
        explicit={}
        for entry in view.list_entries():
            definition,present,value,source=decode_content_entry(encode_content_entry(entry))
            if present and source=='EXPLICIT':explicit[definition['key']]=value
        return {'registry':view.get_registry(),'explicit_values':explicit}
    return (domain(candidate.foundation),domain(candidate.runtime),
            [{'platform_id':p.platform_id,**domain(p)} for p in candidate.platforms],domain(candidate.content),
            domain(candidate.information),domain(candidate.text),protected_directories,candidate.material_contracts)


def restore_candidate(values: InitializationValues, bootstrap: ManagedConfigurationCandidate) -> ManagedConfigurationCandidate:
    """Reconstruct a whole immutable version, retaining definition and value provenance."""
    from .managed_resolution import resolve_managed_configuration, ManagedConfigurationOk
    from .daily_resolution import freeze_daily_domains
    if values['catalog'] != candidate_values(bootstrap)['catalog']:
        raise ValueError('Configuration layout or material binding changed.')
    definitions, explicit, encoded = {}, {}, {}
    for encoded_domain in values['domains']:
        name = encoded_domain['domain_id']
        if name in definitions:
            raise ValueError('Repeated configuration domain.')
        definitions[name], explicit[name] = [], {}
        entries = tuple((entry['parameter_key'], entry['body']) for entry in encoded_domain['entries'])
        if entries_digest(entries) != encoded_domain['digest'] or len(dict(entries)) != len(entries):
            raise ValueError('Configuration digest or key set changed.')
        encoded[name] = entries
        for key, body in entries:
            definition, present, value, source = decode_content_entry(body)
            if definition['key'] != key:
                raise ValueError('Configuration entry identity changed.')
            definitions[name].append(definition)
            if present and source == 'EXPLICIT':
                explicit[name][key] = value
    registries = freeze_daily_domains(definitions)
    def domain(name):
        return {'registry': registries[name], 'explicit_values': explicit[name]}
    restored = resolve_managed_configuration(domain('foundation'), domain('runtime'),
        [{'platform_id': p.platform_id, **domain(platform_domain_id(p.platform_id))} for p in bootstrap.platforms],
        domain('content'), domain('information'), domain('daily_cognition'),
        dict(bootstrap._directories), bootstrap.material_contracts)
    if type(restored) is not ManagedConfigurationOk or candidate_values(restored.value) != values:
        raise ValueError('Configuration version failed complete canonical validation.')
    return restored.value
