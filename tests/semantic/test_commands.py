"""Complete static command identities, closed payloads and copy rejection.

These tests measure actual declarations, not execution of the domain handlers.
The schema pool is lossless and includes all payload variants and audit fields.
"""
from dataclasses import replace
from types import MappingProxyType
import unittest
import json
from companion_memory.persistence.semantic_commands import SemanticCommands,SemanticInputPolicy,validate_values
from companion_memory.persistence._codec import command_descriptor,schema_value
from companion_memory.persistence.schema import encode_value,InvalidValue,Value
from companion_memory.persistence.semantic_records import Record
from companion_memory.persistence.content_codec import encode_content
from companion_memory.memory.semantic_repository import semantic_memory_catalog
from companion_memory.retrieval.semantic_repository import semantic_retrieval_catalog
from companion_memory.cognition.fixed_memory_repository import fixed_memory_catalog
from companion_memory.provider.ledger import LedgerAssembly
from companion_memory.logging_service.object_history import history_catalog
from typing import cast


def declarations():
    repositories=tuple(c.definition for c in (semantic_memory_catalog(),semantic_retrieval_catalog(),fixed_memory_catalog(),history_catalog()))+LedgerAssembly().repositories
    def no_execution(kind,uow,envelope,payload):raise AssertionError('This test only measures declarations.')
    return SemanticCommands(repositories,no_execution)


class SemanticCommandTests(unittest.TestCase):
    def test_complete_descriptors_fit_and_schema_pool_is_lossless(self):
        assembly=declarations();sizes={}
        def expand(nodes: tuple,index: int) -> Value:
            value=nodes[index]
            if value[0]=='scalar':return MappingProxyType(dict(kind=value[1],minimum=value[2],maximum=value[3],choices=value[4]))
            if value[0]=='text':return MappingProxyType({'bounded_text':value[1]})
            if value[0]=='sequence':return MappingProxyType({'sequence':expand(nodes,value[1]),'minimum':value[2],'maximum':value[3]})
            self.assertEqual(value[0],'record')
            return tuple(MappingProxyType({'field':f[0],'schema':expand(nodes,f[1]),'nullable':f[2],'optional':f[3]}) for f in value[1])
        for command in assembly.commands:
            descriptor=cast(Record,command_descriptor(command));nodes=cast(tuple,descriptor['schema_nodes'])
            sizes[command.operation_kind]=len(encode_value(descriptor,8192))
            self.assertEqual(expand(nodes,cast(int,descriptor['input'])),schema_value(command.input_schema))
            self.assertEqual(expand(nodes,cast(int,descriptor['result'])),schema_value(command.result_schema))
            policy=cast(SemanticInputPolicy,command.input_policy)
            for index,schema in zip(cast(tuple,descriptor['payload_variants']),policy.variants,strict=True):
                self.assertEqual(expand(nodes,index),schema_value(schema))
            self.assertEqual(len(cast(tuple,descriptor['audit_schemas'])),len(command.required_audits))
            self.assertLessEqual(sizes[command.operation_kind],8192)
        self.assertEqual(len(sizes),26)
        print({'proof':'COMPLETE_NEW_COMMAND_DECLARATIONS','bytes':sizes,'total':sum(sizes.values())})

    def test_fixed_creation_has_exactly_two_business_writers_and_no_history_slot(self):
        command=next(c for c in declarations().commands if c.operation_kind=='fixed_establish')
        self.assertEqual(tuple(p.owner_module for p in command.participants),('cognition','memory'))
        self.assertEqual(tuple(a.owner_module for a in command.required_audits),('cognition','memory'))
        self.assertEqual(tuple(a.event_slot for a in command.required_audits),('cognition_semantic','memory_semantic'))
        facts=next(f.schema for f in command.result_schema.fields if f.name=='facts')
        from companion_memory.persistence import RecordSchema
        assert type(facts) is RecordSchema
        self.assertEqual(tuple(f.name for f in facts.fields),('cognition','memory'))

    def test_copied_native_policy_is_rejected(self):
        for command in declarations().commands:
            with self.assertRaises(InvalidValue):command_descriptor(replace(command,operation_kind='copied'))

    def test_delete_payload_forbids_every_network_field_before_transaction(self):
        command=next(c for c in declarations().commands if c.operation_kind=='prepare')
        payload={'kind':'DELETE_LOCAL','work_id':'deleted','config':{'database_id':'db','instance_id':'instance','snapshot_id':'config'},
            'space_id':'space','object_ref':{'object_id':'memory','revision':2},'change_seq':1,
            'deletion_ref':{'kind':'delete','key':'original','fingerprint':'a'*64}}
        def envelope(value):return MappingProxyType({'payload':json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'))})
        validate_values(command,envelope(payload))
        for name in ('purpose','partition_id','material_digest','input_leaf_count','input_bytes','original_request_key',
                     'intent','request_ref','artifact_id','deadline_at','rendered_text'):
            with self.subTest(field=name),self.assertRaises(InvalidValue):validate_values(command,envelope(payload|{name:None}))
