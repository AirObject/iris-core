"""DeepSeek closed wire, token partition, configuration and compatibility checks.

Every response and credential is synthetic. These tests cannot read preparation
files or contact a supplier, and passing them grants no live trial authorization.
"""
import copy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from companion_memory.cognition.text_resources import output_schema, resource_digest, prompt_resource, render_learning_instructions, learning_authorizations
from companion_memory.provider.chat_protocol import ChatBinding, decode_response, encode_request
from companion_memory.provider.text_accounting import liability, normalize
from companion_memory.provider.text_stored_schema import validate_usage
from companion_memory.provider.values import as_record, freeze, InvalidData
from companion_memory.configuration.text_resolution import resolve_text_learning_configuration, TextConfigurationOk
from .deepseek_configuration import configuration
from .minimax_configuration import configuration as minimax
from .materials import SUBJECTS, WORLDS


def envelope(content='{"schema_version":1,"memories":[]}'):
    """Documented nonthinking envelope with independent cache alias observations."""
    return {'id':'deepseek-response','object':'chat.completion','created':1,'model':'deepseek-flash',
        'system_fingerprint':'fp-synthetic','choices':[{'index':0,'finish_reason':'stop','logprobs':None,
            'message':{'role':'assistant','content':content}}],
        'usage':{'prompt_tokens':100,'completion_tokens':50,'total_tokens':150,
            'prompt_cache_hit_tokens':20,'prompt_cache_miss_tokens':80,'prompt_tokens_details':{'cached_tokens':20}}}


def binding():
    schema=output_schema('LEARNING')
    return ChatBinding('deepseek-flash',('deepseek-flash',),None,'schema',resource_digest(schema),'text_learning',schema)


class ProtocolTests(unittest.TestCase):
    def test_exact_wire_and_complete_json_rejection(self):
        b=binding()
        wire=json.loads(encode_request({'format_version':2,'messages':[{'role':'SYSTEM','text':'Return JSON.'},{'role':'USER','text':'Frozen input.'}],
            'schema_ref':b.schema_ref,'schema_digest':b.schema_digest,'output_tokens':2048,'reservation_input_bound':1048576,'context_digest':'a'*64},b))
        self.assertEqual(set(wire),{'model','messages','max_tokens','stream','thinking','response_format'})
        self.assertEqual(wire['model'],'deepseek-flash');self.assertEqual(wire['max_tokens'],2048)
        self.assertEqual(wire['thinking'],{'type':'disabled'});self.assertEqual(wire['response_format'],{'type':'json_object'})
        self.assertIs(wire['stream'],False);self.assertEqual([m['role'] for m in wire['messages']],['system','user'])
        self.assertIn(b.schema_bytes.decode(),wire['messages'][0]['content'])
        for content in ('',' ','```json\n{}\n```','text {}','{} {}','{"x":1,"x":2}','{"x":','[]'):
            self.assertEqual(decode_response(json.dumps(envelope(content)).encode(),b).outcome,'INVALID_RESPONSE',content)
        self.assertEqual(decode_response(json.dumps(envelope()).encode(),b).outcome,'SUCCEEDED')

    def test_envelope_identity_and_known_terminal_failures(self):
        for reason,expected in (('length','OUTPUT_LIMIT'),('content_filter','OTHER_REFUSAL'),('tool_calls','INVALID_RESPONSE'),
                ('insufficient_system_resource','INVALID_RESPONSE'),('aborted','INVALID_RESPONSE'),('unknown','INVALID_RESPONSE')):
            value=envelope();value['choices'][0]['finish_reason']=reason
            result=decode_response(json.dumps(value).encode(),binding())
            self.assertEqual(result.outcome,expected);self.assertTrue(result.usage.billing_covered)
        for section,key,value in (('root','model','other'),('root','base_resp',{}),('root','service_tier','default'),
                ('message','reasoning_content','thinking'),('message','name','agent'),('choice','moderation_hit_type','x')):
            raw=envelope();target=raw if section=='root' else raw['choices'][0] if section=='choice' else raw['choices'][0]['message'];target[key]=value
            self.assertEqual(decode_response(json.dumps(raw).encode(),binding()).outcome,
                'MODEL_BINDING_MISMATCH' if key=='model' else 'INVALID_RESPONSE')

    def test_usage_partition_alias_missing_and_extensions(self):
        for update in ({'prompt_cache_miss_tokens':81},{'prompt_cache_hit_tokens':True},{'total_tokens':151},
                {'prompt_tokens_details':{'cached_tokens':19}},{'completion_tokens_details':{'reasoning_tokens':1}},
                {'extra_charge':1},{'prompt_tokens_details':{'cached_tokens':20,'audio_tokens':0}}):
            value=envelope();value['usage'].update(update)
            result=decode_response(json.dumps(value).encode(),binding())
            self.assertFalse(result.usage.billing_covered,update)
        for missing in ('prompt_tokens','completion_tokens','total_tokens','prompt_cache_hit_tokens','prompt_cache_miss_tokens'):
            value=envelope();del value['usage'][missing]
            self.assertFalse(decode_response(json.dumps(value).encode(),binding()).usage.billing_covered,missing)
        value=envelope();del value['usage']['prompt_tokens_details']
        usage=decode_response(json.dumps(value).encode(),binding()).usage
        self.assertTrue(usage.billing_covered);self.assertIsNone(usage.raw_usage['cached_tokens'])
        self.assertEqual(usage.fields['cache_read_tokens'],20)
        self.assertEqual(usage.raw_usage['prompt_cache_miss_tokens'],80)

    def test_metered_cost_and_versioned_raw_usage_survive_validation(self):
        with TemporaryDirectory() as directory:
            _,supplied=configuration(Path(directory).resolve())
            a=as_record(freeze(supplied[0]['explicit_values']['provider.accounts'][0],4096))
            p=as_record(freeze(supplied[0]['explicit_values']['provider.profiles'][0],2048))
            reserve,_=liability(a,p)
            self.assertEqual(reserve,2113537);self.assertEqual(reserve*14,29589518)
            self.assertLessEqual(reserve*14,30000000)
            raw=envelope();del raw['usage']['prompt_tokens_details']
            observation=decode_response(json.dumps(raw).encode(),binding()).usage
            actual=normalize(observation,a,p,reserve)
            self.assertEqual(actual['format_version'],4);self.assertTrue(actual['cost_complete'])
            self.assertEqual(actual['estimated_cost_atoms'],561);self.assertIsNone(actual['reported_cost_atoms'])
            self.assertIsNone(as_record(actual['raw_usage'])['cached_tokens']);self.assertEqual(validate_usage(actual),actual)
            for state in (None,{'prompt_tokens':100}):
                from companion_memory.provider.deepseek_protocol import observe_usage
                unknown=normalize(observe_usage(freeze(state,2048)),a,p,reserve)
                self.assertFalse(unknown['cost_complete']);self.assertEqual(unknown['held_atoms'],reserve)
                self.assertEqual(validate_usage(unknown),unknown)
            unsent=normalize(observation,a,p,reserve,not_sent=True)
            self.assertTrue(unsent['cost_complete']);self.assertTrue(all(v is None for v in as_record(unsent['raw_usage']).values()))
            validate_usage(unsent,not_sent=True)
            corrupt: dict[str,object]=dict(actual);corrupt['raw_usage']={**as_record(actual['raw_usage']),'prompt_cache_miss_tokens':79}
            with self.assertRaises(InvalidData):validate_usage(corrupt)

    def test_fixed_configuration_and_prompt_binding_preserve_legacy(self):
        with TemporaryDirectory() as directory:
            root=Path(directory).resolve();new,supplied=configuration(root)
            self.assertEqual(new.runtime.integer('runtime.operation_timeout_ms'),60000)
            old,_=minimax(root);self.assertEqual(old.runtime.integer('runtime.operation_timeout_ms'),5000)
            for key,value in (('runtime.operation_timeout_ms',5000),('runtime.operation_timeout_ms',60001)):
                supplied[1]['explicit_values'][key]=value
                self.assertIsNot(type(resolve_text_learning_configuration(*supplied)),TextConfigurationOk)
            supplied[1]['explicit_values']['runtime.operation_timeout_ms']=60000
            account=supplied[0]['explicit_values']['provider.accounts'][0]
            for key,value in (('attempt_limit',16),('billing_mode','USAGE_ONLY_TRIAL')):
                before=account[key];account[key]=value
                self.assertIsNot(type(resolve_text_learning_configuration(*supplied)),TextConfigurationOk);account[key]=before
        roster=[{'subject_id':s,'revision':1,'kind':k} for s,k,_ in SUBJECTS]
        text=render_learning_instructions(roster,WORLDS,'deepseek-flash')
        self.assertLessEqual(len(text.encode()),4096)
        subjects=learning_authorizations(text,'deepseek-flash')['subjects']
        assert type(subjects) is tuple
        self.assertEqual(len(subjects),6)
        from companion_memory.persistence.schema import InvalidValue
        with self.assertRaises(InvalidValue):learning_authorizations(text)
        self.assertNotEqual(prompt_resource('LEARNING','MiniMax-M3'),prompt_resource('LEARNING','deepseek-flash'))
