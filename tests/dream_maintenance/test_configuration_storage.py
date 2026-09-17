"""Store complete new configuration and recover its identical original receipt."""
import tempfile
import unittest
import json
import sqlite3
from hashlib import sha256
from pathlib import Path
from companion_memory.configuration.dream_codec import candidate_values
from companion_memory.configuration.dream_resolution import resolve_dream_configuration,DreamConfigurationOk
from companion_memory.configuration.dream_persistence import DreamConfigurationAssembly
from companion_memory.configuration.dream_persistent_results import ConfigurationCommitted
from companion_memory.persistence import PersistenceService,DatabaseResources,Ready
from companion_memory.persistence._codec import assembly_value,valid_assembly_encoding
from .configuration_support import maximum_inputs


class DreamConfigurationStorageTests(unittest.IsolatedAsyncioTestCase):
    async def test_maximum_configuration_initializes_complete_native_host(self):
        from companion_memory.runtime.daily_host import DailyCognitionHost
        from companion_memory.persistence import Found
        from .host_support import make_dream_host
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);supplied=maximum_inputs(root);parsed=resolve_dream_configuration(*supplied)
            if type(parsed) is not DreamConfigurationOk:raise AssertionError(parsed)
            credentials=[];resources=make_dream_host(root,9,credentials).resources;original=None
            for mode in ('CREATE_NEW','OPEN_EXISTING'):
                host=DailyCognitionHost(parsed.value,resources)
                host.configure_entry('entry','partition',('self',),({'kind':'REAL','context_id':None},))
                try:
                    self.assertIs(type(await host.initialize(mode)),Found)
                    if host.stored is None:raise AssertionError('Missing complete configuration')
                    stored=host.stored.snapshot_id
                    if original is None:original=stored
                    else:self.assertEqual(stored,original)
                    assembly=host.combination
                    encoded=assembly.storage._assembly
                    self.assertLessEqual(len(encoded),8388608)
                    declared=json.loads(encoded)
                    repositories=[{'owner':r['owner'],'version':r['version'],'tables':[t['name'] for t in r['tables']]} for r in declared['repositories']]
                    commands=[{'owner':c['owner'],'kind':c['kind'],'version':c['version'],'participants':c['participants'],
                        'declaration_sha256':sha256(json.dumps(c,sort_keys=True,separators=(',',':')).encode()).hexdigest()} for c in declared['commands']]
                    with sqlite3.connect(root/'database/runtime.sqlite3') as db:
                        physical=[{'type':row[0],'name':row[1],'table':row[2],'sql':row[3]} for row in db.execute(
                            "SELECT type,name,tbl_name,sql FROM sqlite_schema WHERE type IN ('table','index') ORDER BY type,name")]
                    print(json.dumps({'scope':'COMPLETE_NATIVE_DREAM_HOST','mode':mode,'configuration_body_bytes':524288,
                        'static_bytes':len(encoded),'static_sha256':sha256(encoded).hexdigest(),'repositories':repositories,
                        'commands':commands,'physical_tables_and_indexes':physical,'credential_resolutions':len(credentials)},ensure_ascii=False))
                    self.assertFalse(credentials)
                finally:self.assertTrue(await host.close())

    async def test_maximum_configuration_persists_and_reopens_with_original_key(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve();supplied=maximum_inputs(root)
            parsed=resolve_dream_configuration(*supplied)
            if type(parsed) is not DreamConfigurationOk:raise AssertionError(parsed)
            value=parsed.value;original=None;payload=candidate_values(value)
            self.assertEqual(sum(len(e['body'].encode()) for d in payload['domains'] for e in d['entries']),524288)
            for mode in ('CREATE_NEW','OPEN_EXISTING'):
                assembly=DreamConfigurationAssembly()
                encoded=assembly_value(assembly.repositories,assembly.commands,assembly_format='DREAM_MAINTENANCE_V1')
                self.assertTrue(valid_assembly_encoding(encoded,'DREAM_MAINTENANCE_V1'))
                self.assertFalse(valid_assembly_encoding(encoded,'DAILY_COGNITION_V1'))
                storage=PersistenceService(assembly.repositories,assembly.commands,assembly_format='DREAM_MAINTENANCE_V1')
                resources=DatabaseResources('dream-database',lambda identity,path:identity=='dream-database' and path==str(root/'database'/'runtime.sqlite3'))
                self.assertIsInstance(await storage.initialize(value.foundation,resources,mode),Ready)
                binding=assembly.bind(storage,'instance',value)
                try:
                    result=await binding.persist_dream_configuration('original',value,actor='bootstrap',protected_directories=supplied[6])
                    if type(result) is not ConfigurationCommitted:raise AssertionError(result)
                    self.assertIsNotNone(result.configuration,result)
                    if original is None:original=result.receipt
                    else:self.assertEqual(result.receipt,original)
                    print({'scope':'CONFIGURATION_OWNER_ONLY','mode':mode,'entries':136,'body_bytes':524288,
                        'command_carrier_limit':2097152,'commit_id':result.receipt.commit_id,'model_sends':0})
                finally:
                    self.assertTrue(binding.close())
                    await storage.close()
                    self.assertEqual(storage.get_health().lifecycle,'CLOSED')
