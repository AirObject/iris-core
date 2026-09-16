"""Check complete daily parsing and reject mixed formats without any I/O."""
import tempfile
import unittest
from pathlib import Path
from typing import cast
from companion_memory.configuration.daily_codec import candidate_values
from companion_memory.configuration.daily_resolution import resolve_daily_configuration,DailyConfigurationOk
from .configuration_support import candidate,inputs


class DailyConfigurationTests(unittest.TestCase):
    def test_all_entries_and_six_domains_are_encoded(self):
        with tempfile.TemporaryDirectory() as root:
            value,_=candidate(Path(root));encoded=candidate_values(value)
            self.assertEqual(len(encoded['domains']),6)
            self.assertEqual(sum(len(d['entries']) for d in encoded['domains']),130)
            self.assertLessEqual(sum(len(e['body'].encode()) for d in encoded['domains'] for e in d['entries']),524288)

    def test_invalid_timezone_and_old_media_combination_are_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            for domain,key,value in ((5,'runtime.timezone','Not/AZone'),(3,'media.processing_concurrency',0),
                    (5,'runtime.learning_scheduler',{})):
                supplied=inputs(Path(root));cast(dict,supplied[domain])['explicit_values'][key]=value
                self.assertNotIsInstance(resolve_daily_configuration(*supplied),DailyConfigurationOk)


class DailyConfigurationPersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_maximum_publication_original_receipt_and_reopen(self):
        from companion_memory.configuration.daily_persistence import DailyConfigurationAssembly
        from companion_memory.configuration.daily_persistent_results import ConfigurationCommitted
        from companion_memory.persistence import PersistenceService,DatabaseResources,Ready
        from companion_memory.persistence.command_capacity import declared_capacity
        from companion_memory.persistence._codec import command_descriptor
        from companion_memory.persistence.schema import encode_value,freeze_value
        from types import MappingProxyType
        from .configuration_support import maximum_inputs
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve();supplied=maximum_inputs(root)
            parsed=resolve_daily_configuration(*supplied)
            if type(parsed) is not DailyConfigurationOk:raise AssertionError(parsed)
            value=parsed.value;original=None
            payload=candidate_values(value)
            self.assertEqual(sum(len(e['body'].encode()) for d in payload['domains'] for e in d['entries']),524288)
            for mode in ('CREATE_NEW','OPEN_EXISTING'):
                assembly=DailyConfigurationAssembly();definition=assembly.commands[0]
                storage=PersistenceService(assembly.repositories,assembly.commands,assembly_format='DAILY_COGNITION_V1')
                resources=DatabaseResources('daily-database',lambda identity,path:identity=='daily-database' and path==str(root/'database'/'runtime.sqlite3'))
                self.assertEqual(declared_capacity(definition),2097152)
                frozen=MappingProxyType({'definition':command_descriptor(definition),'values':freeze_value(definition.input_schema,payload),
                    'intentions':MappingProxyType({'configuration_initialized':MappingProxyType({'actor':'bootstrap'})})})
                carrier=encode_value(frozen,2097152)
                self.assertGreater(len(carrier),1048576)
                self.assertIsInstance(await storage.initialize(value.foundation,resources,mode),Ready)
                binding=assembly.bind(storage,'instance',value)
                try:
                    result=await binding.persist_daily_configuration('original',value,actor='bootstrap',protected_directories=supplied[6])
                    if type(result) is not ConfigurationCommitted:raise AssertionError(result)
                    self.assertIsNotNone(result.configuration,result)
                    if original is None:original=result.receipt
                    else:self.assertEqual(result.receipt,original)
                    print({'mode':mode,'entries':130,'body_bytes':524288,'carrier_bytes':len(carrier),'commit_id':result.receipt.commit_id})
                finally:
                    self.assertTrue(binding.close())
                    await storage.close()
                    self.assertEqual(storage.get_health().lifecycle,'CLOSED')


class DailyCapacityTests(unittest.IsolatedAsyncioTestCase):
    async def test_exact_static_carrier_saved_read_and_reopened(self):
        """A synthetic codec/storage probe, not the complete daily owner graph."""
        from companion_memory.configuration.daily_persistence import DailyConfigurationAssembly
        from companion_memory.persistence import PersistenceService,DatabaseResources,Ready,RepositoryDefinition,TableDefinition
        from companion_memory.persistence._codec import assembly_value,valid_assembly_encoding
        from companion_memory.persistence.schema import InvalidValue,ValueTooLarge
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve();value,_=candidate(root)
            a=DailyConfigurationAssembly()
            def repositories(padding):
                table=TableDefinition('daily_capacity_probe','CREATE TABLE daily_capacity_probe (scope_id TEXT NOT NULL CHECK(1 /*'+padding+'*/))')
                return a.repositories+(RepositoryDefinition('capacity_probe',1,(table,),()),)
            minimum=len(assembly_value(repositories(''),a.commands,assembly_format='DAILY_COGNITION_V1'))
            padded=repositories('x'*(8388608-minimum))
            encoded=assembly_value(padded,a.commands,assembly_format='DAILY_COGNITION_V1')
            self.assertEqual(len(encoded),8388608)
            self.assertTrue(valid_assembly_encoding(encoded,'DAILY_COGNITION_V1'))
            self.assertFalse(valid_assembly_encoding(encoded,'ASYNC_SEMANTIC_V1'))
            with self.assertRaises((InvalidValue,ValueTooLarge)):
                assembly_value(repositories('x'*(8388609-minimum)),a.commands,assembly_format='DAILY_COGNITION_V1')
            for mode in ('CREATE_NEW','OPEN_EXISTING'):
                storage=PersistenceService(padded,a.commands,assembly_format='DAILY_COGNITION_V1')
                resources=DatabaseResources('daily-capacity',lambda identity,path:identity=='daily-capacity' and path==str(root/'database'/'runtime.sqlite3'))
                self.assertIsInstance(await storage.initialize(value.foundation,resources,mode),Ready)
                await storage.close()
                self.assertEqual(storage.get_health().lifecycle,'CLOSED')
            print({'scope':'SYNTHETIC_STATIC_STORAGE_PROBE','static_bytes':len(encoded),'whole_daily_host':False})

    async def test_daily_exception_requires_original_native_command_identity(self):
        from dataclasses import replace
        from companion_memory.configuration.daily_persistence import DailyConfigurationAssembly
        from companion_memory.persistence.command_capacity import declared_capacity
        from companion_memory.persistence._codec import assembly_value
        from companion_memory.persistence.schema import InvalidValue
        a=DailyConfigurationAssembly();d=a.commands[0]
        self.assertEqual(declared_capacity(d),2097152)
        for changed in (replace(d),replace(d,operation_kind='arbitrary')):
            with self.assertRaises(InvalidValue):declared_capacity(changed)
        for format in ('LEGACY','LOCAL_INFORMATION_V1','MODEL_TEXT_LEARNING_V1','ASYNC_SEMANTIC_V1'):
            with self.assertRaises(InvalidValue):assembly_value(a.repositories,a.commands,assembly_format=format)
