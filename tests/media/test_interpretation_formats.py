"""Exact interpretation limits, attribution restrictions and external event states."""
from types import MappingProxyType
import unittest
from companion_memory.media.interpretations import isolate_interpretation, decode_interpretation
from companion_memory.persistence.content_codec import encode_content
from companion_memory.persistence.schema import InvalidValue, ValueTooLarge
from companion_memory.ingress.media_events import isolate_media_event, decode_media_event
from tests.runtime.configuration_support import event


def longest_record(origin='INTERNAL', status='EMPTY'):
    value = dict(interpretation_version=1, interpretation_id='i'*128, blob_id='b'*128,
                 generation=2**63-1, task='TRANSCRIBE', modality='AUDIO', origin=origin,
                 status=status, text='', coverage='COMPLETE', source_ref='s'*128,
                 interpretation_fingerprint='f'*64, prompt_revision='p'*64,
                 scope_kind='EVENT', scope_id='s'*128, event_id='e'*128,
                 provider_request_id='r'*128, created_at_us=2**63-1, failure_reason=None)
    if origin == 'EXTERNAL':
        value.update(interpretation_fingerprint=None, prompt_revision=None, provider_request_id=None)
    if status == 'COMPLETE':
        value['text'] = 'x'
    elif status == 'PARTIAL':
        value.update(text='x', coverage='EXPLICIT_PARTIAL')
    elif status == 'FAILED':
        value.update(text=None, coverage='UNSPECIFIED', failure_reason='EXTERNAL_FAILURE' if origin == 'EXTERNAL' else 'RESULT_LIMIT_EXCEEDED')
    elif status == 'REFUSED':
        value.update(text='敏感信息无法访问', coverage='UNSPECIFIED')
    if origin == 'INTERNAL' and status in ('FAILED','REFUSED'):
        value['source_ref'] = value['provider_request_id']
    return value


class InterpretationFormatTests(unittest.TestCase):
    def test_every_origin_and_completed_state_has_exact_canonical_limit(self):
        for origin, sizes in (
            ('INTERNAL', {'COMPLETE': 1306, 'EMPTY': 1302, 'FAILED': 1327, 'REFUSED': 1331}),
            ('EXTERNAL', {'COMPLETE': 1056, 'PARTIAL': 1063, 'EMPTY': 1052, 'FAILED': 1072, 'REFUSED': 1081}),
        ):
            for status, size in sizes.items():
                with self.subTest(origin=origin, status=status):
                    value = isolate_interpretation(longest_record(origin, status), text_limit=512, record_limit=2048)
                    encoded = encode_content(value, 2048)
                    self.assertEqual(len(encoded), size)
                    self.assertEqual(decode_interpretation(encoded), value)

    def test_fixed_refusal_is_independent_of_zero_text_limit(self):
        value = isolate_interpretation(longest_record(status='REFUSED'), text_limit=0, record_limit=1331)
        self.assertEqual(len(encode_content(value, 1331)), 1331)
        with self.assertRaises(InvalidValue):
            isolate_interpretation(longest_record(status='REFUSED'), text_limit=0, record_limit=1330)

    def test_actual_encoding_distinguishes_text_size_and_complete_record_size(self):
        value = longest_record(status='COMPLETE')
        value['text'] = 'x'*512
        self.assertEqual(len(encode_content(isolate_interpretation(value,text_limit=512,record_limit=2048),2048)),1817)
        value['text'] = '\x00'*123 + 'x'*5
        self.assertEqual(len(encode_content(isolate_interpretation(value,text_limit=512,record_limit=2048),2048)),2048)
        for text in ('\x00'*124, '\x00'*512, 'x'*513):
            value['text'] = text
            with self.assertRaises(ValueTooLarge):
                isolate_interpretation(value,text_limit=512,record_limit=2048)

    def test_origin_cannot_claim_another_owner_or_invalid_state(self):
        for changes in ({'provider_request_id':'forged'}, {'scope_kind':'CONTENT','event_id':None},
                        {'interpretation_fingerprint':'f'*64}, {'status':'COMPLETE','text':''},
                        {'status':'EMPTY','text':'description'}, {'generation':True},
                        {'extra':None}, {'source_ref':'\x00'*22}):
            with self.subTest(changes=changes):
                value=longest_record('EXTERNAL');value.update(changes)
                with self.assertRaises(InvalidValue):
                    isolate_interpretation(value,text_limit=512,record_limit=2048)
        with self.assertRaises(InvalidValue):
            isolate_interpretation(longest_record('INTERNAL','PARTIAL'),text_limit=512,record_limit=2048)

    def test_terminal_internal_attribution_and_canonical_storage_are_not_rewritten(self):
        value = longest_record(status='FAILED')
        value['source_ref'] = 'another-request'
        with self.assertRaises(InvalidValue):
            isolate_interpretation(value, text_limit=512, record_limit=2048)
        encoded = encode_content(isolate_interpretation(longest_record(), text_limit=512, record_limit=2048), 2048)
        with self.assertRaises(InvalidValue):
            decode_interpretation(b' ' + encoded)

    def test_media_wire_rejects_utf16_and_duplicate_keys(self):
        import json
        value = event('example')
        value['event_version'] = 2
        for encoded in (json.dumps(value).encode('utf-16'), b'{"event_version":2,"event_version":2}'):
            with self.assertRaises(InvalidValue):
                decode_media_event(encoded, 2048, occurrence_limit=2, text_limit=512)

    def test_external_events_preserve_reports_without_creating_provider_authority(self):
        for status,text,source,coverage in (
            ('MISSING',None,None,'UNSPECIFIED'), ('COMPLETE','a','report','COMPLETE'),
            ('EMPTY','','report','COMPLETE'), ('PARTIAL','a','report','EXPLICIT_PARTIAL'),
            ('FAILED',None,'report','UNSPECIFIED'), ('REFUSED','敏感信息无法访问','report','UNSPECIFIED'),
        ):
            value=event('example');value['event_version']=2
            value['media']=[{'reference_id':'upload','occurrence_id':'occurrence','modality':'AUDIO',
                             'interpretation':{'status':status,'text':text,'source_ref':source,'coverage':coverage}}]
            owned=isolate_media_event(value,2048,occurrence_limit=2,text_limit=512)
            self.assertIs(type(owned),MappingProxyType)
