"""Canonical six-domain configuration bodies with one aggregate admission bound.

Every complete definition, state, provenance and value uses the existing v2
entry encoder. Text catalog v4 binds the independent material format. Aggregate
admission neither truncates fields nor redistributes per-group quotas.
"""
from __future__ import annotations
from dataclasses import fields
from typing import TYPE_CHECKING, TypedDict
from .content_codec import encode_content_entry, decode_content_entry, dump, entries_digest, platform_domain_id
if TYPE_CHECKING:
    from .text_resolution import TextConfigurationCandidate

CONFIGURATION_BODY_LIMIT=524288


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


def candidate_values(candidate: TextConfigurationCandidate) -> InitializationValues:
    domains: list[EncodedDomain]=[];total=0;count=0
    selected=[('foundation',candidate.foundation,40),('runtime',candidate.runtime,22),('content',candidate.content,36),
              *((platform_domain_id(p.platform_id),p,7) for p in candidate.platforms),
              ('information',candidate.information,8),('text_learning',candidate.text,5)]
    for name,view,expected in selected:
        entries=tuple((e.definition.key,encode_content_entry(e)) for e in view.list_entries())
        if len(entries)!=expected:raise ValueError('A complete fixed configuration domain is required.')
        total+=sum(len(body.encode('utf-8')) for _,body in entries);count+=len(entries)
        domains.append({'domain_id':name,'digest':entries_digest(entries),
                        'entries':[{'parameter_key':key,'body':body} for key,body in entries]})
    if count!=118 or len(domains)!=6 or total>CONFIGURATION_BODY_LIMIT:
        raise ValueError('Complete text configuration exceeds its aggregate capacity.')
    catalog=dump({'version':4,'domains':[{'domain_id':d['domain_id'],'revision':1} for d in domains],
                  'materials':[{f.name:getattr(m,f.name) for f in fields(m)} for m in candidate.material_contracts]})
    if len(catalog.encode('utf-8'))>8192:raise ValueError('Configuration catalog exceeds its complete capacity.')
    return {'domains':domains,'catalog':catalog}


def candidate_inputs(candidate: TextConfigurationCandidate,protected_directories:object) -> tuple[object,...]:
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
