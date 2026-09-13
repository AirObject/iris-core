"""Actual complete configuration boundaries, native capacity authority and SQLite.

The exact aggregate sample is legal full metadata. The carrier hard-limit probe
is separately labeled encoder input, not a claim that a legal config reaches it.
"""
import copy
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from types import MappingProxyType
import unittest
from typing import cast
from companion_memory.configuration.text_resolution import resolve_text_learning_configuration,TextConfigurationOk,TextConfigurationErr,text_learning_snapshot_issue
from companion_memory.configuration.text_codec import candidate_values
from companion_memory.configuration.text_persistence import TextConfigurationAssembly
from companion_memory.configuration.text_persistent_results import ConfigurationCommitted
from companion_memory.configuration.information_resolution import resolve_information_configuration,InformationConfigurationErr,information_snapshot_issue
from companion_memory.configuration.content_resolution import content_snapshot_issue
from companion_memory.persistence import PersistenceService,DatabaseResources,Ready,ResultBoundCommand
from companion_memory.persistence._codec import command_descriptor,prepare_command,assembly_value
from companion_memory.persistence.command_capacity import declared_capacity,ConfigurationInitializationCapacity
from companion_memory.persistence.schema import freeze_value,encode_value,InvalidValue,ValueTooLarge
from companion_memory.persistence.results import OperationIdentity
from tests.text_learning.configuration_support import candidate,inputs,maximum_inputs
from tests.runtime.configuration_support import registry


def frozen_carrier(definition,payload):
    return MappingProxyType({'definition':command_descriptor(definition),'values':freeze_value(definition.input_schema,payload),
        'intentions':MappingProxyType({'configuration_initialized':MappingProxyType({'actor':'bootstrap'})})})


class TextConfigurationTests(unittest.TestCase):
    def test_canonical_utf8_escape_coefficient_and_every_actual_envelope(self):
        from companion_memory.configuration.content_codec import dump
        # Canonical inner JSON contains no literal ASCII control. Ordinary
        # ASCII doubles at most, 2-byte UTF-8 becomes six ASCII bytes, BMP
        # 3-byte UTF-8 becomes six and supplementary 4-byte UTF-8 becomes twelve.
        # Test every control and all encoding-class edges independently of the
        # legal maximum configuration sample and its complete outer envelopes.
        for scalar in (*range(128),128,2047,2048,55295,57344,65535,65536,1114111):
            body=dump({'value':chr(scalar)})
            escaped=len(encode_value(body,8192))-2
            self.assertLessEqual(escaped,3*len(body.encode()),scalar)
        with TemporaryDirectory() as directory:
            supplied,_=maximum_inputs(Path(directory));parsed=resolve_text_learning_configuration(*supplied)
            assert type(parsed) is TextConfigurationOk
            payload=candidate_values(parsed.value);definition=TextConfigurationAssembly().commands[0]
            carrier=frozen_carrier(definition,payload)
            # Use the longest legal audit actor without touching configuration
            # metadata, paths or any registered field limit.
            carrier=MappingProxyType(dict(carrier)|{'intentions':MappingProxyType({'configuration_initialized':MappingProxyType({'actor':'a'*128})})})
            values=carrier['values'];assert type(values) is MappingProxyType
            domains=values['domains'];assert type(domains) is tuple
            domain_sizes=[];entry_overheads=[];domain_overheads=[]
            for domain in domains:
                assert type(domain) is MappingProxyType
                entries=domain['entries'];assert type(entries) is tuple
                entry_sizes=[]
                for entry in entries:
                    assert type(entry) is MappingProxyType and type(entry['body']) is str
                    body_size=len(encode_value(entry['body'],24578))-2
                    self.assertLessEqual(body_size,3*len(entry['body'].encode()))
                    size=len(encode_value(entry,32768));entry_sizes.append(size)
                    entry_overheads.append(size-body_size);self.assertLessEqual(size-body_size,256)
                size=len(encode_value(domain,2097152));domain_sizes.append(size)
                domain_overheads.append(size-sum(entry_sizes));self.assertLessEqual(size-sum(entry_sizes),8192)
            actual=len(encode_value(carrier,2097152));outer=actual-sum(domain_sizes)
            self.assertLessEqual(outer,65536)
            self.assertLessEqual(actual,3*524288+118*256+6*8192+65536)
            print({'source':'ACTUAL_CONFIGURATION_ENVELOPES','carrier_bytes':actual,'entry_overhead_max':max(entry_overheads),
                'domain_overhead_max':max(domain_overheads),'definition_catalog_intent_outer_bytes':outer})

    def test_complete_maximum_metadata_and_aggregate_plus_one(self):
        with TemporaryDirectory() as directory:
            supplied,changed=maximum_inputs(Path(directory));r=resolve_text_learning_configuration(*supplied)
            self.assertIs(type(r),TextConfigurationOk,r)
            assert type(r) is TextConfigurationOk
            payload=candidate_values(r.value);sizes=[len(e['body'].encode()) for d in payload['domains'] for e in d['entries']]
            self.assertEqual(len(sizes),118);self.assertEqual(sum(sizes),524288);self.assertEqual(max(sizes),8192)
            definition=TextConfigurationAssembly().commands[0];carrier=frozen_carrier(definition,payload)
            encoded=encode_value(carrier,2097152)
            self.assertGreater(len(encoded),1048576);self.assertLessEqual(len(encoded),1717760)
            with self.assertRaises(ValueTooLarge):encode_value(carrier,1048576)
            identity=OperationIdentity('database','configuration','initialize_text_learning','instance','original')
            command=ResultBoundCommand(1,payload,{'configuration_initialized':{'actor':'bootstrap'}})
            expected=prepare_command(definition,identity,command,2097152)
            self.assertEqual(prepare_command(definition,identity,command,933888),expected)
            # Caller-controlled limits cannot widen ordinary preflight either.
            ordinary=replace(definition,operation_kind='ordinary',capacity_policy=None)
            with self.assertRaises(ValueTooLarge):prepare_command(ordinary,identity,command,2097152)
            print({'source':'ACTUAL_COMPLETE_CONFIGURATION','entry_count':118,'body_bytes':sum(sizes),'largest_entry':max(sizes),'frozen_carrier_bytes':len(encoded)})
            # Extend an entry that still has room: aggregate rejection is independent.
            domain,declarations=changed[-1];declarations[-1]['description']+='x';domain['registry']=registry(declarations)
            self.assertIs(type(resolve_text_learning_configuration(*supplied)),TextConfigurationErr)

    def test_single_complete_entry_plus_one_rejects_before_publication(self):
        with TemporaryDirectory() as directory:
            supplied,changed=maximum_inputs(Path(directory))
            domain,declarations=changed[0];declarations[0]['description']+='x';domain['registry']=registry(declarations)
            self.assertIs(type(resolve_text_learning_configuration(*supplied)),TextConfigurationErr)

    def test_carrier_encoder_exact_hard_boundary_is_not_a_legal_configuration_sample(self):
        # A separate codec hard-bound probe; full legal initialization is measured above.
        value=MappingProxyType({'probe':'x'*(2097152-len(b'{"probe":""}'))})
        self.assertEqual(len(encode_value(value,2097152)),2097152)
        with self.assertRaises(ValueTooLarge):encode_value(value,2097151)
        with self.assertRaises(ValueTooLarge):encode_value(MappingProxyType({'probe':value['probe']+'x'}),2097152)

    def test_policy_cannot_be_copied_renamed_or_used_in_an_old_assembly(self):
        a=TextConfigurationAssembly();d=a.commands[0]
        self.assertEqual(declared_capacity(d),2097152)
        with self.assertRaises(TypeError):ConfigurationInitializationCapacity()
        for fake in (replace(d),replace(d,operation_kind='ordinary'),replace(d,capacity_policy=2097152),replace(d,capacity_policy=object()),replace(d,capacity_policy=object.__new__(ConfigurationInitializationCapacity))):
            with self.assertRaises(InvalidValue):declared_capacity(fake)
        for format in ('LEGACY','LOCAL_INFORMATION_V1'):
            with self.assertRaises(InvalidValue):assembly_value(a.repositories,a.commands,assembly_format=format)
        with self.assertRaises(InvalidValue):assembly_value(a.repositories,(replace(d,capacity_policy=None),),assembly_format='MODEL_TEXT_LEARNING_V1')

    def test_old_candidate_and_material_types_do_not_alias_new_format(self):
        from tests.information.configuration_support import inputs as old_inputs
        with TemporaryDirectory() as directory:
            root=Path(directory);value,supplied=candidate(root)
            self.assertIsNone(text_learning_snapshot_issue(value))
            self.assertIsNotNone(information_snapshot_issue(value));self.assertIsNotNone(content_snapshot_issue(value))
            old=old_inputs(root)
            altered=(*supplied[:5],*old[5:])
            self.assertIs(type(resolve_information_configuration(*altered)),InformationConfigurationErr)
            broken=(*supplied[:7],old[6])
            self.assertIs(type(resolve_text_learning_configuration(*broken)),TextConfigurationErr)

    def test_wrong_fields_media_resources_and_schema_metadata_are_rejected(self):
        with TemporaryDirectory() as directory:
            supplied=inputs(Path(directory))
            for index,key,bad in ((0,'provider.max_in_flight',2),(1,'learning.material_max_bytes',49152),(3,'media.processing_concurrency',1),
                (3,'media.root_directory',''),(3,'media.file_worker_capacity',3),(3,'media.blob_max_bytes',1024)):
                domain=cast(dict, supplied[index]);original=domain['explicit_values'][key];domain['explicit_values'][key]=bad
                self.assertIs(type(resolve_text_learning_configuration(*supplied)),TextConfigurationErr,(key,bad))
                domain['explicit_values'][key]=original
            for key in supplied[5]['explicit_values']:
                original=supplied[5]['explicit_values'][key]
                supplied[5]['explicit_values'][key]={**original,'unknown':1}
                self.assertIs(type(resolve_text_learning_configuration(*supplied)),TextConfigurationErr,key)
                supplied[5]['explicit_values'][key]=original


class TextConfigurationPersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_maximum_configuration_atomic_publication_and_original_replay(self):
        with TemporaryDirectory() as directory:
            root=Path(directory).resolve();supplied,_=maximum_inputs(root)
            parsed=resolve_text_learning_configuration(*supplied);assert type(parsed) is TextConfigurationOk
            value=parsed.value;original=None
            for mode in ('CREATE_NEW','OPEN_EXISTING'):
                a=TextConfigurationAssembly();storage=PersistenceService(a.repositories,a.commands,assembly_format='MODEL_TEXT_LEARNING_V1')
                resources=DatabaseResources('text-database',lambda identity,path:identity=='text-database' and path==str(root/'database'/'runtime.sqlite3'))
                self.assertIs(type(await storage.initialize(value.foundation,resources,mode)),Ready)
                binding=a.bind(storage,'instance',value)
                try:
                    result=await binding.persist_text_learning_configuration('original',value,actor='bootstrap',protected_directories=supplied[6])
                    self.assertIs(type(result),ConfigurationCommitted,result)
                    assert type(result) is ConfigurationCommitted and result.configuration is not None,result
                    self.assertEqual(candidate_values(result.configuration.candidate),candidate_values(value))
                    self.assertEqual(len(result.configuration.revisions),6)
                    if original is None:original=result.receipt
                    else:self.assertEqual(result.receipt,original);self.assertEqual(result.source,'EXISTING')
                finally:
                    binding.close();await storage.close();self.assertTrue(binding.close())

    async def test_each_write_and_audit_failure_rolls_back_all_configuration_rows(self):
        import sqlite3
        from tests.persistence.support import Hooks,sqlite_fault
        from companion_memory.configuration.text_persistent_results import ConfigurationNotCommitted
        for table in ('configuration_domains','configuration_entries','configuration_snapshots','active_configuration','audit_records','operation_receipts'):
            with self.subTest(table=table),TemporaryDirectory() as directory:
                root=Path(directory).resolve();value,supplied=candidate(root);hooks=Hooks()
                a=TextConfigurationAssembly();storage=PersistenceService(a.repositories,a.commands,assembly_format='MODEL_TEXT_LEARNING_V1')
                path=root/'database'/'runtime.sqlite3'
                resources=DatabaseResources('text-database',lambda identity,p:identity=='text-database' and p==str(path),connect=hooks.connect)
                self.assertIs(type(await storage.initialize(value.foundation,resources,'CREATE_NEW')),Ready)
                binding=a.bind(storage,'instance',value)
                def before(sql):
                    if sql.startswith('INSERT INTO '+table):raise sqlite_fault(sqlite3.SQLITE_FULL)
                hooks.before=before
                try:
                    result=await binding.persist_text_learning_configuration('failed',value,actor='bootstrap',protected_directories=supplied[6])
                    self.assertIs(type(result),ConfigurationNotCommitted,result)
                    with sqlite3.connect(path) as reader:
                        counts={name:reader.execute('SELECT COUNT(*) FROM '+name).fetchone()[0] for name in
                            ('configuration_domains','configuration_entries','configuration_snapshots','active_configuration','audit_records','operation_receipts','required_audit_events')}
                    self.assertEqual(set(counts.values()),{0},counts)
                finally:
                    hooks.before=lambda sql:None;binding.close();await storage.close();binding.close()

    async def test_cancelled_large_publication_retains_actual_transaction_and_cleanup_owner(self):
        import asyncio,threading,tracemalloc
        from tests.persistence.support import Hooks
        with TemporaryDirectory() as directory:
            root=Path(directory).resolve();supplied,_=maximum_inputs(root)
            parsed=resolve_text_learning_configuration(*supplied);assert type(parsed) is TextConfigurationOk
            value=parsed.value;a=TextConfigurationAssembly();hooks=Hooks();entered=threading.Event();release=threading.Event()
            storage=PersistenceService(a.repositories,a.commands,assembly_format='MODEL_TEXT_LEARNING_V1')
            resources=DatabaseResources('text-database',lambda identity,path:identity=='text-database' and path==str(root/'database'/'runtime.sqlite3'),connect=hooks.connect)
            self.assertIs(type(await storage.initialize(value.foundation,resources,'CREATE_NEW')),Ready)
            binding=a.bind(storage,'instance',value);has_snapshot=False
            def after(sql):
                nonlocal has_snapshot
                if sql.startswith('INSERT INTO configuration_snapshots'):has_snapshot=True
                if sql=='COMMIT' and has_snapshot:
                    entered.set();release.wait(15)
            hooks.after=after;tracemalloc.start()
            task=asyncio.create_task(binding.persist_text_learning_configuration('cancelled',value,actor='bootstrap',protected_directories=supplied[6]))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait,10))
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):await task
                self.assertFalse(binding.close())
                self.assertEqual(storage.get_health().writes_in_flight,1)
                release.set()
                for _ in range(500):
                    if binding.close():break
                    await asyncio.sleep(.01)
                self.assertTrue(binding.close())
                print({'source':'ACTUAL_LARGE_INITIALIZATION','traced_current_bytes':tracemalloc.get_traced_memory()[0],
                       'traced_peak_bytes':tracemalloc.get_traced_memory()[1],'writer_limit':1})
            finally:
                release.set();hooks.after=lambda sql:None
                if not task.done():await task
                binding.close();await storage.close();binding.close();tracemalloc.stop()
