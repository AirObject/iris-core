"""Exercise cross-database identity and corrupt committed configuration in real SQLite."""
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from types import MappingProxyType
from typing import cast
import unittest
from unittest.mock import patch

from companion_memory.configuration.daily_codec import candidate_values
from companion_memory.configuration.content_codec import dump,entries_digest
from companion_memory.configuration.daily_persistence import DailyConfigurationAssembly,DailyConfigurationBinding,StoredDailyConfiguration
from companion_memory.configuration.daily_persistent_results import ConfigurationCommitted
from companion_memory.configuration.daily_resolution import DailyConfigurationErr,DailyConfigurationOk,resolve_daily_configuration
from companion_memory.persistence import (PersistenceService,DatabaseResources,Ready,CommandDefinition,LocalCommand,RecordSchema,Field,ScalarSchema,NotCommitted,Committed,UnitOfWork)
from companion_memory.persistence.schema import InvalidValue
from .configuration_support import candidate,inputs,maximum_inputs


class Probe:
    """The probe observes a native UoW and deliberately aborts before any write."""
    def __init__(self,root: Path,identity: str,mode='CREATE_NEW',scope='instance'):
        root.mkdir(exist_ok=True)
        self.root=root;self.identity=identity;self.mode=mode;self.scope=scope
        self.value,self.supplied=candidate(root)
        self.assembly=DailyConfigurationAssembly();self.binding:DailyConfigurationBinding|None=None
        self.expected:StoredDailyConfiguration|None=None;self.accepted:bool|None=None
        self.command=CommandDefinition('configuration','snapshot_probe',1,RecordSchema(()),1,RecordSchema(()),self.assembly.repositories,(),self.probe)
        self.storage=PersistenceService(self.assembly.repositories,self.assembly.commands+(self.command,),assembly_format='DAILY_COGNITION_V1')
        self.operation=self.storage.bind_operation(self.command,self.scope)
    def probe(self,uow:UnitOfWork,values):
        if self.binding is None or self.expected is None:raise AssertionError('Missing probe setup')
        self.accepted=self.binding.participate_snapshot(uow,self.expected)
        raise ValueError('Probe intentionally ends with no effects or receipt')
    async def open(self):
        path=self.root/'database'/'runtime.sqlite3'
        resource=DatabaseResources(self.identity,lambda identity,p:identity==self.identity and p==str(path))
        if type(await self.storage.initialize(self.value.foundation,resource,self.mode)) is not Ready:raise AssertionError('Not ready')
        self.binding=self.assembly.bind(self.storage,self.scope,self.value)
        result=await self.binding.persist_daily_configuration('original',self.value,actor='bootstrap',protected_directories=self.supplied[6])
        if type(result) is not ConfigurationCommitted or result.configuration is None:raise AssertionError(result)
        self.expected=result.configuration
        return result
    async def check(self,expected):
        self.expected=expected;self.accepted=None
        result=await self.operation.execute('probe',LocalCommand(1,{},{}))
        if type(result) is not NotCommitted:raise AssertionError(result)
        return self.accepted
    def counts(self):
        with sqlite3.connect(self.root/'database'/'runtime.sqlite3') as reader:
            return tuple(reader.execute('SELECT count(*) FROM '+table).fetchone()[0] for table in ('configuration_entries','audit_records','operation_receipts'))
    async def close(self):
        if self.binding is not None:self.binding.close()
        await self.storage.close()
        if self.binding is not None and not self.binding.close():raise AssertionError('Actual cleanup remains')


class ConfigurationIntegrityTests(unittest.IsolatedAsyncioTestCase):
    async def test_same_snapshot_name_does_not_grant_foreign_database_or_scope(self):
        with TemporaryDirectory() as directory:
            root=Path(directory);left=Probe(root/'left','left-db');right=Probe(root/'right','right-db')
            try:
                first=await left.open();second=await right.open()
                own=first.configuration;foreign=second.configuration
                if own is None or foreign is None:raise AssertionError('Missing config')
                self.assertEqual(own.snapshot_id,foreign.snapshot_id)
                self.assertNotEqual(own.database_id,foreign.database_id)
                before=left.counts()
                self.assertFalse(await left.check(foreign))
                self.assertTrue(await left.check(own))
                self.assertEqual(left.counts(),before)
                if left.binding is None:raise AssertionError()
                left.binding.close()
                self.assertFalse(await left.check(own))
            finally:await left.close();await right.close()
            reopened=Probe(root/'left','left-db','OPEN_EXISTING')
            try:
                again=await reopened.open()
                self.assertEqual(again.receipt,first.receipt)
                self.assertTrue(await reopened.check(again.configuration))
                self.assertEqual(reopened.counts(),(130,1,1))
            finally:await reopened.close()

    async def test_same_database_with_different_scope_rejects_original_native_snapshot(self):
        with TemporaryDirectory() as directory:
            root=Path(directory);first=Probe(root,'scope-db',scope='first-instance')
            try:
                original=await first.open()
            finally:await first.close()
            second=Probe(root,'scope-db','OPEN_EXISTING',scope='second-instance')
            try:
                path=second.root/'database'/'runtime.sqlite3'
                resource=DatabaseResources(second.identity,lambda identity,p:identity==second.identity and p==str(path))
                self.assertIs(type(await second.storage.initialize(second.value.foundation,resource,'OPEN_EXISTING')),Ready)
                second.binding=second.assembly.bind(second.storage,second.scope,second.value)
                if original.configuration is None:raise AssertionError()
                self.assertEqual(original.configuration.database_id,second.identity)
                self.assertEqual(candidate_values(original.configuration.candidate),candidate_values(second.value))
                self.assertNotEqual(original.configuration.scope_id,second.scope)
                before=second.counts()
                self.assertFalse(await second.check(original.configuration))
                self.assertEqual(second.counts(),before)
            finally:await second.close()

    async def test_corrupt_dependency_with_matching_domain_digest_returns_integrity_failure(self):
        with TemporaryDirectory() as directory:
            probe=Probe(Path(directory),'corrupt-db')
            try:
                result=await probe.open();binding=probe.binding
                if binding is None or result.configuration is None:raise AssertionError()
                with sqlite3.connect(probe.root/'database'/'runtime.sqlite3') as writer:
                    domain='daily_cognition';key='runtime.timezone'
                    body=writer.execute('SELECT body FROM configuration_entries WHERE domain_id=? AND parameter_key=?',(domain,key)).fetchone()[0]
                    value=json.loads(body);value['definition']['dependencies']['value'].append('unknown_configuration_dependency')
                    writer.execute('UPDATE configuration_entries SET body=? WHERE domain_id=? AND parameter_key=?',(dump(value),domain,key))
                    rows=tuple(writer.execute('SELECT parameter_key,body FROM configuration_entries WHERE domain_id=? ORDER BY parameter_key',(domain,)))
                    writer.execute('UPDATE configuration_domains SET digest=? WHERE domain_id=?',(entries_digest(rows),domain))
                loaded=await binding.load_daily_configuration(result.configuration.snapshot_id,probe.value.material_contracts,probe.supplied[6],bootstrap=probe.value)
                self.assertIs(type(loaded),DailyConfigurationErr)
                if type(loaded) is not DailyConfigurationErr:raise AssertionError(loaded)
                self.assertEqual((loaded.error.code,loaded.error.field,loaded.error.reason),('INTEGRITY_FAILURE','storage','CONTENT_MISMATCH'))
                replay=await binding.persist_daily_configuration('original',probe.value,actor='bootstrap',protected_directories=probe.supplied[6])
                self.assertIs(type(replay),ConfigurationCommitted)
                if type(replay) is not ConfigurationCommitted:raise AssertionError(replay)
                self.assertEqual(replay.receipt,result.receipt);self.assertIsNone(replay.configuration)
                self.assertFalse(replay.cleanup_pending);self.assertIsNotNone(replay.error)
                self.assertEqual(probe.counts(),(130,1,1))
            finally:await probe.close()

    async def test_committed_publication_retains_receipt_when_whole_domain_freeze_fails(self):
        with TemporaryDirectory() as directory:
            probe=Probe(Path(directory),'read-failure-db')
            try:
                with patch('companion_memory.configuration.daily_resolution.freeze_daily_domains',side_effect=InvalidValue):
                    # Resolution has already produced the complete native bootstrap.
                    path=probe.root/'database'/'runtime.sqlite3'
                    resource=DatabaseResources(probe.identity,lambda identity,p:identity==probe.identity and p==str(path))
                    self.assertIs(type(await probe.storage.initialize(probe.value.foundation,resource,'CREATE_NEW')),Ready)
                    probe.binding=probe.assembly.bind(probe.storage,'instance',probe.value)
                    result=await probe.binding.persist_daily_configuration('original',probe.value,actor='bootstrap',protected_directories=probe.supplied[6])
                self.assertIs(type(result),ConfigurationCommitted)
                if type(result) is not ConfigurationCommitted:raise AssertionError(result)
                self.assertIsNone(result.configuration);self.assertIsNotNone(result.error)
                self.assertFalse(result.cleanup_pending);self.assertEqual(probe.counts(),(130,1,1))
                replay=await probe.binding.persist_daily_configuration('original',probe.value,actor='bootstrap',protected_directories=probe.supplied[6])
                if type(replay) is not ConfigurationCommitted:raise AssertionError(replay)
                self.assertIsNotNone(replay.configuration);self.assertEqual(replay.receipt,result.receipt)
            finally:await probe.close()

    def test_invalid_timezone_and_complete_capacity_have_distinct_errors(self):
        with TemporaryDirectory() as directory:
            supplied=inputs(Path(directory));supplied[5]['explicit_values']['runtime.timezone']='Not/AZone'
            bad=resolve_daily_configuration(*supplied)
            if type(bad) is not DailyConfigurationErr:raise AssertionError(bad)
            self.assertEqual((bad.error.code,bad.error.field,bad.error.reason),('VALUE_INVALID','runtime.timezone','RANGE_INVALID'))
            supplied=maximum_inputs(Path(directory),extra_bytes=1)
            large=resolve_daily_configuration(*supplied)
            if type(large) is not DailyConfigurationErr:raise AssertionError(large)
            self.assertEqual((large.error.code,large.error.field,large.error.reason),('VALUE_INVALID','value','CAPACITY_INSUFFICIENT'))
