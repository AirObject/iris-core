"""Complete current SELF evidence issued by the native memory participant.

Selection does not retain sources or prevent deletion. Every selected revision,
source binding and required basis is checked again in the publishing transaction.
"""
from types import MappingProxyType
from typing import TYPE_CHECKING, cast
from companion_memory.persistence import UnitOfWork
from companion_memory.persistence.semantic_records import Record
from companion_memory.persistence.schema import InvalidValue
from companion_memory.persistence.owned_statements import OwnerFailure
from companion_memory.persistence.content_codec import encode_content
from .formats import record, sequence
if TYPE_CHECKING:
    from .transactions import MemoryTransactions


def self_related(memory: 'MemoryTransactions', uow: UnitOfWork, value: Record) -> bool:
    body=record(value['content'])
    if value['kind']=='MEMORY':
        subjects=cast(tuple[str,...],body['subject_ids'])
    else:
        subjects=tuple(cast(str,ref['id']) for ref in (record(body['from_ref']),record(body['to_ref'])) if ref['type']=='SUBJECT')
    return any((subject:=memory.subject(uow,sid)) is not None and subject['kind']=='SELF' for sid in subjects)


def evidence(memory: 'MemoryTransactions', uow: UnitOfWork, refs: tuple[Record,...],*,self_only:bool=True,allow_changed:bool=False) -> tuple[Record,...]:
    """Return complete legitimate sources without consulting historical bodies."""
    if len(refs)>16 or len({cast(str,ref['object_id']) for ref in refs})!=len(refs):raise InvalidValue()
    values=[]
    for ref in refs:
        current=memory.current(uow,cast(str,ref['object_id']))
        if current is None or current['revision']!=ref['revision'] or current['lifecycle']!='ACTIVE' or self_only and not self_related(memory,uow,current):
            raise OwnerFailure('PRECONDITION_FAILED','revision','REVISION_CONFLICT')
        links=memory.links(uow,cast(str,current['object_id']),cast(int,current['revision']))
        objects=[current];availability=[]
        for item in sequence(links['bases']):
            link=record(item);basis=memory.current(uow,cast(str,link['basis_id']))
            available=basis is not None and basis['lifecycle']=='ACTIVE' and basis['revision']==link['basis_revision']
            if not available and not allow_changed:raise OwnerFailure('PRECONDITION_FAILED','source','SOURCE_CHANGED')
            availability.append(MappingProxyType({'basis_id':link['basis_id'],'bound_revision':link['basis_revision'],
                'current_revision':None if basis is None else basis['revision'],'state':'DELETED' if basis is None else 'FORGOTTEN' if basis['lifecycle']=='FORGOTTEN' else 'CURRENT' if available else 'REVISED'}))
            if basis is not None and basis['lifecycle']=='ACTIVE':objects.append(basis)
        if len(objects)>9:raise OwnerFailure('RESOURCE_BUSY','material','CAPACITY_REACHED')
        sources={}
        for value in objects:
            current_links=memory.links(uow,cast(str,value['object_id']),cast(int,value['revision']))
            for raw in sequence(current_links['sources']):
                sid=cast(str,record(raw)['source_id'])
                row,source=memory.source(uow,sid)
                members=[]
                if source.get('format')=='FIXED_REVIEWED_TEXT_V1':
                    members.append(MappingProxyType({'event':source['event'],'interpretations':()}))
                else:
                    from companion_memory.ingress.content_storage import ContentIngressTransactions
                    if type(memory.sources) is not ContentIngressTransactions:raise InvalidValue()
                    for raw_member in sequence(source['ordered_members']):
                        payload=memory.sources.read_retained_member(uow,sid,cast(str,source['entry_id']),record(raw_member))
                        members.append(MappingProxyType({'event':payload.event,'interpretations':payload.interpretations}))
                sources[sid]=MappingProxyType({'source_id':sid,'references_revision':row['references_revision'],'digest':row['digest'],'body':source,'members':tuple(members)})
        roots=current_roots(memory,uow,current,allow_changed=allow_changed)
        item={'object':current,'links':links,'bases':tuple(objects[1:]),
            'sources':tuple(sources[sid] for sid in sorted(sources)),'independent_roots':roots}
        if allow_changed:item['basis_availability']=tuple(availability)
        values.append(MappingProxyType(item))
    result=tuple(values)
    encode_content(result,262144)
    return result


def current_roots(memory:'MemoryTransactions',uow:UnitOfWork,current:Record,*,allow_changed:bool) -> tuple[str,...]:
    """Resolve current direct roots, never count cached ancestry as live support.

    A bounded traversal fails as capacity rather than silently truncating a graph.
    The visited set closes cycles; equal direct message IDs remain one root.
    """
    pending=[current];visited=set();roots=set()
    while pending:
        value=pending.pop();oid=cast(str,value['object_id'])
        if oid in visited:continue
        if len(visited)>=64:raise OwnerFailure('RESOURCE_BUSY','material','CAPACITY_REACHED')
        visited.add(oid)
        links=memory.links(uow,oid,cast(int,value['revision']))
        for raw in sequence(links['sources']):
            link=record(raw)
            if link['link_role']!='DIRECT':continue
            memory.source(uow,cast(str,link['source_id']))
            roots.update(cast(str,record(a)['message_id']) for a in sequence(link['target_anchors']))
        for raw in sequence(links['bases']):
            link=record(raw);basis=memory.current(uow,cast(str,link['basis_id']))
            available=basis is not None and basis['lifecycle']=='ACTIVE'
            if (not available or basis is not None and basis['revision']!=link['basis_revision']) and not allow_changed:
                raise OwnerFailure('PRECONDITION_FAILED','source','SOURCE_CHANGED')
            if available and basis is not None:pending.append(basis)
    if len(roots)>8:raise OwnerFailure('RESOURCE_BUSY','material','CAPACITY_REACHED')
    return tuple(sorted(roots))


def same_evidence(left:tuple[Record,...],right:tuple[Record,...]) -> bool:
    """Holder counters belong to release plans, not to source text identity.

    All object/link revisions, actual payloads, digests and current roots remain
    exact. The application owner independently rechecks complete holder plans.
    """
    def semantic(items):
        return tuple(dict(item)|{'sources':tuple({k:v for k,v in record(source).items() if k!='references_revision'}
            for source in sequence(item['sources']))} for item in items)
    return semantic(left)==semantic(right)


async def current_basis_revisions(memory:'MemoryTransactions',ref:Record) -> bool:
    """Read current dependency revisions through the actual memory owner only.

    Persona history never supplies a missing object. Every traversal has the
    same finite object bound as the transactional evidence traversal.
    """
    from companion_memory.persistence.content_codec import decode_content
    from .formats import isolate_links
    information=memory.information
    if information is None:raise InvalidValue()
    pending=[ref];visited=set()
    while pending:
        expected=pending.pop();oid=cast(str,expected['object_id'])
        current=await information.index_current(oid)
        if current is None or current['revision']!=expected['revision'] or current['lifecycle']!='ACTIVE':return False
        if oid in visited:continue
        if len(visited)>=64:return False
        visited.add(oid)
        rows=await memory.rows.read('links_get',{'object_id':oid})
        if len(rows)!=1:raise InvalidValue()
        links=isolate_links(decode_content(cast(str,rows[0]['body']).encode(),2048),oid,cast(int,current['revision']))
        pending.extend(MappingProxyType({'object_id':record(raw)['basis_id'],'revision':record(raw)['basis_revision']}) for raw in sequence(links['bases']))
    return True


async def self_reference(memory: 'MemoryTransactions', value: Record) -> Record|None:
    """Bounded native selection; the publishing UoW repeats every identity check."""
    from .formats import isolate_subject
    from companion_memory.persistence.content_codec import decode_content
    body=record(value['content'])
    ids=cast(tuple[str,...],body['subject_ids']) if value['kind']=='MEMORY' else tuple(cast(str,r['id']) for r in (record(body['from_ref']),record(body['to_ref'])) if r['type']=='SUBJECT')
    for sid in ids:
        rows=await memory.rows.read('subjects_get',{'subject_id':sid})
        if not rows:continue
        subject=isolate_subject(decode_content(cast(str,rows[0]['body']).encode(),1024))
        if subject['instance_id']!=memory.instance_id or any(subject[k]!=rows[0][k] for k in ('subject_id','kind','revision','platform_id','external_subject_id')):raise InvalidValue()
        if subject['kind']=='SELF':return MappingProxyType({'object_id':value['object_id'],'revision':value['revision']})
    return None


def scan_self(memory: 'MemoryTransactions',uow:UnitOfWork,after:str):
    """One native page advances a fair cursor without imposing a SELF total cap."""
    information=memory.information
    if information is None:raise InvalidValue()
    raw=information._records.rows.stage('information_objects',uow,{'after':after,'limit':16})
    objects=tuple(memory.decode_current(row) for row in raw)
    refs=tuple(MappingProxyType({'object_id':value['object_id'],'revision':value['revision']}) for value in objects
        if value['lifecycle']=='ACTIVE' and self_related(memory,uow,value))
    cursor=cast(str,objects[-1]['object_id']) if len(objects)==16 else ''
    return refs,information.sequence(uow)['last_seq'],cursor


def eligible_self_page(memory:'MemoryTransactions',uow:UnitOfWork,after:str):
    """Exclude unavailable support explicitly; never trim a selected source body."""
    refs,watermark,cursor=scan_self(memory,uow,after)
    selected=[];accepted=[];excluded=[]
    for ref in refs:
        try:item=evidence(memory,uow,(ref,))
        except OwnerFailure as failure:
            if failure.code!='PRECONDITION_FAILED' or failure.reason not in ('SOURCE_CHANGED','REVISION_CONFLICT'):raise
            excluded.append(MappingProxyType({'object_id':ref['object_id'],'revision':ref['revision'],'reason':failure.reason}))
        else:accepted.append(ref);selected.extend(item)
    return tuple(accepted),tuple(selected),watermark,cursor,tuple(excluded)
