"""Actual current daily owner graph, initialization audits and exact reopen.

This test uses its real repositories and command descriptors. Whole-host business
and maximum reasoning material acceptance require separate complete host tests.
"""
from pathlib import Path
from tempfile import TemporaryDirectory
from types import MappingProxyType
import sqlite3
import unittest
from companion_memory.runtime.daily_assembly import DailyAssembly
from companion_memory.configuration.daily_resolution import resolve_daily_configuration,DailyConfigurationOk
from companion_memory.configuration.daily_codec import candidate_values
from companion_memory.configuration.daily_persistent_results import ConfigurationCommitted
from companion_memory.persistence import DatabaseResources,Ready,Committed
from companion_memory.persistence._codec import command_descriptor
from companion_memory.persistence.schema import encode_value,freeze_value
from .configuration_support import maximum_inputs

class DailyAssemblyTests(unittest.IsolatedAsyncioTestCase):
    async def test_complete_host_maximum_configuration_import_and_original_reopen(self):
        from .test_host import make_host
        from companion_memory.persistence import Found
        from hashlib import sha256
        import json
        with TemporaryDirectory() as directory:
            root=Path(directory);supplied=maximum_inputs(root);credentials=[];original=None;static=None
            for mode in ('CREATE_NEW','OPEN_EXISTING'):
                host=make_host(root,9,credentials,configuration_input=supplied)
                try:
                    opened=await host.initialize(mode)
                    self.assertIs(type(opened),Found,opened);self.assertEqual(host.state,'READY')
                    self.assertFalse(host.scheduling());self.assertFalse(credentials)
                    if host.runtime is None:raise AssertionError('Complete runtime missing')
                    self.assertIs(host.runtime.provider,host.provider);self.assertIs(host.runtime.embedding,host.provider)
                    definition=host.combination.configuration.commands[0]
                    values=candidate_values(host.configuration)
                    carrier=encode_value(MappingProxyType({'definition':command_descriptor(definition),
                        'values':freeze_value(definition.input_schema,values),
                        'intentions':MappingProxyType({r.event_slot:MappingProxyType({'actor':'daily_cognition'}) for r in definition.required_audits})}),2097152)
                    body_bytes=sum(len(e['body'].encode()) for d in values['domains'] for e in d['entries'])
                    self.assertEqual(body_bytes,524288);self.assertGreater(len(carrier),1048576)
                    with sqlite3.connect(root/'database'/'runtime.sqlite3') as db:
                        tables=[v[0] for v in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
                        indices=[v[0] for v in db.execute("SELECT name FROM sqlite_master WHERE type='index' ORDER BY name")]
                        declaration=db.execute('SELECT assembly FROM application_metadata').fetchone()[0]
                        audits=db.execute('SELECT count(*) FROM audit_records').fetchone()[0]
                        requests=db.execute('SELECT count(*) FROM provider_requests').fetchone()[0]
                        self.assertEqual(requests,0)
                    self.assertLessEqual(len(declaration),8388608)
                    if original is None:original=audits;static=declaration
                    else:self.assertEqual(audits,original);self.assertEqual(declaration,static)
                    print(json.dumps({'acceptance':'complete_maximum_host','mode':mode,'state':host.state,
                        'configuration_body_bytes':body_bytes,'configuration_carrier_bytes':len(carrier),
                        'static_bytes':len(declaration),'static_sha256':sha256(declaration).hexdigest(),
                        'tables':tables,'indices':indices,'commands':[{'module':d.owner_namespace,'kind':d.operation_kind} for d in host.combination.commands],
                        'configuration_domains':{d['domain_id']:sorted(e['parameter_key'] for e in d['entries']) for d in values['domains']},
                        'actual_audits':audits,'requests':requests,'credential_resolutions':len(credentials)},ensure_ascii=False))
                finally:self.assertTrue(await host.close())

    async def test_real_current_declarations_maximum_configuration_and_original_roots_reopen(self):
        with TemporaryDirectory() as directory:
            root=Path(directory);supplied=maximum_inputs(root)
            parsed=resolve_daily_configuration(*supplied)
            if type(parsed) is not DailyConfigurationOk:raise AssertionError(parsed)
            config=parsed.value;path=root/'database'/'runtime.sqlite3';original=None
            for mode in ('CREATE_NEW','OPEN_EXISTING'):
                assembly=DailyAssembly();storage=assembly.storage
                try:
                    initialized=await storage.initialize(config.foundation,DatabaseResources('daily-assembly',lambda i,p:i=='daily-assembly' and p==str(path)),mode)
                    if type(initialized) is not Ready:raise AssertionError(initialized)
                    binding=assembly.configuration.bind(storage,'instance',config)
                    published=await binding.persist_daily_configuration('configuration',config,actor='bootstrap',protected_directories=supplied[6])
                    if type(published) is not ConfigurationCommitted or published.configuration is None:raise AssertionError(published)
                    definition=assembly.configuration.commands[0]
                    carrier=encode_value(MappingProxyType({'definition':command_descriptor(definition),
                        'values':freeze_value(definition.input_schema,candidate_values(config)),
                        'intentions':MappingProxyType({r.event_slot:MappingProxyType({'actor':'bootstrap'}) for r in definition.required_audits})}),2097152)
                    self.assertGreater(len(carrier),1048576);self.assertLessEqual(len(carrier),2097152)
                    self.assertEqual(tuple(r.owner_module for r in definition.required_audits),('configuration',))
                    roots=await binding.initialize_business_roots('business-roots',published.configuration)
                    self.assertIs(type(roots),Committed,roots)
                    self.assertEqual(assembly.ledger.repository.definition.schema_version,5)
                    self.assertEqual(len({r.owner_module for r in assembly.repositories}),len(assembly.repositories))
                    if original is None:original=published.receipt
                    else:self.assertEqual(published.receipt,original)
                    with sqlite3.connect(path) as db:
                        self.assertEqual(db.execute('SELECT count(*) FROM audit_records').fetchone()[0],3)
                        self.assertEqual(db.execute('SELECT count(*) FROM provider_requests').fetchone()[0],0)
                        tables=tuple(row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"))
                    self.assertIn('goals_semantic_decisions',tables);self.assertIn('memory_subject_origins',tables)
                    print({'mode':mode,'real_current_tables':len(tables),'real_current_commands':len(assembly.commands),'configuration_carrier_bytes':len(carrier),'initialization_audits':3,'requests':0})
                    self.assertTrue(binding.release_bootstrap_writers());self.assertTrue(binding.close())
                finally:await storage.close()
