"""Late initialization completions cannot reopen admission after host close."""
import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from companion_memory.persistence import Found,PersistenceService
from companion_memory.persistence.owned_statements import OwnerFailure
from companion_memory.configuration.semantic_persistence import SemanticConfigurationBinding
from companion_memory.runtime.content_service import ContentRuntimeService
from companion_memory.retrieval.semantic_recovery import SemanticRecovery
from tests.semantic.test_semantic_host import make_host


class LifecycleRaceTests(unittest.IsolatedAsyncioTestCase):
    async def test_close_during_mode_read_cannot_publish_recovered_mode(self):
        from companion_memory.persistence.owned_statements import BoundStatements
        with TemporaryDirectory() as directory:
            arrived=asyncio.Event();release=asyncio.Event();original=BoundStatements.read
            async def blocked(instance,name,parameters):
                result=await original(instance,name,parameters)
                if name=='mode_get' and not arrived.is_set():
                    arrived.set();await release.wait()
                return result
            host=make_host(Path(directory).resolve(),1)
            with patch.object(BoundStatements,'read',blocked):
                initialize=asyncio.create_task(host.initialize('CREATE_NEW'))
                try:
                    await asyncio.wait_for(arrived.wait(),5)
                    closing=asyncio.create_task(host.close());await asyncio.sleep(.02)
                    assert host.runtime is not None
                    self.assertEqual(host.runtime.state,'CLOSING')
                    release.set()
                    await asyncio.gather(initialize,return_exceptions=True)
                    await closing
                    self.assertNotEqual(host.runtime.state,'READY')
                    self.assertIsNone(host.runtime.gate.information_checkpoint())
                    self.assertIsNone(host.semantic)
                finally:
                    release.set();await asyncio.gather(initialize,return_exceptions=True)
                    self.assertTrue(await host.close())

    async def test_close_is_irreversible_at_each_initialization_owner_barrier(self):
        for owner,name in ((PersistenceService,'initialize'),(SemanticConfigurationBinding,'persist_semantic_configuration'),
                           (ContentRuntimeService,'initialize'),(SemanticRecovery,'run')):
            with self.subTest(owner=owner.__name__),TemporaryDirectory() as directory:
                arrived=asyncio.Event();release=asyncio.Event();original=getattr(owner,name)
                async def blocked(instance,*args,**kwargs):
                    result=await original(instance,*args,**kwargs)
                    arrived.set();await release.wait();return result
                host=make_host(Path(directory).resolve(),1)
                with patch.object(owner,name,blocked):
                    initialize=asyncio.create_task(host.initialize('CREATE_NEW'))
                    try:
                        await asyncio.wait_for(arrived.wait(),5)
                        closing=asyncio.create_task(host.close());await asyncio.sleep(.02)
                        self.assertEqual(host.state,'CLOSING')
                        if host.runtime is not None:
                            self.assertNotEqual(host.runtime.state,'READY')
                            self.assertIsNone(host.runtime.gate.information_checkpoint())
                        release.set()
                        with self.assertRaises(OwnerFailure):await initialize
                        await closing
                        self.assertNotEqual(host.state,'READY');self.assertIsNone(host.semantic)
                        with self.assertRaises(OwnerFailure):await host.initialize('CREATE_NEW')
                        for _ in range(10):
                            if await host.close():break
                            await asyncio.sleep(.01)
                        self.assertEqual(host.state,'CLOSED')
                        self.assertEqual(host.storage.get_health().lifecycle,'CLOSED')
                        if host.embedding is not None:self.assertEqual(host.embedding.executions,0)
                    finally:
                        release.set()
                        await asyncio.gather(initialize,return_exceptions=True)
                        await host.close()
