"""Byte-exact synthetic envelope, worst-window capacity and usable event limits."""
from pathlib import Path
from tempfile import TemporaryDirectory
from types import MappingProxyType
import unittest

from companion_memory.configuration import RuntimeConfigurationOk, RuntimeConfigurationErr, resolve_runtime_configuration
from companion_memory.configuration.material_contracts import FIXED_BYTES, REFERENCE_BYTES, SYSTEM_TEXT
from companion_memory.ingress import canonical_event, isolate_event
from companion_memory.persistence.schema import InvalidValue
from companion_memory.buffers import MaterialRecord, build_material, decode_material
from tests.runtime.configuration_support import event, inputs


class MaterialBudgetTests(unittest.TestCase):
    def test_minimum_usable_event_and_exact_full_window(self):
        base=event('e')
        self.assertEqual(len(canonical_event(isolate_event(base,1024))),246)
        base['body']='x'*779
        full=isolate_event(base,1024)
        self.assertEqual(len(canonical_event(full)),1024)
        self.assertEqual((FIXED_BYTES,REFERENCE_BYTES,len(SYSTEM_TEXT.encode())),(1057,180,93))
        identities=tuple('i'*128 for _ in range(7))
        rows=tuple(MaterialRecord(role, str(index)+'m'*127,2**63-5+index,'2099-01-01T00:00:00.000000Z',full) for index,role in enumerate(('H','T','T','R')))
        messages=build_material(identities,rows)
        self.assertEqual(sum(len(m['text'].encode()) for m in messages),7249)
        self.assertEqual(decode_material(messages),(identities,rows))
        base['body']='x'*780
        with self.assertRaises(InvalidValue):isolate_event(base,1024)

    def test_escaping_unicode_and_base64_remainders_preserve_exact_content(self):
        for body in ('漢字é🙂','\\n\n\t\r\b\f\x00\x1f"\\','x','xx','xxx'):
            value=isolate_event(event('e',body),1024)
            encoded=canonical_event(value)
            self.assertNotIn(b'\\n',encoded.replace(b'\\\\n',b''))
            row=MaterialRecord('T','message',1,'2026-09-10T00:00:00.000000Z',value)
            messages=build_material(('i',)*7,(row,))
            self.assertEqual(decode_material(messages)[1][0].event,value)
            import base64
            self.assertEqual(len(base64.b64encode(encoded)),4*((len(encoded)+2)//3))

    def test_bound_configuration_accepts_and_original_overflow_configuration_rejects(self):
        with TemporaryDirectory(prefix='iris-runtime-config-') as temporary:
            supplied=inputs(Path(temporary))
            result=resolve_runtime_configuration(*supplied)
            self.assertIs(type(result),RuntimeConfigurationOk,result)
            runtime=supplied[1]['explicit_values']
            assert type(runtime) is dict
            runtime['ingress.event_max_bytes']=4096
            runtime['learning.material_max_bytes']=6144
            platform=supplied[2][0]['explicit_values']
            assert type(platform) is dict
            for key,value in (('history_context_count',2),('target_count',8),('recent_context_count',2)):
                platform['platforms.sample_platform.buffer.'+key]=value
            rejected=resolve_runtime_configuration(*supplied)
            assert type(rejected) is RuntimeConfigurationErr
            self.assertEqual((rejected.error.field,rejected.error.reason),('platforms','BUDGET_INVALID'))
            assert type(result) is RuntimeConfigurationOk
            self.assertEqual(result.value.runtime.integer('ingress.event_max_bytes'),1024)
            self.assertEqual(result.value.platforms[0].count('target_count'),2)

    def test_actual_provider_encoder_and_meter_match_maximum_request_bound(self):
        from companion_memory.provider.normalization import input_units
        from companion_memory.provider.values import as_record,freeze,dump
        with TemporaryDirectory(prefix='iris-request-budget-') as temporary:
            # Use the actual Provider canonical encoder and simulated meter.
            identity='i'*128
            full=isolate_event(event('e','x'*779),1024)
            members=tuple(MaterialRecord(role,str(index)+'m'*127,2**63-5+index,'2099-01-01T00:00:00.000000Z',full) for index,role in enumerate(('H','T','T','R')))
            messages=build_material((identity,)*7,members)
            payload={'messages':[dict(m) for m in messages],'input_units_limit':8192,'output_units_limit':1024}
            profile=as_record(freeze({'max_items':2,'max_input_units':8192,'max_output_units':1024},1024))
            self.assertEqual(input_units('GENERATION',as_record(freeze(payload,16384)),profile),(7249,1024,None))
            from companion_memory.provider.service import OPTIONALS
            normalized:dict[str,object]={key:identity for key in OPTIONALS}
            normalized.update(operation_key=identity,run_id=identity,profile_id=identity,entry_ids=[identity],payload=payload)
            encoded=dump(as_record(freeze(normalized,16384)),16384).encode()
            self.assertEqual(len(encoded),8700)
            self.assertLessEqual(len(encoded),16384)
            from companion_memory.provider.values import DataLimit
            with self.assertRaises(DataLimit):input_units('GENERATION',as_record(freeze({**payload,'messages':payload['messages']*2},32768)),profile)
