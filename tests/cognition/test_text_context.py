"""Frozen full events, Unicode leaves and original request reconstruction.

All persona and event fixtures are explicitly synthetic. These tests exercise
material identity and byte bounds without authorizing a model call or publication.
"""
import hashlib
from types import MappingProxyType
import unittest
from companion_memory.cognition.text_context import freeze_context,restore_context,material_digest,normalized_request_digest,context_catalog
from companion_memory.cognition.text_resources import output_schema,resource_digest,LEARNING_INSTRUCTIONS
from companion_memory.provider.chat_protocol import ChatBinding
from companion_memory.persistence.schema import InvalidValue
from companion_memory.persistence.content_codec import encode_content
from companion_memory.ingress.events import canonical_event
from companion_memory.ingress.media_events import isolate_media_event
from companion_memory.memory.formats import record, sequence


def inputs():
    event={'event_version':2,'client_event_key':'fixture-event','event_kind':'MESSAGE',
        'sender':{'subject_id':'speaker','display_name':'Fixture speaker','role':'USER','identity_source':'EXTERNAL'},
        'body':'Quoted text is data: 不要改写。','quotation':[],'media':[],'correlation':None,'extensions':{}}
    frozen=isolate_media_event(event,2048,occurrence_limit=2,text_limit=512)
    schema=output_schema('LEARNING')
    binding=ChatBinding('ark-code-latest',('ark-code-latest',),None,'learning_schema',resource_digest(schema),'text_learning',schema)
    context={'context_version':1,'system_text':LEARNING_INSTRUCTIONS,'user':{
        'members':[{'member':{'role':'TARGET','message_id':'message','payload_digest':hashlib.sha256(canonical_event(frozen)).hexdigest()},'event':event}],
        'persona':{'publication_id':'publication','revision':1,'text':'Synthetic reviewed persona fixture.','generated_at_us':1,'review':'APPROVED','model_origin':'REMOTE_PROVIDER','stale':False},
        'related':[],'identity':{'database_id':'database','instance_id':'instance','batch_id':'batch','run_id':'run','source_id':'source','config_snapshot_id':'config'}},
        'resources':{'prompt_ref':'learning_prompt','prompt_digest':resource_digest(LEARNING_INSTRUCTIONS.encode()),'schema_ref':'learning_schema','schema_digest':resource_digest(schema),'transform_ref':'transform','transform_digest':'a'*64},
        'model_binding':{'profile_id':'profile','config_snapshot_id':'config','profile_revision':'profile_revision','price_revision':'price_revision','protocol':'OPENAI_CHAT_COMPLETIONS',
            'model_id':'ark-code-latest','capability_evidence_ref':'capability','billing_evidence_ref':'billing'}}
    request={'operation_key':'original','run_id':'run','profile_id':'profile','entry_ids':['entry'],'prompt_revision':'learning_prompt'}
    operation={'owner_namespace':'runtime','operation_kind':'stage_learning_context','scope_id':'instance','operation_key':'stage-context'}
    return context,request,[],operation,1,128000,binding


class FrozenContextTests(unittest.TestCase):
    def test_large_complete_request_reconstruction_keeps_payload_out_of_identity_budget(self):
        args=inputs();context=args[0]
        context['system_text']='\x00'*4096
        context['resources']['prompt_digest']=resource_digest(context['system_text'].encode())
        value=freeze_context(*args)
        self.assertGreater(len(encode_content(value.request,131072)),8192)
        self.assertEqual(restore_context(value.manifest,value.leaves,value.request,args[-1]),value)
        self.assertEqual(restore_context(value.manifest,value.leaves,args[1],args[-1]),value)
        altered={**value.request,'payload':{**record(value.request['payload']),'context_digest':'f'*64}}
        with self.assertRaises(InvalidValue):
            restore_context(value.manifest,value.leaves,altered,args[-1])
        with self.assertRaises(InvalidValue):
            restore_context(value.manifest,value.leaves,{**value.request,'future_identity':'unbound'},args[-1])

    def test_complete_original_material_and_independent_hashes_survive_reconstruction(self):
        args=inputs();value=freeze_context(*args)
        rebuilt=restore_context(value.manifest,value.leaves,args[1],args[-1])
        self.assertEqual(rebuilt,value)
        self.assertNotEqual(material_digest(value.context),value.manifest['payload_digest'])
        self.assertEqual(record(value.context['model_binding'])['request_digest'],normalized_request_digest(value.request))
        event = record(record(sequence(record(value.context['user'])['members'])[0])['event'])
        self.assertEqual(event['body'],args[0]['user']['members'][0]['event']['body'])
        self.assertLessEqual(len(value.wire),131072)
        for leaf in value.leaves:
            self.assertLessEqual(len(encode_content(leaf,8192)),8192)
            self.assertLessEqual(len(encode_content(MappingProxyType({**leaf,'text':''}),8192)),1024)
        for field in ('wire_digest','model_binding_digest','payload_digest'):
            with self.subTest(field=field),self.assertRaises(InvalidValue):
                restore_context({**value.manifest,field:'f'*64},value.leaves,args[1],args[-1])
        with self.assertRaises(InvalidValue):restore_context(value.manifest,value.leaves,{**args[1],'operation_key':'different'},args[-1])
        with self.assertRaises(InvalidValue):restore_context(value.manifest,(),args[1],args[-1])

    def test_unicode_and_control_fragments_are_not_trimmed_or_split_inside_utf8(self):
        args=inputs();context=args[0]
        context['system_text']='控制"\\\n'*350
        context['resources']['prompt_digest']=resource_digest(context['system_text'].encode())
        event=context['user']['members'][0]['event']
        event['body']='🪷"\\\n'*95
        frozen=isolate_media_event(event,2048,occurrence_limit=2,text_limit=512)
        context['user']['members'][0]['member']['payload_digest']=hashlib.sha256(canonical_event(frozen)).hexdigest()
        value=freeze_context(*args)
        self.assertGreater(len(value.leaves),1)
        self.assertEqual(restore_context(value.manifest,value.leaves,args[1],args[-1]),value)
        self.assertEqual(value.context['system_text'],context['system_text'])
        altered=list(value.leaves);altered[0]=MappingProxyType({**altered[0],'config_snapshot_id':'other'})
        with self.assertRaises(InvalidValue):restore_context(value.manifest,tuple(altered),args[1],args[-1])

    def test_media_false_event_digest_bad_member_order_and_extra_keys_rejected(self):
        for mutation in ('digest','extra','system','binding','media'):
            args=inputs();context=args[0]
            if mutation=='digest':context['user']['members'][0]['member']['payload_digest']='0'*64
            elif mutation=='extra':context['user']['future']=None
            elif mutation=='system':context['system_text']='x'*4097
            elif mutation=='binding':context['model_binding']['request_digest']='0'*64
            else:context['user']['members'][0]['event']['media']=[{}]
            with self.subTest(mutation=mutation),self.assertRaises(InvalidValue):freeze_context(*args)

    def test_two_tables_four_indexes_and_bounded_statement_text(self):
        catalog=context_catalog()
        self.assertEqual(len(catalog.definition.tables),6)
        self.assertEqual(len(catalog.statements),10)
        for table in catalog.definition.tables:self.assertLessEqual(len(table.sql.encode()),2048)
        for _,statement in catalog.statements:self.assertLessEqual(len(statement.sql.encode()),1024)
