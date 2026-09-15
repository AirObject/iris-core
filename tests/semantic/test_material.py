"""Complete in-memory handoff, input and artifact carrier qualification.

No provider port, ledger, network transport or durable completion is used here.
These tests prove byte-preserving codecs, not the public settlement workflow.
"""
from hashlib import sha256
import json
import math
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from companion_memory.provider.embedding_protocol import parse_response
from companion_memory.provider.embedding_material import split_handoff,restore_handoff
from companion_memory.provider.values import freeze,as_record,InvalidData,dump
from companion_memory.persistence.schema import InvalidValue,freeze_value,encode_value
from companion_memory.persistence.semantic_records import make_leaf
from companion_memory.retrieval.semantic_payloads import input_leaves,restore_input,artifact_leaves,restore_vector
from companion_memory.retrieval.semantic_binary import vector_bytes
from companion_memory.configuration.semantic_codec import candidate_values
from companion_memory.configuration.semantic_resolution import resolve_semantic_configuration,SemanticConfigurationOk,SemanticConfigurationErr
from companion_memory.configuration.content_codec import decode_content_entry
from tests.runtime.configuration_support import registry
from tests.semantic.configuration_support import candidate
from tests.semantic.test_encoding import SPACE,ARTIFACT

MODEL='doubao-embedding-vision'


class SemanticMaterialTests(unittest.TestCase):
    usage_only=False
    def test_maximum_handoff_all_leaves_and_exact_artifact_round_trip(self):
        vector=(-float.fromhex('0x1.fffffffffffffp+1023'),)*1023+(-0.0,)
        native=as_record(freeze({'vectors':(vector,),'dimensions':1024,'space_id':SPACE,'model_id':MODEL,'input_items':1},40960))
        args={'handoff_id':'handoff','request_id':'request','attempt_id':'attempt','space_id':SPACE,'model_id':MODEL}
        encoded=split_handoff(native,**args)
        self.assertEqual(len(encoded.leaves),10)
        self.assertEqual(restore_handoff(encoded.payload,encoded.leaves,**args),native)
        stored=artifact_leaves(ARTIFACT,native,space_id=SPACE,model_id=MODEL)
        original=vector_bytes(vector)
        self.assertEqual(restore_vector(ARTIFACT,sha256(original).hexdigest(),stored),original)
        print({'proof':'CODEC_ONLY','handoff_payload_bytes':len(dump(native,40960).encode()),'handoff_leaves':len(encoded.leaves),
            'handoff_largest_complete_leaf_bytes':max(len(encode_value(v,8192)) for v in encoded.leaves),'vector_bytes':len(original)})
        for leaves in (encoded.leaves[:-1],tuple(reversed(encoded.leaves)),encoded.leaves+(encoded.leaves[0],)):
            with self.assertRaises((InvalidData,InvalidValue)):restore_handoff(encoded.payload,leaves,**args)
        with self.assertRaises((InvalidData,InvalidValue)):restore_handoff(encoded.payload,encoded.leaves,**(args|{'attempt_id':'other'}))
        for leaves in (stored[:1],tuple(reversed(stored)),stored+(stored[0],)):
            with self.assertRaises(InvalidValue):restore_vector(ARTIFACT,sha256(original).hexdigest(),leaves)
        with self.assertRaises(InvalidValue):restore_vector(ARTIFACT,'0'*64,stored)

    def test_exact_utf8_material_can_cross_leaf_boundary(self):
        text='a'*3071+'海'+'é'*2559
        self.assertEqual(len(text.encode()),8192)
        leaves=input_leaves('work',text,'DOCUMENT')
        self.assertEqual(len(leaves),3)
        self.assertEqual(restore_input('work','DOCUMENT',8192,sha256(text.encode()).hexdigest(),leaves),text)
        self.assertEqual(restore_input('query','QUERY',4,sha256(b' x\n ').hexdigest(),input_leaves('query',' x\n ','QUERY')),' x\n ')
        for bad in (leaves[:-1],tuple(reversed(leaves)),leaves+(leaves[0],)):
            with self.assertRaises(InvalidValue):restore_input('work','DOCUMENT',8192,sha256(text.encode()).hexdigest(),bad)
        with self.assertRaises(InvalidValue):input_leaves('query','x'*513,'QUERY')
        with self.assertRaises(InvalidValue):restore_input('work','QUERY',8192,sha256(text.encode()).hexdigest(),leaves)

    def test_large_wire_integer_coordinate_is_not_a_usage_integer(self):
        value={'id':'response','created':1,'object':'list','model':MODEL,'data':[{'object':'embedding','index':0,'embedding':[10**30]+[0]*1023}],
            'usage':{'prompt_tokens':1,'total_tokens':1}}
        result=parse_response(json.dumps(value).encode(),space_id=SPACE,expected_models=(MODEL,))
        self.assertEqual(result.error,'NONE');self.assertIsNotNone(result.result)
        value['usage']={'prompt_tokens':10**30,'total_tokens':10**30}
        result=parse_response(json.dumps(value).encode(),space_id=SPACE,expected_models=(MODEL,))
        self.assertEqual(result.error,'USAGE');self.assertIsNotNone(result.result);self.assertIsNone(result.usage)

    def test_complete_inherited_metadata_limit_survives_six_new_keys(self):
        from companion_memory.configuration.semantic_schema import semantic_definitions
        added={d['key'] for d in semantic_definitions()}
        with TemporaryDirectory() as directory:
            value,supplied=candidate(Path(directory),offline=False,usage_only=self.usage_only);encoded=candidate_values(value)
            bodies={entry['parameter_key']:entry['body'] for domain in encoded['domains'] for entry in domain['entries']}
            remaining=524288-sum(len(body.encode()) for key,body in bodies.items() if key not in added)
            changed=[]
            for domain in (supplied[0],supplied[1],supplied[3],supplied[2][0]):
                definitions=[]
                for declaration in domain['registry'].list_definitions():
                    raw=decode_content_entry(bodies[declaration.key])[0]
                    amount=min(remaining,8192-len(bodies[declaration.key].encode()))
                    raw['description']+='é'*(amount//2)+'x'*(amount%2);remaining-=amount;definitions.append(raw)
                domain['registry']=registry(definitions);changed.append((domain,definitions))
            self.assertEqual(remaining,0)
            result=resolve_semantic_configuration(*supplied);self.assertIs(type(result),SemanticConfigurationOk,result)
            assert type(result) is SemanticConfigurationOk
            encoded=candidate_values(result.value)
            sizes={entry['parameter_key']:len(entry['body'].encode()) for domain in encoded['domains'] for entry in domain['entries']}
            self.assertEqual(len(sizes),124);self.assertEqual(sum(size for key,size in sizes.items() if key not in added),524288)
            self.assertLessEqual(sum(sizes.values()),573440)
            from companion_memory.persistence._codec import assembly_value
            from companion_memory.runtime.semantic_host import SemanticHost
            from tests.semantic.test_semantic_host import make_host,opened
            import asyncio
            maximum=result.value
            async def initialize_maximum():
                template=make_host(Path(directory).resolve(),1,usage_only=self.usage_only)
                host=SemanticHost(maximum,template.resources)
                try:
                    from companion_memory.persistence import Found
                    ready=await opened(host,'CREATE_NEW');self.assertIs(type(ready),Found,ready)
                    self.assertIsNotNone(host.stored)
                    assert host.embedding is not None
                    self.assertEqual(host.embedding.executions,0)
                    static_bytes=len(assembly_value(host.combination.repositories,host.combination.commands,assembly_format='ASYNC_SEMANTIC_V1'))
                    self.assertLessEqual(static_bytes,4194304)
                    print({'proof':'PUBLIC_MAXIMUM_INHERITED_CONFIGURATION_INITIALIZATION','static_bytes':static_bytes,
                        'repositories':len(host.combination.repositories),'commands':len(host.combination.commands),'keys':len(sizes),'real_sends':0})
                finally:self.assertTrue(await host.close())
            asyncio.run(initialize_maximum())
            print({'proof':'COMPLETE_CONFIGURATION_VALUES_ONLY','entries':len(sizes),'body_bytes':sum(sizes.values()),'inherited_body_bytes':524288,
                'proof_scope':'COMPLETE_CONFIGURATION_ENCODING_ONLY'})
            domain,definitions=changed[-1];definitions[-1]['description']+='x';domain['registry']=registry(definitions)
            self.assertIs(type(resolve_semantic_configuration(*supplied)),SemanticConfigurationErr)
