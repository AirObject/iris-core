"""Exercise intrinsic output constraints before granting any domain write."""
import json
import unittest
from companion_memory.cognition.daily_output import decode_daily_output
from companion_memory.persistence.schema import InvalidValue
from companion_memory.persistence.semantic_records import Record
from typing import cast


def encoded(value):
    return json.dumps(value,ensure_ascii=False,separators=(',',':')).encode()


def common(local=0):
    return {'local_ref':local,'target_anchors':[{'message_id':'event-one','part':'EVENT',
        'item_index':None,'start_utf8':None,'end_utf8':None,'occurrence_id':None,'interpretation_id':None}],
        'auxiliary_refs':[],'basis_refs':[]}


def subject(local=0):
    return {'action':'REGISTER_SUBJECT',**common(local),'subject_kind':'FICTIONAL_CHARACTER',
        'label':'青禾','platform_id':None,'external_subject_id':None}


def memory(local=1):
    return {'action':'CREATE_MEMORY',**common(local),'category':'FACT','body':'青禾看见展板。',
        'subject_ids':[{'local_ref':0}],'speaker_subject_id':None,'stance':'ASSERTED',
        'world_scope':{'kind':'FICTIONAL','context_id':'story'},'occurred_range':None,'applicable_range':None,
        'belief':70,'belief_reason':'故事中的直接叙述。'}


class DailyOutputTests(unittest.TestCase):
    def final(self,actions):
        return decode_daily_output(encoded({'schema_version':1,'kind':'FINAL','actions':actions}),turn=1,completed_tools=0)

    def test_ordered_local_references_and_zero_output(self):
        result=self.final([subject(),memory()])
        self.assertEqual(cast(tuple[Record,...],cast(tuple[Record,...],result['actions'])[1]['subject_ids'])[0]['local_ref'],0)
        self.assertEqual(self.final([])['actions'],())

    def test_forward_wrong_kind_and_duplicate_local_refs_are_rejected(self):
        for actions in ([memory(),subject()],[subject(),memory(0)],[memory()]):
            with self.subTest(actions=actions),self.assertRaises(InvalidValue):self.final(actions)

    def test_tools_are_bounded_and_final_is_required_on_third_turn(self):
        raw=encoded({'schema_version':1,'kind':'TOOL','tools':[{'name':'read_subjects','arguments':{'subject_ids':['person']}}]})
        self.assertEqual(decode_daily_output(raw,turn=1,completed_tools=0)['kind'],'TOOL')
        for turn,count in ((3,2),(2,4)):
            with self.assertRaises(InvalidValue):decode_daily_output(raw,turn=turn,completed_tools=count)
        with self.assertRaises(InvalidValue):
            decode_daily_output(raw.replace(b'read_subjects',b'run_command'),turn=1,completed_tools=0)

    def test_whole_json_and_field_identity_are_required(self):
        for raw in (b'{"schema_version":1,"kind":"FINAL","actions":[],"actions":[]}',
                b'```json\n{"schema_version":1,"kind":"FINAL","actions":[]}\n```',
                b'{"schema_version":1,"kind":"FINAL","actions":[]}<DSML>tail</DSML>',
                b'{"schema_version":1,"kind":"FINAL","actions":[{"action":"CREATE_MEMORY","action":"REGISTER_SUBJECT"}]}',
                b'{"schema_version":true,"kind":"FINAL","actions":[]}',
                b'{"schema_version":1,"kind":"FINAL","actions":[],"tools":[]}'):
            with self.subTest(raw=raw),self.assertRaises(InvalidValue):decode_daily_output(raw,turn=1,completed_tools=0)

    def test_self_arbitrary_identity_and_missing_evidence_are_rejected(self):
        for changes in ({'subject_kind':'SELF'},{'external_subject_id':'invented'}, {'target_anchors':[]}):
            with self.subTest(changes=changes),self.assertRaises(InvalidValue):self.final([{**subject(),**changes}])

    def test_scores_and_goals_require_complete_bounded_evidence(self):
        scores={'action':'SET_SCORES',**common(),'object_id':'memory','expected_revision':1,
            'belief':50,'belief_reason':'依据变化','retention_delta':-10,'retention_reason':'减少关注'}
        self.assertEqual(cast(tuple[Record,...],self.final([scores])['actions'])[0]['retention_delta'],-10)
        with self.assertRaises(InvalidValue):self.final([{**scores,'retention_delta':-11}])
        goal={'action':'CREATE_GOAL',**common(2),'content':'核对展板','subject_refs':[{'local_ref':0}],
            'world_scope':'story','deadline':None,'reminder_lead_seconds':None,'route_id':None,'basis_action_refs':[1]}
        self.assertEqual(len(cast(tuple[Record,...],self.final([subject(),memory(),goal])['actions'])),3)
        with self.assertRaises(InvalidValue):self.final([subject(),memory(),{**goal,'basis_action_refs':[]}])
