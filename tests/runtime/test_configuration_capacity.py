"""Structural platform limits do not promise catalog or atomic-command capacity."""
from contextlib import closing
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from types import MappingProxyType
import unittest
from typing import cast
from companion_memory.configuration.persistence import ConfigurationAssembly,_candidate_values
from companion_memory.configuration.persistent_results import ConfigurationCommitted,ConfigurationRejected
from companion_memory.persistence import PersistenceService,DatabaseResources,Ready
from companion_memory.persistence._codec import command_descriptor,receipt_value
from companion_memory.persistence.schema import encode_value,Value
from companion_memory.provider.values import freeze
from tests.runtime.configuration_support import candidate
from tests.persistence.support import Hooks


def command_bytes(assembly:ConfigurationAssembly,values:object) -> int:
    tree=MappingProxyType({'definition':command_descriptor(assembly.commands[0]),'values':freeze(values,4000000),'intentions':MappingProxyType({'configuration_initialized':MappingProxyType({'actor':'bootstrap'})})})
    return len(encode_value(cast(Value,tree),4000000))


class ConfigurationCapacityTests(unittest.IsolatedAsyncioTestCase):
    async def test_short_and_long_identity_catalog_edges_publish_or_reject_atomically(self):
        for length,counts in ((4,(24,25,64)),(128,(17,18,64))):
            for count in counts:
                with self.subTest(length=length,count=count),TemporaryDirectory(prefix='iris-config-capacity-') as directory:
                    root=Path(directory).resolve();# An explicitly larger audit budget isolates the catalog boundary;
                    # this test does not change the ordinary candidate.
                    selected,supplied=candidate(root,platform_ids=tuple(str(i).zfill(length) for i in range(count)),foundation_changes={'audit.event_max_bytes':8192})
                    values=_candidate_values(selected);catalog_size=len(cast(str,values['catalog']).encode())
                    fits=count==counts[0]
                    self.assertEqual(catalog_size<=8192,fits)
                    path=root/'database'/'runtime.sqlite3'
                    hooks=Hooks();statements=[]
                    resources=DatabaseResources('capacity-database',lambda identity,target:identity=='capacity-database' and target==str(path),connect=hooks.connect)
                    for mode in ('CREATE_NEW','OPEN_EXISTING') if fits else ('CREATE_NEW',):
                        assembly=ConfigurationAssembly();storage=PersistenceService(assembly.repositories,assembly.commands);binding=None
                        try:
                            self.assertIs(type(await storage.initialize(selected.foundation,resources,mode)),Ready)
                            binding=assembly.bind(storage,'instance')
                            statements.clear();hooks.before=statements.append
                            result=await binding.persist_initial_configuration('original',selected,actor='bootstrap',protected_directories=supplied[3])
                            if fits:
                                self.assertIs(type(result),ConfigurationCommitted,result)
                                if type(result) is not ConfigurationCommitted or result.configuration is None:self.fail(result)
                                self.assertEqual(len(result.configuration.candidate.platforms),count)
                                self.assertLessEqual(command_bytes(assembly,values),1048576)
                                self.assertLessEqual(len(encode_value(receipt_value(result.receipt),65536)),65536)
                                self.assertEqual(result.source,'NEW' if mode=='CREATE_NEW' else 'EXISTING')
                            else:
                                self.assertEqual(statements,[])
                                self.assertIs(type(result),ConfigurationRejected,result)
                                if type(result) is not ConfigurationRejected:self.fail(result)
                                self.assertEqual((result.error.code,result.error.operation,result.error.field,result.error.reason),('INVALID_INPUT','persist_initial_configuration','value','LIMIT_EXCEEDED'))
                                with closing(sqlite3.connect(path)) as connection:
                                    for table in ('configuration_domains','configuration_entries','configuration_snapshots','active_configuration','operation_receipts','audit_records'):
                                        self.assertEqual(connection.execute('SELECT count(*) FROM '+table).fetchone()[0],0,table)
                        finally:
                            if binding:binding.close()
                            await storage.close()

    async def test_ordinary_audit_budget_is_an_independent_atomic_capacity_limit(self):
        from companion_memory.configuration.persistent_results import ConfigurationNotCommitted
        for count in (12,13):
            with self.subTest(count=count),TemporaryDirectory(prefix='iris-config-audit-capacity-') as directory:
                root=Path(directory).resolve();selected,supplied=candidate(root,platform_ids=tuple(str(i).zfill(4) for i in range(count)))
                assembly=ConfigurationAssembly();storage=PersistenceService(assembly.repositories,assembly.commands);binding=None
                path=root/'database'/'runtime.sqlite3'
                try:
                    self.assertIs(type(await storage.initialize(selected.foundation,DatabaseResources('capacity',lambda identity,target:target==str(path)),'CREATE_NEW')),Ready)
                    binding=assembly.bind(storage,'instance')
                    result=await binding.persist_initial_configuration('original',selected,actor='bootstrap',protected_directories=supplied[3])
                    self.assertIs(type(result),ConfigurationCommitted if count==12 else ConfigurationNotCommitted,result)
                    with closing(sqlite3.connect(path)) as connection:
                        for table in ('configuration_domains','active_configuration','operation_receipts','audit_records'):
                            observed=connection.execute('SELECT count(*) FROM '+table).fetchone()[0]
                            self.assertGreater(observed,0) if count==12 else self.assertEqual(observed,0)
                finally:
                    if binding:binding.close()
                    await storage.close()

    async def test_full_command_limit_is_checked_before_any_configuration_write(self):
        with TemporaryDirectory(prefix='iris-config-command-capacity-') as directory:
            root=Path(directory).resolve()
            selected,supplied=candidate(root,platform_ids=tuple(str(i).zfill(4) for i in range(12)),foundation_changes={'storage.command_max_bytes':262144})
            assembly=ConfigurationAssembly();values=_candidate_values(selected)
            self.assertLess(len(cast(str,values['catalog']).encode()),8192)
            self.assertGreater(command_bytes(assembly,values),262144)
            path=root/'database'/'runtime.sqlite3';storage=PersistenceService(assembly.repositories,assembly.commands);binding=None
            hooks=Hooks();statements=[]
            resources=DatabaseResources('capacity-database',lambda identity,target:identity=='capacity-database' and target==str(path),connect=hooks.connect)
            try:
                self.assertIs(type(await storage.initialize(selected.foundation,resources,'CREATE_NEW')),Ready)
                binding=assembly.bind(storage,'instance')
                hooks.before=statements.append
                rejected=await binding.persist_initial_configuration('original',selected,actor='bootstrap',protected_directories=supplied[3])
                self.assertEqual(statements,[])
                self.assertIs(type(rejected),ConfigurationRejected,rejected)
                if type(rejected) is not ConfigurationRejected:self.fail(rejected)
                self.assertEqual(rejected.error.reason,'LIMIT_EXCEEDED')
                with closing(sqlite3.connect(path)) as connection:
                    self.assertEqual(connection.execute('SELECT count(*) FROM configuration_domains').fetchone()[0],0)
                    self.assertEqual(connection.execute('SELECT count(*) FROM operation_receipts').fetchone()[0],0)
            finally:
                if binding:binding.close()
                await storage.close()
