"""Actual canonical source encoding, reversible full windows and request budgets."""
import base64
import hashlib
import json
from types import MappingProxyType
import unittest
from companion_memory.buffers.content_material import SourceMember, build_content_material, encode_member
from companion_memory.ingress.media_events import isolate_media_event
from companion_memory.media.interpretations import isolate_interpretation
from companion_memory.persistence.content_codec import encode_content
from tests.runtime.configuration_support import event
from tests.media.test_interpretation_formats import longest_record


class CompleteMaterialTests(unittest.TestCase):
    def test_maximum_event_and_interpretations_are_preserved_in_full_window(self):
        members=maximum_members()
        material=build_content_material(tuple('x'*128 for _ in range(7)),tuple(members),event_limit=2048,interpretation_limit=2048,material_limit=49152)
        size=sum(len(item['text'].encode()) for item in material)
        self.assertLessEqual(size,40180)
        for member,line in zip(members,material[1]['text'].splitlines()[8:-1]):
            decoded=base64.b64decode(line.split('|')[4],validate=True)
            self.assertEqual(decoded,encode_member(member,2048,2048))
            record=json.loads(decoded)
            self.assertEqual(record['event'],json.loads(member.event))
            self.assertEqual(record['interpretations'],[json.loads(value) for value in member.interpretations])
        self.assertEqual(sum(message['text'].count('\n') for message in material),14)


def maximum_members():
    members=[]
    for ordinal,role in enumerate(('H','T','T','R')):
        interpretations=[]
        for index in range(2):
            record=longest_record(status='COMPLETE')
            record.update(interpretation_id=str(index)*128,text='\x00'*123+'x'*5)
            interpretations.append(encode_content(isolate_interpretation(record,text_limit=512,record_limit=2048),2048))
        raw=event('event'+str(ordinal),'x');raw['event_version']=2
        raw['media']=[{'reference_id':'r'*128,'occurrence_id':str(index)*128,'modality':'AUDIO','interpretation':None} for index in range(2)]
        encoded=encode_content(isolate_media_event(raw,2048,occurrence_limit=2,text_limit=512),2048)
        raw['body']='x'*(2049-len(encoded))
        encoded=encode_content(isolate_media_event(raw,2048,occurrence_limit=2,text_limit=512),2048)
        assert len(encoded) == 2048
        header=MappingProxyType({'message_id':'m'*127+str(ordinal),'entry_seq':2**63-5+ordinal,
                                 'received_at_us':253402300799999999,'transferred_at_us':253402300799999999,
                                 'payload_digest':hashlib.sha256(encoded).hexdigest(),
                                 'interpretation_ids':tuple(str(index)*128 for index in range(2))})
        members.append(SourceMember(role,header,encoded,tuple(interpretations)))
    return tuple(members)


class MaterialCharacterTests(unittest.TestCase):
    def test_unicode_quotes_controls_and_all_base64_remainders_round_trip(self):
        remainders = set()
        for suffix in range(3):
            raw = event('encoded', '中文 "quoted" \\ path \x00\n' + 'x' * suffix); raw['event_version'] = 2
            encoded = encode_content(isolate_media_event(raw, 2048, occurrence_limit=2, text_limit=512), 2048)
            member = SourceMember('T', MappingProxyType({'message_id': 'message', 'entry_seq': 1, 'received_at_us': 1000000,
                'transferred_at_us': None, 'payload_digest': hashlib.sha256(encoded).hexdigest(), 'interpretation_ids': ()}), encoded, ())
            full = encode_member(member, 2048, 2048); remainders.add(len(full) % 3)
            material = build_content_material(('instance', 'host', 'platform', 'entry', 'batch', 'run', 'configuration'),
                (member,), event_limit=2048, interpretation_limit=2048, material_limit=49152)
            line = material[1]['text'].splitlines()[8]
            restored = base64.b64decode(line.split('|')[4], validate=True)
            self.assertEqual(restored, full)
            self.assertEqual(json.loads(restored)['event'], json.loads(encoded))
        self.assertEqual(remainders, {0, 1, 2})
