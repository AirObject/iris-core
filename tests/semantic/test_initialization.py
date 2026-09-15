"""Actual atomic semantic configuration publication and original-key recovery.

This exercises the three bootstrap writers and their mandatory audits in real
SQLite. It opens no Provider resource and does not activate a scheduler.
"""
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from companion_memory.configuration.semantic_persistence import SemanticConfigurationAssembly
from companion_memory.configuration.semantic_persistent_results import ConfigurationCommitted,ConfigurationNotCommitted
from companion_memory.persistence import PersistenceService,DatabaseResources,Ready
from companion_memory.persistence._codec import assembly_value,command_descriptor
from companion_memory.persistence.schema import encode_value,InvalidValue
from companion_memory.persistence.semantic_records import Record
from typing import cast
from tests.semantic.configuration_support import candidate
from tests.persistence.support import Hooks,sqlite_fault


class SemanticInitializationTests(unittest.IsolatedAsyncioTestCase):
    async def test_three_real_writers_initialize_and_original_key_recovers(self):
        with TemporaryDirectory() as directory:
            root=Path(directory).resolve();value,supplied=candidate(root,offline=False);original=None
            for mode in ('CREATE_NEW','OPEN_EXISTING'):
                a=SemanticConfigurationAssembly();storage=PersistenceService(a.repositories,a.commands,assembly_format='ASYNC_SEMANTIC_V1')
                resource=DatabaseResources('text-database',lambda identity,path:identity=='text-database' and path==str(root/'database'/'runtime.sqlite3'))
                opened=await storage.initialize(value.foundation,resource,mode);self.assertIs(type(opened),Ready,opened)
                binding=a.bind(storage,'instance',value)
                try:
                    result=await binding.persist_semantic_configuration('original',value,actor='bootstrap',protected_directories=supplied[6])
                    self.assertIs(type(result),ConfigurationCommitted,result)
                    assert type(result) is ConfigurationCommitted and result.configuration is not None,result
                    receipt_result=cast(Record,result.receipt.result)
                    self.assertEqual(len(cast(tuple,receipt_result['targets'])),3)
                    self.assertEqual(set(cast(Record,receipt_result['facts'])),{'memory','retrieval'})
                    if original is None:original=result.receipt
                    else:self.assertEqual(result.receipt,original);self.assertEqual(result.source,'EXISTING')
                    with sqlite3.connect(root/'database'/'runtime.sqlite3') as reader:
                        self.assertEqual(reader.execute('SELECT count(*) FROM memory_semantic_publication').fetchone()[0],1)
                        self.assertEqual(reader.execute("SELECT json_extract(body,'$.scheduler') FROM retrieval_semantic_control").fetchone()[0],'PAUSED')
                        self.assertEqual(reader.execute('SELECT count(*) FROM audit_records').fetchone()[0],3)
                finally:binding.close();await storage.close();self.assertTrue(binding.close())
            print({'proof':'THREE_OWNER_INITIALIZATION','static_bytes':len(assembly_value(a.repositories,a.commands,assembly_format='ASYNC_SEMANTIC_V1')),
                'initialize_descriptor_bytes':len(encode_value(command_descriptor(a.commands[0]),2097152))})

    async def test_each_owner_audit_and_receipt_failure_rolls_back_whole_initialization(self):
        tables=('configuration_domains','configuration_entries','configuration_snapshots','active_configuration',
            'memory_semantic_publication','retrieval_semantic_control','audit_records','operation_receipts')
        for target in tables:
            with self.subTest(table=target),TemporaryDirectory() as directory:
                root=Path(directory).resolve();value,supplied=candidate(root,offline=False);hooks=Hooks();a=SemanticConfigurationAssembly()
                storage=PersistenceService(a.repositories,a.commands,assembly_format='ASYNC_SEMANTIC_V1')
                path=root/'database'/'runtime.sqlite3';resource=DatabaseResources('text-database',lambda identity,p:identity=='text-database' and p==str(path),connect=hooks.connect)
                self.assertIs(type(await storage.initialize(value.foundation,resource,'CREATE_NEW')),Ready)
                binding=a.bind(storage,'instance',value)
                def before(sql):
                    if sql.startswith('INSERT INTO '+target):raise sqlite_fault(sqlite3.SQLITE_FULL)
                hooks.before=before
                try:
                    result=await binding.persist_semantic_configuration('original',value,actor='bootstrap',protected_directories=supplied[6])
                    self.assertIs(type(result),ConfigurationNotCommitted,result)
                    with sqlite3.connect(path) as reader:
                        self.assertEqual({reader.execute('SELECT count(*) FROM '+table).fetchone()[0] for table in tables},{0})
                finally:hooks.before=lambda sql:None;binding.close();await storage.close();binding.close()

    def test_assembly_and_capacity_reject_both_old_new_mismatches(self):
        from companion_memory.configuration.text_persistence import TextConfigurationAssembly
        old=TextConfigurationAssembly();new=SemanticConfigurationAssembly()
        with self.assertRaises(InvalidValue):assembly_value(old.repositories,old.commands,assembly_format='ASYNC_SEMANTIC_V1')
        with self.assertRaises(InvalidValue):assembly_value(new.repositories,new.commands,assembly_format='MODEL_TEXT_LEARNING_V1')
