"""Actual same-UoW imported publication, source evidence and original confirmation."""
from pathlib import Path
import sqlite3
import time
from tempfile import TemporaryDirectory
import unittest
from typing import cast
from unittest.mock import patch
from companion_memory.configuration.daily_persistence import DailyConfigurationAssembly
from companion_memory.configuration.daily_persistent_results import ConfigurationCommitted
from companion_memory.media.service import MediaService
from companion_memory.memory.initial_self_storage import InitialSelfBinding
from companion_memory.persistence import PersistenceService,DatabaseResources,Ready,Committed,Found,NotCommitted,ResultBoundCommandDefinition
from companion_memory.persistence.schema import InvalidValue
from companion_memory.runtime.content_assembly import ContentAssembly
from companion_memory.self_model.approved_import import ApprovedPersonaImport,ApprovedImportGrant
from companion_memory.self_model.approved_import_evidence import verify_approved_persona,TEXT_UTF8_DIGEST
from .configuration_support import candidate

class ImportFixture:
    """Real daily repositories; no Provider or synthetic model output is installed."""
    def __init__(self,root:Path,mode='CREATE_NEW'):
        self.root=root;self.mode=mode;self.value,self.supplied=candidate(root)
        self.configuration=DailyConfigurationAssembly();self.media=MediaService()
        self.content=ContentAssembly(self.media,self.media.repositories,information_format=True,daily_format=True)
        memory=next(c for c in self.content.catalogs if c.definition.owner_module=='memory')
        self.importer=ApprovedPersonaImport(memory)
        from companion_memory.cognition.daily_material_storage import DailyMaterialStorage
        from companion_memory.runtime.daily_schedule import DailySchedule
        catalogs={c.definition.owner_module:c for c in self.content.catalogs}
        self.materials=DailyMaterialStorage(catalogs['cognition']);self.schedule=DailySchedule(catalogs['runtime'])
        self.storage=PersistenceService(self.configuration.repositories+self.content.repositories+self.importer.repositories,
            self.configuration.commands+self.content.commands+self.media.commands+self.importer.commands+self.materials.commands+self.schedule.commands+self.extra_commands(),assembly_format='DAILY_COGNITION_V1')
        self.path=root/'database'/'runtime.sqlite3';self.config=None
    def extra_commands(self) -> tuple[ResultBoundCommandDefinition,...]:
        return ()
    async def open(self):
        self.check_ready=await self.storage.initialize(self.value.foundation,DatabaseResources('persona-daily',lambda i,p:i=='persona-daily' and p==str(self.path)),self.mode)
        if type(self.check_ready) is not Ready:raise AssertionError(self.check_ready)
        self.config=self.configuration.bind(self.storage,'instance',self.value)
        stored=await self.config.persist_daily_configuration('configuration',self.value,actor='bootstrap',protected_directories=self.supplied[6])
        if type(stored) is not ConfigurationCommitted or stored.configuration is None:raise AssertionError(stored)
        self.media.bind(self.storage,stored.configuration,'instance');self.content.bind(self.storage,stored.configuration,'instance')
        self.materials.bind(self.storage,stored.configuration);self.schedule.bind(self.storage,stored.configuration,lambda:None)
        fixtures=Path(__file__).parent/'fixtures'/'approved_persona'
        evidence=verify_approved_persona((fixtures/'review.md').read_bytes(),(fixtures/'publication.json').read_bytes(),(fixtures/'reconciliation.json').read_bytes())
        self.evidence=evidence
        self.grant=self.importer.bind(self.storage,stored.configuration,self.content.memory,
            InitialSelfBinding('bootstrap','self','Iris','SYNTHETIC_FIXTURE'),evidence,new_synthetic_instance=self.mode=='CREATE_NEW')
        return self
    async def close(self):
        self.importer.close();self.materials.close();self.schedule.close();self.content.close()
        await self.media.close()
        if self.config is not None:self.config.close()
        await self.storage.close()
        if not self.importer.close():raise AssertionError('Import still owns actual work')

class PersonaImportTests(unittest.IsolatedAsyncioTestCase):
    async def test_original_publication_and_self_are_atomic_and_reopen_adds_no_model_attempt(self):
        with TemporaryDirectory() as directory:
            root=Path(directory);f=await ImportFixture(root).open()
            try:
                if f.grant is None:raise AssertionError()
                with self.assertRaises(TypeError):ApprovedImportGrant()
                original_write=f.importer.rows.write
                def fail_publication(name,uow,value,expected=None):
                    if name=='persona_publications':raise ValueError('Controlled failure after SELF and import writes')
                    return original_write(name,uow,value,expected)
                with patch.object(f.importer.rows,'write',side_effect=fail_publication):
                    failed=await f.importer.import_approved_persona(f.grant,'approved-import')
                self.assertIs(type(failed),NotCommitted)
                with sqlite3.connect(f.path) as db:
                    self.assertEqual(db.execute('SELECT count(*) FROM memory_subjects').fetchone()[0],0)
                    self.assertEqual(db.execute('SELECT count(*) FROM self_model_persona_imports').fetchone()[0],0)
                    self.assertEqual(db.execute('SELECT count(*) FROM audit_records').fetchone()[0],1)
                first=await f.importer.import_approved_persona(f.grant,'approved-import')
                self.assertIs(type(first),Committed,first)
                if type(first) is not Committed:raise AssertionError(first)
                current=await f.importer.read_current();self.assertIs(type(current),Found)
                if type(current) is not Found:raise AssertionError()
                import hashlib
                self.assertEqual(hashlib.sha256(cast(str,current.value['text']).encode()).hexdigest(),TEXT_UTF8_DIGEST)
                self.assertEqual(current.value['publication_origin'],'IMPORTED_APPROVED')
                self.assertEqual(current.value['generated_at_us'],f.evidence.original_generated_at_us)
                self.assertEqual(current.value['original_candidate_id'],f.evidence.original_candidate_id)
                from companion_memory.self_model.daily_current import DailyCurrentPersona,query_projection
                from companion_memory.self_model.current import Available
                from companion_memory.runtime.content_gate import ContentGate
                gate=ContentGate(1);gate.publish_mode('NORMAL',1)
                reader=DailyCurrentPersona(f.importer,gate)
                view=await reader.port.read_current(time.monotonic()+5)
                self.assertIs(type(view),Available)
                if type(view) is not Available:raise AssertionError(view)
                projection=query_projection(view.value)
                self.assertEqual(projection['publication_origin'],'IMPORTED_APPROVED')
                self.assertEqual(projection['original_candidate_id'],f.evidence.original_candidate_id)
                self.assertEqual(projection['text'],f.evidence.text)
                reader.close()
                replay=await f.importer.import_approved_persona(f.grant,'approved-import')
                self.assertIs(type(replay),Committed)
                if type(replay) is not Committed:raise AssertionError(replay)
                self.assertEqual(first.receipt,replay.receipt)
                rejected=await f.importer.import_approved_persona(f.grant,'different-import')
                self.assertIs(type(rejected),NotCommitted)
                with sqlite3.connect(f.path) as db:
                    self.assertEqual(db.execute('SELECT count(*) FROM memory_subjects').fetchone()[0],1)
                    self.assertEqual(db.execute('SELECT count(*) FROM self_model_persona_imports').fetchone()[0],1)
                    self.assertEqual(db.execute('SELECT count(*) FROM self_model_persona_publications').fetchone()[0],1)
                    self.assertEqual(db.execute('SELECT count(*) FROM self_model_initial_persona_candidates').fetchone()[0],0)
                    self.assertEqual(db.execute('SELECT count(*) FROM audit_records').fetchone()[0],3)
            finally:await f.close()
            f=await ImportFixture(root,'OPEN_EXISTING').open()
            try:
                self.assertIsNone(f.grant)
                confirmed=await f.importer.confirm_import('approved-import')
                if type(confirmed) is not Committed:raise AssertionError(confirmed)
                self.assertEqual(first.receipt,confirmed.receipt)
                current=await f.importer.read_current()
                if type(current) is not Found:raise AssertionError(current)
                self.assertEqual(current.value['text'],f.evidence.text)
            finally:await f.close()
