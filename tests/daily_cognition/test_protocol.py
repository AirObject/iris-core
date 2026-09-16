"""Full daily wire, output and liability checks with no transport or credentials."""
from hashlib import sha256
import json
from types import MappingProxyType
from typing import cast
from companion_memory.configuration import PresentValue
import unittest
from companion_memory.cognition.daily_resources import output_schema,prompt_resource
from companion_memory.provider.daily_protocol import DailyChatBinding,encode_daily_request,decode_daily_response
from companion_memory.provider.daily_handoff import split,restore
from companion_memory.provider.text_accounting import normalize,liability
from companion_memory.provider.daily_stored_schema import validate
from companion_memory.provider.values import as_record,freeze,InvalidData
from companion_memory.persistence.schema import InvalidValue
from .configuration_support import candidate
from pathlib import Path
from tempfile import TemporaryDirectory


def binding(role='LEARNING',model='deepseek-flash'):
    schema=output_schema(role);prompt=prompt_resource(role)
    return DailyChatBinding(role,model,role.lower()+'_schema',sha256(schema).hexdigest(),schema,sha256(prompt).hexdigest(),prompt)


def response(output,*,model='deepseek-flash',finish='stop',extra=None,usage=True):
    result={'id':'completion_1','object':'chat.completion','created':1,'model':model,'system_fingerprint':'fixture',
        'choices':[{'index':0,'message':{'role':'assistant','content':json.dumps(output,ensure_ascii=False)},'finish_reason':finish,'logprobs':None}]}
    if usage:result['usage']={'prompt_tokens':9,'completion_tokens':3,'total_tokens':12,'prompt_cache_hit_tokens':4,'prompt_cache_miss_tokens':5}
    if extra:result.update(extra)
    return json.dumps(result,ensure_ascii=False).encode()

class DailyProtocolTests(unittest.TestCase):
    def test_all_real_prompt_schema_resources_and_complete_wire_limits(self):
        for role in ('LEARNING','GOAL_DEDUP','PERSONA','MEDIA'):
            resource=binding(role);self.assertLessEqual(len(resource.schema_bytes),40960)
            if role=='MEDIA':continue
            body=json.loads(encode_daily_request(resource,'{"source":"untrusted"}'))
            self.assertEqual(set(body),{'model','messages','max_tokens','stream','thinking','response_format'})
            self.assertEqual([m['role'] for m in body['messages']],['system','user'])
            self.assertIn(resource.schema_bytes.decode(),body['messages'][0]['content'])
            self.assertEqual(body['max_tokens'],{'LEARNING':4096,'GOAL_DEDUP':512,'PERSONA':2048}[role])
            self.assertLessEqual(len(encode_daily_request(resource,'"'*262144)),1048576)
        self.assertGreater(len(output_schema('LEARNING')),4096)

    def test_full_output_handoff_missing_reordered_substituted_and_unknown_usage(self):
        resource=binding();raw=response({'schema_version':1,'kind':'FINAL','actions':[]})
        observed=decode_daily_response(raw,resource);self.assertEqual(observed.outcome,'SUCCEEDED')
        stored=split(observed.result,resource,handoff_id='h',request_id='r',attempt_id='a')
        self.assertEqual(restore(stored.payload,stored.leaves,resource,handoff_id='h',request_id='r',attempt_id='a'),observed.result)
        with self.assertRaises((InvalidData,InvalidValue)):restore(stored.payload,(),resource,handoff_id='h',request_id='r',attempt_id='a')
        with self.assertRaises((InvalidData,InvalidValue)):restore(stored.payload,stored.leaves,resource,handoff_id='h',request_id='other',attempt_id='a')
        for finish,state in (('length','OUTPUT_LIMIT'),('content_filter','OTHER_REFUSAL')):
            failed=decode_daily_response(response({'schema_version':1},finish=finish),resource)
            self.assertEqual(failed.outcome,state);self.assertIsNone(failed.result);self.assertTrue(failed.usage.billing_covered)
        self.assertEqual(decode_daily_response(response({},model='other'),resource).outcome,'MODEL_BINDING_MISMATCH')
        self.assertFalse(decode_daily_response(response({},usage=False),resource).usage.billing_covered)
        self.assertEqual(decode_daily_response(response({},extra={'input_sensitive':True}),resource).outcome,'INVALID_RESPONSE')

    def test_deepseek_image_usage_keeps_cache_partition_and_partial_money_hold(self):
        with TemporaryDirectory() as directory:
            configuration,_=candidate(Path(directory));values={entry.definition.key:entry.state.value for entry in configuration.foundation.list_entries() if type(entry.state) is PresentValue}
            account=as_record(freeze(cast(tuple,values['provider.accounts'])[0],8192,owned=True))
            profile=next(as_record(freeze(p,8192,owned=True)) for p in cast(tuple,values['provider.profiles']) if p['material_role']=='MEDIA')
            reserve,_=liability(account,profile)
            for present in (True,False):
                observed=decode_daily_response(response({'schema_version':1,'text':'two shapes'},usage=present),binding('MEDIA'))
                metering=normalize(observed.usage,account,profile,reserve)
                self.assertEqual(metering['format_version'],4)
                self.assertEqual(metering['held_atoms'],0 if present else reserve)
                self.assertEqual(metering['cost_complete'],present)
                attempt=as_record(freeze({'object_id':'a','revision':1,'request_id':'r','ordinal':1,'state':'PREPARED','logical_outcome':None,
                    'account_id':account['account_id'],'profile_id':profile['profile_id'],'capability':'MEDIA_UNDERSTANDING','wire_protocol':'DEEPSEEK_IMAGE_JSON_V1',
                    'execution_owner_id':'worker','created_at':'2026-09-15T00:00:00.000000+00:00','updated_at':'2026-09-15T00:00:00.000000+00:00',
                    'adapter_duration_ms':None,'handoff_id':None,'confirmed_started':None,'ever_unknown':False,'first_error':None,'terminal_error':None,
                    'usage':metering,'result_fingerprint':None,'evidence_revision':0},8192,owned=True))
                validate('attempts',attempt)
