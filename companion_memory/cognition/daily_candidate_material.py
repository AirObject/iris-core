"""Restore complete candidate inputs from original reasoning and read records.

This decoder grants no authority and performs no I/O. Native owners supply the
checked material bodies, and the application coordinator separately compares
the live grant, original source and all expected current revisions.
"""
from dataclasses import dataclass
from hashlib import sha256
from types import MappingProxyType
from typing import cast
from companion_memory.persistence import Value
from companion_memory.persistence.content_codec import decode_content,encode_content
from companion_memory.persistence.schema import InvalidValue,Field,RecordSchema,SequenceSchema,BoundedTextSchema,freeze_value
from companion_memory.memory.formats import ID,WORLD,record,sequence,isolate_object,isolate_subject
from companion_memory.memory.sources import SourcePayload,isolate_source
from companion_memory.ingress.media_events import decode_media_event
from companion_memory.media.interpretations import decode_interpretation
from .daily_candidates import DailyAuthority

Record=MappingProxyType[str,Value]
AUTHORITY=RecordSchema((Field('subject_ids',SequenceSchema(ID,0,16)),Field('worlds',SequenceSchema(WORLD,1,16)),
    Field('writable_objects',SequenceSchema(ID,0,16)),Field('route_ids',SequenceSchema(ID,0,16)),Field('memory_ref',ID),Field('partition_id',ID)))

@dataclass(frozen=True,slots=True)
class CandidateMaterial:
    source:Record
    authority:DailyAuthority
    observed:tuple[Record,...]
    payloads:dict[str,SourcePayload]
    original:Record

def restore_candidate_material(initial:bytes,tool_results:tuple[Record,...]) -> CandidateMaterial:
    """Only values actually delivered in the original context/tools count as read."""
    from companion_memory.provider.values import freeze
    value=cast(Record,freeze(decode_content(initial,262144),262144,owned=True))
    expected={'source','members','persona','authority','authority_digest','related','subjects'}
    if 'request_constraints' in value:
        if value['request_constraints']!={'memory_target_limit':2}:raise InvalidValue()
        expected.add('request_constraints')
    if set(value)!=expected:raise InvalidValue()
    source=isolate_source(value['source']);authority=record(freeze_value(AUTHORITY,value['authority'],owned=True))
    if encode_content(authority,16384)!=encode_content(value['authority'],16384):raise InvalidValue()
    subjects=cast(tuple[str,...],authority['subject_ids']);writable=cast(tuple[str,...],authority['writable_objects']);routes=cast(tuple[str,...],authority['route_ids'])
    worlds=tuple(record(w) for w in sequence(authority['worlds']))
    if len(set(subjects))!=len(subjects) or len(set(writable))!=len(writable) or len(set(routes))!=len(routes):raise InvalidValue()
    roster=tuple(isolate_subject(item) for item in sequence(value['subjects']))
    if tuple(item['subject_id'] for item in roster)!=subjects:raise InvalidValue()
    grant=MappingProxyType({'memory_ref':authority['memory_ref'],'entry_id':source['entry_id'],'partition_id':authority['partition_id'],'subjects':subjects,
        'worlds':worlds,'writable_objects':writable,'route_ids':routes})
    if sha256(encode_content(grant,16384)).hexdigest()!=value['authority_digest']:raise InvalidValue()
    members=tuple(record(m) for m in sequence(value['members']))
    if len(members)!=len(sequence(source['ordered_members'])):raise InvalidValue()
    payloads={}
    for item,member_value in zip(members,sequence(source['ordered_members'])):
        member=record(member_value)
        if set(item)!={'member','payload','interpretations'} or item['member']!=member:raise InvalidValue()
        body=cast(str,item['payload']).encode()
        if sha256(body).hexdigest()!=member['payload_digest']:raise InvalidValue()
        event=decode_media_event(body,8192,occurrence_limit=2,text_limit=512)
        interpretations=tuple(decode_interpretation(cast(str,r).encode()) for r in sequence(item['interpretations']))
        if len(interpretations)!=len(sequence(member['media'])):raise InvalidValue()
        for selected_value,version in zip(sequence(member['media']),interpretations):
            selected=record(selected_value)
            if any(version[key]!=selected[key] for key in ('interpretation_id','blob_id')):raise InvalidValue()
            if version['generation']!=selected['generation'] and not (version['origin']=='INTERNAL' and version['status']=='REFUSED' and cast(int,version['generation'])<cast(int,selected['generation'])):raise InvalidValue()
            if version['scope_kind']=='EVENT' and version['event_id']!=member['message_id']:raise InvalidValue()
        payloads[cast(str,member['message_id'])]=SourcePayload(event,body,interpretations)
    related=sequence(value['related'])
    if len(related)>4:raise InvalidValue()
    observed={}
    def add(raw):
        item=record(raw)
        current=isolate_object({k:v for k,v in item.items() if k!='source_refs'},text_format=True)
        if record(current['content'])['world_scope'] not in worlds:raise InvalidValue()
        # A later actual read of the same object replaces the earlier revision;
        # contradictory bytes for one revision are persistent corruption.
        prior=observed.get(current['object_id'])
        if prior is not None and (cast(int,current['revision'])<cast(int,prior['revision']) or current['revision']==prior['revision'] and current!=prior):raise InvalidValue()
        observed[current['object_id']]=current
    for item in related:add(item)
    for result in tool_results:
        if result['grant_digest']!=value['authority_digest']:raise InvalidValue()
        if result['state']=='RESULT_STORED' and result['name'] in ('search_memories','read_memories'):
            for item in sequence(result['items']):add(item)
    if len(observed)>20:raise InvalidValue()
    return CandidateMaterial(source,DailyAuthority(frozenset(subjects),worlds,frozenset(writable),routes),tuple(observed.values()),payloads,value)
