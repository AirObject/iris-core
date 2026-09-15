"""Maximum single carriers and malformed-vector rejection without provider I/O.

The 4096-record case checks only a binary file format. It neither creates formal
objects nor runs the capacity profile or its long dispatch qualification.
"""
import io
import json
import math
from pathlib import Path
import struct
import tempfile
import unittest
from companion_memory.provider.embedding_protocol import parse_response, request_bytes
from companion_memory.provider.values import as_record,freeze,dump,InvalidData
from companion_memory.retrieval.semantic_binary import (VectorMember,VectorHeader,write_generation,read_members,
    vector_bytes,cosine,scan,InvalidVectorFile,FILE_LIMIT,RECORD_BYTES)
from companion_memory.configuration.semantic_resolution import resolve_semantic_configuration,SemanticConfigurationOk
from companion_memory.configuration.text_resolution import resolve_text_learning_configuration,TextConfigurationOk
from companion_memory.configuration.semantic_codec import candidate_values
from companion_memory.persistence.schema import encode_value
from tests.semantic.configuration_support import inputs,candidate
from tests.text_learning.configuration_support import inputs as old_inputs

SPACE='embedding-space:'+'a'*64
GENERATION='semantic-generation:'+'b'*64
ARTIFACT='embedding-artifact:'+'c'*64


def response(vector):
    return {'id':'request','created':1,'model':'doubao-embedding-vision','object':'list','data':[{'object':'embedding','index':0,'embedding':vector}],
        'usage':{'prompt_tokens':8192,'total_tokens':8192}}


def parsed(value):
    return parse_response(json.dumps(value,separators=(',',':')).encode(),space_id=SPACE,expected_models=('doubao-embedding-vision',))


class EncodingTests(unittest.TestCase):
    def test_largest_finite_coordinates_full_result_and_escaped_request(self):
        vector=[-float.fromhex('0x1.fffffffffffffp+1023')]*1024
        result=parsed(response(vector));self.assertEqual(result.error,'NONE');self.assertIsNotNone(result.result)
        assert result.result is not None and result.usage is not None
        self.assertLessEqual(len(dump(result.result,40960).encode()),40960)
        self.assertEqual(result.usage.estimate(8192,700000),5735)
        raw=request_bytes(as_record(freeze({'texts':['\x00'*8192],'purpose':'DOCUMENT','dimensions':1024},65536)))
        self.assertLessEqual(len(raw),65536);self.assertEqual(json.loads(raw)['input'],['\x00'*8192])
        print(json.dumps({'normalized_max_coordinate_bytes':len(dump(result.result,40960).encode()),'escaped_document_request_bytes':len(raw)}))

    def test_strict_response_and_independent_usage(self):
        for coordinate in (True,float('nan'),float('inf')):
            v=response([coordinate]+[1.0]*1023)
            self.assertIsNone(parsed(v).result)
        for count in (0,1023,1025):self.assertIsNone(parsed(response([1.0]*count)).result)
        self.assertIsNone(parsed(response([0.0]*1024)).result)
        v=response([1]*1024);v['data'][0]['index']=True
        result=parsed(v);self.assertIsNone(result.result);self.assertIsNotNone(result.usage)
        v=response([1]*1024);v['usage']['extra']=0
        result=parsed(v);self.assertIsNotNone(result.result);self.assertIsNone(result.usage);self.assertEqual(result.error,'USAGE')
        raw=json.dumps(response([1]*1024)).encode().replace(b'"created": 1',b'"created": 1,"created": 1')
        self.assertEqual(parse_response(raw,space_id=SPACE,expected_models=('doubao-embedding-vision',)).error,'PROTOCOL')
        raw=json.dumps(response([1]*1024)).encode().replace(b'"embedding": [1',b'"embedding": [1.000000000000000000000000000000000000001',1)
        self.assertEqual(parse_response(raw,space_id=SPACE,expected_models=('doubao-embedding-vision',)).error,'PROTOCOL')

    def test_binary_exact_layout_empty_pages_and_maximum_single_file(self):
        vector=vector_bytes((1.0,-0.0)+tuple(0.0 for _ in range(1022)))
        for count in (0,1,8,9,4096):
            with self.subTest(count=count),tempfile.TemporaryFile('w+b') as file:
                header=write_generation(file,SPACE,GENERATION,4096,(VectorMember(f'object:{i:04d}',1,1,1,ARTIFACT,vector) for i in range(count)),lambda:None)
                self.assertEqual(file.seek(0,2),4096+count*8384)
                self.assertEqual(header.encode()[192:],bytes(3904));self.assertEqual(struct.unpack_from('<I',header.encode(),28)[0],(count+7)//8)
                self.assertEqual(sum(1 for _ in read_members(file,header,lambda:None)),count)
                if count==4096:self.assertEqual(file.seek(0,2),FILE_LIMIT)
        member=VectorMember('x'*128,1,1,1,ARTIFACT,vector)
        self.assertEqual(len(member.encode()),RECORD_BYTES);self.assertEqual(VectorMember.decode(member.encode()),member)
        self.assertEqual(struct.unpack_from('<Q',member.encode(),192+8)[0],1<<63)
        print(json.dumps({'maximum_file_bytes':FILE_LIMIT,'record_bytes':RECORD_BYTES,'maximum_pages':512}))

    def test_binary_tampering_never_returns_candidates(self):
        vector=vector_bytes(tuple(1.0 for _ in range(1024)));file=io.BytesIO()
        header=write_generation(file,SPACE,GENERATION,1,[VectorMember('object',1,1,1,ARTIFACT,vector)],lambda:None)
        raw=file.getvalue()
        for offset in (0,188,192,4096+130,4096+188,4096+192):
            bad=bytearray(raw);bad[offset]^=1
            with self.subTest(offset=offset),self.assertRaises(InvalidVectorFile):scan(io.BytesIO(bad),header,vector,lambda:None,limit=64,minimum_millionths=700000)
        with self.assertRaises(InvalidVectorFile):scan(io.BytesIO(raw+b'x'),header,vector,lambda:None,limit=64,minimum_millionths=700000)
        maximum=vector_bytes(tuple(float.fromhex('0x1.fffffffffffffp+1023') for _ in range(1024)))
        self.assertAlmostEqual(cosine(maximum,maximum),1.0)

    def test_complete_configuration_tiers_and_old_mismatch(self):
        with tempfile.TemporaryDirectory() as root:
            for offline in (True,False):
                value,supplied=candidate(root,offline=offline);encoded=candidate_values(value)
                self.assertEqual(sum(len(d['entries']) for d in encoded['domains']),124)
                self.assertEqual(len(encoded['domains']),6)
                self.assertNotIsInstance(resolve_text_learning_configuration(*supplied),TextConfigurationOk)
                supplied[5]['explicit_values']['retrieval.semantic_storage']['normal_operation_limit']=1000 if offline else 90000
                self.assertNotIsInstance(resolve_semantic_configuration(*supplied),SemanticConfigurationOk)
            self.assertNotIsInstance(resolve_semantic_configuration(*old_inputs(Path(root))),SemanticConfigurationOk)
