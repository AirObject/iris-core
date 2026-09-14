"""Actual text-host storage and restart retain identities without resolving secrets.

Only deterministic configuration values and unavailable credentials are used.
No persona is approved here and no supplier request is possible.
"""
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest
from companion_memory.persistence import Found,Committed
from companion_memory.persistence.text_records import stable_identity
from companion_memory.provider.credentials import CredentialResolver,CredentialUnavailable
from companion_memory.memory.formats import record
from tests.text_learning.configuration_support import candidate
from tests.text_learning.host_driver import confirm_local
from .host import assemble
from .materials import INITIAL_SELF


class TrialHostTests(unittest.IsolatedAsyncioTestCase):
    async def test_each_platform_stores_initial_self_and_reopens_without_key_lookup(self):
        identities=[]
        for platform in ('macos','linux'):
            with self.subTest(identity=platform),TemporaryDirectory() as directory:
                root=Path(directory).resolve();configuration,inputs=candidate(root);calls=[]
                def resolve(*arguments):
                    calls.append(arguments);return CredentialUnavailable('UNAVAILABLE')
                resolver=CredentialResolver(resolve)
                for mode in ('CREATE_NEW','OPEN_EXISTING'):
                    host=assemble(configuration,root,platform,inputs[6],resolver)
                    try:
                        opened=await confirm_local(lambda:host.initialize(mode))
                        self.assertIs(type(opened),Found,opened);assert type(opened) is Found
                        self.assertFalse(record(opened.value)['learning_ready']);assert host.stored is not None
                        if mode=='CREATE_NEW':identities.append(host.stored.database_id)
                        port=host.initialization_port()
                        result=await confirm_local(lambda:port.register_initial_self('initial-self','PRESET',INITIAL_SELF,'SYNTHETIC_FIXTURE',time.monotonic()+5))
                        self.assertIs(type(result),Committed,result);assert type(result) is Committed
                        self.assertEqual(result.source,'NEW' if mode=='CREATE_NEW' else 'EXISTING')
                        identity=stable_identity('self-input',host.stored.database_id,host.resources.instance_id)
                        self.assertTrue(identity)
                        self.assertEqual(calls,[])
                    finally:self.assertTrue(await host.close())
                self.assertEqual(calls,[])
        self.assertEqual(len(set(identities)),2)
