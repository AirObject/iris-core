"""Validate complete daily reasoning outputs without executing model suggestions.

Intrinsic checks reject partial JSON, mixed tool/final output, excess actions,
forward local references and undeclared tools. The runtime must still verify the
frozen grant, source anchors and current revisions before any domain write.
"""
from types import MappingProxyType
from typing import cast
from companion_memory.persistence.content_codec import decode_content, encode_content
from companion_memory.persistence.schema import Field, RecordSchema, SequenceSchema, InvalidValue, Value, freeze_value
from companion_memory.persistence.semantic_records import ID, P, V, N, integer, record as schema, enum, isolate, Record
from companion_memory.persistence.schema import BoundedTextSchema as Text
from companion_memory.memory.formats import WORLD, TIME_RANGE, ANCHOR_SCHEMA, check_world
from .text_output import AUXILIARY, BASIS

LOCAL = integer(0, 7)
EXISTING_REF = schema(existing_id=ID)
LOCAL_REF = schema(local_ref=LOCAL)
TOOL_ARGUMENTS = {
    'search_memories': schema(query=Text(512), world_scope=WORLD, limit=integer(1,4)),
    'read_memories': schema(refs=SequenceSchema(schema(object_id=ID, expected_revision=P),1,4)),
    'read_subjects': schema(subject_ids=SequenceSchema(ID,1,4)),
    'list_goals': schema(world_scope=ID, limit=integer(1,4)),
}
COMMON = (Field('local_ref', LOCAL), Field('target_anchors', SequenceSchema(ANCHOR_SCHEMA,1,2)),
    Field('auxiliary_refs', SequenceSchema(AUXILIARY,0,2)), Field('basis_refs', SequenceSchema(BASIS,0,2)))
SCORES = (Field('belief', integer(0,100)), Field('belief_reason', Text(256)))
REGISTER = RecordSchema((Field('action',enum('REGISTER_SUBJECT')),)+COMMON+schema(
    subject_kind=enum('PLATFORM_PERSON','THING','FICTIONAL_CHARACTER','CONTEXT'), label=Text(256),
    platform_id=(ID,), external_subject_id=(Text(512),)).fields)
SET_SCORES = RecordSchema((Field('action',enum('SET_SCORES')),)+COMMON+SCORES+schema(
    object_id=ID, expected_revision=P, retention_delta=integer(-10,10), retention_reason=Text(256)).fields)
MEMORY_BASE = schema(category=enum('EVENT','FACT','INFERENCE','OPINION'), body=Text(1024),
    stance=enum('ASSERTED','DENIED','UNCERTAIN','SELF_ENDORSED'), world_scope=WORLD,
    occurred_range=(TIME_RANGE,), applicable_range=(TIME_RANGE,))
RELATION_BASE = schema(relation_type=enum('SAME_SUBJECT','PLAYS_ROLE','SUPPORTS','REFUTES','DERIVED_FROM','CITES','CONTEXT','RELATED'),
    assertion=enum('ASSERTED','DENIED','UNCERTAIN','SELF_ENDORSED'), world_scope=WORLD)


def _mapping(value: object) -> dict[str, object]:
    if type(value) is dict and all(type(k) is str for k in value):
        return dict(value)
    if type(value) is MappingProxyType and all(type(k) is str for k in value):
        return dict(value)
    raise InvalidValue()


def _items(value: object, minimum: int, maximum: int) -> tuple[object, ...]:
    if type(value) not in (list,tuple) or not minimum <= len(cast(list, value)) <= maximum:
        raise InvalidValue()
    return tuple(cast(list, value))


def _text(value: Value) -> None:
    if type(value) is not str or not value.strip():
        raise InvalidValue()


def _reference(value: object, created: dict[int,str], expected: tuple[str,...]) -> Record:
    raw = _mapping(value)
    if set(raw) == {'existing_id'}:
        return isolate(EXISTING_REF, raw)
    ref = isolate(LOCAL_REF, raw)
    if created.get(cast(int, ref['local_ref'])) not in expected:
        raise InvalidValue()
    return ref


def _content(value: object, kind: str, created: dict[int,str]) -> Record:
    raw = _mapping(value)
    if kind == 'MEMORY':
        if not {'subject_ids','speaker_subject_id'} <= set(raw):
            raise InvalidValue()
        subjects = tuple(_reference(v,created,('SUBJECT',)) for v in _items(raw.pop('subject_ids'),0,4))
        speaker_raw = raw.pop('speaker_subject_id')
        speaker = None if speaker_raw is None else _reference(speaker_raw,created,('SUBJECT',))
        if len({encode_content(v,256) for v in subjects}) != len(subjects) or speaker is not None and speaker not in subjects:
            raise InvalidValue()
        content = dict(isolate(MEMORY_BASE,raw,4096))
        _text(content['body'])
        for name in ('occurred_range','applicable_range'):
            interval = content[name]
            if type(interval) is MappingProxyType and interval['start_us'] is not None and interval['end_us'] is not None:
                if cast(int,interval['start_us']) > cast(int,interval['end_us']):
                    raise InvalidValue()
        content.update(subject_ids=subjects,speaker_subject_id=speaker)
    else:
        endpoints = {}
        for name in ('from_ref','to_ref'):
            endpoint = _mapping(raw.pop(name, None))
            if set(endpoint) != {'type','id','expected_revision'} or endpoint['type'] not in ('SUBJECT','OBJECT'):
                raise InvalidValue()
            ref = _reference(endpoint['id'],created,('SUBJECT',) if endpoint['type']=='SUBJECT' else ('MEMORY','RELATION'))
            revision = endpoint['expected_revision']
            # Created values have their trusted initial revision; existing values
            # must carry an exact expected revision for the transaction check.
            freeze_value(P,revision)
            if 'local_ref' in ref and revision != 1:
                raise InvalidValue()
            endpoints[name] = MappingProxyType({'type':cast(str,endpoint['type']),'id':ref,'expected_revision':cast(int,revision)})
        content = dict(isolate(RELATION_BASE,raw,4096)); content.update(endpoints)
        if endpoints['from_ref'] == endpoints['to_ref']:
            raise InvalidValue()
    check_world(content['world_scope'])
    return MappingProxyType(content)


def _common(raw: dict[str,object]) -> Record:
    checked = isolate(RecordSchema(COMMON),{name:raw.pop(name) for name in (f.name for f in COMMON)},8192)
    for item in cast(tuple[Record,...],checked['target_anchors']):
        part = item['part']; start,end = item['start_utf8'],item['end_utf8']
        if ((part in ('QUOTATION','MEDIA')) != (item['item_index'] is not None)
                or (start is None) != (end is None) or start is not None and cast(int,start)>=cast(int,end)
                or part=='EVENT' and start is not None
                or (part=='MEDIA') != (item['occurrence_id'] is not None and item['interpretation_id'] is not None)
                or part!='MEDIA' and (item['occurrence_id'] is not None or item['interpretation_id'] is not None)):
            raise InvalidValue()
    for name,key in (('auxiliary_refs','message_id'),('basis_refs','object_id')):
        refs = cast(tuple[Record,...],checked[name])
        if len({cast(str,r[key]) for r in refs}) != len(refs):
            raise InvalidValue()
    return checked


def _action(value: object, created: dict[int,str]) -> Record:
    raw = _mapping(value)
    common = _common(raw)
    action = raw.get('action')
    if action == 'REGISTER_SUBJECT':
        result = isolate(REGISTER,{**raw,**common},8192)
        _text(result['label'])
        if result['subject_kind']=='PLATFORM_PERSON':
            if result['platform_id'] is None or result['external_subject_id'] is None:
                raise InvalidValue()
            _text(result['external_subject_id'])
        elif result['platform_id'] is not None or result['external_subject_id'] is not None:
            raise InvalidValue()
        return result
    if action == 'SET_SCORES':
        result = isolate(SET_SCORES,{**raw,**common},8192)
        _text(result['belief_reason']); _text(result['retention_reason'])
        return result
    if action in ('CREATE_MEMORY','CREATE_RELATION','REPLACE_CURRENT'):
        if action=='CREATE_MEMORY':
            content_raw = {name:raw.pop(name) for name in ('category','body','subject_ids','speaker_subject_id',
                'stance','world_scope','occurred_range','applicable_range')}
            kind='MEMORY'
        elif action=='CREATE_RELATION':
            content_raw = {name:raw.pop(name) for name in ('relation_type','from_ref','to_ref','assertion','world_scope')}
            kind='RELATION'
        else:
            content_raw = _mapping(raw.pop('content',None))
            kind='MEMORY' if 'body' in content_raw else 'RELATION'
        content = _content(content_raw,kind,created)
        extra = schema(object_id=ID,expected_revision=P).fields if action=='REPLACE_CURRENT' else ()
        checked = isolate(RecordSchema((Field('action',enum(cast(str,action))),)+SCORES+extra),raw,8192)
        _text(checked['belief_reason'])
        values: dict[str,Value] = {**checked,**common}
        if action=='REPLACE_CURRENT': values['content']=content
        else: values.update(content)
        return MappingProxyType(values)
    if action=='CREATE_GOAL':
        subjects = tuple(_reference(v,created,('SUBJECT',)) for v in _items(raw.pop('subject_refs',None),0,4))
        bases = tuple(freeze_value(LOCAL,v) for v in _items(raw.pop('basis_action_refs',None),0,2))
        if len(set(bases))!=len(bases) or any(created.get(cast(int,b)) not in ('MEMORY','RELATION') for b in bases):
            raise InvalidValue()
        if not bases and not common['basis_refs']:
            raise InvalidValue()
        if len({encode_content(v,256) for v in subjects}) != len(subjects):
            raise InvalidValue()
        checked = isolate(schema(action=enum('CREATE_GOAL'),content=Text(2048),world_scope=ID,
            deadline=(N,),reminder_lead_seconds=(integer(0,31536000),),route_id=(ID,)),raw,8192)
        _text(checked['content'])
        return MappingProxyType({**checked,**common,'subject_refs':subjects,'basis_action_refs':bases})
    raise InvalidValue()


def isolate_tool(value: object) -> Record:
    """Validate one read-only tool request; no execution or authorization occurs."""
    raw = _mapping(value)
    if set(raw) != {'name','arguments'} or type(raw['name']) is not str or raw['name'] not in TOOL_ARGUMENTS:
        raise InvalidValue()
    name = raw['name']; args = isolate(TOOL_ARGUMENTS[name],raw['arguments'],4096)
    if name=='search_memories':
        _text(args['query']); check_world(args['world_scope'])
    if name in ('read_subjects','read_memories'):
        refs=cast(tuple,args['subject_ids' if name=='read_subjects' else 'refs'])
        ids=refs if name=='read_subjects' else tuple(r['object_id'] for r in refs)
        if len(set(ids))!=len(ids): raise InvalidValue()
    return MappingProxyType({'name':name,'arguments':args})


def decode_daily_output(raw: bytes, *, turn: int, completed_tools: int) -> Record:
    """Parse a full output under the original run counters, rejecting a third tool round.

    This function is pure. It preserves action order and original local references;
    it never generates formal IDs, resolves platform identities or fills evidence.
    """
    if type(raw) is not bytes or type(turn) is not int or not 1<=turn<=3 or type(completed_tools) is not int or not 0<=completed_tools<=4:
        raise InvalidValue()
    try:
        value = _mapping(decode_content(raw,24576))
        if type(value.get('schema_version')) is not int or value['schema_version'] != 1:
            raise InvalidValue()
        if value.get('kind')=='TOOL':
            if set(value)!={'schema_version','kind','tools'} or turn==3:
                raise InvalidValue()
            tools=tuple(isolate_tool(v) for v in _items(value['tools'],1,2))
            if completed_tools+len(tools)>4: raise InvalidValue()
            return MappingProxyType({'schema_version':1,'kind':'TOOL','tools':tools})
        if value.get('kind')!='FINAL' or set(value)!={'schema_version','kind','actions'}:
            raise InvalidValue()
        actions=[]; seen=set(); created:dict[int,str]={}
        for raw_action in _items(value['actions'],0,8):
            action=_action(raw_action,created); local=cast(int,action['local_ref'])
            if local in seen: raise InvalidValue()
            seen.add(local); actions.append(action)
            kind={'REGISTER_SUBJECT':'SUBJECT','CREATE_MEMORY':'MEMORY','CREATE_RELATION':'RELATION','CREATE_GOAL':'GOAL'}.get(cast(str,action['action']))
            if kind is not None:created[local]=kind
        result=MappingProxyType({'schema_version':1,'kind':'FINAL','actions':tuple(actions)})
        encode_content(result,24576)
        return result
    except (KeyError,TypeError,ValueError,UnicodeError):
        raise InvalidValue() from None
