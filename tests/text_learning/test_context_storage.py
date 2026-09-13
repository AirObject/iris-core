"""Actual context transaction atomicity with original-key release and reopening.

The fixture command grants only cognition writes to exercise its participant.
It is not the public runtime command or evidence of batch finalization.
"""
from pathlib import Path
from copy import deepcopy
import hashlib
from types import MappingProxyType
import sqlite3
import tempfile
import time
from typing import cast
import unittest
from companion_memory.cognition.text_context import context_catalog,freeze_context,restore_context,FrozenContext
from companion_memory.persistence.content_codec import encode_content
from companion_memory.persistence.schema import InvalidValue
from companion_memory.ingress.events import canonical_event
from companion_memory.ingress.media_events import isolate_media_event
from companion_memory.persistence.service import OperationPort
from companion_memory.cognition.context_storage import ContextStorage,StoredContext,ReleasedContext
from companion_memory.configuration.text_persistence import TextConfigurationAssembly
from companion_memory.configuration.text_persistent_results import ConfigurationCommitted
from companion_memory.persistence import PersistenceService,DatabaseResources,Ready,Committed,ResultBoundCommand,ResultBoundCommandDefinition,RecordSchema,Field
from companion_memory.persistence.text_results import result_schema,audits,INTENT,result
from companion_memory.persistence.text_records import ID
from tests.cognition.test_text_context import inputs
from tests.text_learning.configuration_support import candidate
from tests.persistence.support import Hooks,sqlite_fault


class ContextStorageTests(unittest.IsolatedAsyncioTestCase):
    async def test_whole_context_stage_release_audit_and_receipt_are_atomic_and_reopenable(self):
        for failing in (None,'INSERT INTO cognition_learning_context_leaves','INSERT INTO audit_records','INSERT INTO operation_receipts','COMMIT'):
            with self.subTest(failing=failing),tempfile.TemporaryDirectory(prefix='text-context-store-') as directory:
                root=Path(directory).resolve();configuration,supplied=candidate(root);config=TextConfigurationAssembly();catalog=context_catalog()
                hooks=Hooks();context_owner: ContextStorage | None=None;frozen: FrozenContext | None=None;lease=None;publisher=None
                args=inputs()
                # Preserve the normal prompt while exercising full event bodies
                # whose request exceeds the separate identity metadata budget.
                template=args[0]['user']['members'][0];members=[]
                for ordinal,role in enumerate(('HISTORY','TARGET','TARGET','RECENT')):
                    member=deepcopy(template)
                    member['member'].update(role=role,message_id='message-'+str(ordinal))
                    event=member['event'];event.update(client_event_key='event-'+str(ordinal),body='\x00'*250)
                    event_value=isolate_media_event(event,2048,occurrence_limit=2,text_limit=512)
                    member['member']['payload_digest']=hashlib.sha256(canonical_event(event_value)).hexdigest()
                    members.append(member)
                args[0]['user']['members']=members
                def handler(uow,values):
                    assert context_owner is not None and frozen is not None
                    if values['action']=='stage':
                        context_owner.stage(uow,frozen,args[-1]);revision=1;rows=1+len(frozen.leaves);previous=None
                    else:
                        revision,deleted=context_owner.release(uow,str(frozen.manifest['object_id']),{'owner_namespace':'runtime',
                            'operation_kind':'fixture_context_terminal','scope_id':'instance','operation_key':values['operation_id']})
                        rows=1+deleted;previous=1
                    refs=({'kind':'CONTEXT','object_id':frozen.manifest['object_id'],'revision':revision},)
                    return result(str(values['operation_id']),'SAVED',{'cognition':{'rows_changed':rows,'references':refs}},refs,
                        ({'object_id':frozen.manifest['object_id'],'previous_revision':previous,'revision':revision},))
                requirements,bindings=audits('fixture_context_transaction',('cognition',))
                definition=ResultBoundCommandDefinition('cognition','fixture_context_transaction',1,RecordSchema((Field('operation_id',ID),Field('action',ID))),
                    1,result_schema(('cognition',),('SAVED',)),(catalog.definition,),requirements,handler,INTENT,bindings)
                storage=PersistenceService(config.repositories+(catalog.definition,),config.commands+(definition,),assembly_format='MODEL_TEXT_LEARNING_V1')
                resources=DatabaseResources('context-database',lambda identity,path:identity=='context-database' and path==str(root/'database'/'runtime.sqlite3'),connect=hooks.connect)
                async def initialize(mode) -> OperationPort:
                    nonlocal publisher,context_owner,lease,frozen
                    self.assertIs(type(await storage.initialize(configuration.foundation,resources,mode)),Ready)
                    publisher=config.bind(storage,'instance',configuration)
                    published=await publisher.persist_text_learning_configuration('configuration',configuration,actor='fixture',protected_directories=supplied[6])
                    assert type(published) is ConfigurationCommitted and published.configuration is not None,published
                    context_owner=ContextStorage(catalog,storage,published.configuration,'instance');lease=storage.claim_module_owner(catalog.definition)
                    args[0]['user']['identity'].update(database_id=published.configuration.database_id,config_snapshot_id=published.configuration.snapshot_id)
                    args[0]['model_binding']['config_snapshot_id']=published.configuration.snapshot_id
                    frozen=freeze_context(*args)
                    self.assertGreater(len(encode_content(frozen.request,131072)),8192)
                    return storage.bind_operation(definition,'instance')
                async def close():
                    if lease is not None:lease.release()
                    if publisher is not None:publisher.close()
                    await storage.close()
                    if publisher is not None:publisher.close()
                try:
                    operation=await initialize('CREATE_NEW');assert frozen is not None and context_owner is not None
                    def command(action):return ResultBoundCommand(1,{'operation_id':action,'action':action},{'cognition_text_learning':{'actor':'fixture'}})
                    invoked=[]
                    def before(sql):
                        if failing is not None and sql.startswith(failing):invoked.append(sql);raise sqlite_fault(sqlite3.SQLITE_FULL)
                    hooks.before=before
                    stored=await operation.execute('stage',command('stage'))
                    if failing:
                        self.assertTrue(invoked);self.assertIsNot(type(stored),Committed,stored)
                        with sqlite3.connect(root/'database'/'runtime.sqlite3') as db:
                            self.assertEqual(tuple(db.execute('SELECT count(*) FROM '+table).fetchone()[0] for table in ('cognition_learning_contexts','cognition_learning_context_leaves')),(0,0))
                        continue
                    self.assertIs(type(stored),Committed,stored)
                    loaded=await context_owner.load(str(frozen.manifest['object_id']),time.monotonic()+3)
                    self.assertIs(type(loaded),StoredContext);assert type(loaded) is StoredContext
                    self.assertEqual(restore_context(loaded.manifest,loaded.leaves,frozen.request,args[-1]),frozen)
                    again=await operation.execute('stage',command('stage'));assert type(again) is Committed and type(stored) is Committed
                    self.assertEqual(again.receipt,stored.receipt)
                    # A canonical physical row with a stale digest must not be
                    # consumed as a successful context release or read.
                    first=frozen.leaves[0]
                    damaged=MappingProxyType({**first,'text':str(first['text']).replace('context_version','context_versioN',1)})
                    self.assertNotEqual(damaged['text'],first['text'])
                    with sqlite3.connect(root/'database'/'runtime.sqlite3') as db:
                        before_counts=tuple(db.execute('SELECT count(*) FROM '+table).fetchone()[0] for table in ('audit_records','operation_receipts'))
                        db.execute('UPDATE cognition_learning_context_leaves SET body=? WHERE object_id=?',
                            (encode_content(damaged,8192).decode(),first['object_id']))
                    with self.assertRaises(InvalidValue):
                        await context_owner.load(str(frozen.manifest['object_id']),time.monotonic()+3)
                    self.assertIsNot(type(await operation.execute('damaged-release',command('release'))),Committed)
                    with sqlite3.connect(root/'database'/'runtime.sqlite3') as db:
                        self.assertEqual(tuple(db.execute('SELECT count(*) FROM '+table).fetchone()[0] for table in ('audit_records','operation_receipts')),before_counts)
                        self.assertEqual(db.execute('SELECT count(*) FROM cognition_learning_context_leaves').fetchone()[0],len(frozen.leaves))
                        db.execute('UPDATE cognition_learning_context_leaves SET body=? WHERE object_id=?',
                            (encode_content(first,8192).decode(),first['object_id']))
                    # Deletion, tombstone, audit and receipt each roll back as one.
                    for release_point in ('DELETE FROM cognition_learning_context_leaves','UPDATE cognition_learning_contexts','INSERT INTO audit_records','INSERT INTO operation_receipts','COMMIT'):
                        seen=[]
                        def fail_release(sql):
                            if sql.startswith(release_point):seen.append(sql);raise sqlite_fault(sqlite3.SQLITE_CONSTRAINT)
                        hooks.before=fail_release
                        failed=await operation.execute('release',command('release'))
                        self.assertTrue(seen,release_point);self.assertIsNot(type(failed),Committed,failed)
                        hooks.before=lambda sql:None
                        loaded=await context_owner.load(str(frozen.manifest['object_id']),time.monotonic()+3)
                        self.assertIs(type(loaded),StoredContext)
                    hooks.before=lambda sql:None
                    released=await operation.execute('release',command('release'));self.assertIs(type(released),Committed,released)
                    loaded=await context_owner.load(str(frozen.manifest['object_id']),time.monotonic()+3);self.assertIs(type(loaded),ReleasedContext)
                    await close()
                    # A new service reconstructs the original static declarations;
                    # no missing leaf is interpreted as permission to restage it.
                    config=TextConfigurationAssembly()
                    storage=PersistenceService(config.repositories+(catalog.definition,),config.commands+(definition,),assembly_format='MODEL_TEXT_LEARNING_V1')
                    operation=await initialize('OPEN_EXISTING');assert context_owner is not None and frozen is not None
                    replay=await operation.execute('release',command('release'));assert type(replay) is Committed and type(released) is Committed
                    self.assertEqual(replay.receipt,released.receipt)
                    self.assertIs(type(await context_owner.load(str(frozen.manifest['object_id']),time.monotonic()+3)),ReleasedContext)
                finally:await close()
