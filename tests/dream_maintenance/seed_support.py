"""Reviewed synthetic fixed material established through its four native commands."""
from types import MappingProxyType
import time
from companion_memory.persistence import Committed
from tests.semantic.test_semantic_host import material


def self_material():
    """Explicit controlled SELF setting, also one of the reviewed semantic seeds."""
    from hashlib import sha256
    from companion_memory.persistence.content_codec import encode_content,decode_content
    from companion_memory.memory.formats import isolate_object
    values=list(material());member=values[0]
    body=decode_content(member['memory_json'].encode(),4096)
    event=decode_content(member['event_json'].encode(),2048)
    if type(body) is not dict or type(event) is not dict:raise AssertionError('Fixture shape')
    body['content']['subject_ids']=['self']
    body['content']['body']='合成试验中的表达应明确区分观察、转述与推测。'
    event['body']=body['content']['body']
    memory_json=encode_content(isolate_object(body,text_format=True),4096).decode()
    from companion_memory.ingress.events import isolate_event
    event_json=encode_content(isolate_event(event,2048),2048).decode()
    values[0]=MappingProxyType(dict(member)|{'memory_json':memory_json,'event_json':event_json,
        'content_digest':sha256(encode_content((event_json,memory_json),8192)).hexdigest()})
    return tuple(values)


async def establish_memory(host,*,with_self=False):
    fixed=host.fixed;stored=host.stored
    if fixed is None or stored is None:raise AssertionError('Missing native owners')
    claims=host.resources.review.claims
    def env(kind,key,payload):return fixed.envelope(kind,key,MappingProxyType(payload),1)
    config={'database_id':stored.database_id,'instance_id':'instance','snapshot_id':stored.snapshot_id}
    operations=[('fixed_begin','begin',{'set_id':'fixed-set','config':config,**{k:claims[k] for k in ('manifest_digest','review_ref','review_digest')}})]
    operations.extend(('fixed_add_member','add-'+str(index),{'set_id':'fixed-set','expected_revision':index+1,**member}) for index,member in enumerate(self_material() if with_self else material()))
    operations.extend((('fixed_seal','seal',{'set_id':'fixed-set','expected_revision':13}),('fixed_establish','establish',{'set_id':'fixed-set','expected_revision':14,'ordinal':0,'expected_member_revision':1})))
    methods={'fixed_begin':fixed.begin,'fixed_add_member':fixed.add_member,'fixed_seal':fixed.seal,'fixed_establish':fixed.establish_one}
    for kind,key,payload in operations:
        result=await methods[kind](env(kind,key,payload),time.monotonic()+5)
        if type(result) is not Committed:raise AssertionError((kind,result))
    memory=host.assembly.memory.information
    if memory is None:raise AssertionError('Missing native memory')
    page=await memory.current_page('',1)
    if len(page)!=1:raise AssertionError('Expected one current memory')
    return page[0]
