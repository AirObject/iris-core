"""Actual atomic SELF/input publication, required audit and original-key replay.

The unrelated learning-source bridge is deliberately unavailable in this fixture;
initial operator input must never invoke or synthesize a conversation source.
"""
from pathlib import Path
import asyncio
import sqlite3
import time
import tempfile
from typing import cast
import unittest
from unittest.mock import patch
from companion_memory.configuration.text_persistence import TextConfigurationAssembly
from companion_memory.configuration.text_persistent_results import ConfigurationCommitted
from companion_memory.persistence import PersistenceService,DatabaseResources,Ready,Committed,Found,NotFound
from companion_memory.persistence.text_records import extend_catalog
from companion_memory.memory.information_repository import information_memory_catalog
from companion_memory.memory.initial_self import initial_self_catalog
from companion_memory.memory.initial_self_storage import InitialSelfBinding,InitialSelfStorage
from companion_memory.memory.initial_self_commands import InitialSelfCommands,InitialSelfPort
from companion_memory.memory.service import MemoryError
from companion_memory.memory.transactions import MemoryTransactions
from companion_memory.memory.sources import SourceParticipants
from companion_memory.logging_service.object_history import history_catalog,HistoryBinding
from tests.persistence.support import Hooks,sqlite_fault
from tests.text_learning.configuration_support import candidate


class InitialSelfStorageTests(unittest.IsolatedAsyncioTestCase):
    async def test_first_self_and_input_publish_once_or_roll_back_together(self):
        for failing in (None,'INSERT INTO memory_initial_self_inputs','INSERT INTO required_audit_events','INSERT INTO audit_records','INSERT INTO operation_receipts','COMMIT'):
            with self.subTest(failing=failing),tempfile.TemporaryDirectory(prefix='initial-self-') as directory:
                root=Path(directory).resolve();configuration,supplied=candidate(root)
                config=TextConfigurationAssembly();catalog=extend_catalog(information_memory_catalog(),initial_self_catalog(),3);history=history_catalog()
                commands=InitialSelfCommands(catalog,lambda:1)
                storage=PersistenceService(config.repositories+(catalog.definition,history.definition),config.commands+commands.commands,assembly_format='MODEL_TEXT_LEARNING_V1')
                hooks=Hooks();publisher=None;memory=None;logging=None
                try:
                    result=await storage.initialize(configuration.foundation,DatabaseResources('self-database',lambda identity,path:identity=='self-database' and path==str(root/'database'/'runtime.sqlite3'),connect=hooks.connect),'CREATE_NEW')
                    self.assertIs(type(result),Ready,result)
                    publisher=config.bind(storage,'instance',configuration)
                    published=await publisher.persist_text_learning_configuration('configuration',configuration,actor='operator',protected_directories=supplied[6])
                    assert type(published) is ConfigurationCommitted and published.configuration is not None,published
                    logging=HistoryBinding(history,storage,'instance',8,8192,configuration.foundation,text_format=True)
                    # This endpoint has no conversation-source effect. A missing
                    # bridge makes any accidental cross-owner call fail the test.
                    memory=MemoryTransactions(catalog,storage,published.configuration,'instance',logging,cast(SourceParticipants,object()))
                    port=commands.bind(memory,InitialSelfBinding('operator','self','Iris','SYNTHETIC_FIXTURE'))
                    assert commands._owner is not None
                    input_id=commands._owner.input_id
                    self.assertIs(type(await port.read_initial(input_id,time.monotonic()+3)),NotFound)
                    self.assertIs(type(await port.read_initial('another-input',time.monotonic()+3)),MemoryError)
                    self.assertIs(type(await object.__new__(InitialSelfPort).read_initial(input_id,time.monotonic()+3)),MemoryError)
                    invoked=[]
                    def before(sql):
                        if failing is not None and sql.startswith(failing):
                            invoked.append(sql);raise sqlite_fault(sqlite3.SQLITE_FULL)
                    hooks.before=before
                    written=await port.register_initial_self('register-self','NO_PRESET','The user chose no preset background.','SYNTHETIC_FIXTURE')
                    if failing is None:
                        self.assertIs(type(written),Committed,written)
                        replay=await port.register_initial_self('register-self','NO_PRESET','The user chose no preset background.','SYNTHETIC_FIXTURE')
                        self.assertIs(type(replay),Committed,replay)
                        assert type(written) is Committed and type(replay) is Committed
                        self.assertEqual(written.receipt,replay.receipt)
                        different=await port.register_initial_self('different-key','NO_PRESET','The user chose no preset background.','SYNTHETIC_FIXTURE')
                        self.assertIsNot(type(different),Committed,different)
                        self.assertIs(type(await port.read_initial(input_id,time.monotonic()+3)),Found)
                        # Actual SQLite reads have ended before this barrier;
                        # withdrawal still wins before delivery and retains the
                        # bounded owner until its real coroutine completes.
                        arrived=asyncio.Event();release=asyncio.Event();original=InitialSelfStorage.read_original
                        async def pause(owner,identity,deadline):
                            value=await original(owner,identity,deadline);arrived.set();await release.wait();return value
                        with patch.object(InitialSelfStorage,'read_original',pause):
                            pending=asyncio.create_task(port.read_initial(input_id,time.monotonic()+3))
                            await asyncio.wait_for(arrived.wait(),3)
                            self.assertIs(type(await port.read_initial(input_id,time.monotonic()+3)),MemoryError)
                            commands.close();self.assertIsNotNone(commands._read_task)
                            release.set();withdrawn=await pending
                            self.assertIs(type(withdrawn),MemoryError);assert type(withdrawn) is MemoryError
                            self.assertEqual(withdrawn.reason,'SERVICE_CLOSED')
                    else:
                        self.assertTrue(invoked,failing);self.assertIsNot(type(written),Committed,written)
                    with sqlite3.connect(root/'database'/'runtime.sqlite3') as connection:
                        counts=tuple(connection.execute('SELECT count(*) FROM '+table).fetchone()[0] for table in ('memory_subjects','memory_initial_self_inputs'))
                        self.assertEqual(counts,(1,1) if failing is None else (0,0))
                finally:
                    commands.close()
                    if memory is not None:memory.close()
                    if logging is not None:logging.close()
                    if publisher is not None:publisher.close()
                    await storage.close()
                    if publisher is not None:publisher.close()
