"""Independent text declarations preserve every unchanged content command byte.

These checks cover the content participant, not the still separate complete
host. A static declaration comparison does not attest model or user quality.
"""
import unittest
from companion_memory.media.service import MediaService
from companion_memory.persistence._codec import command_descriptor,assembly_value
from companion_memory.persistence.content_codec import encode_content
from companion_memory.runtime.content_assembly import ContentAssembly
from companion_memory.configuration.text_persistence import TextConfigurationAssembly


class TextContentAssemblyTests(unittest.TestCase):
    def test_only_four_learning_command_descriptors_change_and_six_tables_are_added(self):
        old_media=MediaService();new_media=MediaService()
        original=ContentAssembly(old_media,old_media.repositories,information_format=True)
        text=ContentAssembly(new_media,new_media.repositories,information_format=True,text_format=True)
        expected={'associate_content_request','store_content_candidate','commit_content_published','commit_content_without_objects'}
        changed=set()
        for name,before in original._semantic_definitions.items():
            after=text.command_definition(name)
            if encode_content(command_descriptor(before),1048576)!=encode_content(command_descriptor(after),1048576):changed.add(name)
        self.assertEqual(changed,expected)
        self.assertEqual(set(text._semantic_definitions)-set(original._semantic_definitions),{'stage_learning_context'})
        old_tables={t.name for r in original.repositories for t in r.tables if t.sql.startswith('CREATE TABLE')}
        new_tables={t.name for r in text.repositories for t in r.tables if t.sql.startswith('CREATE TABLE')}
        self.assertEqual(new_tables-old_tables,{'memory_initial_self_inputs','cognition_learning_contexts','cognition_learning_context_leaves',
            'self_model_initial_persona_runs','self_model_initial_persona_candidates','self_model_persona_publications'})
        self.assertTrue(old_tables<=new_tables)
        configuration=TextConfigurationAssembly()
        self.assertLess(len(assembly_value(configuration.repositories+text.repositories,configuration.commands+text.commands,
            assembly_format='MODEL_TEXT_LEARNING_V1')),2760704)

    def test_text_format_requires_explicit_information_composition(self):
        with self.assertRaises(ValueError):ContentAssembly(text_format=True)


class TextCombinationTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_native_declarations_and_maximum_configuration_open_with_original_bytes(self):
        import hashlib
        from pathlib import Path
        import sqlite3
        from tempfile import TemporaryDirectory
        from companion_memory.runtime.text_assembly import TextLearningAssembly
        from companion_memory.persistence import DatabaseResources,Ready
        from companion_memory.configuration.text_resolution import resolve_text_learning_configuration,TextConfigurationOk
        from companion_memory.configuration.text_persistent_results import ConfigurationCommitted
        from companion_memory.configuration.text_codec import candidate_values
        from tests.text_learning.configuration_support import maximum_inputs
        from tests.information.host_support import host
        with TemporaryDirectory() as directory:
            root=Path(directory).resolve();supplied,_=maximum_inputs(root)
            parsed=resolve_text_learning_configuration(*supplied);assert type(parsed) is TextConfigurationOk
            candidate=parsed.value
            old=host(root)
            old_commands={(c.owner_namespace,c.operation_kind):encode_content(command_descriptor(c),1048576) for c in old.storage._commands}
            expected_changed={('provider',kind) for kind in ('register','prepare','settle','terminate','recover','evidence','initialize_budget')}
            expected_changed|={('runtime',kind) for kind in ('change_content_mode','information_associate_content_request','information_store_content_candidate',
                'information_commit_content_published','information_commit_content_without_objects')}
            receipt=None;static=None
            for mode in ('CREATE_NEW','OPEN_EXISTING'):
                assembly=TextLearningAssembly();self.assertEqual(len(assembly.commands),104)
                actual={(c.owner_namespace,c.operation_kind):encode_content(command_descriptor(c),1048576) for c in assembly.commands}
                changed={key for key in old_commands.keys() & actual.keys() if old_commands[key]!=actual[key]}
                self.assertEqual(changed,expected_changed)
                self.assertEqual(len(old_commands.keys() & actual.keys())-len(changed),81)
                self.assertEqual(old_commands.keys()-actual.keys(),{('configuration','initialize_information')})
                self.assertEqual(len(actual.keys()-old_commands.keys()),11)
                self.assertLessEqual(len(assembly.static_carrier),2760704)
                self.assertLessEqual(sum(map(len,actual.values())),2621440)
                if static is None:static=assembly.static_carrier
                else:self.assertEqual(assembly.static_carrier,static)
                resources=DatabaseResources('full-text-combination',lambda identity,path:identity=='full-text-combination' and path==str(root/'database'/'runtime.sqlite3'))
                self.assertIs(type(await assembly.storage.initialize(candidate.foundation,resources,mode)),Ready)
                publisher=assembly.configuration.bind(assembly.storage,'instance',candidate)
                try:
                    saved=await publisher.persist_text_learning_configuration('configuration',candidate,actor='bootstrap',protected_directories=supplied[6])
                    self.assertIs(type(saved),ConfigurationCommitted,saved);assert type(saved) is ConfigurationCommitted and saved.configuration is not None
                    self.assertEqual(candidate_values(saved.configuration.candidate),candidate_values(candidate))
                    if receipt is None:receipt=saved.receipt
                    else:self.assertEqual(saved.receipt,receipt);self.assertEqual(saved.source,'EXISTING')
                    with sqlite3.connect(root/'database'/'runtime.sqlite3') as connection:
                        declared={table.name for repository in assembly.repositories for table in repository.tables if table.sql.startswith('CREATE TABLE')}
                        actual_tables={row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                        self.assertTrue(declared<=actual_tables)
                finally:
                    publisher.close();await assembly.storage.close();publisher.close()
            assert static is not None
            print({'source':'ACTUAL_TEXT_COMBINATION','commands':104,'unchanged_commands':81,'replaced_commands':13,
                'static_bytes':len(static),'static_sha256':hashlib.sha256(static).hexdigest()})
