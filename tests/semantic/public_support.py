"""Public semantic fixture: exact reviewed material and original command ports."""
from types import MappingProxyType
import time
from companion_memory.persistence import Committed,Found
from companion_memory.persistence.semantic_records import identity
from companion_memory.persistence.semantic_records import isolate
from companion_memory.runtime.semantic_authorization import SemanticActivationAuthority,BINDING,USAGE_BINDING
from tests.semantic.test_semantic_host import make_host,opened,material


async def establish(root,port=1,*,connect=None,usage_only=False):
    host=make_host(root,port,usage_only=usage_only) if connect is None else make_host(root,port,connect=connect,usage_only=usage_only)
    result=await opened(host,'CREATE_NEW');assert type(result) is Found,result
    assert host.initial is not None and host.fixed is not None and host.stored is not None
    result=await host.initial.register_initial_self('initial-self','NO_PRESET','Synthetic fixture self.','SYNTHETIC_FIXTURE');assert type(result) is Committed,result
    result=await host.register_entry('entry-registration','entry','host','sample_platform','external-entry');assert type(result) is Committed,result
    fixed=host.fixed;claims=host.resources.review.claims
    config={'database_id':host.stored.database_id,'instance_id':'instance','snapshot_id':host.stored.snapshot_id}
    def envelope(kind,key,payload):return fixed.envelope(kind,key,MappingProxyType(payload),1)
    result=await fixed.begin(envelope('fixed_begin','begin',{'set_id':'fixed-set','config':config,
        **{k:claims[k] for k in ('manifest_digest','review_ref','review_digest')}}),time.monotonic()+5);assert type(result) is Committed,result
    for ordinal,member in enumerate(material()):
        result=await fixed.add_member(envelope('fixed_add_member','add:'+str(ordinal),{'set_id':'fixed-set','expected_revision':ordinal+1,**member}),time.monotonic()+5)
        assert type(result) is Committed,result
    result=await fixed.seal(envelope('fixed_seal','seal',{'set_id':'fixed-set','expected_revision':13}),time.monotonic()+5);assert type(result) is Committed,result
    result=await fixed.establish_one(envelope('fixed_establish','establish:0',{'set_id':'fixed-set','expected_revision':14,'ordinal':0,'expected_member_revision':1}),time.monotonic()+5)
    assert type(result) is Committed,result
    return host,identity('fixed-memory','instance','fixed-set','member:0')


def activate(host):
    """Grant only the exact nonsecret controlled fixture, never real facts."""
    assert host.stored is not None
    value={'format':'SEMANTIC_TRIAL_AUTH_V1','package_id':'fixture-package','set_id':'fixed-set','instance_id':'instance',
        'database_id':host.stored.database_id,'config_snapshot_id':host.stored.snapshot_id,
        'code_digest':'b'*64,'material_digest':'c'*64,'review_digest':'a'*64,'protocol_digest':'d'*64,'sdk_digest':'e'*64,
        'decision_ref':'fixture-decision','execution':'CONTROLLED','account_evidence_ref':'fixture-account','input_evidence_ref':'fixture-input',
        'server_evidence_ref':'fixture-server','document_ids':tuple(identity('fixed-memory','instance','fixed-set',m['member_id']) for m in material()),
        'queries':tuple({'query_id':'query:'+str(n),'text':'query '+str(n)} for n in range(6)),'expires_at':time.time_ns()//1000+3600000000}
    usage_only=host.configuration.text.record('provider.embedding_transport')['v']==2
    if usage_only:value.update(format='SEMANTIC_TRIAL_AUTH_V2',verification_mode='USER_ALLOCATED_USAGE_TRIAL')
    expected=isolate(USAGE_BINDING if usage_only else BINDING,value,1048576);grant=SemanticActivationAuthority(lambda received:received==expected).grant(value)
    host.bind_activation(grant);return grant
